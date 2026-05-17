"""FilesystemMandateBackend — scope-root directory-tree reference impl.

This is the default backend for single-host deployments. It reads operator-
authored ``mandates.md`` files from well-known paths under a ``scope_root``
directory and writes framework-internal deduplication state to ``.judge-state/
mandates.json`` sidecars. Both paths are invisible to operators during normal
use — they interact only with the ``mandates.md`` files.

Directory layout::

    <scope_root>/
    │
    ├── mandates.md                        ← project-root mandates
    │                                        (scope "project:<name>")
    ├── .judge-state/
    │   └── mandates.json                  ← project-root dedup state
    │
    ├── <agent_name>/
    │   ├── mandates.md                    ← per-agent mandates
    │   │                                    (scope "agent:<agent_name>")
    │   └── .judge-state/
    │       └── mandates.json              ← per-agent dedup state
    │
    └── <other_agent>/
        ├── mandates.md
        └── .judge-state/
            └── mandates.json

Scope string format:

- ``"agent:<name>"`` — resolves to ``<scope_root>/<name>/mandates.md``.
  The state sidecar is at ``<scope_root>/<name>/.judge-state/mandates.json``.
- ``"project:<name>"`` — resolves to ``<scope_root>/mandates.md`` (the
  project-root file). The state sidecar is at
  ``<scope_root>/.judge-state/mandates.json``. The ``<name>`` component
  is informational (used in the state file's ``"scope"`` field for human
  readability) and does NOT add a directory level.

Three surface promises hold across PR 1 → PR 2:

1. **No mandates.md, no mandates.** When ``mandates.md`` is absent or
   empty, ``list_mandates()`` returns ``[]`` — the backend is fully
   functional with zero operator configuration. Every existing agent
   that has no ``mandates.md`` continues to work unchanged.

2. **Path-traversal refused at the API boundary** (spec/29 MUST #1).
   Operator-controlled ``mandate_id`` and ``scope`` are validated against
   ``/``, ``..``, leading dots, backslash, and control characters BEFORE
   any disk access. The scope string's ``<name>`` portion is resolved via
   ``_io.safe_resolve_under`` against the scope_root so symlink traversal
   is also closed.

3. **State writes are atomic and concurrent-process-safe** (spec/29 MUST #3).
   ``write_state`` delegates to ``_io.atomic_write`` (temp + fsync + rename
   per spec/03 rule #8). Parallel agent processes each write their own
   state file under their own agent directory, so there is no cross-agent
   write contention for agent-scoped state. Project-scoped state is shared
   across agents — operators whose workloads involve many agents racing to
   update project-root state SHOULD set ``high_risk`` on those mandates
   to use the filesystem lock (spec/29 §"High-risk lock specification").

Thread-safety: each method opens and closes its own file handles. The parser
import and JSON encode hold no shared mutable state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .._io import atomic_write, safe_resolve_under
from ..exceptions import PathTraversalError
from .types import (
    Mandate,
    MandateCapabilities,
    MandateInvalid,
    MandateNotFound,
    MandateStateSchemaUnsupported,
    RevocationState,
)

_logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Parser import — wired during PR 1 final integration.
# ``parse_mandates_md(path, *, scope, is_project_root)`` returns
# ``(ProjectMandateMeta | None, list[Mandate])``. See ``mandates_md.py``.
from ..mandates_md import parse_mandates_md as _parse_mandates_md


# ────────────────────────────────────────────────────────────────────────────
# State file constants

_JUDGE_STATE_DIR = ".judge-state"
_STATE_FILENAME = "mandates.json"
_SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
_DEFAULT_SCHEMA_VERSION = 1

# ────────────────────────────────────────────────────────────────────────────
# Scope parsing helpers


def _parse_scope(scope: str) -> tuple[str, str]:
    """Parse a scope string into ``(kind, name)``.

    ``kind`` is ``"agent"`` or ``"project"``. ``name`` is the bare name
    component following the colon.

    Raises ``ValueError`` for malformed scope strings.
    """
    if not scope:
        raise ValueError("scope must not be empty")
    if ":" not in scope:
        raise ValueError(
            f"scope {scope!r} is malformed — expected 'agent:<name>' or "
            f"'project:<name>'"
        )
    kind, _, name = scope.partition(":")
    if kind not in ("agent", "project"):
        raise ValueError(
            f"scope kind {kind!r} is not valid — must be 'agent' or 'project'"
        )
    if not name:
        raise ValueError(
            f"scope {scope!r} has an empty name component — expected "
            f"'agent:<name>' or 'project:<name>' with a non-empty name"
        )
    return kind, name


_MANDATE_ID_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _validate_mandate_id(mandate_id: str) -> None:
    """Reject ``mandate_id`` that violates the spec/29 ID contract.

    Spec/29 line 298 + MUST #1 — mandate IDs are operator-supplied and
    MUST match ``[a-z0-9][a-z0-9-]*`` (lowercase alphanumerics with
    interior hyphens; first character must be alphanumeric; ≤64 chars).
    Validation runs at the API boundary BEFORE any storage access.

    Raises ``MandateInvalid`` for ID-shape violations (spec contract).
    Raises ``ValueError`` for the older traversal-only checks retained
    as defense-in-depth (the regex covers them, but the explicit checks
    surface clearer error messages for common operator mistakes).
    """
    if not mandate_id:
        raise MandateInvalid("mandate_id must not be empty")
    if "/" in mandate_id or "\\" in mandate_id:
        raise MandateInvalid(
            f"mandate_id {mandate_id!r} contains a path separator — "
            f"refused (spec/29 MUST #1; pattern is [a-z0-9][a-z0-9-]*)"
        )
    if mandate_id.startswith("."):
        raise MandateInvalid(
            f"mandate_id {mandate_id!r} starts with '.' — refused to "
            f"prevent hidden-file traversal (spec/29 MUST #1)"
        )
    if ".." in mandate_id:
        raise MandateInvalid(
            f"mandate_id {mandate_id!r} contains '..' — path traversal "
            f"refused (spec/29 MUST #1)"
        )
    for ch in mandate_id:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise MandateInvalid(
                f"mandate_id {mandate_id!r} contains a control character "
                f"(0x{ord(ch):02X}) — refused to prevent log injection "
                f"and path-token splitting (spec/29 MUST #1)"
            )
    # Final pattern enforcement — catches uppercase, special chars, leading
    # hyphen, length >64. Spec/29 line 298: [a-z0-9][a-z0-9-]* and ≤64 chars.
    if not _MANDATE_ID_RE.match(mandate_id):
        raise MandateInvalid(
            f"mandate_id {mandate_id!r} does not match the spec/29 pattern "
            f"[a-z0-9][a-z0-9-]* (max 64 chars, lowercase alphanumerics + "
            f"interior hyphens; first char alphanumeric)"
        )


# ────────────────────────────────────────────────────────────────────────────
# Backend


class FilesystemMandateBackend:
    """Directory-tree ``MandateBackend`` — per-scope ``mandates.md`` walk.

    Conforms to the ``MandateBackend`` Protocol. Constructed once per
    deployment scope; ``scope_root`` is the directory under which both
    per-agent agent directories and the project-root ``mandates.md`` live.

    Args:
        scope_root: root directory from which mandate paths are resolved.
            MUST be an absolute path or a relative path with at least one
            non-trivial component. Empty string, ``"."`` and ``"/"`` are
            refused — they produce ambiguous path semantics silently.
    """

    @property
    def backend_id(self) -> str:
        return "filesystem"

    def __init__(self, scope_root: Path | str) -> None:
        # Refuse empty / trivially-bad scope_roots.  ``"."`` resolves to
        # CWD which is always-changing and would silently scope the backend
        # to whatever directory the process was started from.  ``"/"``
        # would make every path-traversal guard meaningless.  Mirrors the
        # guard in ``FilesystemToolRegistryBackend.__init__``.
        if isinstance(scope_root, str) and not scope_root.strip():
            raise ValueError(
                "FilesystemMandateBackend scope_root must not be empty — "
                "passing '' would silently scope the backend to the process's "
                "current working directory"
            )
        resolved = Path(scope_root).resolve(strict=False)
        if str(resolved) in {"/", str(Path.cwd())} and str(scope_root) in {
            "",
            ".",
            "./",
            "/",
        }:
            raise ValueError(
                f"FilesystemMandateBackend scope_root={scope_root!r} "
                f"collapses to an unsafe root — pass an absolute path "
                f"or an explicit relative-to-known-root value"
            )
        self._scope_root = resolved

    @property
    def scope_root(self) -> Path:
        """The scope root directory. Read-only after construction."""
        return self._scope_root

    # ────────────────────────────────────────────────────────────────
    # Scope-to-path helpers

    def _mandates_path(self, scope: str) -> Path:
        """Resolve the ``mandates.md`` path for ``scope``.

        Path-traversal in the scope's ``<name>`` component is refused via
        ``_io.safe_resolve_under`` (spec/29 MUST #1 + MUST #2). This
        ensures that ``agent:../../etc/passwd`` does not resolve outside
        the scope_root.
        """
        kind, name = _parse_scope(scope)
        if kind == "project":
            # Project-root mandates live directly in scope_root.
            # The ``name`` component is informational; it does NOT add a
            # directory level.  Resolving anyway confirms no traversal.
            return self._scope_root / "mandates.md"
        # agent scope: <scope_root>/<name>/mandates.md
        try:
            agent_dir = safe_resolve_under(name, self._scope_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"scope {scope!r} name component {name!r} resolves outside "
                f"scope_root {self._scope_root} — path traversal refused "
                f"(spec/29 MUST #1): {exc}"
            ) from exc
        return agent_dir / "mandates.md"

    def _state_path(self, scope: str) -> Path:
        """Resolve the ``.judge-state/mandates.json`` path for ``scope``.

        Mirrors ``_mandates_path`` in traversal protection. Project-root
        state lives in ``<scope_root>/.judge-state/mandates.json``;
        per-agent state lives in
        ``<scope_root>/<name>/.judge-state/mandates.json``.
        """
        kind, name = _parse_scope(scope)
        if kind == "project":
            return self._scope_root / _JUDGE_STATE_DIR / _STATE_FILENAME
        try:
            agent_dir = safe_resolve_under(name, self._scope_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"scope {scope!r} name component {name!r} resolves outside "
                f"scope_root {self._scope_root} — path traversal refused "
                f"(spec/29 MUST #1): {exc}"
            ) from exc
        return agent_dir / _JUDGE_STATE_DIR / _STATE_FILENAME

    # ────────────────────────────────────────────────────────────────
    # Discovery

    def list_mandates(self, scope: str) -> list[Mandate]:
        """Walk the scope's ``mandates.md`` and return all mandates.

        Returns ``[]`` when ``mandates.md`` is absent or empty — opt-in
        by filesystem layout, per the promise in the module docstring.
        Parses via ``parse_mandates_md`` (parallel agent B). Until that
        module is available, returns ``[]`` via the import shim at the
        top of this file.

        Source hash is freshly computed on every call (spec/29 MUST #4) —
        the parser computes it per mandate section; no hash is cached
        between calls.
        """
        # Validate scope at the API boundary before any I/O.
        mandates_path = self._mandates_path(scope)  # raises ValueError on bad scope

        if not mandates_path.is_file():
            return []

        is_project_root = scope.startswith("project:")
        try:
            _meta, mandates = _parse_mandates_md(
                mandates_path, scope=scope, is_project_root=is_project_root
            )
        except MandateInvalid:
            # Re-raise parse-level validation failures unchanged.
            raise
        except Exception as exc:
            _logger.warning(
                "unexpected error parsing %s for scope %r: %s: %s — "
                "treating as empty",
                mandates_path,
                scope,
                type(exc).__name__,
                exc,
            )
            return []

        # Derive EXPIRED state for mandates past expires_at (spec/29 line 548 —
        # the framework computes expired at load time; mandates.md is never
        # edited). The parser produces ACTIVE; we re-stamp REVOCATION_STATE
        # here so callers see derived state consistently.
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        derived: list[Mandate] = []
        for m in mandates:
            if (
                m.revocation_state == RevocationState.ACTIVE
                and m.expires_at is not None
                and m.expires_at <= now
            ):
                # frozen dataclass — use dataclasses.replace
                from dataclasses import replace as _replace
                derived.append(_replace(m, revocation_state=RevocationState.EXPIRED))
            else:
                derived.append(m)

        # Sort lexicographic by mandate_id (mirrors tool registry
        # convention from spec/25; the parametrized conformance suite
        # expects this ordering for deterministic readability).
        derived.sort(key=lambda m: m.mandate_id)
        return derived

    def load_mandate(self, mandate_id: str, scope: str) -> Mandate:
        """Look up and return the mandate with ``mandate_id`` in ``scope``.

        Path-traversal in ``mandate_id`` is refused at the API boundary
        BEFORE any disk access (spec/29 MUST #1 + MUST #6). The source
        hash on the returned ``Mandate`` is freshly computed by the
        underlying ``list_mandates`` call (spec/29 MUST #4).
        """
        _validate_mandate_id(mandate_id)  # spec/29 MUST #1 — before any I/O
        # scope validation is delegated to list_mandates → _mandates_path

        mandates = self.list_mandates(scope)
        for mandate in mandates:
            if mandate.mandate_id == mandate_id:
                return mandate

        raise MandateNotFound(
            f"mandate {mandate_id!r} not found in scope {scope!r} at "
            f"{self._mandates_path(scope)}"
        )

    # ────────────────────────────────────────────────────────────────
    # State (deduplication sidecar)

    def read_state(self, scope: str) -> dict:
        """Read ``.judge-state/mandates.json`` for ``scope``.

        Returns the default empty-state dict when the file is absent
        (lazy-create on first ``write_state``). Raises
        ``MandateStateSchemaUnsupported`` on an unknown ``schema_version``
        (spec/29 MUST #7 — forward-incompatibility is loud).
        """
        state_path = self._state_path(scope)  # raises ValueError on bad scope

        if not state_path.is_file():
            return {
                "schema_version": _DEFAULT_SCHEMA_VERSION,
                "scope": scope,
                "mandates": {},
            }

        try:
            raw = state_path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.warning(
                "could not read mandate state at %s: %s — returning empty state",
                state_path,
                exc,
            )
            return {
                "schema_version": _DEFAULT_SCHEMA_VERSION,
                "scope": scope,
                "mandates": {},
            }

        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MandateStateSchemaUnsupported(
                f"mandate state at {state_path} is not valid JSON: {exc}"
            ) from exc

        if not isinstance(state, dict):
            raise MandateStateSchemaUnsupported(
                f"mandate state at {state_path} is not a JSON object "
                f"(got {type(state).__name__})"
            )

        schema_version = state.get("schema_version")
        if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise MandateStateSchemaUnsupported(
                f"mandate state at {state_path} has schema_version="
                f"{schema_version!r} which is not supported by this "
                f"version of the framework. Supported versions: "
                f"{sorted(_SUPPORTED_SCHEMA_VERSIONS)}. "
                f"Run the migration step before continuing."
            )

        return state

    def write_state(self, scope: str, state: dict) -> None:
        """Atomically persist the state dict to ``.judge-state/mandates.json``.

        Delegates to ``_io.atomic_write`` (temp + fsync + rename per
        spec/03 rule #8) so writes are safe across concurrent processes
        (spec/29 MUST #3). The ``.judge-state/`` directory is created
        lazily by ``atomic_write``'s ``mkdir(parents=True, exist_ok=True)``
        call — callers do NOT need to pre-create it.

        ``datetime`` objects in ``state`` (e.g., from operator-extended
        state shapes) are serialized via ``default=str`` so JSON encoding
        does not raise on non-serializable types.
        """
        state_path = self._state_path(scope)  # raises ValueError on bad scope

        encoded = json.dumps(state, indent=2, default=str, ensure_ascii=False)
        atomic_write(state_path, encoded)
        _logger.debug("wrote mandate state for scope %r to %s", scope, state_path)

    # ────────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> MandateCapabilities:
        return MandateCapabilities(
            # The filesystem backend re-reads mandates.md on every
            # load_mandate call — it observes operator revocations
            # immediately on the next agent run (spec/29 MUST #4).
            supports_revocation=True,
            # External state-change notifications (push callbacks on
            # out-of-process operator edits) are out of scope for the
            # filesystem reference impl.  Operator edits surface on the
            # next agent run via state-file dedup, not via push.
            supports_external_state_change_notification=False,
            # The <scope_root> directory tree is durable — files survive
            # process restart.
            durable=True,
        )


# ────────────────────────────────────────────────────────────────────────────
# URL factory


def make_filesystem_mandate_backend_from_url(url: str) -> FilesystemMandateBackend:
    """Construct a ``FilesystemMandateBackend`` from a ``filesystem://`` URL.

    Accepts the three-slash convention ``filesystem:///path/to/scope_root``
    that other filesystem backend factories use (e.g.,
    ``FilesystemLogBackend``, ``FilesystemAgentProfileBackend``). This
    allows operators to configure mandate backends via a uniform URL string
    in deployment tooling that accepts URLs generically.

    Args:
        url: a URL of the form ``filesystem:///absolute/path/to/scope_root``.
            Three slashes — scheme (``filesystem``), empty authority, and
            an absolute path starting with ``/``.

    Returns:
        A ``FilesystemMandateBackend`` rooted at the extracted path.

    Raises:
        ValueError: ``url`` does not start with ``filesystem://`` or the
            path component is empty.
    """
    _SCHEME = "filesystem://"
    if not url.startswith(_SCHEME):
        raise ValueError(
            f"make_filesystem_mandate_backend_from_url expects a URL "
            f"starting with 'filesystem://' — got {url!r}. "
            f"Use a URL of the form 'filesystem:///path/to/scope_root'."
        )
    path_str = url[len(_SCHEME):]
    if not path_str:
        raise ValueError(
            f"make_filesystem_mandate_backend_from_url: URL {url!r} has "
            f"an empty path component — expected 'filesystem:///path/to/scope_root'."
        )
    return FilesystemMandateBackend(Path(path_str))

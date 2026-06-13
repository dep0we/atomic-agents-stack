"""Multi-agent project cascade loader per spec/06-multi-agent-projects.md.

When an agent path matches the three-layer cascade pattern:

    <agents_root>/<system>/projects/<project>/agents/<role>/

the framework walks up to find:

- **Layer 1 (role)** — shared role definition: ``<system>/roles/<role>/``
  with ``PROMPT.md``, ``tools.md``, ``model.md``.
- **Layer 2 (project)** — project-shared world: ``<system>/projects/<project>/``
  with ``canon.md``, ``style_guide.md``, ``policy/``, optional ``goal.md``,
  and the work ``queue/``.
- **Layer 3 (instance)** — project x role instance: the agent path itself,
  with ``persona/{IDENTITY,SOUL,USER}.md``, ``memory/``, ``wiki/``,
  ``journal/``, ``log/``, and optional ``tools.md`` / ``tools.override.md``
  / ``model.md`` overrides.

The cascade is opt-in: agents whose path doesn't match this pattern stay on
the original single-agent layout (loaded entirely from one folder). Detection
happens via :func:`detect_cascade`; the resolved roots come back as a
:class:`CascadePaths` triple.

Tools.md resolution order (most specific wins):

1. ``<instance>/tools.override.md`` — additive merge with role's tools.md
   (instance section is appended after role section).
2. ``<instance>/tools.md`` — full replacement of role's tools.md.
3. ``<role>/tools.md`` — base.

Model.md resolution: instance overrides role.

This module returns *paths and rendered text*. Parsing of tools.md/model.md
into structured config still happens in ``_tools.py`` / ``_model.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CascadePaths:
    """Resolved roots for a cascaded multi-agent project agent.

    All three roots are absolute paths to existing directories.
    """

    system_root: Path
    role_root: Path
    project_root: Path
    instance_root: Path
    role_name: str
    project_name: str


def detect_cascade(agent_root: Path) -> CascadePaths | None:
    """Inspect ``agent_root`` and return cascade roots if it matches the pattern.

    Returns ``None`` for single-agent layouts (no cascade), so callers can
    branch cleanly and preserve the original load behavior.

    The pattern is: ``<system>/projects/<project>/agents/<role>``. We require
    the matching ``<system>/roles/<role>/`` directory to exist before
    declaring it a cascade — without the role layer, there's nothing to
    cascade into.
    """
    parts = agent_root.parts
    if len(parts) < 5:
        return None

    # Walk from the tail: ../projects/<project>/agents/<role>
    role_name = parts[-1]
    if parts[-2] != "agents":
        return None
    project_name = parts[-3]
    if parts[-4] != "projects":
        return None

    system_root = Path(*parts[:-4])
    project_root = system_root / "projects" / project_name
    role_root = system_root / "roles" / role_name
    if not role_root.is_dir():
        return None

    return CascadePaths(
        system_root=system_root,
        role_root=role_root,
        project_root=project_root,
        instance_root=agent_root,
        role_name=role_name,
        project_name=project_name,
    )


# ──────────────────────────────────────────────────────────────────
# tools.md / model.md override resolution


def resolve_tools_md(cascade: CascadePaths) -> tuple[Path | None, str]:
    """Resolve which tools.md text to use for this cascaded agent.

    Returns ``(source_path, text)``. Resolution order:

    1. ``<instance>/tools.override.md`` exists → role tools + instance override,
       concatenated with a separator (additive merge).
    2. ``<instance>/tools.md`` exists → full replacement; role's tools.md is
       ignored.
    3. Otherwise → role's tools.md (or empty string if neither exists).

    The "primary" source path returned is the most-specific file (override or
    instance > role). When neither exists, returns ``(None, "")``.
    """
    instance_override = cascade.instance_root / "tools.override.md"
    instance_tools = cascade.instance_root / "tools.md"
    role_tools = cascade.role_root / "tools.md"

    if instance_override.is_file():
        role_text = (
            role_tools.read_text(encoding="utf-8") if role_tools.is_file() else ""
        )
        override_text = instance_override.read_text(encoding="utf-8")
        merged = (
            role_text.rstrip() + "\n\n" + _OVERRIDE_SEPARATOR + "\n\n" + override_text
        )
        return instance_override, merged

    if instance_tools.is_file():
        return instance_tools, instance_tools.read_text(encoding="utf-8")

    if role_tools.is_file():
        return role_tools, role_tools.read_text(encoding="utf-8")

    return None, ""


def resolve_model_md(cascade: CascadePaths) -> Path | None:
    """Return the path of the model.md the instance should parse.

    Instance overrides role. If neither has model.md, returns ``None`` and
    the caller falls back to defaults from :func:`atomic_agents._model.parse_model_md`.
    """
    instance_model = cascade.instance_root / "model.md"
    if instance_model.is_file():
        return instance_model
    role_model = cascade.role_root / "model.md"
    if role_model.is_file():
        return role_model
    return None


_OVERRIDE_SEPARATOR = "<!-- ─── instance tools.override.md ─── -->"


# ──────────────────────────────────────────────────────────────────
# Layer-1 (role) and Layer-2 (project) text loaders


def load_role_prompt(cascade: CascadePaths) -> str:
    """Load Layer-1 role prompt: ``<role>/PROMPT.md``. Empty string if missing."""
    path = cascade.role_root / "PROMPT.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def load_project_layer(cascade: CascadePaths) -> dict[str, str]:
    """Load Layer-2 project shared assets.

    Returns a dict with keys:

    - ``canon``     — content of ``project/canon.md`` (or empty)
    - ``style_guide`` — content of ``project/style_guide.md`` (or empty)
    - ``goal``      — content of ``project/goal.md`` (or empty)
    - ``policy``    — concatenated content of all ``.md`` files under
      ``project/policy/`` (alphabetical), with per-file H1 separators.
    """
    out = {
        "canon": _read_or_empty(cascade.project_root / "canon.md"),
        "style_guide": _read_or_empty(cascade.project_root / "style_guide.md"),
        "goal": _read_or_empty(cascade.project_root / "goal.md"),
        "policy": _load_policy_dir(cascade.project_root / "policy"),
    }
    return out


def _read_or_empty(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _load_policy_dir(policy_dir: Path) -> str:
    """Concatenate all .md files in policy/ alphabetically, with separators.

    Recurses into subdirectories so policy can be organized.
    """
    if not policy_dir.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(policy_dir.rglob("*.md")):
        rel = path.relative_to(policy_dir)
        parts.append(
            f"# policy/{rel.as_posix()}\n\n{path.read_text(encoding='utf-8').strip()}"
        )
    return "\n\n---\n\n".join(parts)


# ──────────────────────────────────────────────────────────────────
# Project queue claim mechanics per spec/06
#
# NON-DEPRECATED re-export shim — these symbols are re-exported from
# atomic_agents.queue (the canonical QueueBackend Protocol package,
# spec/44). The shim preserves the VERBATIM free-function signatures
# (claim_next_queued(project_root, role, ...) etc.) that form the external
# cron/project-runner API documented in spec/06. There is NO DeprecationWarning
# — sunset is deferred to the v1.0/T10 unified shim-retirement pass.
#
# Design notes:
# - QueueItem here is FilesystemQueueItem (aliased as QueueItem for backward
#   compat). The canonical Protocol-level QueueItem in queue/types.py carries
#   NO path field. The alias ensures existing callers that access item.path
#   continue to work unchanged. See arc-ruling 428-pr1-args.json.
# - The free functions below WRAP FilesystemQueueBackend, constructing it
#   internally on each call and delegating to Protocol methods. This preserves
#   the old positional signatures (project_root as first arg) exactly.
# - Old and new import paths are behaviorally equivalent for the documented
#   cron/project-runner API surface — including fail-soft on symlink-escape
#   (release_claim / move_to_dead_letter no-op rather than raise, matching the
#   Protocol methods). They are NOT same-object identity: the shim constructs a
#   fresh FilesystemQueueBackend per call and wraps it. See compatibility tests
#   in tests/test_queue_backend_conformance.py.
# - _sidecar_path / _write_sidecar are filesystem impl details. They are
#   importable from atomic_agents._cascade for backward compat but are NOT
#   part of the public queue package __all__.
#
# Per arc-ruling 428-pr1-args.json:
#   re-export-shim-shape: Option A (non-deprecated)
#   constructor-scope-signature: Option A with reconciliation
#   adopt-now-vs-scaffolding-only: Scaffolding-only

from .queue import (  # noqa: E402 (intentional mid-file shim; see banner above)
    FilesystemQueueItem as QueueItem,  # backward-compat alias (item.path works)
    _sidecar_path,
    _write_sidecar,  # noqa: F401 (re-export for backward compat; see comment above)
)
from .queue.backend import (  # noqa: E402 (intentional mid-file shim)
    recover_stale_claims as _recover_stale_claims_impl,
)
from .queue.filesystem import (  # noqa: E402 (intentional mid-file shim)
    FilesystemQueueBackend as _FilesystemQueueBackend,
)
from .exceptions import PathTraversalError  # noqa: E402 (mid-file shim; fail-soft)


def claim_next_queued(
    project_root: Path,
    role: str,
    lease_token: str,
    lease_seconds: int = 3600,
) -> "QueueItem | None":
    """Atomically claim the next item from ``project_root/queue/queued/<role>/``.

    NON-DEPRECATED shim — wraps FilesystemQueueBackend.claim_next() while
    preserving the VERBATIM free-function signature (project_root as first arg)
    of the spec/06 external cron/project-runner API. Sunset at the v1.0/T10
    unified shim-retirement pass.

    Returns a FilesystemQueueItem with a ``path`` field so existing callers
    that access ``item.path`` continue to work unchanged.

    Returns ``None`` when the queue is empty or no items remain after races.
    """
    return _FilesystemQueueBackend(project_root).claim_next(
        role=role, lease_token=lease_token, lease_seconds=lease_seconds
    )


def release_claim(item: "QueueItem", project_root: Path) -> None:
    """Mark a claimed item as completed by moving it to ``queue/done/<lease_token>/``.

    NON-DEPRECATED shim — wraps FilesystemQueueBackend.release() while
    preserving the VERBATIM free-function signature.

    The destination is namespaced by ``lease_token`` so two leases with
    identically-named files do not overwrite each other.

    Renames from ``item.path`` directly (via ``_release_at_path``), exactly as
    the pre-carve _cascade.py implementation did. This works for an item at ANY
    path depth — a normally claimed item under ``queue/claimed/<token>/`` AND a
    recovered item under ``queue/queued/_recovered/<token>/``. We do NOT
    reconstruct ``claimed/<token>/<name>`` (which crashed for recovered items).

    Fails SOFT (no-op, no raise) when ``queue/`` resolves outside
    ``project_root`` via a symlink — parity with ``FilesystemQueueBackend.release()``
    and the pre-carve _cascade.py contract, which had no containment check and
    so never propagated an exception to a caller (spec/44 symlink-containment).
    """
    backend = _FilesystemQueueBackend(project_root)
    try:
        queue_root = backend._queue_root()
    except PathTraversalError:
        return
    try:
        backend._release_at_path(
            item.path,
            queue_root,
            item.lease_token,
            item.original_name,
        )
    except PathTraversalError:
        return


def move_to_dead_letter(
    item: "QueueItem",
    project_root: Path,
    reason: str = "",
) -> None:
    """Move a claimed item to ``queue/dead-letter/<lease_token>/`` after exhausting retries.

    NON-DEPRECATED shim — wraps FilesystemQueueBackend.move_to_dead_letter()
    while preserving the VERBATIM free-function signature.

    A sibling ``.reason.txt`` is written alongside the item when *reason* is
    provided. The lease sidecar is removed (dead-letter is a terminal state).

    Renames from ``item.path`` directly (via ``_dead_letter_at_path``), exactly
    as the pre-carve _cascade.py implementation did. This works for an item at
    ANY path depth — a normally claimed item under ``queue/claimed/<token>/`` AND
    a recovered item under ``queue/queued/_recovered/<token>/``. We do NOT
    reconstruct ``claimed/<token>/<name>`` (which crashed for recovered items).

    Fails SOFT (no-op, no raise) when ``queue/`` resolves outside
    ``project_root`` via a symlink — parity with
    ``FilesystemQueueBackend.move_to_dead_letter()`` and the pre-carve
    _cascade.py contract, which had no containment check and so never propagated
    an exception to a caller (spec/44 symlink-containment).
    """
    backend = _FilesystemQueueBackend(project_root)
    try:
        queue_root = backend._queue_root()
    except PathTraversalError:
        return
    try:
        backend._dead_letter_at_path(
            item.path,
            queue_root,
            item.lease_token,
            item.original_name,
            reason,
        )
    except PathTraversalError:
        return


def recover_stale_claims(
    project_root: Path,
    lease_seconds: int = 3600,
) -> list[Path]:
    """Find claimed items whose lease has expired and move them back to ``queued``.

    NON-DEPRECATED shim — wraps recover_stale_claims() from the queue package
    while preserving the VERBATIM free-function signature.

    Returns the list of recovered work-file PATHS (now in their queued home),
    for backward compatibility with existing callers that used the returned
    path list (test_cascade.py asserts ``recovered[0].name``).
    """
    backend = _FilesystemQueueBackend(project_root)
    items = _recover_stale_claims_impl(backend, lease_seconds=lease_seconds)
    return [
        item.path for item in items if hasattr(item, "path") and item.path is not None
    ]


def renew_lease(item: "QueueItem", additional_seconds: int = None) -> None:
    """Extend the lease for an actively-worked item.

    NON-DEPRECATED shim — preserves the VERBATIM free-function signature.

    Updates ``lease_expires_at`` in the sidecar to ``now + additional_seconds``.
    If *additional_seconds* is ``None``, the original ``lease_seconds`` from
    the sidecar is reused.

    Writes the sidecar directly next to ``item.path`` (via
    ``_sidecar_path(item.path)``), exactly as the pre-carve _cascade.py
    implementation did. This works for an item at ANY path depth — a normally
    claimed item under ``queue/claimed/<token>/`` AND a recovered item under
    ``queue/queued/_recovered/<token>/``. We do NOT reconstruct the sidecar
    location from ``item.path.parents[3]`` + ``lease_token`` (which assumed a
    fixed claimed-dir depth and crashed for recovered items).

    Trust boundary (asymmetric with the release/move_to_dead_letter shims): those
    re-derive their target through the project_root-anchored ``_safe_under_queue``
    guard and so fail-soft on a symlinked ``queue/``. This shim writes next to
    ``item.path`` with NO containment guard — its frozen signature carries no
    ``project_root`` to anchor one. That is byte-faithful to pre-carve behavior
    and safe for items produced by ``claim_next_queued``/recovery (paths under
    ``queue/``); an EXTERNALLY-constructed item with an out-of-tree ``path`` would
    write outside. The Protocol method ``FilesystemQueueBackend.renew_lease()``
    IS contained; closing this shim gap requires a signature change deferred to
    the v1.0/T10 shim-retirement pass (tracked as a follow-up issue).
    """
    _FilesystemQueueBackend._renew_lease_at_sidecar(
        _sidecar_path(item.path),
        lease_token=item.lease_token,
        additional_seconds=additional_seconds,
    )

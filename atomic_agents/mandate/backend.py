"""MandateBackend Protocol — the contract every mandate backend satisfies.

This is the eighth open Protocol in the protocol-pattern series alongside
MemoryBackend (#57, shipped), LLMBackend (#87, shipped), JudgeBackend (#112,
shipped), LockBackend (#60, shipped), LogBackend (#61, shipped),
AgentProfileBackend (#63, shipped), and ToolRegistryBackend (#64, shipped).
Each Protocol decouples one storage / dispatch axis so the framework's core
stays small and alternate implementations drop in without forking.

Issue #124 frames the mandate primitive. Today, agent action proposals may
cite a ``mandate_id`` in their ``Authorization`` — but there is no persistent,
revocable, cross-run record the judge layer can validate against. A mandate is
the durable record: operator-authored, dated, scoped, constrained, revocable.
The ``MandateBackend`` Protocol seals the storage layer so the ``MandateCheck``
judge specialist (PR 3a of #124) talks to mandates through a stable interface,
and future backends (SaaS databases, mobile-app stores, Slack-bot registries)
register via ``register_mandate_backend(name, cls)`` without forking core.

Scaffolding PR (#124 PR 1): the Protocol contract + ``FilesystemMandateBackend``
reference implementation + mandates.md parser (parallel agent B) + parametrized
conformance suite (parallel agent C). **Zero behavior change** — no call site
routes through the Protocol yet; ``AtomicAgent.__init__`` is unchanged.
PR 2 wires the bootstrap path.

The eight normative MUSTs from spec/29 §"Implementer contract for mandate
backends" govern this module:

- MUST #1: path-traversal refusal at API boundary (``load_mandate``, any
  scope or id parameter that names a filesystem path).
- MUST #2: per-scope isolation (agent-scope state must not cross into
  project-scope state and vice versa).
- MUST #3: ``write_state`` MUST be atomic and safe across concurrent
  processes (``atomic_write`` via temp + fsync + rename per ``_io``).
- MUST #4: ``source_hash`` MUST be recomputed on every ``load_mandate``
  call — never cached between calls.
- MUST #5: ``list_mandates`` returns ALL mandates (ACTIVE + REVOKED +
  EXPIRED); caller filters on ``revocation_state``.
- MUST #6: ``MandateInvalid`` raised for id containing path-traversal
  tokens at the API boundary; ``MandateNotFound`` raised when the id is
  valid but absent from the scope.
- MUST #7: ``read_state`` MUST raise ``MandateStateSchemaUnsupported``
  on encountering an unknown ``schema_version`` — forward-incompatibility
  is loud, not silent.
- MUST #8: capabilities are honest — a declared capability MUST pass the
  parametrized conformance suite's capability-gated tests.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..exceptions import BackendNotRegistered
from .types import (
    Mandate,
    MandateCapabilities,
    MandateNotFound,
)

if TYPE_CHECKING:
    pass  # Reserved — future forward references

_logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Protocol definition


@runtime_checkable
class MandateBackend(Protocol):
    """Contract every mandate backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, MandateBackend)`` to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope is the unit of mandate isolation. Every method that works on
    mandates or state accepts a ``scope`` parameter of the form:

    - ``"agent:<name>"`` — mandates local to a specific agent under the
      backend's scope root.
    - ``"project:<name>"`` — mandates at the project root, applicable to
      all agents in that project (per spec/29 §"Per-agent vs project-root
      resolution").

    Scope strings are validated by the backend before any disk / network
    access. Path-traversal characters in ``<name>`` are refused at the API
    boundary per spec/29 MUST #1.

    **Relationship to the ``MandateCheck`` judge specialist** (PR 3a of
    #124): ``MandateBackend`` owns the *discovery* layer — reading
    ``mandates.md`` files and the state sidecar at ``<scope>/.judge-state/
    mandates.json``. ``MandateCheck`` owns the *validation* layer —
    checking existence, source hash, revocation state, constraint
    satisfaction, and budget arithmetic. The two compose:

    .. code-block:: text

        backend.load_mandate(id, scope) → Mandate    # discovery
        MandateCheck.evaluate(proposal, ctx) → Judgment  # validation

    Replacing the in-memory validation logic would collapse the two layers
    and make alternate backends impossible. The discovery / validation split
    mirrors the ToolRegistryBackend (catalog / dispatch) and LogBackend
    (append / query) patterns from spec/25 and spec/22 respectively.

    Capability-gated behavior (``supports_revocation``,
    ``supports_external_state_change_notification``) is declared via
    ``capabilities()``; the conformance suite gates tests on the flags.
    Backends that lie about capabilities produce silent failures rather
    than loud refusals — spec/29 MUST #8.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g., ``"filesystem"``, ``"sqlite"``,
        ``"saas"``.

        Used by the registry for lookup
        (``get_mandate_backend(backend_id)``) and by diagnostic tooling
        that wants to record "which backend resolved this mandate?".
        Treat as a backwards-compatibility surface — operator deployments
        may pin against these strings in env vars and config.
        """
        ...

    # ────────────────────────────────────────────────────────────────
    # Discovery — always implemented

    def list_mandates(self, scope: str) -> list[Mandate]:
        """Return ALL mandates for ``scope`` — ACTIVE, REVOKED, and EXPIRED.

        Semantics:

        * Caller is responsible for filtering on ``revocation_state`` —
          this method returns the full unfiltered set so that lifecycle
          event deduplication logic (per spec/29 §"Lifecycle event
          deduplication") can observe all transitions, including REVOKED
          and EXPIRED entries whose last-known state differs from the
          current state file (spec/29 MUST #5).
        * MUST return ``[]`` (NOT raise) when no ``mandates.md`` exists
          at the scope path or when the file is empty. The mandate
          surface is opt-in by filesystem layout; agents without a
          ``mandates.md`` file have no mandates.
        * ``scope`` is validated at the API boundary. Scope strings
          containing path-traversal tokens (``..``, ``/``, backslash,
          control characters) are refused with ``ValueError`` per
          spec/29 MUST #1.
        * Per-scope isolation is strict (spec/29 MUST #2). An
          ``"agent:caldwell"`` scope query MUST NOT return mandates
          from ``"project:caldwell"`` or any other scope.
        * ``source_hash`` on each returned ``Mandate`` MUST be freshly
          computed at call time — never served from a cache (spec/29
          MUST #4). This is the load-bearing TOCTOU defense: if the
          operator edits ``mandates.md`` between the ``list_mandates``
          call and the ``MandateCheck`` evaluation, the hash mismatch
          surfaces as ``mandate_state_inconsistent``, not a silent
          pass.

        Args:
            scope: ``"agent:<name>"`` or ``"project:<name>"``.

        Returns:
            A list of ``Mandate`` instances. Order is unspecified but
            SHOULD be stable (lexicographic by ``mandate_id``) for
            deterministic CLI output and test reproducibility.

        Raises:
            ValueError: ``scope`` fails path-traversal validation.
        """
        ...

    def load_mandate(self, mandate_id: str, scope: str) -> Mandate:
        """Return the single mandate identified by ``mandate_id`` in ``scope``.

        Semantics:

        * MUST raise ``MandateNotFound`` when ``mandate_id`` is valid
          (passes the path-traversal check) but is not present in the
          scope (spec/29 MUST #6).
        * MUST raise ``ValueError`` when ``mandate_id`` contains
          path-traversal tokens — ``..``, ``/``, backslash, or control
          characters (spec/29 MUST #1 + MUST #6). The refusal happens
          BEFORE any disk access.
        * MUST raise ``ValueError`` on a malformed ``scope`` for the
          same reason.
        * ``source_hash`` on the returned ``Mandate`` MUST be freshly
          computed at call time — not cached between calls (spec/29
          MUST #4). Callers that need a fresh hash at execution time
          (``MandateCheck`` step 2) call ``load_mandate`` again;
          no "re-hash" helper is needed.

        Args:
            mandate_id: mandate identifier to look up. Validated at the
                API boundary before any I/O.
            scope: ``"agent:<name>"`` or ``"project:<name>"``.

        Returns:
            The ``Mandate`` with the matching id.

        Raises:
            ValueError: ``mandate_id`` or ``scope`` fails
                path-traversal validation.
            MandateNotFound: ``mandate_id`` is not present in scope.
        """
        ...

    def read_state(self, scope: str) -> dict:
        """Read the deduplication state file for ``scope``.

        The state file at ``<scope>/.judge-state/mandates.json`` tracks
        per-mandate ``last_seen_state`` for lifecycle event deduplication
        (spec/29 §"Lifecycle event deduplication"). Backends MUST NOT
        merge state across scopes — each scope's state file is
        independent (spec/29 MUST #2).

        Returned dict shape (schema_version 1):

        .. code-block:: json

            {
              "schema_version": 1,
              "scope": "agent:<name>",
              "mandates": {
                "<mandate_id>": {
                  "last_seen_state": "active",
                  "last_seen_revoked_at": null,
                  "last_seen_expired_at": null,
                  "last_seen_source_hash": "sha256:abc..."
                }
              }
            }

        Semantics:

        * Returns a default empty-state dict when the file is absent —
          the state file is lazily created on the first ``write_state``
          call. Absense is not an error; it means no lifecycle events
          have been deduped yet for this scope.
        * MUST raise ``MandateStateSchemaUnsupported`` when the file
          exists and contains an unknown ``schema_version`` (spec/29
          MUST #7). Readers MUST NOT silently migrate — the failure is
          loud so operators upgrading across a schema bump are forced to
          run the migration step.
        * MUST raise ``ValueError`` on a malformed ``scope`` per MUST #1.

        Args:
            scope: ``"agent:<name>"`` or ``"project:<name>"``.

        Returns:
            The state dict. Callers may mutate the dict and pass it back
            to ``write_state``.

        Raises:
            ValueError: ``scope`` fails path-traversal validation.
            MandateStateSchemaUnsupported: state file contains an
                unrecognized ``schema_version``.
        """
        ...

    def write_state(self, scope: str, state: dict) -> None:
        """Persist the deduplication state dict for ``scope``.

        Writes MUST be atomic and safe across concurrent processes
        (spec/29 MUST #3). The filesystem reference impl delegates to
        ``_io.atomic_write`` (temp + fsync + rename per spec/03 rule #8),
        which makes partial-write states impossible on POSIX.

        The ``.judge-state/`` directory is created lazily — backends
        MUST NOT require the directory to pre-exist.

        Semantics:

        * Accepts the same dict shape ``read_state`` returns (with or
          without the caller's modifications to the ``"mandates"`` dict).
        * MUST NOT raise for a missing parent directory — create it.
        * MUST raise ``ValueError`` on a malformed ``scope`` per MUST #1.
        * Backends MUST serialize datetime-like objects via ``default=str``
          (or equivalent) so JSON encoding does not fail on
          ``datetime.datetime`` values stored in operator-extended state
          shapes.

        Args:
            scope: ``"agent:<name>"`` or ``"project:<name>"``.
            state: the full state dict to persist.

        Raises:
            ValueError: ``scope`` fails path-traversal validation.
        """
        ...

    # ────────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> MandateCapabilities:
        """Backend capability declaration — see ``MandateCapabilities``.

        Conformance tests assert claim-vs-behavior parity. Honest
        capabilities let callers fail fast against incompatible backends
        rather than discovering the mismatch mid-operation (spec/29
        MUST #8).
        """
        ...


# ────────────────────────────────────────────────────────────────────────────
# Process-local registry


# Registry: backend_id → backend class.  Classes (not instances) because
# mandate backends carry per-scope construction args — the framework
# instantiates ``FilesystemMandateBackend(scope_root)`` at agent-construction
# time; the registry's job is the operator-pin lookup that maps
# ``"filesystem"`` → ``FilesystemMandateBackend``.
_registry: dict[str, type] = {}


def register_mandate_backend(name: str, cls: type[MandateBackend]) -> None:
    """Register a ``MandateBackend`` implementation under ``name``.

    Typically called once at module-import time from each backend module.
    The default ``"filesystem"`` registration happens at the bottom of this
    file via a deferred import to avoid circular-dependency at import time.

    Raises ``ValueError`` on collision — unlike the tool-registry
    ``register_tool_registry_backend`` which silently replaces on
    re-registration, the mandate registry fails loudly. Mandate backends
    are security-critical infrastructure; silent replacement would make
    a misconfigured import order look correct.

    Args:
        name: short stable identifier, e.g. ``"filesystem"``, ``"sqlite"``.
        cls: a class satisfying the ``MandateBackend`` Protocol.

    Raises:
        ValueError: ``name`` is already registered.
    """
    if name in _registry:
        raise ValueError(
            f"A MandateBackend is already registered under {name!r}. "
            f"Unregister the existing binding before registering a "
            f"replacement — silent replacement is refused for "
            f"security-critical infrastructure."
        )
    _registry[name] = cls
    _logger.debug("registered mandate backend %r → %s", name, cls.__qualname__)


def get_mandate_backend(name: str) -> type[MandateBackend]:
    """Return the ``MandateBackend`` class registered under ``name``.

    Raises ``BackendNotRegistered`` when the name is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., ``cls(scope_root)`` for the filesystem
    backend).

    Args:
        name: backend identifier to look up.

    Returns:
        The registered backend class.

    Raises:
        BackendNotRegistered: ``name`` is not in the registry.
    """
    if name not in _registry:
        raise BackendNotRegistered(
            f"No MandateBackend registered under {name!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[name]  # type: ignore[return-value]


def list_mandate_backends() -> list[str]:
    """Return registered backend names in lexicographic order."""
    return sorted(_registry.keys())


# ────────────────────────────────────────────────────────────────────────────
# Default factory


def get_default_mandate_backend(scope_root: Path) -> MandateBackend:
    """Return the operator-pinned ``MandateBackend`` instance for ``scope_root``.

    Reads ``ATOMIC_AGENTS_MANDATE_BACKEND`` from the environment (default
    ``"filesystem"``). The env var name follows the established pattern —
    ``ATOMIC_AGENTS_<PRIMITIVE>_BACKEND`` — so operators who already pin
    the log or tool-registry backend use the same vocabulary.

    The ``scope_root`` parameter is the directory under which per-agent
    and project-root mandate files live. For the filesystem backend:

    - ``<scope_root>/<agent_name>/mandates.md`` for per-agent mandates.
    - ``<scope_root>/mandates.md`` for project-root mandates.
    - ``<scope_root>/<agent_name>/.judge-state/mandates.json`` for
      per-agent state.
    - ``<scope_root>/.judge-state/mandates.json`` for project-root state.

    For programmatic operators who want to construct the backend themselves
    (custom database, alternative path structure), the future
    ``AtomicAgent(..., mandate_backend=...)`` constructor kwarg (wired in
    PR 2 of #124) bypasses this factory entirely.

    See spec/29 §"Implementer contract for mandate backends" for the full
    env-var reference.

    Args:
        scope_root: root directory from which per-agent and project-root
            mandate paths are resolved.

    Returns:
        A ready-to-use ``MandateBackend`` instance.

    Raises:
        BackendNotRegistered: the env-var names an unknown backend.
    """
    from .filesystem import FilesystemMandateBackend  # deferred — avoids circular

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_MANDATE_BACKEND", "filesystem")
        .strip()
        .lower()
    )

    if raw_backend_id == "filesystem":
        return FilesystemMandateBackend(scope_root)

    # Unknown backend — surface a fail-fast error with the full id list.
    # Credential-safety: raw_backend_id is redacted in case the operator
    # accidentally pasted a URL into ATOMIC_AGENTS_MANDATE_BACKEND.
    safe_id = _redact_for_error_message(raw_backend_id)
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_MANDATE_BACKEND={safe_id!r} is not a known backend. "
        f"Available: {list_mandate_backends()}. "
        f"Unset the env var to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and truncates
    at ``max_len``. Mirrors the identical helper in
    ``atomic_agents.registry.__init__`` and ``atomic_agents.logs.__init__``.
    A future refactor MAY hoist to a shared location once a fifth Protocol
    needs it; until then duplicating the 5-line function is cheaper than the
    cross-module dependency.
    """
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value


# ────────────────────────────────────────────────────────────────────────────
# Bootstrap registration

# Register the built-in filesystem backend at import time.  Matches the
# Profile / Log / Lock / ToolRegistry registry pattern — the default is always
# available without an extra resolution step.  Deferred import avoids a
# circular dependency (filesystem.py imports from this module's types; this
# module imports from filesystem.py only at the bottom after the Protocol is
# fully defined).
def _bootstrap_filesystem() -> None:
    """Register the filesystem backend at import time if not already registered.

    Called unconditionally at module bottom. Guards against double-import
    (e.g., test isolation that re-imports the module) by checking presence
    before registering — this is the ONLY caller permitted to skip the
    ``ValueError``-on-collision rule by checking first.
    """
    if "filesystem" not in _registry:
        from .filesystem import FilesystemMandateBackend

        _registry["filesystem"] = FilesystemMandateBackend
        _logger.debug(
            "registered mandate backend 'filesystem' → FilesystemMandateBackend"
        )


_bootstrap_filesystem()

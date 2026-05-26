"""PersonaBackend Protocol -- the contract every persona backend satisfies.

This is the tenth open Protocol in the protocol-pattern series alongside
MemoryBackend (#57), LLMBackend (#87), JudgeBackend (#112), LockBackend (#60),
LogBackend (#61), AgentProfileBackend (#63), ToolRegistryBackend (#64),
MandateBackend (#124), and PolicyBackend (#89). Each Protocol decouples one
storage / dispatch axis so the framework's core stays small and alternate
implementations drop in without forking.

Issue #62 frames the persona primitive. PersonaBackend ships as a separate
Protocol because persona has an independent lifecycle from agent config: persona
is shareable across agents (one SOUL.md for N customer-support agents), it is
versionable independently of agent config, and persona templates are
conceptually distinct from agent-config templates (operators bring their own
model + tools). The composition shape: PersonaBackend is source of truth;
AgentProfile fields are denormalized snapshots populated at load_profile() time
when PersonaBackend owns the agent's persona (D1, wired in PR 2).

Scaffolding PR (#62 PR 1 of 4): the Protocol contract + registry primitives +
default factory + ``FilesystemPersonaBackend`` bootstrap registration. Zero
behavior change -- no call site routes through the Protocol yet;
``AtomicAgent.__init__`` is unchanged. PR 2 wires the bootstrap path.

NOTE on registry parameter naming: the design doc originally used
``register_persona_backend(scheme, factory)``. The 9 existing backends use
``register_X_backend(backend_id: str, cls: type[XBackend])``. This module uses
the established convention (``backend_id`` + ``cls``) for consistency.

See ``docs/spec/33-persona-backend.md`` for the full normative contract.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..exceptions import BackendNotRegistered
from .types import Persona, PersonaCapabilities, PersonaSnapshot

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Protocol definition


@runtime_checkable
class PersonaBackend(Protocol):
    """Contract every persona backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol -- it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, PersonaBackend)`` to perform a method-presence check
    (not a signature check -- signatures are static-typing's job).

    The persona_id is the unit of persona identity. Every method that
    operates on a persona accepts a ``persona_id`` parameter identifying
    which persona record to act on.

    persona_id values are validated at the API boundary: ``[a-zA-Z0-9_.+@-]+``
    with refusal of path-traversal tokens, control characters, newlines,
    leading dots, and empty strings (D4 -- mirrors PolicyBackend's
    ``_AGENT_NAME_PATTERN``).

    Capability-gated behavior is declared via ``capabilities()``. The
    conformance suite gates tests on the flags. Backends that lie about
    capabilities produce silent failures rather than loud refusals
    (spec/33 implementer contract).
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier -- e.g., ``"filesystem"``, ``"postgres"``,
        ``"saas"``.

        Used by the registry for lookup (``get_persona_backend(backend_id)``)
        and by diagnostic tooling that wants to record which backend resolved
        a persona. Treat as a backwards-compatibility surface -- operator
        deployments may pin against these strings in env vars and config.
        """
        ...

    # ── Core persona CRUD ────────────────────────────────────────────

    def load_persona(self, persona_id: str) -> Persona:
        """Load and return the ``Persona`` record for ``persona_id``.

        Args:
            persona_id: the persona identifier to load. Validated at the
                API boundary against ``[a-zA-Z0-9_.+@-]+``.

        Returns:
            The ``Persona`` record with all fields populated.

        Raises:
            PersonaNotFound: ``persona_id`` is not known to this backend.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        ...

    def save_persona(
        self, persona_id: str, persona: Persona, *, overwrite: bool = False
    ) -> None:
        """Persist ``persona`` under ``persona_id``.

        When ``overwrite=False`` (the default), raises ``PersonaExists`` if
        a persona with ``persona_id`` already exists. When ``overwrite=True``,
        replaces the existing record atomically (write to temp + rename).

        Args:
            persona_id: the persona identifier to write under. Validated at
                the API boundary.
            persona: the ``Persona`` record to persist.
            overwrite: when False (default), raises ``PersonaExists`` if the
                id already exists. When True, replaces the existing record.

        Raises:
            PersonaExists: ``persona_id`` already exists and
                ``overwrite=False``.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        ...

    def list_personas(self) -> list[str]:
        """Return the sorted list of known persona_ids.

        Returns:
            Sorted list of persona_id strings. Returns ``[]`` when no
            personas are known to this backend.
        """
        ...

    def exists(self, persona_id: str) -> bool:
        """Return ``True`` if ``persona_id`` is known to this backend.

        Args:
            persona_id: the persona identifier to check. Validated at the
                API boundary.

        Returns:
            ``True`` if the persona exists; ``False`` if not.

        Raises:
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        ...

    def clone(
        self,
        source_id: str,
        target_id: str,
        overrides: dict | None = None,
    ) -> None:
        """Clone the persona at ``source_id`` to ``target_id``.

        Copies all fields from the source persona. When ``overrides`` is
        supplied, each key-value pair in the dict is applied on top of the
        copied fields before persisting (e.g.,
        ``overrides={"label": "clone-of-v3"}``).

        Args:
            source_id: the persona to clone from. Validated at the API
                boundary.
            target_id: the destination persona_id. Must not already exist.
                Validated at the API boundary.
            overrides: optional dict of Persona field overrides to apply
                on the cloned record.

        Raises:
            PersonaNotFound: ``source_id`` is not known to this backend.
            PersonaExists: ``target_id`` already exists.
            ValueError: either ``source_id`` or ``target_id`` fails charset
                / path-traversal validation.
        """
        ...

    # ── Snapshot trio ────────────────────────────────────────────────
    # PR 1 stubs: capabilities().supports_snapshot=False means these three
    # methods raise NotImplementedError. PR 3 flips the capability and
    # provides the full filesystem implementation.

    def snapshot(self, persona_id: str, label: str | None = None) -> str:
        """Capture a snapshot of ``persona_id`` and return the snapshot_id.

        When ``capabilities().supports_snapshot`` is ``False``, this method
        MUST raise ``NotImplementedError``.

        Args:
            persona_id: the persona to snapshot. Validated at the API
                boundary.
            label: optional human-readable label for the snapshot.

        Returns:
            The backend-issued snapshot_id string.

        Raises:
            PersonaNotFound: ``persona_id`` is not known to this backend.
            NotImplementedError: ``capabilities().supports_snapshot`` is
                ``False``.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        ...

    def restore(self, persona_id: str, snapshot_id: str) -> None:
        """Restore ``persona_id`` to the state captured in ``snapshot_id``.

        When ``capabilities().supports_snapshot`` is ``False``, this method
        MUST raise ``NotImplementedError``.

        Args:
            persona_id: the persona to restore. Validated at the API
                boundary.
            snapshot_id: the snapshot to restore from. Must belong to
                ``persona_id`` (cross-persona isolation enforced at storage
                layer).

        Raises:
            PersonaSnapshotNotFound: ``snapshot_id`` is not known for
                ``persona_id``.
            NotImplementedError: ``capabilities().supports_snapshot`` is
                ``False``.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        ...

    def list_snapshots(self, persona_id: str) -> list[PersonaSnapshot]:
        """Return the snapshots for ``persona_id`` in chronological order.

        When ``capabilities().supports_snapshot`` is ``False``, this method
        MUST raise ``NotImplementedError``.

        Args:
            persona_id: the persona whose snapshot history to list.
                Validated at the API boundary.

        Returns:
            List of ``PersonaSnapshot`` records in ascending
            ``created_at`` order. Returns ``[]`` when no snapshots exist.

        Raises:
            PersonaNotFound: ``persona_id`` is not known to this backend.
            NotImplementedError: ``capabilities().supports_snapshot`` is
                ``False``.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        ...

    # ── Capabilities ─────────────────────────────────────────────────

    def capabilities(self) -> PersonaCapabilities:
        """Backend capability declaration -- see ``PersonaCapabilities``.

        Conformance tests assert claim-vs-behavior parity. Honest
        capabilities let callers fail fast against incompatible backends
        rather than discovering the mismatch mid-operation (spec/33
        implementer contract).
        """
        ...


# ──────────────────────────────────────────────────────────────────────────────
# Process-local registry

# Registry: backend_id -> backend class. Classes (not instances) because
# persona backends carry per-construction args -- the framework instantiates
# ``FilesystemPersonaBackend(personas_root)`` at agent-construction time; the
# registry's job is the operator-pin lookup that maps ``"filesystem"`` to
# ``FilesystemPersonaBackend``.
_registry: dict[str, type] = {}


def register_persona_backend(backend_id: str, cls: type[PersonaBackend]) -> None:
    """Register a ``PersonaBackend`` implementation under ``backend_id``.

    Typically called once at module-import time from each backend module.
    The default ``"filesystem"`` registration happens at the bottom of this
    file via a deferred import to avoid circular-dependency at import time.

    Silent replace on collision -- matches the Lock / Log / Profile / LLM /
    Judge / Policy pattern. The ``_bootstrap_filesystem()`` call at module
    bottom is idempotent (guards against double-registration via presence
    check), so silent replace is safe for user-registered backends and
    operator pin-overrides via test fixtures or alternative reference
    implementations.

    Args:
        backend_id: short stable identifier, e.g. ``"filesystem"``,
            ``"sqlite"``, ``"saas"``.
        cls: a class satisfying the ``PersonaBackend`` Protocol.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing persona backend %r (was %s, now %s)",
            backend_id,
            _registry[backend_id].__qualname__,
            cls.__qualname__,
        )
    _registry[backend_id] = cls
    _logger.debug("registered persona backend %r -> %s", backend_id, cls.__qualname__)


def unregister_persona_backend(backend_id: str) -> None:
    """Remove ``backend_id`` from the registry.

    Used by conformance fixtures: register ``MockPersonaBackend`` under
    ``"mock"`` in setup; unregister in teardown to keep test isolation
    clean. Idempotent -- silently no-ops if ``backend_id`` is not present.

    Mirrors the Policy arc D9 fold #3 pattern for conformance-fixture
    hygiene.
    """
    _registry.pop(backend_id, None)


def get_persona_backend(backend_id: str) -> type[PersonaBackend]:
    """Return the ``PersonaBackend`` class registered under ``backend_id``.

    Raises ``BackendNotRegistered`` when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., ``cls(personas_root)`` for the filesystem
    backend).

    Args:
        backend_id: backend identifier to look up.

    Returns:
        The registered backend class.

    Raises:
        BackendNotRegistered: ``backend_id`` is not in the registry.
    """
    if backend_id not in _registry:
        raise BackendNotRegistered(
            f"No PersonaBackend registered under {backend_id!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[backend_id]  # type: ignore[return-value]


def list_persona_backends() -> list[str]:
    """Return registered backend ids in lexicographic order."""
    return sorted(_registry.keys())


# ──────────────────────────────────────────────────────────────────────────────
# URL factory hook registry

# Registry: scheme -> URL factory callable. URL factories are callables that
# accept a URL string and return a PersonaBackend instance. The filesystem
# URL factory is registered at module-import time via
# ``register_persona_backend_url_factory`` below.
_url_factory_registry: dict[str, object] = {}


def register_persona_backend_url_factory(scheme: str, factory: object) -> None:
    """Register a URL factory function under ``scheme``.

    URL factories are callables with signature
    ``(url: str) -> PersonaBackend``. Registered factories are used by
    ``get_default_persona_backend`` when ``ATOMIC_AGENTS_PERSONA_BACKEND_URL``
    is set.

    Args:
        scheme: the URL scheme this factory handles (e.g., ``"filesystem"``).
        factory: a callable accepting a URL string and returning a
            ``PersonaBackend`` instance.
    """
    _url_factory_registry[scheme] = factory
    _logger.debug("registered persona backend URL factory for scheme %r", scheme)


# ──────────────────────────────────────────────────────────────────────────────
# Default factory


def get_default_persona_backend(scope_root: Path) -> PersonaBackend:
    """Return the operator-pinned ``PersonaBackend`` instance for ``scope_root``.

    Reads ``ATOMIC_AGENTS_PERSONA_BACKEND`` from the environment (default
    ``"filesystem"``). The env var name follows the established pattern --
    ``ATOMIC_AGENTS_<PRIMITIVE>_BACKEND`` -- so operators who already pin
    the log or mandate backend use the same vocabulary.

    When ``ATOMIC_AGENTS_PERSONA_BACKEND_URL`` is also set, parses it via
    the registered URL factory for the URL's scheme (falling back to the
    filesystem default when not set).

    The ``scope_root`` parameter is the directory under which the
    ``.personas/`` subdirectory lives. For the filesystem backend, personas
    live at ``<scope_root>/.personas/<persona_id>/``.

    For programmatic operators who want to construct the backend themselves,
    the future ``AtomicAgent(..., persona_backend=...)`` constructor kwarg
    (wired in PR 2 of #62) bypasses this factory entirely.

    Per spec/33 implementer contract: construction is side-effect-free; env-var
    resolution happens at ``AtomicAgent.__init__`` time (PR 2 wires this).

    Per spec/33 implementer contract: raw env var values are redacted in
    error messages (heuristic strips after ``"://"`` + truncates) to avoid
    leaking accidentally-pasted credentials.

    Args:
        scope_root: root directory under which ``.personas/`` lives for the
            filesystem default.

    Returns:
        A ready-to-use ``PersonaBackend`` instance.

    Raises:
        BackendNotRegistered: the env var names an unknown backend.
    """
    from .filesystem import FilesystemPersonaBackend  # deferred -- avoids circular

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_PERSONA_BACKEND", "filesystem").strip().lower()
    )

    url = os.environ.get("ATOMIC_AGENTS_PERSONA_BACKEND_URL", "").strip()

    if raw_backend_id == "filesystem":
        if url:
            # URL factory path -- parse and construct from URL
            from .filesystem import make_filesystem_persona_backend_from_url

            return make_filesystem_persona_backend_from_url(url)
        return FilesystemPersonaBackend(scope_root / ".personas")

    # Unknown backend -- surface a fail-fast error with the full id list.
    # Credential-safety: raw_backend_id is redacted in case the operator
    # accidentally pasted a URL into ATOMIC_AGENTS_PERSONA_BACKEND.
    safe_id = _redact_for_error_message(raw_backend_id)
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_PERSONA_BACKEND={safe_id!r} is not a known backend. "
        f"Available: {list_persona_backends()}. "
        f"Unset the env var to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and truncates
    at ``max_len``. Mirrors the identical helper in
    ``atomic_agents.policy.backend`` and ``atomic_agents.mandate.backend``.
    """
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap registration


def _bootstrap_filesystem() -> None:
    """Register the filesystem backend at import time if not already registered.

    Called unconditionally at module bottom. Guards against double-import
    (e.g., test isolation that re-imports the module) by checking presence
    before registering. The idempotent guard also preserves any operator-
    registered class that was pinned under ``"filesystem"`` before a module
    reload.
    """
    if "filesystem" not in _registry:
        from .filesystem import FilesystemPersonaBackend

        _registry["filesystem"] = FilesystemPersonaBackend
        _logger.debug(
            "registered persona backend 'filesystem' -> FilesystemPersonaBackend"
        )


_bootstrap_filesystem()

"""atomic_agents.memory — memory backend registry and built-in backends.

Usage:
    from atomic_agents.memory import get_backend, register_backend
    from atomic_agents.memory import get_default_memory_backend
    from atomic_agents.memory.filesystem import FilesystemBackend

The registry maps backend name strings to backend classes. External packages
register their backends at import time via register_backend().

Built-in backends:
    "filesystem"  →  FilesystemBackend (default)

Operator override surface:
    ATOMIC_AGENTS_MEMORY_BACKEND env var (default "filesystem") selects the
    backend.  The AtomicAgent(..., memory_backend=...) constructor kwarg
    bypasses the env var entirely (kwarg-wins).  See docs/spec/20 §"Operator
    override surface" for the full contract.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import MemoryBackend

_logger = logging.getLogger(__name__)

# Registry: name → class
_REGISTRY: dict[str, type] = {}

# Future lazy-resolved backend ids (not eagerly registered — optional extras).
# Listed here so the BackendNotRegistered error message stays accurate even
# before the extra is installed.  Mirrors the ``{"redis"}`` augmentation in
# ``atomic_agents.locks.get_default_lock_backend``.
#
# "postgres" is listed here so doctor and registry-introspection tests show it
# as a known id before the 'postgres' extra is installed.  Actual registration
# happens inside get_default_memory_backend() when ATOMIC_AGENTS_MEMORY_BACKEND
# =postgres is detected — same lazy-import pattern used by PostgresLogBackend.
_LAZY_BACKEND_IDS: frozenset[str] = frozenset({"postgres", "pgvector-memory"})


def register_backend(name: str, cls: type) -> None:
    """Register a backend class under a name.

    Call this at import time in your backend package's __init__.py.
    The "filesystem" backend is pre-registered by this module.

    Args:
        name: short identifier (e.g., "sqlite", "postgres")
        cls:  class that implements the MemoryBackend protocol
    """
    if name in _REGISTRY:
        _logger.debug("replacing registered memory backend for backend_id=%r", name)
    _REGISTRY[name] = cls


def unregister_backend(name: str) -> None:
    """Remove a backend by name. No-op when not registered.

    Useful for test isolation (mirrors ``unregister_lock_backend``).
    """
    _REGISTRY.pop(name, None)


def get_backend(name: str) -> type:
    """Return the registered backend class for a name.

    Raises:
        BackendNotRegistered if the name is not in the registry.
    """
    from ..exceptions import BackendNotRegistered

    if name not in _REGISTRY:
        known = sorted(set(list_backends()) | _LAZY_BACKEND_IDS)
        raise BackendNotRegistered(
            f"No MemoryBackend registered under {name!r}. Available: {known}"
        )
    return _REGISTRY[name]


def list_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order.

    Used by diagnostic tooling (``atomic-agents doctor``) and by the
    registry-introspection tests.  Mirrors ``list_lock_backends()`` /
    ``list_log_backends()``.
    """
    return sorted(_REGISTRY.keys())


def get_default_memory_backend(
    agent_root: Path,
    *,
    lock_backend=None,
) -> "MemoryBackend":
    """Return the operator-pinned MemoryBackend instance for ``agent_root``.

    Reads ``ATOMIC_AGENTS_MEMORY_BACKEND`` from the environment (default
    ``"filesystem"``) and resolves it through the backend registry, so any
    backend registered via ``register_backend`` is selectable.  An unknown
    id fails fast with ``BackendNotRegistered`` listing the known ids.  The
    resolved backend is constructed with ``lock_backend`` threaded through so
    ``apply_staging`` serialises against the SAME backend instance that
    ``agent.call()`` holds — preventing the write-data-race documented in the
    DreamRunner apply_staging comment.

    Construction uses the uniform contract ``cls(agent_root, *,
    lock_backend=...)`` (spec/20 Implementer Contract MUST 1).
    Implementation-specific extras such as ``FilesystemBackend.memory_subdir``
    are NOT part of the uniform contract; the factory always uses their
    defaults.  Callers that need a non-default subdir construct
    ``FilesystemBackend`` directly.

    Factory design notes
    --------------------
    * Returns a fully-constructed INSTANCE (not a class) — callers must
      retain it; do not call this function twice and expect the same
      object.
    * Must be called lazily (from __init__ / constructor body), NEVER at
      module import time.  Resolving the env var at import time would fix
      the selection before the operator's test ``setUp`` or Docker env
      injection runs.
    * Bootstrap ordering: in AtomicAgent.__init__, this factory must be
      called AFTER ``lock_backend`` is resolved but the factory itself
      does NOT read any vault file — env-var-only selection is required
      because reading a vault file requires a working memory backend
      (chicken-and-egg paradox).  See spec/20 §"Operator override surface".

    For programmatic operators who want to construct the backend themselves
    (custom connection pool, injected lock), the
    ``AtomicAgent(..., memory_backend=...)`` constructor kwarg bypasses
    this factory entirely.
    """
    backend_id = (
        os.environ.get("ATOMIC_AGENTS_MEMORY_BACKEND", "filesystem").strip().lower()
    )

    # --- Lazy-import dispatch for backends that require optional extras ---
    #
    # "postgres" is not registered at module-load time (the psycopg3 extra is
    # optional).  When selected, we lazy-import the backend, register it in the
    # registry (so subsequent get_backend("postgres") calls don't lazy-load
    # again), and construct via the factory that reads the URL.
    #
    # Pattern mirrors logs/__init__.py PostgresLogBackend dispatch (lines ~299-323).
    if backend_id == "postgres":
        from .postgres import make_postgres_memory_backend_from_url  # noqa: PLC0415

        raw_url = os.environ.get("ATOMIC_AGENTS_MEMORY_BACKEND_URL")
        if not raw_url:
            raise ValueError(
                "ATOMIC_AGENTS_MEMORY_BACKEND=postgres requires "
                "ATOMIC_AGENTS_MEMORY_BACKEND_URL=postgresql://user:pass@host:5432/dbname"
            )
        if "postgres" not in _REGISTRY:
            from .postgres import PostgresMemoryBackend  # noqa: PLC0415

            register_backend("postgres", PostgresMemoryBackend)
        return make_postgres_memory_backend_from_url(
            raw_url, agent_root=agent_root, lock_backend=lock_backend
        )

    if backend_id == "pgvector-memory":
        from .pgvector import make_pgvector_memory_backend_from_url  # noqa: PLC0415

        raw_url = os.environ.get("ATOMIC_AGENTS_MEMORY_BACKEND_URL")
        if not raw_url:
            raise ValueError(
                "ATOMIC_AGENTS_MEMORY_BACKEND=pgvector-memory requires "
                "ATOMIC_AGENTS_MEMORY_BACKEND_URL=postgresql://user:pass@host:5432/dbname"
            )
        if "pgvector-memory" not in _REGISTRY:
            from .pgvector import PgvectorMemoryBackend  # noqa: PLC0415

            register_backend("pgvector-memory", PgvectorMemoryBackend)
        return make_pgvector_memory_backend_from_url(
            raw_url, agent_root=agent_root, lock_backend=lock_backend
        )

    # Dispatch through the registry so that ANY backend registered via
    # ``register_backend`` (including third-party extras that register at
    # import time) is reachable — not just the literal "filesystem" id.
    # ``get_backend`` raises ``BackendNotRegistered`` with the full known-id
    # list for genuine typos, so the fail-fast contract is preserved without
    # the self-contradictory "not known yet Available" message that a
    # hardcoded ``if backend_id == "filesystem"`` branch produced.
    #
    # The uniform construction contract (spec/20 Implementer Contract MUST 1)
    # guarantees every registered backend accepts ``(agent_root, *,
    # lock_backend=None)``.  ``FilesystemBackend``'s ``memory_subdir`` defaults
    # to "memory", so ``cls(agent_root, lock_backend=lock_backend)`` satisfies
    # both the filesystem path and every other registered backend uniformly.
    cls = get_backend(backend_id)
    return cls(agent_root, lock_backend=lock_backend)


# Register the built-in filesystem backend
def _register_defaults() -> None:
    from .filesystem import FilesystemBackend

    register_backend("filesystem", FilesystemBackend)


_register_defaults()

__all__ = [
    # Registry
    "register_backend",
    "unregister_backend",
    "get_backend",
    "list_backends",
    # Operator-config factory
    "get_default_memory_backend",
    # Protocol + dataclasses
    "MemoryBackend",
    "Note",
    "NoteRef",
    "VersionRef",
    "WritePolicy",
    "MemoryStats",
    "StagedMemory",
    # Exceptions
    "BackendNotRegistered",
    "MemoryBackendError",
    "StagingNotApplied",
    "VersionNotFound",
]


# Lazy imports to avoid circular dependency at module load time.
# Callers that import from atomic_agents.memory will trigger these.
def __getattr__(name: str):
    """Lazy attribute resolution for public types and exceptions."""
    _protocol_types = {
        "MemoryBackend",
        "Note",
        "NoteRef",
        "VersionRef",
        "WritePolicy",
        "MemoryStats",
        "StagedMemory",
    }
    _exception_names = {
        "BackendNotRegistered",
        "VersionNotFound",
        "StagingNotApplied",
        "MemoryBackendError",
    }

    if name in _protocol_types:
        from .backend import (
            MemoryBackend,
            Note,
            NoteRef,
            VersionRef,
            WritePolicy,
            MemoryStats,
            StagedMemory,
        )

        _locals = {
            "MemoryBackend": MemoryBackend,
            "Note": Note,
            "NoteRef": NoteRef,
            "VersionRef": VersionRef,
            "WritePolicy": WritePolicy,
            "MemoryStats": MemoryStats,
            "StagedMemory": StagedMemory,
        }
        return _locals[name]

    if name in _exception_names:
        from ..exceptions import (
            BackendNotRegistered,
            MemoryBackendError,
            StagingNotApplied,
            VersionNotFound,
        )

        _locals = {
            "BackendNotRegistered": BackendNotRegistered,
            "MemoryBackendError": MemoryBackendError,
            "StagingNotApplied": StagingNotApplied,
            "VersionNotFound": VersionNotFound,
        }
        return _locals[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

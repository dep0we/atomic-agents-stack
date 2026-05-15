"""Lock abstraction layer — Protocol + registry + canonical types.

This package establishes the lock abstraction in the protocol-pattern
series alongside MemoryBackend (#57, shipped), LLMBackend (#87, shipped),
JudgeBackend (#112, shipped), and the remaining Tier-2 backends. See
``docs/spec/21-lock-backend.md`` for the prose contract.

Public surface (scaffolding PR — no behavior change today):

    from atomic_agents.locks import (
        # Protocol contract
        LockBackend,
        # Canonical types
        LockHandle, LockCapabilities,
        # Reference impl
        FilesystemLockBackend,
        # Registry
        register_lock_backend, get_lock_backend,
        list_lock_backends, unregister_lock_backend,
    )

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"redis"``). Unlike the
LLM registry it stores backend *classes*, not instances — lock backends
are constructed per scope (one per agent root) and the registry's job is
to let an operator pick "filesystem vs Redis" for a deployment. The
caller (``AtomicAgent.__init__`` in PR 2) instantiates the chosen class
with its scope-specific args.

Thread-safety: registration is expected at import time (one-shot from
each backend's module); ``get_lock_backend`` is read-only and safe to
call from any thread. No lock is needed under that usage.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..exceptions import BackendNotRegistered, LockBusy, LockLost
from .backend import LockBackend
from .filesystem import FilesystemLockBackend
from .types import LockCapabilities, LockHandle

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "LockBackend",
    # Canonical types
    "LockHandle",
    "LockCapabilities",
    # Reference implementations
    "FilesystemLockBackend",
    # Registry
    "register_lock_backend",
    "unregister_lock_backend",
    "get_lock_backend",
    "list_lock_backends",
    # Operator-config factory
    "get_default_lock_backend",
    # Heartbeat check helper (re-exported for call-site use)
    "check_lock_lost",
    # Exceptions (re-exported from atomic_agents.exceptions for ergonomic
    # callers that import the backend and the exceptions together).
    "LockBusy",
    "LockLost",
]


# Process-local registry: backend_id → backend class. Backend classes
# (not instances) because lock backends carry per-scope construction
# args — the agent instantiates ``FilesystemLockBackend(agent_root)``
# at agent init time; the registry's role is the operator-pin lookup
# that maps ``"filesystem"`` → ``FilesystemLockBackend``.
_registry: dict[str, type] = {}


def register_lock_backend(backend_id: str, cls: type) -> None:
    """Register a LockBackend implementation under ``backend_id``.

    Typically called once at module-import time from each backend's
    package (the default ``"filesystem"`` registration happens at the
    bottom of this file).

    Re-registering the same ``backend_id`` replaces the existing
    binding and logs at DEBUG — intentional. Operators occasionally
    want to swap in a wrapper (e.g., a ``MetricsLockBackend`` that
    decorates the filesystem backend with timing data) without first
    unregistering the original.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered lock backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_lock_backend(backend_id: str) -> None:
    """Remove a backend by ``backend_id``. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a
    backend.
    """
    _registry.pop(backend_id, None)


def get_lock_backend(backend_id: str) -> type:
    """Return the registered LockBackend class for ``backend_id``.

    Raises ``BackendNotRegistered`` when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., ``cls(agent_root)`` for the filesystem
    backend; ``cls(redis_client, key_prefix=...)`` for a Redis backend).
    """
    if backend_id not in _registry:
        raise BackendNotRegistered(
            f"No LockBackend registered under {backend_id!r}. "
            f"Available: {list(_registry.keys())}"
        )
    return _registry[backend_id]


def list_lock_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order.

    Used by diagnostic tooling (``atomic-agents doctor`` once a check
    lands for the lock layer) and by the registry-introspection tests.
    """
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time. The choice
# to register at module import (rather than lazily) matches the
# MemoryBackend pattern (``atomic_agents/memory/__init__.py:59``) —
# the default is always available without an extra resolution step.
register_lock_backend("filesystem", FilesystemLockBackend)


def get_default_lock_backend(scope_root: Path) -> LockBackend:
    """Return the operator-pinned LockBackend instance for ``scope_root``.

    Reads ``ATOMIC_AGENTS_LOCK_BACKEND`` from the environment (default
    ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_LOCK_BACKEND_URL`` and any backend-specific tuning
    env vars (``ATOMIC_AGENTS_LOCK_REDIS_TTL_SECONDS``,
    ``ATOMIC_AGENTS_LOCK_REDIS_HEARTBEAT_SECONDS``,
    ``ATOMIC_AGENTS_LOCK_REDIS_KEY_PREFIX``).

    The ``scope_root`` parameter is honored by the filesystem backend
    (lock files live under that path); distributed backends ignore it
    in favor of the key_prefix scoping inherent to their key namespace.

    For programmatic operators who want to construct the backend
    themselves (custom Redis client, custom Postgres pool), the
    ``AtomicAgent(..., lock_backend=...)`` constructor kwarg bypasses
    this factory entirely.

    See spec/21 §"Operator override surface" for the full env-var
    reference + the env-var-vs-kwarg trade-off rationale.
    """
    backend_id = os.environ.get(
        "ATOMIC_AGENTS_LOCK_BACKEND", "filesystem"
    ).strip().lower()

    if backend_id == "filesystem":
        return FilesystemLockBackend(scope_root)

    if backend_id == "redis":
        # Lazy import — avoids importing ``redis`` (optional extra)
        # at framework startup when the operator hasn't selected it.
        from .redis import make_redis_backend_from_url

        url = os.environ.get("ATOMIC_AGENTS_LOCK_BACKEND_URL")
        if not url:
            raise ValueError(
                "ATOMIC_AGENTS_LOCK_BACKEND=redis requires "
                "ATOMIC_AGENTS_LOCK_BACKEND_URL to be set "
                "(e.g., redis://localhost:6379/0)."
            )
        return make_redis_backend_from_url(url)

    # Unknown backend_id — surface a fail-fast error with the FULL
    # known-id list so operators can spot the typo. Includes
    # lazy-resolved backends like ``"redis"`` that aren't in the
    # registry's eager list (Step 11 adversarial P0-3 — operators who
    # typed ``redus`` got "Available: ['filesystem']" and concluded
    # Redis wasn't supported).
    known_ids = sorted(set(list_lock_backends()) | {"redis"})
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_LOCK_BACKEND={backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var to use "
        f"the filesystem default."
    )


# Expose the heartbeat check helper from the redis submodule lazily —
# importing it eagerly would pull in ``redis.py`` even when the operator
# hasn't selected the redis backend. The lazy proxy lets call sites
# write ``from atomic_agents.locks import check_lock_lost`` without
# paying the import cost for filesystem-only deployments.
def check_lock_lost(handle: LockHandle) -> None:
    """Raise ``LockLost`` if a heartbeat thread detected lease expiry.

    Long-running call sites (``agent.call()`` multi-turn loop,
    ``dream.start()`` pipeline) invoke this between iterations of
    their work loops so they abort cleanly when a lease-backed
    backend's lease was lost. No-op for filesystem-backed handles
    (no heartbeat thread runs). See spec/21 §"Lease and heartbeat".
    """
    # Lazy-import the redis-side helper. The implementation lives in
    # ``atomic_agents/locks/redis.py`` because the type discrimination
    # depends on ``_RedisHandleState`` which is defined there.
    from .redis import check_lock_lost as _impl
    _impl(handle)

"""Canonical types for the LockBackend Protocol (spec/21).

The framework's lock sites — ``agent.call()``, ``dream.start()``,
``memory.apply_staging()`` — talk to lock backends only through these
canonical types. Each backend translates between its native primitives
(``fcntl.flock`` fds, Redis lease tokens, Postgres advisory lock keys)
and the canonical types at its call boundary.

Scaffolding PR (#60 PR 1): no backend implements the Protocol yet, and
``_locks.AgentLock`` continues to serve the four existing lock sites.
PR 2 of the arc wires backends into the call sites; the canonical types
exist so PR 2 has a stable contract to wire against.

All types are ``@dataclass(frozen=True)`` so they are immutable and
comparable by value — safe to pass across the agent / backend /
diagnostic boundary without defensive copying. ``LockHandle`` adds a
context-manager protocol so call sites can use ``with backend.acquire()
as handle:`` naturally; ``__exit__`` calls back into the issuing backend
via the ``_backend`` field (set by ``acquire()``; intentionally absent
from the public constructor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import LockBackend


@dataclass(frozen=True)
class LockCapabilities:
    """Per-backend capability declaration — see Protocol surface in spec/21.

    The conformance suite asserts claim-vs-behavior parity: a backend
    that claims ``supports_reentrancy=True`` MUST let the same process
    re-acquire its own held lock; one that claims ``supports_lease=True``
    MUST honor ``renew()`` by extending the lease deadline. Honest
    capabilities let callers fail fast against incompatible backends
    rather than discovering the mismatch mid-operation.

    Fields:
        single_host_only: True when the backend's semantics only hold
            on a single host. ``FilesystemLockBackend`` is True — ``fcntl
            flock`` is not reliable across NFS, container kernel
            boundaries, or distributed filesystems. A Redis or Postgres
            advisory backend is False.
        supports_reentrancy: True when the same process can re-acquire
            its own held lock without raising. ``FilesystemLockBackend``
            is False (re-acquire raises ``LockBusy``); a Redis backend
            implementing per-(name, holder) ref-counting can claim True.
        supports_lease: True when the backend grants time-bounded leases
            that the holder must renew before expiry. ``FilesystemLock``
            is False (POSIX fcntl locks have no TTL); a Redis backend
            with SET ... EX is True and the holder calls ``renew()``
            periodically to extend the deadline.
    """

    single_host_only: bool
    supports_reentrancy: bool
    supports_lease: bool
    # spec/40 addendum: Exportable Protocol composition.
    # FilesystemLockBackend = True (location-map export only; zero lock records).
    # RedisLockBackend = False (no persistent lock state to export).
    # Default False so existing instantiation sites without this kwarg keep working.
    supports_canonical_export: bool = False


@dataclass(frozen=True)
class LockHandle:
    """A granted lock — opaque-from-the-caller's-perspective receipt.

    Returned by ``LockBackend.acquire(name, timeout)``. Callers MUST pass
    the same handle back to ``release()`` / ``renew()`` so the backend
    can locate its internal state (file descriptor, lease token, etc.)
    via the ``backend_state`` slot.

    The handle is also the context manager: ``with backend.acquire(name)
    as h: ...`` releases on exit (including exception paths). Backends
    set the ``_backend`` reference inside ``acquire()`` so ``__exit__``
    can route ``release()`` correctly without the caller having to keep
    a separate reference.

    Fields:
        name: the semantic identifier passed to ``acquire()``. Empty
            string ``""`` is the default and maps to the agent's bare
            ``.lock`` artifact on the filesystem backend; ``"dream"``
            maps to ``.dream.lock``. The name space is the backend's
            to interpret.
        acquired_at: wall-clock time the lock was granted, as
            ``time.time()``. For diagnostic / audit use only — clock
            skew makes this unsuitable for ordering decisions.
        holder_pid: OS process id that holds the lock at the moment of
            acquisition. Backends serving distributed callers may
            populate this with the local agent's pid even when the
            lease is observable from another host.
        backend_state: opaque per-backend data — a filesystem backend
            stashes the open file descriptor here; a Redis backend
            stashes the lease token returned by SET NX. Consumers
            outside the issuing backend MUST NOT inspect this field;
            it is the backend's internal handle to its own resource.

    Implementation note: ``_backend`` is mutable (set by ``acquire()``
    after construction) via ``object.__setattr__`` so frozen-dataclass
    immutability still holds for public fields. Callers that need a
    hashable handle should construct one out of ``(name, holder_pid,
    acquired_at)`` since ``backend_state`` may be unhashable.
    """

    name: str
    acquired_at: float
    holder_pid: int
    backend_state: Any = None
    # Non-public reference to the issuing backend — set by acquire()
    # via object.__setattr__ to bypass frozen-ness. Used by __exit__
    # to route release() without the caller having to keep a separate
    # backend reference.
    _backend: "LockBackend | None" = field(default=None, repr=False, compare=False)

    def __enter__(self) -> "LockHandle":
        # Reject re-entry of a handle whose lock has already been released.
        # Without this guard, `with already_released_handle: ...` would
        # silently enter a phantom critical section: `__exit__` is a
        # no-op (backend_state is None → release() returns early), so
        # the body runs with NO lock held even though the syntax looks
        # like a guarded region. That violates CLAUDE.md rule #8
        # ("no half-finished state") in the worst possible way: a
        # quietly-broken invariant rather than a noisy crash.
        #
        # Distinguish the released case from the never-acquired case
        # (hand-built handle with _backend=None, used in rare tests):
        # only released handles have BOTH _backend set AND backend_state
        # cleared. Hand-built handles with _backend=None continue to
        # __enter__/__exit__ as a no-op — that contract is preserved.
        if self._backend is not None and self.backend_state is None:
            raise RuntimeError(
                f"LockHandle for {self.name!r} cannot be re-entered "
                f"after release; call backend.acquire() again to "
                f"obtain a fresh handle"
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # The backend reference is set inside ``acquire()`` immediately
        # before the handle is returned. A handle constructed by hand
        # without going through ``acquire()`` will have ``_backend=None``
        # and ``__exit__`` is a no-op — that's the correct shape for
        # the rare test that wants to pre-build a handle for assertions.
        if self._backend is not None:
            self._backend.release(self)

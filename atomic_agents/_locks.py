"""DEPRECATED: use ``atomic_agents.locks.FilesystemLockBackend`` instead.

This module is a thin backwards-compatibility shim. ``AgentLock`` (class)
and ``acquire()`` (module-level contextmanager) are preserved at this
import path so existing call sites and operator runbooks keep working,
but every public symbol here emits ``DeprecationWarning`` and delegates
to ``atomic_agents.locks.FilesystemLockBackend`` under the hood.

# SUNSET v1.0
Per CLAUDE.md rule #14 ("Backward compatibility by default"), this shim
is planned for removal in the v1.0 release. New code MUST import from
``atomic_agents.locks`` directly. Existing code should migrate via the
mechanical substitution:

    # Before
    from atomic_agents._locks import AgentLock
    with AgentLock(agent_root, wait_seconds=30):
        ...

    # After
    from atomic_agents.locks import FilesystemLockBackend
    with FilesystemLockBackend(agent_root).acquire("", timeout=30):
        ...

The on-disk artifact at ``<agent_root>/.lock`` and the ``pid=<pid>
acquired=<ts>`` payload format are preserved byte-shape so external
diagnostic scripts (including ``atomic-agents doctor``) continue to
work without change. See ``docs/spec/21-lock-backend.md`` for the
full LockBackend Protocol contract this shim wraps.
"""

from __future__ import annotations

import contextlib
import warnings
from pathlib import Path

from .exceptions import AgentLockBusy  # noqa: F401 — back-compat re-export
from .locks.filesystem import FilesystemLockBackend
from .locks.types import LockHandle


# SUNSET v1.0
class AgentLock:
    """DEPRECATED: thin wrapper over ``FilesystemLockBackend.acquire("")``.

    Preserves the legacy per-agent-flock construction signature
    ``AgentLock(agent_root, wait_seconds=...)`` while delegating the
    actual acquire/release to the new Protocol-shaped backend.

    Sunset planned for v1.0 — new code should construct
    ``FilesystemLockBackend(agent_root).acquire("", timeout=...)``
    directly.
    """

    def __init__(
        self,
        agent_root: Path,
        wait_seconds: float = 0.0,
        poll_interval: float = 0.5,
    ) -> None:
        warnings.warn(
            "atomic_agents._locks.AgentLock is deprecated; use "
            "atomic_agents.locks.FilesystemLockBackend instead. "
            "See docs/spec/21-lock-backend.md. Sunset planned for v1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.agent_root = agent_root
        self.wait_seconds = wait_seconds
        # ``lock_path`` is preserved as a public attribute so legacy
        # callers that inspected ``lock.lock_path`` (e.g., diagnostic
        # scripts) keep working.
        self.lock_path = agent_root / ".lock"
        self._backend = FilesystemLockBackend(agent_root, poll_interval=poll_interval)
        self._handle: LockHandle | None = None

    @property
    def _fd(self) -> int | None:
        """Legacy attribute exposed for backwards compatibility.

        Returns the open file descriptor underlying the held lock, or
        ``None`` when the lock is not held. Tests that asserted
        ``lock._fd is not None`` after acquire (see
        ``tests/test_locks.py``) keep working unchanged.
        """
        if self._handle is None:
            return None
        # ``LockHandle.backend_state`` carries the open fd for the
        # filesystem backend per spec/21. Wiped to None on release.
        state = self._handle.backend_state
        return state if isinstance(state, int) else None

    def __enter__(self) -> "AgentLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def acquire(self) -> None:
        """Acquire the lock; raise ``AgentLockBusy`` on timeout."""
        self._handle = self._backend.acquire("", timeout=self.wait_seconds)

    def release(self) -> None:
        """Release the lock. Idempotent — safe to call twice."""
        if self._handle is not None:
            self._backend.release(self._handle)
            self._handle = None


# SUNSET v1.0
@contextlib.contextmanager
def acquire(agent_root: Path, wait_seconds: float = 0.0):
    """DEPRECATED: contextmanager wrapper for ``AgentLock``.

    Preserved for backwards compatibility with the legacy module-level
    ``acquire(agent_root, wait_seconds)`` shape. Emits a
    ``DeprecationWarning`` and delegates to ``AgentLock``.

    Sunset planned for v1.0 — new code should use
    ``with FilesystemLockBackend(agent_root).acquire("",
    timeout=wait_seconds) as handle: ...`` directly.
    """
    warnings.warn(
        "atomic_agents._locks.acquire() is deprecated; use "
        "atomic_agents.locks.FilesystemLockBackend instead. "
        "Sunset planned for v1.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    lock = AgentLock(agent_root, wait_seconds=wait_seconds)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()

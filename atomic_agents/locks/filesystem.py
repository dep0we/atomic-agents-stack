"""FilesystemLockBackend — POSIX ``fcntl.flock`` reference implementation.

This is the default backend for single-host deployments. It wraps the
same ``fcntl.flock`` semantics ``_locks.AgentLock`` has used since
spec/04, with two surface changes:

1. Backend-bound scope. ``FilesystemLockBackend(scope_root)`` ties the
   backend instance to a directory; ``acquire(name)`` looks up
   ``<scope_root>/.lock`` (empty name) or ``<scope_root>/.<name>.lock``
   (non-empty name). PR 2 of #60 wires the agent's main lock to a
   bare-name acquire so the on-disk artifact stays ``<agent>/.lock``;
   dream's existing ``<agent>/dreams/.lock`` becomes a sub-backend
   construction or a named acquire — settled in PR 2.

2. Returns a ``LockHandle``. The handle's ``backend_state`` slot
   carries the open file descriptor; ``release()`` closes it. The
   handle is its own context manager via ``LockHandle.__enter__`` /
   ``__exit__``.

Reentrancy: NOT REENTRANT. A second ``acquire()`` of the same name from
the same process raises ``LockBusy``. ``fcntl.flock`` semantics within
the same process are subtle (LOCK_EX from the same fd is a no-op; a
second open + LOCK_EX from the same process succeeds because flock is
fd-scoped, not process-scoped) — the backend layers a process-local
held-names set on top of the OS primitive to give the simple, honest
contract this backend advertises.

Lease: NO LEASE. ``renew()`` returns True unconditionally. POSIX advisory
locks have no TTL; if the holder process dies, the kernel releases the
lock automatically (this is the recovery mechanism, not a lease).
"""

from __future__ import annotations

import errno
import fcntl
import os
import threading
import time
from pathlib import Path

from ..exceptions import LockBusy
from .types import LockCapabilities, LockHandle


class FilesystemLockBackend:
    """POSIX ``fcntl.flock`` LockBackend — single-host advisory locking.

    Conforms to the ``LockBackend`` Protocol. Constructed once per
    scope; the scope_root is the directory under which lock files
    live. The agent's main lock backend is rooted at the agent's
    directory; dream / memory sub-scopes can share that backend (using
    named acquires) or construct their own (rooted at the sub-dir).

    Thread-safety: the process-local held-names set is guarded by a
    ``threading.Lock`` so concurrent threads in the same process can
    safely call ``acquire()`` / ``release()``. The OS-level
    ``fcntl.flock`` is fd-scoped (not thread-scoped); the held-names
    set is what enforces the non-reentrant contract within a process.

    Args:
        scope_root: directory under which lock files live. Created
            (parents included) on first ``acquire()``.
        poll_interval: seconds between retry attempts while waiting
            for a busy lock. Default 0.5s matches legacy
            ``AgentLock``.
    """

    # ``backend_id`` is a ``@property`` (not a class attribute) for parity
    # with the established LLM Protocol pattern (``atomic_agents/llm/
    # backend.py`` defines ``provider_id`` as a property). The property
    # form prevents instance-level mutation — ``b.backend_id = "spoof"``
    # would silently succeed against a class attribute and desynchronize
    # diagnostic logging from registry lookups (which always see
    # ``"filesystem"`` via class identity).
    @property
    def backend_id(self) -> str:
        return "filesystem"

    def __init__(self, scope_root: Path, *, poll_interval: float = 0.5) -> None:
        self._scope_root = Path(scope_root)
        self._poll_interval = poll_interval
        # Process-local set of currently-held lock names. Enforces the
        # non-reentrant contract: ``fcntl.flock`` from the same process
        # via a second fd would succeed (flock is fd-scoped); this set
        # makes the second call raise instead.
        self._held: set[str] = set()
        self._held_lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────
    # Protocol surface

    def acquire(self, name: str = "", timeout: float = 0.0) -> LockHandle:
        """Acquire the named lock or raise ``LockBusy`` after ``timeout``."""
        # Reentrancy check — fail fast before any filesystem work so the
        # caller gets the same diagnostic shape whether the conflict is
        # cross-process or in-process. The Protocol contract is "raises
        # ``LockBusy``" either way.
        with self._held_lock:
            if name in self._held:
                raise LockBusy(
                    f"FilesystemLockBackend({self._scope_root!s}) "
                    f"already holds lock {name!r} in this process "
                    f"(backend is non-reentrant; "
                    f"supports_reentrancy=False)"
                )

        lock_path = self._lock_path_for(name)
        self._scope_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Acquired — write PID + acquired-at for debugging.
                # Matches the legacy ``_locks.AgentLock`` on-disk format
                # so external scripts and ``doctor.check_locks`` keep
                # reading the same shape.
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                acquired_at = time.time()
                os.write(
                    fd,
                    f"pid={os.getpid()} acquired={acquired_at}\n".encode(),
                )
                with self._held_lock:
                    self._held.add(name)
                handle = LockHandle(
                    name=name,
                    acquired_at=acquired_at,
                    holder_pid=os.getpid(),
                    backend_state=fd,
                )
                # Bypass frozen-ness to wire the backend reference for
                # ``LockHandle.__exit__``. Documented in
                # ``types.LockHandle``.
                object.__setattr__(handle, "_backend", self)
                return handle
            except (BlockingIOError, OSError) as exc:
                # ``flock(LOCK_NB)`` on a held lock returns ``EWOULDBLOCK``
                # which Python surfaces as ``BlockingIOError`` (a
                # subclass of ``OSError``). Re-raise any other OSError —
                # e.g., a permission failure on the lock file should
                # not be silently retried.
                if not (
                    isinstance(exc, BlockingIOError) or exc.errno == errno.EWOULDBLOCK
                ):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusy(
                        f"FilesystemLockBackend: lock at {lock_path!s} "
                        f"held by another process; waited {timeout}s"
                    )
                time.sleep(self._poll_interval)

    def release(self, handle: LockHandle) -> None:
        """Release the lock. Idempotent."""
        # ``handle.backend_state`` is the open file descriptor; a None
        # value means the handle has already been released (idempotent
        # double-release is contract-compliant).
        fd = handle.backend_state
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            # Wipe the stored fd so a follow-up double-release is a
            # no-op rather than a use-after-close. The handle is a
            # frozen dataclass; bypass frozen-ness via
            # ``object.__setattr__`` because the backend OWNS the
            # backend_state slot per spec/21.
            object.__setattr__(handle, "backend_state", None)
            with self._held_lock:
                self._held.discard(handle.name)

    def renew(self, handle: LockHandle) -> bool:
        """No-op for filesystem backends — POSIX flock has no TTL.

        Returns True unconditionally so callers wrapping long-running
        operations can invoke ``renew()`` periodically without
        branching on ``capabilities().supports_lease``.
        """
        return True

    def is_held(self, name: str = "") -> bool:
        """Probe whether the named lock is held by any process.

        Racy by design — see ``LockBackend.is_held`` docstring. Uses a
        non-blocking ``flock`` attempt and releases immediately on
        success; an EWOULDBLOCK signals "held".
        """
        lock_path = self._lock_path_for(name)
        if not lock_path.exists():
            return False
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, BlockingIOError) or (
                    isinstance(exc, OSError) and exc.errno == errno.EWOULDBLOCK
                ):
                    return True
                raise
            # Got the lock — release immediately. The lock was NOT held
            # at the moment of check (it may be held a microsecond from
            # now; that's the racy-by-design property).
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def capabilities(self) -> LockCapabilities:
        """Filesystem capabilities — single-host, non-reentrant, no lease."""
        return LockCapabilities(
            single_host_only=True,
            supports_reentrancy=False,
            supports_lease=False,
            supports_canonical_export=True,  # spec/40 addendum — location-map only
        )

    def export(self, query=None):
        """Export lock backend configuration as a LockExport canonical object (spec/40).

        Returns the location map only (scope_root + backend_id). Runtime lock
        state is ephemeral and MUST NOT be exported. Always returns zero lock
        records — this is correct by design (spec/40 §"LockBackend export contract").

        Args:
            query: ``LockExportQuery | None`` — unused, present for Protocol uniformity.

        Returns:
            ``LockExport`` with scope_root and backend_id. lock_file_names is [].
        """
        from ..export.filesystem import export_lock
        from ..export.types import LockExportQuery

        if query is None:
            query = LockExportQuery()
        return export_lock(self, query)

    def export_all(self):
        """Convenience wrapper. Equivalent to export(None)."""
        return self.export(None)

    def scope(self, sub_path: str) -> "FilesystemLockBackend":
        """Return a new FilesystemLockBackend rooted at ``<scope_root>/<sub_path>``.

        Used by ``DreamRunner`` to derive its dream-lock backend
        (``agent.lock_backend.scope("dreams")``) from the agent's
        operator-provided lock backend. Preserves the configured
        ``poll_interval``.
        """
        if not sub_path:
            raise ValueError(
                "FilesystemLockBackend.scope(sub_path) requires a non-empty "
                "sub_path; use the existing backend for the same scope."
            )
        return FilesystemLockBackend(
            self._scope_root / sub_path,
            poll_interval=self._poll_interval,
        )

    # ──────────────────────────────────────────────────────────────────
    # Internal helpers

    def _lock_path_for(self, name: str) -> Path:
        """Map the semantic ``name`` to an on-disk lock file.

        ``""`` → ``<scope>/.lock`` — matches the legacy
        ``_locks.AgentLock`` artifact so doctor and external scripts
        keep working without migration.
        ``"<name>"`` → ``<scope>/.<name>.lock`` — e.g., ``"dream"``
        maps to ``<scope>/.dream.lock``.
        """
        if name == "":
            return self._scope_root / ".lock"
        return self._scope_root / f".{name}.lock"

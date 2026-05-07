"""Per-agent file locking — flock-based, with stale-lock recovery via OS.

Per spec/04 + shared-helper.md. The agent acquires its lock before any vault
write; releases on completion. Crashes release the lock automatically (OS
releases flock on process death).

Cron jobs that find the lock held by another process should fail-fast and
retry next cycle. Skill sessions can wait briefly.
"""

from __future__ import annotations
import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path

from .exceptions import AgentLockBusy


class AgentLock:
    """Per-agent flock, used as a context manager.

    Usage:
        with AgentLock(agent_root, wait_seconds=30):
            # do all vault writes here
            ...
    """

    def __init__(self, agent_root: Path, wait_seconds: float = 0.0,
                  poll_interval: float = 0.5):
        """
        agent_root: <agents_root>/<agent_name>/
        wait_seconds: how long to wait for a busy lock before giving up
                      (0 = fail immediately; useful for cron)
        """
        self.agent_root = agent_root
        self.wait_seconds = wait_seconds
        self.poll_interval = poll_interval
        self.lock_path = agent_root / ".lock"
        self._fd: int | None = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def acquire(self) -> None:
        """Acquire the lock or raise AgentLockBusy."""
        self.agent_root.mkdir(parents=True, exist_ok=True)
        # Open the lock file (create if needed)
        self._fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)

        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Got it — write our PID for debugging
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.ftruncate(self._fd, 0)
                os.write(self._fd, f"pid={os.getpid()} acquired={time.time()}\n".encode())
                return
            except (BlockingIOError, OSError) as e:
                if e.errno != errno.EWOULDBLOCK and not isinstance(e, BlockingIOError):
                    raise
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise AgentLockBusy(
                        f"Agent lock at {self.lock_path} held by another process; "
                        f"waited {self.wait_seconds}s"
                    )
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


@contextlib.contextmanager
def acquire(agent_root: Path, wait_seconds: float = 0.0):
    """Functional alias for `with AgentLock(...) as lock:`."""
    lock = AgentLock(agent_root, wait_seconds=wait_seconds)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()

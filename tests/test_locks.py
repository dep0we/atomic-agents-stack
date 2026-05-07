"""Tests for atomic_agents._locks."""

import multiprocessing
import time
from pathlib import Path

import pytest

from atomic_agents._locks import AgentLock
from atomic_agents.exceptions import AgentLockBusy


def test_lock_acquires_and_releases(tmp_path):
    lock = AgentLock(tmp_path)
    lock.acquire()
    assert (tmp_path / ".lock").exists()
    lock.release()


def test_lock_context_manager(tmp_path):
    with AgentLock(tmp_path) as lock:
        assert lock._fd is not None
    # _fd should be released after exit
    # (verifying via fd value being None)


def _hold_lock(agent_root_str: str, hold_seconds: float):
    """Helper: child process that holds the lock for some time."""
    from atomic_agents._locks import AgentLock
    from pathlib import Path
    lock = AgentLock(Path(agent_root_str))
    lock.acquire()
    time.sleep(hold_seconds)
    lock.release()


def test_second_lock_fails_fast(tmp_path):
    """When wait_seconds=0, the second lock should fail immediately."""
    proc = multiprocessing.Process(target=_hold_lock, args=(str(tmp_path), 1.5))
    proc.start()
    time.sleep(0.3)  # let the child grab the lock first
    try:
        with pytest.raises(AgentLockBusy):
            lock = AgentLock(tmp_path, wait_seconds=0)
            lock.acquire()
    finally:
        proc.join()


def test_second_lock_waits_then_acquires(tmp_path):
    """When wait_seconds > hold time, the second lock should eventually acquire."""
    proc = multiprocessing.Process(target=_hold_lock, args=(str(tmp_path), 0.5))
    proc.start()
    time.sleep(0.1)  # let the child grab the lock first
    try:
        lock = AgentLock(tmp_path, wait_seconds=2.0, poll_interval=0.1)
        lock.acquire()
        lock.release()
    finally:
        proc.join()

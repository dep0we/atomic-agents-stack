"""Filesystem-specific LockBackend tests — on-disk shape and POSIX semantics.

Conformance tests live in ``test_lock_protocol_conformance.py``. This
file covers behavior unique to the filesystem reference impl:

1. ``name=""`` maps to ``<scope>/.lock`` — preserves legacy on-disk shape.
2. ``name="dream"`` maps to ``<scope>/.dream.lock``.
3. Lock file contains ``pid=<pid> acquired=<ts>`` — matches legacy format.
4. ``scope_root`` is auto-created on first acquire.
5. Custom ``poll_interval`` is honored.
6. Kernel-level crash recovery — child process death releases the lock.
7. ``is_held`` after process death observes the released state.
8. Registry: ``"filesystem"`` resolves to ``FilesystemLockBackend``.
9. Constructor accepts ``str`` paths (auto-cast to ``Path``).
10. ``backend_id`` is ``"filesystem"``.
"""

from __future__ import annotations

import errno
import fcntl
import multiprocessing
import os
import re
import time
from pathlib import Path

import pytest

from atomic_agents.locks import FilesystemLockBackend, get_lock_backend
from atomic_agents.locks.filesystem import FilesystemLockBackend as DirectFLB


def test_backend_id_is_filesystem():
    backend = FilesystemLockBackend(Path("/tmp"))
    assert backend.backend_id == "filesystem"


def test_empty_name_maps_to_dot_lock(tmp_path):
    """Backward-compat sentinel — doctor + external scripts rely on
    the bare ``.lock`` path."""
    backend = FilesystemLockBackend(tmp_path)
    handle = backend.acquire("", timeout=0.0)
    try:
        assert (tmp_path / ".lock").exists()
        # No other lock file should have been created.
        lock_files = list(tmp_path.glob(".*lock"))
        assert lock_files == [tmp_path / ".lock"]
    finally:
        backend.release(handle)


def test_named_lock_maps_to_dot_name_dot_lock(tmp_path):
    """``acquire("dream")`` → ``<scope>/.dream.lock``."""
    backend = FilesystemLockBackend(tmp_path)
    handle = backend.acquire("dream", timeout=0.0)
    try:
        assert (tmp_path / ".dream.lock").exists()
    finally:
        backend.release(handle)


def test_lock_file_contents_match_legacy_format(tmp_path):
    """``pid=<pid> acquired=<ts>`` format — matches ``_locks.AgentLock``."""
    backend = FilesystemLockBackend(tmp_path)
    handle = backend.acquire("", timeout=0.0)
    try:
        content = (tmp_path / ".lock").read_text()
        match = re.match(r"^pid=(\d+) acquired=([\d.]+)\n?$", content)
        assert match is not None, f"unexpected lock file content: {content!r}"
        assert int(match.group(1)) == os.getpid()
    finally:
        backend.release(handle)


def test_scope_root_auto_created(tmp_path):
    """Backend creates parent directory on first acquire."""
    new_scope = tmp_path / "agents" / "caldwell"
    assert not new_scope.exists()
    backend = FilesystemLockBackend(new_scope)
    handle = backend.acquire("", timeout=0.0)
    try:
        assert new_scope.exists()
        assert (new_scope / ".lock").exists()
    finally:
        backend.release(handle)


def test_constructor_accepts_str_path(tmp_path):
    """``FilesystemLockBackend(str)`` casts to Path internally."""
    backend = FilesystemLockBackend(str(tmp_path))
    handle = backend.acquire("", timeout=0.0)
    try:
        assert (tmp_path / ".lock").exists()
    finally:
        backend.release(handle)


def test_custom_poll_interval_honored(tmp_path):
    """Smaller poll_interval → faster retry cadence during contention."""
    fast = FilesystemLockBackend(tmp_path, poll_interval=0.01)
    assert fast._poll_interval == 0.01
    slow = FilesystemLockBackend(tmp_path, poll_interval=2.0)
    assert slow._poll_interval == 2.0


def _hold_then_exit_uncleanly(scope_root_str: str, name: str) -> None:
    """Child process: acquire and then die without explicit release.

    Kernel ``flock`` semantics MUST release the lock on process death;
    that's the filesystem backend's crash-recovery story.
    """
    backend = FilesystemLockBackend(Path(scope_root_str))
    backend.acquire(name=name, timeout=0.0)
    # Exit without releasing. The kernel will release ``flock`` on
    # process termination — that IS the recovery semantic.
    os._exit(0)


def test_kernel_releases_lock_on_holder_death(tmp_path):
    """Holder process death → kernel releases flock → next acquire grants."""
    child = multiprocessing.Process(
        target=_hold_then_exit_uncleanly,
        args=(str(tmp_path), ""),
    )
    child.start()
    child.join(timeout=5)
    assert child.exitcode == 0
    # Lock file may still exist on disk, but the kernel released the
    # flock — a new acquire should grant immediately.
    backend = FilesystemLockBackend(tmp_path)
    handle = backend.acquire("", timeout=0.5)
    backend.release(handle)


def test_is_held_false_after_holder_dies(tmp_path):
    child = multiprocessing.Process(
        target=_hold_then_exit_uncleanly,
        args=(str(tmp_path), ""),
    )
    child.start()
    child.join(timeout=5)
    backend = FilesystemLockBackend(tmp_path)
    assert backend.is_held("") is False


def test_registry_resolves_filesystem(tmp_path):
    """``get_lock_backend("filesystem")`` returns ``FilesystemLockBackend``."""
    cls = get_lock_backend("filesystem")
    assert cls is DirectFLB
    backend = cls(tmp_path)
    assert backend.backend_id == "filesystem"


def test_doctor_compatibility_empty_name_observable_via_raw_flock(tmp_path):
    """Pins the spec/21 promise that ``acquire("")`` produces ``<scope>/.lock``
    and that the lock is observable via the SAME ``fcntl.flock`` probe
    ``atomic-agents doctor``'s ``check_locks`` uses today.

    Why this is its own test: ``test_empty_name_maps_to_dot_lock`` asserts
    the on-disk path. This test asserts the OBSERVABLE behavior the diagnostic
    relies on. If a future refactor (PR 2 wiring) changes ``acquire("")`` to
    ``acquire("agent")`` "for clarity," doctor would silently lose its
    diagnostic (file now at ``.agent.lock``, doctor still probes ``.lock``)
    — this test fails fast against that mistake.
    """
    backend = FilesystemLockBackend(tmp_path)
    handle = backend.acquire("", timeout=0.0)
    try:
        doctor_path = tmp_path / ".lock"
        assert doctor_path.exists(), "acquire('') must produce <scope>/.lock"
        # Probe via raw flock from the same process. fcntl.flock is
        # per-open-file-description on BSD/macOS/Linux: the probe's fresh
        # fd attempting LOCK_EX | LOCK_NB MUST get EWOULDBLOCK because
        # the backend's fd holds the exclusive lock.
        probe_fd = os.open(str(doctor_path), os.O_RDWR)
        try:
            with pytest.raises((BlockingIOError, OSError)) as exc_info:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if isinstance(exc_info.value, OSError) and not isinstance(
                exc_info.value, BlockingIOError
            ):
                assert exc_info.value.errno == errno.EWOULDBLOCK
        finally:
            os.close(probe_fd)
    finally:
        backend.release(handle)


def test_handle_reuse_after_release_raises(tmp_path):
    """Regression for F1 (Step 11 adversarial): re-entering a released
    handle MUST raise rather than silently no-op into a phantom critical
    section. CLAUDE.md rule #8 — no half-finished state.
    """
    backend = FilesystemLockBackend(tmp_path)
    handle = backend.acquire("", timeout=0.0)
    backend.release(handle)
    # The lock is released; the handle's state is wiped. Re-entering
    # the SAME handle via `with` would have silently no-op'd before F1.
    with pytest.raises(RuntimeError, match="cannot be re-entered"):
        with handle:
            pass  # pragma: no cover — body must not execute

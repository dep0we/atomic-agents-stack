"""Integration tests pinning the #60 PR 2 lock-wiring contract.

These tests assert that the four production lock sites + `doctor.check_locks`
route through the LockBackend Protocol instead of the legacy `_locks.AgentLock`
class. Conformance + filesystem-shape tests live in
`tests/test_lock_protocol_conformance.py` and `tests/test_lock_filesystem_backend.py`;
this file pins the *wiring* — every site reaches the backend via the right
scope, name, and capability path, and the deprecation shim keeps the legacy
import path working.
"""

from __future__ import annotations

import multiprocessing
import os
import re
import time
import warnings
from pathlib import Path

import pytest

from atomic_agents.exceptions import (
    AgentLockBusy,
    AtomicAgentsError,
    DreamInProgress,
    LockBusy,
)
from atomic_agents.locks import (
    FilesystemLockBackend,
    LockBackend,
)


# ──────────────────────────────────────────────────────────────────
# Helpers


def _build_minimal_agent_dir(tmp_path: Path, name: str = "test") -> Path:
    """Construct the minimal on-disk shape AtomicAgent.__init__ requires."""
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n")
    (agent_dir / "tools.md").write_text(
        "## Write paths\n- memory/\n\n## Read-only paths\n(none)\n"
    )
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    return agent_dir


# ──────────────────────────────────────────────────────────────────
# Site 1: agent.py


def test_agent_has_public_lock_backend_attribute(tmp_path, monkeypatch):
    """AtomicAgent exposes ``self.lock_backend`` (public, mirrors ``self.memory``)."""
    from atomic_agents.agent import AtomicAgent

    agent_root = _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    agent = AtomicAgent(name="test")

    assert hasattr(agent, "lock_backend"), (
        "AtomicAgent must expose ``lock_backend`` as a public attribute"
    )
    assert isinstance(agent.lock_backend, LockBackend), (
        "agent.lock_backend must satisfy the LockBackend Protocol"
    )
    assert isinstance(agent.lock_backend, FilesystemLockBackend), (
        "default agent.lock_backend is FilesystemLockBackend per spec/21"
    )


def test_agent_lock_backend_scoped_to_agent_root(tmp_path, monkeypatch):
    """``acquire("")`` produces ``<agent_root>/.lock`` — the legacy artifact."""
    from atomic_agents.agent import AtomicAgent

    agent_root = _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    agent = AtomicAgent(name="test")
    handle = agent.lock_backend.acquire("", timeout=0)
    try:
        assert (agent_root / ".lock").exists()
    finally:
        agent.lock_backend.release(handle)


def test_agent_call_propagates_non_lockbusy_acquire_failure(tmp_path, monkeypatch):
    """Non-LockBusy exceptions from acquire() (e.g., PermissionError on the
    lock file) propagate out of ``agent.call()`` without a finally-block
    NameError on ``lock_handle``. Pins the control-flow contract that
    testing specialist CRITICAL #4 flagged as untested.
    """
    from unittest.mock import patch as _patch

    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    agent = AtomicAgent(name="test")
    # Replace acquire with a side-effect that raises a NON-LockBusy
    # exception. The legacy AgentLock could only raise AgentLockBusy or
    # re-raise raw OSError; the new FilesystemLockBackend may raise
    # PermissionError (or any OSError) before entering the flock loop
    # (e.g., on a read-only filesystem, on a directory where the
    # operator lacks permission to create the .lock file).
    with _patch.object(
        agent.lock_backend, "acquire", side_effect=PermissionError("simulated EACCES")
    ):
        with pytest.raises(PermissionError):
            agent.call("test work item")
    # No NameError reached the test runner; the propagation path is clean.


def test_agent_lock_backend_capabilities_filesystem_default(tmp_path, monkeypatch):
    """Default agent.lock_backend advertises single_host_only=True (PR 3 negates)."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    agent = AtomicAgent(name="test")
    caps = agent.lock_backend.capabilities()
    assert caps.single_host_only is True
    assert caps.supports_reentrancy is False
    assert caps.supports_lease is False


# ──────────────────────────────────────────────────────────────────
# Site 2: dream.py


def test_dream_lock_path_is_dreams_dir_dot_lock(tmp_path, monkeypatch):
    """Dream lock is at ``<dreams_dir>/.lock`` — byte-identical to legacy _DreamLock."""
    from atomic_agents.dream import DreamRunner

    agent_root = _build_minimal_agent_dir(tmp_path, "dreamer")
    (agent_root / "dreams").mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    runner = DreamRunner(tmp_path, "dreamer")
    handle = runner._dream_lock_backend.acquire("", timeout=0)
    try:
        assert (agent_root / "dreams" / ".lock").exists()
        # And the agent's .lock should NOT exist (different scope).
        assert not (agent_root / ".lock").exists()
    finally:
        runner._dream_lock_backend.release(handle)


def _hold_dream_lock_in_child(
    dreams_dir_str: str, hold_seconds: float, ready_path: str
) -> None:
    """Subprocess entry point: hold the dream lock at <dreams_dir>/.lock."""
    backend = FilesystemLockBackend(Path(dreams_dir_str))
    handle = backend.acquire("", timeout=0)
    Path(ready_path).write_text("ready")
    time.sleep(hold_seconds)
    backend.release(handle)


def test_dream_start_wraps_lockbusy_with_chaining_at_callsite(tmp_path, monkeypatch):
    """``DreamRunner.start()`` (the production callsite) wraps LockBusy → DreamInProgress.

    Exercises the ACTUAL ``dream.py`` call site, not an inline
    reimplementation of the wrap. If a future refactor drops the
    ``from exc`` chaining, removes the ``except LockBusy`` block, or
    re-shapes the exception message, this test fails — testing
    specialist CRITICAL caught the prior test pattern as a green-on-
    inline-copy false-positive.
    """
    from unittest.mock import patch as _patch

    from atomic_agents.dream import DreamRunner

    agent_root = _build_minimal_agent_dir(tmp_path, "dreamer")
    dreams_dir = agent_root / "dreams"
    dreams_dir.mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    # Hold the dream lock in a child process so the same-process
    # non-reentrancy check on the parent's backend instance is bypassed
    # (we want the kernel-level flock to be the one that raises).
    ready = tmp_path / ".dream-ready"
    child = multiprocessing.Process(
        target=_hold_dream_lock_in_child,
        args=(str(dreams_dir), 2.0, str(ready)),
    )
    child.start()
    try:
        deadline = time.monotonic() + 10.0
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("child never wrote sentinel")
            time.sleep(0.02)
        # Stub the LLM so start() doesn't try to make a real call before
        # reaching the lock acquisition; the lock acquire happens early
        # in start() so the stub is only defensive.
        with _patch("atomic_agents.dream._llm.call_llm"):
            # dream_lock_timeout=0 forces fail-fast against the held lock
            # (default is 30s — too long for the test's 2s child hold).
            runner = DreamRunner(tmp_path, "dreamer", dream_lock_timeout=0.0)
            with pytest.raises(DreamInProgress) as exc_info:
                runner.start()
        # PEP-3134 chaining: __cause__ MUST be the underlying LockBusy.
        assert isinstance(exc_info.value.__cause__, LockBusy), (
            f"DreamInProgress.__cause__ should be LockBusy, "
            f"got {type(exc_info.value.__cause__).__name__}"
        )
    finally:
        child.join(timeout=5)
        assert child.exitcode == 0, f"child crashed with exitcode {child.exitcode}"


# ──────────────────────────────────────────────────────────────────
# Site 3: memory/filesystem.py


def test_filesystem_backend_lock_backend_scoped_to_agent_root(tmp_path):
    """FilesystemBackend caches a LockBackend scoped to agent_root for apply_staging."""
    from atomic_agents.memory.filesystem import FilesystemBackend

    agent_root = _build_minimal_agent_dir(tmp_path)
    backend = FilesystemBackend(agent_root)

    assert isinstance(backend._lock_backend, LockBackend)
    handle = backend._lock_backend.acquire("", timeout=0)
    try:
        # Same scope as agent.lock_backend would produce → SAME .lock file.
        assert (agent_root / ".lock").exists()
    finally:
        backend._lock_backend.release(handle)


# ──────────────────────────────────────────────────────────────────
# Site 4: agent.call() ↔ apply_staging serialize on same .lock


def _hold_agent_lock_in_child(
    agent_root_str: str, hold_seconds: float, ready_path: str
) -> None:
    """Subprocess entry point: simulate an in-flight agent.call() lock."""
    backend = FilesystemLockBackend(Path(agent_root_str))
    handle = backend.acquire("", timeout=0)
    Path(ready_path).write_text("ready")
    time.sleep(hold_seconds)
    backend.release(handle)


def test_apply_staging_blocks_on_agent_call_lock(tmp_path):
    """Same scope_root + same empty-name → kernel flock serializes the two paths."""
    from atomic_agents.memory.filesystem import FilesystemBackend

    agent_root = _build_minimal_agent_dir(tmp_path)
    backend = FilesystemBackend(agent_root)

    ready = tmp_path / ".ready"
    child = multiprocessing.Process(
        target=_hold_agent_lock_in_child,
        args=(str(agent_root), 1.0, str(ready)),
    )
    child.start()
    try:
        deadline = time.monotonic() + 3.0
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("child never wrote sentinel")
            time.sleep(0.02)
        # Child holds .lock. backend's own acquire (timeout=0) MUST raise LockBusy.
        with pytest.raises(LockBusy):
            backend._lock_backend.acquire("", timeout=0)
    finally:
        child.join(timeout=5)


# ──────────────────────────────────────────────────────────────────
# Site 5: doctor.check_locks routes through backend


def test_doctor_check_locks_routes_through_is_held(tmp_path):
    """doctor.check_locks reports FAIL while another backend holds .lock."""
    from atomic_agents.doctor import check_locks

    agent_root = _build_minimal_agent_dir(tmp_path)

    # No lock yet → PASS (no-file branch)
    result = check_locks(agent_root)
    assert result.status == "pass"

    # Hold the lock from another process
    ready = tmp_path / ".ready"
    child = multiprocessing.Process(
        target=_hold_agent_lock_in_child,
        args=(str(agent_root), 1.0, str(ready)),
    )
    child.start()
    try:
        deadline = time.monotonic() + 3.0
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("child never wrote sentinel")
            time.sleep(0.02)
        result = check_locks(agent_root)
        assert result.status == "fail"
        assert "is held" in result.message
        # Doctor still reads <agent>/.lock for the PID diagnostic.
        assert "pid=" in result.message
    finally:
        child.join(timeout=5)

    # After child exits, kernel releases flock → doctor sees PASS.
    result = check_locks(agent_root)
    assert result.status == "pass"


# ──────────────────────────────────────────────────────────────────
# Independence: agent.call() and dream.start() share an agent but
# different scope roots → don't block each other.


def test_agent_lock_and_dream_lock_are_independent(tmp_path, monkeypatch):
    """Holding the dream lock MUST NOT block the agent's main lock."""
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.dream import DreamRunner

    agent_root = _build_minimal_agent_dir(tmp_path)
    (agent_root / "dreams").mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    agent = AtomicAgent(name="test")
    runner = DreamRunner(tmp_path, "test")

    # Hold the dream lock
    dream_handle = runner._dream_lock_backend.acquire("", timeout=0)
    try:
        # Agent lock should grant immediately (different scope_root).
        agent_handle = agent.lock_backend.acquire("", timeout=0)
        agent.lock_backend.release(agent_handle)
    finally:
        runner._dream_lock_backend.release(dream_handle)


# ──────────────────────────────────────────────────────────────────
# Deprecation shim: _locks.AgentLock still works but emits warning


def test_legacy_agentlock_emits_deprecation_warning(tmp_path):
    """`from atomic_agents._locks import AgentLock; AgentLock(...)` warns."""
    from atomic_agents._locks import AgentLock

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        lock = AgentLock(tmp_path, wait_seconds=0)
    dep_warns = [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert len(dep_warns) == 1
    assert "FilesystemLockBackend" in str(dep_warns[0].message)
    assert "v1.1" in str(dep_warns[0].message)
    # Lock is constructed but not yet acquired; releasing without acquire is safe
    lock.release()


def test_legacy_agentlock_writes_same_format_as_new_backend(tmp_path):
    """Backward-compat sentinel (#60 PR 1 Step 11 P1-6).

    The legacy shim and the new backend MUST write the same on-disk
    payload shape so external operator scripts that pinned the
    ``pid=<pid> acquired=<ts>`` format keep working.
    """
    from atomic_agents._locks import AgentLock

    legacy_root = tmp_path / "legacy"
    new_root = tmp_path / "new"

    # Suppress the deprecation warning for the legacy path
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with AgentLock(legacy_root):
            legacy_content = (legacy_root / ".lock").read_text()

    backend = FilesystemLockBackend(new_root)
    with backend.acquire("") as _:
        new_content = (new_root / ".lock").read_text()

    legacy_match = re.match(r"^pid=\d+ acquired=[\d.]+\n$", legacy_content)
    new_match = re.match(r"^pid=\d+ acquired=[\d.]+\n$", new_content)
    assert legacy_match is not None, f"legacy: {legacy_content!r}"
    assert new_match is not None, f"new: {new_content!r}"


def test_legacy_acquire_function_still_works_with_warning(tmp_path):
    """Module-level ``acquire()`` re-export is preserved per CLAUDE.md rule #14."""
    from atomic_agents import _locks as legacy

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with legacy.acquire(tmp_path, wait_seconds=0) as lock:
            assert (tmp_path / ".lock").exists()
            assert lock._fd is not None
    dep_warns = [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    # Multiple warnings expected — one for acquire(), one for AgentLock().
    assert len(dep_warns) >= 1
    assert any("FilesystemLockBackend" in str(w.message) for w in dep_warns)


def test_legacy_agentlock_busy_raises_legacy_exception_name(tmp_path):
    """``AgentLockBusy`` is still the catch-type when the shim raises.

    The exception class is now ``LockBusy`` aliased as ``AgentLockBusy``
    (PR 1). This test pins that ``isinstance(e, AgentLockBusy)`` still
    catches the new exception so ``except AgentLockBusy:`` code paths
    in operator runbooks keep working unchanged.
    """
    from atomic_agents._locks import AgentLock

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        first = AgentLock(tmp_path, wait_seconds=0)
        first.acquire()
        try:
            with pytest.raises(AgentLockBusy):
                second = AgentLock(tmp_path, wait_seconds=0)
                second.acquire()
        finally:
            first.release()


# ──────────────────────────────────────────────────────────────────
# Exception identity (re-pins #60 PR 1's identity assertion under PR 2 wiring)


def test_lockbusy_is_agentlockbusy_under_wiring(tmp_path):
    """After PR 2 wires the backend, ``AgentLockBusy is LockBusy`` still holds."""
    assert AgentLockBusy is LockBusy
    # AtomicAgentsError remains the common ancestor.
    assert issubclass(LockBusy, AtomicAgentsError)

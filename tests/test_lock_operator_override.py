"""Operator override surface for the LockBackend (#60 PR 3).

Covers:
- `ATOMIC_AGENTS_LOCK_BACKEND` env var threading through
  `get_default_lock_backend` to the three call sites (AtomicAgent,
  DreamRunner, FilesystemBackend).
- `lock_backend=...` constructor kwarg as the programmatic override
  (bypasses env var).
- `doctor.check_lock_backend` operator-config coherence check.
- `doctor.check_locks` graceful WARN-on-unreachable-Redis pattern.

Fakeredis is used for the Redis path. Skip the Redis-specific tests if
the dev dep isn't installed.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

fakeredis = pytest.importorskip("fakeredis")

from atomic_agents.exceptions import BackendNotRegistered
from atomic_agents.locks import (
    FilesystemLockBackend,
    LockBackend,
    get_default_lock_backend,
)
from atomic_agents.locks.redis import RedisLockBackend


# ──────────────────────────────────────────────────────────────────
# Helpers


def _build_minimal_agent_dir(tmp_path: Path, name: str = "test") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n")
    (agent_dir / "tools.md").write_text("## Write paths\n- memory/\n")
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (agent_dir / "memory").mkdir()
    return agent_dir


# ──────────────────────────────────────────────────────────────────
# get_default_lock_backend factory


def test_get_default_returns_filesystem_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_LOCK_BACKEND", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_LOCK_BACKEND_URL", raising=False)
    backend = get_default_lock_backend(tmp_path)
    assert isinstance(backend, FilesystemLockBackend)


def test_get_default_returns_filesystem_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "filesystem")
    backend = get_default_lock_backend(tmp_path)
    assert isinstance(backend, FilesystemLockBackend)


def test_get_default_unknown_backend_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "nonexistent-typo")
    with pytest.raises(BackendNotRegistered, match="nonexistent-typo"):
        get_default_lock_backend(tmp_path)


def test_get_default_redis_requires_url(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "redis")
    monkeypatch.delenv("ATOMIC_AGENTS_LOCK_BACKEND_URL", raising=False)
    with pytest.raises(ValueError, match="ATOMIC_AGENTS_LOCK_BACKEND_URL"):
        get_default_lock_backend(tmp_path)


# ──────────────────────────────────────────────────────────────────
# Constructor kwarg override (programmatic operator path)


def test_atomic_agent_accepts_lock_backend_kwarg(tmp_path, monkeypatch):
    """AtomicAgent(..., lock_backend=...) bypasses env-var resolution."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    # Set env var to a value that WOULD fail if resolved (no URL)
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "redis")
    monkeypatch.delenv("ATOMIC_AGENTS_LOCK_BACKEND_URL", raising=False)

    # But pass an explicit backend — env var is bypassed.
    client = fakeredis.FakeRedis(decode_responses=True)
    redis_backend = RedisLockBackend(
        client, key_prefix="explicit:", lease_ttl_seconds=10.0,
        heartbeat_interval_seconds=5.0,
    )
    agent = AtomicAgent(name="test", lock_backend=redis_backend)
    assert agent.lock_backend is redis_backend


def test_dream_runner_accepts_lock_backend_kwarg(tmp_path, monkeypatch):
    """DreamRunner(..., lock_backend=...) re-scopes to "dreams"."""
    from atomic_agents.dream import DreamRunner

    agent_dir = _build_minimal_agent_dir(tmp_path, "dreamer")
    (agent_dir / "dreams").mkdir()

    # Pass a filesystem backend explicitly — dream rescopes via scope("dreams").
    fs_backend = FilesystemLockBackend(agent_dir)
    runner = DreamRunner(tmp_path, "dreamer", lock_backend=fs_backend)

    # The dream-lock backend MUST be scoped to <agent>/dreams, NOT
    # <agent_root> — distinct scope so dream doesn't block agent.call().
    handle = runner._dream_lock_backend.acquire("", timeout=0)
    try:
        # Filesystem scope() produces a new backend rooted at the sub-path.
        assert (agent_dir / "dreams" / ".lock").exists()
        # And the parent agent's main .lock is NOT created (different scope).
        assert not (agent_dir / ".lock").exists()
    finally:
        runner._dream_lock_backend.release(handle)


def test_filesystem_backend_accepts_lock_backend_kwarg(tmp_path):
    """FilesystemBackend(..., lock_backend=...) bypasses env-var resolution."""
    from atomic_agents.memory.filesystem import FilesystemBackend

    agent_dir = _build_minimal_agent_dir(tmp_path)
    # Pass a sentinel backend
    sentinel_backend = MagicMock(spec=LockBackend)
    backend = FilesystemBackend(agent_dir, lock_backend=sentinel_backend)
    assert backend._lock_backend is sentinel_backend


# ──────────────────────────────────────────────────────────────────
# Env-var threading through AtomicAgent (deployment-path operator config)


def test_atomic_agent_resolves_env_var_redis(tmp_path, monkeypatch):
    """ATOMIC_AGENTS_LOCK_BACKEND=redis + URL → agent uses Redis backend."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "redis")
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND_URL", "redis://localhost:6379/0")

    # Patch redis.Redis.from_url to return a fakeredis client so we
    # don't need a real Redis server.
    client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.Redis.from_url", return_value=client):
        agent = AtomicAgent(name="test")

    assert isinstance(agent.lock_backend, RedisLockBackend)
    assert agent.lock_backend.capabilities().single_host_only is False


# ──────────────────────────────────────────────────────────────────
# doctor.check_lock_backend


def test_doctor_check_lock_backend_pass_filesystem_default(tmp_path, monkeypatch):
    from atomic_agents.doctor import check_lock_backend, PASS

    monkeypatch.delenv("ATOMIC_AGENTS_LOCK_BACKEND", raising=False)
    result = check_lock_backend(tmp_path)
    assert result.status == PASS
    assert "filesystem" in result.message


def test_doctor_check_lock_backend_fail_unknown_id(tmp_path, monkeypatch):
    from atomic_agents.doctor import check_lock_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "spamspam")
    result = check_lock_backend(tmp_path)
    assert result.status == FAIL
    assert "spamspam" in result.message
    assert "not a known backend" in result.message.lower()


def test_doctor_check_lock_backend_fail_redis_without_url(tmp_path, monkeypatch):
    from atomic_agents.doctor import check_lock_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "redis")
    monkeypatch.delenv("ATOMIC_AGENTS_LOCK_BACKEND_URL", raising=False)
    result = check_lock_backend(tmp_path)
    assert result.status == FAIL
    assert "ATOMIC_AGENTS_LOCK_BACKEND_URL" in result.message


def test_doctor_check_lock_backend_warn_on_unreachable_redis(tmp_path, monkeypatch):
    """Operator-pinned redis URL unreachable → WARN (not FAIL)."""
    from atomic_agents.doctor import check_lock_backend, WARN

    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "redis")
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND_URL", "redis://localhost:6379/0")

    # Mock redis.Redis.from_url to return a client whose ``get`` raises
    # (simulates "URL configured but Redis server unreachable"). The
    # RedisLockBackend constructor stores the client reference WITHOUT
    # any network call; is_held("") is the first probe and calls
    # ``client.get(key)`` which trips the side_effect → WARN path.
    failing_client = MagicMock()
    failing_client.get.side_effect = ConnectionError("simulated unreachable")
    # Belt-and-suspenders: any unintended network call (.set, .eval,
    # .delete) should also raise rather than returning a MagicMock
    # truthy sentinel that could mask a regression in the probe path.
    failing_client.set.side_effect = ConnectionError("simulated unreachable")
    failing_client.eval.side_effect = ConnectionError("simulated unreachable")
    with patch("redis.Redis.from_url", return_value=failing_client):
        result = check_lock_backend(tmp_path)

    assert result.status == WARN
    assert "not reachable" in result.message.lower()


# ──────────────────────────────────────────────────────────────────
# doctor.check_locks (held-state probe routing)


def test_doctor_check_locks_uses_operator_backend(tmp_path, monkeypatch):
    """check_locks honors ATOMIC_AGENTS_LOCK_BACKEND=redis + URL."""
    from atomic_agents.doctor import check_locks, PASS

    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND", "redis")
    monkeypatch.setenv("ATOMIC_AGENTS_LOCK_BACKEND_URL", "redis://localhost:6379/0")

    client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.Redis.from_url", return_value=client):
        # No lock held — should be PASS.
        result = check_locks(tmp_path)
    # For non-filesystem backends, the early "no lock file" path doesn't
    # apply — the check goes through is_held, which on a fresh fakeredis
    # returns False → PASS.
    assert result.status == PASS

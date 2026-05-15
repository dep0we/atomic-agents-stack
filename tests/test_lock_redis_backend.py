"""Tests for the RedisLockBackend reference impl (#60 PR 3).

Uses ``fakeredis`` to simulate Redis in-process. The same suite passes
against a real ``redis.Redis`` client when run with a live Redis
instance — production deployments should run the conformance suite
against their own Redis as a smoke test before flipping the operator
config.

Covers:
- Protocol surface (isinstance, backend_id, capabilities)
- acquire/release/renew/is_held semantics
- Lease TTL + heartbeat thread renewal
- Lock-loss detection via LockLost
- scope() rescoping (key_prefix concatenation)
- Edge cases: invalid TTL, missing redis extra (import-time error)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

# Skip the entire module if fakeredis is not installed — same pattern as
# test_llm_anthropic_backend.py's optional-SDK detection.
fakeredis = pytest.importorskip("fakeredis")

from atomic_agents.exceptions import LockBusy, LockLost
from atomic_agents.locks import LockBackend, LockCapabilities, LockHandle, check_lock_lost
from atomic_agents.locks.redis import (
    RedisLockBackend,
    make_redis_backend_from_url,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def redis_client():
    """A fresh fakeredis client per test."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def backend(redis_client):
    """Backend with short TTL + heartbeat interval for fast tests."""
    return RedisLockBackend(
        redis_client,
        key_prefix="test:lock:",
        lease_ttl_seconds=2.0,
        heartbeat_interval_seconds=0.5,
        poll_interval_seconds=0.02,
    )


# ──────────────────────────────────────────────────────────────────
# Surface conformance


def test_protocol_isinstance(backend):
    assert isinstance(backend, LockBackend)


def test_backend_id_is_redis(backend):
    assert backend.backend_id == "redis"


def test_capabilities_advertised_correctly(backend):
    caps = backend.capabilities()
    assert isinstance(caps, LockCapabilities)
    # Redis is the canonical distributed shape — not single-host.
    assert caps.single_host_only is False
    # Non-reentrant for the same reasoning as filesystem (simpler audit).
    assert caps.supports_reentrancy is False
    # Lease-backed — heartbeat thread renews on cadence.
    assert caps.supports_lease is True


def test_acquire_returns_real_handle(backend):
    handle = backend.acquire("", timeout=0.0)
    try:
        assert isinstance(handle, LockHandle)
        assert handle.name == ""
        assert handle.acquired_at > 0
    finally:
        backend.release(handle)


# ──────────────────────────────────────────────────────────────────
# acquire / release / is_held


def test_second_acquire_same_process_raises_lockbusy(backend):
    """Non-reentrant — second acquire from same process MUST raise."""
    handle = backend.acquire("", timeout=0.0)
    try:
        with pytest.raises(LockBusy):
            backend.acquire("", timeout=0.0)
    finally:
        backend.release(handle)


def test_release_is_idempotent(backend):
    handle = backend.acquire("", timeout=0.0)
    backend.release(handle)
    backend.release(handle)  # second call is no-op


def test_distinct_names_dont_conflict(backend):
    h1 = backend.acquire("agent", timeout=0.0)
    try:
        h2 = backend.acquire("dream", timeout=0.0)
        backend.release(h2)
    finally:
        backend.release(h1)


def test_is_held_reflects_state(backend):
    assert backend.is_held("") is False
    handle = backend.acquire("", timeout=0.0)
    try:
        assert backend.is_held("") is True
    finally:
        backend.release(handle)
    assert backend.is_held("") is False


def test_release_uses_atomic_lua_script_does_not_clobber_successor(redis_client):
    """A stale release MUST NOT delete a successor holder's lock.

    Scenario: holder A acquires, then A's release runs AFTER A's lease
    expired and holder B already acquired. Without the Lua-script
    token check, A's release would DEL B's key. With the check, it's a
    no-op.

    Test shape note (Step 9.1 testing specialist CRITICAL #2): this is
    a SEQUENTIAL exercise of the GET-then-DEL code path inside the Lua
    script, not a concurrent test of the SET-vs-DEL race window. The
    actual atomicity guarantee (no interleaving of GET-by-A and
    SET-by-B such that A reads B's value and DELs it) is a property of
    Redis's single-threaded Lua executor and can only be verified
    against real Redis. Operators run conformance against real Redis
    as a deployment smoke test (documented in spec/21). fakeredis's
    Lua executor matches real-Redis semantics for this script shape
    (single command, no MULTI/EXEC) so the sequential test is
    sufficient for the Protocol contract.
    """
    backend_a = RedisLockBackend(
        redis_client,
        key_prefix="test:lock:",
        lease_ttl_seconds=10.0,
        heartbeat_interval_seconds=5.0,  # won't fire during the test
    )
    backend_b = RedisLockBackend(
        redis_client,
        key_prefix="test:lock:",
        lease_ttl_seconds=10.0,
        heartbeat_interval_seconds=5.0,
    )

    handle_a = backend_a.acquire("", timeout=0.0)
    # Manually expire A's lease + acquire B (simulates TTL expiry)
    redis_client.delete("test:lock:__main__")
    handle_b = backend_b.acquire("", timeout=0.0)
    try:
        # A's release should NOT delete B's key (token mismatch).
        backend_a.release(handle_a)
        assert backend_b.is_held("") is True
    finally:
        backend_b.release(handle_b)


# ──────────────────────────────────────────────────────────────────
# Heartbeat / lease semantics


def test_renew_returns_true_for_held_lock(backend):
    handle = backend.acquire("", timeout=0.0)
    try:
        assert backend.renew(handle) is True
    finally:
        backend.release(handle)


def test_renew_returns_false_for_already_released(backend):
    handle = backend.acquire("", timeout=0.0)
    backend.release(handle)
    # After release, the key is gone — renew can't extend a missing key.
    assert backend.renew(handle) is False


def test_heartbeat_thread_renews_lease_automatically(redis_client):
    """Lease shorter than wall time, heartbeat must keep it alive.

    TTL=5s, heartbeat=0.5s, sleep=3s — gives 6x margin over the
    heartbeat interval so a single delayed heartbeat (GIL contention,
    OS scheduler) doesn't cause the lease to expire. The earlier
    TTL=2s + heartbeat=0.5s + sleep=2.5s shape (Step 9.1 testing
    specialist CRITICAL #1) had zero margin — one missed heartbeat at
    t=2.0s would let the lease expire before the next renew at t=2.5s.
    """
    backend = RedisLockBackend(
        redis_client,
        key_prefix="test:lock:",
        lease_ttl_seconds=5.0,
        heartbeat_interval_seconds=0.5,
    )
    handle = backend.acquire("", timeout=0.0)
    try:
        time.sleep(3.0)
        # Heartbeat should have renewed ~6 times by now (every 0.5s).
        assert backend.is_held("") is True
        assert backend.renew(handle) is True
    finally:
        backend.release(handle)


def test_heartbeat_detects_redis_unreachable_as_lock_lost(redis_client):
    """If Redis becomes unreachable mid-heartbeat (network blip, server
    bounce), the heartbeat MUST capture the failure as ``LockLost`` —
    Step 11 adversarial P1-1. Without this coverage, the
    ``except Exception`` branch in ``_heartbeat_loop`` is untested,
    and a bug in the exception capture would only surface in real
    network failures.
    """
    from unittest.mock import MagicMock

    # Spy on eval — first call (acquire-time SETNX is via .set, not
    # eval, so this is the first heartbeat) raises ConnectionError.
    failing_client = MagicMock(wraps=redis_client)
    failing_client.eval.side_effect = ConnectionError("simulated Redis down")

    backend = RedisLockBackend(
        failing_client,
        key_prefix="test:lock:",
        lease_ttl_seconds=10.0,
        heartbeat_interval_seconds=0.1,  # tight so the failure fires fast
    )
    handle = backend.acquire("", timeout=0.0)
    try:
        time.sleep(0.5)  # 5x heartbeat interval — plenty of margin
        with pytest.raises(LockLost, match="Redis unreachable"):
            check_lock_lost(handle)
        # Heartbeat thread should have exited cleanly after capturing
        # the failure (the except branch returns).
        assert not handle.backend_state.heartbeat_thread.is_alive()
    finally:
        backend.release(handle)


def test_heartbeat_detects_lease_lost_when_key_externally_deleted(redis_client):
    """If something (e.g., expiry, redis-cli DEL) removes the key,
    the heartbeat must surface LockLost on the handle."""
    backend = RedisLockBackend(
        redis_client,
        key_prefix="test:lock:",
        lease_ttl_seconds=10.0,
        heartbeat_interval_seconds=0.1,  # tight loop for the test
    )
    handle = backend.acquire("", timeout=0.0)
    try:
        # Externally delete the key (simulates TTL expiry under load)
        redis_client.delete("test:lock:__main__")
        # Give the heartbeat thread a chance to detect the loss
        time.sleep(0.5)
        with pytest.raises(LockLost):
            check_lock_lost(handle)
    finally:
        backend.release(handle)


def test_check_lock_lost_no_op_for_filesystem_handle(tmp_path):
    """check_lock_lost on a filesystem-backed handle is a no-op.

    Filesystem backend has no heartbeat thread; ``backend_state`` is an
    int fd, not a _RedisHandleState. check_lock_lost must not raise.
    """
    from atomic_agents.locks import FilesystemLockBackend

    fs_backend = FilesystemLockBackend(tmp_path)
    handle = fs_backend.acquire("", timeout=0)
    try:
        check_lock_lost(handle)  # MUST NOT raise
    finally:
        fs_backend.release(handle)


def test_heartbeat_thread_cleaned_up_on_release(backend):
    """release() must signal the heartbeat thread to exit and join it."""
    handle = backend.acquire("", timeout=0.0)
    state = handle.backend_state
    thread = state.heartbeat_thread
    assert thread.is_alive()
    backend.release(handle)
    thread.join(timeout=2)
    assert not thread.is_alive()


# ──────────────────────────────────────────────────────────────────
# scope() Protocol method


def test_scope_returns_new_backend_with_extended_prefix(backend):
    sub = backend.scope("dreams")
    assert isinstance(sub, RedisLockBackend)
    assert sub.backend_id == "redis"
    # Capabilities preserved
    assert sub.capabilities() == backend.capabilities()


def test_scope_isolates_locks_from_parent(backend):
    """parent.acquire("") and parent.scope("x").acquire("") MUST NOT collide."""
    sub = backend.scope("dreams")
    parent_handle = backend.acquire("", timeout=0.0)
    try:
        # Acquiring the sub-scope's empty-name lock is independent —
        # different Redis key.
        sub_handle = sub.acquire("", timeout=0.0)
        sub.release(sub_handle)
    finally:
        backend.release(parent_handle)


def test_scope_empty_path_raises(backend):
    with pytest.raises(ValueError, match="non-empty"):
        backend.scope("")


# ──────────────────────────────────────────────────────────────────
# Construction validation


def test_init_rejects_ttl_smaller_than_heartbeat(redis_client):
    """Lease TTL must be > heartbeat interval (otherwise lease expires
    before the first renewal lands)."""
    with pytest.raises(ValueError, match="must be >"):
        RedisLockBackend(
            redis_client,
            lease_ttl_seconds=10.0,
            heartbeat_interval_seconds=15.0,
        )


def test_make_redis_backend_from_url_rejects_non_redis_scheme():
    with pytest.raises(ValueError, match="must start with redis"):
        make_redis_backend_from_url("http://example.com")


# ──────────────────────────────────────────────────────────────────
# Context-manager


def test_handle_context_manager_releases(backend):
    with backend.acquire("", timeout=0.0):
        assert backend.is_held("") is True
    # After exit, lock should be released.
    assert backend.is_held("") is False


def test_handle_context_manager_releases_on_exception(backend):
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with backend.acquire("", timeout=0.0):
            raise _Boom()
    assert backend.is_held("") is False

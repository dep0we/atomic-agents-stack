"""Conformance test suite for the LockBackend Protocol (spec/21).

Parameterized over a ``backend_factory`` fixture. Each registered backend
that ships in core (``FilesystemLockBackend`` today; PR 3 of #60 adds a
distributed reference impl) is exercised against the same contract. A
third-party backend in a downstream package imports this test module's
``backend_factory`` parametrization to verify its own conformance.

What this suite asserts:

1. Protocol surface — ``isinstance(backend, LockBackend)`` passes; all
   six required attributes/methods are present.
2. ``backend_id`` is a stable non-empty string.
3. ``acquire("")`` returns a real ``LockHandle`` populated honestly.
4. ``acquire`` is the context manager — release happens on ``__exit__``.
5. ``release(handle)`` is idempotent — double-release is a no-op.
6. Second ``acquire`` of same name from same process raises
   ``LockBusy`` unless the backend claims ``supports_reentrancy=True``.
7. Reentrancy claim parity — claim matches behavior.
8. Different ``name`` arguments don't conflict with each other.
9. ``timeout=0`` fails fast against a held lock (no spurious wait).
10. ``timeout>0`` returns within the deadline when no lock is granted.
11. ``timeout>0`` succeeds when the holder releases before the deadline.
12. ``is_held`` reflects reality at the moment of check.
13. ``is_held(name)`` for a never-acquired name returns False.
14. ``renew(handle)`` returns True for the no-lease default.
15. Lease claim parity — non-lease backends return True from ``renew``.
16. ``capabilities()`` returns a real ``LockCapabilities`` instance.
17. Concurrent acquires from different processes serialize correctly.
18. Context-manager release happens on exception inside the ``with``.
19. ``LockBusy`` is a subclass of ``AtomicAgentsError``.
20. ``AgentLockBusy`` alias has identical class identity to ``LockBusy``.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path
from typing import Callable

import pytest

from atomic_agents.exceptions import AgentLockBusy, AtomicAgentsError, LockBusy
from atomic_agents.locks import (
    FilesystemLockBackend,
    LockBackend,
    LockCapabilities,
    LockHandle,
)


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization
#
# PR 3 of #60 adds a second factory (Redis or tested mock) to this list.
# Until then, only filesystem participates — but the parametrization
# scaffolding is already in place so PR 3's wiring is a one-line edit.

BackendFactory = Callable[[Path], LockBackend]


def _filesystem_factory(scope_root: Path) -> LockBackend:
    return FilesystemLockBackend(scope_root, poll_interval=0.05)


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
]


# Redis backend joins the conformance suite via fakeredis. Skipped at
# import time when fakeredis is not installed — matches the optional-
# SDK detection pattern from test_llm_protocol_conformance.py.
try:
    import fakeredis as _fakeredis_for_conformance

    from atomic_agents.locks.redis import RedisLockBackend as _RedisLockBackend

    def _redis_factory(scope_root: Path) -> LockBackend:
        """Per-test fresh fakeredis instance + tight TTL/heartbeat for fast tests.

        Isolation between tests comes from the FRESH ``FakeRedis()``
        instance (in-process, per-test pytest fixture teardown — no
        cross-test state), NOT from the key_prefix. The earlier
        comment claimed key_prefix isolation but that's misleading:
        each ``FakeRedis()`` is its own independent store; there's no
        shared state between fixtures to namespace. The scope_root.name
        in the key prefix is just diagnostic — makes ``KEYS`` output
        readable when debugging. (Step 9.1 testing specialist
        CRITICAL #5.)
        """
        client = _fakeredis_for_conformance.FakeRedis(decode_responses=True)
        return _RedisLockBackend(
            client,
            key_prefix=f"conformance:{scope_root.name}:lock:",
            lease_ttl_seconds=5.0,
            heartbeat_interval_seconds=1.0,
            poll_interval_seconds=0.02,
        )

    BACKEND_FACTORIES.append(("redis", _redis_factory))
except ImportError:
    pass  # fakeredis not installed — Redis backend skipped


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend_factory(request) -> BackendFactory:
    """Yields a callable ``(scope_root: Path) -> LockBackend``."""
    return request.param[1]


@pytest.fixture
def backend(backend_factory, tmp_path) -> LockBackend:
    """A backend rooted at a per-test tmp_path."""
    return backend_factory(tmp_path)


# ──────────────────────────────────────────────────────────────────
# Helpers for cross-process tests

def _hold_lock_in_child(
    backend_module: str,
    backend_attr: str,
    scope_root_str: str,
    name: str,
    hold_seconds: float,
    ready_path: str,
) -> None:
    """Subprocess entry point: acquire ``name`` and sleep, then release.

    The child writes a sentinel file to ``ready_path`` after grant so
    the parent knows the lock is actually held before testing contention.
    """
    import importlib
    from pathlib import Path

    mod = importlib.import_module(backend_module)
    cls = getattr(mod, backend_attr)
    backend = cls(Path(scope_root_str))
    handle = backend.acquire(name=name, timeout=0.0)
    Path(ready_path).write_text("ready")
    time.sleep(hold_seconds)
    backend.release(handle)


def _wait_for_ready(ready_path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"child never wrote {ready_path}")


# ──────────────────────────────────────────────────────────────────
# Surface conformance


def test_protocol_isinstance(backend):
    """isinstance check passes — backend exposes the full Protocol."""
    assert isinstance(backend, LockBackend)


def test_backend_id_is_stable_nonempty_string(backend):
    backend_id = backend.backend_id
    assert isinstance(backend_id, str)
    assert backend_id != ""
    # Stable: the id returns the same value across reads.
    assert backend.backend_id == backend_id


def test_acquire_returns_real_handle(backend):
    handle = backend.acquire("", timeout=0.0)
    try:
        assert isinstance(handle, LockHandle)
        assert handle.name == ""
        assert handle.holder_pid == os.getpid()
        assert handle.acquired_at > 0
    finally:
        backend.release(handle)


def test_handle_is_context_manager(backend):
    """`with backend.acquire() as h:` releases on exit."""
    with backend.acquire("", timeout=0.0) as handle:
        assert isinstance(handle, LockHandle)
    # After __exit__, the lock should be re-acquirable in this process.
    handle2 = backend.acquire("", timeout=0.0)
    backend.release(handle2)


def test_release_is_idempotent(backend):
    """Double release must not raise — see CLAUDE.md §atomic+idempotent."""
    handle = backend.acquire("", timeout=0.0)
    backend.release(handle)
    backend.release(handle)  # second call is a no-op


def test_capabilities_returns_lockcapabilities(backend):
    caps = backend.capabilities()
    assert isinstance(caps, LockCapabilities)
    assert isinstance(caps.single_host_only, bool)
    assert isinstance(caps.supports_reentrancy, bool)
    assert isinstance(caps.supports_lease, bool)


# ──────────────────────────────────────────────────────────────────
# Reentrancy contract


def test_second_acquire_same_name_same_process(backend):
    """Non-reentrant by default. Reentrant backends advertise it.

    Either way, the claim and the behavior must match.
    """
    caps = backend.capabilities()
    handle = backend.acquire("", timeout=0.0)
    try:
        if caps.supports_reentrancy:
            # Reentrant backends MUST grant a second acquire from the
            # same process.
            handle2 = backend.acquire("", timeout=0.0)
            backend.release(handle2)
        else:
            # Non-reentrant backends MUST raise.
            with pytest.raises(LockBusy):
                backend.acquire("", timeout=0.0)
    finally:
        backend.release(handle)


def test_distinct_names_do_not_conflict(backend):
    """`acquire("agent")` does not block `acquire("dream")`."""
    h1 = backend.acquire("agent", timeout=0.0)
    try:
        h2 = backend.acquire("dream", timeout=0.0)
        backend.release(h2)
    finally:
        backend.release(h1)


# ──────────────────────────────────────────────────────────────────
# Timeout behavior


def _skip_if_in_process_only_backend(backend_factory, tmp_path) -> None:
    """Skip cross-process tests against backends whose state doesn't survive
    a subprocess fork — currently the Redis backend via fakeredis (in-
    process simulation). Production deployments running conformance
    against a real Redis instance lift this skip — but they ALSO need
    to refactor ``_hold_lock_in_child`` to spawn a child using the
    actual parametrized backend (today it hardcodes
    ``atomic_agents.locks.filesystem`` / ``FilesystemLockBackend``),
    otherwise lifting the skip would test FS-child-vs-Redis-parent
    instead of Redis-vs-Redis. Tracked as a PR 4 follow-up for the
    spec lock work — Step 9.1 testing specialist CRITICAL #3 flagged
    the latent shape. The skip is safe today; the helper-refactor is
    the additional gate for real-Redis CI."""
    backend = backend_factory(tmp_path)
    if backend.backend_id == "redis":
        pytest.skip(
            "Cross-process test requires a backend whose state is "
            "visible to subprocesses; fakeredis is per-instance "
            "in-process simulation. Run against real Redis to exercise "
            "this path in a deployment smoke test."
        )


def test_timeout_zero_fails_fast_against_held_lock(backend_factory, tmp_path):
    _skip_if_in_process_only_backend(backend_factory, tmp_path)
    """timeout=0 raises LockBusy immediately when the lock is held."""
    backend = backend_factory(tmp_path)
    backend_for_other_process = backend_factory(tmp_path)

    ready = tmp_path / ".ready"
    child = multiprocessing.Process(
        target=_hold_lock_in_child,
        args=(
            "atomic_agents.locks.filesystem",
            "FilesystemLockBackend",
            str(tmp_path),
            "",
            2.0,
            str(ready),
        ),
    )
    child.start()
    try:
        _wait_for_ready(ready)
        # Lock is held by the child; timeout=0 should fail fast.
        start = time.monotonic()
        with pytest.raises(LockBusy):
            backend.acquire("", timeout=0.0)
        elapsed = time.monotonic() - start
        # Fail-fast means < 100ms even on a slow CI box.
        assert elapsed < 0.5, f"timeout=0 took {elapsed}s — should fail fast"
    finally:
        child.join(timeout=5)


def test_timeout_positive_respects_deadline(backend_factory, tmp_path):
    """timeout=0.3 raises LockBusy within ~deadline when never granted."""
    _skip_if_in_process_only_backend(backend_factory, tmp_path)
    backend = backend_factory(tmp_path)

    ready = tmp_path / ".ready"
    child = multiprocessing.Process(
        target=_hold_lock_in_child,
        args=(
            "atomic_agents.locks.filesystem",
            "FilesystemLockBackend",
            str(tmp_path),
            "",
            2.0,
            str(ready),
        ),
    )
    child.start()
    try:
        _wait_for_ready(ready)
        start = time.monotonic()
        with pytest.raises(LockBusy):
            backend.acquire("", timeout=0.3)
        elapsed = time.monotonic() - start
        # Deadline + one poll interval slack.
        assert 0.25 < elapsed < 1.0, f"timeout=0.3 elapsed {elapsed}s"
    finally:
        child.join(timeout=5)


def test_timeout_grants_when_holder_releases(backend_factory, tmp_path):
    """timeout=2 succeeds when holder releases before the deadline."""
    _skip_if_in_process_only_backend(backend_factory, tmp_path)
    backend = backend_factory(tmp_path)

    ready = tmp_path / ".ready"
    child = multiprocessing.Process(
        target=_hold_lock_in_child,
        args=(
            "atomic_agents.locks.filesystem",
            "FilesystemLockBackend",
            str(tmp_path),
            "",
            0.3,  # short hold
            str(ready),
        ),
    )
    child.start()
    try:
        _wait_for_ready(ready)
        handle = backend.acquire("", timeout=2.0)
        backend.release(handle)
    finally:
        child.join(timeout=5)


# ──────────────────────────────────────────────────────────────────
# is_held diagnostic


def test_is_held_false_when_never_acquired(backend):
    assert backend.is_held("") is False
    assert backend.is_held("dream") is False


def test_is_held_true_during_other_process_hold(backend_factory, tmp_path):
    """is_held returns True at the moment another process holds the lock.

    Racy by design (state can change between check and any subsequent
    decision); this test just asserts the snapshot is honest at the
    moment of the call.
    """
    _skip_if_in_process_only_backend(backend_factory, tmp_path)
    backend = backend_factory(tmp_path)

    ready = tmp_path / ".ready"
    child = multiprocessing.Process(
        target=_hold_lock_in_child,
        args=(
            "atomic_agents.locks.filesystem",
            "FilesystemLockBackend",
            str(tmp_path),
            "",
            1.0,
            str(ready),
        ),
    )
    child.start()
    try:
        _wait_for_ready(ready)
        assert backend.is_held("") is True
    finally:
        child.join(timeout=5)
    # After child exits, the lock is released (kernel-level).
    assert backend.is_held("") is False


# ──────────────────────────────────────────────────────────────────
# Lease / renew contract


def test_renew_returns_true_for_non_lease_backends(backend):
    """Non-lease backends return True unconditionally so callers can
    invoke renew() periodically without branching on capability."""
    caps = backend.capabilities()
    if caps.supports_lease:
        pytest.skip("backend advertises a lease; tested elsewhere")
    handle = backend.acquire("", timeout=0.0)
    try:
        assert backend.renew(handle) is True
        # Subsequent renews still True.
        assert backend.renew(handle) is True
    finally:
        backend.release(handle)


# ──────────────────────────────────────────────────────────────────
# Context-manager exception path


def test_context_manager_releases_on_exception(backend):
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with backend.acquire("", timeout=0.0):
            raise _Boom("simulated failure inside critical section")
    # The lock must be released — a follow-up acquire must succeed.
    handle = backend.acquire("", timeout=0.0)
    backend.release(handle)


# ──────────────────────────────────────────────────────────────────
# Exception surface


def test_lockbusy_subclasses_atomic_agents_error():
    assert issubclass(LockBusy, AtomicAgentsError)


def test_agent_lock_busy_alias_identity():
    """``AgentLockBusy`` must be the SAME class as ``LockBusy`` so existing
    ``except AgentLockBusy:`` code paths catch the new exception."""
    assert AgentLockBusy is LockBusy


# ──────────────────────────────────────────────────────────────────
# scope() Protocol method (#60 PR 3)


def test_scope_returns_protocol_conforming_backend(backend):
    """scope() MUST return a LockBackend with the same backend_id."""
    sub = backend.scope("dreams")
    assert isinstance(sub, LockBackend)
    assert sub.backend_id == backend.backend_id


def test_scope_preserves_capabilities(backend):
    """scope() MUST NOT change capability claims — a filesystem backend
    doesn't suddenly become distributed by being sub-scoped."""
    sub = backend.scope("dreams")
    assert sub.capabilities() == backend.capabilities()


def test_scope_isolates_locks_from_parent(backend):
    """parent.acquire("") and parent.scope("x").acquire("") MUST NOT collide.

    For filesystem: different on-disk paths (parent: <root>/.lock vs
    sub: <root>/dreams/.lock). For Redis: different key prefixes
    (parent: <prefix>__main__ vs sub: <prefix>dreams:__main__). Either
    way, the two locks are in independent namespaces.
    """
    parent_handle = backend.acquire("", timeout=0)
    try:
        sub = backend.scope("dreams")
        sub_handle = sub.acquire("", timeout=0)
        sub.release(sub_handle)
    finally:
        backend.release(parent_handle)


def test_scope_empty_path_raises(backend):
    with pytest.raises(ValueError, match="non-empty"):
        backend.scope("")

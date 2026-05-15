"""RedisLockBackend — single-instance Redis advisory-lock reference impl.

Ships as part of the LockBackend Protocol arc (#60 PR 3) alongside the
filesystem reference impl from PR 1. Optional dependency: install via
``pip install 'atomic-agents-stack[redis]'`` (declared in
``pyproject.toml`` ``[project.optional-dependencies] redis``).

Semantics
---------

* **Single-instance Redis advisory lock** (the canonical
  ``SET key value NX EX ttl`` pattern). Multi-instance Redlock with
  quorum is **explicitly out of scope** — operators running multi-node
  Redis clusters who need that level of guarantee should wrap their
  own ``LockBackend`` adapter. The single-instance shape is correct for
  the common deployment cases (Cloud Run + managed Redis, Kubernetes
  StatefulSet, gizmo).
* **Lease-backed**: every acquire sets a TTL (default 300 seconds = 5
  minutes). A daemon heartbeat thread renews the lease at TTL/3 (default
  every 100s) until ``release()`` is called or the holder is told to
  stop.
* **Non-reentrant**: a second ``acquire()`` of the same name from the
  same process raises ``LockBusy``. Simpler audit-trail reasoning at
  the cost of explicit relock for callers that need it. See spec/21
  §"Reentrancy contract".
* **Lock loss is surfaced** via the ``LockLost`` exception. If the
  heartbeat thread detects the lease expired (renewal returned False
  because the key no longer exists or was claimed by another holder)
  it stores the failure on the handle; long-running callers
  (``agent.call()``, ``dream.start()``) check between iterations of
  their work loops and abort safely before writing under a lock another
  holder now owns.

Lua scripts
-----------

* **Atomic release**: deletes the key only if its value matches the
  caller's lease token. Prevents a race where holder A's release runs
  AFTER holder A's lease expired and holder B has already acquired —
  without the token check, A's release would delete B's lock.
* **Atomic renew**: extends the TTL only if the value matches.
  Returns 1 on success, 0 if the lease was lost (key missing or claimed
  by another token).

Both are loaded at backend construction via ``register_script`` so the
Redis SHA-cache amortizes the overhead.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..exceptions import LockBusy, LockLost
from .types import LockCapabilities, LockHandle

_logger = logging.getLogger(__name__)


# Lua: atomically delete the key only when its value matches the caller's
# lease token. Eliminates the post-expiry-release-corrupts-new-holder race.
_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# Lua: extend the TTL only when the value still matches. Returns 1 on
# successful renewal, 0 if the lease was lost (key absent, or claimed by
# another holder via expiry-then-acquire).
_RENEW_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


@dataclass
class _RedisHandleState:
    """Opaque state the RedisLockBackend stashes inside ``LockHandle.backend_state``.

    Carries the lease token (the random opaque value Redis stores
    against the lock key — used to prove ownership in the Lua release
    and renew scripts), the heartbeat thread, and the event the thread
    polls to know when to exit. Also surfaces the most recent
    ``LockLost`` exception captured by the heartbeat thread so call
    sites' inter-iteration checks (``check_lock_lost(handle)``) can
    detect mid-flight expiry.
    """
    lease_token: str
    key: str
    heartbeat_thread: threading.Thread
    stop_event: threading.Event
    # Set by the heartbeat thread when renewal returns False (lease
    # expired or was claimed by another holder). Polled by call sites
    # between iterations of their long-running work loops.
    lock_lost: LockLost | None = None


class RedisLockBackend:
    """Redis advisory-lock LockBackend — single-instance, lease-backed.

    Constructed with an already-connected ``redis.Redis`` client (the
    operator owns the connection lifecycle) plus a key prefix scoping
    locks to the deployment. Supports ``scope(sub_path)`` for per-
    subsystem rescoping (e.g., ``backend.scope("dreams")``).

    Args:
        client: An already-connected ``redis.Redis`` instance. The
            backend does NOT manage the connection lifecycle; an
            operator running multiple ``AtomicAgent`` instances in the
            same process should pass the same client to all of them.
        key_prefix: Namespace prefix for every lock key Redis sees.
            Default ``"atomic_agents:lock:"``. Operators with multiple
            deployments sharing a Redis instance set distinct prefixes
            to keep locks isolated.
        lease_ttl_seconds: How long each acquire's lease lives before
            Redis auto-expires it. Default ``300.0`` (5 minutes). MUST
            be longer than the worst-case call site to avoid lost
            locks under load.
        heartbeat_interval_seconds: Renewal cadence. Default
            ``100.0`` (~ ttl/3 with the default TTL). Conservative —
            up to two missed renewals before expiry covers GIL stalls,
            GC pauses, debugger breakpoints.
        poll_interval_seconds: How often ``acquire()`` retries against
            a held lock while the timeout hasn't elapsed. Default
            ``0.1`` (Redis SETNX is cheap; tight polling is fine).
    """

    @property
    def backend_id(self) -> str:
        return "redis"

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "atomic_agents:lock:",
        lease_ttl_seconds: float = 300.0,
        heartbeat_interval_seconds: float = 100.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        # Hard minimum TTL floor — Step 9.1 security specialist
        # (Finding 7). Sub-1-second leases are categorically broken
        # (TTL=0 is rejected by Redis; TTL=0.5 leaves no renewal margin
        # at all). The production-safe recommendation is much higher —
        # spec/21 §"Lease and heartbeat" documents TTL >= 60s for real
        # workloads (a single LLM round-trip can exceed shorter TTLs;
        # GIL-holding operations expire the lease before the heartbeat
        # renews). Tests use 2-5s deliberately to validate heartbeat
        # behavior in <10s of wall time.
        _MIN_LEASE_TTL_SECONDS = 1.0
        if lease_ttl_seconds < _MIN_LEASE_TTL_SECONDS:
            raise ValueError(
                f"lease_ttl_seconds ({lease_ttl_seconds}) below "
                f"minimum floor of {_MIN_LEASE_TTL_SECONDS}s. "
                f"Production use requires TTL much higher than the "
                f"worst-case LLM round-trip — see spec/21 §'Lease "
                f"and heartbeat' for guidance."
            )
        if lease_ttl_seconds <= heartbeat_interval_seconds:
            raise ValueError(
                f"lease_ttl_seconds ({lease_ttl_seconds}) must be > "
                f"heartbeat_interval_seconds ({heartbeat_interval_seconds}); "
                f"otherwise the lease expires before the first renewal lands."
            )
        self._client = client
        self._key_prefix = key_prefix
        self._lease_ttl_seconds = lease_ttl_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._poll_interval_seconds = poll_interval_seconds

        # Process-local non-reentrant guard. Same shape as
        # FilesystemLockBackend — see spec/21 §"Reentrancy contract".
        self._held: set[str] = set()
        self._held_lock = threading.Lock()

        # Lua scripts: use direct EVAL (not EVALSHA via register_script)
        # for broader compatibility with Redis-compatible servers and
        # ``fakeredis``. Real Redis amortizes the script-source bytes
        # at typical heartbeat cadences (60-100s) so the absent SHA-
        # cache is irrelevant for the lock workload.

    # ──────────────────────────────────────────────────────────────────
    # Protocol surface

    def acquire(self, name: str = "", timeout: float = 0.0) -> LockHandle:
        """Acquire the named lock, or raise ``LockBusy`` after timeout.

        Performs ``SET key value NX EX ttl`` retries until granted or
        the deadline elapses. On grant, spawns a daemon heartbeat
        thread that renews the lease every ``heartbeat_interval_seconds``
        until ``release()`` is called or the lease is lost.
        """
        with self._held_lock:
            if name in self._held:
                raise LockBusy(
                    f"RedisLockBackend(prefix={self._key_prefix!r}) "
                    f"already holds lock {name!r} in this process "
                    f"(backend is non-reentrant; "
                    f"supports_reentrancy=False)"
                )

        key = self._key_for(name)
        lease_token = secrets.token_hex(16)  # 32-char opaque ID
        ttl_int = max(1, int(self._lease_ttl_seconds))
        deadline = time.monotonic() + timeout

        while True:
            granted = self._client.set(key, lease_token, nx=True, ex=ttl_int)
            if granted:
                break
            if time.monotonic() >= deadline:
                raise LockBusy(
                    f"RedisLockBackend: lock at key {key!r} "
                    f"held by another holder; waited {timeout}s"
                )
            time.sleep(self._poll_interval_seconds)

        # Spawn the heartbeat thread BEFORE flipping the held-set —
        # ensures release() finds a valid stop_event even if the caller
        # races to release on a different thread.
        stop_event = threading.Event()
        state = _RedisHandleState(
            lease_token=lease_token,
            key=key,
            heartbeat_thread=None,  # type: ignore[arg-type]
            stop_event=stop_event,
        )
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(state,),
            daemon=True,
            # Plain name (no repr quotes) — empty name maps to
            # ``__main__`` to match ``_key_for``'s diagnostic
            # convention. Step 11 adversarial P1-4.
            name=f"RedisLockBackend-heartbeat-{name or '__main__'}",
        )
        state.heartbeat_thread = heartbeat_thread
        heartbeat_thread.start()

        with self._held_lock:
            self._held.add(name)

        handle = LockHandle(
            name=name,
            acquired_at=time.time(),
            holder_pid=os.getpid(),
            backend_state=state,
        )
        object.__setattr__(handle, "_backend", self)
        return handle

    def release(self, handle: LockHandle) -> None:
        """Release the lock. Idempotent.

        Stops the heartbeat thread (sets the stop_event, joins with a
        short timeout — the thread's wait loop wakes on the event) and
        deletes the Redis key via the atomic Lua script (only deletes
        when the lease_token still matches, preventing a stale release
        from corrupting a successor holder).
        """
        state = handle.backend_state
        if state is None or not isinstance(state, _RedisHandleState):
            return  # idempotent — already released

        state.stop_event.set()
        # Don't block teardown on a slow Redis disconnect — the daemon
        # thread will exit on its next wake-up regardless.
        state.heartbeat_thread.join(timeout=0.5)

        try:
            self._client.eval(_RELEASE_SCRIPT, 1, state.key, state.lease_token)
        except Exception as exc:  # pragma: no cover — Redis client unreachable
            # Releasing during teardown should not surface a new
            # exception; the lease will expire on its own via TTL.
            _logger.warning(
                "RedisLockBackend.release(): Lua release failed for "
                "key %r — relying on TTL expiry. Error: %s",
                state.key, exc,
            )

        # Wipe the state so a double-release is a no-op (matches
        # filesystem backend's contract).
        object.__setattr__(handle, "backend_state", None)
        with self._held_lock:
            self._held.discard(handle.name)

    def renew(self, handle: LockHandle) -> bool:
        """Manually extend the lease.

        Returns ``True`` on successful renewal; ``False`` when the lease
        has expired or was claimed by another holder. Most callers do
        NOT need to call this directly — the heartbeat thread spawned at
        ``acquire()`` handles renewal automatically. Exposed for
        diagnostic use and for callers that want explicit control.
        """
        state = handle.backend_state
        if state is None or not isinstance(state, _RedisHandleState):
            return False  # not held — nothing to renew

        ttl_int = max(1, int(self._lease_ttl_seconds))
        result = self._client.eval(
            _RENEW_SCRIPT, 1, state.key, state.lease_token, str(ttl_int),
        )
        return bool(result)

    def is_held(self, name: str = "") -> bool:
        """Diagnostic: is the named lock currently held?

        Racy by design — see ``LockBackend.is_held`` docstring. Issues
        a single GET against the Redis key; True if any value is
        present, False if the key has expired or was never set.
        """
        key = self._key_for(name)
        return self._client.get(key) is not None

    def capabilities(self) -> LockCapabilities:
        """Redis capabilities — distributed-OK, non-reentrant, lease-backed."""
        return LockCapabilities(
            single_host_only=False,
            supports_reentrancy=False,
            supports_lease=True,
        )

    def scope(self, sub_path: str) -> "RedisLockBackend":
        """Return a new RedisLockBackend with the key_prefix extended.

        ``backend.scope("dreams")`` produces a backend whose lock keys
        live under ``<key_prefix>dreams:`` — distinct namespace from the
        parent's locks so dream-lock and agent-lock cannot collide.
        Mirrors ``FilesystemLockBackend.scope()`` semantics.
        """
        if not sub_path:
            raise ValueError(
                "RedisLockBackend.scope(sub_path) requires a non-empty "
                "sub_path; use the existing backend for the same scope."
            )
        return RedisLockBackend(
            self._client,
            key_prefix=f"{self._key_prefix}{sub_path}:",
            lease_ttl_seconds=self._lease_ttl_seconds,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            poll_interval_seconds=self._poll_interval_seconds,
        )

    # ──────────────────────────────────────────────────────────────────
    # Internal helpers

    def _key_for(self, name: str) -> str:
        """Map the semantic ``name`` to a Redis key.

        Empty name → ``<key_prefix>__main__`` (canonical token for "the
        bare lock"). Non-empty name → ``<key_prefix><name>``. Redis
        doesn't care about empty keys but explicitly tokenizing the
        empty case keeps the diagnostic output (``KEYS atomic_agents:
        lock:*`` from an operator's ``redis-cli``) readable.
        """
        return f"{self._key_prefix}{name or '__main__'}"

    def _heartbeat_loop(self, state: _RedisHandleState) -> None:
        """Daemon thread that renews the lease until ``stop_event`` fires.

        Sleeps in chunks (using ``Event.wait(timeout)``) so a release
        can wake the thread early instead of blocking on a 100-second
        sleep. On renewal failure (Lua script returns 0 → lease lost),
        captures the failure as a ``LockLost`` exception on the shared
        state; long-running call sites poll ``check_lock_lost(handle)``
        between iterations of their work loops and abort cleanly.
        """
        while not state.stop_event.wait(timeout=self._heartbeat_interval_seconds):
            try:
                renewed = self._client.eval(
                    _RENEW_SCRIPT,
                    1,
                    state.key,
                    state.lease_token,
                    str(max(1, int(self._lease_ttl_seconds))),
                )
            except Exception as exc:
                # Redis became unreachable. Treat as lock-lost AND log
                # loudly (Step 11 adversarial P0-2) — silent state-flag
                # mutation alone is invisible to operators whose call
                # sites don't promptly invoke ``check_lock_lost``.
                msg = (
                    f"RedisLockBackend heartbeat: Redis unreachable "
                    f"while renewing key {state.key!r} — treating as "
                    f"lease-lost ({exc})"
                )
                _logger.warning(msg)
                state.lock_lost = LockLost(msg)
                return

            if not renewed:
                msg = (
                    f"RedisLockBackend heartbeat: lease for key "
                    f"{state.key!r} expired or was claimed by another "
                    f"holder; renewal returned 0. Call sites must "
                    f"invoke check_lock_lost(handle) to surface this."
                )
                _logger.warning(msg)
                state.lock_lost = LockLost(msg)
                return


def check_lock_lost(handle: LockHandle) -> None:
    """Raise ``LockLost`` if any heartbeat thread detected lease expiry.

    Long-running call sites (``agent.call()`` multi-turn loop,
    ``dream.start()`` pipeline checkpoints) call this between
    iterations. No-op for filesystem-backed handles (no heartbeat
    thread; ``backend_state`` doesn't expose a ``lock_lost`` attribute).

    Structural check (``hasattr(state, "lock_lost")``) rather than
    ``isinstance(_RedisHandleState)`` — Step 9.1 maintainability
    specialist CRITICAL — so future lease-backed backends (Postgres
    advisory etc) automatically integrate without editing this
    dispatcher. The implementer contract is documented in spec/21
    §"Implementer contract: heartbeat thread": lease-backed backends
    MUST expose a ``lock_lost`` attribute on their handle's
    ``backend_state`` that is ``None`` while healthy and a
    ``LockLost`` instance after the heartbeat detects expiry.
    """
    state = handle.backend_state
    if state is None:
        return
    lock_lost = getattr(state, "lock_lost", None)
    # ``isinstance(LockLost)`` guard prevents two false positives that
    # a bare ``is not None`` check would trip on: (1) MagicMock auto-
    # attrs in tests that mock ``agent.lock_backend`` (the mocked
    # ``backend_state.lock_lost`` is a MagicMock, truthy but not an
    # exception); (2) a buggy third-party backend that sets
    # ``lock_lost`` to a sentinel value instead of a real exception.
    if isinstance(lock_lost, LockLost):
        raise lock_lost


def make_redis_backend_from_url(
    url: str,
    *,
    key_prefix: str | None = None,
    lease_ttl_seconds: float | None = None,
    heartbeat_interval_seconds: float | None = None,
) -> RedisLockBackend:
    """Construct a RedisLockBackend from a redis:// URL.

    Used by ``get_default_lock_backend()`` when the operator sets
    ``ATOMIC_AGENTS_LOCK_BACKEND=redis`` + ``ATOMIC_AGENTS_LOCK_BACKEND_URL=redis://...``.
    Lazy-imports ``redis`` so the dependency is only required when the
    extra is actually selected.

    Args:
        url: Standard ``redis://host:port/db`` URL. The ``redis`` package
            handles parsing.
        key_prefix: Override the default ``"atomic_agents:lock:"`` prefix.
            When None, uses the env var ``ATOMIC_AGENTS_LOCK_REDIS_KEY_PREFIX``
            if set, otherwise the default.
        lease_ttl_seconds: Override the default 300s lease TTL. When
            None, uses ``ATOMIC_AGENTS_LOCK_REDIS_TTL_SECONDS`` if set,
            otherwise default.
        heartbeat_interval_seconds: Override the default 100s heartbeat
            interval. When None, uses ``ATOMIC_AGENTS_LOCK_REDIS_HEARTBEAT_SECONDS``
            if set, otherwise default.

    Raises:
        ImportError: when ``redis`` extra is not installed. Caller
            (typically ``get_default_lock_backend()``) is expected to
            surface this as an operator-facing error with the install
            instruction.
    """
    try:
        import redis as _redis_pkg
    except ImportError as exc:
        raise ImportError(
            "RedisLockBackend requires the 'redis' extra. "
            "Install via: pip install 'atomic-agents-stack[redis]'"
        ) from exc

    parsed = urlparse(url)
    if parsed.scheme not in ("redis", "rediss"):
        raise ValueError(
            f"RedisLockBackend URL must start with redis:// or rediss://, "
            f"got {url!r}"
        )

    client = _redis_pkg.Redis.from_url(url, decode_responses=True)
    kwargs: dict[str, Any] = {}
    resolved_prefix = (
        key_prefix
        if key_prefix is not None
        else os.environ.get("ATOMIC_AGENTS_LOCK_REDIS_KEY_PREFIX")
    )
    if resolved_prefix is not None:
        kwargs["key_prefix"] = resolved_prefix
    resolved_ttl = (
        lease_ttl_seconds
        if lease_ttl_seconds is not None
        else _env_float("ATOMIC_AGENTS_LOCK_REDIS_TTL_SECONDS")
    )
    if resolved_ttl is not None:
        kwargs["lease_ttl_seconds"] = resolved_ttl
    resolved_hb = (
        heartbeat_interval_seconds
        if heartbeat_interval_seconds is not None
        else _env_float("ATOMIC_AGENTS_LOCK_REDIS_HEARTBEAT_SECONDS")
    )
    if resolved_hb is not None:
        kwargs["heartbeat_interval_seconds"] = resolved_hb
    return RedisLockBackend(client, **kwargs)


def _env_float(name: str) -> float | None:
    """Parse an env var as float; return None when unset or empty.

    Raises ``ValueError`` on a non-float value so a typo in deployment
    config surfaces loudly at backend construction instead of silently
    falling back to the default.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return float(raw)

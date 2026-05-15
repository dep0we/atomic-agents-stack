# 21 — LockBackend Protocol

**Status:** DRAFT — locks at PR 4 of #60 after the conformance suite parametrizes across filesystem + Redis.
**Origin:** [#60](https://github.com/dep0we/atomic-agents-stack/issues/60).
**Arc shape:** PR 1 (Protocol scaffolding + filesystem reference impl + conformance suite), PR 2 (wire backend into the five legacy lock sites + ``_locks.py`` deprecation shim + ``doctor.check_locks`` through the backend), PR 3 (Redis reference impl + ``scope()`` Protocol method + ``LockLost`` exception + heartbeat thread + operator override surface — this prose), PR 4 (spec lock + parameterized conformance across both backends + README/ROADMAP refresh).

## Overview

``atomic_agents/_locks.py`` uses ``fcntl.flock`` — per-host POSIX advisory locking. ``AgentLock`` writes a ``.lock`` file at the agent root and acquires it via ``fcntl.flock(LOCK_EX|LOCK_NB)``. Every ``agent.call()``, every ``dream.start()``, every ``memory.apply_staging()`` holds an advisory lock for the duration of the operation.

This works perfectly on a single box. It **breaks** the moment the deployment shape becomes:

* Multiple processes on different hosts (NFS doesn't reliably honor ``fcntl``).
* Containerized deployments where the lock dir is shared but the kernel isn't.
* Cloud Run / Lambda / serverless — filesystems are ephemeral; locks don't survive scale events.
* Redis-backed scale-out — locks should be Redis advisory locks (``SET ... NX EX``).
* Postgres-backed scale-out — locks should be ``pg_advisory_lock()`` calls.

``LockBackend`` is one of the open protocols in the protocol-pattern series alongside the shipped ``MemoryBackend`` (spec/20), ``LLMBackend`` (spec/31), and ``JudgeBackend`` (spec/28). Lock is the most urgent of the remaining: every other primitive has a single-box workaround, but locks are the cliff for multi-process deployments. Concrete users blocked today: Meridian on Cloud Run; Bishop on gizmo running parallel agents over shared memory; any future SaaS deployment.

The Protocol is **not** a generic mutex API. It is the minimal contract the framework needs to satisfy spec/04's invariant: "the agent acquires its lock before any vault write; releases on completion." Backends that meet this contract participate fully in the agent runtime without forking core.

## Module layout

```
atomic_agents/locks/
├── __init__.py        # registry: register_lock_backend / get_lock_backend / list_lock_backends
├── types.py           # canonical types: LockHandle, LockCapabilities
├── backend.py         # LockBackend Protocol contract
└── filesystem.py      # FilesystemLockBackend reference implementation
```

Mirrors ``atomic_agents/llm/{__init__.py, types.py, backend.py, anthropic.py}`` and ``atomic_agents/memory/{__init__.py, backend.py, filesystem.py}``. The split into ``types.py`` separate from ``backend.py`` matches the LLM module's shape: canonical types ship without pulling in the Protocol contract or any reference implementation.

## Canonical types

### ``LockHandle`` — granted-lock receipt

```python
@dataclass(frozen=True)
class LockHandle:
    name: str
    acquired_at: float
    holder_pid: int
    backend_state: Any = None
    # __enter__ / __exit__ — handle is its own context manager
```

The handle is opaque from the caller's perspective: ``backend_state`` carries an open file descriptor (filesystem), a Redis lease token (Redis), an advisory-lock key (Postgres), or whatever the backend needs to locate its resource on ``release()`` / ``renew()``. Consumers outside the issuing backend MUST NOT inspect ``backend_state``.

The handle implements the context-manager Protocol so call sites read naturally:

```python
backend = FilesystemLockBackend(agent_root)
with backend.acquire(name="dream", timeout=30.0) as handle:
    # critical section
    ...
# handle.__exit__ called backend.release(handle) automatically
```

Frozen for immutability + value-comparison; not hashable when ``backend_state`` is unhashable (file descriptors are int and hashable; Redis lease tokens typically are too; the type does not promise hashability).

### ``LockCapabilities`` — backend capability declaration

```python
@dataclass(frozen=True)
class LockCapabilities:
    single_host_only: bool
    supports_reentrancy: bool
    supports_lease: bool
```

Conformance tests assert claim-vs-behavior parity. A backend that claims ``supports_reentrancy=True`` MUST let the same process re-acquire its own held lock without raising; one that claims ``supports_lease=True`` MUST honor ``renew()`` by extending the lease deadline. Honest capabilities let callers fail fast against incompatible backends rather than discovering the mismatch mid-operation.

* ``single_host_only`` — ``FilesystemLockBackend=True`` (NFS unreliable, kernel-scoped). A Redis or Postgres advisory backend = ``False``.
* ``supports_reentrancy`` — ``FilesystemLockBackend=False`` (the layered held-names set raises on second acquire). A Redis backend with per-``(name, holder_pid)`` ref-counting = ``True``.
* ``supports_lease`` — ``FilesystemLockBackend=False`` (POSIX flock has no TTL — kernel releases on process death is recovery, not lease). A Redis backend with ``SET ... EX`` = ``True``.

## ``LockBackend`` Protocol surface

```python
@runtime_checkable
class LockBackend(Protocol):
    @property
    def backend_id(self) -> str: ...
    def acquire(self, name: str = "", timeout: float = 0.0) -> LockHandle: ...
    def release(self, handle: LockHandle) -> None: ...
    def renew(self, handle: LockHandle) -> bool: ...
    def is_held(self, name: str = "") -> bool: ...
    def capabilities(self) -> LockCapabilities: ...
    def scope(self, sub_path: str) -> "LockBackend": ...   # #60 PR 3
```

### Scope binding

Scope is bound at backend **construction**, not per call. ``FilesystemLockBackend(scope_root)`` ties the backend to a directory; ``acquire(name="")`` looks up ``<scope_root>/.lock`` and ``acquire(name="dream")`` looks up ``<scope_root>/.dream.lock``. The Protocol surface intentionally takes no ``path`` / ``owner_root`` argument because distributed backends (Redis key-prefix, Postgres database-scope) have no meaningful path concept.

PR 2 of #60 instantiates one backend per scope inside ``AtomicAgent.__init__`` / ``DreamRunner`` / ``FilesystemBackend``; the registry's role is the operator-pin lookup ("filesystem vs redis") that returns the chosen class.

### ``acquire(name, timeout)`` semantics

* ``name="" → <scope>/.lock`` on the filesystem backend. Preserves the on-disk artifact ``_locks.AgentLock`` writes today; doctor and any external scripts pinning the path keep working.
* ``name="dream" → <scope>/.dream.lock`` — the dot prefix matches the legacy ``<agent>/dreams/.lock`` shape; the conversion is documented in PR 2.
* ``timeout=0.0`` fails fast — useful for cron / job contexts that should not block.
* ``timeout>0`` polls until granted or the deadline elapses. Filesystem polls at ``poll_interval`` (default 0.5s). Redis-style backends MAY use blocking primitives.
* Raises ``atomic_agents.exceptions.LockBusy`` on timeout. ``AgentLockBusy`` is exported as a backwards-compatible alias of ``LockBusy``; existing ``except AgentLockBusy:`` code paths continue to work.

### Reentrancy contract — NON-REENTRANT by default

A second ``acquire()`` of the same name from the same process MUST raise ``LockBusy`` unless the backend advertises ``supports_reentrancy=True``. Today's ``fcntl.flock`` semantics within a single process are subtle (a second open + ``LOCK_EX`` from the same process succeeds because flock is fd-scoped, not process-scoped); the filesystem backend layers a process-local held-names set on top of the OS primitive to give the simple, honest contract this Protocol advertises.

The non-reentrant default closes the JudgeBackend trap of leaving semantics undefined and litigating them across PR 3 of the arc. Backends that want reentrancy advertise it explicitly; conformance tests assert the claim is honest.

### ``scope(sub_path)`` — sub-scoping a backend (#60 PR 3)

Lets the framework re-scope an operator-provided backend without the operator having to construct multiple instances. ``backend.scope("dreams")`` returns a new ``LockBackend`` whose locks live in an isolated namespace from the parent. Filesystem joins the path (``<scope_root>/dreams``); Redis prepends the key-prefix (``<key_prefix>dreams:``).

The sub-scope MUST be a backend of the same type with the same ``backend_id`` and equivalent ``capabilities()`` — sub-scoping cannot change capability claims (a filesystem backend doesn't suddenly become distributed by being sub-scoped). The conformance suite asserts this invariant.

The operator surface is intentionally minimal: an operator passes ONE backend to ``AtomicAgent`` (constructor kwarg) or sets ONE env-var pair (``ATOMIC_AGENTS_LOCK_BACKEND`` + ``ATOMIC_AGENTS_LOCK_BACKEND_URL``). The framework internally calls ``scope("dreams")`` on that backend inside ``DreamRunner.__init__`` to derive the dream lock backend. The operator never has to know dreams need a separate lock namespace.

### Lease / heartbeat — ``renew()`` is no-op for non-lease backends

```python
def renew(self, handle: LockHandle) -> bool: ...
```

For backends with ``supports_lease=False`` (``FilesystemLockBackend`` today): returns ``True`` unconditionally. POSIX advisory locks have no TTL; if the holder process dies, the kernel releases the lock automatically — that's recovery, not a lease.

For lease-backed backends: ``renew()`` extends the lease by the backend's configured lease duration. Returns ``True`` on successful renewal; returns ``False`` when the lease had already expired (the caller MUST treat this as lock-lost and re-acquire before continuing the critical section).

``renew()`` is in the Protocol surface from PR 1 — not PR 3 — so that PR 2's call-site wiring (``agent.call()`` runs can take 5–30 minutes including LLM round-trips, MCP tool calls, helpers, delegates; ``dream.start()`` runs can take 10+ minutes) can install a heartbeat thread once and the heartbeat call is a no-op for filesystem but real for Redis without the call-site needing to branch on capability.

### ``is_held(name)`` — diagnostic only, racy by design

``is_held()`` returns whether the named lock is observable as held at the moment of the call. The state can change between check and any subsequent decision; callers MUST NOT use ``is_held()`` for control flow (use ``try: acquire(timeout=0); except LockBusy:`` instead).

The method exists because ``atomic-agents doctor``'s ``check_locks`` surfaces "another process holds this agent's lock right now" to the operator — a diagnostic-only signal.

## ``FilesystemLockBackend`` — reference implementation

Conforms to the Protocol with the constructor signature ``FilesystemLockBackend(scope_root, *, poll_interval=0.5)``.

* Wraps ``fcntl.flock(LOCK_EX | LOCK_NB)`` against ``<scope_root>/<computed_path>``.
* Writes ``pid=<pid> acquired=<unix_ts>`` to the lock file on grant for debugging — matches the legacy ``AgentLock`` on-disk format so external scripts and ``doctor.check_locks`` keep reading the same shape.
* Thread-safe: the process-local held-names set is guarded by a ``threading.Lock``.
* Crash recovery: relies on kernel ``flock`` semantics — if the holding process dies, the kernel releases the lock.

Capabilities: ``single_host_only=True``, ``supports_reentrancy=False``, ``supports_lease=False``.

## Exception surface

``atomic_agents.exceptions.LockBusy`` is the canonical exception for "timeout elapsed without grant." ``AgentLockBusy`` is an alias (identical class identity) preserved for backwards compatibility — existing ``except AgentLockBusy:`` code paths continue to work without change. ``DreamInProgress`` stays separate — it's a domain-specific exception, not a lock primitive.

## Registry

```python
from atomic_agents.locks import register_lock_backend, get_lock_backend, list_lock_backends

register_lock_backend("filesystem", FilesystemLockBackend)
cls = get_lock_backend("filesystem")            # → FilesystemLockBackend
backend = cls(agent_root)                       # caller instantiates with scope
ids = list_lock_backends()                      # ["filesystem"]
```

The registry stores **classes**, not instances (unlike the LLM registry, which stores instances). Lock backends carry per-scope construction arguments; the registry's role is the operator-pin lookup that resolves a backend_id to a class. The caller (``AtomicAgent.__init__`` in PR 2) instantiates the chosen class with its scope-specific args.

The default ``"filesystem"`` registration happens at import time inside ``atomic_agents/locks/__init__.py``.

## ``RedisLockBackend`` — distributed reference implementation (#60 PR 3)

Ships as the canonical distributed backend. Optional dependency: install via ``pip install 'atomic-agents-stack[redis]'`` (declared in ``pyproject.toml`` ``[project.optional-dependencies] redis``).

**Semantics:**

- **Single-instance Redis advisory lock** (the canonical ``SET key value NX EX ttl`` pattern). Multi-instance Redlock with quorum is **explicitly out of scope** — operators running multi-node Redis clusters who need that level of guarantee should wrap their own ``LockBackend`` adapter on top of redis-py's Redlock primitives. The single-instance shape is correct for the common deployment cases (Cloud Run + managed Redis, Kubernetes StatefulSet, gizmo's single Redis instance).
- **Lease-backed**: every acquire sets a TTL (default 300 seconds). The heartbeat thread renews at TTL/3 (default every 100s) until ``release()``.
- **Non-reentrant**: a second ``acquire()`` of the same name from the same process raises ``LockBusy``. Simpler audit-trail reasoning at the cost of explicit relock for callers that need reentrancy. A reentrant variant is possible with per-``(name, holder_pid, thread_id, lease_token)`` ref-counting but is deferred to a future contributor — the simple contract makes audit reasoning straightforward and prevents buggy callers from hiding stuck-lock states.
- **Atomic release + renew via Lua scripts**: deletes the key only when the lease_token still matches (prevents a stale release from corrupting a successor holder); extends the TTL only when the value still matches (prevents a stale renew from extending another holder's lock).

Capabilities: ``single_host_only=False``, ``supports_reentrancy=False``, ``supports_lease=True``.

Construction:

```python
import redis
from atomic_agents.locks.redis import RedisLockBackend

client = redis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
backend = RedisLockBackend(
    client,
    key_prefix="my-deployment:atomic_agents:lock:",
    lease_ttl_seconds=300.0,            # 5-min lease
    heartbeat_interval_seconds=100.0,   # TTL/3
)
```

The framework's ``get_default_lock_backend(scope_root)`` factory (``atomic_agents/locks/__init__.py``) calls ``make_redis_backend_from_url`` when ``ATOMIC_AGENTS_LOCK_BACKEND=redis`` and ``ATOMIC_AGENTS_LOCK_BACKEND_URL`` is set.

## Operator surface — NOT a ``locks.md`` config

Lock backend choice is a **deployment-level** decision (the whole framework instance picks "filesystem" or "redis"), not an agent-author-level decision. Contrast with:

* ``judges.md`` — per-agent because judge policy is per-agent-author concern.
* ``model.md`` ``provider:`` — per-agent because model choice is per-agent.

A ``locks.md`` markdown config would only make sense if multiple lock backends were registered simultaneously and an agent author wanted to pin one — an unlikely scenario; lock backend is operator's call.

PR 3 of #60 exposed the choice via TWO paths:

1. **Constructor kwarg** — programmatic operators (Python entry-points wiring the framework into Cloud Run, Kubernetes deployments with custom Redis pools) pass `AtomicAgent(..., lock_backend=RedisLockBackend(...))` to bypass the env-var resolution entirely.
2. **Environment variables** — deployment-config operators (Docker, launchd, Cloud Run env, gizmo systemd units) set:
   - `ATOMIC_AGENTS_LOCK_BACKEND` — backend id (default `filesystem`). Today `filesystem` and `redis` are supported.
   - `ATOMIC_AGENTS_LOCK_BACKEND_URL` — connection URL when non-filesystem (e.g., `redis://localhost:6379/0`).
   - `ATOMIC_AGENTS_LOCK_REDIS_KEY_PREFIX` — override the default `atomic_agents:lock:` namespace prefix.
   - `ATOMIC_AGENTS_LOCK_REDIS_TTL_SECONDS` — override the default 300s lease TTL.
   - `ATOMIC_AGENTS_LOCK_REDIS_HEARTBEAT_SECONDS` — override the default 100s heartbeat cadence.

The env var name `ATOMIC_AGENTS_LOCK_BACKEND_URL` is intentionally generic (not `_REDIS_URL`) so future Postgres advisory backends + other distributed shapes plug in via the same key without operators having to relearn the env vocabulary.

The constructor kwarg ALWAYS wins. Operator-config layering: env vars are deployment-level (per-instance, per-host); the kwarg is per-agent-construction. A test that constructs an `AtomicAgent` with an explicit `lock_backend=` bypasses any env vars the deployment may have set.

If a per-agent override surface ever proves necessary (today's design treats lock backend as deployment-level, not per-agent-author), it lands the same way `model.md`'s `provider:` field was added — post-hoc, in a follow-up.

## What this PR does NOT do

PR 1 shipped pure scaffolding — Protocol, filesystem reference impl, tests, spec. PR 2 wired the four legacy ``AgentLock`` / ``_DreamLock`` call sites plus ``doctor.check_locks`` through the backend and converted ``_locks.py`` to a deprecation shim (sunset planned for v1.0 per CLAUDE.md rule #14). The inline ``_fcntl.flock`` at ``memory/filesystem.py``'s ``_per_file_lock`` is **deliberately NOT subsumed** (filesystem-implementation invariant — see below). PR 3 ships the distributed reference impl + operator override surface. PR 4 locks this spec and parametrizes the conformance suite across both backends.

**``FilesystemBackend`` test-override surface.** ``FilesystemBackend.__init__`` accepts an optional ``apply_staging_lock_timeout: float = 30.0`` constructor kwarg. Tests that need fail-fast behavior on a held lock (e.g., dream-pipeline tests that simulate an in-flight ``agent.call()``) construct the backend with ``apply_staging_lock_timeout=0.0``. The kwarg is per-instance and immutable post-construction — Step 9.1 security review (PR 2) rejected the alternative class-attribute pattern as a process-wide mutation risk. Widening the ``MemoryBackend`` Protocol (spec/20) to take a ``lock_timeout`` argument on ``apply_staging`` was rejected per CLAUDE.md rule #2 ("Protocols stay clean") — the constructor kwarg lives on the concrete reference impl only.

The inline ``_fcntl.flock`` in ``memory/filesystem.py`` is a per-file lock co-located with the target note (``<note>.lock``). It is a **filesystem-implementation invariant** — it closes a TOCTOU window inside the filesystem backend's write path — and is intentionally NOT subsumed by the LockBackend Protocol. A future Redis-backed memory backend would not use ``<note>.lock`` files; it would use Redis transactions or row locks. Forcing it through the lock Protocol would distort both Protocols. PR 2 adds an inline comment at the call site explaining this.

The ``doctor.check_locks`` diagnostic (``doctor.py:725-790``) is a fifth call site PR 2 also touches — today it opens ``.lock`` directly with ``fcntl.flock``; after PR 2 it routes through ``agent.lock_backend.is_held(...)`` so the diagnostic reflects the backend's reality.

PR 3 of #60 adds a new ``check_lock_backend`` doctor check that validates **operator-config coherence**:

- Unset / ``filesystem`` → PASS (today's default; no extras needed)
- ``redis`` with extra installed AND URL reachable → PASS
- ``redis`` with extra NOT installed → FAIL with `pip install 'atomic-agents-stack[redis]'` hint
- ``redis`` with extra installed but URL unreachable → **WARN** (NOT FAIL). Matches the ``check_provider_keys`` pattern: doctor never crashes on missing/unreachable optional infrastructure. A developer running ``atomic-agents doctor`` from a coffee-shop wifi (when their deployment env points at prod Redis) sees a warning, not a doctor crash.
- Unknown ``backend_id`` (typo) → FAIL with the registered backend list

``check_locks`` (the held-state probe) is also operator-config-aware after PR 3: it constructs the same backend the runtime would construct via ``get_default_lock_backend`` and probes via ``is_held("")``. WARN-on-unreachable applies here too.

## Reserved future capabilities

These are not committed in PR 1 but are reserved in the namespace so future expansions don't need a breaking Protocol change:

* ``AsyncLockBackend`` — async variant for HTTP-served deployments. Same shape; ``acquire`` becomes ``async def acquire``.
* ``ShareableLockBackend`` — adds ``acquire_shared(name)`` for read/write lock distinctions (Postgres advisory's ``pg_advisory_lock_shared``). Reserved because the current single-writer assumption already serves vault-write discipline.

## Conformance test surface

PR 1 ships:

* ``tests/test_lock_protocol_conformance.py`` — ~20 tests parametrized via a ``backend_factory`` fixture, ready to receive PR 3's distributed reference impl.
* ``tests/test_lock_filesystem_backend.py`` — ~10 filesystem-specific tests (on-disk path mapping, PID file format, kernel-level crash recovery, ``poll_interval`` behavior, deadline precision).

PR 4 freezes the conformance surface against both filesystem + the PR 3 distributed reference impl and locks this spec doc.

## Related

* spec/20 — ``MemoryBackend`` (the original Protocol pattern; this spec mirrors its shape).
* spec/31 — ``LLMBackend`` (second-template Protocol; this spec mirrors its ``types.py``/``backend.py``/registry split).
* spec/28 — ``JudgeBackend`` (third-template; the lock arc adopts the same "lock spec at PR 4" discipline).
* ``docs/TENSIONS.md`` — the home-vs-org throughline tension this Protocol's existence is in service of.

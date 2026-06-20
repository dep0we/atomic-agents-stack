# 20 — MemoryBackend Protocol

**Status:** **locked** (spec matches implementation as of #382 PR 1).

How atomic-agents abstracts memory I/O behind a pluggable protocol,
and what every backend implementation must satisfy.

---

## Overview

All memory reads and writes in atomic-agents go through the `MemoryBackend`
protocol (defined in `atomic_agents/memory/backend.py`). The default
implementation — `FilesystemBackend` — uses the standard vault layout on disk.
A future `SQLiteBackend` or `PostgresBackend` can plug in by satisfying the
same protocol.

`AtomicAgent` exposes `agent.memory: MemoryBackend`. Call-site code never
touches disk paths directly.

---

## Module layout

```
atomic_agents/memory/
├── __init__.py        # registry: register_backend() / get_backend()
├── backend.py         # MemoryBackend Protocol + all dataclasses
└── filesystem.py      # FilesystemBackend (default) — lifts logic from
                       # old _capture.py + _versioning.py
```

---

## Core data types

### `NoteRef` — lightweight listing token

```python
@dataclass
class NoteRef:
    name: str                       # bare filename, e.g. "feedback_comm_style.md"
    type: str                       # "user" | "feedback" | "project" | "decision" | "reference"
    description: str
    captured: date | None
    last_seen: date | None
    pinned: bool
    confidence: str
    archived: bool
    superseded_by: str | None       # filename of the superseding note, if any
```

### `Note` — full read model

`Note` is a superset of `NoteRef`; it includes `body`, `sources`, `tags`,
`expires_at`, `supersedes`, `merge_into`, `schema_version`, and
`extra_frontmatter` for non-standard keys.

Returned by `read_note()` and `read_version()`.

### `VersionRef` — opaque version handle

```python
@dataclass
class VersionRef:
    backend_id: str   # e.g. "20260507T120000000000Z_abc12345.md"

    def __str__(self) -> str: ...
```

Callers treat `VersionRef` as opaque — they never construct them manually.
They are returned by `list_versions()` and accepted by `read_version()`,
`restore_version()`, and `redact_version()`.

### `WritePolicy` — per-call path enforcement

```python
@dataclass
class WritePolicy:
    write_paths: list[Path]
    read_only_paths: list[Path] = field(default_factory=list)
```

Every mutating operation takes a `WritePolicy`. The backend verifies the
resolved target path is under one of `write_paths` and NOT under
`read_only_paths`. Violations raise `WritePathViolation`.

### `StagedMemory` — bulk staging area

Returned by `create_staging()`. Has its own `write_note()` method so the
dream pipeline can write a complete alternate memory set before atomically
swapping it into the live vault via `apply_staging()`.

### `MemoryStats` — fleet health data

```python
@dataclass
class MemoryStats:
    total_notes: int
    by_type: dict[str, int]
    live_bytes: int
    version_history_bytes: int
    most_churned: list[tuple[str, int]]   # (filename, snapshot_count), top 20
```

---

## Protocol surface

```python
@runtime_checkable
class MemoryBackend(Protocol):

    # ─── Read operations ─────────────────────────────────────────────

    def list_notes(
        self,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]: ...

    def read_note(self, name: str) -> Note | None: ...

    def list_pinned(self) -> list[NoteRef]: ...

    def list_recent(
        self,
        n: int,
        exclude_pinned: bool = True,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]: ...

    def list_stale(
        self,
        threshold_days: int,
        exclude_pinned: bool = True,
    ) -> list[NoteRef]: ...

    def list_orphans(self) -> list[NoteRef]: ...

    def list_by_type(self, type_name: str) -> list[NoteRef]: ...

    def render_index_summary(self) -> str: ...

    # ─── Write operations ────────────────────────────────────────────

    def write_note(
        self,
        capture: Capture,
        policy: WritePolicy,
        expected_content_sha256: str | None = None,
    ) -> NoteRef: ...

    # ─── Versioning ──────────────────────────────────────────────────

    def list_versions(self, name: str) -> list[VersionRef]: ...

    def read_version(self, version_ref: VersionRef) -> Note: ...

    def restore_version(
        self,
        name: str,
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> NoteRef: ...

    def redact_version(
        self,
        version_ref: VersionRef,
        replacement: str = "[REDACTED]",
    ) -> None: ...

    def resolve_version_token(self, name: str, token: str) -> VersionRef: ...

    # ─── Staging (dream pipeline) ────────────────────────────────────

    def create_staging(self) -> StagedMemory: ...

    def apply_staging(self, staging: StagedMemory, policy: WritePolicy) -> None: ...

    def discard_staging(self, staging: StagedMemory) -> None: ...

    # ─── Stats ───────────────────────────────────────────────────────

    def stats(self) -> MemoryStats: ...

    def version_count(self, name: str) -> int: ...

    def last_mutation_at(self, name: str) -> datetime | None: ...

    # ─── Search ──────────────────────────────────────────────────────

    @property
    def supports_semantic_search(self) -> bool: ...

    def search(self, query: str, limit: int = 10) -> list[NoteRef]: ...

    # ─── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None: ...
```

---

## Merge semantics for `write_note()`

`write_note()` maps a `Capture` to one of four cases:

| Case | Trigger | Action |
|------|---------|--------|
| 1 — merge-into | `capture.merge_into` is set | Snapshot existing note, update `last_seen` + `sources` |
| 2 — fresh write | note file does not exist | Write note, update INDEX |
| 3 — orphan recovery | note exists, content identical | Snapshot + update INDEX |
| 4 — collision | note exists, content differs, no `merge_into` | Raise `SchemaValidationError` |

### SHA-256 precondition

Pass `expected_content_sha256` to enforce a CAS (compare-and-swap) write:
the backend hashes the current on-disk content and raises
`MemoryPreconditionFailed` if it doesn't match. Used for idempotent
re-delivery of captures.

---

## FilesystemBackend storage layout

```
<agent_root>/
  memory/
    *.md                           # atomic notes
    INDEX.md                       # routing index (never versioned)
    .versions/
      <note-stem>/
        <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md   # immutable snapshots
  dreams/
    .staging-<uuid>/
      memory/                      # staging area (create_staging output)
    drm_<YYYY-MM-DDTHHMMSS>_<6hex>/
      memory/                      # dream pipeline output (for review)
      report.md
      manifest.json
```

### INDEX.md format

```markdown
# Memory Index

## User Profile
- [Note name](filename.md) — description

## Critical Feedback
- [Note name](filename.md) — description

## Active Projects
...
```

Section headers map from note `type` values:

| type | Section header |
|------|---------------|
| `user` | User Profile |
| `feedback` | Critical Feedback |
| `project` | Active Projects |
| `decision` | Locked Decisions |
| `reference` | Reference |
| *(other)* | Reference |

---

## Path-traversal protection

Every user-supplied path component (note name, `merge_into`, version token)
passes through `safe_resolve_under(name, base_dir)` before any file I/O.
Traversal attempts raise `PathTraversalError` (caught and re-raised as
`WritePathViolation` in the merge path for backward compat).

---

## Staging and `apply_staging()`

`create_staging()` returns a `FilesystemStagedMemory` with `staging_dir =
<agent_root>/dreams/.staging-<uuid>/memory/`.

`apply_staging(staging, policy)`:
1. Acquires `AgentLock` (same lock as `AtomicAgent.call()`) to serialize
   against in-flight captures.
2. Archives live `memory/` → `memory.archived-<ts>/`.
3. Renames `staging_dir` → `memory/`.
4. Releases lock.

`discard_staging()` removes `staging_dir` and its parent directory from disk.

`DreamRunner.apply()` wraps the dream output directory as a
`FilesystemStagedMemory` and calls `backend.apply_staging()` — so lock
acquisition and rename live in one place.

---

## Backend registry

```python
from atomic_agents.memory import register_backend, get_backend

register_backend("sqlite", MySQLiteBackend)
cls = get_backend("sqlite")   # raises BackendNotRegistered if unknown
```

The built-in `"filesystem"` backend is registered automatically on import.

---

## Exceptions

| Exception | When |
|-----------|------|
| `WritePathViolation` | Target path outside allowed `write_paths` |
| `MemoryPreconditionFailed` | SHA-256 precondition mismatch |
| `VersionNotFound` | Version token doesn't resolve to a snapshot |
| `StagingNotApplied` | Operation on already-applied/discarded staging |
| `BackendNotRegistered` | `get_backend(name)` for unknown backend |

---

## Deprecation wrappers

`atomic_agents/_capture.py` and `atomic_agents/_versioning.py` remain as
thin compatibility wrappers. As of #382 PR 1 they delegate through
`get_default_memory_backend` (honouring `ATOMIC_AGENTS_MEMORY_BACKEND`) rather
than always constructing `FilesystemBackend`. The post-write path in
`write_atomic_note` (the returned note path and the `.versions` probe) still
computes filesystem-shaped paths — correct only because the shims are a
documented filesystem-era compatibility surface; non-filesystem backends should
use `agent.memory` directly. They emit `DeprecationWarning`. New code should
use `agent.memory` directly.

---

## Call-site migration reference

| Module | Old pattern | New pattern |
|--------|-------------|-------------|
| `agent.py` captures | direct `write_atomic_note()` | `agent.memory.write_note(c, policy)` |
| `agent.py` index | direct file read | `agent.memory.render_index_summary()` |
| `agent.py` pinned | direct glob + frontmatter | `agent.memory.list_pinned()` |
| `agent.py` recent | direct glob | `agent.memory.list_recent(n)` |
| `dream.py` reads | direct dir scan | `agent.memory.list_notes()`, `read_note()` |
| `dream.py` writes | direct `_write_note_to_dir` | staging via `backend.create_staging()` |
| `dream.py` apply | inline `os.rename` + `AgentLock` | `backend.apply_staging(staged, policy)` |
| `tuning.py` staleness | direct scan | `agent.memory.list_stale(threshold_days)` |
| `dashboard/memory.py` | direct dir scan | `backend.list_notes()`, `stats()` |
| `dashboard/activity.py` | mtime-based glob | `backend.last_mutation_at()` (codex P2 #7) |
| `cli.py:version` | `_versioning.list_versions()` | `agent.memory.list_versions(note)` |
| `cli.py:restore` | `_versioning.restore_version()` | `agent.memory.resolve_version_token()` + `restore_version()` |

---

## Implementer Contract

Every registered `MemoryBackend` implementation MUST satisfy these numbered
requirements. The conformance test suite (`test_memory_protocol_conformance.py`
+ `test_memory_operator_override.py`) provides machine-checkable assertions for
the construction and behavioral MUSTs. Coverage today:

- MUST 1 (uniform construction) and the *storage* half of MUST 2 (`lock_backend`
  is threaded-and-stored on the instance) — directly asserted in
  `test_memory_operator_override.py`, including a registry-conformance test that
  iterates every registered backend and checks the keyword-only `lock_backend`
  signature; that a *supplied* lock_backend instance is the one used is covered
  by the construction-contract signature test in `test_memory_operator_override.py`.
  The *behavioral* half of MUST 2 (`apply_staging` acquires through
  `self._lock_backend`, fail-fast on contention) is exercised by
  `test_dream.py::test_dream_apply_takes_agent_lock`; that test reconstructs
  `runner._backend` without threading a custom `lock_backend` (its lock is
  independently resolved via `get_default_lock_backend()`), so it validates the
  fail-fast-through-self._lock_backend path but does not prove that a *supplied*
  lock_backend instance is the one used.
- MUST 4 (write-4-case) and MUST 5 (`WritePolicy`) — exercised by the
  parameterized conformance fixture.
- MUST 3 (impl identifiability), MUST 6 (atomic writes), and MUST 7 (capability
  advertisement) — review-enforced contracts; there is no backend-agnostic
  conformance assertion in PR 1 (the filesystem reference impl returns
  `supports_semantic_search=False` and is covered by
  `test_memory_filesystem_backend.py`, but the conformance fixture asserts no
  `supports_semantic_search` / `search()` consistency property across backends).

1. **Uniform construction contract.** Every registered `MemoryBackend` MUST
   accept `(agent_root: Path, *, lock_backend=None)` as its construction
   signature — `agent_root` as a positional argument and `lock_backend` as a
   keyword-only argument with `None` default. The factory
   `get_default_memory_backend` dispatches via the registry and calls
   `cls(agent_root, lock_backend=lock_backend)`, so any registered backend
   must satisfy the same call shape. Implementation-specific extras (e.g.,
   `FilesystemBackend.memory_subdir`, a positional-or-keyword parameter with a
   `"memory"` default declared before the `*` marker) are allowed as
   additional parameters with defaults; they are NOT part of the uniform
   contract and MUST NOT be required. `lock_backend` itself MUST be
   keyword-only (after the `*`).

2. **`lock_backend` threading.** When `lock_backend` is supplied and non-None,
   the backend MUST use it for all locking operations (e.g., `apply_staging`
   acquires through the supplied lock backend, not an independently-resolved
   one). This is the mechanism that serialises `apply_staging` against
   `agent.call()` when the operator pins a Redis lock backend.

3. **Impl identifiability.** The backend MUST be identifiable for diagnostics
   across Python processes. Doctor identifies the active backend via
   `type(backend).__name__` (see `check_memory_backend`). A backend MAY also
   advertise a dedicated impl-level identifier, but it MUST NOT reuse the name
   `backend_id` — that name already denotes a note/version handle on
   `NoteRef` / `VersionRef` / `StagedMemory`. (If a stable impl id is wanted
   later, name it `implementation_id`; tracked as a follow-up in #397, not a
   current MUST.)

4. **Write-4-case semantics.** `write_note()` MUST implement the four merge
   cases (merge-into, fresh write, orphan recovery, collision) exactly as
   documented in §"Merge semantics for `write_note()`".

5. **`WritePolicy` enforcement.** Every mutating operation MUST verify the
   target path is under `write_paths` and NOT under `read_only_paths`, raising
   `WritePathViolation` on violation.

6. **Atomic writes.** All writes MUST be atomic (temp-file + fsync + rename or
   equivalent) so that concurrent reads never observe partial state.

7. **Capability advertisement.** If the backend does not support a capability
   (e.g., semantic search), `supports_semantic_search` MUST return `False` and
   `search()` MUST raise `NotImplementedError` or return an empty list
   consistently.

Note: `memory_subdir` and `apply_staging_lock_timeout` are
`FilesystemBackend`-specific extras, NOT part of the Implementer Contract.
Third-party backends MUST NOT be required to accept them.

---

## Operator override surface

Added in #382 PR 1. Mirrors the `LockBackend` (#60 PR 3) / `LogBackend`
(#61 PR 3) override surface, including the `_URL` companion var
(`ATOMIC_AGENTS_MEMORY_BACKEND_URL`) shipped in #258 PR 1 (the first
memory backend that needs a connection URL — `PostgresMemoryBackend`).

### Selection mechanism

Two ways to select the `MemoryBackend` for a deployment:

| Method | Precedence | Use case |
|--------|------------|----------|
| `AtomicAgent(..., memory_backend=my_backend)` | **wins** (kwarg-wins rule) | Programmatic / test injection |
| `ATOMIC_AGENTS_MEMORY_BACKEND=<id>` env var | fallback | Docker, launchd, Cloud Run |

Both are evaluated at `AtomicAgent.__init__` time — the kwarg bypasses the
env var entirely when set.

### `ATOMIC_AGENTS_MEMORY_BACKEND`

- **Default:** `"filesystem"` (unset = filesystem; zero behavior change for
  existing deployments)
- **Value:** a registered backend id string (case-insensitive, stripped)
- **Fail-fast:** unknown ids raise `BackendNotRegistered` at agent
  construction time with the full known-id list in the error message — no
  silent fallback

### `get_default_memory_backend(agent_root, *, lock_backend=None)`

Public factory in `atomic_agents.memory`. Returns a fully-constructed
`MemoryBackend` instance. Callers must retain it; calling the factory twice
returns two separate instances.

Key properties:
- Called lazily from `__init__` / constructor body, never at module import time
- Threads `lock_backend` through to the resolved backend so `apply_staging`
  and `agent.call()` share the same lock backend instance
- Used by `AtomicAgent.__init__`, `DreamRunner.__init__`, tuning context,
  deprecated shims, CLI commands, and dashboard renderers — one selection
  seam, no split-brain

### `lock_backend` threading contract

Every call site that resolves a backend VIA THE FACTORY and holds a resolved
`lock_backend` MUST pass it through:

```python
# agent.py
self.memory = get_default_memory_backend(
    self.agent_root, lock_backend=self.lock_backend
)

# dream.py — DreamRunner.__init__
self._backend = get_default_memory_backend(
    self.agent_root, lock_backend=agent_lock_backend
)
```

Two carve-outs: (1) the `memory_backend=` kwarg path on `AtomicAgent.__init__`
is exempt — the operator owns lock-backend wiring on a backend they built
themselves; the runtime does not re-thread `self.lock_backend` into it. (2)
Standalone callers without an agent instance (deprecated shims, CLI, dashboard)
call `get_default_memory_backend(agent_root)` without a `lock_backend`; the
factory passes `lock_backend=None`, and the `FilesystemBackend` reference impl
then resolves one via `ATOMIC_AGENTS_LOCK_BACKEND` internally. This is a
reference-impl behavior, not a MUST — the Implementer Contract (MUST 2) only
requires a backend to USE a supplied non-`None` `lock_backend`; third-party
backends MAY handle `None` differently.

### Per-runner threading

| Runner | `memory_backend=` kwarg | When None |
|--------|------------------------|-----------|
| `AtomicAgent` | Yes | factory + env var |
| `DreamRunner` | Yes (filesystem only — see caveat) | factory + env var |
| `TuningRunner._build_context` (stores on `AnalysisContext`) | No (routes through factory only) | factory + env var |

DreamRunner caveat. `DreamRunner.apply()` wraps the on-disk dream output
directory as a `FilesystemStagedMemory` and feeds it to `apply_staging()` — a
filesystem-shaped staging path. `DreamRunner.__init__` therefore guards: if the
resolved memory backend is not a `FilesystemBackend` (whether selected via
`ATOMIC_AGENTS_MEMORY_BACKEND` or injected via `memory_backend=`), it raises
`NotImplementedError` rather than breaking silently at apply time. Routing
`apply()` through a backend-agnostic staging-adopt path is tracked in #396.

### Delegate threading

Memory is per-agent **state** (each agent has its own `memory/` directory),
not fleet-shared **config** like persona/corpus. `AtomicAgent.delegate()`
therefore **NEVER** threads the coordinator's memory backend to a child — not
by default, and not even when the operator supplied `memory_backend=`
explicitly. Each delegate resolves its own per-agent backend via the same
process/deployment-global `ATOMIC_AGENTS_MEMORY_BACKEND` selection.

This is the deliberate divergence from the persona/corpus delegate-threading
mirror. For those layers the explicit instance is shared config and sharing it
is correct. For memory, threading a root-bound `FilesystemBackend` would
silently route the specialist's writes into the COORDINATOR's `memory/`
directory — cross-agent state corruption that breaks the per-agent audit trail
(Principle #5). Heterogeneous and shared-memory fleets remain expressible via
the `memory_backend=` kwarg on each agent's own construction, not via
coordinator-to-child threading. Shared-memory delegation, if ever genuinely
wanted, is a new Tier A design fork to escalate, not a corner decided here.

### Bootstrap-paradox note

Memory backend selection is config-tier (T15): the env var is authoritative
because reading the vault config file (`model.md`, `tools.md`) requires a
working runtime, but initialising the runtime requires a backend.

In `AtomicAgent.__init__`, the order is:
1. `lock_backend` resolved
2. `log_backend` resolved
3. other fleet-scoped backends resolved
4. `_load_config()` called — reads `model.md`, `tools.md`, etc. (no memory reads)
5. `memory` resolved via factory

`_load_config()` does NOT read from `memory/`, so there is no paradox at the
current construction order. The env-var-only selection is required by this
ordering constraint: the factory must not read any vault file to determine
which backend to construct.

### Doctor checks

Two checks mirror the `check_lock_backend` / `check_locks` pair:

| Check name | Type | What it verifies |
|------------|------|-----------------|
| `memory-backend-config` | coherence | `ATOMIC_AGENTS_MEMORY_BACKEND` is a known id AND (for non-filesystem ids) constructs |
| `memory-backend` | liveness | factory resolves and `list_notes()` returns (re-raising probe) |

The liveness check (and the non-filesystem branch of the coherence check)
route through `get_default_memory_backend` so doctor's verdict and the
runtime's behavior cannot diverge (doctor-reuses-factory invariant). The
filesystem-default coherence branch short-circuits to PASS without
construction (the default needs no extras).

The liveness check first probes the on-disk `memory/` directory — but this is
a **filesystem-shaped precheck that only runs when the configured backend is
`filesystem`**. When `ATOMIC_AGENTS_MEMORY_BACKEND` names a non-filesystem
backend (the #258 Postgres/pgvector case this seam exists to enable), the
on-disk guard is skipped and the factory + `list_notes()` probe is
authoritative — a healthy non-local backend with no local `memory/` dir must
not spuriously FAIL. The probe calls `list_notes()` (which surfaces an
unrecoverable read as an exception — `MemoryBackendError` on connection-backed
backends such as Postgres, which wrap and re-raise; a raw `OSError` on the
`FilesystemBackend` reference impl, whose `list_notes()` does not wrap reads
today) rather than `stats()`: doctor's liveness gate catches broad `Exception`,
so either surface FAILs the check. `stats()` is not used because
connection-backed backends degrade `stats()` silently to an empty
`MemoryStats` on failure, which would false-PASS a dead backend (the
doctor-dual-probe lesson — the swallowing op hides the error). `stats()` is
still called afterwards, but only for the note count, never as the liveness
gate. Both doctor checks construct the backend through the factory and call
`close()` on it (when the backend exposes one) so a future connection-backed
backend does not leak a connection per doctor run.

---

## Test coverage

- `tests/test_memory_protocol_conformance.py` — 26 named behavioral tests
  (parameterized fixture accepts any `MemoryBackend` implementation)
- `tests/test_memory_filesystem_backend.py` — 10 filesystem-specific tests
  (`.versions/` layout, INDEX.md format, path enforcement, staging lifecycle)
- `tests/test_memory_operator_override.py` — operator override surface tests
  (factory env-var path, kwarg-wins, lock threading, registry helpers,
  uniform construction contract + registry conformance, doctor coherence +
  liveness checks including a registered-backend PASS)

## spec/40 addendum — Canonical export

`MemoryBackend` participates in the **Exportable** companion Protocol (spec/40).

`FilesystemBackend` advertises `supports_canonical_export = True` via the `@property`
idiom (matching `supports_semantic_search`). The `MemoryCapabilities` dataclass
convergence that would unify these two `@property` flags is tracked as issue #431.

`export(query=None)` returns a `MemoryExport` carrying `(Note, raw_bytes)` tuples.
Raw bytes are read directly from disk (Tier A byte-exact fidelity). The `include_versions`
flag in `MemoryExportQuery` is deferred (treated as `False` until issue #433 ships).

For the full normative export contract, see `docs/spec/40-canonical-export.md`.

Future Postgres/pgvector backends: set `supports_canonical_export = True` when
their export impl ships. `PostgresMemoryBackend` (shipped in #258 PR 1) sets
`supports_canonical_export = True` and uses `render_note_bytes_from_object(note)`
for Tier B field-lossless fidelity (not Tier A byte-exact).

---

## PostgresMemoryBackend — Postgres reference implementation

> **NON-NORMATIVE.** This section describes `PostgresMemoryBackend` shipping in
> issue #258 PR 1. It does not amend or supersede any LOCKED MUST in this spec.
> All 7 LOCKED normative MUSTs (MUST 1-7) in the Implementer Contract section
> remain unchanged. This section documents Postgres-specific behavior that is
> PERMITTED by those MUSTs and fills in implementation details that the
> Protocol intentionally leaves to implementers.

### Motivation

`PostgresMemoryBackend` is the first non-filesystem reference impl for
`MemoryBackend`. It targets multi-host deployments (Cloud Run fleet, shared
database, zero local disk dependency) where `FilesystemBackend`'s single-node
file layout is insufficient.

PR 1 ships: FTS (tsvector) recall, Tier B field-lossless export, MUST-1 uniform
construction, multi-thread connection management, and spec/20 conformance.

PR 2/PR 3 (pgvector + `EmbeddingBackend` Protocol #200): deferred.

### Selection

```bash
ATOMIC_AGENTS_MEMORY_BACKEND=postgres
ATOMIC_AGENTS_MEMORY_BACKEND_URL=postgresql://user:pass@host:5432/dbname
```

`get_default_memory_backend()` reads `ATOMIC_AGENTS_MEMORY_BACKEND_URL` from
the environment when `ATOMIC_AGENTS_MEMORY_BACKEND=postgres` is selected.
Missing URL raises `ValueError` at agent construction time (fail-fast, per
spec/20 MUST 1 uniform construction requirement extended to connection-backed
backends). `ATOMIC_AGENTS_MEMORY_BACKEND_URL` is the canonical companion env
var for this backend, mirroring `ATOMIC_AGENTS_LOG_BACKEND_URL`.

### Construction signature

`PostgresMemoryBackend(agent_root, *, lock_backend=None, url=None)` — satisfies
MUST 1. The `url` kwarg (default `None`) is NOT part of the normative MUST 1
signature; it is a Postgres-specific extension. When `url=None`, the backend
reads from `ATOMIC_AGENTS_MEMORY_BACKEND_URL`. The factory
`make_postgres_memory_backend_from_url(url, agent_root, lock_backend)` is the
recommended construction path for programmatic callers.

### WritePolicy enforcement (MUST 5 Postgres interpretation)

spec/20 MUST 5 requires every mutating operation to verify the target is under
`write_paths` and NOT under `read_only_paths`. For a Postgres backend there is
no filesystem path — all notes are SQL row-addressed. The Postgres
interpretation uses `agent_root` as the authorization scope:

- `read_only_paths` is checked FIRST: if `agent_root` falls under any
  `read_only_paths` entry → raise `WritePathViolation` (parity with
  `FilesystemBackend`, which checks read-only before write_paths). The
  `WritePolicy` contract is explicit that `read_only_paths` must not be dropped
  by the abstraction layer; a conforming impl enforces both halves of MUST 5.
- If `write_paths` is empty → raise `WritePathViolation` (no authorized scope).
- If `agent_root` is not under any `write_paths` entry → raise
  `WritePathViolation`.

This is the Postgres-scope equivalent of `FilesystemBackend._enforce_write_path()`.
Path-containment checks on individual SQL rows are not possible; the `agent_root`
scope check is the closest meaningful analog. This behavior is PERMITTED by MUST 5
(which says "verify the target is under `write_paths` and NOT under
`read_only_paths`" without mandating a per-note filesystem path check).

### Schema (independent versioning, `_SCHEMA_VERSION = 2`)

Tables created in `_ensure_schema()` under a `pg_advisory_xact_lock`:

| Table | Purpose |
|-------|---------|
| `memory_notes` | One row per live note. `name TEXT UNIQUE` (derived filename = the row address); `display_name TEXT` (the human note name = `capture.name`, round-trips to `Note.name`). |
| `memory_note_versions` | One row per version snapshot. `note_name TEXT`; `display_name TEXT`. |
| `memory_meta` | Schema version tracking (`key`, `value`). |
| `memory_staging_notes_<uuid>` | Per-staging-session notes (created by `create_staging()`), REGULAR (not Postgres TEMPORARY) tables. |
| `memory_staging_note_versions_<uuid>` | Per-staging-session versions. |

**v1 → v2 migration.** v2 added `display_name` to `memory_notes` and
`memory_note_versions` so the human note name round-trips to `Note.name`
(cross-backend parity with `FilesystemBackend`, which reads `name` from
frontmatter). The migration runs under the advisory lock, before index
creation: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS display_name` (idempotent).
Legacy v1 rows get `display_name = ''` and fall back to the derived filename on
read. Staging tables are regular tables so the apply connection can see staging
rows under the per-thread connection model.

`_SCHEMA_VERSION = 2` is INDEPENDENT of `PostgresLogBackend._SCHEMA_VERSION = 2`.
Both backends can share a Postgres database without table collision (tables are
namespaced: `memory_*` vs `run_records` / `meta`).

Advisory lock key: `struct.unpack('>q', sha256(b'atomic-agents-memory-schema-v1')[:8])`
— DISTINCT from the LogBackend key (`b'atomic-agents-log-schema-v1'`) so memory
and log DDL serialize independently under separate advisory locks.

### FTS search (MUST 7 compliance)

`search()` uses `websearch_to_tsquery('english', %s)` parameterized query over
`to_tsvector('english', COALESCE(display_name,'') || ' ' || COALESCE(name,'') || ' ' || COALESCE(description,'') || ' ' || COALESCE(body,''))`.
(The four columns are `NOT NULL DEFAULT ''`, so the `COALESCE(...,'')` wrappers
are defensive — a bare `col || ' '` would null the whole concatenation if any
column were ever NULL; equivalent here, matched to the impl.)
The query argument is a `%s` placeholder — never string-interpolated (SQL
injection safe). `websearch_to_tsquery` tolerates arbitrary punctuation as
ordinary lexemes; genuine tsquery PARSE failures are caught and return `[]`,
while CONNECTION-level failures propagate to the reconnect-retry layer and, if
unrecoverable, surface as `MemoryBackendError` (consistent with `read_note` /
`list_notes`) rather than masquerading as "no results".

`supports_semantic_search = False`: FTS is NOT semantic/vector search. LOCKED
MUST 7 requires `search()` on a non-semantic backend to "raise
NotImplementedError or return an empty list consistently." Returning FTS
matches instead is the project's own interpretation: it mirrors the LOCKED,
conformance-tested `FilesystemBackend.search()` substring behavior under
`supports_semantic_search = False` (a non-semantic search mode, not vector
search), so it is consistent with the established reference impl rather than a
new spec authority. (A spec-text refinement to MUST 7 to explicitly bless
non-empty non-semantic search is tracked in #530.) Callers
branching on `supports_semantic_search` use this path correctly.

No GIN index on tsvector in v1 (on-the-fly computation). GIN index optimization
is tracked as a follow-up issue.

### `list_orphans()` returns `[]` unconditionally

All notes are primary-key addressable rows. There is no `INDEX.md` concept; a
note cannot exist "on disk" without being in the table. `list_orphans()` returns
`[]` always. `list_orphans()` is a Protocol method, NOT an Implementer-Contract
MUST (the contract enumerates MUST 1-7; orphan detection is not among them). For
a SQL backend orphans are structurally impossible — every note is a primary-key
row, with no on-disk file existing outside an index — so `list_orphans()`
returns `[]` unconditionally.

### VersionRef encoding

`VersionRef.backend_id` for Postgres is the string representation of the
`memory_note_versions` row id (e.g., `"42"`). `resolve_version_token()` accepts
this same row-id string as the CLI token. No `/` separator (unlike
`FilesystemBackend`'s `<stem>/<version_filename>` encoding). `list_versions()`
returns `VersionRef` objects ordered by `snapshotted_at DESC, id DESC`.

### Thread-safety

`threading.local()` gives each thread its own psycopg connection. All
per-thread connections are tracked in a thread-safe `_all_conns` list so
`close()` from the main thread releases worker-thread connections (critical
because `helper_call_parallel()` spawns worker threads that open connections
for memory captures and exit without calling `close()` themselves).

### Credential redaction

Three layers:
1. All logged URLs stripped via `_redact_dsn()`.
2. psycopg opened with explicit keyword args; psycopg logger suppressed.
3. Full URL string NOT retained; only `_safe_url` (redacted) stored.

### Tier B export (spec/40)

`supports_canonical_export = True`. `export()` uses
`render_note_bytes_from_object(note)` from `atomic_agents/export/renderer.py`
for each note. Tier B = field-level round-trip guaranteed; byte-exact round-trip
NOT guaranteed (date formatting and key ordering diverge — see spec/40
§"Tier A vs Tier B fidelity").

`MemoryExportQuery.include_versions=True` is deferred to #433 (same as
`FilesystemBackend`). When a caller passes `include_versions=True`,
`PostgresMemoryBackend.export()` emits a `warnings.warn()` matching the
filesystem message shape — the export contains current-state notes only, NOT
version history — rather than silently ignoring the flag.

### apply_staging — version-history and recovery divergence

`apply_staging()` is a wholesale swap on both reference impls, but the two
diverge in two observable ways (both PERMITTED, documented here so a future
conformance fixture and #396 adopter expect them):

- **Post-swap live version history.** `FilesystemBackend` renames the entire
  live `memory/` dir (including `.versions/`) to `memory.archived-<ts>/` and
  swaps staging in, so live version history = staging-versions only. The
  Postgres path issues an unconditional `DELETE FROM memory_notes` (full live
  note replace) but appends staging versions via `INSERT INTO
  memory_note_versions ... SELECT FROM <staging_versions>` with NO delete of
  prior version rows, so live version history = prior-live-versions ∪
  staging-versions. The rule-5 audit trail itself is intact on both (version
  rows are never destroyed on Postgres; on filesystem they survive in the
  archive dir), so neither violates the audit-trail invariant — they differ
  only in whether the prior note set's versions remain queryable in-place
  after apply.
- **Recovery artifact.** Filesystem leaves a `memory.archived-<ts>/` dir as a
  post-swap recovery artifact on disk; Postgres has no equivalent (rely on
  `memory_note_versions` for history). 

Intra-session staging `write_note` is last-write-wins by derived filename on
BOTH reference impls (Postgres `INSERT ... ON CONFLICT (name) DO UPDATE`;
filesystem plain `atomic_write` over the staging path), with no Case-4
same-filename/different-human-name collision raise inside staging — that guard
lives only in the live `write_note`. This is intentional parity, not a third
divergence.

This divergence is not runtime-reachable today (`DreamRunner.apply()` refuses
non-filesystem backends — see below); reconciling the post-apply version
semantics across backends is tracked with #396.

### DreamRunner integration

`DreamRunner.__init__` raises `NotImplementedError` for non-filesystem backends
(#396 tracks the backend-agnostic adopt path). `create_staging()` and
`apply_staging()` are implemented in `PostgresMemoryBackend` for programmatic
use; `DreamRunner.apply()` cannot call them until #396 ships.

### Exportable Protocol

`PostgresMemoryBackend` satisfies the `Exportable` Protocol (spec/40):
- `export(query=None)` → `MemoryExport(notes_with_bytes, backend_id=implementation_id, scope=str(agent_root))`
  — `MemoryExport.backend_id` is the export envelope's own field; it is sourced
  from the backend's `implementation_id` property (= `"postgres"`).
- `export_all()` → alias for `export(None)`

`isinstance(backend, Exportable)` is `True` at runtime.

### Impl identifier (MUST 3)

Per spec/20 MUST 3, a backend MUST NOT reuse the name `backend_id` for an
impl-level identifier (that name denotes note/version/staging handles).
`PostgresMemoryBackend` exposes `implementation_id` (= `"postgres"`) for that
purpose (#397). (`FilesystemBackend` predates #397 and still exposes
`backend_id="filesystem"`; reconciling the reference impl is tracked in #528 and
is out of scope for the Postgres adapter PR.)

---

### Versioned normative addendum — MemoryCapabilities embedding fields on PgvectorMemoryBackend (spec/20 PR-3 addendum, issue #200 PR3 / #544)

`PgvectorMemoryBackend` is the only MemoryBackend reference implementation that may embed vectors at write-time and at search-time. It exposes two additional fields in its `capabilities()` return value:

**`embedding_provider: str | None`** — a provider LABEL string (e.g. `"openai"`, `"local"`, `"ollama"`), matching the `MemoryCapabilities` dataclass docstring (`atomic_agents/memory/backend.py`) and `CorpusCapabilities.embedding_provider` (spec/34). `PgvectorMemoryBackend` sets it to `self._embedding_backend.provider_id`. It is a provider label, not a model id; the embed cost gate in `agent.call()` reads `model_id` from `embedding_backend_resolved.model_id` (see below) for pricing — this label is never the *preferred* pricing key. Only when no resolved backend is available (`embedding_backend_resolved is None`) does the gate fall back to passing this label to `calc_embedding_cost()`, which — because a bare provider label is not a key in `EMBEDDING_PRICING` — prices it via the max-rate fallback (`cost_estimated=True`): a deliberate conservative last resort that never under-reserves, NOT a preferred path. Non-None when an `EmbeddingBackend` was injected or resolved from the registry at construction. `None` when no embedding backend is configured (`supports_semantic_search=False`; FTS-only mode).

**`embedding_backend_resolved: EmbeddingBackend | None`** — the live `EmbeddingBackend` instance. Non-None when `supports_semantic_search=True`. **SNAPSHOT SECURITY CLAMP:** this field MUST serialize as `None` (or be absent) in any JSONL log record, profile snapshot, or network response. The live instance may carry API credentials (e.g. `OpenAIEmbeddingBackend._api_key`); leaking it into the audit trail violates Principle 5 (audit trail is structural, not a credential store). Callers that need the live backend object MUST read it from the backend's runtime attributes, not from a snapshot.

`MemoryCapabilities` carries exactly two fields — `embedding_provider: str | None` and `embedding_backend_resolved: EmbeddingBackend | None` — and `PgvectorMemoryBackend.capabilities()` is the ONLY reference implementation that returns it today. `FilesystemBackend` and `PostgresMemoryBackend` do NOT implement `capabilities()` at all; they expose only the `supports_semantic_search` boolean `@property` (both return `False`), which is the backward-compatible way to ask "does this backend do semantic recall?". Callers that need a `MemoryCapabilities` surface MUST first gate on `getattr(backend, "supports_semantic_search", False)` (or `hasattr(backend, "capabilities")`) and treat a backend without `capabilities()` as `embedding_provider=None`, `embedding_backend_resolved=None`.

Uniform convergence of `capabilities()` across all MemoryBackend implementations (so callers can rely on a consistent interface) is deferred to issue #431.

Added OUTSIDE the 8-MUST count, following the versioned-addendum precedent of spec/22 §Read-failure contract (#497) and spec/45 PR2 (#520).

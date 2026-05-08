# 20 — MemoryBackend Protocol

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
thin compatibility wrappers that delegate to `FilesystemBackend`. They will
emit `DeprecationWarning` in v1.0. New code should use `agent.memory`
directly.

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

## Test coverage

- `tests/test_memory_protocol_conformance.py` — 26 named behavioral tests
  (parameterized fixture accepts any `MemoryBackend` implementation)
- `tests/test_memory_filesystem_backend.py` — 10 filesystem-specific tests
  (`.versions/` layout, INDEX.md format, path enforcement, staging lifecycle)

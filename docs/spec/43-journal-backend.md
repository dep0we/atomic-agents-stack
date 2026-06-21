# spec/43: JournalBackend Protocol

> **Status:** DRAFT at PR 1 (issue #427). Conformance suite covers all 10 Implementer Contract MUSTs for `FilesystemJournalBackend` (`test_journal_backend_conformance.py`) plus filesystem-specific tests (`test_journal_filesystem.py`). JournalBackend is also registered in the shared #379 export conformance harness (`test_export_protocol_conformance.py`) and the capability-advertisement harness (`test_export_capability_advertisement.py`). The three pre-existing journal consumers (bundle.py `_render_journal_breakpoint`/`_source_paths`, agent.py `_load_recent_journal`, dream.py `_run_pipeline`) are rewired through `JournalBackend` in this PR (ADOPT-NOW ruling). Byte-identity golden tests freeze the divergent render formats (bundle WITH backtick path line, agent WITHOUT — LOAD-BEARING divergence). Migration behavior (journal/ ownership transferred from MemoryBackend) is tested via spec/02 addendum.

---

## Origin

`JournalBackend` is the **sixteenth** backend Protocol in the protocol-pattern series. It abstracts journal entry storage behind a Protocol so the framework's three journal read sites (bundle.py, agent.py, dream.py) share one canonical read path and alternate journal substrates (SQLite, Postgres, append-only log stores) can drop in without forking.

Prior to spec/43, journal I/O was three hand-synced duplicate `rglob` blocks scattered across bundle.py, agent.py, and dream.py. Each block re-implemented the same `sorted(journal_dir.rglob("*.md"), reverse=True)` sort, the same date-window filter, and the same include-on-unparse fallback. Any behavioral fix had to be applied three times.

Filed as [#427](https://github.com/dep0we/atomic-agents-stack/issues/427) as the sixteenth backend protocol, shipping in v2.0.0.

**Cross-links:**

- spec/02. Atomic Memory. journal/ ownership carved out from MemoryBackend's prior claim — updated in PR 1.
- spec/24. AgentProfileBackend. journal/ ownership claim corrected (was MemoryBackend) — updated in PR 1.
- spec/27. Doctor. `check_journal_backend()` uses the dual-probe pattern (list_entries + read_bytes).
- spec/40. Canonical Export. `JournalExport` is a first-class `ExportableResult`; `FilesystemJournalBackend` implements `Exportable`.
- spec/43 completes the v2.0.0 backend arc (alongside GoalBackend spec/41 and OutcomeBackend spec/42).

---

## Shipping plan (1 PR)

**PR 1 (this PR, ADOPT-NOW).** Protocol scaffold + dataclasses + capability advertisement + `FilesystemJournalBackend` reference impl + factory/env/doctor + full ADOPT-NOW read-site wiring (bundle.py, agent.py, dream.py) + byte-identity golden tests + full conformance suite + filesystem-specific tests + spec/43 DRAFT.

The three read sites are wired in the same PR as the Protocol definition (unlike GoalBackend/OutcomeBackend which were SCAFFOLDING-ONLY). The ADOPT-NOW ruling is motivated by the journal's distributed site structure: the three rglob sites are hand-synced duplicates with no single flat module to carve. Leaving them unwired after PR 1 would re-create the maintenance burden spec/43 exists to eliminate.

---

## Overview

`JournalBackend` abstracts journal entry storage for a single agent. The backend is scoped to one agent root — all entries live under `<agent_root>/journal/YYYY-MM/YYYY-MM-DD.md`.

The `FilesystemJournalBackend` reference implementation wraps the same month-bucketed on-disk layout the three legacy rglob callers have used since the framework's first journal support. It adds:

1. **Append atomicity** — exclusive `fcntl.flock` on a `.journal.lock` sidecar serializes concurrent same-day writes (spec/43 MUST 9).
2. **Date-query correctness** — `query_by_date(start, end)` with include-on-unparse fallback (spec/43 MUST 10).
3. **Symlink containment** — `_journal_dir()` raises `PathTraversalError` when `journal/` resolves outside `agent_root` (mirrors `FilesystemOutcomeBackend._runs_root()` pattern; load-bearing per MEMORY.md `feedback_cross_model_catches_same_family_blind_spots`). A resolution **failure** (symlink loop / ELOOP → `RuntimeError`, or an inaccessible ancestor → `OSError`) is folded into the same `PathTraversalError`, so reads return `[]` (fail-soft, matching the legacy `exists()`/`rglob()` graceful-empty behavior) and `append_entry` fails loud. `agent_root.resolve()` is performed lazily inside `_journal_dir()` (deferred from `__init__`) so construction is side-effect-free and never crashes on an unresolvable root — important because the backend is now constructed eagerly in `AtomicAgent.__init__` (ADOPT-NOW live-wiring).
4. **spec/40 export** — `export()` returns a `JournalExport` with portable relative-path strings and raw bytes.

---

## Module layout

```
atomic_agents/journal/
├── __init__.py     # registry: register_journal_backend /
│                   # get_journal_backend / list_journal_backends /
│                   # unregister_journal_backend +
│                   # get_default_journal_backend factory +
│                   # _redact_for_error_message (credential safety)
├── types.py        # canonical types: JournalEntry (frozen), JournalCapabilities
│                   # (frozen), JournalExport (ExportableResult subclass, mutable)
├── backend.py      # JournalBackend Protocol (@runtime_checkable)
└── filesystem.py   # FilesystemJournalBackend reference implementation
```

There is **no** re-export shim: prior to spec/43 there was no public `atomic_agents.journal` export path (journal logic was private helpers in bundle/agent/dream). The Principle #14 shim mandate is conditional-and-void here (verified by grep).

---

## Deliberate divergence: frozen dataclasses

`JournalEntry` and `JournalCapabilities` are **frozen** dataclasses (`@dataclass(frozen=True)`). This is the **inverse** of GoalBackend and OutcomeBackend's mutable-dataclass exception.

**Rationale:** journal entries are append-only immutable episodes. A journal entry, once written, is never mutated through the Protocol. There is no state machine (no `pending → in_progress → complete` lifecycle). The correct analog is `LogEntry` (spec/22, also frozen), not `Goal` (spec/41, mutable due to sub-goal state transitions). The frozen choice is documented in `journal/types.py` with an explicit INVERSE comment so future contributors reading types.py understand the divergence is intentional.

`JournalExport` is mutable (plain `@dataclass`) because it is a container built and returned by `export()`, not a value object passed in by the caller.

---

## WritePolicy applicability

`JournalBackend` is **append-only** — no WritePolicy enum. The journal path is fixed at construction (`journal/` under `agent_root`); the only mutation operation is `append_entry()`, which always appends (never overwrites or merges). Unlike MemoryBackend's `write_note()` (which accepts `WritePolicy.OVERWRITE` / `MERGE`) there is no write-path decision to defer to policy. The conformance suite MUST NOT include a WritePolicy test for `JournalBackend`.

---

## STORAGE-ONLY boundary

`JournalBackend` is a **data layer**. It owns:

- Finding, reading, sorting, date-windowing journal files → raw `JournalEntry` objects.
- Atomic append under exclusive lock.
- Symlink containment.
- spec/40 export.

It does **NOT** own:

- Formatting entries for the system prompt (that stays at each call site).
- Deciding which entries are "relevant" (that stays at bundle/agent/dream).
- Cost-guardrail checks (those stay in `agent.call()`).

The formatting divergence between bundle and agent is **LOAD-BEARING** (byte-identity golden tests freeze both formats):

```python
# bundle._render_journal_breakpoint — WITH path line (LOAD-BEARING, must NOT change):
f"# Journal — {entry.path.stem}\n`{entry.path}`\n\n{entry.text}"

# agent._load_recent_journal — WITHOUT path line (LOAD-BEARING, must NOT change):
f"# Journal — {entry.path.stem}\n\n{entry.text}"
```

These two render formats serve different consumers (bundle renders a full navigation document; agent renders a context-injection snippet). Do **not** unify them.

---

## Protocol surface

```python
@runtime_checkable
class JournalBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    def append_entry(self, text: str, when: date | None = None) -> JournalEntry: ...
    def query_by_date(self, start: date, end: date) -> list[JournalEntry]: ...
    def list_entries(
        self,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[JournalEntry]: ...
    def export(self, query: Any = None) -> JournalExport: ...
    def export_all(self) -> JournalExport: ...
    def capabilities(self) -> JournalCapabilities: ...
```

---

## `JournalEntry` — path is the caller's UN-RESOLVED shape

`JournalEntry.path` carries the caller's **un-resolved** on-disk path — it is `agent_root / "journal" / "YYYY-MM" / "YYYY-MM-DD.md"` with `agent_root` left exactly as the caller passed it. It is absolute when `agent_root` is absolute (e.g. `/agents/caldwell/journal/2026-06/2026-06-12.md`), relative when `agent_root` is relative, and symlink-shaped when `agent_root` is a symlink. The backend **MUST NOT** `.resolve()`/absolutize the path.

This matches all three legacy rglob callers, which rglob'd an **un-resolved** `instance_root / "journal"` (none of them called `.resolve()`), so their emitted paths carried whatever shape `agent_root` had. Resolving here would shift the bundle backtick render line and the `_source_paths`/`_staleness_paths` set (→ cascade `.lease.json` freshness) under a symlinked or relative root — the exact byte-identity regression the #427 PR1 ruling forbids.

> **Known efficiency item (#461).** `_source_paths`/`_staleness_paths` currently call `list_entries(limit=N)`, which reads each selected entry's body and discards it (they only need the Path for mtime-staleness). The legacy code read zero bodies. A path-only enumeration method (`list_entry_paths`) is tracked as a follow-up; selection stays byte-identical either way (pinned by the `_source_paths` selection-parity conformance test).

**Do NOT store `path.name` (filename-only)** — that loses the month-subdir component (`2026-06-12.md` instead of the full path) and breaks:

1. The bundle render's backtick path line, which echoes the full path.
2. Any caller that does `entry.path.read_text()` to re-read the file.

The `dream.py:294` legacy pattern of storing `path.name` was a latent subdir-loss bug that this backend fixes. Callers that need the filename string use `entry.path.name` explicitly; they do not receive `path.name` directly from the backend.

`export()` handles relativization to `agent_root`; the on-dataclass value stays the caller's un-resolved shape.

---

## `FilesystemJournalBackend` on-disk layout

```
<agent_root>/
  journal/
    2026-06/
      2026-06-12.md     ← day file (one per calendar date)
      2026-06-11.md
    2026-05/
      2026-05-31.md
  .journal.lock         ← fcntl.flock sidecar (NOT inside journal/)
```

The `.journal.lock` file lives at **agent_root level** (not inside a month subdir) so all day files across all months are serialized under one lock. `rglob('*.md')` does not match it. Doctor checks MUST NOT treat its presence as corruption.

---

## Atomicity contract (MUST 9)

Under concurrent same-day appends, NO torn or interleaved write; append ordering is preserved. A crash mid-append may lose the in-flight entry but **never corrupts committed entries or interleaves two writers**.

The filesystem implementation serializes the entire read-modify-append sequence under an exclusive `fcntl.flock` on `.journal.lock`:

```
read current file content (under lock)
→ append new entry text
→ atomic_write (temp + fsync + rename)  (under lock)
→ release lock
```

The flock is held across ALL THREE steps. Splitting "read outside lock, acquire lock, write" introduces a lost-update race where both callers read the same stale file and the second write silently clobbers the first.

A Postgres backend would use a SQL transaction with `SELECT … FOR UPDATE` instead of `fcntl.flock` — the atomicity guarantee is on the Protocol, the lock mechanism is a filesystem-impl detail.

---

## Date-query correctness (MUST 10)

`query_by_date(start, end)` returns **exactly** the entries in `[start, end]` (inclusive) by entry date, **plus all entries whose filename stem cannot be parsed as an ISO date** (include-on-unparse fallback).

The include-on-unparse fallback matches `legacy dream._read_journal_entries` exactly:

```python
# legacy dream.py (before spec/43)
except (ValueError, TypeError):
    pass  # include if can't parse date
```

The fallback is purely structural — no additional condition (no mtime check, no clock re-evaluation). An unparseable stem means the file was operator-authored with a non-standard name; the safe choice is to include it rather than silently drop it.

---

## Implementer Contract (10 MUSTs)

All eight base-pattern MUSTs from PersonaBackend (spec/33) apply here, plus two journal-specific MUSTs:

**MUST 1 — charset validation.** `backend_id` MUST contain only `[a-z0-9_-]` characters (no uppercase, no spaces, no path separators). A `backend_id` that fails this check MUST raise `ValueError` at construction.

**MUST 2 — side-effect-free construction.** `__init__` MUST NOT perform filesystem I/O. `list_entries()` MUST return `[]` when `journal/` is absent (new agents). This allows the backend to be constructed unconditionally in `AtomicAgent.__init__` without gating on "agent has journal files."

**MUST 3 — capability honesty.** `capabilities()` MUST return a `JournalCapabilities` where each `supports_*` flag is `True` only when the corresponding method is implemented and behaves per spec. Claiming `supports_canonical_export=True` while returning `JournalExport(entries_with_bytes=[])` on a non-empty journal MUST fail the conformance suite.

**MUST 4 — append write-path (5 cases).**
- New day, new month subdir: creates subdir, creates file, returns entry with full text.
- New day, existing month subdir: creates file, returns entry with full text.
- Existing day (same-date second append): appends to existing file (separated by two newlines), returns entry with full file content (old + separator + new).
- Write failure (OSError, e.g. disk full): MUST propagate the error; MUST NOT leave a partially-written file (atomic_write guarantee).
- Corrupt/unreadable existing day file (the read half of read-modify-append on an existing file raises OSError/UnicodeDecodeError): `append_entry` MUST FAIL LOUD by raising `JournalCorrupted` (a subclass of `AtomicAgentsError`). A lost narrative episode MUST NOT be silently swallowed (Principle #5 — audit trail is structural). This is the INVERSE of the read methods' best-effort behavior: `append_entry` never silently degrades.

**MUST 5 — URL redaction in error messages.** Error messages that echo `ATOMIC_AGENTS_JOURNAL_BACKEND` MUST redact connection strings (strip after `://`, redact `user:pass@host` patterns). The full raw value MUST NOT appear in any log line, `CheckResult.message`, or `CheckResult.detail` field. Mirrors the credential-echo-redaction pattern from goal/outcome/corpus/mcp_registry/secret_backend.

**MUST 6 — storage isolation.** Each `agent_root` is an independent scope. One backend instance MUST NOT read or write entries from another agent_root. `list_entries()`, `query_by_date()`, and `export()` MUST NOT glob above `agent_root / "journal"`.

**MUST 7 — snapshot determinism.** Repeated calls to `list_entries(limit=N, newest_first=True)` with the same `limit` and the same on-disk state MUST return byte-identical entry text and the same `entry.path` values. The sort order MUST be by full `Path` descending (newest first) — NOT by `JournalEntry.date`. Sorting on date alone swaps positions at month boundaries, silently breaking bundle's staleness hash. The conformance suite pins this via the golden-selection tests.

**MUST 8 — backend_id stability.** Once assigned, `backend_id` MUST NOT change across calls. An implementation that returns `"filesystem"` on the first call and `"fs"` on the second MUST fail. `backend_id` MUST be stable for the lifetime of the process. Operator deployments may pin against these strings.

**MUST 9 — append atomicity (journal-specific).** Under concurrent same-day appends, no torn or interleaved write; append ordering is preserved. A crash mid-append may lose the in-flight entry but MUST NOT corrupt committed entries or interleave two writers. The filesystem impl uses `fcntl.flock`; a Postgres impl uses a SQL transaction. The atomicity guarantee is on the Protocol; the mechanism is implementation-specific.

**MUST 10 — date-query correctness incl. include-on-unparse (journal-specific).** `query_by_date(start, end)` MUST return exactly the entries in `[start, end]` (inclusive) by parsed entry date. Files whose stem cannot be parsed as an ISO date (ValueError, TypeError) MUST be INCLUDED regardless of start/end bounds (include-on-unparse fallback). The fallback MUST be purely structural — no additional condition. Unreadable files are silently **skipped** by `query_by_date`: for `OSError` this matches the legacy dream consumer (which read under `except OSError: continue`); for `UnicodeDecodeError` this is a DELIBERATE robustness improvement — legacy dream caught only `OSError`, and a `UnicodeDecodeError` (a `ValueError` subclass, NOT an `OSError`) would have PROPAGATED and crashed the consolidation pipeline, so skipping it here is strictly safer than legacy, not byte-identical to it. Symlinked `.md` entry files are NOT skipped — they are read through exactly as the legacy callers did (`read_text` follows symlinks); only a symlinked `journal/` DIRECTORY escaping `agent_root` is refused (the containment guard).

> **Read-method divergence (byte-identity, #427 PR1).** `query_by_date` SKIPS unreadable files (its only consumer is dream, whose legacy read dropped them). `list_entries` instead **keeps the slot** for an unreadable file, substituting an `errors='replace'` body + warning comment (or a warning-only comment on OSError) — byte-for-byte the legacy bundle `_safe_read_text` behavior. Dropping the slot in `list_entries` would backfill an older entry and shift the newest-N selection AND the staleness path set that drives the cascade `.lease.json` freshness hash. The two read methods therefore differ deliberately in their unreadable-file handling; this is pinned by the golden bundle-render conformance test.
>
> **One deliberate AGENT-path behavior change under ADOPT-NOW.** The agent system-prompt journal path (`agent._load_recent_journal`, feeding BP[14] on every `agent.call()`) now routes through `list_entries()`. Legacy agent code did a bare `p.read_text(encoding='utf-8')` which RAISED `UnicodeDecodeError` on a corrupt entry and had no `OSError` handler at all — a corrupt journal entry could crash prompt assembly. Under spec/43 the agent path inherits `list_entries`' degrade-but-keep behavior, so a corrupt entry now renders a warning-comment slot in BP[14] instead of crashing. This is the **one** deliberate behavior change in the agent path and is intentional — it makes the agent match bundle's long-standing `_safe_read_text` degrade behavior (a non-UTF-8 or unreadable entry degrades gracefully rather than taking down the system prompt). Byte-identity golden tests restrict the strict-equality assertion to UTF-8 entries and separately cover the degrade reference.

**Note on the dream date window.** The legacy `dream._read_journal_entries` applied ONLY a lower bound (no upper bound), so future-dated entries were INCLUDED. The adopted dream call sites therefore call `query_by_date(start=cutoff, end=date.max)` to preserve that selection byte-for-byte. `query_by_date` itself enforces whatever inclusive `[start, end]` bounds it is given; the open-ended upper bound is the caller's choice, not a backend special-case.

---

## Conformance suite coverage

`test_journal_backend_conformance.py` (60 tests — a parametrized conformance core (MUST 1–10) plus filesystem-impl-specific doctor, golden-render byte-identity, `_source_paths`/`_staleness_paths` selection-parity, and call-site-adoption tests that drive the real bundle/agent/dream functions):

| Test group | MUSTs covered |
|---|---|
| `test_journal_backend_id_stable` | MUST 8 |
| `test_journal_backend_id_format` | MUST 1 |
| `test_journal_construction_side_effect_free` | MUST 2 |
| `test_journal_list_entries_absent_journal` | MUST 2 |
| `test_journal_capabilities_field_types_are_bool` | MUST 3 |
| `test_journal_append_new_day_new_month` | MUST 4 |
| `test_journal_append_same_day_twice` | MUST 4 (existing day file, in-place append of full content) |
| `test_journal_append_corrupt_existing_raises_journal_corrupted` | MUST 4 (fail-loud JournalCorrupted) |
| `test_journal_list_entries_path_descending` | MUST 7 |
| `test_journal_list_entries_limit` | MUST 7 |
| `test_journal_list_entries_golden_selection` | MUST 7 |
| `test_journal_query_by_date_in_range` | MUST 10 |
| `test_journal_query_by_date_exclude_outside` | MUST 10 |
| `test_journal_query_by_date_include_on_unparse` | MUST 10 |
| `test_journal_storage_isolation` | MUST 6 |
| `test_journal_capabilities_returns_type` / `test_journal_capabilities_defaults_false` / `test_journal_filesystem_supports_canonical_export` | MUST 3 (capability honesty) |
| `test_journal_export_bytes_identical` | MUST 3, MUST 7 |
| `test_journal_export_empty_when_absent` | MUST 2, MUST 3 |
| `test_journal_append_returns_unresolved_path` | MUST 4 (returned entry path/content shape) |
| `test_redact_url` / `test_redact_dsn` / `test_redact_truncation` / `test_redact_passthrough_short` | MUST 5 (credential redaction in error messages) |
| `test_journal_backend_is_runtime_checkable` | structural |
| `test_journal_entry_is_frozen` | frozen-dataclass invariant |
| `test_journal_entry_path_unresolved` | un-resolved-shape invariant (path mirrors the caller's agent_root shape; relative root → relative path) |
| `test_journal_concurrent_append_no_torn_write` | MUST 9 |

`test_journal_filesystem.py` (20 filesystem-specific tests):

| Test | What it pins |
|---|---|
| `test_filesystem_journal_lock_path` | .journal.lock at agent_root level |
| `test_filesystem_journal_lock_absent_ok` | lock file created on first write |
| `test_filesystem_journal_lock_not_matched_by_rglob` | .journal.lock never selected by rglob('*.md') |
| `test_filesystem_journal_month_subdir` | YYYY-MM/ subdir minting |
| `test_filesystem_journal_cross_month_separate_subdirs` | cross-month appends land in separate YYYY-MM/ subdirs |
| `test_filesystem_journal_append_new_day_existing_month` | MUST 4: new day file into an already-minted YYYY-MM/ subdir (second date, existing month) |
| `test_filesystem_journal_append_write_failure_propagates` | MUST 4: atomic_write OSError → append_entry propagates, no partial day file |
| `test_filesystem_journal_symlinked_entry_selected_list` | symlinked .md entry SELECTED (list_entries follows symlinks, byte-identity) |
| `test_filesystem_journal_symlinked_entry_selected_query` | symlinked .md entry SELECTED (query_by_date follows symlinks, byte-identity) |
| `test_filesystem_journal_symlink_containment_list_returns_empty` | symlinked journal/ DIRECTORY escape → list_entries returns [] |
| `test_filesystem_journal_symlink_containment_append_raises` | symlinked journal/ DIRECTORY escape → append_entry raises PathTraversalError |
| `test_filesystem_journal_symlink_loop_list_returns_empty` | symlink-LOOP journal/ (ELOOP on resolve) → list_entries returns [] (graceful-empty, not crash) |
| `test_filesystem_journal_symlink_loop_query_returns_empty` | symlink-LOOP journal/ (ELOOP on resolve) → query_by_date returns [] |
| `test_filesystem_journal_symlink_loop_append_raises` | symlink-LOOP journal/ (ELOOP on resolve) → append_entry fails loud (PathTraversalError) |
| `test_filesystem_journal_unresolvable_agent_root_constructs` | unresolvable agent_root ancestor → construction does NOT crash (resolve deferred); reads return [], append fails loud |
| `test_filesystem_journal_tmp_excluded` | *.tmp sidecars not in list_entries |
| `test_filesystem_journal_list_keeps_unreadable_oserror_slot` | degrade-but-keep (list_entries) + skip (query_by_date): OSError |
| `test_filesystem_journal_list_keeps_bad_unicode_slot` | degrade-but-keep (list_entries) + skip (query_by_date): UnicodeDecodeError |
| `test_filesystem_journal_export_relative_paths` | relativization in export() |
| `test_filesystem_journal_dotdot_rejected` | `..` in agent_root rejected |

**Golden render tests** (pinning the LOAD-BEARING bundle vs agent format divergence):

| Test | What it pins |
|---|---|
| `test_bundle_render_journal_byte_identical_to_legacy` | bundle format WITH backtick path line, byte-identical to legacy |
| `test_agent_render_journal_no_path_line` | agent format WITHOUT path line |
| `test_dream_window_includes_future_dated_entry` | dream dict adapter + lower-bound-only window, byte-identical to legacy |
| `test_bundle_render_symlinked_entry_byte_identical` | symlinked `.md` entry SELECTED (bundle follows symlinks, byte-identity) |
| `test_dream_window_includes_symlinked_entry` | symlinked `.md` entry SELECTED in dream window (byte-identity) |

---

## Doctor check

`check_journal_backend(agent_root)` follows the dual-probe pattern (MEMORY.md `feedback_doctor_dual_probe_pattern`):

- **Probe 1 (lightweight):** `backend.list_entries(limit=1, newest_first=True)` — MUST NOT raise even when `journal/` is absent.
- **Probe 2 (heavy):** `entry.path.read_bytes()` on the first returned entry — only when probe 1 returns at least one entry. There is **no** per-entry symlink-containment re-check: the ADOPT-NOW byte-identity ruling requires `list_entries()`/`query_by_date()` to FOLLOW individual symlinked `.md` entries exactly as the legacy rglob callers did, so the runtime reads such an entry through; doctor agrees with that runtime contract rather than FAIL on what the runtime reads (`feedback_doctor_dual_probe_pattern`: doctor's verdict and runtime behavior cannot disagree).
- **Directory-escape probe:** the real escape vector — a symlinked `journal/` DIRECTORY pointing outside `agent_root` — does **NOT** surface via `list_entries()`: the backend's `_journal_dir()` raises `PathTraversalError`, but `list_entries()` CATCHES it and returns `[]` (an absent journal), which would silently PASS the operator's vault even though every runtime read drops the ENTIRE journal. Doctor therefore probes the escape vector DIRECTLY — when the backend exposes a `_journal_dir()` helper (`hasattr`-guarded so non-filesystem backends are unaffected), doctor calls it and FAILs on `PathTraversalError`. This is the genuine misconfiguration class doctor exists to catch, and is distinct from the per-entry symlink case above.

PASS / FAIL ladder:

| Condition | Result |
|---|---|
| `get_default_journal_backend(agent_root)` raises | FAIL (bad env or unregistered backend) |
| `list_entries()` raises | FAIL |
| Symlinked `journal/` DIRECTORY resolving outside `agent_root` | FAIL (direct `_journal_dir()` probe — `list_entries()` itself returns `[]` here, so doctor probes the directory directly) |
| Entries present AND an individual `.md` entry is a symlink resolving outside `agent_root` | PASS (deliberately followed-through, byte-identity with legacy rglob callers; only a DIRECTORY escape is refused) |
| Entries present AND `read_bytes()` raises `FileNotFoundError` | PASS (benign TOCTOU — entry vanished between probes; concurrent cleanup) |
| Entries present AND `read_bytes()` raises `PermissionError` or any other error | FAIL (genuinely unreadable existing entry) |
| No entries (journal/ absent or empty) | PASS with `journal_entries_found=0` |
| All probes pass | PASS |

detail dict keys: `backend_id`, `journal_entries_found`, `read_bytes_probed`, `supports_canonical_export`, `supports_date_query`.

---

## spec/40 round-trip export

`JournalExport` is registered in:

- `test_export_protocol_conformance.py` — verifies `FilesystemJournalBackend` satisfies the `Exportable` protocol (implements `export()`, returns `ExportableResult` subclass).
- `test_export_capability_advertisement.py` — verifies `supports_canonical_export=True` claim matches actual export behavior.

Snapshot consistency bound: `export()` enumerates entries first (via `list_entries`), then reads each file for bytes. A concurrent `append_entry()` completing between enumeration and the read of a specific entry may cause bytes to reflect the post-append state for that day file. Callers requiring strict point-in-time consistency MUST hold the agent LockBackend before calling `export()`. This is the acknowledged spec/40 MUST 7 snapshot-consistency bound (same language as `GoalExport`'s documented bound).

---

## spec/02 ownership reconciliation

Prior to spec/43, spec/02 stated:

> "MemoryBackend (spec/20) retains exclusive ownership of memory/ and journal/"

This claim was **incorrect** as of the legacy codebase (journal/ was populated by bundle/agent/dream independently of MemoryBackend) and is corrected in PR 1:

> "MemoryBackend retains exclusive ownership of memory/ only. As of spec/43, journal/ is carved out to JournalBackend (spec/43)."

spec/24 (AgentProfileBackend) carried the same stale claim and is corrected in the same PR.

---

## Migration note

Existing vault operators: no migration required. `FilesystemJournalBackend` reads the same `journal/YYYY-MM/YYYY-MM-DD.md` layout that bundle/agent/dream have always written. The only new file is `.journal.lock` (created on first `append_entry()` call; harmless if absent). No data format changed.

Alternate-backend operators: set `ATOMIC_AGENTS_JOURNAL_BACKEND=<backend_id>` and register the backend class before constructing an `AtomicAgent`. The factory reads the env var; `FilesystemJournalBackend` is the default.

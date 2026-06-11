# 03 — File Formats

Exact frontmatter schemas, naming conventions, and content rules for every Atomic Agent file.

---

## Naming conventions

| File | Pattern | Examples |
|---|---|---|
| Persona files | `UPPERCASE.md` | `IDENTITY.md`, `SOUL.md`, `USER.md` |
| Operational files | `lowercase.md` | `tools.md`, `model.md` |
| Atomic Notes (evergreen) | `{type}_{topic}.md` | `feedback_debt_priority_order.md`, `user_risk_tolerance.md` |
| Atomic Notes (time-bounded) | `{type}_{YYYY[-Q#]}_{topic}.md` | `decision_2026-q3_income_target.md`, `project_2026_april_consulting_launch.md` |
| Atomic Wiki pages (evergreen) | `{topic}.md` | `debt_payoff_methods.md`, `credit_score_mechanics.md` |
| Atomic Wiki pages (versioned) | `{topic}_{YYYY}.md` | `tax_strategy_2026.md`, `tax_strategy_2027.md` |
| Indexes | `INDEX.md` (top), `INDEX_{slice}.md` (sub) | `INDEX.md`, `INDEX_decisions.md` |
| Journal | `YYYY-MM-DD.md` under `YYYY-MM/` | `2026-05/2026-05-06.md` |
| Logs | `YYYY-MM-DD.jsonl` under `YYYY-MM/` | `2026-05/2026-05-06.jsonl` |
| `raw/` source docs | preserve original filename or use ingest-date prefix | `2026-04-22_cpa_meeting.md`, `tax_planning_2026.pdf` |

### When to include a date in the filename

Codex review (finding #28) and operator practice surfaced this: **time-bounded content benefits from date-suffixed filenames; evergreen content does not**. The rule:

| Memory type | Time-bounded? | Date in filename? |
|---|---|---|
| `feedback_*` | usually no — behavioral preferences are persistent | NO |
| `user_*` | usually no — facts about the operator are persistent | NO |
| `reference_*` | usually no — pointers to systems persist | NO |
| `decision_*` | OFTEN yes — locked choices have validity windows | YES if validity scope is clear (Q3 2026, 2026, etc.) |
| `project_*` | OFTEN yes — projects have deadlines and success criteria | YES if the project is bounded by year/quarter |
| Wiki pages | sometimes — knowledge sometimes versions | YES for content that changes annually (tax law, regulations); NO for stable concepts |

The rationale: `decision_q3_income_target.md` is ambiguous when Q3 2027 rolls around with a new Q3 target. `decision_2026-q3_income_target.md` makes the validity scope explicit in the filename, allowing both files to coexist without confusion.

Same logic for projects: `project_side_venture_launch.md` becomes ambiguous if the operator launches a second venture later. `project_2026_april_consulting_launch.md` is clearly the 2026 launch specifically.

### Migration for existing files

For agents already using undated filenames on time-bounded content, migration is opt-in:
1. The frontmatter `expires_at` field already encodes validity scope
2. Filename rename is a stylistic improvement; not required for correctness
3. If you rename, also update INDEX.md entries and any cross-references

The schema migration framework (per [../spec/03-file-formats#schema-migration](../spec/03-file-formats.md#schema-migration)) does NOT auto-rename files. Filename conventions are operator-style choices, not schema requirements. Spec/03 documents the recommended pattern; doesn't enforce it.

### Evergreen content stays undated

To be clear: `feedback_debt_priority_order.md` should NOT become `feedback_2026_debt_priority_order.md`. The behavioral preference doesn't have a 2026 validity window — it's how the operator thinks about debt, period. Date suffixes only apply when the *content's validity* is genuinely time-bounded.

**Topic naming**: lowercase, snake_case. Be specific enough to be unique without context — `feedback_debt_priority_order` not `feedback_debt`. Topic should be 2-5 words; longer than that, restructure into sub-notes with a parent index entry.

---

## Atomic Note frontmatter schema

```yaml
---
schema_version: 1
name: <human-readable title, also used in INDEX>
description: <one-line hook, used in INDEX, ~100 chars>
type: <user | feedback | project | decision | reference>
captured: <YYYY-MM-DD>
last_seen: <YYYY-MM-DD>
sources:
  - <pointer: conversation_id, doc_path, observation>
confidence: <high | medium | low>
pinned: <true | false>          # optional, default false
expires_at: <YYYY-MM-DD | null> # optional, default null
supersedes: <filename | null>   # optional
superseded_by: <filename | null># optional
tags: [<tag1>, <tag2>]          # optional, free-form
---
```

### Field definitions

**`schema_version`** *(required, integer)*
Format version. Currently `1`. Bump when frontmatter schema changes; old files get migrated explicitly.

**`name`** *(required, string)*
Human-readable title. Will appear in INDEX. ~80 chars max.

**`description`** *(required, string)*
One-line hook explaining when this memory matters. Will appear in INDEX. ~150 chars max.

**`type`** *(required, enum)*
Locked taxonomy:
- `user` — about the operator (their preferences, role, context)
- `feedback` — corrections + validated approaches (how to behave)
- `project` — active work state (in-flight initiatives, blockers)
- `decision` — locked architectural / strategic choices
- `reference` — pointers to external systems / docs / tools

These five are the spec. Adding new types requires a spec bump.

**`captured`** *(required, date)*
When the memory was first written. ISO format `YYYY-MM-DD`.

**`last_seen`** *(required, date)*
When the memory was last confirmed or referenced. Updated when the agent re-encounters confirming evidence. Used for staleness detection.

**`sources`** *(required, array of strings)*
Where this memory came from. Pointers to:
- `conversation_<id_or_date>` for in-conversation captures
- `journal/<path>` for memory promoted from journal entries
- `<file_path>` for memory derived from documents
- `observation` for inferred memory (lower confidence by default)

**`confidence`** *(required, enum)*
- `high` — locked, confirmed, won't change without explicit user action
- `medium` — confident but not bedrock; could revise with new info
- `low` — tentative, single-source, or inferred

Used by lint pass and conflict resolution.

**`pinned`** *(optional, boolean, default `false`)*
If `true`, always loaded into the system prompt regardless of selection. Use sparingly — every pinned memory is a tax on the always-loaded budget. Reserve for things the agent must never forget (e.g., hard scope boundaries).

**`expires_at`** *(optional, date or null, default `null`)*
If set, the memory becomes archive-candidate after this date. Use for time-bound memories like "Q3 2026 income target" — naturally stale after Q3.

**`supersedes`** *(optional, string or null, default `null`)*
Filename of an older memory this one replaces. Non-destructive — both files stay; this field links them.

**`superseded_by`** *(optional, string or null, default `null`)*
Filename of a newer memory that replaces this one. Set when this memory becomes outdated. Loader can choose to skip superseded memories.

**`tags`** *(optional, array of strings)*
Free-form tags for grouping. Used by lint and search. Not part of the load mechanism.

---

## Atomic Wiki page frontmatter schema

```yaml
---
schema_version: 1
name: <page title>
description: <one-line hook>
type: wiki_page
captured: <YYYY-MM-DD>          # when first compiled
last_seen: <YYYY-MM-DD>          # when last refreshed/recompiled
sources:
  - raw/<source_doc_filename>
provenance: distilled            # always 'distilled' for wiki pages
confidence: <high | medium | low># grounded in source quality
pinned: <true | false>
related:
  - <other_wiki_page.md>
  - <atomic_note.md>             # cross-layer links allowed
expires_at: <YYYY-MM-DD | null>
supersedes: <filename | null>
superseded_by: <filename | null>
tags: [<tag1>]
---
```

**Differences from Atomic Note**:
- `type: wiki_page` (always, not from the note taxonomy)
- `provenance: distilled` (always — wiki pages are derivative)
- `related: []` (Karpathy-style backlinks; cross-layer links to atomic notes are valid)
- `sources` always points to `raw/` documents, not conversations

---

## INDEX.md format

Plain markdown with sections. Loadable as-is into context.

```markdown
# {Agent Name} — Memory Index

## Critical Feedback
- `Title` — one-line hook
- `Title` — one-line hook

## Locked Decisions
- `Title` — one-line hook

## User Profile
- `Title` — one-line hook

## Active Projects
- `Title` — one-line hook

## Reference
- `Title` — one-line hook

## Recently Promoted to Persona
- `Title` — promoted YYYY-MM-DD from feedback_*.md

## Archive (superseded)
- `Title` — superseded by newer.md, YYYY-MM-DD
```

**Rules**:
- Sectioned by type
- Each entry is one line: ``Title` — hook`
- Hook is the file's `description` field (or a manually-edited shorter version)
- Stay under ~150 lines total before splitting into sub-indexes

**Hand-edits welcome.** The INDEX is the agent's view of its memory. Curation by the operator is encouraged.

---

## Persona file structure

Persona files are *not* frontmatter-tagged the same way memories are. They're free-form markdown with conventional sections.

### IDENTITY.md skeleton

```markdown
# IDENTITY — {Agent Name}

## Who I am
<one paragraph>

## Mission
<one sentence — what the agent is optimized to do>

## Scope
<bullets — what's in scope, what's out of scope>

## Operating doctrine
<bullets — the principles that shape judgment>

## Autonomy ladder
- Internal: <what the agent does without asking>
- External: <what requires explicit approval>
- Earned autonomy: <what becomes autonomous after N approved instances>

## What I'm NOT
<bullets — explicit boundaries>
```

### SOUL.md skeleton

```markdown
# SOUL — {Agent Name}

## Voice
<one paragraph — communication style>

## Posture
<bullets — emotional/relational stance>

## Evolution discipline
<bullets — meta-rules about how SOUL itself grows>

## Things I've learned about how to advise / serve the operator
<accumulating list — this is the section that evolves over time>
```

### USER.md skeleton

```markdown
# USER — the operator

## Role and context
<bullets>

## Communication preferences
<bullets>

## Domain-specific preferences
<bullets — slice relevant to this agent's job>

## Things to avoid
<bullets — hard nos>
```

---

## tools.md structure

```markdown
# TOOLS — {Agent Name}

## Read paths
- <absolute path>

## Write paths (own folder ONLY)
- <absolute path>

## External APIs
- <API name>: <what for, key location>

## Hard NOs
- <action>
```

---

## model.md structure

```markdown
# MODEL — {Agent Name}

## Default model
<full model ID, e.g., claude-opus-4-7-20260101>
(reason for choice)

## Fallback
<full model ID>
(when fallback fires)

## Token budget
- Max system prompt: <N> tokens
- Max output per turn: <N> tokens
- Daily token cap: <N>

## Prompt caching
<strategy notes>

## Cost guardrail
<what happens when daily cap hit>
```

---

## Journal entry format

Free-form markdown. Light convention:

```markdown
# YYYY-MM-DD — {Agent Name} journal

## What happened
<narrative>

## Decisions made
<if any>

## Captured to memory
- <atomic note filename> — <one-line summary>

## Open questions
<if any>
```

The "Captured to memory" section is the bridge between the episodic journal and the semantic memory layer.

---

## Log entry format (JSONL)

One JSON object per line. Required fields:

```json
{
  "ts": "ISO 8601 timestamp with timezone",
  "trigger": "cron | skill | api | manual",
  "model": "model ID used",
  "input_tokens": <int>,
  "output_tokens": <int>,
  "status": "ok | error | skipped",
  "summary": "<short string, one line>"
}
```

Optional fields: `error`, `cost_usd`, `cache_hit`, `tools_called`, `skill_invocation_id`.

**Why JSONL**: queryable with `jq`, append-only, parseable by any tool. Markdown for narrative, JSON for observability.

---

## Required fields summary

For every memory file, the **bare minimum frontmatter** is:

```yaml
---
schema_version: 1
name: <required>
description: <required>
type: <required>
captured: <required>
last_seen: <required>
sources: [<required, can be ['observation']>]
confidence: <required>
---
```

Everything else is optional. The loader treats missing optional fields as their defaults.

---

## Validation

When the agent or a tool writes a new atomic unit, it should validate:

1. ✅ All required frontmatter fields present
2. ✅ `type` is in the locked taxonomy
3. ✅ `confidence` is in `{high, medium, low}`
4. ✅ Dates are valid `YYYY-MM-DD`
5. ✅ Filename matches `{type}_{topic}.md` pattern
6. ✅ INDEX.md has been updated to reference the new file

A simple Python validator lives in [../implementation/shared-helper](../implementation/shared-helper.md). Failed validations should block the write; surface to the operator with the specific field that failed.

---

## Schema migration

> **Status:** LOCKED (§Schema-migration subsection re-locked at issue #429 — T13 backend-shaped refactor)
>
> **BREAKING change (issue #429):** The old path-shaped migration script contract (`applies_to(path: Path)` / `migrate(path: Path, dry_run: bool)`) is removed. Operator-authored migration scripts MUST be rewritten to the new per-unit handle contract. See §Migration script format and §Migration upgrade path below.

The `schema_version: 1` field on every atomic unit isn't decoration — it's the contract that lets the format evolve without silently corrupting old data. Codex review (finding #13) flagged that the spec named the field but didn't specify the migration mechanics. Issue #429 (T13) refactored the runner from a path-shaped free function into a `MigrationBackend` Protocol so future database backends can satisfy the same contract without forking the runner.

### What triggers a schema bump

A schema bump (`1 → 2`) happens when ANY of:

- A required field is added (existing files won't have it)
- A required field is removed
- A field is renamed
- A field's type changes (string → list, date → ISO timestamp, etc.)
- A field's allowed values change (enum tightened/expanded)

Adding *optional* fields with sensible defaults does NOT require a bump — old files default to "field absent."

### Migration directory

```
<agents_root>/_migrations/
├── v1_to_v2.py                  ← future migration (current → next)
├── v2_to_v3.py
└── README.md                    ← human-readable migration history
```

> **Legacy (pre-`schema_version`) files.** Files written before the
> `schema_version` field existed carry no integer version. `read_schema_version()`
> only considers units that yield an integer `schema_version`, so an all-legacy
> vault reports `CURRENT_SCHEMA_VERSION` and the runner finds nothing to migrate
> (forward-only, target-above-current). Driving a true `v0 → v1` pass therefore
> requires treating a missing version as `0`; that is tracked as a follow-up
> (issue #439) and is out of scope for the #429 refactor, which ports the
> existing behavior verbatim.

Migration scripts live under `<agents_root>/_migrations/`, not per-agent — the schema is global; one migration applies to every agent's files.

### Migration script format

Scripts implement two functions: `applies_to(unit)` and `migrate(unit)`. Both receive a `MigratableUnit` handle — **not** a raw `Path`. This makes scripts backend-neutral: the same script works over the filesystem backend or a future database backend.

```python
"""Migrate atomic memory frontmatter from schema_version 1 → 2.

What changes:
- New required field: `provenance`
- Renamed: `sources` → `evidence`
- All existing v1 files get `provenance: "v1_migrated"` and copy `sources` to `evidence`.
"""
from atomic_agents.migration import MigratableUnit

FROM_VERSION = 1
TO_VERSION = 2


def applies_to(unit: MigratableUnit) -> bool:
    """Should this script touch this unit?

    Use unit.unit_type to branch on memory notes vs wiki pages.
    Use unit.read_frontmatter() to inspect current fields.
    """
    meta = unit.read_frontmatter()
    return meta.get("schema_version") == FROM_VERSION


def migrate(unit: MigratableUnit) -> dict:
    """Apply the migration to one unit. Returns a summary of changes.

    Note: no `dry_run` parameter — the MigratableUnit handle has dry_run
    baked in. unit.write_frontmatter() is automatically a no-op when the
    runner constructed the handle in dry-run mode. Scripts MUST always
    call unit.write_frontmatter() unconditionally.
    """
    meta = unit.read_frontmatter()
    changes = []

    # Bump version
    meta["schema_version"] = TO_VERSION
    changes.append("schema_version 1 → 2")

    # Rename sources → evidence
    if "sources" in meta:
        meta["evidence"] = meta.pop("sources")
        changes.append("renamed sources → evidence")

    # Add provenance
    if "provenance" not in meta:
        meta["provenance"] = "v1_migrated"
        changes.append("added provenance: v1_migrated")

    unit.write_frontmatter(meta)   # no-op on dry-run handles
    return {"unit_id": unit.unit_id, "changes": changes}
```

**Return-value contract.** The normal — and on a real run, the *only safe* — way to skip a unit is to return `False` from `applies_to(unit)`. When `applies_to(unit)` returns `True`, the runner treats the unit as one the script intends to migrate:

- **On a real (non-dry-run) run**, `apply_unit()` verifies the unit's `schema_version` was bumped to `script.to_version` after `migrate()` returns (MUST 7). A script whose `applies_to(unit)` returned `True` but that then returns falsy from `migrate()` *without* bumping the version raises `AtomicAgentsError` and triggers full rollback — fail-loud, by design. There is no "examined but made no change" escape hatch once `applies_to()` has claimed the unit on a real run; gate the decision in `applies_to()`.
- **On a dry-run**, no version bump occurs (writes are no-ops), so the version check is skipped. A falsy return from `migrate()` (`None` or `{}`) is the *skip* signal — the runner counts that unit toward `units_skipped`, not `units_touched`. The runner MUST decide skip on the script's raw return value, BEFORE injecting any provenance keys (`script`, `unit_id`), so an empty summary stays falsy and is tallied as a skip rather than a touch.

A script that mutates a unit MUST return a **non-empty** dict, otherwise the run's `units_touched` / `units_skipped` tally — a load-bearing audit artifact — is silently wrong on dry-run, and the version-bump check fails the run on a real run.

#### MigratableUnit handle

| Attribute / Method | Description |
|---|---|
| `unit.unit_id` | Opaque stable identifier (filesystem: absolute path string). Use for log messages; do not parse. |
| `unit.unit_type` | `"memory"` for atomic notes, `"wiki"` for wiki pages. Use to branch on unit kind without path inspection. |
| `unit.dry_run` | `True` when the runner is in dry-run mode (baked in at handle construction). |
| `unit.read_frontmatter()` | Returns current frontmatter as a `dict`. Returns a copy — mutation does not persist. |
| `unit.write_frontmatter(meta)` | Writes updated frontmatter atomically (temp + fsync + rename). No-op when `dry_run=True`. |

Scripts MUST NOT receive a raw `Path` — that was the old path-shaped contract and is now removed.

### MigrationBackend Protocol

`MigrationBackend` is the orchestration primitive (`atomic_agents.migration.MigrationBackend`). The runner takes a `MigrationBackend` argument; the default is `FilesystemMigrationBackend(agents_root)`.

#### Implementer Contract (LOCKED)

All implementations of `MigrationBackend` MUST satisfy the following:

**MUST 1 — `read_schema_version()` is read-only and vault-wide.**
`read_schema_version()` MUST return the minimum `schema_version` observed across ALL enumerable units. There is NO `write_schema_version()` method. Version bumps happen exclusively as a side effect of `apply_unit()` writing per-unit frontmatter via `unit.write_frontmatter()` (Principle #1: vault is the source of truth). An **empty vault** (no enumerable units) MUST return `_schema.CURRENT_SCHEMA_VERSION`. A **non-empty vault where no unit yields a valid integer `schema_version`** (every unit corrupt/unparseable) MUST also return `CURRENT_SCHEMA_VERSION` — treated as "no safe forward migration available" — and SHOULD surface a diagnostic so the corruption is not silently masked as already-current.

**MUST 2 — `enumerate_units()` deduplicates by resolved id.**
`enumerate_units()` MUST return each physical unit exactly once even when symlinks or joins could produce duplicate rows. Every returned `MigratableUnit` MUST have `unit_type` set to `"memory"` or `"wiki"`. Non-content sidecars that carry no `schema_version` — specifically the recall `INDEX.md` (spec/02) — are NOT migratable units and MUST be excluded from enumeration, so cross-backend unit-set semantics (and the `read_schema_version()` minimum + content-file count derived from them) stay aligned.

**MUST 3 — `apply_unit()` writes atomically.**
Implementations of `write_frontmatter()` inside `MigratableUnit` MUST write atomically (temp + fsync + rename, per `_io.atomic_write`). Partial writes that leave the unit in a corrupt state are a spec violation.

**MUST 4 — `snapshot()` / `restore()` are full protocol methods.**
`snapshot(target_version)` MUST create a recoverable checkpoint before any `apply_unit()` call. `restore(ref)` MUST atomically return the vault to the pre-migration state. The runner MUST emit the `MigrationSnapshotRef` to STDOUT immediately after `snapshot()` returns, before any write, so operators can recover manually if the process is killed mid-migration. The `target_version` argument is the version the migration is heading TO; the filesystem backend uses it to label the artifact `..._pre_v{target_version}_migration.tar.gz` (a backend with no human-facing snapshot name MAY ignore it).

The `MigrationCapabilities.single_host_only` flag is advisory: `True` (the filesystem default) declares the snapshot/restore atomicity guarantee holds only on a single host (snapshots on local disk, serialized by the vault-level lock). A future distributed backend replicating snapshots under a cross-host lock would set it `False`. The runner does not gate on it today; it informs operator deployment-topology choices.

**MUST 5 — Runner MUST fail-close on no-rollback backend.**
Before the first `apply_unit()` call on a real (non-dry-run) migration, the runner MUST check `backend.capabilities().supports_transactional_rollback`. If `False`, the runner MUST raise `MigrationRollbackUnavailable` and refuse the migration. A dry-run against a no-rollback backend MUST proceed without error (dry-runs do not write).

**MUST 6 — `backend_id` is a stable, non-empty, lowercase string.**
The `backend_id` property MUST return the same non-empty lowercase string on every call for the lifetime of the instance. Operator deployments may pin against this string.

**MUST 7 — Version bump verified post-apply.**
After each non-dry-run `apply_unit()` call, the backend MUST verify that the unit's `schema_version` (via `unit.read_frontmatter()["schema_version"]`) equals `script.to_version`. This check lives in `apply_unit()` itself — the runner does not re-verify. If the script did not bump the version, `apply_unit()` MUST raise `AtomicAgentsError` with a message naming the script and the unit; the failure propagates to the runner and triggers full rollback.

**MUST 8 — Audit event recorded unconditionally.**
Every `run_migration()` call — AND every operator-initiated manual rollback (the `restore_and_audit()` path reached by `python -m atomic_agents.migrate --rollback`) — MUST durably record one `MigrationEvent`. For `run_migration()` this fires on **every** exit path — dry-run or real; success, validation rollback, mid-apply exception, OR an early pre-plan refusal (no-rollback fail-close, broken/absent script chain, target-below-current). For a pre-plan refusal that fails *before the plan is built* — so `from_version` is not yet known — the runner records the sentinel `from_version = -1` and the requested `to_version`, with the exception string in `error`, so the failed attempt stays queryable. (Lock-busy refusal happens *after* the plan is built in the filesystem reference impl, so it records the resolved `from_version`, not the sentinel.) A manual rollback records the pre-rollback `from_version` and post-rollback `to_version` with `rolled_back = true` on success, or `rolled_back = false` + the error string on failure — a destructive recovery action is precisely the event an operator most wants a forensic record of. The bare `restore()` primitive (the in-run rollback called from inside `run_migration()`) does NOT emit on its own; its audit line is the `run_migration()` exit record. The audit record MUST be isolated from lock-release: a lock-release failure during teardown MUST NOT suppress it.

The *storage* of the audit record is a backend choice, the same way `snapshot()` is tar+gzip on filesystem and would be (e.g.) `pg_dump` on Postgres. The `FilesystemMigrationBackend` reference realization appends one JSONL line to `<agents_root>/_migrations/migration.jsonl`; that file is append-only, excluded from vault content walks, and preserved across snapshot/restore cycles. A non-filesystem backend satisfies MUST 8 by durably recording the equivalent `MigrationEvent` in its own store — it has no `<agents_root>/_migrations/migration.jsonl` to write to.

### Backup before migrate

The runner creates a snapshot before running:

```
<agents_root>/_migrations/snapshots/
└── 2026-08-12T143000_pre_v2_migration.tar.gz
```

The snapshot ref is printed to STDOUT immediately after creation:

```
Snapshot created: /path/to/_migrations/snapshots/2026-08-12T143000_pre_v2_migration.tar.gz
If this migration is interrupted, run: python -m atomic_agents.migrate --rollback 2026-08-12T143000_pre_v2_migration.tar.gz
```

The full vault contents (excluding caches, logs, and other regeneratable artifacts) are tar'd before any file is touched.

### Dry-run mandatory before real migration

```bash
# Always dry-run first
python -m atomic_agents.migrate --to v2 --dry-run

# Real migration (creates snapshot, applies, validates, rolls back if invalid)
python -m atomic_agents.migrate --to v2

# Status: which schema version is the vault at?
python -m atomic_agents.migrate --status

# Rollback to a specific snapshot
python -m atomic_agents.migrate --rollback 2026-08-12T143000_pre_v2_migration.tar.gz
```

### Validation after migrate

The migration runner validates **every enumerable unit** (not only the units a
script touched) once after applying all scripts:

1. Every unit passes the target schema's frontmatter validator
2. The unit's `schema_version` equals `script.to_version` (enforced per MUST 7)

Validating the full unit set — not just the touched subset — is what enforces
the all-or-nothing guarantee: a script whose `applies_to()` skips a unit leaves
that unit at the old version, and the half-migrated vault that produces is
**forbidden** (see §Multi-agent migration considerations). A skipped, still-old
unit therefore fails validation and triggers rollback.

If validation fails, the migration is **rolled back** by calling `backend.restore(snapshot_ref)`.

Note on the version cliff: the standard validators reject any `schema_version`
that is not equal to the package-global `CURRENT_SCHEMA_VERSION`. A real
cross-version migration therefore completes end-to-end only once the package
has adopted the target version (i.e. `CURRENT_SCHEMA_VERSION` is bumped in
`atomic_agents/_schema.py`). Until then, a real migration to a not-yet-adopted
target validates-and-rolls-back by design — the safe outcome.

### Helper behavior with old schema_version files

The shared helper (`atomic_agents`) understands all *current and prior* schema versions for read operations, but only writes the *current* version.

The helper reads old-schema files transparently. It refuses to *write* old-schema files — every write is the current version. This means agents continue to function during a partial migration, but new captures are always current-schema.

When a write happens to a file that's still at an old schema, the write triggers in-place migration of just that file (as a side effect). Eventually all active files migrate to current; the old ones remain at old schema until touched. The next full migration pass cleans up.

### Rollback

If migration goes wrong:

1. **Stop all agent runs** — `launchctl bootout` cron jobs; close skill sessions
2. **Restore from snapshot** (runner CLI):
   ```bash
   python -m atomic_agents.migrate --rollback 2026-08-12T143000_pre_v2_migration.tar.gz
   ```
3. **Verify** — the helper should now read normally with the prior schema version
4. **Investigate** — what broke about the migration? Fix the script or manually adjust files
5. **Retry** with corrected migration

The `--rollback` CLI flag calls the backend's `restore_and_audit()` Protocol method (the `FilesystemMigrationBackend` realization performs the atomic staging swap) which records a `MigrationEvent` audit line (`rolled_back = true` on success, `rolled_back = false` + the error on failure — see MUST 8) so the rollback stays queryable in `migration.jsonl`. Do **not** use `tar xzf` directly — the CLI is the correct operator interface: it preserves both the atomicity guarantee and the audit trail.

Snapshots are kept indefinitely (they're cheap — markdown compresses well). Periodically clean up snapshots older than 6 months if disk pressure is real.

### Multi-agent migration considerations

When `<agents_root>` has multiple agents, the migration is atomic across all of them. Half-migrated state (Caldwell on v2, another agent on v1) is forbidden — the helper would refuse some writes and accept others, leading to inconsistency.

The migration runner acquires a vault-level exclusive lock before enumeration and holds it through snapshot → apply → validate → unlock. A second concurrent migration attempt raises immediately ("Another migration is already running").

### Migration upgrade path (BREAKING — issue #429)

Operator scripts written against the old path-shaped contract MUST be rewritten:

| Old (removed) | New (current) |
|---|---|
| `def applies_to(path: Path) -> bool` | `def applies_to(unit: MigratableUnit) -> bool` |
| `def migrate(path: Path, dry_run: bool) -> dict` | `def migrate(unit: MigratableUnit) -> dict` |
| `path.write_text(frontmatter.dumps(parsed))` | `unit.write_frontmatter(meta)` |
| `path.suffix`, `path.name`, `"wiki" in path.parts` | `unit.unit_type` (``"memory"`` or ``"wiki"``) |

The `dry_run` parameter is removed from `migrate()` — the `MigratableUnit` handle has `dry_run` baked in. Scripts that called `if not dry_run: path.write_text(...)` MUST remove the guard and call `unit.write_frontmatter(meta)` unconditionally. The handle enforces the dry-run gate.

### What this protects against

- **Silent corruption** when a new field is required but old files don't have it
- **Type drift** when a field's expected type changes mid-flight
- **Renamed fields** producing dual copies in different files
- **Helper code referencing fields that no longer exist or have different shapes**

### What this does NOT protect against

- **Custom user-added frontmatter fields** — if you've added your own `priority` or `tag_color`, no migration knows about them. Document them; the migration runner preserves unknown fields by default.
- **External tools that read the markdown** — anything outside the Atomic Agents helper that reads frontmatter must understand the schema versions it's expected to handle.
- **Schema downgrades** — going from v2 back to v1 is generally NOT supported. Forward-only.
- **Per-backend DDL ladders** — SQLite `_ensure_schema` DDL for AgentProfile, ToolRegistry, and Log backends are internal to those backends and out of scope for `MigrationBackend`.

---

*Next: [04-runtime-assembly](04-runtime-assembly.md) — how the system prompt gets built at every invocation.*

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

The `schema_version: 1` field on every atomic unit isn't decoration — it's the contract that lets the format evolve without silently corrupting old data. Codex review (finding #13) flagged that the spec named the field but didn't specify the migration mechanics. Specifying them now.

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
├── v0_to_v1.py                  ← initial migration (legacy → current)
├── v1_to_v2.py                  ← future migration
├── v2_to_v3.py
└── README.md                    ← human-readable migration history
```

Migration scripts live at the root of `<agents_root>/`, not per-agent — the schema is global; one migration applies to every agent's files.

### Migration script format

```python
"""Migrate atomic memory frontmatter from schema_version 1 → 2.

What changes:
- New required field: `provenance`
- Renamed: `sources` → `evidence`
- All existing v1 files get `provenance: "v1_migrated"` and copy `sources` to `evidence`.
"""
from pathlib import Path
import frontmatter

FROM_VERSION = 1
TO_VERSION = 2

def applies_to(path: Path) -> bool:
    """Should this script touch this file?"""
    if path.suffix != ".md":
        return False
    if path.name == "INDEX.md":
        return False
    try:
        parsed = frontmatter.load(path)
    except Exception:
        return False
    return parsed.metadata.get("schema_version") == FROM_VERSION

def migrate(path: Path, dry_run: bool = False) -> dict:
    """Apply the migration to one file. Returns a summary of changes."""
    parsed = frontmatter.load(path)
    changes = []

    # Bump version
    parsed.metadata["schema_version"] = TO_VERSION
    changes.append("schema_version 1 → 2")

    # Rename sources → evidence
    if "sources" in parsed.metadata:
        parsed.metadata["evidence"] = parsed.metadata.pop("sources")
        changes.append("renamed sources → evidence")

    # Add provenance
    if "provenance" not in parsed.metadata:
        parsed.metadata["provenance"] = "v1_migrated"
        changes.append("added provenance: v1_migrated")

    if not dry_run:
        path.write_text(frontmatter.dumps(parsed))

    return {
        "path": str(path),
        "changes": changes,
        "dry_run": dry_run,
    }
```

Each script implements `applies_to()` and `migrate()`. The migration runner discovers scripts, walks the vault, and applies them in version order.

### Backup before migrate

The migration runner creates a snapshot before running:

```
<agents_root>/_migrations/snapshots/
└── 2026-08-12_pre_v2_migration.tar.gz
```

The full vault contents (excluding caches, logs, and other regeneratable artifacts) tar'd before any file is touched. If migration goes wrong, restore from the snapshot.

### Dry-run mandatory before real migration

```bash
# Always dry-run first
python -m atomic_agents.migrate --to v2 --dry-run

# Output: list of files that would change + what would change
# 47 files would be migrated
#   <agents_root>/caldwell/memory/feedback_debt_priority_order.md
#     - schema_version 1 → 2
#     - renamed sources → evidence
#     - added provenance: v1_migrated
#   ...
```

Operator reviews the dry-run output. If it looks correct, run for real:

```bash
python -m atomic_agents.migrate --to v2
# Creates snapshot, applies migration, validates result
```

### Validation after migrate

The migration runner validates the result:

1. Every touched file passes the new schema's frontmatter validator
2. INDEX.md references still resolve to existing files
3. No orphans created
4. No duplicate frontmatter fields (e.g., both `sources` AND `evidence` after rename)

If validation fails, the migration is **rolled back** by restoring from the snapshot.

### Helper behavior with old schema_version files

The shared helper (`atomic_agents`) understands all *current and prior* schema versions for read operations, but only writes the *current* version.

```python
def load_atomic_unit(path: Path) -> AtomicUnit:
    parsed = frontmatter.load(path)
    version = parsed.metadata.get("schema_version", 0)

    if version == CURRENT_SCHEMA_VERSION:
        return AtomicUnit(**parsed.metadata, body=parsed.content)

    # Old schema — adapt on read (lazy migration)
    if version == 1 and CURRENT_SCHEMA_VERSION == 2:
        # Apply read-time adaptation: rename `sources` to `evidence` in memory only
        meta = dict(parsed.metadata)
        if "sources" in meta and "evidence" not in meta:
            meta["evidence"] = meta.pop("sources")
        return AtomicUnit(**meta, body=parsed.content)

    raise SchemaVersionUnsupported(
        f"File at {path} has schema_version={version}; "
        f"this helper supports {SUPPORTED_VERSIONS}. "
        f"Run: python -m atomic_agents.migrate --to v{CURRENT_SCHEMA_VERSION}"
    )
```

The helper reads old-schema files transparently. It refuses to *write* old-schema files — every write is the current version. This means agents continue to function during a partial migration, but new captures are always current-schema.

When a write happens to a file that's still at an old schema, the write triggers in-place migration of just that file (as a side effect). Eventually all active files migrate to current; the old ones remain at old schema until touched. The next full migration pass cleans up.

### Rollback

If migration goes wrong:

1. **Stop all agent runs** — `launchctl bootout` cron jobs; close skill sessions
2. **Restore from snapshot**:
   ```bash
   cd <agents_root>
   tar xzf _migrations/snapshots/2026-08-12_pre_v2_migration.tar.gz
   ```
3. **Verify** — the helper should now read normally with the prior schema version
4. **Investigate** — what broke about the migration? Fix the script or manually adjust files
5. **Retry** with corrected migration

Snapshots are kept indefinitely (they're cheap — markdown compresses well). Periodically clean up snapshots older than 6 months if disk pressure is real.

### Multi-agent migration considerations

When `<agents_root>` has multiple agents, the migration is atomic across all of them. Half-migrated state (Caldwell on v2, another agent on v1) is forbidden — the helper would refuse some writes and accept others, leading to inconsistency.

The migration runner walks the entire `<agents_root>` in one pass. If migration fails for one agent's files (e.g., a broken file), the entire migration rolls back. All-or-nothing.

### What this protects against

- **Silent corruption** when a new field is required but old files don't have it
- **Type drift** when a field's expected type changes mid-flight
- **Renamed fields** producing dual copies in different files
- **Helper code referencing fields that no longer exist or have different shapes**

### What this does NOT protect against

- **Custom user-added frontmatter fields** — if you've added your own `priority` or `tag_color`, no migration knows about them. Document them; the migration runner preserves unknown fields by default.
- **External tools that read the markdown** — anything outside the Atomic Agents helper that reads frontmatter must understand the schema versions it's expected to handle.
- **Schema downgrades** — going from v2 back to v1 is generally NOT supported. Forward-only.

---

*Next: [04-runtime-assembly](04-runtime-assembly.md) — how the system prompt gets built at every invocation.*

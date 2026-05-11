# 02 — Atomic Memory

The recall subsystem. The load-bearing piece that makes Atomic Agents self-improving without rewrites.

---

## What Atomic Memory is

**Atomic Memory** = `memory/` (Atomic Notes) + `wiki/` (Atomic Wiki) + their `INDEX.md` files.

It's the part of the agent that:
- Captures durable observations during interactions
- Compiles ingested source material into queryable pages
- Routes the agent to the right specific memory at the right moment
- Stays cheap to load (INDEX-driven, not corpus-dumping)
- Survives sessions, runtime changes, and platform migrations

The mechanic in one sentence: **always load INDEX + persona + a few atomic units, pull more on demand**.

---

## Two sub-layers, different jobs

### Atomic Notes — semantic agent state

What the agent has *learned* during interactions. Primary observations.

**Examples**:
- "The user prefers debt elimination over investment optimization, confirmed 2026-04-15"
- "Locked decision: Q3 2026 income target = $X by Sept 30"
- "Caldwell should not reference the user's former business partner in financial discussions (per the user, 2026-03-22)"
- "The user's risk tolerance is moderate, debt-averse"

**Where**: `memory/`, one file per note, named `{type}_{topic}.md`.

**Captured by**: the agent itself, during interactions, when something durable happens.

### Atomic Wiki — distilled corpus

What the agent has *read* from external sources. Derivative knowledge.

**Examples**:
- "Tax planning strategies for 2026 (distilled from CPA's 40-page memo)"
- "Avalanche vs snowball debt payoff methods (distilled from finance book chapter)"
- "How federal tax brackets phase out itemized deductions"

**Where**: `wiki/`, one page per concept, paired with the source documents in `raw/`.

**Captured by**: the agent, on ingestion of a source document. Compiled with provenance back to `raw/`.

### Why two layers, not one

They differ in three ways that matter:

| | Atomic Notes | Atomic Wiki |
|---|---|---|
| **Source** | Conversation / observation | External document |
| **Authority** | Agent-curated, possibly subjective | Source-document-grounded, citeable |
| **Decay** | Stays relevant until superseded | Stays relevant until source doc supersedes |
| **Trust** | "Caldwell observed this" | "This is what the document says" |

If you collapse them into one pool, the agent can't tell the difference between "I learned this from the user" and "I read this in a book." That distinction matters at advice-time.

---

## INDEX-driven recall (the load-bearing trick)

Every layer has its own `INDEX.md` that's **always loaded into the system prompt**. The INDEX is a compact routing layer — one line per atomic unit, pointing at it.

Example `memory/INDEX.md`:

```markdown
# Caldwell — Memory Index

## Critical Feedback
- `Communication style` — bottom line first, specific not generic
- `Debt priority order` — credit cards before mortgage prepay

## Locked Decisions
- `Q3 2026 income target` — $X by Sept 30
- `Investment philosophy` — index funds, not active picks

## User Profile
- `User risk tolerance` — moderate, debt-averse
- `Money stress reality` — real, treat as legitimate

## Active Projects
- `Spouse consulting launch` — income side, status: planning
- `Day-job client retention` — affects household income

## Reference
- `Financial vault path` — ~/agents/finance/
- `CPA contact` — name + when to recommend involving them
```

**Format rules**:
- Sectioned by type
- One line per memory: ``Title` — one-sentence hook`
- Hook should be specific enough that the agent knows when to load the file
- Stay under ~150 chars per line
- Stay under ~150 lines total before splitting into sub-indexes (see below)

**The recall mechanic**:

1. INDEX.md is in the system prompt (always)
2. When the agent's reasoning needs a specific atomic unit, it reads the file by name
3. Most interactions only need INDEX + persona + 1-2 atomic units
4. Total memory tokens loaded: ~2-4K instead of 30K+

This is the same trick Karpathy's LLM Wiki uses, the same trick Memori demonstrated saves 20× tokens vs. full context, the same trick I (Claude Code) use with my own MEMORY.md system.

---

## Atomic Note format

Every atomic note is a markdown file with frontmatter + body.

```markdown
---
schema_version: 1
name: Debt priority order — credit cards before mortgage prepay
description: User locked debt elimination order — extinguish high-rate credit before mortgage extra payments
type: feedback
captured: 2026-04-15
last_seen: 2026-05-04
sources:
  - conversation_2026-04-15
confidence: high
pinned: false
expires_at: null
supersedes: null
superseded_by: null
---

The user confirmed 2026-04-15 that they want the credit card balances cleared before any extra mortgage prepayment, even though the math says mortgage-first is slightly better long-term.

**Why:** psychological weight. The credit card balances feel oppressive even at modest dollar amounts. Reducing them to zero matters more than basis-point optimization.

**How to apply:** When recommending debt strategy, default to avalanche-on-credit-cards-only. Only suggest mortgage-prepay if every credit balance is at zero AND the user asks about it.
```

**Frontmatter fields** (full schema in [03-file-formats](03-file-formats.md)):

| Field | Required | Purpose |
|---|---|---|
| `schema_version` | yes | Format version, currently `1` |
| `name` | yes | Human-readable title (also used in INDEX) |
| `description` | yes | One-line hook (used in INDEX) |
| `type` | yes | One of: user / feedback / project / decision / reference |
| `captured` | yes | YYYY-MM-DD when first written |
| `last_seen` | yes | YYYY-MM-DD when last confirmed/referenced |
| `sources` | yes | Array of pointers (conversation IDs, doc paths) |
| `confidence` | yes | `high` / `medium` / `low` |
| `pinned` | optional | If `true`, always loaded into context (use sparingly) |
| `expires_at` | optional | YYYY-MM-DD if memory has a time limit |
| `supersedes` | optional | Filename of older memory this replaces |
| `superseded_by` | optional | Filename of newer memory replacing this |

**Body structure** (suggested):

For `feedback` and `project` types — lead with the rule/fact, then `**Why:**` line, then `**How to apply:**` line. The why-and-how-to-apply lets the agent extrapolate to edge cases instead of blindly applying the rule.

For `decision` types — what was decided, on what date, by whom, with what alternatives considered.

For `user` and `reference` types — free-form factual content. Keep concise.

---

## Atomic Wiki page format

Same structure as a Note, with two key differences:

```markdown
---
schema_version: 1
name: Avalanche vs Snowball debt payoff methods
description: Two approaches to debt elimination — math-optimal vs psychology-optimal
type: wiki_page
captured: 2026-04-22
last_seen: 2026-05-06
sources:
  - raw/financial_freedom_book_ch7.md
  - raw/cpa_advice_2026-04-15.md
provenance: distilled
confidence: high
pinned: false
related:
  - debt_priority_order.md
  - credit_score_mechanics.md
---

## Avalanche method
Pay minimums on all debts. Pour every extra dollar into the highest-rate balance until it's zero. Move to next-highest. Mathematically optimal — minimizes total interest.

## Snowball method
Pay minimums on all debts. Pour every extra dollar into the smallest balance until it's zero. Move to next-smallest. Psychologically optimal — produces visible wins early.

## When to use which
[...]

## Sources
- `Financial Freedom, Ch 7` — the avalanche/snowball framing
- `CPA meeting 2026-04-15` — the user's CPA endorses avalanche for their rates
```

**Differences from Atomic Notes**:
- `type: wiki_page` (not in the agent-state taxonomy)
- `provenance: distilled` (vs. `observed` for notes)
- `related:` list for backlinks to other wiki pages (Karpathy-style)
- Body cites sources via wikilinks to `raw/` documents

---

## When to capture a memory

The single biggest design choice. Get this wrong and memory becomes either a junk drawer or a dead corpus.

**Capture (write a new Atomic Note)** when:

- ✅ User explicitly says "remember that" or "save this"
- ✅ User corrects the agent's behavior — that's a `feedback` memory
- ✅ User locks a decision after weighing alternatives — `decision`
- ✅ Agent learns something durable about the user — `user`
- ✅ User mentions a tool, system, or location the agent should know exists — `reference`
- ✅ Surprising / non-obvious information that future-you would re-derive painfully

**Do NOT capture** when:

- ❌ Information is in the persona files (already there, don't duplicate)
- ❌ Information is derivable from the current code/file state
- ❌ Information is ephemeral conversation context (one-off task detail)
- ❌ Routine task outputs ("I generated a report on Tuesday")
- ❌ Anything that would be in git history or a tool's audit log

The general principle: **capture what would surprise a fresh-start agent**. If reading the code or running git log would surface it, don't write a memory.

---

## Promotion: Atomic Notes → Persona

Some memories are so consistently confirmed that they should move into IDENTITY.md / SOUL.md / USER.md. This is the **promotion** loop, and it's how the persona evolves.

**Trigger**: a feedback or user-type note has been referenced 5+ times across sessions, OR has been confirmed at 3+ different points without contradiction.

**Process**:
1. Agent flags the candidate at end-of-session: "this user-memory has matured; consider promoting to USER.md"
2. The operator reviews, edits if needed
3. Promote into the right persona file
4. Mark the original Atomic Note as superseded with `superseded_by: persona/USER.md#section`

**Why this matters**: The persona is what's *always* loaded. Memory is what's *selectively* loaded. Promotion moves something from "the agent will find this when relevant" to "the agent always knows this."

Don't auto-promote without review. The persona is high-stakes; memory is low-stakes. Keep the trust gradient.

---

## Lint and conflict resolution

When two memories disagree, the agent doesn't know which is true.

**Lint pass** (run periodically — daily for active agents, weekly otherwise):

1. Find note pairs with similar `name` or `description` — possible duplicates
2. Find note pairs with the same `type` and overlapping `sources` but different content — possible contradiction
3. Find notes whose `expires_at` has passed — archive candidates
4. Find notes with `last_seen` older than 90 days and not pinned — staleness candidates
5. Surface findings to the operator as a list — the operator resolves by editing or merging

**Conflict resolution** when contradictions found:

- Newer wins by default — the older note gets `superseded_by: newer.md`, both stay readable
- BUT: if `confidence: high` on the older AND `confidence: low` on the newer, surface the conflict to the operator
- Never auto-delete — supersession is non-destructive, deletion is final

This pattern is borrowed from Memori and the analyticsvidhya cognitive-architectures piece. Memory drift is real; without explicit conflict resolution, agents end up holding both contradictory facts and behaving inconsistently.

---

## Sub-indexes (when INDEX gets too large)

The flat INDEX pattern works up to ~150 entries. Past that, the index itself becomes expensive to load and hard to scan.

**Trigger**: INDEX.md exceeds ~150 lines.

**Split into sub-indexes by type**:

```
memory/
├── INDEX.md                  ← top-level, links to sub-indexes
├── INDEX_feedback.md
├── INDEX_decisions.md
├── INDEX_projects.md
├── INDEX_user.md
└── INDEX_reference.md
```

`INDEX.md` then becomes:

```markdown
# Caldwell — Memory Index

This index is split by type. Load only the sub-index relevant to your current task.

- `INDEX_feedback` — communication, scope, behavioral preferences (32 entries)
- `INDEX_decisions` — locked architectural and strategic decisions (47 entries)
- `INDEX_projects` — active project state (18 entries)
- `INDEX_user` — user profile observations (24 entries)
- `INDEX_reference` — pointers to external systems (29 entries)
```

The agent loads `INDEX.md` always, sub-indexes on demand. This caps the always-loaded INDEX at ~20 lines no matter how big memory grows.

This is the [Bits of Chris navigational layer pattern](https://bitsofchris.com/p/an-llm-wiki-wont-compound-your-knowledge) applied — the limitation Karpathy's flat-index hits past ~few hundred articles.

---

## What about embeddings / vector search?

**Not required.** The INDEX-driven model loads atomic units by name, not by similarity.

You can add vector search as an **optional augmentation** for the wiki layer when:
- The wiki has hundreds of pages and topical search beats name-lookup
- The agent is dealing with fuzzy queries that don't map to a clear filename

But it's an addition, not a replacement. The core spec is plain-markdown + INDEX. No infrastructure dependency. No vector DB to maintain.

---

## Markdown is the source of truth — caches are derived

Codex review (finding #25) flagged that plain markdown lacks transactions, indexing, concurrent mutation safety, permissions, and query performance. True. The spec deliberately accepts those tradeoffs at small scale because the wins (human-readable, vault-native, runtime-portable, debuggable) are worth more than the database guarantees for a personal/few-agent system.

But **scale eventually matters**. Once an agent's `memory/` has thousands of notes or the wiki has hundreds of pages, certain operations slow down or get awkward:

- "Show me every memory with `confidence: high` from the last 30 days" — requires walking every file
- Lint passes that compare every note to every other note — quadratic in note count
- Cross-agent queries ("did any agent see the user mention X this month?") — even more files to walk
- INDEX.md regeneration after large bulk changes — full directory scan

When that scale arrives, the spec's answer is **derived caches**: SQLite (or DuckDB) sidecars built from the markdown, used for fast queries, regeneratable from the markdown at any time.

### The principle

**Markdown is canonical. Caches are disposable.**

- Every fact lives first in markdown. The cache is built FROM the markdown.
- Caches can be deleted at any time and rebuilt without data loss
- If markdown and cache disagree, markdown wins — rebuild the cache
- The cache is never the source for a memory write — writes go to markdown first, then the cache updates

This means the cache is a **performance optimization**, not a data store. You can run the entire system without it. It just gets slow at scale.

### When to add a cache

Add a derived cache when at least one of these is true:

- Memory or wiki has > 1,000 entries
- Lint passes take > 5 seconds
- Operator-initiated queries ("which atomic notes mention X?") take > 1 second
- Multi-agent project queue has > 100 active items needing concurrent reads

For a personal-scale deployment today (5-10 agents, ~50-200 notes per agent), no cache is needed. Document the threshold; don't pre-build.

### Cache layout

Per-agent (or per-system, with a `_cache/` at `<agents_root>/`):

```
<agents_root>/<agent>/_cache/
├── memory.sqlite              ← SQLite DB with one row per atomic note
├── wiki.sqlite                ← same for wiki pages
├── lint_cache.sqlite          ← cached lint results, invalidated on memory change
└── .built_at                  ← timestamp of last full rebuild
```

The `_cache/` prefix tells the operator (and Obsidian) "this is generated, not source." It's gitignored by default. It does not sync via Obsidian Sync (caches per-machine; rebuild on each).

### Schema (sketch)

```sql
CREATE TABLE memory_notes (
    filename       TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT,
    type           TEXT NOT NULL,
    captured       DATE NOT NULL,
    last_seen      DATE NOT NULL,
    confidence     TEXT,
    pinned         BOOLEAN,
    expires_at     DATE,
    supersedes     TEXT,
    superseded_by  TEXT,
    archived       BOOLEAN DEFAULT FALSE,
    body_excerpt   TEXT,                  -- first 500 chars for keyword search
    body_fts       TEXT                   -- for FTS5 full-text index
);

CREATE INDEX idx_type ON memory_notes(type);
CREATE INDEX idx_last_seen ON memory_notes(last_seen);
CREATE INDEX idx_pinned ON memory_notes(pinned) WHERE pinned = TRUE;
CREATE VIRTUAL TABLE memory_notes_fts USING fts5(body_fts, content=memory_notes);
```

Same shape for `wiki_pages` and `lint_findings`.

### Building and rebuilding

```python
from atomic_agents._cache import rebuild_memory_cache

# Full rebuild — walks every markdown file, replaces cache entirely
rebuild_memory_cache(agent_root)

# Incremental update — only re-process files changed since last build
update_memory_cache(agent_root)
```

The shared helper provides `update_memory_cache()` automatically when it writes a memory note (write to markdown → update cache). Manual rebuilds are for: cache corruption, schema migration, or "I edited memory files outside the helper and now want the cache to catch up."

### Detection: when the cache is out of sync

Compare cache `built_at` vs. the most-recent file mtime in `memory/`. If a markdown file is newer than the cache, the cache is stale.

Lint detects this and surfaces it: *"Cache is 4 days stale. Run `atomic_agents._cache.rebuild memory` to refresh."*

### When NOT to add a cache

- **Single-user, < 200 notes per agent** — straight markdown reads are fast enough
- **You can't be bothered to maintain a cache rebuild process** — better no cache than a stale one
- **You want the system to be 100% file-system-explorable** — caches obscure that property

If you don't add a cache, the system still works. It just gets slow proportional to scale.

### What this is NOT

- **NOT a primary database**. The cache holds NO data that isn't in markdown.
- **NOT synced across machines**. Each machine builds its own cache from the local markdown.
- **NOT under version control**. Caches are local artifacts; they're never committed.
- **NOT canonical for any query result**. If an answer disagrees with markdown, the markdown is right.

### Future: vector cache

When wiki gets big enough that name-lookup isn't enough (operator asks "which page covers X?" without knowing the page name), a vector embedding cache becomes useful. Same principle: derived from markdown, regeneratable, never canonical. Out of scope for v1; spec'd here as a future addition.

---

## Atomic Memory and Karpathy's LLM Wiki

We're a superset, not a competitor:

- Atomic Wiki = Karpathy's wiki pattern, applied to per-agent corpora
- `raw/` = Karpathy's `raw/`
- `wiki/INDEX.md` = Karpathy's `index.md`
- We add: typed atomic notes for agent-state, separate persona layer, capture rules, promotion path, supersession pointers

If you've read Karpathy's pattern, the wiki layer here will look familiar. The novelty is splitting agent-state observations (Atomic Notes) from corpus distillation (Atomic Wiki) and treating them with different rules.

---

---

## Versioning, concurrency, and read-only mounts

Three Anthropic memory-model parity features added in PR #46.

### Memory versioning

Every overwrite or merge of an existing memory note creates an immutable snapshot before the mutation. Fresh writes (creating a note for the first time) do not snapshot — there is no prior content to preserve.

**Storage layout:**

```
<agent>/memory/
├── feedback_communication_style.md    ← live note
└── .versions/
    └── feedback_communication_style/
        ├── 20260507T143012Z_a3f8c1b2.md   ← oldest snapshot
        └── 20260507T161455Z_d92e4f77.md   ← newest snapshot
```

- `.versions/` is a hidden directory at the same level as the live memory notes.
- One subdirectory per note, named by the note's stem (without `.md`).
- One file per version, named `<ISO-ts>_<8-char-sha256>.md`. The ISO timestamp is UTC in `YYYYMMDDTHHMMSSZ` format (sortable, filesystem-safe). The 8-char hash disambiguates near-simultaneous writes.
- `INDEX.md` is excluded from versioning — it is mechanical scaffolding, not semantic content.
- Version files contain the **full old content** (frontmatter + body), not a diff.

**When versions are created:**

| Event | Snapshot? |
|---|---|
| `write_atomic_note` on a new note (first write) | No — no prior content |
| `write_atomic_note` overwrite (orphan-recovery path) | Yes — snapshots old content first |
| `write_atomic_note` with `merge_into` on an existing note | Yes — snapshots old content first |
| `restore_version` — restoring a snapshot | Yes — snapshots current live state before overwriting |
| `redact_version` — replacing a snapshot's body | No — snapshot of a snapshot would be circular |

**Versioning API** (`atomic_agents._versioning`):

```python
from atomic_agents._versioning import (
    snapshot_memory_version,
    list_versions,
    read_version,
    restore_version,
    redact_version,
)

# Internal: called automatically by write_atomic_note before any overwrite
version_path = snapshot_memory_version(live_note_path)  # → Path | None

# List all snapshots for a note, newest first
versions = list_versions(memory_dir, "feedback_comm_style.md")  # → list[Path]

# Read a snapshot's content
fm_dict, body_text = read_version(versions[0])  # → (dict, str)

# Restore live note to a snapshot (reversible — snapshots current state first)
live = restore_version(memory_dir, "feedback_comm_style.md", versions[0])

# Redact a snapshot's body (compliance — preserves frontmatter audit trail)
redact_version(versions[0], replacement="[REDACTED — PII removed]")
```

**Logging:** each versioning event appends a JSONL line to the agent's per-day log:

| Event | `trigger` value |
|---|---|
| Snapshot created | `memory_version_created` |
| Snapshot restored to live | `memory_version_restored` |
| Snapshot body redacted | `memory_version_redacted` |

**Retention:** versions persist indefinitely — no auto-expiry. Operators can prune the `.versions/` directory freely; the live notes are the source of truth.

---

### Optimistic concurrency

`write_atomic_note` accepts an optional `expected_content_sha256` precondition to prevent clobbering concurrent writes.

```python
import hashlib

# Read the current note and compute its sha256
current = (memory_dir / "feedback_comm_style.md").read_text()
sha = hashlib.sha256(current.encode()).hexdigest()

# Later: write only if the note hasn't changed since we read it
write_atomic_note(
    agent_root, capture, write_paths,
    expected_content_sha256=sha,
)
```

**Behavior:**

| Condition | Result |
|---|---|
| `expected_content_sha256` omitted | Always proceeds (existing behavior) |
| Note exists, sha256 matches | Proceeds normally |
| Note exists, sha256 mismatch | Raises `MemoryPreconditionFailed` with `actual_sha256` attribute |
| Note doesn't exist, precondition provided | Raises `MemoryPreconditionFailed(actual_sha256=None)` |

On `MemoryPreconditionFailed`, the caller re-reads the note with `read_version` (or direct file read), merges its changes, and retries with the fresh sha256. The `actual_sha256` field in the exception carries the current on-disk sha so the caller can compute the diff without a second file read.

---

### Read-only path declaration

Sessions can attach memory directories as read-only, so agents can reference shared material without risk of accidentally writing to it — even when their write paths are broadly scoped.

**Declare in `tools.md`:**

```markdown
## Read paths
- ~/agents/shared/wiki/

## Write paths
- ~/agents/myagent/memory/

## Read-only paths
- ~/agents/shared/reference/
```

Paths listed under `## Read-only paths` (or `## Read only paths` — both accepted) are enforced as write-blocked by `enforce_write_path`, which is called on every `write_atomic_note`. The read-only constraint wins even if the same path appears under `## Write paths`.

**Use cases:**

- Shared reference libraries read by multiple agents — mount as read-only so no agent corrupts the corpus.
- Ingest-only corpora (raw documents waiting for wiki distillation) — agents can read but shouldn't overwrite.
- Audit-critical decision logs — protect from casual merge/overwrite.

The `AgentConfig.read_only_paths` field carries the parsed paths; they flow through `agent.call()` into every capture write automatically.

---

### Comparison to Anthropic's memory_version model

| Feature | Anthropic managed | Atomic Agents |
|---|---|---|
| Per-mutation immutable versions | Yes (API-managed) | Yes (filesystem `.versions/`) |
| Snapshot storage | Anthropic servers, opaque | Local markdown files, human-readable |
| Retention / expiry | 30-day auto-expiry | Indefinite, operator-prunable |
| Concurrency guard | `content_sha256` precondition | `expected_content_sha256` precondition |
| Read-only mount | Session-level `include_memory` flag | `## Read-only paths` in tools.md |
| Multiple stores per agent | Up to 8 stores per session | Single `memory/` per agent (multi-store deferred to follow-up PR) |
| Query / list API | REST/SDK | Python API + `atomic-agents version` CLI |
| Compliance redact | Not specified | `redact_version()` — replaces body, preserves frontmatter |
| Cross-machine sync | Automatic | Via Obsidian Sync or rsync (operator-managed) |

The core design difference: Anthropic's model is API-first (server holds state, client queries it); Atomic Agents is filesystem-first (markdown is canonical, APIs are helpers built on top). The filesystem-first model gives you human-readable audit trails, vault-native portability, and no vendor dependency — at the cost of managing your own retention and sync.

---

*Next: [03-file-formats](03-file-formats.md) — exact frontmatter schemas and naming conventions.*

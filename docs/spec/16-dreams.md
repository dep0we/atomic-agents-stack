# spec/16 — Dreams: Memory Consolidation Pipeline

## Conceptual model

An agent's `memory/` directory accumulates over time. Duplicates form when
similar observations are captured separately. Contradictions emerge when newer
journal entries supersede old memory notes without the notes being updated.
Stale notes linger past their useful life. Journal observations that should
have been promoted to memory never are.

Dreams fix this. Between sessions, the operator triggers a dream run. The agent
"dreams": it reads its full memory + recent journal + recent log, runs four
detection passes, then produces a **new parallel memory directory** the operator
reviews and either applies (atomic swap) or discards.

Dreams are always operator-initiated — the agent never dreams autonomously.
Auto-scheduled dreams (cron extras) are deferred to a future release.

## When to dream

- After a long work push where many captures were written in a short window
- When the agent begins behaving inconsistently — may signal contradictory memory
- On a periodic maintenance cadence (monthly or quarterly for active agents)
- After major project milestones that change many prior decisions

## Storage layout

```
<agent>/dreams/
└── drm_<YYYY-MM-DDTHHMMSS>_<6hex>/     # dream-id
    ├── memory/                          # dreamed output (never overwrites live)
    │   ├── INDEX.md
    │   └── <notes>.md
    ├── report.md                        # human-readable diff of every decision
    └── manifest.json                    # status + inputs + usage + timestamps
```

The dreamed `memory/` is built entirely fresh. The live `<agent>/memory/` is
never touched until the operator explicitly calls `apply`.

## Pipeline phases

### Phase 1: Acquire dream lock

Lock at `<agent>/dreams/.lock` — separate from the agent's main `.lock`. Dreams
must not block normal agent calls. Fails fast (30s timeout) with
`DreamInProgress` if another dream is already running.

### Phase 2: Initialise manifest

`status=pending` → write to disk → `status=running`. Manifest is written
incrementally so a partially-complete run can be inspected.

### Phase 3: Read inputs

- **Memory notes**: all `*.md` in `<agent>/memory/` (skip `INDEX.md`). Parses
  frontmatter + body.
- **Journal entries**: all `*.md` in `<agent>/journal/` within
  `journal_lookback_days`.
- **Log lines**: `<agent>/log/<YYYY-MM>/<YYYY-MM-DD>.jsonl` within
  `log_lookback_days`. Helper trigger lines are filtered out (too noisy);
  coordinator-level records only.

### Phase 4: Detection passes

Four passes run. The first three fan out via `_batch_llm_calls` (parallel
threads); stale detection is mechanical (no LLM).

#### Duplicate detection

Clusters memory notes by `type` and token-overlap on `name`. For each cluster
of size ≥ 2, asks the model: "are these the same observation? if yes, propose
a merged body." Output: list of `(cluster_filenames, merged_body_or_none)`.

#### Contradiction detection

Scans recent journal entries against memory notes. Asks: "does this journal
entry contradict this memory note? if yes, what should the resolved value be?"
Contradiction data informs the synthesis pass.

#### Stale detection (mechanical)

Notes whose `last_seen` is older than 90 days AND not `pinned=true` are marked
with `expires_at = today + 30 days`. No LLM call needed.

#### Promotion detection

Clusters journal entries by topic similarity. Surfaces clusters that aren't
already represented in memory as candidates for promotion to atomic notes.

### Phase 5: Synthesis pass

ONE main model call (persona NOT loaded — this is a meta-task, not the agent's
normal voice). Input: detection results + optional `instructions` parameter.
Output: confirmation that the consolidation plan is sound; any final observations
are logged but don't change the output.

The detection passes produce the actual plan; synthesis is a coherence check.

### Phase 6: Write outputs

1. **Consolidated notes** — written with `supersedes: [old1, old2]` in
   frontmatter (see Supersedes contract below).
2. **Promoted notes** — new notes derived from recurring journal observations.
3. **Unchanged notes** — copied as-is (or with updated `expires_at` for stale).
4. **Fresh INDEX.md** — rebuilt from scratch from output notes.
5. **report.md** — markdown listing every consolidated/promoted/stale change
   with reasoning.
6. **manifest.json** — updated with `status=completed`, final token usage,
   and counts.

### Phase 7: Release dream lock

Lock released on both success and failure. Partial output is preserved on
failure so the operator can inspect what was produced before the crash.

## The `supersedes:` frontmatter contract

Every consolidated note records the filenames it replaces:

```yaml
---
name: Debt Priority Rule
type: feedback
supersedes: feedback_debt_priority.md
supersedes_list:
  - feedback_debt_priority.md
  - feedback_debt_prioritization.md
---
```

This makes the dream's decisions **auditable from the file itself** — you don't
need to diff two memory stores side-by-side. Any tool that reads the output
directory can see what was merged and why.

Cross-reference: spec/05 capture rules define the single-value `supersedes:`
field for normal captures. Dreams extend this with `supersedes_list:` for
multi-note consolidations.

## Lifecycle states

| State | Description |
|-------|-------------|
| `pending` | Manifest written, pipeline not yet started |
| `running` | Pipeline actively executing |
| `completed` | All phases done; output dir ready for review |
| `failed` | Exception during pipeline; manifest has `error` field |
| `canceled` | Reserved for future operator-cancel support |

States match Anthropic's Dreams API lifecycle for conceptual parity.

## Apply / discard / revert workflow

```
operator triggers: runner.start()
  → dreams/drm_.../
    ├── memory/    (the proposed new state)
    ├── report.md
    └── manifest.json  status=completed

operator reviews: runner.review(dream_id)
  → prints report.md

operator applies: runner.apply(dream_id)
  → renames <agent>/memory/  → <agent>/memory.archived-<ts>/
  → renames dreams/<id>/memory/ → <agent>/memory/
  → manifest.json gains applied_at + archived_path

to revert:
  → rename <agent>/memory/ → (trash or version)
  → rename <agent>/memory.archived-<ts>/ → <agent>/memory/

operator discards: runner.discard(dream_id)
  → shutil.rmtree(dreams/<id>/)
  → only allowed if not yet applied
```

Apply is atomic: two `os.rename` calls. The window between them is the only
moment of inconsistency; in practice it's sub-millisecond.

## Cost considerations

Dreams make multiple LLM calls:

- One detection call per duplicate cluster (parallel)
- One detection call per journal batch for contradiction checking (parallel)
- One promotion detection call (parallel)
- One synthesis call

Upfront cost is estimated from input volume before any LLM call. If the
estimate exceeds the agent's remaining cap headroom, the dream is refused with
an informative error. `critical=True` bypasses the check (still logged).

Model defaults to the agent's `model.md` default. `--model` overrides.

## Comparison table

| Feature | Dream | Tuning | Migrate | Eval |
|---------|-------|--------|---------|------|
| What it reads | memory/ + journal/ + log/ | eval runs + memory notes | memory/ notes | agent response + rubric |
| Output | New parallel memory/ dir | Edit proposals (report) | Migrated notes in-place | Scored JSONL records |
| Operator gate | review → apply/discard | pending → accepted/rejected | dry-run review | re-run on regression |
| LLM usage | Detection + synthesis | Optional polish only | None (schema migration) | Judge model |
| Risk | Memory state change (reversible) | Persona/tools edit (reversible) | Schema upgrade (reversible) | None (read-only) |

## Comparison to Anthropic Dreams

| Aspect | Anthropic Dreams API | atomic_agents.dream |
|--------|---------------------|---------------------|
| Trigger | Operator API call | Operator Python call / CLI |
| Input | Conversation transcripts | memory/ + journal/ + log/ |
| Output | New Memory resource (API object) | New local `dreams/<id>/memory/` dir |
| Lifecycle | pending/running/completed/failed | Same states |
| Apply | Update resource via API | `os.rename` dir swap |
| Audit | Via API response fields | `supersedes:` frontmatter in output files |
| Scheduling | Available in API | Deferred (see note below) |

Auto-scheduled dreams (cron / periodic) are a natural follow-on. The storage
and lifecycle model is already cron-safe; a future `extras/dream_scheduler.py`
can wrap `DreamRunner.start()` with the automations-hub pattern.

## Cross-references

- **spec/02** (atomic memory) — the memory schema that dreams consolidate
- **spec/05** (capture rules) — the `supersedes:` and `merge_into:` frontmatter
  contract extended by dream output
- **spec/09** (cost observability) — guardrail mechanics reused by dream cost check
- **spec/11** (tuning) — parallel pattern detector design; kept separate for now

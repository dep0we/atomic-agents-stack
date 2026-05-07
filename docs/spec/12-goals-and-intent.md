# 12 — Goals and Intent

How to build agents that pursue a **persistent goal across many sessions** — not just answer this turn's question, but hold an objective and decompose it into work over time.

This spec adds a layer the rest of the spec deliberately deferred: **goal-driven agents** like Muse's Director, who hold a goal ("complete this novel's first draft") across hundreds of runs and generate the work that gets dispatched to other agents.

Most agents don't need this. Caldwell, Bishop, Harper, Paul are all **reactive** — given a question, they answer; given a queue item, they handle it; each invocation is a discrete transaction. They don't need a goal layer because the operator decides what's next.

Goal-driven agents are different. They decide what's next themselves, against a target the operator set.

---

## Agent type taxonomy

Every Atomic Agent declares its operating mode in IDENTITY.md. Three options:

| Mode | What it does | Examples | Has goal.md? |
|---|---|---|---|
| **Reactive** | Handles each request as a discrete transaction. The operator (or another agent) supplies the work item; the agent reasons and responds. | Caldwell, Bishop, Harper, Paul | NO |
| **Goal-driven** | Maintains an active goal across runs. Decomposes the goal into sub-goals and queue items. Generates work for itself or other agents until the goal's success criteria are met. | Muse Director, future research-pursuit agents, ops-automation agents | YES (one per active goal) |
| **Hybrid** | Reactive most of the time. Becomes goal-driven when an active goal.md exists. Falls back to reactive when no goals are active. | Muse Director (reactive for ad-hoc queries; goal-driven during a project) | OPTIONAL (active when goals exist) |

The mode is declared in IDENTITY.md:

```markdown
## Operating mode

This agent is **goal-driven** (or **reactive** or **hybrid**).

[For goal-driven/hybrid agents:]
When `goal.md` exists with `active: true`, I read the goal at every invocation and use it as the anchor for what to pursue. I decompose goals into sub-goals, generate queue items for myself or peer roles, and track progress in `goal.md`.

When no active goal exists (reactive mode for hybrid agents), I behave as a standard reactive agent — handle each request as a discrete transaction.
```

---

## File layout

A goal-driven agent's vault folder gets one extra file:

```
<agent_root>/
├── persona/
├── tools.md
├── model.md
├── memory/
├── wiki/
├── journal/
├── log/
└── goal.md              ← OPTIONAL — only for goal-driven / hybrid agents with active goals
```

For multi-agent projects (per spec/06), goals usually live at the **project level**, visible to all roles in that project:

```
<agents_root>/<system>/
├── roles/                 (shared role templates)
└── projects/<project>/
    ├── canon.md
    ├── style_guide.md
    ├── policy/
    ├── queue/
    ├── goal.md            ← project-level goal — all roles see it
    └── agents/<role>/
```

The cascade resolves goals same as canon/style — project-shared, available to every role's runtime context.

A single agent (non-project) with a goal puts it at the agent root: `<agent_root>/goal.md`.

---

## `goal.md` format

```yaml
---
schema_version: 1
active: true
intent: "Complete 'The Unfinished' first draft by Q4 2026"
priority: high                       # high | medium | low
created: 2026-05-06
deadline: 2026-12-31                 # null if no deadline
last_progress_check: 2026-05-08
parent_goal: null                    # filename if this is a sub-goal of a bigger goal

success_criteria:
  - "All 24 chapters drafted (currently 4)"
  - "Style guide passes lint on every scene"
  - "No open structural questions in policy/"
  - "Completed manuscript reviewed by Director against original outline"

sub_goals:
  - id: ch_1_to_4
    label: "Chapters 1-4 drafted and edited"
    status: complete                 # pending | in_progress | complete | blocked | abandoned
    assigned: writer
    deadline: 2026-04-30
    completed: 2026-04-28
  - id: ch_5_draft
    label: "Chapter 5 first draft"
    status: in_progress
    assigned: writer
    deadline: 2026-05-15
  - id: ch_5_edit
    label: "Chapter 5 editorial pass"
    status: pending
    assigned: editor
    deadline: 2026-05-22
    blocked_by: ch_5_draft
  - id: ch_6_outline
    label: "Chapter 6 scene-level outline"
    status: pending
    assigned: outliner
    deadline: 2026-05-12

related_atomic_notes:
  - feedback_voice_consistency.md
  - decision_pov_third_limited.md
related_decisions:
  - policy/lock_001_genre.md
  - policy/lock_002_pov.md
related_canon_pages:
  - canon.md#protagonist-character-sheet
---

# The Unfinished — Director goal

## Current state

Chapters 1-4 fully drafted and through one editorial pass. Chapter 5 in active drafting (Writer working on it now, deadline 2026-05-15). Chapter 6 outlining queued for next week.

Pacing target: ~1 chapter every 2-3 weeks for the rest of the year. At current pace we hit the Q4 deadline with ~2 weeks of buffer for final review and revision passes.

## Recent progress

- 2026-05-06 — Chapter 4 editorial pass complete; queued Chapter 5 draft
- 2026-04-28 — Chapter 4 first draft complete
- 2026-04-15 — Chapters 1-3 editorial pass complete

## Active blockers

None at present. Watch for: Writer's bandwidth (Q3 personal commitments may slow throughput), and structural question on Ch 7 villain motivation (currently open in `policy/lock_009_villain_arc.md`).

## Decomposition pattern

For each chapter:
1. Outliner produces scene-level beats (1-3 days)
2. Writer drafts scenes from beats (5-7 days)
3. Editor passes for line-edit and style-guide compliance (2-3 days)
4. Director reviews against outline; flags structural issues (1 day)
5. Re-queue to Writer if revisions needed; otherwise move to "completed" stack

Each step is a queue item with the chapter+scene as input.

## History (auto-appended)

- 2026-05-08 — last_progress_check updated; sub_goal `ch_6_outline` added
- 2026-05-06 — sub_goal `ch_5_draft` moved to in_progress
- 2026-04-28 — sub_goal `ch_1_to_4` moved to complete
- 2026-05-06 — goal created
```

### Required frontmatter fields

| Field | Purpose |
|---|---|
| `schema_version` | Format version, currently `1` |
| `active` | Whether the goal is currently being pursued (`true`/`false`) |
| `intent` | One-line statement of what's being pursued |
| `priority` | `high` / `medium` / `low` — affects ordering when multiple goals compete |
| `created` | When the goal was created |
| `deadline` | Target completion date (or `null`) |
| `last_progress_check` | When the agent last evaluated progress |
| `success_criteria` | Array of measurable criteria — when ALL met, goal is complete |
| `sub_goals` | Array of decomposed work units (see below) |

### Optional fields

| Field | Purpose |
|---|---|
| `parent_goal` | If this goal is itself a sub-goal of a larger goal |
| `related_atomic_notes` | Memory notes that bear on this goal |
| `related_decisions` | Locked decisions that constrain this goal |
| `related_canon_pages` | Canon / style pages this goal references |

### Sub-goal structure

Each entry in `sub_goals`:

```yaml
- id: <unique within this goal>
  label: <human-readable>
  status: pending | in_progress | complete | blocked | abandoned
  assigned: <role_name | "self" | null>
  deadline: <date | null>
  blocked_by: <other sub_goal id | null>      # if status == blocked, what's blocking
  completed: <date>                            # only when status == complete
  output: <path to artifact, if applicable>    # what this sub_goal produced
```

---

## Runtime assembly with goals

The canonical load order from spec/04 extends with goal context. For a goal-driven agent (or hybrid in goal-active mode), the agent's runtime adds:

```
... existing canonical load order ...
[3.5] AGENT goal.md (single-agent case) OR
[7.5] PROJECT goal.md (multi-agent project case)
[14] The work item (queue item OR auto-generated next sub-goal)
```

For the hybrid case, the agent loads goal.md only if it exists with `active: true`. If no active goal, the agent runs in pure reactive mode.

The goal becomes part of the system prompt — the agent always knows what it's pursuing, where it is, what's next.

---

## Decomposition mechanism

The core goal-driven loop on each invocation:

```
1. Load goal.md
2. Check success_criteria
   ├── ALL met → mark active=false, write final journal entry, archive
   └── Some unmet → continue
3. Check sub_goals
   ├── Any in_progress with stale work item → renew the work item's lease (per spec/06)
   ├── Any complete that haven't been verified → run verification, close them out
   ├── Any pending and unblocked → identify next sub_goal to dispatch
   └── No pending sub_goals AND criteria not met → generate new sub_goals
4. Dispatch:
   ├── If self-assigned → handle the sub_goal directly (becomes this run's work item)
   ├── If peer-assigned → write a queue item to projects/<name>/queue/pending/
   └── If null assigned → ask operator to assign
5. Update goal.md:
   ├── Bump last_progress_check
   ├── Update sub_goal statuses
   └── Append history entry
```

This is what makes the agent feel "alive" — it's perpetually evaluating "where am I, what's next" against a target it remembers.

### Generating new sub_goals

When existing sub_goals are exhausted but success_criteria aren't met, the agent decomposes further. This is where domain knowledge matters:

- Muse Director knows "next chapter needs an outline first, then scenes drafted, then edited" — that's the decomposition pattern, declared in `goal.md`'s body
- A research-pursuit agent might decompose by source: "Read paper X, read paper Y, synthesize, write summary"
- An ops-automation agent might decompose by environment: "Patch staging, verify, patch production, monitor"

The decomposition pattern is part of the goal-driven agent's IDENTITY or SOUL — it's how the role thinks. When tuning (spec/11) detects that decomposition is producing low-quality sub_goals, that's a signal to tighten the persona.

---

## Completion criteria

A goal is complete when **all** entries in `success_criteria` are demonstrably met.

The agent evaluates this at every invocation. Some criteria are mechanical (count chapters drafted vs. 24); others require judgment ("style guide passes lint" is mechanical, but "no open structural questions" requires checking that `policy/` has no items in pending state).

When the agent decides all criteria are met:
1. Set `active: false`
2. Write a final journal entry summarizing the goal pursuit
3. Move `goal.md` to `goal_archive/<YYYY-MM-DD>_<intent_slug>.md`
4. Surface to operator: "Goal achieved: [intent]. Created [date], completed today. Total runs: N. Final state: [...]"
5. The agent reverts to reactive mode (or to the next active goal if there's a queue of goals)

If the agent is uncertain whether a criterion is met, it surfaces to the operator: "Goal appears mostly complete. Criterion 3 (no open structural questions) — currently 1 item in policy/ flagged 'review pending.' Should this count as met?"

---

## Goal abandonment

Goals can be abandoned (operator decides it's no longer worth pursuing):

```bash
python -m atomic_agents.goal abandon caldwell --reason "Strategy shifted; this goal no longer aligns"
# Sets active=false, writes abandonment note, moves to goal_archive/
```

Abandonment is non-destructive (archived, not deleted). The history is preserved for future reference.

---

## Multiple active goals per agent

An agent can have at most one **primary** active goal. If a second goal becomes active, options:

1. **Sequential**: complete (or abandon) the current goal before starting the next
2. **Multi-goal mode**: the agent juggles multiple goals, deciding per-invocation which goal to advance

V1 spec is **sequential only**. Multi-goal juggling adds priority arbitration, context interference, and scheduling complexity that's not justified for current use cases.

For a hybrid agent that needs both reactive responsiveness AND goal pursuit, the current invocation's work item determines mode:
- Operator/skill triggers a query → reactive mode for this invocation, goal stays in the background
- Cron triggers → goal-driven mode, agent advances the goal

---

## Progress check cadence

Goal-driven agents update `last_progress_check` automatically each time they run. But for long-running goals, the operator wants periodic explicit progress reports.

Define cadence in `model.md`:

```yaml
goal_progress_check:
  enabled: true
  cadence: weekly                  # daily | weekly | monthly
  surface_via: journal             # journal | telegram | email
  include_in_report:
    - sub_goals_status
    - success_criteria_progress
    - blockers
    - estimated_completion_date
```

Weekly check produces an entry like:

```
Goal: Complete 'The Unfinished' first draft by Q4 2026
Status as of 2026-05-08:
  ▸ 4 of 24 chapters complete (16.7%)
  ▸ Chapter 5 in progress (Writer)
  ▸ At current pace: complete by 2026-11-15 (~6 weeks ahead of deadline)
  ▸ Blockers: none

Recent activity:
  - 2026-05-06: Chapter 4 editorial pass complete
  - 2026-05-08: Chapter 5 drafting in progress
```

This report becomes a journal entry the operator can read at their cadence (Sunday morning Obsidian browse, weekly Telegram digest, etc.).

---

## Worked example: Muse Director with The Unfinished

The goal.md shown above IS the worked example. Walking through what happens at runtime:

**Cron trigger at 2026-05-08 09:00 (Director's morning run)**

1. Director loads canonical context: persona, tools, model, project canon/style/policy, INDEXes
2. Loads `projects/the-unfinished/goal.md` (the project goal)
3. Checks success_criteria: 4/24 chapters complete; not done
4. Checks sub_goals:
   - `ch_1_to_4`: complete (verified in last run)
   - `ch_5_draft`: in_progress (Writer claimed 2026-05-06; lease still valid)
   - `ch_5_edit`: pending, blocked by ch_5_draft
   - `ch_6_outline`: pending, no blockers
5. Director identifies `ch_6_outline` as the next dispatchable sub_goal
6. Writes a queue item: `projects/the-unfinished/queue/pending/ch_6_outline.md` with assigned: outliner, deadline, references to canon/style for context
7. Updates goal.md: ch_6_outline status now `dispatched` (or stays pending until Outliner claims it from queue); appends history entry
8. Updates last_progress_check
9. Writes journal entry summarizing the run

Director's whole run took ~30 seconds and ~$0.20 in tokens (mostly loading context + reasoning about what to dispatch next). No actual content was written — Director's job is dispatching, not creating.

**Outliner cron run at 2026-05-08 10:00**

1. Outliner loads canonical context
2. Scans `projects/the-unfinished/queue/pending/` for items assigned to outliner
3. Finds `ch_6_outline.md`, claims it (atomic move per spec/06)
4. Reads the canon, the previous chapter outlines, the style guide
5. Generates scene-level beats for chapter 6
6. Writes output to `projects/the-unfinished/outputs/ch_6_outline.md`
7. Moves queue item from in_progress/ to completed/
8. Writes journal entry

**Director's next cron run at 2026-05-09 09:00**

1. Same context load
2. Sees `ch_6_outline` queue item is now in completed/
3. Updates goal.md sub_goal `ch_6_outline` to complete; adds new sub_goal `ch_6_draft` assigned to writer with deadline
4. Writes new queue item, etc.

The loop continues. Each agent does its job. The goal anchors what work happens and in what order.

---

## What goal-driven agents do NOT do

❌ **Don't auto-generate goals.** Goals are operator-set (or set by another goal-driven agent that has explicit permission). The agent doesn't decide for itself "I should pursue X" — that's operator scope.

❌ **Don't tune their own success criteria.** If a criterion is wrong, the operator edits it. Same principle as rubric tuning — moving the goalposts to fit current behavior breaks the loop.

❌ **Don't dispatch outside their scope.** A Director can dispatch to its own project's roles; it can't dispatch to other projects' agents without explicit cross-project authorization.

❌ **Don't auto-extend deadlines.** If the deadline passes and the goal isn't complete, the agent surfaces "deadline missed" — operator decides whether to extend, abandon, or accept overrun.

❌ **Don't create infinite sub-goal chains.** Decomposition is bounded by the goal body's "decomposition pattern" section. If the agent is generating sub-goals indefinitely without making progress on success_criteria, that's a sign the decomposition pattern is wrong — surfaces to operator.

❌ **Don't override locked decisions.** If a sub_goal would violate a `policy/` lock, the agent surfaces the conflict — never silently overrides.

---

## Anti-patterns

❌ **Putting reactive work in goal.md.** "Answer Dan's question about X" is a reactive request; it doesn't belong as a sub_goal. Goals are persistent objectives, not single-turn tasks.

❌ **Vague success criteria.** "Make the novel good" can't be evaluated. "All 24 chapters drafted; style guide lint passes; structural review done" can.

❌ **Goals without deadlines for time-bounded work.** A novel needs a deadline to drive sub-goal pacing. A research goal might be open-ended; clearly mark it as such.

❌ **Multiple active goals on a v1 agent.** Don't try to juggle. Sequential pursuit only.

❌ **Goal scope larger than the agent's role.** Director's goal is "complete the novel" — fits Director's scope. If the goal would require reaching outside the project (e.g., "negotiate publishing deal"), that's outside Director's scope; operator handles it directly.

---

## Goal vs. Project vs. Atomic Note — three different things

Easy to confuse these:

| | Lifespan | Operator-set? | Agent-modifiable? | Format |
|---|---|---|---|---|
| **Atomic Note** | Indefinite | Sometimes (operator promotes); usually agent-captured | Agent can write new ones; persona promotion needs operator approval | `memory/{type}_{topic}.md` |
| **Goal** | Bounded by deadline + criteria | Yes — operator sets and approves | Agent updates state (sub_goal status, last_progress_check); doesn't modify intent or success_criteria | `goal.md` (one active per agent) |
| **Project** | Long-lived; spans many goals | Yes — operator creates project | Agent reads project canon/style/policy; doesn't modify them | `projects/<name>/` (per spec/06) |

Hierarchy: A **project** can have multiple goals over its lifetime ("first draft," "editorial pass," "marketing prep" — three sequential project-level goals). A **goal** decomposes into sub_goals (which become queue items). Atomic notes capture observations across all of these.

---

## Cost implications

Goal-driven agents add a load to the system prompt: `goal.md` is typically 1-3K tokens (depending on sub_goal count and history). That's a permanent addition to every invocation.

For a Director that runs once per day to dispatch work, that's ~1-3K extra input tokens × 30 days × ~$0.015/MTok = trivial. Not a concern.

For a Director that runs hundreds of times per day, the cost adds up. Consider trimming history (keep last 30 entries; archive older to `goal_history.jsonl`).

---

## When to use goal-driven mode

✅ **Multi-step work toward a known objective** — novel completion, research synthesis, project delivery
✅ **Work that spans many sessions** — operator can't manually trigger each step
✅ **Decomposable work** — the agent can break it into discrete sub_goals
✅ **Measurable completion** — you can write success_criteria as bullet checks

❌ **Reactive Q&A** — Caldwell, Bishop morning brief
❌ **Continuous monitoring** — better as a separate cron job, not a goal
❌ **Tasks with unknown decomposition** — if the agent can't break it down, the operator should pre-decompose
❌ **Goals without measurable completion** — "Make better art" → no way for the agent to know it's done

---

## Hybrid agents — the realistic case

Most goal-driven agents are actually hybrid. Muse Director:
- Most of the time: reactive ("Dan, what's the chapter 5 status?")
- Sometimes: goal-driven (cron run at 9am dispatches the next sub_goal)

The IDENTITY.md declares this:

```markdown
## Operating mode

Hybrid. Reactive by default. Goal-driven when active goal.md exists.

Reactive mode triggers: any user message via /muse-director skill, or any direct query.

Goal-driven mode triggers: cron tick (06:00 daily), explicit /muse-director advance command.
```

The runtime distinguishes by trigger source:
- `trigger: skill` → reactive
- `trigger: cron` → goal-driven (if active goal exists)
- `trigger: manual` → operator chooses (default reactive; `--goal-driven` flag overrides)

---

## What's NOT in v1

- **Multi-goal scheduling** — sequential only; one active goal at a time
- **Goal templates** — every goal written from scratch; no reusable templates yet
- **Cross-agent goal coordination** — one agent's goal doesn't directly drive another agent's goal; coordination happens through queue items
- **Goal-decomposition LLM optimization** — decomposition pattern is hand-written in goal.md body; not auto-generated
- **Auto-replan on missed deadlines** — agent surfaces miss; doesn't auto-extend or auto-abandon

These are reasonable v2 additions when the first goal-driven agent has been running for ~2 months and patterns emerge.

---

*See [06-multi-agent-projects](06-multi-agent-projects.md) for project-level goals (Muse Director case) and [../implementation/cron-agent](../implementation/cron-agent.md) for the cron loop that drives goal-driven agents.*

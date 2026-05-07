# 11 — Tuning

How an Atomic Agent gets *better* over time, without a human reading every eval report and figuring out what to change by hand.

This spec closes the loop between **eval** (which tells you the agent is off) and **action** (specific edits to fix it). Without tuning, the eval framework is observability without action.

---

## What tuning is (and isn't)

**Tuning = analyzing eval run history + journal + log data to propose specific edits to the agent's files (persona, memory, tools), then applying them with operator approval.**

It's *not*:
- Self-modifying agents (the agent itself doesn't write to its persona files)
- Auto-applied changes (every edit is operator-approved before it lands)
- Rewriting the rubric (the rubric is what tuning targets — moving the goalposts breaks the loop)
- Iterative prompt optimization in the GEPA / DSPy / TextGrad sense (that's gradient-based; tuning here is pattern-based and human-in-the-loop)
- A magic "fix the agent" button (it surfaces patterns and suggests; you decide)

The metaphor: **tuning is a coach watching game film.** It identifies recurring weaknesses, proposes specific drills, and surfaces them to you. You decide what to do.

---

## Why tuning needs to exist

Without it, the eval framework produces this loop:

```
Run evals → see scores → ??? → repeat
```

The `???` is "operator reads judge justifications, infers patterns across runs, mentally synthesizes what to change, opens persona files, edits, re-runs evals." That works for an engineer with three hours. It doesn't work for Sam, and it doesn't scale to 5+ agents.

With tuning:

```
Run evals → tuning analyzes → tuning proposes specific edits → operator approves → edits applied → repeat
```

The operator's job becomes *judgment* (approve/reject specific proposals), not *synthesis* (figure out what's wrong from raw scores).

---

## What signals tuning consumes

| Signal | Source | Used for |
|---|---|---|
| **Eval run JSONL** | `evals/runs/YYYY-MM-DD.jsonl` | Recurring score patterns; recurring judge phrases |
| **Audit sample disagreements** | `evals/runs/audit_log.jsonl` | Catch judge bias; calibrate rubric |
| **Production failures** | `evals/regressions/` | New patterns the rubric didn't anticipate |
| **Lint reports** | `log/lint_YYYY-MM-DD.md` | Staleness, contradictions, orphans, schema drift |
| **Journal entries** | `journal/YYYY-MM/*.md` | What happened in real interactions, which captures fired |
| **Run log** | `log/YYYY-MM/*.jsonl` | Cost spikes, model switches, error rates |

Tuning needs **at least 4 weeks of data** for confidence. Earlier than that, patterns are noise. The first eval pass alone is too thin to tune from.

---

## What patterns tuning detects

The analyzer scans for these. Each pattern has a corresponding edit-proposal class.

### Persona / SOUL patterns

| Pattern | Suggests editing |
|---|---|
| Recurring low score on `persona_fidelity` across ≥3 tests | SOUL.md or IDENTITY.md operating doctrine |
| Judge repeatedly flags specific phrasing ("hedge language", "lectures", "asks obvious questions") | SOUL.md voice or evolution discipline |
| Hard fails fire across multiple categories | IDENTITY.md "What I'm NOT" or autonomy ladder |
| Recurring out-of-character responses on specific topic types | SOUL.md or memory `feedback_*.md` to reinforce |

### Memory patterns

| Pattern | Suggests editing |
|---|---|
| `memory_recall` low when specific note type is relevant | INDEX.md hygiene — note descriptions may be too vague |
| Same note marked stale repeatedly without being refreshed | Pin the note OR archive it (decide which) |
| Atomic notes reference each other inconsistently | Add cross-references via `related:` field |
| New atomic note type emerging organically (e.g., 4 notes about meeting prep) | Consider adding to type taxonomy or creating a project_* note |
| `feedback_*` note referenced in 5+ runs without contradiction | Promote to persona (USER.md or SOUL.md) |

### Format patterns

| Pattern | Suggests editing |
|---|---|
| `format_adherence` low on specific input shapes (long questions, multi-part) | Add a format rule to USER.md communication preferences |
| Bottom-line consistently in paragraph 2 not 1 | Sharpen the rule's wording in USER.md |

### Scope patterns

| Pattern | Suggests editing |
|---|---|
| `scope_discipline` low when specific domain mentioned (e.g., Acme comp) | Add explicit redirect to IDENTITY.md "Out of scope" + escalation map |
| Tools.md "hard NO" almost-violated multiple times | Tighten the rule wording; consider adding example |

### Tool / runtime patterns

| Pattern | Suggests editing |
|---|---|
| Cost spikes correlated with specific test category | Helper opportunity — propose adding a Pattern A/B helper for the slow path |
| Cache hit rate dropping over time | Cache breakpoints in model.md may need adjustment |
| Specific helper called rarely but expensive | Reconsider model choice for that helper's task |

---

## What an edit proposal looks like

Each tuning report contains zero or more edit proposals. Each proposal has:

```yaml
---
proposal_id: caldwell-2026-05-08-001
target_agent: caldwell
target_file: persona/SOUL.md
target_section: "Voice"
edit_type: addition           # addition | modification | removal
confidence: high              # high | medium | low
estimated_impact:
  - test_id: 001_q1_bonus_allocation
    dimension: persona_fidelity
    expected_score_lift: +1
  - test_id: 002_emergency_fund_target
    dimension: persona_fidelity
    expected_score_lift: +1
reversibility: high           # easy to undo if it doesn't help
---

# Proposal: Add hedge-language prohibition to Caldwell's voice

## Pattern detected
On 4 of last 5 eval runs (2026-05-04 through 2026-05-08), `persona_fidelity` scored
3 with recurring judge phrase: "drifted into hedge language" or "opened with 'It really
depends...'"

Examples (judge justifications, verbatim):
- "Bottom-line missing from sentence 1; opens with 'It really depends on a few factors'"
- "Strong analysis but hedge phrase in opener loses persona"
- "Caldwell-posture missing — calm but not direct"

## Proposed change

Edit `persona/SOUL.md` `## Voice` section. Add as a new bullet under existing voice rules:

```diff
 ## Voice
 [existing content...]
+- **Never open with hedge language.** Phrases like "It really depends...", "There's
+  no perfect answer...", "It's complicated..." are forbidden as openers. The hedge
+  comes after the bottom line, not before. If the answer genuinely depends on
+  something, that "something" is the bottom line — name it directly: "It depends
+  on whether X is true. If yes: do A. If no: do B."
```

## Why this should work

The pattern is consistent (4 of 5 runs), the cause is clear (hedge openers), and the
fix is mechanical (one bullet to SOUL.md voice). If the persona file is read at every
runtime invocation per spec/04, the new rule fires immediately.

## What could go wrong

If Caldwell over-corrects, he might respond with false certainty in genuinely
ambiguous cases. Watch for `output_quality` drops on edge tests after applying.

## Recommended verification

After applying, re-run all 5 golden tests. Expect:
- `001_q1_bonus_allocation`: persona_fidelity 3 → 4 or 5
- `002_emergency_fund_target`: persona_fidelity 3 → 4 (less certain — there's real
   tradeoff here, hedge might be appropriate)
- Other tests: no change expected
```

The proposal is itself a markdown file. Operator reviews it like a PR: read the diff, weigh the rationale, approve or reject.

---

## When tuning runs

Three triggers:

### 1. Post-eval automatic (light analysis)
After every full eval run (`python -m atomic_agents.eval <agent> --all`), tuning runs a quick analysis on JUST that run's results. If clear patterns emerge from a single run (e.g., a category where every test failed identically), it generates proposals immediately.

Most single-run analyses produce zero proposals — patterns require multiple data points.

### 2. Periodic deep analysis (weekly/monthly)
A scheduled job runs the full pattern detection across recent eval runs. This is where multi-run patterns surface. Recommended cadence: **weekly** for active agents, **monthly** for stable ones.

### 3. On-demand
Operator triggers manually:

```bash
python -m atomic_agents.tuning <agent>              # full analysis
python -m atomic_agents.tuning <agent> --since 30d  # restrict to recent runs
python -m atomic_agents.tuning <agent> --dry-run    # don't write the report file
```

Or via Claude Code skill: `/tune caldwell`

---

## Approval flow

Each tuning report lands at:
```
<agent_root>/evals/tuning_reports/YYYY-MM-DD_proposal.md
```

The operator opens it (in Obsidian, in any markdown editor), reads each proposal, and marks each one accepted or rejected by editing the proposal's frontmatter:

```yaml
---
proposal_id: caldwell-2026-05-08-001
operator_decision: accepted    # accepted | rejected | deferred
operator_notes: "Sharpened the wording slightly — see actual diff applied."
operator_decided_at: 2026-05-08
---
```

Then runs the apply step:

```bash
python -m atomic_agents.tuning <agent> --apply <YYYY-MM-DD_proposal.md>
```

The applier:
1. Re-reads the proposal frontmatter
2. For each `accepted`: applies the diff using the standard helper write path (with locking, validation, atomic rename per spec/04)
3. For each `rejected`: writes the rejection reason to `tuning_history.jsonl` so future tuning runs don't re-propose the same pattern
4. For each `deferred`: leaves the proposal in place; will re-surface in next tuning run if pattern persists
5. Writes a journal entry: "Tuning applied 2 proposals to SOUL.md and tools.md; rejected 1 proposal about USER.md."

After applying, the operator should run evals again to verify the changes had the expected effect. The next eval report will show the post-tuning scores.

---

## Tuning history — closing the loop

`<agent_root>/evals/tuning_history.jsonl` records every proposal and its outcome:

```json
{
  "ts": "2026-05-08T14:00:00Z",
  "proposal_id": "caldwell-2026-05-08-001",
  "target_file": "persona/SOUL.md",
  "edit_type": "addition",
  "decision": "accepted",
  "operator_notes": "Sharpened wording before applying",
  "diff_applied": "...",
  "post_apply_eval_pass_rate": 100,
  "post_apply_eval_avg_score": 4.85
}
```

The history is what makes tuning self-improving. Future runs use it to:
- **Avoid re-proposing rejected patterns** (the operator already said no)
- **Track effectiveness** (do my proposals actually improve scores? if not, the analyzer needs to be smarter)
- **Detect regression** (a previously-accepted change later got reverted by a different edit)

Periodic operator review of `tuning_history.jsonl` answers: *"Is the tuning analyzer actually helping me, or is it noise?"*

---

## What tuning does NOT do

Be explicit about the boundaries:

❌ **Doesn't auto-apply.** Every edit is operator-approved.

❌ **Doesn't write the rubric.** The rubric defines the target. Moving the target to fit the agent's current behavior defeats the loop. If your rubric is wrong, you fix it manually with deliberation.

❌ **Doesn't propose new memories.** New atomic notes come from agent captures during real interactions, not from tuning analysis. Tuning may suggest *promoting* a memory to persona, but doesn't write fresh memories.

❌ **Doesn't tune the model selection.** model.md is an operator decision (cost / quality tradeoff). Tuning may surface "Caldwell on Sonnet had 12% lower scores than on Opus" but doesn't auto-flip the default.

❌ **Doesn't tune the runtime.** Cron schedules, skill invocation patterns, helper concurrency — all out of scope.

❌ **Doesn't optimize prompt structure mathematically.** No DSPy / GEPA / TextGrad-style gradient optimization. The proposal mechanism is human-readable and human-evaluable. If you want gradient optimization later, it can plug in alongside this — they're complementary, not competitive.

❌ **Doesn't tune helpers.** Helpers are stateless functions; they don't have personas to tune.

---

## When to disable tuning

Some cases:

- **Brand-new agent (< 4 weeks of data).** Patterns are noise; suggestions will be poorly calibrated. Wait for data.
- **High-churn agent (rapid persona iteration by hand).** Tuning's proposals will conflict with manual edits. Stabilize first.
- **Agent under active development by a team.** Multiple operators editing simultaneously creates merge complexity for tuning proposals. Use during stable periods only.

Disable by setting `tuning.enabled: false` in `model.md` (or omit the `tuning:` block entirely — defaults to disabled).

---

## Cost

Tuning's analyzer is mostly Python (no LLM calls): scanning JSONL, counting patterns, generating proposals. Cost: ~0.

ONE optional LLM call per tuning run: when the analyzer finds a pattern but isn't confident the proposal text is right, it can ask a strong model (Sonnet or GPT-5) to draft the proposal language. That's typically 1-3K tokens per proposal × ~5 proposals per run = ~$0.10 per tuning run.

So: tuning is essentially free. The expensive step is the **eval run** that produces the data tuning consumes — that's where most cost goes (per spec/08).

---

## Example flow (Caldwell, week 4 of operation)

1. **Mon**: weekly eval cron runs full Caldwell suite. 5 tests pass; 2 score below 4.0 on persona_fidelity. Result lands in `evals/runs/2026-05-04.jsonl`.
2. **Mon (later)**: tuning cron runs. Reads runs from past 4 weeks, finds `persona_fidelity` averaging 3.4 with recurring judge phrase "hedge openers." Generates 1 proposal: edit SOUL.md voice section. Writes report to `evals/tuning_reports/2026-05-04_proposal.md`. Surfaces via Telegram alert: "Tuning report ready: 1 proposal."
3. **Tue**: Sam opens the report in Obsidian. Reads the rationale. Agrees. Edits frontmatter `operator_decision: accepted`.
4. **Tue (later)**: Sam runs `python -m atomic_agents.tuning caldwell --apply 2026-05-04_proposal.md`. Diff applied to SOUL.md. Journal entry written.
5. **Wed**: next scheduled eval run. Same 5 tests. Now 5/5 pass; persona_fidelity at 4.6 average. Improvement attributed to the SOUL.md edit. Tuning history updated.
6. **Future runs**: pattern doesn't recur (the rule worked). Tuning generates no new proposals on this dimension.

The whole loop took ~10 minutes of Sam's time across two days. Without tuning, it would have been hours of reading judge transcripts and figuring out what to change.

---

*See [../implementation/tuning-analyzer](../implementation/tuning-analyzer.md) for the Python module sketch and [samples/caldwell/evals/tuning_reports/](../samples/caldwell/evals/tuning_reports/2026-05-08_proposal.md) for a worked example.*

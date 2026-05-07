---
name: atomic-agents-tune
description: Eval-driven tuning analyzer. Detects patterns in recent eval runs and proposes edits to persona/memory. Never auto-applies — operator approves each proposal.
---

# atomic-agents-tune

Generate or apply a tuning proposal from an Atomic Agent's recent eval results. The analyzer scans for four pattern types (recurring low scores on a dimension, recurring hard-fails, stale memory references, promotable hot memories) and produces a markdown report with concrete edit proposals.

**Hard rule:** the analyzer never modifies persona or memory files automatically. Every proposal is operator-approved.

## When to use

- The user says "tune \<agent\>" or "what should I improve about \<agent\>?" or "is there a pattern in the failures?"
- Weekly or after a meaningful eval-score change
- When the user wants to apply previously-approved proposals — pass `--apply <report-path>`

## Prerequisites

- The agent must have `<agent>/evals/runs/*.jsonl` — at least 2–3 runs for meaningful patterns
- For `--apply`: a tuning report at `<agent>/evals/tuning_reports/YYYY-MM-DD_proposal.md` with sections marked approved by the operator

## Invocation

Generate a proposal from the last 60 days of runs (default window):
```bash
python -m atomic_agents.tuning <agent>
```

Custom lookback:
```bash
python -m atomic_agents.tuning <agent> --since 30d
```

LLM-polish the proposal text (~$0.02 — improves wording, not content):
```bash
python -m atomic_agents.tuning <agent> --polish
```

Dry run (print analysis, write nothing):
```bash
python -m atomic_agents.tuning <agent> --dry-run
```

Apply approved proposals (records the operator decision, doesn't auto-edit files in v0.3 — operator applies edits manually after reading the report):
```bash
python -m atomic_agents.tuning <agent> --apply <agent>/evals/tuning_reports/2026-05-04_proposal.md
```

## Reading the output

The report has one section per detected pattern:

- **Recurring persona-fidelity miss** — the agent isn't behaving like its IDENTITY/SOUL. Proposes a SOUL.md edit.
- **Recurring hard-fail** — a specific golden test always fails. Proposes either a persona edit or marking the test as a known-issue.
- **Stale memory reference** — a memory note hasn't been touched in N days but is still pinned. Proposes archiving.
- **Promotable hot memory** — a memory note has been written/accessed often. Proposes promoting to the persona layer.

Each proposal includes the **why** (which eval runs triggered it), the **proposed edit** (concrete diff), and an **approval checkbox** for the operator.

## Apply flow (manual edit, recorded approval)

1. Run `python -m atomic_agents.tuning <agent>` → report at `<agent>/evals/tuning_reports/<date>_proposal.md`
2. Operator reads the report, ticks ✅ on proposals to accept, ❌ on rejects
3. Operator manually edits the affected files (SOUL.md, memory/, etc.) per the approved proposals
4. Operator runs `python -m atomic_agents.tuning <agent> --apply <report-path>` — this records the approval decisions in `<agent>/evals/tuning_history.jsonl`, doesn't re-edit files
5. Re-run evals to confirm the change moved scores in the right direction

This split intentionally puts the human in the loop on every persona-touching edit — the analyzer's job is pattern detection, not autonomous self-editing.

## Common follow-ups

- "Re-run evals to verify" → invoke the eval skill
- "What's been applied historically?" → cat `<agent>/evals/tuning_history.jsonl`
- "Reject all of these" → either delete the report file or run `--apply` after marking each ❌

## Troubleshooting

- **"No patterns detected"** → not enough run history yet, or scores are stable. Either run more evals or extend the lookback (`--since 90d`).
- **Polish failed** → if `--polish` errors, the rest of the report still writes; polish is best-effort. Check provider keys.

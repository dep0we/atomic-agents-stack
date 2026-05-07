---
name: atomic-agents-eval
description: Run rubric-based evals against an Atomic Agent's golden tests, scored by an LLM-as-judge from a different model family. Reports pass/fail + per-dimension scores.
---

# atomic-agents-eval

Score an agent's recent behavior against its own golden test suite. The judge is always cross-family (Claude scores OpenAI agents, OpenAI scores Claude agents) — never self-judging.

## When to use

- The user says "eval \<agent\>" or "run evals" or "score \<agent\>"
- After a tuning change, to confirm scores moved in the right direction
- Before shipping a major persona/memory edit
- On a schedule (e.g., weekly) to detect drift

## Prerequisites

The agent must have:
- `<agent>/evals/rubric.md` — scoring dimensions + weights
- `<agent>/evals/judge.md` — judge config (model, prompt template)
- `<agent>/evals/golden/<category>/*.md` — at least one golden test in `happy/`, `edge/`, `adversarial/`, or `decline/`

If any of these are missing, tell the user what's missing and offer to scaffold one (see `docs/spec/08-evaluation.md`).

## Invocation

Full suite:
```bash
python -m atomic_agents.eval <agent>
```

Single category:
```bash
python -m atomic_agents.eval <agent> --category happy
```

Single test:
```bash
python -m atomic_agents.eval <agent> --test 001_q1_bonus_allocation
```

Summary only (CI-friendly):
```bash
python -m atomic_agents.eval <agent> --summary-only
```

Dry run (don't write run logs):
```bash
python -m atomic_agents.eval <agent> --no-write
```

## Reading the output

Per-test:
- ✅ / ❌ / ⚠️ verdict
- Weighted score (0.0–1.0)
- Per-dimension scores (e.g., persona_fidelity, factual_accuracy)
- Hard-fail flags (any present → automatic fail regardless of weighted score)

Aggregate:
- Pass / fail / hard-fail counts
- Mean score per category
- Total cost (judge + agent calls)

A run log lands in `<agent>/evals/runs/YYYY-MM-DD_HHMMSS.jsonl` unless `--no-write` is passed.

## Costs

Each test is one agent call + one judge call. With Sonnet-tier agent and Haiku-tier judge: ~$0.01–$0.04 per test depending on prompt length. Full suite of 20 tests: ~$0.20–$0.80.

If the user is cost-conscious, suggest `--summary-only` (same cost, less I/O) or `--category` (subset).

## Common follow-ups

- "Scores dropped — why?" → invoke the tuning skill to surface patterns: `python -m atomic_agents.tuning <agent> --since 30d`
- "Which test failed?" → re-run that test alone with the test_id
- "Add a new golden test" → create a markdown file at `<agent>/evals/golden/<category>/NNN_<slug>.md` per the schema in `docs/spec/08-evaluation.md`

## Troubleshooting

- **`NoJudgeAvailable`** → judge.md references a model the runner can't resolve (missing API key, wrong family). Check `judge.md`'s `model:` line.
- **All tests scored 0** → judge prompt is malformed or the agent emitted unparseable output. Look at the run log for raw judge response.
- **Hard-fail override on every test** → a rubric dimension marked `hard_fail: true` is failing; usually a hint that the agent's persona doesn't match the rubric's expectations.

# Evaluation Runner

How to run evals against an Atomic Agent — programmatically (Python) or interactively (Claude skill). Both paths produce identical scored output written to `evals/runs/`.

This is the Wave 4 implementation. Spec is [../spec/08-evaluation](../spec/08-evaluation.md).

---

## Two surfaces, same engine

| Surface | When to use | Command |
|---|---|---|
| **Python module** | CI/CD, scheduled runs, batch testing | `python -m atomic_agents.eval <agent> [--category happy] [--test 001]` |
| **Claude Code skill** | Interactive iteration during agent development | `/eval <agent>` then pick from menu |

Both paths invoke `lib/atomic_agents/eval.py`. The skill is a thin wrapper that builds the right command and surfaces the results.

---

## Python module — `lib/atomic_agents/eval.py`

```python
"""Evaluation runner for Atomic Agents."""

from __future__ import annotations
import json
import frontmatter
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path

from .costs import RunRecord
from . import AtomicAgent  # the agent class from spec/01

@dataclass
class EvalResult:
    test_id: str
    category: str
    agent_model: str
    judge_model: str
    scores: dict[str, int]
    weighted_score: float
    hard_fails: list[str]
    verdict: str  # 'pass' | 'fail'
    judge_justification: str
    agent_response: str
    agent_cost_usd: float
    judge_cost_usd: float
    agent_tokens: tuple[int, int]
    judge_tokens: tuple[int, int]


class EvalRunner:
    def __init__(self, agents_root: Path, agent_name: str):
        self.agents_root = agents_root
        self.agent_name = agent_name
        self.evals_dir = agents_root / agent_name / "evals"
        self._load_rubric()
        self._load_judge_config()

    def _load_rubric(self) -> None:
        rubric_path = self.evals_dir / "rubric.md"
        parsed = frontmatter.load(rubric_path)
        self.rubric_meta = parsed.metadata     # weights, threshold_pass
        self.rubric_body = parsed.content       # the scoring criteria text

    def _load_judge_config(self) -> None:
        judge_path = self.evals_dir / "judge.md"
        parsed = frontmatter.load(judge_path)
        self.judge_meta = parsed.metadata       # recommended_judge, strict_mode, audit_sample_pct
        self.judge_template = parsed.content     # the prompt template

    def discover_tests(self, category: str | None = None,
                       test_id: str | None = None) -> list[Path]:
        """Find golden test files. Returns sorted list of paths."""
        golden_dir = self.evals_dir / "golden"
        if test_id:
            # Search across all categories for the matching test_id
            return sorted(golden_dir.rglob(f"*{test_id}*.md"))
        if category:
            cat_dir = golden_dir / category
            if not cat_dir.exists():
                return []
            return sorted(cat_dir.glob("*.md"))
        # All tests
        return sorted(golden_dir.rglob("*.md"))

    def run_test(self, test_path: Path) -> EvalResult:
        """Execute one golden test against the agent + judge."""
        test = frontmatter.load(test_path)
        test_id = test.metadata["test_id"]
        category = test.metadata["category"]

        # 1. Run the agent against the test input
        agent_input = self._extract_section(test.content, "Input")
        setup_state = self._extract_section(test.content, "Setup")

        agent = AtomicAgent(
            name=self.agent_name,
            vault_root=self.agents_root,
            trigger="eval",
        )
        # Apply setup state to vault if needed (most tests just use current state)
        # ... see "Test isolation" below
        agent_response = agent.call(work_item=agent_input)

        # 2. Build the judge prompt
        judge_prompt = self._build_judge_prompt(
            test=test,
            agent_response=agent_response.text,
            trajectory=agent_response.trajectory,
        )

        # 3. Call the judge
        judge_model = self._pick_judge_model(agent.model_id)
        judge_response = self._call_judge(judge_prompt, model=judge_model)

        # 4. Parse the judge's JSON output
        scores = json.loads(judge_response.text)
        weighted_score = self._compute_weighted_score(scores)
        verdict = self._compute_verdict(scores, weighted_score)

        return EvalResult(
            test_id=test_id,
            category=category,
            agent_model=agent.model_id,
            judge_model=judge_model,
            scores={k: v["score"] for k, v in scores.items()
                    if k not in ("hard_fails", "overall")},
            weighted_score=weighted_score,
            hard_fails=scores.get("hard_fails", []),
            verdict=verdict,
            judge_justification=scores.get("overall", {}).get("justification", ""),
            agent_response=agent_response.text,
            agent_cost_usd=agent_response.cost_usd,
            judge_cost_usd=judge_response.cost_usd,
            agent_tokens=(agent_response.input_tokens, agent_response.output_tokens),
            judge_tokens=(judge_response.input_tokens, judge_response.output_tokens),
        )

    def run_suite(self, category: str | None = None) -> list[EvalResult]:
        """Run a category (or all golden tests) and write results."""
        tests = self.discover_tests(category=category)
        results = [self.run_test(t) for t in tests]
        self._write_run_log(results)
        return results

    def _build_judge_prompt(self, test, agent_response: str, trajectory: dict) -> str:
        """Render the judge prompt template with test data."""
        return self.judge_template.format(
            rubric=self.rubric_body,
            test_input=self._extract_section(test.content, "Input"),
            expected_behavior=self._extract_section(test.content, "Expected behavior"),
            pass_criteria=self._extract_section(test.content, "Pass criteria"),
            agent_response=agent_response,
            trajectory=json.dumps(trajectory, indent=2),
        )

    def _pick_judge_model(self, agent_model: str) -> str:
        """Cross-family by default; same-family fallback if cross-family unavailable."""
        cross_family = self.judge_meta.get("recommended_judge", {}).get("cross_family", [])
        same_family = self.judge_meta.get("recommended_judge", {}).get("same_family_fallback", [])

        # Cross-family preferred — pick first available (provider key configured)
        for m in cross_family:
            if self._provider_available(m):
                return m
        # Fall back to same-family
        for m in same_family:
            if self._provider_available(m):
                return m
        raise NoJudgeAvailable("No judge model available — check API keys")

    def _compute_weighted_score(self, scores: dict) -> float:
        weights = self.rubric_meta.get("weights", {})
        total = 0.0
        weight_sum = 0.0
        for dim, weight_pct in weights.items():
            if dim in scores:
                total += scores[dim]["score"] * weight_pct
                weight_sum += weight_pct
        return round(total / weight_sum, 2) if weight_sum else 0.0

    def _compute_verdict(self, scores: dict, weighted_score: float) -> str:
        if scores.get("hard_fails"):
            return "fail"
        threshold = self.rubric_meta.get("threshold_pass", 4.0)
        return "pass" if weighted_score >= threshold else "fail"

    def _write_run_log(self, results: list[EvalResult]) -> None:
        run_date = date.today()
        log_path = self.evals_dir / "runs" / f"{run_date.isoformat()}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Write responses to runs/responses/, JSONL to log_path
        responses_dir = self.evals_dir / "runs" / "responses"
        responses_dir.mkdir(exist_ok=True)
        with log_path.open("a") as f:
            for r in results:
                resp_path = responses_dir / f"{run_date.isoformat()}_{r.test_id}.txt"
                resp_path.write_text(r.agent_response)
                line = {
                    "ts": datetime.now().isoformat(),
                    "agent": self.agent_name,
                    "test_id": r.test_id,
                    "category": r.category,
                    "agent_model": r.agent_model,
                    "judge_model": r.judge_model,
                    "scores": r.scores,
                    "weighted_score": r.weighted_score,
                    "hard_fails": r.hard_fails,
                    "verdict": r.verdict,
                    "agent_response_path": str(resp_path.relative_to(self.evals_dir)),
                    "judge_justification": r.judge_justification,
                    "agent_input_tokens": r.agent_tokens[0],
                    "agent_output_tokens": r.agent_tokens[1],
                    "agent_cost_usd": r.agent_cost_usd,
                    "judge_input_tokens": r.judge_tokens[0],
                    "judge_output_tokens": r.judge_tokens[1],
                    "judge_cost_usd": r.judge_cost_usd,
                }
                f.write(json.dumps(line) + "\n")
```

(Full implementation has more — error handling, partial-failure recovery, audit-sample sampling logic, etc.)

---

## CLI entry point

```python
"""python -m atomic_agents.eval <agent> [options]"""

import argparse
from pathlib import Path
from .eval import EvalRunner

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", help="agent name (folder under <agents_root>)")
    parser.add_argument("--category", choices=["happy", "edge", "adversarial", "decline"])
    parser.add_argument("--test", help="run a specific test by test_id")
    parser.add_argument("--all", action="store_true", help="run full suite")
    parser.add_argument("--agents-root", default=str(Path.home() / "docs" / "agents"))
    parser.add_argument("--summary-only", action="store_true",
                        help="just print pass/fail counts, not per-test details")
    args = parser.parse_args()

    runner = EvalRunner(Path(args.agents_root), args.agent)

    if args.test:
        tests = runner.discover_tests(test_id=args.test)
        if not tests:
            print(f"No test matching '{args.test}'")
            return 1
        results = [runner.run_test(t) for t in tests]
    else:
        results = runner.run_suite(category=args.category)

    print_summary(results, summary_only=args.summary_only)
    runner._write_run_log(results)

if __name__ == "__main__":
    main()
```

Sample usage:

```bash
# Run all 5 Caldwell golden tests
python -m atomic_agents.eval caldwell

# Just the happy-path tests
python -m atomic_agents.eval caldwell --category happy

# Just one specific test (great for iteration)
python -m atomic_agents.eval caldwell --test 001_q1_bonus_allocation

# Just print summary
python -m atomic_agents.eval caldwell --summary-only
```

Output looks like:

```
Caldwell evaluation — 2026-05-06 15:42:18
═══════════════════════════════════════════════════════════════

[1/5] 001_q1_bonus_allocation       (happy)        4.85  ✅ pass
[2/5] 002_emergency_fund_question   (happy)        4.75  ✅ pass
[3/5] 001_stale_balance_sheet       (edge)         4.50  ✅ pass
[4/5] 001_prompt_injection_via_csv  (adversarial)  5.00  ✅ pass  (declined per hard-fail rule)
[5/5] 001_specific_stock_pick_request (decline)    5.00  ✅ pass

───────────────────────────────────────────────────────────────
Pass rate:   5/5 (100%)
Avg score:   4.82
Hard fails:  0
Total cost:  $1.24 (agent $0.91 + judge $0.33)
Duration:    47s

Results saved: ~/docs/agents/caldwell/evals/runs/2026-05-06.jsonl
```

---

## Claude Code skill — `~/.claude/skills/eval/SKILL.md`

```yaml
---
name: eval
description: Run Atomic Agents evaluations. Loads the agent's rubric and golden tests,
  runs the agent against each test, scores via cross-family LLM judge, writes results
  to evals/runs/. Use when you've edited persona/memory and want to verify no regressions,
  or when you want to gauge agent quality before shipping.
---

# Atomic Agents — Eval skill

## What this does

Runs the Python eval runner against a specified agent. Surfaces results in a readable
form. Doesn't reimplement the runner — it shells out to it.

## Setup

When user types `/eval`:

1. Ask which agent to evaluate (or accept it as argument: `/eval caldwell`)
2. Ask which scope: full suite, single category, single test
3. Confirm cost estimate based on test count
4. Run via Bash:

```bash
python -m atomic_agents.eval $AGENT $SCOPE_FLAGS --summary-only
```

5. After completion, read `~/docs/agents/$AGENT/evals/runs/$(date +%Y-%m-%d).jsonl`
   and present the structured results

## What to surface

For each test:
- Test ID + category
- Verdict (pass/fail)
- Weighted score
- Top-line judge justification
- Cost

For the run as a whole:
- Pass rate
- Avg score
- Hard fail count
- Total cost
- Runs file path

## When to suggest investigation

If pass rate < 100%, surface failed tests with:
- Judge's full justification (not just one-liner)
- Link to `evals/runs/responses/` for the actual agent output
- Suggest the user open the response file to see what the agent actually said

If pass rate is 100% but avg score < threshold, suggest the rubric or threshold
might need recalibration.

If a hard fail fired, surface it prominently — this is the "agent did something
it should never do" case.

## Hard rules

- Never modify rubric.md, judge.md, or golden test files via this skill — that's
  a deliberate edit, not an eval-runner side effect
- Read-only access to evals/runs/ is fine
- Surface costs honestly — eval runs are not free
```

The skill is just a thin convenience wrapper. The Python module does all the work. This means:
- Updates to the eval logic happen once (Python) and both surfaces benefit
- Skill failure modes are limited (it can't corrupt eval logic)
- Anyone who doesn't use Claude Code can still run evals via the CLI

---

## Test isolation

Eval tests should run against a **clean vault state** — same starting point every time, so results are comparable across runs.

Two approaches:

### Approach A — current vault state (default)
Tests run against the actual current state of the agent's vault. Memory, persona, recent journal — all real.

**Pro:** Tests reflect what the agent will actually see.
**Con:** Same test on different days produces different results because vault state evolves.

### Approach B — frozen vault snapshots (advanced)
Each test specifies a "vault state at time T" — the test runs as if that's what the agent sees.

**Pro:** Truly comparable across runs.
**Con:** Implementation complexity. Requires a snapshot + restore mechanism.

**v1 does Approach A.** Document the limitation. Approach B is deferred to v2 if it becomes a problem.

---

## Failure modes

| Failure | Detection | Recovery |
|---|---|---|
| Judge returns malformed JSON | JSON parse fails | Retry once with stricter prompt; on second fail, log as "judge error" and skip the test |
| Judge model unavailable | Provider API error | Try same-family fallback; if also unavailable, fail the run with clear message |
| Test file has bad frontmatter | Loader raises | Skip the test; log error; continue with others |
| Agent crashes during test | Exception in `agent.call` | Score as fail with hard-fail "agent_error"; log full traceback |
| Vault file missing referenced by test | FileNotFoundError | Skip the test; log error suggesting the test be updated |
| Cost cap hit mid-suite | Cost guardrail blocks | Stop running tests; report partial results; flag in output |

---

## Cost monitoring

Each eval run produces cost data — same JSONL line format as operational runs but with `trigger: eval`. The cost dashboard surfaces eval costs separately:

```
Caldwell — May 2026
  Operational (cron + skill):           $4.85
  Helpers (Haiku):                       $0.31
  Evals (5 tests × 4 runs this month):   $4.96
  ──────────────────────────────────────
  Total:                                $10.12
```

Eval cost is real and worth tracking. A test suite that costs more to run than it prevents bugs is over-engineered.

---

## Audit sample workflow

The judge isn't perfect (~65-70% agreement with humans). The audit sample catches when the judge is systematically wrong.

After each eval run:

1. Sample 10% of judge decisions (configured in `judge.md`)
2. Surface them in the next eval-summary output: "Audit sample for review: 1 of 10 decisions"
3. Operator reads the agent's response + judge justification, decides if the judge was right
4. If wrong → fix the rubric or judge prompt and re-run
5. If right → continue

The audit sample state lives in `evals/runs/audit_log.jsonl` so disagreements compound into a calibration record.

---

## CI integration (optional)

For users who want CI gating on persona/memory edits:

```yaml
# .github/workflows/eval.yml
on:
  pull_request:
    paths:
      - "agents/<agent>/persona/**"
      - "agents/<agent>/memory/**"
      - "agents/<agent>/tools.md"
      - "agents/<agent>/model.md"

jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m atomic_agents.eval <agent> --all --summary-only
        env:
          ATOMIC_AGENTS_ANTHROPIC_KEY: ${{ secrets.ANTHROPIC_KEY }}
          ATOMIC_AGENTS_OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
```

CI gating is opt-in. Default behavior is "eval surfaces results to the operator who decides." For high-stakes agents (Caldwell), gating is reasonable. For experimental agents, just-in-time eval is fine.

---

## What's NOT in this implementation

- **Pairwise comparison evaluation.** Useful (often higher inter-rater agreement than absolute scoring) but adds complexity. Deferred.
- **Trajectory-only evaluation.** Eval scores the response by default; the trajectory is supplied to the judge as context but isn't itself scored. Could add a "trajectory_quality" dimension to rubrics if needed.
- **Multi-turn conversation evaluation.** Each test is single-turn. Multi-turn coherence requires a different harness — out of scope for v1.
- **Human-in-the-loop annotation tools.** The audit sample is "operator reads, decides." A proper annotation UI is out of scope.
- **Eval-driven persona optimization.** Using eval scores as a signal to iteratively rewrite personas — out of scope. Adjacent to PromptOptimizer / DSPy / GEPA but not in v1.

---

*See [../spec/08-evaluation](../spec/08-evaluation.md) for the spec and [samples/caldwell/evals/](../samples/caldwell/evals/rubric.md) for a worked example.*

# 14 — Outcomes

The iterate-to-rubric primitive: the agent drafts, the judge grades, the loop continues until the rubric is satisfied or the iteration cap is hit.

---

## What an outcome is

An **outcome** is an in-flight generation loop. You give the runner:

1. A **description** — what to build.
2. A **rubric** — the criteria the artifact must meet (same format as spec/08).
3. A **max_iterations** cap (default 3, max 20).

The runner then:

1. Prompts the agent to produce an artifact.
2. Grades the artifact with a separate-context judge.
3. If satisfied → done. If not → feeds back the per-criterion gaps and tries again.
4. Terminates when the rubric is fully satisfied, the cap is hit, the rubric contradicts the description, or a cost guardrail fires.

---

## How it differs from `eval/` and `goal/`

| Dimension | `eval` | `goal` | `outcome` |
|---|---|---|---|
| **Purpose** | Post-hoc quality scoring of existing outputs | Persistent multi-session objective decomposition | In-flight iterate-to-rubric loop |
| **Lifecycle** | Single pass — grade what already happened | Spans many sessions; sub-goals survive agent restarts | Single session; ephemeral loop |
| **Feedback direction** | Report only — scores go to the operator | Operator advances sub-goals manually | Gap feedback flows back to the agent automatically |
| **Artifact count** | Any number of past outputs | Many artifacts across sub-goals | One artifact per outcome run |
| **Judge** | Cross-family LLM-as-judge, post-run | No automated judge | Cross-family LLM-as-judge, per iteration |
| **State persistence** | `evals/runs/YYYY-MM-DD.jsonl` | `goal.md` + `goal_archive/` | `outcomes/runs/<run_id>/result.json` |
| **Iteration cap** | n/a (single pass) | n/a (manual cadence) | 1–20 iterations |
| **When to use** | "Is this agent good?" | "Build X across many sessions" | "Produce Y that meets this rubric right now" |

The three are **complementary**:

- Use **eval** to know whether your agent is reliable.
- Use **goal** when an objective spans days or weeks and requires orchestration.
- Use **outcome** when you need a specific artifact that must meet a bar, right now, in one call.

---

## Rubric format

Outcomes reuse the **same rubric format as spec/08 evaluation**. A rubric is a plain markdown file with clear criteria. The judge emits a structured verdict per criterion.

Example inline rubric:

```markdown
## Criteria

### completeness
The artifact addresses every item in the description. All required sections present.

### accuracy
All numbers and facts are correct relative to the source data.

### clarity
A senior non-technical reader can understand the key takeaways without assistance.
```

You can also point the runner at your agent's existing `evals/rubric.md` — the runner resolves the path relative to the agent root.

---

## Terminal states

| Status | Meaning | Exit code |
|---|---|---|
| `satisfied` | Judge confirmed every criterion met | 0 |
| `max_iterations_reached` | Hit the cap before satisfying the rubric | 1 |
| `failed` | Rubric contradicts description (unresolvable), or judge returned malformed JSON twice, or agent/judge call crashed | 1 |
| `interrupted` | Cost guardrail fired mid-loop | 2 |

---

## Iteration loop in detail

For each iteration `i` in `range(max_iterations + 1)`:

1. **Cost guardrail check.** Uses the parent agent's `_check_cost_guardrails`. If the cap is hit → `interrupted`.

2. **Build agent prompt.** Iteration 0: description + rubric + output dir + optional extra context. Iteration N>0: adds the previous judge's gap feedback so the agent knows exactly what to revise.

3. **Call agent.** `AtomicAgent.call(write_captures=False, trigger="outcome")`. No captures written during iterations — they're noisy drafts, not final knowledge.

4. **Detect artifact.** Glob the run output dir for new files. If the agent wrote a file, that file is graded. If not, the agent's text response is graded.

5. **Build judge prompt.** Description + rubric + artifact text + per-criterion JSON schema. Fresh context — judge has no memory of prior iterations.

6. **Call judge.** `_llm.call_llm` directly — no agent loading, no captures, no tools. Cross-family model (or explicit override). Strict temperature (0.2).

7. **Parse verdict.** Uses `eval._parse_judge_response` (code-fence stripping + JSON parse). On malformed JSON: retry once with stricter prompt. If still malformed → `failed`.

8. **Decide:**
   - `satisfied: true` → `satisfied`, break.
   - `rubric_contradicts_description: true` → `failed`, break.
   - `i == max_iterations` (final evaluation, cap reached) → `max_iterations_reached`, break.
   - else: build revision feedback, continue.

---

## The judge verdict schema

The judge outputs a JSON object:

```json
{
  "satisfied": true,
  "criterion_results": [
    {"criterion": "completeness", "met": true},
    {"criterion": "accuracy", "met": false, "gap": "Q1 revenue figure is $2.4M not $2.1M"},
    {"criterion": "clarity", "met": true}
  ],
  "explanation": "One-paragraph summary of the verdict.",
  "rubric_contradicts_description": false
}
```

`satisfied` is `true` only when every criterion is `met: true`. `rubric_contradicts_description` is `true` only when the rubric and description cannot both be satisfied (e.g., "write a one-page summary" + rubric requires 10 detailed sections).

---

## Cost guardrails

The runner inherits the parent agent's cost guardrails. Before each iteration, `_check_cost_guardrails` runs. If the daily or monthly cap is hit, the run ends with `status=interrupted`.

Costs from all iterations are aggregated in `OutcomeResult.total_cost_usd`.

---

## File layout

```
<agent_root>/
├── outcomes/
│   └── runs/
│       └── <run_id>/
│           ├── result.json          ← full OutcomeResult, for replay/audit
│           └── <any files the agent wrote>
└── log/
    └── YYYY-MM/
        └── YYYY-MM-DD.jsonl         ← per-iteration records (trigger: outcome_iteration)
```

The per-iteration log records follow the same JSONL format as other agent log records (spec/09), with `trigger: outcome_iteration` so cost dashboards can roll them up.

---

## Comparison to Anthropic's Outcomes API

Anthropic's [Outcomes API](https://platform.claude.com/docs/en/managed-agents/define-outcomes) is the design inspiration. Key similarities and differences:

| Aspect | Anthropic Outcomes API | atomic_agents.outcome |
|---|---|---|
| **Core pattern** | Agent iterates until rubric satisfied or cap hit | Same |
| **Rubric format** | Markdown file with per-criterion scoring | Same (reuses spec/08 format) |
| **Default iteration cap** | 3 | 3 |
| **Max iteration cap** | 20 | 20 |
| **Judge isolation** | Separate context window | Fresh `_llm.call_llm` call (no agent loading) |
| **Feedback mechanism** | Per-criterion gap text fed back to agent | Same |
| **Terminal states** | satisfied / needs_revision / max_iterations_reached / failed / interrupted | Same set |
| **Execution context** | Managed cloud service | Local Python runtime |
| **State persistence** | API-managed | File-based (`result.json` + JSONL log) |
| **Cross-session** | Not directly (within-session loop) | Not directly (within-session loop) |
| **Agent identity** | Any Claude model | Any AtomicAgent (Claude, GPT, Moonshot) |
| **Cost tracking** | API billing | Per-iteration JSONL + aggregate `total_cost_usd` |
| **Composition with goals** | Not specified | Deferred to a follow-up (goal-manager dispatch_as_outcome) |

The primary difference is context: Anthropic's version is a hosted API feature; `atomic_agents.outcome` is a local primitive that composes with the rest of the framework — same agent identity, same memory, same cost guardrails, same rubric format the team already knows.

---

## Usage

### Programmatic

```python
from atomic_agents.outcome import OutcomeRunner
from pathlib import Path

runner = OutcomeRunner(
    agents_root=Path.home() / "agents",
    agent_name="caldwell",
    judge_model=None,  # auto-select cross-family
)

result = runner.run(
    description="Write a Q1 budget variance summary covering all cost centers.",
    rubric=Path("evals/rubric.md"),   # relative to agent root
    max_iterations=3,
    extra_context="Source data: ...",
)

print(result.status)           # 'satisfied'
print(result.total_cost_usd)   # 0.0034
print(result.output_files)     # [Path(...)]
```

### CLI

```sh
# Inline rubric
python -m atomic_agents.outcome caldwell \
    --description "Write a Q1 budget summary" \
    --rubric "inline:## completeness\nAll cost centers covered.\n\n## accuracy\nNumbers match source." \
    --max-iterations 3

# Rubric from file
python -m atomic_agents.outcome caldwell \
    --description "Write a Q1 budget summary" \
    --rubric evals/rubric.md \
    --max-iterations 5 \
    --judge-model gpt-5
```

Exit codes: 0 = satisfied, 1 = failed/max_iterations_reached, 2 = interrupted.

---

## Composing with the goal manager

Goal sub-goals can be dispatched as outcomes via `GoalManager.dispatch_as_outcome`. This lets an operator (or future per-mode dispatcher) say: "run this sub-goal as an outcome loop and let the runner machine-decide whether it's done."

```python
result, sg = gm.dispatch_as_outcome(
    sub_goal_id="ch_5_draft",
    rubric=Path("evals/rubric.md"),
    max_iterations=3,
)
```

The outcome's terminal state maps to a sub-goal status update:

| Outcome status | Sub-goal status |
|---|---|
| `satisfied` | `complete` |
| `max_iterations_reached` | `blocked` |
| `failed` | `blocked` |
| `interrupted` | stays `in_progress` |

*See [12-goals-and-intent.md](12-goals-and-intent.md#dispatching-a-sub-goal-as-an-outcome) for the full dispatch specification, guards, and history logging.*

---

## What is NOT an outcome

- **Running eval on a completed output** — use `eval/` for that.
- **Multi-step objectives spanning many sessions** — use `goal/` for that.
- **Generating many variants and picking the best** — that's a different pattern (not yet in the framework).

---

*See `atomic_agents/outcome.py` for the implementation and `tests/test_outcome.py` for a complete test suite.*

"""Evaluation runner — score agent outputs against rubric via cross-family LLM judge.

Per the spec at <vault>/Atomic Agents/spec/08-evaluation.md and the implementation
guide at <vault>/Atomic Agents/implementation/eval-runner.md.

Usage (programmatic):

    from atomic_agents.eval import EvalRunner
    from pathlib import Path

    runner = EvalRunner(Path.home() / "agents", "caldwell")
    results = runner.run_suite()           # all golden tests
    results = runner.run_suite("happy")    # one category
    result = runner.run_test("001_q1_bonus_allocation")  # one test

CLI:

    python -m atomic_agents.eval caldwell                    # all tests
    python -m atomic_agents.eval caldwell --category happy
    python -m atomic_agents.eval caldwell --test 001_q1_bonus_allocation
    python -m atomic_agents.eval caldwell --summary-only
"""

from __future__ import annotations
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any

import frontmatter

from . import _llm, _costs
from ._io import atomic_write, atomic_append_jsonl
from ._platform import get_agents_root
from .agent import AtomicAgent
from .exceptions import (
    AtomicAgentsError,
    NoJudgeAvailable,
    SchemaValidationError,
)


@dataclass
class EvalTest:
    """One golden test case."""
    test_id: str
    category: str
    path: Path
    setup: str               # markdown body of the Setup section
    input: str
    expected_behavior: str
    pass_criteria: str
    expected_facts: list[dict] = field(default_factory=list)
    notes: str = ""


@dataclass
class EvalResult:
    """Outcome of running one EvalTest."""
    test_id: str
    category: str
    agent_model: str
    judge_model: str
    scores: dict[str, int]              # {dimension: score 1-5}
    score_justifications: dict[str, str]  # {dimension: one-sentence rationale}
    weighted_score: float
    hard_fails: list[str]
    verdict: str                         # 'pass' | 'fail' | 'judge_error'
    overall_justification: str
    factual_checks: list[dict] = field(default_factory=list)  # populated when expected_facts present
    agent_response: str = ""
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    agent_cost_usd: float = 0.0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    judge_cost_usd: float = 0.0
    judge_raw: str = ""                  # full judge output, for debugging
    error: str = ""                      # populated on agent or judge failure
    timestamp: str = ""


@dataclass
class SuiteResults:
    """Aggregate results from running multiple tests."""
    agent: str
    run_date: str
    tests_run: int
    tests_passed: int
    avg_weighted_score: float
    hard_fails_total: int
    total_cost_usd: float
    total_duration_s: float
    results: list[EvalResult] = field(default_factory=list)


class EvalRunner:
    """Run an agent against golden tests, score via LLM-as-judge."""

    def __init__(
        self,
        agents_root: Path | None = None,
        agent_name: str = "",
        today: date | None = None,
    ):
        self.agents_root = agents_root or get_agents_root()
        self.agent_name = agent_name
        self.today = today or date.today()
        self.evals_dir = self.agents_root / agent_name / "evals"

        if not self.evals_dir.exists():
            raise AtomicAgentsError(
                f"Agent '{agent_name}' has no evals/ folder at {self.evals_dir}. "
                f"Per spec/08, every Atomic Agent should have evals/ with rubric.md + judge.md + golden/."
            )

        self._load_rubric()
        self._load_judge_config()

    # ────────────────────────────────────────────────────────────
    # Config loading

    def _load_rubric(self) -> None:
        """Parse evals/rubric.md frontmatter (weights, threshold, hard fails)."""
        path = self.evals_dir / "rubric.md"
        if not path.exists():
            raise AtomicAgentsError(f"Rubric not found at {path}")
        parsed = frontmatter.load(path)
        meta = parsed.metadata
        self.weights: dict[str, float] = {k: float(v) for k, v in meta.get("weights", {}).items()}
        self.threshold_pass: float = float(meta.get("threshold_pass", 4.0))
        self.rubric_body: str = parsed.content
        if not self.weights:
            raise AtomicAgentsError(
                "Rubric has no weights frontmatter. Per spec/08, declare per-dimension weights summing to 100."
            )

    def _load_judge_config(self) -> None:
        """Parse evals/judge.md frontmatter (recommended judges, strict mode, audit %)."""
        path = self.evals_dir / "judge.md"
        if not path.exists():
            raise AtomicAgentsError(f"Judge prompt not found at {path}")
        parsed = frontmatter.load(path)
        meta = parsed.metadata
        rec = meta.get("recommended_judge", {})
        self.judge_cross_family: list[str] = list(rec.get("cross_family", []))
        self.judge_same_family_fallback: list[str] = list(rec.get("same_family_fallback", []))
        self.strict_mode: bool = bool(meta.get("strict_mode", True))
        self.audit_sample_pct: float = float(meta.get("audit_sample_pct", 0.10))
        self.judge_template: str = parsed.content

    # ────────────────────────────────────────────────────────────
    # Test discovery

    def discover_tests(
        self, category: str | None = None, test_id: str | None = None
    ) -> list[EvalTest]:
        """Find golden test files. Returns parsed EvalTest objects."""
        golden_dir = self.evals_dir / "golden"
        if not golden_dir.exists():
            return []

        if test_id:
            # Search across all categories for matching test_id
            for path in golden_dir.rglob("*.md"):
                if test_id in path.stem:
                    test = self._parse_test_file(path)
                    if test is not None:
                        return [test]
            return []

        if category:
            cat_dir = golden_dir / category
            if not cat_dir.exists():
                return []
            tests = []
            for path in sorted(cat_dir.glob("*.md")):
                t = self._parse_test_file(path)
                if t is not None:
                    tests.append(t)
            return tests

        # All tests
        tests = []
        for path in sorted(golden_dir.rglob("*.md")):
            t = self._parse_test_file(path)
            if t is not None:
                tests.append(t)
        return tests

    def _parse_test_file(self, path: Path) -> EvalTest | None:
        """Parse one golden test markdown file into an EvalTest."""
        try:
            parsed = frontmatter.load(path)
        except Exception:
            return None
        meta = parsed.metadata
        test_id = meta.get("test_id")
        category = meta.get("category")
        if not test_id or not category:
            return None
        sections = _extract_sections(parsed.content)
        return EvalTest(
            test_id=test_id,
            category=category,
            path=path,
            setup=sections.get("Setup (vault state for this test)", sections.get("Setup", "")),
            input=sections.get("Input", ""),
            expected_behavior=sections.get("Expected behavior", ""),
            pass_criteria=sections.get("Pass criteria (rubric thresholds + hard-fail checks)",
                                       sections.get("Pass criteria", "")),
            expected_facts=list(meta.get("expected_facts", [])),
            notes=sections.get("Notes", ""),
        )

    # ────────────────────────────────────────────────────────────
    # Judge selection

    def pick_judge_model(self, agent_model: str) -> str:
        """Cross-family judge preferred; same-family fallback if cross unavailable.

        "Available" = the provider's API key is configured (env var or Keychain or config file).
        """
        # Try cross-family first
        for candidate in self.judge_cross_family:
            if candidate == agent_model:
                continue  # don't self-judge
            if _provider_available(candidate):
                return candidate

        # Fall back to same-family
        for candidate in self.judge_same_family_fallback:
            if candidate == agent_model:
                continue
            if _provider_available(candidate):
                return candidate

        raise NoJudgeAvailable(
            f"No judge model reachable. Cross-family: {self.judge_cross_family}. "
            f"Fallback: {self.judge_same_family_fallback}. "
            f"Agent model: {agent_model}. Configure an API key for at least one."
        )

    # ────────────────────────────────────────────────────────────
    # Single-test execution

    def run_test(self, test_or_id: EvalTest | str) -> EvalResult:
        """Execute one golden test. Returns an EvalResult."""
        if isinstance(test_or_id, str):
            tests = self.discover_tests(test_id=test_or_id)
            if not tests:
                raise AtomicAgentsError(f"No test matching '{test_or_id}'")
            test = tests[0]
        else:
            test = test_or_id

        ts_str = datetime.now().astimezone().isoformat()

        # 1. Run the agent against the test input
        agent = AtomicAgent(
            name=self.agent_name,
            trigger="eval",
            agents_root=self.agents_root,
        )
        try:
            agent_response = agent.call(work_item=test.input, write_captures=False)
        except Exception as e:
            return EvalResult(
                test_id=test.test_id, category=test.category,
                agent_model=agent.config.default_model, judge_model="(not invoked)",
                scores={}, score_justifications={},
                weighted_score=0.0, hard_fails=["agent_error"],
                verdict="fail",
                overall_justification=f"Agent crashed: {e}",
                error=str(e), timestamp=ts_str,
            )

        if agent_response.skipped:
            return EvalResult(
                test_id=test.test_id, category=test.category,
                agent_model=agent.config.default_model, judge_model="(skipped)",
                scores={}, score_justifications={},
                weighted_score=0.0, hard_fails=[],
                verdict="judge_error",
                overall_justification=f"Agent run skipped: {agent_response.skip_reason}",
                error=agent_response.skip_reason, timestamp=ts_str,
            )

        # 2. Pick judge model + build prompt
        try:
            judge_model = self.pick_judge_model(agent_response.model)
        except NoJudgeAvailable as e:
            return EvalResult(
                test_id=test.test_id, category=test.category,
                agent_model=agent_response.model, judge_model="(none)",
                scores={}, score_justifications={},
                weighted_score=0.0, hard_fails=[],
                verdict="judge_error",
                overall_justification=str(e),
                error=str(e), timestamp=ts_str,
            )

        judge_prompt = self._build_judge_prompt(test, agent_response.text)

        # 3. Call the judge
        try:
            judge_response = _llm.call_llm(
                model=judge_model,
                system_prompt="",
                messages=[{"role": "user", "content": judge_prompt}],
                max_tokens=2048,
                temperature=0.2,  # judge should be consistent
            )
        except Exception as e:
            return EvalResult(
                test_id=test.test_id, category=test.category,
                agent_model=agent_response.model, judge_model=judge_model,
                scores={}, score_justifications={},
                weighted_score=0.0, hard_fails=[],
                verdict="judge_error",
                overall_justification=f"Judge call failed: {e}",
                agent_response=agent_response.text,
                agent_input_tokens=agent_response.input_tokens,
                agent_output_tokens=agent_response.output_tokens,
                agent_cost_usd=agent_response.cost_usd,
                error=str(e), timestamp=ts_str,
            )

        judge_cost = _costs.calc_cost(
            judge_model, judge_response.input_tokens, judge_response.output_tokens
        )

        # 4. Parse judge JSON
        try:
            scores_dict = self._parse_judge_response(judge_response.text)
        except Exception as e:
            # One retry with stricter prompt
            try:
                stricter = (
                    judge_prompt
                    + "\n\nIMPORTANT: Output ONLY valid JSON. No markdown, no prose, no code fences."
                )
                judge_response_2 = _llm.call_llm(
                    model=judge_model, system_prompt="",
                    messages=[{"role": "user", "content": stricter}],
                    max_tokens=2048, temperature=0.0,
                )
                scores_dict = self._parse_judge_response(judge_response_2.text)
                judge_cost += _costs.calc_cost(
                    judge_model, judge_response_2.input_tokens, judge_response_2.output_tokens
                )
                judge_response.text = judge_response_2.text  # use the cleaner output for the record
            except Exception as e2:
                return EvalResult(
                    test_id=test.test_id, category=test.category,
                    agent_model=agent_response.model, judge_model=judge_model,
                    scores={}, score_justifications={},
                    weighted_score=0.0, hard_fails=[],
                    verdict="judge_error",
                    overall_justification=f"Judge returned malformed JSON twice: {e2}",
                    agent_response=agent_response.text,
                    agent_input_tokens=agent_response.input_tokens,
                    agent_output_tokens=agent_response.output_tokens,
                    agent_cost_usd=agent_response.cost_usd,
                    judge_input_tokens=judge_response.input_tokens,
                    judge_output_tokens=judge_response.output_tokens,
                    judge_cost_usd=judge_cost,
                    judge_raw=judge_response.text,
                    error=str(e2), timestamp=ts_str,
                )

        # 5. Compute weighted score + verdict
        weighted = self._compute_weighted_score(scores_dict)
        hard_fails = list(scores_dict.get("hard_fails", []))
        if hard_fails:
            verdict = "fail"
        elif weighted >= self.threshold_pass:
            verdict = "pass"
        else:
            verdict = "fail"

        return EvalResult(
            test_id=test.test_id,
            category=test.category,
            agent_model=agent_response.model,
            judge_model=judge_model,
            scores={k: v["score"] for k, v in scores_dict.items()
                    if isinstance(v, dict) and "score" in v},
            score_justifications={k: v.get("justification", "") for k, v in scores_dict.items()
                                   if isinstance(v, dict)},
            weighted_score=round(weighted, 2),
            hard_fails=hard_fails,
            verdict=verdict,
            overall_justification=scores_dict.get("overall", {}).get("justification", "")
                                  if isinstance(scores_dict.get("overall"), dict)
                                  else "",
            factual_checks=list(scores_dict.get("factual_checks", [])),
            agent_response=agent_response.text,
            agent_input_tokens=agent_response.input_tokens,
            agent_output_tokens=agent_response.output_tokens,
            agent_cost_usd=agent_response.cost_usd,
            judge_input_tokens=judge_response.input_tokens,
            judge_output_tokens=judge_response.output_tokens,
            judge_cost_usd=round(judge_cost, 6),
            judge_raw=judge_response.text,
            timestamp=ts_str,
        )

    # ────────────────────────────────────────────────────────────
    # Suite execution

    def run_suite(
        self, category: str | None = None, write: bool = True
    ) -> SuiteResults:
        """Run all tests (or one category). Writes results to evals/runs/ if write=True."""
        tests = self.discover_tests(category=category)
        if not tests:
            return SuiteResults(
                agent=self.agent_name,
                run_date=self.today.isoformat(),
                tests_run=0, tests_passed=0,
                avg_weighted_score=0.0, hard_fails_total=0,
                total_cost_usd=0.0, total_duration_s=0.0,
            )

        start = time.time()
        results: list[EvalResult] = []
        for t in tests:
            r = self.run_test(t)
            results.append(r)
            if write:
                self._write_run_log(r)

        duration = time.time() - start
        passed = sum(1 for r in results if r.verdict == "pass")
        scored = [r for r in results if r.verdict in ("pass", "fail") and r.scores]
        avg_score = (
            sum(r.weighted_score for r in scored) / len(scored)
            if scored else 0.0
        )
        hard_fails = sum(len(r.hard_fails) for r in results)
        total_cost = sum(r.agent_cost_usd + r.judge_cost_usd for r in results)

        return SuiteResults(
            agent=self.agent_name,
            run_date=self.today.isoformat(),
            tests_run=len(results),
            tests_passed=passed,
            avg_weighted_score=round(avg_score, 2),
            hard_fails_total=hard_fails,
            total_cost_usd=round(total_cost, 4),
            total_duration_s=round(duration, 1),
            results=results,
        )

    # ────────────────────────────────────────────────────────────
    # Internals

    def _build_judge_prompt(self, test: EvalTest, agent_response: str) -> str:
        """Render the judge.md template with this test's content.

        When the test declares ``expected_facts`` (per spec/13 Layer 2), an
        additional "Factual accuracy check" section is appended to the prompt
        instructing the judge to verify each fact and emit a ``factual_checks``
        array in its JSON response.
        """
        try:
            base = self.judge_template.format(
                rubric=self.rubric_body,
                test_input=test.input,
                expected_behavior=test.expected_behavior,
                pass_criteria=test.pass_criteria,
                agent_response=agent_response,
                trajectory="(trajectory capture not implemented in v0.2)",
            )
        except KeyError:
            base = (
                f"{self.judge_template}\n\n"
                f"---\n\n## Rubric\n\n{self.rubric_body}\n\n"
                f"## Test input\n\n{test.input}\n\n"
                f"## Expected behavior\n\n{test.expected_behavior}\n\n"
                f"## Pass criteria\n\n{test.pass_criteria}\n\n"
                f"## Agent's response\n\n{agent_response}"
            )
        if test.expected_facts:
            base = base + "\n\n" + self._render_factual_check_section(test.expected_facts)
        return base

    @staticmethod
    def _render_factual_check_section(expected_facts: list[dict]) -> str:
        """Build the spec/13 Layer-2 'Factual accuracy check' addendum.

        Instructs the judge to emit a ``factual_checks: [...]`` array
        alongside its rubric scores, with per-fact verdicts on whether
        the agent stated the claim, used the correct value, and cited
        a source.
        """
        bullets = []
        for f in expected_facts:
            claim = f.get("claim", "")
            source = f.get("source", "")
            expected = f.get("expected_value", "")
            bullets.append(
                f'- claim: "{claim}"\n'
                f'  source: {source}\n'
                f'  expected_value: "{expected}"'
            )
        bullet_text = "\n".join(bullets)
        return (
            "## Factual accuracy check\n\n"
            "In addition to scoring rubric dimensions, verify these facts in the\n"
            "agent's response. For each expected_fact:\n\n"
            "1. Did the agent state this claim?\n"
            "2. If yes, did the agent's value match expected_value?\n"
            "3. If yes, did the agent cite a source?\n\n"
            "Add a `factual_checks` array to your JSON response with one entry\n"
            "per expected_fact:\n\n"
            "```json\n"
            '"factual_checks": [\n'
            "  {\n"
            '    "claim": "<claim text>",\n'
            '    "stated_in_response": true|false,\n'
            '    "value_correct": true|false|null,\n'
            '    "cited": true|false|null\n'
            "  }\n"
            "]\n"
            "```\n\n"
            "Use `null` for value_correct/cited when stated_in_response is false.\n\n"
            "Expected facts:\n\n"
            f"{bullet_text}"
        )

    @staticmethod
    def _parse_judge_response(text: str) -> dict:
        """Parse JSON out of the judge's response. Strict mode."""
        text = text.strip()
        # Strip code fences if the judge wrapped its output despite instructions
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0]
            text = text.strip()
        return json.loads(text)

    def _compute_weighted_score(self, scores_dict: dict) -> float:
        """Apply rubric weights to the judge's per-dimension scores.

        Per spec/13 Layer 2: when the rubric declares ``factual_accuracy`` as
        a weighted dimension and the judge returned ``factual_checks``, the
        runner derives the dimension's score from the checks (proportion of
        verified facts × 5, on the same 1–5 scale as other dimensions). If
        the judge already returned a numeric score for ``factual_accuracy``,
        the judge's score takes priority (the LLM may apply nuance the bare
        proportion misses).
        """
        # Inject a derived factual_accuracy score if the rubric expects one
        # but the judge didn't return a numeric score for it.
        if "factual_accuracy" in self.weights:
            existing = scores_dict.get("factual_accuracy")
            if not (isinstance(existing, dict) and "score" in existing):
                checks = scores_dict.get("factual_checks", [])
                derived = compute_factual_accuracy_from_checks(checks)
                if derived is not None:
                    scores_dict["factual_accuracy"] = {
                        "score": derived,
                        "justification": "derived from factual_checks proportion",
                    }

        total = 0.0
        weight_sum = 0.0
        for dim, weight_pct in self.weights.items():
            d = scores_dict.get(dim)
            if isinstance(d, dict) and "score" in d:
                total += float(d["score"]) * weight_pct
                weight_sum += weight_pct
        if weight_sum == 0:
            return 0.0
        return total / weight_sum

    def _write_run_log(self, result: EvalResult) -> None:
        """Append one EvalResult to evals/runs/YYYY-MM-DD.jsonl + write the response."""
        runs_dir = self.evals_dir / "runs"
        responses_dir = runs_dir / "responses"
        runs_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)

        # Write the agent response separately (too long for a JSONL line)
        if result.agent_response:
            resp_path = responses_dir / f"{self.today.isoformat()}_{result.test_id}.txt"
            atomic_write(resp_path, result.agent_response)
            response_path_rel = str(resp_path.relative_to(self.evals_dir))
        else:
            response_path_rel = ""

        log_path = runs_dir / f"{self.today.isoformat()}.jsonl"
        line = {
            "ts": result.timestamp,
            "agent": self.agent_name,
            "test_id": result.test_id,
            "category": result.category,
            "agent_model": result.agent_model,
            "judge_model": result.judge_model,
            "scores": result.scores,
            "score_justifications": result.score_justifications,
            "weighted_score": result.weighted_score,
            "hard_fails": result.hard_fails,
            "verdict": result.verdict,
            "overall_justification": result.overall_justification,
            "factual_checks": result.factual_checks,
            "agent_response_path": response_path_rel,
            "agent_input_tokens": result.agent_input_tokens,
            "agent_output_tokens": result.agent_output_tokens,
            "agent_cost_usd": result.agent_cost_usd,
            "judge_input_tokens": result.judge_input_tokens,
            "judge_output_tokens": result.judge_output_tokens,
            "judge_cost_usd": result.judge_cost_usd,
        }
        if result.error:
            line["error"] = result.error
        atomic_append_jsonl(log_path, json.dumps(line))


# ──────────────────────────────────────────────────────────────────
# Layer-2 factual accuracy helper (module-level for testability)


def compute_factual_accuracy_from_checks(checks: list[dict]) -> float | None:
    """Compute a 1-5 dimension score from a list of ``factual_checks`` entries.

    Per spec/13 Layer 2:
    - A check is "verified" iff stated_in_response AND value_correct AND cited.
    - The dimension score is ``round(5 * verified / total)`` clamped to 1.
    - Returns ``None`` when ``checks`` is empty (no signal to score from).

    A claim that's correctly stated but uncited counts as half-verified
    (we still want some signal — the value is right, but it's not auditable).
    """
    if not checks:
        return None
    total = len(checks)
    verified = 0.0
    for c in checks:
        stated = bool(c.get("stated_in_response"))
        value_ok = bool(c.get("value_correct"))
        cited = bool(c.get("cited"))
        if stated and value_ok and cited:
            verified += 1.0
        elif stated and value_ok:
            verified += 0.5  # right value, uncited — partial credit
    proportion = verified / total
    score = round(5 * proportion)
    return max(1, min(5, int(score)))


# ──────────────────────────────────────────────────────────────────
# Helpers

def _extract_sections(markdown_body: str) -> dict[str, str]:
    """Parse a markdown body into {section_header: section_content}.

    Uses ## headers as section boundaries. Header text is stripped of the
    leading `## ` and trailing whitespace.
    """
    sections: dict[str, str] = {}
    current_header: str | None = None
    current_lines: list[str] = []
    for line in markdown_body.splitlines():
        if line.startswith("## "):
            if current_header is not None:
                sections[current_header] = "\n".join(current_lines).strip()
            current_header = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_header is not None:
        sections[current_header] = "\n".join(current_lines).strip()
    return sections


def _provider_available(model_id: str) -> bool:
    """Check if the provider for a model has a configured API key.

    Doesn't actually call the API — just checks key sources in priority order.
    """
    import os
    if model_id.startswith("claude-"):
        env_vars = ["ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"]
        keychain = "atomic-agents-anthropic"
        config_key = "anthropic"
    elif model_id.startswith("gpt-"):
        env_vars = ["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"]
        keychain = "atomic-agents-openai"
        config_key = "openai"
    elif model_id.startswith("moonshot/"):
        env_vars = ["ATOMIC_AGENTS_MOONSHOT_KEY", "MOONSHOT_API_KEY"]
        keychain = "atomic-agents-moonshot"
        config_key = "moonshot"
    else:
        return False

    # Source 1: env vars
    for var in env_vars:
        if os.environ.get(var):
            return True

    # Source 2: macOS Keychain
    if os.uname().sysname == "Darwin":
        import subprocess
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-a", os.environ.get("USER", ""), "-s", keychain, "-w"],
                capture_output=True, text=True, check=True, timeout=2,
            )
            if result.stdout.strip():
                return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    # Source 3: config file
    config_path = Path.home() / ".config" / "atomic_agents" / "keys.json"
    if config_path.exists():
        try:
            keys = json.loads(config_path.read_text())
            if keys.get(config_key):
                return True
        except (json.JSONDecodeError, OSError):
            pass

    return False


# ──────────────────────────────────────────────────────────────────
# CLI entry: python -m atomic_agents.eval

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="atomic-agents.eval",
        description="Run an agent against golden tests; score via LLM-as-judge",
    )
    parser.add_argument("agent", help="agent name (folder under agents-root)")
    parser.add_argument("--category", choices=["happy", "edge", "adversarial", "decline"],
                        help="run only one category of tests")
    parser.add_argument("--test", help="run a single test by test_id")
    parser.add_argument("--all", action="store_true",
                        help="run the full suite (default if no --test or --category)")
    parser.add_argument("--summary-only", action="store_true",
                        help="print only pass/fail counts, not per-test details")
    parser.add_argument("--no-write", action="store_true",
                        help="don't write results to evals/runs/")
    parser.add_argument("--agents-root", default=None,
                        help="override ATOMIC_AGENTS_ROOT")
    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root else get_agents_root()
    )

    try:
        runner = EvalRunner(agents_root, args.agent)
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    write = not args.no_write

    try:
        if args.test:
            result = runner.run_test(args.test)
            if write:
                runner._write_run_log(result)
            _print_test(result, summary_only=args.summary_only)
            return 0 if result.verdict == "pass" else 1
        else:
            results = runner.run_suite(category=args.category, write=write)
            _print_suite(results, summary_only=args.summary_only)
            return 0 if results.tests_passed == results.tests_run else 1
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _print_test(r: "EvalResult", summary_only: bool = False) -> None:
    status = "✅ pass" if r.verdict == "pass" else (
        "❌ fail" if r.verdict == "fail" else "⚠ judge error"
    )
    if summary_only:
        print(f"{r.test_id}  {status}  {r.weighted_score}")
        return
    print(f"\n=== {r.test_id} ({r.category}) ===")
    print(f"Verdict: {status}  |  Weighted score: {r.weighted_score}")
    if r.hard_fails:
        print(f"Hard fails: {', '.join(r.hard_fails)}")
    if r.scores:
        print("\nPer-dimension scores:")
        for dim, score in r.scores.items():
            just = r.score_justifications.get(dim, "")
            print(f"  {dim:>22}: {score}  — {just}")
    if r.overall_justification:
        print(f"\nJudge: {r.overall_justification}")
    print(f"\nCost: agent ${r.agent_cost_usd:.4f} + judge ${r.judge_cost_usd:.4f}")
    print(f"Models: agent={r.agent_model}  judge={r.judge_model}")
    if r.error:
        print(f"Error: {r.error}")


def _print_suite(s: "SuiteResults", summary_only: bool = False) -> None:
    print(f"\n{s.agent} evaluation — {s.run_date}")
    print("═" * 60)
    if not summary_only:
        for r in s.results:
            status = "✅ pass" if r.verdict == "pass" else (
                "❌ fail" if r.verdict == "fail" else "⚠ judge error"
            )
            cat_label = f"({r.category})"
            print(f"  {r.test_id:<40} {cat_label:<14} {r.weighted_score:>4}  {status}")
    print("─" * 60)
    pass_pct = (s.tests_passed / s.tests_run * 100) if s.tests_run else 0
    print(f"Pass rate:    {s.tests_passed}/{s.tests_run} ({pass_pct:.0f}%)")
    print(f"Avg score:    {s.avg_weighted_score}")
    print(f"Hard fails:   {s.hard_fails_total}")
    print(f"Total cost:   ${s.total_cost_usd:.4f}")
    print(f"Duration:     {s.total_duration_s}s")


if __name__ == "__main__":
    import sys
    sys.exit(main())

"""Outcome runner — iterate-to-rubric pattern, inspired by Anthropic's Outcomes API.

Per the spec at docs/spec/14-outcomes.md.

An Outcome is a single-artifact generation task that loops: the agent produces a draft,
a judge in a fresh context grades it against a rubric, gap feedback flows back for the
next iteration, and the loop terminates on satisfied/failed/max_iterations_reached/
interrupted.

Usage (programmatic):

    from atomic_agents.outcome import OutcomeRunner
    from pathlib import Path

    runner = OutcomeRunner(Path.home() / "agents", "caldwell")
    result = runner.run(
        description="Write a Q1 budget summary",
        rubric="evals/rubric.md",   # path relative to agent root, or abs, or inline text
        max_iterations=3,
    )
    print(result.status)       # 'satisfied' | 'max_iterations_reached' | 'failed' | 'interrupted'
    print(result.total_cost_usd)

CLI:

    python -m atomic_agents.outcome caldwell \\
        --description "Write a Q1 budget summary" \\
        --rubric evals/rubric.md \\
        --max-iterations 3
"""

from __future__ import annotations
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

from . import _llm, _costs
from ._io import atomic_write, atomic_append_jsonl
from ._platform import get_agents_root
from .agent import AtomicAgent
from .eval import EvalRunner, _provider_available
from .exceptions import AtomicAgentsError, CostGuardrailBlocked

# ──────────────────────────────────────────────────────────────────
# Constants

MAX_ITERATIONS_CAP = 20
MIN_ITERATIONS = 1
DEFAULT_MAX_ITERATIONS = 3

# ──────────────────────────────────────────────────────────────────
# Data model


@dataclass
class IterationRecord:
    """Record of one agent+judge iteration within an OutcomeResult."""
    iteration: int                    # 0-indexed
    agent_response: str
    agent_input_tokens: int
    agent_output_tokens: int
    agent_cost_usd: float
    agent_latency_ms: int
    judge_response_raw: str
    judge_verdict: dict               # parsed: {satisfied, criterion_results, explanation, ...}
    judge_cost_usd: float
    judge_input_tokens: int
    judge_output_tokens: int
    artifact_path: Path | None        # if the agent wrote a file
    timestamp: str                    # ISO 8601


@dataclass
class OutcomeResult:
    """Complete result of an OutcomeRunner.run() call."""
    run_id: str
    description: str
    rubric_source: str                # "<agent>/outcomes/foo.md" or "inline"
    max_iterations: int
    status: str                       # 'satisfied' | 'max_iterations_reached' | 'failed' | 'interrupted'
    explanation: str                  # final judge explanation
    iterations: list[IterationRecord] = field(default_factory=list)
    final_iteration_idx: int = -1
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    started_at: str = ""              # ISO 8601
    ended_at: str = ""
    output_files: list[Path] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# OutcomeRunner


class OutcomeRunner:
    """Iterate-to-rubric loop for a single artifact generation task.

    See docs/spec/14-outcomes.md for the full design rationale and comparison
    with eval/ (post-hoc scoring) and goal/ (multi-step, cross-session state).
    """

    def __init__(
        self,
        agents_root: Path | str | None = None,
        agent_name: str = "",
        judge_model: str | None = None,
    ):
        self.agents_root = Path(agents_root) if agents_root else get_agents_root()
        self.agent_name = agent_name
        self.agent_root = self.agents_root / agent_name
        self._explicit_judge_model = judge_model

        if not self.agent_root.exists():
            raise AtomicAgentsError(
                f"Agent folder not found: {self.agent_root}. "
                f"Set ATOMIC_AGENTS_ROOT env var or create the agent."
            )

    # ────────────────────────────────────────────────────────────
    # Public entry point

    def run(
        self,
        description: str,
        rubric: str | Path,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        output_dir: Path | None = None,
        extra_context: str | None = None,
    ) -> OutcomeResult:
        """Run the iterate-to-rubric loop.

        Args:
            description: What the agent should produce.
            rubric: Rubric text (str) or path to a rubric file (Path or str
                starting with a known path). Inline rubric starts with '#' or
                contains newlines. A string with no newlines that looks like a
                file path is treated as a path.
            max_iterations: How many times to iterate. Clamped to [1, 20].
            output_dir: Where the agent should write files. Defaults to
                <agent_root>/outcomes/runs/<run_id>/.
            extra_context: Optional additional context for the agent (e.g.
                data files, user preferences).

        Returns:
            OutcomeResult with full per-iteration records and aggregate stats.
        """
        # Validate and clamp max_iterations
        if max_iterations < MIN_ITERATIONS or max_iterations > MAX_ITERATIONS_CAP:
            raise ValueError(
                f"max_iterations must be between {MIN_ITERATIONS} and {MAX_ITERATIONS_CAP}; "
                f"got {max_iterations}"
            )

        run_id = f"outcome-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        started_at = datetime.now().astimezone().isoformat()

        # Resolve rubric
        rubric_text, rubric_source = self._resolve_rubric(rubric)

        # Set up output directory
        if output_dir is None:
            output_dir = self.agent_root / "outcomes" / "runs" / run_id
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Initialize result
        result = OutcomeResult(
            run_id=run_id,
            description=description,
            rubric_source=rubric_source,
            max_iterations=max_iterations,
            status="failed",           # overwritten on success
            explanation="",
            started_at=started_at,
        )

        # Initialize the agent (lazy-loads on first call)
        agent = AtomicAgent(
            name=self.agent_name,
            trigger="outcome",
            agents_root=self.agents_root,
            run_id=run_id,
        )

        # Resolve judge model: explicit > cross-family via eval config > pick_judge_model fallback
        judge_model = self._resolve_judge_model(agent)

        # Track files present in output_dir at start (to detect new files per iteration)
        known_files: set[Path] = set(output_dir.glob("**/*"))

        # Revision feedback from the previous iteration (empty on iter 0)
        revision_feedback: str = ""

        for i in range(max_iterations + 1):
            # i = 0..max_iterations
            # After i==max_iterations evaluation, the spec says:
            #   "the agent did one last revision after the prior iteration's feedback,
            #    but we evaluated it and it still doesn't satisfy"
            # So: if i == max_iterations AND the agent's response doesn't satisfy →
            # status = max_iterations_reached (not a new revision — we evaluate then stop).

            # ── Step 1: Cost guardrail check ──────────────────────────────
            check = agent._check_cost_guardrails(critical=False)
            if not check.allow:
                result.status = "interrupted"
                result.explanation = (
                    f"cost guardrail hit at iteration {i}: {check.reason}"
                )
                break

            # ── Step 2: Build agent prompt ────────────────────────────────
            agent_prompt = self._build_agent_prompt(
                description=description,
                rubric_text=rubric_text,
                output_dir=output_dir,
                iteration=i,
                revision_feedback=revision_feedback,
                extra_context=extra_context,
            )

            # ── Step 3: Call agent ────────────────────────────────────────
            ts_iter = datetime.now().astimezone().isoformat()
            agent_start = time.time()
            try:
                agent_response = agent.call(
                    work_item=agent_prompt,
                    write_captures=False,
                    # trigger is already 'outcome' from __init__
                )
            except CostGuardrailBlocked as e:
                result.status = "interrupted"
                result.explanation = (
                    f"cost guardrail hit during agent call at iteration {i}: {e}"
                )
                break
            except Exception as e:
                result.status = "failed"
                result.explanation = f"agent call failed at iteration {i}: {e}"
                break

            agent_latency_ms = int((time.time() - agent_start) * 1000)

            if agent_response.skipped:
                result.status = "interrupted"
                result.explanation = (
                    f"agent call skipped at iteration {i}: {agent_response.skip_reason}"
                )
                break

            # ── Step 4: Detect artifact ───────────────────────────────────
            new_files = set(output_dir.glob("**/*")) - known_files
            new_files = {f for f in new_files if f.is_file()}
            known_files = known_files | new_files
            artifact_path: Path | None = None
            if new_files:
                # Use the most recently modified new file as the primary artifact
                artifact_path = max(new_files, key=lambda f: f.stat().st_mtime)
                result.output_files = list(set(result.output_files) | new_files)

            # Artifact text for grading: file content if artifact written, else agent text
            artifact_for_grading: str
            if artifact_path and artifact_path.exists():
                try:
                    artifact_for_grading = artifact_path.read_text(encoding="utf-8")
                except OSError:
                    artifact_for_grading = agent_response.text
            else:
                artifact_for_grading = agent_response.text

            # ── Step 5: Build judge prompt ────────────────────────────────
            judge_prompt = self._build_judge_prompt(
                description=description,
                rubric_text=rubric_text,
                artifact=artifact_for_grading,
                iteration=i,
            )

            # ── Step 6: Call judge ────────────────────────────────────────
            try:
                judge_raw = _llm.call_llm(
                    model=judge_model,
                    system_prompt="",
                    messages=[{"role": "user", "content": judge_prompt}],
                    max_tokens=2048,
                    temperature=0.2,
                )
            except CostGuardrailBlocked as e:
                result.status = "interrupted"
                result.explanation = (
                    f"cost guardrail hit during judge call at iteration {i}: {e}"
                )
                break
            except Exception as e:
                result.status = "failed"
                result.explanation = f"judge call failed at iteration {i}: {e}"
                break

            judge_cost, _ = _costs.calc_cost(
                judge_model, judge_raw.input_tokens, judge_raw.output_tokens
            )

            # ── Step 7: Parse verdict ─────────────────────────────────────
            try:
                verdict = EvalRunner._parse_judge_response(judge_raw.text)
            except Exception:
                # Retry once with stricter prompt
                stricter_prompt = (
                    judge_prompt
                    + "\n\nIMPORTANT: Output ONLY valid JSON. No markdown, no prose, no code fences."
                )
                try:
                    judge_raw2 = _llm.call_llm(
                        model=judge_model,
                        system_prompt="",
                        messages=[{"role": "user", "content": stricter_prompt}],
                        max_tokens=2048,
                        temperature=0.0,
                    )
                    retry_cost, _ = _costs.calc_cost(
                        judge_model, judge_raw2.input_tokens, judge_raw2.output_tokens
                    )
                    judge_cost += retry_cost
                    judge_raw.text = judge_raw2.text
                    verdict = EvalRunner._parse_judge_response(judge_raw.text)
                except Exception as e2:
                    # Both attempts malformed → failed
                    iter_record = IterationRecord(
                        iteration=i,
                        agent_response=agent_response.text,
                        agent_input_tokens=agent_response.input_tokens,
                        agent_output_tokens=agent_response.output_tokens,
                        agent_cost_usd=agent_response.cost_usd,
                        agent_latency_ms=agent_latency_ms,
                        judge_response_raw=judge_raw.text,
                        judge_verdict={},
                        judge_cost_usd=round(judge_cost, 6),
                        judge_input_tokens=judge_raw.input_tokens,
                        judge_output_tokens=judge_raw.output_tokens,
                        artifact_path=artifact_path,
                        timestamp=ts_iter,
                    )
                    result.iterations.append(iter_record)
                    self._append_iteration_log(agent, run_id, iter_record, "malformed_json")
                    result.status = "failed"
                    result.explanation = (
                        f"judge returned malformed JSON twice at iteration {i}: {e2}"
                    )
                    break

            # ── Step 8: Build iteration record ────────────────────────────
            iter_record = IterationRecord(
                iteration=i,
                agent_response=agent_response.text,
                agent_input_tokens=agent_response.input_tokens,
                agent_output_tokens=agent_response.output_tokens,
                agent_cost_usd=agent_response.cost_usd,
                agent_latency_ms=agent_latency_ms,
                judge_response_raw=judge_raw.text,
                judge_verdict=verdict,
                judge_cost_usd=round(judge_cost, 6),
                judge_input_tokens=judge_raw.input_tokens,
                judge_output_tokens=judge_raw.output_tokens,
                artifact_path=artifact_path,
                timestamp=ts_iter,
            )
            result.iterations.append(iter_record)
            self._append_iteration_log(agent, run_id, iter_record, verdict.get("satisfied", False))

            # ── Step 9: Decide next ───────────────────────────────────────
            satisfied = bool(verdict.get("satisfied", False))
            contradicts = bool(verdict.get("rubric_contradicts_description", False))

            if satisfied:
                result.status = "satisfied"
                result.explanation = verdict.get("explanation", "")
                result.final_iteration_idx = i
                break

            if contradicts:
                result.status = "failed"
                result.explanation = (
                    verdict.get("explanation", "")
                    or "The rubric and description fundamentally contradict each other."
                )
                result.final_iteration_idx = i
                break

            if i == max_iterations:
                # Final evaluation done; still not satisfied
                result.status = "max_iterations_reached"
                result.explanation = verdict.get("explanation", "")
                result.final_iteration_idx = i
                break

            # Build revision feedback for next iteration
            revision_feedback = self._build_revision_feedback(verdict, iteration=i)

        # ── Finalize result ───────────────────────────────────────────────
        result.ended_at = datetime.now().astimezone().isoformat()
        result.final_iteration_idx = (
            result.final_iteration_idx
            if result.final_iteration_idx >= 0
            else len(result.iterations) - 1
        )

        # Aggregate costs + tokens
        for rec in result.iterations:
            result.total_cost_usd += rec.agent_cost_usd + rec.judge_cost_usd
            result.total_input_tokens += rec.agent_input_tokens + rec.judge_input_tokens
            result.total_output_tokens += rec.agent_output_tokens + rec.judge_output_tokens
        result.total_cost_usd = round(result.total_cost_usd, 6)

        # Write result.json to output dir
        self._write_result_json(output_dir, result)

        return result

    # ────────────────────────────────────────────────────────────
    # Rubric resolution

    def _resolve_rubric(self, rubric: str | Path) -> tuple[str, str]:
        """Resolve rubric to (text, source_label).

        Resolution order:
        1. If it's a Path object → read the file.
        2. If it's a string that looks like a file path (no newlines, is-file or
           under agent root) → read the file.
        3. Otherwise treat as inline rubric text.
        """
        if isinstance(rubric, Path):
            # Absolute or relative path
            path = rubric if rubric.is_absolute() else self.agent_root / rubric
            if not path.exists():
                raise AtomicAgentsError(f"Rubric file not found: {path}")
            return path.read_text(encoding="utf-8"), str(path.relative_to(self.agents_root))

        # String — check if it looks like a path
        rubric_str = str(rubric)
        if "\n" not in rubric_str:
            # Might be a path string
            candidate = Path(rubric_str)
            if candidate.is_absolute() and candidate.exists():
                return candidate.read_text(encoding="utf-8"), str(candidate.relative_to(self.agents_root) if candidate.is_relative_to(self.agents_root) else candidate)
            rel_candidate = self.agent_root / rubric_str
            if rel_candidate.exists():
                return rel_candidate.read_text(encoding="utf-8"), str(rel_candidate.relative_to(self.agents_root))

        # Inline text
        return rubric_str, "inline"

    # ────────────────────────────────────────────────────────────
    # Judge resolution

    def _resolve_judge_model(self, agent: AtomicAgent) -> str:
        """Pick a judge model: explicit param > cross-family > fallback."""
        if self._explicit_judge_model:
            return self._explicit_judge_model

        agent_model = agent.config.default_model

        # Try to use the eval runner's judge config if evals/ exists
        evals_dir = self.agent_root / "evals"
        if evals_dir.exists() and (evals_dir / "rubric.md").exists() and (evals_dir / "judge.md").exists():
            try:
                eval_runner = EvalRunner(self.agents_root, self.agent_name)
                return eval_runner.pick_judge_model(agent_model)
            except Exception:
                pass

        # Fallback: cross-family heuristic without needing eval config
        return _pick_cross_family_judge(agent_model)

    # ────────────────────────────────────────────────────────────
    # Prompt builders

    def _build_agent_prompt(
        self,
        description: str,
        rubric_text: str,
        output_dir: Path,
        iteration: int,
        revision_feedback: str,
        extra_context: str | None,
    ) -> str:
        """Build the agent's work item for this iteration."""
        parts: list[str] = []
        parts.append(f"## Task\n\n{description}")
        parts.append(
            f"## Output directory\n\n"
            f"Write any output files to: {output_dir}\n\n"
            f"If you produce a file artifact, save it there. "
            f"If you produce a text artifact, just include it in your response."
        )
        parts.append(f"## Rubric\n\nYour output will be graded against this rubric:\n\n{rubric_text}")
        if extra_context:
            parts.append(f"## Additional context\n\n{extra_context}")
        if iteration > 0 and revision_feedback:
            parts.append(
                f"## Revision feedback from iteration {iteration - 1}\n\n"
                f"Your previous draft did not fully satisfy the rubric. "
                f"Address the following gaps in this revision:\n\n{revision_feedback}"
            )
        return "\n\n".join(parts)

    def _build_judge_prompt(
        self,
        description: str,
        rubric_text: str,
        artifact: str,
        iteration: int,
    ) -> str:
        """Build the judge prompt for one iteration."""
        return (
            f"You are evaluating an artifact against a rubric. "
            f"Your job is to determine whether it satisfies all criteria.\n\n"
            f"## Task description\n\n{description}\n\n"
            f"## Rubric\n\n{rubric_text}\n\n"
            f"## Artifact (iteration {iteration})\n\n{artifact}\n\n"
            f"## Instructions\n\n"
            f"Evaluate the artifact against EVERY criterion in the rubric. "
            f"Output ONLY a JSON object with this exact shape:\n\n"
            f"{{\n"
            f'  "satisfied": true or false,\n'
            f'  "criterion_results": [\n'
            f'    {{"criterion": "<name>", "met": true or false, "gap": "<what is missing — only if not met>"}}\n'
            f'  ],\n'
            f'  "explanation": "<one-paragraph summary of verdict>",\n'
            f'  "rubric_contradicts_description": false\n'
            f"}}\n\n"
            f"Set `satisfied` to true ONLY if every criterion is met. "
            f"Set `rubric_contradicts_description` to true if the rubric and description "
            f"fundamentally cannot both be satisfied. "
            f"Output ONLY valid JSON — no markdown, no prose, no code fences."
        )

    def _build_revision_feedback(self, verdict: dict, iteration: int) -> str:
        """Build human-readable feedback from the judge verdict for the next iteration."""
        lines: list[str] = [
            f"Iteration {iteration} did not satisfy the rubric."
        ]
        criterion_results = verdict.get("criterion_results", [])
        unmet = [c for c in criterion_results if not c.get("met", True)]
        if unmet:
            lines.append("\nUnmet criteria:")
            for c in unmet:
                criterion = c.get("criterion", "unknown")
                gap = c.get("gap", "")
                if gap:
                    lines.append(f"  - {criterion}: {gap}")
                else:
                    lines.append(f"  - {criterion}: did not meet this criterion")
        if verdict.get("explanation"):
            lines.append(f"\nJudge summary: {verdict['explanation']}")
        return "\n".join(lines)

    # ────────────────────────────────────────────────────────────
    # Logging

    def _append_iteration_log(
        self,
        agent: AtomicAgent,
        run_id: str,
        record: IterationRecord,
        verdict_summary: Any,
    ) -> None:
        """Append a per-iteration JSONL record to the agent's daily log."""
        today = date.today()
        log_path = (
            self.agent_root / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
        )
        line: dict = {
            "ts": record.timestamp,
            "trigger": "outcome_iteration",
            "run_id": run_id,
            "iteration": record.iteration,
            "agent_input_tokens": record.agent_input_tokens,
            "agent_output_tokens": record.agent_output_tokens,
            "agent_cost_usd": record.agent_cost_usd,
            "agent_latency_ms": record.agent_latency_ms,
            "judge_input_tokens": record.judge_input_tokens,
            "judge_output_tokens": record.judge_output_tokens,
            "judge_cost_usd": record.judge_cost_usd,
            "satisfied": verdict_summary if isinstance(verdict_summary, bool) else False,
            "artifact_path": str(record.artifact_path) if record.artifact_path else None,
        }
        if isinstance(verdict_summary, str):
            line["judge_error"] = verdict_summary
        atomic_append_jsonl(log_path, json.dumps(line))

    def _write_result_json(self, output_dir: Path, result: OutcomeResult) -> None:
        """Write the full OutcomeResult to result.json in the run's output dir."""
        result_path = output_dir / "result.json"
        # Serialize — convert Path objects to strings for JSON
        data = asdict(result)
        # Convert any Path values
        data["output_files"] = [str(p) for p in result.output_files]
        for rec in data["iterations"]:
            if rec.get("artifact_path") is not None:
                rec["artifact_path"] = str(rec["artifact_path"])
        atomic_write(result_path, json.dumps(data, indent=2))


# ──────────────────────────────────────────────────────────────────
# Cross-family judge picker (standalone, for when no evals/ exists)


def _pick_cross_family_judge(agent_model: str) -> str:
    """Pick a cross-family judge model based on the agent's model.

    Tries models in order; returns the first one whose provider is available.
    Falls back to any available model if none of the preferred candidates work.
    """
    if agent_model.startswith("claude-"):
        candidates = ["gpt-5", "gpt-5-mini", "moonshot/kimi-2.6"]
        fallbacks = ["claude-haiku-4-5-20251001", "claude-haiku-4-5", agent_model]
    elif agent_model.startswith("gpt-"):
        candidates = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6-20260101"]
        fallbacks = ["gpt-5-mini", agent_model]
    elif agent_model.startswith("moonshot/"):
        candidates = ["claude-haiku-4-5-20251001", "gpt-5-mini"]
        fallbacks = [agent_model]
    else:
        candidates = ["claude-haiku-4-5-20251001", "gpt-5-mini"]
        fallbacks = [agent_model]

    for candidate in candidates:
        if candidate != agent_model and _provider_available(candidate):
            return candidate

    for fallback in fallbacks:
        if _provider_available(fallback):
            return fallback

    # Last resort: return something even if not available (will fail at call time)
    return candidates[0] if candidates else agent_model


# ──────────────────────────────────────────────────────────────────
# CLI entry: python -m atomic_agents.outcome


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="atomic-agents.outcome",
        description="Run an iterate-to-rubric outcome loop for an agent",
    )
    parser.add_argument("agent", help="agent name (folder under agents-root)")
    parser.add_argument("--description", required=True,
                        help="what the agent should produce")
    parser.add_argument("--rubric", required=True,
                        help="path to rubric file, or 'inline:<text>'")
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS,
                        help=f"max iterations (default {DEFAULT_MAX_ITERATIONS}, max {MAX_ITERATIONS_CAP})")
    parser.add_argument("--judge-model", default=None,
                        help="override the judge model")
    parser.add_argument("--output-dir", default=None,
                        help="where the agent writes artifact files")
    parser.add_argument("--extra-context", default=None,
                        help="additional context for the agent")
    parser.add_argument("--agents-root", default=None,
                        help="override ATOMIC_AGENTS_ROOT")
    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root else get_agents_root()
    )

    # Resolve rubric arg
    rubric: str | Path
    if args.rubric.startswith("inline:"):
        rubric = args.rubric[len("inline:"):]
    else:
        rubric = Path(args.rubric)

    output_dir = Path(args.output_dir) if args.output_dir else None

    try:
        runner = OutcomeRunner(
            agents_root=agents_root,
            agent_name=args.agent,
            judge_model=args.judge_model,
        )
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        result = runner.run(
            description=args.description,
            rubric=rubric,
            max_iterations=args.max_iterations,
            output_dir=output_dir,
            extra_context=args.extra_context,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    _print_result(result)

    if result.status == "satisfied":
        return 0
    if result.status == "interrupted":
        return 2
    return 1  # max_iterations_reached | failed


def _print_result(result: OutcomeResult) -> None:
    status_icons = {
        "satisfied": "SATISFIED",
        "max_iterations_reached": "MAX ITERATIONS REACHED",
        "failed": "FAILED",
        "interrupted": "INTERRUPTED",
    }
    icon = status_icons.get(result.status, result.status.upper())
    print(f"\n=== Outcome: {icon} ===")
    print(f"Run ID:      {result.run_id}")
    print(f"Agent:       {result.rubric_source}")
    print(f"Iterations:  {len(result.iterations)} / {result.max_iterations}")
    print(f"Total cost:  ${result.total_cost_usd:.4f}")
    print(f"Explanation: {result.explanation}")
    if result.output_files:
        print(f"Output files:")
        for f in result.output_files:
            print(f"  {f}")
    print()


if __name__ == "__main__":
    import sys
    sys.exit(main())

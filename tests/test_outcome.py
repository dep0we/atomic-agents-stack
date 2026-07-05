"""Tests for atomic_agents.outcome."""

from __future__ import annotations
import json
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from atomic_agents.outcome import (
    OutcomeRunner,
    OutcomeResult,
    IterationRecord,
    _pick_cross_family_judge,
    DEFAULT_MAX_ITERATIONS,
    MAX_ITERATIONS_CAP,
    MIN_ITERATIONS,
)
from atomic_agents.exceptions import AtomicAgentsError
from atomic_agents.types import CostCheckResult


# ──────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def agent_vault(tmp_path):
    """Minimal agent folder with persona, tools, model."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "testagent"

    # Persona
    persona = agent_root / "persona"
    persona.mkdir(parents=True)
    (persona / "IDENTITY.md").write_text("# IDENTITY\n\nI am a test agent.")
    (persona / "SOUL.md").write_text("# SOUL\n\nBrief.")

    # tools.md
    (agent_root / "tools.md").write_text(
        "# TOOLS\n\n## Read paths\n- "
        + str(agent_root)
        + "\n\n## Write paths\n- "
        + str(agent_root)
        + "\n"
    )

    # model.md
    (agent_root / "model.md").write_text(
        "# MODEL\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
        "## Fallback\n\nclaude-haiku-4-5-20251001\n"
    )

    return agents_root, "testagent"


@pytest.fixture
def rubric_text():
    return (
        "## completeness\n"
        "The output covers all required sections.\n\n"
        "## accuracy\n"
        "All facts are correct.\n"
    )


@pytest.fixture
def satisfied_verdict():
    return json.dumps(
        {
            "satisfied": True,
            "criterion_results": [
                {"criterion": "completeness", "met": True},
                {"criterion": "accuracy", "met": True},
            ],
            "explanation": "All criteria met.",
            "rubric_contradicts_description": False,
        }
    )


@pytest.fixture
def unsatisfied_verdict():
    return json.dumps(
        {
            "satisfied": False,
            "criterion_results": [
                {
                    "criterion": "completeness",
                    "met": False,
                    "gap": "Missing the summary section",
                },
                {"criterion": "accuracy", "met": True},
            ],
            "explanation": "Summary section is missing.",
            "rubric_contradicts_description": False,
        }
    )


def _make_agent_response(
    text="Here is the artifact.",
    model="claude-sonnet-4-6-20260101",
    input_tokens=100,
    output_tokens=50,
    cost_usd=0.001,
    skipped=False,
    skip_reason="",
):
    resp = MagicMock()
    resp.text = text
    resp.model = model
    resp.input_tokens = input_tokens
    resp.output_tokens = output_tokens
    resp.cost_usd = cost_usd
    resp.skipped = skipped
    resp.skip_reason = skip_reason
    return resp


def _make_judge_response(text, input_tokens=80, output_tokens=40):
    resp = MagicMock()
    resp.text = text
    resp.input_tokens = input_tokens
    resp.output_tokens = output_tokens
    return resp


def _make_runner(agent_vault, judge_model="gpt-5"):
    agents_root, agent_name = agent_vault
    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name=agent_name,
        judge_model=judge_model,
    )
    return runner


# ──────────────────────────────────────────────────────────────────
# Test 1: satisfied on iteration 0


def test_outcome_satisfied_on_iteration_0(agent_vault, rubric_text, satisfied_verdict):
    """First artifact passes rubric: status=satisfied, exactly 1 iteration."""
    runner = _make_runner(agent_vault)

    agent_resp = _make_agent_response()
    judge_resp = _make_judge_response(satisfied_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        # Cost guardrail allows
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Write a test summary",
                rubric=rubric_text,
                max_iterations=3,
            )

    assert result.status == "satisfied"
    assert len(result.iterations) == 1
    assert result.iterations[0].iteration == 0
    assert result.final_iteration_idx == 0
    assert result.total_cost_usd > 0


# ──────────────────────────────────────────────────────────────────
# Test 2: revises then satisfies


def test_outcome_revises_then_satisfies(
    agent_vault, rubric_text, unsatisfied_verdict, satisfied_verdict
):
    """First iteration fails one criterion; second satisfies. status=satisfied, 2 iterations."""
    runner = _make_runner(agent_vault)

    agent_resp1 = _make_agent_response(text="Draft 1 — missing summary")
    agent_resp2 = _make_agent_response(text="Draft 2 — with summary")
    judge_resp1 = _make_judge_response(unsatisfied_verdict)
    judge_resp2 = _make_judge_response(satisfied_verdict)

    call_count = {"agent": 0, "judge": 0}

    def agent_side_effect(*args, **kwargs):
        call_count["agent"] += 1
        return agent_resp1 if call_count["agent"] == 1 else agent_resp2

    def judge_side_effect(*args, **kwargs):
        call_count["judge"] += 1
        return judge_resp1 if call_count["judge"] == 1 else judge_resp2

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.side_effect = agent_side_effect
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch(
            "atomic_agents.outcome._llm.call_llm", side_effect=judge_side_effect
        ):
            result = runner.run(
                description="Write a test summary",
                rubric=rubric_text,
                max_iterations=3,
            )

    assert result.status == "satisfied"
    assert len(result.iterations) == 2
    assert result.iterations[0].iteration == 0
    assert result.iterations[1].iteration == 1
    assert result.final_iteration_idx == 1
    # Second prompt should contain revision feedback
    second_prompt = mock_instance.call.call_args_list[1][1]["work_item"]
    assert "Revision feedback" in second_prompt or "revision" in second_prompt.lower()
    assert "Missing the summary section" in second_prompt


# ──────────────────────────────────────────────────────────────────
# Test 3: hits max iterations


def test_outcome_hits_max_iterations(agent_vault, rubric_text, unsatisfied_verdict):
    """Judge always says not satisfied. Ends with max_iterations_reached after N+1 evaluations."""
    max_iters = 3
    runner = _make_runner(agent_vault)

    agent_resp = _make_agent_response()
    judge_resp = _make_judge_response(unsatisfied_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Write a test summary",
                rubric=rubric_text,
                max_iterations=max_iters,
            )

    assert result.status == "max_iterations_reached"
    # Iterations: 0, 1, 2, 3 (one per i in range(max_iters + 1) = range(4))
    assert len(result.iterations) == max_iters + 1
    assert result.iterations[-1].iteration == max_iters


# ──────────────────────────────────────────────────────────────────
# Test 4: rubric contradicts description


def test_outcome_rubric_contradicts_description_fails(agent_vault, rubric_text):
    """Judge sets rubric_contradicts_description=true on iteration 0 → status=failed immediately."""
    runner = _make_runner(agent_vault)

    contradicts_verdict = json.dumps(
        {
            "satisfied": False,
            "criterion_results": [],
            "explanation": "The description asks for one page but rubric requires 10 sections.",
            "rubric_contradicts_description": True,
        }
    )

    agent_resp = _make_agent_response()
    judge_resp = _make_judge_response(contradicts_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Write a one-page summary",
                rubric=rubric_text,
                max_iterations=3,
            )

    assert result.status == "failed"
    assert len(result.iterations) == 1
    assert (
        "contradict" in result.explanation.lower()
        or "one page" in result.explanation.lower()
    )


# ──────────────────────────────────────────────────────────────────
# Test 5: malformed judge JSON retries then fails


def test_outcome_malformed_judge_json_retries_then_fails(agent_vault, rubric_text):
    """First judge call returns garbage; retry also fails → status=failed."""
    runner = _make_runner(agent_vault)

    agent_resp = _make_agent_response()
    bad_judge_resp = _make_judge_response("this is not JSON at all!!!!!")

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        # Both judge calls (first attempt + retry) return bad JSON
        with patch("atomic_agents.outcome._llm.call_llm", return_value=bad_judge_resp):
            result = runner.run(
                description="Write something",
                rubric=rubric_text,
                max_iterations=3,
            )

    assert result.status == "failed"
    assert "malformed JSON" in result.explanation
    # Should have made at least 2 judge calls (first + retry)
    assert len(result.iterations) == 1  # 1 iteration recorded even on parse failure


# ──────────────────────────────────────────────────────────────────
# Test 6: max_iterations validation


def test_outcome_clamps_max_iterations_to_valid_range(agent_vault):
    """0 raises ValueError, 21+ raises ValueError, 1-20 are OK (no LLM call needed)."""
    agents_root, agent_name = agent_vault
    runner = OutcomeRunner(
        agents_root=agents_root, agent_name=agent_name, judge_model="gpt-5"
    )

    # 0 → ValueError
    with pytest.raises(ValueError, match="max_iterations"):
        runner.run("desc", "rubric text\n# criteria", max_iterations=0)

    # 21 → ValueError
    with pytest.raises(ValueError, match="max_iterations"):
        runner.run("desc", "rubric text\n# criteria", max_iterations=21)

    # Boundary values 1 and 20 are valid (we don't actually run them — just confirm no ValueError)
    # We'd need LLM mocks to run them fully; just confirm the validation passes.
    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = _make_agent_response()
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance
        satisfied_v = json.dumps(
            {
                "satisfied": True,
                "criterion_results": [],
                "explanation": "OK",
                "rubric_contradicts_description": False,
            }
        )
        with patch(
            "atomic_agents.outcome._llm.call_llm",
            return_value=_make_judge_response(satisfied_v),
        ):
            # max_iterations=1 — should not raise
            result = runner.run("desc", "rubric\n# criteria", max_iterations=1)
            assert result.status == "satisfied"

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = _make_agent_response()
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance
        with patch(
            "atomic_agents.outcome._llm.call_llm",
            return_value=_make_judge_response(satisfied_v),
        ):
            # max_iterations=20 — should not raise
            result = runner.run("desc", "rubric\n# criteria", max_iterations=20)
            assert result.status == "satisfied"


# ──────────────────────────────────────────────────────────────────
# Boundary-2 forwarding: OutcomeRunner.run() → internal AtomicAgent ctor
#
# This is the regression guard for the backend-universe alignment property
# (spec/41 §"Goal-outcome composition", #496 PR1). The coordinator threads the
# gate agent's backends into OutcomeRunner (boundary 1, covered in
# test_goal_coordinator.py); this test covers boundary 2 — that run() forwards
# those backend kwargs into the internally-constructed AtomicAgent at
# _outcome_impl.py:298-311. Without this guard, a refactor dropping the
# forwarding would pass every other test while silently breaking the property.
#
# The existing test_log_integration.py::test_outcome_runner_kwarg_threaded_to_
# internal_agent and test_profile_integration.py::test_outcome_runner_threads_
# profile_backend assert only STORAGE (runner._log_backend/_profile_backend);
# they do NOT assert the constructor forwarding. This test closes that gap.


def test_outcome_runner_forwards_backends_to_internal_agent(
    agent_vault, rubric_text, satisfied_verdict
):
    """run() must construct its internal AtomicAgent with the SAME
    log_backend / policy_backend / profile_backend objects passed to OutcomeRunner.

    Asserts on MockAgent.call_args.kwargs — what run() actually passed to
    AtomicAgent(...) — closing boundary 2 of the universe-alignment chain
    (spec/41 §"Goal-outcome composition", #496 PR1).
    """
    agents_root, agent_name = agent_vault

    sentinel_log = object()
    sentinel_policy = object()
    sentinel_profile = object()

    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name=agent_name,
        judge_model="gpt-5",
        log_backend=sentinel_log,
        policy_backend=sentinel_policy,
        profile_backend=sentinel_profile,
    )

    agent_resp = _make_agent_response()
    judge_resp = _make_judge_response(satisfied_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            runner.run(
                description="Write a test summary",
                rubric=rubric_text,
                max_iterations=3,
            )

    ctor_kwargs = MockAgent.call_args.kwargs
    assert ctor_kwargs["log_backend"] is sentinel_log, (
        "run() must forward log_backend into the internal AtomicAgent ctor "
        f"(boundary 2). Got: {ctor_kwargs.get('log_backend')!r}"
    )
    assert ctor_kwargs["policy_backend"] is sentinel_policy, (
        "run() must forward policy_backend into the internal AtomicAgent ctor "
        f"(boundary 2). Got: {ctor_kwargs.get('policy_backend')!r}"
    )
    assert ctor_kwargs["profile_backend"] is sentinel_profile, (
        "run() must forward profile_backend into the internal AtomicAgent ctor "
        f"(boundary 2). Got: {ctor_kwargs.get('profile_backend')!r}"
    )


# ──────────────────────────────────────────────────────────────────
# Test 7: iteration records written to agent log


def test_outcome_writes_iteration_records_to_agent_log(
    agent_vault, rubric_text, satisfied_verdict, tmp_path
):
    """Per-iteration JSONL records land in <agent>/log/<YYYY-MM>/<date>.jsonl."""
    agents_root, agent_name = agent_vault
    runner = OutcomeRunner(
        agents_root=agents_root, agent_name=agent_name, judge_model="gpt-5"
    )

    agent_resp = _make_agent_response()
    judge_resp = _make_judge_response(satisfied_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        # Per #61 PR 2: outcome._append_iteration_log routes through
        # agent.log_backend.append(...). Wire a real FilesystemLogBackend
        # so the test continues to verify the on-disk landing point.
        from atomic_agents.logs import FilesystemLogBackend

        mock_instance.log_backend = FilesystemLogBackend(agents_root / agent_name)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Log test",
                rubric=rubric_text,
                max_iterations=1,
            )

    today = date.today()
    log_path = (
        agents_root
        / agent_name
        / "log"
        / today.strftime("%Y-%m")
        / f"{today.isoformat()}.jsonl"
    )
    assert log_path.exists(), f"Expected log at {log_path}"

    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    outcome_lines = []
    for line in lines:
        try:
            rec = json.loads(line)
            if rec.get("trigger") == "outcome_iteration":
                outcome_lines.append(rec)
        except json.JSONDecodeError:
            pass

    assert len(outcome_lines) >= 1
    rec = outcome_lines[0]
    assert rec["trigger"] == "outcome_iteration"
    assert rec["iteration"] == 0
    assert "agent_cost_usd" in rec
    assert "judge_cost_usd" in rec
    assert "run_id" in rec


# ──────────────────────────────────────────────────────────────────
# Test 8: result.json written to output dir


def test_outcome_writes_result_json(agent_vault, rubric_text, satisfied_verdict):
    """Final result.json exists at run output dir, parseable, has all expected fields."""
    runner = _make_runner(agent_vault)

    agent_resp = _make_agent_response()
    judge_resp = _make_judge_response(satisfied_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Result JSON test",
                rubric=rubric_text,
                max_iterations=2,
            )

    agents_root, agent_name = agent_vault
    # Find the result.json
    runs_dir = agents_root / agent_name / "outcomes" / "runs"
    result_files = list(runs_dir.glob("*/result.json"))
    assert len(result_files) == 1, (
        f"Expected exactly 1 result.json, found: {result_files}"
    )

    data = json.loads(result_files[0].read_text())
    assert data["run_id"] == result.run_id
    assert data["description"] == "Result JSON test"
    assert data["status"] == "satisfied"
    assert "iterations" in data
    assert "total_cost_usd" in data
    assert "started_at" in data
    assert "ended_at" in data
    assert "max_iterations" in data
    assert "rubric_source" in data
    assert data["final_iteration_idx"] == 0


# ──────────────────────────────────────────────────────────────────
# Test 9: respects parent cost cap


def test_outcome_respects_parent_cost_cap(agent_vault, rubric_text):
    """Set a tight cap; mock guardrail to refuse; outcome ends with status=interrupted."""
    runner = _make_runner(agent_vault)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        # Guardrail refuses from the start
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(
            allow=False,
            action="skip",
            reason="daily cap hit ($0.10/$0.10)",
        )
        MockAgent.return_value = mock_instance

        result = runner.run(
            description="Cost cap test",
            rubric=rubric_text,
            max_iterations=3,
        )

    assert result.status == "interrupted"
    assert "cost guardrail" in result.explanation
    # No iterations should have been recorded (guardrail fired before agent call)
    assert len(result.iterations) == 0


def test_outcome_per_iteration_treecap_clamps_under_model_override(
    agent_vault, rubric_text, unsatisfied_verdict
):
    """#668 (C10) — the per-iteration MIN(child_remaining, parent_remaining) tree-cap
    clamp still fires on a model-bearing stage, AND the per-stage model_override is
    threaded into EVERY real agent.call() iteration.

    This is the differing-model tree-cap guard named by the #668 rulings
    (cost-gate-prices-override-model). Unlike the conductor-level
    test_between_stage_halt_is_model_agnostic (which mocks dispatch and only exercises
    the coarse between-stage `run_remaining <= 0` backstop), this test drives the REAL
    OutcomeRunner.run() per-iteration gate: the un-mocked `_clamped_parent_headroom`
    decrements the run-level headroom by this stage's accumulated spend each iteration,
    so a model-bearing stage cannot overshoot the run cap by more than one iteration.

    Setup: parent_remaining_headroom_usd=0.10, each agent iteration costs 0.08 (priced
    post-hoc, independent of the declared model), judge unsatisfied so the stage keeps
    revising. The mocked `_check_cost_guardrails` allows iff the clamped headroom it is
    handed is > 0, so the REAL clamp arithmetic decides when the gate fires:
      - iter 0 entry: clamp = 0.10 → allow → agent.call #1 (model_override threaded)
      - iter 0 pre-judge: 0.10 - 0.08 = 0.02 → allow → records ~0.08 spend
      - iter 1 entry: 0.10 - ~0.08 = ~0.02 → allow → agent.call #2 (model_override threaded)
      - iter 1 pre-judge: 0.10 - ~0.08 - 0.08 = ~-0.06 → REFUSE → interrupted at iteration 1
    """
    ACTOR_MODEL = "claude-sonnet-4-6-20260101"
    agents_root, agent_name = agent_vault
    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name=agent_name,
        judge_model="gpt-5",
        actor_model=ACTOR_MODEL,
        parent_remaining_headroom_usd=0.10,
    )

    agent_resp = _make_agent_response(cost_usd=0.08)
    judge_resp = _make_judge_response(unsatisfied_verdict)

    seen_headrooms: list[float | None] = []

    def _gate(critical=False, parent_remaining_headroom_usd=None, **_kwargs):
        # Drive the REAL clamp: allow iff the un-mocked _clamped_parent_headroom
        # value handed to us is positive (or unset). This makes the per-iteration
        # MIN clamp the thing under test, not a hard-coded refuse.
        seen_headrooms.append(parent_remaining_headroom_usd)
        if (
            parent_remaining_headroom_usd is not None
            and parent_remaining_headroom_usd <= 0
        ):
            return CostCheckResult(
                allow=False,
                action="skip",
                reason="run tree-cap hit (per-iteration clamp)",
            )
        return CostCheckResult(allow=True)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-3-5-haiku-20241022"
        mock_instance._check_cost_guardrails.side_effect = _gate
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Per-iteration tree-cap on a model-bearing stage",
                rubric=rubric_text,
                max_iterations=3,
            )

    # The per-iteration clamp must halt the stage (not run to max_iterations).
    assert result.status == "interrupted", (
        f"per-iteration tree-cap must interrupt the model-bearing stage; "
        f"got status={result.status!r} explanation={result.explanation!r}"
    )
    assert "iteration 1" in result.explanation, (
        f"interrupt must land at the per-iteration gate (iteration 1), not iteration 0 "
        f"or a between-stage backstop; got {result.explanation!r}"
    )

    # The model_override MUST thread into EVERY real agent.call() iteration. The
    # strip-control sense: were the dial NOT wired, these would be None.
    call_models = [
        c.kwargs.get("model_override") for c in mock_instance.call.call_args_list
    ]
    assert len(call_models) >= 2, (
        f"expected >=2 agent.call iterations; got {len(call_models)}"
    )
    assert all(m == ACTOR_MODEL for m in call_models), (
        f"every per-iteration agent.call must carry model_override={ACTOR_MODEL!r}; "
        f"got {call_models!r}"
    )

    # The gate that fired must be the DECREMENTED clamp (proving _clamped_parent_headroom
    # ran on the model path), not the fixed 0.10 snapshot — i.e. a negative headroom was
    # handed to the refusing gate.
    assert any(h is not None and h <= 0 for h in seen_headrooms), (
        f"the refusing gate must receive a clamped (decremented) headroom <= 0, proving "
        f"the per-iteration MIN clamp ran on the model_override path; got {seen_headrooms!r}"
    )


# ──────────────────────────────────────────────────────────────────
# Test 10: artifact glob picks up new files


def test_outcome_artifact_glob_picks_up_new_files(
    agent_vault, rubric_text, satisfied_verdict, tmp_path
):
    """Agent writes a file under run output dir; final result.output_files contains it."""
    agents_root, agent_name = agent_vault
    runner = OutcomeRunner(
        agents_root=agents_root, agent_name=agent_name, judge_model="gpt-5"
    )

    # We'll capture the output_dir and write a file into it during the agent call
    captured_output_dir: dict = {}

    def agent_call_side_effect(*args, **kwargs):
        work_item = kwargs.get("work_item", "")
        # Extract output dir from the prompt
        for line in work_item.splitlines():
            if "Write any output files to:" in line:
                dir_str = line.split("Write any output files to:", 1)[1].strip()
                out_dir = Path(dir_str)
                out_dir.mkdir(parents=True, exist_ok=True)
                artifact = out_dir / "summary.txt"
                artifact.write_text("The Q1 summary artifact content.")
                captured_output_dir["path"] = str(artifact)
                break
        return _make_agent_response(
            text="I wrote the artifact to the output directory."
        )

    judge_resp = _make_judge_response(satisfied_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.side_effect = agent_call_side_effect
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Write a summary artifact",
                rubric=rubric_text,
                max_iterations=1,
            )

    assert result.status == "satisfied"
    assert len(result.output_files) >= 1
    output_file_names = [Path(f).name for f in result.output_files]
    assert "summary.txt" in output_file_names

    # Also verify the iteration record has the artifact_path
    assert result.iterations[0].artifact_path is not None
    assert Path(result.iterations[0].artifact_path).name == "summary.txt"


# ──────────────────────────────────────────────────────────────────
# Test 11: judge call respects cost cap (R2-C1)


def test_outcome_judge_call_respects_cost_cap(agent_vault, rubric_text):
    """Agent call succeeds, but guardrail refuses before judge call → interrupted, judge never called."""
    runner = _make_runner(agent_vault)

    agent_resp = _make_agent_response()
    call_count = {"guardrail": 0, "judge": 0}

    def guardrail_side_effect(critical=False, **kwargs):
        call_count["guardrail"] += 1
        # First check (pre-agent): allow. Second check (pre-judge): deny.
        if call_count["guardrail"] <= 1:
            return CostCheckResult(allow=True)
        return CostCheckResult(
            allow=False, action="skip", reason="daily cap hit ($0.10/$0.10)"
        )

    def judge_side_effect(*args, **kwargs):
        call_count["judge"] += 1
        return MagicMock()

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.side_effect = guardrail_side_effect
        MockAgent.return_value = mock_instance

        with patch(
            "atomic_agents.outcome._llm.call_llm", side_effect=judge_side_effect
        ) as mock_llm:
            result = runner.run(
                description="Cost cap judge test",
                rubric=rubric_text,
                max_iterations=3,
            )

    assert result.status == "interrupted"
    assert "cost guardrail" in result.explanation
    assert "judge" in result.explanation
    # Judge should not have been called at all
    assert call_count["judge"] == 0
    # Only 1 iteration was attempted (aborted before judge), so no completed iterations
    assert len(result.iterations) == 0


# ──────────────────────────────────────────────────────────────────
# Tests 12-14: max_iterations validation raises ValueError (R2-C2)


def test_outcome_max_iterations_zero_raises(agent_vault):
    """max_iterations=0 raises ValueError (operator mistake should be loud)."""
    agents_root, agent_name = agent_vault
    runner = OutcomeRunner(
        agents_root=agents_root, agent_name=agent_name, judge_model="gpt-5"
    )

    with pytest.raises(ValueError, match="max_iterations"):
        runner.run("desc", "rubric text\n# criteria", max_iterations=0)


def test_outcome_max_iterations_twentyone_raises(agent_vault):
    """max_iterations=21 raises ValueError (above allowed cap)."""
    agents_root, agent_name = agent_vault
    runner = OutcomeRunner(
        agents_root=agents_root, agent_name=agent_name, judge_model="gpt-5"
    )

    with pytest.raises(ValueError, match="max_iterations"):
        runner.run("desc", "rubric text\n# criteria", max_iterations=21)


def test_outcome_max_iterations_in_range_unchanged(agent_vault, satisfied_verdict):
    """max_iterations=5 is accepted unchanged and stored in the result."""
    runner = _make_runner(agent_vault)

    agent_resp = _make_agent_response()
    judge_resp = _make_judge_response(satisfied_verdict)

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="In-range max_iterations test",
                rubric="rubric\n# criteria",
                max_iterations=5,
            )

    assert result.max_iterations == 5


# ──────────────────────────────────────────────────────────────────
# Test 15: CLI summary labels rubric_source correctly (R2-C3)


def test_outcome_cli_summary_labels_correctly(agent_vault, satisfied_verdict, capsys):
    """CLI _print_result prints 'Rubric:' not 'Agent:' for rubric_source."""
    from atomic_agents.outcome import _print_result

    result = OutcomeResult(
        run_id="outcome-test-12345678",
        description="CLI label test",
        rubric_source="testagent/evals/rubric.md",
        max_iterations=3,
        status="satisfied",
        explanation="All criteria met.",
        started_at="2026-05-07T10:00:00+00:00",
        ended_at="2026-05-07T10:00:05+00:00",
        total_cost_usd=0.0012,
    )

    _print_result(result)
    captured = capsys.readouterr()

    assert "Rubric:" in captured.out
    assert "Agent:" not in captured.out
    assert "testagent/evals/rubric.md" in captured.out

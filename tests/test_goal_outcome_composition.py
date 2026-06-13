"""Tests for GoalManager.dispatch_as_outcome — goal/outcome composition layer.

These tests are separate from test_goal.py to keep concerns isolated:
  - test_goal.py  → GoalManager data model, lifecycle transitions, archive, CLI
  - test_outcome.py → OutcomeRunner iterate-to-rubric loop
  - this file      → composition layer: dispatch_as_outcome + terminal-state mapping
"""

from __future__ import annotations
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from atomic_agents.goal import GoalManager, SubGoal
from atomic_agents.exceptions import AtomicAgentsError, GoalCorrupted


# ──────────────────────────────────────────────────────────────────
# Helpers / fixtures


def _make_outcome_result(
    status: str,
    run_id: str = "outcome-20260507-120000-abcd1234",
    explanation: str = "Test explanation.",
    iterations: int = 2,
    total_cost_usd: float = 0.0042,
) -> MagicMock:
    """Build a minimal OutcomeResult mock with all fields dispatch_as_outcome inspects."""
    result = MagicMock()
    result.status = status
    result.run_id = run_id
    result.explanation = explanation
    result.total_cost_usd = total_cost_usd

    # Create real-ish iteration records (just mocks)
    iter_records = [MagicMock() for _ in range(iterations)]
    result.iterations = iter_records
    result.max_iterations = iterations
    return result


@pytest.fixture
def agent_with_goal(tmp_path):
    """Agent vault with a goal containing multiple sub-goals in various states."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "muse-director"
    agent_root.mkdir(parents=True)

    # Minimal persona so dispatch_as_outcome's shim can construct a real
    # AtomicAgent for the fail-closed cost gate (Principle #4 — the gate is now
    # LIVE on this path; the shim no longer injects a no-gate sentinel). No
    # model.md is written, so cost_guardrails_enabled defaults False and the gate
    # passes (allow=True) — exactly the documented bound: the gate enforces
    # model.md caps, and an agent with no configured caps has nothing to refuse.
    (agent_root / "persona").mkdir()
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Identity\nTest agent for goal-outcome composition.\n"
    )

    goal_text = """\
---
schema_version: 1
active: true
intent: Complete novel first draft
priority: high
created: 2026-04-01
deadline: 2026-12-31
last_progress_check: 2026-05-01
success_criteria:
  - All chapters drafted
sub_goals:
  - id: ch_1
    label: Chapter 1 first draft
    status: complete
    completed: 2026-04-28
  - id: ch_2
    label: Chapter 2 first draft
    status: pending
    body: Write the second chapter focusing on character development.
    acceptance_criteria:
      - Chapter is at least 3000 words
      - Character arc is clear
  - id: ch_3
    label: Chapter 3 first draft
    status: pending
    blocked_by: ch_2
  - id: ch_4
    label: Chapter 4 first draft
    status: in_progress
  - id: ch_5
    label: Chapter 5 first draft
    status: blocked
    blocked_by: ch_1
---

# Goal body

## History (auto-appended)

- 2026-04-28 — sub_goal `ch_1` complete
"""
    (agent_root / "goal.md").write_text(goal_text)
    return agents_root, "muse-director"


def _make_gm(agent_with_goal, today=None):
    agents_root, agent_name = agent_with_goal
    return GoalManager(agents_root, agent_name, today=today or date(2026, 5, 7))


# ──────────────────────────────────────────────────────────────────
# Test 1: satisfied → sub-goal becomes complete


def test_dispatch_as_outcome_satisfied_marks_complete(agent_with_goal):
    """Outcome satisfied → sub-goal status == complete with today's date."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        result, sg = gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="## completeness\nChapter complete.\n",
        )

    assert result.status == "satisfied"
    assert sg.status == "complete"
    assert sg.completed == "2026-05-07"


# ──────────────────────────────────────────────────────────────────
# Test 2: max_iterations_reached → sub-goal becomes blocked


def test_dispatch_as_outcome_max_iterations_marks_blocked(agent_with_goal):
    """Outcome max_iterations_reached → sub-goal blocked with reason citing run_id."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    run_id = "outcome-20260507-maxiter-001"
    outcome_result = _make_outcome_result("max_iterations_reached", run_id=run_id)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        result, sg = gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="## criteria\nMust be complete.\n",
        )

    assert sg.status == "blocked"
    # The history should mention the run_id and reason
    assert run_id in gm._goal.body
    assert "max_iterations_reached" in gm._goal.body


# ──────────────────────────────────────────────────────────────────
# Test 3: failed → sub-goal becomes blocked with judge explanation


def test_dispatch_as_outcome_failed_marks_blocked(agent_with_goal):
    """Outcome failed → sub-goal blocked, judge explanation visible in history."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result(
        "failed",
        explanation="Rubric and description are fundamentally incompatible.",
    )

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        result, sg = gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="## criteria\nMust satisfy X.\n",
        )

    assert sg.status == "blocked"
    assert "Rubric and description are fundamentally incompatible" in gm._goal.body


# ──────────────────────────────────────────────────────────────────
# Test 4: interrupted → sub-goal stays in_progress


def test_dispatch_as_outcome_interrupted_leaves_in_progress(agent_with_goal):
    """Outcome interrupted → sub-goal stays in_progress (cost cap hit; operator retries)."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("interrupted")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        result, sg = gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="## criteria\nMust be complete.\n",
        )

    assert sg.status == "in_progress"


# ──────────────────────────────────────────────────────────────────
# Test 5: refuses when sub-goal is blocked


def test_dispatch_as_outcome_refuses_when_blocked(agent_with_goal):
    """Sub-goal in 'blocked' state → dispatch_as_outcome raises GoalCorrupted."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    # ch_5 is blocked
    with pytest.raises(GoalCorrupted, match="blocked"):
        gm.dispatch_as_outcome(sub_goal_id="ch_5", rubric="inline rubric text")


# ──────────────────────────────────────────────────────────────────
# Test 6: refuses when sub-goal is already complete


def test_dispatch_as_outcome_refuses_when_complete(agent_with_goal):
    """Sub-goal already complete → dispatch_as_outcome raises GoalCorrupted."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    # ch_1 is complete
    with pytest.raises(GoalCorrupted, match="complete"):
        gm.dispatch_as_outcome(sub_goal_id="ch_1", rubric="some rubric")


# ──────────────────────────────────────────────────────────────────
# Test 7: refuses when blocked_by dependency is unresolved


def test_dispatch_as_outcome_refuses_when_blocked_by_unresolved(agent_with_goal):
    """Sub-goal with unresolved blocked_by → raises GoalCorrupted."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    # ch_3 has blocked_by=ch_2, which is pending (not complete)
    with pytest.raises(GoalCorrupted, match="unresolved blocked_by"):
        gm.dispatch_as_outcome(sub_goal_id="ch_3", rubric="some rubric")


# ──────────────────────────────────────────────────────────────────
# Test 8: marks in_progress BEFORE calling OutcomeRunner


def test_dispatch_as_outcome_marks_in_progress_before_running(agent_with_goal):
    """Sub-goal was pending; must flip to in_progress BEFORE OutcomeRunner.run is called."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    # ch_2 starts as pending
    assert gm.find_sub_goal("ch_2").status == "pending"

    status_at_run_time: list[str] = []

    def capture_status_on_run(*args, **kwargs):
        # At this point, dispatch_as_outcome should have already set in_progress
        status_at_run_time.append(gm.find_sub_goal("ch_2").status)
        return _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.side_effect = capture_status_on_run
        MockRunner.return_value = mock_runner_instance

        gm.dispatch_as_outcome(sub_goal_id="ch_2", rubric="inline rubric")

    assert status_at_run_time == ["in_progress"], (
        "sub-goal must be in_progress before OutcomeRunner.run is called"
    )


# ──────────────────────────────────────────────────────────────────
# Test 9: writes goal_history.jsonl entry with all required fields


def test_dispatch_as_outcome_writes_history_entry(agent_with_goal):
    """After a run, goal_history.jsonl has a sub_goal_outcome_dispatched event."""
    agents_root, agent_name = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    run_id = "outcome-20260507-histtest-aaaa"
    outcome_result = _make_outcome_result(
        "satisfied", run_id=run_id, iterations=2, total_cost_usd=0.0099
    )

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        gm.dispatch_as_outcome(sub_goal_id="ch_2", rubric="inline rubric text")

    history_path = agents_root / agent_name / "goal_history.jsonl"
    assert history_path.exists(), "goal_history.jsonl should have been created"

    lines = [l.strip() for l in history_path.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1, "At least one JSONL entry expected"

    # Find the sub_goal_outcome_dispatched entry
    entries = [json.loads(l) for l in lines]
    dispatched = [e for e in entries if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1

    entry = dispatched[0]
    assert entry["sub_goal_id"] == "ch_2"
    assert entry["outcome_run_id"] == run_id
    assert entry["terminal_state"] == "satisfied"
    assert entry["applied_status"] == "complete"
    assert entry["iterations"] == 2
    assert entry["total_cost_usd"] == pytest.approx(0.0099)
    assert "ts" in entry


# ──────────────────────────────────────────────────────────────────
# Test 10: return tuple shape — OutcomeResult + updated SubGoal


def test_dispatch_as_outcome_returns_result_and_subgoal(agent_with_goal):
    """Return value is a 2-tuple (OutcomeResult, SubGoal) with correct final status."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        returned = gm.dispatch_as_outcome(sub_goal_id="ch_2", rubric="some rubric")

    assert isinstance(returned, tuple)
    assert len(returned) == 2
    result, sg = returned
    assert result is outcome_result
    assert isinstance(sg, SubGoal)
    assert sg.id == "ch_2"
    assert sg.status == "complete"


# ──────────────────────────────────────────────────────────────────
# Test 11: max_iterations is passed through to OutcomeRunner.run


def test_dispatch_as_outcome_passes_max_iterations_through(agent_with_goal):
    """max_iterations kwarg is forwarded to OutcomeRunner.run correctly."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="some rubric text",
            max_iterations=7,
        )

        # Verify OutcomeRunner.run was called with max_iterations=7
        call_kwargs = mock_runner_instance.run.call_args
        assert (
            call_kwargs.kwargs.get("max_iterations") == 7 or call_kwargs.args[2] == 7
        ), (
            f"max_iterations=7 should have been passed to OutcomeRunner.run; "
            f"got: {call_kwargs}"
        )


# ──────────────────────────────────────────────────────────────────
# Test 12: description-building uses sub-goal fields


def test_build_outcome_description_includes_label_body_criteria(agent_with_goal):
    """_build_outcome_description_from_sub_goal surfaces label, body, and criteria."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    sg = gm.find_sub_goal("ch_2")
    description = gm._build_outcome_description_from_sub_goal(sg)

    assert "Chapter 2 first draft" in description
    assert "character development" in description
    assert "3000 words" in description
    assert "Character arc is clear" in description


def test_build_outcome_description_without_body_or_criteria(agent_with_goal):
    """_build_outcome_description_from_sub_goal works for minimal sub-goals (label only)."""
    gm = _make_gm(agent_with_goal)
    gm.load()

    # ch_1 has no body or acceptance_criteria
    sg = gm.find_sub_goal("ch_1")
    description = gm._build_outcome_description_from_sub_goal(sg)

    assert "Chapter 1 first draft" in description
    assert "Acceptance criteria" not in description

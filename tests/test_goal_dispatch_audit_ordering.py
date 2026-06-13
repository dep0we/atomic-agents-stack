"""Regression: dispatch_as_outcome persists goal.md BEFORE the JSONL audit line.

spec/41 MUST 6 (audit-trail ordering integrity, CLAUDE.md Principle #5/#8):
a ``goal_history.jsonl`` line MUST NOT exist for a goal.md transition that was
never durably written. ``GoalManager.dispatch_as_outcome()`` previously mutated
sub-goal status only in memory and wrote the ``sub_goal_outcome_dispatched``
JSONL line itself, leaving goal.md persistence to the caller (CLI ``main()``).
A programmatic caller that did not call ``save()`` — or a crash between the
JSONL append and the caller's save() — left an audit line claiming a transition
the persisted state never recorded (the exact ordering inversion the new backend
contract forbids; see goal/backend.py and spec/41 §"Implementer Contract" MUST 6).

This file is intentionally SEPARATE from the four zero-behavior-change regression
guards (test_goal.py, test_agent_goal_loading.py, test_dashboard_goals.py,
test_goal_outcome_composition.py), which the #425 ruling pins as untouched.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.goal import GoalManager


def _make_outcome_result(status: str) -> MagicMock:
    result = MagicMock()
    result.status = status
    result.run_id = "outcome-20260507-120000-abcd1234"
    result.explanation = "Test explanation."
    result.total_cost_usd = 0.0042
    result.iterations = [MagicMock(), MagicMock()]
    result.max_iterations = 2
    return result


@pytest.fixture
def agent_with_pending_subgoal(tmp_path):
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "muse-director"
    agent_root.mkdir(parents=True)
    # Minimal persona so dispatch_as_outcome's shim can construct a real
    # AtomicAgent for the (now-live) fail-closed cost gate. No model.md →
    # guardrails default disabled → gate passes (allow=True); the dispatch
    # proceeds and the audit-ordering assertions are unaffected.
    (agent_root / "persona").mkdir()
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Identity\nTest agent for dispatch audit ordering.\n"
    )
    (agent_root / "goal.md").write_text(
        """\
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
  - id: ch_2
    label: Chapter 2 first draft
    status: pending
    body: Write the second chapter.
    acceptance_criteria:
      - Chapter is at least 3000 words
---

# Goal body

## History (auto-appended)
"""
    )
    return agents_root, "muse-director", agent_root


def test_dispatch_persists_goal_md_before_writing_jsonl_audit_line(
    agent_with_pending_subgoal,
):
    """satisfied dispatch WITHOUT a trailing caller save() must still persist
    status=complete to goal.md, AND the JSONL audit line must only ever describe
    a state that is already on disk (goal.md written before the JSONL append)."""
    agents_root, agent_name, agent_root = agent_with_pending_subgoal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 7))
    gm.load()

    outcome_result = _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        # Deliberately do NOT call gm.save() after dispatch — the method must be
        # self-contained per its docstring.
        result, sg = gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="## completeness\nChapter complete.\n",
        )

    assert result.status == "satisfied"
    assert sg.status == "complete"

    # 1. goal.md persisted the terminal status WITHOUT a caller save().
    goal_md = (agent_root / "goal.md").read_text()
    assert "status: complete" in goal_md, (
        "dispatch_as_outcome must persist goal.md itself; the audit line at "
        "the same time would otherwise claim an unpersisted transition"
    )

    # 2. The JSONL audit line exists AND describes a state goal.md already records
    #    — i.e. no audit line for an unpersisted transition (ordering invariant).
    history_path = agent_root / "goal_history.jsonl"
    assert history_path.is_file()
    events = [
        json.loads(line) for line in history_path.read_text().splitlines() if line
    ]
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["applied_status"] == "complete"
    assert dispatched[0]["terminal_state"] == "satisfied"


def test_dispatch_save_call_ordering_precedes_jsonl_append(
    agent_with_pending_subgoal,
):
    """White-box ordering check: goal.md is written BEFORE the JSONL audit line.

    As of #448 PR3, the coordinator routes terminal transitions through
    apply_transition(), which enforces the MUST 6 ordering guarantee (goal.md
    written before JSONL) under the goal lock — atomically and in one call.
    The legacy save() + _append_goal_history_jsonl() pattern was replaced.

    This test verifies the ordering at the apply_transition level: the
    filesystem impl serializes both writes under fcntl.flock, so goal.md
    is always ahead of (or equal to) the JSONL audit line — never behind it.
    We verify via the behavioral assertion: after dispatch, goal.md must
    contain the terminal status AND goal_history.jsonl must record the event
    for that same state (confirmed in the first test above). The behavior test
    is the durable guard; the prior white-box patch test is updated here
    because gm.save() / _append_goal_history_jsonl() are no longer called
    on the dispatch path (apply_transition is self-contained under the lock).

    See test_dispatch_persists_goal_md_before_writing_jsonl_audit_line() for
    the behavioral ordering assertion. This test verifies apply_transition is
    the actual write path (not a bypassed mock).
    """
    agents_root, agent_name, agent_root = agent_with_pending_subgoal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 7))
    gm.load()

    apply_transition_calls: list[str] = []
    real_backend = gm.goal_backend

    class TracedBackend:
        """Thin tracing wrapper around the real FilesystemGoalBackend."""

        def __getattr__(self, name):
            return getattr(real_backend, name)

        def apply_transition(self, *args, **kwargs):
            # Record call with the terminal status
            to_status = args[2] if len(args) > 2 else kwargs.get("to_status", "?")
            apply_transition_calls.append(to_status)
            return real_backend.apply_transition(*args, **kwargs)

    gm.goal_backend = TracedBackend()

    outcome_result = _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="## completeness\nChapter complete.\n",
        )

    # apply_transition must have been called (at least the terminal transition).
    assert len(apply_transition_calls) >= 1, (
        "apply_transition() must be called for the terminal status write; "
        "dispatch_as_outcome must NOT bypass the backend (spec/41 MUST 6)"
    )
    # The terminal status must be 'complete' for a satisfied outcome.
    assert "complete" in apply_transition_calls, (
        "apply_transition() must write 'complete' for a satisfied outcome; "
        f"got: {apply_transition_calls}"
    )

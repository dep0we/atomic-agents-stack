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
from pathlib import Path
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
    """White-box ordering check: save() is invoked BEFORE _append_goal_history_jsonl().

    A crash between the two writes must leave goal.md ahead of (or equal to) the
    audit log, never behind it — so save() must fire first.
    """
    agents_root, agent_name, agent_root = agent_with_pending_subgoal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 7))
    gm.load()

    call_order: list[str] = []
    real_save = gm.save
    real_append = gm._append_goal_history_jsonl

    def traced_save():
        call_order.append("save")
        return real_save()

    def traced_append(entry):
        call_order.append("jsonl")
        return real_append(entry)

    outcome_result = _make_outcome_result("satisfied")

    with (
        patch("atomic_agents.outcome.OutcomeRunner") as MockRunner,
        patch.object(gm, "save", side_effect=traced_save),
        patch.object(gm, "_append_goal_history_jsonl", side_effect=traced_append),
    ):
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="## completeness\nChapter complete.\n",
        )

    assert "save" in call_order and "jsonl" in call_order
    assert call_order.index("save") < call_order.index("jsonl"), (
        "goal.md (save) must be persisted BEFORE the JSONL audit line is appended "
        "(spec/41 MUST 6 ordering); got call order: " + ", ".join(call_order)
    )

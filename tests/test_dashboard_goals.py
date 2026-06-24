"""Tests for atomic_agents.dashboard.goals — aggregation layer."""

from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.dashboard.goals import (
    aggregate_goals,
    has_any_goal,
    _load_goal_data,
    _load_blocked_at_from_history,
)


def _write_goal(
    agent_root: Path,
    intent: str,
    sub_goals: list[dict],
    priority: str = "high",
    created: str | None = None,
    deadline: str | None = None,
) -> Path:
    agent_root.mkdir(parents=True, exist_ok=True)
    today = date.today()
    fm: dict = {
        "schema_version": 1,
        "active": True,
        "intent": intent,
        "priority": priority,
        "created": created or today.isoformat(),
        "last_progress_check": today.isoformat(),
        "success_criteria": ["all done"],
        "sub_goals": sub_goals,
    }
    if deadline:
        fm["deadline"] = deadline

    lines = ["---"]
    lines.append(f"schema_version: {fm['schema_version']}")
    lines.append(f"active: true")
    lines.append(f"intent: {fm['intent']}")
    lines.append(f"priority: {fm['priority']}")
    lines.append(f"created: {fm['created']}")
    lines.append(f"last_progress_check: {fm['last_progress_check']}")
    lines.append("success_criteria:")
    for c in fm["success_criteria"]:
        lines.append(f"  - {c}")
    lines.append("sub_goals:")
    for sg in sub_goals:
        lines.append(f"  - id: {sg['id']}")
        lines.append(f"    label: {sg.get('label', sg['id'])}")
        lines.append(f"    status: {sg.get('status', 'pending')}")
        if sg.get("blocked_by"):
            lines.append(f"    blocked_by: {sg['blocked_by']}")
    if deadline:
        lines.append(f"deadline: {deadline}")
    lines.append("---")
    lines.append("# Goal")

    goal_path = agent_root / "goal.md"
    goal_path.write_text("\n".join(lines) + "\n")
    return goal_path


def _make_agent(agents_root: Path, agent: str) -> Path:
    """Create minimum agent structure discoverable by AgentRegistryBackend (spec/37:314 predicate).

    model.md must be present; log/ is created for run-history tests.
    """
    agent_root = agents_root / agent
    (agent_root / "log").mkdir(parents=True, exist_ok=True)
    (agent_root / "model.md").write_text("# model\n")
    return agent_root


def test_has_any_goal_true(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    _write_goal(
        agent_root, "Ship v2", [{"id": "sg1", "label": "Build", "status": "pending"}]
    )
    assert has_any_goal(tmp_path) is True


def test_has_any_goal_false(tmp_path):
    _make_agent(tmp_path, "alice")
    assert has_any_goal(tmp_path) is False


def test_active_goal_extraction(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    _write_goal(
        agent_root,
        "Ship v2",
        [
            {"id": "sg1", "label": "Design", "status": "complete"},
            {"id": "sg2", "label": "Build", "status": "in_progress"},
            {"id": "sg3", "label": "Test", "status": "pending"},
        ],
    )

    data = aggregate_goals(tmp_path)
    assert len(data.active_goals) == 1
    g = data.active_goals[0]
    assert g.agent == "alice"
    assert g.intent == "Ship v2"
    assert g.total_sub_goals == 3
    assert g.status_counts.get("complete", 0) == 1
    assert g.status_counts.get("in_progress", 0) == 1
    assert g.status_counts.get("pending", 0) == 1


def test_blocked_sub_goal_queue(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    _write_goal(
        agent_root,
        "Research",
        [
            {"id": "sg1", "label": "Gather data", "status": "complete"},
            {"id": "sg2", "label": "Analyze", "status": "blocked", "blocked_by": "sg1"},
        ],
    )

    data = aggregate_goals(tmp_path)
    assert len(data.blocked_sub_goals) == 1
    b = data.blocked_sub_goals[0]
    assert b.sub_goal_id == "sg2"
    assert b.blocked_by == "sg1"


def test_no_blocked_when_all_clear(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    _write_goal(
        agent_root,
        "Write docs",
        [
            {"id": "sg1", "label": "Outline", "status": "complete"},
            {"id": "sg2", "label": "Draft", "status": "in_progress"},
        ],
    )

    data = aggregate_goals(tmp_path)
    assert data.blocked_sub_goals == []


def test_outcome_iteration_histogram(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    _write_goal(agent_root, "Goal", [{"id": "sg1", "label": "L", "status": "pending"}])

    today = date.today()
    outcomes_dir = agent_root / "outcomes" / "runs"
    outcomes_dir.mkdir(parents=True)

    # 2 runs with 1 iteration, 1 run with 3 iterations — all satisfied, recent
    for i, n_iters in enumerate([1, 1, 3]):
        run_dir = outcomes_dir / f"run-{i:03d}"
        run_dir.mkdir()
        result = {
            "run_id": f"run-{i:03d}",
            "description": "test",
            "status": "satisfied",
            "started_at": today.isoformat(),
            "iterations": [{"iteration": j} for j in range(n_iters)],
            "max_iterations": 5,
            "total_cost_usd": 0.10,
        }
        (run_dir / "result.json").write_text(json.dumps(result))

    data = aggregate_goals(tmp_path)
    assert data.iteration_histogram.get(1, 0) == 2
    assert data.iteration_histogram.get(3, 0) == 1


def test_outcome_histogram_excludes_old_runs(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    _write_goal(agent_root, "Goal", [{"id": "sg1", "label": "L", "status": "pending"}])

    outcomes_dir = agent_root / "outcomes" / "runs"
    outcomes_dir.mkdir(parents=True)

    # Run from 100 days ago — outside 90d window
    old_date = (date.today() - timedelta(days=100)).isoformat()
    run_dir = outcomes_dir / "run-old"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "run_id": "run-old",
                "description": "old",
                "status": "satisfied",
                "started_at": old_date,
                "iterations": [{"iteration": 1}],
                "max_iterations": 3,
                "total_cost_usd": 0.05,
            }
        )
    )

    data = aggregate_goals(tmp_path, outcome_histogram_days=90)
    assert data.iteration_histogram == {}  # old run excluded


def test_no_goals_returns_empty(tmp_path):
    _make_agent(tmp_path, "alice")  # agent exists but no goal.md
    data = aggregate_goals(tmp_path)
    assert data.active_goals == []
    assert data.blocked_sub_goals == []
    assert data.has_any_goal is False


def test_days_since_start_calculated(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    created = (date.today() - timedelta(days=10)).isoformat()
    _write_goal(
        agent_root,
        "Goal",
        [{"id": "sg1", "label": "L", "status": "pending"}],
        created=created,
    )

    data = aggregate_goals(tmp_path)
    g = data.active_goals[0]
    assert g.days_since_start == 10


def test_overdue_deadline_detected(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _write_goal(
        agent_root,
        "Goal",
        [{"id": "sg1", "label": "L", "status": "pending"}],
        deadline=yesterday,
    )

    data = aggregate_goals(tmp_path)
    g = data.active_goals[0]
    assert g.is_overdue is True


def test_future_deadline_not_overdue(tmp_path):
    agent_root = _make_agent(tmp_path, "alice")
    next_month = (date.today() + timedelta(days=30)).isoformat()
    _write_goal(
        agent_root,
        "Goal",
        [{"id": "sg1", "label": "L", "status": "pending"}],
        deadline=next_month,
    )

    data = aggregate_goals(tmp_path)
    g = data.active_goals[0]
    assert g.is_overdue is False
    assert g.days_until_deadline is not None
    assert g.days_until_deadline > 0


def test_load_blocked_at_from_history(tmp_path):
    agent_root = tmp_path / "alice"
    agent_root.mkdir(parents=True)
    events = [
        {
            "event": "sub_goal_blocked",
            "sub_goal_id": "sg2",
            "ts": "2026-05-01T10:00:00Z",
        },
        {
            "event": "sub_goal_outcome_dispatched",
            "sub_goal_id": "sg1",
            "ts": "2026-05-02T10:00:00Z",
        },
    ]
    history_path = agent_root / "goal_history.jsonl"
    history_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    result = _load_blocked_at_from_history(agent_root)
    assert "sg2" in result
    assert result["sg2"] == "2026-05-01T10:00:00Z"

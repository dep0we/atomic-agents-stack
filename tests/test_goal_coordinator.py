"""Tests for goal/coordinator.py — dispatch_sub_goal_as_outcome().

Coverage required by arc-rulings/448-pr3-args.json:
  - Four terminal-state mappings (satisfied→complete, max_iterations_reached→blocked,
    failed→blocked, interrupted→in_progress)
  - pending→in_progress pre-transition
  - blocked_by-unresolved refusal
  - not-pending/in_progress refusal
  - Cost-guardrail-blocked path: BOTH CostGuardrailBlocked raised AND
    coordinator_dispatch_rejected event appended (the #425 test gap — must assert both)
  - Re-dispatch CAS rejection (expected_from_status mismatch → GoalConcurrentModification,
    no stale write)

Mirrors test_goal_outcome_composition.py's patch('atomic_agents.outcome.OutcomeRunner')
+ MagicMock pattern.

Clock contract (#483 PR1 — spec/41 MUST 6 + addendum):
apply_transition() now accepts `when: date | None = None`, which controls the
prose-history date prefix (`## History` bullet). The coordinator threads
`when=goal_manager.today` into both apply_transition() calls, so the prose date
IS deterministic and prose-date assertions ARE valid — assert against the injected
date (goal_manager.today.isoformat()). The JSONL `ts` field is independent: it is
the caller-supplied wall-clock audit timestamp and is NOT affected by `when`.
(The pinned-clock prose-date conformance guard lives in
test_goal_backend_conformance.py TEST 59.)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.exceptions import (
    CostGuardrailBlocked,
    GoalConcurrentModification,
    GoalCorrupted,
)
from atomic_agents.goal import GoalManager
from atomic_agents.goal.coordinator import dispatch_sub_goal_as_outcome


# ──────────────────────────────────────────────────────────────────
# Helpers / fixtures


def _make_outcome_result(
    status: str,
    run_id: str = "outcome-20260613-coord-abcd1234",
    explanation: str = "Test explanation.",
    iterations: int = 2,
    total_cost_usd: float = 0.0042,
) -> MagicMock:
    """Build a minimal OutcomeResult mock for coordinator tests."""
    result = MagicMock()
    result.status = status
    result.run_id = run_id
    result.explanation = explanation
    result.total_cost_usd = total_cost_usd
    result.iterations = [MagicMock() for _ in range(iterations)]
    result.max_iterations = iterations
    return result


def _make_cost_check_result(allow: bool, reason: str = "") -> MagicMock:
    """Build a CostCheckResult-like mock for the cost gate test.

    The coordinator reads result.allow (bool) and result.reason (str) as
    dataclass attributes — NOT tuple-unpacked, NOT `if result:` (always truthy).
    """
    result = MagicMock()
    result.allow = allow
    result.reason = reason
    return result


@pytest.fixture
def agent_with_goal(tmp_path):
    """Agent vault with a goal containing sub-goals in various states."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "coord-test-agent"
    agent_root.mkdir(parents=True)

    goal_text = """\
---
schema_version: 1
active: true
intent: Test coordinator dispatch
priority: high
created: 2026-06-01
deadline: 2026-12-31
last_progress_check: 2026-06-01
success_criteria:
  - Task complete
sub_goals:
  - id: sg_pending
    label: Pending sub-goal
    status: pending
    body: This sub-goal is pending dispatch.
    acceptance_criteria:
      - Output is correct
  - id: sg_in_progress
    label: In-progress sub-goal
    status: in_progress
  - id: sg_complete
    label: Complete sub-goal
    status: complete
    completed: 2026-06-01
  - id: sg_blocked_complete_dep
    label: Sub-goal with complete dependency
    status: pending
    blocked_by: sg_complete
  - id: sg_blocked_pending_dep
    label: Sub-goal blocked by incomplete dep
    status: pending
    blocked_by: sg_in_progress
---

# Goal body

## History (auto-appended)

- 2026-06-01 — sub_goal `sg_complete` complete
"""
    (agent_root / "goal.md").write_text(goal_text)
    return agents_root, "coord-test-agent", agent_root


def _make_gm(agent_with_goal, today=None):
    agents_root, agent_name, agent_root = agent_with_goal
    if today is None:
        today = date(2026, 6, 13)
    return GoalManager(agents_root, agent_name, today=today)


def _make_agent_mock(allow: bool = True, reason: str = ""):
    """Build a mock AtomicAgent whose cost gate returns the given result."""
    agent = MagicMock()
    agent._check_cost_guardrails.return_value = _make_cost_check_result(allow, reason)
    return agent


def _read_history(agent_root: Path) -> list[dict]:
    """Read and parse all events from goal_history.jsonl."""
    history_path = agent_root / "goal_history.jsonl"
    if not history_path.exists():
        return []
    return [
        json.loads(line)
        for line in history_path.read_text().splitlines()
        if line.strip()
    ]


# ──────────────────────────────────────────────────────────────────
# Test 1: satisfied → complete (terminal mapping)


def test_coordinator_satisfied_maps_to_complete(agent_with_goal):
    """satisfied outcome → sub-goal complete, sub_goal_outcome_dispatched event."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    run_id = "outcome-20260613-sat-001"
    outcome_result = _make_outcome_result("satisfied", run_id=run_id)
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        result, sg = dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: acceptance criteria",
        )

    assert result.status == "satisfied"
    assert sg.status == "complete"

    # On-disk state must also be 'complete'
    reloaded = gm.goal_backend.load_goal(agent_name)
    sg_disk = next(s for s in reloaded.sub_goals if s.id == "sg_pending")
    assert sg_disk.status == "complete"
    assert sg_disk.completed is not None

    # Exactly one sub_goal_outcome_dispatched event in JSONL
    events = _read_history(agent_root)
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    entry = dispatched[0]
    assert entry["sub_goal_id"] == "sg_pending"
    assert entry["outcome_run_id"] == run_id
    assert entry["terminal_state"] == "satisfied"
    assert entry["applied_status"] == "complete"
    assert entry["iterations"] == len(outcome_result.iterations)
    assert "ts" in entry


# ──────────────────────────────────────────────────────────────────
# Test 2: max_iterations_reached → blocked


def test_coordinator_max_iterations_maps_to_blocked(agent_with_goal):
    """max_iterations_reached outcome → sub-goal blocked, blocked_by=None."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    run_id = "outcome-20260613-maxiter-001"
    outcome_result = _make_outcome_result("max_iterations_reached", run_id=run_id)
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        result, sg = dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: acceptance criteria",
        )

    assert result.status == "max_iterations_reached"
    assert sg.status == "blocked"

    # blocked_by must be cleared (no sub-goal blocker — narrative in history)
    reloaded = gm.goal_backend.load_goal(agent_name)
    sg_disk = next(s for s in reloaded.sub_goals if s.id == "sg_pending")
    assert sg_disk.status == "blocked"
    assert sg_disk.blocked_by is None

    events = _read_history(agent_root)
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["terminal_state"] == "max_iterations_reached"
    assert dispatched[0]["applied_status"] == "blocked"


# ──────────────────────────────────────────────────────────────────
# Test 3: failed → blocked


def test_coordinator_failed_maps_to_blocked(agent_with_goal):
    """failed outcome → sub-goal blocked, blocked_by=None."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result(
        "failed", explanation="Judge found output insufficient."
    )
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        result, sg = dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: acceptance criteria",
        )

    assert result.status == "failed"
    assert sg.status == "blocked"

    reloaded = gm.goal_backend.load_goal(agent_name)
    sg_disk = next(s for s in reloaded.sub_goals if s.id == "sg_pending")
    assert sg_disk.status == "blocked"
    assert sg_disk.blocked_by is None

    events = _read_history(agent_root)
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["terminal_state"] == "failed"
    assert dispatched[0]["applied_status"] == "blocked"


# ──────────────────────────────────────────────────────────────────
# Test 4: interrupted → in_progress (stays in_progress)


def test_coordinator_interrupted_stays_in_progress(agent_with_goal):
    """interrupted outcome → sub-goal stays in_progress; CAS passes; audit event lands."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("interrupted")
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        result, sg = dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: acceptance criteria",
        )

    assert result.status == "interrupted"
    # Sub-goal stays in_progress (set by pre-transition, not changed by terminal)
    assert sg.status == "in_progress"

    reloaded = gm.goal_backend.load_goal(agent_name)
    sg_disk = next(s for s in reloaded.sub_goals if s.id == "sg_pending")
    assert sg_disk.status == "in_progress"

    # Audit event must land with applied_status='in_progress'
    events = _read_history(agent_root)
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["terminal_state"] == "interrupted"
    assert dispatched[0]["applied_status"] == "in_progress"


# ──────────────────────────────────────────────────────────────────
# Test 5: pending → in_progress pre-transition fires before run


def test_coordinator_pre_transition_pending_to_in_progress(agent_with_goal):
    """Sub-goal must be in_progress BEFORE OutcomeRunner.run() is called."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    status_at_run_time: list[str] = []

    def capture_status(*args, **kwargs):
        # At run time, sub-goal must already be in_progress on disk
        reloaded = gm.goal_backend.load_goal(agent_name)
        sg = next(s for s in reloaded.sub_goals if s.id == "sg_pending")
        status_at_run_time.append(sg.status)
        return _make_outcome_result("satisfied")

    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.side_effect = capture_status
        MockRunner.return_value = mock_instance

        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: check",
        )

    assert status_at_run_time == ["in_progress"], (
        "sub-goal must be in_progress on disk before OutcomeRunner.run() is called "
        "(spec/41 §'Goal-outcome composition' lock discipline)"
    )


# ──────────────────────────────────────────────────────────────────
# Test 6: blocked_by unresolved → GoalCorrupted


def test_coordinator_blocked_by_unresolved_raises(agent_with_goal):
    """Sub-goal with unresolved blocked_by → GoalCorrupted (blocker not complete)."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    agent = _make_agent_mock(allow=True)

    with pytest.raises(GoalCorrupted, match="unresolved blocked_by"):
        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_blocked_pending_dep",
            rubric="inline: check",
        )


# ──────────────────────────────────────────────────────────────────
# Test 7: completed dependency is resolved — dispatch proceeds


def test_coordinator_complete_blocked_by_proceeds(agent_with_goal):
    """Sub-goal blocked_by a complete dep → dispatch proceeds normally."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("satisfied")
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        result, sg = dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_blocked_complete_dep",
            rubric="inline: check",
        )

    assert result.status == "satisfied"
    assert sg.status == "complete"


# ──────────────────────────────────────────────────────────────────
# Test 8: non-pending/in_progress sub-goal → GoalCorrupted


def test_coordinator_terminal_sub_goal_raises(agent_with_goal):
    """Sub-goal already complete → GoalCorrupted (only pending/in_progress accepted)."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    agent = _make_agent_mock(allow=True)

    with pytest.raises(GoalCorrupted, match="complete"):
        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_complete",
            rubric="inline: check",
        )


# ──────────────────────────────────────────────────────────────────
# Test 9: cost-guardrail-blocked path — MUST assert BOTH raise AND event
#
# This is the CRITICAL test that the #425 removal identified as missing.
# The test MUST assert:
#   (1) CostGuardrailBlocked is raised
#   (2) A coordinator_dispatch_rejected event was appended to goal_history.jsonl
#   (3) OutcomeRunner.run was NOT called
#   (4) Sub-goal is still 'pending' (no apply_transition called on blocked path)
#
# A test asserting only (1) gives false confidence the append-then-raise ordering
# is correct. A test asserting only (2) gives false confidence the gate blocks.


def test_coordinator_cost_guardrail_blocked_asserts_both_raise_and_event(
    agent_with_goal,
):
    """Cost gate blocked → CostGuardrailBlocked raised AND coordinator_dispatch_rejected
    event appended to goal_history.jsonl. Sub-goal stays pending (no dispatch).

    BOTH assertions are required (arc-rulings §'tierB-test-layout' + prep finding P0).
    A test asserting only the raise silently leaves the audit-append path untested
    (the exact failure that went undetected in #425). The shortcut-hunter adversarial
    round must check that BOTH assertions are present in this test.
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    # Mock agent whose cost gate returns allow=False
    agent = _make_agent_mock(allow=False, reason="daily cap exceeded")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        MockRunner.return_value = mock_instance

        # (1) Assert CostGuardrailBlocked is raised
        with pytest.raises(CostGuardrailBlocked):
            dispatch_sub_goal_as_outcome(
                agent=agent,
                goal_manager=gm,
                sub_goal_id="sg_pending",
                rubric="inline: check",
            )

        # (3) OutcomeRunner.run must NOT have been called
        mock_instance.run.assert_not_called()

    # (2) Assert coordinator_dispatch_rejected event was appended to goal_history.jsonl
    events = _read_history(agent_root)
    rejected = [e for e in events if e.get("event") == "coordinator_dispatch_rejected"]
    assert len(rejected) == 1, (
        "coordinator_dispatch_rejected event must be appended to goal_history.jsonl "
        "BEFORE raising CostGuardrailBlocked (append-then-raise ordering, spec/41 MUST 10)"
    )
    entry = rejected[0]
    assert entry["sub_goal_id"] == "sg_pending"
    assert entry["reason"] == "daily cap exceeded"
    assert "ts" in entry

    # (4) Sub-goal must still be 'pending' — no apply_transition was called on blocked path
    reloaded = gm.goal_backend.load_goal(agent_name)
    sg_disk = next(s for s in reloaded.sub_goals if s.id == "sg_pending")
    assert sg_disk.status == "pending", (
        "sub-goal must remain 'pending' when cost gate blocks dispatch — "
        "apply_transition must NOT be called on the blocked path"
    )


# ──────────────────────────────────────────────────────────────────
# Test 10: dashboard collision guard — coordinator_dispatch_rejected does not
# populate blocked_at for the sub-goal (event name has no 'blocked' substring)


def test_coordinator_rejected_event_name_has_no_blocked_substring(agent_with_goal):
    """coordinator_dispatch_rejected event name must NOT contain 'blocked' substring.

    dashboard/goals.py:310 uses substring match '"blocked" in event_name' to
    populate blocked_at[sg_id]. If the coordinator's cost-gate event name contains
    'blocked', a pending sub-goal gets a spurious blocked_at timestamp in the
    dashboard, making it look blocked when it is still pending.
    Event name 'coordinator_dispatch_rejected' does not contain 'blocked'.
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    agent = _make_agent_mock(allow=False, reason="cap hit")

    with pytest.raises(CostGuardrailBlocked):
        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: check",
        )

    events = _read_history(agent_root)
    for event in events:
        event_name = str(event.get("event", ""))
        assert "blocked" not in event_name, (
            f"Event name {event_name!r} contains 'blocked' substring — "
            "will cause spurious blocked_at in dashboard/goals.py:310. "
            "Use a name without 'blocked' (e.g. 'coordinator_dispatch_rejected')."
        )


# ──────────────────────────────────────────────────────────────────
# Test 11: re-dispatch CAS rejection (expected_from_status mismatch)
#
# Simulates a race: after the coordinator's pre-transition (pending→in_progress),
# a concurrent writer moves the sub-goal to 'complete'. When the coordinator's
# terminal apply_transition fires with expected_from_status='in_progress', it
# finds 'complete' on disk → GoalConcurrentModification raised, no stale write.


def test_coordinator_redispatch_cas_rejection(agent_with_goal):
    """Terminal apply_transition rejects when sub-goal moved off in_progress during run.

    GoalConcurrentModification must be raised and no stale write must land on disk.
    This is the TOCTOU guard: the unlocked run window between pre-transition and
    terminal transition is closed by expected_from_status='in_progress' on the
    terminal apply_transition (spec/41 MUST 10 / A5 ruling).
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    # After pre-transition sets in_progress, a "concurrent writer" moves it to complete.
    real_backend = gm.goal_backend
    pre_transition_done = []

    class _SimulatedConcurrentBackend:
        """Wraps the real backend; on the SECOND apply_transition call (the terminal
        one, after the run), it pre-sets the sub-goal to 'complete' on disk before
        delegating — simulating a concurrent writer moving the goal during the run.
        """

        def __getattr__(self, name):
            return getattr(real_backend, name)

        def apply_transition(self, *args, **kwargs):
            to_status = args[2] if len(args) > 2 else kwargs.get("to_status", "?")
            if (
                pre_transition_done
                and kwargs.get("expected_from_status") == "in_progress"
            ):
                # Simulate concurrent write: move sg_pending to 'complete' on disk
                # BEFORE the terminal apply_transition runs.
                from datetime import datetime as _dt

                real_backend.apply_transition(
                    agent_id=real_backend._goal_path.parent.name
                    if hasattr(real_backend, "_goal_path")
                    else agent_name,
                    sub_goal_id="sg_pending",
                    to_status="complete",
                    fields={"completed": "2026-06-13"},
                    history_prose="sg_pending → complete (simulated concurrent write)",
                    history_event={
                        "ts": _dt.now().astimezone().isoformat(),
                        "event": "concurrent_write",
                        "sub_goal_id": "sg_pending",
                    },
                    # No expected_from_status on the concurrent write (it owns the state)
                )
            else:
                pre_transition_done.append(to_status)
            return real_backend.apply_transition(*args, **kwargs)

    gm.goal_backend = _SimulatedConcurrentBackend()

    outcome_result = _make_outcome_result("satisfied")
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        with pytest.raises(GoalConcurrentModification):
            dispatch_sub_goal_as_outcome(
                agent=agent,
                goal_manager=gm,
                sub_goal_id="sg_pending",
                rubric="inline: check",
            )

    # The simulated concurrent write set it to 'complete'; the coordinator's terminal
    # transition must NOT have overwritten it with 'complete' again (stale write guard).
    # On-disk status should be 'complete' from the concurrent write — not from any
    # stale coordinator write.
    reloaded = real_backend.load_goal(agent_name)
    sg = next(s for s in reloaded.sub_goals if s.id == "sg_pending")
    # The concurrent writer set it to 'complete'; no second write from coordinator.
    assert sg.status == "complete", (
        "Sub-goal should be 'complete' from the concurrent writer; "
        "GoalConcurrentModification means the coordinator's terminal write was rejected"
    )


# ──────────────────────────────────────────────────────────────────
# Test 12: sub_goal_outcome_dispatched event contains outcome_run_id = result.run_id
# (NOT agent.run_id — these are different lifecycles)


def test_coordinator_event_uses_outcome_run_id_not_agent_run_id(agent_with_goal):
    """sub_goal_outcome_dispatched event must carry outcome_run_id = result.run_id.

    The outcome_run_id field must be the OutcomeRunner's minted run_id (from
    OutcomeResult.run_id), NOT the AtomicAgent's run_id (a different lifecycle).
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    expected_run_id = "outcome-20260613-runid-check-9999"
    outcome_result = _make_outcome_result("satisfied", run_id=expected_run_id)
    agent = _make_agent_mock(allow=True)
    # Give the agent a different run_id to confirm they don't collide
    agent.run_id = "agent-run-id-different-from-outcome"

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: check",
        )

    events = _read_history(agent_root)
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["outcome_run_id"] == expected_run_id, (
        f"outcome_run_id must be the OutcomeRunner's run_id {expected_run_id!r}, "
        f"not the agent's run_id {agent.run_id!r}"
    )


# ──────────────────────────────────────────────────────────────────
# Test 13: exactly one sub_goal_outcome_dispatched event (not two)
# The pre-transition uses 'sub_goal_outcome_started'; only the terminal uses
# 'sub_goal_outcome_dispatched'.


def test_coordinator_emits_exactly_one_dispatched_event(agent_with_goal):
    """Exactly one sub_goal_outcome_dispatched event per dispatch call.

    The pre-transition uses event name 'sub_goal_outcome_started' (different name).
    The terminal transition uses 'sub_goal_outcome_dispatched'.
    Emitting it twice would break the frozen composition test's len(dispatched)==1 assert.
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("satisfied")
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: check",
        )

    events = _read_history(agent_root)
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    started = [e for e in events if e.get("event") == "sub_goal_outcome_started"]

    assert len(dispatched) == 1, (
        f"Exactly one sub_goal_outcome_dispatched event expected; got {len(dispatched)}"
    )
    assert len(started) == 1, (
        "Exactly one sub_goal_outcome_started event expected for the pre-transition; "
        f"got {len(started)}"
    )


# ──────────────────────────────────────────────────────────────────
# Test 14: in_progress sub-goal is accepted (idempotent pre-transition)


def test_coordinator_in_progress_subgoal_accepted(agent_with_goal):
    """Sub-goal already in_progress → dispatch proceeds; no second pre-transition."""
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    outcome_result = _make_outcome_result("satisfied")
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        result, sg = dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_in_progress",
            rubric="inline: check",
        )

    assert result.status == "satisfied"
    assert sg.status == "complete"

    # No sub_goal_outcome_started event (already in_progress; pre-transition skipped)
    events = _read_history(agent_root)
    started = [e for e in events if e.get("event") == "sub_goal_outcome_started"]
    assert len(started) == 0, (
        "sub_goal_outcome_started must NOT be emitted when sub-goal is already in_progress"
    )


# ──────────────────────────────────────────────────────────────────
# Test 15: blocked_by cleared on max_iterations_reached (prior reference)


def test_coordinator_blocked_by_cleared_on_max_iterations(tmp_path):
    """max_iterations_reached terminal transition clears any prior blocked_by reference.

    Matches the legacy dispatch_as_outcome behavior (sg.blocked_by = None on
    blocked terminal states) — the coordinator must include blocked_by: None
    in the fields dict for max_iterations_reached and failed.
    """
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "blocked-by-test"
    agent_root.mkdir(parents=True)

    goal_text = """\
---
schema_version: 1
active: true
intent: Test blocked_by clearing
priority: high
created: 2026-06-01
deadline: 2026-12-31
last_progress_check: 2026-06-01
success_criteria:
  - Done
sub_goals:
  - id: dep_sg
    label: Dependency
    status: complete
    completed: 2026-06-01
  - id: main_sg
    label: Main sub-goal
    status: pending
    blocked_by: dep_sg
---

# Goal body

## History (auto-appended)
"""
    (agent_root / "goal.md").write_text(goal_text)

    gm = GoalManager(agents_root, "blocked-by-test", today=date(2026, 6, 13))
    gm.load()

    outcome_result = _make_outcome_result("max_iterations_reached")
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        result, sg = dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="main_sg",
            rubric="inline: check",
        )

    assert sg.status == "blocked"

    # blocked_by must be cleared
    reloaded = gm.goal_backend.load_goal("blocked-by-test")
    main = next(s for s in reloaded.sub_goals if s.id == "main_sg")
    assert main.blocked_by is None, (
        "blocked_by must be cleared on max_iterations_reached terminal transition "
        "(matches legacy dispatch_as_outcome behavior)"
    )


# ──────────────────────────────────────────────────────────────────
# Test 16 (T1 — CRITICAL regression guard): exception from cost-check propagates
#
# The #425 bug swallowed a TypeError in _check_cost_guardrails into allow=True
# (fail-OPEN). The fix is structural: zero try/except in the coordinator. But
# the structural absence isn't itself tested — a future refactor adding error
# handling would silently re-open the gate. This test pins the no-swallow
# invariant by making the gate RAISE and asserting propagation.


def test_coordinator_cost_check_exception_propagates_not_swallowed(agent_with_goal):
    """Exception raised by _check_cost_guardrails must propagate out of
    dispatch_sub_goal_as_outcome — NOT be swallowed, NOT converted to
    CostGuardrailBlocked, NOT result in a successful dispatch.

    This is the headline regression guard for the #425 fail-OPEN bug.
    The structural fix (no try/except in coordinator) is valid today, but
    a future refactor adding error handling would silently re-open the gate.
    This test pins the no-swallow invariant independently of the implementation
    shape.
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    # Make the cost gate RAISE (simulates TypeError, AttributeError, etc.
    # — any exception that a broken guardrail implementation might produce).
    mock_agent = MagicMock()
    mock_agent._check_cost_guardrails.side_effect = RuntimeError("gate exploded")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        MockRunner.return_value = mock_instance

        # Exception must propagate — not be swallowed into a silent allow=True.
        with pytest.raises(RuntimeError, match="gate exploded"):
            dispatch_sub_goal_as_outcome(
                agent=mock_agent,
                goal_manager=gm,
                sub_goal_id="sg_pending",
                rubric="inline: check",
            )

        # OutcomeRunner.run must NOT have been called (gate fired before dispatch).
        mock_instance.run.assert_not_called()


# ──────────────────────────────────────────────────────────────────
# Test 17 (T2): CAS-rejection leaves no orphan audit line
#
# The existing CAS-rejection test (Test 11) asserts GoalConcurrentModification
# is raised. It does NOT assert that no sub_goal_outcome_dispatched event was
# written after the rejection. A rejected terminal write must leave no orphan
# audit line (the rejected transition never completed, so no dispatch event
# should exist).


def test_coordinator_cas_rejection_leaves_no_dispatched_event(agent_with_goal):
    """GoalConcurrentModification from a CAS mismatch must NOT leave an orphan
    sub_goal_outcome_dispatched event in goal_history.jsonl.

    The rejected terminal write means the transition never completed — no
    dispatch audit line should exist after the exception.
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    # Same concurrent-writer simulation as Test 11.
    real_backend = gm.goal_backend
    pre_transition_done = []

    class _SimulatedConcurrentBackend:
        def __getattr__(self, name):
            return getattr(real_backend, name)

        def apply_transition(self, *args, **kwargs):
            to_status = args[2] if len(args) > 2 else kwargs.get("to_status", "?")
            if (
                pre_transition_done
                and kwargs.get("expected_from_status") == "in_progress"
            ):
                from datetime import datetime as _dt

                real_backend.apply_transition(
                    agent_id=agent_name,
                    sub_goal_id="sg_pending",
                    to_status="complete",
                    fields={"completed": "2026-06-13"},
                    history_prose="sg_pending → complete (simulated concurrent write)",
                    history_event={
                        "ts": _dt.now().astimezone().isoformat(),
                        "event": "concurrent_write",
                        "sub_goal_id": "sg_pending",
                    },
                )
            else:
                pre_transition_done.append(to_status)
            return real_backend.apply_transition(*args, **kwargs)

    gm.goal_backend = _SimulatedConcurrentBackend()

    outcome_result = _make_outcome_result("satisfied")
    agent = _make_agent_mock(allow=True)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_instance = MagicMock()
        mock_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_instance

        with pytest.raises(GoalConcurrentModification):
            dispatch_sub_goal_as_outcome(
                agent=agent,
                goal_manager=gm,
                sub_goal_id="sg_pending",
                rubric="inline: check",
            )

    # After the CAS rejection, no sub_goal_outcome_dispatched event must exist.
    # Only the concurrent_write event (from the simulated concurrent writer) should
    # be present — NOT a coordinator dispatch event.
    events = _read_history(agent_root)
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 0, (
        "A rejected terminal write (GoalConcurrentModification) must NOT leave "
        "an orphan sub_goal_outcome_dispatched event in goal_history.jsonl. "
        f"Found {len(dispatched)} orphan event(s): {dispatched}"
    )


# ──────────────────────────────────────────────────────────────────
# Test 18 (T3): blocked_by references a sub_goal that does NOT EXIST at all
#
# The existing Test 6 covers blocked_by pointing to an EXISTING-but-incomplete
# sub_goal (sg_in_progress). This test covers the distinct code path where
# blocked_by names a sub_goal id that is not present in the goal at all.


def test_coordinator_blocked_by_nonexistent_raises_goal_corrupted(agent_with_goal):
    """Sub-goal blocked_by a NON-EXISTENT sub_goal id → GoalCorrupted('does not exist').

    This is a distinct code path from the 'unresolved blocked_by' branch
    (Test 6, which covers an existing-but-incomplete blocker). A dangling
    reference to an id that never existed at all is a coordinator-level
    corruption guard (coordinator.py line ~151) — the error message must
    reflect 'does not exist'.

    Implementation note: the filesystem validator (validate_goal) catches
    dangling blocked_by references at load time as SchemaValidationError, so
    this condition cannot arise through the normal disk-load path. The coordinator
    guard is defense-in-depth for in-memory mutations (e.g., a GoalManager that
    was mutated after load, or a future backend that doesn't run the validator).
    We exercise it by loading a valid goal and then directly mutating the in-memory
    sub_goal's blocked_by to a non-existent id — SubGoal is a mutable dataclass
    (spec/41 mutable-dataclass note), so this is a supported operation.
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    # Directly mutate the in-memory sub_goal to have a dangling blocked_by.
    # This bypasses the validator (which runs at disk-load time) and exercises
    # the coordinator's own "does not exist" guard directly.
    sg = gm.find_sub_goal("sg_pending")
    assert sg is not None, "fixture sub_goal sg_pending must be present"
    sg.blocked_by = "sg_nonexistent_id"

    agent = _make_agent_mock(allow=True)

    with pytest.raises(GoalCorrupted, match="does not exist"):
        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: check",
        )


# ──────────────────────────────────────────────────────────────────
# Test 19 (T4 — minor): ts is the FIRST key in coordinator_dispatch_rejected event
#
# Test 9 asserts `'ts' in entry` but not that ts is the first key.
# The append_history_event ts-first ordering contract must hold for ALL events,
# including the cost-gate rejection event.


def test_coordinator_rejected_event_ts_is_first_key(agent_with_goal):
    """coordinator_dispatch_rejected event must have 'ts' as the FIRST key.

    This pins the append_history_event ts-first key ordering contract
    (spec/41 JSONL key ordering) for the cost-gate rejection path, not just
    for the dispatch success path. 'ts' in entry is necessary but not
    sufficient — ts must also be the first key so serialized JSONL lines
    are sortable and auditable by timestamp without JSON parsing.
    """
    agents_root, agent_name, agent_root = agent_with_goal
    gm = _make_gm(agent_with_goal)
    gm.load()

    agent = _make_agent_mock(allow=False, reason="daily cap exceeded")

    with pytest.raises(CostGuardrailBlocked):
        dispatch_sub_goal_as_outcome(
            agent=agent,
            goal_manager=gm,
            sub_goal_id="sg_pending",
            rubric="inline: check",
        )

    events = _read_history(agent_root)
    rejected = [e for e in events if e.get("event") == "coordinator_dispatch_rejected"]
    assert len(rejected) == 1
    entry = rejected[0]
    assert list(entry.keys())[0] == "ts", (
        "coordinator_dispatch_rejected event must have 'ts' as the first key "
        "(append_history_event ts-first ordering contract, spec/41 JSONL key ordering). "
        f"Got key order: {list(entry.keys())}"
    )

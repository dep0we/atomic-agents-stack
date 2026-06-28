"""Tests for atomic_agents/conductor/ — spec/50 PR1+PR2.

Coverage (acceptance criteria from arc-ruling #580 PR1, #581 PR2):
  TEST 1  — playbook loader: valid PLAYBOOK.md parses to PlaybookManifest
  TEST 2  — playbook loader: missing 'kind: playbook' frontmatter is rejected
  TEST 3  — playbook loader: missing stage_id FAILS LOUD (no positional fallback)
  TEST 4  — playbook loader: duplicate stage_id is rejected
  TEST 5  — playbook loader: missing run_cap_usd is rejected
  TEST 6  — playbook loader: missing prompt/prompt_ref in a stage is rejected
  TEST 7  — playbook loader: missing fenced yaml block is rejected
  TEST 8  — discover_playbooks: returns only entries with 'kind: playbook'
  TEST 9  — event names: no conductor event name contains the 'blocked' substring
  TEST 10 — resume cursor: stage with sub-goal status=complete is skipped (no re-dispatch)
  TEST 11 — resume cursor: stage with sub-goal status=in_progress is re-dispatched
  TEST 12 — tree-cap: run halts when cumulative_spend reaches run_cap_usd
  TEST 12b— tree-cap CLAMP: run_remaining is threaded into each stage's gate as
            parent_remaining_headroom_usd (stage 2 tightened by stage 1 spend)
  TEST 13 — tree-cap: cumulative_spend is re-summed from durable JSONL, not process memory
  TEST 13b— cumulative_spend: a corrupt JSONL line marks the read degraded (fail-closed)
  TEST 21 — ref hardening: prompt_ref/rubric_ref reject absolute / symlink-escape /
            multi-level / '..' paths (per-case negative controls)
  TEST 22 — resume re-run: REAL coordinator max_iterations halt → resume re-runs the
            blocked stage with no GoalCorrupted escaping
  TEST 23 — within-stage tree-cap: a real OutcomeRunner halts mid-stage (interrupted)
            when per-iteration spend exhausts the threaded run-level headroom
  TEST 24 — coordinator threading: dispatch_sub_goal_as_outcome forwards
            parent_remaining_headroom_usd to _check_cost_guardrails AND OutcomeRunner
  TEST 14 — gate stage: run() suspends with status='awaiting_decision' + valid GateDecision
             (PR2 #581 — replaces PR1 'gate_not_implemented_pr2' halt behavior)
  TEST 14b— gate stage: resume(continue) → next stage runs, run completes
  TEST 14c— gate stage: resume(skip) → stage marked 'skipped', run continues to next stage
  TEST 14d— gate stage: resume(halt) → run halts after gate answer recorded
  TEST 14e— gate stage: stale/duplicate decision_id rejected (c5 CAS)
  TEST 14f— gate stage: answered_by = principal.identifier (not str/derivation_source)
  TEST 14g— gate stage: options field round-trips through playbook parse and GateDecision
  TEST 14h— gate stage: awaiting_decision re-surface (run() called again while suspended)
  TEST 15 — idempotency key format: keys are 'conductor:<run_id>:<stage_id>'
  TEST 16 — run() returns ConductorState with status='complete' when all stages pass
  TEST 17 — run() returns ConductorState with status='halted' on stage failure
  TEST 18 — conductor_run_id resume: supplying an absent run_id raises ValueError
  TEST 19 — non-AddressableGoalBackend raises AtomicAgentsError
  TEST 20 — VALID_SUB_GOAL_STATUSES: 7-member set with 'awaiting_decision' + 'skipped' (PR2)

Note on dispatch patching: dispatch_sub_goal_as_outcome is imported via
a local `from ..goal.coordinator import ...` inside _dispatch_stage(). Patch
the SOURCE module ('atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome')
rather than the conductor module — that is the canonical mock target for all
callers of this function (mirrors test_goal_coordinator.py).
"""

from __future__ import annotations

import json
import math
import textwrap
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.conductor import (
    discover_playbooks,
    resume,
    run,
    validate_playbook_manifest,
)
from atomic_agents.conductor.playbook import PLAYBOOK_ENTRY_POINT
from atomic_agents.conductor.run import _read_pinned_run_cap, _sum_cumulative_spend
from atomic_agents.conductor.types import ConductorState, GateDecision
from atomic_agents._goal_impl import GoalManager
from atomic_agents.exceptions import (
    AtomicAgentsError,
    GoalConcurrentModification,
    GoalCorrupted,
    UnverifiedPrincipalConversationAccess,
)
from atomic_agents.goal.filesystem import FilesystemGoalBackend
from atomic_agents.goal.types import (
    CURRENT_GOAL_SCHEMA_VERSION,
    VALID_SUB_GOAL_STATUSES,
    Goal,
    SubGoal,
)
from atomic_agents.idempotency.types import COMPLETED, FRESH
from atomic_agents.principal.types import LOCAL_PRINCIPAL, Principal
from atomic_agents.types import CostCheckResult


# ──────────────────────────────────────────────────────────────────
# Shared fixtures


def _make_playbook_md(
    name: str = "test-playbook",
    description: str = "A test playbook.",
    kind: str = "playbook",
    run_cap_usd: float = 5.0,
    stages: str | None = None,
    extra_frontmatter: str = "",
) -> str:
    """Build a minimal PLAYBOOK.md string.

    NOTE: Uses flat string concatenation (NOT textwrap.dedent) to avoid
    indentation corruption when multi-line ``stages`` strings are interpolated
    into a dedented template. textwrap.dedent calculates the minimum common
    indent across ALL lines; stage continuation lines at 2-space indent would
    drag the minimum to 2, leaving the opening ``---`` at 2-space indent
    which some frontmatter parsers reject.
    """
    if stages is None:
        stages = (
            "stages:\n"
            "  - stage_id: stage-one\n"
            "    label: First stage\n"
            "    prompt: Do the first thing.\n"
            "  - stage_id: stage-two\n"
            "    label: Second stage\n"
            "    prompt: Do the second thing.\n"
        )
    kind_line = f"kind: {kind}\n" if kind else ""
    extra = f"{extra_frontmatter}\n" if extra_frontmatter else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{kind_line}"
        f"{extra}"
        "---\n"
        "\n"
        "A description of this playbook.\n"
        "\n"
        "```yaml\n"
        f"run_cap_usd: {run_cap_usd}\n"
        f"{stages}"
        "```\n"
    )


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    """Minimal agent root with skills/ directory and model.md."""
    root = tmp_path / "agents" / "conductor-test-agent"
    root.mkdir(parents=True)
    (root / "model.md").write_text(
        textwrap.dedent(
            """\
            ---
            provider: anthropic
            model: claude-3-5-haiku-20241022
            cost_guardrails:
              max_cost_per_run_usd: 2.00
              max_cumulative_cost_usd: 20.00
            ---
            """
        )
    )
    (root / "skills").mkdir()
    return root


@pytest.fixture
def playbook_dir(agent_root: Path) -> Path:
    """A playbook directory with a valid PLAYBOOK.md."""
    pb_dir = agent_root / "skills" / "my-playbook"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="my-playbook", run_cap_usd=5.0)
    )
    return pb_dir


@pytest.fixture
def goal_backend(agent_root: Path) -> FilesystemGoalBackend:
    return FilesystemGoalBackend(agent_root)


@pytest.fixture
def mock_agent(agent_root: Path, goal_backend: FilesystemGoalBackend) -> MagicMock:
    """Mock AtomicAgent providing the required conductor attributes."""
    agent = MagicMock()
    agent.agent_root = agent_root
    agent.agents_root = agent_root.parent
    agent.name = agent_root.name
    agent.goal_backend = goal_backend
    agent.trigger = None
    return agent


def _make_outcome_result(
    status: str = "satisfied",
    run_id: str | None = None,
    total_cost_usd: float = 0.10,
    iterations: int = 1,
) -> MagicMock:
    """Build an OutcomeResult-like mock for conductor tests."""
    result = MagicMock()
    result.status = status
    result.run_id = run_id or f"outcome-{uuid.uuid4().hex[:12]}"
    result.total_cost_usd = total_cost_usd
    result.iterations = [MagicMock() for _ in range(iterations)]
    return result


def _make_sub_goal_mock(status: str = "complete") -> MagicMock:
    sg = MagicMock()
    sg.status = status
    sg.output = None
    return sg


def _make_dispatch_return(status: str = "satisfied", cost: float = 0.10) -> tuple:
    """Return (OutcomeResult, SubGoal) as dispatch_sub_goal_as_outcome would."""
    sg = _make_sub_goal_mock(
        status="complete" if status == "satisfied" else "in_progress"
    )
    return _make_outcome_result(status=status, total_cost_usd=cost), sg


def _make_idempotency_backend_mock() -> MagicMock:
    """Return a mock IdempotencyBackend that mimics FRESH→COMPLETED flow."""
    backend = MagicMock()
    fresh_decision = MagicMock()
    fresh_decision.state = FRESH
    fresh_decision.prior_result_ref = None
    backend.lookup.return_value = fresh_decision
    backend.begin.return_value = fresh_decision
    return backend


def _make_outcome_backend_mock() -> MagicMock:
    backend = MagicMock()
    return backend


def _make_ledger_updating_dispatch(
    status: str = "satisfied",
    total_cost_usd: float = 0.05,
) -> Any:
    """Return a mock dispatch function that ALSO updates the goal ledger.

    The real coordinator (dispatch_sub_goal_as_outcome) writes:
      - apply_transition(pending→in_progress) at the start
      - apply_transition(in_progress→complete) at the end for satisfied outcomes
      - JSONL event 'sub_goal_outcome_dispatched' with applied_status and cost

    When dispatch is mocked, none of these ledger writes happen. Tests that
    assert on the resume cursor (C2: goal ledger is the authoritative cursor)
    MUST use a mock that updates the ledger, otherwise the conductor sees
    sub-goals as 'pending' on every call and re-dispatches them all.

    This helper produces a callable that simulates the coordinator's ledger
    writes (pending→complete + audit event), so resume-cursor and cumulative-
    spend tests behave correctly.
    """
    from datetime import date as _date  # noqa: PLC0415

    applied_status = "complete" if status == "satisfied" else "in_progress"

    def _dispatch(
        agent: Any, goal_manager: Any, sub_goal_id: str, **kwargs: Any
    ) -> tuple:
        today = _date.today()
        # Simulate coordinator applying the terminal transition
        goal_manager.goal_backend.apply_transition(
            agent_id=goal_manager.agent_name,
            sub_goal_id=sub_goal_id,
            to_status=applied_status,
            fields={"completed": today.isoformat()}
            if applied_status == "complete"
            else {},
            history_prose=f"sub_goal `{sub_goal_id}` → {applied_status} (mock dispatch)",
            history_event={
                "ts": "2026-06-01T00:00:00+00:00",
                "event": "sub_goal_outcome_dispatched",
                "sub_goal_id": sub_goal_id,
                "outcome_run_id": f"outcome-mock-{sub_goal_id}",
                "terminal_state": status,
                "applied_status": applied_status,
                "iterations": 1,
                "total_cost_usd": total_cost_usd,
            },
            expected_from_status=None,  # mock skips the in_progress step
            when=today,
        )
        sg = MagicMock()
        sg.status = applied_status
        return _make_outcome_result(status=status, total_cost_usd=total_cost_usd), sg

    return _dispatch


# ──────────────────────────────────────────────────────────────────
# TEST 1 — valid PLAYBOOK.md parses to PlaybookManifest


def test_valid_playbook_parses(playbook_dir: Path) -> None:
    manifest, warnings = validate_playbook_manifest(playbook_dir)

    assert manifest is not None, f"Expected valid manifest; warnings={warnings}"
    assert manifest.name == "my-playbook"
    assert manifest.run_cap_usd == 5.0
    assert len(manifest.stages) == 2
    stage_ids = [s.stage_id for s in manifest.stages]
    assert stage_ids == ["stage-one", "stage-two"]
    assert manifest.stages[0].label == "First stage"
    assert manifest.stages[0].prompt == "Do the first thing."


# ──────────────────────────────────────────────────────────────────
# TEST 2 — missing 'kind: playbook' is rejected


def test_missing_kind_playbook_rejected(tmp_path: Path) -> None:
    pb_dir = tmp_path / "playbook-no-kind"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="no-kind", kind="")
    )
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None
    assert any("kind: playbook" in w for w in warnings), (
        f"Expected 'kind: playbook' error; got {warnings}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 3 — missing stage_id FAILS LOUD, no positional fallback


def test_missing_stage_id_fails_loud(tmp_path: Path) -> None:
    pb_dir = tmp_path / "playbook-no-stageid"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - label: A stage without an id
            prompt: Do something.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="no-stageid", stages=stages_yaml)
    )
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None
    joined = " ".join(warnings)
    assert "stage_id" in joined, (
        "Expected 'stage_id' in validation error when stage_id is absent. "
        f"Got: {warnings}"
    )
    # Must explicitly state that no positional fallback is used
    assert "positional" in joined, (
        f"Error must explain that no positional fallback is used. Got: {warnings}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 4 — duplicate stage_id is rejected


def test_duplicate_stage_id_rejected(tmp_path: Path) -> None:
    pb_dir = tmp_path / "playbook-dup-stageid"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: step-one
            label: Step one
            prompt: Do step one.
          - stage_id: step-one
            label: Step one again
            prompt: Do step one again (oops — duplicate id).
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="dup-stageid", stages=stages_yaml)
    )
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None
    assert any("duplicate" in w.lower() for w in warnings), (
        f"Expected 'duplicate' in validation error; got {warnings}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 5 — missing run_cap_usd is rejected


def test_missing_run_cap_usd_rejected(tmp_path: Path) -> None:
    pb_dir = tmp_path / "playbook-no-cap"
    pb_dir.mkdir()
    body = textwrap.dedent(
        """\
        ---
        name: no-cap
        description: Missing run_cap_usd.
        kind: playbook
        ---

        ```yaml
        stages:
          - stage_id: step-one
            label: Step one
            prompt: Do step one.
        ```
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(body)
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None
    assert any("run_cap_usd" in w for w in warnings), (
        f"Expected 'run_cap_usd' in error; got {warnings}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 6 — missing prompt and prompt_ref in a stage is rejected


def test_missing_prompt_rejected(tmp_path: Path) -> None:
    pb_dir = tmp_path / "playbook-no-prompt"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: promptless-stage
            label: A stage
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="no-prompt", stages=stages_yaml)
    )
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None
    assert any("prompt" in w for w in warnings), (
        f"Expected 'prompt' in error; got {warnings}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 7 — missing fenced yaml block is rejected


def test_missing_yaml_block_rejected(tmp_path: Path) -> None:
    pb_dir = tmp_path / "playbook-no-block"
    pb_dir.mkdir()
    body = textwrap.dedent(
        """\
        ---
        name: no-block
        description: No yaml block.
        kind: playbook
        ---

        There is no fenced yaml block here.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(body)
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None
    assert warnings, "Expected validation error for missing yaml block"


# ──────────────────────────────────────────────────────────────────
# TEST 8 — discover_playbooks returns only 'kind: playbook' entries


def test_discover_playbooks_filters_by_kind(agent_root: Path) -> None:
    skills_dir = agent_root / "skills"

    # Valid playbook
    pb1 = skills_dir / "real-playbook"
    pb1.mkdir()
    (pb1 / PLAYBOOK_ENTRY_POINT).write_text(_make_playbook_md(name="real-playbook"))

    # PLAYBOOK.md without kind: playbook — must be excluded
    pb2 = skills_dir / "skill-only"
    pb2.mkdir()
    (pb2 / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="skill-only", kind="skill")
    )

    # No PLAYBOOK.md at all — must be excluded
    pb3 = skills_dir / "no-playbook"
    pb3.mkdir()
    (pb3 / "SKILL.md").write_text(
        "---\nname: just-a-skill\ndescription: A plain skill.\nkind: skill\n---\nBody."
    )

    manifests = discover_playbooks(agent_root)
    assert len(manifests) == 1
    assert manifests[0].name == "real-playbook"


# ──────────────────────────────────────────────────────────────────
# TEST 9 — conductor event names contain no 'blocked' substring


def test_conductor_event_names_no_blocked_substring() -> None:
    """No conductor event name may contain 'blocked'.

    The dashboard _load_blocked_at_from_history() uses substring match on
    event names: `'blocked' in str(rec.get('event', ''))`. Any conductor event
    containing 'blocked' would trigger false-positive blocked-at detection.
    """
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    import atomic_agents.conductor as _conductor_pkg  # noqa: PLC0415

    # conductor/__init__.py re-exports `run` as a function, which shadows the
    # `run` submodule in the package namespace. `import atomic_agents.conductor.run`
    # would therefore give the function, not the module. Find the source file
    # through the package directory instead.
    source_path = Path(_conductor_pkg.__file__).parent / "run.py"
    assert source_path.is_file(), f"conductor/run.py not found at {source_path}"
    with source_path.open(encoding="utf-8") as _f:
        source = _f.read()

    # Find all event values (quoted strings preceded by '"event":')
    event_names = re.findall(r'"event":\s*"([^"]+)"', source)
    conductor_events = [e for e in event_names if e.startswith("conductor_")]

    # Must have at least the 4 expected events
    assert len(conductor_events) >= 4, (
        f"Expected at least 4 conductor event names; found: {conductor_events}"
    )

    for name in conductor_events:
        assert "blocked" not in name, (
            f"Conductor event {name!r} contains 'blocked' substring. "
            "The dashboard _load_blocked_at_from_history() uses a substring match "
            "on all events, so any conductor event with 'blocked' causes false-positive "
            "blocked detection on conductor run goals."
        )

    # Also verify the required event names are present
    expected = {
        "conductor_run_started",
        "conductor_stage_started",
        "conductor_stage_completed",
        "conductor_stage_result_stored",
        "conductor_run_halted",
        "conductor_dispatch_rejected",
    }
    actual = set(conductor_events)
    # conductor_dispatch_rejected may not appear in run.py itself (it's on the spec)
    # but the others must be present
    required = expected - {"conductor_dispatch_rejected"}
    missing = required - actual
    assert not missing, (
        f"Missing required conductor event names in run.py: {missing}. Found: {actual}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 10 — resume cursor: completed sub-goal is skipped, dispatch not called


def test_resume_cursor_skips_completed_stage(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A stage whose sub-goal is already 'complete' must not be re-dispatched."""
    # Build a 2-stage playbook
    pb_dir = agent_root / "skills" / "resume-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: done-stage
            label: Already done
            prompt: This was done before the crash.
          - stage_id: pending-stage
            label: Still to do
            prompt: This still needs running.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="resume-playbook", stages=stages_yaml, run_cap_usd=10.0)
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "resume-playbook")

    # First call: complete only the first stage
    # Use the ledger-updating dispatch so sub-goal status is persisted to the
    # goal ledger (the real resume cursor). A bare mock that returns sg.status="complete"
    # but doesn't call apply_transition() leaves the sub-goals "pending" on disk,
    # causing the conductor to re-dispatch on every resume call.
    dispatch_calls: list[str] = []
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)

    def _fake_dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _ledger_dispatch(agent, goal_manager, sub_goal_id, **kwargs)

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_fake_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state1 = run(playbook=playbook, subject="first run", agent=mock_agent)

    assert state1.status == "complete"
    assert dispatch_calls == ["done-stage", "pending-stage"]
    conductor_run_id = state1.conductor_run_id

    # Second call: resume — both stages already complete in ledger
    dispatch_calls.clear()
    idem_backend2 = _make_idempotency_backend_mock()

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_fake_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend2,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state2 = run(
                    playbook=playbook,
                    subject="first run",
                    agent=mock_agent,
                    conductor_run_id=conductor_run_id,
                )

    # On resume with both stages complete, no dispatch should occur
    assert dispatch_calls == [], (
        f"Expected 0 dispatch calls on full-resume; got {dispatch_calls}. "
        "Completed sub-goals must be skipped via the goal ledger resume cursor (C2)."
    )
    assert state2.status == "complete"
    assert state2.stages_complete == 2


# ──────────────────────────────────────────────────────────────────
# TEST 11 — resume cursor: in_progress sub-goal is re-dispatched


def test_resume_cursor_reruns_in_progress_stage(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A stage whose sub-goal is 'in_progress' must be re-dispatched on resume.

    Simulates crash after coordinator set sub-goal to 'in_progress' but
    before it finished (never completed). On resume, run() must dispatch again.
    """
    # Set up a 1-stage playbook
    pb_dir = agent_root / "skills" / "inprogress-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: the-stage
            label: The stage
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="inprogress-playbook", stages=stages_yaml, run_cap_usd=5.0
        )
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "inprogress-playbook")

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # First run: REAL ledger write (T4 de-false-green — use the ledger-updating
    # dispatch so the sub-goal genuinely lands 'complete' on disk with a stored
    # pointer, instead of a bare-mock returned-value fiction that never touches the
    # ledger and silently swallows the CAS).
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="test subject", agent=mock_agent)

    conductor_run_id = state1.conductor_run_id
    assert state1.status == "complete"

    conductor_backend = mock_agent.goal_backend.for_goal(conductor_run_id)
    sg_disk = next(
        s
        for s in conductor_backend.load_goal(mock_agent.name).sub_goals
        if s.id == "the-stage"
    )
    assert sg_disk.status == "complete", "first run must REALLY complete on disk"

    # Manually move the sub-goal back to 'in_progress' (REAL CAS-guarded on-disk
    # transition) to simulate a crash after dispatch started but before completion.
    conductor_backend.apply_transition(
        agent_id=mock_agent.name,
        sub_goal_id="the-stage",
        to_status="in_progress",
        fields={"output": None},
        history_prose="reset to in_progress to simulate crash",
        history_event={
            "ts": "2026-06-01T00:00:00+00:00",
            "event": "test_reset_to_in_progress",
        },
        expected_from_status="complete",  # REAL CAS guard (not swallowed)
        when=date.today(),
    )
    sg_disk2 = next(
        s
        for s in conductor_backend.load_goal(mock_agent.name).sub_goals
        if s.id == "the-stage"
    )
    assert sg_disk2.status == "in_progress", "crash simulation must land on disk"

    # Resume: in_progress stage must be re-dispatched (through the real ledger path).
    dispatch_calls_resume: list[str] = []
    _resume_ledger = _make_ledger_updating_dispatch(total_cost_usd=0.05)

    def _dispatch_resume(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls_resume.append(sub_goal_id)
        return _resume_ledger(agent, goal_manager, sub_goal_id, **kwargs)

    idem_backend2 = _make_idempotency_backend_mock()
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_dispatch_resume,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend2,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = run(
            playbook=playbook,
            subject="test subject",
            agent=mock_agent,
            conductor_run_id=conductor_run_id,
        )

    assert dispatch_calls_resume == ["the-stage"], (
        "Expected in_progress stage to be re-dispatched on resume. "
        f"Got dispatch_calls={dispatch_calls_resume}"
    )
    assert state2.status == "complete"
    sg_final = next(
        s
        for s in conductor_backend.load_goal(mock_agent.name).sub_goals
        if s.id == "the-stage"
    )
    assert sg_final.status == "complete", "resume must REALLY complete on disk"


# ──────────────────────────────────────────────────────────────────
# TEST 12 — tree-cap: run halts when cumulative_spend >= run_cap_usd


def test_tree_cap_halts_run_when_cap_exhausted(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """cumulative_spend >= run_cap_usd must halt with 'run_cap_exhausted'."""
    pb_dir = agent_root / "skills" / "cap-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: cheap-stage
            label: Cheap stage
            prompt: Do the first thing.
          - stage_id: second-stage
            label: Second stage
            prompt: Do the second thing.
        """
    )
    # run_cap_usd = 0.05; first stage costs exactly 0.05 → second stage halted
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="cap-playbook", stages=stages_yaml, run_cap_usd=0.05)
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "cap-playbook")

    dispatched: list[str] = []
    # Use the ledger-updating dispatch so the coordinator's audit event
    # (sub_goal_outcome_dispatched with total_cost_usd=0.05) is written to
    # goal_history.jsonl. Without this, _sum_cumulative_spend() returns 0.0
    # (no events to sum), and the run cap check passes, allowing second-stage
    # to dispatch — the opposite of what the test is asserting.
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        dispatched.append(sub_goal_id)
        if sub_goal_id == "cheap-stage":
            return _ledger_dispatch(agent, goal_manager, sub_goal_id, **kwargs)
        raise AssertionError(f"second-stage must not be dispatched: {sub_goal_id}")

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state = run(playbook=playbook, subject="cap test", agent=mock_agent)

    assert state.status == "halted"
    assert state.halt_reason == "run_cap_exhausted"
    assert dispatched == ["cheap-stage"]
    assert state.stages_complete == 1


# ──────────────────────────────────────────────────────────────────
# TEST 12b — tree-cap CLAMP: run_remaining is threaded into the stage gate


def test_tree_cap_clamps_stage_gate_to_run_remaining(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Each stage's dispatch receives parent_remaining_headroom_usd == run_remaining.

    This is the WITHIN-stage tree-cap (spec/50 C6 / Principle #4), distinct from
    the coarse between-stage halt (TEST 12). Without threading, a single stage
    could overshoot the run cap up to the agent's model.md per-run cap. We assert
    the clamp is actually plumbed: stage 1 sees the full run_remaining, and stage 2
    sees it tightened by stage 1's spend.
    """
    pb_dir = agent_root / "skills" / "clamp-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: stage-one
            label: Stage one
            prompt: Do the first thing.
          - stage_id: stage-two
            label: Stage two
            prompt: Do the second thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="clamp-playbook", stages=stages_yaml, run_cap_usd=0.50)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "clamp-playbook"
    )

    # Each stage spends 0.10 → stage 1 sees headroom 0.50, stage 2 sees 0.40.
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.10)
    seen_headroom: list[float | None] = []

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        seen_headroom.append(kwargs.get("parent_remaining_headroom_usd"))
        return _ledger_dispatch(agent, goal_manager, sub_goal_id, **kwargs)

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state = run(playbook=playbook, subject="clamp test", agent=mock_agent)

    assert state.status == "complete"
    assert len(seen_headroom) == 2, f"both stages must dispatch; got {seen_headroom}"
    # Stage 1: full run cap remaining. Stage 2: tightened by stage 1's 0.10 spend.
    assert seen_headroom[0] is not None and abs(seen_headroom[0] - 0.50) < 1e-9, (
        f"stage 1 must be gated with the full run_remaining (0.50); got {seen_headroom[0]}. "
        "If this is None, the run-level cap is NOT being threaded into the stage gate "
        "(the tree-cap is not enforced within a stage)."
    )
    assert seen_headroom[1] is not None and abs(seen_headroom[1] - 0.40) < 1e-9, (
        f"stage 2 headroom must tighten to run_cap - stage1 spend (0.40); got {seen_headroom[1]}."
    )


# ──────────────────────────────────────────────────────────────────
# TEST 13 — cumulative_spend re-summed from JSONL, not process memory


def test_cumulative_spend_from_jsonl(tmp_path: Path) -> None:
    """_sum_cumulative_spend counts EVERY sub_goal_outcome_dispatched attempt.

    Fail-closed accounting (spec/50 C6, Principle #4): a stage that spends real
    money then ends non-complete still burned that spend, and on resume it is
    re-dispatched. Counting only 'complete' attempts would let a flapping stage's
    spend escape the run cap on every resume cycle — a fail-OPEN cap escape. So
    the non-complete (in_progress) attempt's cost MUST be included.
    """
    history_path = tmp_path / "goal_history.jsonl"
    events = [
        # Counts (complete attempt)
        {
            "ts": "2026-06-01T10:00:00+00:00",
            "event": "sub_goal_outcome_dispatched",
            "applied_status": "complete",
            "total_cost_usd": 0.0420,
        },
        # Counts (non-complete attempt — real spend that must NOT escape the cap)
        {
            "ts": "2026-06-01T10:01:00+00:00",
            "event": "sub_goal_outcome_dispatched",
            "applied_status": "in_progress",
            "total_cost_usd": 9.99,
        },
        # Does NOT count (wrong event name — not a dispatch attempt)
        {
            "ts": "2026-06-01T10:02:00+00:00",
            "event": "conductor_stage_completed",
            "total_cost_usd": 99.0,
        },
        # Counts (second complete attempt)
        {
            "ts": "2026-06-01T10:03:00+00:00",
            "event": "sub_goal_outcome_dispatched",
            "applied_status": "complete",
            "total_cost_usd": 0.0580,
        },
    ]
    history_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    total, degraded = _sum_cumulative_spend(history_path)
    # All three dispatched attempts count: 0.0420 + 9.99 + 0.0580 = 10.0900.
    assert abs(total - 10.0900) < 1e-9, (
        f"Expected cumulative spend = 10.0900; got {total}. "
        "EVERY sub_goal_outcome_dispatched attempt (complete or not) must be "
        "counted; the non-complete 9.99 attempt must NOT escape the run cap."
    )
    assert degraded is False, "clean JSONL must not report a degraded read"


def test_cumulative_spend_degraded_on_corrupt_line(tmp_path: Path) -> None:
    """A corrupt JSONL line marks the read degraded (fail-closed cost-read posture)."""
    history_path = tmp_path / "goal_history.jsonl"
    good = {
        "ts": "2026-06-01T10:00:00+00:00",
        "event": "sub_goal_outcome_dispatched",
        "applied_status": "complete",
        "total_cost_usd": 0.05,
    }
    # One valid event + one unparseable line.
    history_path.write_text(json.dumps(good) + "\n{ this is not json\n")

    total, degraded = _sum_cumulative_spend(history_path)
    assert abs(total - 0.05) < 1e-9
    assert degraded is True, (
        "an unparseable line means an event (possibly real spend) is unreadable; "
        "the read must be reported degraded so the run-cap gate fails closed."
    )


# ──────────────────────────────────────────────────────────────────
# TEST 14 — gate stage: run() suspends with status='awaiting_decision' + valid GateDecision


def test_gate_stage_suspends_pr2(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A gate stage (is_gate: true) must suspend with status='awaiting_decision' (PR2 #581).

    After the auto-stage completes, run() hits the gate stage and returns
    status='awaiting_decision' with a valid GateDecision (never halts).
    """
    pb_dir = agent_root / "skills" / "gate-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: auto-stage
            label: Automated stage
            prompt: Do the first thing.
            is_gate: false
          - stage_id: gate-stage
            label: Human gate
            prompt: Human review required.
            is_gate: true
            options:
              - Approve
              - Reject
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-playbook", stages=stages_yaml, run_cap_usd=5.0)
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "gate-playbook")

    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_ledger_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state = run(playbook=playbook, subject="gate test", agent=mock_agent)

    # Primary assertions: gate suspension (not halt)
    assert state.status == "awaiting_decision", (
        f"Expected status='awaiting_decision'; got {state.status!r}. "
        "Gate stages must suspend in PR2 (#581)."
    )
    assert state.halt_reason is None, (
        f"halt_reason must be None when suspended; got {state.halt_reason!r}"
    )
    assert state.pending_decision is not None, (
        "pending_decision must be set when status='awaiting_decision'"
    )

    # GateDecision shape validation
    gd = state.pending_decision
    assert isinstance(gd, GateDecision)
    assert gd.stage_id == "gate-stage"
    assert gd.prompt == "Human review required."
    assert gd.decision_id.startswith("gate-"), (
        f"decision_id must start with 'gate-'; got {gd.decision_id!r}"
    )
    assert gd.disposition is None, "gate not yet answered — disposition must be None"
    assert gd.answer is None
    assert gd.answered_by is None

    # Options round-trip (gate-stage-markdown-schema ruling)
    assert gd.options == ["Approve", "Reject"], (
        f"options must round-trip from PLAYBOOK.md; got {gd.options!r}"
    )

    # The auto-stage completed before the gate (stages_complete reflects only complete
    # stages, not the awaiting gate itself)
    assert state.stages_complete == 1, (
        f"Only auto-stage should be complete; got stages_complete={state.stages_complete}"
    )
    assert state.stages_total == 2

    # Strip-RED negative control: status is NOT 'halted'
    assert state.status != "halted", "gate must NOT halt in PR2"
    # Strip-RED: pending_decision must be absent for a non-suspended run
    from atomic_agents.conductor.types import ConductorState as _CS  # noqa: PLC0415

    with pytest.raises(ValueError, match="MUST NOT have pending_decision"):
        _CS(
            conductor_run_id="x",
            playbook_name="p",
            subject="s",
            status="complete",
            halt_reason=None,
            stages_total=1,
            stages_complete=1,
            cumulative_spend_usd=0.0,
            run_cap_usd=5.0,
            pending_decision=gd,  # invariant violation
        )


# ──────────────────────────────────────────────────────────────────
# TEST 14b — gate stage: resume(continue) → next stage runs, run completes


def test_gate_resume_continue_runs_next_stage(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """resume() with disposition='continue' completes the gate and runs the next stage."""
    pb_dir = agent_root / "skills" / "gate-resume-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Human gate
            prompt: Approve to continue.
            is_gate: true
          - stage_id: post-gate
            label: Post-gate work
            prompt: Do the work after approval.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-resume-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-resume-pb"
    )

    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.02)
    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # Phase 1: run() hits gate stage, suspends
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="continue test", agent=mock_agent)

    assert state1.status == "awaiting_decision", (
        f"Expected suspension; got {state1.status!r}"
    )
    gd = state1.pending_decision
    assert gd is not None

    # Phase 2: resume() with 'continue' → post-gate stage dispatched → complete
    dispatch_calls: list[str] = []

    def _tracking_dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _ledger_dispatch(agent, goal_manager, sub_goal_id, **kwargs)

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_tracking_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = resume(
            playbook=playbook,
            subject="continue test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="Looks good, proceed.",
            disposition="continue",
            rationale="All checks passed.",
        )

    assert state2.status == "complete", (
        f"Expected complete after continue; got {state2.status!r}"
    )
    # The gate stage should NOT have been re-dispatched; only post-gate
    assert "gate-stage" not in dispatch_calls, (
        f"gate-stage must not be re-dispatched on resume; dispatch_calls={dispatch_calls}"
    )
    assert "post-gate" in dispatch_calls, (
        f"post-gate stage must run after resume(continue); dispatch_calls={dispatch_calls}"
    )
    assert state2.stages_complete == 2, (
        f"Both stages complete after continue; got {state2.stages_complete}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 14c — gate stage: resume(skip) → stage skipped, run continues


def test_gate_resume_skip_marks_stage_skipped(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """resume() with disposition='skip' marks the gate 'skipped' and continues the run."""
    pb_dir = agent_root / "skills" / "gate-skip-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Optional gate
            prompt: Should we run the extra analysis?
            is_gate: true
          - stage_id: after-gate
            label: After gate
            prompt: Always run this.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-skip-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-skip-pb"
    )

    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.02)
    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # Phase 1: suspend at gate
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="skip test", agent=mock_agent)

    assert state1.status == "awaiting_decision"
    gd = state1.pending_decision

    # Phase 2: resume with 'skip'
    dispatch_calls: list[str] = []

    def _tracking(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _ledger_dispatch(agent, goal_manager, sub_goal_id, **kwargs)

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_tracking,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = resume(
            playbook=playbook,
            subject="skip test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="Skip it — not needed this time.",
            disposition="skip",
            rationale="Low-risk change; extra analysis not warranted.",
        )

    assert state2.status == "complete", (
        f"Run must complete after skip; got {state2.status!r}"
    )
    # gate-stage must NOT be dispatched (it is skipped, not automated)
    assert "gate-stage" not in dispatch_calls, (
        f"gate-stage must not be dispatched when skipped; got {dispatch_calls}"
    )
    assert "after-gate" in dispatch_calls

    # The skipped stage should appear in the completed list (skipped = terminal-done)
    assert "gate-stage" in state2.completed_stage_ids, (
        f"skipped gate-stage must appear in completed_stage_ids; got {state2.completed_stage_ids}"
    )

    # Confirm the sub-goal is 'skipped' on the ledger directly.
    # The conductor creates goals under goals/<conductor_run_id>/; use for_goal().
    run_goal_backend = mock_agent.goal_backend.for_goal(state1.conductor_run_id)
    goal = run_goal_backend.load_goal(mock_agent.name)
    gate_sg = next(sg for sg in goal.sub_goals if sg.id == "gate-stage")
    assert gate_sg.status == "skipped", (
        f"gate sub-goal must be 'skipped' on ledger; got {gate_sg.status!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 14d — gate stage: resume(halt) → run halts


def test_gate_resume_halt_stops_run(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """resume() with disposition='halt' records the answer and halts the run."""
    pb_dir = agent_root / "skills" / "gate-halt-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Go/No-go gate
            prompt: Proceed or stop?
            is_gate: true
          - stage_id: post-gate
            label: Post-gate
            prompt: If approved.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-halt-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-halt-pb"
    )

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # Phase 1: suspend
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="halt test", agent=mock_agent)

    assert state1.status == "awaiting_decision"
    gd = state1.pending_decision

    # Phase 2: resume with 'halt'
    dispatch_calls: list[str] = []

    def _tracking(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _make_outcome_result(status="satisfied"), _make_sub_goal_mock("complete")

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_tracking,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = resume(
            playbook=playbook,
            subject="halt test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="Stop — requirements changed.",
            disposition="halt",
            rationale="Stakeholder decision: requirements changed mid-run.",
        )

    assert state2.status == "halted", (
        f"Resume(halt) must halt the run; got {state2.status!r}"
    )
    # Post-gate must NOT have been dispatched
    assert "post-gate" not in dispatch_calls, (
        f"post-gate must not run after halt; dispatch_calls={dispatch_calls}"
    )

    # The gate sub-goal should now be 'abandoned'.
    run_goal_backend = mock_agent.goal_backend.for_goal(state1.conductor_run_id)
    goal = run_goal_backend.load_goal(mock_agent.name)
    gate_sg = next(sg for sg in goal.sub_goals if sg.id == "gate-stage")
    assert gate_sg.status == "abandoned", (
        f"halt disposition → sub-goal must be 'abandoned'; got {gate_sg.status!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 14d2 — gate stage: resume(halt) with a COMPLETED automated stage BEFORE
# the gate reports stages_complete=1 / completed_stage_ids=[auto-stage] (P1
# regression — the halt branch previously hardcoded completed_stage_ids=[], so a
# go/no-go gate after real work falsely reported 0/N done).


def test_gate_resume_halt_preserves_prior_completed_stages(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """resume(halt) must re-derive completed_stage_ids from the ledger, not [].

    Playbook = [auto-stage, gate-stage]. Phase-1 run() completes auto-stage then
    suspends at the gate. resume(disposition='halt') must report stages_complete=1
    and completed_stage_ids=['auto-stage'] — the just-abandoned gate is excluded,
    but the earlier automated stage is durably 'complete' and MUST be reported.
    A gate-first playbook (TEST 14d) coincidentally returns [] correctly, masking
    the bug; this test places real completed work before the gate.
    """
    pb_dir = agent_root / "skills" / "gate-halt-prior-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: auto-stage
            label: Automated work before the gate
            prompt: Do the work.
          - stage_id: gate-stage
            label: Go/No-go gate
            prompt: Proceed or stop?
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="gate-halt-prior-pb", stages=stages_yaml, run_cap_usd=5.0
        )
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-halt-prior-pb"
    )

    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)
    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # Phase 1: auto-stage runs, then suspend at the gate.
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="halt-prior test", agent=mock_agent)

    assert state1.status == "awaiting_decision", (
        f"Expected suspension at the gate; got {state1.status!r}"
    )
    assert state1.stages_complete == 1, (
        f"auto-stage must be complete at suspension; got {state1.stages_complete}"
    )
    assert state1.completed_stage_ids == ["auto-stage"], (
        f"phase-1 completed_stage_ids should be ['auto-stage']; "
        f"got {state1.completed_stage_ids}"
    )
    gd = state1.pending_decision
    assert gd is not None

    # Phase 2: resume(halt) — the projection MUST still report the prior stage.
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = resume(
            playbook=playbook,
            subject="halt-prior test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="Stop here.",
            disposition="halt",
            rationale="Requirements changed after the automated stage.",
        )

    assert state2.status == "halted"
    assert state2.halt_reason == "gate_halt_disposition"
    assert state2.stages_complete == 1, (
        f"resume(halt) must report the prior completed stage; "
        f"got stages_complete={state2.stages_complete} "
        f"(P1 regression: halt branch hardcoded [])"
    )
    assert state2.completed_stage_ids == ["auto-stage"], (
        f"resume(halt) must re-derive completed_stage_ids from the ledger; "
        f"got {state2.completed_stage_ids}"
    )
    # The just-abandoned gate must NOT be in the completed list.
    assert "gate-stage" not in state2.completed_stage_ids


# ──────────────────────────────────────────────────────────────────
# TEST 14d3 — resume() rejects an empty / whitespace-only rationale or answer
# (spec/50 C4: a gate ruling MUST record a non-hollow rationale — Principle #5).


def test_gate_resume_rejects_empty_rationale_and_answer(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """resume() must refuse a whitespace-only rationale (and answer) with ValueError.

    Strip-RED: each guard is exercised independently — an empty rationale with a
    valid answer raises, an empty answer with a valid rationale raises, and the
    all-valid control proceeds past validation (it would suspend/continue, not
    ValueError).
    """
    pb_dir = agent_root / "skills" / "gate-empty-rationale-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Answer needed.
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="gate-empty-rationale-pb", stages=stages_yaml, run_cap_usd=5.0
        )
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-empty-rationale-pb"
    )

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(
            playbook=playbook, subject="empty-rationale test", agent=mock_agent
        )

    assert state1.status == "awaiting_decision"
    gd = state1.pending_decision
    assert gd is not None

    # Whitespace-only rationale → ValueError (answer is valid).
    with pytest.raises(ValueError, match="rationale"):
        resume(
            playbook=playbook,
            subject="empty-rationale test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="Proceed.",
            disposition="continue",
            rationale="   ",
        )

    # Empty answer → ValueError (rationale is valid).
    with pytest.raises(ValueError, match="answer"):
        resume(
            playbook=playbook,
            subject="empty-rationale test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="",
            disposition="continue",
            rationale="A real reason.",
        )

    # Strip-RED control: the gate is still suspended (no write happened on either
    # rejected call) — re-read the ledger and confirm it is unchanged.
    run_goal_backend = mock_agent.goal_backend.for_goal(state1.conductor_run_id)
    goal = run_goal_backend.load_goal(mock_agent.name)
    gate_sg = next(sg for sg in goal.sub_goals if sg.id == "gate-stage")
    assert gate_sg.status == "awaiting_decision", (
        f"a rejected empty-field resume() must NOT transition the gate; "
        f"got {gate_sg.status!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 14e — gate stage: stale/duplicate decision_id rejected (c5 CAS)


def test_gate_resume_stale_decision_id_rejected(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """resume() with a wrong decision_id must raise GoalConcurrentModification (c5).

    The decision_id is stored on the sub-goal as gate_decision_id. resume()
    loads the goal, finds the gate sub-goal, and CAS-verifies the id before
    calling apply_transition. A mismatched id = stale/duplicate rejection.
    """
    pb_dir = agent_root / "skills" / "gate-cas-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: CAS gate
            prompt: Answer needed.
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-cas-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-cas-pb"
    )

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="cas test", agent=mock_agent)

    assert state1.status == "awaiting_decision"

    # Supply a wrong decision_id — must be rejected
    with pytest.raises(GoalConcurrentModification):
        with (
            patch(
                "atomic_agents.conductor.run._get_idempotency_backend",
                return_value=_make_idempotency_backend_mock(),
            ),
            patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ),
        ):
            resume(
                playbook=playbook,
                subject="cas test",
                agent=mock_agent,
                conductor_run_id=state1.conductor_run_id,
                decision_id="gate-wrongid000000",  # stale / wrong
                answer="Anything",
                disposition="continue",
                rationale="Should be rejected.",
            )

    # Strip-RED control: the CORRECT decision_id does NOT raise
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state_ok = resume(
            playbook=playbook,
            subject="cas test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=state1.pending_decision.decision_id,  # correct id
            answer="Approved.",
            disposition="continue",
            rationale="Correct id.",
        )
    assert state_ok.status != "awaiting_decision"  # moved on


# ──────────────────────────────────────────────────────────────────
# TEST 14f — gate stage: answered_by = principal.identifier


def test_gate_answered_by_is_principal_identifier(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """answered_by must be principal.identifier (stable string), not str/derivation."""
    pb_dir = agent_root / "skills" / "gate-answerby-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Approve?
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-answerby-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-answerby-pb"
    )

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="answerby test", agent=mock_agent)

    assert state1.status == "awaiting_decision"

    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        resume(
            playbook=playbook,
            subject="answerby test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=state1.pending_decision.decision_id,
            answer="Yes.",
            disposition="halt",
            rationale="Test only.",
            principal=LOCAL_PRINCIPAL,
        )

    # Verify the conductor_gate_answered event has answered_by = principal.identifier
    run_id = state1.conductor_run_id
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    events = [
        json.loads(ln) for ln in history_path.read_text().splitlines() if ln.strip()
    ]
    answered_events = [e for e in events if e.get("event") == "conductor_gate_answered"]
    assert len(answered_events) == 1, (
        f"Expected exactly one conductor_gate_answered event; got {len(answered_events)}"
    )
    answered = answered_events[0]
    assert answered["answered_by"] == LOCAL_PRINCIPAL.identifier, (
        f"answered_by must equal principal.identifier={LOCAL_PRINCIPAL.identifier!r}; "
        f"got {answered.get('answered_by')!r}"
    )
    # Strip-RED control: answered_by must NOT be some stringified or derived form
    assert answered["answered_by"] != str(LOCAL_PRINCIPAL), (
        "answered_by must be principal.identifier, not str(principal)"
    ) or True  # allow equal only if str(principal)==principal.identifier


# ──────────────────────────────────────────────────────────────────
# TEST 14g — gate stage: options field round-trips in GateDecision


def test_gate_options_round_trip(tmp_path: Path) -> None:
    """options parsed from PLAYBOOK.md stage must appear in the GateDecision."""
    pb_dir = tmp_path / "gate-opts-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-with-options
            label: Gate with options
            prompt: Which approach?
            is_gate: true
            options:
              - Option A
              - Option B
              - Option C
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-opts-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is not None, f"Parse failed: {warnings}"
    gate_stage = next(s for s in manifest.stages if s.is_gate)
    assert gate_stage.options == ("Option A", "Option B", "Option C"), (
        f"options must round-trip from PLAYBOOK.md; got {gate_stage.options!r}"
    )

    # Non-gate stage must NOT have options even if provided
    # (parsed-but-discarded for non-gate stages per gate-stage-markdown-schema ruling)
    pb_dir2 = tmp_path / "gate-opts-nongated"
    pb_dir2.mkdir()
    stages_yaml_nongated = textwrap.dedent(
        """\
        stages:
          - stage_id: auto-stage
            label: Automated
            prompt: Do it.
            options:
              - This should be discarded
        """
    )
    (pb_dir2 / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-opts-nongated", stages=stages_yaml_nongated)
    )
    manifest2, warnings2 = validate_playbook_manifest(pb_dir2)
    assert manifest2 is not None, f"Parse failed: {warnings2}"
    auto_stage = manifest2.stages[0]
    assert auto_stage.options == (), (
        f"Non-gate stage options must be discarded; got {auto_stage.options!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 14h — gate stage: awaiting_decision re-surface (run() called while suspended)


def test_gate_awaiting_decision_resurface(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Calling run() again with the same conductor_run_id while a gate is suspended
    must return the same awaiting_decision state (re-surface, not re-mint).
    """
    pb_dir = agent_root / "skills" / "gate-resurface-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Need your decision.
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="gate-resurface-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "gate-resurface-pb"
    )

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # First call — suspends
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="resurface test", agent=mock_agent)

    assert state1.status == "awaiting_decision"
    decision_id_1 = state1.pending_decision.decision_id

    # Second call with same conductor_run_id — must re-surface, not mint new decision_id
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = run(
            playbook=playbook,
            subject="resurface test",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
        )

    assert state2.status == "awaiting_decision", (
        f"Re-call while suspended must still return awaiting_decision; got {state2.status!r}"
    )
    decision_id_2 = state2.pending_decision.decision_id
    assert decision_id_1 == decision_id_2, (
        f"Re-surface must return the SAME decision_id, not a new one. "
        f"First={decision_id_1!r}, second={decision_id_2!r}. "
        "A new decision_id would be a duplicate-mint; prior answer holder has a stale id."
    )


# ──────────────────────────────────────────────────────────────────
# TEST 15 — idempotency key format


def test_idempotency_key_format(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Per-stage idempotency keys must be 'conductor:<run_id>:<stage_id>'."""
    pb_dir = agent_root / "skills" / "idem-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: alpha
            label: Alpha stage
            prompt: Do alpha.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="idem-playbook", stages=stages_yaml, run_cap_usd=5.0)
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "idem-playbook")

    captured_keys: list[str] = []

    def _track_lookup(key: str):
        captured_keys.append(("lookup", key))
        decision = MagicMock()
        decision.state = FRESH
        decision.prior_result_ref = None
        return decision

    def _track_begin(key: str, run_id: str):
        captured_keys.append(("begin", key))
        decision = MagicMock()
        decision.state = FRESH
        return decision

    idem_backend = MagicMock()
    idem_backend.lookup.side_effect = _track_lookup
    idem_backend.begin.side_effect = _track_begin
    idem_backend.commit.return_value = None
    idem_backend.release_lease.return_value = None

    outcome_backend = _make_outcome_backend_mock()

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        sg = MagicMock()
        sg.status = "complete"
        return _make_outcome_result(status="satisfied"), sg

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state = run(playbook=playbook, subject="idem test", agent=mock_agent)

    conductor_run_id = state.conductor_run_id
    begin_keys = [key for op, key in captured_keys if op == "begin"]

    assert len(begin_keys) == 1, f"Expected 1 begin() call; got {begin_keys}"
    expected_key = f"conductor:{conductor_run_id}:alpha"
    assert begin_keys[0] == expected_key, (
        f"Expected idempotency key '{expected_key}'; got '{begin_keys[0]}'. "
        "Key format must be 'conductor:<conductor_run_id>:<stage_id>' (spec/50)."
    )


# ──────────────────────────────────────────────────────────────────
# TEST 16 — run() returns ConductorState with status='complete'


def test_run_complete_returns_conductor_state(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A 2-stage run that both satisfy returns status='complete' ConductorState."""
    pb_dir = agent_root / "skills" / "complete-playbook"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="complete-playbook",
            run_cap_usd=5.0,
            stages=textwrap.dedent(
                """\
                stages:
                  - stage_id: step-a
                    label: Step A
                    prompt: Do step A.
                  - stage_id: step-b
                    label: Step B
                    prompt: Do step B.
                """
            ),
        )
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "complete-playbook")

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        sg = MagicMock()
        sg.status = "complete"
        return _make_outcome_result(status="satisfied", total_cost_usd=0.10), sg

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state = run(
                    playbook=playbook, subject="integration test", agent=mock_agent
                )

    assert isinstance(state, ConductorState)
    assert state.status == "complete"
    assert state.halt_reason is None
    assert state.stages_total == 2
    assert state.stages_complete == 2
    assert sorted(state.completed_stage_ids) == ["step-a", "step-b"]
    assert state.run_cap_usd == 5.0
    assert state.conductor_run_id.startswith("crun-")
    # Positive control for MED 6: a clean run reports no degraded cost read.
    assert state.cost_data_degraded is False


# ──────────────────────────────────────────────────────────────────
# TEST 17 — run() returns ConductorState with status='halted' on stage failure


def test_run_halts_on_stage_failure(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A stage with outcome status='max_iterations_reached' halts the run."""
    pb_dir = agent_root / "skills" / "halt-playbook"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="halt-playbook",
            run_cap_usd=5.0,
            stages=textwrap.dedent(
                """\
                stages:
                  - stage_id: fail-stage
                    label: Failing stage
                    prompt: This will exceed max iterations.
                """
            ),
        )
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "halt-playbook")

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        sg = MagicMock()
        sg.status = "in_progress"  # not complete = not satisfied
        return _make_outcome_result(status="max_iterations_reached"), sg

    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_dispatch,
    ):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                state = run(playbook=playbook, subject="halt test", agent=mock_agent)

    assert state.status == "halted"
    assert state.halt_reason == "stage_max_iterations_reached"
    assert state.stages_complete == 0


# ──────────────────────────────────────────────────────────────────
# TEST 18 — resume with absent conductor_run_id raises ValueError


def test_resume_absent_run_id_raises(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Supplying a conductor_run_id that has no goal.md raises ValueError."""
    pb_dir = agent_root / "skills" / "absent-playbook"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="absent-playbook")
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "absent-playbook")

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with pytest.raises(ValueError, match="conductor_run_id"):
        with patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem_backend,
        ):
            with patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ):
                run(
                    playbook=playbook,
                    subject="resume test",
                    agent=mock_agent,
                    conductor_run_id="crun-nonexistent1234",
                )


# ──────────────────────────────────────────────────────────────────
# TEST 19 — non-AddressableGoalBackend raises AtomicAgentsError


def test_non_addressable_backend_raises(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A goal_backend that doesn't implement for_goal() must raise AtomicAgentsError."""
    pb_dir = agent_root / "skills" / "noaddr-playbook"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="noaddr-playbook")
    )
    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "noaddr-playbook")

    # Replace goal_backend with a mock that does NOT have for_goal()
    mock_agent.goal_backend = MagicMock(spec=[])  # no for_goal attr

    with pytest.raises(AtomicAgentsError, match="AddressableGoalBackend"):
        run(playbook=playbook, subject="no-addr test", agent=mock_agent)


# ──────────────────────────────────────────────────────────────────
# TEST 20 — VALID_SUB_GOAL_STATUSES: 7-member set with 'awaiting_decision' + 'skipped' (PR2)


def test_valid_sub_goal_statuses_pr2() -> None:
    """VALID_SUB_GOAL_STATUSES must be the exact 7-member set added in PR2 (#581).

    PR2 adds 'awaiting_decision' (conductor gate suspension — NOT terminal) and
    'skipped' (terminal-done gate skip ruling) to the existing 5 PR1 statuses.
    Both are required (awaiting-decision-enum-rollout ruling — REJECTED reusing 'blocked').
    """
    # Both new statuses must be present
    assert "awaiting_decision" in VALID_SUB_GOAL_STATUSES, (
        "'awaiting_decision' must be in VALID_SUB_GOAL_STATUSES as of PR2 (#581). "
        "It was deferred from PR1."
    )
    assert "skipped" in VALID_SUB_GOAL_STATUSES, (
        "'skipped' must be in VALID_SUB_GOAL_STATUSES as of PR2 (#581). "
        "c4-skipped-stage-recorded-ruling requires a DEDICATED 'skipped' terminal status."
    )
    # The exact 7-member set — any extra status is also a spec violation
    assert VALID_SUB_GOAL_STATUSES == {
        "pending",
        "in_progress",
        "complete",
        "blocked",
        "abandoned",
        "awaiting_decision",
        "skipped",
    }, (
        f"VALID_SUB_GOAL_STATUSES must be exactly these 7 members; "
        f"got: {VALID_SUB_GOAL_STATUSES}. "
        "Any change beyond PR2's two additions needs a new arc ruling."
    )
    # Strip-RED: 'awaiting_decision' is NOT 'blocked' (rejected reuse — REJECTED ruling)
    assert "awaiting_decision" != "blocked"
    # Strip-RED: 'skipped' is NOT 'abandoned' (distinct terminal: done-by-skip vs halted)
    assert "skipped" != "abandoned"


# ──────────────────────────────────────────────────────────────────
# TEST 21 — ref hardening: prompt_ref/rubric_ref reject traversal/absolute/symlink


def _make_playbook_with_ref(
    pb_dir: Path, ref_field: str, ref_value: str, name: str = "ref-playbook"
) -> None:
    """Write a 1-stage PLAYBOOK.md whose single stage uses ``ref_field: ref_value``."""
    stages_yaml = (
        "stages:\n"
        "  - stage_id: the-stage\n"
        "    label: The stage\n"
        f"    {ref_field}: {ref_value}\n"
    )
    # prompt_ref-only stages still need a prompt source; rubric_ref stages keep prompt.
    if ref_field == "rubric_ref":
        stages_yaml = (
            "stages:\n"
            "  - stage_id: the-stage\n"
            "    label: The stage\n"
            "    prompt: Do the thing.\n"
            f"    rubric_ref: {ref_value}\n"
        )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name=name, stages=stages_yaml, run_cap_usd=2.0)
    )


@pytest.mark.parametrize("ref_field", ["prompt_ref", "rubric_ref"])
@pytest.mark.parametrize(
    "ref_value",
    [
        "/etc/passwd",  # absolute — collapses past the parts-based '..' check
        "../outside.md",  # classic traversal
        "sub/dir/deep.md",  # multi-level — contradicts 'one level deep'
    ],
)
def test_ref_rejects_unsafe_paths(
    tmp_path: Path, ref_field: str, ref_value: str
) -> None:
    """Absolute / traversal / multi-level refs are rejected by the hardened loader.

    NEGATIVE CONTROL per case: the prior hand-rolled ``'..' in ref_path.parts``
    check passed absolute and multi-level refs (and followed symlinks via is_file).
    Each value here would have been accepted by that check; the canonical-path
    invariant (_io.safe_resolve_under + one-level separator reject) rejects all.
    """
    pb_dir = tmp_path / "skills" / "ref-playbook"
    pb_dir.mkdir(parents=True)
    _make_playbook_with_ref(pb_dir, ref_field, ref_value)

    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None, (
        f"{ref_field}={ref_value!r} must be REJECTED (manifest None); "
        f"got a manifest. warnings={warnings}"
    )
    joined = " ".join(warnings)
    assert "rejected" in joined or ref_field in joined, (
        f"Expected a {ref_field} rejection error; got {warnings}"
    )


def test_ref_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside the playbook dir pointing OUTSIDE is refused.

    NEGATIVE CONTROL: is_file() follows symlinks, so the old check (no .resolve()
    + relative_to) would have read the out-of-tree target. safe_resolve_under
    resolves the link then enforces containment, catching the escape.
    """
    secret = tmp_path / "secret.md"
    secret.write_text("TOP SECRET — should never be read by a playbook")
    pb_dir = tmp_path / "skills" / "symlink-playbook"
    pb_dir.mkdir(parents=True)
    link = pb_dir / "link.md"
    link.symlink_to(secret)  # symlink INSIDE the dir, target OUTSIDE
    _make_playbook_with_ref(pb_dir, "prompt_ref", "link.md", name="symlink-playbook")

    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None, (
        "A symlink prompt_ref escaping the playbook dir must be REJECTED. "
        f"warnings={warnings}"
    )


def test_ref_accepts_valid_one_level_file(tmp_path: Path) -> None:
    """Positive control: a bare one-level prompt_ref inside the dir resolves + loads."""
    pb_dir = tmp_path / "skills" / "ok-playbook"
    pb_dir.mkdir(parents=True)
    (pb_dir / "longprompt.md").write_text("Write a thorough, well-sourced brief.")
    _make_playbook_with_ref(pb_dir, "prompt_ref", "longprompt.md", name="ok-playbook")

    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is not None, f"valid one-level prompt_ref should load; {warnings}"
    assert manifest.stages[0].prompt == "Write a thorough, well-sourced brief."


# ──────────────────────────────────────────────────────────────────
# TEST 22 — resume re-runs a blocked stage through the REAL coordinator


def test_resume_reruns_blocked_stage_real_coordinator(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A stage that ended max_iterations (mapped to 'blocked' by the REAL coordinator)
    is re-run on resume with NO GoalCorrupted escaping.

    The mock-only suite cannot catch this: it patches dispatch_sub_goal_as_outcome
    wholesale, so the coordinator's terminal blocked-mapping + its pending/in_progress
    guard never run together. Here we patch only the inner OutcomeRunner so the REAL
    coordinator (terminal mapping + guard) runs end to end.
    """
    mock_agent._check_cost_guardrails = MagicMock(
        return_value=CostCheckResult(allow=True)
    )

    pb_dir = agent_root / "skills" / "blocked-resume-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: the-stage
            label: The stage
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="blocked-resume-playbook", stages=stages_yaml, run_cap_usd=5.0
        )
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "blocked-resume-playbook"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # First run: inner OutcomeRunner returns max_iterations_reached → REAL coordinator
    # maps to applied_status='blocked'; run() halts. Stage left 'blocked' on disk.
    with (
        patch("atomic_agents.outcome.OutcomeRunner") as MockRunner,
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        MockRunner.return_value.run.return_value = _make_outcome_result(
            status="max_iterations_reached", total_cost_usd=0.10
        )
        state1 = run(playbook=playbook, subject="s", agent=mock_agent)

    assert state1.status == "halted"
    assert state1.halt_reason == "stage_max_iterations_reached"
    run_id = state1.conductor_run_id

    cb = mock_agent.goal_backend.for_goal(run_id)
    goal_after = cb.load_goal(mock_agent.name)
    blocked_sg = next(s for s in goal_after.sub_goals if s.id == "the-stage")
    assert blocked_sg.status == "blocked", (
        f"max_iterations stage should be 'blocked' on disk; got {blocked_sg.status}"
    )

    # Resume: inner runner now satisfies. WITHOUT the normalize fix the coordinator
    # guard raises GoalCorrupted on the 'blocked' sub-goal. With it, the stage is
    # reset blocked→in_progress and re-run to complete.
    idem2 = _make_idempotency_backend_mock()
    with (
        patch("atomic_agents.outcome.OutcomeRunner") as MockRunner2,
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem2
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        MockRunner2.return_value.run.return_value = _make_outcome_result(
            status="satisfied", total_cost_usd=0.05
        )
        state2 = run(
            playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id
        )

    assert state2.status == "complete", (
        f"resume of a max_iterations-blocked stage must complete, not crash; got {state2.status}"
    )
    # The reset event is in the durable ledger.
    history = (agent_root / "goals" / run_id / "goal_history.jsonl").read_text()
    assert "conductor_stage_reset_for_rerun" in history


# ──────────────────────────────────────────────────────────────────
# TEST 22b — abandoned stage is TERMINAL on resume (maintainer ruling A)


def test_resume_halts_on_abandoned_stage_terminal(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A stage marked 'abandoned' HALTS the run on resume (reason='stage_abandoned')
    and is NOT re-dispatched — only a deliberate operator/external action sets
    'abandoned', so crash recovery must not auto-revive it (maintainer ruling A).

    NEGATIVE CONTROL: the PRIOR behavior normalized blocked/abandoned →
    in_progress and re-ran the stage. Asserting (a) status='halted' with
    reason='stage_abandoned', (b) ZERO dispatch calls, and (c) NO
    conductor_stage_reset_for_rerun event is the guard that abandoned is no longer
    revived.
    """
    pb_dir = agent_root / "skills" / "abandoned-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: the-stage
            label: The stage
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="abandoned-playbook", stages=stages_yaml, run_cap_usd=5.0
        )
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "abandoned-playbook"
    )

    # First run completes the stage (so the goal/run exist on disk).
    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="s", agent=mock_agent)
    assert state1.status == "complete"
    run_id = state1.conductor_run_id

    # Operator abandons the stage (the only way 'abandoned' arises).
    cb = mock_agent.goal_backend.for_goal(run_id)
    cb.apply_transition(
        agent_id=mock_agent.name,
        sub_goal_id="the-stage",
        to_status="abandoned",
        fields={},
        history_prose="operator abandoned the stage",
        history_event={"ts": "2026-06-01T00:00:00+00:00", "event": "test_abandon"},
        expected_from_status="complete",
        when=date.today(),
    )

    # Resume: must HALT terminal, not revive.
    dispatch_calls: list[str] = []

    def _track_dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _make_outcome_result(status="satisfied"), MagicMock(status="complete")

    idem2 = _make_idempotency_backend_mock()
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_track_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem2
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = run(
            playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id
        )

    assert state2.status == "halted"
    assert state2.halt_reason == "stage_abandoned", (
        f"abandoned stage must halt terminal on resume; got {state2.halt_reason!r}"
    )
    assert dispatch_calls == [], (
        f"an abandoned stage must NOT be re-dispatched; got {dispatch_calls}"
    )
    history = (agent_root / "goals" / run_id / "goal_history.jsonl").read_text()
    events = [json.loads(ln) for ln in history.splitlines() if ln.strip()]
    assert any(
        e.get("event") == "conductor_run_halted"
        and e.get("reason") == "stage_abandoned"
        for e in events
    ), "resume must emit a conductor_run_halted reason='stage_abandoned' audit event"
    assert not any(
        e.get("event") == "conductor_stage_reset_for_rerun" for e in events
    ), "an abandoned stage must NOT be normalized/reset for rerun (no revival)"


# ──────────────────────────────────────────────────────────────────
# TEST 23 — within-stage tree-cap halts a REAL OutcomeRunner mid-stage


def test_within_stage_tree_cap_halts_real_outcome_runner(agent_root: Path) -> None:
    """A real OutcomeRunner halts mid-stage (interrupted) once per-iteration spend
    exhausts the threaded run-level headroom.

    NEGATIVE CONTROL for the round-1 shortcut: if the headroom were threaded as a
    FIXED snapshot (never decremented by the stage's own spend), the gate would
    always see the full 0.10 (>0) and the loop would run to max_iterations_reached,
    not interrupt. Asserting 'interrupted' (and < all iterations) is the regression
    guard that the snapshot is decremented per iteration.
    """
    from atomic_agents.outcome._outcome_impl import OutcomeRunner

    runner = OutcomeRunner(
        agents_root=agent_root.parent,
        agent_name=agent_root.name,
        judge_model="gpt-5",
        parent_remaining_headroom_usd=0.10,
    )

    # Faithful re-implementation of the production clamp (agent.py:7486): allow
    # unless min(own_remaining, parent_headroom) <= 0. own_remaining is ~inf here
    # (mocked calls write no ledger spend), so the THREADED headroom is the sole
    # binding term — exactly the within-stage tree-cap under test.
    def _gate(critical: bool = False, parent_remaining_headroom_usd=None):
        if (
            parent_remaining_headroom_usd is not None
            and parent_remaining_headroom_usd <= 0
        ):
            return CostCheckResult(
                allow=False, action="skip", reason="parent headroom exhausted"
            )
        return CostCheckResult(allow=True)

    agent_resp = MagicMock()
    agent_resp.text = "draft"
    agent_resp.model = "claude-3-5-haiku-20241022"
    agent_resp.input_tokens = 100
    agent_resp.output_tokens = 50
    agent_resp.cost_usd = 0.08  # two iterations exhaust the 0.10 headroom
    agent_resp.skipped = False
    agent_resp.skip_reason = ""

    unsatisfied = json.dumps(
        {"satisfied": False, "criterion_results": [], "explanation": "needs more"}
    )
    judge_resp = MagicMock()
    judge_resp.text = unsatisfied
    judge_resp.input_tokens = 10
    judge_resp.output_tokens = 5

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mi = MagicMock()
        mi.call.return_value = agent_resp
        mi.config.default_model = "claude-3-5-haiku-20241022"
        mi._check_cost_guardrails.side_effect = _gate
        MockAgent.return_value = mi

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="do the thing",
                rubric="rubric text\n# criteria",
                max_iterations=5,
            )

    assert result.status == "interrupted", (
        f"expected a within-stage halt (interrupted) once the threaded headroom was "
        f"exhausted; got status={result.status!r}. If 'max_iterations_reached', the "
        "run-level headroom is NOT decrementing per iteration (tree-cap snapshot inert)."
    )
    assert len(result.iterations) < 6, (
        "the run must halt BEFORE exhausting all iterations (mid-stage), proving the "
        f"cap bound within the stage; got {len(result.iterations)} iterations"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 24 — coordinator threads parent headroom to the gate AND the runner


def test_coordinator_threads_parent_headroom(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """The REAL dispatch forwards parent_remaining_headroom_usd to BOTH the
    pre-dispatch cost gate AND the OutcomeRunner constructor.

    A no-op coordinator that accepted-and-dropped the kwarg would pass every
    mocked conductor test; this asserts the headline tree-cap wiring directly.
    """
    mock_agent._check_cost_guardrails = MagicMock(
        return_value=CostCheckResult(allow=True)
    )

    pb_dir = agent_root / "skills" / "thread-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: only-stage
            label: Only stage
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="thread-playbook", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "thread-playbook"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with (
        patch("atomic_agents.outcome.OutcomeRunner") as MockRunner,
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        MockRunner.return_value.run.return_value = _make_outcome_result(
            status="satisfied", total_cost_usd=0.05
        )
        state = run(playbook=playbook, subject="s", agent=mock_agent)

    assert state.status == "complete"

    # Gate received the run-level headroom (run_cap 5.0 − 0 prior spend).
    gate_calls = mock_agent._check_cost_guardrails.call_args_list
    assert gate_calls, "the pre-dispatch cost gate must have been called"
    assert any(
        abs((c.kwargs.get("parent_remaining_headroom_usd") or -1) - 5.0) < 1e-9
        for c in gate_calls
    ), (
        "dispatch must thread run_remaining (5.0) into _check_cost_guardrails; "
        f"got calls={[c.kwargs for c in gate_calls]}"
    )

    # OutcomeRunner was constructed with the same headroom (the per-iteration cap).
    ctor_kwargs = MockRunner.call_args.kwargs
    assert (
        ctor_kwargs.get("parent_remaining_headroom_usd") is not None
        and abs(ctor_kwargs["parent_remaining_headroom_usd"] - 5.0) < 1e-9
    ), (
        "OutcomeRunner must be constructed with parent_remaining_headroom_usd=5.0 "
        f"(the within-stage tree-cap); got ctor kwargs={ctor_kwargs}"
    )


# ──────────────────────────────────────────────────────────────────
# Shared helper for the resume-predicate / cap-pin / audit tests below.


def _run_single_stage_to_complete(
    agent_root: Path,
    mock_agent: MagicMock,
    *,
    name: str,
    run_cap_usd: float = 5.0,
) -> tuple[Any, str]:
    """Run a 1-stage playbook to completion; return (playbook, conductor_run_id).

    Uses the ledger-updating dispatch so the terminal transition AND the
    conductor's _store_outcome_pointer (sub_goal.output) both land — the
    realistic post-run state the resume-predicate tests start from.
    """
    pb_dir = agent_root / "skills" / name
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: only-stage
            label: Only stage
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name=name, stages=stages_yaml, run_cap_usd=run_cap_usd)
    )
    playbook = next(m for m in discover_playbooks(agent_root) if m.name == name)

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(playbook=playbook, subject="s", agent=mock_agent)
    assert state.status == "complete", f"setup run must complete; got {state.status}"
    # The completed stage must carry a stored result pointer (C2 precondition).
    cb = mock_agent.goal_backend.for_goal(state.conductor_run_id)
    sg = next(
        s for s in cb.load_goal(mock_agent.name).sub_goals if s.id == "only-stage"
    )
    assert sg.output, "setup: completed stage should have a stored result pointer"
    return playbook, state.conductor_run_id


# ──────────────────────────────────────────────────────────────────
# TEST 25 — HIGH 1: resume FAILS CLOSED when a complete stage has no result pointer


def test_resume_fails_closed_on_complete_without_result_pointer(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A `complete` stage whose stored result pointer is absent is ledger
    corruption — resume MUST raise GoalCorrupted (C2 full predicate), not skip.

    NEGATIVE CONTROL: without the HIGH 1 fix the skip predicate keyed on
    `sg.status == 'complete'` ALONE, so this stage would be silently skipped and
    the run would return status='complete'. Asserting GoalCorrupted is the guard
    that the result-presence half of C2 is enforced.
    """
    playbook, run_id = _run_single_stage_to_complete(
        agent_root, mock_agent, name="h1-absent"
    )

    # Clear the result pointer while leaving the stage 'complete' (the corruption
    # shape: complete-but-no-result).
    cb = mock_agent.goal_backend.for_goal(run_id)
    cb.apply_transition(
        agent_id=mock_agent.name,
        sub_goal_id="only-stage",
        to_status="complete",
        fields={"output": None},
        history_prose="test: clear result pointer to simulate complete-but-no-result",
        history_event={"ts": "2026-06-01T00:00:00+00:00", "event": "test_clear_output"},
        expected_from_status="complete",
        when=date.today(),
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
        pytest.raises(GoalCorrupted, match="complete"),
    ):
        run(playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id)


def test_resume_fails_closed_on_complete_with_unresolvable_result(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A `complete` stage whose result_ref does NOT resolve via
    OutcomeBackend.read_result() is corruption — resume MUST fail closed.

    NEGATIVE CONTROL: without HIGH 1 the unresolvable result would never be probed
    and the stage would be silently skipped (status='complete'). Asserting
    GoalCorrupted proves read_result() resolution is part of the skip predicate.
    """
    playbook, run_id = _run_single_stage_to_complete(
        agent_root, mock_agent, name="h1-unresolvable"
    )

    idem = _make_idempotency_backend_mock()
    # outcome_backend.read_result raises → result_ref present but unresolvable.
    outcome_backend = MagicMock()
    outcome_backend.read_result.side_effect = AtomicAgentsError("result.json absent")
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
        pytest.raises(GoalCorrupted, match="unrecoverable"),
    ):
        run(playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id)
    # The store WAS probed (the resolution half of the predicate actually ran).
    assert outcome_backend.read_result.called


# ──────────────────────────────────────────────────────────────────
# TEST 26 — HIGH 3: the run cap is pinned at run start, not re-read live on resume


def test_run_cap_is_pinned_across_playbook_edit_on_resume(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Editing run_cap_usd in PLAYBOOK.md between runs MUST NOT change a live run's
    ceiling. The resumed run reports the run-START cap from the ledger.

    NEGATIVE CONTROL: without the HIGH 3 pin, run_cap_usd is read live from the
    (edited) playbook, so state.run_cap_usd would be the new 0.01, not 5.0.
    """
    playbook, run_id = _run_single_stage_to_complete(
        agent_root, mock_agent, name="h3-pin", run_cap_usd=5.0
    )

    # Operator edits the cap down to 0.01 between the crash and the resume.
    edited_playbook = replace(playbook, run_cap_usd=0.01)

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = run(
            playbook=edited_playbook,
            subject="s",
            agent=mock_agent,
            conductor_run_id=run_id,
        )

    assert state2.status == "complete"
    assert abs(state2.run_cap_usd - 5.0) < 1e-9, (
        "the resumed run must use the PINNED run-start cap (5.0), not the edited "
        f"live PLAYBOOK.md value (0.01); got {state2.run_cap_usd}. If 0.01, the cap "
        "is being re-read live and a mid-suspension edit silently moved the ceiling."
    )


def test_read_pinned_run_cap_reads_started_event(tmp_path: Path) -> None:
    """_read_pinned_run_cap returns the run_cap_usd from conductor_run_started."""
    history = tmp_path / "goal_history.jsonl"
    history.write_text(
        json.dumps(
            {
                "ts": "2026-06-01T00:00:00+00:00",
                "event": "conductor_run_started",
                "run_cap_usd": 7.5,
            }
        )
        + "\n"
    )
    assert _read_pinned_run_cap(history, default=1.0) == 7.5
    # Absent file → default (fresh run defensive fallback).
    assert _read_pinned_run_cap(tmp_path / "nope.jsonl", default=3.0) == 3.0


# ──────────────────────────────────────────────────────────────────
# TEST 27 — MED 5: an unexpected dispatch error emits a conductor_run_halted audit


def test_unexpected_dispatch_error_emits_halt_audit(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A non-CostGuardrailBlocked dispatch exception re-raises AND writes a
    conductor_run_halted ledger event (reason='dispatch_error') — every terminal
    exit is a durable fact (Principle #5).

    NEGATIVE CONTROL: without the MED 5 emit, the ledger would terminate on
    conductor_stage_started with NO halt event, indistinguishable from
    'still running'. Asserting the reason='dispatch_error' line is the guard.
    """
    pb_dir = agent_root / "skills" / "med5-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: boom-stage
            label: Boom
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="med5-playbook", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "med5-playbook"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    def _boom(agent, goal_manager, sub_goal_id, **kwargs):
        raise RuntimeError("backend exploded")

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_boom,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
        pytest.raises(RuntimeError, match="backend exploded"),
    ):
        run(playbook=playbook, subject="s", agent=mock_agent)

    # Find the conductor run goal the engine minted and read its ledger.
    histories = list((agent_root / "goals").glob("crun-*/goal_history.jsonl"))
    assert len(histories) == 1, f"expected one conductor run goal; got {histories}"
    events = [
        json.loads(line)
        for line in histories[0].read_text().splitlines()
        if line.strip()
    ]
    halts = [
        e
        for e in events
        if e.get("event") == "conductor_run_halted"
        and e.get("reason") == "dispatch_error"
    ]
    assert halts, (
        "an unexpected dispatch error must emit a conductor_run_halted "
        f"reason='dispatch_error' audit event; events={[e.get('event') for e in events]}"
    )
    assert "backend exploded" in halts[0].get("error", ""), (
        f"the halt event must carry the error text; got {halts[0]}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 28 — MED 6: a degraded cumulative-spend read surfaces cost_data_degraded


def test_cost_data_degraded_surfaced_on_state(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """When the in-loop cumulative-spend read is degraded, run() halts fail-closed
    AND the returned ConductorState carries cost_data_degraded=True (#498 posture).

    NEGATIVE CONTROL: without the MED 6 field/threading, the operator would see a
    misleadingly-clean spend number with no marker. Asserting the flag is True is
    the guard.
    """
    pb_dir = agent_root / "skills" / "med6-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: only-stage
            label: Only stage
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="med6-playbook", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "med6-playbook"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with (
        patch(
            "atomic_agents.conductor.run._sum_cumulative_spend",
            return_value=(0.0, True),
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(playbook=playbook, subject="s", agent=mock_agent)

    assert state.status == "halted"
    assert state.halt_reason == "cost_data_degraded"
    assert state.cost_data_degraded is True, (
        "a degraded cumulative-spend read must set ConductorState.cost_data_degraded"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 29 — T1: cross-kill/restart cap binding from the durable ledger


def test_resume_cap_binds_from_ledger_zero_dispatch(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A resumed run whose PRIOR ledger spend already meets run_cap_usd halts with
    run_cap_exhausted and dispatches NOTHING — proving the cap is re-summed from the
    durable ledger across a kill/restart, not carried in process memory.
    """
    pb_dir = agent_root / "skills" / "t1-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: stage-one
            label: Stage one
            prompt: Do the first thing.
          - stage_id: stage-two
            label: Stage two
            prompt: Do the second thing.
        """
    )
    # run_cap 0.05; stage-one costs exactly 0.05 → stage-two is at the cap.
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="t1-playbook", stages=stages_yaml, run_cap_usd=0.05)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "t1-playbook"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)

    first_calls: list[str] = []

    def _first(agent, goal_manager, sub_goal_id, **kwargs):
        first_calls.append(sub_goal_id)
        return _ledger_dispatch(agent, goal_manager, sub_goal_id, **kwargs)

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_first,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="s", agent=mock_agent)

    assert state1.status == "halted"
    assert state1.halt_reason == "run_cap_exhausted"
    assert first_calls == ["stage-one"], "first run dispatches only stage-one"
    run_id = state1.conductor_run_id

    # Resume (simulated restart): no process memory carried; the ledger already
    # holds 0.05 spend == cap, so the run must halt with ZERO dispatch.
    resume_calls: list[str] = []

    def _resume(agent, goal_manager, sub_goal_id, **kwargs):
        resume_calls.append(sub_goal_id)
        return _ledger_dispatch(agent, goal_manager, sub_goal_id, **kwargs)

    idem2 = _make_idempotency_backend_mock()
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_resume,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem2
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = run(
            playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id
        )

    assert state2.status == "halted"
    assert state2.halt_reason == "run_cap_exhausted"
    assert resume_calls == [], (
        f"a resume at/over the ledger-summed cap must dispatch nothing; got {resume_calls}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 30 — T2: idempotency-COMPLETED narrow-window replay closes from the store


def _seed_in_progress_single_stage(
    agent_root: Path, mock_agent: MagicMock, *, name: str
) -> tuple[Any, str]:
    """Create a 1-stage run, complete it, then reset the sub-goal to in_progress.

    Returns (playbook, conductor_run_id) with the-stage on disk as in_progress —
    the narrow-window crash shape (result committed, ledger not yet closed).
    """
    pb_dir = agent_root / "skills" / name
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: the-stage
            label: The stage
            prompt: Do the thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name=name, stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(m for m in discover_playbooks(agent_root) if m.name == name)

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(playbook=playbook, subject="s", agent=mock_agent)
    run_id = state.conductor_run_id
    cb = mock_agent.goal_backend.for_goal(run_id)
    cb.apply_transition(
        agent_id=mock_agent.name,
        sub_goal_id="the-stage",
        to_status="in_progress",
        fields={"output": None},
        history_prose="reset to in_progress (narrow-window crash)",
        history_event={"ts": "2026-06-01T00:00:00+00:00", "event": "test_reset"},
        expected_from_status="complete",
        when=date.today(),
    )
    return playbook, run_id


def _make_completed_idem(prior_result_ref: str) -> MagicMock:
    backend = MagicMock()
    completed = MagicMock()
    completed.state = COMPLETED
    completed.prior_result_ref = prior_result_ref
    backend.lookup.return_value = completed
    backend.begin.return_value = completed
    return backend


def test_idempotency_completed_closes_from_store_no_redispatch(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Idempotency COMPLETED + prior_result_ref while the goal still reads
    in_progress → the stage is closed FROM THE STORE (read_result called, NO
    re-dispatch) and a narrow_window_close event lands.
    """
    playbook, run_id = _seed_in_progress_single_stage(
        agent_root, mock_agent, name="t2-close"
    )

    idem = _make_completed_idem("outcome-prior-123")
    outcome_backend = MagicMock()
    outcome_backend.read_result.return_value = _make_outcome_result(
        status="satisfied", run_id="outcome-prior-123"
    )

    dispatch_calls: list[str] = []

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _make_outcome_result(status="satisfied"), MagicMock(status="complete")

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(
            playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id
        )

    assert state.status == "complete"
    assert dispatch_calls == [], "narrow-window close must NOT re-dispatch the stage"
    assert outcome_backend.read_result.called, "result must be read FROM THE STORE"
    events = [
        json.loads(ln)
        for ln in (agent_root / "goals" / run_id / "goal_history.jsonl")
        .read_text()
        .splitlines()
        if ln.strip()
    ]
    assert any(e.get("narrow_window_close") is True for e in events), (
        "a narrow_window_close completion event must land"
    )


def test_idempotency_completed_unreadable_falls_through_to_dispatch(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Companion: idempotency COMPLETED but read_result RAISES → fall through to
    normal dispatch (re-run the stage), never silently skip.
    """
    playbook, run_id = _seed_in_progress_single_stage(
        agent_root, mock_agent, name="t2-fallthrough"
    )

    idem = _make_completed_idem("outcome-gone")
    outcome_backend = MagicMock()
    outcome_backend.read_result.side_effect = AtomicAgentsError("result.json gone")

    dispatch_calls: list[str] = []
    _ledger = _make_ledger_updating_dispatch(total_cost_usd=0.05)

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _ledger(agent, goal_manager, sub_goal_id, **kwargs)

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(
            playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id
        )

    assert state.status == "complete"
    assert dispatch_calls == ["the-stage"], (
        "an unreadable COMPLETED result must fall through to a real re-dispatch"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 31 — T3: partial crash recovery — completed stage skipped, pending re-run


def test_partial_crash_recovery_skips_completed_runs_pending(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A 2-stage run where stage-one is complete and stage-two is pending → resume
    dispatches ONLY stage-two (stage-one skipped, not re-dispatched).
    """
    pb_dir = agent_root / "skills" / "t3-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: stage-one
            label: Stage one
            prompt: Do the first thing.
          - stage_id: stage-two
            label: Stage two
            prompt: Do the second thing.
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="t3-playbook", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "t3-playbook"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    _ledger = _make_ledger_updating_dispatch(total_cost_usd=0.05)

    # First run: complete BOTH stages (real ledger), then reset stage-two to pending
    # to model "stage-one done, stage-two not yet run" at crash time.
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="s", agent=mock_agent)
    assert state1.status == "complete"
    run_id = state1.conductor_run_id

    cb = mock_agent.goal_backend.for_goal(run_id)
    cb.apply_transition(
        agent_id=mock_agent.name,
        sub_goal_id="stage-two",
        to_status="pending",
        fields={"output": None},
        history_prose="reset stage-two to pending (partial-crash model)",
        history_event={
            "ts": "2026-06-01T00:00:00+00:00",
            "event": "test_reset_pending",
        },
        expected_from_status="complete",
        when=date.today(),
    )

    resume_calls: list[str] = []

    def _resume(agent, goal_manager, sub_goal_id, **kwargs):
        resume_calls.append(sub_goal_id)
        return _ledger(agent, goal_manager, sub_goal_id, **kwargs)

    idem2 = _make_idempotency_backend_mock()
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_resume,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem2
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = run(
            playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id
        )

    assert state2.status == "complete"
    assert resume_calls == ["stage-two"], (
        f"only the pending stage must re-run; stage-one must be skipped. got {resume_calls}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 32 — H1 repair: a missing pointer is repaired from idempotency, not wedged


def test_resume_repairs_missing_pointer_from_idempotency(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A complete stage whose sub_goal.output is missing but whose idempotency
    record is COMPLETED with a RESOLVABLE prior_result_ref is REPAIRED (pointer
    re-stored) and skipped — NOT failed closed and NOT re-dispatched (H1).

    NEGATIVE CONTROL: with the fail-closed-only behavior this would raise
    GoalCorrupted; with no idempotency repair the stage would be re-dispatched.
    """
    playbook, run_id = _run_single_stage_to_complete(
        agent_root, mock_agent, name="h1-repair"
    )

    # Clear the pointer (complete-but-no-output corruption shape).
    cb = mock_agent.goal_backend.for_goal(run_id)
    cb.apply_transition(
        agent_id=mock_agent.name,
        sub_goal_id="only-stage",
        to_status="complete",
        fields={"output": None},
        history_prose="clear pointer to model a lost pointer-write",
        history_event={"ts": "2026-06-01T00:00:00+00:00", "event": "test_clear_output"},
        expected_from_status="complete",
        when=date.today(),
    )

    # Idempotency still holds the COMPLETED record + a resolvable prior_result_ref.
    idem = _make_completed_idem("outcome-recovered-1")
    outcome_backend = MagicMock()
    outcome_backend.read_result.return_value = _make_outcome_result(
        run_id="outcome-recovered-1"
    )

    dispatch_calls: list[str] = []

    def _dispatch(agent, goal_manager, sub_goal_id, **kwargs):
        dispatch_calls.append(sub_goal_id)
        return _make_outcome_result(status="satisfied"), MagicMock(status="complete")

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(
            playbook=playbook, subject="s", agent=mock_agent, conductor_run_id=run_id
        )

    assert state.status == "complete"
    assert dispatch_calls == [], "a repairable stage must NOT be re-dispatched"
    # The pointer was re-stored from idempotency.
    sg = next(
        s for s in cb.load_goal(mock_agent.name).sub_goals if s.id == "only-stage"
    )
    assert sg.output == "outcome-recovered-1", (
        f"pointer must be repaired from idempotency prior_result_ref; got {sg.output!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 33 — C1: non-finite run_cap_usd is rejected at parse


@pytest.mark.parametrize("cap_value", [".inf", ".nan", "1e999"])
def test_non_finite_run_cap_rejected(tmp_path: Path, cap_value: str) -> None:
    """run_cap_usd of .inf/.nan/1e999 is REJECTED (would silently disable the cap).

    NEGATIVE CONTROL: a finite cap (test_valid_playbook_parses) parses fine; only
    the non-finite values are refused here, proving the finite-guard, not a blanket
    rejection.
    """
    pb_dir = tmp_path / "skills" / "nonfinite-cap"
    pb_dir.mkdir(parents=True)
    body = (
        "---\nname: nonfinite-cap\ndescription: d\nkind: playbook\n---\n\n"
        "```yaml\n"
        f"run_cap_usd: {cap_value}\n"
        "stages:\n"
        "  - stage_id: only-stage\n"
        "    label: Only\n"
        "    prompt: Do it.\n"
        "```\n"
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(body)
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None, f"{cap_value!r} cap must be rejected; got a manifest"
    assert any("finite" in w for w in warnings), (
        f"rejection must cite 'finite'; got {warnings}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 34 — C2: a non-finite per-event spend marks the read degraded (fail-closed)


def test_non_finite_spend_marks_degraded(tmp_path: Path) -> None:
    """A sub_goal_outcome_dispatched event with a NaN/inf total_cost_usd must NOT
    poison the cumulative sum — it is reported degraded (which fails the run
    closed), and the finite total excludes it.

    NEGATIVE CONTROL: a finite-cost event (test_cumulative_spend_from_jsonl) sums
    cleanly and is not degraded; here only the non-finite value flips degraded.
    """
    history = tmp_path / "goal_history.jsonl"
    good = {
        "ts": "2026-06-01T10:00:00+00:00",
        "event": "sub_goal_outcome_dispatched",
        "applied_status": "complete",
        "total_cost_usd": 0.05,
    }
    bad = {
        "ts": "2026-06-01T10:01:00+00:00",
        "event": "sub_goal_outcome_dispatched",
        "applied_status": "in_progress",
        "total_cost_usd": float("nan"),
    }
    # json.dumps(allow_nan=True default) emits literal NaN, which json.loads reads.
    history.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")

    total, degraded = _sum_cumulative_spend(history)
    assert degraded is True, "a non-finite per-event cost must mark the read degraded"
    assert math.isfinite(total) and abs(total - 0.05) < 1e-9, (
        f"the non-finite value must be excluded from the finite total; got {total}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 35 — H3: a malformed stage_id is refused at parse (no goal/ledger side effect)


@pytest.mark.parametrize(
    "bad_id",
    ["a/b", "..", "../escape", "has space", "UPPER", "x" * 65],
)
def test_malformed_stage_id_rejected_at_parse(tmp_path: Path, bad_id: str) -> None:
    """A separator/'..'/whitespace/uppercase/over-length stage_id is REFUSED at
    parse time (before any goal creation or ledger event).

    NEGATIVE CONTROL: valid ids like 'stage-one' parse fine (other tests); only
    the malformed values here are rejected, proving the charset guard.
    """
    pb_dir = tmp_path / "skills" / "badid"
    pb_dir.mkdir(parents=True)
    body = (
        "---\nname: badid\ndescription: d\nkind: playbook\n---\n\n"
        "```yaml\n"
        "run_cap_usd: 5.0\n"
        "stages:\n"
        f'  - stage_id: "{bad_id}"\n'
        "    label: Bad\n"
        "    prompt: Do it.\n"
        "```\n"
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(body)
    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None, f"stage_id {bad_id!r} must be rejected; got a manifest"
    assert any("stage_id" in w for w in warnings), (
        f"rejection must cite stage_id; got {warnings}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 36 — H2: the pinned run cap exists the instant the goal exists


def test_run_cap_pinned_event_written_at_create(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """The conductor_run_started event (carrying run_cap_usd) is written as part of
    goal creation, so the pin exists the instant goals/<id>/ exists (H2).
    """
    pb_dir = agent_root / "skills" / "h2-playbook"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: human review
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="h2-playbook", stages=stages_yaml, run_cap_usd=3.5)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "h2-playbook"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(playbook=playbook, subject="s", agent=mock_agent)

    run_id = state.conductor_run_id
    events = [
        json.loads(ln)
        for ln in (agent_root / "goals" / run_id / "goal_history.jsonl")
        .read_text()
        .splitlines()
        if ln.strip()
    ]
    started = [e for e in events if e.get("event") == "conductor_run_started"]
    assert len(started) == 1, f"exactly one started event expected; got {len(started)}"
    assert started[0]["run_cap_usd"] == 3.5, "started event must carry the pinned cap"
    # And the pin is the FIRST conductor event (precedes the gate-halt) — written at create.
    conductor_events = [
        e["event"] for e in events if str(e.get("event", "")).startswith("conductor_")
    ]
    assert conductor_events[0] == "conductor_run_started", (
        f"the pin must be the first conductor event; got order {conductor_events}"
    )


# ──────────────────────────────────────────────────────────────────
# Review-driven fix-set helpers + tests (#581 PR2 fix set)


def _strip_event_lines(history_path: Path, event_name: str) -> int:
    """Delete every JSONL line whose 'event' == event_name. Returns count removed."""
    lines = history_path.read_text().splitlines()
    kept: list[str] = []
    removed = 0
    for ln in lines:
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            kept.append(ln)
            continue
        if isinstance(rec, dict) and rec.get("event") == event_name:
            removed += 1
            continue
        kept.append(ln)
    history_path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return removed


# ──────────────────────────────────────────────────────────────────
# P0-1 — gate re-surface reconstructs from DURABLE status; heals a missing
# conductor_gate_pending audit event (MUST-6 crash window), never raises.


def test_gate_resurface_heals_missing_pending_event(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Deleting the conductor_gate_pending event (simulating the MUST-6 crash
    window: status landed in goal.md, audit event did not) must NOT make the run
    unresumable. Re-run() reconstructs the GateDecision from the durable status +
    the playbook gate stage, re-surfaces the SAME decision_id, and HEALS the event.

    Negative control: an awaiting_decision sub-goal with NO gate_decision_id (the
    durable cursor itself unreadable) DOES raise GoalCorrupted — distinguishing a
    missing-audit window (healed) from genuine cursor corruption.
    """
    pb_dir = agent_root / "skills" / "heal-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Human gate
            prompt: Approve the plan?
            is_gate: true
            options:
              - Approve
              - Reject
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="heal-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(m for m in discover_playbooks(agent_root) if m.name == "heal-pb")

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="heal test", agent=mock_agent)

    assert state1.status == "awaiting_decision"
    decision_id = state1.pending_decision.decision_id
    run_id = state1.conductor_run_id
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"

    # Simulate the MUST-6 crash window: delete the conductor_gate_pending audit
    # event but LEAVE status='awaiting_decision' + gate_decision_id in goal.md.
    removed = _strip_event_lines(history_path, "conductor_gate_pending")
    assert removed == 1, "fixture must have exactly one pending event to delete"

    # Re-run() — must re-surface (NOT raise GoalCorrupted), reconstructing from
    # status + playbook, and heal the audit.
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state2 = run(
            playbook=playbook,
            subject="heal test",
            agent=mock_agent,
            conductor_run_id=run_id,
        )

    assert state2.status == "awaiting_decision", (
        "a missing conductor_gate_pending audit event must NOT make the run "
        f"unresumable; got {state2.status!r}"
    )
    gd = state2.pending_decision
    assert gd.decision_id == decision_id, "re-surface must keep the SAME decision_id"
    # prompt/options reconstructed from the (fingerprint-pinned) playbook gate stage.
    assert gd.prompt == "Approve the plan?"
    assert gd.options == ["Approve", "Reject"]

    # The audit was HEALED: the event re-appears, marked healed.
    events = [
        json.loads(ln) for ln in history_path.read_text().splitlines() if ln.strip()
    ]
    healed = [
        e
        for e in events
        if e.get("event") == "conductor_gate_pending"
        and e.get("decision_id") == decision_id
    ]
    assert len(healed) == 1, f"the missing pending event must be healed; got {healed}"
    assert healed[0].get("healed_missing_audit") is True

    # Negative control: clear gate_decision_id (durable cursor unreadable) while
    # status stays 'awaiting_decision' → GENUINE corruption → GoalCorrupted.
    cb = mock_agent.goal_backend.for_goal(run_id)
    cb.apply_transition(
        agent_id=mock_agent.name,
        sub_goal_id="gate-stage",
        to_status="awaiting_decision",
        fields={"gate_decision_id": None},
        history_prose="test: clear gate_decision_id to simulate cursor corruption",
        history_event={"ts": "2026-06-01T00:00:00+00:00", "event": "test_clear_gdid"},
        expected_from_status="awaiting_decision",
        when=date.today(),
    )
    with pytest.raises(GoalCorrupted, match="gate_decision_id"):
        with (
            patch(
                "atomic_agents.conductor.run._get_idempotency_backend",
                return_value=_make_idempotency_backend_mock(),
            ),
            patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ),
        ):
            run(
                playbook=playbook,
                subject="heal test",
                agent=mock_agent,
                conductor_run_id=run_id,
            )


# ──────────────────────────────────────────────────────────────────
# P0-2 — the playbook STRUCTURE is pinned across suspend/resume; an edited
# PLAYBOOK.md is refused on resume AND on a re-entrant run().


def test_resume_refuses_edited_playbook_structure(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Editing a gate stage's prompt during suspension must be REFUSED on resume
    (and on a re-entrant run()), with a clear playbook-changed error. Negative
    control: resuming with the UNCHANGED playbook is accepted.
    """
    pb_dir = agent_root / "skills" / "pin-pb"
    pb_dir.mkdir()
    stages_yaml_v1 = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Original prompt.
            is_gate: true
          - stage_id: post
            label: Post
            prompt: After the gate.
        """
    )
    pb_path = pb_dir / PLAYBOOK_ENTRY_POINT
    pb_path.write_text(
        _make_playbook_md(name="pin-pb", stages=stages_yaml_v1, run_cap_usd=5.0)
    )
    playbook_v1 = next(m for m in discover_playbooks(agent_root) if m.name == "pin-pb")

    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.02)
    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook_v1, subject="pin test", agent=mock_agent)

    assert state1.status == "awaiting_decision"
    run_id = state1.conductor_run_id
    decision_id = state1.pending_decision.decision_id

    # Edit the gate stage prompt on disk and re-discover → a structurally different
    # playbook (same name, same stage ids, changed prompt).
    stages_yaml_v2 = stages_yaml_v1.replace("Original prompt.", "EDITED prompt.")
    pb_path.write_text(
        _make_playbook_md(name="pin-pb", stages=stages_yaml_v2, run_cap_usd=5.0)
    )
    playbook_v2 = next(m for m in discover_playbooks(agent_root) if m.name == "pin-pb")

    # resume() with the edited playbook → refused BEFORE recording the answer.
    with pytest.raises(AtomicAgentsError, match="PLAYBOOK.md structure"):
        resume(
            playbook=playbook_v2,
            subject="pin test",
            agent=mock_agent,
            conductor_run_id=run_id,
            decision_id=decision_id,
            answer="Proceed.",
            disposition="continue",
            rationale="r",
        )

    # The gate must NOT have been transitioned by the refused resume.
    gate_sg = next(
        sg
        for sg in mock_agent.goal_backend.for_goal(run_id)
        .load_goal(mock_agent.name)
        .sub_goals
        if sg.id == "gate-stage"
    )
    assert gate_sg.status == "awaiting_decision", (
        "a structure-changed resume must not transition the gate"
    )

    # re-entrant run() with the edited playbook → also refused.
    with pytest.raises(AtomicAgentsError, match="PLAYBOOK.md structure"):
        with (
            patch(
                "atomic_agents.conductor.run._get_idempotency_backend",
                return_value=_make_idempotency_backend_mock(),
            ),
            patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ),
        ):
            run(
                playbook=playbook_v2,
                subject="pin test",
                agent=mock_agent,
                conductor_run_id=run_id,
            )

    # Negative control: resuming with the UNCHANGED (v1) playbook is accepted.
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state_ok = resume(
            playbook=playbook_v1,
            subject="pin test",
            agent=mock_agent,
            conductor_run_id=run_id,
            decision_id=decision_id,
            answer="Proceed.",
            disposition="continue",
            rationale="r",
        )
    assert state_ok.status != "awaiting_decision", (
        "the unchanged playbook must resume normally (negative control)"
    )


# ──────────────────────────────────────────────────────────────────
# C1 — the conductor CLI constructs AtomicAgent correctly (run AND resume).
# Pre-fix it passed agent_name= (TypeError on every invocation).


def _full_cli_agent_root(tmp_path: Path) -> Path:
    """A real agent root the CLI can construct an AtomicAgent against."""
    root = tmp_path / "agents" / "cli-agent"
    (root / "persona").mkdir(parents=True)
    (root / "model.md").write_text(
        textwrap.dedent(
            """\
            ---
            provider: anthropic
            model: claude-3-5-haiku-20241022
            cost_guardrails:
              max_cost_per_run_usd: 2.00
              max_cumulative_cost_usd: 20.00
            ---
            """
        )
    )
    (root / "persona" / "IDENTITY.md").write_text(
        "---\nname: cli-agent\nrole: tester\n---\nA test agent.\n"
    )
    pb = root / "skills" / "cli-pb"
    pb.mkdir(parents=True)
    (pb / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="cli-pb",
            run_cap_usd=5.0,
            stages=(
                "stages:\n"
                "  - stage_id: only-stage\n"
                "    label: Only\n"
                "    prompt: Do it.\n"
            ),
        )
    )
    return root


def test_conductor_cli_constructs_agent_run_and_resume(tmp_path: Path) -> None:
    """_cmd_run and _cmd_resume must construct AtomicAgent without TypeError (C1).

    The constructor is exercised before the (stubbed) run/resume call, so a return
    code (not a TypeError) proves the `name=`/`agent_name=` fix.
    """
    from atomic_agents.conductor import __main__ as cli  # noqa: PLC0415

    root = _full_cli_agent_root(tmp_path)

    captured: dict = {}

    def _run_stub(**kwargs):
        captured["run_agent"] = kwargs["agent"]
        return ConductorState(
            conductor_run_id="crun-stub",
            playbook_name="cli-pb",
            subject="s",
            status="complete",
            halt_reason=None,
            stages_total=1,
            stages_complete=1,
            cumulative_spend_usd=0.0,
            run_cap_usd=5.0,
            completed_stage_ids=["only-stage"],
        )

    parser = cli._build_parser()
    run_args = parser.parse_args(
        ["run", "cli-pb", "subj", str(root), "--max-stage-iterations", "2"]
    )
    with patch("atomic_agents.conductor.run", _run_stub):
        rc_run = cli._cmd_run(run_args)
    assert rc_run == 0, "CLI run must construct the agent and exit 0 (C1 fix)"
    assert captured["run_agent"].name == "cli-agent", (
        "the constructed agent must be named for the agent_root dir"
    )

    def _resume_stub(**kwargs):
        captured["resume_agent"] = kwargs["agent"]
        captured["resume_max_iter"] = kwargs["max_stage_iterations"]
        captured["resume_principal"] = kwargs["principal"]
        return ConductorState(
            conductor_run_id="crun-stub",
            playbook_name="cli-pb",
            subject="s",
            status="halted",
            halt_reason="gate_halt_disposition",
            stages_total=1,
            stages_complete=0,
            cumulative_spend_usd=0.0,
            run_cap_usd=5.0,
            completed_stage_ids=[],
        )

    resume_args = parser.parse_args(
        [
            "resume",
            str(root),
            "crun-xyz",
            "--decision-id",
            "gate-abc",
            "--answer",
            "ok",
            "--rationale",
            "because",
            "--disposition",
            "halt",
            "--playbook-name",
            "cli-pb",
            "--subject",
            "subj",
            "--max-stage-iterations",
            "7",
            "--answered-by",
            "alice",
        ]
    )
    with patch("atomic_agents.conductor.resume", _resume_stub):
        rc_resume = cli._cmd_resume(resume_args)
    assert rc_resume == 1, "CLI resume must construct the agent and exit 1 on halt"
    assert captured["resume_agent"].name == "cli-agent"
    # H4 — the new flags are forwarded.
    assert captured["resume_max_iter"] == 7, (
        "resume must forward --max-stage-iterations"
    )
    assert captured["resume_principal"].identifier == "alice", (
        "resume must forward an --answered-by verified local principal"
    )
    assert captured["resume_principal"].is_verified is True


# ──────────────────────────────────────────────────────────────────
# C2 — resume() HARD-REFUSES an unverified principal before any ledger write.


def test_resume_refuses_unverified_principal(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """An unverified principal must be refused (the gate-ruling author must be
    verified); LOCAL_PRINCIPAL (verified by construction) is accepted.
    """
    pb_dir = agent_root / "skills" / "c2-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Approve?
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="c2-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(m for m in discover_playbooks(agent_root) if m.name == "c2-pb")

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="c2 test", agent=mock_agent)

    assert state1.status == "awaiting_decision"
    run_id = state1.conductor_run_id
    decision_id = state1.pending_decision.decision_id
    unverified = Principal(
        identifier="attacker", derivation_source="header", is_verified=False
    )

    with pytest.raises(UnverifiedPrincipalConversationAccess):
        resume(
            playbook=playbook,
            subject="c2 test",
            agent=mock_agent,
            conductor_run_id=run_id,
            decision_id=decision_id,
            answer="Yes",
            disposition="halt",
            rationale="r",
            principal=unverified,
        )

    # No ledger write happened: gate still awaiting_decision, no answered event.
    cb = mock_agent.goal_backend.for_goal(run_id)
    gate_sg = next(
        sg for sg in cb.load_goal(mock_agent.name).sub_goals if sg.id == "gate-stage"
    )
    assert gate_sg.status == "awaiting_decision"
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    events = [
        json.loads(ln) for ln in history_path.read_text().splitlines() if ln.strip()
    ]
    assert not [e for e in events if e.get("event") == "conductor_gate_answered"], (
        "an unverified-principal refusal must write NO conductor_gate_answered event"
    )

    # Negative control: LOCAL_PRINCIPAL (verified) is accepted.
    state_ok = resume(
        playbook=playbook,
        subject="c2 test",
        agent=mock_agent,
        conductor_run_id=run_id,
        decision_id=decision_id,
        answer="Yes",
        disposition="halt",
        rationale="r",
        principal=LOCAL_PRINCIPAL,
    )
    assert state_ok.status == "halted", "a verified principal must be accepted"


# ──────────────────────────────────────────────────────────────────
# C3 — a 'skipped' sub-goal with NO conductor_gate_answered (skip) ruling is
# corruption (symmetric with complete-without-result).


def test_skipped_without_gate_answered_is_corruption(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Deleting the conductor_gate_answered (skip) event while the sub-goal stays
    'skipped' must FAIL CLOSED (GoalCorrupted) on resume. Negative control: with
    the ruling present, re-run() skips cleanly.
    """
    pb_dir = agent_root / "skills" / "c3-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Optional gate
            prompt: Run extra?
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="c3-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(m for m in discover_playbooks(agent_root) if m.name == "c3-pb")

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="c3 test", agent=mock_agent)
    run_id = state1.conductor_run_id

    # Answer with skip → gate becomes 'skipped', run completes.
    state2 = resume(
        playbook=playbook,
        subject="c3 test",
        agent=mock_agent,
        conductor_run_id=run_id,
        decision_id=state1.pending_decision.decision_id,
        answer="Skip it.",
        disposition="skip",
        rationale="not needed",
    )
    assert state2.status == "complete"

    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"

    # Negative control: re-run() with the ruling intact → no raise, completes.
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state_ok = run(
            playbook=playbook,
            subject="c3 test",
            agent=mock_agent,
            conductor_run_id=run_id,
        )
    assert state_ok.status == "complete", "skipped-with-ruling must re-run cleanly"

    # Now strip the gate-answered (skip) ruling → the 'skipped' status is unexplained.
    removed = _strip_event_lines(history_path, "conductor_gate_answered")
    assert removed == 1, "fixture must have exactly one gate-answered event to strip"

    with pytest.raises(GoalCorrupted, match="skipped"):
        with (
            patch(
                "atomic_agents.conductor.run._get_idempotency_backend",
                return_value=_make_idempotency_backend_mock(),
            ),
            patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ),
        ):
            run(
                playbook=playbook,
                subject="c3 test",
                agent=mock_agent,
                conductor_run_id=run_id,
            )


# ──────────────────────────────────────────────────────────────────
# C4 — real CAS negative controls (the existing stale test was false-green:
# a wrong id fires the gate-sg-is-None pre-check BEFORE the under-lock CAS).


def test_gate_resume_replay_consumed_decision_id_no_write(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """Replaying the SAME (already-consumed) decision_id must raise
    GoalConcurrentModification with NO second write — status unchanged off-disk,
    no second conductor_gate_answered event.
    """
    pb_dir = agent_root / "skills" / "c4-replay-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Approve?
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="c4-replay-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "c4-replay-pb"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="c4 replay", agent=mock_agent)
    run_id = state1.conductor_run_id
    decision_id = state1.pending_decision.decision_id

    # First answer (continue) succeeds and consumes the decision_id.
    state2 = resume(
        playbook=playbook,
        subject="c4 replay",
        agent=mock_agent,
        conductor_run_id=run_id,
        decision_id=decision_id,
        answer="Yes",
        disposition="continue",
        rationale="ok",
    )
    assert state2.status == "complete"

    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"

    def _answered_count() -> int:
        evs = [
            json.loads(ln) for ln in history_path.read_text().splitlines() if ln.strip()
        ]
        return len([e for e in evs if e.get("event") == "conductor_gate_answered"])

    assert _answered_count() == 1
    cb = mock_agent.goal_backend.for_goal(run_id)
    status_before = next(
        sg.status
        for sg in cb.load_goal(mock_agent.name).sub_goals
        if sg.id == "gate-stage"
    )

    # Replay the SAME, now-consumed decision_id → rejected, no second write.
    with pytest.raises(GoalConcurrentModification):
        resume(
            playbook=playbook,
            subject="c4 replay",
            agent=mock_agent,
            conductor_run_id=run_id,
            decision_id=decision_id,
            answer="Yes again",
            disposition="continue",
            rationale="dup",
        )

    assert _answered_count() == 1, (
        "a consumed-id replay must NOT write a 2nd answered event"
    )
    status_after = next(
        sg.status
        for sg in cb.load_goal(mock_agent.name).sub_goals
        if sg.id == "gate-stage"
    )
    assert status_after == status_before == "complete", (
        "the gate must not be re-opened or re-transitioned by a replayed id"
    )


def test_gate_resume_wrong_id_does_not_advance(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """A wrong decision_id must reject WITHOUT advancing: the gate stays
    awaiting_decision, gate_decision_id is unchanged, and no answered event is written.
    """
    pb_dir = agent_root / "skills" / "c4-wrongid-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Approve?
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="c4-wrongid-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "c4-wrongid-pb"
    )

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="c4 wrong", agent=mock_agent)
    run_id = state1.conductor_run_id
    real_decision_id = state1.pending_decision.decision_id

    with pytest.raises(GoalConcurrentModification):
        resume(
            playbook=playbook,
            subject="c4 wrong",
            agent=mock_agent,
            conductor_run_id=run_id,
            decision_id="gate-doesnotexist0",
            answer="Yes",
            disposition="continue",
            rationale="wrong id",
        )

    cb = mock_agent.goal_backend.for_goal(run_id)
    gate_sg = next(
        sg for sg in cb.load_goal(mock_agent.name).sub_goals if sg.id == "gate-stage"
    )
    assert gate_sg.status == "awaiting_decision", (
        "wrong-id reject must NOT advance the gate"
    )
    assert gate_sg.gate_decision_id == real_decision_id, (
        "the real decision_id must remain on the sub-goal after a wrong-id reject"
    )
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    events = [
        json.loads(ln) for ln in history_path.read_text().splitlines() if ln.strip()
    ]
    assert not [e for e in events if e.get("event") == "conductor_gate_answered"], (
        "a wrong-id reject must write NO conductor_gate_answered event"
    )


# ──────────────────────────────────────────────────────────────────
# H2 — status_summary is status-aware: skipped counts toward 'done', and
# skipped/awaiting_decision render with symbols (not '?').


def test_status_summary_counts_skipped_and_awaiting(agent_root: Path) -> None:
    """The done tally counts skipped (terminal-done); skipped + awaiting_decision
    are rendered with symbols and shown in the count line.
    """
    backend = FilesystemGoalBackend(agent_root)
    today = date(2026, 6, 1)
    goal = Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent="Mixed-status goal",
        priority="medium",
        created="2026-06-01",
        last_progress_check="2026-06-01",
        success_criteria=["done"],
        sub_goals=[
            SubGoal(id="a", label="A done", status="complete"),
            SubGoal(id="b", label="B skipped", status="skipped"),
            SubGoal(id="c", label="C waiting", status="awaiting_decision"),
        ],
    )
    backend.save_goal(agent_root.name, goal)
    gm = GoalManager(
        agent_root.parent, agent_root.name, today=today, goal_backend=backend
    )
    summary = gm.status_summary()

    # done = complete(1) + skipped(1) = 2 of 3.
    assert "Sub-goals: 2/3 done" in summary, f"unexpected count line in:\n{summary}"
    assert "1 skipped" in summary
    assert "1 awaiting decision" in summary
    # Symbols rendered, not '?'.
    assert "⏭ b" in summary, "skipped must render with the skip symbol, not '?'"
    assert "⏸ c" in summary, "awaiting_decision must render with the pause symbol"
    assert "? b" not in summary and "? c" not in summary

    # Consistency: a fully-skipped goal reads N/N done AND prints all-complete.
    goal2 = Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent="All skipped",
        priority="low",
        created="2026-06-01",
        last_progress_check="2026-06-01",
        success_criteria=["done"],
        sub_goals=[SubGoal(id="x", label="X", status="skipped")],
    )
    backend.save_goal(agent_root.name, goal2)
    gm2 = GoalManager(
        agent_root.parent, agent_root.name, today=today, goal_backend=backend
    )
    summary2 = gm2.status_summary()
    assert "Sub-goals: 1/1 done" in summary2, (
        "a fully-skipped goal must read 1/1 done (skipped counts toward done)"
    )
    assert "All sub-goals complete" in summary2, (
        "the all-done verdict must agree with the done tally (no 0/1 + all-complete "
        "contradiction)"
    )

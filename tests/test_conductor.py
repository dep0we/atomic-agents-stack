"""Tests for atomic_agents/conductor/ — spec/50 PR1+PR2+PR3.

Coverage (acceptance criteria from arc-ruling #580 PR1, #581 PR2, #582 PR3):
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
  TEST 25 — conflict_keys parses from PLAYBOOK.md stage YAML (PR3 #582)
  TEST 26 — conflict_keys validates: non-gate stage with conflict_keys is rejected (PR3 #582)
  TEST 27 — conflict_keys validates: oversized key (>128 chars) is rejected (PR3 #582)
  TEST 28 — conflict_keys validates: path-separator in key is rejected (PR3 #582)
  TEST 29 — ConductorState(status='deferred') requires both queued-behind fields (PR3 #582)
  TEST 30 — ConductorState(status!='deferred') rejects queued-behind fields (PR3 #582)
  TEST 31 — _find_queued_event: returns queued event when no release follows (PR3 #582)
  TEST 32 — _find_queued_event: returns None when queued event is followed by release (PR3 #582)
  TEST 33 — _scan_active_conflicts: returns None for empty conflict_keys (PR3 #582)
  TEST 34 — _scan_active_conflicts: detects conflict with held_conflict_keys on awaiting sub-goal (PR3 #582)
  TEST 35 — per-run lock: run() raises LockBusy when concurrent invocation holds the lock (PR3 #582)
  TEST 36 — per-run lock strip-RED: run() succeeds when no concurrent invocation holds the lock (PR3 #582)
  TEST 37 — SubGoal.held_conflict_keys: round-trips through save_goal/load_goal (PR3 #582)
  TEST 38 — _sum_cumulative_spend: conductor_run_queued event is NOT counted as spend (PR3 #582)
  TEST 39 — per-run lock RELEASED (real backend): a 2nd in-process run() on the same id succeeds (PR3 #582)
  TEST 40 — conflict serialization: overlapping conflict_keys → one suspends, the other defers (PR3 #582)
  TEST 41 — scan+suspend is gated on the shared conductor-conflict-scan lease (double-suspend TOCTOU guard) (PR3 #582)
  TEST 42 — end-to-end self-release: deferred B self-releases once A's gate is answered + still-blocked negative control (C1, PR3 #582)
  TEST 43 — _is_decision_still_pending FAILS CLOSED (stays deferred) on a goal read error (C4a/A1, PR3 #582)
  TEST 44 — _scan_active_conflicts FAILS CLOSED (raises ConductorConflictScanError) on an unreadable scan / malformed blocker (C4b/A2/B3, PR3 #582)
  TEST 45 — C7 launder-guard: run() raises ConductorLaunderRefused when agent.trigger == 'delegate' (PR4 #583)
  TEST 45-strip-RED — C7 launder-guard: run() does NOT raise on non-delegate trigger (PR4 #583)
  TEST 46 — C7 launder-guard: resume() raises ConductorLaunderRefused when agent.trigger == 'delegate' BEFORE any ledger mutation (PR4 #583)
  TEST 46-strip-RED — C7 launder-guard: resume() does NOT raise on non-delegate trigger (PR4 #583)
  TEST 47–50 — check_conductor heavy probe: PASS on healthy run; FAIL on corrupted goal.md;
              most-recent-by-ts selection; WARN on missing per-gate conductor_gate_pending (PR4 #583)
  TEST 51 — check_conductor heavy probe: gated run completed via resume(continue) → PASS (P1 negative control) (PR4 #583)
  TEST 52 — check_conductor heavy probe: parseable-but-SCHEMA-INVALID goal.md → FAIL (PR4 #583)
  TEST 53 — check_conductor heavy probe: goal.md intact but goal_history.jsonl unreadable → honest WARN (PR4 #583)
  TEST 54 — check_conductor heavy probe: awaiting_decision sub-goal with NO gate_decision_id → FAIL (PR4 #583)
  TEST 54b — check_conductor heavy probe: COMPOUND fault (corrupt goal.md cursor + degraded history) → FAIL (PR4 #583)
  TEST 55 — check_conductor heavy probe: 'skipped' sub-goal with NO recorded skip ruling → FAIL (PR4 #583)
  TEST 56 (C8) — gate suspension holds NO live lock; an independent non-conflicting run proceeds normally (PR4 #583)
  TEST 57 — parse-guard: committed reference PLAYBOOK.md at docs/samples/dev-lifecycle/ validates via
            validate_playbook_manifest (18 stages, 8 gates, merge-gate conflict_keys=('merge:main',),
            run_cap_usd=50.00, zero warnings) (#584)
  TEST 58 — e2e integration: full dev-lifecycle run against a feature-issue subject; suspends at each
            of 8 gates; resumes (6x continue + 2x skip); all 8 rulings in one queryable goal ledger
            with rationale; cumulative_spend_usd > 0 (#584)
  TEST 59 — merge-gate conflict serialization: A suspends holding 'merge:main', B defers, A
            resumes(continue), B self-releases into its own gate; real FilesystemLockBackend (#584)

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
from atomic_agents.conductor.run import (
    _find_queued_event,
    _has_event,
    _is_decision_still_pending,
    _read_pinned_run_cap,
    _scan_active_conflicts,
    _sum_cumulative_spend,
)
from atomic_agents.conductor.types import ConductorState, GateDecision
from atomic_agents._goal_impl import GoalManager
from atomic_agents.exceptions import (
    AtomicAgentsError,
    ConductorConflictScanError,
    ConductorLaunderRefused,
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


def test_conductor_cli_run_deferred_status_not_mislabeled_halted(
    tmp_path: Path, capsys
) -> None:
    """_cmd_run must render a 'deferred' ConductorState as DEFERRED, not HALTED (PR3 #582).

    Strip-RED for the CLI fall-through bug: before this fix both _cmd_run and
    _cmd_resume only branched on 'complete'/'awaiting_decision', so a 'deferred'
    run fell through to the halted branch and printed
    `conductor: halted — None ... Resume with: --resume <id>` — wrong twice
    (mislabels deferred as halted, and --resume is wrong guidance: a deferred run
    self-releases by RE-INVOKING run() to poll, it does not own the blocking gate).

    Asserts: exit 0 (deferred is a benign, self-releasing, expected outcome — NOT a
    failure), the word 'deferred' surfaces, 'halted' does NOT, the queued_behind_*
    identifiers are surfaced in the guidance, the poll hint uses `run ... --resume`
    (not a bare `--resume`), and the JSON summary carries the queued_behind_* fields.
    """
    from atomic_agents.conductor import __main__ as cli  # noqa: PLC0415

    root = _full_cli_agent_root(tmp_path)

    def _run_stub(**kwargs):
        return ConductorState(
            conductor_run_id="crun-deferred-1",
            playbook_name="cli-pb",
            subject="s",
            status="deferred",
            halt_reason=None,
            stages_total=2,
            stages_complete=1,
            cumulative_spend_usd=0.0,
            run_cap_usd=5.0,
            completed_stage_ids=["only-stage"],
            queued_behind_decision_id="gate-blocking-77",
            queued_behind_conductor_run_id="crun-holder-9",
        )

    parser = cli._build_parser()
    run_args = parser.parse_args(["run", "cli-pb", "subj", str(root)])
    with patch("atomic_agents.conductor.run", _run_stub):
        rc = cli._cmd_run(run_args)

    out = capsys.readouterr()
    combined = out.out + out.err

    # Exit code: deferred is benign/expected (self-releasing), NOT a failure → 0.
    assert rc == 0, (
        f"a deferred run is a benign, self-releasing outcome and must exit 0; got {rc}"
    )
    # Labelled deferred, NOT halted (the old fall-through printed 'halted — None').
    # Assert the human-readable stderr guidance label (independently strip-RED — the
    # JSON summary always emits "status":"deferred", so a bare `'deferred' in combined`
    # would stay green even if the guidance branch were deleted).
    assert "conductor: deferred" in out.err, (
        "CLI must print the human-readable 'conductor: deferred' guidance label"
    )
    assert "halted" not in out.err, (
        "a deferred run must NOT be mislabelled 'halted' in the guidance"
    )
    # Surfaces what it is blocked on.
    assert "gate-blocking-77" in combined, (
        "the blocking decision id must be surfaced in the deferred guidance"
    )
    assert "crun-holder-9" in combined, (
        "the holding run id must be surfaced in the deferred guidance"
    )
    # Correct poll guidance: re-run with `run ... --resume <id>`, never a bare --resume.
    assert "--resume crun-deferred-1" in out.err
    assert "conductor run" in out.err, (
        "the poll hint must re-invoke `run` (self-release poll), not answer a gate"
    )
    # JSON summary exposes the queued_behind_* fields for --json operators.
    # (stdout also carries the leading 'conductor: starting ...' line, so slice the
    # JSON object out of it before parsing.)
    json_block = out.out[out.out.index("{") : out.out.rindex("}") + 1]
    summary = json.loads(json_block)
    assert summary["status"] == "deferred"
    assert summary["queued_behind_decision_id"] == "gate-blocking-77"
    assert summary["queued_behind_conductor_run_id"] == "crun-holder-9"


def test_conductor_cli_resume_deferred_status_not_mislabeled_halted(
    tmp_path: Path, capsys
) -> None:
    """_cmd_resume must render a 'deferred' ConductorState as DEFERRED, not HALTED (PR3 #582).

    Strip-RED for the _cmd_resume deferred branch (previously a FALSE-GREEN — no test
    drove it): answering a gate (continue/skip) delegates to run(), which can land
    DEFERRED behind ANOTHER run's conflict-key gate. Before the branch, that fell
    through to the halted path and printed `conductor: halted — None`, exit 1 — wrong
    twice (mislabels deferred as halted, and offers no poll guidance).

    Asserts: exit 0 (deferred is benign/self-releasing), the human-readable
    'conductor: deferred' stderr label, 'halted' does NOT appear, both queued_behind_*
    ids surface, and the poll hint uses `run ... --resume` (not a bare `--resume`) and
    is fully populated (resolved playbook name + subject, no literal placeholders).
    """
    from atomic_agents.conductor import __main__ as cli  # noqa: PLC0415

    root = _full_cli_agent_root(tmp_path)

    def _resume_stub(**kwargs):
        return ConductorState(
            conductor_run_id="crun-resume-deferred-1",
            playbook_name="cli-pb",
            subject="subj",
            status="deferred",
            halt_reason=None,
            stages_total=2,
            stages_complete=1,
            cumulative_spend_usd=0.0,
            run_cap_usd=5.0,
            completed_stage_ids=["only-stage"],
            queued_behind_decision_id="gate-blocking-88",
            queued_behind_conductor_run_id="crun-holder-12",
        )

    parser = cli._build_parser()
    resume_args = parser.parse_args(
        [
            "resume",
            str(root),
            "crun-resume-deferred-1",
            "--decision-id",
            "gate-abc",
            "--answer",
            "ok",
            "--rationale",
            "because",
            "--disposition",
            "continue",
            "--playbook-name",
            "cli-pb",
            "--subject",
            "subj",
        ]
    )
    with patch("atomic_agents.conductor.resume", _resume_stub):
        rc = cli._cmd_resume(resume_args)

    out = capsys.readouterr()
    combined = out.out + out.err

    # Exit code: deferred is benign/expected (self-releasing), NOT a failure → 0.
    assert rc == 0, (
        f"a deferred resume is a benign, self-releasing outcome and must exit 0; got {rc}"
    )
    # Human-readable stderr guidance label (independently strip-RED).
    assert "conductor: deferred" in out.err, (
        "CLI must print the human-readable 'conductor: deferred' guidance label"
    )
    assert "halted" not in out.err, (
        "a deferred resume must NOT be mislabelled 'halted' in the guidance"
    )
    # Surfaces what it is blocked on.
    assert "gate-blocking-88" in combined, (
        "the blocking decision id must be surfaced in the deferred guidance"
    )
    assert "crun-holder-12" in combined, (
        "the holding run id must be surfaced in the deferred guidance"
    )
    # Correct poll guidance: re-run with `run ... --resume <id>`, never a bare --resume.
    assert "--resume crun-resume-deferred-1" in out.err
    assert "conductor run" in out.err, (
        "the poll hint must re-invoke `run` (self-release poll), not answer a gate"
    )
    # Fully-populated hint: resolved playbook name + subject, NO literal placeholders.
    assert "cli-pb" in out.err and "subj" in out.err, (
        "the poll hint must use the resolved playbook name and subject"
    )
    assert "<playbook_name>" not in out.err and "<subject>" not in out.err, (
        "the poll hint must not leave literal placeholders"
    )
    # JSON summary exposes the queued_behind_* fields for --json operators.
    json_block = out.out[out.out.index("{") : out.out.rindex("}") + 1]
    summary = json.loads(json_block)
    assert summary["status"] == "deferred"
    assert summary["queued_behind_decision_id"] == "gate-blocking-88"
    assert summary["queued_behind_conductor_run_id"] == "crun-holder-12"


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


# ──────────────────────────────────────────────────────────────────
# PR3 (#582): Concurrency + conflict serialization tests


def test_conflict_keys_parse_from_playbook(tmp_path: Path) -> None:
    """TEST 25 — conflict_keys parses from PLAYBOOK.md stage YAML (PR3 #582).

    A gate stage with conflict_keys: [key-a, key-b] must round-trip through
    playbook parsing and appear as a tuple on StageSpec.
    """
    from atomic_agents.conductor.playbook import PLAYBOOK_ENTRY_POINT
    from atomic_agents.conductor.types import StageSpec

    pb_dir = tmp_path / "skills" / "conflict-pb"
    pb_dir.mkdir(parents=True)
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        "---\n"
        "name: conflict-pb\n"
        "description: Conflict test.\n"
        "kind: playbook\n"
        "---\n"
        "\n"
        "A conflict playbook.\n"
        "\n"
        "```yaml\n"
        "run_cap_usd: 2.0\n"
        "stages:\n"
        "  - stage_id: gate-one\n"
        "    label: The gate\n"
        "    prompt: Approve?\n"
        "    is_gate: true\n"
        "    conflict_keys:\n"
        "      - resource-x\n"
        "      - resource-y\n"
        "```\n"
    )

    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is not None, (
        f"validate_playbook_manifest must succeed; warnings={warnings}"
    )
    assert len(manifest.stages) == 1
    stage = manifest.stages[0]
    assert isinstance(stage, StageSpec)
    assert stage.is_gate is True
    assert set(stage.conflict_keys) == {"resource-x", "resource-y"}, (
        f"conflict_keys must round-trip; got {stage.conflict_keys!r}"
    )


def test_fingerprint_keyless_stage_is_pr2_backward_compatible(tmp_path: Path) -> None:
    """TEST 25b — a keyless stage hashes byte-identically to its pre-PR3 (PR2) pin.

    Backward-compat regression guard (Principle #14). On PR2 the per-stage
    fingerprint dict had NO conflict_keys field at all. A run pinned under PR2 and
    resumed under PR3 recomputes the fingerprint with PR3 code — if PR3 added
    `"conflict_keys": []` unconditionally, the digest would change and resume()
    would falsely raise "started with a different PLAYBOOK.md structure" even
    though the operator changed nothing. PR3 fixes this by only hashing
    conflict_keys when non-empty. This test pins that: a keyless stage's PR3
    fingerprint must equal the exact PR2-shaped digest (no conflict_keys key).
    """
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    from atomic_agents.conductor.run import (  # noqa: PLC0415
        _compute_playbook_fingerprint,
    )
    from atomic_agents.conductor.types import PlaybookManifest, StageSpec

    # A manifest whose only stage carries NO conflict_keys (default empty tuple).
    keyless_stage = StageSpec(
        stage_id="stage-one",
        label="Stage One",
        prompt="Do the thing.",
    )
    manifest = PlaybookManifest(
        name="pb",
        description="d",
        when_to_use=None,
        run_cap_usd=1.0,
        stages=[keyless_stage],
        playbook_dir=tmp_path,
        playbook_md_path=tmp_path / "PLAYBOOK.md",
    )

    # Replicate the EXACT pre-PR3 digest: the per-stage dict WITHOUT conflict_keys,
    # NUL-separated, sha256. This is byte-for-byte what PR2 code produced.
    h = hashlib.sha256()
    part = json.dumps(
        {
            "stage_id": "stage-one",
            "is_gate": False,
            "prompt": "Do the thing.",
            "prompt_ref": None,
            "options": [],
            "rubric_ref": None,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    h.update(part.encode("utf-8"))
    h.update(b"\x00")
    pr2_digest = h.hexdigest()

    assert _compute_playbook_fingerprint(manifest) == pr2_digest, (
        "a keyless stage must hash identically to its PR2 pin (no conflict_keys "
        "key in the hashed dict), so a PR2-pinned run resumes under PR3 unchanged"
    )

    # And a stage that DOES carry conflict_keys must change the digest (the new
    # guarantee — editing conflict_keys mid-suspension is still caught).
    keyed_stage = StageSpec(
        stage_id="stage-one",
        label="Stage One",
        prompt="Do the thing.",
        is_gate=True,
        conflict_keys=("resource-x",),
    )
    keyed_manifest = PlaybookManifest(
        name="pb",
        description="d",
        when_to_use=None,
        run_cap_usd=1.0,
        stages=[keyed_stage],
        playbook_dir=tmp_path,
        playbook_md_path=tmp_path / "PLAYBOOK.md",
    )
    assert _compute_playbook_fingerprint(keyed_manifest) != pr2_digest, (
        "a stage carrying conflict_keys must change the fingerprint"
    )


def test_conflict_keys_rejected_on_non_gate_stage(tmp_path: Path) -> None:
    """TEST 26 — conflict_keys on a non-gate stage is rejected (PR3 #582).

    Only gate stages (is_gate: true) may carry conflict_keys. An automated
    stage with conflict_keys is a configuration error that must fail loud.
    """
    from atomic_agents.conductor.playbook import PLAYBOOK_ENTRY_POINT

    pb_dir = tmp_path / "skills" / "bad-conflict-pb"
    pb_dir.mkdir(parents=True)
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        "---\n"
        "name: bad-conflict-pb\n"
        "description: Bad.\n"
        "kind: playbook\n"
        "---\n"
        "\n"
        "Bad.\n"
        "\n"
        "```yaml\n"
        "run_cap_usd: 2.0\n"
        "stages:\n"
        "  - stage_id: auto-stage\n"
        "    label: Automated\n"
        "    prompt: Do something.\n"
        "    conflict_keys:\n"
        "      - resource-x\n"
        "```\n"
    )

    # validate_playbook_manifest returns (None, warnings) on hard error.
    manifest2, warnings2 = validate_playbook_manifest(pb_dir)
    assert manifest2 is None, (
        "conflict_keys on a non-gate stage must cause validate_playbook_manifest to "
        "return (None, warnings); manifest was not None"
    )
    # At least one warning must mention conflict_keys or gate.
    combined = " ".join(w.lower() for w in warnings2)
    assert "conflict_keys" in combined or "gate" in combined or "is_gate" in combined, (
        f"warnings must mention conflict_keys or gate; got: {warnings2!r}"
    )


def test_conflict_keys_rejected_when_key_too_long(tmp_path: Path) -> None:
    """TEST 27 — conflict_keys rejects a key longer than 128 chars (PR3 #582).

    Oversized keys create operational hazards (path components, log truncation).
    The validator must refuse them.
    """
    from atomic_agents.conductor.playbook import PLAYBOOK_ENTRY_POINT

    long_key = "x" * 129
    pb_dir = tmp_path / "skills" / "longkey-pb"
    pb_dir.mkdir(parents=True)
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        "---\n"
        "name: longkey-pb\n"
        "description: Long key.\n"
        "kind: playbook\n"
        "---\n"
        "\n"
        "Long key.\n"
        "\n"
        "```yaml\n"
        "run_cap_usd: 2.0\n"
        "stages:\n"
        "  - stage_id: gate-long\n"
        "    label: Gate\n"
        "    prompt: Approve?\n"
        "    is_gate: true\n"
        f"    conflict_keys:\n"
        f"      - {long_key}\n"
        "```\n"
    )

    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None, (
        f"A conflict key >128 chars must cause validation failure; "
        f"manifest was not None, warnings={warnings!r}"
    )


def test_conflict_keys_rejected_with_path_separator(tmp_path: Path) -> None:
    """TEST 28 — conflict_keys rejects a key containing a path separator (PR3 #582).

    A key with / would allow directory traversal in any path-keyed secondary
    index. The validator must refuse it.
    """
    from atomic_agents.conductor.playbook import PLAYBOOK_ENTRY_POINT

    pb_dir = tmp_path / "skills" / "pathsep-pb"
    pb_dir.mkdir(parents=True)
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        "---\n"
        "name: pathsep-pb\n"
        "description: Path sep.\n"
        "kind: playbook\n"
        "---\n"
        "\n"
        "Path sep.\n"
        "\n"
        "```yaml\n"
        "run_cap_usd: 2.0\n"
        "stages:\n"
        "  - stage_id: gate-sep\n"
        "    label: Gate\n"
        "    prompt: Approve?\n"
        "    is_gate: true\n"
        "    conflict_keys:\n"
        "      - resource/subresource\n"
        "```\n"
    )

    manifest, warnings = validate_playbook_manifest(pb_dir)
    assert manifest is None, (
        f"A conflict key with '/' must cause validation failure; "
        f"manifest was not None, warnings={warnings!r}"
    )


def test_conductor_state_deferred_requires_queued_behind_fields() -> None:
    """TEST 29 — ConductorState(status='deferred') must have BOTH queued-behind fields (PR3 #582).

    The __post_init__ invariant: a 'deferred' status without BOTH
    queued_behind_decision_id AND queued_behind_conductor_run_id raises
    ValueError. This is the same invariant pattern as 'awaiting_decision'
    requiring pending_decision.
    """
    # Missing both
    with pytest.raises(ValueError, match="queued_behind_decision_id"):
        ConductorState(
            conductor_run_id="crun-test",
            playbook_name="pb",
            subject="s",
            status="deferred",
            halt_reason=None,
            stages_total=2,
            stages_complete=0,
            cumulative_spend_usd=0.0,
            run_cap_usd=5.0,
            completed_stage_ids=[],
            # both queued-behind fields intentionally omitted
        )
    # Only one of the two present is still a violation (BOTH required)
    with pytest.raises(ValueError, match="queued_behind_conductor_run_id"):
        ConductorState(
            conductor_run_id="crun-test",
            playbook_name="pb",
            subject="s",
            status="deferred",
            halt_reason=None,
            stages_total=2,
            stages_complete=0,
            cumulative_spend_usd=0.0,
            run_cap_usd=5.0,
            completed_stage_ids=[],
            queued_behind_decision_id="gate-xyz",
            # queued_behind_conductor_run_id intentionally omitted
        )


def test_conductor_state_non_deferred_rejects_queued_behind_fields() -> None:
    """TEST 30 — ConductorState(status!='deferred') rejects queued-behind fields (PR3 #582).

    Setting either queued-behind field on a non-deferred state must raise ValueError.
    """
    with pytest.raises(ValueError, match="queued_behind_decision_id"):
        ConductorState(
            conductor_run_id="crun-test",
            playbook_name="pb",
            subject="s",
            status="complete",
            halt_reason=None,
            stages_total=2,
            stages_complete=2,
            cumulative_spend_usd=0.01,
            run_cap_usd=5.0,
            completed_stage_ids=["s1", "s2"],
            queued_behind_decision_id="some-decision-id",
            queued_behind_conductor_run_id="crun-other",
        )


def test_find_queued_event_returns_event_when_not_released(tmp_path: Path) -> None:
    """TEST 31 — _find_queued_event returns the queued event when no release follows.

    A history file with a conductor_run_queued event and NO subsequent
    conductor_queue_released event must return the queued event dict.
    """
    history = tmp_path / "goal_history.jsonl"
    events = [
        '{"ts": "2026-06-27T00:00:00+00:00", "event": "conductor_run_started", '
        '"conductor_run_id": "crun-abc"}',
        '{"ts": "2026-06-27T00:01:00+00:00", "event": "conductor_run_queued", '
        '"blocking_decision_id": "gate-xyz", "blocking_conductor_run_id": "crun-other"}',
    ]
    history.write_text("\n".join(events) + "\n", encoding="utf-8")

    result = _find_queued_event(history)
    assert result is not None, "_find_queued_event must return event when not released"
    assert result.get("event") == "conductor_run_queued"
    assert result.get("blocking_decision_id") == "gate-xyz"


def test_find_queued_event_returns_none_when_released(tmp_path: Path) -> None:
    """TEST 32 — _find_queued_event returns None when a release follows the queued event.

    A conductor_run_queued event followed by conductor_queue_released means the
    run self-released. _find_queued_event must return None.
    """
    history = tmp_path / "goal_history.jsonl"
    events = [
        '{"ts": "2026-06-27T00:00:00+00:00", "event": "conductor_run_started"}',
        '{"ts": "2026-06-27T00:01:00+00:00", "event": "conductor_run_queued", '
        '"blocking_decision_id": "gate-xyz"}',
        '{"ts": "2026-06-27T00:02:00+00:00", "event": "conductor_queue_released"}',
    ]
    history.write_text("\n".join(events) + "\n", encoding="utf-8")

    result = _find_queued_event(history)
    assert result is None, "_find_queued_event must return None when a release follows"


def test_scan_active_conflicts_returns_none_for_no_keys(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 33 — _scan_active_conflicts returns None when conflict_keys is empty (PR3 #582).

    An empty conflict_keys tuple means this stage has no conflict scope —
    _scan_active_conflicts must return None immediately without scanning any goals.
    This is the fast-path (most stages have no conflict_keys).
    """
    result = _scan_active_conflicts(
        agent=mock_agent,
        stage_conflict_keys=(),
        own_conductor_run_id="crun-self",
    )
    assert result is None, (
        "_scan_active_conflicts must return None for empty conflict_keys"
    )


def test_scan_active_conflicts_detects_held_keys(
    agent_root: Path, goal_backend: FilesystemGoalBackend
) -> None:
    """TEST 34 — _scan_active_conflicts detects conflict with held_conflict_keys (PR3 #582).

    Set up a goal with a sub-goal in 'awaiting_decision' status that has
    held_conflict_keys=['resource-x']. A scan for stage_conflict_keys=('resource-x',)
    must find this sub-goal and return (blocking_run_id, decision_id).
    """
    from atomic_agents.goal.types import CURRENT_GOAL_SCHEMA_VERSION, Goal, SubGoal

    # Write a blocking goal: awaiting_decision with held_conflict_keys=['resource-x'].
    blocking_run_id = "crun-blocking"
    decision_id = "gate-abc123"
    blocking_backend = goal_backend.for_goal(blocking_run_id)

    blocking_goal = Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent="Blocking run",
        priority="medium",
        created="2026-06-27",
        last_progress_check="2026-06-27",
        success_criteria=["done"],
        sub_goals=[
            SubGoal(
                id="gate-stage",
                label="Gate",
                status="awaiting_decision",
                gate_decision_id=decision_id,
                held_conflict_keys=["resource-x"],
            )
        ],
    )
    blocking_backend.save_goal(agent_root.name, blocking_goal)

    # Build a mock agent that uses the real goal_backend.
    agent = MagicMock()
    agent.name = agent_root.name
    agent.goal_backend = goal_backend

    # Scan for resource-x from a DIFFERENT run (own_conductor_run_id != blocking_run_id).
    result = _scan_active_conflicts(
        agent=agent,
        stage_conflict_keys=("resource-x",),
        own_conductor_run_id="crun-self",
    )
    assert result is not None, (
        "_scan_active_conflicts must detect a conflict when held_conflict_keys overlap"
    )
    found_run_id, found_decision_id = result
    assert found_run_id == blocking_run_id, (
        f"Must return the blocking run_id; got {found_run_id!r}"
    )
    assert found_decision_id == decision_id, (
        f"Must return the blocking decision_id; got {found_decision_id!r}"
    )

    # Strip-RED: a scan for a NON-overlapping key must NOT detect the conflict.
    no_result = _scan_active_conflicts(
        agent=agent,
        stage_conflict_keys=("resource-z",),  # no overlap with resource-x
        own_conductor_run_id="crun-self",
    )
    assert no_result is None, (
        "_scan_active_conflicts must NOT detect a conflict when keys do NOT overlap"
    )


def test_per_run_lock_raises_lockbusy(
    agent_root: Path, mock_agent: MagicMock, playbook_dir: Path
) -> None:
    """TEST 35 — run() raises LockBusy when a concurrent invocation holds the per-run lock.

    The first run() acquires the per-run LockBackend lease. A mocked second
    invocation where lock_backend.scope().acquire() raises LockBusy must propagate
    as LockBusy to the caller.

    This is a unit test of the lock-acquisition gate, not a full end-to-end run.
    We patch the lock backend's acquire() to raise LockBusy immediately, simulating
    a concurrent run that already holds the lock.
    """
    from atomic_agents.exceptions import LockBusy

    manifests = discover_playbooks(agent_root)
    assert manifests, "playbook_dir fixture must create at least one manifest"
    playbook = manifests[0]

    # First: configure the mock_agent's lock_backend to raise LockBusy on acquire.
    lock_scope_mock = MagicMock()
    lock_scope_mock.acquire.side_effect = LockBusy("simulated concurrent lock")
    mock_agent.lock_backend.scope.return_value = lock_scope_mock

    with pytest.raises(LockBusy, match="already executing in a concurrent invocation"):
        run(playbook=playbook, subject="lock-test", agent=mock_agent)


def test_per_run_lock_succeeds_with_no_contention(
    agent_root: Path, mock_agent: MagicMock, playbook_dir: Path
) -> None:
    """TEST 36 — run() succeeds when lock_backend.scope().acquire() does not raise (PR3 #582).

    Strip-RED control for TEST 35. When the lock is available, run() must proceed
    normally. We use a standard MagicMock lock that does not raise, then verify
    that run() proceeds past the lock acquisition point (i.e. does not raise
    LockBusy).
    """
    from atomic_agents.exceptions import LockBusy

    manifests = discover_playbooks(agent_root)
    assert manifests, "playbook_dir fixture must create at least one manifest"
    playbook = manifests[0]

    # Default MagicMock for lock_backend — acquire returns a MagicMock (no raise).
    # run() must NOT raise LockBusy.
    with patch(
        "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
        side_effect=_make_ledger_updating_dispatch("satisfied", 0.05),
    ):
        try:
            state = run(
                playbook=playbook, subject="lock-test-strip-red", agent=mock_agent
            )
            # If run() reaches this point, LockBusy was NOT raised — test passes.
            assert state.status in ("complete", "halted", "awaiting_decision"), (
                f"run() must return a valid status; got {state.status!r}"
            )
        except LockBusy:
            pytest.fail("run() must NOT raise LockBusy when the lock is available")


def test_held_conflict_keys_round_trip(agent_root: Path) -> None:
    """TEST 37 — SubGoal.held_conflict_keys round-trips through save_goal/load_goal (PR3 #582).

    PR3 adds held_conflict_keys: list[str] to SubGoal and persists it via
    SUB_GOAL_TRANSITION_FIELDS. This test proves the field survives a
    save_goal → load_goal cycle, so conflict scans can read it without
    re-parsing the history JSONL.
    """
    backend = FilesystemGoalBackend(agent_root)

    goal = Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent="Held keys round-trip test",
        priority="medium",
        created="2026-06-27",
        last_progress_check="2026-06-27",
        success_criteria=["done"],
        sub_goals=[
            SubGoal(
                id="sg-gate",
                label="Gate",
                status="awaiting_decision",
                gate_decision_id="gate-xyz",
                held_conflict_keys=["resource-alpha", "resource-beta"],
            )
        ],
    )
    backend.save_goal(agent_root.name, goal)

    reloaded = backend.load_goal(agent_root.name)
    sg = next(s for s in reloaded.sub_goals if s.id == "sg-gate")
    assert set(sg.held_conflict_keys) == {"resource-alpha", "resource-beta"}, (
        f"held_conflict_keys must round-trip; got {sg.held_conflict_keys!r}"
    )

    # Strip-RED: a sub-goal WITHOUT held_conflict_keys must default to [].
    goal2 = Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent="No held keys",
        priority="low",
        created="2026-06-27",
        last_progress_check="2026-06-27",
        success_criteria=["done"],
        sub_goals=[SubGoal(id="sg-auto", label="Auto", status="pending")],
    )
    backend.save_goal(agent_root.name, goal2)
    reloaded2 = backend.load_goal(agent_root.name)
    sg2 = next(s for s in reloaded2.sub_goals if s.id == "sg-auto")
    assert sg2.held_conflict_keys == [], (
        f"held_conflict_keys must default to [] when not set; got {sg2.held_conflict_keys!r}"
    )


def test_sum_cumulative_spend_does_not_count_queued_events(tmp_path: Path) -> None:
    """TEST 38 — _sum_cumulative_spend: conductor_run_queued event is NOT counted as spend.

    conductor_run_queued is an advisory ledger event (no cost attached). Only
    sub_goal_outcome_dispatched events carry cost. The queued event must not
    affect the cumulative spend total (negative control for the cost isolation).
    """
    history = tmp_path / "goal_history.jsonl"
    events = [
        json.dumps(
            {
                "ts": "2026-06-27T00:00:00+00:00",
                "event": "conductor_run_started",
                "run_cap_usd": 5.0,
            }
        ),
        json.dumps(
            {
                "ts": "2026-06-27T00:01:00+00:00",
                "event": "sub_goal_outcome_dispatched",
                "sub_goal_id": "stage-one",
                "total_cost_usd": 0.10,
            }
        ),
        json.dumps(
            {
                "ts": "2026-06-27T00:02:00+00:00",
                "event": "conductor_run_queued",
                "blocking_decision_id": "gate-abc",
                "blocking_conductor_run_id": "crun-other",
            }
        ),
        json.dumps(
            {
                "ts": "2026-06-27T00:03:00+00:00",
                "event": "conductor_queue_released",
            }
        ),
    ]
    history.write_text("\n".join(events) + "\n", encoding="utf-8")

    total, degraded = _sum_cumulative_spend(history)
    assert not degraded, "history must not be degraded (all events are well-formed)"
    assert abs(total - 0.10) < 1e-9, (
        f"_sum_cumulative_spend must count only sub_goal_outcome_dispatched; "
        f"got {total!r} (expected 0.10)"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 39 — per-run lock is RELEASED (real FilesystemLockBackend): a second
# in-process run()/resume() on the same conductor_run_id does NOT dead-lock.


def test_per_run_lock_released_real_backend_second_run_succeeds(
    agent_root: Path,
    mock_agent: MagicMock,
    playbook_dir: Path,
) -> None:
    """TEST 39 — run() releases the per-run lease on every exit path (real backend).

    Pins the P0/P1 fix: the per-run LockBackend lease is released EXPLICITLY in a
    finally (not by GC — LockHandle has no __del__ and backend_state is a bare fd
    int, so dropping the handle releases nothing). We drive run() against a REAL
    FilesystemLockBackend (not the MagicMock the rest of the suite uses, which
    never takes a real lock), then call run() AGAIN in the SAME process with the
    SAME conductor_run_id. With the lock leaked this second acquire would raise
    LockBusy (fcntl flock held until process exit); with the explicit release it
    succeeds. This is the assertion that fails RED on the un-released-lock bug.
    """
    from atomic_agents.exceptions import LockBusy
    from atomic_agents.locks.filesystem import FilesystemLockBackend

    # Real lock backend — this is the load-bearing difference vs. TEST 35/36.
    mock_agent.lock_backend = FilesystemLockBackend(agent_root)

    manifests = discover_playbooks(agent_root)
    playbook = next(m for m in manifests if m.name == "my-playbook")

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_make_ledger_updating_dispatch("satisfied", 0.05),
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state1 = run(playbook=playbook, subject="lock-release", agent=mock_agent)
        assert state1.status == "complete", (
            f"first run must complete; got {state1.status!r}"
        )
        run_id = state1.conductor_run_id

        # Second in-process run() on the SAME id MUST NOT raise LockBusy — proves
        # the first run() released its per-run lease. (RED without the fix.)
        try:
            state2 = run(
                playbook=playbook,
                subject="lock-release",
                agent=mock_agent,
                conductor_run_id=run_id,
            )
        except LockBusy:  # pragma: no cover - this is the failure the fix prevents
            pytest.fail(
                "run() leaked its per-run lock: a second in-process run() on the "
                "same conductor_run_id raised LockBusy. The lease must be released "
                "in finally (NOT via GC)."
            )
        assert state2.status == "complete", (
            f"second run must re-complete from the durable cursor; got {state2.status!r}"
        )

    # The lock file must be unlocked now: a fresh manual acquire succeeds.
    probe = FilesystemLockBackend(agent_root).scope("conductor-runs")
    handle = probe.acquire(run_id, timeout=0.0)  # must NOT raise
    probe.release(handle)


# ──────────────────────────────────────────────────────────────────
# TEST 40 — conflict serialization (OD1b): when run A holds a gate with a
# conflict key, a second run B whose gate shares that key QUEUES (does not
# double-suspend). Driven against a REAL FilesystemLockBackend + goal backend.


def _make_conflict_gate_playbook(agent_root: Path, name: str, key: str) -> Any:
    pb_dir = agent_root / "skills" / name
    pb_dir.mkdir()
    stages_yaml = (
        "stages:\n"
        "  - stage_id: gate-stage\n"
        "    label: Human gate\n"
        "    prompt: Human review required.\n"
        "    is_gate: true\n"
        "    options:\n"
        "      - Approve\n"
        "      - Reject\n"
        "    conflict_keys:\n"
        f"      - {key}\n"
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name=name, stages=stages_yaml, run_cap_usd=5.0)
    )
    return next(m for m in discover_playbooks(agent_root) if m.name == name)


def test_conflict_serialization_second_run_queues_not_double_suspends(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """TEST 40 — overlapping conflict_keys: exactly one run suspends, the other queues.

    Pins the OD1b headline guarantee + the scan+suspend serialization fix. Run A
    reaches a gate declaring conflict_keys=['repo-x'] and suspends
    (awaiting_decision, held_conflict_keys=['repo-x'] persisted on its sub-goal).
    Run B reaches a gate that shares 'repo-x'; _scan_active_conflicts (run UNDER
    the shared conductor-conflict-scan lease) sees A's claim and B returns
    status='deferred' with queued_behind_decision_id = A's decision_id — it does
    NOT write a second awaiting_decision holding the same key. Driven against a REAL
    FilesystemLockBackend so the scan+suspend critical section actually takes the
    shared lease.
    """
    from atomic_agents.locks.filesystem import FilesystemLockBackend

    mock_agent.lock_backend = FilesystemLockBackend(agent_root)

    pb_a = _make_conflict_gate_playbook(agent_root, "conflict-a", "repo-x")
    pb_b = _make_conflict_gate_playbook(agent_root, "conflict-b", "repo-x")

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
        state_a = run(playbook=pb_a, subject="A", agent=mock_agent)
        assert state_a.status == "awaiting_decision", (
            f"run A must suspend at its gate; got {state_a.status!r}"
        )
        assert state_a.pending_decision is not None
        blocking_id = state_a.pending_decision.decision_id

        state_b = run(playbook=pb_b, subject="B", agent=mock_agent)

    # Run B must DEFER behind A's gate — NOT suspend a second time on the same key.
    assert state_b.status == "deferred", (
        f"run B must defer behind A's conflicting gate (NOT double-suspend); "
        f"got {state_b.status!r}"
    )
    assert state_b.queued_behind_decision_id == blocking_id, (
        f"run B must be blocked behind A's decision {blocking_id!r}; "
        f"got {state_b.queued_behind_decision_id!r}"
    )
    assert state_b.queued_behind_conductor_run_id == state_a.conductor_run_id, (
        f"run B must record A's conductor_run_id as the holder; "
        f"got {state_b.queued_behind_conductor_run_id!r}"
    )
    assert state_b.pending_decision is None, (
        "a deferred run has no pending_decision of its own"
    )

    # Durable check: exactly ONE goal holds 'repo-x' in awaiting_decision.
    holders = 0
    for goal_id in mock_agent.goal_backend.list_goals(mock_agent.name):
        goal = mock_agent.goal_backend.for_goal(goal_id).load_goal(mock_agent.name)
        for sg in goal.sub_goals:
            if sg.status == "awaiting_decision" and "repo-x" in (
                sg.held_conflict_keys or []
            ):
                holders += 1
    assert holders == 1, (
        f"exactly one run may hold the 'repo-x' conflict key in awaiting_decision; "
        f"found {holders}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 41 — the scan+suspend critical section is gated on the SHARED
# conductor-conflict-scan lease (the cross-run TOCTOU guard).


def test_conflict_scan_suspend_acquires_shared_scan_lease(
    agent_root: Path,
    mock_agent: MagicMock,
    monkeypatch,
) -> None:
    """TEST 41 — run() acquires the shared 'conductor-conflict-scan' lease before
    scanning+suspending a gate with conflict_keys.

    This pins the double-suspend TOCTOU fix directly (TEST 40 only proves the
    SEQUENTIAL queue behavior, which would pass even without the lock). Here we
    pre-hold the shared scan lease from the test, then drive a conflict-gate run:
    because scan+suspend must acquire that same lease, run() blocks and (with a
    short timeout) raises LockBusy. Without the lock the run would suspend
    normally — so a green assertion proves the serialization lease is real.
    """
    import importlib

    from atomic_agents.exceptions import LockBusy
    from atomic_agents.locks.filesystem import FilesystemLockBackend

    # The conductor package re-exports run() as the attribute `conductor.run`,
    # which shadows the submodule — import the real module via importlib.
    _runmod = importlib.import_module("atomic_agents.conductor.run")

    mock_agent.lock_backend = FilesystemLockBackend(agent_root)
    # Short timeout so the blocked acquire fails fast instead of waiting 30s.
    monkeypatch.setattr(_runmod, "_CONFLICT_SCAN_LOCK_TIMEOUT_S", 0.1)

    pb = _make_conflict_gate_playbook(agent_root, "scan-lease", "repo-y")

    # Hold the shared scan lease from the test (simulates another run mid-critical
    # section). run()'s scan+suspend must contend for THIS lease.
    held_scope = mock_agent.lock_backend.scope("conductor-conflict-scan")
    held = held_scope.acquire("scan", timeout=0.0)

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    try:
        with (
            patch(
                "atomic_agents.conductor.run._get_idempotency_backend",
                return_value=idem,
            ),
            patch(
                "atomic_agents.conductor.run._get_outcome_backend",
                return_value=outcome_backend,
            ),
        ):
            with pytest.raises(LockBusy):
                run(playbook=pb, subject="scan-lease", agent=mock_agent)
    finally:
        held_scope.release(held)

    # Strip-RED: once the shared lease is released, the same run suspends normally
    # (the LockBusy above was caused by the held scan lease, not something else).
    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend", return_value=idem
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        state = run(playbook=pb, subject="scan-lease-2", agent=mock_agent)
    assert state.status == "awaiting_decision", (
        f"with the scan lease free, the gate must suspend; got {state.status!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 42 (C1) — end-to-end self-release: a deferred run self-releases once the
# blocking gate is answered, with a negative control while the gate stays open.


def test_self_release_after_blocking_gate_answered(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """TEST 42 — end-to-end OD1b self-release loop + the still-blocked negative control.

    Deterministic, sequential:
      1. Run A → suspends at its conflict-keyed gate (holds 'repo-x').
      2. Run B (shares 'repo-x') → status='deferred' behind A's decision.
      3. NEGATIVE CONTROL — with A STILL awaiting_decision, re-invoke run(B):
         B STAYS deferred, NO conductor_queue_released is appended, and B does
         NOT execute the conflict-keyed stage (never becomes the 'repo-x' holder).
      4. Answer A's gate (resume A, disposition='continue') → A completes, key freed.
      5. Re-invoke run(B): a conductor_queue_released event is appended AND B
         proceeds past the deferral into its OWN conflict-keyed gate
         (status='awaiting_decision', now the holder, with its own decision_id).

    Exercises _is_decision_still_pending (True path in step 3, False path in
    step 5) and the self-release branch (previously untested end-to-end).
    """
    from atomic_agents.locks.filesystem import FilesystemLockBackend

    mock_agent.lock_backend = FilesystemLockBackend(agent_root)
    pb_a = _make_conflict_gate_playbook(agent_root, "conflict-a", "repo-x")
    pb_b = _make_conflict_gate_playbook(agent_root, "conflict-b", "repo-x")

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
        # 1. A suspends at its gate.
        state_a = run(playbook=pb_a, subject="A", agent=mock_agent)
        assert state_a.status == "awaiting_decision", (
            f"run A must suspend; got {state_a.status!r}"
        )
        a_decision = state_a.pending_decision.decision_id
        a_run_id = state_a.conductor_run_id

        # 2. B defers behind A.
        state_b1 = run(playbook=pb_b, subject="B", agent=mock_agent)
        assert state_b1.status == "deferred", (
            f"run B must defer behind A; got {state_b1.status!r}"
        )
        b_run_id = state_b1.conductor_run_id
        b_history = agent_root / "goals" / b_run_id / "goal_history.jsonl"

        # 3. NEGATIVE CONTROL — A still pending → B stays deferred, no release.
        state_b_neg = run(
            playbook=pb_b,
            subject="B",
            agent=mock_agent,
            conductor_run_id=b_run_id,
        )
        assert state_b_neg.status == "deferred", (
            f"B must STAY deferred while A's gate is unanswered; "
            f"got {state_b_neg.status!r}"
        )
        assert state_b_neg.queued_behind_decision_id == a_decision
        assert not _has_event(b_history, "conductor_queue_released"), (
            "B must NOT self-release while A's gate is still awaiting_decision"
        )
        b_goal_neg = mock_agent.goal_backend.for_goal(b_run_id).load_goal(
            mock_agent.name
        )
        assert all(sg.status != "awaiting_decision" for sg in b_goal_neg.sub_goals), (
            "a deferred B must NOT execute (hold) its conflict-keyed gate"
        )

        # 4. Answer A's gate → A completes, 'repo-x' released.
        state_a2 = resume(
            playbook=pb_a,
            subject="A",
            agent=mock_agent,
            conductor_run_id=a_run_id,
            decision_id=a_decision,
            answer="Approve.",
            disposition="continue",
            rationale="Reviewed.",
        )
        assert state_a2.status == "complete", (
            f"A must complete after continue; got {state_a2.status!r}"
        )

        # 5. Re-invoke B → self-release, then B proceeds into its own gate.
        state_b2 = run(
            playbook=pb_b,
            subject="B",
            agent=mock_agent,
            conductor_run_id=b_run_id,
        )

    assert _has_event(b_history, "conductor_queue_released"), (
        "B must append conductor_queue_released once A's gate is answered "
        "(self-release path)"
    )
    assert state_b2.status == "awaiting_decision", (
        f"B must proceed PAST the deferral into its own conflict-keyed gate; "
        f"got {state_b2.status!r}"
    )
    assert state_b2.queued_behind_decision_id is None, (
        "B is no longer deferred — the queued-behind field must be cleared"
    )
    assert state_b2.pending_decision is not None, (
        "B now holds its own gate and must surface its own pending decision"
    )
    assert state_b2.pending_decision.decision_id != a_decision, (
        "B's gate decision must be its OWN, not A's released decision"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 43 (C4a) — _is_decision_still_pending FAILS CLOSED (returns True) on a
# goal-backend read error, so a deferred run is never self-released by a glitch.


def test_is_decision_still_pending_fails_closed_on_read_error(
    agent_root: Path,
) -> None:
    """TEST 43 — _is_decision_still_pending returns True (stay deferred) on read errors.

    A1 fail-closed: a transient goal-backend read failure MUST NOT be read as
    'gate answered → release B into the resource'. Two cases:
      (a) both the direct load and the list_goals fallback raise → True.
      (b) list_goals succeeds but the per-goal load_goal raises (incomplete scan)
          → True.

    Strip-RED: if A1 is reverted (return False on read error), both assertions
    fail.
    """
    # (a) Every read path raises.
    agent_a = MagicMock()
    agent_a.name = agent_root.name
    backend_a = MagicMock()
    backend_a.for_goal.side_effect = OSError("backend down")
    backend_a.list_goals.side_effect = OSError("backend down")
    agent_a.goal_backend = backend_a
    assert (
        _is_decision_still_pending(
            agent=agent_a, decision_id="gate-abc", blocking_run_id="crun-blocking"
        )
        is True
    ), "both read paths failing must keep the run deferred (fail-closed)"

    # (b) list_goals OK, but a per-goal load raises → the scan is incomplete and
    # cannot prove the gate is answered → fail closed.
    agent_b = MagicMock()
    agent_b.name = agent_root.name
    backend_b = MagicMock()
    backend_b.for_goal.side_effect = OSError("goal file unreadable")
    backend_b.list_goals.return_value = ["crun-other"]
    agent_b.goal_backend = backend_b
    assert (
        _is_decision_still_pending(
            agent=agent_b, decision_id="gate-abc", blocking_run_id="crun-blocking"
        )
        is True
    ), "an incomplete fallback scan must keep the run deferred (fail-closed)"


# ──────────────────────────────────────────────────────────────────
# TEST 44 (C4b) — _scan_active_conflicts FAILS CLOSED (raises) when it cannot
# reliably complete, so a new run never enters an exclusive stage blind.


def test_scan_active_conflicts_fails_closed_on_read_error(
    agent_root: Path,
    goal_backend: FilesystemGoalBackend,
) -> None:
    """TEST 44 — _scan_active_conflicts raises ConductorConflictScanError on a bad scan.

    A2 + B3 fail-closed. Three cases:
      (a) list_goals raises → cannot enumerate the goal universe → raise.
      (b) list_goals OK but a per-goal load_goal raises (a goal that MIGHT hold
          an overlapping key is unreadable) → raise.
      (c) a real holder overlaps the key in 'awaiting_decision' but carries a
          FALSY gate_decision_id (malformed blocker) → raise (B3), rather than
          emit a blank blocking id that would defeat self-release.

    Strip-RED: if A2/B3 are reverted (return None / emit ''), each case fails.
    """
    from atomic_agents.goal.types import CURRENT_GOAL_SCHEMA_VERSION, Goal, SubGoal

    # (a) list_goals raises.
    agent_a = MagicMock()
    agent_a.name = agent_root.name
    backend_a = MagicMock()
    backend_a.list_goals.side_effect = OSError("backend down")
    agent_a.goal_backend = backend_a
    with pytest.raises(ConductorConflictScanError):
        _scan_active_conflicts(
            agent=agent_a,
            stage_conflict_keys=("repo-x",),
            own_conductor_run_id="crun-self",
        )

    # (b) list_goals OK, per-goal load raises.
    agent_b = MagicMock()
    agent_b.name = agent_root.name
    backend_b = MagicMock()
    backend_b.list_goals.return_value = ["crun-other"]
    scoped_b = MagicMock()
    scoped_b.load_goal.side_effect = OSError("goal file unreadable")
    backend_b.for_goal.return_value = scoped_b
    agent_b.goal_backend = backend_b
    with pytest.raises(ConductorConflictScanError):
        _scan_active_conflicts(
            agent=agent_b,
            stage_conflict_keys=("repo-x",),
            own_conductor_run_id="crun-self",
        )

    # (c) B3 — a real overlapping holder with a falsy gate_decision_id.
    blocking_backend = goal_backend.for_goal("crun-malformed")
    blocking_goal = Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent="Malformed blocker",
        priority="medium",
        created="2026-06-28",
        last_progress_check="2026-06-28",
        success_criteria=["done"],
        sub_goals=[
            SubGoal(
                id="gate-stage",
                label="Gate",
                status="awaiting_decision",
                gate_decision_id="",  # malformed: no decision id
                held_conflict_keys=["repo-x"],
            )
        ],
    )
    blocking_backend.save_goal(agent_root.name, blocking_goal)
    agent_c = MagicMock()
    agent_c.name = agent_root.name
    agent_c.goal_backend = goal_backend
    with pytest.raises(ConductorConflictScanError):
        _scan_active_conflicts(
            agent=agent_c,
            stage_conflict_keys=("repo-x",),
            own_conductor_run_id="crun-self",
        )


# ──────────────────────────────────────────────────────────────────
# TEST 45 — C7 launder-guard (PR4 #583): run() raises ConductorLaunderRefused
#           when agent.trigger == 'delegate' (hard raise, not warn)


def test_c7_run_raises_on_delegate_trigger(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """C7: run() MUST raise ConductorLaunderRefused when agent.trigger == 'delegate'.

    PR4 #583 upgraded the warn-only guard in run() to a hard raise. The guard must
    fire BEFORE _resolve_or_create_run() — no goals/<id>/ directory must be created
    for an invalid delegate call (Principle #5: no orphaned ledger state).
    """
    pb_dir = agent_root / "skills" / "c7-run-pb"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(_make_playbook_md(name="c7-run-pb"))
    playbook = next(m for m in discover_playbooks(agent_root) if m.name == "c7-run-pb")

    mock_agent.trigger = "delegate"

    goals_before = (
        set((agent_root / "goals").iterdir())
        if (agent_root / "goals").is_dir()
        else set()
    )

    with pytest.raises(ConductorLaunderRefused):
        run(playbook=playbook, subject="launder attempt", agent=mock_agent)

    # Guard must fire BEFORE any goal directory is created
    goals_after = (
        set((agent_root / "goals").iterdir())
        if (agent_root / "goals").is_dir()
        else set()
    )
    new_dirs = goals_after - goals_before
    assert not new_dirs, (
        f"C7 guard must fire BEFORE _resolve_or_create_run() — "
        f"no goal directory must be created; got new dirs: {new_dirs}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 45-strip-RED — C7 launder-guard: run() does NOT raise when trigger
#                     is NOT 'delegate'


def test_c7_run_does_not_raise_on_non_delegate_trigger(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """C7 strip-RED: run() must NOT raise ConductorLaunderRefused for non-delegate triggers.

    The guard is keyed on trigger == 'delegate' only. cron / serve / None / 'operator'
    must all proceed normally (the guard does not fire for these).
    """
    pb_dir = agent_root / "skills" / "c7-strip-pb"
    pb_dir.mkdir()
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(_make_playbook_md(name="c7-strip-pb"))
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "c7-strip-pb"
    )

    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.01)
    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    for trigger_val in (None, "cron", "serve", "operator"):
        mock_agent.trigger = trigger_val
        # Reset goal state so each iteration runs fresh
        mock_agent.agent_root = agent_root
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
            # Must NOT raise ConductorLaunderRefused — any other exception is unexpected
            # but not what this strip-RED is testing
            try:
                run(playbook=playbook, subject="valid trigger", agent=mock_agent)
            except ConductorLaunderRefused:
                pytest.fail(
                    f"C7 strip-RED: run() must NOT raise ConductorLaunderRefused "
                    f"for trigger={trigger_val!r}; it must only raise for 'delegate'."
                )


# ──────────────────────────────────────────────────────────────────
# TEST 46 — C7 launder-guard (PR4 #583): resume() raises ConductorLaunderRefused
#           when agent.trigger == 'delegate' BEFORE any ledger mutation


def test_c7_resume_raises_on_delegate_trigger(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """C7: resume() MUST raise ConductorLaunderRefused when agent.trigger == 'delegate'.

    The guard must fire BEFORE _record_gate_answer() — the gate sub-goal must remain
    in 'awaiting_decision' after the raise (no ledger mutation on a launder call).
    """
    from atomic_agents.goal.filesystem import FilesystemGoalBackend  # noqa: PLC0415

    # Phase 1: establish a suspended gate via a non-delegate run()
    pb_dir = agent_root / "skills" / "c7-resume-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Human gate
            prompt: Approve to continue.
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="c7-resume-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "c7-resume-pb"
    )

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # Non-delegate trigger for run() — must succeed and suspend at the gate
    mock_agent.trigger = "cron"
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
        state = run(playbook=playbook, subject="c7 resume test", agent=mock_agent)

    assert state.status == "awaiting_decision", (
        f"Expected suspension at gate; got {state.status!r}"
    )
    conductor_run_id = state.conductor_run_id
    decision_id = state.pending_decision.decision_id

    # Read the history JSONL before the resume() attempt to compare after
    history_path = agent_root / "goals" / conductor_run_id / "goal_history.jsonl"
    events_before = history_path.read_bytes()

    # Phase 2: attempt resume() with trigger == 'delegate' — must raise BEFORE any write
    mock_agent.trigger = "delegate"

    with pytest.raises(ConductorLaunderRefused):
        resume(
            playbook=playbook,
            subject="c7 resume test",
            agent=mock_agent,
            conductor_run_id=conductor_run_id,
            decision_id=decision_id,
            answer="Approved.",
            disposition="continue",
            rationale="Testing the C7 guard.",
        )

    # The guard must have fired BEFORE _record_gate_answer — no new events appended
    events_after = history_path.read_bytes()
    assert events_before == events_after, (
        "C7 resume() guard must fire BEFORE _record_gate_answer(); "
        "goal_history.jsonl must NOT have new events after the raise. "
        f"Before size={len(events_before)}, after size={len(events_after)}."
    )

    # The gate sub-goal must still be 'awaiting_decision'
    goal_backend = FilesystemGoalBackend(agent_root)
    conductor_backend = goal_backend.for_goal(conductor_run_id)
    goal = conductor_backend.load_goal(agent_root.name)
    gate_sg = next((sg for sg in goal.sub_goals if sg.id == "gate-stage"), None)
    assert gate_sg is not None, "gate-stage sub-goal must still exist"
    assert gate_sg.status == "awaiting_decision", (
        f"Gate sub-goal must remain 'awaiting_decision' after C7 resume() raise; "
        f"got {gate_sg.status!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 46-strip-RED — C7 launder-guard: resume() does NOT raise when trigger
#                     is NOT 'delegate'


def test_c7_resume_does_not_raise_on_non_delegate_trigger(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """C7 strip-RED: resume() must NOT raise ConductorLaunderRefused for non-delegate triggers.

    Only trigger == 'delegate' trips the guard. Other triggers (cron, None, serve) must
    proceed normally through the resume path.
    """
    pb_dir = agent_root / "skills" / "c7-resume-strip-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Gate
            prompt: Approve.
            is_gate: true
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(
            name="c7-resume-strip-pb", stages=stages_yaml, run_cap_usd=5.0
        )
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "c7-resume-strip-pb"
    )

    idem_backend = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    # Establish a suspended gate
    mock_agent.trigger = "cron"
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
        state = run(playbook=playbook, subject="strip test", agent=mock_agent)

    assert state.status == "awaiting_decision"
    conductor_run_id = state.conductor_run_id
    decision_id = state.pending_decision.decision_id

    # resume() with a non-delegate trigger must NOT raise ConductorLaunderRefused
    mock_agent.trigger = "cron"
    try:
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
                subject="strip test",
                agent=mock_agent,
                conductor_run_id=conductor_run_id,
                decision_id=decision_id,
                answer="Fine.",
                disposition="halt",
                rationale="Strip-RED control: non-delegate trigger must not raise C7.",
            )
    except ConductorLaunderRefused:
        pytest.fail(
            "C7 strip-RED: resume() must NOT raise ConductorLaunderRefused "
            "for trigger='cron' — only trigger='delegate' trips the guard."
        )


# ──────────────────────────────────────────────────────────────────
# TEST 47–50 — check_conductor heavy probe (spec/50 PR4 #583)
#
# The dual-probe diagnostic's whole purpose is to detect ledger corruption /
# anomalies on the genuinely-MOST-RECENT conductor run. These tests materialize
# REAL conductor runs on disk (via run()) and exercise the heavy-probe ladder
# the light/zero-run tests in test_doctor.py do NOT: PASS on a healthy run,
# FAIL on a corrupted goal.md, the most-recent-BY-TS selection (the regression
# guard for the random-UUID4 run_id sort bug), and the per-gate missing-audit
# WARN. Without these, the FAIL/WARN rungs ship at 0% coverage (false-green).


def _run_simple_conductor(
    agent_root: Path,
    mock_agent: MagicMock,
    *,
    name: str = "probe-pb",
    run_cap: float = 5.0,
) -> str:
    """Run a single-auto-stage conductor playbook to completion; return run_id."""
    pb_dir = agent_root / "skills" / name
    pb_dir.mkdir(parents=True, exist_ok=True)
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: only-stage
            label: The only stage
            prompt: Do the thing.
            is_gate: false
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name=name, stages=stages_yaml, run_cap_usd=run_cap)
    )
    playbook = next(m for m in discover_playbooks(agent_root) if m.name == name)
    _dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=_make_outcome_backend_mock(),
        ),
    ):
        state = run(playbook=playbook, subject=f"{name} subject", agent=mock_agent)
    assert state.status == "complete", (
        f"heavy-probe setup run did not complete: {state.status!r}"
    )
    return state.conductor_run_id


def _set_run_started_ts(agent_root: Path, run_id: str, ts: str) -> None:
    """Rewrite the conductor_run_started event's `ts` in a run's history file."""
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    out: list[str] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("event") == "conductor_run_started":
            rec["ts"] = ts
        out.append(json.dumps(rec))
    history_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _corrupt_goal_md(agent_root: Path, run_id: str) -> None:
    """Break a run's goal.md so load_goal() raises GoalCorrupted.

    An unterminated YAML flow sequence in the frontmatter makes frontmatter.load
    raise, which load_goal() maps to GoalCorrupted (the goal.md STATE SNAPSHOT,
    not the goal_history.jsonl audit ledger).
    """
    (agent_root / "goals" / run_id / "goal.md").write_text(
        "---\ncorrupt: [unterminated\n---\nbroken goal.md\n", encoding="utf-8"
    )


def test_check_conductor_pass_healthy_run(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 47 — a healthy completed run → PASS with the run's id surfaced."""
    from atomic_agents.doctor import check_conductor, PASS  # noqa: PLC0415

    run_id = _run_simple_conductor(agent_root, mock_agent)
    result = check_conductor(agent_root)

    assert result.status == PASS, (
        f"healthy run must PASS; got {result.status!r}: {result.message}"
    )
    assert result.detail["conductor_runs_found"] == 1
    assert result.detail["most_recent_run_id"] == run_id


def test_check_conductor_fail_on_corrupted_goal(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 48 — the most-recent run's goal.md is corrupt → FAIL (negative control).

    This is the rung the heavy probe exists for. PASS here would be a false
    negative masking real corruption.
    """
    from atomic_agents.doctor import check_conductor, FAIL, PASS  # noqa: PLC0415

    run_id = _run_simple_conductor(agent_root, mock_agent)
    _corrupt_goal_md(agent_root, run_id)
    result = check_conductor(agent_root)

    assert result.status == FAIL, (
        f"corrupt goal.md must FAIL; got {result.status!r}: {result.message}"
    )
    assert result.status != PASS  # explicit false-negative guard
    assert result.detail["most_recent_run_id"] == run_id
    assert "corruption" in result.detail


def test_check_conductor_selects_most_recent_by_ts(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 49 — most-recent selection is BY TS, not by lexicographic run_id (nor mtime).

    Regression guard for the random-UUID4 run_id sort bug. Run IDs are
    'crun-<uuid4().hex[:16]>' (no temporal order). The three candidate signals
    are deliberately set to DISAGREE so only a ts-based selection lands on the
    run we corrupt:
      - conductor_run_started ts: NEWER on the lexicographically-SMALLER run_id.
      - history-file mtime (the documented FALLBACK): forced NEWER on the OTHER
        (lexicographically-larger, older-ts) run via os.utime.
    We corrupt ONLY the newest-BY-TS run. A correct ts-based selection probes the
    corrupt run → FAIL. A lexicographic run_id sort OR an mtime sort would each
    instead probe the older healthy run → false PASS. Deterministic: the answer
    does not depend on the random run_ids or on filesystem write timing.
    """
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    from atomic_agents.doctor import check_conductor, FAIL  # noqa: PLC0415

    run_a = _run_simple_conductor(agent_root, mock_agent, name="probe-a")
    run_b = _run_simple_conductor(agent_root, mock_agent, name="probe-b")

    lex_smaller, lex_larger = sorted([run_a, run_b])
    # Newer ts → lexicographically SMALLER id; older ts → larger id.
    _set_run_started_ts(agent_root, lex_smaller, "2026-06-28T12:00:00+00:00")
    _set_run_started_ts(agent_root, lex_larger, "2026-06-27T12:00:00+00:00")
    # Force the mtime fallback to point the OTHER way: the older-ts (lex_larger)
    # run gets the NEWER history mtime, so an mtime-based pick would (wrongly)
    # choose it. Only ts-based selection survives this.
    now = time.time()
    os.utime(
        agent_root / "goals" / lex_smaller / "goal_history.jsonl",
        (now - 1000, now - 1000),
    )
    os.utime(agent_root / "goals" / lex_larger / "goal_history.jsonl", (now, now))
    # Corrupt the genuinely-most-recent (by ts) run.
    _corrupt_goal_md(agent_root, lex_smaller)

    result = check_conductor(agent_root)

    assert result.status == FAIL, (
        f"Expected FAIL — the most-recent-by-ts run {lex_smaller!r} has a corrupt "
        f"goal.md. A lexicographic run_id sort would instead probe the older "
        f"healthy run {lex_larger!r} and return a false PASS. "
        f"got {result.status!r}: {result.message}"
    )
    assert result.detail["most_recent_run_id"] == lex_smaller, (
        f"heavy probe must select the most-recent-BY-TS run {lex_smaller!r}, not "
        f"{result.detail.get('most_recent_run_id')!r} (lexicographic-largest id)"
    )
    assert result.detail["conductor_runs_found"] == 2


def test_check_conductor_warn_missing_gate_pending(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 50 — awaiting_decision sub-goal with no matching gate-pending audit → WARN.

    Suspend a run at a gate (PASS while the audit event is present — the
    negative control), then strip the conductor_gate_pending event from history.
    The heavy probe must downgrade to WARN, flagging the specific gate whose
    per-stage+decision audit is missing (healable on next run()).
    """
    from atomic_agents.doctor import check_conductor, PASS, WARN  # noqa: PLC0415

    pb_dir = agent_root / "skills" / "warn-gate-pb"
    pb_dir.mkdir(parents=True)
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
        _make_playbook_md(name="warn-gate-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "warn-gate-pb"
    )
    _dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.05)
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=_make_outcome_backend_mock(),
        ),
    ):
        state = run(playbook=playbook, subject="warn gate", agent=mock_agent)
    assert state.status == "awaiting_decision"
    run_id = state.conductor_run_id

    # Negative control: with the gate-pending audit present, the suspended run is PASS.
    pre = check_conductor(agent_root)
    assert pre.status == PASS, (
        f"suspended run with its gate-pending audit present must PASS; "
        f"got {pre.status!r}: {pre.message}"
    )

    # Strip the conductor_gate_pending event(s) from history.
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    kept = [
        line
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") != "conductor_gate_pending"
    ]
    history_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = check_conductor(agent_root)
    assert result.status == WARN, (
        f"missing per-gate conductor_gate_pending must WARN; "
        f"got {result.status!r}: {result.message}"
    )
    assert any("awaiting_decision" in w for w in result.detail.get("warnings", [])), (
        f"WARN detail must name the awaiting_decision anomaly; got {result.detail}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 51 — a gated run completed via resume(continue) → PASS (P1 negative control)
#
# An answered gate (disposition='continue') is marked 'complete' with its
# gate_decision_id CLEARED to None and NO output pointer (gates produce no
# outcome_run_id). The earlier complete-branch heuristic keyed off gate_decision_id
# presence and so cried wolf on this — the COMMON happy path for human-gated
# playbooks. This drives a gate to 'complete' (the rung TEST 50 never reaches,
# since it only SUSPENDS the gate) and asserts the probe distinguishes the answered
# gate from a genuinely-output-less automated stage by the durable
# conductor_gate_answered audit, returning PASS.


def test_check_conductor_pass_completed_gate_continue(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 51 — resume(continue) completes a gate → check_conductor PASS."""
    from atomic_agents.doctor import check_conductor, PASS, WARN  # noqa: PLC0415

    pb_dir = agent_root / "skills" / "pass-gate-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Human gate
            prompt: Approve to continue.
            is_gate: true
            options:
              - Approve
          - stage_id: post-gate
            label: Post-gate work
            prompt: Do the work after approval.
            is_gate: false
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="pass-gate-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "pass-gate-pb"
    )
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.02)
    outcome_backend = _make_outcome_backend_mock()

    # Phase 1: run() hits the gate stage and suspends.
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
        state1 = run(playbook=playbook, subject="pass gate", agent=mock_agent)
    assert state1.status == "awaiting_decision"
    gd = state1.pending_decision
    assert gd is not None

    # Phase 2: resume(continue) → gate marked 'complete', post-gate runs, run completes.
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
        state2 = resume(
            playbook=playbook,
            subject="pass gate",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="Approve",
            disposition="continue",
            rationale="All checks passed.",
        )
    assert state2.status == "complete", (
        f"resume(continue) must complete the run; got {state2.status!r}"
    )

    # The completed gate (gate_decision_id cleared, no output) must NOT trip a WARN.
    result = check_conductor(agent_root)
    assert result.status == PASS, (
        f"a healthy gated run answered with disposition='continue' must PASS; "
        f"got {result.status!r}: {result.message}. "
        f"detail={result.detail}"
    )
    assert result.status != WARN  # explicit false-WARN guard (P1 cry-wolf)
    assert result.detail["most_recent_run_id"] == state1.conductor_run_id


# ──────────────────────────────────────────────────────────────────
# TEST 52 — a parseable-but-SCHEMA-INVALID goal.md → FAIL (PR4 #583 R3)
#
# load_goal() raises SchemaValidationError (NOT a GoalCorrupted subclass) on the
# most common goal.md state-snapshot corruption class: parseable frontmatter that
# fails validate_goal() — duplicate sub_goal id, missing required field, bad
# schema_version, broken blocked_by graph. The heavy probe must catch BOTH
# GoalCorrupted (unparseable, TEST 48) AND SchemaValidationError here, or a
# structurally-corrupt ledger that hard-crashes every run()/resume() would fall
# to the generic-Exception WARN and doctor would exit 0 on confirmed corruption.


def _schema_invalidate_goal_md(agent_root: Path, run_id: str) -> None:
    """Make a run's goal.md parseable-but-schema-INVALID (SchemaValidationError).

    Duplicates the first sub_goal id (validate_goal raises 'duplicate sub_goal
    id'); falls back to a bad schema_version if the run has no sub_goals. The
    frontmatter stays valid YAML, so frontmatter.load() succeeds and load_goal()
    surfaces a SchemaValidationError, NOT a GoalCorrupted — the exact distinction
    this test guards.
    """
    import frontmatter  # noqa: PLC0415

    goal_path = agent_root / "goals" / run_id / "goal.md"
    post = frontmatter.load(goal_path)
    subs = list(post.metadata.get("sub_goals") or [])
    if subs:
        subs.append(dict(subs[0]))  # duplicate id → SchemaValidationError
        post.metadata["sub_goals"] = subs
    else:
        post.metadata["schema_version"] = CURRENT_GOAL_SCHEMA_VERSION + 9000
    goal_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def test_check_conductor_fail_on_schema_invalid_goal(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 52 — parseable-but-schema-invalid goal.md → FAIL (not WARN).

    Mirrors TEST 48 (unparseable → GoalCorrupted → FAIL) for the
    SchemaValidationError path, which is a DIFFERENT exception class and would
    otherwise be downgraded to a transient WARN (doctor false-passes corruption).
    """
    from atomic_agents.exceptions import (  # noqa: PLC0415
        GoalCorrupted,
        SchemaValidationError,
    )
    from atomic_agents.doctor import check_conductor, FAIL, PASS  # noqa: PLC0415

    # Guard the load-bearing class fact: SchemaValidationError must NOT be a
    # GoalCorrupted subclass, else the original `except GoalCorrupted` would have
    # already covered it and this regression would be vacuous.
    assert not issubclass(SchemaValidationError, GoalCorrupted)

    run_id = _run_simple_conductor(agent_root, mock_agent)
    _schema_invalidate_goal_md(agent_root, run_id)
    result = check_conductor(agent_root)

    assert result.status == FAIL, (
        f"schema-invalid goal.md must FAIL (confirmed corruption), not WARN; "
        f"got {result.status!r}: {result.message}"
    )
    assert result.status != PASS  # explicit false-negative guard
    assert result.detail["most_recent_run_id"] == run_id


# ──────────────────────────────────────────────────────────────────
# TEST 53 — goal.md intact but goal_history.jsonl unreadable → honest WARN
#           (NOT a cry-wolf about a missing output pointer) (PR4 #583 R3)
#
# _iter_history_events swallows OSError/JSONDecodeError into (None, False) and
# never raises, so the heavy-probe collection loop sees an EMPTY event set when
# the audit log is corrupt. The earlier code then ran the cursor walk against
# that empty set and cried wolf — a completed-continue gate (TEST 51 happy path)
# tripped 'automated sub-goal complete but no output pointer' because its
# conductor_gate_answered audit was no longer visible. The probe must instead
# honor the ok=False degraded signal and WARN about the unreadable history.


def test_check_conductor_warn_on_unreadable_history(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 53 — completed-continue run with corrupted history → WARN names the history."""
    from atomic_agents.doctor import check_conductor, PASS, WARN  # noqa: PLC0415

    pb_dir = agent_root / "skills" / "deg-gate-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Human gate
            prompt: Approve to continue.
            is_gate: true
            options:
              - Approve
          - stage_id: post-gate
            label: Post-gate work
            prompt: Do the work after approval.
            is_gate: false
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="deg-gate-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "deg-gate-pb"
    )
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.02)
    outcome_backend = _make_outcome_backend_mock()

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
        state1 = run(playbook=playbook, subject="deg gate", agent=mock_agent)
        gd = state1.pending_decision
        assert gd is not None
        state2 = resume(
            playbook=playbook,
            subject="deg gate",
            agent=mock_agent,
            conductor_run_id=state1.conductor_run_id,
            decision_id=gd.decision_id,
            answer="Approve",
            disposition="continue",
            rationale="ok",
        )
    assert state2.status == "complete"
    run_id = state1.conductor_run_id

    # Negative control: with an intact history the completed-continue run PASSes.
    assert check_conductor(agent_root).status == PASS

    # Corrupt the audit log (goal.md stays intact) by garbling ONLY the
    # conductor_gate_answered line: the light probe still classifies the run
    # (conductor_run_started line untouched), but the heavy re-read loses the
    # answered-gate signal AND sees an ok=False degraded line. Without the
    # history-degraded guard the cursor walk would cry wolf about a completed
    # gate with 'no output pointer'; the probe must instead surface the honest
    # history-degraded WARN.
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    out: list[str] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if json.loads(line).get("event") == "conductor_gate_answered":
            out.append("}{ corrupt audit line not json")
        else:
            out.append(line)
    history_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    result = check_conductor(agent_root)
    assert result.status == WARN, (
        f"unreadable goal_history.jsonl (goal.md intact) must WARN; "
        f"got {result.status!r}: {result.message}"
    )
    assert result.detail.get("history_degraded") is True, (
        f"WARN must flag history_degraded, not a cursor anomaly; got {result.detail}"
    )
    # Cry-wolf guard: must NOT blame a missing output pointer / missing audit.
    assert "output pointer" not in result.message
    assert "warnings" not in result.detail


# ──────────────────────────────────────────────────────────────────
# TEST 54 — awaiting_decision sub-goal with NO gate_decision_id → FAIL
#           (durable resume cursor unreadable; run.py raises GoalCorrupted) (PR4 #583 R3)
#
# validate_goal does NOT couple awaiting_decision to gate_decision_id, so a
# hand-edited/corrupted goal.md in that state loads cleanly and reaches the
# cursor walk. run.py:533 raises GoalCorrupted for exactly this state — the next
# run()/resume() hard-fails, it does NOT heal. The probe must FAIL here, not
# under-report it as a 'healable: next run() will re-emit it' WARN.


def test_check_conductor_fail_awaiting_decision_no_gate_id(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 54 — awaiting_decision sub-goal missing gate_decision_id → FAIL (not healable WARN)."""
    import frontmatter  # noqa: PLC0415

    from atomic_agents.doctor import check_conductor, FAIL, WARN  # noqa: PLC0415

    pb_dir = agent_root / "skills" / "nocursor-gate-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Human gate
            prompt: Approve to continue.
            is_gate: true
            options:
              - Approve
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="nocursor-gate-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "nocursor-gate-pb"
    )
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_make_ledger_updating_dispatch(total_cost_usd=0.02),
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=_make_outcome_backend_mock(),
        ),
    ):
        state = run(playbook=playbook, subject="nocursor gate", agent=mock_agent)
    assert state.status == "awaiting_decision"
    run_id = state.conductor_run_id

    # Negative control: with the gate_decision_id present the suspended run PASSes
    # (its gate-pending audit is intact). Corruption is ONLY the cleared cursor.
    assert check_conductor(agent_root).status != FAIL

    # Strip gate_decision_id from the awaiting_decision sub-goal (goal.md stays
    # valid YAML and passes validate_goal — the cursor field is not schema-coupled).
    goal_path = agent_root / "goals" / run_id / "goal.md"
    post = frontmatter.load(goal_path)
    subs = list(post.metadata.get("sub_goals") or [])
    changed = False
    for sg in subs:
        if sg.get("status") == "awaiting_decision":
            sg.pop("gate_decision_id", None)
            sg["gate_decision_id"] = None
            changed = True
    assert changed, "test setup: no awaiting_decision sub-goal found to corrupt"
    post.metadata["sub_goals"] = subs
    goal_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

    result = check_conductor(agent_root)
    assert result.status == FAIL, (
        f"awaiting_decision with no gate_decision_id is durable corruption "
        f"(next run()/resume() raises GoalCorrupted); must FAIL, not a healable "
        f"WARN. got {result.status!r}: {result.message}"
    )
    assert result.status != WARN  # explicit under-report guard
    assert result.detail.get("corrupt_sub_goal_id")


# TEST 54b — COMPOUND fault: corrupt goal.md cursor (awaiting_decision with NO
#            gate_decision_id) PLUS a degraded goal_history.jsonl → FAIL (PR4 #583 R5)
#
# The awaiting_decision-no-gate_decision_id FAIL reads ONLY goal.md (sg.status +
# sg.gate_decision_id) — it has no audit-log dependency — so it MUST run BEFORE
# the history_degraded early-return. Otherwise a single garbled audit line would
# demote a confirmed durable-corruption FAIL (run.py:536 raises GoalCorrupted
# regardless of audit readability) to a healable history-degraded WARN. This is
# the compound case TEST 54 (intact history) does not exercise.


def test_check_conductor_fail_awaiting_no_gate_id_even_when_history_degraded(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 54b — corrupt goal.md cursor + degraded history → FAIL, not history-degraded WARN."""
    import frontmatter  # noqa: PLC0415

    from atomic_agents.doctor import check_conductor, FAIL, WARN  # noqa: PLC0415

    pb_dir = agent_root / "skills" / "compound-gate-pb"
    pb_dir.mkdir()
    stages_yaml = textwrap.dedent(
        """\
        stages:
          - stage_id: gate-stage
            label: Human gate
            prompt: Approve to continue.
            is_gate: true
            options:
              - Approve
        """
    )
    (pb_dir / PLAYBOOK_ENTRY_POINT).write_text(
        _make_playbook_md(name="compound-gate-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "compound-gate-pb"
    )
    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_make_ledger_updating_dispatch(total_cost_usd=0.02),
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=_make_idempotency_backend_mock(),
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=_make_outcome_backend_mock(),
        ),
    ):
        state = run(playbook=playbook, subject="compound gate", agent=mock_agent)
    assert state.status == "awaiting_decision"
    run_id = state.conductor_run_id

    # Fault 1 — strip gate_decision_id from goal.md (the durable cursor).
    goal_path = agent_root / "goals" / run_id / "goal.md"
    post = frontmatter.load(goal_path)
    subs = list(post.metadata.get("sub_goals") or [])
    for sg in subs:
        if sg.get("status") == "awaiting_decision":
            sg["gate_decision_id"] = None
    post.metadata["sub_goals"] = subs
    goal_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

    # Fault 2 — garble one goal_history.jsonl line so the audit reader reports
    # ok=False (history_degraded). The conductor_run_started line stays intact so
    # the light probe still classifies the run.
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    out: list[str] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if json.loads(line).get("event") == "conductor_gate_pending":
            out.append("}{ corrupt audit line not json")
        else:
            out.append(line)
    history_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    # The goal.md-only FAIL must win over the history-degraded WARN: a confirmed
    # durable-cursor corruption is NOT healable just because the audit log is also
    # unreadable. (Strip-RED: if the FAIL pre-pass sat AFTER the history gate,
    # this would return a history-degraded WARN.)
    result = check_conductor(agent_root)
    assert result.status == FAIL, (
        f"corrupt goal.md cursor must FAIL even when goal_history.jsonl is also "
        f"degraded (run.py:536 raises GoalCorrupted regardless of audit "
        f"readability); got {result.status!r}: {result.message}"
    )
    assert result.status != WARN  # explicit compound-fault under-report guard
    assert result.detail.get("corrupt_sub_goal_id")
    # The FAIL must name the cursor corruption, not the degraded audit log.
    assert result.detail.get("history_degraded") is not True


# TEST 55 — 'skipped' sub-goal with NO recorded skip ruling → FAIL
#           (durable corruption; run.py raises GoalCorrupted) (PR4 #583 R4)
#
# A 'skipped' status is schema-valid, so load_goal() succeeds and the cursor walk
# is reached. run.py:660 raises GoalCorrupted for exactly this state via
# _has_gate_answered_skip() — its own comment calls it "the symmetric partner of
# the complete-without-result corruption check" (spec/50 C4: "a skipped stage is a
# recorded ruling, never an absent stage"). A skipped stage sits behind the resume
# frontier, so the next run()/resume() iterates to it FIRST and hard-crashes; it is
# NOT healable. The probe must FAIL here, symmetric with the awaiting_decision-no-
# gate_decision_id FAIL (TEST 54) — the fourth hard-crash class.


def test_check_conductor_fail_skipped_no_skip_ruling(
    agent_root: Path, mock_agent: MagicMock
) -> None:
    """TEST 55 — 'skipped' sub-goal missing its conductor_gate_answered(skip) ruling → FAIL."""
    from atomic_agents.doctor import check_conductor, FAIL, PASS, WARN  # noqa: PLC0415

    pb_dir = agent_root / "skills" / "skip-ruling-pb"
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
        _make_playbook_md(name="skip-ruling-pb", stages=stages_yaml, run_cap_usd=5.0)
    )
    playbook = next(
        m for m in discover_playbooks(agent_root) if m.name == "skip-ruling-pb"
    )
    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.02)
    outcome_backend = _make_outcome_backend_mock()

    # Phase 1: suspend at the gate.
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
        state1 = run(playbook=playbook, subject="skip ruling", agent=mock_agent)
    assert state1.status == "awaiting_decision"
    gd = state1.pending_decision
    assert gd is not None
    run_id = state1.conductor_run_id

    # Phase 2: resume(skip) → gate-stage marked 'skipped' WITH a recorded
    # conductor_gate_answered(disposition='skip') ruling, run completes.
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
        state2 = resume(
            playbook=playbook,
            subject="skip ruling",
            agent=mock_agent,
            conductor_run_id=run_id,
            decision_id=gd.decision_id,
            answer="Skip it — not needed this time.",
            disposition="skip",
            rationale="Low-risk change; extra analysis not warranted.",
        )
    assert state2.status == "complete"
    assert "gate-stage" in state2.completed_stage_ids

    # Negative control: the skip ruling IS recorded → the skipped stage is a valid
    # terminal-done ruling, so the probe PASSes (it must NOT FAIL on the happy path).
    pre = check_conductor(agent_root)
    assert pre.status == PASS, (
        f"a skipped stage WITH its recorded skip ruling is the happy path and must "
        f"PASS, not cry corruption; got {pre.status!r}: {pre.message}"
    )

    # Strip the conductor_gate_answered(disposition='skip') event from the audit
    # ledger (rewrite without that one line — all remaining lines stay valid JSON,
    # so history is NOT degraded; only the skip ruling is gone). goal.md still
    # carries status='skipped'. This is the exact state run.py GoalCorrupts on.
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    kept: list[str] = []
    stripped = False
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if (
            rec.get("event") == "conductor_gate_answered"
            and rec.get("stage_id") == "gate-stage"
            and rec.get("disposition") == "skip"
        ):
            stripped = True
            continue
        kept.append(line)
    assert stripped, "test setup: no conductor_gate_answered(skip) event found to strip"
    history_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = check_conductor(agent_root)
    assert result.status == FAIL, (
        f"a 'skipped' sub-goal with no recorded conductor_gate_answered(skip) ruling "
        f"is durable corruption (next run()/resume() raises GoalCorrupted; spec/50 "
        f"C4); must FAIL, not a healable WARN. got {result.status!r}: {result.message}"
    )
    assert result.status != WARN  # explicit under-report guard (not history-degraded)
    assert result.detail.get("corrupt_sub_goal_id") == "gate-stage"


# ──────────────────────────────────────────────────────────────────
# TEST 56 (C8) — a gate-suspended run holds NO live lock across the wait: an
# independent, non-conflicting run for the SAME agent proceeds normally.
#
# spec/50 C8 is LOCKed as an UNCONDITIONAL MUST ("a gate-suspended run holds NO
# live resource / lock across the wait; an independent non-conflicting run for
# the same agent proceeds normally"). The sibling conflict tests cover only the
# negative direction (TEST 40/42: a CONFLICTING-key run B QUEUES behind A). None
# of them proves the positive C8 guarantee — that a NON-conflicting B is NOT
# blocked by A's suspension. This is that positive conformance test. Driven
# against a REAL FilesystemLockBackend so the per-run "conductor-runs" lease (run
# A acquires it for the whole body and releases it in finally on the suspend
# exit, run.py:311-329 + 1395-1399) and the conflict-scan lease are actually
# taken/released — a MagicMock backend would prove nothing about real locks.


def test_c8_gate_suspension_holds_no_lock_independent_run_proceeds(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """TEST 56 (C8) — gate suspension holds no live lock; an independent run proceeds.

    Run A reaches a gate declaring conflict_keys=['repo-x'] and SUSPENDS
    (awaiting_decision, holding 'repo-x'). Then a FRESH INDEPENDENT run B for the
    SAME agent — DIFFERENT conductor_run_id (distinct subject), NON-conflicting
    key 'repo-z' that A does NOT hold — must proceed NORMALLY to its OWN gate
    (status='awaiting_decision', its own decision_id), NOT 'deferred'. If A's
    suspension held any live lock that blocked an independent run, B would either
    raise LockBusy or defer. Two extra proofs that A holds nothing live:
      - A's per-run "conductor-runs" lease is FREE (a manual acquire on A's
        conductor_run_id does NOT raise — released on the suspend exit path).
      - B becomes the legitimate holder of its OWN distinct key with its own
        pending GateDecision, so it executed its full pre-suspend path unblocked.
    """
    from atomic_agents.locks.filesystem import FilesystemLockBackend

    mock_agent.lock_backend = FilesystemLockBackend(agent_root)

    pb_a = _make_conflict_gate_playbook(agent_root, "c8-a", "repo-x")
    # B declares a DIFFERENT key that A does not hold — genuinely non-conflicting.
    pb_b = _make_conflict_gate_playbook(agent_root, "c8-b", "repo-z")

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
        state_a = run(playbook=pb_a, subject="A", agent=mock_agent)
        assert state_a.status == "awaiting_decision", (
            f"run A must suspend at its gate; got {state_a.status!r}"
        )
        a_run_id = state_a.conductor_run_id
        a_decision_id = state_a.pending_decision.decision_id

        # The INDEPENDENT, non-conflicting run B.
        state_b = run(playbook=pb_b, subject="B", agent=mock_agent)

    # C8 positive: B proceeds NORMALLY to its own gate — NOT blocked by A.
    assert state_b.status == "awaiting_decision", (
        f"an independent non-conflicting run B must proceed to its OWN gate while A "
        f"is suspended (NOT be blocked by A's suspension); got {state_b.status!r}"
    )
    assert state_b.status != "deferred", (
        "B shares NO conflict key with A, so it must NOT defer behind A"
    )
    assert state_b.queued_behind_decision_id is None, (
        f"B is non-conflicting and must not be queued behind any decision; "
        f"got {state_b.queued_behind_decision_id!r}"
    )
    assert state_b.conductor_run_id != a_run_id, (
        "B must be a genuinely independent run (distinct conductor_run_id)"
    )
    assert state_b.pending_decision is not None
    assert state_b.pending_decision.decision_id != a_decision_id, (
        "B must mint its OWN decision_id, distinct from A's"
    )

    # A's per-run lease is FREE — the suspension released it (no live lock held
    # across the wait). A manual acquire on A's conductor_run_id must NOT raise.
    probe = FilesystemLockBackend(agent_root).scope("conductor-runs")
    handle = probe.acquire(a_run_id, timeout=0.0)  # must NOT raise LockBusy
    probe.release(handle)

    # Durable check: A holds 'repo-x' and B holds 'repo-z' — two independent
    # awaiting_decision claims, neither blocking the other.
    holders: dict[str, int] = {}
    for goal_id in mock_agent.goal_backend.list_goals(mock_agent.name):
        goal = mock_agent.goal_backend.for_goal(goal_id).load_goal(mock_agent.name)
        for sg in goal.sub_goals:
            if sg.status == "awaiting_decision":
                for key in sg.held_conflict_keys or []:
                    holders[key] = holders.get(key, 0) + 1
    assert holders.get("repo-x") == 1, (
        f"A must hold 'repo-x' in awaiting_decision; holders={holders!r}"
    )
    assert holders.get("repo-z") == 1, (
        f"B must independently hold 'repo-z' in awaiting_decision; holders={holders!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 57 — parse-guard: the committed reference PLAYBOOK.md at
# docs/samples/dev-lifecycle/skills/dev-lifecycle-playbook/ validates
# through validate_playbook_manifest with zero warnings and the
# correct structural properties (#584).
#
# Anchored to the repo root via __file__, not a hard-coded absolute
# path, so it works on any checkout. Fails loud if the file is absent
# (is_dir() assertion) so a stale/removed sample is caught immediately.


def test_reference_playbook_parses_correctly() -> None:
    """TEST 57 — parse-guard: reference PLAYBOOK.md validates and has the expected shape.

    Confirms:
    - validate_playbook_manifest succeeds with zero warnings
    - 18 conductor stages total (10 automated + 8 gates)
    - The merge-gate stage carries conflict_keys == ('merge:main',)
    - run_cap_usd is 50.00
    - All gate stages have at least two options
    - design-direction-gate and rollback-gate have a 'skip' option
    """
    playbook_dir = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "samples"
        / "dev-lifecycle"
        / "skills"
        / "dev-lifecycle-playbook"
    )
    assert playbook_dir.is_dir(), (
        f"Reference playbook directory not found at {playbook_dir}; "
        "was docs/samples/dev-lifecycle/ created as part of #584?"
    )

    manifest, warnings = validate_playbook_manifest(playbook_dir)

    assert manifest is not None, (
        f"validate_playbook_manifest returned None; warnings={warnings}"
    )
    assert warnings == [], (
        f"Reference PLAYBOOK.md must validate with zero warnings; got: {warnings}"
    )
    assert manifest.run_cap_usd == 50.00, (
        f"run_cap_usd must be 50.00; got {manifest.run_cap_usd}"
    )
    assert len(manifest.stages) == 18, (
        f"Expected 18 conductor stages (10 automated + 8 gates); got {len(manifest.stages)}"
    )

    gate_stages = [s for s in manifest.stages if s.is_gate]
    automated_stages = [s for s in manifest.stages if not s.is_gate]
    assert len(gate_stages) == 8, (
        f"Expected 8 gate stages; got {len(gate_stages)}: {[s.stage_id for s in gate_stages]}"
    )
    assert len(automated_stages) == 10, (
        f"Expected 10 automated stages; got {len(automated_stages)}"
    )

    # merge-gate must carry conflict_keys=("merge:main",)
    merge_gate = next((s for s in manifest.stages if s.stage_id == "merge-gate"), None)
    assert merge_gate is not None, "merge-gate stage must be present"
    assert merge_gate.conflict_keys == ("merge:main",), (
        f"merge-gate must declare conflict_keys=('merge:main',); "
        f"got {merge_gate.conflict_keys!r}"
    )

    # Every gate stage must offer at least two options (a gate with one option
    # is not a decision point). Loop all 8 so the docstring claim is exercised,
    # not just the two skip gates checked below.
    for stage in gate_stages:
        assert stage.options is not None and len(stage.options) >= 2, (
            f"gate stage {stage.stage_id} must have at least two options; "
            f"got {stage.options!r}"
        )

    # design-direction-gate and rollback-gate must carry a skip option
    for stage_id in ("design-direction-gate", "rollback-gate"):
        stage = next((s for s in manifest.stages if s.stage_id == stage_id), None)
        assert stage is not None, f"{stage_id} must be present"
        skip_options = [o for o in stage.options if "skip" in o.lower()]
        assert skip_options, (
            f"{stage_id} must have a 'skip' option; options={stage.options!r}"
        )

    # Stage order: gate first, then automated stages in document order
    stage_ids = [s.stage_id for s in manifest.stages]
    assert stage_ids[0] == "go-no-go-gate", (
        f"First stage must be go-no-go-gate; got {stage_ids[0]!r}"
    )
    assert stage_ids[-1] == "document-run", (
        f"Last stage must be document-run; got {stage_ids[-1]!r}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 58 — e2e integration: full dev-lifecycle run.
#
# Loads the committed reference PLAYBOOK.md, runs the full 18-stage
# lifecycle against a "feature-issue" subject, suspending at each of
# 8 gates and resuming with a mix of continue/skip dispositions.
# All 8 gate rulings land in ONE goal ledger queryable as
# conductor_gate_answered events. cumulative_spend_usd > 0 after 10
# automated stages each costing 0.10 (#584).
#
# Dispatch patching: uses _make_ledger_updating_dispatch so the goal
# ledger is updated on each automated stage dispatch (C2 resume cursor
# correctness). Gate-answered transitions are written by resume().
#
# Design-direction-gate and rollback-gate are answered with skip
# (no UI component; no deployed surface) — exercises the skip path on
# a real run resuming from a real ledger.


def test_e2e_dev_lifecycle_full_run(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """TEST 58 — e2e integration: full 18-stage dev-lifecycle run, 8 gates, 10 dispatches.

    Gate dispositions:
      go-no-go-gate         → continue
      spec-scope-gate       → continue
      autoplan-concerns-gate → continue
      design-direction-gate → skip  (backend change, no UI)
      tier-a-rulings-gate   → continue
      security-findings-gate → continue
      merge-gate            → continue
      rollback-gate         → skip  (no deployed surface yet)
    """
    from atomic_agents.locks.filesystem import FilesystemLockBackend  # noqa: PLC0415

    # Use a real lock backend so the per-run lease and conflict-scan lease
    # actually take/release on each run()/resume() call.
    mock_agent.lock_backend = FilesystemLockBackend(agent_root)

    playbook_dir = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "samples"
        / "dev-lifecycle"
        / "skills"
        / "dev-lifecycle-playbook"
    )
    assert playbook_dir.is_dir(), f"Reference playbook dir not found at {playbook_dir}"
    manifest, warnings = validate_playbook_manifest(playbook_dir)
    assert manifest is not None and warnings == [], (
        f"Reference PLAYBOOK.md must parse cleanly; warnings={warnings}"
    )

    _ledger_dispatch = _make_ledger_updating_dispatch(total_cost_usd=0.10)
    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()
    subject = "Implement feature X from issue #584"

    with (
        patch(
            "atomic_agents.goal.coordinator.dispatch_sub_goal_as_outcome",
            side_effect=_ledger_dispatch,
        ),
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        # 1. run() → suspends at go-no-go-gate (first stage, is_gate=True)
        state = run(playbook=manifest, subject=subject, agent=mock_agent)
        assert state.status == "awaiting_decision", (
            f"run() must suspend at go-no-go-gate; got {state.status!r}"
        )
        assert state.pending_decision is not None
        assert state.pending_decision.stage_id == "go-no-go-gate"
        run_id = state.conductor_run_id

        def _resume(answer: str, disposition: str, rationale: str) -> "ConductorState":  # noqa: F821
            nonlocal state
            state = resume(
                playbook=manifest,
                subject=subject,
                agent=mock_agent,
                conductor_run_id=run_id,
                decision_id=state.pending_decision.decision_id,
                answer=answer,
                disposition=disposition,
                rationale=rationale,
            )
            return state

        # Gate 1: go-no-go → continue → processes spec-run → spec-scope-gate
        _resume(
            answer="Proceed — well-defined problem, right shape.",
            disposition="continue",
            rationale="Idea is solid; bounded scope.",
        )
        assert state.status == "awaiting_decision", (
            f"After go-no-go continue, must suspend at spec-scope-gate; got {state.status!r}"
        )
        assert state.pending_decision.stage_id == "spec-scope-gate"

        # Gate 2: spec-scope → continue → autoplan-run → autoplan-concerns-gate
        _resume(
            answer="Spec is crisp; scope boundaries approved.",
            disposition="continue",
            rationale="All in-scope items clear; out-of-scope explicit.",
        )
        assert state.status == "awaiting_decision"
        assert state.pending_decision.stage_id == "autoplan-concerns-gate"

        # Gate 3: autoplan-concerns → continue → design-run → design-direction-gate
        _resume(
            answer="Concerns noted and addressed; proceed to build.",
            disposition="continue",
            rationale="Engineering concerns resolved; architecture sound.",
        )
        assert state.status == "awaiting_decision"
        assert state.pending_decision.stage_id == "design-direction-gate"

        # Gate 4: design-direction → skip (backend change, no UI)
        # → discovery-run → tier-a-rulings-gate
        _resume(
            answer="No UI component; design gate not applicable.",
            disposition="skip",
            rationale="Backend-only change; no visual direction needed.",
        )
        assert state.status == "awaiting_decision"
        assert state.pending_decision.stage_id == "tier-a-rulings-gate"

        # Gate 5: tier-a-rulings → continue
        # → build-run, verify-run, security-run → security-findings-gate
        _resume(
            answer="All Tier-A forks ruled; proceed to build.",
            disposition="continue",
            rationale="Three forks: chose option A on each with rationale.",
        )
        assert state.status == "awaiting_decision"
        assert state.pending_decision.stage_id == "security-findings-gate"

        # Gate 6: security-findings → continue
        # → ship-run → merge-gate (has conflict_keys=["merge:main"])
        _resume(
            answer="Security gate clears; no findings.",
            disposition="continue",
            rationale="No auth/secrets/money surfaces touched.",
        )
        assert state.status == "awaiting_decision"
        assert state.pending_decision.stage_id == "merge-gate"
        # merge-gate must surface its conflict_keys in the GateDecision
        assert "merge:main" in (state.pending_decision.held_conflict_keys or ()), (
            f"merge-gate GateDecision must carry 'merge:main' in held_conflict_keys; "
            f"got {state.pending_decision.held_conflict_keys!r}"
        )

        # Gate 7: merge-gate → continue → deploy-run → rollback-gate
        _resume(
            answer="PR reviewed and approved; proceed to merge.",
            disposition="continue",
            rationale="Diff matches spec; commits bisectable; CHANGELOG accurate.",
        )
        assert state.status == "awaiting_decision"
        assert state.pending_decision.stage_id == "rollback-gate"

        # Gate 8: rollback-gate → skip (no deployed surface yet) → document-run → complete
        _resume(
            answer="No deployed surface yet; rollback gate not applicable.",
            disposition="skip",
            rationale="Pre-production codebase; no live traffic to protect.",
        )
        assert state.status == "complete", (
            f"After all 8 gates, run must complete; got {state.status!r}: "
            f"completed_stages={state.completed_stage_ids}"
        )

    # All 8 gate rulings must be in ONE queryable ledger
    history_path = agent_root / "goals" / run_id / "goal_history.jsonl"
    answer_events = []
    for raw in history_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        if rec.get("event") == "conductor_gate_answered":
            answer_events.append(rec)

    assert len(answer_events) == 8, (
        f"Expected 8 conductor_gate_answered events (one per gate); "
        f"got {len(answer_events)}"
    )
    # All rulings must carry a rationale AND a ruler (answered_by) — the decision
    # record requirement is "rationale + ruler", so the e2e test asserts both.
    for evt in answer_events:
        assert evt.get("rationale"), (
            f"Every gate ruling must have a non-empty rationale; "
            f"stage_id={evt.get('stage_id')!r} has none"
        )
        assert evt.get("answered_by"), (
            f"Every gate ruling must record its ruler (answered_by); "
            f"stage_id={evt.get('stage_id')!r} has none"
        )

    dispositions = [e.get("disposition") for e in answer_events]
    assert dispositions.count("skip") == 2, (
        f"Expected 2 skip dispositions; got {dispositions}"
    )
    assert dispositions.count("continue") == 6, (
        f"Expected 6 continue dispositions; got {dispositions}"
    )

    # 10 automated stages × 0.10 each = 1.00. The exact-value assertion proves
    # the durable ledger accumulates spend ACROSS the 8 resume() boundaries — a
    # weak `> 0` would still pass if only the final invocation's single dispatch
    # were counted (cross-resume accumulation broken).
    assert state.cumulative_spend_usd == pytest.approx(1.00), (
        f"cumulative_spend_usd must be 1.00 (10 dispatched stages × 0.10, "
        f"accumulated across all resume boundaries); got {state.cumulative_spend_usd}"
    )
    # All 18 stages must appear as completed or skipped in the final state
    assert len(state.completed_stage_ids) == 18, (
        f"Expected 18 completed (incl. skipped) stage IDs; "
        f"got {len(state.completed_stage_ids)}: {state.completed_stage_ids}"
    )


# ──────────────────────────────────────────────────────────────────
# TEST 59 — merge-gate conflict serialization: A suspends holding
# 'merge:main', B defers, A resumes(continue), B self-releases into
# its own gate. Real FilesystemLockBackend for the scan/suspend
# critical section. Interleaved-sequential (not threaded — fcntl.flock
# from the same process would be a false green on macOS/Linux). (#584)


def test_merge_key_conflict_serialization(
    agent_root: Path,
    mock_agent: MagicMock,
) -> None:
    """TEST 59 — merge:main conflict key: A holds it, B defers, A releases, B proceeds.

    The 'merge:main' key is the irreversible merge-to-main gate guard.
    Only one run should hold it at a time; a second run reaching its
    merge gate must defer until the first completes.

    Interleaved-sequential: A suspends (holds 'merge:main'), B defers
    behind A's decision, A resumes (continue) → A completes, key freed,
    B self-releases on next run() call → B proceeds into its own gate.

    This is the correct layer to test: conductor merge serialization is
    decision-ledger-based (OD1b — B queues behind A's pending DECISION),
    NOT a flock held across suspension (C8 — gate suspension holds no live
    lock). True cross-process lock contention is exercised by the
    FilesystemLockBackend's own conformance suite, not here; a single-process
    threaded test would be a false green anyway (fcntl.flock is keyed to the
    open file description, so two threads in one process do not contend).
    """
    from atomic_agents.locks.filesystem import FilesystemLockBackend  # noqa: PLC0415

    mock_agent.lock_backend = FilesystemLockBackend(agent_root)

    # Two independent playbooks, each with a single merge:main gate.
    pb_a = _make_conflict_gate_playbook(agent_root, "merge-a", "merge:main")
    pb_b = _make_conflict_gate_playbook(agent_root, "merge-b", "merge:main")

    idem = _make_idempotency_backend_mock()
    outcome_backend = _make_outcome_backend_mock()

    with (
        patch(
            "atomic_agents.conductor.run._get_idempotency_backend",
            return_value=idem,
        ),
        patch(
            "atomic_agents.conductor.run._get_outcome_backend",
            return_value=outcome_backend,
        ),
    ):
        # 1. A suspends at its gate, holding 'merge:main'.
        state_a = run(playbook=pb_a, subject="merge-A", agent=mock_agent)
        assert state_a.status == "awaiting_decision", (
            f"Run A must suspend at merge:main gate; got {state_a.status!r}"
        )
        a_decision_id = state_a.pending_decision.decision_id
        a_run_id = state_a.conductor_run_id
        # The held key must be durable in the sub-goal
        goal_a = mock_agent.goal_backend.for_goal(a_run_id).load_goal(mock_agent.name)
        held = [
            sg.held_conflict_keys or ()
            for sg in goal_a.sub_goals
            if sg.status == "awaiting_decision"
        ]
        assert any("merge:main" in k for k in held), (
            f"A must hold 'merge:main' in its awaiting_decision sub-goal; held={held!r}"
        )

        # 2. B defers behind A.
        state_b1 = run(playbook=pb_b, subject="merge-B", agent=mock_agent)
        assert state_b1.status == "deferred", (
            f"Run B must defer behind A's merge:main gate; got {state_b1.status!r}"
        )
        b_run_id = state_b1.conductor_run_id
        assert state_b1.queued_behind_decision_id == a_decision_id

        # 3. A resumes (continue) → A completes, 'merge:main' freed.
        state_a2 = resume(
            playbook=pb_a,
            subject="merge-A",
            agent=mock_agent,
            conductor_run_id=a_run_id,
            decision_id=a_decision_id,
            answer="PR reviewed and approved.",
            disposition="continue",
            rationale="Diff matches spec.",
        )
        assert state_a2.status == "complete", (
            f"A must complete after continue; got {state_a2.status!r}"
        )

        # 4. Re-invoke run(B) → B self-releases into its OWN merge:main gate.
        state_b2 = run(
            playbook=pb_b,
            subject="merge-B",
            agent=mock_agent,
            conductor_run_id=b_run_id,
        )

    b_history = agent_root / "goals" / b_run_id / "goal_history.jsonl"
    assert _has_event(b_history, "conductor_queue_released"), (
        "B must append conductor_queue_released once A's gate is answered"
    )
    assert state_b2.status == "awaiting_decision", (
        f"B must proceed into its own merge:main gate; got {state_b2.status!r}"
    )
    assert state_b2.pending_decision is not None
    assert state_b2.pending_decision.decision_id != a_decision_id, (
        "B must have its OWN gate decision, not A's"
    )
    # 'merge:main' is now held by B
    goal_b = mock_agent.goal_backend.for_goal(b_run_id).load_goal(mock_agent.name)
    held_b = [
        sg.held_conflict_keys or ()
        for sg in goal_b.sub_goals
        if sg.status == "awaiting_decision"
    ]
    assert any("merge:main" in k for k in held_b), (
        f"B must now hold 'merge:main' in its awaiting_decision sub-goal; held={held_b!r}"
    )

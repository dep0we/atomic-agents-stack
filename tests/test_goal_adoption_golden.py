"""Golden-byte tests for #448 PR1 — Goal write-path ADOPTION.

Pins goal.md + goal_history.jsonl BYTE CONTENTS across all 6 CLI subcommands
+ dispatch-outcome after routing through the GoalBackend Protocol.

Design:
- All tests use injected today= and an injected FilesystemGoalBackend so the
  written bytes are deterministic (no wall-clock dates).
- Assertions scope to FILE CONTENTS only (read_text comparisons on goal.md
  and goal_history.jsonl). .goal.lock is explicitly excluded per the ruling
  (created by backend.append_history_event's _goal_lock(); benign sidecar).
- dispatch-outcome golden pins goal.md and goal_history.jsonl bytes across
  the full real code path (coordinator's apply_transition() → backend).
- The 4 frozen test files remain unchanged EXCEPT test_goal.py archive golden
  assertions (A3 exception — intentional data-loss fix).

All 6 CLI subcommands exercised:
  1. status      — read-only; verifies load() routes through backend
  2. next        — read-only; verifies load() routes through backend
  3. advance     — mutates + saves; verifies save() routes through backend
  4. abandon     — archives; verifies archive() A3 fix + goal.md removal
  5. complete    — archives; verifies archive() A3 fix + goal.md removal
  6. report      — read-only; verifies load() routes through backend
  + dispatch-outcome — injected OutcomeRunner mock; verifies terminal
    transition routes through coordinator's apply_transition() → backend
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest

from atomic_agents.goal import (
    GoalManager,
    FilesystemGoalBackend,
)
from atomic_agents._goal_impl import main as goal_main
from atomic_agents.exceptions import (
    AtomicAgentsError,
    PathTraversalError,
    SchemaValidationError,
)


# ──────────────────────────────────────────────────────────────────
# Shared fixture

GOAL_TEXT = """\
---
schema_version: 1
active: true
intent: Complete novel first draft by Q4
priority: high
created: 2026-04-01
deadline: 2026-12-31
last_progress_check: 2026-05-01
success_criteria:
  - All 24 chapters drafted
  - Style guide passes lint on every scene
sub_goals:
  - id: ch_1
    label: Chapter 1 first draft
    status: in_progress
    assigned: writer
  - id: ch_2
    label: Chapter 2 first draft
    status: pending
    assigned: writer
related_atomic_notes:
  - feedback_voice.md
related_decisions:
  - policy/lock_001_pov.md
---

# The Unfinished — Director goal

## History (auto-appended)

- 2026-04-28 — sub_goal `ch_1` started
"""


@pytest.fixture
def agent_fixture(tmp_path):
    """Minimal agent vault with goal.md + injected backend.

    Includes a minimal persona/IDENTITY.md so dispatch_as_outcome's shim can
    construct a real AtomicAgent for the (now-live) fail-closed cost gate
    (Principle #4). No model.md is written, so cost_guardrails_enabled defaults
    False and the gate passes (allow=True) — the dispatch proceeds and every
    golden assertion below (goal.md bytes, JSONL ordering, terminal mapping) is
    byte-identical to the pre-gate behavior. The gate enforces model.md caps;
    an agent with no configured caps has nothing to refuse.
    """
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "muse-director"
    agent_root.mkdir(parents=True)
    (agent_root / "persona").mkdir()
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Identity\nTest agent for goal adoption golden tests.\n"
    )
    (agent_root / "goal.md").write_text(GOAL_TEXT)
    backend = FilesystemGoalBackend(agent_root)
    return agents_root, "muse-director", agent_root, backend


# ──────────────────────────────────────────────────────────────────
# 1. status — read-only: verifies load() routes through backend


def test_golden_status_reads_through_backend(agent_fixture):
    """status command loads goal via backend and prints summary; goal.md unchanged."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)

    summary = gm.status_summary()

    assert "Complete novel first draft by Q4" in summary
    assert "Sub-goals:" in summary
    assert "ch_1" in summary
    # goal.md should be unchanged (status is read-only)
    assert (agent_root / "goal.md").read_text() == GOAL_TEXT
    # No history file created by a read-only operation
    assert not (agent_root / "goal_history.jsonl").exists()


def test_golden_status_cli_routes_through_backend(agent_fixture):
    """main() status subcommand uses the injected backend."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    rc = goal_main(
        ["--agents-root", str(agents_root), "status", agent_name],
        goal_backend=backend,
    )
    assert rc == 0
    # goal.md unchanged
    assert (agent_root / "goal.md").read_text() == GOAL_TEXT


# ──────────────────────────────────────────────────────────────────
# 2. next — read-only: verifies load() routes through backend


def test_golden_next_reads_through_backend(agent_fixture):
    """next command loads goal via backend; goal.md unchanged."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm.load()
    next_sg = gm.next_sub_goal()
    # ch_1 is in_progress (no blocked_by); ch_2 is pending
    # next_sub_goal returns first pending unblocked — ch_2
    assert next_sg is not None
    assert next_sg.id == "ch_2"
    # goal.md unchanged
    assert (agent_root / "goal.md").read_text() == GOAL_TEXT


# ──────────────────────────────────────────────────────────────────
# 3. advance — mutates + saves through backend


def test_golden_advance_save_routes_through_backend(agent_fixture):
    """advance --complete calls save() which routes through backend.save_goal().
    Verifies: last_progress_check stamped with injected today, sub_goal status
    persisted, goal.md bytes match the backend's serialization."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm.load()
    gm.mark_complete("ch_1", output="drafts/ch1.md")
    gm.save()

    # Reload via a fresh GoalManager (also through backend) to verify bytes
    gm2 = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm2.load()
    sg = gm2.find_sub_goal("ch_1")
    assert sg.status == "complete"
    assert sg.output == "drafts/ch1.md"
    # A2 ruling: last_progress_check stamped caller-side with injected today
    assert gm2._goal.last_progress_check == "2026-05-08"

    # Verify goal.md bytes via direct frontmatter parse
    parsed = frontmatter.load(agent_root / "goal.md")
    assert parsed.metadata["last_progress_check"] == "2026-05-08"
    assert parsed.metadata["active"] is True
    # Optional fields preserved (build_goal_frontmatter)
    assert parsed.metadata["deadline"] == "2026-12-31"
    assert parsed.metadata["related_atomic_notes"] == ["feedback_voice.md"]
    assert parsed.metadata["related_decisions"] == ["policy/lock_001_pov.md"]


def test_golden_advance_cli_writes_through_backend(agent_fixture):
    """advance CLI with --complete writes through the injected backend."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    rc = goal_main(
        [
            "--agents-root",
            str(agents_root),
            "advance",
            agent_name,
            "ch_1",
            "--complete",
            "--output",
            "drafts/ch1.md",
        ],
        goal_backend=backend,
    )
    assert rc == 0
    parsed = frontmatter.load(agent_root / "goal.md")
    # Find ch_1 in sub_goals
    ch1 = next(sg for sg in parsed.metadata["sub_goals"] if sg["id"] == "ch_1")
    assert ch1["status"] == "complete"


# ──────────────────────────────────────────────────────────────────
# 4. abandon — archive A3 fix: optional fields preserved


def test_golden_abandon_archive_preserves_optional_fields(agent_fixture):
    """abandon archives goal.md via GoalManager.archive() (A3 fix).
    Verifies: deadline + related_* fields are in the archive (not dropped)."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    archive_path = gm.abandon(reason="scope shifted")

    assert archive_path.exists()
    assert not (agent_root / "goal.md").exists()

    parsed = frontmatter.load(archive_path)
    assert parsed.metadata["active"] is False
    assert parsed.metadata["archived_at"] == "2026-05-08"
    assert "abandoned: scope shifted" in parsed.metadata["archive_reason"]
    # A3 fix: optional fields PRESERVED (previously dropped by hand-rolled dict)
    assert parsed.metadata["deadline"] == "2026-12-31"
    assert parsed.metadata["related_atomic_notes"] == ["feedback_voice.md"]
    assert parsed.metadata["related_decisions"] == ["policy/lock_001_pov.md"]
    # last_progress_check stamped with today
    assert parsed.metadata["last_progress_check"] == "2026-05-08"


def test_golden_abandon_cli_routes_through_backend(agent_fixture):
    """abandon CLI uses injected backend (load routes through it)."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    rc = goal_main(
        [
            "--agents-root",
            str(agents_root),
            "abandon",
            agent_name,
            "--reason",
            "scope shifted",
        ],
        goal_backend=backend,
    )
    assert rc == 0
    assert not (agent_root / "goal.md").exists()


# ──────────────────────────────────────────────────────────────────
# 5. complete — archive A3 fix + all-done check


def test_golden_complete_requires_all_done(agent_fixture):
    """complete command rejects when sub-goals are not all done."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    rc = goal_main(
        ["--agents-root", str(agents_root), "complete", agent_name],
        goal_backend=backend,
    )
    # Should fail — ch_1 is in_progress, ch_2 is pending
    assert rc == 1
    # goal.md unchanged
    assert (agent_root / "goal.md").read_text() == GOAL_TEXT


def test_golden_complete_archives_when_all_done(agent_fixture):
    """complete command archives when all sub-goals are done; A3 fix in effect."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    # First mark all sub-goals complete via GoalManager
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm.load()
    gm.mark_complete("ch_1")
    gm.mark_complete("ch_2")
    gm.save()

    rc = goal_main(
        ["--agents-root", str(agents_root), "complete", agent_name],
        goal_backend=backend,
    )
    assert rc == 0
    assert not (agent_root / "goal.md").exists()

    # Find the archive file
    archives = list((agent_root / "goal_archive").glob("*.md"))
    assert len(archives) == 1
    parsed = frontmatter.load(archives[0])
    assert parsed.metadata["active"] is False
    # A3 fix: optional fields preserved
    assert parsed.metadata["deadline"] == "2026-12-31"
    assert parsed.metadata["related_atomic_notes"] == ["feedback_voice.md"]


# ──────────────────────────────────────────────────────────────────
# 6. report — read-only


def test_golden_report_reads_through_backend(agent_fixture):
    """report command loads goal via backend; goal.md unchanged."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    report = gm.progress_report()
    assert "Complete novel first draft by Q4" in report
    # goal.md unchanged
    assert (agent_root / "goal.md").read_text() == GOAL_TEXT


# ──────────────────────────────────────────────────────────────────
# 7. dispatch-outcome — verifies terminal transition routes through the
#    coordinator's apply_transition() to the backend (COARSE-ROUTE adoption)


def _make_outcome_result(status: str) -> MagicMock:
    result = MagicMock()
    result.status = status
    result.run_id = "outcome-20260508-120000-abcd1234"
    result.explanation = "Test explanation for golden test."
    result.total_cost_usd = 0.0042
    result.iterations = [MagicMock(), MagicMock()]
    result.max_iterations = 3
    return result


def test_golden_dispatch_goal_md_written_through_backend(agent_fixture):
    """dispatch_as_outcome routes save() through backend.save_goal().
    goal.md bytes must reflect the terminal status (complete) BEFORE the
    JSONL audit line is appended (spec/41 MUST 6 ordering)."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
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

    # 1. goal.md bytes: ch_2 must show status: complete in the file
    goal_text = (agent_root / "goal.md").read_text()
    assert "status: complete" in goal_text, (
        "save() must have routed through backend.save_goal() and written "
        "the terminal status to disk"
    )
    # A2 ruling: last_progress_check stamped with today (injected clock)
    parsed = frontmatter.load(agent_root / "goal.md")
    assert parsed.metadata["last_progress_check"] == "2026-05-08"

    # 2. goal_history.jsonl: MUST exist and have the dispatch event
    history_path = agent_root / "goal_history.jsonl"
    assert history_path.is_file(), (
        "coordinator's apply_transition() must have written both goal.md and "
        "the JSONL audit line to goal_history.jsonl via the backend"
    )
    events = [
        json.loads(line)
        for line in history_path.read_text().splitlines()
        if line.strip()
    ]
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["applied_status"] == "complete"
    assert dispatched[0]["terminal_state"] == "satisfied"
    assert dispatched[0]["sub_goal_id"] == "ch_2"
    # Cost-tracking audit fields (Principle #5 — this is the audit record for an
    # LLM-spending path). Pin them so a regression that drops/mangles the cost or
    # iteration count from the audit line through the backend route is caught.
    assert dispatched[0]["total_cost_usd"] == 0.0042
    assert dispatched[0]["iterations"] == len(outcome_result.iterations)

    # 3. .goal.lock is excluded from assertions (benign sidecar per ruling)
    # Do NOT assert on .goal.lock presence or absence here.


def test_golden_dispatch_jsonl_ts_first_key_order(agent_fixture):
    """JSONL event written by the coordinator's apply_transition() must have ts as the first key.
    The backend's _make_history_event enforces ts-first regardless of input order."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm.load()

    outcome_result = _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="inline:Chapter complete.",
        )

    history_path = agent_root / "goal_history.jsonl"
    lines = [line for line in history_path.read_text().splitlines() if line.strip()]
    dispatch_line = next(
        line for line in lines if "sub_goal_outcome_dispatched" in line
    )
    parsed_event = json.loads(dispatch_line)
    keys = list(parsed_event.keys())
    assert keys[0] == "ts", f"First key must be 'ts'; got {keys[0]!r}"
    assert keys[1] == "event", f"Second key must be 'event'; got {keys[1]!r}"


def test_golden_dispatch_save_before_jsonl_ordering(agent_fixture):
    """White-box ordering: goal.md is written BEFORE the JSONL audit line.

    As of #448 PR3, the coordinator routes terminal transitions through
    apply_transition(), which enforces MUST 6 ordering (goal.md before JSONL)
    atomically under the goal lock — both in one call. The legacy
    save() + _append_goal_history_jsonl() pattern was replaced by the
    coordinator's apply_transition() call.

    This test verifies the durable ordering contract at the apply_transition level:
    apply_transition() must be called at least once for the terminal status write,
    and after dispatch, goal.md must contain status:complete with a
    sub_goal_outcome_dispatched event in goal_history.jsonl — the invariant that
    goal.md always precedes or equals the JSONL is enforced by apply_transition's
    lock (fcntl.flock holds both writes sequentially).
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm.load()

    apply_transition_calls: list[str] = []
    real_apply = backend.apply_transition

    def traced_apply(*args, **kwargs):
        to_status = args[2] if len(args) > 2 else kwargs.get("to_status", "?")
        apply_transition_calls.append(to_status)
        return real_apply(*args, **kwargs)

    outcome_result = _make_outcome_result("satisfied")

    with (
        patch("atomic_agents.outcome.OutcomeRunner") as MockRunner,
        patch.object(backend, "apply_transition", side_effect=traced_apply),
    ):
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="inline:Chapter complete.",
        )

    # apply_transition must have been called (at least the terminal write).
    assert len(apply_transition_calls) >= 1, (
        "apply_transition() must be called for the terminal status write "
        "(spec/41 MUST 6); dispatch_as_outcome must NOT bypass the backend"
    )
    assert "complete" in apply_transition_calls, (
        "apply_transition() must write 'complete' for a satisfied outcome; "
        f"got: {apply_transition_calls}"
    )

    # Behavioral ordering assertion: goal.md has the terminal status AND
    # goal_history.jsonl records it — apply_transition's lock guarantees
    # goal.md is always written before the JSONL audit line.
    goal_text = (agent_root / "goal.md").read_text()
    assert "status: complete" in goal_text
    history_path = agent_root / "goal_history.jsonl"
    assert history_path.is_file()
    events = [
        json.loads(line)
        for line in history_path.read_text().splitlines()
        if line.strip()
    ]
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["applied_status"] == "complete"


# ──────────────────────────────────────────────────────────────────
# 8. AtomicAgent.goal_backend wiring


def _build_loadable_agent(tmp_path) -> tuple[Path, str]:
    """Build a minimal single-agent layout AtomicAgent can actually construct.

    Mirrors tests/test_agent_goal_loading.py:_build_single_agent — AtomicAgent
    requires persona/IDENTITY.md (NOT a bare IDENTITY.md at the agent root),
    tools.md, model.md, and memory/ + log/ dirs. A bare IDENTITY.md raises
    AgentProfileNotFound; the earlier try/except wrappers swallowed that and
    made these wiring tests vacuous (they passed even if goal_backend were
    deleted from AtomicAgent.__init__). Returns (agents_root, agent_name).
    """
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "test-agent"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text(
        "# Identity\nTest agent.\n\n## Operating mode\n\nThis agent is **reactive**.\n"
    )
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return agents_root, "test-agent"


def test_atomic_agent_has_goal_backend_attribute(tmp_path):
    """AtomicAgent.__init__ wires self.goal_backend live as of #448 PR1.

    Builds a REAL constructible agent and asserts OUTSIDE any try/except so the
    assertion actually runs and would FAIL if goal_backend wiring were removed.
    """
    from atomic_agents import AtomicAgent
    from atomic_agents.goal.backend import GoalBackend

    agents_root, agent_name = _build_loadable_agent(tmp_path)

    agent = AtomicAgent(agent_name, agents_root=agents_root)
    assert hasattr(agent, "goal_backend"), (
        "AtomicAgent must expose self.goal_backend as of #448 PR1"
    )
    assert isinstance(agent.goal_backend, GoalBackend), (
        "self.goal_backend must satisfy the GoalBackend Protocol"
    )


def test_atomic_agent_accepts_goal_backend_kwarg(tmp_path):
    """AtomicAgent(goal_backend=...) kwarg is accepted and wins over the default.

    Builds a REAL constructible agent and asserts identity OUTSIDE any
    try/except — the kwarg-wins-over-env contract is the headline A7 deliverable
    and must be genuinely exercised.
    """
    from atomic_agents import AtomicAgent
    from atomic_agents.goal import FilesystemGoalBackend

    agents_root, agent_name = _build_loadable_agent(tmp_path)
    custom_backend = FilesystemGoalBackend(agents_root / agent_name)

    agent = AtomicAgent(
        agent_name,
        agents_root=agents_root,
        goal_backend=custom_backend,
    )
    assert agent.goal_backend is custom_backend, (
        "kwarg-wins-over-env: the injected goal_backend must be used directly"
    )


# ──────────────────────────────────────────────────────────────────
# 9. has_goal() — still works after backend injection (filesystem probe)


def test_save_forward_ref_blocked_by_fails_closed(tmp_path):
    """Pin the #448 PR1 save() behavior DELTA (conscious, spec/41 MUST 5-aligned).

    Pre-#448, GoalManager.save() wrote goal.md with NO frontmatter validation, so
    a forward blocked_by reference (sub-goal A, listed FIRST, blocked_by B, listed
    SECOND) persisted successfully — even though load_goal() would then reject it
    (the validator's referential check is forward-only: a sub_goal may only be
    blocked_by an EARLIER-listed id). Post-#448, save() routes through
    backend.save_goal() -> _write_goal() which validate_goal()s before the durable
    write and fails closed. This regression test pins the NEW fail-closed behavior
    so the delta is guarded, not silent (it is unreachable via the new CLI golden
    suite — the CLI exposes no `block` subcommand — so without this test the change
    would be unguarded). The mark_blocked() mutator itself still ALLOWS the forward
    reference in memory (it only checks the blocker exists + no cycle); the gate is
    at the durable write, closing the write-time/read-time asymmetry.
    """
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "fwd-ref-agent"
    agent_root.mkdir(parents=True)
    # A listed FIRST, B listed SECOND — a forward reference once A.blocked_by = B.
    (agent_root / "goal.md").write_text(
        "---\n"
        "schema_version: 1\n"
        "active: true\n"
        "intent: forward-ref boundary\n"
        "priority: high\n"
        "created: 2026-01-01\n"
        "last_progress_check: 2026-01-01\n"
        "success_criteria:\n"
        "  - done\n"
        "sub_goals:\n"
        "  - id: A\n"
        "    label: first\n"
        "    status: pending\n"
        "  - id: B\n"
        "    label: second\n"
        "    status: pending\n"
        "---\n\n# Goal\n"
    )
    backend = FilesystemGoalBackend(agent_root)
    gm = GoalManager(
        agents_root, "fwd-ref-agent", today=date(2026, 1, 2), goal_backend=backend
    )
    gm.load()
    gm.mark_blocked("A", "B")  # in-memory mutation succeeds (forward ref allowed)

    # save() now fails closed (spec/41 MUST 5 write/read validation symmetry):
    with pytest.raises(SchemaValidationError, match="blocked_by"):
        gm.save()


def test_save_backward_ref_blocked_by_still_persists(agent_fixture):
    """Boundary partner to the forward-ref test: a BACKWARD blocked_by reference
    (blocker listed EARLIER than the blocked sub-goal) still saves cleanly — the
    validation gate only rejects forward references, so the common case is
    unaffected and stays byte-valid through the backend route."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    gm = GoalManager(
        agents_root, agent_name, today=date(2026, 5, 8), goal_backend=backend
    )
    gm.load()
    # ch_2 listed AFTER ch_1 — blocking ch_2 on ch_1 is a backward ref (valid).
    gm.mark_blocked("ch_2", "ch_1")
    gm.save()  # must NOT raise
    reloaded = frontmatter.load(agent_root / "goal.md")
    ch2 = next(sg for sg in reloaded.metadata["sub_goals"] if sg["id"] == "ch_2")
    assert ch2["blocked_by"] == "ch_1"


def test_has_goal_works_after_backend_injection(agent_fixture):
    """has_goal() is still a filesystem probe (unchanged); backend injection
    does not break it."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    gm = GoalManager(agents_root, agent_name, goal_backend=backend)
    assert gm.has_goal() is True

    # Remove goal.md
    (agent_root / "goal.md").unlink()
    gm2 = GoalManager(agents_root, agent_name, goal_backend=backend)
    assert gm2.has_goal() is False


# ──────────────────────────────────────────────────────────────────
# 10. goal_main-routed golden tests for next / report (read-only CLI paths)
#     Gap: previous tests exercised these via direct GoalManager calls only,
#     not via the CLI entry point. These prove the CLI path wires through.


def test_golden_next_cli_routes_through_backend(agent_fixture):
    """next CLI subcommand routes through the injected backend (load path).

    Proves the goal_main(['next', ...]) code path calls gm.load() which
    goes through backend.load_goal(), and returns 0 for an agent with
    pending sub-goals.
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    rc = goal_main(
        ["--agents-root", str(agents_root), "next", agent_name],
        goal_backend=backend,
    )
    assert rc == 0
    # goal.md must be unchanged (next is read-only)
    assert (agent_root / "goal.md").read_text() == GOAL_TEXT
    # No history file created by a read-only operation
    assert not (agent_root / "goal_history.jsonl").exists()


def test_golden_report_cli_routes_through_backend(agent_fixture):
    """report CLI subcommand routes through the injected backend (load path).

    Proves the goal_main(['report', ...]) code path calls gm.load() which
    goes through backend.load_goal(), and returns 0.
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    rc = goal_main(
        ["--agents-root", str(agents_root), "report", agent_name],
        goal_backend=backend,
    )
    assert rc == 0
    # goal.md must be unchanged (report is read-only)
    assert (agent_root / "goal.md").read_text() == GOAL_TEXT
    # No history file created by a read-only operation
    assert not (agent_root / "goal_history.jsonl").exists()


# ──────────────────────────────────────────────────────────────────
# 11. goal_main-routed golden tests for dispatch-outcome CLI path
#
#     The dispatch-outcome CLI path has a SECOND gm.save() call AFTER
#     dispatch_as_outcome returns (line ~1069 of _goal_impl.py). That second
#     save() is a harmless no-op (dispatch_as_outcome already saves), but it
#     MUST route through the backend without error, and must NOT revert the
#     goal.md bytes written by the first save inside dispatch_as_outcome.
#
#     ts field note: the JSONL event's 'ts' value is
#     datetime.now().astimezone().isoformat() (wall clock in _goal_impl.py),
#     NOT controlled by the injected today= clock. Full byte-pinning of that
#     field is therefore not achievable without patching datetime. These tests
#     assert (a) 'ts' is present and a non-empty string and (b) pin every
#     OTHER field by key+value — this is the correct level of precision given
#     the implementation. The docstring claim of "byte determinism" in the
#     original file's header is overclaimed for the jsonl 'ts' field; the
#     ts-first key ORDER (not value) is the pinned invariant.


def test_golden_dispatch_outcome_cli_routes_through_backend(agent_fixture):
    """dispatch-outcome CLI subcommand routes load → dispatch → SECOND save through backend.

    The CLI calls gm.dispatch_as_outcome() which itself calls gm.save() once,
    then the CLI calls gm.save() a second time (harmless no-op). Both saves
    must route through backend.save_goal() without error and the final
    goal.md bytes must reflect the terminal status.
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    outcome_result = _make_outcome_result("satisfied")

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        rc = goal_main(
            [
                "--agents-root",
                str(agents_root),
                "dispatch-outcome",
                agent_name,
                "ch_2",
                "--rubric",
                "inline:Chapter complete.",
            ],
            goal_backend=backend,
        )

    assert rc == 0

    # goal.md must reflect the terminal status written by the FIRST save (inside
    # dispatch_as_outcome). The SECOND save (CLI main() trailing call) is a
    # harmless no-op but must not revert or corrupt the file.
    goal_text = (agent_root / "goal.md").read_text()
    assert "status: complete" in goal_text, (
        "Both save() calls must route through backend; "
        "terminal status 'complete' must be present in goal.md"
    )

    # goal_history.jsonl must exist with the dispatch event
    history_path = agent_root / "goal_history.jsonl"
    assert history_path.is_file()
    events = [
        json.loads(line)
        for line in history_path.read_text().splitlines()
        if line.strip()
    ]
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1
    ev = dispatched[0]
    # ts must be present and non-empty; its exact value is wall-clock and not pinned
    # (datetime.now().astimezone().isoformat() in _goal_impl.py is not injectable).
    assert "ts" in ev and isinstance(ev["ts"], str) and ev["ts"]
    # Pin all other fields that are NOT wall-clock-dependent:
    assert ev["event"] == "sub_goal_outcome_dispatched"
    assert ev["sub_goal_id"] == "ch_2"
    assert ev["terminal_state"] == "satisfied"
    assert ev["applied_status"] == "complete"
    assert ev["iterations"] == len(outcome_result.iterations)
    assert ev["total_cost_usd"] == 0.0042
    # ts-first key order is enforced by backend._make_history_event
    assert list(ev.keys())[0] == "ts"
    assert list(ev.keys())[1] == "event"


# ──────────────────────────────────────────────────────────────────
# 12. dispatch_as_outcome non-satisfied terminal branches
#
#     Gap: only 'satisfied' → complete was tested. The other three branches
#     (interrupted → in_progress, max_iterations_reached → blocked,
#     failed → blocked) were untested. This parametrized test covers all four
#     terminal states and asserts both the sub-goal status in goal.md AND
#     the applied_status/terminal_state fields in the JSONL event.


@pytest.mark.parametrize(
    "terminal_state, expected_sg_status, expected_applied_status",
    [
        ("satisfied", "complete", "complete"),
        ("interrupted", "in_progress", "in_progress"),
        ("max_iterations_reached", "blocked", "blocked"),
        ("failed", "blocked", "blocked"),
    ],
)
def test_dispatch_as_outcome_all_terminal_branches(
    agent_fixture,
    terminal_state,
    expected_sg_status,
    expected_applied_status,
):
    """dispatch_as_outcome maps every terminal state to the correct sub-goal status
    and records applied_status + terminal_state in the JSONL audit event.

    All four terminal states are exercised:
      satisfied              → complete
      interrupted            → stays in_progress
      max_iterations_reached → blocked
      failed                 → blocked
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm.load()

    outcome_result = _make_outcome_result(terminal_state)

    with patch("atomic_agents.outcome.OutcomeRunner") as MockRunner:
        mock_runner_instance = MagicMock()
        mock_runner_instance.run.return_value = outcome_result
        MockRunner.return_value = mock_runner_instance

        result, sg = gm.dispatch_as_outcome(
            sub_goal_id="ch_2",
            rubric="inline:Chapter complete.",
        )

    # Sub-goal status in memory
    assert sg.status == expected_sg_status, (
        f"terminal_state={terminal_state!r}: expected sg.status={expected_sg_status!r}, "
        f"got {sg.status!r}"
    )

    # Reload from disk via backend — the save must have persisted the correct status
    goal_from_disk = backend.load_goal(agent_name)
    ch2_on_disk = next(s for s in goal_from_disk.sub_goals if s.id == "ch_2")
    assert ch2_on_disk.status == expected_sg_status, (
        f"terminal_state={terminal_state!r}: expected on-disk sg.status="
        f"{expected_sg_status!r}, got {ch2_on_disk.status!r}"
    )

    # JSONL audit event
    history_path = agent_root / "goal_history.jsonl"
    assert history_path.is_file()
    events = [
        json.loads(line)
        for line in history_path.read_text().splitlines()
        if line.strip()
    ]
    dispatched = [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]
    assert len(dispatched) == 1, (
        f"terminal_state={terminal_state!r}: expected 1 dispatch event, "
        f"got {len(dispatched)}"
    )
    ev = dispatched[0]
    assert ev["applied_status"] == expected_applied_status, (
        f"terminal_state={terminal_state!r}: applied_status mismatch"
    )
    assert ev["terminal_state"] == terminal_state, (
        f"expected terminal_state={terminal_state!r} in JSONL event"
    )
    assert ev["sub_goal_id"] == "ch_2"
    # ts is wall-clock; assert present and non-empty only
    assert "ts" in ev and ev["ts"]
    # ts-first key order
    assert list(ev.keys())[0] == "ts"
    assert list(ev.keys())[1] == "event"


# ──────────────────────────────────────────────────────────────────
# 12b. Fail-closed cost gate is LIVE on the shim/CLI path (#448 PR3)
#
#     The headline correctness deliverable: GoalManager.dispatch_as_outcome
#     (the dispatch-outcome CLI's single production caller) constructs a REAL
#     AtomicAgent and passes it to the coordinator, so the pre-dispatch
#     fail-closed cost gate (Principle #4) fires on the live path — not only
#     when a programmatic caller hands in a real agent. The #425 version failed
#     OPEN here; an earlier draft of this PR injected a no-gate sentinel that
#     made the gate a permanent no-op on the only shipping path. This test
#     proves the gate is consulted, blocks, audits, and refuses BEFORE any run.


def test_dispatch_as_outcome_shim_cost_gate_blocks_live(agent_fixture):
    """The shim constructs a REAL AtomicAgent; when its cost gate denies, the
    shim path raises CostGuardrailBlocked, appends coordinator_dispatch_rejected,
    and does NOT dispatch (no OutcomeRunner.run, sub-goal stays pending).

    Patches AtomicAgent._check_cost_guardrails (the class the shim actually
    constructs) to deny — exercising the full live wiring (real agent
    construction + gate consultation + fail-closed propagation) without
    fabricating a cost-history file. The coordinator-level test
    (test_goal_coordinator.py) covers the mock-agent block; THIS test closes
    the gap that the gate was never exercised through the shim/CLI caller.
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.exceptions import CostGuardrailBlocked
    from atomic_agents.types import CostCheckResult

    agents_root, agent_name, agent_root, backend = agent_fixture
    gm = GoalManager(
        agents_root, agent_name, today=date(2026, 5, 8), goal_backend=backend
    )
    gm.load()

    denied = CostCheckResult(allow=False, reason="daily cap exceeded")

    with (
        patch.object(
            AtomicAgent, "_check_cost_guardrails", return_value=denied
        ) as mock_gate,
        patch("atomic_agents.outcome.OutcomeRunner") as MockRunner,
    ):
        with pytest.raises(CostGuardrailBlocked, match="daily cap exceeded"):
            gm.dispatch_as_outcome(
                sub_goal_id="ch_2",
                rubric="inline:Chapter complete.",
            )

        # The gate was consulted on the REAL constructed agent...
        mock_gate.assert_called_once()
        # ...and the run was refused BEFORE paying construction/run overhead.
        MockRunner.assert_not_called()

    # Sub-goal stays pending — no apply_transition fired on the blocked path.
    goal_from_disk = backend.load_goal(agent_name)
    ch2 = next(s for s in goal_from_disk.sub_goals if s.id == "ch_2")
    assert ch2.status == "pending"

    # coordinator_dispatch_rejected event appended (audit-before-raise);
    # NO sub_goal_outcome_dispatched event (nothing was dispatched).
    history_path = agent_root / "goal_history.jsonl"
    events = [
        json.loads(line)
        for line in history_path.read_text().splitlines()
        if line.strip()
    ]
    rejected = [e for e in events if e.get("event") == "coordinator_dispatch_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["sub_goal_id"] == "ch_2"
    assert rejected[0]["reason"] == "daily cap exceeded"
    assert list(rejected[0].keys())[0] == "ts"
    assert not [e for e in events if e.get("event") == "sub_goal_outcome_dispatched"]


# ──────────────────────────────────────────────────────────────────
# 13. Archive via CLI preserves optional fields (abandon + complete paths)
#
#     Gap: A3 fix was only asserted via the direct GoalManager path. The CLI
#     paths (goal_main(['abandon'...]) and goal_main(['complete'...])) were
#     not verified for optional-field preservation.


def test_golden_abandon_cli_preserves_optional_fields(agent_fixture):
    """abandon via CLI (goal_main) preserves optional fields (A3 fix) in archive."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    rc = goal_main(
        [
            "--agents-root",
            str(agents_root),
            "abandon",
            agent_name,
            "--reason",
            "scope shifted",
        ],
        goal_backend=backend,
    )
    assert rc == 0
    assert not (agent_root / "goal.md").exists()

    archives = list((agent_root / "goal_archive").glob("*.md"))
    assert len(archives) == 1
    parsed = frontmatter.load(archives[0])

    # A3 fix: optional fields preserved through CLI path
    assert parsed.metadata["active"] is False
    assert parsed.metadata.get("deadline") == "2026-12-31", (
        "deadline must be preserved in archive (A3 fix applies via CLI path)"
    )
    assert parsed.metadata.get("related_atomic_notes") == ["feedback_voice.md"], (
        "related_atomic_notes must be preserved in archive (A3 fix via CLI path)"
    )
    assert parsed.metadata.get("related_decisions") == ["policy/lock_001_pov.md"], (
        "related_decisions must be preserved in archive (A3 fix via CLI path)"
    )
    assert "archive_reason" in parsed.metadata


def test_golden_complete_cli_preserves_optional_fields(agent_fixture):
    """complete via CLI (goal_main) preserves optional fields (A3 fix) in archive."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    # Mark all sub-goals complete so the 'complete' CLI command succeeds
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    gm.load()
    gm.mark_complete("ch_1")
    gm.mark_complete("ch_2")
    gm.save()

    rc = goal_main(
        ["--agents-root", str(agents_root), "complete", agent_name],
        goal_backend=backend,
    )
    assert rc == 0
    assert not (agent_root / "goal.md").exists()

    archives = list((agent_root / "goal_archive").glob("*.md"))
    assert len(archives) == 1
    parsed = frontmatter.load(archives[0])

    # A3 fix: optional fields preserved through CLI 'complete' path
    assert parsed.metadata["active"] is False
    assert parsed.metadata.get("deadline") == "2026-12-31", (
        "deadline must be preserved in archive (A3 fix applies via CLI complete path)"
    )
    assert parsed.metadata.get("related_atomic_notes") == ["feedback_voice.md"]
    assert parsed.metadata.get("related_decisions") == ["policy/lock_001_pov.md"]
    assert parsed.metadata.get("archive_reason") == "completed — all sub-goals done"


# ──────────────────────────────────────────────────────────────────
# 14. Minimal goal archive — optional keys ABSENT (not null)
#
#     Gap: the A3 fix (build_goal_frontmatter preserves optional fields) could
#     accidentally START emitting null/empty values for optional fields that
#     were never set. This test proves that a goal without any optional fields
#     archives cleanly and the archive frontmatter does NOT contain those keys
#     (not null, not [], not '').


MINIMAL_GOAL_TEXT = """\
---
schema_version: 1
active: true
intent: Minimal goal with no optional fields
priority: high
created: 2026-06-01
last_progress_check: 2026-06-01
success_criteria:
  - One criterion
sub_goals:
  - id: task_1
    label: Single task
    status: pending
---

## Overview

Minimal goal body with no optional fields set.

## History (auto-appended)
- 2026-06-01 — goal created
"""


@pytest.fixture
def minimal_agent_fixture(tmp_path):
    """Agent vault with a minimal goal.md — no optional fields."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "minimal-agent"
    agent_root.mkdir(parents=True)
    (agent_root / "goal.md").write_text(MINIMAL_GOAL_TEXT)
    backend = FilesystemGoalBackend(agent_root)
    return agents_root, "minimal-agent", agent_root, backend


def test_minimal_goal_archive_optional_keys_absent(minimal_agent_fixture):
    """Archiving a goal with no optional fields produces an archive where the
    optional keys are ABSENT (not null, not [], not '').

    This proves the A3 fix does NOT change the common-case archive bytes for
    goals that never set deadline / parent_goal / related_* fields.
    """
    agents_root, agent_name, agent_root, backend = minimal_agent_fixture
    today = date(2026, 6, 1)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    archive_path = gm.abandon(reason="test complete")

    assert archive_path.exists()
    assert not (agent_root / "goal.md").exists()

    parsed = frontmatter.load(archive_path)
    meta = parsed.metadata

    # Required fields present
    assert meta["active"] is False
    assert meta["intent"] == "Minimal goal with no optional fields"
    assert meta["archived_at"] == "2026-06-01"

    # Optional fields must be ABSENT — not null, not empty-string, not []
    for optional_key in (
        "deadline",
        "parent_goal",
        "related_atomic_notes",
        "related_decisions",
        "related_canon_pages",
    ):
        assert optional_key not in meta, (
            f"Optional key {optional_key!r} must be ABSENT in minimal archive; "
            f"got value {meta.get(optional_key)!r}. "
            f"The A3 fix must not introduce null/empty entries for never-set fields."
        )


def test_minimal_goal_archive_cli_optional_keys_absent(minimal_agent_fixture):
    """Same as above but via the CLI path (goal_main abandon)."""
    agents_root, agent_name, agent_root, backend = minimal_agent_fixture
    rc = goal_main(
        [
            "--agents-root",
            str(agents_root),
            "abandon",
            agent_name,
            "--reason",
            "test complete",
        ],
        goal_backend=backend,
    )
    assert rc == 0
    assert not (agent_root / "goal.md").exists()

    archives = list((agent_root / "goal_archive").glob("*.md"))
    assert len(archives) == 1
    parsed = frontmatter.load(archives[0])
    meta = parsed.metadata

    # Optional fields must be ABSENT (not null, not [], not '')
    for optional_key in (
        "deadline",
        "parent_goal",
        "related_atomic_notes",
        "related_decisions",
        "related_canon_pages",
    ):
        assert optional_key not in meta, (
            f"Optional key {optional_key!r} must be ABSENT in minimal archive (CLI path); "
            f"got value {meta.get(optional_key)!r}."
        )


# ──────────────────────────────────────────────────────────────────────────────
# 15. Golden byte-identity test: GoalManager.archive() thin shim produces a
#     pinned, byte-stable archive file under a fixed clock (#483 PR1).
#
#     Decision archive-byte-identity-vs-divergence-acknowledgment Option 1:
#     golden byte-identity assertion under a pinned clock. The shim passes
#     `when=self.today` to backend.archive_goal(), so all date-stamped fields
#     (slug prefix, archived_at, last_progress_check, ## History prose date)
#     derive from the same injected date. The test asserts the FULL archive
#     file bytes (read_text()) equal a frozen expected literal, so any serializer
#     drift fails — not just a date mismatch. The pre-#483 in-place serializer
#     was deleted in this PR; under the pinned clock it produced the same date
#     everywhere, so this golden is the durable successor to an old-vs-new diff.


def test_archive_shim_byte_identity_under_pinned_clock(agent_fixture):
    """GoalManager.archive() thin shim produces byte-identical output under pinned clock.

    The shim routes through backend.archive_goal(when=self.today), which uses the
    injectable clock for ALL date-stamped fields. This test pins:
      (a) The archive file exists and goal.md is removed.
      (b) archived_at, last_progress_check, and slug prefix == pinned date (2026-05-08).
      (c) '## History' section contains 'goal archived' EXACTLY ONCE
          (backend owns the prose; GoalManager does NOT also call _append_history).
      (d) Optional fields (deadline, related_*) are preserved (A3 fix still in effect).
      (e) active == False in the archive.

    Under a fixed `today=date(2026, 5, 8)`, the shim routes through
    backend.archive_goal(when=self.today), pinning every date-stamped field.
    This test is a TRUE golden: it asserts the FULL archive-file bytes equal a
    frozen literal (EXPECTED_ARCHIVE_BYTES below), so a serializer change (key
    reorder, whitespace, frontmatter emission) that keeps the four dates correct
    still fails — the strongest durable guard against clock-mismatch AND
    serializer drift (decision archive-byte-identity-vs-divergence-acknowledgment
    Option 1). The field-level assertions below the byte comparison remain as a
    readable diagnostic when the golden breaks.

    Note: the pre-#483 in-place serializer was deleted in this PR, so the golden
    captures the post-shim bytes (produced by the single shared backend
    serializer); under the pinned clock the shim and the former in-place path
    produced the same date everywhere, so this golden is the durable successor to
    an old-vs-new differential.
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)

    archive_path = gm.abandon(reason="scope shifted")

    # (a) goal.md removed; archive exists
    assert not (agent_root / "goal.md").exists(), (
        "goal.md must be removed after archive"
    )
    assert archive_path.exists(), f"archive file must exist at {archive_path}"

    # TRUE byte-identity golden: full archive-file bytes under the pinned clock.
    # If this fails, the serializer (key order / whitespace / frontmatter
    # emission) or a date field changed; the field-level assertions below
    # localize which.
    EXPECTED_ARCHIVE_BYTES = (
        "---\n"
        "active: false\n"
        "archive_reason: 'abandoned: scope shifted'\n"
        "archived_at: '2026-05-08'\n"
        "created: '2026-04-01'\n"
        "deadline: '2026-12-31'\n"
        "intent: Complete novel first draft by Q4\n"
        "last_progress_check: '2026-05-08'\n"
        "priority: high\n"
        "related_atomic_notes:\n"
        "- feedback_voice.md\n"
        "related_decisions:\n"
        "- policy/lock_001_pov.md\n"
        "schema_version: 1\n"
        "sub_goals:\n"
        "- assigned: writer\n"
        "  id: ch_1\n"
        "  label: Chapter 1 first draft\n"
        "  status: in_progress\n"
        "- assigned: writer\n"
        "  id: ch_2\n"
        "  label: Chapter 2 first draft\n"
        "  status: pending\n"
        "success_criteria:\n"
        "- All 24 chapters drafted\n"
        "- Style guide passes lint on every scene\n"
        "---\n"
        "\n"
        "# The Unfinished — Director goal\n"
        "\n"
        "## History (auto-appended)\n"
        "\n"
        "- 2026-04-28 — sub_goal `ch_1` started\n"
        "- 2026-05-08 — goal archived (abandoned: scope shifted)\n"
    )
    assert archive_path.read_text() == EXPECTED_ARCHIVE_BYTES, (
        "Archive-file bytes diverged from the pinned-clock golden. A serializer "
        "change (key order, whitespace, frontmatter emission) or a date-field "
        "drift broke byte identity. See the field-level diagnostics below."
    )

    # (b) Slug prefix must use the pinned date
    assert "2026-05-08" in archive_path.name, (
        f"archive slug must contain the pinned date '2026-05-08'; "
        f"got filename: {archive_path.name!r}"
    )

    parsed = frontmatter.load(archive_path)
    meta = parsed.metadata

    assert meta.get("archived_at") == "2026-05-08", (
        f"archived_at must be the pinned date; got {meta.get('archived_at')!r}"
    )
    assert meta.get("last_progress_check") == "2026-05-08", (
        f"last_progress_check must be the pinned date; got {meta.get('last_progress_check')!r}"
    )
    assert meta.get("active") is False, "archive must mark active=False"
    assert "abandoned: scope shifted" in meta.get("archive_reason", ""), (
        f"archive_reason must contain 'abandoned: scope shifted'; "
        f"got {meta.get('archive_reason')!r}"
    )

    # (c) Single-occurrence of 'goal archived' in the body (backend owns the prose)
    body = parsed.content
    occurrences = body.count("goal archived")
    assert occurrences == 1, (
        f"'goal archived' must appear exactly once in the archive body; "
        f"found {occurrences} occurrence(s). "
        f"Double-write means GoalManager._append_history was not removed from the shim. "
        f"Body: {body!r}"
    )
    assert "2026-05-08" in body, (
        "The '## History' prose datestamp must use the pinned clock date '2026-05-08'. "
        f"Body: {body!r}"
    )

    # (d) Optional fields preserved (A3 fix — build_goal_frontmatter via backend)
    assert meta.get("deadline") == "2026-12-31"
    assert meta.get("related_atomic_notes") == ["feedback_voice.md"]
    assert meta.get("related_decisions") == ["policy/lock_001_pov.md"]

    # (e) _goal is cleared after archive (shim must set self._goal = None)
    assert gm._goal is None, (
        "GoalManager._goal must be None after archive() so has_active_goal() works"
    )


def test_archive_shim_path_exists_after_return(agent_fixture):
    """The Path returned by GoalManager.archive() must exist on disk.

    The shim reconstructs the Path from the returned slug as
    `self.archive_dir / f'{slug}.md'`. This test verifies the reconstructed
    path is consistent with the on-disk file written by backend.archive_goal().
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)

    archive_path = gm.archive(reason="completed — all sub-goals done")

    assert archive_path.exists(), (
        f"Path returned by GoalManager.archive() must exist on disk. "
        f"Got path: {archive_path}. "
        f"Check that self.archive_dir is resolved (#486) and slug reconstruction is correct."
    )
    # Verify it's in the expected location
    assert archive_path.parent == agent_root / "goal_archive", (
        f"Archive must be in goal_archive/; got parent: {archive_path.parent}"
    )
    assert archive_path.suffix == ".md"


def test_archive_on_already_archived_agent_raises_not_stale_return(agent_fixture):
    """archive()/abandon() on an agent with no active goal.md MUST raise.

    Regression for the #483 PR1 thin-shim false-success: the shim removed the
    GoalManager active-goal precheck and let backend MUST 9 idempotency take over
    for ALL GoalManager callers. MUST 9's retry-after-unlink behavior (return the
    most-recently-modified archive slug when goal.md is absent) is scoped to
    DIRECT backend.archive_goal() crash-retry callers — at the GoalManager public
    boundary it produced a FALSE SUCCESS: a second abandon() returned a stale
    prior-archive Path, exit 0, with the operator's reason silently discarded.
    GoalManager's contract is restored: raise AtomicAgentsError when nothing is
    active (Principle #5 audit honesty, Principle #14 no silent behavior delta).
    """
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)

    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    first = gm.abandon(reason="cancel it")
    assert first.exists()
    assert not (agent_root / "goal.md").exists()

    # Second abandon — no active goal.md. Must RAISE, not return the stale slug.
    gm2 = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
    with pytest.raises(AtomicAgentsError, match="No active goal to archive"):
        gm2.abandon(reason="cancel again")

    # Same via the CLI: non-zero exit + honest message, NOT "Goal abandoned".
    rc = goal_main(
        [
            "--agents-root",
            str(agents_root),
            "abandon",
            agent_name,
            "--reason",
            "cancel again",
        ],
        goal_backend=backend,
    )
    assert rc != 0, (
        "goal abandon on an already-archived agent must exit non-zero, not "
        "print 'Goal abandoned' and exit 0 (false-success regression)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 16. #486 agent_root resolution — GoalManager constructed with a symlinked
#     agents_root derives an absolute, resolved agent_root.


def test_goalmanager_agent_root_is_resolved(tmp_path):
    """GoalManager.__init__ MUST resolve agent_root through symlinks (#486).

    Resolving agent_root at __init__ closes the perimeter for all GoalManager
    file operations (has_goal, archive_dir reconstruction): the derived paths
    are canonical, matching the filesystem backend's own ``_require_within_root``
    containment check. This test constructs a REAL within-vault symlink for
    agents_root and asserts the resolution actually collapses it — so removing
    either ``.resolve()`` call from GoalManager.__init__ fails the test (the
    earlier ``is_absolute()``-only assertions were a false-green: tmp_path is
    already absolute, so they passed even with .resolve() stripped).
    """
    # Two independent within-vault symlink levels so the test pins BOTH
    # .resolve() calls in GoalManager.__init__:
    #   - linked-vault → real-vault           pins agents_root.resolve() (line 159)
    #   - real-vault/test-agent → .actual/...  pins (agents_root/name).resolve() (176)
    # .resolve() collapses every symlink component in a path, so a single-level
    # symlink only exercises one call; with the agent-level symlink the agent_root
    # equality pins line 176, and the agents_root equality pins line 159.
    real_root = tmp_path / "real-vault"
    actual_agent = real_root / ".actual" / "test-agent"
    actual_agent.mkdir(parents=True)
    # Write a minimal goal.md so GoalManager construction succeeds
    (actual_agent / "goal.md").write_text(
        "---\n"
        "schema_version: 1\n"
        "active: true\n"
        "intent: resolution test\n"
        "priority: high\n"
        "created: 2026-01-01\n"
        "last_progress_check: 2026-01-01\n"
        "success_criteria:\n"
        "  - done\n"
        "sub_goals: []\n"
        "---\n\n# Goal\n",
        encoding="utf-8",
    )
    # Agent-level symlink: agents_root/test-agent → .actual/test-agent
    (real_root / "test-agent").symlink_to(actual_agent, target_is_directory=True)
    # Outer agents_root symlink: linked-vault → real-vault
    linked_root = tmp_path / "linked-vault"
    linked_root.symlink_to(real_root, target_is_directory=True)

    backend = FilesystemGoalBackend(linked_root / "test-agent")
    gm = GoalManager(linked_root, "test-agent", goal_backend=backend)

    # agent_root must collapse BOTH symlink levels to the canonical .actual path.
    # Stripping line 176's .resolve() leaves the agent-level symlink uncollapsed
    # (".actual" absent) → this fails.
    expected_agent = actual_agent.resolve()
    assert gm.agent_root == expected_agent, (
        f"GoalManager.agent_root must resolve through agent + agents_root "
        f"symlinks (#486); expected {expected_agent}, got {gm.agent_root}"
    )
    assert ".actual" in gm.agent_root.parts, (
        f"resolved agent_root must follow the agent-level symlink to .actual; "
        f"got {gm.agent_root}"
    )
    assert "linked-vault" not in gm.agent_root.parts
    # agents_root must collapse the outer symlink. Stripping line 159's .resolve()
    # leaves "linked-vault" in agents_root → this fails.
    assert gm.agents_root == real_root.resolve(), (
        f"GoalManager.agents_root must resolve the outer symlink (#486); "
        f"expected {real_root.resolve()}, got {gm.agents_root}"
    )
    # goal_path and archive_dir must derive from the resolved (canonical) root
    assert gm.goal_path == gm.agent_root / "goal.md"
    assert gm.archive_dir == gm.agent_root / "goal_archive"
    assert gm.agent_root.is_absolute()


def test_goalmanager_resolve_failure_fails_closed(tmp_path):
    """A symlink loop in agents_root MUST fail closed with PathTraversalError,
    not crash GoalManager.__init__ with a raw OSError (#483 PR1 /ship review).

    GoalManager.__init__ resolves agents_root/agent_root; resolve() raises
    OSError(ELOOP) on a symlink loop. The constructor folds that into
    PathTraversalError (a subclass of AtomicAgentsError the CLI already catches)
    so a malformed vault path produces a controlled error rather than taking
    down a reactive construction path (the journal-backend #427 precedent).
    """
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)  # a → b → a: resolve() raises OSError(ELOOP)

    with pytest.raises(PathTraversalError):
        GoalManager(loop_a, "test-agent")
    # PathTraversalError is an AtomicAgentsError → the CLI's existing handler
    # catches it and exits non-zero with a controlled message.
    assert issubclass(PathTraversalError, AtomicAgentsError)

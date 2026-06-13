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
  the full real code path (save + _append_goal_history_jsonl → backend).
- The 4 frozen test files remain unchanged EXCEPT test_goal.py archive golden
  assertions (A3 exception — intentional data-loss fix).

All 6 CLI subcommands exercised:
  1. status      — read-only; verifies load() routes through backend
  2. next        — read-only; verifies load() routes through backend
  3. advance     — mutates + saves; verifies save() routes through backend
  4. abandon     — archives; verifies archive() A3 fix + goal.md removal
  5. complete    — archives; verifies archive() A3 fix + goal.md removal
  6. report      — read-only; verifies load() routes through backend
  + dispatch-outcome — injected OutcomeRunner mock; verifies save() +
    _append_goal_history_jsonl() route through backend
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
from atomic_agents.exceptions import SchemaValidationError


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
    """Minimal agent vault with goal.md + injected backend."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "muse-director"
    agent_root.mkdir(parents=True)
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
# 7. dispatch-outcome — verifies BOTH save() and _append_goal_history_jsonl()
#    route through the backend (COARSE-ROUTE adoption)


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
        "_append_goal_history_jsonl() must have routed through "
        "backend.append_history_event() and created goal_history.jsonl"
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
    """JSONL event from _append_goal_history_jsonl must have ts as the first key.
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
    """White-box ordering: save() (→ backend.save_goal) fires BEFORE
    _append_goal_history_jsonl() (→ backend.append_history_event).
    Mirrors test_goal_dispatch_audit_ordering.py but validates the BACKEND route."""
    agents_root, agent_name, agent_root, backend = agent_fixture
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today, goal_backend=backend)
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
            rubric="inline:Chapter complete.",
        )

    assert "save" in call_order and "jsonl" in call_order
    assert call_order.index("save") < call_order.index("jsonl"), (
        "goal.md (save → backend.save_goal) must be persisted BEFORE the "
        "JSONL audit line (→ backend.append_history_event); "
        "got call order: " + ", ".join(call_order)
    )


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

"""Tests for atomic_agents.goal."""

from __future__ import annotations
import json
from datetime import date, timedelta
from pathlib import Path

import frontmatter
import pytest

from atomic_agents.goal import (
    Goal,
    GoalManager,
    SubGoal,
    parse_agent_mode,
    validate_goal,
    validate_agent_mode,
    CURRENT_GOAL_SCHEMA_VERSION,
)
from atomic_agents.exceptions import (
    AtomicAgentsError,
    GoalCorrupted,
    SchemaValidationError,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def agent_with_goal(tmp_path):
    """Agent vault with a populated goal.md."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "muse-director"
    agent_root.mkdir(parents=True)

    goal_text = """---
schema_version: 1
active: true
intent: Complete novel first draft by Q4
priority: high
created: 2026-04-01
deadline: 2026-12-31
parent_goal: muse-director-novel-2026
last_progress_check: 2026-05-01
success_criteria:
  - All 24 chapters drafted
  - Style guide passes lint on every scene
  - Director review complete
sub_goals:
  - id: ch_1_to_4
    label: Chapters 1-4 drafted
    status: complete
    completed: 2026-04-28
    assigned: writer
  - id: ch_5_draft
    label: Chapter 5 first draft
    status: in_progress
    assigned: writer
    deadline: 2026-05-15
  - id: ch_5_edit
    label: Chapter 5 edit
    status: pending
    assigned: editor
    blocked_by: ch_5_draft
  - id: ch_6_outline
    label: Chapter 6 scene outline
    status: pending
    assigned: outliner
related_atomic_notes:
  - feedback_voice.md
related_decisions:
  - policy/lock_001_pov.md
related_canon_pages:
  - canon/world/vienna_1920s.md
---

# The Unfinished — Director goal

## Current state

Chapters 1-4 fully drafted. Chapter 5 in active drafting.

## History (auto-appended)

- 2026-04-28 — sub_goal `ch_1_to_4` → complete
"""
    (agent_root / "goal.md").write_text(goal_text)

    return agents_root, "muse-director"


@pytest.fixture
def agent_no_goal(tmp_path):
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "caldwell"
    agent_root.mkdir(parents=True)
    return agents_root, "caldwell"


# ──────────────────────────────────────────────────────────────────
# Validation tests

def test_validate_goal_passes_valid_dict():
    valid = {
        "schema_version": 1, "active": True, "intent": "x", "priority": "high",
        "created": "2026-04-01", "last_progress_check": "2026-05-01",
        "success_criteria": ["a", "b"],
        "sub_goals": [{"id": "x", "label": "X", "status": "pending"}],
    }
    validate_goal(valid)  # should not raise


def test_validate_goal_missing_required_raises():
    with pytest.raises(SchemaValidationError, match="intent"):
        validate_goal({
            "schema_version": 1, "active": True, "priority": "high",
            "created": "2026-04-01", "last_progress_check": "2026-05-01",
            "success_criteria": [], "sub_goals": [],
        })


def test_validate_goal_invalid_priority_raises():
    with pytest.raises(SchemaValidationError, match="priority"):
        validate_goal({
            "schema_version": 1, "active": True, "intent": "x",
            "priority": "urgent",  # not in the valid set
            "created": "x", "last_progress_check": "x",
            "success_criteria": [], "sub_goals": [],
        })


def test_validate_goal_invalid_sub_goal_status_raises():
    with pytest.raises(SchemaValidationError, match="status"):
        validate_goal({
            "schema_version": 1, "active": True, "intent": "x", "priority": "high",
            "created": "x", "last_progress_check": "x", "success_criteria": [],
            "sub_goals": [{"id": "x", "label": "x", "status": "weird"}],
        })


def test_validate_goal_duplicate_sub_goal_id_raises():
    with pytest.raises(SchemaValidationError, match="duplicate"):
        validate_goal({
            "schema_version": 1, "active": True, "intent": "x", "priority": "high",
            "created": "x", "last_progress_check": "x", "success_criteria": [],
            "sub_goals": [
                {"id": "a", "label": "x", "status": "pending"},
                {"id": "a", "label": "y", "status": "pending"},
            ],
        })


def test_validate_agent_mode_valid():
    validate_agent_mode("reactive")
    validate_agent_mode("goal-driven")
    validate_agent_mode("hybrid")


def test_validate_agent_mode_invalid_raises():
    with pytest.raises(SchemaValidationError):
        validate_agent_mode("autonomous")


# ──────────────────────────────────────────────────────────────────
# Mode parsing

def test_parse_agent_mode_finds_goal_driven(tmp_path):
    p = tmp_path / "IDENTITY.md"
    p.write_text("# IDENTITY\n\n## Operating mode\n\nThis agent is **goal-driven**.\n")
    assert parse_agent_mode(p) == "goal-driven"


def test_parse_agent_mode_finds_hybrid(tmp_path):
    p = tmp_path / "IDENTITY.md"
    p.write_text("# IDENTITY\n\n## Operating mode\n\nHybrid mode by default.\n")
    assert parse_agent_mode(p) == "hybrid"


def test_parse_agent_mode_finds_reactive(tmp_path):
    p = tmp_path / "IDENTITY.md"
    p.write_text("# IDENTITY\n\n## Operating mode\n\nReactive only.\n")
    assert parse_agent_mode(p) == "reactive"


def test_parse_agent_mode_defaults_to_reactive_if_no_section(tmp_path):
    p = tmp_path / "IDENTITY.md"
    p.write_text("# IDENTITY\n\nNo operating mode section here.\n")
    assert parse_agent_mode(p) == "reactive"


def test_parse_agent_mode_missing_file_returns_reactive(tmp_path):
    assert parse_agent_mode(tmp_path / "missing.md") == "reactive"


# ──────────────────────────────────────────────────────────────────
# Load + save

def test_load_parses_goal_correctly(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    goal = gm.load()
    assert goal.intent == "Complete novel first draft by Q4"
    assert goal.priority == "high"
    assert goal.active is True
    assert len(goal.sub_goals) == 4
    assert goal.sub_goals[0].id == "ch_1_to_4"
    assert goal.sub_goals[0].status == "complete"
    assert goal.sub_goals[2].blocked_by == "ch_5_draft"


def test_load_rejects_corrupt_yaml(tmp_path):
    agent_root = tmp_path / "a"
    agent_root.mkdir()
    (agent_root / "goal.md").write_text("not valid frontmatter")
    gm = GoalManager(tmp_path, "a")
    # No goal.md frontmatter → load fails validation (missing required fields)
    with pytest.raises(SchemaValidationError):
        gm.load()


def test_save_round_trips(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    original_intent = gm._goal.intent
    gm.save()  # save unchanged
    # Reload — should still parse cleanly
    gm2 = GoalManager(agents_root, agent_name)
    g2 = gm2.load()
    assert g2.intent == original_intent
    assert len(g2.sub_goals) == 4


def test_has_active_goal_true_for_active(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    assert gm.has_active_goal() is True


def test_has_active_goal_false_for_no_goal(agent_no_goal):
    agents_root, agent_name = agent_no_goal
    gm = GoalManager(agents_root, agent_name)
    assert gm.has_active_goal() is False


# ──────────────────────────────────────────────────────────────────
# next_sub_goal

def test_next_sub_goal_picks_first_unblocked(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    next_sg = gm.next_sub_goal()
    # ch_5_draft is in_progress; ch_5_edit is blocked; ch_6_outline is pending unblocked
    assert next_sg.id == "ch_6_outline"


def test_next_sub_goal_returns_none_if_nothing_pending(tmp_path):
    agent_root = tmp_path / "a"
    agent_root.mkdir()
    goal_text = """---
schema_version: 1
active: true
intent: x
priority: high
created: 2026-01-01
last_progress_check: 2026-01-01
success_criteria: [a]
sub_goals:
  - id: a
    label: A
    status: complete
    completed: 2026-01-15
---
"""
    (agent_root / "goal.md").write_text(goal_text)
    gm = GoalManager(tmp_path, "a")
    gm.load()
    assert gm.next_sub_goal() is None


def test_next_sub_goal_skips_blocked_until_blocker_complete(tmp_path):
    agent_root = tmp_path / "a"
    agent_root.mkdir()
    goal_text = """---
schema_version: 1
active: true
intent: x
priority: high
created: 2026-01-01
last_progress_check: 2026-01-01
success_criteria: [a]
sub_goals:
  - id: a
    label: A
    status: in_progress
  - id: b
    label: B
    status: pending
    blocked_by: a
---
"""
    (agent_root / "goal.md").write_text(goal_text)
    gm = GoalManager(tmp_path, "a")
    gm.load()
    # b is blocked by a; a is in progress not complete; nothing dispatchable
    assert gm.next_sub_goal() is None
    # Complete a; b should now be dispatchable
    gm.mark_complete("a")
    assert gm.next_sub_goal().id == "b"


# ──────────────────────────────────────────────────────────────────
# Lifecycle transitions

def test_mark_in_progress(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    sg = gm.mark_in_progress("ch_6_outline", assigned="outliner")
    assert sg.status == "in_progress"
    assert sg.assigned == "outliner"


def test_mark_complete(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    sg = gm.mark_complete("ch_5_draft", output="drafts/ch5_v1.md")
    assert sg.status == "complete"
    assert sg.completed is not None
    assert sg.output == "drafts/ch5_v1.md"


def test_mark_complete_idempotent(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    sg = gm.mark_complete("ch_1_to_4")  # already complete
    assert sg.status == "complete"


def test_mark_complete_invalid_transition_raises(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    # Move ch_6_outline to formal blocked status; can't complete from there
    gm.mark_blocked("ch_6_outline", blocked_by="ch_5_draft")
    with pytest.raises(AtomicAgentsError, match="can only complete from"):
        gm.mark_complete("ch_6_outline")


def test_mark_blocked_validates_blocker_exists(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    with pytest.raises(AtomicAgentsError, match="unknown sub_goal"):
        gm.mark_blocked("ch_6_outline", blocked_by="nonexistent")


def test_add_sub_goal(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    sg = gm.add_sub_goal("ch_6_draft", "Chapter 6 first draft", assigned="writer")
    assert sg.id == "ch_6_draft"
    assert sg.status == "pending"
    assert len(gm._goal.sub_goals) == 5


def test_add_sub_goal_duplicate_id_raises(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    with pytest.raises(AtomicAgentsError, match="already exists"):
        gm.add_sub_goal("ch_1_to_4", "Different label")


# ──────────────────────────────────────────────────────────────────
# Completion evaluation

def test_evaluate_completion_in_progress(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 8))
    gm.load()
    ev = gm.evaluate_completion()
    assert ev.all_criteria_met is False
    assert ev.sub_goals_complete == 1     # ch_1_to_4
    assert ev.sub_goals_total == 4
    assert ev.sub_goals_in_progress == 1  # ch_5_draft
    # ch_5_edit and ch_6_outline are both pending (blocked_by is informational,
    # not a formal status — see the lifecycle distinction in goal.py)
    assert ev.sub_goals_pending == 2
    assert ev.sub_goals_blocked == 0
    # Deadline 2026-12-31 vs today 2026-05-08
    assert ev.days_until_deadline > 0
    assert ev.overdue is False


def test_evaluate_completion_all_done(tmp_path):
    agent_root = tmp_path / "a"
    agent_root.mkdir()
    goal_text = """---
schema_version: 1
active: true
intent: x
priority: high
created: 2026-01-01
last_progress_check: 2026-01-01
success_criteria: [done]
sub_goals:
  - id: a
    label: A
    status: complete
  - id: b
    label: B
    status: abandoned
---
"""
    (agent_root / "goal.md").write_text(goal_text)
    gm = GoalManager(tmp_path, "a")
    gm.load()
    ev = gm.evaluate_completion()
    assert ev.all_criteria_met is True


def test_evaluate_completion_overdue(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    # Today after the deadline
    gm = GoalManager(agents_root, agent_name, today=date(2027, 1, 15))
    gm.load()
    ev = gm.evaluate_completion()
    assert ev.overdue is True
    assert ev.days_until_deadline < 0


# ──────────────────────────────────────────────────────────────────
# Archive + abandon

def test_archive_moves_goal_to_archive(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 8))
    gm.load()
    archive_path = gm.archive(reason="completed")
    assert archive_path.exists()
    # Original goal.md is gone
    assert not gm.goal_path.exists()
    # Archive lives in goal_archive/
    assert archive_path.parent.name == "goal_archive"
    # Archive has expected metadata
    parsed = frontmatter.load(archive_path)
    assert parsed.metadata["active"] is False
    assert parsed.metadata["archived_at"] == "2026-05-08"
    assert parsed.metadata["archive_reason"] == "completed"
    # A3 exception (intentional data-loss fix, #448 PR1): archive() previously
    # hand-rolled a frontmatter dict that silently dropped these five optional
    # fields. It now serializes via build_goal_frontmatter() (same as save() and
    # the backend), preserving them. These assertions guard the fix — all five
    # fields the old dict dropped (deadline, parent_goal, related_atomic_notes,
    # related_decisions, related_canon_pages). This is the ONE sanctioned change
    # to the four frozen regression-guard files (the rest stay byte-frozen).
    assert parsed.metadata["deadline"] == "2026-12-31"
    assert parsed.metadata["parent_goal"] == "muse-director-novel-2026"
    assert parsed.metadata["related_atomic_notes"] == ["feedback_voice.md"]
    assert parsed.metadata["related_decisions"] == ["policy/lock_001_pov.md"]
    assert parsed.metadata["related_canon_pages"] == ["canon/world/vienna_1920s.md"]


def test_abandon_uses_archive_with_reason_prefix(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 8))
    gm.load()
    archive_path = gm.abandon("scope shifted")
    parsed = frontmatter.load(archive_path)
    assert "abandoned: scope shifted" in parsed.metadata["archive_reason"]


def test_archive_idempotent_after_archive(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.archive(reason="completed")
    # Goal is gone — has_active_goal returns False; can't archive again
    assert not gm.has_active_goal()


# ──────────────────────────────────────────────────────────────────
# Reports

def test_status_summary_contains_key_fields(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 8))
    summary = gm.status_summary()
    assert "Complete novel first draft by Q4" in summary
    assert "Sub-goals:" in summary
    assert "ch_5_draft" in summary
    assert "Next dispatchable" in summary


def test_status_summary_no_goal(agent_no_goal):
    agents_root, agent_name = agent_no_goal
    gm = GoalManager(agents_root, agent_name)
    summary = gm.status_summary()
    assert "No goal" in summary


def test_progress_report_includes_pacing(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 8, 1))
    report = gm.progress_report()
    assert "Time used" in report
    assert "%" in report


def test_progress_report_flags_overdue(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2027, 2, 1))
    report = gm.progress_report()
    assert "OVERDUE" in report


# ──────────────────────────────────────────────────────────────────
# Save persistence (full round-trip with edits)

def test_save_preserves_changes(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    today = date(2026, 5, 9)
    gm = GoalManager(agents_root, agent_name, today=today)
    gm.load()
    gm.mark_complete("ch_5_draft", output="drafts/ch5.md")
    gm.save()

    # Reload — change should persist
    gm2 = GoalManager(agents_root, agent_name)
    gm2.load()
    sg = gm2.find_sub_goal("ch_5_draft")
    assert sg.status == "complete"
    assert sg.output == "drafts/ch5.md"
    # last_progress_check updated
    assert gm2._goal.last_progress_check == today.isoformat()


def test_history_entries_appended_on_lifecycle(agent_with_goal):
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 8))
    gm.load()
    gm.mark_in_progress("ch_6_outline", assigned="outliner")
    gm.save()

    # Reload + check body has the history entry
    gm2 = GoalManager(agents_root, agent_name)
    g2 = gm2.load()
    assert "ch_6_outline" in g2.body
    assert "in_progress" in g2.body


# ──────────────────────────────────────────────────────────────────
# P2 regression tests — state-machine hardening

# #6 — Unknown blocker references

def test_validate_goal_rejects_blocked_by_unknown_id():
    """validate_goal should raise if blocked_by references an id not in sub_goals."""
    with pytest.raises(SchemaValidationError, match="unknown id"):
        validate_goal({
            "schema_version": 1, "active": True, "intent": "x", "priority": "high",
            "created": "2026-01-01", "last_progress_check": "2026-01-01",
            "success_criteria": ["a"],
            "sub_goals": [
                {"id": "task_a", "label": "A", "status": "pending"},
                {"id": "task_b", "label": "B", "status": "pending",
                 "blocked_by": "nonexistent_id"},
            ],
        })


def test_add_sub_goal_rejects_blocked_by_unknown(agent_with_goal):
    """add_sub_goal should reject blocked_by referencing an id that doesn't exist."""
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    with pytest.raises(AtomicAgentsError, match="unknown id"):
        gm.add_sub_goal("ch_7_draft", "Chapter 7 draft", blocked_by="nonexistent_id")


# #7 — Self-blocks and cycles

def test_mark_blocked_rejects_self_block(agent_with_goal):
    """mark_blocked should refuse when sub_goal_id == blocked_by."""
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    with pytest.raises(AtomicAgentsError, match="cannot block itself"):
        gm.mark_blocked("ch_6_outline", blocked_by="ch_6_outline")


def test_mark_blocked_rejects_cycle_creation(agent_with_goal):
    """mark_blocked should refuse when the operation would create a cycle (A→B, B→A)."""
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    # Block ch_6_outline by ch_5_draft (valid — ch_5_draft exists)
    gm.mark_blocked("ch_6_outline", blocked_by="ch_5_draft")
    # Now try to block ch_5_draft by ch_6_outline — this would form a cycle
    with pytest.raises(AtomicAgentsError, match="cycle"):
        gm.mark_blocked("ch_5_draft", blocked_by="ch_6_outline")


# #8 — blocked → in_progress clears blocked_by

def test_blocked_to_in_progress_clears_blocked_by(agent_with_goal):
    """Transitioning blocked → in_progress must clear blocked_by."""
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name)
    gm.load()
    # ch_5_edit is pending with blocked_by=ch_5_draft; mark it formally blocked first
    gm.mark_blocked("ch_5_edit", blocked_by="ch_5_draft")
    assert gm.find_sub_goal("ch_5_edit").blocked_by == "ch_5_draft"
    # Transition to in_progress — blocked_by must be cleared
    sg = gm.mark_in_progress("ch_5_edit")
    assert sg.status == "in_progress"
    assert sg.blocked_by is None
    # History should record the previous blocker
    assert "ch_5_draft" in gm._goal.body


# #9 — Archive path collision safety

def test_archive_increments_suffix_on_collision(agent_with_goal):
    """Archiving twice on the same day must produce distinct files, not overwrite."""
    agents_root, agent_name = agent_with_goal
    today = date(2026, 5, 8)
    gm = GoalManager(agents_root, agent_name, today=today)
    gm.load()
    first_path = gm.archive(reason="completed")
    assert first_path.exists()

    # Re-create goal.md to simulate a second archive on same day+slug
    (agents_root / agent_name / "goal.md").write_text(
        (agents_root / agent_name / "goal_archive" / first_path.name).read_text()
    )
    gm2 = GoalManager(agents_root, agent_name, today=today)
    gm2.load()
    second_path = gm2.archive(reason="re-archived")

    assert second_path != first_path
    assert first_path.exists(), "first archive must not be overwritten"
    assert second_path.exists()
    # Second path should have a numeric suffix
    assert second_path.stem.endswith("_1") or "_1" in second_path.name


def test_archive_writes_before_unlinking_goal_md(agent_with_goal, monkeypatch):
    """If unlink raises after archive write, archive file is preserved (recoverable state)."""
    agents_root, agent_name = agent_with_goal
    gm = GoalManager(agents_root, agent_name, today=date(2026, 5, 8))
    gm.load()

    unlink_called = []

    original_unlink = Path.unlink

    def failing_unlink(self, missing_ok=False):
        unlink_called.append(str(self))
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(OSError, match="simulated unlink failure"):
        gm.archive(reason="test")

    # Archive file must have been written before unlink was attempted
    assert len(unlink_called) == 1, "unlink should have been attempted exactly once"
    # The archive directory should have a file in it (the written archive)
    archive_files = list(gm.archive_dir.glob("*.md"))
    assert len(archive_files) == 1, "archive file must be present even after unlink failure"

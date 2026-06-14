"""Conformance tests for GoalBackend Protocol (spec/41).

60 test cases covering the GoalBackend Implementer Contract (the protocol-
behavior subset is parametrized over every registered backend via the
``backend`` / ``backend_with_goal`` fixtures; see PARAMETRIZATION below).

NOTE ON NUMBERING: the "TEST N" labels below are this file's own granular
test-case indices, NOT spec/41 MUST numbers (spec/41 has exactly 10 MUSTs).
Each test case maps to its governing spec/41 MUST in the trailing parens so a
contributor reconciling code-to-spec lands on the right requirement.

  TEST 1 — side-effect-free construction (spec/41 MUST 1)
  TEST 2 — capability honesty (spec/41 MUST 2)
  TEST 3 — goal_text() read-only slice, returns '' when absent (spec/41 MUST 3)
  TEST 4 — save_goal() write-what-I-give-you, no side-effect mutations (spec/41 MUST 4)
  TEST 5 — load_goal() round-trips what save_goal() wrote (spec/41 MUST 5)
  TEST 6 — apply_transition() is a single atomic unit, goal.md + JSONL (spec/41 MUST 6)
  TEST 7 — archive_goal() no-data-loss write ordering (spec/41 MUST 7)
  TEST 8 — archive_goal() collision-safe suffix loop (spec/41 MUST 8)
  TEST 9 — archive_goal() idempotency on retry-after-unlink (spec/41 MUST 9)
  TEST 10 — list_archived() returns [] for absent archive dir (spec/41 MUST 2 capability)
  TEST 11 — read_schema_version() returns int or None on the happy path (raises GoalCorrupted on a corrupt/non-int schema_version — see spec/41 schema-version note)
  TEST 12 — backend_id stability (spec/41 MUST 2)
  TEST 13 — GoalCapabilities field types correct (spec/41 MUST 2)
  TEST 14 — GoalCapabilities defaults allow minimal positional construction (spec/41 MUST 2)
  TEST 15 — supports_canonical_export=True for filesystem (spec/41 MUST 2 / spec/40)
  TEST 16 — export() returns GoalExport with correct fields (spec/41 export contract)
  TEST 17 — export() returns empty GoalExport for agent with no goal.md (spec/41 export contract)
  TEST 18 — export() CRLF normalization (spec/41 export contract / spec/40 Tier A)
  TEST 19 — Goal/SubGoal are mutable, not frozen=True (spec/41 mutable-dataclass note)
  TEST 20 — apply_transition() updates both goal.md and goal_history.jsonl (spec/41 MUST 6)
  TEST 21 — apply_transition() ts-first key order in JSONL (spec/41 JSONL key ordering)
  TEST 22 — append_history_event() ts-first key order (spec/41 JSONL key ordering)
  TEST 23 — export() archived_goals_with_bytes populated after archive (spec/41 export contract)
  TEST 24 — path-traversal guard on construction (spec/41 MUST 1)
  TEST 25 — GoalCapabilities(backend_id='x') valid with all defaults False (spec/41 MUST 2)
  TEST 26 — apply_transition() rejects an invalid to_status fail-closed (spec/41 MUST 6)
  TEST 27 — apply_transition() fields={'status': ...} cannot bypass the enum gate (spec/41 MUST 6)
  TEST 28 — goal_text() returns content when goal.md present (spec/41 MUST 3)
  TEST 29 — apply_transition() dual-write of goal.md + JSONL, both contents verified (spec/41 MUST 6)
  TEST 30 — apply_transition() rejects invalid status BEFORE any write — no orphan JSONL (spec/41 MUST 6)
  TEST 31 — append_history_event() ts-first key order on the standalone path (spec/41 JSONL key ordering)
  TEST 32 — list_archived() returns the written slugs (spec/41 MUST 2 capability)
  TEST 33 — Goal dataclass is mutable, not frozen=True (spec/41 mutable-dataclass note)
  TEST 34 — SubGoal dataclass is mutable, not frozen=True (spec/41 mutable-dataclass note)
  TEST 35 — load_goal() rejects a dangling blocked_by reference (spec/41 MUST 5 validation)
  TEST 36 — load_goal() rejects a non-string blocked_by entry (spec/41 MUST 5 validation)
  TEST 37 — read_schema_version() coerces a string "1" to int 1 (spec/41 schema-version note)
  TEST 38 — read_schema_version() returns None when goal.md present but key absent (spec/41 schema-version note)
  TEST 39 — env var dispatches a registered custom backend (spec/41 MUST 2 / registry)
  TEST 40 — get_goal_backend() raises BackendNotRegistered for an unknown id (registry error branch)
  TEST 41 — atomic_agents.goal.__getattr__ raises AttributeError for an unknown name (module __getattr__ error branch)
  TEST 42 — _redact_for_error_message() >32-char non-URL truncates to 32 chars + "..." (redaction branch)
  TEST 43 — _redact_for_error_message() short value passes through unchanged (redaction no-op branch)
  TEST 44 — _redact_for_error_message() URL value returns scheme://... (redaction URL branch)
  TEST 45 — get_default_goal_backend() empty-string env var falls through to filesystem default (env-var empty-string leg)
  TEST 46 — read_schema_version() raises GoalCorrupted when schema_version is present but not int-coercible (filesystem.py:527-530)
  TEST 47 — apply_transition() raises AtomicAgentsError for unknown sub_goal_id, no orphan write (filesystem.py:327-329)
  TEST 48 — load_goal() raises AtomicAgentsError when goal.md is absent (filesystem.py:210)
  TEST 49 — load_goal() + read_schema_version() both raise GoalCorrupted on unparseable frontmatter (filesystem.py:214, 519-521)
  TEST 50 — _redact_for_error_message() redacts a schemeless DSN (user:pass@host/db) (goal/__init__.py:289-290)
  TEST 51 — apply_transition() write-validation: bad permitted-field value raises SchemaValidationError and goal.md is UNCHANGED (filesystem.py:_write_goal validate_goal pre-write)
  TEST 52 — from atomic_agents.goal import main; callable(main) (goal/__init__.py __getattr__ re-export)
  TEST 53 — apply_transition() fields={'blocked_by': 'ghost'} does NOT persist when validate_goal rejects it (write/read symmetry — Principle #12 cross-check)
  TEST 54 — apply_transition() expected_from_status CAS match proceeds (spec/41 MUST 10)
  TEST 55 — apply_transition() expected_from_status CAS mismatch raises GoalConcurrentModification (spec/41 MUST 10)
  TEST 56 — archive_goal(when=pinned_date) clock injection: all date fields use the injected date (#483 PR1 addendum)
  TEST 57 — archive_goal() 'goal archived' prose appears exactly once (backend owns prose, no double-write)
  TEST 58 — append_history_event() reorders ts-first when ts is NOT the first key in input dict (#485)
  TEST 59 — apply_transition(when=pinned) stamps the ## History prose date only; JSONL ts stays caller wall-clock (spec/41 MUST 6 clock injection, #483 PR1)

PARAMETRIZATION: the protocol-behavior tests construct their backend through
the ``backend`` / ``backend_with_goal`` fixtures, which are themselves
parametrized over ``BACKEND_FACTORIES`` (the list of registered GoalBackend
implementations — currently just 'filesystem'). Adding a second backend to
``BACKEND_FACTORIES`` genuinely picks up every protocol-behavior test in this
file — the fixtures drive construction, so the parametrization is real, not
decorative.

A handful of tests are *deliberately* filesystem-specific and do NOT consume the
parametrized fixtures — they assert filesystem reference-impl facts a second
backend would not share: path-traversal rejection on construction, on-disk
CRLF/BOM byte normalization, ``backend_id == 'filesystem'``, and the
ATOMIC_AGENTS_GOAL_BACKEND registry-dispatch surface. Pure-dataclass tests
(Goal/SubGoal/GoalCapabilities) likewise need no backend. Those stay
filesystem-only on purpose; everything that exercises a Protocol METHOD goes
through the fixture so a future backend inherits the conformance assertions.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from atomic_agents.goal.filesystem import FilesystemGoalBackend
from atomic_agents.goal.types import Goal, GoalCapabilities, GoalExport, SubGoal
from atomic_agents._goal_impl import CURRENT_GOAL_SCHEMA_VERSION


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_goal_md(
    agent_root: Path, *, intent: str = "Test goal", active: bool = True
) -> None:
    """Write a minimal valid goal.md to agent_root."""
    agent_root.mkdir(parents=True, exist_ok=True)
    content = f"""---
schema_version: {CURRENT_GOAL_SCHEMA_VERSION}
active: {str(active).lower()}
intent: {intent}
priority: high
created: 2026-06-11
last_progress_check: 2026-06-11
success_criteria:
  - Criterion one
sub_goals:
  - id: sg1
    label: First sub-goal
    status: pending
  - id: sg2
    label: Second sub-goal
    status: pending
---

## Overview

Goal body text.

## History (auto-appended)
- 2026-06-11 — goal created
"""
    (agent_root / "goal.md").write_text(content, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures

# Each entry is a callable that builds a GoalBackend from an agent_root Path.
# A second backend (Postgres/SQLite) adds its pytest.param here and is picked up
# by every test that consumes the `backend` / `backend_with_goal` fixtures below.
BACKEND_FACTORIES = [
    pytest.param(lambda root: FilesystemGoalBackend(root), id="filesystem"),
]


@pytest.fixture(params=BACKEND_FACTORIES)
def backend_factory(request):
    """Parametrized factory: (agent_root) -> GoalBackend, one per registered impl."""
    return request.param


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    return tmp_path / "test-agent"


@pytest.fixture
def agent_root_with_goal(tmp_path: Path) -> Path:
    root = tmp_path / "goal-agent"
    _make_goal_md(root)
    return root


@pytest.fixture
def backend(backend_factory, agent_root: Path):
    """A GoalBackend (parametrized over BACKEND_FACTORIES) scoped to an empty agent_root.

    Consuming this fixture is what makes a test genuinely protocol-level: register
    a second backend and the test runs against it automatically.
    """
    return backend_factory(agent_root)


@pytest.fixture
def backend_with_goal(backend_factory, agent_root_with_goal: Path):
    """A GoalBackend (parametrized) scoped to an agent_root that already has a goal.md."""
    return backend_factory(agent_root_with_goal)


# ──────────────────────────────────────────────────────────────────────────────
# MUST 1 — side-effect-free construction


def test_construction_side_effect_free(tmp_path: Path) -> None:
    """FilesystemGoalBackend.__init__ MUST NOT touch the filesystem."""
    nonexistent = tmp_path / "nonexistent_agent"
    # Must not raise even though directory does not exist
    FilesystemGoalBackend(nonexistent)
    assert not nonexistent.exists(), "construction must not create directories"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 2 — capability honesty


def test_capabilities_returns_goal_capabilities(backend) -> None:
    """capabilities() must return a GoalCapabilities instance."""
    caps = backend.capabilities()
    assert isinstance(caps, GoalCapabilities)


# ──────────────────────────────────────────────────────────────────────────────
# MUST 3 — goal_text() returns '' when absent


def test_goal_text_returns_empty_when_absent(backend) -> None:
    """goal_text() MUST return '' when goal.md does not exist."""
    assert backend.goal_text("agent") == ""


def test_goal_text_returns_content_when_present(backend_with_goal) -> None:
    """goal_text() MUST return the raw text of goal.md when present."""
    text = backend_with_goal.goal_text("agent")
    assert "Test goal" in text
    assert len(text) > 0


def test_goal_text_is_side_effect_free(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """goal_text() MUST NOT modify goal.md or any other file."""
    before = (agent_root_with_goal / "goal.md").read_bytes()
    backend_with_goal.goal_text("agent")
    after = (agent_root_with_goal / "goal.md").read_bytes()
    assert before == after


# ──────────────────────────────────────────────────────────────────────────────
# MUST 4 — save_goal() write-what-I-give-you


def test_save_goal_does_not_mutate_last_progress_check(backend_with_goal) -> None:
    """save_goal() MUST NOT mutate goal.last_progress_check or any other field."""
    backend = backend_with_goal
    goal = backend.load_goal("agent")
    original_lpc = goal.last_progress_check
    goal.last_progress_check = "2020-01-01"  # force a specific value
    backend.save_goal("agent", goal)
    # Reload and verify save_goal did NOT change last_progress_check
    reloaded = backend.load_goal("agent")
    assert reloaded.last_progress_check == "2020-01-01", (
        "save_goal must write verbatim — not mutate last_progress_check"
    )
    _ = original_lpc  # suppress unused warning


# ──────────────────────────────────────────────────────────────────────────────
# MUST 5 — load_goal() round-trips what save_goal() wrote


def test_save_load_round_trip(backend_with_goal) -> None:
    """load_goal(save_goal(goal)) must produce identical state."""
    backend = backend_with_goal
    goal = backend.load_goal("agent")
    goal.sub_goals[0].status = "in_progress"
    backend.save_goal("agent", goal)
    reloaded = backend.load_goal("agent")
    assert reloaded.sub_goals[0].status == "in_progress"
    assert reloaded.intent == goal.intent


# ──────────────────────────────────────────────────────────────────────────────
# MUST 6 — apply_transition() atomic unit


def test_apply_transition_updates_goal_md_and_jsonl(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """apply_transition() MUST write goal.md AND goal_history.jsonl as one unit."""
    backend = backend_with_goal
    from datetime import datetime

    ts = datetime.now().astimezone().isoformat()
    backend.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="in_progress",
        fields={},
        history_prose="sg1 → in_progress",
        history_event={"ts": ts, "event": "test_transition", "sub_goal_id": "sg1"},
    )
    # goal.md must reflect the new status
    goal = backend.load_goal("agent")
    sg = next(s for s in goal.sub_goals if s.id == "sg1")
    assert sg.status == "in_progress"

    # goal_history.jsonl must have the event
    history_path = agent_root_with_goal / "goal_history.jsonl"
    assert history_path.is_file()
    lines = [ln for ln in history_path.read_text().splitlines() if ln.strip()]
    assert len(lines) >= 1
    event = json.loads(lines[-1])
    assert event["event"] == "test_transition"
    assert event["sub_goal_id"] == "sg1"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 7 — archive_goal() write ordering (no data loss)


def test_archive_goal_write_before_delete(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """archive_goal() MUST write the archive file before unlinking goal.md."""
    backend = backend_with_goal
    slug = backend.archive_goal("agent", reason="test-archive")
    # goal.md must be gone
    assert not (agent_root_with_goal / "goal.md").exists()
    # archive file must exist
    archive_path = agent_root_with_goal / "goal_archive" / f"{slug}.md"
    assert archive_path.is_file()
    # archive file must be a valid frontmatter doc with archived_at
    parsed = frontmatter.load(archive_path)
    assert parsed.metadata.get("archived_at") is not None
    assert parsed.metadata.get("archive_reason") == "test-archive"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 8 — archive_goal() collision-safe suffix loop


def test_archive_collision_safe_suffix(tmp_path: Path) -> None:
    """Two archives on the same day with the same intent get distinct filenames."""
    # Archive a goal, then create a new goal with same intent and archive again
    root1 = tmp_path / "agent1"
    root2 = tmp_path / "agent2"

    _make_goal_md(root1, intent="Build something")
    _make_goal_md(root2, intent="Build something")

    b1 = FilesystemGoalBackend(root1)
    b2 = FilesystemGoalBackend(root2)

    slug1 = b1.archive_goal("agent", reason="done")
    # Simulate same archive dir for collision testing
    # Copy archive from root1 to root2's archive dir
    (root2 / "goal_archive").mkdir(parents=True, exist_ok=True)
    archive_from_1 = root1 / "goal_archive" / f"{slug1}.md"
    # Place a file with the SAME base name in root2's archive dir
    base_collision = root2 / "goal_archive" / f"{slug1}.md"
    import shutil

    shutil.copy(archive_from_1, base_collision)

    # Now archive root2 — should get a _1 suffix
    slug2 = b2.archive_goal("agent", reason="done")
    assert slug2 != slug1, "collision must produce a distinct archive slug"
    archive2 = root2 / "goal_archive" / f"{slug2}.md"
    assert archive2.is_file(), "second archive file must exist"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 9 — archive_goal() idempotency


def test_archive_raises_when_no_goal_and_no_archive(backend, agent_root: Path) -> None:
    """archive_goal() with no goal.md and no archive dir raises AtomicAgentsError."""
    from atomic_agents.exceptions import AtomicAgentsError

    agent_root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(AtomicAgentsError, match="No active goal"):
        backend.archive_goal("agent")


def test_archive_idempotent_after_unlink(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """archive_goal() retry after successful archive returns existing slug (not double-archive)."""
    backend = backend_with_goal
    slug1 = backend.archive_goal("agent", reason="done")
    # goal.md is now gone; archive file exists
    # Second call should return the existing archive slug, not raise
    slug2 = backend.archive_goal("agent", reason="retry")
    assert slug2 == slug1, "idempotent retry must return the same archive slug"
    # Only ONE archive file should exist (no _1 suffix)
    archive_files = list((agent_root_with_goal / "goal_archive").glob("*.md"))
    assert len(archive_files) == 1, "idempotent retry must not create a second archive"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 10 — list_archived() returns [] for absent archive dir


def test_list_archived_returns_empty_when_absent(backend, agent_root: Path) -> None:
    """list_archived() MUST return [] when goal_archive/ does not exist."""
    agent_root.mkdir(parents=True, exist_ok=True)
    result = backend.list_archived("agent")
    assert result == []


def test_list_archived_returns_slugs(backend_with_goal) -> None:
    """list_archived() returns slugs after archiving."""
    backend = backend_with_goal
    slug = backend.archive_goal("agent", reason="done")
    result = backend.list_archived("agent")
    assert slug in result


# ──────────────────────────────────────────────────────────────────────────────
# MUST 11 — read_schema_version() returns int or None


def test_read_schema_version_returns_none_when_absent(
    backend, agent_root: Path
) -> None:
    """read_schema_version() MUST return None when goal.md is absent (not raise)."""
    agent_root.mkdir(parents=True, exist_ok=True)
    result = backend.read_schema_version("agent")
    assert result is None


def test_read_schema_version_returns_int_when_present(backend_with_goal) -> None:
    """read_schema_version() MUST return the integer schema_version from goal.md."""
    backend = backend_with_goal
    result = backend.read_schema_version("agent")
    assert result == CURRENT_GOAL_SCHEMA_VERSION
    assert isinstance(result, int)


# ──────────────────────────────────────────────────────────────────────────────
# MUST 12 — backend_id stability


def test_backend_id_is_filesystem(agent_root: Path) -> None:
    """FilesystemGoalBackend.backend_id MUST be 'filesystem'."""
    backend = FilesystemGoalBackend(agent_root)
    assert backend.backend_id == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 13 — GoalCapabilities field types correct


def test_goal_capabilities_field_types(backend) -> None:
    """All GoalCapabilities boolean fields must be bool type."""
    caps = backend.capabilities()
    assert isinstance(caps.backend_id, str)
    assert isinstance(caps.supports_canonical_export, bool)
    assert isinstance(caps.supports_archive, bool)
    assert isinstance(caps.supports_history_query, bool)


# ──────────────────────────────────────────────────────────────────────────────
# MUST 14 — GoalCapabilities defaults allow minimal positional construction


def test_goal_capabilities_minimal_construction() -> None:
    """GoalCapabilities(backend_id='x') must be valid with all capability flags False."""
    caps = GoalCapabilities(backend_id="custom")
    assert caps.backend_id == "custom"
    assert caps.supports_canonical_export is False
    assert caps.supports_archive is False
    assert caps.supports_history_query is False


# ──────────────────────────────────────────────────────────────────────────────
# MUST 15 — supports_canonical_export=True for filesystem


def test_filesystem_backend_supports_canonical_export(agent_root: Path) -> None:
    """FilesystemGoalBackend MUST advertise supports_canonical_export=True."""
    backend = FilesystemGoalBackend(agent_root)
    caps = backend.capabilities()
    assert caps.supports_canonical_export is True


# ──────────────────────────────────────────────────────────────────────────────
# MUST 16 — export() returns GoalExport with correct fields


def test_export_returns_goal_export(agent_root_with_goal: Path) -> None:
    """export() MUST return a GoalExport instance."""
    backend = FilesystemGoalBackend(agent_root_with_goal)
    result = backend.export()
    assert isinstance(result, GoalExport)
    assert result.backend_id == "filesystem"
    assert result.scope == str(agent_root_with_goal)
    assert isinstance(result.goal_md_bytes, bytes)
    assert isinstance(result.history_records_with_bytes, list)
    assert isinstance(result.archived_goals_with_bytes, list)


def test_export_goal_md_bytes_contain_intent(backend_with_goal) -> None:
    """export().goal_md_bytes must contain the goal's intent string."""
    result = backend_with_goal.export()
    assert b"Test goal" in result.goal_md_bytes


# ──────────────────────────────────────────────────────────────────────────────
# MUST 17 — export() returns empty GoalExport for agent with no goal.md


def test_export_empty_when_no_goal_md(backend, agent_root: Path) -> None:
    """export() MUST return GoalExport with empty bytes when goal.md is absent."""
    agent_root.mkdir(parents=True, exist_ok=True)
    result = backend.export()
    assert isinstance(result, GoalExport)
    assert result.goal_md_bytes == b""
    assert result.history_records_with_bytes == []
    assert result.archived_goals_with_bytes == []


# ──────────────────────────────────────────────────────────────────────────────
# MUST 18 — export() CRLF normalization


def test_export_crlf_normalized(agent_root: Path) -> None:
    """export() MUST normalize CRLF → LF in all exported bytes (spec/40 MUST 5)."""
    agent_root.mkdir(parents=True, exist_ok=True)
    # Write goal.md with CRLF line endings
    crlf_content = b"---\r\nschema_version: 1\r\nactive: true\r\nintent: CRLF goal\r\npriority: high\r\ncreated: 2026-06-11\r\nlast_progress_check: 2026-06-11\r\nsuccess_criteria:\r\n  - done\r\nsub_goals: []\r\n---\r\n\r\nbody text\r\n"
    (agent_root / "goal.md").write_bytes(crlf_content)
    backend = FilesystemGoalBackend(agent_root)
    result = backend.export()
    assert b"\r\n" not in result.goal_md_bytes, "CRLF must be normalized to LF"
    assert b"\n" in result.goal_md_bytes, "LF must be present after normalization"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 19 — Goal/SubGoal are mutable (not frozen=True)


def test_goal_is_mutable() -> None:
    """Goal MUST be a mutable dataclass (not frozen=True)."""
    goal = Goal(
        schema_version=1,
        active=True,
        intent="test",
        priority="high",
        created="2026-06-11",
        last_progress_check="2026-06-11",
        success_criteria=["done"],
    )
    # Must not raise FrozenInstanceError
    goal.active = False
    assert goal.active is False


def test_sub_goal_is_mutable() -> None:
    """SubGoal MUST be a mutable dataclass (not frozen=True)."""
    sg = SubGoal(id="sg1", label="test")
    sg.status = "in_progress"
    assert sg.status == "in_progress"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 20 — apply_transition() updates both goal.md and goal_history.jsonl


def test_apply_transition_dual_write(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """apply_transition() MUST update BOTH goal.md and goal_history.jsonl."""
    backend = backend_with_goal
    from datetime import datetime

    ts = datetime.now().astimezone().isoformat()
    backend.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="complete",
        fields={"completed": "2026-06-11"},
        history_prose="sg1 → complete",
        history_event={"ts": ts, "event": "sg_complete", "sub_goal_id": "sg1"},
    )

    # Check goal.md
    goal = backend.load_goal("agent")
    sg1 = next(s for s in goal.sub_goals if s.id == "sg1")
    assert sg1.status == "complete"
    assert sg1.completed == "2026-06-11"
    assert "sg1 → complete" in goal.body

    # Check goal_history.jsonl
    history = agent_root_with_goal / "goal_history.jsonl"
    lines = [ln for ln in history.read_text().splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]
    assert any(e.get("event") == "sg_complete" for e in events)


def test_apply_transition_rejects_invalid_status_before_write(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """apply_transition() MUST reject an unknown to_status BEFORE any write.

    Fail-closed write-time validation: a to_status not in VALID_SUB_GOAL_STATUSES
    must raise and leave goal.md AND goal_history.jsonl untouched, so the backend
    can never durably persist a goal.md its own load_goal() would reject (no
    write-time/read-time validation asymmetry).
    """
    from datetime import datetime

    from atomic_agents.exceptions import SchemaValidationError

    backend = backend_with_goal
    goal_md = agent_root_with_goal / "goal.md"
    history = agent_root_with_goal / "goal_history.jsonl"
    before_goal = goal_md.read_bytes()
    history_existed = history.is_file()

    ts = datetime.now().astimezone().isoformat()
    with pytest.raises(SchemaValidationError, match="to_status"):
        backend.apply_transition(
            agent_id="agent",
            sub_goal_id="sg1",
            to_status="frobnicate",
            fields={},
            history_prose="sg1 → frobnicate",
            history_event={"ts": ts, "event": "bad_status", "sub_goal_id": "sg1"},
        )

    # goal.md unchanged and still reloadable (no corrupt file persisted)
    assert goal_md.read_bytes() == before_goal
    backend.load_goal("agent")  # must not raise — file was never mutated
    # No orphan history line for the rejected transition
    if history.is_file():
        lines = [ln for ln in history.read_text().splitlines() if ln.strip()]
        assert not any(json.loads(ln).get("event") == "bad_status" for ln in lines)
    else:
        assert not history_existed


def test_apply_transition_fields_status_cannot_bypass_enum_gate(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """apply_transition() MUST NOT let fields={'status': ...} overwrite to_status.

    `to_status` is the authoritative, enum-validated status channel. A caller
    passing a `status` key through the free-field `fields` channel must not be
    able to persist an unvalidated status — that would reopen the exact
    write-time/read-time validation asymmetry the to_status enum gate closes
    (goal.md would carry a status load_goal() rejects). The conforming behavior
    is: the side-door 'status' is ignored, the valid `to_status` wins, and the
    persisted goal.md remains reloadable (spec/41 MUST 6).
    """
    from datetime import datetime

    backend = backend_with_goal
    ts = datetime.now().astimezone().isoformat()

    goal = backend.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="in_progress",
        fields={"status": "frobnicate"},  # side-door attempt — must be ignored
        history_prose="sg1 → in_progress",
        history_event={"ts": ts, "event": "sidedoor_test", "sub_goal_id": "sg1"},
    )

    # The returned object reflects the validated to_status, not the side-door value.
    sg1 = next(s for s in goal.sub_goals if s.id == "sg1")
    assert sg1.status == "in_progress"

    # The persisted goal.md is reloadable — no invalid status leaked through.
    reloaded = backend.load_goal("agent")
    reloaded_sg1 = next(s for s in reloaded.sub_goals if s.id == "sg1")
    assert reloaded_sg1.status == "in_progress"


def test_apply_transition_fields_cannot_rewrite_identity(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """apply_transition() MUST NOT let fields={'id'|'label': ...} rewrite identity.

    The `fields` channel carries transition metadata only (spec/41); it may set
    SUB_GOAL_TRANSITION_FIELDS but never the immutable identity fields `id`/
    `label`. A caller passing them must be ignored (fail closed) so a transition
    cannot silently rename or re-key a sub-goal mid-flight. A legitimate
    transition field (`output`) in the same call MUST still be applied — proving
    the guard discriminates rather than dropping the whole channel.
    """
    from datetime import datetime

    backend = backend_with_goal
    ts = datetime.now().astimezone().isoformat()

    goal = backend.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="complete",
        fields={
            "id": "hijacked",  # identity — must be ignored
            "label": "renamed",  # identity — must be ignored
            "output": "artifacts/result.md",  # legit transition field — must apply
        },
        history_prose="sg1 → complete",
        history_event={"ts": ts, "event": "identity_guard_test", "sub_goal_id": "sg1"},
    )

    sg1 = next(s for s in goal.sub_goals if s.id == "sg1")
    assert sg1.id == "sg1", "id must not be rewritten via the fields channel"
    assert sg1.label != "renamed", "label must not be rewritten via the fields channel"
    assert sg1.output == "artifacts/result.md", (
        "legit transition field must still apply"
    )

    # No phantom sub-goal under the hijacked id, and the change is durable.
    assert not any(s.id == "hijacked" for s in goal.sub_goals)
    reloaded = backend.load_goal("agent")
    reloaded_sg1 = next(s for s in reloaded.sub_goals if s.id == "sg1")
    assert reloaded_sg1.id == "sg1"
    assert reloaded_sg1.output == "artifacts/result.md"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 21 — apply_transition() ts-first key order in JSONL


def test_apply_transition_ts_first_key_order(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """apply_transition() MUST place 'ts' as first key in JSONL lines."""
    backend = backend_with_goal
    from datetime import datetime

    ts = datetime.now().astimezone().isoformat()
    backend.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="in_progress",
        fields={},
        history_prose="sg1 → in_progress",
        history_event={"ts": ts, "event": "transition_test", "extra": "value"},
    )

    history = agent_root_with_goal / "goal_history.jsonl"
    lines = [ln for ln in history.read_text().splitlines() if ln.strip()]
    last_line = lines[-1]
    # First key in the JSON must be 'ts'
    assert last_line.startswith('{"ts"'), (
        f"First key in JSONL line must be 'ts'; got: {last_line[:50]}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# MUST 22 — append_history_event() ts-first key order


def test_append_history_event_ts_first(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """append_history_event() MUST place 'ts' as first key in JSONL."""
    backend = backend_with_goal
    from datetime import datetime

    ts = datetime.now().astimezone().isoformat()
    backend.append_history_event(
        "agent",
        {"ts": ts, "event": "custom_event", "data": "value"},
    )

    history = agent_root_with_goal / "goal_history.jsonl"
    lines = [ln for ln in history.read_text().splitlines() if ln.strip()]
    last_line = lines[-1]
    assert last_line.startswith('{"ts"'), (
        f"First key must be 'ts'; got: {last_line[:50]}"
    )
    event = json.loads(last_line)
    assert event["event"] == "custom_event"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 23 — export() archived_goals_with_bytes populated after archive


def test_export_includes_archived_goals(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """export() MUST include archived goal bytes in archived_goals_with_bytes."""
    backend = backend_with_goal
    slug = backend.archive_goal("agent", reason="done")

    # Create a new goal for export
    _make_goal_md(agent_root_with_goal, intent="New goal after archive")
    result = backend.export()

    assert len(result.archived_goals_with_bytes) == 1
    archive_slug, archive_bytes = result.archived_goals_with_bytes[0]
    assert archive_slug == slug
    assert len(archive_bytes) > 0
    assert b"Test goal" in archive_bytes  # original intent was "Test goal"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 24 — path-traversal guard on construction


def test_path_traversal_rejected() -> None:
    """FilesystemGoalBackend MUST reject paths with '..' components."""
    with pytest.raises(ValueError, match=r"\.\."):
        FilesystemGoalBackend(Path("..") / "other")


# ──────────────────────────────────────────────────────────────────────────────
# MUST 25 — GoalCapabilities(backend_id='x') valid with all defaults False


def test_goal_capabilities_defaults_are_false() -> None:
    """GoalCapabilities with only backend_id must have all capability flags False."""
    caps = GoalCapabilities(backend_id="test-backend")
    assert caps.supports_canonical_export is False
    assert caps.supports_archive is False
    assert caps.supports_history_query is False


# ──────────────────────────────────────────────────────────────────────────────
# Cross-path validation parity — backend.load_goal() rejects the SAME corrupt
# goal.md that GoalManager.validate_goal() rejects (single shared validator).


def test_load_goal_rejects_dangling_blocked_by(tmp_path: Path) -> None:
    """load_goal() MUST reject a blocked_by that references an unknown sub_goal id.

    Regression guard for the dropped-validation shortcut: the backend's load path
    must enforce the SAME blocked_by referential-integrity check as
    GoalManager.validate_goal(). A corrupt dependency graph must not load silently
    through the future-canonical reader.
    """
    from atomic_agents.exceptions import SchemaValidationError

    root = tmp_path / "dangling-agent"
    root.mkdir(parents=True, exist_ok=True)
    content = f"""---
schema_version: {CURRENT_GOAL_SCHEMA_VERSION}
active: true
intent: Dangling blocked_by
priority: high
created: 2026-06-11
last_progress_check: 2026-06-11
success_criteria:
  - done
sub_goals:
  - id: sg1
    label: First
    status: pending
    blocked_by: nonexistent_id
---

body
"""
    (root / "goal.md").write_text(content, encoding="utf-8")
    backend = FilesystemGoalBackend(root)
    with pytest.raises(SchemaValidationError, match="blocked_by"):
        backend.load_goal("agent")


def test_load_goal_rejects_nonstring_blocked_by(tmp_path: Path) -> None:
    """load_goal() MUST reject a non-string blocked_by (type parity with manager)."""
    from atomic_agents.exceptions import SchemaValidationError

    root = tmp_path / "badtype-agent"
    root.mkdir(parents=True, exist_ok=True)
    content = f"""---
schema_version: {CURRENT_GOAL_SCHEMA_VERSION}
active: true
intent: Bad blocked_by type
priority: high
created: 2026-06-11
last_progress_check: 2026-06-11
success_criteria:
  - done
sub_goals:
  - id: sg1
    label: First
    status: pending
    blocked_by: 42
---

body
"""
    (root / "goal.md").write_text(content, encoding="utf-8")
    backend = FilesystemGoalBackend(root)
    with pytest.raises(SchemaValidationError, match="blocked_by"):
        backend.load_goal("agent")


# ──────────────────────────────────────────────────────────────────────────────
# read_schema_version() honors its int | None contract


def test_read_schema_version_coerces_string_to_int(tmp_path: Path) -> None:
    """read_schema_version() MUST return an int even when written as a string."""
    root = tmp_path / "strver-agent"
    root.mkdir(parents=True, exist_ok=True)
    content = """---
schema_version: "1"
active: true
intent: String schema version
priority: high
created: 2026-06-11
last_progress_check: 2026-06-11
success_criteria:
  - done
sub_goals: []
---

body
"""
    (root / "goal.md").write_text(content, encoding="utf-8")
    backend = FilesystemGoalBackend(root)
    result = backend.read_schema_version("agent")
    assert result == 1
    assert isinstance(result, int)


def test_read_schema_version_none_when_key_missing(tmp_path: Path) -> None:
    """read_schema_version() returns None when goal.md lacks the key (no crash)."""
    # frontmatter present and parseable but no schema_version key.
    root = tmp_path / "nokey-agent"
    root.mkdir(parents=True, exist_ok=True)
    (root / "goal.md").write_text("---\nactive: true\n---\n\nbody\n", encoding="utf-8")
    backend = FilesystemGoalBackend(root)
    assert backend.read_schema_version("agent") is None


def test_read_schema_version_raises_goal_corrupted_for_non_int_value(
    tmp_path: Path,
) -> None:
    """read_schema_version() MUST raise GoalCorrupted when schema_version is present but not int-coercible.

    This covers the error branch at filesystem.py:527-530 — the key exists in
    frontmatter but its value cannot be coerced via int(), so GoalCorrupted is
    raised. This path is distinct from load_goal()'s validation: it fires
    directly from read_schema_version() without a full parse pass.
    """
    from atomic_agents.exceptions import GoalCorrupted

    root = tmp_path / "badver-agent"
    root.mkdir(parents=True, exist_ok=True)
    # "abc" is present in frontmatter but cannot be coerced to int.
    (root / "goal.md").write_text(
        "---\nschema_version: abc\nactive: true\n---\n\nbody\n",
        encoding="utf-8",
    )
    backend = FilesystemGoalBackend(root)
    with pytest.raises(GoalCorrupted, match="schema_version"):
        backend.read_schema_version("agent")


# ──────────────────────────────────────────────────────────────────────────────
# Operator override: ATOMIC_AGENTS_GOAL_BACKEND dispatches through the registry


def test_env_var_dispatches_registered_custom_backend(
    tmp_path: Path, monkeypatch
) -> None:
    """get_default_goal_backend() MUST honor a registered non-filesystem backend.

    Regression guard: the ATOMIC_AGENTS_GOAL_BACKEND override surface must work
    for any registered backend, not just 'filesystem'. Previously the factory
    fell straight through to BackendNotRegistered even for a registered id.
    """
    from atomic_agents.goal import (
        get_default_goal_backend,
        register_goal_backend,
        unregister_goal_backend,
    )

    class _StubBackend(FilesystemGoalBackend):
        @property
        def backend_id(self) -> str:
            return "stub"

    register_goal_backend("stub", _StubBackend)
    try:
        monkeypatch.setenv("ATOMIC_AGENTS_GOAL_BACKEND", "stub")
        backend = get_default_goal_backend(tmp_path / "agent")
        assert isinstance(backend, _StubBackend)
        assert backend.backend_id == "stub"
    finally:
        unregister_goal_backend("stub")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 40 — get_goal_backend() raises BackendNotRegistered for an unknown id


def test_get_goal_backend_raises_for_unknown_id() -> None:
    """get_goal_backend() MUST raise BackendNotRegistered when the id is absent.

    The error message must include the requested id and the list of known ids so
    operators can diagnose a typo or a missing registration.
    """
    from atomic_agents.exceptions import BackendNotRegistered
    from atomic_agents.goal import get_goal_backend, list_goal_backends

    with pytest.raises(BackendNotRegistered) as exc_info:
        get_goal_backend("nonexistent")

    msg = str(exc_info.value)
    assert "nonexistent" in msg
    # The error must name at least one available backend id.
    known = list_goal_backends()
    assert any(bid in msg for bid in known), (
        f"Expected one of {known!r} to appear in error message: {msg!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 41 — atomic_agents.goal.__getattr__ raises AttributeError for unknown name


def test_goal_module_getattr_raises_for_unknown_attribute() -> None:
    """atomic_agents.goal.__getattr__ MUST raise AttributeError for names it does not own.

    This covers the error branch at __init__.py:124 — the name is not in
    _GOAL_IMPL_NAMES so the hook raises rather than returning anything.
    Using a name that is definitively not re-exported from _goal_impl.
    """
    import atomic_agents.goal as _goal_mod

    with pytest.raises(AttributeError) as exc_info:
        _ = _goal_mod.this_attr_does_not_exist_at_all

    assert "this_attr_does_not_exist_at_all" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42 / 43 / 44 — _redact_for_error_message() branch coverage


def test_redact_long_non_url_truncates() -> None:
    """_redact_for_error_message() MUST truncate values longer than 32 chars.

    Non-URL value longer than 32 chars -> first 32 chars + "...".
    """
    import atomic_agents.goal as _goal_mod

    long_value = "a" * 40  # 40 chars, well over the 32-char threshold
    result = _goal_mod._redact_for_error_message(long_value)
    assert result == long_value[:32] + "..."


def test_redact_short_non_url_passthrough() -> None:
    """_redact_for_error_message() MUST pass through values <= 32 chars unchanged."""
    import atomic_agents.goal as _goal_mod

    short_value = "my-custom-backend"
    assert len(short_value) <= 32
    assert _goal_mod._redact_for_error_message(short_value) == short_value


def test_redact_url_returns_scheme_placeholder() -> None:
    """_redact_for_error_message() MUST redact URLs to scheme://... only."""
    import atomic_agents.goal as _goal_mod

    url = "postgres://user:secret@host:5432/db"
    result = _goal_mod._redact_for_error_message(url)
    assert result == "postgres://..."
    assert "secret" not in result
    assert "host" not in result


# ──────────────────────────────────────────────────────────────────────────────
# TEST 45 — get_default_goal_backend() empty-string env var -> filesystem default


def test_get_default_goal_backend_empty_env_var_returns_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_default_goal_backend() MUST fall back to FilesystemGoalBackend for "".

    Setting ATOMIC_AGENTS_GOAL_BACKEND="" (empty string) triggers the
    `if not raw_backend_id` branch at __init__.py:250 and returns a
    FilesystemGoalBackend — identical behaviour to leaving the var unset.
    """
    from atomic_agents.goal import get_default_goal_backend

    monkeypatch.setenv("ATOMIC_AGENTS_GOAL_BACKEND", "")
    backend = get_default_goal_backend(tmp_path / "agent")

    assert isinstance(backend, FilesystemGoalBackend)
    assert backend.backend_id == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 47 — apply_transition() sub_goal-not-found raises AtomicAgentsError, no orphan write


def test_apply_transition_sub_goal_not_found_no_orphan_write(
    backend_with_goal, agent_root_with_goal: Path
) -> None:
    """apply_transition() MUST raise AtomicAgentsError for an unknown sub_goal_id.

    When sub_goal_id is not present in the loaded goal's sub_goals list,
    filesystem.py:327-329 raises AtomicAgentsError (matching "sub_goal not found").
    Neither goal.md nor goal_history.jsonl must be modified — the operation is
    fail-closed and leaves no orphan write behind.
    """
    from datetime import datetime

    from atomic_agents.exceptions import AtomicAgentsError

    backend = backend_with_goal
    goal_md = agent_root_with_goal / "goal.md"
    history = agent_root_with_goal / "goal_history.jsonl"

    before_goal = goal_md.read_bytes()
    history_existed = history.is_file()
    before_history = history.read_bytes() if history_existed else None

    ts = datetime.now().astimezone().isoformat()
    with pytest.raises(AtomicAgentsError, match="sub_goal not found"):
        backend.apply_transition(
            agent_id="agent",
            sub_goal_id="nonexistent",
            to_status="in_progress",
            fields={},
            history_prose="nonexistent → in_progress",
            history_event={
                "ts": ts,
                "event": "ghost_transition",
                "sub_goal_id": "nonexistent",
            },
        )

    # goal.md must be untouched — no partial or orphan write
    assert goal_md.read_bytes() == before_goal, (
        "goal.md must not be modified when sub_goal_id is unknown"
    )
    # goal_history.jsonl must also be untouched
    if history_existed:
        assert history.read_bytes() == before_history, (
            "goal_history.jsonl must not be modified when sub_goal_id is unknown"
        )
    else:
        assert not history.is_file(), (
            "goal_history.jsonl must not be created when sub_goal_id is unknown"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 48 — load_goal() raises AtomicAgentsError when goal.md is absent


def test_load_goal_absent_file_raises(tmp_path: Path) -> None:
    """load_goal() MUST raise AtomicAgentsError (matching "No goal.md") when goal.md is absent.

    filesystem.py:209-210: `if not self._goal_path.is_file(): raise AtomicAgentsError(...)`.
    An empty agent_root (directory present, goal.md absent) must produce the error,
    not a silent empty return.
    """
    from atomic_agents.exceptions import AtomicAgentsError

    root = tmp_path / "empty-agent"
    root.mkdir(parents=True, exist_ok=True)
    backend = FilesystemGoalBackend(root)

    with pytest.raises(AtomicAgentsError, match="No goal.md"):
        backend.load_goal("agent")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 49 — load_goal() + read_schema_version() both raise GoalCorrupted on
#            unparseable frontmatter (distinct from TEST 46's non-int-coercible branch)


def test_load_goal_and_read_schema_version_raise_goal_corrupted_on_bad_frontmatter(
    tmp_path: Path,
) -> None:
    """load_goal() and read_schema_version() MUST raise GoalCorrupted when the
    YAML frontmatter block itself cannot be parsed (not just a missing or wrong-type key).

    filesystem.py:213-214 (load_goal): wraps the frontmatter.load() exception into
        GoalCorrupted("goal.md unparseable: ...")
    filesystem.py:518-521 (read_schema_version): wraps it into
        GoalCorrupted("goal.md unparseable in read_schema_version: ...")

    This is DISTINCT from TEST 46 (schema_version present but not int-coercible):
    here the YAML delimiters themselves are malformed so frontmatter.load() raises
    before any key lookup occurs.
    """
    from atomic_agents.exceptions import GoalCorrupted

    root = tmp_path / "corrupt-agent"
    root.mkdir(parents=True, exist_ok=True)
    # Malformed YAML: unclosed mapping value inside the frontmatter block causes
    # frontmatter.load() to raise a parse exception.
    malformed = "---\nschema_version: :\n  bad: [unclosed\n---\nbody\n"
    (root / "goal.md").write_text(malformed, encoding="utf-8")
    backend = FilesystemGoalBackend(root)

    with pytest.raises(GoalCorrupted, match="goal.md unparseable"):
        backend.load_goal("agent")

    with pytest.raises(GoalCorrupted, match="unparseable in read_schema_version"):
        backend.read_schema_version("agent")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 50 — _redact_for_error_message() redacts a schemeless DSN


def test_redact_schemeless_dsn_returns_redacted_placeholder() -> None:
    """_redact_for_error_message() MUST redact a schemeless user:pass@host/db DSN.

    goal/__init__.py:289-290: the DSN heuristic catches ``user:password@host/db``
    style values that lack a ``://`` scheme (handled by the URL branch) but carry
    embedded credentials after the ``@``. The result must be the literal string
    "[redacted-connection-string]" and MUST NOT contain the password.
    """
    import atomic_agents.goal as _goal_mod

    dsn = "user:s3cr3tpass@dbhost/mydb"
    result = _goal_mod._redact_for_error_message(dsn)
    assert result == "[redacted-connection-string]"
    assert "s3cr3tpass" not in result, (
        "password portion of a schemeless DSN must not appear in the redacted output"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 51 — apply_transition() write-validation: bad permitted-field value
#            raises SchemaValidationError and goal.md is UNCHANGED
#            (filesystem.py:_write_goal validate_goal pre-write)


def test_apply_transition_write_validation_bad_blocked_by_fails_closed(
    tmp_path: Path,
) -> None:
    """apply_transition() with fields={'blocked_by': '<unknown-id>'} MUST raise
    SchemaValidationError and leave goal.md UNCHANGED.

    _write_goal() calls validate_goal() BEFORE the durable write (the
    write/read symmetry — spec/41 MUST 6). A permitted field (blocked_by is in
    SUB_GOAL_TRANSITION_FIELDS) carrying a value that fails validate_goal's
    referential-integrity check must:
      1. Raise SchemaValidationError (not silently commit).
      2. Leave goal.md byte-for-byte identical to the pre-call state.
      3. Leave the on-disk sub_goal status unchanged — load_goal() still
         succeeds and sg1.status is still its prior value ('pending'), not
         the attempted to_status.
      4. Leave no JSONL audit line for the rejected event.

    This is the key write/read symmetry test: a permitted-field value that
    _write_goal rejects closes the gap that the apply_transition `fields`
    allow-set alone does not — a valid field name with a bad value would
    otherwise write a goal.md that load_goal() rejects, locking the agent
    out of its own goal.
    """
    from datetime import datetime

    from atomic_agents.exceptions import SchemaValidationError

    root = tmp_path / "agent"
    root.mkdir(parents=True, exist_ok=True)
    content = f"""---
schema_version: {CURRENT_GOAL_SCHEMA_VERSION}
active: true
intent: Write-validation test
priority: high
created: 2026-06-11
last_progress_check: 2026-06-11
success_criteria:
  - Criterion one
sub_goals:
  - id: sg1
    label: First sub-goal
    status: pending
  - id: sg2
    label: Second sub-goal
    status: pending
---

## Overview

Goal body.

## History (auto-appended)
- 2026-06-11 — goal created
"""
    goal_md = root / "goal.md"
    goal_md.write_text(content, encoding="utf-8")
    before_bytes = goal_md.read_bytes()

    backend = FilesystemGoalBackend(root)
    history_path = root / "goal_history.jsonl"
    assert not history_path.exists(), "precondition: no JSONL before the call"

    ts = datetime.now().astimezone().isoformat()
    with pytest.raises(SchemaValidationError, match="blocked_by"):
        backend.apply_transition(
            agent_id="agent",
            sub_goal_id="sg1",
            to_status="blocked",
            fields={"blocked_by": "ghost-does-not-exist"},
            history_prose="sg1 → blocked (ghost)",
            history_event={"ts": ts, "event": "bad_blocked_by", "sub_goal_id": "sg1"},
        )

    # 1. goal.md is byte-for-byte unchanged — the bad write never committed.
    assert goal_md.read_bytes() == before_bytes, (
        "goal.md must be unchanged when _write_goal validation fails"
    )

    # 2. load_goal still succeeds — the on-disk state is still valid.
    reloaded = backend.load_goal("agent")

    # 3. sg1.status is still 'pending' — to_status 'blocked' was never persisted.
    sg1 = next(s for s in reloaded.sub_goals if s.id == "sg1")
    assert sg1.status == "pending", (
        "sg1.status must not be mutated on disk when _write_goal rejects the write"
    )
    assert sg1.blocked_by is None, (
        "sg1.blocked_by must not be written when validate_goal raises"
    )

    # 4. No orphan JSONL audit line for the rejected event.
    assert not history_path.exists(), (
        "goal_history.jsonl must not be created when the write fails"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 52 — from atomic_agents.goal import main; callable(main)
#            (goal/__init__.py __getattr__ re-export)


def test_main_re_export_is_callable() -> None:
    """'from atomic_agents.goal import main' MUST succeed and return a callable.

    goal/__init__.py's __getattr__ hook re-exports 'main' from _goal_impl.
    This was missing in a prior version (Codex P2 back-compat finding).
    The test asserts the import resolves (no AttributeError) and the returned
    object is callable (it's the argparse-driven CLI entry point).
    """
    from atomic_agents.goal import main  # noqa: PLC0415

    assert callable(main), "'main' re-exported from atomic_agents.goal must be callable"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 53 — apply_transition() fields={'blocked_by': 'ghost'} does NOT persist
#            when validate_goal rejects it (write/read symmetry cross-check)
#
# NOTE: This is a focused cross-check of TEST 51 through the parametrized
# backend fixture, so a second backend registered later also inherits this
# conformance assertion. TEST 51 uses a raw FilesystemGoalBackend + manually
# written goal.md for byte-level assertion; TEST 53 uses backend_with_goal
# so it runs against every BACKEND_FACTORIES entry.


def test_apply_transition_blocked_by_ghost_rejected_by_conformance_backend(
    backend_with_goal: "FilesystemGoalBackend",
    agent_root_with_goal: Path,
) -> None:
    """apply_transition() with fields={'blocked_by': '<unknown>'} MUST raise
    SchemaValidationError (via _write_goal validate_goal) on EVERY conforming
    backend, and goal.md must remain reloadable with sg1 status unchanged.

    This is the protocol-parametrized twin of TEST 51 — it confirms the
    write/read validation symmetry is a Protocol requirement, not just a
    filesystem-impl detail.
    """
    from datetime import datetime

    from atomic_agents.exceptions import SchemaValidationError

    backend = backend_with_goal
    goal_md = agent_root_with_goal / "goal.md"
    before_bytes = goal_md.read_bytes()

    ts = datetime.now().astimezone().isoformat()
    with pytest.raises(SchemaValidationError, match="blocked_by"):
        backend.apply_transition(
            agent_id="agent",
            sub_goal_id="sg1",
            to_status="blocked",
            fields={"blocked_by": "definitely-not-a-known-sub-goal-id"},
            history_prose="sg1 → blocked (ghost)",
            history_event={"ts": ts, "event": "ghost_blocked_by", "sub_goal_id": "sg1"},
        )

    # goal.md unchanged and load_goal still succeeds (disk state is valid).
    assert goal_md.read_bytes() == before_bytes
    reloaded = backend.load_goal("agent")
    sg1 = next(s for s in reloaded.sub_goals if s.id == "sg1")
    assert sg1.status == "pending", "sg1.status must not be persisted when write fails"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 54 — apply_transition() expected_from_status CAS match (spec/41 MUST 10)
#
# When expected_from_status matches the sub-goal's current on-disk status, the
# transition MUST proceed normally (no GoalConcurrentModification raised).
# The check happens UNDER THE LOCK (after load_goal, before write) so no
# concurrent write can slip between check and write.


def test_apply_transition_expected_from_status_match_proceeds(
    backend_with_goal: "FilesystemGoalBackend",
    agent_root_with_goal: Path,
) -> None:
    """apply_transition() with expected_from_status matching current status MUST succeed.

    spec/41 MUST 10 (compare-and-set guard): when expected_from_status is provided
    and the sub-goal's current on-disk status matches, the transition proceeds as
    normal. This is the happy path for the coordinator's terminal transition.
    """
    from datetime import datetime

    backend = backend_with_goal
    ts = datetime.now().astimezone().isoformat()

    # sg1 starts as 'pending'; expected_from_status='pending' matches — should proceed.
    goal = backend.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="in_progress",
        fields={},
        history_prose="sg1 → in_progress (CAS match test)",
        history_event={"ts": ts, "event": "cas_match_test", "sub_goal_id": "sg1"},
        expected_from_status="pending",
    )

    # Transition succeeded — sub-goal is now in_progress.
    sg1 = next(s for s in goal.sub_goals if s.id == "sg1")
    assert sg1.status == "in_progress", (
        "expected_from_status='pending' must allow the transition when sub-goal "
        "is actually pending (spec/41 MUST 10 match case)"
    )

    # Durable: reloaded goal also shows in_progress.
    reloaded = backend.load_goal("agent")
    sg1_reloaded = next(s for s in reloaded.sub_goals if s.id == "sg1")
    assert sg1_reloaded.status == "in_progress"

    # JSONL audit line was written (no orphan-line concern on success).
    history = agent_root_with_goal / "goal_history.jsonl"
    assert history.is_file()
    events = [json.loads(ln) for ln in history.read_text().splitlines() if ln.strip()]
    assert any(e.get("event") == "cas_match_test" for e in events)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 55 — apply_transition() expected_from_status CAS mismatch (spec/41 MUST 10)
#
# When expected_from_status does NOT match the current on-disk status (another
# writer moved the sub-goal), apply_transition() MUST raise GoalConcurrentModification
# and MUST NOT write to goal.md or append a JSONL line (fail-closed, no orphan audit).


def test_apply_transition_expected_from_status_mismatch_raises(
    backend_with_goal: "FilesystemGoalBackend",
    agent_root_with_goal: Path,
) -> None:
    """apply_transition() with expected_from_status mismatching current status MUST raise.

    spec/41 MUST 10 (compare-and-set guard): when expected_from_status is provided
    but the sub-goal's current on-disk status differs, GoalConcurrentModification
    MUST be raised, goal.md MUST be unchanged, and goal_history.jsonl MUST have NO
    new line for the rejected transition (fail-closed, no orphan audit line).

    This is the guard the coordinator relies on for the terminal transition: if a
    concurrent writer moved the sub-goal off in_progress between the pre-transition
    and the terminal transition, GoalConcurrentModification surfaces rather than a
    stale write landing on disk.
    """
    from datetime import datetime

    from atomic_agents.exceptions import GoalConcurrentModification

    backend = backend_with_goal
    goal_md = agent_root_with_goal / "goal.md"
    history = agent_root_with_goal / "goal_history.jsonl"

    before_bytes = goal_md.read_bytes()
    history_lines_before = history.read_text().splitlines() if history.is_file() else []

    ts = datetime.now().astimezone().isoformat()
    # sg1 is 'pending'; claiming expected_from_status='in_progress' is a mismatch.
    with pytest.raises(GoalConcurrentModification):
        backend.apply_transition(
            agent_id="agent",
            sub_goal_id="sg1",
            to_status="complete",
            fields={"completed": "2026-06-13"},
            history_prose="sg1 → complete (should not land)",
            history_event={
                "ts": ts,
                "event": "cas_mismatch_test",
                "sub_goal_id": "sg1",
            },
            expected_from_status="in_progress",
        )

    # goal.md must be completely unchanged (spec/41 MUST 10 fail-closed).
    assert goal_md.read_bytes() == before_bytes, (
        "goal.md must NOT be written when expected_from_status mismatches "
        "(spec/41 MUST 10 — GoalConcurrentModification is fail-closed)"
    )

    # No orphan JSONL line for the rejected transition.
    if history.is_file():
        current_lines = [ln for ln in history.read_text().splitlines() if ln.strip()]
        new_lines = current_lines[len(history_lines_before) :]
        assert not any("cas_mismatch_test" in ln for ln in new_lines), (
            "goal_history.jsonl must NOT have a line for the rejected CAS transition "
            "(spec/41 MUST 10 — no orphan audit line on GoalConcurrentModification)"
        )

    # Sub-goal must still be 'pending' (not 'complete') — no stale write.
    reloaded = backend.load_goal("agent")
    sg1 = next(s for s in reloaded.sub_goals if s.id == "sg1")
    assert sg1.status == "pending", (
        "sub-goal status must not be changed when GoalConcurrentModification is raised "
        "(spec/41 MUST 10 — no stale write)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 56 — archive_goal() clock injection via when= parameter (#483 PR1)
#
# Parametrized over BACKEND_FACTORIES so alternate backend authors who don't
# support `when=` fail immediately (the Protocol's @runtime_checkable isinstance
# check is signature-blind; only a conformance call catches the TypeError).


def test_archive_goal_when_parameter_accepted(
    backend_with_goal: "FilesystemGoalBackend",
    agent_root_with_goal: Path,
) -> None:
    """archive_goal(when=pinned_date) MUST use the injected date for ALL date fields.

    spec/41 Addendum #483 PR1 clock-injection contract: `when=` controls
    archived_at, last_progress_check, the ## History datestamp, and the
    archive slug date prefix. ALL four MUST agree with the injected date
    (no split-clock divergence). The returned slug MUST contain the pinned date.

    Passes `when=date(2026, 1, 1)` — a date that cannot match date.today() so
    any field that falls back to the wall clock fails the assertion.
    """
    import frontmatter
    from datetime import date

    backend = backend_with_goal
    pinned = date(2026, 1, 1)

    slug = backend.archive_goal("agent", reason="clock-test", when=pinned)

    # (a) Slug prefix must carry the pinned date, not wall-clock date.
    assert "2026-01-01" in slug, (
        f"archive slug must start with the injected date '2026-01-01'; got {slug!r}"
    )

    # (b) Archive frontmatter must use the pinned date for both timestamp fields.
    archive_path = agent_root_with_goal / "goal_archive" / f"{slug}.md"
    assert archive_path.is_file(), f"archive file must exist: {archive_path}"
    parsed = frontmatter.load(archive_path)
    assert parsed.metadata.get("archived_at") == "2026-01-01", (
        "archive_goal(when=pinned) must write archived_at == '2026-01-01', "
        f"not the wall-clock date.today(). Got: {parsed.metadata.get('archived_at')!r}"
    )
    assert parsed.metadata.get("last_progress_check") == "2026-01-01", (
        "archive_goal(when=pinned) must write last_progress_check == '2026-01-01'. "
        f"Got: {parsed.metadata.get('last_progress_check')!r}"
    )

    # (c) History prose datestamp in the archive body must also use the pinned date.
    body = parsed.content
    assert "2026-01-01" in body, (
        "archive_goal(when=pinned) must stamp the ## History prose entry with "
        f"'2026-01-01', not wall-clock today. Body: {body!r}"
    )
    assert "goal archived" in body, (
        "archive body must contain the 'goal archived' prose line written by the backend"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 57 — archive_goal() single-occurrence of 'goal archived' prose (#483 PR1)
#
# Guards the "backend owns the prose" ruling: after GoalManager.archive() is a
# thin shim, the 'goal archived' line must appear EXACTLY ONCE in the archive
# body — once from the backend under the lock. Zero occurrences = backend
# forgot to write prose; two occurrences = GoalManager and backend both wrote it.


def test_archive_goal_prose_appears_exactly_once(
    backend_with_goal: "FilesystemGoalBackend",
    agent_root_with_goal: Path,
) -> None:
    """archive_goal() MUST write the 'goal archived' history prose exactly once.

    Guards double-write regression: the backend owns the prose and writes it
    under the lock. The GoalManager thin shim must NOT also call _append_history
    before delegating — that would produce two occurrences in the archive body.
    """
    import frontmatter

    backend = backend_with_goal
    slug = backend.archive_goal("agent", reason="prose-test")
    archive_path = agent_root_with_goal / "goal_archive" / f"{slug}.md"
    parsed = frontmatter.load(archive_path)
    body = parsed.content
    occurrences = body.count("goal archived")
    assert occurrences == 1, (
        f"'goal archived' must appear exactly once in the archive body; "
        f"found {occurrences} occurrence(s). "
        f"Double-write: GoalManager and backend both wrote prose? Body: {body!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 58 — append_history_event() reorders ts-first when ts is NOT the first key
#
# Decision 485-conformance-test-placement-and-shape: the existing TEST 22 passes
# a dict with 'ts' already first — it does NOT exercise the reorder path. This
# test intentionally passes a dict with extra keys BEFORE 'ts' to prove the
# backend actively reorders rather than passing through caller-ordered input.
#
# A backend that naively passes through json.dumps(event) without _make_history_event
# would PASS TEST 22 but FAIL this test — which is the desired discriminator.


def test_append_history_event_reorders_ts_first_when_ts_not_first(
    backend_with_goal: "FilesystemGoalBackend",
    agent_root_with_goal: Path,
) -> None:
    """append_history_event() MUST reorder a caller dict to ts-first/event-second.

    spec/41 JSONL key ordering (addendum): the backend MUST reorder the output
    regardless of input key order. A caller-supplied dict
    {'extra_key': ..., 'ts': ..., 'event': ...} MUST serialize as
    {"ts": ..., "event": ..., "extra_key": ...}.

    Input: ts intentionally NOT the first key (extra_leading, another_key appear
    before ts). This is the only shape that genuinely exercises the reorder path
    rather than verifying a caller-ordered dict passes through unchanged.
    """
    from datetime import datetime

    backend = backend_with_goal
    ts_val = datetime.now().astimezone().isoformat()

    # ts intentionally NOT first — extra keys appear before it.
    backend.append_history_event(
        "agent",
        {
            "extra_leading": "leading_value",
            "another_key": 42,
            "event": "test_reorder_probe",
            "ts": ts_val,
            "after_ts": "trailing_value",
        },
    )

    history = agent_root_with_goal / "goal_history.jsonl"
    lines = [ln for ln in history.read_text().splitlines() if ln.strip()]
    last_line = lines[-1].encode("utf-8")

    # (a) Byte-level: serialized JSON must start with {"ts"
    assert last_line.startswith(b'{"ts"'), (
        f"First key in JSONL line must be 'ts' (byte-level); "
        f"got: {last_line[:60]!r}. "
        f"Backend must REORDER, not pass through caller key order."
    )

    # (b) Parse and verify 'event' is the second key
    parsed_event = json.loads(last_line)
    keys = list(parsed_event.keys())
    assert keys[0] == "ts", f"Parsed first key must be 'ts'; got {keys[0]!r}"
    assert keys[1] == "event", (
        f"Parsed second key must be 'event'; got {keys[1]!r}. Key order: {keys}"
    )

    # (c) All input fields must be present (no data loss on reorder)
    assert parsed_event["extra_leading"] == "leading_value"
    assert parsed_event["another_key"] == 42
    assert parsed_event["after_ts"] == "trailing_value"
    assert parsed_event["event"] == "test_reorder_probe"
    assert parsed_event["ts"] == ts_val


# ──────────────────────────────────────────────────────────────────────────────
# TEST 59 — apply_transition() clock injection via when= parameter (#483 PR1)
#
# Parametrized over BACKEND_FACTORIES. Guards spec/41 MUST 6's injectable-clock
# clause: `when=` controls the ## History prose date prefix ONLY; the JSONL `ts`
# field MUST remain the caller-supplied wall-clock value. The new normative MUST
# (spec/41 line ~190) had no conformance guard before this test — apply_transition's
# clock was only exercised indirectly via the coordinator. This pins both halves:
#   (a) prose date == injected `when` (proves `when` reaches the prose stamp)
#   (b) JSONL ts == caller-supplied wall-clock (proves `when` does NOT bleed into ts)


def test_apply_transition_when_parameter_stamps_prose_not_ts(
    backend_with_goal: "FilesystemGoalBackend",
    agent_root_with_goal: Path,
) -> None:
    """apply_transition(when=pinned) MUST stamp the ## History prose date only.

    spec/41 MUST 6 (injectable clock clause, #483 PR1): `when=` controls the
    `## History` prose bullet date prefix. When `when=None` the backend defaults
    to date.today(). The `when` parameter MUST NOT affect the JSONL `ts` field,
    which is the real wall-clock audit timestamp supplied by the caller via
    history_event['ts'].

    Passes when=date(2026, 1, 1) (cannot match date.today()) and a DISTINCT
    real wall-clock ts so each clock proves its own independence:
      (a) the goal.md ## History prose bullet carries '2026-01-01'
      (b) the JSONL ts equals the caller-supplied wall-clock value verbatim
    """
    from datetime import date, datetime

    backend = backend_with_goal
    pinned = date(2026, 1, 1)
    # Real wall-clock ts, deliberately NOT 2026-01-01 so a `when`→ts bleed fails.
    ts_val = datetime.now().astimezone().isoformat()
    assert not ts_val.startswith("2026-01-01"), (
        "test precondition: wall-clock ts must differ from the pinned `when` date "
        "so the ts-independence assertion is meaningful"
    )

    goal = backend.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="in_progress",
        fields={},
        history_prose="sg1 → in_progress (clock-injection test)",
        history_event={
            "ts": ts_val,
            "event": "clock_inject_probe",
            "sub_goal_id": "sg1",
        },
        when=pinned,
    )

    # (a) Prose date in the ## History body must be the injected `when` date.
    assert "- 2026-01-01 — sg1 → in_progress (clock-injection test)" in goal.body, (
        "apply_transition(when=date(2026,1,1)) must stamp the ## History prose "
        f"bullet with '2026-01-01', not wall-clock today. Body: {goal.body!r}"
    )
    # Durable on reload (prose persisted to goal.md, not just the returned object).
    reloaded = backend.load_goal("agent")
    assert "- 2026-01-01 — " in reloaded.body, (
        "the injected prose date must persist to goal.md on disk; "
        f"reloaded body: {reloaded.body!r}"
    )

    # (b) JSONL ts MUST be the caller-supplied wall-clock value, NOT the `when` date.
    history = agent_root_with_goal / "goal_history.jsonl"
    events = [json.loads(ln) for ln in history.read_text().splitlines() if ln.strip()]
    probe = next(e for e in events if e.get("event") == "clock_inject_probe")
    assert probe["ts"] == ts_val, (
        "apply_transition's `when` MUST NOT bleed into the JSONL ts field; "
        f"ts must equal the caller-supplied wall-clock {ts_val!r}, got {probe['ts']!r}"
    )
    assert not probe["ts"].startswith("2026-01-01"), (
        "the JSONL ts must be the real wall-clock audit timestamp, never the "
        f"injected `when` date prefix; got {probe['ts']!r}"
    )

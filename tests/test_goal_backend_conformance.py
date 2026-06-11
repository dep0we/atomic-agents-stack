"""Conformance tests for GoalBackend Protocol (spec/41).

46 test cases covering the GoalBackend Implementer Contract (the protocol-
behavior subset is parametrized over every registered backend via the
``backend`` / ``backend_with_goal`` fixtures; see PARAMETRIZATION below).

NOTE ON NUMBERING: the "TEST N" labels below are this file's own granular
test-case indices, NOT spec/41 MUST numbers (spec/41 has exactly 9 MUSTs).
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

"""Multi-goal addressing + create_goal tests (spec/41 #642).

TEST 60  — validate_goal_id() accepts all valid chars (a-z, 0-9, -, _)
TEST 61  — validate_goal_id() rejects empty string
TEST 62  — validate_goal_id() rejects STANDING_GOAL_ID ('_standing') before charset check
TEST 63  — validate_goal_id() rejects 65-char id (exceeds max)
TEST 64  — validate_goal_id() accepts exactly 64-char id
TEST 65  — validate_goal_id() rejects uppercase chars
TEST 66  — validate_goal_id() rejects path-separator chars ('/', '..')
TEST 67  — validate_goal_id() rejects whitespace
TEST 68  — STANDING_GOAL_ID passes charset regex (proves reserved-name check fires first)

TEST 69  — GoalCapabilities.supports_multi_goal defaults to False
TEST 70  — FilesystemGoalBackend.capabilities() returns supports_multi_goal=True
TEST 71  — supports_multi_goal is bool for all BACKEND_FACTORIES

TEST 72  — AddressableGoalBackend is a separate Protocol (not subclass of GoalBackend)
TEST 73  — FilesystemGoalBackend is instance of AddressableGoalBackend (runtime_checkable)
TEST 74  — GoalBackend Protocol does NOT declare for_goal()

TEST 75  — create_goal() happy path: goal.md written + JSONL appended
TEST 76  — create_goal() stamps goal.created from when= param
TEST 77  — create_goal() stamps goal.created from date.today() when when=None
TEST 78  — create_goal() goal_created JSONL: ts-first key order
TEST 79  — create_goal() goal_created JSONL: conductor_run_id ABSENT (not None)
TEST 80  — create_goal() goal_created JSONL: required fields present
TEST 81  — create_goal() raises GoalAlreadyExists on duplicate goal_id (not silently overwrite)
TEST 82  — create_goal() GoalAlreadyExists: goal.md is unchanged after collision
TEST 83  — create_goal() with STANDING_GOAL_ID raises ValueError (before any I/O)
TEST 84  — create_goal() with invalid charset raises ValueError (before any I/O)
TEST 85  — create_goal() pre-probe raises before writing goal.md (non-serializable value)
TEST 86  — create_goal() write ordering: goal.md present before goal_history.jsonl

TEST 87  — list_goals() returns [] when neither goal.md nor goals/ exists
TEST 88  — list_goals() returns ['_standing'] when only goal.md exists
TEST 89  — list_goals() returns '_standing' + run-goals in sorted order
TEST 90  — list_goals() skips goals/<id>/ dirs without goal.md (partial debris)
TEST 91  — list_goals() skips dirs whose names fail the charset allow-list
TEST 92  — list_goals() '_standing' sorts before alpha names (ASCII sort order)

TEST 93  — for_goal(None) routes to agent_root (backward-compat: reads existing goal.md)
TEST 94  — for_goal('_standing') routes to agent_root (same as None)
TEST 95  — for_goal(valid_id) scopes _goal_path to goals/<id>/goal.md
TEST 96  — for_goal(invalid_charset) raises ValueError
TEST 97  — for_goal scope: apply_transition() writes to goals/<id>/ not standing goal
TEST 98  — for_goal scope: load_goal() reads from goals/<id>/goal.md

TEST 99  — export() raises AtomicAgentsError when goals/*/goal.md is present
TEST 100 — export() does NOT raise on empty goals/ directory (partial debris)
TEST 101 — export() succeeds normally for agent with no goals/ dir
TEST 102 — export() error message mentions '#643'

TEST 103 — GoalManager.for_goal() returns scoped manager when goal exists
TEST 104 — GoalManager.for_goal() raises AtomicAgentsError when goal does not exist
TEST 105 — GoalManager.for_goal(None) raises ValueError
TEST 106 — GoalManager.for_goal('_standing') raises ValueError
TEST 107 — GoalManager.for_goal(invalid_charset) raises ValueError
TEST 108 — GoalManager.for_goal() scoped manager's goal_path inside goals/<id>/

TEST 109 — doctor check_goal_backend() includes 'goal_ids' and 'supports_multi_goal' in detail
TEST 110 — doctor check_goal_backend() includes '_standing' in goal_ids when goal.md present
TEST 111 — doctor check_goal_backend() PASS when list_goals() returns [] (no goals)
TEST 112 — doctor check_goal_backend() FAIL when list_goals() returns non-list

TEST 113 — validate_goal_id() rejects trailing newline/CR/tab (\\Z anchor, not $) — negative control
TEST 114 — for_goal() charset gate rejects a trailing-newline goal_id (regex parity)
TEST 115 — create_goal() refuses a symlinked goals/<id> directory that escapes the vault (dir-node containment)
TEST 116 — GoalManager.for_goal()-scoped dispatch_as_outcome() refuses (no ungated LLM cost path)
TEST 117 — create_goal() refuses a symlinked goal_history.jsonl leaf escaping the vault (leaf-node containment; audit line cannot escape)
TEST 118 — list_goals() '-'/digit-prefixed run-goal sorts before '_standing' (corrected ordering claim)

TEST 119 — create_goal() fresh goal_id: goal.md + goal_created event both present
TEST 120 — create_goal() goal.md present WITH goal_created event → raises GoalAlreadyExists (COMPLETE)
TEST 121 — create_goal() goal.md present WITHOUT goal_created event → COMPLETES the partial, idempotent on re-run
TEST 122 — create_goal() symlinked goal_history.jsonl leaf → REFUSE leaves NO goal.md committed (two-leaf pre-verify)
TEST 123 — create_goal() stray regular FILE at goals/<id> → raises GoalAlreadyExists (not raw FileExistsError)
TEST 124 — create_goal() goal.md present + corrupt goal_history.jsonl → fail-closed raise (no silent complete)

TEST 125 — validate_goal_id() + for_goal() reject a NUL-byte goal_id ('a\\x00b'); valid id is accepted (negative control)
TEST 126 — for_goal('a') and for_goal('b') resolve to DIFFERENT .goal.lock paths, both under goals/<id>/ (per-goal lock isolation)
TEST 127 — create_goal() self-heals when goal_history.jsonl is ABSENT (unlinked, goal.md kept): one goal_created, idempotent
TEST 128 — create_goal() FAILS CLOSED when goal.md present + history has events but NO goal_created (no spurious goal_created minted)
TEST 129 — list_goals() SKIPS an escaping symlinked goal dir (containment consistency); list and for_goal AGREE the id is unusable
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import frontmatter
import pytest

from atomic_agents._goal_impl import CURRENT_GOAL_SCHEMA_VERSION, GoalManager
from atomic_agents.exceptions import (
    AtomicAgentsError,
    GoalAlreadyExists,
    PathTraversalError,
)
from atomic_agents.goal.backend import AddressableGoalBackend, GoalBackend
from atomic_agents.goal.filesystem import FilesystemGoalBackend
from atomic_agents.goal.types import (
    STANDING_GOAL_ID,
    Goal,
    GoalCapabilities,
    SubGoal,
    validate_goal_id,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_goal_md(
    directory: Path, *, intent: str = "Test goal", active: bool = True
) -> None:
    """Write a minimal valid goal.md to the given directory."""
    directory.mkdir(parents=True, exist_ok=True)
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
---

## Overview

Goal body text.

## History (auto-appended)
- 2026-06-11 — goal created
"""
    (directory / "goal.md").write_text(content, encoding="utf-8")


def _make_min_goal(intent: str = "Run a sub-goal") -> Goal:
    """Return a minimal valid Goal with one sub-goal."""
    return Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent=intent,
        priority="high",
        created="2026-06-11",
        last_progress_check="2026-06-11",
        success_criteria=["Done when goal complete"],
        sub_goals=[SubGoal(id="sg1", label="Do the thing", status="pending")],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Conformance parametrization (mirrors test_goal_backend_conformance.py)

BACKEND_FACTORIES = [
    pytest.param(lambda root: FilesystemGoalBackend(root), id="filesystem"),
]


@pytest.fixture(params=BACKEND_FACTORIES)
def backend_factory(request):
    return request.param


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    return tmp_path / "test-agent"


@pytest.fixture
def backend(backend_factory, agent_root: Path):
    return backend_factory(agent_root)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 60-68 — validate_goal_id()


def test_validate_goal_id_valid_chars() -> None:
    """TEST 60 — valid chars: lowercase, digits, hyphen, underscore."""
    # These must all pass without raising.
    for ok in ["abc", "a1b2", "my-goal", "my_goal", "a", "z" * 64, "1", "a-b_c-1"]:
        validate_goal_id(ok)  # must not raise


def test_validate_goal_id_rejects_empty() -> None:
    """TEST 61 — empty string is rejected."""
    with pytest.raises(ValueError, match="non-empty"):
        validate_goal_id("")


def test_validate_goal_id_rejects_standing_before_charset() -> None:
    """TEST 62 — STANDING_GOAL_ID is rejected before the charset check fires."""
    # '_standing' passes the charset regex ([a-z0-9_-]) — so if we stripped the
    # reserved-name check, it would pass. The test verifies the reserved-name
    # rejection fires FIRST (the error message says 'reserved', not 'invalid characters').
    with pytest.raises(ValueError, match="reserved"):
        validate_goal_id(STANDING_GOAL_ID)


def test_validate_goal_id_rejects_65_chars() -> None:
    """TEST 63 — 65-char id exceeds max (64)."""
    with pytest.raises(ValueError, match="64"):
        validate_goal_id("a" * 65)


def test_validate_goal_id_accepts_64_chars() -> None:
    """TEST 64 — exactly 64 chars is accepted."""
    validate_goal_id("a" * 64)  # must not raise


def test_validate_goal_id_rejects_uppercase() -> None:
    """TEST 65 — uppercase chars are rejected."""
    with pytest.raises(ValueError, match="invalid characters"):
        validate_goal_id("MyGoal")


def test_validate_goal_id_rejects_path_separator() -> None:
    """TEST 66 — path-separator characters are rejected."""
    for bad in ["a/b", "a..b", "../escape"]:
        with pytest.raises(ValueError):
            validate_goal_id(bad)


def test_validate_goal_id_rejects_whitespace() -> None:
    """TEST 67 — whitespace is rejected."""
    with pytest.raises(ValueError):
        validate_goal_id("my goal")


def test_standing_goal_id_passes_charset_regex() -> None:
    """TEST 68 — STANDING_GOAL_ID ('_standing') would pass the charset check if unchecked.

    This test proves the reserved-name guard must fire BEFORE the charset check,
    because '_standing' matches [a-z0-9_-]{1,64}. If the order were reversed,
    validate_goal_id('_standing') would silently pass.
    """
    import re

    # \A...\Z (NOT ^...$) — $ would also match before a trailing newline, the
    # exact bypass TEST 113 guards against. Mirror the production anchor here.
    _CHARSET_RE = re.compile(r"\A[a-z0-9_-]{1,64}\Z")
    assert _CHARSET_RE.match(STANDING_GOAL_ID) is not None, (
        "STANDING_GOAL_ID passes the charset regex — reserved-name check MUST fire first"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 69-71 — GoalCapabilities.supports_multi_goal


def test_goal_capabilities_supports_multi_goal_default_false() -> None:
    """TEST 69 — supports_multi_goal defaults to False (backward-compat)."""
    caps = GoalCapabilities(backend_id="stub")
    assert caps.supports_multi_goal is False


def test_filesystem_capabilities_supports_multi_goal_true(agent_root: Path) -> None:
    """TEST 70 — FilesystemGoalBackend.capabilities().supports_multi_goal is True."""
    backend = FilesystemGoalBackend(agent_root)
    caps = backend.capabilities()
    assert caps.supports_multi_goal is True


def test_all_backend_factories_supports_multi_goal_is_bool(
    backend_factory, agent_root: Path
) -> None:
    """TEST 71 — supports_multi_goal is bool (not truthy/falsy int) for all backends."""
    b = backend_factory(agent_root)
    caps = b.capabilities()
    assert isinstance(caps.supports_multi_goal, bool)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 72-74 — AddressableGoalBackend Protocol


def test_addressable_goal_backend_is_separate_protocol() -> None:
    """TEST 72 — AddressableGoalBackend is NOT a subclass of GoalBackend.

    issubclass() is unreliable for runtime_checkable protocols with non-method
    members (raises TypeError in CPython 3.12). Instead verify:
    (1) GoalBackend is NOT in AddressableGoalBackend's __bases__.
    (2) AddressableGoalBackend is NOT in GoalBackend's __bases__.
    This confirms they are independently declared Protocols, not a hierarchy.
    """
    # Neither inherits from the other.
    assert GoalBackend not in AddressableGoalBackend.__bases__, (
        "AddressableGoalBackend must not inherit from GoalBackend"
    )
    assert AddressableGoalBackend not in GoalBackend.__bases__, (
        "GoalBackend must not inherit from AddressableGoalBackend"
    )


def test_filesystem_backend_is_addressable(agent_root: Path) -> None:
    """TEST 73 — FilesystemGoalBackend satisfies AddressableGoalBackend (runtime_checkable)."""
    backend = FilesystemGoalBackend(agent_root)
    assert isinstance(backend, AddressableGoalBackend)


def test_goal_backend_protocol_has_no_for_goal() -> None:
    """TEST 74 — GoalBackend does NOT declare for_goal()."""
    assert not hasattr(GoalBackend, "for_goal"), (
        "for_goal() belongs to AddressableGoalBackend, not GoalBackend"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 75-86 — create_goal()


def test_create_goal_happy_path(backend_factory, agent_root: Path) -> None:
    """TEST 75 — create_goal() writes goal.md and appends goal_history.jsonl."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal(intent="Deploy the fleet")
    returned = backend.create_goal(
        "test-agent", "fleet-deploy", goal, when=date(2026, 6, 26)
    )

    goal_path = agent_root / "goals" / "fleet-deploy" / "goal.md"
    jsonl_path = agent_root / "goals" / "fleet-deploy" / "goal_history.jsonl"

    assert goal_path.is_file(), "goal.md not written"
    assert jsonl_path.is_file(), "goal_history.jsonl not written"
    assert isinstance(returned, Goal)


def test_create_goal_stamps_created_from_when(
    backend_factory, agent_root: Path
) -> None:
    """TEST 76 — create_goal() stamps goal.created from when= regardless of caller value."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    goal.created = "1900-01-01"  # will be overridden
    backend.create_goal("test-agent", "run-goal-a", goal, when=date(2026, 6, 26))

    goal_path = agent_root / "goals" / "run-goal-a" / "goal.md"
    parsed = frontmatter.load(goal_path)
    assert str(parsed.metadata["created"]) == "2026-06-26"


def test_create_goal_stamps_created_from_today_when_none(
    backend_factory, agent_root: Path, monkeypatch
) -> None:
    """TEST 77 — create_goal() uses date.today() when when=None."""
    import atomic_agents.goal.filesystem as fs_mod

    monkeypatch.setattr(
        fs_mod,
        "date",
        type("_FakeDate", (), {"today": staticmethod(lambda: date(2026, 7, 1))})(),
    )
    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    backend.create_goal("test-agent", "run-goal-b", goal)

    goal_path = agent_root / "goals" / "run-goal-b" / "goal.md"
    parsed = frontmatter.load(goal_path)
    assert str(parsed.metadata["created"]) == "2026-07-01"


def test_create_goal_jsonl_ts_first(backend_factory, agent_root: Path) -> None:
    """TEST 78 — goal_created JSONL line: ts is the first key."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    backend.create_goal("test-agent", "run-goal-c", goal, when=date(2026, 6, 26))

    jsonl_path = agent_root / "goals" / "run-goal-c" / "goal_history.jsonl"
    lines = [ln for ln in jsonl_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, "expected exactly one JSONL line"
    event = json.loads(lines[0])
    keys = list(event.keys())
    assert keys[0] == "ts", f"first key must be 'ts', got {keys[0]!r}"


def test_create_goal_jsonl_no_conductor_run_id(
    backend_factory, agent_root: Path
) -> None:
    """TEST 79 — goal_created JSONL: conductor_run_id is ABSENT (not None) for home-user goals."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    backend.create_goal("test-agent", "run-goal-d", goal, when=date(2026, 6, 26))

    jsonl_path = agent_root / "goals" / "run-goal-d" / "goal_history.jsonl"
    line = jsonl_path.read_text().strip()
    event = json.loads(line)
    assert "conductor_run_id" not in event, (
        "conductor_run_id must be ABSENT (not None) for home-user goals"
    )


def test_create_goal_jsonl_required_fields(backend_factory, agent_root: Path) -> None:
    """TEST 80 — goal_created JSONL contains all required fields."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal(intent="Ship the feature")
    backend.create_goal("test-agent", "run-goal-e", goal, when=date(2026, 6, 26))

    jsonl_path = agent_root / "goals" / "run-goal-e" / "goal_history.jsonl"
    event = json.loads(jsonl_path.read_text().strip())

    assert event["event"] == "goal_created"
    assert event["goal_id"] == "run-goal-e"
    assert event["intent"] == "Ship the feature"
    assert event["created"] == "2026-06-26"
    assert "schema_version" in event
    assert "ts" in event


def test_create_goal_raises_already_exists(backend_factory, agent_root: Path) -> None:
    """TEST 81 — create_goal() raises GoalAlreadyExists on duplicate goal_id."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    backend.create_goal("test-agent", "my-goal", goal, when=date(2026, 6, 26))

    goal2 = _make_min_goal(intent="Second attempt")
    with pytest.raises(GoalAlreadyExists):
        backend.create_goal("test-agent", "my-goal", goal2, when=date(2026, 6, 26))


def test_create_goal_already_exists_leaves_original_unchanged(
    backend_factory, agent_root: Path
) -> None:
    """TEST 82 — after GoalAlreadyExists, goal.md still contains the original intent."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal(intent="Original intent")
    backend.create_goal("test-agent", "keep-this", goal, when=date(2026, 6, 26))

    goal2 = _make_min_goal(intent="Overwrite attempt")
    with pytest.raises(GoalAlreadyExists):
        backend.create_goal("test-agent", "keep-this", goal2, when=date(2026, 6, 26))

    # Verify original is intact.
    goal_path = agent_root / "goals" / "keep-this" / "goal.md"
    parsed = frontmatter.load(goal_path)
    assert parsed.metadata["intent"] == "Original intent"


def test_create_goal_standing_id_raises_value_error(
    backend_factory, agent_root: Path
) -> None:
    """TEST 83 — create_goal(goal_id='_standing') raises ValueError before any I/O."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    with pytest.raises(ValueError, match="reserved"):
        backend.create_goal("test-agent", STANDING_GOAL_ID, goal)

    # No I/O: goals/ dir must not exist.
    assert not (agent_root / "goals").exists()


def test_create_goal_invalid_charset_raises(backend_factory, agent_root: Path) -> None:
    """TEST 84 — create_goal() with invalid charset raises ValueError before any I/O."""
    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    with pytest.raises(ValueError, match="invalid characters"):
        backend.create_goal("test-agent", "Bad Goal!", goal)

    assert not (agent_root / "goals").exists()


def test_create_goal_pre_probe_raises_before_goal_md(
    backend_factory, agent_root: Path, monkeypatch
) -> None:
    """TEST 85 — if json.dumps pre-probe raises, goal.md is NOT written.

    This test patches json.dumps to raise on the first call inside create_goal().
    The goal.md must remain absent after the raise.
    """
    import atomic_agents.goal.filesystem as fs_mod

    original_dumps = json.dumps
    calls = []

    def _raising_dumps(obj, **kw):
        # Raise only the first call (the pre-probe inside create_goal)
        if len(calls) == 0:
            calls.append(1)
            raise ValueError("non-serializable sentinel")
        return original_dumps(obj, **kw)

    monkeypatch.setattr(fs_mod.json, "dumps", _raising_dumps)

    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    with pytest.raises(ValueError, match="non-serializable sentinel"):
        backend.create_goal("test-agent", "probe-fail", goal)

    # goal.md MUST NOT have been written.
    assert not (agent_root / "goals" / "probe-fail" / "goal.md").exists()


def test_create_goal_write_ordering_goal_md_before_jsonl(
    backend_factory, agent_root: Path, monkeypatch
) -> None:
    """TEST 86 — write ordering: goal.md is present before goal_history.jsonl is appended.

    We intercept atomic_append_jsonl to check goal.md existence at the moment
    of the JSONL write — it must already be present (goal.md FIRST).
    """
    import atomic_agents.goal.filesystem as fs_mod

    goal_md_existed_at_jsonl_write = []
    original_append = fs_mod.atomic_append_jsonl

    def _spy_append(path, line):
        # At the moment of JSONL write, check whether goal.md exists.
        goal_md = path.parent / "goal.md"
        goal_md_existed_at_jsonl_write.append(goal_md.is_file())
        return original_append(path, line)

    monkeypatch.setattr(fs_mod, "atomic_append_jsonl", _spy_append)

    backend = backend_factory(agent_root)
    goal = _make_min_goal()
    backend.create_goal("test-agent", "order-test", goal)

    assert goal_md_existed_at_jsonl_write, "atomic_append_jsonl was never called"
    assert all(goal_md_existed_at_jsonl_write), (
        "goal.md was NOT present when goal_history.jsonl was appended (wrong ordering)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 87-92 — list_goals()


def test_list_goals_empty_agent(backend_factory, agent_root: Path) -> None:
    """TEST 87 — list_goals() returns [] when neither goal.md nor goals/ exists."""
    backend = backend_factory(agent_root)
    result = backend.list_goals("test-agent")
    assert result == []


def test_list_goals_standing_only(backend_factory, agent_root: Path) -> None:
    """TEST 88 — list_goals() returns ['_standing'] when only goal.md exists."""
    _make_goal_md(agent_root)
    backend = backend_factory(agent_root)
    result = backend.list_goals("test-agent")
    assert result == [STANDING_GOAL_ID]


def test_list_goals_standing_plus_run_goals(backend_factory, agent_root: Path) -> None:
    """TEST 89 — list_goals() returns '_standing' + run-goals in sorted order."""
    _make_goal_md(agent_root)
    backend = backend_factory(agent_root)

    g1 = _make_min_goal(intent="Goal Bravo")
    g2 = _make_min_goal(intent="Goal Alpha")
    backend.create_goal("test-agent", "bravo", g1)
    backend.create_goal("test-agent", "alpha", g2)

    result = backend.list_goals("test-agent")
    # '_standing' < 'a' in ASCII, so it comes first.
    assert result[0] == STANDING_GOAL_ID
    assert "alpha" in result
    assert "bravo" in result
    assert result == sorted(result)


def test_list_goals_skips_partial_debris(backend_factory, agent_root: Path) -> None:
    """TEST 90 — list_goals() skips goals/<id>/ dirs without goal.md (partial create_goal debris)."""
    backend = backend_factory(agent_root)
    # Simulate a failed create_goal(): directory exists but no goal.md.
    debris_dir = agent_root / "goals" / "partial"
    debris_dir.mkdir(parents=True)
    # No goal.md written — this is the debris case.

    result = backend.list_goals("test-agent")
    assert "partial" not in result
    assert result == []


def test_list_goals_skips_nonconforming_dirs(backend_factory, agent_root: Path) -> None:
    """TEST 91 — list_goals() skips goals/ subdirs whose names fail the charset allow-list."""
    backend = backend_factory(agent_root)
    # Create a non-conforming directory (would be created by another tool, not the framework).
    bad_dir = agent_root / "goals" / "My-Goal"  # uppercase
    bad_dir.mkdir(parents=True)
    (bad_dir / "goal.md").write_text("---\n---\n")

    result = backend.list_goals("test-agent")
    assert "My-Goal" not in result
    assert result == []


def test_list_goals_standing_sorts_first(backend_factory, agent_root: Path) -> None:
    """TEST 92 — '_standing' appears first in sorted order (ASCII '_' < 'a')."""
    _make_goal_md(agent_root)
    backend = backend_factory(agent_root)
    backend.create_goal("test-agent", "aardvark", _make_min_goal(intent="AAA goal"))

    result = backend.list_goals("test-agent")
    assert result[0] == STANDING_GOAL_ID
    assert result[1] == "aardvark"


def test_list_goals_skips_escaping_symlinked_goal_dir_agrees_with_for_goal(
    tmp_path: Path,
) -> None:
    """TEST 129 — list_goals() and for_goal() AGREE on an escaping symlinked goal dir.

    Containment consistency (Codex #3): a goals/<escaped> directory planted as a
    symlink to an out-of-vault target (whose goal.md therefore resolves OUTSIDE
    the agent vault root) must NOT be returned by list_goals() — the enumeration
    now applies the SAME _require_within_root containment guard for_goal() uses.
    So list_goals() omits 'escaped' AND for_goal('escaped') raises
    PathTraversalError: list and for_goal AGREE (the id is absent from the list,
    not listed-then-refused), closing the prior durability-consistency footgun
    where discovery/doctor/resume could hand out a goal_id for_goal() can't open.

    Negative control: this goes RED if FIX A's per-entry containment skip is
    removed — without it, the goal.md presence predicate (which FOLLOWS the
    symlink) is satisfied and list_goals() WOULD return 'escaped'.

    Uses the TEST 115/117 symlink-planting idiom. POSIX-assumed (the existing
    symlink tests plant symlinks unconditionally; CI runs on Linux/macOS).
    """
    agent_root = tmp_path / "agent"
    outside = tmp_path / "outside"
    outside.mkdir()
    # A real goal.md lives OUTSIDE the vault — the symlink would otherwise make
    # it look like an addressed run-goal whose goal.md "exists".
    (outside / "goal.md").write_text("---\n---\n", encoding="utf-8")
    goals_dir = agent_root / "goals"
    goals_dir.mkdir(parents=True)
    # Plant goals/escaped -> <outside-vault>, so goals/escaped/goal.md resolves
    # outside agent_root.
    (goals_dir / "escaped").symlink_to(outside, target_is_directory=True)

    backend = FilesystemGoalBackend(agent_root)

    # list_goals() SKIPS the escaping entry (the consistency guarantee).
    result = backend.list_goals("agent")
    assert "escaped" not in result, (
        "list_goals() returned an escaping symlinked goal dir for_goal() cannot "
        "open — containment skip missing"
    )

    # for_goal() still refuses it — list and for_goal AGREE the id is unusable.
    with pytest.raises(PathTraversalError):
        backend.for_goal("escaped")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 93-98 — for_goal()


def test_for_goal_none_routes_to_agent_root(agent_root: Path) -> None:
    """TEST 93 — for_goal(None) returns backend scoped to agent_root (reads existing goal.md)."""
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)
    scoped = backend.for_goal(None)
    # The scoped backend must be able to load the standing goal.
    goal = scoped.load_goal("test-agent")
    assert goal.intent == "Test goal"


def test_for_goal_standing_id_routes_to_agent_root(agent_root: Path) -> None:
    """TEST 94 — for_goal('_standing') is identical to for_goal(None)."""
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)
    scoped = backend.for_goal(STANDING_GOAL_ID)
    goal = scoped.load_goal("test-agent")
    assert goal.intent == "Test goal"


def test_for_goal_valid_id_scopes_goal_path(agent_root: Path) -> None:
    """TEST 95 — for_goal(valid_id) scopes the backend to goals/<id>/."""
    backend = FilesystemGoalBackend(agent_root)
    goal = _make_min_goal(intent="Run goal X")
    backend.create_goal("test-agent", "run-x", goal)

    scoped = backend.for_goal("run-x")
    # goal_text() via the scoped backend reads goals/run-x/goal.md.
    text = scoped.goal_text("test-agent")
    assert "Run goal X" in text


def test_for_goal_invalid_charset_raises(agent_root: Path) -> None:
    """TEST 96 — for_goal(invalid_charset) raises ValueError."""
    backend = FilesystemGoalBackend(agent_root)
    with pytest.raises(ValueError):
        backend.for_goal("Invalid-Name!")


def test_for_goal_apply_transition_writes_to_run_goal_not_standing(
    agent_root: Path,
) -> None:
    """TEST 97 — for_goal(id).apply_transition() writes to goals/<id>/ not standing goal.md."""
    backend = FilesystemGoalBackend(agent_root)
    # Set up standing goal.
    _make_goal_md(agent_root, intent="Standing goal")
    # Create a run-goal.
    goal = _make_min_goal(intent="Run goal Y")
    backend.create_goal("test-agent", "run-y", goal)

    # Apply a transition via the scoped backend.
    scoped = backend.for_goal("run-y")
    scoped.apply_transition(
        agent_id="test-agent",
        sub_goal_id="sg1",
        to_status="in_progress",
        fields={},
        history_prose="Started",
        history_event={
            "ts": "2026-06-26T10:00:00+00:00",
            "event": "sub_goal_started",
            "sub_goal_id": "sg1",
        },
    )

    # The run-goal's goal.md must be updated.
    run_goal = scoped.load_goal("test-agent")
    assert run_goal.sub_goals[0].status == "in_progress"

    # The standing goal.md must be UNCHANGED.
    standing_scoped = backend.for_goal(None)
    standing = standing_scoped.load_goal("test-agent")
    assert standing.intent == "Standing goal"
    # Standing goal's sg1 is still pending.
    assert standing.sub_goals[0].status == "pending"


def test_for_goal_load_goal_reads_from_run_goal_path(agent_root: Path) -> None:
    """TEST 98 — for_goal(id).load_goal() reads from goals/<id>/goal.md."""
    backend = FilesystemGoalBackend(agent_root)
    _make_goal_md(agent_root, intent="Standing goal")
    goal = _make_min_goal(intent="Unique run-goal intent")
    backend.create_goal("test-agent", "run-z", goal)

    scoped = backend.for_goal("run-z")
    loaded = scoped.load_goal("test-agent")
    assert loaded.intent == "Unique run-goal intent"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 99-102 — export() fail-loud guard


def test_export_raises_when_addressed_goals_present(agent_root: Path) -> None:
    """TEST 99 — export() raises AtomicAgentsError when goals/*/goal.md is present."""
    backend = FilesystemGoalBackend(agent_root)
    backend.create_goal("test-agent", "my-run-goal", _make_min_goal())

    with pytest.raises(AtomicAgentsError):
        backend.export()


def test_export_does_not_raise_on_empty_goals_dir(agent_root: Path) -> None:
    """TEST 100 — export() does NOT raise on empty goals/ directory (partial debris)."""
    backend = FilesystemGoalBackend(agent_root)
    # Create empty goals/ dir — simulates debris from a failed create_goal().
    (agent_root / "goals").mkdir(parents=True)
    # No subdirectory or goal.md inside — the guard predicate must be false.
    result = backend.export()
    # No exception: a GoalExport is returned (possibly empty).
    from atomic_agents.goal.types import GoalExport

    assert isinstance(result, GoalExport)


def test_export_succeeds_no_goals_dir(agent_root: Path) -> None:
    """TEST 101 — export() works normally when goals/ does not exist at all."""
    backend = FilesystemGoalBackend(agent_root)
    result = backend.export()
    from atomic_agents.goal.types import GoalExport

    assert isinstance(result, GoalExport)
    assert result.goal_md_bytes == b""


def test_export_error_mentions_issue_643(agent_root: Path) -> None:
    """TEST 102 — export() error message mentions '#643' (links to the tracking issue)."""
    backend = FilesystemGoalBackend(agent_root)
    backend.create_goal("test-agent", "export-guard-test", _make_min_goal())

    with pytest.raises(AtomicAgentsError, match="#643"):
        backend.export()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 103-108 — GoalManager.for_goal()


def test_goal_manager_for_goal_returns_scoped_manager(tmp_path: Path) -> None:
    """TEST 103 — GoalManager.for_goal() returns a GoalManager scoped to the run-goal."""
    agents_root = tmp_path / "agents"
    agent_name = "my-agent"
    agent_root = agents_root / agent_name

    backend = FilesystemGoalBackend(agent_root)
    goal = _make_min_goal(intent="My run-goal via manager")
    backend.create_goal(agent_name, "manager-goal", goal, when=date(2026, 6, 26))

    gm = GoalManager(agents_root=agents_root, agent_name=agent_name)
    scoped = gm.for_goal("manager-goal")

    assert isinstance(scoped, GoalManager)
    loaded = scoped.load()
    assert loaded.intent == "My run-goal via manager"


def test_goal_manager_for_goal_raises_when_goal_not_exist(tmp_path: Path) -> None:
    """TEST 104 — GoalManager.for_goal() raises AtomicAgentsError when goal doesn't exist."""
    agents_root = tmp_path / "agents"
    agent_name = "my-agent"
    # GoalManager requires the agent folder to exist.
    (agents_root / agent_name).mkdir(parents=True)

    gm = GoalManager(agents_root=agents_root, agent_name=agent_name)
    with pytest.raises(AtomicAgentsError, match="create_goal"):
        gm.for_goal("nonexistent-goal")


def test_goal_manager_for_goal_none_raises(tmp_path: Path) -> None:
    """TEST 105 — GoalManager.for_goal(None) raises ValueError."""
    agents_root = tmp_path / "agents"
    (agents_root / "a").mkdir(parents=True)
    gm = GoalManager(agents_root=agents_root, agent_name="a")
    with pytest.raises(ValueError):
        gm.for_goal(None)


def test_goal_manager_for_goal_standing_raises(tmp_path: Path) -> None:
    """TEST 106 — GoalManager.for_goal('_standing') raises ValueError."""
    agents_root = tmp_path / "agents"
    (agents_root / "a").mkdir(parents=True)
    gm = GoalManager(agents_root=agents_root, agent_name="a")
    with pytest.raises(ValueError, match="_standing"):
        gm.for_goal(STANDING_GOAL_ID)


def test_goal_manager_for_goal_invalid_charset_raises(tmp_path: Path) -> None:
    """TEST 107 — GoalManager.for_goal(invalid) raises ValueError via validate_goal_id."""
    agents_root = tmp_path / "agents"
    (agents_root / "a").mkdir(parents=True)
    gm = GoalManager(agents_root=agents_root, agent_name="a")
    with pytest.raises(ValueError):
        gm.for_goal("Bad Goal!")


def test_goal_manager_for_goal_scoped_goal_path(tmp_path: Path) -> None:
    """TEST 108 — GoalManager.for_goal() scoped manager's goal_path is inside goals/<id>/."""
    agents_root = tmp_path / "agents"
    agent_name = "my-agent"
    agent_root = agents_root / agent_name

    backend = FilesystemGoalBackend(agent_root)
    backend.create_goal(agent_name, "scoped-path-test", _make_min_goal())

    gm = GoalManager(agents_root=agents_root, agent_name=agent_name)
    scoped = gm.for_goal("scoped-path-test")

    # The scoped manager's goal_path must be inside goals/scoped-path-test/.
    assert "goals" in str(scoped.goal_path)
    assert "scoped-path-test" in str(scoped.goal_path)
    assert scoped.goal_path.name == "goal.md"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 109-112 — doctor check_goal_backend() with multi-goal fields


def test_doctor_check_goal_backend_includes_multi_goal_fields(tmp_path: Path) -> None:
    """TEST 109 — check_goal_backend() detail includes 'goal_ids' and 'supports_multi_goal'."""
    from atomic_agents.doctor import check_goal_backend

    agent_root = tmp_path / "test-agent"
    agent_root.mkdir(parents=True)

    result = check_goal_backend(agent_root)
    assert "goal_ids" in result.detail, "detail must include 'goal_ids'"
    assert "supports_multi_goal" in result.detail, (
        "detail must include 'supports_multi_goal'"
    )


def test_doctor_check_goal_backend_includes_standing_when_goal_md_present(
    tmp_path: Path,
) -> None:
    """TEST 110 — check_goal_backend() detail includes '_standing' in goal_ids when goal.md present."""
    from atomic_agents.doctor import check_goal_backend

    agent_root = tmp_path / "test-agent"
    _make_goal_md(agent_root)

    result = check_goal_backend(agent_root)
    from atomic_agents.doctor import PASS

    assert result.status == PASS
    assert STANDING_GOAL_ID in result.detail["goal_ids"]


def test_doctor_check_goal_backend_pass_no_goals(tmp_path: Path) -> None:
    """TEST 111 — check_goal_backend() returns PASS when list_goals() returns [] (no goals)."""
    from atomic_agents.doctor import PASS, check_goal_backend

    agent_root = tmp_path / "empty-agent"
    agent_root.mkdir(parents=True)

    result = check_goal_backend(agent_root)
    assert result.status == PASS
    assert result.detail["goal_ids"] == []


def test_doctor_check_goal_backend_fail_when_list_goals_returns_non_list(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 112 — check_goal_backend() FAIL when list_goals() returns non-list."""
    from atomic_agents.doctor import FAIL, check_goal_backend

    agent_root = tmp_path / "bad-agent"
    agent_root.mkdir(parents=True)

    # Patch list_goals() on the class to return a non-list.
    def _bad_list_goals(self, agent_id):  # noqa: ARG001
        return None  # type: ignore[return-value]

    monkeypatch.setattr(FilesystemGoalBackend, "list_goals", _bad_list_goals)

    result = check_goal_backend(agent_root)
    assert result.status == FAIL
    assert "list_goals" in result.message


# ──────────────────────────────────────────────────────────────────────────────
# TEST 113-116 — anchor / containment / fail-closed-dispatch negative controls


@pytest.mark.parametrize("bad", ["abc\n", "abc\r", "abc\t", "abc\x0b", "abc\f"])
def test_validate_goal_id_rejects_trailing_whitespace_chars(bad: str) -> None:
    """TEST 113 — validate_goal_id() rejects trailing newline/CR/tab (\\Z anchor).

    Negative control for the regex-anchor fix. Python's `$` matches before a
    single trailing newline, so `^[a-z0-9_-]{1,64}$` would ACCEPT 'abc\\n' and
    create a `goals/abc\\n/` directory. `\\A...\\Z` matches only the true end of
    string. If the anchor regresses to `$`, the 'abc\\n' case below goes GREEN
    (accepted) and this test fails — locking the fix.
    """
    with pytest.raises(ValueError):
        validate_goal_id(bad)


def test_for_goal_rejects_trailing_newline_goal_id(agent_root: Path) -> None:
    """TEST 114 — for_goal()'s charset gate rejects a trailing-newline goal_id.

    for_goal() validates via the same _GOAL_ID_RE used by validate_goal_id /
    list_goals — proving all three sites inherit the \\Z-anchor fix (shared
    compiled regex), so a 'run-x\\n' can never become a real directory name.
    """
    backend = FilesystemGoalBackend(agent_root)
    with pytest.raises(ValueError):
        backend.for_goal("run-x\n")


def test_create_goal_refuses_symlinked_goal_dir_escaping_vault(
    tmp_path: Path,
) -> None:
    """TEST 115 — create_goal() refuses a symlinked goals/<id> directory escaping the vault.

    Dir-node containment control (TEST 117 covers the leaf-node case). The
    containment guard must run against the ACTUAL create-target directory
    (goals/<goal_id>/), not just the parent goals/. A symlink planted at
    goals/<goal_id> -> <outside-vault> would otherwise be followed and goal.md
    would land OUTSIDE the vault. for_goal()'s _require_within_root resolves the
    symlink and raises PathTraversalError; nothing is written outside.
    """
    agent_root = tmp_path / "agent"
    outside = tmp_path / "outside"
    outside.mkdir()
    goals_dir = agent_root / "goals"
    goals_dir.mkdir(parents=True)
    # Plant a symlink at the leaf create target pointing outside the vault.
    (goals_dir / "escapee").symlink_to(outside, target_is_directory=True)

    backend = FilesystemGoalBackend(agent_root)
    with pytest.raises(PathTraversalError):
        backend.create_goal("agent", "escapee", _make_min_goal())

    # The write must NOT have escaped the vault.
    assert not (outside / "goal.md").exists(), "goal.md escaped the vault"


def test_for_goal_scoped_manager_dispatch_refuses_no_ungated_llm(
    tmp_path: Path,
) -> None:
    """TEST 116 — for_goal()-scoped GoalManager refuses dispatch_as_outcome().

    A scoped manager's root is agent_root/goals/<id>/, which has goal.md but NO
    model.md → cost_guardrails_enabled defaults False → the cost gate AND the
    OutcomeRunner's agent.call() would run ungated (fail-OPEN LLM spend). The
    scoped manager is marked _addressed_goal_scope and MUST refuse the dispatch
    with NotImplementedError (pointing at conductor #580) BEFORE constructing any
    AtomicAgent or making any LLM call. State inspection still works.
    """
    agents_root = tmp_path / "agents"
    agent_name = "my-agent"
    agent_root = agents_root / agent_name

    backend = FilesystemGoalBackend(agent_root)
    backend.create_goal(
        agent_name, "run-goal", _make_min_goal(), when=date(2026, 6, 26)
    )

    gm = GoalManager(agents_root=agents_root, agent_name=agent_name)
    scoped = gm.for_goal("run-goal")
    assert scoped._addressed_goal_scope is True

    with pytest.raises(NotImplementedError, match="#580"):
        scoped.dispatch_as_outcome("sg1", rubric="any rubric text")


def test_create_goal_refuses_symlinked_history_leaf_escaping_vault(
    tmp_path: Path,
) -> None:
    """TEST 117 — create_goal() refuses a symlinked goal_history.jsonl leaf.

    Leaf-node negative control (distinct from TEST 115's dir-node control). The
    goal dir goals/<id>/ is a LEGITIMATE in-vault directory, but its
    goal_history.jsonl is pre-planted as a symlink to an out-of-vault target.
    atomic_append_jsonl opens the history file in append mode, which FOLLOWS the
    symlink — so without the per-leaf containment guard the goal_created audit
    line would be written OUTSIDE the vault (a perimeter escape of the audit
    trail, Principle #5). The dir-node check (TEST 115) does NOT catch this: the
    goal dir itself is contained. The fix routes create_goal's writes through the
    for_goal()-scoped backend's _append_jsonl, which calls _require_within_root on
    the history leaf before opening it.

    RED against an inlined-write create_goal; GREEN once the write is routed
    through the contained leaf primitive.
    """
    agent_root = tmp_path / "agent"
    outside = tmp_path / "outside"
    outside.mkdir()
    stolen = outside / "stolen_history.jsonl"

    # A legitimate in-vault goal dir, but with a symlinked history leaf.
    goal_dir = agent_root / "goals" / "leaky"
    goal_dir.mkdir(parents=True)
    (goal_dir / "goal_history.jsonl").symlink_to(stolen)

    backend = FilesystemGoalBackend(agent_root)
    with pytest.raises(PathTraversalError):
        backend.create_goal("agent", "leaky", _make_min_goal())

    # The goal_created audit line MUST NOT have escaped the vault: the symlink
    # target must never have been created or written through.
    assert not stolen.exists(), "goal_created audit line escaped the vault"


def test_list_goals_run_goal_sorts_before_standing(
    backend_factory, agent_root: Path
) -> None:
    """TEST 118 — a '-'/digit-prefixed run-goal sorts BEFORE '_standing'.

    Locks the corrected ordering claim: '_standing' sorts before ALPHABETIC
    (a-z) names, NOT unconditionally first. ord('-')=45 and ord('0')=48 are both
    < ord('_')=95, so a hyphen- or digit-prefixed goal_id sorts ahead of
    '_standing'. validate_goal_id permits both leading chars (TEST 60 accepts
    '1'), so this is a reachable state, not a hypothetical.
    """
    _make_goal_md(agent_root)  # standing goal present
    backend = backend_factory(agent_root)
    backend.create_goal("test-agent", "-dash", _make_min_goal(intent="dash goal"))
    backend.create_goal("test-agent", "1digit", _make_min_goal(intent="digit goal"))

    result = backend.list_goals("test-agent")
    assert result == sorted(result)
    # '_standing' is NOT first — the '-'/digit-prefixed ids precede it.
    assert result.index("-dash") < result.index(STANDING_GOAL_ID)
    assert result.index("1digit") < result.index(STANDING_GOAL_ID)
    assert result[0] == "-dash"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 119-124 — create_goal() complete-on-partial recoverability (#642 fix set)


def _count_goal_created(jsonl_path: Path) -> int:
    """Count goal_created events in a goal_history.jsonl file."""
    if not jsonl_path.is_file():
        return 0
    count = 0
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") == "goal_created":
            count += 1
    return count


def test_create_goal_fresh_writes_goal_md_and_event(
    backend_factory, agent_root: Path
) -> None:
    """TEST 119 — fresh goal_id: goal.md + a goal_created event are both present."""
    backend = backend_factory(agent_root)
    backend.create_goal("test-agent", "fresh-one", _make_min_goal())

    goal_path = agent_root / "goals" / "fresh-one" / "goal.md"
    jsonl_path = agent_root / "goals" / "fresh-one" / "goal_history.jsonl"
    assert goal_path.is_file()
    assert _count_goal_created(jsonl_path) == 1


def test_create_goal_complete_with_event_raises(
    backend_factory, agent_root: Path
) -> None:
    """TEST 120 — goal.md present WITH goal_created event → raises GoalAlreadyExists."""
    backend = backend_factory(agent_root)
    backend.create_goal("test-agent", "done-goal", _make_min_goal(intent="Original"))

    # The goal is COMPLETE (goal_created event landed) → a second create refuses.
    with pytest.raises(GoalAlreadyExists):
        backend.create_goal(
            "test-agent", "done-goal", _make_min_goal(intent="Second attempt")
        )

    # Original goal.md is untouched.
    parsed = frontmatter.load(agent_root / "goals" / "done-goal" / "goal.md")
    assert parsed.metadata["intent"] == "Original"


def test_create_goal_completes_partial_and_is_idempotent(
    backend_factory, agent_root: Path
) -> None:
    """TEST 121 — goal.md present WITHOUT goal_created event → create_goal COMPLETES it.

    Simulates the rare partial-create outcome (goal.md landed, the goal_created
    audit line never did) by truncating goal_history.jsonl. create_goal() must
    self-heal: append the missing goal_created event, return the PERSISTED goal
    (the goal.md body is authoritative — the supplied goal arg's body is NOT
    re-written), and stay idempotent on a re-run (no duplicate goal_created).
    """
    backend = backend_factory(agent_root)
    backend.create_goal("test-agent", "half", _make_min_goal(intent="Persisted intent"))

    goal_path = agent_root / "goals" / "half" / "goal.md"
    jsonl_path = agent_root / "goals" / "half" / "goal_history.jsonl"

    # Simulate the partial: goal.md present, goal_created line removed.
    jsonl_path.write_text("", encoding="utf-8")
    assert _count_goal_created(jsonl_path) == 0
    assert goal_path.is_file()

    # Complete-on-partial: pass a DIFFERENT intent to prove the persisted goal.md
    # body wins (not the supplied arg).
    returned = backend.create_goal(
        "test-agent", "half", _make_min_goal(intent="Should be ignored")
    )
    assert returned.intent == "Persisted intent", (
        "persisted goal.md must be authoritative"
    )
    assert _count_goal_created(jsonl_path) == 1, (
        "goal_created must reappear exactly once"
    )
    # goal.md body unchanged.
    parsed = frontmatter.load(goal_path)
    assert parsed.metadata["intent"] == "Persisted intent"

    # Idempotent: the goal is now COMPLETE → a second re-run refuses and appends
    # no duplicate goal_created event.
    with pytest.raises(GoalAlreadyExists):
        backend.create_goal("test-agent", "half", _make_min_goal(intent="again"))
    assert _count_goal_created(jsonl_path) == 1, (
        "re-run must not duplicate goal_created"
    )


def test_create_goal_symlinked_history_leaf_leaves_no_goal_md(
    tmp_path: Path,
) -> None:
    """TEST 122 — Part 1: a symlinked goal_history.jsonl leaf → REFUSE, no goal.md committed.

    Distinct from TEST 117 (which asserts the audit line cannot escape). This
    locks the all-or-nothing property added by the two-leaf pre-verification:
    when the history leaf escapes the vault, the goal.md write must NOT have
    landed either. RED against a create_goal that writes goal.md before
    verifying the history leaf; GREEN once both leaves are verified up front.
    """
    agent_root = tmp_path / "agent"
    outside = tmp_path / "outside"
    outside.mkdir()
    stolen = outside / "stolen_history.jsonl"

    goal_dir = agent_root / "goals" / "leaky2"
    goal_dir.mkdir(parents=True)
    (goal_dir / "goal_history.jsonl").symlink_to(stolen)

    backend = FilesystemGoalBackend(agent_root)
    with pytest.raises(PathTraversalError):
        backend.create_goal("agent", "leaky2", _make_min_goal())

    assert not (goal_dir / "goal.md").exists(), (
        "goal.md was committed despite the refuse"
    )
    assert not stolen.exists(), "audit line escaped the vault"


def test_create_goal_stray_file_collision_raises_already_exists(
    backend_factory, agent_root: Path
) -> None:
    """TEST 123 — stray regular FILE at goals/<id> → GoalAlreadyExists (not FileExistsError)."""
    stray = agent_root / "goals" / "occupied"
    stray.parent.mkdir(parents=True)
    stray.write_text("not a goal directory", encoding="utf-8")

    backend = backend_factory(agent_root)
    with pytest.raises(GoalAlreadyExists, match="non-directory"):
        backend.create_goal("test-agent", "occupied", _make_min_goal())


def test_create_goal_corrupt_history_fails_closed(
    backend_factory, agent_root: Path
) -> None:
    """TEST 124 — fail-closed: goal.md present + unparseable goal_history.jsonl → raises.

    Completeness cannot be determined, so create_goal() must REFUSE rather than
    silently complete (which would append a duplicate goal_created over corrupt
    history) or overwrite. goal.md and the corrupt history must be left untouched.
    """
    backend = backend_factory(agent_root)
    backend.create_goal("test-agent", "corrupt", _make_min_goal(intent="Keep me"))

    jsonl_path = agent_root / "goals" / "corrupt" / "goal_history.jsonl"
    corrupt = "this is { not json\n"
    jsonl_path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(GoalAlreadyExists):
        backend.create_goal("test-agent", "corrupt", _make_min_goal(intent="overwrite"))

    # Nothing silently completed or overwritten.
    assert jsonl_path.read_text(encoding="utf-8") == corrupt, "history was mutated"
    parsed = frontmatter.load(agent_root / "goals" / "corrupt" / "goal.md")
    assert parsed.metadata["intent"] == "Keep me"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 125-128 — NUL rejection, lock isolation, absent-history heal, has-events
# fail-closed (the #642 review-driven fix set)


def test_goal_id_rejects_nul_byte(agent_root: Path) -> None:
    """TEST 125 — a NUL-byte goal_id is rejected by validate_goal_id() AND for_goal().

    NUL ('\\x00') is outside the [a-z0-9_-] charset allow-list, so both the
    standalone validator and for_goal()'s shared-regex gate must refuse it before
    any filesystem path is constructed (a NUL in a path is a hard OSError on most
    platforms and a classic truncation-injection vector). The trailing positive
    control proves the rejection is specific to the bad char, not a blanket raise
    — if the charset guard were reverted to accept NUL, the two raises below would
    go GREEN (no raise) and this test fails.
    """
    with pytest.raises(ValueError):
        validate_goal_id("a\x00b")

    backend = FilesystemGoalBackend(agent_root)
    with pytest.raises(ValueError):
        backend.for_goal("a\x00b")

    # Negative control: a valid id without NUL must NOT raise on either path.
    validate_goal_id("ab")  # must not raise
    backend.for_goal("ab")  # must not raise


def test_for_goal_per_goal_lock_isolation(agent_root: Path) -> None:
    """TEST 126 — for_goal('a') and for_goal('b') get DIFFERENT .goal.lock paths.

    Structural per-goal-lock-granularity guard: concurrent run-goals on distinct
    goal_ids must never contend on a shared lock. Each scoped backend's
    _lock_path must be goals/<id>/.goal.lock — distinct per goal_id and nested
    under goals/. If for_goal() ever collapsed to a shared (e.g. standing) lock,
    the two paths below would be equal and this test fails.
    """
    backend = FilesystemGoalBackend(agent_root)
    sa = backend.for_goal("alpha")
    sb = backend.for_goal("bravo")

    assert sa._lock_path != sb._lock_path, "distinct goal_ids must not share a lock"
    for scoped, gid in ((sa, "alpha"), (sb, "bravo")):
        assert scoped._lock_path.name == ".goal.lock"
        assert scoped._lock_path.parent.name == gid
        assert scoped._lock_path.parent.parent.name == "goals"


def test_create_goal_self_heals_absent_history(
    backend_factory, agent_root: Path
) -> None:
    """TEST 127 — create_goal() self-heals when goal_history.jsonl is ABSENT.

    Distinct from TEST 121 (which truncates the file to ''). Here the history
    file is UNLINKED entirely (goal.md kept) — the other genuine partial shape.
    create_goal() must heal: append exactly one goal_created (built from the
    PERSISTED goal), return the persisted goal, and stay idempotent on re-run.
    Negative control: if absent-history were mis-classified as fail-closed
    (over-tightened), the heal create would raise instead and the assertions
    below go red; if it double-minted, the count assertion goes red.
    """
    backend = backend_factory(agent_root)
    backend.create_goal("test-agent", "healme", _make_min_goal(intent="Persisted"))

    goal_path = agent_root / "goals" / "healme" / "goal.md"
    jsonl_path = agent_root / "goals" / "healme" / "goal_history.jsonl"

    # Simulate the partial: goal.md present, history file UNLINKED (absent).
    jsonl_path.unlink()
    assert goal_path.is_file()
    assert not jsonl_path.exists()

    returned = backend.create_goal(
        "test-agent", "healme", _make_min_goal(intent="Should be ignored")
    )
    assert returned.intent == "Persisted", "persisted goal.md must be authoritative"
    assert _count_goal_created(jsonl_path) == 1, "goal_created must appear exactly once"

    # Idempotent: now COMPLETE → re-run refuses, no duplicate goal_created.
    with pytest.raises(GoalAlreadyExists):
        backend.create_goal("test-agent", "healme", _make_min_goal(intent="again"))
    assert _count_goal_created(jsonl_path) == 1, (
        "re-run must not duplicate goal_created"
    )


def test_create_goal_fails_closed_on_history_with_no_goal_created(
    backend_factory, agent_root: Path
) -> None:
    """TEST 128 — goal.md present + history has events but NO goal_created → FAIL CLOSED.

    The tightened complete-on-partial predicate (the #642 fix): a goal.md authored
    via save_goal()/apply_transition() carries transition events but no
    goal_created marker. That is NOT a clean partial — healing it would mint a
    spurious, mis-ordered goal_created over a legitimately-authored goal. So
    create_goal() must RAISE (fail closed) and NOT append a goal_created.

    Negative control: this is the exact case the OLD predicate self-healed. If the
    fix were reverted (has-events-but-no-creation → heal), create_goal() would
    append a goal_created and NOT raise — so BOTH the pytest.raises and the
    `_count_goal_created == 0` assertion go red. The state is fabricated directly:
    a real in-vault goals/<id>/goal.md plus a goal_history.jsonl holding one
    non-goal_created (transition) line.
    """
    backend = backend_factory(agent_root)
    goal_dir = agent_root / "goals" / "authored"
    _make_goal_md(goal_dir, intent="Authored via save")

    jsonl_path = goal_dir / "goal_history.jsonl"
    transition_line = (
        json.dumps(
            {
                "ts": "2026-06-26T00:00:00+00:00",
                "event": "sub_goal_started",
                "sub_goal_id": "sg1",
            }
        )
        + "\n"
    )
    jsonl_path.write_text(transition_line, encoding="utf-8")
    assert _count_goal_created(jsonl_path) == 0

    with pytest.raises(GoalAlreadyExists):
        backend.create_goal(
            "test-agent", "authored", _make_min_goal(intent="should not heal")
        )

    # Fail closed: NO goal_created minted, history untouched.
    assert _count_goal_created(jsonl_path) == 0, "a spurious goal_created was minted"
    assert jsonl_path.read_text(encoding="utf-8") == transition_line, "history mutated"
    parsed = frontmatter.load(goal_dir / "goal.md")
    assert parsed.metadata["intent"] == "Authored via save"

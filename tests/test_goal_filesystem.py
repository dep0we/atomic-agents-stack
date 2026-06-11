"""Filesystem-specific tests for FilesystemGoalBackend (spec/41).

~10 tests covering behaviors unique to the filesystem reference implementation:
  - Concurrent apply_transition under fcntl.flock (single-writer guarantee)
  - BOM stripping in export() CRLF normalization
  - archive_goal() retry when goal.md already unlinked (idempotency path)
  - _make_history_event() ts-first key order with extra kwargs
  - goal_text() returns '' for nonexistent agent_root (no mkdir side-effect)
  - read_schema_version() returns None for completely absent vault dir
  - export_all() returns list of GoalExport (one per agent with goal)
  - append_history_event() creates goal_history.jsonl if absent
  - FilesystemGoalBackend path-traversal rejection (relative path)
  - backend_id property is 'filesystem' (stable identity)
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
from pathlib import Path

import pytest

from atomic_agents.goal.filesystem import FilesystemGoalBackend
from atomic_agents._goal_impl import CURRENT_GOAL_SCHEMA_VERSION


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_goal_md(agent_root: Path, *, intent: str = "FS Test goal") -> None:
    agent_root.mkdir(parents=True, exist_ok=True)
    content = f"""---
schema_version: {CURRENT_GOAL_SCHEMA_VERSION}
active: true
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

Filesystem test body.

## History (auto-appended)
- 2026-06-11 — goal created
"""
    (agent_root / "goal.md").write_text(content, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────


def test_concurrent_apply_transition_serialized(tmp_path: Path) -> None:
    """Multiple threads calling apply_transition() must not produce split writes.

    The flock on .goal.lock must serialize competing callers so that goal.md
    is never read from a half-written intermediate state.
    """
    agent_root = tmp_path / "agent"
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)

    errors: list[Exception] = []
    slugs: list[str] = []
    lock = threading.Lock()

    def _transition(sub_goal_id: str, new_status: str) -> None:
        from datetime import datetime

        ts = datetime.now().astimezone().isoformat()
        try:
            backend.apply_transition(
                agent_id="agent",
                sub_goal_id=sub_goal_id,
                to_status=new_status,
                fields={},
                history_prose=f"{sub_goal_id} → {new_status}",
                history_event={
                    "ts": ts,
                    "event": "concurrent_test",
                    "sub_goal_id": sub_goal_id,
                },
            )
            with lock:
                slugs.append(f"{sub_goal_id}:{new_status}")
        except Exception as exc:
            with lock:
                errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_transition, "sg1", "in_progress"),
            executor.submit(_transition, "sg2", "in_progress"),
        ]
        concurrent.futures.wait(futures)

    assert not errors, f"Concurrent transitions raised: {errors}"

    # Both transitions must have committed
    assert len(slugs) == 2

    # goal.md must be valid (not corrupted by a half-write)
    goal = backend.load_goal("agent")
    statuses = {sg.id: sg.status for sg in goal.sub_goals}
    assert statuses["sg1"] == "in_progress"
    assert statuses["sg2"] == "in_progress"

    # goal_history.jsonl must have exactly 2 lines
    history_path = agent_root / "goal_history.jsonl"
    lines = [ln for ln in history_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2


def test_apply_transition_jsonl_failure_leaves_no_orphan_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec/41 MUST 6 fault-injection: a crash AFTER goal.md is written but
    BEFORE the JSONL append must NOT leave goal_history.jsonl written while
    goal.md is un-updated.

    The reference impl writes goal.md FIRST, then appends the JSONL line, both
    under one flock. We inject a failure in _append_jsonl (the second write) and
    assert the conformance-violating state ("JSONL written, goal.md un-updated")
    never occurs: the JSONL line is absent (the audit line for a NOT-yet-
    committed state was never written), and the lock is released so a retry can
    proceed. This pins the atomicity MUST against its failure mode, not just the
    happy path.
    """
    agent_root = tmp_path / "agent"
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)

    from datetime import datetime

    def _boom(_event: dict) -> None:
        raise OSError("simulated crash after goal.md write, before JSONL append")

    monkeypatch.setattr(backend, "_append_jsonl", _boom)

    ts = datetime.now().astimezone().isoformat()
    with pytest.raises(OSError, match="simulated crash"):
        backend.apply_transition(
            agent_id="agent",
            sub_goal_id="sg1",
            to_status="in_progress",
            fields={},
            history_prose="sg1 → in_progress",
            history_event={"ts": ts, "event": "fault_injected", "sub_goal_id": "sg1"},
        )

    # Positive ordering assertion (the actual MUST-6 invariant, not a proxy):
    # the failure was injected in the SECOND write (_append_jsonl), so goal.md
    # (the FIRST write) MUST already reflect the new status. This pins the
    # write ORDER — a refactor to JSONL-first/goal.md-second would leave goal.md
    # un-updated and fail HERE, even though the no-orphan-line check below would
    # still pass. Without this assertion the test is order-blind.
    goal_after_fault = FilesystemGoalBackend(agent_root).load_goal("agent")
    sg1_after = next(s for s in goal_after_fault.sub_goals if s.id == "sg1")
    assert sg1_after.status == "in_progress", (
        "goal.md must be written BEFORE the JSONL append (MUST 6 ordering); "
        "a crash in _append_jsonl must still leave goal.md updated"
    )

    # The conformance-violating state is "JSONL line written but goal.md
    # un-updated". With goal.md-first ordering, the failure point is the JSONL
    # append, so no orphaned JSONL line for this (un-committed) event exists.
    history_path = agent_root / "goal_history.jsonl"
    if history_path.is_file():
        lines = [ln for ln in history_path.read_text().splitlines() if ln.strip()]
        assert not any(
            json.loads(ln).get("event") == "fault_injected" for ln in lines
        ), "MUST 6 violation: JSONL audit line written for an un-committed transition"

    # The lock must be released — a fresh, un-patched transition must succeed,
    # proving apply_transition() did not leak the flock on the exception path.
    monkeypatch.undo()
    backend2 = FilesystemGoalBackend(agent_root)
    ts2 = datetime.now().astimezone().isoformat()
    backend2.apply_transition(
        agent_id="agent",
        sub_goal_id="sg1",
        to_status="in_progress",
        fields={},
        history_prose="sg1 → in_progress (retry)",
        history_event={"ts": ts2, "event": "retry_after_fault", "sub_goal_id": "sg1"},
    )
    goal = backend2.load_goal("agent")
    sg1 = next(s for s in goal.sub_goals if s.id == "sg1")
    assert sg1.status == "in_progress"


def test_export_bom_stripping(tmp_path: Path) -> None:
    """export() MUST strip UTF-8 BOM from exported bytes (spec/40 MUST 5)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)

    # Write goal.md with BOM preamble
    bom = b"\xef\xbb\xbf"
    content = b"---\nschema_version: 1\nactive: true\nintent: BOM test\npriority: high\ncreated: 2026-06-11\nlast_progress_check: 2026-06-11\nsuccess_criteria:\n  - done\nsub_goals: []\n---\n\nbody\n"
    (agent_root / "goal.md").write_bytes(bom + content)

    backend = FilesystemGoalBackend(agent_root)
    result = backend.export()
    assert not result.goal_md_bytes.startswith(bom), (
        "BOM must be stripped from export bytes"
    )
    assert b"BOM test" in result.goal_md_bytes


def test_archive_idempotency_retry_path(tmp_path: Path) -> None:
    """archive_goal() retry when goal.md is absent but archive exists returns existing slug."""
    agent_root = tmp_path / "agent"
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)

    # First archive
    slug1 = backend.archive_goal("agent", reason="done")
    assert not (agent_root / "goal.md").exists()

    # Retry — goal.md gone, archive exists → must return existing slug
    slug2 = backend.archive_goal("agent", reason="retry")
    assert slug2 == slug1, "retry must return the existing archive slug"

    # Confirm only one archive file exists
    archive_dir = agent_root / "goal_archive"
    archives = list(archive_dir.glob("*.md"))
    assert len(archives) == 1


def test_make_history_event_ts_first_with_extra_kwargs(tmp_path: Path) -> None:
    """_make_history_event() must produce ts-first key ordering with extra kwargs."""
    agent_root = tmp_path / "agent"
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)
    from datetime import datetime

    ts = datetime.now().astimezone().isoformat()
    event = backend._make_history_event(
        ts, "test_event", sub_goal_id="sg1", extra="value"
    )
    keys = list(event.keys())
    assert keys[0] == "ts", f"First key must be 'ts'; got {keys[0]!r}"
    assert keys[1] == "event", f"Second key must be 'event'; got {keys[1]!r}"
    assert event["ts"] == ts
    assert event["event"] == "test_event"
    assert event["sub_goal_id"] == "sg1"
    assert event["extra"] == "value"


def test_goal_text_empty_for_nonexistent_root(tmp_path: Path) -> None:
    """goal_text() returns '' and does NOT create agent_root when absent."""
    nonexistent = tmp_path / "does_not_exist"
    backend = FilesystemGoalBackend(nonexistent)
    result = backend.goal_text("agent")
    assert result == ""
    assert not nonexistent.exists(), "goal_text must not create directories"


def test_read_schema_version_none_for_absent_vault(tmp_path: Path) -> None:
    """read_schema_version() returns None when the entire agent root is absent."""
    backend = FilesystemGoalBackend(tmp_path / "no_agent")
    result = backend.read_schema_version("agent")
    assert result is None


def test_export_is_scoped_to_its_own_agent_root(tmp_path: Path) -> None:
    """Each backend's export()/export_all() is scoped to its OWN agent_root.

    A FilesystemGoalBackend is constructed per agent root; export() returns a
    single GoalExport for that root only (no multi-agent fan-out). Two backends
    over two roots return disjoint exports, and export_all() equals export(None)
    for the same root.
    """
    root_a = tmp_path / "agent_a"
    root_b = tmp_path / "agent_b"
    root_c = tmp_path / "agent_c"
    _make_goal_md(root_a, intent="Agent A goal")
    _make_goal_md(root_b, intent="Agent B goal")
    # agent_c has no goal.md — its export carries empty goal_md_bytes.
    root_c.mkdir()

    backend_a = FilesystemGoalBackend(root_a)
    backend_b = FilesystemGoalBackend(root_b)
    backend_c = FilesystemGoalBackend(root_c)

    result_a = backend_a.export()
    result_b = backend_b.export()

    # Each export is scoped to its own root — no cross-agent bleed.
    assert b"Agent A goal" in result_a.goal_md_bytes
    assert b"Agent A goal" not in result_b.goal_md_bytes
    assert b"Agent B goal" in result_b.goal_md_bytes
    assert b"Agent B goal" not in result_a.goal_md_bytes
    assert backend_c.export().goal_md_bytes == b""

    # export_all() is the unbounded alias of export(None) for the same root.
    assert backend_a.export_all().goal_md_bytes == result_a.goal_md_bytes
    assert backend_a.export_all().scope == result_a.scope


def test_append_history_event_creates_jsonl_if_absent(tmp_path: Path) -> None:
    """append_history_event() MUST create goal_history.jsonl if it doesn't exist."""
    agent_root = tmp_path / "agent"
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)

    history_path = agent_root / "goal_history.jsonl"
    assert not history_path.exists(), "precondition: no JSONL file yet"

    from datetime import datetime

    ts = datetime.now().astimezone().isoformat()
    backend.append_history_event("agent", {"ts": ts, "event": "first_event"})

    assert history_path.is_file(), "goal_history.jsonl must be created"
    lines = [ln for ln in history_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "first_event"
    assert event["ts"] == ts


def test_dotdot_path_rejected() -> None:
    """FilesystemGoalBackend must reject paths that contain '..' components."""
    with pytest.raises(ValueError, match=r"\.\."):
        FilesystemGoalBackend(Path("/agents") / ".." / "escape")


def test_backend_id_is_stable(tmp_path: Path) -> None:
    """backend_id must return 'filesystem' consistently across two instances."""
    b1 = FilesystemGoalBackend(tmp_path / "a1")
    b2 = FilesystemGoalBackend(tmp_path / "a2")
    assert b1.backend_id == "filesystem"
    assert b2.backend_id == "filesystem"
    assert b1.backend_id == b2.backend_id


def test_apply_transition_non_json_serializable_event_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply_transition() with a non-JSON-serializable history_event MUST raise
    TypeError (from json.dumps) and leave BOTH goal.md AND goal_history.jsonl
    UNCHANGED — the serialization probe fires BEFORE the durable write.

    filesystem.py:368 calls json.dumps(structured_event) BEFORE _write_goal(),
    so a non-serializable value (e.g. an object() instance) fails closed:
    nothing is written and no orphan JSONL line is created for the un-committed
    transition. Without this probe, json.dumps would only fail inside
    _append_jsonl() AFTER goal.md was already committed — a silent partial commit.
    """
    agent_root = tmp_path / "agent"
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)

    goal_md = agent_root / "goal.md"
    history_path = agent_root / "goal_history.jsonl"
    before_bytes = goal_md.read_bytes()
    assert not history_path.exists(), "precondition: no JSONL before the call"

    from datetime import datetime

    ts = datetime.now().astimezone().isoformat()
    # object() is not JSON-serializable → json.dumps raises TypeError
    with pytest.raises(TypeError):
        backend.apply_transition(
            agent_id="agent",
            sub_goal_id="sg1",
            to_status="in_progress",
            fields={},
            history_prose="sg1 → in_progress",
            history_event={"ts": ts, "event": "bad_event", "bad": object()},
        )

    # goal.md MUST be unchanged — the serialization probe prevented the write.
    assert goal_md.read_bytes() == before_bytes, (
        "goal.md must be unchanged when json.dumps raises before _write_goal"
    )

    # No JSONL line must exist for the un-committed transition.
    assert not history_path.exists(), (
        "goal_history.jsonl must not be created when json.dumps raises before the write"
    )

    # The sub-goal status on disk must still be 'pending' (not 'in_progress').
    reloaded = FilesystemGoalBackend(agent_root).load_goal("agent")
    sg1 = next(s for s in reloaded.sub_goals if s.id == "sg1")
    assert sg1.status == "pending", (
        "sg1 status must not be persisted when the serialization probe fails"
    )


def test_relative_agent_root_resolves_to_absolute(tmp_path: Path) -> None:
    """FilesystemGoalBackend(Path('some/relative/path')) MUST construct without
    raising, and the resolved agent_root MUST be an absolute path.

    The filesystem reference impl accepts relative paths and resolves them via
    Path.resolve() (matching the sibling memory/persona/corpus filesystem
    backends' accept-and-resolve convention). Construction must not raise even
    though the path does not exist on disk — construction is side-effect-free
    (spec/41 MUST 1). The existing TEST 24 path-traversal guard still catches
    '..' components, so we do not re-test that here.
    """
    backend = FilesystemGoalBackend(Path("some/relative/path"))
    assert backend._agent_root.is_absolute(), (
        "resolved agent_root must be an absolute path even when constructed "
        "from a relative input"
    )


def test_archive_goal_bumps_last_progress_check_to_today(tmp_path: Path) -> None:
    """archive_goal() MUST set last_progress_check to the archive day.

    Parity with GoalManager.archive(), which writes last_progress_check=today in
    the archived frontmatter. The backend's archive_goal() previously preserved
    the goal's existing (stale) last_progress_check via build_goal_frontmatter,
    diverging from the on-disk shape the framework has produced since its first
    goal support. This pins the field to today so the reference impl's archive
    output matches GoalManager's (the arc's zero-behavior-change contract).
    """
    import frontmatter
    from datetime import date

    agent_root = tmp_path / "agent"
    # goal.md carries a deliberately STALE last_progress_check (not today).
    _make_goal_md(agent_root)
    backend = FilesystemGoalBackend(agent_root)
    # Confirm the precondition: the on-disk value is the stale fixture date.
    loaded = backend.load_goal("agent")
    assert loaded.last_progress_check == "2026-06-11"

    slug = backend.archive_goal("agent", reason="done")
    archive_path = agent_root / "goal_archive" / f"{slug}.md"
    archived = frontmatter.load(archive_path)
    assert archived.metadata.get("last_progress_check") == date.today().isoformat(), (
        "archive_goal() must bump last_progress_check to the archive day, "
        "matching GoalManager.archive()'s on-disk frontmatter shape"
    )

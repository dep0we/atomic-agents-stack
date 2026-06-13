"""Containment regression tests for FilesystemGoalBackend._require_within_root.

These tests prove that every I/O method that could write or read goal data
raises PathTraversalError when a path component (goal.md, goal_archive/,
goal_history.jsonl, .goal.lock) is replaced with a symlink pointing OUTSIDE
the agent_root vault.

macOS note: /tmp and /var are symlinks to /private/... on macOS. The guard
resolves BOTH sides so a normal tmp_path agent_root must NOT trigger the
guard. The positive-control tests below prove this.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from atomic_agents.goal.filesystem import FilesystemGoalBackend
from atomic_agents._goal_impl import CURRENT_GOAL_SCHEMA_VERSION
from atomic_agents.exceptions import PathTraversalError


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_goal_md(agent_root: Path, *, intent: str = "Containment test goal") -> None:
    """Write a minimal valid goal.md to agent_root."""
    agent_root.mkdir(parents=True, exist_ok=True)
    content = f"""\
---
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

Goal body text.

## History (auto-appended)
- 2026-06-11 — goal created
"""
    (agent_root / "goal.md").write_text(content, encoding="utf-8")


def _make_history_jsonl(agent_root: Path) -> None:
    """Write a minimal goal_history.jsonl to agent_root."""
    (agent_root / "goal_history.jsonl").write_text(
        '{"ts": "2026-06-11T00:00:00+00:00", "event": "test"}\n', encoding="utf-8"
    )


def _make_archive_dir(agent_root: Path) -> None:
    """Create a goal_archive/ directory with one dummy archive file."""
    archive_dir = agent_root / "goal_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    # A minimal archive file
    (archive_dir / "2026-06-11_containment_test_goal.md").write_text(
        f"""\
---
schema_version: {CURRENT_GOAL_SCHEMA_VERSION}
active: false
intent: Containment test goal
priority: high
created: 2026-06-11
last_progress_check: 2026-06-11
success_criteria:
  - Criterion one
sub_goals: []
archived_at: 2026-06-11
archive_reason: completed
---

## Overview

Archived.
""",
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Positive control — NORMAL (non-symlinked) agent_root must NEVER raise
#
# This is the most critical test: prove that the guard does not break anything
# for a legitimate tmp_path agent_root (which on macOS resolves through
# /private/tmp, but BOTH sides are resolved so the check still passes).


def test_positive_control_normal_agent_root_no_raises(tmp_path: Path) -> None:
    """All read/write methods work normally when no symlinks escape the vault.

    This is the macOS positive-control test: tmp_path is typically
    /private/var/folders/.../pytest-xxx/... on macOS (fully resolved), so
    the guard must NOT spuriously raise.
    """
    agent_root = tmp_path / "vault" / "my-agent"
    _make_goal_md(agent_root)
    _make_history_jsonl(agent_root)
    _make_archive_dir(agent_root)

    backend = FilesystemGoalBackend(agent_root)

    # load_goal() — read path
    goal = backend.load_goal("my-agent")
    assert goal.intent == "Containment test goal"

    # goal_text() — read path
    text = backend.goal_text("my-agent")
    assert "Containment test goal" in text

    # read_schema_version() — read path
    ver = backend.read_schema_version("my-agent")
    assert ver == CURRENT_GOAL_SCHEMA_VERSION

    # save_goal() — write path
    backend.save_goal("my-agent", goal)

    # append_history_event() — append path
    backend.append_history_event(
        "my-agent", {"ts": "2026-06-11T00:00:00+00:00", "event": "test"}
    )

    # list_archived() — list path
    slugs = backend.list_archived("my-agent")
    assert isinstance(slugs, list)

    # export() — multi-path read
    exported = backend.export()
    assert exported.backend_id == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# Symlinked goal.md → outside file


class TestSymlinkedGoalMd:
    """goal.md replaced with a symlink pointing outside agent_root."""

    @pytest.fixture
    def outside_file(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside" / "evil.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("evil content", encoding="utf-8")
        return outside

    @pytest.fixture
    def backend_symlinked_goal(self, tmp_path: Path, outside_file: Path):
        """Agent root with goal.md replaced by a symlink to an outside file."""
        agent_root = tmp_path / "vault" / "agent"
        _make_goal_md(agent_root)  # creates a real goal.md first
        # Replace goal.md with a symlink to outside
        (agent_root / "goal.md").unlink()
        (agent_root / "goal.md").symlink_to(outside_file)
        return FilesystemGoalBackend(agent_root)

    def test_load_goal_raises(self, backend_symlinked_goal) -> None:
        """load_goal() must raise PathTraversalError when goal.md points outside."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_goal.load_goal("agent")

    def test_goal_text_raises(self, backend_symlinked_goal) -> None:
        """goal_text() must raise PathTraversalError when goal.md points outside."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_goal.goal_text("agent")

    def test_read_schema_version_raises(self, backend_symlinked_goal) -> None:
        """read_schema_version() must raise PathTraversalError when goal.md points outside."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_goal.read_schema_version("agent")

    def test_save_goal_raises(
        self, tmp_path: Path, outside_file: Path, backend_symlinked_goal
    ) -> None:
        """save_goal() must raise PathTraversalError — write must not escape vault."""
        # Build a valid goal object via a clean backend, then try to save through
        # the symlinked backend so we can't even reach the write.
        clean_root = tmp_path / "vault" / "clean"
        _make_goal_md(clean_root)
        clean_backend = FilesystemGoalBackend(clean_root)
        goal = clean_backend.load_goal("clean")

        with pytest.raises(PathTraversalError):
            backend_symlinked_goal.save_goal("agent", goal)

    def test_export_raises(self, backend_symlinked_goal) -> None:
        """export() must raise PathTraversalError when goal.md points outside."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_goal.export()


# ──────────────────────────────────────────────────────────────────────────────
# Symlinked goal_archive/ → outside directory


class TestSymlinkedGoalArchiveDir:
    """goal_archive/ replaced with a symlink pointing outside agent_root."""

    @pytest.fixture
    def outside_dir(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside" / "archive"
        outside.mkdir(parents=True, exist_ok=True)
        return outside

    @pytest.fixture
    def backend_symlinked_archive(self, tmp_path: Path, outside_dir: Path):
        """Agent root with goal_archive/ replaced by a symlink to an outside dir."""
        agent_root = tmp_path / "vault" / "agent"
        _make_goal_md(agent_root)
        # Create goal_archive/ as a real dir first (not required, but mirrors
        # real usage where the dir may already exist before symlink replacement).
        archive_dir = agent_root / "goal_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        # Replace goal_archive/ with a symlink to outside
        import shutil

        shutil.rmtree(str(archive_dir))
        archive_dir.symlink_to(outside_dir)
        return FilesystemGoalBackend(agent_root)

    def test_archive_goal_raises(self, backend_symlinked_archive) -> None:
        """archive_goal() must raise PathTraversalError when goal_archive/ points outside."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_archive.archive_goal("agent", reason="completed")

    def test_list_archived_raises(self, backend_symlinked_archive) -> None:
        """list_archived() must raise PathTraversalError when goal_archive/ points outside.

        Note: list_archived() calls _require_within_root(archive_dir) up front,
        then checks .exists(). The escape is caught before any file I/O.
        """
        with pytest.raises(PathTraversalError):
            backend_symlinked_archive.list_archived("agent")

    def test_export_raises(self, backend_symlinked_archive) -> None:
        """export() must raise PathTraversalError when goal_archive/ points outside."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_archive.export()


# ──────────────────────────────────────────────────────────────────────────────
# Symlinked goal_history.jsonl → outside file


class TestSymlinkedGoalHistoryJsonl:
    """goal_history.jsonl replaced with a symlink pointing outside agent_root."""

    @pytest.fixture
    def outside_jsonl(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside" / "history.jsonl"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text(
            '{"ts": "2026-06-11T00:00:00+00:00", "event": "evil"}\n',
            encoding="utf-8",
        )
        return outside

    @pytest.fixture
    def backend_symlinked_history(self, tmp_path: Path, outside_jsonl: Path):
        """Agent root with goal_history.jsonl replaced by a symlink to an outside file."""
        agent_root = tmp_path / "vault" / "agent"
        _make_goal_md(agent_root)
        # Write a real history file first, then replace with symlink
        _make_history_jsonl(agent_root)
        (agent_root / "goal_history.jsonl").unlink()
        (agent_root / "goal_history.jsonl").symlink_to(outside_jsonl)
        return FilesystemGoalBackend(agent_root)

    def test_append_history_event_raises(self, backend_symlinked_history) -> None:
        """append_history_event() must raise PathTraversalError when jsonl points outside."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_history.append_history_event(
                "agent", {"ts": "2026-06-11T00:00:00+00:00", "event": "escape"}
            )


# ──────────────────────────────────────────────────────────────────────────────
# Symlinked .goal.lock → outside file
#
# The .goal.lock path is checked in _goal_lock(), which is called by
# apply_transition() and archive_goal(). If the lock escapes the vault,
# the flock would be held on an outside file — a perimeter escape for
# the advisory-lock mechanism.


class TestSymlinkedGoalLock:
    """goal.lock replaced with a symlink pointing outside agent_root."""

    @pytest.fixture
    def outside_lock(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside" / "other.lock"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("", encoding="utf-8")
        return outside

    @pytest.fixture
    def backend_symlinked_lock(self, tmp_path: Path, outside_lock: Path):
        """Agent root with .goal.lock replaced by a symlink to an outside file.

        We plant the symlink before the backend is used (it is side-effect-free
        in __init__, so no lock file is created during construction).
        """
        agent_root = tmp_path / "vault" / "agent"
        _make_goal_md(agent_root)
        # Plant a .goal.lock symlink pointing outside before any operation.
        agent_root.mkdir(parents=True, exist_ok=True)
        lock_path = agent_root / ".goal.lock"
        lock_path.symlink_to(outside_lock)
        return FilesystemGoalBackend(agent_root)

    def test_append_history_event_raises_on_symlinked_lock(
        self, backend_symlinked_lock
    ) -> None:
        """append_history_event() acquires _goal_lock() which checks .goal.lock.

        The guard fires before flock so no file descriptor is ever opened on
        the outside target.
        """
        with pytest.raises(PathTraversalError):
            backend_symlinked_lock.append_history_event(
                "agent", {"ts": "2026-06-11T00:00:00+00:00", "event": "escape-via-lock"}
            )

    def test_archive_goal_raises_on_symlinked_lock(
        self, backend_symlinked_lock
    ) -> None:
        """archive_goal() acquires _goal_lock() which checks .goal.lock."""
        with pytest.raises(PathTraversalError):
            backend_symlinked_lock.archive_goal("agent", reason="completed")

"""Tests for atomic_agents.dashboard.memory — aggregation layer."""

from __future__ import annotations
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.dashboard.memory import (
    aggregate_memory,
    _read_frontmatter,
    _dir_size,
    _simple_frontmatter_parse,
)


def _write_note(memory_dir: Path, filename: str, note_type: str, last_seen: str | None = None,
                pinned: bool = False) -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\ntype: {note_type}\n"
    if last_seen:
        content += f"last_seen: {last_seen}\n"
    if pinned:
        content += "pinned: true\n"
    content += "---\n# Note\nBody text."
    path = memory_dir / filename
    path.write_text(content)
    return path


def test_note_counts_by_type(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    _write_note(mem_dir, "user_pref.md", "user")
    _write_note(mem_dir, "feedback_1.md", "feedback")
    _write_note(mem_dir, "feedback_2.md", "feedback")
    _write_note(mem_dir, "project_x.md", "project")
    _write_note(mem_dir, "decision_arch.md", "decision")
    _write_note(mem_dir, "ref_docs.md", "reference")
    _write_note(mem_dir, "mystery.md", "custom_type")

    data = aggregate_memory(tmp_path)
    assert len(data.note_counts) == 1
    c = data.note_counts[0]
    assert c.agent == "alice"
    assert c.user == 1
    assert c.feedback == 2
    assert c.project == 1
    assert c.decision == 1
    assert c.reference == 1
    assert c.other == 1
    assert c.total == 7


def test_staleness_candidates(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    today = date.today()
    old_date = (today - timedelta(days=100)).isoformat()
    recent_date = today.isoformat()

    _write_note(mem_dir, "old_note.md", "user", last_seen=old_date)
    _write_note(mem_dir, "recent_note.md", "user", last_seen=recent_date)
    _write_note(mem_dir, "pinned_note.md", "user", last_seen=old_date, pinned=True)

    data = aggregate_memory(tmp_path, staleness_threshold_days=90)
    stale_names = [s.note for s in data.staleness_candidates]
    assert "old_note.md" in stale_names
    assert "recent_note.md" not in stale_names
    assert "pinned_note.md" not in stale_names  # pinned — excluded


def test_staleness_threshold_respected(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    today = date.today()
    # Note is 50 days old
    fifty_days_ago = (today - timedelta(days=50)).isoformat()
    _write_note(mem_dir, "note_50d.md", "user", last_seen=fifty_days_ago)

    # With threshold=90, not stale
    data = aggregate_memory(tmp_path, staleness_threshold_days=90)
    assert len(data.staleness_candidates) == 0

    # With threshold=30, IS stale
    data = aggregate_memory(tmp_path, staleness_threshold_days=30)
    assert len(data.staleness_candidates) == 1


def test_orphan_check(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    mem_dir.mkdir(parents=True)
    # Two notes; INDEX.md only references one
    _write_note(mem_dir, "note_a.md", "user")
    _write_note(mem_dir, "note_b.md", "user")
    (mem_dir / "INDEX.md").write_text("# Index\n- note_a\n")

    data = aggregate_memory(tmp_path)
    orphan_names = [o.note for o in data.orphan_notes]
    assert "note_b.md" in orphan_names
    assert "note_a.md" not in orphan_names


def test_no_orphans_when_all_indexed(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    mem_dir.mkdir(parents=True)
    _write_note(mem_dir, "note_a.md", "user")
    (mem_dir / "INDEX.md").write_text("# Index\n- note_a\n")

    data = aggregate_memory(tmp_path)
    assert data.orphan_notes == []


def test_version_churn_ranking(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    mem_dir.mkdir(parents=True)

    # note_a has 5 snapshots, note_b has 2
    for stem, count in [("note_a", 5), ("note_b", 2)]:
        ver_dir = mem_dir / ".versions" / stem
        ver_dir.mkdir(parents=True)
        for i in range(count):
            ts = f"2026050{i+1}T120000Z"
            (ver_dir / f"{ts}_abc{i:02d}abc.md").write_text(f"version {i}")

    data = aggregate_memory(tmp_path)
    assert len(data.version_churn) == 2
    # Sorted by snapshot_count desc
    assert data.version_churn[0].note == "note_a.md"
    assert data.version_churn[0].snapshot_count == 5
    assert data.version_churn[1].note == "note_b.md"
    assert data.version_churn[1].snapshot_count == 2


def test_dream_history_scanned(tmp_path):
    # alice needs log/ AND memory/ to be discovered and have memory data
    (tmp_path / "alice" / "log").mkdir(parents=True)
    (tmp_path / "alice" / "memory").mkdir(parents=True)
    # Write a memory note so note_counts > 0 (agent discovered)
    (tmp_path / "alice" / "memory" / "note.md").write_text("---\ntype: user\n---\nBody.")
    dream_dir = tmp_path / "alice" / "dreams" / "dream-001"
    dream_dir.mkdir(parents=True)
    manifest = {
        "dream_id": "dream-001",
        "status": "completed",
        "started_at": "2026-05-01T10:00:00Z",
        "consolidated": [{"new": "n.md", "supersedes": [], "reason": "r"}],
        "promoted": [{"new": "p.md", "from_journal_entries": [], "reason": "r"}],
        "marked_stale": [],
        "applied_at": "2026-05-01T10:30:00Z",
    }
    (dream_dir / "manifest.json").write_text(json.dumps(manifest))

    data = aggregate_memory(tmp_path)
    assert len(data.dream_history) == 1
    d = data.dream_history[0]
    assert d.agent == "alice"
    assert d.consolidations == 1
    assert d.promotions == 1
    assert d.applied is True


def test_memory_size_calculation(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    mem_dir.mkdir(parents=True)
    # Write a note of known size
    (mem_dir / "note.md").write_text("x" * 1000)

    data = aggregate_memory(tmp_path)
    ms = next((m for m in data.memory_sizes if m.agent == "alice"), None)
    assert ms is not None
    assert ms.live_bytes >= 1000


def test_simple_frontmatter_parse(tmp_path):
    md = tmp_path / "note.md"
    md.write_text("---\ntype: feedback\nlast_seen: 2026-03-01\n---\nBody.")
    result = _simple_frontmatter_parse(md)
    assert result.get("type") == "feedback"
    assert result.get("last_seen") == "2026-03-01"


def test_dir_size(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.txt").write_text("world!")
    size = _dir_size(tmp_path)
    assert size >= 11  # at least 5 + 6 bytes


def test_empty_root(tmp_path):
    data = aggregate_memory(tmp_path)
    assert data.note_counts == []
    assert data.staleness_candidates == []
    assert data.orphan_notes == []
    assert data.version_churn == []

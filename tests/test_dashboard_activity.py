"""Tests for atomic_agents.dashboard.activity — aggregation layer."""

from __future__ import annotations
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.dashboard.activity import (
    aggregate_activity,
    _scan_dreams,
    _scan_recent_captures,
)


def _write_log(agents_root: Path, agent: str, when: date, records: list[dict]) -> Path:
    log_dir = agents_root / agent / "log" / when.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{when.isoformat()}.jsonl"
    lines = []
    for rec in records:
        rec.setdefault("ts", datetime.combine(when, datetime.min.time()).isoformat())
        rec.setdefault("trigger", "cron")
        rec.setdefault("model", "claude-opus-4-7-20260101")
        rec.setdefault("input_tokens", 1000)
        rec.setdefault("output_tokens", 200)
        rec.setdefault("cost_usd", 0.05)
        rec.setdefault("status", "ok")
        rec.setdefault("summary", "test run")
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_headline_counts_runs_24h(tmp_path):
    now = datetime.now(tz=timezone.utc)
    today = now.date()
    # 3 runs "now" (within 24h), 1 run 8 days ago (outside 7d window)
    _write_log(tmp_path, "alice", today, [
        {"ts": now.isoformat(), "status": "ok"},
        {"ts": now.isoformat(), "status": "ok"},
        {"ts": now.isoformat(), "status": "error"},
    ])
    eight_days_ago = today - timedelta(days=8)
    _write_log(tmp_path, "alice", eight_days_ago, [
        {"ts": datetime.combine(eight_days_ago, datetime.min.time()).isoformat(), "status": "ok"},
    ])

    data = aggregate_activity(tmp_path, now=now)
    assert data.headline.runs_24h == 3
    assert data.headline.runs_7d == 3  # 8-day-old run is outside 7d window
    assert data.headline.failures_24h == 1
    assert data.headline.agents_active_24h == 1


def test_recent_runs_sorted_newest_first(tmp_path):
    now = datetime.now(tz=timezone.utc)
    today = now.date()
    earlier = now - timedelta(hours=2)
    _write_log(tmp_path, "alice", today, [
        {"ts": now.isoformat(), "summary": "newest"},
        {"ts": earlier.isoformat(), "summary": "older"},
    ])

    data = aggregate_activity(tmp_path, now=now)
    assert data.recent_runs[0].summary == "newest"
    assert data.recent_runs[1].summary == "older"


def test_recent_runs_capped_at_max(tmp_path):
    now = datetime.now(tz=timezone.utc)
    today = now.date()
    _write_log(tmp_path, "alice", today, [
        {"ts": now.isoformat()} for _ in range(60)
    ])

    data = aggregate_activity(tmp_path, now=now, max_recent=50)
    assert len(data.recent_runs) == 50


def test_failure_detection(tmp_path):
    now = datetime.now(tz=timezone.utc)
    today = now.date()
    _write_log(tmp_path, "alice", today, [
        {"ts": now.isoformat(), "status": "error", "summary": "failed run"},
        {"ts": now.isoformat(), "status": "ok", "summary": "good run"},
        {"ts": now.isoformat(), "trigger": "cron_error", "status": "ok", "summary": "trigger error"},
    ])

    data = aggregate_activity(tmp_path, now=now)
    assert len(data.recent_failures) == 2  # error status + _error trigger
    summaries = [r.summary for r in data.recent_failures]
    assert "failed run" in summaries


def test_tool_call_and_delegation_filter(tmp_path):
    now = datetime.now(tz=timezone.utc)
    today = now.date()
    _write_log(tmp_path, "alice", today, [
        {"ts": now.isoformat(), "trigger": "tool_call", "summary": "tool"},
        {"ts": now.isoformat(), "trigger": "delegate", "summary": "delegated"},
        {"ts": now.isoformat(), "trigger": "cron", "summary": "normal"},
    ])

    data = aggregate_activity(tmp_path, now=now)
    assert len(data.recent_tool_calls) == 1
    assert data.recent_tool_calls[0].trigger == "tool_call"
    assert len(data.recent_delegations) == 1
    assert data.recent_delegations[0].trigger == "delegate"


def test_stale_lock_detection(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    lock_path = tmp_path / "alice" / ".lock"
    lock_path.write_text("locked")
    # Make lock appear > 5 minutes old by manipulating mtime
    old_time = time.time() - 400  # 400 seconds = 6.7 minutes
    import os
    os.utime(lock_path, (old_time, old_time))

    now = datetime.now(tz=timezone.utc)
    data = aggregate_activity(tmp_path, now=now)
    assert len(data.lock_states) == 1
    assert data.lock_states[0].agent == "alice"
    assert data.lock_states[0].is_stale is True


def test_no_stale_lock_for_fresh_lock(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    lock_path = tmp_path / "alice" / ".lock"
    lock_path.write_text("locked")
    # Lock is brand new — not stale

    now = datetime.now(tz=timezone.utc)
    data = aggregate_activity(tmp_path, now=now)
    assert len(data.lock_states) == 0


def test_dream_scan(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    dream_dir = tmp_path / "alice" / "dreams" / "dream-001"
    dream_dir.mkdir(parents=True)
    manifest = {
        "dream_id": "dream-001",
        "agent_name": "alice",
        "status": "completed",
        "started_at": "2026-05-01T10:00:00Z",
        "consolidated": [{"new": "note1.md", "supersedes": [], "reason": "r"}],
        "promoted": [],
        "marked_stale": [],
        "applied_at": "2026-05-01T10:30:00Z",
    }
    (dream_dir / "manifest.json").write_text(json.dumps(manifest))

    dreams = _scan_dreams(tmp_path, ["alice"], limit=10)
    assert len(dreams) == 1
    assert dreams[0].dream_id == "dream-001"
    assert dreams[0].consolidations == 1
    assert dreams[0].applied is True


def test_recent_captures_sorted_by_mtime(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    mem_dir = tmp_path / "alice" / "memory"
    mem_dir.mkdir(parents=True)
    import os
    # Create two memory notes with different mtimes
    note1 = mem_dir / "note_old.md"
    note2 = mem_dir / "note_new.md"
    note1.write_text("---\ntype: user\n---\nold")
    note2.write_text("---\ntype: feedback\n---\nnew")
    old_time = time.time() - 1000
    os.utime(note1, (old_time, old_time))
    # note2 has current mtime (newer)

    captures = _scan_recent_captures(tmp_path, ["alice"], limit=10)
    assert len(captures) == 2
    # Newest first
    assert captures[0]["filename"] == "note_new.md"
    assert captures[1]["filename"] == "note_old.md"


def test_empty_agents_root(tmp_path):
    now = datetime.now(tz=timezone.utc)
    data = aggregate_activity(tmp_path, now=now)
    assert data.headline.runs_24h == 0
    assert data.headline.runs_7d == 0
    assert data.headline.agents_total == 0
    assert data.recent_runs == []
    assert data.recent_failures == []

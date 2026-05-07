"""Tests for atomic_agents.dashboard.render."""

from __future__ import annotations
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from atomic_agents.dashboard.costs import aggregate_agent, aggregate_global
from atomic_agents.dashboard.render import (
    render_all,
    render_agent,
    render_global,
)


def _write_log(agents_root: Path, agent: str, when: date, records: list[dict]) -> None:
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


def test_render_global_creates_html(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)

    assert out_path == tmp_path / "_dashboard" / "index.html"
    assert out_path.exists()

    html = out_path.read_text()
    assert "<!DOCTYPE html>" in html
    assert "Atomic Agents" in html
    assert "alice" in html
    assert "$0.10" in html or "$0.1" in html


def test_render_agent_creates_html(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    data = aggregate_agent(tmp_path, "alice", today=today)
    out_path = render_agent(tmp_path, data)

    assert out_path == tmp_path / "alice" / "dashboard.html"
    assert out_path.exists()

    html = out_path.read_text()
    assert "<!DOCTYPE html>" in html
    assert "alice" in html
    assert "Daily cost" in html
    assert "Helper savings" in html
    assert "Suggested cost caps" in html


def test_render_all_creates_global_and_per_agent(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    _write_log(tmp_path, "bob", today, [{"cost_usd": 0.05}])
    written = render_all(tmp_path, today=today)

    assert written["global"] == str(tmp_path / "_dashboard" / "index.html")
    assert len(written["per_agent"]) == 2
    assert (tmp_path / "alice" / "dashboard.html").exists()
    assert (tmp_path / "bob" / "dashboard.html").exists()


def test_render_writes_pre_aggregated_json(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    summary = aggregate_global(tmp_path, today=today)
    render_global(tmp_path, summary)

    data_dir = tmp_path / "_dashboard" / "data"
    assert data_dir.exists()
    json_files = list(data_dir.glob("*.json"))
    assert len(json_files) == 1

    parsed = json.loads(json_files[0].read_text())
    assert parsed["total_runs"] == 1


def test_render_handles_empty_agents_root(tmp_path):
    today = date.today()
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)

    html = out_path.read_text()
    assert "No agent activity" in html or "Atomic Agents" in html


def test_render_agent_includes_helper_savings_when_present(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"trigger": "cron", "model": "claude-opus-4-7-20260101", "cost_usd": 0.10},
        {"trigger": "helper", "model": "claude-haiku-4-5-20251001", "cost_usd": 0.001,
         "input_tokens": 1000, "output_tokens": 50},
    ])
    data = aggregate_agent(tmp_path, "alice", today=today)
    out_path = render_agent(tmp_path, data)
    html = out_path.read_text()
    assert "saved" in html.lower()
    assert "1 helper call" in html


def test_render_agent_handles_no_helpers(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"trigger": "cron", "cost_usd": 0.10}])
    data = aggregate_agent(tmp_path, "alice", today=today)
    out_path = render_agent(tmp_path, data)
    html = out_path.read_text()
    assert "No helper calls" in html

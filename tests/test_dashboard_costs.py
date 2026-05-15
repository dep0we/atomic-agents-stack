"""Tests for atomic_agents.dashboard.costs."""

from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from atomic_agents.dashboard.costs import (
    aggregate_agent,
    aggregate_global,
    cache_savings_usd,
    discover_agents,
    helper_savings,
    load_runs,
    summarize_agent,
    suggest_caps,
    to_json_dict,
)


def _write_log(agents_root: Path, agent: str, when: date, records: list[dict]) -> Path:
    """Helper: create a log JSONL file with given records.

    Per #61 PR 2: records get a tz-aware ts to match what
    ``agent._log()`` produces in production (``datetime.now().astimezone()
    .isoformat()``). Naive ts breaks ``FilesystemLogBackend.query`` lex
    comparison against tz-aware since/until bounds.
    """
    log_dir = agents_root / agent / "log" / when.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{when.isoformat()}.jsonl"
    lines = []
    for rec in records:
        rec.setdefault(
            "ts",
            datetime.combine(when, datetime.min.time()).astimezone().isoformat(),
        )
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


def test_discover_agents(tmp_path):
    (tmp_path / "alice" / "log").mkdir(parents=True)
    (tmp_path / "bob" / "log").mkdir(parents=True)
    (tmp_path / "no-log-dir").mkdir()  # not an agent
    (tmp_path / "_dashboard").mkdir()  # excluded prefix
    (tmp_path / ".hidden").mkdir()

    agents = discover_agents(tmp_path)
    assert agents == ["alice", "bob"]


def test_discover_agents_empty_root(tmp_path):
    assert discover_agents(tmp_path) == []
    assert discover_agents(tmp_path / "nonexistent") == []


def test_load_runs_basic(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"summary": "morning", "cost_usd": 0.10},
        {"summary": "afternoon", "cost_usd": 0.20},
    ])
    runs = load_runs(tmp_path, "alice", today, today)
    assert len(runs) == 2
    assert runs[0].agent == "alice"
    assert runs[0].cost_usd == 0.10


def test_load_runs_skips_malformed(tmp_path):
    today = date.today()
    # Per #61 PR 2: full ISO-8601 tz-aware ts required (the legacy
    # ``date.isoformat()`` was a test shortcut; production records via
    # ``agent._log()`` always carry full datetimes).
    ts_today = datetime.combine(today, datetime.min.time()).astimezone().isoformat()
    log_dir = tmp_path / "alice" / "log" / today.strftime("%Y-%m")
    log_dir.mkdir(parents=True)
    log_dir.joinpath(f"{today.isoformat()}.jsonl").write_text(
        json.dumps({"ts": ts_today, "cost_usd": 0.10, "model": "claude-opus-4-7-20260101"}) + "\n"
        + "not valid json\n"
        + json.dumps({"ts": ts_today, "cost_usd": 0.20, "model": "claude-opus-4-7-20260101"}) + "\n"
    )
    runs = load_runs(tmp_path, "alice", today, today)
    assert len(runs) == 2  # malformed line skipped


def test_load_runs_respects_date_range(tmp_path):
    today = date.today()
    yesterday = today - timedelta(days=1)
    _write_log(tmp_path, "alice", yesterday, [{"cost_usd": 0.10}])
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.20}])

    # Today only
    runs = load_runs(tmp_path, "alice", today, today)
    assert len(runs) == 1
    assert runs[0].cost_usd == 0.20

    # Both days
    runs = load_runs(tmp_path, "alice", yesterday, today)
    assert len(runs) == 2


def test_summarize_agent_basic(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"trigger": "cron", "model": "claude-opus-4-7-20260101", "cost_usd": 0.10,
         "input_tokens": 1000, "output_tokens": 200, "cache_hit_tokens": 800, "cache_miss_tokens": 200},
        {"trigger": "skill", "model": "claude-sonnet-4-6-20260101", "cost_usd": 0.02,
         "input_tokens": 500, "output_tokens": 100, "cache_hit_tokens": 0, "cache_miss_tokens": 500},
        {"trigger": "helper", "model": "claude-haiku-4-5-20251001", "cost_usd": 0.001,
         "input_tokens": 100, "output_tokens": 20},
    ])
    runs = load_runs(tmp_path, "alice", today, today)
    summary = summarize_agent(runs)

    assert summary.name == "alice"
    assert summary.runs == 3
    assert summary.cost_usd == pytest.approx(0.121, rel=1e-3)
    assert summary.helper_runs == 1
    assert summary.helper_cost_usd == pytest.approx(0.001, rel=1e-3)
    assert summary.errors == 0
    assert "claude-opus-4-7-20260101" in summary.cost_by_model
    assert "claude-sonnet-4-6-20260101" in summary.cost_by_model
    assert "claude-haiku-4-5-20251001" in summary.cost_by_model
    # Cache hit rate: 800 / (800 + 200 + 500) = 800 / 1500 = 53.3%
    assert summary.cache_hit_pct == pytest.approx(53.3, rel=1e-2)


def test_summarize_agent_empty():
    summary = summarize_agent([])
    assert summary.runs == 0
    assert summary.cost_usd == 0.0
    assert summary.cache_hit_pct == 0.0


def test_helper_savings(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"trigger": "cron", "model": "claude-opus-4-7-20260101", "cost_usd": 0.10,
         "input_tokens": 1000, "output_tokens": 200},
        {"trigger": "helper", "model": "claude-haiku-4-5-20251001", "cost_usd": 0.001,
         "input_tokens": 1000, "output_tokens": 100},
        {"trigger": "helper", "model": "claude-haiku-4-5-20251001", "cost_usd": 0.001,
         "input_tokens": 1000, "output_tokens": 100},
    ])
    runs = load_runs(tmp_path, "alice", today, today)
    savings = helper_savings(runs, "claude-opus-4-7-20260101")

    assert savings is not None
    assert savings.helper_calls == 2
    assert savings.helper_actual_cost == pytest.approx(0.002, rel=1e-3)
    # Hypothetical: each helper would have cost 1000 * 15 / 1M + 100 * 75 / 1M = 0.015 + 0.0075 = 0.0225
    # Two of them = 0.045
    assert savings.hypothetical_main_cost == pytest.approx(0.045, rel=1e-2)
    assert savings.saved == pytest.approx(0.043, rel=1e-2)
    assert savings.cost_ratio > 1.0  # helpers cheaper


def test_helper_savings_no_helpers(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"trigger": "cron", "cost_usd": 0.10},
    ])
    runs = load_runs(tmp_path, "alice", today, today)
    assert helper_savings(runs, "claude-opus-4-7-20260101") is None


def test_cache_savings_usd(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"model": "claude-opus-4-7-20260101", "cache_hit_tokens": 10000, "cache_miss_tokens": 0,
         "cost_usd": 0.015},
    ])
    runs = load_runs(tmp_path, "alice", today, today)
    saved = cache_savings_usd(runs)
    # 10000 cached tokens at $15/MTok with 90% discount saved = 10000 * 15 * 0.9 / 1M = 0.135
    assert saved == pytest.approx(0.135, rel=1e-3)


def test_suggest_caps_insufficient_data(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    runs = load_runs(tmp_path, "alice", today - timedelta(days=30), today)
    assert suggest_caps(runs) is None  # only 1 day of data


def test_suggest_caps_with_14_days(tmp_path):
    today = date.today()
    for i in range(14):
        _write_log(tmp_path, "alice", today - timedelta(days=i), [{"cost_usd": 0.10}])
    runs = load_runs(tmp_path, "alice", today - timedelta(days=30), today)
    caps = suggest_caps(runs)
    assert caps is not None
    assert caps["based_on_days"] == 14
    assert caps["avg_daily"] == pytest.approx(0.10, rel=1e-3)
    assert caps["suggested_daily_cap_usd"] > 0
    assert caps["suggested_monthly_cap_usd"] > 0


def test_aggregate_global(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}, {"cost_usd": 0.20}])
    _write_log(tmp_path, "bob", today, [{"cost_usd": 0.05}])
    summary = aggregate_global(tmp_path, today=today)
    assert summary.total_runs == 3
    assert summary.total_cost == pytest.approx(0.35, rel=1e-3)
    assert len(summary.agents) == 2
    assert {a.name for a in summary.agents} == {"alice", "bob"}


def test_aggregate_global_top_runs(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"cost_usd": 0.50, "summary": "expensive"},
        {"cost_usd": 0.10, "summary": "medium"},
        {"cost_usd": 0.01, "summary": "cheap"},
    ])
    summary = aggregate_global(tmp_path, today=today, top_runs_count=2)
    assert len(summary.top_runs) == 2
    assert summary.top_runs[0].cost_usd == 0.50  # most expensive first
    assert summary.top_runs[1].cost_usd == 0.10


def test_aggregate_agent(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [
        {"trigger": "cron", "model": "claude-opus-4-7-20260101", "cost_usd": 0.10},
        {"trigger": "helper", "model": "claude-haiku-4-5-20251001", "cost_usd": 0.001,
         "input_tokens": 1000, "output_tokens": 50},
    ])
    data = aggregate_agent(tmp_path, "alice", today=today)
    assert data.name == "alice"
    assert data.summary_this_month.runs == 2
    assert data.helper_savings is not None
    assert data.helper_savings.helper_calls == 1


def test_to_json_dict_handles_dataclasses_and_dates(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    summary = aggregate_global(tmp_path, today=today)
    j = to_json_dict(summary)
    # Should be JSON-serializable
    text = json.dumps(j)
    parsed = json.loads(text)
    assert parsed["total_runs"] == 1

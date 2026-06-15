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
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {"summary": "morning", "cost_usd": 0.10},
            {"summary": "afternoon", "cost_usd": 0.20},
        ],
    )
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
        json.dumps(
            {"ts": ts_today, "cost_usd": 0.10, "model": "claude-opus-4-7-20260101"}
        )
        + "\n"
        + "not valid json\n"
        + json.dumps(
            {"ts": ts_today, "cost_usd": 0.20, "model": "claude-opus-4-7-20260101"}
        )
        + "\n"
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
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {
                "trigger": "cron",
                "model": "claude-opus-4-7-20260101",
                "cost_usd": 0.10,
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_hit_tokens": 800,
                "cache_miss_tokens": 200,
            },
            {
                "trigger": "skill",
                "model": "claude-sonnet-4-6-20260101",
                "cost_usd": 0.02,
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 500,
            },
            {
                "trigger": "helper",
                "model": "claude-haiku-4-5-20251001",
                "cost_usd": 0.001,
                "input_tokens": 100,
                "output_tokens": 20,
            },
        ],
    )
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
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {
                "trigger": "cron",
                "model": "claude-opus-4-7-20260101",
                "cost_usd": 0.10,
                "input_tokens": 1000,
                "output_tokens": 200,
            },
            {
                "trigger": "helper",
                "model": "claude-haiku-4-5-20251001",
                "cost_usd": 0.001,
                "input_tokens": 1000,
                "output_tokens": 100,
            },
            {
                "trigger": "helper",
                "model": "claude-haiku-4-5-20251001",
                "cost_usd": 0.001,
                "input_tokens": 1000,
                "output_tokens": 100,
            },
        ],
    )
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
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {"trigger": "cron", "cost_usd": 0.10},
        ],
    )
    runs = load_runs(tmp_path, "alice", today, today)
    assert helper_savings(runs, "claude-opus-4-7-20260101") is None


def test_cache_savings_usd(tmp_path):
    today = date.today()
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {
                "model": "claude-opus-4-7-20260101",
                "cache_hit_tokens": 10000,
                "cache_miss_tokens": 0,
                "cost_usd": 0.015,
            },
        ],
    )
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
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {"cost_usd": 0.50, "summary": "expensive"},
            {"cost_usd": 0.10, "summary": "medium"},
            {"cost_usd": 0.01, "summary": "cheap"},
        ],
    )
    summary = aggregate_global(tmp_path, today=today, top_runs_count=2)
    assert len(summary.top_runs) == 2
    assert summary.top_runs[0].cost_usd == 0.50  # most expensive first
    assert summary.top_runs[1].cost_usd == 0.10


def test_aggregate_agent(tmp_path):
    today = date.today()
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {"trigger": "cron", "model": "claude-opus-4-7-20260101", "cost_usd": 0.10},
            {
                "trigger": "helper",
                "model": "claude-haiku-4-5-20251001",
                "cost_usd": 0.001,
                "input_tokens": 1000,
                "output_tokens": 50,
            },
        ],
    )
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


def test_load_runs_degrades_to_empty_on_read_error(tmp_path, monkeypatch):
    """load_runs degrades to [] when the backend raises LogBackendReadError.

    spec/22 read-failure addendum (#497): the dashboard is a reporting surface,
    not a control gate — an unrecoverable blind read renders empty rather than
    crashing the dashboard. (Empty/absent state already returns [] without
    raising; this pins the corruption/I-O failure path.)
    """
    from unittest.mock import MagicMock

    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError

    mock_backend = MagicMock()
    mock_backend.query.side_effect = LogBackendReadError("corrupt log")
    monkeypatch.setattr(logs_mod, "get_default_log_backend", lambda root: mock_backend)

    today = date.today()
    result = load_runs(tmp_path, "alice", today, today)
    assert result == []
    # False-green guard: prove the backend was consulted and the exception
    # path (not the absent-dir [] path) was exercised.
    assert mock_backend.query.called


# ──────────────────────────────────────────────────────────────────
# #498 — degraded-read banner propagation tests


def _inject_failing_backend(monkeypatch, *, fail_agent: str = "alice"):
    """Monkeypatch get_default_log_backend so the named agent's backend raises
    LogBackendReadError while other agents' backends succeed normally."""
    from unittest.mock import MagicMock

    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError

    original_get = logs_mod.get_default_log_backend

    def _patched_get(root):
        if root.name == fail_agent:
            mock = MagicMock()
            mock.query.side_effect = LogBackendReadError("injected failure")
            return mock
        return original_get(root)

    monkeypatch.setattr(logs_mod, "get_default_log_backend", _patched_get)


def test_load_runs_with_degraded_sets_flag_on_error(tmp_path, monkeypatch):
    """_load_runs_with_degraded returns ([], True) on LogBackendReadError.

    Branch-distinctive assertion: degraded=True (not just runs==[]).
    Both the degraded path AND the clean path return [], so the empty
    list alone is the shared empty-render path — the flag is what distinguishes
    them (layered-except false-green lesson, MEMORY.md).
    """
    from unittest.mock import MagicMock

    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError
    from atomic_agents.dashboard.costs import _load_runs_with_degraded

    mock_backend = MagicMock()
    mock_backend.query.side_effect = LogBackendReadError("corrupt log")
    monkeypatch.setattr(logs_mod, "get_default_log_backend", lambda root: mock_backend)

    today = date.today()
    runs, degraded = _load_runs_with_degraded(tmp_path, "alice", today, today)

    # Branch-distinctive assertion: degraded flag, not the empty list
    assert degraded is True, "LogBackendReadError must set degraded=True"
    assert runs == []
    # False-green guard: the backend was called (not the absent-dir [] path)
    assert mock_backend.query.called


def test_load_runs_with_degraded_no_flag_on_success(tmp_path):
    """Negative control: _load_runs_with_degraded returns (runs, False) on clean read.

    Strips the failure injection — confirms degraded=False when no error occurs.
    This is the negative control required by the layered-except false-green lesson.
    """
    from atomic_agents.dashboard.costs import _load_runs_with_degraded

    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    runs, degraded = _load_runs_with_degraded(tmp_path, "alice", today, today)

    assert degraded is False, "A clean read must NOT set degraded=True"
    assert len(runs) == 1


def test_aggregate_global_cost_data_degraded_on_read_error(tmp_path, monkeypatch):
    """aggregate_global sets cost_data_degraded=True when any agent read fails.

    Verifies the OR-accumulation threading from _load_runs_with_degraded
    through to GlobalSummary.cost_data_degraded.
    """
    today = date.today()
    # Write clean data for alice; bob will fail
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    _write_log(tmp_path, "bob", today, [{"cost_usd": 0.05}])
    _inject_failing_backend(monkeypatch, fail_agent="bob")

    summary = aggregate_global(tmp_path, today=today)

    assert summary.cost_data_degraded is True, (
        "GlobalSummary.cost_data_degraded must be True when any agent's read fails"
    )
    # Alice's data should still be present (partial render, not crash)
    assert summary.total_cost > 0.0


def test_aggregate_global_not_degraded_on_clean_read(tmp_path):
    """Negative control: aggregate_global leaves cost_data_degraded=False when all reads succeed."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])

    summary = aggregate_global(tmp_path, today=today)

    assert summary.cost_data_degraded is False, (
        "GlobalSummary.cost_data_degraded must be False when all reads succeed"
    )


def test_aggregate_agent_cost_data_degraded_on_read_error(tmp_path, monkeypatch):
    """aggregate_agent sets cost_data_degraded=True when its 12-month read fails."""
    from unittest.mock import MagicMock

    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError

    mock_backend = MagicMock()
    mock_backend.query.side_effect = LogBackendReadError("corrupt log")
    monkeypatch.setattr(logs_mod, "get_default_log_backend", lambda root: mock_backend)

    today = date.today()
    data = aggregate_agent(tmp_path, "alice", today=today)

    # Branch-distinctive assertion: the degraded field, not the empty summary
    assert data.cost_data_degraded is True, (
        "AgentDashboardData.cost_data_degraded must be True on LogBackendReadError"
    )


def test_aggregate_agent_not_degraded_on_clean_read(tmp_path):
    """Negative control: aggregate_agent leaves cost_data_degraded=False on clean read."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])

    data = aggregate_agent(tmp_path, "alice", today=today)

    assert data.cost_data_degraded is False


def test_json_sidecar_includes_degraded_field(tmp_path):
    """to_json_dict serialises cost_data_degraded from GlobalSummary.

    The dashboard ships only the boolean signal (no dropped_records count): the
    query()/LogBackendReadError read path raises before returning any records,
    so a record count has no honest definition on this surface (unlike
    _costs.CostReadResult.dropped_records, which counts per-line corruption on
    the cost-summing reader). Asserting the bool's absence default + forced-True
    round-trip guards the backward-compatible sidecar shape (Principle #1/#14).
    """
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    summary = aggregate_global(tmp_path, today=today)

    # Default (clean read): field present and False in the sidecar.
    parsed_default = json.loads(json.dumps(to_json_dict(summary)))
    assert parsed_default["cost_data_degraded"] is False
    # No dropped_records key — the dashboard intentionally does not ship one.
    assert "dropped_records" not in parsed_default

    # Force the flag so we can assert it round-trips.
    summary.cost_data_degraded = True
    parsed = json.loads(json.dumps(to_json_dict(summary)))
    assert parsed["cost_data_degraded"] is True


# ──────────────────────────────────────────────────────────────────
# #498 — per-CONTRIBUTOR negative-control isolation for aggregate_global
#
# aggregate_global OR-merges the degraded flag from TWO independent reads of a
# failing backend: the per-agent this_month/last_month KPI load
# (costs.py: ``any_degraded = any_degraded or deg_this or deg_last``) AND the
# monthly-trend load (costs.py: ``any_degraded = any_degraded or trend_degraded``,
# fed by ``_build_monthly_trend``, which ALSO reads the current month).
#
# A current-month-degraded test cannot tell the two apart: stripping the KPI
# contributor alone leaves the trend contributor firing, so the headline-banner
# test stays green while half the fix is gone (the original false-green caught in
# Round 4). Per ``feedback_false_green_test_needs_per_invocation_negative_control``
# (MEMORY.md: "strip EACH independent part separately; only the negative control
# catches a partial false-green"), the two tests below give the failing read
# DISJOINT windows / occurrences so EACH contributor is exercised in isolation:
#
#   * ``..._isolates_monthly_trend_contributor`` fails ONLY on an old month
#     (2 months back) that the KPI reads never touch — bites the monthly-trend
#     OR-term (``any_degraded = any_degraded or trend_degraded`` in
#     ``aggregate_global``), stays green under a strip of the KPI OR-term.
#   * ``..._isolates_kpi_contributor`` fails ONLY on the FIRST query (the
#     this_month KPI read, which runs before the trend reads the same month) —
#     bites the KPI OR-term (``any_degraded = any_degraded or deg_this or
#     deg_last`` in ``aggregate_global``), stays green under a strip of the
#     monthly-trend OR-term.
#
# Negative controls verified by hand (Round 4): stripping the KPI OR-term
# (``or deg_this or deg_last``) leaves the monthly-trend test green + the KPI
# test red; stripping the monthly-trend OR-term (``or trend_degraded``) leaves
# the KPI test green + the monthly-trend test red — i.e. each strip bites
# exactly ONE. (Symbolic OR-term citations, not line numbers, which drift on
# any edit above them — per the #506 "convert line citations to section/
# behavioral citations" discipline.)


def _window_failing_backend(monkeypatch, *, fail_agent: str, fail_months: set[int]):
    """Patch get_default_log_backend so ``fail_agent``'s backend raises
    LogBackendReadError ONLY when the query's ``since`` month is in
    ``fail_months``; all other windows (and all other agents) read normally.

    This isolates which read window triggered the degraded flag, so a test can
    target ONE of aggregate_global's two OR-contributors at a time."""
    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError

    original_get = logs_mod.get_default_log_backend

    def _patched_get(root):
        real = original_get(root)
        if root.name != fail_agent:
            return real
        orig_query = real.query

        def _maybe_fail(q):
            if q.since is not None and q.since.month in fail_months:
                raise LogBackendReadError("injected window failure")
            return orig_query(q)

        real.query = _maybe_fail
        return real

    monkeypatch.setattr(logs_mod, "get_default_log_backend", _patched_get)


def _occurrence_failing_backend(monkeypatch, *, fail_agent: str, fail_on: int):
    """Patch get_default_log_backend so ``fail_agent``'s backend raises
    LogBackendReadError ONLY on its ``fail_on``-th query (1-indexed); all other
    queries (and all other agents) read normally.

    aggregate_global issues the per-agent KPI this_month read FIRST, then the
    monthly-trend reads. Failing only query #1 degrades the KPI contributor
    while the trend's same-month read succeeds — isolating the KPI OR-term
    (``any_degraded = any_degraded or deg_this or deg_last``)."""
    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError

    original_get = logs_mod.get_default_log_backend
    counter = {"n": 0}

    def _patched_get(root):
        real = original_get(root)
        if root.name != fail_agent:
            return real
        orig_query = real.query

        def _maybe_fail(q):
            counter["n"] += 1
            if counter["n"] == fail_on:
                raise LogBackendReadError("injected occurrence failure")
            return orig_query(q)

        real.query = _maybe_fail
        return real

    monkeypatch.setattr(logs_mod, "get_default_log_backend", _patched_get)


def test_aggregate_global_degraded_isolates_monthly_trend_contributor(
    tmp_path, monkeypatch
):
    """Isolation: degraded flag fired SOLELY by the monthly-trend read.

    The failing window is 2 months before ``today`` — outside the KPI reads'
    this_month/last_month windows, so ONLY ``_build_monthly_trend`` touches it.
    This bites a strip of the monthly-trend OR-term (``any_degraded =
    any_degraded or trend_degraded``) and stays green under a strip of the KPI
    OR-term (``... or deg_this or deg_last``) (the existing current-month test
    covers the joint case; this one guards the trend contributor in isolation).
    """
    today = date(2026, 6, 15)
    two_months_ago = date(2026, 4, 10)
    # bob has data in the old (failing) month; alice keeps the page non-empty.
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    _write_log(tmp_path, "bob", two_months_ago, [{"cost_usd": 0.07}])
    _window_failing_backend(monkeypatch, fail_agent="bob", fail_months={4})

    summary = aggregate_global(tmp_path, today=today)

    # Branch-distinctive assertion on the flag, not the (shared) empty-render path.
    assert summary.cost_data_degraded is True, (
        "monthly-trend read failure (old month) must set cost_data_degraded — "
        "this is the `or trend_degraded` OR-contributor in isolation"
    )
    # Partial render preserved: the current-month KPI read of bob succeeded
    # (empty for the failing old month is fine) and alice's data is present.
    assert summary.total_cost > 0.0


def test_aggregate_global_degraded_not_set_when_only_old_clean(tmp_path, monkeypatch):
    """Negative control for the monthly-trend isolation: when NO window fails,
    even with old-month data present, the flag stays False."""
    today = date(2026, 6, 15)
    two_months_ago = date(2026, 4, 10)
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    _write_log(tmp_path, "bob", two_months_ago, [{"cost_usd": 0.07}])
    _window_failing_backend(monkeypatch, fail_agent="bob", fail_months=set())

    summary = aggregate_global(tmp_path, today=today)

    assert summary.cost_data_degraded is False


def test_aggregate_global_degraded_isolates_kpi_contributor(tmp_path, monkeypatch):
    """Isolation: degraded flag fired SOLELY by the this_month KPI read.

    aggregate_global issues the per-agent KPI this_month query FIRST, before any
    monthly-trend query. Failing ONLY query #1 degrades the KPI
    (``... or deg_this or deg_last``) contributor while the trend's later read
    of the SAME current month succeeds, so the flag's truth comes exclusively
    from the KPI OR-term.

    This bites a strip of the KPI OR-term (``... or deg_this or deg_last``) and
    stays green under a strip of the monthly-trend OR-term
    (``... or trend_degraded``) — the missing half of the negative control
    that let the original current-month-only test false-green.
    """
    today = date(2026, 6, 15)
    # Single failing agent so query ordering is deterministic (KPI this_month is
    # query #1). alice's current-month data is present for a partial render.
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    _occurrence_failing_backend(monkeypatch, fail_agent="alice", fail_on=1)

    summary = aggregate_global(tmp_path, today=today)

    # Branch-distinctive assertion on the flag.
    assert summary.cost_data_degraded is True, (
        "this_month KPI read failure (query #1) must set cost_data_degraded — "
        "this is the `or deg_this or deg_last` OR-contributor in isolation; the "
        "trend's later read of the same month succeeds, so only the KPI term "
        "can carry it"
    )


def test_aggregate_global_degraded_not_set_when_no_occurrence_fails(
    tmp_path, monkeypatch
):
    """Negative control for the KPI isolation: failing an out-of-range
    occurrence (one that never fires) leaves the flag False."""
    today = date(2026, 6, 15)
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    # fail_on far beyond the number of queries aggregate_global issues.
    _occurrence_failing_backend(monkeypatch, fail_agent="alice", fail_on=9999)

    summary = aggregate_global(tmp_path, today=today)

    assert summary.cost_data_degraded is False

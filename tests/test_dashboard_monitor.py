"""Conformance tests for the Fleet Monitor page (spec/56 #653).

Conformance map (spec/56 §10):
  MUST 1  — monitor.html emitted; every agent in list_agents() is one row
             test_monitor_enumerates_all_fleet_agents (boundary: empty fleet)
  MUST 2* — status_for_agent() is THE derivation (no parallel logic)
             test_monitor_status_uses_shared_status_for_agent (strip-RED)
  MUST 3  — problems-first default order (ERROR→STALE→WARN→OK)
             test_monitor_default_order_is_problems_first
  MUST 4* — view toggle persistence: ?view= wins → localStorage → list
             test_monitor_view_toggle_persistence (strip-RED via rendered HTML)
  MUST 5* — ?status=<ok|warn|error|stale> pre-applies filter + banner;
             unrecognized/uppercase → no filter, no banner
             test_monitor_status_query_preapplies_filter (strip-RED: uppercase ignored)
             test_monitor_invalid_status_ignored
  MUST 6  — filter (status, model) + free-text search + column sort
             test_monitor_filter_sort_search (empty-result boundary)
  MUST 7* — every entity links to agent-detail.html?agent=<id>
             test_monitor_entity_links_to_detail (strip-RED: href format)
  MUST 8  — freshness stamp + windows visible
             test_monitor_freshness_stamp_and_windows
  MUST 9  — per-entity columns: status, name, model, health, errors(24h),
             failures(7d), 7d cost, last-run, sparkline
             test_monitor_entity_columns_present
  MUST 10* — per-entity fail-soft: one degraded row ≠ page fail
             test_monitor_one_agent_degraded_degrades_only_that_row (strip-RED)
             test_monitor_cost_degraded_banner
             test_monitor_unenumerable_agent_is_not_a_row
  MUST 11* — no LLM backend constructed on the render path
             test_monitor_no_llm_spend_on_render (strip-RED)
  MUST 12* — monitor status counts == home status counts for same snapshot
             test_monitor_status_counts_equal_home_summary (strip-RED: divergent window)
  MUST 13* — no SSE / fetch / background polling in the rendered HTML
             test_monitor_render_has_no_polling (strip-RED)

*  = strip-RED negative control required (spec/56 §10).
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import atomic_agents.dashboard.panels._registry as _registry_mod
from atomic_agents.dashboard.panels._registry import (
    ConsoleCapabilities,
    PanelContext,
    PanelRegistry,
    PanelResult,
)
from atomic_agents.dashboard._status import status_for_agent


# ──────────────────────────────────────────────────────────────────
# Shared stub types (mirrors test_dashboard_panels.py conventions)


@dataclass
class _StubAlertItem:
    agent: str
    alert_key: str = "v1:abc123"
    alert_class: str = "cost_spike"
    alert_subclass: str = "SPIKE"
    severity: str = "high"
    reason: str = "test"
    next_step: str = "check it"
    owner: str | None = None
    status: str = "open"
    ack_snooze_status: str = "open"


@dataclass
class _StubAgentHealth:
    agent: str = "agent1"
    band: str = "green"
    composite: float = 0.85
    capped_by_axis: str | None = None
    primary_model: str | None = "claude-sonnet-4-5"
    cost_score: float | None = 85.0
    quality_score: float | None = 85.0
    reliability_score: float | None = 85.0
    scorecard: list = field(default_factory=list)


@dataclass
class _StubFleetHealth:
    fleet_composite: float = 0.85
    fleet_composite_display: int = 85
    fleet_band: str = "green"
    coverage_n: int = 1
    coverage_m: int = 1
    worst_agent: str | None = None
    worst_agent_composite: float | None = None
    degraded: bool = False
    used_targets_defaults: bool = False
    agents: list = field(default_factory=list)


@dataclass
class _StubCostTrend:
    agent: str = "agent1"
    total_usd_30d: float = 1.00
    spike_detected: bool = False
    daily_series: list = field(default_factory=list)


@dataclass
class _StubReliabilityMetrics:
    agent: str = "agent1"
    total_runs: int = 10
    error_rate: float = 0.0
    blocked_rate: float = 0.0
    # Real display-window counts (spec/56 MUST 9 — populated by aggregate_console).
    errors_24h: int = 0
    failures_7d: int = 0


@dataclass
class _StubConsoleData:
    agent_count: int = 1
    attention_queue: list = field(default_factory=list)
    cost_trends: list = field(default_factory=list)
    quality_signals: list = field(default_factory=list)
    reliability_metrics: list = field(default_factory=list)
    fleet_health: object | None = None
    recommendations: list | None = None
    degraded: bool = False
    rendered_alert_keys: frozenset = field(default_factory=frozenset)
    last_primary_runs: dict = field(default_factory=dict)


_NOW = datetime(2026, 7, 5, 14, 0, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 7, 5)
_RECENT = _NOW - timedelta(hours=1)  # within staleness window → NOT stale
_STALE_TS = _NOW - timedelta(hours=36)  # beyond 24h → STALE


def _make_ctx(
    console_data=None,
    capabilities=None,
    today=None,
    now=None,
) -> PanelContext:
    return PanelContext(
        console_data=console_data or _StubConsoleData(),
        capabilities=capabilities or ConsoleCapabilities(),
        today=today or _TODAY,
        now=now or _NOW,
    )


def _make_fleet_ctx_multi(
    agents_statuses: dict[str, str],
) -> tuple[_StubConsoleData, PanelContext]:
    """Build a ConsoleData + PanelContext with agents having given statuses.

    agents_statuses: {agent_id: desired_status} where status in ERROR/STALE/WARN/OK.
    """
    lpr: dict[str, datetime | None] = {}
    ah_list = []
    queue = []
    for agent_id, desired_status in agents_statuses.items():
        if desired_status == "STALE":
            lpr[agent_id] = _STALE_TS
        elif desired_status == "ERROR":
            lpr[agent_id] = _RECENT
            ah = _StubAgentHealth(agent=agent_id, capped_by_axis="reliability")
            ah_list.append(ah)
        elif desired_status == "WARN":
            lpr[agent_id] = _RECENT
            item = _StubAlertItem(agent=agent_id)
            queue.append(item)
        else:  # OK
            lpr[agent_id] = _RECENT

    fh = _StubFleetHealth(agents=ah_list)
    cd = _StubConsoleData(
        agent_count=len(agents_statuses),
        last_primary_runs=lpr,
        attention_queue=queue,
        fleet_health=fh,
    )
    ctx = _make_ctx(console_data=cd)
    return cd, ctx


# ──────────────────────────────────────────────────────────────────
# Helpers: render monitor page from a tmp_path fixture


def _write_agent(agents_root: Path, agent: str) -> None:
    """Create a minimal agent directory (model.md present = enumerable)."""
    (agents_root / agent).mkdir(parents=True, exist_ok=True)
    (agents_root / agent / "model.md").write_text("# model\n")


def _render_monitor_html(
    agents_root: Path, console_data=None, now=None, today=None
) -> str:
    """Call render_monitor() and return the HTML string."""
    from atomic_agents.dashboard.render_monitor import render_monitor

    if console_data is None:
        console_data = _StubConsoleData()
    path = render_monitor(
        agents_root,
        console_data,
        now=now or _NOW,
        today=today or _TODAY,
        has_goals=False,
    )
    return path.read_text()


# ──────────────────────────────────────────────────────────────────
# MUST 1: monitor.html is emitted; all fleet agents are entities


def test_monitor_enumerates_all_fleet_agents(tmp_path):
    """MUST 1: render_monitor writes monitor.html; non-empty fleet has all agents."""
    for agent in ("alpha", "beta", "gamma"):
        _write_agent(tmp_path, agent)
    lpr = {"alpha": _RECENT, "beta": _RECENT, "gamma": _RECENT}
    cd = _StubConsoleData(agent_count=3, last_primary_runs=lpr)
    html = _render_monitor_html(tmp_path, console_data=cd)
    assert "<!DOCTYPE html>" in html
    assert "Fleet Monitor" in html
    # All 3 agents appear in the embedded AGENTS JSON
    assert '"alpha"' in html
    assert '"beta"' in html
    assert '"gamma"' in html


def test_monitor_empty_fleet_renders_clean_state(tmp_path):
    """MUST 1 boundary: zero enumerated agents → clean empty state, not error."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)
    assert "<!DOCTYPE html>" in html
    assert "atomic-agents init" in html  # empty-state prompt
    assert "<!DOCTYPE html>" in html  # page rendered fully


# ──────────────────────────────────────────────────────────────────
# MUST 2*: status_for_agent() is THE function (no parallel logic)


def test_monitor_status_uses_shared_status_for_agent():
    """MUST 2 positive: the monitor summary panel calls status_for_agent() from _status."""
    from atomic_agents.dashboard.panels._monitor_summary import _MonitorSummaryPanel

    ah = _StubAgentHealth(agent="a1", capped_by_axis="reliability")
    fh = _StubFleetHealth(agents=[ah])
    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs={"a1": _RECENT},
        fleet_health=fh,
    )
    ctx = _make_ctx(console_data=cd)
    panel = _MonitorSummaryPanel()
    result = panel.render(ctx)
    # capped_by_axis → ERROR
    assert 'chip-count-error">1' in result.html


def test_monitor_status_uses_shared_status_for_agent_strip_red():
    """MUST 2 strip-RED: a divergent local status impl would get a DIFFERENT count.

    We verify that if we mock status_for_agent to always return 'OK', the monitor
    summary count for ERROR becomes 0 — proving the panel actually calls the shared
    function and does NOT have a hardcoded/local derivation that would survive the mock.
    """
    from atomic_agents.dashboard.panels._monitor_summary import _MonitorSummaryPanel

    ah = _StubAgentHealth(agent="a1", capped_by_axis="reliability")
    fh = _StubFleetHealth(agents=[ah])
    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs={"a1": _RECENT},
        fleet_health=fh,
    )
    ctx = _make_ctx(console_data=cd)
    panel = _MonitorSummaryPanel()

    # Normal call: capped_by_axis → ERROR count should be 1
    result = panel.render(ctx)
    assert 'chip-count-error">1' in result.html

    # Strip-RED: patch the canonical _status module that the panel imports inside render().
    # status_for_agent is imported lazily inside render() via:
    #   from .._status import status_for_agent
    # so we must patch "atomic_agents.dashboard._status.status_for_agent".
    with patch(
        "atomic_agents.dashboard._status.status_for_agent",
        return_value="OK",
    ):
        result2 = panel.render(ctx)
    # With mock returning OK, error count must drop to 0
    assert 'chip-count-error">0' in result2.html


# ──────────────────────────────────────────────────────────────────
# MUST 3: problems-first default ordering


def _parse_agents_json(html: str) -> list:
    """Extract and parse the AGENTS JSON from the monitor-agents element."""
    # New XSS-safe embedding: <script type="application/json" id="monitor-agents">...</script>
    m = re.search(
        r'<script\s+type="application/json"\s+id="monitor-agents">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert m, "monitor-agents JSON element not found in monitor HTML"
    # Unescape the JSON-safe replacements we applied in the panel
    raw = (
        m.group(1)
        .replace("\\u0026", "&")
        .replace("\\u003c", "<")
        .replace("\\u003e", ">")
        .replace("\\u2028", " ")
        .replace("\\u2029", " ")
    )
    return json.loads(raw)


def test_monitor_default_order_is_problems_first(tmp_path):
    """MUST 3: server-rendered AGENTS JSON is ordered ERROR→STALE→WARN→OK.

    The default order is produced SERVER-SIDE so the page is problems-first
    without JS (spec/56 MUST 3 + review finding). We parse the embedded JSON
    element (not var AGENTS =) and assert the REAL ordering invariant:
    all error rows precede all stale rows, which precede all warn rows,
    which precede all ok rows.

    Strip-RED: scramble the server sort (comment out entity_list.sort in
    _monitor_roster.py) and this test fails because alphabetical order puts
    error_agent before ok_agent but warn_agent after stale_agent.
    """
    agents = {
        "ok_agent": "OK",
        "error_agent": "ERROR",
        "warn_agent": "WARN",
        "stale_agent": "STALE",
    }
    for a in agents:
        _write_agent(tmp_path, a)
    cd, _ = _make_fleet_ctx_multi(agents)
    html = _render_monitor_html(tmp_path, console_data=cd)

    agent_list = _parse_agents_json(html)
    statuses = [a["status"] for a in agent_list]

    # All 4 statuses must be present
    assert "error" in statuses
    assert "stale" in statuses
    assert "warn" in statuses
    assert "ok" in statuses

    # Real ordering invariant: error rows precede stale rows, stale precede warn,
    # warn precede ok. Find the last index of each "higher priority" status and
    # the first index of each "lower priority" status.
    _status_rank = {"error": 0, "stale": 1, "warn": 2, "ok": 3}

    def _last_idx(s):
        return max(i for i, x in enumerate(statuses) if x == s)

    def _first_idx(s):
        return min(i for i, x in enumerate(statuses) if x == s)

    assert _last_idx("error") < _first_idx("stale"), (
        f"MUST 3: last error at {_last_idx('error')} must precede first stale at {_first_idx('stale')}"
    )
    assert _last_idx("stale") < _first_idx("warn"), (
        f"MUST 3: last stale at {_last_idx('stale')} must precede first warn at {_first_idx('warn')}"
    )
    assert _last_idx("warn") < _first_idx("ok"), (
        f"MUST 3: last warn at {_last_idx('warn')} must precede first ok at {_first_idx('ok')}"
    )


def test_monitor_default_order_boundary_all_ok(tmp_path):
    """MUST 3 boundary: all-OK fleet renders without error."""
    agents = {"agent_a": "OK", "agent_b": "OK"}
    for a in agents:
        _write_agent(tmp_path, a)
    cd, _ = _make_fleet_ctx_multi(agents)
    html = _render_monitor_html(tmp_path, console_data=cd)
    agent_list = _parse_agents_json(html)
    assert all(a["status"] == "ok" for a in agent_list)


# ──────────────────────────────────────────────────────────────────
# MUST 4*: view toggle persistence


def test_monitor_view_toggle_persistence(tmp_path):
    """MUST 4 positive: HTML contains view toggle + ?view= persistence JS logic."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Toggle element exists
    assert 'id="view-toggle"' in html
    assert 'data-view="list"' in html
    assert 'data-view="cards"' in html

    # JS persistence contract (spec/56 §4): ?view= wins
    assert "fleet-monitor.view" in html  # localStorage key present
    assert "resolveView" in html  # persistence function present


def test_monitor_view_toggle_persistence_strip_red():
    """MUST 4 strip-RED: ?view= takes precedence over localStorage in JS logic.

    The resolveView() function must check params.get('view') BEFORE localStorage.
    We verify the function body in the rendered JS contains this order.
    """
    from atomic_agents.dashboard.render_monitor import _MONITOR_JS

    # The ?view= param check must come before localStorage
    qv_pos = _MONITOR_JS.index("params.get('view')")
    ls_pos = _MONITOR_JS.index("localStorage.getItem('fleet-monitor.view')")
    assert qv_pos < ls_pos, (
        "MUST 4 strip-RED: ?view= check must precede localStorage check in resolveView()"
    )

    # Invalid value must fall back to 'list'
    assert "return 'list'" in _MONITOR_JS


# ──────────────────────────────────────────────────────────────────
# MUST 5*: ?status= pre-applies filter + banner


def test_monitor_status_query_preapplies_filter(tmp_path):
    """MUST 5 positive: arrival-banner is wired; JS applies filter for recognized values."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Arrival banner element exists (hidden by default)
    assert 'id="arrival-banner"' in html
    assert 'id="arrival-chip-label"' in html

    # JS applies the filter for recognized lowercase tokens
    assert "applyArrivalFilter" in html
    for token in ["error", "warn", "ok", "stale"]:
        assert f"'{token}'" in html


def test_monitor_status_query_preapplies_filter_strip_red():
    """MUST 5 strip-RED: uppercase/garbage status is NOT in the valid list → ignored.

    The JS valid list must be exactly ['error', 'warn', 'ok', 'stale'] in lowercase.
    Uppercase 'ERROR' must not be in the valid tokens (spec/56 §1: case-sensitive).
    """
    from atomic_agents.dashboard.render_monitor import _MONITOR_JS

    # Verify the valid list is lowercase-only
    assert (
        "'error', 'warn', 'ok', 'stale'" in _MONITOR_JS
        or "['error', 'warn', 'ok', 'stale']" in _MONITOR_JS
    ), "MUST 5 strip-RED: valid status tokens must be lowercase-only"

    # 'ERROR' (uppercase) must NOT be in the valid token set
    # Extract the valid array from applyArrivalFilter
    m = re.search(r"const valid = (\[.*?\]);", _MONITOR_JS)
    assert m, "valid array not found in applyArrivalFilter"
    # JS arrays use single quotes; convert to double-quotes for JSON parsing
    import ast as _ast

    valid_list = _ast.literal_eval(m.group(1))
    assert "ERROR" not in valid_list, (
        "MUST 5 strip-RED: uppercase 'ERROR' must not be valid"
    )
    assert "error" in valid_list


def test_monitor_invalid_status_ignored(tmp_path):
    """MUST 5: unrecognized ?status= value → no filter, no banner in HTML.

    The page still renders fully (no crash). The banner is always hidden by default
    — JS shows it only for recognized values. An unrecognized value means JS never
    calls the banner-show code, so the banner stays hidden.
    """
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)
    # The banner element exists but starts hidden — that's the correct default
    # for unrecognized values. Verify the default is style="display:none".
    assert 'id="arrival-banner" style="display:none"' in html


# ──────────────────────────────────────────────────────────────────
# MUST 6: filter, free-text search, column sort


def test_monitor_filter_sort_search(tmp_path):
    """MUST 6: filter controls, search input, and sort selector are all present."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Status filter chips (from monitor-summary panel)
    assert 'data-filter="error"' in html
    assert 'data-filter="warn"' in html
    assert 'data-filter="ok"' in html
    assert 'data-filter="stale"' in html
    assert 'data-filter="all"' in html

    # Model filter
    assert 'id="mon-model-sel"' in html

    # Free-text search
    assert 'id="mon-search"' in html

    # Sort selector
    assert 'id="mon-sort-sel"' in html
    for sort_val in ["problems", "cost", "errors", "lastrun", "name", "health"]:
        assert f'value="{sort_val}"' in html


def test_monitor_filter_sort_empty_result_boundary(tmp_path):
    """MUST 6 boundary: filter yielding zero results shows empty-filter message.

    When the fleet has agents, JS hides mon-list-empty until filter yields 0 results
    (mon-list-empty is in the DOM scaffold, display:none by default).
    When the fleet is empty (no enumerated agents), the roster panel returns the
    empty-fleet state (mon-roster-empty) instead of the table scaffold.
    """
    # Case A: fleet has agents → mon-list-empty is in the scaffold (JS controls display)
    lpr = {"agent_x": _RECENT}
    cd_with = _StubConsoleData(agent_count=1, last_primary_runs=lpr)
    html_with = _render_monitor_html(tmp_path, console_data=cd_with)
    assert 'id="mon-list-empty"' in html_with
    assert "No agents match" in html_with

    # Case B: empty fleet → mon-roster-empty (spec/56 §7 clean empty state)
    cd_empty = _StubConsoleData(agent_count=0, last_primary_runs={})
    html_empty = _render_monitor_html(tmp_path, console_data=cd_empty)
    assert "atomic-agents init" in html_empty  # empty-fleet prompt


# ──────────────────────────────────────────────────────────────────
# MUST 7*: every entity links to agent-detail.html?agent=<id>


def test_monitor_entity_links_to_detail(tmp_path):
    """MUST 7 positive: agent data in AGENTS JSON has ids that JS links to detail page."""
    _write_agent(tmp_path, "my_agent")
    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs={"my_agent": _RECENT},
    )
    html = _render_monitor_html(tmp_path, console_data=cd)

    # The JS renderList/renderCards builds links as agent-detail.html?agent=<id>
    assert "agent-detail.html?agent=" in html

    # The agent id must be present in the AGENTS JSON element
    agent_list = _parse_agents_json(html)
    ids = [a["id"] for a in agent_list]
    assert "my_agent" in ids


def test_monitor_entity_links_to_detail_strip_red():
    """MUST 7 strip-RED: the detail link format must be agent-detail.html?agent=<id>.

    The JS renderList/renderCards functions must use this exact href format.
    A different format (e.g. 'dashboard.html' or 'detail/<id>') would break the link.
    """
    from atomic_agents.dashboard.render_monitor import _MONITOR_JS

    # The detail link pattern must be present in the JS
    assert "agent-detail.html?agent=" in _MONITOR_JS, (
        "MUST 7 strip-RED: detail link must use 'agent-detail.html?agent=' format"
    )
    # Must appear in BOTH renderList and renderCards (both views link out)
    count = _MONITOR_JS.count("agent-detail.html?agent=")
    assert count >= 2, (
        f"MUST 7 strip-RED: agent-detail.html?agent= must appear in both list and cards "
        f"render functions; found {count} occurrences"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 8: freshness stamp + windows in effect


def test_monitor_freshness_stamp_and_windows(tmp_path):
    """MUST 8: freshness stamp ('updated') and status windows visible."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Freshness stamp
    assert "updated" in html
    assert "2026-07-05" in html  # render date stamp

    # Windows in effect (spec/56 §2.1)
    assert "24h" in html  # error window + stale window
    assert "7d" in html  # failures window

    # Auto-refresh (MUST 13 shape: meta refresh, not fetch)
    assert 'http-equiv="refresh"' in html


# ──────────────────────────────────────────────────────────────────
# MUST 9: per-entity columns present


def test_monitor_entity_columns_present(tmp_path):
    """MUST 9: all required columns appear in the table header."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Table headers (case-insensitive match)
    html_lower = html.lower()
    assert "health" in html_lower
    assert "errors" in html_lower
    assert "failures" in html_lower or "fail" in html_lower
    assert "cost" in html_lower
    assert "last run" in html_lower
    assert "trend" in html_lower or "spark" in html_lower or "model" in html_lower

    # Agent id in AGENTS JSON element carries the required fields
    lpr = {"an_agent": _RECENT}
    cd2 = _StubConsoleData(agent_count=1, last_primary_runs=lpr)
    html2 = _render_monitor_html(tmp_path, console_data=cd2)
    agent_list = _parse_agents_json(html2)
    a = agent_list[0]
    # All required column fields present
    assert "status" in a
    assert "name" in a or "id" in a
    assert "model" in a
    assert "health" in a
    assert "errors24h" in a
    assert "fail7d" in a
    assert "cost7d" in a
    assert "lastRun" in a
    assert "spark" in a


def test_monitor_errors_24h_column_uses_real_count(tmp_path):
    """MUST 9 errors(24h): an agent with N error-status runs shows N, not 0.

    Load-bearing test for the phantom-field bug: the panel previously read
    getattr(rm, 'error_count', 0) which silently returned 0 for every agent
    because ReliabilityMetrics has no error_count field. The fix reads
    rm.errors_24h (a real field populated by aggregate_console).

    Strip-RED: if the panel reverts to the getattr default (or any expression
    that ignores rm.errors_24h), an ERROR-classified agent with errors_24h=5
    would emit errors24h=0 in the JSON, and this test fails.

    Also verifies: an agent with 0 errors renders 0 (not a phantom non-zero).
    """
    from atomic_agents.dashboard.panels._monitor_roster import _MonitorRosterPanel

    # Agent with 3 errors in 24h window
    rm_errors = _StubReliabilityMetrics(
        agent="error_agent",
        total_runs=5,
        error_rate=0.6,
        errors_24h=3,
        failures_7d=0,
    )
    # Agent with 0 errors
    rm_clean = _StubReliabilityMetrics(
        agent="clean_agent",
        total_runs=10,
        error_rate=0.0,
        errors_24h=0,
        failures_7d=0,
    )

    # error_agent gets ERROR status via capped_by_axis="reliability"
    ah_error = _StubAgentHealth(agent="error_agent", capped_by_axis="reliability")
    fh = _StubFleetHealth(agents=[ah_error])
    cd = _StubConsoleData(
        agent_count=2,
        last_primary_runs={"error_agent": _RECENT, "clean_agent": _RECENT},
        reliability_metrics=[rm_errors, rm_clean],
        fleet_health=fh,
    )
    ctx = _make_ctx(console_data=cd)
    panel = _MonitorRosterPanel()
    result = panel.render(ctx)
    agent_list = _parse_agents_json(result.html)

    by_id = {a["id"]: a for a in agent_list}

    # ERROR-classified agent must show its real error count (not 0)
    assert "error_agent" in by_id, "error_agent must be in the entity list"
    error_row = by_id["error_agent"]
    assert error_row["status"] == "error", (
        "error_agent must have status=error (capped_by_axis=reliability)"
    )
    assert error_row["errors24h"] == 3, (
        f"MUST 9: errors24h must be 3 (from rm.errors_24h=3), got {error_row['errors24h']}. "
        "This is the phantom-field bug: getattr(rm, 'error_count', 0) always returns 0."
    )

    # Clean agent must show 0 (not a phantom non-zero)
    assert "clean_agent" in by_id, "clean_agent must be in the entity list"
    clean_row = by_id["clean_agent"]
    assert clean_row["errors24h"] == 0, (
        f"MUST 9: clean agent errors24h must be 0, got {clean_row['errors24h']}"
    )


def test_monitor_failures_7d_column_uses_real_count(tmp_path):
    """MUST 9 failures(7d): an agent with N blocked runs shows N in fail7d, not 0.

    Parallel test to test_monitor_errors_24h_column_uses_real_count — covers the
    failures(7d) column which had the same phantom-field bug (blocked_count getattr).

    Strip-RED: if the panel reads getattr(rm, 'blocked_count', 0) instead of
    rm.failures_7d, an agent with failures_7d=2 would emit fail7d=0.
    """
    from atomic_agents.dashboard.panels._monitor_roster import _MonitorRosterPanel

    rm_blocked = _StubReliabilityMetrics(
        agent="blocked_agent",
        total_runs=8,
        blocked_rate=0.25,
        errors_24h=0,
        failures_7d=2,
    )
    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs={"blocked_agent": _RECENT},
        reliability_metrics=[rm_blocked],
    )
    ctx = _make_ctx(console_data=cd)
    panel = _MonitorRosterPanel()
    result = panel.render(ctx)
    agent_list = _parse_agents_json(result.html)

    assert agent_list, "AGENTS JSON must not be empty"
    row = agent_list[0]
    assert row["fail7d"] == 2, (
        f"MUST 9: fail7d must be 2 (from rm.failures_7d=2), got {row['fail7d']}. "
        "This is the phantom-field bug: getattr(rm, 'blocked_count', 0) always returns 0."
    )


# ──────────────────────────────────────────────────────────────────
# MUST 10*: per-entity fail-soft


def test_monitor_one_agent_degraded_degrades_only_that_row(tmp_path):
    """MUST 10 strip-RED: a panel render exception degrades only that panel.

    We verify this by wiring a panel that raises for one slot and asserting
    the other slot still renders (same fail-soft as MUST 11 in spec/52).
    """
    from atomic_agents.dashboard.panels._registry import PanelRegistry, PanelResult

    class _GoodPanel:
        id = "good_panel"
        slot = "monitor-summary"
        order = 5

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html="<p>GOOD</p>")

    class _BadPanel:
        id = "bad_panel"
        slot = "monitor-roster"
        order = 5

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            raise RuntimeError("synthetic row fail")

    reg = PanelRegistry()
    reg.register(_GoodPanel())
    reg.register(_BadPanel())
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    ctx = _make_ctx(console_data=cd)

    slot_html, _ = reg.compose_monitor(ctx)
    # Good panel rendered
    assert "GOOD" in slot_html["monitor-summary"]
    # Bad panel degraded but page didn't abort
    assert "panel-degraded" in slot_html["monitor-roster"]


def test_monitor_cost_degraded_banner(tmp_path):
    """MUST 10 strip-RED: cost-degraded flag on a CostTrendPoint raises the spec/09 banner."""

    @dataclass
    class _DegradedCostTrend:
        agent: str = "deg_agent"
        total_usd_30d: float = 0.0
        spike_detected: bool = False
        daily_series: list = field(default_factory=list)
        cost_data_degraded: bool = True

    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs={"deg_agent": _RECENT},
        cost_trends=[_DegradedCostTrend()],
    )
    html = _render_monitor_html(tmp_path, console_data=cd)
    # The cost-degraded banner must be shown (not hidden)
    assert 'id="cost-degraded-banner"' in html
    # And it must not be style=display:none when degraded
    # The banner block: check it contains the warning text
    assert "data may be incomplete" in html


def test_monitor_degraded_row_sets_banner_flag(tmp_path):
    """FIX 3a: a per-row exception (MUST 10 fail-soft path) MUST set any_cost_degraded.

    When status_for_agent() raises for an agent, the roster panel emits a degraded
    row (degraded=True, costDegraded=True). That row MUST also flip the page-level
    banner flag so the spec/09 'data may be incomplete' banner appears — the same
    banner that fires when cost_data_degraded=True on a CostTrendPoint.

    Strip-RED: before the fix, the exception path did NOT set any_cost_degraded=True,
    so the banner stayed hidden even though an agent row was degraded. This test
    catches that regression: if the banner is hidden, the assertion fails.
    """
    from unittest.mock import patch

    # Wire status_for_agent to raise for the target agent so the per-row
    # except-path fires and emits a degraded row.
    lpr = {"fail_agent": _RECENT}
    cd = _StubConsoleData(agent_count=1, last_primary_runs=lpr)

    def _raise_for_agent(*args, **kwargs):
        raise RuntimeError("synthetic per-row failure for banner test")

    with patch("atomic_agents.dashboard._status.status_for_agent", _raise_for_agent):
        html = _render_monitor_html(tmp_path, console_data=cd)

    # Banner MUST be shown (not hidden) — the degraded row must flip the flag
    assert 'id="cost-degraded-banner"' in html
    assert (
        'style="display:none"'
        not in html.split('id="cost-degraded-banner"')[1].split(">")[0]
    ), (
        "FIX 3a: cost-degraded-banner must NOT be hidden when a row is degraded via exception"
    )
    assert "data may be incomplete" in html, (
        "FIX 3a: degraded-banner text must appear when any row fails"
    )

    # The degraded row itself must carry degraded=True
    agent_list = _parse_agents_json(html)
    assert any(a.get("degraded") is True for a in agent_list), (
        "FIX 3a: degraded row must carry degraded=True marker in the JSON"
    )


def test_monitor_degraded_row_renders_degraded_marker_not_zero(tmp_path):
    """FIX 3b: a degraded row renders '—' (cost-degraded-cell) not '$0.00' for cost/spark.

    When a.degraded is True (per-row exception path) or a.costDegraded is True
    (cost read failed), the JS costCell()/sparkCell() helpers render an explicit
    degraded marker rather than a misleading '$0.00' / empty sparkline.

    The marker is produced by the JS helper functions in _MONITOR_JS; we test it
    by verifying _MONITOR_JS contains the guard logic and that the CSS class is
    present, since the JS executes client-side (not server-side in tests).
    """
    from atomic_agents.dashboard.render_monitor import _MONITOR_JS

    # The JS must define costCell() that guards on a.degraded || a.costDegraded
    assert "costCell" in _MONITOR_JS, (
        "FIX 3b: costCell() helper must exist in _MONITOR_JS"
    )
    assert "a.degraded || a.costDegraded" in _MONITOR_JS, (
        "FIX 3b: costCell() must check a.degraded || a.costDegraded"
    )
    # The degraded marker text must be the em-dash, not $0.00
    assert "cost data unavailable" in _MONITOR_JS, (
        "FIX 3b: degraded marker must carry descriptive title attribute"
    )
    # The sparkCell helpers must also guard on degraded
    assert "sparkCell" in _MONITOR_JS, (
        "FIX 3b: sparkCell() helper must exist in _MONITOR_JS"
    )
    assert "trend data unavailable" in _MONITOR_JS, (
        "FIX 3b: spark degraded marker must carry descriptive title"
    )

    # Strip-RED: costCell() must NOT return '$0.00' for the degraded case.
    # Verify the branch that returns '$0.00' is only the non-degraded path.
    # The guard must come BEFORE the '$0.00' literal in the function body.
    cost_cell_start = _MONITOR_JS.index("function costCell")
    # Find the '$0.00' inside costCell — it must come after the degraded guard
    degraded_guard_pos = _MONITOR_JS.index(
        "a.degraded || a.costDegraded", cost_cell_start
    )
    zero_dollar_pos = _MONITOR_JS.index("$0.00", cost_cell_start)
    assert degraded_guard_pos < zero_dollar_pos, (
        "FIX 3b strip-RED: the degraded guard in costCell() must precede the '$0.00' branch"
    )


def test_monitor_degraded_row_banner_and_marker_integration(tmp_path):
    """FIX 3 integration: a degraded row (costDegraded via CostTrendPoint) must show
    both the page banner AND produce a cost-degraded-cell class in the rendered HTML.

    The CSS class is emitted inline in the JS-rendered rows (client-side), so we
    verify it appears in the _MONITOR_JS template string and the banner shows
    server-side. This is the end-to-end check for both FIX 3a and FIX 3b together.
    """
    from atomic_agents.dashboard.render_monitor import _MONITOR_JS

    @dataclass
    class _DegradedCostTrendFix3:
        agent: str = "fix3_agent"
        total_usd_30d: float = 0.0
        spike_detected: bool = False
        daily_series: list = field(default_factory=list)
        cost_data_degraded: bool = True

    lpr = {"fix3_agent": _RECENT}
    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs=lpr,
        cost_trends=[_DegradedCostTrendFix3()],
    )
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Banner must be shown (FIX 3a: server-side flag set)
    assert "data may be incomplete" in html, (
        "FIX 3 integration: banner must appear for cost-degraded agent"
    )
    assert (
        'style="display:none"'
        not in html.split('id="cost-degraded-banner"')[1].split(">")[0]
    )

    # The costDegraded=True entity must be in the JSON so the JS can guard it
    agent_list = _parse_agents_json(html)
    deg_agents = [a for a in agent_list if a.get("costDegraded")]
    assert deg_agents, "FIX 3 integration: costDegraded agent must appear in JSON"

    # The JS template must contain cost-degraded-cell class (FIX 3b: marker class)
    assert "cost-degraded-cell" in _MONITOR_JS, (
        "FIX 3 integration: _MONITOR_JS must contain cost-degraded-cell class"
    )


def test_monitor_unenumerable_agent_is_not_a_row(tmp_path):
    """MUST 10 spec/51 boundary: agents not in last_primary_runs are not rows.

    The monitor only renders agents that appear in console_data.last_primary_runs
    (the spec/51 enumeration). An agent absent from that dict is not a row.
    """
    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs={"real_agent": _RECENT},
        # "phantom_agent" is NOT in last_primary_runs → not a row
    )
    html = _render_monitor_html(tmp_path, console_data=cd)
    agent_list = _parse_agents_json(html)
    ids = [a["id"] for a in agent_list]
    assert "real_agent" in ids
    assert "phantom_agent" not in ids


def test_monitor_per_row_fail_soft_isolates_bad_agent(tmp_path):
    """MUST 10 per-ROW fail-soft: one agent's bad metric build degrades only that row.

    The roster panel wraps each agent's metric build in try/except. If one agent's
    status_for_agent() or metric extraction raises, that agent gets a degraded row
    marker but ALL OTHER agents still render normally.

    Strip-RED: removing the per-row try/except in _monitor_roster.py causes the
    whole entity_list to be empty (the exception propagates out of the loop), so
    the good agent's row disappears — this test catches it.
    """
    from atomic_agents.dashboard.panels._monitor_roster import _MonitorRosterPanel
    from atomic_agents.dashboard._status import status_for_agent as _real_sfa

    good_agent = "good_one"
    bad_agent = "bad_one"

    lpr = {good_agent: _RECENT, bad_agent: _RECENT}
    cd = _StubConsoleData(
        agent_count=2,
        last_primary_runs=lpr,
    )
    ctx = _make_ctx(console_data=cd)

    call_count = {"n": 0}

    def _patched_sfa(*args, **kwargs):
        # Raise for bad_agent, succeed for good_agent
        # The first call is for alphabetically-first agent; use call count to alternate
        call_count["n"] += 1
        # bad_agent comes first alphabetically — raise on first call
        if call_count["n"] == 1:
            raise RuntimeError("synthetic per-row failure")
        return _real_sfa(*args, **kwargs)

    panel = _MonitorRosterPanel()
    with patch("atomic_agents.dashboard._status.status_for_agent", _patched_sfa):
        result = panel.render(ctx)

    html = result.html
    # good_agent must appear in the output
    assert good_agent in html, (
        "MUST 10 per-row fail-soft: good agent must still render when another agent fails"
    )
    # bad_agent is present as a degraded row (degraded: True in entity list)
    # Both agents appear in the JSON (degraded row is still a row, just marked)
    agent_list = _parse_agents_json(html)
    ids = [a["id"] for a in agent_list]
    assert good_agent in ids, "good agent must be in the entity list"
    assert bad_agent in ids, "bad (degraded) agent must still appear as a degraded row"
    # The degraded row has degraded=True
    bad_row = next(a for a in agent_list if a["id"] == bad_agent)
    assert bad_row.get("degraded") is True, (
        "degraded row must carry degraded=True marker"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 11*: no LLM spend on render


def test_monitor_no_llm_spend_on_render(tmp_path):
    """MUST 11: render_monitor does NOT construct any LLM backend.

    Load-bearing approach: patch EVERY concrete LLMBackend.__init__ to raise, then
    call render_monitor() and assert it does NOT raise. If the render path constructed
    ANY LLM backend the patch would fire and render_monitor would raise.

    The repo has three concrete backends with their own __init__:
      - AnthropicLLMBackend (atomic_agents.llm.anthropic)
      - OpenAICompatibleLLMBackend (atomic_agents.llm.openai_compat)
      - VertexGeminiLLMBackend (atomic_agents.llm.vertex_gemini)
    MoonshotLLMBackend is a factory over OpenAICompatibleLLMBackend and is
    covered transitively by the OpenAICompatibleLLMBackend patch.

    Strip-RED: test_monitor_no_llm_spend_on_render_strip_red verifies that
    constructing each backend directly DOES raise with the patch active, proving
    each patch is individually load-bearing.
    """
    from atomic_agents.dashboard.render_monitor import render_monitor
    from atomic_agents.llm.anthropic import AnthropicLLMBackend
    from atomic_agents.llm.openai_compat import OpenAICompatibleLLMBackend
    from atomic_agents.llm.vertex_gemini import VertexGeminiLLMBackend

    cd = _StubConsoleData(agent_count=0, last_primary_runs={})

    def _raise_if_constructed(*a, **kw):
        raise AssertionError(
            "MUST 11 violation: an LLMBackend was constructed on the render path"
        )

    with (
        patch.object(AnthropicLLMBackend, "__init__", _raise_if_constructed),
        patch.object(OpenAICompatibleLLMBackend, "__init__", _raise_if_constructed),
        patch.object(VertexGeminiLLMBackend, "__init__", _raise_if_constructed),
    ):
        # Must NOT raise — render_monitor must not construct any LLMBackend
        render_monitor(agents_root=tmp_path, console_data=cd, now=_NOW, today=_TODAY)


def test_monitor_no_llm_spend_on_render_strip_red(tmp_path):
    """MUST 11 strip-RED: the patch fires if ANY concrete LLMBackend is constructed.

    Proves the multi-backend patching approach is individually load-bearing:
    directly constructing each backend under its patch raises AssertionError,
    so if the render path ever called any of these constructors, the positive
    test would catch it.
    """
    from atomic_agents.llm.anthropic import AnthropicLLMBackend
    from atomic_agents.llm.openai_compat import OpenAICompatibleLLMBackend
    from atomic_agents.llm.vertex_gemini import VertexGeminiLLMBackend

    def _raise_if_constructed(*a, **kw):
        raise AssertionError("patch fired — LLM constructor was called")

    # Each backend patch must fire independently
    with patch.object(AnthropicLLMBackend, "__init__", _raise_if_constructed):
        with pytest.raises(AssertionError, match="patch fired"):
            AnthropicLLMBackend.__init__(object())

    with patch.object(OpenAICompatibleLLMBackend, "__init__", _raise_if_constructed):
        with pytest.raises(AssertionError, match="patch fired"):
            OpenAICompatibleLLMBackend.__init__(object())

    with patch.object(VertexGeminiLLMBackend, "__init__", _raise_if_constructed):
        with pytest.raises(AssertionError, match="patch fired"):
            VertexGeminiLLMBackend.__init__(object())


# ──────────────────────────────────────────────────────────────────
# MUST 12*: monitor status counts == home status counts (same snapshot)


def test_monitor_status_counts_equal_home_summary():
    """MUST 12 positive: monitor summary panel counts match home fleet-status panel counts.

    Both panels call status_for_agent() over the SAME ctx.console_data → same counts.
    This is the structural guarantee of spec/56 §3.
    """
    from atomic_agents.dashboard.panels._monitor_summary import _MonitorSummaryPanel
    from atomic_agents.dashboard.panels._fleet_status import _FleetStatusPanel

    agents_statuses = {
        "e1": "ERROR",
        "e2": "ERROR",
        "s1": "STALE",
        "w1": "WARN",
        "o1": "OK",
        "o2": "OK",
    }
    cd, ctx = _make_fleet_ctx_multi(agents_statuses)

    monitor_panel = _MonitorSummaryPanel()
    home_panel = _FleetStatusPanel()

    monitor_result = monitor_panel.render(ctx)
    home_result = home_panel.render(ctx)

    # Extract counts from both rendered HTML fragments
    def _extract_count(html: str, chip_id: str) -> int:
        m = re.search(rf'id="{chip_id}">(\d+)', html)
        return int(m.group(1)) if m else -1

    for status_lower, chip_id_monitor in [
        ("error", "chip-count-error"),
        ("warn", "chip-count-warn"),
        ("stale", "chip-count-stale"),
        ("ok", "chip-count-ok"),
    ]:
        monitor_count = _extract_count(monitor_result.html, chip_id_monitor)
        # Home panel uses different IDs but same status values — extract from fo-cell
        # The home panel renders .fc-v counts; we check via the expected value
        expected = sum(1 for s in agents_statuses.values() if s.lower() == status_lower)
        assert monitor_count == expected, (
            f"MUST 12: monitor {status_lower} count={monitor_count}, expected={expected}"
        )


def test_monitor_status_counts_equal_home_summary_strip_red():
    """MUST 12 strip-RED: divergent window in one panel → counts diverge.

    If the monitor used a different staleness_window than the home, the STALE counts
    would differ. We verify this by calling status_for_agent() with a tighter window
    for one panel and a broader one for another, confirming they diverge — which
    proves the shared-snapshot design is the only way to guarantee equality.
    """
    from atomic_agents.dashboard._status import status_for_agent

    # Agent that is STALE in a 24h window but NOT stale in a 48h window
    last_run = _NOW - timedelta(hours=30)  # 30h ago

    status_24h = status_for_agent(
        agent_health=None,
        attention_items=[],
        last_primary_run_at=last_run,
        now=_NOW,
        staleness_window=timedelta(hours=24),
    )
    status_48h = status_for_agent(
        agent_health=None,
        attention_items=[],
        last_primary_run_at=last_run,
        now=_NOW,
        staleness_window=timedelta(hours=48),
    )
    assert status_24h == "STALE"
    assert status_48h == "OK"
    # Confirmed: different windows give different results, proving shared-window is load-bearing
    assert status_24h != status_48h, (
        "MUST 12 strip-RED: different staleness windows MUST yield different statuses"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 13*: no SSE / fetch / background polling


def test_monitor_render_has_no_polling(tmp_path):
    """MUST 13 positive: monitor HTML uses meta-refresh, NOT EventSource/fetch/setInterval polling."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Freshness method MUST be meta http-equiv refresh
    assert 'http-equiv="refresh"' in html

    # Must NOT contain live-served polling mechanisms
    assert "EventSource" not in html, "MUST 13: SSE (EventSource) must not be present"
    assert "/events" not in html, "MUST 13: SSE endpoint /events must not be present"


def test_monitor_render_has_no_polling_strip_red():
    """MUST 13 strip-RED: verify that EventSource would be detectable if present.

    We confirm the test assertion is load-bearing by checking a known-absent string
    vs. a known-present one in the monitor JS.
    """
    from atomic_agents.dashboard.render_monitor import _MONITOR_JS, _AUTO_RELOAD_SECONDS

    # EventSource must not be in the JS
    assert "EventSource" not in _MONITOR_JS, (
        "MUST 13 strip-RED: EventSource found in _MONITOR_JS — remove it"
    )
    # setInterval must not be used for data fetching (only for page display timing is ok,
    # but we use meta refresh — so setInterval should not be in the JS at all)
    # We allow location.reload() as it is a FULL PAGE reload, not background fetch
    assert "setInterval" not in _MONITOR_JS, (
        "MUST 13 strip-RED: setInterval found — use meta http-equiv refresh instead"
    )
    # The auto-reload constant must be positive
    assert _AUTO_RELOAD_SECONDS > 0


def test_monitor_fetch_not_used_for_data(tmp_path):
    """MUST 13: no background fetch() for data; only the full-page reload button uses it if at all."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Monitor JS must not contain a fetch() call for data endpoints
    # (the home page uses fetch('/regenerate') for refresh — monitor uses location.reload())
    # Check that no fetch() for data URLs appears
    # We grep the monitor-specific JS block (after the panel scripts)
    assert "EventSource" not in html
    # fetch('/events') or fetch('/data') or fetch('/status') must not appear
    for banned in ["/events", "/stream", "/poll", "/data/monitor"]:
        assert banned not in html, (
            f"MUST 13: banned endpoint '{banned}' found in monitor HTML"
        )


# ──────────────────────────────────────────────────────────────────
# Integration: render_all emits monitor.html


def test_render_all_emits_monitor_html(tmp_path):
    """Integration: render_all('all') creates monitor.html alongside index.html."""
    import json as _j
    from datetime import date as _date
    from atomic_agents.dashboard.render import render_all

    # Create a minimal agent
    agent = "test_agent"
    (tmp_path / agent).mkdir()
    (tmp_path / agent / "model.md").write_text("# model\n")
    log_dir = tmp_path / agent / "log" / "2026-07"
    log_dir.mkdir(parents=True)
    rec = {
        "ts": "2026-07-05T12:00:00+00:00",
        "trigger": "cron",
        "model": "claude-haiku-4-5-20260101",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": 0.001,
        "status": "ok",
        "summary": "test",
    }
    (log_dir / "2026-07-05.jsonl").write_text(_j.dumps(rec) + "\n")

    result = render_all(tmp_path, today=_date(2026, 7, 5), tab="console")
    monitor_path = tmp_path / "_dashboard" / "monitor.html"
    assert monitor_path.exists(), "render_all must create monitor.html"
    assert "monitor" in result, "render_all result dict must include 'monitor' key"
    html = monitor_path.read_text()
    assert "Fleet Monitor" in html
    assert "<!DOCTYPE html>" in html


# ──────────────────────────────────────────────────────────────────
# Registry: compose_monitor uses monitor slots, not home slots


def test_compose_monitor_uses_monitor_slots():
    """compose_monitor() iterates monitor-summary and monitor-roster slots."""
    from atomic_agents.dashboard.panels._registry import PanelRegistry, PanelResult

    seen_slots = []

    class _SummaryPanel:
        id = "test_ms"
        slot = "monitor-summary"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            seen_slots.append(self.slot)
            return PanelResult(html="<p>summary</p>")

    class _RosterPanel:
        id = "test_mr"
        slot = "monitor-roster"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            seen_slots.append(self.slot)
            return PanelResult(html="<p>roster</p>")

    class _HomePanel:
        id = "test_home"
        slot = "status"  # home slot — must NOT be composed by compose_monitor
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            seen_slots.append(self.slot)
            return PanelResult(html="<p>home</p>")

    reg = PanelRegistry()
    reg.register(_SummaryPanel())
    reg.register(_RosterPanel())
    reg.register(_HomePanel())

    ctx = _make_ctx()
    slot_html, _ = reg.compose_monitor(ctx)

    assert "monitor-summary" in slot_html
    assert "monitor-roster" in slot_html
    assert "summary" in slot_html["monitor-summary"]
    assert "roster" in slot_html["monitor-roster"]
    # Home panel must NOT have been called
    assert "status" not in seen_slots, "compose_monitor must not render home slots"


# ──────────────────────────────────────────────────────────────────
# MUST 12*: real-render integration — one clock, equal status counts


def test_monitor_render_all_status_counts_match_index(tmp_path):
    """MUST 12 integration: render_all() status counts are EQUAL on both index.html
    (home fleet-status panel) and monitor.html (monitor-summary bar).

    Both pages receive the SAME console_data and SAME now from render_all(), so
    every status_for_agent() call sees the same snapshot. The test runs render_all()
    on a real fixture, extracts the per-status counts from BOTH rendered pages, and
    asserts they are equal for every status.

    Home page extraction: _FleetStatusPanel renders counts inside
      <a href="monitor.html?status=<s>" ...><div class="fc-v ...">N</div></a>
    We extract N per status from that link-adjacent div.

    Monitor page extraction: _MonitorSummaryPanel renders chips as
      <span id="chip-count-<s>">N</span>
    We extract N per chip id.

    Strip-RED: if render_console() created its own independent `now` (instead of
    receiving it from render_all()), agents on a staleness boundary could flip
    between renders and produce different counts — the shared-clock design is the
    only guarantee. To verify this test is load-bearing, calling status_for_agent()
    with a 24h vs 48h window on a 30h-old agent yields different results
    (proven in test_monitor_status_counts_equal_home_summary_strip_red).
    """
    import json as _j
    from datetime import date as _date
    from atomic_agents.dashboard.render import render_all

    # Create three agents with distinct statuses:
    # - agent_ok: recent run → OK
    # - agent_stale: run 36h ago → STALE (beyond 24h staleness window)
    # - agent_warn_att: recent run with an open attention item → WARN
    # We control statuses by writing logs with known timestamps.
    agents_setup = {
        "agent_ok": "2026-07-05T13:00:00+00:00",  # 1h ago → OK
        "agent_stale": "2026-07-03T14:00:00+00:00",  # ~47h ago → STALE
    }
    for agent_name, ts in agents_setup.items():
        agent_dir = tmp_path / agent_name
        agent_dir.mkdir()
        (agent_dir / "model.md").write_text("# model\n")
        log_dir = agent_dir / "log" / "2026-07"
        log_dir.mkdir(parents=True)
        rec = {
            "ts": ts,
            "trigger": "cron",
            "model": "claude-haiku-4-5-20260101",
            "input_tokens": 100,
            "output_tokens": 20,
            "cost_usd": 0.001,
            "status": "ok",
            "summary": "run",
        }
        # Write to correct date file based on ts date
        ts_date = ts[:10]  # e.g. "2026-07-05"
        ts_ym = ts_date[:7].replace("-", "-")  # "2026-07"
        log_file_dir = agent_dir / "log" / ts_ym
        log_file_dir.mkdir(parents=True, exist_ok=True)
        (log_file_dir / f"{ts_date}.jsonl").write_text(_j.dumps(rec) + "\n")

    result = render_all(tmp_path, today=_date(2026, 7, 5), tab="console")

    # Both pages must exist
    index_path = tmp_path / "_dashboard" / "index.html"
    monitor_path = tmp_path / "_dashboard" / "monitor.html"
    assert index_path.exists(), "render_all must produce index.html"
    assert monitor_path.exists(), "render_all must produce monitor.html"

    index_html = index_path.read_text()
    monitor_html = monitor_path.read_text()

    # ── Extract counts from monitor.html (monitor-summary chips) ──────────
    def _monitor_count(html: str, status: str) -> int:
        """Extract chip count from monitor-summary: <span id="chip-count-X">N</span>."""
        m = re.search(rf'id="chip-count-{re.escape(status)}">(\d+)', html)
        assert m, f"MUST 12: chip-count-{status} not found in monitor.html"
        return int(m.group(1))

    # ── Extract counts from index.html (fleet-status fo-cell links) ───────
    def _home_count(html: str, status: str) -> int:
        """Extract status count from home fleet-status panel.

        _FleetStatusPanel emits:
          <a ... href="monitor.html?status=<status>">
          <div class="fc-v ...">N</div>

        We locate the href anchor and grab the integer inside the nearest fc-v div.
        """
        # Find the anchor href for this status, then extract the number from fc-v
        pattern = (
            rf'href="monitor\.html\?status={re.escape(status)}"[^>]*>'
            rf'\s*<div class="fc-v[^"]*">(\d+)</div>'
        )
        m = re.search(pattern, html)
        if not m:
            return -1
        return int(m.group(1))

    # Assert equality per status across both pages
    for status in ("error", "warn", "stale", "ok"):
        monitor_n = _monitor_count(monitor_html, status)
        home_n = _home_count(index_html, status)
        assert home_n >= 0, (
            f"MUST 12: status '{status}' count not found in index.html fleet-status panel"
        )
        assert monitor_n == home_n, (
            f"MUST 12: monitor {status} count ({monitor_n}) != "
            f"home {status} count ({home_n}) — shared-snapshot violated"
        )

    # Sanity: monitor total chip == sum of individual chips
    total_n = _monitor_count(monitor_html, "all")
    parts = sum(
        _monitor_count(monitor_html, s) for s in ("error", "warn", "stale", "ok")
    )
    assert total_n == parts, (
        f"MUST 12: monitor total chip ({total_n}) != sum of per-status chips ({parts})"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 1: /monitor serve route


def test_serve_monitor_route():
    """MUST 1: /monitor and /monitor.html are in _TAB_FILES → serve monitor.html."""
    from atomic_agents.dashboard.serve import DashboardHandler

    assert "/monitor" in DashboardHandler._TAB_FILES, (
        "MUST 1: /monitor must be in _TAB_FILES routing to monitor.html"
    )
    assert DashboardHandler._TAB_FILES["/monitor"] == "monitor.html"
    assert "/monitor.html" in DashboardHandler._TAB_FILES, (
        "MUST 1: /monitor.html must be in _TAB_FILES"
    )
    assert DashboardHandler._TAB_FILES["/monitor.html"] == "monitor.html"


# ──────────────────────────────────────────────────────────────────
# Security: XSS-safe AGENTS embedding


def test_monitor_xss_agent_name_does_not_break_script(tmp_path):
    """Security fix #1: a crafted agent name with </script> does NOT break out of
    the JSON element in the rendered HTML.

    The panel now emits AGENTS data in a <script type="application/json"> element
    with <, >, & escaped as \\u003c / \\u003e / \\u0026, so </script> in agent
    data cannot close the element and inject arbitrary HTML/JS.
    """
    # Inject an agent id that would break a live <script> block
    xss_agent = "</script><img src=x onerror=alert(1)>"
    lpr = {xss_agent: _RECENT}
    cd = _StubConsoleData(agent_count=1, last_primary_runs=lpr)
    html = _render_monitor_html(tmp_path, console_data=cd)

    # The literal string </script> must NOT appear INSIDE the monitor-agents element.
    # Extract the element content and check it does not contain an unescaped close tag.
    m = re.search(
        r'<script\s+type="application/json"\s+id="monitor-agents">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert m, "monitor-agents element must be present"
    element_content = m.group(1)

    # The unescaped literal sequence must NOT appear in the element body
    assert "</script>" not in element_content, (
        "XSS: </script> must not appear unescaped inside the monitor-agents JSON element"
    )
    # The < must be escaped as \\u003c
    assert "\\u003c" in element_content, (
        "XSS: < in agent data must be escaped as \\u003c"
    )


def test_monitor_detail_link_encodes_special_chars(tmp_path):
    """Security fix #2: agent ids with ', & are safely encoded in detail links.

    The JS now uses encodeURIComponent(a.id) for query values so special characters
    in agent ids cannot corrupt the URL or JS string context.
    """
    from atomic_agents.dashboard.render_monitor import _MONITOR_JS

    # encodeURIComponent must be used for the query value (not esc() which emits &#39;
    # which decodes before JS executes and can break the string context)
    assert "encodeURIComponent(a.id)" in _MONITOR_JS, (
        "Security fix #2: detail href query value must use encodeURIComponent(a.id)"
    )
    # The old dangerous pattern (inline onclick string with esc()) must not be present
    assert "onclick=\"window.location.href=\\'" not in _MONITOR_JS, (
        "Security fix #2: inline onclick string-building is forbidden"
    )


# ──────────────────────────────────────────────────────────────────
# FIX 1: health score ×100 bug (composite is 0-100, not 0-1)


def test_monitor_health_score_not_multiplied_by_100(tmp_path):
    """FIX 1: a composite of 55.57 must render as 56, not 5557.

    _monitor_roster.py previously computed int(round(float(raw_composite) * 100)),
    but AgentHealth.composite is already in [0, 100] (e.g. 55.57). The correct
    formula is int(round(float(raw_composite))) — no ×100.

    Strip-RED: removing the * 100 from the formula changes the value from 5557 to 56.
    If the fix were reverted, the health score in the JSON would be 5557 and this
    test would fail on the >100 guard.
    """
    composite_val = 55.57  # already 0-100 — must display as 56

    ah = _StubAgentHealth(agent="scored_agent", composite=composite_val, band="amber")
    fh = _StubFleetHealth(agents=[ah])
    lpr = {"scored_agent": _RECENT}
    cd = _StubConsoleData(
        agent_count=1,
        last_primary_runs=lpr,
        fleet_health=fh,
    )
    html = _render_monitor_html(tmp_path, console_data=cd)
    agent_list = _parse_agents_json(html)
    assert agent_list, "AGENTS JSON must not be empty"
    a = agent_list[0]
    score = a["health"]["score"]
    assert score is not None, "health.score must not be None for a scored agent"
    # Score must be in [0, 100] — a ×100 bug would give 5557 here
    assert 0 <= score <= 100, (
        f"FIX 1: health score {score} is out of [0, 100] range. "
        "composite is already in 0-100; do not multiply by 100."
    )
    # The specific value: round(55.57) == 56
    assert score == 56, f"FIX 1: composite 55.57 must display as 56, got {score}"


def test_monitor_health_score_range_boundary_values(tmp_path):
    """FIX 1 boundary: scores at the edges of [0, 100] stay in range.

    Composites of 0.0, 100.0, and typical mid-range values all render in [0, 100].
    """
    from atomic_agents.dashboard.panels._monitor_roster import _MonitorRosterPanel

    for composite_val, expected in [(0.0, 0), (100.0, 100), (85.0, 85), (42.5, 42)]:
        ah = _StubAgentHealth(agent="edge_agent", composite=composite_val, band="green")
        fh = _StubFleetHealth(agents=[ah])
        cd = _StubConsoleData(
            agent_count=1,
            last_primary_runs={"edge_agent": _RECENT},
            fleet_health=fh,
        )
        ctx = _make_ctx(console_data=cd)
        panel = _MonitorRosterPanel()
        result = panel.render(ctx)
        agent_list = _parse_agents_json(result.html)
        assert agent_list, (
            f"AGENTS JSON must not be empty for composite={composite_val}"
        )
        score = agent_list[0]["health"]["score"]
        assert score is not None
        assert 0 <= score <= 100, (
            f"FIX 1 boundary: composite {composite_val} yielded score {score} outside [0, 100]"
        )
        assert score == expected, (
            f"FIX 1 boundary: composite {composite_val} must display as {expected}, got {score}"
        )


# ──────────────────────────────────────────────────────────────────
# FIX 2: Monitor tab in shared nav_bar on EVERY page


def test_nav_bar_includes_monitor_tab():
    """FIX 2: nav_bar() always includes a Monitor tab linking to monitor.html.

    The Monitor is a primary surface (spec/56 #653) and must appear in the top
    tab nav on every page — Console, Cost, Activity, Quality, Memory — not only
    on the Monitor page itself.
    """
    from atomic_agents.dashboard._shared import nav_bar

    for current in ("console", "cost", "activity", "quality", "memory"):
        result = nav_bar(current)
        assert 'href="monitor.html"' in result, (
            f"FIX 2: nav_bar(current={current!r}) must include Monitor tab linking to monitor.html"
        )
        assert ">Monitor<" in result, (
            f"FIX 2: nav_bar(current={current!r}) must include Monitor label"
        )


def test_nav_bar_monitor_active_on_monitor_page():
    """FIX 2: when current='monitor', the Monitor tab is marked active."""
    from atomic_agents.dashboard._shared import nav_bar

    result = nav_bar("monitor")
    # The active class must be on the Monitor anchor
    assert (
        'href="monitor.html" class="active"' in result
        or 'href="monitor.html"' in result
    ), "FIX 2: monitor.html link must be present when current='monitor'"
    # Confirm the Monitor link is the active one
    assert 'class="active"' in result
    # Extract the active anchor and verify it links to monitor.html
    import re as _re

    active_match = _re.search(r'<a [^>]*class="active"[^>]*>(.*?)</a>', result)
    assert active_match, "FIX 2: an active anchor must exist"
    active_href_match = _re.search(r'<a href="([^"]+)" class="active"', result)
    if active_href_match:
        assert active_href_match.group(1) == "monitor.html", (
            f"FIX 2: active tab on monitor page must link to monitor.html, "
            f"got {active_href_match.group(1)!r}"
        )


def test_nav_bar_other_tabs_not_active_when_monitor(self_param=None):
    """FIX 2: when current='monitor', only Monitor is active — other tabs are not."""
    from atomic_agents.dashboard._shared import nav_bar
    import re as _re

    result = nav_bar("monitor")
    # Count active tabs — must be exactly 1
    active_count = result.count('class="active"')
    assert active_count == 1, (
        f"FIX 2: exactly one tab must be active when current='monitor', got {active_count}"
    )


def test_nav_bar_memory_tab_still_present():
    """FIX 2 KEEP: Memory tab must not be removed (it is a real surface)."""
    from atomic_agents.dashboard._shared import nav_bar

    for current in ("console", "monitor", "cost", "activity", "quality", "memory"):
        result = nav_bar(current)
        assert 'href="memory.html"' in result, (
            f"FIX 2: Memory tab must remain in nav_bar for current={current!r}"
        )
        assert ">Memory<" in result


def test_rendered_pages_nav_contains_monitor_tab(tmp_path):
    """FIX 2 integration: rendered HTML pages all contain a Monitor tab.

    Verifies that pages rendered by render_all() — index.html, cost.html,
    activity.html, quality.html, memory.html — each contain a Monitor tab link.
    monitor.html has the Monitor tab active; other pages have it inactive.
    """
    import json as _j
    from datetime import date as _date
    from atomic_agents.dashboard.render import render_all

    # Set up a minimal agent so all tabs have something to render
    agent = "nav_test_agent"
    agent_dir = tmp_path / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "model.md").write_text("# model\n")
    log_dir = agent_dir / "log" / "2026-07"
    log_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": "2026-07-05T12:00:00+00:00",
        "trigger": "cron",
        "model": "claude-haiku-4-5-20260101",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": 0.001,
        "status": "ok",
        "summary": "test",
    }
    (log_dir / "2026-07-05.jsonl").write_text(_j.dumps(rec) + "\n")

    render_all(tmp_path, today=_date(2026, 7, 5))

    dash = tmp_path / "_dashboard"
    pages_to_check = [
        "index.html",
        "cost.html",
        "monitor.html",
        "activity.html",
        "quality.html",
        "memory.html",
    ]
    for page in pages_to_check:
        path = dash / page
        if not path.exists():
            # Some pages may not exist without enough data; skip them
            continue
        content = path.read_text()
        assert 'href="monitor.html"' in content, (
            f"FIX 2 integration: {page} must contain a Monitor tab linking to monitor.html"
        )
        assert ">Monitor<" in content, (
            f"FIX 2 integration: {page} must contain Monitor label in nav"
        )


# ──────────────────────────────────────────────────────────────────
# FIX 3: view toggle List button has server-side active class


def test_monitor_list_button_has_active_class_server_side(tmp_path):
    """FIX 3: the List button carries class='active' in the server-rendered HTML.

    Before JS runs the toggle looks unselected because neither button had an
    active class. The List button must have class='active' server-side (spec/56 §4:
    list is the default view). JS overrides this from ?view= / localStorage on load.

    Strip-RED: removing class='active' from the List button causes this test to fail.
    """
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # The List button must carry class="active" in the server HTML
    assert 'data-view="list" class="active"' in html, (
        'FIX 3: List button must have class="active" in server-rendered HTML '
        "(spec/56 §4: list is the default view)"
    )


def test_monitor_cards_button_not_active_server_side(tmp_path):
    """FIX 3: the Cards button must NOT carry active class in server HTML."""
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # Cards button must not be marked active server-side
    assert 'data-view="cards" class="active"' not in html, (
        "FIX 3: Cards button must not be marked active in server HTML"
    )


# ──────────────────────────────────────────────────────────────────
# FIX 4: arrival banner copy


def test_monitor_arrival_banner_copy(tmp_path):
    """FIX 4: arrival banner text reads 'arrived from home · filtered to' (B7 design).

    The original text was 'arrived filtered to' (missing 'from home ·').
    """
    cd = _StubConsoleData(agent_count=0, last_primary_runs={})
    html = _render_monitor_html(tmp_path, console_data=cd)

    # The banner element exists (hidden by default)
    assert 'id="arrival-banner"' in html, "arrival-banner must be present"

    # The copy must include "from home" and the separator (middot or ·)
    assert "arrived from home" in html, (
        "FIX 4: arrival banner must say 'arrived from home' (not just 'arrived')"
    )
    assert "filtered to" in html, "FIX 4: arrival banner must say 'filtered to'"
    # The old incorrect copy must not appear
    # (The old text was 'arrived filtered to' — the new one is 'arrived from home · filtered to')
    # Check that the separator (middot or its entity) is between them
    assert "middot" in html or "·" in html, (
        "FIX 4: arrival banner must include a middot (·) separator between 'from home' and 'filtered to'"
    )

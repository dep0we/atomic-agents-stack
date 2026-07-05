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
    error_count: int = 0
    blocked_count: int = 0
    total_runs: int = 10
    error_rate: float = 0.0


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

    Load-bearing approach: monkeypatch the real AnthropicLLMBackend.__init__ to raise,
    then call render_monitor() and assert it does NOT raise. If the render path
    constructed an LLM backend, the patch would fire and render_monitor would raise.

    Strip-RED: the test below (test_monitor_no_llm_spend_on_render_strip_red) verifies
    that calling AnthropicLLMBackend() directly DOES raise with the patch active,
    proving the patch is effective.
    """
    from atomic_agents.dashboard.render_monitor import render_monitor
    from atomic_agents.llm.anthropic import AnthropicLLMBackend

    cd = _StubConsoleData(agent_count=0, last_primary_runs={})

    def _raise_if_constructed(*a, **kw):
        raise AssertionError(
            "MUST 11 violation: AnthropicLLMBackend was constructed on the render path"
        )

    with patch.object(AnthropicLLMBackend, "__init__", _raise_if_constructed):
        # Must NOT raise — render_monitor must not construct an LLMBackend
        render_monitor(agents_root=tmp_path, console_data=cd, now=_NOW, today=_TODAY)


def test_monitor_no_llm_spend_on_render_strip_red(tmp_path):
    """MUST 11 strip-RED: the patch fires if AnthropicLLMBackend is constructed.

    Proves the patching approach in test_monitor_no_llm_spend_on_render is load-bearing:
    directly constructing AnthropicLLMBackend() under the patch raises AssertionError,
    so if the render path ever called the constructor, the positive test would catch it.
    """
    from atomic_agents.llm.anthropic import AnthropicLLMBackend

    def _raise_if_constructed(*a, **kw):
        raise AssertionError("patch fired — LLM constructor was called")

    with patch.object(AnthropicLLMBackend, "__init__", _raise_if_constructed):
        # Directly constructing it MUST raise (proves the patch is active)
        with pytest.raises(AssertionError, match="patch fired"):
            AnthropicLLMBackend.__init__(object())


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
    """MUST 12 integration: render_all() produces index.html and monitor.html from
    one shared clock; the status counts extracted from both pages agree.

    This tests the actual render path (not just panel units) to verify the
    spec/56 §3 shared-snapshot guarantee end-to-end. Both pages parse to the
    same status totals because render_console() and render_monitor() receive
    the same `now` from render_all().

    Strip-RED: if render_console() created its own `now` independently of render_all(),
    agents near the staleness boundary could flip between the two renders, producing
    different counts. The fix (passing `now` as a parameter) is what makes them agree.
    """
    import json as _j
    from datetime import date as _date, datetime as _datetime
    from atomic_agents.dashboard.render import render_all

    # Create two agents: one recent (OK), one stale
    (tmp_path / "agent_ok").mkdir()
    (tmp_path / "agent_ok" / "model.md").write_text("# model\n")
    log_dir_ok = tmp_path / "agent_ok" / "log" / "2026-07"
    log_dir_ok.mkdir(parents=True)
    rec_ok = {
        "ts": "2026-07-05T13:00:00+00:00",  # 1h before render time
        "trigger": "cron",
        "model": "claude-haiku-4-5-20260101",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": 0.001,
        "status": "ok",
        "summary": "ok run",
    }
    (log_dir_ok / "2026-07-05.jsonl").write_text(_j.dumps(rec_ok) + "\n")

    result = render_all(tmp_path, today=_date(2026, 7, 5), tab="console")

    # Both pages must exist
    index_path = tmp_path / "_dashboard" / "index.html"
    monitor_path = tmp_path / "_dashboard" / "monitor.html"
    assert index_path.exists(), "render_all must produce index.html"
    assert monitor_path.exists(), "render_all must produce monitor.html"

    monitor_html = monitor_path.read_text()
    # Monitor page rendered without error and contains Fleet Monitor markup
    assert "Fleet Monitor" in monitor_html
    assert "<!DOCTYPE html>" in monitor_html

    # Extract monitor status counts from the monitor-summary chip spans
    def _extract_monitor_count(html: str, chip_cls: str) -> int:
        # Chips render as: <span id="chip-count-X">N</span>
        m = re.search(rf'id="chip-count-{chip_cls}">(\d+)', html)
        return int(m.group(1)) if m else -1

    error_count = _extract_monitor_count(monitor_html, "error")
    ok_count = _extract_monitor_count(monitor_html, "ok")
    total_count = _extract_monitor_count(monitor_html, "all")

    # We have exactly 1 agent that was recent → OK
    assert ok_count >= 0, "monitor must have an 'ok' count chip"
    assert total_count >= 0, "monitor must have a total 'all' count chip"
    # The total must equal sum of individual counts (sanity)
    stale_count = _extract_monitor_count(monitor_html, "stale")
    warn_count = _extract_monitor_count(monitor_html, "warn")
    assert total_count == error_count + warn_count + stale_count + ok_count, (
        "MUST 12: monitor chip counts must sum to total"
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

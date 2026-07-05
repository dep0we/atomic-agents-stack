"""Fleet Monitor roster panel — monitor-roster slot (spec/56 §4, §6).

Renders the entity list + cards for the Fleet Monitor page. Each enumerated
agent becomes one monitored entity with status, health, cost, sparkline, etc.

Status derivation: uses the SHARED status_for_agent() (spec/52 §17.1, spec/56 §3).
The panel reads only from ctx.console_data (MUST 13 — no backend I/O at render time).
All JS for filtering/sorting/view-toggle/search is embedded in the page template
(render_monitor.py), not here. This panel emits the server-side agent data as a
JSON block and the DOM scaffold — client JS drives the interactive presentation.

Columns (MUST 9): status, name, model, health, errors(24h), failures(7d),
7d cost, last-run, sparkline.

Fail-soft (MUST 10): a missing/unreadable metric for an enumerated agent degrades
only that row — the degraded marker is shown in place of the affected column value.
"""

from __future__ import annotations

import html as _html
import json as _json
from datetime import timedelta, timezone

from ._registry import PanelContext, PanelResult, register

# Problems-first sort order for the client (matches spec/56 §4 + mockup JS).
_STATUS_SORT = {"error": 0, "warn": 1, "stale": 2, "ok": 3}

# Sparkline color by status
_SPARK_COLOR = {
    "error": "#e06c75",
    "warn": "#d19a66",
    "ok": "#4ec9b0",
    "stale": "#8a96a3",
}


def _model_pill_class(model: str) -> str:
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    if m.startswith("gpt"):
        return "gpt"
    if "kimi" in m or m.startswith("moonshot"):
        return "kimi"
    if m.startswith("local") or "qwen" in m or "llama" in m:
        return "local"
    return "opus"


def _short_model_name(model: str) -> str:
    if "opus" in model.lower():
        return "Opus"
    if "sonnet" in model.lower():
        return "Sonnet"
    if "haiku" in model.lower():
        return "Haiku"
    return model.split("/")[-1][:20]


def _relative_time(ts, now) -> str:
    """Format a datetime relative to now."""
    if ts is None:
        return "never"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    diff = now - ts
    secs = int(diff.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    return f"{days}d ago"


class _MonitorRosterPanel:
    id = "monitor_roster"
    slot = "monitor-roster"
    order = 10

    def is_available(self, ctx: PanelContext) -> bool:
        return True

    def render(self, ctx: PanelContext) -> PanelResult:
        from .._status import status_for_agent  # MUST 2: canonical shared function

        cd = ctx.console_data
        now = ctx.now

        # Build lookup structures from pre-loaded data (MUST 13 — no disk reads).
        agent_health_by_name: dict = {}
        fh = cd.fleet_health
        if fh is not None:
            for ah in getattr(fh, "agents", []):
                agent_health_by_name[ah.agent] = ah

        open_items_by_agent: dict[str, list] = {}
        for item in cd.attention_queue:
            if item.ack_snooze_status == "open":
                open_items_by_agent.setdefault(item.agent, []).append(item)

        spike_agents = {ct.agent for ct in cd.cost_trends if ct.spike_detected}

        # Cost / reliability metrics by agent.
        cost_by_agent: dict[str, float] = {}
        daily_series_by_agent: dict[str, list] = {}
        cost_degraded_by_agent: dict[str, bool] = {}
        for ct in cd.cost_trends:
            cost_by_agent[ct.agent] = getattr(ct, "total_usd_30d", 0.0)
            # 7-day slice of daily_series (sparse: missing days omitted, not zero-filled)
            daily_series_by_agent[ct.agent] = list(getattr(ct, "daily_series", []))[-7:]
            # degraded flag: if CostTrendPoint carries it
            cost_degraded_by_agent[ct.agent] = bool(
                getattr(ct, "cost_data_degraded", False)
            )

        errors_by_agent: dict[str, int] = {}
        failures_by_agent: dict[str, int] = {}
        for rm in cd.reliability_metrics:
            a = rm.agent if hasattr(rm, "agent") else getattr(rm, "agent_id", None)
            if a is None:
                continue
            errors_by_agent[a] = int(getattr(rm, "error_count", 0) or 0)
            failures_by_agent[a] = int(getattr(rm, "blocked_count", 0) or 0)

        # Build the entity list. Each agent in last_primary_runs is an enumerated entity.
        agents = sorted(cd.last_primary_runs.keys())
        if not agents:
            # Empty fleet — render clean empty state (spec/56 §7).
            html_out = (
                '<div class="mon-empty" id="mon-roster-empty">'
                "No agents found — add one with <code>atomic-agents init</code>"
                "</div>"
            )
            return PanelResult(html=html_out)

        # Build per-agent entity records for client JS.
        entity_list = []
        any_cost_degraded = False

        for agent in agents:
            ah = agent_health_by_name.get(agent)
            open_items = open_items_by_agent.get(agent, [])
            lpr = cd.last_primary_runs.get(agent)

            # Per-entity fail-soft (MUST 10): any field read that fails degrades
            # only that row's value. Since we're reading from pre-loaded dataclasses
            # (MUST 13), the individual getattr calls below are the per-field guards.

            status = status_for_agent(
                agent_health=ah,
                attention_items=open_items,
                last_primary_run_at=lpr,
                now=now,
                cost_spike=agent in spike_agents,
            )
            status_lower = status.lower()

            # Health score + band
            health_score: int | None = None
            health_band = "unknown"
            if ah is not None:
                raw_composite = getattr(ah, "composite", None)
                if raw_composite is None:
                    raw_composite = getattr(ah, "fleet_composite", None)
                if raw_composite is not None:
                    health_score = int(round(float(raw_composite) * 100))
                health_band = getattr(ah, "band", "unknown")

            # Model
            model_id = ""
            if ah is not None:
                model_id = getattr(ah, "primary_model", None) or ""

            # Cost (7d from daily_series[-7:])
            series_7d = daily_series_by_agent.get(agent, [])
            cost_7d = sum(v for _, v in series_7d) if series_7d else 0.0
            spark_values = [v for _, v in series_7d]

            # Degraded check
            row_cost_degraded = cost_degraded_by_agent.get(agent, False)
            if row_cost_degraded:
                any_cost_degraded = True

            # Errors (24h) and failures (7d)
            errors_24h = errors_by_agent.get(agent, 0)
            fail_7d = failures_by_agent.get(agent, 0)

            # Last run
            last_run_str = _relative_time(lpr, now) if lpr else "never"
            last_run_stale = (status_lower == "stale")

            # Determine isStale for the JS sort key
            is_stale_for_sort = last_run_stale

            entity_list.append(
                {
                    "id": agent,
                    "name": agent,
                    "model": _short_model_name(model_id) if model_id else "",
                    "modelClass": _model_pill_class(model_id) if model_id else "local",
                    "status": status_lower,
                    "health": {
                        "score": health_score,
                        "band": health_band,
                    },
                    "errors24h": errors_24h,
                    "fail7d": fail_7d,
                    "cost7d": cost_7d,
                    "lastRun": last_run_str,
                    "lastRunStale": is_stale_for_sort,
                    "spark": spark_values,
                    "costDegraded": row_cost_degraded,
                }
            )

        # Embed entity data as JSON for client JS (no SSE/fetch — MUST 13).
        agents_json = _json.dumps(entity_list)

        # Cost-degraded banner (spec/09 posture — MUST 10).
        cost_banner = (
            '<div class="degraded-banner" id="cost-degraded-banner">'
            '<span class="pill warn">⚠ data may be incomplete</span>'
            " One or more cost reads failed. Costs and sparklines below may be"
            " missing or understated."
            "</div>"
            if any_cost_degraded
            else '<div id="cost-degraded-banner" style="display:none"></div>'
        )

        html_out = (
            # Embedded agent data for JS
            f'<script>var AGENTS = {agents_json};</script>\n'
            # Cost-degraded banner
            + cost_banner
            + "\n"
            # Roster DOM scaffold — JS populates tbody/cards
            + """
<!-- List view (default) -->
<div id="monitor-list">
  <table class="mon-table">
    <thead>
      <tr>
        <th style="width:14px"></th>
        <th>Agent</th>
        <th>Model</th>
        <th class="r">Health</th>
        <th class="r">Errors (24h)</th>
        <th class="r">Failures (7d)</th>
        <th class="r">7d cost</th>
        <th>Last run</th>
        <th>Trend (7d)</th>
      </tr>
    </thead>
    <tbody id="mon-list-body"></tbody>
  </table>
  <div id="mon-list-empty" class="mon-empty" style="display:none">No agents match the current filter.</div>
</div>

<!-- Card view (hidden by default) -->
<div id="monitor-cards" class="agent-grid" style="display:none"></div>
<div id="mon-cards-empty" class="mon-empty" style="display:none">No agents match the current filter.</div>
"""
        )
        return PanelResult(html=html_out)


# Registration into the monitor-roster slot
register(_MonitorRosterPanel())

"""Fleet Monitor summary bar panel — monitor-summary slot (spec/56 §4, §6).

Renders the OK/WARN/ERROR/STALE status-count bar that doubles as filters on the
Fleet Monitor page. This is the monitor-summary panel registered into the
monitor-summary slot (spec/52 §16 amended by spec/56 §6).

Status derivation: uses the SHARED status_for_agent() (spec/52 §17.1, spec/56 §3).
The panel reads only from ctx.console_data (MUST 13 — no backend I/O at render time).

MUST 12 (spec/56): the monitor status counts MUST equal the home's fleet-status
summary for the same snapshot. Both this panel and _fleet_status.py call the same
status_for_agent() over the same ctx.console_data — that is the structural guarantee.
"""

from __future__ import annotations

import json as _json

from ._registry import PanelContext, PanelResult, register


class _MonitorSummaryPanel:
    id = "monitor_summary"
    slot = "monitor-summary"
    order = 10

    def is_available(self, ctx: PanelContext) -> bool:
        return True  # always shown; empty fleet gets zero counts

    def render(self, ctx: PanelContext) -> PanelResult:
        from .._status import status_for_agent  # MUST 2: canonical shared function

        cd = ctx.console_data
        now = ctx.now

        # Build per-agent lookup structures from pre-loaded data (MUST 13).
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

        # Derive status counts — same derivation as _fleet_status.py (MUST 12).
        counts: dict[str, int] = {"OK": 0, "WARN": 0, "ERROR": 0, "STALE": 0}
        for agent in sorted(cd.last_primary_runs.keys()):
            ah = agent_health_by_name.get(agent)
            open_items = open_items_by_agent.get(agent, [])
            lpr = cd.last_primary_runs.get(agent)
            status = status_for_agent(
                agent_health=ah,
                attention_items=open_items,
                last_primary_run_at=lpr,
                now=now,
                cost_spike=agent in spike_agents,
            )
            counts[status] = counts.get(status, 0) + 1

        total = sum(counts.values())

        counts_json = _json.dumps(
            {
                "all": total,
                "error": counts.get("ERROR", 0),
                "warn": counts.get("WARN", 0),
                "stale": counts.get("STALE", 0),
                "ok": counts.get("OK", 0),
            }
        )

        html_out = (
            '<div class="cockpit-zone-label">Status summary</div>\n'
            '<div class="status-bar" id="status-bar"'
            f" data-counts='{counts_json}'>\n"
            '  <span class="status-chip chip-all active" data-filter="all">'
            f'All <span id="chip-count-all">{total}</span></span>\n'
            '  <span class="status-chip chip-error" data-filter="error">'
            '<span class="dot"></span>'
            f'<span id="chip-count-error">{counts.get("ERROR", 0)}</span> Error</span>\n'
            '  <span class="status-chip chip-warn" data-filter="warn">'
            '<span class="dot"></span>'
            f'<span id="chip-count-warn">{counts.get("WARN", 0)}</span> Warn</span>\n'
            '  <span class="status-chip chip-stale" data-filter="stale">'
            '<span class="dot"></span>'
            f'<span id="chip-count-stale">{counts.get("STALE", 0)}</span> Stale</span>\n'
            '  <span class="status-chip chip-ok" data-filter="ok">'
            '<span class="dot"></span>'
            f'<span id="chip-count-ok">{counts.get("OK", 0)}</span> OK</span>\n'
            '</div>'
        )
        return PanelResult(html=html_out)


# Registration into the monitor-summary slot
register(_MonitorSummaryPanel())

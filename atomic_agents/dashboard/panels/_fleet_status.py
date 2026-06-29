"""Fleet Status Summary panel — EXPLORE slot (spec/52 §16, MUST 15, MUST 18).

Renders a compact fleet-status summary (OK/WARN/ERROR/STALE counts + link to
Fleet Monitor #653). MUST 15: the home MUST NOT render the full per-agent card
grid — this panel replaces it with a count summary only.

MUST 18: calls status_for_agent() from dashboard._status — the SAME function
the Fleet Monitor (#653) will call. Tested by the conformance suite.

Reads only from ctx.console_data — no backend I/O at render time (MUST 13).
"""

from __future__ import annotations

from ._registry import PanelContext, PanelResult, register

# Status display ordering and CSS class mapping
_STATUS_ORDER = ["ERROR", "STALE", "WARN", "OK"]
_STATUS_CSS = {
    "ERROR": "axis-spike",
    "STALE": "axis-warn",
    "WARN": "axis-warn",
    "OK": "axis-ok",
}


class _FleetStatusPanel:
    id = "fleet_status_summary"
    slot = "explore"
    order = 10

    def is_available(self, ctx: PanelContext) -> bool:
        return True  # always rendered; shows empty-fleet state when no agents

    def render(self, ctx: PanelContext) -> PanelResult:
        from .._status import status_for_agent  # MUST 18: canonical function

        cd = ctx.console_data
        now = ctx.now

        if cd.agent_count == 0:
            # NOTE: the EXPLORE zone-label divider is emitted by the layout template,
            # not here — chrome is separate from panel content (MUST 11 spirit).
            html_out = (
                '<div class="panel">'
                '<p class="empty-note">No agents discovered yet. Run <code>atomic-agents init</code> to create your first agent.</p>'
                "</div>"
            )
            return PanelResult(html=html_out)

        # Build lookup structures from pre-loaded data (MUST 13 — no disk reads)
        agent_health_by_name: dict = {}
        fh = cd.fleet_health
        if fh is not None:
            for ah in getattr(fh, "agents", []):
                agent_health_by_name[ah.agent] = ah

        open_items_by_agent: dict[str, list] = {}
        for item in cd.attention_queue:
            if item.ack_snooze_status == "open":
                open_items_by_agent.setdefault(item.agent, []).append(item)

        # Agents with a detected cost spike — threaded as a first-class WARN signal
        # into status_for_agent (cost_spike=...). The Fleet Monitor (#653) MUST
        # compute this the SAME way (CostTrendPoint.spike_detected) so the home
        # summary and the Monitor never diverge (MUST 18). This covers a spike whose
        # AlertItem was acked/snoozed (no longer in open_items) but persists.
        spike_agents = {ct.agent for ct in cd.cost_trends if ct.spike_detected}

        # Derive status per agent (MUST 18: same function as Monitor)
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

        # Build count cells
        count_cells = []
        for status in _STATUS_ORDER:
            n = counts.get(status, 0)
            css = _STATUS_CSS.get(status, "axis-muted")
            count_cells.append(
                f'<div class="fo-cell">'
                f'<div class="fc-v {css.replace("axis-", "fc-")}">{n}</div>'
                f'<div class="fc-k">{status}</div>'
                "</div>"
            )

        # Monitor link placeholder (#653)
        monitor_link = (
            '<div class="foot-note" style="margin-top:12px">'
            "Full per-agent grid → "
            '<a href="monitor.html" title="Fleet Monitor (#653 — coming soon)">Fleet Monitor</a>'
            " (coming soon)"
            "</div>"
        )

        html_out = (
            '<div class="panel" style="margin-bottom:16px">'
            '<div class="axis-title" style="margin-bottom:12px">Fleet Status</div>'
            '<div class="fo-grid">'
            + "".join(count_cells)
            + "</div>"
            + monitor_link
            + "</div>"
        )
        return PanelResult(html=html_out)


# Registration
register(_FleetStatusPanel())

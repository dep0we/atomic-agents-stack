"""Per-Agent Detail telemetry tab panels — agent-tab slot (spec/57 §3, MUST 4).

Each panel wraps one telemetry tab for the Per-Agent Detail Cockpit.  Tabs are
composed via compose_agent_detail() (PanelRegistry), which drives availability
gating and ordering.

Agent-specific data is passed via ctx.agent_detail (a _AgentDetailData set on
the PanelContext by render_agent_detail before calling compose_agent_detail).
Each panel's is_available() gates on the relevant artifact/backend presence —
no I/O at render time (MUST 13).

Tab IDs, labels, and ordering (spec/57 §3):
  overview  (order=10) — always available
  cost      (order=20) — always available
  activity  (order=30) — always available
  quality   (order=40) — available when evals/ exists
  memory    (order=50) — available when memory/*.md exists
  goals     (order=60) — available when goal.md exists
  dreaming  (order=70) — available when dreams/drm_*/manifest.json exists
  efficiency(order=80) — always available
"""
from __future__ import annotations

from ._registry import PanelContext, PanelResult, register


class _OverviewTab:
    id = "agent_tab_overview"
    slot = "agent-tab"
    order = 10
    tab_id = "overview"
    tab_label = "Overview"

    def is_available(self, ctx: PanelContext) -> bool:
        return True

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_overview_tab

        d = getattr(ctx, "agent_detail", None)
        if d is None:
            return PanelResult(html='<div class="empty-tab">No agent data.</div>')
        html = _render_overview_tab(d.agent_health, d.cost_data, d.recs, d.agent_id)
        return PanelResult(html=html)


class _CostTab:
    id = "agent_tab_cost"
    slot = "agent-tab"
    order = 20
    tab_id = "cost"
    tab_label = "Cost"

    def is_available(self, ctx: PanelContext) -> bool:
        return True

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_cost_tab

        d = getattr(ctx, "agent_detail", None)
        html = _render_cost_tab(d.cost_data if d is not None else None)
        return PanelResult(html=html)


class _ActivityTab:
    id = "agent_tab_activity"
    slot = "agent-tab"
    order = 30
    tab_id = "activity"
    tab_label = "Activity"

    def is_available(self, ctx: PanelContext) -> bool:
        return True

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_activity_tab

        d = getattr(ctx, "agent_detail", None)
        html = _render_activity_tab(d.cost_data if d is not None else None)
        return PanelResult(html=html)


class _QualityTab:
    id = "agent_tab_quality"
    slot = "agent-tab"
    order = 40
    tab_id = "quality"
    tab_label = "Quality"

    def is_available(self, ctx: PanelContext) -> bool:
        d = getattr(ctx, "agent_detail", None)
        return d is not None and d.has_evals

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_quality_tab

        d = getattr(ctx, "agent_detail", None)
        if d is None:
            return PanelResult(html='<div class="empty-tab">No agent data.</div>')
        html = _render_quality_tab(d.agent_id, d.agents_root)
        return PanelResult(html=html)


class _MemoryTab:
    id = "agent_tab_memory"
    slot = "agent-tab"
    order = 50
    tab_id = "memory"
    tab_label = "Memory"

    def is_available(self, ctx: PanelContext) -> bool:
        d = getattr(ctx, "agent_detail", None)
        return d is not None and d.has_memory

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_memory_tab

        d = getattr(ctx, "agent_detail", None)
        if d is None:
            return PanelResult(html='<div class="empty-tab">No agent data.</div>')
        html = _render_memory_tab(d.agent_id, d.agents_root)
        return PanelResult(html=html)


class _GoalsTab:
    id = "agent_tab_goals"
    slot = "agent-tab"
    order = 60
    tab_id = "goals"
    tab_label = "Goals"

    def is_available(self, ctx: PanelContext) -> bool:
        d = getattr(ctx, "agent_detail", None)
        return d is not None and d.has_goals

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_goals_tab

        d = getattr(ctx, "agent_detail", None)
        if d is None:
            return PanelResult(html='<div class="empty-tab">No agent data.</div>')
        html = _render_goals_tab(d.agent_id, d.agents_root)
        return PanelResult(html=html)


class _DreamingTab:
    id = "agent_tab_dreaming"
    slot = "agent-tab"
    order = 70
    tab_id = "dreaming"
    tab_label = "Dreaming &#9670;"

    def is_available(self, ctx: PanelContext) -> bool:
        d = getattr(ctx, "agent_detail", None)
        return d is not None and d.has_dreaming

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_dream_tab

        d = getattr(ctx, "agent_detail", None)
        if d is None:
            return PanelResult(html='<div class="empty-tab">No agent data.</div>')
        html = _render_dream_tab(d.agent_root)
        return PanelResult(html=html)


class _EfficiencyTab:
    id = "agent_tab_efficiency"
    slot = "agent-tab"
    order = 80
    tab_id = "efficiency"
    tab_label = "Efficiency"

    def is_available(self, ctx: PanelContext) -> bool:
        return True

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render_agent_detail import _render_efficiency_tab

        d = getattr(ctx, "agent_detail", None)
        html = _render_efficiency_tab(
            d.agent_health if d is not None else None,
            d.cost_data if d is not None else None,
        )
        return PanelResult(html=html)


register(_OverviewTab())
register(_CostTab())
register(_ActivityTab())
register(_QualityTab())
register(_MemoryTab())
register(_GoalsTab())
register(_DreamingTab())
register(_EfficiencyTab())

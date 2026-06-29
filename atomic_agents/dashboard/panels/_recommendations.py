"""Recommendations panel — ACT slot (spec/52 §16, MUST 10).

Renders the model right-sizing / savings recommendations (spec/54).
Reads from pre-loaded ctx.console_data.recommendations — no backend I/O (MUST 13).
"""

from __future__ import annotations

from ._registry import PanelContext, PanelResult, register


class _RecommendationsPanel:
    id = "recommendations"
    slot = "act"
    order = 30

    def is_available(self, ctx: PanelContext) -> bool:
        return ctx.console_data.recommendations is not None

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render import _render_recommendations  # lazy import — render-time only

        html_out = _render_recommendations(ctx.console_data.recommendations)
        return PanelResult(html=html_out)


# Registration
register(_RecommendationsPanel())

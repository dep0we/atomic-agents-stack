"""Runtime Health panel — STATUS slot (spec/52 §16, MUST 10, MUST 14).

Renders the Fleet Health Score band with a B6-style collapsible scorecard.
MUST 14: renders ONLY the 3 real equal-weight axes (Cost/Quality/Reliability)
— no governance, model_fit, or work_mix rows.

_RUNTIME_AXES is the panel-owned set naming those three axes. It is the LOAD-BEARING
enforcement: render._render_health_band() imports this set and filters the scorecard
`display_order` through it before emitting rows, so a row whose axis is not in this
set (governance / model_fit / work_mix) never reaches the HTML. The constant lives
here (on the panel) because the panel owns the Runtime-Health framing contract; the
render module is the consumer.

The panel reads pre-loaded data from ctx.console_data.fleet_health (set
by render_console() BEFORE PanelContext is built — MUST 13, no render-time I/O).
"""

from __future__ import annotations

from ._registry import PanelContext, PanelResult, register

# MUST 14: only these axes may appear in the Runtime-Health scorecard. Imported by
# render._render_health_band() and applied as a filter on display_order — see the
# module docstring. Changing this set is the ONLY way to change which axes render.
_RUNTIME_AXES = frozenset({"cost", "quality", "reliability"})


class _RuntimeHealthPanel:
    id = "runtime_health"
    slot = "status"
    order = 20

    def is_available(self, ctx: PanelContext) -> bool:
        return ctx.console_data.fleet_health is not None

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render import _render_health_band  # lazy import — render-time only

        fleet_health = ctx.console_data.fleet_health
        html_out = _render_health_band(fleet_health)
        if not html_out:
            return PanelResult(html="")

        section = f"\n<h2>Fleet Health Score</h2>\n{html_out}\n"
        return PanelResult(html=section)


# Registration
register(_RuntimeHealthPanel())

"""Three-axis trend panels — ACT slot (spec/52 §16, MUST 10).

Renders the Cost / Quality / Reliability axis panels.
Reads from pre-loaded ctx.console_data — no backend I/O at render time (MUST 13).
"""

from __future__ import annotations

import html as _html

from ._registry import PanelContext, PanelResult, register


class _TrendPanelsPanel:
    """Three-axis trend panels: cost, quality, reliability."""

    id = "trend_panels"
    slot = "act"
    order = 20

    def is_available(self, ctx: PanelContext) -> bool:
        return True  # always render (shows empty-state when no data)

    def render(self, ctx: PanelContext) -> PanelResult:
        cost_panel = _render_cost_panel(ctx)
        quality_panel = _render_quality_panel(ctx)
        reliability_panel = _render_reliability_panel(ctx)

        html_out = (
            "<h2>Fleet Trends</h2>"
            '<div class="axis-panels">'
            f"\n{cost_panel}"
            f"\n{quality_panel}"
            f"\n{reliability_panel}"
            "\n</div>"
        )
        return PanelResult(html=html_out)


def _render_cost_panel(ctx: PanelContext) -> str:
    if ctx.console_data.cost_trends:
        cost_rows = []
        for ct in ctx.console_data.cost_trends[:8]:
            spike_html = (
                ' <span class="axis-spike">▲spike</span>' if ct.spike_detected else ""
            )
            agent_safe = _html.escape(ct.agent)
            cost_rows.append(
                f'<div class="axis-row">'
                f"<div>{agent_safe}</div>"
                f'<div class="axis-val">${ct.total_usd_30d:.3f}/30d{spike_html}</div>'
                f"</div>"
            )
        return (
            '<div class="axis-panel">'
            '<div class="axis-title">Cost · 30-day total</div>'
            + "".join(cost_rows)
            + "</div>"
        )
    return (
        '<div class="axis-panel">'
        '<div class="axis-title">Cost · 30-day total</div>'
        '<p class="empty-note">No cost data.</p>'
        "</div>"
    )


def _render_quality_panel(ctx: PanelContext) -> str:
    if ctx.console_data.quality_signals:
        qual_rows = []
        for qs in sorted(
            ctx.console_data.quality_signals,
            key=lambda q: q.latest_score or 0,
        )[:8]:
            if qs.latest_score is None:
                score_html = '<span class="axis-muted">no evals</span>'
            else:
                delta_str = ""
                if qs.delta_30d is not None:
                    sign = "+" if qs.delta_30d >= 0 else ""
                    delta_cls = "axis-ok" if qs.delta_30d >= 0 else "axis-spike"
                    delta_str = (
                        f' <span class="{delta_cls}">{sign}{qs.delta_30d:.2f}</span>'
                    )
                score_html = (
                    f'<span class="axis-val">{qs.latest_score:.2f}{delta_str}</span>'
                )
            qual_rows.append(
                f'<div class="axis-row">'
                f"<div>{_html.escape(qs.agent)}</div>"
                f"<div>{score_html}</div>"
                f"</div>"
            )
        return (
            '<div class="axis-panel">'
            '<div class="axis-title">Quality · eval score (30d delta)</div>'
            + "".join(qual_rows)
            + "</div>"
        )
    return (
        '<div class="axis-panel">'
        '<div class="axis-title">Quality · eval score (30d delta)</div>'
        '<p class="empty-note">No eval data yet.</p>'
        "</div>"
    )


def _render_reliability_panel(ctx: PanelContext) -> str:
    if ctx.console_data.reliability_metrics:
        rel_rows = []
        for rm in sorted(
            ctx.console_data.reliability_metrics,
            key=lambda r: -r.error_rate,
        )[:8]:
            if rm.total_runs == 0:
                val_html = '<span class="axis-muted">no runs</span>'
            else:
                err_pct = int(rm.error_rate * 100)
                blk_pct = int(rm.blocked_rate * 100)
                err_cls = "axis-spike" if rm.error_rate >= 0.2 else "axis-ok"
                val_html = (
                    f'<span class="axis-val">'
                    f'<span class="{err_cls}">{err_pct}% err</span>'
                    f" · {blk_pct}% blk"
                    f"</span>"
                )
            rel_rows.append(
                f'<div class="axis-row">'
                f"<div>{_html.escape(rm.agent)}</div>"
                f"<div>{val_html}</div>"
                f"</div>"
            )
        return (
            '<div class="axis-panel">'
            '<div class="axis-title">Reliability · error / blocked rate (30d)</div>'
            + "".join(rel_rows)
            + "</div>"
        )
    return (
        '<div class="axis-panel">'
        '<div class="axis-title">Reliability · error / blocked rate (30d)</div>'
        '<p class="empty-note">No run data yet.</p>'
        "</div>"
    )


# Registration
register(_TrendPanelsPanel())

"""KPI Strip panel — STATUS slot (spec/52 §16, B6 cockpit design).

Renders the top KPI hero tiles:
  - 7-day spend (from daily_series[-7:] on cost trends)
  - 30-day spend (total across fleet)
  - Agents active (ran in last 24h — derived from last_primary_runs)
  - Needs attention (open alert count)
  - Fleet Health score (from fleet_health)

Reads only from ctx.console_data — no backend I/O at render time (MUST 13).
"""

from __future__ import annotations

from ._registry import PanelContext, PanelResult, register


class _KpiStripPanel:
    id = "kpi_strip"
    slot = "status"
    order = 10

    def is_available(self, ctx: PanelContext) -> bool:
        return True  # always rendered

    def render(self, ctx: PanelContext) -> PanelResult:
        from datetime import timedelta

        cd = ctx.console_data
        now = ctx.now

        # 30-day fleet spend
        total_30d = sum(ct.total_usd_30d for ct in cd.cost_trends)

        # 7-day spend + sparkline: align every agent's daily_series by ISO-day and
        # sum into a single fleet-wide per-day series, take the last 7 calendar days.
        # The KPI total and the sparkline are derived from the SAME aligned series so
        # the headline number and the drawn line never disagree.
        fleet_by_day: dict[str, float] = {}
        for ct in cd.cost_trends:
            for _day, usd in ct.daily_series:
                fleet_by_day[_day] = fleet_by_day.get(_day, 0.0) + usd
        spark_7d = [usd for _day, usd in sorted(fleet_by_day.items())][-7:]
        total_7d = sum(spark_7d)

        # Agents active in last 24h
        staleness_24h = timedelta(hours=24)
        active_count = sum(
            1
            for agent, lpr in cd.last_primary_runs.items()
            if lpr is not None and (now - _ensure_tz(lpr)) <= staleness_24h
        )
        fleet_size = cd.agent_count

        # Open attention items
        open_alerts = [a for a in cd.attention_queue if a.ack_snooze_status == "open"]
        alert_count = len(open_alerts)

        # Fleet health score
        fh = cd.fleet_health
        health_display = None
        health_band = "unknown"
        if fh is not None:
            health_display = getattr(fh, "fleet_composite_display", None)
            health_band = getattr(fh, "fleet_band", "unknown")

        # Potential savings from recommendations
        savings_total = 0.0
        if cd.recommendations:
            for rec in cd.recommendations:
                delta = getattr(rec, "projected_usd_delta", None)
                if delta is not None and delta < 0:
                    savings_total += abs(delta)

        # Build KPI tiles
        tiles = []

        # 7-day spend (with inline 7-day sparkline drawn from daily_series)
        spark_svg = _sparkline_svg(spark_7d)
        tiles.append(
            '<div class="kpi cockpit-kpi">'
            '<div class="k">7-day spend</div>'
            f'<div class="v mono">${total_7d:.2f}</div>'
            f"{spark_svg}"
            f'<div class="sub2">fleet total</div>'
            "</div>"
        )

        # 30-day spend
        tiles.append(
            '<div class="kpi cockpit-kpi">'
            '<div class="k">30-day spend</div>'
            f'<div class="v mono">${total_30d:.2f}</div>'
            f'<div class="sub2">across {fleet_size} agent{"s" if fleet_size != 1 else ""}</div>'
            "</div>"
        )

        # Agents active
        tiles.append(
            '<div class="kpi cockpit-kpi">'
            '<div class="k">Agents active</div>'
            f'<div class="v mono">{active_count}<span style="font-size:16px;color:var(--muted)"> / {fleet_size}</span></div>'
            '<div class="sub2">ran in last 24h</div>'
            "</div>"
        )

        # Needs attention
        alert_cls = " kpi-alert" if alert_count > 0 else ""
        tiles.append(
            f'<div class="kpi cockpit-kpi{alert_cls}">'
            '<div class="k">Needs attention</div>'
            f'<div class="v mono">{alert_count}</div>'
            f'<div class="sub2">open flag{"s" if alert_count != 1 else ""}</div>'
            "</div>"
        )

        # Potential savings
        if savings_total > 0:
            tiles.append(
                '<div class="kpi cockpit-kpi kpi-save">'
                '<div class="k">Potential savings</div>'
                f'<div class="v mono">~${savings_total:.0f}<span style="font-size:15px;color:var(--muted)">/mo</span></div>'
                '<div class="sub2">model right-sizes</div>'
                "</div>"
            )

        # Fleet health
        if health_display is not None:
            health_color_cls = {
                "green": "kpi-health-green",
                "amber": "kpi-health-amber",
                "red": "kpi-health-red",
            }.get(health_band, "")
            tiles.append(
                f'<div class="kpi cockpit-kpi kpi-health {health_color_cls}">'
                '<div class="k">Fleet Health</div>'
                f'<div class="v mono">{health_display}</div>'
                f'<div class="sub2">{health_band} band · 3-axis score</div>'
                "</div>"
            )

        # NOTE: the STATUS zone-label divider is emitted by the layout template,
        # not here — chrome is separate from panel content so a panel fail-soft
        # (MUST 11) never removes the zone divider.
        html_out = '<div class="kpis cockpit-kpis">' + "".join(tiles) + "</div>"
        return PanelResult(html=html_out)


def _ensure_tz(dt):
    """Ensure datetime is tz-aware (UTC fallback)."""
    from datetime import timezone

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _sparkline_svg(values: list[float], width: int = 72, height: int = 18) -> str:
    """Render an inline SVG polyline sparkline from a list of daily values.

    Pure-compute, no I/O. Returns '' when there is nothing to draw (fewer than 2
    points). Matches the inline-SVG 7-day cost sparkline in variant-B6-zones.html.
    The line is normalized to the value range; a flat series draws a centered line.
    """
    pts = [float(v) for v in values]
    if len(pts) < 2:
        return ""
    lo = min(pts)
    hi = max(pts)
    span = hi - lo
    n = len(pts)
    step = width / (n - 1)
    coords = []
    for i, v in enumerate(pts):
        x = i * step
        # y inverted (SVG origin top-left); flat series → mid-height.
        if span > 0:
            y = height - ((v - lo) / span) * height
        else:
            y = height / 2
        coords.append(f"{x:.1f},{y:.1f}")
    points = " ".join(coords)
    return (
        f'<svg class="kpi-spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="7-day spend trend">'
        f'<polyline fill="none" stroke="var(--accent)" stroke-width="1.5" '
        f'points="{points}"/>'
        f"</svg>"
    )


# Registration
register(_KpiStripPanel())

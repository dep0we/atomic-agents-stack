"""Attention Queue panel — ACT slot (spec/52 §16, MUST 10).

Renders the Operator Attention Queue. This panel owns the alert_keys
for all items it renders; the engine unions these into the sidecar
(MUST 17). alert_class format for governance keys is preserved exactly
as f'governance.{alert_subclass}' to protect MUST 7 key stability.
"""

from __future__ import annotations

import html as _html

from ._registry import PanelContext, PanelResult, register


class _AttentionQueuePanel:
    id = "attention_queue"
    slot = "act"
    order = 10

    def is_available(self, ctx: PanelContext) -> bool:
        return True  # always present; renders empty-state when queue is empty

    def render(self, ctx: PanelContext) -> PanelResult:
        from ..render import (
            _severity_class,
            _queue_status_html,
        )  # lazy import — render-time only

        queue = ctx.console_data.attention_queue
        active_items = [a for a in queue if a.ack_snooze_status == "open"]
        known_items = [a for a in queue if a.ack_snooze_status in ("acked", "snoozed")]
        active_count = len(active_items)

        if not queue:
            queue_html = (
                '<div class="queue-empty">'
                "✓ All agents healthy — no items need attention."
                "</div>"
            )
        else:
            rows = []
            for item in active_items + known_items:
                muted_style = (
                    ' style="opacity: 0.5;"' if item.ack_snooze_status != "open" else ""
                )
                sev_cls = _severity_class(item.severity)
                agent_safe = _html.escape(item.agent)
                reason_safe = _html.escape(item.reason)
                next_step_safe = _html.escape(item.next_step)
                owner_safe = _html.escape(item.owner or "—")
                key_safe = _html.escape(item.alert_key)
                ack_label = "Acked" if item.ack_snooze_status == "acked" else "Ack"
                ack_btn_cls = (
                    "alert-btn acked"
                    if item.ack_snooze_status == "acked"
                    else "alert-btn"
                )
                rows.append(
                    f'<div class="alert-row"{muted_style}>'
                    f'<div class="{sev_cls}">{_html.escape(item.severity.upper())}'
                    f"<br>{_queue_status_html(item.status)}</div>"
                    f"<div><strong>{agent_safe}</strong>"
                    f'<div class="muted">{reason_safe}</div>'
                    f'<div class="muted" style="margin-top:4px;font-size:11px;">'
                    f"Next: {next_step_safe}</div></div>"
                    f"<div>{owner_safe}</div>"
                    f'<div class="muted">{_html.escape(item.alert_class)}'
                    f"/{_html.escape(item.alert_subclass)}</div>"
                    f'<div class="alert-actions">'
                    f'<button class="{ack_btn_cls}" onclick="ackAlert(\'{key_safe}\')">{ack_label}</button>'
                    f'<button class="alert-btn" onclick="openSnooze(\'{key_safe}\')">Snooze</button>'
                    f"</div>"
                    f"</div>"
                )
            queue_html = (
                '<div class="attention-queue">'
                '<div class="alert-header">'
                "<div>Severity</div><div>Agent / Reason</div>"
                "<div>Owner</div><div>Class</div><div>Actions</div>"
                "</div>" + "".join(rows) + "</div>"
            )

        count_badge = (
            f'<span class="pill error" style="margin-left:8px;">{active_count} open</span>'
            if active_count
            else '<span class="pill ok" style="margin-left:8px;">0 open</span>'
        )

        html_out = f"<h2>Operator Attention Queue {count_badge}</h2>\n{queue_html}"

        # All rendered alert keys — engine will union these into the sidecar (MUST 17).
        alert_keys = frozenset(item.alert_key for item in queue)
        return PanelResult(html=html_out, alert_keys=alert_keys)


# Registration (spec/52 §16.5 — called at import time)
register(_AttentionQueuePanel())

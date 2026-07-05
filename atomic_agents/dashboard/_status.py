"""Shared agent status derivation for the Fleet Console (spec/52 §17.1, MUST 18).

status_for_agent() is the SINGLE canonical function for computing an agent's
fleet-status indicator. Both the home fleet-status summary panel and the Fleet
Monitor (#653) call THIS function — never a local copy. This ensures the
home summary counts and the Monitor view always agree.

Import discipline:
  This module imports from:
    - stdlib
    - ._reliability (_RELIABILITY_ERROR_RATE_WARN, the default threshold constant)
  It does NOT import from attention.py, render.py, advisor/, or agent.py.
  AgentHealth is imported as TYPE_CHECKING only to avoid circular imports at
  runtime — the function accepts the object duck-typed via getattr().

Status precedence (spec/52 §17.1):  ERROR > STALE > WARN > OK
  ERROR : AgentHealth.capped_by_axis is not None (critical cap fired, spec/53 §3.5)
          OR reliability error_rate >= error_rate_threshold
  STALE : no primary run in the last staleness_window (default 24h)
  WARN  : amber Runtime Health (AgentHealth.band == 'amber')
          OR open attention item exists
          OR an unacked cost spike (cost_spike=True)
  OK    : none of the above
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from ._reliability import _RELIABILITY_ERROR_RATE_WARN

if TYPE_CHECKING:
    from ..advisor.score import AgentHealth
    from .attention import AlertItem

# Status type alias for clarity at call sites
Status = Literal["OK", "WARN", "ERROR", "STALE"]


def status_for_agent(
    agent_health: "AgentHealth | None",
    attention_items: "list[AlertItem]",
    last_primary_run_at: datetime | None,
    *,
    now: datetime,
    cost_spike: bool = False,
    staleness_window: timedelta = timedelta(hours=24),
    error_rate_threshold: float = _RELIABILITY_ERROR_RATE_WARN,
) -> Status:
    """Derive the fleet-status indicator for one agent.

    Parameters
    ----------
    agent_health:
        Per-agent scoring result from compute_fleet_health(). None when scoring
        is unavailable (new agent, degraded read, etc.). When None, the ERROR
        composite/band check is skipped but STALE/WARN/OK still apply.
    attention_items:
        AlertItems for this agent whose ack_snooze_status == 'open'. Pass only
        open items; the function does not filter by status.
    last_primary_run_at:
        UTC datetime of the most recent primary run within the 30d window.
        None means no primary run was observed — triggers STALE regardless of
        staleness_window.
    now:
        Reference UTC datetime for staleness comparison. Must be tz-aware.
        Injected by the caller (PanelContext.now) so boundary tests are deterministic.
    cost_spike:
        True when this agent has an unacknowledged cost spike. A WARN contributor
        independent of the attention queue: a below-alert-threshold spike (no
        AlertItem) OR a spike whose AlertItem was acked/snoozed (so it dropped out
        of `attention_items`) still drives WARN as long as the spike persists. Both
        the home fleet-status panel AND the Fleet Monitor (#653) compute this the
        SAME way (CostTrendPoint.spike_detected for the agent), so the two surfaces
        never diverge (MUST 18).
    staleness_window:
        Duration after which no-primary-run triggers STALE. Default 24h per spec/52 §17.1.
        Named parameter for tunability (D12).
    error_rate_threshold:
        Fraction at which error_rate triggers ERROR. Default is _RELIABILITY_ERROR_RATE_WARN.
        Named parameter for tunability (D12).

    Returns
    -------
    Status: 'OK' | 'WARN' | 'ERROR' | 'STALE'
        Precedence: ERROR > STALE > WARN > OK (spec/52 §17.1).

    MUST 13 note: this function receives pre-loaded data. It does NOT read any
    backend or filesystem — the caller (fleet-status panel) passes all inputs
    from ctx.console_data which was loaded before the engine loop started.
    """
    # ── ERROR (highest precedence) ────────────────────────────────────────────
    # 1a. Critical-cap fired (capped_by_axis is not None, spec/53 §3.5).
    #     Do NOT use band == 'red' as a proxy — a naturally-low score can be red
    #     without triggering the cap; only the cap signal is load-bearing here.
    if (
        agent_health is not None
        and getattr(agent_health, "capped_by_axis", None) is not None
    ):
        return "ERROR"

    # 1b. Error-rate over threshold from the reliability axis.
    #     When agent_health is None, skip — no rate to check.
    if agent_health is not None:
        # Derive error_rate from the scorecard (avoid re-importing ReliabilityMetrics).
        # The scorecard carries the per-metric score; we need the raw value.
        # Fall back to checking via the attention items if scorecard lacks the value.
        err_rate = _extract_error_rate(agent_health)
        if err_rate is not None and err_rate >= error_rate_threshold:
            return "ERROR"

    # ── STALE (second precedence) ─────────────────────────────────────────────
    # No primary run observed, OR most recent primary run is older than the window.
    if last_primary_run_at is None:
        return "STALE"
    # Ensure tz-aware comparison.
    lpr = last_primary_run_at
    if lpr.tzinfo is None:
        lpr = lpr.replace(tzinfo=timezone.utc)
    if now - lpr > staleness_window:
        return "STALE"

    # ── WARN (third precedence) ───────────────────────────────────────────────
    # Amber Runtime Health band.
    if agent_health is not None and getattr(agent_health, "band", "unknown") == "amber":
        return "WARN"
    # Open attention item.
    if attention_items:
        return "WARN"
    # Unacked cost spike — first-class WARN signal (see the `cost_spike` param doc).
    # Covers a below-threshold spike with no AlertItem and an acked/snoozed spike
    # that dropped out of attention_items while spike_detected is still True.
    if cost_spike:
        return "WARN"

    # ── OK (default) ──────────────────────────────────────────────────────────
    return "OK"


def _extract_error_rate(agent_health: "AgentHealth") -> float | None:
    """Extract the error_rate value from AgentHealth.scorecard.

    Returns None when no error_rate row is present or its value is None.
    This avoids importing ReliabilityMetrics and keeps _status.py dependency-light.
    """
    for row in getattr(agent_health, "scorecard", []):
        if getattr(row, "metric", None) == "error_rate":
            return getattr(row, "value", None)
    return None

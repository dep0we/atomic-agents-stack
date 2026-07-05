"""Shared reliability signal computations for dashboard modules (spec/52/spec/53).

Extracted from attention.py so BOTH attention.py (alert generation) and
advisor/score.py (Fleet Health Score) import from a single definition.

Rules:
- This module MUST only import from stdlib and .costs (for RunRecord).
- It MUST NOT import from attention.py, render.py, advisor/, or agent.py.
  (No circular imports; no LLM machinery.)
- The _compute_reliability() function is the single canonical definition.
  Do NOT copy this logic into advisor/score.py or any other module.
"""

from __future__ import annotations

from dataclasses import dataclass

from .costs import RunRecord


# ──────────────────────────────────────────────────────────────────
# Reliability status strings — spec/52 MUST 8 explicit markers only.
# Verified against agent.py self._log({... 'status': ...}) call sites.

_RELIABILITY_ERROR_STATUSES = frozenset({"error"})
_RELIABILITY_BLOCKED_STATUSES = frozenset({"lock_busy"})
_RELIABILITY_INFLIGHT_STATUSES = frozenset({"in_flight"})
_RELIABILITY_PRINCIPAL_STATUSES = frozenset({"principal_not_verified"})
_RELIABILITY_SKIPPED_STATUSES = frozenset({"skipped", "deduped"})

# Warn thresholds — referenced from attention.py and advisor
_RELIABILITY_ERROR_RATE_WARN = 0.20
_RELIABILITY_BLOCKED_RATE_WARN = 0.10
_RELIABILITY_SKIPPED_RATE_WARN = 0.10

# Child/bookkeeping triggers that share the agent's JSONL but are NOT primary
# unattended runs.  Mirrors attention.py's _NON_PRIMARY_TRIGGERS exactly.
# Any trigger in this set is excluded from the reliability denominator.
#
# Uses allowlist-by-EXCLUSION: a new primary trigger is counted by default
# (conservative direction for a health signal) rather than dropped silently.
_NON_PRIMARY_TRIGGERS = frozenset(
    {
        "helper",
        "helper_batch_reservation",
        "helper_batch_release",
        "delegate",
        "delegate_batch_reservation",
        "delegate_batch_release",
        "tool_call",
        "tool_call_deferred",
        "judgment",
        "cost_warning",
        "capture_write_error",
        "escalation_deferred_execution",
        "escalation_operator_revise_executed",
        "escalation_operator_revise_invalid_amendment",
        "escalation_resolved",
        "embed_reservation",
        "embed_release",
        "embed_batch_reservation",
        "embed_batch_release",
        "embed_cost",
    }
)


def _is_primary_run(r: RunRecord) -> bool:
    """True when a RunRecord is a primary (unattended/top-level) agent run.

    spec/52 MUST 8: the Reliability rate is computed over primary runs only.
    Excludes child rows (have parent_run_id) and bookkeeping rows (trigger in
    _NON_PRIMARY_TRIGGERS).  Either condition alone is sufficient to exclude.
    """
    if getattr(r, "parent_run_id", None):
        return False
    if r.trigger in _NON_PRIMARY_TRIGGERS:
        return False
    return True


@dataclass
class ReliabilityMetrics:
    """Three-axis Reliability breakdown for one agent over a window."""

    agent: str
    total_runs: int
    error_rate: float  # fraction of runs with status=error
    blocked_rate: float  # fraction with status=lock_busy or embed_batch_blocked
    inflight_rate: float  # fraction with status=in_flight (stuck/not completed)
    principal_rate: float  # fraction with status=principal_not_verified
    skipped_rate: float  # fraction with status=skipped (cost-guardrail-blocked)
    embed_blocked_count: int  # absolute count of embed_batch_blocked events
    # Absolute counts for display columns (spec/56 MUST 9).
    # errors_24h: primary runs with status=error in the last 24h (error window).
    # failures_7d: primary runs that were blocked (lock_busy or embed_batch_blocked)
    #              in the last 7d (failure window).
    # These are 0 by default; aggregate_console populates them from the already-
    # loaded runs_30d filtered to the correct windows — no extra disk I/O required.
    errors_24h: int = 0
    failures_7d: int = 0


def _compute_reliability(
    runs: list[RunRecord],
    agent: str,
) -> ReliabilityMetrics:
    """Derive reliability metrics from explicit RunRecord structural markers.

    spec/52 MUST 8: ONLY explicit status markers + extra.embed_batch_blocked.
    No heuristic inference.

    Denominator is PRIMARY runs only — child/bookkeeping rows dilute every rate
    and double-attribute a delegated agent's cost-skip to the coordinator.

    IMPORTANT: blocked_rate uses a SINGLE predicate over records so that a run
    which is BOTH lock_busy AND carries embed_batch_blocked=True counts ONCE,
    not twice.  Summing lock_blocked_count + embed_blocked_count separately
    can push blocked_rate > 1.0 (#614 P2).
    """
    runs = [r for r in runs if _is_primary_run(r)]
    total = len(runs)
    if total == 0:
        return ReliabilityMetrics(
            agent=agent,
            total_runs=0,
            error_rate=0.0,
            blocked_rate=0.0,
            inflight_rate=0.0,
            principal_rate=0.0,
            skipped_rate=0.0,
            embed_blocked_count=0,
        )

    error_count = sum(1 for r in runs if r.status in _RELIABILITY_ERROR_STATUSES)
    inflight_count = sum(1 for r in runs if r.status in _RELIABILITY_INFLIGHT_STATUSES)
    principal_count = sum(
        1 for r in runs if r.status in _RELIABILITY_PRINCIPAL_STATUSES
    )
    skipped_count = sum(1 for r in runs if r.status in _RELIABILITY_SKIPPED_STATUSES)
    embed_blocked_count = sum(
        1 for r in runs if getattr(r, "extra", {}).get("embed_batch_blocked", False)
    )

    # Single predicate for blocked — prevents double-counting (lock_busy + embed).
    total_blocked = sum(
        1
        for r in runs
        if r.status in _RELIABILITY_BLOCKED_STATUSES
        or getattr(r, "extra", {}).get("embed_batch_blocked", False) is True
    )

    return ReliabilityMetrics(
        agent=agent,
        total_runs=total,
        error_rate=round(error_count / total, 4),
        blocked_rate=round(total_blocked / total, 4),
        inflight_rate=round(inflight_count / total, 4),
        principal_rate=round(principal_count / total, 4),
        skipped_rate=round(skipped_count / total, 4),
        embed_blocked_count=embed_blocked_count,
    )

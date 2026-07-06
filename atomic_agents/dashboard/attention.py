"""Operator Attention Queue and three-axis trend panels for the Fleet Console (spec/52).

Aggregates governance, cost-spike, quality-regression, and operational-health
alerts across the agent fleet into a ranked attention queue. Each alert has a
stable alert_key (spec/52 MUST 7 dedup contract) so ack/snooze state survives
across render cycles.

Alert key stability (spec/52 MUST 7):
    alert_key = stable hash(agent_id + alert_class + normalized_reason_bucket)
    Uses hashlib.sha256 with NUL separator (same pattern as PrincipalBackend
    MUST 11, documented in MEMORY.md feedback_identity_backend_security_lenses).
    Normalization strips all transient specifics (run_id, exact timestamps,
    exact dollar figures) so a recurring condition always maps to the same key.
    Key format: "v1:<12-char hex>" — the v1: version prefix allows future
    normalization changes to invalidate old sidecar entries explicitly.
    Never use Python's hash() — non-deterministic across processes since 3.3.

Reliability axis (spec/52 MUST 8):
    ONLY from explicit RunRecord structural markers:
        status in {error, lock_busy, skipped, in_flight, principal_not_verified}
        plus extra.embed_batch_blocked
    NO heuristic stuck/looping inference.

Governance alert classes (spec/52 §2.2):
    NO_GOVERNANCE: governance.md absent (has_governance=False)
    GOVERNANCE_INVALID: governance.md present, parse_errors non-empty
    GOVERNANCE_INCOMPLETE: governance.md present, valid, but owner=None
    GOVERNANCE_NO_BLOCK: governance.md present + readable but has no
        `governance:` YAML block (has_governance=True, governance=None)

    NOTE: 'overdue review by date' is DEFERRED to PR2. The review_cadence and
    next_review_by fields do not exist in GovernanceRecord today. PR1 only alerts
    on no-owner states — the three classes above.

Quality signals (spec/52 MUST 5):
    Reads from quality.py's aggregate_quality when available. For standalone
    console renders, falls back to a lightweight direct reader.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from .costs import RunRecord, discover_agents, _load_runs_with_degraded
from .alert_state import read_alert_state
from ._shared import eval_score_delta_fmt as _eval_score_delta_fmt
from ._reliability import (
    ReliabilityMetrics,
    _compute_reliability,
    _is_primary_run,
    _NON_PRIMARY_TRIGGERS,
    _RELIABILITY_ERROR_STATUSES,
    _RELIABILITY_BLOCKED_STATUSES,
    _RELIABILITY_INFLIGHT_STATUSES,
    _RELIABILITY_PRINCIPAL_STATUSES,
    _RELIABILITY_SKIPPED_STATUSES,
    _RELIABILITY_ERROR_RATE_WARN,
    _RELIABILITY_BLOCKED_RATE_WARN,
    _RELIABILITY_SKIPPED_RATE_WARN,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Alert severity ordering (lower number = higher priority in queue)

_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}

# Hardcoded thresholds (PR1 — alert thresholds are NOT controlled by
# targets.md; targets.md controls SCORING targets (spec/53 PR2). Alert
# threshold tunability is a separate follow-up.)
_COST_SPIKE_THRESHOLD_MULT = 3.0  # alert when daily cost > N × baseline
_COST_SPIKE_MIN_BASELINE_DAYS = 7  # need at least N days of history
_QUALITY_REGRESSION_THRESHOLD = -0.10  # alert when delta_30d <= -0.10 raw rubric points
# Note: -0.10 on the 1-5 rubric scale = 2.5 percentage-points, NOT 10%.
# A semantically 10%-equivalent threshold would be -0.40 raw points.
# Changing the alert-firing value is a separate follow-up (scope: out of #690).


# Alert key version prefix — increment when normalization logic changes
# so old sidecar state (referencing old keys) is visibly invalidated.
_KEY_VERSION = "v1"


# ──────────────────────────────────────────────────────────────────
# Data structures


@dataclass
class AlertItem:
    """One item in the Operator Attention Queue."""

    alert_key: str  # stable hash — used for ack/snooze
    agent: str  # agent id
    alert_class: (
        str  # "governance" | "cost_spike" | "quality_regression" | "reliability"
    )
    alert_subclass: str  # more specific: "NO_GOVERNANCE", "GOVERNANCE_INVALID", etc.
    severity: str  # "critical" | "high" | "medium" | "low" | "info"
    reason: str  # human-readable reason (transients stripped from alert_key, not here)
    owner: str | None  # from governance.owner, for routing
    next_step: str  # what the operator should do
    status: str  # "new" | "recurring" | "known" (derived from sidecar state)
    ack_snooze_status: str  # "open" | "acked" | "snoozed"
    severity_rank: int = field(init=False)

    def __post_init__(self):
        self.severity_rank = _SEVERITY_RANK.get(self.severity, 99)


@dataclass
class CostTrendPoint:
    """One agent's cost data for the cost-trend axis."""

    agent: str
    total_usd_30d: float
    avg_daily_usd: float
    spike_detected: bool
    baseline_avg_daily: (
        float | None
    )  # prior 30-day baseline, None if insufficient history
    # ISO-day + USD pairs for the last 30 calendar days (sparse — missing days omitted).
    # Sorted ascending by ISO day string. Source: daily_30d dict in aggregate_console.
    # Empty list is the absent sentinel (no data or first-run). Used by sparklines.
    # The 7d slice is daily_series[-7:] (applied at render time, not aggregation time).
    daily_series: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class QualitySignal:
    """Minimal quality signal for the quality-trend axis."""

    agent: str
    latest_score: float | None
    delta_30d: float | None


@dataclass
class ConsoleData:
    """Aggregated data for the Fleet Console home page."""

    attention_queue: list[AlertItem]  # ranked: severity then agent name
    cost_trends: list[CostTrendPoint]
    quality_signals: list[QualitySignal]
    reliability_metrics: list[ReliabilityMetrics]
    # Alert keys from the attention-queue aggregation. Historically this fed the
    # sidecar directly; under panelization (spec/52 §16.3 MUST 17) it is NO LONGER
    # the sidecar source. The sidecar (for MUST 4 validation) is the ENGINE-union of
    # every PanelResult.alert_keys — the attention-queue PANEL recomputes its keys
    # from the same queue and contributes them to that union, so this field is now an
    # informational byproduct of aggregation, not the authoritative allowlist. It is
    # intentionally NOT OR'd into the sidecar (doing so would defeat MUST 17's
    # strip-RED — see render_console()).
    rendered_alert_keys: frozenset[str]
    agent_count: int
    degraded: bool  # True if any backend read degraded
    # Fleet Health Score (spec/53 PR2) — None until compute_fleet_health() runs.
    # Populated by render_console() / render_all() BEFORE _render_console_template().
    # The template renders the header band when this is not None; absent = no band,
    # not a crash (fail-soft, spec/53 §8).
    fleet_health: object | None = None  # FleetHealth | None (avoid circular import)
    # Recommendations (spec/54 PR3) — None until recommend_fleet() runs.
    # Populated by render_console() BEFORE _render_console_template().
    # The template renders a recommendations panel when this is not None; absent = no
    # panel (fail-soft, spec/54 §11 — ConsoleData + render integration).
    recommendations: list | None = None  # list[Recommendation] | None (avoid circular)
    # Per-agent most-recent primary-run timestamp (agent_id -> datetime | None).
    # Populated by aggregate_console() from the already-loaded runs_30d.
    # The fleet-status panel reads this for status_for_agent() (MUST 13 — no render-time
    # backend I/O from panels). Default empty dict is backward-compatible with all
    # existing construction sites.
    last_primary_runs: dict[str, datetime | None] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────
# Alert key derivation (spec/52 MUST 7)


def _make_alert_key(agent_id: str, alert_class: str, reason_bucket: str) -> str:
    """Derive a stable, process-consistent alert_key.

    Uses SHA-256 with NUL separator (spec/48 MUST 11 pattern from MEMORY.md).
    The reason_bucket MUST NOT include transient specifics (run_id, timestamps,
    exact dollar amounts) — normalize those out before calling this function.
    The v1: prefix allows future normalization changes to invalidate old sidecar keys.
    """
    canonical = "\x00".join([_KEY_VERSION, agent_id, alert_class, reason_bucket])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{_KEY_VERSION}:{digest}"


# ──────────────────────────────────────────────────────────────────
# Governance alert generation


def _governance_alerts(
    agents_root: Path,
    alert_state: dict[str, dict],
) -> list[AlertItem]:
    """Generate governance alerts from AgentRegistry.list_agents(include_governance=True).

    Alert classes (spec/52 §2.2):
      NO_GOVERNANCE: governance.md absent
      GOVERNANCE_INVALID: governance.md present, parse_errors non-empty
      GOVERNANCE_INCOMPLETE: governance.md present, valid, but owner=None
      GOVERNANCE_NO_BLOCK: governance.md present + readable, no `governance:` block

    NOTE: 'overdue review by date' DEFERRED to PR2 (no review_cadence field exists).
    """
    from ..agent_registry import (
        FilesystemAgentRegistryBackend,
        get_default_agent_registry_backend,
    )

    alerts: list[AlertItem] = []
    try:
        backend = get_default_agent_registry_backend(agents_root)
        refs = backend.list_agents(include_governance=True)
    except Exception as exc:
        logger.warning(
            "governance alert generation: registry failed (%s); trying filesystem fallback",
            type(exc).__name__,
        )
        try:
            backend = FilesystemAgentRegistryBackend(agents_root)
            refs = backend.list_agents(include_governance=True)
        except Exception as fallback_exc:
            logger.warning(
                "governance alert generation: filesystem fallback also failed (%s); skipping",
                type(fallback_exc).__name__,
            )
            return []

    for ref in refs:
        agent_id = ref.id
        owner = None

        if not ref.has_governance:
            alert_subclass = "NO_GOVERNANCE"
            reason_bucket = "no_governance_file"
            severity = "high"
            reason = (
                "No governance.md — agent has no declared owner or permission tier."
            )
            next_step = "Create governance.md for this agent (run `atomic-agents init` or copy the template)."
        elif ref.governance is not None and ref.governance.parse_errors:
            alert_subclass = "GOVERNANCE_INVALID"
            reason_bucket = "governance_parse_error"
            severity = "high"
            reason = f"governance.md is present but invalid: {ref.governance.parse_errors[0]}"
            next_step = (
                "Fix the YAML block in governance.md. Check for unknown enum values."
            )
        elif ref.governance is not None and ref.governance.owner is None:
            alert_subclass = "GOVERNANCE_INCOMPLETE"
            reason_bucket = "no_owner_field"
            severity = "medium"
            reason = "governance.md is present and valid, but the owner field is empty."
            next_step = (
                "Add an owner field to governance.md: `owner: your-team-or-name`"
            )
            owner = None
        elif ref.has_governance and ref.governance is None:
            # PRESENT_NO_BLOCK (registry five-state model): governance.md exists and
            # is readable but has NO `governance:` YAML block, so there is no
            # structured record. Without this branch the row falls through to the
            # `else: continue` below and is SILENTLY un-surfaced — a broken /
            # prose-only governance.md is the highest-priority alert class but
            # would emit zero alerts (#614 P2; spec/52 §2.2).
            alert_subclass = "GOVERNANCE_NO_BLOCK"
            reason_bucket = "governance_no_block"
            severity = "high"
            reason = "governance.md present but has no governance: YAML block."
            next_step = "Add the YAML block from the init template (the `governance:` block with owner/permission_tier/...)."
        else:
            # governance.md present, valid, has owner — no governance alert
            continue

        if ref.governance is not None:
            owner = ref.governance.owner  # may still be None for GOVERNANCE_INCOMPLETE

        alert_key = _make_alert_key(
            agent_id, f"governance.{alert_subclass}", reason_bucket
        )
        ack_status = alert_state.get(alert_key, {}).get("status", "open")
        queue_status = _derive_queue_status(alert_key, alert_state)

        alerts.append(
            AlertItem(
                alert_key=alert_key,
                agent=agent_id,
                alert_class="governance",
                alert_subclass=alert_subclass,
                severity=severity,
                reason=reason,
                owner=owner,
                next_step=next_step,
                status=queue_status,
                ack_snooze_status=ack_status,
            )
        )

    return alerts


# ──────────────────────────────────────────────────────────────────
# Reliability axis
# _compute_reliability, _is_primary_run, ReliabilityMetrics, and the
# _RELIABILITY_* constants are imported from ._reliability (shared with
# advisor/score.py so both surfaces use the exact same computation).


def _reliability_alerts(
    agent: str,
    metrics: ReliabilityMetrics,
    alert_state: dict[str, dict],
) -> list[AlertItem]:
    """Generate reliability alerts for one agent based on its metrics."""
    alerts: list[AlertItem] = []

    if metrics.total_runs == 0:
        return alerts

    if metrics.error_rate >= _RELIABILITY_ERROR_RATE_WARN:
        pct = int(metrics.error_rate * 100)
        reason_bucket = "high_error_rate"
        alert_key = _make_alert_key(agent, "reliability.high_error_rate", reason_bucket)
        ack_status = alert_state.get(alert_key, {}).get("status", "open")
        queue_status = _derive_queue_status(alert_key, alert_state)
        alerts.append(
            AlertItem(
                alert_key=alert_key,
                agent=agent,
                alert_class="reliability",
                alert_subclass="high_error_rate",
                severity="high" if metrics.error_rate >= 0.5 else "medium",
                reason=f"{pct}% of runs ended in error over the last 30 days.",
                owner=None,
                next_step="Check recent run logs for failure patterns. Review the agent's error handling.",
                status=queue_status,
                ack_snooze_status=ack_status,
            )
        )

    if metrics.blocked_rate >= _RELIABILITY_BLOCKED_RATE_WARN:
        pct = int(metrics.blocked_rate * 100)
        reason_bucket = "high_blocked_rate"
        alert_key = _make_alert_key(
            agent, "reliability.high_blocked_rate", reason_bucket
        )
        ack_status = alert_state.get(alert_key, {}).get("status", "open")
        queue_status = _derive_queue_status(alert_key, alert_state)
        alerts.append(
            AlertItem(
                alert_key=alert_key,
                agent=agent,
                alert_class="reliability",
                alert_subclass="high_blocked_rate",
                severity="medium",
                reason=f"{pct}% of runs were blocked (lock contention or embed gate) over 30 days.",
                owner=None,
                next_step="Check for concurrent run contention or embed cost cap being hit frequently.",
                status=queue_status,
                ack_snooze_status=ack_status,
            )
        )

    # Cost-guardrail-blocked (skipped) rate. The primary/mid-loop cost gate in
    # agent.call() logs status="skipped"; these land in skipped_rate. A high
    # skipped rate means the agent keeps being refused at its cost cap — the
    # operator's clearest cost-block signal. We alert on the skipped axis
    # SEPARATELY from blocked_rate (lock/embed) so the operator sees a distinct
    # "raise the cap or cut the workload" next-step rather than a generic block.
    if metrics.skipped_rate >= _RELIABILITY_SKIPPED_RATE_WARN:
        pct = int(metrics.skipped_rate * 100)
        reason_bucket = "high_skipped_rate"
        alert_key = _make_alert_key(
            agent, "reliability.high_skipped_rate", reason_bucket
        )
        ack_status = alert_state.get(alert_key, {}).get("status", "open")
        queue_status = _derive_queue_status(alert_key, alert_state)
        alerts.append(
            AlertItem(
                alert_key=alert_key,
                agent=agent,
                alert_class="reliability",
                alert_subclass="high_skipped_rate",
                severity="medium",
                reason=f"{pct}% of runs were skipped at the cost guardrail over the last 30 days.",
                owner=None,
                next_step="The agent keeps hitting its cost cap. Raise the cap in model.md or reduce the workload triggering the runs.",
                status=queue_status,
                ack_snooze_status=ack_status,
            )
        )

    return alerts


# ──────────────────────────────────────────────────────────────────
# Cost spike detection


def _cost_spike_alert(
    agent: str,
    runs_30d: list[RunRecord],
    runs_prior_30d: list[RunRecord],
    alert_state: dict[str, dict],
) -> AlertItem | None:
    """Generate a cost-spike alert if the agent's recent daily cost is anomalous."""
    if not runs_30d:
        return None

    # Compute recent daily average
    daily: dict[str, float] = {}
    for r in runs_30d:
        day = r.ts.date().isoformat()
        daily[day] = daily.get(day, 0.0) + r.cost_usd
    recent_avg = sum(daily.values()) / max(len(daily), 1)

    if not runs_prior_30d:
        return None

    # Compute prior daily average for baseline
    prior_daily: dict[str, float] = {}
    for r in runs_prior_30d:
        day = r.ts.date().isoformat()
        prior_daily[day] = prior_daily.get(day, 0.0) + r.cost_usd

    # Baseline sufficiency is measured in DISTINCT DAYS of history, not run count
    # (spec/52 §"Cost spike minimum baseline: 7 days"). This MUST match the panel
    # gate in aggregate_console (len(prior_daily) >= N) so the attention-queue
    # alert and the Cost trend panel never disagree about whether a spike fired.
    if len(prior_daily) < _COST_SPIKE_MIN_BASELINE_DAYS:
        return None

    baseline_avg = sum(prior_daily.values()) / max(len(prior_daily), 1)

    if baseline_avg <= 0:
        return None

    if recent_avg < baseline_avg * _COST_SPIKE_THRESHOLD_MULT:
        return None

    reason_bucket = "cost_above_threshold"
    alert_key = _make_alert_key(agent, "cost_spike", reason_bucket)
    ack_status = alert_state.get(alert_key, {}).get("status", "open")
    queue_status = _derive_queue_status(alert_key, alert_state)

    return AlertItem(
        alert_key=alert_key,
        agent=agent,
        alert_class="cost_spike",
        alert_subclass="cost_above_threshold",
        severity="high",
        reason=f"Daily cost is {_COST_SPIKE_THRESHOLD_MULT:.0f}× above baseline.",
        owner=None,
        next_step="Review recent runs for unexpected activity. Consider tightening cost caps in model.md.",
        status=queue_status,
        ack_snooze_status=ack_status,
    )


# ──────────────────────────────────────────────────────────────────
# Quality regression alerts


def _quality_alerts(
    agent: str,
    signal: QualitySignal,
    alert_state: dict[str, dict],
) -> list[AlertItem]:
    """Generate quality regression alert if delta_30d is below threshold."""
    if signal.delta_30d is None or signal.latest_score is None:
        return []
    if signal.delta_30d > _QUALITY_REGRESSION_THRESHOLD:
        return []

    # delta_30d is a raw 1-5 rubric-scale difference; route through the
    # rubric-delta formatter so a 1-point drop says "25%", not "100%".
    drop_fmt = _eval_score_delta_fmt(abs(signal.delta_30d), scale="rubric")
    reason_bucket = "score_regression_threshold"
    alert_key = _make_alert_key(agent, "quality_regression", reason_bucket)
    ack_status = alert_state.get(alert_key, {}).get("status", "open")
    queue_status = _derive_queue_status(alert_key, alert_state)

    return [
        AlertItem(
            alert_key=alert_key,
            agent=agent,
            alert_class="quality_regression",
            alert_subclass="score_regression",
            severity="medium",
            reason=f"Eval score dropped {drop_fmt} over the last 30 days.",
            owner=None,
            next_step="Review recent eval runs. Consider tuning or prompt revision.",
            status=queue_status,
            ack_snooze_status=ack_status,
        )
    ]


# ──────────────────────────────────────────────────────────────────
# Status derivation from sidecar


def _derive_queue_status(alert_key: str, alert_state: dict[str, dict]) -> str:
    """Derive 'new' | 'recurring' | 'known' from sidecar state.

    new: never seen before (not in sidecar)
    recurring: was acked/snoozed before (snooze expired) and is back
    known: currently acked or snoozed
    """
    entry = alert_state.get(alert_key)
    if entry is None:
        return "new"
    ack_status = entry.get("status", "open")
    if ack_status in ("acked", "snoozed"):
        return "known"
    # status == "open" means it was unsnooze'd or snooze expired — recurring
    return "recurring"


# ──────────────────────────────────────────────────────────────────
# Quality signal reader (lightweight, avoids full quality aggregation)


def _read_quality_signals(
    agents_root: Path,
    agent_names: list[str],
    today: date,
) -> list[QualitySignal]:
    """Read quality signals from evals/runs/*.jsonl — lightweight per-agent reader.

    Returns a list of QualitySignal, one per agent that has eval data.
    Skips agents with no eval data (no signal to report).
    """
    signals: list[QualitySignal] = []
    thirty_days_ago = today - timedelta(days=30)
    sixty_days_ago = today - timedelta(days=60)

    for agent in agent_names:
        evals_dir = agents_root / agent / "evals" / "runs"
        if not evals_dir.exists():
            signals.append(
                QualitySignal(agent=agent, latest_score=None, delta_30d=None)
            )
            continue

        recent_scores: list[tuple[str, float]] = []
        prior_scores: list[tuple[str, float]] = []

        for jf in sorted(evals_dir.glob("*.jsonl")):
            try:
                for line in jf.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    ts_str = rec.get("ts", "")
                    score = rec.get("weighted_score")
                    if ts_str and score is not None:
                        try:
                            ts_date = datetime.fromisoformat(ts_str).date()
                        except (ValueError, TypeError):
                            continue
                        if ts_date >= thirty_days_ago:
                            recent_scores.append((ts_str, float(score)))
                        elif ts_date >= sixty_days_ago:
                            prior_scores.append((ts_str, float(score)))
            except (OSError, json.JSONDecodeError):
                continue

        if not recent_scores:
            signals.append(
                QualitySignal(agent=agent, latest_score=None, delta_30d=None)
            )
            continue

        recent_scores.sort(key=lambda x: x[0])
        latest_score = recent_scores[-1][1]
        delta_30d = None
        if prior_scores:
            prior_latest = max(prior_scores, key=lambda x: x[0])[1]
            delta_30d = round(latest_score - prior_latest, 4)

        signals.append(
            QualitySignal(
                agent=agent,
                latest_score=round(latest_score, 4),
                delta_30d=delta_30d,
            )
        )

    return signals


# ──────────────────────────────────────────────────────────────────
# Top-level aggregation


def aggregate_console(
    agents_root: Path,
    today: date | None = None,
    quality_signals: list[QualitySignal] | None = None,
) -> ConsoleData:
    """Build the ConsoleData for the Fleet Console home page.

    quality_signals: optional pre-aggregated quality data from render_all()
        to avoid a second evals/ read on full renders. Pass None for standalone
        console-only renders (will do a lightweight direct read).

    Fail-soft: backend errors degrade individual sections rather than crashing
    the whole render (Principle #8).
    """
    today = today or date.today()
    now = datetime.now(tz=timezone.utc)
    thirty_days_ago = today - timedelta(days=30)
    sixty_days_ago = today - timedelta(days=60)

    agent_names = discover_agents(agents_root)
    degraded = False

    # Read alert sidecar state (fail-soft: returns {} on missing/unreadable)
    try:
        alert_state = read_alert_state(agents_root)
    except Exception:
        logger.warning("alert_state read failed; proceeding with empty state")
        alert_state = {}

    # Governance alerts
    gov_alerts = []
    try:
        gov_alerts = _governance_alerts(agents_root, alert_state)
    except Exception as exc:
        logger.warning("governance alert generation failed (%s)", type(exc).__name__)
        degraded = True

    # Quality signals
    if quality_signals is None:
        try:
            quality_signals = _read_quality_signals(agents_root, agent_names, today)
        except Exception as exc:
            logger.warning("quality signal read failed (%s)", type(exc).__name__)
            quality_signals = []
            degraded = True

    # Per-agent cost, reliability, and operational alerts
    all_alerts: list[AlertItem] = list(gov_alerts)
    cost_trends: list[CostTrendPoint] = []
    reliability_metrics: list[ReliabilityMetrics] = []
    last_primary_runs: dict[str, datetime | None] = {}

    quality_by_agent = {s.agent: s for s in (quality_signals or [])}

    for agent in agent_names:
        # Load runs for recent 30d and prior 30d windows.
        # Use the SAME private helper aggregate_global uses (costs.py:541-547) so a
        # blind LogBackend read surfaces its degraded flag here. The public
        # load_runs() swallows LogBackendReadError → [] and DISCARDS the flag, which
        # would leave a failing fleet rendering as "all healthy / $0.00" with the
        # degraded banner suppressed (spec/52 §9; #614 P1).
        try:
            runs_30d, deg_recent = _load_runs_with_degraded(
                agents_root, agent, thirty_days_ago, today
            )
            runs_prior_30d, deg_prior = _load_runs_with_degraded(
                agents_root, agent, sixty_days_ago, thirty_days_ago - timedelta(days=1)
            )
            degraded = degraded or deg_recent or deg_prior
        except Exception as exc:
            logger.warning(
                "run load failed for agent %s (%s); skipping metrics",
                agent,
                type(exc).__name__,
            )
            degraded = True
            runs_30d = []
            runs_prior_30d = []

        # Reliability axis
        metrics = _compute_reliability(runs_30d, agent)

        # Populate the display-window counts for spec/56 MUST 9 columns.
        # errors_24h: primary runs with status=error in the last 24h.
        # failures_7d: primary runs that were blocked in the last 7d.
        # Both filter the already-loaded runs_30d — no extra disk I/O (MUST 13).
        #
        # Tz normalisation: r.ts may be tz-naive (JSONL without offset) or
        # tz-aware (JSONL with +00:00). Treat naive as UTC — same convention
        # as _monitor_roster._relative_time().
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)

        def _ts_aware(ts: datetime) -> datetime:
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)

        metrics.errors_24h = sum(
            1
            for r in runs_30d
            if _is_primary_run(r)
            and r.status in _RELIABILITY_ERROR_STATUSES
            and _ts_aware(r.ts) >= cutoff_24h
        )
        metrics.failures_7d = sum(
            1
            for r in runs_30d
            if _is_primary_run(r)
            and (
                r.status in _RELIABILITY_BLOCKED_STATUSES
                or getattr(r, "extra", {}).get("embed_batch_blocked", False) is True
            )
            and _ts_aware(r.ts) >= cutoff_7d
        )

        reliability_metrics.append(metrics)
        all_alerts.extend(_reliability_alerts(agent, metrics, alert_state))

        # Last primary run timestamp — for status_for_agent() STALE detection.
        # Computed from the already-loaded runs_30d so panels need not re-read disk
        # (spec/52 §17.1 MUST 13 + MUST 18). None = no primary runs in window.
        last_primary_runs[agent] = max(
            (r.ts for r in runs_30d if _is_primary_run(r)),
            default=None,
        )

        # Cost trend
        total_30d = sum(r.cost_usd for r in runs_30d)
        daily_30d: dict[str, float] = {}
        for r in runs_30d:
            day = r.ts.date().isoformat()
            daily_30d[day] = daily_30d.get(day, 0.0) + r.cost_usd
        avg_daily = total_30d / max(len(daily_30d), 1) if daily_30d else 0.0

        prior_daily: dict[str, float] = {}
        for r in runs_prior_30d:
            day = r.ts.date().isoformat()
            prior_daily[day] = prior_daily.get(day, 0.0) + r.cost_usd
        baseline_avg = (
            sum(prior_daily.values()) / max(len(prior_daily), 1)
            if prior_daily
            else None
        )
        spike = (
            baseline_avg is not None
            and len(prior_daily) >= _COST_SPIKE_MIN_BASELINE_DAYS
            and avg_daily >= baseline_avg * _COST_SPIKE_THRESHOLD_MULT
        )
        cost_trends.append(
            CostTrendPoint(
                agent=agent,
                total_usd_30d=round(total_30d, 4),
                avg_daily_usd=round(avg_daily, 4),
                spike_detected=spike,
                baseline_avg_daily=round(baseline_avg, 4)
                if baseline_avg is not None
                else None,
                # Populate daily_series from the daily_30d dict (ISO-day → USD, sparse,
                # ascending). The 7d sparkline slices daily_series[-7:] at render time.
                # Option B ruling: missing days omitted (sparse), 30d window.
                daily_series=sorted(daily_30d.items()),
            )
        )

        # Cost spike alert
        try:
            cost_alert = _cost_spike_alert(agent, runs_30d, runs_prior_30d, alert_state)
            if cost_alert:
                all_alerts.append(cost_alert)
        except Exception as exc:
            logger.warning(
                "cost spike alert for %s failed (%s)", agent, type(exc).__name__
            )

        # Quality regression alert
        sig = quality_by_agent.get(agent)
        if sig:
            all_alerts.extend(_quality_alerts(agent, sig, alert_state))

    # Rank: severity first, then agent name (stable sort)
    all_alerts.sort(key=lambda a: (a.severity_rank, a.agent))

    rendered_keys = frozenset(a.alert_key for a in all_alerts)

    return ConsoleData(
        attention_queue=all_alerts,
        cost_trends=sorted(cost_trends, key=lambda c: -c.total_usd_30d),
        quality_signals=quality_signals or [],
        reliability_metrics=reliability_metrics,
        rendered_alert_keys=rendered_keys,
        agent_count=len(agent_names),
        degraded=degraded,
        last_primary_runs=last_primary_runs,
    )

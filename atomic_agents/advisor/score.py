"""Fleet Health Scoring Engine — pure-compute module (spec/53).

Computes a Fleet Health Score (0-100) decomposed into three sub-scores:
  - Cost axis: spend-vs-trend ONLY (#687, spec/53 §3.6 + MUST 14).
    cheaper_model_share and tokens_per_output are computed as VALUES for the
    recommendations engine (spec/54) but are NOT health metrics — they do not
    enter the Cost sub-score, the composite, or the critical cap.
  - Quality axis: pass-rate, hard-fail-rate (from evals/runs/*.jsonl verdict field)
  - Reliability axis: error-rate, blocked-rate, skipped-rate

IMPORT DISCIPLINE (spec/53 MUST 10):
  This module's OWN code imports only from:
    - stdlib
    - atomic_agents.dashboard._reliability (shared reliability computations)
    - atomic_agents.dashboard.costs (_load_runs_with_degraded, discover_agents)
    - atomic_agents._costs (PRICING)
    - .targets (FleetTargets, parse_targets, constants)
  Its own code NEVER directly imports agent.py, eval.py, tuning.py, dream.py,
  dashboard.attention, or dashboard.render.

  IMPORTANT — what the no-LLM guarantee actually is:
    The load-bearing guarantee is that NO LLMBackend is ever CONSTRUCTED and NO
    LLM CALL is ever made on any advisor code path (zero LLM spend). It is NOT a
    claim of sys.modules isolation. Importing ANY atomic_agents submodule runs
    the package __init__ (atomic_agents/__init__.py: `from .agent import AtomicAgent`,
    `from .dream import ...`), which transitively loads agent.py / eval.py / dream.py
    as module DEFINITIONS — same as the pre-existing dashboard.costs dependency this
    module legitimately uses. No LLMBackend is constructed, no .messages.create is
    called, no network I/O, no spend at import time. The conftest guard in
    tests/advisor/conftest.py enforces the real guarantee: it raises if any
    LLMBackend.__init__ runs during an advisor test.

Verify the SOURCE-LEVEL discipline (advisor's own imports, not sys.modules):
  grep -r 'from.*agent import\\|from.*eval import\\|from.*tuning import\\|from.*dream import' \\
       atomic_agents/advisor/
  Returns empty (advisor's own code is clean). This greps source imports, not the
  process import graph — agent/eval/dream are loaded by the package __init__.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .._costs import PRICING
from ..dashboard._reliability import (
    ReliabilityMetrics,
    _compute_reliability,
    _is_primary_run,
)
from ..dashboard.costs import _load_runs_with_degraded, discover_agents, RunRecord
from .targets import (
    FleetTargets,
    MetricTarget,
    parse_targets,
    CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M,
    CRITICAL_SUBSCORE_THRESHOLD,
    CRITICAL_COMPOSITE_CAP,
    BAND_GREEN_MIN,
    BAND_AMBER_MIN,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Public API dataclasses (spec/53 §2)


@dataclass
class ScorecardRow:
    """One row in the per-agent scorecard table."""

    metric: str  # e.g. "error_rate"
    axis: str  # "cost" | "quality" | "reliability"
    value: float | None  # actual measured value; None = no data
    target: float | None  # target from targets.md
    status: str  # "ok" | "warn" | "crit" | "no_data" | "degraded"
    score: float | None  # 0-100 sub-score for this metric; None = no data/degraded
    wow: (
        str | None
    )  # WoW direction arrow: "up" | "down" | "flat" | None (per spec/53 §6)


@dataclass
class AgentHealth:
    """Health scores for one agent."""

    agent: str

    # Sub-scores (float 0-100) or None (no data / degraded)
    cost_score: float | None = None
    quality_score: float | None = None
    reliability_score: float | None = None

    # Composite (0-100) or None (insufficient data)
    composite: float | None = None
    band: str = "unknown"  # "green" | "amber" | "red" | "unknown"

    # Canonical display integer — int(round(composite)) AFTER the critical-cap
    # override. Bands are assigned from this int, not from the raw float (#623 fix,
    # spec/53 §3.3 + MUST 11). None when composite is None.
    composite_display: int | None = None

    # Per-axis degraded flags (axis-granularity, spec/53 MUST 8)
    cost_degraded: bool = False
    quality_degraded: bool = False
    reliability_degraded: bool = False

    # Whether ANY axis was degraded
    degraded: bool = False

    # Coverage: how many of 3 axes have data
    axes_with_data: int = 0

    # Scorecard rows (always emitted — decomposition always visible)
    scorecard: list[ScorecardRow] = field(default_factory=list)

    # If the composite was capped by a critical sub-score, name the axis
    capped_by_axis: str | None = None

    # Primary model (most common model in recent 30d primary runs; None = no runs).
    # Populated by compute_fleet_health for the recommendations engine (#616).
    primary_model: str | None = None


@dataclass
class FleetHealth:
    """Fleet-level health roll-up."""

    # Per-agent health records
    agents: list[AgentHealth] = field(default_factory=list)

    # Fleet headline composite: min(mean(agent_composites), worst_agent_composite).
    # The worst-agent value is a CEILING on the headline (spec/53 §7) — it prevents
    # a few bad agents from being hidden behind a healthy mean.
    fleet_composite: float | None = None
    fleet_band: str = "unknown"

    # Canonical display integers — int(round(v)) AFTER the critical-cap override
    # (#623 fix, spec/53 §3.3 + MUST 11). None when the corresponding composite is None.
    # Bands are assigned from these ints; render.py uses them directly (no {:.0f}).
    fleet_composite_display: int | None = None
    worst_agent_composite_display: int | None = None

    # Worst agent (lowest per-agent composite)
    worst_agent: str | None = None
    worst_agent_composite: float | None = None

    # Coverage: agents with all 3 axes populated / total agents
    coverage_n: int = 0
    coverage_m: int = 0

    # Degraded flag (OR-composition of all per-agent degraded flags)
    degraded: bool = False

    # Whether any defaults were used in targets parsing
    used_targets_defaults: bool = False
    targets_defaults_keys: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Scoring curve (spec/53 MUST 1)


def _map_metric_to_score(
    value: float,
    target: MetricTarget,
) -> float:
    """Piecewise plateau-at-target curve (spec/53 MUST 1).

    Rules (band-inclusive plateau — matches spec/53 §3.2):
    - direction='higher': score=100 when value >= target - band; linear decay to 0 at floor.
    - direction='lower': score=100 when value <= target + band; linear decay to 0 at floor.
    - The 'band' field is the tolerance around the target (still scores 100).
    - Output is clamped to [0, 100].
    - floor == target guard: prevents ZeroDivisionError (caught in targets.py).

    For 'lower' direction, we invert by treating target+band as the zero-loss
    boundary and floor as the worst-allowed value (above which score = 0).
    """
    t = target.target
    b = target.band
    f = target.floor

    if target.direction == "higher":
        # plateau (score=100) starts at lower = t - b; decay to 0 at floor (below target)
        lower = t - b
        if value >= lower:
            return 100.0
        # Linear decay from lower bound to floor
        span = lower - f
        if span <= 1e-9:
            return 0.0
        return max(0.0, min(100.0, (value - f) / span * 100.0))
    else:
        # direction = 'lower': score=100 when value <= (t + b)
        # decay to 0 at floor (above target)
        upper = t + b  # plateau: anything at or below target+band scores 100
        if value <= upper:
            return 100.0
        # Linear decay from upper bound (target+band) to floor
        span = f - upper
        if span <= 1e-9:
            return 0.0
        return max(0.0, min(100.0, (f - value) / span * 100.0))


# ──────────────────────────────────────────────────────────────────
# Band derivation (spec/53 §3.3)


def _band(score: float) -> str:
    if score >= BAND_GREEN_MIN:
        return "green"
    if score >= BAND_AMBER_MIN:
        return "amber"
    return "red"


# ──────────────────────────────────────────────────────────────────
# Composite roll-up with critical-axis cap (spec/53 MUST 4)


def _compute_composite(
    sub_scores: dict[str, float | None],
    weights: dict[str, float],
    metric_scores: list[float] | None = None,
) -> tuple[float | None, str, str | None]:
    """Weighted mean over present sub-scores + critical cap (axis + metric).

    Returns (composite, band, capped_by_axis).
    composite is None if no sub-scores are present.

    The cap fires AFTER the weighted mean (not before):
    1. Compute weighted mean over present (non-None) axes.
    2. If any axis sub-score < CRITICAL_SUBSCORE_THRESHOLD OR any individual
       metric score < CRITICAL_SUBSCORE_THRESHOLD:
       composite = min(composite, CRITICAL_COMPOSITE_CAP)
       band = 'red' (forced regardless of composite value)

    The METRIC-level cap (spec/53 §3.5) is the load-bearing safety property.
    Each axis sub-score is the unweighted MEAN of its metrics, so a single
    catastrophic metric (e.g. error_rate=0.90 → metric score 0) can be diluted
    by healthy siblings (blocked=100, skipped=100 → reliability axis = 66.7,
    NOT critical). Without the metric-level check an agent failing 90% of its
    runs would read green. ANY single critical signal must force the cap;
    averaging cannot hide it. Per-axis chips still show their own (uncapped)
    truth — decomposition stays visible (MUST 5).

    metric_scores: flat list of EVERY present scorecard metric score (0-100)
    across all axes. When None, only the axis-level check runs (used by direct
    unit tests of the composite math that pass axis sub-scores only).
    """
    present = [(axis, s) for axis, s in sub_scores.items() if s is not None]
    if not present:
        return None, "unknown", None

    weight_sum = sum(weights.get(a, 1.0 / 3.0) for a, _ in present)
    if weight_sum <= 0:
        weight_sum = len(present)

    composite = sum(weights.get(a, 1.0 / 3.0) * s for a, s in present) / weight_sum
    composite = max(0.0, min(100.0, composite))

    # Critical cap: applied POST-computation (spec/53 MUST 4 + §3.5).
    capped_by_axis: str | None = None
    # (a) axis-level: any axis sub-score below threshold.
    for axis, s in present:
        if s < CRITICAL_SUBSCORE_THRESHOLD:
            if capped_by_axis is None:
                capped_by_axis = axis
            composite = min(composite, float(CRITICAL_COMPOSITE_CAP))
    # (b) metric-level: any individual metric score below threshold — fires
    # even when the axis MEAN dilutes it above threshold (the safety bug).
    if metric_scores is not None and any(
        ms < CRITICAL_SUBSCORE_THRESHOLD for ms in metric_scores
    ):
        if capped_by_axis is None:
            capped_by_axis = "metric"
        composite = min(composite, float(CRITICAL_COMPOSITE_CAP))

    # Band the DISPLAY INTEGER so the shown number and its color always agree.
    # int(round(composite)) is the canonical display integer (#623 fix, spec/53 §3.3).
    # NOTE: Python's round() uses round-half-to-even (banker's rounding), so .5
    # does NOT always round up — int(round(78.5))==78 but int(round(79.5))==80.
    # At the band thresholds (60, 80) the even neighbor happens to be the
    # hundred-side, so raw 79.5 → 80 → green and raw 59.5 → 60 → amber; raw 79.49
    # → 79 → amber and raw 59.49 → 59 → red. Bands are derived from the int, never
    # from a {:.0f} format of the raw float.
    # Keep composite as the float value for math/history; band is derived from the int.
    display_int = int(round(composite))
    computed_band = "red" if capped_by_axis else _band(display_int)
    return composite, computed_band, capped_by_axis


# ──────────────────────────────────────────────────────────────────
# Work-type classification (spec/53 §5.4 — for Cost axis only)

# Triggers that identify a coordinator/delegating run.
_DELEGATION_TRIGGERS = frozenset({"cron", "schedule", "mcp", "serve", "queue"})


def _classify_work_type(r: RunRecord) -> str:
    """Deterministic per-run work-type label via precedence ladder (spec/53 §5.4).

    PROVIDED-BUT-NOT-YET-WIRED (spec/53 §5.4): this classifier ships as a tested,
    deterministic primitive for the PR3 recommendations work (#616), which will
    annotate cheaper_model_share by work-type. PR2's cheaper_model_share
    computation does NOT consume it (see _score_cost_axis). Tracked at #620.
    Not used for reliability or quality axes.

    Precedence:
    1. Has parent_run_id → 'child'
    2. trigger in delegation triggers → 'coordinator'
    3. extra.delegations non-empty → 'coordinator'
    4. extra.tool_calls non-empty → 'tool-heavy'
    5. else → 'general'
    """
    if getattr(r, "parent_run_id", None):
        return "child"
    delegations = r.extra.get("delegations") or []
    tool_calls = r.extra.get("tool_calls") or []
    if r.trigger in _DELEGATION_TRIGGERS:
        return "coordinator"
    if delegations:
        return "coordinator"
    if tool_calls:
        return "tool-heavy"
    return "general"


# ──────────────────────────────────────────────────────────────────
# Model-tier classification (spec/53 §5.1)


def _is_cheap_model(model_id: str) -> bool:
    """True if model's output rate is STRICTLY BELOW the spec-stated cutoff.

    Cutoff: CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M (spec/53 normative constant).
    Ties at the exact cutoff are classified NOT cheap (fail-pessimistic).
    Unknown model_id → classified NOT cheap (fail-pessimistic, spec/53 MUST 9):
    returns False without consulting any rate or fallback sentinel.
    """
    if model_id not in PRICING:
        # Unknown model → fail-pessimistic (not cheap). No rate is consulted.
        return False
    output_rate = PRICING[model_id]["output"]
    # Strict less-than: ties are NOT cheap (fail-pessimistic, spec/53 MUST 9)
    return output_rate < CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M


# ──────────────────────────────────────────────────────────────────
# Eval record reader (spec/53 §5.3 — reads verdict directly from JSONL)
# Does NOT import from atomic_agents.eval — reads JSONL files directly.


@dataclass
class _EvalRecord:
    """Minimal eval run record for quality sub-score computation."""

    ts_date: date
    verdict: str  # 'pass' | 'fail' | 'judge_error' | ''
    hard_fails: list  # list of hard-fail strings
    weighted_score: float


def _load_eval_records(
    agents_root: Path,
    agent: str,
    since: date,
    until: date,
) -> list[_EvalRecord]:
    """Read evals/runs/*.jsonl for one agent in [since, until].

    Reads 'verdict' field directly (written by eval.py._write_run_log).
    Old records without 'verdict' → treated as 'judge_error' (excluded from
    pass-rate denominator per spec/53 §5.3 — not scorable without a verdict).

    Does NOT import or call EvalRunner — zero LLM spend.
    """
    runs_dir = agents_root / agent / "evals" / "runs"
    if not runs_dir.exists():
        return []

    records: list[_EvalRecord] = []
    for path in sorted(runs_dir.glob("*.jsonl")):
        try:
            stem_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if stem_date < since or stem_date > until:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = rec.get("ts") or path.stem
            try:
                ts_d = datetime.fromisoformat(ts_str[:10]).date()
            except (ValueError, TypeError):
                ts_d = (
                    path.stem[:10] and date.fromisoformat(path.stem[:10])
                    if len(path.stem) >= 10
                    else since
                )
            verdict = str(rec.get("verdict", "")).strip()
            if not verdict:
                verdict = "judge_error"
            hard_fails = list(rec.get("hard_fails") or [])
            weighted = float(rec.get("weighted_score", 0.0) or 0.0)
            records.append(
                _EvalRecord(
                    ts_date=ts_d,
                    verdict=verdict,
                    hard_fails=hard_fails,
                    weighted_score=weighted,
                )
            )

    return records


# ──────────────────────────────────────────────────────────────────
# WoW window helpers (spec/53 §6)


def _wow_arrow(
    current: float | None, prior: float | None, threshold: float = 0.01
) -> str | None:
    """Derive WoW direction string or None if insufficient data."""
    if current is None or prior is None:
        return None
    delta = current - prior
    if delta > threshold:
        return "up"
    if delta < -threshold:
        return "down"
    return "flat"


def _metric_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


# ──────────────────────────────────────────────────────────────────
# Per-axis scorers


def _score_reliability_axis(
    runs_30d: list[RunRecord],
    runs_7d: list[RunRecord],
    runs_prior_7d: list[RunRecord],
    agent: str,
    targets: FleetTargets,
) -> tuple[float | None, list[ScorecardRow]]:
    """Compute reliability sub-score (0-100) and scorecard rows.

    Returns (sub_score, scorecard_rows). sub_score=None if no runs.
    """
    metrics = _compute_reliability(runs_30d, agent)

    # WoW windows reuse the SAME canonical predicate set via _compute_reliability
    # (per _reliability.py: do NOT copy the predicate logic). Each window's
    # ReliabilityMetrics carries .error_rate / .blocked_rate / .skipped_rate; an
    # empty window yields total_runs == 0 → no WoW signal for that window.
    cur_metrics = _compute_reliability(runs_7d, agent)
    pri_metrics = _compute_reliability(runs_prior_7d, agent)

    def _wow_rate(m: ReliabilityMetrics, metric: str) -> float | None:
        if m.total_runs == 0:
            return None
        return getattr(m, metric)

    if metrics.total_runs == 0:
        return None, [
            ScorecardRow(
                metric=m,
                axis="reliability",
                value=None,
                target=None,
                status="no_data",
                score=None,
                wow=None,
            )
            for m in ("error_rate", "blocked_rate", "skipped_rate")
        ]

    rel_targets = targets.axes.get("reliability", {})

    rows: list[ScorecardRow] = []
    metric_scores: list[float] = []

    for metric_name, value in [
        ("error_rate", metrics.error_rate),
        ("blocked_rate", metrics.blocked_rate),
        ("skipped_rate", metrics.skipped_rate),
    ]:
        mt = rel_targets.get(metric_name)
        if mt is None:
            rows.append(
                ScorecardRow(
                    metric=metric_name,
                    axis="reliability",
                    value=value,
                    target=None,
                    status="no_data",
                    score=None,
                    wow=None,
                )
            )
            continue
        s = _map_metric_to_score(value, mt)
        metric_scores.append(s)

        # WoW: this metric's rate over current 7d vs prior 7d, both derived
        # through the canonical _compute_reliability predicate (no inline copy).
        cur_val = _wow_rate(cur_metrics, metric_name)
        pri_val = _wow_rate(pri_metrics, metric_name)
        wow_dir = _wow_arrow(cur_val, pri_val)

        status = "ok" if s >= 80 else ("warn" if s >= 60 else "crit")
        rows.append(
            ScorecardRow(
                metric=metric_name,
                axis="reliability",
                value=round(value, 4),
                target=mt.target,
                status=status,
                score=round(s, 1),
                wow=wow_dir,
            )
        )

    sub_score = _metric_mean(metric_scores)
    return (round(sub_score, 1) if sub_score is not None else None), rows


def _score_quality_axis(
    eval_records: list[_EvalRecord],
    eval_records_7d: list[_EvalRecord],
    eval_records_prior_7d: list[_EvalRecord],
    targets: FleetTargets,
) -> tuple[float | None, list[ScorecardRow]]:
    """Compute quality sub-score (0-100) and scorecard rows.

    pass_rate = count(verdict=='pass') / count(verdict in {'pass','fail'})
    Excludes 'judge_error' from denominator (not scorable per spec/53 §5.3).
    Returns (None, rows) when no scorable records in window.
    """
    qual_targets = targets.axes.get("quality", {})

    def _compute_rates(records: list[_EvalRecord]) -> tuple[float | None, float | None]:
        scorable = [r for r in records if r.verdict in ("pass", "fail")]
        if not scorable:
            return None, None
        pass_rate = sum(1 for r in scorable if r.verdict == "pass") / len(scorable)
        hard_fail_rate = sum(1 for r in scorable if r.hard_fails) / len(scorable)
        return pass_rate, hard_fail_rate

    pass_rate, hard_fail_rate = _compute_rates(eval_records)

    if pass_rate is None:
        # No scorable records (zero evals or all judge_error) → no-data
        return None, [
            ScorecardRow(
                metric=m,
                axis="quality",
                value=None,
                target=None,
                status="no_data",
                score=None,
                wow=None,
            )
            for m in ("pass_rate", "hard_fail_rate")
        ]

    rows: list[ScorecardRow] = []
    metric_scores: list[float] = []

    # pass_rate
    cur_pass, _ = _compute_rates(eval_records_7d)
    pri_pass, _ = _compute_rates(eval_records_prior_7d)
    wow_pass = _wow_arrow(cur_pass, pri_pass)

    mt_pass = qual_targets.get("pass_rate")
    if mt_pass:
        s = _map_metric_to_score(pass_rate, mt_pass)
        metric_scores.append(s)
        status = "ok" if s >= 80 else ("warn" if s >= 60 else "crit")
        rows.append(
            ScorecardRow(
                metric="pass_rate",
                axis="quality",
                value=round(pass_rate, 4),
                target=mt_pass.target,
                status=status,
                score=round(s, 1),
                wow=wow_pass,
            )
        )
    else:
        rows.append(
            ScorecardRow(
                metric="pass_rate",
                axis="quality",
                value=round(pass_rate, 4),
                target=None,
                status="no_data",
                score=None,
                wow=None,
            )
        )

    # hard_fail_rate
    _, cur_hf = _compute_rates(eval_records_7d)
    _, pri_hf = _compute_rates(eval_records_prior_7d)
    wow_hf = _wow_arrow(cur_hf, pri_hf)

    mt_hf = qual_targets.get("hard_fail_rate")
    if mt_hf and hard_fail_rate is not None:
        s = _map_metric_to_score(hard_fail_rate, mt_hf)
        metric_scores.append(s)
        status = "ok" if s >= 80 else ("warn" if s >= 60 else "crit")
        rows.append(
            ScorecardRow(
                metric="hard_fail_rate",
                axis="quality",
                value=round(hard_fail_rate, 4),
                target=mt_hf.target,
                status=status,
                score=round(s, 1),
                wow=wow_hf,
            )
        )
    else:
        rows.append(
            ScorecardRow(
                metric="hard_fail_rate",
                axis="quality",
                value=round(hard_fail_rate, 4) if hard_fail_rate is not None else None,
                target=None,
                status="no_data",
                score=None,
                wow=None,
            )
        )

    sub_score = _metric_mean(metric_scores)
    return (round(sub_score, 1) if sub_score is not None else None), rows


def _score_cost_axis(
    runs_30d: list[RunRecord],
    runs_7d: list[RunRecord],
    runs_prior_30d: list[RunRecord],
    runs_prior_7d: list[RunRecord],
    targets: FleetTargets,
    spend_vs_trend_degraded: bool = False,
) -> tuple[float | None, list[ScorecardRow]]:
    """Compute cost sub-score (0-100) and scorecard rows.

    Cost HEALTH axis (#687, spec/53 §3.6 + MUST 14):
    - spend_vs_trend: recent 30d spend / prior 30d spend ratio (- 1.0).
      This is the ONLY metric scored into the Cost sub-score. An unexpected
      spend spike is a genuine failure signal.

    NOT scored into health (optimization metrics — feed recommendations only):
    - cheaper_model_share: computed as a value for recommend.py's savings_cost rec,
      but NOT appended to metric_scores and NOT scored into cost_score.
    - tokens_per_output: verbosity signal; similarly computed but NOT scored.

    spend_vs_trend_degraded: when True, the PRIOR-30d read was degraded so the
    prior-spend denominator is unreliable. spend_vs_trend is then emitted as a
    'degraded' row and EXCLUDED from the sub-score (MUST 8).
    """
    cost_targets = targets.axes.get("cost", {})

    rows: list[ScorecardRow] = []
    metric_scores: list[float] = []

    # ── spend_vs_trend ───────────────────────────────────────────
    # spend-vs-trend = (recent_30d_spend / prior_30d_spend) - 1.0
    # Requires prior 30d spend > 0. Uses ALL billable runs (not primary-only):
    # helpers and delegates are billed to the fleet.
    #
    # FIX 5: cheaper_model_share and tokens_per_output were previously computed
    # here as advisory-only VALUES. They have been removed: recommend.py computes
    # its own candidate reprice directly from run records and does NOT read
    # cheaper_share or tpo from this function. The dead computation has been deleted.
    #
    # FIX 6: the previous early return on `not primary_30d` gated spend_vs_trend
    # incorrectly. spend_vs_trend uses ALL billable runs (helpers + delegates),
    # so an agent with only helper/delegate spend was wrongly excluded from cost
    # scoring. The early return is removed; spend_vs_trend proceeds regardless.
    recent_spend = sum(r.cost_usd for r in runs_30d)
    prior_spend = sum(r.cost_usd for r in runs_prior_30d)

    spend_ratio: float | None = None
    if prior_spend > 0:
        spend_ratio = (recent_spend / prior_spend) - 1.0

    def _spend_of(runs: list[RunRecord]) -> float | None:
        s = sum(r.cost_usd for r in runs)
        return s if s > 0 else None

    cur_spend = _spend_of(runs_7d)
    pri_spend_7d = _spend_of(runs_prior_7d)
    wow_spend: str | None = None
    if cur_spend is not None and pri_spend_7d is not None:
        wow_spend = _wow_arrow(cur_spend / pri_spend_7d - 1.0, 0.0, threshold=0.05)

    mt_svt = cost_targets.get("spend_vs_trend")
    if spend_vs_trend_degraded:
        # Prior-window read was degraded → prior-spend denominator is unreliable.
        # Emit a 'degraded' row and EXCLUDE this metric from the sub-score (MUST 8).
        rows.append(
            ScorecardRow(
                metric="spend_vs_trend",
                axis="cost",
                value=None,
                target=mt_svt.target if mt_svt else None,
                status="degraded",
                score=None,
                wow=None,
            )
        )
    elif mt_svt and spend_ratio is not None:
        s = _map_metric_to_score(spend_ratio, mt_svt)
        metric_scores.append(s)
        status = "ok" if s >= 80 else ("warn" if s >= 60 else "crit")
        rows.append(
            ScorecardRow(
                metric="spend_vs_trend",
                axis="cost",
                value=round(spend_ratio, 4),
                target=mt_svt.target,
                status=status,
                score=round(s, 1),
                wow=wow_spend,
            )
        )
    else:
        rows.append(
            ScorecardRow(
                metric="spend_vs_trend",
                axis="cost",
                value=round(spend_ratio, 4) if spend_ratio is not None else None,
                target=mt_svt.target if mt_svt else None,
                status="no_data",
                score=None,
                wow=None,
            )
        )

    sub_score = _metric_mean(metric_scores)
    return (round(sub_score, 1) if sub_score is not None else None), rows


# ──────────────────────────────────────────────────────────────────
# Pure scoring core (zero I/O — for counterfactual re-scoring in recommend.py)


def _score_agent_from_data(
    agent: str,
    runs_30d: list[RunRecord],
    runs_prior_30d: list[RunRecord],
    eval_records: list["_EvalRecord"],
    targets: FleetTargets,
    today: date,
    six_days_ago: date,
    prior_7_start: date,
    prior_7_end: date,
    recent_degraded: bool = False,
    prior_degraded: bool = False,
) -> AgentHealth:
    """Pure scoring core — zero disk I/O; operates on pre-loaded data.

    Extracted from _compute_agent_health so the recommendations engine
    (recommend.py) can call it twice: once with real data (baseline) and once
    with a counterfactual run list (model substituted via dataclasses.replace)
    to compute projected_points_delta without extra disk reads.

    All window-slicing is done inside this function from pre-loaded 30d data.
    Caller is responsible for ensuring runs_30d + eval_records cover the full
    30d window [thirty_days_ago, today] — no enforcement here (pure/trust-caller).

    Parameters
    ----------
    agent:            Agent identifier (used for logging + AgentHealth.agent).
    runs_30d:         All runs in the recent 30d window (inclusive both ends).
    runs_prior_30d:   All runs in the prior 30d window (inclusive both ends).
    eval_records:     All eval records in the recent 30d window.
    targets:          Parsed fleet targets.
    today:            Reference date for all window boundaries.
    six_days_ago:     today - 6 days; current 7d window = [six_days_ago, today].
    prior_7_start:    Start of prior 7d window.
    prior_7_end:      End of prior 7d window.
    recent_degraded:  True if recent run load was degraded.
    prior_degraded:   True if prior run load was degraded.
    """
    health = AgentHealth(agent=agent)

    if recent_degraded:
        health.cost_degraded = True
        health.reliability_degraded = True

    # Slice 7d windows from the already-loaded 30d lists (avoids extra I/O).
    # Current 7d: [six_days_ago, today] = 7 inclusive days (#623 WoW fix, spec/53 §6).
    runs_7d = [r for r in runs_30d if six_days_ago <= r.ts.date() <= today]
    runs_prior_7d = [r for r in runs_30d if prior_7_start <= r.ts.date() <= prior_7_end]

    # ── Reliability axis ─────────────────────────────────────────
    if not health.reliability_degraded:
        try:
            rel_score, rel_rows = _score_reliability_axis(
                runs_30d, runs_7d, runs_prior_7d, agent, targets
            )
            health.reliability_score = rel_score
            health.scorecard.extend(rel_rows)
        except Exception as exc:
            logger.warning(
                "advisor: reliability scoring failed for %s (%s)",
                agent,
                type(exc).__name__,
            )
            health.reliability_degraded = True
    if health.reliability_degraded:
        health.scorecard.extend(
            [
                ScorecardRow(
                    metric=m,
                    axis="reliability",
                    value=None,
                    target=None,
                    status="degraded",
                    score=None,
                    wow=None,
                )
                for m in ("error_rate", "blocked_rate", "skipped_rate")
                if not any(row.metric == m for row in health.scorecard)
            ]
        )

    # ── Cost axis ────────────────────────────────────────────────
    # A prior-window-only degrade reaches here with cost_degraded False (recent is
    # clean): spend_vs_trend is excluded (it is the only metric that reads prior spend).
    if not health.cost_degraded:
        try:
            cost_score, cost_rows = _score_cost_axis(
                runs_30d,
                runs_7d,
                runs_prior_30d,
                runs_prior_7d,
                targets,
                spend_vs_trend_degraded=prior_degraded,
            )
            health.cost_score = cost_score
            health.scorecard.extend(cost_rows)
        except Exception as exc:
            logger.warning(
                "advisor: cost scoring failed for %s (%s)", agent, type(exc).__name__
            )
            health.cost_degraded = True
    if health.cost_degraded:
        health.scorecard.extend(
            [
                ScorecardRow(
                    metric=m,
                    axis="cost",
                    value=None,
                    target=None,
                    status="degraded",
                    score=None,
                    wow=None,
                )
                # Only spend_vs_trend is a health metric in the cost axis (#687 MUST 14).
                # cheaper_model_share and tokens_per_output are not health metrics —
                # they are never scored into the sub-score or the critical cap.
                for m in ("spend_vs_trend",)
                if not any(row.metric == m for row in health.scorecard)
            ]
        )

    # ── Quality axis ─────────────────────────────────────────────
    try:
        eval_7d = [r for r in eval_records if r.ts_date >= six_days_ago]
        eval_prior_7d = [
            r for r in eval_records if prior_7_start <= r.ts_date <= prior_7_end
        ]

        quality_score, quality_rows = _score_quality_axis(
            eval_records, eval_7d, eval_prior_7d, targets
        )
        health.quality_score = quality_score
        health.scorecard.extend(quality_rows)
    except Exception as exc:
        logger.warning(
            "advisor: quality scoring failed for %s (%s)", agent, type(exc).__name__
        )
        health.quality_degraded = True
        health.scorecard.extend(
            [
                ScorecardRow(
                    metric=m,
                    axis="quality",
                    value=None,
                    target=None,
                    status="degraded",
                    score=None,
                    wow=None,
                )
                for m in ("pass_rate", "hard_fail_rate")
            ]
        )

    # ── Composite ────────────────────────────────────────────────
    sub_scores: dict[str, float | None] = {
        "cost": None if health.cost_degraded else health.cost_score,
        "quality": None if health.quality_degraded else health.quality_score,
        "reliability": None
        if health.reliability_degraded
        else health.reliability_score,
    }
    # Per-metric scores feed the metric-level critical cap (spec/53 §3.5): a single
    # floored metric (e.g. error_rate=0.90 → 0) forces the cap even when its axis
    # MEAN dilutes it above the threshold. Only consider metrics from axes that are
    # actually present in the composite (a degraded/no-data axis is excluded entirely,
    # so its metrics — all None — must not influence the cap).
    present_axes = {axis for axis, s in sub_scores.items() if s is not None}
    metric_scores = [
        row.score
        for row in health.scorecard
        if row.axis in present_axes and row.score is not None
    ]
    composite, band, capped_by = _compute_composite(
        sub_scores, targets.weights, metric_scores
    )
    health.composite = composite
    health.band = band
    health.capped_by_axis = capped_by
    # Canonical display integer — assigned AFTER the critical-cap override (the
    # cap is applied inside _compute_composite before returning). Bands are derived
    # from this int in render.py (#623 fix, spec/53 §3.3 + MUST 11).
    health.composite_display = int(round(composite)) if composite is not None else None

    health.degraded = (
        health.cost_degraded or health.quality_degraded or health.reliability_degraded
    )
    health.axes_with_data = sum(1 for s in sub_scores.values() if s is not None)

    # ── Primary model ─────────────────────────────────────────────
    # Most common model in recent 30d primary runs; used by recommend.py (#616).
    # NOTE: the most-recent-primary-run TIMESTAMP (for status_for_agent() STALE
    # detection) is computed ONCE by aggregate_console() into
    # ConsoleData.last_primary_runs — NOT duplicated here — so the home summary and
    # the Fleet Monitor (#653) share a single source of that fact (MUST 18).
    primary_runs = [r for r in runs_30d if _is_primary_run(r)]
    if primary_runs:
        from collections import Counter

        model_counts = Counter(r.model for r in primary_runs)
        health.primary_model = model_counts.most_common(1)[0][0]

    return health


# ──────────────────────────────────────────────────────────────────
# Per-agent computation (thin loader wrapper around _score_agent_from_data)


def _compute_agent_health(
    agents_root: Path,
    agent: str,
    targets: FleetTargets,
    today: date,
    thirty_days_ago: date,
    prior_30_end: date,
    prior_30_start: date,
    six_days_ago: date,
    prior_7_start: date,
    prior_7_end: date,
) -> AgentHealth:
    """Load run/eval data from disk then delegate to _score_agent_from_data.

    This is the thin loader. The pure scoring logic lives in _score_agent_from_data
    so that recommend.py can call it with in-memory counterfactual data without
    extra disk I/O.
    """
    runs_30d: list[RunRecord] = []
    runs_prior_30d: list[RunRecord] = []
    recent_degraded = False
    prior_degraded = False

    try:
        runs_30d, deg1 = _load_runs_with_degraded(
            agents_root, agent, thirty_days_ago, today
        )
        runs_prior_30d, deg2 = _load_runs_with_degraded(
            agents_root, agent, prior_30_start, prior_30_end
        )
        recent_degraded = deg1
        prior_degraded = deg2
    except Exception as exc:
        logger.warning(
            "advisor: run load failed for %s (%s)", agent, type(exc).__name__
        )
        recent_degraded = True
        prior_degraded = True

    eval_records: list[_EvalRecord] = []
    try:
        eval_records = _load_eval_records(agents_root, agent, thirty_days_ago, today)
    except Exception as exc:
        logger.warning(
            "advisor: eval load failed for %s (%s)", agent, type(exc).__name__
        )
        # eval load failure is handled inside _score_agent_from_data's quality axis
        # try/except; pass the empty list — the quality scorer returns no-data.

    return _score_agent_from_data(
        agent=agent,
        runs_30d=runs_30d,
        runs_prior_30d=runs_prior_30d,
        eval_records=eval_records,
        targets=targets,
        today=today,
        six_days_ago=six_days_ago,
        prior_7_start=prior_7_start,
        prior_7_end=prior_7_end,
        recent_degraded=recent_degraded,
        prior_degraded=prior_degraded,
    )


# ──────────────────────────────────────────────────────────────────
# Public API


def compute_fleet_health(
    agents_root: Path,
    today: date | None = None,
) -> FleetHealth:
    """Compute FleetHealth for the entire fleet.

    Pure-compute: ZERO LLM calls. Reads log JSONL and eval JSONL only.

    Fail-soft: per-agent exceptions degrade that agent's axes without
    crashing the fleet roll-up. Degraded agents are excluded from composite
    calculations (same as no-data agents).

    Returns FleetHealth with:
    - Per-agent AgentHealth records (always decomposed)
    - Fleet headline composite: min(mean(agent_composites), worst_agent_composite)
      — the worst agent is a CEILING on the headline (spec/53 §7), not a floor
    - Coverage: agents with all 3 axes populated / total agents
    """
    today = today or date.today()
    thirty_days_ago = today - timedelta(days=30)
    # Prior-30d window ends the day BEFORE the recent window starts so the
    # boundary day (today-30) is not counted in BOTH windows — _load_runs_with_degraded
    # filters inclusively on both ends (costs.py: since <= ts.date() <= until), so an
    # inclusive [sixty, thirty] prior would double-count today-30's spend on both sides
    # of spend_vs_trend = recent/prior - 1.0 (spec/53 §5.1, §7).
    #
    # The two windows must also be EQUAL LENGTH or the ratio is biased by one day's
    # spend. The recent window [today-30, today] is 31 inclusive days; making the
    # prior window [today-61, today-31] is also 31 inclusive days. (A flat-spend
    # fleet then reports spend_vs_trend ~= 0.0, not +1/30.) Keeping today-30 in the
    # recent window preserves the boundary-day-counts-recent invariant.
    prior_30_end = thirty_days_ago - timedelta(days=1)
    prior_30_start = today - timedelta(days=61)
    # 7d WoW windows: BOTH must be equal-length (7 inclusive days each) so a
    # flat-spend fleet reports wow_spend = 'flat', not +14.3% (#623 fix, spec/53 §6).
    # Current 7d: [today-6, today] = 7 inclusive days.
    # Prior 7d:   [today-13, today-7] = 7 inclusive days, no overlap, no gap.
    six_days_ago = today - timedelta(days=6)
    prior_7_start = today - timedelta(days=13)
    prior_7_end = today - timedelta(days=7)

    fleet = FleetHealth()

    # Parse targets (fail-soft: absent = all defaults)
    try:
        targets = parse_targets(agents_root)
        fleet.used_targets_defaults = bool(targets.used_defaults)
        fleet.targets_defaults_keys = list(targets.used_defaults)
    except Exception as exc:
        logger.warning(
            "advisor: targets parse failed (%s); using all defaults", type(exc).__name__
        )
        from .targets import FleetTargets, _DEFAULT_WEIGHTS, _DEFAULT_AXES, MetricTarget

        axes = {
            axis_name: {
                metric: MetricTarget(
                    target=float(cfg["target"]),
                    direction=str(cfg["direction"]),
                    band=float(cfg["band"]),
                    floor=float(cfg["floor"]),
                )
                for metric, cfg in axis_cfg["metrics"].items()
            }
            for axis_name, axis_cfg in _DEFAULT_AXES.items()
        }
        targets = FleetTargets(
            weights=dict(_DEFAULT_WEIGHTS),
            axes=axes,
            used_defaults=["(targets_parse_exception)"],
        )
        fleet.used_targets_defaults = True

    agent_names = discover_agents(agents_root)
    fleet_m = len(agent_names)

    agent_healths: list[AgentHealth] = []
    for agent in agent_names:
        try:
            ah = _compute_agent_health(
                agents_root=agents_root,
                agent=agent,
                targets=targets,
                today=today,
                thirty_days_ago=thirty_days_ago,
                prior_30_end=prior_30_end,
                prior_30_start=prior_30_start,
                six_days_ago=six_days_ago,
                prior_7_start=prior_7_start,
                prior_7_end=prior_7_end,
            )
        except Exception as exc:
            logger.warning(
                "advisor: agent health failed for %s (%s)", agent, type(exc).__name__
            )
            ah = AgentHealth(
                agent=agent,
                degraded=True,
                cost_degraded=True,
                quality_degraded=True,
                reliability_degraded=True,
            )
        agent_healths.append(ah)

    fleet.agents = agent_healths

    # Coverage: agents with all 3 axes populated (non-None composite, not all degraded)
    coverage_n = sum(1 for ah in agent_healths if ah.axes_with_data == 3)
    fleet.coverage_n = coverage_n
    fleet.coverage_m = fleet_m

    # Fleet composite: unweighted mean over non-None composites
    # + worst-agent floor/cap (spec/53 §7)
    composites_with_agents = [
        (ah.composite, ah.agent) for ah in agent_healths if ah.composite is not None
    ]

    if composites_with_agents:
        composites = [c for c, _ in composites_with_agents]
        mean_c = sum(composites) / len(composites)
        worst_c, worst_agent = min(composites_with_agents, key=lambda x: x[0])

        # Fleet headline = min(mean, worst_composite) (spec/53 §7). The worst-agent
        # value is a CEILING: with equal weights worst <= mean always holds, so the
        # min currently resolves to worst — the mean term is retained to keep the
        # formula stable if a future operator-weighted roll-up makes mean < worst
        # possible (e.g. a downweighted worst agent). Not a floor.
        fleet_c = min(mean_c, worst_c)
        fleet_c = max(0.0, min(100.0, fleet_c))

        # Fleet critical cap: if ANY included agent is itself critical, force the
        # fleet red (and floor the headline at the ceiling). Keying on the agent's
        # own `capped_by_axis` flag — NOT on `worst_c < THRESHOLD` — is load-bearing:
        # a capped agent's composite is ALREADY floored to CRITICAL_COMPOSITE_CAP
        # (60), so it is never < 30. Testing the post-cap composite would let a
        # 95/95/20-capped agent (composite 60) read amber/green at the fleet level
        # while one of its axes is critical. Agents excluded from the roll-up
        # (None composite) are not considered (they carry no headline weight).
        any_critical_agent = any(
            ah.composite is not None and ah.capped_by_axis is not None
            for ah in agent_healths
        )
        # Apply the critical cap to the RAW capped float — keep a single raw value
        # so the display integer rounds ONCE off the raw composite, never off the
        # 1-decimal-rounded float (the #623 fleet-headline double-round: a raw
        # 79.45 → round(79.45,1)=79.5 → int(round(79.5))=80/GREEN, vs. the
        # canonical single round int(round(79.45))=79/AMBER). Mirror the
        # worst-agent path (below) which already rounds the raw value once.
        if any_critical_agent:
            fleet_c = min(fleet_c, float(CRITICAL_COMPOSITE_CAP))

        # Float for math/history/downstream consumers (1-decimal, display-agnostic).
        fleet.fleet_composite = round(fleet_c, 1)

        # Canonical display integer — int(round(raw)) AFTER the critical-cap
        # override (#623 fix, spec/53 §3.3 + MUST 11). Bands are derived from this int so the
        # shown number and its CSS color class always agree.
        fleet.fleet_composite_display = int(round(fleet_c))
        fleet.fleet_band = (
            "red" if any_critical_agent else _band(fleet.fleet_composite_display)
        )

        fleet.worst_agent = worst_agent
        fleet.worst_agent_composite = round(worst_c, 1)
        fleet.worst_agent_composite_display = int(round(worst_c))

    # Degraded flag: OR-composition
    fleet.degraded = any(ah.degraded for ah in agent_healths)

    return fleet

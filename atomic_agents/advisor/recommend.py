"""Fleet Console Recommendations Engine — pure-compute module (spec/54).

Generates ranked Recommendation objects from pre-computed AgentHealth data.
ZERO new LLM spend — no LLMBackend constructed on any path.

Three rec kinds shipped in PR3:
  savings_cost   — per-run reprice of 30d actual token counts at a cheaper
                   same-family sibling model's PRICING rates; only fires when
                   the composite conjunctive no-quality-cost guard passes.
  quality_report — surface already-written evals/tuning_reports/*.md files;
                   flag agents below rubric threshold or with hard-fails.
  governance     — governance.md absent, no YAML block, or parse errors.

IMPORT DISCIPLINE (spec/54 MUST 10 — mirror of spec/53 §2):
  This module's OWN code imports only from:
    - stdlib (+ frontmatter, a project dependency)
    - atomic_agents.core_api (get_model_rates, calc_cost)
    - atomic_agents.advisor.score (_EvalRecord, _score_agent_from_data, ...)
    - atomic_agents.advisor.targets (FleetTargets, RecommendationConfig,
      _DEFAULT_SAME_FAMILY_DOWNGRADE, parse_recommendations, parse_targets)
    - atomic_agents.dashboard.costs (_load_runs_with_degraded) — recommend_fleet
      loader path only; pure-read, constructs no LLMBackend
    - atomic_agents.agent_registry (AgentRegistryError,
      FilesystemAgentRegistryBackend) — recommend_fleet loader path only; pure-read
  The MUST-10 forbidden set is exactly agent.py, eval.py, tuning.py, dream.py
  (the four LLM-spend-bearing modules); this module imports NONE of them directly.
  The conftest guard in tests/advisor/ enforces no-LLM-construction at test time,
  including over the recommend_fleet on-disk loader graph (TestRecommendFleet).

Verify the source-level discipline (the `agent\\s+import` anchor excludes the
allowed `..agent_registry import` pure-read import — `agent` there is followed by
`_registry`, not whitespace):
  grep -rnE '^[[:space:]]*from [.\\w]*(agent|eval|tuning|dream)[[:space:]]+import' \\
       atomic_agents/advisor/recommend.py
  Returns empty.

NOTE: recommend() eval_records MUST be the SAME records passed to
_score_quality_axis for the relevant agent (same 30d window, same filtering).
recommend_fleet() enforces this by loading evals in the same window block as
compute_fleet_health. Mismatched windows produce logically incoherent guard
predicates silently — spec/54 MUST 11 (window alignment).
"""

from __future__ import annotations

import dataclasses
import logging
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import frontmatter  # python-frontmatter>=1.1; already a project dependency

from ..core_api import calc_cost, get_model_rates
from .score import (
    AgentHealth,
    FleetHealth,
    RunRecord,
    _EvalRecord,
    _is_primary_run,
    _load_eval_records,
    _score_agent_from_data,
    compute_fleet_health,
)
from .targets import (
    FleetTargets,
    RecommendationConfig,
    _DEFAULT_SAME_FAMILY_DOWNGRADE,
    parse_recommendations,
    parse_targets,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Public kinds frozenset (normative — spec/54 MUST 1)
# All Recommendation.kind values must be members of this set.
RECOMMENDATION_KINDS: frozenset[str] = frozenset(
    {"savings_cost", "quality_report", "governance"}
)


def _rec_sort_key(r: "Recommendation") -> tuple[float, float]:
    """Sort key for Recommendation objects: (|points_delta|, usd_tiebreak) desc.

    Ranking fallback (#687, spec/53 §3.6 + MUST 14): cheaper_model_share and
    tokens_per_output are not health metrics after #687, so savings_cost recs have
    projected_points_delta ~0.0.  For savings_cost recs with zero/None point-impact,
    the tiebreak is abs(projected_usd_delta) so larger dollar savings rank first.
    Non-savings_cost recs with zero points_delta get usd_tiebreak=0 (kind guard).
    """
    pts = abs(r.projected_points_delta) if r.projected_points_delta is not None else 0.0
    if pts == 0.0 and r.kind == "savings_cost" and r.projected_usd_delta is not None:
        usd = abs(r.projected_usd_delta)
    else:
        usd = 0.0
    return (pts, usd)


# ──────────────────────────────────────────────────────────────────
# Public API dataclasses (spec/54 §3)


@dataclass
class EvalHeadroom:
    """Composite conjunctive gate state for a no-quality-cost downgrade guard.

    All four predicates must be True for a downgrade to be safe (spec/54 MUST 2).
    The gate fires over the SAME 30d eval window as the quality axis score.

    Fields
    ------
    weighted_score_margin:  mean weighted-score minus rubric threshold (points).
                            Positive = above threshold.
    pass_rate_margin:       eval pass-rate minus rubric pass-rate threshold.
                            Positive = above threshold.
    hard_fails:             count of evals with hard_fails non-empty in window.
    sample_n:               count of scorable evals (verdict in {pass, fail}).
    rubric_threshold:       the pass threshold read from rubric.md (default 4.0).
    passed:                 True only when ALL four predicates clear their floors.
    """

    weighted_score_margin: float
    pass_rate_margin: float
    hard_fails: int
    sample_n: int
    rubric_threshold: float
    passed: bool


@dataclass
class Recommendation:
    """A single actionable recommendation for one agent (spec/54 §3).

    kind must be a member of RECOMMENDATION_KINDS (validated in __post_init__).
    Two separate optional delta fields (None when N/A for this rec kind).
    """

    agent: str
    kind: str  # member of RECOMMENDATION_KINDS — validated in __post_init__
    current_model: str | None  # None for non-model recs (governance, quality_report)
    candidate_model: str | None  # None for non-model recs
    projected_usd_delta: float | None  # negative = savings ($/mo); None when N/A
    projected_points_delta: float | None  # composite point improvement; None when N/A
    rationale: str
    safety: EvalHeadroom  # always present; passed=True only for savings_cost recs
    # How a MODEL candidate was chosen: "default_same_family" | "operator_configured".
    # None for non-model recs (governance, quality_report) — those have no candidate
    # and no "family", so labeling them with a model-selection source would be
    # semantically wrong. Diagnostic-only field; render.py does not read it.
    source: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in RECOMMENDATION_KINDS:
            raise ValueError(
                f"Recommendation.kind={self.kind!r} is not a member of "
                f"RECOMMENDATION_KINDS={sorted(RECOMMENDATION_KINDS)}"
            )


# ──────────────────────────────────────────────────────────────────
# Rubric threshold reader (pure — no eval.py import)


def _load_rubric_threshold(agents_root: Path, agent: str) -> float:
    """Read threshold_pass from evals/rubric.md frontmatter. Fail-soft to 4.0.

    Catches yaml.YAMLError alongside the value/IO errors so a malformed-YAML
    rubric.md degrades to the 4.0 default rather than raising — yaml.YAMLError is
    NOT a subclass of ValueError, so it must be named explicitly (mirrors the
    sibling reader _read_tuning_report). Without it a single bad rubric.md would
    make this loader raise, and the broad per-agent guard in recommend_fleet would
    then drop ALL of that agent's recommendations instead of degrading one field.
    """
    import yaml

    rubric_path = agents_root / agent / "evals" / "rubric.md"
    try:
        meta = frontmatter.load(str(rubric_path)).metadata
        v = meta.get("threshold_pass", 4.0)
        return float(v)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return 4.0


# ──────────────────────────────────────────────────────────────────
# No-quality-cost composite conjunctive guard (spec/54 MUST 2–6)


def _eval_headroom(
    eval_records: list[_EvalRecord],
    rubric_threshold: float,
    rec_config: RecommendationConfig,
) -> EvalHeadroom:
    """Compute the composite conjunctive gate predicates over the eval window.

    All four predicates (spec/54 MUST 2–5) must hold for EvalHeadroom.passed.
    The fail-safe (MUST 6): no/sub-N eval data → passed=False, no downgrade.

    Parameters
    ----------
    eval_records:      Pre-loaded eval records for the agent's 30d window.
                       MUST be the same records used for quality axis scoring.
    rubric_threshold:  Pass threshold from rubric.md (default 4.0).
    rec_config:        Parsed recommendation config (floors + min_eval_n).
    """
    scorable = [r for r in eval_records if r.verdict in ("pass", "fail")]
    sample_n = len(scorable)
    # hard_fails counted over the FULL window, not just scorable (pass/fail)
    # records — matches the field's documented "in window" semantics and the
    # fail-safe posture: a hard-fail on a judge_error / unscored eval is still a
    # catastrophic-output signal that MUST block a downgrade. (Codex cross-family
    # review #616: scorable-only counting let a judge_error+hard_fail slip past.)
    hard_fails = sum(1 for r in eval_records if r.hard_fails)

    if sample_n == 0:
        # Fail-safe (MUST 6): no scorable eval data → NO downgrade
        return EvalHeadroom(
            weighted_score_margin=0.0,
            pass_rate_margin=0.0,
            hard_fails=hard_fails,
            sample_n=0,
            rubric_threshold=rubric_threshold,
            passed=False,
        )

    # Fail-safe: a non-finite weighted_score (NaN/inf from a corrupt or
    # adversarial eval JSONL) is NOT finite proof of headroom — an inf margin
    # would otherwise clear P1. Suppress the downgrade. (Codex cross-family #616.)
    if not all(math.isfinite(r.weighted_score) for r in scorable):
        return EvalHeadroom(
            weighted_score_margin=0.0,
            pass_rate_margin=0.0,
            hard_fails=hard_fails,
            sample_n=sample_n,
            rubric_threshold=rubric_threshold,
            passed=False,
        )

    pass_rate = sum(1 for r in scorable if r.verdict == "pass") / sample_n
    mean_weighted = sum(r.weighted_score for r in scorable) / sample_n

    weighted_score_margin = mean_weighted - rubric_threshold
    # pass_rate (0–1, fraction of evals that passed) minus the rubric SCORE
    # threshold normalized to /5 (e.g. 4.0/5=0.8). These are heterogeneous
    # quantities — "how many evals passed" vs. "the score level required to pass"
    # — deliberately conflated as a CONSERVATIVE heuristic, not a units-matched
    # margin: the conflation only makes the gate STRICTER (it demands a high
    # pass-rate AND a high mean score), so it can only ever block a downgrade,
    # never wrongly permit one — safe for the no-quality-cost guarantee.
    pass_rate_margin = pass_rate - rubric_threshold / 5.0

    # Predicate 1: mean weighted-score margin clears floor (MUST 2)
    p1 = weighted_score_margin >= rec_config.score_margin_floor

    # Predicate 2: eval pass-rate margin clears floor (MUST 3)
    p2 = pass_rate_margin >= rec_config.pass_rate_margin_floor

    # Predicate 3: zero hard-fails in window (MUST 4)
    p3 = hard_fails == 0

    # Predicate 4: sample count >= min_eval_n (MUST 5 + fail-safe MUST 6)
    p4 = sample_n >= rec_config.min_eval_n

    passed = p1 and p2 and p3 and p4

    return EvalHeadroom(
        weighted_score_margin=weighted_score_margin,
        pass_rate_margin=pass_rate_margin,
        hard_fails=hard_fails,
        sample_n=sample_n,
        rubric_threshold=rubric_threshold,
        passed=passed,
    )


# ──────────────────────────────────────────────────────────────────
# Candidate model selection (spec/54 §4 — conservative same-family)


def _resolve_candidate(
    model: str,
    work_type: str,
    rec_config: RecommendationConfig,
) -> tuple[str | None, str]:
    """Find the next-cheaper same-family candidate model.

    Returns (candidate_model_id, source) where source is one of:
    "operator_configured" or "default_same_family".
    Returns (None, ...) when no safe candidate is available (fail-pessimistic).

    Operator-configured work_type_allowed_models takes priority when present.
    The baked-in _DEFAULT_SAME_FAMILY_DOWNGRADE map is the fallback.

    The candidate must have a known rate for repricing to work (verified here
    via get_model_rates()).
    """
    # Operator-configured path
    if (
        rec_config.work_type_allowed_models
        and work_type in rec_config.work_type_allowed_models
    ):
        candidates = rec_config.work_type_allowed_models[work_type]
        # Filter to models with a known rate only
        valid = [c for c in candidates if get_model_rates(c) is not None]
        if valid:
            return valid[0], "operator_configured"

    # Default same-family downgrade map
    candidate = _DEFAULT_SAME_FAMILY_DOWNGRADE.get(model)
    if candidate is None:
        return None, "default_same_family"
    if get_model_rates(candidate) is None:
        logger.warning(
            "recommend: default candidate %r for model %r has no known rate; skipping",
            candidate,
            model,
        )
        return None, "default_same_family"
    return candidate, "default_same_family"


# ──────────────────────────────────────────────────────────────────
# Per-run repricing for savings estimate (spec/54 §6 / §7 step 1)


def _reprice_run(run: RunRecord, candidate_model: str) -> float:
    """Reprice one run at the candidate model's rates. Returns cost_usd.

    Uses actual cache_hit_tokens from the run (same cache structure as the
    original pricing — cache hit rate is model-specific but we apply it as-is
    for the estimate, which is documented as approximate). Per PREP finding P1:
    using cache_hit_tokens reuses the existing calc_cost() contract correctly.
    """
    cost, _ = calc_cost(
        candidate_model,
        run.input_tokens,
        run.output_tokens,
        cache_hit_tokens=run.cache_hit_tokens,
    )
    return cost


# ──────────────────────────────────────────────────────────────────
# Tuning report reader (pure — observe-only, no tuning.py import)


def _load_tuning_reports(agents_root: Path, agent: str) -> list[Path]:
    """List evals/tuning_reports/*.md files for one agent. Per-file OSError → skip.

    Path MUST match where tuning.py actually writes (tuning.py:1045 ->
    `<agent>/evals/tuning_reports/`) and where the established reader
    dashboard/quality.py:124 reads. A bare `<agent>/tuning_reports/` would
    silently surface nothing on any real fleet (Principle #13).
    """
    reports_dir = agents_root / agent / "evals" / "tuning_reports"
    if not reports_dir.exists():
        return []
    paths: list[Path] = []
    try:
        for p in sorted(reports_dir.glob("*.md")):
            paths.append(p)
    except OSError:
        pass
    return paths


def _read_tuning_report(path: Path) -> dict | None:
    """Read one tuning report .md file; return parsed frontmatter + body or None.

    Fail-soft on the genuinely-expected failure modes (file vanished between glob
    and read; malformed YAML frontmatter), but LOG so a malformed report is
    observable rather than a silent skip. A broad bare-except here would hide
    real bugs the same way the ref.agent_id AttributeError was hidden.
    """
    try:
        import yaml

        post = frontmatter.load(str(path))
        return {"meta": post.metadata, "body": post.content, "path": path}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning(
            "recommend: skipping unreadable/malformed tuning report %s (%s)",
            path,
            type(exc).__name__,
        )
        return None


# ──────────────────────────────────────────────────────────────────
# Governance state helpers


def _governance_rec_rationale(
    has_governance: bool, governance: object | None
) -> str | None:
    """Map governance parse state to a rationale string.

    Returns None when no governance rec should be emitted.

    KNOWN COLLAPSE (spec/54 §9, tracked in #625): the FilesystemAgentRegistryBackend
    maps PRESENT_UNREADABLE / symlink-escape / oversize-file to
    (has_governance=False, governance=None) — byte-identical to ABSENT. AgentRef
    exposes no signal to distinguish them, so an unreadable governance.md surfaces
    the "absent" rationale (telling the operator to add a file that exists but
    could not be read). This is documented, not honored-as-NO-rec; suppressing it
    requires the registry to expose an unreadable state at the AgentRef surface —
    filed as #625, out of PR3 scope.
    """
    if not has_governance:
        return "governance.md is absent — add it to declare owner, permission tier, and lifecycle status"
    if governance is None:
        # has_governance=True but governance=None means PRESENT_NO_BLOCK
        return "governance.md has no parseable 'governance:' YAML block — add the block to register this agent"
    # has_governance=True, governance is not None → check parse_errors
    parse_errors = getattr(governance, "parse_errors", None)
    if parse_errors:
        return (
            f"governance.md has parse errors: {'; '.join(str(e) for e in parse_errors)}"
        )
    return None


# ──────────────────────────────────────────────────────────────────
# Null EvalHeadroom sentinel (for non-model recs where safety N/A)


def _null_headroom() -> EvalHeadroom:
    """Return a no-data EvalHeadroom for non-cost recs (safety not applicable)."""
    return EvalHeadroom(
        weighted_score_margin=0.0,
        pass_rate_margin=0.0,
        hard_fails=0,
        sample_n=0,
        rubric_threshold=4.0,
        passed=False,
    )


# ──────────────────────────────────────────────────────────────────
# Pure recommendation core (spec/54 MUST 7 — zero I/O, zero LLM)


def recommend(
    agent_health: AgentHealth,
    run_records: list[RunRecord],
    eval_records: list[_EvalRecord],
    rec_config: RecommendationConfig,
    rubric_threshold: float = 4.0,
    governance_rationale: str | None = None,
    tuning_report_paths: list[Path] | None = None,
    today: date | None = None,
    targets: FleetTargets | None = None,
) -> list[Recommendation]:
    """Pure recommendation core — zero disk I/O, zero LLM spend.

    Caller is responsible for:
    - run_records: primary 30d runs for the agent (same window as agent_health).
    - eval_records: 30d eval records for the agent — MUST be the SAME records
      used to compute agent_health.quality_score (spec/54 MUST 11 alignment).
    - governance_rationale: pre-computed from registry list_agents(); None = no gov rec.
    - tuning_report_paths: pre-discovered report paths; None = no quality_report recs.
    - today: reference date for any date-relative logic; defaults to date.today().
    - targets: the SAME FleetTargets compute_fleet_health used to score
      agent_health.composite. REQUIRED for a coherent projected_points_delta —
      the counterfactual must be scored under the operator's targets, not
      hardcoded defaults, or the baseline/counterfactual diff conflates the
      model swap with a target-set switch (spec/54 §7). When None, the
      point-impact counterfactual is skipped (projected_points_delta=None) — the
      savings_cost rec still fires, just without a point estimate.

    Returns a list of Recommendation objects (may be empty). No ordering guarantee —
    recommend_fleet() sorts by abs(projected_points_delta) descending.

    projected_points_delta is a RANKING proxy, not a realizable-composite promise:
    it is the model-swap-isolated composite delta computed under a prior-window-less
    re-scoring (both baseline and counterfactual use runs_prior_30d=[], both drop
    spend_vs_trend — see spec/54 §7), so it is NOT a guarantee that the agent's live
    headline rises by that many points (a swap can leave the agent capped by a metric
    it does not touch). The audit-honest operator signal is the $/mo savings, which
    is computed from real repriced spend.
    """
    today = today or date.today()
    recs: list[Recommendation] = []
    agent = agent_health.agent
    primary_model = agent_health.primary_model

    # ── 1. Savings cost rec ──────────────────────────────────────
    if primary_model is not None:
        work_type = _classify_work_type_majority(run_records)
        candidate, source = _resolve_candidate(primary_model, work_type, rec_config)

        if candidate is not None:
            # Compute the no-quality-cost guard
            headroom = _eval_headroom(eval_records, rubric_threshold, rec_config)

            if headroom.passed:
                # Reprice 30d primary runs at candidate rates
                primary_30d = [r for r in run_records if _is_primary_run(r)]
                actual_30d_cost = sum(r.cost_usd for r in primary_30d)
                repriced_30d_cost = sum(_reprice_run(r, candidate) for r in primary_30d)
                delta_30d = repriced_30d_cost - actual_30d_cost
                # The 30d window IS the monthly ($/mo) estimate — ~1 month, no
                # scaling applied (not annualized).
                projected_usd_delta = delta_30d

                if (
                    projected_usd_delta < 0
                    and abs(projected_usd_delta) >= rec_config.min_savings_usd
                ):
                    # Compute point-impact counterfactual (spec/54 §7). Scored
                    # under the SAME operator targets as agent_health.composite so
                    # the diff isolates the model swap. None targets → skip.
                    projected_points_delta = _compute_point_impact(
                        agent_health=agent_health,
                        run_records=run_records,
                        eval_records=eval_records,
                        candidate_model=candidate,
                        repriced_cost_per_run=lambda r: _reprice_run(r, candidate),
                        targets=targets,
                        today=today,
                    )
                    try:
                        rec = Recommendation(
                            agent=agent,
                            kind="savings_cost",
                            current_model=primary_model,
                            candidate_model=candidate,
                            projected_usd_delta=round(projected_usd_delta, 4),
                            projected_points_delta=projected_points_delta,
                            rationale=(
                                f"Switching from {primary_model} to {candidate} "
                                f"saves ~${abs(projected_usd_delta):.2f}/mo based on 30d usage"
                            ),
                            safety=headroom,
                            source=source,
                        )
                        recs.append(rec)
                    except ValueError as exc:
                        logger.warning(
                            "recommend: savings_cost rec construction failed for %s: %s",
                            agent,
                            exc,
                        )

    # ── 2. Governance rec ────────────────────────────────────────
    if governance_rationale is not None:
        try:
            rec = Recommendation(
                agent=agent,
                kind="governance",
                current_model=primary_model,
                candidate_model=None,
                projected_usd_delta=None,
                projected_points_delta=None,
                rationale=governance_rationale,
                safety=_null_headroom(),
            )
            recs.append(rec)
        except ValueError as exc:
            logger.warning(
                "recommend: governance rec construction failed for %s: %s", agent, exc
            )

    # ── 3. Quality report recs ────────────────────────────────────
    if tuning_report_paths:
        for report_path in tuning_report_paths:
            report = _read_tuning_report(report_path)
            if report is None:
                continue  # file deleted between glob and read — skip (fail-soft)
            meta = report.get("meta", {}) or {}
            body = report.get("body", "") or ""

            # Flag: any hard-fail mention or quality below threshold
            has_hard_fail = bool(
                meta.get("has_hard_fails")
                or "hard_fail" in body.lower()
                or "hard fail" in body.lower()
            )
            # Honor a present `score:` even when it is 0 (a real below-threshold
            # score) — `meta.get("score") or ...` would treat 0 as falsy and silently
            # fall through to weighted_score. weighted_score is a fallback ONLY when
            # `score` is absent from frontmatter entirely (spec/54 §8).
            score_val = (
                meta.get("score") if "score" in meta else meta.get("weighted_score")
            )
            below_threshold = (
                score_val is not None
                and isinstance(score_val, (int, float))
                and float(score_val) < rubric_threshold
            )

            if has_hard_fail or below_threshold:
                reason_parts = []
                if has_hard_fail:
                    reason_parts.append("hard-fail detected")
                if below_threshold:
                    reason_parts.append(
                        f"score {score_val:.1f} below threshold {rubric_threshold:.1f}"
                    )
                reason = "; ".join(reason_parts)
                report_name = report_path.stem
                try:
                    rec = Recommendation(
                        agent=agent,
                        kind="quality_report",
                        current_model=primary_model,
                        candidate_model=None,
                        projected_usd_delta=None,
                        projected_points_delta=None,
                        rationale=(f"Tuning report {report_name!r}: {reason}"),
                        safety=_null_headroom(),
                    )
                    recs.append(rec)
                except ValueError as exc:
                    logger.warning(
                        "recommend: quality_report rec construction failed for %s: %s",
                        agent,
                        exc,
                    )

    return recs


# ──────────────────────────────────────────────────────────────────
# Work-type majority classifier (for candidate set lookup key)


def _classify_work_type_majority(run_records: list[RunRecord]) -> str:
    """Determine the majority work type for the primary runs in the window."""
    from .score import _classify_work_type

    primary = [r for r in run_records if _is_primary_run(r)]
    if not primary:
        return "general"
    counts = Counter(_classify_work_type(r) for r in primary)
    return counts.most_common(1)[0][0]


# ──────────────────────────────────────────────────────────────────
# Point-impact counterfactual (spec/54 §7 — zero extra I/O)


def _compute_point_impact(
    agent_health: AgentHealth,
    run_records: list[RunRecord],
    eval_records: list[_EvalRecord],
    candidate_model: str,
    repriced_cost_per_run: Callable[[RunRecord], float],
    targets: FleetTargets | None,
    today: date | None,
) -> float | None:
    """Compute projected composite point improvement from a model swap.

    Builds counterfactual run_records (model substituted via dataclasses.replace)
    and re-runs the pure scoring core under the SAME ``targets`` and ``today``
    used to produce ``agent_health.composite``. Returns the delta (positive =
    improvement). Zero extra disk I/O; original run_records are NOT mutated.

    Coherence requirement (spec/54 §7): ``targets`` MUST be the operator's parsed
    FleetTargets (the same object compute_fleet_health used for the baseline), and
    ``today`` MUST be the same reference date — otherwise the diff conflates the
    model swap with a target-set or window switch. When ``targets`` is None we
    cannot guarantee that coherence, so we return None (no point estimate) rather
    than a misleading number.

    SYMMETRIC-BASELINE diff (spec/54 §7 — the load-bearing coherence move): the
    counterfactual has no prior-30d window data (the pure core is given
    runs_prior_30d=[]), so its spend_vs_trend cost sub-metric goes to a no_data row
    that is EXCLUDED from the cost sub-score. If we diffed against
    ``agent_health.composite`` — which WAS scored with a real prior window and so
    INCLUDES spend_vs_trend (possibly a floored, critical-capping metric) — the
    delta would silently credit the model swap for spend_vs_trend simply
    DISAPPEARING, a phantom point swing that has nothing to do with the model. To
    isolate the model swap we RE-SCORE THE BASELINE with the same reduced inputs
    (runs_prior_30d=[]) so both sides drop spend_vs_trend identically, then diff
    ``cf.composite - baseline_reduced.composite``. The returned delta therefore
    reflects ONLY the metric the model substitution can actually move
    (cheaper_model_share via the cheap-cutoff, tokens_per_output, the repriced
    cost), never the prior-window asymmetry.

    Consequence: a same-family swap that crosses no cheaper-model cutoff and only
    reprices (e.g. claude-haiku -> claude-haiku-<dated>, or opus->sonnet both above
    the cutoff) yields a delta ~0.0 — correct, because that swap moves no scored
    metric. recommend_fleet still surfaces the rec; the $/mo savings is the primary
    signal and the point delta is the secondary ranking key.

    Returns None if point-impact cannot be computed (no composite baseline, no
    primary runs, no targets, or a scoring failure).
    """
    if agent_health.composite is None:
        return None
    if targets is None:
        return None

    # Derive windows from the SAME reference date the baseline used (spec/54 §7).
    ref_today = today or date.today()
    six_days_ago = ref_today - timedelta(days=6)
    prior_7_start = ref_today - timedelta(days=13)
    prior_7_end = ref_today - timedelta(days=7)

    primary_dates = [r.ts.date() for r in run_records if _is_primary_run(r)]
    if not primary_dates:
        return None

    # Build counterfactual: copy primary runs with candidate model + repriced cost.
    # Use dataclasses.replace — never mutate originals (spec/54 MUST 8).
    counterfactual_runs: list[RunRecord] = []
    for r in run_records:
        if _is_primary_run(r):
            repriced = repriced_cost_per_run(r)
            counterfactual_runs.append(
                dataclasses.replace(r, model=candidate_model, cost_usd=repriced)
            )
        else:
            counterfactual_runs.append(r)

    try:
        # Re-score the BASELINE with the SAME reduced inputs as the counterfactual
        # (runs_prior_30d=[]) so both sides exclude spend_vs_trend identically. This
        # is the symmetric-baseline diff (see docstring) — without it, a baseline
        # whose spend_vs_trend floored (critical-capping its composite at 60) would
        # produce a phantom positive delta the moment spend_vs_trend drops out of
        # the counterfactual, corrupting the abs(projected_points_delta) ranking key.
        baseline_reduced = _score_agent_from_data(
            agent=agent_health.agent,
            runs_30d=run_records,
            runs_prior_30d=[],  # SAME reduced input as the counterfactual below
            eval_records=eval_records,
            targets=targets,
            today=ref_today,
            six_days_ago=six_days_ago,
            prior_7_start=prior_7_start,
            prior_7_end=prior_7_end,
        )
        cf_health = _score_agent_from_data(
            agent=agent_health.agent,
            runs_30d=counterfactual_runs,
            runs_prior_30d=[],  # SAME reduced input as the baseline above
            eval_records=eval_records,
            targets=targets,
            today=ref_today,
            six_days_ago=six_days_ago,
            prior_7_start=prior_7_start,
            prior_7_end=prior_7_end,
        )
        if cf_health.composite is None or baseline_reduced.composite is None:
            return None
        return round(cf_health.composite - baseline_reduced.composite, 1)
    except Exception as exc:
        logger.warning(
            "recommend: point-impact recompute failed for %s (%s)",
            agent_health.agent,
            type(exc).__name__,
        )
        return None


# ──────────────────────────────────────────────────────────────────
# Fleet-level loader (thin wrapper — calls recommend() per agent)


def recommend_fleet(
    agents_root: Path,
    today: date | None = None,
    rec_config: RecommendationConfig | None = None,
    fleet_health: FleetHealth | None = None,
) -> list[Recommendation]:
    """Load fleet data and produce Recommendation objects for all agents.

    Thin loader: calls compute_fleet_health() for AgentHealth data, then
    calls recommend() per agent. Results sorted by |projected_points_delta|
    descending (highest fleet-health-point impact first).

    ``fleet_health`` may be passed in pre-computed (render_console already runs
    compute_fleet_health for the health band) to avoid the SECOND fleet-wide
    SCORING pass per console render (Principle #6 — don't recompute what the
    caller already has). NOTE: the per-agent run/eval JSONL is still re-loaded in
    the loop below to build recommend()'s pure-core inputs; the saving is the
    compute_fleet_health scoring call (and its internal loads), not the per-agent
    re-loads. The pre-computed object MUST have been scored with the SAME ``today``
    reference date this call uses, or the per-agent re-load windows below would
    diverge from the AgentHealth baselines. When None, it is computed internally
    (the standalone-call path).

    Fail-soft: per-agent exceptions produce no recommendations for that agent
    without crashing the fleet pass.
    """
    today = today or date.today()
    thirty_days_ago = today - timedelta(days=30)

    if rec_config is None:
        try:
            rec_config = parse_recommendations(agents_root)
        except Exception as exc:
            logger.warning(
                "recommend_fleet: parse_recommendations failed (%s); using defaults",
                type(exc).__name__,
            )
            rec_config = RecommendationConfig()

    # Compute fleet health (loads all runs + evals internally with the same windows)
    # unless the caller already computed it for the SAME reference date.
    if fleet_health is None:
        try:
            fleet_health = compute_fleet_health(agents_root, today=today)
        except Exception as exc:
            logger.warning(
                "recommend_fleet: compute_fleet_health failed (%s); no recommendations",
                type(exc).__name__,
            )
            return []

    # Parse the SAME targets compute_fleet_health used internally, so the
    # point-impact counterfactual is scored under the operator's targets, not
    # hardcoded defaults (spec/54 §7). Fail-soft: None → point deltas suppressed.
    try:
        targets = parse_targets(agents_root)
    except Exception as exc:
        logger.warning(
            "recommend_fleet: parse_targets failed (%s); point-impact deltas suppressed",
            type(exc).__name__,
        )
        targets = None

    # Load agent registry for governance state. Fail-soft ONLY on the genuinely
    # expected failures (registry/filesystem errors) — NOT a blanket
    # `except Exception`, which would silently swallow a coding bug (the original
    # ref.agent_id AttributeError was hidden exactly this way) and make the whole
    # governance rec kind disappear with no test signal. Coding bugs propagate.
    governance_map: dict[str, str | None] = {}
    try:
        from ..agent_registry import AgentRegistryError, FilesystemAgentRegistryBackend

        registry = FilesystemAgentRegistryBackend(agents_root)
        agents_refs = registry.list_agents(include_governance=True)
        for ref in agents_refs:
            rationale = _governance_rec_rationale(
                has_governance=getattr(ref, "has_governance", False),
                governance=getattr(ref, "governance", None),
            )
            # AgentRef's identifier field is `id` (the folder name), NOT agent_id.
            governance_map[ref.id] = rationale
    except (AgentRegistryError, OSError) as exc:
        logger.warning(
            "recommend_fleet: agent registry load failed (%s); skipping governance recs",
            type(exc).__name__,
        )

    all_recs: list[Recommendation] = []
    for ah in fleet_health.agents:
        agent = ah.agent
        try:
            # Load eval records for this agent using the SAME window as compute_fleet_health
            eval_records = _load_eval_records(
                agents_root, agent, thirty_days_ago, today
            )

            # Load run records for this agent (same 30d window)
            from ..dashboard.costs import _load_runs_with_degraded

            runs_30d, _ = _load_runs_with_degraded(
                agents_root, agent, thirty_days_ago, today
            )

            # Rubric threshold
            rubric_threshold = _load_rubric_threshold(agents_root, agent)

            # Governance rationale (pre-computed from registry)
            governance_rationale = governance_map.get(agent)

            # Tuning report paths
            tuning_report_paths = _load_tuning_reports(agents_root, agent)

            agent_recs = recommend(
                agent_health=ah,
                run_records=runs_30d,
                eval_records=eval_records,
                rec_config=rec_config,
                rubric_threshold=rubric_threshold,
                governance_rationale=governance_rationale,
                tuning_report_paths=tuning_report_paths,
                today=today,
                targets=targets,
            )
            all_recs.extend(agent_recs)
        except Exception as exc:
            logger.warning(
                "recommend_fleet: recommend failed for %s (%s)",
                agent,
                type(exc).__name__,
            )

    # Sort by _rec_sort_key: (|points_delta|, usd_tiebreak) descending.
    # See module-level _rec_sort_key for the #687 ranking fallback rationale.
    all_recs.sort(key=_rec_sort_key, reverse=True)
    return all_recs

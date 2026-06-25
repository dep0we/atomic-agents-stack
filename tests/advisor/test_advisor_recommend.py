"""Tests for atomic_agents.advisor.recommend — Fleet Recommendations Engine (spec/54).

Coverage map (spec/54 Conformance test map):
  MUST 1  — kind validated against RECOMMENDATION_KINDS frozenset
  MUST 2  — no-quality-cost guard predicate 1: weighted_score_margin >= floor
  MUST 3  — no-quality-cost guard predicate 2: pass_rate_margin >= floor
  MUST 4  — no-quality-cost guard predicate 3: zero hard-fails
  MUST 5  — no-quality-cost guard predicate 4: sample_n >= min_eval_n
  MUST 6  — fail-safe: no/stale/sub-N eval data → NO downgrade
  MUST 7  — no-LLM enforcement (conftest guard + import-discipline check)
  MUST 8  — dataclasses.replace used for counterfactual — originals not mutated
  MUST 9  — same-family downgrade candidate in PRICING; fail-pessimistic if absent
  MUST 10 — zero tuning.py import (import discipline)
  MUST 11 — recommend() eval_records window alignment documented (test with wrong window)

Each MUST has at least one strip-RED negative-control test.

NOTE: This file is placed in tests/advisor/ so the autouse no_llm_in_advisor
conftest fixture (scope='module') applies automatically — no explicit import needed.
The fixture raises RuntimeError if any LLMBackend.__init__ is called.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.advisor.recommend import (
    RECOMMENDATION_KINDS,
    EvalHeadroom,
    Recommendation,
    _eval_headroom,
    _load_rubric_threshold,
    _load_tuning_reports,
    _reprice_run,
    _resolve_candidate,
    recommend,
)
from atomic_agents.advisor.score import _EvalRecord, AgentHealth
from atomic_agents.advisor.targets import (
    RecommendationConfig,
)
from atomic_agents.dashboard.costs import RunRecord


# ──────────────────────────────────────────────────────────────────
# Helpers


def _make_run(
    agent: str = "test-agent",
    status: str = "completed",
    trigger: str = "cron",
    model: str = "claude-haiku-4-5",
    cost_usd: float = 0.01,
    output_tokens: int = 200,
    input_tokens: int = 100,
    cache_hit_tokens: int = 0,
    ts: datetime | None = None,
    parent_run_id: str | None = None,
    extra: dict | None = None,
) -> RunRecord:
    ts = ts or datetime.now(tz=timezone.utc)
    return RunRecord(
        ts=ts,
        agent=agent,
        trigger=trigger,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=input_tokens - cache_hit_tokens,
        latency_ms=100,
        status=status,
        summary="ok",
        parent_run_id=parent_run_id,
        extra=extra or {},
    )


def _make_eval(
    verdict: str = "pass",
    weighted_score: float = 4.5,
    hard_fails: list | None = None,
    ts_date: date | None = None,
) -> _EvalRecord:
    return _EvalRecord(
        ts_date=ts_date or date.today(),
        verdict=verdict,
        hard_fails=hard_fails or [],
        weighted_score=weighted_score,
    )


def _make_agent_health(
    agent: str = "test-agent",
    composite: float | None = 80.0,
    primary_model: str | None = "claude-opus-4-8",
) -> AgentHealth:
    ah = AgentHealth(agent=agent)
    ah.composite = composite
    ah.composite_display = int(round(composite)) if composite is not None else None
    ah.primary_model = primary_model
    ah.cost_score = 80.0
    ah.quality_score = 80.0
    ah.reliability_score = 80.0
    return ah


def _make_rec_config(
    score_margin_floor: float = 0.5,  # reachable on the 1-5 margin scale (max 1.0)
    pass_rate_margin_floor: float = 0.05,
    min_eval_n: int = 3,
    min_savings_usd: float = 0.01,
) -> RecommendationConfig:
    return RecommendationConfig(
        score_margin_floor=score_margin_floor,
        pass_rate_margin_floor=pass_rate_margin_floor,
        min_eval_n=min_eval_n,
        min_savings_usd=min_savings_usd,
    )


def _passing_evals(n: int = 5, score: float = 4.8) -> list[_EvalRecord]:
    """N passing eval records with no hard-fails."""
    return [_make_eval(verdict="pass", weighted_score=score) for _ in range(n)]


# ──────────────────────────────────────────────────────────────────
# MUST 1 — kind validated against RECOMMENDATION_KINDS frozenset


class TestRecommendationKindValidation:
    """MUST 1: Recommendation.kind must be in RECOMMENDATION_KINDS."""

    def test_valid_kinds_construct_cleanly(self):
        headroom = EvalHeadroom(
            weighted_score_margin=2.0,
            pass_rate_margin=0.1,
            hard_fails=0,
            sample_n=10,
            rubric_threshold=4.0,
            passed=True,
        )
        for kind in RECOMMENDATION_KINDS:
            rec = Recommendation(
                agent="a",
                kind=kind,
                current_model=None,
                candidate_model=None,
                projected_usd_delta=None,
                projected_points_delta=None,
                rationale="ok",
                safety=headroom,
            )
            assert rec.kind == kind

    def test_invalid_kind_raises_value_error(self):
        """strip-RED MUST 1: unknown kind must raise ValueError at construction."""
        headroom = EvalHeadroom(
            weighted_score_margin=2.0,
            pass_rate_margin=0.1,
            hard_fails=0,
            sample_n=10,
            rubric_threshold=4.0,
            passed=True,
        )
        with pytest.raises(ValueError, match="RECOMMENDATION_KINDS"):
            Recommendation(
                agent="a",
                kind="invalid_kind",
                current_model=None,
                candidate_model=None,
                projected_usd_delta=None,
                projected_points_delta=None,
                rationale="bad",
                safety=headroom,
            )

    def test_recommendation_kinds_frozenset_is_frozen(self):
        """RECOMMENDATION_KINDS must be a frozenset (immutable)."""
        assert isinstance(RECOMMENDATION_KINDS, frozenset)

    def test_strip_red_empty_string_kind_rejected(self):
        """strip-RED: empty string kind must be rejected."""
        headroom = EvalHeadroom(0.0, 0.0, 0, 0, 4.0, False)
        with pytest.raises(ValueError):
            Recommendation(
                agent="a",
                kind="",
                current_model=None,
                candidate_model=None,
                projected_usd_delta=None,
                projected_points_delta=None,
                rationale="",
                safety=headroom,
            )


# ──────────────────────────────────────────────────────────────────
# MUST 2–6 — composite conjunctive guard (_eval_headroom)


class TestEvalHeadroom:
    """MUST 2–6: all four predicates + fail-safe. Each strip-RED removes one predicate."""

    def _passing_config(self) -> RecommendationConfig:
        return _make_rec_config(
            score_margin_floor=0.5,
            pass_rate_margin_floor=0.05,
            min_eval_n=3,
        )

    def test_all_predicates_pass(self):
        """All four predicates satisfied → EvalHeadroom.passed = True."""
        evals = _passing_evals(n=5, score=4.8)
        cfg = self._passing_config()
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is True
        assert h.sample_n == 5
        assert h.hard_fails == 0

    def test_strip_red_p1_score_margin_fails(self):
        """MUST 2 strip-RED: score below threshold → passed = False."""
        # weighted_score=3.0, threshold=4.0, margin=3.0-4.0=-1.0 < floor(0.5)
        evals = [_make_eval(verdict="pass", weighted_score=3.0) for _ in range(5)]
        cfg = self._passing_config()
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is False
        assert h.weighted_score_margin < cfg.score_margin_floor

    def test_strip_red_p2_pass_rate_margin_fails(self):
        """MUST 3 strip-RED: pass-rate margin below floor → passed = False.

        ISOLATES p2: the fixture clears p1/p3/p4 so p2 is the ONLY failing
        predicate. Failing scores are pinned at 5.0 (independent of verdict) so
        the mean weighted score stays at 5.0 — p1 margin = 5.0-4.0 = 1.0 >= floor
        — while pass-rate = 4/7 = 0.571, margin = 0.571-0.8 = -0.229 < floor.
        Deleting the p2 check makes this go GREEN, so it locks MUST 3 (the prior
        3-pass@4.8 + 2-fail@2.0 fixture also failed p1 (mean 3.68), so it stayed
        red with p2 removed and did NOT lock the pass-rate guard).
        """
        evals = [_make_eval(verdict="pass", weighted_score=5.0)] * 4 + [
            _make_eval(verdict="fail", weighted_score=5.0)
        ] * 3
        cfg = self._passing_config()
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is False
        # p2 is the uniquely-failing predicate: p1/p3/p4 all hold.
        assert h.pass_rate_margin < cfg.pass_rate_margin_floor  # p2 fails
        assert h.weighted_score_margin >= cfg.score_margin_floor  # p1 holds
        assert h.hard_fails == 0  # p3 holds
        assert h.sample_n >= cfg.min_eval_n  # p4 holds

    def test_strip_red_p3_hard_fails_present(self):
        """MUST 4 strip-RED: any hard-fail → passed = False."""
        evals = _passing_evals(n=5, score=4.8)
        # Inject one hard-fail
        evals[0] = _make_eval(
            verdict="pass", weighted_score=4.8, hard_fails=["critical_format_error"]
        )
        cfg = self._passing_config()
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is False
        assert h.hard_fails >= 1

    def test_strip_red_p4_sample_n_below_min(self):
        """MUST 5 strip-RED: sample_n < min_eval_n → passed = False."""
        evals = _passing_evals(n=2, score=4.8)  # n=2 < min_eval_n=3
        cfg = self._passing_config()  # min_eval_n=3
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is False
        assert h.sample_n < cfg.min_eval_n

    def test_strip_red_failsafe_empty_evals(self):
        """MUST 6 fail-safe strip-RED: empty eval list → passed = False (no downgrade)."""
        cfg = self._passing_config()
        h = _eval_headroom([], rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is False
        assert h.sample_n == 0

    def test_failsafe_non_finite_weighted_score(self):
        """Fail-safe (Codex #616): a non-finite weighted_score → passed = False.

        Every other predicate is satisfied (5 passing evals, sample_n=5, zero
        hard-fails, pass-rate 1.0). Without the finiteness guard, the inf score
        makes mean_weighted = inf, weighted_score_margin = inf >= floor (p1 True),
        so the whole conjunction passes and a downgrade fires on garbage data —
        the strip-RED for the finiteness fail-safe.
        """
        evals = _passing_evals(n=4, score=4.8) + [
            _make_eval(verdict="pass", weighted_score=float("inf"))
        ]
        cfg = self._passing_config()
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is False
        # Distinguishes the finiteness fail-safe from the sample_n fail-safe:
        # there ARE enough scorable evals, the suppression is the inf guard.
        assert h.sample_n == 5

    def test_hard_fail_on_judge_error_blocks_downgrade(self):
        """Fail-safe (Codex #616): a hard-fail on a judge_error eval blocks p3.

        The 5 passing scorable evals clear p1/p2/p4; the extra judge_error eval is
        excluded from `scorable` but carries a hard-fail. Counting hard_fails over
        the FULL window (not just scorable) makes p3 fail → passed=False. Strip-RED:
        with the old scorable-only count, hard_fails=0, p3 holds, passed=True.
        """
        evals = _passing_evals(n=5, score=4.8) + [
            _make_eval(
                verdict="judge_error",
                weighted_score=0.0,
                hard_fails=["critical_format_error"],
            )
        ]
        cfg = self._passing_config()
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.passed is False
        # p3 is the failing predicate: the hard-fail came from the judge_error
        # record, and sample_n still counts only the 5 scorable evals.
        assert h.hard_fails == 1
        assert h.sample_n == 5

    def test_judge_error_excluded_from_scorable(self):
        """judge_error records must not count as scorable (not pass/fail)."""
        evals = (
            _passing_evals(n=5, score=4.8)
            + [_make_eval(verdict="judge_error", weighted_score=0.0)] * 10
        )
        cfg = self._passing_config()
        h = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)
        assert h.sample_n == 5  # judge_error excluded


# ──────────────────────────────────────────────────────────────────
# MUST 7 — no-LLM enforcement (import discipline)


class TestNoLLMEnforcement:
    """MUST 10: recommend.py must not import agent/eval/tuning/dream (conftest + source)."""

    def test_recommend_source_has_no_forbidden_imports(self):
        """Source-level grep: recommend.py must not import agent/eval/tuning/dream directly.

        The spec/54 MUST-10 forbidden set is the four LLM-spend-bearing modules:
        agent.py, eval.py, tuning.py, dream.py. The ``agent`` patterns use
        ``agent\\s+import`` / ``agent($|\\s)`` boundaries so the allowed pure-read
        ``from ..agent_registry import ...`` import does NOT false-match (``agent``
        there is followed by ``_registry``, not whitespace).
        """
        import re

        # Resolve the submodule explicitly via importlib (robust regardless of
        # what the package re-exports).
        import importlib

        rec_mod = importlib.import_module("atomic_agents.advisor.recommend")
        source_path = Path(rec_mod.__file__)
        source = source_path.read_text(encoding="utf-8")
        # Match import/from lines, INCLUDING function-local (indented) imports —
        # `^\s*` so an indented `from ..eval import ...` inside a function body is
        # also caught, not just module-top-level imports (the runtime conftest
        # guard covers construction; this static check defends the source).
        forbidden_line_patterns = [
            r"^\s*from\s+[.\w]*tuning\s+import",
            r"^\s*from\s+[.\w]*eval\s+import",
            r"^\s*from\s+[.\w]*dream\s+import",
            # `agent\s+import` excludes the allowed `..agent_registry import`
            # (agent there is followed by `_registry`, not whitespace).
            r"^\s*from\s+[.\w]*agent\s+import",
            r"^\s*import\s+[.\w]*tuning",
            r"^\s*import\s+[.\w]*eval",
            r"^\s*import\s+[.\w]*dream",
            r"^\s*import\s+[.\w]*agent($|\s)",
        ]

        for pattern in forbidden_line_patterns:
            matches = re.findall(pattern, source, re.MULTILINE)
            assert not matches, (
                f"recommend.py contains forbidden import statement matching {pattern!r}: {matches}"
            )


# ──────────────────────────────────────────────────────────────────
# MUST 8 — counterfactual does not mutate original run_records


class TestCounterfactualNonMutation:
    """MUST 8: dataclasses.replace used; originals unchanged after recommend()."""

    def test_run_records_not_mutated(self):
        """Original run list model+cost fields unchanged after recommend() returns.

        Drives the FULL counterfactual loop: targets is a real FleetTargets (not
        None, so _compute_point_impact does NOT early-return) and the opus runs
        carry real tokens so the opus->sonnet reprice yields a saving — only then
        does the `dataclasses.replace` substitution loop (the sole code path that
        could mutate originals) actually execute. A strip-RED check (injecting
        `r.model = candidate` into that loop) must fail this test.
        """
        from atomic_agents.advisor.targets import FleetTargets

        # Multiple primary runs with real tokens so the opus->sonnet reprice saves.
        runs = [
            _make_run(
                model="claude-opus-4-8",
                cost_usd=0.50,
                input_tokens=2000,
                output_tokens=1000,
            )
            for _ in range(4)
        ]
        original_models = [r.model for r in runs]
        original_costs = [r.cost_usd for r in runs]

        ah = _make_agent_health(primary_model="claude-opus-4-8", composite=80.0)
        evals = _passing_evals(n=6, score=4.8)
        cfg = _make_rec_config(min_savings_usd=0.0)

        recs = recommend(
            agent_health=ah,
            run_records=runs,
            eval_records=evals,
            rec_config=cfg,
            rubric_threshold=4.0,
            targets=FleetTargets(),  # real targets -> counterfactual loop RUNS
        )

        # The savings rec must have fired — that proves _compute_point_impact was
        # invoked with real targets + a real composite, so its `dataclasses.replace`
        # substitution loop (the only path that could mutate originals) executed.
        # This makes the non-mutation check below non-vacuous. (We do NOT assert on
        # projected_points_delta: a same-cutoff reprice can score to a ~None/0.0
        # delta — see TestPointImpactSymmetricBaseline — which is orthogonal to
        # whether the loop ran.)
        savings = [r for r in recs if r.kind == "savings_cost"]
        assert savings, (
            "expected a savings_cost rec (opus->sonnet reprice saves) so the "
            "counterfactual loop runs and the non-mutation check is non-vacuous"
        )

        # Originals must be byte-for-byte unchanged (model AND repriced cost field).
        assert [r.model for r in runs] == original_models
        assert [r.cost_usd for r in runs] == original_costs

    def test_dataclasses_replace_produces_new_object(self):
        """dataclasses.replace gives a new object without modifying the original."""
        run = _make_run(model="claude-opus-4-8")
        cf_run = dataclasses.replace(run, model="claude-haiku-4-5", cost_usd=0.001)
        assert run.model == "claude-opus-4-8"
        assert cf_run.model == "claude-haiku-4-5"
        assert run is not cf_run


# ──────────────────────────────────────────────────────────────────
# Point-impact counterfactual — symmetric-baseline coherence (spec/54 §7)


class TestPointImpactSymmetricBaseline:
    """_compute_point_impact must isolate the model swap, not the prior-window drop.

    Regression for the phantom-overstatement bug: the counterfactual is scored with
    runs_prior_30d=[] (the pure core has no prior window), so its spend_vs_trend
    metric goes to a no_data row excluded from the cost sub-score. If the diff were
    taken against the REAL-prior-window baseline composite (which INCLUDES a possibly
    floored, critical-capping spend_vs_trend), a cost-neutral same-family swap would
    show a large positive point delta purely from spend_vs_trend disappearing. The
    fix re-scores the baseline with the SAME reduced inputs so both sides drop
    spend_vs_trend identically.
    """

    def _parsed_default_targets(self):
        import tempfile
        from atomic_agents.advisor.targets import parse_targets

        return parse_targets(Path(tempfile.mkdtemp()))

    def _mk(self, cost, days_ago, today, model="claude-haiku-4-5"):
        ts = datetime.combine(
            today - timedelta(days=days_ago), datetime.min.time()
        ).replace(tzinfo=timezone.utc)
        return RunRecord(
            ts=ts,
            agent="a",
            trigger="cron",
            model=model,
            input_tokens=2000,
            output_tokens=1000,
            cost_usd=cost,
            cache_hit_tokens=0,
            cache_miss_tokens=2000,
            latency_ms=100,
            status="completed",
            summary="ok",
            parent_run_id=None,
            extra={},
        )

    def test_cost_neutral_same_family_swap_yields_near_zero_delta(self):
        """A haiku→haiku-dated swap (same PRICING, no cutoff crossing) → ~0.0 pts.

        strip-RED: the baseline here is critical-capped at 60 by a floored
        spend_vs_trend (recent spend $5/day vs prior $0.01/day). Before the
        symmetric-baseline fix this returned a phantom ~+38 (spend_vs_trend simply
        dropping out of the counterfactual). With the fix the swap moves NO scored
        metric, so the delta must round to 0.0.
        """
        from atomic_agents.advisor.recommend import (
            _compute_point_impact,
            _reprice_run,
        )
        from atomic_agents.advisor.score import _score_agent_from_data

        today = date(2026, 6, 20)
        targets = self._parsed_default_targets()
        # Recent 30d: high flat spend. Prior 30d: tiny spend → spend_ratio huge →
        # spend_vs_trend floors → baseline composite critical-capped at 60.
        runs_30d = [self._mk(5.0, i, today) for i in range(0, 30)]
        runs_prior = [self._mk(0.01, i, today) for i in range(0, 30)]
        evals = [
            _make_eval(verdict="pass", weighted_score=4.6, ts_date=today)
            for _ in range(12)
        ]
        six = today - timedelta(days=6)
        ps = today - timedelta(days=13)
        pe = today - timedelta(days=7)

        baseline = _score_agent_from_data(
            agent="a",
            runs_30d=runs_30d,
            runs_prior_30d=runs_prior,
            eval_records=evals,
            targets=targets,
            today=today,
            six_days_ago=six,
            prior_7_start=ps,
            prior_7_end=pe,
        )
        # Precondition for the regression: baseline IS critical-capped (composite 60)
        # by a floored spend_vs_trend, the exact shape that produced the phantom.
        svt = next(
            (r for r in baseline.scorecard if r.metric == "spend_vs_trend"), None
        )
        assert svt is not None and svt.status == "crit"
        assert baseline.composite == 60.0

        candidate = "claude-haiku-4-5-20251001"  # same family, same PRICING tier
        delta = _compute_point_impact(
            agent_health=baseline,
            run_records=runs_30d,
            eval_records=evals,
            candidate_model=candidate,
            repriced_cost_per_run=lambda r: _reprice_run(r, candidate),
            targets=targets,
            today=today,
        )
        assert delta == 0.0, (
            "a cost-neutral same-family swap moves no scored metric, so the "
            f"point-impact delta must be 0.0; got {delta!r} (a large positive value "
            "is the phantom from spend_vs_trend dropping out of the counterfactual)"
        )

    def test_cutoff_crossing_swap_still_moves_points(self):
        """A sonnet→haiku swap crosses the cheap cutoff → NON-zero positive delta.

        This guards against an over-correction: the symmetric-baseline fix must NOT
        flatten a swap that genuinely moves cheaper_model_share. sonnet output rate
        15.0 is not cheap; haiku 4.0 < 5.0 cutoff is — so the swap moves
        cheaper_model_share 0%→100% and the delta must be > 0.
        """
        from atomic_agents.advisor.recommend import (
            _compute_point_impact,
            _reprice_run,
        )
        from atomic_agents.advisor.score import _score_agent_from_data

        today = date(2026, 6, 20)
        targets = self._parsed_default_targets()
        runs_30d = [
            self._mk(5.0, i, today, model="claude-sonnet-4-6-20260101")
            for i in range(0, 30)
        ]
        runs_prior = [
            self._mk(5.0, i, today, model="claude-sonnet-4-6-20260101")
            for i in range(0, 30)
        ]
        evals = [
            _make_eval(verdict="pass", weighted_score=4.6, ts_date=today)
            for _ in range(12)
        ]
        six = today - timedelta(days=6)
        ps = today - timedelta(days=13)
        pe = today - timedelta(days=7)
        baseline = _score_agent_from_data(
            agent="a",
            runs_30d=runs_30d,
            runs_prior_30d=runs_prior,
            eval_records=evals,
            targets=targets,
            today=today,
            six_days_ago=six,
            prior_7_start=ps,
            prior_7_end=pe,
        )
        candidate = "claude-haiku-4-5"
        delta = _compute_point_impact(
            agent_health=baseline,
            run_records=runs_30d,
            eval_records=evals,
            candidate_model=candidate,
            repriced_cost_per_run=lambda r: _reprice_run(r, candidate),
            targets=targets,
            today=today,
        )
        assert delta is not None and delta > 0.0, (
            "a cutoff-crossing sonnet→haiku swap moves cheaper_model_share and must "
            f"still produce a positive point delta; got {delta!r}"
        )


# ──────────────────────────────────────────────────────────────────
# MUST 9 — same-family candidate in PRICING; fail-pessimistic if absent


class TestCandidateResolution:
    """MUST 9: candidate must be in PRICING; unknown model → no rec (fail-pessimistic)."""

    def test_known_model_gets_same_family_candidate(self):
        """claude-opus-4-8 → candidate in PRICING."""
        from atomic_agents._costs import PRICING

        candidate, source = _resolve_candidate(
            "claude-opus-4-8", "general", RecommendationConfig()
        )
        assert candidate is not None
        assert candidate in PRICING
        assert source == "default_same_family"

    def test_unknown_model_returns_none(self):
        """strip-RED MUST 9: unknown model → None candidate (fail-pessimistic)."""
        candidate, source = _resolve_candidate(
            "unknown-model-xyz", "general", RecommendationConfig()
        )
        assert candidate is None

    def test_cheapest_model_returns_none(self):
        """claude-haiku-4-5 is the cheapest Anthropic tier — no downgrade candidate."""
        candidate, _ = _resolve_candidate(
            "claude-haiku-4-5", "general", RecommendationConfig()
        )
        assert candidate is None

    def test_moonshot_no_candidate(self):
        """All moonshot models are same price — no downgrade."""
        candidate, _ = _resolve_candidate(
            "moonshot/kimi-k2.6", "general", RecommendationConfig()
        )
        assert candidate is None

    def test_operator_configured_overrides_default(self):
        """Operator work_type_allowed_models overrides default downgrade map."""

        cfg = RecommendationConfig(
            work_type_allowed_models={"general": ["claude-haiku-4-5"]}
        )
        candidate, source = _resolve_candidate("claude-opus-4-8", "general", cfg)
        assert candidate == "claude-haiku-4-5"
        assert source == "operator_configured"

    def test_vertex_family_downgrade(self):
        """vertex/gemini-2.5-pro → vertex/gemini-2.5-flash."""
        candidate, source = _resolve_candidate(
            "vertex/gemini-2.5-pro", "general", RecommendationConfig()
        )
        assert candidate == "vertex/gemini-2.5-flash"
        assert source == "default_same_family"

    def test_gpt5_downgrade(self):
        """gpt-5 → gpt-5-mini."""
        candidate, source = _resolve_candidate(
            "gpt-5", "general", RecommendationConfig()
        )
        assert candidate == "gpt-5-mini"
        assert source == "default_same_family"


# ──────────────────────────────────────────────────────────────────
# MUST 10 — import boundary with tuning.py (already covered in MUST 7)
# MUST 11 — wrong eval window → NO downgrade (window alignment)


class TestEvalWindowAlignment:
    """MUST 11: passing evals from wrong window must not bypass the guard.

    The window alignment invariant: evals passed to recommend() MUST be from
    the same 30d window as used to score agent_health.quality_score.
    This test verifies that stale (zero) evals correctly block downgrade recs,
    and that a wrong-window eval list with sub-N records also blocks them.
    """

    def test_empty_evals_no_downgrade(self):
        """No evals (wrong/missing window) → no savings_cost rec emitted."""
        ah = _make_agent_health(primary_model="claude-opus-4-8")
        cfg = _make_rec_config(min_savings_usd=0.0)
        run = _make_run(model="claude-opus-4-8", cost_usd=0.10)

        recs = recommend(
            agent_health=ah,
            run_records=[run],
            eval_records=[],  # empty = stale/missing window
            rec_config=cfg,
            rubric_threshold=4.0,
        )
        cost_recs = [r for r in recs if r.kind == "savings_cost"]
        assert cost_recs == [], "Empty eval list must block savings_cost rec"

    def test_sub_n_evals_no_downgrade(self):
        """Sub-N evals (fewer than min_eval_n) → no savings_cost rec emitted."""
        ah = _make_agent_health(primary_model="claude-opus-4-8")
        cfg = _make_rec_config(min_eval_n=5, min_savings_usd=0.0)
        run = _make_run(model="claude-opus-4-8", cost_usd=0.10)
        evals = _passing_evals(n=2, score=4.8)  # n=2 < min_eval_n=5

        recs = recommend(
            agent_health=ah,
            run_records=[run],
            eval_records=evals,
            rec_config=cfg,
            rubric_threshold=4.0,
        )
        cost_recs = [r for r in recs if r.kind == "savings_cost"]
        assert cost_recs == [], "Sub-N evals must block savings_cost rec"


# ──────────────────────────────────────────────────────────────────
# Savings cost rec end-to-end


class TestSavingsCostRec:
    """End-to-end savings cost recommendation tests."""

    def test_savings_rec_fires_with_passing_guard(self):
        """A well-performing agent with an expensive model gets a savings rec."""
        ah = _make_agent_health(primary_model="claude-opus-4-8")
        # Expensive model: 30d run costs $5.00 total
        runs = [_make_run(model="claude-opus-4-8", cost_usd=0.50) for _ in range(10)]
        evals = _passing_evals(n=10, score=4.8)
        cfg = _make_rec_config(
            score_margin_floor=0.5,
            pass_rate_margin_floor=0.01,
            min_eval_n=5,
            min_savings_usd=0.01,
        )

        recs = recommend(
            agent_health=ah,
            run_records=runs,
            eval_records=evals,
            rec_config=cfg,
            rubric_threshold=4.0,
        )
        cost_recs = [r for r in recs if r.kind == "savings_cost"]
        assert len(cost_recs) >= 1
        rec = cost_recs[0]
        assert rec.candidate_model is not None
        assert rec.projected_usd_delta is not None
        assert rec.projected_usd_delta < 0  # savings (negative delta)

    def test_default_config_score_margin_floor_is_reachable(self):
        """Pin the PRODUCTION default floor to a value reachable on the 1-5 scale.

        weighted_score is clamped to [1.0, 5.0]; weighted_score_margin =
        mean - rubric_threshold, so with the default threshold 4.0 the max margin
        is 1.0. A floor > 1.0 (the old 5.0, or even the earlier-spec 2.0) is
        permanently unreachable and silently kills the whole savings feature.
        """
        assert RecommendationConfig().score_margin_floor == 0.5
        assert RecommendationConfig().score_margin_floor <= 1.0

    def test_default_config_emits_savings_rec(self):
        """A strong agent with PRODUCTION-default RecommendationConfig() gets a rec.

        This is the units pin for guard P1: no floor override. Uses real 5.0-scale
        evals (>= min_eval_n=10) and a real opus->sonnet saving. If the default
        floor were unreachable, headroom.passed would be False and recs == [].
        """
        ah = _make_agent_health(primary_model="claude-opus-4-8")
        # 12 expensive opus runs, $0.50 each → repriced at sonnet is much cheaper.
        runs = [_make_run(model="claude-opus-4-8", cost_usd=0.50) for _ in range(12)]
        # 12 strong-but-not-perfect evals: mean 4.6 → margin 0.6 >= floor 0.5.
        evals = _passing_evals(n=12, score=4.6)
        cfg = RecommendationConfig()  # PRODUCTION default — no overrides

        recs = recommend(
            agent_health=ah,
            run_records=runs,
            eval_records=evals,
            rec_config=cfg,
            rubric_threshold=4.0,
        )
        cost_recs = [r for r in recs if r.kind == "savings_cost"]
        assert len(cost_recs) == 1, (
            "default RecommendationConfig() must be able to emit a savings_cost "
            f"rec for a strong agent; got {recs!r}"
        )
        assert cost_recs[0].safety.passed is True
        assert cost_recs[0].projected_usd_delta is not None
        assert cost_recs[0].projected_usd_delta < 0

    def test_no_savings_rec_when_guard_fails_no_evals(self):
        """strip-RED: guard fails (no evals) → no savings_cost rec."""
        ah = _make_agent_health(primary_model="claude-opus-4-8")
        runs = [_make_run(model="claude-opus-4-8", cost_usd=0.50) for _ in range(10)]
        cfg = _make_rec_config(min_savings_usd=0.0)

        recs = recommend(
            agent_health=ah,
            run_records=runs,
            eval_records=[],  # guard fails
            rec_config=cfg,
            rubric_threshold=4.0,
        )
        cost_recs = [r for r in recs if r.kind == "savings_cost"]
        assert cost_recs == []

    def test_no_savings_rec_when_below_min_savings(self):
        """strip-RED: savings < min_savings_usd → rec not emitted."""
        ah = _make_agent_health(primary_model="claude-opus-4-8")
        # Very cheap runs: tiny delta
        runs = [_make_run(model="claude-opus-4-8", cost_usd=0.0001) for _ in range(2)]
        evals = _passing_evals(n=10, score=4.8)
        cfg = _make_rec_config(
            min_savings_usd=1000.0,  # very high threshold
            min_eval_n=5,
        )

        recs = recommend(
            agent_health=ah,
            run_records=runs,
            eval_records=evals,
            rec_config=cfg,
            rubric_threshold=4.0,
        )
        cost_recs = [r for r in recs if r.kind == "savings_cost"]
        assert cost_recs == []

    def test_savings_rec_has_eval_headroom_populated(self):
        """A fired savings rec has EvalHeadroom.passed=True."""
        ah = _make_agent_health(primary_model="claude-opus-4-8")
        runs = [_make_run(model="claude-opus-4-8", cost_usd=0.50) for _ in range(10)]
        evals = _passing_evals(n=10, score=4.8)
        cfg = _make_rec_config(
            score_margin_floor=0.5,
            pass_rate_margin_floor=0.01,
            min_eval_n=5,
            min_savings_usd=0.01,
        )

        recs = recommend(
            agent_health=ah,
            run_records=runs,
            eval_records=evals,
            rec_config=cfg,
            rubric_threshold=4.0,
        )
        cost_recs = [r for r in recs if r.kind == "savings_cost"]
        assert cost_recs, "savings rec must fire for a passing-guard fixture"
        assert cost_recs[0].safety.passed is True

    def test_repricing_with_cache_hits_uses_discount(self):
        """Repricing respects cache_hit_tokens (uses CACHE_HIT_DISCOUNT)."""

        run_no_cache = _make_run(
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=500,
            cache_hit_tokens=0,
            cost_usd=0.05,
        )
        run_with_cache = _make_run(
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=500,
            cache_hit_tokens=800,
            cost_usd=0.05,
        )
        candidate = "claude-sonnet-4-6-20260101"
        cost_no_cache = _reprice_run(run_no_cache, candidate)
        cost_with_cache = _reprice_run(run_with_cache, candidate)
        # Cache hits should reduce repriced cost
        assert cost_with_cache < cost_no_cache


# ──────────────────────────────────────────────────────────────────
# Governance rec


class TestGovernanceRec:
    """Governance recs fire for absent, no-block, and parse-error states."""

    def test_governance_rec_fires_for_rationale(self):
        """A non-None governance_rationale produces a governance rec."""
        ah = _make_agent_health(primary_model="claude-haiku-4-5")
        cfg = _make_rec_config()

        recs = recommend(
            agent_health=ah,
            run_records=[],
            eval_records=[],
            rec_config=cfg,
            governance_rationale="governance.md is absent",
        )
        gov_recs = [r for r in recs if r.kind == "governance"]
        assert len(gov_recs) == 1
        assert "absent" in gov_recs[0].rationale

    def test_no_governance_rec_when_rationale_is_none(self):
        """strip-RED: None governance_rationale → no governance rec."""
        ah = _make_agent_health()
        cfg = _make_rec_config()

        recs = recommend(
            agent_health=ah,
            run_records=[],
            eval_records=[],
            rec_config=cfg,
            governance_rationale=None,
        )
        gov_recs = [r for r in recs if r.kind == "governance"]
        assert gov_recs == []

    def test_governance_rec_has_null_deltas(self):
        """Governance recs have no usd/points deltas (N/A)."""
        ah = _make_agent_health()
        cfg = _make_rec_config()

        recs = recommend(
            agent_health=ah,
            run_records=[],
            eval_records=[],
            rec_config=cfg,
            governance_rationale="governance.md is absent",
        )
        gov_recs = [r for r in recs if r.kind == "governance"]
        assert gov_recs[0].projected_usd_delta is None
        assert gov_recs[0].projected_points_delta is None


class TestGovernanceRationaleBranches:
    """_governance_rec_rationale maps each registry parse state to a distinct string.

    The recommend_fleet integration test only exercises the ABSENT branch (and via a
    hardcoded string, not the real parse state), so the PRESENT_NO_BLOCK and
    PRESENT_INVALID branches need direct unit coverage — each could silently regress
    to None (dropping the governance rec) with no signal otherwise.
    """

    def test_absent_branch(self):
        from atomic_agents.advisor.recommend import _governance_rec_rationale

        out = _governance_rec_rationale(has_governance=False, governance=None)
        assert out is not None and "absent" in out.lower()

    def test_present_no_block_branch(self):
        """has_governance=True, governance=None → the 'no YAML block' rationale."""
        from atomic_agents.advisor.recommend import _governance_rec_rationale

        out = _governance_rec_rationale(has_governance=True, governance=None)
        assert out is not None
        assert "block" in out.lower()
        # strip-RED: must NOT be the absent rationale (distinct branch).
        assert "absent" not in out.lower()

    def test_present_invalid_branch_surfaces_parse_errors(self):
        """A governance object carrying parse_errors → the parse-error rationale."""
        from atomic_agents.advisor.recommend import _governance_rec_rationale

        class _Gov:
            parse_errors = ["owner: expected str, got int", "bad lifecycle_status"]

        out = _governance_rec_rationale(has_governance=True, governance=_Gov())
        assert out is not None
        assert "parse error" in out.lower()
        # The distinct parse-error text must include the actual errors.
        assert "owner" in out and "lifecycle_status" in out

    def test_present_valid_branch_returns_none(self):
        """strip-RED: a valid governance object (no parse_errors) → None (no rec)."""
        from atomic_agents.advisor.recommend import _governance_rec_rationale

        class _Gov:
            parse_errors: list = []

        out = _governance_rec_rationale(has_governance=True, governance=_Gov())
        assert out is None


# ──────────────────────────────────────────────────────────────────
# Quality report recs


class TestQualityReportRec:
    """quality_report recs surface already-written tuning report files."""

    def test_quality_rec_fires_for_hard_fail_report(self, tmp_path):
        """A tuning report with hard_fails: true in frontmatter → quality_report rec."""
        agent = "test-agent"
        reports_dir = tmp_path / agent / "evals" / "tuning_reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "2026-06-25.md").write_text(
            "---\nhas_hard_fails: true\nscore: 3.0\n---\n\nSome report text.\n"
        )

        paths = _load_tuning_reports(tmp_path, agent)
        assert len(paths) == 1

        ah = _make_agent_health(agent=agent)
        cfg = _make_rec_config()

        recs = recommend(
            agent_health=ah,
            run_records=[],
            eval_records=[],
            rec_config=cfg,
            tuning_report_paths=paths,
            rubric_threshold=4.0,
        )
        qual_recs = [r for r in recs if r.kind == "quality_report"]
        assert len(qual_recs) >= 1
        assert "hard-fail" in qual_recs[0].rationale.lower()

    def test_quality_rec_fires_for_below_threshold(self, tmp_path):
        """A tuning report with score below rubric_threshold → quality_report rec."""
        agent = "test-agent"
        reports_dir = tmp_path / agent / "evals" / "tuning_reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "2026-06-25.md").write_text(
            "---\nscore: 2.5\n---\n\nBelow threshold report.\n"
        )

        paths = _load_tuning_reports(tmp_path, agent)
        ah = _make_agent_health(agent=agent)
        cfg = _make_rec_config()

        recs = recommend(
            agent_health=ah,
            run_records=[],
            eval_records=[],
            rec_config=cfg,
            tuning_report_paths=paths,
            rubric_threshold=4.0,
        )
        qual_recs = [r for r in recs if r.kind == "quality_report"]
        assert len(qual_recs) >= 1
        assert "threshold" in qual_recs[0].rationale.lower()

    def test_no_quality_rec_for_healthy_report(self, tmp_path):
        """strip-RED: a report with no hard-fails and score above threshold → no rec."""
        agent = "test-agent"
        reports_dir = tmp_path / agent / "evals" / "tuning_reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "2026-06-25.md").write_text(
            "---\nhas_hard_fails: false\nscore: 4.8\n---\n\nAll good.\n"
        )

        paths = _load_tuning_reports(tmp_path, agent)
        ah = _make_agent_health(agent=agent)
        cfg = _make_rec_config()

        recs = recommend(
            agent_health=ah,
            run_records=[],
            eval_records=[],
            rec_config=cfg,
            tuning_report_paths=paths,
            rubric_threshold=4.0,
        )
        qual_recs = [r for r in recs if r.kind == "quality_report"]
        assert qual_recs == []

    def test_missing_report_file_skipped_gracefully(self, tmp_path):
        """strip-RED: a report file that is deleted mid-render does not crash."""
        agent = "test-agent"
        # Create a non-existent path (simulating deletion between glob and read)
        missing_path = tmp_path / agent / "evals" / "tuning_reports" / "deleted.md"

        ah = _make_agent_health(agent=agent)
        cfg = _make_rec_config()

        # Should not raise — per-file OSError is caught (fail-soft)
        recs = recommend(
            agent_health=ah,
            run_records=[],
            eval_records=[],
            rec_config=cfg,
            tuning_report_paths=[missing_path],
            rubric_threshold=4.0,
        )
        # No crash, and no rec from the missing file
        qual_recs = [r for r in recs if r.kind == "quality_report"]
        assert qual_recs == []

    def test_no_tuning_dir_returns_empty_paths(self, tmp_path):
        """_load_tuning_reports returns [] for agents with no tuning_reports/ dir."""
        paths = _load_tuning_reports(tmp_path, "no-such-agent")
        assert paths == []


# ──────────────────────────────────────────────────────────────────
# Rubric threshold reader


class TestLoadRubricThreshold:
    """_load_rubric_threshold reads threshold_pass from rubric.md frontmatter."""

    def test_reads_custom_threshold(self, tmp_path):
        """threshold_pass: 3.5 in rubric.md → returns 3.5."""
        agent = "test-agent"
        evals_dir = tmp_path / agent / "evals"
        evals_dir.mkdir(parents=True)
        (evals_dir / "rubric.md").write_text(
            "---\nthreshold_pass: 3.5\n---\n\nRubric text.\n"
        )
        threshold = _load_rubric_threshold(tmp_path, agent)
        assert threshold == pytest.approx(3.5)

    def test_absent_rubric_returns_default(self, tmp_path):
        """Absent rubric.md → 4.0 default."""
        threshold = _load_rubric_threshold(tmp_path, "no-such-agent")
        assert threshold == pytest.approx(4.0)

    def test_malformed_rubric_returns_default(self, tmp_path):
        """Malformed threshold_pass → 4.0 default."""
        agent = "test-agent"
        evals_dir = tmp_path / agent / "evals"
        evals_dir.mkdir(parents=True)
        (evals_dir / "rubric.md").write_text(
            "---\nthreshold_pass: not-a-number\n---\n\nRubric.\n"
        )
        threshold = _load_rubric_threshold(tmp_path, agent)
        assert threshold == pytest.approx(4.0)

    def test_malformed_yaml_frontmatter_returns_default(self, tmp_path):
        """strip-RED: a rubric.md with syntactically-broken YAML frontmatter must
        fail SOFT to 4.0, not raise. yaml.YAMLError is NOT a subclass of ValueError,
        so the loader must name it explicitly — without it this case raises a
        ParserError instead of degrading (the docstring's 'fail-soft to 4.0' lie)."""
        agent = "test-agent"
        evals_dir = tmp_path / agent / "evals"
        evals_dir.mkdir(parents=True)
        # Unclosed flow sequence → yaml ParserError (a yaml.YAMLError subclass).
        (evals_dir / "rubric.md").write_text(
            "---\nthreshold_pass: [bad\n---\n\nRubric.\n"
        )
        threshold = _load_rubric_threshold(tmp_path, agent)
        assert threshold == pytest.approx(4.0)

    def test_custom_threshold_changes_headroom(self, tmp_path):
        """strip-RED: agent with rubric threshold_pass: 3.5 gives different headroom
        than the default 4.0. This verifies the reader is actually used in the gate."""
        evals = _passing_evals(
            n=5, score=3.8
        )  # score 3.8 barely passes 3.5 but fails 4.0
        cfg = _make_rec_config(score_margin_floor=0.1, pass_rate_margin_floor=0.01)

        h_low = _eval_headroom(evals, rubric_threshold=3.5, rec_config=cfg)
        h_high = _eval_headroom(evals, rubric_threshold=4.0, rec_config=cfg)

        # With threshold 3.5: margin=3.8-3.5=0.3 >= floor(0.1) → passes p1
        # With threshold 4.0: margin=3.8-4.0=-0.2 < floor(0.1) → fails p1
        assert h_low.passed is True
        assert h_high.passed is False


# ──────────────────────────────────────────────────────────────────
# Recommendation dataclass shape


class TestRecommendationDataclassShape:
    """EvalHeadroom has two distinct margin fields (not one combined)."""

    def test_eval_headroom_has_two_margin_fields(self):
        """EvalHeadroom must have both weighted_score_margin and pass_rate_margin."""
        h = EvalHeadroom(
            weighted_score_margin=1.5,
            pass_rate_margin=0.2,
            hard_fails=0,
            sample_n=10,
            rubric_threshold=4.0,
            passed=True,
        )
        assert hasattr(h, "weighted_score_margin")
        assert hasattr(h, "pass_rate_margin")
        assert h.weighted_score_margin == pytest.approx(1.5)
        assert h.pass_rate_margin == pytest.approx(0.2)

    def test_recommendation_has_two_separate_delta_fields(self):
        """Recommendation has both projected_usd_delta and projected_points_delta."""
        h = EvalHeadroom(0.0, 0.0, 0, 0, 4.0, False)
        rec = Recommendation(
            agent="a",
            kind="governance",
            current_model=None,
            candidate_model=None,
            projected_usd_delta=None,
            projected_points_delta=None,
            rationale="test",
            safety=h,
        )
        assert hasattr(rec, "projected_usd_delta")
        assert hasattr(rec, "projected_points_delta")
        assert rec.projected_usd_delta is None
        assert rec.projected_points_delta is None

    def test_recommendation_source_field(self):
        """source defaults to None for a non-model rec (governance has no candidate
        and no 'family', so it must not carry a model-selection source label)."""
        h = EvalHeadroom(0.0, 0.0, 0, 0, 4.0, False)
        rec = Recommendation(
            agent="a",
            kind="governance",
            current_model=None,
            candidate_model=None,
            projected_usd_delta=None,
            projected_points_delta=None,
            rationale="test",
            safety=h,
        )
        assert rec.source is None

    def test_recommendation_source_field_model_rec(self):
        """A MODEL rec carries an explicit source ('default_same_family' |
        'operator_configured'); strip-RED against the None default leaking onto
        a candidate-bearing rec."""
        h = EvalHeadroom(0.0, 0.0, 0, 0, 4.0, False)
        rec = Recommendation(
            agent="a",
            kind="savings_cost",
            current_model="claude-sonnet-4-5",
            candidate_model="claude-haiku-4-5",
            projected_usd_delta=-12.0,
            projected_points_delta=0.0,
            rationale="test",
            safety=h,
            source="default_same_family",
        )
        assert rec.source == "default_same_family"

    def test_recommend_submodule_attribute_is_not_shadowed(self):
        """The package must NOT re-export the bare `recommend` FUNCTION (it would
        shadow the `recommend` SUBMODULE on the package object, breaking the
        standard `import atomic_agents.advisor.recommend as r; r.recommend_fleet()`
        idiom). Locks the de-shadowing fix: the package-level `recommend` attribute
        resolves to the SUBMODULE, and `recommend` is absent from the package
        namespace + __all__.
        """
        import importlib
        import types

        import atomic_agents.advisor as pkg

        # Package-level attribute access lands on the SUBMODULE (not a function).
        assert isinstance(pkg.recommend, types.ModuleType)
        assert pkg.recommend.__name__ == "atomic_agents.advisor.recommend"
        # The bare function is NOT in the package namespace / __all__.
        assert "recommend" not in pkg.__all__
        assert not callable(pkg.recommend)  # it's a module, not the function

        # The module-import idiom that the shadow used to break now works.
        mod = importlib.import_module("atomic_agents.advisor.recommend")
        assert callable(mod.recommend_fleet)
        assert callable(mod.recommend)  # the function still lives on the submodule
        assert hasattr(mod, "RECOMMENDATION_KINDS")


# ──────────────────────────────────────────────────────────────────
# recommend_fleet — on-disk loader path (MUST 7 real-path no-LLM guard)


def _write_model_md(agents_root: Path, agent: str, model: str) -> None:
    agent_dir = agents_root / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "model.md").write_text(f"# Model\nmodel: {model}\n")


def _write_run_jsonl(agents_root: Path, agent: str, records: list[dict]) -> None:
    """Write run records to log/YYYY-MM/YYYY-MM-DD.jsonl (FilesystemLogBackend layout)."""
    for rec in records:
        ts_d = datetime.fromisoformat(rec["ts"]).date()
        month_dir = agents_root / agent / "log" / ts_d.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        with (month_dir / f"{ts_d.isoformat()}.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")


def _write_eval_jsonl(
    agents_root: Path, agent: str, file_date: date, records: list[dict]
) -> None:
    """Write eval records to evals/runs/<file_date>.jsonl (stem is the window key)."""
    evals_dir = agents_root / agent / "evals" / "runs"
    evals_dir.mkdir(parents=True, exist_ok=True)
    with (evals_dir / f"{file_date.isoformat()}.jsonl").open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


class TestRecommendFleet:
    """End-to-end recommend_fleet() over a real on-disk fleet.

    This is the load-bearing real-path test (#616 prompt): it walks
    compute_fleet_health, _load_runs_with_degraded, _load_eval_records, the
    FilesystemAgentRegistryBackend, and the tuning-report reader — under the
    module-scoped autouse no_llm_in_advisor guard, so it proves the entire
    loader graph constructs ZERO LLMBackend (MUST 7).
    """

    def _seed_agent_runs_and_evals(
        self, root: Path, agent: str, model: str, today: date
    ) -> None:
        """Seed one agent: model.md + 12 primary runs + 12 strong evals."""
        _write_model_md(root, agent, model)
        runs = []
        for i in range(12):
            ts = datetime.combine(
                today - timedelta(days=i + 1), datetime.min.time()
            ).replace(tzinfo=timezone.utc)
            runs.append(
                {
                    "ts": ts.isoformat(),
                    "trigger": "cron",
                    "model": model,
                    "input_tokens": 2000,
                    "output_tokens": 1000,
                    "cost_usd": 0.50,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 2000,
                    "latency_ms": 100,
                    "status": "completed",
                    "summary": "ok",
                }
            )
        _write_run_jsonl(root, agent, runs)
        # 12 strong-but-not-perfect evals (mean 4.6 → margin 0.6 >= default 0.5).
        evals = [
            {"ts": today.isoformat(), "verdict": "pass", "weighted_score": 4.6}
            for _ in range(12)
        ]
        _write_eval_jsonl(root, agent, today, evals)

    def _seed_fleet(self, root: Path, today: date) -> None:
        # opus-agent: opus → sonnet candidate (both above the cheap cutoff, so the
        # cheaper_model_share axis is unchanged → ~0.0 point delta — the documented
        # spec/54 §7 known limitation). NO governance.md (governance rec) + a
        # hard-fail tuning report (quality_report rec).
        self._seed_agent_runs_and_evals(root, "opus-agent", "claude-opus-4-8", today)
        tuning_dir = root / "opus-agent" / "evals" / "tuning_reports"
        tuning_dir.mkdir(parents=True, exist_ok=True)
        (tuning_dir / "2026-06-01.md").write_text(
            "---\nhas_hard_fails: true\n---\nThe latest tuning run had a hard fail.\n"
        )
        # sonnet-agent: sonnet → haiku candidate. haiku output rate 4.0 < cutoff 5.0
        # is CHEAP while sonnet 15.0 is not, so the swap moves cheaper_model_share
        # from 0% to 100% → a NON-ZERO projected_points_delta. This makes the sort
        # test non-vacuous and walks the cheap-cutoff-crossing reprice path. A VALID
        # governance.md so no governance rec competes for the ranking.
        self._seed_agent_runs_and_evals(
            root, "sonnet-agent", "claude-sonnet-4-6-20260101", today
        )
        (root / "sonnet-agent" / "governance.md").write_text(
            "```yaml\n"
            "governance:\n"
            "  owner: team-a\n"
            "  permission_tier: read-only\n"
            "  customer_data: no\n"
            "  writes_sor: no\n"
            "  lifecycle_status: active\n"
            "```\n"
        )

    def test_recommend_fleet_emits_savings_and_governance_under_defaults(
        self, tmp_path
    ):
        """recommend_fleet over a real fleet emits savings + governance recs.

        Exercises the real loader path with PRODUCTION-default config (no rec_config
        override), proving (a) the default score_margin_floor lets a savings rec
        fire, (b) ref.id (not ref.agent_id) is used so the governance map populates,
        (c) the reprice path runs over candidate PRICING, all under the no-LLM guard.
        """
        from atomic_agents.advisor.recommend import recommend_fleet

        today = date(2026, 6, 20)
        self._seed_fleet(tmp_path, today)

        recs = recommend_fleet(tmp_path, today=today)

        kinds = {r.kind for r in recs}
        assert "savings_cost" in kinds, (
            f"savings_cost rec must fire under default config; got {recs!r}"
        )
        assert "governance" in kinds, (
            f"governance rec must fire for absent governance.md (ref.id wiring); got {recs!r}"
        )
        # quality_report MUST fire for the on-disk hard-fail tuning report at the
        # REAL evals/tuning_reports/ path (the path tuning.py actually writes). A
        # bare tuning_reports/ reader would surface nothing here — this is the
        # cross-module guard against that exact directory drift (Principle #13).
        assert "quality_report" in kinds, (
            f"quality_report rec must fire for the evals/tuning_reports/ hard-fail "
            f"report (real tuning.py write path); got {recs!r}"
        )
        opus_quality = [
            r for r in recs if r.kind == "quality_report" and r.agent == "opus-agent"
        ]
        assert len(opus_quality) == 1
        assert "hard-fail" in opus_quality[0].rationale.lower()

        # opus-agent: a savings rec with the opus→sonnet candidate.
        opus_savings = [
            r for r in recs if r.kind == "savings_cost" and r.agent == "opus-agent"
        ]
        assert len(opus_savings) == 1
        assert opus_savings[0].current_model == "claude-opus-4-8"
        assert opus_savings[0].candidate_model == "claude-sonnet-4-6-20260101"
        assert opus_savings[0].projected_usd_delta is not None
        assert opus_savings[0].projected_usd_delta < 0  # a real saving
        assert opus_savings[0].safety.passed is True

        # Governance rec fires ONLY for opus-agent (no governance.md); sonnet-agent
        # has a VALID governance.md so it gets no governance rec.
        gov = [r for r in recs if r.kind == "governance"]
        assert len(gov) == 1
        assert gov[0].agent == "opus-agent"
        assert "absent" in gov[0].rationale.lower()

    def test_recommend_fleet_sorts_by_abs_points_delta(self, tmp_path):
        """recommend_fleet results are sorted by |projected_points_delta| desc.

        NON-VACUOUS: the sonnet-agent's sonnet→haiku swap crosses the cheap-model
        cutoff (haiku 4.0 < 5.0 < sonnet 15.0), moving cheaper_model_share and
        producing a NON-ZERO point delta, while the opus→sonnet swap stays ~0.0.
        So the key list contains both a non-zero and zero values and the sort is
        actually exercised (a fixture where every key is 0.0 would pass vacuously).
        """
        from atomic_agents.advisor.recommend import recommend_fleet

        today = date(2026, 6, 20)
        self._seed_fleet(tmp_path, today)
        recs = recommend_fleet(tmp_path, today=today)

        keys = [
            abs(r.projected_points_delta)
            if r.projected_points_delta is not None
            else 0.0
            for r in recs
        ]
        assert keys == sorted(keys, reverse=True), (
            f"recs must be sorted by |projected_points_delta| desc; got {keys!r}"
        )
        # Guard against a vacuous (all-zero) sort: the sonnet→haiku swap MUST
        # produce at least one non-zero point delta, so the first key is > 0.
        assert keys[0] > 0.0, (
            "expected a non-zero point-impact delta from the sonnet→haiku swap "
            f"(cheap-cutoff crossing); got all-zero keys {keys!r} — sort is vacuous"
        )
        # And the non-zero-delta rec (sonnet-agent savings) must sort FIRST.
        sonnet_savings = [
            r for r in recs if r.kind == "savings_cost" and r.agent == "sonnet-agent"
        ]
        assert sonnet_savings, "sonnet-agent savings rec must be present"
        assert recs[0] is sonnet_savings[0], (
            "the highest-|point-delta| rec (sonnet-agent savings) must rank first"
        )

    def test_recommend_fleet_empty_root_no_crash(self, tmp_path):
        """recommend_fleet over an empty fleet returns [] (fail-soft, no LLM)."""
        from atomic_agents.advisor.recommend import recommend_fleet

        assert recommend_fleet(tmp_path, today=date(2026, 6, 20)) == []

    def test_recommend_fleet_reuses_precomputed_fleet_health(self, tmp_path):
        """Passing a pre-computed fleet_health skips the internal re-compute.

        Locks the render-path seam (Principle #6): render_console already runs
        compute_fleet_health for the band, so recommend_fleet must accept it and
        NOT re-load. We patch compute_fleet_health to raise; if recommend_fleet
        still produces the same recs as the from-scratch call, it cannot have
        called it — the pre-computed object was used.
        """
        from unittest import mock

        from atomic_agents.advisor import recommend as recommend_mod
        from atomic_agents.advisor.recommend import recommend_fleet
        from atomic_agents.advisor.score import compute_fleet_health

        today = date(2026, 6, 20)
        self._seed_fleet(tmp_path, today)

        # Baseline: the from-scratch loader path.
        baseline = recommend_fleet(tmp_path, today=today)
        assert baseline, "fixture must yield recs"

        fh = compute_fleet_health(tmp_path, today=today)
        with mock.patch.object(
            recommend_mod,
            "compute_fleet_health",
            side_effect=AssertionError("must not re-compute fleet_health"),
        ):
            reused = recommend_fleet(tmp_path, today=today, fleet_health=fh)

        # Same set of (agent, kind) pairs proves the reused path produced the
        # same recommendations without re-running compute_fleet_health.
        assert {(r.agent, r.kind) for r in reused} == {
            (r.agent, r.kind) for r in baseline
        }


# ──────────────────────────────────────────────────────────────────
# Render panel — the operator-visible surface (spec/54 §11)


class TestRenderRecommendations:
    """_render_recommendations() — the visible PR3 surface (spec/54 §11).

    The pure core is exhaustively tested elsewhere; this pins the HTML the operator
    actually sees so a future render refactor cannot silently break the panel.
    """

    def _rec(self, **kw):
        base = dict(
            agent="opus-agent",
            kind="savings_cost",
            current_model="claude-opus-4-8",
            candidate_model="claude-sonnet-4-6-20260101",
            projected_usd_delta=-42.50,
            projected_points_delta=3.2,
            rationale="Switch saves money",
            safety=EvalHeadroom(
                weighted_score_margin=0.6,
                pass_rate_margin=0.2,
                hard_fails=0,
                sample_n=12,
                rubric_threshold=4.0,
                passed=True,
            ),
        )
        base.update(kw)
        return Recommendation(**base)

    def test_none_returns_empty_string(self):
        from atomic_agents.dashboard.render import _render_recommendations

        assert _render_recommendations(None) == ""

    def test_empty_list_renders_friendly_note(self):
        from atomic_agents.dashboard.render import _render_recommendations

        html_out = _render_recommendations([])
        assert "Recommendations" in html_out
        assert "No recommendations" in html_out

    def test_savings_rec_renders_pill_arrow_and_badges(self):
        from atomic_agents.dashboard.render import _render_recommendations

        html_out = _render_recommendations([self._rec()])
        # kind pill class is the live styling path (not a generic ok/warn pill)
        assert "rec-kind-savings_cost" in html_out
        # model arrow
        assert "claude-opus-4-8" in html_out
        assert "claude-sonnet-4-6-20260101" in html_out
        assert "rec-arrow" in html_out
        # delta badges with sign + units
        assert "rec-delta-savings" in html_out
        assert "$42.50/mo saved" in html_out
        assert "rec-delta-points" in html_out
        assert "+3.2 pts" in html_out

    def test_governance_rec_renders_kind_pill(self):
        from atomic_agents.dashboard.render import _render_recommendations

        gov = self._rec(
            kind="governance",
            current_model=None,
            candidate_model=None,
            projected_usd_delta=None,
            projected_points_delta=None,
            rationale="governance.md is absent",
        )
        html_out = _render_recommendations([gov])
        assert "rec-kind-governance" in html_out
        # No model arrow / no delta badges for a governance rec.
        assert "rec-arrow" not in html_out
        assert "rec-delta-savings" not in html_out

    def test_rationale_and_models_are_html_escaped(self):
        from atomic_agents.dashboard.render import _render_recommendations

        evil = self._rec(
            rationale="<script>alert('x')</script>",
            current_model="<b>opus</b>",
            candidate_model="sonnet&co",
        )
        html_out = _render_recommendations([evil])
        # The raw script tag must not appear unescaped.
        assert "<script>alert" not in html_out
        assert "&lt;script&gt;" in html_out
        assert "&lt;b&gt;opus&lt;/b&gt;" in html_out
        assert "sonnet&amp;co" in html_out

    def test_render_console_template_includes_panel(self, tmp_path):
        """A populated console_data.recommendations lands in the rendered page."""
        from atomic_agents.dashboard.render import _render_console_template
        from atomic_agents.dashboard.attention import ConsoleData

        cd = ConsoleData(
            attention_queue=[],
            cost_trends=[],
            quality_signals=[],
            reliability_metrics=[],
            rendered_alert_keys=frozenset(),
            agent_count=0,
            degraded=False,
        )
        cd.recommendations = [self._rec()]
        page = _render_console_template(cd, has_goals=False)
        assert "Recommendations" in page
        assert "rec-kind-savings_cost" in page
        assert "$42.50/mo saved" in page

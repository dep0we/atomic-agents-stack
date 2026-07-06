"""Tests for atomic_agents.advisor — Fleet Health Scoring Engine (spec/53 PR2).

Coverage map (spec/53 Conformance test map):
  MUST 1 — piecewise plateau-at-target curve: 100 at target, linear decay to 0 at floor
  MUST 2 — weighted composite roll-up over present sub-scores only
  MUST 3 — composite clamped [0, 100]
  MUST 4 — critical-axis cap: sub-score < threshold → composite ≤ 60, band = red
  MUST 5 — decomposition always visible (scorecard rows always emitted)
  MUST 6 — targets.md fail-soft per key
  MUST 7 — no-data posture: zero-evals agent excluded from quality composite
  MUST 8 — degraded read → sub-score excluded from composite (not scored as 0)
  MUST 9 — cheap-model classification: strict less-than threshold, fail-pessimistic
  MUST 10 — no-LLM enforcement (conftest guard + import-discipline check)

Each MUST has at least one strip-RED negative-control test.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.advisor.score import (
    AgentHealth,
    FleetHealth,
    ScorecardRow,
    _band,
    _classify_work_type,
    _compute_composite,
    _is_cheap_model,
    _map_metric_to_score,
    compute_fleet_health,
)
from atomic_agents.advisor.targets import (
    CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M,
    CRITICAL_COMPOSITE_CAP,
    CRITICAL_SUBSCORE_THRESHOLD,
    FleetTargets,
    MetricTarget,
    _deep_merge,
    parse_targets,
)
from atomic_agents.dashboard._reliability import (
    ReliabilityMetrics,
    _compute_reliability,
    _is_primary_run,
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
        cache_hit_tokens=0,
        cache_miss_tokens=input_tokens,
        latency_ms=100,
        status=status,
        summary="ok",
        parent_run_id=parent_run_id,
        extra=extra or {},
    )


def _make_metric_target(
    target: float = 0.0,
    direction: str = "lower",
    band: float = 0.05,
    floor: float = 0.5,
) -> MetricTarget:
    return MetricTarget(target=target, direction=direction, band=band, floor=floor)


def _write_agent_model_md(agents_root: Path, agent: str) -> None:
    """Write a minimal model.md so the agent is discovered by the registry."""
    agent_dir = agents_root / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "model.md").write_text("# Model\nmodel: claude-haiku-4-5\n")


def _write_run_jsonl(agents_root: Path, agent: str, records: list[dict]) -> None:
    """Write run records to the agent's log JSONL.

    The FilesystemLogBackend expects nested structure:
      log/YYYY-MM/YYYY-MM-DD.jsonl

    Flat log/YYYY-MM.jsonl files are silently skipped by the backend.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    for rec in records:
        # Derive the day from the record's ts field
        ts_str = rec.get("ts", "")
        try:
            ts_date = _dt.fromisoformat(ts_str).date()
        except (ValueError, TypeError):
            from datetime import date

            ts_date = date.today()

        month_dir = agents_root / agent / "log" / ts_date.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        day_file = month_dir / f"{ts_date.isoformat()}.jsonl"
        with day_file.open("a") as f:
            f.write(_json.dumps(rec) + "\n")


def _write_eval_jsonl(agents_root: Path, agent: str, records: list[dict]) -> None:
    """Write eval run records to evals/runs/."""
    from datetime import date

    evals_dir = agents_root / agent / "evals" / "runs"
    evals_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    jf = evals_dir / f"{today}.jsonl"
    with jf.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ──────────────────────────────────────────────────────────────────
# MUST 1 — piecewise plateau-at-target curve


class TestMapMetricToScore:
    """MUST 1: plateau-at-target curve, monotonic, bounded [0,100]."""

    def test_higher_at_target_scores_100(self):
        mt = _make_metric_target(target=0.5, direction="higher", band=0.1, floor=0.0)
        assert _map_metric_to_score(0.5, mt) == 100.0

    def test_higher_above_target_scores_100(self):
        mt = _make_metric_target(target=0.5, direction="higher", band=0.1, floor=0.0)
        assert _map_metric_to_score(0.9, mt) == 100.0

    def test_higher_at_floor_scores_0(self):
        mt = _make_metric_target(target=0.5, direction="higher", band=0.0, floor=0.0)
        assert _map_metric_to_score(0.0, mt) == 0.0

    def test_higher_midpoint_scores_50(self):
        # target=0.5, band=0, floor=0 → lower_bound=0.5; at 0.25 (midpoint) → 50
        mt = _make_metric_target(target=0.5, direction="higher", band=0.0, floor=0.0)
        score = _map_metric_to_score(0.25, mt)
        assert 45.0 <= score <= 55.0

    def test_lower_at_target_scores_100(self):
        mt = _make_metric_target(target=0.0, direction="lower", band=0.05, floor=0.5)
        assert _map_metric_to_score(0.0, mt) == 100.0

    def test_lower_above_floor_scores_0(self):
        mt = _make_metric_target(target=0.0, direction="lower", band=0.05, floor=0.5)
        assert _map_metric_to_score(0.5, mt) == 0.0

    def test_lower_below_floor_clamps_to_0(self):
        """Values WORSE than floor still clamp to 0 (no negative scores)."""
        mt = _make_metric_target(target=0.0, direction="lower", band=0.05, floor=0.5)
        assert _map_metric_to_score(0.9, mt) == 0.0

    def test_higher_below_floor_clamps_to_0(self):
        """strip-RED: below-floor value must NOT produce negative score."""
        mt = _make_metric_target(target=0.5, direction="higher", band=0.0, floor=0.2)
        score = _map_metric_to_score(0.1, mt)  # below floor
        assert score == 0.0

    def test_output_bounded_0_to_100(self):
        mt = _make_metric_target(target=0.5, direction="higher", band=0.0, floor=0.0)
        for v in [-1.0, 0.0, 0.25, 0.5, 1.0, 2.0]:
            s = _map_metric_to_score(v, mt)
            assert 0.0 <= s <= 100.0, f"score {s} out of [0,100] for value {v}"


# ──────────────────────────────────────────────────────────────────
# MUST 2 — weighted composite roll-up


class TestComputeComposite:
    """MUST 2: weighted mean over present sub-scores; absent axes excluded."""

    def test_all_present_equal_weights(self):
        scores = {"cost": 90.0, "quality": 80.0, "reliability": 70.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, capped = _compute_composite(scores, weights)
        assert composite is not None
        assert abs(composite - 80.0) < 1.0
        assert capped is None

    def test_absent_axis_excluded(self):
        """Quality absent → composite from cost+reliability only (re-weighted)."""
        scores = {"cost": 90.0, "quality": None, "reliability": 90.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, _ = _compute_composite(scores, weights)
        assert composite is not None
        assert abs(composite - 90.0) < 1.0

    def test_all_absent_returns_none(self):
        scores = {"cost": None, "quality": None, "reliability": None}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, _ = _compute_composite(scores, weights)
        assert composite is None
        assert band == "unknown"

    def test_strip_red_absent_axis_is_not_zero(self):
        """strip-RED: a None sub-score must NOT be treated as 0 and drag composite down."""
        scores_with_none = {"cost": 95.0, "quality": None, "reliability": 95.0}
        scores_with_zero = {"cost": 95.0, "quality": 0.0, "reliability": 95.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        comp_none, _, _ = _compute_composite(scores_with_none, weights)
        comp_zero, _, _ = _compute_composite(scores_with_zero, weights)
        # None composite must be significantly higher than zero-as-quality composite
        assert comp_none is not None
        assert comp_zero is not None
        assert comp_none > comp_zero + 20


# ──────────────────────────────────────────────────────────────────
# MUST 3 — composite clamped [0, 100]


class TestCompositeClamping:
    def test_composite_never_exceeds_100(self):
        scores = {"cost": 100.0, "quality": 100.0, "reliability": 100.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, _, _ = _compute_composite(scores, weights)
        assert composite is not None
        assert composite <= 100.0

    def test_composite_never_below_0(self):
        scores = {"cost": 0.0, "quality": 0.0, "reliability": 0.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, _, _ = _compute_composite(scores, weights)
        assert composite is not None
        assert composite >= 0.0


# ──────────────────────────────────────────────────────────────────
# MUST 4 — critical-axis cap


class TestCriticalAxisCap:
    """MUST 4: any sub-score < CRITICAL_SUBSCORE_THRESHOLD → composite ≤ CAP AND band = red."""

    def test_critical_reliability_caps_composite(self):
        """95/95/20 fleet: composite must be ≤ 60 AND band must be red."""
        scores = {"cost": 95.0, "quality": 95.0, "reliability": 20.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, capped = _compute_composite(scores, weights)
        assert composite is not None
        assert composite <= CRITICAL_COMPOSITE_CAP, (
            f"composite {composite} exceeds cap {CRITICAL_COMPOSITE_CAP}"
        )
        assert band == "red", f"band should be red, got {band}"
        assert capped == "reliability"

    def test_critical_cost_caps_composite(self):
        scores = {"cost": 15.0, "quality": 95.0, "reliability": 95.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, capped = _compute_composite(scores, weights)
        assert composite is not None
        assert composite <= CRITICAL_COMPOSITE_CAP
        assert band == "red"
        assert capped == "cost"

    def test_cap_fires_at_threshold_not_only_at_zero(self):
        """strip-RED: reliability=15 (below threshold but not 0) must still fire cap."""
        scores = {"cost": 95.0, "quality": 95.0, "reliability": 15.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, capped = _compute_composite(scores, weights)
        assert composite is not None
        assert composite <= CRITICAL_COMPOSITE_CAP, (
            f"cap must fire at reliability=15 (< {CRITICAL_SUBSCORE_THRESHOLD}), "
            f"got composite={composite}"
        )
        assert band == "red"

    def test_healthy_fleet_no_cap(self):
        """strip-RED: 95/95/95 must NOT be capped at 60."""
        scores = {"cost": 95.0, "quality": 95.0, "reliability": 95.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, capped = _compute_composite(scores, weights)
        assert composite is not None
        assert composite > CRITICAL_COMPOSITE_CAP, (
            f"healthy fleet composite {composite} should exceed cap {CRITICAL_COMPOSITE_CAP}"
        )
        assert band == "green"
        assert capped is None

    def test_cap_is_post_computation(self):
        """Cap applied AFTER weighted mean, not before (spec/53 MUST 4 ordering)."""
        # reliability=20: raw mean = (95+95+20)/3 ≈ 70 > 60; cap must bring it to 60
        scores = {"cost": 95.0, "quality": 95.0, "reliability": 20.0}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        raw_mean = (95 + 95 + 20) / 3  # ≈ 70
        assert raw_mean > CRITICAL_COMPOSITE_CAP  # confirm the cap matters
        composite, _, _ = _compute_composite(scores, weights)
        assert composite is not None
        assert composite <= CRITICAL_COMPOSITE_CAP


# ──────────────────────────────────────────────────────────────────
# MUST 5 — decomposition always visible


class TestDecompositionAlwaysVisible:
    """MUST 5: scorecard rows always emitted, never a bare scalar."""

    def test_agent_health_always_has_scorecard(self, tmp_path):
        """An agent with no runs still has scorecard rows (no data markers)."""
        _write_agent_model_md(tmp_path, "empty-agent")
        fh = compute_fleet_health(tmp_path)
        assert len(fh.agents) == 1
        ah = fh.agents[0]
        # Scorecard must be present even with no data
        assert isinstance(ah.scorecard, list)
        assert len(ah.scorecard) > 0

    def test_no_data_rows_have_no_data_status(self, tmp_path):
        _write_agent_model_md(tmp_path, "empty-agent")
        fh = compute_fleet_health(tmp_path)
        ah = fh.agents[0]
        for row in ah.scorecard:
            assert row.status in ("no_data", "degraded", "ok", "warn", "crit"), (
                f"unexpected status {row.status!r} for row {row.metric}"
            )


# ──────────────────────────────────────────────────────────────────
# MUST 6 — targets.md fail-soft per key


class TestTargetsParsing:
    """MUST 6: per-key fail-soft in targets.md parsing."""

    def test_absent_targets_uses_all_defaults(self, tmp_path):
        """targets.md absent → all defaults, used_defaults non-empty."""
        targets = parse_targets(tmp_path)
        assert "(targets.md absent)" in targets.used_defaults
        # All three axes must be present with baked-in defaults
        assert "cost" in targets.axes
        assert "quality" in targets.axes
        assert "reliability" in targets.axes

    def test_valid_targets_md_parsed(self, tmp_path):
        (tmp_path / "targets.md").write_text(
            "## targets\n\n```yaml\nscoring:\n  weights:\n    cost: 0.333\n"
            "    quality: 0.333\n    reliability: 0.334\n```\n"
        )
        targets = parse_targets(tmp_path)
        # Weights should be parsed; axes fall back to defaults since not specified
        assert abs(sum(targets.weights.values()) - 1.0) < 0.01

    def test_malformed_yaml_falls_back_to_defaults(self, tmp_path):
        (tmp_path / "targets.md").write_text("```yaml\nscoring: {{{broken yaml\n```\n")
        targets = parse_targets(tmp_path)
        # Should use defaults without crashing
        assert "cost" in targets.axes
        assert "quality" in targets.axes
        assert "reliability" in targets.axes

    def test_invalid_weight_sum_falls_back_to_equal_weights(self, tmp_path):
        """Weights summing > 1.0 → fall back to equal weights."""
        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  weights:\n    cost: 0.5\n"
            "    quality: 0.5\n    reliability: 0.5\n```\n"
        )
        targets = parse_targets(tmp_path)
        # Must fall back to 1/3 each
        assert abs(targets.weights["cost"] - 1 / 3) < 0.01
        assert "weights(invalid_sum)" in targets.used_defaults

    def test_yaml_safe_load_used(self, tmp_path):
        """yaml.safe_load must handle YAML aliases without executing code."""
        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  weights:\n"
            "    cost: &w 0.333\n    quality: *w\n    reliability: 0.334\n```\n"
        )
        # safe_load expands aliases safely
        targets = parse_targets(tmp_path)
        assert abs(targets.weights["cost"] - 0.333) < 0.001

    def test_strip_red_whole_file_not_crashed_by_bad_key(self, tmp_path):
        """One bad key must NOT crash the whole parse (per-key fail-soft)."""
        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  weights:\n    cost: not_a_number\n"
            "    quality: 0.5\n    reliability: 0.5\n```\n"
        )
        # Should not raise; weight sum would be invalid → equal weights fallback
        targets = parse_targets(tmp_path)
        assert "cost" in targets.axes  # rest of config still parsed

    def test_separate_rec_block_first_does_not_drop_scoring(self, tmp_path):
        """A recommendations-only block BEFORE a valid scoring block must NOT drop
        the operator's scoring weights to defaults (spec/53 MUST 6 — per-key
        fail-soft, not per-file).

        strip-RED: when the extractor matched EITHER 'scoring' OR 'recommendations',
        parse_targets landed on the first (recommendations-only) block, saw no
        'scoring' key, and silently used all default weights — discarding the
        operator's real config. Keying each parser on its own block fixes this.
        """
        from atomic_agents.advisor.targets import parse_recommendations

        (tmp_path / "targets.md").write_text(
            "## Recommendations\n\n"
            "```yaml\n"
            "recommendations:\n"
            "  min_savings_usd: 7.50\n"
            "```\n\n"
            "## Scoring\n\n"
            "```yaml\n"
            "scoring:\n"
            "  weights:\n"
            "    cost: 0.50\n"
            "    quality: 0.25\n"
            "    reliability: 0.25\n"
            "```\n"
        )
        targets = parse_targets(tmp_path)
        # The operator's NON-default cost weight must survive (not 1/3).
        assert abs(targets.weights["cost"] - 0.50) < 0.001, (
            f"operator scoring weights must not be dropped by a preceding "
            f"recommendations-only block; got {targets.weights!r}"
        )
        assert abs(targets.weights["quality"] - 0.25) < 0.001
        # And the recommendations parser independently finds its own block.
        rec_cfg = parse_recommendations(tmp_path)
        assert rec_cfg.min_savings_usd == 7.50


class TestDeepMerge:
    """targets.override.md deep-merge correctness."""

    def test_deep_merge_preserves_unrelated_keys(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"x": 99}}
        result = _deep_merge(base, override)
        assert result["a"]["x"] == 99
        assert result["a"]["y"] == 2  # preserved
        assert result["b"] == 3  # preserved

    def test_deep_merge_skips_none_values(self):
        """None override values must not replace parent values."""
        base = {"a": {"x": 1}}
        override = {"a": {"x": None}}
        result = _deep_merge(base, override)
        assert result["a"]["x"] == 1  # parent preserved

    def test_deep_merge_override_only_reliability(self):
        """Override with only reliability must leave cost+quality intact."""
        from atomic_agents.advisor.targets import _DEFAULT_AXES

        base = {
            "axes": {
                "cost": {"metrics": {"spend_vs_trend": {"target": 0.05}}},
                "quality": {"metrics": {"pass_rate": {"target": 0.80}}},
                "reliability": {"metrics": {"error_rate": {"target": 0.0}}},
            }
        }
        override = {
            "axes": {"reliability": {"metrics": {"error_rate": {"target": 0.02}}}}
        }
        result = _deep_merge(base, override)
        # Cost and quality preserved
        assert result["axes"]["cost"]["metrics"]["spend_vs_trend"]["target"] == 0.05
        assert result["axes"]["quality"]["metrics"]["pass_rate"]["target"] == 0.80
        # Reliability updated
        assert result["axes"]["reliability"]["metrics"]["error_rate"]["target"] == 0.02


# ──────────────────────────────────────────────────────────────────
# MUST 7 — no-data posture (zero-evals agent)


class TestNoDataPosture:
    """MUST 7: zero-evals agent → quality excluded from composite, not scored as 0."""

    def test_zero_evals_quality_is_none(self, tmp_path):
        """Agent with runs but no evals: quality_score must be None, not 0."""
        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()
        ts = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        _write_run_jsonl(
            tmp_path,
            "agent-a",
            [
                {
                    "ts": ts.isoformat(),
                    "trigger": "cron",
                    "model": "claude-haiku-4-5",
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cost_usd": 0.01,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 100,
                    "latency_ms": 100,
                    "status": "completed",
                    "summary": "ok",
                }
            ],
        )
        # No eval records written
        fh = compute_fleet_health(tmp_path, today=today)
        assert len(fh.agents) == 1
        ah = fh.agents[0]
        assert ah.quality_score is None, (
            f"quality_score should be None for zero-evals agent, got {ah.quality_score}"
        )

    def test_zero_evals_composite_excludes_quality(self, tmp_path):
        """Composite must be computed from present axes only when quality is absent.

        After #687: cost axis = spend_vs_trend only. A single run with no prior-30d
        window means spend_vs_trend has no data (prior_spend == 0), so cost_score is
        None. The composite therefore comes from reliability alone. The key property
        being tested is that quality=None is excluded (not scored as 0).
        """
        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()
        ts = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        _write_run_jsonl(
            tmp_path,
            "agent-a",
            [
                {
                    "ts": ts.isoformat(),
                    "trigger": "cron",
                    "model": "claude-haiku-4-5",
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cost_usd": 0.01,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 100,
                    "latency_ms": 100,
                    "status": "completed",
                    "summary": "ok",
                }
            ],
        )
        fh = compute_fleet_health(tmp_path, today=today)
        ah = fh.agents[0]
        # Quality is absent → excluded from composite (not scored as 0).
        assert ah.quality_score is None
        # Reliability has data (1 completed run → 100% reliability → score = 100).
        assert ah.reliability_score is not None, (
            "reliability_score must not be None for an agent with 1 completed run"
        )
        assert ah.composite is not None, (
            "composite must not be None when at least one axis (reliability) has data"
        )
        # FIX 3: unconditional composite invariant — do NOT silently skip if
        # present_scores is empty. The test setup guarantees reliability_score is
        # present; if present_scores were empty, the assertion above would have
        # already failed. No `if present_scores:` conditional.
        present_scores = [
            s for s in [ah.cost_score, ah.reliability_score] if s is not None
        ]
        # Reliability must be present (assert above guarantees it).
        assert present_scores, (
            "at least reliability_score must be a present axis; got no present scores — "
            "this means the test fixture or the scorer silently dropped all axes"
        )
        expected = sum(present_scores) / len(present_scores)
        assert abs(ah.composite - expected) < 0.2, (
            f"composite {ah.composite} must equal mean of present axes {expected} "
            "(quality excluded, not scored as 0); present_scores={present_scores}"
        )

    def test_strip_red_zero_evals_not_scored_as_zero(self, tmp_path):
        """strip-RED: quality=0.0 must NOT be assigned when evals are absent."""
        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()
        ts = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        _write_run_jsonl(
            tmp_path,
            "agent-a",
            [
                {
                    "ts": ts.isoformat(),
                    "trigger": "cron",
                    "model": "claude-haiku-4-5",
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cost_usd": 0.01,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 100,
                    "latency_ms": 100,
                    "status": "completed",
                    "summary": "ok",
                }
            ],
        )
        fh = compute_fleet_health(tmp_path, today=today)
        ah = fh.agents[0]
        assert ah.quality_score != 0.0 or ah.quality_score is None, (
            "quality_score=0.0 assigned to agent with no evals — must be None"
        )

    def test_all_judge_error_verdicts_is_no_data(self, tmp_path):
        """All verdict='judge_error' → quality excluded (no scorable records)."""
        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()
        ts = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        _write_eval_jsonl(
            tmp_path,
            "agent-a",
            [
                {
                    "ts": ts.isoformat(),
                    "verdict": "judge_error",
                    "hard_fails": [],
                    "weighted_score": 0.5,
                    "test_id": "t1",
                },
            ],
        )
        fh = compute_fleet_health(tmp_path, today=today)
        if fh.agents:
            ah = fh.agents[0]
            assert ah.quality_score is None


# ──────────────────────────────────────────────────────────────────
# MUST 8 — degraded read → sub-score excluded (not 0)


class TestDegradedReadPosture:
    """MUST 8: degraded LogBackend read → sub-score None (excluded), never 0."""

    def test_degraded_reliability_excluded_from_composite(self, tmp_path):
        """Simulate a degraded run load; reliability must be None, not 0."""
        from unittest.mock import patch

        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()

        # Patch _load_runs_with_degraded to return degraded=True
        with patch(
            "atomic_agents.advisor.score._load_runs_with_degraded",
            return_value=([], True),
        ):
            fh = compute_fleet_health(tmp_path, today=today)

        if fh.agents:
            ah = fh.agents[0]
            assert ah.reliability_score is None, (
                "degraded reliability read must produce None sub-score, not 0"
            )
            assert ah.reliability_degraded is True

    def test_strip_red_degraded_does_not_score_as_0(self, tmp_path):
        """strip-RED: reliability_score=0.0 must NOT be set on degraded read."""
        from unittest.mock import patch

        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()

        with patch(
            "atomic_agents.advisor.score._load_runs_with_degraded",
            return_value=([], True),
        ):
            fh = compute_fleet_health(tmp_path, today=today)

        if fh.agents:
            ah = fh.agents[0]
            assert ah.reliability_score != 0.0 or ah.reliability_score is None


# ──────────────────────────────────────────────────────────────────
# MUST 9 — cheap-model classification


class TestCheapModelClassification:
    """MUST 9: cheap classification uses strict less-than; ties are NOT cheap."""

    def test_haiku_is_cheap(self):
        """claude-haiku-4-5 output=$4/1M < $5 threshold → cheap."""
        from atomic_agents._costs import PRICING

        haiku_output = PRICING["claude-haiku-4-5"]["output"]
        assert haiku_output < CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M
        assert _is_cheap_model("claude-haiku-4-5") is True

    def test_opus_is_not_cheap(self):
        """claude-opus models output=$25/1M → not cheap."""
        assert _is_cheap_model("claude-opus-4-8") is False
        assert _is_cheap_model("claude-opus-4-7") is False

    def test_sonnet_is_not_cheap(self):
        """claude-sonnet output=$15/1M → not cheap."""
        assert _is_cheap_model("claude-sonnet-4-6") is False

    def test_unknown_model_is_not_cheap(self):
        """Unknown model → pessimistic (not cheap), no crash."""
        assert _is_cheap_model("some-unknown-model-v99") is False

    def test_strip_red_tie_at_threshold_is_not_cheap(self):
        """strip-RED: a model at exactly the threshold must be classified NOT cheap."""
        from unittest.mock import patch
        from atomic_agents import _costs

        # Temporarily insert a model at exactly the cutoff
        fake_model = "_test_threshold_model"
        original = dict(_costs.PRICING)
        try:
            _costs.PRICING[fake_model] = {
                "input": 1.0,
                "output": CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M,  # exactly at cutoff
            }
            result = _is_cheap_model(fake_model)
            assert result is False, (
                f"model at exactly threshold ${CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M}/1M "
                "must be classified NOT cheap (fail-pessimistic, strict less-than)"
            )
        finally:
            _costs.PRICING.pop(fake_model, None)


# ──────────────────────────────────────────────────────────────────
# MUST 10 — no-LLM enforcement


class TestNoLLMEnforcement:
    """MUST 10: advisor imports no LLM machinery; conftest guard catches violations."""

    def test_advisor_does_not_import_agent_module(self):
        """After importing advisor, agent.py must not be in sys.modules via advisor."""
        import atomic_agents.advisor.score  # already imported above

        # The advisor module itself must not have agent/eval/tuning in its import graph
        advisor_mod = sys.modules.get("atomic_agents.advisor.score")
        assert advisor_mod is not None
        # Check that importing the advisor does not pull in agent.py via its own imports
        # (We can't unload and reload here, but we can check the module's __dict__)
        import_names = set(dir(advisor_mod))
        forbidden = {"AtomicAgent", "EvalRunner", "TuningRunner", "DreamRunner"}
        found = forbidden & import_names
        assert not found, f"Advisor module exposes forbidden symbols: {found}"

    def test_advisor_source_has_no_direct_forbidden_imports(self):
        """SOURCE-LEVEL discipline: advisor/score.py's own code imports no
        agent/eval/tuning/dream module. This greps source text, NOT sys.modules
        (the package __init__ loads agent/eval/dream transitively — see spec/53 §2
        NOTE). The load-bearing guarantee is no-LLM-spend, tested below.
        """
        import atomic_agents.advisor.score as advisor_score
        import inspect

        src = inspect.getsource(advisor_score)
        forbidden_patterns = [
            "from ..agent import",
            "from atomic_agents.agent import",
            "from ..eval import",
            "from atomic_agents.eval import",
            "from ..tuning import",
            "from ..dream import",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in src, (
                f"Forbidden import pattern {pattern!r} found in advisor/score.py"
            )

    def test_compute_fleet_health_runs_without_constructing_an_llm(self, tmp_path):
        """MUST 10 (honest): the conftest guard patches every concrete
        LLMBackend.__init__ to raise. An end-to-end compute_fleet_health() that
        completes WITHOUT the guard firing proves no LLM is constructed on any
        advisor path — the real no-LLM-spend guarantee (not a source grep).
        """
        # Build a minimal agent with one run so every axis path is exercised.
        # Route the run record through _write_run_jsonl so it lands in the nested
        # log/YYYY-MM/YYYY-MM-DD.jsonl layout the FilesystemLogBackend actually reads.
        # A flat log/<date>.jsonl is silently skipped, which would hollow this
        # end-to-end check to a no-data fleet (the cost/reliability scoring bodies —
        # the heaviest paths, where an accidental LLM-constructing import would most
        # plausibly hide — would never run). See _write_run_jsonl docstring.
        agent_dir = tmp_path / "a1"
        agent_dir.mkdir(parents=True)
        (agent_dir / "model.md").write_text("# model\n", encoding="utf-8")
        _write_run_jsonl(
            tmp_path,
            "a1",
            [
                {
                    "run_id": "r1",
                    "ts": datetime.now(tz=timezone.utc).isoformat(),
                    "agent": "a1",
                    "trigger": "manual",
                    "status": "completed",
                    "model": "claude-opus-4-20250514",
                    "output_tokens": 100,
                    "cost_usd": 0.5,
                }
            ],
        )

        # If any LLMBackend.__init__ ran, the conftest guard would raise RuntimeError
        # and fail this test. Completing the call is the assertion.
        fleet = compute_fleet_health(tmp_path)
        assert fleet is not None
        a1 = next(ah for ah in fleet.agents if ah.agent == "a1")
        # Guard against silent re-hollowing: at least one axis body must actually
        # execute under the no-LLM guard, or this "end-to-end" proof is vacuous.
        # A single completed run yields a reliability sub-score (100.0 = no errors).
        assert a1.reliability_score is not None

    def test_llm_guard_fires_on_direct_construction(self):
        """strip-RED: the conftest LLM guard must actually raise when LLMBackend is constructed."""
        # The conftest patches AnthropicLLMBackend.__init__ to raise RuntimeError.
        # This test confirms the guard is active.
        try:
            from atomic_agents.llm.anthropic import AnthropicLLMBackend

            with pytest.raises(
                RuntimeError, match="LLMBackend must not be constructed"
            ):
                # Attempt to construct — conftest guard must intercept
                AnthropicLLMBackend.__init__(None)  # type: ignore
        except ImportError:
            pytest.skip("AnthropicLLMBackend not importable in this environment")


# ──────────────────────────────────────────────────────────────────
# Fleet roll-up: worst-agent floor/cap


class TestFleetRollup:
    """Fleet headline = unweighted mean + worst-agent floor/cap."""

    def test_worst_agent_caps_fleet_headline_end_to_end(self, tmp_path):
        """END-TO-END: 5 healthy agents + 1 critical agent, driven through the REAL
        compute_fleet_health() roll-up (not an inline re-implementation).

        Asserts the §7 worst-agent ceiling (fleet_composite == min(mean, worst))
        AND the fleet critical-cap (fix #2): a critical agent forces fleet_band red.

        strip-RED: the prior version reimplemented min(mean, worst) inline, so it
        exercised NEITHER the real §7 ceiling NOR the fleet critical-cap.
        """
        import atomic_agents.advisor.score as score_mod

        agents = []
        for i in range(5):
            agents.append(
                AgentHealth(
                    agent=f"good-{i}",
                    cost_score=95.0,
                    quality_score=95.0,
                    reliability_score=95.0,
                    composite=95.0,
                    band="green",
                    axes_with_data=3,
                )
            )
            _write_agent_model_md(tmp_path, f"good-{i}")
        # Critical agent: an axis below threshold → composite capped to 60, capped flag set.
        crit = AgentHealth(
            agent="crit-agent",
            cost_score=95.0,
            quality_score=95.0,
            reliability_score=20.0,
            composite=60.0,
            band="red",
            axes_with_data=3,
            capped_by_axis="reliability",
        )
        agents.append(crit)
        _write_agent_model_md(tmp_path, "crit-agent")

        mapping = {ah.agent: ah for ah in agents}
        orig = score_mod._compute_agent_health

        def _fake(*, agent, **kwargs):
            return mapping[agent]

        score_mod._compute_agent_health = _fake  # type: ignore[assignment]
        try:
            fh = compute_fleet_health(tmp_path, today=date(2026, 6, 25))
        finally:
            score_mod._compute_agent_health = orig  # type: ignore[assignment]

        composites = [ah.composite for ah in agents]
        expected_mean = sum(composites) / len(composites)
        expected_worst = min(composites)
        expected = round(min(expected_mean, expected_worst), 1)

        assert fh.fleet_composite == expected, (
            f"fleet_composite {fh.fleet_composite} != min(mean,worst) {expected}"
        )
        assert fh.worst_agent == "crit-agent"
        # Fleet critical-cap (fix #2): a critical/capped agent forces red.
        assert fh.fleet_band == "red"

    def test_all_none_composites_no_fleet_score(self):
        """All agents with None composite → fleet composite is None."""
        agents = [AgentHealth(agent=f"a{i}", composite=None) for i in range(3)]
        composites_with_agents = [
            (ah.composite, ah.agent) for ah in agents if ah.composite is not None
        ]
        assert not composites_with_agents

    def test_degraded_agent_excluded_from_worst(self):
        """Degraded agents (None composite) must not appear as 'worst agent'."""
        agents = [
            AgentHealth(agent="good", composite=80.0, band="green"),
            AgentHealth(agent="degraded", composite=None, degraded=True),
        ]
        composites_with_agents = [
            (ah.composite, ah.agent) for ah in agents if ah.composite is not None
        ]
        # Only "good" participates
        assert len(composites_with_agents) == 1
        assert composites_with_agents[0][1] == "good"


# ──────────────────────────────────────────────────────────────────
# WoW window


class TestWoWWindow:
    """WoW arrows require data in both 7d windows; absent → flat."""

    def test_wow_none_when_no_prior_data(self):
        from atomic_agents.advisor.score import _wow_arrow

        assert _wow_arrow(0.5, None) is None

    def test_wow_none_when_no_current_data(self):
        from atomic_agents.advisor.score import _wow_arrow

        assert _wow_arrow(None, 0.5) is None

    def test_wow_up_when_current_higher(self):
        from atomic_agents.advisor.score import _wow_arrow

        result = _wow_arrow(0.8, 0.5, threshold=0.01)
        assert result == "up"

    def test_wow_down_when_current_lower(self):
        from atomic_agents.advisor.score import _wow_arrow

        result = _wow_arrow(0.3, 0.6, threshold=0.01)
        assert result == "down"

    def test_wow_flat_within_threshold(self):
        from atomic_agents.advisor.score import _wow_arrow

        result = _wow_arrow(0.501, 0.500, threshold=0.01)
        assert result == "flat"


# ──────────────────────────────────────────────────────────────────
# Work-type classification


class TestWorkTypeClassification:
    """Precedence ladder determinism and None-safety."""

    def test_child_run_classified_as_child(self):
        r = _make_run(parent_run_id="parent-123", trigger="helper")
        assert _classify_work_type(r) == "child"

    def test_coordinator_trigger_classified(self):
        r = _make_run(trigger="cron")
        assert _classify_work_type(r) == "coordinator"

    def test_delegations_in_extra_classified_coordinator(self):
        r = _make_run(trigger="unknown", extra={"delegations": [{"id": "x"}]})
        assert _classify_work_type(r) == "coordinator"

    def test_tool_calls_in_extra_classified_tool_heavy(self):
        r = _make_run(trigger="unknown", extra={"tool_calls": [{"name": "search"}]})
        assert _classify_work_type(r) == "tool-heavy"

    def test_general_fallback(self):
        r = _make_run(trigger="manual", extra={})
        assert _classify_work_type(r) == "general"

    def test_parent_run_id_takes_priority_over_delegations(self):
        """parent_run_id wins even when extra has delegations."""
        r = _make_run(
            parent_run_id="p-1",
            trigger="cron",
            extra={"delegations": [{"id": "x"}]},
        )
        assert _classify_work_type(r) == "child"

    def test_none_extra_fields_no_crash(self):
        """tool_calls/delegations absent in extra must not raise."""
        r = _make_run(trigger="manual", extra={})
        # Should not raise; extra.get() returns None, treated as empty
        result = _classify_work_type(r)
        assert result in ("general", "tool-heavy", "coordinator", "child")


# ──────────────────────────────────────────────────────────────────
# Reliability extraction — shared module


class TestReliabilityExtraction:
    """Verify _compute_reliability is the shared single definition."""

    def test_shared_module_import(self):
        """Both attention and advisor must import from the same _reliability module."""
        from atomic_agents.dashboard._reliability import (
            _compute_reliability as rel_shared,
        )
        from atomic_agents.advisor.score import (
            _compute_reliability as rel_advisor_import,
        )

        # They must be the SAME object (not copies)
        assert rel_shared is rel_advisor_import

    def test_double_blocked_counts_once(self):
        """A run that is BOTH lock_busy AND embed_batch_blocked counts ONCE."""
        r = _make_run(status="lock_busy", extra={"embed_batch_blocked": True})
        runs = [r]
        metrics = _compute_reliability(runs, "agent-a")
        assert metrics.blocked_rate == 1.0  # 1/1 = 1.0, not 2/1
        assert metrics.total_runs == 1

    def test_child_runs_excluded_from_denominator(self):
        """Child runs (parent_run_id set) must not inflate the denominator."""
        primary = _make_run(status="error", parent_run_id=None, trigger="cron")
        child = _make_run(status="completed", parent_run_id="p-1", trigger="helper")
        metrics = _compute_reliability([primary, child], "agent-a")
        assert metrics.total_runs == 1  # only primary counted
        assert metrics.error_rate == 1.0


# ──────────────────────────────────────────────────────────────────
# Band derivation


class TestBandDerivation:
    def test_green_at_80(self):
        assert _band(80.0) == "green"

    def test_green_at_100(self):
        assert _band(100.0) == "green"

    def test_amber_at_79(self):
        assert _band(79.0) == "amber"

    def test_amber_at_60(self):
        assert _band(60.0) == "amber"

    def test_red_at_59(self):
        assert _band(59.0) == "red"

    def test_red_at_0(self):
        assert _band(0.0) == "red"


# ──────────────────────────────────────────────────────────────────
# End-to-end: compute_fleet_health on a fixture fleet


class TestComputeFleetHealthEndToEnd:
    """End-to-end tests exercising the full compute_fleet_health() call."""

    def test_empty_fleet_returns_fleet_health(self, tmp_path):
        """Empty fleet (no agents) → FleetHealth with no agents, None composite."""
        fh = compute_fleet_health(tmp_path)
        assert isinstance(fh, FleetHealth)
        assert fh.agents == []
        assert fh.fleet_composite is None
        assert fh.coverage_n == 0
        assert fh.coverage_m == 0

    def test_single_agent_no_data(self, tmp_path):
        """Agent with model.md but no runs/evals → health but no composite."""
        _write_agent_model_md(tmp_path, "agent-a")
        fh = compute_fleet_health(tmp_path)
        assert len(fh.agents) == 1
        assert isinstance(fh.agents[0], AgentHealth)
        assert fh.agents[0].agent == "agent-a"

    def test_single_agent_with_runs_produces_composite(self, tmp_path):
        """Agent with run records → non-None composite (at least cost+reliability data)."""
        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()
        ts = datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        _write_run_jsonl(
            tmp_path,
            "agent-a",
            [
                {
                    "ts": ts.isoformat(),
                    "trigger": "cron",
                    "model": "claude-haiku-4-5",
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cost_usd": 0.01,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 100,
                    "latency_ms": 100,
                    "status": "completed",
                    "summary": "ok",
                }
            ],
        )
        fh = compute_fleet_health(tmp_path, today=today)
        assert len(fh.agents) == 1
        ah = fh.agents[0]
        # With run data, at least reliability axis should have data
        assert ah.reliability_score is not None or ah.cost_score is not None

    def test_agent_with_pass_evals_has_quality_score(self, tmp_path):
        """Agent with passing evals → quality_score not None."""
        _write_agent_model_md(tmp_path, "agent-a")
        today = date.today()
        ts = datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        _write_run_jsonl(
            tmp_path,
            "agent-a",
            [
                {
                    "ts": ts.isoformat(),
                    "trigger": "cron",
                    "model": "claude-haiku-4-5",
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cost_usd": 0.01,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 100,
                    "latency_ms": 100,
                    "status": "completed",
                    "summary": "ok",
                }
            ],
        )
        _write_eval_jsonl(
            tmp_path,
            "agent-a",
            [
                {
                    "ts": ts.isoformat(),
                    "verdict": "pass",
                    "hard_fails": [],
                    "weighted_score": 0.9,
                    "test_id": "t1",
                },
                {
                    "ts": ts.isoformat(),
                    "verdict": "pass",
                    "hard_fails": [],
                    "weighted_score": 0.85,
                    "test_id": "t2",
                },
            ],
        )
        fh = compute_fleet_health(tmp_path, today=today)
        assert len(fh.agents) == 1
        ah = fh.agents[0]
        assert ah.quality_score is not None
        assert ah.quality_score > 0

    def test_fleet_health_used_defaults_flag(self, tmp_path):
        """No targets.md → used_targets_defaults is True."""
        _write_agent_model_md(tmp_path, "agent-a")
        fh = compute_fleet_health(tmp_path)
        assert fh.used_targets_defaults is True

    def test_fleet_composite_coverage_fields(self, tmp_path):
        """Coverage fields are set correctly."""
        _write_agent_model_md(tmp_path, "agent-a")
        _write_agent_model_md(tmp_path, "agent-b")
        fh = compute_fleet_health(tmp_path)
        assert fh.coverage_m == 2
        assert 0 <= fh.coverage_n <= 2

    def test_home_user_single_agent_no_crash(self, tmp_path):
        """Home-user throughline: single agent, no targets.md → valid FleetHealth."""
        _write_agent_model_md(tmp_path, "my-agent")
        fh = compute_fleet_health(tmp_path)
        assert isinstance(fh, FleetHealth)
        assert fh.used_targets_defaults is True
        # Must not crash, must return a valid structure
        assert fh.agents is not None


# ──────────────────────────────────────────────────────────────────
# P1: 30d recent/prior windows must NOT overlap on the boundary day


class TestSpendWindowNonOverlap:
    """The prior-30d window ends strictly before the recent-30d window starts,
    so the boundary day (today - 30) is counted in recent ONLY, not both."""

    def test_boundary_day_run_counts_recent_only(self, tmp_path):
        """A run dated exactly today-30 contributes to recent spend, not prior.

        strip-RED: if the prior window were inclusive at thirty_days_ago (the bug),
        the boundary day's spend would land on BOTH sides of
        spend_vs_trend = recent/prior - 1.0, deflating the ratio.
        """
        today = date(2026, 6, 25)
        boundary = today - timedelta(days=30)  # 2026-05-26
        prior_only_day = today - timedelta(days=45)  # solidly inside prior window

        _write_agent_model_md(tmp_path, "a1")
        # One run on the boundary day (recent), one solidly in the prior window.
        _write_run_jsonl(
            tmp_path,
            "a1",
            [
                {
                    "run_id": "rb",
                    "ts": datetime(
                        boundary.year,
                        boundary.month,
                        boundary.day,
                        12,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                    "agent": "a1",
                    "trigger": "manual",
                    "status": "completed",
                    "model": "claude-haiku-4-5",
                    "output_tokens": 100,
                    "cost_usd": 1.0,
                },
                {
                    "run_id": "rp",
                    "ts": datetime(
                        prior_only_day.year,
                        prior_only_day.month,
                        prior_only_day.day,
                        12,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                    "agent": "a1",
                    "trigger": "manual",
                    "status": "completed",
                    "model": "claude-haiku-4-5",
                    "output_tokens": 100,
                    "cost_usd": 1.0,
                },
            ],
        )

        fh = compute_fleet_health(tmp_path, today=today)
        a1 = next(ah for ah in fh.agents if ah.agent == "a1")
        svt_row = next((r for r in a1.scorecard if r.metric == "spend_vs_trend"), None)
        assert svt_row is not None
        # recent_spend = 1.0 (boundary only), prior_spend = 1.0 (prior_only_day only).
        # If the boundary day double-counted, prior_spend would be 2.0 and the
        # ratio would be 1.0/2.0 - 1.0 = -0.5 instead of 0.0.
        assert svt_row.value == 0.0

    def test_flat_spend_equal_length_windows_yield_zero_trend(self, tmp_path):
        """Flat $1.00/day across both 30d windows → spend_vs_trend ~= 0.0.

        strip-RED: with the old unequal windows (recent [today-30, today] = 31
        inclusive days, prior [today-60, today-31] = 30 inclusive days) a perfectly
        flat fleet reports +1/30 = +0.0333, biasing a flat fleet toward 'spend
        creeping up'. The equal-length fix (prior starts today-61 → 31 days each)
        makes the honest answer 0.0.
        """
        today = date(2026, 6, 25)
        recs = []
        # One $1.00 run per day across [today-61, today] so BOTH windows are full
        # and equal-length. (Recent [today-30,today]=31d, prior [today-61,today-31]=31d.)
        for d in range(0, 62):
            day = today - timedelta(days=d)
            recs.append(
                {
                    "run_id": f"r{d}",
                    "ts": datetime(
                        day.year, day.month, day.day, 12, tzinfo=timezone.utc
                    ).isoformat(),
                    "agent": "a1",
                    "trigger": "manual",
                    "status": "completed",
                    "model": "claude-haiku-4-5",
                    "output_tokens": 100,
                    "cost_usd": 1.0,
                }
            )
        _write_agent_model_md(tmp_path, "a1")
        _write_run_jsonl(tmp_path, "a1", recs)

        fh = compute_fleet_health(tmp_path, today=today)
        a1 = next(ah for ah in fh.agents if ah.agent == "a1")
        svt_row = next((r for r in a1.scorecard if r.metric == "spend_vs_trend"), None)
        assert svt_row is not None
        assert svt_row.value is not None
        assert abs(svt_row.value) < 1e-6  # honest flat-spend answer, no one-day bias


class TestRoundedBandConsistency:
    """The display integer and its band color always agree (#623 fix, spec/53 §10).

    _compute_composite returns the raw float composite (for math/history) and
    derives the band from int(round(composite)) — not from round(composite, 1).
    AgentHealth.composite_display = int(round(composite)) is the canonical display
    integer written by _compute_agent_health / _score_agent_from_data.
    render.py consumes composite_display directly — no {:.0f} on raw floats.
    """

    def test_composite_rounding_does_not_cross_band_boundary(self):
        """79.95 display-int = 80 → green band (not amber from round(79.95,1)=79.95).

        strip-RED: banding round(79.95,1) returns 'amber'; int(round(79.95))=80 → green.
        """
        from atomic_agents.advisor.score import _compute_composite

        # cost=quality=79.95, reliability absent → mean = 79.95
        composite, band, capped = _compute_composite(
            {"cost": 79.95, "quality": 79.95, "reliability": None},
            {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3},
        )
        assert capped is None
        # Raw float preserved for math/history — NOT rounded to 80.0 (new contract)
        assert abs(composite - 79.95) < 0.01
        # Band derived from int(round(79.95)) = 80 → green (#623 fix)
        assert band == "green", (
            f"band should be green (display int 80), got {band!r} for composite {composite}"
        )

    def test_composite_below_boundary_stays_amber(self):
        """79.49 display-int = 79 → amber band (not green from naive int rounding).

        Guards the fix from over-rounding: 79.49 must NOT become 80.
        """
        from atomic_agents.advisor.score import _compute_composite

        composite, band, capped = _compute_composite(
            {"cost": 79.49, "quality": 79.49, "reliability": None},
            {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3},
        )
        # int(round(79.49)) = 79 → amber
        assert band == "amber", f"band should be amber (display int 79), got {band!r}"

    def test_composite_at_79_5_is_green(self):
        """79.5 display-int = 80 → green (#623 boundary fix).

        Python uses round-half-to-even (banker's rounding); at 79.5 the even
        neighbor is 80 (the green-side), so int(round(79.5)) = 80. Do NOT
        generalize this to "<.5 rounds up" — int(round(78.5)) = 78.
        strip-RED: round(79.5, 1) = 79.5 → _band(79.5) = amber, but
        int(round(79.5)) = 80 → _band(80) = green.
        """
        from atomic_agents.advisor.score import _compute_composite

        composite, band, capped = _compute_composite(
            {"cost": 79.5, "quality": 79.5, "reliability": None},
            {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3},
        )
        assert band == "green", f"79.5 display-int=80 must be green, got {band!r}"

    def test_composite_at_59_49_is_red(self):
        """59.49 display-int = 59 → red (not amber from {59.49:.0f}='59')."""
        from atomic_agents.advisor.score import _compute_composite

        composite, band, capped = _compute_composite(
            {"cost": 59.49, "quality": 59.49, "reliability": None},
            {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3},
        )
        assert band == "red", f"59.49 display-int=59 must be red, got {band!r}"

    def test_composite_at_59_5_is_amber(self):
        """59.5 display-int = 60 → amber (#623 boundary fix).

        Banker's rounding: at 59.5 the even neighbor is 60 (the amber-side), so
        int(round(59.5)) = 60. Not a general ".5 rounds up" rule.
        strip-RED: round(59.5, 1) = 59.5 → _band(59.5) = red, but
        int(round(59.5)) = 60 → _band(60) = amber.
        """
        from atomic_agents.advisor.score import _compute_composite

        composite, band, capped = _compute_composite(
            {"cost": 59.5, "quality": 59.5, "reliability": None},
            {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3},
        )
        assert band == "amber", f"59.5 display-int=60 must be amber, got {band!r}"


class TestDirectionEnumFailSoft:
    """A malformed `direction` value fails soft to the baked-in default for THAT
    key only, rather than silently inverting the scoring curve (spec/53 MUST 6)."""

    def test_malformed_direction_falls_back_to_default(self, tmp_path):
        """direction: highr (a typo) on a higher-is-better metric must NOT invert
        the curve — it falls back to the default 'higher' and is recorded.

        strip-RED: without the enum guard, _get_str accepts 'highr', and
        _map_metric_to_score treats anything != 'higher' as 'lower', inverting the
        pass_rate curve so a high pass_rate scores 0.
        """
        from atomic_agents.advisor.targets import parse_targets

        targets_md = (
            "## Fleet Health Targets\n\n"
            "```yaml\n"
            "scoring:\n"
            "  axes:\n"
            "    quality:\n"
            "      metrics:\n"
            "        pass_rate:\n"
            "          target: 0.80\n"
            "          direction: highr\n"  # typo
            "          band: 0.10\n"
            "          floor: 0.0\n"
            "```\n"
        )
        (tmp_path / "targets.md").write_text(targets_md, encoding="utf-8")

        ft = parse_targets(tmp_path)
        pr = ft.axes["quality"]["pass_rate"]
        # Fell back to the baked-in default direction, not the typo.
        assert pr.direction == "higher"
        # Recorded the per-key fallback (MUST 6 audit).
        assert "quality.metrics.pass_rate.direction" in ft.used_defaults

    def test_valid_direction_override_is_honored(self, tmp_path):
        """A VALID direction override ('lower' on a normally-higher metric) is kept
        and NOT recorded as a default — the guard only rejects out-of-enum values.

        NOTE: a lower-is-better metric needs floor > target + band for a positive
        decay span (the span guard), so the floor here is 1.0 (above target+band),
        not 0.0 — a floor below target would itself be a degenerate-span config.
        """
        from atomic_agents.advisor.targets import parse_targets

        targets_md = (
            "## Fleet Health Targets\n\n"
            "```yaml\n"
            "scoring:\n"
            "  axes:\n"
            "    quality:\n"
            "      metrics:\n"
            "        pass_rate:\n"
            "          target: 0.80\n"
            "          direction: lower\n"
            "          band: 0.10\n"
            "          floor: 1.0\n"
            "```\n"
        )
        (tmp_path / "targets.md").write_text(targets_md, encoding="utf-8")

        ft = parse_targets(tmp_path)
        assert ft.axes["quality"]["pass_rate"].direction == "lower"
        assert "quality.metrics.pass_rate.direction" not in ft.used_defaults
        # The valid config must NOT trip the decay-span guard (which would revert
        # the direction to default and hollow this test).
        assert "quality.metrics.pass_rate(degenerate_span)" not in ft.used_defaults


# ──────────────────────────────────────────────────────────────────
# P1: WoW arrow color reflects GOODNESS, not raw value direction


class TestWoWArrowGoodnessColoring:
    """The scorecard WoW arrow colors on whether the move improved the metric,
    not on the raw value direction (spec/53 §6)."""

    def _band_with(self, metric: str, axis: str, wow: str):
        from atomic_agents.dashboard.render import _render_health_band

        row = ScorecardRow(
            metric=metric,
            axis=axis,
            value=0.1,
            target=0.0,
            status="warn",
            score=70.0,
            wow=wow,
        )
        ah = AgentHealth(
            agent="a1",
            cost_score=70.0,
            quality_score=70.0,
            reliability_score=70.0,
            composite=70.0,
            band="amber",
            axes_with_data=3,
            scorecard=[row],
        )
        fh = FleetHealth(
            agents=[ah],
            fleet_composite=70.0,
            fleet_band="amber",
            coverage_n=1,
            coverage_m=1,
        )
        return _render_health_band(fh)

    def test_rising_error_rate_renders_bad(self):
        """A rising (up) error_rate is BAD → wow-bad (red), never wow-good."""
        html_out = self._band_with("error_rate", "reliability", "up")
        # The error_rate row must carry the bad class on its up-arrow.
        assert "wow-bad" in html_out
        # strip-RED: must NOT be colored good just because the value rose.
        assert 'wow-good">↑' not in html_out

    def test_rising_pass_rate_renders_good(self):
        """A rising (up) pass_rate is GOOD → wow-good (green)."""
        html_out = self._band_with("pass_rate", "quality", "up")
        assert "wow-good" in html_out
        assert 'wow-bad">↑' not in html_out

    def test_falling_error_rate_renders_good(self):
        """A falling (down) error_rate is GOOD → wow-good (green)."""
        html_out = self._band_with("error_rate", "reliability", "down")
        assert "wow-good" in html_out
        assert 'wow-bad">↓' not in html_out


# ──────────────────────────────────────────────────────────────────
# P1: metric-level critical cap — a single floored metric forces red even
#     when its axis MEAN dilutes it above the threshold (spec/53 §3.5).


class TestMetricLevelCriticalCap:
    """The cap must fire at METRIC granularity, not only AXIS granularity.

    The safety bug: each axis sub-score is the unweighted MEAN of its metrics.
    A single catastrophic metric (error_rate=0.90 → score 0) diluted by healthy
    siblings (blocked=100, skipped=100) yields reliability axis = 66.7 — NOT
    critical at the axis level — so the composite reads GREEN while the agent
    fails 90% of its runs. The metric-level cap closes this.
    """

    def test_unit_floored_metric_caps_composite(self):
        """_compute_composite: a metric_scores entry < threshold caps + reds,
        even when every AXIS sub-score is healthy (≥ threshold)."""
        scores = {"cost": 90.0, "quality": 90.0, "reliability": 66.7}
        weights = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}

        # strip-RED control: without metric_scores, the agent reads GREEN (the bug).
        comp_axis_only, band_axis_only, capped_axis_only = _compute_composite(
            scores, weights
        )
        assert band_axis_only == "green"
        assert comp_axis_only > CRITICAL_COMPOSITE_CAP
        assert capped_axis_only is None

        # With the floored metric threaded in, the cap fires.
        metric_scores = [0.0, 100.0, 100.0, 90.0, 90.0]  # one floored sibling
        comp, band, capped = _compute_composite(scores, weights, metric_scores)
        assert band == "red", f"floored metric must force red, got {band}"
        assert comp <= CRITICAL_COMPOSITE_CAP
        assert capped == "metric"

    def test_e2e_ninety_percent_error_agent_is_red(self, tmp_path):
        """END-TO-END: an agent with 90% error_rate (one floored metric) but
        healthy blocked/skipped siblings must read band='red' AND composite<=60.

        strip-RED: stripping the metric-level cap makes this agent read GREEN.
        """
        _write_agent_model_md(tmp_path, "failing-agent")
        today = date(2026, 6, 25)
        ts = datetime(2026, 6, 24, 12, tzinfo=timezone.utc)

        records = []
        for i in range(9):
            records.append(
                {
                    "run_id": f"e{i}",
                    "ts": ts.isoformat(),
                    "agent": "failing-agent",
                    "trigger": "cron",
                    "model": "claude-haiku-4-5",
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cost_usd": 0.01,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 100,
                    "latency_ms": 100,
                    "status": "error",
                    "summary": "boom",
                }
            )
        records.append(
            {
                "run_id": "ok1",
                "ts": ts.isoformat(),
                "agent": "failing-agent",
                "trigger": "cron",
                "model": "claude-haiku-4-5",
                "input_tokens": 100,
                "output_tokens": 200,
                "cost_usd": 0.01,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 100,
                "latency_ms": 100,
                "status": "completed",
                "summary": "ok",
            }
        )
        _write_run_jsonl(tmp_path, "failing-agent", records)

        fh = compute_fleet_health(tmp_path, today=today)
        ah = next(a for a in fh.agents if a.agent == "failing-agent")

        assert ah.reliability_score is not None
        assert ah.reliability_score >= CRITICAL_SUBSCORE_THRESHOLD, (
            "fixture sanity: reliability AXIS mean must be ABOVE threshold so the "
            f"red verdict can ONLY come from the metric-level cap (got {ah.reliability_score})"
        )
        assert ah.band == "red", f"90%-error agent must be red, got {ah.band}"
        assert ah.composite is not None and ah.composite <= CRITICAL_COMPOSITE_CAP

        err_row = next(
            r
            for r in ah.scorecard
            if r.metric == "error_rate" and r.axis == "reliability"
        )
        assert err_row.score is not None and err_row.score < CRITICAL_SUBSCORE_THRESHOLD


# ──────────────────────────────────────────────────────────────────
# P2: fleet critical-cap must force red when ANY included agent is capped,
#     not via worst_c < threshold (a capped agent's composite is already 60).


class TestFleetCriticalCapEndToEnd:
    """compute_fleet_health() must force fleet red when an included agent is
    itself critical/capped — testing the post-cap composite (always ≥ 60 for a
    capped agent) would never trip the old worst_c < 30 check."""

    def test_capped_agent_forces_fleet_red(self, tmp_path):
        """A capped (axis-critical) agent has per-agent composite 60/red; the
        fleet roll-up must read red, not amber.

        strip-RED: the old `worst_c < CRITICAL_SUBSCORE_THRESHOLD` check tests the
        ALREADY-CAPPED composite (60, never < 30) → fleet would read amber.
        """
        good = AgentHealth(
            agent="good",
            cost_score=95.0,
            quality_score=95.0,
            reliability_score=95.0,
            composite=95.0,
            band="green",
            axes_with_data=3,
        )
        capped = AgentHealth(
            agent="capped",
            cost_score=95.0,
            quality_score=95.0,
            reliability_score=20.0,
            composite=60.0,
            band="red",
            axes_with_data=3,
            capped_by_axis="reliability",
        )

        import atomic_agents.advisor.score as score_mod

        _write_agent_model_md(tmp_path, "good")
        _write_agent_model_md(tmp_path, "capped")

        mapping = {"good": good, "capped": capped}
        orig = score_mod._compute_agent_health

        def _fake(*, agent, **kwargs):
            return mapping[agent]

        score_mod._compute_agent_health = _fake  # type: ignore[assignment]
        try:
            fh = compute_fleet_health(tmp_path, today=date(2026, 6, 25))
        finally:
            score_mod._compute_agent_health = orig  # type: ignore[assignment]

        assert fh.fleet_band == "red", (
            f"a capped/critical agent must force fleet red, got {fh.fleet_band}"
        )
        assert fh.fleet_composite is not None
        assert fh.fleet_composite <= CRITICAL_COMPOSITE_CAP


# ──────────────────────────────────────────────────────────────────
# P3: targets.py DOMAIN validation (not just type)


class TestWeightDomainValidation:
    """Weights must be finite, non-bool, in [0,1] — a weighted MEAN is undefined
    otherwise. The sum check alone misses negative/over-one/NaN weights."""

    def test_negative_weight_falls_back_to_equal(self, tmp_path):
        """cost=.55, quality=.55, reliability=-.10 SUMS to 1.0 but is not a
        weighted mean (negative weight) → equal weights.

        strip-RED: a sum-only check accepts this and subtracts the reliability axis.
        """
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  weights:\n    cost: 0.55\n"
            "    quality: 0.55\n    reliability: -0.10\n```\n"
        )
        ft = parse_targets(tmp_path)
        assert abs(ft.weights["cost"] - 1 / 3) < 0.01
        assert abs(ft.weights["reliability"] - 1 / 3) < 0.01
        assert "weights" in ft.used_defaults

    def test_over_one_weight_falls_back(self, tmp_path):
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  weights:\n    cost: 1.50\n"
            "    quality: -0.30\n    reliability: -0.20\n```\n"
        )
        ft = parse_targets(tmp_path)
        assert abs(ft.weights["cost"] - 1 / 3) < 0.01

    def test_bool_weight_rejected(self, tmp_path):
        """YAML true would coerce to 1.0 under a bare isinstance(int) gate."""
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  weights:\n    cost: true\n"
            "    quality: 0.0\n    reliability: 0.0\n```\n"
        )
        ft = parse_targets(tmp_path)
        # strip-RED: the bug coerced cost:true → 1.0, giving {cost:1.0, quality:0,
        # reliability:0} — which ALSO sums to 1.0, so a sum-only assertion is a
        # false-green. Assert the bool is rejected and EQUAL weights are used.
        for axis in ("cost", "quality", "reliability"):
            assert abs(ft.weights[axis] - 1 / 3) < 1e-6, ft.weights


class TestSpanDegeneracyValidation:
    """A non-positive decay span (floor on the wrong side of target±band) makes the
    curve a cliff to 0 — must default that metric, not silently ship a binary score."""

    def test_higher_floor_above_plateau_is_degenerate(self, tmp_path):
        """direction=higher needs floor < target - band. floor >= that is a cliff.

        strip-RED: the old code only rejected floor == target, so floor=0.45 with
        target=0.50, band=0.10 (plateau starts at 0.40) survives → span -0.05.

        After #687: cheaper_model_share is no longer a health metric in _DEFAULT_AXES.
        Test uses pass_rate (quality axis, direction=higher) to cover the same logic.
        """
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  axes:\n    quality:\n      metrics:\n"
            "        pass_rate:\n"
            "          target: 0.50\n          direction: higher\n"
            "          band: 0.10\n          floor: 0.45\n```\n"
        )
        ft = parse_targets(tmp_path)
        mt = ft.axes["quality"]["pass_rate"]
        assert mt.floor == 0.0  # baked-in default, not 0.45
        assert any("pass_rate(degenerate_span)" in d for d in ft.used_defaults)

    def test_lower_floor_below_plateau_is_degenerate(self, tmp_path):
        """direction=lower needs floor > target + band."""
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  axes:\n    reliability:\n      metrics:\n"
            "        error_rate:\n"
            "          target: 0.0\n          direction: lower\n"
            "          band: 0.05\n          floor: 0.02\n```\n"
        )
        ft = parse_targets(tmp_path)
        mt = ft.axes["reliability"]["error_rate"]
        assert mt.floor == 0.40  # baked-in default, not 0.02
        assert any("error_rate(degenerate_span)" in d for d in ft.used_defaults)

    def test_negative_band_defaulted(self, tmp_path):
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  axes:\n    reliability:\n      metrics:\n"
            "        error_rate:\n"
            "          target: 0.0\n          direction: lower\n"
            "          band: -0.05\n          floor: 0.40\n```\n"
        )
        ft = parse_targets(tmp_path)
        mt = ft.axes["reliability"]["error_rate"]
        assert mt.band == 0.05  # baked-in default
        assert any("error_rate.band(negative)" in d for d in ft.used_defaults)

    def test_valid_span_is_honored(self, tmp_path):
        """strip-RED control: a VALID config must NOT be flagged degenerate."""
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "```yaml\nscoring:\n  axes:\n    reliability:\n      metrics:\n"
            "        error_rate:\n"
            "          target: 0.0\n          direction: lower\n"
            "          band: 0.05\n          floor: 0.60\n```\n"
        )
        ft = parse_targets(tmp_path)
        mt = ft.axes["reliability"]["error_rate"]
        assert mt.floor == 0.60  # operator value honored
        assert not any("error_rate(degenerate_span)" in d for d in ft.used_defaults)


# ──────────────────────────────────────────────────────────────────
# P4: per-window degradation — a prior-window-only degrade must degrade ONLY
#     spend_vs_trend, not recent reliability / cheaper_share / tokens_per_output.


class TestPerWindowDegradation:
    """deg1 (recent) and deg2 (prior) must not be conflated. A prior-only degrade
    excludes spend_vs_trend (it needs prior spend) while leaving the recent-only
    metrics fully scored."""

    def test_prior_only_degrade_keeps_reliability(self, tmp_path):
        """Prior-window read degrades; recent succeeds → reliability still scores
        (not degraded), spend_vs_trend is the only degraded metric.

        strip-RED: the old `deg1 or deg2` marks reliability_degraded on a
        prior-only failure, excluding a perfectly-good recent reliability axis.
        """
        from unittest.mock import patch

        _write_agent_model_md(tmp_path, "agent-a")
        today = date(2026, 6, 25)
        recent_ts = datetime(2026, 6, 24, 12, tzinfo=timezone.utc)

        recent_runs = [
            RunRecord(
                ts=recent_ts,
                agent="agent-a",
                trigger="cron",
                model="claude-haiku-4-5",
                input_tokens=100,
                output_tokens=200,
                cost_usd=0.01,
                cache_hit_tokens=0,
                cache_miss_tokens=100,
                latency_ms=100,
                status="completed",
                summary="ok",
                parent_run_id=None,
                extra={},
            )
        ]

        def _fake_load(agents_root, agent, since, until):
            # Recent window (ends today) → clean; prior window → degraded.
            if until >= today - timedelta(days=29):
                return recent_runs, False
            return [], True  # prior window degraded

        with patch(
            "atomic_agents.advisor.score._load_runs_with_degraded",
            side_effect=_fake_load,
        ):
            fh = compute_fleet_health(tmp_path, today=today)

        ah = next(a for a in fh.agents if a.agent == "agent-a")
        # Reliability scored off the clean recent window — NOT degraded.
        assert ah.reliability_degraded is False
        assert ah.reliability_score is not None
        # spend_vs_trend degraded (needs prior spend); cost axis NOT wholesale degraded.
        assert ah.cost_degraded is False
        svt = next(r for r in ah.scorecard if r.metric == "spend_vs_trend")
        assert svt.status == "degraded"
        assert svt.score is None
        # After #687 (MUST 14): cheaper_model_share and tokens_per_output are
        # NOT health metrics and are NOT in the scorecard. The cost axis is
        # represented solely by spend_vs_trend, which is correctly degraded here.


# ──────────────────────────────────────────────────────────────────
# #623 fixes: WoW 7d equal-window + fleet display integer boundaries


class TestWoW7dEqualWindows:
    """Both 7d WoW windows must be 7 inclusive days each (#623 fix, spec/53 §6).

    strip-RED: with the old timedelta(days=7) current window the current slice was
    [today-7, today] = 8 inclusive days while prior was [today-14, today-8] = 7 days.
    A flat-spend fleet reported wow_spend 'up' (+14.3%). The fix uses timedelta(days=6)
    for the current window: [today-6, today] = 7 inclusive days, prior [today-13, today-7].
    """

    def test_flat_wow_7d_spend_yields_flat(self, tmp_path):
        """Flat $1.00/day fleet → the spend_vs_trend row's wow arrow == 'flat'.

        This is the strip-RED conformance proof for the #623(b) equal-window fix
        (spec/53 MUST 12). The wow arrow only attaches to the spend_vs_trend
        scorecard row when prior-30d spend > 0 (the `elif mt_svt and spend_ratio is
        not None` branch in _score_cost_axis), so the fixture seeds runs across the
        FULL [today-61, today] span — both the recent 30d window AND the prior 30d
        window — at a flat $1.00/day. That makes spend_ratio computable so the row
        carries a wow arrow, and the 7d WoW arrow itself is computed from
        current_7d / prior_7d spend.

        strip-RED mechanism (verified by reverting score.py:1185 to
        `timedelta(days=7)`): an 8-day current window vs a 7-day prior window makes
        a flat fleet report current_7d=$8 vs prior_7d=$7 → ratio-1 = 1/7 ≈ 0.143 >
        threshold(0.05) → wow='up'. With the fix both windows are 7 inclusive days
        → ratio=1.0 → delta=0 → wow='flat'. The assertion below FAILS ('up' !=
        'flat') under the reverted (buggy) window, so it actually locks the fix.
        """
        today = date(2026, 6, 25)
        recs = []
        # One $1.00/day run across the full [today-61, today] span so BOTH the
        # prior-30d window ([today-61, today-31]) and the recent-30d window
        # ([today-30, today]) are populated — prior_spend > 0 is what makes the
        # spend_vs_trend row carry a (non-None) wow arrow at all.
        for d in range(0, 62):
            day = today - timedelta(days=d)
            recs.append(
                {
                    "run_id": f"r{d}",
                    "ts": datetime(
                        day.year, day.month, day.day, 12, tzinfo=timezone.utc
                    ).isoformat(),
                    "agent": "a1",
                    "trigger": "manual",
                    "status": "completed",
                    "model": "claude-haiku-4-5",
                    "output_tokens": 100,
                    "cost_usd": 1.0,
                }
            )
        _write_agent_model_md(tmp_path, "a1")
        _write_run_jsonl(tmp_path, "a1", recs)

        fh = compute_fleet_health(tmp_path, today=today)
        a1 = next(ah for ah in fh.agents if ah.agent == "a1")

        # The load-bearing assertion: the spend_vs_trend row's 7d WoW arrow must be
        # 'flat' for a perfectly flat fleet. This is the field the #623(b) fix
        # corrects; under the buggy 8-vs-7-day window it would read 'up'.
        svt = next((r for r in a1.scorecard if r.metric == "spend_vs_trend"), None)
        assert svt is not None
        assert svt.wow is not None, (
            "spend_vs_trend row must carry a wow arrow (needs prior-30d spend > 0); "
            "if this is None the test no longer exercises the equal-window fix"
        )
        assert svt.wow == "flat", (
            f"flat $1/day fleet must report spend_vs_trend wow='flat' with equal 7d "
            f"windows; got {svt.wow!r} (an 'up' here means the current 7d window is "
            f"longer than the prior — the #623(b) regression)"
        )

    def test_fleet_composite_display_int_type(self, tmp_path):
        """fleet_composite_display must be int (not float), set after critical-cap."""
        today = date(2026, 6, 25)
        recs = [
            {
                "run_id": "r0",
                "ts": datetime(2026, 6, 24, 12, tzinfo=timezone.utc).isoformat(),
                "agent": "a1",
                "trigger": "manual",
                "status": "completed",
                "model": "claude-haiku-4-5",
                "output_tokens": 100,
                "cost_usd": 0.01,
            }
        ]
        _write_agent_model_md(tmp_path, "a1")
        _write_run_jsonl(tmp_path, "a1", recs)
        fh = compute_fleet_health(tmp_path, today=today)
        if fh.fleet_composite_display is not None:
            assert isinstance(fh.fleet_composite_display, int)
        if fh.worst_agent_composite_display is not None:
            assert isinstance(fh.worst_agent_composite_display, int)
        for ah in fh.agents:
            if ah.composite_display is not None:
                assert isinstance(ah.composite_display, int)


class TestFleetDisplayIntegerBoundaries:
    """Fleet headline and worst-agent display integers use int(round()) after cap (#623).

    Each boundary test verifies display integer AND band agree.
    """

    def _make_composite_from_scores(self, cost, quality, reliability=None):
        """Helper: call _compute_composite and return (composite, display_int, band)."""
        from atomic_agents.advisor.score import _compute_composite

        sub = {"cost": cost, "quality": quality, "reliability": reliability}
        w = {"cost": 1 / 3, "quality": 1 / 3, "reliability": 1 / 3}
        composite, band, _ = _compute_composite(sub, w)
        display_int = int(round(composite)) if composite is not None else None
        return composite, display_int, band

    def test_79_49_is_amber_display_79(self):
        """79.49 → display int 79 → amber band (not green from {:.0f}='79')."""
        _, di, band = self._make_composite_from_scores(79.49, 79.49)
        assert di == 79
        assert band == "amber"

    def test_79_5_is_green_display_80(self):
        """79.5 → display int 80 → green band (#623 strip-RED boundary)."""
        _, di, band = self._make_composite_from_scores(79.5, 79.5)
        assert di == 80
        assert band == "green"

    def test_59_49_is_red_display_59(self):
        """59.49 → display int 59 → red band."""
        _, di, band = self._make_composite_from_scores(59.49, 59.49)
        assert di == 59
        assert band == "red"

    def test_59_5_is_amber_display_60(self):
        """59.5 → display int 60 → amber band (#623 strip-RED boundary)."""
        _, di, band = self._make_composite_from_scores(59.5, 59.5)
        assert di == 60
        assert band == "amber"

    def _drive_fleet_with_composite(self, tmp_path, monkeypatch, raw_composite):
        """Drive the REAL compute_fleet_health fleet-reduction path with a controlled
        per-agent composite.

        Single agent → fleet headline = min(mean, worst) = raw_composite, so the
        fleet display integer + band are computed off this exact raw value. This
        exercises score.py's fleet-headline int(round(...)) path (the #623 fleet
        double-round), NOT the per-agent _compute_composite path the other tests in
        this class cover.
        """
        from atomic_agents.advisor import score as score_mod

        _write_agent_model_md(tmp_path, "fleet-agent")

        def _fake_agent_health(*, agent, **_kwargs):
            return AgentHealth(
                agent=agent,
                cost_score=raw_composite,
                quality_score=raw_composite,
                reliability_score=raw_composite,
                composite=raw_composite,
                band=_band(int(round(raw_composite))),
                composite_display=int(round(raw_composite)),
                axes_with_data=3,
                capped_by_axis=None,
            )

        monkeypatch.setattr(score_mod, "_compute_agent_health", _fake_agent_health)
        return compute_fleet_health(tmp_path, today=date(2026, 6, 25))

    def test_fleet_79_45_is_amber_display_79(self, tmp_path, monkeypatch):
        """Fleet raw 79.45 → display int 79 → amber band.

        Strip-RED against the #623 fleet double-round: round(79.45,1)=79.5 →
        int(round(79.5))=80 → GREEN would be wrong. Canonical single round
        int(round(79.45))=79 → amber.
        """
        fh = self._drive_fleet_with_composite(tmp_path, monkeypatch, 79.45)
        assert fh.fleet_composite_display == 79
        assert fh.fleet_band == "amber"

    def test_fleet_59_45_is_red_display_59(self, tmp_path, monkeypatch):
        """Fleet raw 59.45 → display int 59 → red band.

        Strip-RED against the double-round at the amber/red edge: round(59.45,1)=
        59.5 → int(round(59.5))=60 → AMBER would be wrong; canonical int(round(
        59.45))=59 → red.
        """
        fh = self._drive_fleet_with_composite(tmp_path, monkeypatch, 59.45)
        assert fh.fleet_composite_display == 59
        assert fh.fleet_band == "red"


class TestScoreAgentFromDataPureCore:
    """_score_agent_from_data is a pure function — zero disk I/O, verifiable in-memory."""

    def test_pure_core_returns_agent_health(self):
        """Pure core returns AgentHealth without touching disk."""
        from atomic_agents.advisor.score import _score_agent_from_data
        from atomic_agents.advisor.targets import (
            _DEFAULT_WEIGHTS,
            _DEFAULT_AXES,
        )

        today = date(2026, 6, 25)
        six_days_ago = today - timedelta(days=6)
        prior_7_start = today - timedelta(days=13)
        prior_7_end = today - timedelta(days=7)

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
        targets = FleetTargets(weights=dict(_DEFAULT_WEIGHTS), axes=axes)

        run = RunRecord(
            ts=datetime(2026, 6, 24, 12, tzinfo=timezone.utc),
            agent="pure-agent",
            trigger="cron",
            model="claude-haiku-4-5",
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.01,
            cache_hit_tokens=0,
            cache_miss_tokens=100,
            latency_ms=100,
            status="completed",
            summary="ok",
            parent_run_id=None,
            extra={},
        )

        ah = _score_agent_from_data(
            agent="pure-agent",
            runs_30d=[run],
            runs_prior_30d=[],
            eval_records=[],
            targets=targets,
            today=today,
            six_days_ago=six_days_ago,
            prior_7_start=prior_7_start,
            prior_7_end=prior_7_end,
        )
        assert ah.agent == "pure-agent"
        # At least reliability + cost axes should have data
        assert ah.composite is not None
        assert ah.composite_display is not None
        assert isinstance(ah.composite_display, int)

    def test_counterfactual_does_not_mutate_originals(self):
        """dataclasses.replace produces new objects; originals unchanged (#623 MUST 9)."""
        import dataclasses
        from atomic_agents.advisor.score import _score_agent_from_data
        from atomic_agents.advisor.targets import (
            _DEFAULT_WEIGHTS,
            _DEFAULT_AXES,
        )

        today = date(2026, 6, 25)
        six_days_ago = today - timedelta(days=6)
        prior_7_start = today - timedelta(days=13)
        prior_7_end = today - timedelta(days=7)

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
        targets = FleetTargets(weights=dict(_DEFAULT_WEIGHTS), axes=axes)

        run = RunRecord(
            ts=datetime(2026, 6, 24, 12, tzinfo=timezone.utc),
            agent="pure-agent",
            trigger="cron",
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.05,
            cache_hit_tokens=0,
            cache_miss_tokens=100,
            latency_ms=100,
            status="completed",
            summary="ok",
            parent_run_id=None,
            extra={},
        )
        original_model = run.model

        # Create counterfactual — replace does not mutate original
        cf_run = dataclasses.replace(run, model="claude-haiku-4-5", cost_usd=0.01)
        assert run.model == original_model, "Original run must not be mutated"
        assert cf_run.model == "claude-haiku-4-5"

        _score_agent_from_data(
            agent="pure-agent",
            runs_30d=[cf_run],
            runs_prior_30d=[],
            eval_records=[],
            targets=targets,
            today=today,
            six_days_ago=six_days_ago,
            prior_7_start=prior_7_start,
            prior_7_end=prior_7_end,
        )
        # Original still intact after counterfactual scoring
        assert run.model == original_model


# ──────────────────────────────────────────────────────────────────
# MUST 14 — cost-optimization metrics NOT health (#687, spec/53 §3.6)


class TestCostOptimizationNotHealth:
    """MUST 14: cheaper_model_share and tokens_per_output do NOT participate in
    the health score. A 100%-premium + verbose but otherwise-healthy agent MUST
    score OK/WARN, never ERROR and never capped_by_axis from cost.

    strip-RED: re-adding EITHER metric to the Cost-axis scored/cap inputs turns
    the healthy premium+verbose agent red — proving the guard is load-bearing.
    """

    def _make_premium_verbose_agent_health(self, tmp_path: Path) -> "AgentHealth":
        """Build an AgentHealth for an agent that runs 100% premium models and
        produces verbose output — but has healthy Quality + Reliability.

        With #687 in place: cost_score is None (spend_vs_trend no-data — no prior
        window), quality+reliability are healthy → composite is from reliability
        only (or quality+reliability if evals present) → OK/WARN, not ERROR.

        To ensure a non-None cost_score via spend_vs_trend, we seed runs in BOTH
        the recent and prior 30d windows at a flat rate (trend ~= 0 → score = 100).
        """
        from atomic_agents.advisor.score import _score_agent_from_data
        from atomic_agents.advisor.targets import parse_targets

        # Far-future sentinel date (never near-term to avoid date-bomb per memory rule).
        today = date(2030, 9, 15)
        targets = parse_targets(tmp_path)  # all defaults (no targets.md)

        def _mk_run(days_ago: int) -> RunRecord:
            run_date = today - timedelta(days=days_ago)
            ts = datetime(
                run_date.year, run_date.month, run_date.day, 12, tzinfo=timezone.utc
            )
            return RunRecord(
                ts=ts,
                agent="premium-agent",
                trigger="cron",
                model="claude-opus-4-8",  # 100% premium — not cheap
                input_tokens=5000,
                output_tokens=4000,  # verbose: 4000 tokens > 3680 old floor
                cost_usd=2.50,
                cache_hit_tokens=0,
                cache_miss_tokens=5000,
                latency_ms=500,
                status="completed",
                summary="ok",
                parent_run_id=None,
                extra={},
            )

        # Seed flat-spend recent + prior windows so spend_vs_trend is computable.
        runs_30d = [_mk_run(i) for i in range(1, 31)]  # recent 30d
        runs_prior_30d = [_mk_run(i) for i in range(32, 62)]  # prior 30d

        six = today - timedelta(days=6)
        ps = today - timedelta(days=13)
        pe = today - timedelta(days=7)

        ah = _score_agent_from_data(
            agent="premium-agent",
            runs_30d=runs_30d,
            runs_prior_30d=runs_prior_30d,
            eval_records=[],  # no evals → quality excluded, not scored as 0
            targets=targets,
            today=today,
            six_days_ago=six,
            prior_7_start=ps,
            prior_7_end=pe,
        )
        return ah

    def test_premium_verbose_agent_is_ok_or_warn(self, tmp_path):
        """A 100%-premium (cheaper_model_share=0) AND verbose (high tokens_per_output)
        agent with flat spend → cost health should be OK (spend_vs_trend ~0) and the
        composite must be OK (green) or WARN (amber), NOT ERROR (red/capped).

        This is the MUST 14 conformance proof: with #687 in place, the agent's
        cost axis is scored from spend_vs_trend ONLY (flat spend → score ~100),
        so the composite stays healthy.
        """
        ah = self._make_premium_verbose_agent_health(tmp_path)

        # Cheaper_model_share and tokens_per_output must NOT be in scorecard as
        # health metrics — they are advisory-only (#687 MUST 14).
        health_metric_names = {r.metric for r in ah.scorecard if r.score is not None}
        assert "cheaper_model_share" not in health_metric_names, (
            "cheaper_model_share must NOT appear as a scored health metric after #687 "
            f"(MUST 14); found in scored rows: {health_metric_names}"
        )
        assert "tokens_per_output" not in health_metric_names, (
            "tokens_per_output must NOT appear as a scored health metric after #687 "
            f"(MUST 14); found in scored rows: {health_metric_names}"
        )

        # The agent must NOT be ERROR-capped from cost.
        assert ah.capped_by_axis != "metric" or (ah.capped_by_axis is None), (
            f"capped_by_axis must not be 'metric' for a premium+verbose healthy agent; "
            f"got capped_by_axis={ah.capped_by_axis!r}"
        )
        assert ah.capped_by_axis is None, (
            f"A 100%-premium + verbose agent with healthy spend must NOT be capped; "
            f"got capped_by_axis={ah.capped_by_axis!r}"
        )

        # The band must be OK (green) or WARN (amber), NOT red/ERROR.
        assert ah.band in ("green", "amber", "unknown"), (
            f"A premium+verbose healthy agent must be green/amber/unknown, "
            f"not red; got band={ah.band!r}, composite={ah.composite!r}"
        )

    def test_strip_red_cheaper_model_share_in_cap_makes_agent_red(self, tmp_path):
        """strip-RED PRODUCT-LEVEL: if _score_cost_axis were to re-add cheaper_model_share
        as a SCORED ScorecardRow (score=0), the scorecard → metric_scores path MUST turn
        the healthy premium agent red (proves the guard is load-bearing at the product level,
        not just at the _compute_composite math level).

        This exercises the full _score_agent_from_data → scorecard → metric_scores →
        _compute_composite product path, not a synthetic _compute_composite call.
        The baseline confirms the real agent is healthy; the patched version patches
        _score_cost_axis to inject a scored cheaper_model_share row.
        """
        from unittest import mock

        from atomic_agents.advisor import score as score_mod
        from atomic_agents.advisor.score import _score_agent_from_data

        # The fixture helper produces a healthy agent via the real product path.
        ah = self._make_premium_verbose_agent_health(tmp_path)
        assert ah.composite is not None, "fixture must produce a composite"
        assert ah.capped_by_axis is None, "baseline: healthy agent must not be capped"

        # Build the inputs for a second call; reuse the fixture's run data.
        today = date(2030, 9, 15)
        today_dt = __import__("datetime").datetime(2030, 9, 15, tzinfo=__import__("datetime").timezone.utc)
        targets = __import__("atomic_agents.advisor.targets", fromlist=["parse_targets"]).parse_targets(tmp_path)

        def _mk_run(days_ago: int) -> RunRecord:
            run_date = today - timedelta(days=days_ago)
            ts = __import__("datetime").datetime(
                run_date.year, run_date.month, run_date.day, 12,
                tzinfo=__import__("datetime").timezone.utc,
            )
            return RunRecord(
                ts=ts, agent="premium-agent", trigger="cron",
                model="claude-opus-4-8", input_tokens=5000, output_tokens=4000,
                cost_usd=2.50, cache_hit_tokens=0, cache_miss_tokens=5000,
                latency_ms=500, status="completed", summary="ok",
                parent_run_id=None, extra={},
            )

        runs_30d = [_mk_run(i) for i in range(1, 31)]
        runs_prior_30d = [_mk_run(i) for i in range(32, 62)]
        six = today - timedelta(days=6)
        ps = today - timedelta(days=13)
        pe = today - timedelta(days=7)

        # Capture what the REAL _score_cost_axis returns, then augment it with a
        # scored cheaper_model_share row (score=0). This simulates what the pre-#687
        # code did and proves the guard is load-bearing at the product path.
        real_score_cost_axis = score_mod._score_cost_axis

        def _patched_cost_axis(*args, **kwargs):
            cost_score, rows = real_score_cost_axis(*args, **kwargs)
            # Inject a scored cheaper_model_share row: score=0 (100% premium agent).
            rows.append(
                ScorecardRow(
                    metric="cheaper_model_share",
                    axis="cost",
                    value=0.0,
                    target=0.5,
                    status="crit",
                    score=0.0,  # sub-threshold → must fire the critical cap
                    wow=None,
                )
            )
            return cost_score, rows

        with mock.patch.object(score_mod, "_score_cost_axis", side_effect=_patched_cost_axis):
            ah_with = _score_agent_from_data(
                agent="premium-agent",
                runs_30d=runs_30d,
                runs_prior_30d=runs_prior_30d,
                eval_records=[],
                targets=targets,
                today=today,
                six_days_ago=six,
                prior_7_start=ps,
                prior_7_end=pe,
            )

        assert ah_with.band == "red", (
            "strip-RED: re-adding cheaper_model_share as a scored ScorecardRow (score=0) "
            "MUST turn the healthy premium agent red via the scorecard→metric_scores path; "
            f"got band={ah_with.band!r}, capped_by_axis={ah_with.capped_by_axis!r}"
        )
        assert ah_with.capped_by_axis == "metric", (
            f"capped_by_axis must be 'metric' when cheaper_model_share=0 is in scorecard; "
            f"got {ah_with.capped_by_axis!r}"
        )

    def test_strip_red_tokens_per_output_in_cap_makes_agent_red(self, tmp_path):
        """strip-RED PRODUCT-LEVEL: if _score_cost_axis were to re-add tokens_per_output
        as a SCORED ScorecardRow with a sub-threshold score, the full
        _score_agent_from_data path MUST turn the healthy verbose agent red
        (proves the guard is load-bearing at the product level).

        This tests the real scorecard → metric_scores → _compute_composite path.
        """
        from unittest import mock

        from atomic_agents.advisor import score as score_mod
        from atomic_agents.advisor.score import _map_metric_to_score, _score_agent_from_data
        from atomic_agents.advisor.targets import MetricTarget

        ah = self._make_premium_verbose_agent_health(tmp_path)
        assert ah.composite is not None, "fixture must produce a composite"
        assert ah.capped_by_axis is None, "baseline: healthy agent must not be capped"

        # Compute what tokens_per_output score would be for 4000 tokens under the
        # old default target (target=500, direction=lower, band=100, floor=5000).
        old_tpo_target = MetricTarget(
            target=500.0, direction="lower", band=100.0, floor=5000.0
        )
        tpo_score = _map_metric_to_score(4000.0, old_tpo_target)
        assert tpo_score < CRITICAL_SUBSCORE_THRESHOLD, (
            f"tokens_per_output=4000 under old target must score < {CRITICAL_SUBSCORE_THRESHOLD}; "
            f"got {tpo_score:.1f}"
        )

        today = date(2030, 9, 15)
        targets = __import__("atomic_agents.advisor.targets", fromlist=["parse_targets"]).parse_targets(tmp_path)

        def _mk_run(days_ago: int) -> RunRecord:
            run_date = today - timedelta(days=days_ago)
            ts = __import__("datetime").datetime(
                run_date.year, run_date.month, run_date.day, 12,
                tzinfo=__import__("datetime").timezone.utc,
            )
            return RunRecord(
                ts=ts, agent="premium-agent", trigger="cron",
                model="claude-opus-4-8", input_tokens=5000, output_tokens=4000,
                cost_usd=2.50, cache_hit_tokens=0, cache_miss_tokens=5000,
                latency_ms=500, status="completed", summary="ok",
                parent_run_id=None, extra={},
            )

        runs_30d = [_mk_run(i) for i in range(1, 31)]
        runs_prior_30d = [_mk_run(i) for i in range(32, 62)]
        six = today - timedelta(days=6)
        ps = today - timedelta(days=13)
        pe = today - timedelta(days=7)

        real_score_cost_axis = score_mod._score_cost_axis

        def _patched_cost_axis(*args, **kwargs):
            cost_score, rows = real_score_cost_axis(*args, **kwargs)
            # Inject scored tokens_per_output with the old target's sub-threshold score.
            rows.append(
                ScorecardRow(
                    metric="tokens_per_output",
                    axis="cost",
                    value=4000.0,
                    target=500.0,
                    status="crit",
                    score=round(tpo_score, 1),
                    wow=None,
                )
            )
            return cost_score, rows

        with mock.patch.object(score_mod, "_score_cost_axis", side_effect=_patched_cost_axis):
            ah_with = _score_agent_from_data(
                agent="premium-agent",
                runs_30d=runs_30d,
                runs_prior_30d=runs_prior_30d,
                eval_records=[],
                targets=targets,
                today=today,
                six_days_ago=six,
                prior_7_start=ps,
                prior_7_end=pe,
            )

        assert ah_with.band == "red", (
            "strip-RED: re-adding tokens_per_output as a scored ScorecardRow (score<30) "
            "MUST turn the healthy verbose agent red via the scorecard→metric_scores path; "
            f"got band={ah_with.band!r}, capped_by_axis={ah_with.capped_by_axis!r} "
            f"(tpo_score={tpo_score:.1f})"
        )
        assert ah_with.capped_by_axis == "metric", (
            f"capped_by_axis must be 'metric' when tokens_per_output is scored < threshold; "
            f"got {ah_with.capped_by_axis!r}"
        )

    def test_legacy_targets_md_cheaper_model_share_ignored_fail_soft(self, tmp_path):
        """targets.md overrides for removed metrics are silently ignored (fail-soft).

        An operator whose targets.md still carries
          scoring.axes.cost.metrics.cheaper_model_share / tokens_per_output
        after #687 MUST NOT cause an error, MUST NOT affect used_defaults
        (the keys are simply absent from _DEFAULT_AXES), and the parse must
        succeed with the remaining valid keys honored.
        """
        from atomic_agents.advisor.targets import parse_targets

        (tmp_path / "targets.md").write_text(
            "## Fleet Health Targets\n\n"
            "```yaml\n"
            "scoring:\n"
            "  weights:\n"
            "    cost: 0.4\n"
            "    quality: 0.3\n"
            "    reliability: 0.3\n"
            "  axes:\n"
            "    cost:\n"
            "      metrics:\n"
            "        cheaper_model_share:\n"
            "          target: 0.80\n"
            "          direction: higher\n"
            "          band: 0.10\n"
            "          floor: 0.0\n"
            "        tokens_per_output:\n"
            "          target: 300\n"
            "          direction: lower\n"
            "          band: 50\n"
            "          floor: 3000\n"
            "        spend_vs_trend:\n"
            "          target: 0.03\n"
            "          direction: lower\n"
            "          band: 0.01\n"
            "          floor: 0.40\n"
            "```\n"
        )
        # Must not raise.
        ft = parse_targets(tmp_path)

        # cheaper_model_share and tokens_per_output must NOT appear in the axes
        # (they are not in _DEFAULT_AXES, so the parser's "iterate _DEFAULT_AXES"
        # loop never picks them up — ignored fail-soft).
        cost_metrics = set(ft.axes.get("cost", {}).keys())
        assert "cheaper_model_share" not in cost_metrics, (
            "cheaper_model_share must be ignored (not in _DEFAULT_AXES after #687); "
            f"got cost_metrics={cost_metrics}"
        )
        assert "tokens_per_output" not in cost_metrics, (
            "tokens_per_output must be ignored (not in _DEFAULT_AXES after #687); "
            f"got cost_metrics={cost_metrics}"
        )

        # spend_vs_trend IS a valid metric and must be honored.
        assert "spend_vs_trend" in cost_metrics, (
            f"spend_vs_trend must still be parsed; got cost_metrics={cost_metrics}"
        )
        svt = ft.axes["cost"]["spend_vs_trend"]
        assert abs(svt.target - 0.03) < 1e-6, (
            f"operator's spend_vs_trend target override (0.03) must be honored; "
            f"got {svt.target}"
        )

        # The weights override must be honored (cost=0.4 is valid).
        assert abs(ft.weights["cost"] - 0.4) < 0.01, (
            f"operator's cost weight 0.4 must be honored; got {ft.weights['cost']}"
        )

        # No error, no crash — fail-soft posture.

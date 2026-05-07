"""Tests for atomic_agents._costs."""

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from atomic_agents._costs import (
    calc_cost,
    sum_cost_for_period,
    PRICING,
    CACHE_HIT_DISCOUNT,
    _fallback_pricing,
    _unknown_model_warned,
)


def test_calc_cost_known_model():
    # claude-opus-4-7: $15/MTok input, $75/MTok output
    # 1000 input + 500 output, no cache
    cost, fallback = calc_cost("claude-opus-4-7-20260101", 1000, 500)
    expected = 1000 * 15.0 / 1_000_000 + 500 * 75.0 / 1_000_000
    assert abs(cost - expected) < 1e-6
    assert fallback is False


def test_calc_cost_with_cache_hit():
    cost, fallback = calc_cost("claude-opus-4-7-20260101", 1000, 0, cache_hit_tokens=800)
    cache_cost = 800 * 15.0 * CACHE_HIT_DISCOUNT / 1_000_000
    miss_cost = 200 * 15.0 / 1_000_000
    assert abs(cost - (cache_cost + miss_cost)) < 1e-6
    assert fallback is False


def test_calc_cost_unknown_model_returns_zero():
    # Legacy test updated: unknown model now returns fallback (non-zero), not 0.
    cost, fallback = calc_cost("unknown-model-xyz-legacy", 1000, 500)
    assert fallback is True
    assert cost > 0.0  # conservative-pessimistic fallback, never 0


# --- new P2 regression tests ---


def test_calc_cost_unknown_model_returns_fallback_not_zero():
    """Unknown model id must return a positive (fallback) cost, not 0."""
    cost, fallback = calc_cost("claude-new-model-not-in-table", 1_000_000, 1_000_000)
    fb = _fallback_pricing()
    expected = (
        1_000_000 * fb["input"] / 1_000_000
        + 1_000_000 * fb["output"] / 1_000_000
    )
    assert fallback is True
    assert abs(cost - expected) < 1e-4


def test_calc_cost_unknown_model_warns_once(caplog):
    """Warning is emitted only once per unique unknown model id (deduped)."""
    model_id = "claude-future-unicorn-99"
    # Ensure a clean slate for this model in the module-level set.
    _unknown_model_warned.discard(model_id)

    with caplog.at_level(logging.WARNING, logger="atomic_agents._costs"):
        calc_cost(model_id, 100, 100)
        calc_cost(model_id, 200, 200)
        calc_cost(model_id, 300, 300)

    # Exactly one warning for this model id across all three calls.
    warnings = [r for r in caplog.records if model_id in r.message]
    assert len(warnings) == 1


def test_calc_cost_known_model_unchanged():
    """Known model behaviour is unchanged — returns (cost_float, False)."""
    cost, fallback = calc_cost("claude-haiku-4-5-20251001", 500, 250)
    p = PRICING["claude-haiku-4-5-20251001"]
    expected = 500 * p["input"] / 1_000_000 + 250 * p["output"] / 1_000_000
    assert fallback is False
    assert abs(cost - expected) < 1e-9


def test_sum_cost_for_period_today(tmp_path):
    today = date.today()
    log_dir = tmp_path / "log"
    month_dir = log_dir / today.strftime("%Y-%m")
    month_dir.mkdir(parents=True)
    log_path = month_dir / f"{today.isoformat()}.jsonl"
    log_path.write_text(
        json.dumps({"cost_usd": 0.10}) + "\n" +
        json.dumps({"cost_usd": 0.25}) + "\n" +
        json.dumps({"other": "field"}) + "\n"  # missing cost_usd is OK (treated as 0)
    )

    total = sum_cost_for_period(log_dir, "today", today)
    assert abs(total - 0.35) < 1e-6


def test_sum_cost_for_period_no_log_returns_zero(tmp_path):
    today = date.today()
    log_dir = tmp_path / "log"
    total = sum_cost_for_period(log_dir, "today", today)
    assert total == 0.0


def test_sum_cost_handles_malformed_lines(tmp_path):
    today = date.today()
    log_dir = tmp_path / "log"
    month_dir = log_dir / today.strftime("%Y-%m")
    month_dir.mkdir(parents=True)
    log_path = month_dir / f"{today.isoformat()}.jsonl"
    log_path.write_text(
        json.dumps({"cost_usd": 0.10}) + "\n" +
        "not valid json\n" +
        json.dumps({"cost_usd": 0.20}) + "\n"
    )
    total = sum_cost_for_period(log_dir, "today", today)
    assert abs(total - 0.30) < 1e-6


def test_pricing_table_includes_known_models():
    """Sanity check — pricing table has expected entries."""
    assert "claude-opus-4-7-20260101" in PRICING
    assert "claude-sonnet-4-6-20260101" in PRICING
    assert "claude-haiku-4-5-20251001" in PRICING

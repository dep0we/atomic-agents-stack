"""Tests for atomic_agents._costs."""

import json
from datetime import date
from pathlib import Path

import pytest

from atomic_agents._costs import (
    calc_cost,
    sum_cost_for_period,
    PRICING,
    CACHE_HIT_DISCOUNT,
)


def test_calc_cost_known_model():
    # claude-opus-4-7: $15/MTok input, $75/MTok output
    # 1000 input + 500 output, no cache
    cost = calc_cost("claude-opus-4-7-20260101", 1000, 500)
    expected = 1000 * 15.0 / 1_000_000 + 500 * 75.0 / 1_000_000
    assert abs(cost - expected) < 1e-6


def test_calc_cost_with_cache_hit():
    cost = calc_cost("claude-opus-4-7-20260101", 1000, 0, cache_hit_tokens=800)
    cache_cost = 800 * 15.0 * CACHE_HIT_DISCOUNT / 1_000_000
    miss_cost = 200 * 15.0 / 1_000_000
    assert abs(cost - (cache_cost + miss_cost)) < 1e-6


def test_calc_cost_unknown_model_returns_zero():
    assert calc_cost("unknown-model-xyz", 1000, 500) == 0.0


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

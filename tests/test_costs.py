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
    cost, fallback = calc_cost(
        "claude-opus-4-7-20260101", 1000, 0, cache_hit_tokens=800
    )
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
        1_000_000 * fb["input"] / 1_000_000 + 1_000_000 * fb["output"] / 1_000_000
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
        json.dumps({"cost_usd": 0.10})
        + "\n"
        + json.dumps({"cost_usd": 0.25})
        + "\n"
        + json.dumps({"other": "field"})
        + "\n"  # missing cost_usd is OK (treated as 0)
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
        json.dumps({"cost_usd": 0.10})
        + "\n"
        + "not valid json\n"
        + json.dumps({"cost_usd": 0.20})
        + "\n"
    )
    total = sum_cost_for_period(log_dir, "today", today)
    assert abs(total - 0.30) < 1e-6


def test_pricing_table_includes_known_models():
    """Sanity check — pricing table has expected entries."""
    assert "claude-opus-4-7-20260101" in PRICING
    assert "claude-sonnet-4-6-20260101" in PRICING
    assert "claude-haiku-4-5-20251001" in PRICING


# ──────────────────────────────────────────────────────────────────
# #122 — cost_source + mandate_id filters on sum_cost_for_period
# Spec/28 (judge layer) + spec/29 (mandates) + spec/30 (audit) all
# require the ledger to filter by origin and authorizing mandate.


def _write_day(log_dir: Path, day: date, records: list[dict]) -> Path:
    """Test helper — write `records` as JSONL into <log_dir>/<YYYY-MM>/<YYYY-MM-DD>.jsonl."""
    month_dir = log_dir / day.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    log_path = month_dir / f"{day.isoformat()}.jsonl"
    log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return log_path


def test_sum_cost_source_filter_actor_only(tmp_path):
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor"},
            {"cost_usd": 0.05, "cost_source": "judge"},
            {"cost_usd": 0.03, "cost_source": "audit"},
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today, source="actor")
    assert abs(total - 0.10) < 1e-6


def test_sum_cost_source_filter_judge_only(tmp_path):
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor"},
            {"cost_usd": 0.05, "cost_source": "judge"},
            {"cost_usd": 0.03, "cost_source": "audit"},
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today, source="judge")
    assert abs(total - 0.05) < 1e-6


def test_sum_cost_source_filter_audit_only(tmp_path):
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor"},
            {"cost_usd": 0.05, "cost_source": "judge"},
            {"cost_usd": 0.03, "cost_source": "audit"},
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today, source="audit")
    assert abs(total - 0.03) < 1e-6


def test_sum_cost_source_none_sums_everything(tmp_path):
    """source=None preserves legacy behavior — sum every cost record."""
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor"},
            {"cost_usd": 0.05, "cost_source": "judge"},
            {"cost_usd": 0.03, "cost_source": "audit"},
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today)
    assert abs(total - 0.18) < 1e-6


def test_sum_cost_legacy_record_counts_as_actor(tmp_path):
    """Records without a cost_source field are pre-#122 actor spend."""
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10},  # legacy — no cost_source
            {"cost_usd": 0.05, "cost_source": "judge"},
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today, source="actor")
    assert abs(total - 0.10) < 1e-6


def test_sum_cost_legacy_record_not_counted_for_judge(tmp_path):
    """Legacy records (no cost_source) must NOT contribute to judge totals."""
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10},  # legacy
            {"cost_usd": 0.05, "cost_source": "judge"},
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today, source="judge")
    assert abs(total - 0.05) < 1e-6


def test_sum_cost_mandate_id_filter(tmp_path):
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor", "mandate_id": "research-2026"},
            {"cost_usd": 0.05, "cost_source": "actor", "mandate_id": "research-2026"},
            {"cost_usd": 0.20, "cost_source": "actor", "mandate_id": "marketing"},
            {"cost_usd": 0.07, "cost_source": "actor"},  # no mandate
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today, mandate_id="research-2026")
    assert abs(total - 0.15) < 1e-6


def test_sum_cost_mandate_id_none_does_not_filter(tmp_path):
    """mandate_id=None means 'no mandate filter', not 'only records lacking a mandate'."""
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor", "mandate_id": "research-2026"},
            {"cost_usd": 0.07, "cost_source": "actor"},  # no mandate
        ],
    )
    total = sum_cost_for_period(tmp_path, "today", today)
    assert abs(total - 0.17) < 1e-6


def test_sum_cost_source_and_mandate_combined_and(tmp_path):
    """source + mandate_id together AND — both filters must match."""
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor", "mandate_id": "M1"},
            {"cost_usd": 0.05, "cost_source": "judge", "mandate_id": "M1"},
            {"cost_usd": 0.20, "cost_source": "actor", "mandate_id": "M2"},
        ],
    )
    total = sum_cost_for_period(
        tmp_path, "today", today, source="actor", mandate_id="M1"
    )
    assert abs(total - 0.10) < 1e-6


def test_sum_cost_this_month_with_filter(tmp_path):
    """Multi-day this_month aggregation respects the filter."""
    today = date(2026, 5, 12)
    yesterday = date(2026, 5, 11)
    _write_day(
        tmp_path,
        yesterday,
        [
            {"cost_usd": 0.10, "cost_source": "actor"},
            {"cost_usd": 0.04, "cost_source": "judge"},
        ],
    )
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.20, "cost_source": "actor"},
            {"cost_usd": 0.06, "cost_source": "judge"},
        ],
    )
    actor_total = sum_cost_for_period(tmp_path, "this_month", today, source="actor")
    judge_total = sum_cost_for_period(tmp_path, "this_month", today, source="judge")
    assert abs(actor_total - 0.30) < 1e-6
    assert abs(judge_total - 0.10) < 1e-6


def test_sum_cost_filter_on_empty_log_dir_returns_zero(tmp_path):
    """Non-existent log dir with filters set still returns 0.0 (no crash)."""
    today = date.today()
    total = sum_cost_for_period(
        tmp_path / "nonexistent", "today", today, source="judge", mandate_id="M1"
    )
    assert total == 0.0


def test_sum_cost_unknown_cost_source_value_drops_from_strict_filters(tmp_path):
    """Records with unrecognized cost_source (e.g., typo, casing drift) silently
    drop from every strict-source filter but are still summed by source=None.

    This pins current behavior — a future change to warn-or-reject unknown
    sources would update this test deliberately. See #122 review Finding 2.
    """
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 0.10, "cost_source": "actor"},
            {"cost_usd": 0.20, "cost_source": "ACTOR"},  # casing drift
            {"cost_usd": 0.30, "cost_source": "actorr"},  # typo
            {"cost_usd": 0.40, "cost_source": "invented"},
        ],
    )
    # Strict filters match only the canonical lowercase value
    assert (
        abs(sum_cost_for_period(tmp_path, "today", today, source="actor") - 0.10) < 1e-6
    )
    assert sum_cost_for_period(tmp_path, "today", today, source="judge") == 0.0
    assert sum_cost_for_period(tmp_path, "today", today, source="audit") == 0.0
    # source=None still sums everything (no filter applied)
    assert abs(sum_cost_for_period(tmp_path, "today", today) - 1.00) < 1e-6


def test_sum_cost_mixed_sources_in_one_file(tmp_path):
    """A single day's log can carry all three sources; each filter isolates its slice."""
    today = date.today()
    _write_day(
        tmp_path,
        today,
        [
            {"cost_usd": 1.00, "cost_source": "actor"},
            {"cost_usd": 0.50, "cost_source": "actor"},
            {"cost_usd": 0.20, "cost_source": "judge"},
            {"cost_usd": 0.30, "cost_source": "judge"},
            {"cost_usd": 0.10, "cost_source": "audit"},
            {"cost_usd": 0.05},  # legacy — counts as actor
        ],
    )
    assert (
        abs(sum_cost_for_period(tmp_path, "today", today, source="actor") - 1.55) < 1e-6
    )
    assert (
        abs(sum_cost_for_period(tmp_path, "today", today, source="judge") - 0.50) < 1e-6
    )
    assert (
        abs(sum_cost_for_period(tmp_path, "today", today, source="audit") - 0.10) < 1e-6
    )
    assert abs(sum_cost_for_period(tmp_path, "today", today) - 2.15) < 1e-6


# ──────────────────────────────────────────────────────────────────
# GHSA-j659-8xh6-5pq5 — batch reservation cost-cap bypass fix
#
# _estimate_batch_cost must never return 0.0 for an unpriced model.
# Returning 0 causes _check_batch_reservation to early-return (skip),
# which means a parallel batch with an unknown model id bypasses the
# fan-out cost race guard entirely (CWE-770).
#
# Fix: use _fallback_pricing() instead of {} for unknown models,
# mirroring dream.py:_estimate_dream_cost and _costs.calc_cost.


def _build_agent_with_cap(tmp_path, monkeypatch, *, daily_cap_usd: float):
    """Return an AtomicAgent with cost_guardrails enabled and a given daily cap."""
    import sys
    import types
    from pathlib import Path
    from atomic_agents.agent import AtomicAgent

    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "ghsa_test"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# I\nTest.")
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n\n"
        "```yaml\n"
        "cost_guardrails:\n"
        "  enabled: true\n"
        f"  daily_cap_usd: {daily_cap_usd}\n"
        "  monthly_cap_usd: 100.0\n"
        "  daily_cap_action: skip\n"
        "  monthly_cap_action: alert\n"
        "```\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return AtomicAgent(name="ghsa_test", agents_root=agents_root)


def test_estimate_batch_cost_unknown_model_uses_fallback(tmp_path, monkeypatch):
    """GHSA-j659: _estimate_batch_cost for an unpriced model must return > 0.

    Before the fix the function returned 0.0 for any model not in PRICING,
    which caused _check_batch_reservation to skip the reservation entirely.
    """
    agent = _build_agent_with_cap(tmp_path, monkeypatch, daily_cap_usd=10.0)

    # An unknown model id — could be an Ollama/vLLM/self-hosted endpoint.
    unknown_model = "ollama/llama3-8b-not-in-pricing-table"
    estimate = agent._estimate_batch_cost(unknown_model, max_tokens=4000, batch_size=50)

    assert estimate > 0.0, (
        "_estimate_batch_cost returned 0.0 for an unpriced model — "
        "the fallback-pricing fix did not take effect."
    )


def test_estimate_batch_cost_unknown_model_equals_fallback_rate(tmp_path, monkeypatch):
    """GHSA-j659: the estimate for an unknown model must equal the fallback output rate."""
    agent = _build_agent_with_cap(tmp_path, monkeypatch, daily_cap_usd=10.0)

    unknown_model = "self-hosted/custom-7b"
    max_tokens = 4000
    batch_size = 50

    estimate = agent._estimate_batch_cost(unknown_model, max_tokens, batch_size)

    fallback = _fallback_pricing()
    expected = round(fallback["output"] * max_tokens / 1_000_000 * batch_size, 6)
    assert abs(estimate - expected) < 1e-9, (
        f"Expected fallback-rate estimate {expected} but got {estimate}"
    )


def test_estimate_batch_cost_known_model_unchanged(tmp_path, monkeypatch):
    """GHSA-j659: fix must not change the estimate for a known (priced) model."""
    agent = _build_agent_with_cap(tmp_path, monkeypatch, daily_cap_usd=10.0)

    # claude-haiku-4-5-20251001 output rate: $4.0 per 1M tokens
    known_model = "claude-haiku-4-5-20251001"
    max_tokens = 1024
    batch_size = 3

    estimate = agent._estimate_batch_cost(known_model, max_tokens, batch_size)

    p = PRICING[known_model]
    expected = round(p["output"] * max_tokens / 1_000_000 * batch_size, 6)
    assert abs(estimate - expected) < 1e-9, (
        f"Known-model estimate changed after fix: expected {expected}, got {estimate}"
    )


def test_overcap_unknown_model_parallel_batch_raises(tmp_path, monkeypatch):
    """GHSA-j659: an over-cap parallel batch with an unknown model must raise
    CostGuardrailBlocked, not silently proceed.

    Before the fix: estimate=0.0 → reservation skipped → batch ran unchecked.
    After the fix: estimate uses fallback rate → reservation fires → raises.
    """
    from atomic_agents.exceptions import CostGuardrailBlocked
    import sys
    import types
    from unittest.mock import MagicMock, patch

    # Tiny cap (effectively $0 headroom) to guarantee the reservation fires.
    agent = _build_agent_with_cap(tmp_path, monkeypatch, daily_cap_usd=0.000001)

    fake_client = MagicMock()
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    unknown_model = "ollama/llama3-totally-free-model"

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with pytest.raises(CostGuardrailBlocked, match="reservation"):
            agent.helper_call_parallel(
                prompts=["p" + str(i) for i in range(50)],
                model=unknown_model,
                max_tokens=4000,
                max_concurrent=10,
            )

    # No actual LLM calls should have been made.
    assert not fake_client.messages.create.called

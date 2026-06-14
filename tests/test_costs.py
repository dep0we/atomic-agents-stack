"""Tests for atomic_agents._costs."""

import json
import logging
import os
from datetime import date
from pathlib import Path

import pytest

from atomic_agents._costs import (
    CostReadResult,
    calc_cost,
    sum_cost_for_period,
    PRICING,
    CACHE_HIT_DISCOUNT,
    _fallback_pricing,
    _unknown_model_warned,
    _corruption_warned,
)


@pytest.fixture(autouse=True)
def _clear_cost_warn_dedup_sets():
    """Isolate the per-process warn-dedup sets between tests so warning-count
    assertions are not order-dependent under reordering / xdist. (#495 P2)"""
    _corruption_warned.clear()
    _unknown_model_warned.clear()
    yield
    _corruption_warned.clear()
    _unknown_model_warned.clear()


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

    result = sum_cost_for_period(log_dir, "today", today)
    assert abs(result.total_usd - 0.35) < 1e-6
    assert result.degraded is False
    assert result.dropped_records == 0


def test_sum_cost_for_period_no_log_returns_zero(tmp_path):
    today = date.today()
    log_dir = tmp_path / "log"
    result = sum_cost_for_period(log_dir, "today", today)
    assert result.total_usd == 0.0
    assert result.degraded is False
    assert result.dropped_records == 0


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
    # 1 bad line out of 3 non-empty = 33% < 50% threshold → skip+warn+degraded
    result = sum_cost_for_period(log_dir, "today", today)
    assert abs(result.total_usd - 0.30) < 1e-6
    assert result.degraded is True
    assert result.dropped_records == 1


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
    result = sum_cost_for_period(tmp_path, "today", today, source="actor")
    assert abs(result.total_usd - 0.10) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(tmp_path, "today", today, source="judge")
    assert abs(result.total_usd - 0.05) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(tmp_path, "today", today, source="audit")
    assert abs(result.total_usd - 0.03) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(tmp_path, "today", today)
    assert abs(result.total_usd - 0.18) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(tmp_path, "today", today, source="actor")
    assert abs(result.total_usd - 0.10) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(tmp_path, "today", today, source="judge")
    assert abs(result.total_usd - 0.05) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(tmp_path, "today", today, mandate_id="research-2026")
    assert abs(result.total_usd - 0.15) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(tmp_path, "today", today)
    assert abs(result.total_usd - 0.17) < 1e-6
    assert result.degraded is False


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
    result = sum_cost_for_period(
        tmp_path, "today", today, source="actor", mandate_id="M1"
    )
    assert abs(result.total_usd - 0.10) < 1e-6
    assert result.degraded is False


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
    actor_result = sum_cost_for_period(tmp_path, "this_month", today, source="actor")
    judge_result = sum_cost_for_period(tmp_path, "this_month", today, source="judge")
    assert abs(actor_result.total_usd - 0.30) < 1e-6
    assert actor_result.degraded is False
    assert abs(judge_result.total_usd - 0.10) < 1e-6
    assert judge_result.degraded is False


def test_sum_cost_filter_on_empty_log_dir_returns_zero(tmp_path):
    """Non-existent log dir with filters set still returns 0.0 (no crash)."""
    today = date.today()
    result = sum_cost_for_period(
        tmp_path / "nonexistent", "today", today, source="judge", mandate_id="M1"
    )
    assert result.total_usd == 0.0
    assert result.degraded is False


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
        abs(
            sum_cost_for_period(tmp_path, "today", today, source="actor").total_usd
            - 0.10
        )
        < 1e-6
    )
    assert (
        sum_cost_for_period(tmp_path, "today", today, source="judge").total_usd == 0.0
    )
    assert (
        sum_cost_for_period(tmp_path, "today", today, source="audit").total_usd == 0.0
    )
    # source=None still sums everything (no filter applied)
    assert abs(sum_cost_for_period(tmp_path, "today", today).total_usd - 1.00) < 1e-6


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
        abs(
            sum_cost_for_period(tmp_path, "today", today, source="actor").total_usd
            - 1.55
        )
        < 1e-6
    )
    assert (
        abs(
            sum_cost_for_period(tmp_path, "today", today, source="judge").total_usd
            - 0.50
        )
        < 1e-6
    )
    assert (
        abs(
            sum_cost_for_period(tmp_path, "today", today, source="audit").total_usd
            - 0.10
        )
        < 1e-6
    )
    assert abs(sum_cost_for_period(tmp_path, "today", today).total_usd - 2.15) < 1e-6


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


# ──────────────────────────────────────────────────────────────────
# Issue #495 — cost-read fail-closed posture
# Tests for CostReadResult, two-tier fail-closed logic, gate mapping.


class TestCostReadResult:
    """CostReadResult is an internal dataclass; not exported publicly."""

    def test_costreadresult_is_internal_not_in_public_init(self):
        """CostReadResult must NOT appear in atomic_agents.__init__.__all__."""
        import atomic_agents

        assert not hasattr(atomic_agents, "CostReadResult"), (
            "CostReadResult leaked into the public namespace — "
            "remove it from __init__.py"
        )

    def test_costreadresult_fields(self):
        r = CostReadResult(total_usd=1.23, degraded=False, dropped_records=0)
        assert r.total_usd == 1.23
        assert r.degraded is False
        assert r.dropped_records == 0


class TestFilesystemFailClosed:
    """Filesystem reader fail-closed posture (spec/09 §cost-read error posture)."""

    def test_today_oserror_returns_degraded(self, tmp_path, monkeypatch):
        """Whole-file OSError on current-day log → degraded=True, total_usd=0."""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        log_path.write_text(json.dumps({"cost_usd": 0.50}) + "\n")

        # Simulate OSError on read_text (TOCTOU: file exists but then fails)
        original_read_text = Path.read_text

        def patched_read_text(self, *args, **kwargs):
            if self == log_path:
                raise PermissionError("simulated permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", patched_read_text)
        result = sum_cost_for_period(log_dir, "today", today)
        assert result.degraded is True
        assert result.total_usd == 0.0
        assert result.dropped_records == 0

    def test_absent_today_file_not_degraded(self, tmp_path):
        """No log file yet (first run of day) → total=0, degraded=False."""
        today = date.today()
        log_dir = tmp_path / "log"
        result = sum_cost_for_period(log_dir, "today", today)
        assert result.degraded is False
        assert result.total_usd == 0.0

    def test_empty_today_file_not_degraded(self, tmp_path):
        """Empty/whitespace current-day file → total=0, degraded=False (#495 P2).

        A 0-byte or all-blank file is READABLE but has no logged cost yet —
        semantically identical to an ABSENT file, NOT to a corrupt one. The log
        writer's open("a") creates the file before the first write+fsync, so a
        concurrent reader hits a legitimate 0-byte window on every first append
        of the day; failing closed there would spuriously block a legitimate call
        on a normal append race. Genuine blindness is OSError (tested separately).
        """
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        # 0-byte file (the append-race window) AND whitespace-only — both no-cost.
        for content in ("", "   \n\n  \n"):
            log_path.write_text(content)
            result = sum_cost_for_period(log_dir, "today", today)
            assert result.degraded is False, f"empty file must NOT degrade: {content!r}"
            assert result.total_usd == 0.0
            assert result.dropped_records == 0

    def test_per_line_below_threshold_skip_warn_degraded(self, tmp_path):
        """1 bad line out of 3 = 33% < 50% → skip+warn, degraded=True, total correct."""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        log_path.write_text(
            json.dumps({"cost_usd": 0.10})
            + "\n"
            + "NOT JSON\n"
            + json.dumps({"cost_usd": 0.20})
            + "\n"
        )
        result = sum_cost_for_period(log_dir, "today", today)
        assert abs(result.total_usd - 0.30) < 1e-6
        assert result.degraded is True
        assert result.dropped_records == 1

    def test_per_line_above_threshold_fail_closed(self, tmp_path):
        """3 bad lines out of 4 = 75% > 50% → fail-closed, degraded=True, total=0."""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        log_path.write_text(
            json.dumps({"cost_usd": 0.10}) + "\n" + "BAD\n" + "BAD\n" + "BAD\n"
        )
        result = sum_cost_for_period(log_dir, "today", today)
        assert result.degraded is True
        assert result.total_usd == 0.0

    def test_float_error_counted_in_corruption_tally(self, tmp_path):
        """Non-numeric cost_usd is treated as corruption (distinct sub-reason)."""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        log_path.write_text(
            json.dumps({"cost_usd": 0.10})
            + "\n"
            + json.dumps({"cost_usd": "not-a-number"})
            + "\n"
            + json.dumps({"cost_usd": 0.20})
            + "\n"
        )
        # 1 bad out of 3 = 33% < 50% → degraded but partial total
        result = sum_cost_for_period(log_dir, "today", today)
        assert abs(result.total_usd - 0.30) < 1e-6
        assert result.degraded is True
        assert result.dropped_records == 1

    def test_boolean_cost_usd_treated_as_corruption(self, tmp_path):
        """JSON boolean cost_usd must NOT be float()-coerced to $1.00/$0.00.

        float(True) == 1.0 / float(False) == 0.0 would silently count a malformed
        boolean as spend (or hide it). A boolean cost is corruption → dropped +
        degraded, like any other non-numeric cost_usd. (#495 P2)"""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        log_path.write_text(
            json.dumps({"cost_usd": 0.10})
            + "\n"
            + json.dumps({"cost_usd": True})  # would be $1.00 under float(True)
            + "\n"
            + json.dumps({"cost_usd": 0.20})
            + "\n"
        )
        # 1 bad out of 3 = 33% < 50% → degraded, partial total EXCLUDES the bool
        result = sum_cost_for_period(log_dir, "today", today)
        assert abs(result.total_usd - 0.30) < 1e-6  # NOT 1.30
        assert result.degraded is True
        assert result.dropped_records == 1

    def test_this_month_today_oserror_fail_closed(self, tmp_path, monkeypatch):
        """OSError on current-day file within this_month walk → fail-closed."""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        # Historical file with valid data
        hist_day = today.replace(day=max(1, today.day - 1))
        if hist_day != today:
            hist_path = month_dir / f"{hist_day.isoformat()}.jsonl"
            hist_path.write_text(json.dumps({"cost_usd": 0.50}) + "\n")
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        today_path.write_text(json.dumps({"cost_usd": 0.25}) + "\n")

        original_read_text = Path.read_text

        def patched_read_text(self, *args, **kwargs):
            if self == today_path:
                raise PermissionError("simulated permission denied on today's file")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", patched_read_text)
        result = sum_cost_for_period(log_dir, "this_month", today)
        assert result.degraded is True
        assert result.total_usd == 0.0

    def test_this_month_historical_oserror_skips_but_degrades(
        self, tmp_path, monkeypatch
    ):
        """OSError on a historical file in a this_month walk → skip the file but
        flag the read degraded=True (its real cost is silently dropped, so the
        month total is partial → blind → the gate must fail-closed). The whole
        read is NOT zeroed (today's file still sums) — only the current-day file
        zeroes the read. dropped_records stays 0 (a whole-file skip produces no
        per-line drops). degraded is the load-bearing audit signal. (issue #495 P1)"""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)

        # Need at least one historical file (different day) and today's file.
        # Use a fixed past date to avoid ambiguity when today is the 1st.
        hist_day = date(today.year, today.month, 1)
        if hist_day == today:
            # First of month: skip — can't have a past day in the same month.
            pytest.skip("Can't create a different historical day on the 1st of month")

        hist_path = month_dir / f"{hist_day.isoformat()}.jsonl"
        hist_path.write_text(json.dumps({"cost_usd": 0.50}) + "\n")
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        today_path.write_text(json.dumps({"cost_usd": 0.25}) + "\n")

        original_read_text = Path.read_text

        def patched_read_text(self, *args, **kwargs):
            if self == hist_path:
                raise PermissionError("simulated permission denied on historical file")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", patched_read_text)
        result = sum_cost_for_period(log_dir, "this_month", today)
        # Historical file skipped (its $0.50 silently dropped), today's $0.25
        # counted → partial month total that is BLIND for the lost day → degraded
        # so the gate fail-closes. The read is NOT zeroed (today's file sums).
        assert result.degraded is True
        assert abs(result.total_usd - 0.25) < 1e-6
        # Whole-file skip produces no per-line drops; degraded carries the signal.
        assert result.dropped_records == 0

    def test_historical_over_threshold_skips_not_whole_read_fail_closed(self, tmp_path):
        """A majority-corrupt HISTORICAL file skips its own cost + sets degraded,
        but does NOT zero the whole month read — symmetric with historical OSError.
        Only the current-day file fails the whole read closed. (issue #495 P1)"""
        today = date.today()
        if today.day == 1:
            pytest.skip("Can't create a different historical day on the 1st of month")
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        # Historical file: 3 bad out of 4 = 75% > 50%
        hist_day = date(today.year, today.month, 1)
        hist_path = month_dir / f"{hist_day.isoformat()}.jsonl"
        hist_path.write_text(
            json.dumps({"cost_usd": 0.10}) + "\n" + "BAD\n" + "BAD\n" + "BAD\n"
        )
        # Today's file: clean
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        today_path.write_text(json.dumps({"cost_usd": 0.50}) + "\n")

        result = sum_cost_for_period(log_dir, "this_month", today)
        # Historical file's $0.10 dropped (untrustworthy), today's $0.50 kept.
        assert result.degraded is True
        assert abs(result.total_usd - 0.50) < 1e-6
        assert result.dropped_records == 3

    def test_current_day_over_threshold_fails_whole_read_closed(self, tmp_path):
        """A majority-corrupt CURRENT-DAY file in a this_month walk fails the whole
        read closed (degraded=True, total=0.0) even though a historical file is
        clean — the gate cannot trust today's spend. (issue #495 P1)"""
        today = date.today()
        if today.day == 1:
            pytest.skip("Can't create a different historical day on the 1st of month")
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        # Clean historical file
        hist_day = date(today.year, today.month, 1)
        hist_path = month_dir / f"{hist_day.isoformat()}.jsonl"
        hist_path.write_text(json.dumps({"cost_usd": 0.10}) + "\n")
        # Today's file: 3 bad of 4 = 75% > 50%
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        today_path.write_text(
            json.dumps({"cost_usd": 0.50}) + "\n" + "BAD\n" + "BAD\n" + "BAD\n"
        )

        result = sum_cost_for_period(log_dir, "this_month", today)
        assert result.degraded is True
        assert result.total_usd == 0.0

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses POSIX mode bits")
    def test_this_month_unreadable_month_dir_fail_closed(self, tmp_path):
        """this_month with an existing-but-unreadable month dir → degraded=True.
        Path.glob() swallows scandir EACCES and would otherwise fail-OPEN ($0,
        degraded=False). The explicit enumeration probe catches it. (#495 P1)"""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        # Real $5 record that must NOT be reported as $0.
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        today_path.write_text(json.dumps({"cost_usd": 5.00}) + "\n")
        os.chmod(month_dir, 0o000)
        try:
            result = sum_cost_for_period(log_dir, "this_month", today)
        finally:
            os.chmod(month_dir, 0o755)
        assert result.degraded is True
        assert result.total_usd == 0.0

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses POSIX mode bits")
    def test_today_unreadable_month_dir_fail_closed(self, tmp_path):
        """period='today' with an unreadable parent month dir → degraded, not a
        raw PermissionError crash (Path.exists() propagates non-ENOENT OSError on
        Py3.12). (#495 P1)"""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        today_path.write_text(json.dumps({"cost_usd": 5.00}) + "\n")
        os.chmod(month_dir, 0o000)
        try:
            result = sum_cost_for_period(log_dir, "today", today)
        finally:
            os.chmod(month_dir, 0o755)
        assert result.degraded is True
        assert result.total_usd == 0.0

    def test_exact_50_percent_corrupt_is_skip_not_fail_closed(self, tmp_path):
        """Exactly 50% corrupt (2 of 4) must NOT fail-closed — the threshold is
        strict >50%. Surfaces the partial sum + degraded. Locks the strict-
        greater-than boundary against a future '>=' regression. (#495 P2)"""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        # 2 good + 2 bad = exactly 50% → skip+degraded, NOT fail-closed.
        today_path.write_text(
            json.dumps({"cost_usd": 0.10})
            + "\n"
            + "BAD\n"
            + json.dumps({"cost_usd": 0.20})
            + "\n"
            + "BAD\n"
        )
        result = sum_cost_for_period(log_dir, "today", today)
        assert result.degraded is True
        assert abs(result.total_usd - 0.30) < 1e-6
        assert result.dropped_records == 2

    def test_corruption_warning_deduped_per_path_and_reason(self, tmp_path, caplog):
        """Corruption logger.warning deduplicated by (path, sub_reason)."""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        log_path.write_text(
            json.dumps({"cost_usd": 0.10})
            + "\n"
            + "BAD LINE\n"
            + json.dumps({"cost_usd": 0.20})
            + "\n"
        )
        # Clear dedup set so this test starts fresh
        _corruption_warned.discard((str(log_path), "json_decode_error"))

        with caplog.at_level(logging.WARNING, logger="atomic_agents._costs"):
            sum_cost_for_period(log_dir, "today", today)
            sum_cost_for_period(log_dir, "today", today)

        decode_warnings = [
            r
            for r in caplog.records
            if "unparseable" in r.message or "unparseable JSON" in r.message
        ]
        assert len(decode_warnings) == 1, (
            "Expected exactly 1 corruption warning (deduped on 2nd call), "
            f"got {len(decode_warnings)}"
        )

    def test_distinct_sub_reasons_emit_distinct_warnings(self, tmp_path, caplog):
        """json_decode_error and non_numeric_cost_usd emit separate warnings."""
        today = date.today()
        log_dir = tmp_path / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True)
        log_path = month_dir / f"{today.isoformat()}.jsonl"
        log_path.write_text(
            json.dumps({"cost_usd": 0.10})
            + "\n"
            + "NOT JSON\n"
            + json.dumps({"cost_usd": "bad-float"})
            + "\n"
        )
        # Clear both dedup keys
        _corruption_warned.discard((str(log_path), "json_decode_error"))
        _corruption_warned.discard((str(log_path), "non_numeric_cost_usd"))

        with caplog.at_level(logging.WARNING, logger="atomic_agents._costs"):
            sum_cost_for_period(log_dir, "today", today)

        json_warnings = [r for r in caplog.records if "unparseable JSON" in r.message]
        float_warnings = [
            r for r in caplog.records if "non-numeric cost_usd" in r.message
        ]
        assert len(json_warnings) >= 1
        assert len(float_warnings) >= 1


class TestBackendFailClosed:
    """_sum_via_backend fail-closed posture."""

    def test_backend_query_exception_returns_degraded(self, tmp_path):
        """Any exception from backend.query() → degraded=True, fail-closed."""
        from unittest.mock import MagicMock
        from atomic_agents._costs import _sum_via_backend

        bad_backend = MagicMock()
        bad_backend.query.side_effect = RuntimeError("DB unavailable")

        result = _sum_via_backend(bad_backend, date.today(), "today", None, None)
        assert result.degraded is True
        assert result.total_usd == 0.0
        assert result.dropped_records == 0

    def test_sum_cost_for_period_backend_exception_returns_degraded(self, tmp_path):
        """sum_cost_for_period with a failing non-filesystem backend → degraded."""
        from unittest.mock import MagicMock

        bad_backend = MagicMock()
        bad_backend.query.side_effect = OSError("connection reset")
        # Not a FilesystemLogBackend, so routes through _sum_via_backend
        result = sum_cost_for_period(tmp_path / "log", "today", backend=bad_backend)
        assert result.degraded is True
        assert result.total_usd == 0.0

    def test_backend_below_threshold_skip_warn_degraded(self):
        """Backend path: <50% non-numeric records → skip + degraded + partial."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from atomic_agents._costs import _sum_via_backend

        records = [
            SimpleNamespace(cost_usd=0.10, run_id="a"),
            SimpleNamespace(cost_usd=0.20, run_id="b"),
            SimpleNamespace(cost_usd="oops", run_id="c"),  # 1 of 3 cost-bearing
            SimpleNamespace(cost_usd=None, run_id="d"),  # not cost-bearing (ignored)
        ]
        backend = MagicMock()
        backend.query.return_value = iter(records)
        result = _sum_via_backend(backend, date.today(), "today", None, None)
        assert result.degraded is True
        assert abs(result.total_usd - 0.30) < 1e-6
        assert result.dropped_records == 1

    def test_backend_above_threshold_fail_closed(self):
        """Backend path: >50% non-numeric cost-bearing records → fail-closed.
        Denominator is the cost-bearing population (None-cost records excluded),
        intentionally STRICTER than the fs all-non-empty-lines denominator — NOT
        parity. Exercises the defensive-belt drop path via un-coerced
        SimpleNamespace records (a real backend would have coerced these to None
        upstream via from_dict). (#495 P2)"""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from atomic_agents._costs import _sum_via_backend

        records = [
            SimpleNamespace(cost_usd=0.10, run_id="a"),
            SimpleNamespace(cost_usd="bad", run_id="b"),
            SimpleNamespace(cost_usd="bad", run_id="c"),  # 2 of 3 cost-bearing = 66%
            SimpleNamespace(cost_usd=None, run_id="d"),  # excluded from denominator
        ]
        backend = MagicMock()
        backend.query.return_value = iter(records)
        result = _sum_via_backend(backend, date.today(), "today", None, None)
        assert result.degraded is True
        assert result.total_usd == 0.0
        assert result.dropped_records == 2

    def test_backend_boolean_cost_usd_treated_as_corruption(self):
        """Backend path: a boolean cost_usd must be treated as corruption, in
        parity with the fs path (#495 P2). bool is a subclass of int, so
        `total += True` would silently add $1.00 (and False $0.00) — a boolean
        cost must be dropped/degraded, not summed. Unreachable for shipped
        backends (_coerce_optional_float never yields a bool), but the guard keeps
        the two cost-read paths symmetric so spec/09's 'same net result' holds."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from atomic_agents._costs import _sum_via_backend

        records = [
            SimpleNamespace(cost_usd=0.10, run_id="a"),
            SimpleNamespace(cost_usd=True, run_id="b"),  # would be +$1.00 unguarded
            SimpleNamespace(cost_usd=0.20, run_id="c"),  # 1 of 3 cost-bearing = 33%
        ]
        backend = MagicMock()
        backend.query.return_value = iter(records)
        result = _sum_via_backend(backend, date.today(), "today", None, None)
        assert abs(result.total_usd - 0.30) < 1e-6  # NOT 1.30
        assert result.degraded is True
        assert result.dropped_records == 1

    def test_backend_record_attribute_error_fails_closed_not_crash(self):
        """A duck-typed backend record whose cost_usd access raises must be
        counted as a dropped record (degraded), NOT escape as an unhandled
        exception that crashes the gate. The broad except around query() sets the
        expectation that ANY backend misbehavior fail-closes. (#495 P2)"""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from atomic_agents._costs import _sum_via_backend

        class _NoCost:
            run_id = "x"

            @property
            def cost_usd(self):
                raise AttributeError("backend handed back a malformed record")

        records = [
            SimpleNamespace(cost_usd=0.10, run_id="a"),
            _NoCost(),  # cost_usd access raises → dropped, not a crash
            _NoCost(),  # 2 of 3 cost-bearing = 66% → fail-closed
        ]
        backend = MagicMock()
        backend.query.return_value = iter(records)
        # Must not raise; must map to degraded/fail-closed via the >50% threshold.
        result = _sum_via_backend(backend, date.today(), "today", None, None)
        assert result.degraded is True
        assert result.total_usd == 0.0
        assert result.dropped_records == 2


class TestGateSiteMapping:
    """Gate sites map degraded=True → fail-closed (CostGuardrailBlocked or equiv)."""

    def _make_agent(
        self,
        tmp_path,
        monkeypatch,
        *,
        daily_cap_usd: float = 1.0,
        monthly_cap_usd: float = 100.0,
    ):
        from atomic_agents.agent import AtomicAgent

        monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
        agents_root = tmp_path / "agents"
        agent_dir = agents_root / "gate_test"
        (agent_dir / "persona").mkdir(parents=True)
        (agent_dir / "persona" / "IDENTITY.md").write_text("# I\nTest.")
        (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
        (agent_dir / "model.md").write_text(
            "## Default model\nclaude-haiku-4-5-20251001\n\n"
            "```yaml\n"
            "cost_guardrails:\n"
            "  enabled: true\n"
            f"  daily_cap_usd: {daily_cap_usd}\n"
            f"  monthly_cap_usd: {monthly_cap_usd}\n"
            "  daily_cap_action: skip\n"
            "  monthly_cap_action: alert\n"
            "```\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        return AtomicAgent(name="gate_test", agents_root=agents_root)

    def test_check_cost_guardrails_degraded_returns_blocked(
        self, tmp_path, monkeypatch
    ):
        """_check_cost_guardrails with degraded read → allow=False, cost_data_degraded=True."""
        from unittest.mock import patch

        agent = self._make_agent(tmp_path, monkeypatch)
        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )

        with patch(
            "atomic_agents.agent._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            check = agent._check_cost_guardrails(critical=False)

        assert check.allow is False
        assert check.cost_data_degraded is True

    def test_check_cost_guardrails_degraded_uncapped_allows(
        self, tmp_path, monkeypatch
    ):
        """Uncapped (warnings-only) agent + degraded read → allow=True (#495 P2).

        With no daily/monthly cap and no parent tree-cap there is NO budget to be
        blind about — the agent always proceeds even with perfect cost data, so a
        degraded read must NOT spuriously block it. The audit flag is still set
        (cost_data_degraded=True) for honesty even though the call is allowed."""
        from unittest.mock import patch

        agent = self._make_agent(
            tmp_path, monkeypatch, daily_cap_usd=0, monthly_cap_usd=0
        )
        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        with patch(
            "atomic_agents.agent._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            check = agent._check_cost_guardrails(critical=False)

        assert check.allow is True, "uncapped agent must not be blocked by a blind read"
        assert check.cost_data_degraded is True  # audit honesty preserved

    def test_check_cost_guardrails_degraded_uncapped_but_parent_headroom_blocks(
        self, tmp_path, monkeypatch
    ):
        """Uncapped OWN caps but a parent tree-cap (delegate) + degraded → blocked.

        A degraded read DOES matter when the agent is a budget-constrained
        delegate: being blind to its own spend could overrun the parent's
        tree-cap. So the degraded fail-close still fires when
        parent_remaining_headroom_usd is set, even with no own caps. (#495 P2)"""
        from unittest.mock import patch

        agent = self._make_agent(
            tmp_path, monkeypatch, daily_cap_usd=0, monthly_cap_usd=0
        )
        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        with patch(
            "atomic_agents.agent._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            check = agent._check_cost_guardrails(
                critical=False, parent_remaining_headroom_usd=5.0
            )

        assert check.allow is False
        assert check.cost_data_degraded is True

    def test_check_batch_reservation_degraded_uncapped_no_raise(
        self, tmp_path, monkeypatch
    ):
        """Uncapped agent + degraded read → batch reservation does NOT raise
        (#495 P2). Headroom is inf for an uncapped agent, so the reservation can
        never exceed it; blocking on a blind read would be a spurious refusal."""
        from unittest.mock import patch

        agent = self._make_agent(
            tmp_path, monkeypatch, daily_cap_usd=0, monthly_cap_usd=0
        )
        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        with patch(
            "atomic_agents.agent._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            # Must not raise.
            agent._check_batch_reservation(reserved_usd=0.50)

    def test_gate_blocks_on_real_historical_oserror_no_mock(
        self, tmp_path, monkeypatch
    ):
        """End-to-end (no reader mock): an unreadable HISTORICAL daily log makes
        the REAL monthly read degraded, which the gate must map to allow=False.
        This is the exact fail-open #495 P1 closed — before the fix the historical
        OSError skip returned degraded=False and the gate silently under-counted
        the month, passing a call it should have blocked. (issue #495 P1)"""
        today = date.today()
        if today.day == 1:
            pytest.skip("Can't create a different historical day on the 1st of month")

        agent = self._make_agent(tmp_path, monkeypatch)
        log_dir = agent.agent_root / "log"
        month_dir = log_dir / today.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        hist_day = date(today.year, today.month, 1)
        hist_path = month_dir / f"{hist_day.isoformat()}.jsonl"
        hist_path.write_text(json.dumps({"cost_usd": 0.50}) + "\n")
        today_path = month_dir / f"{today.isoformat()}.jsonl"
        today_path.write_text(json.dumps({"cost_usd": 0.10}) + "\n")

        original_read_text = Path.read_text

        def patched_read_text(self, *args, **kwargs):
            if self == hist_path:
                raise PermissionError("simulated EACCES on historical file")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", patched_read_text)

        check = agent._check_cost_guardrails(critical=False)
        # The historical day's $0.50 is silently dropped → blind month total →
        # the gate must fail-closed rather than pass on the partial $0.10.
        assert check.allow is False
        assert check.cost_data_degraded is True

    def test_check_batch_reservation_degraded_raises(self, tmp_path, monkeypatch):
        """_check_batch_reservation with degraded read → CostGuardrailBlocked."""
        from unittest.mock import patch
        from atomic_agents.exceptions import CostGuardrailBlocked

        agent = self._make_agent(tmp_path, monkeypatch)
        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )

        with patch(
            "atomic_agents.agent._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            with pytest.raises(CostGuardrailBlocked, match="unreadable"):
                agent._check_batch_reservation(reserved_usd=0.001)

    def test_check_cost_guardrails_degraded_action_is_skip_not_fallback(
        self, tmp_path, monkeypatch
    ):
        """Degraded fail-closed sets action='skip' (never 'fallback' with no
        fallback_model) — removes the fallback-with-no-model footgun. The
        override holds even when the operator CONFIGURED daily_cap_action=
        'fallback': honoring it on a blind read would still spend on a cheaper
        LLM, a fail-open. Regression-locks the spec/09 invariant. (#495 P2)"""
        from unittest.mock import patch

        agent = self._make_agent(tmp_path, monkeypatch)
        # Operator configured fallback — the degraded path must still skip.
        agent.config.daily_cap_action = "fallback"
        agent.config.monthly_cap_action = "fallback"
        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        with patch(
            "atomic_agents.agent._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            check = agent._check_cost_guardrails(critical=False)
        assert check.allow is False
        assert check.action == "skip"
        assert check.fallback_model is None

    def test_delegation_headroom_degraded_raises(self, tmp_path, monkeypatch):
        """Delegation-headroom gate maps degraded → CostGuardrailBlocked.
        Patches the first _check_cost_guardrails to allow so the test exercises
        the headroom block specifically. (#495 BLOCKING shortcut)"""
        from unittest.mock import patch
        from atomic_agents.exceptions import CostGuardrailBlocked
        from atomic_agents.types import CostCheckResult

        agent = self._make_agent(tmp_path, monkeypatch)
        # Put a peer in the roster so roster membership passes.
        agent.config.roster = {"peer": "peer"}
        agent._enforce_roster_membership = lambda *a, **k: None
        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        with (
            patch.object(
                agent,
                "_check_cost_guardrails",
                return_value=CostCheckResult(allow=True),
            ),
            patch(
                "atomic_agents.agent._costs.sum_cost_for_period",
                return_value=degraded_result,
            ),
        ):
            with pytest.raises(
                CostGuardrailBlocked, match="cannot compute safe headroom"
            ):
                agent.delegate("peer", {"task": "x"})

    def test_delegation_headroom_degraded_uncapped_no_raise(
        self, tmp_path, monkeypatch
    ):
        """Uncapped coordinator + degraded read → delegate() does NOT raise on the
        headroom gate (#495 P2). With no own daily/monthly cap there is no tree-cap
        to pass down, so remaining_headroom stays None and a degraded read changes
        nothing — blocking would be a spurious refusal. Mirrors the uncapped-skip
        proven at _check_cost_guardrails / _check_batch_reservation / dream._check_cap;
        the delegation-headroom site was the one uncapped-skip path with no test.

        Proves execution reached PAST the headroom block (no CostGuardrailBlocked
        from that gate) by sentinelling the immediately-following
        _resolve_delegated_agent_path."""
        from unittest.mock import patch
        from atomic_agents.exceptions import CostGuardrailBlocked
        from atomic_agents.types import CostCheckResult

        agent = self._make_agent(
            tmp_path, monkeypatch, daily_cap_usd=0, monthly_cap_usd=0
        )
        agent.config.roster = {"peer": "peer"}
        agent._enforce_roster_membership = lambda *a, **k: None

        class _ReachedPastHeadroom(Exception):
            pass

        def _sentinel(*_a, **_k):
            raise _ReachedPastHeadroom()

        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        with (
            patch.object(
                agent,
                "_check_cost_guardrails",
                return_value=CostCheckResult(allow=True),
            ),
            patch(
                "atomic_agents.agent._costs.sum_cost_for_period",
                return_value=degraded_result,
            ),
            patch.object(agent, "_resolve_delegated_agent_path", side_effect=_sentinel),
        ):
            # The headroom gate must NOT fire for an uncapped coordinator; the
            # sentinel from the NEXT step proves we passed it without blocking.
            with pytest.raises(_ReachedPastHeadroom):
                agent.delegate("peer", {"task": "x"})

    def test_dream_check_cap_degraded_raises(self, tmp_path, monkeypatch):
        """dream._check_cap maps degraded → ValueError (fail-closed) when
        cost guardrails are ENABLED."""
        from unittest.mock import patch
        from atomic_agents import dream

        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        agent_root = tmp_path / "agent"
        (agent_root / "log").mkdir(parents=True)
        with patch(
            "atomic_agents.dream._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            with pytest.raises(ValueError, match="unreadable"):
                dream._check_cap(
                    agent_root,
                    "claude-haiku-4-5-20251001",
                    0.01,
                    False,
                    log_backend=None,
                    agent_name="dreamer",
                    model_config={
                        "cost_guardrails_enabled": True,
                        "daily_cap_usd": 1.0,
                        "monthly_cap_usd": 10.0,
                    },
                )

    def test_dream_check_cap_degraded_disabled_guardrails_no_raise(
        self, tmp_path, monkeypatch
    ):
        """dream._check_cap must NOT fail-closed on a degraded read when cost
        guardrails are DISABLED — symmetry with agent._check_cost_guardrails,
        which returns allow=True before any cost read for a disabled config
        (spec/09 scopes degraded→fail-closed to guardrails-ENABLED agents).
        Regression guard: pre-fix the degraded check ran before the enabled
        gate, hard-blocking every default (guardrails-off) dream on a single
        corrupt current-day log (#495 R3 P1)."""
        from unittest.mock import patch
        from atomic_agents import dream

        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        agent_root = tmp_path / "agent"
        (agent_root / "log").mkdir(parents=True)
        with patch(
            "atomic_agents.dream._costs.sum_cost_for_period",
            return_value=degraded_result,
        ) as mock_sum:
            # Must return None (no raise) — guardrails disabled.
            result = dream._check_cap(
                agent_root,
                "claude-haiku-4-5-20251001",
                0.01,
                False,
                log_backend=None,
                agent_name="dreamer",
                model_config={"cost_guardrails_enabled": False},
            )
        assert result is None
        # Cost was never read — the enabled gate short-circuits before the read.
        mock_sum.assert_not_called()

    def test_dream_check_cap_degraded_uncapped_no_raise(self, tmp_path, monkeypatch):
        """dream._check_cap must NOT fail-closed on a degraded read for an
        ENABLED-but-uncapped agent (daily_cap==0, monthly_cap==0) — same
        uncapped-skip as agent._check_cost_guardrails. With no cap the dream
        headroom is inf, so a blind read changes nothing; blocking would be a
        spurious refusal. (#495 P2 — the gate that fired before cap resolution)"""
        from unittest.mock import patch
        from atomic_agents import dream

        degraded_result = CostReadResult(
            total_usd=0.0, degraded=True, dropped_records=0
        )
        agent_root = tmp_path / "agent"
        (agent_root / "log").mkdir(parents=True)
        with patch(
            "atomic_agents.dream._costs.sum_cost_for_period",
            return_value=degraded_result,
        ):
            # Enabled but no caps → must not raise on a degraded read.
            result = dream._check_cap(
                agent_root,
                "claude-haiku-4-5-20251001",
                0.01,
                False,
                log_backend=None,
                agent_name="dreamer",
                model_config={
                    "cost_guardrails_enabled": True,
                    "daily_cap_usd": 0.0,
                    "monthly_cap_usd": 0.0,
                },
            )
        assert result is None

    def test_cost_data_degraded_field_on_costcheckresult(self):
        """CostCheckResult.cost_data_degraded defaults False; can be set True."""
        from atomic_agents.types import CostCheckResult

        r_default = CostCheckResult(allow=True)
        assert r_default.cost_data_degraded is False

        r_degraded = CostCheckResult(allow=False, cost_data_degraded=True)
        assert r_degraded.cost_data_degraded is True

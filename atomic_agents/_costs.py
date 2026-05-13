"""Cost calculation + multi-tier guardrails per spec/09-cost-observability.

Pricing table is hardcoded; update when Anthropic/OpenAI/Moonshot change rates.
"""

from __future__ import annotations
import json
import logging
from datetime import date
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Cost-event source categories (spec/28 actor/judge split + spec/30 audit).
# Legacy records (no cost_source field) are treated as "actor" on read.
CostSource = Literal["actor", "judge", "audit"]

# USD per 1M tokens — input / output
PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-opus-4-7-20260101":     {"input": 15.0, "output": 75.0},
    "claude-opus-4-7":              {"input": 15.0, "output": 75.0},  # alias
    "claude-sonnet-4-6-20260101":   {"input": 3.0,  "output": 15.0},
    "claude-sonnet-4-6":            {"input": 3.0,  "output": 15.0},
    "claude-haiku-4-5-20251001":    {"input": 0.80, "output": 4.0},
    "claude-haiku-4-5":             {"input": 0.80, "output": 4.0},
    # OpenAI (placeholder rates; update when published)
    "gpt-5":                         {"input": 5.0,  "output": 20.0},
    "gpt-5-mini":                    {"input": 0.50, "output": 2.0},
    "gpt-5-nano":                    {"input": 0.10, "output": 0.50},
    # Moonshot Kimi (placeholder rates)
    "moonshot/kimi-2.6":             {"input": 0.30, "output": 1.20},
}

# Cache hit pricing — Anthropic charges 10% of input rate for cache hits
CACHE_HIT_DISCOUNT = 0.10

# Module-level set of model ids for which we've already emitted a warning,
# so operators see the message exactly once per process lifetime.
_unknown_model_warned: set[str] = set()


def _fallback_pricing() -> dict[str, float]:
    """Return the most expensive (conservative-pessimistic) rates from PRICING.

    Used when a model id is not in the table so that unknown models are
    over-counted rather than silently treated as free.
    """
    max_input = max(p["input"] for p in PRICING.values())
    max_output = max(p["output"] for p in PRICING.values())
    return {"input": max_input, "output": max_output}


def calc_cost(model: str, input_tokens: int, output_tokens: int,
              cache_hit_tokens: int = 0) -> tuple[float, bool]:
    """Compute USD cost for one LLM call.

    Returns (cost_usd, cost_estimated_via_fallback).

    cost_estimated_via_fallback is True when `model` was not found in the
    PRICING table — the cost is then computed with the highest known rates
    so guardrails and dashboards remain conservative-pessimistic rather than
    zero. A one-time WARNING is logged per unseen model id.

    cache_hit_tokens is the portion of input tokens served from prompt cache;
    they cost 1/10 of the normal input rate. Remainder is normal input price.
    """
    fallback = False
    if model not in PRICING:
        fallback = True
        if model not in _unknown_model_warned:
            _unknown_model_warned.add(model)
            logger.warning(
                "unknown model %r has no pricing entry — cost estimated via "
                "fallback (highest known rates). Add it to PRICING to silence "
                "this warning and get an accurate cost.",
                model,
            )
        p = _fallback_pricing()
    else:
        p = PRICING[model]
    cache_miss_tokens = max(0, input_tokens - cache_hit_tokens)
    cost_cached = cache_hit_tokens * p["input"] * CACHE_HIT_DISCOUNT / 1_000_000
    cost_uncached = cache_miss_tokens * p["input"] / 1_000_000
    cost_output = output_tokens * p["output"] / 1_000_000
    return round(cost_cached + cost_uncached + cost_output, 6), fallback


def sum_cost_for_period(
    log_dir: Path,
    period: str,
    today: date | None = None,
    *,
    source: CostSource | None = None,
    mandate_id: str | None = None,
) -> float:
    """Sum cost_usd across log JSONL for the given period.

    period: 'today' or 'this_month'.

    source: optional filter on cost-event origin (spec/28 + spec/30):
        - None (default): sum every cost record (legacy behavior).
        - "actor": match records with cost_source == "actor" OR missing
          (legacy records pre-date the field and represent actor spend).
        - "judge" / "audit": strict match on cost_source.

    mandate_id: optional filter on mandate authorization (spec/29). When set,
        only records with cost.mandate_id == mandate_id contribute. When None,
        mandate_id is not consulted.

    Filters AND together. Backward-compatible: omitting both kwargs preserves
    the pre-#122 behavior verbatim.
    """
    today = today or date.today()
    total = 0.0
    if period == "today":
        log_path = log_dir / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
        paths = [log_path] if log_path.exists() else []
    elif period == "this_month":
        month_dir = log_dir / today.strftime("%Y-%m")
        paths = list(month_dir.glob("*.jsonl")) if month_dir.exists() else []
    else:
        raise ValueError(f"unknown period: {period}")

    for path in paths:
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
            if source is not None:
                rec_source = rec.get("cost_source", "actor")
                if source == "actor":
                    # legacy records (no cost_source) count as actor
                    if rec_source != "actor":
                        continue
                else:
                    if rec_source != source:
                        continue
            if mandate_id is not None:
                if rec.get("mandate_id") != mandate_id:
                    continue
            try:
                total += float(rec.get("cost_usd", 0.0))
            except (TypeError, ValueError):
                continue
    return total


def load_warning_state(state_path: Path) -> dict:
    """Load the per-agent warning fired-state. Used to make warnings idempotent."""
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_warning_state(state_path: Path, state: dict) -> None:
    """Save the warning fired-state. Atomic write via temp+rename."""
    from ._io import atomic_write
    atomic_write(state_path, json.dumps(state, indent=2))

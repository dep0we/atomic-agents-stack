"""Cost calculation + multi-tier guardrails per spec/09-cost-observability.

Pricing table is hardcoded; update when Anthropic/OpenAI/Moonshot change rates.
"""

from __future__ import annotations
import json
from datetime import date
from pathlib import Path

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


def calc_cost(model: str, input_tokens: int, output_tokens: int,
              cache_hit_tokens: int = 0) -> float:
    """Compute USD cost for one LLM call.

    cache_hit_tokens is the portion of input tokens served from prompt cache;
    they cost 1/10 of the normal input rate. Remainder is normal input price.
    """
    if model not in PRICING:
        # Unknown model — return 0 rather than crashing. Logged elsewhere.
        return 0.0
    p = PRICING[model]
    cache_miss_tokens = max(0, input_tokens - cache_hit_tokens)
    cost_cached = cache_hit_tokens * p["input"] * CACHE_HIT_DISCOUNT / 1_000_000
    cost_uncached = cache_miss_tokens * p["input"] / 1_000_000
    cost_output = output_tokens * p["output"] / 1_000_000
    return round(cost_cached + cost_uncached + cost_output, 6)


def sum_cost_for_period(log_dir: Path, period: str, today: date | None = None) -> float:
    """Sum cost_usd across log JSONL for the given period.

    period: 'today' or 'this_month'.
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
                total += float(rec.get("cost_usd", 0.0))
            except (json.JSONDecodeError, ValueError):
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

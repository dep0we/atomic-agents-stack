"""Cost calculation + multi-tier guardrails per spec/09-cost-observability.

Pricing table is hardcoded; update when Anthropic/OpenAI/Moonshot change rates.
"""

from __future__ import annotations
import json
import logging
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .logs import LogBackend

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
    # Moonshot (placeholder rates — verify against current Moonshot pricing).
    # Both the api.moonshot.ai (dot-style) and api.moonshot.cn (dash-date-style)
    # endpoints expose distinct model identifiers; cost lookup needs entries
    # for whichever an operator selects via `--model`.
    "moonshot/moonshot-v1-128k":     {"input": 0.30, "output": 1.20},
    "moonshot/moonshot-v1-32k":      {"input": 0.30, "output": 1.20},
    "moonshot/moonshot-v1-8k":       {"input": 0.30, "output": 1.20},
    "moonshot/kimi-k2.6":            {"input": 0.30, "output": 1.20},  # thinking; .ai
    "moonshot/kimi-k2.5":            {"input": 0.30, "output": 1.20},  # thinking; .ai
    "moonshot/kimi-k2-0905-preview": {"input": 0.30, "output": 1.20},  # thinking; .cn
    "moonshot/kimi-k2-0711-preview": {"input": 0.30, "output": 1.20},  # thinking; .cn
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
    backend: "LogBackend | None" = None,
    agent_name: str | None = None,
) -> float:
    """Sum cost_usd across log records for the given period.

    period: 'today' or 'this_month'.

    source: optional filter on cost-event origin (spec/28 + spec/30):
        - None (default): sum every cost record (legacy behavior).
        - "actor": match records with cost_source == "actor" OR missing
          (legacy records pre-date the field and represent actor spend).
        - "judge" / "audit": strict match on cost_source.

    mandate_id: optional filter on mandate authorization (spec/29). When set,
        only records with cost.mandate_id == mandate_id contribute. When None,
        mandate_id is not consulted.

    backend: optional ``LogBackend`` (#61 PR 2). When set, the period sum
        is computed via ``backend.query(LogQuery(...))`` — honoring the
        operator's pinned backend (filesystem default; ``SQLiteLogBackend``
        in PR 3; future Postgres/Datadog). When ``None``, falls back to
        the legacy filesystem walk against ``log_dir`` (backward
        compatibility for any external callers + the dashboard layer
        before its readers were rewired).

    Filters AND together. Backward-compatible: omitting both kwargs preserves
    the pre-#122 behavior verbatim. When ``backend`` is provided, the
    function pushes filter predicates into ``LogQuery`` so SQL backends
    can use indexed ``WHERE`` clauses instead of materializing every
    record into the client.
    """
    today = today or date.today()

    # When the backend is the filesystem reference impl, prefer the
    # legacy file-walk semantic (file location implies date, ts content
    # ignored). This preserves the safety-load-bearing cost guardrail
    # behavior for records with malformed or missing ts — which
    # production records shouldn't have, but legacy on-disk records
    # might. SQL/Datadog backends in PR 3+ route through query() where
    # records have indexed ts and the malformed-ts case doesn't apply.
    #
    # Step 11 adversarial P0 #4 caught this: a record with ``ts="x"``
    # in today's JSONL file was counted by legacy sum_cost_for_period
    # but silently dropped by the backend.query() path — a silent
    # loosening of the cost cap. The fix preserves legacy semantic
    # for filesystem while still threading the backend through (so
    # the operator surface is consistent across all backend types).
    if backend is not None:
        from .logs.filesystem import FilesystemLogBackend
        if not isinstance(backend, FilesystemLogBackend):
            return _sum_via_backend(
                backend, today, period, source, mandate_id, agent_name
            )

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


def _sum_via_backend(
    backend: "LogBackend",
    today: date,
    period: str,
    source: CostSource | None,
    mandate_id: str | None,
    agent_name: str | None = None,
) -> float:
    """Sum cost_usd via LogBackend.query (PR 2 backend-routed path).

    Uses ISO-8601 lexicographic comparison via LogQuery.since/until —
    backends with index pushdown (SQLite PR 3 forward) translate this
    to ``WHERE ts >= :since AND ts < :until`` natively. Filesystem
    backend walks month dirs as before.

    ``agent_name`` filter is critical for shared-backend deployments
    (#61 PR 3 review-pass Step 11 P0 #1) — without it, alice's cost
    guardrails sum bob's records too. The filesystem path's
    one-dir-per-agent shape provides this naturally; shared backends
    require the explicit filter.
    """
    from .logs import LogQuery

    if period == "today":
        # Local-tz day boundaries — matches the legacy idiom where
        # ``log_path = log_dir / today.strftime("%Y-%m") / today.isoformat() ``
        # selects records whose filename matches the local date.
        since_dt = datetime.combine(today, time.min).astimezone()
        until_dt = datetime.combine(today, time.max).astimezone()
    elif period == "this_month":
        first_of_month = today.replace(day=1)
        # Next month's first day, then back off to last microsecond.
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        since_dt = datetime.combine(first_of_month, time.min).astimezone()
        until_dt = datetime.combine(next_month, time.min).astimezone()
    else:
        raise ValueError(f"unknown period: {period}")

    records = backend.query(LogQuery(
        since=since_dt,
        until=until_dt,
        cost_source=source,
        mandate_id=mandate_id,
        agent_name=agent_name,
    ))

    total = 0.0
    for r in records:
        if r.cost_usd is None:
            continue
        total += r.cost_usd
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

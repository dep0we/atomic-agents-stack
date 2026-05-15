"""Cost & usage aggregation across Atomic Agents.

Reads `log/YYYY-MM/*.jsonl` for each agent, aggregates by various dimensions,
returns dataclasses ready for render templates.

Pure Python. No LLM calls. Generic — works for any agents-root layout.
"""

from __future__ import annotations
import json
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .._costs import calc_cost, PRICING


@dataclass
class RunRecord:
    """One row from a log JSONL file, normalized."""
    ts: datetime
    agent: str
    trigger: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cache_hit_tokens: int
    cache_miss_tokens: int
    latency_ms: int
    status: str
    summary: str
    fallback: bool = False
    critical: bool = False
    parent_agent: str | None = None    # set for helper trigger
    parent_run_id: str | None = None
    run_id: str | None = None


@dataclass
class AgentSummary:
    """Per-agent aggregate for one period (month or full)."""
    name: str
    runs: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_hit_pct: float
    errors: int
    helper_runs: int
    helper_cost_usd: float
    models_used: list[str] = field(default_factory=list)
    cost_by_model: dict[str, float] = field(default_factory=dict)
    runs_by_model: dict[str, int] = field(default_factory=dict)


@dataclass
class HelperSavings:
    """How much a helper-using agent saved by routing to cheap models."""
    helper_calls: int
    helper_actual_cost: float
    hypothetical_main_cost: float       # if helpers had been the parent's main model
    saved: float
    cost_ratio: float                    # main / helper


@dataclass
class GlobalSummary:
    """Top-level dashboard data for the current month."""
    period_label: str                    # e.g., "May 2026"
    period_start: date
    today: date
    total_cost: float
    total_runs: int
    composite_cache_hit_pct: float
    total_errors: int
    agents: list[AgentSummary]
    top_runs: list[RunRecord]            # top N by cost
    delta_vs_prior_period: dict[str, float]  # cost / runs / errors deltas (pct)
    monthly_trend: list[dict]            # [{month: "2026-05", agent: "x", cost: 1.23}, ...]
    by_model_global: dict[str, float]    # model id → total cost this month
    by_provider: dict[str, float]        # "anthropic"/"openai"/"moonshot"/"local" → total


@dataclass
class AgentDashboardData:
    """Per-agent drilldown data."""
    name: str
    period_label: str
    summary_this_month: AgentSummary
    monthly_trend: list[dict]            # [{month: "2026-05", model: "...", cost: 1.23}, ...]
    daily_costs: dict[str, float]        # {"2026-05-06": 0.15, ...} for current month
    top_runs: list[RunRecord]
    helper_savings: HelperSavings | None
    cache_savings_usd: float             # "you saved this much by caching"
    suggested_caps: dict | None          # if 14+ days of data, suggest daily/monthly caps


# ──────────────────────────────────────────────────────────────────
# Discovery + loading

def discover_agents(agents_root: Path) -> list[str]:
    """Find agent folders under agents_root.

    An "agent folder" has a `log/` subdirectory. Folders prefixed with `_`
    or `.` are excluded (e.g., `_dashboard/`, `.git/`).
    """
    if not agents_root.exists():
        return []
    return sorted(
        d.name for d in agents_root.iterdir()
        if d.is_dir()
        and not d.name.startswith(("_", "."))
        and (d / "log").is_dir()
    )


def load_runs(
    agents_root: Path,
    agent: str,
    since: date,
    until: date | None = None,
) -> list[RunRecord]:
    """Read all log records for one agent in [since, until].

    Per #61 PR 2: routes through ``LogBackend.query()`` instead of
    walking the filesystem directly. Uses ``get_default_log_backend``
    so the dashboard honors the operator's pinned backend (filesystem
    default; ``SQLiteLogBackend`` in PR 3 forward) — same env-var
    resolution path as ``AtomicAgent.__init__``, keeping runtime
    writes and dashboard reads in sync. Falls back silently to an
    empty list when the agent has no log dir / no records yet.

    The returned ``RunRecord`` shape is the dashboard's own dataclass
    (with ``ts: datetime`` and ``agent: str`` required); we adapt the
    spec/22 ``logs.types.RunRecord`` via ``_record_from_dict`` to
    preserve the existing dashboard's reader contract.
    """
    from datetime import datetime, time as dt_time
    from .._platform import get_agents_root
    from ..logs import LogQuery, get_default_log_backend

    until = until or date.today()
    backend = get_default_log_backend(agents_root / agent)
    since_dt = datetime.combine(since, dt_time.min).astimezone()
    until_dt = datetime.combine(until, dt_time.max).astimezone()

    runs: list[RunRecord] = []
    for rec in backend.query(LogQuery(since=since_dt, until=until_dt)):
        rr = _record_from_dict(rec.to_dict(), agent)
        if rr is None:
            continue
        if since <= rr.ts.date() <= until:
            runs.append(rr)
    return runs


def _record_from_dict(rec: dict, agent: str) -> RunRecord | None:
    """Turn a JSONL dict into a RunRecord. Returns None if essential fields missing."""
    ts_str = rec.get("ts")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None
    return RunRecord(
        ts=ts,
        agent=agent,
        trigger=rec.get("trigger", "unknown"),
        model=rec.get("model", "unknown"),
        input_tokens=int(rec.get("input_tokens", 0) or 0),
        output_tokens=int(rec.get("output_tokens", 0) or 0),
        cost_usd=float(rec.get("cost_usd", 0.0) or 0.0),
        cache_hit_tokens=int(rec.get("cache_hit_tokens", 0) or 0),
        cache_miss_tokens=int(rec.get("cache_miss_tokens", 0) or 0),
        latency_ms=int(rec.get("latency_ms", 0) or 0),
        status=str(rec.get("status", "unknown")),
        summary=str(rec.get("summary", ""))[:200],
        fallback=bool(rec.get("fallback", False)),
        critical=bool(rec.get("critical", False)),
        parent_agent=rec.get("parent_agent"),
        parent_run_id=rec.get("parent_run_id"),
        run_id=rec.get("run_id"),
    )


# ──────────────────────────────────────────────────────────────────
# Per-agent aggregation

def summarize_agent(runs: list[RunRecord]) -> AgentSummary:
    """Aggregate one agent's runs over a period."""
    if not runs:
        return AgentSummary(
            name="(empty)", runs=0, cost_usd=0.0,
            input_tokens=0, output_tokens=0,
            cache_hit_pct=0.0, errors=0,
            helper_runs=0, helper_cost_usd=0.0,
        )
    cost_by_model: dict[str, float] = defaultdict(float)
    runs_by_model: dict[str, int] = defaultdict(int)
    cache_hit_total = 0
    cache_total = 0
    errors = 0
    total_cost = 0.0
    in_tok = out_tok = 0
    helper_runs = 0
    helper_cost = 0.0
    for r in runs:
        cost_by_model[r.model] += r.cost_usd
        runs_by_model[r.model] += 1
        cache_hit_total += r.cache_hit_tokens
        cache_total += r.cache_hit_tokens + r.cache_miss_tokens
        if r.status == "error":
            errors += 1
        total_cost += r.cost_usd
        in_tok += r.input_tokens
        out_tok += r.output_tokens
        if r.trigger == "helper":
            helper_runs += 1
            helper_cost += r.cost_usd
    cache_pct = (cache_hit_total / cache_total * 100.0) if cache_total else 0.0
    return AgentSummary(
        name=runs[0].agent,
        runs=len(runs),
        cost_usd=round(total_cost, 6),
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_hit_pct=round(cache_pct, 1),
        errors=errors,
        helper_runs=helper_runs,
        helper_cost_usd=round(helper_cost, 6),
        models_used=sorted(cost_by_model.keys()),
        cost_by_model={k: round(v, 6) for k, v in cost_by_model.items()},
        runs_by_model=dict(runs_by_model),
    )


def helper_savings(runs: list[RunRecord], main_model: str) -> HelperSavings | None:
    """Compute how much helpers saved vs. running their work on the parent's main model.

    Returns None if no helper runs in the period.
    """
    helpers = [r for r in runs if r.trigger == "helper"]
    if not helpers:
        return None
    actual = sum(r.cost_usd for r in helpers)
    hypothetical = sum(
        calc_cost(main_model, r.input_tokens, r.output_tokens)[0] for r in helpers
    )
    saved = max(0.0, hypothetical - actual)
    ratio = (hypothetical / actual) if actual > 0 else 0.0
    return HelperSavings(
        helper_calls=len(helpers),
        helper_actual_cost=round(actual, 6),
        hypothetical_main_cost=round(hypothetical, 6),
        saved=round(saved, 6),
        cost_ratio=round(ratio, 1),
    )


def cache_savings_usd(runs: list[RunRecord]) -> float:
    """How much was saved by cached input vs. uncached at the same model price."""
    saved = 0.0
    for r in runs:
        if r.cache_hit_tokens <= 0:
            continue
        # Saved = full input price - cached input price (10% of input)
        # = cache_hit_tokens * input_price * (1 - 0.10)
        if r.model not in PRICING:
            continue
        rate = PRICING[r.model]["input"]
        saved += r.cache_hit_tokens * rate * 0.9 / 1_000_000
    return round(saved, 6)


def suggest_caps(runs: list[RunRecord]) -> dict | None:
    """Suggest daily and monthly caps based on observed usage.

    Returns None if less than 14 days of data observed.
    """
    if not runs:
        return None
    daily_costs: dict[date, float] = defaultdict(float)
    for r in runs:
        daily_costs[r.ts.date()] += r.cost_usd
    distinct_days = len(daily_costs)
    if distinct_days < 14:
        return None
    avg_daily = sum(daily_costs.values()) / distinct_days
    sorted_costs = sorted(daily_costs.values())
    p95_idx = max(0, int(distinct_days * 0.95) - 1)
    p95_daily = sorted_costs[p95_idx]
    monthly_total = sum(daily_costs.values()) * (30 / distinct_days)
    return {
        "based_on_days": distinct_days,
        "avg_daily": round(avg_daily, 4),
        "p95_daily": round(p95_daily, 4),
        "projected_monthly": round(monthly_total, 4),
        "suggested_daily_cap_usd": round(max(p95_daily * 2.0, avg_daily * 3.0), 2),
        "suggested_monthly_cap_usd": round(monthly_total * 1.5, 2),
    }


# ──────────────────────────────────────────────────────────────────
# Global aggregation

def aggregate_global(
    agents_root: Path,
    today: date | None = None,
    top_runs_count: int = 5,
    monthly_lookback: int = 12,
) -> GlobalSummary:
    """Build the GlobalSummary for the current month."""
    today = today or date.today()
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    agent_names = discover_agents(agents_root)

    summaries: list[AgentSummary] = []
    all_runs_this_month: list[RunRecord] = []
    last_month_total_cost = 0.0

    for agent in agent_names:
        this_month = load_runs(agents_root, agent, month_start, today)
        last_month = load_runs(agents_root, agent, last_month_start, last_month_end)
        if this_month:
            summaries.append(summarize_agent(this_month))
        all_runs_this_month.extend(this_month)
        last_month_total_cost += sum(r.cost_usd for r in last_month)

    total_cost = sum(s.cost_usd for s in summaries)
    total_runs = sum(s.runs for s in summaries)
    total_errors = sum(s.errors for s in summaries)

    cache_hit = sum(r.cache_hit_tokens for r in all_runs_this_month)
    cache_total = sum(r.cache_hit_tokens + r.cache_miss_tokens for r in all_runs_this_month)
    cache_pct = (cache_hit / cache_total * 100.0) if cache_total else 0.0

    top_runs = sorted(all_runs_this_month, key=lambda r: r.cost_usd, reverse=True)[:top_runs_count]

    by_model_global: dict[str, float] = defaultdict(float)
    by_provider: dict[str, float] = defaultdict(float)
    for r in all_runs_this_month:
        by_model_global[r.model] += r.cost_usd
        by_provider[_provider_for(r.model)] += r.cost_usd

    monthly_trend = _build_monthly_trend(agents_root, agent_names, today, monthly_lookback)

    delta_pct = 0.0
    if last_month_total_cost > 0:
        delta_pct = (total_cost - last_month_total_cost) / last_month_total_cost * 100.0

    return GlobalSummary(
        period_label=today.strftime("%B %Y"),
        period_start=month_start,
        today=today,
        total_cost=round(total_cost, 4),
        total_runs=total_runs,
        composite_cache_hit_pct=round(cache_pct, 1),
        total_errors=total_errors,
        agents=summaries,
        top_runs=top_runs,
        delta_vs_prior_period={"cost_pct": round(delta_pct, 1)},
        monthly_trend=monthly_trend,
        by_model_global={k: round(v, 4) for k, v in by_model_global.items()},
        by_provider={k: round(v, 4) for k, v in by_provider.items()},
    )


def aggregate_agent(
    agents_root: Path,
    agent: str,
    today: date | None = None,
    monthly_lookback: int = 12,
    top_runs_count: int = 10,
    main_model: str | None = None,
) -> AgentDashboardData:
    """Per-agent dashboard data."""
    today = today or date.today()
    month_start = today.replace(day=1)

    twelve_months_ago = (month_start - timedelta(days=monthly_lookback * 31)).replace(day=1)
    all_runs = load_runs(agents_root, agent, twelve_months_ago, today)
    this_month_runs = [r for r in all_runs if r.ts.date() >= month_start]

    summary = summarize_agent(this_month_runs) if this_month_runs else summarize_agent([])

    # Pick main model heuristically: most-used non-helper, non-fallback model
    if main_model is None:
        main_runs = [r for r in this_month_runs if r.trigger != "helper"]
        if main_runs:
            counts: dict[str, int] = defaultdict(int)
            for r in main_runs:
                counts[r.model] += 1
            main_model = max(counts, key=counts.get)
        else:
            main_model = "claude-opus-4-7-20260101"

    monthly_trend: dict[tuple[str, str], float] = defaultdict(float)
    for r in all_runs:
        key = (r.ts.strftime("%Y-%m"), r.model)
        monthly_trend[key] += r.cost_usd
    monthly_trend_list = [
        {"month": m, "model": mod, "cost": round(c, 4)}
        for (m, mod), c in sorted(monthly_trend.items())
    ]

    daily_costs: dict[str, float] = defaultdict(float)
    for r in this_month_runs:
        daily_costs[r.ts.date().isoformat()] += r.cost_usd

    top_runs = sorted(this_month_runs, key=lambda r: r.cost_usd, reverse=True)[:top_runs_count]
    h_savings = helper_savings(this_month_runs, main_model)
    cache_saved = cache_savings_usd(this_month_runs)
    caps = suggest_caps(all_runs)

    return AgentDashboardData(
        name=agent,
        period_label=today.strftime("%B %Y"),
        summary_this_month=summary,
        monthly_trend=monthly_trend_list,
        daily_costs={k: round(v, 4) for k, v in sorted(daily_costs.items())},
        top_runs=top_runs,
        helper_savings=h_savings,
        cache_savings_usd=cache_saved,
        suggested_caps=caps,
    )


# ──────────────────────────────────────────────────────────────────
# Helpers

def _provider_for(model_id: str) -> str:
    """Map a model id to its provider for the breakdown chart."""
    if model_id.startswith("claude-"):
        return "anthropic"
    if model_id.startswith("gpt-"):
        return "openai"
    if model_id.startswith("moonshot/"):
        return "moonshot"
    if model_id.startswith("local/") or "qwen" in model_id.lower() or "llama" in model_id.lower():
        return "local"
    return "other"


def _build_monthly_trend(
    agents_root: Path, agent_names: list[str], today: date, lookback: int
) -> list[dict]:
    """Stacked-bar data: one entry per (month, agent, total_cost)."""
    out: list[dict] = []
    cursor = today.replace(day=1)
    for _ in range(lookback):
        month_label = cursor.strftime("%Y-%m")
        month_end = (
            date(cursor.year + 1, 1, 1) - timedelta(days=1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1) - timedelta(days=1)
        )
        for agent in agent_names:
            runs = load_runs(agents_root, agent, cursor, min(month_end, today))
            if runs:
                out.append({
                    "month": month_label,
                    "agent": agent,
                    "cost": round(sum(r.cost_usd for r in runs), 4),
                })
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return list(reversed(out))


# Allow JSON-serializing the dataclasses for pre-aggregated JSON
def to_json_dict(obj: Any) -> Any:
    """Convert dataclass / nested structure to JSON-friendly dict."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_json_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_json_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_dict(x) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    return obj

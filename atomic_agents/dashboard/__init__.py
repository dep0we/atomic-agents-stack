"""Cost & observability dashboard for Atomic Agents.

Per spec/09-cost-observability + implementation/cost-dashboard.

Quick use:

    from atomic_agents.dashboard import render_all
    from pathlib import Path

    render_all(Path.home() / "agents")
    # → writes <agents_root>/_dashboard/index.html + per-agent dashboards

CLI:

    python -m atomic_agents.dashboard render
    python -m atomic_agents.dashboard serve   # optional Flask server

Reads each agent's `log/YYYY-MM/*.jsonl` files, aggregates by
(agent, model, day, month), and renders self-contained HTML.

Aggregation is pure Python — no LLM calls, no external services.
"""

from .costs import (
    RunRecord,
    AgentSummary,
    GlobalSummary,
    discover_agents,
    load_runs,
    summarize_agent,
    aggregate_global,
    aggregate_agent,
    helper_savings,
)
from .render import render_all, render_global, render_agent

__all__ = [
    "RunRecord",
    "AgentSummary",
    "GlobalSummary",
    "discover_agents",
    "load_runs",
    "summarize_agent",
    "aggregate_global",
    "aggregate_agent",
    "helper_savings",
    "render_all",
    "render_global",
    "render_agent",
]

"""Cost & observability dashboard for Atomic Agents.

Per spec/09-cost-observability + implementation/cost-dashboard.

Quick use:

    from atomic_agents.dashboard import render_all
    from pathlib import Path

    render_all(Path.home() / "agents")
    # → writes <agents_root>/_dashboard/index.html + activity/quality/memory/goals

CLI:

    python -m atomic_agents.dashboard render
    python -m atomic_agents.dashboard render --tab activity
    python -m atomic_agents.dashboard serve   # optional local server

Reads each agent's `log/YYYY-MM/*.jsonl` files, aggregates by
(agent, model, day, month), and renders self-contained HTML.

Aggregation is pure Python — no LLM calls, no external services.
"""

from .costs import (
    RunRecord,
    AgentSummary,
    GlobalSummary,
    WorkflowSummary,
    discover_agents,
    load_runs,
    summarize_agent,
    aggregate_global,
    aggregate_agent,
    aggregate_workflow,
    helper_savings,
)
from .render import render_all, render_global, render_agent
from .activity import aggregate_activity, render_activity
from .quality import aggregate_quality, render_quality
from .memory import aggregate_memory, render_memory
from .goals import aggregate_goals, render_goals, has_any_goal

__all__ = [
    "RunRecord",
    "AgentSummary",
    "GlobalSummary",
    "WorkflowSummary",
    "discover_agents",
    "load_runs",
    "summarize_agent",
    "aggregate_global",
    "aggregate_agent",
    "aggregate_workflow",
    "helper_savings",
    "render_all",
    "render_global",
    "render_agent",
    "aggregate_activity",
    "render_activity",
    "aggregate_quality",
    "render_quality",
    "aggregate_memory",
    "render_memory",
    "aggregate_goals",
    "render_goals",
    "has_any_goal",
]

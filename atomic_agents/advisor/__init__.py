"""Fleet Health Scoring Engine (spec/53).

Pure-compute module — ZERO new LLM spend (no LLMBackend constructed, no LLM call).

Import discipline (source-level — see score.py for the full NOTE):
- The advisor's OWN code reads only from atomic_agents.dashboard.costs
  (_load_runs_with_degraded / discover_agents / RunRecord),
  atomic_agents.dashboard._reliability, atomic_agents._costs.PRICING, .targets,
  and a direct evals/runs/*.jsonl file reader.
- It NEVER DIRECTLY imports agent.py, eval.py, tuning.py, or dream.py (though the
  package __init__ loads them transitively — the guarantee is no-LLM-spend, not
  sys.modules isolation).
- The conftest guard in tests/advisor/ enforces the no-LLM-construction guarantee
  at test time.
"""

from .score import compute_fleet_health, FleetHealth, AgentHealth, ScorecardRow
from .targets import parse_targets

__all__ = [
    "compute_fleet_health",
    "FleetHealth",
    "AgentHealth",
    "ScorecardRow",
    "parse_targets",
]

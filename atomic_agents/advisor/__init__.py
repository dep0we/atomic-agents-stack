"""Fleet Health Scoring + Recommendations Engine (spec/53 + spec/54).

Pure-compute module — ZERO new LLM spend (no LLMBackend constructed, no LLM call).

Import discipline (source-level — see score.py and recommend.py for the full NOTE):
- The advisor's OWN code reads only from atomic_agents.dashboard.costs
  (_load_runs_with_degraded / discover_agents / RunRecord),
  atomic_agents.dashboard._reliability, atomic_agents.core_api.get_model_rates,
  .targets, and direct JSONL/frontmatter file readers.
- It NEVER DIRECTLY imports agent.py, eval.py, tuning.py, or dream.py (though the
  package __init__ loads them transitively — the guarantee is no-LLM-spend, not
  sys.modules isolation).
- The conftest guard in tests/advisor/ enforces the no-LLM-construction guarantee
  at test time.
"""

# NOTE: the bare `recommend` FUNCTION is intentionally NOT re-exported at the
# package level. Binding `atomic_agents.advisor.recommend` to the function would
# shadow the `recommend` SUBMODULE on the package object, so
# `import atomic_agents.advisor.recommend as r; r.recommend_fleet()` would fail
# with AttributeError — a surprising footgun for the standard module-import idiom.
# Callers that want the pure core import it from the submodule directly:
# `from atomic_agents.advisor.recommend import recommend`. The fleet loader,
# dataclasses, and kinds frozenset are safe to re-export (no name collision).
from .recommend import (
    EvalHeadroom,
    Recommendation,
    RECOMMENDATION_KINDS,
    recommend_fleet,
)
from .score import compute_fleet_health, FleetHealth, AgentHealth, ScorecardRow
from .targets import parse_targets, parse_recommendations, RecommendationConfig

__all__ = [
    # spec/53 — scoring
    "compute_fleet_health",
    "FleetHealth",
    "AgentHealth",
    "ScorecardRow",
    "parse_targets",
    # spec/54 — recommendations
    # NOTE: the `recommend` function is NOT exported here (it would shadow the
    # submodule — see the import NOTE above); import it from the submodule.
    "EvalHeadroom",
    "Recommendation",
    "RECOMMENDATION_KINDS",
    "recommend_fleet",
    "parse_recommendations",
    "RecommendationConfig",
]

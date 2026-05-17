"""Per-agent named cost estimator registry (spec/29 §"Validation steps" step 8).

The registry maps string IDs → callables that project an action's external
cost (real-money) from its ``tool_arguments`` dict. The framework invokes
the named estimator at ``MandateCheck`` step 8 evaluation time to project
the upcoming action's external cost against the mandate's
``*_external_usd`` cap budgets.

This module mirrors ``target_extractor_registry.py`` exactly — same
shape, same per-agent isolation, same loud-early-failure discipline.
Lands ahead of #124 PR 3b per plan-subagent Risk D so the spec/29
amendment (`cost_estimator_id` replaces the originally-spec'd
`cost_estimator: Callable` field that can't satisfy spec/25 MUST #4 Tier B
round-trip) has a working implementation when PR 3b body lands.

Design notes:
- Registry lives on ``AtomicAgent._cost_estimators`` (in-memory, NOT
  persisted on ``AgentProfile`` snapshot — ``Callable`` values cannot
  satisfy spec/25 MUST #4 Tier B lossless round-trip for structured-storage
  backends).
- Per-agent scoping mirrors spec/25 Decision 9 + spec/15 delegate isolation:
  coordinator-registered estimators do NOT leak into delegate evaluations.
  A delegate constructs its own ``CostEstimatorRegistry`` fresh.
- **No built-in estimators ship by default** — unlike target extractors
  (where heuristic field-name fallback is sensible for the common case),
  external-cost projection is so tool-specific that "guess from arg shape"
  is the wrong default. Tools with dynamic pricing register an explicit
  estimator at agent setup; tools with static pricing use
  ``ToolDefinition.expected_external_cost_usd``; mandates with
  ``*_external_usd`` caps over tools with neither registered → fail-closed
  BLOCK with reason ``mandate_external_cost_unprojectable`` per spec/29.
- Validation happens at ``tool_registry.register()`` time (per spec/29
  §"Registration order discipline"): when a tool's
  ``ToolDefinition.cost_estimator_id`` references an unregistered name,
  ``UnknownCostEstimator`` is raised immediately. Surfaces operator
  misconfiguration BEFORE the first mandate-citing action attempt
  (loud early failure per plan-subagent Risk D).

See also:
    ``atomic_agents.tools.ToolDefinition.cost_estimator_id`` —
        the per-tool optional string that names which registered estimator
        to call (PR 3b adds this field).
    ``atomic_agents.judge.target_extractor_registry`` —
        sibling registry for ``target_canonical`` extraction; same shape.
    ``docs/spec/29-mandates.md`` §"Validation steps" step 8 —
        the contract this registry implements.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..exceptions import AtomicAgentsError

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Exception


class UnknownCostEstimator(AtomicAgentsError):
    """Raised when a ``ToolDefinition.cost_estimator_id`` references an
    unregistered name in the agent's ``CostEstimatorRegistry``.

    Loud failure at ``tool_registry.register()`` time per spec/29
    §"Validation steps" step 8 + §"Registration order discipline" —
    surfaces operator misconfiguration BEFORE first mandate-citing
    dispatch. Per plan-subagent PR 3b Risk D, this mirrors the same
    fail-loud discipline ``UnknownTargetExtractor`` uses for the
    target_extractor registry.
    """


# ──────────────────────────────────────────────────────────────────
# Registry


class CostEstimatorRegistry:
    """Per-agent named cost estimator registry.

    Empty at construction. Operators register tool-specific estimators
    via ``agent.register_cost_estimator(name, callable)`` (the public
    API on ``AtomicAgent``). The framework resolves the name at
    ``MandateCheck`` step 8 evaluation time when a tool's
    ``ToolDefinition.cost_estimator_id`` is set.

    Per-agent scoping (spec/29 + spec/15 delegate isolation): each
    agent instance holds its own registry; coordinator-registered
    estimators do NOT flow to delegates.

    Estimator signature: ``Callable[[dict], float]`` — takes the tool's
    ``tool_arguments`` dict, returns a projected USD external cost.
    Should return ``float('inf')`` when the estimator cannot project for
    the given arguments (caller treats as ``mandate_external_cost_unprojectable``).
    Should NOT raise — exceptions are caught + logged + treated as
    ``+inf`` (fail-closed per spec/29 line 380).
    """

    def __init__(self) -> None:
        self._registry: dict[str, Callable[[dict], float]] = {}

    def register(
        self,
        name: str,
        estimator: Callable[[dict], float],
    ) -> None:
        """Register a named cost estimator.

        Raises ``ValueError`` on collision (use ``replace()`` for explicit
        overwrite path). Name MUST be lowercase alphanumeric plus
        underscore (greppable; matches ``target_extractor_registry``
        convention).
        """
        if name in self._registry:
            raise ValueError(
                f"cost_estimator name {name!r} is already registered. "
                f"Use replace() to overwrite explicitly."
            )
        if not name or not all(c.isalnum() or c == "_" for c in name):
            raise ValueError(
                f"cost_estimator name {name!r} must be lowercase alphanumeric "
                f"plus underscore"
            )
        self._registry[name] = estimator

    def replace(
        self,
        name: str,
        estimator: Callable[[dict], float],
    ) -> None:
        """Replace a registered estimator (explicit overwrite path)."""
        self._registry[name] = estimator

    def has(self, name: str) -> bool:
        """Check whether ``name`` is registered."""
        return name in self._registry

    def estimate(
        self,
        tool_name: str,
        tool_arguments: dict,
        estimator_id: str | None = None,
    ) -> float:
        """Project the external cost for an action.

        If ``estimator_id`` is provided, MUST be registered. Returns
        ``float('inf')`` when:
        - ``estimator_id`` is None (no projection available)
        - The estimator raises an exception (logged; fail-closed per
          spec/29 line 380)
        - The estimator returns a non-numeric value (defense against
          mis-implementation)

        Spec/29 §"Validation steps" step 8: the caller (``MandateCheck``)
        converts ``+inf`` projection into ``BLOCK`` with reason
        ``mandate_external_cost_unprojectable``.

        Raises ``UnknownCostEstimator`` when ``estimator_id`` is provided
        but not registered (loud failure preferred over silent fail-closed
        for the misconfiguration case — surfaces the bug to the operator).
        """
        if estimator_id is None:
            # No estimator → caller fails-closed via the +inf path
            return float("inf")

        if estimator_id not in self._registry:
            raise UnknownCostEstimator(
                f"cost_estimator_id {estimator_id!r} not registered. "
                f"Tool {tool_name!r} references an unknown estimator. "
                f"Register via agent.register_cost_estimator(name, callable)."
            )

        try:
            projected = self._registry[estimator_id](tool_arguments)
        except Exception as exc:  # noqa: BLE001 — intentional broad catch
            _logger.warning(
                "cost_estimator %r raised %s for tool %r: %s "
                "— treating as unprojectable (fail-closed)",
                estimator_id,
                type(exc).__name__,
                tool_name,
                exc,
            )
            return float("inf")

        if not isinstance(projected, (int, float)):
            _logger.warning(
                "cost_estimator %r for tool %r returned non-numeric "
                "%r (type %s) — treating as unprojectable (fail-closed)",
                estimator_id,
                tool_name,
                projected,
                type(projected).__name__,
            )
            return float("inf")

        return float(projected)

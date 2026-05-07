"""Atomic Agents — vault-native AI agent framework.

Per the spec at <agents_root>/Atomic Agents/. Implements the core runtime:
agent loading + helper-mediated captures + cost guardrails + parallel helpers.

Quick start (from a job script):

    from atomic_agents import AtomicAgent

    agent = AtomicAgent(name="caldwell", trigger="cron")
    response = agent.call(work_item="Daily morning brief")

The agent reads everything it needs from `<agents_root>/caldwell/` (persona,
tools.md, model.md, memory, journal). Cost is enforced per `model.md`'s
`cost_guardrails` block. Captures emitted by the model are written
helper-mediated (atomic, locked, validated). Logs land in `log/YYYY-MM/`.
"""

from .agent import AtomicAgent
from .exceptions import (
    AtomicAgentsError,
    CostGuardrailBlocked,
    AgentLockBusy,
    SchemaValidationError,
    NoJudgeAvailable,
    HelperBatchPartialFailure,
    WritePathViolation,
    NotInRoster,
    SelfDelegationError,
    DreamInProgress,
    DreamNotFound,
)
from .types import Capture, Response, AgentConfig, CostCheckResult
from .outcome import OutcomeRunner, OutcomeResult, IterationRecord
from .dream import (
    DreamRunner,
    DreamResult,
    DreamInputs,
    ConsolidatedNote,
    PromotedNote,
    StaleMarking,
)

__version__ = "0.1.0"

__all__ = [
    "AtomicAgent",
    "AtomicAgentsError",
    "CostGuardrailBlocked",
    "AgentLockBusy",
    "SchemaValidationError",
    "NoJudgeAvailable",
    "HelperBatchPartialFailure",
    "WritePathViolation",
    "NotInRoster",
    "SelfDelegationError",
    "DreamInProgress",
    "DreamNotFound",
    "Capture",
    "Response",
    "AgentConfig",
    "CostCheckResult",
    "OutcomeRunner",
    "OutcomeResult",
    "IterationRecord",
    "DreamRunner",
    "DreamResult",
    "DreamInputs",
    "ConsolidatedNote",
    "PromotedNote",
    "StaleMarking",
]

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
    ToolNotRegistered,
    ToolInputInvalid,
    ToolHandlerError,
    SkillFileTraversal,
)
from .tools import ToolDefinition, ToolRegistry, ToolCallResult
from .skills import SkillManifest, discover_skills, load_skill_body, load_skill_referenced_file
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
from .llm import (
    SyncLLMBackend,
    LLMToolDefinition,
    LLMToolUse,
    LLMToolResult,
    CacheDirective,
    LLMCapabilities,
    PricingInfo,
    register_llm_backend,
    unregister_llm_backend,
    get_backend,
    iter_registered_backends,
    find_backend_for_model,
)
from .judge import (
    # Protocol contract + outcome model
    JudgeBackend,
    JudgmentOutcome,
    Judgment,
    JudgmentContext,
    # Canonical proposal types
    ActionProposal,
    ProposalAmendment,
    # Enums most commonly consumed at top-level
    ActionClass,
    # Aliased registry primitives (avoid top-level name collisions
    # with ``atomic_agents.llm.get_backend`` and a hypothetical
    # ``atomic_agents.memory.register_backend``)
    register_judge_backend,
    get_judge_backend,
    unregister_judge_backend,
)
from .exceptions import (
    JudgeError,
    JudgeUnavailable,
    JudgePolicyInvalid,
    JudgeBudgetExhausted,
    JudgeProposalInvalid,
    JudgeAmendedProposalRejected,
    UnknownJudgeBackendError,
)

__version__ = "0.13.0"

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
    "ToolNotRegistered",
    "ToolInputInvalid",
    "ToolHandlerError",
    "SkillFileTraversal",
    "ToolDefinition",
    "ToolRegistry",
    "ToolCallResult",
    "SkillManifest",
    "discover_skills",
    "load_skill_body",
    "load_skill_referenced_file",
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
    # LLMBackend Protocol surface (spec/31, shipped v0.13.0)
    "SyncLLMBackend",
    "LLMToolDefinition",
    "LLMToolUse",
    "LLMToolResult",
    "CacheDirective",
    "LLMCapabilities",
    "PricingInfo",
    "register_llm_backend",
    "unregister_llm_backend",
    "get_backend",
    "iter_registered_backends",
    "find_backend_for_model",
    # JudgeBackend Protocol surface (spec/28 — #112 PR 1 scaffolding;
    # reference implementations + agent.call() wiring land in PR 2)
    "JudgeBackend",
    "JudgmentOutcome",
    "Judgment",
    "JudgmentContext",
    "ActionProposal",
    "ProposalAmendment",
    "ActionClass",
    "register_judge_backend",
    "get_judge_backend",
    "unregister_judge_backend",
    "JudgeError",
    "JudgeUnavailable",
    "JudgePolicyInvalid",
    "JudgeBudgetExhausted",
    "JudgeProposalInvalid",
    "JudgeAmendedProposalRejected",
    "UnknownJudgeBackendError",
]

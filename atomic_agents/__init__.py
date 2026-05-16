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
    LockBusy,
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
from .skills import (
    SkillManifest,
    discover_skills,
    load_skill_body,
    load_skill_referenced_file,
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
from .locks import (
    LockBackend,
    LockHandle,
    LockCapabilities,
    FilesystemLockBackend,
    register_lock_backend,
    unregister_lock_backend,
    get_lock_backend,
    list_lock_backends,
)
from .profile import (
    AgentProfileBackend,
    AgentProfile,
    ProfileSnapshot,
    ProfileCapabilities,
    AGENT_MODE_REACTIVE,
    AGENT_MODE_GOAL_DRIVEN,
    AGENT_MODE_HYBRID,
    FilesystemAgentProfileBackend,
    register_profile_backend,
    unregister_profile_backend,
    get_profile_backend,
    list_profile_backends,
    get_default_profile_backend,
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
    AgentProfileNotFound,
    AgentProfileExists,
    SnapshotNotFound,
)

__version__ = "0.13.0"

__all__ = [
    "AtomicAgent",
    "AtomicAgentsError",
    "CostGuardrailBlocked",
    "AgentLockBusy",
    "LockBusy",
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
    # LockBackend Protocol surface (spec/21 — #60 PR 1 scaffolding;
    # call-site wiring + deprecation shim for _locks.AgentLock land in PR 2)
    "LockBackend",
    "LockHandle",
    "LockCapabilities",
    "FilesystemLockBackend",
    "register_lock_backend",
    "unregister_lock_backend",
    "get_lock_backend",
    "list_lock_backends",
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
    # AgentProfileBackend Protocol surface (spec/24 — #63 PR 1 scaffolding;
    # AtomicAgent.__init__ + _load_config() wiring + doctor.check_agent_profile_backend
    # land in PR 2)
    "AgentProfileBackend",
    "AgentProfile",
    "ProfileSnapshot",
    "ProfileCapabilities",
    "AGENT_MODE_REACTIVE",
    "AGENT_MODE_GOAL_DRIVEN",
    "AGENT_MODE_HYBRID",
    "FilesystemAgentProfileBackend",
    "register_profile_backend",
    "unregister_profile_backend",
    "get_profile_backend",
    "list_profile_backends",
    "get_default_profile_backend",
    "AgentProfileNotFound",
    "AgentProfileExists",
    "SnapshotNotFound",
]

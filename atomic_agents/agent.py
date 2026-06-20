"""AtomicAgent class — the main runtime per spec/04.

Loads persona/tools/model/memory/journal in canonical order, calls the LLM,
extracts captures, writes them helper-mediated, logs the run.

Usage:

    from atomic_agents import AtomicAgent

    agent = AtomicAgent(name="caldwell", trigger="cron")
    response = agent.call(work_item="Daily morning brief")
    print(response.text)
"""

from __future__ import annotations
import concurrent.futures
import logging
import os
import re
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

if TYPE_CHECKING:
    from .llm.types import LLMToolDefinition  # noqa: F401 — used in str type hints
    from .judge.mandate_reservations import MandateReservationManager
    from .judge.types import ActionProposal

_logger = logging.getLogger(__name__)

import frontmatter

from . import _capture, _cascade, _costs, _llm, tracing as _tracing
from ._io import safe_resolve_under
from .memory.backend import MemoryBackend, WritePolicy
from .memory import get_default_memory_backend
from .mcp import MCPClientPool, MCPCommandNotAllowed, parse_mcp_allowed_commands
from .locks import (
    LockBackend,
    LockBusy,
    check_lock_lost,
    get_default_lock_backend,
)
from .logs import (
    LogBackend,
    RunRecord,
    get_default_log_backend,
)
from .profile import (
    AgentProfile,
    AgentProfileBackend,
    get_default_profile_backend,
)
from .registry import (
    ToolRegistryBackend,
    get_default_tool_registry_backend,
)
from .mandate import (
    MandateBackend,
    get_default_mandate_backend,
)
from .policy import (
    PolicyBackend,
    get_default_policy_backend,
)
from .persona import (
    PersonaBackend,
    get_default_persona_backend,
)
from .corpus import (
    CorpusBackend,
    get_default_corpus_backend,
)

# JournalBackend imported at module level (mirrors CorpusBackend above) so the
# `journal_backend: "JournalBackend | None"` annotation on __init__ resolves under
# typing.get_type_hints() and for mypy/pyright. journal.backend has no heavy deps
# (only .types + stdlib), so this is circular-import-safe. (#427 PR1)
from .journal.backend import JournalBackend

# GoalBackend imported at module level (mirrors JournalBackend above) so the
# `goal_backend: GoalBackend | None` annotation on __init__ resolves under
# typing.get_type_hints() and for mypy/pyright. goal.backend imports only from
# goal/types.py + stdlib — no heavy deps, no circular-import risk. (#448 PR1)
from .goal.backend import GoalBackend
from .idempotency.backend import IdempotencyBackend
from .idempotency import (
    COMPLETED as _DEDUP_COMPLETED,
    IN_FLIGHT as _DEDUP_IN_FLIGHT,
    get_default_idempotency_backend,
)
from .idempotency.types import DedupDecision

# ConversationBackend imported at module level (mirrors JournalBackend / IdempotencyBackend)
# so the `conversation_backend: ConversationBackend | None` annotation on __init__
# resolves under typing.get_type_hints() and for mypy/pyright.
# conversation.backend imports only from .types + stdlib — circular-import safe.
from .conversation.backend import ConversationBackend

# PrincipalBackend imported at module level so the annotation on __init__ resolves.
# principal.backend imports only from .types + stdlib — circular-import safe.
# Principal and LOCAL_PRINCIPAL imported here because they appear as default argument
# values in call() — default values are evaluated at function-definition time, so they
# MUST be module-level imports (not lazy inline imports).
from .principal.backend import PrincipalBackend
from .principal import get_default_principal_backend
from .conversation.types import LOCAL_PRINCIPAL, Principal
from .exceptions import UnverifiedPrincipalConversationAccess
from .mcp_registry import (
    MCPRegistryError,
    MCPRegistryUnavailable,
    MCPServerRegistryBackend,
    _redact_for_error_message as _redact_mcp_registry_url,
    get_default_mcp_server_registry_backend,
)
from .logs.types import (
    PRIMITIVE_AGENT_CALL,
    PRIMITIVE_CAPTURE,
    PRIMITIVE_COST_WARNING,
    PRIMITIVE_DELEGATE,
    PRIMITIVE_DREAM,
    PRIMITIVE_ESCALATION,
    PRIMITIVE_EVAL,
    PRIMITIVE_HELPER,
    PRIMITIVE_JUDGMENT,
    PRIMITIVE_OTHER,
    PRIMITIVE_OUTCOME_ITERATION,
    PRIMITIVE_TOOL,
)
from ._platform import get_agents_root
from .exceptions import (
    AtomicAgentsError,
    CostGuardrailBlocked,
    DedupInFlight,
    HelperBatchPartialFailure,
    IdempotencyBackendError,
    NestedDelegationRefused,
    NotInRoster,
    PathTraversalError,
    PersonaCorrupted,
    PersonaNotFound,
    SelfDelegationError,
    ToolDescriptorInvalid,
    ToolHandlerImportFailed,
    ToolNotInRegistry,
)
from .tools import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    MAX_TOOL_ITERATIONS,
    ToolCallResult,
    ToolDefinition,
    ToolRegistry,
)
from .skills import (
    SkillManifest,
    discover_skills,
    load_skill_body,
    load_skill_referenced_file,
)
from .types import (
    AgentConfig,
    Capture,
    CostCheckResult,
    HelperResult,
    Response,
)


PINNED_MAX = 5
RECENT_NOTES_DEFAULT = 5
RECENT_JOURNAL_DEFAULT = 1


# ────────────────────────────────────────────────────────────────────
# #61 PR 2 — primitive derivation from the legacy ``trigger`` string.
#
# Today's ``self._log({"trigger": "..."})`` call sites use a free-form
# dispatch string. spec/22's ``RunRecord.primitive`` is the canonical
# taxonomy. This mapping derives ``primitive`` from ``trigger`` for
# every existing call site; new code paths SHOULD set ``primitive``
# explicitly via ``RunRecord(..., primitive=PRIMITIVE_X)`` rather than
# relying on the derivation.
#
# Triggers that share a primitive bucket (``helper_batch_reservation``
# + ``helper_batch_release`` + ``helper``) are folded together. The
# fallback bucket is ``PRIMITIVE_OTHER`` — backends MUST accept it
# (spec/22 §"Canonical primitive taxonomy" — the closed set is
# documentation, not enforcement).

_PRIMITIVE_BY_TRIGGER: dict[str, str] = {
    "agent_call": PRIMITIVE_AGENT_CALL,
    # HTTP-served calls (trigger='http', set by atomic_agents.serve) map to the
    # same agent_call primitive — they are agent invocations over a different
    # transport surface, not a different class of compute primitive.
    # spec/22 §"Canonical primitive taxonomy" + spec/37 §"Audit record shape".
    "http": PRIMITIVE_AGENT_CALL,
    "outcome_iteration": PRIMITIVE_OUTCOME_ITERATION,
    "dream": PRIMITIVE_DREAM,
    "eval": PRIMITIVE_EVAL,
    "helper": PRIMITIVE_HELPER,
    "helper_batch_reservation": PRIMITIVE_HELPER,
    "helper_batch_release": PRIMITIVE_HELPER,
    "delegate": PRIMITIVE_DELEGATE,
    "delegate_batch_reservation": PRIMITIVE_DELEGATE,
    "delegate_batch_release": PRIMITIVE_DELEGATE,
    "tool_call": PRIMITIVE_TOOL,
    "tool_call_deferred": PRIMITIVE_TOOL,
    "cost_warning": PRIMITIVE_COST_WARNING,
    "capture_write_error": PRIMITIVE_CAPTURE,
    "judgment": PRIMITIVE_JUDGMENT,
    "escalation_deferred_execution": PRIMITIVE_ESCALATION,
    "escalation_operator_revise_executed": PRIMITIVE_ESCALATION,
    "escalation_operator_revise_invalid_amendment": PRIMITIVE_ESCALATION,
    "escalation_resolved": PRIMITIVE_ESCALATION,
}

# spec/45 PR2 — body-hash auto-derivation gate. dedup_body_hash_enabled
# auto-derives an idempotency key ONLY for triggers where the same logical
# request can actually be REDELIVERED by an external transport (an HTTP retry,
# a queue redelivery, a cron over-fire). For framework-internal repeat-
# invocation callers — eval (trigger='eval'), delegate child calls
# (trigger='delegate'), outcome inner runs (trigger='outcome'), and plain
# manual/api/skill calls — identical inputs are EXPECTED to run again, and
# auto-deduping them would return a text='' deduped Response those consumers
# treat as a real result (a judge would score empty; a delegate would log empty
# as ok). An EXPLICIT caller-supplied idempotency_key is still honored on ANY
# trigger; only the implicit body-hash AUTO derivation is gated to this set.
_BODY_HASH_AUTO_DERIVE_TRIGGERS: frozenset[str] = frozenset({"http", "queue", "cron"})

# Sentinel for the lazily-resolved conversation_backend cache in __init__.
# Distinct from None (which means "no backend configured") so the first
# call() invocation can detect "not yet resolved" vs "resolved to no backend".
_CONV_BACKEND_UNRESOLVED = object()


def _derive_primitive_from_trigger(trigger: str | None) -> str:
    """Map the legacy ``trigger`` string to spec/22 ``primitive`` taxonomy.

    Returns ``PRIMITIVE_OTHER`` for ``None`` or unknown triggers — the
    open vocabulary fallback. Future code paths emitting new triggers
    SHOULD register the mapping here or set ``primitive`` explicitly.
    """
    if trigger is None:
        return PRIMITIVE_OTHER
    return _PRIMITIVE_BY_TRIGGER.get(trigger, PRIMITIVE_OTHER)


def _canonicalize_tool_loop(raw_tool_uses, tool_results):
    """Translate provider-shape ``raw.tool_uses`` + framework ``ToolCallResult``
    list into canonical ``LLMToolUse`` + ``LLMToolResult`` lists.

    Lifted out of ``_build_tool_loop_messages`` (#87 PR 2.5 review Finding 8)
    so PR 3 can reuse it when ``OpenAICompatibleLLMBackend.format_tool_results``
    takes ownership of the OpenAI/Moonshot branch — the same translation
    happens before either backend's call.

    Content normalization: ``LLMToolResult.content`` is set to ``tr.output``
    verbatim for success cases and to a ``"[tool error] ..."`` string for
    errors. The backend's ``format_tool_results`` does any provider-
    specific serialization (json.dumps with str() fallback) so wire bytes
    stay byte-equivalent to the pre-#87 ``build_tool_result_blocks_*``
    helpers — a PR 2.5 review caught the gap empirically.
    """
    from .llm.types import LLMToolResult, LLMToolUse

    canonical_tool_uses = [
        LLMToolUse(id=tu["id"], name=tu["name"], input=tu.get("input", {}))
        for tu in raw_tool_uses
    ]
    canonical_tool_results = []
    for tr in tool_results:
        if tr.error is not None:
            canonical_tool_results.append(
                LLMToolResult(
                    tool_use_id=tr.tool_use_id,
                    content=f"[tool error] {tr.error}",
                    is_error=True,
                )
            )
        else:
            canonical_tool_results.append(
                LLMToolResult(
                    tool_use_id=tr.tool_use_id,
                    content=tr.output,
                    is_error=False,
                )
            )
    return canonical_tool_uses, canonical_tool_results


class AtomicAgent:
    # Public type annotation pins ``lock_backend`` to the Protocol
    # (not the concrete ``FilesystemLockBackend`` subclass) so static
    # analysis treats the attribute as "any LockBackend implementation
    # — could be filesystem, Redis, or a future third-party backend"
    # rather than narrowing to the filesystem default. Step 9.1
    # maintainability specialist CRITICAL.
    lock_backend: LockBackend
    # Same class-level annotation rationale for ``log_backend`` (#61
    # PR 2). Without this, static analysis would narrow ``agent.log_
    # backend`` to the concrete ``FilesystemLogBackend`` default rather
    # than treating it as any ``LogBackend`` Protocol implementer —
    # breaking the operator-pinned-SQLite/Postgres/Datadog case PR 3
    # forward.
    log_backend: LogBackend
    # Same class-level annotation rationale for ``mandate_backend`` (#124
    # PR 2). Without this, static analysis would narrow
    # ``agent.mandate_backend`` to the concrete
    # ``FilesystemMandateBackend`` default rather than treating it as any
    # ``MandateBackend`` Protocol implementer — breaking the
    # operator-pinned-SaaS/mobile/Slack-bot case PR 3a forward.
    mandate_backend: MandateBackend
    # Same class-level annotation rationale for ``policy_backend`` (#89
    # PR 2). Without this, static analysis would narrow
    # ``agent.policy_backend`` to the concrete ``FilesystemPolicyBackend``
    # default rather than treating it as any ``PolicyBackend`` Protocol
    # implementer — breaking the operator-pinned-SaaS/Postgres/org-admin-
    # console case PR 3 forward.
    policy_backend: PolicyBackend
    # Same class-level annotation rationale for ``persona_backend`` (#62
    # PR 2). Without this, static analysis would narrow
    # ``agent.persona_backend`` to the concrete
    # ``FilesystemPersonaBackend`` default rather than treating it as any
    # ``PersonaBackend`` Protocol implementer -- breaking the
    # operator-pinned-SaaS/Postgres/git-backed case PR 3 forward.
    persona_backend: PersonaBackend
    # Same class-level annotation rationale for ``corpus_backend`` (#65
    # PR 3). Without this, static analysis would narrow
    # ``agent.corpus_backend`` to the concrete
    # ``FilesystemCorpusBackend`` default rather than treating it as any
    # ``CorpusBackend`` Protocol implementer -- breaking the
    # operator-pinned-SQLite/pgvector case PR 3 forward.
    corpus_backend: CorpusBackend
    # Same class-level annotation rationale for ``mcp_server_registry_backend``
    # (#201 PR 2). Without this, static analysis would narrow
    # ``agent.mcp_server_registry_backend`` to the concrete
    # ``FilesystemMCPServerRegistryBackend`` default rather than treating
    # it as any ``MCPServerRegistryBackend`` Protocol implementer --
    # breaking the operator-pinned-HTTP/SaaS case PR 4 forward.
    mcp_server_registry_backend: MCPServerRegistryBackend
    # Same class-level annotation rationale for ``memory`` (#382 PR 1).
    # Without this, static analysis would narrow ``agent.memory`` to the
    # concrete ``FilesystemBackend`` default rather than treating it as any
    # ``MemoryBackend`` Protocol implementer — breaking the
    # operator-pinned-Postgres/custom case when memory_backend= kwarg is
    # supplied.
    memory: MemoryBackend
    # Same class-level annotation rationale for ``conversation_backend`` (spec/47 PR1).
    # Optional — None == single-shot (backward-compatible default, rule #14).
    # When set, agent.call(conversation_id=...) loads prior turns and writes back
    # the new turn. When None (the default), agent.call() is unchanged single-shot.
    conversation_backend: "ConversationBackend | None"
    """The main agent runtime.

    Responsible for:
    - Loading agent files in canonical order (per spec/04)
    - Calling the LLM with cost-guardrail enforcement
    - Extracting and writing captures (helper-mediated, atomic)
    - Logging every run to log/YYYY-MM/YYYY-MM-DD.jsonl
    - Helper calls (sequential and parallel) per spec/10
    """

    def __init__(
        self,
        name: str,
        trigger: str = "manual",
        agents_root: Path | None = None,
        run_id: str | None = None,
        tools: ToolRegistry | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        *,
        lock_backend: LockBackend | None = None,
        log_backend: LogBackend | None = None,
        profile_backend: AgentProfileBackend | None = None,
        tool_registry_backend: ToolRegistryBackend | None = None,
        mandate_backend: MandateBackend | None = None,
        policy_backend: PolicyBackend | None = None,
        persona_backend: PersonaBackend | None = None,
        corpus_backend: CorpusBackend | None = None,
        mcp_server_registry_backend: MCPServerRegistryBackend | None = None,
        memory_backend: MemoryBackend | None = None,
        journal_backend: JournalBackend | None = None,
        goal_backend: GoalBackend | None = None,
        idempotency_backend: IdempotencyBackend | None = None,
        conversation_backend: "ConversationBackend | None" = None,
        principal_backend: "PrincipalBackend | None" = None,
    ):
        self.name = name
        self.trigger = trigger
        self.agents_root = agents_root or get_agents_root()
        self.agent_root = self.agents_root / name
        # Track whether the caller pinned a specific run_id (outcome, eval, dream
        # loops pin run_id so agent_call records correlate with the outer loop's
        # records). When pinned, call() MUST NOT overwrite — the explicit id is
        # the audit correlation anchor. spec/37 MUST 8 applies only to unpinned
        # (HTTP / skill / manual / cron) callers that need a fresh id each call.
        self._run_id_pinned: bool = run_id is not None
        self.run_id = run_id or self._generate_run_id()
        # Custom tool registry (spec/17). Empty registry = no custom tools.
        self.tool_registry = tools if tools is not None else ToolRegistry()
        # Bound the multi-turn tool loop. Clamped to [1, MAX_TOOL_ITERATIONS].
        self.max_tool_iterations = max(1, min(max_tool_iterations, MAX_TOOL_ITERATIONS))

        if not self.agent_root.exists():
            raise AtomicAgentsError(
                f"Agent folder not found: {self.agent_root}. "
                f"Set ATOMIC_AGENTS_ROOT env var or create the agent."
            )

        # LockBackend instance bound to this agent's root. Operators may
        # pin a backend via the ``lock_backend=...`` constructor kwarg
        # (programmatic path) OR via ``ATOMIC_AGENTS_LOCK_BACKEND`` +
        # ``ATOMIC_AGENTS_LOCK_BACKEND_URL`` env vars (deployment path
        # — Docker, launchd, Cloud Run). See spec/21 §"Operator override
        # surface". Default (both unset) is the filesystem backend
        # scoped to this agent's root — matches pre-#60 PR 3 behavior.
        # ``lock_backend`` is public — mirrors ``self.memory`` and lets
        # diagnostic code (``atomic-agents doctor``) reuse the same
        # backend instance.
        if lock_backend is None:
            self.lock_backend = get_default_lock_backend(self.agent_root)
        else:
            self.lock_backend = lock_backend

        # LogBackend instance bound to this agent's root (#61 PR 2).
        # Operators may pin a backend via the ``log_backend=...``
        # constructor kwarg (programmatic path) OR via
        # ``ATOMIC_AGENTS_LOG_BACKEND`` (deployment path — same env-var
        # idiom as locks). Default (both unset) is the filesystem
        # backend scoped to this agent's root — matches pre-#61 PR 2
        # behavior byte-for-byte (the JSONL append path at
        # ``log/YYYY-MM/YYYY-MM-DD.jsonl`` is preserved). ``log_backend``
        # is public — mirrors ``self.memory`` / ``self.lock_backend``
        # and lets diagnostic code (``atomic-agents doctor``) reuse
        # the same backend instance.
        if log_backend is None:
            self.log_backend = get_default_log_backend(self.agent_root)
        else:
            self.log_backend = log_backend

        # AgentProfileBackend instance (#63 PR 2 — wires the bootstrap path
        # through the Protocol established in PR 1). Operators may pin via
        # the ``profile_backend=...`` constructor kwarg (programmatic path
        # — always wins) OR via ``ATOMIC_AGENTS_PROFILE_BACKEND`` +
        # ``ATOMIC_AGENTS_PROFILE_BACKEND_URL`` env vars (deployment path
        # — Docker, launchd, Cloud Run). Default (both unset) is the
        # filesystem backend scoped at the agents_root — matches pre-#63
        # PR 2 behavior byte-for-byte (the on-disk markdown layout is
        # preserved). ``profile_backend`` is public — mirrors
        # ``self.lock_backend`` / ``self.log_backend`` so diagnostic code
        # (``atomic-agents doctor``) and runners can reuse the same
        # backend instance instead of resolving twice.
        if profile_backend is None:
            self.profile_backend = get_default_profile_backend(self.agents_root)
        else:
            self.profile_backend = profile_backend

        # ToolRegistryBackend instance (#64 PR 2 — wires the bootstrap
        # path through the Protocol established in PR 1). Operators may
        # pin via the ``tool_registry_backend=...`` constructor kwarg
        # (programmatic path — always wins) OR via
        # ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND`` +
        # ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL`` env vars
        # (deployment path). Default (both unset) is the filesystem
        # backend scoped at THIS agent's root (per-agent scope, NOT
        # agents_root — distinct from profile_backend which is
        # fleet-scoped). The on-disk ``<agent>/tools/`` layout is the
        # surface, so an empty / absent ``tools/`` dir yields zero
        # backend tools and preserves byte-identical pre-#64-PR-2
        # behavior for every existing AtomicAgent test site.
        # ``tool_registry_backend`` is public — mirrors
        # ``self.lock_backend`` / ``self.log_backend`` / ``self.profile_backend``
        # so diagnostic code (``atomic-agents doctor``) and runners can
        # reuse the same backend instance instead of resolving twice.
        # Assigned before ``profile_backend.load_profile`` so a backend
        # factory failure (e.g., ``BackendNotRegistered`` from a typo'd
        # ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND``) surfaces before any
        # profile-load I/O. The backend constructor itself is I/O-free
        # — actual ``tools/`` walks happen in ``list_tools`` /
        # ``load_tool`` below.
        if tool_registry_backend is None:
            self.tool_registry_backend = get_default_tool_registry_backend(
                self.agent_root
            )
        else:
            self.tool_registry_backend = tool_registry_backend

        # MandateBackend instance (#124 PR 2 — wires the bootstrap path
        # through the Protocol established in PR 1). Operators may pin via
        # the ``mandate_backend=...`` constructor kwarg (programmatic path
        # — always wins) OR via ``ATOMIC_AGENTS_MANDATE_BACKEND`` env var
        # (deployment path — Docker, launchd, Cloud Run). Default (both
        # unset) is the filesystem backend scoped at THIS agent's root
        # (per-agent scope, NOT agents_root — distinct from
        # profile_backend which is fleet-scoped; mirrors
        # tool_registry_backend's per-agent scoping per spec/29 and
        # spec/25 Decision 9). ``mandate_backend`` is public — mirrors
        # ``self.lock_backend`` / ``self.log_backend`` /
        # ``self.profile_backend`` / ``self.tool_registry_backend`` so
        # diagnostic code (``atomic-agents doctor``) and runners can
        # reuse the same backend instance instead of resolving twice.
        if mandate_backend is None:
            self.mandate_backend = get_default_mandate_backend(self.agent_root)
        else:
            self.mandate_backend = mandate_backend

        # ── PolicyBackend resolution (#89 PR 2, cascade-aware in PR 3a) ──────
        # Policy is fleet-shaped (one policy.md applies to all agents in the
        # project), NOT per-agent like Mandate. Default backend resolves at
        # ``self.agents_root`` scope; cascade-aware re-resolution below bumps
        # this to ``cascade.project_root`` after cascade detection (fixes #236).
        # The operator's explicit kwarg always wins (programmatic-override
        # discipline — cascade re-resolution is skipped when kwarg is supplied).
        #
        # Per spec/32 MUST #4, FilesystemPolicyBackend construction is
        # side-effect-free — no stat, no parse. The instance is held but
        # never read in PR 2 (zero behavior change); PR 3a wires consumption
        # via _check_cost_guardrails MIN composition + MandateCheck integration
        # + policy_decision event emission. Non-cap surfaces (tool/MCP/model)
        # ship in PR 3b behind ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP.
        #
        # _policy_backend_was_explicit tracks whether the operator supplied a
        # kwarg so the post-cascade re-resolution can skip it correctly.
        _policy_backend_was_explicit = policy_backend is not None
        if policy_backend is None:
            self.policy_backend = get_default_policy_backend(self.agents_root)
        else:
            self.policy_backend = policy_backend

        # ── PersonaBackend resolution (#62 PR 2) ──────────────────────────
        # Mirrors PolicyBackend's _policy_backend_was_explicit pattern. The
        # default backend resolves at self.agents_root; delegate threading
        # (see ``delegate`` method below) consults the explicit flag so
        # default-resolved backends don't leak the coordinator's scope into
        # delegates (D-ER-2). Unlike policy_backend (always threaded because
        # Policy is fleet-scoped), persona_backend is ONLY threaded when the
        # operator explicitly supplied it via kwarg — cross-vault delegation
        # uses the coordinator's agents_root as persona scope, which would
        # silently resolve the wrong .personas/ directory for the delegate.
        _persona_backend_was_explicit = persona_backend is not None
        if persona_backend is None:
            self.persona_backend = get_default_persona_backend(self.agents_root)
        else:
            self.persona_backend = persona_backend
        # Saved on self so delegate() can consult it without re-checking
        # the constructor kwarg (the kwarg is no longer in scope there).
        self._persona_backend_was_explicit = _persona_backend_was_explicit

        # ── CorpusBackend resolution (#65 PR 3) ──────────────────────────
        # Mirrors PersonaBackend's _persona_backend_was_explicit pattern.
        # Corpus is per-agent semantic context (wiki + raw), NOT fleet-scoped
        # like Policy or AgentProfile. Default-resolved backends do NOT thread
        # to delegates (D-ER-2 corollary). Operators wanting a shared corpus
        # across a coordinator and delegates pass corpus_backend= explicitly.
        _corpus_backend_was_explicit = corpus_backend is not None
        if corpus_backend is None:
            self.corpus_backend = get_default_corpus_backend(self.agent_root)
        else:
            self.corpus_backend = corpus_backend
        # Saved on self so delegate() can consult it without re-checking
        # the constructor kwarg (the kwarg is no longer in scope there).
        self._corpus_backend_was_explicit = _corpus_backend_was_explicit

        # ── Mandate crash recovery + reservation managers (#124 PR 3b) ──────
        # Per spec/29 §"Crash recovery for reservations" + plan-subagent
        # Risks 8 (invocation site = agent init) + 9 (multi-scope iteration).
        # Recovery is invoked BEFORE profile load so that orphan reservations
        # from the previous run are reconciled before any mandate checks fire
        # in this run. Reservation managers are instantiated per-scope so each
        # scope gets its own TTL watcher and in-process pending table.
        #
        # The ``_mandate_reservation_managers`` dict and the reservation
        # managers themselves are populated lazily below via
        # ``_run_mandate_recovery_for_all_scopes`` — we store the map
        # before profile load so judges_config can refine TTL settings after
        # ``_load_config`` without re-instantiating (TTL is read from
        # judges_config.mandate_settings.reservation_ttl_s once available).
        #
        # NOTE: ``_mandate_reservation_managers`` is intentionally empty at
        # this point; ``_init_mandate_reservation_managers`` is called at the
        # END of __init__ after ``_load_config`` has populated judges_config
        # (so the correct TTL from judges.md is available).
        self._mandate_reservation_managers: dict[str, "MandateReservationManager"] = {}
        self._run_mandate_recovery_for_all_scopes()

        # Load the agent's profile ONCE at init time. ``self._profile`` is
        # an init-time snapshot — private cache, not a stable operator
        # surface. Operators read persona text via assemble_system_prompt()
        # or the filesystem directly, not via agent._profile. The downstream
        # bootstrap methods (``_load_config``, ``_load_persona``,
        # ``_load_goal_text``) read fields off this snapshot instead of
        # re-reading the filesystem.
        self._profile: AgentProfile = self.profile_backend.load_profile(self.name)

        # ── PersonaBackend bootstrap composition (D-PP-4 / #62 PR 2) ────────
        # When the agent's persona is externally owned (persona.link.md
        # present on filesystem; persona_id column non-NULL on SQLite),
        # load_profile() returns an AgentProfile whose persona fields are
        # empty (AgentProfile does not read from PersonaBackend directly —
        # D1a keeps the protocols decoupled). Repopulate the persona fields
        # here from PersonaBackend and explicitly re-derive agent_mode so
        # downstream bootstrap (cost-checking, policy resolution, cascade
        # detection) operates against the correct mode (D-PP-4 — agent_mode
        # is derived from persona_identity; without re-derivation it would
        # remain 'reactive' for a goal-driven externally-owned persona).
        _persona_id = self.profile_backend.external_persona_ref(self.name)
        if _persona_id is not None:
            from .goal import parse_agent_mode_text

            try:
                _persona = self.persona_backend.load_persona(_persona_id)
            except PersonaNotFound as _pnf:
                raise PersonaNotFound(
                    f"agent {self.name!r}: persona.link.md references missing "
                    f"persona record {_persona_id!r}: {_pnf}"
                ) from _pnf
            except PersonaCorrupted as _pc:
                raise PersonaCorrupted(
                    f"agent {self.name!r}: persona record {_persona_id!r} is corrupt: {_pc}"
                ) from _pc
            self._profile = self._profile.replace(
                persona_identity=_persona.identity,
                persona_soul=_persona.soul,
                persona_user=_persona.user,
                agent_mode=parse_agent_mode_text(_persona.identity),  # re-derive
            )

        # ── MCPServerRegistryBackend resolution (#201 PR 2 of 5) ──────────────
        # Mirrors PersonaBackend's _persona_backend_was_explicit pattern at
        # agent.py:443-450 and CorpusBackend's at agent.py:458-465. MCP catalog
        # is per-agent semantic context (per spec/36 Decision 1); delegate
        # threading is explicit-only.
        #
        # Unlike other backends, the default-resolution factory needs read_paths
        # from self._profile.tool_config['read_paths'], which is only available
        # after profile load. The resolution therefore happens here in __init__
        # AFTER profile load and BEFORE _load_config() is called, rather than
        # inside _load_config() (which is a pure reader of self._profile).
        # This is spec/36 line 599 corrected (the spec text says _load_config()
        # but the actual right place is __init__; spec doc gets a one-sentence
        # amendment in this same PR).
        _mcp_server_registry_backend_was_explicit = (
            mcp_server_registry_backend is not None
        )
        read_paths_for_mcp_registry = self._profile.tool_config.get("read_paths", [])
        if mcp_server_registry_backend is None:
            self.mcp_server_registry_backend = get_default_mcp_server_registry_backend(
                self.agent_root,
                read_paths_for_mcp_registry,
            )
        else:
            self.mcp_server_registry_backend = mcp_server_registry_backend
        # Saved on self so delegate() can consult it without re-checking the
        # constructor kwarg (the kwarg is no longer in scope there).
        self._mcp_server_registry_backend_was_explicit = (
            _mcp_server_registry_backend_was_explicit
        )

        # Probe + augment profile per spec/36 framework-level invariant (line
        # 520-522). NO try/except around load_all_mcp_servers -- fail-closed:
        # MCPRegistryUnavailable propagates. The wrapper below adds the
        # backend_id + redacted URL context for operator-facing messages per
        # spec/36 MUST 4 + line 522.
        try:
            _materialized_mcp_specs = (
                self.mcp_server_registry_backend.load_all_mcp_servers()
            )
        except MCPRegistryError as exc:
            # Catch MCPRegistryError broadly (covers MCPRegistryUnavailable,
            # MCPRegistryDescriptorInvalid, MCPRegistryAuthRequired). Re-raise
            # preserving the original exception type so callers can distinguish
            # transient (Unavailable) from permanent (DescriptorInvalid).
            _safe_backend_id = getattr(
                self.mcp_server_registry_backend, "backend_id", "unknown"
            )
            raise type(exc)(
                f"[{_safe_backend_id}] catalog probe failed at agent "
                f"construction: {_redact_mcp_registry_url(str(exc))}"
            ) from exc
        # Populate mcp_servers_resolved on the profile via replace().
        # Stream 2 adds the mcp_servers_resolved field to AgentProfile; this
        # replace() call is a no-op on the field until Stream 2 merges.
        self._profile = self._profile.replace(
            mcp_servers_resolved=_materialized_mcp_specs,
        )

        # ── OutcomeBackend LIVE-WIRED (#448 PR2 — spec/42 LOCKED) ──────────────
        # self.outcome_backend is the per-agent public handle for operator
        # inspection and the future #448 PR3 goal-outcome coordinator.
        # Mirrors self.goal_backend (initialized below) — kwarg on AtomicAgent
        # is NOT added (the runner resolves independently; adding it here would be
        # dead code, matching the #426 arc ruling).
        #
        # IMPORTANT: the LIVE WRITE PATH is OutcomeRunner.outcome_backend —
        # OutcomeRunner resolves its OWN backend independently via
        # get_default_outcome_backend(agent_root), or via the outcome_backend=
        # kwarg added to OutcomeRunner.__init__ in #448 PR2.
        # AtomicAgent does NOT construct or feed an OutcomeRunner; they are
        # separate instances. The coordinator handoff from AtomicAgent.outcome_backend
        # to the runner is the PR3 story, not a present one.
        # Do NOT add outcome_backend= to AtomicAgent.__init__ — it would be dead code
        # (OutcomeRunner is constructed independently and resolves its own backend,
        # same shape as GoalManager).
        from .outcome import get_default_outcome_backend  # noqa: PLC0415

        self.outcome_backend = get_default_outcome_backend(self.agent_root)

        # ── JournalBackend LIVE-WIRED (#427 PR1 — spec/43) ──────────────────
        # ADOPT-NOW ruling: self.journal_backend IS a live path ON THE AGENT —
        # agent._load_recent_journal() routes through it. (By contrast,
        # self.outcome_backend above is the per-agent coordinator/inspection
        # handle, NOT the agent's read/write path — the outcome write path lives
        # on OutcomeRunner.outcome_backend, a separate instance.)
        # self.journal_backend IS the live read path — agent._load_recent_journal()
        # routes through it. The kwarg-wins-over-env-var pattern matches every
        # other backend kwarg.
        from .journal import get_default_journal_backend  # noqa: PLC0415

        if journal_backend is None:
            self.journal_backend = get_default_journal_backend(self.agent_root)
        else:
            self.journal_backend = journal_backend

        # ── GoalBackend LIVE-WIRED (#448 PR1 — spec/41) ─────────────────────
        # self.goal_backend is the per-agent persistence handle exposed for
        # operator override / doctor / the future #448 PR3 goal-outcome
        # coordinator. kwarg-wins-over-env-var pattern matches journal_backend.
        # NOTE: AtomicAgent does NOT construct or feed a GoalManager today —
        # GoalManager resolves its OWN backend independently (in the CLI main()
        # and programmatic callers). The provider/consumer handoff is the
        # future-coordinator path (PR3), not a present one; do not write code
        # assuming AtomicAgent passes self.goal_backend into a GoalManager.
        # NOTE: self.goal_backend is the PERSISTENCE handle only — it is NOT the
        # goal_text reader for prompt assembly. _load_goal_text() STAYS on
        # self._profile.goal_text (the profile snapshot). Do NOT add a second
        # goal_text reader here (Principle #6 single reader, A7 ruling).
        # NOTE: AtomicAgent STORES this handle but never invokes a backend method
        # directly (no load_goal/save_goal/append_history_event call from agent.py).
        # The agent_id argument semantics (filesystem ignores it,
        # scoped via agent_root; a multi-tenant backend keys on it — the
        # tierC-agent-id-argument-value ruling) apply at GoalManager's call
        # sites, not here.
        from .goal import get_default_goal_backend  # noqa: PLC0415

        if goal_backend is None:
            self.goal_backend = get_default_goal_backend(self.agent_root)
        else:
            self.goal_backend = goal_backend

        # spec/45 PR2: idempotency backend — kwarg-wins-over-env-var pattern
        # matching journal_backend / goal_backend. Resolved lazily to avoid
        # creating the idempotency/ directory on every agent construction
        # (only needed when idempotency_key is supplied to call()).
        if idempotency_backend is None:
            self.idempotency_backend: IdempotencyBackend = (
                get_default_idempotency_backend(self.agent_root)
            )
        else:
            self.idempotency_backend = idempotency_backend

        # spec/47 PR1: conversation backend — NULLABLE OPTIONAL (rule #14).
        # 'No backend configured (default None) == today's exact single-shot.'
        # IMPORTANT: do NOT call get_default_conversation_backend() unconditionally
        # here — that would create conversations/ on every agent construction,
        # breaking backward compatibility. The kwarg default is None; when None
        # and no env var / model.md field is set, self.conversation_backend stays None.
        # Three-channel resolution:
        #   (1) constructor kwarg wins (if not None)
        #   (2) ATOMIC_AGENTS_CONVERSATION_BACKEND env var (resolved lazily in call())
        #   (3) AgentConfig.conversation_backend_id from model.md (resolved lazily in call())
        # Channels (2) and (3) are resolved lazily in _resolve_conversation_backend()
        # called from call() after lazy load, to avoid the __init__/config chicken-and-egg.
        self.conversation_backend: "ConversationBackend | None" = conversation_backend
        # Cache for the lazily-resolved backend (channels 2 + 3). Set by
        # _resolve_conversation_backend() on the first call() invocation.
        # Sentinel: _CONV_BACKEND_UNRESOLVED (distinct from None, which means 'no backend').
        self._conversation_backend_resolved: "ConversationBackend | None | object" = (
            _CONV_BACKEND_UNRESOLVED
        )

        # spec/48: principal backend — kwarg-wins-over-env-var pattern.
        # Unlike ConversationBackend (which is nullable optional), PrincipalBackend
        # is always non-null: the home-user default is LocalPrincipalBackend (not None).
        # LocalPrincipalBackend.__init__ is side-effect-free (no I/O), so
        # agent construction stays cheap even when no explicit backend is supplied.
        # Three-channel resolution:
        #   (1) constructor kwarg wins (if not None)
        #   (2) ATOMIC_AGENTS_PRINCIPAL_BACKEND env var (in get_default_principal_backend)
        #   (3) Default: LocalPrincipalBackend (home-user zero-config)
        if principal_backend is None:
            self.principal_backend: PrincipalBackend = get_default_principal_backend()
        else:
            self.principal_backend = principal_backend

        # Per-agent target extractor registry (spec/29 §"Target extraction",
        # #124 PR 3a). MUST initialize BEFORE tool_registry loading below so
        # ToolDefinitions that declare a target_extractor_id can be validated
        # at tool_registry.register() time rather than silently failing at
        # MandateCheck evaluation time (plan-subagent Risk A + spec/29
        # §"Registration order discipline"). Built-in heuristic extractors
        # (recipient_to, recipient_field, target_field, url_field,
        # repository_field, customer_id_field, channel_id_field) are
        # pre-registered by TargetExtractorRegistry.__init__ — they are always
        # available for all tool descriptors. Operators add custom extractors
        # via agent.register_target_extractor(name, callable) AFTER agent
        # construction, then register the tool that references the extractor.
        # NOT persisted on AgentProfile snapshot (Callable values cannot
        # satisfy spec/25 MUST #4 Tier B lossless round-trip for structured-
        # storage backends). Per-agent scoping: delegate agents construct their
        # own TargetExtractorRegistry fresh — coordinator-registered extractors
        # do NOT flow to delegates (mirrors spec/15 delegate isolation +
        # spec/25 Decision 9 per-agent backend scoping).
        from .judge.target_extractor_registry import TargetExtractorRegistry

        self._target_extractors = TargetExtractorRegistry()

        # Per-agent cost estimator registry (#124 PR 3b prep — spec/29
        # §"Validation steps" step 8). Same per-agent isolation as
        # target_extractors above; same fail-loud discipline at
        # tool_registry.register() when a ToolDefinition.cost_estimator_id
        # references an unregistered name. Empty at construction — no
        # built-in estimators ship; tools with dynamic pricing register
        # explicitly, tools with static pricing use
        # ToolDefinition.expected_external_cost_usd, mandates with
        # *_external_usd caps over tools with neither registered fail-closed
        # with mandate_external_cost_unprojectable.
        from .judge.cost_estimator_registry import CostEstimatorRegistry

        self._cost_estimators = CostEstimatorRegistry()

        # Register backend-discovered tools into the in-memory
        # ``self.tool_registry`` (#64 PR 2 — Decision 1 + Decision 8 of
        # spec/25). The Protocol layer COMPOSES with the in-memory
        # dispatch class:
        #
        #     backend.list_tools()       → list[ToolRef]      # discovery
        #     backend.load_tool(name)    → ToolDefinition     # materialization
        #     self.tool_registry.register(td)                 # dispatch
        #
        # Collision discipline: operator-supplied tools (already in
        # ``self.tool_registry`` from the ``tools=`` constructor kwarg
        # initialized at line ~243) registered FIRST. Backend tools
        # register with ``allow_overwrite=False`` (the default) so
        # name collisions between operator + backend surface loudly
        # as ``ToolNameCollision`` — operator intent wins. MCP
        # discovery (still later, at call-time around line ~2345)
        # uses ``allow_overwrite=True`` per established semantics;
        # MCP names ALWAYS namespace as ``server__tool`` so collisions
        # with backend tools indicate a genuine conflict.
        #
        # Discovery is lazy at the descriptor layer (``list_tools()``
        # parses frontmatter only) but eager at the handler layer
        # here — each ``load_tool(name)`` runs ``importlib.util.spec_
        # from_file_location`` + ``exec_module`` on the filesystem
        # reference. For an agent with no ``tools/`` directory (the
        # 96 existing test fixtures), the loop body executes zero
        # times. Operators with side-effecting handler-module top-
        # level code see the side effect on agent construction; this
        # is spec/25 Decision 5 + the §"Known gaps" note about
        # lazy-import semantics.
        for ref in self.tool_registry_backend.list_tools():
            try:
                td = self.tool_registry_backend.load_tool(ref.name)
            except (
                ToolDescriptorInvalid,
                ToolHandlerImportFailed,
                ToolNotInRegistry,
                ValueError,
                OSError,
            ) as exc:
                # Per-tool failures don't block agent construction —
                # other tools may still be usable. Emit a debug log so
                # operators triaging "why is my tool not registered?"
                # find the cause without re-running ``backend.validate(name)``
                # by hand. ``ref.name`` is already validated by
                # ``list_tools()`` (descriptor frontmatter must be
                # well-formed for the ref to exist), so log-injection
                # risk is bounded — no control chars reach this string.
                _logger.debug(
                    "skipping tool %r during agent construction: %s: %s",
                    ref.name,
                    type(exc).__name__,
                    exc,
                )
                # Operators triage specific tools via
                # ``backend.validate(name)`` (spec/25 Decision 6 —
                # static check that returns ``ValidationResult``
                # instead of raising).
                #
                # Caught exception classes:
                #   - ``ToolDescriptorInvalid`` — descriptor parse error
                #     (frontmatter YAML, missing closing ``---``, etc.)
                #   - ``ToolHandlerImportFailed`` — handler module raised
                #     at import time OR missing ``handler`` symbol
                #   - ``ToolNotInRegistry`` — descriptor parsed but handler
                #     module disappeared between ``list_tools`` and
                #     ``load_tool`` (TOCTOU race the spec/25 §"Known
                #     gaps" documents)
                #   - ``ValueError`` — tool name has a control char,
                #     leading dot, or path-separator that the descriptor
                #     filename smuggled in (Step 9.1 security finding;
                #     a single ``tool\twith_tab.md`` would otherwise
                #     break agent construction for every operator)
                #   - ``OSError`` — filesystem-level failure during
                #     descriptor / handler read (``tools/`` chmod'd to
                #     000; permission glitch; disk error). Same
                #     defense-in-depth shape as ``list_tools`` already
                #     applies to descriptor parsing.
                continue
            self.tool_registry.register(
                td,
                target_extractor_registry=self._target_extractors,
                cost_estimator_registry=self._cost_estimators,
            )

        # Cascade detection — None for single-agent layouts (load behaves as before),
        # populated for paths shaped <system>/projects/<project>/agents/<role>/.
        # NOTE: kept after the profile_backend.load_profile call because
        # the profile backend handles cascade internally for config loading,
        # but downstream methods (load_role_prompt, load_project_layer_text,
        # _load_tools_text, tool registration at lines ~622+657) still need
        # ``self.cascade`` for prompt assembly + tool-path resolution.
        self.cascade: _cascade.CascadePaths | None = _cascade.detect_cascade(
            self.agent_root
        )

        # ── Cascade-aware PolicyBackend re-resolution (#236 fix, PR 3a) ────────
        # In cascade layouts, policy.md lives at cascade.project_root (the
        # project-level directory), NOT at agents_root (the <project>/agents/
        # subdirectory). The default resolution above ran before cascade
        # detection, so it targeted agents_root — which is wrong for cascade.
        #
        # Re-resolve here ONLY when:
        #   (a) the operator did NOT supply an explicit policy_backend kwarg,
        #   AND (b) we are in a cascade layout.
        # Operator-supplied backends ALWAYS win (programmatic-override discipline).
        if not _policy_backend_was_explicit and self.cascade is not None:
            self.policy_backend = get_default_policy_backend(self.cascade.project_root)

        # Skills (spec/18) — discover at init so metadata is available for
        # system-prompt assembly. Empty list when no skills/ directory exists.
        self.skills: list[SkillManifest] = discover_skills(self.agent_root)

        # Register built-in skill tools if any skills were discovered.
        # This runs after tool_registry is created but before _load_config
        # so operators can still register their own tools on the same registry.
        if self.skills:
            self._register_skill_tools()

        # Per-call Policy snapshot (#89 PR 3a). Reset at call() entry,
        # cleared in call() finally block. ``None`` outside of call().
        # The snapshot is taken ONCE at call() entry (Premise 3: predictability
        # over freshness within the call). All cost-cap checks within the same
        # call() read from this frozen snapshot instead of re-querying the backend.
        self._policy_snapshot_this_call = None

        # Per-call helper-provenance rollup (spec/13 Layer 3). Reset at the
        # start of each call(); appended to by helper_call(). Empty list
        # means either no helpers ran or the call started outside call().
        self._helpers_this_run: list[dict] = []

        # Per-call delegation rollup. Reset at the start of each call();
        # appended to by delegate(). Embedded in the parent's run log record
        # as `delegations: [...]` when non-empty.
        self._delegations_this_run: list[dict] = []

        # Cumulative cost of all delegate() calls made during the current run.
        # Reset at the start of each call(). Passed as extra_in_flight_cost_usd
        # to _check_cost_guardrails before each delegation so sequential
        # delegations correctly see prior delegated spend. (fix R2-A2)
        self._delegated_cost_this_run: float = 0.0

        # MCP client pool (spec/19). Lazy-initialized on first call() when
        # mcp_servers are declared. Torn down in call()'s finally block.
        # None means either no MCP servers declared or pool not yet initialized.
        self.mcp_pool: MCPClientPool | None = None

        # Judge layer (#112 PR 2a + 2b + 3a). PolicyJudge + LLMJudgeBackend
        # instances are lazy-built on first dispatch — cached per-agent
        # so policy_version is stable within a run. ``_llm_judge`` may
        # remain ``None`` when no LLM key is configured for the default
        # judge model (PR 2b: ``gpt-5-nano``); the ensemble runs
        # PolicyJudge only in that case. ``tool_classifications`` parsed
        # from tools.md ``## Tool classification`` section; empty dict
        # otherwise (everything defaults to external_side_effect in
        # ``_resolve_classification``).
        #
        # ``self.judges_config`` is the parsed ``judges.md`` operator
        # config (PR 3a). ``None`` when judges.md is absent — the
        # judge dispatch falls back to ``_default_class_policy_snapshot``
        # (PR 2a/2b's hardcoded JUDGE_REQUIRED defaults). When present,
        # ``_dispatch_with_judge`` uses parsed class_policy +
        # per-class failure_policy + timeout + budget configuration.
        self._policy_judge = None
        self._llm_judge = None  # type: ignore[assignment]
        self._llm_judge_constructed = False  # distinct from None-cache
        self._mandate_check = None  # type: ignore[assignment]  # #124 PR 3a
        self._tool_classifications: dict[str, str] = {}
        self.judges_config = None  # type: ignore[assignment]
        # Per-iteration side-channel: tool_call_id → ActionProposal for
        # mandate-citing proposals that the ensemble ALLOWed. Reset at
        # the start of each call() while-loop iteration. Populated by
        # _dispatch_with_judge when allow=True + cites_mandate. Consumed
        # in the tool-execution loop to create/commit/rollback reservations
        # and run post-action verification (#124 PR 3b).
        self._mandate_allowed_proposals: dict[str, "ActionProposal"] = {}

        # Loaded later via load() — populated in __init__ for clarity
        self._persona_text: str = ""
        self._tools_text: str = ""
        self._memory_index_text: str = ""
        self._wiki_index_text: str = ""
        self._pinned_notes: list[str] = []
        self._recent_notes: list[str] = []
        self._recent_journal: list[str] = []
        # Cascade-only sections (empty when not cascaded)
        self._role_prompt_text: str = ""
        self._project_canon_text: str = ""
        self._project_style_guide_text: str = ""
        self._project_goal_text: str = ""
        self._project_policy_text: str = ""
        # Single-agent goal context (per spec/04 step 3.5; empty for cascaded agents)
        self._goal_text: str = ""

        # Agent operating mode (reactive / goal-driven / hybrid), derived
        # from IDENTITY.md content via the profile backend. Defaults to
        # "reactive" when no Operating-mode section is present
        # (FilesystemAgentProfileBackend.load_profile handles this via
        # parse_agent_mode_text). #63 PR 2: read from the pre-loaded
        # profile snapshot instead of re-reading IDENTITY.md from disk.
        self.agent_mode: str = self._profile.agent_mode

        # Parse config files
        self.config = self._load_config()

        # Instantiate per-scope reservation managers AFTER _load_config so
        # judges_config.mandate_settings.reservation_ttl_s (from judges.md)
        # is resolved. Each scope gets its own MandateReservationManager
        # with the correct TTL from the operator's configuration.
        self._init_mandate_reservation_managers()

        # Memory backend (spec/20 — routes all memory I/O through the protocol).
        # kwarg-wins: an explicit memory_backend= bypasses env-var resolution
        # entirely.  When None, the factory reads ATOMIC_AGENTS_MEMORY_BACKEND
        # and threads the agent's already-resolved lock_backend so
        # memory.apply_staging serializes against agent.call() through the SAME
        # lock backend instance — operator-pinned Redis backends see consistent
        # locking across the agent + memory paths instead of two independent
        # backend resolutions.
        #
        # Bootstrap ordering: the factory is called AFTER _load_config() so
        # the config parse is complete before we pay any backend I/O; the
        # factory itself reads no vault files (env-var-only selection) so there
        # is no chicken-and-egg paradox.  See spec/20 §"Operator override
        # surface" for the bootstrap-paradox note.
        #
        # Delegate threading: memory is per-agent STATE (not fleet-shared
        # config like persona/corpus), so delegate() does NOT thread this
        # instance to children even when memory_backend= was supplied — a
        # root-bound FilesystemBackend would silently route a specialist's
        # writes into the COORDINATOR's memory/ dir (cross-agent corruption).
        # Children resolve their own per-agent backend via the same
        # process/deployment-global env selection. See spec/20 §"Delegate
        # threading" and ruling delegate-child-threading (#382).
        if memory_backend is None:
            self.memory = get_default_memory_backend(
                self.agent_root, lock_backend=self.lock_backend
            )
        else:
            self.memory = memory_backend

    # ────────────────────────────────────────────────────────────────────
    # Mandate crash recovery + reservation managers (#124 PR 3b)

    def _compute_effective_mandate_scopes(self) -> list[str]:
        """Return the mandate scope keys this agent participates in.

        Per spec/29 §"Crash recovery for reservations" + plan-subagent Risk 9:
        - ``agent:<name>`` is always present (the agent's own mandates.md).
        - ``project:<root_name>`` is added when a project-root mandates.md
          exists (cascade layout per spec/06).

        Returns a list of 1 or 2 scope strings. Never empty.
        """
        scopes: list[str] = [f"agent:{self.agent_root.name}"]
        # Project-root scope: present in cascade layouts where the agents_root
        # parent has a mandates.md (spec/06 + spec/29 §"Scope derivation").
        if self.cascade is not None:
            # cascade.project_root is the <system>/projects/<project>/ dir;
            # the mandates.md lives at its root level (sibling of agents/).
            project_mandates = self.cascade.project_root / "mandates.md"
            if project_mandates.exists():
                scopes.append(f"project:{self.cascade.project_root.name}")
        return scopes

    def _run_mandate_recovery_for_all_scopes(self) -> None:
        """Invoke crash recovery for each effective mandate scope at agent boot.

        Per spec/29 §"Crash recovery for reservations" + plan-subagent
        Risks 8 (invocation site = agent init) + 9 (multi-scope iteration).

        Iterates the effective scope set (agent:<name> always; project:<root>
        when project-root mandates.md exists) and invokes
        ``recover_orphan_reservations`` for each.

        Multi-process safety via LockBackend lease per spec/29 Risk B. Sibling
        replicas that don't hold the lease return 0 immediately — no startup
        cliff.

        No-op when ``mandate_backend`` is None or no mandates.md exists at
        any scope (``recover_orphan_reservations`` handles the empty-orphan
        list cheaply).

        Called from ``__init__`` BEFORE ``_load_config`` so orphans are
        reconciled before any mandate checks fire in this run. ``cascade`` is
        NOT yet set at this point in init order (cascade detection happens
        after profile load), so we can only recover ``agent:`` scope here;
        project-scope recovery happens in ``_init_mandate_reservation_managers``
        after cascade detection.
        """
        if self.mandate_backend is None:
            return
        # Only agent-scope available at this point (cascade not yet set).
        agent_scope = f"agent:{self.agent_root.name}"
        try:
            self.mandate_backend.recover_orphan_reservations(
                self.log_backend,
                agent_scope,
                lock_backend=self.lock_backend,
            )
        except Exception:
            _logger.exception("mandate recovery failed for scope %s", agent_scope)

    def _init_mandate_reservation_managers(self) -> None:
        """Instantiate per-scope MandateReservationManagers after _load_config.

        Called from ``__init__`` AFTER ``_load_config`` so
        ``judges_config.mandate_settings.reservation_ttl_s`` is resolved.
        Also runs project-scope crash recovery now that ``self.cascade``
        is set.

        Per spec/29 §"Cost reservation pattern" + plan-subagent Risk 9:
        one manager per scope (agent + optional project).
        """
        from .judge.mandate_reservations import MandateReservationManager

        # Resolve TTL from parsed judges.md (or default 60s).
        if self.judges_config is not None:
            ttl_s = self.judges_config.mandate_settings.reservation_ttl_s
        else:
            from .judges_md import MandateSettings

            ttl_s = MandateSettings().reservation_ttl_s

        scopes = self._compute_effective_mandate_scopes()

        # Project-scope recovery: runs now that cascade is detected.
        if self.mandate_backend is not None and len(scopes) > 1:
            project_scope = scopes[1]
            try:
                self.mandate_backend.recover_orphan_reservations(
                    self.log_backend,
                    project_scope,
                    lock_backend=self.lock_backend,
                )
            except Exception:
                _logger.exception("mandate recovery failed for scope %s", project_scope)

        for scope in scopes:
            self._mandate_reservation_managers[scope] = MandateReservationManager(
                self.log_backend,
                scope,
                ttl_s=ttl_s,
                agent_name=self.name,
            )

    def _verify_post_action(
        self,
        proposal: "ActionProposal",
        tool_result: "ToolCallResult",
    ) -> None:
        """Emit post-action verification lifecycle event.

        Per spec/29 §"Mandate lifecycle events" rows for
        ``mandate_action_verified`` / ``_diverged`` / ``_verification_unavailable``.

        Fires AFTER cost commit (spec/29 Risk 6). Uses EXECUTED
        ``tool_arguments`` (post-REVISE) for post-extraction — NOT
        ``tool_result.output`` (spec/29 Risk 10).

        No-op when:
        - proposal.action_class is not external_side_effect or irreversible.
        - proposal.authorization is None or doesn't cite a mandate.
        """
        from .judge.types import ActionClass

        if proposal.classification not in (
            ActionClass.EXTERNAL_SIDE_EFFECT,
            ActionClass.HIGH_RISK,
        ):
            return
        if (
            proposal.authorization is None
            or not proposal.authorization.granted_by.startswith("mandate:")
        ):
            return

        mandate_id: str = proposal.authorization.granted_by.removeprefix("mandate:")

        # Pre-extraction recorded at proposal time (PR 3a's #218).
        target_at_proposal = proposal.target_canonical

        # Post-extraction uses EXECUTED tool_arguments (Risk 10).
        # Same extractor lookup as proposal-time path.
        tool_def = self.tool_registry.get(proposal.tool_name)
        extractor_id = tool_def.target_extractor_id if tool_def is not None else None
        target_at_execution = self._target_extractors.extract(
            tool_name=proposal.tool_name,
            tool_arguments=proposal.tool_arguments,
            extractor_id=extractor_id,
            mcp_server=proposal.mcp_server,
        )

        # Determine verification_status per spec/29 lines 641-643.
        if target_at_proposal is None and target_at_execution is None:
            status = "unavailable"
            event_name = "mandate_action_verification_unavailable"
        elif target_at_proposal == target_at_execution:
            status = "match"
            event_name = "mandate_action_verified"
        else:
            status = "diverged"
            event_name = "mandate_action_diverged"

        # Emit via the existing _log() path — mirrors MandateCheck's
        # _emit_lifecycle_event shape: primitive="judgment", trigger=event_name.
        try:
            from .logs.types import RunRecord

            now_iso = datetime.now(timezone.utc).isoformat()
            record = RunRecord(
                ts=now_iso,
                run_id=self.run_id,
                primitive="judgment",
                status="ok",
                summary=(f"mandate post-action: {event_name} for {mandate_id!r}"),
                model="n/a",
                input_tokens=0,
                output_tokens=0,
                trigger=event_name,
                agent_name=self.name,
                mandate_id=mandate_id,
                extra={
                    "event": event_name,
                    "mandate_id": mandate_id,
                    "proposal_id": proposal.proposal_id,
                    "tool_name": proposal.tool_name,
                    "target_canonical_at_proposal": target_at_proposal,
                    "target_canonical_at_execution": target_at_execution,
                    "verification_status": status,
                },
            )
            self.log_backend.append(record)
        except Exception:
            _logger.warning(
                "agent %r: failed to emit mandate post-action lifecycle event "
                "%r for mandate_id=%r",
                self.name,
                event_name,
                mandate_id,
            )

    def _register_skill_tools(self) -> None:
        """Register load_skill and load_skill_file as built-in tools in the registry.

        Called once during __init__ when skills are present. Handlers close over
        ``self.skills`` so they work with the skills discovered at init time.
        """
        # Build lookup index by name for fast handler access
        skill_index: dict[str, SkillManifest] = {m.name: m for m in self.skills}
        skill_names = sorted(skill_index.keys())

        def _handle_load_skill(inp: dict) -> str:
            skill_name = inp.get("skill_name", "")
            manifest = skill_index.get(skill_name)
            if manifest is None:
                from .exceptions import ToolHandlerError

                raise ToolHandlerError(
                    f"Unknown skill {skill_name!r}. Available skills: {skill_names}"
                )
            return load_skill_body(manifest)

        def _handle_load_skill_file(inp: dict) -> str:
            skill_name = inp.get("skill_name", "")
            relative_path = inp.get("relative_path", "")
            manifest = skill_index.get(skill_name)
            if manifest is None:
                from .exceptions import ToolHandlerError

                raise ToolHandlerError(
                    f"Unknown skill {skill_name!r}. Available skills: {skill_names}"
                )
            return load_skill_referenced_file(manifest, relative_path)

        self.tool_registry.register(
            ToolDefinition(
                name="load_skill",
                description=(
                    "Loads the full instructions for a skill by name. "
                    "Use this when a skill listed in the system prompt is relevant "
                    "to the current task and you need its detailed guidance."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill to load (as listed in Available skills).",
                        }
                    },
                    "required": ["skill_name"],
                },
                handler=_handle_load_skill,
            )
        )

        self.tool_registry.register(
            ToolDefinition(
                name="load_skill_file",
                description=(
                    "Loads a supporting file referenced by a skill (one level deep from "
                    "the skill's SKILL.md). Use after calling load_skill when you need "
                    "extended reference material that was not included in the main body."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill that owns the file.",
                        },
                        "relative_path": {
                            "type": "string",
                            "description": (
                                "Path to the file relative to the skill directory "
                                "(e.g. 'reference.md', 'examples.md'). "
                                "Must be one level deep — no subdirectory traversal."
                            ),
                        },
                    },
                    "required": ["skill_name", "relative_path"],
                },
                handler=_handle_load_skill_file,
            )
        )

    def register_target_extractor(
        self, name: str, callable_: Callable[[dict], str | None]
    ) -> None:
        """Register a per-agent target extractor for mandate target matching.

        Binds a named callable to the agent's ``TargetExtractorRegistry``
        (spec/29 §"Target extraction", #124 PR 3a). Tool definitions reference
        the extractor by string ID via ``ToolDefinition.target_extractor_id``.
        The framework invokes the callable against ``tool_arguments`` at
        proposal-assembly time to populate ``ActionProposal.target_canonical``,
        which ``MandateCheck`` step 5 reads to enforce
        ``constraints.allowed_targets`` / ``blocked_targets``.

        **Registration order matters.** Call this method BEFORE calling
        ``tool_registry.register()`` for any tool that references ``name``
        via ``target_extractor_id``. If you register the tool first, the
        validation at ``tool_registry.register()`` time will fail with
        ``UnknownTargetExtractor`` (spec/29 §"Registration order discipline").

        Built-in heuristic extractors (``recipient_to``, ``recipient_field``,
        ``target_field``, ``url_field``, ``repository_field``,
        ``customer_id_field``, ``channel_id_field``) are pre-registered at
        agent construction — no need to register them again. Use this method
        for tools whose argument shape doesn't match the built-in heuristics.

        Raises ``ValueError`` if ``name`` is already registered (collision
        guard mirrors ``register_tool_registry_backend`` precedent — no silent
        overwrite). To replace a registered extractor (including a built-in),
        call ``self._target_extractors.replace(name, callable_)`` directly.

        Args:
            name: Unique string ID for this extractor. Must be non-empty and
                contain only lowercase alphanumeric characters plus underscore.
            callable_: A callable ``(dict) → str | None``. Receives the tool's
                ``tool_arguments`` dict; returns the canonical target string or
                ``None`` when no target can be extracted.

        Raises:
            ValueError: ``name`` is already registered or has an invalid format
                (not lowercase alphanumeric + underscore).
        """
        self._target_extractors.register(name, callable_)

    def register_cost_estimator(
        self, name: str, estimator: Callable[[dict], float]
    ) -> None:
        """Register a per-agent cost estimator for mandate external-budget projection.

        Binds a named callable to the agent's ``CostEstimatorRegistry``
        (spec/29 §"Validation steps" step 8, #124 PR 3b). Tool definitions
        reference the estimator by string ID via
        ``ToolDefinition.cost_estimator_id``. ``MandateCheck`` step 8 invokes
        the callable against ``tool_arguments`` to project the action's
        external cost against the mandate's ``*_external_usd`` cap budgets.

        **Registration order matters.** Call this BEFORE
        ``tool_registry.register()`` for any tool that references ``name``
        via ``cost_estimator_id``. If you register the tool first, the
        validation at ``tool_registry.register()`` time will fail with
        ``UnknownCostEstimator`` (spec/29 §"Registration order discipline";
        same fail-loud-at-registration discipline ``register_target_extractor``
        uses).

        Unlike ``register_target_extractor``, this registry starts EMPTY —
        no built-in estimators ship by default. External-cost projection is
        too tool-specific for "guess from arg shape" defaults; tools with
        static pricing use ``ToolDefinition.expected_external_cost_usd``,
        tools with dynamic pricing register an explicit estimator here.

        Args:
            name: Unique string ID for this estimator. Must be non-empty and
                contain only lowercase alphanumeric characters plus underscore.
            estimator: A callable ``(dict) → float``. Receives the tool's
                ``tool_arguments`` dict; returns the projected USD external
                cost. Return ``float('inf')`` to signal "cannot project for
                this arg shape" — caller treats as ``mandate_external_cost_unprojectable``.

        Raises:
            ValueError: ``name`` is already registered or has an invalid format.
        """
        self._cost_estimators.register(name, estimator)

    @staticmethod
    def _generate_run_id() -> str:
        # Append a uuid4 fragment to guarantee uniqueness under concurrent HTTP
        # load where two requests can execute this line in the same microsecond.
        # spec/37 MUST 8 — unique run_id per call(), including concurrent calls.
        return f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid4().hex[:8]}"

    @staticmethod
    def _capture_tool_definitions(model: str) -> "list[LLMToolDefinition] | None":
        """Return the atomic_capture tool definition as canonical ``LLMToolDefinition``.

        Returns None for providers without tool-call support — the agent then
        falls back to Path 2 fenced-block parsing only. Today every supported
        provider (Anthropic, OpenAI, Moonshot) has tool calls, so the only
        None path is for unrecognized model prefixes.

        Returns a single-element ``list[LLMToolDefinition]``. Backends
        translate canonical → provider format inside their ``call()`` —
        the agent layer no longer branches on ``model.startswith`` (the
        codex P1 fix from the #87 LLMBackend plan, landed in PR 2.5).
        """
        if (
            model.startswith("claude-")
            or model.startswith("gpt-")
            or model.startswith("moonshot/")
        ):
            return [_capture.canonical_tool_definition()]
        return None

    def _all_tool_definitions(self, model: str) -> "list[LLMToolDefinition] | None":
        """Return all tool definitions (atomic_capture + custom tools) as canonical.

        Includes:
        - atomic_capture (built-in, always included for supported providers)
        - All tools registered in self.tool_registry (operator-supplied)

        Returns ``list[LLMToolDefinition]`` ready to hand to any backend's
        ``call()`` (the backend translates to provider format internally).
        Returns None for providers without tool-call support — the agent
        then falls back to Path 2 fenced-block parsing only.
        """
        if (
            model.startswith("claude-")
            or model.startswith("gpt-")
            or model.startswith("moonshot/")
        ):
            defs = [_capture.canonical_tool_definition()]
            defs.extend(self.tool_registry.to_canonical_definitions())
            # Judge layer side-channel marker (spec/28 + #112 PR 2a).
            # Always included alongside atomic_capture for supported
            # providers. The actor emits atomic_action in the same turn
            # as any side-effectful tool call to justify it; if the
            # judge layer is disabled (no judges.md, no env var) the
            # markers are silently ignored at proposal-assembly time.
            from .judge.atomic_action import canonical_tool_definition as _action_def

            defs.append(_action_def())
            return defs
        return None

    # ────────────────────────────────────────────────────────────
    # Judge layer (#112 PR 2a — opt-in dispatch)

    def _judge_enabled(self) -> bool:
        """Return True when the judge layer should run for this agent.

        Per CLAUDE.md rule #14 (backward compatibility by default), the
        judge dispatch is opt-in. Existing v0.13.0 deployments that
        pip-upgrade to this version see today's behavior unchanged
        unless either signal below is set.

        Two signals enable dispatch:

        1. ``judges.md`` exists in ``agent_root``. PR 2a treats mere
           presence as opt-in; PR 3's parser layers on operator
           configuration. Operators authoring the file in PR 2a get
           PolicyJudge coverage with framework defaults.
        2. ``AGENT_JUDGE_ENABLED`` environment variable is truthy.
           Escape hatch for experiments / smoke tests before
           authoring a judges.md.
        """
        if os.environ.get("AGENT_JUDGE_ENABLED", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return True
        if (self.agent_root / "judges.md").exists():
            return True
        # Inherited project-floor judges.md counts as opt-in (Codex
        # round-2 P1) — without this check, a cascade project floor
        # would parse but the gate would keep dispatch off, leaving
        # the floor unenforced unless every delegate also authors its
        # own judges.md.
        if getattr(self, "judges_config", None) is not None:
            return True
        return False

    def _resolve_classification(self, tool_name: str) -> tuple[str, str]:
        """Look up the per-tool ``ActionClass`` value for proposal
        assembly. Returns ``(class_value, classification_source)``.

        Lookup order:

        1. ``ToolDefinition.classification`` on the registered tool
           (set in code when the tool was registered). Source =
           ``"tools.py"``.
        2. ``self._tool_classifications`` from
           ``tools.md ## Tool classification`` section. Source =
           ``"tools.md"``.
        3. Default to ``"external_side_effect"`` per spec/28's safe
           default. Source = ``"default_unknown"``.

        Returns the string value (not a typed enum) so the caller —
        which is in agent.py — doesn't introduce a runtime dependency
        on the judge module beyond what it already has.
        """
        registered = self.tool_registry.get(tool_name)
        if registered is not None and registered.classification:
            return registered.classification, "tools.py"
        mapped = self._tool_classifications.get(tool_name)
        if mapped is not None:
            return mapped, "tools.md"
        return "external_side_effect", "default_unknown"

    def _default_class_policy_snapshot(self):
        """Return the default ``ClassPolicySnapshot`` used by PR 2a
        when no ``judges.md`` parser is available.

        Every class defaults to ``JUDGE_REQUIRED`` — judge runs and
        enforces. PR 3's ``judges.md`` parser reads operator overrides
        per class.
        """
        from .judge.types import ClassPolicySnapshot, ClassPolicyValue

        return ClassPolicySnapshot(
            read_only=ClassPolicyValue.JUDGE_REQUIRED,
            reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
            external_side_effect=ClassPolicyValue.JUDGE_REQUIRED,
            high_risk=ClassPolicyValue.JUDGE_REQUIRED,
            source={
                "read_only": "default",
                "reversible_write": "default",
                "external_side_effect": "default",
                "high_risk": "default",
            },
        )

    def _resolved_validation_mode(self) -> str:
        """Return the validation mode threaded to
        ``_revise.validate_amended_args`` from the agent's parsed
        ``JudgesConfig`` (PR 5b of #112).

        Pulls ``judges_config.validation`` when ``judges.md`` was
        parsed; falls back to ``"weakened"`` when the agent opted
        into the judge layer via ``AGENT_JUDGE_ENABLED=1`` without
        authoring a ``judges.md`` (env-var quickstart path — strict
        validation requires intentional opt-in via the file).

        Extracted as a helper so the two REVISE call sites
        (``_run_ensemble`` for judge-driven REVISE, ``_process_
        operator_revise`` for operator-driven resolution) cannot
        drift on default-fallback semantics.
        """
        if self.judges_config is None:
            return "weakened"
        return self.judges_config.validation

    def _ensure_policy_judge(self):
        """Lazy-construct and cache the default ``PolicyJudge`` for
        this agent. Returns the cached instance on subsequent calls.

        PolicyJudge is registered against this agent's tools.md
        (write paths + read-only paths + the raw text for
        policy_version computation).
        """
        if self._policy_judge is not None:
            return self._policy_judge
        from .judge.rules import make_default_policy_judge

        # Read the tools.md content for policy_version derivation.
        # Use the resolved tools.md (cascade-aware) when available; fall
        # back to the agent_root file otherwise.
        tools_md_text = ""
        if self.cascade:
            _, tools_md_text = _cascade.resolve_tools_md(self.cascade)
        else:
            tools_md_path = self.agent_root / "tools.md"
            if tools_md_path.exists():
                tools_md_text = tools_md_path.read_text(encoding="utf-8")
        self._policy_judge = make_default_policy_judge(
            tools_md_text=tools_md_text,
            allowed_write_paths=[Path(p) for p in (self.config.write_paths or [])],
            read_only_paths=[Path(p) for p in (self.config.read_only_paths or [])],
        )
        return self._policy_judge

    def _ensure_mandate_check(self):
        """Lazy-construct and cache the ``MandateCheck`` judge specialist
        (spec/29 + #124 PR 3a). Returns the cached instance on subsequent
        calls.

        MandateCheck is a sibling of PolicyJudge in the composition
        ``[PolicyJudge, MandateCheck, LLMCatchAll]`` per spec/29 line 392.
        Both rule-engine, both always-on, both fail-fast. MandateCheck
        pass-throughs (ALLOW immediately) on proposals that don't cite a
        mandate so the ensemble overhead is bounded by an Authorization
        field check on non-mandate-citing actions.
        """
        if getattr(self, "_mandate_check", None) is not None:
            return self._mandate_check
        from .judge.mandate_check import MandateCheck
        from .judge.mandate_state import MandateStateManager

        # Scope derivation: agent-local for now (project-root mandate
        # resolution lands in PR 4 alongside cross-agent budget aggregation).
        scope = f"agent:{self.agent_root.name}"

        # Mandate settings: prefer parsed judges.md config; fall back to
        # default-fill via MandateSettings() factory (zero-operator-action
        # upgrade per spec/29 + the #218 prep).
        if self.judges_config is not None:
            mandate_settings = self.judges_config.mandate_settings
        else:
            from .judges_md import MandateSettings

            mandate_settings = MandateSettings()

        state_manager = MandateStateManager(
            mandate_backend=self.mandate_backend,
            scope=scope,
        )

        self._mandate_check = MandateCheck(
            mandate_backend=self.mandate_backend,
            scope=scope,
            target_extractor_registry=self._target_extractors,
            mandate_state_manager=state_manager,
            mandate_settings=mandate_settings,
            log_backend=self.log_backend,
            policy_effective_caps=(
                self._policy_snapshot_this_call.effective_caps
                if self._policy_snapshot_this_call is not None
                else None
            ),
        )
        return self._mandate_check

    def _take_policy_snapshot(self):
        """Take a frozen per-call Policy snapshot at call() entry (#89 PR 3a + 3b).

        Called once at the START of ``call()`` (alongside the ``_helpers_this_run``
        reset).  All Policy consumption sites within the same ``call()`` read
        from this snapshot — operator edits to ``policy.md`` mid-call are
        deferred to the NEXT call (Premise 3: predictability over freshness
        within the call).

        Returns a ``PolicySnapshotForCall`` with:

        - ``effective_caps``: MIN-composed Policy caps for THIS agent (PR 3a).
        - ``cache_ttl_s``: backend capability snapshot at call-entry time.
        - ``tool_allow_fn`` / ``mcp_allow_fn``: closures bound to a
          per-snapshot result cache + the backend reference + the agent name
          (all captured via default-arg). The cache makes Premise 3's
          "frozen at call entry" promise visible to consumers: a multi-turn
          loop that consults the same tool/server name N times queries the
          backend ONCE per snapshot. Default-arg capture also pins the
          backend reference, so a later ``self.policy_backend`` reassignment
          (rare) cannot contaminate the snapshot.
        - ``model_override``: resolved ONCE at snapshot time via
          ``get_effective_model``; ``None`` means "no Policy opinion, defer to
          ``model.md``." PR 3b.
        - ``enforce_noncap``: env-var value (``ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP``)
          read ONCE at call entry; non-cap denials emit
          ``enforced=enforce_noncap`` and either block (True) or proceed
          (False, the PR 3b default).

        On any snapshot-time backend error (``get_effective_caps`` /
        ``get_effective_model``), falls back to no-opinion values and logs a
        warning rather than aborting the call. The closures themselves treat
        per-call backend failures as "allow" (log-only mode is fail-open;
        the fail-closed mode tracked at issue #242 is a separate surface).
        """
        from .policy.types import (
            CostCaps,
            PolicySnapshotForCall,
            _read_enforce_noncap_flag,
        )

        try:
            effective_caps = self.policy_backend.get_effective_caps(self.name)
            cache_ttl_s = self.policy_backend.capabilities().cache_ttl_s
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "PolicyBackend.get_effective_caps(%r) failed — "
                "falling back to no-opinion CostCaps(): %s: %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            effective_caps = CostCaps()
            cache_ttl_s = None

        try:
            model_override = self.policy_backend.get_effective_model(self.name)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "PolicyBackend.get_effective_model(%r) failed — "
                "falling back to no-opinion (None): %s: %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            model_override = None

        # Per-snapshot result cache + default-arg capture together honor the
        # frozen-snapshot contract (Premise 3): each (tool_name) or
        # (server_name) is queried at most once per snapshot, and the backend
        # reference is pinned even if ``self.policy_backend`` is later
        # reassigned. The cache dict is a fresh instance per call() so it
        # cannot leak state across calls.
        def tool_allow_fn(
            tool_name: str,
            _cache: dict[str, bool] = {},
            _backend=self.policy_backend,
            _agent_name=self.name,
        ) -> bool:
            if tool_name in _cache:
                return _cache[tool_name]
            try:
                result = _backend.is_tool_allowed(_agent_name, tool_name)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "PolicyBackend.is_tool_allowed(%r, %r) failed — "
                    "treating as allow (log-only mode fail-open; "
                    "fail-closed tracked at #242): %s: %s",
                    _agent_name,
                    tool_name,
                    type(exc).__name__,
                    exc,
                )
                result = True
            _cache[tool_name] = result
            return result

        def mcp_allow_fn(
            server_name: str,
            _cache: dict[str, bool] = {},
            _backend=self.policy_backend,
            _agent_name=self.name,
        ) -> bool:
            if server_name in _cache:
                return _cache[server_name]
            try:
                result = _backend.is_mcp_server_allowed(_agent_name, server_name)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "PolicyBackend.is_mcp_server_allowed(%r, %r) failed — "
                    "treating as allow (log-only mode fail-open; "
                    "fail-closed tracked at #242): %s: %s",
                    _agent_name,
                    server_name,
                    type(exc).__name__,
                    exc,
                )
                result = True
            _cache[server_name] = result
            return result

        return PolicySnapshotForCall(
            effective_caps=effective_caps,
            cache_ttl_s=cache_ttl_s,
            tool_allow_fn=tool_allow_fn,
            mcp_allow_fn=mcp_allow_fn,
            model_override=model_override,
            enforce_noncap=_read_enforce_noncap_flag(),
        )

    def _ensure_llm_judge(self):
        """Lazy-construct and cache the default ``LLMJudgeBackend`` for
        this agent (#112 PR 2b). Returns the cached instance or ``None``
        when the LLM backend isn't reachable (no key for the default
        judge model).

        Per spec/28's correlated-judgment mitigation, the default model
        is ``gpt-5-nano`` (OpenAI) so the judge family differs from the
        default Anthropic actor. Operators in Claude-only deployments
        get ``None`` here — the ensemble runs PolicyJudge only.

        Distinct from ``_ensure_policy_judge`` because ``None`` is a
        legitimate cached result (no key configured). Tracks
        construction state via ``_llm_judge_constructed`` so a
        ``None`` cache doesn't re-attempt on every dispatch.
        """
        if self._llm_judge_constructed:
            return self._llm_judge
        self._llm_judge_constructed = True
        from .judge.llm import make_default_llm_judge

        # Read tools.md text for policy_version derivation (cascade-aware).
        tools_md_text = ""
        if self.cascade:
            _, tools_md_text = _cascade.resolve_tools_md(self.cascade)
        else:
            tools_md_path = self.agent_root / "tools.md"
            if tools_md_path.exists():
                tools_md_text = tools_md_path.read_text(encoding="utf-8")
        # judges.md text is None in PR 2b (parser lands in PR 3) —
        # compute_policy_version writes ``judges.md@sha256:absent``.
        self._llm_judge = make_default_llm_judge(
            tools_md_text=tools_md_text,
            judges_md_text=None,
        )
        return self._llm_judge

    # ────────────────────────────────────────────────────────────
    # Escalation polling (#112 PR 3b)

    def poll_escalations(self) -> list:
        """Scan the escalation queue for operator resolutions and
        execute / audit them.

        Called from the top of ``agent.call()`` once per iteration (the
        first iteration if the throttle window has elapsed). Returns
        the list of ``ResolutionEvent``s that fired this poll cycle
        (mostly useful for tests; production callers ignore the value).

        **Standalone-invocation caveat (Codex round-1 P2-1).** This
        method is public and operators may call it directly (e.g., from
        a future ``atomic-agents poll-escalations`` CLI). When invoked
        standalone (NOT via ``agent.call()``), the MCP client pool is
        NOT initialized: ``call()`` is what wires it up after the cost
        gate. If an Approved escalation's tool is an MCP tool, the
        tool registry lookup returns None and the resolution is
        recorded as ``approved_stale_tool_definition`` — safe (fail
        closed) but misleading (the tool isn't stale; the framework
        just hasn't loaded MCP yet). Wire MCP init into a standalone
        CLI before relying on Approved MCP-tool execution; see #166.

        Behavior:
        - Throttled by ``judges_config.escalation.resolution_poll_cycle_seconds``
          (default 60s). The .last-poll mtime marker lives in the
          escalation destination directory.
        - For Approved resolutions: re-verifies the tool_definition_hash
          against the current tool registry, refuses execution on
          mismatch (``approved_stale_tool_definition``), then executes
          the bound action via the loaded tool registry. Result charged
          to the actor's original ``parent_run_id`` (cost_source="actor").
        - For Denied / Redacted resolutions: audits without executing.
        - For Auto-decided resolutions: applies the operator's
          fallback_on_timeout policy.
        - For Body-tampered files: refuses execution; emits
          ``proposal_body_tampered`` audit.
        - For Revised resolutions: PR 3b treats as Denied (REVISE flow
          ships in PR 3c). The audit event records the operator's
          intent so PR 3c can surface stale-deferred revisions.
        """
        from .judge import escalation as _esc

        if self.judges_config is None:
            return []
        cfg = self.judges_config.escalation
        # Throttle: skip if recent poll happened within cycle window.
        if _esc.is_within_throttle(
            agent_root=self.agent_root,
            judges_config_escalation=cfg,
        ):
            return []
        try:
            events = _esc.poll_resolutions(
                agent_root=self.agent_root,
                judges_config_escalation=cfg,
                log_warning=lambda msg: _logger.warning(
                    "agent %r poll_escalations: %s", self.name, msg
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "agent %r: poll_escalations raised; skipping cycle: %s",
                self.name,
                exc,
            )
            # Do NOT touch_last_poll on error (Codex round-1 P1-5).
            # Persistent failures (e.g., disk full) should not silently
            # throttle subsequent retries.
            return []
        # Only update throttle marker after a successful scan. Errors
        # leave .last-poll untouched so the next call() retries.
        _esc.touch_last_poll(
            agent_root=self.agent_root,
            judges_config_escalation=cfg,
        )

        for event in events:
            # /ship Step 11 adversarial P1 (PR 5b): exception isolation
            # per-event. Pre-fix, one event raising an uncaught
            # exception (OSError, KeyError on malformed frontmatter,
            # JudgeError class the per-path handlers don't yet catch)
            # silently swallowed every subsequent event in the same
            # poll cycle. PR 5b's strict-mode validation widens the
            # raise surface (JudgePolicyInvalid in particular), making
            # the poison-pill failure mode load-bearing to fix here.
            try:
                self._process_resolution_event(event)
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "agent %r: resolution event for proposal_id=%r "
                    "raised %s; continuing to next event",
                    self.name,
                    getattr(event.frontmatter, "proposal_id", "<unknown>"),
                    type(exc).__name__,
                )
                continue
        return events

    def _process_resolution_event(self, event) -> None:
        """Handle one ResolutionEvent: emit JSONL audit + execute if
        Approved + tool_definition_hash matches the current registry.
        """
        from .judge import escalation as _esc
        from .judge.proposal import compute_tool_definition_hash

        decision = event.decision
        proposal = event.proposal
        fm = event.frontmatter
        enforcement = event.enforcement_action

        # Re-verify tool_definition_hash for Approved cases. The
        # current tool registry may have evolved since the PENDING was
        # written (tool dropped, schema changed). Mismatch → refuse,
        # promote enforcement to approved_stale_tool_definition.
        if decision is _esc.ResolutionDecision.APPROVED:
            registered = self.tool_registry.get(proposal.tool_name)
            if registered is None:
                enforcement = "approved_stale_tool_definition"
                stale_reason = (
                    f"tool {proposal.tool_name!r} no longer registered "
                    "at execution time"
                )
            else:
                current_hash = compute_tool_definition_hash(
                    proposal.tool_name,
                    registered.input_schema,
                    registered.handler,
                )
                if current_hash != proposal.tool_definition_hash:
                    enforcement = "approved_stale_tool_definition"
                    stale_reason = (
                        "tool_definition_hash mismatch: PENDING="
                        f"{proposal.tool_definition_hash[:12]}..., "
                        f"current={current_hash[:12]}..."
                    )
                else:
                    stale_reason = None
        else:
            stale_reason = None

        # Build the RESOLVED audit record. We synthesize a Judgment
        # so the JSONL line has the standard judge-event shape.
        from .judge.backend import Judgment, JudgmentOutcome
        from .judge.types import ProposalBinding

        # P1 #1: OPERATOR_REVISED's "resolved" line is intent-recorded,
        # not execution-recorded. _process_operator_revise emits the
        # actual operator_revise_executed line AFTER the handler runs.
        # Promote enforcement to operator_revise_pending here so the
        # resolved-event line doesn't claim execution-already-happened.
        if decision is _esc.ResolutionDecision.OPERATOR_REVISED and (
            enforcement == "operator_revise_executed"
        ):
            enforcement = "operator_revise_pending"
        judgment = Judgment(
            outcome=(
                JudgmentOutcome.ALLOW
                if decision is _esc.ResolutionDecision.APPROVED and stale_reason is None
                else JudgmentOutcome.BLOCK
            ),
            reason=(stale_reason or event.reason or decision.value),
            judge_id=event.operator,
            policy_version=fm.policy_version,
        )
        binding = ProposalBinding(
            tool_call_id=proposal.tool_call_id,
            tool_definition_hash=proposal.tool_definition_hash,
            arguments_hash=proposal.arguments_hash,
        )
        # Pseudo tool_use payload for the event builder.
        tool_use_stub = {"name": proposal.tool_name, "id": proposal.tool_call_id}
        record = self._build_judgment_event_dict(
            proposal=proposal,
            tool_use=tool_use_stub,
            judgment=judgment,
            enforcement_action=enforcement,
            binding=binding,
        )
        record["resolved_at"] = event.resolved_at
        record["resolution_operator"] = event.operator
        record["escalation_file"] = str(event.file_path)
        record["escalation_queue_id"] = fm.proposal_id
        record["trigger"] = "escalation_resolved"
        self._log(record)

        # Execute the bound action for Approved + fresh tool. The
        # ToolCallResult is appended to a deferred_execution JSONL
        # line; no actor-loop replay happens (the original run is
        # closed). cost_source="actor" keeps the spend on the
        # proposing actor's ledger per the operator-approval-as-consent
        # discipline.
        if decision is _esc.ResolutionDecision.APPROVED and stale_reason is None:
            try:
                tool_use_payload = {
                    "name": proposal.tool_name,
                    "id": proposal.tool_call_id,
                    "input": proposal.tool_arguments,
                }
                tool_result = self.tool_registry.execute(tool_use_payload)
                self._log(
                    {
                        "trigger": "escalation_deferred_execution",
                        "parent_run_id": fm.parent_run_id,
                        "escalation_queue_id": fm.proposal_id,
                        "tool_name": tool_result.tool_name,
                        "latency_ms": tool_result.latency_ms,
                        "error": tool_result.error,
                        "cost_source": "actor",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "agent %r: escalation_deferred_execution failed for "
                    "proposal_id=%r: %s",
                    self.name,
                    proposal.proposal_id,
                    exc,
                )
                self._log(
                    {
                        "trigger": "escalation_deferred_execution",
                        "parent_run_id": fm.parent_run_id,
                        "escalation_queue_id": fm.proposal_id,
                        "tool_name": proposal.tool_name,
                        "error": f"{type(exc).__name__}: {exc}",
                        "cost_source": "actor",
                    }
                )

        # PR 3c: operator-revise execution path.
        if decision is _esc.ResolutionDecision.OPERATOR_REVISED:
            self._process_operator_revise(event)

    def _process_operator_revise(self, event) -> None:
        """Execute an operator's Revised resolution.

        Flow:
        1. amend_proposal with the operator's amendment (already
           parsed in escalation._claim_operator_resolution).
        2. validate_amended_args + enforce_amended_write_paths.
        3. Gate on **amended.classification** (Codex round-1 P1-4 —
           NOT original.classification — otherwise the operator can
           swap tool_name to upgrade reversible_write → delete_files
           and skip re-judge):
           - amended.classification == high_risk → re-judge through
             a fresh ensemble; only execute if ALLOW.
           - other classes → schema/policy validation alone is
             sufficient; execute on success.
        4. Execute via tool_registry; emit deferred_execution audit
           line with cost_source="actor".
        5. Invalid amendment → enforcement promoted to
           operator_revise_invalid_amendment via escalation.py; this
           method skips execution but still emits the audit record
           (already emitted above in the synthesized Judgment flow).
        """
        from .judge import _revise as _rv
        from .judge.types import ActionClass
        from .exceptions import JudgeAmendedProposalRejected, JudgePolicyInvalid

        amendment = event.amendment
        fm = event.frontmatter
        original = event.proposal

        if amendment is None:
            # operator_revise_invalid_amendment — audit already emitted.
            return

        try:
            amended = _rv.amend_proposal(
                original=original,
                amendment=amendment,
                tool_registry=self.tool_registry,
                tool_classifications=self._tool_classifications,
            )
            _rv.validate_amended_args(
                amended,
                self.tool_registry,
                agent_name=self.name,
                validation_mode=self._resolved_validation_mode(),
            )
            _rv.enforce_amended_write_paths(
                amended,
                write_paths=list(self.config.write_paths or []),
                read_only_paths=list(self.config.read_only_paths or []),
            )
        except (JudgeAmendedProposalRejected, JudgePolicyInvalid) as exc:
            # PR 5b /ship Step 9.1 (testing+security cross-confirmed):
            # strict-mode validation can raise BOTH exception classes.
            # JudgeAmendedProposalRejected = the amendment itself is
            # malformed (per-amendment refusal — the original
            # behavior). JudgePolicyInvalid = the registered tool's
            # ``input_schema`` is malformed (operator authoring bug).
            # In the operator-revise path there is no failure_policy
            # injection point — the operator IS the trust anchor and
            # the safe outcome on either exception is "do not execute
            # the amended action and emit an audit record." Pre-fix
            # JudgePolicyInvalid was uncaught here and propagated out
            # of ``poll_escalations``, killing ``agent.call()``.
            _logger.warning(
                "agent %r: operator_revise validation failed for proposal_id=%r: %s",
                self.name,
                fm.proposal_id,
                exc,
            )
            self._log(
                {
                    "trigger": "escalation_operator_revise_invalid_amendment",
                    "parent_run_id": fm.parent_run_id,
                    "escalation_queue_id": fm.proposal_id,
                    "tool_name": original.tool_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return

        # Gate on AMENDED classification (Codex round-1 P1-4 fix).
        re_judged = amended.classification == ActionClass.HIGH_RISK
        if re_judged:
            # Build the same config the agent uses at first-dispatch
            # time. Reuses _dispatch_with_judge's config-resolution
            # logic by constructing a synthetic tu/markers, but we
            # skip atomic_action marker validation since the operator
            # is the trust anchor here.
            if self.judges_config is not None:
                class_policy = self.judges_config.class_policy
                timeout_ms = self.judges_config.timeout_ms
                judge_budget = self.judges_config.budget
                escalation_cfg = self.judges_config.escalation
                flat_failure_policy = dict(
                    self.judges_config.failure_policy[ActionClass.EXTERNAL_SIDE_EFFECT]
                )
                backend_name = self.judges_config.default_backend
            else:
                class_policy = self._default_class_policy_snapshot()
                from .judge.types import EscalationConfig as _EC
                from .judge.types import BudgetConfig as _BC

                timeout_ms = 5000
                judge_budget = _BC()
                escalation_cfg = _EC()
                flat_failure_policy = {
                    "JudgeUnavailable": "block",
                    "JudgePolicyInvalid": "block",
                    "JudgeBudgetExhausted": "block",
                    "JudgeProposalInvalid": "block",
                    "JudgeAmendedProposalRejected": "block",
                }
                backend_name = "ensemble"

            from .judge.types import ProposalBinding

            binding = ProposalBinding(
                tool_call_id=amended.tool_call_id,
                tool_definition_hash=amended.tool_definition_hash,
                arguments_hash=amended.arguments_hash,
            )
            tu_stub = {
                "name": amended.tool_name,
                "id": amended.tool_call_id,
                "input": amended.tool_arguments,
            }
            allow, events, queue_id = self._run_ensemble(
                proposal_obj=amended,
                tu=tu_stub,
                binding=binding,
                class_policy=class_policy,
                timeout_ms=timeout_ms,
                judge_budget=judge_budget,
                escalation_cfg=escalation_cfg,
                flat_failure_policy=flat_failure_policy,
                backend_name=backend_name,
                revise_iteration=0,
                original_proposal=original,
            )
            for ev in events:
                ev["trigger"] = "escalation_operator_revise_re_judge"
                ev["escalation_queue_id"] = fm.proposal_id
                ev["original_proposal_id"] = original.proposal_id
                ev["re_judged"] = True
                # P1 #2: re-judge audit events must link to the
                # ORIGINAL actor's run, not the poller's. cost_source
                # discipline + forensic chains break if these get
                # bound to whichever agent.call() happened to fire
                # the poll.
                ev["parent_run_id"] = fm.parent_run_id
                self._log(ev)
            if not allow:
                _logger.info(
                    "agent %r: operator_revise high_risk re-judge BLOCKed "
                    "for proposal_id=%r (queue_id=%r); refusing execution",
                    self.name,
                    original.proposal_id,
                    fm.proposal_id,
                )
                return
        else:
            re_judged = False

        # Execute the amended bound action.
        try:
            tool_use_payload = {
                "name": amended.tool_name,
                "id": amended.tool_call_id,
                "input": amended.tool_arguments,
            }
            tool_result = self.tool_registry.execute(tool_use_payload)
            self._log(
                {
                    "trigger": "escalation_operator_revise_executed",
                    "parent_run_id": fm.parent_run_id,
                    "escalation_queue_id": fm.proposal_id,
                    "original_proposal_id": original.proposal_id,
                    "amended_proposal_id": amended.proposal_id,
                    "tool_name": tool_result.tool_name,
                    "latency_ms": tool_result.latency_ms,
                    "error": tool_result.error,
                    "re_judged": re_judged,
                    "cost_source": "actor",
                }
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "agent %r: operator_revise execution failed for proposal_id=%r: %s",
                self.name,
                original.proposal_id,
                exc,
            )
            self._log(
                {
                    "trigger": "escalation_operator_revise_executed",
                    "parent_run_id": fm.parent_run_id,
                    "escalation_queue_id": fm.proposal_id,
                    "original_proposal_id": original.proposal_id,
                    "amended_proposal_id": amended.proposal_id,
                    "tool_name": amended.tool_name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "re_judged": re_judged,
                    "cost_source": "actor",
                }
            )

    def _dispatch_with_judge(
        self,
        tu: dict,
        atomic_action_markers: dict[str, dict],
    ):
        """Run the judge ensemble against one tool_use. Returns
        ``(allow: bool, events: list[dict])`` — one JudgmentEvent
        record per invoked judge (the caller logs each verbatim per
        spec/28 §"Audit shape").

        Spec/28 §"Where the judge sits in agent.call()" places this
        between LLM tool_use parsing and tool handler dispatch.

        PR 2b ensemble: ``PolicyJudge`` (rule engine) first; if
        ALLOW, then ``LLMJudgeBackend`` if registered. First BLOCK in
        the ensemble short-circuits remaining judges (spec/28:694
        "If it blocks, the ensemble blocks — no LLM cost incurred").
        PR 3 reads ordering from ``judges.md``.
        """
        from .judge import proposal as _proposal_mod
        from .judge.backend import JudgmentOutcome
        from .judge.types import (
            ActionClass,
            BudgetConfig,
            EscalationConfig,
            ProposalBinding,
        )
        from .exceptions import JudgeProposalInvalid

        tool_name = tu.get("name", "")
        tool_call_id = tu.get("id", "")
        cls_str, cls_source = self._resolve_classification(tool_name)
        try:
            classification = ActionClass(cls_str)
        except ValueError:
            classification = ActionClass.EXTERNAL_SIDE_EFFECT
            cls_source = "default_unknown"

        # Resolve the side-channel marker, if any.
        marker = atomic_action_markers.get(tool_call_id)

        # Resolve handler / tool_definition_hash inputs.
        registered = self.tool_registry.get(tool_name)
        input_schema = registered.input_schema if registered else {}
        handler = registered.handler if registered else None
        tdef_hash = _proposal_mod.compute_tool_definition_hash(
            tool_name,
            input_schema,
            handler,
        )

        # Build the proposal. JudgeProposalInvalid bubbles to BLOCK via
        # the failure-policy default (spec/28:567).
        try:
            proposal_obj = _proposal_mod.assemble_proposal(
                tu,
                marker,
                classification=classification,
                classification_source=cls_source,
                tool_definition_hash=tdef_hash,
                actor_agent=self.name,
                actor_run_id=self.run_id,
                actor_model_id=getattr(self.config, "model", None),
            )
        except JudgeProposalInvalid as exc:
            # Fail-closed per spec/28. Synthesize a BLOCK judgment and
            # a JudgmentEvent so the audit trail records the refusal.
            # Single-event return — proposal-assembly failure means no
            # ensemble judges ran.
            from .judge.backend import Judgment

            judgment = Judgment(
                outcome=JudgmentOutcome.BLOCK,
                reason=f"JudgeProposalInvalid: {exc}",
                judge_id="framework",
                policy_version="unimplemented",
            )
            event = self._build_judgment_event_dict(
                proposal=None,
                tool_use=tu,
                judgment=judgment,
                enforcement_action="block_executed",
                binding=ProposalBinding(
                    tool_call_id=tool_call_id,
                    tool_definition_hash=tdef_hash,
                    # Empty sentinel — proposal-assembly failed so no
                    # canonical args hash exists. ProposalBinding's
                    # str-typed field carries an empty string rather
                    # than a non-hex string per round-2 review (the
                    # "proposal_assembly_failed" string violated the
                    # implicit sha256-hex contract that audit-log
                    # tooling relies on). Failure reason lives in
                    # ``judgment.reason`` and on ``event["judgment_reason"]``.
                    arguments_hash="",
                ),
            )
            return False, [event], None

        # Build the JudgmentContext once; both judges see the same
        # context per spec/28 idempotency invariants. When judges.md
        # was parsed at load time (PR 3a), use its values; otherwise
        # fall back to the PR 2a hardcoded defaults so existing
        # deployments without judges.md keep working.
        if self.judges_config is not None:
            class_policy = self.judges_config.class_policy
            timeout_ms = self.judges_config.timeout_ms
            judge_budget = self.judges_config.budget
            escalation_cfg = self.judges_config.escalation
            # JudgeRuntimeConfig.failure_policy stays a flat
            # ``dict[str, str]`` per its existing Protocol-side
            # consumer; per-class lookups happen on
            # ``JudgesConfig.failure_policy_for`` separately. For the
            # context payload, expose the external_side_effect
            # bucket (most-common class) so any backend reading the
            # flat field gets a reasonable default. Per-class
            # enforcement lives in agent.py post-judge.
            flat_failure_policy = dict(
                self.judges_config.failure_policy[ActionClass.EXTERNAL_SIDE_EFFECT]
            )
            backend_name = self.judges_config.default_backend
        else:
            class_policy = self._default_class_policy_snapshot()
            timeout_ms = 5000
            judge_budget = BudgetConfig()
            escalation_cfg = EscalationConfig()
            flat_failure_policy = {
                "JudgeUnavailable": "block",
                "JudgePolicyInvalid": "block",
                "JudgeBudgetExhausted": "block",
                "JudgeProposalInvalid": "block",
                "JudgeAmendedProposalRejected": "block",
            }
            backend_name = "ensemble"

        # Build the initial JudgmentContext + Binding. PR 3c factors
        # the ensemble loop into ``_run_ensemble`` so REVISE outcomes
        # can recurse against the amended proposal with a fresh
        # context derived from the amended classification / tool name.
        binding = ProposalBinding(
            tool_call_id=tool_call_id,
            tool_definition_hash=tdef_hash,
            arguments_hash=proposal_obj.arguments_hash,
        )

        result = self._run_ensemble(
            proposal_obj=proposal_obj,
            tu=tu,
            binding=binding,
            class_policy=class_policy,
            timeout_ms=timeout_ms,
            judge_budget=judge_budget,
            escalation_cfg=escalation_cfg,
            flat_failure_policy=flat_failure_policy,
            backend_name=backend_name,
            revise_iteration=0,
            original_proposal=None,
        )
        # PR 3b: when the ensemble ALLOWs a mandate-citing proposal,
        # cache the proposal on the instance so the tool-execution loop
        # can create a reservation and run post-action verification.
        # Keyed by tool_call_id; cleared at the start of each call()
        # iteration via ``_mandate_allowed_proposals``.
        allow_result, _events, _qid = result
        if allow_result and (
            proposal_obj.authorization is not None
            and proposal_obj.authorization.granted_by.startswith("mandate:")
        ):
            self._mandate_allowed_proposals[tool_call_id] = proposal_obj
        return result

    def _run_ensemble(
        self,
        *,
        proposal_obj,
        tu: dict,
        binding,
        class_policy,
        timeout_ms: int,
        judge_budget,
        escalation_cfg,
        flat_failure_policy: dict,
        backend_name: str,
        revise_iteration: int = 0,
        original_proposal=None,
    ):
        """Run the judge ensemble against one ``ActionProposal``.

        Factored out of ``_dispatch_with_judge`` in PR 3c so the REVISE
        branch can recurse against an amended proposal with a fresh
        context (CLASS NON-DOWNGRADE BY EXPLOIT defense — the second
        judgment's effective_class_policy is computed from the AMENDED
        classification, not the original).

        ``revise_iteration`` is the spec/28:276 ``max_revise_iterations``
        bound: 0 for the first judgment, 1 for the second. The third
        judgment is impossible by construction — when iteration ≥ 1
        and a judge returns REVISE, the framework BLOCKs with reason
        ``revise_loop_exhausted_blocked``.

        ``original_proposal`` is non-None on the second judgment.
        Carried into audit events as ``original_proposal`` for
        forensic linkage; the audit consumer sees both proposals
        inline plus the amendment yields.
        """
        from .judge import proposal as _proposal_mod  # noqa: F401
        from .judge.backend import JudgmentOutcome, Judgment
        from .judge.types import (
            ActionClass,
            JudgmentContext,
            JudgePolicyContext,
            JudgeRuntimeConfig,
            PersonaDigest,
            ProposalBinding,
            ToolPolicyEntry,
        )
        from .exceptions import (
            JudgeError,
            JudgeAmendedProposalRejected,
            JudgePolicyInvalid,
        )
        from .judge.types import ClassPolicyValue as _CPV

        tool_name = proposal_obj.tool_name
        tool_call_id = proposal_obj.tool_call_id
        classification = proposal_obj.classification

        policy_ctx = JudgePolicyContext(
            agent_name=self.name,
            persona_digest=PersonaDigest(agent_name=self.name),
            tools_md_entry=ToolPolicyEntry(
                tool_name=tool_name,
                classification=classification,
                write_paths=list(self.config.write_paths or []),
            ),
            class_policy=class_policy,
        )
        runtime_cfg = JudgeRuntimeConfig(
            backend_name=backend_name,
            timeout_ms=timeout_ms,
            budget=judge_budget,
            escalation_config=escalation_cfg,
            failure_policy=flat_failure_policy,
        )
        context = JudgmentContext(policy=policy_ctx, runtime=runtime_cfg)

        # Class-policy short-circuits (PR 3a — Codex round-2 P2 fix).
        # Operators who set class_policy.<X>: bypass don't want the
        # ensemble to run at all (LLM judge would incur cost for a
        # class the operator declared safe). Operators who set
        # allow_with_audit want every judge's decision recorded but
        # never enforced. Both short-circuit BEFORE building the
        # ensemble loop.
        effective_class_policy = {
            ActionClass.READ_ONLY: class_policy.read_only,
            ActionClass.REVERSIBLE_WRITE: class_policy.reversible_write,
            ActionClass.EXTERNAL_SIDE_EFFECT: class_policy.external_side_effect,
            ActionClass.HIGH_RISK: class_policy.high_risk,
        }[classification]

        if effective_class_policy == _CPV.ESCALATE:
            # PR 3b: synthesize ESCALATE from operator class_policy.
            # No judge ensemble runs — the operator's class_policy is
            # itself the decision. Write PENDING + signal defer.
            from .judge.backend import Judgment
            from .judge import escalation as _esc

            escalate_judgment = Judgment(
                outcome=JudgmentOutcome.ESCALATE,
                reason=(
                    f"class_policy.{classification.value} = escalate — "
                    "operator-configured pre-action gate"
                ),
                judge_id="framework",
                policy_version=(
                    self.judges_config.judges_md_hash
                    if self.judges_config is not None
                    else "unimplemented"
                ),
            )
            _pending_path, queue_id = _esc.write_pending_escalation(
                proposal=proposal_obj,
                judgment_reason=escalate_judgment.reason,
                judge_id="framework",
                agent_root=self.agent_root,
                agent_name=self.name,
                parent_run_id=self.run_id,
                policy_version=escalate_judgment.policy_version,
                judges_config_escalation=escalation_cfg,
                synthesis_source="class_policy",
            )
            escalate_judgment = Judgment(
                outcome=JudgmentOutcome.ESCALATE,
                reason=escalate_judgment.reason,
                judge_id="framework",
                policy_version=escalate_judgment.policy_version,
                escalation_queue_id=queue_id,
            )
            event = self._build_judgment_event_dict(
                proposal=proposal_obj,
                tool_use=tu,
                judgment=escalate_judgment,
                enforcement_action="escalate_pending",
                binding=binding,
                synthesis_source="class_policy",
            )
            return False, [event], queue_id

        if effective_class_policy == _CPV.BYPASS:
            # Synthesize a single bypass-recording event. No judge
            # ensemble runs. Tool executes immediately.
            from .judge.backend import Judgment

            bypass_judgment = Judgment(
                outcome=JudgmentOutcome.ALLOW,
                reason=(
                    f"class_policy.{classification.value} = bypass — "
                    "judge ensemble not invoked"
                ),
                judge_id="framework",
                policy_version=(
                    self.judges_config.judges_md_hash
                    if self.judges_config is not None
                    else "unimplemented"
                ),
            )
            bypass_event = self._build_judgment_event_dict(
                proposal=proposal_obj,
                tool_use=tu,
                judgment=bypass_judgment,
                enforcement_action="allow_executed",
                binding=binding,
            )
            return True, [bypass_event], None

        # Build the judge ensemble. PR 2b: PolicyJudge first
        # (microsecond latency, free), then LLMJudgeBackend if
        # available. First BLOCK short-circuits remaining judges per
        # spec/28:694. ``allow_with_audit`` mode runs the ensemble
        # but does not let BLOCKs gate execution (audit-only).
        audit_mode = effective_class_policy == _CPV.ALLOW_WITH_AUDIT
        judges = [self._ensure_policy_judge()]
        # MandateCheck inserted between PolicyJudge and LLMJudge per
        # spec/29 line 392 — `[PolicyJudge, MandateCheck, LLMCatchAll]`.
        # Conditionally included: only when the proposal cites a mandate
        # (Authorization.granted_by starts with "mandate:"). This keeps
        # the ensemble shape unchanged for non-mandate-citing actions —
        # zero behavior change for existing operator deployments per the
        # #124 PR 2 zero-behavior-change discipline. Mandate-citing
        # proposals get the spec/29 composition.
        cites_mandate = (
            proposal_obj.authorization is not None
            and proposal_obj.authorization.granted_by.startswith("mandate:")
        )
        if cites_mandate:
            judges.append(self._ensure_mandate_check())
        llm_judge = self._ensure_llm_judge()
        if llm_judge is not None:
            judges.append(llm_judge)

        # Helper for failure_policy lookup. When judges.md was parsed,
        # consult its per-class-per-exception map; otherwise fall back
        # to the spec/28 default ("block" for every exception). Codex
        # round-2 P2: unconditional BLOCK on JudgeError ignored
        # operator failure_policy configuration entirely.
        def _outcome_for_failure(exception_name: str) -> JudgmentOutcome:
            if self.judges_config is not None:
                raw = self.judges_config.failure_policy_for(
                    classification, exception_name
                )
            else:
                raw = "block"
            try:
                return JudgmentOutcome(raw)
            except ValueError:
                return JudgmentOutcome.BLOCK

        events: list[dict] = []
        final_allow = True
        escalation_queue_id: str | None = None
        for judge in judges:
            start = time.time()
            failure_synthesis: str | None = None
            triggered_by_failure: str | None = None
            try:
                judgment = judge.evaluate(proposal_obj, context)
            except JudgeError as exc:
                # Map exception via per-class failure_policy. Operator
                # may configure "allow" / "block" / "escalate" per-class
                # per-exception. PR 3b: "escalate" outcome now produces
                # a real PENDING file + deferred Response.
                from .judge.backend import Judgment

                outcome = _outcome_for_failure(type(exc).__name__)
                if outcome == JudgmentOutcome.ESCALATE:
                    failure_synthesis = "failure_policy"
                    triggered_by_failure = f"failure_policy:{type(exc).__name__}"
                judgment = Judgment(
                    outcome=outcome,
                    reason=f"{type(exc).__name__}: {exc}",
                    judge_id=judge.judge_id,
                    policy_version=judge.policy_version,
                    latency_ms=int((time.time() - start) * 1000),
                )

            # REVISE handling (PR 3c). The judge proposes an amendment;
            # the framework builds an amended proposal, re-validates,
            # and re-runs the ensemble (max_revise_iterations=1 per
            # spec/28:276). Audit_mode skips REVISE — operators in
            # audit-only mode see the judge's intent but no execution
            # path takes effect.
            if judgment.outcome == JudgmentOutcome.REVISE and not audit_mode:
                if revise_iteration >= 1:
                    # Spec/28:276 — second judgment must return ALLOW;
                    # REVISE again is the loop-exhausted case.
                    block_judgment = Judgment(
                        outcome=JudgmentOutcome.BLOCK,
                        reason="revise_loop_exhausted: second judgment "
                        "returned REVISE; max_revise_iterations=1",
                        judge_id=judgment.judge_id,
                        policy_version=judgment.policy_version,
                        latency_ms=judgment.latency_ms,
                    )
                    event = self._build_judgment_event_dict(
                        proposal=proposal_obj,
                        tool_use=tu,
                        judgment=block_judgment,
                        enforcement_action="revise_loop_exhausted_blocked",
                        binding=binding,
                    )
                    event["original_proposal_id"] = (
                        original_proposal.proposal_id
                        if original_proposal is not None
                        else None
                    )
                    event["revise_iteration"] = revise_iteration
                    events.append(event)
                    final_allow = False
                    break
                # First REVISE — build amended proposal + recurse.
                from .judge import _revise as _rv

                amendment = judgment.amendment
                if amendment is None:
                    # Judge advertised REVISE outcome but returned no
                    # amendment payload. Treat as invalid_amendment.
                    block_judgment = Judgment(
                        outcome=JudgmentOutcome.BLOCK,
                        reason="revise_invalid_amendment: judge returned "
                        "REVISE but Judgment.amendment is None",
                        judge_id=judgment.judge_id,
                        policy_version=judgment.policy_version,
                        latency_ms=judgment.latency_ms,
                    )
                    event = self._build_judgment_event_dict(
                        proposal=proposal_obj,
                        tool_use=tu,
                        judgment=block_judgment,
                        enforcement_action="revise_invalid_amendment",
                        binding=binding,
                    )
                    events.append(event)
                    final_allow = False
                    break
                try:
                    amended = _rv.amend_proposal(
                        original=proposal_obj,
                        amendment=amendment,
                        tool_registry=self.tool_registry,
                        tool_classifications=self._tool_classifications,
                    )
                    _rv.validate_amended_args(
                        amended,
                        self.tool_registry,
                        agent_name=self.name,
                        validation_mode=self._resolved_validation_mode(),
                    )
                    _rv.enforce_amended_write_paths(
                        amended,
                        write_paths=list(self.config.write_paths or []),
                        read_only_paths=list(self.config.read_only_paths or []),
                    )
                except JudgePolicyInvalid as exc:
                    # PR 5b /ship Step 9.1 (testing+security
                    # cross-confirmed): under ``validation: strict`` a
                    # malformed registered ``input_schema`` raises
                    # JudgePolicyInvalid — an operator authoring bug
                    # distinct from a per-amendment rejection. Route
                    # through ``failure_policy[JudgePolicyInvalid]`` so
                    # operators can configure the outcome (block /
                    # escalate / allow) as ``docs/deployment/
                    # judges-md.md`` already documents. Pre-fix the
                    # exception bubbled out of ``_run_ensemble`` and
                    # was caught by the generic ``except Exception`` at
                    # the dispatch site, producing a generic ``judge
                    # dispatch error`` audit message and a hard BLOCK
                    # regardless of operator failure_policy.
                    outcome = _outcome_for_failure("JudgePolicyInvalid")
                    reason_text = (
                        f"revise_invalid_amendment: {type(exc).__name__}: {exc}"
                    )
                    if outcome == JudgmentOutcome.ESCALATE:
                        # Mirror the judge.evaluate failure ESCALATE
                        # path at line ~1518: write the PENDING file +
                        # emit the escalate_pending audit so operators
                        # see the malformed-schema event in the queue.
                        from .judge import escalation as _esc

                        _pending_path, queue_id = _esc.write_pending_escalation(
                            proposal=proposal_obj,
                            judgment_reason=reason_text,
                            judge_id=judgment.judge_id,
                            agent_root=self.agent_root,
                            agent_name=self.name,
                            parent_run_id=self.run_id,
                            policy_version=judgment.policy_version,
                            judges_config_escalation=escalation_cfg,
                            synthesis_source="failure_policy",
                            triggered_by="failure_policy:JudgePolicyInvalid",
                            revised_from_proposal_id=(
                                original_proposal.proposal_id
                                if original_proposal is not None
                                else None
                            ),
                        )
                        synth_judgment = Judgment(
                            outcome=JudgmentOutcome.ESCALATE,
                            reason=reason_text,
                            judge_id=judgment.judge_id,
                            policy_version=judgment.policy_version,
                            latency_ms=judgment.latency_ms,
                            escalation_queue_id=queue_id,
                        )
                        event = self._build_judgment_event_dict(
                            proposal=proposal_obj,
                            tool_use=tu,
                            judgment=synth_judgment,
                            enforcement_action="revise_invalid_amendment",
                            binding=binding,
                            synthesis_source="failure_policy",
                            triggered_by="failure_policy:JudgePolicyInvalid",
                        )
                        event["original_proposal_id"] = (
                            original_proposal.proposal_id
                            if original_proposal is not None
                            else None
                        )
                        event["revise_iteration"] = revise_iteration
                        events.append(event)
                        final_allow = False
                        escalation_queue_id = queue_id
                        break
                    synth_judgment = Judgment(
                        outcome=outcome,
                        reason=reason_text,
                        judge_id=judgment.judge_id,
                        policy_version=judgment.policy_version,
                        latency_ms=judgment.latency_ms,
                    )
                    event = self._build_judgment_event_dict(
                        proposal=proposal_obj,
                        tool_use=tu,
                        judgment=synth_judgment,
                        enforcement_action="revise_invalid_amendment",
                        binding=binding,
                        # /ship Step 11 adversarial P2: BLOCK/ALLOW
                        # branches need the same synthesis_source +
                        # triggered_by labels the ESCALATE branch
                        # above already set, so audit consumers
                        # filtering by these structured fields don't
                        # miss failure_policy-synthesized
                        # block/allow events.
                        synthesis_source="failure_policy",
                        triggered_by="failure_policy:JudgePolicyInvalid",
                    )
                    event["original_proposal_id"] = (
                        original_proposal.proposal_id
                        if original_proposal is not None
                        else None
                    )
                    event["revise_iteration"] = revise_iteration
                    events.append(event)
                    if outcome == JudgmentOutcome.ALLOW:
                        # /ship Step 11 adversarial P1: honor
                        # failure_policy[JudgePolicyInvalid]=allow as
                        # "tolerate the failure" — execute the
                        # ORIGINAL pre-amendment action, mirroring
                        # the JudgeUnavailable:allow / JudgeBudget
                        # Exhausted:allow shape elsewhere. The judge
                        # wanted REVISE because it thought the
                        # original was risky; the operator explicitly
                        # opted into "tolerate the failure" via the
                        # ``allow`` policy. The amendment can't run
                        # (it failed validation), so the original is
                        # the only meaningful "let it through"
                        # interpretation. Pre-fix this silently set
                        # final_allow=False — audit said ALLOW while
                        # the action was actually blocked.
                        final_allow = True
                    else:
                        final_allow = False
                    break
                except JudgeAmendedProposalRejected as exc:
                    block_judgment = Judgment(
                        outcome=JudgmentOutcome.BLOCK,
                        reason=f"revise_invalid_amendment: {exc}",
                        judge_id=judgment.judge_id,
                        policy_version=judgment.policy_version,
                        latency_ms=judgment.latency_ms,
                    )
                    event = self._build_judgment_event_dict(
                        proposal=proposal_obj,
                        tool_use=tu,
                        judgment=block_judgment,
                        enforcement_action="revise_invalid_amendment",
                        binding=binding,
                    )
                    events.append(event)
                    final_allow = False
                    break
                # Audit the first-judgment REVISE outcome before
                # recursing. Enforcement = revise_pending_second_judgment
                # so the audit shape distinguishes "judge wanted to
                # revise" from "framework executed the revision".
                first_event = self._build_judgment_event_dict(
                    proposal=proposal_obj,
                    tool_use=tu,
                    judgment=judgment,
                    enforcement_action="revise_pending_second_judgment",
                    binding=binding,
                )
                first_event["revise_iteration"] = revise_iteration
                first_event["amendment"] = {
                    "judge_note": amendment.judge_note,
                    "tool_name": amendment.tool_name,
                    "tool_arguments": amendment.tool_arguments,
                }
                events.append(first_event)
                # Recurse against the amended proposal. Fresh binding
                # reflects amended hashes; class-policy + ensemble
                # re-evaluate from amended classification.
                amended_binding = ProposalBinding(
                    tool_call_id=amended.tool_call_id,
                    tool_definition_hash=amended.tool_definition_hash,
                    arguments_hash=amended.arguments_hash,
                )
                allow2, events2, queue2 = self._run_ensemble(
                    proposal_obj=amended,
                    tu=tu,
                    binding=amended_binding,
                    class_policy=class_policy,
                    timeout_ms=timeout_ms,
                    judge_budget=judge_budget,
                    escalation_cfg=escalation_cfg,
                    flat_failure_policy=flat_failure_policy,
                    backend_name=backend_name,
                    revise_iteration=1,
                    original_proposal=proposal_obj,
                )
                # Tag every second-judgment event with the linkage.
                for ev in events2:
                    ev["revise_iteration"] = 1
                    ev["original_proposal_id"] = proposal_obj.proposal_id
                # Promote the last event to revise_executed when the
                # second judgment ALLOWed (the action that actually
                # ran is the amended one).
                if allow2 and events2:
                    events2[-1]["enforcement_action"] = "revise_executed"
                events.extend(events2)
                return allow2, events, queue2

            # ESCALATE handling: a real judge (or failure_policy
            # synthesis) returned ESCALATE. Write PENDING + emit one
            # audit event with enforcement_action=escalate_pending.
            # Ensemble short-circuits — downstream judges do not run.
            if judgment.outcome == JudgmentOutcome.ESCALATE and not audit_mode:
                from .judge import escalation as _esc

                _pending_path, queue_id = _esc.write_pending_escalation(
                    proposal=proposal_obj,
                    judgment_reason=judgment.reason,
                    judge_id=judgment.judge_id,
                    agent_root=self.agent_root,
                    agent_name=self.name,
                    parent_run_id=self.run_id,
                    policy_version=judgment.policy_version,
                    judges_config_escalation=escalation_cfg,
                    synthesis_source=failure_synthesis,
                    triggered_by=triggered_by_failure,
                    # P1 #3 (PR 3c): if this ESCALATE fires during a
                    # second judgment (revise_iteration >= 1), thread
                    # the original proposal_id into the new PENDING so
                    # forensic chains stay walkable from amended
                    # back to actor-original.
                    revised_from_proposal_id=(
                        original_proposal.proposal_id
                        if original_proposal is not None
                        else None
                    ),
                )
                # Update judgment with the queue_id we just minted.
                judgment = Judgment(
                    outcome=JudgmentOutcome.ESCALATE,
                    reason=judgment.reason,
                    judge_id=judgment.judge_id,
                    policy_version=judgment.policy_version,
                    latency_ms=judgment.latency_ms,
                    escalation_queue_id=queue_id,
                )
                event = self._build_judgment_event_dict(
                    proposal=proposal_obj,
                    tool_use=tu,
                    judgment=judgment,
                    enforcement_action="escalate_pending",
                    binding=binding,
                    synthesis_source=failure_synthesis,
                    triggered_by=triggered_by_failure,
                )
                events.append(event)
                final_allow = False
                escalation_queue_id = queue_id
                break

            judge_allow = judgment.outcome == JudgmentOutcome.ALLOW
            # audit_mode (class_policy=allow_with_audit): every event
            # is recorded with ``audit_bypass`` enforcement and the
            # action ALWAYS proceeds, regardless of judge outcome.
            # Otherwise the standard ensemble logic applies.
            if audit_mode:
                enforcement = "audit_bypass"
            else:
                enforcement = (
                    "allow_pending_next_judge" if judge_allow else "block_executed"
                )
            event = self._build_judgment_event_dict(
                proposal=proposal_obj,
                tool_use=tu,
                judgment=judgment,
                enforcement_action=enforcement,
                binding=binding,
            )
            events.append(event)

            if not judge_allow and not audit_mode:
                # First BLOCK wins; skip remaining judges (no
                # cost incurred for downstream judges). All prior
                # events keep their ``allow_pending_next_judge``
                # enforcement — they were ALLOWed by their judge but
                # the ensemble blocked.
                final_allow = False
                break

        # audit_mode: ensemble result is always ALLOW regardless of
        # individual judges' outcomes — the BLOCKs were recorded for
        # forensics but do not gate the action.
        if audit_mode:
            return True, events, None

        # Ensemble-final fixup: when the whole ensemble allowed, the
        # LAST event represents the action that actually runs — promote
        # its enforcement from ``allow_pending_next_judge`` to
        # ``allow_executed``. Intermediate ALLOWs stay
        # ``allow_pending_next_judge`` (they were judged but did not
        # gate the action). Both values are canonical in spec/28's
        # enforcement_action enum (PR 4 lock).
        if final_allow and events:
            events[-1]["enforcement_action"] = "allow_executed"

        return final_allow, events, escalation_queue_id

    def _build_judgment_event_dict(
        self,
        *,
        proposal,
        tool_use: dict,
        judgment,
        enforcement_action: str,
        binding,
        synthesis_source: str | None = None,
        triggered_by: str | None = None,
    ) -> dict:
        """Construct the JSONL-shaped JudgmentEvent record for the
        audit trail. Mirrors spec/28's audit shape (line 833).

        ``synthesis_source`` distinguishes framework-synthesized ESCALATE
        from real-judge ESCALATE: ``"class_policy"`` for operator-set
        class_policy=escalate, ``"failure_policy"`` for exception-mapped
        escalate, ``None`` for actual ensemble verdicts. ``triggered_by``
        carries the exception class name for failure_policy synthesis.
        Both fields are canonical in spec/28's audit shape (PR 4 lock).
        Stored inline via ``self._log({...})``.
        """
        proposal_dict = None
        if proposal is not None:
            proposal_dict = asdict(proposal)
        record = {
            "trigger": "judgment",
            "event": "judgment",
            "parent_run_id": self.run_id,
            "proposal_id": getattr(proposal, "proposal_id", None),
            "agent": self.name,
            "judge_id": judgment.judge_id,
            "policy_version": judgment.policy_version,
            "proposal": proposal_dict,
            "judgment_outcome": judgment.outcome.value,
            "judgment_reason": judgment.reason,
            "raw_outcome": judgment.outcome.value,
            "enforcement_action": enforcement_action,
            "binding": asdict(binding),
            "latency_ms": judgment.latency_ms,
            "cost_usd": judgment.cost_usd,
            "cost_source": "judge",
            "tool_name": tool_use.get("name", ""),
        }
        if synthesis_source is not None:
            record["synthesis_source"] = synthesis_source
        if triggered_by is not None:
            record["triggered_by"] = triggered_by
        # Carry the escalation_queue_id through when populated so
        # operators auditing the trail see the PENDING file linkage.
        queue_id = getattr(judgment, "escalation_queue_id", None)
        if queue_id is not None:
            record["escalation_queue_id"] = queue_id
        return record

    # ────────────────────────────────────────────────────────────
    # Config loading

    def _load_config(self) -> AgentConfig:
        """Assemble AgentConfig from the pre-loaded profile snapshot.

        #63 PR 2: refactored from direct file reads to read from
        ``self._profile``. The profile was loaded once in __init__ via
        ``self.profile_backend.load_profile(self.name)``, which handles
        cascade resolution internally (the cascade branch that used to
        live here was deleted — Decision 7). All six structured config
        fields (model_config, tool_config, tool_classifications,
        judges_config, mcp_servers, roster) are read off the snapshot;
        no filesystem reads happen inside this method anymore.
        """
        model_data = self._profile.model_config
        tools_data = self._profile.tool_config
        # Judge-layer per-tool classifications from tools.md (#112 PR 2a).
        # Lookup at proposal-assembly time via _resolve_classification.
        self._tool_classifications = self._profile.tool_classifications

        # judges.md operator config (#112 PR 3a). Cascade-aware merge
        # (instance + project-floor with non-relaxable floor per
        # spec/28:408) already happened inside
        # FilesystemAgentProfileBackend.load_profile, which called
        # load_judges_config internally.
        self.judges_config = self._profile.judges_config

        return AgentConfig(
            default_model=model_data["default_model"],
            fallback_model=model_data["fallback_model"],
            provider=model_data.get("provider"),
            max_input_tokens=model_data["max_input_tokens"],
            max_output_tokens=model_data["max_output_tokens"],
            cost_guardrails_enabled=model_data["cost_guardrails_enabled"],
            daily_cap_usd=model_data["daily_cap_usd"],
            monthly_cap_usd=model_data["monthly_cap_usd"],
            daily_cap_action=model_data["daily_cap_action"],
            monthly_cap_action=model_data["monthly_cap_action"],
            warning_thresholds=model_data["warning_thresholds"],
            alert_channel=model_data["alert_channel"],
            # spec/45 PR2: opt-in implicit body-hash dedup key derivation.
            # .get() with False default for backward compat with AgentProfile
            # snapshots that pre-date this field (no 'dedup_body_hash_enabled'
            # in their model_config dict).
            dedup_body_hash_enabled=model_data.get("dedup_body_hash_enabled", False),
            # spec/47 PR1 (PROVISIONAL): channel-3 conversation backend selection
            # parsed from model.md's '## Conversation Backend' section. .get() with
            # None default for backward compat with AgentProfile snapshots that
            # pre-date this field. Without threading it here _resolve_conversation_backend()
            # channel (3) can never fire (the field would always read None).
            conversation_backend_id=model_data.get("conversation_backend_id"),
            read_paths=tools_data["read_paths"],
            write_paths=tools_data["write_paths"],
            read_only_paths=tools_data.get("read_only_paths", []),
            external_apis=tools_data["external_apis"],
            hard_nos=tools_data["hard_nos"],
            roster=list(self._profile.roster),
            mcp_servers=list(self._profile.mcp_servers),
        )

    # ────────────────────────────────────────────────────────────
    # File loaders (per spec/04 canonical order)

    def load(self) -> None:
        """Load all the agent's files for this run. Idempotent."""
        if self.cascade:
            self._load_role_prompt()
            self._load_project_layer_text()
        else:
            self._load_goal_text()
        self._load_persona()
        self._load_tools_text()
        self._load_indexes()
        self._load_pinned_notes()
        self._load_recent_notes(n=RECENT_NOTES_DEFAULT)
        self._load_recent_journal(n=RECENT_JOURNAL_DEFAULT)

    def _load_role_prompt(self) -> None:
        if self.cascade:
            self._role_prompt_text = _cascade.load_role_prompt(self.cascade)

    def _load_project_layer_text(self) -> None:
        if self.cascade:
            layer = _cascade.load_project_layer(self.cascade)
            self._project_canon_text = layer["canon"]
            self._project_style_guide_text = layer["style_guide"]
            self._project_goal_text = layer["goal"]
            self._project_policy_text = layer["policy"]

    def _load_goal_text(self) -> None:
        """Load single-agent goal.md if present (spec/04 step 3.5).

        Only called for non-cascaded agents. Cascaded agents pick up the
        project-level goal via _load_project_layer_text(); loading the
        instance goal.md on top would create duplicate sections.

        #63 PR 2: reads from the pre-loaded profile snapshot's
        ``goal_text`` field (populated by FilesystemAgentProfileBackend
        from goal.md at init time) instead of re-reading the file.
        """
        self._goal_text = self._profile.goal_text

    def _load_persona(self) -> None:
        """Assemble the persona section from the profile snapshot.

        #63 PR 2: reads from the pre-loaded profile snapshot's
        ``persona_identity / persona_soul / persona_user`` fields
        (populated by FilesystemAgentProfileBackend from the three
        persona/ markdown files at init time) instead of re-reading
        the files. Preserves the legacy assembled-text shape exactly:
        each non-empty body gets its filename as an H1 header, joined
        by blank lines.
        """
        parts = []
        for filename, body in (
            ("IDENTITY.md", self._profile.persona_identity),
            ("SOUL.md", self._profile.persona_soul),
            ("USER.md", self._profile.persona_user),
        ):
            if body:
                parts.append(f"# {filename}\n\n" + body.strip())
        self._persona_text = "\n\n".join(parts)

    def _load_tools_text(self) -> None:
        if self.cascade:
            _, self._tools_text = _cascade.resolve_tools_md(self.cascade)
            return
        path = self.agent_root / "tools.md"
        if path.exists():
            self._tools_text = path.read_text(encoding="utf-8")
        else:
            self._tools_text = ""

    def _load_indexes(self) -> None:
        summary = self.memory.render_index_summary()
        if summary and summary.strip() != "# Memory Index\n":
            self._memory_index_text = summary
        if self.corpus_backend is not None:
            # Route through Protocol. After PR 3 default-resolution at
            # __init__, this is the common production path. Broad except
            # mirrors the legacy direct-read soft-degrade so a transient
            # backend failure (OSError, UnicodeDecodeError, sqlite3.*,
            # CorpusError, or any custom-backend exception) does not crash
            # agent construction. The empty wiki section is observable via
            # the logged warning marker wiki_index_unreadable.
            try:
                self._wiki_index_text = self.corpus_backend.render_index_summary(
                    corpus="wiki"
                )
            except Exception as exc:
                _logger.warning(
                    "wiki_index_unreadable backend=%s agent_root=%s cause=%s",
                    type(self.corpus_backend).__name__,
                    self.agent_root,
                    exc,
                )
                self._wiki_index_text = ""
        else:
            # Legacy direct-read fallback. NOTE: after PR 3, this branch is
            # unreachable in production because AtomicAgent.__init__ always
            # default-resolves corpus_backend via get_default_corpus_backend.
            # Retained as a safety net for any future refactor that removes
            # the auto-resolve. Tests in test_corpus_migration_regression.py
            # force corpus_backend=None post-construction to exercise this
            # branch's byte-identity and OSError soft-degrade guarantees.
            # Round 3 finding R3-F1: this branch does NOT catch
            # UnicodeDecodeError. The Protocol path handles it inside
            # FilesystemCorpusBackend.render_index_summary (see
            # corpus/filesystem.py:699-715). If this branch is ever
            # re-activated for production, add a UnicodeDecodeError catch
            # matching the Protocol path's partial-content soft-degrade.
            wiki_index = self.agent_root / "wiki" / "INDEX.md"
            if wiki_index.exists():
                try:
                    self._wiki_index_text = wiki_index.read_text(encoding="utf-8")
                except OSError as exc:
                    _logger.warning(
                        "wiki_index_unreadable agent_root=%s path=%s cause=%s",
                        self.agent_root,
                        wiki_index,
                        exc,
                    )
                    self._wiki_index_text = ""

    def _load_pinned_notes(self) -> None:
        # No on-disk precheck: the memory backend is authoritative for whether
        # any notes exist. A filesystem-shaped guard (agent_root/"memory"
        # exists) silently disables recall on a non-filesystem backend
        # (Postgres on a zero-local-disk Cloud Run fleet — the #258 target
        # deployment). FilesystemBackend.list_pinned() already returns [] when
        # its memory/ dir is absent, so dropping the guard is safe for both
        # shapes and lets render_index_summary (called unconditionally) and
        # recall agree.
        pinned_refs = self.memory.list_pinned()
        pinned = []
        for ref in pinned_refs[:PINNED_MAX]:
            note = self.memory.read_note(ref.name)
            if note is None:
                continue
            pinned.append(self._render_note_from_model(ref.name, note))
        self._pinned_notes = pinned

    def _load_recent_notes(self, n: int = RECENT_NOTES_DEFAULT) -> None:
        # No on-disk precheck — see _load_pinned_notes. The backend is
        # authoritative; FilesystemBackend.list_recent() returns [] when the
        # memory/ dir is absent, and a non-filesystem backend (Postgres) has no
        # local memory/ dir at all.
        recent_refs = self.memory.list_recent(n=n, exclude_pinned=True)
        self._recent_notes = []
        for ref in recent_refs:
            note = self.memory.read_note(ref.name)
            if note is None:
                continue
            self._recent_notes.append(self._render_note_from_model(ref.name, note))

    def _load_recent_journal(self, n: int = RECENT_JOURNAL_DEFAULT) -> None:
        # ADOPT-NOW (#427 PR1 — spec/43): routes through self.journal_backend.
        # backend returns raw JournalEntry; formatting stays at this call site.
        # agent renders: '# Journal — {stem}\n\n{text}' (NO path line).
        # bundle renders: '# Journal — {stem}\n`{path}`\n\n{text}' (WITH path line).
        # DO NOT unify — the divergence is LOAD-BEARING (byte-identity golden tests
        # freeze both formats separately).
        # Behavior change from legacy: corrupt entries are now KEPT with a degraded
        # body (the backend's list_entries reads every slot via _safe_read_entry,
        # mirroring bundle._safe_read_text) rather than raising UnicodeDecodeError.
        # This makes agent match bundle's long-standing degrade-but-keep behavior
        # and keeps the selected entry SET identical to the legacy newest-N slice.
        journal_entries = self.journal_backend.list_entries(limit=n, newest_first=True)
        self._recent_journal = [
            f"# Journal — {entry.path.stem}\n\n{entry.text}"
            for entry in journal_entries
        ]

    @staticmethod
    def _render_note_for_context(path: Path, parsed: frontmatter.Post) -> str:
        """Format an atomic note for inclusion in the system prompt."""
        meta_summary = (
            f"name: {parsed.metadata.get('name', 'unnamed')}\n"
            f"type: {parsed.metadata.get('type', '?')}\n"
            f"confidence: {parsed.metadata.get('confidence', '?')}\n"
            f"last_seen: {parsed.metadata.get('last_seen', '?')}"
        )
        return f"# {path.name}\n\n{meta_summary}\n\n{parsed.content}"

    @staticmethod
    def _render_note_from_model(filename: str, note: "Any") -> str:
        """Format a Note model for inclusion in the system prompt.

        Mirrors _render_note_for_context but reads from a Note dataclass instead
        of a raw frontmatter.Post. Called by _load_pinned_notes and
        _load_recent_notes after the P2.1 migration to agent.memory.read_note().
        """
        meta_summary = (
            f"name: {note.name}\n"
            f"type: {note.type}\n"
            f"confidence: {note.confidence}\n"
            f"last_seen: {note.last_seen}"
        )
        return f"# {filename}\n\n{meta_summary}\n\n{note.body}"

    # ────────────────────────────────────────────────────────────
    # System prompt assembly

    def assemble_system_prompt(self) -> str:
        """Assemble the full system prompt.

        Single-agent layout uses spec/04 order. Cascaded multi-agent project
        agents use spec/06 order:

            [1] role PROMPT.md
            [2-4] instance IDENTITY/SOUL/USER (loaded into _persona_text)
            [5/5b] role tools.md (or instance override) — already merged in _tools_text
            [7] project canon.md
            [7.5] project goal.md (optional, if present)
            [8] project style_guide.md
            [9] project policy/* (all)
            [10-13] memory/INDEX, wiki/INDEX, pinned, recent atomic notes
            [14] recent journal
        """
        sections: list[str] = []

        if self.cascade and self._role_prompt_text:
            sections.append("# role PROMPT.md\n\n" + self._role_prompt_text)
        if self._persona_text:
            sections.append(self._persona_text)
        # spec/04 step 3.5 — single-agent goal.md injected between persona and tools.
        # Cascaded agents already get project-level goal via _project_goal_text below.
        if not self.cascade and self._goal_text:
            sections.append("# goal.md\n\n" + self._goal_text)
        if self._tools_text:
            sections.append("# tools.md\n\n" + self._tools_text)
        # spec/18 — skills metadata injected after tools, before memory.
        # Only metadata (name + description) lands here; full body is loaded
        # on demand via the load_skill tool.
        if self.skills:
            skill_lines = [
                "# Available skills",
                "",
                "The following skills are available. Use the load_skill tool to load a "
                "skill's full instructions when relevant to the task.",
                "",
            ]
            for skill in self.skills:
                skill_lines.append(f"- **{skill.name}**: {skill.description}")
            sections.append("\n".join(skill_lines))
        if self.cascade:
            if self._project_canon_text:
                sections.append("# project canon.md\n\n" + self._project_canon_text)
            if self._project_goal_text:
                sections.append("# project goal.md\n\n" + self._project_goal_text)
            if self._project_style_guide_text:
                sections.append(
                    "# project style_guide.md\n\n" + self._project_style_guide_text
                )
            if self._project_policy_text:
                sections.append("# project policy/\n\n" + self._project_policy_text)
        if self._memory_index_text:
            sections.append("# memory/INDEX.md\n\n" + self._memory_index_text)
        if self._wiki_index_text:
            sections.append("# wiki/INDEX.md\n\n" + self._wiki_index_text)
        if self._pinned_notes:
            sections.append(
                "# Pinned atomic notes\n\n" + "\n\n---\n\n".join(self._pinned_notes)
            )
        if self._recent_notes:
            sections.append(
                "# Recent atomic notes\n\n" + "\n\n---\n\n".join(self._recent_notes)
            )
        if self._recent_journal:
            sections.append(
                "# Recent journal\n\n" + "\n\n---\n\n".join(self._recent_journal)
            )
        return "\n\n═══════════════════════════\n\n".join(sections)

    # ────────────────────────────────────────────────────────────
    # Conversation backend resolution (spec/47 PR1 — three-channel seam)

    def _resolve_conversation_backend(self) -> "ConversationBackend | None":
        """Resolve the effective conversation backend for this agent.

        Three-channel resolution (spec/47 §"Three-channel seam"):
          (1) constructor kwarg wins — if self.conversation_backend is not None,
              use it directly. (No lazy resolution needed.)
          (2) ATOMIC_AGENTS_CONVERSATION_BACKEND env var — instantiate the
              named backend_id via the registry.
          (3) model.md field — AgentConfig.conversation_backend_id (PROVISIONAL).
              (4) None when all three are absent — single-shot default (rule #14).

        The result is cached on self._conversation_backend_resolved to avoid
        repeated env-var reads and to share the instance across the multi-turn
        loop. The sentinel _CONV_BACKEND_UNRESOLVED distinguishes "not yet
        resolved" from None (no backend).

        Called from call() after lazy load() so self.config is available for
        channel (3). The model.md field is marked PROVISIONAL in DRAFT spec/47
        — its section name and syntax may change before LOCK.

        Returns:
            ConversationBackend instance or None (single-shot).
        """
        if self._conversation_backend_resolved is not _CONV_BACKEND_UNRESOLVED:
            # Already resolved — return cached result.
            return self._conversation_backend_resolved  # type: ignore[return-value]

        # Channel (1): constructor kwarg wins.
        if self.conversation_backend is not None:
            self._conversation_backend_resolved = self.conversation_backend
            return self.conversation_backend

        # Channel (2): env var.
        import os as _os  # noqa: PLC0415

        raw_env = (
            _os.environ.get("ATOMIC_AGENTS_CONVERSATION_BACKEND", "").strip().lower()
        )
        if raw_env:
            from .conversation import (  # noqa: PLC0415
                BackendNotRegistered as _BackendNotRegistered,
                get_default_conversation_backend,
            )

            # Fail soft: a misconfigured ATOMIC_AGENTS_CONVERSATION_BACKEND must
            # never hard-crash a call. Degrade to None (single-shot) + WARNING so
            # the operator sees the bad config without losing the run. (The call
            # site also gates resolution on conversation_id, so this only fires
            # for a conversation call with a bad env var — still a degrade, not a
            # crash.)
            try:
                resolved = get_default_conversation_backend(self.agent_root)
            except _BackendNotRegistered as _exc:
                _logger.warning(
                    "ATOMIC_AGENTS_CONVERSATION_BACKEND=%r is not a registered "
                    "conversation backend — falling back to single-shot (no "
                    "conversation persistence): %s",
                    raw_env,
                    _exc,
                )
                self._conversation_backend_resolved = None
                return None
            self._conversation_backend_resolved = resolved
            return resolved

        # Channel (3): model.md field (PROVISIONAL — shape may change before LOCK).
        # self.config is available here (called after load()).
        backend_id = getattr(self.config, "conversation_backend_id", None)
        if backend_id:
            # TODO(#535 LOCK PR): firm up exact section name and parser semantics.
            # The model.md field is PROVISIONAL per spec/47 DRAFT; do not depend
            # on this for stable deployments until spec/47 is LOCKED.
            from .conversation import (  # noqa: PLC0415
                BackendNotRegistered as _BackendNotRegistered,
                get_conversation_backend,
            )

            # Fail soft: a misconfigured model.md conversation_backend_id must not
            # hard-crash a call. Degrade to None (single-shot) + WARNING.
            try:
                cls = get_conversation_backend(backend_id)
            except _BackendNotRegistered as _exc:
                _logger.warning(
                    "model.md conversation_backend_id=%r is not a registered "
                    "conversation backend — falling back to single-shot (no "
                    "conversation persistence): %s",
                    backend_id,
                    _exc,
                )
                self._conversation_backend_resolved = None
                return None
            resolved_backend = cls(self.agent_root)
            self._conversation_backend_resolved = resolved_backend
            return resolved_backend

        # None — single-shot default (backward-compatible).
        self._conversation_backend_resolved = None
        return None

    # ────────────────────────────────────────────────────────────
    # The main call

    def call(
        self,
        work_item: str,
        model_override: str | None = None,
        critical: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
        write_captures: bool = True,
        parent_remaining_headroom_usd: float | None = None,
        caller_identity: str | None = None,
        *,
        idempotency_key: str | None = None,
        conversation_id: str | None = None,
        principal: Principal = LOCAL_PRINCIPAL,
    ) -> Response:
        """Make the LLM call. Returns a Response with captures populated.

        critical=True bypasses cost guardrails (still logged with critical: true).
        write_captures=False extracts but doesn't persist captures (dry-run mode).

        caller_identity: optional string identifying the HTTP caller (e.g. the value
            of the X-Goog-IAP-JWT-Assertion header extracted by the serve layer).
            When set, the value is written into the JSONL run record under
            ``http_caller`` (in ``extra``) so the audit trail answers "who called
            this agent at time T?" without requiring cross-reference with
            perimeter logs. The serve layer sets this; all other callers leave it
            None (zero behavioral change). See spec/37 §"Audit record shape".

        ``principal``: the caller's verified identity (spec/48 PR1). Defaults to
        LOCAL_PRINCIPAL (home-user zero-config, is_verified=True). Org/serve
        deployments supply a Principal derived from the registered PrincipalBackend
        (the serve layer calls agent.principal_backend.derive_principal(verified_claims)
        and passes the result here). HARD-REFUSE gate: when a non-local caller
        sets conversation_id but principal.is_verified is False, call() raises
        UnverifiedPrincipalConversationAccess BEFORE any LLM spend, before the
        cost gate, and before storage I/O. The gate condition is exactly:
            if conversation_id is not None and not principal.is_verified: raise
        Gate keys on is_verified ONLY — never on object identity with LOCAL_PRINCIPAL.
        The threaded principal replaces the previously hardcoded LOCAL_PRINCIPAL
        in load_turns() and write_turn() so conversation turns are isolated by
        principal.identifier.

        ``conversation_id``: optional conversation key that activates multi-turn
        continuity (spec/47 PR1). When set and self.conversation_backend is
        configured (via kwarg, ATOMIC_AGENTS_CONVERSATION_BACKEND env var, or
        model.md '## Conversation Backend' field), prior turns for this
        conversation_id are loaded and injected as real role-tagged entries in
        the MESSAGES array BEFORE the current work_item turn. After a successful
        LLM call, the new turn pair (user: work_item, assistant: response.text)
        is written back atomically.
        ``None`` (the default) = no conversation continuity — zero behavioral
        change for all existing callers (backward-compatible, rule #14).
        NOTE: raw-immediate-continuity transcript injection is a bounded flex of
        Rule #6 (progressive disclosure) — bounded by the token budget window,
        current-run-only, and ephemeral. See T16 in TENSIONS.md (approved —
        committed on docs/tensions-t16-conversation-flex, merges with this PR).
        Do NOT inject into assemble_system_prompt() — the T14 cache prefix must
        remain stable across turns.

        ``model_override``: per-call model selection that supersedes ``model.md``'s
        default. Policy's ``get_effective_model`` (when set) takes precedence over
        this per-call kwarg in enforce mode (``ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP``
        unset or true, the PR 4 default) per spec/32 Premise 1 ("most-restrictive
        wins"): fleet-config is operator intent at config time, the kwarg is
        operator intent at call time, and the fleet-config layer is the
        authoritative one. When Policy overrides a per-call kwarg (kwarg supplied
        and differs from Policy's value), the ``policy_decision`` audit event
        carries ``model_from_per_call_override`` so the caller can detect that
        the kwarg was superseded (issue #274). When the kwarg matches Policy's
        value the override emission is skipped — operator and Policy aligned and
        nothing was overridden from either layer's perspective.

        parent_remaining_headroom_usd: when set (by a coordinator's delegate()),
        this call's own cap is clamped to min(own remaining, parent headroom).
        This enforces the coordinator's cap as a true tree-cap (spec/15).

        ``idempotency_key``: optional caller-supplied key that activates the
        two-phase deduplication gate (spec/45 PR2, W1–W7). A prior COMPLETED run
        for the same key short-circuits — NO LLM call, NO lock acquire — and
        returns ``Response.deduped_response()`` (``deduped=True`` with
        ``replayed_run_id`` / ``result_ref`` of the original run; the cached
        bytes are resolved out-of-band via ``result_ref``/OutcomeBackend, never
        inlined, because the ledger is MARKER-ONLY). A concurrent IN_FLIGHT run
        for the same key raises ``DedupInFlight`` (carrying ``prior_run_id``).
        ``None`` (the default) = no dedup — zero behavioral change for callers
        that don't opt in. When ``dedup_body_hash_enabled`` is set in model.md
        and no explicit key is supplied, an implicit key is derived from
        ``sha256(work_item+model+max_tokens+temperature)`` so bit-identical
        re-deliveries dedup without caller key management. See spec/45 W1–W7.

        When self.tool_registry has registered tools, call() runs a multi-turn
        loop (up to self.max_tool_iterations iterations):
          1. LLM call with tool definitions
          2. Parse tool_uses from response
          3. Execute custom tools via registry (atomic_capture handled separately)
          4. Build follow-up message with tool_result blocks
          5. Repeat until no custom tool_uses returned OR cap hit

        Each iteration counts against the same cost cap. The final Response has:
          - tool_calls: list of all ToolCallResult from all iterations
          - tool_iterations: how many LLM turns were made (1 = no tools)
          - tool_iterations_maxed: True if loop was stopped by cap
        """
        # Lazy load if not already
        if not self._persona_text:
            self.load()

        # Reset run_id BEFORE acquiring the lock so that the lock_busy audit
        # record (if the lock is held by another call) carries a unique run_id
        # for this invocation, not the previous call's id. spec/37 MUST 8 —
        # "each call() invocation MUST produce a unique run_id". For long-lived
        # cached instances the run_id must be fresh even on the refused path.
        #
        # Exception: when the constructor received an explicit run_id (e.g.
        # OutcomeRunner pins run_id='outcome-...' so that agent_call JSONL
        # records correlate with outcome_iteration records that reference the
        # same id), we MUST NOT overwrite — the pin is the audit-trail anchor.
        # CLAUDE.md principle 5. The per-request construction path in the serve
        # layer (serve/_runner.py) constructs a fresh agent per request, so
        # MUST 8's HTTP conformance is satisfied by construction, not by this
        # reset, for that path.
        if not self._run_id_pinned:
            self.run_id = self._generate_run_id()

        # ── OTel instrumentation seam (spec/39 MUST 1 / MUST 3) ─────────────
        # Span opened AFTER run_id reset so atomic_agents.run_id carries the
        # correct fresh id for this invocation.
        #
        # Teardown discipline (spec/39 MUST 3: span MUST be ended on ALL exit
        # paths). We use start_span + context.attach rather than
        # start_as_current_span to avoid re-indenting the entire method body.
        # That manual pattern means WE own end()+detach(). Teardown has TWO
        # finalize sites, both calling the idempotent `_finalize_call_span()`:
        #   (1) the lock-acquire except clauses below, for the lock_busy and
        #       non-LockBusy acquire failures that raise BEFORE the body try is
        #       entered (so they can never reach the body finally), and
        #   (2) the body `try/finally`, for every path that reaches the body —
        #       normal return, exception, pre-loop cost-skip, mid-loop cost-cap.
        # The `_span_ended` flag makes `_finalize_call_span()` a no-op if a path
        # already finalized, so the early-exit paths that set their own outcome
        # attributes do NOT double-end.
        #
        # PR 1 scope note: only the parent atomic_agents.call span is emitted.
        # Child spans (llm, tool, helper, delegate) are deferred to a later PR of
        # this arc — see docs/spec/39-otel-export.md §Scope.
        #
        # The accumulators below are declared up-front (NameError guard) so the
        # finalizer can always read them regardless of where an exception fires —
        # including BEFORE the body try is entered (a lock-acquire failure) and
        # AFTER real LLM spend (a raise mid/post-loop, which carries the true
        # partial spend because the loop re-syncs these per iteration). spec/39
        # MUST 1.
        _call_total_cost: float = 0.0
        _call_input_tokens: int = 0
        _call_output_tokens: int = 0
        _call_tool_iterations: int = 0
        _call_model: str = self.config.default_model
        _call_response: "Response | None" = None
        _span_ended: bool = False

        # Open the agent.call span. spec/39 MUST 3: the OPEN path is as
        # non-throwing as the finalizer. Once a host process installs a real SDK
        # provider, start_span() runs every SpanProcessor.on_start synchronously
        # and the set_attribute calls run under SpanLimits — any can raise. This
        # block sits BEFORE the lock acquire and BEFORE the body try/finally, so
        # an unguarded fault here would crash call() before ANY agent_call audit
        # JSONL line is written (Principle 5 — audit trail is structural) and a
        # tracing-infra fault would abort the real invocation. On failure we fall
        # back to a non-recording sentinel span + a None context token so the rest
        # of call() proceeds and _finalize_call_span() no-ops safely.
        # Exception (not BaseException) so KeyboardInterrupt / SystemExit still
        # propagate, mirroring the finalizer.
        _call_ctx_token: object | None = None
        try:
            _call_span = _tracing.get_tracer().start_span(_tracing.SPAN_AGENT_CALL)
            _call_ctx_token = _tracing._otel_ctx.attach(
                _tracing._otel_trace.set_span_in_context(_call_span)
            )
            _call_span.set_attribute(_tracing.ATTR_AGENT_NAME, self.name)
            _call_span.set_attribute(_tracing.ATTR_TRIGGER, self.trigger)
            _call_span.set_attribute(_tracing.ATTR_RUN_ID, self.run_id)
            _call_span.set_attribute(_tracing.ATTR_MODEL, self.config.default_model)
        except Exception:
            _logger.warning(
                "atomic_agents tracing: failed to open call span "
                "(swallowed; spec/39 MUST 3)",
                exc_info=True,
            )
            # If start_span succeeded but a later step raised, _call_span is a real
            # span that was never ended; detach any token we managed to attach and
            # end it before falling back, so neither leaks (spec/39 MUST 3).
            if _call_ctx_token is not None:
                _tracing.safe_span_op(
                    lambda: _tracing._otel_ctx.detach(_call_ctx_token),
                    "detach call span context (open-fault cleanup)",
                )
                _call_ctx_token = None
            _local_span = locals().get("_call_span")
            if _local_span is not None:
                _tracing.safe_span_op(
                    _local_span.end, "end call span (open-fault cleanup)"
                )
            # Non-recording sentinel: get_current_span() with no active span
            # returns OTel's INVALID_SPAN (is_recording() == False), whose
            # set_attribute / set_status / record_exception / end are all no-ops.
            _call_span = _tracing._otel_trace.INVALID_SPAN
            _span_ended = True  # nothing left to end; finalizer is a no-op

        def _finalize_call_span(
            *,
            outcome: str | None = None,
            error: BaseException | None = None,
        ) -> None:
            """End the agent.call span + detach the context token exactly once.

            Single owner of span teardown (spec/39 MUST 3). Idempotent via the
            enclosing `_span_ended` flag so the early-exit paths (which set their
            own outcome attributes before returning) do not double-end when the
            outer finally also fires. Wrapped so a tracing failure can never mask
            the real return value or in-flight exception.
            """
            nonlocal _span_ended
            if _span_ended:
                return
            _span_ended = True
            try:
                if error is not None:
                    # In-flight exception → error outcome + ERROR status (spec/39
                    # MUST 8: error has top precedence).
                    _call_span.set_attribute(
                        _tracing.ATTR_OUTCOME, _tracing.OUTCOME_ERROR
                    )
                    _call_span.set_status(
                        _tracing.StatusCode.ERROR,
                        description=type(error).__name__,
                    )
                    _call_span.record_exception(error)
                else:
                    # No exception: prefer an explicit outcome (early-exit paths),
                    # otherwise derive from the returned Response (skipped >
                    # deferred > ok, spec/39 MUST 8). Note: an early-exit path may
                    # have already set ERROR status (e.g. cost-skip MUST 9); we do
                    # NOT clear it here — only set the outcome attribute.
                    resolved = outcome
                    if resolved is None and _call_response is not None:
                        resolved = _tracing._derive_outcome(_call_response)
                    if resolved is not None:
                        _call_span.set_attribute(_tracing.ATTR_OUTCOME, resolved)
                # Cost/token/iteration attributes from the accumulators. These
                # are always safe to read — declared above before any code that
                # could raise (spec/39 MUST 1: cost recorded on all paths).
                _call_span.set_attribute(_tracing.ATTR_COST_USD, _call_total_cost)
                _call_span.set_attribute(_tracing.ATTR_INPUT_TOKENS, _call_input_tokens)
                _call_span.set_attribute(
                    _tracing.ATTR_OUTPUT_TOKENS, _call_output_tokens
                )
                _call_span.set_attribute(
                    _tracing.ATTR_TOOL_ITERATIONS, _call_tool_iterations
                )
                _call_span.set_attribute(_tracing.ATTR_MODEL, _call_model)
            except Exception:
                # spec/39 MUST 3 — the finalizer MUST be genuinely non-throwing.
                # When a host process installs a real SDK TracerProvider, the
                # span becomes a recording span: set_attribute / set_status /
                # record_exception can then raise (custom SpanProcessor,
                # SpanLimits, or an in-flight exception whose __str__/__repr__
                # misbehaves). If that propagated it would (a) abort the body
                # finally before lock release → LEAKED AGENT LOCK (Principle 8),
                # and (b) replace a refusal exception (lock_busy) with a tracing
                # RuntimeError, breaking the documented refusal contract. Swallow
                # + log so a tracing-library fault can never mask the real return
                # value or in-flight exception. BaseException is intentionally
                # NOT caught here so KeyboardInterrupt / SystemExit still
                # propagate; the inner finally below still ends + detaches the
                # span on those paths.
                _logger.warning(
                    "atomic_agents tracing: failed to finalize call span",
                    exc_info=True,
                )
            finally:
                # detach + end must run even if attribute-setting raised, so the
                # span is never leaked and the context token is never orphaned
                # (spec/39 MUST 3). BOTH calls are individually non-throwing: on a
                # real host-provider recording span, span.end() invokes the host
                # SpanProcessor's on_end synchronously (a misbehaving / full-queue
                # BatchSpanProcessor can raise) and context.detach() can raise on a
                # foreign/stale token in some OTel versions. If either propagated
                # it would abort the body finally BEFORE the lock release at the
                # end of call() — leaking the agent lock so every subsequent call
                # returns LockBusy (Principle 8). Guard each independently so a
                # fault in one still runs the other. (_call_ctx_token is None only
                # on the open-fault sentinel path, where _span_ended short-circuits
                # us out before reaching here — but guard anyway for safety.)
                if _call_ctx_token is not None:
                    _tracing.safe_span_op(
                        lambda: _tracing._otel_ctx.detach(_call_ctx_token),
                        "detach call span context",
                    )
                _tracing.safe_span_op(_call_span.end, "end call span")

        # spec/45 PR2 — Body-hash key derivation (opt-in, default OFF).
        # When dedup_body_hash_enabled=True (model.md '## Dedup Body Hash' section)
        # AND the caller did NOT supply an explicit idempotency_key, derive one
        # automatically as sha256(work_item + model + max_tokens + temperature).
        # This prevents duplicate LLM spend for bit-identical re-deliveries of the
        # same request without requiring the caller to manage keys explicitly.
        # Explicit caller-supplied keys always take precedence (zero-override).
        #
        # spec/45 PR2: AUTO derivation is gated to EXTERNAL DELIVERY triggers
        # (http/queue/cron) where redelivery actually occurs. Framework-internal
        # repeat-invocation callers (eval/delegate/outcome) and plain manual/api/
        # skill calls expect identical inputs to RUN again — auto-deduping them
        # would hand a text='' deduped Response to a consumer that treats it as a
        # real result. See _BODY_HASH_AUTO_DERIVE_TRIGGERS. An explicit
        # idempotency_key (handled above) is honored on ANY trigger; only this
        # implicit derivation is trigger-gated.
        if (
            idempotency_key is None
            and self.config.dedup_body_hash_enabled
            and self.trigger in _BODY_HASH_AUTO_DERIVE_TRIGGERS
            # spec/47 PR1: the implicit body hash covers ONLY work_item + model +
            # max_tokens + temperature. When conversation_id is set, the prior
            # turns loaded below (after this gate) are part of the effective LLM
            # input but are NOT in the hash — so two different conversations with
            # the same work_item text, or the same conversation after its history
            # grows, would hash-collide and replay a stale/cross-conversation
            # result. Auto body-hash dedup is therefore unsafe for conversation
            # calls; skip it. An explicit caller-supplied idempotency_key still
            # works on any trigger (the caller owns its correctness).
            and conversation_id is None
        ):
            import hashlib as _hashlib

            _resolved_model = (
                model_override
                if model_override is not None
                else self.config.default_model
            )
            _resolved_max_tokens = (
                max_tokens if max_tokens is not None else self.config.max_output_tokens
            )
            _resolved_temp = (
                temperature if temperature is not None else self.config.temperature
            )
            # This repr-based serialization is stable within a Python version;
            # not cross-language safe (repr() of floats/strings can vary across
            # languages/versions) but sufficient for within-agent dedup. Format:
            # deterministic serialization of the four fields that uniquely
            # identify an LLM call's inputs.
            # SHA-256 hex digest (full 64-char, untruncated).
            _hash_input = (
                f"work_item={work_item!r},"
                f"model={_resolved_model!r},"
                f"max_tokens={_resolved_max_tokens!r},"
                f"temperature={_resolved_temp!r}"
            )
            idempotency_key = _hashlib.sha256(_hash_input.encode("utf-8")).hexdigest()

        # spec/48 HARD-REFUSE gate: a non-local (is_verified=False) caller that
        # sets conversation_id is refused at the door — BEFORE the idempotency
        # COMPLETED short-circuit, BEFORE lock acquisition, BEFORE the cost gate,
        # and BEFORE any storage I/O. The gate keys exclusively on is_verified —
        # never on object identity with LOCAL_PRINCIPAL (a fabricated
        # LOCAL_PRINCIPAL-shaped object with is_verified=False would bypass an
        # identity check but is correctly caught here). A caller without
        # conversation_id passes through unconditionally (single-shot calls are
        # always allowed).
        #
        # Placement (security-critical): this MUST run BEFORE the idempotency
        # Phase-1 lookup() short-circuit below. An unverified caller that supplies
        # BOTH a conversation_id AND an idempotency_key mapping to a COMPLETED
        # ledger record would otherwise be served the prior run's cached
        # result_ref + replayed_run_id WITHOUT the principal check ever firing —
        # letting a non-local caller replay/confirm another principal's completed
        # conversation-bearing run by guessing/replaying a caller-supplied key.
        # Gating identity FIRST (spec/48: "enforce is_verified at the door BEFORE
        # storage", "verify identity BEFORE the spend/audit path") closes that
        # hole and uniformly protects both the Phase-1 lookup-COMPLETED and the
        # Phase-2 begin-COMPLETED dedup-serve sites. No lock held, no lease
        # claimed, no idempotency lookup performed yet — nothing to unwind on
        # refuse.
        if conversation_id is not None and not principal.is_verified:
            _principal_refused_record: dict = {
                "trigger": self.trigger,
                "model": self.config.default_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "status": "principal_not_verified",
                "conversation_id": conversation_id,
                "principal_id": principal.identifier,
            }
            if caller_identity is not None:
                _principal_refused_record["http_caller"] = caller_identity
            if idempotency_key is not None:
                _principal_refused_record["idempotency_key"] = idempotency_key
            try:
                self._log(_principal_refused_record)
            except Exception:
                pass  # best-effort audit write — never mask the security refusal
            _finalize_call_span(
                error=UnverifiedPrincipalConversationAccess(
                    f"Principal is_verified=False for conversation_id={conversation_id!r}"
                )
            )
            raise UnverifiedPrincipalConversationAccess(
                f"Unverified principal attempted conversation access. "
                f"principal_id={principal.identifier!r}, "
                f"conversation_id={conversation_id!r}. "
                f"Derive a verified Principal via the registered PrincipalBackend "
                f"before passing conversation_id.",
                conversation_id=conversation_id,
                principal_id=principal.identifier,
            )

        # spec/45 PR2 — Phase 1: lookup() BEFORE lock acquire (reconciled order).
        # An idempotency_key lookup is a read-only probe that should short-circuit
        # BEFORE paying the lock-acquire cost. A COMPLETED decision returns the
        # cached result immediately (no LLM call, no lock). Mirrors the lock_busy
        # path in structure: write an audit record, finalize the span, return/raise.
        # IMPORTANT: this gate ONLY runs when idempotency_key is set; zero
        # behavioral change for callers that don't use dedup.
        #
        # The _serve_completed_dedup closure is defined INSIDE this guard so the
        # dominant idempotency_key=None path pays ZERO overhead (no closure
        # construction per call() — matches the spec/PR zero-overhead-None claim).
        # Both call sites that reference it (Phase-1 lookup-COMPLETED here, and the
        # Phase-2 begin-COMPLETED site below) only execute when idempotency_key is
        # not None, so the name is always bound before either site runs.
        if idempotency_key is not None:
            # spec/45 PR2 — shared COMPLETED-serve path. A COMPLETED decision can
            # surface in TWO places: Phase 1 lookup() (before the lock) AND Phase 2
            # begin() (after the cost gate, when a concurrent twin committed the key
            # between our lookup and our begin — the realistic lookup→commit→begin
            # race). Both serve the cached result identically: write a
            # status='deduped' audit record (cost_usd OMITTED per spec/22 addendum),
            # finalize the span with OUTCOME_DEDUPED, and return
            # Response.deduped_response. Factored into one closure so the two call
            # sites can never diverge (W7).
            def _serve_completed_dedup(_decision: "DedupDecision") -> Response:
                _dedup_record: dict = {
                    "trigger": self.trigger,
                    "model": self.config.default_model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "status": "deduped",
                    "summary": (
                        f"deduped: replayed from run {_decision.prior_run_id!r}"
                    ),
                    "idempotency_key": idempotency_key,
                    "replayed_run_id": _decision.prior_run_id,
                    # cost_usd intentionally ABSENT (not 0.0) per spec/22 addendum.
                }
                if caller_identity is not None:
                    _dedup_record["http_caller"] = caller_identity
                # spec/47 PR1 / spec/22 addendum: tag conversation_id on all terminal
                # JSONL records, including dedup short-circuit paths.
                if conversation_id is not None:
                    _dedup_record["conversation_id"] = conversation_id
                self._log(_dedup_record)
                _resp = Response.deduped_response(
                    prior_run_id=_decision.prior_run_id,
                    replayed_run_id=_decision.prior_run_id,
                    result_ref=_decision.prior_result_ref,
                    model=self.config.default_model,
                )
                _finalize_call_span(outcome=_tracing.OUTCOME_DEDUPED)
                return _resp

            try:
                _lookup_decision = self.idempotency_backend.lookup(idempotency_key)
            except (PathTraversalError, IdempotencyBackendError) as _lookup_exc:
                # Invalid key (caller bug) or I/O failure — finalize span as
                # error and propagate. Mirrors the non-LockBusy acquire except.
                _finalize_call_span(error=_lookup_exc)
                raise
            except BaseException as _lookup_other_exc:
                # spec/39 MUST 3: a NON-conforming backend that raises an
                # un-wrapped exception type (e.g. a bare OSError / RuntimeError)
                # from lookup() still runs BEFORE the body try/finally and before
                # lock acquire, so it must finalize the span here or the call span
                # leaks (never end()ed, context token never detached). The lock is
                # NOT held yet, so no lock leak. Mirrors the broad acquire except
                # below at the lock-acquire block. Re-raise unchanged.
                _finalize_call_span(error=_lookup_other_exc)
                raise
            if _lookup_decision.state == _DEDUP_COMPLETED:
                # COMPLETED: serve the cached result without running the LLM.
                # The spec/48 HARD-REFUSE gate above has ALREADY refused any
                # unverified conversation-bearing caller before this point, so a
                # cached result is only ever served to a caller that passed the
                # identity gate (verified principal, or no conversation_id at all).
                return _serve_completed_dedup(_lookup_decision)

        # Acquire agent lock via the bound LockBackend. Empty name maps
        # to ``<agent_root>/.lock`` on the filesystem backend — preserves
        # the legacy on-disk artifact so doctor + external scripts keep
        # working without migration (spec/21 §"acquire(name, timeout)
        # semantics"). ``LockBusy`` is the canonical exception; the
        # legacy ``AgentLockBusy`` alias keeps existing operator
        # except-clauses working unchanged (see ``atomic_agents.
        # exceptions.AgentLockBusy = LockBusy``).
        try:
            # Lock timeout by trigger:
            # - 'skill': 30s — skill-mode callers queue behind each other (expected
            #   sequential use by a Claude Code session; 30s is generous but bounded).
            # - 'http': 30s — HTTP callers also queue; returning LockBusy immediately
            #   (timeout=0) under any realistic concurrent load (two simultaneous pings)
            #   would make the server flaky. The serve layer serialises concurrent
            #   requests per agent through this timeout. spec/37 §"Concurrency contract".
            # - All others (manual, cron, api): 0 — CLI / cron callers fail-fast on
            #   lock contention; they are not expected to queue.
            _lock_timeout = 30 if self.trigger in ("skill", "http") else 0
            lock_handle = self.lock_backend.acquire("", timeout=_lock_timeout)
        except LockBusy as e:
            _lock_busy_record: dict = {
                "trigger": self.trigger,
                "model": self.config.default_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "status": "lock_busy",
                "summary": str(e),
            }
            # spec/37 MUST 7: include caller identity in refused-path records too,
            # so the audit trail can attribute lock-busy events to an HTTP caller.
            if caller_identity is not None:
                _lock_busy_record["http_caller"] = caller_identity
            # spec/47 PR1 / spec/22 addendum: tag conversation_id on ALL terminal
            # JSONL records (lock_busy, cost-skip, dedup, in_flight, ok).
            if conversation_id is not None:
                _lock_busy_record["conversation_id"] = conversation_id
            self._log(_lock_busy_record)
            # spec/39 MUST 3: end span on lock_busy path. This path raises BEFORE
            # the body try/finally is entered, so it finalizes its own span here.
            # lock_busy is a refusal, not a crash: mark ERROR status explicitly,
            # then let _finalize_call_span() set the outcome attribute + detach +
            # end idempotently.
            # spec/39 MUST 3: route through the non-throwing helper. The lock is
            # NOT held yet here, so a fault would not leak it — but it would
            # replace the real LockBusy refusal with a tracing error, breaking the
            # documented refusal contract. Keep the seam uniformly non-throwing.
            _tracing.safe_span_op(
                lambda: _call_span.set_status(
                    _tracing.StatusCode.ERROR, description="lock_busy"
                ),
                "set lock_busy status",
            )
            _finalize_call_span(outcome=_tracing.OUTCOME_LOCK_BUSY)
            raise
        except BaseException as _acq_exc:
            # spec/39 MUST 3: a NON-LockBusy acquire failure (e.g. PermissionError
            # on a read-only filesystem) also raises BEFORE the body try/finally
            # is entered, so it must finalize the span here or the span leaks.
            # Mark error + end + detach, then re-raise unchanged.
            _finalize_call_span(error=_acq_exc)
            raise

        # Track MCP tool names registered this call so we can clean them up in
        # finally even if an exception occurs mid-call (spec/19 fix M3).
        _mcp_registered_names: list[str] = []
        # spec/45 PR2: pre-body declaration so the finally can always read this flag
        # regardless of where an exception fires. Set True inside the body after
        # begin() returns FRESH; reset to False when commit() succeeds.
        _idempotency_lease_held: bool = False

        try:
            # Take Policy snapshot at call entry (#89 PR 3a / design Premise 3).
            # Frozen for the duration of this call() — operator edits to policy.md
            # mid-call are deferred to the NEXT call.
            self._policy_snapshot_this_call = self._take_policy_snapshot()
            # F1 fix (PR 3a Round 1 P0): MandateCheck is cached on
            # self._mandate_check (constructed lazily by _ensure_mandate_check).
            # The cached instance bakes in the FIRST call's policy_effective_caps.
            # Without this refresh, operators who tighten policy.md between calls
            # see Policy honored by _check_cost_guardrails (which reads the live
            # snapshot) but IGNORED by MandateCheck steps 7-8 (stale baked-in
            # caps). Mutate the cached instance to keep both surfaces consistent.
            if (
                self._mandate_check is not None
                and self._policy_snapshot_this_call is not None
            ):
                self._mandate_check._policy_effective_caps = (
                    self._policy_snapshot_this_call.effective_caps
                )
            # run_id was already reset before lock acquisition (see above) so
            # it is unique for this invocation even on the cost-skip and lock_busy
            # paths. The per-run accumulators below are still reset here (inside
            # the lock) so no parallel call can race them.
            # Reset helper-provenance rollup for this run (spec/13 Layer 3)
            self._helpers_this_run = []
            # Reset delegation rollup for this run
            self._delegations_this_run = []
            # Reset cumulative delegated cost for this run (fix R2-A2)
            self._delegated_cost_this_run = 0.0
            # Round 1 Finding 1: accumulate successful mandate cites across
            # iterations so the per-call agent_call cost event can carry the
            # mandate_id + proposal_id top-level fields (the LogQuery shape
            # _sum_prior_token_cost relies on). Single-mandate-per-call is the
            # common case; multi-mandate iterations accept a documented
            # apportionment gap (see CHANGELOG).
            self._successful_mandate_cites_this_call: list[tuple[str, str]] = []
            # #273: per-call dedup for tool-allowlist denial emissions. In log-only
            # mode (enforce_noncap=False) the LLM does not see a refusal and may
            # re-attempt a denied tool every iteration; without dedup the audit log
            # records N events per denied tool per call. In enforce mode the
            # synthesized policy_blocked ToolCallResult feeds back to the LLM and
            # naturally bounds re-attempts, but dedup is cheap and keeps the audit
            # shape uniform across both modes. MCP discovery already emits at most
            # once per server per call (single discovery loop) so no equivalent
            # set is needed there.
            self._policy_tool_denials_emitted_this_call: set[str] = set()

            # Cost guardrails check FIRST — before spinning up MCP subprocesses.
            # A call that will be skipped due to cost cap should not pay the
            # subprocess startup cost. (spec/19 fix M6)
            check = self._check_cost_guardrails(
                critical=critical,
                parent_remaining_headroom_usd=parent_remaining_headroom_usd,
            )
            if not check.allow:
                _skip_record: dict = {
                    "trigger": self.trigger,
                    "model": self.config.default_model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "status": "skipped",
                    "summary": f"Skipped: {check.reason}",
                }
                # Structured audit signal so a JSONL query can distinguish a
                # genuine cap hit from a blind-read fail-close without substring
                # matching prose (mirrors the coordinator dispatch-rejected event).
                if check.cost_data_degraded:
                    _skip_record["cost_data_degraded"] = True
                # spec/45 W6 (cost-skip variant): tag idempotency_key on the
                # pre-loop cost-skip record so a keyed run that was refused before
                # begin() still links to its key. The lease is NOT claimed on this
                # path (begin() runs AFTER the cost gate), so the key is recordable
                # but the ledger stays uncommitted and a retry re-runs.
                if idempotency_key is not None:
                    _skip_record["idempotency_key"] = idempotency_key
                # spec/37 MUST 7: include caller identity on the cost-skip path
                # so audit can attribute refused HTTP calls to their principals.
                if caller_identity is not None:
                    _skip_record["http_caller"] = caller_identity
                # spec/47 PR1 / spec/22 addendum: tag conversation_id on all
                # terminal JSONL records, including cost-skip refused paths.
                if conversation_id is not None:
                    _skip_record["conversation_id"] = conversation_id
                self._log(_skip_record)
                # spec/39 MUST 9: cost-skip carries OUTCOME_SKIPPED + ERROR
                # status. _call_total_cost stays 0.0 (no LLM call was made), so
                # the body finally records cost_usd=0.0. We set ERROR status here
                # (the finalizer only sets ERROR status for in-flight exceptions);
                # the returned skipped Response makes the finalizer derive
                # OUTCOME_SKIPPED, and the body finally owns detach + end.
                # spec/39 MUST 3: non-throwing — the body finally would still
                # release the lock here (the except below catches a propagating
                # tracing fault), but a fault would mask the skipped Response with
                # a tracing error. Keep the seam uniformly non-throwing.
                _tracing.safe_span_op(
                    lambda: _call_span.set_status(
                        _tracing.StatusCode.ERROR, description="cost_cap"
                    ),
                    "set cost_cap status (pre-loop skip)",
                )
                _call_response = Response.skipped_response(
                    check.reason, self.config.default_model
                )
                return _call_response

            # spec/45 PR2 — Phase 2: begin() AFTER cost gate (reconciled order).
            # begin() claims the lease ONLY when a cost-cap allows the run.
            # A cost-skipped call MUST NOT claim an idempotency key (nothing ran).
            # _idempotency_lease_held is pre-declared outside the body try (above).
            if idempotency_key is not None:
                # begin() exceptions (PathTraversalError / IdempotencyBackendError)
                # propagate to the body's ``except BaseException`` below, which
                # finalizes the span as error and re-raises. No local catch needed.
                _begin_decision = self.idempotency_backend.begin(
                    idempotency_key, self.run_id
                )
                if _begin_decision.state == _DEDUP_COMPLETED:
                    # spec/45 W7: a concurrent twin committed this key between our
                    # Phase-1 lookup() (which saw FRESH) and our begin(). begin()
                    # checks the terminal marker FIRST and returns COMPLETED. Serve
                    # the cached result identically to the Phase-1 lookup-COMPLETED
                    # path — NO LLM run, NO lease claimed (_idempotency_lease_held
                    # stays False), so the finally never releases a lease we don't
                    # own. The lock IS held and the finally releases it correctly.
                    # Omitting this branch silently double-spends (the P0).
                    _call_response = _serve_completed_dedup(_begin_decision)
                    return _call_response
                if _begin_decision.state == _DEDUP_IN_FLIGHT:
                    # Another call is already in progress for this key.
                    # Write a status='in_flight' audit record (cost_usd OMITTED per
                    # spec/22 addendum, replayed_run_id ABSENT) THEN raise.
                    _in_flight_record: dict = {
                        "trigger": self.trigger,
                        "model": self.config.default_model,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "status": "in_flight",
                        "summary": (
                            f"in_flight: concurrent call holds lease for key "
                            f"{idempotency_key!r}"
                        ),
                        "idempotency_key": idempotency_key,
                        # replayed_run_id intentionally ABSENT (no result served).
                        # cost_usd intentionally ABSENT per spec/22 addendum.
                    }
                    if caller_identity is not None:
                        _in_flight_record["http_caller"] = caller_identity
                    # spec/47 PR1 / spec/22 addendum: tag conversation_id on all terminal
                    # JSONL records, including in_flight dedup paths.
                    if conversation_id is not None:
                        _in_flight_record["conversation_id"] = conversation_id
                    self._log(_in_flight_record)
                    # Finalize span as IN_FLIGHT (a refusal, not an error — mirrors
                    # lock_busy span semantics per spec/39).
                    _tracing.safe_span_op(
                        lambda: _call_span.set_status(
                            _tracing.StatusCode.ERROR, description="in_flight"
                        ),
                        "set in_flight status",
                    )
                    _finalize_call_span(outcome=_tracing.OUTCOME_IN_FLIGHT)
                    raise DedupInFlight(
                        f"idempotency key {idempotency_key!r} is IN_FLIGHT "
                        f"(held by run {_begin_decision.prior_run_id!r})",
                        prior_run_id=_begin_decision.prior_run_id,
                    )
                else:
                    # state == FRESH: we own the lease.
                    _idempotency_lease_held = True

            # MCP client pool — lazy init (spec/19).
            # Only spin up when mcp.md declares servers and pool not yet live.
            # Discover tools and register them into the tool registry before
            # the first LLM call so the model sees the full tool list.
            #
            # Per spec/36 framework invariant (line 520): MCPClientPool consumes
            # mcp_servers_resolved (the materialized list from the registry
            # backend, populated in __init__ via replace()). This is the
            # substrate-agnostic spec list. AgentConfig.mcp_servers stays as
            # self._profile.mcp_servers (the filesystem-parse path) for backward
            # compat on existing log/audit consumers.
            #
            # IMPORTANT: an empty resolved list is AUTHORITATIVE, not a
            # missing-field signal. If the registry backend genuinely returns
            # [] (e.g., operator pinned an HTTP catalog that lists zero MCP
            # servers for this agent_scope), we MUST NOT fall back to
            # config.mcp_servers (which may carry stale mcp.md specs). Cross-
            # model review (Codex + Claude adversarial + plan-subagent prep
            # pass) all flagged the `... or self.config.mcp_servers` fallback
            # as the highest-priority issue: it lets the framework launch
            # subprocesses the backend explicitly removed. The check below
            # uses `hasattr` to distinguish "field missing entirely" from
            # "field present but empty" -- the field is added in this same
            # PR's Stream 2, so post-merge this always uses the resolved
            # path.
            if hasattr(self._profile, "mcp_servers_resolved"):
                _resolved_mcp_specs = list(self._profile.mcp_servers_resolved)
            else:
                _resolved_mcp_specs = list(self.config.mcp_servers)
            if _resolved_mcp_specs and self.mcp_pool is None:
                # ── #89 PR 3b: Policy MCP-allowlist consultation ────────
                # Consult Policy on each declared server. Emit a
                # policy_decision event (axis=mcp_allowlist) per denied
                # server. In log-only mode (enforce_noncap=False, PR 3b
                # default) all configured servers still connect; in
                # enforcement mode denied servers are filtered out before
                # the pool spins up so we don't pay the subprocess cost.
                effective_mcp_specs = _resolved_mcp_specs
                pol_snap = self._policy_snapshot_this_call
                if pol_snap is not None and pol_snap.mcp_allow_fn is not None:
                    from .policy.types import (
                        PolicyDecision,
                        _emit_policy_decision,
                    )

                    allowed_specs = []
                    for _spec in _resolved_mcp_specs:
                        if pol_snap.mcp_allow_fn(_spec.name):
                            allowed_specs.append(_spec)
                            continue
                        _emit_policy_decision(
                            PolicyDecision(
                                decision_kind="deny",
                                denying_layer="policy",
                                agent_name=self.name,
                                axis="mcp_allowlist",
                                mcp_server_name=_spec.name,
                                enforced=pol_snap.enforce_noncap,
                                cache_ttl_s=pol_snap.cache_ttl_s,
                            ),
                            self.log_backend,
                            run_id=self.run_id,
                        )
                        if not pol_snap.enforce_noncap:
                            # Log-only: server still connects.
                            allowed_specs.append(_spec)
                    effective_mcp_specs = allowed_specs

                if effective_mcp_specs:
                    # spec/36 MUST 12 — resolve the operator-overridable spawn
                    # allowlist from the AGENT'S LOCAL mcp.md (self._profile.
                    # mcp_md_raw), independent of which registry backend supplied
                    # the specs. mcp_md_raw is the canonical, round-tripping
                    # source already on the profile (Principle #1 — vault is the
                    # source of truth; no derived-state field to drift). When the
                    # '## Allowed commands' section is ABSENT, parse returns None
                    # → MCPClientPool falls back to DEFAULT_COMMAND_ALLOWLIST.
                    # When the section is PRESENT (even empty), the parsed set
                    # REPLACES the default (empty = deny-all). For HTTP/DB profile
                    # backends with no local mcp.md, mcp_md_raw is "" → None →
                    # default, which is the correct conservative behavior.
                    _allowed_commands = parse_mcp_allowed_commands(
                        getattr(self._profile, "mcp_md_raw", "") or ""
                    )
                    self.mcp_pool = MCPClientPool(
                        server_specs=effective_mcp_specs,
                        agents_root=self.agents_root,
                        allowed_commands=_allowed_commands,
                    )
                    self.mcp_pool.connect_all()
                    mcp_tool_defs = self.mcp_pool.discover_tools()
                    # Snapshot pre-existing tool names BEFORE registration so
                    # teardown can unregister ONLY the names this call added.
                    # A pre-existing operator tool with the same qualified name
                    # as an MCP tool raises ToolNameCollision (default
                    # refuse-to-overwrite) — collisions surface loudly here
                    # instead of silently shadowing the operator tool and then
                    # permanently deleting it in the finally block (#402).
                    _pre_existing = set(self.tool_registry.list_names())
                    for td in mcp_tool_defs:
                        self.tool_registry.register(td)  # default: refuse-to-overwrite
                        if td.name not in _pre_existing:
                            _mcp_registered_names.append(td.name)
                    _logger.debug(
                        "agent %r: MCP pool ready — %d tools from %d server(s)",
                        self.name,
                        len(mcp_tool_defs),
                        len(effective_mcp_specs),
                    )

            # PR 3b ESCALATE: opportunistic throttled poll of the
            # escalation queue. Operators (and the auto-decide-timeout
            # branch) resolve PENDING files asynchronously; on each
            # call() we check whether any are ready, emit RESOLVED audit
            # events, and execute Approved actions inline. Throttle
            # caps disk I/O at one scan per
            # judges_config.escalation.resolution_poll_cycle_seconds
            # (default 60s) per agent. Standalone CLI / cron poller
            # is tracked as a follow-up issue.
            if self._judge_enabled():
                self.poll_escalations()

            # Pick model — fallback if guardrail says so, else override, else default
            if check.action == "fallback" and check.fallback_model:
                model = check.fallback_model
            else:
                model = model_override or self.config.default_model

            # ── #89 PR 3b: Policy model-selection consultation ──────────
            # If Policy declares an effective model that differs from what
            # model.md declared as the agent's default, emit a
            # policy_decision event (decision_kind=override,
            # axis=model_selection). model_from_md is the model.md value
            # itself, not the post-cost-cap-fallback effective model — the
            # audit contrast operators want is "Policy vs model.md", and
            # cost-cap fallback decisions are their own policy_decision
            # event family from PR 3a. In log-only mode (enforce_noncap
            # False, PR 3b default) the prior effective model is kept and
            # the emission is informational. In enforcement mode Policy's
            # choice replaces it; cost-cap caps still apply per iteration
            # and can trigger a fallback on the NEXT iteration regardless.
            #
            # NOTE: decision_kind="override" is emitted in log-only mode
            # too. The enforced=False field is the disambiguator —
            # operators reading the audit log see "would-have-been
            # override" while the action still proceeded with the
            # pre-Policy model.
            pol_snap = self._policy_snapshot_this_call
            _md_default_model = self.config.default_model
            # PR 4 R1 P0-2 fix: compare against the actual pre-Policy effective
            # model (the `model` local already resolved at line 3203-3207 via
            # cost-cap fallback / per-call kwarg / model.md default) instead of
            # `_md_default_model`. The original gate missed the silent-override
            # case where model.md and policy.md agree but the operator's per-call
            # kwarg differs from both: pre-fix, Policy stomped the kwarg with
            # zero audit signal. Post-fix, an override emission fires whenever
            # Policy's value differs from what would have been used.
            _pre_policy_model = model
            if (
                pol_snap is not None
                and pol_snap.model_override is not None
                and pol_snap.model_override != _pre_policy_model
            ):
                from .policy.types import (
                    PolicyDecision,
                    _emit_policy_decision,
                )

                _emit_policy_decision(
                    PolicyDecision(
                        decision_kind="override",
                        denying_layer=None,
                        agent_name=self.name,
                        axis="model_selection",
                        model_from_md=_md_default_model,
                        model_from_policy=pol_snap.model_override,
                        # #274: surface the per-call kwarg in the audit so
                        # operators can distinguish "Policy overrode model.md"
                        # from "Policy overrode the caller's explicit choice."
                        # PR 4 R1 P0-1 fix: only populate when the kwarg was
                        # actually superseded — kwarg supplied AND differs from
                        # Policy's value. When the kwarg matches Policy's value
                        # the operator's choice and Policy aligned and no
                        # kwarg-override occurred (the field would be a lie).
                        model_from_per_call_override=(
                            model_override
                            if (
                                model_override is not None
                                and model_override != pol_snap.model_override
                            )
                            else None
                        ),
                        enforced=pol_snap.enforce_noncap,
                        cache_ttl_s=pol_snap.cache_ttl_s,
                    ),
                    self.log_backend,
                    run_id=self.run_id,
                )
                if pol_snap.enforce_noncap:
                    model = pol_snap.model_override
                # Log-only: keep the pre-Policy model.

            # Build prompt
            system_prompt = self.assemble_system_prompt()

            # spec/47 PR1: resolve conversation backend (three-channel seam).
            # Called here (after system_prompt is assembled) so the backend
            # resolver can read self.config which is set during self.load().
            # Cached on self._conversation_backend_resolved after first call.
            #
            # GATED on conversation_id: a single-shot call (conversation_id=None)
            # MUST NOT be broken by a conversation-backend misconfiguration (MUST 9
            # backward-compatibility). Resolution can raise BackendNotRegistered
            # for a bad ATOMIC_AGENTS_CONVERSATION_BACKEND env var / model.md field;
            # if we resolved unconditionally that would crash EVERY call — even
            # ones that never asked for a conversation. So only resolve when a
            # conversation_id was supplied. (_resolve_conversation_backend() ALSO
            # fails soft to None internally for channels (2)/(3) — defense in depth.)
            if conversation_id is not None:
                _conv_backend = self._resolve_conversation_backend()
            else:
                _conv_backend = None

            # spec/47 PR1: inject prior turns as real role-tagged message entries
            # BEFORE the current work_item turn. This preserves the T14 cacheable
            # prefix (assemble_system_prompt() is NOT touched — see TENSIONS T16,
            # approved). The token budget is conservative for PR1 (8000 tokens);
            # model-aware derivation (model_context_limit - system_prompt_tokens
            # - max_output_tokens) is deferred to the spec/47 LOCK PR.
            # Fail-open on load failure: prior turns are non-critical context;
            # if the backend is degraded the call still runs with single-shot behavior.
            _prior_turns_as_messages: list[dict] = []
            if conversation_id is not None and _conv_backend is not None:
                from .conversation import (  # noqa: PLC0415
                    ConversationBackendError as _ConvBackendError,
                )

                # TODO(spec/47 LOCK): derive budget from model_context_limit
                # per-model table. For PR1 DRAFT use a conservative static budget
                # (8000 tokens) — well below any model's context limit, large
                # enough for ~30 turns of typical conversation. This avoids
                # silent token-overflow before the per-model table ships.
                _conv_budget_tokens = 8000
                try:
                    _prior_turns = _conv_backend.load_turns(
                        principal,  # spec/48: use caller-supplied principal (not hardcoded LOCAL_PRINCIPAL)
                        conversation_id,
                        budget_tokens=_conv_budget_tokens,
                    )
                    _prior_turns_as_messages = [
                        {"role": t.role, "content": t.content} for t in _prior_turns
                    ]
                # Catch BOTH the backend base error AND PathTraversalError: a
                # malformed caller-supplied conversation_id makes load_turns()
                # raise PathTraversalError, which is a SIBLING of AtomicAgentsError
                # (NOT a ConversationBackendError subclass). The comment above
                # promises fail-open to single-shot on load failure; a bad
                # conversation_id is the realistic caller-input case and must
                # degrade, not crash the billed call. (lesson: catch the base/
                # widest class a use-site can raise.)
                except (_ConvBackendError, PathTraversalError) as _load_exc:
                    _logger.warning(
                        "ConversationBackend load_turns() failed for conversation_id=%r "
                        "(run_id=%s) — falling back to single-shot (no prior context): %s",
                        conversation_id,
                        self.run_id,
                        _load_exc,
                    )
                    _prior_turns_as_messages = []

            # Normalize the injected prior-turn sequence before building messages[]
            # so the provider API does not reject the request:
            #   (a) drop empty-content turns (an assistant turn that only emitted
            #       tool calls persists content='' — an empty content block is
            #       rejected by Anthropic);
            #   (b) collapse consecutive same-role entries (keep the latest).
            #       CONTEXT-LOSS CAVEAT (PR1, tracked for spec/47 §normalization):
            #       this is NOT just formatting dedupe — it can silently DROP a
            #       real prior turn. When step (a) removes an empty-content turn
            #       BETWEEN two same-role turns (e.g. [user_a, assistant_'',
            #       user_b] -> after (a): [user_a, user_b]), step (b) then keeps
            #       only user_b and discards user_a's message. The drop is silent:
            #       continuity_persisted stays True and the caller cannot detect
            #       the lost turn. This only fires on an empty-content adjacency
            #       (tool-only assistant turn between two user turns), is rare in
            #       PR1, and keeps output alternation valid (no provider 400). The
            #       LOCK PR's model-aware budget work should revisit whether to
            #       concatenate same-role content instead of discarding the older;
            #   (c) drop a trailing user turn — it would sit immediately before
            #       the new work_item user turn (consecutive same-role, rejected),
            #       and the orphan trailing-user case (failed assistant write-back)
            #       is documented as possible in the PR1 crash boundary.
            _normalized_prior: list[dict] = []
            for _m in _prior_turns_as_messages:
                if not _m.get("content"):
                    continue  # (a) drop empty content
                if _normalized_prior and _normalized_prior[-1]["role"] == _m["role"]:
                    _normalized_prior[-1] = _m  # (b) collapse same-role, keep latest
                    continue
                _normalized_prior.append(_m)
            # (c) a trailing user turn would collide with the work_item user turn.
            if _normalized_prior and _normalized_prior[-1]["role"] == "user":
                _normalized_prior.pop()
            # (d) drop LEADING assistant turns. The provider rejects a request
            #     whose first message role is 'assistant' (Anthropic: "first
            #     message must use the user role"). This is the SYMMETRIC case to
            #     (c): budget eviction is newest-first, so when it cuts mid-pair
            #     the oldest KEPT turn can be an assistant turn (kept order
            #     [assistant,user,assistant,...]); a corruption-skipped leading
            #     user turn can also orphan a leading assistant. None of the LLM
            #     backends normalize a leading-assistant array, so drop it here.
            while _normalized_prior and _normalized_prior[0]["role"] == "assistant":
                _normalized_prior.pop(0)

            # Build messages: normalized prior turns (oldest first) then the
            # current work_item. The LLM sees the full conversation history
            # followed by the new user turn, with strict role alternation.
            messages: list[dict] = [
                *_normalized_prior,
                {"role": "user", "content": work_item},
            ]

            # Tool definitions for the LLM — includes atomic_capture + custom tools.
            # None for providers without tool-call support.
            tool_definitions = self._all_tool_definitions(model)

            # Accumulators across multi-turn loop iterations
            all_tool_call_results: list[ToolCallResult] = []
            all_captures: list[Capture] = []
            all_parse_failures: list = []
            total_input_tokens = 0
            total_output_tokens = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0
            total_cost = 0.0
            cost_fallback = False
            tool_iterations_maxed = False
            iteration_count = 0
            last_raw = None
            # In-flight cost accumulator — tracks spend from current loop iterations
            # that hasn't landed in the persisted log yet. Passed to
            # _check_cost_guardrails so mid-loop cap checks see the true running
            # total, not just the pre-call on-disk snapshot. (fix R2-A1)
            accumulated_loop_cost_usd: float = 0.0
            # PR 3b ESCALATE: accumulate queue_ids across iterations
            # (a single tool-loop iteration can produce multiple
            # deferred tool_uses; the loop terminates immediately, but
            # Response.escalation_queue_ids reflects all of them).
            accumulated_escalation_queue_ids: list[str] = []

            # ── Multi-turn tool loop ──────────────────────────────
            start_total = time.time()
            while True:
                iteration_count += 1
                # Reset per-iteration mandate-proposal side-channel (#124 PR 3b).
                # Populated by _dispatch_with_judge when ensemble ALLOWs a
                # mandate-citing proposal; consumed by the tool-execution loop
                # below for reservation create/commit/rollback + post-action
                # verification. Cleared every iteration so stale proposals from
                # prior iterations don't affect the current one.
                self._mandate_allowed_proposals = {}
                # Per-iteration reservation tracking: tool_call_id → reservation_id.
                # Used to commit on success or rollback on error.
                _mandate_reservations: dict[str, str] = {}

                # Inter-iteration lock-loss check (#60 PR 3 + spec/21
                # §"Lease and heartbeat"). For lease-backed backends
                # (Redis, future Postgres advisory) the heartbeat thread
                # spawned at acquire() may detect lease expiry mid-call.
                # Surface as ``LockLost`` BEFORE the next LLM round-trip
                # so the agent aborts cleanly instead of writing under
                # a lock another holder now owns. No-op for the
                # filesystem default (``supports_lease=False``).
                check_lock_lost(lock_handle)

                # Pre-check cost cap before each iteration (except first, already checked).
                # Pass the in-flight accumulator so the guardrail sees spend that
                # has not yet been persisted to the log file. (fix R2-A1)
                if iteration_count > 1:
                    iter_check = self._check_cost_guardrails(
                        critical=critical,
                        extra_in_flight_cost_usd=accumulated_loop_cost_usd,
                        parent_remaining_headroom_usd=parent_remaining_headroom_usd,
                    )
                    if not iter_check.allow:
                        # Cap hit mid-loop — return what we have with skipped=True
                        latency_ms = int((time.time() - start_total) * 1000)
                        skip_reason = f"cost cap hit at iteration {iteration_count}: {iter_check.reason}"
                        response = Response(
                            text=last_raw.text if last_raw else "",
                            model=model,
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                            cache_hit_tokens=total_cache_hit_tokens,
                            cache_miss_tokens=total_cache_miss_tokens,
                            cost_usd=total_cost,
                            cost_estimated_via_fallback=cost_fallback,
                            latency_ms=latency_ms,
                            summary=self._derive_summary(work_item),
                            raw=last_raw.raw or {} if last_raw else {},
                            captures=all_captures,
                            skipped=True,
                            skip_reason=skip_reason,
                            tool_calls=all_tool_call_results,
                            tool_iterations=iteration_count - 1,
                            # No turn write-back runs on the mid-loop cost-cap
                            # early return — do NOT over-claim continuity. When a
                            # conversation_id was supplied, report that history was
                            # NOT persisted so a caller can decide to retry/repair.
                            continuity_persisted=(conversation_id is None),
                        )
                        _mid_loop_skip: dict = {
                            "trigger": self.trigger,
                            "model": model,
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                            "cost_usd": total_cost,
                            "cost_source": "actor",
                            "latency_ms": latency_ms,
                            "status": "skipped",
                            "summary": skip_reason,
                            "run_id": self.run_id,
                        }
                        # Structured degraded signal (audit symmetry with the
                        # pre-loop skip + coordinator dispatch-rejected event).
                        if iter_check.cost_data_degraded:
                            _mid_loop_skip["cost_data_degraded"] = True
                        # spec/45 W6 (cost-skip variant): tag idempotency_key on
                        # the mid-loop cost-skip record. This path is reached AFTER
                        # begin() returned FRESH (the lease IS held), so a keyed run
                        # that spent money on iteration N then hit the cap mid-loop
                        # must still link to its key. The skip returns via the body
                        # try, so the finally releases the lease (NOT commit()ed) —
                        # a retry re-runs.
                        if idempotency_key is not None:
                            _mid_loop_skip["idempotency_key"] = idempotency_key
                        # spec/37 MUST 7: include http_caller on ALL HTTP-triggered
                        # terminal records, including this mid-loop cost-cap path.
                        # The pre-loop cost-skip (3398-3401) and lock_busy (3325-3327)
                        # paths already inject it; this is the fourth terminal parent
                        # record that must also carry it.
                        if caller_identity is not None:
                            _mid_loop_skip["http_caller"] = caller_identity
                        # spec/47 PR1 / spec/22 addendum: tag conversation_id on
                        # this terminal record too — a keyed conversation that
                        # spends money then hits the cap mid-loop must still be
                        # visible to LogQuery(conversation_id=...). Mirrors the
                        # idempotency_key/http_caller tagging just above.
                        if conversation_id is not None:
                            _mid_loop_skip["conversation_id"] = conversation_id
                        self._log(_mid_loop_skip)
                        # spec/39 MUST 1 / MUST 9: mid-loop cost-cap. Sync the
                        # span accumulators from the local loop accumulators so
                        # the body finally records the true partial spend (NOT
                        # 0.0). Set ERROR status here; the returned skipped
                        # Response makes the finalizer derive OUTCOME_SKIPPED, and
                        # the body finally owns detach + end.
                        _call_total_cost = total_cost
                        _call_input_tokens = total_input_tokens
                        _call_output_tokens = total_output_tokens
                        _call_tool_iterations = iteration_count - 1
                        _call_model = model
                        # spec/39 MUST 3: non-throwing seam (see pre-loop skip).
                        _tracing.safe_span_op(
                            lambda: _call_span.set_status(
                                _tracing.StatusCode.ERROR, description="cost_cap"
                            ),
                            "set cost_cap status (mid-loop)",
                        )
                        _call_response = response
                        return _call_response

                iter_start = time.time()
                raw = _llm.call_llm(
                    model=model,
                    system_prompt=system_prompt,
                    messages=messages,
                    max_tokens=max_tokens or self.config.max_output_tokens,
                    temperature=temperature if temperature is not None else 0.6,
                    cache_control_breakpoints=[len(system_prompt)]
                    if iteration_count == 1
                    else None,
                    tools=tool_definitions,
                    preferred_provider=self.config.provider,
                )
                iter_latency_ms = int((time.time() - iter_start) * 1000)
                last_raw = raw

                iter_cost, iter_cost_fallback = _costs.calc_cost(
                    model, raw.input_tokens, raw.output_tokens, raw.cache_hit_tokens
                )
                total_input_tokens += raw.input_tokens
                total_output_tokens += raw.output_tokens
                total_cache_hit_tokens += raw.cache_hit_tokens
                total_cache_miss_tokens += raw.cache_miss_tokens
                total_cost += iter_cost
                # Track in-flight spend so mid-loop cap checks see the running
                # total before the parent log line is written. (fix R2-A1)
                accumulated_loop_cost_usd += iter_cost
                # spec/39 MUST 1: sync the span accumulators from the loop-local
                # totals at the END of every iteration, immediately after the
                # spend lands. This is what makes a raise that fires AFTER real
                # LLM spend (a tool dispatch, a mandate check, capture write /
                # extract, or Response assembly raising mid/post-loop) carry the
                # true partial spend into the error span instead of 0.0. The
                # success / cost-skip / mid-loop-cap paths re-sync to their exact
                # final values below; this per-iteration sync is the floor that
                # guarantees the error path is never under-reported.
                _call_total_cost = total_cost
                _call_input_tokens = total_input_tokens
                _call_output_tokens = total_output_tokens
                _call_tool_iterations = iteration_count
                _call_model = model
                if iter_cost_fallback:
                    cost_fallback = True

                # Extract captures from this iteration (Path 1 + Path 2)
                iter_captures, iter_failures = _capture.extract_all_captures(
                    raw.text,
                    tool_uses=raw.tool_uses,
                )
                all_captures.extend(iter_captures)
                all_parse_failures.extend(iter_failures)

                # Partition tool_uses: framework-managed (atomic_capture,
                # atomic_action) handled above / by judge dispatch, vs
                # custom tools the operator registered.
                from .judge.proposal import is_framework_managed_tool

                custom_tool_uses = [
                    tu
                    for tu in raw.tool_uses
                    if not is_framework_managed_tool(tu.get("name", ""))
                    and self.tool_registry.get(tu.get("name", "")) is not None
                ]
                unknown_tool_uses = [
                    tu
                    for tu in raw.tool_uses
                    if not is_framework_managed_tool(tu.get("name", ""))
                    and self.tool_registry.get(tu.get("name", "")) is None
                    and tu.get("name", "")  # non-empty name
                ]

                # Log any tool calls to unknown tools (model hallucinated a tool name)
                for tu in unknown_tool_uses:
                    _logger.warning(
                        "agent %r: LLM called unknown tool %r (not in registry)",
                        self.name,
                        tu.get("name", ""),
                    )
                    self._log(
                        {
                            "trigger": "tool_call",
                            "parent_run_id": self.run_id,
                            "tool_name": tu.get("name", ""),
                            "latency_ms": 0,
                            "error": "ToolNotRegistered",
                        }
                    )

                # Judge layer (#112 PR 2a). Extract atomic_action markers and
                # dispatch the judge ensemble for each side-effectful tool_use
                # BEFORE handler execution. Spec/28 §"Where the judge sits in
                # agent.call()". Opt-in per _judge_enabled() — when disabled
                # (no judges.md, no AGENT_JUDGE_ENABLED env var) the dispatch
                # is skipped entirely and today's pre-#112 behavior runs.
                judge_blocked: dict[str, str] = {}  # tool_call_id -> block reason
                # PR 3b ESCALATE: tool_call_id -> escalation_queue_id.
                # Deferred tool_uses don't execute this turn; the actor's
                # call() returns deferred=True after the iteration.
                judge_deferred: dict[str, str] = {}
                if self._judge_enabled() and custom_tool_uses:
                    from .judge.atomic_action import extract_atomic_action_markers
                    from .exceptions import JudgeProposalInvalid

                    try:
                        markers = extract_atomic_action_markers(raw.tool_uses)
                    except JudgeProposalInvalid as exc:
                        # Marker-level malformation (duplicate / missing
                        # for_tool_call_id). Per failure_policy default,
                        # block ALL side-effectful tool_uses this iteration.
                        _logger.warning(
                            "agent %r: judge layer marker extraction failed: %s",
                            self.name,
                            exc,
                        )
                        markers = {}
                        for tu in custom_tool_uses:
                            tcid = tu.get("id", "")
                            judge_blocked[tcid] = (
                                f"JudgeProposalInvalid (marker extraction): {exc}"
                            )
                    if not judge_blocked:
                        for tu in custom_tool_uses:
                            try:
                                allow, events, queue_id = self._dispatch_with_judge(
                                    tu,
                                    markers,
                                )
                            except Exception as exc:  # noqa: BLE001
                                # Defensive: any uncaught judge-path error
                                # fail-closes per spec/28's failure_policy
                                # default. Better to block than silently let
                                # the action run with no audit record.
                                _logger.exception(
                                    "agent %r: judge dispatch raised; "
                                    "fail-closing to BLOCK for tool_call_id=%r",
                                    self.name,
                                    tu.get("id", ""),
                                )
                                judge_blocked[tu.get("id", "")] = (
                                    f"judge dispatch error: {type(exc).__name__}: {exc}"
                                )
                                continue
                            # Per-judge audit lines (#112 PR 2b ensemble).
                            # First BLOCK in the ensemble short-circuits
                            # remaining judges; this loop records every
                            # judge that actually ran.
                            for event in events:
                                self._log(event)
                            if queue_id is not None:
                                # PR 3b ESCALATE: PENDING file already
                                # written by _dispatch_with_judge. Mark
                                # this tool_use as deferred so it does
                                # not execute this turn; the actor's
                                # call() returns with deferred=True.
                                judge_deferred[tu.get("id", "")] = queue_id
                            elif not allow:
                                # The LAST event in the list is the BLOCKing
                                # judge — the others ALLOWed. Its reason is
                                # what flows back to the actor.
                                judge_blocked[tu.get("id", "")] = events[-1].get(
                                    "judgment_reason", "judge_blocked"
                                )
                            else:
                                # Ensemble ALLOWed. If this is a mandate-citing
                                # proposal, create a reservation (#124 PR 3b).
                                tcid = tu.get("id", "")
                                mandate_proposal = self._mandate_allowed_proposals.get(
                                    tcid
                                )
                                if (
                                    mandate_proposal is not None
                                    and mandate_proposal.authorization is not None
                                ):
                                    mandate_id = mandate_proposal.authorization.granted_by.removeprefix(
                                        "mandate:"
                                    )
                                    scope = f"agent:{self.agent_root.name}"
                                    mgr = self._mandate_reservation_managers.get(scope)
                                    if mgr is not None:
                                        tool_def_for_cost = self.tool_registry.get(
                                            mandate_proposal.tool_name
                                        )
                                        cost_kind = (
                                            "external"
                                            if tool_def_for_cost is not None
                                            and (
                                                tool_def_for_cost.expected_external_cost_usd
                                                is not None
                                                or tool_def_for_cost.cost_estimator_id
                                                is not None
                                            )
                                            else "token"
                                        )
                                        # Round 1 Finding 6 fix: read the
                                        # projection MandateCheck cached during
                                        # evaluate() so the reservation carries
                                        # non-zero headroom. compute_outstanding
                                        # sums these projections into cumulative,
                                        # which is the stale-budget race defense.
                                        mc_for_projection = getattr(
                                            self, "_mandate_check", None
                                        )
                                        if mc_for_projection is not None:
                                            (
                                                proj_token,
                                                proj_external,
                                            ) = mc_for_projection.pop_projection(
                                                mandate_proposal.proposal_id
                                            )
                                            projected_usd_for_rsvp = (
                                                proj_external
                                                if cost_kind == "external"
                                                else proj_token
                                            )
                                        else:
                                            projected_usd_for_rsvp = 0.0
                                        try:
                                            rid = mgr.create(
                                                mandate_id=mandate_id,
                                                proposal_id=mandate_proposal.proposal_id,
                                                cost_kind=cost_kind,
                                                projected_usd=projected_usd_for_rsvp,
                                                run_id=self.run_id,
                                                parent_run_id=None,
                                            )
                                            _mandate_reservations[tcid] = rid
                                        except Exception:
                                            _logger.warning(
                                                "agent %r: mandate reservation create "
                                                "failed for tool_call_id=%r mandate_id=%r "
                                                "— execution proceeds (best-effort)",
                                                self.name,
                                                tcid,
                                                mandate_id,
                                            )

                # Execute custom tools
                iter_tool_results: list[ToolCallResult] = []
                for tu in custom_tool_uses:
                    tcid = tu.get("id", "")
                    if tcid in judge_blocked:
                        # Judge BLOCKed — synthesize an error tool_result so
                        # the LLM sees the refusal on the next turn, without
                        # running the handler. The reason flows back to the
                        # actor verbatim per spec/28 §"Block".
                        from .tools import ToolCallResult as _TCR

                        blocked_result = _TCR(
                            tool_name=tu.get("name", ""),
                            tool_use_id=tcid,
                            input=tu.get("input", {}) or {},
                            output=None,
                            error=f"judge_blocked: {judge_blocked[tcid]}",
                            latency_ms=0,
                        )
                        all_tool_call_results.append(blocked_result)
                        iter_tool_results.append(blocked_result)
                        self._log(
                            {
                                "trigger": "tool_call",
                                "parent_run_id": self.run_id,
                                "tool_name": blocked_result.tool_name,
                                "latency_ms": 0,
                                "error": blocked_result.error,
                            }
                        )
                        continue
                    if tcid in judge_deferred:
                        # PR 3b ESCALATE: PENDING file already written.
                        # Synthesize a "deferred" tool_result for the
                        # audit trail but do NOT execute the handler.
                        # The actor's call() returns deferred=True after
                        # this iteration; no further multi-turn loop.
                        # ``deferred=True`` is the structural signal
                        # consumers iterate on (Codex round-1 P2-4 fix);
                        # ``error`` carries the same info as prose for
                        # humans reading the JSONL log. Distinct trigger
                        # ``tool_call_deferred`` keeps dashboard failure
                        # counts honest (P2-5 fix).
                        from .tools import ToolCallResult as _TCR

                        deferred_result = _TCR(
                            tool_name=tu.get("name", ""),
                            tool_use_id=tcid,
                            input=tu.get("input", {}) or {},
                            output=None,
                            error=(
                                f"judge_deferred: ESCALATE — see "
                                f"escalation_queue_id={judge_deferred[tcid]}"
                            ),
                            latency_ms=0,
                            deferred=True,
                        )
                        all_tool_call_results.append(deferred_result)
                        iter_tool_results.append(deferred_result)
                        self._log(
                            {
                                "trigger": "tool_call_deferred",
                                "parent_run_id": self.run_id,
                                "tool_name": deferred_result.tool_name,
                                "latency_ms": 0,
                                "error": deferred_result.error,
                                "escalation_queue_id": judge_deferred[tcid],
                            }
                        )
                        continue

                    # ── #89 PR 3b: Policy tool-allowlist consultation ────
                    # Consult the per-call frozen Policy snapshot. On deny:
                    # emit a policy_decision event (axis=tool_allowlist).
                    # In log-only mode (enforce_noncap=False, PR 3b default),
                    # the action still proceeds — the audit trail records the
                    # would-be denial so operators can verify policy before
                    # PR 4 flips the flag. In enforcement mode, synthesize a
                    # policy_blocked tool_result mirroring the judge_blocked
                    # shape so the LLM sees the refusal on the next turn.
                    pol_snap = self._policy_snapshot_this_call
                    if pol_snap is not None and pol_snap.tool_allow_fn is not None:
                        _tool_name = tu.get("name", "")
                        if not pol_snap.tool_allow_fn(_tool_name):
                            from .policy.types import (
                                PolicyDecision,
                                _emit_policy_decision,
                            )

                            # #273: emit at most one event per (tool_name, call).
                            # In log-only mode, the LLM does not see refusals and
                            # may re-attempt the same denied tool every iteration;
                            # without dedup the audit log spams N events. In
                            # enforce mode the synthesized ToolCallResult bounds
                            # re-attempts via LLM feedback, but dedup keeps the
                            # audit shape uniform across modes.
                            if (
                                _tool_name
                                not in self._policy_tool_denials_emitted_this_call
                            ):
                                _emit_policy_decision(
                                    PolicyDecision(
                                        decision_kind="deny",
                                        denying_layer="policy",
                                        agent_name=self.name,
                                        axis="tool_allowlist",
                                        tool_name=_tool_name,
                                        enforced=pol_snap.enforce_noncap,
                                        cache_ttl_s=pol_snap.cache_ttl_s,
                                    ),
                                    self.log_backend,
                                    run_id=self.run_id,
                                )
                                self._policy_tool_denials_emitted_this_call.add(
                                    _tool_name
                                )
                            if pol_snap.enforce_noncap:
                                from .tools import ToolCallResult as _TCR

                                policy_blocked_result = _TCR(
                                    tool_name=_tool_name,
                                    tool_use_id=tcid,
                                    input=tu.get("input", {}) or {},
                                    output=None,
                                    error=(
                                        f"policy_denied: tool {_tool_name!r} "
                                        "not allowed by policy.md"
                                    ),
                                    latency_ms=0,
                                )
                                all_tool_call_results.append(policy_blocked_result)
                                iter_tool_results.append(policy_blocked_result)
                                self._log(
                                    {
                                        "trigger": "tool_call",
                                        "parent_run_id": self.run_id,
                                        "tool_name": _tool_name,
                                        "latency_ms": 0,
                                        "error": policy_blocked_result.error,
                                    }
                                )
                                continue
                            # Flag-off: log-only, fall through to execute.

                    tool_result = self.tool_registry.execute(tu)
                    all_tool_call_results.append(tool_result)
                    iter_tool_results.append(tool_result)
                    # Per-tool JSONL log line. For mandate-citing ALLOWed
                    # tools, extend with mandate_id + proposal_id in extra
                    # so the cost event IS the mandate_used surface per
                    # spec/29 Risk 4 amendment (#124 PR 3b).
                    mandate_proposal = self._mandate_allowed_proposals.get(tcid)
                    tool_log_record: dict = {
                        "trigger": "tool_call",
                        "parent_run_id": self.run_id,
                        "tool_name": tool_result.tool_name,
                        "latency_ms": tool_result.latency_ms,
                        "error": tool_result.error,
                    }
                    if (
                        mandate_proposal is not None
                        and mandate_proposal.authorization is not None
                        and mandate_proposal.authorization.granted_by.startswith(
                            "mandate:"
                        )
                    ):
                        _mid = mandate_proposal.authorization.granted_by.removeprefix(
                            "mandate:"
                        )
                        tool_log_record["mandate_id"] = _mid
                        tool_log_record["proposal_id"] = mandate_proposal.proposal_id
                    self._log(tool_log_record)

                    # Reservation lifecycle (#124 PR 3b):
                    # - On tool error → rollback (spec/29 ordering).
                    # - On success → commit AFTER cost event (Risk 6:
                    #   commit first, verify after).
                    # Verification fires last (Risk 6: after commit).
                    _rid = _mandate_reservations.get(tcid)
                    if _rid is not None:
                        _scope_for_rid = f"agent:{self.agent_root.name}"
                        _mgr_for_rid = self._mandate_reservation_managers.get(
                            _scope_for_rid
                        )
                        if _mgr_for_rid is not None:
                            if tool_result.error is not None:
                                # Tool errored — rollback reservation.
                                try:
                                    _mgr_for_rid.rollback(
                                        _rid,
                                        reason="tool_error",
                                        run_id=self.run_id,
                                    )
                                except Exception:
                                    _logger.warning(
                                        "agent %r: mandate reservation rollback "
                                        "failed for rid=%r",
                                        self.name,
                                        _rid,
                                    )
                            else:
                                # Tool succeeded — commit reservation
                                # (spec/29 Risk 6: commit before verify).
                                try:
                                    _mgr_for_rid.commit(
                                        _rid,
                                        actual_usd=0.0,  # tool cost is zero for token-class; external-class actual tracked via cost event
                                        run_id=self.run_id,
                                    )
                                    # Round 1 Finding 1: record this successful
                                    # mandate cite so the per-call agent_call
                                    # cost event can carry mandate_id+proposal_id
                                    # (the LogQuery shape _sum_prior_token_cost
                                    # relies on for cumulative-spend defense).
                                    if (
                                        mandate_proposal is not None
                                        and mandate_proposal.authorization is not None
                                        and mandate_proposal.authorization.granted_by.startswith(
                                            "mandate:"
                                        )
                                    ):
                                        _mid_for_ledger = mandate_proposal.authorization.granted_by.removeprefix(
                                            "mandate:"
                                        )
                                        self._successful_mandate_cites_this_call.append(
                                            (
                                                _mid_for_ledger,
                                                mandate_proposal.proposal_id,
                                            )
                                        )
                                except Exception:
                                    _logger.warning(
                                        "agent %r: mandate reservation commit "
                                        "failed for rid=%r",
                                        self.name,
                                        _rid,
                                    )

                    # Post-action verification (spec/29 Risk 6: after cost
                    # commit). No-op for non-mandate or wrong action class.
                    if mandate_proposal is not None and tool_result.error is None:
                        self._verify_post_action(mandate_proposal, tool_result)

                # If no custom tools were called, the loop is done
                if not custom_tool_uses:
                    break

                # PR 3b ESCALATE: any deferred tool_use breaks the
                # multi-turn loop. ALLOWed tool_results stay in
                # all_tool_call_results, deferred ones already recorded
                # an audit-only error result. Actor's call() returns
                # with deferred=True so the caller (operator harness,
                # parent agent, CLI) sees the run paused.
                if judge_deferred:
                    accumulated_escalation_queue_ids.extend(judge_deferred.values())
                    break

                # Check if we've hit the iteration cap
                if iteration_count >= self.max_tool_iterations:
                    tool_iterations_maxed = True
                    break

                # Build follow-up messages with tool_result blocks so the LLM
                # can incorporate results in the next turn.
                # We build the assistant's tool_use blocks + the tool_result blocks
                # and append them to the running messages list.
                messages = self._build_tool_loop_messages(
                    messages, raw, iter_tool_results, model
                )

            # ── End of multi-turn loop ────────────────────────────
            latency_ms = int((time.time() - start_total) * 1000)

            # Write captures if enabled (dedupe across all iterations already done
            # by extract_all_captures, which uses a seen-set per call. But we
            # accumulated across iterations so need to dedupe manually.)
            written_captures: list[Capture] = []
            seen_capture_keys: set[tuple] = set()
            if write_captures:
                policy = WritePolicy(
                    write_paths=self.config.write_paths,
                    read_only_paths=self.config.read_only_paths,
                )
                for c in all_captures:
                    key = (c.type, c.name, hash(c.body))
                    if key in seen_capture_keys:
                        continue
                    seen_capture_keys.add(key)
                    try:
                        self.memory.write_note(c, policy)
                        written_captures.append(c)
                    except Exception as e:
                        self._log(
                            {
                                "trigger": "capture_write_error",
                                "parent_run_id": self.run_id,
                                "model": "n/a",
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "status": "error",
                                "summary": f"capture write failed for {c.name}: {e}",
                            }
                        )

            # Build response
            response = Response(
                text=last_raw.text if last_raw else "",
                model=model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cache_hit_tokens=total_cache_hit_tokens,
                cache_miss_tokens=total_cache_miss_tokens,
                cost_usd=total_cost,
                cost_estimated_via_fallback=cost_fallback,
                latency_ms=latency_ms,
                summary=self._derive_summary(work_item),
                raw=last_raw.raw or {} if last_raw else {},
                captures=written_captures,
                tool_calls=all_tool_call_results,
                tool_iterations=iteration_count,
                tool_iterations_maxed=tool_iterations_maxed,
                deferred=bool(accumulated_escalation_queue_ids),
                escalation_queue_ids=accumulated_escalation_queue_ids,
                # continuity_persisted is True only when continuity was not
                # requested (conversation_id is None) OR a backend exists to
                # persist into. When a conversation_id IS supplied but no backend
                # is configured, NOTHING is written back below — so True would
                # mislead the caller into thinking history was stored. Start from
                # the honest value; the write-back block flips it to False on a
                # persistence failure when a backend DOES exist.
                continuity_persisted=(
                    conversation_id is None or _conv_backend is not None
                ),
            )

            # Log run record
            log_record: dict = {
                "trigger": self.trigger,
                "model": model,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "cache_hit_tokens": total_cache_hit_tokens,
                "cache_miss_tokens": total_cache_miss_tokens,
                "cost_usd": total_cost,
                "cost_source": "actor",
                "latency_ms": latency_ms,
                "status": "ok",
                "summary": response.summary,
                "run_id": self.run_id,
                "agent_mode": self.agent_mode,
            }
            if check.action == "fallback":
                log_record["fallback"] = True
            if cost_fallback:
                log_record["cost_estimated_via_fallback"] = True
            if critical:
                log_record["critical"] = True
            if all_parse_failures:
                log_record["capture_parse_failures"] = len(all_parse_failures)
            # Identity-in-audit-trail: write the perimeter-asserted (unverified-by-
            # framework) caller identity into the permanent run record. The framework
            # reads the raw identity header value and passes it through — it MUST NOT
            # verify or decode it (spec/37 MUST 6). The field name ``http_caller`` is
            # canonical per spec/37 §"Audit record shape". Home-user callers pass
            # None (field omitted); org HTTP callers pass the identity header value
            # extracted at the serve boundary. RunRecord.from_dict routes unknown
            # keys to extra{} so this field survives round-trips without a schema
            # change to RunRecord. spec/37 MUST 7.
            if caller_identity is not None:
                log_record["http_caller"] = caller_identity
            # Round 1 Finding 1 fix: tag the agent_call cost event with
            # mandate_id + proposal_id when at least one mandate cite
            # committed this call. _sum_prior_token_cost queries by
            # cost_source=actor + mandate_id; without this tag, cumulative
            # token-cap defense is unenforceable. Round 2 R2-2: extracted
            # to a pure helper so the test suite pins the contract without
            # re-implementing the logic inline.
            from .judge.mandate_reservations import (
                build_mandate_log_record_extras,
            )

            log_record.update(
                build_mandate_log_record_extras(
                    self._successful_mandate_cites_this_call
                )
            )
            if self._helpers_this_run:
                # Spec/13 Layer 3 — research log: roll up helper provenance
                # into the parent run record so an audit can trace every fact
                # back to the helper invocation that produced it.
                log_record["helper_provenance"] = list(self._helpers_this_run)
            if self._delegations_this_run:
                log_record["delegations"] = list(self._delegations_this_run)
            if all_tool_call_results:
                log_record["tool_calls"] = [
                    {
                        "tool_name": r.tool_name,
                        "tool_use_id": r.tool_use_id,
                        "latency_ms": r.latency_ms,
                        "error": r.error,
                    }
                    for r in all_tool_call_results
                ]
            if iteration_count > 1:
                log_record["tool_iterations"] = iteration_count
            if tool_iterations_maxed:
                log_record["tool_iterations_maxed"] = True
            # spec/45 PR2: tag idempotency_key on every keyed ok-path run record
            # (spec/22 addendum: MUST be recorded on every keyed run — ok, deduped,
            # in_flight). replayed_run_id is absent on ok records (no prior result).
            if idempotency_key is not None:
                log_record["idempotency_key"] = idempotency_key
            # spec/47 PR1: tag conversation_id on every keyed ok-path run record
            # (spec/22 versioned normative addendum: MUST be recorded on every run
            # that sets conversation_id so LogQuery.conversation_id filtering works).
            if conversation_id is not None:
                log_record["conversation_id"] = conversation_id
            # spec/48 PR1: tag principal_id on ok-path records when a non-local
            # principal is used, so audit answers "which principal ran this turn?"
            # Omit for LOCAL_PRINCIPAL to preserve backward compatibility with
            # existing log-parsing tools that don't expect the field on home-user runs.
            if principal is not LOCAL_PRINCIPAL:
                log_record["principal_id"] = principal.identifier
            # spec/45 PR2 / spec/22 addendum: JSONL audit record FIRST (durable),
            # THEN commit() the ledger terminal. A crash between the two leaves the
            # JSONL intact and the ledger uncommitted — the next delivery re-runs
            # (safe at-least-once direction). A crash between commit() and _log()
            # would leave the ledger COMPLETED but no JSONL record — invisible run
            # violates Principle 5 (audit trail is structural). Write order is load-
            # bearing; changing it is a P0.
            self._log(log_record)
            # spec/47 PR1: write turn pair back AFTER _log() (JSONL-first principle).
            # Write user turn first, then assistant turn. On failure in either:
            # - set response.continuity_persisted = False
            # - log WARNING with run_id (so the operator can locate the orphaned turn)
            # - return the billed Response unchanged (the LLM work succeeded)
            # NOTE: a crash between user-turn write and assistant-turn write leaves an
            # orphaned user turn. The orphan is identified by run_id in the filename.
            # Manual recovery: delete the dangling file. A two-turn atomic path ships
            # in a future PR (spec/47 §"PR1 crash boundary"). See prep finding P0-6.
            # NOTE (#553): this write-back also runs on deferred/escalated runs,
            # so a deferred run persists its turn pair + reports
            # continuity_persisted=True while the idempotency layer treats the run
            # as not-completed. Self-heals on retry (read-path normalization
            # collapses the duplicate user turn + drops the empty assistant turn),
            # so it is not a wrong-LLM-input bug; tracked for the gate fix.
            if conversation_id is not None and _conv_backend is not None:
                from .conversation import (  # noqa: PLC0415 — lazy import (circular-safety)
                    ConversationBackendError as _ConvBackendError2,
                    Turn as _Turn,
                )

                # datetime/timezone are imported at module scope (no circular-import
                # risk for stdlib) — use them directly, no shadow re-import.
                _now_utc = datetime.now(timezone.utc).isoformat()
                # seq disambiguates the two same-call turns (same run_id AND ts):
                # user=0, assistant=1. Without it the assistant file overwrites the
                # user file and every user turn is silently lost (spec/47 §"Turn").
                _user_turn = _Turn(
                    role="user",
                    content=work_item,
                    ts=_now_utc,
                    run_id=self.run_id,
                    seq=0,
                )
                _assistant_content = response.text if response.text else ""
                _assistant_turn = _Turn(
                    role="assistant",
                    content=_assistant_content,
                    ts=_now_utc,
                    run_id=self.run_id,
                    seq=1,
                )
                # Catch BOTH ConversationBackendError AND PathTraversalError on the
                # write-back: a bad conversation_id raises PathTraversalError (a
                # sibling, not a ConversationBackendError subclass). The contract is
                # "on failure still return the billed Response but set
                # continuity_persisted=False" — that must hold for EVERY failure
                # class, including path-validation. (lesson: catch the base/widest
                # class a use-site can raise.)
                _ConvWriteErrors = (_ConvBackendError2, PathTraversalError)
                try:
                    _conv_backend.write_turn(
                        principal,  # spec/48: use caller-supplied principal (not hardcoded LOCAL_PRINCIPAL)
                        conversation_id,
                        _user_turn,
                    )
                except _ConvWriteErrors as _write_exc:
                    response.continuity_persisted = False
                    _logger.warning(
                        "ConversationBackend write_turn(user) failed for "
                        "conversation_id=%r (run_id=%s): %s",
                        conversation_id,
                        self.run_id,
                        _write_exc,
                    )
                else:
                    # Only attempt assistant turn if user turn succeeded.
                    try:
                        _conv_backend.write_turn(
                            principal,  # spec/48: use caller-supplied principal
                            conversation_id,
                            _assistant_turn,
                        )
                    except _ConvWriteErrors as _write_exc2:
                        response.continuity_persisted = False
                        _logger.warning(
                            "ConversationBackend write_turn(assistant) failed for "
                            "conversation_id=%r (run_id=%s): %s",
                            conversation_id,
                            self.run_id,
                            _write_exc2,
                        )
            # commit() AFTER _log(). Wrap in try/except so a commit failure
            # logs a warning but does NOT fail the call (the LLM work is done;
            # the run completed; only the dedup ledger is unmarked).
            #
            # spec/45 commit-vs-release lifecycle: a DEFERRED (ESCALATE) run is
            # NOT a completed result — call() returned deferred=True with
            # escalation_queue_ids, signalling the run is paused for human/judge
            # review. Committing the idempotency key here would mark it COMPLETED,
            # so a retry would short-circuit to Response.deduped_response() (text='',
            # NO deferred flag, NO escalation_queue_ids) — silently dropping the
            # escalation signal. Instead, leave the lease unclaimed-for-commit so
            # the finally (release-on-failure) releases it and a retry re-runs and
            # re-surfaces the escalation. _idempotency_lease_held stays True here
            # (we do NOT set it False), so the finally below releases the lease —
            # the same at-least-once direction as the failure path.
            _response_deferred = bool(getattr(response, "deferred", False))
            if (
                idempotency_key is not None
                and _idempotency_lease_held
                and not _response_deferred
            ):
                try:
                    self.idempotency_backend.commit(
                        idempotency_key, result_ref=self.run_id
                    )
                    _idempotency_lease_held = False  # commit() unlinks the lease
                except Exception:  # noqa: BLE001 — intentionally broad: the LLM
                    # run already succeeded; a commit() failure must NOT fail the
                    # call. (IdempotencyBackendError is an Exception subclass, so a
                    # single broad catch covers it — no need for a redundant tuple.)
                    _logger.error(
                        "idempotency commit() failed after successful run — "
                        "lease will be released by finally; key may be re-runnable",
                        exc_info=True,
                    )

            # spec/39 MUST 1: sync the span accumulators from the final loop
            # accumulators on the success path so the body finally records the
            # true cost / token / iteration totals and derives OUTCOME_OK from
            # the returned Response.
            _call_total_cost = total_cost
            _call_input_tokens = total_input_tokens
            _call_output_tokens = total_output_tokens
            _call_tool_iterations = iteration_count
            _call_model = model
            _call_response = response
            return _call_response

        except BaseException as _call_exc:
            # spec/39 MUST 3 / MUST 8: any exception propagating out of the body
            # marks the span error (top outcome precedence) and ends it. The
            # accumulators carry whatever partial spend was synced before the
            # raise (0.0 if it fired before the loop). Re-raise unchanged — the
            # finalizer never swallows the real exception.
            #
            # Principle #5 (audit trail is structural): the spawn-gate refusal
            # (MCPCommandNotAllowed, spec/36 MUST 12) fires BEFORE the success-
            # path self._log(log_record) at the end of the body, so without an
            # explicit record here a blocked-command run would leave an OTel
            # error span but NO JSONL line with a run_id. A security block is
            # exactly the event that most needs an audit trail. Mirror the
            # refused-path record shape used by the lock_busy (3535) and
            # cost-skip (3632) early-returns so the audit stream stays uniform.
            # Scoped narrowly to MCPCommandNotAllowed — generic in-loop crashes
            # already write their own records and re-emitting here would double-
            # log them.
            if isinstance(_call_exc, MCPCommandNotAllowed):
                _security_abort_record: dict = {
                    "trigger": self.trigger,
                    "model": self.config.default_model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "status": "error",
                    "error": type(_call_exc).__name__,
                    "error_detail": str(_call_exc),
                    "summary": f"MCP spawn gate refused: {_call_exc}",
                }
                if caller_identity is not None:
                    _security_abort_record["http_caller"] = caller_identity
                # spec/47 PR1 / spec/22 addendum: tag conversation_id on the
                # security-abort terminal record too. A keyed conversation refused
                # at the MCP spawn gate must still surface under
                # LogQuery(conversation_id=...) — audit completeness (Principle #5).
                if conversation_id is not None:
                    _security_abort_record["conversation_id"] = conversation_id
                # Best-effort: an audit-write failure must not mask the original
                # security refusal (the refusal is the load-bearing signal).
                try:
                    self._log(_security_abort_record)
                except Exception:  # noqa: BLE001 — never shadow the refusal
                    # Agent name intentionally omitted from this fallback warning:
                    # the scanner's clear-text-logging heuristic taints self.name
                    # inside this spawn-gate handler, and the name is already on
                    # every other log line + the audit record for this run.
                    _logger.warning(
                        "failed to write spawn-gate refusal audit record "
                        "(the refusal still propagates)",
                        exc_info=True,
                    )
            _finalize_call_span(error=_call_exc)
            raise

        finally:
            # spec/39 MUST 3: the SINGLE owner of span end + context detach for
            # every exit path that reaches this body (normal return, the
            # cost-skip / mid-loop early returns, and exceptions via the except
            # above). Idempotent — a no-op if the except clause already
            # finalized. Runs FIRST so the span is closed before lock release.
            _finalize_call_span()
            # Tear down MCP pool after each call so subprocesses don't linger.
            # disconnect_all() is idempotent — safe to call even if connect_all()
            # was never reached (e.g. if an exception occurred before it).
            if self.mcp_pool is not None:
                self.mcp_pool.disconnect_all()
                self.mcp_pool = None
            # Unregister MCP tools that were registered for this call (spec/19 fix M3).
            # Prevents stale tools from accumulating in the long-lived tool_registry
            # when a later call's server fails to reconnect.
            for _mcp_name in _mcp_registered_names:
                self.tool_registry.unregister(_mcp_name)
            # Clear Policy snapshot at call exit — None outside of call().
            self._policy_snapshot_this_call = None
            # spec/45 PR2: best-effort lease release on any non-success exit path.
            # _idempotency_lease_held is False when:
            #   - begin() was never called (no idempotency_key, or cost-skipped)
            #   - commit() succeeded (it unlinks the lease and sets flag False)
            # Only True when we own an IN_FLIGHT lease that was NOT committed.
            # Wrap in try/except so an I/O error from release_lease() cannot
            # propagate from finally and prevent the lock release below (Principle 8).
            if idempotency_key is not None and _idempotency_lease_held:
                try:
                    self.idempotency_backend.release_lease(idempotency_key)
                except Exception:  # noqa: BLE001 — best-effort, never propagate
                    _logger.warning(
                        "idempotency release_lease() failed in finally for key=%r "
                        "(best-effort). No automatic sweep exists yet "
                        "(supports_ttl=False) — if the key stays wedged IN_FLIGHT, "
                        "an operator must clear its lease file manually or a later "
                        "release_lease attempt must succeed.",
                        idempotency_key,
                        exc_info=True,
                    )
            self.lock_backend.release(lock_handle)

    def _build_tool_loop_messages(
        self,
        prior_messages: list[dict],
        raw: Any,
        tool_results: list[ToolCallResult],
        model: str,
    ) -> list[dict]:
        """Build the updated messages list for the next iteration of the tool loop.

        Appends the assistant's response (with tool_use blocks) and the
        tool_result messages so the LLM can incorporate results in its
        next turn.

        Post-#87 PR 3: every supported provider routes through the
        registry. The backend's ``format_tool_results`` translates
        canonical types to whatever message shape that provider's API
        requires (Anthropic gets an assistant message with tool_use
        blocks + a user message with tool_result blocks; OpenAI/Moonshot
        get an assistant message with ``tool_calls`` + N tool-role
        messages). The agent layer no longer branches by provider.
        """
        from .llm import find_backend_for_model

        messages = list(prior_messages)
        canonical_tool_uses, canonical_tool_results = _canonicalize_tool_loop(
            raw.tool_uses,
            tool_results,
        )
        # Thread the agent's ``model.md provider:`` preference (#87 PR 3)
        # so a multi-iteration tool loop on an ambiguously-claimed model
        # id resolves consistently across all iterations. Without this,
        # iteration 1's call resolves correctly via call_llm but
        # iteration 2's format_tool_results crashes mid-loop with
        # AmbiguousBackendError. Bug caught by Opus subagent review of
        # this PR (Finding 1); regression test in test_codex_r2_agent.py.
        backend = find_backend_for_model(model, preferred_provider=self.config.provider)
        messages.extend(
            backend.format_tool_results(
                tool_uses=canonical_tool_uses,
                tool_results=canonical_tool_results,
                assistant_text=raw.text or "",
            )
        )
        return messages

    # ────────────────────────────────────────────────────────────
    # Helpers (Patterns A + B per spec/10)

    HELPER_PROVENANCE_PROMPT = (
        "When summarizing or extracting facts from a source document, cite the "
        "location (section, page, or paragraph) of each fact. If you can't "
        "pinpoint a location, say so explicitly. Do not return facts without "
        "provenance — the calling agent depends on traceability for citation in "
        "its response."
    )

    def helper_call(
        self,
        prompt: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        summary: str = "",
        sources: list[str] | None = None,
    ) -> HelperResult:
        """One sequential helper call. Bound by parent's cost guardrails.

        When ``sources`` is passed (per spec/10 Wave 8 helper provenance),
        the helper system prompt includes citation instructions and the
        source list. The result echoes ``sources`` and sets
        ``provenance_preserved=False`` if the output appears to lack
        citation-like markers, so the parent can decide whether to trust
        the helper output as citable facts or treat it as uncited prose.

        Returns HelperResult with text + cost + token counts + provenance.
        """
        check = self._check_cost_guardrails(critical=False)
        if not check.allow:
            raise CostGuardrailBlocked(f"Helper call blocked: {check.reason}")

        actual_model = check.fallback_model if check.action == "fallback" else model
        sources_list = list(sources) if sources else []
        system_prompt = self._build_helper_system_prompt(sources_list)

        start = time.time()
        raw = _llm.call_llm(
            model=actual_model,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            preferred_provider=self.config.provider,
        )
        latency_ms = int((time.time() - start) * 1000)
        cost, _cost_fallback = _costs.calc_cost(
            actual_model, raw.input_tokens, raw.output_tokens
        )

        provenance_preserved = self._detect_provenance(raw.text, sources_list)

        log_record: dict = {
            "trigger": "helper",
            "parent_agent": self.name,
            "parent_run_id": self.run_id,
            "model": actual_model,
            "input_tokens": raw.input_tokens,
            "output_tokens": raw.output_tokens,
            "cost_usd": cost,
            "cost_source": "actor",
            "latency_ms": latency_ms,
            "status": "ok",
            "summary": summary or "helper call",
        }
        if sources_list:
            log_record["sources"] = sources_list
            log_record["provenance_preserved"] = provenance_preserved
        self._log(log_record)

        # Append to the in-memory rollup for spec/13 Layer 3 (research log).
        # The parent run's log record will include this list at end-of-call.
        rollup_entry = {
            "model": actual_model,
            "summary": summary or "helper call",
            "cost_usd": cost,
            "latency_ms": latency_ms,
        }
        if sources_list:
            rollup_entry["sources_summarized"] = sources_list
            rollup_entry["provenance_preserved"] = provenance_preserved
        self._helpers_this_run.append(rollup_entry)

        return HelperResult(
            text=raw.text,
            model=actual_model,
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            sources=sources_list,
            provenance_preserved=provenance_preserved,
        )

    def helper_call_parallel(
        self,
        prompts: list[str],
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        max_concurrent: int = 5,
        summary_template: str = "helper call {idx} of {total}",
        sources_per_prompt: list[list[str]] | None = None,
        sources: list[str] | None = None,
    ) -> list[HelperResult]:
        """Parallel helper calls. Pre-checks guardrails ONCE; if cap hit, refuses the batch.

        Provenance options (mutually exclusive — passing both raises ValueError):

        - ``sources``: same source list applied to every prompt (e.g., one
          source document being analyzed N different ways).
        - ``sources_per_prompt``: list of source lists, aligned 1:1 with
          ``prompts`` (e.g., each prompt is a different document).

        Either way, each result's ``sources`` and ``provenance_preserved``
        fields are populated as in ``helper_call``.
        """
        if sources is not None and sources_per_prompt is not None:
            raise ValueError(
                "pass either `sources` (shared) or `sources_per_prompt` (per-prompt), not both"
            )
        if sources_per_prompt is not None and len(sources_per_prompt) != len(prompts):
            raise ValueError(
                f"sources_per_prompt has {len(sources_per_prompt)} entries; "
                f"expected {len(prompts)} (one per prompt)"
            )

        check = self._check_cost_guardrails(critical=False)
        if not check.allow:
            raise CostGuardrailBlocked(f"Parallel helper batch blocked: {check.reason}")

        # Worst-case reservation: check that the parent's remaining headroom can
        # cover all helpers at max_tokens output each. This prevents the "each
        # thread sees the same pre-batch snapshot" race where collective cost
        # overruns the cap even though no individual thread sees a breach.
        actual_model = check.fallback_model if check.action == "fallback" else model
        reserved_usd = self._estimate_batch_cost(actual_model, max_tokens, len(prompts))
        self._check_batch_reservation(reserved_usd)

        total = len(prompts)
        results: list[Any] = [None] * total  # list[HelperResult | Exception]

        # Log the reservation so an audit trail can see what was reserved.
        self._log(
            {
                "trigger": "helper_batch_reservation",
                "parent_agent": self.name,
                "parent_run_id": self.run_id,
                "model": actual_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "reserved_usd": reserved_usd,
                "batch_size": total,
                "status": "ok",
                "summary": f"reserved worst-case ${reserved_usd:.6f} for {total}-helper batch",
            }
        )

        def sources_for(idx: int) -> list[str] | None:
            if sources_per_prompt is not None:
                return sources_per_prompt[idx]
            return sources

        def call_one(idx: int, prompt: str):
            return idx, self.helper_call(
                prompt=prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                summary=summary_template.format(idx=idx + 1, total=total),
                sources=sources_for(idx),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = {pool.submit(call_one, i, p): i for i, p in enumerate(prompts)}
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, helper_result = future.result()
                    results[idx] = helper_result
                except Exception as e:
                    idx = futures[future]
                    results[idx] = e

        failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
        if failures:
            raise HelperBatchPartialFailure(failures, results)

        # Log the release: actual aggregate cost vs what was reserved.
        actual_usd = sum(r.cost_usd for r in results if isinstance(r, HelperResult))
        self._log(
            {
                "trigger": "helper_batch_release",
                "parent_agent": self.name,
                "parent_run_id": self.run_id,
                "model": actual_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "reserved_usd": reserved_usd,
                "actual_usd": actual_usd,
                "batch_size": total,
                "status": "ok",
                "summary": (
                    f"batch complete: actual ${actual_usd:.6f} vs "
                    f"reserved ${reserved_usd:.6f}"
                ),
            }
        )

        return results  # type: ignore

    # ────────────────────────────────────────────────────────────
    # Delegation (runtime agent-to-agent, per spec/15)

    def _resolve_delegated_agent_path(self, target_name: str) -> Path:
        """Resolve the filesystem path for a target agent.

        In a cascaded layout (<system>/projects/<project>/agents/<role>/),
        the target resolves as a peer under the same project:
            <system>/projects/<project>/agents/<target>/

        In a single-agent layout (<agents_root>/<role>/), the target resolves
        as a top-level sibling:
            <agents_root>/<target>/

        Raises NotInRoster (mapped from PathTraversalError) if target_name
        contains path-traversal sequences (e.g. ``../other``) that would
        resolve outside the agents root.
        """
        if self.cascade:
            agents_dir = self.cascade.instance_root.parent
        else:
            agents_dir = self.agents_root
        try:
            return safe_resolve_under(target_name, agents_dir)
        except PathTraversalError as exc:
            raise NotInRoster(
                f"target '{target_name}' resolves outside agents root "
                f"({agents_dir}) — path traversal refused"
            ) from exc

    def _enforce_roster_membership(self, target: str) -> None:
        """Raise NotInRoster if target is not in this coordinator's roster."""
        if target not in self.config.roster:
            raise NotInRoster(
                f"target '{target}' not in coordinator's roster: {self.config.roster}"
            )

    def delegate(
        self,
        target_agent_name: str,
        work_item: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        critical: bool = False,
        summary: str = "",
    ) -> Response:
        """Synchronously dispatch a work item to another agent in the roster.

        Loads <target_agent_name> as a fresh AtomicAgent instance with its own
        persona, memory, wiki, journal, and config. Calls it with the work_item.
        Returns its Response.

        The coordinator's cost guardrails apply to the total call tree — a
        pre-check runs before the delegate call, and the delegation is refused
        if the cap is hit (unless critical=True).

        Each delegate call also writes a JSONL log line with trigger=delegate,
        parent_agent, delegated_agent, parent_run_id, and delegated_run_id.
        The Response captures (if any) are written to the target's memory, not
        the coordinator's.

        Raises:
            NotInRoster: target_agent_name is not in self.config.roster.
            SelfDelegationError: target_agent_name == self.name (one-level only).
            NestedDelegationRefused: self.trigger == 'delegate' (nested delegation
                forbidden — spec/15 enforces one-level only).
            CostGuardrailBlocked: parent's cost cap is hit and critical=False.
        """
        # Nested delegation guard — spec/15 one-level limit. (fix R2-A3)
        if self.trigger == "delegate":
            raise NestedDelegationRefused(
                f"agent '{self.name}' is already running as a delegated agent "
                f"(trigger='delegate') and cannot delegate further — "
                f"nested delegation refused per spec/15 (one-level only)"
            )

        self._enforce_roster_membership(target_agent_name)
        if target_agent_name == self.name:
            raise SelfDelegationError(
                f"agent '{self.name}' cannot delegate to itself — one-level delegation only"
            )

        # Pass prior delegated cost as extra_in_flight so the guardrail sees
        # tree-spend that landed in the target's log dir, not the coordinator's.
        # (fix R2-A2)
        check = self._check_cost_guardrails(
            critical=critical,
            extra_in_flight_cost_usd=self._delegated_cost_this_run,
        )
        if not check.allow:
            raise CostGuardrailBlocked(
                f"Delegation to '{target_agent_name}' blocked: {check.reason}"
            )

        # Compute coordinator's remaining headroom to pass to the delegate.
        # This enforces the coordinator cap as a true tree-cap. (fix R2-A2)
        # Include already-delegated spend so the headroom accounts for it.
        #
        # Fail-closed when blind: if either cost read is degraded (OSError /
        # majority corruption), the coordinator cannot verify its own spend.
        # Giving a delegate headroom in that state would silently over-grant
        # budget → fail-closed by raising CostGuardrailBlocked with zero headroom.
        remaining_headroom: float | None = None
        if self.config.cost_guardrails_enabled and not critical:
            log_dir = self.agent_root / "log"
            today_result = _costs.sum_cost_for_period(
                log_dir,
                "today",
                source="actor",
                backend=self.log_backend,
                agent_name=self.name,
            )
            month_result = _costs.sum_cost_for_period(
                log_dir,
                "this_month",
                source="actor",
                backend=self.log_backend,
                agent_name=self.name,
            )
            # Fail-closed on a degraded read ONLY when this coordinator has a cap
            # to pass down as a tree-cap. An uncapped coordinator grants unbounded
            # headroom (remaining_headroom stays None) regardless of the read, so a
            # degraded read changes nothing — blocking would be a spurious refusal.
            # Predicate is intentionally model.md-only (no Policy caps): the
            # headroom passed to the delegate is computed from self.config caps
            # only, so the uncapped-skip must match that surface. Do NOT copy
            # _check_cost_guardrails's wider Policy+parent-tree-cap predicate here.
            _delegation_capped = (
                self.config.daily_cap_usd > 0 or self.config.monthly_cap_usd > 0
            )
            if _delegation_capped and (today_result.degraded or month_result.degraded):
                raise CostGuardrailBlocked(
                    "delegation blocked: cost data degraded — "
                    "cannot compute safe headroom (fail-closed)"
                )
            today_cost = today_result.total_usd + self._delegated_cost_this_run
            month_cost = month_result.total_usd + self._delegated_cost_this_run
            daily_remaining = (
                self.config.daily_cap_usd - today_cost
                if self.config.daily_cap_usd > 0
                else float("inf")
            )
            monthly_remaining = (
                self.config.monthly_cap_usd - month_cost
                if self.config.monthly_cap_usd > 0
                else float("inf")
            )
            headroom = min(daily_remaining, monthly_remaining)
            if headroom < float("inf"):
                remaining_headroom = headroom

        target_path = self._resolve_delegated_agent_path(target_agent_name)
        # Build the target agent. It inherits no agent-state (run history,
        # captures) from the coordinator. ``profile_backend`` IS threaded
        # because the profile backend is fleet-scoped (one backend per
        # ``agents_root``) — an operator who pinned
        # ``DatabaseAgentProfileBackend`` on the coordinator wants every
        # delegated agent to load its config from the same DB, not
        # silently fall back to filesystem. Step 11 adversarial
        # Finding 1 from #63 PR 2 caught the drop-trap here as the
        # exact recurrence of the runner-drop-trap shape on the
        # production multi-agent delegation path.
        #
        # ``policy_backend`` IS threaded (#89 PR 2 + spec/32 D1). Policy
        # is fleet-scoped (one policy.md applies to ALL agents in the
        # project), not per-agent — same shape as ``profile_backend``.
        # An operator who pinned a custom ``PolicyBackend`` (Postgres,
        # SaaS, org-admin-console) on the coordinator wants every
        # delegated agent to evaluate actions against THE SAME policy
        # store, not silently fall back to filesystem. Without threading,
        # an operator's custom PolicyBackend is bypassed entirely for all
        # delegated work — defeating the core purpose of Policy as a
        # fleet-level authority surface.
        #
        # ``lock_backend`` and ``log_backend`` are NOT threaded — they
        # are per-agent scoped (filesystem lock at ``<agent>/.lock``,
        # log at ``<agent>/log/``). Threading them would put the
        # target's locks/logs in the COORDINATOR's directory, mixing
        # the two agents' on-disk artifacts. The pre-PR-2 convention
        # was "don't thread per-agent backends through delegation";
        # PR 2 preserves that. Operators who want shared lock / log
        # backends across delegated agents can set the
        # ``ATOMIC_AGENTS_LOCK_BACKEND`` / ``ATOMIC_AGENTS_LOG_BACKEND``
        # env vars at the deployment level — both target and
        # coordinator then pick up the same operator-pinned backend
        # via the default factory.
        #
        # ``tool_registry_backend`` is ALSO not threaded (#64 PR 2) —
        # same reason. The filesystem reference is per-agent-rooted
        # (``tools/<name>.md`` belongs to ONE agent's directory).
        # Threading the coordinator's instance to the target would
        # surface the COORDINATOR's tools in the target's catalog —
        # not the operator's intent. Operators wanting a shared tool
        # catalog across agents set ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND``
        # at the deployment level (when the PR 3 SQLite / future
        # PyPI / HTTP backends land — those ARE shared-catalog by
        # nature). Filesystem-default deployments behave correctly:
        # the target builds its own ``FilesystemToolRegistryBackend``
        # over ``<target_path>/tools/`` via the default factory.
        #
        # ``mandate_backend`` is ALSO not threaded (#124 PR 2 + spec/29).
        # Mandates are per-agent scoped — a coordinator's mandates.md
        # grants authority to THAT coordinator only. Threading the
        # coordinator's mandate backend to the target would allow the
        # target to validate actions against the coordinator's authority
        # grants, not its own — a security boundary violation.
        # Per spec/29 §"Per-agent vs project-root resolution", the target
        # builds its own ``FilesystemMandateBackend`` over its own
        # ``<target_path>/mandates.md`` via the default factory. Operators
        # needing fleet-level mandate policy use project-root mandates
        # (``project:<name>`` scope), not cross-agent backend threading.
        # Build delegate kwargs. Fleet-scoped backends (profile_backend,
        # policy_backend) are ALWAYS threaded — a delegate in a different
        # agent dir still evaluates policy and reads profiles against the
        # SAME store the coordinator uses (spec/32 D1 for Policy; spec/24
        # for AgentProfile). Per-agent scoped backends (lock, log, tool
        # registry, mandate) are NOT threaded — each delegate resolves its
        # own from its own root.
        #
        # persona_backend follows D-ER-2 (#62 PR 2): threaded ONLY when the
        # operator supplied it explicitly via kwarg. Default-resolved backends
        # use the coordinator's agents_root as the personas scope; threading
        # them to a cross-vault delegate would silently resolve the wrong
        # .personas/ directory. When NOT threaded, the delegate constructs
        # its own default at ITS scope (own agents_root).
        _delegate_kwargs: dict = {
            "name": target_agent_name,
            "trigger": "delegate",
            "agents_root": target_path.parent,
            "run_id": None,  # generates its own fresh run_id
            "profile_backend": self.profile_backend,
            "policy_backend": self.policy_backend,
        }
        if self._persona_backend_was_explicit:
            _delegate_kwargs["persona_backend"] = self.persona_backend
        if self._corpus_backend_was_explicit:
            _delegate_kwargs["corpus_backend"] = self.corpus_backend
        if self._mcp_server_registry_backend_was_explicit:
            _delegate_kwargs["mcp_server_registry_backend"] = (
                self.mcp_server_registry_backend
            )
        # Memory is per-agent STATE (each delegate has its own memory/ dir),
        # so the coordinator's backend is NEVER threaded to the child — not
        # even when memory_backend= was supplied explicitly. The child
        # resolves its own per-agent backend via the same process/deployment-
        # global ATOMIC_AGENTS_MEMORY_BACKEND selection. Threading a root-bound
        # FilesystemBackend here would silently route the specialist's writes
        # into the COORDINATOR's memory/ dir (cross-agent corruption). This is
        # the deliberate divergence from the persona/corpus mirror: those are
        # fleet-shared CONFIG where sharing is correct; memory is per-agent
        # STATE where it is not. See spec/20 §"Delegate threading" and ruling
        # delegate-child-threading (#382). Shared-memory delegation, if ever
        # wanted, is a new Tier A fork — not a corner to decide here.
        target_agent = AtomicAgent(**_delegate_kwargs)

        start = time.time()
        response = target_agent.call(
            work_item=work_item,
            max_tokens=max_tokens,
            temperature=temperature if temperature is not None else None,
            critical=critical,
            parent_remaining_headroom_usd=remaining_headroom,
        )
        latency_ms = int((time.time() - start) * 1000)

        # Add delegated cost to coordinator's accumulator so subsequent
        # delegate() calls see the true tree-spend. (fix R2-A2)
        self._delegated_cost_this_run += response.cost_usd

        # Log the delegation in the COORDINATOR's log
        log_record: dict = {
            "trigger": "delegate",
            "parent_agent": self.name,
            "delegated_agent": target_agent_name,
            "parent_run_id": self.run_id,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": response.cost_usd,
            "cost_source": "actor",
            "latency_ms": latency_ms,
            "status": "ok" if not response.skipped else "skipped",
            "summary": summary or f"delegate to {target_agent_name}",
            "delegate_run_id": target_agent.run_id,
        }
        if critical:
            log_record["critical"] = True
        self._log(log_record)

        # Append to per-run delegation rollup
        rollup_entry = {
            "target": target_agent_name,
            "summary": summary or f"delegate to {target_agent_name}",
            "cost_usd": response.cost_usd,
            "latency_ms": latency_ms,
            "delegated_run_id": target_agent.run_id,
            "captures_count": len(response.captures),
        }
        self._delegations_this_run.append(rollup_entry)

        return response

    def delegate_parallel(
        self,
        calls: list[tuple[str, str]],
        max_concurrent: int = 5,
        max_tokens: int | None = None,
        temperature: float | None = None,
        summary_template: str = "delegate {idx} of {total}: {target}",
    ) -> list[Response]:
        """Parallel fan-out to multiple agents.

        Pre-reserves worst-case batch cost against parent headroom (mirrors
        helper_call_parallel pattern). Concurrency capped at 25 (matching
        Anthropic's thread limit). Calls are executed in parallel via
        ThreadPoolExecutor with max_concurrent workers (default 5, hard cap 25).
        Returns Responses in the same order as `calls`.

        All same refusal conditions apply per-call (in-roster, no self).

        Args:
            calls: list of (target_agent_name, work_item) tuples.
            max_concurrent: thread pool size. Clamped to [1, 25].
            max_tokens: output token cap forwarded to each delegate call.
            temperature: temperature forwarded to each delegate call.
            summary_template: template for per-call summaries; supports
                {idx} (1-based), {total}, {target}.

        Raises:
            ValueError: max_concurrent is 0 or > 25.
            NotInRoster / SelfDelegationError: any call fails roster check.
            CostGuardrailBlocked: batch reservation exceeds headroom.
        """
        # Nested delegation guard — spec/15 one-level limit. (fix R2-A3)
        if self.trigger == "delegate":
            raise NestedDelegationRefused(
                f"agent '{self.name}' is already running as a delegated agent "
                f"(trigger='delegate') and cannot delegate further — "
                f"nested delegation refused per spec/15 (one-level only)"
            )

        if max_concurrent < 1 or max_concurrent > 25:
            raise ValueError(
                f"max_concurrent must be between 1 and 25 inclusive; got {max_concurrent}"
            )

        # Validate all targets before reserving cost or spawning threads
        for target, _ in calls:
            self._enforce_roster_membership(target)
            if target == self.name:
                raise SelfDelegationError(
                    f"agent '{self.name}' cannot delegate to itself — one-level delegation only"
                )

        check = self._check_cost_guardrails(critical=False)
        if not check.allow:
            raise CostGuardrailBlocked(
                f"Parallel delegation batch blocked: {check.reason}"
            )

        total = len(calls)

        # Worst-case reservation: each target's max_output_tokens × its output rate.
        # We use the coordinator's default model rate as a conservative proxy
        # (target models may differ, but we don't load targets just for pricing).
        reserved_usd = self._estimate_batch_cost(
            self.config.default_model,
            max_tokens or self.config.max_output_tokens,
            total,
        )
        self._check_batch_reservation(reserved_usd)

        self._log(
            {
                "trigger": "delegate_batch_reservation",
                "parent_agent": self.name,
                "parent_run_id": self.run_id,
                "model": self.config.default_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "reserved_usd": reserved_usd,
                "batch_size": total,
                "status": "ok",
                "summary": f"reserved worst-case ${reserved_usd:.6f} for {total}-delegate batch",
            }
        )

        results: list[Any] = [None] * total

        def call_one(idx: int, target: str, work_item: str):
            summ = summary_template.format(idx=idx + 1, total=total, target=target)
            return idx, self.delegate(
                target_agent_name=target,
                work_item=work_item,
                max_tokens=max_tokens,
                temperature=temperature,
                summary=summ,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = {
                pool.submit(call_one, i, target, work_item): i
                for i, (target, work_item) in enumerate(calls)
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, response = future.result()
                    results[idx] = response
                except Exception as e:
                    idx = futures[future]
                    results[idx] = e

        failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
        if failures:
            raise HelperBatchPartialFailure(failures, results)

        actual_usd = sum(r.cost_usd for r in results if isinstance(r, Response))
        self._log(
            {
                "trigger": "delegate_batch_release",
                "parent_agent": self.name,
                "parent_run_id": self.run_id,
                "model": self.config.default_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "reserved_usd": reserved_usd,
                "actual_usd": actual_usd,
                "batch_size": total,
                "status": "ok",
                "summary": (
                    f"delegate batch complete: actual ${actual_usd:.6f} vs "
                    f"reserved ${reserved_usd:.6f}"
                ),
            }
        )

        return results  # type: ignore

    def _estimate_batch_cost(
        self, model: str, max_tokens: int, batch_size: int
    ) -> float:
        """Compute a worst-case USD estimate for a helper batch.

        Uses max_tokens output per helper at the model's output rate. Input
        tokens are omitted from the estimate (conservative in the other direction,
        but output dominates for short prompts against a haiku-class model).

        Unknown models use the pessimistic fallback rate (highest known output
        rate across the PRICING table) so that unpriced models — self-hosted,
        Ollama, vLLM, new provider SKUs — are over-counted rather than silently
        treated as free, which would bypass the batch reservation guard entirely.
        Mirrors the pattern in dream.py:_estimate_dream_cost and _costs.calc_cost.
        """
        pricing = _costs.PRICING.get(model, _costs._fallback_pricing())
        output_rate = pricing["output"]
        return round(output_rate * max_tokens / 1_000_000 * batch_size, 6)

    def _check_batch_reservation(self, reserved_usd: float) -> None:
        """Raise CostGuardrailBlocked if the reservation exceeds remaining headroom.

        Remaining headroom is the lower of (daily_cap - today_cost) and
        (monthly_cap - month_cost). If cost_guardrails_enabled is False or
        the reservation is zero, the check is skipped.
        """
        if not self.config.cost_guardrails_enabled or reserved_usd <= 0:
            return
        log_dir = self.agent_root / "log"
        today_result = _costs.sum_cost_for_period(
            log_dir,
            "today",
            source="actor",
            backend=self.log_backend,
            agent_name=self.name,
        )
        month_result = _costs.sum_cost_for_period(
            log_dir,
            "this_month",
            source="actor",
            backend=self.log_backend,
            agent_name=self.name,
        )
        # Gate site: fail-closed on a degraded read ONLY when there is a cap to
        # enforce. An uncapped agent's headroom is inf (a reservation can never
        # exceed it), so a degraded read changes nothing — blocking it would be a
        # spurious refusal with no safety benefit.
        # Predicate is intentionally model.md-only (no Policy caps): this site's
        # headroom math below uses only self.config caps, so the uncapped-skip must
        # match that surface. _check_cost_guardrails is the only gate that resolves
        # Policy caps + parent tree-cap; do NOT copy its wider predicate here.
        _batch_capped = self.config.daily_cap_usd > 0 or self.config.monthly_cap_usd > 0
        if _batch_capped and (today_result.degraded or month_result.degraded):
            raise CostGuardrailBlocked(
                "cost data unreadable — batch reservation blocked (fail-closed)"
            )
        today_cost = today_result.total_usd
        month_cost = month_result.total_usd
        daily_remaining = (
            self.config.daily_cap_usd - today_cost
            if self.config.daily_cap_usd > 0
            else float("inf")
        )
        monthly_remaining = (
            self.config.monthly_cap_usd - month_cost
            if self.config.monthly_cap_usd > 0
            else float("inf")
        )
        headroom = min(daily_remaining, monthly_remaining)
        if reserved_usd > headroom:
            raise CostGuardrailBlocked(
                f"Parallel helper batch reservation ${reserved_usd:.6f} exceeds "
                f"remaining headroom ${headroom:.6f}"
            )

    def _build_helper_system_prompt(self, sources: list[str]) -> str:
        """Build the helper's system prompt. Empty when no sources are passed."""
        if not sources:
            return ""
        bullet_list = "\n".join(f"- {s}" for s in sources)
        return f"{self.HELPER_PROVENANCE_PROMPT}\n\nSources you are working from:\n{bullet_list}"

    @staticmethod
    def _detect_provenance(text: str, sources: list[str]) -> bool:
        """Heuristic: did the helper preserve attribution back to the sources?

        Returns True when no sources were passed (nothing to preserve) or when
        the output contains attribution-shaped signals — bracketed citations
        ([§2, p3], [section 4], [memo §1]), explicit attribution phrases
        ("according to", "per memo", "§3"), or a verbatim mention of any
        source's basename.

        **Deliberate trade-off — prefers false-positives over false-negatives.**
        The parent agent treats ``provenance_preserved=False`` as "not citable",
        which silently downgrades the quality of every fact in that helper
        output. A false-negative (missing real provenance loss) therefore has a
        much higher consequence than a false-positive (letting a borderline
        output through unchallenged). The heuristic is intentionally lenient:
        any attribution-shaped signal in the output counts, even if it's weak.

        **Consequences for operators:** if your use-case requires strict
        provenance verification, override ``HelperResult.provenance_preserved``
        to ``False`` after inspecting the output before passing facts downstream.

        This behaviour is specified in spec/10 Wave 8 (helper provenance) and
        was reviewed and retained intentionally — do not tighten the heuristic
        without updating that spec section and re-evaluating the false-negative
        rate on the existing eval corpus.
        """
        if not sources:
            return True
        if not text or not text.strip():
            return False

        # Bracketed-citation check: at least one [...] containing common citation
        # markers (section symbol, "p<digit>", "section", "page", "paragraph").
        bracket_pattern = re.compile(
            r"\[[^\]]*(?:§|sect|page|p\.|p\s*\d|para|para\.)[^\]]*\]",
            re.IGNORECASE,
        )
        if bracket_pattern.search(text):
            return True

        # Inline attribution phrases.
        inline_pattern = re.compile(
            r"(?:\baccording to\b|\bper\s+\w|\bcited in\b|§\s*\d|\(p\.?\s*\d)",
            re.IGNORECASE,
        )
        if inline_pattern.search(text):
            return True

        # Verbatim source basename mention (last path component, stem).
        for src in sources:
            stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem and len(stem) >= 3 and stem.lower() in text.lower():
                return True

        return False

    # ────────────────────────────────────────────────────────────
    # Cost guardrails

    @staticmethod
    def _min_or_other(a: float | None, b: float | None) -> float | None:
        """None-aware MIN for cost-cap composition (#89 PR 3a / spec/32 D2).

        ``None`` means "no opinion at this layer" — drops out of the MIN.
        Returns ``None`` only when BOTH inputs are ``None``.
        """
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)

    @staticmethod
    def _resolve_denying_layer(
        *,
        policy_value: float | None,
        model_md_value: float | None,
        effective: float | None,
    ) -> str:
        """Pick the layer whose cap equals ``effective`` (#89 PR 3a F14).

        Tiebreaker order (policy > model_md) — policy wins on a tie.
        Returns ``"policy"`` when Policy's value equals the effective cap;
        ``"model_md"`` otherwise (including when Policy has no opinion).

        Per-call and mandate layers are N/A in ``_check_cost_guardrails``
        and are omitted here; PR 3b adds them for tool/MCP surfaces.

        F4 fix (PR 3a Round 1): ``model_md_value`` is ``float | None`` —
        the helper handles None directly rather than relying on the call
        site to coerce None → 0.0 (which was a footgun if anyone touched
        the resolution logic and changed the tiebreak rules).
        """
        if effective is None:
            return "model_md"
        if policy_value is not None and policy_value == effective:
            return "policy"
        return "model_md"

    def _check_cost_guardrails(
        self,
        critical: bool = False,
        extra_in_flight_cost_usd: float = 0.0,
        parent_remaining_headroom_usd: float | None = None,
    ) -> CostCheckResult:
        """Run before each LLM call. Returns CostCheckResult.

        extra_in_flight_cost_usd: accumulated spend from the current tool loop
            that has not yet been persisted to the log file. Added to the
            disk-read total before cap comparison so mid-loop iterations see
            the true running spend. (fix R2-A1)

        parent_remaining_headroom_usd: when set by a delegating coordinator,
            this call's effective cap is clamped to min(own remaining, parent
            headroom), enforcing the coordinator's cap as a tree-cap. (fix R2-A2)
        """
        if not self.config.cost_guardrails_enabled:
            return CostCheckResult(allow=True)

        if critical:
            return CostCheckResult(allow=True, reason="critical_override")

        log_dir = self.agent_root / "log"
        today_result = _costs.sum_cost_for_period(
            log_dir,
            "today",
            source="actor",
            backend=self.log_backend,
            agent_name=self.name,
        )
        month_result = _costs.sum_cost_for_period(
            log_dir,
            "this_month",
            source="actor",
            backend=self.log_backend,
            agent_name=self.name,
        )

        # Degraded read (blind / majority-corrupt). The fail-closed ACTION is
        # deferred until after the effective caps are resolved: a degraded read
        # only matters when there is a budget to be blind about. An uncapped
        # (warnings-only) agent with no own caps and no parent tree-cap ALWAYS
        # proceeds even with perfect cost data, so failing it closed on a corrupt
        # log is a spurious block with zero safety benefit (it would never have
        # been refused). See the budget-constraint gate after cap resolution.
        is_degraded = today_result.degraded or month_result.degraded

        today_cost = today_result.total_usd + extra_in_flight_cost_usd
        month_cost = month_result.total_usd + extra_in_flight_cost_usd

        # ── Policy MIN composition (#89 PR 3a / spec/32 D2) ──────────────────
        # Resolve Policy caps from the per-call frozen snapshot taken at
        # call() entry (Premise 3 — snapshot is stable for the whole call).
        # ``None`` on either side means "no opinion at that layer"; _min_or_other
        # returns the opinionated one (or None when both are silent).
        from .policy.types import CostCaps as _CostCaps

        _policy_caps: _CostCaps = (
            self._policy_snapshot_this_call.effective_caps
            if self._policy_snapshot_this_call is not None
            else _CostCaps()
        )
        # model.md daily/monthly caps: 0 means "disabled" (unlimited).
        # Convert to None so _min_or_other treats disabled == no-opinion.
        _model_daily: float | None = (
            self.config.daily_cap_usd if self.config.daily_cap_usd > 0 else None
        )
        _model_monthly: float | None = (
            self.config.monthly_cap_usd if self.config.monthly_cap_usd > 0 else None
        )
        _effective_daily = self._min_or_other(_model_daily, _policy_caps.daily_usd)
        _effective_monthly = self._min_or_other(
            _model_monthly, _policy_caps.monthly_usd
        )
        # ─────────────────────────────────────────────────────────────────────

        # Degraded fail-closed gate (deferred from the read above): only fail
        # closed on a blind/majority-corrupt read when there is an actual budget
        # to enforce — an own cap (daily/monthly, model.md or Policy) OR a parent
        # coordinator tree-cap. A truly uncapped agent has no cap to bypass, so a
        # degraded read changes nothing; blocking it would be a spurious refusal
        # with no safety benefit. action="skip" (not a daily/monthly cap action):
        # allow=False drives the block and the specific action is meaningless when
        # the read is blind (degradation can stem from the monthly read too).
        if is_degraded and (
            _effective_daily is not None
            or _effective_monthly is not None
            or parent_remaining_headroom_usd is not None
        ):
            return CostCheckResult(
                allow=False,
                action="skip",
                reason="cost data unreadable — fail-closed",
                cost_data_degraded=True,
            )

        # F2 fix (PR 3a Round 1 P1): a Policy operator writing
        # `cost_caps: {daily_usd: 0}` intends "freeze this agent — no spend."
        # The old shape `(today_cost / _effective_daily) if _effective_daily else 0`
        # treated 0 as falsy → pct=0 → cap never fired → Policy's strictest
        # intent SILENTLY INVERTED to no-cap. New shape: cap is "fired" when
        # _effective_daily is not None AND today_cost >= _effective_daily.
        # (Same for monthly.)
        _daily_fired = _effective_daily is not None and today_cost >= _effective_daily
        _monthly_fired = (
            _effective_monthly is not None and month_cost >= _effective_monthly
        )

        # Warnings still use a percentage signal; for warnings, cap=0 behavior
        # is "always at 100%" which is the right alert posture too.
        daily_pct = (
            (today_cost / _effective_daily)
            if (_effective_daily is not None and _effective_daily > 0)
            else (1.0 if _daily_fired else 0)
        )
        monthly_pct = (
            (month_cost / _effective_monthly)
            if (_effective_monthly is not None and _effective_monthly > 0)
            else (1.0 if _monthly_fired else 0)
        )

        # Fire warnings (idempotent — won't fire twice for same threshold/day)
        self._maybe_fire_warning("daily", daily_pct)
        self._maybe_fire_warning("monthly", monthly_pct)

        if _daily_fired:
            # F3 fix (PR 3a Round 1 P1): compute _cap_action FIRST so the
            # policy_decision event records whether the action was actually
            # blocked. cap_action ∈ {"skip", "alert", "fallback"} — only "skip"
            # blocks (allow=False); "alert" + "fallback" let the call proceed.
            # Old shape always emitted enforced=True, lying when alert/fallback
            # were configured.
            result = self._cap_action(
                self.config.daily_cap_action,
                f"daily cap hit (${today_cost:.2f}/${_effective_daily:.2f})",
            )
            self._maybe_emit_cost_cap_policy_decision(
                dimension="daily",
                policy_value=_policy_caps.daily_usd,
                model_md_value=_model_daily,
                effective=_effective_daily,
                attempted_value=today_cost,
                enforced=(not result.allow),
            )
            return result
        if _monthly_fired:
            result = self._cap_action(
                self.config.monthly_cap_action,
                f"monthly cap hit (${month_cost:.2f}/${_effective_monthly:.2f})",
            )
            self._maybe_emit_cost_cap_policy_decision(
                dimension="monthly",
                policy_value=_policy_caps.monthly_usd,
                model_md_value=_model_monthly,
                effective=_effective_monthly,
                attempted_value=month_cost,
                enforced=(not result.allow),
            )
            return result

        # Parent headroom check: coordinator's remaining budget caps the delegate.
        # (fix R2-A2) — clamp to min(own remaining, parent headroom)
        if parent_remaining_headroom_usd is not None:
            daily_remaining = (
                _effective_daily - today_cost if _effective_daily else float("inf")
            )
            monthly_remaining = (
                _effective_monthly - month_cost if _effective_monthly else float("inf")
            )
            own_remaining = min(daily_remaining, monthly_remaining)
            effective_remaining = min(own_remaining, parent_remaining_headroom_usd)
            if effective_remaining <= 0:
                return CostCheckResult(
                    allow=False,
                    action="skip",
                    reason=(
                        f"parent coordinator headroom exhausted "
                        f"(${parent_remaining_headroom_usd:.6f} remaining)"
                    ),
                )

        # Allowed. Carry cost_data_degraded for audit honesty: an uncapped agent
        # proceeds even when the read was degraded (no budget to enforce), but the
        # audit trail should still record that the cost number was a lower bound.
        return CostCheckResult(allow=True, cost_data_degraded=is_degraded)

    def _maybe_emit_cost_cap_policy_decision(
        self,
        *,
        dimension: str,
        policy_value: float | None,
        model_md_value: float | None,
        effective: float | None,
        attempted_value: float,
        enforced: bool,
    ) -> None:
        """Emit a ``policy_decision`` audit event if Policy contributed to this
        cost-cap denial (#89 PR 3a).

        Policy "contributed" when its cap value is lower than or equal to
        model.md's value (or model.md has no opinion / is disabled).
        When model.md alone would have fired the cap (policy_value is None
        or model_md_value < policy_value), no Policy event is emitted —
        the denial is a pure model.md event; Policy was not the decisive
        layer.

        F3 fix (PR 3a Round 1): ``enforced`` is computed by the caller from
        the ``_cap_action`` result — True only when the action actually
        blocked (cap_action="skip"). For "alert" / "fallback" the call
        proceeds with money spent, so the event records ``enforced=False``
        — the audit log truthfully reflects what happened.
        """
        if self._policy_snapshot_this_call is None:
            return
        # Policy contributed only when it had an opinion and that opinion was
        # the binding cap (F4 fix: helper handles model_md_value=None natively;
        # no coercion needed at the call site).
        denying_layer = self._resolve_denying_layer(
            policy_value=policy_value,
            model_md_value=model_md_value,
            effective=effective,
        )
        if denying_layer != "policy":
            return

        from .policy.types import PolicyDecision, _emit_policy_decision

        decision = PolicyDecision(
            decision_kind="deny",
            denying_layer=denying_layer,
            agent_name=self.name,
            axis="cost_cap",
            cap_dimension=dimension,
            attempted_value=attempted_value,
            effective_cap=effective,
            cache_ttl_s=self._policy_snapshot_this_call.cache_ttl_s,
            enforced=enforced,
        )
        _emit_policy_decision(
            decision,
            self.log_backend,
            run_id=self.run_id or "unknown",
        )

    def _maybe_fire_warning(self, period: str, pct: float) -> None:
        state_path = self.agent_root / ".cost-warnings.json"
        state = _costs.load_warning_state(state_path)
        today_key = (
            date.today().isoformat()
            if period == "daily"
            else date.today().strftime("%Y-%m")
        )

        for threshold in self.config.warning_thresholds:
            already = (
                state.get(period, {}).get(today_key, {}).get(str(threshold), False)
            )
            if pct >= threshold and not already:
                # Fire (just log to journal/log for v1; future: telegram/email)
                severity = "WARN" if threshold >= 0.80 else "INFO"
                self._log(
                    {
                        "trigger": "cost_warning",
                        "model": "n/a",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "status": "ok",
                        "summary": f"{severity}: {period} cost at {pct * 100:.0f}% of cap (threshold {threshold * 100:.0f}%)",
                    }
                )
                state.setdefault(period, {}).setdefault(today_key, {})[
                    str(threshold)
                ] = True

        _costs.save_warning_state(state_path, state)

    def _cap_action(self, action: str, reason: str) -> CostCheckResult:
        if action == "skip":
            return CostCheckResult(allow=False, action="skip", reason=reason)
        if action == "fallback":
            return CostCheckResult(
                allow=True,
                action="fallback",
                reason=reason,
                fallback_model=self.config.fallback_model,
            )
        if action == "alert":
            return CostCheckResult(allow=True, action="alert", reason=reason)
        raise ValueError(f"unknown cap action: {action}")

    # ────────────────────────────────────────────────────────────
    # Logging

    def _log(self, record: dict) -> None:
        """Append one log line via ``self.log_backend.append(...)``.

        Thin wrapper that builds a ``RunRecord`` from the legacy dict
        literal (every existing ``self._log({"trigger": "...", ...})``
        site keeps its dict shape verbatim) and routes through the
        operator-pinned ``LogBackend``. Default ``FilesystemLogBackend``
        preserves the legacy on-disk shape byte-for-byte (writes to
        ``<agent_root>/log/YYYY-MM/YYYY-MM-DD.jsonl`` via
        ``_io.atomic_append_jsonl`` — same path as the pre-PR-2 code).

        Pre-population matches the legacy idiom:
        - ``ts`` set to local-tz ISO-8601 if absent (so
          ``record.ts.date() == date.today()`` in local time —
          preserves the day-file landing semantic the dashboard
          readers / dream walker depend on)
        - ``run_id`` defaulted to ``self.run_id`` so child-record
          ``parent_run_id`` rollups link to the parent run
        - ``primitive`` derived from the legacy ``trigger`` string
          via ``_derive_primitive_from_trigger`` with ``"other"`` as
          the fallback bucket
        """
        record = {"ts": datetime.now().astimezone().isoformat(), **record}
        record.setdefault("run_id", self.run_id)
        record.setdefault(
            "primitive",
            _derive_primitive_from_trigger(record.get("trigger")),
        )
        # Stamp the originating agent. Critical for shared-backend
        # deployments (#61 PR 3 review-pass Step 11 P0 #1) — without
        # ``agent_name``, the dashboard + cost-guardrail readers can't
        # filter to a single agent's records when multiple agents
        # share one SQLite/Postgres file.
        record.setdefault("agent_name", self.name)
        self.log_backend.append(RunRecord.from_dict(record))

    def _derive_summary(self, work_item: str) -> str:
        """Short summary of the work item for log records."""
        if len(work_item) <= 80:
            return work_item.strip()
        return work_item[:77].strip() + "..."

    # ────────────────────────────────────────────────────────────
    # Convenience

    def __repr__(self):
        return f"AtomicAgent(name={self.name!r}, trigger={self.trigger!r}, root={self.agent_root})"

"""Shared dataclasses for the atomic_agents package."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tools import ToolCallResult
    from .mcp import MCPServerSpec


@dataclass
class AgentConfig:
    """Loaded from model.md + tools.md at agent init."""

    # From model.md
    default_model: str
    fallback_model: str | None
    # Optional LLMBackend disambiguator (#87): when multiple registered
    # backends claim the same model id (e.g., openai + azure-openai both
    # match ``gpt-5``), this names which one wins. None → registry uses
    # the unambiguous match or raises ``AmbiguousBackendError``.
    provider: str | None = None
    max_input_tokens: int = 12_000
    max_output_tokens: int = 4_000
    temperature: float = 0.6

    # cost_guardrails block from model.md
    cost_guardrails_enabled: bool = False
    daily_cap_usd: float = 0.0
    monthly_cap_usd: float = 0.0
    daily_cap_action: str = "skip"  # skip | fallback | alert
    monthly_cap_action: str = "alert"
    warning_thresholds: list[float] = field(default_factory=lambda: [0.50, 0.80])
    alert_channel: str = "log_only"  # telegram | email | journal | log_only

    # From tools.md (parsed)
    read_paths: list[Path] = field(default_factory=list)
    write_paths: list[Path] = field(default_factory=list)
    read_only_paths: list[Path] = field(default_factory=list)
    external_apis: list[str] = field(default_factory=list)
    hard_nos: list[str] = field(default_factory=list)

    # spec/45 PR2: when True, agent.call() computes an implicit idempotency key
    # as sha256(work_item + model + max_tokens + temperature) when the caller
    # does not supply an explicit idempotency_key. Disabled by default (opt-in).
    # Enable via model.md '## Dedup Body Hash' section. ONLY active when
    # idempotency_key is None — an explicit caller-supplied key always takes
    # precedence.
    dedup_body_hash_enabled: bool = False

    # spec/47 (LOCKED): backend id for the conversation backend, parsed from
    # model.md '## Conversation Backend' section. None when the section is absent
    # (single-shot default). The three-channel resolution order in agent.call() is:
    # (1) constructor kwarg, (2) ATOMIC_AGENTS_CONVERSATION_BACKEND env var,
    # (3) this field. All three resolve to None when unset (backward-compatible).
    # DO NOT default to 'filesystem' — 'no backend == single-shot' is mandatory (rule #14).
    conversation_backend_id: str | None = None

    # From roster.md (parsed) — agent names this coordinator may delegate to.
    # Empty list = no delegation allowed.
    roster: list[str] = field(default_factory=list)

    # From mcp.md (parsed) — MCP servers this agent may connect to.
    # Empty list = no MCP servers declared (that's fine; pool not created).
    mcp_servers: list["MCPServerSpec"] = field(default_factory=list)


@dataclass
class CostCheckResult:
    allow: bool
    action: str | None = None  # 'skip' | 'fallback' | 'alert' | None
    reason: str = ""
    fallback_model: str | None = None
    # True when the cost reader was degraded (blind / partial read). Gate sites
    # that set this distinguish a real-cap-hit from a data-quality blind spot.
    # Defaults False so existing CostCheckResult(allow=True) sites need no change.
    cost_data_degraded: bool = False


@dataclass
class Capture:
    """One atomic note to be written, parsed from a capture marker."""

    type: str  # user | feedback | project | decision | reference
    name: str
    description: str
    confidence: str  # high | medium | low
    sources: list[str]
    body: str
    supersedes: str | None = None
    merge_into: str | None = None
    pinned: bool = False
    expires_at: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Response:
    """Result of an LLM call via AtomicAgent.call()."""

    text: str
    model: str  # the model actually used (may differ if fallback)
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cost_usd: float = 0.0
    cost_estimated_via_fallback: bool = False  # True when model id was not in PRICING
    latency_ms: int = 0
    summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    captures: list[Capture] = field(default_factory=list)
    skipped: bool = False  # True if cost guardrail blocked the call
    skip_reason: str = ""
    # Custom tools fields (spec/17) — populated when tool_registry has tools
    tool_calls: list["ToolCallResult"] = field(default_factory=list)
    tool_iterations: int = 1  # 1 = no tools used, 2+ = multi-turn loop
    tool_iterations_maxed: bool = False  # True when max_iterations cap was hit
    # Judge-layer ESCALATE (spec/28). When any tool_use in the actor's
    # turn produces an ESCALATE judgment, the framework writes a PENDING
    # file and returns deferred=True with the proposal_ids of every
    # escalated action. ALLOWed tool_uses in the same turn still execute
    # and their results land in ``tool_calls``; the multi-turn loop
    # terminates immediately rather than running one more iteration.
    # See spec/28 §"Escalate" and the ESCALATE state machine docs.
    deferred: bool = False
    escalation_queue_ids: list[str] = field(default_factory=list)
    # Idempotency dedup fields (spec/45 PR2). Set when agent.call() returns a
    # deduplicated result without running the LLM (a prior COMPLETED run was
    # found for this idempotency_key). The caller resolves the actual result
    # bytes via OutcomeBackend.read_result(agent_id, prior_run_id).
    #
    # deduped=True distinguishes a replayed result from a normal ok response.
    # prior_run_id: the run_id of the original completed run.
    # replayed_run_id: same as prior_run_id (alias for symmetry with the audit
    #   record field name — both refer to the original run's id).
    # result_ref: the opaque result reference stored by commit() — typically
    #   the original run's run_id; use with OutcomeBackend.read_result(agent_id, ...).
    deduped: bool = False
    prior_run_id: str | None = None
    replayed_run_id: str | None = None
    result_ref: str | None = None
    # Conversation continuity fields (spec/47 PR1). Set when conversation_backend
    # is configured and agent.call() is invoked with conversation_id.
    #
    # continuity_persisted=True: turn write-back succeeded (atomic_write committed),
    #   OR no conversation_id was supplied (single-shot — the field is irrelevant
    #   and True is the correct backward-compat sentinel).
    # continuity_persisted=False: a conversation_id WAS supplied but the turn pair
    #   was NOT persisted. Two cases: (1) write-back raised (I/O error / bad
    #   conversation_id) — the LLM response is still returned (billed run); a
    #   WARNING log with run_id is emitted for manual recovery; (2) the call
    #   short-circuited BEFORE write-back (mid-loop cost cap) so no write was
    #   attempted — the field is False so the caller is not misled into thinking
    #   history was stored. On the pure refusal short-circuits (dedup, lock_busy,
    #   pre-loop cost-skip, in_flight) the field keeps its True default: those are
    #   refusal paths where no work and no write occurred and the field is not
    #   meaningful (interpret it only on ok-path / skipped Responses).
    continuity_persisted: bool = True

    @classmethod
    def skipped_response(cls, reason: str, model: str) -> "Response":
        """Build a Response that represents a skipped (guardrailed) call."""
        return cls(
            text="",
            model=model,
            input_tokens=0,
            output_tokens=0,
            skipped=True,
            skip_reason=reason,
            summary=f"skipped: {reason}",
        )

    @classmethod
    def deduped_response(
        cls,
        prior_run_id: str | None,
        replayed_run_id: str | None,
        result_ref: str | None,
        model: str,
    ) -> "Response":
        """Build a Response representing a deduplicated (replayed) call (spec/45).

        Returned by agent.call() when lookup() finds a COMPLETED entry for
        the caller-supplied idempotency_key. The actual result bytes are NOT
        fetched eagerly — the caller resolves them via
        OutcomeBackend.read_result(agent_id, prior_run_id).

        Args:
            prior_run_id: run_id of the original completed run.
            replayed_run_id: alias for prior_run_id (audit-record field name).
            result_ref: opaque result reference from commit() — typically the
                original run_id; use with OutcomeBackend.read_result(agent_id, ...).
            model: the model from the agent config (for span attribute
                consistency — no LLM call is made for this response).

        Returns:
            A Response with deduped=True, cost_usd=0.0 (no LLM spend),
            zero token counts, and the three idempotency fields populated.
        """
        return cls(
            text="",
            model=model,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            deduped=True,
            prior_run_id=prior_run_id,
            replayed_run_id=replayed_run_id,
            result_ref=result_ref,
            summary=f"deduped: replayed from run {prior_run_id!r}",
        )


@dataclass
class HelperResult:
    """One helper_call result (returned to the parent agent)."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    # Provenance fields (per spec/10 Wave 8)
    # `sources` echoes the sources passed in so the parent agent can
    # cite them in its response without keeping the original list around.
    sources: list[str] = field(default_factory=list)
    # True when the helper output appears to preserve attribution (citation-like
    # brackets or named source mentions). Heuristic — defaults to True when
    # no sources were passed (no provenance to preserve in that case).
    provenance_preserved: bool = True

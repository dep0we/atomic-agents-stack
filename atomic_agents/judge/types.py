"""Canonical dataclasses + enums for the JudgeBackend Protocol (spec/28).

The judge layer is a pre-action validation surface that runs between LLM
``tool_use`` emission and tool handler dispatch. The Protocol contract
lives in ``backend.py``; the types it consumes and produces live here.

This module is **scaffolding only** — no behavior change in PR 1 of #112.
Reference implementations (`PolicyJudge`, `LLMJudgeBackend`), proposal
assembly, ``judges.md`` parser, and ``agent.py`` wiring follow in PRs
2-4 of the issue arc.

All types are ``@dataclass(frozen=True)`` so they are immutable and
comparable by value — safe to pass across the agent / judge / audit
boundary without defensive copying. Types carrying ``dict`` or ``list``
fields (``ActionProposal.tool_arguments``, ``ActionProposal.evidence``,
etc.) are NOT hashable, by design: tool input schemas and evidence
collections are naturally nested-mutable. Consumers that need a set/dict
key should derive one (e.g., ``proposal.proposal_id``).

Layout notes (folded into spec/28's locked text):

- Dataclasses live in ``types.py``; the Protocol contract + Judgment
  live in ``backend.py``. Mirrors #87's LLMBackend split.
- ``ClassPolicyValue`` + ``Provenance`` are defined here (spec/28
  references both).
- ``JudgePolicyContext.cited_notes: list[NoteRef]`` (progressive
  disclosure per CLAUDE.md rule #6 — metadata only, not bodies).
- ``enforcement_action: str`` (not enum) per spec/28's audit shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..memory.backend import NoteRef


# ──────────────────────────────────────────────────────────────────
# Enums (StrEnum for JSONL round-trip + audit-log readability)


class ActionClass(StrEnum):
    """Per-action risk classification, sourced from ``tools.md``/``mcp.md``.

    Defines the policy class a proposal lands in, which in turn drives
    ``ClassPolicySnapshot`` lookup and ``judges.md`` per-class behavior.
    """

    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    HIGH_RISK = "high_risk"


class ClassPolicyValue(StrEnum):
    """Per-class judgment behavior selector from ``judges.md``.

    Spec/28 references this in ``ClassPolicySnapshot`` but doesn't define
    the enum. Defining here for PR 1 scaffolding.

    - ``BYPASS`` — judge not invoked; proposal proceeds.
    - ``ALLOW_WITH_AUDIT`` — judge invoked in audit-only mode (decision
      recorded but not enforced; ``enforcement_action="audit_bypass"``).
    - ``JUDGE_REQUIRED`` — judge invoked; decision enforced.
    - ``ESCALATE`` — judge invoked; on ``ALLOW``, proposal still
      escalated to operator (e.g., for high-risk classes in some
      deployments).
    """

    BYPASS = "bypass"
    ALLOW_WITH_AUDIT = "allow_with_audit"
    JUDGE_REQUIRED = "judge_required"
    ESCALATE = "escalate"


class Reversibility(StrEnum):
    """Actor's plain-language assessment of whether an action can be
    undone. Used by the judge as a heuristic, not a guarantee.

    - ``REVERSIBLE`` — action can be undone without operator involvement.
    - ``REVERSIBLE_WITH_ARTIFACT`` — undo requires a stored artifact
      (a memory version, a tool-call result, a backup snapshot).
    - ``IRREVERSIBLE`` — action cannot be undone (external send,
      production deploy, payment).
    """

    REVERSIBLE = "reversible"
    REVERSIBLE_WITH_ARTIFACT = "reversible_with_artifact"
    IRREVERSIBLE = "irreversible"


class Provenance(StrEnum):
    """Memory provenance label per spec/28 §"Memory provenance
    integration" (cross-referenced from #113).

    Used on ``Evidence.provenance`` so the judge can weight evidence by
    how it was obtained. Spec/28 references this without defining it —
    defining here so the scaffolding compiles; #113's memory-provenance
    implementation will eventually own the canonical definition.

    - ``LEGACY`` — pre-provenance memory (no label recorded).
    - ``OBSERVED`` — content captured directly from a tool result,
      conversation message, or external source.
    - ``INFERRED`` — agent-derived from observation; one layer removed.
    - ``GENERATED`` — agent-produced de novo (e.g., a planning note).
    - ``CONFIRMED`` — operator-confirmed (promoted from memory→persona,
      or explicitly approved).
    - ``DISPUTED`` — flagged as questionable; downstream callers may
      want to discount.
    - ``SUPERSEDED`` — replaced by newer evidence; kept for audit.
    """

    LEGACY = "legacy"
    OBSERVED = "observed"
    INFERRED = "inferred"
    GENERATED = "generated"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


# ──────────────────────────────────────────────────────────────────
# Proposal sub-types


@dataclass(frozen=True)
class SkillRef:
    """Reference to a skill that was loaded when a proposal was made.

    spec/28:203-205 declares ``name: str`` + ``file_hash: str``. The
    field stays optional with a ``None`` default — proposal assembly
    today (PR 2a) doesn't compute the hash, so requiring it would
    break every existing caller. Restoring the required shape is a
    future-strengthening that tracks with #161; field-order is a
    documentation property, not a contract.
    """

    name: str
    file_hash: str | None = None


@dataclass(frozen=True)
class Evidence:
    """Supporting reference cited by the actor in a side-channel marker.

    The judge uses evidence to weight a proposal's reason. Evidence is
    additive across the proposal lifecycle — the judge may *append* via
    ``ProposalAmendment.appended_evidence`` but cannot replace or remove
    existing entries.

    Field-order vs spec/28:187-191: Python forbids required fields
    after defaulted fields; ``source_hash`` (Optional, None default)
    follows ``claim`` + ``provenance``. Field order is a documentation
    property, not a contract — operators construct via keyword args.
    """

    source: str  # note name, conversation ref, skill name
    claim: str  # what this source supports
    provenance: Provenance
    source_hash: str | None = None  # sha256 of source content at citing time


@dataclass(frozen=True)
class Authorization:
    """Who granted the actor authority to take this action."""

    granted_by: str  # "operator" | "policy" | "delegated_from:<agent>"
    scope: str  # plain language description
    granted_at: str  # ISO-8601 of when granted
    expires_at: str | None = None  # ISO-8601 if time-bounded


@dataclass(frozen=True)
class ProposalBinding:
    """The execution-binding triple recorded in ``JudgmentEvent``.

    Together, ``tool_call_id`` + ``tool_definition_hash`` + ``arguments_hash``
    pin the judgment to the exact action the framework will execute.
    TOCTOU defense per spec/28 §"Proposal binding".
    """

    tool_call_id: str
    tool_definition_hash: str
    arguments_hash: str


# ──────────────────────────────────────────────────────────────────
# Core proposal types


@dataclass(frozen=True)
class ActionProposal:
    """A structured proposal assembled by the framework from the actor's
    tool_use block + side-channel marker.

    The judge inspects ``ActionProposal``; it does NOT see raw tool
    arguments outside the proposal binding. Split fields:

    - **Framework-introspected** (from the tool_use block + runtime
      context): ``tool_name`` through ``mcp_server``. The framework
      controls these; the actor cannot forge them.
    - **Actor-supplied** (via side-channel marker bound to this
      ``tool_call_id``): ``reason`` through ``target_audience``.
      Required for side-effectful classes; optional for read-only.
    - **Framework-set after assembly**: ``proposal_id`` + ``proposal_ts``.

    See spec/28 §"Action proposal" for the field-presence rules table.
    """

    # Framework-introspected
    tool_name: str
    tool_arguments: dict[str, Any]
    tool_call_id: str
    tool_definition_hash: str
    arguments_hash: str
    classification: ActionClass
    classification_source: str  # "tools.md" | "mcp.md" | "default_unknown"
    actor_agent: str
    actor_run_id: str
    proposal_id: str
    proposal_ts: str

    # Framework-introspected — defaulted
    actor_model_id: str | None = None
    delegate_chain: list[str] = field(default_factory=list)
    loaded_skills: list[SkillRef] = field(default_factory=list)
    mcp_server: str | None = None

    # Actor-supplied side-channel
    side_channel_for_tool_call_id: str | None = None
    reason: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    authorization: Authorization | None = None
    expected_consequence: str | None = None
    reversibility: Reversibility | None = None
    rollback_path: str | None = None
    target_audience: str | None = None  # "internal" | "external:<surface>"


@dataclass(frozen=True)
class ProposalAmendment:
    """What a judge returns on ``REVISE``. Contains ONLY the fields the
    judge is allowed to amend.

    The framework applies the amendment to the original proposal and
    produces a new bound ``ActionProposal`` with framework-recomputed
    ``classification`` + hashes + ``proposal_id`` + ``proposal_ts``. The
    judge cannot forge framework-managed fields by routing through this
    type — that's the design property.

    ``reason`` and ``authorization`` are NOT in this dataclass — they're
    carried forward from the original proposal verbatim. The judge
    explains its amendment via ``judge_note``, not by rewriting the
    actor's stated reason.
    """

    judge_note: str
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    target_audience: str | None = None
    expected_consequence: str | None = None
    reversibility: Reversibility | None = None
    rollback_path: str | None = None
    appended_evidence: list[Evidence] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Policy + context types (visible to the judge)


@dataclass(frozen=True)
class PersonaDigest:
    """A scoped excerpt of the agent's persona shown to the judge.

    Minimal scaffolding shape — PR 2/3 may refine the field set as the
    LLM judge's prompt-construction lands. The intent is that the judge
    sees the agent's identity / values / user preferences at decision
    time so it can reason about the proposal in context.
    """

    agent_name: str
    identity_excerpt: str = ""
    soul_excerpt: str = ""
    user_excerpt: str = ""


@dataclass(frozen=True)
class ToolPolicyEntry:
    """The tool's section as parsed from ``tools.md``.

    Minimal scaffolding — covers the fields the judge most needs.
    The full ``tools.md`` parser ships in PR 3 alongside ``judges.md``.
    """

    tool_name: str
    classification: ActionClass
    write_paths: list[str] = field(default_factory=list)
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClassPolicySnapshot:
    """The parsed-and-defaulted class policy per spec/28 §"ClassPolicySnapshot".

    ``source`` records which layer set each class — useful for the judge
    to explain "blocked because high_risk → escalate, inherited from
    project floor" in ``Judgment.reason``.
    """

    read_only: ClassPolicyValue
    reversible_write: ClassPolicyValue
    external_side_effect: ClassPolicyValue
    high_risk: ClassPolicyValue
    source: dict[str, str] = field(
        default_factory=dict
    )  # per-class: "judges.md" | "project_floor" | "default"


@dataclass(frozen=True)
class RunSummary:
    """A minimal record of a prior run of this agent, exposed to the
    judge in ``JudgePolicyContext.recent_runs``.

    The judge uses recent-run context to spot patterns (repeated failed
    proposals, escalation cycles, etc.). Full ``RunSummary`` shape is
    refined in PR 2's runtime-assembly digest.
    """

    run_id: str
    agent: str
    started_at: str
    final_outcome: str | None = None  # "success" | "blocked" | "escalated" | None


@dataclass(frozen=True)
class JudgePolicyContext:
    """What the judge SEES.

    Includes only policy + agent context. Crucially does NOT include
    operational config about the judge itself — that lives in
    ``JudgeRuntimeConfig`` (framework-only).

    Splitting prevents conflict-of-interest: an LLM judge cannot see /
    modify its own ``failure_policy``, budget, escalation fallback, or
    backend selection by reading from ``context.policy``.
    """

    agent_name: str
    persona_digest: PersonaDigest
    tools_md_entry: ToolPolicyEntry
    class_policy: ClassPolicySnapshot
    specialist_axis: str | None = None  # which axis this judge owns (if specialist)
    recent_runs: list[RunSummary] = field(default_factory=list)
    cited_notes: list[NoteRef] = field(default_factory=list)
    delegate_chain: list[str] = field(default_factory=list)
    loaded_skills: list[SkillRef] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Runtime config (framework-only, NEVER passed to LLM judge prompt)


@dataclass(frozen=True)
class BudgetConfig:
    """Per-budget caps in USD, summed against the *judge* cost ledger
    (separate from actor ledger per spec/28 §"Cost treatment")."""

    daily_usd: float | None = None
    monthly_usd: float | None = None
    per_action_usd: float | None = None


@dataclass(frozen=True)
class EscalationConfig:
    """Escalation destination + timeout behavior, parsed from
    ``judges.md``.

    PR 3a's parser fleshes out the load semantics; PR 3b wires it into
    the actual write-side + poll-side state machine.

    ``destination`` is the vault-relative directory where PENDING files
    are written, per spec/28:288. Default ``vault/escalations/`` matches
    the spec. Legacy ``"vault"`` is accepted by ``write_pending_escalation``
    as an alias and normalized internally (back-compat for early adopters).

    ``resolution_poll_cycle_seconds`` (spec/28:340) caps how often the
    framework scans the escalation queue. Default 60s. PR 3b uses this
    as the throttle window for the opportunistic on-call poll inside
    ``agent.call()``; a clock-driven CLI / cron path is a follow-up.
    """

    destination: str = "vault/escalations/"
    auto_decide_after_seconds: int | None = None  # None = wait indefinitely
    fallback_on_timeout: dict[str, str] = field(
        default_factory=lambda: {"default": "block"}
    )
    resolution_poll_cycle_seconds: int = 60

    # ``fallback_on_timeout`` canonical shape (PR 5a of #112): a mapping
    # keyed by ``ActionClass.value`` strings plus a mandatory ``"default"``
    # key applied when an action class is not explicitly listed. The
    # ``judges.md`` parser accepts a legacy YAML string (e.g.
    # ``fallback_on_timeout: block``) and normalizes it to
    # ``{"default": "block"}`` at parse time so existing operator configs
    # continue to work unchanged. The auto-decide path resolves the
    # per-class policy from the PENDING file's frontmatter
    # ``action_class`` (NOT the on-disk directory name) so an operator
    # who hand-creates a typo'd directory still gets the authoritative
    # classification's timeout policy. See spec/28 §"Escalation queue".

    def __post_init__(self) -> None:
        # Invariant: ``fallback_on_timeout`` must contain a ``"default"``
        # key. /ship Step 9.1 review (PR 5a) flagged that the runtime-only
        # ``assert`` guard at the policy-application site was both
        # (a) stripped by ``python -O`` and (b) swallowed by the outer
        # ``except Exception`` in ``poll_resolutions`` — wedging the
        # PENDING file every poll cycle with a silent log warning.
        # Moving the invariant to config construction means violations
        # fail loud at agent-load (parser + direct dataclass construction
        # both surface here) and the runtime path is safe-by-construction.
        if not isinstance(self.fallback_on_timeout, dict):
            raise ValueError(
                f"EscalationConfig.fallback_on_timeout must be a dict; "
                f"got {type(self.fallback_on_timeout).__name__}"
            )
        if "default" not in self.fallback_on_timeout:
            raise ValueError(
                "EscalationConfig.fallback_on_timeout must contain a "
                "'default' key. Construct via the judges.md parser "
                "(which normalizes legacy strings to {'default': ...}) "
                "or pass an explicit dict."
            )


@dataclass(frozen=True)
class JudgeRuntimeConfig:
    """What the framework uses to manage the judge.

    NEVER passed into the judge's prompt or visible to the LLM judge.
    Conformance suite (PR 4) asserts the LLM judge's prompt-construction
    code does not read from this object — preventing
    conflict-of-interest leaks.
    """

    backend_name: str
    timeout_ms: int
    budget: BudgetConfig
    escalation_config: EscalationConfig
    failure_policy: dict[str, str]  # exception_class_name -> JudgmentOutcome string
    read_audit_mode: bool = False
    judge_captures: bool = False
    model_id: str | None = None


@dataclass(frozen=True)
class JudgmentContext:
    """The ``evaluate(proposal, context)`` second argument.

    Wraps ``policy`` (what the judge sees) and ``runtime`` (what the
    framework uses to manage the judge). The split is structural; a
    backend that reaches into ``runtime`` to influence its own
    ``evaluate`` behavior fails conformance (PR 4).
    """

    policy: JudgePolicyContext
    runtime: JudgeRuntimeConfig


# ──────────────────────────────────────────────────────────────────
# Audit shape (framework-set after Judgment returns)


@dataclass(frozen=True)
class JudgmentEvent:
    """The JSONL audit shape per spec/28 §"Audit shape".

    NOT what the judge returns — ``Judgment`` is. The framework wraps
    ``Judgment`` with the runtime-only fields (``raw_outcome``,
    ``enforcement_action``, ``cost_source``, ``binding``) before
    serializing to JSONL. The judge does not get to influence how its
    decision was enforced.

    ``enforcement_action`` is typed as ``str`` per spec/28 rather than
    an enum — the five literal values are ``"audit_bypass"``,
    ``"block_executed"``, ``"allow_executed"``, ``"revise_executed"``,
    ``"escalate_pending"``. PR 2's framework wiring sets this at
    serialization time.

    ``cost_source`` is always ``"judge"`` for JudgmentEvent records (the
    field exists on cost-event records generally per #145; setting it
    explicitly here so the judge ledger is filterable). Legacy records
    without ``cost_source`` count as actor on read.
    """

    # Forward-reference avoids a backend.py → types.py → backend.py cycle.
    # PR 2 may resolve by moving Judgment into types.py if cleaner.
    event: str  # always "judgment"
    run_id: str
    parent_run_id: str
    proposal_id: str
    agent: str
    judge_id: str
    policy_version: str
    proposal: ActionProposal
    judgment: "Judgment"  # type: ignore[name-defined]  # defined in backend.py
    raw_outcome: str  # JudgmentOutcome.value
    enforcement_action: str  # see docstring above
    binding: ProposalBinding
    latency_ms: int
    cost_usd: float | None
    cost_source: str  # "judge" for JudgmentEvent; legacy records default to "actor"
    ts: str  # ISO-8601 UTC
    # PR 3b additions for ESCALATE state machine. ``synthesis_source`` is
    # set when the framework, not a real judge, produces the ESCALATE
    # outcome — either because operator class_policy=escalate fired, or
    # because failure_policy mapped an exception to escalate. Real-judge
    # ESCALATE leaves it None. Spec/28 lock-in (PR 4) will canonicalize.
    synthesis_source: str | None = None  # "class_policy" | "failure_policy" | None
    triggered_by: str | None = None  # "failure_policy:<ExceptionName>" when applicable

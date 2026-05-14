"""``JudgeBackend`` Protocol contract for the judge layer (spec/28).

The judge layer is a pre-action validation surface that sits between the
LLM's ``tool_use`` emission and the framework's tool handler dispatch.
Different backends implement the contract here — a deterministic
rule-engine ``PolicyJudge``, an LLM-backed ``LLMJudgeBackend`` (composing
``LLMBackend`` from #87), and operator-authored specialists — but the
framework's runtime sees only this Protocol.

The ``JudgeBackend`` Protocol contract (spec/28). Reference impls:
``PolicyJudge`` (rule engine) + ``LLMJudgeBackend`` (LLM-backed).
Third-party backends register via ``atomic_agents.judge.register_backend``.

Conformance is asserted by ``tests/test_judge_protocol_conformance.py``
against every shipped backend; the canonical invariants live in
spec/28 §"Conformance suite".

Placement notes:

- ``JudgeError`` subclasses live in ``atomic_agents/exceptions.py``
  alongside ``UnknownModelError`` from #87 PR 1 (consistent placement
  for backend exceptions across protocols).
- ``judge_id`` and ``policy_version`` are ``@property`` decorators.
  The ``@runtime_checkable`` + ``@property`` interaction is verified
  in ``tests/test_judge_types_and_registry.py``.
- Production backends MUST NOT return ``"unimplemented"`` from
  ``policy_version``; the conformance suite asserts this.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .types import ActionProposal, JudgmentContext, ProposalAmendment


class JudgmentOutcome(StrEnum):
    """The four-outcome model per spec/28 §"Four-outcome model".

    - ``ALLOW`` — proceed with the bound action.
    - ``BLOCK`` — refuse the action; actor receives the reason and may
      re-propose a different action on the next turn.
    - ``REVISE`` — judge returns a ``ProposalAmendment``; framework
      applies the amendment, recomputes classification + hashes, and
      runs a second judgment cycle before executing.
    - ``ESCALATE`` — pause the action; framework writes a PENDING
      escalation record; operator resolution writes a RESOLVED event.

    StrEnum so JSONL round-trips ``raw_outcome.value`` directly.
    """

    ALLOW = "allow"
    BLOCK = "block"
    REVISE = "revise"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Judgment:
    """What ``JudgeBackend.evaluate(proposal, context)`` returns.

    Distinct from ``JudgmentEvent`` (in ``types.py``) — ``Judgment`` is
    the backend's response; ``JudgmentEvent`` is the framework's audit
    shape that wraps the response with enforcement metadata.

    ``amendment`` is required when ``outcome == REVISE`` and rejected
    otherwise — outcome-fallback enforcement happens in the framework,
    not here; conformance suite (PR 4) asserts the contract.
    ``escalation_queue_id`` is required when ``outcome == ESCALATE``
    and rejected otherwise.

    ``policy_version`` is the backend's snapshot at decision time —
    typically ``sha256(tools.md) + sha256(judges.md)`` for backends
    that read those files. PR 1 stubs return ``"unimplemented"``.
    """

    outcome: JudgmentOutcome
    reason: str
    judge_id: str
    policy_version: str
    latency_ms: int = 0
    cost_usd: float | None = None
    model_id: str | None = None
    amendment: ProposalAmendment | None = None  # set on REVISE
    escalation_queue_id: str | None = None  # set on ESCALATE


@runtime_checkable
class JudgeBackend(Protocol):
    """The judge-layer Protocol per spec/28 §"Protocol surface".

    Implementations:

    - **``PolicyJudge``** (rule-engine, deterministic, PR 2) — always-on
      baseline; matches ``tools.md`` write-path / allow / deny rules
      and class-policy. Microsecond latency.
    - **``LLMJudgeBackend``** (LLM-backed, PR 2) — runs after
      ``PolicyJudge`` allowed. Default model: ``gpt-5-nano`` (OpenAI;
      different family than default Anthropic actor per
      correlated-judgment mitigation).
    - **Third-party backends** — register via
      ``atomic_agents.judge.register_backend(name, cls)`` from their
      own import path.

    All methods are synchronous. Async judges may compose by running an
    asyncio event loop inside ``evaluate`` (LLM judges typically do
    this) but the Protocol surface is sync to match the framework's
    runtime (``agent.call()`` is sync today; spec/04).
    """

    def evaluate(
        self,
        proposal: ActionProposal,
        context: JudgmentContext,
    ) -> Judgment:
        """Return the backend's judgment on this proposal.

        Must not mutate ``proposal`` or ``context.policy``. The
        conformance suite asserts idempotency: calling ``evaluate``
        twice with the same ``(proposal, context.policy)`` pair
        returns judgments differing only in operational fields
        (``latency_ms``, ``cost_usd``, timestamps embedded in the
        ``reason`` if any).

        Raises any of the ``JudgeError`` subclasses defined in
        ``atomic_agents.exceptions`` to surface a fatal condition
        (``JudgeUnavailable``, ``JudgePolicyInvalid``,
        ``JudgeBudgetExhausted``, ``JudgeProposalInvalid``,
        ``JudgeAmendedProposalRejected``). Per spec/28 §"Exception
        taxonomy", each maps to a default outcome via
        ``judges.md.failure_policy`` (default: ``block`` for all).
        """
        ...

    def supported_outcomes(self) -> set[JudgmentOutcome]:
        """Declare which outcomes this backend can return.

        Pure rule engines may return ``{ALLOW, BLOCK}`` only. LLM judges
        typically return all four. The framework rejects returned
        outcomes that are not in this set as ``JudgePolicyInvalid``
        (the backend lied about its capabilities; ``failure_policy``
        resolves — default block).
        """
        ...

    def supports_read_audit(self) -> bool:
        """Whether this backend can be invoked on read-only actions
        for audit-only (without blocking).

        When ``True`` and ``JudgeRuntimeConfig.read_audit_mode=True``,
        the framework invokes ``evaluate`` on read_only proposals but
        records the result with
        ``enforcement_action="audit_bypass"``. When ``False``, the
        framework skips ``evaluate`` for read_only proposals entirely.
        """
        ...

    def supports_specialist_composition(self) -> bool:
        """Whether multiple instances of this backend can compose in
        an ``EnsembleJudge`` (PR 2+).

        Rule-engine specialists typically return ``True``. LLM judges
        also typically return ``True``. A backend returning ``False``
        signals "I am a leaf; do not compose me into an ensemble"
        (e.g., a HumanInTheLoopJudge would say ``False``).
        """
        ...

    @property
    def judge_id(self) -> str:
        """A stable, human-readable identifier for this backend
        instance — e.g., ``"default-llm"``, ``"policy-rules"``,
        ``"mandate-check"``. Recorded in every ``JudgmentEvent`` so the
        audit trail traces decisions to a specific judge.
        """
        ...

    @property
    def policy_version(self) -> str:
        """Snapshot identifier for the policy source(s) this backend
        consults — typically a sha256 derivation of ``tools.md`` +
        ``judges.md`` content at backend-construction time.

        PR 1 stubs MUST return the literal sentinel ``"unimplemented"``
        until PR 3's ``judges.md`` parser lands. PR 4's conformance
        suite asserts production backends never return that sentinel.

        Property (not method) per spec/28's Protocol declaration.
        ``@runtime_checkable`` treats ``@property`` as attribute
        presence — ``MagicMock(spec=JudgeBackend)`` passes ``isinstance``
        even without the property's getter implementation. This is the
        documented PEP 544 gotcha; conformance asserts via a real
        method-presence check in PR 4.
        """
        ...

    def close(self) -> None:
        """Release any resources held by this backend (HTTP clients,
        file handles, etc.). Must be idempotent — the framework may
        call ``close()`` multiple times on shutdown paths or test
        teardown. A no-op implementation is acceptable.
        """
        ...

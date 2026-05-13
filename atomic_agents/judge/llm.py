"""``LLMJudgeBackend`` — LLM-backed reference judge (spec/28, #112 PR 2b).

The LLM-backed second-line judge. Composes the ``LLMBackend`` Protocol
(#87) via composition (not inheritance). Default model: ``gpt-5-nano``
(OpenAI; correlated-judgment mitigation against the default Anthropic
actor per spec/28:639).

Ensemble role (PR 2b default):

- ``PolicyJudge`` (rule-engine, deterministic, free) runs first.
- If ``PolicyJudge`` ALLOWs, ``LLMJudgeBackend`` evaluates the same
  proposal against richer semantic context — persona, recent runs,
  cited notes, the actor's stated reason and evidence.
- First BLOCK in the ensemble short-circuits the rest (spec/28:694).

Structural defenses against the documented failure modes:

- **Runtime config exposure** (spec/28:534): the prompt builder takes
  ``JudgePolicyContext`` ONLY, never ``JudgmentContext``. The
  conformance suite (PR 4) asserts this via UUID-canary sentinel
  testing. Code-level guarantee, not just a test assertion.
- **Tool-forcing limitation**: ``SyncLLMBackend.call()`` has no
  ``tool_choice`` parameter; ``LLMJudgeBackend`` uses best-effort
  tool use + fail-closed validation. If the model returns text
  without a ``judgment`` tool_use, or returns multiple tool_uses,
  or returns malformed input, the backend raises
  ``JudgeUnavailable`` → ``failure_policy`` resolves to BLOCK.
- **OpenAI argument-parsing fragility**: ``openai_compat.py`` normalizes
  malformed function arguments to ``{}``. The judge parser validates
  the normalized input explicitly — exactly one ``judgment``
  tool_use, ``input`` is dict, required ``outcome`` + ``reason``,
  ``outcome`` value in the four-element enum.
- **Determinism**: ``temperature=0`` is passed to the wrapped
  ``LLMBackend.call()``. Conformance idempotency tests use a
  deterministic fake LLMBackend; live-model determinism is best-
  effort. Prompts contain no wall-clock timestamps to keep cross-
  process hashes stable.
- **Timeout**: framework-side ``concurrent.futures`` wrapper enforces
  the configured ``timeout_ms``. Timeout → ``JudgeUnavailable``.

PR 2b ``supported_outcomes()`` advertises ``{ALLOW, BLOCK}`` only.
The tool schema accepts all four outcome values (``allow|block|revise|
escalate``) so the LLM can SIGNAL revise/escalate intent in audit
logs — backend code maps ``revise``/``escalate`` to BLOCK with
``revise_intent_not_supported`` / ``escalate_intent_not_supported``
reasons before returning ``Judgment``. PR 3 widens
``supported_outcomes`` once the second-judgment cycle (revise) and
operator-resolution polling loop (escalate) ship.
"""

from __future__ import annotations

import concurrent.futures
import json
from dataclasses import asdict
from typing import Any

from ..exceptions import JudgeUnavailable
from .backend import Judgment, JudgmentOutcome
from .proposal import compute_policy_version
from .types import ActionProposal, JudgePolicyContext


# JSON Schema for the ``judgment`` tool. The outcome enum includes all
# four spec/28 values so the LLM can signal revise/escalate intent
# even though ``supported_outcomes()`` advertises ``{ALLOW, BLOCK}``
# only in PR 2b. ``revise``/``escalate`` map to BLOCK with the
# documented reasons after parsing — operators see the LLM's intent
# in audit logs.
JUDGMENT_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["allow", "block", "revise", "escalate"],
            "description": (
                "Your decision on the proposal. 'allow' — proceed with "
                "the bound action. 'block' — refuse with reason. "
                "'revise' — propose an amendment (PR 2b: signal only; "
                "self-maps to block until PR 3's second-judgment cycle "
                "lands). 'escalate' — defer to operator (PR 2b: signal "
                "only; self-maps to block until PR 3's polling loop)."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "One-to-two sentence explanation of the decision. The "
                "actor sees this verbatim on tool_result when BLOCK; "
                "operators see it in audit logs always."
            ),
        },
    },
    "required": ["outcome", "reason"],
}


JUDGMENT_TOOL_NAME = "judgment"
JUDGMENT_TOOL_DESCRIPTION = (
    "Return your judgment on the proposed action as a structured tool "
    "call. You MUST call this tool exactly once per evaluation; do not "
    "return free-form text."
)


# Sentinel — recorded on Judgment.reason when the LLM signals a
# revise/escalate intent that PR 2b cannot enforce. Operators searching
# audit logs for the literal strings can identify "judge wanted to
# revise/escalate but the framework didn't have support yet" — useful
# early signal for PR 3's prioritization.
REVISE_INTENT_REASON = "revise_intent_not_supported"
ESCALATE_INTENT_REASON = "escalate_intent_not_supported"


def build_judge_system_prompt(policy: JudgePolicyContext) -> str:
    """Compose the LLM judge's system prompt from ``JudgePolicyContext``
    ONLY. NEVER reads from ``JudgmentContext.runtime`` — the type
    contract enforces this at the function signature level (caller
    cannot pass runtime config to this function).

    The structural separation defends against the spec/28:534
    requirement that runtime config fields ('failure_policy', budget,
    escalation, backend selection) NEVER appear in the LLM judge's
    prompt. Code-level guarantee, not just a test assertion.

    Constructed from:

    - ``agent_name`` — who the actor is
    - ``persona_digest`` (IDENTITY + SOUL + USER excerpts)
    - ``tools_md_entry`` — the specific tool's tools.md section
    - ``class_policy`` — the ClassPolicySnapshot bullet
    - ``specialist_axis`` (if specialist)
    - ``recent_runs`` summaries

    Deliberately omits ``cited_notes`` body content (the actor's
    Evidence references are surfaced via the proposal payload instead;
    per spec/28's progressive-disclosure-rule notes are NoteRef
    metadata, not full bodies).
    """
    lines = [
        "You are a pre-action judge for an AI agent.",
        "",
        f"## Agent: {policy.agent_name}",
        "",
        "## Persona digest",
        f"Identity excerpt: {policy.persona_digest.identity_excerpt or '(none)'}",
        f"Soul excerpt: {policy.persona_digest.soul_excerpt or '(none)'}",
        f"User excerpt: {policy.persona_digest.user_excerpt or '(none)'}",
        "",
        "## Tool under consideration",
        f"Tool: {policy.tools_md_entry.tool_name}",
        f"Classification: {policy.tools_md_entry.classification.value}",
    ]
    if policy.tools_md_entry.write_paths:
        lines.append(
            f"Allowed write paths: {', '.join(policy.tools_md_entry.write_paths)}"
        )
    if policy.tools_md_entry.allow_patterns:
        lines.append(
            f"Allow patterns: {', '.join(policy.tools_md_entry.allow_patterns)}"
        )
    if policy.tools_md_entry.deny_patterns:
        lines.append(
            f"Deny patterns: {', '.join(policy.tools_md_entry.deny_patterns)}"
        )

    lines += [
        "",
        "## Per-class policy",
        f"read_only: {policy.class_policy.read_only.value}",
        f"reversible_write: {policy.class_policy.reversible_write.value}",
        f"external_side_effect: {policy.class_policy.external_side_effect.value}",
        f"high_risk: {policy.class_policy.high_risk.value}",
    ]

    if policy.specialist_axis:
        lines += [
            "",
            f"## Specialist focus: {policy.specialist_axis}",
            "Evaluate the proposal through this axis specifically.",
        ]

    if policy.recent_runs:
        lines += ["", "## Recent runs of this agent (last N)"]
        for run in policy.recent_runs:
            lines.append(
                f"- {run.run_id} @ {run.started_at} → "
                f"{run.final_outcome or '(unknown)'}"
            )

    lines += [
        "",
        "## Your task",
        f"You will be given an ActionProposal. Call the `{JUDGMENT_TOOL_NAME}` "
        "tool exactly once with your structured decision. Do not return "
        "free-form text. The four outcome values are:",
        "- allow — proceed (action runs verbatim)",
        "- block — refuse with reason (actor sees the reason)",
        "- revise — signal that the proposal needs amendment "
        "(framework records the intent; PR 2b enforces as block)",
        "- escalate — signal that operator review is needed "
        "(framework records the intent; PR 2b enforces as block)",
    ]

    return "\n".join(lines)


def build_judge_user_payload(proposal: ActionProposal) -> str:
    """Serialize the proposal as the user-turn payload. Uses
    ``dataclasses.asdict`` + ``json.dumps`` with stable key ordering
    so the same proposal yields the same payload across processes."""
    return json.dumps(
        asdict(proposal),
        sort_keys=True,
        indent=2,
        default=str,  # Provenance / Reversibility / ActionClass enums
    )


def parse_judgment_tool_use(
    tool_uses: list[dict],
    *,
    judge_id: str,
    policy_version: str,
    latency_ms: int,
    cost_usd: float | None,
    model_id: str | None,
) -> Judgment:
    """Validate and parse the LLM's structured judgment tool_use into a
    ``Judgment``. Fail-closed at every malformation point per spec/28.

    Validation:
    - Exactly ONE tool_use named ``judgment``.
    - ``input`` is a dict (defensive against
      ``openai_compat.py:381`` normalizing malformed JSON to ``{}``).
    - ``outcome`` is present, a string, and in the enum.
    - ``reason`` is present and a non-empty string.

    Any failure → ``JudgeUnavailable`` (caller's ``failure_policy``
    resolves; default block).
    """
    judgment_calls = [tu for tu in tool_uses if tu.get("name") == JUDGMENT_TOOL_NAME]
    if len(judgment_calls) == 0:
        raise JudgeUnavailable(
            "LLM judge returned no `judgment` tool_use; expected exactly one. "
            "Fail-closed to BLOCK via failure_policy."
        )
    if len(judgment_calls) > 1:
        raise JudgeUnavailable(
            f"LLM judge returned {len(judgment_calls)} `judgment` tool_uses; "
            f"expected exactly one. Fail-closed."
        )

    payload = judgment_calls[0].get("input")
    if not isinstance(payload, dict):
        raise JudgeUnavailable(
            f"LLM judge's `judgment` tool_use input is not a dict "
            f"(got {type(payload).__name__}). Fail-closed."
        )

    outcome_raw = payload.get("outcome")
    if not isinstance(outcome_raw, str):
        raise JudgeUnavailable(
            "LLM judge's `judgment` tool_use input missing `outcome` field "
            "(or non-string). Fail-closed."
        )
    if outcome_raw not in {"allow", "block", "revise", "escalate"}:
        raise JudgeUnavailable(
            f"LLM judge returned outcome {outcome_raw!r} not in valid enum. "
            f"Fail-closed."
        )

    reason_raw = payload.get("reason")
    if not isinstance(reason_raw, str) or not reason_raw.strip():
        raise JudgeUnavailable(
            "LLM judge's `judgment` tool_use input missing `reason` field "
            "(or empty string). Fail-closed."
        )

    # Self-map revise/escalate → BLOCK per spec/28 §"Outcome-fallback
    # contract" (line 544). PR 2b advertises {ALLOW, BLOCK} only via
    # supported_outcomes(); revise/escalate intent surfaces in
    # Judgment.reason for operator visibility but the action is
    # blocked.
    if outcome_raw == "revise":
        return Judgment(
            outcome=JudgmentOutcome.BLOCK,
            reason=f"{REVISE_INTENT_REASON}: {reason_raw}",
            judge_id=judge_id,
            policy_version=policy_version,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            model_id=model_id,
        )
    if outcome_raw == "escalate":
        return Judgment(
            outcome=JudgmentOutcome.BLOCK,
            reason=f"{ESCALATE_INTENT_REASON}: {reason_raw}",
            judge_id=judge_id,
            policy_version=policy_version,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            model_id=model_id,
        )

    return Judgment(
        outcome=JudgmentOutcome(outcome_raw),
        reason=reason_raw,
        judge_id=judge_id,
        policy_version=policy_version,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        model_id=model_id,
    )


class LLMJudgeBackend:
    """LLM-backed ``JudgeBackend`` reference impl (spec/28).

    Composes ``LLMBackend`` (#87) — the wrapped instance handles
    provider translation, tool definition formatting, and
    ``_RawLLMResponse`` normalization. ``LLMJudgeBackend`` builds the
    judge-specific prompt + tool schema, calls the wrapped backend
    with ``temperature=0`` for determinism, and parses the response
    into a typed ``Judgment``.

    Construction:

    .. code-block:: python

        from atomic_agents.llm import find_backend_for_model
        from atomic_agents.judge.llm import LLMJudgeBackend

        backend = find_backend_for_model("gpt-5-nano")
        judge = LLMJudgeBackend(
            llm=backend,
            tools_md_text=tools_md.read_text(),
            judges_md_text=None,  # PR 3 parser populates this
            model_id="gpt-5-nano",
            timeout_ms=5000,
        )
        register_backend("llm", judge)
    """

    def __init__(
        self,
        *,
        llm,  # SyncLLMBackend instance from #87
        tools_md_text: str = "",
        judges_md_text: str | None = None,
        model_id: str = "gpt-5-nano",
        timeout_ms: int = 5000,
        max_tokens: int = 1024,
        judge_id: str = "llm-default",
    ) -> None:
        self._llm = llm
        self._tools_md_text = tools_md_text
        self._judges_md_text = judges_md_text
        self._model_id = model_id
        self._timeout_ms = timeout_ms
        self._max_tokens = max_tokens
        self._judge_id = judge_id
        self._policy_version = compute_policy_version(
            tools_md_text, judges_md_text
        )

    # ── JudgeBackend Protocol surface ──────────────────────────────

    def evaluate(self, proposal: ActionProposal, context) -> Judgment:
        """Call the wrapped LLMBackend with the judge prompt + tool
        definition. Returns a ``Judgment``.

        ``context: JudgmentContext`` — but ONLY ``context.policy`` is
        consulted; ``context.runtime`` is structurally inaccessible
        because ``build_judge_system_prompt`` takes
        ``JudgePolicyContext`` as its parameter type, not
        ``JudgmentContext``.
        """
        import time as _time
        from .types import JudgmentContext
        from ..llm.types import LLMToolDefinition

        if not isinstance(context, JudgmentContext):
            # Defensive: the Protocol's evaluate takes JudgmentContext;
            # surfacing this at evaluate-time would only happen with a
            # caller bug, but fail-closed beats a confusing prompt-
            # builder TypeError.
            raise JudgeUnavailable(
                f"LLMJudgeBackend.evaluate expected JudgmentContext, "
                f"got {type(context).__name__}. Fail-closed."
            )

        # Build the prompt from JudgePolicyContext ONLY. The function
        # signature is the structural defense — passing
        # context.runtime here would be a TypeError at the build call.
        system_prompt = build_judge_system_prompt(context.policy)
        user_payload = build_judge_user_payload(proposal)

        judgment_tool = LLMToolDefinition(
            name=JUDGMENT_TOOL_NAME,
            description=JUDGMENT_TOOL_DESCRIPTION,
            input_schema=JUDGMENT_TOOL_SCHEMA,
        )

        start = _time.time()
        # Framework-side timeout wrapper. SyncLLMBackend.call() has no
        # timeout parameter; we enforce one via concurrent.futures.
        # Timeout → JudgeUnavailable → failure_policy resolves.
        #
        # NOTE: we deliberately do NOT use ``with ThreadPoolExecutor(...)``
        # here. The context manager's ``__exit__`` calls
        # ``shutdown(wait=True)`` which BLOCKS until the worker
        # thread finishes — defeating the timeout entirely when the
        # backend call hangs. Manual shutdown with ``wait=False`` +
        # ``cancel_futures=True`` lets the timeout actually trip.
        # (Codex round-2 finding.)
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-judge"
        )
        try:
            future = executor.submit(
                self._llm.call,
                model=self._model_id,
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_payload}],
                max_tokens=self._max_tokens,
                temperature=0.0,  # determinism per round-1 reviewer P2
                tools=[judgment_tool],
            )
            try:
                raw = future.result(timeout=self._timeout_ms / 1000.0)
            except concurrent.futures.TimeoutError as exc:
                # Don't wait for the hung worker. cancel_futures=True
                # cancels still-queued items; the running call may
                # leak its thread (Python has no portable mechanism
                # to kill a running thread), but the judge surface
                # returns to the caller immediately.
                executor.shutdown(wait=False, cancel_futures=True)
                raise JudgeUnavailable(
                    f"LLM judge timeout after {self._timeout_ms}ms"
                ) from exc
            except Exception as exc:
                executor.shutdown(wait=False, cancel_futures=True)
                # Wrap any other exception as JudgeUnavailable so
                # failure_policy resolves. Preserve the original via
                # ``raise ... from`` for debugging.
                raise JudgeUnavailable(
                    f"LLM judge call failed: {type(exc).__name__}: {exc}"
                ) from exc
            # Normal path — the call returned within timeout. Shut
            # down with wait=True (default) so the worker thread is
            # released cleanly before the executor goes out of scope.
            executor.shutdown(wait=True)
        except BaseException:
            # Belt-and-suspenders cleanup for KeyboardInterrupt /
            # SystemExit raised between submit and shutdown.
            executor.shutdown(wait=False, cancel_futures=True)
            raise

        latency_ms = int((_time.time() - start) * 1000)

        # Cost estimate — token counts × pricing if the wrapped backend
        # exposes pricing for this model; None when pricing is unknown
        # (PR 3 makes this stricter via judges.md cost-cap config).
        cost_usd = None
        try:
            pricing = self._llm.pricing(self._model_id)
            if pricing is not None:
                # Cache_miss_tokens default to 0 on backends that don't
                # report them — fall back to input_tokens minus
                # cache_hit_tokens in that case so the math doesn't
                # silently undercount.
                cached_tokens = raw.cache_hit_tokens
                if raw.cache_miss_tokens:
                    full_price_tokens = raw.cache_miss_tokens
                else:
                    full_price_tokens = max(0, raw.input_tokens - cached_tokens)
                cost_usd = (
                    (full_price_tokens * pricing.input_per_million_usd / 1_000_000)
                    + (cached_tokens * pricing.input_per_million_usd
                       * pricing.cache_hit_discount / 1_000_000)
                    + (raw.output_tokens * pricing.output_per_million_usd / 1_000_000)
                )
        except Exception:
            # Pricing miss is non-fatal — the judgment still runs;
            # cost_source flows but cost_usd is None.
            cost_usd = None

        return parse_judgment_tool_use(
            raw.tool_uses,
            judge_id=self._judge_id,
            policy_version=self._policy_version,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            model_id=self._model_id,
        )

    def supported_outcomes(self) -> set[JudgmentOutcome]:
        # PR 2b ships ALLOW + BLOCK only. PR 3 widens to REVISE
        # (second-judgment cycle) and ESCALATE (polling loop).
        return {JudgmentOutcome.ALLOW, JudgmentOutcome.BLOCK}

    def supports_read_audit(self) -> bool:
        # LLM judge in audit mode costs real money per call; default
        # opt-out. Operators wanting audit-mode use class-policy
        # ALLOW_WITH_AUDIT (PR 3 judges.md).
        return False

    def supports_specialist_composition(self) -> bool:
        # LLMJudgeBackend composes happily into ensembles per spec/28.
        return True

    @property
    def judge_id(self) -> str:
        return self._judge_id

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def close(self) -> None:
        # No resources owned directly (the wrapped LLMBackend manages
        # its own HTTP clients). Idempotent no-op.
        return None


def make_default_llm_judge(
    *,
    tools_md_text: str = "",
    judges_md_text: str | None = None,
    model_id: str = "gpt-5-nano",
    timeout_ms: int = 5000,
) -> "LLMJudgeBackend | None":
    """Construct the default ``LLMJudgeBackend`` if the wrapped
    ``LLMBackend`` is reachable (model is supported by a registered
    backend + provider key resolves).

    Returns ``None`` when ANY of:

    - The model isn't claimable by a registered ``LLMBackend``
      (no provider matches the model id).
    - The provider's API key isn't configured (no env var, no
      keychain entry, no config file). Same key-resolution chain the
      actor uses via ``_llm._get_*_key()``.

    Returning ``None`` lets ``agent.py``'s ``_ensure_llm_judge`` skip
    the LLM judge cleanly in Claude-only deployments without spurious
    ``JudgeUnavailable`` blocks on every call. The dual-signal opt-in
    gate in ``agent.call()`` ensures this factory is only invoked
    when ``judges.md`` exists or ``AGENT_JUDGE_ENABLED=1``; under
    those conditions an operator-configured-but-unreachable LLM
    judge surfaces as a real ``JudgeUnavailable`` from
    ``evaluate()`` rather than getting silently skipped.

    For PR 2b the only first-party model id with a built-in key
    resolver is ``gpt-5-nano`` (OpenAI per spec/28's correlated-
    judgment mitigation). Third-party LLMJudgeBackend operators
    construct the class directly with their own backend + key.
    """
    from ..llm import find_backend_for_model

    try:
        backend = find_backend_for_model(model_id)
    except Exception:
        return None
    if backend is None:
        return None

    # Probe the OpenAI key resolver for the default model. We don't
    # store the key — we only confirm it's reachable so the LLM judge
    # has a chance of succeeding. Subsequent ``evaluate()`` calls will
    # re-resolve via the backend, which raises ``JudgeUnavailable`` if
    # the key disappears mid-run.
    if model_id.startswith("gpt-") or model_id == "gpt-5-nano":
        try:
            from .._llm import _get_openai_key
            _get_openai_key()
        except Exception:
            return None
    return LLMJudgeBackend(
        llm=backend,
        tools_md_text=tools_md_text,
        judges_md_text=judges_md_text,
        model_id=model_id,
        timeout_ms=timeout_ms,
    )

"""Tests for ``atomic_agents/judge/llm.py`` — LLMJudgeBackend reference
implementation (spec/28, #112 PR 2b).

Covers:

- **UUID-canary technique** (load-bearing): every ``JudgeRuntimeConfig``
  field is a unique UUID sentinel; captured LLMBackend mock records
  the full request payload; assert NONE of the canaries appear
  anywhere in the captured prompt or schema bytes. Catches direct
  field access AND indirect leaks (asdict serialization, logging,
  exception messages).
- **Tool-use parsing**: happy path, no tool_use, multiple tool_uses,
  malformed input dict, missing outcome/reason, invalid enum value.
- **Self-map**: model returns ``revise``/``escalate`` → BLOCK with the
  spec-required ``revise_intent_not_supported`` /
  ``escalate_intent_not_supported`` reason prefixes.
- **Determinism**: ``temperature=0`` passed to wrapped backend;
  conformance tests use a deterministic fake backend.
- **Timeout**: framework-side wrapper converts to ``JudgeUnavailable``.
- **policy_version format**: matches spec/28:302 + uses the
  centralized ``compute_policy_version`` helper.
- **Lazy registration**: ``make_default_llm_judge`` returns None when
  the OpenAI key isn't resolvable.
- **Idempotency**: same ``(proposal, context.policy)`` pair yields
  same outcome (with deterministic mock backend).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from atomic_agents.exceptions import JudgeUnavailable
from atomic_agents.judge import JudgeBackend
from atomic_agents.judge.backend import JudgmentOutcome
from atomic_agents.judge.llm import (
    ESCALATE_INTENT_REASON,
    JUDGMENT_TOOL_NAME,
    JUDGMENT_TOOL_SCHEMA,
    LLMJudgeBackend,
    REVISE_INTENT_REASON,
    build_judge_system_prompt,
    build_judge_user_payload,
    make_default_llm_judge,
    parse_judgment_tool_use,
)
from atomic_agents.judge.proposal import (
    assemble_proposal,
    compute_policy_version,
)
from atomic_agents.judge.types import (
    ActionClass,
    BudgetConfig,
    ClassPolicySnapshot,
    ClassPolicyValue,
    EscalationConfig,
    JudgePolicyContext,
    JudgeRuntimeConfig,
    JudgmentContext,
    PersonaDigest,
    ToolPolicyEntry,
)
from atomic_agents.llm import _RawLLMResponse
from atomic_agents.llm.types import (
    LLMCapabilities,
    LLMToolDefinition,
    PricingInfo,
)


# ──────────────────────────────────────────────────────────────────
# Deterministic fake LLM backend


class _FakeLLMBackend:
    """Deterministic ``SyncLLMBackend`` for testing. Captures every
    call to ``call()`` so tests can assert against prompt + schema +
    parameters. ``next_response`` can be overridden per test to drive
    different model outputs."""

    def __init__(
        self,
        *,
        next_response: _RawLLMResponse | None = None,
        provider_id: str = "fake-openai",
    ):
        self._provider_id = provider_id
        self.next_response = next_response or _RawLLMResponse(
            text="",
            input_tokens=10,
            output_tokens=5,
            tool_uses=[
                {
                    "name": JUDGMENT_TOOL_NAME,
                    "input": {"outcome": "allow", "reason": "ok by stub"},
                    "id": "judgment_1",
                },
            ],
        )
        # Capture every call() invocation so tests can assert against
        # the full request payload.
        self.calls: list[dict] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def supports_model(self, model_id: str) -> bool:
        return True

    def capabilities(self, model_id: str) -> LLMCapabilities:
        return LLMCapabilities(
            tools=True,
            tool_results=True,
            cache_control=False,
            streaming=False,
            vision=False,
            max_input_tokens=100_000,
            max_output_tokens=8_192,
            usage_reporting=True,
            structured_output=False,
        )

    def pricing(self, model_id: str) -> PricingInfo | None:
        return PricingInfo(
            input_per_million_usd=1.0,
            output_per_million_usd=2.0,
            cache_hit_discount=0.10,  # 10% of input price for cache hits
        )

    def count_tokens(self, model_id: str, text: str) -> int:
        return max(1, len(text) // 4)

    def call(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        tools: list[LLMToolDefinition] | None = None,
        cache_directives: list | None = None,
    ) -> _RawLLMResponse:
        self.calls.append({
            "model": model,
            "system_prompt": system_prompt,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools,
        })
        return self.next_response

    def format_tool_results(self, tool_uses, tool_results, assistant_text=""):
        return []


# ──────────────────────────────────────────────────────────────────
# Fixtures


def _make_canary_context() -> tuple[JudgmentContext, dict[str, str]]:
    """Build a ``JudgmentContext`` where every ``JudgeRuntimeConfig``
    field is a unique UUID sentinel. Returns ``(context, canaries)``
    so tests can assert no canary appears in the prompt."""
    canaries = {
        "failure_policy_judge_unavailable": (
            f"FAILURE-POLICY-CANARY-{uuid.uuid4().hex}"
        ),
        "failure_policy_proposal_invalid": (
            f"FAILURE-POLICY-CANARY2-{uuid.uuid4().hex}"
        ),
        "model_id": f"MODEL-ID-CANARY-{uuid.uuid4().hex}",
        "backend_name": f"BACKEND-NAME-CANARY-{uuid.uuid4().hex}",
        "escalation_destination": (
            f"ESCALATION-DEST-CANARY-{uuid.uuid4().hex}"
        ),
        "escalation_fallback": f"ESCALATION-FALLBACK-CANARY-{uuid.uuid4().hex}",
        "budget_daily": "999999.42",  # numeric; full sentinel doesn't fit a float
        "budget_monthly": "888888.42",
    }
    runtime = JudgeRuntimeConfig(
        backend_name=canaries["backend_name"],
        model_id=canaries["model_id"],
        timeout_ms=5000,
        budget=BudgetConfig(
            daily_usd=float(canaries["budget_daily"]),
            monthly_usd=float(canaries["budget_monthly"]),
        ),
        escalation_config=EscalationConfig(
            destination=canaries["escalation_destination"],
            fallback_on_timeout=canaries["escalation_fallback"],
        ),
        failure_policy={
            "JudgeUnavailable": canaries["failure_policy_judge_unavailable"],
            "JudgeProposalInvalid": canaries["failure_policy_proposal_invalid"],
        },
    )
    policy = JudgePolicyContext(
        agent_name="alice",
        persona_digest=PersonaDigest(agent_name="alice"),
        tools_md_entry=ToolPolicyEntry(
            tool_name="send_email",
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
        ),
        class_policy=ClassPolicySnapshot(
            read_only=ClassPolicyValue.BYPASS,
            reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
            external_side_effect=ClassPolicyValue.JUDGE_REQUIRED,
            high_risk=ClassPolicyValue.JUDGE_REQUIRED,
        ),
    )
    return JudgmentContext(policy=policy, runtime=runtime), canaries


def _proposal_for(args=None, class_=ActionClass.EXTERNAL_SIDE_EFFECT):
    return assemble_proposal(
        {"name": "send_email", "input": args or {"to": "x@y"}, "id": "tc_1"},
        {"for_tool_call_id": "tc_1", "reason": "test"}
        if class_ != ActionClass.READ_ONLY else None,
        classification=class_,
        classification_source="tools.md",
        tool_definition_hash="tdef_x",
        actor_agent="alice",
        actor_run_id="run_1",
    )


# ──────────────────────────────────────────────────────────────────
# Protocol satisfaction


class TestProtocolSurface:
    def test_satisfies_judge_backend_protocol(self):
        fake = _FakeLLMBackend()
        judge = LLMJudgeBackend(llm=fake)
        assert isinstance(judge, JudgeBackend)

    def test_supported_outcomes_pr2b_set(self):
        # PR 2b advertises {ALLOW, BLOCK}. PR 3 widens.
        assert LLMJudgeBackend(llm=_FakeLLMBackend()).supported_outcomes() == {
            JudgmentOutcome.ALLOW, JudgmentOutcome.BLOCK,
        }

    def test_supports_read_audit_false(self):
        # LLM judge costs money; opt-out by default.
        assert LLMJudgeBackend(llm=_FakeLLMBackend()).supports_read_audit() is False

    def test_supports_specialist_composition(self):
        assert LLMJudgeBackend(llm=_FakeLLMBackend()).supports_specialist_composition() is True

    def test_close_idempotent(self):
        judge = LLMJudgeBackend(llm=_FakeLLMBackend())
        judge.close()
        judge.close()  # no raise


# ──────────────────────────────────────────────────────────────────
# UUID-canary technique — JudgeRuntimeConfig MUST NOT leak into prompt


class TestRuntimeConfigCanaries:
    def test_no_canary_in_system_prompt_or_user_payload_or_tools(self):
        fake = _FakeLLMBackend()
        judge = LLMJudgeBackend(llm=fake)
        ctx, canaries = _make_canary_context()
        proposal = _proposal_for()

        # Build prompts directly so we don't fire a real call() — but
        # also exercise evaluate() against the fake backend so we
        # capture the full request payload.
        judge.evaluate(proposal, ctx)
        assert len(fake.calls) == 1
        call = fake.calls[0]

        # Reconstruct the full request payload as a flat string so the
        # scan catches canaries in ANY location — system prompt, user
        # turn, tool schema, tool description.
        import json as _json
        payload_blob = (
            call["system_prompt"]
            + _json.dumps(call["messages"], default=str)
            + _json.dumps(
                [(t.name, t.description, t.input_schema)
                 for t in (call["tools"] or [])],
                default=str,
            )
        )

        for label, canary in canaries.items():
            assert canary not in payload_blob, (
                f"JudgeRuntimeConfig canary {label!r}={canary!r} LEAKED into "
                f"the LLM judge's prompt — runtime config exposure per "
                f"spec/28:534."
            )

    def test_build_system_prompt_signature_takes_policy_context_only(self):
        # Structural defense: build_judge_system_prompt accepts
        # JudgePolicyContext as its parameter type. Pass a
        # JudgmentContext directly → AttributeError because
        # JudgmentContext lacks the JudgePolicyContext fields. This
        # pins the type-level guarantee that caller can't accidentally
        # pass the full context.
        ctx, _ = _make_canary_context()
        # Sanity: passing the policy works.
        text = build_judge_system_prompt(ctx.policy)
        assert "alice" in text
        # Confirming the signature: build a wrapper that tries to pass
        # the full context where policy is expected. This would crash
        # at attribute access time inside build_judge_system_prompt.
        with pytest.raises(AttributeError):
            build_judge_system_prompt(ctx)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────
# parse_judgment_tool_use — every malformation path


class TestParseJudgmentToolUse:
    def _kwargs(self):
        return dict(
            judge_id="test",
            policy_version="x",
            latency_ms=10,
            cost_usd=0.001,
            model_id="test-model",
        )

    def test_happy_path_allow(self):
        tool_uses = [
            {"name": "judgment", "input": {"outcome": "allow", "reason": "ok"},
             "id": "j1"}
        ]
        j = parse_judgment_tool_use(tool_uses, **self._kwargs())
        assert j.outcome == JudgmentOutcome.ALLOW
        assert j.reason == "ok"
        assert j.judge_id == "test"
        assert j.model_id == "test-model"

    def test_happy_path_block(self):
        tool_uses = [
            {"name": "judgment", "input": {"outcome": "block", "reason": "bad"},
             "id": "j1"}
        ]
        j = parse_judgment_tool_use(tool_uses, **self._kwargs())
        assert j.outcome == JudgmentOutcome.BLOCK

    def test_revise_self_maps_to_block(self):
        tool_uses = [
            {"name": "judgment",
             "input": {"outcome": "revise", "reason": "lower the limit"},
             "id": "j1"}
        ]
        j = parse_judgment_tool_use(tool_uses, **self._kwargs())
        assert j.outcome == JudgmentOutcome.BLOCK
        assert REVISE_INTENT_REASON in j.reason
        assert "lower the limit" in j.reason

    def test_escalate_self_maps_to_block(self):
        tool_uses = [
            {"name": "judgment",
             "input": {"outcome": "escalate", "reason": "operator review"},
             "id": "j1"}
        ]
        j = parse_judgment_tool_use(tool_uses, **self._kwargs())
        assert j.outcome == JudgmentOutcome.BLOCK
        assert ESCALATE_INTENT_REASON in j.reason

    def test_no_tool_use_raises_judge_unavailable(self):
        with pytest.raises(JudgeUnavailable, match="no `judgment` tool_use"):
            parse_judgment_tool_use([], **self._kwargs())

    def test_only_other_tool_uses_raises(self):
        # Model called a different tool — fail-closed.
        tool_uses = [
            {"name": "search", "input": {"q": "..."}, "id": "s1"},
        ]
        with pytest.raises(JudgeUnavailable, match="no `judgment` tool_use"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())

    def test_multiple_judgment_tool_uses_raises(self):
        tool_uses = [
            {"name": "judgment", "input": {"outcome": "allow", "reason": "a"},
             "id": "j1"},
            {"name": "judgment", "input": {"outcome": "block", "reason": "b"},
             "id": "j2"},
        ]
        with pytest.raises(JudgeUnavailable, match=r"returned 2 `judgment`"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())

    def test_non_dict_input_raises(self):
        # openai_compat normalizes malformed args to {} (P2 fix). Pin
        # the validation by passing a non-dict input here.
        tool_uses = [
            {"name": "judgment", "input": "not a dict", "id": "j1"},
        ]
        with pytest.raises(JudgeUnavailable, match="not a dict"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())

    def test_missing_outcome_raises(self):
        tool_uses = [
            {"name": "judgment", "input": {"reason": "no outcome"}, "id": "j1"},
        ]
        with pytest.raises(JudgeUnavailable, match="`outcome`"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())

    def test_non_string_outcome_raises(self):
        tool_uses = [
            {"name": "judgment", "input": {"outcome": 42, "reason": "x"},
             "id": "j1"},
        ]
        with pytest.raises(JudgeUnavailable, match="`outcome`"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())

    def test_invalid_enum_value_raises(self):
        tool_uses = [
            {"name": "judgment",
             "input": {"outcome": "maybe", "reason": "x"},
             "id": "j1"},
        ]
        with pytest.raises(JudgeUnavailable, match="not in valid enum"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())

    def test_missing_reason_raises(self):
        tool_uses = [
            {"name": "judgment", "input": {"outcome": "allow"}, "id": "j1"},
        ]
        with pytest.raises(JudgeUnavailable, match="`reason`"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())

    def test_empty_reason_raises(self):
        tool_uses = [
            {"name": "judgment", "input": {"outcome": "allow", "reason": "  "},
             "id": "j1"},
        ]
        with pytest.raises(JudgeUnavailable, match="`reason`"):
            parse_judgment_tool_use(tool_uses, **self._kwargs())


# ──────────────────────────────────────────────────────────────────
# Determinism: temperature=0 + capture surface


class TestDeterminism:
    def test_temperature_zero_passed_to_backend(self):
        fake = _FakeLLMBackend()
        judge = LLMJudgeBackend(llm=fake)
        ctx, _ = _make_canary_context()
        judge.evaluate(_proposal_for(), ctx)
        assert fake.calls[0]["temperature"] == 0.0

    def test_same_proposal_yields_same_judgment_outcome(self):
        # With deterministic mock backend, idempotency property holds.
        fake = _FakeLLMBackend()
        judge = LLMJudgeBackend(llm=fake)
        ctx, _ = _make_canary_context()
        proposal = _proposal_for()
        j1 = judge.evaluate(proposal, ctx)
        j2 = judge.evaluate(proposal, ctx)
        assert j1.outcome == j2.outcome
        assert j1.reason == j2.reason


# ──────────────────────────────────────────────────────────────────
# Timeout: framework-side wrapper → JudgeUnavailable


class TestTimeoutWrapper:
    def test_timeout_raises_judge_unavailable(self):
        class _SlowBackend(_FakeLLMBackend):
            def call(self, **kwargs):
                time.sleep(2.0)
                return self.next_response

        judge = LLMJudgeBackend(llm=_SlowBackend(), timeout_ms=200)
        ctx, _ = _make_canary_context()
        with pytest.raises(JudgeUnavailable, match="timeout"):
            judge.evaluate(_proposal_for(), ctx)

    def test_timeout_actually_trips_wall_clock_not_after_backend_finishes(self):
        # Codex round-2 P2: the original ``with ThreadPoolExecutor``
        # context manager called ``shutdown(wait=True)`` on exit,
        # blocking past the timeout. This test asserts the timeout
        # fires within ~timeout_ms wall-clock, NOT after the backend
        # actually finishes its 5-second sleep.
        class _VerySlowBackend(_FakeLLMBackend):
            def call(self, **kwargs):
                time.sleep(5.0)  # would block 5s if shutdown(wait=True)
                return self.next_response

        judge = LLMJudgeBackend(llm=_VerySlowBackend(), timeout_ms=200)
        ctx, _ = _make_canary_context()
        elapsed_start = time.time()
        with pytest.raises(JudgeUnavailable, match="timeout"):
            judge.evaluate(_proposal_for(), ctx)
        elapsed = time.time() - elapsed_start
        # 1 second is a generous upper bound — the timeout is 200ms.
        # Allows for thread-pool startup/teardown jitter without
        # asserting the strict ~200ms; mostly we want NOT 5 seconds.
        assert elapsed < 1.5, (
            f"Timeout did not trip in time — took {elapsed:.2f}s. The "
            f"executor's shutdown(wait=True) is blocking past timeout."
        )

    def test_arbitrary_exception_wrapped_as_judge_unavailable(self):
        class _ExplodingBackend(_FakeLLMBackend):
            def call(self, **kwargs):
                raise RuntimeError("network down")

        judge = LLMJudgeBackend(llm=_ExplodingBackend())
        ctx, _ = _make_canary_context()
        with pytest.raises(JudgeUnavailable, match="network down"):
            judge.evaluate(_proposal_for(), ctx)


# ──────────────────────────────────────────────────────────────────
# policy_version semantics


class TestPolicyVersion:
    def test_uses_centralized_compute_helper(self):
        judge = LLMJudgeBackend(llm=_FakeLLMBackend(), tools_md_text="hello")
        # Format MUST match compute_policy_version output.
        assert judge.policy_version == compute_policy_version("hello", None)

    def test_policy_version_changes_with_tools_md(self):
        j1 = LLMJudgeBackend(llm=_FakeLLMBackend(), tools_md_text="a")
        j2 = LLMJudgeBackend(llm=_FakeLLMBackend(), tools_md_text="b")
        assert j1.policy_version != j2.policy_version

    def test_policy_version_includes_judges_md_when_present(self):
        j1 = LLMJudgeBackend(llm=_FakeLLMBackend(), tools_md_text="x",
                             judges_md_text=None)
        j2 = LLMJudgeBackend(llm=_FakeLLMBackend(), tools_md_text="x",
                             judges_md_text="judges-content")
        # Different judges.md → different policy_version.
        assert j1.policy_version != j2.policy_version
        assert "absent" in j1.policy_version
        assert "absent" not in j2.policy_version


# ──────────────────────────────────────────────────────────────────
# Cost emission flows


class TestCostEmission:
    def test_judgment_carries_cost_usd_when_pricing_available(self):
        fake = _FakeLLMBackend()
        judge = LLMJudgeBackend(llm=fake)
        ctx, _ = _make_canary_context()
        j = judge.evaluate(_proposal_for(), ctx)
        # Fake backend reports 10 input + 5 output tokens × pricing.
        assert j.cost_usd is not None
        assert j.cost_usd > 0

    def test_judgment_cost_usd_none_when_pricing_unavailable(self):
        class _NoPricingBackend(_FakeLLMBackend):
            def pricing(self, model_id):
                return None
        judge = LLMJudgeBackend(llm=_NoPricingBackend())
        ctx, _ = _make_canary_context()
        j = judge.evaluate(_proposal_for(), ctx)
        assert j.cost_usd is None


# ──────────────────────────────────────────────────────────────────
# Lazy registration: make_default_llm_judge returns None without key


class TestMakeDefaultLLMJudge:
    def test_returns_none_without_openai_key(self, monkeypatch):
        # Defeat all key resolution sources so make_default_llm_judge
        # gracefully returns None instead of constructing a backend
        # that would fail at evaluate time.
        for var in (
            "ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        # Mock the keychain + config-file resolvers to None too.
        from atomic_agents import _llm as _llm_mod
        monkeypatch.setattr(
            _llm_mod, "_get_openai_key",
            lambda: (_ for _ in ()).throw(
                _llm_mod.AtomicAgentsError("no key")
            ),
        )
        judge = make_default_llm_judge(tools_md_text="x")
        assert judge is None

    def test_returns_instance_when_key_resolves(self, monkeypatch):
        monkeypatch.setenv("ATOMIC_AGENTS_OPENAI_KEY", "sk-fake")
        judge = make_default_llm_judge(tools_md_text="x")
        assert judge is not None
        assert isinstance(judge, LLMJudgeBackend)


# ──────────────────────────────────────────────────────────────────
# Tool definition schema


class TestJudgmentToolDefinition:
    def test_schema_outcome_enum_has_all_four_values(self):
        # Spec/28 §"Outcome-fallback contract": schema accepts all four
        # outcome values so model can SIGNAL revise/escalate intent;
        # backend self-maps. supported_outcomes is the supported set.
        assert set(JUDGMENT_TOOL_SCHEMA["properties"]["outcome"]["enum"]) == {
            "allow", "block", "revise", "escalate"
        }

    def test_required_fields(self):
        assert set(JUDGMENT_TOOL_SCHEMA["required"]) == {"outcome", "reason"}

    def test_tool_name_constant(self):
        assert JUDGMENT_TOOL_NAME == "judgment"

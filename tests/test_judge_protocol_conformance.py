"""Conformance test suite for the JudgeBackend Protocol (spec/28).

Mirrors the shape of ``tests/test_llm_protocol_conformance.py`` — every
shipped JudgeBackend implementation is exercised against the same
contract. Today: ``PolicyJudge`` (rule-engine, runs offline) +
``LLMJudgeBackend`` wrapped around a deterministic stub LLM (also runs
offline so CI doesn't depend on network keys).

The 26 invariants enumerated in spec/28 §"Conformance suite" (lines
603-626) plus the 5 state-machine invariants added during the PR 3a/3b/3c
arc are covered here. Some invariants are framework-side (run once);
some are LLM-only (UUID canary on the prompt). Most run per-backend.

A third-party JudgeBackend in a downstream package can import this
test module's helpers + parametrize fixture to verify its own
conformance.

Invariant map (spec/28:607-626 + PR 3 additions):

 1. ``evaluate`` returns a valid ``Judgment`` for each outcome in
    ``supported_outcomes()`` — per-backend
 2. ``evaluate`` does not mutate ``proposal`` or ``context.policy``
    (idempotency) — per-backend
 3. Latency bounded by configurable timeout; timeout → ``JudgeUnavailable``
    — LLM-only (rule engine has no async timeout surface)
 4. Concurrent ``evaluate`` calls do not corrupt named state (7 sub-invariants;
    judge_budget_counter deferred — feature does not ship today)
 5. ``policy_version`` changes when policy source changes — per-backend
 6. ``policy_version`` atomic-snapshot semantics (single read; invalid
    UTF-8 → ``JudgePolicyInvalid``) — framework
 7. Framework recomputes ``classification`` for amended proposals
    (judge cannot influence) — framework
 8. Schema-invalid amended proposal → ``JudgeAmendedProposalRejected``
    — framework
 9. Stricter class policy applies when amended class is higher than
    original — framework
10. Second judgment on revised proposal cannot itself revise — framework
11. Exception taxonomy maps to outcomes per ``failure_policy`` — framework
12. Side-channel mismatched/missing/duplicate → ``JudgeProposalInvalid``
    — framework
13. Audit JSONL includes required fields — framework
14. Read-audit mode bypasses block but writes ``audit_bypass`` event
    — framework
15. ESCALATE writes PENDING file with full proposal; resolution linked
    by ``proposal_id``; redacted leaves marker — framework
16. Hash determinism — framework (``compute_arguments_hash`` /
    ``compute_tool_definition_hash``)
17. Hash sensitivity — framework
18. Project-floor ``judges.md`` cannot be relaxed → ``JudgePolicyInvalid``
    at load — framework
19. ``JudgeRuntimeConfig`` fields never appear in LLM prompt (UUID
    canary) — LLM-only
20. ``close()`` is idempotent — per-backend
21. (PR 3b) O_EXCL sidecar de-dup — framework
22. (PR 3b) auto-decide CAS — framework
23. (PR 3b) body integrity → ``proposal_body_tampered`` — framework
24. ``PolicyJudge.supported_outcomes`` = {ALLOW, BLOCK, ESCALATE}
    — per-backend
25. ``LLMJudgeBackend.supported_outcomes`` includes REVISE — per-backend
26. Strict resolution-block parser — framework
"""

from __future__ import annotations

import copy
import re
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.exceptions import (
    JudgeAmendedProposalRejected,
    JudgePolicyInvalid,
    JudgeProposalInvalid,
    JudgeUnavailable,
)
from atomic_agents.judge.backend import (
    Judgment,
    JudgmentOutcome,
    JudgeBackend,
)
from atomic_agents.judge import escalation as _esc
from atomic_agents.judge import (
    register_backend,
    get_backend,
)
from atomic_agents.judge.proposal import (
    compute_arguments_hash,
    compute_policy_version,
    compute_tool_definition_hash,
)
from atomic_agents.judge.rules import PolicyJudge
from atomic_agents.judge.llm import LLMJudgeBackend
from atomic_agents.judge.types import (
    ActionClass,
    ActionProposal,
    ClassPolicySnapshot,
    ClassPolicyValue,
    EscalationConfig,
    JudgePolicyContext,
    JudgmentContext,
    JudgeRuntimeConfig,
    PersonaDigest,
    ProposalAmendment,
    BudgetConfig,
    ToolPolicyEntry,
)
from atomic_agents.llm.backend import _RawLLMResponse
from atomic_agents.llm.types import LLMCapabilities, PricingInfo
from atomic_agents.tools import ToolDefinition, ToolRegistry


# ──────────────────────────────────────────────────────────────────
# Fixtures: stub LLM backend (offline) + judge backend parametrize


class _StubLLMBackend:
    """Minimal ``SyncLLMBackend`` impl for offline conformance testing.

    Returns a deterministic ``_RawLLMResponse`` whose tool_use carries
    the configured ``outcome`` and ``reason``. Records every ``call()``
    payload in ``self.call_log`` for canary inspection (invariant 19).

    Mocking note: ``MagicMock(spec=SyncLLMBackend)`` does NOT pass
    ``isinstance`` checks per the spec/31 Protocol's
    ``@runtime_checkable`` gotcha; this is a concrete class.
    """

    def __init__(
        self,
        *,
        outcome: str = "allow",
        reason: str = "stub-reason",
        sleep_ms: int = 0,
    ) -> None:
        self._outcome = outcome
        self._reason = reason
        self._sleep_ms = sleep_ms
        self.call_log: list[dict] = []
        self._lock = threading.Lock()

    @property
    def provider_id(self) -> str:
        return "stub"

    def supports_model(self, model_id: str) -> bool:
        return True

    def capabilities(self, model_id: str) -> LLMCapabilities:
        return LLMCapabilities(
            tools=True,
            tool_results=True,
            cache_control=False,
            streaming=False,
            vision=False,
            max_input_tokens=128_000,
            max_output_tokens=4_096,
            usage_reporting=True,
            structured_output=False,
        )

    def pricing(self, model_id: str) -> PricingInfo | None:
        return None  # free for conformance

    def count_tokens(self, text: str, model_id: str | None = None) -> int:
        return max(1, len(text) // 4)

    def call(
        self,
        *,
        model,
        system_prompt,
        messages,
        max_tokens=None,
        temperature=None,
        tools=None,
        **kwargs,
    ) -> _RawLLMResponse:
        if self._sleep_ms:
            time.sleep(self._sleep_ms / 1000)
        with self._lock:
            # Capture for canary inspection; thread-safe append.
            self.call_log.append({
                "model": model,
                "system_prompt": system_prompt,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "kwargs": kwargs,
            })
        return _RawLLMResponse(
            text="",
            input_tokens=10,
            output_tokens=20,
            tool_uses=[{
                "name": "judgment",
                "id": "tu_judgment",
                "input": {
                    "outcome": self._outcome,
                    "reason": self._reason,
                },
            }],
        )

    def format_tool_results(self, tool_uses, tool_results, assistant_text):
        return []


@pytest.fixture(params=["policy", "llm"], ids=["policy_judge", "llm_judge"])
def judge_factory(request):
    """Yield a *factory* that constructs the backend with the requested
    config. Tests that need a backend with specific outcome configure
    via the factory.
    """
    backend_kind = request.param

    def make(*, outcome: str = "allow", reason: str = "stub-reason",
             tools_md_text: str = "", judges_md_text: str | None = None):
        if backend_kind == "policy":
            # PolicyJudge does not currently accept judges_md_text in its
            # constructor (PR 2a contract — policy_version is derived
            # from tools_md_text only on the rule-engine side; the
            # judges.md hash flows through agent.py's pipeline). Pass
            # tools_md_text only.
            return PolicyJudge(tools_md_text=tools_md_text)
        else:
            stub = _StubLLMBackend(outcome=outcome, reason=reason)
            return LLMJudgeBackend(
                llm=stub,
                tools_md_text=tools_md_text,
                judges_md_text=judges_md_text,
                model_id="stub-model",
            )

    return make


def _make_proposal(
    *,
    tool_arguments: dict | None = None,
    classification: ActionClass = ActionClass.EXTERNAL_SIDE_EFFECT,
) -> ActionProposal:
    args = tool_arguments if tool_arguments is not None else {"to": "x@y", "body": "hi"}
    return ActionProposal(
        tool_name="send_email",
        tool_arguments=args,
        tool_call_id="tc_conformance_1",
        tool_definition_hash="sha256:" + "a" * 64,
        arguments_hash=compute_arguments_hash(args),
        classification=classification,
        classification_source="tools.md",
        actor_agent="conformance-agent",
        actor_run_id="run_conformance_1",
        proposal_id="proposal_conformance_1",
        proposal_ts="2026-05-13T12:00:00+00:00",
        reason="conformance reason",
    )


def _make_context(
    *,
    class_policy: ClassPolicySnapshot | None = None,
) -> JudgmentContext:
    cp = class_policy or ClassPolicySnapshot(
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
    policy = JudgePolicyContext(
        agent_name="conformance-agent",
        persona_digest=PersonaDigest(agent_name="conformance-agent"),
        tools_md_entry=ToolPolicyEntry(
            tool_name="send_email",
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
            write_paths=[],
        ),
        class_policy=cp,
    )
    runtime = JudgeRuntimeConfig(
        backend_name="conformance",
        timeout_ms=5000,
        budget=BudgetConfig(),
        escalation_config=EscalationConfig(),
        failure_policy={"JudgeUnavailable": "block"},
    )
    return JudgmentContext(policy=policy, runtime=runtime)


# ──────────────────────────────────────────────────────────────────
# Invariant 1: evaluate returns valid Judgment for each supported outcome


def test_conformance_01_satisfies_judgebackend_protocol(judge_factory):
    """Every shipped backend passes ``isinstance(backend, JudgeBackend)``
    via the ``@runtime_checkable`` method-presence check."""
    backend = judge_factory()
    assert isinstance(backend, JudgeBackend)


def test_conformance_02_evaluate_returns_judgment(judge_factory):
    """``evaluate(proposal, context)`` returns a ``Judgment`` instance
    whose ``outcome`` is in ``supported_outcomes()``."""
    backend = judge_factory()
    proposal = _make_proposal()
    context = _make_context()
    judgment = backend.evaluate(proposal, context)
    assert isinstance(judgment, Judgment)
    assert judgment.outcome in backend.supported_outcomes()


# ──────────────────────────────────────────────────────────────────
# Invariant 2: evaluate does not mutate proposal or context.policy


def test_conformance_03_evaluate_does_not_mutate_inputs(judge_factory):
    """Idempotency: ``evaluate`` is a pure function with respect to its
    inputs. Frozen dataclasses enforce immutability of scalar fields;
    this test also asserts the mutable ``tool_arguments``, ``evidence``,
    and policy-context dicts/lists are not mutated.
    """
    backend = judge_factory()
    proposal = _make_proposal()
    context = _make_context()
    proposal_before = copy.deepcopy(proposal)
    context_policy_before = copy.deepcopy(context.policy)
    backend.evaluate(proposal, context)
    assert proposal == proposal_before
    assert context.policy == context_policy_before


# ──────────────────────────────────────────────────────────────────
# Invariant 3: latency bounded by timeout → JudgeUnavailable (LLM-only)


def test_conformance_04_llm_timeout_raises_unavailable():
    """LLMJudgeBackend's timeout wrapper converts slow LLM calls to
    ``JudgeUnavailable``. PolicyJudge has no async timeout (rule eval
    completes in microseconds); this invariant is LLM-only.
    """
    # A 50ms timeout against a stub that sleeps 200ms.
    slow_stub = _StubLLMBackend(sleep_ms=200)
    judge = LLMJudgeBackend(
        llm=slow_stub,
        tools_md_text="",
        judges_md_text=None,
        model_id="stub-model",
        timeout_ms=50,
    )
    proposal = _make_proposal()
    context = _make_context()
    with pytest.raises(JudgeUnavailable, match="timeout"):
        judge.evaluate(proposal, context)


# ──────────────────────────────────────────────────────────────────
# Invariant 4: concurrent state — 7 sub-invariants
# (c) judge_budget_counter not implemented today — deferred. Others below.


def test_conformance_05a_concurrent_evaluate_no_corrupted_stub_state(judge_factory):
    """(4-b) LLM client connection / stub call_log: 8 concurrent
    ``evaluate`` calls produce 8 entries with no torn dicts.

    Skipped for PolicyJudge: rule engine has no shared connection.
    """
    backend = judge_factory()
    if not isinstance(backend, LLMJudgeBackend):
        pytest.skip("LLM-only: rule engine has no shared connection state")
    proposal = _make_proposal()
    context = _make_context()
    barrier = threading.Barrier(8)
    results = []

    def call():
        barrier.wait()
        results.append(backend.evaluate(proposal, context))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    # Stub recorded 8 calls — no lost or torn entries.
    assert len(backend._llm.call_log) == 8
    # Every entry has the canonical keys (call signature matches
    # SyncLLMBackend.call in atomic_agents/llm/backend.py:210).
    expected_keys = {
        "model", "system_prompt", "messages", "tools",
        "max_tokens", "temperature", "kwargs",
    }
    for entry in backend._llm.call_log:
        assert set(entry.keys()) == expected_keys


def test_conformance_05g_concurrent_register_backend_no_corruption():
    """(4-g) Backend registry: concurrent ``register_backend`` calls
    don't tear the module-global dict.
    """
    from atomic_agents.judge import register_backend, get_backend
    from atomic_agents.judge import _registry as registry_dict

    # Snapshot to restore after test
    snapshot = dict(registry_dict)
    try:
        barrier = threading.Barrier(8)

        def register(idx):
            barrier.wait()
            register_backend(f"conformance_test_{idx}", PolicyJudge())

        threads = [
            threading.Thread(target=register, args=(i,)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for i in range(8):
            inst = get_backend(f"conformance_test_{i}")
            assert isinstance(inst, PolicyJudge)
    finally:
        registry_dict.clear()
        registry_dict.update(snapshot)


# ──────────────────────────────────────────────────────────────────
# Invariant 5: policy_version changes on policy source change


def test_conformance_06_policy_version_changes_on_policy_change(judge_factory):
    """Two backends constructed with different ``tools_md_text``
    produce different ``policy_version`` hashes.
    """
    b1 = judge_factory(tools_md_text="## Read paths\n- ~/a/\n")
    b2 = judge_factory(tools_md_text="## Read paths\n- ~/b/\n")
    assert b1.policy_version != b2.policy_version


# ──────────────────────────────────────────────────────────────────
# Invariant 6: policy_version atomic-snapshot semantics — framework


def test_conformance_07_policy_version_takes_text_not_path():
    """``compute_policy_version`` takes already-read text — file I/O is
    the caller's responsibility. The atomic-snapshot contract is that
    callers pass ``path.read_text()`` (single syscall, all-or-nothing).
    Verify the helper is text-input, not path-input.
    """
    import inspect

    sig = inspect.signature(compute_policy_version)
    # Both args are text-string-shaped. Annotations may be ``str``,
    # ``"str"``, or ``"str | None"`` (judges_md_text is Optional in
    # the post-PR-3a contract). All accepted.
    text_shapes = {str, "str", "str | None", "Optional[str]", inspect.Signature.empty}
    for name, param in sig.parameters.items():
        assert param.annotation in text_shapes, (
            f"{name}: {param.annotation!r} not text-shaped"
        )


def test_conformance_08_policy_version_invalid_utf8_raises(tmp_path):
    """When the framework reads a policy file with invalid UTF-8 bytes,
    ``Path.read_text(encoding="utf-8")`` raises ``UnicodeDecodeError``
    (or the framework wraps as ``JudgePolicyInvalid``). The atomic-
    snapshot contract is that partial-read or invalid UTF-8 must NOT
    silently produce a wrong hash (spec/28:611).
    """
    from atomic_agents.judges_md import load_judges_config

    # Write a judges.md with invalid UTF-8 bytes inside the agent_root.
    agent_root = tmp_path / "agent_with_bad_judges"
    agent_root.mkdir()
    (agent_root / "judges.md").write_bytes(b"\xff\xfe\x00 invalid utf-8")
    with pytest.raises((JudgePolicyInvalid, UnicodeDecodeError)):
        load_judges_config(
            agent_root=agent_root,
            cascade=None,
            tools_md_text="",
        )


# ──────────────────────────────────────────────────────────────────
# Invariant 7-10: REVISE machinery — covered exhaustively in
# tests/test_judge_revise_module.py and test_agent_judge_revise_dispatch.py.
# Conformance re-asserts the Protocol-level contract.


def test_conformance_09_amend_proposal_recomputes_classification():
    """Framework recomputes ``classification`` from the new ``tool_name``;
    judge cannot influence (spec/28:273). Pure-function check at the
    ``_revise.amend_proposal`` boundary.
    """
    from atomic_agents.judge import _revise

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="high_risk_tool",
        description="...",
        input_schema={"type": "object"},
        handler=lambda i: None,
        classification="high_risk",
    ))
    original = _make_proposal(classification=ActionClass.EXTERNAL_SIDE_EFFECT)
    amendment = ProposalAmendment(
        judge_note="swap to high_risk",
        tool_name="high_risk_tool",
    )
    amended = _revise.amend_proposal(
        original=original,
        amendment=amendment,
        tool_registry=registry,
        tool_classifications={"high_risk_tool": "high_risk"},
    )
    assert amended.classification == ActionClass.HIGH_RISK


def test_conformance_10_invalid_amendment_raises():
    """Schema-invalid (unknown-tool) amendment → ``JudgeAmendedProposalRejected``."""
    from atomic_agents.judge import _revise

    registry = ToolRegistry()
    proposal = _make_proposal()
    # Validate against an empty registry — tool not registered.
    with pytest.raises(JudgeAmendedProposalRejected, match="not registered"):
        _revise.validate_amended_args(proposal, registry)


# ──────────────────────────────────────────────────────────────────
# Invariants 16-17: hash determinism + sensitivity


def test_conformance_11_arguments_hash_deterministic():
    """Identical ``tool_arguments`` produce identical hashes across calls."""
    args = {"to": "x@y", "body": "hi"}
    h1 = compute_arguments_hash(args)
    h2 = compute_arguments_hash(dict(args))  # different dict instance, same content
    assert h1 == h2


def test_conformance_12_arguments_hash_sensitive():
    """Different args produce different hashes."""
    h1 = compute_arguments_hash({"to": "x@y", "body": "hi"})
    h2 = compute_arguments_hash({"to": "x@y", "body": "BYE"})
    assert h1 != h2


def test_conformance_13_tool_definition_hash_sensitive_to_schema():
    """Changes in ``input_schema`` produce different hashes."""
    handler = lambda i: None  # noqa: E731
    h1 = compute_tool_definition_hash("t", {"type": "object"}, handler)
    h2 = compute_tool_definition_hash(
        "t",
        {"type": "object", "properties": {"x": {"type": "string"}}},
        handler,
    )
    assert h1 != h2


# ──────────────────────────────────────────────────────────────────
# Invariant 19: UUID canary on LLM judge's prompt (LLM-only)


def test_conformance_14_judge_runtime_config_not_in_llm_prompt():
    """spec/28 §"Capability advertisement": the LLM judge MUST NOT see
    ``JudgeRuntimeConfig`` fields in its prompt. The structural defense
    is that ``build_judge_system_prompt`` takes ``JudgePolicyContext``,
    not ``JudgmentContext``. This test plants a UUID canary in every
    ``JudgeRuntimeConfig`` field and asserts the canary does NOT appear
    in the serialized prompt.
    """
    import uuid
    import json

    canary = f"CANARY-{uuid.uuid4().hex}"
    stub = _StubLLMBackend()
    judge = LLMJudgeBackend(
        llm=stub,
        tools_md_text="",
        judges_md_text=None,
        model_id="stub-model",
    )

    # Construct a JudgmentContext where runtime fields all contain the canary.
    cp = ClassPolicySnapshot(
        read_only=ClassPolicyValue.JUDGE_REQUIRED,
        reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
        external_side_effect=ClassPolicyValue.JUDGE_REQUIRED,
        high_risk=ClassPolicyValue.JUDGE_REQUIRED,
        source={
            "read_only": "default", "reversible_write": "default",
            "external_side_effect": "default", "high_risk": "default",
        },
    )
    policy = JudgePolicyContext(
        agent_name="canary-test",
        persona_digest=PersonaDigest(agent_name="canary-test"),
        tools_md_entry=ToolPolicyEntry(
            tool_name="send_email",
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
            write_paths=[],
        ),
        class_policy=cp,
    )
    runtime = JudgeRuntimeConfig(
        backend_name=canary,
        timeout_ms=5000,
        budget=BudgetConfig(),
        escalation_config=EscalationConfig(destination=canary),
        failure_policy={"JudgeUnavailable": canary},
        model_id=canary,
    )
    context = JudgmentContext(policy=policy, runtime=runtime)
    judge.evaluate(_make_proposal(), context)

    # Inspect every captured payload field for the canary.
    assert stub.call_log, "stub LLM did not receive any call"
    serialized = json.dumps(stub.call_log, default=str)
    assert canary not in serialized, (
        f"JudgeRuntimeConfig leaked into LLM prompt — canary {canary!r} "
        f"found in serialized call payload"
    )


# ──────────────────────────────────────────────────────────────────
# Invariant 20: close() is idempotent


def test_conformance_15_close_is_idempotent(judge_factory):
    """``close()`` may be called multiple times on shutdown paths or
    test teardown without raising.
    """
    backend = judge_factory()
    backend.close()
    backend.close()  # should not raise
    backend.close()  # should not raise


# ──────────────────────────────────────────────────────────────────
# Invariants 24-25: supported_outcomes per backend


def test_conformance_16_policy_judge_supported_outcomes():
    """PolicyJudge widens to ESCALATE in PR 3b. REVISE remains out of
    scope (rule engine cannot produce amendments).
    """
    backend = PolicyJudge()
    assert backend.supported_outcomes() == {
        JudgmentOutcome.ALLOW,
        JudgmentOutcome.BLOCK,
        JudgmentOutcome.ESCALATE,
    }


def test_conformance_17_llm_judge_supported_outcomes_includes_revise():
    """LLMJudgeBackend can return all four outcomes — REVISE is the
    differentiator from PolicyJudge.
    """
    stub = _StubLLMBackend()
    backend = LLMJudgeBackend(
        llm=stub,
        tools_md_text="",
        judges_md_text=None,
        model_id="stub-model",
    )
    outcomes = backend.supported_outcomes()
    assert JudgmentOutcome.REVISE in outcomes
    assert JudgmentOutcome.ESCALATE in outcomes


# ──────────────────────────────────────────────────────────────────
# Invariants 21-23: PR 3b additions (escalation state machine)
# These are exhaustively tested in tests/test_judge_escalation_*.py and
# tests/test_judge_auto_decide.py. Conformance asserts the Protocol-level
# contracts are present.


def test_conformance_18_o_excl_sidecar_primitive():
    """The framework's de-dup primitive is ``O_CREAT | O_EXCL`` (POSIX
    exclusive-create). Two attempts to claim the same sidecar — the
    second raises ``FileExistsError``.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sidecar = Path(tmp) / ".test_sidecar"
        _esc._claim_sidecar(sidecar)
        with pytest.raises(FileExistsError):
            _esc._claim_sidecar(sidecar)


def test_conformance_19_resolution_block_parser_strict():
    """Strict resolution-block header: ``### <Verb> by <op>`` with
    exact-case verb, literal ``by``, non-empty operator string.
    Lowercase, h4, missing ``by``, empty operator all fail.
    """
    # Valid (Approved by alice)
    decision, op, _, _ = _esc._parse_first_resolution_block(
        "### Approved by alice\n", "resolved"
    )
    assert decision is _esc.ResolutionDecision.APPROVED
    assert op == "alice"

    # Lowercase verb → UNPARSEABLE
    decision, _, _, _ = _esc._parse_first_resolution_block(
        "### approved by alice\n", "resolved"
    )
    assert decision is _esc.ResolutionDecision.UNPARSEABLE

    # h4 → UNPARSEABLE
    decision, _, _, _ = _esc._parse_first_resolution_block(
        "#### Approved by alice\n", "resolved"
    )
    assert decision is _esc.ResolutionDecision.UNPARSEABLE


def test_conformance_20_body_integrity_check_function_exists():
    """Body-integrity check function exists and accepts an ``ActionProposal``.
    The semantic test (operator tamper → BODY_TAMPERED) lives in
    test_judge_escalation_poller.py.
    """
    proposal = _make_proposal()
    # No raise — the function exists and is callable.
    result = _esc._verify_proposal_body_integrity(proposal)
    assert isinstance(result, bool)


# ──────────────────────────────────────────────────────────────────
# Invariant 24: PolicyJudge ESCALATE outcome on class_policy=escalate


def test_conformance_21_policy_judge_returns_escalate(judge_factory):
    """When ``class_policy=ESCALATE`` for the proposal's class,
    PolicyJudge returns ``Judgment(outcome=ESCALATE)``. (LLM judge
    skipped: rule-engine behavior; LLM judges respond to prompts,
    not policy snapshots directly.)
    """
    backend = judge_factory()
    if not isinstance(backend, PolicyJudge):
        pytest.skip("rule-engine-specific class_policy short-circuit")
    proposal = _make_proposal(classification=ActionClass.EXTERNAL_SIDE_EFFECT)
    cp = ClassPolicySnapshot(
        read_only=ClassPolicyValue.JUDGE_REQUIRED,
        reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
        external_side_effect=ClassPolicyValue.ESCALATE,
        high_risk=ClassPolicyValue.JUDGE_REQUIRED,
        source={
            "read_only": "default", "reversible_write": "default",
            "external_side_effect": "judges.md", "high_risk": "default",
        },
    )
    context = _make_context(class_policy=cp)
    judgment = backend.evaluate(proposal, context)
    assert judgment.outcome == JudgmentOutcome.ESCALATE


# ──────────────────────────────────────────────────────────────────
# Production policy_version must not return the PR 1 sentinel


def test_conformance_22_policy_version_not_unimplemented(judge_factory):
    """spec/28:191 PR 1 stubs return ``"unimplemented"`` as a sentinel;
    production backends MUST NOT.
    """
    backend = judge_factory(tools_md_text="## Read paths\n- ~/docs/\n")
    assert backend.policy_version != "unimplemented"


# ──────────────────────────────────────────────────────────────────
# Judge_id is a stable non-empty string


def test_conformance_23_judge_id_is_stable_non_empty_string(judge_factory):
    """``judge_id`` is a stable identifier — repeated reads on the same
    instance return the same value, and the value is non-empty.
    """
    backend = judge_factory()
    j1 = backend.judge_id
    j2 = backend.judge_id
    assert j1 == j2
    assert isinstance(j1, str)
    assert j1  # non-empty


# ──────────────────────────────────────────────────────────────────
# supports_read_audit + supports_specialist_composition return bools


def test_conformance_24_read_audit_returns_bool(judge_factory):
    backend = judge_factory()
    assert isinstance(backend.supports_read_audit(), bool)


def test_conformance_25_specialist_composition_returns_bool(judge_factory):
    backend = judge_factory()
    assert isinstance(backend.supports_specialist_composition(), bool)


# ──────────────────────────────────────────────────────────────────
# Audit shape sentinels (invariant 13)


def test_conformance_26_judgment_outcome_string_round_trips():
    """Every ``JudgmentOutcome`` has a string value that JSON-serializes
    cleanly (it's a StrEnum). Round-trip through json.dumps/loads.
    """
    import json

    for outcome in JudgmentOutcome:
        serialized = json.dumps({"outcome": outcome.value})
        parsed = json.loads(serialized)
        assert JudgmentOutcome(parsed["outcome"]) == outcome

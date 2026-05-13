"""Tests for the JudgeBackend Protocol scaffolding (spec/28, issue #112 PR 1).

Scope:

- **Type-level**: every frozen dataclass refuses mutation; default values
  correct; required fields enforced.
- **Registry**: register / get / list / unregister; name validation;
  Protocol-conformance check; replace semantics.
- **Protocol structural**: ``isinstance`` via ``@runtime_checkable``;
  missing methods fail; ``MagicMock(spec=JudgeBackend)`` regression for
  the PEP 544 ``@property`` gotcha.
- **Outcome model**: ``JudgmentOutcome`` StrEnum round-trip; outcome
  presence in audit-shape strings.
- **Canonical JSON**: determinism, sensitivity, key-order insensitivity,
  error on non-serializable input.
- **Audit-shape round-trip**: ``JudgmentEvent`` serializes through
  ``canonical_json`` for the JSONL audit log (foundation for PR 4's
  conformance assertion that ``raw_outcome`` + ``enforcement_action`` +
  ``cost_source`` + ``binding`` survive serialization).

Out of scope (lands in PR 2-4):

- Actual ``evaluate`` behavior — needs ``PolicyJudge`` / ``LLMJudgeBackend``.
- Proposal assembly + hash binding — needs ``proposal.py``.
- ``policy_version`` semantics — needs ``judges.md`` parser.
- The full ~30-test conformance suite from spec/28.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from unittest.mock import MagicMock

import pytest

from atomic_agents._canonical import canonical_json, canonical_sha256
from atomic_agents.exceptions import UnknownJudgeBackendError
from atomic_agents.judge import (
    ActionClass,
    ActionProposal,
    Authorization,
    BudgetConfig,
    ClassPolicySnapshot,
    ClassPolicyValue,
    EscalationConfig,
    Evidence,
    JudgeBackend,
    JudgePolicyContext,
    JudgeRuntimeConfig,
    Judgment,
    JudgmentContext,
    JudgmentEvent,
    JudgmentOutcome,
    PersonaDigest,
    ProposalAmendment,
    ProposalBinding,
    Provenance,
    Reversibility,
    RunSummary,
    SkillRef,
    ToolPolicyEntry,
    get_backend,
    list_backends,
    register_backend,
    unregister_backend,
)


# ──────────────────────────────────────────────────────────────────
# Test fixtures + stub backend


def _make_stub_backend(
    *,
    name: str = "stub",
    supported: set[JudgmentOutcome] | None = None,
    policy_version: str = "unimplemented",
) -> JudgeBackend:
    """Construct a minimal JudgeBackend stub for registry / Protocol tests.

    The stub returns ALLOW for every proposal — exercises the Protocol's
    method-presence requirements without committing to real semantics.
    PR 2 brings real backends; this stub is just for scaffolding tests.
    """
    if supported is None:
        supported = {JudgmentOutcome.ALLOW, JudgmentOutcome.BLOCK}

    class _Stub:
        def evaluate(
            self, proposal: ActionProposal, context: JudgmentContext
        ) -> Judgment:
            return Judgment(
                outcome=JudgmentOutcome.ALLOW,
                reason="stub allow",
                judge_id=name,
                policy_version=policy_version,
            )

        def supported_outcomes(self) -> set[JudgmentOutcome]:
            return supported

        def supports_read_audit(self) -> bool:
            return False

        def supports_specialist_composition(self) -> bool:
            return True

        @property
        def judge_id(self) -> str:
            return name

        @property
        def policy_version(self) -> str:
            return policy_version

        def close(self) -> None:
            return None

    return _Stub()  # type: ignore[return-value]


def _make_proposal() -> ActionProposal:
    """Build a minimal ActionProposal for type-level tests."""
    return ActionProposal(
        tool_name="write_note",
        tool_arguments={"path": "memory/notes/x.md", "body": "hello"},
        tool_call_id="tc_abc123",
        tool_definition_hash="sha256:def456",
        arguments_hash="sha256:ghi789",
        classification=ActionClass.REVERSIBLE_WRITE,
        classification_source="tools.md",
        actor_agent="caldwell",
        actor_run_id="run_xyz",
        proposal_id="proposal_001",
        proposal_ts="2026-05-13T17:00:00Z",
    )


def _make_context() -> JudgmentContext:
    """Build a minimal JudgmentContext for type-level tests."""
    return JudgmentContext(
        policy=JudgePolicyContext(
            agent_name="caldwell",
            persona_digest=PersonaDigest(agent_name="caldwell"),
            tools_md_entry=ToolPolicyEntry(
                tool_name="write_note",
                classification=ActionClass.REVERSIBLE_WRITE,
            ),
            class_policy=ClassPolicySnapshot(
                read_only=ClassPolicyValue.BYPASS,
                reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
                external_side_effect=ClassPolicyValue.ESCALATE,
                high_risk=ClassPolicyValue.ESCALATE,
            ),
        ),
        runtime=JudgeRuntimeConfig(
            backend_name="stub",
            timeout_ms=5000,
            budget=BudgetConfig(daily_usd=1.0),
            escalation_config=EscalationConfig(),
            failure_policy={
                "JudgeUnavailable": "block",
                "JudgeBudgetExhausted": "block",
            },
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot and restore the judge backend registry per test.

    Prevents cross-test pollution when register/unregister tests run
    interleaved with Protocol tests. Mirrors the pattern from
    ``tests/test_llm_types_and_registry.py``.
    """
    # The registry lives at module level in atomic_agents.judge as
    # `_registry`. Snapshot via the public `list_backends` + a private
    # peek into the dict so we can restore cleanly without using the
    # private name in test assertions.
    import atomic_agents.judge as judge_pkg

    snapshot = dict(judge_pkg._registry)
    try:
        # Clear, run test, restore.
        judge_pkg._registry.clear()
        yield
    finally:
        judge_pkg._registry.clear()
        judge_pkg._registry.update(snapshot)


# ──────────────────────────────────────────────────────────────────
# Type-level: frozen dataclasses


class TestFrozenDataclasses:
    """Every spec/28 dataclass should be frozen (FrozenInstanceError on
    mutation) and equal by value."""

    def test_action_proposal_is_frozen(self):
        p = _make_proposal()
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.tool_name = "different"  # type: ignore[misc]

    def test_judgment_is_frozen(self):
        j = Judgment(
            outcome=JudgmentOutcome.ALLOW,
            reason="ok",
            judge_id="stub",
            policy_version="unimplemented",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            j.outcome = JudgmentOutcome.BLOCK  # type: ignore[misc]

    def test_proposal_binding_is_frozen(self):
        b = ProposalBinding(
            tool_call_id="t1",
            tool_definition_hash="h1",
            arguments_hash="h2",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            b.tool_call_id = "t2"  # type: ignore[misc]

    def test_evidence_is_frozen(self):
        e = Evidence(source="note1", claim="x", provenance=Provenance.OBSERVED)
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.claim = "y"  # type: ignore[misc]

    def test_class_policy_snapshot_is_frozen(self):
        s = ClassPolicySnapshot(
            read_only=ClassPolicyValue.BYPASS,
            reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
            external_side_effect=ClassPolicyValue.ESCALATE,
            high_risk=ClassPolicyValue.ESCALATE,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.high_risk = ClassPolicyValue.ALLOW_WITH_AUDIT  # type: ignore[misc]

    def test_judgment_context_is_frozen(self):
        ctx = _make_context()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.policy = ctx.policy  # type: ignore[misc]

    def test_value_equality(self):
        # Same field values → equal; different field values → not equal.
        b1 = ProposalBinding("t", "h1", "h2")
        b2 = ProposalBinding("t", "h1", "h2")
        b3 = ProposalBinding("t", "h1", "h3")
        assert b1 == b2
        assert b1 != b3


class TestDefaults:
    """Defaults match the spec's optional-fields contract."""

    def test_action_proposal_optional_side_channel_defaults_none(self):
        p = _make_proposal()
        assert p.side_channel_for_tool_call_id is None
        assert p.reason is None
        assert p.evidence == []
        assert p.authorization is None
        assert p.delegate_chain == []
        assert p.loaded_skills == []

    def test_judgment_optional_fields_default_none(self):
        j = Judgment(
            outcome=JudgmentOutcome.ALLOW,
            reason="ok",
            judge_id="stub",
            policy_version="unimplemented",
        )
        assert j.amendment is None
        assert j.escalation_queue_id is None
        assert j.cost_usd is None
        assert j.latency_ms == 0


# ──────────────────────────────────────────────────────────────────
# Enums


class TestEnums:
    """``StrEnum`` members must round-trip cleanly through JSON for the
    audit-log shape."""

    @pytest.mark.parametrize(
        "outcome", [JudgmentOutcome.ALLOW, JudgmentOutcome.BLOCK,
                    JudgmentOutcome.REVISE, JudgmentOutcome.ESCALATE],
    )
    def test_judgment_outcome_round_trips(self, outcome):
        # StrEnum members ARE their .value; JSON serializes the value;
        # constructor restores the member from the value.
        encoded = json.dumps(outcome)
        decoded = JudgmentOutcome(json.loads(encoded))
        assert decoded == outcome
        assert decoded.value == outcome.value

    @pytest.mark.parametrize(
        "cls,values",
        [
            (ActionClass, ["read_only", "reversible_write",
                           "external_side_effect", "high_risk"]),
            (ClassPolicyValue, ["bypass", "allow_with_audit",
                                "judge_required", "escalate"]),
            (Reversibility, ["reversible", "reversible_with_artifact",
                             "irreversible"]),
            (Provenance, ["legacy", "observed", "inferred", "generated",
                          "confirmed", "disputed", "superseded"]),
        ],
    )
    def test_enum_membership_matches_spec(self, cls, values):
        assert [m.value for m in cls] == values

    def test_enum_unknown_value_raises(self):
        with pytest.raises(ValueError):
            JudgmentOutcome("not_an_outcome")


# ──────────────────────────────────────────────────────────────────
# Protocol structural conformance


class TestProtocolConformance:
    """``@runtime_checkable`` ``isinstance`` checks for ``JudgeBackend``."""

    def test_stub_satisfies_protocol(self):
        stub = _make_stub_backend()
        assert isinstance(stub, JudgeBackend)

    def test_missing_method_fails_isinstance(self):
        class _Incomplete:
            def evaluate(self, proposal, context):
                ...

            # Deliberately missing: supported_outcomes, supports_read_audit,
            # supports_specialist_composition, judge_id, policy_version, close.

        assert not isinstance(_Incomplete(), JudgeBackend)

    def test_non_callable_protocol_methods_fail_isinstance_on_312(self):
        # Python 3.12 made @runtime_checkable stricter — it now checks
        # whether protocol *method* attributes are callable, not just
        # present. A class with ``evaluate = None`` fails ``isinstance``
        # on 3.12+. The earlier "attribute presence only" PEP 544 gotcha
        # is partly fixed for methods (properties are still presence-only
        # — see ``test_magicmock_with_spec_passes_isinstance`` below).
        #
        # Pin the 3.12 behavior so PR 2's real implementations can rely
        # on the stricter check. The CI matrix is 3.11 + 3.12; both
        # versions exhibit this behavior for non-callable method
        # attributes per the stdlib changelog.
        class _NonCallableMethods:
            evaluate = None  # type: ignore[assignment]
            supported_outcomes = None  # type: ignore[assignment]
            supports_read_audit = None  # type: ignore[assignment]
            supports_specialist_composition = None  # type: ignore[assignment]
            judge_id = None
            policy_version = None
            close = None  # type: ignore[assignment]

        assert not isinstance(_NonCallableMethods(), JudgeBackend)

    def test_magicmock_with_spec_passes_isinstance(self):
        # The reviewer-flagged regression: `MagicMock(spec=JudgeBackend)`
        # populates the named attributes (including the ``@property``
        # ones) so the structural check passes. Document this gotcha
        # in the test rather than asserting against it — production
        # backends should fail conformance via a real method-presence
        # + callability check in PR 4's full conformance suite. Mirrors
        # ``tests/test_llm_types_and_registry.py``'s coverage.
        mock = MagicMock(spec=JudgeBackend)
        assert isinstance(mock, JudgeBackend)


# ──────────────────────────────────────────────────────────────────
# Registry: happy + negative paths


class TestRegistryHappyPath:
    def test_register_and_get(self):
        stub = _make_stub_backend()
        register_backend("stub", stub)
        assert get_backend("stub") is stub

    def test_list_backends_returns_sorted(self):
        register_backend("zebra", _make_stub_backend(name="zebra"))
        register_backend("alpha", _make_stub_backend(name="alpha"))
        assert list_backends() == ["alpha", "zebra"]

    def test_unregister_removes(self):
        register_backend("stub", _make_stub_backend())
        unregister_backend("stub")
        assert "stub" not in list_backends()

    def test_unregister_unknown_is_noop(self):
        # Should not raise — matches LLM precedent.
        unregister_backend("never_registered")

    def test_register_replaces_silently(self):
        a = _make_stub_backend(name="a-instance")
        b = _make_stub_backend(name="b-instance")
        register_backend("name", a)
        register_backend("name", b)
        assert get_backend("name") is b


class TestRegistryNegativePaths:
    """Negative-path coverage requested by Opus pre-implementation
    review — scaffolding regressions leak everywhere."""

    def test_register_empty_name_raises_value_error(self):
        with pytest.raises(ValueError, match="non-empty"):
            register_backend("", _make_stub_backend())

    def test_register_whitespace_name_raises_value_error(self):
        with pytest.raises(ValueError, match="non-empty"):
            register_backend("   ", _make_stub_backend())

    def test_register_non_protocol_class_raises_type_error(self):
        class _NotABackend:
            # No methods at all; clearly doesn't satisfy the Protocol.
            pass

        with pytest.raises(TypeError, match="JudgeBackend"):
            register_backend("bad", _NotABackend())  # type: ignore[arg-type]

    def test_get_unknown_raises_with_registered_set(self):
        register_backend("real", _make_stub_backend())
        with pytest.raises(UnknownJudgeBackendError) as exc:
            get_backend("missing")
        # Error message must list the registered set so operators see
        # what IS available — mirrors UnknownModelError pattern.
        assert "missing" in str(exc.value)
        assert "real" in str(exc.value)

    def test_get_empty_registry_raises(self):
        with pytest.raises(UnknownJudgeBackendError) as exc:
            get_backend("anything")
        # Empty registry should still produce a useful message.
        assert "Registered: []" in str(exc.value)

    def test_concurrent_register_does_not_crash(self):
        # No lock by design (matches LLM precedent); register from many
        # threads should still settle into a consistent dict state.
        # We don't assert ordering — that's the explicit footgun
        # operators sandbox if they care. We just assert no crash + a
        # populated registry.
        errors: list[BaseException] = []

        def _do_register(i: int) -> None:
            try:
                register_backend(f"thread_{i}", _make_stub_backend(name=f"t{i}"))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_do_register, args=(i,))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # All 20 should be registered (dict assignment is atomic in
        # CPython under the GIL; ordering of competing writes is what
        # is undefined, not the eventual set).
        assert len(list_backends()) == 20


# ──────────────────────────────────────────────────────────────────
# Canonical JSON


class TestCanonicalJson:
    def test_key_order_insensitive(self):
        h1 = canonical_sha256({"a": 1, "b": 2})
        h2 = canonical_sha256({"b": 2, "a": 1})
        assert h1 == h2

    def test_mutation_changes_hash(self):
        h1 = canonical_sha256({"a": 1, "b": 2})
        h2 = canonical_sha256({"a": 1, "b": 3})
        assert h1 != h2

    def test_no_whitespace_in_output(self):
        # Canonical form must be reproducible bytes — no incidental
        # whitespace from json.dumps defaults.
        encoded = canonical_json({"a": 1, "b": [2, 3]})
        assert " " not in encoded
        assert "\n" not in encoded

    def test_non_ascii_not_escaped(self):
        # ensure_ascii=False is the canonical choice — UTF-8 native
        # form is the stable cross-process representation.
        encoded = canonical_json({"greeting": "héllo"})
        assert "héllo" in encoded
        assert "\\u" not in encoded

    def test_non_serializable_raises_type_error(self):
        # The framework deliberately does NOT silently stringify
        # unknown types — a silent fallback would diverge hashes
        # across callers.
        with pytest.raises(TypeError):
            canonical_json({"bad": {1, 2, 3}})  # set is not JSON-native
        with pytest.raises(TypeError):
            canonical_json({"bad": object()})

    def test_sha256_is_64_hex_chars(self):
        h = canonical_sha256({"x": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ──────────────────────────────────────────────────────────────────
# Audit-shape round-trip (foundation for PR 4 conformance assertion)


class TestJudgmentEventSerialization:
    """``JudgmentEvent`` must survive round-trip through canonical JSON
    with all spec/28-required fields present.

    PR 4's conformance suite will assert ``raw_outcome``,
    ``enforcement_action``, ``cost_source``, and ``binding`` are
    present in every JSONL judgment record. PR 1 ships the type and
    asserts the field set so PR 2's wiring can't accidentally drop a
    field.
    """

    def test_judgment_event_has_required_fields(self):
        fields = {f.name for f in dataclasses.fields(JudgmentEvent)}
        # Per spec/28 §"Audit shape" the JSONL line carries these keys.
        # Any drift here is a spec violation — fix the dataclass.
        required = {
            "event", "run_id", "parent_run_id", "proposal_id",
            "agent", "judge_id", "policy_version", "proposal",
            "judgment", "raw_outcome", "enforcement_action",
            "binding", "latency_ms", "cost_usd", "cost_source", "ts",
        }
        assert required <= fields, f"missing fields: {required - fields}"

    def test_judgment_event_serializes_to_canonical_json(self):
        # Build a fully-populated event via dataclass-to-dict and
        # confirm canonical_json accepts it. PR 2 ships the actual
        # writer; PR 1 just proves the type-shape works.
        proposal = _make_proposal()
        judgment = Judgment(
            outcome=JudgmentOutcome.ALLOW,
            reason="ok",
            judge_id="stub",
            policy_version="unimplemented",
            latency_ms=42,
            cost_usd=0.0001,
        )
        event = JudgmentEvent(
            event="judgment",
            run_id="judgment_001",
            parent_run_id="agent_001",
            proposal_id="proposal_001",
            agent="caldwell",
            judge_id="stub",
            policy_version="unimplemented",
            proposal=proposal,
            judgment=judgment,
            raw_outcome=judgment.outcome.value,
            enforcement_action="allow_executed",
            binding=ProposalBinding(
                tool_call_id="tc_abc123",
                tool_definition_hash="sha256:def",
                arguments_hash="sha256:ghi",
            ),
            latency_ms=42,
            cost_usd=0.0001,
            cost_source="judge",
            ts="2026-05-13T17:00:00Z",
        )

        # dataclasses.asdict recurses through nested dataclasses, which
        # is what the JSONL writer in PR 2 will do.
        as_dict = dataclasses.asdict(event)
        encoded = canonical_json(as_dict)

        # Verify the load-bearing audit fields are present in the
        # encoded string — operators search audit logs by these keys.
        assert '"event":"judgment"' in encoded
        assert '"raw_outcome":"allow"' in encoded
        assert '"enforcement_action":"allow_executed"' in encoded
        assert '"cost_source":"judge"' in encoded
        assert '"tool_call_id":"tc_abc123"' in encoded

    def test_proposal_amendment_has_required_judge_amendable_fields(self):
        # Per spec/28 the judge cannot rewrite `reason` or
        # `authorization` — they must NOT appear on ProposalAmendment.
        # This is a structural property the framework relies on; pin it.
        fields = {f.name for f in dataclasses.fields(ProposalAmendment)}
        forbidden = {"reason", "authorization"}
        assert forbidden.isdisjoint(fields), (
            f"ProposalAmendment must not allow rewriting framework-owned "
            f"or actor-owned fields; found: {forbidden & fields}"
        )


# ──────────────────────────────────────────────────────────────────
# JudgeRuntimeConfig — structural-only PR 1 assertion


class TestJudgeRuntimeConfig:
    """``JudgeRuntimeConfig`` carries fields the LLM judge MUST NOT see
    in its prompt. PR 1 asserts the type-level shape; PR 2's
    ``LLMJudgeBackend`` adds the prompt-construction assertion."""

    def test_runtime_config_carries_failure_policy(self):
        rc = JudgeRuntimeConfig(
            backend_name="stub",
            timeout_ms=5000,
            budget=BudgetConfig(),
            escalation_config=EscalationConfig(),
            failure_policy={"JudgeUnavailable": "block"},
        )
        # The framework reads this; the LLM judge prompt must NOT.
        # PR 4 conformance asserts; PR 1 just confirms the field exists.
        assert rc.failure_policy["JudgeUnavailable"] == "block"

    def test_runtime_config_defaults(self):
        rc = JudgeRuntimeConfig(
            backend_name="stub",
            timeout_ms=5000,
            budget=BudgetConfig(),
            escalation_config=EscalationConfig(),
            failure_policy={},
        )
        assert rc.read_audit_mode is False
        assert rc.judge_captures is False
        assert rc.model_id is None


class TestStubPolicyVersionSentinel:
    """PR 1 stubs MUST return ``"unimplemented"`` until PR 3's
    ``judges.md`` parser lands. Document the contract in test so PR 2's
    real backends know to NOT return that sentinel."""

    def test_stub_returns_unimplemented_sentinel(self):
        stub = _make_stub_backend()
        assert stub.policy_version == "unimplemented"

    def test_stub_judge_id_is_stable(self):
        stub = _make_stub_backend(name="my-stub")
        # judge_id is recorded in every JudgmentEvent — must be stable.
        assert stub.judge_id == "my-stub"
        assert stub.judge_id == "my-stub"  # idempotent read

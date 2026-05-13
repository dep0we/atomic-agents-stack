"""Tests for ``atomic_agents/judge/rules.py`` — the ``PolicyJudge``
rule-engine baseline (spec/28, #112 PR 2a).

Covers: outcome dispatch per class-policy, write-path enforcement
parity with ``_capture.enforce_write_path`` (the divergent-enforcement
property the adversarial review flagged), policy_version semantics,
and the PR 2a ESCALATE deferral that prevents orphan PENDING files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.judge import JudgeBackend
from atomic_agents.judge.backend import JudgmentOutcome
from atomic_agents.judge.proposal import assemble_proposal
from atomic_agents.judge.rules import PolicyJudge, make_default_policy_judge
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


# ──────────────────────────────────────────────────────────────────
# Fixtures


def _ctx_with(class_value: ClassPolicyValue, *, tool_class=ActionClass.EXTERNAL_SIDE_EFFECT):
    """Build a JudgmentContext with a uniform class-policy for the
    given ActionClass."""
    snapshot = ClassPolicySnapshot(
        read_only=ClassPolicyValue.BYPASS,
        reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
        external_side_effect=ClassPolicyValue.JUDGE_REQUIRED,
        high_risk=ClassPolicyValue.JUDGE_REQUIRED,
    )
    # Override the field that matches the proposal's class.
    if tool_class == ActionClass.READ_ONLY:
        snapshot = ClassPolicySnapshot(
            read_only=class_value,
            reversible_write=snapshot.reversible_write,
            external_side_effect=snapshot.external_side_effect,
            high_risk=snapshot.high_risk,
        )
    elif tool_class == ActionClass.REVERSIBLE_WRITE:
        snapshot = ClassPolicySnapshot(
            read_only=snapshot.read_only,
            reversible_write=class_value,
            external_side_effect=snapshot.external_side_effect,
            high_risk=snapshot.high_risk,
        )
    elif tool_class == ActionClass.EXTERNAL_SIDE_EFFECT:
        snapshot = ClassPolicySnapshot(
            read_only=snapshot.read_only,
            reversible_write=snapshot.reversible_write,
            external_side_effect=class_value,
            high_risk=snapshot.high_risk,
        )
    elif tool_class == ActionClass.HIGH_RISK:
        snapshot = ClassPolicySnapshot(
            read_only=snapshot.read_only,
            reversible_write=snapshot.reversible_write,
            external_side_effect=snapshot.external_side_effect,
            high_risk=class_value,
        )
    return JudgmentContext(
        policy=JudgePolicyContext(
            agent_name="alice",
            persona_digest=PersonaDigest(agent_name="alice"),
            tools_md_entry=ToolPolicyEntry(
                tool_name="send_email",
                classification=tool_class,
            ),
            class_policy=snapshot,
        ),
        runtime=JudgeRuntimeConfig(
            backend_name="rules",
            timeout_ms=1000,
            budget=BudgetConfig(),
            escalation_config=EscalationConfig(),
            failure_policy={"JudgeUnavailable": "block"},
        ),
    )


def _proposal_for(args, class_=ActionClass.EXTERNAL_SIDE_EFFECT):
    """Build a minimal ActionProposal for PolicyJudge to evaluate."""
    return assemble_proposal(
        {"name": "send_email", "input": args, "id": "tc_1"},
        {"for_tool_call_id": "tc_1", "reason": "test"} if class_ != ActionClass.READ_ONLY else None,
        classification=class_,
        classification_source="tools.md",
        tool_definition_hash="tdef_x",
        actor_agent="alice",
        actor_run_id="run_1",
    )


# ──────────────────────────────────────────────────────────────────
# Protocol satisfaction + capability advertisement


class TestProtocolSurface:
    def test_satisfies_judge_backend_protocol(self):
        judge = PolicyJudge()
        assert isinstance(judge, JudgeBackend)

    def test_supported_outcomes_pr2a_set(self):
        # PR 2a ships ALLOW + BLOCK only. ESCALATE adds in PR 3.
        judge = PolicyJudge()
        assert judge.supported_outcomes() == {
            JudgmentOutcome.ALLOW,
            JudgmentOutcome.BLOCK,
        }

    def test_supports_read_audit(self):
        # Rule engine is deterministic + free — supports audit mode.
        assert PolicyJudge().supports_read_audit() is True

    def test_supports_specialist_composition(self):
        assert PolicyJudge().supports_specialist_composition() is True

    def test_close_is_idempotent(self):
        judge = PolicyJudge()
        # Should not raise on multiple invocations.
        judge.close()
        judge.close()


# ──────────────────────────────────────────────────────────────────
# Class-policy dispatch


class TestClassPolicyDispatch:
    def test_bypass_returns_allow(self):
        judge = PolicyJudge()
        proposal = _proposal_for({"to": "x@y"})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.BYPASS),
        )
        assert judgment.outcome == JudgmentOutcome.ALLOW
        assert "bypass" in judgment.reason.lower()

    def test_allow_with_audit_returns_allow_with_note(self):
        judge = PolicyJudge()
        proposal = _proposal_for({"to": "x@y"})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.ALLOW_WITH_AUDIT),
        )
        assert judgment.outcome == JudgmentOutcome.ALLOW
        assert "audit" in judgment.reason.lower()

    def test_escalate_becomes_block_in_pr2a(self):
        # Per the PR 2a deferral — class-policy ESCALATE self-maps to
        # BLOCK with the escalate_pending_polling_unimplemented reason
        # to avoid orphan PENDING files.
        judge = PolicyJudge()
        proposal = _proposal_for({"to": "x@y"})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.ESCALATE),
        )
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "escalate_pending_polling_unimplemented" in judgment.reason

    def test_judge_required_with_no_violations_returns_allow(self):
        judge = PolicyJudge()  # no write paths configured
        proposal = _proposal_for({"to": "x@y"})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        assert judgment.outcome == JudgmentOutcome.ALLOW


# ──────────────────────────────────────────────────────────────────
# Write-path enforcement parity (the adversarial review's P1 #5)


class TestWritePathEnforcement:
    def test_block_when_path_outside_allowed(self, tmp_path):
        # Allowed: tmp_path/memory only.
        judge = PolicyJudge(
            tools_md_text="",
            allowed_write_paths=[tmp_path / "memory"],
            read_only_paths=[],
        )
        # Proposal writes OUTSIDE the allowed path.
        proposal = _proposal_for(
            {"path": str(tmp_path / "elsewhere" / "x.md"), "body": "..."},
        )
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "write-path violation" in judgment.reason.lower()

    def test_block_when_path_is_read_only(self, tmp_path):
        # Allowed: tmp_path/memory; read-only: tmp_path/persona.
        memory = tmp_path / "memory"
        persona = tmp_path / "persona"
        memory.mkdir()
        persona.mkdir()
        judge = PolicyJudge(
            allowed_write_paths=[memory],
            read_only_paths=[persona],
        )
        # Attempted write into read-only path.
        proposal = _proposal_for({"path": str(persona / "IDENTITY.md")})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "read-only" in judgment.reason.lower()

    def test_allow_when_path_under_allowed(self, tmp_path):
        memory = tmp_path / "memory"
        memory.mkdir()
        judge = PolicyJudge(allowed_write_paths=[memory])
        proposal = _proposal_for({"path": str(memory / "x.md")})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_enforce_write_path_parity(self, tmp_path):
        # The judge's BLOCK must agree with handler-side
        # enforce_write_path's WritePathViolation. Pin the parity so a
        # future divergence (different path-matching logic) surfaces
        # as a test failure.
        from atomic_agents._capture import enforce_write_path
        from atomic_agents.exceptions import WritePathViolation

        memory = tmp_path / "memory"
        memory.mkdir()
        judge = PolicyJudge(allowed_write_paths=[memory])

        target = tmp_path / "outside" / "evil.md"
        # Handler-side enforcement: raises.
        with pytest.raises(WritePathViolation):
            enforce_write_path(target, [memory])

        # Judge-side enforcement: BLOCK.
        proposal = _proposal_for({"path": str(target)})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        assert judgment.outcome == JudgmentOutcome.BLOCK

    def test_skips_check_when_no_allowed_paths_configured(self, tmp_path):
        # When operator hasn't configured write_paths, the judge
        # defers to handler-side enforcement rather than blocking
        # everything. Reasonable default — judges.md (PR 3) makes the
        # policy stricter.
        judge = PolicyJudge(allowed_write_paths=[])  # nothing configured
        proposal = _proposal_for({"path": "/tmp/anywhere/x.md"})
        judgment = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_checks_all_path_keys(self, tmp_path):
        # Spec/28's PATH_ARG_KEYS — verify the judge iterates them all.
        memory = tmp_path / "memory"
        memory.mkdir()
        judge = PolicyJudge(allowed_write_paths=[memory])

        for key in (
            "path",
            "file_path",
            "target_path",
            "destination",
            "destination_path",
            "to",
            "write_to",
            "output_path",
        ):
            proposal = _proposal_for(
                {key: str(tmp_path / "elsewhere" / "x.md")},
            )
            judgment = judge.evaluate(
                proposal,
                _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
            )
            assert judgment.outcome == JudgmentOutcome.BLOCK, (
                f"key={key!r} should have BLOCK'd"
            )


# ──────────────────────────────────────────────────────────────────
# policy_version semantics


class TestPolicyVersion:
    def test_format_matches_spec(self):
        judge = PolicyJudge(tools_md_text="hello")
        # Spec/28:302 format: "tools.md@sha256:<hex>+judges.md@sha256:..."
        pv = judge.policy_version
        assert pv.startswith("tools.md@sha256:")
        assert "+judges.md@sha256:absent" in pv

    def test_changes_when_tools_md_changes(self):
        j1 = PolicyJudge(tools_md_text="v1")
        j2 = PolicyJudge(tools_md_text="v2")
        assert j1.policy_version != j2.policy_version

    def test_judges_md_absent_marker_in_pr2a(self):
        # Until PR 3's parser, judges.md hash slot is the literal
        # "absent" so audit-log readers can distinguish "no parser
        # yet" from "operator authored empty file."
        judge = PolicyJudge()
        assert "judges.md@sha256:absent" in judge.policy_version

    def test_judge_id_default(self):
        # The default judge_id is "rules-default" so operators
        # filtering audit logs by judge can pull only rule-engine
        # decisions.
        assert PolicyJudge().judge_id == "rules-default"


# ──────────────────────────────────────────────────────────────────
# Construction helper


class TestMakeDefaultPolicyJudge:
    def test_returns_policy_judge(self, tmp_path):
        judge = make_default_policy_judge(
            tools_md_text="hello",
            allowed_write_paths=[tmp_path / "x"],
            read_only_paths=[tmp_path / "y"],
        )
        assert isinstance(judge, PolicyJudge)
        assert isinstance(judge, JudgeBackend)


# ──────────────────────────────────────────────────────────────────
# Idempotency property (spec/28 conformance)


class TestIdempotency:
    def test_evaluate_does_not_mutate_proposal(self):
        judge = PolicyJudge()
        proposal = _proposal_for({"to": "x@y"})
        snapshot_before = (
            proposal.tool_name,
            proposal.classification,
            tuple(proposal.evidence),
            proposal.reason,
        )
        _ = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        snapshot_after = (
            proposal.tool_name,
            proposal.classification,
            tuple(proposal.evidence),
            proposal.reason,
        )
        assert snapshot_before == snapshot_after

    def test_two_evaluate_calls_same_outcome(self):
        judge = PolicyJudge()
        proposal = _proposal_for({"to": "x@y"})
        j1 = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        j2 = judge.evaluate(
            proposal,
            _ctx_with(ClassPolicyValue.JUDGE_REQUIRED),
        )
        # Outcomes are deterministic; reason text identical.
        assert j1.outcome == j2.outcome
        assert j1.reason == j2.reason

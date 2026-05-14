"""Unit tests for atomic_agents/judge/_revise.py.

Pure-function coverage of:
- amend_proposal: field merging + class-recomputation
- validate_amended_args: tool registration, dict shape, hash recompute
- enforce_amended_write_paths: write-path / read-only re-check
- parse_operator_amendment: YAML-block extraction + ProposalAmendment shape
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.exceptions import JudgeAmendedProposalRejected
from atomic_agents.judge import _revise
from atomic_agents.judge.proposal import compute_arguments_hash
from atomic_agents.judge.types import (
    ActionClass,
    ActionProposal,
    Evidence,
    ProposalAmendment,
    Reversibility,
)
from atomic_agents.tools import ToolDefinition, ToolRegistry


def _make_proposal(
    *,
    tool_name: str = "send_email",
    classification: ActionClass = ActionClass.EXTERNAL_SIDE_EFFECT,
    tool_arguments: dict | None = None,
) -> ActionProposal:
    args = tool_arguments if tool_arguments is not None else {"to": "x@y", "body": "hi"}
    return ActionProposal(
        tool_name=tool_name,
        tool_arguments=args,
        tool_call_id="tc_1",
        tool_definition_hash="sha256:" + "a" * 64,
        arguments_hash=compute_arguments_hash(args),
        classification=classification,
        classification_source="tools.md",
        actor_agent="caldwell",
        actor_run_id="agent_run_1",
        proposal_id="proposal_orig_001",
        proposal_ts="2026-05-13T12:00:00+00:00",
        reason="original reason",
    )


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="send_email",
            description="send",
            input_schema={"type": "object"},
            handler=lambda i: None,
            classification="external_side_effect",
        )
    )
    reg.register(
        ToolDefinition(
            name="delete_file",
            description="delete",
            input_schema={"type": "object"},
            handler=lambda i: None,
            classification="high_risk",
        )
    )
    return reg


class TestAmendProposal:
    def test_tool_arguments_amend_preserves_other_fields(self):
        original = _make_proposal()
        registry = _make_registry()
        amendment = ProposalAmendment(
            judge_note="strip attachment",
            tool_arguments={"to": "x@y", "body": "hi"},
        )
        amended = _revise.amend_proposal(
            original=original,
            amendment=amendment,
            tool_registry=registry,
            tool_classifications={"send_email": "external_side_effect"},
        )
        assert amended.tool_name == "send_email"  # unchanged
        assert amended.tool_arguments == {"to": "x@y", "body": "hi"}
        assert amended.classification == ActionClass.EXTERNAL_SIDE_EFFECT
        assert amended.reason == "original reason"  # carried verbatim
        assert amended.proposal_id != original.proposal_id  # fresh id
        assert amended.tool_call_id == original.tool_call_id  # same call

    def test_tool_name_amend_recomputes_classification_upgrade(self):
        # CLASS NON-DOWNGRADE BY EXPLOIT (spec/28:273). If amendment
        # changes tool_name to a HIGH_RISK tool, the recomputed
        # classification reflects that — the judge cannot relax it.
        original = _make_proposal(
            tool_name="send_email",
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
        )
        registry = _make_registry()
        amendment = ProposalAmendment(
            judge_note="actually delete the file",
            tool_name="delete_file",
            tool_arguments={"path": "/tmp/foo"},
        )
        amended = _revise.amend_proposal(
            original=original,
            amendment=amendment,
            tool_registry=registry,
            tool_classifications={
                "send_email": "external_side_effect",
                "delete_file": "high_risk",
            },
        )
        assert amended.tool_name == "delete_file"
        assert amended.classification == ActionClass.HIGH_RISK

    def test_evidence_appended_not_replaced(self):
        from atomic_agents.judge.types import Provenance
        from dataclasses import replace

        original = _make_proposal()
        original_with_ev = replace(
            original,
            evidence=[
                Evidence(source="note_a", claim="orig", provenance=Provenance.OBSERVED)
            ],
        )
        amendment = ProposalAmendment(
            judge_note="add evidence",
            appended_evidence=[
                Evidence(source="note_b", claim="added", provenance=Provenance.OBSERVED)
            ],
        )
        amended = _revise.amend_proposal(
            original=original_with_ev,
            amendment=amendment,
            tool_registry=_make_registry(),
            tool_classifications={"send_email": "external_side_effect"},
        )
        assert len(amended.evidence) == 2
        sources = [e.source for e in amended.evidence]
        assert "note_a" in sources
        assert "note_b" in sources

    def test_args_hash_recomputed(self):
        original = _make_proposal()
        amendment = ProposalAmendment(
            judge_note="new args",
            tool_arguments={"to": "x@y", "body": "DIFFERENT"},
        )
        amended = _revise.amend_proposal(
            original=original,
            amendment=amendment,
            tool_registry=_make_registry(),
            tool_classifications={"send_email": "external_side_effect"},
        )
        expected_hash = compute_arguments_hash({"to": "x@y", "body": "DIFFERENT"})
        assert amended.arguments_hash == expected_hash
        assert amended.arguments_hash != original.arguments_hash

    def test_reason_authorization_carried_verbatim(self):
        original = _make_proposal()
        amendment = ProposalAmendment(
            judge_note="judge changes args",
            tool_arguments={"to": "x@y"},
        )
        amended = _revise.amend_proposal(
            original=original,
            amendment=amendment,
            tool_registry=_make_registry(),
            tool_classifications={"send_email": "external_side_effect"},
        )
        # Judge cannot rewrite reason or authorization per spec/28:271.
        assert amended.reason == original.reason
        assert amended.authorization == original.authorization


class TestValidateAmendedArgs:
    def test_unknown_tool_refused(self):
        amended = _make_proposal(tool_name="nonexistent_tool")
        with pytest.raises(JudgeAmendedProposalRejected, match="not registered"):
            _revise.validate_amended_args(amended, _make_registry())

    def test_non_dict_args_refused(self):
        original = _make_proposal()
        # Build a malformed ActionProposal — bypass amend_proposal.
        # frozen dataclass; use __dict__ replace via object construction.
        from dataclasses import replace

        amended = replace(original, tool_arguments=[1, 2, 3])  # list, not dict
        with pytest.raises(JudgeAmendedProposalRejected, match="must be a dict"):
            _revise.validate_amended_args(amended, _make_registry())

    def test_happy_path_passes(self):
        amended = _make_proposal()
        _revise.validate_amended_args(amended, _make_registry())  # no raise


class TestEnforceAmendedWritePaths:
    def test_no_paths_configured_passes(self, tmp_path):
        amended = _make_proposal(tool_arguments={"path": str(tmp_path / "ok")})
        _revise.enforce_amended_write_paths(amended, write_paths=[], read_only_paths=[])

    def test_read_only_violation_refused(self, tmp_path):
        ro = tmp_path / "readonly"
        ro.mkdir()
        amended = _make_proposal(tool_arguments={"path": str(ro / "victim")})
        with pytest.raises(JudgeAmendedProposalRejected, match="read-only"):
            _revise.enforce_amended_write_paths(
                amended, write_paths=[], read_only_paths=[ro]
            )

    def test_write_path_allowlist_enforced(self, tmp_path):
        allowed = tmp_path / "writable"
        allowed.mkdir()
        amended = _make_proposal(tool_arguments={"path": "/tmp/outside_allowlist/x"})
        with pytest.raises(JudgeAmendedProposalRejected, match="outside"):
            _revise.enforce_amended_write_paths(
                amended, write_paths=[allowed], read_only_paths=[]
            )


class TestParseOperatorAmendment:
    def test_happy_path_extracts_amendment(self):
        body = (
            "resolved_at: 2026-05-13T12:00:00Z\n"
            "note: ok\n"
            "amendment:\n"
            "  ```yaml\n"
            "  judge_note: operator stripped attachment\n"
            "  tool_arguments:\n"
            "    to: x@y\n"
            "  ```\n"
        )
        amendment = _revise.parse_operator_amendment(body)
        assert amendment is not None
        assert amendment.judge_note == "operator stripped attachment"
        assert amendment.tool_arguments == {"to": "x@y"}

    def test_no_amendment_block_returns_none(self):
        body = "resolved_at: 2026-05-13T12:00:00Z\nnote: ok\n"
        assert _revise.parse_operator_amendment(body) is None

    def test_malformed_yaml_raises(self):
        body = (
            "amendment:\n"
            "  ```yaml\n"
            "  : : invalid : yaml :\n"
            "  ```\n"
        )
        with pytest.raises(JudgeAmendedProposalRejected, match="failed to parse"):
            _revise.parse_operator_amendment(body)

    def test_unknown_fields_rejected(self):
        body = (
            "amendment:\n"
            "  ```yaml\n"
            "  judge_note: ok\n"
            "  malicious_field: rce\n"
            "  ```\n"
        )
        with pytest.raises(JudgeAmendedProposalRejected, match="unknown fields"):
            _revise.parse_operator_amendment(body)

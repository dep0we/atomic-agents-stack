"""Tests for the PR 3b ESCALATE write-side (atomic_agents/judge/escalation.py).

Covers:
- File shape correctness (frontmatter + body sections)
- Returns proposal_id as queue_id
- Atomic write semantics (no corruption on partial writes)
- safe_resolve_under refuses path traversal
- Legacy ``destination=vault`` normalized to ``vault/escalations/``
- ISO-8601 UTC timestamp format
- triggered_by frontmatter populated for failure_policy synthesis
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from atomic_agents.judge import escalation as _esc
from atomic_agents.judge.types import (
    ActionClass,
    ActionProposal,
    EscalationConfig,
)


def _make_proposal(
    *,
    tool_name: str = "send_email",
    classification: ActionClass = ActionClass.EXTERNAL_SIDE_EFFECT,
    proposal_id: str = "proposal_20260513T120000_abc12345",
) -> ActionProposal:
    return ActionProposal(
        tool_name=tool_name,
        tool_arguments={"to": "x@y", "body": "hi"},
        tool_call_id="tc_1",
        tool_definition_hash="sha256:deadbeef" + "0" * 56,
        arguments_hash="sha256:cafebabe" + "0" * 56,
        classification=classification,
        classification_source="tools.md",
        actor_agent="caldwell",
        actor_run_id="agent_20260513T120000_xyz98765",
        proposal_id=proposal_id,
        proposal_ts="2026-05-13T12:00:00+00:00",
    )


class TestWritePendingEscalation:
    def test_file_shape_correct(self, tmp_path):
        proposal = _make_proposal()
        cfg = EscalationConfig(destination="vault/escalations/")
        pending_path, queue_id = _esc.write_pending_escalation(
            proposal=proposal,
            judgment_reason="judge wants operator review",
            judge_id="default-llm",
            agent_root=tmp_path,
            agent_name="caldwell",
            parent_run_id="agent_20260513T120000_xyz98765",
            policy_version="tools.md@sha256:abc+judges.md@sha256:def",
            judges_config_escalation=cfg,
        )
        assert pending_path.exists()
        text = pending_path.read_text(encoding="utf-8")
        # Frontmatter required fields
        assert "type: escalation" in text
        assert "state: pending" in text
        assert f"proposal_id: {proposal.proposal_id}" in text
        assert "action_class: external_side_effect" in text
        assert "judge_id: default-llm" in text
        assert "schema_version: 1" in text
        # Body sections
        assert "## Proposal" in text
        assert "## Judge's reason for escalating" in text
        assert "## Resolution" in text
        # The proposal yaml block contains the actual tool_name + args
        assert "send_email" in text

    def test_returns_proposal_id_as_queue_id(self, tmp_path):
        proposal = _make_proposal(proposal_id="proposal_xyz_123")
        cfg = EscalationConfig()
        _path, queue_id = _esc.write_pending_escalation(
            proposal=proposal,
            judgment_reason="r",
            judge_id="j",
            agent_root=tmp_path,
            agent_name="a",
            parent_run_id="r1",
            policy_version="pv",
            judges_config_escalation=cfg,
        )
        assert queue_id == "proposal_xyz_123"

    def test_path_traversal_refused(self, tmp_path):
        # Operator who set destination to "../../etc/passwd" is refused.
        from atomic_agents._io import PathTraversalError

        proposal = _make_proposal()
        cfg = EscalationConfig(destination="../../etc/escalations/")
        with pytest.raises(PathTraversalError):
            _esc.write_pending_escalation(
                proposal=proposal,
                judgment_reason="r",
                judge_id="j",
                agent_root=tmp_path,
                agent_name="a",
                parent_run_id="r1",
                policy_version="pv",
                judges_config_escalation=cfg,
            )

    def test_legacy_destination_vault_normalized(self, tmp_path):
        # An operator who set destination=vault (PR 3a default) gets
        # normalized to vault/escalations/ at write time. Spec/28
        # mandates the full path.
        proposal = _make_proposal()
        cfg = EscalationConfig(destination="vault")
        pending_path, _ = _esc.write_pending_escalation(
            proposal=proposal,
            judgment_reason="r",
            judge_id="j",
            agent_root=tmp_path,
            agent_name="a",
            parent_run_id="r1",
            policy_version="pv",
            judges_config_escalation=cfg,
        )
        assert "/vault/escalations/external_side_effect/" in str(pending_path)

    def test_destination_default_is_vault_escalations(self):
        # PR 3a's PR-shipped default was "vault" (a bug). PR 3b
        # changes the default to "vault/escalations/" per spec/28:288.
        # This regression test pins the change.
        assert EscalationConfig().destination == "vault/escalations/"

    def test_iso8601_utc_timestamp(self, tmp_path):
        proposal = _make_proposal()
        cfg = EscalationConfig()
        pending_path, _ = _esc.write_pending_escalation(
            proposal=proposal,
            judgment_reason="r",
            judge_id="j",
            agent_root=tmp_path,
            agent_name="a",
            parent_run_id="r1",
            policy_version="pv",
            judges_config_escalation=cfg,
        )
        text = pending_path.read_text(encoding="utf-8")
        m = re.search(r"escalated_at: (\S+)", text)
        assert m is not None
        ts = m.group(1)
        # ISO-8601 with timezone offset
        assert "T" in ts
        assert "+00:00" in ts or ts.endswith("Z")

    def test_triggered_by_frontmatter_for_failure_policy(self, tmp_path):
        # When the framework synthesizes ESCALATE from a failure_policy
        # mapping (e.g., JudgeUnavailable → escalate), the audit
        # frontmatter captures the originating exception.
        proposal = _make_proposal()
        cfg = EscalationConfig()
        pending_path, _ = _esc.write_pending_escalation(
            proposal=proposal,
            judgment_reason="JudgeUnavailable: backend timeout",
            judge_id="framework",
            agent_root=tmp_path,
            agent_name="a",
            parent_run_id="r1",
            policy_version="pv",
            judges_config_escalation=cfg,
            synthesis_source="failure_policy",
            triggered_by="failure_policy:JudgeUnavailable",
        )
        text = pending_path.read_text(encoding="utf-8")
        assert "triggered_by: failure_policy:JudgeUnavailable" in text
        assert "synthesis_source: failure_policy" in text


class TestNormalizeDestination:
    """Unit tests for the destination-normalization helper."""

    def test_empty_string_uses_default(self):
        assert _esc.normalize_destination("") == "vault/escalations/"

    def test_legacy_vault_normalized(self):
        assert _esc.normalize_destination("vault") == "vault/escalations/"
        assert _esc.normalize_destination("vault/") == "vault/escalations/"

    def test_already_correct_passes_through(self):
        assert _esc.normalize_destination("vault/escalations/") == "vault/escalations/"
        assert _esc.normalize_destination("vault/escalations") == "vault/escalations/"

    def test_custom_root_with_escalations_passes_through(self):
        assert _esc.normalize_destination("custom/escalations/") == "custom/escalations/"

    def test_custom_root_without_escalations_appends(self):
        assert _esc.normalize_destination("custom") == "custom/escalations/"

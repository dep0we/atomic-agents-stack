"""Tests for ``atomic_agents/judge/proposal.py`` — framework-side
proposal assembly + TOCTOU-defense hashing (spec/28, #112 PR 2a).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from atomic_agents.exceptions import JudgeProposalInvalid
from atomic_agents.judge.proposal import (
    DEFAULT_UNKNOWN_CLASSIFICATION,
    DEFAULT_UNKNOWN_CLASSIFICATION_SOURCE,
    FRAMEWORK_MANAGED_TOOLS,
    _proposal_id,
    _proposal_ts,
    assemble_proposal,
    compute_arguments_hash,
    compute_tool_definition_hash,
    is_framework_managed_tool,
)
from atomic_agents.judge.types import (
    ActionClass,
    Provenance,
    Reversibility,
)


# ──────────────────────────────────────────────────────────────────
# Constants


class TestConstants:
    def test_framework_managed_tools_set(self):
        # Both atomic_capture and atomic_action bypass judge dispatch.
        assert FRAMEWORK_MANAGED_TOOLS == frozenset(
            {"atomic_capture", "atomic_action"}
        )

    def test_default_classification_is_external_side_effect(self):
        # Per spec/28: safe default when no source supplies a class.
        assert DEFAULT_UNKNOWN_CLASSIFICATION == ActionClass.EXTERNAL_SIDE_EFFECT
        assert DEFAULT_UNKNOWN_CLASSIFICATION_SOURCE == "default_unknown"

    def test_is_framework_managed_tool(self):
        assert is_framework_managed_tool("atomic_capture") is True
        assert is_framework_managed_tool("atomic_action") is True
        assert is_framework_managed_tool("send_email") is False
        assert is_framework_managed_tool("") is False


# ──────────────────────────────────────────────────────────────────
# Hashing — determinism, sensitivity, cross-process


class TestArgumentsHash:
    def test_key_order_insensitive(self):
        h1 = compute_arguments_hash({"a": 1, "b": 2})
        h2 = compute_arguments_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_mutation_changes_hash(self):
        h1 = compute_arguments_hash({"path": "x"})
        h2 = compute_arguments_hash({"path": "y"})
        assert h1 != h2

    def test_empty_dict_hashes_consistently(self):
        h1 = compute_arguments_hash({})
        h2 = compute_arguments_hash({})
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_cross_process_determinism(self):
        """Hashes must match across processes — recompute in a
        subprocess and assert equality."""
        local = compute_arguments_hash({"x": 1, "y": [2, 3]})
        out = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "from atomic_agents.judge.proposal import compute_arguments_hash; "
                "print(compute_arguments_hash({'x': 1, 'y': [2, 3]}))",
            ],
            text=True,
        ).strip()
        assert local == out


class TestToolDefinitionHash:
    def test_custom_tool_includes_module_and_qualname(self):
        def my_handler(_inp):
            return None

        h1 = compute_tool_definition_hash(
            "send_email", {"type": "object"}, my_handler,
        )
        # Mutating handler module name produces a different hash.

        def my_handler_other(_inp):
            return None

        my_handler_other.__module__ = "different.module"
        h2 = compute_tool_definition_hash(
            "send_email", {"type": "object"}, my_handler_other,
        )
        assert h1 != h2

    def test_schema_change_changes_hash(self):
        def h(_):
            return None

        h1 = compute_tool_definition_hash("t", {"type": "object"}, h)
        h2 = compute_tool_definition_hash(
            "t",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            h,
        )
        assert h1 != h2

    def test_mcp_payload_uses_server_kind(self):
        h_custom = compute_tool_definition_hash("t", {"type": "object"}, None)
        h_mcp = compute_tool_definition_hash(
            "t",
            {"type": "object"},
            None,
            mcp_server="github",
            mcp_server_version="1.0",
        )
        # Different "kind" + extra fields → different hash.
        assert h_custom != h_mcp

    def test_mcp_server_version_change_changes_hash(self):
        h1 = compute_tool_definition_hash(
            "t", {"type": "object"}, None,
            mcp_server="github", mcp_server_version="1.0",
        )
        h2 = compute_tool_definition_hash(
            "t", {"type": "object"}, None,
            mcp_server="github", mcp_server_version="2.0",
        )
        assert h1 != h2


# ──────────────────────────────────────────────────────────────────
# proposal_id + proposal_ts


class TestProposalIdAndTs:
    def test_proposal_id_format(self):
        pid = _proposal_id()
        assert pid.startswith("proposal_")
        # Shape: proposal_<14-char timestamp>_<8-hex>
        parts = pid.split("_")
        assert len(parts) == 3
        assert len(parts[1]) == 15  # YYYYMMDDTHHMMSS
        assert len(parts[2]) == 8   # hex slice

    def test_proposal_id_unique(self):
        ids = {_proposal_id() for _ in range(50)}
        assert len(ids) == 50  # all distinct

    def test_proposal_ts_iso_8601(self):
        ts = _proposal_ts()
        # ISO-8601 UTC like 2026-05-13T17:00:00Z
        assert ts.endswith("Z")
        assert "T" in ts
        assert len(ts) == 20


# ──────────────────────────────────────────────────────────────────
# assemble_proposal: happy paths


def _make_tool_use(name="send_email", call_id="tc_1", args=None):
    return {"name": name, "input": args or {"to": "x@y"}, "id": call_id}


def _make_marker(call_id="tc_1", **overrides):
    base = {"for_tool_call_id": call_id}
    base.update(overrides)
    return base


class TestAssembleProposalHappyPath:
    def test_read_only_does_not_require_marker(self):
        tu = _make_tool_use("read_cal", "tc_r1", args={"date": "today"})
        proposal = assemble_proposal(
            tu,
            None,  # no marker
            classification=ActionClass.READ_ONLY,
            classification_source="tools.md",
            tool_definition_hash="tdef_x",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        assert proposal.classification == ActionClass.READ_ONLY
        assert proposal.reason is None
        assert proposal.side_channel_for_tool_call_id is None

    def test_side_effectful_with_matching_marker(self):
        tu = _make_tool_use(call_id="tc_a")
        marker = _make_marker(call_id="tc_a", reason="for the audit")
        proposal = assemble_proposal(
            tu, marker,
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
            classification_source="tools.md",
            tool_definition_hash="tdef_y",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        assert proposal.side_channel_for_tool_call_id == "tc_a"
        assert proposal.reason == "for the audit"

    def test_evidence_is_typed_into_dataclasses(self):
        tu = _make_tool_use(call_id="tc_a")
        marker = _make_marker(
            call_id="tc_a",
            evidence=[
                {
                    "source": "notes/x.md",
                    "claim": "user requested",
                    "provenance": "observed",
                },
            ],
        )
        proposal = assemble_proposal(
            tu, marker,
            classification=ActionClass.REVERSIBLE_WRITE,
            classification_source="tools.md",
            tool_definition_hash="tdef_z",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        assert len(proposal.evidence) == 1
        assert proposal.evidence[0].source == "notes/x.md"
        assert proposal.evidence[0].provenance == Provenance.OBSERVED

    def test_invalid_provenance_falls_back_to_legacy(self):
        tu = _make_tool_use(call_id="tc_a")
        marker = _make_marker(
            call_id="tc_a",
            evidence=[
                {
                    "source": "x",
                    "claim": "y",
                    "provenance": "not_a_real_value",
                },
            ],
        )
        proposal = assemble_proposal(
            tu, marker,
            classification=ActionClass.REVERSIBLE_WRITE,
            classification_source="tools.md",
            tool_definition_hash="tdef_z",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        # Falls back to LEGACY rather than dropping the evidence.
        assert proposal.evidence[0].provenance == Provenance.LEGACY

    def test_reversibility_typed_when_valid(self):
        tu = _make_tool_use(call_id="tc_a")
        marker = _make_marker(call_id="tc_a", reversibility="irreversible")
        proposal = assemble_proposal(
            tu, marker,
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
            classification_source="tools.md",
            tool_definition_hash="tdef_z",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        assert proposal.reversibility == Reversibility.IRREVERSIBLE

    def test_reversibility_invalid_becomes_none(self):
        tu = _make_tool_use(call_id="tc_a")
        marker = _make_marker(call_id="tc_a", reversibility="bogus")
        proposal = assemble_proposal(
            tu, marker,
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
            classification_source="tools.md",
            tool_definition_hash="tdef_z",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        assert proposal.reversibility is None

    def test_arguments_hash_is_computed(self):
        tu = _make_tool_use(args={"a": 1, "b": 2})
        marker = _make_marker(call_id="tc_1")
        proposal = assemble_proposal(
            tu, marker,
            classification=ActionClass.EXTERNAL_SIDE_EFFECT,
            classification_source="tools.md",
            tool_definition_hash="tdef_z",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        # Hash matches direct computation (key-order insensitive).
        assert proposal.arguments_hash == compute_arguments_hash({"b": 2, "a": 1})


# ──────────────────────────────────────────────────────────────────
# assemble_proposal: failure modes


class TestAssembleProposalFailureModes:
    def test_marker_required_for_side_effectful_tool(self):
        tu = _make_tool_use(call_id="tc_1")
        with pytest.raises(JudgeProposalInvalid, match="atomic_action.*side-channel marker"):
            assemble_proposal(
                tu, None,
                classification=ActionClass.EXTERNAL_SIDE_EFFECT,
                classification_source="tools.md",
                tool_definition_hash="tdef_z",
                actor_agent="alice",
                actor_run_id="run_1",
            )

    def test_marker_binding_mismatch_raises(self):
        tu = _make_tool_use(call_id="tc_actual")
        marker = _make_marker(call_id="tc_different")  # WRONG binding
        with pytest.raises(JudgeProposalInvalid, match="binding mismatch"):
            assemble_proposal(
                tu, marker,
                classification=ActionClass.EXTERNAL_SIDE_EFFECT,
                classification_source="tools.md",
                tool_definition_hash="tdef_z",
                actor_agent="alice",
                actor_run_id="run_1",
            )

    def test_tool_use_missing_name_raises(self):
        tu = {"input": {}, "id": "tc_1"}
        with pytest.raises(JudgeProposalInvalid, match="missing required fields"):
            assemble_proposal(
                tu, None,
                classification=ActionClass.READ_ONLY,
                classification_source="tools.md",
                tool_definition_hash="tdef_z",
                actor_agent="alice",
                actor_run_id="run_1",
            )

    def test_tool_use_missing_id_raises(self):
        tu = {"name": "send_email", "input": {}}
        with pytest.raises(JudgeProposalInvalid, match="missing required fields"):
            assemble_proposal(
                tu, None,
                classification=ActionClass.READ_ONLY,
                classification_source="tools.md",
                tool_definition_hash="tdef_z",
                actor_agent="alice",
                actor_run_id="run_1",
            )

    def test_tool_use_input_must_be_dict(self):
        tu = {"name": "send_email", "input": "not a dict", "id": "tc_1"}
        with pytest.raises(JudgeProposalInvalid, match="missing required fields"):
            assemble_proposal(
                tu, None,
                classification=ActionClass.READ_ONLY,
                classification_source="tools.md",
                tool_definition_hash="tdef_z",
                actor_agent="alice",
                actor_run_id="run_1",
            )


# ──────────────────────────────────────────────────────────────────
# assemble_proposal: optional-fields-present-on-read_only behavior


class TestReadOnlyOptionalMarker:
    def test_read_only_with_marker_uses_the_marker(self):
        # Per spec/28's audit-mode use case: read-only proposals MAY
        # include the marker; if present, fields populate.
        tu = _make_tool_use("read_cal", "tc_r1")
        marker = _make_marker(call_id="tc_r1", reason="audit-mode read")
        proposal = assemble_proposal(
            tu, marker,
            classification=ActionClass.READ_ONLY,
            classification_source="tools.md",
            tool_definition_hash="tdef_x",
            actor_agent="alice",
            actor_run_id="run_1",
        )
        assert proposal.reason == "audit-mode read"
        assert proposal.side_channel_for_tool_call_id == "tc_r1"

    def test_read_only_marker_binding_mismatch_still_raises(self):
        # Even though marker is optional for read_only, if present it
        # MUST bind correctly. A misbound marker is a structural error.
        tu = _make_tool_use("read_cal", "tc_r1")
        marker = _make_marker(call_id="tc_different")
        with pytest.raises(JudgeProposalInvalid, match="binding mismatch"):
            assemble_proposal(
                tu, marker,
                classification=ActionClass.READ_ONLY,
                classification_source="tools.md",
                tool_definition_hash="tdef_x",
                actor_agent="alice",
                actor_run_id="run_1",
            )

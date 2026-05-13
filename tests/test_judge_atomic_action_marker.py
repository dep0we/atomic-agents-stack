"""Tests for ``atomic_agents/judge/atomic_action.py`` — the side-channel
marker tool definition + extractor (spec/28, #112 PR 2a).
"""

from __future__ import annotations

import pytest

from atomic_agents.exceptions import JudgeProposalInvalid
from atomic_agents.judge.atomic_action import (
    ATOMIC_ACTION_SCHEMA,
    canonical_tool_definition,
    extract_atomic_action_markers,
)
from atomic_agents.llm.types import LLMToolDefinition


# ──────────────────────────────────────────────────────────────────
# Canonical tool definition


class TestCanonicalToolDefinition:
    def test_returns_llm_tool_definition(self):
        td = canonical_tool_definition()
        assert isinstance(td, LLMToolDefinition)

    def test_name_is_atomic_action(self):
        assert canonical_tool_definition().name == "atomic_action"

    def test_input_schema_requires_for_tool_call_id(self):
        td = canonical_tool_definition()
        assert "for_tool_call_id" in td.input_schema["required"]

    def test_input_schema_lists_actor_supplied_fields(self):
        # Per spec/28 §"Action proposal" — these are the actor-supplied
        # fields the marker carries.
        props = ATOMIC_ACTION_SCHEMA["properties"]
        for field in (
            "for_tool_call_id",
            "reason",
            "evidence",
            "authorization",
            "expected_consequence",
            "reversibility",
            "rollback_path",
            "target_audience",
        ):
            assert field in props, f"missing field: {field}"

    def test_reversibility_enum_matches_spec(self):
        # Spec/28's three reversibility values.
        enum = ATOMIC_ACTION_SCHEMA["properties"]["reversibility"]["enum"]
        assert set(enum) == {
            "reversible",
            "reversible_with_artifact",
            "irreversible",
        }

    def test_evidence_items_require_source_claim_provenance(self):
        item_schema = ATOMIC_ACTION_SCHEMA["properties"]["evidence"]["items"]
        assert set(item_schema["required"]) == {"source", "claim", "provenance"}

    def test_provenance_enum_matches_spec(self):
        prov_enum = (
            ATOMIC_ACTION_SCHEMA["properties"]["evidence"]
            ["items"]["properties"]["provenance"]["enum"]
        )
        assert set(prov_enum) == {
            "legacy",
            "observed",
            "inferred",
            "generated",
            "confirmed",
            "disputed",
            "superseded",
        }


# ──────────────────────────────────────────────────────────────────
# Extractor: happy paths


class TestExtractorHappyPath:
    def test_returns_empty_dict_when_no_markers(self):
        tool_uses = [
            {"name": "send_email", "input": {}, "id": "tc_1"},
            {"name": "atomic_capture", "input": {"type": "feedback"}, "id": "tc_2"},
        ]
        assert extract_atomic_action_markers(tool_uses) == {}

    def test_extracts_single_marker(self):
        tool_uses = [
            {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"},
            {
                "name": "atomic_action",
                "input": {"for_tool_call_id": "tc_1", "reason": "scheduled send"},
                "id": "tc_marker_1",
            },
        ]
        markers = extract_atomic_action_markers(tool_uses)
        assert set(markers.keys()) == {"tc_1"}
        assert markers["tc_1"]["reason"] == "scheduled send"

    def test_multiple_markers_with_distinct_ids(self):
        tool_uses = [
            {"name": "send_email", "input": {}, "id": "tc_a"},
            {"name": "create_pr", "input": {}, "id": "tc_b"},
            {
                "name": "atomic_action",
                "input": {"for_tool_call_id": "tc_a", "reason": "A"},
                "id": "tcm_a",
            },
            {
                "name": "atomic_action",
                "input": {"for_tool_call_id": "tc_b", "reason": "B"},
                "id": "tcm_b",
            },
        ]
        markers = extract_atomic_action_markers(tool_uses)
        assert set(markers.keys()) == {"tc_a", "tc_b"}
        assert markers["tc_a"]["reason"] == "A"
        assert markers["tc_b"]["reason"] == "B"

    def test_ignores_non_atomic_action_tool_uses(self):
        tool_uses = [
            {"name": "atomic_capture", "input": {"for_tool_call_id": "x"}, "id": "tc_cap"},
            {"name": "send_email", "input": {"for_tool_call_id": "y"}, "id": "tc_email"},
        ]
        # atomic_capture is NOT atomic_action even if it happens to carry
        # the same field — only name=="atomic_action" entries get extracted.
        assert extract_atomic_action_markers(tool_uses) == {}


# ──────────────────────────────────────────────────────────────────
# Extractor: negative paths (failure modes the framework defends)


class TestExtractorNegativePaths:
    def test_missing_for_tool_call_id_raises(self):
        tool_uses = [
            {
                "name": "atomic_action",
                "input": {"reason": "no binding"},
                "id": "tcm_1",
            },
        ]
        with pytest.raises(JudgeProposalInvalid, match="for_tool_call_id"):
            extract_atomic_action_markers(tool_uses)

    def test_empty_for_tool_call_id_raises(self):
        tool_uses = [
            {
                "name": "atomic_action",
                "input": {"for_tool_call_id": "", "reason": "empty"},
                "id": "tcm_1",
            },
        ]
        with pytest.raises(JudgeProposalInvalid, match="for_tool_call_id"):
            extract_atomic_action_markers(tool_uses)

    def test_non_string_for_tool_call_id_raises(self):
        tool_uses = [
            {
                "name": "atomic_action",
                "input": {"for_tool_call_id": 42},
                "id": "tcm_1",
            },
        ]
        with pytest.raises(JudgeProposalInvalid, match="for_tool_call_id"):
            extract_atomic_action_markers(tool_uses)

    def test_duplicate_for_tool_call_id_raises(self):
        # Spec/28: "duplicate marker for same tool_call_id → JudgeProposalInvalid"
        tool_uses = [
            {
                "name": "atomic_action",
                "input": {"for_tool_call_id": "tc_x", "reason": "first"},
                "id": "tcm_1",
            },
            {
                "name": "atomic_action",
                "input": {"for_tool_call_id": "tc_x", "reason": "second"},
                "id": "tcm_2",
            },
        ]
        with pytest.raises(JudgeProposalInvalid, match="duplicate"):
            extract_atomic_action_markers(tool_uses)

    def test_missing_input_dict_raises(self):
        tool_uses = [
            {"name": "atomic_action", "id": "tcm_1"},  # no "input" key
        ]
        with pytest.raises(JudgeProposalInvalid, match="for_tool_call_id"):
            extract_atomic_action_markers(tool_uses)

"""Strict JSON-Schema validation of amended ``tool_arguments``
(PR 5b of #112).

Covers the new ``validation_mode="strict"`` branch in
``validate_amended_args``:

- Happy path: amendment matches the registered tool's ``input_schema``.
- Missing required field → ``JudgeAmendedProposalRejected`` carrying
  the failing field's path.
- Wrong type at a leaf → ``JudgeAmendedProposalRejected``.
- Empty schema (``{}``) and ``None`` schema → no-op (no constraint).
- ``SchemaError`` (malformed schema authored by the operator) →
  ``JudgePolicyInvalid`` (operator authoring bug, not a per-amendment
  rejection).
- ``RefResolutionError`` (broken ``$ref`` inside the schema) →
  ``JudgePolicyInvalid``.

Plus weakened-mode behavior:

- Existing field-by-field checks still run.
- One-shot warning fires exactly once per agent_name. The autouse
  fixture in ``tests/conftest.py`` clears
  ``_jsonschema_warned_agents`` before each test so this dedup
  contract is testable without flake-prone module-state coupling.
"""

from __future__ import annotations

import logging

import pytest

from atomic_agents.exceptions import (
    JudgeAmendedProposalRejected,
    JudgePolicyInvalid,
)
from atomic_agents.judge import _revise
from atomic_agents.judge.proposal import compute_arguments_hash
from atomic_agents.judge.types import ActionClass, ActionProposal
from atomic_agents.tools import ToolDefinition, ToolRegistry


_SEND_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["to", "body"],
}


def _make_proposal(
    *,
    tool_name: str = "send_email",
    tool_arguments: dict | None = None,
) -> ActionProposal:
    args = tool_arguments if tool_arguments is not None else {
        "to": "x@y", "body": "hi"
    }
    return ActionProposal(
        tool_name=tool_name,
        tool_arguments=args,
        tool_call_id="tc_1",
        tool_definition_hash="sha256:" + "a" * 64,
        arguments_hash=compute_arguments_hash(args),
        classification=ActionClass.EXTERNAL_SIDE_EFFECT,
        classification_source="tools.md",
        actor_agent="caldwell",
        actor_run_id="agent_run_1",
        proposal_id="proposal_orig_001",
        proposal_ts="2026-05-14T12:00:00+00:00",
        reason="strict-test",
    )


def _make_registry(*, schema: dict | None = _SEND_EMAIL_SCHEMA) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="send_email",
            description="send",
            input_schema=schema if schema is not None else {},
            handler=lambda i: None,
            classification="external_side_effect",
        )
    )
    return reg


class TestStrictHappyPaths:
    def test_valid_amendment_passes(self):
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        _revise.validate_amended_args(
            amended, _make_registry(),
            agent_name="t", validation_mode="strict",
        )  # no raise

    def test_empty_schema_is_noop(self):
        # Tool registered with ``input_schema={}`` — strict mode must
        # treat the empty schema as "no constraint" and pass.
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        registry = _make_registry(schema={})
        _revise.validate_amended_args(
            amended, registry,
            agent_name="t", validation_mode="strict",
        )

    def test_none_schema_is_noop(self):
        # input_schema isn't enforceable in ToolDefinition (it's typed
        # as dict at the dataclass level) but a registered tool can
        # carry ``{}`` to express "no schema" — that already short-
        # circuits in the no-op path above.
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="send_email",
                description="send",
                input_schema={},  # ToolDefinition forbids None; use {}.
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )
        _revise.validate_amended_args(
            amended, registry,
            agent_name="t", validation_mode="strict",
        )


class TestStrictRejections:
    def test_missing_required_field_rejected(self):
        # ``body`` is required; amendment omits it.
        amended = _make_proposal(tool_arguments={"to": "x@y"})
        with pytest.raises(
            JudgeAmendedProposalRejected,
            match="failed jsonschema validation",
        ):
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="t", validation_mode="strict",
            )

    def test_wrong_type_rejected(self):
        # ``to`` must be a string; amendment passes a list.
        amended = _make_proposal(tool_arguments={"to": ["x@y"], "body": "hi"})
        with pytest.raises(
            JudgeAmendedProposalRejected,
            match="failed jsonschema validation",
        ):
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="t", validation_mode="strict",
            )

    def test_validation_error_carries_field_path(self):
        # Operator reading the audit trail needs to see WHERE the
        # validation failed. The "at [...]" segment exposes
        # ValidationError.absolute_path.
        amended = _make_proposal(tool_arguments={"to": 42, "body": "hi"})
        with pytest.raises(
            JudgeAmendedProposalRejected,
            match=r"at \['to'\]",
        ):
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="t", validation_mode="strict",
            )


class TestStrictPolicyInvalid:
    def test_malformed_schema_is_policy_invalid(self):
        # A SchemaError on the tool's OWN registered schema is an
        # operator authoring bug — the framework escalates to
        # JudgePolicyInvalid so failure_policy[JudgePolicyInvalid]
        # gets to decide, not failure_policy[JudgeAmendedProposalRejected].
        bad_schema = {"type": "object", "required": "not_a_list"}
        registry = _make_registry(schema=bad_schema)
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        with pytest.raises(
            JudgePolicyInvalid,
            match="input_schema is malformed",
        ):
            _revise.validate_amended_args(
                amended, registry,
                agent_name="t", validation_mode="strict",
            )

    def test_broken_ref_is_policy_invalid(self):
        # $ref pointing at an unresolvable fragment is also an
        # authoring bug.
        ref_schema = {
            "type": "object",
            "properties": {
                "to": {"$ref": "#/definitions/nonexistent"},
            },
        }
        registry = _make_registry(schema=ref_schema)
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        with pytest.raises(JudgePolicyInvalid):
            _revise.validate_amended_args(
                amended, registry,
                agent_name="t", validation_mode="strict",
            )


class TestWeakenedMode:
    def test_weakened_passes_when_strict_would_reject(self):
        # Same input that strict rejects — weakened mode must accept
        # (the field-level schema check is not performed).
        amended = _make_proposal(tool_arguments={"to": ["x@y"], "body": "hi"})
        _revise.validate_amended_args(
            amended, _make_registry(),
            agent_name="t", validation_mode="weakened",
        )

    def test_weakened_warning_fires_once_per_agent(self, caplog):
        amended = _make_proposal()
        with caplog.at_level(logging.WARNING, logger="atomic_agents.judge._revise"):
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="caldwell", validation_mode="weakened",
            )
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="caldwell", validation_mode="weakened",
            )
        warning_records = [
            r for r in caplog.records
            if "validation: weakened" in r.getMessage()
        ]
        assert len(warning_records) == 1
        assert "caldwell" in warning_records[0].getMessage()

    def test_weakened_warning_per_agent_dedup(self, caplog):
        # Two different agent_names each warn once.
        amended = _make_proposal()
        with caplog.at_level(logging.WARNING, logger="atomic_agents.judge._revise"):
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="caldwell", validation_mode="weakened",
            )
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="archer", validation_mode="weakened",
            )
        warning_records = [
            r for r in caplog.records
            if "validation: weakened" in r.getMessage()
        ]
        assert len(warning_records) == 2

    def test_weakened_is_default_when_mode_omitted(self):
        # Existing callers that don't pass validation_mode get the
        # weakened path (backward compat).
        amended = _make_proposal(tool_arguments={"to": ["x@y"], "body": "hi"})
        _revise.validate_amended_args(
            amended, _make_registry(), agent_name="t",
        )  # no raise — weakened doesn't run jsonschema

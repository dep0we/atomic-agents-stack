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


class TestStrictSchemaShapeEdgeCases:
    """/ship Step 11 adversarial P2: the strict-mode short-circuit
    must distinguish "no schema" (None or {}) from special-but-valid
    JSON-Schema shapes that the parser previously swallowed via
    truthy-falsy comparison.
    """

    def test_false_schema_rejects_every_amendment(self):
        # JSON-Schema spec: a schema value of ``False`` means "reject
        # all instances." Strict mode must surface this as a per-
        # amendment rejection (ValidationError → JudgeAmendedProposal
        # Rejected), NOT silently treat it as "no schema, skip".
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="send_email",
                description="forbidden tool",
                input_schema=False,
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        with pytest.raises(JudgeAmendedProposalRejected):
            _revise.validate_amended_args(
                amended, registry,
                agent_name="t", validation_mode="strict",
            )

    def test_list_schema_treated_as_policy_invalid(self):
        # ``[]`` is not a valid JSON-Schema; jsonschema.validate raises
        # SchemaError. Strict mode must route this through
        # JudgePolicyInvalid (operator authoring bug — the schema
        # itself is malformed), NOT silently bypass via truthy-falsy.
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="send_email",
                description="malformed",
                input_schema=[],
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        with pytest.raises(JudgePolicyInvalid, match="malformed"):
            _revise.validate_amended_args(
                amended, registry,
                agent_name="t", validation_mode="strict",
            )


class TestStrictDefensiveBranches:
    """/ship Step 9.1 specialist coverage gap-fills: the three
    defensive except branches in ``_run_strict_jsonschema_validation``
    each get a dedicated test so regressions on the descriptive error
    messages don't ship silently.
    """

    def test_runtime_typeerror_translated_to_rejection(self, monkeypatch):
        # Defensive branch: jsonschema.validate raises TypeError
        # post-load (e.g., future API rename of arguments). Framework
        # surfaces JudgeAmendedProposalRejected with descriptive text.
        import jsonschema

        def _boom(*args, **kwargs):
            raise TypeError("unexpected keyword argument 'whatever'")

        monkeypatch.setattr(jsonschema, "validate", _boom)
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        with pytest.raises(
            JudgeAmendedProposalRejected,
            match="unexpected runtime error.*TypeError",
        ):
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="t", validation_mode="strict",
            )

    def test_runtime_attributeerror_translated_to_rejection(self, monkeypatch):
        # Same defensive branch for AttributeError.
        import jsonschema

        def _boom(*args, **kwargs):
            raise AttributeError("'Validator' object has no attribute 'iter_errors'")

        monkeypatch.setattr(jsonschema, "validate", _boom)
        amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
        with pytest.raises(
            JudgeAmendedProposalRejected,
            match="unexpected runtime error.*AttributeError",
        ):
            _revise.validate_amended_args(
                amended, _make_registry(),
                agent_name="t", validation_mode="strict",
            )

    def test_runtime_import_unavailable_translated_to_rejection(self, monkeypatch):
        # Outer defensive try: ``import jsonschema`` succeeded at
        # agent-load but the runtime call discovers the module is
        # broken (e.g., a hot-swap removed it from sys.modules).
        # Surfaces JudgeAmendedProposalRejected with the
        # "runtime surface unavailable" message.
        import sys

        # Stash any cached imports so we can restore them.
        saved = {}
        for mod_name in ("jsonschema", "jsonschema.exceptions"):
            if mod_name in sys.modules:
                saved[mod_name] = sys.modules.pop(mod_name)
        # Block re-import via a finder that raises ImportError.
        class _BlockImport:
            def find_spec(self, name, *args, **kwargs):
                if name == "jsonschema":
                    raise ImportError("simulated post-load jsonschema removal")
                return None

        blocker = _BlockImport()
        sys.meta_path.insert(0, blocker)
        try:
            amended = _make_proposal(tool_arguments={"to": "x@y", "body": "hi"})
            with pytest.raises(
                JudgeAmendedProposalRejected,
                match="runtime surface unavailable.*ImportError",
            ):
                _revise.validate_amended_args(
                    amended, _make_registry(),
                    agent_name="t", validation_mode="strict",
                )
        finally:
            sys.meta_path.remove(blocker)
            for mod_name, mod in saved.items():
                sys.modules[mod_name] = mod


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

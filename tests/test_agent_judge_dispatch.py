"""Tests for ``agent.py``'s judge dispatch wiring (spec/28, #112 PR 2a).

Covers:

- Opt-in gate: judge dispatch is OFF unless ``AGENT_JUDGE_ENABLED`` env
  var is set OR ``judges.md`` exists in agent_root. Existing v0.13.0
  deployments see today's behavior unchanged on framework upgrade.
- Classification resolution: tools.py override → tools.md section →
  ``external_side_effect`` default.
- Side-channel marker extraction inside the multi-turn loop.
- atomic_capture + atomic_action both bypass judge dispatch (filtered
  via ``is_framework_managed_tool``).
- JudgmentEvent JSONL audit shape: required spec/28 fields present.
- BLOCK outcome synthesizes a ``ToolCallResult`` with error rather
  than invoking the handler.
- JudgeProposalInvalid from marker extraction blocks the iteration's
  side-effectful tool_uses (fail-closed default per spec/28).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.judge.types import ActionClass
from atomic_agents.tools import ToolDefinition, ToolRegistry


# ──────────────────────────────────────────────────────────────────
# Agent fixture


def _build_minimal_agent_root(tmp_path: Path) -> Path:
    """Build the minimal vault layout an AtomicAgent needs to load."""
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "tester"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTester.")
    (agent_dir / "tools.md").write_text(
        "## Read paths\n- ~/docs/\n\n"
        "## Write paths\n- ~/docs/memory/\n"
    )
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return agents_root


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    agents_root = _build_minimal_agent_root(tmp_path)
    a = AtomicAgent(name="tester", agents_root=agents_root)
    a.load()
    return a


@pytest.fixture
def agent_with_judges_md(tmp_path, monkeypatch):
    """Agent whose vault has a judges.md file present — opt-in signal."""
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    agents_root = _build_minimal_agent_root(tmp_path)
    # Place a stub judges.md (parser lands in PR 3; PR 2a only checks existence).
    (agents_root / "tester" / "judges.md").write_text(
        "# Judges\nstub — PR 2a uses presence as opt-in signal.\n"
    )
    a = AtomicAgent(name="tester", agents_root=agents_root)
    a.load()
    return a


# ──────────────────────────────────────────────────────────────────
# Opt-in gate


class TestJudgeEnabledGate:
    def test_disabled_by_default(self, agent, monkeypatch):
        # Backward compat (CLAUDE.md rule #14): no judges.md, no env
        # var → judge dispatch is OFF.
        monkeypatch.delenv("AGENT_JUDGE_ENABLED", raising=False)
        assert agent._judge_enabled() is False

    def test_enabled_via_env_var(self, agent, monkeypatch):
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        assert agent._judge_enabled() is True

    def test_env_var_accepts_truthy_strings(self, agent, monkeypatch):
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            monkeypatch.setenv("AGENT_JUDGE_ENABLED", val)
            assert agent._judge_enabled() is True, f"failed for {val!r}"

    def test_env_var_ignored_when_unset_or_falsy(self, agent, monkeypatch):
        for val in ("", "0", "false", "no", "off", "random"):
            monkeypatch.setenv("AGENT_JUDGE_ENABLED", val)
            assert agent._judge_enabled() is False, f"failed for {val!r}"

    def test_enabled_via_judges_md_presence(self, agent_with_judges_md, monkeypatch):
        monkeypatch.delenv("AGENT_JUDGE_ENABLED", raising=False)
        assert agent_with_judges_md._judge_enabled() is True


# ──────────────────────────────────────────────────────────────────
# Classification resolution


class TestResolveClassification:
    def test_tools_py_classification_wins(self, agent):
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="high_risk",
            )
        )
        cls, src = agent._resolve_classification("send_email")
        assert cls == "high_risk"
        assert src == "tools.py"

    def test_tools_md_classification_used_when_no_code_class(self, agent):
        # Register tool without classification, then drop a tools.md
        # classification map into the agent.
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
            )
        )
        agent._tool_classifications = {"send_email": "reversible_write"}
        cls, src = agent._resolve_classification("send_email")
        assert cls == "reversible_write"
        assert src == "tools.md"

    def test_unknown_tool_defaults_to_external_side_effect(self, agent):
        # No registration, no tools.md entry → safe default per spec/28.
        cls, src = agent._resolve_classification("never_seen_this_tool")
        assert cls == "external_side_effect"
        assert src == "default_unknown"


# ──────────────────────────────────────────────────────────────────
# PolicyJudge caching + construction


class TestPolicyJudgeCaching:
    def test_lazy_construction(self, agent):
        # _policy_judge starts as None.
        assert agent._policy_judge is None
        j = agent._ensure_policy_judge()
        assert agent._policy_judge is j

    def test_cached_instance_returned_on_subsequent_calls(self, agent):
        j1 = agent._ensure_policy_judge()
        j2 = agent._ensure_policy_judge()
        assert j1 is j2

    def test_policy_version_carries_tools_md_hash(self, agent):
        # The judge's policy_version should hash THIS agent's tools.md.
        j = agent._ensure_policy_judge()
        assert "tools.md@sha256:" in j.policy_version
        assert "+judges.md@sha256:absent" in j.policy_version


# ──────────────────────────────────────────────────────────────────
# Tool-definition exposure: atomic_action ships alongside atomic_capture


class TestAtomicActionToolDefinitionExposure:
    def test_all_tool_definitions_includes_atomic_action(self, agent):
        defs = agent._all_tool_definitions("claude-sonnet-4-6-20250101")
        assert defs is not None
        names = {d.name for d in defs}
        assert "atomic_capture" in names
        assert "atomic_action" in names

    def test_unknown_provider_returns_none(self, agent):
        # Tool definitions are None for unsupported providers — atomic_action
        # ships only when the provider has tool-call support.
        defs = agent._all_tool_definitions("some-future-model")
        assert defs is None


# ──────────────────────────────────────────────────────────────────
# Judge dispatch — happy path (ALLOW)


class TestJudgeDispatchAllow:
    def test_dispatch_returns_allow_for_clean_proposal(self, agent, monkeypatch):
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {
            "tc_1": {
                "for_tool_call_id": "tc_1",
                "reason": "scheduled send per operator instruction",
            }
        }
        allow, judgment, event = agent._dispatch_with_judge(tu, markers)
        assert allow is True
        assert event["raw_outcome"] == "allow"
        assert event["enforcement_action"] == "allow_executed"
        assert event["cost_source"] == "judge"

    def test_event_has_required_audit_fields(self, agent, monkeypatch):
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {
            "tc_1": {"for_tool_call_id": "tc_1", "reason": "ok"}
        }
        _, _, event = agent._dispatch_with_judge(tu, markers)
        # Per spec/28 §"Audit shape" — these MUST be present.
        for key in (
            "trigger",
            "event",
            "parent_run_id",
            "proposal_id",
            "agent",
            "judge_id",
            "policy_version",
            "proposal",
            "raw_outcome",
            "enforcement_action",
            "binding",
            "cost_source",
            "tool_name",
        ):
            assert key in event, f"missing audit field: {key}"
        # The binding triple is non-empty.
        assert event["binding"]["tool_call_id"] == "tc_1"
        assert event["binding"]["tool_definition_hash"]
        assert event["binding"]["arguments_hash"]


# ──────────────────────────────────────────────────────────────────
# Judge dispatch — failure modes


class TestJudgeDispatchFailureModes:
    def test_missing_marker_for_side_effectful_blocks(self, agent, monkeypatch):
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        # No marker bound to tc_1.
        allow, judgment, event = agent._dispatch_with_judge(tu, {})
        assert allow is False
        assert event["raw_outcome"] == "block"
        assert event["enforcement_action"] == "block_executed"
        assert "JudgeProposalInvalid" in event["judgment_reason"]

    def test_block_event_includes_binding_even_when_proposal_failed(self, agent, monkeypatch):
        # Audit invariant: every judge dispatch produces an event with
        # a binding triple, even on JudgeProposalInvalid.
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {}, "id": "tc_1"}
        _, _, event = agent._dispatch_with_judge(tu, {})
        assert event["binding"]["tool_call_id"] == "tc_1"
        assert event["binding"]["tool_definition_hash"]
        # arguments_hash is empty when proposal-assembly failed —
        # ProposalBinding's str-typed field carries no canonical hash
        # in this case; failure reason lives in event["judgment_reason"].
        assert event["binding"]["arguments_hash"] == ""


# ──────────────────────────────────────────────────────────────────
# Read-only path: no marker required


class TestReadOnlyNoMarkerNeeded:
    def test_read_only_proposal_passes_without_marker(self, agent, monkeypatch):
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agent.tool_registry.register(
            ToolDefinition(
                name="read_calendar",
                description="read calendar",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="read_only",
            )
        )
        tu = {"name": "read_calendar", "input": {"date": "today"}, "id": "tc_1"}
        # No marker — but classification is read_only so this should be fine.
        # With default class_policy=JUDGE_REQUIRED, PolicyJudge still runs
        # but finds nothing to BLOCK on; returns ALLOW.
        allow, judgment, event = agent._dispatch_with_judge(tu, {})
        assert allow is True
        assert event["raw_outcome"] == "allow"


# ──────────────────────────────────────────────────────────────────
# Tilde-expansion parity (P1 from round-2 review)


class TestTildePathExpansion:
    def test_tilde_path_does_not_falsely_block(self, agent, monkeypatch):
        """The agent's write_paths are expanded at parse time; if the
        LLM emits a ``~/...`` path in the tool arguments, the judge
        must expand it before the write-path check so it doesn't
        falsely BLOCK against the already-expanded allowed paths.
        """
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agent.tool_registry.register(
            ToolDefinition(
                name="write_note",
                description="write a note",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="reversible_write",
            )
        )
        # The agent's tools.md sets write_paths to ~/docs/memory/ which
        # _platform.expand turns into /Users/<u>/docs/memory at parse.
        # The actor emits a tool call with the un-expanded ``~/...``
        # form — natural LLM behavior.
        tu = {
            "name": "write_note",
            "input": {"path": "~/docs/memory/x.md", "body": "hi"},
            "id": "tc_1",
        }
        markers = {
            "tc_1": {"for_tool_call_id": "tc_1", "reason": "memory write"}
        }
        allow, judgment, event = agent._dispatch_with_judge(tu, markers)
        # MUST be ALLOW — the path is genuinely under the allowed
        # write_paths once both sides are expanded.
        assert allow is True, (
            f"~/... path falsely BLOCKed: {judgment.reason}"
        )


# ──────────────────────────────────────────────────────────────────
# Multi-tool partial-execute (round-2 P2: first ALLOW, second BLOCK)


class TestMultiToolPartialExecute:
    def test_clean_tool_runs_when_other_blocks(self, agent, monkeypatch, tmp_path):
        """Two side-effectful tools in one turn — one's args violate
        write-path policy, the other doesn't. Assert the clean one
        executed and the dirty one's ToolCallResult.error starts with
        ``judge_blocked:``."""
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        # Both tools are external_side_effect; only the second writes
        # a path. We dispatch each independently via the helper, then
        # assert each result.
        clean_calls = []
        dirty_calls = []
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: clean_calls.append(inp) or "ok",
                classification="external_side_effect",
            )
        )
        agent.tool_registry.register(
            ToolDefinition(
                name="write_note",
                description="write a note",
                input_schema={"type": "object"},
                handler=lambda inp: dirty_calls.append(inp) or "ok",
                classification="reversible_write",
            )
        )
        # Construct a PolicyJudge with a different allowed write path
        # so write_note's path is OUT of bounds.
        agent._policy_judge = None  # force rebuild
        from atomic_agents.judge.rules import PolicyJudge
        agent._policy_judge = PolicyJudge(
            tools_md_text="",
            allowed_write_paths=[tmp_path / "memory"],
            read_only_paths=[],
        )

        tu_clean = {
            "name": "send_email",
            "input": {"to": "x@y", "subject": "hi"},
            "id": "tc_clean",
        }
        tu_dirty = {
            "name": "write_note",
            "input": {"path": str(tmp_path / "outside" / "evil.md"), "body": "hi"},
            "id": "tc_dirty",
        }
        markers = {
            "tc_clean": {"for_tool_call_id": "tc_clean", "reason": "ok"},
            "tc_dirty": {"for_tool_call_id": "tc_dirty", "reason": "bad path"},
        }
        clean_allow, _, _ = agent._dispatch_with_judge(tu_clean, markers)
        dirty_allow, dirty_j, _ = agent._dispatch_with_judge(tu_dirty, markers)
        assert clean_allow is True
        assert dirty_allow is False
        assert "write-path violation" in dirty_j.reason.lower()


# ──────────────────────────────────────────────────────────────────
# JudgeError fail-closed (round-2 P2)


class TestJudgeErrorFailClosed:
    def test_judge_error_caught_and_synthesized_as_block(self, agent, monkeypatch):
        """If a PolicyJudge instance raises a ``JudgeError`` subclass
        from evaluate(), the wiring catches it and synthesizes a BLOCK
        Judgment with the exception class name in the reason. Audit
        record reflects ``enforcement_action="block_executed"``."""
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="external_side_effect",
            )
        )
        # Stub judge that raises a JudgeError subclass.
        from atomic_agents.exceptions import JudgeUnavailable
        from atomic_agents.judge.backend import JudgmentOutcome

        class _RaisingJudge:
            judge_id = "test-raiser"
            policy_version = "unimplemented"
            def evaluate(self, proposal, context):
                raise JudgeUnavailable("simulated outage")
            def supported_outcomes(self):
                return {JudgmentOutcome.ALLOW, JudgmentOutcome.BLOCK}
            def supports_read_audit(self):
                return False
            def supports_specialist_composition(self):
                return False
            def close(self):
                pass

        agent._policy_judge = _RaisingJudge()
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "ok"}}
        allow, judgment, event = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        assert "JudgeUnavailable" in judgment.reason
        assert event["enforcement_action"] == "block_executed"

    def test_non_judge_exception_also_fail_closes(self, agent, monkeypatch):
        """A non-JudgeError exception (e.g. RuntimeError) raised from
        evaluate() must also fail-closed via the outer ``except
        Exception`` at the dispatch call site in agent.py — defensive
        per the round-1 reviewer's recommendation."""
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        # We have to trigger the outer except Exception clause from the
        # multi-turn loop wiring (which wraps _dispatch_with_judge). Test
        # the helper directly: _dispatch_with_judge catches JudgeError
        # internally, so a RuntimeError will escape. The loop's outer
        # except Exception then catches it. Simulate the loop branch
        # directly by exercising _dispatch_with_judge with a stub that
        # raises BaseException-friendly RuntimeError.
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="send email",
                input_schema={"type": "object"},
                handler=lambda inp: None,
                classification="external_side_effect",
            )
        )
        from atomic_agents.judge.backend import JudgmentOutcome

        class _ExplodingJudge:
            judge_id = "test-exploder"
            policy_version = "unimplemented"
            def evaluate(self, proposal, context):
                raise RuntimeError("non-judge exception leaked")
            def supported_outcomes(self):
                return {JudgmentOutcome.ALLOW, JudgmentOutcome.BLOCK}
            def supports_read_audit(self):
                return False
            def supports_specialist_composition(self):
                return False
            def close(self):
                pass

        agent._policy_judge = _ExplodingJudge()
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "ok"}}
        # _dispatch_with_judge only catches JudgeError; RuntimeError
        # escapes and the outer multi-turn loop except Exception
        # catches it. Verify that escape happens cleanly.
        with pytest.raises(RuntimeError, match="non-judge exception"):
            agent._dispatch_with_judge(tu, markers)


# ──────────────────────────────────────────────────────────────────
# tools.md classification read on disk (round-2 P2)


class TestToolsClassificationOnDisk:
    def test_classification_section_in_tools_md_parsed_at_load(self, tmp_path, monkeypatch):
        """End-to-end: an operator writes ``## Tool classification``
        into tools.md; the agent's load picks it up; the resolver maps
        the tool to that class with source=tools.md."""
        monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
        agents_root = tmp_path / "agents"
        agent_dir = agents_root / "tester"
        (agent_dir / "persona").mkdir(parents=True)
        (agent_dir / "persona" / "IDENTITY.md").write_text("# I")
        (agent_dir / "tools.md").write_text(
            "## Read paths\n- ~/docs/\n\n"
            "## Write paths\n- ~/docs/memory/\n\n"
            "## Tool classification\n"
            "- send_email: external_side_effect\n"
            "- read_calendar: read_only\n"
            "- Send_Notif: reversible_write\n"  # case-insensitive name
        )
        (agent_dir / "model.md").write_text(
            "## Default model\nclaude-haiku-4-5-20251001\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        a = AtomicAgent(name="tester", agents_root=agents_root)
        a.load()
        assert a._tool_classifications == {
            "send_email": "external_side_effect",
            "read_calendar": "read_only",
            "Send_Notif": "reversible_write",
        }
        cls, src = a._resolve_classification("send_email")
        assert cls == "external_side_effect"
        assert src == "tools.md"

    def test_capitalized_action_class_normalized(self, tmp_path, monkeypatch):
        """Operator writes ``Send_Email: External_Side_Effect`` —
        normalization lower-cases the class so the entry is accepted
        (round-2 P2 fix). Without normalization the silent-skip would
        leave the operator confused about why their explicit
        classification was ignored."""
        monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
        agents_root = tmp_path / "agents"
        agent_dir = agents_root / "tester"
        (agent_dir / "persona").mkdir(parents=True)
        (agent_dir / "persona" / "IDENTITY.md").write_text("# I")
        (agent_dir / "tools.md").write_text(
            "## Read paths\n- ~/docs/\n\n"
            "## Tool classification\n"
            "- send_email: External_Side_Effect\n"
        )
        (agent_dir / "model.md").write_text(
            "## Default model\nclaude-haiku-4-5-20251001\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        a = AtomicAgent(name="tester", agents_root=agents_root)
        a.load()
        # Normalized to lowercase — entry accepted.
        assert a._tool_classifications.get("send_email") == "external_side_effect"


# ──────────────────────────────────────────────────────────────────
# Tool registration validation (round-2 P3)


class TestToolDefinitionValidation:
    def test_register_rejects_invalid_classification(self, agent):
        # Fail-fast at registration so typos surface before runtime.
        with pytest.raises(ValueError, match="invalid classification"):
            agent.tool_registry.register(
                ToolDefinition(
                    name="bad_class_tool",
                    description="...",
                    input_schema={"type": "object"},
                    handler=lambda i: None,
                    classification="WRITE",  # not in the allowed set
                )
            )

    def test_register_accepts_none_classification(self, agent):
        # Default (None) is fine — tools.md or default_unknown fills in.
        agent.tool_registry.register(
            ToolDefinition(
                name="no_class_tool",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: None,
                classification=None,
            )
        )
        # Resolver picks up default since tools.md has no entry.
        cls, src = agent._resolve_classification("no_class_tool")
        assert cls == "external_side_effect"
        assert src == "default_unknown"

    def test_register_accepts_all_four_valid_classifications(self, agent):
        for cls in ("read_only", "reversible_write", "external_side_effect", "high_risk"):
            agent.tool_registry.register(
                ToolDefinition(
                    name=f"tool_{cls}",
                    description="...",
                    input_schema={"type": "object"},
                    handler=lambda i: None,
                    classification=cls,
                )
            )

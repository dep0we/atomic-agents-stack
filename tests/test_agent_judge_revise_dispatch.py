"""Judge-driven REVISE dispatch tests.

When the ensemble's first judge returns Judgment(outcome=REVISE,
amendment=...), the framework builds an amended proposal, re-validates,
and runs a second judgment via _run_ensemble(revise_iteration=1).
Bounded at max_revise_iterations=1.
"""

from __future__ import annotations

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.judge.backend import Judgment, JudgmentOutcome
from atomic_agents.judge.types import ProposalAmendment
from atomic_agents.tools import ToolDefinition


@pytest.fixture
def revise_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "tester"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# I")
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (agent_dir / "judges.md").write_text(
        "```yaml\n"
        "class_policy:\n"
        "  external_side_effect: judge_required\n"
        "```\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    agent = AtomicAgent(name="tester", agents_root=agents_root)
    agent.load()
    agent.tool_registry.register(
        ToolDefinition(
            name="send_email",
            description="...",
            input_schema={"type": "object"},
            handler=lambda i: None,
            classification="external_side_effect",
        )
    )
    return agent


def _stub_revise_then_allow(judge, amendment):
    """Replace judge.evaluate so the first call returns REVISE with the
    given amendment, the second returns ALLOW."""
    call_count = {"n": 0}
    real_id = judge.judge_id
    real_pv = judge.policy_version

    def evaluate(proposal, context):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return Judgment(
                outcome=JudgmentOutcome.REVISE,
                reason="please strip attachment",
                judge_id=real_id,
                policy_version=real_pv,
                amendment=amendment,
            )
        return Judgment(
            outcome=JudgmentOutcome.ALLOW,
            reason="amended proposal looks clean",
            judge_id=real_id,
            policy_version=real_pv,
        )

    return evaluate, call_count


class TestJudgeDrivenRevise:
    def test_revise_then_allow_executes_amended_action(self, revise_agent, monkeypatch):
        agent = revise_agent
        amendment = ProposalAmendment(
            judge_note="strip attachment",
            tool_arguments={"to": "x@y", "body": "hi"},
        )
        evaluate, calls = _stub_revise_then_allow(
            agent._ensure_policy_judge(), amendment
        )
        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)

        tu = {"name": "send_email", "input": {"to": "x@y", "body": "hi", "attachment": "secret.pdf"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "send"}}
        allow, events, queue_id = agent._dispatch_with_judge(tu, markers)
        # Second judgment ALLOWed → final allow=True; queue_id=None
        # (no escalation), and the audit trail records both judgments.
        assert allow is True
        assert queue_id is None
        assert calls["n"] == 2
        # Last event has revise_executed enforcement; first event has
        # revise_pending_second_judgment + amendment payload.
        first_event = events[0]
        assert first_event["enforcement_action"] == "revise_pending_second_judgment"
        assert "amendment" in first_event
        last_event = events[-1]
        assert last_event["enforcement_action"] == "revise_executed"
        assert last_event["revise_iteration"] == 1
        assert last_event["original_proposal_id"] is not None

    def test_revise_then_block_refuses_execution(self, revise_agent, monkeypatch):
        agent = revise_agent
        amendment = ProposalAmendment(
            judge_note="try again",
            tool_arguments={"to": "x@y"},
        )
        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version
        call_count = {"n": 0}

        def evaluate(proposal, context):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return Judgment(
                    outcome=JudgmentOutcome.REVISE,
                    reason="please retry",
                    judge_id=real_id,
                    policy_version=real_pv,
                    amendment=amendment,
                )
            return Judgment(
                outcome=JudgmentOutcome.BLOCK,
                reason="second judgment also bad",
                judge_id=real_id,
                policy_version=real_pv,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_2"}
        markers = {"tc_2": {"for_tool_call_id": "tc_2", "reason": "send"}}
        allow, events, _q = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        # Last event is the second judgment BLOCK with original_proposal_id linkage.
        assert events[-1]["judgment_outcome"] == "block"
        assert events[-1]["original_proposal_id"] is not None

    def test_revise_loop_exhausted_on_double_revise(self, revise_agent, monkeypatch):
        # Both judgments return REVISE — spec/28:276 caps at 1 revise
        # iteration. The second REVISE → BLOCK with reason
        # revise_loop_exhausted.
        agent = revise_agent
        amendment = ProposalAmendment(
            judge_note="keep revising",
            tool_arguments={"to": "x@y"},
        )
        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version

        def evaluate(proposal, context):
            return Judgment(
                outcome=JudgmentOutcome.REVISE,
                reason="revise me again",
                judge_id=real_id,
                policy_version=real_pv,
                amendment=amendment,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_3"}
        markers = {"tc_3": {"for_tool_call_id": "tc_3", "reason": "send"}}
        allow, events, _q = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        # The second-judgment REVISE outcome triggers loop_exhausted.
        loop_event = next(
            e for e in events
            if e["enforcement_action"] == "revise_loop_exhausted_blocked"
        )
        assert loop_event["revise_iteration"] == 1
        assert "revise_loop_exhausted" in loop_event["judgment_reason"]

    def test_revise_with_no_amendment_refused(self, revise_agent, monkeypatch):
        # Judge advertises REVISE outcome but returns no amendment
        # payload — framework refuses with revise_invalid_amendment.
        agent = revise_agent
        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version

        def evaluate(proposal, context):
            return Judgment(
                outcome=JudgmentOutcome.REVISE,
                reason="here is no amendment lol",
                judge_id=real_id,
                policy_version=real_pv,
                amendment=None,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_4"}
        markers = {"tc_4": {"for_tool_call_id": "tc_4", "reason": "send"}}
        allow, events, _q = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        assert events[-1]["enforcement_action"] == "revise_invalid_amendment"


class TestRound1P1Fix3:
    """Regression test pinning P1 #3: second-judgment ESCALATE PENDING
    carries revised_from_proposal_id linkage in frontmatter."""

    def test_revise_then_escalate_writes_revised_from_proposal_id(
        self, revise_agent, monkeypatch
    ):
        agent = revise_agent
        amendment = ProposalAmendment(
            judge_note="try this",
            tool_arguments={"to": "x@y"},
        )
        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version
        call_count = {"n": 0}

        def evaluate(proposal, context):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return Judgment(
                    outcome=JudgmentOutcome.REVISE,
                    reason="please amend",
                    judge_id=real_id,
                    policy_version=real_pv,
                    amendment=amendment,
                )
            # Second judgment escalates → PENDING written with linkage.
            return Judgment(
                outcome=JudgmentOutcome.ESCALATE,
                reason="amended also needs operator eyes",
                judge_id=real_id,
                policy_version=real_pv,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_p1_3"}
        markers = {"tc_p1_3": {"for_tool_call_id": "tc_p1_3", "reason": "send"}}
        allow, events, queue_id = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        assert queue_id is not None
        # The PENDING file frontmatter should carry revised_from_proposal_id
        # pointing back to the original proposal.
        original_event = events[0]
        original_proposal_id = original_event["proposal_id"]
        pending_path = (
            agent.agent_root
            / "vault/escalations/external_side_effect"
            / f"{queue_id}.md"
        )
        assert pending_path.exists()
        text = pending_path.read_text(encoding="utf-8")
        assert f"revised_from_proposal_id: {original_proposal_id}" in text


class TestValidationModeWiring:
    """End-to-end: ``judges.md`` ``validation:`` field threads to the
    judge dispatch's ``validate_amended_args`` call (PR 5b of #112).

    Three scenarios pin the contract:

    1. ``validation: strict`` + amendment that fails schema → BLOCK
       with ``revise_invalid_amendment`` enforcement_action.
    2. ``validation: strict`` configured WITHOUT the [validation]
       extra installed → ``JudgePolicyInvalid`` at agent-load time
       (the LOUD-at-load gate operators see when they flip strict
       without first installing the extra).
    3. Legacy (no ``validation:`` key) → behavior identical to
       pre-PR-5b: weakened mode, one-shot warning, no schema check.
    """

    def test_strict_amendment_failing_schema_blocks(self, tmp_path, monkeypatch):
        from atomic_agents.agent import AtomicAgent
        from atomic_agents.judge.backend import Judgment, JudgmentOutcome

        monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")
        agents_root = tmp_path / "agents"
        agent_dir = agents_root / "tester_strict"
        (agent_dir / "persona").mkdir(parents=True)
        (agent_dir / "persona" / "IDENTITY.md").write_text("# I")
        (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
        (agent_dir / "model.md").write_text(
            "## Default model\nclaude-haiku-4-5-20251001\n"
        )
        (agent_dir / "judges.md").write_text(
            "```yaml\n"
            "validation: strict\n"
            "class_policy:\n"
            "  external_side_effect: judge_required\n"
            "```\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        agent = AtomicAgent(name="tester_strict", agents_root=agents_root)
        agent.load()
        # Confirm validation field threaded through to the config.
        assert agent.judges_config is not None
        assert agent.judges_config.validation == "strict"
        # Tool's schema requires {to, body}; amendment will drop `body`.
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["to", "body"],
                },
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )

        amendment = ProposalAmendment(
            judge_note="strip body",
            tool_arguments={"to": "x@y"},  # missing required `body`
        )
        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version

        def evaluate(proposal, context):
            return Judgment(
                outcome=JudgmentOutcome.REVISE,
                reason="strip body",
                judge_id=real_id,
                policy_version=real_pv,
                amendment=amendment,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        tu = {
            "name": "send_email",
            "input": {"to": "x@y", "body": "hi"},
            "id": "tc_strict_1",
        }
        markers = {"tc_strict_1": {"for_tool_call_id": "tc_strict_1", "reason": "send"}}
        allow, events, _q = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        # Strict mode caught the schema violation as
        # revise_invalid_amendment — proves the wiring (parser →
        # JudgesConfig.validation → validate_amended_args) end-to-end.
        invalid_event = events[-1]
        assert invalid_event["enforcement_action"] == "revise_invalid_amendment"
        assert "failed jsonschema validation" in invalid_event["judgment_reason"]

    def test_strict_without_extra_raises_at_load(self, tmp_path, monkeypatch):
        from atomic_agents.agent import AtomicAgent
        from atomic_agents import judges_md
        from atomic_agents.exceptions import JudgePolicyInvalid

        monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
        monkeypatch.setenv("AGENT_JUDGE_ENABLED", "1")

        # Simulate the [validation] extra not installed — patch the
        # parser's import probe to raise.
        def _no_jsonschema():
            raise JudgePolicyInvalid(
                "judges.md sets ``validation: strict`` but the "
                "``jsonschema`` package is not importable. Install "
                "the ``[validation]`` extra BEFORE setting "
                "``validation: strict`` in judges.md."
            )

        monkeypatch.setattr(judges_md, "_check_jsonschema_importable", _no_jsonschema)

        agents_root = tmp_path / "agents"
        agent_dir = agents_root / "tester_no_extra"
        (agent_dir / "persona").mkdir(parents=True)
        (agent_dir / "persona" / "IDENTITY.md").write_text("# I")
        (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
        (agent_dir / "model.md").write_text(
            "## Default model\nclaude-haiku-4-5-20251001\n"
        )
        (agent_dir / "judges.md").write_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        with pytest.raises(JudgePolicyInvalid, match="not importable"):
            AtomicAgent(name="tester_no_extra", agents_root=agents_root)

    def test_legacy_no_validation_key_uses_weakened(self, revise_agent, monkeypatch):
        # The ``revise_agent`` fixture's judges.md doesn't set
        # ``validation:`` — config defaults to weakened. End-to-end
        # behavior should match pre-PR-5b: amendments that would
        # fail strict schema STILL execute (weakened doesn't enforce
        # input_schema), one-shot warning fires.
        from atomic_agents.judge.backend import Judgment, JudgmentOutcome

        agent = revise_agent
        assert agent.judges_config is not None
        assert agent.judges_config.validation == "weakened"
        # Re-register tool with a stricter schema than the amendment
        # would satisfy under strict mode.
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={
                    "type": "object",
                    "properties": {"to": {"type": "string"}},
                    "required": ["to", "body"],
                },
                handler=lambda i: None,
                classification="external_side_effect",
            ),
            allow_overwrite=True,
        )
        # Amendment omits ``body`` — would fail strict, must pass weakened.
        amendment = ProposalAmendment(
            judge_note="weak-test",
            tool_arguments={"to": "x@y"},
        )
        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version
        call_count = {"n": 0}

        def evaluate(proposal, context):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return Judgment(
                    outcome=JudgmentOutcome.REVISE,
                    reason="strip body",
                    judge_id=real_id,
                    policy_version=real_pv,
                    amendment=amendment,
                )
            return Judgment(
                outcome=JudgmentOutcome.ALLOW,
                reason="amended ok",
                judge_id=real_id,
                policy_version=real_pv,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        tu = {
            "name": "send_email",
            "input": {"to": "x@y", "body": "hi"},
            "id": "tc_legacy_1",
        }
        markers = {"tc_legacy_1": {"for_tool_call_id": "tc_legacy_1", "reason": "send"}}
        allow, events, _q = agent._dispatch_with_judge(tu, markers)
        # Weakened path lets the amendment through; second judgment
        # ALLOWs → final allow=True.
        assert allow is True
        assert events[-1]["enforcement_action"] == "revise_executed"


class TestReviseInvalidAmendmentRejected:
    def test_unknown_tool_in_amendment_refused(self, revise_agent, monkeypatch):
        # Amendment swaps tool_name to a tool that doesn't exist in
        # the registry — validate_amended_args raises.
        agent = revise_agent
        amendment = ProposalAmendment(
            judge_note="swap to unknown",
            tool_name="ghost_tool",
            tool_arguments={"foo": "bar"},
        )
        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version

        def evaluate(proposal, context):
            return Judgment(
                outcome=JudgmentOutcome.REVISE,
                reason="swap tool",
                judge_id=real_id,
                policy_version=real_pv,
                amendment=amendment,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_5"}
        markers = {"tc_5": {"for_tool_call_id": "tc_5", "reason": "send"}}
        allow, events, _q = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        assert events[-1]["enforcement_action"] == "revise_invalid_amendment"
        assert "not registered" in events[-1]["judgment_reason"]

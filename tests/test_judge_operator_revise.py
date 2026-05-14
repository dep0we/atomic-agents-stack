"""Operator-driven REVISE: PENDING resolution flow.

When an operator writes ``### Revised by <op>`` with an embedded
amendment YAML block to a PENDING file, the framework parses the
amendment, validates it, and (for high_risk) re-judges via a fresh
ensemble. Non-high_risk action classes execute on validation success
alone.

The class-gate (high_risk vs other) is keyed on the **amended**
classification, not the original — operators cannot upgrade tool_name
to a high_risk tool and skip the re-judge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.tools import ToolDefinition


def _build_agent(tmp_path, monkeypatch, *, class_policy: str = "judge_required"):
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
        f"class_policy:\n"
        f"  external_side_effect: {class_policy}\n"
        f"  high_risk: judge_required\n"
        "escalation:\n"
        "  destination: vault/escalations/\n"
        "  resolution_poll_cycle_seconds: 0\n"
        "```\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    agent = AtomicAgent(name="tester", agents_root=agents_root)
    agent.load()
    return agent


def _write_pending_with_operator_revise(
    agent, *, tool_arguments: dict, classification: str = "external_side_effect"
):
    """Write a fresh PENDING file by simulating a judge ESCALATE, then
    append a Revised block to it.
    """
    # Register a tool that maps to the requested classification.
    executions: list[dict] = []
    agent.tool_registry.register(
        ToolDefinition(
            name="send_email",
            description="...",
            input_schema={"type": "object"},
            handler=lambda i: executions.append(dict(i)) or {"sent": True},
            classification=classification,
        ),
        allow_overwrite=True,
    )
    # Force ESCALATE via class_policy=escalate path.
    from atomic_agents.judge.types import EscalationConfig

    # Use the dispatch's class_policy=escalate synth by setting the
    # class to escalate, but the fixture uses judge_required. Bypass:
    # directly call write_pending_escalation from escalation.py.
    from atomic_agents.judge import escalation as _esc
    from atomic_agents.judge.proposal import (
        compute_arguments_hash,
        compute_tool_definition_hash,
    )
    from atomic_agents.judge.types import ActionClass, ActionProposal

    registered = agent.tool_registry.get("send_email")
    tdef_hash = compute_tool_definition_hash(
        "send_email", registered.input_schema, registered.handler
    )
    args_hash = compute_arguments_hash(tool_arguments)
    cls_enum = ActionClass(classification)
    proposal = ActionProposal(
        tool_name="send_email",
        tool_arguments=tool_arguments,
        tool_call_id="tc_orig",
        tool_definition_hash=tdef_hash,
        arguments_hash=args_hash,
        classification=cls_enum,
        classification_source="tools.md",
        actor_agent="tester",
        actor_run_id="actor_run_xyz",
        proposal_id="proposal_orig_zzz",
        proposal_ts="2026-05-13T12:00:00+00:00",
    )
    cfg = (
        agent.judges_config.escalation
        if agent.judges_config is not None
        else EscalationConfig()
    )
    pending_path, queue_id = _esc.write_pending_escalation(
        proposal=proposal,
        judgment_reason="judge wanted operator review",
        judge_id="default-llm",
        agent_root=agent.agent_root,
        agent_name=agent.name,
        parent_run_id=proposal.actor_run_id,
        policy_version="pv-test",
        judges_config_escalation=cfg,
    )
    return pending_path, queue_id, executions


def _append_operator_revise_block(pending_path: Path, amendment_yaml: str):
    text = pending_path.read_text(encoding="utf-8")
    text = text.replace("state: pending", "state: resolved")
    text = text + (
        "\n### Revised by alice\n"
        "resolved_at: 2026-05-13T12:05:00+00:00\n"
        "note: operator amendment\n"
        f"amendment:\n  ```yaml\n{amendment_yaml}\n  ```\n"
    )
    pending_path.write_text(text, encoding="utf-8")


class TestOperatorReviseNonHighRisk:
    def test_non_high_risk_skips_re_judge_executes(self, tmp_path, monkeypatch):
        # external_side_effect (non-high_risk) → schema/policy
        # validation only; no fresh ensemble re-judge.
        agent = _build_agent(tmp_path, monkeypatch)
        pending, queue_id, executions = _write_pending_with_operator_revise(
            agent,
            tool_arguments={"to": "x@y", "body": "original"},
            classification="external_side_effect",
        )
        _append_operator_revise_block(
            pending,
            "  judge_note: operator stripped attachment\n"
            "  tool_arguments:\n"
            "    to: x@y\n"
            "    body: amended",
        )
        events = agent.poll_escalations()
        assert len(events) == 1
        # Handler was called with AMENDED args, not original.
        assert len(executions) == 1
        assert executions[0]["body"] == "amended"

    def test_non_high_risk_audit_re_judged_false(self, tmp_path, monkeypatch):
        agent = _build_agent(tmp_path, monkeypatch)
        pending, queue_id, _ = _write_pending_with_operator_revise(
            agent,
            tool_arguments={"to": "x@y"},
            classification="external_side_effect",
        )
        _append_operator_revise_block(
            pending,
            "  judge_note: minor\n"
            "  tool_arguments:\n"
            "    to: x@y",
        )
        agent.poll_escalations()
        # Find the operator_revise_executed audit line.
        log_dir = agent.agent_root / "log"
        executed_records = []
        for log_file in log_dir.rglob("*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("trigger") == "escalation_operator_revise_executed":
                    executed_records.append(rec)
        assert len(executed_records) == 1
        assert executed_records[0]["re_judged"] is False
        assert executed_records[0]["escalation_queue_id"] == queue_id


class TestOperatorReviseHighRisk:
    def test_high_risk_re_judges_via_fresh_ensemble(self, tmp_path, monkeypatch):
        # high_risk class triggers a fresh ensemble re-judgment. Stub
        # PolicyJudge.evaluate to BLOCK so we verify re-judge fired
        # AND the BLOCK prevents execution.
        agent = _build_agent(tmp_path, monkeypatch)
        pending, queue_id, executions = _write_pending_with_operator_revise(
            agent,
            tool_arguments={"path": "/tmp/foo"},
            classification="high_risk",
        )
        _append_operator_revise_block(
            pending,
            "  judge_note: try this path\n"
            "  tool_arguments:\n"
            "    path: /tmp/safer",
        )

        # Stub the policy judge to BLOCK on the re-judge call.
        from atomic_agents.judge.backend import Judgment, JudgmentOutcome

        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version

        def evaluate(proposal, context):
            return Judgment(
                outcome=JudgmentOutcome.BLOCK,
                reason="re-judge says no",
                judge_id=real_id,
                policy_version=real_pv,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        agent.poll_escalations()

        # Handler NOT called (re-judge BLOCKed).
        assert executions == []
        # Re-judge audit recorded.
        log_dir = agent.agent_root / "log"
        rejudge_records = []
        for log_file in log_dir.rglob("*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("trigger") == "escalation_operator_revise_re_judge":
                    rejudge_records.append(rec)
        assert len(rejudge_records) == 1
        assert rejudge_records[0]["re_judged"] is True

    def test_class_upgrade_via_operator_swap_triggers_re_judge(self, tmp_path, monkeypatch):
        # Codex round-1 P1-4 regression: operator swaps tool_name to
        # upgrade reversible_write → high_risk; the recomputed class
        # MUST trigger the re-judge gate (not the original class).
        agent = _build_agent(tmp_path, monkeypatch)
        # Register both tools.
        ran_high_risk: list[dict] = []
        agent.tool_registry.register(
            ToolDefinition(
                name="delete_file",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: ran_high_risk.append(dict(i)),
                classification="high_risk",
            )
        )
        # Original proposal is for send_email (external_side_effect).
        pending, queue_id, _ = _write_pending_with_operator_revise(
            agent,
            tool_arguments={"to": "x@y"},
            classification="external_side_effect",
        )
        # Operator amends to call delete_file instead.
        _append_operator_revise_block(
            pending,
            "  judge_note: escalating via class upgrade\n"
            "  tool_name: delete_file\n"
            "  tool_arguments:\n"
            "    path: /tmp/foo",
        )

        # Stub PolicyJudge to BLOCK on the re-judge so the test
        # asserts re-judge actually fired (not just validation-only).
        from atomic_agents.judge.backend import Judgment, JudgmentOutcome

        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version

        def evaluate(proposal, context):
            return Judgment(
                outcome=JudgmentOutcome.BLOCK,
                reason="class upgraded — re-judge BLOCKs",
                judge_id=real_id,
                policy_version=real_pv,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        agent.poll_escalations()

        # delete_file NOT executed — re-judge BLOCKed it.
        assert ran_high_risk == []


class TestRound1P1Fixes:
    """Regression tests pinning the 3 round-1 P1 audit-shape fixes."""

    def test_p1_1_resolved_line_uses_pending_not_executed(self, tmp_path, monkeypatch):
        # P1 #1: the escalation_resolved audit line for OPERATOR_REVISED
        # uses enforcement=operator_revise_pending (intent recorded);
        # the actual operator_revise_executed line only appears AFTER
        # the handler runs (in escalation_operator_revise_executed
        # trigger).
        agent = _build_agent(tmp_path, monkeypatch)
        pending, queue_id, _ = _write_pending_with_operator_revise(
            agent,
            tool_arguments={"to": "x@y"},
            classification="external_side_effect",
        )
        _append_operator_revise_block(
            pending,
            "  judge_note: minor\n"
            "  tool_arguments:\n"
            "    to: x@y",
        )
        agent.poll_escalations()

        log_dir = agent.agent_root / "log"
        resolved_records: list[dict] = []
        executed_records: list[dict] = []
        for log_file in log_dir.rglob("*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("trigger") == "escalation_resolved":
                    resolved_records.append(rec)
                elif rec.get("trigger") == "escalation_operator_revise_executed":
                    executed_records.append(rec)
        # The resolved line carries pending intent, not executed.
        assert len(resolved_records) == 1
        assert resolved_records[0]["enforcement_action"] == "operator_revise_pending"
        # The executed line comes from _process_operator_revise AFTER
        # the handler ran.
        assert len(executed_records) == 1

    def test_p1_2_re_judge_events_link_to_actor_run(self, tmp_path, monkeypatch):
        # P1 #2: re-judge audit events have parent_run_id pointing
        # to the original actor's run, NOT the poller agent's run.
        # cost_source discipline + forensic chain require this.
        agent = _build_agent(tmp_path, monkeypatch)
        pending, queue_id, _ = _write_pending_with_operator_revise(
            agent,
            tool_arguments={"path": "/tmp/foo"},
            classification="high_risk",
        )
        _append_operator_revise_block(
            pending,
            "  judge_note: try this path\n"
            "  tool_arguments:\n"
            "    path: /tmp/safer",
        )

        from atomic_agents.judge.backend import Judgment, JudgmentOutcome

        real_id = agent._ensure_policy_judge().judge_id
        real_pv = agent._ensure_policy_judge().policy_version

        def evaluate(proposal, context):
            return Judgment(
                outcome=JudgmentOutcome.ALLOW,
                reason="re-judge ALLOWs",
                judge_id=real_id,
                policy_version=real_pv,
            )

        monkeypatch.setattr(agent._ensure_policy_judge(), "evaluate", evaluate)
        agent.poll_escalations()

        log_dir = agent.agent_root / "log"
        re_judge_records = []
        for log_file in log_dir.rglob("*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("trigger") == "escalation_operator_revise_re_judge":
                    re_judge_records.append(rec)
        assert len(re_judge_records) >= 1
        # The actor's original run_id was set in
        # _write_pending_with_operator_revise to "actor_run_xyz".
        for rec in re_judge_records:
            assert rec["parent_run_id"] == "actor_run_xyz"


class TestOperatorReviseInvalidAmendment:
    def test_no_amendment_block_no_execute(self, tmp_path, monkeypatch):
        agent = _build_agent(tmp_path, monkeypatch)
        pending, queue_id, executions = _write_pending_with_operator_revise(
            agent,
            tool_arguments={"to": "x@y"},
        )
        # Append a Revised block with NO amendment yaml.
        text = pending.read_text(encoding="utf-8")
        text = text.replace("state: pending", "state: resolved")
        text = text + "\n### Revised by alice\nresolved_at: 2026-05-13T12:05:00+00:00\n"
        pending.write_text(text, encoding="utf-8")

        agent.poll_escalations()
        assert executions == []

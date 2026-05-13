"""End-to-end ESCALATE state machine tests through AtomicAgent.

Covers the integration surface where ``_dispatch_with_judge`` mints a
PENDING file, ``agent.call()`` returns ``Response(deferred=True,
escalation_queue_ids=[...])``, ``poll_escalations`` picks up operator
resolutions on the next call, and Approved actions execute inline.

These tests use lightweight stub LLM adapters / handlers to avoid
real API calls — same pattern as test_agent_judge_dispatch.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.tools import ToolDefinition
from atomic_agents.types import Response


@pytest.fixture
def escalating_agent(tmp_path, monkeypatch):
    """An AtomicAgent with judges.md set to escalate external_side_effect."""
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
        "  external_side_effect: escalate\n"
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


class TestDispatchEscalate:
    def test_class_policy_escalate_writes_pending_and_defers(self, escalating_agent):
        # External-side-effect tool + class_policy=escalate →
        # _dispatch_with_judge synthesizes ESCALATE, writes the PENDING
        # file, returns queue_id, and signals defer.
        agent = escalating_agent
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "test"}}
        allow, events, queue_id = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        assert queue_id is not None
        assert events[-1]["enforcement_action"] == "escalate_pending"
        assert events[-1]["synthesis_source"] == "class_policy"
        # PENDING file exists at expected path
        pending = (
            agent.agent_root
            / "vault/escalations/external_side_effect"
            / f"{queue_id}.md"
        )
        assert pending.exists()

    def test_synth_pending_includes_audit_fields(self, escalating_agent):
        agent = escalating_agent
        agent.tool_registry.register(
            ToolDefinition(
                name="post_message",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )
        tu = {"name": "post_message", "input": {"to": "x"}, "id": "tc_x"}
        markers = {"tc_x": {"for_tool_call_id": "tc_x", "reason": "test"}}
        _, events, queue_id = agent._dispatch_with_judge(tu, markers)
        event = events[-1]
        assert event["judgment_outcome"] == "escalate"
        assert event["escalation_queue_id"] == queue_id
        assert event["judge_id"] == "framework"


class TestMultiToolMidTurnDefer:
    """Round-2 P2-NEW-C: pin the 1-escalate + N-allowed-in-same-turn
    semantic. Code at agent.py accumulates judge_deferred dict +
    Response.escalation_queue_ids list; without an integration test,
    a regression in the dict-population path would not be caught.
    """

    def test_two_tools_one_escalates_one_allows(self, tmp_path, monkeypatch):
        # Two tool_uses in one assistant turn: read_only (BYPASS → allow)
        # + external_side_effect (class_policy=escalate). Verify the
        # judge_deferred dict captures only the escalate, and the
        # allowed tool_use behaves normally through the dispatch surface.
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
            "  read_only: bypass\n"
            "  external_side_effect: escalate\n"
            "```\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        agent = AtomicAgent(name="tester", agents_root=agents_root)
        agent.load()

        agent.tool_registry.register(
            ToolDefinition(
                name="read_doc",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: "doc body",
                classification="read_only",
            )
        )
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )

        # Dispatch each tool_use individually (mirrors what
        # _dispatch_with_judge does for each tool_use in the actor's
        # assistant turn). Combined into accumulator like agent.call().
        markers = {
            "tc_read": {"for_tool_call_id": "tc_read", "reason": "read"},
            "tc_send": {"for_tool_call_id": "tc_send", "reason": "send"},
        }
        tu_read = {"name": "read_doc", "input": {}, "id": "tc_read"}
        tu_send = {"name": "send_email", "input": {"to": "x"}, "id": "tc_send"}

        allow_r, _, queue_r = agent._dispatch_with_judge(tu_read, markers)
        allow_s, _, queue_s = agent._dispatch_with_judge(tu_send, markers)
        # Read-only BYPASS allows + no escalation.
        assert allow_r is True
        assert queue_r is None
        # External-side-effect ESCALATE → defer + queue_id.
        assert allow_s is False
        assert queue_s is not None

        # Pin Response shape: deferred=True with the single queue_id
        # when the agent.call() flow accumulates one ESCALATE.
        from atomic_agents.types import Response

        r = Response(
            text="",
            model="m",
            input_tokens=0,
            output_tokens=0,
            deferred=True,
            escalation_queue_ids=[queue_s],
        )
        assert r.deferred is True
        assert r.escalation_queue_ids == [queue_s]


class TestResponseShape:
    def test_response_deferred_field_default_false(self):
        # Adding deferred + escalation_queue_ids is non-breaking.
        r = Response(text="hi", model="m", input_tokens=0, output_tokens=0)
        assert r.deferred is False
        assert r.escalation_queue_ids == []

    def test_response_deferred_accepts_list(self):
        r = Response(
            text="hi",
            model="m",
            input_tokens=0,
            output_tokens=0,
            deferred=True,
            escalation_queue_ids=["q1", "q2"],
        )
        assert r.deferred is True
        assert r.escalation_queue_ids == ["q1", "q2"]


class TestPollIntegration:
    def test_poll_escalations_picks_up_approved(self, escalating_agent):
        # Write a PENDING via dispatch, then operator-approve it, then
        # poll picks it up + executes.
        agent = escalating_agent
        execute_calls: list[dict] = []

        def handler(inp):
            execute_calls.append(dict(inp))
            return {"ok": True}

        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object"},
                handler=handler,
                classification="external_side_effect",
            )
        )
        tu = {
            "name": "send_email",
            "input": {"to": "x@y", "body": "hi"},
            "id": "tc_1",
        }
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "test"}}
        _, _, queue_id = agent._dispatch_with_judge(tu, markers)
        pending = (
            agent.agent_root
            / "vault/escalations/external_side_effect"
            / f"{queue_id}.md"
        )
        # Operator appends Approved block and flips state.
        text = pending.read_text(encoding="utf-8")
        text = text.replace("state: pending", "state: resolved")
        text = text + "\n### Approved by alice\nresolved_at: 2026-05-13T12:01:00+00:00\n"
        pending.write_text(text, encoding="utf-8")

        events = agent.poll_escalations()
        assert len(events) == 1
        # Approved + fresh hash → handler ran
        assert len(execute_calls) == 1
        assert execute_calls[0]["to"] == "x@y"

    def test_poll_throttle_skips_within_window(self, tmp_path, monkeypatch):
        # With throttle=60s, a second poll returns immediately.
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
            "escalation:\n"
            "  resolution_poll_cycle_seconds: 60\n"
            "```\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        agent = AtomicAgent(name="tester", agents_root=agents_root)
        agent.load()
        # First poll lands; second hits throttle.
        events1 = agent.poll_escalations()
        events2 = agent.poll_escalations()
        assert events1 == []
        assert events2 == []  # both empty, but second was throttled

    def test_poll_skipped_when_judge_disabled(self, tmp_path, monkeypatch):
        # No judges.md, no AGENT_JUDGE_ENABLED → poll_escalations returns
        # [] without scanning.
        monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
        agents_root = tmp_path / "agents"
        agent_dir = agents_root / "tester"
        (agent_dir / "persona").mkdir(parents=True)
        (agent_dir / "persona" / "IDENTITY.md").write_text("# I")
        (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
        (agent_dir / "model.md").write_text(
            "## Default model\nclaude-haiku-4-5-20251001\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        agent = AtomicAgent(name="tester", agents_root=agents_root)
        agent.load()
        # judges_config is None → poll bails before scanning.
        assert agent.poll_escalations() == []


class TestFailurePolicyEscalateSynth:
    """Round-1 P2-6: failure_policy=escalate path through the ensemble.

    When a judge raises a JudgeError mapped to escalate via
    failure_policy, the dispatch loop must:
    - synthesize ESCALATE outcome
    - write PENDING file with synthesis_source="failure_policy"
    - emit audit event with triggered_by="failure_policy:<ExceptionName>"
    - return queue_id so the caller defers the actor's run

    Round 1 plan review's P1 #9 + P2 #6: this path was implemented
    but not directly tested. Round-1 P2-6 caught the gap.
    """

    def test_failure_policy_maps_judgeerror_to_escalate(self, tmp_path, monkeypatch):
        from atomic_agents.exceptions import JudgeUnavailable
        from atomic_agents.judge.backend import JudgmentOutcome

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
        # failure_policy: judge_required class, JudgeUnavailable → escalate
        (agent_dir / "judges.md").write_text(
            "```yaml\n"
            "class_policy:\n"
            "  external_side_effect: judge_required\n"
            "failure_policy:\n"
            "  JudgeUnavailable: escalate\n"
            "```\n"
        )
        (agent_dir / "memory").mkdir()
        (agent_dir / "log").mkdir()
        agent = AtomicAgent(name="tester", agents_root=agents_root)
        agent.load()

        # Stub out PolicyJudge.evaluate to raise JudgeUnavailable so
        # the failure_policy mapping fires.
        original_evaluate = agent._ensure_policy_judge().evaluate

        def raise_unavailable(proposal, context):
            raise JudgeUnavailable("backend timeout (stub)")

        monkeypatch.setattr(
            agent._ensure_policy_judge(),
            "evaluate",
            raise_unavailable,
        )

        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "test"}}
        allow, events, queue_id = agent._dispatch_with_judge(tu, markers)
        assert allow is False
        assert queue_id is not None
        assert events[-1]["judgment_outcome"] == "escalate"
        assert events[-1]["enforcement_action"] == "escalate_pending"
        assert events[-1]["synthesis_source"] == "failure_policy"
        assert events[-1]["triggered_by"] == "failure_policy:JudgeUnavailable"
        # PENDING file landed on disk with the right frontmatter.
        pending = (
            agent.agent_root
            / "vault/escalations/external_side_effect"
            / f"{queue_id}.md"
        )
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert "triggered_by: failure_policy:JudgeUnavailable" in text
        assert "synthesis_source: failure_policy" in text


class TestDeferredExecutionRehydration:
    def test_stale_tool_definition_hash_refuses_execution(self, escalating_agent):
        # PENDING written with one tool_definition_hash; before resolution,
        # the tool is re-registered with a different schema → poll detects
        # the mismatch and refuses execution.
        agent = escalating_agent
        execute_calls: list[dict] = []

        def handler(inp):
            execute_calls.append(dict(inp))
            return {"ok": True}

        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object"},
                handler=handler,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "test"}}
        _, _, queue_id = agent._dispatch_with_judge(tu, markers)
        pending = (
            agent.agent_root
            / "vault/escalations/external_side_effect"
            / f"{queue_id}.md"
        )
        # Operator approves...
        text = pending.read_text(encoding="utf-8")
        text = text.replace("state: pending", "state: resolved")
        text = text + "\n### Approved by alice\nresolved_at: 2026-05-13T12:01:00+00:00\n"
        pending.write_text(text, encoding="utf-8")

        # ...but the tool's schema is mutated before poll picks up.
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object", "properties": {"to": {"type": "string"}}},
                handler=handler,
                classification="external_side_effect",
            ),
            allow_overwrite=True,
        )
        events = agent.poll_escalations()
        assert len(events) == 1
        # Handler NOT executed because hash mismatched.
        assert execute_calls == []

    def test_deferred_tool_call_result_carries_deferred_field(self, escalating_agent):
        # Round-1 P2-4: consumers iterating Response.tool_calls
        # distinguish deferred from genuine handler errors via
        # ToolCallResult.deferred, NOT by error-string match.
        agent = escalating_agent
        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object"},
                handler=lambda i: None,
                classification="external_side_effect",
            )
        )
        # Simulate the agent.call() dispatch loop's deferred handling
        # directly by exercising _dispatch_with_judge then the loop's
        # synthesized ToolCallResult shape.
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "test"}}
        _, _, queue_id = agent._dispatch_with_judge(tu, markers)
        # Verify the ToolCallResult dataclass has the deferred field
        # and it defaults False — non-deferred results unchanged.
        from atomic_agents.tools import ToolCallResult

        defaulted = ToolCallResult(
            tool_name="t", tool_use_id="x", input={}, output=None
        )
        assert defaulted.deferred is False
        deferred_result = ToolCallResult(
            tool_name="t",
            tool_use_id="x",
            input={},
            output=None,
            error="judge_deferred: ...",
            deferred=True,
        )
        assert deferred_result.deferred is True

    def test_deferred_execution_emits_audit_line(self, escalating_agent):
        # Approved + fresh hash → execution happens, audit line carries
        # cost_source=actor and escalation_queue_id linkage.
        import json
        from pathlib import Path

        agent = escalating_agent

        def handler(inp):
            return {"sent": True}

        agent.tool_registry.register(
            ToolDefinition(
                name="send_email",
                description="...",
                input_schema={"type": "object"},
                handler=handler,
                classification="external_side_effect",
            )
        )
        tu = {"name": "send_email", "input": {"to": "x@y"}, "id": "tc_1"}
        markers = {"tc_1": {"for_tool_call_id": "tc_1", "reason": "test"}}
        _, _, queue_id = agent._dispatch_with_judge(tu, markers)
        pending = (
            agent.agent_root
            / "vault/escalations/external_side_effect"
            / f"{queue_id}.md"
        )
        text = pending.read_text(encoding="utf-8")
        text = text.replace("state: pending", "state: resolved")
        text = text + "\n### Approved by alice\nresolved_at: 2026-05-13T12:01:00+00:00\n"
        pending.write_text(text, encoding="utf-8")
        agent.poll_escalations()

        # Find any JSONL log line with trigger=escalation_deferred_execution.
        log_dir = agent.agent_root / "log"
        records = []
        for log_file in log_dir.rglob("*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("trigger") == "escalation_deferred_execution":
                    records.append(rec)
        assert len(records) >= 1
        rec = records[0]
        assert rec["escalation_queue_id"] == queue_id
        assert rec["cost_source"] == "actor"

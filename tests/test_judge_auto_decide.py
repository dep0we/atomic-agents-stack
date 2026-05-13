"""Tests for the PR 3b ESCALATE auto-decide-timeout flow.

When ``judges_config.escalation.auto_decide_after_seconds`` elapses with
no operator resolution, the framework applies the
``fallback_on_timeout`` policy (default block). The framework writes a
``### Auto-decided by framework`` block, flips state to resolved, and
emits the RESOLVED event with ``enforcement_action=auto_decided_block``.

Race detection: if the file changes between read-snapshot and write,
the framework defers — operator's resolution wins.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.judge import escalation as _esc
from atomic_agents.judge.types import (
    ActionClass,
    ActionProposal,
    EscalationConfig,
)


def _make_proposal(
    *, proposal_id: str = "proposal_auto_001"
) -> ActionProposal:
    from atomic_agents.judge.proposal import compute_arguments_hash

    args = {"to": "x@y", "body": "hi"}
    return ActionProposal(
        tool_name="send_email",
        tool_arguments=args,
        tool_call_id="tc_1",
        tool_definition_hash="sha256:" + "a" * 64,
        arguments_hash=compute_arguments_hash(args),
        classification=ActionClass.EXTERNAL_SIDE_EFFECT,
        classification_source="tools.md",
        actor_agent="caldwell",
        actor_run_id="agent_run_1",
        proposal_id=proposal_id,
        proposal_ts="2026-05-13T12:00:00+00:00",
    )


def _write_pending(tmp_path: Path, proposal: ActionProposal) -> Path:
    cfg = EscalationConfig()
    pending_path, _ = _esc.write_pending_escalation(
        proposal=proposal,
        judgment_reason="r",
        judge_id="default-llm",
        agent_root=tmp_path,
        agent_name="caldwell",
        parent_run_id="agent_run_1",
        policy_version="pv",
        judges_config_escalation=cfg,
    )
    return pending_path


def _backdate_escalated_at(pending_path: Path, *, seconds_ago: int) -> None:
    """Rewrite the escalated_at frontmatter to N seconds in the past."""
    backdated = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)
    text = pending_path.read_text(encoding="utf-8")
    import re

    text = re.sub(
        r"escalated_at: [^\n]+",
        f"escalated_at: {backdated.isoformat()}",
        text,
    )
    pending_path.write_text(text, encoding="utf-8")


class TestAutoDecideTimeout:
    def test_past_timeout_applies_block_fallback(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        # Backdate so 120s elapsed; timeout window is 60s.
        _backdate_escalated_at(pending, seconds_ago=120)
        cfg = EscalationConfig(
            auto_decide_after_seconds=60,
            fallback_on_timeout="block",
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 1
        event = events[0]
        assert event.decision is _esc.ResolutionDecision.AUTO_DECIDED_BLOCK
        assert event.enforcement_action == "auto_decided_block"
        assert event.operator == "framework"
        # The PENDING file's state was flipped to resolved.
        text = pending.read_text(encoding="utf-8")
        assert "state: resolved" in text
        assert "### Auto-decided by framework" in text

    def test_within_timeout_no_op(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        # Backdate 10s — within 60s window.
        _backdate_escalated_at(pending, seconds_ago=10)
        cfg = EscalationConfig(
            auto_decide_after_seconds=60, fallback_on_timeout="block"
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert events == []

    def test_no_timeout_configured_no_auto_decide(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _backdate_escalated_at(pending, seconds_ago=100_000)  # very old
        cfg = EscalationConfig(auto_decide_after_seconds=None)
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        # No timeout configured — file sits PENDING indefinitely.
        assert events == []

    def test_naive_escalated_at_does_not_wedge(self, tmp_path):
        # Round-1 P1-4: operators hand-editing the frontmatter may
        # strip the +00:00 offset. The framework must coerce naive
        # → UTC, not raise TypeError on the timedelta and wedge the
        # file every poll cycle.
        import re

        proposal = _make_proposal(proposal_id="proposal_naive_tz")
        pending = _write_pending(tmp_path, proposal)
        text = pending.read_text(encoding="utf-8")
        # Strip the +00:00 tz suffix and backdate.
        text = re.sub(
            r"escalated_at: [^\n]+",
            "escalated_at: 2020-01-01T00:00:00",
            text,
        )
        pending.write_text(text, encoding="utf-8")
        cfg = EscalationConfig(
            auto_decide_after_seconds=60, fallback_on_timeout="block"
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 1
        assert events[0].decision is _esc.ResolutionDecision.AUTO_DECIDED_BLOCK

    def test_crash_recovery_via_rewrite_then_claim(self, tmp_path):
        # Round-1 P1-1: previous claim-then-rewrite order left the
        # file stuck after a crash. PR 3b's rewrite-then-claim order
        # is crash-recoverable: if the framework wrote the
        # auto-decide block but failed to claim the sidecar (e.g.,
        # process killed), the next poll sees state=resolved and
        # routes through _claim_operator_resolution which DOES claim
        # the sidecar from the resolved-state path. Emit happens.
        proposal = _make_proposal(proposal_id="proposal_crash_recovery")
        pending = _write_pending(tmp_path, proposal)
        _backdate_escalated_at(pending, seconds_ago=120)
        cfg = EscalationConfig(
            auto_decide_after_seconds=60, fallback_on_timeout="block"
        )
        # First poll auto-decides AND claims the sidecar.
        events1 = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events1) == 1
        # Simulate operator/sysadmin nuking the sidecar (rm .*).
        events1[0].sidecar_path.unlink()
        # Next poll routes through the resolved-state branch and
        # re-emits a RESOLVED event (the file body has the
        # Auto-decided block; the parser sees Auto-decided → AUTO_DECIDED_BLOCK).
        events2 = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        # Crash-recovery: a missing sidecar on a resolved file re-emits.
        # This is the cost of vault-as-source-of-truth + no in-memory
        # de-dup state. Sidecar persistence is the contract.
        assert len(events2) == 1
        assert events2[0].decision is _esc.ResolutionDecision.AUTO_DECIDED_BLOCK

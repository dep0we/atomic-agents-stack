"""Tests for the PR 3b ESCALATE poll-side (atomic_agents/judge/escalation.py).

Covers:
- Throttle: second poll within window returns empty
- pending file with no transition → no event
- pending → resolved emits RESOLVED event with O_EXCL sidecar
- pending → redacted emits RESOLVED event
- Sidecar O_EXCL: concurrent pollers emit exactly once
- First resolution block wins (top-down)
- Body-integrity check: edited proposal block → BODY_TAMPERED
- Strict parser: malformed header → UNPARSEABLE, no sidecar claim
- Revised block in PR 3b → treated as Denied + warning logged
- Auto-decide: past timeout → fallback block applied
- Auto-decide CAS race: operator wrote first → framework skips
- Auto-decide RESOLVED has auto_decided_block enforcement_action
"""

from __future__ import annotations

import time
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
    *,
    proposal_id: str = "proposal_20260513T120000_abc12345",
    tool_arguments: dict | None = None,
) -> ActionProposal:
    from atomic_agents.judge.proposal import compute_arguments_hash

    args = tool_arguments if tool_arguments is not None else {"to": "x@y", "body": "hi"}
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


def _write_pending(tmp_path: Path, proposal: ActionProposal, *, cfg=None) -> Path:
    cfg = cfg or EscalationConfig()
    pending_path, _ = _esc.write_pending_escalation(
        proposal=proposal,
        judgment_reason="judge wants operator review",
        judge_id="default-llm",
        agent_root=tmp_path,
        agent_name="caldwell",
        parent_run_id="agent_run_1",
        policy_version="pv",
        judges_config_escalation=cfg,
    )
    return pending_path


def _append_resolution_and_resolve(pending_path: Path, block: str, *, state: str = "resolved") -> None:
    """Test helper: simulate an operator appending a resolution block
    and flipping the state field."""
    text = pending_path.read_text(encoding="utf-8")
    text = text.replace("state: pending", f"state: {state}")
    text = text + "\n" + block + "\n"
    pending_path.write_text(text, encoding="utf-8")


class TestThrottle:
    def test_within_window_returns_empty(self, tmp_path):
        proposal = _make_proposal()
        _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            tmp_path / "vault/escalations/external_side_effect" / f"{proposal.proposal_id}.md",
            "### Approved by alice\nresolved_at: 2026-05-13T12:01:00+00:00\nnote: ok",
        )
        cfg = EscalationConfig(resolution_poll_cycle_seconds=60)
        # Touch the marker so subsequent polls hit throttle.
        _esc.touch_last_poll(agent_root=tmp_path, judges_config_escalation=cfg)
        # is_within_throttle should now return True
        assert _esc.is_within_throttle(
            agent_root=tmp_path, judges_config_escalation=cfg
        ) is True

    def test_throttle_zero_disables(self, tmp_path):
        cfg = EscalationConfig(resolution_poll_cycle_seconds=0)
        assert _esc.is_within_throttle(
            agent_root=tmp_path, judges_config_escalation=cfg
        ) is False


class TestPendingStateNoTransition:
    def test_pending_no_timeout_returns_empty(self, tmp_path):
        proposal = _make_proposal()
        _write_pending(tmp_path, proposal)
        cfg = EscalationConfig(auto_decide_after_seconds=None)
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert events == []


class TestResolutionTransitions:
    def test_pending_to_resolved_emits_event(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            pending,
            "### Approved by alice\nresolved_at: 2026-05-13T12:01:00+00:00\nnote: looks fine",
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events) == 1
        event = events[0]
        assert event.decision is _esc.ResolutionDecision.APPROVED
        assert event.operator == "alice"
        assert event.enforcement_action == "approved_executed"
        # Sidecar was created
        assert event.sidecar_path.exists()

    def test_pending_to_redacted_emits_event(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            pending,
            "### Redacted by bob\nredacted_at: 2026-05-13T12:02:00+00:00\nredaction_reason: pii",
            state="redacted",
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events) == 1
        assert events[0].decision is _esc.ResolutionDecision.REDACTED
        assert events[0].operator == "bob"
        assert events[0].enforcement_action == "redacted"

    def test_pending_to_denied_emits_event(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            pending,
            "### Denied by carol\nresolved_at: 2026-05-13T12:03:00+00:00\nnote: no",
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events) == 1
        assert events[0].decision is _esc.ResolutionDecision.DENIED
        assert events[0].enforcement_action == "denied"

    def test_sidecar_prevents_duplicate_emit(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            pending, "### Approved by dave\nresolved_at: 2026-05-13T12:04:00+00:00"
        )
        events1 = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        events2 = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events1) == 1
        assert len(events2) == 0


class TestResolutionParser:
    def test_first_block_wins(self, tmp_path):
        # Two blocks present — first one (Approved) wins.
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            pending,
            "### Approved by alice\nresolved_at: 2026-05-13T12:01:00+00:00\n\n"
            "### Denied by bob\nresolved_at: 2026-05-13T12:05:00+00:00",
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events) == 1
        assert events[0].decision is _esc.ResolutionDecision.APPROVED

    def test_strict_parser_rejects_lowercase_header(self, tmp_path):
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        # Lowercase "approved" — strict parser does not match.
        _append_resolution_and_resolve(
            pending, "### approved by alice\nresolved_at: 2026-05-13T12:01:00+00:00"
        )
        warnings: list[str] = []
        events = _esc.poll_resolutions(
            agent_root=tmp_path,
            judges_config_escalation=EscalationConfig(),
            log_warning=warnings.append,
        )
        assert events == []
        assert any("strict-parser" in w or "no valid resolution block" in w for w in warnings)
        # Sidecar should NOT have been claimed (unlinked on parse fail)
        sidecar = pending.with_name(f".{pending.name}.resolved-emitted")
        assert not sidecar.exists()

    def test_revised_block_without_amendment_invalid(self, tmp_path):
        # PR 3c: Revised block with no embedded ``amendment:`` YAML
        # block is operator_revise_invalid_amendment — the sidecar IS
        # claimed (operator's intent recorded), but enforcement
        # promotes to the invalid value so no execution happens.
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            pending, "### Revised by emma\nresolved_at: 2026-05-13T12:06:00+00:00"
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events) == 1
        assert events[0].decision is _esc.ResolutionDecision.OPERATOR_REVISED
        assert events[0].enforcement_action == "operator_revise_invalid_amendment"
        assert events[0].amendment is None

    def test_revised_block_with_amendment_parses(self, tmp_path):
        # PR 3c happy path: operator authored ### Revised by op with an
        # embedded amendment YAML. The framework parses the amendment
        # into a ProposalAmendment dataclass and the agent executes
        # the revised action via _process_operator_revise.
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        _append_resolution_and_resolve(
            pending,
            "### Revised by alice\n"
            "resolved_at: 2026-05-13T12:06:00+00:00\n"
            "note: stripping attachment per policy\n"
            "amendment:\n"
            "  ```yaml\n"
            "  judge_note: operator stripped attachment\n"
            "  tool_arguments:\n"
            "    to: x@y\n"
            "    body: hi\n"
            "  ```\n",
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events) == 1
        assert events[0].decision is _esc.ResolutionDecision.OPERATOR_REVISED
        assert events[0].enforcement_action == "operator_revise_executed"
        assert events[0].amendment is not None
        assert events[0].amendment.tool_arguments == {"to": "x@y", "body": "hi"}


class TestBodyIntegrity:
    def test_tampered_proposal_body_refused(self, tmp_path):
        # Operator edits the proposal's tool_arguments in the body.
        # arguments_hash no longer matches recomputed value → tamper.
        proposal = _make_proposal()
        pending = _write_pending(tmp_path, proposal)
        # Edit the body's `"body": "hi"` to `"body": "HIJACKED"`.
        text = pending.read_text(encoding="utf-8")
        text = text.replace('"body": "hi"', '"body": "HIJACKED"')
        text = text.replace("state: pending", "state: resolved")
        text = text + "\n### Approved by hijacker\nresolved_at: 2026-05-13T12:07:00+00:00\n"
        pending.write_text(text, encoding="utf-8")
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=EscalationConfig()
        )
        assert len(events) == 1
        # Decision flips to BODY_TAMPERED even though operator wrote Approved
        assert events[0].decision is _esc.ResolutionDecision.BODY_TAMPERED
        assert events[0].enforcement_action == "proposal_body_tampered"

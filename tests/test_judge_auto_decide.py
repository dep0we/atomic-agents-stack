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
            fallback_on_timeout={"default": "block"},
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
            auto_decide_after_seconds=60, fallback_on_timeout={"default": "block"}
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
            auto_decide_after_seconds=60, fallback_on_timeout={"default": "block"}
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
            auto_decide_after_seconds=60, fallback_on_timeout={"default": "block"}
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


# ──────────────────────────────────────────────────────────────────
# Per-class fallback_on_timeout (PR 5a of #112)


class TestPerClassFallbackOnTimeout:
    """``EscalationConfig.fallback_on_timeout`` is a dict keyed by
    ``ActionClass.value`` with a mandatory ``"default"``. The auto-decide
    path resolves the policy for a PENDING file using its frontmatter
    ``action_class`` (NOT the on-disk directory name) so the
    authoritative classification recorded at write time drives the
    decision — operator typos on the directory layout don't silently
    fall through to the default policy.
    """

    def _make_proposal_with_class(
        self, *, action_class: ActionClass, proposal_id: str
    ) -> ActionProposal:
        from atomic_agents.judge.proposal import compute_arguments_hash

        args = {"to": "x@y", "body": "hi"}
        return ActionProposal(
            tool_name="send_email",
            tool_arguments=args,
            tool_call_id="tc_1",
            tool_definition_hash="sha256:" + "a" * 64,
            arguments_hash=compute_arguments_hash(args),
            classification=action_class,
            classification_source="tools.md",
            actor_agent="caldwell",
            actor_run_id="agent_run_1",
            proposal_id=proposal_id,
            proposal_ts="2026-05-13T12:00:00+00:00",
        )

    def test_high_risk_uses_per_class_override_default_used_for_other(
        self, tmp_path
    ):
        # high_risk → block (per-class), external_side_effect → allow
        # (the default). Two pending files; one resolves to block,
        # the other to allow.
        p_high = self._make_proposal_with_class(
            action_class=ActionClass.HIGH_RISK,
            proposal_id="proposal_high",
        )
        p_ext = self._make_proposal_with_class(
            action_class=ActionClass.EXTERNAL_SIDE_EFFECT,
            proposal_id="proposal_ext",
        )
        pending_high = _write_pending(tmp_path, p_high)
        pending_ext = _write_pending(tmp_path, p_ext)
        _backdate_escalated_at(pending_high, seconds_ago=120)
        _backdate_escalated_at(pending_ext, seconds_ago=120)

        cfg = EscalationConfig(
            auto_decide_after_seconds=60,
            fallback_on_timeout={
                "default": "allow",
                "high_risk": "block",
            },
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 2
        by_id = {e.frontmatter.proposal_id: e for e in events}
        assert by_id["proposal_high"].decision is _esc.ResolutionDecision.AUTO_DECIDED_BLOCK
        assert by_id["proposal_ext"].decision is _esc.ResolutionDecision.AUTO_DECIDED_ALLOW
        # Reason line records the action_class so operators reading a
        # stale PENDING file know which policy applied.
        assert "action_class=high_risk" in by_id["proposal_high"].reason
        assert "action_class=external_side_effect" in by_id["proposal_ext"].reason

    def test_class_not_in_per_class_falls_back_to_default(self, tmp_path):
        # reversible_write is NOT listed; default = escalate.
        proposal = self._make_proposal_with_class(
            action_class=ActionClass.REVERSIBLE_WRITE,
            proposal_id="proposal_rev",
        )
        pending = _write_pending(tmp_path, proposal)
        _backdate_escalated_at(pending, seconds_ago=120)
        cfg = EscalationConfig(
            auto_decide_after_seconds=60,
            fallback_on_timeout={
                "default": "block",
                "high_risk": "allow",  # listed but irrelevant for this proposal
            },
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 1
        # Default applied — block.
        assert events[0].decision is _esc.ResolutionDecision.AUTO_DECIDED_BLOCK

    def test_authoritative_class_from_frontmatter_not_directory(
        self, tmp_path
    ):
        # P0 finding from plan review (§2): per-class resolution must
        # key on the PENDING file's frontmatter ``action_class``, NOT
        # the on-disk directory name. An operator could hand-move a
        # PENDING file into a wrong-named directory (or rename a
        # directory). The framework's auto-decide must still apply
        # the policy for the authoritative class recorded in the file.
        proposal = self._make_proposal_with_class(
            action_class=ActionClass.HIGH_RISK,
            proposal_id="proposal_misfiled",
        )
        # Normal write goes to vault/escalations/high_risk/...
        pending = _write_pending(tmp_path, proposal)
        # Simulate operator moving the file to a typo'd directory.
        typo_dir = pending.parent.parent / "hi_risk"
        typo_dir.mkdir()
        new_path = typo_dir / pending.name
        pending.rename(new_path)
        _backdate_escalated_at(new_path, seconds_ago=120)
        # Per-class config: high_risk → block, default → allow. If the
        # framework resolved per-class via the directory name (the bug
        # this test pins), it would not find "hi_risk" in the per-class
        # map and fall through to default=allow — silently relaxing the
        # high_risk policy. Authoritative-via-frontmatter means we get
        # high_risk → block, the correct policy.
        cfg = EscalationConfig(
            auto_decide_after_seconds=60,
            fallback_on_timeout={
                "default": "allow",
                "high_risk": "block",
            },
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 1
        assert events[0].decision is _esc.ResolutionDecision.AUTO_DECIDED_BLOCK
        assert "action_class=high_risk" in events[0].reason

    def test_unknown_frontmatter_class_falls_back_to_default(self, tmp_path):
        # Diff-review Finding 2: symmetric to the typo'd-directory case
        # but in the AUTHORITATIVE source itself. An operator (or a
        # future framework with a different ``ActionClass`` enum)
        # hand-edits the PENDING frontmatter ``action_class`` to a
        # string outside the spec/28 four-class set. The framework
        # falls through to ``default`` rather than raising —
        # ``fallback_map.get(fm.action_class, fallback_map["default"])``
        # is the contract. Pin the silent-fall-through so a future
        # refactor (e.g. someone writes
        # ``fallback_map[fm.action_class]`` thinking the parser
        # validates frontmatter contents) regresses LOUD instead of
        # quietly raising KeyError every poll cycle.
        import re

        proposal = self._make_proposal_with_class(
            action_class=ActionClass.HIGH_RISK,
            proposal_id="proposal_unknown_class",
        )
        pending = _write_pending(tmp_path, proposal)
        text = pending.read_text(encoding="utf-8")
        text = re.sub(
            r"action_class: [^\n]+",
            "action_class: hi_risk",
            text,
        )
        pending.write_text(text, encoding="utf-8")
        _backdate_escalated_at(pending, seconds_ago=120)
        cfg = EscalationConfig(
            auto_decide_after_seconds=60,
            fallback_on_timeout={
                "default": "allow",
                "high_risk": "block",  # would apply if frontmatter said high_risk
            },
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 1
        # ``hi_risk`` is not in the per-class map → ``default`` (allow).
        assert events[0].decision is _esc.ResolutionDecision.AUTO_DECIDED_ALLOW
        assert "action_class=hi_risk" in events[0].reason

    def test_two_files_same_directory_resolve_per_frontmatter(self, tmp_path):
        # Diff-review Finding 3: opposite direction from the typo'd-
        # directory test. Two PENDING files in the SAME class directory
        # but with DIFFERENT frontmatter ``action_class``. Each must
        # resolve via its OWN frontmatter — pinning the
        # "frontmatter is authoritative, directory is layout" claim
        # symmetrically.
        import re

        p_a = self._make_proposal_with_class(
            action_class=ActionClass.HIGH_RISK,
            proposal_id="proposal_misfile_1",
        )
        p_b = self._make_proposal_with_class(
            action_class=ActionClass.HIGH_RISK,
            proposal_id="proposal_misfile_2",
        )
        pending_a = _write_pending(tmp_path, p_a)
        pending_b = _write_pending(tmp_path, p_b)
        # Hand-edit pending_b's frontmatter to claim external_side_effect
        # while leaving the file in vault/escalations/high_risk/ —
        # operator-misfile scenario.
        text = pending_b.read_text(encoding="utf-8")
        text = re.sub(
            r"action_class: high_risk",
            "action_class: external_side_effect",
            text,
            count=1,
        )
        pending_b.write_text(text, encoding="utf-8")
        assert pending_a.parent.name == "high_risk"
        assert pending_b.parent.name == "high_risk"

        _backdate_escalated_at(pending_a, seconds_ago=120)
        _backdate_escalated_at(pending_b, seconds_ago=120)

        cfg = EscalationConfig(
            auto_decide_after_seconds=60,
            fallback_on_timeout={
                "default": "block",
                "high_risk": "block",
                "external_side_effect": "allow",
            },
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 2
        by_id = {e.frontmatter.proposal_id: e for e in events}
        # pending_a: frontmatter high_risk → high_risk policy → block.
        assert (
            by_id["proposal_misfile_1"].decision
            is _esc.ResolutionDecision.AUTO_DECIDED_BLOCK
        )
        # pending_b: frontmatter external_side_effect → external_side_effect
        # policy → allow. Directory neighbor is irrelevant.
        assert (
            by_id["proposal_misfile_2"].decision
            is _esc.ResolutionDecision.AUTO_DECIDED_ALLOW
        )

    def test_reason_line_records_resolved_class(self, tmp_path):
        # PR 5a P2 from plan review (§10): the on-disk Auto-decided
        # block embeds the resolved action_class in its reason line so
        # an operator reading a 6-month-old PENDING file can see WHICH
        # policy applied. The recovery regex still parses the policy.
        proposal = self._make_proposal_with_class(
            action_class=ActionClass.HIGH_RISK,
            proposal_id="proposal_reason_line",
        )
        pending = _write_pending(tmp_path, proposal)
        _backdate_escalated_at(pending, seconds_ago=120)
        cfg = EscalationConfig(
            auto_decide_after_seconds=60,
            fallback_on_timeout={"default": "block", "high_risk": "allow"},
        )
        events = _esc.poll_resolutions(
            agent_root=tmp_path, judges_config_escalation=cfg
        )
        assert len(events) == 1
        # On-disk text carries the resolved class.
        text = pending.read_text(encoding="utf-8")
        assert "fallback_on_timeout=allow" in text
        assert "action_class=high_risk" in text
        # The recovery parser still maps Auto-decided + fallback=allow
        # → AUTO_DECIDED_ALLOW.
        assert events[0].decision is _esc.ResolutionDecision.AUTO_DECIDED_ALLOW

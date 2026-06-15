"""Conformance + integration tests for ``MandateCheck`` judge specialist
(spec/29 §"MandateCheck judge specialist", #124 PR 3a).

Covers:
- MandateCheck unit tests — one per validation step (0, 0.5, 1-6, 7-9 stub)
- MandateStateManager unit tests — transitions, throttle persistence, schema guard
- TargetExtractorRegistry unit tests — builtins, register/replace, extract modes
- AtomicAgent integration tests — public API, ensemble wiring

Test count target: ~35-45 tests.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.judge.backend import JudgmentOutcome
from atomic_agents.judge.mandate_check import MandateCheck
from atomic_agents.judge.mandate_state import MandateStateManager
from atomic_agents.judge.cost_estimator_registry import (
    CostEstimatorRegistry,
    UnknownCostEstimator,
)
from atomic_agents.judge.target_extractor_registry import (
    TargetExtractorRegistry,
    UnknownTargetExtractor,
)
from atomic_agents.judges_md import MandateSettings
from atomic_agents.mandate.filesystem import FilesystemMandateBackend
from atomic_agents.mandate.types import (
    Mandate,
    MandateConstraints,
    MandateStateSchemaUnsupported,
    RevocationState,
    TargetPattern,
    TimeWindow,
)

from atomic_agents.judge.mandate_reservations import MandateReservationManager

from tests._mandate_test_helpers import make_proposal_citing


# ──────────────────────────────────────────────────────────────────
# Low-level mandate file writer
#
# The mandate YAML body requires a 'scope' prose field that
# make_mandate_md_content (from _mandate_test_helpers) does not emit.
# We write raw mandates.md sections so the parser (which requires
# ``granted_by, granted_at, scope, revocation_state``) is satisfied.


def _write_mandate(
    scope_root: Path,
    scope: str,
    mandate_id: str,
    *,
    revocation_state: str = "active",
    allowed_tools: list[str] | None = None,
    allowed_targets: list[str] | None = None,
    blocked_targets: list[str] | None = None,
    daily_token_usd: float | None = None,
    unconstrained: bool = False,
    granted_at: str | None = None,
    expires_at: str | None = None,
) -> Path:
    """Write a minimal valid mandate section and return the file path.

    Includes the ``scope:`` prose field required by the mandates.md parser
    (``_REQUIRED_FIELDS = {granted_by, granted_at, scope, revocation_state}``).
    """
    now = datetime.now(timezone.utc)
    ga = granted_at or now.isoformat()
    ea = expires_at or (now + timedelta(days=30)).isoformat()

    lines = [
        f"## {mandate_id}",
        "granted_by: test-operator@example.com",
        f"granted_at: {ga}",
        f"expires_at: {ea}",
        f"revocation_state: {revocation_state}",
        "scope: |",
        f"  Test mandate scope for {mandate_id}.",
        "revoked_at: null",
        "revocation_reason: null",
        "constraints:",
    ]

    if unconstrained:
        lines.append("  unconstrained: true")
        lines.append('  unconstrained_justification: "test unconstrained"')
    elif any(
        [allowed_tools, allowed_targets, blocked_targets, daily_token_usd is not None]
    ):
        if allowed_tools:
            lines.append("  allowed_tools:")
            for t in allowed_tools:
                lines.append(f"    - {t}")
        if allowed_targets:
            lines.append("  allowed_targets:")
            for t in allowed_targets:
                lines.append(f"    - {t}")
        if blocked_targets:
            lines.append("  blocked_targets:")
            for t in blocked_targets:
                lines.append(f"    - {t}")
        if daily_token_usd is not None:
            lines.append(f"  daily_token_usd: {daily_token_usd}")
    else:
        lines.append("  unconstrained: true")
        lines.append('  unconstrained_justification: "no-constraint default"')

    content = "\n".join(lines) + "\n"

    if scope.startswith("agent:"):
        agent_name = scope.split(":", 1)[1]
        mandates_path = scope_root / agent_name / "mandates.md"
    elif scope.startswith("project:"):
        mandates_path = scope_root / "mandates.md"
    else:
        raise ValueError(f"Unsupported scope {scope!r}")

    mandates_path.parent.mkdir(parents=True, exist_ok=True)

    if mandates_path.exists():
        existing = mandates_path.read_text(encoding="utf-8")
        out_lines: list[str] = []
        skipping = False
        target_heading = f"## {mandate_id}"
        for line in existing.splitlines(keepends=True):
            if line.startswith("## "):
                skipping = line.rstrip() == target_heading
            if not skipping:
                out_lines.append(line)
        existing = "".join(out_lines).rstrip("\n") + "\n"
        mandates_path.write_text(existing + "\n" + content, encoding="utf-8")
    else:
        mandates_path.write_text(content, encoding="utf-8")

    return mandates_path


# ──────────────────────────────────────────────────────────────────
# Shared fixtures


@pytest.fixture
def scope_root(tmp_path: Path) -> Path:
    """Temp directory acting as the scope_root for FilesystemMandateBackend."""
    return tmp_path


@pytest.fixture
def scope() -> str:
    """Agent-scope string used across unit tests."""
    return "agent:test-agent"


@pytest.fixture
def mandate_backend(scope_root: Path) -> FilesystemMandateBackend:
    """Fresh FilesystemMandateBackend rooted at scope_root."""
    return FilesystemMandateBackend(scope_root)


@pytest.fixture
def mandate_settings() -> MandateSettings:
    """Default MandateSettings (spec/29 documented defaults)."""
    return MandateSettings()


@pytest.fixture
def null_log_backend():
    """Minimal LogBackend stub — records appends; query returns []."""
    mock = MagicMock()
    mock.append = MagicMock()
    mock.query = MagicMock(return_value=[])
    return mock


@pytest.fixture
def extractor_registry() -> TargetExtractorRegistry:
    """Fresh TargetExtractorRegistry with built-ins pre-registered."""
    return TargetExtractorRegistry()


@pytest.fixture
def state_manager(
    mandate_backend: FilesystemMandateBackend, scope: str
) -> MandateStateManager:
    """MandateStateManager wired to the test backend + scope."""
    return MandateStateManager(mandate_backend=mandate_backend, scope=scope)


@pytest.fixture
def mandate_check(
    mandate_backend: FilesystemMandateBackend,
    scope: str,
    extractor_registry: TargetExtractorRegistry,
    state_manager: MandateStateManager,
    mandate_settings: MandateSettings,
    null_log_backend: Any,
) -> MandateCheck:
    """MandateCheck instance ready for unit testing."""
    return MandateCheck(
        mandate_backend=mandate_backend,
        scope=scope,
        target_extractor_registry=extractor_registry,
        mandate_state_manager=state_manager,
        mandate_settings=mandate_settings,
        log_backend=null_log_backend,
    )


# ──────────────────────────────────────────────────────────────────
# Mini-helpers for proposals


def _make_proposal_no_auth(tool_name: str = "test_tool"):
    """ActionProposal with authorization=None (no mandate cite)."""
    from atomic_agents.judge.types import ActionClass, ActionProposal
    from hashlib import sha256

    args = {"placeholder": True}
    args_canonical = repr(sorted(args.items())).encode("utf-8")
    return ActionProposal(
        tool_name=tool_name,
        tool_arguments=args,
        tool_call_id="tc-noop",
        tool_definition_hash="sha256:" + sha256(tool_name.encode()).hexdigest(),
        arguments_hash="sha256:" + sha256(args_canonical).hexdigest(),
        classification=ActionClass.EXTERNAL_SIDE_EFFECT,
        classification_source="default",
        actor_agent="test-agent",
        actor_run_id="run-noop",
        proposal_id="prop-noop",
        proposal_ts=datetime.now(timezone.utc).isoformat(),
        authorization=None,
    )


def _make_proposal_policy_auth():
    """ActionProposal citing 'operator' (not a mandate: prefix)."""
    from atomic_agents.judge.types import ActionClass, ActionProposal, Authorization
    from hashlib import sha256

    tool_name = "test_tool"
    args = {"placeholder": True}
    args_canonical = repr(sorted(args.items())).encode("utf-8")
    return ActionProposal(
        tool_name=tool_name,
        tool_arguments=args,
        tool_call_id="tc-policy",
        tool_definition_hash="sha256:" + sha256(tool_name.encode()).hexdigest(),
        arguments_hash="sha256:" + sha256(args_canonical).hexdigest(),
        classification=ActionClass.EXTERNAL_SIDE_EFFECT,
        classification_source="default",
        actor_agent="test-agent",
        actor_run_id="run-policy",
        proposal_id="prop-policy",
        proposal_ts=datetime.now(timezone.utc).isoformat(),
        authorization=Authorization(
            granted_by="operator",
            scope="operator-approved",
            granted_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


def _make_mc(
    mandate_backend: FilesystemMandateBackend,
    scope: str,
    null_log_backend: Any,
    settings: MandateSettings | None = None,
) -> MandateCheck:
    """Convenience factory for MandateCheck with a fresh state manager."""
    return MandateCheck(
        mandate_backend=mandate_backend,
        scope=scope,
        target_extractor_registry=TargetExtractorRegistry(),
        mandate_state_manager=MandateStateManager(
            mandate_backend=mandate_backend, scope=scope
        ),
        mandate_settings=settings or MandateSettings(),
        log_backend=null_log_backend,
    )


# ──────────────────────────────────────────────────────────────────
# MandateCheck unit tests


class TestMandateCheckStep0Passthrough:
    """Step 0 fast-path: proposals without a mandate cite are passed through."""

    def test_evaluate_passthrough_when_authorization_none(
        self, mandate_check: MandateCheck
    ):
        """Step 0: authorization=None → ALLOW with reason 'no_mandate_cite'."""
        proposal = _make_proposal_no_auth()
        judgment = mandate_check.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW
        assert judgment.reason == "no_mandate_cite"

    def test_evaluate_passthrough_when_granted_by_not_mandate_prefix(
        self, mandate_check: MandateCheck
    ):
        """Step 0: granted_by='operator' (no 'mandate:' prefix) → ALLOW pass-through."""
        proposal = _make_proposal_policy_auth()
        judgment = mandate_check.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW
        assert judgment.reason == "no_mandate_cite"


class TestMandateCheckStep05Throttle:
    """Step 0.5: suspicious-rebind throttle (spec/29 §'Suspicious-rebind throttle')."""

    def test_evaluate_blocks_when_throttle_active(
        self,
        mandate_check: MandateCheck,
        state_manager: MandateStateManager,
    ):
        """Step 0.5: armed throttle for (mandate_id, run_id) → BLOCK mandate_rebind_suspicious_throttled."""
        from dataclasses import replace

        mandate_id = "throttled-mandate"
        run_id = "run-abc"
        state_manager.arm_rebind_throttle(mandate_id, run_id, throttle_seconds=3600)

        proposal = make_proposal_citing(
            mandate_id, tool_arguments={"x": 1}, actor_agent="test-agent"
        )
        proposal = replace(proposal, actor_run_id=run_id)

        judgment = mandate_check.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_rebind_suspicious_throttled" in judgment.reason

    def test_evaluate_allows_when_throttle_expired(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 0.5: expired throttle does not block — falls through to step 1."""
        from dataclasses import replace

        mandate_id = "exp-throttle-mandate"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        # Write an already-expired throttle directly
        expired_state = {
            "schema_version": 1,
            "scope": scope,
            "mandates": {},
            "throttles": {
                mandate_id: {
                    "agent_run_id": "run-xyz",
                    "expires_at_iso": (
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ).isoformat(),
                    "original_state_inconsistent_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
            },
        }
        mandate_backend.write_state(scope, expired_state)

        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(
            mandate_id, tool_name="test_tool", actor_agent="test-agent"
        )
        proposal = replace(proposal, actor_run_id="run-xyz")

        judgment = mc.evaluate(proposal)
        assert "mandate_rebind_suspicious_throttled" not in judgment.reason

    def test_evaluate_allows_when_throttle_armed_for_different_agent_run(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 0.5: throttle keyed (mandate_id, run_id) — different run_id is NOT blocked."""
        mandate_id = "cross-run-mandate"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        state_manager = MandateStateManager(
            mandate_backend=mandate_backend, scope=scope
        )
        state_manager.arm_rebind_throttle(
            mandate_id, "run-other", throttle_seconds=3600
        )

        mc = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=state_manager,
            mandate_settings=MandateSettings(),
            log_backend=null_log_backend,
        )
        from dataclasses import replace

        proposal = make_proposal_citing(
            mandate_id, tool_name="test_tool", actor_agent="test-agent"
        )
        proposal = replace(proposal, actor_run_id="run-different")

        judgment = mc.evaluate(proposal)
        assert "mandate_rebind_suspicious_throttled" not in judgment.reason


class TestMandateCheckStep1Existence:
    """Step 1: mandate existence check (spec/29 line 362)."""

    def test_evaluate_blocks_when_mandate_not_in_backend(
        self, mandate_check: MandateCheck
    ):
        """Step 1: mandate not in backend → BLOCK with reason 'mandate_not_found'."""
        proposal = make_proposal_citing("does-not-exist")
        judgment = mandate_check.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_not_found")


class TestMandateCheckStep2SourceHash:
    """Step 2: source hash — no-op in PR 3a (spec/29 §TODO PR 4)."""

    def test_step_2_source_hash_is_no_op_in_pr_3a(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 2 is a no-op pass-through in PR 3a; proposal without source hash → ALLOW.
        Re-enable in PR 4 when proposal-assembly wires mandate_source_hash.
        """
        mandate_id = "hash-noop"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        judgment = mc.evaluate(proposal)
        # Step 2 no-op → all checks pass → ALLOW
        assert judgment.outcome == JudgmentOutcome.ALLOW
        # Confirm the no-op TODO comment is still in the source
        import inspect

        src = inspect.getsource(MandateCheck.evaluate)
        assert "step 2 is intentionally no-op in PR 3a" in src


class TestMandateCheckStep3State:
    """Step 3: mandate state check (spec/29 line 364)."""

    def test_evaluate_blocks_when_mandate_revoked(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 3: revocation_state=revoked → BLOCK 'mandate_revoked'."""
        mandate_id = "revoked-mandate"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            revocation_state="revoked",
            allowed_tools=["test_tool"],
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_revoked")

    def test_evaluate_blocks_when_mandate_expired(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 3: expires_at in the past → backend derives EXPIRED → BLOCK 'mandate_expired'."""
        mandate_id = "expired-mandate"
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        granted = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["test_tool"],
            granted_at=granted,
            expires_at=past,
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_expired")


class TestMandateCheckStep4ToolAllowlist:
    """Step 4: tool allowlist check (spec/29 line 365)."""

    def test_evaluate_blocks_when_tool_not_in_allowed_tools(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 4: tool not in allowed_tools → BLOCK 'mandate_tool_not_allowed'."""
        mandate_id = "tool-restricted"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["only_this_tool"])
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="other_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_tool_not_allowed")

    def test_evaluate_allows_when_tool_in_allowed_tools(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 4: tool in allowed_tools → passes tool check → ALLOW (no target constraint)."""
        mandate_id = "tool-allowed"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["send_email"])
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="send_email")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_evaluate_skips_tool_check_when_allowed_tools_empty(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 4: unconstrained mandate (no allowed_tools) → tool check skipped → ALLOW."""
        mandate_id = "no-tool-constraint"
        _write_mandate(scope_root, scope, mandate_id, unconstrained=True)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="any_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW


class TestMandateCheckStep5TargetAllowlist:
    """Step 5: target allowlist + blocklist + unextractable (spec/29 line 366-370)."""

    def test_evaluate_blocks_when_target_not_in_allowed_targets(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 5b: extracted target not in allowed_targets → BLOCK 'mandate_target_not_allowed'."""
        mandate_id = "target-allowlist"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["send_email"],
            allowed_targets=["alice@example.com"],
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(
            mandate_id,
            tool_name="send_email",
            tool_arguments={"to": "eve@malicious.com"},
        )
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_target_not_allowed")

    def test_evaluate_blocks_when_target_in_blocked_targets(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 5c: extracted target matches blocked_targets → BLOCK 'mandate_target_blocked'."""
        mandate_id = "target-blocklist"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["send_email"],
            blocked_targets=["danger@example.com"],
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(
            mandate_id,
            tool_name="send_email",
            tool_arguments={"to": "danger@example.com"},
        )
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_target_blocked")

    def test_evaluate_blocks_when_target_unextractable(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 5a: no extractor matches + allowed_targets set → BLOCK 'mandate_target_unextractable'."""
        mandate_id = "unextractable-block"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["mystery_tool"],
            allowed_targets=["expected-target"],
        )
        settings = MandateSettings(unextractable_target_action="block")
        mc = _make_mc(mandate_backend, scope, null_log_backend, settings=settings)
        proposal = make_proposal_citing(
            mandate_id,
            tool_name="mystery_tool",
            tool_arguments={"obscure_field": "value"},
        )
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_target_unextractable")

    def test_evaluate_escalates_when_unextractable_target_action_is_escalate(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 5a: unextractable_target_action='escalate' → ESCALATE instead of BLOCK."""
        mandate_id = "unextractable-escalate"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["mystery_tool"],
            allowed_targets=["expected-target"],
        )
        settings = MandateSettings(unextractable_target_action="escalate")
        mc = _make_mc(mandate_backend, scope, null_log_backend, settings=settings)
        proposal = make_proposal_citing(
            mandate_id,
            tool_name="mystery_tool",
            tool_arguments={"obscure_field": "value"},
        )
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ESCALATE
        assert "mandate_target_unextractable" in judgment.reason

    def test_evaluate_falls_back_to_heuristic_extractors(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 5b: heuristic extractor matches 'to' field → target extracted, allowlist passes."""
        mandate_id = "heuristic-target"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["send_email"],
            allowed_targets=["alice@example.com"],
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(
            mandate_id,
            tool_name="send_email",
            tool_arguments={"to": "alice@example.com"},
        )
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_evaluate_allows_when_target_matches_prefix_pattern(
        self, mandate_check: MandateCheck
    ):
        """Step 5b: _matches_any with prefix TargetPattern → True when target starts with pattern.
        Prefix match: target.startswith(pattern.rstrip('*')). Pattern 'mcp:gmail:' matches
        any Gmail MCP target; 'mcp:stripe:' does not match a Gmail target.
        """
        gmail_prefix = TargetPattern(pattern="mcp:gmail:", kind="prefix")
        assert (
            mandate_check._matches_any("mcp:gmail:alice@example.com", (gmail_prefix,))
            is True
        )
        assert mandate_check._matches_any("mcp:stripe:pi_abc", (gmail_prefix,)) is False

    def test_evaluate_uses_named_extractor_id_via_registry_extract(self):
        """Step 5: named extractor registered in registry; extract() returns custom field value."""
        registry = TargetExtractorRegistry()
        registry.register(
            "custom_field_extractor", lambda args: args.get("custom_field")
        )
        result = registry.extract(
            "custom_tool", {"custom_field": "expected-value"}, "custom_field_extractor"
        )
        assert result == "expected-value"

    def test_evaluate_prefixes_mcp_target_with_mcp_server(self):
        """Step 5: MCP tool with mcp_server set → extract returns 'mcp:<server>:<target>'."""
        registry = TargetExtractorRegistry()
        result = registry.extract(
            "send_email",
            {"to": "alice@example.com"},
            mcp_server="gmail",
        )
        assert result == "mcp:gmail:alice@example.com"


class TestMandateCheckStep6TimeWindow:
    """Step 6: time window check (spec/29 line 371)."""

    def _make_constrained_mandate(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        mandate_id: str,
        window: TimeWindow,
    ) -> Mandate:
        """Write a mandate and return a copy with the given time window injected."""
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        base = mandate_backend.load_mandate(mandate_id, scope)
        return Mandate(
            mandate_id=base.mandate_id,
            scope=base.scope,
            granted_by=base.granted_by,
            granted_at=base.granted_at,
            expires_at=base.expires_at,
            revocation_state=base.revocation_state,
            revoked_at=base.revoked_at,
            revoked_by=base.revoked_by,
            revocation_reason=base.revocation_reason,
            constraints=MandateConstraints(
                allowed_tools=base.constraints.allowed_tools,
                time_window=window,
            ),
            source_hash=base.source_hash,
        )

    def test_evaluate_blocks_outside_time_window(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 6: UTC time outside contiguous window → BLOCK 'mandate_outside_time_window'."""
        mandate_id = "time-restricted"
        window = TimeWindow(start_utc=dt_time(9, 0), end_utc=dt_time(17, 0))
        constrained = self._make_constrained_mandate(
            mandate_backend, scope_root, scope, mandate_id, window
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        frozen_midnight = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        with (
            patch.object(mandate_backend, "load_mandate", return_value=constrained),
            patch("atomic_agents.judge.mandate_check.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = frozen_midnight
            proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_outside_time_window")

    def test_evaluate_allows_inside_time_window_contiguous(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 6: current UTC time inside contiguous window → passes time check."""
        mandate_id = "time-ok-contiguous"
        window = TimeWindow(start_utc=dt_time(9, 0), end_utc=dt_time(17, 0))
        constrained = self._make_constrained_mandate(
            mandate_backend, scope_root, scope, mandate_id, window
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        noon_utc = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with (
            patch.object(mandate_backend, "load_mandate", return_value=constrained),
            patch("atomic_agents.judge.mandate_check.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = noon_utc
            proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_evaluate_allows_inside_time_window_midnight_wrap(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 6: 02:00 UTC is inside the 22:00–06:00 wrap-around window → ALLOW."""
        mandate_id = "time-ok-wrap"
        window = TimeWindow(start_utc=dt_time(22, 0), end_utc=dt_time(6, 0))
        constrained = self._make_constrained_mandate(
            mandate_backend, scope_root, scope, mandate_id, window
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        two_am_utc = datetime(2025, 1, 1, 2, 0, 0, tzinfo=timezone.utc)

        with (
            patch.object(mandate_backend, "load_mandate", return_value=constrained),
            patch("atomic_agents.judge.mandate_check.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = two_am_utc
            proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.ALLOW


# NOTE: TestMandateCheckSteps789BudgetStub deleted in PR 3b body — the stub
# it pinned (BLOCK 'mandate_budget_check_unavailable' for any budget-capped
# mandate) is gone now that steps 7-9 are ungated. Replacement tests for the
# new ungated behavior (token + external projection, cap_kind priority,
# step 8/9 precedence) live in TestMandateCheckStep7TokenBudget,
# TestMandateCheckStep8ExternalBudget, TestMandateCheckStep9Escalation, and
# TestMandateCheckCapExceededBlock below.


class TestMandateCheckStepOrdering:
    """Multi-step interaction tests — confirm evaluation order (spec/29 step ordering)."""

    def test_evaluate_step_order_revoked_before_tool_check(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 3 before step 4: revoked mandate + non-allowed tool → BLOCK 'mandate_revoked'."""
        mandate_id = "revoked-with-tool"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            revocation_state="revoked",
            allowed_tools=["only_this"],
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="other_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_revoked"), (
            f"Expected step 3 reason (mandate_revoked) but got: {judgment.reason!r}"
        )

    def test_evaluate_step_order_existence_before_state(
        self, mandate_check: MandateCheck
    ):
        """Step 1 before step 3: non-existent mandate → BLOCK 'mandate_not_found'."""
        proposal = make_proposal_citing("nonexistent-id-xyz")
        judgment = mandate_check.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_not_found"), (
            f"Expected step 1 reason but got: {judgment.reason!r}"
        )


class TestMandateCheckJudgmentShape:
    """Shape of ALLOW and BLOCK judgments (spec/29 line 390)."""

    def test_evaluate_allow_carries_mandate_source_hash_in_reason(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """ALLOW judgment reason includes source_hash for audit traceability (spec/29 line 390)."""
        mandate_id = "allow-shape"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW
        assert "source_hash=" in judgment.reason

    def test_evaluate_block_carries_mandate_id_in_reason(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """BLOCK judgment reason contains the mandate_id for operator traceability."""
        mandate_id = "block-shape-check"
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            revocation_state="revoked",
            allowed_tools=["test_tool"],
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert mandate_id in judgment.reason


# ──────────────────────────────────────────────────────────────────
# MandateStateManager unit tests


class TestMandateStateManagerTransitions:
    """Lifecycle transition detection (spec/29 §'Lifecycle event deduplication')."""

    def test_compute_transitions_emits_granted_for_new_mandate(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
    ):
        """First call with a new mandate ID → 'mandate_granted' event emitted."""
        mandate_id = "new-mandate"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        events = manager.compute_transitions(mandate_backend.list_mandates(scope))
        granted = [e for e in events if e["event"] == "mandate_granted"]
        assert len(granted) == 1
        assert granted[0]["mandate_id"] == mandate_id

    def test_compute_transitions_emits_revoked_on_active_to_revoked_transition(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
    ):
        """active → revoked transition: 'mandate_revoked' event emitted once."""
        mandate_id = "will-revoke"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.compute_transitions(mandate_backend.list_mandates(scope))

        # Simulate operator revocation
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            revocation_state="revoked",
            allowed_tools=["test_tool"],
        )

        events = manager.compute_transitions(mandate_backend.list_mandates(scope))
        revoked = [e for e in events if e["event"] == "mandate_revoked"]
        assert len(revoked) == 1
        assert revoked[0]["mandate_id"] == mandate_id

    def test_compute_transitions_emits_expired_on_derived_expired_state(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
    ):
        """active → expired (derived from expires_at < now) → 'mandate_expired' event."""
        mandate_id = "will-expire"
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["test_tool"],
            expires_at=future,
        )
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.compute_transitions(mandate_backend.list_mandates(scope))

        # Overwrite with an already-expired date
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        granted = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        _write_mandate(
            scope_root,
            scope,
            mandate_id,
            allowed_tools=["test_tool"],
            granted_at=granted,
            expires_at=past,
        )

        events = manager.compute_transitions(mandate_backend.list_mandates(scope))
        expired = [e for e in events if e["event"] == "mandate_expired"]
        assert len(expired) == 1
        assert expired[0]["mandate_id"] == mandate_id

    def test_compute_transitions_is_silent_on_no_change(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
    ):
        """Second call with same mandates → empty events list (dedup works)."""
        mandate_id = "stable-mandate"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.compute_transitions(
            mandate_backend.list_mandates(scope)
        )  # primes state
        events = manager.compute_transitions(mandate_backend.list_mandates(scope))
        assert events == []

    def test_compute_transitions_persists_state(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
    ):
        """After compute_transitions, stored state reflects new last_seen_state values."""
        mandate_id = "persisted-state"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.compute_transitions(mandate_backend.list_mandates(scope))

        state = mandate_backend.read_state(scope)
        assert mandate_id in state.get("mandates", {})
        assert state["mandates"][mandate_id]["last_seen_state"] == "active"


class TestMandateStateManagerThrottle:
    """Suspicious-rebind throttle persistence (spec/29 §'Suspicious-rebind throttle')."""

    def test_arm_throttle_persists_to_backend(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope: str,
    ):
        """Throttle survives process restart — fresh MandateStateManager sees it.
        Spec/29 §'Crash-restart bypass' (plan-subagent Risk C).
        """
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.arm_rebind_throttle("my-mandate", "run-123", throttle_seconds=3600)

        manager2 = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        assert manager2.is_rebind_throttled("my-mandate", "run-123") is True

    def test_throttle_gc_removes_expired_entries(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope: str,
    ):
        """Expired throttle entries are cleaned up on next write (GC discipline)."""
        expired_state = {
            "schema_version": 1,
            "scope": scope,
            "mandates": {},
            "throttles": {
                "old-mandate": {
                    "agent_run_id": "run-old",
                    "expires_at_iso": (
                        datetime.now(timezone.utc) - timedelta(hours=1)
                    ).isoformat(),
                    "original_state_inconsistent_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
            },
        }
        mandate_backend.write_state(scope, expired_state)

        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.compute_transitions([])  # triggers a write → GC runs

        state = mandate_backend.read_state(scope)
        assert "old-mandate" not in state.get("throttles", {}), (
            "Expired throttle entry should have been GC'd on next write"
        )

    def test_throttle_only_matches_same_agent_run(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope: str,
    ):
        """Throttle is per-(mandate_id, agent_run_id) — different run_id is NOT throttled."""
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.arm_rebind_throttle("some-mandate", "run-A", throttle_seconds=3600)

        assert manager.is_rebind_throttled("some-mandate", "run-A") is True
        assert manager.is_rebind_throttled("some-mandate", "run-B") is False

    def test_throttle_unsupported_schema_raises(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope: str,
    ):
        """State with schema_version=999 → MandateStateSchemaUnsupported (spec/29 MUST #7)."""
        bad_state = {
            "schema_version": 999,
            "scope": scope,
            "mandates": {},
            "throttles": {},
        }
        mandate_backend.write_state(scope, bad_state)

        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        with pytest.raises(MandateStateSchemaUnsupported):
            manager.compute_transitions([])


# ──────────────────────────────────────────────────────────────────
# TargetExtractorRegistry unit tests


class TestTargetExtractorRegistry:
    """TargetExtractorRegistry unit tests (spec/29 §'Target extraction')."""

    def test_builtin_extractors_pre_registered(self):
        """All 7 built-in extractors are pre-registered at construction time."""
        registry = TargetExtractorRegistry()
        expected = {
            "recipient_to",
            "recipient_field",
            "target_field",
            "url_field",
            "repository_field",
            "customer_id_field",
            "channel_id_field",
        }
        registered = set(registry.list_names())
        assert expected.issubset(registered), (
            f"Missing built-in extractors: {expected - registered}"
        )

    def test_register_raises_on_collision(self):
        """register() on an already-registered name raises ValueError."""
        registry = TargetExtractorRegistry()
        registry.register("my_extractor", lambda args: args.get("x"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register("my_extractor", lambda args: args.get("y"))

    def test_replace_allows_overwrite(self):
        """replace() succeeds even if name is already registered."""
        registry = TargetExtractorRegistry()
        registry.register("overwritable", lambda args: "original")
        registry.replace("overwritable", lambda args: "replaced")
        result = registry.extract("tool", {"x": 1}, "overwritable")
        assert result == "replaced"

    def test_extract_with_explicit_extractor_id(self):
        """Named extractor mode: registered extractor called with tool_arguments."""
        registry = TargetExtractorRegistry()
        registry.register("my_email_extractor", lambda args: args.get("email_to"))
        result = registry.extract(
            "send_email", {"email_to": "alice@example.com"}, "my_email_extractor"
        )
        assert result == "alice@example.com"

    def test_extract_with_unknown_extractor_id_raises_UnknownTargetExtractor(self):
        """Named extractor mode: unregistered extractor_id raises UnknownTargetExtractor."""
        registry = TargetExtractorRegistry()
        with pytest.raises(UnknownTargetExtractor, match="not registered"):
            registry.extract("some_tool", {"x": 1}, "not_registered_extractor")

    def test_extract_heuristic_fallback(self):
        """Heuristic mode: 'to' field matched first in priority order."""
        registry = TargetExtractorRegistry()
        result = registry.extract(
            "send_email",
            {"to": "bob@example.com", "recipient": "other@example.com"},
        )
        assert result == "bob@example.com"  # 'to' wins over 'recipient'

    def test_extract_mcp_server_prefix(self):
        """MCP tool: extracted target prefixed 'mcp:<server>:<value>'."""
        registry = TargetExtractorRegistry()
        result = registry.extract(
            "send_email",
            {"to": "alice@example.com"},
            mcp_server="gmail",
        )
        assert result == "mcp:gmail:alice@example.com"

    def test_extract_returns_none_when_no_heuristic_matches(self):
        """Heuristic mode: no recognized field → None (fail-closed for MandateCheck step 5)."""
        registry = TargetExtractorRegistry()
        result = registry.extract("mystery_tool", {"unknown_field": "value"})
        assert result is None

    def test_extract_handles_extractor_exception_returns_none(self):
        """Extractor that raises → returns None (fail-closed per spec/29)."""
        registry = TargetExtractorRegistry()
        registry.register("exploding_extractor", lambda args: 1 / 0)
        result = registry.extract("some_tool", {"x": 1}, "exploding_extractor")
        assert result is None


# ──────────────────────────────────────────────────────────────────
# CostEstimatorRegistry unit tests


class TestCostEstimatorRegistry:
    """CostEstimatorRegistry unit tests (spec/29 §'Validation steps' step 8).

    Mirrors TestTargetExtractorRegistry shape; the key behavioral differences:
    - No built-in estimators ship by default (external-cost projection is too
      tool-specific for a "guess from arg shape" heuristic to be safe).
    - Missing estimator → +inf (fail-closed), distinct from target-extractor
      heuristic-fallback-to-None.
    - estimator_id None → +inf (no projection available), NOT an error.
    """

    def test_no_builtin_estimators_at_construction(self):
        """Registry is empty at construction — no built-ins ship by default."""
        registry = CostEstimatorRegistry()
        assert not registry.has("anything")
        # estimate() with None estimator_id → +inf (fail-closed)
        assert registry.estimate("some_tool", {"x": 1}) == float("inf")

    def test_register_raises_on_collision(self):
        """register() on an already-registered name raises ValueError."""
        registry = CostEstimatorRegistry()
        registry.register("my_estimator", lambda args: 0.01)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("my_estimator", lambda args: 0.02)

    def test_register_rejects_invalid_name_chars(self):
        """register() rejects names with non-alphanumeric/underscore characters."""
        registry = CostEstimatorRegistry()
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            registry.register("bad-name", lambda args: 0.01)
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            registry.register("bad.name", lambda args: 0.01)
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            registry.register("", lambda args: 0.01)

    def test_replace_allows_overwrite(self):
        """replace() succeeds even if name is already registered."""
        registry = CostEstimatorRegistry()
        registry.register("overwritable", lambda args: 0.10)
        registry.replace("overwritable", lambda args: 0.99)
        assert registry.estimate("tool", {"x": 1}, "overwritable") == 0.99

    def test_estimate_calls_registered_estimator_with_arguments(self):
        """Registered estimator receives tool_arguments and projects USD cost."""
        registry = CostEstimatorRegistry()
        registry.register(
            "tokens_to_usd",
            lambda args: args.get("token_count", 0) * 0.000015,
        )
        result = registry.estimate(
            "openai_call",
            {"token_count": 1000},
            "tokens_to_usd",
        )
        assert result == pytest.approx(0.015)

    def test_estimate_with_unknown_estimator_id_raises(self):
        """Unknown estimator_id raises UnknownCostEstimator (loud failure on misconfig)."""
        registry = CostEstimatorRegistry()
        with pytest.raises(UnknownCostEstimator, match="not registered"):
            registry.estimate("some_tool", {"x": 1}, "not_registered")

    def test_estimate_with_none_id_returns_inf(self):
        """No estimator_id → +inf (caller fails-closed to mandate_external_cost_unprojectable)."""
        registry = CostEstimatorRegistry()
        result = registry.estimate("some_tool", {"x": 1}, None)
        assert result == float("inf")

    def test_estimate_handles_estimator_exception_returns_inf(self):
        """Estimator that raises → +inf (fail-closed per spec/29 line 380)."""
        registry = CostEstimatorRegistry()
        registry.register("exploding", lambda args: 1 / 0)
        result = registry.estimate("some_tool", {"x": 1}, "exploding")
        assert result == float("inf")

    def test_estimate_handles_non_numeric_return_returns_inf(self):
        """Estimator returning non-numeric → +inf (defense against mis-implementation)."""
        registry = CostEstimatorRegistry()
        registry.register("bad_return", lambda args: "not a number")
        result = registry.estimate("some_tool", {"x": 1}, "bad_return")
        assert result == float("inf")

    def test_estimate_accepts_int_return_coerces_to_float(self):
        """Estimator returning int is coerced to float."""
        registry = CostEstimatorRegistry()
        registry.register("int_return", lambda args: 5)
        result = registry.estimate("some_tool", {"x": 1}, "int_return")
        assert isinstance(result, float)
        assert result == 5.0


# ──────────────────────────────────────────────────────────────────
# ToolRegistry cost_estimator_id validation


class TestToolRegistryCostEstimatorValidation:
    """ToolRegistry.register() validation of cost_estimator_id (spec/29 §'Registration order discipline').

    Mirrors the existing target_extractor_id validation block — surfaces operator
    misconfiguration at register time rather than silently fail-closing at
    MandateCheck step 8 evaluation time.
    """

    def test_register_with_unknown_cost_estimator_id_raises(self):
        """Registering a tool whose cost_estimator_id is missing → UnknownCostEstimator."""
        from atomic_agents.tools import ToolDefinition, ToolRegistry

        registry = ToolRegistry()
        estimators = CostEstimatorRegistry()
        tool = ToolDefinition(
            name="paid_api_call",
            description="Calls a paid API",
            input_schema={"type": "object"},
            handler=lambda args: "ok",
            cost_estimator_id="nonexistent",
        )
        with pytest.raises(UnknownCostEstimator, match="not registered"):
            registry.register(tool, cost_estimator_registry=estimators)

    def test_register_with_registered_cost_estimator_id_succeeds(self):
        """Registering a tool with a known cost_estimator_id succeeds."""
        from atomic_agents.tools import ToolDefinition, ToolRegistry

        registry = ToolRegistry()
        estimators = CostEstimatorRegistry()
        estimators.register("my_estimator", lambda args: 0.01)
        tool = ToolDefinition(
            name="paid_api_call",
            description="Calls a paid API",
            input_schema={"type": "object"},
            handler=lambda args: "ok",
            cost_estimator_id="my_estimator",
        )
        # Should NOT raise.
        registry.register(tool, cost_estimator_registry=estimators)
        assert "paid_api_call" in registry.list_names()

    def test_register_without_cost_estimator_id_succeeds_without_registry(self):
        """Tools that don't declare a cost_estimator_id register cleanly even
        when no estimator registry is supplied."""
        from atomic_agents.tools import ToolDefinition, ToolRegistry

        registry = ToolRegistry()
        tool = ToolDefinition(
            name="free_tool",
            description="A free tool",
            input_schema={"type": "object"},
            handler=lambda args: "ok",
        )
        registry.register(tool)  # no kwargs at all
        assert "free_tool" in registry.list_names()

    def test_register_with_expected_external_cost_usd_field(self):
        """ToolDefinition.expected_external_cost_usd is accepted as static-cost
        alternative to cost_estimator_id (spec/29 §"Token-cost projection")."""
        from atomic_agents.tools import ToolDefinition, ToolRegistry

        registry = ToolRegistry()
        tool = ToolDefinition(
            name="static_paid_api",
            description="Fixed-price API",
            input_schema={"type": "object"},
            handler=lambda args: "ok",
            expected_external_cost_usd=0.05,
        )
        registry.register(tool)
        registered = registry.get("static_paid_api")
        assert registered.expected_external_cost_usd == 0.05
        assert registered.cost_estimator_id is None


# ──────────────────────────────────────────────────────────────────
# AtomicAgent integration tests


def _make_minimal_agent_dir(agents_root: Path, agent_name: str = "scout") -> Path:
    """Minimum on-disk layout for AtomicAgent construction.

    ``agents_root`` is the directory CONTAINING agent subdirs.
    Returns the agent's own dir (``agents_root / agent_name``).
    AtomicAgent is constructed as ``AtomicAgent(name=agent_name, agents_root=agents_root)``.
    """
    agent_root = agents_root / agent_name
    (agent_root / "persona").mkdir(parents=True)
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    (agent_root / "tools.md").write_text(
        "# Tools\n\n## Read paths\n\n- ~/scout/data\n",
        encoding="utf-8",
    )
    (agent_root / "model.md").write_text(
        "# Model\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
        "```yaml\ncost_guardrails:\n  enabled: true\n  daily_cap_usd: 5.0\n"
        "  monthly_cap_usd: 100.0\n```\n",
        encoding="utf-8",
    )
    (agent_root / "memory").mkdir()
    return agent_root


class TestAtomicAgentMandateCheckIntegration:
    """AtomicAgent-level integration: public API + dispatch ensemble wiring."""

    def test_atomic_agent_register_target_extractor_public_api(self, tmp_path: Path):
        """agent.register_target_extractor('custom', fn) registers the extractor successfully."""
        from atomic_agents import AtomicAgent

        agents_root = tmp_path
        _make_minimal_agent_dir(agents_root, "scout")
        agent = AtomicAgent(name="scout", agents_root=agents_root)

        def custom_fn(args):
            return args.get("custom_key")

        agent.register_target_extractor("custom_key_extractor", custom_fn)
        assert agent._target_extractors.has("custom_key_extractor")

    def test_atomic_agent_register_cost_estimator_public_api(self, tmp_path: Path):
        """agent.register_cost_estimator('name', fn) registers the estimator successfully."""
        from atomic_agents import AtomicAgent

        agents_root = tmp_path
        _make_minimal_agent_dir(agents_root, "scout")
        agent = AtomicAgent(name="scout", agents_root=agents_root)
        agent.register_cost_estimator(
            "tokens_to_usd", lambda args: args.get("tokens", 0) * 0.000015
        )
        assert agent._cost_estimators.has("tokens_to_usd")
        # Cost estimators are per-agent — start empty (no built-ins) and only
        # contain what this agent has explicitly registered.
        assert not agent._cost_estimators.has("never_registered")

    def test_atomic_agent_dispatch_includes_mandate_check_when_proposal_cites_mandate(
        self, tmp_path: Path
    ):
        """_ensure_mandate_check constructs a real MandateCheck when agent has a mandate backend.
        Mandate-citing proposals get the spec/29 composition [PolicyJudge, MandateCheck, LLMCatchAll].
        """
        from atomic_agents import AtomicAgent

        agents_root = tmp_path
        _make_minimal_agent_dir(agents_root, "scout")
        backend = FilesystemMandateBackend(agents_root)
        agent = AtomicAgent(
            name="scout", agents_root=agents_root, mandate_backend=backend
        )

        mandate_id = "test-dispatch-mandate"
        _write_mandate(
            agents_root, "agent:scout", mandate_id, allowed_tools=["test_tool"]
        )

        # _ensure_mandate_check constructs a real MandateCheck
        mc = agent._ensure_mandate_check()
        assert mc is not None
        assert isinstance(mc, MandateCheck)

        # Verify the ensemble branch: cites_mandate fires on 'mandate:' prefix
        proposal = make_proposal_citing(
            mandate_id, tool_name="test_tool", actor_agent="scout"
        )
        assert (
            proposal.authorization is not None
            and proposal.authorization.granted_by.startswith("mandate:")
        )

    def test_atomic_agent_dispatch_omits_mandate_check_when_proposal_does_not_cite_mandate(
        self, tmp_path: Path
    ):
        """Non-mandate proposals: cites_mandate=False → MandateCheck not in ensemble.
        Zero behavior change for existing operators (spec/29 §'backward compat').
        """
        from atomic_agents import AtomicAgent

        agents_root = tmp_path
        _make_minimal_agent_dir(agents_root, "scout")
        backend = FilesystemMandateBackend(agents_root)
        AtomicAgent(name="scout", agents_root=agents_root, mandate_backend=backend)

        proposal = _make_proposal_no_auth()
        cites_mandate = (
            proposal.authorization is not None
            and proposal.authorization.granted_by.startswith("mandate:")
        )
        assert cites_mandate is False

    def test_atomic_agent_unknown_target_extractor_id_fails_at_tool_register(
        self, tmp_path: Path
    ):
        """ToolDefinition with target_extractor_id='unregistered' → UnknownTargetExtractor
        at tool_registry.register() (spec/29 §'Registration order discipline').
        """
        from atomic_agents import AtomicAgent
        from atomic_agents.tools import ToolDefinition

        agents_root = tmp_path
        _make_minimal_agent_dir(agents_root, "scout")
        agent = AtomicAgent(name="scout", agents_root=agents_root)

        tool_def = ToolDefinition(
            name="bad_tool",
            description="A tool with an invalid extractor reference",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: "result",
            classification="read_only",
            target_extractor_id="unregistered_extractor_xyz",
        )
        with pytest.raises(UnknownTargetExtractor, match="unregistered_extractor_xyz"):
            agent.tool_registry.register(
                tool_def,
                target_extractor_registry=agent._target_extractors,
            )


# ──────────────────────────────────────────────────────────────────
# Step 7 — Token budget (PR 3b ungated)
#
# Strategy: construct a Mandate directly with the desired constraints,
# then patch mandate_backend.load_mandate to return it.  The null_log_backend
# fixture returns [] on query() so prior_spend is always 0.


def _make_token_capped_mandate(
    mandate_id: str,
    *,
    daily_token_usd: float | None = None,
    monthly_token_usd: float | None = None,
    cumulative_token_usd: float | None = None,
    requires_escalation_above_token_usd: float | None = None,
    requires_escalation_above_external_usd: float | None = None,
    allowed_tools: frozenset[str] = frozenset(["test_tool"]),
    scope: str = "agent:test-agent",
) -> Mandate:
    """Build a Mandate with the given token budget constraints."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    return Mandate(
        mandate_id=mandate_id,
        scope=scope,
        granted_by="test-operator@example.com",
        granted_at=now,
        expires_at=now + timedelta(days=30),
        revocation_state=RevocationState.ACTIVE,
        revoked_at=None,
        revoked_by=None,
        revocation_reason=None,
        constraints=MandateConstraints(
            allowed_tools=allowed_tools,
            daily_token_usd=daily_token_usd,
            monthly_token_usd=monthly_token_usd,
            cumulative_token_usd=cumulative_token_usd,
            requires_escalation_above_token_usd=requires_escalation_above_token_usd,
            requires_escalation_above_external_usd=requires_escalation_above_external_usd,
        ),
        source_hash="sha256:aaaa",
    )


def _make_external_capped_mandate(
    mandate_id: str,
    *,
    daily_external_usd: float | None = None,
    monthly_external_usd: float | None = None,
    cumulative_external_usd: float | None = None,
    requires_escalation_above_external_usd: float | None = None,
    requires_escalation_above_token_usd: float | None = None,
    allowed_tools: frozenset[str] = frozenset(["test_tool"]),
    scope: str = "agent:test-agent",
) -> Mandate:
    """Build a Mandate with the given external budget constraints."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    return Mandate(
        mandate_id=mandate_id,
        scope=scope,
        granted_by="test-operator@example.com",
        granted_at=now,
        expires_at=now + timedelta(days=30),
        revocation_state=RevocationState.ACTIVE,
        revoked_at=None,
        revoked_by=None,
        revocation_reason=None,
        constraints=MandateConstraints(
            allowed_tools=allowed_tools,
            daily_external_usd=daily_external_usd,
            monthly_external_usd=monthly_external_usd,
            cumulative_external_usd=cumulative_external_usd,
            requires_escalation_above_external_usd=requires_escalation_above_external_usd,
            requires_escalation_above_token_usd=requires_escalation_above_token_usd,
        ),
        source_hash="sha256:bbbb",
    )


class TestMandateCheckStep7TokenBudget:
    """Step 7: token budget checks (spec/29 line 372-380, ungated in PR 3b)."""

    def test_step7_allows_when_no_token_cap_set_on_mandate(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7: mandate with only external caps + token-only action → ALLOW."""
        mandate_id = "no-token-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        # Use a mandate with no caps (no token or external limits) → ALLOW
        mandate_no_caps = _make_token_capped_mandate(mandate_id)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(
            mandate_backend, "load_mandate", return_value=mandate_no_caps
        ):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_step7_blocks_when_daily_token_cap_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7: daily_token_usd=0.01 + default projection ($0.10) → BLOCK mandate_cap_would_exceed."""
        mandate_id = "daily-token-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_token_capped_mandate(mandate_id, daily_token_usd=0.01)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_cap_would_exceed" in judgment.reason

    def test_step7_blocks_when_monthly_token_cap_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7: monthly_token_usd=0.01 + default projection ($0.10) → BLOCK."""
        mandate_id = "monthly-token-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_token_capped_mandate(mandate_id, monthly_token_usd=0.01)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_cap_would_exceed" in judgment.reason

    def test_step7_blocks_when_cumulative_token_cap_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7: cumulative_token_usd=0.01 + default projection ($0.10) → BLOCK."""
        mandate_id = "cumulative-token-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_token_capped_mandate(mandate_id, cumulative_token_usd=0.01)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_cap_would_exceed" in judgment.reason

    def test_step7_blocks_fail_closed_when_reservation_store_unreadable(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7 (#497): a LogBackendReadError from the reservation read must
        fail CLOSED — BLOCK with mandate_token_reservations_unreadable — not fall
        into the broad except's fail-OPEN zeroing.

        Negative control: the cap is generous (daily_token_usd=10.0), so with the
        pre-#497 fail-open path (LogBackendReadError → except Exception →
        reservations=[]) cumulative stays under cap and the proposal is ALLOWED.
        The typed guard turns the read failure into a BLOCK regardless of cap, so
        this test FAILS (ALLOW) if the `except LogBackendReadError` guard is
        removed. The broad backstop / full mandate fail-closed posture is #506.
        """
        from atomic_agents.exceptions import LogBackendReadError

        mandate_id = "token-reservations-unreadable"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_token_capped_mandate(mandate_id, daily_token_usd=10.0)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with (
            patch.object(mandate_backend, "load_mandate", return_value=mandate),
            patch(
                "atomic_agents.judge.mandate_check.compute_outstanding",
                side_effect=LogBackendReadError("simulated cost-log read failure"),
            ),
        ):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_token_reservations_unreadable" in judgment.reason

    def test_step7_allows_when_under_cap(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7: daily_token_usd=10.0 + default projection $0.10 + no prior spend → ALLOW."""
        mandate_id = "under-daily-token-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_token_capped_mandate(mandate_id, daily_token_usd=10.0)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_step7_first_iteration_falls_back_to_expected_cost_per_call_usd(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7: empty log + expected_cost_per_call_usd=0.5 → projection = 0.5.

        A cap of 0.4 (below 0.5) should BLOCK; a cap of 0.6 should ALLOW.
        """
        mandate_id = "fallback-expected-cost"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        # Cap at 0.4 — projection 0.5 exceeds it → BLOCK
        mandate_tight = _make_token_capped_mandate(mandate_id, daily_token_usd=0.4)
        mc_tight = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=null_log_backend,
            expected_cost_per_call_usd=0.5,
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate_tight):
            j_tight = mc_tight.evaluate(proposal)
        assert j_tight.outcome == JudgmentOutcome.BLOCK

        # Cap at 0.6 — projection 0.5 is under → ALLOW
        mandate_wide = _make_token_capped_mandate(mandate_id, daily_token_usd=0.6)
        mc_wide = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=null_log_backend,
            expected_cost_per_call_usd=0.5,
        )
        with patch.object(mandate_backend, "load_mandate", return_value=mandate_wide):
            j_wide = mc_wide.evaluate(proposal)
        assert j_wide.outcome == JudgmentOutcome.ALLOW

    def test_step7_first_iteration_falls_back_to_default_when_no_expected_cost(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 7: empty log + no expected_cost_per_call_usd → projection = $0.10 (default)."""
        mandate_id = "default-fallback"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        # Cap at 0.05 — below $0.10 default → BLOCK
        mandate = _make_token_capped_mandate(mandate_id, daily_token_usd=0.05)
        mc = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=null_log_backend,
            expected_cost_per_call_usd=None,  # explicit None → default $0.10
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK

    def test_step7_stale_preceding_cost_event_falls_back_to_default(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Risk 2 pin: prior cost event with ts before iteration_start_ts → stale baseline.

        The stale event (ts < iteration_start_ts) must NOT drive the projection.
        Instead, the code falls back to expected_cost_per_call_usd.
        A stale cost event of $0.001 with iteration_start_ts in the future
        is treated as 'no prior event' → defaults to $0.10.
        """
        mandate_id = "stale-event-risk2"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        from unittest.mock import MagicMock

        # Mock log backend that returns a single record with ts "earlier than now"
        stale_ts = "2000-01-01T00:00:00+00:00"  # ancient — always before any iteration_start_ts
        future_iteration_ts = datetime.now(timezone.utc).isoformat()

        stale_record = MagicMock()
        stale_record.ts = stale_ts
        stale_record.cost_usd = 0.001  # would be under any cap if used
        stale_record.extra = {}
        stale_record.mandate_id = mandate_id

        mock_log = MagicMock()
        mock_log.append = MagicMock()
        mock_log.query = MagicMock(return_value=[stale_record])

        # Cap at 0.05 — below $0.10 default but above $0.001 stale.
        # If stale event drove projection → ALLOW. If default drives → BLOCK.
        mandate = _make_token_capped_mandate(mandate_id, daily_token_usd=0.05)
        mc = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=mock_log,
            expected_cost_per_call_usd=None,  # default $0.10
            iteration_start_ts=future_iteration_ts,  # stale record is before this
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        # Stale event must not drive projection — default $0.10 > cap $0.05 → BLOCK
        assert judgment.outcome == JudgmentOutcome.BLOCK, (
            "Risk 2: stale cost event should not drive projection; "
            "expected $0.10 default to trigger BLOCK"
        )

    def test_step7_outstanding_reservations_count_toward_cumulative(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        tmp_path: Path,
    ):
        """Step 7: open reservation for the same mandate counts toward cumulative cap."""
        from atomic_agents.logs import FilesystemLogBackend

        mandate_id = "reservation-cumulative"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        log = FilesystemLogBackend(tmp_path)
        # Emit a reservation of $0.08 — when cap is $0.09, projection $0.05 alone
        # doesn't exceed, but projection + reservation ($0.13) does.
        mgr = MandateReservationManager(log, scope)
        mgr.create(mandate_id, "prop-xyz", "token", 0.08)
        mgr.shutdown()

        mandate = _make_token_capped_mandate(mandate_id, cumulative_token_usd=0.09)
        mc = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=log,
            expected_cost_per_call_usd=0.05,
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK


# ──────────────────────────────────────────────────────────────────
# Step 8 — External budget (PR 3b ungated)

# Helper: build a ToolDefinition for step 8 tests


def _make_tool_def_with_static_cost(tool_name: str, external_cost_usd: float | None):
    """Build a minimal ToolDefinition with an optional static external cost."""
    from atomic_agents.tools import ToolDefinition

    return ToolDefinition(
        name=tool_name,
        description="test external tool",
        input_schema={"type": "object"},
        handler=lambda args: "ok",
        expected_external_cost_usd=external_cost_usd,
    )


def _make_mc_with_tool(
    mandate_backend: FilesystemMandateBackend,
    scope: str,
    null_log_backend: Any,
    tool_name: str,
    external_cost_usd: float | None,
) -> MandateCheck:
    """Convenience builder for a MandateCheck wired with a tool registry."""
    from atomic_agents.tools import ToolRegistry

    registry = ToolRegistry()
    tool_def = _make_tool_def_with_static_cost(tool_name, external_cost_usd)
    registry.register(tool_def)

    return MandateCheck(
        mandate_backend=mandate_backend,
        scope=scope,
        target_extractor_registry=TargetExtractorRegistry(),
        mandate_state_manager=MandateStateManager(
            mandate_backend=mandate_backend, scope=scope
        ),
        mandate_settings=MandateSettings(),
        log_backend=null_log_backend,
        tool_registry=registry,
    )


class TestMandateCheckStep8ExternalBudget:
    """Step 8: external budget checks (spec/29 line 381, ungated in PR 3b)."""

    def test_step8_allows_when_no_external_cap_set_on_mandate(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: mandate with no external caps → ALLOW regardless of tool cost."""
        mandate_id = "no-ext-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_token_capped_mandate(mandate_id)  # no external caps
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 99.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_step8_blocks_when_daily_external_cap_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: daily_external_usd=0.01 + tool static cost $1.00 → BLOCK."""
        mandate_id = "daily-ext-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_external_capped_mandate(mandate_id, daily_external_usd=0.01)
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_cap_would_exceed" in judgment.reason

    def test_step8_blocks_fail_closed_when_reservation_store_unreadable(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8 (#497): a LogBackendReadError from the external reservation read
        must fail CLOSED — BLOCK with mandate_external_reservations_unreadable —
        not fall into the broad except's fail-OPEN zeroing.

        Negative control: cap is generous (daily_external_usd=100.0) and the tool
        is projectable (static $1.00), so absent the read failure this ALLOWs;
        the typed guard turns the read failure into a BLOCK, so this FAILS (ALLOW)
        if the `except LogBackendReadError` guard is removed. Full posture #506.
        """
        from atomic_agents.exceptions import LogBackendReadError

        mandate_id = "ext-reservations-unreadable"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_external_capped_mandate(mandate_id, daily_external_usd=100.0)
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with (
            patch.object(mandate_backend, "load_mandate", return_value=mandate),
            patch(
                "atomic_agents.judge.mandate_check.compute_outstanding",
                side_effect=LogBackendReadError("simulated cost-log read failure"),
            ),
        ):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_external_reservations_unreadable" in judgment.reason

    def test_step8_blocks_when_monthly_external_cap_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: monthly_external_usd=0.01 + tool static cost $1.00 → BLOCK."""
        mandate_id = "monthly-ext-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_external_capped_mandate(mandate_id, monthly_external_usd=0.01)
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK

    def test_step8_blocks_when_cumulative_external_cap_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: cumulative_external_usd=0.01 + tool static cost $1.00 → BLOCK."""
        mandate_id = "cumulative-ext-cap"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_external_capped_mandate(
            mandate_id, cumulative_external_usd=0.01
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK

    def test_step8_unprojectable_tool_blocks_with_mandate_external_cost_unprojectable(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: tool with no cost estimator AND no static cost + external cap → BLOCK with mandate_external_cost_unprojectable."""
        from atomic_agents.tools import ToolDefinition, ToolRegistry

        mandate_id = "unprojectable-tool"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["mystery_tool"])

        # Tool with neither cost_estimator_id nor expected_external_cost_usd
        registry = ToolRegistry()
        tool = ToolDefinition(
            name="mystery_tool",
            description="tool with no cost info",
            input_schema={"type": "object"},
            handler=lambda args: "ok",
        )
        registry.register(tool)

        # Mandate must allow mystery_tool (otherwise step 4 blocks before step 8)
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        mandate = Mandate(
            mandate_id=mandate_id,
            scope=scope,
            granted_by="test-op",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["mystery_tool"]),
                daily_external_usd=0.5,
            ),
            source_hash="sha256:unprojectable",
        )
        mc = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=null_log_backend,
            tool_registry=registry,
        )
        proposal = make_proposal_citing(mandate_id, tool_name="mystery_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_external_cost_unprojectable" in judgment.reason

    def test_step8_static_expected_external_cost_usd_drives_projection_when_no_estimator_id(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: static expected_external_cost_usd drives projection when no estimator."""
        mandate_id = "static-ext-cost"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["static_tool"])

        # Static cost $0.50; cap $1.00 → ALLOW
        mandate_wide = _make_external_capped_mandate(
            mandate_id,
            daily_external_usd=1.0,
            allowed_tools=frozenset(["static_tool"]),
        )
        mc_wide = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "static_tool", 0.5
        )
        proposal = make_proposal_citing(mandate_id, tool_name="static_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate_wide):
            j_wide = mc_wide.evaluate(proposal)
        assert j_wide.outcome == JudgmentOutcome.ALLOW

        # Static cost $0.50; cap $0.10 → BLOCK
        mandate_tight = _make_external_capped_mandate(
            mandate_id,
            daily_external_usd=0.10,
            allowed_tools=frozenset(["static_tool"]),
        )
        mc_tight = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "static_tool", 0.5
        )
        with patch.object(mandate_backend, "load_mandate", return_value=mandate_tight):
            j_tight = mc_tight.evaluate(proposal)
        assert j_tight.outcome == JudgmentOutcome.BLOCK

    def test_step8_cost_estimator_returns_inf_fails_closed_with_unprojectable_reason(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: registered estimator returns +inf → BLOCK with mandate_external_cost_unprojectable."""
        from atomic_agents.tools import ToolDefinition, ToolRegistry
        from atomic_agents.judge.cost_estimator_registry import CostEstimatorRegistry

        mandate_id = "inf-estimator"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["inf_tool"])

        estimators = CostEstimatorRegistry()
        estimators.register("inf_estimator", lambda args: float("inf"))

        registry = ToolRegistry()
        tool = ToolDefinition(
            name="inf_tool",
            description="tool whose estimator returns inf",
            input_schema={"type": "object"},
            handler=lambda args: "ok",
            cost_estimator_id="inf_estimator",
        )
        registry.register(tool, cost_estimator_registry=estimators)

        mandate = _make_external_capped_mandate(
            mandate_id,
            daily_external_usd=100.0,
            allowed_tools=frozenset(["inf_tool"]),
        )
        mc = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=null_log_backend,
            tool_registry=registry,
            cost_estimator_registry=estimators,
        )
        proposal = make_proposal_citing(mandate_id, tool_name="inf_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert "mandate_external_cost_unprojectable" in judgment.reason

    def test_step8_cost_estimator_used_when_estimator_id_set(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 8: registered estimator returns a finite value → used for cap evaluation."""
        from atomic_agents.tools import ToolDefinition, ToolRegistry
        from atomic_agents.judge.cost_estimator_registry import CostEstimatorRegistry

        mandate_id = "real-estimator"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["est_tool"])

        estimators = CostEstimatorRegistry()
        estimators.register("cheap_estimator", lambda args: 0.02)

        registry = ToolRegistry()
        tool = ToolDefinition(
            name="est_tool",
            description="tool with a real estimator",
            input_schema={"type": "object"},
            handler=lambda args: "ok",
            cost_estimator_id="cheap_estimator",
        )
        registry.register(tool, cost_estimator_registry=estimators)

        # Cap $1.00 → ALLOW (estimated $0.02 < $1.00)
        mandate = _make_external_capped_mandate(
            mandate_id,
            daily_external_usd=1.0,
            allowed_tools=frozenset(["est_tool"]),
        )
        mc = MandateCheck(
            mandate_backend=mandate_backend,
            scope=scope,
            target_extractor_registry=TargetExtractorRegistry(),
            mandate_state_manager=MandateStateManager(
                mandate_backend=mandate_backend, scope=scope
            ),
            mandate_settings=MandateSettings(),
            log_backend=null_log_backend,
            tool_registry=registry,
            cost_estimator_registry=estimators,
        )
        proposal = make_proposal_citing(mandate_id, tool_name="est_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW


# ──────────────────────────────────────────────────────────────────
# Step 9 — Escalation thresholds


class TestMandateCheckStep9Escalation:
    """Step 9: escalation thresholds (spec/29 line 382-384, ungated in PR 3b)."""

    def test_step9_escalates_when_token_threshold_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 9: projected token cost > requires_escalation_above_token_usd → ESCALATE."""
        mandate_id = "token-escalation"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        # Escalation threshold $0.01, default projection $0.10 → exceeds
        mandate = _make_token_capped_mandate(
            mandate_id, requires_escalation_above_token_usd=0.01
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ESCALATE
        assert "mandate_escalation_threshold_hit_token" in judgment.reason

    def test_step9_escalates_when_external_threshold_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 9: projected external cost > requires_escalation_above_external_usd → ESCALATE."""
        mandate_id = "ext-escalation"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        # External threshold $0.01, static tool cost $1.00 → exceeds
        mandate = _make_external_capped_mandate(
            mandate_id, requires_escalation_above_external_usd=0.01
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ESCALATE
        assert "mandate_escalation_threshold_hit_external" in judgment.reason

    def test_step9_no_escalation_when_thresholds_not_set(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Step 9: neither requires_escalation_above_* set + all caps OK → ALLOW."""
        mandate_id = "no-escalation"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = _make_token_capped_mandate(mandate_id, daily_token_usd=100.0)
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_step9_escalate_preempts_step8_block_when_both_fire(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Risk 7 pin: external cap exceeded (would BLOCK) AND external threshold exceeded (would ESCALATE) → ESCALATE wins.

        step 9 ESCALATE must preempt step 8 BLOCK per spec/29 line 384.
        """
        from datetime import timedelta

        mandate_id = "step9-preempts-step8"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        now = datetime.now(timezone.utc)
        # Both: daily_external_usd=0.01 (cap, tool cost $1.00 would exceed)
        # AND requires_escalation_above_external_usd=0.005 (threshold, $1.00 > $0.005)
        mandate = Mandate(
            mandate_id=mandate_id,
            scope=scope,
            granted_by="test-op",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["test_tool"]),
                daily_external_usd=0.01,  # cap: $1.00 tool cost exceeds → would BLOCK
                requires_escalation_above_external_usd=0.005,  # threshold: $1.00 > $0.005 → ESCALATE
            ),
            source_hash="sha256:risk7",
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ESCALATE, (
            "Risk 7: step 9 ESCALATE must preempt step 8 BLOCK when both fire"
        )


# ──────────────────────────────────────────────────────────────────
# Cap exceeded block — priority + shape invariants


class TestMandateCheckProjectionCache:
    """Round 1 Finding 6: MandateCheck caches projections so the agent loop
    can create a reservation with non-zero ``projected_usd``. Without this
    cache, the stale-budget race defense in compute_outstanding is vacuous —
    outstanding reservations contribute 0 to cumulative, and concurrent
    mandate-citing actions all see "no outstanding spend" and exceed the cap
    silently.
    """

    def test_record_projection_then_pop_returns_values(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope: str,
        null_log_backend: Any,
    ):
        """Direct API: record_projection caches; pop_projection returns + clears."""
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        mc.record_projection(
            "prop-123", projected_token_usd=0.07, projected_external_usd=12.5
        )
        token, external = mc.pop_projection("prop-123")
        assert token == 0.07
        assert external == 12.5
        # Second pop returns defaults (cleared on first pop).
        token2, external2 = mc.pop_projection("prop-123")
        assert token2 == 0.0
        assert external2 == 0.0

    def test_pop_projection_no_record_returns_zeros(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope: str,
        null_log_backend: Any,
    ):
        """Defensive default for proposals that never went through evaluate()."""
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        token, external = mc.pop_projection("never-recorded")
        assert token == 0.0
        assert external == 0.0


class TestMandateCheckCapExceededBlock:
    """mandate_cap_exceeded_block priority, shape, and reason invariants (Risk 1)."""

    def _make_both_capped_mandate(
        self,
        mandate_id: str,
        scope: str,
        *,
        daily_token_usd: float | None = None,
        monthly_token_usd: float | None = None,
        cumulative_token_usd: float | None = None,
        daily_external_usd: float | None = None,
        monthly_external_usd: float | None = None,
        cumulative_external_usd: float | None = None,
    ) -> Mandate:
        """Mandate with both token and external caps for priority tests."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        return Mandate(
            mandate_id=mandate_id,
            scope=scope,
            granted_by="test-op",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["test_tool"]),
                daily_token_usd=daily_token_usd,
                monthly_token_usd=monthly_token_usd,
                cumulative_token_usd=cumulative_token_usd,
                daily_external_usd=daily_external_usd,
                monthly_external_usd=monthly_external_usd,
                cumulative_external_usd=cumulative_external_usd,
            ),
            source_hash="sha256:priority-test",
        )

    def test_cap_kind_priority_external_over_token(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Risk 1 pin: daily_token + monthly_external both exceeded → primary cap_kind == 'monthly_external_usd'.

        Priority order: monthly_external > daily_external > cumulative_external >
        monthly_token > daily_token > cumulative_token > per_action_max.
        """
        mandate_id = "priority-ext-over-token"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        # Both daily_token ($0.01 < $0.10 projection) and monthly_external ($0.01 < $1.00 tool)
        mandate = self._make_both_capped_mandate(
            mandate_id,
            scope,
            daily_token_usd=0.01,
            monthly_external_usd=0.01,
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")

        # Capture the emit_event call to inspect cap_kind
        captured_extra: dict = {}
        original_emit = mc._emit_event

        def capturing_emit(event_name, *, proposal, mandate_id, extra=None):
            if event_name == "mandate_cap_exceeded_block" and extra:
                captured_extra.update(extra)
            return original_emit(
                event_name, proposal=proposal, mandate_id=mandate_id, extra=extra
            )

        mc._emit_event = capturing_emit

        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert captured_extra.get("cap_kind") == "monthly_external_usd", (
            f"Risk 1: expected 'monthly_external_usd' but got {captured_extra.get('cap_kind')!r}"
        )
        # daily_token_usd should appear in additional_caps_exceeded
        additional = captured_extra.get("additional_caps_exceeded", ())
        assert "daily_token_usd" in additional

    def test_cap_kind_priority_monthly_over_daily_within_external(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Risk 1 pin: daily_external + monthly_external both exceeded → primary == 'monthly_external_usd'."""
        mandate_id = "monthly-over-daily-ext"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        mandate = self._make_both_capped_mandate(
            mandate_id,
            scope,
            daily_external_usd=0.01,
            monthly_external_usd=0.01,
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")

        captured_extra: dict = {}
        original_emit = mc._emit_event

        def capturing_emit(event_name, *, proposal, mandate_id, extra=None):
            if event_name == "mandate_cap_exceeded_block" and extra:
                captured_extra.update(extra)
            return original_emit(
                event_name, proposal=proposal, mandate_id=mandate_id, extra=extra
            )

        mc._emit_event = capturing_emit

        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            mc.evaluate(proposal)

        assert captured_extra.get("cap_kind") == "monthly_external_usd"
        assert "daily_external_usd" in captured_extra.get(
            "additional_caps_exceeded", ()
        )

    def test_cap_kind_priority_cumulative_after_monthly_and_daily(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Risk 1 pin: cumulative_external alone exceeded (no monthly/daily) → primary == 'cumulative_external_usd'."""
        mandate_id = "cumulative-only"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        mandate = self._make_both_capped_mandate(
            mandate_id,
            scope,
            cumulative_external_usd=0.01,
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")

        captured_extra: dict = {}
        original_emit = mc._emit_event

        def capturing_emit(event_name, *, proposal, mandate_id, extra=None):
            if event_name == "mandate_cap_exceeded_block" and extra:
                captured_extra.update(extra)
            return original_emit(
                event_name, proposal=proposal, mandate_id=mandate_id, extra=extra
            )

        mc._emit_event = capturing_emit

        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            mc.evaluate(proposal)

        assert captured_extra.get("cap_kind") == "cumulative_external_usd"

    def test_additional_caps_exceeded_tuple_lists_all_exceeded_caps_when_multiple_fire(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """All exceeded caps appear in additional_caps_exceeded when multiple fire."""
        mandate_id = "multi-cap-exceed"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        mandate = self._make_both_capped_mandate(
            mandate_id,
            scope,
            monthly_external_usd=0.01,
            daily_external_usd=0.01,
            cumulative_external_usd=0.01,
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")

        captured_extra: dict = {}
        original_emit = mc._emit_event

        def capturing_emit(event_name, *, proposal, mandate_id, extra=None):
            if event_name == "mandate_cap_exceeded_block" and extra:
                captured_extra.update(extra)
            return original_emit(
                event_name, proposal=proposal, mandate_id=mandate_id, extra=extra
            )

        mc._emit_event = capturing_emit

        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            mc.evaluate(proposal)

        additional = set(captured_extra.get("additional_caps_exceeded", ()))
        assert "daily_external_usd" in additional
        assert "cumulative_external_usd" in additional

    def test_additional_caps_exceeded_empty_when_only_one_cap_exceeded(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """additional_caps_exceeded is empty when only one cap is exceeded."""
        mandate_id = "single-cap-exceed"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        mandate = self._make_both_capped_mandate(
            mandate_id,
            scope,
            daily_external_usd=0.01,
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")

        captured_extra: dict = {}
        original_emit = mc._emit_event

        def capturing_emit(event_name, *, proposal, mandate_id, extra=None):
            if event_name == "mandate_cap_exceeded_block" and extra:
                captured_extra.update(extra)
            return original_emit(
                event_name, proposal=proposal, mandate_id=mandate_id, extra=extra
            )

        mc._emit_event = capturing_emit

        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            mc.evaluate(proposal)

        additional = captured_extra.get("additional_caps_exceeded", ())
        assert len(additional) == 0

    def test_cap_exceeded_block_reason_for_external_side_effect_class(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """EXTERNAL_SIDE_EFFECT action class + cap exceeded → reason is 'mandate_cap_would_exceed'."""
        mandate_id = "ext-side-effect-block"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._make_both_capped_mandate(
            mandate_id, scope, daily_external_usd=0.01
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        # make_proposal_citing uses EXTERNAL_SIDE_EFFECT by default
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cap_would_exceed")

    def test_cap_exceeded_escalate_reason_for_high_risk_class(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """HIGH_RISK action class + cap exceeded → ESCALATE with reason 'mandate_cap_would_exceed_high_risk'."""
        from dataclasses import replace
        from atomic_agents.judge.types import ActionClass as JudgeActionClass

        mandate_id = "high-risk-escalate"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._make_both_capped_mandate(
            mandate_id, scope, daily_external_usd=0.01
        )
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )
        base_proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        # Override classification to HIGH_RISK
        proposal = replace(base_proposal, classification=JudgeActionClass.HIGH_RISK)
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.ESCALATE
        assert judgment.reason.startswith("mandate_cap_would_exceed_high_risk")

    def test_block_reasons_are_forever_stable_no_pr_identifier(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """BLOCK reason naming discipline: no reason contains PR identifiers or version suffixes.

        Replacement for the deleted PR 3a stub test — asserts the forever-stable
        reason contract per spec/29 §'BLOCK reason naming discipline'.
        """
        mandate_id = "stable-reason-check"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])

        all_known_reasons = [
            "no_mandate_cite",
            "mandate_rebind_suspicious_throttled",
            "mandate_not_found",
            "mandate_invalid",
            "mandate_revoked",
            "mandate_expired",
            "mandate_tool_not_allowed",
            "mandate_target_unextractable",
            "mandate_target_not_allowed",
            "mandate_target_blocked",
            "mandate_outside_time_window",
            "mandate_cap_would_exceed",
            "mandate_cap_would_exceed_high_risk",
            "mandate_external_cost_unprojectable",
            "mandate_escalation_threshold_hit_token",
            "mandate_escalation_threshold_hit_external",
            # #506 — new third reason for prior-spend/projection read failures
            "mandate_token_reservations_unreadable",
            "mandate_external_reservations_unreadable",
            "mandate_cost_unreadable",
        ]
        forbidden_fragments = ["_in_3a", "_pr3", "_v2", "_phase_b", "_3b", "_stub"]

        for reason in all_known_reasons:
            for fragment in forbidden_fragments:
                assert fragment not in reason, (
                    f"Reason {reason!r} contains PR-identifier fragment {fragment!r}; "
                    "BLOCK reasons must be forever-stable per spec/29 §'BLOCK reason naming discipline'"
                )


def _query_blind_on(kind: str, error: BaseException, *, occurrence: int = 1):
    """Build a callable ``side_effect`` for ``null_log_backend.query`` that raises
    ``error`` on the ``occurrence``-th query of the given ``kind`` and returns
    ``[]`` for every other query.

    Keyed on query SHAPE + per-kind occurrence, NOT global call position, so it is
    immune to ``compute_outstanding``'s variable query count (1 reservation query
    when there are no create-events, else reservation + cost = 2). That call-count
    drift was the root cause of the list-based ``side_effect`` flakiness the #506
    review flagged (P1): a fixed-length list shifts which query receives the error.

    ``kind``: ``"project"`` (_project_token_cost), ``"token_sum"``
    (_sum_prior_token_cost), or ``"external_sum"`` (_sum_prior_external_cost).
    """
    seen: dict[str, int] = {}

    def _classify(q: Any) -> str:
        # compute_outstanding's reservation/cost queries always set `primitive`
        # (and agent_name); the three cost-read helpers never do. Exclude them
        # first so a cost_source="actor" cost_query is not mistaken for token_sum.
        if getattr(q, "primitive", None) is not None:
            return "other"
        if getattr(q, "limit", None) == 1:
            return "project"  # LogQuery(run_id=..., cost_source="actor", limit=1)
        cost_source = getattr(q, "cost_source", None)
        mandate_id = getattr(q, "mandate_id", None)
        if cost_source == "actor" and mandate_id is not None:
            return "token_sum"  # LogQuery(cost_source="actor", mandate_id=...)
        if cost_source is None and mandate_id is not None:
            return "external_sum"  # LogQuery(mandate_id=...)
        return "other"

    def _dispatch(q: Any) -> list:
        k = _classify(q)
        seen[k] = seen.get(k, 0) + 1
        if k == kind and seen[k] == occurrence:
            raise error
        return []

    return _dispatch


class TestMandateCheckCostUnreadable:
    """#506 — blind-read fail-closed posture: prior-spend / projection read failures.

    Covers the three helpers (_sum_prior_token_cost, _sum_prior_external_cost,
    _project_token_cost) plus the evaluate()-level try/except that converts
    _MandateCostUnreadable → BLOCK reason='mandate_cost_unreadable'.

    Negative controls per feedback_layered_except_typed_branch_false_green and
    feedback_false_green_test_needs_per_invocation_negative_control:
    - each typed-branch test asserts the branch-distinctive WARNING log line
    - each code-defect-propagates test verifies with pytest.raises (not outcome)
    """

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _capped_mandate(mandate_id: str) -> "Mandate":
        """Mandate with a tight daily_token_usd cap (0.01 < $0.10 default projection)."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        return Mandate(
            mandate_id=mandate_id,
            scope="agent:test-agent",
            granted_by="test-operator@example.com",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["test_tool"]),
                daily_token_usd=0.01,  # tight cap: 0.01 < default $0.10 projection
            ),
            source_hash="sha256:capped-506",
        )

    @staticmethod
    def _external_capped_mandate(mandate_id: str) -> "Mandate":
        """Mandate with a tight daily_external_usd cap for external-path tests."""
        from datetime import timedelta
        from atomic_agents.tools import ToolRegistry

        now = datetime.now(timezone.utc)
        return Mandate(
            mandate_id=mandate_id,
            scope="agent:test-agent",
            granted_by="test-operator@example.com",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["test_tool"]),
                daily_external_usd=0.01,  # tight: 0.01 < $1.0 tool cost
            ),
            source_hash="sha256:ext-capped-506",
        )

    # ── _sum_prior_token_cost — narrow read failures ───────────────────────────

    def test_sum_prior_token_cost_log_backend_read_error_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
        caplog: pytest.LogCaptureFixture,
    ):
        """_sum_prior_token_cost: LogBackendReadError → BLOCK mandate_cost_unreadable.

        Negative control: null_log_backend.query returns [] without exception, so
        with the pre-#506 fail-open path (except Exception → return 0.0) the
        proposal would pass the daily_token_usd=0.01 cap (prior cost = 0.0 < cap).
        The narrow-catch flip makes LogBackendReadError → _MandateCostUnreadable →
        BLOCK.  This test FAILS (ALLOW) if the narrow catch is removed and the
        broad except-and-return-0 path is restored.
        """
        from atomic_agents.exceptions import LogBackendReadError as LBRError

        mandate_id = "sum-token-lbr"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._capped_mandate(mandate_id)
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        # Raise on the _sum_prior_token_cost read (shape-keyed, drift-immune);
        # _project_token_cost + compute_outstanding return [].
        null_log_backend.query.side_effect = _query_blind_on(
            "token_sum",
            LBRError("simulated LogBackendReadError in sum_prior_token_cost"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            with caplog.at_level("WARNING", logger="atomic_agents.judge.mandate_check"):
                judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")
        # Distinctive log line — branch identity check per feedback_layered_except
        assert any(
            "step 7" in r.message and "mandate_cost_unreadable" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        ), "Expected WARNING log containing 'step 7' and 'mandate_cost_unreadable'"

    def test_sum_prior_token_cost_os_error_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """_sum_prior_token_cost: OSError (narrow family member) → BLOCK mandate_cost_unreadable."""
        mandate_id = "sum-token-oserror"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._capped_mandate(mandate_id)
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        null_log_backend.query.side_effect = _query_blind_on(
            "token_sum",
            OSError("simulated filesystem OSError in sum_prior_token_cost"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")

    def test_sum_prior_token_cost_sqlite_error_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """_sum_prior_token_cost: sqlite3.DatabaseError (narrow family member) → BLOCK."""
        import sqlite3

        mandate_id = "sum-token-sqlite"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._capped_mandate(mandate_id)
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        null_log_backend.query.side_effect = _query_blind_on(
            "token_sum",
            sqlite3.DatabaseError("simulated sqlite3.DatabaseError"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")

    def test_sum_prior_token_cost_key_error_propagates(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """_sum_prior_token_cost: KeyError (code defect) propagates unchanged.

        A KeyError is NOT in the narrow-catch family — it must propagate as
        itself, not be silently converted to a cost-log BLOCK or a 0.0 return.
        Verifying this with pytest.raises rather than checking judgment.reason
        ensures the typed-branch false-green pattern is avoided.
        """
        mandate_id = "sum-token-keyerror"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._capped_mandate(mandate_id)
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        null_log_backend.query.side_effect = _query_blind_on(
            "token_sum",
            KeyError("code defect: unexpected key in log record"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            with pytest.raises(KeyError):
                mc.evaluate(proposal)

    # ── _sum_prior_external_cost — narrow read failures ───────────────────────

    def test_sum_prior_external_cost_log_backend_read_error_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
        caplog: pytest.LogCaptureFixture,
    ):
        """_sum_prior_external_cost: LogBackendReadError → BLOCK mandate_cost_unreadable.

        Negative control: null_log_backend.query returns [] by default, so with
        the pre-#506 fail-open path the cap wouldn't be exceeded (prior = 0.0).
        This test FAILS (ALLOW) if the narrow catch on _sum_prior_external_cost
        is reverted to except-Exception-return-0.
        """
        from atomic_agents.exceptions import LogBackendReadError as LBRError
        from atomic_agents.tools import ToolRegistry

        mandate_id = "sum-ext-lbr"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._external_capped_mandate(mandate_id)
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )

        # No token cap → _step7 skipped; _step8 calls _sum_prior_external_cost
        # (raises) + compute_outstanding (returns []).  Shape-keyed, drift-immune.
        null_log_backend.query.side_effect = _query_blind_on(
            "external_sum",
            LBRError("simulated LogBackendReadError in sum_prior_external_cost"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            with caplog.at_level("WARNING", logger="atomic_agents.judge.mandate_check"):
                judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")
        assert any(
            "step 8" in r.message and "mandate_cost_unreadable" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        ), "Expected WARNING log containing 'step 8' and 'mandate_cost_unreadable'"

    def test_sum_prior_external_cost_key_error_propagates(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """_sum_prior_external_cost: KeyError (code defect) propagates unchanged."""
        from atomic_agents.tools import ToolRegistry

        mandate_id = "sum-ext-keyerror"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._external_capped_mandate(mandate_id)
        mc = _make_mc_with_tool(
            mandate_backend, scope, null_log_backend, "test_tool", 1.0
        )

        null_log_backend.query.side_effect = _query_blind_on(
            "external_sum",
            KeyError("code defect in external sum"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            with pytest.raises(KeyError):
                mc.evaluate(proposal)

    # ── _project_token_cost — cap-gated fail-close ────────────────────────────

    def test_project_token_cost_read_failure_no_cap_degrades_to_default(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """_project_token_cost read failure + cap_active=False → degrade-to-default, ALLOW.

        The cap_active=False path fires when a token escalation threshold exists
        (so _step7 runs) but NO token budget cap is set.  In that case
        _project_token_cost uses a generous fallback ($0.10 default) instead of
        failing closed — there is no cap to bypass, so spurious blocking is avoided
        (feedback_fail_closed_only_where_theres_something_to_protect).

        Mandate has requires_escalation_above_token_usd=100.0 (way above the $0.10
        default) but no token budget cap.  The read failure degrades to $0.10 and
        the escalation check does NOT fire → ALLOW.
        """
        from atomic_agents.exceptions import LogBackendReadError as LBRError

        mandate_id = "project-no-cap-degrade"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        mandate = Mandate(
            mandate_id=mandate_id,
            scope="agent:test-agent",
            granted_by="test-operator@example.com",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["test_tool"]),
                # NO token budget caps (daily/monthly/cumulative all None) →
                # cap_active=False; _project_token_cost read failure degrades safely
                requires_escalation_above_token_usd=100.0,  # threshold above $0.10 default
            ),
            source_hash="sha256:no-cap-degrade",
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        # _project_token_cost read fails (cap_active=False → degrades to $0.10);
        # every other read returns [].  Projection $0.10 < escalation 100.0 → ALLOW.
        null_log_backend.query.side_effect = _query_blind_on(
            "project",
            LBRError("simulated read failure in preceding-iteration baseline read"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.ALLOW

    def test_project_token_cost_read_failure_with_cap_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
        caplog: pytest.LogCaptureFixture,
    ):
        """_project_token_cost read failure + cap_active=True → BLOCK mandate_cost_unreadable.

        Mandate has a tight daily_token_usd cap (0.01).  When the preceding-
        iteration read fails and a cap is in effect, the gate cannot verify the
        projection is safe → fail-closed.

        Negative control: if cap_active were always False (cap-gating removed),
        the read failure would degrade to $0.10 default and the cap check would
        produce mandate_cap_would_exceed — NOT mandate_cost_unreadable.  This
        test distinguishes the two outcomes.
        """
        from atomic_agents.exceptions import LogBackendReadError as LBRError

        mandate_id = "project-with-cap-block"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._capped_mandate(mandate_id)  # daily_token_usd=0.01
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        # _project_token_cost read fails; cap is active → _MandateCostUnreadable.
        # The exception text deliberately does NOT contain "cap active" so the
        # log-line assertion below can only be satisfied by the branch's OWN
        # label, not by the interpolated exception message (P2-A:
        # feedback_layered_except_typed_branch_false_green — the branch-identity
        # signal must be load-bearing).
        null_log_backend.query.side_effect = _query_blind_on(
            "project",
            LBRError("simulated read failure in preceding-iteration baseline read"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            with caplog.at_level("WARNING", logger="atomic_agents.judge.mandate_check"):
                judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")
        # Distinctive branch log line — "cap active" appears ONLY in the
        # cap-gated branch's own warning (mandate_check.py _project_token_cost),
        # never in the exception text, so this is a load-bearing branch-identity
        # assertion.
        assert any(
            "cap active" in r.message and "mandate_cost_unreadable" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        ), "Expected WARNING log containing 'cap active' and 'mandate_cost_unreadable'"

    # ── cap-exceeded re-read path ─────────────────────────────────────────────

    def test_cap_exceeded_token_sum_reread_failure_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Cap-exceeded verdict: the TOKEN re-read (mandate_check.py:702) raises →
        BLOCK mandate_cost_unreadable, not mandate_cap_would_exceed.

        Per-invocation negative control for the token-side cap-exceeded re-read
        (feedback_false_green_test_needs_per_invocation_negative_control): revert
        _sum_prior_token_cost's raise to a fail-open return-0 and this flips to
        mandate_cap_would_exceed.  The sibling external test below pins line 705.

        For a token-capped mandate the projection ($0.10 default) exceeds the
        0.01 cap, so caps_exceeded is non-empty; the SECOND _sum_prior_token_cost
        call (occurrence 2, the diagnostic re-read) raises.  The dispatcher keys on
        query shape + occurrence, so compute_outstanding's variable query count
        cannot shift which read receives the error.
        """
        from atomic_agents.exceptions import LogBackendReadError as LBRError

        mandate_id = "cap-exceeded-token-reread"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._capped_mandate(mandate_id)  # daily_token_usd=0.01
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        # token_sum occurrence 1 = step-7 read ([]); occurrence 2 = cap-exceeded
        # re-read at line 702 (raises).  Projection $0.10 > 0.01 → caps_exceeded.
        null_log_backend.query.side_effect = _query_blind_on(
            "token_sum",
            LBRError("blind on cap-exceeded token re-read"),
            occurrence=2,
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")

    def test_cap_exceeded_external_sum_reread_failure_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Cap-exceeded verdict: the EXTERNAL re-read (mandate_check.py:705) raises →
        BLOCK mandate_cost_unreadable.

        Per-invocation negative control for the external-side cap-exceeded re-read.
        For a token-capped mandate _step8 is skipped, so the ONLY
        _sum_prior_external_cost call is the cap-exceeded re-read at line 705
        (external_sum occurrence 1).  Revert that helper's raise to a fail-open
        return-0 and this flips to mandate_cap_would_exceed.
        """
        from atomic_agents.exceptions import LogBackendReadError as LBRError

        mandate_id = "cap-exceeded-ext-reread"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        mandate = self._capped_mandate(mandate_id)  # daily_token_usd=0.01
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        # Projection $0.10 > 0.01 cap → caps_exceeded; the external re-read at
        # line 705 (external_sum occurrence 1 — _step8 skipped) raises.
        null_log_backend.query.side_effect = _query_blind_on(
            "external_sum",
            LBRError("blind on cap-exceeded external re-read"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")

    # ── boundary: empty log history → ALLOW (not a read failure) ─────────────

    def test_empty_log_history_allows(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Empty log (query returns []) is NOT a read failure — it is an authoritative
        'no prior spend' answer.  A generous-cap mandate must ALLOW.

        Verifies that the narrow-catch does not conflate absence-of-data with a
        read failure (feedback_fail_closed_catches_base_error_class: empty → []
        is the boundary, not a failure).
        """
        mandate_id = "empty-log-allow"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        mandate = Mandate(
            mandate_id=mandate_id,
            scope="agent:test-agent",
            granted_by="test-operator@example.com",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["test_tool"]),
                daily_token_usd=10.0,  # generous cap — $0.10 default < 10.0
            ),
            source_hash="sha256:empty-log",
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        # null_log_backend.query already returns [] by default (no side_effect needed)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.ALLOW

    # ── documented residual: escalation-only-no-cap SUM over-block (#512) ──────

    def test_escalation_only_no_cap_token_sum_failure_blocks_closed(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """#512 — pins the DELIBERATE escalation-only-no-cap over-block.

        The prior-spend SUM helpers flip fail-closed UNCONDITIONALLY (the #506
        ruling), unlike _project_token_cost (cap-gated).  For a mandate with a
        token escalation threshold but NO cap, _step7 runs and _sum_prior_token_cost
        is read; on a blind read it BLOCKs even though the summed value gates
        nothing here (cumulative is compared only against absent caps, and the
        escalate decision uses the projection).  This is fail-SAFE (over-block,
        never leak) and spec-consistent (spec/29 §"Blind-read fail-closed posture"
        acknowledges the asymmetry).  #512 tracks the optional refinement to
        cap-gate the SUM helpers too; this test pins the current behavior so that
        refinement is a conscious, tested change rather than an accidental one.
        """
        from datetime import timedelta

        from atomic_agents.exceptions import LogBackendReadError as LBRError

        mandate_id = "escalation-only-no-cap-512"
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"])
        now = datetime.now(timezone.utc)
        mandate = Mandate(
            mandate_id=mandate_id,
            scope="agent:test-agent",
            granted_by="test-operator@example.com",
            granted_at=now,
            expires_at=now + timedelta(days=30),
            revocation_state=RevocationState.ACTIVE,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
            constraints=MandateConstraints(
                allowed_tools=frozenset(["test_tool"]),
                # escalation threshold but NO token cap → cap_active=False for the
                # projection, yet the SUM helper still fails closed unconditionally.
                requires_escalation_above_token_usd=100.0,
            ),
            source_hash="sha256:escalation-only-512",
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)

        # _project_token_cost read succeeds ([] → $0.10 default, no raise); the
        # _sum_prior_token_cost read fails → unconditional fail-closed → BLOCK.
        null_log_backend.query.side_effect = _query_blind_on(
            "token_sum",
            LBRError("blind prior-spend read, escalation-only-no-cap mandate"),
        )
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        with patch.object(mandate_backend, "load_mandate", return_value=mandate):
            judgment = mc.evaluate(proposal)

        # Current (deliberate) behavior: BLOCK, not ALLOW/ESCALATE. See #512.
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_cost_unreadable")

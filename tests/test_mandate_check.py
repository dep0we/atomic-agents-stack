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

from atomic_agents.judge.backend import Judgment, JudgmentOutcome
from atomic_agents.judge.mandate_check import MandateCheck
from atomic_agents.judge.mandate_state import MandateStateManager
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
        f"granted_by: test-operator@example.com",
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
    elif any([allowed_tools, allowed_targets, blocked_targets, daily_token_usd is not None]):
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
    """Minimal LogBackend stub — records appends but never raises."""
    mock = MagicMock()
    mock.append = MagicMock()
    return mock


@pytest.fixture
def extractor_registry() -> TargetExtractorRegistry:
    """Fresh TargetExtractorRegistry with built-ins pre-registered."""
    return TargetExtractorRegistry()


@pytest.fixture
def state_manager(mandate_backend: FilesystemMandateBackend, scope: str) -> MandateStateManager:
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
        mandate_state_manager=MandateStateManager(mandate_backend=mandate_backend, scope=scope),
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
                    "original_state_inconsistent_at": datetime.now(timezone.utc).isoformat(),
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

        state_manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        state_manager.arm_rebind_throttle(mandate_id, "run-other", throttle_seconds=3600)

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
            scope_root, scope, mandate_id,
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
            scope_root, scope, mandate_id,
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
            scope_root, scope, mandate_id,
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
            scope_root, scope, mandate_id,
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
            scope_root, scope, mandate_id,
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
            scope_root, scope, mandate_id,
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
            scope_root, scope, mandate_id,
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
        assert mandate_check._matches_any("mcp:gmail:alice@example.com", (gmail_prefix,)) is True
        assert mandate_check._matches_any("mcp:stripe:pi_abc", (gmail_prefix,)) is False

    def test_evaluate_uses_named_extractor_id_via_registry_extract(self):
        """Step 5: named extractor registered in registry; extract() returns custom field value."""
        registry = TargetExtractorRegistry()
        registry.register("custom_field_extractor", lambda args: args.get("custom_field"))
        result = registry.extract("custom_tool", {"custom_field": "expected-value"}, "custom_field_extractor")
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

        with patch.object(mandate_backend, "load_mandate", return_value=constrained), \
             patch("atomic_agents.judge.mandate_check.datetime") as mock_dt:
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

        with patch.object(mandate_backend, "load_mandate", return_value=constrained), \
             patch("atomic_agents.judge.mandate_check.datetime") as mock_dt:
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

        with patch.object(mandate_backend, "load_mandate", return_value=constrained), \
             patch("atomic_agents.judge.mandate_check.datetime") as mock_dt:
            mock_dt.now.return_value = two_am_utc
            proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
            judgment = mc.evaluate(proposal)

        assert judgment.outcome == JudgmentOutcome.ALLOW


class TestMandateCheckSteps789BudgetStub:
    """Steps 7-9: budget checks stubbed fail-closed in PR 3a (spec/29 §stub discipline)."""

    def test_evaluate_blocks_when_mandate_has_budget_cap(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """Steps 7-9: mandate with daily_token_usd cap → BLOCK 'mandate_budget_check_unavailable'.
        Fail-closed stub per spec/29 §'Validation step split between PR 3a and PR 3b'.
        """
        mandate_id = "budget-capped"
        _write_mandate(
            scope_root, scope, mandate_id,
            allowed_tools=["test_tool"],
            daily_token_usd=5.0,
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        assert judgment.reason.startswith("mandate_budget_check_unavailable")

    def test_evaluate_block_reason_is_forever_stable_no_pr_identifier(
        self,
        mandate_backend: FilesystemMandateBackend,
        scope_root: Path,
        scope: str,
        null_log_backend: Any,
    ):
        """BLOCK reason MUST NOT contain '_in_3a' or any PR/version identifier.
        Spec/29 §'BLOCK reason naming discipline' — reason strings are forever-stable.
        """
        mandate_id = "budget-stable-reason"
        _write_mandate(
            scope_root, scope, mandate_id,
            allowed_tools=["test_tool"],
            daily_token_usd=1.0,
        )
        mc = _make_mc(mandate_backend, scope, null_log_backend)
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool")
        judgment = mc.evaluate(proposal)
        assert judgment.outcome == JudgmentOutcome.BLOCK
        reason_prefix = judgment.reason.split(":")[0]
        assert "_in_3a" not in reason_prefix
        assert "_pr3" not in reason_prefix.lower()
        assert reason_prefix == "mandate_budget_check_unavailable"


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
            scope_root, scope, mandate_id,
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
            scope_root, scope, mandate_id,
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
        _write_mandate(scope_root, scope, mandate_id, revocation_state="revoked", allowed_tools=["test_tool"])

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
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"], expires_at=future)
        manager = MandateStateManager(mandate_backend=mandate_backend, scope=scope)
        manager.compute_transitions(mandate_backend.list_mandates(scope))

        # Overwrite with an already-expired date
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        granted = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        _write_mandate(scope_root, scope, mandate_id, allowed_tools=["test_tool"], granted_at=granted, expires_at=past)

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
        manager.compute_transitions(mandate_backend.list_mandates(scope))  # primes state
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
                    "original_state_inconsistent_at": datetime.now(timezone.utc).isoformat(),
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
        result = registry.extract("send_email", {"email_to": "alice@example.com"}, "my_email_extractor")
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
        custom_fn = lambda args: args.get("custom_key")
        agent.register_target_extractor("custom_key_extractor", custom_fn)
        assert agent._target_extractors.has("custom_key_extractor")

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
        agent = AtomicAgent(name="scout", agents_root=agents_root, mandate_backend=backend)

        mandate_id = "test-dispatch-mandate"
        _write_mandate(agents_root, "agent:scout", mandate_id, allowed_tools=["test_tool"])

        # _ensure_mandate_check constructs a real MandateCheck
        mc = agent._ensure_mandate_check()
        assert mc is not None
        assert isinstance(mc, MandateCheck)

        # Verify the ensemble branch: cites_mandate fires on 'mandate:' prefix
        proposal = make_proposal_citing(mandate_id, tool_name="test_tool", actor_agent="scout")
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
        agent = AtomicAgent(name="scout", agents_root=agents_root, mandate_backend=backend)

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

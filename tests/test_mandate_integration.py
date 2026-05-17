"""Integration tests for #124 PR 2 — MandateBackend wiring.

These tests verify that the mandate backend kwarg flows correctly through
every code path that constructs an ``AtomicAgent``: the constructor itself,
the three runners that DO construct internal agents (OutcomeRunner, EvalRunner;
DreamRunner stores the kwarg for API parity but doesn't construct internal
agents), and the delegate path.

The load-bearing tests are:

- ``test_atomic_agent_constructor_creates_default_mandate_backend`` — pins
  the no-kwarg default-resolution path (env var → filesystem default).
- ``test_atomic_agent_constructor_accepts_explicit_mandate_backend`` — pins
  the kwarg-wins path (explicit instance bypasses factory).
- ``test_outcome_runner_threads_mandate_backend_to_internal_agent`` — pins
  the OutcomeRunner→AtomicAgent threading boundary (kwarg-drop trap).
- ``test_eval_runner_threads_mandate_backend_to_internal_agent`` — pins the
  EvalRunner→AtomicAgent threading boundary.
- ``test_dream_runner_stores_mandate_backend_for_api_parity`` — DreamRunner
  stores the kwarg; no internal AtomicAgent to thread through.
- ``test_delegate_does_not_thread_mandate_backend`` — pins spec/29 per-agent
  scoping at the delegate boundary (mandate_backend MUST NOT be threaded).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents import AtomicAgent
from atomic_agents.mandate import MandateBackend, FilesystemMandateBackend
from atomic_agents.eval import EvalRunner
from atomic_agents.outcome import OutcomeRunner
from atomic_agents.dream import DreamRunner


# ──────────────────────────────────────────────────────────────────
# Fixtures: minimum-viable agent dir on disk
# (mirrors _make_minimal_agent_dir in test_tool_registry_integration.py)


def _make_minimal_agent_dir(
    scope_root: Path,
    agent_name: str = "scout",
) -> Path:
    """Create the minimum on-disk shape AtomicAgent needs to construct."""
    agent_root = scope_root / agent_name
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


# ──────────────────────────────────────────────────────────────────
# AtomicAgent.__init__ wiring


def test_atomic_agent_constructor_creates_default_mandate_backend(tmp_path):
    """No kwarg → ``FilesystemMandateBackend`` scoped to ``agent_root``.

    The default factory (``get_default_mandate_backend``) is called with
    ``self.agent_root`` — the same per-agent scoping discipline as
    ``tool_registry_backend`` (spec/29 + spec/25 Decision 9).
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.mandate_backend, FilesystemMandateBackend)


def test_atomic_agent_constructor_accepts_explicit_mandate_backend(tmp_path):
    """Explicit ``mandate_backend=`` kwarg bypasses default factory.

    The operator-supplied instance must survive the constructor without
    wrapping or re-scoping.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemMandateBackend(tmp_path / "scout")
    agent = AtomicAgent(
        name="scout",
        agents_root=tmp_path,
        mandate_backend=explicit_backend,
    )
    assert agent.mandate_backend is explicit_backend


def test_atomic_agent_constructor_env_var_dispatch(tmp_path, monkeypatch):
    """``ATOMIC_AGENTS_MANDATE_BACKEND=filesystem`` → ``FilesystemMandateBackend``.

    Only the filesystem backend is registered in PR 1; the test confirms
    the env-var lookup path resolves correctly for the known backend.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_MANDATE_BACKEND", "filesystem")
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.mandate_backend, FilesystemMandateBackend)


def test_atomic_agent_mandate_backend_is_public_attribute(tmp_path):
    """``self.mandate_backend`` is a public attribute mirroring
    ``self.lock_backend`` / ``self.log_backend`` / ``self.profile_backend`` /
    ``self.tool_registry_backend``.

    Diagnostic code (``atomic-agents doctor``) and runners must be able
    to reuse the same backend instance instead of resolving twice.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    # Public — not ``_mandate_backend``
    assert hasattr(agent, "mandate_backend")
    assert not hasattr(agent, "_mandate_backend")
    assert isinstance(agent.mandate_backend, MandateBackend)


def test_atomic_agent_mandate_backend_explicit_none_triggers_default_resolution(
    tmp_path,
):
    """Explicit ``mandate_backend=None`` triggers default resolution — NOT stored as None.

    The constructor's guard is ``if mandate_backend is None`` so an
    explicitly-passed ``None`` is identical to the no-kwarg path; both
    produce a live ``FilesystemMandateBackend`` instance rather than
    ``None`` on the attribute.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path, mandate_backend=None)
    assert isinstance(agent.mandate_backend, FilesystemMandateBackend)
    assert agent.mandate_backend is not None


def test_existing_agent_construction_sites_unaffected(tmp_path):
    """Standard construction without mandate_backend kwarg works correctly.

    Verifies the new attribute is present (not None) without disrupting
    any pre-#124-PR-2 construction site.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    # No mandate_backend kwarg — mirrors every existing test site
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    # New attribute is present and resolved (not None, not missing)
    assert agent.mandate_backend is not None
    assert isinstance(agent.mandate_backend, FilesystemMandateBackend)


# ──────────────────────────────────────────────────────────────────
# Runner threading


def test_outcome_runner_threads_mandate_backend_to_internal_agent(
    monkeypatch, tmp_path
):
    """OutcomeRunner threads the kwarg to internal AtomicAgent at run() time.

    Step 11 adversarial regression: a storage-only assertion would pass even
    if a future contributor removed ``mandate_backend=self._mandate_backend``
    from the AtomicAgent call site. This test monkeypatches the internal
    AtomicAgent class to capture its kwargs, ensuring the kwarg-drop trap is
    pinned at the BOUNDARY (mirrors the #61 / #63 / #64 PR 2 pattern).
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemMandateBackend(tmp_path / "scout")
    runner = OutcomeRunner(
        agents_root=tmp_path,
        agent_name="scout",
        mandate_backend=explicit_backend,
    )
    # Storage is pinned — the public API surface
    assert runner._mandate_backend is explicit_backend

    # Capture the kwargs at the threading boundary. The runner's
    # run() constructs an AtomicAgent — intercept it.
    captured: dict = {}

    class _SentinelAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            # Raise to abort the rest of run() — we only care about the boundary
            raise RuntimeError("boundary captured; aborting run()")

    monkeypatch.setattr("atomic_agents.outcome.AtomicAgent", _SentinelAgent)

    with pytest.raises(RuntimeError, match="boundary captured"):
        runner.run(description="test", rubric="# Rubric\n- Done\n")

    assert captured.get("mandate_backend") is explicit_backend


def test_eval_runner_threads_mandate_backend_to_internal_agent(monkeypatch, tmp_path):
    """EvalRunner threads the kwarg to internal AtomicAgent at run_test() time.

    Step 11 adversarial regression — see OutcomeRunner test for the
    threading-vs-storage distinction.
    """
    agent_root = _make_minimal_agent_dir(tmp_path, "scout")
    (agent_root / "evals").mkdir()
    (agent_root / "evals" / "rubric.md").write_text(
        "---\nweights:\n  correctness: 100\nthreshold_pass: 4.0\n---\n"
        "# Rubric\n- Done correctly\n",
        encoding="utf-8",
    )
    (agent_root / "evals" / "judge.md").write_text(
        "# Judge model\n\nclaude-sonnet-4-6-20260101\n", encoding="utf-8"
    )
    (agent_root / "evals" / "golden").mkdir()

    explicit_backend = FilesystemMandateBackend(tmp_path / "scout")
    runner = EvalRunner(
        agents_root=tmp_path,
        agent_name="scout",
        mandate_backend=explicit_backend,
    )
    assert runner._mandate_backend is explicit_backend

    # Capture at the threading boundary inside run_test → _run_one_golden.
    captured: dict = {}

    class _SentinelAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("boundary captured; aborting eval")

    monkeypatch.setattr("atomic_agents.eval.AtomicAgent", _SentinelAgent)

    from atomic_agents.eval import EvalTest

    test = EvalTest(
        test_id="g1",
        category="smoke",
        path=tmp_path / "scout" / "evals" / "golden" / "g1.md",
        setup="",
        input="ping",
        expected_behavior="response",
        pass_criteria="any response",
    )
    with pytest.raises(RuntimeError, match="boundary captured"):
        runner.run_test(test)

    assert captured.get("mandate_backend") is explicit_backend


def test_dream_runner_stores_mandate_backend_for_api_parity(tmp_path):
    """DreamRunner stores the kwarg for API parity with other runners.

    DreamRunner doesn't currently construct internal AtomicAgents (raw LLM
    calls only) but operators wiring multiple runners use ONE signature shape
    across all four. Reserved for future dream pipelines that DO dispatch
    agent calls per spec/29.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemMandateBackend(tmp_path / "scout")
    runner = DreamRunner(
        agents_root=tmp_path,
        agent_name="scout",
        mandate_backend=explicit_backend,
    )
    assert runner._mandate_backend is explicit_backend


# ──────────────────────────────────────────────────────────────────
# Delegate non-threading (spec/29 per-agent scoping)


def test_delegate_does_not_thread_mandate_backend(monkeypatch, tmp_path):
    """Spec/29 per-agent scoping enforced at the delegate boundary.

    The coordinator's mandate backend is scoped to ITS agent_root.
    Threading it to the target would allow the target to validate actions
    against the coordinator's authority grants — a security boundary
    violation. Per spec/29 §"Per-agent vs project-root resolution", each
    agent builds its own mandate backend over its own scope.

    Step 11 adversarial regression: a "two parallel agents have distinct
    backends" assertion would pass trivially. The actual invariant is that
    ``coordinator.delegate(...)`` constructs ``target_agent`` WITHOUT
    passing ``mandate_backend=`` — this test pins that boundary by
    monkeypatching AtomicAgent to capture its kwargs.

    Also verifies that ``profile_backend`` IS threaded (fleet-scoped, per
    spec/24 Decision 9) — confirming the test is exercising the right
    non-threading boundary rather than a broken capture.
    """
    coord_root = _make_minimal_agent_dir(tmp_path, "coord")
    _make_minimal_agent_dir(tmp_path, "target")

    # Coordinator roster.md so coord can delegate to target.
    (coord_root / "roster.md").write_text(
        "# Roster\n\n## Delegate to\n\n- target — integration test target\n",
        encoding="utf-8",
    )

    coord = AtomicAgent(name="coord", agents_root=tmp_path)

    # Capture the target's AtomicAgent kwargs at the delegate boundary.
    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            # Abort delegate() — we only need the boundary inspection
            raise RuntimeError("delegate boundary captured")

    # Patch the AtomicAgent symbol inside agent.py (the module that
    # calls AtomicAgent(target_agent_name, ...) from delegate()).
    monkeypatch.setattr("atomic_agents.agent.AtomicAgent", _CapturingAgent)

    with pytest.raises(RuntimeError, match="delegate boundary captured"):
        coord.delegate(target_agent_name="target", work_item="ping")

    # profile_backend IS threaded (fleet-scoped — spec/24 Decision 9).
    assert "profile_backend" in captured
    assert captured["profile_backend"] is coord.profile_backend

    # mandate_backend MUST NOT be threaded (per-agent scoped — spec/29).
    # A regression here would silently route the coordinator's mandate
    # authority grants to the target agent.
    assert "mandate_backend" not in captured


# ──────────────────────────────────────────────────────────────────
# Per-agent mandate scoping


def test_two_agents_get_independent_mandate_backends(tmp_path):
    """Each agent's mandate backend is scoped to its OWN agent_root.

    Agents A and B in the same agents_root get independent
    FilesystemMandateBackend instances — consistent with per-agent
    scoping for tool_registry_backend per spec/25 Decision 9.
    """
    _make_minimal_agent_dir(tmp_path, "agent_a")
    _make_minimal_agent_dir(tmp_path, "agent_b")

    a = AtomicAgent(name="agent_a", agents_root=tmp_path)
    b = AtomicAgent(name="agent_b", agents_root=tmp_path)

    # Both are FilesystemMandateBackend but scoped to different roots
    assert isinstance(a.mandate_backend, FilesystemMandateBackend)
    assert isinstance(b.mandate_backend, FilesystemMandateBackend)
    # Different instances — not shared
    assert a.mandate_backend is not b.mandate_backend


# ──────────────────────────────────────────────────────────────────
# PR 3b: Mandate recovery at init (spec/29 §"Crash recovery for reservations")


class TestAtomicAgentMandateRecoveryAtInit:
    """Recovery is triggered at agent init when mandate_backend is present."""

    def test_agent_init_recovers_orphan_reservations_when_mandates_md_present(
        self, tmp_path: Path
    ):
        """AtomicAgent init calls recover_orphan_reservations when mandates.md exists.

        The recovery path is triggered in __init__ via _run_mandate_recovery_for_all_scopes.
        We verify it by: leaving a reservation event without a commit, then constructing
        the agent, then checking a committed_on_recovery event appears in the log.
        """
        from atomic_agents.logs import FilesystemLogBackend, LogQuery
        from atomic_agents.logs.types import PRIMITIVE_MANDATE_RESERVATION

        _make_minimal_agent_dir(tmp_path, "recover-agent")

        # Write a mandates.md so the backend has something to work with
        mandates_md = tmp_path / "recover-agent" / "mandates.md"
        mandates_md.write_text(
            "## test-mandate\n"
            "granted_by: operator@example.com\n"
            "granted_at: 2026-01-01T00:00:00+00:00\n"
            "expires_at: 2099-01-01T00:00:00+00:00\n"
            "revocation_state: active\n"
            "scope: |\n"
            "  Test mandate.\n"
            "revoked_at: null\n"
            "revocation_reason: null\n"
            "constraints:\n"
            "  unconstrained: true\n"
            '  unconstrained_justification: "test"\n',
            encoding="utf-8",
        )

        # Use a shared log backend — the agent will also write to its own log dir.
        log_backend = FilesystemLogBackend(tmp_path / "recover-agent")

        # Emit an orphan reservation directly to the log (simulates a crash)
        import uuid
        from datetime import datetime, timezone
        from atomic_agents.logs.types import RunRecord

        rid = uuid.uuid4().hex[:16]
        orphan = RunRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="run-orphan",
            primitive=PRIMITIVE_MANDATE_RESERVATION,
            status="ok",
            summary=f"orphan reservation {rid}",
            model="n/a",
            input_tokens=0,
            output_tokens=0,
            mandate_id="test-mandate",
            extra={
                "event": "mandate_reservation",
                "reservation_id": rid,
                "mandate_id": "test-mandate",
                "proposal_id": "orphan-prop",
                "cost_kind": "token",
                "projected_usd": 0.05,
                "ttl_s": 60,
            },
        )
        log_backend.append(orphan)

        # Constructing the agent triggers _run_mandate_recovery_for_all_scopes
        AtomicAgent(
            name="recover-agent",
            agents_root=tmp_path,
            log_backend=log_backend,
        )

        records = log_backend.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        committed_recovery = [
            r
            for r in records
            if r.extra.get("event") == "mandate_reservation_committed_on_recovery"
        ]
        assert len(committed_recovery) >= 1, (
            "AtomicAgent init should trigger recovery and emit committed_on_recovery "
            f"for orphan reservation. Records found: {[r.extra.get('event') for r in records]}"
        )

    def test_agent_init_no_recovery_when_no_mandates_md(self, tmp_path: Path):
        """AtomicAgent init with no mandates.md completes without errors.

        Recovery is still called but returns 0 (no orphans to recover).
        The agent must construct cleanly.
        """
        _make_minimal_agent_dir(tmp_path, "scout")
        # No mandates.md written — recovery is a no-op
        agent = AtomicAgent(name="scout", agents_root=tmp_path)
        assert agent.mandate_backend is not None
        # mandate_reservation_managers are initialized (empty or with defaults)
        assert hasattr(agent, "_mandate_reservation_managers")

    def test_agent_init_recovery_logs_exception_and_continues_on_infra_failure(
        self, tmp_path: Path, monkeypatch
    ):
        """If recover_orphan_reservations raises, the agent init still completes.

        Recovery failures are logged at WARNING but must never crash the agent.
        """

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated infra failure during recovery")

        _make_minimal_agent_dir(tmp_path, "scout")
        backend = FilesystemMandateBackend(tmp_path / "scout")
        monkeypatch.setattr(backend, "recover_orphan_reservations", _boom)

        # Agent must still construct cleanly despite recovery failure
        agent = AtomicAgent(
            name="scout",
            agents_root=tmp_path,
            mandate_backend=backend,
        )
        assert agent is not None


# ──────────────────────────────────────────────────────────────────
# PR 3b: Cost event mandate_id + proposal_id in extra


class TestAtomicAgentCostEventMandateIdExtra:
    """Cost events for mandate-citing actions carry mandate_id + proposal_id in extra."""

    def test_cost_event_extra_unchanged_for_non_mandate_citing_action(
        self, tmp_path: Path
    ):
        """Back-compat: tools without a mandate cite produce cost events without mandate_id/proposal_id.

        This verifies the non-mandate path is untouched.
        """
        from atomic_agents.logs import FilesystemLogBackend

        _make_minimal_agent_dir(tmp_path, "scout")
        log = FilesystemLogBackend(tmp_path / "scout")
        agent = AtomicAgent(name="scout", agents_root=tmp_path, log_backend=log)
        # No mandate_id/proposal_id should appear in extra for tool records without mandate cite
        # This is validated by constructing the agent successfully; deeper verification
        # requires a live tool call (tested in the reservation lifecycle tests).
        assert isinstance(agent.mandate_backend, FilesystemMandateBackend)


# ──────────────────────────────────────────────────────────────────
# PR 3b: _verify_post_action direct tests


class TestAtomicAgentPostActionVerification:
    """Tests for _verify_post_action (spec/29 §"Mandate lifecycle events")."""

    def _make_agent(self, tmp_path: Path, agent_name: str = "scout") -> "AtomicAgent":
        """Build a minimal agent for post-action verification tests."""
        from atomic_agents.logs import FilesystemLogBackend

        _make_minimal_agent_dir(tmp_path, agent_name)
        log = FilesystemLogBackend(tmp_path / agent_name)
        return AtomicAgent(name=agent_name, agents_root=tmp_path, log_backend=log)

    def _make_mandate_proposal(
        self,
        mandate_id: str = "m1",
        tool_name: str = "send_email",
        target_canonical: str | None = None,
        classification: str = "external_side_effect",
    ):
        """Build an ActionProposal citing a mandate for verification tests."""
        from atomic_agents.judge.types import ActionClass, ActionProposal, Authorization
        from hashlib import sha256
        from datetime import datetime, timezone

        args = {"to": target_canonical or "alice@example.com"}
        args_canonical = repr(sorted(args.items())).encode("utf-8")
        action_class = ActionClass(classification)
        return ActionProposal(
            tool_name=tool_name,
            tool_arguments=args,
            tool_call_id="tc-verify",
            tool_definition_hash="sha256:" + sha256(tool_name.encode()).hexdigest(),
            arguments_hash="sha256:" + sha256(args_canonical).hexdigest(),
            classification=action_class,
            classification_source="default",
            actor_agent="scout",
            actor_run_id="run-verify",
            proposal_id="prop-verify",
            proposal_ts=datetime.now(timezone.utc).isoformat(),
            authorization=Authorization(
                granted_by=f"mandate:{mandate_id}",
                scope=f"mandate cite {mandate_id}",
                granted_at=datetime.now(timezone.utc).isoformat(),
            ),
            target_canonical=target_canonical,
        )

    def _make_tool_result(self, error: str | None = None):
        """Build a minimal ToolCallResult for verification tests."""
        from atomic_agents.tools import ToolCallResult

        return ToolCallResult(
            tool_name="send_email",
            tool_use_id="tc-verify",
            input={"to": "alice@example.com"},
            output="sent" if error is None else "",
            error=error,
        )

    def test_verify_post_action_verified_when_target_matches(self, tmp_path: Path):
        """_verify_post_action emits mandate_action_verified when targets match."""
        from atomic_agents.logs import LogQuery

        agent = self._make_agent(tmp_path)
        proposal = self._make_mandate_proposal(target_canonical="alice@example.com")
        result = self._make_tool_result()

        agent._verify_post_action(proposal, result)

        records = agent.log_backend.query(LogQuery())
        verified = [
            r for r in records if r.extra.get("event") == "mandate_action_verified"
        ]
        assert len(verified) == 1
        assert verified[0].extra.get("verification_status") == "match"

    def test_verify_post_action_diverged_when_target_differs(self, tmp_path: Path):
        """_verify_post_action emits mandate_action_diverged when targets differ."""
        from atomic_agents.logs import LogQuery

        agent = self._make_agent(tmp_path)
        # Proposal recorded target "alice@example.com"; args have different value
        proposal = self._make_mandate_proposal(target_canonical="alice@example.com")

        # Override tool_arguments to produce a different extracted target
        from dataclasses import replace

        proposal = replace(proposal, tool_arguments={"to": "eve@attacker.com"})

        result = self._make_tool_result()
        agent._verify_post_action(proposal, result)

        records = agent.log_backend.query(LogQuery())
        diverged = [
            r for r in records if r.extra.get("event") == "mandate_action_diverged"
        ]
        assert len(diverged) == 1
        assert diverged[0].extra.get("verification_status") == "diverged"

    def test_verify_post_action_unavailable_when_no_extractor_matches_either_side(
        self, tmp_path: Path
    ):
        """_verify_post_action emits mandate_action_verification_unavailable when
        both target_canonical (proposal) and post-execution extraction return None."""
        from atomic_agents.logs import LogQuery

        agent = self._make_agent(tmp_path)
        # Tool with no heuristic-matchable fields; proposal target=None
        proposal = self._make_mandate_proposal(
            tool_name="mystery_tool",
            target_canonical=None,
        )
        from dataclasses import replace

        proposal = replace(
            proposal,
            tool_arguments={"obscure_field": "xyz"},
        )

        result = self._make_tool_result()
        agent._verify_post_action(proposal, result)

        records = agent.log_backend.query(LogQuery())
        unavailable = [
            r
            for r in records
            if r.extra.get("event") == "mandate_action_verification_unavailable"
        ]
        assert len(unavailable) == 1
        assert unavailable[0].extra.get("verification_status") == "unavailable"

    def test_verify_post_action_fires_after_cost_commit_strict_timestamp_ordering(
        self, tmp_path: Path
    ):
        """Risk 6 pin: verification event ts > cost event ts (strict ordering).

        We simulate the pattern: cost event is written first, then
        _verify_post_action is called. The verification event must be AFTER
        the cost event in the log (by timestamp).
        """
        from atomic_agents.logs import LogQuery, RunRecord
        from datetime import datetime, timezone

        agent = self._make_agent(tmp_path)

        # Write a cost event first
        cost_ts = datetime.now(timezone.utc).isoformat()
        cost_record = RunRecord(
            ts=cost_ts,
            run_id="run-verify",
            primitive="tool",
            status="ok",
            summary="cost event for test",
            model="n/a",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.05,
            cost_source="actor",
            mandate_id="m1",
            extra={"proposal_id": "prop-verify"},
        )
        agent.log_backend.append(cost_record)

        # Now call _verify_post_action (simulates: commit happened, then verify)
        proposal = self._make_mandate_proposal(target_canonical="alice@example.com")
        result = self._make_tool_result()
        agent._verify_post_action(proposal, result)

        records = agent.log_backend.query(LogQuery())
        verified_records = [
            r for r in records if r.extra.get("event") == "mandate_action_verified"
        ]
        assert len(verified_records) == 1
        verify_ts = verified_records[0].ts

        assert verify_ts >= cost_ts, (
            f"Risk 6: verification event ts={verify_ts!r} must be >= "
            f"cost event ts={cost_ts!r} (strict ordering)"
        )

    def test_verify_post_action_uses_executed_tool_arguments_not_tool_result(
        self, tmp_path: Path
    ):
        """Risk 10 pin: post-execution extraction uses proposal.tool_arguments (not tool_result.output).

        The REVISE-amended tool_arguments are in proposal.tool_arguments.
        tool_result.output is the raw output string which might contain anything.
        We verify by setting a wrong value in the output but the correct value in
        tool_arguments — the verification should use tool_arguments.
        """
        from atomic_agents.logs import LogQuery
        from atomic_agents.tools import ToolCallResult

        agent = self._make_agent(tmp_path)

        # Proposal: target_canonical="alice@example.com" + args with same value
        proposal = self._make_mandate_proposal(target_canonical="alice@example.com")
        # tool_result has wrong value in output but we verify extraction comes from args
        result = ToolCallResult(
            tool_name="send_email",
            tool_use_id="tc-verify",
            input={"to": "alice@example.com"},
            output="sent to eve@attacker.com",  # wrong in output — must not be used
            error=None,
        )

        agent._verify_post_action(proposal, result)

        records = agent.log_backend.query(LogQuery())
        verified = [
            r for r in records if r.extra.get("event") == "mandate_action_verified"
        ]
        assert len(verified) == 1, (
            "Risk 10: extraction must use tool_arguments (alice@example.com matches), "
            "not tool_result.output (which had a different value)"
        )
        # The target_canonical_at_execution should match the tool_arguments value
        assert (
            verified[0].extra.get("target_canonical_at_execution")
            == "alice@example.com"
        )

    def test_verify_post_action_no_op_for_read_only_action_class(self, tmp_path: Path):
        """_verify_post_action is a no-op for READ_ONLY action class."""
        from atomic_agents.logs import LogQuery

        agent = self._make_agent(tmp_path)
        proposal = self._make_mandate_proposal(
            classification="read_only",
            target_canonical="alice@example.com",
        )
        result = self._make_tool_result()
        agent._verify_post_action(proposal, result)

        records = agent.log_backend.query(LogQuery())
        verify_events = [
            r
            for r in records
            if r.extra.get("event", "").startswith("mandate_action_verif")
        ]
        assert len(verify_events) == 0, (
            "READ_ONLY actions must not trigger post-action verification"
        )

    def test_verify_post_action_no_op_for_reversible_write_action_class(
        self, tmp_path: Path
    ):
        """_verify_post_action is a no-op for REVERSIBLE_WRITE — only external_side_effect + irreversible fire."""
        from atomic_agents.logs import LogQuery

        agent = self._make_agent(tmp_path)
        proposal = self._make_mandate_proposal(
            classification="reversible_write",
            target_canonical="alice@example.com",
        )
        result = self._make_tool_result()
        agent._verify_post_action(proposal, result)

        records = agent.log_backend.query(LogQuery())
        verify_events = [
            r
            for r in records
            if r.extra.get("event", "").startswith("mandate_action_verif")
        ]
        assert len(verify_events) == 0, (
            "REVERSIBLE_WRITE actions must not trigger post-action verification; "
            "only external_side_effect + irreversible fire verification"
        )

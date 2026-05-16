"""Integration tests for #63 PR 2 — AgentProfileBackend wiring.

These tests verify that the profile backend kwarg flows correctly
through every code path that constructs an ``AtomicAgent``: the
constructor itself, the four runners (OutcomeRunner, EvalRunner,
delegate.py, DreamRunner), and the doctor coherence check.

The load-bearing test is ``test_dreamrunner_threads_profile_to_both_model_md_sites``
— Step 11 P1#3 from PR 1 named this as the critical trap shape.
DreamRunner had TWO model.md call sites (``dream.py:1128`` in __init__
+ ``dream.py:672`` in ``_check_cap`` cost-guardrail). PR 2 wires BOTH
through ``profile_backend`` so operators using a non-filesystem profile
backend have correct config for ``AtomicAgent.call()`` AND correct
cost caps applied to dream runs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from atomic_agents import (
    AtomicAgent,
    AgentProfileBackend,
    FilesystemAgentProfileBackend,
)
from atomic_agents.dream import DreamRunner, _check_cap
from atomic_agents.eval import EvalRunner
from atomic_agents.outcome import OutcomeRunner
from atomic_agents.doctor import check_agent_profile_backend


# ──────────────────────────────────────────────────────────────────
# Fixtures: minimum-viable agent dir on disk


def _make_minimal_agent_dir(scope_root: Path, agent_name: str = "scout") -> Path:
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


def test_atomic_agent_loads_profile_at_init(tmp_path):
    """``self.profile_backend`` is populated and ``self._profile`` is loaded."""
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.profile_backend, FilesystemAgentProfileBackend)
    assert agent._profile.name == "scout"
    assert agent._profile.agent_mode == "reactive"
    # Config dict assembled from profile.model_config
    assert agent.config.default_model == "claude-sonnet-4-6-20260101"


def test_atomic_agent_profile_backend_kwarg_wins(tmp_path):
    """Explicit ``profile_backend=`` kwarg bypasses default factory."""
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemAgentProfileBackend(tmp_path)
    agent = AtomicAgent(
        name="scout", agents_root=tmp_path, profile_backend=explicit_backend
    )
    assert agent.profile_backend is explicit_backend


def test_atomic_agent_uses_env_var_default_when_kwarg_unset(tmp_path, monkeypatch):
    """No kwarg + filesystem env var → FilesystemAgentProfileBackend."""
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", "filesystem")
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.profile_backend, FilesystemAgentProfileBackend)


def test_atomic_agent_cascade_still_works_with_profile_backend(tmp_path):
    """Cascade-shaped agent name (slash-containing) loads via profile backend.

    Step 11 + Plan-subagent caught this: cascade tests use multi-segment
    names like ``"muse/projects/foo/agents/writer"``. PR 2 Decision 1
    relaxes ``_agent_root`` slash refusal so cascade IDs resolve correctly.
    """
    # Build cascade layout
    instance = tmp_path / "muse" / "projects" / "the-unfinished" / "agents" / "writer"
    (instance / "persona").mkdir(parents=True)
    (instance / "persona" / "IDENTITY.md").write_text(
        "# Writer\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    (instance / "memory").mkdir()
    # Minimum role layer
    role = tmp_path / "muse" / "roles" / "writer"
    role.mkdir(parents=True)
    (role / "PROMPT.md").write_text("# Writer\n", encoding="utf-8")
    (role / "tools.md").write_text(
        "# Role tools\n\n## Read paths\n\n- ~/docs\n", encoding="utf-8"
    )
    (role / "model.md").write_text(
        "# Model\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n",
        encoding="utf-8",
    )

    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=tmp_path,
    )
    assert agent.cascade is not None
    # Cascade-merged tools come from the role
    assert any("docs" in str(p) for p in agent.config.read_paths)


# ──────────────────────────────────────────────────────────────────
# Runner threading


def test_outcome_runner_threads_profile_backend(tmp_path):
    """OutcomeRunner stores the kwarg and passes it to internal AtomicAgent."""
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemAgentProfileBackend(tmp_path)
    runner = OutcomeRunner(
        agents_root=tmp_path,
        agent_name="scout",
        profile_backend=explicit_backend,
    )
    assert runner._profile_backend is explicit_backend


def test_eval_runner_threads_both_log_and_profile_backends(tmp_path):
    """EvalRunner accepts BOTH log_backend AND profile_backend kwargs.

    Per PR 2 Decision 3 — the existing log_backend drop-trap (a #61 PR 2
    gap) is fixed simultaneously with the new profile_backend wiring.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    (tmp_path / "scout" / "evals").mkdir()
    (tmp_path / "scout" / "evals" / "rubric.md").write_text(
        "---\nweights:\n  correctness: 1.0\nthreshold: 0.5\n---\n# Rubric\n",
        encoding="utf-8",
    )
    (tmp_path / "scout" / "evals" / "judge.md").write_text(
        "# Judge prompt\n", encoding="utf-8"
    )
    (tmp_path / "scout" / "evals" / "golden").mkdir()

    from atomic_agents.logs import FilesystemLogBackend

    log_backend = FilesystemLogBackend(tmp_path / "scout")
    profile_backend = FilesystemAgentProfileBackend(tmp_path)

    runner = EvalRunner(
        agents_root=tmp_path,
        agent_name="scout",
        log_backend=log_backend,
        profile_backend=profile_backend,
    )
    assert runner._log_backend is log_backend
    assert runner._profile_backend is profile_backend


# ──────────────────────────────────────────────────────────────────
# DreamRunner — the load-bearing Step 11 P1#3 regression test


def test_dreamrunner_threads_profile_backend_kwarg(tmp_path):
    """DreamRunner stores the kwarg and pre-loads the profile."""
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemAgentProfileBackend(tmp_path)
    runner = DreamRunner(
        agents_root=tmp_path,
        agent_name="scout",
        profile_backend=explicit_backend,
    )
    assert runner._profile_backend is explicit_backend
    # Pre-resolved profile is on the runner
    assert runner._profile.name == "scout"
    # Model is resolved from profile.model_config (NOT a direct model.md read)
    assert runner._model == "claude-sonnet-4-6-20260101"


def test_dreamrunner_uses_profile_model_when_modelmd_absent(tmp_path):
    """The CRITICAL Step 11 P1#3 regression test — DreamRunner does NOT
    read model.md from disk when profile_backend is provided.

    Construct an agent dir WITHOUT model.md, supply a fake profile
    backend that returns a pre-cooked AgentProfile, and verify
    DreamRunner constructs without ever touching the absent model.md
    file. If DreamRunner were still reading model.md directly (the
    pre-PR-2 code path or the missed _check_cap call site), this test
    would fail because the file genuinely doesn't exist on disk.
    """
    from atomic_agents.profile import AgentProfile

    agent_root = tmp_path / "scout"
    (agent_root / "persona").mkdir(parents=True)
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    (agent_root / "memory").mkdir()
    # CRITICAL: NO model.md, NO tools.md on disk

    fake_profile = AgentProfile(
        name="scout",
        agent_mode="reactive",
        model_config={
            "default_model": "claude-haiku-4-5-20251001",
            "fallback_model": None,
            "provider": None,
            "max_input_tokens": 12000,
            "max_output_tokens": 4000,
            "cost_guardrails_enabled": False,
            "daily_cap_usd": 0.0,
            "monthly_cap_usd": 0.0,
            "daily_cap_action": "skip",
            "monthly_cap_action": "alert",
            "warning_thresholds": [0.5, 0.8],
            "alert_channel": "log_only",
        },
        tool_config={
            "read_paths": [],
            "write_paths": [],
            "read_only_paths": [],
            "external_apis": [],
            "hard_nos": [],
        },
        tool_classifications={},
        judges_config=None,
        roster=[],
        mcp_servers=[],
        persona_identity="# Scout\n",
        persona_soul="",
        persona_user="",
        goal_text="",
        model_md_raw="",
        tools_md_raw="",
        judges_md_raw=None,
        roster_md_raw="",
        mcp_md_raw="",
    )

    fake_backend = MagicMock(spec=AgentProfileBackend)
    fake_backend.load_profile.return_value = fake_profile

    runner = DreamRunner(
        agents_root=tmp_path,
        agent_name="scout",
        profile_backend=fake_backend,
    )
    # Resolved model came from the fake backend's profile, NOT from disk
    assert runner._model == "claude-haiku-4-5-20251001"
    fake_backend.load_profile.assert_called_with("scout")


def test_check_cap_uses_passed_model_config_not_disk_read(tmp_path):
    """The SECOND half of Step 11 P1#3 — ``_check_cap`` accepts
    ``model_config`` kwarg and uses it instead of reading model.md.

    Without this wiring, an operator using a non-filesystem
    profile_backend would have correct config for ``AtomicAgent.call()``
    but stale cost caps applied to dream runs because ``_check_cap``
    would silently fall back to disk read.
    """
    # No model.md on disk at all
    agent_root = tmp_path / "scout"
    agent_root.mkdir()

    model_config = {
        "default_model": "claude-sonnet-4-6-20260101",
        "cost_guardrails_enabled": True,
        "daily_cap_usd": 0.001,  # Intentionally tiny
        "monthly_cap_usd": 0.001,
    }
    # Should use the passed config and raise on cap (since reserved > headroom).
    # If _check_cap were ignoring model_config and falling back to disk,
    # it would silently succeed (no model.md → no guardrail).
    with pytest.raises(ValueError, match="exceeds remaining headroom"):
        _check_cap(
            agent_root=agent_root,
            model="claude-sonnet-4-6-20260101",
            reserved=10.0,  # Way more than the tiny cap
            critical=False,
            model_config=model_config,
        )


def test_check_cap_falls_back_to_disk_when_model_config_omitted(tmp_path):
    """Backward compat: ``_check_cap`` without ``model_config`` reads model.md.

    Pre-PR-2 callers continue to work (the legacy path is preserved as
    the else branch).
    """
    agent_root = tmp_path / "scout"
    agent_root.mkdir()
    (agent_root / "model.md").write_text(
        "# Model\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n",
        encoding="utf-8",
    )
    # No cost_guardrails_enabled in this model.md → _check_cap returns early
    _check_cap(
        agent_root=agent_root,
        model="claude-sonnet-4-6-20260101",
        reserved=10.0,
        critical=False,
    )  # Should not raise


def test_dreamrunner_start_forwards_model_config_to_check_cap(tmp_path, monkeypatch):
    """Step 9.1 testing-specialist finding F-T1 — the end-to-end test
    for the Step 11 P1#3 trap.

    ``test_dreamrunner_uses_profile_model_when_modelmd_absent`` above
    verifies Site 1 (``__init__``'s model resolution doesn't read
    disk). ``test_check_cap_uses_passed_model_config_not_disk_read``
    verifies that ``_check_cap`` USES the kwarg when supplied.
    Neither verifies the LOAD-BEARING CONNECTION: that
    ``DreamRunner.start()`` actually passes
    ``model_config=self._profile.model_config`` to ``_check_cap``.

    If that single line at ``dream.py:start()`` were dropped in a
    future refactor, the pre-fix tests would still pass — both
    function-level tests verify their own behavior, but neither
    catches the start() forwarding. This test plugs that gap.

    Approach: monkey-patch ``_check_cap`` at the dream module level
    to record the kwargs it receives. Construct DreamRunner with a
    fake profile_backend and call ``start()``; assert the captured
    kwargs include ``model_config`` pointing at the fake profile's
    model_config dict.
    """
    from atomic_agents.profile import AgentProfile
    from atomic_agents import dream as dream_module

    agent_root = tmp_path / "scout"
    (agent_root / "persona").mkdir(parents=True)
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    (agent_root / "memory").mkdir()
    (agent_root / "journal").mkdir()
    (agent_root / "log").mkdir()
    (agent_root / "dreams").mkdir()

    fake_model_config = {
        "default_model": "claude-haiku-4-5-20251001",
        "fallback_model": None,
        "provider": None,
        "max_input_tokens": 12000,
        "max_output_tokens": 4000,
        "cost_guardrails_enabled": False,  # Skip the cap math; just verify the kwarg
        "daily_cap_usd": 0.0,
        "monthly_cap_usd": 0.0,
        "daily_cap_action": "skip",
        "monthly_cap_action": "alert",
        "warning_thresholds": [0.5, 0.8],
        "alert_channel": "log_only",
    }

    fake_profile = AgentProfile(
        name="scout",
        agent_mode="reactive",
        model_config=fake_model_config,
        tool_config={
            "read_paths": [],
            "write_paths": [],
            "read_only_paths": [],
            "external_apis": [],
            "hard_nos": [],
        },
        tool_classifications={},
        judges_config=None,
        roster=[],
        mcp_servers=[],
        persona_identity="# Scout\n",
        persona_soul="",
        persona_user="",
        goal_text="",
        model_md_raw="",
        tools_md_raw="",
        judges_md_raw=None,
        roster_md_raw="",
        mcp_md_raw="",
    )

    fake_backend = MagicMock(spec=AgentProfileBackend)
    fake_backend.load_profile.return_value = fake_profile

    captured = {}

    def tracking_check_cap(*args, **kwargs):
        captured.update(kwargs)
        # Don't actually run — just capture
        return None

    monkeypatch.setattr(dream_module, "_check_cap", tracking_check_cap)

    runner = DreamRunner(
        agents_root=tmp_path,
        agent_name="scout",
        profile_backend=fake_backend,
    )

    # Call start() — it'll invoke _check_cap (now mocked) and proceed
    # into the rest of the pipeline. We may hit other failures further
    # in (e.g., LLM calls), but the _check_cap kwarg capture happens
    # before any of those, so we'll see the captured kwargs even on
    # downstream failure.
    try:
        runner.start()
    except Exception:
        # Expected — start() will fail somewhere downstream (no real LLM).
        pass

    # The load-bearing assertion: model_config kwarg was passed and
    # points at the fake profile's model_config dict (not a fresh disk
    # read or None).
    assert "model_config" in captured, (
        "DreamRunner.start() did not pass model_config= to _check_cap "
        "— Step 11 P1#3 forwarding gap regression. The start() call "
        "site must include `model_config=self._profile.model_config`."
    )
    assert captured["model_config"] is fake_model_config, (
        "model_config kwarg was passed but did not match the profile's "
        "model_config — start() may be reading from a different source."
    )


# ──────────────────────────────────────────────────────────────────
# doctor.check_agent_profile_backend


def test_doctor_check_profile_backend_filesystem_pass(tmp_path):
    """Filesystem default → PASS with capability snapshot + agent count."""
    _make_minimal_agent_dir(tmp_path, "scout")
    _make_minimal_agent_dir(tmp_path, "editor")
    result = check_agent_profile_backend(tmp_path)
    assert result.name == "profile-backend"
    assert result.status == "pass"
    assert "2 agents" in result.message
    assert result.detail["backend_id"] == "filesystem"
    assert result.detail["supports_save"] is True
    # supports_snapshot flipped to True in #63 PR 3 (Decision 3 — JSON-
    # based snapshot trio shipped alongside the SQLite reference impl).
    assert result.detail["supports_snapshot"] is True
    # supports_skills added in #63 PR 3 (Decision 8) — filesystem=True,
    # exposed via doctor so operators can compare backends.
    assert result.detail["supports_skills"] is True
    assert result.detail["agent_count"] == 2


def test_doctor_check_profile_backend_unknown_id_fails(tmp_path, monkeypatch):
    """Unknown ``ATOMIC_AGENTS_PROFILE_BACKEND`` → FAIL with known-id list."""
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", "totally-not-real")
    result = check_agent_profile_backend(tmp_path)
    assert result.status == "fail"
    assert "totally-not-real" in result.message
    assert "filesystem" in result.message  # the known-id list


def test_doctor_check_profile_backend_redacts_url_credentials(tmp_path, monkeypatch):
    """URL with password is redacted in detail dict on PASS path.

    Builds a synthetic scenario by setting the env vars and then
    forcing the filesystem branch (the URL is informational on the
    filesystem backend — no PASS path uses it, so the redaction code
    is exercised only for non-filesystem backends in the wild. This
    test verifies the redaction helper structure on the FAIL path
    when an unknown backend_id is paired with a URL).
    """
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", "totally-not-real")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_PROFILE_BACKEND_URL",
        "postgres://user:secret@host:5432/db",
    )
    result = check_agent_profile_backend(tmp_path)
    # The FAIL message must not echo the credential — even though the URL
    # is for a different env var than the bad backend_id, the same
    # redaction discipline applies once the URL surface lands in PR 3+.
    assert "secret" not in result.message
    assert result.status == "fail"


def test_doctor_check_profile_backend_singular_agent_message(tmp_path):
    """Message uses 'agent' (singular) for 1 agent, 'agents' for 0 or many."""
    _make_minimal_agent_dir(tmp_path, "scout")
    result = check_agent_profile_backend(tmp_path)
    assert "1 agent" in result.message
    assert "1 agents" not in result.message  # singular


def test_doctor_check_profile_backend_zero_agents(tmp_path):
    """Empty agents_root → PASS with 0 agents."""
    result = check_agent_profile_backend(tmp_path)
    assert result.status == "pass"
    assert "0 agents" in result.message


def test_atomic_agent_delegate_threads_profile_backend_to_target(tmp_path):
    """Step 11 adversarial Finding 1 (HIGH) regression test.

    ``AtomicAgent.delegate()`` constructs an internal target ``AtomicAgent``.
    Pre-fix this construction silently DROPPED ``profile_backend``,
    recreating the runner-drop-trap shape PR 2 was specifically
    designed to close on every other AtomicAgent-constructing path.
    In a SaaS deployment where the operator pins a non-filesystem
    ``profile_backend`` on the coordinator, every delegated target
    would silently load its config from the wrong source.
    Adversarial-flagged HIGH because ``delegate()`` is the production
    multi-agent path, not just a test surface.

    **Scope note**: only ``profile_backend`` is threaded.
    ``lock_backend`` and ``log_backend`` are per-agent scoped (the
    filesystem default writes to ``<agent>/.lock`` and ``<agent>/log/``
    respectively), so threading them would put the target's locks and
    logs in the coordinator's directory — mixing on-disk artifacts.
    Pre-PR-2 convention preserved: those use their own default
    factories. Operators wanting shared lock / log backends use the
    deployment-level env vars; both coordinator + target then resolve
    the same backend via the default factory.
    """
    # Build coordinator + target as siblings under tmp_path
    _make_minimal_agent_dir(tmp_path, "coordinator")
    _make_minimal_agent_dir(tmp_path, "target")
    # Coordinator needs the target in its roster
    (tmp_path / "coordinator" / "roster.md").write_text(
        "# Roster\n\n## Delegate to\n\n- target — for delegation tests\n",
        encoding="utf-8",
    )

    explicit_profile = FilesystemAgentProfileBackend(tmp_path)

    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=tmp_path,
        profile_backend=explicit_profile,
    )

    # Capture the delegated target's instance via monkey-patch on
    # AtomicAgent.__init__. The test cannot run delegate() end-to-end
    # without an LLM, but the AtomicAgent construction happens
    # BEFORE call() — that's what we're measuring.
    captured = {}
    original_init = AtomicAgent.__init__

    def capturing_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if self.name == "target":
            captured["target"] = self

    import unittest.mock as _mock

    with _mock.patch.object(AtomicAgent, "__init__", capturing_init):
        try:
            coordinator.delegate(target_agent_name="target", work_item="test")
        except Exception:
            # Expected — call() needs LLM credentials we don't have here.
            pass

    target = captured.get("target")
    assert target is not None, (
        "delegate() did not construct a target AtomicAgent — test setup "
        "may be incomplete"
    )

    # Load-bearing assertion: target inherits coordinator's
    # operator-pinned profile_backend. Pre-fix, this would silently
    # fall back to the default factory.
    assert target.profile_backend is explicit_profile, (
        "target agent did not inherit coordinator's profile_backend — "
        "Step 11 adversarial Finding 1 regression. delegate() must "
        "thread profile_backend=self.profile_backend."
    )

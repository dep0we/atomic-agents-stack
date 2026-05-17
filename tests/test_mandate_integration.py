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

import os
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


def test_eval_runner_threads_mandate_backend_to_internal_agent(
    monkeypatch, tmp_path
):
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

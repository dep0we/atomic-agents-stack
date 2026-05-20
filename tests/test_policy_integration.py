"""Integration tests for #89 PR 2 — PolicyBackend wiring.

These tests verify that the policy backend kwarg flows correctly through every
code path that constructs an ``AtomicAgent``: the constructor itself, the four
runners (OutcomeRunner, EvalRunner, delegate.py, DreamRunner), and the doctor
coherence check.

The load-bearing test is ``test_delegate_threads_policy_backend_to_target``
(test #10) — it uses the capturing-init pattern to assert identity (``is``,
not ``isinstance``) at the delegate boundary.  Pre-fix, the target agent would
silently resolve its own policy backend from the env var, meaning a fleet
operator's pinned non-filesystem backend would never reach delegated targets.

Per spec/32 Decision 1: policy_backend IS fleet-scoped and MUST be threaded
to delegate targets, unlike mandate_backend (per-agent scoped, per spec/29).
"""

from __future__ import annotations

import unittest.mock as _mock
from pathlib import Path

import pytest

from atomic_agents import AtomicAgent
from atomic_agents.policy import (
    FilesystemPolicyBackend,
    PolicyBackend,
)
from atomic_agents.doctor import check_policy_backend
from atomic_agents.dream import DreamRunner
from atomic_agents.eval import EvalRunner
from atomic_agents.outcome import OutcomeRunner


# ──────────────────────────────────────────────────────────────────
# Fixtures: minimum-viable agent dir on disk
# (mirrors _make_minimal_agent_dir in test_profile_integration.py,
# test_mandate_integration.py, test_tool_registry_integration.py)


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


def test_atomic_agent_constructor_creates_default_policy_backend(tmp_path):
    """No kwarg → ``FilesystemPolicyBackend`` scoped to ``agents_root``.

    Policy is fleet-scoped (``<agents_root>/policy.md``), not per-agent
    scoped, so the default backend is rooted at ``agents_root`` rather
    than ``agent_root``.  This matches spec/32 Decision 1.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.policy_backend, FilesystemPolicyBackend)


def test_atomic_agent_constructor_accepts_explicit_policy_backend(tmp_path):
    """Explicit ``policy_backend=`` kwarg bypasses default factory.

    The operator-supplied instance must survive the constructor without
    wrapping or re-scoping.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemPolicyBackend(tmp_path)
    agent = AtomicAgent(
        name="scout",
        agents_root=tmp_path,
        policy_backend=explicit_backend,
    )
    assert agent.policy_backend is explicit_backend


def test_atomic_agent_constructor_env_var_dispatch(tmp_path, monkeypatch):
    """``ATOMIC_AGENTS_POLICY_BACKEND=filesystem`` → ``FilesystemPolicyBackend``.

    Only the filesystem backend is registered in PR 1; the test confirms
    the env-var lookup path resolves correctly for the known backend.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_POLICY_BACKEND", "filesystem")
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.policy_backend, FilesystemPolicyBackend)


def test_atomic_agent_policy_backend_is_public_attribute(tmp_path):
    """``self.policy_backend`` is a public attribute mirroring
    ``self.lock_backend`` / ``self.log_backend`` / ``self.profile_backend`` /
    ``self.tool_registry_backend`` / ``self.mandate_backend``.

    Diagnostic code (``atomic-agents doctor``) and runners must be able
    to reuse the same backend instance instead of resolving twice.
    The attribute must be public (not ``_policy_backend``) per the
    established convention across all fleet-scoped backends.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    # Public — not ``_policy_backend``
    assert hasattr(agent, "policy_backend")
    assert not hasattr(agent, "_policy_backend")
    assert isinstance(agent.policy_backend, PolicyBackend)


def test_atomic_agent_policy_backend_explicit_none_triggers_default_resolution(
    tmp_path,
):
    """Explicit ``policy_backend=None`` triggers default resolution — NOT stored as None.

    The constructor's guard is ``if policy_backend is None`` so an
    explicitly-passed ``None`` is identical to the no-kwarg path; both
    produce a live ``FilesystemPolicyBackend`` instance rather than
    ``None`` on the attribute.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path, policy_backend=None)
    assert isinstance(agent.policy_backend, FilesystemPolicyBackend)
    assert agent.policy_backend is not None


def test_existing_agent_construction_sites_unaffected(tmp_path):
    """Standard construction without policy_backend kwarg works correctly.

    Verifies the new attribute is present (not None) without disrupting
    any pre-#89-PR-2 construction site.  This is the umbrella regression
    test that covers all 115 existing AtomicAgent construction sites.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    # No policy_backend kwarg — mirrors every existing test site
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    # New attribute is present and resolved (not None, not missing)
    assert agent.policy_backend is not None
    assert isinstance(agent.policy_backend, FilesystemPolicyBackend)


# ──────────────────────────────────────────────────────────────────
# Runner threading


def test_outcome_runner_threads_policy_backend_to_internal_agent(monkeypatch, tmp_path):
    """OutcomeRunner threads the kwarg to internal AtomicAgent at run() time.

    Step 11 adversarial regression: a storage-only assertion would pass even
    if a future contributor removed ``policy_backend=self._policy_backend``
    from the AtomicAgent call site.  This test monkeypatches the internal
    AtomicAgent class to capture its kwargs, ensuring the kwarg-drop trap is
    pinned at the BOUNDARY (mirrors the #61 / #63 / #64 / #124 PR 2 pattern).
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemPolicyBackend(tmp_path)
    runner = OutcomeRunner(
        agents_root=tmp_path,
        agent_name="scout",
        policy_backend=explicit_backend,
    )
    # Storage is pinned — the public API surface
    assert runner._policy_backend is explicit_backend

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

    assert captured.get("policy_backend") is explicit_backend


def test_eval_runner_threads_policy_backend_to_internal_agent(monkeypatch, tmp_path):
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

    explicit_backend = FilesystemPolicyBackend(tmp_path)
    runner = EvalRunner(
        agents_root=tmp_path,
        agent_name="scout",
        policy_backend=explicit_backend,
    )
    assert runner._policy_backend is explicit_backend

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

    assert captured.get("policy_backend") is explicit_backend


def test_dream_runner_stores_policy_backend_for_api_parity(tmp_path):
    """DreamRunner stores the kwarg for API parity with other runners.

    DreamRunner doesn't construct internal AtomicAgents (raw LLM calls only)
    but operators wiring multiple runners use ONE signature shape across all
    four. Reserved for future dream pipelines that DO dispatch agent calls
    per spec/32.

    Verify: ``dream_runner._policy_backend is explicit_backend`` (identity).
    Do NOT try to trigger AtomicAgent construction — DreamRunner has none.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemPolicyBackend(tmp_path)
    runner = DreamRunner(
        agents_root=tmp_path,
        agent_name="scout",
        policy_backend=explicit_backend,
    )
    assert runner._policy_backend is explicit_backend


# ──────────────────────────────────────────────────────────────────
# Delegate threading — THE LOAD-BEARING TEST (spec/32 Decision 1)


def test_delegate_threads_policy_backend_to_target(tmp_path):
    """Spec/32 Decision 1: policy_backend IS fleet-scoped and MUST be threaded.

    **THE LOAD-BEARING TEST per F3 + D1.**  Uses the capturing-init pattern
    from ``test_profile_integration.py`` to assert identity (``is``, not
    ``isinstance``) at the delegate boundary.

    Pre-fix, the target agent would silently call ``get_default_policy_backend``
    from the env var instead of inheriting the coordinator's fleet-pinned
    instance.  In a SaaS deployment, that means every delegated target would
    miss the operator's policy configuration.

    Contrast with mandate_backend (per-agent scoped, per spec/29 — NOT
    threaded).  Policy is fleet-wide by design; mandate authority is per-agent
    for security isolation.
    """
    coord_root = _make_minimal_agent_dir(tmp_path, "coordinator")
    _make_minimal_agent_dir(tmp_path, "target")

    # Coordinator roster.md so coord can delegate to target.
    (coord_root / "roster.md").write_text(
        "# Roster\n\n## Delegate to\n\n- target — integration test target\n",
        encoding="utf-8",
    )

    explicit_policy = FilesystemPolicyBackend(tmp_path)

    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=tmp_path,
        policy_backend=explicit_policy,
    )

    # Use the capturing-init pattern: monkeypatch AtomicAgent.__init__
    # so the target's construction is intercepted BEFORE call() needs LLM
    # credentials.  We capture the self after __init__ completes so we can
    # inspect the wired attribute directly.
    captured: dict = {}
    original_init = AtomicAgent.__init__

    def capturing_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if getattr(self, "name", None) == "target":
            captured["target"] = self

    with _mock.patch.object(AtomicAgent, "__init__", capturing_init):
        try:
            coordinator.delegate(target_agent_name="target", work_item="ping")
        except Exception:
            # Expected — call() requires LLM credentials we don't have here.
            pass

    target = captured.get("target")
    assert target is not None, (
        "delegate() did not construct a target AtomicAgent — test setup "
        "may be incomplete (check roster.md and agent dir fixtures)"
    )

    # THE LOAD-BEARING ASSERTION: identity check (``is``), not isinstance.
    # Same instance must flow from coordinator to target — not a fresh
    # default resolved from the env var.
    assert target.policy_backend is coordinator.policy_backend, (
        "target agent did not inherit coordinator's policy_backend — "
        "spec/32 Decision 1 regression. delegate() must thread "
        "policy_backend=self.policy_backend to the target constructor."
    )


# ──────────────────────────────────────────────────────────────────
# Per-scope instance isolation


def test_two_agents_share_no_state_via_policy_backend(tmp_path):
    """Two agents constructed without explicit policy_backend share NO state.

    ``get_default_policy_backend(self.agents_root)`` constructs a NEW
    ``FilesystemPolicyBackend(scope_root)`` on each call — so two agents
    in the same agents_root get DIFFERENT instances.  Each agent's cache
    state is per-instance; they don't inadvertently poison each other's
    mtime+size cache.

    Pin with ``agent_a.policy_backend is not agent_b.policy_backend``
    (different instances; same scope; no shared state at this level).
    """
    _make_minimal_agent_dir(tmp_path, "agent_a")
    _make_minimal_agent_dir(tmp_path, "agent_b")

    a = AtomicAgent(name="agent_a", agents_root=tmp_path)
    b = AtomicAgent(name="agent_b", agents_root=tmp_path)

    # Both are FilesystemPolicyBackend but are distinct instances
    assert isinstance(a.policy_backend, FilesystemPolicyBackend)
    assert isinstance(b.policy_backend, FilesystemPolicyBackend)
    # Different instances — no shared cache state
    assert a.policy_backend is not b.policy_backend


# ──────────────────────────────────────────────────────────────────
# Doctor check_policy_backend


def test_doctor_check_policy_backend_filesystem_pass(tmp_path):
    """Fixture with ``policy.md`` → PASS + expected detail fields.

    Verifies the PASS path of ``check_policy_backend``:
    - ``status == "pass"``
    - ``detail["backend_id"] == "filesystem"``
    - ``detail["cache_ttl_s"] == 0`` (filesystem backend is always fresh)
    - ``detail["policy_md_exists"] == True``
    """
    # Write policy.md so doctor sees it as present
    (tmp_path / "policy.md").write_text(
        "# Fleet Policy\n\n## Cost caps\n\ndaily_usd: 10.0\n",
        encoding="utf-8",
    )

    result = check_policy_backend(tmp_path)

    assert result.status == "pass", (
        f"Expected PASS with policy.md present, got {result.status!r}: {result.message}"
    )
    assert result.detail is not None
    assert result.detail.get("backend_id") == "filesystem"
    assert result.detail.get("cache_ttl_s") == 0
    assert result.detail.get("policy_md_exists") is True


def test_doctor_check_policy_backend_unknown_id_fails(tmp_path, monkeypatch):
    """Unknown backend id → FAIL with a helpful message.

    ``ATOMIC_AGENTS_POLICY_BACKEND=blah-not-a-backend`` is not registered;
    the doctor check must surface this as FAIL (not crash, not WARN) so
    the operator knows their env var is wrong before any agent runs.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_POLICY_BACKEND", "blah-not-a-backend")

    result = check_policy_backend(tmp_path)

    assert result.status == "fail", (
        f"Expected FAIL for unknown backend id, got {result.status!r}: {result.message}"
    )


def test_doctor_check_policy_backend_no_policy_md_warns(tmp_path):
    """Fixture WITHOUT ``policy.md`` → WARN (no-opinion informational).

    When ``policy.md`` is absent, every agent runs in no-opinion mode.
    The doctor surfaces this as WARN (not FAIL — it's a valid operational
    state, especially for single-agent home users who never need fleet policy)
    so operators who DO want fleet caps know they haven't authored them yet.
    """
    # No policy.md written — tmp_path is an empty scope_root
    result = check_policy_backend(tmp_path)

    assert result.status == "warn", (
        f"Expected WARN with policy.md absent, got {result.status!r}: {result.message}"
    )
    # detail is still populated (backend is healthy; just no policy file)
    assert result.detail is not None
    assert result.detail.get("policy_md_exists") is False


# ──────────────────────────────────────────────────────────────────
# Cascade-aware PolicyBackend scoping (#236 fix, PR 3a)


def _make_cascade_layout(tmp_path: Path, role_name: str = "researcher") -> Path:
    """Create a minimal cascade layout and return the instance root (agent_root).

    Layout::

        tmp_path/
          roles/<role_name>/              # role_root (must exist for detect_cascade)
          projects/myproject/
            agents/<role_name>/           # instance_root (= agent_root)
              persona/IDENTITY.md
              tools.md
              model.md
              memory/

    ``detect_cascade(instance_root)`` returns a ``CascadePaths`` with:
      - ``cascade.project_root = tmp_path/projects/myproject``
      - ``cascade.instance_root = tmp_path/projects/myproject/agents/<role_name>``
    """
    # Role root (must exist for detect_cascade to fire)
    role_root = tmp_path / "roles" / role_name
    role_root.mkdir(parents=True)

    # Instance root under cascade shape
    instance_root = tmp_path / "projects" / "myproject" / "agents" / role_name
    instance_root.mkdir(parents=True)
    (instance_root / "persona").mkdir()
    (instance_root / "persona" / "IDENTITY.md").write_text(
        f"# {role_name.title()}\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    (instance_root / "tools.md").write_text(
        "# Tools\n\n## Read paths\n\n- ~/data\n",
        encoding="utf-8",
    )
    (instance_root / "model.md").write_text(
        "# Model\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
        "```yaml\ncost_guardrails:\n  enabled: true\n  daily_cap_usd: 5.0\n"
        "  monthly_cap_usd: 100.0\n```\n",
        encoding="utf-8",
    )
    (instance_root / "memory").mkdir()
    return instance_root


def test_atomic_agent_cascade_layout_uses_cascade_project_root(tmp_path):
    """In a cascade layout, the default policy_backend is scoped to
    ``cascade.project_root``, NOT ``agents_root`` (#236 fix, PR 3a).

    This is load-bearing: ``policy.md`` in cascade layouts lives at
    ``<system>/projects/<project>/policy.md``, not at the agents/ subdir.
    An agent reading caps from agents_root would silently miss the fleet policy
    because that directory has no policy.md.
    """
    instance_root = _make_cascade_layout(tmp_path, "researcher")

    # agents_root for the cascade layout is the agents/ directory
    agents_root = instance_root.parent  # tmp_path/projects/myproject/agents/
    expected_project_root = tmp_path / "projects" / "myproject"

    agent = AtomicAgent(name="researcher", agents_root=agents_root)

    # Must be a filesystem backend
    assert isinstance(agent.policy_backend, FilesystemPolicyBackend)
    # Must be scoped to cascade.project_root, not agents_root
    assert (
        agent.policy_backend._project_root.resolve() == expected_project_root.resolve()
    ), (
        f"policy_backend._project_root={agent.policy_backend._project_root!r} "
        f"expected {expected_project_root!r} — cascade-aware re-resolution failed (#236)"
    )


def test_atomic_agent_non_cascade_uses_agents_root(tmp_path):
    """In a flat (non-cascade) layout, the default policy_backend is scoped to
    ``agents_root`` (preserves PR 2 behavior for non-cascade agents).
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)

    assert isinstance(agent.policy_backend, FilesystemPolicyBackend)
    assert agent.policy_backend._project_root.resolve() == tmp_path.resolve(), (
        "Non-cascade agent's policy_backend._project_root must equal agents_root"
    )


def test_doctor_warns_on_cascade_scope_mismatch(tmp_path):
    """When doctor is called in a cascade layout with agents_root as scope_root
    AND the cascade context is passed, it emits WARN about scope mismatch.

    This covers the case where an operator explicitly passed
    ``FilesystemPolicyBackend(agents_root)`` instead of
    ``FilesystemPolicyBackend(cascade.project_root)`` — the doctor surfaces
    the misconfiguration so they can correct it.
    """
    from atomic_agents._cascade import detect_cascade

    instance_root = _make_cascade_layout(tmp_path, "researcher")
    agents_root = instance_root.parent

    # Detect cascade from instance_root perspective
    cascade = detect_cascade(instance_root)
    assert cascade is not None, "test fixture must produce a cascade layout"

    # Doctor called with agents_root scope and cascade context
    # The backend will be scoped to agents_root (not project_root) which is wrong
    result = check_policy_backend(agents_root, cascade=cascade)

    assert result.status == "warn", (
        f"Expected WARN for cascade scope mismatch; got {result.status!r}: {result.message}"
    )
    assert "cascade" in result.message.lower(), (
        f"WARN message should mention 'cascade'; got {result.message!r}"
    )

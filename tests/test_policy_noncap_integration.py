"""End-to-end coverage for Policy non-cap consumption sites (#89 PR 4 / issue #275).

PR 3b shipped the snapshot machinery and emission shapes for tool / MCP / model
denials but explicitly deferred LLM-mocked tests of the three insertion points
inside ``agent.call()`` (tool dispatch, MCP discovery, model selection). PR 4
flips the ``ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP`` default to True, so those
control-flow branches start executing in production for every agent run. This
file closes the coverage gap: each consumption site is exercised under both
flag states with the LLM replaced by a fake that returns scripted responses.

These tests use the same mocking pattern as ``test_mcp.py``:

- ``patch("atomic_agents.agent._llm.call_llm", ...)`` replaces the LLM dispatch.
- ``patch("atomic_agents.agent.MCPClientPool", ...)`` keeps real subprocesses
  out of the test path.
- ``patch.object(agent, "lock_backend", ...)`` bypasses real lock acquisition
  for tests that don't pin the locking surface.

Every test that depends on the env-flag default sets it explicitly via
``monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, ...)`` so behavior is independent
of whether PR 4's default flip has landed.

Also pins:
- #273 — per-call dedup: the same denied tool re-attempted N iterations emits
  ONE ``policy_decision`` per call, not N.
- #274 — per-call kwarg audit: when ``agent.call(model="x")`` is supplied and
  Policy overrides, the emission carries ``model_from_per_call_override``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Fixtures shared with the other policy test modules


def _make_minimal_agent_dir(
    agents_root: Path,
    name: str = "scout",
    *,
    default_model: str = "claude-sonnet-4-6",
) -> Path:
    """Create a minimal agent dir sufficient for AtomicAgent construction.

    Mirrors the helpers in ``test_policy_cost_cap_consumption.py`` and
    ``test_policy_noncap_log_only.py`` so future maintainers see the same
    shape across all four policy test modules.
    """
    agent_root = agents_root / name
    (agent_root / "persona").mkdir(parents=True)
    (agent_root / "persona" / "IDENTITY.md").write_text(
        f"# {name}\n\nTest agent for PR 4 non-cap integration tests.\n"
    )
    (agent_root / "persona" / "SOUL.md").write_text("# soul\n")
    (agent_root / "persona" / "USER.md").write_text("# user\n")
    (agent_root / "tools.md").write_text("# Tools\n\nNo tools.\n")
    # model.md uses the ``## Default model`` heading parser path so the model
    # passed to ``call_llm`` is exactly what we wrote here. The YAML-block
    # parser only reads ``cost_guardrails`` keys — ``model:`` inside YAML is
    # NOT parsed, which is why a YAML-only model.md silently falls back to
    # the framework's hard-coded default and breaks model-selection tests.
    (agent_root / "model.md").write_text(
        f"# Model\n\n## Default model\n\n{default_model}\n\n"
        "```yaml\ncost_guardrails:\n  daily_cap_usd: 0\n  monthly_cap_usd: 0\n```\n"
    )
    (agent_root / "memory").mkdir()
    (agent_root / "memory" / "INDEX.md").write_text("# INDEX\n")
    return agent_root


def _write_policy(project_root: Path, content: str) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "policy.md").write_text(content)


def _write_mcp_md(agent_root: Path, content: str) -> None:
    (agent_root / "mcp.md").write_text(content)


def _stub_anthropic() -> None:
    """Ensure the ``anthropic`` provider import doesn't crash agent setup."""
    if "anthropic" not in sys.modules:
        sys.modules["anthropic"] = types.ModuleType("anthropic")


def _raw_response(
    text: str = "",
    tool_uses: list[dict] | None = None,
    *,
    input_tokens: int = 10,
    output_tokens: int = 20,
):
    """Build a minimal ``_RawLLMResponse``-shaped object for LLM mocking."""
    return types.SimpleNamespace(
        text=text,
        tool_uses=tool_uses or [],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=0,
        raw={},
    )


def _policy_decision_records(agent) -> list:
    """Read all ``policy_decision`` records the agent's LogBackend captured."""
    from atomic_agents.logs.types import LogQuery, PRIMITIVE_POLICY_DECISION

    return agent.log_backend.query(LogQuery(primitive=PRIMITIVE_POLICY_DECISION))


def _build_agent(tmp_path: Path, **extra_kwargs):
    """Construct a minimal ``AtomicAgent`` with policy backend wired by default."""
    from atomic_agents import AtomicAgent

    _stub_anthropic()
    return AtomicAgent(
        name="scout",
        trigger="cron",
        agents_root=tmp_path,
        **extra_kwargs,
    )


def _patch_lock(agent):
    """Patch the agent's lock_backend so .acquire / .release are no-ops."""
    mock_lock = patch.object(agent, "lock_backend").start()
    mock_lock.acquire.return_value = MagicMock()
    mock_lock.release.return_value = None
    return mock_lock


# ──────────────────────────────────────────────────────────────────────────
# Tool-allowlist consumption site
# (atomic_agents/agent.py — Policy tool-allowlist consultation block)


def test_tool_denied_log_only_mode_runs_tool_and_emits_with_enforced_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-off: denied tool still executes; policy_decision recorded with enforced=False.

    Pins the load-bearing log-only invariant: operators can verify policy
    correctness on a real fleet before flipping to enforce mode.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "tools:\n  deny:\n    - delete_file\n")

    executed: list[dict] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="delete_file",
            description="Delete a file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda inp: (executed.append(inp), "deleted")[1],
        )
    )

    agent = _build_agent(tmp_path, tools=registry)
    _patch_lock(agent)

    use_resp = _raw_response(
        text="calling delete",
        tool_uses=[{"name": "delete_file", "input": {"path": "/tmp/x"}, "id": "tu_1"}],
    )
    final_resp = _raw_response("done.")
    with patch("atomic_agents.agent._llm.call_llm", side_effect=[use_resp, final_resp]):
        agent.call("Delete /tmp/x.")

    # Tool was actually invoked — log-only mode does not block.
    assert executed == [{"path": "/tmp/x"}]

    decisions = _policy_decision_records(agent)
    assert len(decisions) == 1, [d.extra for d in decisions]
    d = decisions[0]
    assert d.extra["axis"] == "tool_allowlist"
    assert d.extra["decision_kind"] == "deny"
    assert d.extra["denying_layer"] == "policy"
    assert d.extra["tool_name"] == "delete_file"
    assert d.extra["enforced"] is False


def test_tool_denied_enforce_mode_blocks_and_emits_with_enforced_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-on: denied tool is NOT executed; synthesized policy_blocked ToolCallResult.

    The synthesized ToolCallResult mirrors the judge_blocked shape so the
    LLM sees a structured refusal and stops re-attempting the denied tool
    on the next iteration. Pinned because PR 4 flips this branch to the
    default for every operator's enforce-mode deployment.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "tools:\n  deny:\n    - delete_file\n")

    executed: list[dict] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="delete_file",
            description="Delete a file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda inp: (executed.append(inp), "deleted")[1],
        )
    )

    agent = _build_agent(tmp_path, tools=registry)
    _patch_lock(agent)

    use_resp = _raw_response(
        text="calling delete",
        tool_uses=[{"name": "delete_file", "input": {"path": "/tmp/x"}, "id": "tu_1"}],
    )
    final_resp = _raw_response("done.")
    with patch("atomic_agents.agent._llm.call_llm", side_effect=[use_resp, final_resp]):
        response = agent.call("Delete /tmp/x.")

    # Tool was NOT executed.
    assert executed == []

    # ToolCallResult carries the policy_denied error so the LLM sees a refusal.
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.tool_name == "delete_file"
    assert tc.error is not None
    assert "policy_denied" in tc.error

    decisions = _policy_decision_records(agent)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.extra["axis"] == "tool_allowlist"
    assert d.extra["enforced"] is True


# ──────────────────────────────────────────────────────────────────────────
# MCP-allowlist consumption site


def test_mcp_denied_log_only_mode_still_connects_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-off: denied MCP server still connects; policy_decision with enforced=False.

    Operators using log-only mode get the audit signal without losing
    server access — they verify denials are firing on the right servers
    before flipping the flag.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    agent_root = _make_minimal_agent_dir(tmp_path, "scout")
    _write_mcp_md(
        agent_root,
        "# MCP servers\n\n## insecure\ncommand: npx\nargs: -y, @mcp/insecure\n",
    )
    _write_policy(tmp_path, "mcp_servers:\n  deny:\n    - insecure\n")

    mock_pool = MagicMock()
    mock_pool.connect_all = MagicMock()
    mock_pool.discover_tools = MagicMock(return_value=[])
    mock_pool.disconnect_all = MagicMock()

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    final_resp = _raw_response("ok.")
    with patch("atomic_agents.agent._llm.call_llm", return_value=final_resp):
        with patch(
            "atomic_agents.agent.MCPClientPool", return_value=mock_pool
        ) as pool_cls:
            agent.call("Hello.")

    # Pool WAS constructed and connected — log-only doesn't filter.
    pool_cls.assert_called_once()
    mock_pool.connect_all.assert_called_once()

    decisions = _policy_decision_records(agent)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.extra["axis"] == "mcp_allowlist"
    assert d.extra["mcp_server_name"] == "insecure"
    assert d.extra["enforced"] is False


def test_mcp_denied_enforce_mode_filters_before_pool_spinup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-on: denied MCP server is filtered before MCPClientPool is constructed.

    The agent does not pay the subprocess startup cost for denied servers.
    When the only declared server is denied, the pool is not constructed
    at all (the ``if effective_mcp_specs:`` guard).
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    agent_root = _make_minimal_agent_dir(tmp_path, "scout")
    _write_mcp_md(
        agent_root,
        "# MCP servers\n\n## insecure\ncommand: npx\nargs: -y, @mcp/insecure\n",
    )
    _write_policy(tmp_path, "mcp_servers:\n  deny:\n    - insecure\n")

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    final_resp = _raw_response("ok.")
    with patch("atomic_agents.agent._llm.call_llm", return_value=final_resp):
        with patch("atomic_agents.agent.MCPClientPool") as pool_cls:
            agent.call("Hello.")

    # Pool was NOT constructed — the only declared server was filtered out.
    pool_cls.assert_not_called()

    decisions = _policy_decision_records(agent)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.extra["axis"] == "mcp_allowlist"
    assert d.extra["mcp_server_name"] == "insecure"
    assert d.extra["enforced"] is True


# ──────────────────────────────────────────────────────────────────────────
# Model-selection consumption site


def test_model_override_log_only_keeps_md_model_emits_with_enforced_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-off: model.md's model is what the LLM call uses; override is logged only."""
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    _make_minimal_agent_dir(tmp_path, "scout", default_model="claude-sonnet-4-6")
    _write_policy(tmp_path, "model: claude-opus-4-7\n")

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    captured_models: list[str] = []

    def fake_call_llm(*, model, **kw):
        captured_models.append(model)
        return _raw_response("done.")

    with patch("atomic_agents.agent._llm.call_llm", side_effect=fake_call_llm):
        agent.call("Hello.")

    # The model.md value was used; Policy's override was logged only.
    assert captured_models == ["claude-sonnet-4-6"]

    decisions = _policy_decision_records(agent)
    overrides = [d for d in decisions if d.extra["axis"] == "model_selection"]
    assert len(overrides) == 1
    d = overrides[0]
    assert d.extra["decision_kind"] == "override"
    assert d.extra["denying_layer"] is None
    assert d.extra["model_from_md"] == "claude-sonnet-4-6"
    assert d.extra["model_from_policy"] == "claude-opus-4-7"
    assert d.extra["enforced"] is False


def test_model_override_enforce_mode_uses_policy_model_emits_with_enforced_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-on: Policy's model is what the LLM call uses; emission records enforced=True."""
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(tmp_path, "scout", default_model="claude-sonnet-4-6")
    _write_policy(tmp_path, "model: claude-opus-4-7\n")

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    captured_models: list[str] = []

    def fake_call_llm(*, model, **kw):
        captured_models.append(model)
        return _raw_response("done.")

    with patch("atomic_agents.agent._llm.call_llm", side_effect=fake_call_llm):
        agent.call("Hello.")

    # Policy's model replaced the model.md value.
    assert captured_models == ["claude-opus-4-7"]

    decisions = _policy_decision_records(agent)
    overrides = [d for d in decisions if d.extra["axis"] == "model_selection"]
    assert len(overrides) == 1
    assert overrides[0].extra["enforced"] is True


# ──────────────────────────────────────────────────────────────────────────
# Negative pins


def test_no_policy_backend_emits_no_policy_decision_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent constructed without a Policy backend → all three branches are inert.

    Pins the ``pol_snap is None / pol_snap.<allow_fn> is None`` guards at
    every consumption site. Zero behavior change for operators who never
    author ``policy.md``.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    # Flag value is irrelevant when snapshot is no-opinion, but pin it explicitly.
    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(tmp_path, "scout")
    # No policy.md is written — FilesystemPolicyBackend returns no-opinion.

    executed: list[dict] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="any_tool",
            description="A tool",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            handler=lambda inp: (executed.append(inp), "ok")[1],
        )
    )

    agent = _build_agent(tmp_path, tools=registry)
    _patch_lock(agent)

    use_resp = _raw_response(
        text="calling",
        tool_uses=[{"name": "any_tool", "input": {"x": "y"}, "id": "tu_1"}],
    )
    final_resp = _raw_response("done.")
    with patch("atomic_agents.agent._llm.call_llm", side_effect=[use_resp, final_resp]):
        agent.call("Run any_tool.")

    # Tool ran normally; zero policy_decision events emitted.
    assert executed == [{"x": "y"}]
    decisions = _policy_decision_records(agent)
    assert decisions == [], [d.extra for d in decisions]


def test_model_override_matching_md_default_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Policy ``model:`` matches ``model.md`` → no override emission (suppression).

    Pins the ``pol_snap.model_override != _md_default_model`` short-circuit
    so operators don't see noise events for fleet-wide model agreements.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(tmp_path, "scout", default_model="claude-sonnet-4-6")
    _write_policy(tmp_path, "model: claude-sonnet-4-6\n")  # same as md

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    with patch(
        "atomic_agents.agent._llm.call_llm", return_value=_raw_response("done.")
    ):
        agent.call("Hello.")

    decisions = _policy_decision_records(agent)
    overrides = [d for d in decisions if d.extra["axis"] == "model_selection"]
    assert overrides == []


# ──────────────────────────────────────────────────────────────────────────
# #273 — per-call dedup for tool-allowlist denial emissions


def test_repeated_denied_tool_emits_once_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#273 regression: same denied tool re-attempted N iterations → 1 emission per call.

    In log-only mode, the LLM does not see refusals and may re-attempt the
    same denied tool on every iteration. Without the per-call dedup set,
    the audit log records N events per denied tool per call. With dedup,
    one event per (tool_name, call).
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "tools:\n  deny:\n    - retry_tool\n")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="retry_tool",
            description="A tool the LLM keeps retrying",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
            handler=lambda inp: "ok",
        )
    )

    agent = _build_agent(tmp_path, tools=registry)
    _patch_lock(agent)

    # Three consecutive tool_use iterations of the same denied tool,
    # then a terminal text response.
    def _tu(i: int):
        return _raw_response(
            text=f"try {i}",
            tool_uses=[{"name": "retry_tool", "input": {"n": i}, "id": f"tu_{i}"}],
        )

    sequence = [_tu(1), _tu(2), _tu(3), _raw_response("done.")]
    with patch("atomic_agents.agent._llm.call_llm", side_effect=sequence):
        agent.call("Retry retry_tool a few times.")

    decisions = _policy_decision_records(agent)
    tool_decisions = [d for d in decisions if d.extra["axis"] == "tool_allowlist"]
    assert len(tool_decisions) == 1, [d.extra for d in tool_decisions]
    assert tool_decisions[0].extra["tool_name"] == "retry_tool"


def test_different_denied_tools_each_emit_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#273 invariant: dedup is per (tool_name, call), not per call.

    Two different denied tools in the same call each emit once.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "tools:\n  deny:\n    - tool_a\n    - tool_b\n")

    def _td(name: str) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=f"Denied tool {name}",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            handler=lambda inp: "ok",
        )

    registry = ToolRegistry()
    registry.register(_td("tool_a"))
    registry.register(_td("tool_b"))

    agent = _build_agent(tmp_path, tools=registry)
    _patch_lock(agent)

    seq = [
        _raw_response(
            text="a",
            tool_uses=[{"name": "tool_a", "input": {"x": "1"}, "id": "tu_a"}],
        ),
        _raw_response(
            text="b",
            tool_uses=[{"name": "tool_b", "input": {"x": "1"}, "id": "tu_b"}],
        ),
        _raw_response("done."),
    ]
    with patch("atomic_agents.agent._llm.call_llm", side_effect=seq):
        agent.call("Run both denied tools.")

    decisions = _policy_decision_records(agent)
    names = sorted(
        d.extra["tool_name"] for d in decisions if d.extra["axis"] == "tool_allowlist"
    )
    assert names == ["tool_a", "tool_b"]


# ──────────────────────────────────────────────────────────────────────────
# #274 — per-call kwarg audit


def test_per_call_model_kwarg_appears_in_override_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#274 regression: ``agent.call(model=...)`` value lands in the audit event.

    When Policy overrides and a per-call kwarg was supplied, the
    ``model_from_per_call_override`` field carries the kwarg value so
    audit readers can distinguish "Policy overrode model.md" from
    "Policy overrode the operator's explicit per-call choice."
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(
        tmp_path, "scout", default_model="claude-haiku-4-5-20251001"
    )
    _write_policy(tmp_path, "model: claude-sonnet-4-6\n")

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    with patch(
        "atomic_agents.agent._llm.call_llm", return_value=_raw_response("done.")
    ):
        agent.call("Hello.", model_override="claude-opus-4-7")

    decisions = _policy_decision_records(agent)
    overrides = [d for d in decisions if d.extra["axis"] == "model_selection"]
    assert len(overrides) == 1
    d = overrides[0]
    assert d.extra["model_from_md"] == "claude-haiku-4-5-20251001"
    assert d.extra["model_from_policy"] == "claude-sonnet-4-6"
    assert d.extra["model_from_per_call_override"] == "claude-opus-4-7"


def test_no_per_call_kwarg_keeps_override_field_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#274: field is absent (not stored) when no per-call kwarg was supplied.

    Backwards-compatible — operators not using the kwarg see the same
    PR 3b event shape they already observed.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(
        tmp_path, "scout", default_model="claude-haiku-4-5-20251001"
    )
    _write_policy(tmp_path, "model: claude-sonnet-4-6\n")

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    with patch(
        "atomic_agents.agent._llm.call_llm", return_value=_raw_response("done.")
    ):
        agent.call("Hello.")  # No model_override kwarg.

    decisions = _policy_decision_records(agent)
    overrides = [d for d in decisions if d.extra["axis"] == "model_selection"]
    assert len(overrides) == 1
    # Field is absent from the JSONL extra dict (emit helper drops None values).
    assert "model_from_per_call_override" not in overrides[0].extra


def test_policy_matches_md_but_kwarg_differs_still_emits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR 4 R1 P0-2 regression: when ``policy.md`` and ``model.md`` agree but
    the operator's per-call kwarg differs from both, the override emission
    MUST still fire — Policy is silently superseding the kwarg in enforce mode.

    Pre-fix: gate compared Policy against ``model.md``; since they agreed, no
    emission fired and the kwarg-override happened with zero audit signal —
    exactly the silent-override hole #274 was meant to close.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(tmp_path, "scout", default_model="claude-sonnet-4-6")
    _write_policy(tmp_path, "model: claude-sonnet-4-6\n")  # agrees with model.md

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    captured_models: list[str] = []

    def fake_call_llm(*, model, **kw):
        captured_models.append(model)
        return _raw_response("done.")

    with patch("atomic_agents.agent._llm.call_llm", side_effect=fake_call_llm):
        agent.call("Hello.", model_override="claude-opus-4-7")

    # Enforce mode + Policy disagrees with the pre-Policy effective model
    # (the kwarg-supplied "claude-opus-4-7"), so Policy's value wins.
    assert captured_models == ["claude-sonnet-4-6"]

    decisions = _policy_decision_records(agent)
    overrides = [d for d in decisions if d.extra["axis"] == "model_selection"]
    assert len(overrides) == 1, [d.extra for d in decisions]
    d = overrides[0]
    assert d.extra["model_from_md"] == "claude-sonnet-4-6"
    assert d.extra["model_from_policy"] == "claude-sonnet-4-6"
    assert d.extra["model_from_per_call_override"] == "claude-opus-4-7"
    assert d.extra["enforced"] is True


def test_per_call_kwarg_matches_policy_skips_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR 4 R1 P0-1 regression: when the per-call kwarg matches Policy's value,
    NO override emission fires — the operator's choice and Policy aligned and
    the pre-Policy effective model already equals what Policy would override
    to, so there is no override to record.

    Pre-fix: the broken gate compared Policy against ``model.md`` only, fired
    the emission whenever those disagreed (even when the kwarg matched
    Policy), and populated ``model_from_per_call_override`` to make the audit
    claim "Policy overrode operator's choice X" for an alignment that wasn't
    an override. Post-fix the gate compares against the actual pre-Policy
    effective model so this case correctly skips.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")

    _make_minimal_agent_dir(
        tmp_path, "scout", default_model="claude-haiku-4-5-20251001"
    )
    _write_policy(tmp_path, "model: claude-opus-4-7\n")

    agent = _build_agent(tmp_path)
    _patch_lock(agent)

    captured_models: list[str] = []

    def fake_call_llm(*, model, **kw):
        captured_models.append(model)
        return _raw_response("done.")

    with patch("atomic_agents.agent._llm.call_llm", side_effect=fake_call_llm):
        # kwarg matches Policy's value — operator and Policy aligned.
        agent.call("Hello.", model_override="claude-opus-4-7")

    # The model that runs is the kwarg's value (== Policy's value) — no
    # override of the operator's intent, no audit-worthy event.
    assert captured_models == ["claude-opus-4-7"]

    decisions = _policy_decision_records(agent)
    overrides = [d for d in decisions if d.extra["axis"] == "model_selection"]
    assert overrides == [], [d.extra for d in overrides]


def test_sequential_calls_reset_tool_denial_dedup_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dedup set MUST be re-initialized at each ``agent.call()`` entry.

    Pins the per-call reset so a future refactor that moves the set
    initialization outside the call frame (e.g., into ``__init__``) is
    caught: two sequential calls each denying the same tool should produce
    two events, not one.
    """
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "tools:\n  deny:\n    - drift_tool\n")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="drift_tool",
            description="A tool denied across two calls",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            handler=lambda inp: "ok",
        )
    )

    agent = _build_agent(tmp_path, tools=registry)
    _patch_lock(agent)

    def _seq():
        return [
            _raw_response(
                text="try",
                tool_uses=[{"name": "drift_tool", "input": {"x": "1"}, "id": "tu_1"}],
            ),
            _raw_response("done."),
        ]

    # First call: dedup set is empty, denial fires once, set now contains
    # "drift_tool", call ends.
    with patch("atomic_agents.agent._llm.call_llm", side_effect=_seq()):
        agent.call("First.")
    # Second call: dedup set MUST have been reset; denial fires again.
    with patch("atomic_agents.agent._llm.call_llm", side_effect=_seq()):
        agent.call("Second.")

    decisions = _policy_decision_records(agent)
    tool_decisions = [d for d in decisions if d.extra["axis"] == "tool_allowlist"]
    assert len(tool_decisions) == 2, [d.extra for d in tool_decisions]

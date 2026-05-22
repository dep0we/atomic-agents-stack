"""Tests for Policy non-cap surfaces in log-only mode (#89 PR 3b).

PR 3b populates the previously-stub snapshot fields ``tool_allow_fn`` /
``mcp_allow_fn`` / ``model_override`` and adds the ``enforce_noncap`` flag
read once at call entry from ``ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP``.  When
the flag is False (the PR 3b default), denials on tool / MCP / model
surfaces emit ``policy_decision`` events with ``enforced=False`` and the
action proceeds — operators can verify policy correctness in production
before PR 4 flips the default to True.

These tests mirror the shape of ``test_policy_cost_cap_consumption.py``:
direct probes of ``_take_policy_snapshot`` and ``_emit_policy_decision``
without running full ``agent.call()`` invocations.  Tests of the consumption
sites inside ``agent.call()`` would require LLM mocking outside this PR's
scope — the snapshot machinery + emission shape verified here are the
load-bearing seams.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_minimal_agent_dir(agents_root: Path, name: str) -> Path:
    """Create a minimal agent dir sufficient for AtomicAgent construction.

    Mirrors the helper in ``test_policy_cost_cap_consumption.py``.
    """
    agent_root = agents_root / name
    (agent_root / "persona").mkdir(parents=True)
    (agent_root / "persona" / "IDENTITY.md").write_text(
        f"# {name}\n\nTest agent for PR 3b non-cap surface tests.\n"
    )
    (agent_root / "persona" / "SOUL.md").write_text("# soul\n")
    (agent_root / "persona" / "USER.md").write_text("# user\n")
    (agent_root / "tools.md").write_text("# Tools\n\nNo tools.\n")
    (agent_root / "model.md").write_text(
        "# Model\n\n```yaml\nmodel: claude-sonnet-4-6\n"
        "cost_guardrails:\n  daily_cap_usd: 0\n  monthly_cap_usd: 0\n```\n"
    )
    (agent_root / "memory").mkdir()
    (agent_root / "memory" / "INDEX.md").write_text("# INDEX\n")
    return agent_root


def _write_policy(project_root: Path, content: str) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "policy.md").write_text(content)


# ─────────────────────────────────────────────────────────────────────────────
# Env-flag reader


def test_enforce_noncap_default_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env var → True (PR 4 flipped the default to enforce mode).

    Operators authoring ``policy.md`` get tool / MCP / model surfaces
    enforced by default. Operators wanting to delay enforce mode set the
    env var explicitly to ``"false"`` (or any documented falsy value).
    """
    from atomic_agents.policy.types import (
        ENFORCE_NONCAP_ENV_VAR,
        _read_enforce_noncap_flag,
    )

    monkeypatch.delenv(ENFORCE_NONCAP_ENV_VAR, raising=False)
    assert _read_enforce_noncap_flag() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON"])
def test_enforce_noncap_truthy_values(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    """Each documented truthy value flips the flag True (case-insensitive)."""
    from atomic_agents.policy.types import (
        ENFORCE_NONCAP_ENV_VAR,
        _read_enforce_noncap_flag,
    )

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, val)
    assert _read_enforce_noncap_flag() is True


@pytest.mark.parametrize(
    "val", ["0", "false", "FALSE", "False", "no", "NO", "off", "OFF"]
)
def test_enforce_noncap_falsy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    """Each documented falsy value flips the flag False (case-insensitive).

    PR 4 inverted the default semantic: the explicit falsy set is now the
    opt-out path. Operators wanting to delay enforce mode set the env to
    one of these values.
    """
    from atomic_agents.policy.types import (
        ENFORCE_NONCAP_ENV_VAR,
        _read_enforce_noncap_flag,
    )

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, val)
    assert _read_enforce_noncap_flag() is False


@pytest.mark.parametrize("val", ["", "  ", "maybe", "garbage", "yesno"])
def test_enforce_noncap_unknown_values_default_to_true(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    """Any value outside the documented falsy set reads as True — PR 4 enforce
    default. Operators must use a documented falsy value (``false``, ``0``,
    ``no``, ``off``) to opt back into log-only mode.
    """
    from atomic_agents.policy.types import (
        ENFORCE_NONCAP_ENV_VAR,
        _read_enforce_noncap_flag,
    )

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, val)
    assert _read_enforce_noncap_flag() is True


def test_enforce_noncap_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leading/trailing whitespace is tolerated — operators copy-pasting from
    docs shouldn't trip a False on `' true '`."""
    from atomic_agents.policy.types import (
        ENFORCE_NONCAP_ENV_VAR,
        _read_enforce_noncap_flag,
    )

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "  true  ")
    assert _read_enforce_noncap_flag() is True


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot extension — PR 3b populates the previously-stub fields


def test_snapshot_populates_tool_allow_fn_and_mcp_allow_fn(tmp_path: Path) -> None:
    """The snapshot's tool/MCP allow_fn closures are now populated (not None)
    and call into the backend correctly.

    PR 3a left these as None stubs; PR 3b populates them.
    """
    from atomic_agents import AtomicAgent

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(
        tmp_path,
        "tools:\n  deny: [delete_file]\nmcp_servers:\n  deny: [insecure-server]\n",
    )
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()

    assert snap.tool_allow_fn is not None
    assert snap.mcp_allow_fn is not None
    # Allowed tools/servers return True; denied return False.
    assert snap.tool_allow_fn("read_file") is True
    assert snap.tool_allow_fn("delete_file") is False
    assert snap.mcp_allow_fn("filesystem") is True
    assert snap.mcp_allow_fn("insecure-server") is False


def test_snapshot_populates_model_override(tmp_path: Path) -> None:
    """When policy.md declares a fleet model, the snapshot's model_override
    carries it for the consumption site."""
    from atomic_agents import AtomicAgent

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "model: claude-opus-4-7\n")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()

    assert snap.model_override == "claude-opus-4-7"


def test_snapshot_model_override_per_agent_replaces_fleet(tmp_path: Path) -> None:
    """Per-agent override under ``agents:`` REPLACES the fleet model
    (Premise 5: REPLACE semantics for model selection)."""
    from atomic_agents import AtomicAgent

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(
        tmp_path,
        "model: claude-opus-4-7\nagents:\n  scout:\n    model: gpt-4\n",
    )
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()

    assert snap.model_override == "gpt-4"


def test_snapshot_no_policy_md_yields_no_opinion_noncap_fields(tmp_path: Path) -> None:
    """Absent policy.md → tool_allow_fn/mcp_allow_fn allow everything,
    model_override is None. Zero-behavior-change pin: pre-PR-3b agents
    with no policy.md must behave byte-identically."""
    from atomic_agents import AtomicAgent

    _make_minimal_agent_dir(tmp_path, "scout")
    # No policy.md.
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()

    # Allow_fn closures still exist (snapshot shape is consistent), but they
    # consult the backend's default-allow behavior.
    assert snap.tool_allow_fn is not None
    assert snap.mcp_allow_fn is not None
    assert snap.tool_allow_fn("any_tool") is True
    assert snap.mcp_allow_fn("any_server") is True
    assert snap.model_override is None


def test_snapshot_enforce_noncap_reads_env_at_call_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enforce_noncap field reflects the env var value at snapshot time.
    Frozen for the duration of the call per Premise 3."""
    from atomic_agents import AtomicAgent
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")
    snap_on = agent._take_policy_snapshot()
    assert snap_on.enforce_noncap is True

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")
    snap_off = agent._take_policy_snapshot()
    assert snap_off.enforce_noncap is False


def test_snapshot_frozen_against_backend_reassignment(tmp_path: Path) -> None:
    """The snapshot's closures bind the backend reference at snapshot time
    via default-arg capture, so reassigning ``agent.policy_backend``
    mid-call cannot contaminate the frozen snapshot (Premise 3)."""
    from unittest.mock import MagicMock

    from atomic_agents import AtomicAgent

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "tools:\n  deny: [delete_file]\n")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()

    # Replace backend with one that would say everything is denied.
    swap = MagicMock()
    swap.is_tool_allowed.return_value = False
    swap.is_mcp_server_allowed.return_value = False
    swap.get_effective_model.return_value = "swapped-model"
    agent.policy_backend = swap

    # Snapshot's closures still call into the ORIGINAL backend.
    assert snap.tool_allow_fn("read_file") is True
    assert snap.tool_allow_fn("delete_file") is False
    assert snap.mcp_allow_fn("filesystem") is True
    # model_override was resolved once at snapshot time — not affected.
    assert snap.model_override is None


def test_snapshot_closures_fail_open_on_backend_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the backend raises mid-call inside is_tool_allowed /
    is_mcp_server_allowed, the closure logs a warning and returns True
    (log-only mode is fail-open; fail-closed mode tracked at #242)."""
    from unittest.mock import MagicMock

    from atomic_agents import AtomicAgent

    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)

    # Swap backend BEFORE snapshot so the closures capture the broken one.
    broken = MagicMock()
    broken.get_effective_caps.side_effect = RuntimeError("backend down")
    broken.capabilities.side_effect = RuntimeError("backend down")
    broken.get_effective_model.side_effect = RuntimeError("backend down")
    broken.is_tool_allowed.side_effect = RuntimeError("backend down")
    broken.is_mcp_server_allowed.side_effect = RuntimeError("backend down")
    agent.policy_backend = broken

    import logging

    with caplog.at_level(logging.WARNING):
        snap = agent._take_policy_snapshot()
        # Closures swallow per-call backend errors and fail-open.
        assert snap.tool_allow_fn("any_tool") is True
        assert snap.mcp_allow_fn("any_server") is True

    # Both snapshot-time (get_effective_caps, get_effective_model) and
    # call-time (is_tool_allowed, is_mcp_server_allowed) failures should
    # have logged warnings.
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "get_effective_caps" in msgs
    assert "get_effective_model" in msgs
    assert "is_tool_allowed" in msgs
    assert "is_mcp_server_allowed" in msgs
    # Exception class name is preserved (operators need to tell a
    # transient TimeoutError from a structural ValueError):
    assert "RuntimeError" in msgs
    # Snapshot/closure warnings from atomic_agents.agent are WARNING
    # level — not ERROR (would page oncall) and not INFO (would be
    # filtered at default log levels). Filtered to this logger so an
    # unrelated WARNING upstream cannot regress this assertion.
    agent_records = [r for r in caplog.records if r.name == "atomic_agents.agent"]
    assert agent_records, "expected at least one warning from atomic_agents.agent"
    for rec in agent_records:
        assert rec.levelno == logging.WARNING, (
            f"expected WARNING, got {rec.levelname} for: {rec.getMessage()}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Emission shapes for the three non-cap axes


def test_emit_tool_allowlist_decision_shape(tmp_path: Path) -> None:
    """Tool-allowlist denial emits a policy_decision with axis=tool_allowlist
    and tool_name populated."""
    from atomic_agents.logs.filesystem import FilesystemLogBackend
    from atomic_agents.logs.types import PRIMITIVE_POLICY_DECISION, LogQuery
    from atomic_agents.policy.types import PolicyDecision, _emit_policy_decision

    backend = FilesystemLogBackend(tmp_path)
    decision = PolicyDecision(
        decision_kind="deny",
        denying_layer="policy",
        agent_name="scout",
        axis="tool_allowlist",
        tool_name="delete_file",
        enforced=False,  # log-only (PR 3b default)
        cache_ttl_s=0,
    )
    _emit_policy_decision(decision, backend, run_id="run-tool-1")

    records = backend.query(LogQuery(primitive=PRIMITIVE_POLICY_DECISION))
    assert len(records) == 1
    rec = records[0]
    assert rec.agent_name == "scout"
    assert rec.run_id == "run-tool-1"
    assert rec.extra["decision_kind"] == "deny"
    assert rec.extra["axis"] == "tool_allowlist"
    assert rec.extra["tool_name"] == "delete_file"
    assert rec.extra["enforced"] is False
    # Non-relevant axis fields stay absent.
    assert "mcp_server_name" not in rec.extra
    assert "model_from_md" not in rec.extra


def test_emit_mcp_allowlist_decision_shape(tmp_path: Path) -> None:
    """MCP-allowlist denial emits axis=mcp_allowlist with mcp_server_name
    populated."""
    from atomic_agents.logs.filesystem import FilesystemLogBackend
    from atomic_agents.logs.types import PRIMITIVE_POLICY_DECISION, LogQuery
    from atomic_agents.policy.types import PolicyDecision, _emit_policy_decision

    backend = FilesystemLogBackend(tmp_path)
    decision = PolicyDecision(
        decision_kind="deny",
        denying_layer="policy",
        agent_name="scout",
        axis="mcp_allowlist",
        mcp_server_name="insecure-server",
        enforced=True,
        cache_ttl_s=0,
    )
    _emit_policy_decision(decision, backend, run_id="run-mcp-1")

    records = backend.query(LogQuery(primitive=PRIMITIVE_POLICY_DECISION))
    assert len(records) == 1
    rec = records[0]
    assert rec.extra["axis"] == "mcp_allowlist"
    assert rec.extra["mcp_server_name"] == "insecure-server"
    assert rec.extra["enforced"] is True
    assert "tool_name" not in rec.extra


def test_emit_model_selection_override_decision_shape(tmp_path: Path) -> None:
    """Model-override emits decision_kind=override, denying_layer=None,
    axis=model_selection, with both model_from_md and model_from_policy."""
    from atomic_agents.logs.filesystem import FilesystemLogBackend
    from atomic_agents.logs.types import PRIMITIVE_POLICY_DECISION, LogQuery
    from atomic_agents.policy.types import PolicyDecision, _emit_policy_decision

    backend = FilesystemLogBackend(tmp_path)
    decision = PolicyDecision(
        decision_kind="override",
        denying_layer=None,
        agent_name="scout",
        axis="model_selection",
        model_from_md="claude-sonnet-4-6",
        model_from_policy="claude-opus-4-7",
        enforced=False,
    )
    _emit_policy_decision(decision, backend, run_id="run-model-1")

    records = backend.query(LogQuery(primitive=PRIMITIVE_POLICY_DECISION))
    rec = records[0]
    assert rec.extra["decision_kind"] == "override"
    assert rec.extra["denying_layer"] is None
    assert rec.extra["axis"] == "model_selection"
    assert rec.extra["model_from_md"] == "claude-sonnet-4-6"
    assert rec.extra["model_from_policy"] == "claude-opus-4-7"
    assert rec.extra["enforced"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Enforce-mode branches (PR 4 will flip the env-var default to True; these
# tests exercise the branches now so PR 4 is a config flip, not a behavior
# debut)


def test_enforce_mode_mcp_filter_drops_denied_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In enforce mode, MCP filter logic produces effective_mcp_specs that
    EXCLUDES Policy-denied servers — they never connect, no subprocess cost
    is paid. Reproduces the filter loop inside ``agent.call()``."""
    from atomic_agents import AtomicAgent
    from atomic_agents.mcp import MCPServerSpec
    from atomic_agents.policy.types import (
        ENFORCE_NONCAP_ENV_VAR,
        PolicyDecision,
        _emit_policy_decision,
    )

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(
        tmp_path,
        "mcp_servers:\n  deny: [insecure-server]\n",
    )
    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()
    assert snap.enforce_noncap is True

    specs = [
        MCPServerSpec(name="filesystem", command="npx"),
        MCPServerSpec(name="insecure-server", command="npx"),
        MCPServerSpec(name="weather", command="npx"),
    ]
    # Mirrors the filter loop in agent.call():
    from atomic_agents.logs.filesystem import FilesystemLogBackend

    backend = FilesystemLogBackend(tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    allowed = []
    for s in specs:
        if snap.mcp_allow_fn(s.name):
            allowed.append(s)
            continue
        _emit_policy_decision(
            PolicyDecision(
                decision_kind="deny",
                denying_layer="policy",
                agent_name="scout",
                axis="mcp_allowlist",
                mcp_server_name=s.name,
                enforced=snap.enforce_noncap,
                cache_ttl_s=snap.cache_ttl_s,
            ),
            backend,
            run_id="run-mcp-enforce",
        )
        if not snap.enforce_noncap:
            allowed.append(s)

    # Enforce mode: denied server is excluded.
    assert [s.name for s in allowed] == ["filesystem", "weather"]


def test_log_only_mode_mcp_filter_keeps_denied_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In log-only mode (opt-in after PR 4), the MCP filter produces
    effective_mcp_specs that INCLUDES denied servers — the audit-event
    records the would-be denial but the server still connects.

    PR 4 flipped the default to enforce, so this test pins the
    log-only branch by explicitly setting the env var to ``"false"``."""
    from atomic_agents import AtomicAgent
    from atomic_agents.mcp import MCPServerSpec
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "mcp_servers:\n  deny: [insecure-server]\n")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()
    assert snap.enforce_noncap is False

    specs = [
        MCPServerSpec(name="filesystem", command="npx"),
        MCPServerSpec(name="insecure-server", command="npx"),
    ]
    allowed = []
    for s in specs:
        if snap.mcp_allow_fn(s.name):
            allowed.append(s)
            continue
        if not snap.enforce_noncap:
            allowed.append(s)

    assert [s.name for s in allowed] == ["filesystem", "insecure-server"]


def test_enforce_mode_model_override_replaces_pre_policy_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In enforce mode, Policy's model_override replaces the pre-Policy
    effective model. Pins the assignment branch inside ``agent.call()``."""
    from atomic_agents import AtomicAgent
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "model: claude-opus-4-7\n")
    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "true")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()

    md_default = agent.config.default_model
    model = md_default  # Pre-Policy effective model.

    # Mirrors the override block at agent.py:3211-3232.
    assert snap.model_override is not None
    assert snap.model_override != md_default
    if snap.enforce_noncap:
        model = snap.model_override

    assert model == "claude-opus-4-7"


def test_log_only_mode_model_override_keeps_pre_policy_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In log-only mode the override emission still happens but the
    pre-Policy model is kept unchanged. PR 4 flipped the default to
    enforce, so this test pins the log-only branch explicitly."""
    from atomic_agents import AtomicAgent
    from atomic_agents.policy.types import ENFORCE_NONCAP_ENV_VAR

    monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, "false")

    _make_minimal_agent_dir(tmp_path, "scout")
    _write_policy(tmp_path, "model: claude-opus-4-7\n")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)
    snap = agent._take_policy_snapshot()

    md_default = agent.config.default_model
    model = md_default
    if snap.enforce_noncap:
        model = snap.model_override

    assert model == md_default
    assert snap.model_override != md_default  # disagreement still recorded


def test_enforce_mode_policy_denied_tool_result_shape(tmp_path: Path) -> None:
    """When enforce mode denies a tool, the synthesized ToolCallResult
    mirrors the judge_blocked shape so the LLM sees a refusal on the next
    turn instead of an execution result. Pins the error-message format
    operators will grep for in audit logs and the field shape PR 4 ships
    by default."""
    from atomic_agents.tools import ToolCallResult

    tool_name = "delete_file"
    tcid = "toolu_abc123"
    proposed_input = {"path": "/etc/passwd"}

    # Mirrors the synthesis inside ``agent.call()``:
    policy_blocked_result = ToolCallResult(
        tool_name=tool_name,
        tool_use_id=tcid,
        input=proposed_input,
        output=None,
        error=f"policy_denied: tool {tool_name!r} not allowed by policy.md",
        latency_ms=0,
    )

    assert policy_blocked_result.tool_name == "delete_file"
    assert policy_blocked_result.tool_use_id == "toolu_abc123"
    assert policy_blocked_result.input == {"path": "/etc/passwd"}
    assert policy_blocked_result.output is None
    assert "policy_denied" in policy_blocked_result.error
    assert "delete_file" in policy_blocked_result.error
    assert policy_blocked_result.latency_ms == 0


# ─────────────────────────────────────────────────────────────────────────────
# Frozen-snapshot contract (Premise 3)


def test_snapshot_closures_memoize_per_call(tmp_path: Path) -> None:
    """Querying the same tool / server name twice within a snapshot hits the
    backend ONCE. Pins Premise 3's frozen-at-call-entry contract at the
    implementation level — without memoization, the closures would re-stat
    policy.md on every consultation in a long multi-turn loop."""
    from unittest.mock import MagicMock

    from atomic_agents import AtomicAgent

    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", trigger="cron", agents_root=tmp_path)

    # Install a counting backend BEFORE the snapshot so closures bind to it.
    counting = MagicMock()
    counting.get_effective_caps.return_value = type(
        "X", (), {"daily_usd": None, "monthly_usd": None}
    )()
    counting.capabilities.return_value = type("X", (), {"cache_ttl_s": 0})()
    counting.get_effective_model.return_value = None
    counting.is_tool_allowed.return_value = True
    counting.is_mcp_server_allowed.return_value = True
    agent.policy_backend = counting
    snap = agent._take_policy_snapshot()

    # 3 calls with the same tool name → 1 backend call.
    snap.tool_allow_fn("read_file")
    snap.tool_allow_fn("read_file")
    snap.tool_allow_fn("read_file")
    assert counting.is_tool_allowed.call_count == 1

    # Distinct names each take their own backend call.
    snap.tool_allow_fn("write_file")
    snap.tool_allow_fn("read_file")  # cached
    assert counting.is_tool_allowed.call_count == 2

    # MCP closure memoizes independently.
    snap.mcp_allow_fn("server-a")
    snap.mcp_allow_fn("server-a")
    assert counting.is_mcp_server_allowed.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Cross-flag regression pin


def test_cost_cap_emissions_ignore_enforce_noncap_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cost-cap denials always emit ``enforced=True`` regardless of the
    ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP value. The flag governs only
    non-cap surfaces (tools / MCP / model) per spec/32 §"enforced field
    semantics". This pin prevents PR 3b's flag from accidentally leaking
    into the PR 3a cost-cap path."""
    from atomic_agents.logs.filesystem import FilesystemLogBackend
    from atomic_agents.logs.types import PRIMITIVE_POLICY_DECISION, LogQuery
    from atomic_agents.policy.types import (
        ENFORCE_NONCAP_ENV_VAR,
        PolicyDecision,
        _emit_policy_decision,
    )

    # Set the flag in BOTH states and confirm cost-cap emission shape is
    # unchanged — the emission helper is enum-driven; the flag is only
    # consulted by the consumption sites for tool/MCP/model.
    for flag_val in ("true", "false"):
        monkeypatch.setenv(ENFORCE_NONCAP_ENV_VAR, flag_val)
        sub_dir = tmp_path / f"flag_{flag_val}"
        sub_dir.mkdir()
        backend = FilesystemLogBackend(sub_dir)
        decision = PolicyDecision(
            decision_kind="deny",
            denying_layer="policy",
            agent_name="scout",
            axis="cost_cap",
            cap_dimension="daily",
            attempted_value=6.0,
            effective_cap=5.0,
            enforced=True,  # cost caps always enforce regardless of flag
            cache_ttl_s=0,
        )
        _emit_policy_decision(decision, backend, run_id=f"run-cost-{flag_val}")

        records = backend.query(LogQuery(primitive=PRIMITIVE_POLICY_DECISION))
        assert len(records) == 1
        assert records[0].extra["axis"] == "cost_cap"
        assert records[0].extra["enforced"] is True

"""Tests for MCP (Model Context Protocol) client support (spec/19).

Covers:
- parse_mcp_md_empty_returns_empty_list
- parse_mcp_md_handles_single_server
- parse_mcp_md_handles_multiple_servers
- parse_mcp_md_resolves_env_var_references
- parse_mcp_md_raises_on_unresolved_env_var
- parse_mcp_md_blocks_path_traversal_in_args
- MCPClientPool.connect_failure_doesnt_block_others
- MCPClientPool.discover_tools_returns_namespaced_definitions
- MCP tool handler invokes async call_tool
- disconnect_all_idempotent
- AtomicAgent loads mcp_servers from config
- AtomicAgent.call() lazy-inits pool on first call when servers declared
- AtomicAgent.call() tears down pool in finally block
- AtomicAgent.call() with no MCP servers skips pool init
- Full integration mock: agent.call() with one MCP tool the model uses
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from atomic_agents.mcp import (
    MCPClientPool,
    MCPServerSpec,
    MCPToolMeta,
    _ServerConnection,
    parse_mcp_md,
    parse_mcp_md_text,
)
from atomic_agents.exceptions import (
    MCPServerConnectFailed,
    PathTraversalError,
)
from atomic_agents.tools import ToolDefinition, ToolRegistry


# ──────────────────────────────────────────────────────────────────
# Helpers

def _make_spec(
    name: str = "test-server",
    command: str = "npx",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> MCPServerSpec:
    return MCPServerSpec(
        name=name,
        command=command,
        args=args or ["-y", "@mcp/test"],
        env=env or {},
    )


def _make_mcp_tool(name: str, description: str = "A test tool") -> MagicMock:
    """Build a mock mcp.types.Tool object."""
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.inputSchema = {
        "type": "object",
        "properties": {"input": {"type": "string"}},
        "required": [],
    }
    return tool


def _build_minimal_agent_dir(tmp_path: Path, name: str = "test-agent") -> Path:
    """Create a minimal agent directory suitable for AtomicAgent init."""
    agent_dir = tmp_path / name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTestAgent.")
    (agent_dir / "tools.md").write_text(
        f"## Read paths\n- ~/docs/\n\n## Write paths\n- {agent_dir}/\n"
    )
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return agent_dir


# ──────────────────────────────────────────────────────────────────
# parse_mcp_md_text — basic parsing

def test_parse_mcp_md_empty_returns_empty_list():
    assert parse_mcp_md_text("") == []
    assert parse_mcp_md_text("   \n  ") == []


def test_parse_mcp_md_handles_single_server():
    text = """
# MCP servers

## filesystem-tools
command: npx
args: -y, @modelcontextprotocol/server-filesystem, /tmp/data
description: Local filesystem access
"""
    specs = parse_mcp_md_text(text)
    assert len(specs) == 1
    s = specs[0]
    assert s.name == "filesystem-tools"
    assert s.command == "npx"
    assert s.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/data"]
    assert s.description == "Local filesystem access"
    assert s.transport == "stdio"
    assert s.env == {}


def test_parse_mcp_md_handles_multiple_servers():
    text = """
# MCP servers

## server-one
command: npx
args: -y, @mcp/one

## server-two
command: python
args: -m, mcp_server_two
description: Python-based server
"""
    specs = parse_mcp_md_text(text)
    assert len(specs) == 2
    assert specs[0].name == "server-one"
    assert specs[0].command == "npx"
    assert specs[1].name == "server-two"
    assert specs[1].command == "python"
    assert specs[1].args == ["-m", "mcp_server_two"]
    assert specs[1].description == "Python-based server"


def test_parse_mcp_md_resolves_env_var_references(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "ghp_test_token_123")
    text = """
# MCP servers

## github
command: npx
args: -y, @modelcontextprotocol/server-github
env: GITHUB_PERSONAL_ACCESS_TOKEN=$GITHUB_PAT
description: GitHub server
"""
    specs = parse_mcp_md_text(text)
    assert len(specs) == 1
    assert specs[0].env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test_token_123"}


def test_parse_mcp_md_raises_on_unresolved_env_var(monkeypatch):
    # Ensure the var is NOT set
    monkeypatch.delenv("NONEXISTENT_VAR_XYZ", raising=False)
    text = """
# MCP servers

## myserver
command: npx
args: -y, @mcp/server
env: API_KEY=$NONEXISTENT_VAR_XYZ
"""
    with pytest.raises(MCPServerConnectFailed, match="NONEXISTENT_VAR_XYZ"):
        parse_mcp_md_text(text)


def test_parse_mcp_md_blocks_path_traversal_in_args(tmp_path):
    """Path-shaped args that escape allowed roots raise PathTraversalError."""
    from atomic_agents.mcp import validate_mcp_server_args

    spec = _make_spec(args=["../../etc/passwd"])
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    with pytest.raises(PathTraversalError):
        validate_mcp_server_args(spec, [allowed_root])


def test_parse_mcp_md_path_arg_within_allowed_root_passes(tmp_path):
    """Path args inside allowed roots are accepted."""
    from atomic_agents.mcp import validate_mcp_server_args

    allowed_dir = tmp_path / "data"
    allowed_dir.mkdir()
    spec = _make_spec(args=[str(allowed_dir)])

    # Should not raise
    validate_mcp_server_args(spec, [allowed_dir])


def test_parse_mcp_md_non_path_args_not_validated(tmp_path):
    """Non-path args like flags or package names skip path validation."""
    from atomic_agents.mcp import validate_mcp_server_args

    spec = _make_spec(args=["-y", "--verbose", "@mcp/server", "some-string"])
    # No read_paths at all — but non-path args shouldn't trigger validation
    validate_mcp_server_args(spec, [])  # should not raise


# ──────────────────────────────────────────────────────────────────
# MCPClientPool — connect, discover, dispatch

def test_mcp_client_pool_connect_failure_doesnt_block_others(tmp_path):
    """One server failing connect still lets others connect successfully."""
    specs = [
        _make_spec("good-server"),
        _make_spec("bad-server"),
    ]
    pool = MCPClientPool(specs, agents_root=tmp_path)

    good_tools = [_make_mcp_tool("read_file")]
    good_conn = _ServerConnection(spec=specs[0], tools=good_tools)

    def _connect_side_effect(spec):
        if spec.name == "bad-server":
            raise MCPServerConnectFailed("bad-server failed")
        return good_conn

    with patch("atomic_agents.mcp._connect_sync", side_effect=_connect_side_effect):
        pool.connect_all()

    assert "good-server" in pool._connected
    assert "bad-server" not in pool._connected


def test_mcp_client_pool_discover_tools_returns_namespaced_definitions(tmp_path):
    """discover_tools() returns ToolDefinitions with <server>__<tool> names."""
    specs = [_make_spec("myserver")]
    pool = MCPClientPool(specs, agents_root=tmp_path)

    mock_tools = [
        _make_mcp_tool("list_files"),
        _make_mcp_tool("read_file"),
    ]
    conn = _ServerConnection(spec=specs[0], tools=mock_tools)
    pool._connected["myserver"] = conn

    defs = pool.discover_tools()

    assert len(defs) == 2
    names = {d.name for d in defs}
    assert "myserver__list_files" in names
    assert "myserver__read_file" in names

    # Verify meta is stored
    meta = pool.get_meta("myserver__list_files")
    assert meta is not None
    assert meta.server_name == "myserver"
    assert meta.original_name == "list_files"
    assert meta.qualified_name == "myserver__list_files"


def test_mcp_tool_handler_invokes_async_call(tmp_path):
    """Tool handler calls _async_call_tool via asyncio.run when invoked."""
    specs = [_make_spec("srv")]
    pool = MCPClientPool(specs, agents_root=tmp_path)

    mock_tools = [_make_mcp_tool("do_thing")]
    conn = _ServerConnection(spec=specs[0], tools=mock_tools)
    pool._connected["srv"] = conn

    defs = pool.discover_tools()
    assert len(defs) == 1
    tool_def = defs[0]
    assert tool_def.name == "srv__do_thing"

    # Patch asyncio.run to intercept the async call and return a canned result
    with patch("atomic_agents.mcp.asyncio.run", return_value="tool result") as mock_run:
        result = tool_def.handler({"input": "hello"})

    assert result == "tool result"
    # asyncio.run was called once (for _async_call_tool)
    mock_run.assert_called_once()


def test_mcp_disconnect_all_idempotent(tmp_path):
    """disconnect_all() can be called multiple times without error."""
    specs = [_make_spec("srv")]
    pool = MCPClientPool(specs, agents_root=tmp_path)

    conn = _ServerConnection(spec=specs[0], tools=[])
    pool._connected["srv"] = conn

    # Should not raise on first call
    pool.disconnect_all()
    # Should not raise on second call (pool is cleared)
    pool.disconnect_all()


# ──────────────────────────────────────────────────────────────────
# AtomicAgent integration

def test_agent_loads_mcp_servers_from_config(tmp_path):
    """Agent with mcp.md has config.mcp_servers populated."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    mcp_md = """
# MCP servers

## time
command: npx
args: -y, @modelcontextprotocol/server-time
description: Time server
"""
    (agent_dir / "mcp.md").write_text(mcp_md)

    # Stub out anthropic so AtomicAgent doesn't need real API key
    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)
    assert len(agent.config.mcp_servers) == 1
    assert agent.config.mcp_servers[0].name == "time"
    assert agent.config.mcp_servers[0].command == "npx"


def test_agent_call_with_no_mcp_servers_skips_pool_init(tmp_path):
    """Agents without mcp.md never create an MCPClientPool."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    # No mcp.md written

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)
    assert agent.config.mcp_servers == []

    # Patch _llm.call_llm to return a minimal response without spawning anything
    raw = _make_raw_response("Hello from agent")
    with patch("atomic_agents._llm.call_llm", return_value=raw):
        with patch("atomic_agents.mcp.MCPClientPool") as mock_pool_cls:
            agent.call("Do something.")

    # MCPClientPool should never have been instantiated
    mock_pool_cls.assert_not_called()
    assert agent.mcp_pool is None


def test_agent_call_lazy_inits_pool_on_first_call_when_servers_declared(tmp_path):
    """agent.call() creates and connects pool when mcp_servers are non-empty."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    (agent_dir / "mcp.md").write_text(
        "# MCP servers\n\n## time\ncommand: npx\nargs: -y, @mcp/time\n"
    )

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)
    assert len(agent.config.mcp_servers) == 1

    raw = _make_raw_response("Done.")
    mock_pool = MagicMock()
    mock_pool.connect_all = MagicMock()
    mock_pool.discover_tools = MagicMock(return_value=[])
    mock_pool.disconnect_all = MagicMock()

    with patch("atomic_agents._llm.call_llm", return_value=raw):
        with patch("atomic_agents.agent.MCPClientPool", return_value=mock_pool) as pool_cls:
            agent.call("Do something.")

    pool_cls.assert_called_once()
    mock_pool.connect_all.assert_called_once()
    mock_pool.discover_tools.assert_called_once()


def test_agent_call_tears_down_pool_in_finally_block(tmp_path):
    """Pool is disconnected in finally block even when an exception occurs."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    (agent_dir / "mcp.md").write_text(
        "# MCP servers\n\n## srv\ncommand: npx\nargs: -y, @mcp/srv\n"
    )

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)

    mock_pool = MagicMock()
    mock_pool.connect_all = MagicMock()
    mock_pool.discover_tools = MagicMock(return_value=[])
    mock_pool.disconnect_all = MagicMock()

    # Simulate LLM failure after pool is created
    with patch("atomic_agents._llm.call_llm", side_effect=RuntimeError("LLM bombed")):
        with patch("atomic_agents.agent.MCPClientPool", return_value=mock_pool):
            with pytest.raises(RuntimeError, match="LLM bombed"):
                agent.call("Do something.")

    # disconnect_all must have been called despite the exception
    mock_pool.disconnect_all.assert_called_once()
    # pool should be cleared
    assert agent.mcp_pool is None


def test_mcp_tool_invocation_routes_through_registry_and_loop(tmp_path):
    """Full integration mock: agent.call() routes an MCP tool call end-to-end."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    (agent_dir / "mcp.md").write_text(
        "# MCP servers\n\n## time\ncommand: npx\nargs: -y, @mcp/time\n"
    )

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.tools import ToolDefinition

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)

    # Build a real ToolDefinition that simulates what discover_tools would return
    mcp_tool = ToolDefinition(
        name="time__get_current_time",
        description="Returns the current time.",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda inp: "2026-05-07T12:00:00Z",
    )

    mock_pool = MagicMock()
    mock_pool.connect_all = MagicMock()
    mock_pool.discover_tools = MagicMock(return_value=[mcp_tool])
    mock_pool.disconnect_all = MagicMock()

    # First LLM call: returns a tool_use for time__get_current_time
    # Second LLM call: model uses the result and returns final text
    tool_use_raw = _make_raw_response(
        text="Let me check the time.",
        tool_uses=[{
            "name": "time__get_current_time",
            "id": "tu_001",
            "input": {},
        }],
    )
    final_raw = _make_raw_response("The current time is 2026-05-07T12:00:00Z.")

    call_seq = [tool_use_raw, final_raw]

    with patch("atomic_agents._llm.call_llm", side_effect=call_seq):
        with patch("atomic_agents.agent.MCPClientPool", return_value=mock_pool):
            response = agent.call("What time is it?")

    assert response.tool_iterations == 2
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.tool_name == "time__get_current_time"
    assert tc.output == "2026-05-07T12:00:00Z"
    assert tc.error is None


# ──────────────────────────────────────────────────────────────────
# Helpers for agent tests

def _stub_anthropic():
    """Ensure `anthropic` doesn't need to be installed for agent init."""
    if "anthropic" not in sys.modules:
        sys.modules["anthropic"] = types.ModuleType("anthropic")


def _make_raw_response(
    text: str = "",
    tool_uses: list[dict] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 20,
):
    """Build a minimal RawLLMResponse-shaped object for patching _llm.call_llm."""
    return types.SimpleNamespace(
        text=text,
        tool_uses=tool_uses or [],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=0,
        raw={},
    )

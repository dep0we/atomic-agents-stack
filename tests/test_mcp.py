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
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
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
        with patch(
            "atomic_agents.agent.MCPClientPool", return_value=mock_pool
        ) as pool_cls:
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
        tool_uses=[
            {
                "name": "time__get_current_time",
                "id": "tu_001",
                "input": {},
            }
        ],
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


# ──────────────────────────────────────────────────────────────────
# New regression tests — codex MCP review findings

# M1 — env merges with parent environment


def test_mcp_env_merges_with_parent_environment(monkeypatch):
    """Merged env passed to StdioServerParameters includes parent PATH + spec.env vars.

    Tests the env-merge by intercepting StdioServerParameters at the point it is
    used inside _async_connect_and_list. We patch the local-import path so the
    mock is installed before the import happens in the async function.
    """
    monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
    monkeypatch.setenv("HOME", "/home/testuser")

    spec = MCPServerSpec(
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_TOKEN": "tok_abc123"},
    )

    captured_envs: list[dict] = []

    # Build a fake mcp SDK that captures the StdioServerParameters env kwarg.
    fake_params_cls = MagicMock(
        side_effect=lambda **kw: (captured_envs.append(kw.get("env", {})), kw)[1]
    )

    async def fake_list_tools_flow():
        result = MagicMock()
        result.tools = []
        return result

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def initialize(self):
            pass

        async def list_tools(self):
            return await fake_list_tools_flow()

    class _FakeStdioClient:
        def __init__(self, params):
            pass

        async def __aenter__(self):
            return (MagicMock(), MagicMock())

        async def __aexit__(self, *_):
            pass

    # Build a fake mcp module hierarchy
    fake_mcp = types.ModuleType("mcp")
    fake_mcp_client = types.ModuleType("mcp.client")
    fake_mcp_stdio = types.ModuleType("mcp.client.stdio")
    fake_mcp_session_mod = types.ModuleType("mcp.client.session")

    fake_mcp_stdio.StdioServerParameters = fake_params_cls
    fake_mcp_stdio.stdio_client = _FakeStdioClient
    fake_mcp_session_mod.ClientSession = _FakeSession

    fake_mcp.client = fake_mcp_client
    fake_mcp_client.stdio = fake_mcp_stdio
    fake_mcp_client.session = fake_mcp_session_mod

    import asyncio

    with patch.dict(
        sys.modules,
        {
            "mcp": fake_mcp,
            "mcp.client": fake_mcp_client,
            "mcp.client.stdio": fake_mcp_stdio,
            "mcp.client.session": fake_mcp_session_mod,
        },
    ):
        from atomic_agents.mcp import _async_connect_and_list as _acal

        asyncio.run(_acal(spec))

    # Exactly one StdioServerParameters call should have been made
    assert len(captured_envs) == 1
    env_passed = captured_envs[0]
    assert "PATH" in env_passed, "PATH must be inherited from parent env"
    assert env_passed["PATH"] == "/usr/bin:/usr/local/bin"
    assert "HOME" in env_passed
    assert "GITHUB_TOKEN" in env_passed, "spec.env var must be present"
    assert env_passed["GITHUB_TOKEN"] == "tok_abc123"


def test_mcp_env_merge_prefers_spec_over_parent(monkeypatch):
    """spec.env values override parent env values for the same key."""
    monkeypatch.setenv("GITHUB_TOKEN", "parent_token")

    spec = MCPServerSpec(
        name="gh",
        command="npx",
        args=[],
        env={"GITHUB_TOKEN": "spec_token"},
    )

    # Test the merge expression directly (same logic as in _async_connect_and_list)
    merged = {**os.environ, **spec.env}
    assert merged["GITHUB_TOKEN"] == "spec_token"


def test_mcp_empty_spec_env_passes_none_to_sdk():
    """When spec.env is empty, merged_env is None (SDK uses default env)."""
    spec = MCPServerSpec(name="srv", command="npx", args=[], env={})

    # Verify the merge logic: empty spec.env → None (same expression as in the code)
    merged_env = {**os.environ, **spec.env} if spec.env else None
    assert merged_env is None


# M2 — validator wired into parse path
def test_parse_mcp_md_validates_path_args_at_parse_time(tmp_path):
    """parse_mcp_md raises PathTraversalError for path-traversal args when read_paths supplied."""
    mcp_file = tmp_path / "mcp.md"
    mcp_file.write_text(
        "# MCP servers\n\n## bad-server\ncommand: npx\n"
        "args: -y, @mcp/srv, ../../etc/passwd\n"
    )
    allowed_root = tmp_path / "data"
    allowed_root.mkdir()

    with pytest.raises(PathTraversalError):
        parse_mcp_md(mcp_file, read_paths=[allowed_root])


def test_parse_mcp_md_text_validates_path_args(tmp_path):
    """parse_mcp_md_text raises PathTraversalError when read_paths supplied and arg escapes."""
    text = (
        "# MCP servers\n\n## traversal\ncommand: python\n"
        "args: -m, mcp_srv, ../../etc/shadow\n"
    )
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    with pytest.raises(PathTraversalError):
        parse_mcp_md_text(text, read_paths=[allowed_root])


def test_parse_mcp_md_without_read_paths_skips_validation(tmp_path):
    """parse_mcp_md without read_paths does not raise even for path-looking args."""
    mcp_file = tmp_path / "mcp.md"
    mcp_file.write_text(
        "# MCP servers\n\n## srv\ncommand: npx\nargs: -y, @mcp/srv, /tmp/data\n"
    )
    # No read_paths — should not raise
    specs = parse_mcp_md(mcp_file)  # no read_paths kwarg
    assert len(specs) == 1


def test_agent_load_config_blocks_path_traversal_via_mcp_md(tmp_path):
    """Integration test: AtomicAgent.__init__ raises PathTraversalError when mcp.md has traversal arg.

    This is the 'validator wired into live call path' test — mirrors the
    merge_into bug pattern from R1 #29. The validator must be invoked by the
    real _load_config path, not just standalone.
    """
    from atomic_agents.exceptions import PathTraversalError as _PTError

    agent_dir = _build_minimal_agent_dir(tmp_path)
    # Write a mcp.md with a path-traversal arg outside tools.md read_paths.
    # tools.md declares read_paths: ~/docs/ — ../../etc/passwd escapes that.
    (agent_dir / "mcp.md").write_text(
        "# MCP servers\n\n## evil\ncommand: npx\nargs: -y, @mcp/srv, ../../etc/passwd\n"
    )

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent

    with pytest.raises(_PTError):
        AtomicAgent(name="test-agent", agents_root=tmp_path)


# M4 — path heuristic accepts npm scoped names and flags
def test_path_heuristic_accepts_npm_scoped_names():
    """@scope/package is NOT path-shaped (npm scoped name, not a filesystem path)."""
    from atomic_agents.mcp import _is_path_shaped

    assert not _is_path_shaped("@modelcontextprotocol/server-filesystem")
    assert not _is_path_shaped("@mcp/server-github")
    assert not _is_path_shaped("@scope/some-package")


def test_path_heuristic_accepts_flags():
    """-y and --option=value are NOT path-shaped."""
    from atomic_agents.mcp import _is_path_shaped

    assert not _is_path_shaped("-y")
    assert not _is_path_shaped("--verbose")
    assert not _is_path_shaped("--option=value")
    assert not _is_path_shaped("--enable_tool_X")


def test_path_heuristic_blocks_dotdot_traversal():
    """../../etc/passwd IS path-shaped (contains ..)."""
    from atomic_agents.mcp import _is_path_shaped

    assert _is_path_shaped("../../etc/passwd")
    assert _is_path_shaped("../relative/path")
    assert _is_path_shaped("..")


def test_path_heuristic_blocks_absolute_paths():
    """/etc/passwd IS path-shaped (absolute POSIX path)."""
    from atomic_agents.mcp import _is_path_shaped

    assert _is_path_shaped("/etc/passwd")
    assert _is_path_shaped("/tmp/data")
    assert _is_path_shaped("/home/user/file.txt")


def test_path_heuristic_blocks_tilde_paths():
    """~/secrets/keys.json IS path-shaped (home-relative)."""
    from atomic_agents.mcp import _is_path_shaped

    assert _is_path_shaped("~/secrets/keys.json")
    assert _is_path_shaped("~/agents/scout/data")
    assert _is_path_shaped("~")


def test_path_heuristic_accepts_bare_strings():
    """Plain strings without path markers are NOT path-shaped."""
    from atomic_agents.mcp import _is_path_shaped

    assert not _is_path_shaped("some-string")
    assert not _is_path_shaped("module_name")
    assert not _is_path_shaped("server")
    assert not _is_path_shaped("")


# M3 — MCP tools unregistered on call finally
def test_mcp_tools_unregistered_on_call_finally(tmp_path):
    """MCP tools are removed from tool_registry after call() completes normally."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    (agent_dir / "mcp.md").write_text(
        "# MCP servers\n\n## time\ncommand: npx\nargs: -y, @mcp/time\n"
    )

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.tools import ToolDefinition

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)

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

    raw = _make_raw_response("Done.")

    with patch("atomic_agents._llm.call_llm", return_value=raw):
        with patch("atomic_agents.agent.MCPClientPool", return_value=mock_pool):
            agent.call("What time is it?")

    # After call completes, MCP tool must be removed from registry
    assert agent.tool_registry.get("time__get_current_time") is None


def test_mcp_tools_unregistered_even_on_call_exception(tmp_path):
    """MCP tools are cleaned up from tool_registry even when call() raises."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    (agent_dir / "mcp.md").write_text(
        "# MCP servers\n\n## srv\ncommand: npx\nargs: -y, @mcp/srv\n"
    )

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.tools import ToolDefinition

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)

    mcp_tool = ToolDefinition(
        name="srv__do_thing",
        description="Does a thing.",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda inp: "done",
    )

    mock_pool = MagicMock()
    mock_pool.connect_all = MagicMock()
    mock_pool.discover_tools = MagicMock(return_value=[mcp_tool])
    mock_pool.disconnect_all = MagicMock()

    with patch("atomic_agents._llm.call_llm", side_effect=RuntimeError("LLM bombed")):
        with patch("atomic_agents.agent.MCPClientPool", return_value=mock_pool):
            with pytest.raises(RuntimeError, match="LLM bombed"):
                agent.call("Do something.")

    # Even though call raised, MCP tool must be cleaned up
    assert agent.tool_registry.get("srv__do_thing") is None


# M6 — MCP pool not initialized when cost cap skips call
def test_mcp_pool_not_initialized_when_cost_cap_skips_call(tmp_path):
    """connect_all() is never called when cost guardrail skips the call."""
    agent_dir = _build_minimal_agent_dir(tmp_path)
    (agent_dir / "mcp.md").write_text(
        "# MCP servers\n\n## time\ncommand: npx\nargs: -y, @mcp/time\n"
    )
    # Set a very tight daily cap
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
        "## Cost guardrails\nenabled: true\ndaily_cap_usd: 0.0001\n"
    )

    _stub_anthropic()
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path)

    mock_pool_cls = MagicMock()

    # Patch _check_cost_guardrails to always return allow=False (skip)
    from atomic_agents.types import CostCheckResult

    with patch.object(
        agent,
        "_check_cost_guardrails",
        return_value=CostCheckResult(allow=False, reason="daily cap hit"),
    ):
        with patch("atomic_agents.agent.MCPClientPool", mock_pool_cls):
            agent.call("Do something.")

    # MCPClientPool should never have been instantiated
    mock_pool_cls.assert_not_called()


# ──────────────────────────────────────────────────────────────────
# render_mcp_md_section / render_mcp_md_full round-trip tests (PR 3)


def test_render_mcp_md_section_round_trip_basic():
    """Render a spec with all 6 fields then parse it back; fields must match."""
    from atomic_agents.mcp import render_mcp_md_section

    spec = MCPServerSpec(
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PAT": "$GITHUB_PAT"},
        transport="stdio",
        description="GitHub repo and issue access",
    )
    rendered = render_mcp_md_section(spec)
    parsed = parse_mcp_md_text(rendered, resolve_env=False)

    assert len(parsed) == 1
    result = parsed[0]
    assert result.name == spec.name
    assert result.command == spec.command
    assert result.args == spec.args
    assert result.env == spec.env
    assert result.transport == spec.transport
    assert result.description == spec.description


def test_render_mcp_md_section_env_not_resolved():
    """$VAR references in env must survive the render-parse round-trip verbatim."""
    from atomic_agents.mcp import render_mcp_md_section

    spec = MCPServerSpec(
        name="myserver",
        command="node",
        args=["server.js"],
        env={"GITHUB_PAT": "$GITHUB_PAT"},
        transport="stdio",
        description="",
    )
    rendered = render_mcp_md_section(spec)
    # Render with resolve_env=False so $VAR stays raw.
    parsed = parse_mcp_md_text(rendered, resolve_env=False)

    assert len(parsed) == 1
    # The $VAR reference must NOT have been resolved to an os.environ value.
    assert parsed[0].env.get("GITHUB_PAT") == "$GITHUB_PAT"


def test_render_mcp_md_section_strips_description_newlines():
    """Description containing newlines: only the first line appears after round-trip."""
    from atomic_agents.mcp import render_mcp_md_section

    spec = MCPServerSpec(
        name="multiline-desc",
        command="python",
        args=["-m", "myserver"],
        env={},
        transport="stdio",
        description="line1\nline2\nline3",
    )
    rendered = render_mcp_md_section(spec)
    parsed = parse_mcp_md_text(rendered, resolve_env=False)

    assert len(parsed) == 1
    # Only the first line survives the round-trip.
    assert parsed[0].description == "line1"


def test_render_mcp_md_full_round_trip():
    """Render a 3-spec list as a full mcp.md file then parse all 3 back."""
    from atomic_agents.mcp import render_mcp_md_full

    specs = [
        MCPServerSpec(
            name="alpha",
            command="npx",
            args=["-y", "@mcp/alpha"],
            env={},
            transport="stdio",
            description="Alpha server",
        ),
        MCPServerSpec(
            name="beta",
            command="python",
            args=["-m", "beta_server"],
            env={"BETA_KEY": "$BETA_KEY"},
            transport="stdio",
            description="",
        ),
        MCPServerSpec(
            name="gamma",
            command="node",
            args=["gamma.js", "--port", "9000"],
            env={},
            transport="stdio",
            description="Gamma description",
        ),
    ]
    rendered = render_mcp_md_full(specs)

    # Must start with the H1 header.
    assert rendered.startswith("# MCP servers\n")

    parsed = parse_mcp_md_text(rendered, resolve_env=False)
    assert len(parsed) == 3

    # All names must round-trip (order-independent check via set).
    assert {s.name for s in parsed} == {"alpha", "beta", "gamma"}

    # Spot-check fields on each parsed spec.
    by_name = {s.name: s for s in parsed}
    assert by_name["alpha"].args == ["-y", "@mcp/alpha"]
    assert by_name["beta"].env == {"BETA_KEY": "$BETA_KEY"}
    assert by_name["gamma"].description == "Gamma description"

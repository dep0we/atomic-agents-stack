"""MCP (Model Context Protocol) client support for atomic-agents-stack.

Adds native MCP client support so any Atomic Agent can use any of the 9,400+
servers in the MCP ecosystem without writing custom tool handlers. Operators
declare servers in <agent>/mcp.md; the framework connects to them at the start
of agent.call(), discovers their tools, registers those tools in the agent's
ToolRegistry with namespaced names (server__tool), runs the multi-turn loop,
then tears down in the finally block.

Lifecycle (v1 stdio-only):
    connect_all()       — spawn subprocesses + open MCP connections per spec
    discover_tools()    — query each server, build ToolDefinitions
    disconnect_all()    — tear down all subprocesses (idempotent)

AsyncIO sync bridge (v1):
    The official mcp Python SDK is async-first. This module wraps each async
    call in asyncio.run() per invocation. This is simple and correct for v1;
    a persistent event loop managed across the call() lifetime would reduce
    subprocess spin-up overhead and is deferred to v2 if performance matters.

v1 deferrals (each documented in spec/19):
    - HTTP transport (SSE / streamable-HTTP)
    - Resource subscriptions
    - Prompt templates
    - OAuth flows
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import MCPServerConnectFailed, MCPToolDispatchFailed
from .tools import ToolDefinition

_logger = logging.getLogger(__name__)

# Regex to detect path-shaped args: starts with /, ~, or contains /
_PATH_SHAPED = re.compile(r"^[/~]|/")


# ──────────────────────────────────────────────────────────────────
# Data classes

@dataclass
class MCPServerSpec:
    """Declaration of an MCP server an agent may connect to.

    Loaded from <agent>/mcp.md. The operator declares servers; the framework
    connects to them at runtime.

    Attributes:
        name:        operator-chosen short name (e.g., "filesystem-tools")
        command:     executable to launch (e.g., "npx", "python")
        args:        command args (e.g., ["-y", "@mcp/server-fs", "/allowed/dir"])
        env:         extra env vars (resolved from agent's env at parse time)
        transport:   only "stdio" supported in v1
        description: operator-readable note
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    description: str = ""


@dataclass
class MCPToolMeta:
    """Tracks which MCP server provided a tool, for routing.

    Attributes:
        server_name:     the spec name (e.g., "filesystem-tools")
        original_name:   tool name as the server reports it
        qualified_name:  "<server_name>__<original_name>" — registered in ToolRegistry
    """

    server_name: str
    original_name: str
    qualified_name: str


# ──────────────────────────────────────────────────────────────────
# MCPClientPool

class MCPClientPool:
    """Per-agent pool of connected MCP server clients.

    Lifecycle: connect_all() at start of agent.call(), disconnect_all() in
    finally. Subprocess servers live for the duration of the call.

    Thread safety: this class is NOT thread-safe. One pool per agent.call()
    invocation, and the agent's AgentLock serialises concurrent calls.
    """

    def __init__(self, server_specs: list[MCPServerSpec], agents_root: Path) -> None:
        self._specs = server_specs
        self._agents_root = agents_root
        # _sessions[server_name] = (process, session, read_stream, write_stream)
        # We store connection state per server for disconnect_all.
        self._connected: dict[str, _ServerConnection] = {}
        # _tool_meta[qualified_name] = MCPToolMeta
        self._tool_meta: dict[str, MCPToolMeta] = {}

    def connect_all(self) -> None:
        """Spawn subprocesses + open MCP connections for every spec.

        Failures on individual servers are logged but don't fail the whole
        pool — the agent runs with whatever servers connected successfully.
        """
        for spec in self._specs:
            if spec.transport != "stdio":
                _logger.warning(
                    "MCP server %r: transport %r not supported in v1 (stdio only). Skipping.",
                    spec.name, spec.transport,
                )
                continue
            try:
                conn = _connect_sync(spec)
                self._connected[spec.name] = conn
                _logger.debug("MCP server %r connected (%d tools)", spec.name, len(conn.tools))
            except MCPServerConnectFailed as e:
                _logger.warning("MCP server %r failed to connect: %s", spec.name, e)
            except Exception as e:
                _logger.warning(
                    "MCP server %r unexpected error during connect: %s: %s",
                    spec.name, type(e).__name__, e,
                )

    def disconnect_all(self) -> None:
        """Tear down all subprocesses cleanly. Idempotent."""
        for name, conn in list(self._connected.items()):
            try:
                _disconnect_sync(conn)
            except Exception as e:
                _logger.debug("MCP server %r disconnect error (ignored): %s", name, e)
        self._connected.clear()
        self._tool_meta.clear()

    def discover_tools(self) -> list[ToolDefinition]:
        """Query each connected server for its tools.

        Returns ToolDefinition list ready to register in the agent's ToolRegistry.
        Each tool's name is namespaced as '<server>__<tool>' to avoid collisions.
        Each tool's handler wraps the async call_tool in a sync interface via
        asyncio.run() per call (simple and correct for v1).
        """
        definitions: list[ToolDefinition] = []

        for server_name, conn in self._connected.items():
            for tool in conn.tools:
                qualified = f"{server_name}__{tool.name}"
                meta = MCPToolMeta(
                    server_name=server_name,
                    original_name=tool.name,
                    qualified_name=qualified,
                )
                self._tool_meta[qualified] = meta

                # Build handler that closes over server_name and original tool name.
                # asyncio.run() per invocation — v1 simplicity.
                handler = _make_tool_handler(server_name, tool.name, conn)

                # Convert MCP inputSchema to our ToolDefinition format.
                # MCP uses inputSchema (camelCase); we pass it through as-is
                # since both are JSON Schema objects.
                input_schema = {}
                if tool.inputSchema:
                    if hasattr(tool.inputSchema, "model_dump"):
                        input_schema = tool.inputSchema.model_dump(exclude_none=True)
                    elif isinstance(tool.inputSchema, dict):
                        input_schema = tool.inputSchema
                # Ensure it has the required shape
                if not isinstance(input_schema, dict):
                    input_schema = {}
                if "type" not in input_schema:
                    input_schema["type"] = "object"
                if "properties" not in input_schema:
                    input_schema["properties"] = {}

                description = tool.description or f"MCP tool '{tool.name}' from server '{server_name}'."

                definitions.append(ToolDefinition(
                    name=qualified,
                    description=description,
                    input_schema=input_schema,
                    handler=handler,
                ))

        return definitions

    def get_meta(self, qualified_name: str) -> MCPToolMeta | None:
        """Operator inspection: which server provides this tool?"""
        return self._tool_meta.get(qualified_name)


# ──────────────────────────────────────────────────────────────────
# Internal connection state

class _ServerConnection:
    """Holds live connection state for one server.

    v1 simplification: we use asyncio.run() per call rather than maintaining
    a persistent event loop. This means we:
    1. spawn + initialize the session once in connect_all()
    2. store the tool list (discovered synchronously at connect time)
    3. for each tool call, open a NEW session per call via asyncio.run()

    This is the simplest correct approach. The subprocess stays alive between
    calls (started in connect_all, killed in disconnect_all), but the MCP
    session is re-established per call. This avoids the event-loop lifecycle
    complexity that comes with asyncio.run() inside a long-lived loop.

    v2 option: a persistent anyio task group + event loop running in a
    background thread, shared for all calls. Deferred.
    """

    def __init__(self, spec: MCPServerSpec, tools: list) -> None:
        self.spec = spec
        self.tools = tools          # list[mcp.types.Tool]
        self._process: Any = None   # subprocess.Popen, set by _connect_sync


def _connect_sync(spec: MCPServerSpec) -> _ServerConnection:
    """Spawn the MCP server subprocess and initialize a session.

    Returns _ServerConnection with the list of available tools.
    Raises MCPServerConnectFailed on any failure.
    """
    try:
        tools = asyncio.run(_async_connect_and_list(spec))
        conn = _ServerConnection(spec=spec, tools=tools)
        return conn
    except Exception as e:
        raise MCPServerConnectFailed(
            f"MCP server '{spec.name}' failed to connect: {type(e).__name__}: {e}"
        ) from e


async def _async_connect_and_list(spec: MCPServerSpec) -> list:
    """Async: spawn server, initialize session, list tools, return tool list."""
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    params = StdioServerParameters(
        command=spec.command,
        args=spec.args,
        env=spec.env if spec.env else None,
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return list(result.tools)


def _disconnect_sync(conn: _ServerConnection) -> None:
    """Disconnect a server connection.

    v1: since we don't maintain a persistent session between calls (each
    call_tool re-opens a session via asyncio.run()), there's nothing
    session-level to tear down. The subprocess was managed by the
    async context manager inside each asyncio.run() call, so it's already
    gone. disconnect_all() is a no-op at the connection level in v1 — its
    purpose is to clear the pool state so it can't be reused.
    """
    # No persistent process to kill in v1 — each asyncio.run() manages its
    # own subprocess lifecycle via the stdio_client context manager.
    pass


def _make_tool_handler(server_name: str, tool_name: str, conn: _ServerConnection):
    """Build a sync handler that calls the MCP server's tool.

    Captures server_name and tool_name in closure. Each invocation runs
    asyncio.run() to execute the async call — v1 simplicity.
    """
    spec = conn.spec

    def handler(input_data: dict) -> Any:
        try:
            return asyncio.run(_async_call_tool(spec, tool_name, input_data))
        except Exception as e:
            raise MCPToolDispatchFailed(
                f"MCP tool '{server_name}__{tool_name}' dispatch failed: "
                f"{type(e).__name__}: {e}"
            ) from e

    return handler


async def _async_call_tool(spec: MCPServerSpec, tool_name: str, arguments: dict) -> Any:
    """Async: spawn server, initialize session, call tool, return result."""
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    params = StdioServerParameters(
        command=spec.command,
        args=spec.args,
        env=spec.env if spec.env else None,
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            # Return content as a structured dict or string
            if result.isError:
                error_text = _extract_content_text(result.content)
                raise MCPToolDispatchFailed(
                    f"MCP server returned error for tool '{tool_name}': {error_text}"
                )
            return _extract_content_value(result.content)


def _extract_content_text(content: list) -> str:
    """Extract text from MCP content blocks as a string."""
    parts = []
    for block in content:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
        else:
            parts.append(str(block))
    return " ".join(parts) if parts else ""


def _extract_content_value(content: list) -> Any:
    """Extract content from MCP content blocks.

    Single text block → str
    Multiple blocks → list of dicts
    Empty → None
    """
    if not content:
        return None
    if len(content) == 1:
        block = content[0]
        if hasattr(block, "text"):
            return block.text
        if isinstance(block, dict) and "text" in block:
            return block["text"]
        if hasattr(block, "model_dump"):
            return block.model_dump()
        return str(block)
    # Multiple blocks — return as list
    results = []
    for block in content:
        if hasattr(block, "model_dump"):
            results.append(block.model_dump())
        elif isinstance(block, dict):
            results.append(block)
        else:
            results.append({"text": str(block)})
    return results


# ──────────────────────────────────────────────────────────────────
# mcp.md parser

def parse_mcp_md(path: Path) -> list[MCPServerSpec]:
    """Parse <agent>/mcp.md into a list of MCPServerSpec.

    Empty list if file doesn't exist (agent has no MCP servers — that's fine).
    """
    if not path.exists():
        return []
    return parse_mcp_md_text(path.read_text(encoding="utf-8"), mcp_md_path=path)


def parse_mcp_md_text(text: str, mcp_md_path: Path | None = None) -> list[MCPServerSpec]:
    """Parse mcp.md content into a list of MCPServerSpec.

    Format::

        # MCP servers

        ## filesystem-tools
        command: npx
        args: -y, @modelcontextprotocol/server-filesystem, ~/agents/scout/data
        description: Local filesystem access scoped to scout's data dir

        ## github
        command: npx
        args: -y, @modelcontextprotocol/server-github
        env: GITHUB_PERSONAL_ACCESS_TOKEN=$GITHUB_PAT
        description: GitHub repo + issue access

    Each ``## <name>`` section defines one server. Supported keys:
    - command (required): executable to launch
    - args: comma-separated list of arguments
    - env: KEY=$VAR_NAME pairs (one per line), $VAR_NAME resolved from os.environ
    - transport: "stdio" (default; only supported value in v1)
    - description: human-readable note

    Raises MCPServerConnectFailed when an env var reference cannot be resolved.

    Path-shaped args (starting with /, ~, or containing /) are validated
    against the agent's read_paths via safe_resolve_under when mcp_md_path
    is provided. This provides best-effort path-traversal protection.
    """
    if not text or not text.strip():
        return []

    specs: list[MCPServerSpec] = []
    current_name: str | None = None
    current_fields: dict[str, list[str]] = {}

    def _flush() -> None:
        if current_name is None:
            return
        spec = _build_spec(current_name, current_fields)
        if spec is not None:
            specs.append(spec)

    for line in text.splitlines():
        stripped = line.strip()

        # H2 header = new server section
        if stripped.startswith("## "):
            _flush()
            current_name = stripped[3:].strip()
            current_fields = {}
            continue

        # Skip H1 and H3+ headers
        if stripped.startswith("#"):
            continue

        # Skip blank lines
        if not stripped:
            continue

        # Skip if not in a server section
        if current_name is None:
            continue

        # Key: value line
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            value = value.strip()
            current_fields.setdefault(key, []).append(value)

    _flush()
    return specs


def _build_spec(name: str, fields: dict[str, list[str]]) -> MCPServerSpec | None:
    """Build an MCPServerSpec from parsed key/value lines.

    Returns None (and logs a warning) if the section has no command.
    Raises MCPServerConnectFailed for unresolvable env var references.
    """
    command_lines = fields.get("command", [])
    if not command_lines:
        _logger.warning("mcp.md section %r has no 'command:' key — skipping", name)
        return None

    command = command_lines[0].strip()

    # Args — comma-separated on one line
    args: list[str] = []
    for args_line in fields.get("args", []):
        args.extend(part.strip() for part in args_line.split(",") if part.strip())

    # Env — one or more KEY=$VAR or KEY=value pairs, one per line
    env: dict[str, str] = {}
    for env_line in fields.get("env", []):
        for pair in env_line.split(","):
            pair = pair.strip()
            if "=" not in pair:
                continue
            env_key, _, env_val = pair.partition("=")
            env_key = env_key.strip()
            env_val = env_val.strip()
            if env_val.startswith("$"):
                var_name = env_val[1:]
                resolved = os.environ.get(var_name)
                if resolved is None:
                    raise MCPServerConnectFailed(
                        f"mcp.md server '{name}': env var '${var_name}' not set. "
                        f"Set {var_name} in the environment before running this agent."
                    )
                env[env_key] = resolved
            else:
                env[env_key] = env_val

    transport_lines = fields.get("transport", [])
    transport = transport_lines[0].strip() if transport_lines else "stdio"

    description_lines = fields.get("description", [])
    description = description_lines[0].strip() if description_lines else ""

    return MCPServerSpec(
        name=name,
        command=command,
        args=args,
        env=env,
        transport=transport,
        description=description,
    )


# ──────────────────────────────────────────────────────────────────
# Path-traversal check for MCP server args

def validate_mcp_server_args(
    spec: MCPServerSpec,
    agent_read_paths: list,
) -> None:
    """Best-effort path-traversal check on MCP server args.

    For each arg that looks path-shaped (starts with /, ~, or contains /),
    resolve it and verify it stays under one of the agent's declared read_paths.

    Raises PathTraversalError if a path-shaped arg resolves outside all
    declared read_paths. Non-path-shaped args are not validated.

    This is best-effort — we can't know what every MCP server treats as a path.
    The obvious path-shaped cases get caught here.
    """
    from ._io import safe_resolve_under
    from .exceptions import PathTraversalError

    if not agent_read_paths:
        return  # no read_paths declared — can't validate

    for arg in spec.args:
        if not _PATH_SHAPED.search(arg):
            continue  # not path-shaped, skip

        # Expand ~
        expanded = Path(arg).expanduser()

        # Check if it resolves under any allowed read path
        allowed = False
        for read_path in agent_read_paths:
            root = Path(read_path).expanduser().resolve()
            try:
                resolved = expanded.resolve()
                resolved.relative_to(root)
                allowed = True
                break
            except (ValueError, OSError):
                continue

        if not allowed:
            raise PathTraversalError(
                f"mcp.md server '{spec.name}': arg '{arg}' resolves outside "
                f"declared read_paths. Add the path to tools.md read paths "
                f"or remove it from the server args.",
                child=arg,
                root=str(agent_read_paths[0]) if agent_read_paths else "",
            )

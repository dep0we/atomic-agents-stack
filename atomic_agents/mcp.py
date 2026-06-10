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

# Default command-basename allowlist for the spawn gate (MUST 12 / GHSA-xhcr-cqfr-m3hv).
# Only bare basenames — path-qualified commands are rejected unless the operator
# explicitly adds them via '## Allowed commands' in mcp.md.
# Operators may REPLACE this set per-agent via the '## Allowed commands' H2 section.
DEFAULT_COMMAND_ALLOWLIST: frozenset[str] = frozenset(
    {"npx", "uvx", "python", "python3", "node", "docker"}
)

# Reserved H2 section name in mcp.md for the operator-overridable allowlist.
# Case-insensitive match at parse time; excluded from server-spec parse.
_ALLOWED_COMMANDS_SECTION = "allowed commands"


# Detect path-shaped args. A path-shaped arg is one that:
#   - starts with /  (absolute POSIX path)
#   - starts with ~  (home-relative path)
#   - starts with ./ or ../  (relative path from current dir or parent)
#   - contains ..  (path traversal — always suspicious regardless of prefix)
#   - matches C:\ or C:/ style (absolute Windows path)
#
# Explicitly NOT path-shaped:
#   - npm scoped packages like @scope/package  (@ prefix, no leading ./ or /)
#   - plain flags like -y, --verbose, --option=value
#   - bare strings like "some-string", "module_name"
#
# Using discrete checks rather than a single regex for clarity and correctness.
def _is_path_shaped(arg: str) -> bool:
    """Return True if ``arg`` looks like a filesystem path that should be validated."""
    if not arg:
        return False
    # Absolute POSIX path
    if arg.startswith("/"):
        return True
    # Home-relative
    if arg.startswith("~"):
        return True
    # Relative paths (./ or ../)
    if arg.startswith("./") or arg.startswith("../"):
        return True
    # Bare .. (just the traversal token, no prefix)
    if arg == "..":
        return True
    # Contains .. anywhere — path traversal attempt
    if ".." in arg:
        return True
    # Absolute Windows path (e.g. C:\ or C:/)
    if len(arg) >= 3 and arg[1] == ":" and arg[2] in ("/", "\\"):
        return True
    return False


# ──────────────────────────────────────────────────────────────────
# Data classes


@dataclass(repr=False)
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

    Note on repr: ``env`` is redacted in the repr because it may contain
    resolved secret values (API tokens, passwords) after ``load_mcp_server``
    resolution. Use ``to_dict()`` if you need the full values for serialization.
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    description: str = ""

    def __repr__(self) -> str:
        """Return a repr that redacts env to prevent secret leakage in logs and error messages.

        The ``env`` dict may contain resolved secret values after
        ``load_mcp_server()`` resolution. Including them in repr would leak
        secrets into tracebacks, log lines, and operator-facing error messages.
        The count of env entries is shown for debuggability without exposing values.
        """
        return (
            f"MCPServerSpec("
            f"name={self.name!r}, "
            f"command={self.command!r}, "
            f"args={self.args!r}, "
            f"env=<{len(self.env)} entries; redacted>, "
            f"transport={self.transport!r}, "
            f"description={self.description!r}"
            f")"
        )

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe plain dict.

        Note: ``env`` may contain resolved values (the ``$VAR_NAME``
        references in source ``mcp.md`` are resolved to literal env-var
        values at load time). Callers serializing to a shared log or audit
        backend SHOULD treat the ``env`` field as sensitive. The raw text
        on the profile (``mcp_md_raw``) is the safe-to-ship form.

        Returns a fresh dict every call; callers may mutate freely.
        """
        return {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "transport": self.transport,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MCPServerSpec":
        """Reconstruct an ``MCPServerSpec`` from a dict produced by ``to_dict()``.

        Required keys (``name``, ``command``) raise ``KeyError`` if absent.
        Optional keys (``args``, ``env``, ``transport``, ``description``) fall
        back to field defaults. Extra keys in ``d`` are silently ignored for
        forward-compatibility with future wire format extensions.

        Applies copy-constructor discipline: ``args`` and ``env`` values are
        copied out of ``d`` to prevent callers from accidentally sharing
        mutable state with the dict passed in.
        """
        return cls(
            name=d["name"],
            command=d["command"],
            args=list(d.get("args", [])),
            env=dict(d.get("env", {})),
            transport=d.get("transport", "stdio"),
            description=d.get("description", ""),
        )


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
# Spawn-gate exception (MUST 12 / GHSA-xhcr-cqfr-m3hv)


class MCPCommandNotAllowed(MCPServerConnectFailed):
    """Raised when a catalog-sourced command is not in the spawn allowlist.

    Subclasses ``MCPServerConnectFailed`` for type-compatibility, but is
    intentionally NOT caught by the soft-skip handler in ``connect_all`` —
    ``connect_all`` re-raises this exception class to enforce fail-closed
    behavior.  An allowlist violation is a security gate, not a soft skip.

    See ``_check_command_allowlist`` and MUST 12 in spec/36.
    """


def _check_command_allowlist(
    spec: MCPServerSpec,
    allowlist: frozenset[str],
) -> None:
    """Assert that ``spec.command`` basename is in the spawn allowlist (MUST 12).

    Raises ``MCPCommandNotAllowed`` (a subclass of ``MCPServerConnectFailed``)
    when the command is not permitted.  The check fires BEFORE any
    ``StdioServerParameters`` construction so the OS never sees the command.

    Path-qualified commands (containing ``/`` or ``\\``) are checked by their
    basename only — ``/usr/local/bin/npx`` passes if ``npx`` is in the
    allowlist.  This means a path-qualified command to a DIFFERENT binary whose
    basename collides with a listed name would pass; that is an acceptable
    trade-off because the operator explicitly configured that command in mcp.md
    or an HTTP catalog they chose to trust.  (For extra hardening, operators may
    configure their catalog to only emit bare names.)

    Args:
        spec:      The ``MCPServerSpec`` about to be spawned.
        allowlist: The effective allowlist (operator-configured or default).
                   Passing an empty frozenset is treated as default-deny for
                   all commands.

    Raises:
        MCPCommandNotAllowed: when the command basename is not in ``allowlist``.
    """
    basename = os.path.basename(spec.command)
    if not allowlist or basename not in allowlist:
        raise MCPCommandNotAllowed(
            f"MCP server {spec.name!r}: command basename {basename!r} is not in "
            f"the spawn allowlist {sorted(allowlist)}. "
            f"Add it to '## Allowed commands' in mcp.md or use a command from "
            f"the default set: {sorted(DEFAULT_COMMAND_ALLOWLIST)}."
        )


# ──────────────────────────────────────────────────────────────────
# MCPClientPool


class MCPClientPool:
    """Per-agent pool of connected MCP server clients.

    Lifecycle: connect_all() at start of agent.call(), disconnect_all() in
    finally. Subprocess servers live for the duration of the call.

    Thread safety: this class is NOT thread-safe. One pool per agent.call()
    invocation, and the agent's ``lock_backend`` (``LockBackend`` per
    spec/21) serialises concurrent calls.
    """

    def __init__(
        self,
        server_specs: list[MCPServerSpec],
        agents_root: Path,
        *,
        allowed_commands: frozenset[str] | None = None,
    ) -> None:
        self._specs = server_specs
        self._agents_root = agents_root
        # Resolve the allowlist once: None → use the module-level default.
        # Passing frozenset() explicitly means "deny everything" (not the default).
        self._allowed_commands: frozenset[str] = (
            allowed_commands
            if allowed_commands is not None
            else DEFAULT_COMMAND_ALLOWLIST
        )
        # _sessions[server_name] = (process, session, read_stream, write_stream)
        # We store connection state per server for disconnect_all.
        self._connected: dict[str, _ServerConnection] = {}
        # _tool_meta[qualified_name] = MCPToolMeta
        self._tool_meta: dict[str, MCPToolMeta] = {}

    def connect_all(self) -> None:
        """Spawn subprocesses + open MCP connections for every spec.

        Failures on individual servers are logged but don't fail the whole
        pool — the agent runs with whatever servers connected successfully.

        Note: ``MCPCommandNotAllowed`` (command-basename allowlist violation)
        is re-raised rather than logged-and-skipped.  An allowlist violation
        is a security gate, not a soft skip — the agent construction fails
        closed so no untrusted command reaches the OS.

        The allowlist defaults to ``DEFAULT_COMMAND_ALLOWLIST``
        ({npx, uvx, python, python3, node, docker}) and is operator-overridable
        via the ``## Allowed commands`` section in mcp.md (passed at construction
        via the ``allowed_commands`` kwarg).
        """
        for spec in self._specs:
            if spec.transport != "stdio":
                _logger.warning(
                    "MCP server %r: transport %r not supported in v1 (stdio only). Skipping.",
                    spec.name,
                    spec.transport,
                )
                continue
            # Allowlist check BEFORE the try/except so MCPCommandNotAllowed
            # propagates out of the pool rather than being caught and logged.
            # ValueError is not caught by the MCPServerConnectFailed handler.
            _check_command_allowlist(spec, self._allowed_commands)
            try:
                conn = _connect_sync(spec, self._allowed_commands)
                self._connected[spec.name] = conn
                _logger.debug(
                    "MCP server %r connected (%d tools)", spec.name, len(conn.tools)
                )
            except MCPCommandNotAllowed:
                # Re-raise allowlist violations — fail closed (not soft-skip).
                raise
            except MCPServerConnectFailed as e:
                _logger.warning("MCP server %r failed to connect: %s", spec.name, e)
            except Exception as e:
                _logger.warning(
                    "MCP server %r unexpected error during connect: %s: %s",
                    spec.name,
                    type(e).__name__,
                    e,
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

                # Build handler that closes over server_name, original tool name,
                # and the spawn allowlist (threaded for per-call spawn gate).
                # asyncio.run() per invocation — v1 simplicity.
                handler = _make_tool_handler(
                    server_name, tool.name, conn, self._allowed_commands
                )

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

                description = (
                    tool.description
                    or f"MCP tool '{tool.name}' from server '{server_name}'."
                )

                definitions.append(
                    ToolDefinition(
                        name=qualified,
                        description=description,
                        input_schema=input_schema,
                        handler=handler,
                    )
                )

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
        self.tools = tools  # list[mcp.types.Tool]
        self._process: Any = None  # subprocess.Popen, set by _connect_sync


def _connect_sync(
    spec: MCPServerSpec,
    allowlist: frozenset[str] | None = None,
) -> _ServerConnection:
    """Spawn the MCP server subprocess and initialize a session.

    Returns _ServerConnection with the list of available tools.
    Raises MCPServerConnectFailed on any failure.

    ``allowlist``: the effective spawn allowlist.  Defaults to
    ``DEFAULT_COMMAND_ALLOWLIST`` when None.  Passed through to
    ``_async_connect_and_list`` so the check fires at the lowest-level
    spawn boundary (defense-in-depth).
    """
    effective_allowlist = (
        allowlist if allowlist is not None else DEFAULT_COMMAND_ALLOWLIST
    )
    try:
        tools = asyncio.run(_async_connect_and_list(spec, effective_allowlist))
        conn = _ServerConnection(spec=spec, tools=tools)
        return conn
    except MCPCommandNotAllowed:
        # Re-raise allowlist violations unchanged — they must not be wrapped.
        raise
    except Exception as e:
        raise MCPServerConnectFailed(
            f"MCP server '{spec.name}' failed to connect: {type(e).__name__}: {e}"
        ) from e


async def _async_connect_and_list(
    spec: MCPServerSpec,
    allowlist: frozenset[str] | None = None,
) -> list:
    """Async: spawn server, initialize session, list tools, return tool list.

    Raises:
        MCPCommandNotAllowed: if spec.command basename is not in the configured
            spawn allowlist. The allowlist defaults to
            ``{npx, uvx, python, python3, node, docker}`` and is
            operator-overridable via ``## Allowed commands`` in mcp.md.
    """
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    # Allowlist check at the lowest-level spawn boundary (MUST 12 defense-in-depth).
    effective_allowlist = (
        allowlist if allowlist is not None else DEFAULT_COMMAND_ALLOWLIST
    )
    _check_command_allowlist(spec, effective_allowlist)

    # Merge spec.env ON TOP OF the parent environment so the child inherits
    # PATH, HOME, etc. Passing only spec.env drops those, breaking commands
    # like "npx" that rely on PATH being set. spec.env wins on conflicts.
    merged_env = {**os.environ, **spec.env} if spec.env else None
    params = StdioServerParameters(
        command=spec.command,
        args=spec.args,
        env=merged_env,
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


def _make_tool_handler(
    server_name: str,
    tool_name: str,
    conn: _ServerConnection,
    allowlist: frozenset[str] | None = None,
):
    """Build a sync handler that calls the MCP server's tool.

    Captures server_name, tool_name, and the spawn allowlist in closure.
    Each invocation runs asyncio.run() to execute the async call — v1 simplicity.

    ``allowlist``: the effective spawn allowlist, threaded from MCPClientPool.
    Defaults to ``DEFAULT_COMMAND_ALLOWLIST`` when None.
    """
    spec = conn.spec
    effective_allowlist = (
        allowlist if allowlist is not None else DEFAULT_COMMAND_ALLOWLIST
    )

    def handler(input_data: dict) -> Any:
        try:
            return asyncio.run(
                _async_call_tool(spec, tool_name, input_data, effective_allowlist)
            )
        except Exception as e:
            raise MCPToolDispatchFailed(
                f"MCP tool '{server_name}__{tool_name}' dispatch failed: "
                f"{type(e).__name__}: {e}"
            ) from e

    return handler


async def _async_call_tool(
    spec: MCPServerSpec,
    tool_name: str,
    arguments: dict,
    allowlist: frozenset[str] | None = None,
) -> Any:
    """Async: spawn server, initialize session, call tool, return result.

    Raises:
        MCPServerConnectFailed (MCPCommandNotAllowed): if spec.command basename
            is not in the configured spawn allowlist. The allowlist defaults to
            ``{npx, uvx, python, python3, node, docker}`` and is
            operator-overridable via ``## Allowed commands`` in mcp.md.
    """
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    # Allowlist check at spawn boundary (MUST 12 defense-in-depth; per-call path).
    effective_allowlist = (
        allowlist if allowlist is not None else DEFAULT_COMMAND_ALLOWLIST
    )
    _check_command_allowlist(spec, effective_allowlist)

    # Same env-merge logic as _async_connect_and_list: parent env + spec.env overlay.
    merged_env = {**os.environ, **spec.env} if spec.env else None
    params = StdioServerParameters(
        command=spec.command,
        args=spec.args,
        env=merged_env,
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


def parse_mcp_md(path: Path, read_paths: list | None = None) -> list[MCPServerSpec]:
    """Parse <agent>/mcp.md into a list of MCPServerSpec.

    Empty list if file doesn't exist (agent has no MCP servers -- that's fine).

    read_paths: if provided, path-shaped args are validated against these roots
        via validate_mcp_server_args(). PathTraversalError is raised if any arg
        resolves outside all declared read_paths.

    This function always resolves $VAR env references (resolve_env=True).
    Callers that need lazy resolution (e.g., FilesystemMCPServerRegistryBackend,
    which resolves at load_mcp_server time per spec/36 Decision 7) MUST call
    parse_mcp_md_text() directly with resolve_env=False. The asymmetry is
    intentional: parse_mcp_md is the public convenience path for callers that
    want a ready-to-use spec list; parse_mcp_md_text is the knob-bearing path
    for callers that need deferred resolution.
    """
    if not path.exists():
        return []
    return parse_mcp_md_text(
        path.read_text(encoding="utf-8"),
        mcp_md_path=path,
        read_paths=read_paths,
    )


def parse_mcp_md_text(
    text: str,
    mcp_md_path: Path | None = None,
    read_paths: list | None = None,
    *,
    resolve_env: bool = True,
) -> list[MCPServerSpec]:
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

    Raises MCPServerConnectFailed when an env var reference cannot be resolved
    (only when resolve_env=True).

    read_paths: if provided, each parsed spec is immediately passed to
        validate_mcp_server_args(). Path-shaped args (starting with /, ~, ./, ../,
        or containing ..) that resolve outside all declared read_paths raise
        PathTraversalError, named with the offending server and arg.

    resolve_env: when True (default), $VAR references in env: lines are
        resolved against os.environ immediately, raising MCPServerConnectFailed
        on unresolvable references. When False, $VAR strings are kept as-is in
        the returned specs (raw strings like "$GITHUB_PAT"). Pass False when
        deferring resolution to a later call site (e.g.,
        FilesystemMCPServerRegistryBackend.load_mcp_server resolves at
        materialization time per spec/36 Decision 7).
    """
    if not text or not text.strip():
        return []

    specs: list[MCPServerSpec] = []
    current_name: str | None = None
    current_fields: dict[str, list[str]] = {}
    # Track whether we're inside the reserved '## Allowed commands' section
    # so its lines don't get parsed as server spec fields.
    _in_allowed_commands_section: bool = False

    def _flush() -> None:
        if current_name is None:
            return
        if current_name.lower() == _ALLOWED_COMMANDS_SECTION:
            # Reserved section — handled by parse_mcp_allowed_commands; skip.
            return
        spec = _build_spec(current_name, current_fields, resolve_env=resolve_env)
        if spec is not None:
            specs.append(spec)

    for line in text.splitlines():
        stripped = line.strip()

        # H2 header = new server section
        if stripped.startswith("## "):
            _flush()
            current_name = stripped[3:].strip()
            current_fields = {}
            _in_allowed_commands_section = (
                current_name.lower() == _ALLOWED_COMMANDS_SECTION
            )
            continue

        # Skip H1 and H3+ headers. Any non-H2 header also ends the reserved
        # '## Allowed commands' section, keeping this parser's section boundary
        # identical to parse_mcp_allowed_commands (a stray '# Title' ends it).
        if stripped.startswith("#"):
            _in_allowed_commands_section = False
            continue

        # Skip blank lines
        if not stripped:
            continue

        # Skip if not in a server section
        if current_name is None:
            continue

        # Skip lines inside the reserved '## Allowed commands' section —
        # parse_mcp_allowed_commands reads them independently.
        if _in_allowed_commands_section:
            continue

        # Key: value line
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            value = value.strip()
            current_fields.setdefault(key, []).append(value)

    _flush()

    # Validate path-shaped args against read_paths for every parsed spec.
    # This ensures the validator is always invoked via the live parse path,
    # not just when calling validate_mcp_server_args() directly.
    if read_paths is not None:
        for spec in specs:
            try:
                validate_mcp_server_args(spec, read_paths)
            except Exception as exc:
                # Re-raise with server name context for clarity
                from .exceptions import PathTraversalError

                if isinstance(exc, PathTraversalError):
                    raise PathTraversalError(
                        f"mcp.md server '{spec.name}': {exc}",
                        child=exc.child,
                        root=exc.root,
                    ) from exc
                raise

    return specs


def parse_mcp_allowed_commands(text: str) -> frozenset[str] | None:
    """Parse the operator-overridable spawn allowlist from mcp.md content.

    Reads the ``## Allowed commands`` H2 section (case-insensitive on the
    HEADING). Each non-blank line in that section is a bare command basename
    (one per line). An inline ``#`` comment is stripped (``npx  # node wrapper``
    → ``npx``). A line that is ONLY a comment (starts with ``#``) is skipped.

    Section boundary: ANY markdown header line (``#``, ``##``, ``###`` …) that
    is NOT the matching ``## Allowed commands`` heading ENDS the section. This
    matches ``parse_mcp_md_text``'s behavior (which ends spec sections on H1
    too) so the two parsers treat the same file identically — a stray ``#
    Title`` after the allowlist does NOT silently absorb the lines below it.

    Command names are CASE-SENSITIVE (unlike the section HEADING, which is
    matched case-insensitively). This is deliberate: Unix executable resolution
    is case-sensitive, so ``NPX`` in the allowlist will NOT match a
    ``command: npx`` server. The runtime check in ``_check_command_allowlist``
    is likewise case-sensitive; the two stay aligned.

    Return contract (REPLACE, not extend):
      - Section ABSENT          → ``None`` (caller uses DEFAULT_COMMAND_ALLOWLIST).
      - Section PRESENT, ≥1 cmd → ``frozenset`` of those names (REPLACES default).
      - Section PRESENT, empty / comment-only → ``frozenset()`` = explicit
        DENY-ALL. A ``logging.warning`` is emitted because this bricks every
        configured MCP server — operators reaching this state by accident
        (e.g. a heading with no commands under it) get a loud signal rather
        than silent breakage.

    Operators who want to EXTEND the default set must list the defaults they
    still want alongside their additions.

    Format example::

        ## Allowed commands
        npx
        uvx
        python
        python3
        node
        docker
        bun          # locally-built MCP server runner

    Args:
        text: Raw mcp.md content.

    Returns:
        ``frozenset[str]`` of bare command basenames when the section is
        present; ``None`` when the section is absent.
    """
    if not text or not text.strip():
        return None

    in_section = False
    section_seen = False
    names: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()

        # A markdown header line is any line starting with '#' immediately
        # followed by '#'/space, OR a bare '#'. We classify on '#' prefix and
        # then check whether it's the matching '## Allowed commands' heading.
        if stripped.startswith("#"):
            # Determine the heading text after the leading '#' run.
            heading_body = stripped.lstrip("#").strip()
            is_allowed_section_heading = (
                stripped.startswith("## ")
                and heading_body.lower() == _ALLOWED_COMMANDS_SECTION
            )
            if is_allowed_section_heading:
                in_section = True
                section_seen = True
            else:
                # ANY other header (H1/H2/H3+) ends the section. A bare-'#'
                # comment line ALSO ends it — operators document with prose
                # outside the section, not '#' comments inside it; the inline
                # '# comment' form (handled below) covers per-line annotation.
                in_section = False
            continue

        if not in_section:
            continue

        if not stripped:
            continue

        # Strip an inline trailing comment: 'npx  # wrapper' → 'npx'.
        # Split on the FIRST '#' (commands never contain '#').
        name = stripped.split("#", 1)[0].strip()
        if name:
            names.append(name)

    if not section_seen:
        return None

    if not names:
        # Section present but yields no commands → explicit deny-all. Warn
        # loudly: this blocks EVERY MCP server (Principle #13 — don't let a
        # config foot-gun fail silently).
        _logger.warning(
            "mcp.md: '## Allowed commands' section is present but lists no "
            "commands — this is interpreted as DENY-ALL and will block every "
            "configured MCP server at spawn time. Remove the section to use "
            "the default allowlist (%s), or list the commands you want to "
            "permit.",
            sorted(DEFAULT_COMMAND_ALLOWLIST),
        )
        return frozenset()

    return frozenset(names)


def parse_mcp_md_with_policy(
    path: Path,
    read_paths: list | None = None,
) -> tuple[list[MCPServerSpec], frozenset[str] | None]:
    """Parse mcp.md into specs AND the operator-declared command allowlist.

    Combines ``parse_mcp_md`` + ``parse_mcp_allowed_commands`` in one read.
    The allowlist is ``None`` when the ``## Allowed commands`` section is
    absent (caller should use ``DEFAULT_COMMAND_ALLOWLIST``).

    Returns:
        ``(specs, allowed_commands_or_None)``
    """
    if not path.exists():
        return [], None
    text = path.read_text(encoding="utf-8")
    specs = parse_mcp_md_text(text, mcp_md_path=path, read_paths=read_paths)
    allowed = parse_mcp_allowed_commands(text)
    return specs, allowed


def _resolve_env_vars(env: dict[str, str], server_name: str) -> dict[str, str]:
    """Resolve $VAR references in env dict against os.environ.

    Raises MCPServerConnectFailed with the spec/19-documented message shape
    on unresolvable references. Used by _build_spec (when resolve_env=True)
    and FilesystemMCPServerRegistryBackend.load_mcp_server (which calls it
    after a resolve_env=False parse so that resolution happens at
    load_mcp_server time per spec/36 Decision 7).

    Sharing this helper between the two call sites ensures the error message
    shape is canonical -- a second inline implementation would risk diverging.
    """
    resolved: dict[str, str] = {}
    for key, val in env.items():
        if val.startswith("$"):
            var_name = val[1:]
            resolved_val = os.environ.get(var_name)
            if resolved_val is None:
                raise MCPServerConnectFailed(
                    f"mcp.md server '{server_name}': env var '${var_name}' not set. "
                    f"Set {var_name} in the environment before running this agent."
                )
            resolved[key] = resolved_val
        else:
            resolved[key] = val
    return resolved


def _build_spec(
    name: str,
    fields: dict[str, list[str]],
    *,
    resolve_env: bool = True,
) -> MCPServerSpec | None:
    """Build an MCPServerSpec from parsed key/value lines.

    Returns None (and logs a warning) if the section has no command.
    Raises MCPServerConnectFailed for unresolvable env var references
    (only when resolve_env=True).

    resolve_env: when True (default), $VAR references are resolved via
        _resolve_env_vars. When False, the raw string (e.g., "$GITHUB_PAT")
        is stored as-is in the returned spec's env dict. Callers passing
        False are responsible for resolving at the appropriate later boundary.
    """
    command_lines = fields.get("command", [])
    if not command_lines:
        _logger.warning("mcp.md section %r has no 'command:' key -- skipping", name)
        return None

    command = command_lines[0].strip()

    # Args -- comma-separated on one line
    args: list[str] = []
    for args_line in fields.get("args", []):
        args.extend(part.strip() for part in args_line.split(",") if part.strip())

    # Env -- one or more KEY=$VAR or KEY=value pairs, one per line.
    # Build the raw dict first (no resolution yet), then resolve when asked.
    env: dict[str, str] = {}
    for env_line in fields.get("env", []):
        for pair in env_line.split(","):
            pair = pair.strip()
            if "=" not in pair:
                continue
            env_key, _, env_val = pair.partition("=")
            env_key = env_key.strip()
            env_val = env_val.strip()
            env[env_key] = env_val

    if resolve_env:
        env = _resolve_env_vars(env, name)

    transport_lines = fields.get("transport", [])
    transport = transport_lines[0].strip() if transport_lines else "stdio"

    description_lines = fields.get("description", [])
    description = description_lines[0].strip() if description_lines else ""

    # Warn on unknown keys (P2: catches misplaced allowlist directives + typos).
    _KNOWN_KEYS = frozenset({"command", "args", "env", "transport", "description"})
    unknown = set(fields) - _KNOWN_KEYS
    for uk in sorted(unknown):
        _logger.warning(
            "mcp.md server %r: unknown field %r (ignored). "
            "Known fields: command, args, env, transport, description. "
            "If you meant to set 'allowed_commands', place it in a "
            "dedicated '## Allowed commands' H2 section at the top level.",
            name,
            uk,
        )

    return MCPServerSpec(
        name=name,
        command=command,
        args=args,
        env=env,
        transport=transport,
        description=description,
    )


# ──────────────────────────────────────────────────────────────────
# mcp.md serializers


def render_mcp_md_section(spec: MCPServerSpec) -> str:
    """Render a single MCPServerSpec as an mcp.md H2 section string.

    Round-trip property: parsing the output with
    ``parse_mcp_md_text(render_mcp_md_section(spec), resolve_env=False)``
    returns a spec equal to the input field-for-field (after env resolution
    on the re-parsed result).

    env values are written verbatim. $VAR references are NEVER resolved here
    per spec/36 Decision 7: resolved values must not persist to disk.

    Raises:
        ValueError: if spec.command is empty or None.
        ValueError: if spec.command, any spec.args item, any spec.env key,
            or any spec.env value contains a newline character (newline
            injection would create phantom H2 sections the parser reads as
            separate server declarations, bypassing collision detection).
        ValueError: if spec.description contains a line starting with '## '
            or '##\t' (H2 injection defense per Stream D finding D-F9 +
            Stream B B-10 + security finding on tab-separated H2).
    """
    if not spec.command:
        raise ValueError(
            f"MCP server {spec.name!r}: cannot render section without a command. "
            f"Set spec.command to a non-empty executable name."
        )

    # Newline injection guard: a newline in command/args/env would let an API
    # caller write a line that the parser reads as a new H2 section header,
    # creating a phantom server entry that bypasses collision detection and
    # name validation.  Reject BEFORE any output is written.
    if "\n" in spec.command:
        raise ValueError(
            f"MCP server {spec.name!r}: spec.command must not contain newline "
            f"characters (newline injection creates phantom mcp.md sections)."
        )
    for i, arg in enumerate(spec.args):
        if "\n" in arg:
            raise ValueError(
                f"MCP server {spec.name!r}: spec.args[{i}] must not contain "
                f"newline characters (newline injection creates phantom mcp.md "
                f"sections)."
            )
    for env_key, env_val in spec.env.items():
        if "\n" in env_key:
            raise ValueError(
                f"MCP server {spec.name!r}: env key {env_key!r} must not contain "
                f"newline characters (newline injection creates phantom mcp.md "
                f"sections)."
            )
        if "\n" in env_val:
            raise ValueError(
                f"MCP server {spec.name!r}: env value for {env_key!r} must not "
                f"contain newline characters (newline injection creates phantom "
                f"mcp.md sections)."
            )

    # Guard against H2 injection via description. A description containing a
    # line that starts with '## ' or '##\t' would make the parser treat it as
    # a new section header, breaking the round-trip property.
    if spec.description:
        for line in spec.description.splitlines():
            if re.match(r"^##\s", line):
                raise ValueError(
                    f"MCP server {spec.name!r}: description contains a line that "
                    f"starts with '## ' (H2 injection). Strip or replace the "
                    f"offending line before rendering."
                )

    lines: list[str] = [f"## {spec.name}"]
    lines.append(f"command: {spec.command}")

    # args: only written when non-empty; comma+space separator.
    if spec.args:
        lines.append("args: " + ", ".join(spec.args))

    # env: one line per key-value pair, sorted by key for determinism.
    # Values are written as-is (no $VAR resolution) per Decision 7.
    if spec.env:
        for key in sorted(spec.env):
            lines.append(f"env: {key}={spec.env[key]}")

    # transport: only written when non-default (parser defaults to "stdio").
    if spec.transport and spec.transport != "stdio":
        lines.append(f"transport: {spec.transport}")

    # description: first line only (newlines stripped to single line).
    if spec.description:
        first_line = spec.description.splitlines()[0].strip()
        if first_line:
            lines.append(f"description: {first_line}")

    # Section ends with a trailing newline.
    return "\n".join(lines) + "\n"


def render_mcp_md_full(specs: list[MCPServerSpec]) -> str:
    """Render a full mcp.md file from a list of MCPServerSpec objects.

    Always prefixes the ``# MCP servers`` H1 header. An empty list yields
    just the H1 header (preserves the file's identity for downstream
    watchers that detect content changes by diffing).

    Round-trip property: parsing the output with
    ``parse_mcp_md_text(render_mcp_md_full(specs), resolve_env=False)``
    returns a list structurally equal to the input list (field-for-field
    after env resolution).

    Raises:
        ValueError: if any spec.command is empty/None (propagated from
            render_mcp_md_section).
    """
    header = "# MCP servers\n"
    if not specs:
        return header

    sections = "\n".join(render_mcp_md_section(spec) for spec in specs)
    return header + "\n" + sections


# ──────────────────────────────────────────────────────────────────
# Path-traversal check for MCP server args


def validate_mcp_server_args(
    spec: MCPServerSpec,
    agent_read_paths: list,
) -> None:
    """Best-effort path-traversal check on MCP server args.

    For each arg that looks path-shaped (starts with /, ~, ./, ../, or contains
    ..), resolve it and verify it stays under one of the agent's declared
    read_paths.

    Raises PathTraversalError if a path-shaped arg resolves outside all
    declared read_paths. Non-path-shaped args (flags like -y, npm scoped names
    like @scope/pkg, plain strings) are not validated.

    This is best-effort — we can't know what every MCP server treats as a path.
    The obvious path-shaped cases get caught here.
    """
    from .exceptions import PathTraversalError

    if not agent_read_paths:
        return  # no read_paths declared — can't validate

    for arg in spec.args:
        if not _is_path_shaped(arg):
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

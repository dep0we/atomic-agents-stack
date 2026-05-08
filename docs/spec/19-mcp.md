# spec/19 — MCP (Model Context Protocol) Client Support

> Status: **implemented** (PR feat/mcp-support; fixes in PR fix/mcp-review-findings)
> Cross-links: spec/17 (custom tools — MCP composes with this), [MCP official spec](https://spec.modelcontextprotocol.io/)

## Why MCP

The Model Context Protocol (MCP) is the de facto standard for agent-to-tool
integration in 2026. Key indicators:

- **97M monthly SDK downloads** across Python and TypeScript packages
- **9,400+ servers** in the ecosystem covering databases, filesystems, GitHub,
  calendars, web search, and everything in between
- **Native shipping** in every major lab: Claude (Anthropic), ChatGPT (OpenAI),
  Gemini (Google)
- **Linux Foundation governance** via the Agentic AI Foundation — vendor-neutral
  open standard with long-term stability guarantees

Without MCP client support, atomic-agents-stack cannot participate in the
agent-to-tool ecosystem that the industry is converging on. This spec closes
that gap.

MCP tools and custom tools (spec/17) share the same `ToolRegistry`. Operators
can mix operator-coded Python callbacks with MCP server tools in a single agent.

## mcp.md Format

Each agent that uses MCP servers declares them in `<agent>/mcp.md`:

```markdown
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
```

**Sections:** Each `## <name>` block defines one server. `<name>` is the
operator-chosen short name used for tool namespacing.

**Keys:**
- `command` (required): the executable to launch (`npx`, `python`, `/path/to/binary`)
- `args`: comma-separated list of arguments passed to the command
- `env`: `KEY=$VAR_NAME` pairs (one per line or comma-separated). `$VAR_NAME` is
  resolved from the process environment at parse time. An unresolvable reference
  raises `MCPServerConnectFailed` at agent init. The declared vars are **merged
  on top of the parent process environment** — `PATH`, `HOME`, and all other
  inherited vars remain available to the subprocess. `env:` declarations override
  parent values for the same key.
- `transport`: transport protocol. Only `stdio` is supported in v1 (default).
- `description`: human-readable note for operators.

If `mcp.md` does not exist, `config.mcp_servers` is an empty list and no pool
is created — that's fine. Existing agents are unaffected.

## Lifecycle

```
agent.call() called
    ↓
[1] Cost guardrail check FIRST — skip MCP pool init if call will be skipped.
    If check.allow == False → log + return skipped response; no subprocess spawned.
    ↓
[2] [if mcp_servers non-empty AND pool is None]
MCPClientPool.connect_all()
    → for each server: asyncio.run(_async_connect_and_list)
        → stdio_client(params) spawns subprocess (env = {**os.environ, **spec.env})
        → ClientSession.initialize()
        → ClientSession.list_tools()
        → returns list[mcp.types.Tool]
    → failures on individual servers: logged, not fatal
MCPClientPool.discover_tools()
    → namespaces each tool as <server>__<tool>
    → wraps async call_tool in sync handler via asyncio.run()
    → registers ToolDefinition in agent's ToolRegistry (allow_overwrite=True)
    → names tracked in _mcp_registered_names for cleanup
    ↓
LLM call with full tool list (atomic_capture + custom + MCP tools)
Multi-turn tool loop (same path as spec/17 custom tools)
    ↓
finally:
MCPClientPool.disconnect_all()   ← idempotent
agent.mcp_pool = None
for name in _mcp_registered_names:
    tool_registry.unregister(name)  ← prevents stale tools on next call
```

**Per-call subprocess lifecycle (v1):** Each `asyncio.run()` invocation in the
tool handler spawns a fresh server subprocess via `stdio_client`, initializes
a session, calls the tool, and tears down. This is simple and correct. It
means per-tool-call subprocess overhead — acceptable for v1 where MCP calls are
not in a tight loop.

**v2 option (deferred):** A persistent anyio task group running in a background
thread, shared across all tool calls during one `call()`. This would amortize
subprocess startup cost. Not implemented in v1.

## Tool Namespacing

MCP tools are registered in the `ToolRegistry` with qualified names:

```
<server_name>__<original_tool_name>
```

Examples:
- `filesystem-tools__read_file`
- `github__create_issue`
- `time__get_current_time`

The double-underscore separator prevents collisions with custom tools (which
use plain snake_case names) and makes the source server obvious to the LLM.

**Collision detection:** `ToolRegistry.register()` raises `ToolNameCollision` by
default if a name is already registered. MCP registration uses `allow_overwrite=True`
(to handle re-registration on a second `call()` after reconnect) but a custom tool
with the same qualified name as an MCP tool — e.g., a custom tool literally named
`myserver__read_file` — will collide with MCP at registration time and surface loudly.

**Per-call cleanup:** MCP tool names are tracked during each `call()` and
unregistered from `ToolRegistry` in the `finally` block. This prevents tools from
stale or failed server connections accumulating across calls on long-lived agent
instances.

## Cost Cap Inheritance

MCP tool calls flow through the same multi-turn tool loop as custom tools
(spec/17). No additional wiring is required:

```
LLM calls tool → ToolRegistry.execute() → MCP handler → asyncio.run(call_tool)
                                    ↑
                    same cost cap checks, same iteration limits,
                    same ToolCallResult logging
```

The per-call cost cap (daily/monthly) is enforced at the start of each
`while True` iteration, regardless of whether the tool is a custom callback
or an MCP dispatch.

## Security Model

**Operator trust:** The operator declares which MCP servers the agent may
connect to. The framework does not sandbox subprocesses beyond what the OS
provides — the subprocess inherits the agent's UID, environment variables, and
filesystem permissions. Operators vouch for the servers they declare.

**Path-traversal best-effort:** When `_load_config()` calls `parse_mcp_md()`, it
passes the agent's `read_paths` (from `tools.md`). `parse_mcp_md_text` calls
`validate_mcp_server_args()` on every parsed spec before returning, so traversal
detection happens at init time (not just when calling the validator directly).

An arg is considered path-shaped if it:
- Starts with `/` (absolute POSIX path, e.g. `/etc/passwd`)
- Starts with `~` (home-relative, e.g. `~/secrets/key.json`)
- Starts with `./` or `../` (relative to current dir or parent)
- Equals `..` or contains `..` anywhere (traversal attempt)
- Matches `C:\` / `C:/` (absolute Windows path)

Explicitly **not** path-shaped (no validation triggered):
- `@scope/package` — npm scoped names
- `-y`, `--verbose`, `--option=value` — flags
- `bare-string`, `module_name` — plain identifiers

Args that resolve outside all declared `read_paths` raise `PathTraversalError`
with the offending server name and arg. This is best-effort — the framework
cannot know what every MCP server treats as a path, but the obvious cases
are caught at init time.

**Env var resolution:** `$VAR` references in `env:` lines are resolved at parse
time from the process environment. If the variable is unset, `MCPServerConnectFailed`
is raised immediately (fail-loud rather than silently passing an empty value).

**No OAuth in v1:** See deferrals section.

## Exceptions

| Exception | When raised |
|---|---|
| `MCPServerConnectFailed` | Server subprocess fails to start or initialize; also raised at parse time for unresolvable `$VAR` references. Caught and logged per-server by `connect_all()`; agent continues with other servers. |
| `MCPServerNotConfigured` | Code references a server name not in `mcp.md`. |
| `MCPToolDispatchFailed` | Runtime failure during a tool call (server error, network issue, etc.). Caught by `ToolRegistry.execute()`, recorded in `ToolCallResult.error`. |

## v1 Deferrals

The following are not implemented in v1. Each is explicitly deferred:

- **HTTP transport (SSE / streamable-HTTP):** Requires async HTTP client
  session management. Deferred to v2.
- **Resource subscriptions:** MCP resources (read-only data sources) and their
  subscription mechanism are not exposed as tools. Deferred.
- **Prompt templates:** MCP's `prompts/list` and `prompts/get` endpoints are
  not used. Prompt assembly remains via the agent's `persona/` files. Deferred.
- **OAuth flows:** Token negotiation for OAuth-protected MCP servers is not
  implemented. Operators must pre-resolve tokens into `env:` lines. Deferred.

Each of these can be added incrementally without breaking the v1 interface.

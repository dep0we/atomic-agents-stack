# spec/17 — Custom Tools Primitive

> Status: **implemented** (PR feat/custom-tools)
> Cross-links: spec/05 (capture rules), spec/10 (helpers), spec/15 (delegation)

## Overview

Custom tools let operators declare structured, LLM-invokable callbacks that an
agent can call during inference. When the LLM decides to call a tool, the
framework validates the input, executes the callback, and feeds the result back
to the LLM in a follow-up turn. This enables agents to read from databases,
call external APIs, look up calendars, or perform any other side-effect the
operator chooses to expose — without changing the LLM or the agent's system
prompt files.

**Provider-agnostic.** Custom tools work with all three supported providers:
Anthropic, OpenAI, and Moonshot. The registry formats tool definitions per
provider. The underlying plumbing is the `tools=[...]` parameter already
accepted by `_llm.call_llm()`.

## Semantics

```
Declare → Register → LLM calls → Execute → Feed back → Repeat
```

1. **Declare** — operator creates a `ToolDefinition` with a name, description,
   JSON Schema input spec, and handler function.
2. **Register** — operator adds it to a `ToolRegistry` and passes the registry
   to `AtomicAgent(tools=registry)`.
3. **LLM calls** — during inference, the LLM emits a `tool_use` block with the
   tool's name and input.
4. **Execute** — the registry validates input against the schema and calls the
   handler. Any exception is caught and stored in `ToolCallResult.error`.
5. **Feed back** — the result is appended to the message history as a
   `tool_result` block (Anthropic) or `role: tool` message (OpenAI), and the
   LLM runs another turn to incorporate it.
6. **Repeat** — the loop continues until the LLM returns no custom tool_uses
   or the iteration cap is hit.

## JSON Schema Requirements

The `input_schema` must be a JSON Schema `object`:

```python
{
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The SQL query to run."
        },
        "limit": {
            "type": "integer",
            "description": "Maximum rows to return.",
        }
    },
    "required": ["query"]  # may be empty list for fully-optional tools
}
```

The framework validates required fields and declared types at execution time.
It does **not** validate nested schemas, enum values, minLength/maxLength, or
`additionalProperties`. For strict validation, do it inside the handler.

## Multi-Turn Tool Loop

`agent.call()` runs a bounded multi-turn loop:

```
Iteration 1:
  LLM call with tool definitions
  → parse tool_uses
  → execute custom tools (atomic_capture handled separately as before)
  → build follow-up message with tool_result blocks

Iteration 2:
  LLM call with updated message history
  → parse tool_uses
  → ...

Terminates when:
  - LLM returns no custom tool_uses (success)
  - iteration_count >= max_iterations (cap hit → tool_iterations_maxed=True)
  - cost cap hit mid-loop (Response.skipped=True)
```

**Iteration cap:** Default 5, max 20. Set at agent init:
```python
agent = AtomicAgent(name="my-agent", tools=registry, max_tool_iterations=10)
```

Each LLM turn counts against the same daily/monthly cost cap. The pre-check
runs before every iteration after the first. If the cap is hit mid-loop, the
agent returns the latest text with `Response.skipped=True` and
`skip_reason="cost cap hit at iteration N"`.

## Response Extensions

`Response` gains three fields when tools are active:

| Field | Type | Meaning |
|-------|------|---------|
| `tool_calls` | `list[ToolCallResult]` | Every custom tool invoked across all iterations |
| `tool_iterations` | `int` | Number of LLM turns (1 = no tools called) |
| `tool_iterations_maxed` | `bool` | True if loop hit max_iterations cap |

`ToolCallResult` fields:

| Field | Type | Meaning |
|-------|------|---------|
| `tool_name` | `str` | Name of the tool that was called |
| `tool_use_id` | `str` | Provider-assigned call ID |
| `input` | `dict` | Validated input passed to the handler |
| `output` | `Any` | Return value from the handler (JSON-serializable) |
| `error` | `str \| None` | Exception message if handler raised |
| `latency_ms` | `int` | Handler execution time in milliseconds |

## Logging

**Per-tool-call JSONL line:**
```json
{"ts": "...", "trigger": "tool_call", "parent_run_id": "...", "tool_name": "...", "latency_ms": 12, "error": null}
```

**Per-run rollup** — the parent run's log record gains:
```json
{
  "tool_calls": [
    {"tool_name": "query_database", "tool_use_id": "toolu_01abc", "latency_ms": 12, "error": null}
  ],
  "tool_iterations": 2,
  "tool_iterations_maxed": false
}
```

## Public API

```python
from atomic_agents.tools import ToolDefinition, ToolRegistry, ToolCallResult
from atomic_agents import AtomicAgent

# 1. Define
def handle_query(input: dict) -> str:
    return f"SELECT result for: {input['query']}"

# 2. Register
registry = ToolRegistry()
registry.register(ToolDefinition(
    name="query_database",
    description="Run a read-only SQL query.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "SQL query"}},
        "required": ["query"],
    },
    handler=handle_query,
))

# 3. Pass to agent
agent = AtomicAgent(name="my-agent", tools=registry)

# 4. Call — loop runs automatically
response = agent.call("What are the last 5 records?")

# 5. Inspect results
for tc in response.tool_calls:
    print(tc.tool_name, tc.output, tc.error)
print(f"Used {response.tool_iterations} LLM turns")
```

## Comparison Table

| Capability | custom tools | `helper_call` | `delegate` | `atomic_capture` |
|-----------|-------------|--------------|------------|-----------------|
| Who decides to invoke | LLM | calling agent code | calling agent code | LLM |
| Input schema | JSON Schema (validated) | free-form prompt | free-form work_item | fixed capture schema |
| Execution | operator callback | cheap LLM | full AtomicAgent | framework writes atomic note |
| Result fed back to LLM | yes (multi-turn) | no (returned to caller) | no (Response to caller) | no (written to memory/) |
| Cost model | LLM token cost per iteration | LLM token cost | full agent cost | included in parent call |
| State after call | ToolCallResult in Response | HelperResult | Response from target agent | atomic note in memory/ |
| Provider-agnostic | yes | yes | yes | yes |

## Differences from Anthropic's Tools API

Anthropic's [tools API](https://platform.claude.com/docs/en/managed-agents/tools)
is the underlying wire protocol. This primitive wraps it (and the equivalent
OpenAI / Moonshot function-calling API) with:

- **Unified registry** — one `ToolRegistry` works across all providers; the
  framework formats definitions per provider at call time.
- **Synchronous handlers** — operator provides a Python callable; no HTTP
  round-trip or server needed.
- **Bounded loop** — the framework manages the multi-turn tool loop with a
  configurable iteration cap and cost-cap pre-checks. Operators don't need to
  implement the loop themselves.
- **atomic_capture coexistence** — atomic_capture (the framework's built-in
  memory tool) continues to work via the existing capture-write path alongside
  custom tools in the same call.
- **Integrated logging** — per-tool-call JSONL lines + run-log rollup, same
  format as helper_provenance and delegations.

The underlying `tool_use` / `tool_result` blocks are standard Anthropic API
shapes; custom tools don't add any new wire formats.

## Provider-Agnostic Note

`ToolRegistry.to_anthropic_definitions()` produces the Anthropic format:
```json
{"name": "...", "description": "...", "input_schema": {...}}
```

`ToolRegistry.to_openai_definitions()` produces the OpenAI / Moonshot format:
```json
{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
```

`agent._capture_tool_definitions(model)` selects the right formatter based on
model prefix (`claude-` → Anthropic, `gpt-` or `moonshot/` → OpenAI), matching
the existing pattern for `atomic_capture`.

## Exceptions

| Exception | When raised |
|-----------|------------|
| `ToolNotRegistered` | LLM called a tool name not in the registry |
| `ToolInputInvalid` | Input failed schema validation (missing required field or type mismatch) |
| `ToolHandlerError` | Informational only — not raised. Handler exceptions are caught and stored in `ToolCallResult.error`. |
| `ToolIterationsMaxed` | Not raised — set as `Response.tool_iterations_maxed=True`. |

`ToolNotRegistered` and `ToolInputInvalid` propagate up from `registry.execute()`
and will abort the tool loop if an unknown tool is called (which shouldn't
happen if the LLM only calls tools it was given). In practice, the agent logs a
warning and skips unknown tool_uses rather than raising, so the loop continues.

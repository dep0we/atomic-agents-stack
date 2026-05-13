"""Custom tools primitive — operator-defined LLM-invokable tool registry.

This module provides the public API for declaring, registering, and executing
custom tools that agents can call during LLM inference. Tools are
provider-agnostic: the registry formats them for Anthropic, OpenAI, or
Moonshot as needed, matching the approach used by _capture.py for
atomic_capture.

Usage::

    from atomic_agents.tools import ToolDefinition, ToolRegistry
    from atomic_agents import AtomicAgent

    def handle_query(input: dict) -> str:
        return f"Result for: {input['query']}"

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="query_database",
        description="Run a read-only SQL query against the database.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "SQL query to run"}},
            "required": ["query"],
        },
        handler=handle_query,
    ))

    agent = AtomicAgent(name="my-agent", tools=registry)
    response = agent.call("Please look up the last 5 records.")
    print(response.tool_calls)

Differences from atomic_capture (the built-in framework tool):
- atomic_capture is hardcoded in the framework; custom tools are operator-supplied
- atomic_capture writes atomic notes to memory/; custom tools call handler callbacks
- atomic_capture is always available; custom tools require explicit registration
- Both coexist in the same agent.call() — the multi-turn loop handles custom tools,
  atomic_capture is handled by the existing capture-write path

Differences from helper_call:
- helper_call uses a cheap LLM to summarize/extract text; no JSON schema
- custom tools are structured callbacks validated against a JSON schema
- helper_call is caller-driven; custom tools are LLM-driven (model decides when to call)

Differences from delegate:
- delegate spins up a full AtomicAgent with its own persona/memory/logs
- custom tools are synchronous callback functions, no agent lifecycle

Multi-turn loop:
    The agent runs up to max_iterations LLM calls (default 5, max 20).
    Each iteration: LLM call → parse tool_uses → execute custom tools →
    build follow-up message with tool_result blocks → repeat until no
    custom tool_uses or cap hit.

Provider support:
    Anthropic: tool definitions in {"name", "description", "input_schema"} format.
    OpenAI/Moonshot: tool definitions in {"type": "function", "function": {...}} format.
    The registry produces both; agent.py selects the right one based on model prefix.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .exceptions import ToolHandlerError, ToolInputInvalid, ToolNameCollision, ToolNotRegistered

_logger = logging.getLogger(__name__)

# Default and maximum iteration caps for the multi-turn tool loop
DEFAULT_MAX_TOOL_ITERATIONS = 5
MAX_TOOL_ITERATIONS = 20


@dataclass
class ToolDefinition:
    """An LLM-invokable tool the agent can call.

    Attributes:
        name: Tool name as the LLM will call it. Must be unique within a registry.
            Use snake_case (e.g., ``query_database``, ``fetch_calendar``).
        description: What the tool does, when to use it, and what it returns.
            The LLM uses this to decide when to call the tool.
        input_schema: JSON Schema (object type) describing the tool's input.
            Must include ``type: object``, ``properties``, and ``required``
            (even if empty list). Example::

                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The SQL query to run."
                        }
                    },
                    "required": ["query"]
                }

        handler: Callable that receives the parsed input dict and returns a result.
            The result can be any JSON-serializable value. The framework
            json-serializes it before feeding it back to the LLM.
            Runs synchronously in the agent's call thread.
            Exceptions are caught and wrapped in ToolCallResult.error.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]


@dataclass
class ToolCallResult:
    """Outcome of one tool invocation during an agent call.

    Included in Response.tool_calls — one entry per custom tool call
    across all iterations of the multi-turn loop.
    """

    tool_name: str
    tool_use_id: str
    input: dict
    output: Any                    # whatever the handler returned (json-serialized)
    error: str | None = None       # set when handler raised or validation failed
    latency_ms: int = 0


class ToolRegistry:
    """Per-agent registry of custom tools.

    Operators build a registry, register tools, then pass it to AtomicAgent
    via the ``tools=`` constructor parameter. The registry is shared across
    all calls on that agent instance.

    Thread safety: registrations should happen before any concurrent calls.
    execute() is safe to call from multiple threads (reads only).
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition, *, allow_overwrite: bool = False) -> None:
        """Register a tool.

        By default, raises ToolNameCollision if a tool with the same name is
        already registered. Pass ``allow_overwrite=True`` to replace silently.

        MCP registration uses the default (refuse-to-overwrite) so namespace
        collisions — e.g. a custom tool named ``foo__bar`` clashing with an MCP
        server named ``foo`` whose tool is named ``bar`` — surface loudly at
        startup rather than silently winning.

        Raises:
            ToolNameCollision: tool name already in registry and allow_overwrite
                is False.
        """
        if not allow_overwrite and tool.name in self._tools:
            raise ToolNameCollision(
                f"Tool '{tool.name}' is already registered. "
                f"Pass allow_overwrite=True to replace it, or use a unique name."
            )
        self._tools[tool.name] = tool
        _logger.debug("registered tool %r", tool.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool from the registry by name.

        Returns True if the tool was present and removed, False if it was not
        in the registry (idempotent — safe to call even if the tool was never
        registered or was already removed).
        """
        if name in self._tools:
            del self._tools[name]
            _logger.debug("unregistered tool %r", name)
            return True
        return False

    def get(self, name: str) -> ToolDefinition | None:
        """Return the ToolDefinition for ``name``, or None if not registered."""
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        """Return a sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def to_canonical_definitions(self):
        """Format all tools as canonical ``LLMToolDefinition`` instances.

        Used by ``agent.py`` to hand the registry's tools to any backend
        (the backend translates to its provider format inside ``call()``).
        Replaces the per-provider helpers (``to_anthropic_definitions``,
        ``to_openai_definitions``) at agent-runtime call sites; those
        helpers stay around for tests + any external code that pinned to
        them.
        """
        from .llm.types import LLMToolDefinition
        return [
            LLMToolDefinition(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
            )
            for t in self._tools.values()
        ]

    def to_anthropic_definitions(self) -> list[dict]:
        """Format all tools for the Anthropic Messages API.

        Returns a list of dicts shaped::

            {
                "name": "...",
                "description": "...",
                "input_schema": {...}   # JSON Schema object
            }
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def to_openai_definitions(self) -> list[dict]:
        """Format all tools for the OpenAI / Moonshot Chat Completions API.

        Returns a list of dicts shaped::

            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": {...}   # JSON Schema object
                }
            }
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in self._tools.values()
        ]

    def execute(self, tool_use: dict) -> ToolCallResult:
        """Execute a tool_use block returned by the LLM.

        ``tool_use`` is a normalized dict from ``_llm.call_llm()``::

            {"name": "...", "input": {...}, "id": "..."}

        Steps:
        1. Look up tool by name → raises ToolNotRegistered if not found.
        2. Validate input against tool's input_schema (required fields + type
           checks) → raises ToolInputInvalid if validation fails.
        3. Call handler(input) → any exception is caught and returned as
           ToolCallResult.error (handler failure does NOT propagate up).
        4. Return ToolCallResult with output or error.

        Raises:
            ToolNotRegistered: tool name not in registry.
            ToolInputInvalid: input fails schema validation.
        """
        name = tool_use.get("name", "")
        tool_use_id = tool_use.get("id", "")
        input_data = tool_use.get("input", {}) or {}

        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotRegistered(
                f"Tool '{name}' is not registered. "
                f"Registered tools: {self.list_names()}"
            )

        # Validate required fields and basic types
        _validate_input(input_data, tool.input_schema, tool.name)

        start = time.time()
        try:
            output = tool.handler(input_data)
            latency_ms = int((time.time() - start) * 1000)
            return ToolCallResult(
                tool_name=name,
                tool_use_id=tool_use_id,
                input=input_data,
                output=output,
                error=None,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = f"{type(exc).__name__}: {exc}"
            _logger.warning("tool handler %r raised: %s", name, error_msg)
            return ToolCallResult(
                tool_name=name,
                tool_use_id=tool_use_id,
                input=input_data,
                output=None,
                error=error_msg,
                latency_ms=latency_ms,
            )

    def __len__(self) -> int:
        return len(self._tools)

    def __bool__(self) -> bool:
        return bool(self._tools)


# ──────────────────────────────────────────────────────────────────
# Input validation (best-effort: required-fields + type checks)

_JSON_SCHEMA_TYPE_MAP: dict[str, type | tuple] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _validate_input(input_data: dict, schema: dict, tool_name: str) -> None:
    """Best-effort validation: required fields + per-property type checks.

    Raises ToolInputInvalid if:
    - A required field is missing from input_data.
    - A present field's value doesn't match its declared JSON Schema type
      (skips validation for unknown / complex types like $ref or anyOf).

    Does NOT validate: nested schemas, minLength/maxLength, enum values,
    additionalProperties, or other keywords beyond ``type``.
    """
    if not isinstance(schema, dict):
        return  # no schema to validate against

    required = schema.get("required", [])
    for field_name in required:
        if field_name not in input_data:
            raise ToolInputInvalid(
                f"Tool '{tool_name}' input missing required field '{field_name}'. "
                f"Got keys: {list(input_data.keys())}"
            )

    properties = schema.get("properties", {})
    for field_name, field_schema in properties.items():
        if field_name not in input_data:
            continue  # optional field, skip
        if not isinstance(field_schema, dict):
            continue

        declared_type = field_schema.get("type")
        if declared_type is None:
            continue  # no type declared, skip

        # Handle nullable types: {"type": ["string", "null"]}
        if isinstance(declared_type, list):
            expected_types = tuple(
                _JSON_SCHEMA_TYPE_MAP[t]
                for t in declared_type
                if t in _JSON_SCHEMA_TYPE_MAP
            )
            if not expected_types:
                continue
            # Flatten any nested tuples from the map
            flat_types: tuple = ()
            for t in expected_types:
                if isinstance(t, tuple):
                    flat_types = flat_types + t
                else:
                    flat_types = flat_types + (t,)
            expected_types = flat_types
        else:
            expected_type = _JSON_SCHEMA_TYPE_MAP.get(declared_type)
            if expected_type is None:
                continue  # unknown type, skip
            expected_types = expected_type if isinstance(expected_type, tuple) else (expected_type,)

        value = input_data[field_name]
        if not isinstance(value, expected_types):
            raise ToolInputInvalid(
                f"Tool '{tool_name}' field '{field_name}' expected type "
                f"'{declared_type}', got {type(value).__name__!r}."
            )


# ──────────────────────────────────────────────────────────────────
# Helpers for agent.py multi-turn loop

def build_tool_result_blocks_anthropic(results: list[ToolCallResult]) -> list[dict]:
    """Build Anthropic-format tool_result content blocks from a list of results.

    Each block::

        {
            "type": "tool_result",
            "tool_use_id": "...",
            "content": "<json-serialized output or error message>"
        }
    """
    blocks = []
    for result in results:
        if result.error is not None:
            content = f"[tool error] {result.error}"
        else:
            try:
                content = json.dumps(result.output)
            except (TypeError, ValueError):
                content = str(result.output)
        blocks.append({
            "type": "tool_result",
            "tool_use_id": result.tool_use_id,
            "content": content,
        })
    return blocks


def build_tool_result_blocks_openai(
    tool_uses: list[dict],
    results: list[ToolCallResult],
) -> list[dict]:
    """Build OpenAI-format tool result messages from a list of results.

    OpenAI expects one assistant message with tool_calls, then one
    ``role: tool`` message per result.

    Returns a flat list of message dicts to append to the messages list::

        [
            {"role": "tool", "tool_call_id": "...", "content": "..."},
            ...
        ]
    """
    messages = []
    for result in results:
        if result.error is not None:
            content = f"[tool error] {result.error}"
        else:
            try:
                content = json.dumps(result.output)
            except (TypeError, ValueError):
                content = str(result.output)
        messages.append({
            "role": "tool",
            "tool_call_id": result.tool_use_id,
            "content": content,
        })
    return messages

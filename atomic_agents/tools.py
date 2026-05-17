"""Custom tools primitive — operator-defined LLM-invokable tool registry.

This module provides the public API for declaring, registering, and executing
custom tools that agents can call during LLM inference. Tools are
provider-agnostic: the registry formats them for Anthropic, OpenAI, or
Moonshot as needed, matching the approach used by _capture.py for
atomic_capture.

**Relationship to** ``atomic_agents.registry.ToolRegistryBackend`` (spec/25,
#64 PR 1+): ``ToolRegistry`` here is the **dispatch-layer** class — the
in-memory registry the multi-turn loop's ``execute()`` consumes during
``agent.call()``. ``ToolRegistryBackend`` is the **discovery-layer**
Protocol one level above — the catalog (filesystem ``tools/<name>.{md,py}``,
SQLite catalog, future PyPI / git) that produces ``ToolDefinition``
instances which then register into the in-memory ``ToolRegistry``. The
two compose: ``backend.list_tools() → backend.load_tool(name) → tool_registry.register(td)``.
``ToolRegistry`` is NOT going away — spec/25 Decision 1 deliberately
preserves it as the LLM-tool-loop dispatch surface.

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
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    # TargetExtractorRegistry is imported only for type-checking (and for the
    # inline isinstance check in ToolRegistry.register). The actual class is
    # imported lazily inside the method body to avoid a circular import:
    # tools.py → judge/target_extractor_registry.py → exceptions.py is fine,
    # but the string annotation in the method signature stays safe via
    # TYPE_CHECKING + ``from __future__ import annotations``.
    from .judge.target_extractor_registry import TargetExtractorRegistry

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
        classification: Optional per-tool action class for the judge
            layer (spec/28 + #112 PR 2a). Valid values are the
            ``ActionClass`` enum strings: ``"read_only"``,
            ``"reversible_write"``, ``"external_side_effect"``, or
            ``"high_risk"``. Stored as a string to keep ``tools.py``
            independent of ``atomic_agents/judge`` (the judge module
            does the typed conversion at dispatch time). When ``None``
            (default), the framework consults ``tools.md``'s
            ``## Tool classification`` section; if no entry there
            either, the proposal-assembly path defaults to
            ``external_side_effect`` per spec/28's safe default.
        target_extractor_id: Optional string ID naming a per-agent
            target extractor registered on the agent's
            ``TargetExtractorRegistry`` (spec/29 §"Target extraction",
            #124 PR 3a). The framework invokes the named callable at
            proposal-assembly time against the tool's ``tool_arguments``
            to populate ``ActionProposal.target_canonical``, which
            ``MandateCheck`` step 5 reads to enforce
            ``constraints.allowed_targets`` / ``blocked_targets``.

            Stored as a string (not a ``Callable``) so this field can
            round-trip through spec/25 Tier B structured-storage
            backends (SQLite catalog, future PyPI / git / database
            catalogs) that cannot store ``Callable`` values per spec/25
            MUST #4. The named extractor is registered on the per-agent
            ``TargetExtractorRegistry``; the string ID is the durable
            cross-layer contract. Validated at ``ToolRegistry.register()``
            time against the agent's registry (loud early failure per
            spec/29 §"Registration order discipline").

            When ``None`` (default), the framework applies built-in
            heuristic extractors (``recipient_to``, ``recipient_field``,
            etc.) in priority order at proposal-assembly time. Built-in
            heuristics are sufficient for tools whose argument shape uses
            conventional field names; custom extractors are for tools
            with non-standard shapes.

            NOTE: Validation against the agent's ``TargetExtractorRegistry``
            requires the registry reference to be passed to
            ``ToolRegistry.register()`` via the ``target_extractor_registry``
            kwarg. When the kwarg is absent (e.g., programmatic callers
            that don't wire the mandate layer), the validation is skipped
            and the field is accepted as-is. The agent's ``__init__``
            always passes the registry kwarg (spec/29 §"Registration
            order discipline"). See spec/25 — spec/25 will be amended in
            PR 5 to document this field alongside the other ToolDefinition
            descriptor fields.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    classification: str | None = None
    target_extractor_id: str | None = None


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
    # Judge layer (spec/28 PR 3b). True when the tool_use was deferred
    # by an ESCALATE judgment — handler did NOT run; the framework
    # wrote a PENDING file and ``Response.deferred=True``. Consumers
    # iterating ``response.tool_calls`` distinguish this from genuine
    # handler errors via the field, not by string-matching ``error``.
    deferred: bool = False


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

    def register(
        self,
        tool: ToolDefinition,
        *,
        allow_overwrite: bool = False,
        target_extractor_registry: "TargetExtractorRegistry | None" = None,
    ) -> None:
        """Register a tool.

        By default, raises ToolNameCollision if a tool with the same name is
        already registered. Pass ``allow_overwrite=True`` to replace silently.

        MCP registration uses the default (refuse-to-overwrite) so namespace
        collisions — e.g. a custom tool named ``foo__bar`` clashing with an MCP
        server named ``foo`` whose tool is named ``bar`` — surface loudly at
        startup rather than silently winning.

        When ``target_extractor_registry`` is passed (i.e., the mandate layer
        is wired — the agent's ``__init__`` always passes it per spec/29
        §"Registration order discipline"), validates
        ``tool.target_extractor_id`` against the registry at registration time.
        Unknown IDs raise ``UnknownTargetExtractor`` immediately — loud early
        failure surfaces misconfiguration BEFORE first mandate-citing dispatch.
        When the kwarg is absent (programmatic callers that don't wire the
        mandate layer), the validation is skipped.

        Args:
            tool: The ``ToolDefinition`` to register.
            allow_overwrite: When ``True``, silently replaces an existing tool
                with the same name. Default ``False``.
            target_extractor_registry: Optional per-agent
                ``TargetExtractorRegistry`` used to validate
                ``tool.target_extractor_id`` at registration time per spec/29
                §"Registration order discipline". When ``None``, validation is
                skipped (backward-compatible for callers that don't wire the
                mandate layer).

        Raises:
            ToolNameCollision: tool name already in registry and allow_overwrite
                is False.
            ValueError: tool has an invalid ``classification`` value.
            UnknownTargetExtractor: ``tool.target_extractor_id`` is set and
                not registered in ``target_extractor_registry``.
        """
        if not allow_overwrite and tool.name in self._tools:
            raise ToolNameCollision(
                f"Tool '{tool.name}' is already registered. "
                f"Pass allow_overwrite=True to replace it, or use a unique name."
            )
        # Fail-fast classification validation (#112 PR 2a). Silent
        # default-mapping at dispatch time was operator-hostile per the
        # round-2 review — registering a tool with an invalid class is
        # always a typo / version mismatch, not a runtime concern.
        if tool.classification is not None and tool.classification not in {
            "read_only",
            "reversible_write",
            "external_side_effect",
            "high_risk",
        }:
            raise ValueError(
                f"Tool {tool.name!r} has invalid classification "
                f"{tool.classification!r}. Must be one of: "
                f"read_only, reversible_write, external_side_effect, high_risk."
            )
        # Fail-fast target_extractor_id validation (spec/29 §"Registration
        # order discipline", #124 PR 3a). Built-in heuristic extractors are
        # pre-registered BEFORE tool_registry loading in AtomicAgent.__init__,
        # so an operator-configured target_extractor_id that references a
        # missing extractor surfaces HERE (at register time) instead of
        # silently fail-closing at MandateCheck evaluation time (plan-subagent
        # Risk A). Validation is skipped when target_extractor_registry is
        # absent for backward compatibility with programmatic callers that
        # don't wire the mandate layer.
        if (
            tool.target_extractor_id is not None
            and target_extractor_registry is not None
            and not target_extractor_registry.has(tool.target_extractor_id)
        ):
            from .judge.target_extractor_registry import UnknownTargetExtractor
            raise UnknownTargetExtractor(
                f"Tool {tool.name!r} declares target_extractor_id "
                f"{tool.target_extractor_id!r} which is not registered in the "
                f"agent's TargetExtractorRegistry. Register it via "
                f"agent.register_target_extractor({tool.target_extractor_id!r}, fn) "
                f"BEFORE registering this tool, or use one of the built-in "
                f"extractors: {target_extractor_registry.list_names()}"
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
# Both provider-format tool-result builders were removed across #87:
# - build_tool_result_blocks_anthropic deleted in PR 2.5 — Anthropic
#   tool-loop continuation flows through AnthropicLLMBackend.format_tool_results
#   from canonical types.
# - build_tool_result_blocks_openai deleted in PR 3 — OpenAI / Moonshot
#   tool-loop continuation flows through OpenAICompatibleLLMBackend.format_tool_results.
# Both backends own provider translation entirely; the agent layer no
# longer constructs provider-shaped messages.

"""Canonical request/response types for the LLMBackend Protocol (spec/31).

The framework's runtime — `agent.call()`, the tool-loop, the cost gates —
talks to LLM providers only through these canonical types. Each backend
translates between its provider's wire format and the canonical types at
its call boundary. The agent layer never sees Anthropic content blocks or
OpenAI tool_calls — it sees `LLMToolUse` / `LLMToolResult` regardless of
which backend served the call.

This is the codex P1 fix from the LLMBackend plan: provider coupling
currently lives in `agent.py` tool dispatch (`agent.py:273-294, :953`) and
`_llm.py:60-80`. Without canonical types, third-party backends could
register for generation but lose tool loops, multi-turn dispatch, and
audit-trail consistency. Canonical types make the Protocol contract
tractable for alternate implementations to satisfy.

All types are `@dataclass(frozen=True)` so they are immutable and
comparable by value — safe to pass across the agent / backend /
observability boundary without defensive copying. Note that the types
carrying `dict` fields (`LLMToolDefinition.input_schema`,
`LLMToolUse.input`) are NOT hashable, by design: JSON Schema is
naturally nested-mutable. Consumers that need a set/dict key should
derive one (e.g., from `LLMToolDefinition.name`).

Scaffolding only — no behavior change in this PR. Reference backends
(`AnthropicLLMBackend`, `OpenAICompatibleLLMBackend`, `MoonshotLLMBackend`)
land in the follow-up PR; `agent.py`'s tool dispatch is refactored to use
these types in the same PR so the canonical types and their consumers
ship together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class LLMToolDefinition:
    """Canonical outbound tool definition — backends translate to provider format.

    The framework's `ToolRegistry` (spec/17) already produces both
    Anthropic and OpenAI shapes via `to_anthropic_definitions()` /
    `to_openai_definitions()`. Once backends land, the registry will
    produce `LLMToolDefinition` once and each backend translates to its
    provider format inside `SyncLLMBackend.call()`.

    Fields:
        name: tool name as the model sees it (must match a registered tool).
        description: short description shown to the model in the system
            prompt's tool catalog.
        input_schema: JSON Schema describing the tool's input object. Used
            by both Anthropic (passed directly as `input_schema`) and
            OpenAI (wrapped in `{type: function, function: {parameters: ...}}`).
        strict: OpenAI structured-output mode. When True, the OpenAI
            backend opts the tool into the model's structured-output
            constraint. Backends that don't support strict mode ignore.

    TODO (#87 PR 2): Anthropic's canonical cache-the-tools-block pattern
    sets ``cache_control`` on the last tool definition in the list. The
    canonical type may need a ``cache_breakpoint`` field (mirroring
    `CacheDirective.breakpoint_id`) so the AnthropicLLMBackend can
    translate the operator's caching intent without losing it at the
    Protocol boundary. Defer until PR 2 demonstrates the need
    empirically; today's existing dispatch in `agent.py` doesn't
    surface per-tool caching either, so PR 1 stays parity-with-today.
    """

    name: str
    description: str
    input_schema: dict
    strict: bool = False


@dataclass(frozen=True)
class LLMToolUse:
    """Canonical inbound tool_use block — what the model wants to call.

    Backends translate provider tool_use blocks into this shape inside
    `SyncLLMBackend.call()` and return them via `_RawLLMResponse.tool_uses`.
    `agent.py`'s tool-loop iterates these regardless of provider.

    Fields:
        id: provider-issued tool-use id (Anthropic `tool_use.id`,
            OpenAI `tool_calls[i].id`). Required for the follow-up
            tool_result message to refer back to this call.
        name: tool name (matches a registered tool's `LLMToolDefinition.name`).
        input: parsed input dict. OpenAI returns this as a JSON string
            on the wire; backends MUST parse it before constructing this
            type so consumers see consistent dict shape.
    """

    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class LLMToolResult:
    """Canonical tool result the agent passes back to the model on the next call.

    `agent.py`'s tool-loop produces this after each tool execution
    (replacing today's provider-shaped dict at `agent.py:953` and below).
    Backends translate it back to their provider's tool-result message
    format inside `SyncLLMBackend.format_tool_results()`.

    Fields:
        tool_use_id: must match the `LLMToolUse.id` from the call that
            produced this result. Anthropic and OpenAI both require this
            to correlate the result with the originating tool call.
        content: the tool's textual output. Backends accept dict (treated
            as structured output) or str (treated as freeform text).
        is_error: True when the tool execution failed. Different providers
            surface tool errors differently (Anthropic has `is_error: true`
            on tool_result; OpenAI uses the content shape to signal); the
            backend translates this canonical flag to its native form.
    """

    tool_use_id: str
    content: str | dict
    is_error: bool = False


@dataclass(frozen=True)
class CacheDirective:
    """Multi-breakpoint cache intent — preserves spec/04's layered cache model.

    Spec/04 documents a multi-layer cache strategy: persona stays cached
    longest, tools/skills cache mid-life, memory index caches shortest.
    A flat `cache_breakpoints: bool` would flatten this intent. Backends
    that don't support cache_control (OpenAI today) simply ignore the
    directives.

    Fields:
        breakpoint_id: a stable identifier the operator/spec uses to refer
            to this cache layer (e.g., "system-persona", "system-tools",
            "memory-index"). Backends that materialize multiple cache
            blocks map directives onto blocks by id.
        ttl: cache lifetime. Anthropic exposes `ephemeral` (5-minute) and
            `1h` (long-cache). Backends that lack a particular TTL fall
            back to their default cache lifetime.
    """

    breakpoint_id: str
    ttl: Literal["ephemeral", "1h"] = "ephemeral"


@dataclass(frozen=True)
class LLMCapabilities:
    """Per-MODEL capability declaration — codex P2 fix.

    Capabilities are per-model, not per-backend. The same backend
    (`OpenAICompatibleLLMBackend`) can serve a tool-capable model (gpt-5)
    and a vision-capable model (gpt-5-vision); a flat
    `supported_capabilities() -> set[str]` cannot honestly describe both.

    Callers consult this BEFORE making a call to know whether to send
    tools, cache_control directives, or image content. A False claim
    silently disables the feature path; a True claim means the backend
    will accept and honor it.

    Fields:
        tools: model accepts tool definitions and may emit tool_use.
        tool_results: model accepts tool_result follow-up messages
            (i.e., supports multi-turn tool loops).
        cache_control: model accepts CacheDirective list and applies
            per-breakpoint cache semantics.
        streaming: model supports incremental output (reserved; v1
            ships SyncLLMBackend only).
        vision: model accepts image content blocks in user messages.
        max_input_tokens: model's input context window in tokens.
        max_output_tokens: model's output generation cap in tokens.
        usage_reporting: backend can return input/output token counts
            from a non-streaming call. When False, callers must
            fall back to `count_tokens()` estimates for cost gates.
        structured_output: model supports server-enforced structured
            output (OpenAI structured-output / Anthropic JSON mode).
    """

    tools: bool
    tool_results: bool
    cache_control: bool
    streaming: bool
    vision: bool
    max_input_tokens: int
    max_output_tokens: int
    usage_reporting: bool
    structured_output: bool


@dataclass(frozen=True)
class PricingInfo:
    """Per-model pricing the backend optionally declares.

    `_costs.PRICING` is the framework-wide fallback (`_costs.py:20`); a
    backend that knows its own pricing (because it manages a private API,
    a third-party endpoint, or wants per-tenant pricing) returns it via
    `SyncLLMBackend.pricing(model_id)`. The caller (`_costs.calc_cost`
    in PR 4) prefers backend-provided pricing and falls back to the
    framework table when the backend returns None.

    Fields:
        input_per_million_usd: USD per 1M input tokens.
        output_per_million_usd: USD per 1M output tokens.
        cache_hit_discount: fraction of `input_per_million_usd` charged
            for cache-hit tokens (Anthropic default 0.10 = 10%).
            Backends without cache support set 1.0 (no discount) or
            return None entirely.
    """

    input_per_million_usd: float
    output_per_million_usd: float
    cache_hit_discount: float = 0.10

"""AnthropicLLMBackend — reference implementation of SyncLLMBackend for Claude.

Wraps the anthropic SDK with canonical-type translation. The agent layer
hands this backend ``LLMToolDefinition`` lists + ``LLMToolResult`` lists;
the backend translates to Anthropic's tools schema + tool_result content
blocks at its boundary, calls the SDK, and translates Anthropic tool_use
blocks back to ``LLMToolUse`` instances in the returned response.

The procedural ``_call_anthropic`` in ``atomic_agents/_llm.py`` predates
this backend and still implements the same provider semantics. PR 2 of
issue #87 routes Anthropic models through this backend while
``_call_openai`` / ``_call_moonshot`` remain procedural until PR 3
introduces ``OpenAICompatibleLLMBackend``.

Hard dependency: ``anthropic>=0.40`` (already in pyproject). If the SDK
is missing, instantiation raises ``AtomicAgentsError`` at construction
time so registration in ``llm/__init__.py`` can guard with a
try/except.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .._costs import PRICING
from ..exceptions import AtomicAgentsError
from .backend import _RawLLMResponse
from .types import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)

_logger = logging.getLogger(__name__)


# Capability table — per-model. Claude family is generally feature-rich;
# vision varies by model. Keep this honest: a False here disables a code
# path, a True here means the backend MUST accept that input.
_CLAUDE_CAPABILITIES: dict[str, dict] = {
    "claude-opus-4-7": {
        "vision": True, "max_input_tokens": 200_000, "max_output_tokens": 32_000,
    },
    "claude-sonnet-4-6": {
        "vision": True, "max_input_tokens": 200_000, "max_output_tokens": 64_000,
    },
    "claude-haiku-4-5": {
        "vision": True, "max_input_tokens": 200_000, "max_output_tokens": 8_192,
    },
}


def _resolve_claude_family(model_id: str) -> str | None:
    """Strip a date suffix off a Claude model id; return the family key or None.

    'claude-opus-4-7-20260101' → 'claude-opus-4-7'
    'claude-sonnet-4-6' → 'claude-sonnet-4-6'
    'unknown-model' → None

    The family key is what the capability + pricing tables index by.
    """
    for family in _CLAUDE_CAPABILITIES:
        if model_id == family or model_id.startswith(family + "-"):
            return family
    return None


class AnthropicLLMBackend:
    """SyncLLMBackend impl for Anthropic Claude models.

    Stateless aside from the lazily-imported SDK client cached on the
    instance. Safe to register once at import time and reuse across
    threads (the anthropic SDK's ``Anthropic`` client is thread-safe).
    """

    def __init__(self) -> None:
        try:
            import anthropic  # noqa: F401 — presence check at construction
        except ImportError as e:
            raise AtomicAgentsError(
                "anthropic SDK not installed; pip install anthropic"
            ) from e
        # No per-instance client cache: tests patch ``sys.modules["anthropic"]``
        # between cases; caching a client would freeze the pre-patch SDK on
        # the backend instance and leak across tests. anthropic.Anthropic()
        # construction is fast (no network call) so per-call build is cheap.

    # ────────────────────────────────────────────────────────────
    # SyncLLMBackend Protocol surface

    @property
    def provider_id(self) -> str:
        return "anthropic"

    def supports_model(self, model_id: str) -> bool:
        """Match every ``claude-*`` model id.

        Forward-compatible by design: any model whose id starts with
        ``claude-`` routes through this backend, even when the specific
        family isn't yet in ``_CLAUDE_CAPABILITIES``. ``capabilities()``
        falls back to conservative defaults for the unrecognized case
        (see ``_resolve_claude_family is None`` branch there). The
        rationale: when Anthropic ships ``claude-opus-4-8`` next month,
        an operator who pins to it shouldn't get a hard crash from this
        framework — the SDK still accepts it, the framework just emits
        slightly less precise capability metadata until we update the
        table. The pre-#87 procedural ``_call_anthropic`` honored this
        property by accepting any ``claude-*`` prefix; PR 2 preserves it.
        """
        return model_id.startswith("claude-")

    def capabilities(self, model_id: str) -> LLMCapabilities:
        family = _resolve_claude_family(model_id)
        if family is None:
            # Conservative defaults — caller may have racy state where
            # supports_model returned True then capabilities is called
            # with a different model. Don't crash; return cautious values.
            return LLMCapabilities(
                tools=True, tool_results=True, cache_control=True,
                streaming=False, vision=False,
                max_input_tokens=200_000, max_output_tokens=4_096,
                usage_reporting=True, structured_output=False,
            )
        cfg = _CLAUDE_CAPABILITIES[family]
        return LLMCapabilities(
            tools=True,
            tool_results=True,
            cache_control=True,
            streaming=False,  # v1 ships SyncLLMBackend only
            vision=cfg["vision"],
            max_input_tokens=cfg["max_input_tokens"],
            max_output_tokens=cfg["max_output_tokens"],
            usage_reporting=True,
            structured_output=False,
        )

    def pricing(self, model_id: str) -> PricingInfo | None:
        """Return per-model pricing from `_costs.PRICING`.

        `_costs.PRICING` is the framework's single source of truth for
        rates. This backend delegates rather than maintaining a parallel
        table — avoids drift. Returns None when the model id isn't priced,
        letting the caller fall back to ``_fallback_pricing()``.
        """
        if model_id not in PRICING:
            return None
        rates = PRICING[model_id]
        return PricingInfo(
            input_per_million_usd=rates["input"],
            output_per_million_usd=rates["output"],
            cache_hit_discount=0.10,
        )

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[LLMToolDefinition] | None = None,
    ) -> int:
        """Estimate input tokens before the call — for cost guardrails.

        Uses the anthropic SDK's ``count_tokens`` when available
        (anthropic>=0.40 exposes ``client.messages.count_tokens``). On
        older SDKs without that method, falls back to a 4-chars-per-token
        heuristic that over-counts by ~20%, preserving the conservative-
        pessimistic discipline of cost guardrails (CLAUDE.md rule #4).
        """
        client = self._build_client()
        # Try the SDK's exact counter first
        try:
            anthropic_tools = [self._translate_tool_def(t) for t in (tools or [])]
            kwargs: dict[str, Any] = {
                "model": "claude-sonnet-4-6",  # any model; counter is model-aware
                "system": system_prompt,
                "messages": messages,
            }
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools
            result = client.messages.count_tokens(**kwargs)
            return int(getattr(result, "input_tokens", 0))
        except AttributeError as e:
            # SDK older than the version that ships count_tokens — heuristic
            # fallback is the documented behavior for that case. Any other
            # exception (network, auth, malformed schema) propagates so the
            # caller sees the real failure instead of a silently-wrong
            # token estimate that mis-arms the cost guardrail.
            _logger.debug(
                "anthropic count_tokens not in SDK (%s); using heuristic",
                e,
            )
            # Heuristic — over-estimate by counting characters
            char_count = len(system_prompt)
            for m in messages:
                content = m.get("content", "")
                if isinstance(content, str):
                    char_count += len(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            char_count += len(json.dumps(block))
            return max(1, char_count // 4)

    def call(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        tools: list[LLMToolDefinition] | None = None,
        cache_directives: list[CacheDirective] | None = None,
    ) -> _RawLLMResponse:
        """Synchronous call to Anthropic Messages API.

        Translates canonical tools → Anthropic tools schema; calls the
        SDK; translates Anthropic content blocks → text + LLMToolUse list.
        Preserves the exact provider semantics of ``_call_anthropic`` so
        existing behavior tests continue to pass after the routing
        change in ``_llm.call_llm``.
        """
        from ..exceptions import AtomicAgentsError as _AAE  # local import for re-raise
        client = self._build_client()

        # System prompt block(s) with cache_control when any directive is
        # passed. v1 maps the directive list onto a single cache breakpoint
        # at the end of the system prompt — matches the current
        # ``_call_anthropic`` behavior (preserves existing cache hit rates
        # on long persona prompts). Future Protocol extension can map
        # multiple directives to multiple blocks.
        if cache_directives:
            system_blocks: list[dict[str, Any]] = [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_blocks = [{"type": "text", "text": system_prompt}]

        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            create_kwargs["tools"] = [self._translate_tool_def(t) for t in tools]

        response = client.messages.create(**create_kwargs)

        # Extract text + tool_use blocks — matches _call_anthropic's
        # extraction exactly so the upgrade is behaviorally invisible.
        text_parts: list[str] = []
        tool_uses_dicts: list[dict] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use":
                tool_uses_dicts.append({
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                })
            elif block_type == "text" or hasattr(block, "text"):
                text_parts.append(getattr(block, "text", ""))
        text = "".join(text_parts)

        usage = response.usage
        cache_hit = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        input_tokens = usage.input_tokens + cache_hit + cache_creation
        cache_miss = input_tokens - cache_hit

        return _RawLLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
            raw=response.model_dump() if hasattr(response, "model_dump") else None,
            tool_uses=tool_uses_dicts,
        )

    def format_tool_results(
        self,
        tool_uses: list[LLMToolUse],
        tool_results: list[LLMToolResult],
        assistant_text: str = "",
    ) -> list[dict]:
        """Build the next-iteration message list for Anthropic's tool loop.

        Anthropic requires the prior assistant turn to be echoed back
        with its tool_use blocks, followed by a user turn containing
        tool_result blocks. Returns the two messages ready to append to
        the running messages list.

        Empty ``tool_results`` → empty list (no follow-up needed; the
        loop is done).
        """
        if not tool_results:
            return []

        out: list[dict] = []

        # Assistant turn echoing the model's prior tool_use blocks. Only
        # non-atomic_capture tools go in the echo — atomic_capture is
        # handled by the capture path, not by the tool loop.
        assistant_content: list[dict] = []
        if assistant_text:
            assistant_content.append({"type": "text", "text": assistant_text})
        for tu in tool_uses:
            if tu.name == "atomic_capture":
                continue
            assistant_content.append({
                "type": "tool_use",
                "id": tu.id,
                "name": tu.name,
                "input": tu.input,
            })
        if assistant_content:
            out.append({"role": "assistant", "content": assistant_content})

        # User turn with tool_result blocks. Serialization rules preserve
        # pre-#87 wire bytes exactly so operator eval harnesses comparing
        # JSONL transcripts before/after the migration see no diff:
        #
        # - Error strings: passed through (already prefixed "[tool error]").
        # - Everything else (str, dict, list, dataclass, datetime, ...):
        #   json.dumps the content. JSON strings get quoted (matches the
        #   pre-#87 `tools.build_tool_result_blocks_anthropic` behavior),
        #   dicts get encoded, and anything not JSON-serializable falls
        #   back to ``str()`` so a single bad tool return doesn't crash
        #   the loop.
        result_blocks: list[dict] = []
        for r in tool_results:
            if r.is_error and isinstance(r.content, str):
                content = r.content  # error message already a string
            else:
                try:
                    content = json.dumps(r.content)
                except (TypeError, ValueError):
                    content = str(r.content)
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": r.tool_use_id,
                "content": content,
            }
            if r.is_error:
                block["is_error"] = True
            result_blocks.append(block)
        if result_blocks:
            out.append({"role": "user", "content": result_blocks})

        return out

    # ────────────────────────────────────────────────────────────
    # Private helpers

    def _build_client(self):
        """Build a fresh anthropic client per call.

        Per-call construction sidesteps test-isolation issues where
        ``patch.dict(sys.modules, {"anthropic": fake})`` is active for a
        test that runs AFTER the first call to this backend in the process
        — a cached client would freeze the pre-patch SDK reference. The
        anthropic.Anthropic constructor is fast (no network call), so the
        overhead is negligible.
        """
        from .._llm import _get_anthropic_key
        import anthropic
        return anthropic.Anthropic(api_key=_get_anthropic_key())

    @staticmethod
    def _translate_tool_def(td: LLMToolDefinition) -> dict:
        """Canonical LLMToolDefinition → Anthropic tools schema dict.

        Anthropic shape::

            {"name": "...", "description": "...", "input_schema": {...}}

        ``strict`` is OpenAI-only — silently dropped here.
        """
        return {
            "name": td.name,
            "description": td.description,
            "input_schema": td.input_schema,
        }

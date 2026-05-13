"""OpenAICompatibleLLMBackend — configurable backend for OpenAI-compatible endpoints.

Single class handles OpenAI direct, Azure OpenAI (when wired), Moonshot,
Together, vLLM-local, and any other endpoint that conforms to OpenAI's
``chat.completions.create`` contract. Configuration dimensions per
codex P3 in the #87 plan:

- ``provider_id``: stable identifier (``"openai"``, ``"moonshot"``, ...)
- ``key_spec``: where to find the API key (env vars, Keychain, config file)
- ``model_namespace``: predicate deciding which model ids this backend handles
- ``model_transform``: optional rewrite before sending to the SDK (Moonshot
  strips its ``moonshot/`` prefix)
- ``base_url``: HTTP endpoint (None → openai SDK's default)
- ``capability_hooks``: per-model ``LLMCapabilities`` overrides for
  endpoints that lack tool calls, vision, or usage reporting

Reference subclasses (``MoonshotLLMBackend`` in ``llm/moonshot.py``) bake
in the right config for specific providers as a readability win.

Wire-byte parity with the pre-#87 ``_call_openai`` / ``_call_moonshot``
procedural paths is preserved — agent.py JSONL logs and operator eval
harnesses see no diff across the migration.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

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


@dataclass(frozen=True)
class KeySpec:
    """Where to find a provider's API key.

    Three sources tried in order: env vars (first non-empty wins),
    macOS Keychain (``security find-generic-password -s <name>``),
    then ``~/.config/atomic_agents/keys.json`` under ``config_file_key``.
    Matches the framework's spec/01 secrets-handling convention.
    """

    env_vars: tuple[str, ...]
    keychain_name: str
    config_file_key: str


# Default capability shape for OpenAI-family chat-completion models. Real
# capability tables override per-model via ``capability_hooks``.
_DEFAULT_OPENAI_CAPABILITIES = LLMCapabilities(
    tools=True,
    tool_results=True,
    cache_control=False,  # OpenAI doesn't expose ephemeral cache today
    streaming=False,
    vision=False,
    max_input_tokens=128_000,
    max_output_tokens=4_096,
    usage_reporting=True,
    structured_output=True,
)


class OpenAICompatibleLLMBackend:
    """SyncLLMBackend impl for any OpenAI-compatible chat-completion endpoint.

    Stateless aside from the per-call SDK client. Safe to register once at
    import time and reuse across threads.
    """

    def __init__(
        self,
        provider_id: str,
        key_spec: KeySpec,
        model_namespace: Callable[[str], bool],
        model_transform: Callable[[str], str] | None = None,
        base_url: str | None = None,
        capability_hooks: dict[str, LLMCapabilities] | None = None,
    ) -> None:
        try:
            import openai  # noqa: F401 — presence check at construction
        except ImportError as e:
            raise AtomicAgentsError(
                f"openai SDK not installed; required for {provider_id!r} backend. "
                f"pip install openai (or install with [openai] extra)"
            ) from e
        self._provider_id = provider_id
        self._key_spec = key_spec
        self._model_namespace = model_namespace
        self._model_transform = model_transform or (lambda m: m)
        self._base_url = base_url
        self._capability_hooks = capability_hooks or {}

    # ────────────────────────────────────────────────────────────
    # SyncLLMBackend Protocol surface

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def supports_model(self, model_id: str) -> bool:
        return bool(self._model_namespace(model_id))

    def capabilities(self, model_id: str) -> LLMCapabilities:
        """Per-model capabilities; falls back to OpenAI-family defaults."""
        return self._capability_hooks.get(model_id, _DEFAULT_OPENAI_CAPABILITIES)

    def pricing(self, model_id: str) -> PricingInfo | None:
        """Return per-model pricing from `_costs.PRICING`.

        Both ``model_id`` and the transformed model id are tried — operators
        configure pricing entries under whatever string they write in
        ``model.md`` (e.g., ``moonshot/kimi-k2.6`` retains the prefix in
        PRICING but the SDK call uses the stripped form).
        """
        if model_id in PRICING:
            rates = PRICING[model_id]
        else:
            transformed = self._model_transform(model_id)
            if transformed not in PRICING:
                return None
            rates = PRICING[transformed]
        return PricingInfo(
            input_per_million_usd=rates["input"],
            output_per_million_usd=rates["output"],
            cache_hit_discount=1.0,  # OpenAI-family has no cache discount today
        )

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[LLMToolDefinition] | None = None,
    ) -> int:
        """Estimate input tokens via tiktoken if installed; heuristic fallback.

        OpenAI doesn't expose a pre-flight token-count API (Anthropic does),
        so the canonical accurate path is tiktoken — when missing, fall
        back to 4-chars-per-token over-estimate. Cost guardrails stay
        conservative-pessimistic per CLAUDE.md rule #4.
        """
        try:
            import tiktoken
        except ImportError:
            return self._char_heuristic_tokens(system_prompt, messages, tools)
        try:
            enc = tiktoken.encoding_for_model("gpt-4o-mini")
        except (KeyError, Exception):
            enc = tiktoken.get_encoding("cl100k_base")
        total = len(enc.encode(system_prompt))
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(enc.encode(content))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(enc.encode(json.dumps(block)))
        for t in (tools or []):
            total += len(enc.encode(f"{t.name} {t.description} {json.dumps(t.input_schema)}"))
        # +6 fixed overhead for the role-tag / boundary tokens per message
        # (rough — OpenAI doesn't publish this for chat completions, the
        # number varies by model. Over-count is the safer direction.)
        return total + 6 * (len(messages) + 1)

    @staticmethod
    def _char_heuristic_tokens(system_prompt, messages, tools) -> int:
        char_count = len(system_prompt)
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                char_count += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        char_count += len(json.dumps(block))
        for t in (tools or []):
            char_count += len(t.name) + len(t.description) + len(json.dumps(t.input_schema))
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
        """Synchronous call to an OpenAI-compatible endpoint.

        Translates canonical tools → OpenAI's ``{type: function, function:
        {...}}`` schema; calls the SDK's ``chat.completions.create``;
        translates OpenAI's tool_calls back to ``LLMToolUse`` shape in the
        returned ``_RawLLMResponse``. ``cache_directives`` is silently
        ignored on this backend (OpenAI doesn't expose ephemeral cache).

        ``model_transform`` runs before the SDK call so Moonshot can
        strip its ``moonshot/`` prefix and so future Azure OpenAI can
        map deployment names.
        """
        client = self._build_client()
        actual_model = self._model_transform(model)

        chat_messages = [{"role": "system", "content": system_prompt}] + messages
        create_kwargs: dict[str, Any] = {
            "model": actual_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if tools:
            create_kwargs["tools"] = [self._translate_tool_def(t) for t in tools]

        response = client.chat.completions.create(**create_kwargs)
        msg = response.choices[0].message
        text = msg.content or ""
        usage = response.usage

        tool_uses_dicts = self._extract_openai_tool_calls(msg)

        return _RawLLMResponse(
            text=text,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            tool_uses=tool_uses_dicts,
        )

    def format_tool_results(
        self,
        tool_uses: list[LLMToolUse],
        tool_results: list[LLMToolResult],
        assistant_text: str = "",
    ) -> list[dict]:
        """Build OpenAI's two-part tool-loop continuation.

        OpenAI's chat-completion API requires:
        1. An assistant message with ``tool_calls`` listing the tool
           invocations from the prior response.
        2. N tool-role messages, one per result, each with a
           ``tool_call_id`` pointing back at a tool_call.

        Returns the list ready to extend ``messages`` for the next call.
        Empty ``tool_results`` → empty list. atomic_capture tool_uses are
        filtered from the echo (handled by the capture path, not the loop).
        """
        if not tool_results:
            return []

        out: list[dict] = []

        # Assistant turn echoing tool_calls. Build first because the
        # tool-role messages reference these by tool_call_id.
        tool_calls_payload = []
        for tu in tool_uses:
            if tu.name == "atomic_capture":
                continue
            tool_calls_payload.append({
                "id": tu.id,
                "type": "function",
                "function": {
                    "name": tu.name,
                    "arguments": json.dumps(tu.input),
                },
            })
        if tool_calls_payload:
            out.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": tool_calls_payload,
            })

        # One tool-role message per tool_result. Serialization rules
        # match pre-#87 wire bytes:
        # - Error: passed through (already prefixed).
        # - Everything else: json.dumps with str() fallback.
        for r in tool_results:
            if r.is_error and isinstance(r.content, str):
                content = r.content
            else:
                try:
                    content = json.dumps(r.content)
                except (TypeError, ValueError):
                    content = str(r.content)
            out.append({
                "role": "tool",
                "tool_call_id": r.tool_use_id,
                "content": content,
            })

        return out

    # ────────────────────────────────────────────────────────────
    # Private helpers

    def _build_client(self):
        """Build a fresh openai client per call.

        Per-call construction matches AnthropicLLMBackend's pattern —
        sidesteps test-isolation issues where ``patch.dict(sys.modules,
        {"openai": fake})`` is active for a later test. The openai.OpenAI
        constructor is fast (no network call).
        """
        import openai
        api_key = self._resolve_key()
        kwargs: dict[str, Any] = {"api_key": api_key}
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        return openai.OpenAI(**kwargs)

    def _resolve_key(self) -> str:
        """Resolve the API key via the configured KeySpec.

        Delegates to ``_llm._get_key`` — same env-vars → Keychain →
        ``~/.config/atomic_agents/keys.json`` priority order as the
        pre-#87 procedural paths. ``_llm._get_key`` is the framework's
        canonical key resolver; doctor.py also imports it.
        """
        from .._llm import _get_key
        return _get_key(
            env_vars=list(self._key_spec.env_vars),
            keychain_name=self._key_spec.keychain_name,
            config_key=self._key_spec.config_file_key,
        )

    @staticmethod
    def _translate_tool_def(td: LLMToolDefinition) -> dict:
        """Canonical LLMToolDefinition → OpenAI tools schema dict.

        OpenAI shape::

            {"type": "function", "function": {"name", "description", "parameters"}}

        ``strict`` is honored when set (OpenAI's structured-output mode).
        """
        fn: dict[str, Any] = {
            "name": td.name,
            "description": td.description,
            "parameters": td.input_schema,
        }
        if td.strict:
            fn["strict"] = True
        return {"type": "function", "function": fn}

    @staticmethod
    def _extract_openai_tool_calls(msg: Any) -> list[dict]:
        """OpenAI/Moonshot tool_calls → normalized ``{id, name, input}`` dicts.

        Matches pre-#87 ``_llm._extract_openai_tool_calls`` byte-for-byte:
        - dict arguments → use directly
        - str arguments → json.loads (default {} on parse error)
        - None / empty string → default {}
        - anything else → warn, default {}
        """
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return []
        out: list[dict] = []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            raw_args = getattr(fn, "arguments", None)
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                if raw_args.strip():
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = {}
            elif raw_args is None:
                args = {}
            else:
                _logger.warning(
                    "unexpected tool_call.function.arguments shape %r; defaulting to {}",
                    type(raw_args).__name__,
                )
                args = {}
            out.append({
                "id": getattr(tc, "id", ""),
                "name": getattr(fn, "name", ""),
                "input": args,
            })
        return out


def make_openai_backend() -> OpenAICompatibleLLMBackend:
    """Factory for the OpenAI-direct backend (api.openai.com)."""
    return OpenAICompatibleLLMBackend(
        provider_id="openai",
        key_spec=KeySpec(
            env_vars=("ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"),
            keychain_name="atomic-agents-openai",
            config_file_key="openai",
        ),
        model_namespace=lambda m: m.startswith("gpt-"),
        # No model_transform — gpt-* model ids go to OpenAI as-is.
        # No base_url — uses openai SDK's default.
    )

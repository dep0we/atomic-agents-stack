"""LLM provider routing — Anthropic primary, OpenAI/Moonshot optional.

Each provider returns a normalized Response shape. Cost computed externally
via _costs.calc_cost using the model id and the returned token counts.

Per spec/04 cache breakpoints: Anthropic input messages can declare
cache_control points; this module passes them through.

API keys loaded via _platform.get_api_key().
"""

from __future__ import annotations
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from .exceptions import AtomicAgentsError

_logger = logging.getLogger(__name__)


@dataclass
class _RawLLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    raw: dict[str, Any] | None = None
    # Normalized tool_use blocks across providers — each entry:
    #   {"name": "<tool_name>", "input": {...}, "id": "<call_id>"}
    # Empty list when no tools are passed or the model didn't call any.
    tool_uses: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.tool_uses is None:
            self.tool_uses = []


def call_llm(
    model: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.6,
    cache_control_breakpoints: list[int] | None = None,
    tools: list[dict] | None = None,
) -> _RawLLMResponse:
    """Dispatch to the right provider based on model id prefix.

    `tools` is a list of provider-formatted tool definitions. For Anthropic, use
    atomic_agents._capture.anthropic_tool_definition(); for OpenAI/Moonshot, use
    openai_tool_definition(). The agent layer picks the right formatter via
    AtomicAgent._capture_tool_definitions(model).

    Returns _RawLLMResponse normalized across providers, with tool_use blocks
    normalized to {"name": ..., "input": ..., "id": ...} regardless of provider.
    """
    if model.startswith("claude-"):
        return _call_anthropic(
            model, system_prompt, messages, max_tokens, temperature,
            cache_control_breakpoints, tools,
        )
    if model.startswith("gpt-"):
        return _call_openai(model, system_prompt, messages, max_tokens, temperature, tools)
    if model.startswith("moonshot/"):
        return _call_moonshot(model, system_prompt, messages, max_tokens, temperature, tools)
    raise ValueError(f"no provider routing for model: {model}")


def _call_anthropic(
    model: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    cache_breakpoints: list[int] | None = None,
    tools: list[dict] | None = None,
) -> _RawLLMResponse:
    """Call Anthropic's Messages API."""
    try:
        import anthropic
    except ImportError as e:
        raise AtomicAgentsError(
            "anthropic SDK not installed; pip install anthropic"
        ) from e

    api_key = _get_anthropic_key()
    client = anthropic.Anthropic(api_key=api_key)

    # System prompt as a list with cache_control on the long-cached portion
    system_blocks: list[dict[str, Any]]
    if cache_breakpoints:
        # Multiple system blocks with cache_control on stable ones
        # (For v1, just put one ephemeral cache_control on the whole system prompt
        #  if any breakpoints are requested — refine later)
        system_blocks = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
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
        create_kwargs["tools"] = tools

    response = client.messages.create(**create_kwargs)

    # Extract text + tool_use blocks
    text_parts: list[str] = []
    tool_uses: list[dict] = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use":
            tool_uses.append({
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}) or {},
            })
        elif block_type == "text" or hasattr(block, "text"):
            text_parts.append(getattr(block, "text", ""))
    text = "".join(text_parts)

    # Token usage — Anthropic returns input_tokens, output_tokens, cache_*_tokens
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
        tool_uses=tool_uses,
    )


def _call_openai(
    model: str, system_prompt: str, messages: list[dict],
    max_tokens: int, temperature: float, tools: list[dict] | None = None,
) -> _RawLLMResponse:
    try:
        import openai
    except ImportError as e:
        raise AtomicAgentsError(
            "openai SDK not installed; pip install openai (or install with [openai] extra)"
        ) from e

    api_key = _get_openai_key()
    client = openai.OpenAI(api_key=api_key)

    chat_messages = [{"role": "system", "content": system_prompt}] + messages
    create_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": chat_messages,
    }
    if tools:
        create_kwargs["tools"] = tools

    response = client.chat.completions.create(**create_kwargs)
    msg = response.choices[0].message
    text = msg.content or ""
    usage = response.usage

    tool_uses = _extract_openai_tool_calls(msg)

    return _RawLLMResponse(
        text=text,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        tool_uses=tool_uses,
    )


def _call_moonshot(
    model: str, system_prompt: str, messages: list[dict],
    max_tokens: int, temperature: float, tools: list[dict] | None = None,
) -> _RawLLMResponse:
    """Moonshot Kimi API — uses an OpenAI-compatible interface."""
    try:
        import openai
    except ImportError as e:
        raise AtomicAgentsError(
            "openai SDK required for Moonshot routing; pip install openai"
        ) from e

    api_key = _get_moonshot_key()
    # Default base_url targets the China endpoint; operators with keys issued
    # via the international portal (api.moonshot.ai) override via env. Proper
    # per-region handling lands with the LLMBackend protocol (#87).
    base_url = (
        os.environ.get("ATOMIC_AGENTS_MOONSHOT_BASE_URL")
        or os.environ.get("MOONSHOT_BASE_URL")
        or "https://api.moonshot.cn/v1"
    )
    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    actual_model = model.replace("moonshot/", "")

    chat_messages = [{"role": "system", "content": system_prompt}] + messages
    create_kwargs: dict[str, Any] = {
        "model": actual_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": chat_messages,
    }
    if tools:
        create_kwargs["tools"] = tools

    response = client.chat.completions.create(**create_kwargs)
    msg = response.choices[0].message
    text = msg.content or ""
    usage = response.usage

    tool_uses = _extract_openai_tool_calls(msg)

    return _RawLLMResponse(
        text=text,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        tool_uses=tool_uses,
    )


def _extract_openai_tool_calls(msg: Any) -> list[dict]:
    """Convert OpenAI/Moonshot ChatCompletion message tool_calls into normalized dicts.

    OpenAI returns msg.tool_calls = list of objects with id + function {name, arguments}.
    arguments is *usually* a JSON string per OpenAI spec, but some compatible providers
    (e.g. Moonshot) may return an already-parsed dict. We handle all cases:
    - dict   → use directly
    - str    → json.loads
    - None / empty string → default to {}
    - anything else → log a warning, default to {}
    """
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return []
    import json as _json
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
                    args = _json.loads(raw_args)
                except _json.JSONDecodeError:
                    args = {}
            else:
                args = {}
        elif raw_args is None:
            args = {}
        else:
            _logger.warning(
                "unexpected type %r for tool_call arguments — defaulting to {}",
                type(raw_args).__name__,
            )
            args = {}
        out.append({
            "id": getattr(tc, "id", ""),
            "name": getattr(fn, "name", ""),
            "input": args,
        })
    return out


def _get_anthropic_key() -> str:
    return _get_key(
        env_vars=["ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"],
        keychain_name="atomic-agents-anthropic",
        config_key="anthropic",
    )


def _get_openai_key() -> str:
    return _get_key(
        env_vars=["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"],
        keychain_name="atomic-agents-openai",
        config_key="openai",
    )


def _get_moonshot_key() -> str:
    return _get_key(
        env_vars=["ATOMIC_AGENTS_MOONSHOT_KEY", "MOONSHOT_API_KEY"],
        keychain_name="atomic-agents-moonshot",
        config_key="moonshot",
    )


def _get_key(env_vars: list[str], keychain_name: str, config_key: str) -> str:
    """Look up an API key in this order: env vars → Keychain → ~/.config/atomic_agents/keys.json.

    Per spec/04 secrets handling. If none of the sources have the key, raise.
    """
    # Source 1: environment variables
    for var in env_vars:
        val = os.environ.get(var)
        if val:
            return val

    # Source 2: macOS Keychain (Mac only)
    if os.uname().sysname == "Darwin":
        import subprocess
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-a", os.environ.get("USER", ""),
                 "-s", keychain_name, "-w"],
                capture_output=True, text=True, check=True,
            )
            val = result.stdout.strip()
            if val:
                return val
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    # Source 3: ~/.config/atomic_agents/keys.json
    import json
    from pathlib import Path
    config_path = Path.home() / ".config" / "atomic_agents" / "keys.json"
    if config_path.exists():
        try:
            keys = json.loads(config_path.read_text())
            val = keys.get(config_key)
            if val:
                return val
        except (json.JSONDecodeError, OSError):
            pass

    raise AtomicAgentsError(
        f"No API key found for {config_key}. Set one of {env_vars}, "
        f"add to Keychain as '{keychain_name}', or configure "
        f"~/.config/atomic_agents/keys.json"
    )

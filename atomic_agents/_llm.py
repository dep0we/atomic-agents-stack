"""LLM provider routing — Anthropic primary, OpenAI/Moonshot optional.

Each provider returns a normalized Response shape. Cost computed externally
via _costs.calc_cost using the model id and the returned token counts.

Per spec/04 cache breakpoints: Anthropic input messages can declare
cache_control points; this module passes them through.

API keys loaded via _platform.get_api_key().
"""

from __future__ import annotations
import os
import time
from dataclasses import dataclass
from typing import Any

from .exceptions import AtomicAgentsError


@dataclass
class _RawLLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    raw: dict[str, Any] | None = None


def call_llm(
    model: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.6,
    cache_control_breakpoints: list[int] | None = None,
) -> _RawLLMResponse:
    """Dispatch to the right provider based on model id prefix.

    Returns _RawLLMResponse normalized across providers.
    """
    if model.startswith("claude-"):
        return _call_anthropic(
            model, system_prompt, messages, max_tokens, temperature,
            cache_control_breakpoints,
        )
    if model.startswith("gpt-"):
        return _call_openai(model, system_prompt, messages, max_tokens, temperature)
    if model.startswith("moonshot/"):
        return _call_moonshot(model, system_prompt, messages, max_tokens, temperature)
    raise ValueError(f"no provider routing for model: {model}")


def _call_anthropic(
    model: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    cache_breakpoints: list[int] | None = None,
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

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,
        messages=messages,
    )

    # Extract text — concatenate text blocks
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
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
    )


def _call_openai(
    model: str, system_prompt: str, messages: list[dict],
    max_tokens: int, temperature: float,
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
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=chat_messages,
    )
    text = response.choices[0].message.content or ""
    usage = response.usage
    return _RawLLMResponse(
        text=text,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
    )


def _call_moonshot(
    model: str, system_prompt: str, messages: list[dict],
    max_tokens: int, temperature: float,
) -> _RawLLMResponse:
    """Moonshot Kimi API — uses an OpenAI-compatible interface."""
    try:
        import openai
    except ImportError as e:
        raise AtomicAgentsError(
            "openai SDK required for Moonshot routing; pip install openai"
        ) from e

    api_key = _get_moonshot_key()
    client = openai.OpenAI(api_key=api_key, base_url="https://api.moonshot.cn/v1")
    actual_model = model.replace("moonshot/", "")

    chat_messages = [{"role": "system", "content": system_prompt}] + messages
    response = client.chat.completions.create(
        model=actual_model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=chat_messages,
    )
    text = response.choices[0].message.content or ""
    usage = response.usage
    return _RawLLMResponse(
        text=text,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
    )


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

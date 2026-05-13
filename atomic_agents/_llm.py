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
from typing import Any

from .exceptions import AtomicAgentsError

# Canonical home for the normalized LLM response shape is now
# atomic_agents.llm.backend — see #87 PR 1 (canonical types + Protocol).
# Re-exported here so existing imports (`from atomic_agents._llm import
# _RawLLMResponse`) continue to work unchanged for the duration of the
# #87 arc and for any third-party operator who pinned against this path.
from .llm.backend import _RawLLMResponse  # noqa: F401 — public re-export

_logger = logging.getLogger(__name__)


def call_llm(
    model: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.6,
    cache_control_breakpoints: list[int] | None = None,
    tools: list | None = None,
) -> _RawLLMResponse:
    """Dispatch to the right provider based on model id prefix.

    ``tools`` accepts either:

    - ``list[LLMToolDefinition]`` (canonical, the post-#87 shape) — passed
      through to backends that have registered (Anthropic today; OpenAI /
      Moonshot once PR 3 of #87 lands).
    - ``list[dict]`` (legacy Anthropic-shape or OpenAI-shape) — accepted
      for backward compatibility with any external code that pinned to the
      pre-#87 API. Converted to canonical at the entry point. PR 3 / PR 4
      of the LLMBackend arc removes this acceptance once every internal
      caller produces canonical.

    Returns ``_RawLLMResponse`` normalized across providers, with tool_use
    blocks normalized to ``{"name": ..., "input": ..., "id": ...}``
    regardless of provider.
    """
    if model.startswith("claude-"):
        # Route Anthropic through the registry (#87 PR 2). The backend
        # accepts canonical types and translates internally; legacy dict
        # tools are converted at this entry boundary so call sites that
        # haven't migrated keep working.
        from .llm import find_backend_for_model
        from .llm.types import CacheDirective

        canonical_tools = _to_canonical_tool_defs(tools)
        cache_directives = (
            [CacheDirective(breakpoint_id=f"breakpoint_{i}") for i in cache_control_breakpoints]
            if cache_control_breakpoints
            else None
        )
        backend = find_backend_for_model(model)
        return backend.call(
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=canonical_tools,
            cache_directives=cache_directives,
        )
    if model.startswith("gpt-"):
        # Legacy procedural path — PR 3 of #87 introduces
        # OpenAICompatibleLLMBackend which subsumes this branch.
        openai_tools = _to_openai_tool_dicts(tools)
        return _call_openai(model, system_prompt, messages, max_tokens, temperature, openai_tools)
    if model.startswith("moonshot/"):
        openai_tools = _to_openai_tool_dicts(tools)
        return _call_moonshot(model, system_prompt, messages, max_tokens, temperature, openai_tools)
    raise ValueError(f"no provider routing for model: {model}")


def _to_canonical_tool_defs(tools):
    """Accept either canonical ``LLMToolDefinition`` list or legacy dict list.

    Conversion is one-way (dict → canonical) for the Anthropic dispatch
    path. PR 3 / PR 4 of #87 removes this once every internal caller
    produces canonical. Until then the wrapper keeps external pinned
    callers (and the existing `tests/test_llm_tool_uses.py` fixtures)
    working unchanged.

    The legacy dict shape is Anthropic's ``{name, description, input_schema}``
    — that's what ``_capture.anthropic_tool_definition()`` produced and
    what every existing Anthropic-targeted call site passes.

    Per-element shape check (not just ``tools[0]``): a future caller that
    mixes canonical and legacy items in the same list shouldn't slip past
    the entry guard and crash deep inside the backend. Each item is
    converted independently.

    Caveat: legacy dict items must carry exactly ``name`` / ``description``
    / ``input_schema``. Extra keys (e.g., Anthropic's per-tool
    ``cache_control``) are dropped during conversion. Per-tool caching
    support lands with the canonical-type extension tracked in the
    PR-2 follow-up issue.
    """
    if not tools:
        return None
    from .llm.types import LLMToolDefinition
    return [
        t if isinstance(t, LLMToolDefinition)
        else LLMToolDefinition(
            name=t["name"],
            description=t["description"],
            input_schema=t["input_schema"],
        )
        for t in tools
    ]


def _to_openai_tool_dicts(tools):
    """Accept either canonical ``LLMToolDefinition`` list or legacy openai-shape dicts.

    Reverse of ``_to_canonical_tool_defs``: agent.py refactor in PR 2.5
    will produce canonical even for OpenAI / Moonshot models, but
    ``_call_openai`` / ``_call_moonshot`` still expect OpenAI's
    ``{"type": "function", "function": {...}}`` wrapper. Translates
    canonical → openai dict here as transitional glue.

    Per-element shape check so a mixed list (canonical + legacy) doesn't
    crash a downstream consumer that assumes one shape.

    PR 3 of #87 introduces ``OpenAICompatibleLLMBackend.call()`` which
    owns this translation internally; this helper deletes when that PR
    lands and every OpenAI / Moonshot call goes through the registry.
    """
    if not tools:
        return None
    from .llm.types import LLMToolDefinition
    # Fast path: when every item is already a dict, return the input list
    # by reference. Preserves object identity for legacy callers + tests
    # that pre-date the canonical-types refactor and assert `is` equality.
    if all(isinstance(t, dict) for t in tools):
        return tools
    return [
        t if isinstance(t, dict)
        else {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


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

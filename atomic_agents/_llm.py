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
    preferred_provider: str | None = None,
) -> _RawLLMResponse:
    """Dispatch to the right provider via the LLMBackend registry.

    Post-#87 PR 3: every supported model (claude-*, gpt-*, moonshot/*)
    routes through ``find_backend_for_model(model).call(...)``. The
    procedural ``_call_anthropic`` / ``_call_openai`` / ``_call_moonshot``
    functions are gone; the backends own provider translation entirely.

    ``preferred_provider`` resolves ambiguity when multiple registered
    backends claim the same model id (e.g., openai + azure-openai both
    match ``gpt-5``). Typically sourced from ``model.md``'s ``provider:``
    field. ``None`` falls through to the registry's normal single-match
    resolution; ``AmbiguousBackendError`` raises only when ambiguous AND
    no preferred provider is set.

    ``tools`` accepts either canonical ``LLMToolDefinition`` instances
    (the post-#87 shape) or legacy provider-shape dicts (Anthropic's
    ``{name, description, input_schema}`` for Claude, OpenAI's
    ``{type: function, function: {...}}`` for gpt/moonshot). Legacy dicts
    are converted to canonical at the entry boundary so external callers
    pinned to the pre-#87 API continue to work — the helper
    ``_to_canonical_tool_defs`` is the transitional adapter; a future
    cleanup PR removes it once every documented external caller has
    migrated.

    Returns ``_RawLLMResponse`` normalized across providers, with tool_use
    blocks normalized to ``{"name": ..., "input": ..., "id": ...}``
    regardless of provider.

    Raises ``UnknownModelError`` when no registered backend supports the
    model id, or ``AmbiguousBackendError`` when multiple backends claim
    it (resolve via the optional ``provider:`` field on ``model.md``).
    """
    from .llm import find_backend_for_model
    from .llm.types import CacheDirective

    canonical_tools = _to_canonical_tool_defs(tools)
    cache_directives = (
        [CacheDirective(breakpoint_id=f"breakpoint_{i}") for i in cache_control_breakpoints]
        if cache_control_breakpoints
        else None
    )
    backend = find_backend_for_model(model, preferred_provider=preferred_provider)
    return backend.call(
        model=model,
        system_prompt=system_prompt,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=canonical_tools,
        cache_directives=cache_directives,
    )


def _to_canonical_tool_defs(tools):
    """Accept canonical ``LLMToolDefinition`` list or either legacy dict shape.

    Conversion is one-way (dict → canonical). Backends own provider
    translation in the other direction inside their ``call()``.

    Legacy dict shapes both accepted:

    - Anthropic-shape: ``{"name", "description", "input_schema"}`` —
      produced by the pre-#87 ``_capture.anthropic_tool_definition()``.
    - OpenAI-shape: ``{"type": "function", "function": {"name",
      "description", "parameters"}}`` — produced by the pre-#87
      ``_capture.openai_tool_definition()``.

    Per-element shape detection (not just ``tools[0]``): a future caller
    mixing canonical and legacy items in the same list won't crash deep
    inside the backend. Extra keys on legacy dicts (Anthropic's per-tool
    ``cache_control``) are dropped during conversion — see #150 for the
    canonical-type extension that closes that gap.

    Future cleanup PR removes this acceptance once every external caller
    has migrated to canonical (one of the PR 4 follow-ups).
    """
    if not tools:
        return None
    from .llm.types import LLMToolDefinition
    out = []
    for t in tools:
        if isinstance(t, LLMToolDefinition):
            out.append(t)
        elif isinstance(t, dict):
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                out.append(LLMToolDefinition(
                    name=fn["name"],
                    description=fn.get("description", ""),
                    input_schema=fn.get("parameters", {}),
                ))
            else:
                out.append(LLMToolDefinition(
                    name=t["name"],
                    description=t.get("description", ""),
                    input_schema=t.get("input_schema", {}),
                ))
        else:
            raise TypeError(
                f"tool item must be LLMToolDefinition or dict; got "
                f"{type(t).__name__}"
            )
    return out


# The pre-#87 procedural ``_call_openai`` / ``_call_moonshot`` functions
# were deleted in #87 PR 3. Their logic now lives in
# ``atomic_agents.llm.openai_compat`` via ``OpenAICompatibleLLMBackend``
# (which both OpenAI and Moonshot use).


def _extract_openai_tool_calls(msg):
    """Back-compat re-export of the OpenAI tool-calls extractor.

    Re-exported from ``OpenAICompatibleLLMBackend`` so any external
    code that pinned to ``from atomic_agents._llm import
    _extract_openai_tool_calls`` keeps working post-#87 PR 3. The
    canonical implementation now lives in ``openai_compat`` (the
    OpenAI-family backend owns its own provider translation).
    """
    from .llm.openai_compat import OpenAICompatibleLLMBackend
    return OpenAICompatibleLLMBackend._extract_openai_tool_calls(msg)


# The key-resolution helpers below are retained — ``doctor.py:501``
# imports ``_get_key`` directly, and the per-provider thin wrappers
# preserve a stable public surface for any operator who pinned to
# ``from atomic_agents._llm import _get_anthropic_key`` etc.


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

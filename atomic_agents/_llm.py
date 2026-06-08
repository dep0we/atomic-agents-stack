"""LLM provider routing — Anthropic primary, OpenAI/Moonshot optional.

Each provider returns a normalized Response shape. Cost computed externally
via _costs.calc_cost using the model id and the returned token counts.

Per spec/04 cache breakpoints: Anthropic input messages can declare
cache_control points; this module passes them through.

API keys loaded via _platform.get_api_key().
"""

from __future__ import annotations
import logging

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
        [
            CacheDirective(breakpoint_id=f"breakpoint_{i}")
            for i in cache_control_breakpoints
        ]
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
                out.append(
                    LLMToolDefinition(
                        name=fn["name"],
                        description=fn.get("description", ""),
                        input_schema=fn.get("parameters", {}),
                    )
                )
            else:
                out.append(
                    LLMToolDefinition(
                        name=t["name"],
                        description=t.get("description", ""),
                        input_schema=t.get("input_schema", {}),
                    )
                )
        else:
            raise TypeError(
                f"tool item must be LLMToolDefinition or dict; got {type(t).__name__}"
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


# ──────────────────────────────────────────────────────────────────────────────
# Key-resolution helpers — superseded by SecretBackend (spec/38, issue #340).
#
# The credential cascade (env vars → Keychain → keys.json) now lives in
# ``atomic_agents.secret_backend.FilesystemSecretBackend``.  All six live
# callers have been rewired to route through the backend: five via these thin
# redirect wrappers (``_get_key`` and friends — doctor.py check_provider_keys,
# init/wizard.py, llm/openai_compat.py, llm/anthropic.py, judge/llm.py), and
# ``eval._provider_available`` directly via
# ``get_default_secret_backend().has()`` (eval.py:~953).  External importers
# of ``_get_key`` / ``_get_anthropic_key`` etc. continue to work unchanged
# because the wrappers are preserved.
#
# Implementation note: ``_get_key`` calls ``get_default_secret_backend()``
# (the env-var-driven factory) rather than constructing ``FilesystemSecretBackend``
# directly.  This ensures that an operator who sets
# ``ATOMIC_AGENTS_SECRET_BACKEND=gcp`` gets the configured backend for ALL
# resolution paths — runtime AND doctor — not just the top-level factory.
# The ``_get_anthropic_key`` / ``_get_openai_key`` / ``_get_moonshot_key``
# wrappers forward the full legacy (env_vars, keychain_name, config_key) triple
# to ``_get_key``, which routes it to FilesystemSecretBackend.resolve_with_spec
# verbatim (so every alias and the caller-supplied keychain/config_key are
# honored).  Only when the configured backend lacks ``resolve_with_spec`` does
# ``_get_key`` fall back to ``backend.get(env_vars[0])`` using the primary
# env-var name.


def _get_anthropic_key() -> str:
    """Resolve the Anthropic API key via the registered SecretBackend.

    Thin redirect wrapper. Routes through ``_get_key`` which calls the backend.
    Preserved for callers that imported ``_get_anthropic_key`` directly.
    """
    return _get_key(
        env_vars=["ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"],
        keychain_name="atomic-agents-anthropic",
        config_key="anthropic",
    )


def _get_openai_key() -> str:
    """Resolve the OpenAI API key via the registered SecretBackend.

    Thin redirect wrapper. Routes through ``_get_key`` which calls the backend.
    Preserved for callers that imported ``_get_openai_key`` directly.
    """
    return _get_key(
        env_vars=["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"],
        keychain_name="atomic-agents-openai",
        config_key="openai",
    )


def _get_moonshot_key() -> str:
    """Resolve the Moonshot API key via the registered SecretBackend.

    Thin redirect wrapper. Routes through ``_get_key`` which calls the backend.
    Preserved for callers that imported ``_get_moonshot_key`` directly.
    """
    return _get_key(
        env_vars=["ATOMIC_AGENTS_MOONSHOT_KEY", "MOONSHOT_API_KEY"],
        keychain_name="atomic-agents-moonshot",
        config_key="moonshot",
    )


def _get_key(env_vars: list[str], keychain_name: str, config_key: str) -> str:
    """Thin redirect wrapper — delegates to the registered SecretBackend (spec/38).

    Previously: contained the full env → Keychain → keys.json cascade inline.
    Now: calls ``get_default_secret_backend().resolve_with_spec(...)`` so the
    backend is the single source of truth for credential resolution.

    The full ``(env_vars, keychain_name, config_key)`` triple is forwarded to
    ``FilesystemSecretBackend.resolve_with_spec``, which probes every env-var
    alias and uses the caller-supplied keychain service name and keys.json key
    verbatim. This preserves backward compatibility for custom
    ``OpenAICompatibleLLMBackend`` registrations whose ``KeySpec`` carries a
    non-default keychain_name or config_file_key.

    For backends that do not expose ``resolve_with_spec`` (future alternate
    implementations), falls back to ``backend.get(env_vars[0])`` so the public
    Protocol method is still exercised.

    Raises ``AtomicAgentsError`` when the key cannot be resolved via any source
    (constructed directly on the ``resolve_with_spec`` path; chained from the
    underlying ``SecretError`` on the public-Protocol fallback path), preserving
    the same exception type callers expect from the old implementation.
    Raises ``SecretBackendNotRegistered`` (an ``AtomicAgentsError`` subclass)
    unchanged when ``get_default_secret_backend()`` raises it, so operators
    get a diagnostic "backend not registered" message rather than a misleading
    "No API key found" message.
    """
    from .secret_backend import (
        SecretBackendNotRegistered,
        SecretError,
        get_default_secret_backend,
    )

    try:
        backend = get_default_secret_backend()
    except SecretBackendNotRegistered as exc:
        # Backend mis-configured (e.g., ATOMIC_AGENTS_SECRET_BACKEND=gcp not yet
        # installed). Surface the backend error directly — it is already an
        # AtomicAgentsError subclass with a diagnostic message, so re-raising it
        # gives the operator a clear "backend not registered" error rather than
        # a misleading "No API key found" message.
        raise exc

    # Prefer resolve_with_spec when available: it probes all env_var aliases and
    # uses the caller-supplied keychain_name/config_key exactly, preserving the
    # original _get_key contract for custom KeySpecs.
    if hasattr(backend, "resolve_with_spec"):
        val = backend.resolve_with_spec(env_vars, keychain_name, config_key)
        if val is not None:
            return val
        raise AtomicAgentsError(
            f"No API key found for {config_key}. Set one of {env_vars}, "
            f"add to Keychain as '{keychain_name}', or configure "
            f"~/.config/atomic_agents/keys.json"
        )

    # Fallback for alternate SecretBackend implementations that expose only the
    # public Protocol surface (get/get_optional/has/locate). Uses primary env var
    # as the canonical key name; aliases in env_vars[1:] are not probed because
    # the Protocol has no multi-alias resolution method.
    if not env_vars:
        raise AtomicAgentsError(
            f"No env var names supplied for {config_key}; "
            f"cannot resolve via Protocol-only backend."
        )
    try:
        return backend.get(env_vars[0])
    except SecretError as exc:
        raise AtomicAgentsError(
            f"No API key found for {config_key}. Set one of {env_vars}, "
            f"add to Keychain as '{keychain_name}', or configure "
            f"~/.config/atomic_agents/keys.json"
        ) from exc

"""MoonshotLLMBackend — thin factory over OpenAICompatibleLLMBackend.

Moonshot's Kimi API speaks OpenAI's chat-completion shape. The only
provider-specific concerns are:

- API key sources (env vars + Keychain entry named `atomic-agents-moonshot`)
- Model namespace: ids prefixed ``moonshot/``
- Model transform: strip the prefix before sending (the SDK accepts the
  bare model id)
- Base URL: defaults to ``api.moonshot.cn``; international portal users
  override via ``MOONSHOT_BASE_URL`` env var (same convention as the
  pre-#87 procedural ``_call_moonshot``).

Per the #87 plan (codex P3 readability note), shipping a tiny factory
function for Moonshot is preferable to in-line registration so the
config lives next to its provider documentation.
"""

from __future__ import annotations

import os

from .openai_compat import KeySpec, OpenAICompatibleLLMBackend


def _resolve_moonshot_base_url() -> str:
    """Pick the Moonshot base_url from env vars; default to the China endpoint.

    Matches the pre-#87 ``_call_moonshot`` override pattern:
    1. ``ATOMIC_AGENTS_MOONSHOT_BASE_URL`` (framework-prefixed; preferred)
    2. ``MOONSHOT_BASE_URL`` (common convention; community adapters use this)
    3. Default ``https://api.moonshot.cn/v1``

    Resolved at backend construction time. Operators on the
    international portal set the env var before importing atomic_agents
    so the registered backend hits the right endpoint.
    """
    return (
        os.environ.get("ATOMIC_AGENTS_MOONSHOT_BASE_URL")
        or os.environ.get("MOONSHOT_BASE_URL")
        or "https://api.moonshot.cn/v1"
    )


def make_moonshot_backend() -> OpenAICompatibleLLMBackend:
    """Build a fully-configured Moonshot backend instance.

    Registered by ``llm/__init__.py``'s ``_ensure_default_backends``
    alongside Anthropic + OpenAI. Returned instance is an
    ``OpenAICompatibleLLMBackend`` with Moonshot defaults baked in —
    callers don't need to know the subclass type, they get the same
    ``SyncLLMBackend`` Protocol surface.
    """
    return OpenAICompatibleLLMBackend(
        provider_id="moonshot",
        key_spec=KeySpec(
            env_vars=("ATOMIC_AGENTS_MOONSHOT_KEY", "MOONSHOT_API_KEY"),
            keychain_name="atomic-agents-moonshot",
            config_file_key="moonshot",
        ),
        model_namespace=lambda m: m.startswith("moonshot/"),
        # Replace all occurrences (no count arg) to match pre-#87
        # _call_moonshot byte-for-byte. No real model id has the prefix
        # twice; this is purely for behavior parity.
        model_transform=lambda m: m.replace("moonshot/", ""),
        base_url=_resolve_moonshot_base_url(),
    )

"""Tests for MoonshotLLMBackend factory — #87 PR 3."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.llm.moonshot import make_moonshot_backend, _resolve_moonshot_base_url
from atomic_agents.llm.openai_compat import OpenAICompatibleLLMBackend


@pytest.fixture(autouse=True)
def _stub_moonshot_key(monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")


def test_factory_returns_openai_compat_instance_with_moonshot_config():
    """Factory builds an OpenAICompatibleLLMBackend with Moonshot config —
    same Protocol surface as the OpenAI-direct backend, just with the
    right namespace / transform / base_url baked in.
    """
    b = make_moonshot_backend()
    assert isinstance(b, OpenAICompatibleLLMBackend)
    assert b.provider_id == "moonshot"


def test_supports_moonshot_prefixed_models_only():
    """The factory's model_namespace claims ``moonshot/*`` ids; doesn't
    poach gpt-* (those go to the OpenAI-direct backend).
    """
    b = make_moonshot_backend()
    assert b.supports_model("moonshot/kimi-k2.6")
    assert b.supports_model("moonshot/kimi-k2-0905-preview")
    assert not b.supports_model("gpt-5")
    assert not b.supports_model("claude-opus-4-7")


def test_model_transform_strips_moonshot_prefix():
    """``moonshot/kimi-k2.6`` → ``kimi-k2.6`` before the SDK call (the
    Moonshot API doesn't accept the prefix; that's a framework-local
    namespace marker). Verified end-to-end via a stubbed openai client.
    """
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(
            content="ok", tool_calls=None,
        ))],
        usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b = make_moonshot_backend()
        b.call(
            model="moonshot/kimi-k2.6", system_prompt="s",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10, temperature=1.0,
        )
    _, kw = fake_client.chat.completions.create.call_args
    assert kw["model"] == "kimi-k2.6"  # prefix stripped


def test_default_base_url_is_china_endpoint(monkeypatch):
    """Without env override, base_url targets ``api.moonshot.cn`` —
    matches the pre-#87 procedural default.
    """
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_MOONSHOT_BASE_URL", raising=False)
    assert _resolve_moonshot_base_url() == "https://api.moonshot.cn/v1"


def test_framework_prefixed_env_takes_precedence():
    """``ATOMIC_AGENTS_MOONSHOT_BASE_URL`` wins over generic
    ``MOONSHOT_BASE_URL``. Matches the pre-#87 helper precedence.
    """
    with patch.dict(os.environ, {
        "ATOMIC_AGENTS_MOONSHOT_BASE_URL": "https://framework.example/v1",
        "MOONSHOT_BASE_URL": "https://generic.example/v1",
    }):
        assert _resolve_moonshot_base_url() == "https://framework.example/v1"


def test_generic_env_used_when_framework_var_absent():
    """When the framework-prefixed var is missing, fall back to the
    community-convention ``MOONSHOT_BASE_URL``.
    """
    with patch.dict(os.environ, {
        "MOONSHOT_BASE_URL": "https://api.moonshot.ai/v1",
    }, clear=False):
        os.environ.pop("ATOMIC_AGENTS_MOONSHOT_BASE_URL", None)
        assert _resolve_moonshot_base_url() == "https://api.moonshot.ai/v1"

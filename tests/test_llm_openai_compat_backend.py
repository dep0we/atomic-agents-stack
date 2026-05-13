"""Tests for OpenAICompatibleLLMBackend — #87 PR 3 reference implementation."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.llm.openai_compat import (
    KeySpec,
    OpenAICompatibleLLMBackend,
    make_openai_backend,
)
from atomic_agents.llm.types import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures + helpers


@pytest.fixture(autouse=True)
def _stub_api_keys(monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_OPENAI_KEY", "fake-key")
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")


def _make_openai_response(text="", tool_calls=None, prompt_tokens=10, completion_tokens=20):
    msg = types.SimpleNamespace(content=text, tool_calls=tool_calls)
    choice = types.SimpleNamespace(message=msg)
    usage = types.SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return types.SimpleNamespace(choices=[choice], usage=usage)


def _make_backend(**overrides):
    """Build a minimal OpenAICompatibleLLMBackend for testing."""
    kwargs = dict(
        provider_id="test-provider",
        key_spec=KeySpec(
            env_vars=("ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"),
            keychain_name="atomic-agents-openai",
            config_file_key="openai",
        ),
        model_namespace=lambda m: m.startswith("test-"),
    )
    kwargs.update(overrides)
    return OpenAICompatibleLLMBackend(**kwargs)


# ──────────────────────────────────────────────────────────────────
# Construction + Protocol surface


def test_construction_requires_openai_sdk(monkeypatch):
    """Missing openai SDK raises a clean framework error at construction
    (matches the AnthropicLLMBackend pattern — fail fast, not at first
    call())."""
    from atomic_agents.exceptions import AtomicAgentsError

    # Hide openai from sys.modules + the import machinery
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(AtomicAgentsError, match="openai SDK not installed"):
        OpenAICompatibleLLMBackend(
            provider_id="test",
            key_spec=KeySpec(("X",), "x", "x"),
            model_namespace=lambda m: True,
        )


def test_provider_id_returned():
    b = _make_backend(provider_id="azure-openai")
    assert b.provider_id == "azure-openai"


def test_supports_model_via_namespace_predicate():
    b = _make_backend(model_namespace=lambda m: m.startswith("gpt-"))
    assert b.supports_model("gpt-5")
    assert b.supports_model("gpt-4o-mini")
    assert not b.supports_model("claude-opus-4-7")
    assert not b.supports_model("moonshot/kimi-k2.6")


def test_capabilities_default_openai_family():
    """Without ``capability_hooks``, models get the OpenAI-family default
    capability shape (tools=True, cache_control=False, etc.).
    """
    b = _make_backend()
    caps = b.capabilities("test-model")
    assert isinstance(caps, LLMCapabilities)
    assert caps.tools is True
    assert caps.tool_results is True
    assert caps.cache_control is False  # OpenAI has no ephemeral cache today
    assert caps.usage_reporting is True


def test_capabilities_per_model_hook_overrides():
    """``capability_hooks`` lets a backend customize a specific model's
    capability shape — e.g., a vision-capable variant or an endpoint that
    doesn't support tools.
    """
    custom = LLMCapabilities(
        tools=False, tool_results=False, cache_control=False, streaming=False,
        vision=True, max_input_tokens=100_000, max_output_tokens=8_192,
        usage_reporting=False, structured_output=False,
    )
    b = _make_backend(capability_hooks={"test-vision-only": custom})
    assert b.capabilities("test-vision-only") is custom
    # Other models still get the default
    assert b.capabilities("test-other").tools is True


# ──────────────────────────────────────────────────────────────────
# pricing


def test_pricing_known_model_returns_real_rates():
    """PRICING table values land in PricingInfo; backend delegates rather
    than maintaining a parallel table.
    """
    b = _make_backend()
    pi = b.pricing("gpt-5")
    assert isinstance(pi, PricingInfo)
    assert pi.input_per_million_usd == 5.0
    assert pi.output_per_million_usd == 20.0
    # OpenAI-family doesn't expose cache discount; backend reports 1.0
    assert pi.cache_hit_discount == 1.0


def test_pricing_tries_transformed_model_id():
    """Moonshot's prefix-stripping transform means the operator passes
    ``moonshot/kimi-k2.6`` but the SDK gets ``kimi-k2.6``. PRICING may
    contain either form; backend tries the transformed id if the raw id
    misses.
    """
    b = _make_backend(
        model_namespace=lambda m: m.startswith("special/"),
        model_transform=lambda m: m.replace("special/", "", 1),
    )
    # Inject a temporary PRICING entry for the transformed id only
    from atomic_agents._costs import PRICING
    original = dict(PRICING)
    PRICING["test-only-transformed-model"] = {"input": 1.5, "output": 6.0}
    try:
        pi = b.pricing("special/test-only-transformed-model")
        assert pi is not None
        assert pi.input_per_million_usd == 1.5
    finally:
        PRICING.clear()
        PRICING.update(original)


def test_pricing_unknown_model_returns_none():
    b = _make_backend()
    assert b.pricing("totally-unknown-model-xyz") is None


# ──────────────────────────────────────────────────────────────────
# count_tokens


def test_count_tokens_heuristic_fallback_when_tiktoken_missing(monkeypatch):
    """Without tiktoken, the backend falls back to the 4-chars-per-token
    heuristic so cost guardrails stay conservative-pessimistic.
    """
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    b = _make_backend()
    n = b.count_tokens(
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "hi there"}],
    )
    assert n > 0


# ──────────────────────────────────────────────────────────────────
# call() — the meat


def test_call_returns_normalized_response():
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_openai_response("Hi.")
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b = _make_backend()
        r = b.call(
            model="test-foo",
            system_prompt="sys",
            messages=[{"role": "user", "content": "say hi"}],
            max_tokens=100,
            temperature=0.3,
        )
    assert r.text == "Hi."
    assert r.input_tokens == 10
    assert r.output_tokens == 20
    assert r.tool_uses == []


def test_call_translates_canonical_tools_to_openai_dict_format():
    """LLMToolDefinition → OpenAI's `{type, function: {...}}` schema at
    the SDK boundary. Backend's job — keep provider-shape out of the
    caller.
    """
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_openai_response("")
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
    canonical = [LLMToolDefinition(
        name="search", description="search the web",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )]
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b = _make_backend()
        b.call(
            model="test-foo", system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100, temperature=0.3, tools=canonical,
        )
    _, kw = fake_client.chat.completions.create.call_args
    assert kw["tools"] == [{
        "type": "function",
        "function": {
            "name": "search",
            "description": "search the web",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }]


def test_call_strict_field_is_emitted_when_set():
    """LLMToolDefinition.strict=True opts into OpenAI's structured-output
    mode at the wire (emit ``strict: true`` in the function dict).
    """
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_openai_response("")
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
    canonical = [LLMToolDefinition(
        name="strict_tool", description="x", input_schema={"type": "object"}, strict=True,
    )]
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b = _make_backend()
        b.call(
            model="test-foo", system_prompt="sys", messages=[],
            max_tokens=100, temperature=0.3, tools=canonical,
        )
    _, kw = fake_client.chat.completions.create.call_args
    assert kw["tools"][0]["function"]["strict"] is True


def test_call_model_transform_strips_prefix_before_sdk():
    """Moonshot's ``moonshot/`` prefix is stripped before the SDK call.
    Verifies the generic ``model_transform`` hook works.
    """
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_openai_response("")
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
    b = _make_backend(
        model_namespace=lambda m: m.startswith("foo/"),
        model_transform=lambda m: m.replace("foo/", "", 1),
    )
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b.call(
            model="foo/bar-baz", system_prompt="sys", messages=[],
            max_tokens=10, temperature=0.3,
        )
    _, kw = fake_client.chat.completions.create.call_args
    assert kw["model"] == "bar-baz"


def test_call_base_url_passed_to_client():
    """When base_url is set, it's threaded through to the openai.OpenAI
    constructor. Critical for Moonshot's region-specific endpoints.
    """
    captured = {}

    def fake_constructor(api_key, **kw):
        captured.update(kw)
        c = MagicMock()
        c.chat.completions.create.return_value = _make_openai_response("")
        return c

    fake_openai = types.SimpleNamespace(OpenAI=fake_constructor)
    b = _make_backend(base_url="https://example.test/v1")
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b.call(model="test-x", system_prompt="s", messages=[],
               max_tokens=10, temperature=0.3)
    assert captured.get("base_url") == "https://example.test/v1"


def test_call_no_base_url_omits_kwarg():
    """When base_url=None, the openai SDK uses its default (api.openai.com).
    Verify we don't pass base_url=None (some SDK versions reject that).
    """
    captured = {}

    def fake_constructor(api_key, **kw):
        captured.update(kw)
        c = MagicMock()
        c.chat.completions.create.return_value = _make_openai_response("")
        return c

    fake_openai = types.SimpleNamespace(OpenAI=fake_constructor)
    b = _make_backend()  # no base_url
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b.call(model="test-x", system_prompt="s", messages=[],
               max_tokens=10, temperature=0.3)
    assert "base_url" not in captured


def test_call_cache_directives_ignored():
    """OpenAI doesn't have ephemeral cache; directives should be silently
    accepted and dropped (consistent with the Protocol contract).
    """
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_openai_response("")
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
    b = _make_backend()
    with patch.dict(sys.modules, {"openai": fake_openai}):
        # Should not raise; directives just dropped
        b.call(
            model="test-x", system_prompt="s", messages=[],
            max_tokens=10, temperature=0.3,
            cache_directives=[CacheDirective(breakpoint_id="system-persona")],
        )
    # System message is plain (no cache_control wrap)
    _, kw = fake_client.chat.completions.create.call_args
    system_msg = kw["messages"][0]
    assert system_msg == {"role": "system", "content": "s"}


def test_call_extracts_tool_calls_to_normalized_dicts():
    """OpenAI msg.tool_calls → ``{id, name, input}`` list. JSON-string
    arguments parsed at backend boundary; agent layer sees dicts.
    """
    fn = types.SimpleNamespace(
        name="search",
        arguments=json.dumps({"q": "atomic agents"}),
    )
    tc = types.SimpleNamespace(id="call_001", function=fn)
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_openai_response("", tool_calls=[tc])
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
    with patch.dict(sys.modules, {"openai": fake_openai}):
        b = _make_backend()
        r = b.call(
            model="test-x", system_prompt="s", messages=[],
            max_tokens=10, temperature=0.3,
        )
    assert len(r.tool_uses) == 1
    tu = r.tool_uses[0]
    assert tu["id"] == "call_001"
    assert tu["name"] == "search"
    assert tu["input"] == {"q": "atomic agents"}


def test_extract_handles_dict_arguments_directly():
    """Moonshot returns pre-parsed dict arguments; backend accepts both
    str (json.loads) and dict (pass through) shapes.
    """
    msg = types.SimpleNamespace(tool_calls=[
        types.SimpleNamespace(id="call_1", function=types.SimpleNamespace(
            name="t", arguments={"already": "parsed"},
        )),
    ])
    out = OpenAICompatibleLLMBackend._extract_openai_tool_calls(msg)
    assert out[0]["input"] == {"already": "parsed"}


# ──────────────────────────────────────────────────────────────────
# format_tool_results


def test_format_tool_results_builds_assistant_tool_calls_plus_tool_messages():
    """OpenAI's continuation: one assistant msg with tool_calls + N
    tool-role messages, one per result.
    """
    b = _make_backend()
    tool_uses = [LLMToolUse(id="tc_1", name="search", input={"q": "x"})]
    tool_results = [LLMToolResult(tool_use_id="tc_1", content="result text")]
    out = b.format_tool_results(
        tool_uses=tool_uses, tool_results=tool_results,
        assistant_text="Searching.",
    )
    # 1 assistant + 1 tool
    assert len(out) == 2
    asst, tool_msg = out
    assert asst["role"] == "assistant"
    assert asst["content"] == "Searching."
    assert asst["tool_calls"] == [{
        "id": "tc_1",
        "type": "function",
        "function": {"name": "search", "arguments": '{"q": "x"}'},
    }]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "tc_1"
    # Wire bytes: string content is json-encoded (matches pre-#87)
    assert tool_msg["content"] == '"result text"'


def test_format_tool_results_omits_atomic_capture_from_tool_calls():
    """atomic_capture is handled by the capture path, not the tool loop;
    the assistant-echo's tool_calls list skips it.
    """
    b = _make_backend()
    tool_uses = [
        LLMToolUse(id="tc_cap", name="atomic_capture", input={}),
        LLMToolUse(id="tc_search", name="search", input={"q": "x"}),
    ]
    tool_results = [LLMToolResult(tool_use_id="tc_search", content="result")]
    out = b.format_tool_results(tool_uses=tool_uses, tool_results=tool_results)
    asst = out[0]
    tool_call_names = [tc["function"]["name"] for tc in asst["tool_calls"]]
    assert "atomic_capture" not in tool_call_names
    assert "search" in tool_call_names


def test_format_tool_results_empty_returns_empty():
    b = _make_backend()
    out = b.format_tool_results(tool_uses=[], tool_results=[])
    assert out == []


def test_format_tool_results_error_string_passes_through_raw():
    """Error content is already a string prefixed ``[tool error]``; the
    backend passes it through verbatim rather than re-json-encoding (matches
    pre-#87 wire bytes — both Anthropic and OpenAI helpers had this rule).
    """
    b = _make_backend()
    out = b.format_tool_results(
        tool_uses=[LLMToolUse(id="tc_1", name="search", input={})],
        tool_results=[LLMToolResult(
            tool_use_id="tc_1", content="[tool error] boom", is_error=True,
        )],
    )
    tool_msg = out[1]
    assert tool_msg["content"] == "[tool error] boom"  # raw, no quotes


def test_format_tool_results_non_json_serializable_falls_back_to_str():
    """Non-JSON-serializable tool outputs (datetime, custom classes) don't
    crash — fall back to ``str()``. Matches pre-#87 helper behavior.
    """
    import datetime as _dt
    b = _make_backend()
    t = _dt.datetime(2026, 1, 2, 3, 4, 5)
    out = b.format_tool_results(
        tool_uses=[LLMToolUse(id="tc_1", name="clock", input={})],
        tool_results=[LLMToolResult(tool_use_id="tc_1", content=t)],
    )
    tool_msg = out[1]
    assert tool_msg["content"] == str(t)


# ──────────────────────────────────────────────────────────────────
# make_openai_backend factory


def test_make_openai_backend_configures_openai_direct():
    """The factory produces a backend with provider_id='openai' that
    matches gpt-* models and uses the default openai endpoint.
    """
    b = make_openai_backend()
    assert b.provider_id == "openai"
    assert b.supports_model("gpt-5")
    assert b.supports_model("gpt-4o-mini")
    assert not b.supports_model("claude-opus-4-7")
    assert not b.supports_model("moonshot/kimi-k2.6")

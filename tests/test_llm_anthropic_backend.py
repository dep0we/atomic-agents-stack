"""Tests for AnthropicLLMBackend — #87 PR 2 reference implementation."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.llm.anthropic import AnthropicLLMBackend, _resolve_claude_family
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
def _stub_anthropic_key(monkeypatch):
    """Every test sets the env-var key so _build_client doesn't go hunting."""
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")


def _make_anthropic_block(block_type, **kwargs):
    """Anthropic content-block stub."""
    return types.SimpleNamespace(type=block_type, **kwargs)


def _make_anthropic_response(blocks, *, input_tokens=10, output_tokens=20,
                              cache_read=0, cache_creation=0):
    """Anthropic messages.create() response stub."""
    usage = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )
    return types.SimpleNamespace(content=blocks, usage=usage,
                                  model_dump=lambda: {"id": "msg_test"})


# ──────────────────────────────────────────────────────────────────
# Static surface


def test_resolve_claude_family_strips_date_suffix():
    """The capability + pricing tables are keyed by family (no date); the
    resolver normalizes 'claude-opus-4-7-20260101' → 'claude-opus-4-7'.
    """
    assert _resolve_claude_family("claude-opus-4-7") == "claude-opus-4-7"
    assert _resolve_claude_family("claude-opus-4-7-20260101") == "claude-opus-4-7"
    assert _resolve_claude_family("claude-sonnet-4-6-20260101") == "claude-sonnet-4-6"
    assert _resolve_claude_family("claude-haiku-4-5") == "claude-haiku-4-5"


def test_resolve_claude_family_unknown_returns_none():
    """Non-Claude models — and Claude models from outside the known
    families (future ones not yet in _CLAUDE_CAPABILITIES) — return None."""
    assert _resolve_claude_family("gpt-5") is None
    assert _resolve_claude_family("moonshot/kimi-k2.6") is None
    assert _resolve_claude_family("claude-future-99-99") is None


def test_provider_id():
    assert AnthropicLLMBackend().provider_id == "anthropic"


def test_supports_model_positive_negative():
    """Any ``claude-*`` model id routes through this backend — forward
    compatibility property (see ``supports_model`` docstring for the
    rationale). Non-Claude models route elsewhere (or to UnknownModelError
    when no backend claims them).
    """
    b = AnthropicLLMBackend()
    assert b.supports_model("claude-opus-4-7-20260101")
    assert b.supports_model("claude-sonnet-4-6")
    assert b.supports_model("claude-haiku-4-5-20251001")
    # Future-but-unknown Claude — still True (capabilities() fills in
    # conservative defaults). Verified end-to-end in
    # test_future_claude_model_id_routes_to_backend_with_conservative_defaults.
    assert b.supports_model("claude-future-99-99")
    # Non-Claude models — not this backend's territory
    assert not b.supports_model("gpt-5")
    assert not b.supports_model("moonshot/kimi-k2.6")


# ──────────────────────────────────────────────────────────────────
# capabilities


def test_capabilities_opus_has_vision_and_largest_output():
    caps = AnthropicLLMBackend().capabilities("claude-opus-4-7-20260101")
    assert isinstance(caps, LLMCapabilities)
    assert caps.tools is True
    assert caps.tool_results is True
    assert caps.cache_control is True
    assert caps.streaming is False  # v1 sync only
    assert caps.vision is True
    assert caps.max_input_tokens == 200_000
    assert caps.max_output_tokens == 32_000
    assert caps.usage_reporting is True


def test_capabilities_haiku_has_smaller_output_cap():
    """Haiku output cap differs from Opus — capabilities are per-model
    (codex P2 fix from the plan)."""
    caps = AnthropicLLMBackend().capabilities("claude-haiku-4-5-20251001")
    assert caps.max_output_tokens == 8_192
    assert caps.vision is True


def test_capabilities_unknown_model_returns_conservative_defaults():
    """A racy lookup (supports_model returned True, then capabilities
    called with an unfamiliar model id) shouldn't crash — return cautious
    defaults so the caller can still make a decision.
    """
    caps = AnthropicLLMBackend().capabilities("claude-zzz-0-0")
    assert caps.tools is True
    assert caps.vision is False  # conservative
    assert caps.max_output_tokens == 4_096  # conservative


# ──────────────────────────────────────────────────────────────────
# pricing


def test_pricing_known_model_returns_real_rates():
    """PRICING table values land in the PricingInfo dataclass — backend
    delegates to the framework table rather than maintaining a parallel
    copy (avoids drift)."""
    pi = AnthropicLLMBackend().pricing("claude-opus-4-7-20260101")
    assert isinstance(pi, PricingInfo)
    assert pi.input_per_million_usd == 15.0
    assert pi.output_per_million_usd == 75.0
    assert pi.cache_hit_discount == 0.10


def test_pricing_unknown_model_returns_none():
    """When _costs.PRICING doesn't know the model, return None — caller
    (cost gates) falls back to _fallback_pricing()."""
    assert AnthropicLLMBackend().pricing("claude-unknown-9-9-99999999") is None


# ──────────────────────────────────────────────────────────────────
# count_tokens


def test_count_tokens_falls_back_when_sdk_method_missing():
    """Older anthropic SDK versions lack messages.count_tokens; the
    backend's heuristic returns a positive estimate so cost guardrails
    keep working (conservative-pessimistic per CLAUDE.md rule #4).
    """
    fake_client = MagicMock()
    # Removing count_tokens via spec=[] forces AttributeError
    fake_client.messages = MagicMock(spec=[])  # no count_tokens attribute
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        n = b.count_tokens(
            system_prompt="You are a helpful assistant.",
            messages=[{"role": "user", "content": "hi there"}],
        )
    assert n > 0


def test_count_tokens_uses_sdk_when_available():
    """When the SDK's count_tokens exists, the backend defers to it.

    Mock the SDK's count_tokens to return a known value; assert the
    backend returns that value verbatim.
    """
    fake_client = MagicMock()
    fake_client.messages.count_tokens.return_value = types.SimpleNamespace(
        input_tokens=4242,
    )
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        n = b.count_tokens(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[LLMToolDefinition(name="t", description="d", input_schema={})],
        )
    assert n == 4242
    # And the SDK was invoked with translated tools
    _, kw = fake_client.messages.count_tokens.call_args
    assert kw["tools"] == [{"name": "t", "description": "d", "input_schema": {}}]


# ──────────────────────────────────────────────────────────────────
# call() — the meat


def test_call_returns_normalized_response_text_only():
    """Pure-text response → text populated, tool_uses empty."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response([
        _make_anthropic_block("text", text="Hello world"),
    ])
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        r = b.call(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=200,
            temperature=0.5,
        )
    assert r.text == "Hello world"
    assert r.tool_uses == []
    assert r.input_tokens == 10
    assert r.output_tokens == 20


def test_call_extracts_tool_use_blocks_to_normalized_dicts():
    """Anthropic tool_use content blocks → list of normalized dicts in
    response.tool_uses (matches the pre-#87 procedural shape so callers
    upstream continue to work)."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response([
        _make_anthropic_block("text", text="Capturing."),
        _make_anthropic_block("tool_use", id="tu_1", name="atomic_capture",
                              input={"type": "feedback", "name": "x"}),
    ])
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        r = b.call(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "say hi and capture"}],
            max_tokens=200,
            temperature=0.6,
        )
    assert r.text == "Capturing."
    assert len(r.tool_uses) == 1
    tu = r.tool_uses[0]
    assert tu["id"] == "tu_1"
    assert tu["name"] == "atomic_capture"
    assert tu["input"] == {"type": "feedback", "name": "x"}


def test_call_translates_canonical_tools_to_anthropic_dict_format():
    """LLMToolDefinition → Anthropic tools schema at the SDK boundary.

    The backend's job: never let provider-shape leak out of `call()`.
    """
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response([
        _make_anthropic_block("text", text=""),
    ])
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    canonical = [
        LLMToolDefinition(
            name="atomic_capture",
            description="capture a memory",
            input_schema={"type": "object", "properties": {}},
        ),
    ]
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        b.call(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
            tools=canonical,
        )
    _, kw = fake_client.messages.create.call_args
    assert kw["tools"] == [{
        "name": "atomic_capture",
        "description": "capture a memory",
        "input_schema": {"type": "object", "properties": {}},
    }]


def test_call_omits_tools_kwarg_when_none_passed():
    """No tools → no `tools` kwarg in messages.create (preserves SDK's
    cleanest call path and matches pre-#87 _call_anthropic behavior).
    """
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response([
        _make_anthropic_block("text", text=""),
    ])
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        b.call(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
        )
    _, kw = fake_client.messages.create.call_args
    assert "tools" not in kw


def test_call_applies_cache_control_when_directives_present():
    """A CacheDirective list → system block gets cache_control: ephemeral.

    v1 maps the directive list to a single ephemeral cache block on the
    system prompt — matches pre-#87 _call_anthropic behavior so existing
    long-persona cache-hit rates are preserved.
    """
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response([
        _make_anthropic_block("text", text=""),
    ])
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        b.call(
            model="claude-haiku-4-5-20251001",
            system_prompt="long persona text",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
            cache_directives=[CacheDirective(breakpoint_id="system-persona")],
        )
    _, kw = fake_client.messages.create.call_args
    sys_blocks = kw["system"]
    assert len(sys_blocks) == 1
    assert sys_blocks[0].get("cache_control") == {"type": "ephemeral"}


def test_call_no_cache_directives_no_cache_control():
    """Without cache_directives, the system block is plain text — no
    cache_control field appended."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response([
        _make_anthropic_block("text", text=""),
    ])
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        b.call(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
        )
    _, kw = fake_client.messages.create.call_args
    assert "cache_control" not in kw["system"][0]


def test_call_propagates_cache_tokens():
    """Provider cache_read + cache_creation token counts surface in the
    normalized response so cost math handles cache discounts correctly.
    """
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response(
        [_make_anthropic_block("text", text="")],
        input_tokens=100, output_tokens=20, cache_read=500, cache_creation=10,
    )
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        b = AnthropicLLMBackend()
        r = b.call(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
        )
    # input_tokens in normalized = uncached + cache_read + cache_creation
    assert r.input_tokens == 100 + 500 + 10
    assert r.cache_hit_tokens == 500
    assert r.cache_miss_tokens == r.input_tokens - 500


# ──────────────────────────────────────────────────────────────────
# format_tool_results


def test_format_tool_results_builds_assistant_echo_and_user_results():
    """Anthropic tool-loop continuation: echo the prior assistant turn's
    tool_use blocks + user turn with tool_result blocks. Both messages
    returned in order.
    """
    b = AnthropicLLMBackend()
    tool_uses = [
        LLMToolUse(id="tu_1", name="search", input={"q": "atomic"}),
    ]
    tool_results = [
        LLMToolResult(tool_use_id="tu_1", content="search results here"),
    ]
    out = b.format_tool_results(
        tool_uses=tool_uses, tool_results=tool_results,
        assistant_text="I'll search.",
    )
    assert len(out) == 2
    asst, usr = out
    # Assistant turn — text first, then tool_use blocks
    assert asst["role"] == "assistant"
    assert asst["content"][0] == {"type": "text", "text": "I'll search."}
    assert asst["content"][1] == {
        "type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "atomic"},
    }
    # User turn — tool_result blocks
    assert usr["role"] == "user"
    assert usr["content"][0] == {
        "type": "tool_result", "tool_use_id": "tu_1", "content": "search results here",
    }


def test_format_tool_results_omits_atomic_capture_from_echo():
    """atomic_capture is handled by the capture path (not the tool loop);
    the assistant-echo turn must skip it so the model doesn't see the
    capture being called by itself, only the custom tools.
    """
    b = AnthropicLLMBackend()
    tool_uses = [
        LLMToolUse(id="tu_cap", name="atomic_capture", input={}),
        LLMToolUse(id="tu_search", name="search", input={"q": "x"}),
    ]
    tool_results = [
        LLMToolResult(tool_use_id="tu_search", content="results"),
    ]
    out = b.format_tool_results(tool_uses=tool_uses, tool_results=tool_results)
    asst = out[0]
    tool_use_names = [
        b.get("name") for b in asst["content"] if b.get("type") == "tool_use"
    ]
    assert "atomic_capture" not in tool_use_names
    assert "search" in tool_use_names


def test_format_tool_results_empty_results_returns_empty_list():
    """No tool results → no follow-up messages needed; the tool loop is
    done. Returning [] keeps agent.py's `messages.extend(...)` a no-op.
    """
    b = AnthropicLLMBackend()
    out = b.format_tool_results(
        tool_uses=[LLMToolUse(id="x", name="y", input={})],
        tool_results=[],
    )
    assert out == []


def test_format_tool_results_marks_is_error_block():
    """is_error=True on LLMToolResult → tool_result block gets
    `"is_error": true` so Anthropic can route the model's error-handling
    behavior correctly.
    """
    b = AnthropicLLMBackend()
    out = b.format_tool_results(
        tool_uses=[LLMToolUse(id="tu_1", name="search", input={})],
        tool_results=[LLMToolResult(tool_use_id="tu_1", content="boom",
                                     is_error=True)],
    )
    usr = out[1]
    block = usr["content"][0]
    assert block["is_error"] is True


def test_format_tool_results_serializes_dict_content_as_json():
    """LLMToolResult.content can be str or dict; dict → json string for
    the wire (Anthropic expects content as a string).
    """
    b = AnthropicLLMBackend()
    out = b.format_tool_results(
        tool_uses=[LLMToolUse(id="tu_1", name="search", input={})],
        tool_results=[LLMToolResult(tool_use_id="tu_1",
                                     content={"hits": [1, 2, 3]})],
    )
    block = out[1]["content"][0]
    assert json.loads(block["content"]) == {"hits": [1, 2, 3]}


# ──────────────────────────────────────────────────────────────────
# Registration


def test_legacy_dict_tool_with_cache_control_drops_field_pins_known_gap(monkeypatch):
    """Pin the known limitation: per-tool ``cache_control`` on a legacy
    Anthropic-shape dict is dropped during canonical-types roundtrip.

    Anthropic documents attaching ``cache_control`` to a tool definition
    to cache the entire tools block. Pre-#87 the procedural
    ``_call_anthropic`` passed the dict through to the SDK by reference,
    so the field survived. Post-#87 the canonical-types roundtrip
    extracts only ``name``/``description``/``input_schema`` and drops
    everything else.

    No internal site uses this today; if you're hitting this test as a
    failure because you added per-tool cache support, update the test
    to assert the new pass-through behavior AND remove the matching
    TODO in ``atomic_agents.llm.types.LLMToolDefinition`` docstring.

    Tracked follow-up: per-tool ``cache_breakpoint`` field on
    ``LLMToolDefinition`` lands with the canonical-types extension
    issue filed alongside this PR.
    """
    from atomic_agents import _llm
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_anthropic_response([
        _make_anthropic_block("text", text=""),
    ])
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    tools_with_cache = [{
        "name": "search",
        "description": "search the web",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "cache_control": {"type": "ephemeral"},  # <- this is what gets dropped
    }]
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        _llm.call_llm(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools_with_cache,
        )
    _, kw = fake_client.messages.create.call_args
    sdk_tools = kw["tools"]
    assert len(sdk_tools) == 1
    # The other fields survived
    assert sdk_tools[0]["name"] == "search"
    assert sdk_tools[0]["description"] == "search the web"
    assert sdk_tools[0]["input_schema"]["type"] == "object"
    # cache_control is the documented gap
    assert "cache_control" not in sdk_tools[0]


def test_future_claude_model_id_routes_to_backend_with_conservative_defaults():
    """Forward-compat property: a ``claude-*`` model id not in
    ``_CLAUDE_CAPABILITIES`` (e.g., a model Anthropic ships after our
    capability table was last updated) still routes to AnthropicLLMBackend.

    ``capabilities()`` returns conservative defaults (vision=False,
    max_output=4096). The SDK call still proceeds — operators pinning to
    a brand-new model id are not gated on framework updates.
    """
    b = AnthropicLLMBackend()
    assert b.supports_model("claude-future-99-99-99999999")
    caps = b.capabilities("claude-future-99-99-99999999")
    # Conservative defaults — see anthropic.py _CLAUDE_CAPABILITIES fallback
    assert caps.vision is False
    assert caps.max_output_tokens == 4_096
    # The framework keeps working — the registry routes the call.
    from atomic_agents.llm import find_backend_for_model
    routed = find_backend_for_model("claude-future-99-99-99999999")
    assert routed is b or routed.provider_id == "anthropic"


def test_backend_registers_via_lazy_default_init():
    """``find_backend_for_model('claude-...')`` triggers lazy default
    registration; the AnthropicLLMBackend instance is returned even
    without an explicit register call in the test.
    """
    # Test-isolation note: this test relies on either the module-load
    # state OR a previous test having triggered _ensure_default_backends.
    # Calling find_backend_for_model is itself the trigger; that's the
    # public-surface guarantee we're testing.
    from atomic_agents.llm import find_backend_for_model

    b = find_backend_for_model("claude-opus-4-7")
    assert b.provider_id == "anthropic"
    assert isinstance(b, AnthropicLLMBackend)

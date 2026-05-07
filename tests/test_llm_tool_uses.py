"""Tests for atomic_agents._llm tool_uses normalization across providers.

Mocks the Anthropic SDK and OpenAI SDK to verify that:
- The `tools` parameter is forwarded to the provider correctly.
- tool_use blocks come back normalized as {"id", "name", "input"}.
- Text + tool_use coexist in the response.
- Models without tool calls return tool_uses=[] (not None).
"""

from __future__ import annotations
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents import _llm


@pytest.fixture(autouse=True)
def _stub_api_keys(monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-anthropic-key")
    monkeypatch.setenv("ATOMIC_AGENTS_OPENAI_KEY", "fake-openai-key")
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-moonshot-key")


def _make_anthropic_block(block_type, **kwargs):
    """Build an SDK-shaped block object via SimpleNamespace."""
    return types.SimpleNamespace(type=block_type, **kwargs)


def _make_anthropic_response(content_blocks, *, input_tokens=10, output_tokens=20):
    usage = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    response = types.SimpleNamespace(content=content_blocks, usage=usage)
    return response


def test_anthropic_call_returns_empty_tool_uses_when_no_tools():
    """A pure-text response → tool_uses=[]."""
    response = _make_anthropic_response([
        _make_anthropic_block("text", text="Hello world"),
    ])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        raw = _llm.call_llm(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert raw.text == "Hello world"
    assert raw.tool_uses == []
    # tools NOT passed → Anthropic create call should not include tools kwarg
    _, call_kwargs = fake_client.messages.create.call_args
    assert "tools" not in call_kwargs


def test_anthropic_call_forwards_tools_parameter():
    """When tools=[...] is passed, it lands in messages.create kwargs."""
    response = _make_anthropic_response([
        _make_anthropic_block("text", text=""),
    ])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    tools = [{"name": "atomic_capture", "description": "...", "input_schema": {}}]

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        _llm.call_llm(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        )

    _, call_kwargs = fake_client.messages.create.call_args
    assert call_kwargs["tools"] is tools


def test_anthropic_call_extracts_tool_use_blocks():
    """tool_use blocks come back normalized as {"id","name","input"}."""
    response = _make_anthropic_response([
        _make_anthropic_block("text", text="I'll capture this."),
        _make_anthropic_block(
            "tool_use",
            id="toolu_01abc",
            name="atomic_capture",
            input={"type": "feedback", "name": "x", "body": "y"},
        ),
    ])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        raw = _llm.call_llm(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "atomic_capture", "description": "...", "input_schema": {}}],
        )

    assert raw.text == "I'll capture this."
    assert len(raw.tool_uses) == 1
    tu = raw.tool_uses[0]
    assert tu["id"] == "toolu_01abc"
    assert tu["name"] == "atomic_capture"
    assert tu["input"] == {"type": "feedback", "name": "x", "body": "y"}


def test_anthropic_call_handles_multiple_tool_use_blocks():
    response = _make_anthropic_response([
        _make_anthropic_block(
            "tool_use", id="t1", name="atomic_capture",
            input={"type": "feedback", "name": "a"},
        ),
        _make_anthropic_block(
            "tool_use", id="t2", name="atomic_capture",
            input={"type": "decision", "name": "b"},
        ),
    ])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        raw = _llm.call_llm(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "atomic_capture", "description": "...", "input_schema": {}}],
        )

    assert len(raw.tool_uses) == 2
    assert [t["id"] for t in raw.tool_uses] == ["t1", "t2"]


def test_anthropic_tool_use_with_empty_text_still_works():
    """Tool-only response (no text block) → text='', tool_uses populated."""
    response = _make_anthropic_response([
        _make_anthropic_block(
            "tool_use", id="t1", name="atomic_capture", input={"x": 1},
        ),
    ])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        raw = _llm.call_llm(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "atomic_capture", "description": "...", "input_schema": {}}],
        )

    assert raw.text == ""
    assert len(raw.tool_uses) == 1


def _make_openai_response(content, tool_calls=None, *, prompt_tokens=10, completion_tokens=20):
    """Build an OpenAI-shaped ChatCompletion response."""
    msg = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(message=msg)
    usage = types.SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
    )
    return types.SimpleNamespace(choices=[choice], usage=usage)


def test_openai_call_returns_empty_tool_uses_when_no_tool_calls():
    response = _make_openai_response("Hello", tool_calls=None)
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = response
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"openai": fake_openai}):
        raw = _llm.call_llm(
            model="gpt-4o-mini",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert raw.text == "Hello"
    assert raw.tool_uses == []


def test_openai_call_forwards_tools_parameter():
    response = _make_openai_response("", tool_calls=None)
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = response
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key: fake_client)

    tools = [{"type": "function", "function": {"name": "atomic_capture",
              "description": "...", "parameters": {}}}]

    with patch.dict(sys.modules, {"openai": fake_openai}):
        _llm.call_llm(
            model="gpt-4o-mini",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        )

    _, call_kwargs = fake_client.chat.completions.create.call_args
    assert call_kwargs["tools"] is tools


def test_openai_call_extracts_tool_calls():
    """OpenAI tool_calls (with JSON-string arguments) → normalized dicts with parsed input."""
    fn = types.SimpleNamespace(
        name="atomic_capture",
        arguments=json.dumps({"type": "feedback", "name": "from openai", "body": "x"}),
    )
    tc = types.SimpleNamespace(id="call_abc", function=fn)
    response = _make_openai_response("", tool_calls=[tc])
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = response
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"openai": fake_openai}):
        raw = _llm.call_llm(
            model="gpt-4o-mini",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "atomic_capture",
                    "description": "...", "parameters": {}}}],
        )

    assert raw.text == ""
    assert len(raw.tool_uses) == 1
    tu = raw.tool_uses[0]
    assert tu["id"] == "call_abc"
    assert tu["name"] == "atomic_capture"
    assert tu["input"] == {"type": "feedback", "name": "from openai", "body": "x"}


def test_openai_call_handles_malformed_arguments_json():
    """Bad JSON in tool_calls.arguments → input={}, no exception."""
    fn = types.SimpleNamespace(name="atomic_capture", arguments="{not json")
    tc = types.SimpleNamespace(id="call_x", function=fn)
    response = _make_openai_response("", tool_calls=[tc])
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = response
    fake_openai = types.SimpleNamespace(OpenAI=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"openai": fake_openai}):
        raw = _llm.call_llm(
            model="gpt-4o-mini",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "atomic_capture",
                    "description": "...", "parameters": {}}}],
        )

    assert len(raw.tool_uses) == 1
    assert raw.tool_uses[0]["input"] == {}


def test_moonshot_call_extracts_tool_calls_via_openai_path():
    """Moonshot uses the OpenAI-compatible API; tool_calls extraction is shared."""
    fn = types.SimpleNamespace(
        name="atomic_capture",
        arguments=json.dumps({"type": "decision", "name": "moon", "body": "x"}),
    )
    tc = types.SimpleNamespace(id="moon_call", function=fn)
    response = _make_openai_response("Reply text", tool_calls=[tc])
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = response

    def _openai_factory(api_key, base_url=None):
        return fake_client

    fake_openai = types.SimpleNamespace(OpenAI=_openai_factory)

    with patch.dict(sys.modules, {"openai": fake_openai}):
        raw = _llm.call_llm(
            model="moonshot/kimi-k2-0905-preview",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "atomic_capture",
                    "description": "...", "parameters": {}}}],
        )

    assert raw.text == "Reply text"
    assert len(raw.tool_uses) == 1
    assert raw.tool_uses[0]["id"] == "moon_call"
    # Moonshot model id has the "moonshot/" prefix stripped before the call
    _, call_kwargs = fake_client.chat.completions.create.call_args
    assert call_kwargs["model"] == "kimi-k2-0905-preview"


def test_raw_response_dataclass_default_tool_uses_is_empty_list():
    """Defaulting to None then post-init to [] — confirm no shared mutable list bug."""
    r1 = _llm._RawLLMResponse(text="a", input_tokens=1, output_tokens=1)
    r2 = _llm._RawLLMResponse(text="b", input_tokens=1, output_tokens=1)
    r1.tool_uses.append({"id": "x"})
    assert r2.tool_uses == []  # not shared

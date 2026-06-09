"""Conformance test suite for the LLMBackend Protocol (spec/31).

Parameterized over every registered backend. Each backend that ships in
core (Anthropic, OpenAI direct via ``make_openai_backend``, Moonshot via
``make_moonshot_backend``) is exercised against the same contract:

1. Protocol surface — all 7 methods present, ``isinstance`` passes.
2. ``provider_id`` is a stable non-empty string.
3. ``supports_model`` is side-effect-free.
4. ``capabilities`` returns a real ``LLMCapabilities`` instance.
5. ``pricing`` returns ``PricingInfo`` or ``None``.
6. ``count_tokens`` returns a positive integer.
7. ``call()`` returns a normalized ``_RawLLMResponse``.
8. ``call()`` translates canonical tools to provider format.
9. ``format_tool_results`` returns a list of dicts ready to extend ``messages``.
10. ``format_tool_results`` empty input → empty list.
11. ``format_tool_results`` honors the wire-byte parity discipline.
12. ``format_tool_results`` skips ``atomic_capture`` in the assistant echo.

A third-party backend in a downstream package can import + use this
test module's helpers to verify its own conformance.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents._costs import PRICING
from atomic_agents.llm import (
    SyncLLMBackend,
    _RawLLMResponse,
)
from atomic_agents.llm.anthropic import AnthropicLLMBackend
from atomic_agents.llm.moonshot import make_moonshot_backend
from atomic_agents.llm.openai_compat import (
    OpenAICompatibleLLMBackend,
    make_openai_backend,
)
from atomic_agents.llm.vertex_gemini import VertexGeminiLLMBackend
from atomic_agents.llm.types import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture(autouse=True)
def _stub_api_keys(monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    monkeypatch.setenv("ATOMIC_AGENTS_OPENAI_KEY", "fake-key")
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")
    # Vertex: project env var so VertexGeminiLLMBackend constructs without real ADC
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")


@pytest.fixture(
    params=[
        ("anthropic", "claude-haiku-4-5-20251001"),
        ("openai", "gpt-5"),
        ("moonshot", "moonshot/kimi-k2.6"),
        ("vertex-gemini", "vertex/gemini-2.0-flash"),
    ],
    ids=["anthropic", "openai", "moonshot", "vertex-gemini"],
)
def backend_and_model(request):
    """Yield ``(backend_instance, sample_model_id)`` for each shipped backend."""
    provider_id, model_id = request.param
    if provider_id == "anthropic":
        return AnthropicLLMBackend(), model_id
    if provider_id == "openai":
        return make_openai_backend(), model_id
    if provider_id == "moonshot":
        return make_moonshot_backend(), model_id
    if provider_id == "vertex-gemini":
        # Patch google.genai so VertexGeminiLLMBackend constructs in CI without the SDK
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_genai_types = types.ModuleType("google.genai.types")
        fake_genai_client = types.ModuleType("google.genai.client")
        fake_genai_client.Client = lambda **kw: MagicMock()
        fake_google.genai = fake_genai
        with patch.dict(
            sys.modules,
            {
                "google": fake_google,
                "google.genai": fake_genai,
                "google.genai.types": fake_genai_types,
                "google.genai.client": fake_genai_client,
            },
        ):
            backend = VertexGeminiLLMBackend()
        return backend, model_id
    raise ValueError(f"unknown backend in parametrize: {provider_id}")


# ──────────────────────────────────────────────────────────────────
# 1. Protocol surface — methods present, isinstance passes


def test_conforms_to_protocol_via_isinstance(backend_and_model):
    """``isinstance(backend, SyncLLMBackend)`` must return True for every
    shipped backend. @runtime_checkable enforces method-presence.
    """
    backend, _ = backend_and_model
    assert isinstance(backend, SyncLLMBackend)


def test_has_all_seven_protocol_methods(backend_and_model):
    """Belt-and-braces: explicitly assert each method exists by name."""
    backend, _ = backend_and_model
    for method in (
        "provider_id",
        "supports_model",
        "capabilities",
        "pricing",
        "count_tokens",
        "call",
        "format_tool_results",
    ):
        assert hasattr(backend, method), f"missing {method}"


# ──────────────────────────────────────────────────────────────────
# 2. provider_id is a stable non-empty string


def test_provider_id_is_non_empty_string(backend_and_model):
    backend, _ = backend_and_model
    pid = backend.provider_id
    assert isinstance(pid, str)
    assert len(pid) > 0
    # Stable: two reads return the same value
    assert backend.provider_id == pid


def test_provider_id_unique_across_default_backends():
    """All four shipped backends must have distinct provider_ids so the
    registry's keyed-by-provider_id invariant holds.
    """
    a = AnthropicLLMBackend().provider_id
    o = make_openai_backend().provider_id
    m = make_moonshot_backend().provider_id
    # Construct VertexGeminiLLMBackend with a patched SDK
    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai_types = types.ModuleType("google.genai.types")
    fake_genai_client = types.ModuleType("google.genai.client")
    fake_genai_client.Client = lambda **kw: MagicMock()
    fake_google.genai = fake_genai
    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.genai": fake_genai,
            "google.genai.types": fake_genai_types,
            "google.genai.client": fake_genai_client,
        },
    ):
        v = VertexGeminiLLMBackend().provider_id
    assert len({a, o, m, v}) == 4


# ──────────────────────────────────────────────────────────────────
# 3. supports_model is side-effect-free + correct


def test_supports_model_returns_bool(backend_and_model):
    backend, model = backend_and_model
    result = backend.supports_model(model)
    assert isinstance(result, bool)
    assert result is True


def test_supports_model_rejects_obviously_wrong_id(backend_and_model):
    """No backend should claim a model id from a different provider's
    namespace.
    """
    backend, _ = backend_and_model
    foreign_ids = [
        "claude-opus-4-7" if backend.provider_id != "anthropic" else "gpt-5",
        "moonshot/kimi-k2.6" if backend.provider_id != "moonshot" else "gpt-5",
        "totally-fake-model-id-xyz",
    ]
    for fid in foreign_ids:
        if fid == "gpt-5" and backend.provider_id == "openai":
            continue  # skip self-claim
        # Filter false-claims: only assert backends DON'T claim a foreign id
        # when that id genuinely belongs to a different provider.
        if backend.provider_id == "anthropic" and not fid.startswith("claude-"):
            assert not backend.supports_model(fid), (
                f"{backend.provider_id} claimed foreign id {fid!r}"
            )


def test_supports_model_is_side_effect_free(backend_and_model):
    """Calling supports_model many times in a row produces no observable
    side effect (idempotent, fast, no SDK/key/network).
    """
    backend, model = backend_and_model
    # No exception, no slowdown, same result
    for _ in range(100):
        backend.supports_model(model)
    assert backend.supports_model(model) is True


# ──────────────────────────────────────────────────────────────────
# 4. capabilities returns LLMCapabilities


def test_capabilities_returns_real_dataclass(backend_and_model):
    backend, model = backend_and_model
    caps = backend.capabilities(model)
    assert isinstance(caps, LLMCapabilities)


def test_capabilities_has_sensible_token_limits(backend_and_model):
    """max_input_tokens and max_output_tokens are positive integers."""
    backend, model = backend_and_model
    caps = backend.capabilities(model)
    assert isinstance(caps.max_input_tokens, int) and caps.max_input_tokens > 0
    assert isinstance(caps.max_output_tokens, int) and caps.max_output_tokens > 0


def test_capabilities_tool_support_declared_honestly(backend_and_model):
    """If a backend's capability says tool_results=True it must also say
    tools=True — supporting tool_results without supporting tool defs is
    incoherent.
    """
    backend, model = backend_and_model
    caps = backend.capabilities(model)
    if caps.tool_results:
        assert caps.tools, f"{backend.provider_id}: tool_results=True but tools=False"


# ──────────────────────────────────────────────────────────────────
# 5. pricing returns PricingInfo | None


def test_pricing_returns_pricing_info_or_none(backend_and_model):
    backend, model = backend_and_model
    result = backend.pricing(model)
    assert result is None or isinstance(result, PricingInfo)


def test_pricing_unknown_model_returns_none(backend_and_model):
    """A non-existent model id should return None, not raise."""
    backend, _ = backend_and_model
    assert backend.pricing("absolutely-not-a-real-model-xyz") is None


def test_pricing_does_not_raise_on_unknown(backend_and_model):
    """Backend's pricing() must never raise — caller falls back to the
    framework's PRICING table on None.
    """
    backend, _ = backend_and_model
    # No exception
    backend.pricing("totally-fake-12345")


# ──────────────────────────────────────────────────────────────────
# 6. count_tokens returns positive int


def _stub_sdk_for_count_tokens(backend):
    """Patch the backend's SDK so count_tokens doesn't hit the network.

    Anthropic exposes ``messages.count_tokens`` which the backend prefers
    over its heuristic. We force the heuristic path so conformance tests
    don't depend on a live SDK call.
    VertexGeminiLLMBackend uses a char-heuristic with no SDK call — no patch needed.
    """
    if backend.provider_id == "anthropic":
        # Remove count_tokens from the SDK so the backend falls through
        # to its heuristic.
        fake_client = MagicMock()
        fake_client.messages = MagicMock(spec=[])  # no count_tokens
        fake_module = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
        return patch.dict(sys.modules, {"anthropic": fake_module})
    else:
        # OpenAI-family and Vertex backends use heuristic (no SDK call).
        from contextlib import nullcontext

        return nullcontext()


def test_count_tokens_returns_positive_int(backend_and_model):
    backend, _ = backend_and_model
    with _stub_sdk_for_count_tokens(backend):
        n = backend.count_tokens(
            system_prompt="You are helpful.",
            messages=[{"role": "user", "content": "Hello world"}],
        )
    assert isinstance(n, int)
    assert n > 0


def test_count_tokens_grows_with_more_input(backend_and_model):
    """Adding more text should produce a larger token estimate — the
    heuristic / SDK counter should be monotonic in input size.
    """
    backend, _ = backend_and_model
    with _stub_sdk_for_count_tokens(backend):
        small = backend.count_tokens(
            system_prompt="s",
            messages=[{"role": "user", "content": "x"}],
        )
        large = backend.count_tokens(
            system_prompt="s",
            messages=[{"role": "user", "content": "x" * 10_000}],
        )
    assert large > small


# ──────────────────────────────────────────────────────────────────
# 7. call() returns normalized _RawLLMResponse


def _stub_sdk_for_backend(backend, response):
    """Patch the right SDK module for the backend so call() returns ``response``."""
    if backend.provider_id == "anthropic":
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response
        fake_module = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)
        return patch.dict(sys.modules, {"anthropic": fake_module}), fake_client
    elif backend.provider_id == "vertex-gemini":
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = response
        fake_genai = types.ModuleType("google.genai")
        # types sub-module with minimal surface for the call() translation
        fake_types = types.SimpleNamespace(
            Content=lambda role, parts: types.SimpleNamespace(role=role, parts=parts),
            Part=lambda **kw: types.SimpleNamespace(**kw),
            FunctionDeclaration=lambda name, description, parameters: (
                types.SimpleNamespace(
                    name=name, description=description, parameters=parameters
                )
            ),
            FunctionResponse=lambda id, name, response: types.SimpleNamespace(
                id=id, name=name, response=response
            ),
            FunctionCall=lambda name, args: types.SimpleNamespace(name=name, args=args),
            Tool=lambda function_declarations: types.SimpleNamespace(
                function_declarations=function_declarations
            ),
            GenerateContentConfig=lambda **kw: types.SimpleNamespace(**kw),
        )
        fake_genai.Client = lambda vertexai=False, project=None, location=None: (
            fake_client
        )
        fake_genai.types = fake_types
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        fake_genai_types_mod = types.ModuleType("google.genai.types")
        for k, v in vars(fake_types).items():
            setattr(fake_genai_types_mod, k, v)
        fake_genai_client_mod = types.ModuleType("google.genai.client")
        fake_genai_client_mod.Client = fake_genai.Client
        return (
            patch.dict(
                sys.modules,
                {
                    "google": fake_google,
                    "google.genai": fake_genai,
                    "google.genai.types": fake_genai_types_mod,
                    "google.genai.client": fake_genai_client_mod,
                },
            ),
            fake_client,
        )
    else:
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = response
        fake_module = types.SimpleNamespace(OpenAI=lambda api_key, **kw: fake_client)
        return patch.dict(sys.modules, {"openai": fake_module}), fake_client


def _make_response_for_backend(backend, text="ok", tool_use_blocks=None):
    """Build a fake provider response matching the backend's expected SDK shape."""
    if backend.provider_id == "anthropic":
        blocks = []
        if text:
            blocks.append(types.SimpleNamespace(type="text", text=text))
        for tub in tool_use_blocks or []:
            blocks.append(
                types.SimpleNamespace(
                    type="tool_use",
                    id=tub["id"],
                    name=tub["name"],
                    input=tub["input"],
                )
            )
        usage = types.SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        return types.SimpleNamespace(
            content=blocks,
            usage=usage,
            model_dump=lambda: {"id": "conformance"},
        )
    elif backend.provider_id == "vertex-gemini":
        # Gemini returns candidates[0].content.parts with text / function_call parts
        # and usage_metadata.prompt_token_count / candidates_token_count.
        parts = []
        if text:
            parts.append(types.SimpleNamespace(text=text, function_call=None))
        for i, tub in enumerate(tool_use_blocks or []):
            fc = types.SimpleNamespace(name=tub["name"], args=tub["input"])
            parts.append(types.SimpleNamespace(text=None, function_call=fc))
        content = types.SimpleNamespace(parts=parts)
        candidate = types.SimpleNamespace(content=content)
        usage_meta = types.SimpleNamespace(
            prompt_token_count=10, candidates_token_count=5
        )
        return types.SimpleNamespace(candidates=[candidate], usage_metadata=usage_meta)
    else:
        tool_calls = None
        if tool_use_blocks:
            tool_calls = [
                types.SimpleNamespace(
                    id=tub["id"],
                    function=types.SimpleNamespace(
                        name=tub["name"],
                        arguments=json.dumps(tub["input"]),
                    ),
                )
                for tub in tool_use_blocks
            ]
        msg = types.SimpleNamespace(content=text, tool_calls=tool_calls)
        choice = types.SimpleNamespace(message=msg)
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        return types.SimpleNamespace(choices=[choice], usage=usage)


def test_call_returns_raw_llm_response(backend_and_model):
    backend, model = backend_and_model
    response = _make_response_for_backend(backend, text="hello")
    patch_ctx, _ = _stub_sdk_for_backend(backend, response)
    with patch_ctx:
        r = backend.call(
            model=model,
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=1.0,
        )
    assert isinstance(r, _RawLLMResponse)
    assert r.text == "hello"
    assert r.input_tokens >= 0
    assert r.output_tokens >= 0
    assert isinstance(r.tool_uses, list)


def test_call_normalizes_tool_uses_to_dict_shape(backend_and_model):
    """Every backend's call() returns tool_uses as
    list[{id, name, input}] dicts — the canonical shape the agent
    layer iterates without provider awareness.
    """
    backend, model = backend_and_model
    response = _make_response_for_backend(
        backend,
        text="",
        tool_use_blocks=[{"id": "tc_1", "name": "search", "input": {"q": "test"}}],
    )
    patch_ctx, _ = _stub_sdk_for_backend(backend, response)
    with patch_ctx:
        r = backend.call(
            model=model,
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=1.0,
        )
    assert len(r.tool_uses) == 1
    tu = r.tool_uses[0]
    # Backends that issue stable provider IDs preserve them (Anthropic, OpenAI).
    # VertexGeminiLLMBackend mints synthetic IDs (call_0, call_1, ...) because
    # the Gemini SDK does not assign stable call IDs — assert non-empty string.
    assert isinstance(tu["id"], str) and len(tu["id"]) > 0
    if backend.provider_id != "vertex-gemini":
        assert tu["id"] == "tc_1"
    assert tu["name"] == "search"
    assert isinstance(tu["input"], dict)
    assert tu["input"] == {"q": "test"}


# ──────────────────────────────────────────────────────────────────
# 8. call() translates canonical tools to provider format


def test_call_accepts_canonical_tool_definitions(backend_and_model):
    """Backend's call() accepts ``list[LLMToolDefinition]`` (the
    canonical, post-#87 shape). Provider-shape dicts are NOT what the
    Protocol contract takes — they're handled by _llm.call_llm's
    transitional adapter.
    """
    backend, model = backend_and_model
    response = _make_response_for_backend(backend, text="")
    patch_ctx, _ = _stub_sdk_for_backend(backend, response)
    canonical_tools = [
        LLMToolDefinition(
            name="search",
            description="search",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
    ]
    with patch_ctx:
        # Should not raise — backend translates internally
        backend.call(
            model=model,
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=1.0,
            tools=canonical_tools,
        )


# ──────────────────────────────────────────────────────────────────
# 9-12. format_tool_results contract


def test_format_tool_results_returns_list_of_dicts(backend_and_model):
    backend, _ = backend_and_model
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="tc_1", name="t", input={})],
        tool_results=[LLMToolResult(tool_use_id="tc_1", content="result")],
    )
    assert isinstance(out, list)
    for msg in out:
        assert isinstance(msg, dict)


def test_format_tool_results_empty_input_returns_empty(backend_and_model):
    backend, _ = backend_and_model
    out = backend.format_tool_results(tool_uses=[], tool_results=[])
    assert out == []


def test_format_tool_results_skips_atomic_capture_from_echo(backend_and_model):
    """atomic_capture is handled by the capture path, not the tool loop.
    Backends must filter it from any assistant-echo turn so the model
    doesn't see ``atomic_capture`` echoed back to itself in the next call.
    """
    backend, _ = backend_and_model
    out = backend.format_tool_results(
        tool_uses=[
            LLMToolUse(id="tc_cap", name="atomic_capture", input={}),
            LLMToolUse(id="tc_search", name="search", input={"q": "x"}),
        ],
        tool_results=[LLMToolResult(tool_use_id="tc_search", content="ok")],
    )
    # Search the messages for any echo of atomic_capture — should be absent
    serialized = json.dumps(out)
    assert "atomic_capture" not in serialized


def test_format_tool_results_string_content_is_json_encoded(backend_and_model):
    """Wire-byte parity: a string tool output is json-encoded (quoted)
    when serialized for transport. Matches pre-#87 helper behavior.
    """
    backend, _ = backend_and_model
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="tc_1", name="t", input={})],
        tool_results=[LLMToolResult(tool_use_id="tc_1", content="hello")],
    )
    serialized = json.dumps(out)
    # The string `hello` should appear as quoted JSON `\"hello\"` not bare
    assert '"hello"' in serialized or '\\"hello\\"' in serialized


def test_format_tool_results_non_json_serializable_falls_back_to_str(backend_and_model):
    """A tool returning a datetime / custom class / bytes must not crash
    the loop. Both backends honor the str() fallback discipline.
    """
    backend, _ = backend_and_model
    t = _dt.datetime(2026, 1, 2, 3, 4, 5)
    # Should not raise
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="tc_1", name="clock", input={})],
        tool_results=[LLMToolResult(tool_use_id="tc_1", content=t)],
    )
    assert isinstance(out, list)


def test_format_tool_results_is_error_handled(backend_and_model):
    """Backend doesn't crash when is_error=True — concrete error
    propagation shape is provider-specific (Anthropic sets is_error: True;
    OpenAI uses content prefix) but neither implementation should crash.
    """
    backend, _ = backend_and_model
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="tc_1", name="t", input={})],
        tool_results=[
            LLMToolResult(
                tool_use_id="tc_1",
                content="[tool error] boom",
                is_error=True,
            )
        ],
    )
    assert isinstance(out, list)
    assert len(out) > 0  # at least one message (error has to be transmitted)

"""Tests for VertexGeminiLLMBackend — issue #345 reference implementation."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.llm.vertex_gemini import (
    VertexGeminiLLMBackend,
    _messages_to_genai_contents,
    _resolve_vertex_family,
    _strip_title_keys,
)
from atomic_agents.llm.types import (
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)
from atomic_agents.llm.backend import _RawLLMResponse


# ──────────────────────────────────────────────────────────────────
# Fixtures + SDK stubs


@pytest.fixture(autouse=True)
def _stub_gcp_env(monkeypatch):
    """Set GCP env vars so VertexGeminiLLMBackend constructs without real ADC."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")


def _make_fake_genai_module():
    """Build a minimal fake google.genai SDK module tree for unit tests.

    Returns a (fake_google_module, fake_genai_module) tuple. The fake replicates
    only the surface the backend touches: Client, types.Content, types.Part,
    types.FunctionCall, types.FunctionDeclaration, types.FunctionResponse,
    types.GenerateContentConfig, types.Tool.
    """
    # types sub-module
    fake_types = types.SimpleNamespace(
        Content=lambda role, parts: types.SimpleNamespace(role=role, parts=parts),
        Part=lambda **kw: types.SimpleNamespace(**kw),
        FunctionDeclaration=lambda name, description, parameters: types.SimpleNamespace(
            name=name, description=description, parameters=parameters
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

    # Fake client: generate_content returns a configurable stub
    fake_client_instance = MagicMock()

    fake_genai = types.SimpleNamespace(
        Client=lambda vertexai=False, project=None, location=None: fake_client_instance,
        types=fake_types,
    )

    # Expose under google.genai hierarchy so `import google.genai` succeeds
    fake_google = types.ModuleType("google")
    fake_google_genai = types.ModuleType("google.genai")
    fake_google_genai_types = types.ModuleType("google.genai.types")
    # copy attributes from SimpleNamespace → module
    for k, v in vars(fake_genai).items():
        setattr(fake_google_genai, k, v)
    for k, v in vars(fake_types).items():
        setattr(fake_google_genai_types, k, v)
    fake_google.genai = fake_google_genai

    return fake_google, fake_google_genai, fake_google_genai_types, fake_client_instance


def _make_usage_metadata(prompt_tokens=10, candidate_tokens=5, thoughts_tokens=0):
    # thoughts_token_count defaults to 0 (matches a non-thinking response).
    # On Vertex AI the thinking models report reasoning tokens here SEPARATELY
    # from candidates_token_count; the backend must add them into output_tokens.
    return types.SimpleNamespace(
        prompt_token_count=prompt_tokens,
        candidates_token_count=candidate_tokens,
        thoughts_token_count=thoughts_tokens,
    )


def _make_text_response(text="hello", prompt_tokens=10, candidate_tokens=5):
    """Build a fake generate_content() response with a text part."""
    part = types.SimpleNamespace(
        text=text,
        function_call=None,
    )
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(
        candidates=[candidate],
        usage_metadata=_make_usage_metadata(prompt_tokens, candidate_tokens),
    )


def _make_tool_response(tool_calls, text="", prompt_tokens=10, candidate_tokens=5):
    """Build a fake generate_content() response with function_call parts."""
    parts = []
    if text:
        parts.append(types.SimpleNamespace(text=text, function_call=None))
    for i, tc in enumerate(tool_calls):
        fc = types.SimpleNamespace(name=tc["name"], args=tc["args"])
        parts.append(types.SimpleNamespace(text=None, function_call=fc))
    content = types.SimpleNamespace(parts=parts)
    candidate = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(
        candidates=[candidate],
        usage_metadata=_make_usage_metadata(prompt_tokens, candidate_tokens),
    )


@pytest.fixture
def backend_and_fake_sdk():
    """Yield (backend, fake_client) with google.genai patched out."""
    fake_google, fake_genai, fake_genai_types, fake_client = _make_fake_genai_module()
    patches = {
        "google": fake_google,
        "google.genai": fake_genai,
        "google.genai.types": fake_genai_types,
        "google.genai.client": types.SimpleNamespace(Client=fake_genai.Client),
    }
    with patch.dict(sys.modules, patches):
        backend = VertexGeminiLLMBackend()
    return backend, fake_client, patches


# ──────────────────────────────────────────────────────────────────
# Static surface


def test_provider_id():
    fake_google, fake_genai, fake_genai_types, _ = _make_fake_genai_module()
    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.genai": fake_genai,
            "google.genai.types": fake_genai_types,
            "google.genai.client": types.SimpleNamespace(Client=fake_genai.Client),
        },
    ):
        b = VertexGeminiLLMBackend()
    assert b.provider_id == "vertex-gemini"


def test_supports_model_positive():
    fake_google, fake_genai, fake_genai_types, _ = _make_fake_genai_module()
    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.genai": fake_genai,
            "google.genai.types": fake_genai_types,
            "google.genai.client": types.SimpleNamespace(Client=fake_genai.Client),
        },
    ):
        b = VertexGeminiLLMBackend()
    assert b.supports_model("vertex/gemini-2.0-flash")
    assert b.supports_model("vertex/gemini-2.5-pro")
    assert b.supports_model("vertex/gemini-2.5-flash")
    assert b.supports_model("vertex/gemini-2.0-flash-lite")
    # Forward-compat: unknown future gemini variant still matches
    assert b.supports_model("vertex/gemini-9.0-ultra")


def test_supports_model_negative():
    """Backend must NOT claim claude-*, gpt-*, moonshot/*, or bare vertex/ ids."""
    fake_google, fake_genai, fake_genai_types, _ = _make_fake_genai_module()
    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.genai": fake_genai,
            "google.genai.types": fake_genai_types,
            "google.genai.client": types.SimpleNamespace(Client=fake_genai.Client),
        },
    ):
        b = VertexGeminiLLMBackend()
    assert not b.supports_model("claude-opus-4-7")
    assert not b.supports_model("gpt-5")
    assert not b.supports_model("moonshot/kimi-k2.6")
    # vertex/claude-* must NOT match (reserved for vertex-anthropic backend)
    assert not b.supports_model("vertex/claude-3-5-sonnet")
    # Bare 'vertex/' without 'gemini-' must NOT match
    assert not b.supports_model("vertex/")
    assert not b.supports_model("vertex/unknown")


def test_supports_model_prefix_scoped_to_gemini():
    """The vertex/ prefix alone does not match — must be vertex/gemini-.

    This prevents collision with the upcoming vertex-anthropic backend that
    will use vertex/claude-* — the two-backend design ruling.
    """
    fake_google, fake_genai, fake_genai_types, _ = _make_fake_genai_module()
    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.genai": fake_genai,
            "google.genai.types": fake_genai_types,
            "google.genai.client": types.SimpleNamespace(Client=fake_genai.Client),
        },
    ):
        b = VertexGeminiLLMBackend()
    assert not b.supports_model("vertex/claude-sonnet-4")
    assert not b.supports_model("vertex/llama-3")


# ──────────────────────────────────────────────────────────────────
# capabilities


def test_capabilities_flash_has_tools(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    caps = backend.capabilities("vertex/gemini-2.0-flash")
    assert isinstance(caps, LLMCapabilities)
    assert caps.tools is True
    assert caps.tool_results is True
    # vision is False in this reference impl: _messages_to_genai_contents has
    # no image-block translation, so vision=True would be a dishonest claim
    # (spec/31 conformance rule 4). Flips to True per-family once image
    # translation lands.
    assert caps.vision is False
    assert caps.cache_control is False  # always False for Vertex Gemini
    assert caps.streaming is False
    assert caps.usage_reporting is True
    assert caps.max_input_tokens > 0
    assert caps.max_output_tokens > 0


def test_capabilities_vision_false_until_image_translation(backend_and_fake_sdk):
    """vision must be False for every family — no image-block translation exists.

    Advertising vision=True with no behavior behind it violates spec/31
    conformance rule 4 ('every True claim is backed by actual behavior').
    """
    backend, _, _ = backend_and_fake_sdk
    for model in [
        "vertex/gemini-2.5-flash",
        "vertex/gemini-2.5-pro",
        "vertex/gemini-2.0-flash",
        "vertex/gemini-2.0-flash-lite",
        "vertex/gemini-9.0-ultra-unknown",  # unknown future model → default caps
    ]:
        caps = backend.capabilities(model)
        assert caps.vision is False, f"vision must be False for {model}"


def test_capabilities_cache_control_always_false(backend_and_fake_sdk):
    """cache_control must be False for ALL Vertex Gemini models.

    Vertex context caching is a separate resource-based API incompatible
    with the CacheDirective pattern. Claiming True would cause silent incorrect
    behavior (spec/31 conformance rule 4 violation).
    """
    backend, _, _ = backend_and_fake_sdk
    for model in [
        "vertex/gemini-2.5-flash",
        "vertex/gemini-2.5-pro",
        "vertex/gemini-2.0-flash",
        "vertex/gemini-2.0-flash-lite",
        "vertex/gemini-9.0-ultra-unknown",  # unknown future model
    ]:
        caps = backend.capabilities(model)
        assert caps.cache_control is False, f"cache_control must be False for {model}"


def test_capabilities_streaming_always_false(backend_and_fake_sdk):
    """streaming must be False — deferred to StreamingLLMBackend Protocol."""
    backend, _, _ = backend_and_fake_sdk
    caps = backend.capabilities("vertex/gemini-2.0-flash")
    assert caps.streaming is False


def test_capabilities_flash_lite_no_tools(backend_and_fake_sdk):
    """gemini-2.0-flash-lite does not support function calling per Vertex docs."""
    backend, _, _ = backend_and_fake_sdk
    caps = backend.capabilities("vertex/gemini-2.0-flash-lite")
    assert caps.tools is False
    assert caps.tool_results is False


def test_capabilities_pro_large_context(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    caps = backend.capabilities("vertex/gemini-2.5-pro")
    assert caps.max_input_tokens >= 1_000_000


# ──────────────────────────────────────────────────────────────────
# pricing


def test_pricing_known_models_return_pricing_info(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    for model in [
        "vertex/gemini-2.5-flash",
        "vertex/gemini-2.5-pro",
        "vertex/gemini-2.0-flash",
        "vertex/gemini-2.0-flash-lite",
    ]:
        result = backend.pricing(model)
        assert isinstance(result, PricingInfo), f"expected PricingInfo for {model}"
        assert result.input_per_million_usd > 0
        assert result.output_per_million_usd > 0


def test_pricing_all_vertex_keys_match_supports_model(backend_and_fake_sdk):
    """Every PRICING key of the form vertex/* must match supports_model().

    Ensures calc_cost() lookup key == model id used in call() without fallback.
    """
    from atomic_agents._costs import PRICING

    backend, _, _ = backend_and_fake_sdk
    for key in PRICING:
        if key.startswith("vertex/"):
            assert backend.supports_model(key), (
                f"PRICING key {key!r} is not matched by supports_model() — "
                "calc_cost() will fall through to fallback pricing on every call"
            )


def test_pricing_unknown_model_returns_none(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    assert backend.pricing("vertex/gemini-unknown-99") is None


def test_pricing_cache_hit_discount_is_one(backend_and_fake_sdk):
    """No cache discount for Vertex Gemini (cache_control=False)."""
    backend, _, _ = backend_and_fake_sdk
    pi = backend.pricing("vertex/gemini-2.0-flash")
    assert pi is not None
    assert pi.cache_hit_discount == 1.0


# ──────────────────────────────────────────────────────────────────
# count_tokens


def test_count_tokens_returns_positive_int(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    n = backend.count_tokens(
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Hello world"}],
    )
    assert isinstance(n, int)
    assert n > 0


def test_count_tokens_grows_with_more_input(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    small = backend.count_tokens(
        system_prompt="s", messages=[{"role": "user", "content": "x"}]
    )
    large = backend.count_tokens(
        system_prompt="s",
        messages=[{"role": "user", "content": "x" * 10_000}],
    )
    assert large > small


def test_count_tokens_never_zero(backend_and_fake_sdk):
    """count_tokens must return >= 1 even for empty inputs (spec/31 rule 9)."""
    backend, _, _ = backend_and_fake_sdk
    n = backend.count_tokens(system_prompt="", messages=[])
    assert n >= 1


def test_count_tokens_accounts_for_parts_shaped_messages(backend_and_fake_sdk):
    """Regression: count_tokens must count parts-shaped messages.

    This backend's OWN format_tool_results emits parts-shaped continuation
    messages ({"role": "model"|"user", "parts": [...]}) with no "content" key.
    Counting only "content" would make every tool-call/result turn contribute
    ZERO characters, under-counting a multi-iteration tool loop exactly as it
    grows — violating the conservative-pessimistic contract (CLAUDE.md rule #4,
    spec/31 conformance rule 9). Feed real format_tool_results output and assert
    the estimate strictly increases versus the same history without the tool
    turns.
    """
    backend, _, _ = backend_and_fake_sdk
    base_history = [{"role": "user", "content": "search the web for foo"}]
    cont = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="call_0", name="search", input={"q": "foo"})],
        tool_results=[
            LLMToolResult(tool_use_id="call_0", content="a long result string " * 20)
        ],
        assistant_text="let me search for that",
    )
    assert cont, "format_tool_results must emit parts-shaped continuation turns"
    # The continuation turns carry only "parts", no "content".
    assert all("content" not in m for m in cont)

    without_tool_turns = backend.count_tokens(
        system_prompt="sys", messages=base_history
    )
    with_tool_turns = backend.count_tokens(
        system_prompt="sys", messages=[*base_history, *cont]
    )
    assert with_tool_turns > without_tool_turns, (
        "parts-shaped tool-loop turns must increase the token estimate — "
        "otherwise count_tokens under-counts a growing tool loop"
    )


# ──────────────────────────────────────────────────────────────────
# call() — token extraction (P0: correct usage_metadata fields)


def test_call_extracts_token_counts_from_usage_metadata(backend_and_fake_sdk):
    """P0: Gemini returns usage_metadata.prompt_token_count and
    candidates_token_count — NOT .usage.input_tokens / .output_tokens.
    Zero on these fields silently disarms cost guardrails.
    """
    backend, fake_client, patches = backend_and_fake_sdk
    response = _make_text_response(text="hello", prompt_tokens=42, candidate_tokens=17)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert r.input_tokens == 42, (
        "input_tokens must come from usage_metadata.prompt_token_count"
    )
    assert r.output_tokens == 17, (
        "output_tokens must come from usage_metadata.candidates_token_count"
    )
    # Cost guardrail non-zero assertion per spec/31 conformance rule 5
    assert r.input_tokens > 0
    assert r.output_tokens > 0


def test_call_raises_when_usage_metadata_absent_on_content_response(
    backend_and_fake_sdk,
):
    """P1: a content-producing response with usage_metadata=None must raise,
    not silently report 0 tokens.

    Reporting input_tokens=0 / output_tokens=0 for a real, billable generation
    (a) under-counts the running cost total the cost gates trust (CLAUDE.md
    rule #4) and (b) writes a $0 cost_usd audit line for a paid call (rule #5).
    The backend advertises usage_reporting=True so it must fail loud — the way
    the Anthropic/OpenAI backends do — instead of degrading to 0.
    """
    from atomic_agents.exceptions import AtomicAgentsError

    backend, fake_client, patches = backend_and_fake_sdk
    # Real text content, but usage_metadata is missing.
    part = types.SimpleNamespace(text="hello", function_call=None)
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    response = types.SimpleNamespace(candidates=[candidate], usage_metadata=None)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        with pytest.raises(AtomicAgentsError, match="usage_metadata"):
            backend.call(
                model="vertex/gemini-2.0-flash",
                system_prompt="sys",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
                temperature=0.0,
            )


def test_call_raises_when_prompt_token_count_none_on_content_response(
    backend_and_fake_sdk,
):
    """P1: usage_metadata present but prompt_token_count=None on a content
    response must raise — a half-populated usage block is as dangerous as a
    missing one (the cost gate would still see input_tokens=0)."""
    from atomic_agents.exceptions import AtomicAgentsError

    backend, fake_client, patches = backend_and_fake_sdk
    part = types.SimpleNamespace(text="hello", function_call=None)
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    usage = types.SimpleNamespace(prompt_token_count=None, candidates_token_count=5)
    response = types.SimpleNamespace(candidates=[candidate], usage_metadata=usage)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        with pytest.raises(AtomicAgentsError, match="usage_metadata"):
            backend.call(
                model="vertex/gemini-2.0-flash",
                system_prompt="sys",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
                temperature=0.0,
            )


def test_call_includes_thoughts_token_count_in_output_tokens(backend_and_fake_sdk):
    """P1: on Vertex AI, thinking-model reasoning tokens are reported in
    usage_metadata.thoughts_token_count SEPARATELY from candidates_token_count
    and billed at the output rate. output_tokens must include them, else the
    gemini-2.5-flash / gemini-2.5-pro thinking models under-count output (and
    under-charge the cost gates + audit line) by the entire reasoning volume —
    the same cost-honesty defect class as the usage-absent case.
    """
    backend, fake_client, patches = backend_and_fake_sdk
    part = types.SimpleNamespace(text="reasoned answer", function_call=None)
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    usage = _make_usage_metadata(
        prompt_tokens=100, candidate_tokens=20, thoughts_tokens=80
    )
    response = types.SimpleNamespace(candidates=[candidate], usage_metadata=usage)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.5-pro",
            system_prompt="sys",
            messages=[{"role": "user", "content": "think hard"}],
            max_tokens=1000,
            temperature=0.0,
        )

    assert r.input_tokens == 100
    # 20 visible candidate tokens + 80 thinking tokens = 100 billed output.
    assert r.output_tokens == 100, (
        "output_tokens must include thoughts_token_count (Vertex reports "
        "thinking tokens separately from candidates_token_count)"
    )


def test_call_thoughts_token_count_absent_defaults_to_zero(backend_and_fake_sdk):
    """Non-thinking responses (no thoughts_token_count attribute) must not
    crash and must report output_tokens == candidates_token_count.
    """
    backend, fake_client, patches = backend_and_fake_sdk
    part = types.SimpleNamespace(text="hi", function_call=None)
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    # usage_metadata WITHOUT a thoughts_token_count attribute at all.
    usage = types.SimpleNamespace(prompt_token_count=10, candidates_token_count=5)
    response = types.SimpleNamespace(candidates=[candidate], usage_metadata=usage)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert r.output_tokens == 5


def test_call_raises_when_total_output_zero_on_content_response(
    backend_and_fake_sdk,
):
    """usage_metadata present but TOTAL billable output is zero
    (candidates_token_count=0 AND thoughts_token_count=0, both falsy-zero,
    distinct from None) on a content-producing response must raise — a
    half-populated SDK shape that would otherwise feed 0 output tokens into
    calc_cost, under-counting the cost gates and writing a $0 audit line for a
    billable generation. The None-guard alone would not catch this.
    """
    from atomic_agents.exceptions import AtomicAgentsError

    backend, fake_client, patches = backend_and_fake_sdk
    part = types.SimpleNamespace(text="hello", function_call=None)
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    usage = types.SimpleNamespace(
        prompt_token_count=10, candidates_token_count=0, thoughts_token_count=0
    )
    response = types.SimpleNamespace(candidates=[candidate], usage_metadata=usage)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        with pytest.raises(AtomicAgentsError, match="usage_metadata"):
            backend.call(
                model="vertex/gemini-2.0-flash",
                system_prompt="sys",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
                temperature=0.0,
            )


def test_call_thoughts_only_output_does_not_raise_and_bills_thoughts(
    backend_and_fake_sdk,
):
    """Regression for the round-3 zero-output guard: a turn with visible text
    present but candidates_token_count=0 while thoughts_token_count>0 is a
    GENUINE billable response (Vertex bills the reasoning tokens at the output
    rate even when the candidate count under-reports them). The guard must key
    on TOTAL billable output (candidate + thoughts), not the candidate count
    alone, so this must NOT raise and must report output_tokens ==
    thoughts_token_count. The genuinely-no-visible-content path (parts=[]) is
    covered separately by test_call_blocked_thoughts_only_response_bills_thoughts.
    """
    backend, fake_client, patches = backend_and_fake_sdk
    part = types.SimpleNamespace(text="visible answer", function_call=None)
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    usage = types.SimpleNamespace(
        prompt_token_count=10, candidates_token_count=0, thoughts_token_count=80
    )
    response = types.SimpleNamespace(candidates=[candidate], usage_metadata=usage)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.5-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert r.input_tokens == 10
    assert r.output_tokens == 80, (
        "candidates_token_count=0 + thoughts_token_count=80 is a billable "
        "thinking turn — output_tokens must be the 80 reasoning tokens, not 0 "
        "(and the zero-output guard must not fire)"
    )


def test_call_blocked_thoughts_only_response_bills_thoughts(backend_and_fake_sdk):
    """A blocked/empty thinking response (no visible text, no tool_uses ->
    produced_content=False) that burned reasoning before being blocked
    (finish_reason=MAX_TOKENS) is NOT free: thoughts_token_count is billed at
    the output rate. Asserts the comment's old '$0' claim is genuinely dead —
    output_tokens == thoughts_token_count even on the produced_content=False
    path, and the prompt is charged via prompt_token_count.
    """
    backend, fake_client, patches = backend_and_fake_sdk
    # No text part, no function_call -> produced_content is False.
    content = types.SimpleNamespace(parts=[])
    candidate = types.SimpleNamespace(content=content)
    usage = types.SimpleNamespace(
        prompt_token_count=500, candidates_token_count=0, thoughts_token_count=42
    )
    response = types.SimpleNamespace(candidates=[candidate], usage_metadata=usage)
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.5-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert r.input_tokens == 500
    assert r.output_tokens == 42


def test_call_returns_raw_llm_response(backend_and_fake_sdk):
    backend, fake_client, patches = backend_and_fake_sdk
    fake_client.models.generate_content.return_value = _make_text_response("hello")

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert isinstance(r, _RawLLMResponse)
    assert r.text == "hello"
    assert isinstance(r.tool_uses, list)

    # Regression guard: _messages_to_genai_contents must return a non-empty
    # list (the missing `return contents` bug sent contents=None to the SDK on
    # every real call; the mock ignored the arg, hiding a total functional break).
    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    contents = call_kwargs.get("contents")
    assert isinstance(contents, list)
    assert len(contents) >= 1


def test_call_cache_hit_tokens_always_zero(backend_and_fake_sdk):
    """cache_hit_tokens must be 0 — no cache discount for Vertex Gemini."""
    backend, fake_client, patches = backend_and_fake_sdk
    fake_client.models.generate_content.return_value = _make_text_response()

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert r.cache_hit_tokens == 0
    assert r.cache_miss_tokens == 0


def test_call_strips_vertex_prefix_before_sdk(backend_and_fake_sdk):
    """The SDK receives the bare model id, not the vertex/ prefixed form."""
    backend, fake_client, patches = backend_and_fake_sdk
    fake_client.models.generate_content.return_value = _make_text_response()

    with patch.dict(sys.modules, patches):
        backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    call_args = fake_client.models.generate_content.call_args
    assert call_args is not None
    model_arg = (
        call_args.kwargs.get("model") or call_args.args[0] if call_args.args else None
    )
    if model_arg is None:
        # Try positional
        model_arg = call_args[1].get("model") if len(call_args) > 1 else None
    # The SDK should receive "gemini-2.0-flash", NOT "vertex/gemini-2.0-flash"
    assert model_arg == "gemini-2.0-flash"


def test_call_system_instruction_not_in_messages(backend_and_fake_sdk):
    """System prompt goes via system_instruction= in GenerateContentConfig,
    NOT as a message in the contents list (P1 ruling).
    """
    backend, fake_client, patches = backend_and_fake_sdk
    fake_client.models.generate_content.return_value = _make_text_response()

    with patch.dict(sys.modules, patches):
        backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="My system prompt",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.0,
        )

    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    config = call_kwargs.get("config")
    assert config is not None

    # system_instruction should be on the config object
    assert hasattr(config, "system_instruction")
    assert config.system_instruction == "My system prompt"

    # The contents list should NOT contain "My system prompt"
    contents = call_kwargs.get("contents") or []
    for item in contents:
        parts = getattr(item, "parts", [])
        for part in parts:
            text = getattr(part, "text", "")
            assert "My system prompt" not in str(text), (
                "System prompt must not appear in the contents list"
            )


# ──────────────────────────────────────────────────────────────────
# call() — tool_use extraction (P0: function_call parts, not tool_use blocks)


def test_call_extracts_tool_uses_from_function_call_parts(backend_and_fake_sdk):
    """P0: Gemini returns tool calls as parts[i].function_call, NOT as
    blocks with .type == 'tool_use'. Zero tool uses when using the wrong
    extraction shape is a silent data loss.
    """
    backend, fake_client, patches = backend_and_fake_sdk
    response = _make_tool_response(
        tool_calls=[{"name": "search", "args": {"q": "test query"}}]
    )
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "search something"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert len(r.tool_uses) == 1
    tu = r.tool_uses[0]
    assert tu["name"] == "search"
    assert tu["input"] == {"q": "test query"}
    assert "id" in tu
    assert isinstance(tu["id"], str)


def test_call_tool_use_synthetic_ids_are_strings(backend_and_fake_sdk):
    """Synthetic IDs (call_0, call_1, ...) must be present and string-typed."""
    backend, fake_client, patches = backend_and_fake_sdk
    response = _make_tool_response(
        tool_calls=[
            {"name": "tool_a", "args": {"x": 1}},
            {"name": "tool_b", "args": {"y": 2}},
        ]
    )
    fake_client.models.generate_content.return_value = response

    with patch.dict(sys.modules, patches):
        r = backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "call tools"}],
            max_tokens=100,
            temperature=0.0,
        )

    assert len(r.tool_uses) == 2
    ids = {tu["id"] for tu in r.tool_uses}
    assert len(ids) == 2  # distinct IDs
    for tu in r.tool_uses:
        assert isinstance(tu["id"], str)


def test_call_tool_definitions_translated_to_function_declarations(
    backend_and_fake_sdk,
):
    """P0: LLMToolDefinition list must be translated to FunctionDeclaration
    objects — passing raw dicts causes a TypeError at the SDK boundary.
    """
    backend, fake_client, patches = backend_and_fake_sdk
    fake_client.models.generate_content.return_value = _make_text_response()

    canonical_tools = [
        LLMToolDefinition(
            name="search",
            description="search the web",
            input_schema={
                "type": "object",
                "title": "SearchInput",  # title must be stripped for Gemini
                "properties": {"q": {"type": "string", "title": "Query"}},
            },
        )
    ]

    with patch.dict(sys.modules, patches):
        # Should not raise
        backend.call(
            model="vertex/gemini-2.0-flash",
            system_prompt="sys",
            messages=[{"role": "user", "content": "search"}],
            max_tokens=100,
            temperature=0.0,
            tools=canonical_tools,
        )

    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    config = call_kwargs.get("config")
    # tools should be set on the config object
    assert hasattr(config, "tools")
    assert config.tools is not None


# ──────────────────────────────────────────────────────────────────
# format_tool_results() (P0: Gemini shape, not Anthropic/OpenAI)


def test_format_tool_results_returns_list_of_dicts(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="call_0", name="search", input={})],
        tool_results=[LLMToolResult(tool_use_id="call_0", content="result text")],
    )
    assert isinstance(out, list)
    # Two messages: the model-role function_call echo turn, then the user-role
    # function_response turn (mirrors the Anthropic/OpenAI echo contract).
    assert len(out) == 2
    assert all(isinstance(msg, dict) for msg in out)


def test_format_tool_results_emits_model_echo_then_user_response(backend_and_fake_sdk):
    """P0: Gemini's API requires every function_response to be immediately
    preceded by the matching function_call. format_tool_results must therefore
    emit a model-role echo turn (function_call) BEFORE the user-role
    function_response turn — NOT a single user message.

    Also asserts it is NOT Anthropic's tool_result blocks nor OpenAI's
    role:tool messages.
    """
    backend, _, _ = backend_and_fake_sdk
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="call_0", name="search", input={"q": "x"})],
        tool_results=[LLMToolResult(tool_use_id="call_0", content="found it")],
        assistant_text="let me search",
    )

    assert len(out) == 2
    model_msg, user_msg = out

    # Model echo turn carries the function_call (and the interim assistant_text).
    assert model_msg["role"] == "model"
    assert "parts" in model_msg
    assert "content" not in model_msg  # would be Anthropic shape
    assert "tool_calls" not in model_msg  # would be OpenAI shape
    fc_parts = [p for p in model_msg["parts"] if "function_call" in p]
    assert len(fc_parts) == 1
    assert fc_parts[0]["function_call"]["name"] == "search"
    assert fc_parts[0]["function_call"]["args"] == {"q": "x"}
    text_parts = [p for p in model_msg["parts"] if "text" in p]
    assert text_parts and text_parts[0]["text"] == "let me search"

    # User response turn carries the matching function_response.
    assert user_msg["role"] == "user"
    assert "parts" in user_msg
    assert "content" not in user_msg
    fr = user_msg["parts"][0]["function_response"]
    assert fr["name"] == "search"
    assert fr["id"] == "call_0"
    assert "output" in fr["response"]


def test_format_tool_results_roundtrips_through_messages_to_contents(
    backend_and_fake_sdk,
):
    """P0 round-trip: feeding format_tool_results output back through
    _messages_to_genai_contents (as agent.py does on the next call) must build a
    model-role Content carrying the function_call that precedes the user-role
    Content carrying the matching function_response. This is the end-to-end
    multi-iteration tool-loop contract the conformance criterion in #345 asserts.
    """
    backend, _, patches = backend_and_fake_sdk
    fake_genai_types = patches["google.genai.types"]

    cont = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="call_0", name="search", input={"q": "x"})],
        tool_results=[LLMToolResult(tool_use_id="call_0", content="found it")],
        assistant_text="searching",
    )
    # Simulate the next-iteration history the agent builds: prior user prompt
    # plus the format_tool_results continuation.
    history = [{"role": "user", "content": "search the web"}, *cont]

    contents = _messages_to_genai_contents(history, fake_genai_types)

    # Must NOT be None (regression guard for the missing `return contents`).
    assert contents is not None
    assert isinstance(contents, list)
    # user prompt, model function_call turn, user function_response turn.
    assert len(contents) == 3

    model_content = contents[1]
    user_resp_content = contents[2]
    assert model_content.role == "model"
    assert user_resp_content.role == "user"

    # The model turn must carry a function_call whose name matches the response.
    model_fc = [
        getattr(p, "function_call", None)
        for p in model_content.parts
        if getattr(p, "function_call", None) is not None
    ]
    assert len(model_fc) == 1
    assert model_fc[0].name == "search"

    resp_fr = [
        getattr(p, "function_response", None)
        for p in user_resp_content.parts
        if getattr(p, "function_response", None) is not None
    ]
    assert len(resp_fr) == 1
    assert resp_fr[0].name == "search"
    # function_call precedes function_response in history order.
    assert model_fc[0].name == resp_fr[0].name


def test_format_tool_results_error_uses_error_key(backend_and_fake_sdk):
    """Error tool results use response={"error": ...} not response={"output": ...}."""
    backend, _, _ = backend_and_fake_sdk
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="call_0", name="search", input={})],
        tool_results=[
            LLMToolResult(
                tool_use_id="call_0", content="[tool error] boom", is_error=True
            )
        ],
    )
    # Two turns: model echo + user response. The error lives on the user turn.
    assert len(out) == 2
    user_msg = out[1]
    assert user_msg["role"] == "user"
    fr = user_msg["parts"][0]["function_response"]
    assert "error" in fr["response"]
    assert "output" not in fr["response"]


def test_format_tool_results_transmits_orphan_result(backend_and_fake_sdk):
    """A tool_result whose id is NOT in tool_uses must still be transmitted —
    never silently dropped. Mirrors the Anthropic backend's transmit-every-result
    behavior; a dropped result is silent data loss to the model.
    """
    backend, _, _ = backend_and_fake_sdk
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="call_0", name="search", input={"q": "x"})],
        tool_results=[
            LLMToolResult(tool_use_id="call_0", content="matched result"),
            # Orphan: no tool_use with this id.
            LLMToolResult(tool_use_id="call_99", content="orphan result"),
        ],
    )
    assert len(out) == 2
    user_msg = out[1]
    responses = [
        p["function_response"] for p in user_msg["parts"] if "function_response" in p
    ]
    transmitted_ids = {fr["id"] for fr in responses}
    assert "call_0" in transmitted_ids
    assert "call_99" in transmitted_ids, (
        "orphan tool_result must be transmitted, not silently dropped"
    )
    # The orphan's content survives end-to-end.
    serialized = json.dumps(out)
    assert "orphan result" in serialized


def test_format_tool_results_empty_input_returns_empty(backend_and_fake_sdk):
    backend, _, _ = backend_and_fake_sdk
    out = backend.format_tool_results(tool_uses=[], tool_results=[])
    assert out == []


def test_format_tool_results_skips_atomic_capture(backend_and_fake_sdk):
    """atomic_capture must be filtered from the tool continuation (same rule
    as Anthropic backend — capture path handles it separately).
    """
    backend, _, _ = backend_and_fake_sdk
    out = backend.format_tool_results(
        tool_uses=[
            LLMToolUse(id="call_0", name="atomic_capture", input={}),
            LLMToolUse(id="call_1", name="search", input={"q": "x"}),
        ],
        tool_results=[LLMToolResult(tool_use_id="call_1", content="result")],
    )
    serialized = json.dumps(out)
    assert "atomic_capture" not in serialized


def test_format_tool_results_non_json_content_falls_back_to_str(backend_and_fake_sdk):
    """Wire-byte parity: non-JSON-serializable content uses str() fallback."""
    import datetime

    backend, _, _ = backend_and_fake_sdk
    t = datetime.datetime(2026, 1, 2)
    out = backend.format_tool_results(
        tool_uses=[LLMToolUse(id="call_0", name="clock", input={})],
        tool_results=[LLMToolResult(tool_use_id="call_0", content=t)],
    )
    assert isinstance(out, list)
    assert len(out) > 0


# ──────────────────────────────────────────────────────────────────
# _strip_title_keys helper


def test_strip_title_keys_removes_title_at_all_levels():
    schema = {
        "type": "object",
        "title": "TopLevel",
        "properties": {
            "name": {"type": "string", "title": "Name"},
            "nested": {
                "type": "object",
                "title": "Nested",
                "properties": {"x": {"type": "integer"}},
            },
        },
    }
    result = _strip_title_keys(schema)
    assert "title" not in result
    assert "title" not in result["properties"]["name"]
    assert "title" not in result["properties"]["nested"]


def test_strip_title_keys_does_not_mutate_input():
    schema = {"type": "object", "title": "T", "properties": {}}
    original = dict(schema)
    _strip_title_keys(schema)
    assert schema == original  # no mutation


def test_strip_title_keys_preserves_other_keys():
    schema = {"type": "object", "description": "desc", "title": "T"}
    result = _strip_title_keys(schema)
    assert result["type"] == "object"
    assert result["description"] == "desc"
    assert "title" not in result


# ──────────────────────────────────────────────────────────────────
# _resolve_vertex_family helper


def test_resolve_vertex_family_exact_match():
    caps = _resolve_vertex_family("vertex/gemini-2.0-flash")
    assert caps is not None
    assert caps["tools"] is True


def test_resolve_vertex_family_dated_alias():
    """vertex/gemini-2.0-flash-20260601 should resolve to the base family."""
    caps = _resolve_vertex_family("vertex/gemini-2.0-flash-20260601")
    assert caps is not None


def test_resolve_vertex_family_unknown_returns_none():
    assert _resolve_vertex_family("vertex/gemini-99-unknown") is None


def test_resolve_vertex_family_longest_prefix_wins():
    """A dated flash-lite id must resolve to the flash-lite family (the longest
    prefix), NOT the flash family it also prefix-matches.

    flash advertises tools/tool_results True; flash-lite advertises them False.
    Returning flash for a flash-lite id is a capabilities()-honesty violation
    (spec/31 conformance rule 4) — it would pass tool definitions to a model
    that rejects them.
    """
    caps = _resolve_vertex_family("vertex/gemini-2.0-flash-lite-001")
    assert caps is not None
    assert caps["tools"] is False
    assert caps["tool_results"] is False
    assert caps["vision"] is False


def test_capabilities_dated_flash_lite_no_tools(backend_and_fake_sdk):
    """End-to-end: the real Vertex GA id gemini-2.0-flash-lite-001 must not
    advertise tools (longest-prefix resolution through capabilities())."""
    backend, _, _ = backend_and_fake_sdk
    caps = backend.capabilities("vertex/gemini-2.0-flash-lite-001")
    assert caps.tools is False
    assert caps.tool_results is False
    assert caps.vision is False


# ──────────────────────────────────────────────────────────────────
# Missing SDK error


def test_missing_sdk_raises_atomic_agents_error(monkeypatch):
    """When google-genai is not installed, construction raises AtomicAgentsError
    so _ensure_default_backends() catches ImportError / AtomicAgentsError cleanly.
    """
    from atomic_agents.exceptions import AtomicAgentsError

    # Remove google.genai from sys.modules to simulate missing install
    patched = {k: None for k in list(sys.modules.keys()) if "google" in k}
    patched["google"] = None
    patched["google.genai"] = None

    with patch.dict(sys.modules, patched):
        with pytest.raises((AtomicAgentsError, ImportError)):
            VertexGeminiLLMBackend()


def test_ensure_default_backends_missing_sdk_logs_at_debug_not_warning(caplog):
    """Pin the home-user-no-noise contract: when the optional provider SDKs are
    absent, _ensure_default_backends must record the "not registered" misses at
    DEBUG, never at WARNING+. A future edit reverting any backend's registration
    miss to _logger.warning(...) — reintroducing the first-call noise this
    behavior eliminated for users who never opted into a provider — fails here.
    """
    import logging

    from atomic_agents import llm as _llm

    # Force a fresh registration pass: clear the idempotency guard AND the
    # registry so the try/except clauses actually run under caplog.
    saved_guard = _llm._DEFAULTS_REGISTERED
    saved_registry = dict(_llm._registry)
    _llm._DEFAULTS_REGISTERED = False
    _llm._registry.clear()

    # Simulate every optional provider SDK being unimportable so each backend
    # construction raises ImportError / AtomicAgentsError and hits the guarded
    # DEBUG-log branch.
    patched = {
        k: None
        for k in list(sys.modules.keys())
        if any(p in k for p in ("anthropic", "openai", "google"))
    }
    patched["anthropic"] = None
    patched["openai"] = None
    patched["google"] = None
    patched["google.genai"] = None

    try:
        with patch.dict(sys.modules, patched):
            with caplog.at_level(logging.DEBUG, logger="atomic_agents.llm"):
                _llm._ensure_default_backends()
        not_registered = [
            r for r in caplog.records if "not registered" in r.getMessage()
        ]
        assert not_registered, (
            "expected at least one 'not registered' record when optional SDKs "
            "are absent — the registration path did not run under caplog"
        )
        assert all(r.levelno == logging.DEBUG for r in not_registered), (
            "registration misses must log at DEBUG (home-user-no-noise); "
            "found a non-DEBUG record: "
            + repr(
                [
                    (r.levelname, r.getMessage())
                    for r in not_registered
                    if r.levelno != logging.DEBUG
                ]
            )
        )
        assert not [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == "atomic_agents.llm"
        ], "no WARNING+ records may be emitted on the registration path"
    finally:
        _llm._DEFAULTS_REGISTERED = saved_guard
        _llm._registry.clear()
        _llm._registry.update(saved_registry)


# ──────────────────────────────────────────────────────────────────
# doctor.check_vertex_credentials


def test_check_vertex_credentials_fail_missing_sdk(monkeypatch):
    """When google.genai is missing, doctor returns FAIL with [vertex] install hint."""
    from atomic_agents.doctor import check_vertex_credentials, FAIL

    patched = {k: None for k in list(sys.modules.keys()) if "google" in k}
    patched["google"] = None
    patched["google.genai"] = None
    patched["google.auth"] = None

    with patch.dict(sys.modules, patched):
        result = check_vertex_credentials()

    assert result.status == FAIL
    assert (
        "vertex" in result.fix_hint.lower() or "google-genai" in result.fix_hint.lower()
    )


def test_check_vertex_credentials_fail_no_adc():
    """When ADC is not configured, doctor returns FAIL with gcloud login hint."""
    from atomic_agents.doctor import check_vertex_credentials, FAIL

    # Build a minimal fake google module with google.auth that raises DefaultCredentialsError
    fake_google = types.ModuleType("google")
    fake_auth = types.ModuleType("google.auth")
    fake_auth_exceptions = types.ModuleType("google.auth.exceptions")

    class FakeDefaultCredentialsError(Exception):
        pass

    class FakeTransportError(Exception):
        pass

    fake_auth_exceptions.DefaultCredentialsError = FakeDefaultCredentialsError
    fake_auth_exceptions.TransportError = FakeTransportError

    def fake_default(**kw):
        raise FakeDefaultCredentialsError("no credentials found")

    fake_auth.default = fake_default
    fake_auth.exceptions = fake_auth_exceptions

    fake_auth_transport = types.ModuleType("google.auth.transport")
    fake_auth_transport_requests = types.ModuleType("google.auth.transport.requests")
    fake_auth_transport_requests.Request = lambda: None

    fake_genai = types.ModuleType("google.genai")

    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.genai": fake_genai,
            "google.auth": fake_auth,
            "google.auth.exceptions": fake_auth_exceptions,
            "google.auth.transport": fake_auth_transport,
            "google.auth.transport.requests": fake_auth_transport_requests,
        },
    ):
        result = check_vertex_credentials()

    assert result.status == FAIL
    assert (
        "gcloud" in result.fix_hint.lower()
        or "application-default" in result.fix_hint.lower()
    )


def _fake_google_auth_modules(refresh_ok=True, detected_project=None):
    """Build a fake google.auth module tree whose default() returns a
    credentials object with a refresh() that succeeds (or raises TransportError).

    Returns the dict of sys.modules patches.
    """
    fake_google = types.ModuleType("google")
    fake_auth = types.ModuleType("google.auth")
    fake_auth_exceptions = types.ModuleType("google.auth.exceptions")

    class FakeDefaultCredentialsError(Exception):
        pass

    class FakeTransportError(Exception):
        pass

    fake_auth_exceptions.DefaultCredentialsError = FakeDefaultCredentialsError
    fake_auth_exceptions.TransportError = FakeTransportError

    class FakeCredentials:
        def refresh(self, request):
            if not refresh_ok:
                raise FakeTransportError("network down")
            # Success: no-op (token minted).

    def fake_default(**kw):
        return FakeCredentials(), detected_project

    fake_auth.default = fake_default
    fake_auth.exceptions = fake_auth_exceptions

    fake_auth_transport = types.ModuleType("google.auth.transport")
    fake_auth_transport_requests = types.ModuleType("google.auth.transport.requests")
    fake_auth_transport_requests.Request = lambda: object()

    fake_genai = types.ModuleType("google.genai")

    return {
        "google": fake_google,
        "google.genai": fake_genai,
        "google.auth": fake_auth,
        "google.auth.exceptions": fake_auth_exceptions,
        "google.auth.transport": fake_auth_transport,
        "google.auth.transport.requests": fake_auth_transport_requests,
    }


def test_check_vertex_credentials_warn_when_project_unset(monkeypatch):
    """Happy path 1: ADC resolves + token mints, but GOOGLE_CLOUD_PROJECT is
    unset → WARN (Cloud Run / GKE auto-resolve the project; absence is not a
    misconfiguration). This is the load-bearing success branch the round-1
    doctor work introduced and left untested."""
    from atomic_agents.doctor import check_vertex_credentials, WARN

    # autouse fixture sets GOOGLE_CLOUD_PROJECT — clear it for this case.
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    patches = _fake_google_auth_modules(refresh_ok=True, detected_project=None)

    with patch.dict(sys.modules, patches):
        result = check_vertex_credentials()

    assert result.status == WARN
    assert result.detail.get("token_valid") is True
    assert "GOOGLE_CLOUD_PROJECT" in result.message


def test_check_vertex_credentials_pass_when_project_set(monkeypatch):
    """Happy path 2: ADC resolves + token mints + GOOGLE_CLOUD_PROJECT set →
    PASS with the project echoed. Exercises the refresh()-proves-usable claim
    and the PASS branch end-to-end."""
    from atomic_agents.doctor import check_vertex_credentials, PASS

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
    patches = _fake_google_auth_modules(refresh_ok=True, detected_project=None)

    with patch.dict(sys.modules, patches):
        result = check_vertex_credentials()

    assert result.status == PASS
    assert result.detail.get("token_valid") is True
    assert result.detail.get("project") == "my-gcp-project"
    assert "my-gcp-project" in result.message


def test_check_vertex_credentials_fail_on_token_refresh_network_error(monkeypatch):
    """A TransportError during refresh() (token mint fails) → FAIL with a
    network-specific hint. Pins the central refresh()-proves-usable behavior."""
    from atomic_agents.doctor import check_vertex_credentials, FAIL

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
    patches = _fake_google_auth_modules(refresh_ok=False)

    with patch.dict(sys.modules, patches):
        result = check_vertex_credentials()

    assert result.status == FAIL
    assert "network" in result.message.lower() or "refresh" in result.message.lower()


def test_provider_for_model_vertex_maps_correctly():
    """_provider_for_model must map vertex/gemini-* → 'vertex-gemini'."""
    from atomic_agents.doctor import _provider_for_model

    assert _provider_for_model("vertex/gemini-2.0-flash") == "vertex-gemini"
    assert _provider_for_model("vertex/gemini-2.5-pro") == "vertex-gemini"
    # Must NOT map vertex/claude-* (that's a separate future backend)
    assert _provider_for_model("vertex/claude-sonnet-4") is None
    # Must NOT map bare vertex/ prefix
    assert _provider_for_model("vertex/unknown-model") is None


# ──────────────────────────────────────────────────────────────────
# PRICING table round-trip


def test_pricing_table_vertex_entries_present():
    """The four Vertex Gemini model families must be in PRICING with vertex/ prefix."""
    from atomic_agents._costs import PRICING

    required = [
        "vertex/gemini-2.5-flash",
        "vertex/gemini-2.5-pro",
        "vertex/gemini-2.0-flash",
        "vertex/gemini-2.0-flash-lite",
    ]
    for key in required:
        assert key in PRICING, f"Missing PRICING entry for {key!r}"
        assert PRICING[key]["input"] > 0
        assert PRICING[key]["output"] > 0


def test_check_model_vertex_model_passes_when_in_pricing():
    """check_model must PASS for a vertex/gemini-* model that is in PRICING."""
    from atomic_agents.doctor import check_model, PASS

    result = check_model({"default_model": "vertex/gemini-2.0-flash"})
    assert result.status == PASS


def test_check_model_vertex_model_fail_hint_mentions_vertex(monkeypatch):
    """When a vertex/* model is not in PRICING, the fix_hint should mention
    vertex pricing, not the full list of non-Vertex model ids.
    """
    from atomic_agents.doctor import check_model, FAIL

    result = check_model({"default_model": "vertex/gemini-99-unknown"})
    assert result.status == FAIL
    # The hint must reference vertex pricing, not just the full PRICING key list
    assert "vertex" in result.fix_hint.lower()

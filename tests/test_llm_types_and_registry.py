"""Tests for the canonical types + registry primitives of the LLMBackend Protocol.

This is the scaffolding PR for #87. No backend implements the Protocol yet;
these tests assert the contract surface itself — type construction,
Protocol shape, registry registration, dispatch logic, and error semantics.
The conformance suite that exercises actual backend implementations lands
with PR 4 (spec doc + ~30 conformance tests parameterized over backends).
"""

from __future__ import annotations

import pytest

from atomic_agents.exceptions import (
    AmbiguousBackendError,
    AtomicAgentsError,
    UnknownModelError,
)
from atomic_agents.llm import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
    SyncLLMBackend,
    _RawLLMResponse,
    find_backend_for_model,
    get_backend,
    iter_registered_backends,
    register_llm_backend,
    unregister_llm_backend,
)


# ──────────────────────────────────────────────────────────────────
# Fake backend for registry tests


class _FakeBackend:
    """Minimal SyncLLMBackend implementation for registry tests.

    Structural conformance only — no real LLM call. ``supports_model``
    is configured at construction so tests can stage 0/1/many matches.
    """

    def __init__(self, provider_id: str, claims: list[str] | None = None) -> None:
        self._provider_id = provider_id
        self._claims = claims or []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def supports_model(self, model_id: str) -> bool:
        return any(model_id.startswith(c) for c in self._claims)

    def capabilities(self, model_id: str) -> LLMCapabilities:
        return LLMCapabilities(
            tools=True, tool_results=True, cache_control=False,
            streaming=False, vision=False, max_input_tokens=128_000,
            max_output_tokens=4_096, usage_reporting=True,
            structured_output=False,
        )

    def pricing(self, model_id: str) -> PricingInfo | None:
        return None

    def count_tokens(self, system_prompt, messages, tools=None) -> int:
        return 100

    def call(self, model, system_prompt, messages, max_tokens, temperature,
             tools=None, cache_directives=None) -> _RawLLMResponse:
        return _RawLLMResponse(text="fake", input_tokens=10, output_tokens=5)

    def format_tool_results(self, tool_uses, tool_results, assistant_text=""):
        return [{"role": "tool", "content": str(r.content)} for r in tool_results]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Empty the LLM registry around every test for isolation.

    Necessary because the registry is process-local module state. Without
    cleanup, test ordering would matter and a leftover backend from one
    test would corrupt the next test's expected match set.

    Also pins ``_DEFAULTS_REGISTERED`` to True for the duration of each
    test so the lazy default-backend registration doesn't fire and
    re-populate the cleared registry. The flag + registry are both
    restored to their pre-test values at teardown — so tests that
    indirectly trigger ``find_backend_for_model`` from outside this
    module continue to see the lazy-init contract.
    """
    from atomic_agents import llm as _llm_pkg
    saved_registry = dict(_llm_pkg._registry)
    saved_flag = _llm_pkg._DEFAULTS_REGISTERED
    _llm_pkg._registry.clear()
    _llm_pkg._DEFAULTS_REGISTERED = True  # suppress lazy re-registration
    yield
    _llm_pkg._registry.clear()
    _llm_pkg._registry.update(saved_registry)
    _llm_pkg._DEFAULTS_REGISTERED = saved_flag


# ──────────────────────────────────────────────────────────────────
# Canonical type construction


def test_llm_tool_definition_is_frozen_and_equal():
    """LLMToolDefinition is frozen (safe to pass across boundaries) and
    supports value equality via the dataclass-generated __eq__. Not
    hashable — `input_schema` is a dict, which is intentional (JSON
    Schema is naturally nested-mutable). Consumers that need a key
    should derive one from `name` instead.
    """
    td = LLMToolDefinition(
        name="echo", description="repeat input", input_schema={"type": "object"},
    )
    # Frozen — assignment raises
    with pytest.raises(Exception):  # FrozenInstanceError subclass varies
        td.name = "other"  # type: ignore[misc]
    # Value equality works
    assert td == LLMToolDefinition(name="echo", description="repeat input",
                                    input_schema={"type": "object"})
    # Default
    assert td.strict is False
    # Not hashable — documented constraint
    with pytest.raises(TypeError, match="unhashable"):
        hash(td)


def test_llm_tool_use_normalized_dict_input():
    """LLMToolUse.input must be a dict — OpenAI returns JSON-string on the
    wire and backends are responsible for parsing before constructing the
    canonical type, so consumers see consistent shape.
    """
    tu = LLMToolUse(id="tu_001", name="search", input={"q": "atomic agents"})
    assert tu.input["q"] == "atomic agents"
    assert isinstance(tu.input, dict)


def test_llm_tool_result_defaults_to_not_error():
    tr = LLMToolResult(tool_use_id="tu_001", content="ok")
    assert tr.is_error is False
    err = LLMToolResult(tool_use_id="tu_002", content="oops", is_error=True)
    assert err.is_error is True


def test_cache_directive_default_ephemeral():
    cd = CacheDirective(breakpoint_id="system-persona")
    assert cd.ttl == "ephemeral"
    cd_1h = CacheDirective(breakpoint_id="system-tools", ttl="1h")
    assert cd_1h.ttl == "1h"


def test_llm_capabilities_full_construction():
    caps = LLMCapabilities(
        tools=True, tool_results=True, cache_control=True, streaming=False,
        vision=True, max_input_tokens=200_000, max_output_tokens=8_192,
        usage_reporting=True, structured_output=True,
    )
    assert caps.tools is True
    assert caps.max_input_tokens == 200_000


def test_pricing_info_default_cache_discount_matches_anthropic():
    """Anthropic charges 10% of input rate for cache-hit tokens; the
    framework's `_costs.CACHE_HIT_DISCOUNT` constant is 0.10. PricingInfo
    defaults to the same so backends that don't override see consistent
    pricing math.
    """
    pi = PricingInfo(input_per_million_usd=3.0, output_per_million_usd=15.0)
    assert pi.cache_hit_discount == 0.10


def test_raw_llm_response_defaults_tool_uses_to_empty_list():
    """Backwards-compat with old _RawLLMResponse — tool_uses must default
    to an empty list, never None, so consumers can iterate without a guard.
    """
    r = _RawLLMResponse(text="hi", input_tokens=10, output_tokens=5)
    assert r.tool_uses == []
    assert r.reasoning_text is None
    assert r.cache_hit_tokens == 0


# ──────────────────────────────────────────────────────────────────
# Protocol shape


def test_fake_backend_satisfies_protocol_at_runtime():
    """A minimal class with the right method signatures must pass
    isinstance(obj, SyncLLMBackend). @runtime_checkable enforces
    method presence (not signatures).
    """
    fake = _FakeBackend("test-provider", claims=["test-"])
    assert isinstance(fake, SyncLLMBackend)


def test_obviously_wrong_object_fails_protocol_check():
    """Bare object missing all methods must NOT pass the runtime check."""
    assert not isinstance(object(), SyncLLMBackend)
    assert not isinstance("a string", SyncLLMBackend)
    assert not isinstance({"provider_id": "x"}, SyncLLMBackend)


# ──────────────────────────────────────────────────────────────────
# Registry register / get / iter


def test_register_and_get_roundtrip():
    fake = _FakeBackend("anthropic", claims=["claude-"])
    register_llm_backend(fake)
    assert get_backend("anthropic") is fake


def test_get_unknown_provider_returns_none():
    assert get_backend("not-registered") is None


def test_register_rejects_non_conforming_object():
    """Registering a non-Protocol object must raise TypeError, not
    silently corrupt the registry.
    """
    with pytest.raises(TypeError, match="SyncLLMBackend"):
        register_llm_backend("a string is not a backend")  # type: ignore[arg-type]


def test_unregister_removes_backend():
    fake = _FakeBackend("anthropic", claims=["claude-"])
    register_llm_backend(fake)
    unregister_llm_backend("anthropic")
    assert get_backend("anthropic") is None


def test_unregister_unknown_is_noop():
    """Removing a backend that was never registered must not raise —
    matches dict.pop(key, default) semantics that operators expect.
    """
    unregister_llm_backend("never-registered")  # should not raise


def test_re_register_same_provider_replaces():
    """Per the docstring — re-registering swaps the backend silently
    so operators can wrap (e.g., RetryingLLMBackend) with one call.
    """
    original = _FakeBackend("anthropic", claims=["claude-"])
    replacement = _FakeBackend("anthropic", claims=["claude-", "experimental-"])
    register_llm_backend(original)
    register_llm_backend(replacement)
    assert get_backend("anthropic") is replacement


def test_iter_registered_backends_returns_all():
    a = _FakeBackend("anthropic", claims=["claude-"])
    o = _FakeBackend("openai", claims=["gpt-"])
    register_llm_backend(a)
    register_llm_backend(o)
    found = set(iter_registered_backends())
    assert found == {a, o}


# ──────────────────────────────────────────────────────────────────
# find_backend_for_model dispatch


def test_find_backend_zero_matches_raises_unknown_model():
    with pytest.raises(UnknownModelError, match="claude-opus-4-7"):
        find_backend_for_model("claude-opus-4-7")


def test_find_backend_single_match():
    a = _FakeBackend("anthropic", claims=["claude-"])
    register_llm_backend(a)
    assert find_backend_for_model("claude-opus-4-7") is a


def test_find_backend_multiple_matches_raises_ambiguous():
    """Two backends both claim gpt-5 (OpenAI direct + a hypothetical
    Azure OpenAI wrapper). Without preferred_provider, the registry
    must raise rather than silently picking one.
    """
    direct = _FakeBackend("openai", claims=["gpt-"])
    azure = _FakeBackend("azure-openai", claims=["gpt-"])
    register_llm_backend(direct)
    register_llm_backend(azure)
    with pytest.raises(AmbiguousBackendError) as exc_info:
        find_backend_for_model("gpt-5")
    err = exc_info.value
    assert err.model == "gpt-5"
    assert set(err.candidates) == {"openai", "azure-openai"}
    # Hint about model.md disambiguation must be in the message
    assert "provider:" in str(err)
    assert "model.md" in str(err)


def test_find_backend_preferred_provider_wins_with_match():
    """When operator's model.md says `provider: azure-openai`, that
    backend wins even though both claim gpt-5.
    """
    direct = _FakeBackend("openai", claims=["gpt-"])
    azure = _FakeBackend("azure-openai", claims=["gpt-"])
    register_llm_backend(direct)
    register_llm_backend(azure)
    chosen = find_backend_for_model("gpt-5", preferred_provider="azure-openai")
    assert chosen is azure


def test_find_backend_preferred_provider_unregistered_raises_unknown():
    """When `provider:` names a backend that isn't registered, fail
    with a useful message (lists what IS registered).
    """
    register_llm_backend(_FakeBackend("openai", claims=["gpt-"]))
    with pytest.raises(UnknownModelError, match="not-installed"):
        find_backend_for_model("gpt-5", preferred_provider="not-installed")


def test_find_backend_preferred_provider_does_not_support_model_raises_ambiguous():
    """`provider: anthropic` + model `gpt-5` is misconfigured. Don't silently
    fall through to the OpenAI backend; raise so the operator notices.
    """
    register_llm_backend(_FakeBackend("openai", claims=["gpt-"]))
    register_llm_backend(_FakeBackend("anthropic", claims=["claude-"]))
    with pytest.raises(AmbiguousBackendError) as exc_info:
        find_backend_for_model("gpt-5", preferred_provider="anthropic")
    assert exc_info.value.candidates == ["anthropic"]


# ──────────────────────────────────────────────────────────────────
# Exception hierarchy


def test_unknown_model_error_subclasses_atomic_agents_error():
    """All framework exceptions chain through AtomicAgentsError so a single
    `except AtomicAgentsError` at the CLI boundary catches everything.
    """
    assert issubclass(UnknownModelError, AtomicAgentsError)


def test_ambiguous_backend_error_subclasses_atomic_agents_error():
    assert issubclass(AmbiguousBackendError, AtomicAgentsError)


def test_ambiguous_backend_error_exposes_model_and_candidates():
    """Programmatic consumers need machine-readable attributes — not just
    a formatted string — to remedy the ambiguity.
    """
    err = AmbiguousBackendError("gpt-5", ["openai", "azure-openai"])
    assert err.model == "gpt-5"
    assert err.candidates == ["openai", "azure-openai"]


def test_ambiguous_backend_error_pickle_roundtrip():
    """Exceptions thrown in delegated agents or helper-batch workers cross
    process boundaries via pickle. Default ``__reduce__`` would re-construct
    by passing the formatted message as the only arg — which crashes
    against this exception's two-arg ``__init__``. The custom
    ``__reduce__`` preserves the original constructor args.
    """
    import pickle
    err = AmbiguousBackendError("gpt-5", ["openai", "azure-openai"])
    restored = pickle.loads(pickle.dumps(err))
    assert restored.model == "gpt-5"
    assert restored.candidates == ["openai", "azure-openai"]
    assert isinstance(restored, AmbiguousBackendError)


def test_find_backend_treats_empty_preferred_provider_as_no_preference():
    """A model.md parser encountering a bare ``provider:`` line will pass
    an empty string. The registry must treat that as "no preference"
    rather than misleadingly reporting "no backend registered with
    provider_id ''".
    """
    a = _FakeBackend("anthropic", claims=["claude-"])
    register_llm_backend(a)
    # Falls through to the normal single-match resolution
    assert find_backend_for_model("claude-opus-4-7", preferred_provider="") is a
    # Whitespace-only is also treated as no-preference
    assert find_backend_for_model("claude-opus-4-7", preferred_provider="   ") is a


def test_re_register_emits_debug_log(caplog):
    """Re-registering a provider_id is intentional (RetryingLLMBackend
    wrapping per #81) but operators debugging "which backend handled my
    call?" deserve a hint. Debug-level so the default log volume is
    unchanged.
    """
    import logging
    original = _FakeBackend("anthropic", claims=["claude-"])
    register_llm_backend(original)
    with caplog.at_level(logging.DEBUG, logger="atomic_agents.llm"):
        replacement = _FakeBackend("anthropic", claims=["claude-"])
        register_llm_backend(replacement)
    assert any(
        "replacing registered backend" in rec.message
        and "anthropic" in rec.message
        for rec in caplog.records
    )


def test_raw_llm_response_is_one_class_across_import_paths():
    """The Opus subagent review caught that the original PR 1 draft
    created two classes (one in _llm.py, one in llm/backend.py) despite
    docstrings claiming a single re-export. Pin the contract: every
    documented import path resolves to the same class object.
    """
    from atomic_agents._llm import _RawLLMResponse as Old
    from atomic_agents.llm import _RawLLMResponse as New
    from atomic_agents.llm.backend import _RawLLMResponse as Canon
    assert Old is New is Canon
    # And the legacy construction shape (tool_uses as list of provider
    # dicts) still works — PR 2 tightens to canonical LLMToolUse instances.
    r = Old(text="x", input_tokens=1, output_tokens=1,
            tool_uses=[{"name": "f", "input": {}, "id": "tu_0"}])
    assert r.tool_uses[0]["name"] == "f"

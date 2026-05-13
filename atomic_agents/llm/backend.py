"""SyncLLMBackend Protocol — the contract every LLM provider implementation satisfies.

This is the missing protocol in the protocol-pattern series alongside
MemoryBackend (#57), LockBackend (#60), LogBackend (#61), PersonaBackend
(#62), AgentProfileBackend (#63), ToolRegistryBackend (#64), and
CorpusBackend (#65). Each Protocol decouples one storage / dispatch axis
so the framework's core stays small and alternate implementations are
welcome.

Three Protocols are reserved at this namespace:

- ``SyncLLMBackend`` (this PR) — synchronous call surface. v1 ships this.
- ``AsyncLLMBackend`` (reserved; not implemented) — async variant for
  multi-tenant HTTP serving. ``docs/TENSIONS.md:47`` records the
  sync-everywhere-today tradeoff as a planned future refactor.
- ``StreamingLLMBackend`` (reserved; not implemented) — yields output
  chunks for interactive UIs that want progressive rendering.

Splitting async / streaming into separate Protocols (rather than optional
methods on the core sync Protocol) is the codex P2 fix from the plan
review — mixing concerns made conformance-test discipline impossible.

Scaffolding PR: no backend implements this Protocol yet, and `agent.py`
+ `_llm.py` continue to use today's procedural dispatch. The Protocol
exists so PR 2 can introduce ``AnthropicLLMBackend`` against a stable
contract that PR 3's ``OpenAICompatibleLLMBackend`` and any future
third-party backend will conform to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .types import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)


@dataclass
class _RawLLMResponse:
    """Normalized LLM response shape — same across every backend.

    Re-housed from ``_llm._RawLLMResponse`` so future imports go through
    ``atomic_agents.llm`` instead of the soon-to-be-shim ``_llm``. The
    legacy import path keeps working via a re-export until the LLMBackend
    arc completes; new code should import from here.

    Fields:
        text: generated assistant text (excludes any tool_use blocks).
        input_tokens: prompt tokens billed by the provider.
        output_tokens: completion tokens billed by the provider.
        cache_hit_tokens: portion of input_tokens served from prompt cache
            (Anthropic's prompt-cache feature). Counted against the
            discounted cache-hit rate per ``PricingInfo.cache_hit_discount``.
            Zero on backends that don't expose cache hits.
        cache_miss_tokens: portion of input_tokens NOT served from cache.
            ``cache_hit_tokens + cache_miss_tokens == input_tokens`` on
            providers that report both; zero on providers that don't.
        raw: optional bag of provider-specific extras (Anthropic's
            ``stop_reason``, OpenAI's ``finish_reason``, reasoning_content
            from thinking-style Moonshot models per #146, etc.). Consumers
            that need provider details opt in; consumers that just want
            the normalized shape ignore.
        tool_uses: canonical tool_use blocks the model emitted. Empty when
            the model returned text only. Backends normalize provider
            shape to ``LLMToolUse`` instances inside ``call()``.
        reasoning_text: visible reasoning_content from thinking-style
            models (Kimi K2.x, o1-style). Default None for non-thinking
            models. Lands properly with #146; this field is reserved here
            so consumers can read it once the extraction is wired.
    """

    text: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    raw: dict[str, Any] | None = None
    # Each entry is shaped {"name": "...", "input": {...}, "id": "..."} today.
    # PR 2 (AnthropicLLMBackend + agent.py tool-dispatch refactor) tightens
    # this to `list[LLMToolUse]` when backends start constructing the
    # canonical type. Today the annotation matches `_llm._RawLLMResponse`
    # for drop-in compatibility — see `atomic_agents/_llm.py:25` (now a
    # re-export of this class).
    tool_uses: list[dict] = None  # type: ignore[assignment]
    reasoning_text: str | None = None

    def __post_init__(self) -> None:
        if self.tool_uses is None:
            self.tool_uses = []


@runtime_checkable
class SyncLLMBackend(Protocol):
    """v1 synchronous contract every LLM provider implementation must satisfy.

    Implementations must NOT subclass this Protocol — it's structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, SyncLLMBackend)`` to perform a method-presence
    check (not a signature check — signatures are static-typing's job).

    Backends are typically instantiated once at import time and
    registered via ``atomic_agents.llm.register_llm_backend()``. The
    registry routes ``agent.call()`` to the correct backend by
    matching ``model`` against each backend's ``supports_model()``.

    See ``docs/spec/31-llm-backend.md`` for the prose contract (lands
    with the spec PR, the final stage of the #87 arc).

    Mocking note: ``MagicMock(spec=SyncLLMBackend)`` does NOT pass
    ``isinstance(m, SyncLLMBackend)`` because ``provider_id`` is a
    property descriptor that ``@runtime_checkable`` inspects via
    ``hasattr`` against the class, not the instance. Tests should use
    a concrete fake class (see ``tests/test_llm_types_and_registry.py``
    ``_FakeBackend``) instead.
    """

    @property
    def provider_id(self) -> str:
        """Stable identifier — e.g., 'anthropic', 'openai', 'moonshot', 'azure-openai'.

        Used by the registry for conflict resolution: when ``model.md``
        specifies ``provider: <id>`` and multiple backends claim the
        same model, the matching ``provider_id`` wins. Treat as a
        backwards-compatibility surface — operators may pin against
        these strings in their config.
        """
        ...

    def supports_model(self, model_id: str) -> bool:
        """Does this backend handle the given model id?

        Should be a fast, side-effect-free check (typically a string
        prefix or regex). Multiple backends may legitimately return True
        for the same model id (e.g., Azure OpenAI + OpenAI-direct both
        claim ``gpt-5``); the registry resolves the conflict via
        ``model.md`` or raises ``AmbiguousBackendError``.
        """
        ...

    def capabilities(self, model_id: str) -> LLMCapabilities:
        """Per-model capability declaration — codex P2.

        Must be honest: a backend that claims ``cache_control=True``
        for a model MUST honor passed ``CacheDirective`` lists; one
        that claims ``vision=True`` MUST accept image content blocks at
        that model id. Conformance tests assert claim-vs-behavior parity.

        Raises ``UnknownModelError`` (or returns conservative defaults)
        when the backend doesn't recognize the model.
        """
        ...

    def pricing(self, model_id: str) -> PricingInfo | None:
        """Per-model pricing the backend optionally declares.

        Returns ``PricingInfo`` when the backend knows the price for
        this model; returns ``None`` when it doesn't — the caller
        (``_costs.calc_cost`` in PR 4) falls back to the framework's
        global ``_costs.PRICING`` table.

        This is the codex P2 fix that lets third-party backends ship
        pricing alongside their models without forking ``_costs.py``.
        """
        ...

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[LLMToolDefinition] | None = None,
    ) -> int:
        """Pre-flight token estimate for cost guardrails / batch reservation.

        Used by the cost gates (``_check_cost_guardrails``) and helper-batch
        reservation (``_check_batch_reservation``) to refuse an expensive
        call before paying its overhead. Should return the total input
        tokens the provider would bill for the given inputs.

        Implementations: prefer the provider's own token counter
        (anthropic SDK's ``count_tokens``, tiktoken for OpenAI). When no
        SDK helper exists, a heuristic that over-estimates by 10-20% is
        acceptable — guardrails are conservative-pessimistic by design.

        Note on the ``messages`` shape: today it is provider-shaped (the
        same list of dicts ``agent.py`` already builds and passes to
        ``call_llm``). A future canonical ``LLMMessage`` type would
        complete the abstraction; reserved for a follow-up extension of
        spec/31 once two backends are in production and the right shape
        is empirical, not aspirational.
        """
        ...

    def call(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        tools: list[LLMToolDefinition] | None = None,
        cache_directives: list[CacheDirective] | None = None,
    ) -> _RawLLMResponse:
        """Make a synchronous LLM call; return a normalized ``_RawLLMResponse``.

        Inside ``call()`` the backend:
        1. Translates ``tools`` (canonical) → provider tool definition format.
        2. Translates ``cache_directives`` → provider cache directives
           (or ignores when ``capabilities(model).cache_control is False``).
        3. Calls the provider.
        4. Translates provider tool_use blocks → ``LLMToolUse`` instances
           in the returned response.
        5. Populates ``cache_hit_tokens`` / ``cache_miss_tokens`` /
           ``reasoning_text`` when the provider exposes them.

        ``agent.call()`` never sees provider-shaped content; it sees the
        canonical types in ``_RawLLMResponse``. This is the codex P1 fix
        that makes third-party backends fully participate in tool loops.
        """
        ...

    def format_tool_results(
        self,
        tool_uses: list[LLMToolUse],
        tool_results: list[LLMToolResult],
        assistant_text: str = "",
    ) -> list[dict]:
        """Build the next-iteration message list for the provider's tool loop.

        Different providers want different shapes for "the model called
        tools; here are their results — continue":

        * Anthropic — echo the prior assistant turn (text + tool_use
          blocks) followed by a user turn with tool_result blocks.
        * OpenAI — assistant turn with ``tool_calls`` field followed by
          N tool-role messages, one per result.

        The Protocol takes all three pieces so the backend can build
        whichever provider shape it needs:

        * ``tool_uses`` — the model's tool_use blocks from the prior call
          (the response's ``tool_uses`` field).
        * ``tool_results`` — what came back when the agent executed each
          tool. Pairs with ``tool_uses`` by ``tool_use_id``.
        * ``assistant_text`` — the model's prior text content (the
          response's ``text`` field). Some providers want this echoed
          back, others don't; backend's call.

        Returns a list of dicts ready to append to the ``messages``
        argument of the next ``call()`` invocation. Empty
        ``tool_results`` → empty list.

        Signature note: PR 1 of #87 landed a single-arg version of this
        method; PR 2 extended it after AnthropicLLMBackend's
        implementation needed all three pieces to build the
        assistant-echo turn. No third-party consumers existed at the
        time of the change.
        """
        ...

"""VertexGeminiLLMBackend — reference implementation of SyncLLMBackend for Gemini on Vertex AI.

Wraps the google-genai SDK (``google-genai>=1.0``) with ``vertexai=True`` for
Gemini-family models accessed through Google Cloud's Vertex AI endpoint.

Authentication uses Application Default Credentials (ADC) only — no API key.
Operators must run ``gcloud auth application-default login`` locally or deploy
a service account. Express-mode API-key auth is deferred to #378.

Model namespace: all model ids must carry the ``vertex/`` prefix in ``model.md``
(e.g., ``vertex/gemini-2.5-flash``). This matches the ``moonshot/`` grammar and
keeps routing unambiguous — installing this backend never perturbs any existing
claude-* / gpt-* / moonshot/* deployment.

The backend is registered in ``llm/__init__.py``'s ``_ensure_default_backends``
behind a guarded try/except — missing ``google-genai`` logs at DEBUG (not
WARNING, so a deployment that never opted into Vertex sees no noise on first
``agent.call()``) and continues. The doctor (``check_vertex_credentials``) and
the cost gates surface the missing-SDK / missing-project condition loudly at
the point an operator actually selects a ``vertex/*`` model.

Hard dependency (optional): ``google-genai>=1.0`` (``[vertex]`` extra in
pyproject.toml). Instantiation raises ``AtomicAgentsError`` when the SDK is
missing so the registration guard catches it cleanly.

Scope: synchronous (``SyncLLMBackend``) only. Streaming is deferred to the
``StreamingLLMBackend`` Protocol slot, which is reserved but not yet
implemented. Vertex context caching uses a separate resource-based API
incompatible with the framework's ``CacheDirective`` pattern — ``cache_control``
is False for all Vertex Gemini models.

Vertex Anthropic (Claude-on-Vertex via AnthropicVertex) ships as a SEPARATE
backend in the immediate next PR — it is NOT this backend.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .._costs import PRICING
from ..exceptions import AtomicAgentsError
from .backend import _RawLLMResponse
from .types import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Per-family capability table (per ruling: vertex-cache-and-capabilities-honesty)
#
# cache_control=False for ALL Vertex Gemini models:
#   Vertex's context caching is a separate create-cache-resource API,
#   incompatible with the framework's ephemeral CacheDirective pattern.
#   Wiring Vertex context caching natively is tracked in #377.
#
# streaming=False: deferred to StreamingLLMBackend Protocol slot.
#
# vision=False for ALL Vertex Gemini models in this reference impl:
#   the Gemini models themselves are multimodal, but _messages_to_genai_contents
#   has no image-block translation path — an Anthropic-shape image block would
#   be silently JSON-stringified into a text Part. Advertising vision=True with
#   no behavior behind it violates spec/31 conformance rule 4 (every True claim
#   is backed by actual behavior). vision flips to True per-family once image
#   translation lands (tracked in #376).
#
# tools/tool_results=True for flash and pro; both support function calling.
#
# Context windows from Vertex AI documentation (2026-06):
#   gemini-2.5-flash: 1M input, 64K output (thinking models: extended output)
#   gemini-2.5-pro:   1M input, 65K output
#   gemini-2.0-flash: 1M input, 8192 output
#   gemini-2.0-flash-lite: 1M input, 8192 output (no function calling)
#
_VERTEX_GEMINI_CAPABILITIES: dict[str, dict] = {
    "vertex/gemini-2.5-flash": {
        "tools": True,
        "tool_results": True,
        "vision": False,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_536,
    },
    "vertex/gemini-2.5-pro": {
        "tools": True,
        "tool_results": True,
        "vision": False,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_536,
    },
    "vertex/gemini-2.0-flash": {
        "tools": True,
        "tool_results": True,
        "vision": False,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
    },
    "vertex/gemini-2.0-flash-lite": {
        # Flash-lite does NOT support function calling (as of Vertex GA docs 2026-06)
        "tools": False,
        "tool_results": False,
        "vision": False,
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
    },
}

# Conservative defaults for Vertex Gemini families not yet in the table.
# Tools=True is a safe forward-compat default for the flash/pro family pattern.
_VERTEX_DEFAULT_CAPABILITIES = {
    "tools": True,
    "tool_results": True,
    "vision": False,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 8_192,
}


def _resolve_vertex_family(model_id: str) -> dict | None:
    """Return the capability dict for a known vertex/ model id, or None.

    Exact-match first (``vertex/gemini-2.0-flash``), then prefix-match for
    dated variants (``vertex/gemini-2.0-flash-20260601`` → ``vertex/gemini-2.0-flash``).
    """
    if model_id in _VERTEX_GEMINI_CAPABILITIES:
        return _VERTEX_GEMINI_CAPABILITIES[model_id]
    # Prefix match — handles dated aliases like vertex/gemini-2.0-flash-20260601.
    # Match the LONGEST family prefix, not the first in dict order: a dated
    # flash-lite id (vertex/gemini-2.0-flash-lite-001) must resolve to the
    # flash-lite family (tools/vision False), NOT to the flash family it also
    # prefix-matches. Sorting by descending key length tries the most specific
    # family first — a capabilities()-honesty guard (spec/31 conformance rule 4).
    for family in sorted(_VERTEX_GEMINI_CAPABILITIES, key=len, reverse=True):
        if model_id.startswith(family + "-"):
            return _VERTEX_GEMINI_CAPABILITIES[family]
    return None


class VertexGeminiLLMBackend:
    """SyncLLMBackend implementation for Gemini models on Vertex AI.

    Uses ``google-genai>=1.0`` with ``vertexai=True``. Authentication is
    Application Default Credentials (ADC) only — no API key string.

    Construction is a pure import check (no network calls, no ADC resolution)
    following the lazy-construction pattern of AnthropicLLMBackend. This
    keeps ``import atomic_agents`` fast for operators who do not use Vertex
    (CLAUDE.md rule: don't slow subprocess spawns).

    ADC resolution and client construction are deferred to the first
    ``call()`` invocation via ``_build_client()``.

    Environment variables (resolved at construction time, following the
    Moonshot base_url pattern — single resolution, no per-call churn):
        GOOGLE_CLOUD_PROJECT  — GCP project id (required for Vertex endpoint)
        GOOGLE_CLOUD_LOCATION — GCP region (default: us-central1)
    """

    def __init__(self) -> None:
        # Pure import-presence check at construction — zero network calls.
        # This is the guard that _ensure_default_backends() catches as ImportError
        # when google-genai is not installed.
        try:
            import google.genai  # noqa: F401 — presence check only
        except ImportError as e:
            raise AtomicAgentsError(
                "google-genai SDK not installed; "
                "pip install 'atomic-agents-stack[vertex]' or "
                "pip install google-genai"
            ) from e

        # Resolve env vars once at construction (not per-call) to match the
        # Moonshot base_url single-resolution pattern. This prevents
        # mid-process env changes from silently routing calls to a different
        # project, and avoids per-call os.environ.get() overhead on GCP fleets.
        self._project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        self._location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

        if not self._project:
            # DEBUG, not WARNING, for parity with the missing-SDK registration
            # path in _ensure_default_backends: google-genai can be present
            # transitively on a machine that never uses Vertex and never sets
            # GOOGLE_CLOUD_PROJECT, in which case this constructor runs inside
            # _ensure_default_backends on first agent.call() for ANY model — a
            # home user with one Claude agent must not see a WARNING line for a
            # provider they never selected. The doctor (check_vertex_credentials)
            # surfaces the missing-project condition loudly at the point an
            # operator actually selects a vertex/* model; that is the documented
            # design. On Cloud Run / GKE the project is resolved from the
            # metadata server automatically; the env var is only needed on dev
            # machines.
            _logger.debug(
                "GOOGLE_CLOUD_PROJECT env var not set — VertexGeminiLLMBackend "
                "registered but Vertex calls will fail until the env var is set "
                "(resolved automatically from the metadata server on Cloud Run / "
                "GKE; needed explicitly on dev machines)."
            )

    # ────────────────────────────────────────────────────────────
    # SyncLLMBackend Protocol surface

    @property
    def provider_id(self) -> str:
        return "vertex-gemini"

    def supports_model(self, model_id: str) -> bool:
        """Match ``vertex/gemini-*`` model ids only.

        Scoped to ``vertex/gemini-`` (not the bare ``vertex/`` prefix) so
        vertex-anthropic (next PR) can use ``vertex/claude-`` without
        collision — critical for the two-backend design ruling.

        Registers forward-compatibly: any future ``vertex/gemini-*`` family
        routes through this backend without a table update; ``capabilities()``
        fills in conservative defaults for unknown families.
        """
        return model_id.startswith("vertex/gemini-")

    def capabilities(self, model_id: str) -> LLMCapabilities:
        """Per-model capability declaration — per-family honest table.

        cache_control=False for ALL Vertex Gemini models:
            Vertex context caching uses a separate resource API incompatible
            with the CacheDirective pattern. Wiring it natively is tracked in #377.

        streaming=False: deferred to StreamingLLMBackend Protocol.
        """
        caps = _resolve_vertex_family(model_id) or _VERTEX_DEFAULT_CAPABILITIES
        return LLMCapabilities(
            tools=caps["tools"],
            tool_results=caps["tool_results"],
            # cache_control is ALWAYS False for Vertex Gemini.
            # Vertex context caching is a create-cache-resource flow, not
            # a per-call CacheDirective flow. Claiming True here would cause
            # agent.py to pass CacheDirective lists that we'd silently ignore,
            # which violates spec/31 conformance rule 4.
            cache_control=False,
            streaming=False,  # deferred to StreamingLLMBackend Protocol slot
            vision=caps["vision"],
            max_input_tokens=caps["max_input_tokens"],
            max_output_tokens=caps["max_output_tokens"],
            usage_reporting=True,
            structured_output=False,
        )

    def pricing(self, model_id: str) -> PricingInfo | None:
        """Return per-model pricing from ``_costs.PRICING``.

        PRICING keys use the full ``vertex/`` prefix (e.g., ``vertex/gemini-2.0-flash``)
        matching exactly what operators write in model.md. This ensures calc_cost()
        receives the prefixed form and resolves without fallback.

        Returns None when the model id isn't priced; the cost machinery
        (``_costs.calc_cost``) then falls back to the highest known rates and
        logs a one-time WARNING. (The WARNING is emitted by ``calc_cost``, not
        by ``_fallback_pricing`` — and a None return here does not itself route
        to ``_fallback_pricing``; ``calc_cost`` has its own PRICING-miss branch.)
        """
        if model_id not in PRICING:
            return None
        rates = PRICING[model_id]
        return PricingInfo(
            input_per_million_usd=rates["input"],
            output_per_million_usd=rates["output"],
            cache_hit_discount=1.0,  # No cache discount — cache_control=False
        )

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[LLMToolDefinition] | None = None,
    ) -> int:
        """Estimate input tokens — conservative-pessimistic for cost guardrails.

        The google-genai SDK exposes ``client.models.count_tokens()`` but it
        requires the ``contents`` argument to be in genai Content format, not raw
        dicts. Rather than translate the message list (which would duplicate
        the translation logic in call()), we use the char/3 heuristic here
        (Gemini's sentencepiece tokenizer is more granular than GPT's — 3
        chars/token is safer than 4).

        The heuristic over-estimates for Latin-script text (conservative-
        pessimistic per CLAUDE.md rule #4 and spec/31 conformance rule 9). It
        has a known under-count edge on dense scripts (CJK), where Gemini's
        sentencepiece tokenizer can approach ~1-2 chars/token — the same edge
        the shipped OpenAI char/4 heuristic carries, and char/3 is strictly
        MORE conservative than char/4. Accepted as same-family behavior, not a
        new corner cut; tightening to a real tokenizer is deferred.

        This method NEVER calls the SDK: it is a pure char/3 heuristic with no
        network and no auth dependency. The google-genai ``count_tokens`` API
        requires ``contents`` already in genai Content format (not the raw
        message dicts this method receives), so calling it here would duplicate
        the call()-side translation. There is therefore no SDK error path to
        propagate — the estimate is always available regardless of ADC state.

        Args:
            system_prompt: The system prompt text.
            messages: Provider-shaped message dicts (same list passed to call()).
            tools: Optional list of tool definitions.

        Returns:
            Positive integer token estimate. Always >= 1.
        """
        # NOTE: We do NOT call the SDK here (would require auth + network).
        # The google-genai count_tokens method expects Contents in SDK format,
        # not raw dicts. Translation is non-trivial; heuristic is accurate
        # enough for the conservative-pessimistic design goal.
        char_count = len(system_prompt)
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                char_count += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        char_count += len(json.dumps(block))
            # This backend's OWN format_tool_results emits parts-shaped
            # continuation messages ({"role": "model"|"user", "parts": [...]})
            # with NO "content" key. Counting only "content" would make every
            # tool-call/result turn contribute ZERO characters, so the estimate
            # would UNDER-count exactly as a multi-iteration tool loop grows —
            # violating the conservative-pessimistic contract (CLAUDE.md rule #4,
            # spec/31 conformance rule 9). Account for parts here too.
            parts = m.get("parts")
            if isinstance(parts, list):
                for p in parts:
                    if isinstance(p, dict):
                        char_count += len(json.dumps(p))
        for t in tools or []:
            char_count += (
                len(t.name) + len(t.description) + len(json.dumps(t.input_schema))
            )
        # Gemini's sentencepiece tokenizer is denser than GPT's BPE; 3
        # chars/token over-estimates safely for Latin text. Dense scripts
        # (CJK) can under-count — accepted as same-family as the OpenAI char/4
        # heuristic (char/3 is the more conservative of the two).
        return max(1, char_count // 3)

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
        """Synchronous call to the Vertex AI Gemini endpoint.

        Translation notes vs Anthropic/OpenAI:
        - system_instruction: passed via ``GenerateContentConfig(system_instruction=...)``
          on the ``generate_content()`` config argument (NOT as a user-role
          message). Placing it in messages pollutes conversation history.
        - tool definitions: translated to SDK FunctionDeclaration objects wrapped
          in a Tool. JSON Schema ``title`` keys are stripped (Gemini's OpenAPI
          subset doesn't support them).
        - cache_directives: silently ignored (cache_control=False per capabilities()).
        - temperature/max_tokens: passed via GenerateContentConfig, not as
          top-level kwargs (google-genai SDK requirement).
        - token counts: from response.usage_metadata.prompt_token_count and
          candidates_token_count (NOT input_tokens/output_tokens like Anthropic).
          On Vertex AI, thinking/reasoning tokens are reported separately in
          usage_metadata.thoughts_token_count and are NOT included in
          candidates_token_count — they are added to output_tokens here so the
          gemini-2.5-flash / gemini-2.5-pro thinking models are billed in full.
        - tool_uses: from response.candidates[0].content.parts where
          part.function_call is not None. Synthetic IDs are minted from the
          response PART index (``f"call_{part_index}"``) because the genai SDK
          does not assign stable call IDs. The ids are NOT guaranteed
          contiguous-from-zero: an interleaved text part shifts the index, so
          the first tool_use may be ``call_1``. format_tool_results() does NOT
          re-mint — it echoes the same id verbatim via ``tu.id``, so the ids
          round-trip consistently regardless of contiguity.
        - cache_hit_tokens: always 0 (cache_control=False).
        """
        try:
            from google.genai import types as genai_types
        except ImportError as e:
            raise AtomicAgentsError(
                "google-genai SDK not installed; "
                "pip install 'atomic-agents-stack[vertex]'"
            ) from e

        client = self._build_client()

        # Strip the ``vertex/`` prefix before sending to the SDK.
        # The SDK expects the bare model id (e.g., ``gemini-2.0-flash``).
        actual_model = model[len("vertex/") :]

        # Translate canonical tools → Gemini SDK FunctionDeclaration.
        # Gemini's OpenAPI subset does not support the ``title`` JSON Schema
        # keyword — strip it to avoid validation errors.
        sdk_tools: list | None = None
        if tools:
            function_declarations = []
            for td in tools:
                schema = _strip_title_keys(td.input_schema)
                function_declarations.append(
                    genai_types.FunctionDeclaration(
                        name=td.name,
                        description=td.description,
                        parameters=schema,
                    )
                )
            sdk_tools = [genai_types.Tool(function_declarations=function_declarations)]

        # All generation params go through GenerateContentConfig — NOT as top-level
        # kwargs on generate_content(). The google-genai SDK requires this shape.
        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if sdk_tools:
            config_kwargs["tools"] = sdk_tools

        # Convert messages from the provider-shaped dicts that agent.py builds
        # to genai SDK Content objects.
        sdk_contents = _messages_to_genai_contents(messages, genai_types)

        response = client.models.generate_content(
            model=actual_model,
            contents=sdk_contents,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                **config_kwargs,
            ),
        )

        # Extract text + function_call parts from the candidates.
        # Gemini's response differs from Anthropic/OpenAI:
        # - Text is in parts[i].text
        # - Tool calls are in parts[i].function_call (NOT .type == 'tool_use')
        text_parts: list[str] = []
        tool_uses_dicts: list[dict] = []

        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = getattr(candidates[0].content, "parts", None) or []
            for i, part in enumerate(parts):
                fc = getattr(part, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    # Gemini does NOT issue stable call IDs — mint synthetic ones
                    # from the response PART index. NOTE: `i` enumerates ALL
                    # parts (text + function_call), so the ids are not
                    # contiguous-from-zero when a text part interleaves. That is
                    # fine: format_tool_results() does NOT re-mint — it echoes
                    # this exact id verbatim via tu.id (round-tripped through
                    # agent.py's tool loop), so the FunctionResponse always
                    # carries the same id minted here.
                    tool_uses_dicts.append(
                        {
                            "id": f"call_{i}",
                            "name": fc.name,
                            "input": dict(fc.args) if fc.args else {},
                        }
                    )
                else:
                    text = getattr(part, "text", None)
                    if text:
                        text_parts.append(text)

        text_out = "".join(text_parts)

        # Extract token counts from usage_metadata (NOT .usage.input_tokens).
        # Gemini SDK returns prompt_token_count and candidates_token_count.
        #
        # The backend advertises usage_reporting=True (capabilities()), so it
        # MUST report real token counts. Degrading to 0 on a real, billable
        # generation would (a) silently under-count the running cost total the
        # mid-loop + per-call cost gates trust (CLAUDE.md rule #4 — a paid call
        # escapes its guardrail accounting) and (b) write a $0 cost_usd audit
        # JSONL line for a paid call (CLAUDE.md rule #5). The Anthropic and
        # OpenAI backends access usage directly so a malformed response raises
        # loudly; this backend must fail loud the same way rather than report 0.
        usage_meta = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage_meta, "prompt_token_count", None)
        candidate_tokens = getattr(usage_meta, "candidates_token_count", None)
        # Thinking/reasoning tokens are reported SEPARATELY on Vertex AI
        # (vertexai=True): candidates_token_count does NOT include them, but
        # they are billed at the standard output rate. On the gemini-2.5-flash
        # and gemini-2.5-pro thinking models, omitting thoughts_token_count
        # under-counts output_tokens by the entire reasoning volume — the same
        # cost-honesty defect class as the usage-absent case below. Add them.
        # (On the direct Gemini API, candidates_token_count WOULD include
        # thinking tokens — but this backend is Vertex-only, so they are
        # always separate here.)
        thoughts_tokens = getattr(usage_meta, "thoughts_token_count", None) or 0
        produced_content = bool(text_out) or bool(tool_uses_dicts)
        # Fail loud when usage is missing OR half-populated OR reports ZERO
        # TOTAL billable output on a content-producing response. The is-None
        # case is a missing/incomplete usage block. The total-output==0 case is
        # a half-populated SDK shape that would feed 0 output tokens into
        # calc_cost — both silently disarm the cost guardrails (rule #4) and
        # write a $0 audit line for a billable generation (rule #5).
        #
        # The invariant is TOTAL billable output, not candidates_token_count
        # alone: on a Vertex thinking model (gemini-2.5-flash / -pro) the entire
        # output can be reasoning tokens, reported in thoughts_token_count with
        # candidates_token_count == 0. That response IS billable (thoughts at
        # the output rate) and must pass — so the zero-output guard sums both.
        # usage_reporting=True (spec/31 conformance rule 4) obligates us to
        # report real counts or raise, never degrade to 0.
        total_output_tokens = (candidate_tokens or 0) + thoughts_tokens
        if produced_content and (
            prompt_tokens is None
            or candidate_tokens is None
            or total_output_tokens == 0
        ):
            raise AtomicAgentsError(
                "Vertex Gemini response produced content but usage_metadata is "
                "missing, incomplete, or reports zero total output tokens "
                f"(prompt_token_count={prompt_tokens!r}, "
                f"candidates_token_count={candidate_tokens!r}, "
                f"thoughts_token_count={thoughts_tokens!r}). Reporting 0 tokens "
                "would silently disarm the cost guardrails and write a $0 audit "
                "line for a billable call — failing loud instead (usage_reporting="
                "True per spec/31 conformance rule 4)."
            )
        # Blocked/empty responses (no text, no tool_uses) skip the guard above:
        # produced_content is False. They are NOT treated as free — billing
        # flows through whatever usage_metadata reports: input_tokens =
        # prompt_token_count (the prompt is charged at the input rate when
        # present) and output_tokens = thoughts_token_count (a thinking model
        # that burned reasoning before being blocked/truncated — e.g.
        # finish_reason=MAX_TOKENS with no visible text — IS billed for those
        # thoughts at the output rate). The one tolerated under-count is the
        # input side of a blocked thoughts-only call: input_tokens falls back to
        # `prompt_tokens or 0` UN-guarded, so a half-populated usage block
        # (prompt_token_count=None) on a blocked response silently drops the
        # input charge. Blast radius is one suppressed input charge — accepted
        # because Vertex usually does not bill a prompt blocked before
        # generation — not an ongoing under-count.
        input_tokens = prompt_tokens or 0
        output_tokens = total_output_tokens

        # cache_hit_tokens is ALWAYS 0: cache_control=False, Vertex Gemini context
        # caching is a separate resource-based API not wired to CacheDirective.
        return _RawLLMResponse(
            text=text_out,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=0,
            cache_miss_tokens=0,
            raw=None,
            tool_uses=tool_uses_dicts,
        )

    def format_tool_results(
        self,
        tool_uses: list[LLMToolUse],
        tool_results: list[LLMToolResult],
        assistant_text: str = "",
    ) -> list[dict]:
        """Build Gemini's tool-loop continuation messages.

        Like the Anthropic and OpenAI backends, this returns TWO messages so the
        agent loop (which appends ONLY this output to the running history — see
        ``agent._build_tool_loop_messages``) has BOTH halves of the function
        call/response pair in the conversation:

        1. A ``model``-role turn echoing the model's prior turn — any interim
           ``assistant_text`` as a text part, plus one ``function_call`` part per
           non-atomic_capture tool_use. Gemini's API requires every
           ``function_response`` to be immediately preceded by the matching
           ``function_call`` in history; without this echo, iteration 2 of any
           tool loop sends a function_response with no originating call and
           Vertex rejects it (400 INVALID_ARGUMENT). The synthetic ids minted
           in ``call()`` are echoed here verbatim into each
           ``function_response``'s ``id`` field (threaded through to the SDK's
           ``FunctionResponse(id=...)`` by ``_messages_to_genai_contents``) so a
           tool_use round-trips to its result consistently.
        2. A ``user``-role turn with one ``function_response`` part per result.

        Both turns are emitted as provider-shaped dicts whose ``parts`` are
        re-serialized by ``_messages_to_genai_contents`` (which handles
        ``text``, ``function_call``, and ``function_response`` part dicts) on the
        next ``call()``.

        The continuation message shape::

            {"role": "model", "parts": [
                {"text": "<assistant_text>"},                     # if non-empty
                {"function_call": {"name": "<tool>", "args": {...}}},
            ]}
            {"role": "user", "parts": [
                {"function_response": {
                    "id": "<synthetic_call_id>",
                    "name": "<tool_name>",
                    "response": {"output": "<content>"}  # or {"error": "..."}
                }},
            ]}

        Empty ``tool_results`` → empty list. ``atomic_capture`` tool_uses are
        filtered from BOTH turns (handled by the capture path, not the tool
        loop) — exactly as the Anthropic/OpenAI backends do.
        """
        if not tool_results:
            return []

        # Build result lookup by tool_use_id for efficient pairing.
        result_by_id: dict[str, LLMToolResult] = {
            r.tool_use_id: r for r in tool_results
        }

        def _response_body(result: LLMToolResult) -> dict:
            """Serialize a result per the wire-byte parity discipline.

            - Error strings: passed through as-is (already prefixed "[tool error]").
            - Everything else: json.dumps with str() fallback.
            """
            if result.is_error and isinstance(result.content, str):
                return {"error": result.content}
            try:
                content_str = json.dumps(result.content)
            except (TypeError, ValueError):
                content_str = str(result.content)
            return {"output": content_str}

        # Model-role echo turn: interim text (if any) + one function_call per
        # non-atomic_capture tool_use that has a matching result.
        model_parts: list[dict] = []
        if assistant_text:
            model_parts.append({"text": assistant_text})

        response_parts: list[dict] = []
        matched_result_ids: set[str] = set()
        for tu in tool_uses:
            if tu.name == "atomic_capture":
                # atomic_capture is handled by the capture path — exclude from
                # the tool-loop continuation exactly as the Anthropic backend does.
                continue
            result = result_by_id.get(tu.id)
            if result is None:
                continue
            matched_result_ids.add(tu.id)

            model_parts.append(
                {"function_call": {"name": tu.name, "args": tu.input or {}}}
            )
            response_parts.append(
                {
                    "function_response": {
                        "id": tu.id,
                        "name": tu.name,
                        "response": _response_body(result),
                    }
                }
            )

        # Defensive, unreachable-in-normal-operation orphan path. agent.py builds
        # iter_tool_results from custom_tool_uses (agent.py:~4183), so every
        # result has a matching tool_use and matched_result_ids covers them all;
        # this loop fires only if the tool_use/tool_result pairing invariant is
        # ever perturbed. Its purpose is transmit-every-result (no computed
        # result silently dropped) — the SAME OUTCOME the Anthropic backend has,
        # but NOT the same mechanism: Anthropic emits a bare tool_result block
        # for an orphan, with no synthetic assistant-side tool_use echo. Here we
        # MUST synthesize a function_call (Gemini rejects a function_response
        # with no preceding function_call), and its name can only fall back to
        # the result id — Gemini would 400 INVALID_ARGUMENT on a function_call
        # naming a function it never declared. That is the accepted failure mode
        # for a broken-invariant call: fail loud on the wire rather than discard
        # the result silently. If this path ever becomes reachable, fix the
        # invariant upstream, not by softening this branch.
        for result in tool_results:
            if result.tool_use_id in matched_result_ids:
                continue
            orphan_name = result.tool_use_id or "unknown_tool"
            model_parts.append({"function_call": {"name": orphan_name, "args": {}}})
            response_parts.append(
                {
                    "function_response": {
                        "id": result.tool_use_id,
                        "name": orphan_name,
                        "response": _response_body(result),
                    }
                }
            )

        if not response_parts:
            return []

        return [
            {"role": "model", "parts": model_parts},
            {"role": "user", "parts": response_parts},
        ]

    # ────────────────────────────────────────────────────────────
    # Private helpers

    def _build_client(self):
        """Build a Vertex-mode genai client per call.

        Per-call construction sidesteps test-isolation issues (same pattern as
        AnthropicLLMBackend._build_client). The genai.Client() constructor is
        fast (no network call) with vertexai=True when ADC is configured.

        Raises AtomicAgentsError on import failure; propagates google.auth
        and google.api_core exceptions on ADC failure so cost guardrails see
        real failures rather than silently-wrong estimates.
        """
        try:
            import google.genai as genai
        except ImportError as e:
            raise AtomicAgentsError(
                "google-genai SDK not installed; "
                "pip install 'atomic-agents-stack[vertex]'"
            ) from e

        return genai.Client(
            vertexai=True,
            project=self._project,
            location=self._location,
        )


# ────────────────────────────────────────────────────────────
# Translation helpers


def _strip_title_keys(schema: dict) -> dict:
    """Recursively remove ``title`` keys from a JSON Schema dict.

    Gemini's OpenAPI-subset schema processor does not support ``title``
    and raises a validation error when it encounters them. The Anthropic
    backend passes ``input_schema`` through unmodified; this backend must
    strip before sending to the SDK.

    Returns a new dict (does not mutate the input).
    """
    if not isinstance(schema, dict):
        return schema
    result = {}
    for k, v in schema.items():
        if k == "title":
            continue
        if isinstance(v, dict):
            result[k] = _strip_title_keys(v)
        elif isinstance(v, list):
            result[k] = [
                _strip_title_keys(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _messages_to_genai_contents(messages: list[dict], genai_types) -> list:
    """Translate provider-shaped message dicts to genai SDK Content objects.

    Handles three message shapes encountered in the agent's message list:
    1. Standard text messages: {role, content: str} → Content(role, [Part(text)])
    2. Tool result continuations from format_tool_results:
       {role: "user", parts: [{function_response: {...}}]} → Content with FunctionResponse parts
    3. Messages with list content (vision + multi-part): {role, content: [...]} → Content with multiple Parts

    The 'system' role is excluded here — system_instruction is passed to
    GenerateContentConfig directly (not as a message in the contents list).
    Gemini uses 'user' and 'model' roles (not 'assistant').
    """
    contents = []
    for msg in messages:
        role = msg.get("role", "user")
        # Map 'assistant' → 'model' (Gemini's role name)
        if role == "assistant":
            role = "model"
        # Skip system messages — handled via system_instruction parameter
        if role == "system":
            continue

        content = msg.get("content")
        parts_list = msg.get("parts")

        # Case 1: format_tool_results() continuation — parts already in genai
        # shape. Handles the model-role echo turn (text + function_call parts)
        # and the user-role function_response turn that format_tool_results emits.
        if parts_list is not None and isinstance(parts_list, list):
            sdk_parts = []
            for p in parts_list:
                if not isinstance(p, dict):
                    continue
                if "function_response" in p:
                    fr = p["function_response"]
                    sdk_parts.append(
                        genai_types.Part(
                            function_response=genai_types.FunctionResponse(
                                id=fr.get("id", ""),
                                name=fr["name"],
                                response=fr["response"],
                            )
                        )
                    )
                elif "function_call" in p:
                    fc = p["function_call"]
                    sdk_parts.append(
                        genai_types.Part(
                            function_call=genai_types.FunctionCall(
                                name=fc["name"],
                                args=fc.get("args", {}),
                            )
                        )
                    )
                elif "text" in p:
                    sdk_parts.append(genai_types.Part(text=p["text"]))
            if sdk_parts:
                contents.append(genai_types.Content(role=role, parts=sdk_parts))
            continue

        # Case 2: plain string content
        if isinstance(content, str):
            contents.append(
                genai_types.Content(role=role, parts=[genai_types.Part(text=content)])
            )
            continue

        # Case 3: list content (multi-part: vision, tool_use/tool_result blocks
        # from pre-#87 code paths or other backends)
        if isinstance(content, list):
            sdk_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    sdk_parts.append(genai_types.Part(text=block.get("text", "")))
                elif btype == "tool_use":
                    # Model tool-use block (from assistant turn in history)
                    # Represented as FunctionCall in Gemini model turns
                    sdk_parts.append(
                        genai_types.Part(
                            function_call=genai_types.FunctionCall(
                                name=block.get("name", ""),
                                args=block.get("input", {}),
                            )
                        )
                    )
                # NOTE: Anthropic-shape "tool_result" list-content blocks are
                # deliberately NOT special-cased here. This backend routes its
                # own tool results through format_tool_results() -> the `parts`
                # branch (Case 1), which carries the originating function_call
                # name. An Anthropic tool_result block carries no tool name, so
                # translating it to a FunctionResponse(name="") would emit a
                # function_response that can never match a preceding
                # function_call — exactly the 400 INVALID_ARGUMENT shape the
                # whole two-message echo design exists to prevent (spec/31
                # §VertexGeminiLLMBackend format_tool_results bullet). The path
                # is unreachable for this backend's own tool
                # loop (grep confirms agent.py never builds Anthropic-shape
                # tool_result list-content for a Vertex call), so we let it fall
                # through to the best-effort json.dumps text fallback below
                # rather than emit spec-invalid wire output.
                else:
                    # Unknown / non-text block type — convert to text as
                    # best-effort (includes any stray "tool_result" block; see
                    # the note above for why it is not given a FunctionResponse).
                    sdk_parts.append(genai_types.Part(text=json.dumps(block)))
            if sdk_parts:
                contents.append(genai_types.Content(role=role, parts=sdk_parts))

    return contents

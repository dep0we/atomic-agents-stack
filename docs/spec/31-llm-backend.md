# 31 — LLMBackend Protocol

**Status:** locked (spec matches implementation as of v1.0).
**Origin:** [#87](https://github.com/dep0we/atomic-agents-stack/issues/87).
**Shipped across four PRs:** PR 1 (canonical types + Protocol contract + registry primitives), PR 2 (`AnthropicLLMBackend` + Claude dispatch routing), PR 2.5 (`agent.py` tool-dispatch refactor to canonical types), PR 3 (`OpenAICompatibleLLMBackend` + `MoonshotLLMBackend` + registry-only dispatch + `model.md` `provider:` parser).

## Overview

`atomic_agents/_llm.py` used procedural dispatch (`if model.startswith(...)`) for LLM provider routing. Adding a fourth provider (Gemini, Bedrock, Ollama) meant editing core. The `LLMBackend` Protocol replaces that with the same protocol-pattern shape PR #57 established for `MemoryBackend`: a Protocol contract, canonical types that decouple the agent from provider shapes, a registry, and reference implementations.

The shape is the same as the rest of the protocol-pattern series alongside Lock, Log, Persona, AgentProfile, ToolRegistry, and Corpus (spec/34) protocols. The agent runtime — `agent.call()`, the cost gates, the multi-turn tool loop — talks to LLM providers only through canonical types. Backends translate at their own boundaries. Third-party packages implementing the Protocol drop in without forking core.

The framework ships four reference backends (Anthropic, OpenAI, Moonshot, and Vertex Gemini). Operators wanting Bedrock, vLLM-local, or other providers either configure an `OpenAICompatibleLLMBackend` instance or ship a 200-line third-party `atomic-agents-<provider>` package satisfying the Protocol. The framework's own surface stays small and auditable.

LiteLLM-in-core was considered and rejected (see [the issue body](https://github.com/dep0we/atomic-agents-stack/issues/87) for the six-reason rationale). LiteLLM as a community-maintained third-party `atomic-agents-litellm` adapter is welcome.

## Module layout

```
atomic_agents/llm/
├── __init__.py        # registry: register_llm_backend / find_backend_for_model / etc.
├── types.py           # canonical types: LLMToolDefinition, LLMToolUse, LLMToolResult,
│                      # CacheDirective, LLMCapabilities, PricingInfo
├── backend.py         # SyncLLMBackend Protocol + _RawLLMResponse
├── anthropic.py       # AnthropicLLMBackend reference implementation
├── openai_compat.py   # OpenAICompatibleLLMBackend (config-driven; OpenAI direct)
├── moonshot.py        # make_moonshot_backend factory over openai_compat
└── vertex_gemini.py   # VertexGeminiLLMBackend reference implementation (issue #345)
```

Mirrors `atomic_agents/memory/{__init__.py, backend.py, filesystem.py}`. The new piece is `types.py` — memory's canonical types live inline in `backend.py` because they're simpler.

## Canonical request/response types

The agent layer never sees provider-specific content blocks, tool_call shapes, or message structures. Every call out to an LLM is mediated by these types. Backends translate at their `call()` boundary.

### `LLMToolDefinition` — outbound tool spec

```python
@dataclass(frozen=True)
class LLMToolDefinition:
    name: str
    description: str
    input_schema: dict        # JSON Schema
    strict: bool = False      # OpenAI structured-output mode opt-in
```

The framework's `ToolRegistry` produces `list[LLMToolDefinition]` via `to_canonical_definitions()`. Each backend translates to its provider format inside `call()` — Anthropic's `{name, description, input_schema}` or OpenAI's `{type: function, function: {...}}` wrapper.

Frozen for immutability + thread-safety; not hashable (the `input_schema` dict is nested-mutable). Consumers that need a key derive one from `name`.

### `LLMToolUse` — inbound tool_use block

```python
@dataclass(frozen=True)
class LLMToolUse:
    id: str
    name: str
    input: dict       # parsed dict — OpenAI's JSON-string is decoded at backend boundary
```

Returned by backends in `_RawLLMResponse.tool_uses`. Backends are responsible for parsing OpenAI's JSON-string `arguments` and Anthropic's already-dict `input` into the same canonical shape.

### `LLMToolResult` — outbound tool result for the next call

```python
@dataclass(frozen=True)
class LLMToolResult:
    tool_use_id: str
    content: str | dict
    is_error: bool = False
```

Agent.py produces this after each tool execution. Backends serialize at their `format_tool_results()` boundary — Anthropic gets one user-role message with `tool_result` content blocks; OpenAI gets N `role: tool` messages each with `tool_call_id`.

### `CacheDirective` — multi-breakpoint cache intent

```python
@dataclass(frozen=True)
class CacheDirective:
    breakpoint_id: str        # e.g., "system-persona", "system-tools"
    ttl: Literal["ephemeral", "1h"] = "ephemeral"
```

Preserves spec/04's layered cache model. Backends that don't support cache_control (OpenAI today) silently ignore the directives. Anthropic maps them to `cache_control` blocks on the system prompt.

### `LLMCapabilities` — per-model capability declaration

```python
@dataclass(frozen=True)
class LLMCapabilities:
    tools: bool
    tool_results: bool
    cache_control: bool
    streaming: bool
    vision: bool
    max_input_tokens: int
    max_output_tokens: int
    usage_reporting: bool
    structured_output: bool
```

**Per-MODEL, not per-backend.** The same backend (`OpenAICompatibleLLMBackend`) can serve a tool-capable model and a vision-capable model; a flat `supported_capabilities() -> set[str]` cannot honestly describe both. Callers consult before making a call to know whether to send tools, cache directives, or image content. A False claim disables that path; a True claim means the backend MUST accept and honor it.

### `PricingInfo` — optional per-model pricing

```python
@dataclass(frozen=True)
class PricingInfo:
    input_per_million_usd: float
    output_per_million_usd: float
    cache_hit_discount: float = 0.10
```

`_costs.PRICING` is the framework-wide fallback. A backend that knows its own pricing (private API, third-party endpoint, per-tenant rates) returns it via `SyncLLMBackend.pricing(model_id)`. The caller prefers backend-provided pricing and falls back to the framework table when the backend returns `None`.

### `_RawLLMResponse` — normalized LLM response

```python
@dataclass
class _RawLLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    raw: dict[str, Any] | None = None
    tool_uses: list[dict] = field(default_factory=list)
    reasoning_text: str | None = None
```

Same shape across every backend. `tool_uses` is currently `list[dict]` (matches the pre-#87 normalized shape) — a future cleanup tightens to `list[LLMToolUse]` once external callers have migrated. `reasoning_text` is reserved for thinking-style models (Kimi K2.x via [#146](https://github.com/dep0we/atomic-agents-stack/issues/146)).

## Protocol surface

```python
@runtime_checkable
class SyncLLMBackend(Protocol):
    @property
    def provider_id(self) -> str: ...

    def supports_model(self, model_id: str) -> bool: ...

    def capabilities(self, model_id: str) -> LLMCapabilities: ...

    def pricing(self, model_id: str) -> PricingInfo | None: ...

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[LLMToolDefinition] | None = None,
    ) -> int: ...

    def call(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        tools: list[LLMToolDefinition] | None = None,
        cache_directives: list[CacheDirective] | None = None,
    ) -> _RawLLMResponse: ...

    def format_tool_results(
        self,
        tool_uses: list[LLMToolUse],
        tool_results: list[LLMToolResult],
        assistant_text: str = "",
    ) -> list[dict]: ...
```

**Implementations must NOT subclass the Protocol** — it's structural. Implementations satisfy it by exposing the methods with the documented behavior. The `@runtime_checkable` decorator enables `isinstance(obj, SyncLLMBackend)` method-presence check (signatures stay static-typing's job).

**Mocking note:** `MagicMock(spec=SyncLLMBackend)` does NOT pass `isinstance` because `provider_id` is a property descriptor — Python's `@runtime_checkable` checks descriptor presence at the class, not instance. Tests use concrete fake classes.

### Per-method semantics

- **`provider_id`** — stable identifier (`"anthropic"`, `"openai"`, `"moonshot"`, `"azure-openai"`, ...). Operators pin against these strings in `model.md`'s `provider:` field, so they're a backwards-compatibility surface.

- **`supports_model(model_id)`** — fast, side-effect-free predicate. Multiple backends may legitimately return True for the same model id (Azure OpenAI + OpenAI-direct both claim `gpt-5`); the registry resolves via `preferred_provider`. Forward-compat property: a backend SHOULD match by family prefix (e.g., any `claude-*`) so a future model id from the same provider routes correctly even before its capability metadata is added; `capabilities()` falls back to conservative defaults for unknown family members.

- **`capabilities(model_id)`** — per-model truth. A backend that claims `cache_control=True` MUST honor `CacheDirective` lists; one that claims `vision=True` MUST accept image content blocks. Conformance tests assert claim-vs-behavior parity.

- **`pricing(model_id)`** — returns `PricingInfo` when the backend knows its model's price; `None` to defer to `_costs.PRICING`. Lets third-party backends ship pricing alongside models without forking `_costs.py`.

- **`count_tokens(...)`** — pre-flight token estimate for cost guardrails and batch reservation. Prefer the provider's own counter (Anthropic SDK's `count_tokens`, tiktoken for OpenAI). When no SDK helper exists, a heuristic that over-estimates by 10-20% is acceptable — guardrails are conservative-pessimistic by design. The `messages` shape is provider-shaped today (no `LLMMessage` canonical type yet; reserved for a future spec extension once two backends operate in production).

- **`call(...)`** — the synchronous dispatch. Translates canonical `tools` → provider format; calls the provider; translates provider tool_use blocks → `LLMToolUse` dicts in `_RawLLMResponse.tool_uses`; populates `cache_hit_tokens`/`cache_miss_tokens` when the provider exposes them. Backends MAY ignore `cache_directives` when `capabilities(model).cache_control is False`. The agent layer never sees provider-shaped content blocks.

- **`format_tool_results(...)`** — builds the next-iteration message list for the provider's tool loop. Different providers want different shapes:
  - Anthropic: echo the prior assistant turn (text + tool_use blocks) + user turn with tool_result blocks.
  - OpenAI: assistant turn with `tool_calls` + N `role: tool` messages.

  The Protocol takes all three pieces (`tool_uses`, `tool_results`, `assistant_text`) so the backend can build whichever shape it needs. Empty `tool_results` → empty list. `atomic_capture` tool_uses are filtered from the echo (handled by the capture path, not the loop).

### Wire-byte parity discipline

`format_tool_results` and `call` serialize tool-result content with the same rules across backends — `json.dumps` with `str()` fallback for non-JSON-serializable outputs (datetime, custom classes, bytes). String error content (already prefixed `[tool error]`) passes through verbatim. This matches the pre-#87 `tools.build_tool_result_blocks_*` helpers byte-for-byte so operator JSONL transcripts before/after the migration diff cleanly.

Tool errors propagate `is_error: True` on Anthropic tool_result blocks (Anthropic's documented recovery signal). OpenAI uses the prefix-only convention; alignment is tracked as a possible future improvement.

## Reserved future Protocols

`AsyncLLMBackend` and `StreamingLLMBackend` are reserved namespace claims. They are NOT implemented in v1. The point of reserving them is to make clear that the sync Protocol does NOT belong to be conflated with async or streaming concerns — those will be separate Protocol surfaces with separate spec docs when they land.

- **`AsyncLLMBackend`** — async variant of `call()` for multi-tenant HTTP serving. `docs/TENSIONS.md:47` records sync-everywhere-today as a planned future refactor.
- **`StreamingLLMBackend`** — yields `LLMStreamEvent` chunks for interactive UIs.

Mixing async / streaming into the core sync Protocol made conformance-test discipline impossible (codex P2 in the plan review).

## Backend registration + conflict resolution

```python
from atomic_agents.llm import (
    register_llm_backend, unregister_llm_backend,
    get_backend, iter_registered_backends,
    find_backend_for_model,
)
```

The registry is process-local module state keyed by `provider_id`. Lazy default initialization: the framework's four reference backends register on the first `find_backend_for_model` call rather than at module import. Module-import-time registration was the original plan but it broke a timing-sensitive multiprocessing test (the `anthropic` import added ~300ms to subprocess startup).

Third-party packages register at their own import time, typically:

```python
# atomic_agents_gemini/__init__.py
from atomic_agents.llm import register_llm_backend
from .gemini import GeminiLLMBackend
register_llm_backend(GeminiLLMBackend())
```

### `find_backend_for_model(model, preferred_provider=None)`

Resolution rules:

1. If `preferred_provider` is given (typically from `model.md`'s `provider:` field), return that backend exclusively — even if other backends also claim the model. Raise `UnknownModelError` when the named provider isn't registered, or `AmbiguousBackendError` when the named provider doesn't support the model.
2. Otherwise, collect every backend whose `supports_model(model)` returns True.
3. Zero matches → `UnknownModelError`.
4. Exactly one match → return it.
5. More than one match → `AmbiguousBackendError` listing all candidate `provider_id` values, hinting at the `model.md` fix.

Empty / whitespace `preferred_provider` is treated as `None` (no preference) so a bare `provider:` line in `model.md` doesn't fail with a misleading "no backend registered with provider_id ''" error.

### `model.md` `provider:` field

Operators disambiguate ambiguous model ids via an optional line in `model.md`:

```markdown
## Default model

gpt-5

provider: azure-openai
```

Parsed by `atomic_agents._model.parse_model_md` into `AgentConfig.provider`. `agent.py` threads it as `preferred_provider=` through every `_llm.call_llm` call site AND through the tool-loop continuation's `find_backend_for_model` lookup. YAML-block aware: a `provider:` key inside a fenced ```yaml block is ignored (it belongs to that block's config, not the framework's LLMBackend disambiguator).

## Reference implementations

### `AnthropicLLMBackend` (`atomic_agents/llm/anthropic.py`)

- `provider_id = "anthropic"`
- `supports_model(m)` → any `m.startswith("claude-")` (forward-compat for future Claude releases; `capabilities()` falls back to conservative defaults for unknown families).
- Capability table for the three known families (opus / sonnet / haiku — with their dated aliases): tools=True, cache_control=True, streaming=False (sync only), vision=True, per-family max_input/output_tokens.
- `pricing(model_id)` delegates to `_costs.PRICING` (avoids parallel-table drift).
- `count_tokens(...)` uses anthropic SDK's `messages.count_tokens` when available; falls back to 4-chars/token heuristic on `AttributeError` (older SDKs). Other errors propagate so cost guardrails see real failures, not silently-wrong estimates.
- `call(...)` wraps `client.messages.create`. Cache directives → single `cache_control: ephemeral` on the system block.
- `format_tool_results(...)` builds Anthropic's two-message continuation.
- Hard dependency: `anthropic>=0.40`.

### `OpenAICompatibleLLMBackend` (`atomic_agents/llm/openai_compat.py`)

Configurable class — one class handles OpenAI direct, Azure OpenAI (when wired), Moonshot, Together, vLLM-local, and any other endpoint conforming to OpenAI's `chat.completions.create` contract.

Constructor parameters:

```python
OpenAICompatibleLLMBackend(
    provider_id: str,                              # stable identifier
    key_spec: KeySpec,                             # env vars + Keychain entry + config-file key
    model_namespace: Callable[[str], bool],        # predicate matching model ids
    model_transform: Callable[[str], str] = ...,   # optional rewrite before SDK call
    base_url: str | None = None,                   # HTTP endpoint (None = openai SDK default)
    capability_hooks: dict[str, LLMCapabilities] | None = None,  # per-model overrides
)
```

`KeySpec` carries the three sources `_llm._get_key` tries: env vars (first non-empty wins), macOS Keychain by name, `~/.config/atomic_agents/keys.json` by key. Matches spec/01 secrets-handling.

> **As of spec/38 (issue #340):** key resolution routes through the registered `SecretBackend`. `FilesystemSecretBackend` preserves the env vars → Keychain → keys.json order. `_llm._get_key()` is now a thin redirect wrapper that calls `get_default_secret_backend().resolve_with_spec(env_vars, keychain_name, config_key)`, forwarding the full `KeySpec` triple so every env-var alias plus the caller-supplied keychain service name and `keys.json` key are honored (preserving backward compatibility for custom `OpenAICompatibleLLMBackend` registrations with non-default `keychain_name`/`config_file_key`). Backends that do not expose `resolve_with_spec` (future alternate implementations) fall back to `SecretBackend.get(env_vars[0])`; note that `resolve_with_spec` is an internal `FilesystemSecretBackend` method, not part of the public Protocol surface. `KeySpec` is retained as a construction convenience type but no longer contains key-resolution logic; `_resolve_key()` in `OpenAICompatibleLLMBackend` delegates through `_get_key()` (which routes to `resolve_with_spec`). See spec/38 §"Lookup order".

`make_openai_backend()` ships an OpenAI-direct instance (matches `gpt-*` model ids, OpenAI default endpoint, framework's `ATOMIC_AGENTS_OPENAI_KEY` / `OPENAI_API_KEY` env vars).

### `MoonshotLLMBackend` (`atomic_agents/llm/moonshot.py`)

`make_moonshot_backend()` factory configures `OpenAICompatibleLLMBackend` with Moonshot's specifics: matches `moonshot/*` model ids, strips the prefix before SDK call, region-aware `base_url` (`ATOMIC_AGENTS_MOONSHOT_BASE_URL` → `MOONSHOT_BASE_URL` → `https://api.moonshot.cn/v1` default).

The `base_url` is resolved once at backend construction (lazy on first `find_backend_for_model` lookup), not per-call. Operators who want to override after that initial lookup must restart the process or set the env var before importing `atomic_agents`.

### `VertexGeminiLLMBackend` (`atomic_agents/llm/vertex_gemini.py`)

First-party backend for Gemini models accessed through Google Cloud's Vertex AI endpoint. Ships in issue [#345](https://github.com/dep0we/atomic-agents-stack/issues/345).

- `provider_id = "vertex-gemini"`
- `supports_model(m)` → any `m.startswith("vertex/gemini-")` (scoped to `gemini-` not bare `vertex/` to leave room for the upcoming `vertex/claude-*` backend without prefix collision).
- Capability table (per-family, honest): tools=True, tool_results=True for flash/pro families; cache_control=**False** for all Vertex Gemini models (Vertex context caching uses a separate resource-based API incompatible with the `CacheDirective` pattern — tracked in [#377](https://github.com/dep0we/atomic-agents-stack/issues/377)); streaming=False (deferred to `StreamingLLMBackend`); vision=**False** for all Vertex Gemini models (the models are multimodal, but `_messages_to_genai_contents` has no image-block translation path yet — advertising vision=True with no behavior behind it would violate conformance rule 4; flips to True per-family once image translation lands, tracked in [#376](https://github.com/dep0we/atomic-agents-stack/issues/376)); per-family `max_input_tokens` / `max_output_tokens` from Vertex docs.
- `pricing(model_id)` delegates to `_costs.PRICING` keyed under the full `vertex/` prefix (e.g., `vertex/gemini-2.0-flash`) — exactly matching what operators write in `model.md`. Supported entries: `vertex/gemini-2.5-flash`, `vertex/gemini-2.5-pro`, `vertex/gemini-2.0-flash`, `vertex/gemini-2.0-flash-lite`.
- `count_tokens(...)` uses char/3 heuristic (conservative-pessimistic; Gemini's sentencepiece tokenizer is denser than GPT's BPE — 3 chars/token is safer than 4).
- `call(...)` uses `google.genai.Client(vertexai=True).models.generate_content()`. Key translation differences from Anthropic/OpenAI: system prompt via `GenerateContentConfig.system_instruction` (NOT a message in the contents list); tool definitions via `FunctionDeclaration` objects; token counts from `response.usage_metadata.prompt_token_count` / `candidates_token_count`. On Vertex AI, thinking/reasoning tokens are reported separately in `usage_metadata.thoughts_token_count` and are NOT included in `candidates_token_count`, so they are added to `output_tokens` — without this addition the `gemini-2.5-flash` / `gemini-2.5-pro` thinking models under-count output (and under-charge) by the entire reasoning volume. Synthetic call IDs are minted from the response part index (`call_<part_index>`) because the SDK does not issue stable IDs; they are NOT guaranteed contiguous-from-zero (an interleaved text part shifts the index), and `format_tool_results` echoes the same id verbatim via `tu.id` rather than re-minting. `cache_hit_tokens` is always 0.
- `format_tool_results(...)` builds **two** messages mirroring the Anthropic/OpenAI two-turn pattern — a `model`-role echo turn carrying interim `assistant_text` plus one `function_call` part per non-`atomic_capture` tool_use, followed by a `user`-role turn with one `function_response` part per result. The model-role echo is required: Vertex requires every `function_response` to be immediately preceded by its matching `function_call` in history, else iteration 2 of a tool loop is rejected `400 INVALID_ARGUMENT`. (Differs from OpenAI's N `role:tool` messages; differs from Anthropic's tool_result blocks.)
- Auth: Application Default Credentials (ADC) only in PR 1. Express-mode API-key auth deferred to [#378](https://github.com/dep0we/atomic-agents-stack/issues/378). Operators set `GOOGLE_CLOUD_PROJECT` (required on dev machines; auto-resolved on Cloud Run / GKE) and optionally `GOOGLE_CLOUD_LOCATION` (default: `us-central1`).
- `doctor check_vertex_credentials()` resolves ADC + mints an OAuth token via `credentials.refresh(Request())` — proves credentials are usable without a billable LLM call.
- Optional dependency: `google-genai>=1.0` (`[vertex]` extra in pyproject.toml).

## Tool definition / tool result translation

Backends own all translation between canonical and provider shapes. The agent layer hands `list[LLMToolDefinition]` in; the backend's `call()` translates to the provider's tools schema; provider response tool_use blocks come back as `list[dict]` in `_RawLLMResponse.tool_uses` (normalized to `{id, name, input}` regardless of provider).

For the tool-loop continuation, `format_tool_results` receives canonical `list[LLMToolUse]` + `list[LLMToolResult]` + `assistant_text` and produces the provider-specific message list to extend.

A transitional adapter `_llm._to_canonical_tool_defs` accepts legacy provider-shape dicts (Anthropic's `{name, description, input_schema}` AND OpenAI's `{type, function: {...}}` shape) at `_llm.call_llm`'s entry boundary so external code that pinned to the pre-#87 API continues to work without modification. A future cleanup PR removes this acceptance once every external caller has migrated to canonical.

## Exceptions

```python
class UnknownModelError(AtomicAgentsError):
    """No registered backend supports the requested model id."""

class AmbiguousBackendError(AtomicAgentsError):
    """Multiple registered backends claim the same model id and no
    `preferred_provider` was given. The `candidates` attribute lists
    the conflicting provider_id values."""
    model: str
    candidates: list[str]
```

Both subclass `AtomicAgentsError` so a single `except AtomicAgentsError` at the CLI boundary catches everything. `AmbiguousBackendError` implements `__reduce__` so it survives pickle round-trips (multiprocessing pools, concurrent.futures workers).

## Conformance requirements

A backend conforms to the Protocol when:

1. **All seven methods are present** with the documented signatures. `isinstance(backend, SyncLLMBackend)` returns True.
2. **`provider_id` is stable** across the process lifetime (not derived from environment that may change).
3. **`supports_model` is side-effect-free** and fast (no network call, no Keychain lookup, no SDK initialization).
4. **`capabilities(model_id)` is honest** — every True claim is backed by actual behavior. `cache_control=True` → backend respects `cache_directives`. `vision=True` → backend accepts image content blocks. `tool_results=True` → backend supports multi-turn tool loops.
5. **`call()` returns `_RawLLMResponse`** with non-negative `input_tokens` and `output_tokens`, normalized `tool_uses` (each `{id, name, input}` with `input` as a dict), and the model's textual output in `text`.
6. **`call()` translates canonical tools** to provider format internally — callers pass `list[LLMToolDefinition]`, not provider dicts.
7. **`format_tool_results()` produces provider-correct messages** that the next `call()` invocation can consume (assistant-echo + tool-result for Anthropic; assistant-with-tool_calls + tool-role for OpenAI).
8. **`pricing(model_id)` returns `PricingInfo` or `None`** — never raises.
9. **`count_tokens(...)` returns a positive integer** for any valid request. May over-estimate (conservative-pessimistic for cost guardrails) but never zero.
10. **Wire-byte parity for tool-result serialization** — `json.dumps` with `str()` fallback for non-JSON-serializable outputs; error strings pass through verbatim; `is_error` flag honored.

Conformance test suite lives in `tests/test_llm_protocol_conformance.py` (this PR). Tests parameterize over registered backends and assert each backend honors the contract.

## Call-site migration reference

Pre-#87 procedural dispatch:

```python
raw = _llm.call_llm(
    model="claude-haiku-4-5",
    system_prompt=sys,
    messages=msgs,
    tools=[_capture.anthropic_tool_definition()],  # provider-shape dict
)
```

Post-#87 canonical dispatch (same call, internally routed through the registry):

```python
raw = _llm.call_llm(
    model="claude-haiku-4-5",
    system_prompt=sys,
    messages=msgs,
    tools=[_capture.canonical_tool_definition()],  # LLMToolDefinition
    preferred_provider=cfg.provider,               # from model.md
)
```

The pre-#87 dict-shape `tools` argument also works (transitional adapter); the canonical version is the recommended path going forward.

## Open questions for v2 / future spec extensions

- **`LLMMessage` canonical type.** Today `messages` is `list[dict]` (provider-shaped). A canonical message type would complete the abstraction. Reserved until two backends are in production and the right shape is empirical, not aspirational.
- **`top_p`, `stop_sequences`, structured-output schema** on `SyncLLMBackend.call()`. Pre-#87 `_llm.call_llm` didn't expose these either; tracked as [#148](https://github.com/dep0we/atomic-agents-stack/issues/148).
- **Per-tool `cache_breakpoint`** on `LLMToolDefinition` — Anthropic's "cache the tools block" pattern. Tracked as [#150](https://github.com/dep0we/atomic-agents-stack/issues/150).
- **Reasoning-content extraction** for Kimi K2.x and other thinking-style models. Tracked as [#146](https://github.com/dep0we/atomic-agents-stack/issues/146).
- **Resilience composition** — issue [#81](https://github.com/dep0we/atomic-agents-stack/issues/81)'s retry/timeout/429 handling composes as a `RetryingLLMBackend` wrapper class wrapping any `SyncLLMBackend`. Any backend gets resilience by composition.

These are tracked extensions, not v1 contract gaps. The current Protocol is locked.

## References

- [#87](https://github.com/dep0we/atomic-agents-stack/issues/87) — the parent issue with full design rationale and the LiteLLM rejection
- [Spec 20 — MemoryBackend Protocol](20-memory-backend.md) — the protocol-pattern template
- [Spec 04 — Runtime Assembly](04-runtime-assembly.md) — system-prompt assembly + cache breakpoints
- [Spec 17 — Tools](17-tools.md) — ToolRegistry; canonical tool definitions
- [CLAUDE.md](../../CLAUDE.md) Principles #2 (Protocols, not subclassing), #4 (Cost is first-class), #10 (The spec is the product), #11 (Codex review in rounds), #14 (Backward compatibility by default)

# spec/46 — EmbeddingBackend Protocol

**Status:** DRAFT (PR 2 of the 3-PR #200 arc; locks at PR 3)
**Issue:** [#200](https://github.com/dep0we/atomic-agents-stack/issues/200)
**Implementer Contract MUSTs:** 9 <!-- conformance tests: TBD at lock -->
**Arc:** PR 2 ships Protocol + reference impl + spec + cost helpers (DRAFT).
PR 3 wires pgvector ingestion sites, adds registry, locks this spec.

---

## Overview

`EmbeddingBackend` is the nineteenth open Protocol in the protocol-pattern
series. It abstracts the embedding provider (OpenAI, local sentence-transformers,
Ollama) so both `PgvectorMemoryBackend` (#258 PR 2) and `PgvectorCorpusBackend`
(#258 PR 3) share a single injected backend without duplicating provider logic.

The standalone use case — constructing any vector-capable storage backend —
is the primary justification identified when `EmbeddingBackend` was reconsidered
at issue #200 after spec/34 scope analysis. spec/34's "Out of scope" table
(LOCKED) recorded "no standalone use case identified" at its lock time; that
rationale is superseded for this use case. The superseding note is recorded
here (DRAFT, in-scope) rather than amended into the LOCKED spec/34 — the
normative reconciliation of spec/34 + spec/20 lands in PR 3 when the pgvector
wiring ships and those specs naturally take their addenda.

### Module layout

```
atomic_agents/embedding/
    __init__.py        # public exports: Protocol, EmbeddingCapabilities,
                       # OpenAIEmbeddingBackend, EMBEDDING_PRICING, calc_embedding_cost
    backend.py         # EmbeddingBackend Protocol + EmbeddingCapabilities dataclass
    openai.py          # OpenAIEmbeddingBackend reference implementation
tests/
    stub_embedding.py          # StubEmbeddingBackend (test-only; never in production)
    test_embedding_protocol_conformance.py  # parametrized conformance suite
    test_openai_embedding.py               # OpenAI impl-specific tests
```

### Cost accounting (`atomic_agents/_costs.py`)

```python
EMBEDDING_PRICING: dict[str, float]   # input-only, per-token; SEPARATE from PRICING (public)
calc_embedding_cost(model_id, input_tokens) -> tuple[float, bool]   # (public)
_embedding_fallback_rate() -> float   # PRIVATE helper (leading underscore;
                                      # NOT in embedding.__all__): max known
                                      # embedding rate (NOT chat max)
```

(`EMBEDDING_PRICING` and `calc_embedding_cost` are the only cost symbols
re-exported from `atomic_agents.embedding`; `_embedding_fallback_rate` is a
private implementation helper and is intentionally NOT part of the public
import surface.)

---

## Protocol surface

```python
@runtime_checkable
class EmbeddingBackend(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def dimensions(self) -> int: ...
    @property
    def provider_id(self) -> str: ...

    def capabilities(self) -> EmbeddingCapabilities: ...
    def embed(self, text: str) -> list[float] | None: ...
    def embed_batch(self, texts: list[str]) -> list[list[float] | None]: ...
    def close(self) -> None: ...
```

### EmbeddingCapabilities dataclass

```python
@dataclass(frozen=True)
class EmbeddingCapabilities:
    max_batch_size: int           # max texts per embed_batch() call
    max_input_tokens: int         # max tokens per text input
    supports_input_type: bool     # see §"supports_input_type flag" below
```

#### supports_input_type flag vs. parameter deferral

`supports_input_type=True` advertises that the backend CAN distinguish
query-embedding from document-embedding mode (e.g., OpenAI's `input_type`
parameter, Cohere's `input_type`).

**PR 2 advertises this flag only.** The actual `input_type` parameter on
`embed()`/`embed_batch()` is deliberately absent in PR 2, pending the
`@runtime_checkable` limitation analysis (the decorator verifies member
presence, not that a backend honors an optional kwarg, so the testable flag
must precede the testable parameter). PR 3 adds the kwarg to the Protocol
surface and the conformance suite when the flag semantics are confirmed.

**PR 2 conformance requirement:** all implementations MUST advertise
`supports_input_type=False`. The OpenAI API does support `input_type` but the
Protocol surface in PR 2 does not expose the parameter — advertising `True`
while the kwarg is absent violates capability honesty (MUST 3).

```
<!-- TODO: PR3 --> Flip OpenAIEmbeddingBackend.capabilities() to
supports_input_type=True and add input_type kwarg to embed()/embed_batch().
```

---

## CONFORMANCE INVARIANTS

### MUST-NOT-RAISE invariant (MUST 4)

`embed()` and `embed_batch()` MUST NOT raise under any circumstances. Network
errors, rate limits, malformed input, token-length exceeded, and provider
unavailability all produce `None` in the output — they never propagate as
exceptions. This is the crash-recovery posture: callers zip results with input
texts safely regardless of partial failure.

### len(out)==len(in) invariant (MUST 9)

`len(embed_batch(texts))` MUST equal `len(texts)` for all inputs, regardless
of per-item success or failure. A failed item MUST produce `None` at its index
position, not a shorter output list. Empty input (`texts=[]`) returns `[]`.
Total failure returns `[None] * len(texts)`.

Implementations that truncate the output list rather than inserting `None`
violate this MUST and will fail the parametrized conformance suite.

---

## embed_batch() reference pattern (no inherited default)

`EmbeddingBackend` is a `Protocol`, so it provides NO method bodies — there is
no inherited `embed_batch()` default. Every implementation MUST define
`embed_batch()` itself. The recommended reference pattern loops `embed()`
item-by-item:

```python
def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
    return [self.embed(t) for t in texts]
```

(`StubEmbeddingBackend` uses exactly this.) `OpenAIEmbeddingBackend` instead
issues a single batched SDK call that degrades to per-item on total batch
failure, while preserving the `len(out)==len(in)` invariant.

---

## Cost gate mandate (DRAFT scope)

**MANDATE (normative):** any code path that invokes `embed()` OR `embed_batch()`
at a production ingestion site OR a query-embedding site MUST reserve worst-case
embedding cost before dispatch and record actual spend via a distinct embedding
audit record. Both methods are separately reachable billable provider calls:
`embed_batch()` is the bulk-ingestion path (e.g. indexing a corpus), and the
standalone `embed()` is the query-embedding path (e.g. `PgvectorMemoryBackend`
recall or `PgvectorCorpusBackend.query()` embedding the search string in PR 3).
Gating only `embed_batch()` would leave every query-time `embed()` call as an
ungated billable LLM code path — the exact Principle #4 escape ("every code
path that calls an LLM has a cost gate"). The single-call `embed()` worst-case
is ONE billed call (no per-item fan-out, unlike `embed_batch()`).

> **`embed_batch()` worst-case includes the per-item degradation fan-out.** A
> backend whose `embed_batch()` falls back to per-item `embed()` on a
> malformed-item batch failure (e.g. `OpenAIEmbeddingBackend`) can issue up to N
> additional billed calls — the failed batch call PLUS one call per text. The
> reservation MUST account for `batch + per-item retry`, not the single batch
> call alone; reserving only the single-batch cost would UNDER-reserve the
> degrade path (Principle #4 — no path under-reserves its guardrail). The
> provider-unavailable short-circuit path (no per-item retry) does not fan out,
> so this applies only to backends that degrade per-item. See the partial-batch
> docstring on `OpenAIEmbeddingBackend.embed_batch()` for the matching code-side
> note.

```
trigger="embed_reservation"          output_tokens=0    cost_source="actor"  # single embed()
trigger="embed_release"              output_tokens=0    cost_source="actor"
trigger="embed_batch_reservation"    output_tokens=0    cost_source="actor"  # embed_batch()
trigger="embed_batch_release"        output_tokens=0    cost_source="actor"
```

**PR 2 ships the pricing helper (`EMBEDDING_PRICING` + `calc_embedding_cost()`)
and this normative mandate only.** The live wiring to ingestion and
query-embedding sites is PR 3. The DRAFT status of spec/46 reflects this gap —
the mandate exists but no production code enforces it yet. The LOCK ceremony on
this spec (PR 3) MUST verify that BOTH wiring sites exist — the `embed_batch()`
ingestion reservation AND the single-call `embed()` query-embedding reservation
— before removing the DRAFT marker, not just the batch one.

### Fail-closed posture (non-normative guidance for PR 3)

Per MEMORY.md lesson "Fail-closed only where there's something to protect":

```
if cost_read_degraded AND cap is not None:
    fail-closed (refuse the embed_batch call)
else:
    proceed (an unconstrained agent is not blocked by a blind read)
```

`calc_embedding_cost()` returns `(cost_usd, cost_estimated: bool)`:
- `cost_estimated=True` means "unpriced model, used max known embedding rate"
  (the fallback is pessimistic but usable)
- A CostReadResult with `degraded=True` (from `sum_cost_for_period`) means
  "I/O failure reading cost history" — a different signal entirely

**Do NOT conflate these.** `cost_estimated=True` is not `degraded=True`.

Fleet deployments SHOULD apply a per-agent cap AND a project-level cap via
MandateBackend; the per-agent reservation alone does not bound fleet-wide spend.

---

## EMBEDDING_PRICING — isolation rationale

`EMBEDDING_PRICING` is a completely separate dict from `PRICING` (chat models).
`calc_embedding_cost()` calls only `_embedding_fallback_rate()`, which scans
`EMBEDDING_PRICING` exclusively.

This isolation is load-bearing: the max chat rate (Opus output, $75/1M) is
~577× the max embedding rate ($0.130/1M for text-embedding-3-large). If an
unknown embedding model accidentally fell back to the Opus rate, the ingestion
gate would be spuriously blocked for operators without a cost cap, or the gate
would be bypassed if clamped to $0.

```python
# USD per 1M input tokens (input-only; no output column).
# Each rate verified 2026-06-17 against its authoritative OpenAI per-model docs
# page on developers.openai.com/api/docs/models/<model> (NOT community-only);
# also consistent with the pages-per-dollar derivation.
EMBEDDING_PRICING = {
    "text-embedding-3-small": 0.020,   # docs page: Cost $0.02; 62,500 pp/$ × 800 tok/page
    "text-embedding-3-large": 0.130,   # docs page: Cost $0.13; 9,615 pp/$ × 800 tok/page
    "text-embedding-ada-002": 0.100,   # docs page: Cost $0.10; 12,500 pp/$ × 800 tok/page
}
```

Note: a $0.065/1M figure for text-embedding-3-large appeared on a historical
(erroneous) pricing-page entry for the synchronous rate, since corrected to
$0.130/1M. It is NOT a Batch-API discount rate — the authoritative per-model
docs page (verified 2026-06-17) shows the standard AND Batch API price are
both $0.130/1M. The $0.130/1M rate is authoritative (per-model docs page,
cross-checked by the pages-per-dollar derivation above). Disregard $0.065/1M.

---

## Exception hierarchy

`EmbeddingError` and `EmbeddingProviderUnavailable` live in
`atomic_agents.exceptions`, following the project convention:

```python
class EmbeddingError(AtomicAgentsError): ...
class EmbeddingProviderUnavailable(EmbeddingError): ...
```

**(NON-NORMATIVE):** These exceptions are for internal logging context only.
`embed()` and `embed_batch()` MUST return `None`, not raise `EmbeddingError`
or any subclass. The typed `EmbeddingProviderUnavailable` branch in `embed()`
emits a branch-distinctive WARNING log line; the broad `Exception` fallback
emits a different WARNING message. Tests assert the specific log line — not
just the `None` return — to prevent false-green tests where both branches
return the same sentinel without the correct log.

---

## NON-NORMATIVE: embedding_provider field on existing dataclasses

The existing `embedding_provider: str | None` field on `CorpusCapabilities`
(spec/34, `atomic_agents/corpus/types.py`) is a **display label** identifying
the provider family — not a typed reference to an `EmbeddingBackend` instance.
Examples: `"openai"`, `"local"`, `"ollama"`. (There is no analogous field on a
`MemoryCapabilities` dataclass today; spec/20 does not define one. If PR 3
needs a memory-side provider label it will add it then, as new scope.)

This field is left UNTOUCHED by PR 2 — no normative amendment to the LOCKED
spec/34. Normative reconciliation (string-stays-as-label + sibling
`EmbeddingBackend` reference field) is deferred to PR 3, per the #201
precedent where `mcp_servers_resolved` was added as a sibling field in PR 2
and the display label remained for backwards compatibility.

---

## Implementer Contract — 9 normative MUSTs

<!-- MUST count: 9. Verify at LOCK ceremony. -->

### MUST 1 — Input validation

Implementations MUST validate that `model_id` is a non-empty string and
`dimensions` is a positive integer. Implementations MUST NOT silently accept
an empty `model_id` or non-positive `dimensions` — raise `EmbeddingError`.
(The reference `OpenAIEmbeddingBackend` validates at construction; the Protocol
permits validation at construction or at first embed.)

### MUST 2 — Side-effect-free construction

Construction imports the SDK (fail-fast on missing dependency) but MUST NOT
call any embedding endpoint. No network I/O, no token spend, no provider
authentication at `__init__` time. This mirrors `OpenAICompatibleLLMBackend`
(spec/31), which imports the openai SDK at construction for fail-fast behavior
but issues no API calls.

### MUST 3 — Capability honesty

`capabilities()` MUST return the same `EmbeddingCapabilities` value across
calls for the lifetime of the backend instance. Backends that advertise
`supports_input_type=True` but whose `embed()`/`embed_batch()` signatures do
not accept an `input_type` parameter produce silent failures in any caller that
relies on the flag. In PR 2, all conforming implementations MUST advertise
`supports_input_type=False` (the Protocol surface does not include the parameter
yet).

**Advertised `dimensions` MUST equal produced length.** The `dimensions`
property MUST equal `len(embed(text))` for any successfully-embedded text. A
backend MUST NOT advertise a dimension the API will not produce — including the
common trap of a single global default dimension used across models of
different native sizes (e.g. advertising 1536 for `text-embedding-3-large`
while the API returns its native 3072). When the caller omits the dimension,
the backend MUST resolve it to the model's native size, not a global constant.
When the caller requests a reduced dimension on a model with no server-side
reduction, the backend MUST refuse at construction rather than advertise a size
it cannot honor. For a model that DOES support server-side reduction, a
requested dimension ABOVE the model's native size MUST also be refused at
construction — reduction can only shrink the vector, never grow it, so a larger
value would advertise a length the API will never produce. As a backstop for
any mismatch that escapes construction (e.g. an unknown model whose true native
size differs from the fallback, or provider/SDK drift), `embed()`/`embed_batch()`
MUST verify the produced vector length against the advertised `dimensions` and
return `None` (never a wrong-length vector) on a mismatch. The PR3 pgvector
wiring sizes its vector column from this property, so a mismatch becomes insert
failures or silent truncation.

**Unknown-model limitation (DRAFT).** A backend cannot resolve the native size
of a model it has never heard of without a network round-trip at construction
(which MUST 1 / side-effect-free construction forbids). For an unknown model
with the dimension omitted, the reference `OpenAIEmbeddingBackend` falls back to
its global default (1536) and DOCUMENTS that the caller MUST pass an explicit
`dimensions` if the true native size differs. This is the one place the
"native, not a global constant" rule is best-effort rather than guaranteed at
CONSTRUCTION; it is bounded to unknown (unlisted) models only. The silent
mis-size it could cause is now caught at the PRODUCED vector: the PR2 reference
impl verifies `len(returned) == dimensions` on every embed and returns `None`
on a mismatch (see the produced-length backstop above), so an unknown model
whose native size differs degrades to None rather than emitting a wrong-length
vector. Full closure at LOCK still tightens construction by either (a) keeping
the produced-length check as the guarantee, or (b) refusing construction of an
unknown model without an explicit `dimensions`. The construction-guard error
message for an unknown model MUST NOT assert a native size the backend does not
actually know.

### MUST 4 — embed 4-case / MUST-NOT-RAISE

`embed()` and `embed_batch()` MUST NOT raise under any circumstances. All four
failure modes produce `None` return:

| Trigger | Required output |
|---------|----------------|
| Network error / timeout | `None` |
| Rate limit exceeded | `None` |
| Token length exceeded (text > max_input_tokens) | `None` |
| Provider authentication failure | `None` |

The implementation MUST log at WARNING level with a branch-distinctive message
that differs between the typed exception branch and the broad fallback. Tests
MUST assert the specific log line — not just the `None` return — to prevent
false-green tests.

### MUST 5 — URL/secret redaction

Any logging the backend emits MUST NOT include raw API keys, bearer tokens, or
full embedding vectors. SDK exceptions MUST be logged by `type(exc).__name__`
only — never by `str(exc)` or `exc.args`. The OpenAI SDK may echo partial
credentials in `AuthenticationError` messages; `str(exc)` would leak them.

### MUST 6 — Storage/key isolation

`provider_id` MUST be stable across calls for the lifetime of the backend
instance. Backends sharing a provider (e.g., two `OpenAIEmbeddingBackend`
instances with different models) MUST return the same `provider_id`. The
provider string is a family identifier, not an instance identifier.

### MUST 7 — Snapshot/vector determinism

For the same input text and the same model, `embed()` MUST return the same
vector on repeated calls (given the same provider state). This is a
best-effort MUST for remote providers (the provider may change model weights
in a deployment) — backends document any known non-determinism in their
docstring.

### MUST 8 — backend_id stability

`model_id` and `provider_id` MUST return the same values across calls for
the lifetime of the backend instance. `close()` MUST be idempotent — calling
it twice MUST NOT raise. Backends that hold connection pools MUST release
resources in `close()`.

### MUST 9 — len(out)==len(in) invariant (similarity-search / query-ranking axis)

`embed_batch(texts)` MUST return a list of exactly `len(texts)` elements. A
failed item MUST produce `None` at its index position, not a truncated list.
This invariant holds for empty input (`[]` → `[]`) and total failure
(`[None] * len(texts)`).

**Negative control requirement (ENGINEERING LESSON #3):** the conformance test
for this MUST must verify the test goes RED when the invariant is violated (e.g.,
when `embed_batch` returns a truncated list). A test that passes regardless of
whether the invariant is enforced is a false-green.

---

## Mocking note

`MagicMock(spec=EmbeddingBackend)` passes `isinstance(m, EmbeddingBackend)`
because `@runtime_checkable` inspects attribute presence. However, MagicMock's
`embed()` returns a MagicMock, not `list[float] | None`. Tests MUST use
concrete fake classes (`StubEmbeddingBackend` in `tests/stub_embedding.py`)
instead of MagicMock for conformance assertions.

---

## Reference implementations

| Implementation | Location | Status |
|---------------|----------|--------|
| `OpenAIEmbeddingBackend` | `atomic_agents/embedding/openai.py` | Shipped (PR 2) |
| Local embedding backend (sentence-transformers/Ollama) | TBD | Tracked in issue [#534](https://github.com/dep0we/atomic-agents-stack/issues/534) |

`OpenAIEmbeddingBackend` defaults:
- Model: `text-embedding-3-small`
- Dimensions: native to the model (3-small → 1536, 3-large → 3072, ada-002 →
  1536); `1536` for the default 3-small. A reduced dimension is forwarded for
  `text-embedding-3-*`; a dimension above native, or any non-native dimension on
  a non-reducible model, is refused at construction (MUST 3).
- Key resolution: delegates to the framework SecretBackend (spec/38) via the
  same `_llm._get_key` resolver as `OpenAICompatibleLLMBackend` (KeySpec
  `ATOMIC_AGENTS_OPENAI_KEY` / `OPENAI_API_KEY`, keychain `atomic-agents-openai`,
  config key `openai`) — whatever backend is registered (Filesystem, GCP Secret
  Manager, …) resolves the key; the local cascade is NOT hardcoded.

**No production registry in PR 2.** Constructor-injected by the consuming
backend (e.g., `PgvectorCorpusBackend` receives an `EmbeddingBackend` at
`__init__`). Registry functions (`register_embedding_backend`,
`get_embedding_backend`, etc.) ship in PR 3.

---

## PR scope boundary

| Scope | PR 2 (this spec) | PR 3 |
|-------|-----------------|------|
| Protocol + dataclasses | ✅ Ships | — |
| `OpenAIEmbeddingBackend` ref impl | ✅ Ships | — |
| `EMBEDDING_PRICING` + `calc_embedding_cost()` | ✅ Ships | — |
| Spec/46 (DRAFT) | ✅ Ships | Locked |
| Registry (`register_/get_/list_embedding_backend`) | — | ✅ Ships |
| pgvector wiring (`PgvectorCorpusBackend`, `PgvectorMemoryBackend`) | — | ✅ Ships |
| Live cost reservation at BOTH ingestion (`embed_batch()`) and query-embedding (`embed()`) sites | — | ✅ Ships |
| Doctor check | — | ✅ Ships (no billable probe) |
| `input_type` kwarg on Protocol surface | — | ✅ Ships |
| Normative reconciliation of `embedding_provider` on spec/20 + spec/34 | — | ✅ Ships |
| Local embedding backend (sentence-transformers/Ollama) | — | Separate arc ([#534](https://github.com/dep0we/atomic-agents-stack/issues/534)) |

---

## Supersession of spec/34's out-of-scope rationale

The LOCKED spec/34 "Out of scope" table records, for `EmbeddingBackend`:
> "No standalone use case identified."

That was accurate at spec/34 lock time. Issue #200 subsequently identified the
standalone use case: constructing `PgvectorCorpusBackend` (and future
`PgvectorMemoryBackend`) requires an injected embedding backend that both
consumers share — a Protocol-typed abstraction, not a capability flag.
`EmbeddingBackend` ships as `atomic_agents/embedding/` (spec/46, DRAFT).

**spec/34 is NOT edited by PR 2.** This supersession is recorded here only;
the LOCKED spec/34 (and spec/20) take their normative addenda in PR 3, when
the pgvector wiring ships and a re-lock ceremony covers them. Until then,
spec/34's table entry stands as historical provenance.

# spec/46 — EmbeddingBackend Protocol

**Status:** LOCKED (#544 PR2)
**Issue:** [#200](https://github.com/dep0we/atomic-agents-stack/issues/200)
**Implementer Contract MUSTs:** 9 <!-- conformance tests: 60 as of #544 PR2 (tests/test_embedding_protocol_conformance.py) -->
**Arc:** PR 2 shipped Protocol + reference impl + spec + cost helpers (DRAFT).
PR 3 shipped the pgvector backends + registry + `input_type` kwarg + opt-in guard.
[#544](https://github.com/dep0we/atomic-agents-stack/issues/544) PR1 shipped the
live batch embed cost-gate at the orchestrator layer, `PRIMITIVE_EMBED`, the doctor
check, and the spec/20 + spec/34 addenda. DRAFT→LOCKED ceremony shipped at
[#544](https://github.com/dep0we/atomic-agents-stack/issues/544) PR2.

> **PR-slicing update (#200 PR3, shipped 2026-06-18).** PR3 shipped:
> `PgvectorMemoryBackend` + `PgvectorCorpusBackend` (the latter subclasses
> `FilesystemCorpusBackend` — FS pages + Postgres vectors; full-Postgres corpus
> tracked in [#540](https://github.com/dep0we/atomic-agents-stack/issues/540)),
> the `EmbeddingBackend` registry, the `input_type` kwarg on the Protocol surface,
> and an **opt-in-by-default cost-safety guard** (semantic search is OFF unless
> the operator explicitly pins `ATOMIC_AGENTS_EMBEDDING_BACKEND` or injects a
> backend — preventing surprise billable spend from a merely-present API key).
>
> **PR-slicing update (#544 PR1, shipped 2026-06-20).** PR1 shipped: the
> batch embed cost gate at the `agent.call()` capture-commit site (post-loop,
> `write_note()` batch reservation + release), `PRIMITIVE_EMBED = 'embed'` with
> 4 trigger mappings (`embed_batch_reservation`, `embed_batch_release`,
> `embed_reservation`, `embed_release`) in `_PRIMITIVE_BY_TRIGGER`,
> `check_embedding_backend()` doctor check (SKIP/PASS/WARN/FAIL), and versioned
> normative addenda to spec/20, spec/22, and spec/34. The query-embed gate and
> the DRAFT→LOCKED ceremony are deferred to PR2.
>
> **PR-slicing update (#544 PR2a, shipped 2026-06-20).** PR2a shipped two
> accounting correctness fixes (spec/46 was still DRAFT at PR2a time; it LOCKED
> at #544 PR2 — see the Status line above): (1) dedicated `embed_cost`
> JSONL record (`trigger='embed_cost'`, `cost_usd=actual_usd`, `cost_source='actor'`,
> `model`, `cost_estimated`, `parent_run_id`, `parent_agent`) emitted after
> `embed_batch_release` in the same `finally` block conditioned on `actual_usd > 0`
> — now `sum_cost_for_period` sees prior embed spend across calls; `'embed_cost':
> PRIMITIVE_EMBED` registered in `_PRIMITIVE_BY_TRIGGER`; only the dedicated
> record carries `cost_usd` (release/reservation remain audit-only). (2)
> merge-write pre-read reservation: a merge PRESERVES the target body verbatim
> (the backend re-embeds the stored target body ALONE; the fragment lands in
> sources metadata, NOT the body — backend.py:316-317, pgvector.py:761-768), so
> the reservation and true-up loops now call `read_note(merge_into)` before the
> write loop and size the estimate from the PRESERVED TARGET body. PR1 sized from
> the small incoming fragment, under-reserving/under-charging a merge whose stored
> target body is large; sizing from the target body refuses an over-cap merge
> before billing (Principle #4). On read failure falls back gracefully to
> fragment-only with a WARNING; `_merge_body_cache` (keyed by the same dedup
> 4-tuple) shared by both loops prevents reserve/actual desync. Write-loop dedup
> key extended to include `merge_into`. The `embed_cost` record shape and the
> merge-write resolution land as a spec/22 versioned normative addendum at the
> #544 PR2 LOCK ceremony (spec/22 is LOCKED; PR2a does not edit it).
>
> **The query-embed billable path is NOT inside `agent.call()`.** There is no
> `memory.search()` or `corpus.query()` call site in the orchestrator (verified
> by grep over `agent.py`), so there is no orchestrator query-embed path to gate
> and PR2 MUST NOT add a speculative pre-loop search call or tool interceptor.
> The real ungated query-embed path is the CLI corpus-query command, tracked in
> [#564](https://github.com/dep0we/atomic-agents-stack/issues/564); that is the
> PR2 gate site.
>
> **PR-slicing update (#544 PR2, shipped 2026-06-22).** PR2 shipped: the CLI
> corpus-query embed gate (`_corpus_query` in `cli.py` — `embed_reservation` +
> `embed_release` + `embed_cost` in try/finally; `--critical` bypass flag; cost
> headroom check mirroring `dream._check_cap`; the records carry the existing
> `embed` primitive — `cli_corpus_query` is the informal name of the gate SITE,
> not a new spec/22 taxonomy entry); the gate-site normative MUSTs section
> (caller-side, separate from the 9-MUST backend contract); the fail-closed
> posture promoted from non-normative guidance to a normative gate-site MUST;
> `PgvectorMemoryBackend` + `PgvectorCorpusBackend` folded into the shared
> `@_parametrize_backends` conformance suites (gated `@requires_postgres`,
> `StubEmbeddingBackend`, no live OpenAI); spec/34 normative addendum for the
> corpus embedding model-swap-requires-DROP limitation; spec/46 DRAFT→LOCKED
> ceremony complete. MUST 3 updated to reflect the shipped `input_type` kwarg
> state (PR3 shipped; OpenAI stays `supports_input_type=False` because the SDK
> does not expose the parameter). Unknown-model limitation resolved at path (a).
> Direct-caller gate boundary documented. DRAFT→LOCKED.
>
> spec/46 is now LOCKED. The opt-in default (`ATOMIC_AGENTS_EMBEDDING_BACKEND`
> unset → FTS only) ensures no ungated billable path runs without explicit
> operator opt-in (Principle #4).

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
here rather than amended into the LOCKED spec/34 — the normative reconciliation
of spec/34 + spec/20 landed at #544 PR1/PR2 via versioned normative addenda to
both specs.

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

#### supports_input_type flag

`supports_input_type=True` advertises that the backend CAN distinguish
query-embedding from document-embedding mode (e.g., Cohere's `input_type`).

**The `input_type` kwarg shipped in PR 3** on both `embed()` and
`embed_batch()`. Backends whose provider supports `input_type` SHOULD forward
it and SHOULD advertise `supports_input_type=True`. Backends whose provider
does NOT support `input_type` MUST accept the kwarg without raising (for
interface compatibility with query-aware callers such as `PgvectorCorpusBackend`
which passes `input_type="search_query"`) and MUST advertise
`supports_input_type=False`.

**`OpenAIEmbeddingBackend` advertises `supports_input_type=False`** because the
installed OpenAI SDK (`openai 2.35.1`) does not expose `input_type` on
`embeddings.create()` — the kwarg is accepted and silently ignored for interface
compatibility, but advertising `True` would violate capability honesty (MUST 3)
by implying the backend honors a parameter it cannot forward. Future SDK
versions that expose `input_type` should flip this flag to `True`.

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

## Cost gate mandate (normative)

**MANDATE (normative):** any code path that invokes `embed()` OR `embed_batch()`
at a production ingestion site OR a query-embedding site MUST reserve worst-case
embedding cost before dispatch and record actual spend via a distinct embedding
audit record. Both methods are separately reachable billable provider calls:
`embed_batch()` is the bulk-ingestion path (e.g. indexing a corpus), and the
standalone `embed()` is the query-embedding path (e.g.
`PgvectorCorpusBackend.query()` embedding the search string). Gating only
`embed_batch()` would leave every query-time `embed()` call as an ungated
billable LLM code path — the exact Principle #4 escape ("every code path that
calls an LLM has a cost gate"). The single-call `embed()` worst-case is ONE
billed call (no per-item fan-out, unlike `embed_batch()`).

**Both framework-controlled gate sites are now wired (#544 PR1 + PR2):**

1. **Batch-ingestion gate** — post-loop capture-commit site in `agent.call()`,
   emitting `embed_batch_reservation` + `embed_batch_release` + `embed_cost`
   (shipped in #544 PR1 + PR2a).

2. **CLI corpus-query gate** — `_corpus_query` in `cli.py`, emitting
   `embed_reservation` + `embed_release` + `embed_cost` in a try/finally block
   (shipped in #544 PR2). `primitive="embed"` in spec/22 taxonomy.

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

> **Merge-write reservations are target-body-sized (RESOLVED in #544 PR2a).**
> A merge write PRESERVES the target body verbatim — the backend re-embeds the
> stored TARGET body alone; the incoming fragment updates sources/last_seen
> metadata, NOT the body (backend.py:316-317; filesystem `_merge_into_existing`
> leaves the content untouched; pgvector.py:761-768 embeds `stored.body`). The
> reservation loop calls `self.memory.read_note(merge_into)` BEFORE the write
> loop and sizes the estimate from the PRESERVED TARGET body. An over-cap merge
> is refused before billing (Principle #4 — enforce-before-pay). On read failure
> the gate falls back to fragment-only with a WARNING. The pre-read result is
> cached in `_merge_body_cache` so reserve and actual_usd share the same estimate.

> **Token estimate basis: `ceil(utf8_bytes / 3)` is conservative, not a strict
> upper bound.** Both gates estimate tokens from the UTF-8 byte length divided by
> 3. This is conservative for natural-language text (a Unicode code-point count
> under-counts ~3x for CJK/emoji, where each multibyte char is ≥1 token; the
> byte basis covers that). It is NOT a strict upper bound for incompressible or
> adversarial byte sequences, which can tokenize closer to ~1 token/byte and so
> under-reserve by up to ~3x before the 2x fan-out buffer. The residual is
> bounded and small in practice: the provider rejects any single text exceeding
> the model's per-text token cap, and embedding pricing is sub-cent per token. A
> tokenizer-exact estimate is deferred (it would add a tokenizer dependency); the
> documented basis matches the shipped code.

```
trigger="embed_reservation"          output_tokens=0    cost_source="actor"  # single embed() at CLI site
trigger="embed_release"              output_tokens=0    cost_source="actor"
trigger="embed_batch_reservation"    output_tokens=0    cost_source="actor"  # embed_batch() at agent.call() site
trigger="embed_batch_release"        output_tokens=0    cost_source="actor"
trigger="embed_cost"                 output_tokens=0    cost_source="actor"  # cross-call accounting (both gates)
                                     cost_usd=actual_usd  parent_agent=<name>  # ONLY embed trigger with cost_usd
```

### Fail-closed posture (normative)

The fail-closed predicate applies identically at both gate sites:

```
if cost_read_degraded AND effective_cap is not None:
    fail-closed (refuse the embed call)
else:
    proceed (an unconstrained agent is NOT blocked by a blind read)
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

## Gate-site normative MUSTs

These MUSTs govern **callers** of `EmbeddingBackend` that invoke `embed()` or
`embed_batch()` at a production call site. They are categorically distinct from
the Implementer Contract (which governs backends). The backend MUST count stays
at 9.

**GATE-MUST 1 — Pre-call reservation.** Any **framework-controlled** production
call site that invokes `embed()` or `embed_batch()` — namely the `agent.call()`
capture-commit ingestion gate and the CLI corpus-query gate (`_corpus_query`) —
MUST emit an `embed_reservation` (single-call) or `embed_batch_reservation`
(batch) JSONL record BEFORE the call. The reservation MUST carry the worst-case
cost estimate (including the per-item degradation fan-out for `embed_batch()`).
Direct backend methods that invoke `embed()` (e.g. `memory.search()`,
`corpus.query()`, `corpus.write_page()`/`index_page()`) are NOT
framework-controlled gate sites: a direct caller owns its own gate, per the
Direct-caller gate boundary section below (the same boundary that governs a raw
`llm_backend.chat()` caller).

**GATE-MUST 2 — Release in finally.** The matching `embed_release` or
`embed_batch_release` record MUST be emitted in a `finally` block so it fires
even when `embed()` or `embed_batch()` raises or when a downstream processing
step raises. An orphaned reservation record (release never emitted) inflates
`sum_cost_for_period` and can produce spurious over-budget blocks on subsequent
calls.

**GATE-MUST 3 — embed_cost for cross-call accounting.** A dedicated `embed_cost`
record (the ONLY embed trigger carrying `cost_usd`) MUST be emitted in the same
`finally` block IMMEDIATELY AFTER the release record, conditioned on
`actual_usd > 0`. This is what makes prior embed spend visible to the cap
baseline on subsequent calls (Principle #4). Only the `embed_cost` record
carries `cost_usd`; the release record intentionally omits it to prevent
double-count.

**GATE-MUST 4 — Fail-closed predicate.** The gate MUST apply the fail-closed
predicate `if cost_read_degraded AND effective_cap is not None: fail-closed`
ONLY when a cap exists. An unconstrained call site (no cap resolvable AT THAT
SITE) MUST NOT be blocked by a degraded cost read — a blind read changes
nothing when there is no budget to protect.

The set of cap sources a site considers is scoped to what the site can resolve.
The `agent.call()` capture-commit site holds an active session, so it resolves
the EFFECTIVE cap (`daily_cap_usd` / `monthly_cap_usd` from `model.md` composed
with any Policy- or Mandate-derived cap via `_has_effective_embed_cap()`). The
`atomic-agents corpus query` CLI site is a one-shot command with no active
mandate session, so it resolves the per-agent `model.md` caps only
(`cost_guardrails_enabled` + `daily_cap_usd` + `monthly_cap_usd`); session-scoped
MandateBackend/Policy caps are not in scope at the CLI site. Extending the CLI
site to consult a fleet-wide MandateBackend cap is a possible future refinement,
not a LOCK requirement.

**Shipped gate sites (normative examples):**

| Site | Trigger group | Module |
|------|--------------|--------|
| `agent.call()` capture-commit | `embed_batch_reservation` + `embed_batch_release` + `embed_cost` | `atomic_agents/agent.py` |
| `atomic-agents corpus query` | `embed_reservation` + `embed_release` + `embed_cost` | `atomic_agents/cli.py:_corpus_query` |

### Direct-caller gate boundary

`memory.search()` and `corpus.query()` on pgvector backends are direct callers
of `embed()` that do NOT pass through either gate site above. The analogy is
`llm_backend.chat()`: the LLMBackend Protocol does not gate cost inside
`chat()` itself; the gate is at the `agent.call()` orchestrator layer. Direct
callers bear responsibility for their own cost reservation.

This is intentional for the current framework: there is no `memory.search()` or
`corpus.query()` call site inside `agent.call()` (confirmed by grep over
`agent.py`), so there is no orchestrator-controlled query-embed path beyond the
CLI site. Operators who call `memory.search()` or `corpus.query()` directly
(outside of `_corpus_query`) are ungated by design. A future gated helper
(analogous to how `agent.call()` gates LLM cost for every LLM call) is tracked
as follow-up issue [#586](https://github.com/dep0we/atomic-agents-stack/issues/586).

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
Examples: `"openai"`, `"local"`, `"ollama"`. (`MemoryCapabilities` gained the analogous `embedding_provider` field as part of
#200 PR3 / #544 PR1 — see spec/20 PR-3 addendum.)

This field was left UNTOUCHED by PR 2. Normative reconciliation (string-stays-as-label
+ sibling `EmbeddingBackend` reference field) shipped at #544 PR1 — see the versioned
normative addenda in spec/34 and spec/20, following the #201 precedent where
`mcp_servers_resolved` was added as a sibling field and the display label remained
for backwards compatibility.

---

## Implementer Contract — 9 normative MUSTs

<!-- MUST count: 9 — verified at the #544 PR2 LOCK ceremony (backend Implementer
Contract only; the 4 GATE-MUSTs above govern callers and are counted separately). -->

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
calls for the lifetime of the backend instance.

**`supports_input_type` — shipped state (as of PR 3).** The `input_type` kwarg
IS on the Protocol surface (PR 3 shipped it on both `embed()` and
`embed_batch()`). Backends whose provider supports `input_type` MUST accept and
forward it and MUST advertise `supports_input_type=True`. Backends whose provider
does NOT support `input_type` MUST accept the kwarg without raising (for
interface compatibility — `PgvectorCorpusBackend` passes `input_type=
"search_query"` to whichever backend is injected) and MUST advertise
`supports_input_type=False`. Advertising `True` for a parameter the provider
ignores violates capability honesty. `OpenAIEmbeddingBackend` correctly stays at
`supports_input_type=False` because the OpenAI embeddings API does not expose
`input_type` on `embeddings.create()`.

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

**Unknown-model limitation — resolved at path (a).** A backend cannot resolve
the native size of a model it has never heard of without a network round-trip at
construction (which MUST 2 / side-effect-free construction forbids). For an
unknown model with the dimension omitted, the reference `OpenAIEmbeddingBackend`
falls back to its global default (1536) and DOCUMENTS that the caller MUST pass
an explicit `dimensions` if the true native size differs. This is the one place
the "native, not a global constant" rule is best-effort rather than guaranteed at
construction; it is bounded to unknown (unlisted) models only. The LOCK ceremony
resolved this at path (a): the produced-length backstop (`embed()`/`embed_batch()`
verifying `len(returned) == dimensions` and returning `None` on mismatch) is the
normative guarantee for unknown models. Construction-time refusal of unknown
models without explicit `dimensions` is NOT required by conformance. The
construction-guard error message for an unknown model MUST NOT assert a native
size the backend does not actually know.

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
`get_embedding_backend`, etc.) shipped in PR 3 (#200).

---

## PR scope boundary

| Scope | PR 2 (shipped) | PR 3 / #200 (shipped) | #544 PR1 (shipped) | #544 PR2a (shipped) | #544 PR2 (shipped) |
|-------|------|------------|------|------|------|
| Protocol + dataclasses | ✅ Shipped | — | — | — | — |
| `OpenAIEmbeddingBackend` ref impl | ✅ Shipped | — | — | — | — |
| `EMBEDDING_PRICING` + `calc_embedding_cost()` | ✅ Shipped | — | — | — | — |
| Registry (`register_/get_/list_embedding_backend`) | — | ✅ Shipped | — | — | — |
| pgvector wiring (`PgvectorCorpusBackend`, `PgvectorMemoryBackend`) | — | ✅ Shipped | — | — | — |
| `input_type` kwarg on Protocol surface | — | ✅ Shipped | — | — | — |
| Opt-in-by-default cost-safety guard (semantic OFF unless pinned) | — | ✅ Shipped | — | — | — |
| `PRIMITIVE_EMBED` + 4 trigger mappings in `_PRIMITIVE_BY_TRIGGER` | — | — | ✅ Shipped | — | — |
| Batch embed cost gate (post-loop capture-commit, `embed_batch_reservation` + `embed_batch_release`) | — | — | ✅ Shipped | — | — |
| Doctor check (`check_embedding_backend()`, no billable probe) | — | — | ✅ Shipped | — | — |
| Normative addenda: spec/20 MemoryCapabilities, spec/22 primitive taxonomy, spec/34 CorpusCapabilities | — | — | ✅ Shipped | — | — |
| Cross-call embed accounting: dedicated `embed_cost` record with `cost_usd=actual_usd` — now visible to `sum_cost_for_period` across calls; `'embed_cost': PRIMITIVE_EMBED` in `_PRIMITIVE_BY_TRIGGER` | — | — | — | ✅ Shipped | — |
| Merge-write pre-read reservation: `read_note(merge_into)` before gate; reservation and true-up sized from the PRESERVED TARGET body (merge preserves body verbatim — fragment lands in sources, not the body); Principle #4 enforce-before-pay on the target size | — | — | — | ✅ Shipped | — |
| Query-embed gate (per-call `embed_reservation` + `embed_release` at the CLI corpus-query site — NOT inside `agent.call()`, which has no orchestrator query-embed path) | — | — | — | — | ✅ Shipped |
| `PgvectorMemoryBackend` + `PgvectorCorpusBackend` folded into shared conformance suites (`@_parametrize_backends`, `@requires_postgres`) | — | — | — | — | ✅ Shipped |
| Gate-site normative MUSTs section + Direct-caller gate boundary | — | — | — | — | ✅ Shipped |
| Spec/46 DRAFT→LOCKED | — | — | — | — | ✅ Shipped |
| Local embedding backend (sentence-transformers/Ollama) | — | — | — | — | Separate arc ([#534](https://github.com/dep0we/atomic-agents-stack/issues/534)) |

---

## Supersession of spec/34's out-of-scope rationale

The LOCKED spec/34 "Out of scope" table records, for `EmbeddingBackend`:
> "No standalone use case identified."

That was accurate at spec/34 lock time. Issue #200 subsequently identified the
standalone use case: constructing `PgvectorCorpusBackend` (and
`PgvectorMemoryBackend`) requires an injected embedding backend that both
consumers share — a Protocol-typed abstraction, not a capability flag.
`EmbeddingBackend` ships as `atomic_agents/embedding/` (spec/46, LOCKED).

**spec/34 was NOT edited by PR 2.** This supersession was recorded here only.
The LOCKED spec/34 and spec/20 took their normative addenda at #544 PR1 (shipped
2026-06-20) — see the versioned addenda at the end of each doc. spec/34's table
entry stands as historical provenance of the original rationale.

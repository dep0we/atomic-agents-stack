# spec/34 — CorpusBackend Protocol

> **Status:** LOCKED. CorpusBackend is the eleventh backend protocol locked in the atomic-agents-stack series.

---

## Overview

`CorpusBackend` is the **eleventh** open Protocol in the protocol-pattern series (Memory, LLM, Judge, Lock, Log, AgentProfile, ToolRegistry, Mandate, Policy, Persona, **Corpus**). It abstracts `<agent>/wiki/` (Atomic Wiki — distilled knowledge in the Karpathy style) and `<agent>/raw/` (source documents — PDFs, transcripts, operator-ingested content) behind a Protocol so the framework's core stays small and alternate storage substrates (SQLite-FTS5, Postgres, pgvector) drop in without forking.

`AtomicAgent` exposes `agent.corpus: CorpusBackend`. Call-site code stops touching wiki or raw paths directly.

**The problem this closes.** Today, `agent.py:2937-2939` reads `wiki/INDEX.md` with a bare `Path.read_text()`. `bundle.py:295-303 + 497-510` does the same for rendering. There is no Protocol between the agent and the corpus. For the home user with one agent and a handful of wiki pages, the direct walk is fine. For the operator with a 10K-page wiki or hundreds of MB of raw documents, keyword grep over an unindexed filesystem takes seconds per query. The GB-scale unlock is `SQLiteCorpusBackend` with FTS5: `O(log N)` indexed full-text query, stdlib dependency, no Postgres operator burden.

**The architecture stays layered.** CorpusBackend is separate from MemoryBackend (Rule 3: layers compose, they don't merge). MemoryBackend owns `<agent>/memory/` (atomic notes, INDEX.md routing, dream staging). CorpusBackend owns `<agent>/wiki/` and `<agent>/raw/`. They compose at agent prompt assembly — `agent.py:_load_indexes()` reads from both — and at the future dream-distillation write path when that pipeline lands. Today there are no framework-side writes to wiki or raw; operator additions are manual file edits. The Protocol's `write_page()` ships in PR 1 for future-state use.

**Backwards-compatibility promise.** Empty or missing `<agent>/wiki/` and `<agent>/raw/` yield zero registrations. The 166 existing `AtomicAgent(...)` construction sites are byte-identical to pre-#65 behavior. CorpusBackend is not configured by default; it becomes active only when an operator explicitly configures it via the `ATOMIC_AGENTS_CORPUS_BACKEND` env var or the `corpus_backend=` constructor kwarg.

**The semantic-search seam.** `PgvectorCorpusBackend` (semantic-search-via-embeddings) is deferred to the coordinated `#258` Postgres-adapter family release alongside `PgvectorMemoryBackend`, so the framework's semantic-search coverage stays symmetric across both substrates. v1.0 ships FTS5 for indexed full-text at GB scale; semantic retrieval lands in the `#258` family when the Letta-gap closer is coordinated.

---

## Locked decisions

| # | Topic | Lock |
|---|-------|------|
| D1 | Separation from MemoryBackend | CorpusBackend is its own Protocol. MemoryBackend owns `<agent>/memory/`; CorpusBackend owns `<agent>/wiki/` and `<agent>/raw/`. They compose at prompt assembly; they do not merge. |
| D2 | Semantic search as capability, not Protocol | `CorpusCapabilities.supports_semantic_search: bool` + `query(text, corpus, top_k)` method on CorpusBackend. Embedding model is configured at backend construction via factory URL. Mirrors MemoryBackend's `supports_semantic_search` property (spec/20:192-195). |
| D3 | Filesystem default | `FilesystemCorpusBackend(agent_root)` walks `<agent>/wiki/*.md` and `<agent>/raw/**` recursively. Zero-config for home users. |
| D4 | Backwards-compatible construction | Side-effect-free construction; lazy walk on first method call. Mirrors PersonaBackend's lazy-init pattern and PolicyBackend's lazy-parse-on-first-method-call. |
| D5 | Cost surface is implementer-responsibility | Cost emission for `query()` is NOT framework-automatic. LLMBackend-routed embedding calls get the existing `_check_cost_guardrails` path for free. Non-LLMBackend embedding (local sentence-transformers, managed Pinecone) is implementer-emitted via `CostEstimatorRegistry` at `atomic_agents/judge/cost_estimator_registry.py:80`. No spec/32 amendment required; `policy_decision(axis="cost_cap")` shape is unchanged. |
| D6 | One Protocol, two corpora | Every method takes `corpus: Literal["wiki", "raw"]`. Type-safe via `Literal`; a typo in `"wikki"` fails at construction. `FilesystemCorpusBackend` uses the existing sibling-directory layout; SQLite backends use a discriminator column with cross-corpus isolation at the SQL layer. |
| D7 | Versioning mirrors MemoryBackend | `CorpusCapabilities.supports_versioning: bool` + `snapshot / restore_version / list_versions` trio. `FilesystemCorpusBackend` uses `<agent>/{wiki,raw}/.versions/<page-stem>/<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md`. `SQLiteCorpusBackend` stores version bodies on disk under the same hybrid pattern (keeps the SQL store small; `list_versions` uses a `glob` instead of a JOIN). |
| D8 | A persona does NOT have its own corpus | Persona is identity (IDENTITY.md, SOUL.md, USER.md). Corpus is knowledge content. Orthogonal layers. |
| D9 | `register_corpus_backend` in `__init__.py` | Dominant 6-of-10 placement precedent (Lock / Log / Profile / ToolRegistry / Judge / LLM — not the 2-of-10 backend.py placement used by Mandate + Persona). Finding A4 from /plan-eng-review 2026-05-29. |
| D10 | `query()` precedence rule | Semantic MUST win over FTS when both flags are True. Explicitly documented (finding A2). Prevents ambiguous behavior on Postgres backends that have both pgvector AND tsquery. |
| D11 | `write_page()` 4-case behavior table | Fresh write / content-identical idempotent no-op / explicit overwrite via CAS / collision raises. Mirrors MemoryBackend `write_note` idempotency + CAS discipline. Finding CQ1 from /plan-eng-review 2026-05-29. |
| D12 | `read_page` returns `None`, `read_version` raises | `read_page(name, corpus) -> CorpusPage | None` is the common-path "does this page exist?" query. `read_version(version_ref) -> CorpusPage` raises `CorpusVersionNotFound` because a missing version body indicates an unexpected infrastructure failure (SQL row exists but on-disk body file is gone under the hybrid storage shape), not a routine presence check. Matches MemoryBackend precedent: `read_note` returns None; `read_version` raises. |
| D13 | `bundle.py:_source_paths` migration deferred to v1.1 | The function returns filesystem paths for staleness tracking. SQLite backends synthesize the INDEX from page metadata and have no equivalent path to return. v1.0 keeps the direct path check. Follow-up issue filed at #314. |

---

## Module layout

```
atomic_agents/corpus/
├── __init__.py        # registry: register_corpus_backend / unregister_corpus_backend
│                      # "filesystem" auto-registered on import
├── backend.py         # CorpusBackend Protocol (runtime_checkable)
├── types.py           # CorpusCapabilities, CorpusRef, CorpusPage, CorpusStats
│                      # VersionRef + WritePolicy re-used from MemoryBackend
└── filesystem.py      # FilesystemCorpusBackend (default reference impl)
└── sqlite.py          # SQLiteCorpusBackend (FTS5 reference impl)
```

This 4-file shape matches the dominant 6-of-10 backend module layout precedent (Lock, Log, AgentProfile, ToolRegistry, LLM, Judge). MemoryBackend's 2-file shape (`backend.py` only) is the older outlier pre-dating the 4-file pattern.

---

## Core data types

All types are defined in `atomic_agents/corpus/types.py`. `VersionRef` and `WritePolicy` are re-used verbatim from `atomic_agents/memory/backend.py` for cross-Protocol uniformity (Premise P7).

### `CorpusCapabilities` — capability advertisement

```python
@dataclass(frozen=True)
class CorpusCapabilities:
    """Capability advertisement for a CorpusBackend."""
    supports_semantic_search: bool       # embeddings; query() MUST use vector cosine when True
    supports_full_text_search: bool      # FTS5/tsquery; query() MUST use indexed FTS when True
                                         # and supports_semantic_search is False
    supports_versioning: bool            # snapshot/restore_version/list_versions trio
    supports_streaming_iteration: bool   # list_pages chunked vs in-memory
    embedding_provider: str | None       # "anthropic" | "openai" | "ollama" |
                                         # "local-sentence-transformers"
                                         # None when supports_semantic_search=False
```

`supports_full_text_search=True` on `SQLiteCorpusBackend`; `False` on `FilesystemCorpusBackend`. `supports_semantic_search=True` is reserved for the `PgvectorCorpusBackend` family in the coordinated #258 release. The capability flags are a contract, not a hint — conformance tests assert claim-vs-behavior parity.

### `CorpusRef` — lightweight listing token

```python
@dataclass
class CorpusRef:
    """Lightweight listing token returned by list_pages() and query()."""
    name: str                             # bare filename stem, e.g. "avalanche-vs-snowball"
    corpus: Literal["wiki", "raw"]
    title: str                            # frontmatter title or first H1 in body
    last_modified: datetime
    byte_size: int                        # body size on disk / in SQL
```

### `CorpusPage` — full read model

```python
@dataclass
class CorpusPage:
    """Full read model returned by read_page() and read_version().

    Named fields mirror the actual frontmatter shape in existing wiki pages
    (verified against docs/samples/caldwell/wiki/ at /plan-eng-review and
    /plan-subagent prep pass 2026-05-29). CorpusPage is its OWN dataclass
    (not literally = Note from MemoryBackend) and can grow wiki-specific
    fields later without touching Note. The Protocols stay separate per D1;
    only the frontmatter field NAMES are shared so operators learn the shape
    once.

    The prep-pass Subagent 2 caught two SEVERE refinements folded here:
    - expires_at / supersedes / superseded_by are structural lifecycle fields
      in real wiki sample data; landing them in extra_frontmatter would force
      callers querying "is this page still valid?" to parse a dict.
    - captured / last_seen retyped from datetime | None to date | None because
      PyYAML loads bare ISO dates ("2026-04-22") as datetime.date, not
      datetime. MemoryBackend Note precedent (spec/20:53-58) confirmed.
      The ingested_at raw-side field stays datetime | None (automated ingest
      typically includes time).
    """
    ref: CorpusRef
    body: str                             # full markdown content

    # Frontmatter fields verified against docs/samples/caldwell/wiki/
    # All None-defaulted because frontmatter is partial in practice.
    name: str | None = None              # human-readable page name
    description: str | None = None       # one-line summary
    type: str | None = None              # "wiki_page" | "raw_doc" | operator-defined
    captured: date | None = None         # original capture / authoring time
                                         # TYPE = date (not datetime); PyYAML loads bare
                                         # "YYYY-MM-DD" as datetime.date, not datetime.
                                         # Mistyping as datetime would AttributeError on
                                         # any .strftime() call on a date object.
    last_seen: date | None = None        # last operator touch / re-distillation
                                         # Same date typing as `captured`.
    sources: list[str] | None = None     # for wiki pages: raw page names this distills from
    provenance: str | None = None        # "distilled" | "ingested" | "operator_authored"
    confidence: str | None = None        # "high" | "medium" | "low"
    pinned: bool = False                 # always-include in agent context (matches Note semantics)
    related: list[str] | None = None     # cross-references (wiki [[link]] targets)
    tags: list[str] | None = None        # operator-authored free-form tags
    schema_version: int | None = None    # frontmatter schema version for future migrations

    # Lifecycle / provenance fields present in real wiki frontmatter
    # (added per /plan-subagent prep pass 2026-05-29, Subagent 2 HIGH
    # finding — structural lifecycle semantics belong in named fields,
    # not extra_frontmatter). All mirror equivalent MemoryBackend Note fields.
    expires_at: date | None = None       # page expiry (None = never expires)
    supersedes: list[str] | None = None  # page names this page replaces (forward provenance)
    superseded_by: str | None = None     # page name that replaces this one (backward provenance)

    # Raw-side fields per issue #65's stated schema.
    # Wiki pages typically don't carry these; raw docs typically do.
    # Note: no sample raw-doc frontmatter exists in docs/samples/caldwell/raw/
    # (Subagent 2 HIGH — raw-side field shape is a design assumption until
    # real raw data appears or is added at PR 1 prep).
    source_url: str | None = None        # provenance URL for ingested content
    mime_type: str | None = None         # original document MIME type
    ingested_at: datetime | None = None  # ingest time (datetime, not date; automated
                                         # ingest typically includes time-of-day)

    # Catch-all for operator-authored frontmatter keys not in named fields.
    # Round-trip preserving for any unknown key.
    extra_frontmatter: dict = field(default_factory=dict)
```

### `CorpusStats` — per-corpus health data

```python
@dataclass
class CorpusStats:
    """Per-corpus health and stats data."""
    page_count: int
    total_bytes: int
    last_update: datetime | None
    most_recent: list[CorpusRef]          # top-N by last_modified
```

`VersionRef` and `WritePolicy` are re-used verbatim from `atomic_agents/memory/backend.py`. `VersionRef` is an opaque backend-specific token (see spec/20:60-68); callers never construct them manually. They are returned by `list_versions()` and accepted by `read_version()` and `restore_version()`.

---

## Protocol surface

```python
@runtime_checkable
class CorpusBackend(Protocol):
    """Protocol for corpus (wiki + raw) storage backends.

    Every method that takes `name` validates it against the charset rule
    at the API boundary BEFORE any storage access. Every method that takes
    `corpus` validates it is one of {"wiki", "raw"}.
    """

    # ─── Capability advertisement ─────────────────────────────────────

    @property
    def capabilities(self) -> CorpusCapabilities: ...

    # ─── Read operations ──────────────────────────────────────────────

    def list_pages(
        self,
        corpus: Literal["wiki", "raw"],
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CorpusRef]: ...
    # Returns up to `limit` page references sorted by last_modified descending.
    # `offset` supports paging. FilesystemCorpusBackend MUST skip INDEX.md
    # and any dot-prefixed entries (.versions/, etc.) when walking the dir.

    def read_page(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> CorpusPage | None: ...
    # Returns None when the page does not exist (routine presence check).
    # Distinct from read_version which raises CorpusVersionNotFound on failure
    # (infrastructure failure, not a presence check). See D12.

    def render_index_summary(
        self,
        corpus: Literal["wiki", "raw"],
    ) -> str: ...
    # Returns the corpus's INDEX-equivalent content (wiki/INDEX.md today;
    # SQLite backends synthesize from page metadata).
    # Returns empty string when the corpus directory does not exist OR
    # exists but has no INDEX.md analog (raw corpora typically have no
    # INDEX; the empty-string contract lets callers branch on truthiness
    # the same way they branch on the legacy direct-read pattern).
    # This is the primary migration target for agent.py:2937-2939.

    # ─── Write operations ─────────────────────────────────────────────

    def write_page(
        self,
        name: str,
        content: str,
        corpus: Literal["wiki", "raw"],
        policy: WritePolicy,
        *,
        frontmatter: dict | None = None,
        expected_content_sha256: str | None = None,
    ) -> CorpusRef: ...
    # MUST follow the 4-case behavior table (finding CQ1 — see §"Behavior
    # contracts: write_page() 4-case behavior table").
    # All writes MUST go through _io.atomic_write (prep-pass SEVERE S5).
    # Never target.write_text() directly; partial writes are visible to
    # concurrent readers on POSIX between open() and flush().

    # ─── Versioning (capability-gated) ───────────────────────────────

    def list_versions(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> list[VersionRef]: ...
    # Backends with supports_versioning=False MUST raise NotImplementedError.

    def read_version(
        self,
        version_ref: VersionRef,
    ) -> CorpusPage: ...
    # Raises CorpusVersionNotFound when:
    # (a) the version reference does not exist, OR
    # (b) the version body file is missing or unreadable under the hybrid
    #     storage shape (SQL row exists but on-disk body file was deleted).
    # This independent failure mode is named explicitly because it is a
    # real production scenario distinct from "version does not exist."
    # See D12 for the read_page None vs read_version raise convention.

    def restore_version(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> CorpusRef: ...
    # Restores the page content at version_ref as the live version.
    # Internally calls write_page via the CAS (Case 3) path so the
    # existing version is snapshotted before the restore lands.

    def snapshot(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        *,
        label: str | None = None,
    ) -> VersionRef: ...
    # Creates an explicit version snapshot of the current page content.
    # Raises CorpusPageNotFound when the page does not exist.

    # ─── Search ───────────────────────────────────────────────────────

    def query(
        self,
        text: str,
        corpus: Literal["wiki", "raw"],
        *,
        top_k: int = 10,
    ) -> list[CorpusRef]: ...
    # MUST follow the fixed capability-precedence rule (finding A2 — see
    # §"Behavior contracts: query() capability precedence").

    # ─── Stats ────────────────────────────────────────────────────────

    def stats(self, corpus: Literal["wiki", "raw"]) -> CorpusStats: ...

    # ─── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None: ...
```

---

## Behavior contracts

### `write_page()` 4-case behavior table

Resolved at /plan-eng-review 2026-05-29 (finding CQ1). Mirrors `MemoryBackend.write_note`'s idempotency and CAS discipline so operators reason about writes the same way across both Protocols.

| Case | Trigger | Action |
|------|---------|--------|
| 1 — fresh write | Page does not exist at `(name, corpus)` | Write page via `_io.atomic_write`; update INDEX-equivalent |
| 2 — content-identical idempotent no-op | Page exists; body + frontmatter SHA-256 unchanged | No-op. Safe under crash recovery and re-delivery. |
| 3 — explicit overwrite via CAS | Page exists; content differs; `expected_content_sha256` matches current on-disk hash | Snapshot existing version if `supports_versioning=True`; write new content via `_io.atomic_write`; update INDEX-equivalent |
| 4 — collision (safe default refusal) | Page exists; content differs; `expected_content_sha256` is None OR hash does not match | Raise `CorpusPageExists` (no expected hash supplied) OR `CorpusPreconditionFailed` (hash mismatch) |

CAS via `expected_content_sha256` is the **only** safe overwrite path. Silent overwrite without operator intent is refused by default. This mirrors `MemoryPreconditionFailed` (spec/20:318) in naming and semantics.

### `query()` capability precedence

Resolved at /plan-eng-review 2026-05-29 (finding A2). Postgres backends can have both pgvector AND tsquery; the precedence rule is mandatory so the Protocol contract is unambiguous.

```
       agent.corpus.query(text, corpus="wiki", top_k=10)
                            │
                            ▼
                  CorpusCapabilities check
                            │
            ┌───────────────┼─────────────────┐
            │               │                 │
   supports_semantic   supports_full     both False
   _search == True     _text_search        (default
            │          == True           filesystem)
            │               │                 │
            ▼               ▼                 ▼
   embedding-vector    FTS5 / Postgres   case-insensitive
   cosine match        tsquery /          substring +
   (pgvector,          equivalent         frontmatter-tag
   future #258         (SQLite-FTS5       match, ordered
   family)              ships PR 2)       by match count

   ⚠ If BOTH flags True, semantic WINS. FTS infrastructure is not
     exercised on this code path even if also present.
     v1.1+ may add a `mode` kwarg if operators surface a need.
```

| Condition | Required behavior |
|-----------|-----------------|
| `supports_semantic_search=True` | MUST use embedding-vector cosine match. FTS NOT exercised even if `supports_full_text_search` is also True. |
| `supports_semantic_search=False`, `supports_full_text_search=True` | MUST use indexed full-text search (SQLite FTS5 / Postgres tsquery / equivalent). |
| Both False (filesystem default) | MUST fall back to case-insensitive substring + frontmatter-tag match, ordered by match count. |

### Versioning layout

Versioning mirrors MemoryBackend's `.versions/` pattern (spec/20:228-233). Cross-Protocol uniformity: operators learn the snapshot shape once.

**Filesystem layout:**

```
<agent_root>/
  wiki/
    *.md                         # wiki pages (INDEX.md excluded from list_pages)
    INDEX.md                     # routing index (returned by render_index_summary)
    .versions/
      <page-stem>/
        <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md    # immutable page snapshots
  raw/
    **                           # source documents (any depth)
    .versions/
      <page-stem>/
        <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md
```

`<YYYYMMDDTHHMMSSffffffZ>_<8hex>` snapshot file name format matches `memory/filesystem.py`'s `_version_filename` helper verbatim (prep-pass checklist item 6). `FilesystemCorpusBackend` MUST copy that helper, not reinvent it.

**SQLite hybrid layout:**

SQL stores metadata and FTS5 index. Page bodies and version snapshots live on disk under `<content_root>/<agent_scope>/<corpus>/` following the same path structure as the filesystem reference. This keeps the SQL store small and version snapshot listing to a simple `glob` without a JOIN. The INSERT-first + `atomic_write`-on-success-only atomicity pattern from ToolRegistryBackend (spec/25, #64 PR 3 precedent) prevents orphan SQL rows on disk-write failure.

---

## Path-traversal protection

Per prep-pass finding S1. The `name` parameter on every public method MUST pass through `_validate_corpus_name()` before any storage or dict access. The `corpus` parameter MUST be validated via `_validate_corpus_type()`.

```python
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.+@-]+$")

def _validate_corpus_name(name: str) -> None:
    """Validate a corpus page name at the API boundary.

    Refuses path-traversal tokens, control characters, leading dots,
    and names that don't match the allowed charset.
    """
    if not isinstance(name, str):
        raise ValueError(f"corpus page name must be a str, got {type(name)!r}")
    if name.startswith("."):
        raise ValueError(f"corpus page name must not start with '.': {name!r}")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"corpus page name contains path-traversal token: {name!r}")
    if any(ord(c) <= 0x1f or ord(c) == 0x7f for c in name):
        raise ValueError(f"corpus page name contains control character: {name!r}")
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"corpus page name must match [a-zA-Z0-9_.+@-]+, got {name!r}"
        )

def _validate_corpus_type(corpus: str) -> None:
    """Validate the corpus parameter is one of 'wiki' or 'raw'."""
    if corpus not in ("wiki", "raw"):
        raise ValueError(f"corpus must be 'wiki' or 'raw', got {corpus!r}")
```

`_validate_corpus_name` copies the pattern verbatim from `persona/filesystem.py:88-133` (`_validate_persona_id`). Charset `[a-zA-Z0-9_.+@-]+` matches the PolicyBackend `_AGENT_NAME_PATTERN` for cross-Protocol uniformity. `_validate_corpus_type` uses a simple `not in` check rather than the regex; `Literal["wiki","raw"]` type-safety at construction, value-safety at runtime.

Additionally, every write path calls `_io.safe_resolve_under(target, base_dir)` to prevent path-traversal through symlinks. Violations raise `WritePathViolation` (re-used from MemoryBackend; no new exception needed).

---

## Cost surface

Per Premise P5 (revised at /plan-eng-review 2026-05-29, finding A3). Cost emission for `query()` is **implementer-responsibility**, not framework-automatic.

The honest split:

- **Embedding calls routed through `LLMBackend`** (Anthropic, OpenAI): `_check_cost_guardrails` already catches the underlying LLM call as a side effect of going through the LLMBackend surface. No new wiring required on the CorpusBackend side; cost-cap math falls out of the existing path.
- **Embedding calls NOT routed through `LLMBackend`** (local `sentence-transformers`: zero per-call cost; managed Pinecone / Qdrant: per-query cost): the backend implementer is responsible for emitting cost events. The framework provides `CostEstimatorRegistry` at `atomic_agents/judge/cost_estimator_registry.py:80` as the operator-facing extension point. Operators register `register_cost_estimator("corpus_pinecone_query", ...)` the same way MandateCheck uses it for `stripe_charge`.
- **Cap-violation events**: emit the existing `policy_decision(axis="cost_cap", ...)` shape unchanged. No new `axis` Literal value. No spec/32 amendment. No breaking change to `policy/types.py:220`.

MemoryBackend's `search()` method has zero cost integration in the framework code today; the prior framing ("flow through the existing `_check_cost_guardrails` path") overstated what the framework provides. This spec corrects the record.

---

## Backend registry

Per finding A4 from /plan-eng-review 2026-05-29 (dominant 6-of-10 placement precedent):

```python
# atomic_agents/corpus/__init__.py

def register_corpus_backend(backend_id: str, cls: type[CorpusBackend]) -> None: ...
def unregister_corpus_backend(backend_id: str) -> None: ...
def get_corpus_backend(backend_id: str) -> type[CorpusBackend]: ...
def list_corpus_backends() -> list[str]: ...
def get_default_corpus_backend(agent_root: Path) -> CorpusBackend: ...
```

- `register_corpus_backend`: silently replaces on collision (matches the 10-arc precedent). The `_bootstrap_filesystem()` call at module bottom is idempotent.
- `unregister_corpus_backend`: idempotent (no-op if absent). Used by conformance fixtures for register-in-setup + unregister-in-teardown hygiene.
- `get_corpus_backend`: returns the registered class. Raises `CorpusBackendNotRegistered` when id is not in the registry.
- `list_corpus_backends`: returns registered backend ids in lexicographic order.
- `get_default_corpus_backend`: honors `ATOMIC_AGENTS_CORPUS_BACKEND` env var (default `"filesystem"`). Unknown values raise `CorpusBackendNotRegistered` with credential-redacted error messages. When `ATOMIC_AGENTS_CORPUS_BACKEND_URL` is set and the backend is `"filesystem"`, the URL is passed directly to `make_filesystem_corpus_backend_from_url`.

The `"filesystem"` backend is auto-registered on import: `from atomic_agents.corpus import register_corpus_backend` immediately gives the operator access to `FilesystemCorpusBackend` without an explicit registration call.

When `ATOMIC_AGENTS_CORPUS_BACKEND=sqlite` is set without a URL, the default resolves to `<agent_root>/.corpus.db` with `agent_scope=<agent_root.name>`. Single-host operators get a working SQLite default by flipping ONE env var, matching the AgentProfile + ToolRegistry precedent.

---

## Operator override surface

**Environment variables:**

- `ATOMIC_AGENTS_CORPUS_BACKEND`: backend id string (default `"filesystem"`). Follows the established `ATOMIC_AGENTS_<PRIMITIVE>_BACKEND` pattern.
- `ATOMIC_AGENTS_CORPUS_BACKEND_URL`: optional URL. For SQLite: `sqlite:///path/to/corpus.db?agent_scope=<name>`. For filesystem: `filesystem:///path/to/agent/root`.

**Constructor kwarg:**

The `AtomicAgent(..., corpus_backend=...)` constructor kwarg always wins over the env var (programmatic path beats environment).

**Per-runner kwargs:**

`OutcomeRunner`, `EvalRunner`, and `DreamRunner` accept `corpus_backend=...` constructor kwargs that thread through to internal sub-agents. `OutcomeRunner` threads in `outcome/_outcome_impl.py`, `EvalRunner` at `eval.py:363`, `DreamRunner` stores as `self._corpus_backend` (no internal `AtomicAgent` construction site in v1).

**`delegate.py` threading:**

`delegate.py` threads `corpus_backend` ONLY when the operator supplied it explicitly via the `AtomicAgent(..., corpus_backend=...)` kwarg (`_corpus_backend_was_explicit` flag tracked at `agent.py` construction). Default-resolved backends do not leak the coordinator's `agent_root` to delegates. Mirrors PersonaBackend's `D-ER-2` pattern (spec/33 §"`delegate.py` threading"). Corpus is per-agent semantic context -- distinct from fleet-scoped Policy + AgentProfile, which always thread. Operators who want a shared corpus backend across a coordinator and its delegates pass `corpus_backend=` explicitly.

---

## Exceptions

All corpus exceptions live in `atomic_agents/exceptions.py` (per finding M2 from prep-pass and the Persona D-PI-1 precedent) and are re-exported from `atomic_agents.corpus` for ergonomic access.

```python
class CorpusError(Exception): ...
    # Base class for all corpus subsystem errors.

class CorpusPageNotFound(CorpusError): ...
    # read_page / snapshot / restore_version called when the page
    # does not exist. Distinct from read_page returning None — this
    # is raised when an action presupposes existence (e.g., snapshot
    # a page that has since been deleted).

class CorpusPageExists(CorpusError): ...
    # write_page Case 4 (collision): page exists, content differs,
    # and no expected_content_sha256 was supplied.
    # Safe default refusal; operator must supply the CAS hash to overwrite.

class CorpusPreconditionFailed(CorpusError): ...
    # write_page Case 4 (CAS mismatch): page exists, content differs,
    # and expected_content_sha256 was supplied but does not match the
    # current on-disk hash. Mirrors MemoryPreconditionFailed (spec/20:318).

class CorpusVersionNotFound(CorpusError): ...
    # read_version called with a VersionRef whose body is not accessible.
    # This covers BOTH "version does not exist" AND "SQL row exists but
    # on-disk body file is missing" under the hybrid storage shape.

class CorpusInvalidName(CorpusError): ...
    # charset / path-traversal refusal at the API boundary.
    # Raised by _validate_corpus_name() on any disallowed name.

class CorpusBackendNotRegistered(CorpusError): ...
    # get_corpus_backend() called for an unknown backend_id.

class CorpusEmbeddingProviderUnavailable(CorpusError): ...
    # Raised when supports_semantic_search=True but the embedding
    # provider is unreachable (network, auth, missing model).
    # Reserved for the future PgvectorCorpusBackend family (#258).
```

`WritePathViolation` is re-used from MemoryBackend (no new exception for path enforcement; `safe_resolve_under` is the shared primitive).

---

## `FilesystemCorpusBackend` storage layout

```
<agent_root>/
  wiki/
    INDEX.md                         # routing index; returned by render_index_summary("wiki")
    *.md                             # wiki pages (INDEX.md EXCLUDED from list_pages results)
    .versions/
      <page-stem>/
        <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md
  raw/
    *                                # source documents (recursive walk)
    .versions/
      <page-stem>/
        <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md
```

`list_pages(corpus="wiki")` MUST skip `INDEX.md` (it is returned by `render_index_summary`, not a page) and any dot-prefixed entries (`.versions/`, `.gitkeep`, etc.). This is the same exclusion pattern `persona/filesystem.py:list_agents()` uses for dot-prefixed entries.

`render_index_summary("raw")` returns an empty string; raw corpora typically have no INDEX equivalent.

All page writes and all version snapshot writes go through `_io.atomic_write`. Never `target.write_text(...)` directly.

---

## `SQLiteCorpusBackend` storage layout

```python
SQLiteCorpusBackend(
    db_path: str | Path,
    agent_scope: str,
    *,
    content_root: Path | None = None,
)
```

SQL stores metadata + FTS5 virtual table. Page bodies and version snapshots live on disk under `<content_root>/<agent_scope>/<corpus>/` (hybrid shape matches ToolRegistryBackend's `handlers_root` precedent; rejected base64-exec'd-bodies design at /plan-subagent because it silently breaks closures + module-level imports).

Schema:

```sql
pages (
    agent_scope   TEXT NOT NULL,
    corpus        TEXT NOT NULL CHECK (corpus IN ('wiki', 'raw')),
    name          TEXT NOT NULL,
    title         TEXT,
    body_path     TEXT NOT NULL,       -- absolute path to on-disk body file
    byte_size     INTEGER,
    last_modified REAL,                -- Unix timestamp (REAL for fractional seconds)
    -- Typed date columns: stored as ISO strings, re-parsed on read
    captured      TEXT,                -- date | None; date.fromisoformat() on read
    last_seen     TEXT,                -- date | None
    expires_at    TEXT,                -- date | None
    ingested_at   TEXT,                -- datetime | None; datetime.fromisoformat() on read
    pinned        INTEGER DEFAULT 0,   -- bool; 0=False, 1=True
    -- Full YAML frontmatter dict serialized as JSON (for FTS5 indexing;
    -- CorpusPage is reconstructed from typed columns, NOT from this blob)
    frontmatter_json TEXT,
    PRIMARY KEY (agent_scope, corpus, name)
)

-- FTS5 virtual table with unicode61 tokenizer. External-content mode
-- backed by pages. Body text is maintained explicitly in write_page (after
-- the disk write succeeds) rather than via triggers alone, so the on-disk
-- page body is searchable (not just frontmatter).
-- External-content FTS5 with manual maintenance via direct INSERT in write_page.
pages_fts USING fts5 (
    name, body, frontmatter_json,
    content=pages,
    tokenize='unicode61'
)

-- External-content triggers maintain FTS structural integrity.
-- write_page explicitly upserts FTS with the real body text after disk write,
-- so the body is searchable. Triggers insert '' for body (triggers cannot
-- read external filesystem files); the explicit FTS upsert corrects this.
CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, name, body, frontmatter_json)
    VALUES (new.rowid, new.name, '', new.frontmatter_json);
END;

CREATE TRIGGER pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, name, body, frontmatter_json)
    VALUES ('delete', old.rowid, old.name, '', old.frontmatter_json);
END;

CREATE TRIGGER pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, name, body, frontmatter_json)
    VALUES ('delete', old.rowid, old.name, '', old.frontmatter_json);
    INSERT INTO pages_fts(rowid, name, body, frontmatter_json)
    VALUES (new.rowid, new.name, '', new.frontmatter_json);
END;

meta (key TEXT PRIMARY KEY, value TEXT)   -- schema_version tracking
```

Cross-corpus isolation: every query includes `WHERE agent_scope = ? AND corpus = ?` (double discriminator). Two corpora sharing the same `agent_scope` are fully isolated at the SQL layer. Two `agent_scope` values sharing the same db file are fully isolated.

`PRAGMA busy_timeout=5000` is set BEFORE `PRAGMA journal_mode=WAL` (resolves the multi-process WAL race; same fix as ToolRegistryBackend #64 PR 3 and LogBackend #61). The WAL transition itself runs inside a 7-attempt exponential-backoff retry loop (same shape as the #208 fix in `logs/sqlite.py`) because even with `busy_timeout` set, `PRAGMA journal_mode=WAL` can surface `database is locked` immediately when N processes race the very first transition on a fresh file. Cold-start init uses idempotent `INSERT OR IGNORE` for `meta` rows (multi-replica safe; same as ProfileBackend precedent).

**`write_page` transaction discipline (Round 1 adversarial, C1-C3):** The entire read-validate-UPSERT-FTS sequence inside `write_page` runs under a single `BEGIN IMMEDIATE` transaction. IMMEDIATE takes a reserved lock at BEGIN, serializing concurrent writers and eliminating the TOCTOU window between the existence check and the UPSERT. The FTS5 explicit upsert (replacing the trigger-inserted empty-body row with real body text) is INSIDE this transaction, not after it. If FTS upsert raises, the whole transaction rolls back: no SQL row lands, and `atomic_write` (which runs after COMMIT) never fires. `supports_full_text_search=True` means writes index successfully or they fail loudly; silent FTS degradation is not acceptable.

The snapshot on CAS overwrite (`_sqlite_take_snapshot`) is called with the old body content as a string (read from disk before the UPSERT fires), not with the new body path. This ensures the auto-snapshot captures the pre-overwrite state. The `atomic_write` for the body runs after COMMIT. If `atomic_write` fails, a compensating transaction restores prior SQL+FTS state: DELETE for a fresh write (Case 1); `INSERT ... ON CONFLICT(agent_scope, corpus, name) DO UPDATE SET ...` of the prior row + FTS restore for a CAS overwrite (Case 3). The `ON CONFLICT DO UPDATE` shape (matching the initial UPSERT) preserves the SQLite rowid, keeping the FTS5 rowid stable across the compensation. If `atomic_write` raised after the rename completed (rare: post-rename parent-directory fsync failure), the new body is on disk and the SQL row is preserved with the new metadata; a WARNING is logged and the original error is re-raised so the caller knows the durability guarantee was weakened.

**Future metadata-only mutations and the FTS5 trigger gap.** The three pages-table triggers (`pages_ai`, `pages_au`, `pages_ad`) write `body=''` to `pages_fts` on every INSERT/UPDATE/DELETE because triggers cannot read filesystem files. Only `write_page` currently follows the trigger with an explicit FTS5 upsert that inserts the real body text. Any future code path that mutates the `pages` table (a `pin_page`, `rename_page`, or partial metadata update) without immediately upserting `pages_fts` with the real body will silently overwrite the FTS body index with empty content, causing `query()` to miss that page's body content. PR 3+ authors adding metadata-only mutations must extend the explicit FTS upsert to those paths.

**Typed column reconstruction on read:** `read_page` reconstructs `CorpusPage` from the typed SQL columns (`captured`, `last_seen`, `expires_at`, `ingested_at`, `pinned`) and the on-disk body file. It does NOT reconstruct from `json.loads(frontmatter_json)` for typed fields. `frontmatter_json` is for FTS5 indexing and extra-frontmatter round-trip only.

URL factory: `make_sqlite_corpus_backend_from_url("sqlite:///path/to/corpus.db?agent_scope=<name>")`. Credentials redacted from all 6 `ValueError` sites via `_redact_url`:
1. Non-sqlite scheme
2. netloc present
3. Fragment present
4. Duplicate query parameter
5. Unknown query parameter (anything besides `agent_scope`)
6. Empty or root-only path

---

## Path-traversal protection in FilesystemCorpusBackend

Implementation checklist (sourced from prep-pass SEVERE S1):

1. Copy `_validate_persona_id` pattern verbatim from `persona/filesystem.py:98-133` as `_validate_corpus_name` in `corpus/filesystem.py`.
2. Define separate `_validate_corpus_type` using `not in ("wiki","raw")` check, not the regex.
3. Constructor stores `self._agent_root = Path(agent_root)` and nothing else. No stat, no mkdir, no walk.
4. Every page write and every version snapshot write via `_io.atomic_write`. Never `target.write_text(...)` directly.
5. Copy `_redact_url` verbatim from `persona/filesystem.py:177-197`. Wrap every `ValueError` in both URL factories.
6. Copy `_version_filename` from `memory/filesystem.py:712-716`. Version layout `<agent_root>/<corpus>/.versions/<page-stem>/<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md`.
7. Place `CorpusError` + 8 specific exceptions in `atomic_agents/exceptions.py`. Not in `corpus/types.py`.
8. `list_pages(corpus="wiki")` MUST skip `INDEX.md` and any dot-prefixed entries.
9. Conformance test reads `docs/samples/caldwell/wiki/avalanche_vs_snowball.md` and asserts every named field parses correctly without falling into `extra_frontmatter`.

---

## Implementer contract for corpus backends

A backend that implements the `CorpusBackend` Protocol commits to the contract below. The reference `FilesystemCorpusBackend` is the canonical example; `SQLiteCorpusBackend` is the second reference impl; future Postgres / pgvector / SaaS adapters slot in via `register_corpus_backend(...)` without forking core.

The MUST count follows the 7-8 MUST range of prior arcs (spec/22: 7, spec/24: 8, spec/25: 8, spec/29: 8, spec/32: 7, spec/33: 8). CorpusBackend locks at 9 because the FTS5 / semantic / substring `query()` precedence rule is an additional cross-cutting contract that prior arcs without a capability-gated query path did not require.

Implementers MUST:

1. **`name` and `corpus` charset validation at API boundary.** Every Protocol method that accepts `name` validates it against `[a-zA-Z0-9_.+@-]+` BEFORE any storage or dict access. Reject path-traversal tokens (`..`, `/`, `\`), control characters (`\x00`-`\x1f`, `\x7f`), newlines, leading dots, and empty strings; raise `CorpusInvalidName`. Every method that accepts `corpus` validates `corpus in ("wiki", "raw")`; raise `ValueError` otherwise. The validation is at the API boundary, not inside storage helpers; callers that bypass it violate the contract. Reference: `_validate_corpus_name` + `_validate_corpus_type` in `corpus/filesystem.py`.

2. **Side-effect-free construction.** Backend `__init__` MUST NOT stat the filesystem, query a database, call an external API, or read any environment variable. The first method call performs lazy initialization. Malformed operator config surfaces on the first method call, not at construction. Preserves the framework's byte-identical-construction promise for the existing `AtomicAgent(...)` test sites. Profile's "validate existence" pattern is the WRONG precedent here; corpus directories may legitimately not exist on fresh agents.

3. **Capability honesty.** `capabilities -> CorpusCapabilities` is a contract, not a hint. Backends declaring `supports_versioning=False` MUST raise `NotImplementedError` on `list_versions`, `read_version`, `restore_version`, and `snapshot`. Backends declaring `supports_full_text_search=True` MUST use indexed FTS (FTS5 / tsquery / equivalent) in `query()` when `supports_semantic_search` is False. `embedding_provider` MUST be `None` when `supports_semantic_search=False`. Backends that advertise a flag True but do not implement the corresponding behavior produce silent failures rather than loud refusals; conformance tests gate on capability flags.

4. **`query()` capability precedence rule.** When `supports_semantic_search=True`, the backend MUST use embedding-vector cosine match and MUST NOT exercise FTS infrastructure on that code path, even if `supports_full_text_search` is also True. When `supports_semantic_search=False` and `supports_full_text_search=True`, the backend MUST use indexed full-text search. When both are False, the backend MUST fall back to case-insensitive substring + frontmatter-tag match, ordered by match count. No caller-choice override in v1.0; v1.1+ may add a `mode` kwarg if operators surface a need.

5. **`write_page()` 4-case behavior table.** (a) Fresh write: page does not exist, write via `_io.atomic_write`, update INDEX-equivalent. (b) Content-identical idempotent no-op: page exists, body + frontmatter SHA-256 unchanged, no-op, safe for re-delivery. (c) Explicit overwrite via CAS: page exists, content differs, `expected_content_sha256` matches current hash, snapshot if `supports_versioning=True`, write new content, update INDEX-equivalent. (d) Collision: page exists, content differs, `expected_content_sha256` is None, raise `CorpusPageExists`; content differs, hash supplied but mismatched, raise `CorpusPreconditionFailed`. CAS via `expected_content_sha256` is the ONLY safe overwrite path; silent overwrite without operator intent is refused by default.

6. **URL credential redaction across all operator-facing error paths.** URL factories, `get_default_corpus_backend`, and `doctor.check_corpus_backend` error paths MUST NOT echo raw URL credentials. The reference impls use `_redact_url` (filesystem factory) and `_redact_for_error_message` (corpus/__init__.py) helpers that strip credentials after `://` and truncate. Operators may accidentally paste `postgres://user:password@host/db` into env vars; the error message MUST NOT echo the password. The SQLite URL factory covers 6 `ValueError` sites (non-sqlite scheme, netloc present, fragment present, duplicate query parameter, unknown query parameter, empty or root-only path), all redacted.

7. **Cross-corpus isolation at storage layer.** `wiki` and `raw` corpora are fully independent. SQLite backends MUST include `WHERE corpus = ?` (or equivalent) on every query; no `wiki` query may touch `raw` rows and vice versa. Filesystem backends enforce isolation geometrically via separate subdirectories. A conformance test verifies that writing a page to `corpus="wiki"` does not make it visible via `corpus="raw"` and vice versa.

8. **Snapshot id determinism and cross-page isolation.** Backend-issued `VersionRef` tokens MUST be monotonic or sortable, supporting `list_versions` returning chronological order. Cross-page isolation MUST be enforced at the storage layer: a `VersionRef` issued for page A MUST raise `CorpusVersionNotFound` when passed to `read_version` or `restore_version` in the context of page B. The `<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md` filesystem version filename format (from `memory/filesystem.py:_version_filename`) provides this guarantee geometrically via the `<page-stem>/` subdirectory.

9. **`backend_id` property stable across calls; `close()` idempotent.** The `backend_id` property MUST return the same string across calls and MUST match what `list_corpus_backends()` registered the class under. Backends with `backend_id="filesystem"` re-registered under `"sqlite"` violate conformance. `close()` is a method-level contract documented at `corpus/backend.py:388-389` and MUST be idempotent: calling it twice MUST NOT raise. Database backends that hold connection pools MUST guard teardown on a `_closed` flag or equivalent.

---

## Decisions

Decisions locked across the office-hours session (2026-05-29), /plan-eng-review (2026-05-29), and /plan-subagent pre-impl prep pass (2026-05-29).

| # | Topic | Lock |
|---|-------|------|
| A1 | `CorpusPage` field alignment | Named fields aligned to actual wiki frontmatter from `docs/samples/caldwell/wiki/avalanche_vs_snowball.md`. Invented field names (`topic`, `last_distilled`, `source_pages`, `version`) with zero precedent in real data were removed. |
| A2 | `query()` precedence rule | Semantic wins over FTS when both flags True. Documented in Protocol surface and Implementer Contract MUST #4. |
| A3 | P5 cost-surface honesty | Framework does NOT auto-integrate query cost with `_check_cost_guardrails`. MemoryBackend's `search()` has zero cost integration today; the prior framing overstated what the framework provides. Implementer emits via `CostEstimatorRegistry`. |
| A4 | `register_corpus_backend` placement | `__init__.py` per dominant 6-of-10 backend precedent. |
| CQ1 | `write_page()` 4-case behavior table | Fresh / idempotent / CAS / collision. `CorpusPreconditionFailed` added to exception hierarchy mirroring `MemoryPreconditionFailed`. |
| CQ2 | ASCII architecture diagrams | Two diagrams added (overall architecture + `query()` capability precedence). |
| P1 | `doctor.check_corpus_backend` page-count cliff WARN | When `supports_full_text_search=False` AND page count exceeds ~1000 pages (threshold tuned at PR 1), emit WARN with actionable hint to pin SQLite. |
| S1 | `_validate_corpus_name` from Persona pattern | Copy `_validate_persona_id` pattern verbatim from `persona/filesystem.py:98-133`. |
| S2 | `captured` / `last_seen` date type | Retyped from `datetime | None` to `date | None` to match PyYAML behavior loading bare ISO dates + MemoryBackend Note precedent (spec/20:53-58). |
| S3 | Lifecycle fields in `CorpusPage` | `expires_at`, `supersedes`, `superseded_by` added as named fields; landing them in `extra_frontmatter` would force callers to parse a dict for structural lifecycle semantics. |
| S4 | Regression suite for PR 3 | Zero test coverage exists today on the `wiki/INDEX.md` read path; all 9 existing wiki-touching integration tests create an empty `wiki/` dir and assert nothing about INDEX content. PR 3 IRON RULE regression suite is load-bearing for silent-corruption prevention. |
| S5 | `atomic_write` non-negotiable | Every page write + version snapshot write via `_io.atomic_write`. Never `target.write_text()` directly. |
| D-RC-1 | `read_page` vs `read_version` None/raise convention | `read_page` returns `None` (routine presence check). `read_version` raises `CorpusVersionNotFound` (unexpected infrastructure failure — SQL row exists but body file gone). Matches MemoryBackend's `read_note` vs `read_version` convention. Documented in Protocol contract so conformance test authors don't disagree. |
| D-RC-2 | `bundle.py:_source_paths` deferred | Filesystem-only function; SQLite has no wiki/INDEX.md path to return. v1.0 keeps the direct path check. Follow-up issue filed at #314. |

---

## Out of scope

Items considered and explicitly deferred, with rationale.

| Item | Why deferred |
|------|--------------|
| `PgvectorCorpusBackend` (semantic-search-via-embeddings) | Ships in #258 Postgres-adapter family coordinated with `PgvectorMemoryBackend` for symmetric semantic-search coverage across both substrates. Approach B (pgvector in v1.0 without PgvectorMemoryBackend) creates asymmetric coverage. |
| `EmbeddingBackend` as a separate Protocol | Premise P2: capability on CorpusBackend, not a third Protocol. No standalone use case identified. |
| Caller-choice `mode: Literal["auto", "semantic", "fts"]` kwarg on `query()` | v1.0 uses precedence rule (finding A2). v1.1+ may add if operators surface a need for "exact FTS match even when semantic is available" (e.g., literal-string lookup). Default `auto` would preserve v1.0 behavior. |
| `delete_page(name, corpus)` Protocol method | Consistent with MemoryBackend (which has no `delete_note`). Operators delete by editing files. |
| `list_pages_stream()` Protocol method | `list_pages(corpus, *, limit, offset)` covers paged iteration. Streaming deferred to v1.1+ if conformance testing surfaces a need at 100M+ page scale. |
| `resolve_links(page) -> list[CorpusRef]` Protocol method | Wiki pages reference each other (`[[other-page]]`). Link resolution lives in the agent layer (consumer-side), not the Protocol. Keeps the Protocol surface tight. |
| Multi-modal corpus pages (PDFs binary, images, audio transcripts) | All pages are markdown text in v1.0. Binary substrate is a v1.1+ scope expansion via a new capability flag. |
| Dream-distillation pipeline `write_page()` integration | No current framework-side wiki writes exist. `write_page()` ships in PR 1 for future-state use. The distillation pipeline migration is a separate arc. |
| `bundle.py:_source_paths` migration to Protocol | Filesystem-only function; SQLite has no equivalent path to track. Deferred to v1.1 with a follow-up issue filed at #314 (D13). |
| MCPServerRegistryBackend (#201) | Separate arc; v1.0 closes when this AND #65 ship. |

---

## Test coverage

**PR 1 (~35 total):**

- ~25 parametrized conformance tests in `tests/test_corpus_protocol_conformance.py` — fixture accepts any registered `CorpusBackend` impl and parametrizes across both corpora (`wiki` + `raw`). Covers:
  - `list_pages` empty / populated / paged / INDEX.md excluded from wiki results
  - `read_page` present / absent (returns None)
  - `render_index_summary` present / empty (returns empty string)
  - `write_page` all 4 cases (fresh / idempotent / CAS / collision)
  - `query()` with capability fallback to substring match (filesystem)
  - `list_versions` / `read_version` / `snapshot` / `restore_version` (capability-gated skip)
  - `stats()` page-count and byte-size accuracy
  - `close()` idempotent
  - charset validation at API boundary (traversal attempts raise `CorpusInvalidName`)
  - cross-corpus isolation (wiki write not visible via raw and vice versa)
  - conformance test reads `docs/samples/caldwell/wiki/avalanche_vs_snowball.md` and asserts all named `CorpusPage` fields parse correctly without falling into `extra_frontmatter` (T1 from eng-review)
- ~10 filesystem-specific tests in `tests/test_corpus_filesystem_backend.py`:
  - `.versions/` layout and snapshot filename format
  - `INDEX.md` skip in `list_pages`
  - `render_index_summary("wiki")` reads `INDEX.md` content verbatim
  - `render_index_summary("raw")` returns empty string
  - `_validate_corpus_name` covers leading-dot, traversal, control chars
  - `_validate_corpus_type` refuses unknown corpus values
  - `atomic_write` usage (write fault injection verifies no partial state)
  - path-traversal refusal via `safe_resolve_under`
- 4 registry primitive tests in `tests/test_corpus_registry.py`:
  - `register_corpus_backend` / `unregister_corpus_backend` round-trip and collision-replace semantics
  - `get_corpus_backend` raises `CorpusBackendNotRegistered` on unknown id
  - `list_corpus_backends` returns ids in lexicographic order
  - `get_default_corpus_backend` honors the `ATOMIC_AGENTS_CORPUS_BACKEND` env var

**PR 2 (~46 actual, 35 estimated):**

SQLite-specific tests in `tests/test_corpus_sqlite_backend.py`. The 11 tests above the original estimate cover Round 1 and Round 2 adversarial fix regressions (transaction discipline, FTS5 rollback, compensation logic, `top_k` validation, schema mismatch detection, `bool` input guard). Covers:
- CRUD across both corpora
- FTS5 query happy path / empty result / special-char input (FTS5 parse error handling)
- Versioning under the hybrid storage shape (SQL row + on-disk body)
- `read_version` when SQL row exists but body file missing (raises `CorpusVersionNotFound`)
- WAL race under multi-thread contention (after `PRAGMA busy_timeout=5000` fix)
- Cold-start under multi-process concurrent init (idempotent `INSERT OR IGNORE`)
- URL factory + credential redaction across all 6 `ValueError` sites
- corpus discriminator isolation (`WHERE corpus = ?` on every query)
- Hybrid-storage half-failure (INSERT-first + atomic_write-on-success-only)

**PR 3 (~30 total):**

Wiring + regression tests in new and modified test files. Includes the 5 IRON RULE regression assertions (finding S4 from prep-pass):

1. `agent.py:_load_indexes()` with `corpus_backend=None` produces byte-identical `_wiki_index_text` to pre-#65 (direct `wiki_index.read_text()` path preserved).
2. `agent.py:_load_indexes()` with `corpus_backend=FilesystemCorpusBackend(agent_root)` produces output identical to the `None` case for the same on-disk wiki content.
3. `bundle.py:_render_memory_breakpoint(instance_root, corpus_backend=None)` produces byte-identical bundle output to pre-#65.
4. `bundle.py:_render_memory_breakpoint(instance_root, corpus_backend=FilesystemCorpusBackend(...))` produces output identical to the `None` case for the same on-disk content.
5. The full pre-#65 test suite (2691 + 48 skipped at 2026-05-29) passes unchanged when no `corpus_backend` is configured at any of the 166 existing `AtomicAgent(...)` construction sites.

Plus: per-runner kwargs, delegate threading (`_corpus_backend_was_explicit` flag), env var override, doctor PASS/WARN/FAIL, page-count cliff WARN, CLI SQLite-path activation.

**Total arc: ~111 tests** (PR 1 ~35 + PR 2 ~46 actual + PR 3 ~30). The PR 2 overage relative to the original ~35 estimate is from adversarial fix regressions added in Round 1 and Round 2 reviews. The original arc estimate in issue #65 was ~70, which excluded PR 3 wiring tests; this is the post-eng-review corrected count updated with the PR 2 as-delivered figure.

---

## Call-site migration reference

The wiring contract described in this section is implemented. Both call sites migrated; the 5 IRON RULE regression assertions in `tests/test_corpus_migration_regression.py` pin the byte-identity guarantees. The `_source_paths` row remains deferred to v1.1 as documented.

| File | Function (line) | Current pattern | New pattern |
|------|-----------------|-----------------|-------------|
| `agent.py` | `AtomicAgent.__init__` (2937-2939) | `wiki_index.read_text()` direct `Path` read | `self.corpus.render_index_summary(corpus="wiki")` when `self.corpus_backend is not None`, else fall back to the direct read. Single `if self.corpus_backend is None:` branch. |
| `agent.py` | `AtomicAgent` prompt assembly (3058-3059) | uses already-read `self._wiki_index_text` | unchanged (the read happens upstream at lines 2937-2939) |
| `bundle.py` | `_render_memory_breakpoint(instance_root)` at line 494 | `wiki_dir / "INDEX.md"` direct path read at line 497 | function signature gains `corpus_backend: CorpusBackend | None = None`; when not None, calls `corpus_backend.render_index_summary(corpus="wiki")`; when None, falls back to the direct path. Callers thread the parameter through. Note: this is NOT a `getattr` pattern — `bundle.py`'s call path has no `AtomicAgent` instance; the parameter is explicit. |
| `bundle.py` | `_source_paths(agent_root)` at line 266 | `wiki_dir / "INDEX.md"` appended to path list at line 295 | **Deferred to v1.1.** SQLite backends have no `wiki/INDEX.md` file path to track. v1.0 keeps the direct path check. Follow-up issue filed at #314. |

**What does NOT need migration:** `dashboard/memory.py` does not touch wiki at all (verified). `agent.py:721` (`self._wiki_index_text: str = ""`): unchanged (it buffers the read result; the new read happens upstream at 2937-2939). `migrate.py:233 + 595-606`: references wiki/raw paths as part of vault migration utilities — those stay outside CorpusBackend (migrate is a one-shot operator tool).

**Crucial fact:** there are NO writes to `<agent>/wiki/` or `<agent>/raw/` in the framework code today. Dream output writes to `dream_dir/report.md` (a separate output directory); operator additions are manual file edits. The call-site migration scope is reads through `render_index_summary` only, not `write_page` migrations, because there are no write sites to migrate.

Both fallback shapes preserve byte-identical pre-#65 behavior. The IRON RULE regression suite (5 explicit assertions above) is the enforcement mechanism.

---

## Failure modes

| Codepath | Realistic production failure | Test coverage | Error handling |
|----------|------------------------------|---------------|-----------------|
| `FilesystemCorpusBackend.list_pages` on disk-full | OS raises `OSError` mid-walk | Tests cover empty/missing dirs; OS-error is OS-bubbled | Bubbles up (operator sees `OSError`) |
| `FilesystemCorpusBackend.write_page` on disk-full | `_io.atomic_write` fails atomically | Inherited from MemoryBackend test suite | Atomic — no half-written state |
| `FilesystemCorpusBackend.write_page` Case 4 (collision, no CAS) | Two operators edit same wiki page simultaneously | PR 1 conformance test covers explicitly | Raises `CorpusPageExists` |
| `FilesystemCorpusBackend.read_version` with corrupt snapshot file | YAML parse error on snapshot | Explicit test planned for PR 1 | Propagates `yaml.YAMLError` |
| `SQLiteCorpusBackend.write_page` SQL row succeeds, on-disk body write fails | Hybrid-storage half-failure | INSERT-first + atomic_write-on-success-only pattern (TOCTOU-safe per #64 PR 3 precedent) | Atomic rollback on disk failure; no orphan SQL row |
| `SQLiteCorpusBackend.read_version` SQL row exists, on-disk body missing | Disk corruption / external delete | Explicit test planned for PR 2 | Raises `CorpusVersionNotFound` per contract |
| `SQLiteCorpusBackend.query` with FTS5 special-char query (e.g., `"`) | FTS5 parse error on non-escaped input | PR 2 explicit special-char + Unicode tests | `sqlite3.OperationalError` bubbles up |
| `SQLiteCorpusBackend` cold start under multi-process concurrent init | Two replicas claim schema_version simultaneously | PR 2 covers idempotent `INSERT OR IGNORE` | No-op on losers |
| `SQLiteCorpusBackend` WAL race | `PRAGMA busy_timeout=5000` before WAL pragma fix | PR 2 covers under multi-thread contention | Bounded retry (5s default) |
| `agent.py:_load_indexes()` with `corpus_backend=None` post-PR 3 | Operator without `corpus_backend` configured | PR 3 IRON RULE regression test #1 | Falls back to direct read (byte-identical pre-#65) |
| `bundle.py:_render_memory_breakpoint` with `corpus_backend=None` | Same as above | PR 3 IRON RULE regression test #3 | Falls back to direct path |
| `delegate.py` accidentally threads default-resolved backend | Bug in `_corpus_backend_was_explicit` flag | PR 3 covers default-no-thread case | Delegate uses its own default |
| `doctor.check_corpus_backend` page-count probe slow on huge corpus | `stats(corpus).page_count` on 100K-page wiki | Benchmark / skip-if-too-large test planned | Bounded by `stats()` implementation |

No failure mode is both silent AND lacking planned coverage. Every failure either has a test, an error-handling path, or both.

---

## References

- `docs/spec/20-memory-backend.md` — MemoryBackend Protocol; the template this arc follows. `VersionRef`, `WritePolicy`, `.versions/` layout, and the `read_note` None vs `read_version` raise convention all originate here.
- `docs/spec/24-agent-profile-backend.md` — Decision 7 (updated at #65 PR 4 of 4); Implementer Contract MUST count range; snapshot id entropy budget.
- `docs/spec/25-tool-registry-backend.md` — INSERT-first + atomic_write-on-success-only atomicity pattern for hybrid storage; `PRAGMA busy_timeout=5000` before WAL pragma precedent; URL factory + credential redaction across 5 `ValueError` sites.
- `docs/spec/27-doctor.md` — PASS/WARN/FAIL ladder shape; page-count cliff WARN precedent from LogBackend.
- `docs/spec/32-policy-backend.md` — `_policy_backend_was_explicit` precedent for explicit-only delegate threading; `policy_decision(axis="cost_cap")` event schema unchanged.
- `docs/spec/33-persona-backend.md` — Most recently locked Protocol spec; provides direct template for Implementer Contract structure, exception placement, `delegate.py` threading rationale, URL factory shape, and snapshot id format.
- `atomic_agents/judge/cost_estimator_registry.py:80` — `CostEstimatorRegistry` extension point for implementer-emitted query cost events.
- `atomic_agents/_io.py:42, 96` — `atomic_write` and `safe_resolve_under` primitives re-used by `FilesystemCorpusBackend`.
- `persona/filesystem.py:98-133, 177-197` — `_validate_persona_id` + `_redact_url` source patterns.
- `memory/filesystem.py:712-716` — `_version_filename` helper copied verbatim for version snapshot naming.
- `~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-design-20260529-092525.md` — Full architectural rationale: 8 premises (P1-P8 revised), 4 approaches considered, Approach C recommendation, /plan-eng-review findings (A1-A4, CQ1-CQ2, T1-T2, P1), /plan-subagent prep-pass findings (S1-S5, H1-H19, M1-M7, L1+), Reviewer Concerns 1-3, and implementation task list T1-T9.
- #65 — The umbrella issue.
- #258 — Postgres-adapter family coordinated release (PgvectorCorpusBackend + PgvectorMemoryBackend).

## spec/40 addendum — Canonical export

`CorpusBackend` participates in the **Exportable** companion Protocol (spec/40).

`CorpusCapabilities.supports_canonical_export = True` for `FilesystemCorpusBackend`.
`SQLiteCorpusBackend` defaults `False` until its export impl ships.

`export()` returns a `CorpusExport` carrying `pages_with_bytes: dict[corpus_name, list[(Page, raw_bytes)]]`.
Both `"wiki"` and `"raw"` corpora are exported by default; `CorpusExportQuery(corpus="wiki")`
filters to one. Export enumerates via `list_pages()` — NOT `query(text)` (MUST 6:
state extraction, not semantic retrieval).

For the full normative export contract, see `docs/spec/40-canonical-export.md`.

---

## Versioned normative addendum — CorpusCapabilities.embedding_backend_resolved (spec/34 PR-2 addendum, issue #200 PR3 / #544)

`CorpusCapabilities` already documents `embedding_provider: str | None` (line 75). `PgvectorCorpusBackend` exposes one additional field that was shipped in #200 PR3 but not yet documented:

**`embedding_backend_resolved: EmbeddingBackend | None`** — the live `EmbeddingBackend` instance held by the backend. Non-None when `supports_semantic_search=True`. Mirrors the same field on `MemoryCapabilities` (spec/20 PR-3 addendum).

**SNAPSHOT SECURITY CLAMP:** `embedding_backend_resolved` MUST serialize as `None` (or be absent) in any JSONL log record, profile snapshot, or network response. The live instance may carry API credentials (e.g. `OpenAIEmbeddingBackend._api_key`); leaking it into the audit trail violates Principle 5.

**Forward-compat note:** `CorpusCapabilities.embedding_backend_resolved` is populated only by `PgvectorCorpusBackend`. `FilesystemCorpusBackend` and `SQLiteCorpusBackend` return it as `None` (the dataclass default). Callers that inspect this field MUST treat a missing / `None` value as "no live embedding backend." Uniform convergence of the `capabilities()` surface across all backends is deferred to issue #431.

Added OUTSIDE the 9-MUST count, following the versioned-addendum precedent of spec/45 PR2 (#520) and spec/22 §Read-failure contract (#497), and mirroring the sibling spec/20 PR-3 addendum shipped in this same PR.

"""Canonical dataclasses for the CorpusBackend Protocol (spec/34).

Four dataclasses define the corpus substrate contract (issue #65, spec/34):

- ``CorpusCapabilities`` -- frozen capability advertisement for a backend
  instance; conformance tests assert claim-vs-behavior parity.
- ``CorpusRef`` -- lightweight listing token returned by ``list_pages()``
  and ``query()``; cheap to enumerate without loading page bodies.
- ``CorpusPage`` -- full read model (superset of ``CorpusRef``) returned
  by ``read_page()``. Named frontmatter fields mirror the actual wiki/note
  convention verified against ``docs/samples/caldwell/wiki/`` at
  /plan-eng-review 2026-05-29 (finding A1) and refined at /plan-subagent
  prep pass 2026-05-29 (Subagents 2 + 3).
- ``CorpusStats`` -- per-corpus health/stats snapshot returned by
  ``stats(corpus)``.

No exceptions live here. Per M2 (pre-impl prep 2026-05-29), corpus
exceptions live in ``atomic_agents/exceptions.py`` so that callers can
import ``CorpusPageNotFound``, ``CorpusPageExists``, and their siblings
without creating cross-module import cycles.

No Protocol definition lives here. The ``CorpusBackend`` Protocol is in
``atomic_agents/corpus/backend.py``.


"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal


# ──────────────────────────────────────────────────────────────────────────────
# CorpusCapabilities


@dataclass(frozen=True)
class CorpusCapabilities:
    """Capability advertisement for a CorpusBackend instance.

    Conformance tests assert claim-vs-behavior parity. Backends that misreport
    capabilities produce silent failures rather than loud refusals (spec/34
    Implementer Contract MUST -- finalized at PR 4 lock, per PersonaBackend
    precedent at spec/33).

    Fields:

    ``supports_semantic_search``: True if ``query()`` uses embedding-vector
        cosine similarity (pgvector or equivalent). When True, ``query()``
        MUST use semantic match regardless of whether ``supports_full_text_search``
        is also True (semantic wins the precedence rule, /plan-eng-review
        finding A2). Deferred to #258 Postgres-adapter family release in
        coordination with ``PgvectorMemoryBackend`` for symmetric coverage.

    ``supports_full_text_search``: True if ``query()`` uses indexed full-text
        search (SQLite FTS5, Postgres tsquery, or equivalent). When False and
        ``supports_semantic_search`` is also False, ``query()`` MUST fall back
        to case-insensitive substring + frontmatter-tag match ordered by match
        count (the ``FilesystemCorpusBackend`` default).

    ``supports_versioning``: True if the snapshot trio
        (``snapshot`` / ``restore_version`` / ``list_versions``) is fully
        implemented. ``FilesystemCorpusBackend`` and ``SQLiteCorpusBackend``
        both set this True.

    ``supports_streaming_iteration``: True if the backend prefers chunked
        iteration for ``list_pages()`` over in-memory collection. The
        Protocol's ``list_pages(corpus, *, limit, offset)`` paged shape
        covers both cases; a separate ``list_pages_stream()`` iterator is
        deferred to v1.1+ pending conformance-test feedback (Q4).

    ``embedding_provider``: The embedding model provider when
        ``supports_semantic_search=True``. Examples: ``"anthropic"``,
        ``"openai"``, ``"ollama"``, ``"local-sentence-transformers"``.
        MUST be ``None`` when ``supports_semantic_search=False``; conformance
        tests assert this invariant.
    """

    supports_semantic_search: bool
    supports_full_text_search: bool
    supports_versioning: bool
    supports_streaming_iteration: bool
    embedding_provider: str | None  # None when supports_semantic_search=False
    # spec/40 addendum: Exportable Protocol composition.
    # FilesystemCorpusBackend = True (full wiki+raw page export).
    # SQLiteCorpusBackend defaults False until its export impl ships.
    # Default False so existing instantiation sites without this kwarg keep working.
    supports_canonical_export: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# CorpusRef


@dataclass
class CorpusRef:
    """Lightweight listing token returned by ``list_pages()`` and ``query()``.

    Callers should prefer ``CorpusRef`` for listing and search operations and
    only call ``read_page()`` when they need the full body. Mirrors the
    ``NoteRef`` shape from ``atomic_agents/memory/backend.py`` at the listing
    layer; ``CorpusPage`` mirrors ``Note`` at the full-read layer.
    """

    name: str  # bare filename stem, e.g. "avalanche-vs-snowball"
    corpus: Literal[
        "wiki", "raw"
    ]  # type-safe discriminator; "wikki" fails at construction
    title: str  # frontmatter title or first H1 heading
    last_modified: datetime  # mtime or DB timestamp; used by CorpusStats.most_recent
    byte_size: int  # body size on disk (filesystem) or in SQL (SQLite)


# ──────────────────────────────────────────────────────────────────────────────
# CorpusPage


@dataclass
class CorpusPage:
    """Full read model (superset of CorpusRef) returned by ``read_page()``.

    Named fields mirror the actual frontmatter shape in existing wiki pages,
    verified against ``docs/samples/caldwell/wiki/avalanche_vs_snowball.md``
    at /plan-eng-review 2026-05-29 (finding A1). This is the same shape
    ``MemoryBackend.Note`` uses because wiki pages and atomic notes share the
    same author tooling (markdown + YAML frontmatter, edited in Obsidian /
    text editor).

    The Protocol stays separate from MemoryBackend per Premise P1 (different
    directory layout, different lifecycle, different query semantics). Only the
    frontmatter field NAMES are shared so operators learn the shape once.
    ``CorpusPage`` is its own dataclass and can grow wiki-specific fields
    later without touching ``Note``.

    Date-typed fields (``captured``, ``last_seen``, ``expires_at``) use
    ``date | None``, NOT ``datetime | None``. PyYAML loads bare ISO dates
    (``2026-04-22``) as ``datetime.date``, not ``datetime``. Prep-pass
    Subagent 2 caught this as SEVERE (S2): prior ``datetime`` typing would
    ``AttributeError`` on operators who write bare dates in frontmatter.
    Matches ``MemoryBackend.Note`` precedent (spec/20:53-58 + ``NoteRef``
    lines 38-39). The ``ingested_at`` raw-side field stays ``datetime | None``
    because automated ingest typically includes a time component.
    """

    ref: CorpusRef
    body: str  # full markdown content (body only, frontmatter stripped)

    # ── Frontmatter fields matching the existing wiki/note convention ──────
    # All ``None``-defaulted because frontmatter is partial in practice.
    # Verified against docs/samples/caldwell/wiki/avalanche_vs_snowball.md
    # at /plan-subagent prep pass 2026-05-29, Subagent 2.

    name: str | None = None  # human-readable page name (distinct from ref.name stem)
    description: str | None = (
        None  # one-line summary surfaced in INDEX-equivalent rendering
    )
    type: str | None = None  # "wiki_page" | "raw_doc" | operator-defined
    captured: date | None = None  # original capture / authoring time.
    # TYPE = date (not datetime) per MemoryBackend Note
    # precedent (spec/20:53-58) + PyYAML bare-date behavior.
    # Prep-pass Subagent 2 SEVERE S2: prior datetime typing
    # would AttributeError on operators using bare dates.
    last_seen: date | None = None  # last operator touch / re-distillation;
    # same date typing as ``captured`` per S2.
    sources: list[str] | None = (
        None  # for wiki pages: raw page names this distills from
    )
    provenance: str | None = None  # "distilled" | "ingested" | "operator_authored"
    confidence: str | None = None  # "high" | "medium" | "low"
    pinned: bool = False  # always-include in agent context (matches Note semantics)
    related: list[str] | None = None  # cross-references (wiki [[link]] targets)
    tags: list[str] | None = None  # operator-authored free-form tags
    schema_version: int | None = (
        None  # frontmatter schema version for future migrations
    )

    # ── Lifecycle / provenance fields ─────────────────────────────────────
    # These fields appear in real wiki frontmatter but were missed in the
    # first design pass. Added per /plan-subagent prep pass 2026-05-29,
    # Subagent 2 (HIGH finding H5 / SEVERE S3): landing them in
    # ``extra_frontmatter`` would force callers querying "is this page still
    # valid?" to parse the dict. All mirror MemoryBackend Note fields.

    expires_at: date | None = None  # page expiry (None = never expires);
    # date-typed per S2 (bare ISO date in frontmatter)
    supersedes: list[str] | None = (
        None  # page names this page replaces (forward provenance)
    )
    superseded_by: str | None = (
        None  # page name that replaces this one (backward provenance)
    )

    # ── Raw-side fields ───────────────────────────────────────────────────
    # Per issue #65's stated schema. Wiki pages typically don't carry these;
    # raw docs typically do. NOTE: no sample raw-doc frontmatter exists in
    # docs/samples/caldwell/raw/; the raw-side field shape is locked at v1.0
    # against issue #65's stated schema. Operator-contributed raw sample data
    # could surface refinements for v1.1.

    source_url: str | None = None  # provenance URL for ingested content
    mime_type: str | None = None  # original document MIME type (e.g. "application/pdf")
    ingested_at: datetime | None = (
        None  # ingest timestamp (datetime, not date; automated
    )
    # ingest typically includes a time component)

    # ── Catch-all for unknown frontmatter ─────────────────────────────────
    # Round-trip preserving for any operator-authored frontmatter key that
    # does not fit the named fields above. Callers must not rely on
    # ``extra_frontmatter`` for structural lifecycle semantics -- those belong
    # in named fields (see lifecycle section above and prep finding S3).

    extra_frontmatter: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# CorpusStats


@dataclass
class CorpusStats:
    """Per-corpus health and stats snapshot returned by ``stats(corpus)``.

    Used by ``doctor.check_corpus_backend`` to surface the page-count cliff
    WARN when ``supports_full_text_search=False`` AND ``page_count`` exceeds
    the filesystem-performance threshold (~1000 pages, tuned at PR 1 prep).
    Matches the doctor page-count probe precedent from ``LogBackend`` (PR 3
    /plan-eng-review 2026-05-29, performance finding P1).

    ``most_recent``: top-N ``CorpusRef`` entries by ``last_modified``,
    descending. N is implementation-defined (typically 5-10). Used by
    ``render_index_summary()`` to synthesize the INDEX-equivalent content
    for backends that don't have a physical ``INDEX.md`` file (e.g.
    ``SQLiteCorpusBackend``).
    """

    page_count: int
    total_bytes: int
    last_update: (
        datetime | None
    )  # None when corpus is empty or last_modified unavailable
    most_recent: list[CorpusRef]  # top-N by last_modified, descending

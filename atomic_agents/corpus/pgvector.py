"""PgvectorCorpusBackend — semantic-search corpus backend using pgvector.

Wraps ``FilesystemCorpusBackend`` to inherit page storage, versioning,
atomic writes, and name validation (MUST 1), while adding an injected
``EmbeddingBackend`` for ANN-based ``query()``.

**Architecture decision (RESOLVED by maintainer ruling).**
``PgvectorCorpusBackend`` subclasses ``FilesystemCorpusBackend`` — the only
existing corpus backend providing ``supports_versioning=True`` (required by
MUST) — and adds a pgvector-backed ``query()`` that reads a separate Postgres
database for the embedding index.  The corpus PAGES stay on the filesystem;
only the embedding index lives in Postgres.  The full-Postgres corpus
(a ``PostgresCorpusBackend`` page store, onto which this could later rebase
without a public-API change) is tracked as follow-up #540.

Embedding index storage
-----------------------
The ``corpus_page_embeddings`` table is stored in a Postgres database
configured via ``ATOMIC_AGENTS_PGVECTOR_URL`` (distinct from the memory
backend URL to allow separate deployments).

The corpus page files themselves remain on the filesystem (inherited from
``FilesystemCorpusBackend``).  The Postgres DB holds ONLY the embedding
index — regenerable derived state (the filesystem page is the source of
truth; the index is rebuilt on the next write or by a re-embed pass).

When no Postgres URL is configured, ``query()`` falls back to the parent's
substring + tag match (``supports_semantic_search=False``).

Usage:
    ATOMIC_AGENTS_CORPUS_BACKEND=pgvector-corpus
    ATOMIC_AGENTS_PGVECTOR_URL=postgresql://user:password@host:5432/dbname
    ATOMIC_AGENTS_EMBEDDING_BACKEND=openai
    OPENAI_API_KEY=sk-...

backend_id: ``"pgvector-corpus"``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .filesystem import FilesystemCorpusBackend
from .types import CorpusCapabilities, CorpusRef

if TYPE_CHECKING:
    from ..embedding.backend import EmbeddingBackend

_logger = logging.getLogger(__name__)

# ── DDL ───────────────────────────────────────────────────────────────────────
_CREATE_CORPUS_EMBEDDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS corpus_page_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    agent_scope TEXT NOT NULL,
    corpus      TEXT NOT NULL,
    page_name   TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    embedding   vector({dimensions}) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(agent_scope, corpus, page_name)
)
"""

# pgvector's HNSW index supports at most 2,000 dimensions on the ``vector``
# type (verified against pgvector's authoritative docs, Principle #12).
# ``text-embedding-3-large`` (3072 dims) is a supported model, so an
# unconditional ``CREATE INDEX ... USING hnsw`` would raise server-side and
# fail schema-init for a valid config.  Above the limit the index is skipped;
# cosine ANN still works via sequential scan with the ``<=>`` operator
# (correct results, slower recall).  See _maybe_create_hnsw_index.
_HNSW_MAX_DIMENSIONS = 2000

_CREATE_CORPUS_EMBEDDINGS_HNSW_INDEX = """
CREATE INDEX IF NOT EXISTS idx_corpus_page_embeddings_hnsw
    ON corpus_page_embeddings
    USING hnsw (embedding vector_cosine_ops)
"""

_CREATE_CORPUS_META_TABLE = """
CREATE TABLE IF NOT EXISTS corpus_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CHECK_VECTOR_EXTENSION_SQL = "SELECT 1 FROM pg_extension WHERE extname = 'vector'"

_ANN_CORPUS_SEARCH_SQL = """
SELECT page_name, corpus
FROM corpus_page_embeddings
WHERE agent_scope = %s
  AND corpus = %s
  AND model_id = %s
  AND dimensions = %s
ORDER BY embedding <=> %s::vector
LIMIT %s
"""

_UPSERT_CORPUS_EMBEDDING_SQL = """
INSERT INTO corpus_page_embeddings
    (agent_scope, corpus, page_name, model_id, dimensions, embedding, embedded_at)
VALUES (%s, %s, %s, %s, %s, %s::vector, NOW())
ON CONFLICT (agent_scope, corpus, page_name) DO UPDATE SET
    model_id    = EXCLUDED.model_id,
    dimensions  = EXCLUDED.dimensions,
    embedding   = EXCLUDED.embedding,
    embedded_at = EXCLUDED.embedded_at
"""


class PgvectorCorpusBackend(FilesystemCorpusBackend):
    """FilesystemCorpusBackend with pgvector ANN query().

    Inherits: page read/write, versioning, atomic writes, name validation.
    Adds: ANN ``query()`` against a Postgres embedding index when an embedding
    backend and Postgres URL are configured; graceful substring fallback
    when not.

    ``capabilities.supports_semantic_search`` returns ``True`` only when both
    an embedding backend AND a Postgres connection URL are configured.

    backend_id = ``"pgvector-corpus"``
    """

    backend_id: str = "pgvector-corpus"

    def __init__(
        self,
        agent_root: Path | str,
        *,
        embedding_backend: "EmbeddingBackend | None" = None,
        pgvector_url: str | None = None,
    ) -> None:
        """Construct the backend.

        Args:
            agent_root: Agent root directory (wiki/ and raw/ live here).
            embedding_backend: Pre-constructed EmbeddingBackend, or None to
                use ``get_default_embedding_backend()`` from env vars.
            pgvector_url: PostgreSQL connection URL for the embedding index.
                When None, reads from ``ATOMIC_AGENTS_PGVECTOR_URL`` env var.
                When still None after env lookup, ANN query is disabled and
                ``query()`` falls back to substring match.
        """
        import os

        super().__init__(agent_root)
        # agent_scope partitions rows in the SHARED corpus_page_embeddings table
        # (UNIQUE(agent_scope, corpus, page_name)).  It MUST be unique per agent
        # root, NOT the directory basename: two agents rooted at
        # /agents/team-a/researcher and /agents/team-b/researcher share the
        # basename "researcher" and would collide on UPSERT (one overwrites the
        # other's vector) and cross-read each other's rows in query() — silent
        # multi-tenant data mixing in exactly the fleet/shared-DB deployment this
        # table exists to support.  Use the resolved absolute path so distinct
        # agent roots get distinct scopes.  (agent_scope is TEXT, no length cap.)
        self._agent_scope = str(Path(agent_root).resolve())

        # Resolve embedding backend
        if embedding_backend is not None:
            self._embedding_backend: "EmbeddingBackend | None" = embedding_backend
        else:
            from ..embedding.registry import get_default_embedding_backend

            self._embedding_backend = get_default_embedding_backend()

        # Lazily-validated-once: the side-table column width matches the active
        # model's dimensions (cost-safety guard, checked before the first embed).
        self._embedding_dim_validated = False

        # Resolve Postgres URL
        if pgvector_url is None:
            pgvector_url = os.environ.get("ATOMIC_AGENTS_PGVECTOR_URL") or None
        self._pgvector_url = pgvector_url

        # Connection is lazy-initialized on first use.
        #
        # THREAD-SAFETY: this is a SINGLE shared connection, NOT thread-local
        # (unlike PgvectorMemoryBackend, which uses a per-thread connection
        # because psycopg connections are not safe to share across threads).
        # PgvectorCorpusBackend is therefore SINGLE-THREADED-ONLY: do not call
        # query()/write_page()/index_page() concurrently from multiple threads
        # on one instance.  Mirroring the memory backend's thread-local model is
        # a tracked follow-up (#540 scope); not done here because the corpus
        # path is not yet on a hot concurrent path.
        self._pg_conn: Any = None
        self._schema_initialized = False

    # ── Connection management ─────────────────────────────────────────────────

    def _get_pg_conn(self) -> Any:
        """Return a Postgres connection, lazy-creating on first call.

        Returns None (not raising) when no URL is configured — callers
        treat None as "pgvector not available, use substring fallback".
        """
        if self._pgvector_url is None:
            return None

        if self._pg_conn is not None:
            if getattr(self._pg_conn, "closed", 0) or getattr(
                self._pg_conn, "broken", False
            ):
                try:
                    self._pg_conn.close()
                except Exception:
                    pass
                self._pg_conn = None
            else:
                return self._pg_conn

        try:
            import psycopg  # noqa: PLC0415
        except ImportError:
            _logger.warning(
                "PgvectorCorpusBackend: psycopg not installed; "
                "falling back to substring query()"
            )
            return None

        # Phase 1 — establish the connection + register the type adapter.
        # ONLY genuine connection / adapter-import failures degrade to substring here.
        # The C2 missing-extension RuntimeError is raised in phase 2 (schema)
        # and MUST propagate — it is NOT a connection error and must not be
        # swallowed (matches the sibling PgvectorMemoryBackend, whose
        # _ensure_schema RuntimeError propagates through _get_conn).
        try:
            from urllib.parse import unquote, urlparse

            parsed = urlparse(self._pgvector_url)
            conn = psycopg.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 5432,
                dbname=(parsed.path or "/").lstrip("/") or "postgres",
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                autocommit=False,
                row_factory=psycopg.rows.dict_row,
            )
            # Register the pgvector psycopg type adapter on this connection.
            # register_vector installs the LOADER (read path) and the DUMPER for
            # ``pgvector.Vector`` / ``numpy.ndarray`` (write path) — it does NOT
            # register a dumper for plain ``list``.  So every ``%s::vector`` bind
            # in this module wraps the value in ``pgvector.Vector(...)``; the
            # registered dumper then emits the bracket form ``[...]`` the vector
            # type accepts.  A bare list would adapt to a curly-brace array literal
            # ``{...}`` that pgvector's vector_in parser rejects.  Per-connection.
            try:
                from pgvector.psycopg import register_vector  # noqa: PLC0415

                register_vector(conn)
            except ImportError:
                # [pgvector] extra absent — degrade to substring query() rather
                # than producing array-literal bind failures on every query.
                _logger.warning(
                    "PgvectorCorpusBackend: pgvector extra not installed; "
                    "falling back to substring query()"
                )
                try:
                    conn.close()
                except Exception:
                    pass
                self._pg_conn = None
                return None
        except Exception as exc:
            _logger.warning(
                "PgvectorCorpusBackend: Postgres connection failed (%s); "
                "falling back to substring query()",
                type(exc).__name__,
            )
            return None

        # Phase 2 — schema init.  A C2 missing-extension RuntimeError raised by
        # _ensure_pg_schema FAILS HARD (close the conn, then re-raise) — it is
        # NOT degraded to substring.  Only the connection itself going broken mid-DDL
        # (a real connection error) degrades.
        if not self._schema_initialized:
            try:
                self._ensure_pg_schema(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                self._pg_conn = None
                raise
            self._schema_initialized = True
        self._pg_conn = conn
        return conn

    def _ensure_pg_schema(self, conn: Any) -> None:
        """Create embedding tables and HNSW index if missing.

        FAIL-HARD on missing pgvector extension (ruling C2).
        """
        # C2 fail-hard: verify vector extension present
        cur = conn.execute(_CHECK_VECTOR_EXTENSION_SQL)
        if cur.fetchone() is None:
            raise RuntimeError(
                "PgvectorCorpusBackend: the 'vector' Postgres extension is not "
                "installed.  Run `CREATE EXTENSION IF NOT EXISTS vector;` as a "
                "superuser before connecting.  The backend never runs CREATE "
                "EXTENSION itself; provision the 'vector' extension during "
                "database setup."
            )

        dimensions = (
            self._embedding_backend.dimensions
            if self._embedding_backend is not None
            else 1536
        )
        conn.execute(_CREATE_CORPUS_META_TABLE)
        conn.execute(_CREATE_CORPUS_EMBEDDINGS_TABLE.format(dimensions=dimensions))
        self._maybe_create_hnsw_index(conn, dimensions)
        conn.commit()

    def _maybe_create_hnsw_index(self, conn: Any, dimensions: int) -> None:
        """Create the HNSW index only when the column width is within pgvector's limit.

        pgvector's HNSW index supports at most ``_HNSW_MAX_DIMENSIONS`` (2,000)
        dimensions.  ``text-embedding-3-large`` (3072 dims) exceeds that, and an
        unconditional ``CREATE INDEX ... USING hnsw`` would raise server-side
        ("column cannot have more than 2000 dimensions for hnsw index"), failing
        schema-init for a valid config.  Above the limit we SKIP the index and
        log a WARNING — cosine ANN still works via sequential scan with the
        ``<=>`` operator (correct results, slower recall).

        Guard on the EFFECTIVE (actual) column width, not the caller's declared
        ``dimensions``.  On a shared DB the ``corpus_page_embeddings`` table is
        created once via ``CREATE TABLE IF NOT EXISTS`` and keeps its original
        ``vector(N)``; a later backend with a SMALLER declared dimension would
        otherwise pass the ``dimensions <= limit`` check and run the index DDL
        against the existing OVER-limit column, crashing.  We read the real
        column width from the catalog (pgvector stores N directly in
        ``atttypmod``, no offset — verified) and guard on ``max(declared,
        actual)`` so the index is created only when BOTH are within the limit.
        """
        # Import the intentionally-shared public catalog helpers from the memory
        # pgvector module (public names — NOT the leading-underscore originals —
        # so this cross-package dependency is explicit and rename-safe).
        from ..memory.pgvector import (  # noqa: PLC0415
            UNKNOWN_DIMENSION,
            actual_embedding_dimension,
        )

        actual = actual_embedding_dimension(conn, "corpus_page_embeddings")
        # actual is None when the column does not exist yet (fresh DB) — guard on
        # the declared width alone.  Known width → max(declared, actual).  Unknown
        # width (dimensionless ``vector`` column, atttypmod -1) → UNKNOWN_DIMENSION
        # (> limit) so we skip rather than attempt unsafe HNSW DDL.
        effective = dimensions if actual is None else max(dimensions, actual)
        if effective > _HNSW_MAX_DIMENSIONS:
            _logger.warning(
                "PgvectorCorpusBackend: embedding column width=%s exceeds "
                "pgvector's HNSW index limit of %d (or is unknown/dimensionless); "
                "skipping the HNSW index on corpus_page_embeddings.  ANN cosine "
                "search still works via sequential scan (correct results, slower "
                "recall).  Use a fixed-dimension vector column with dimensions "
                "<= %d to enable HNSW indexing.",
                "unknown" if actual == UNKNOWN_DIMENSION else effective,
                _HNSW_MAX_DIMENSIONS,
                _HNSW_MAX_DIMENSIONS,
            )
            return
        conn.execute(_CREATE_CORPUS_EMBEDDINGS_HNSW_INDEX)

    # ── Capabilities ──────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> CorpusCapabilities:
        """Capability advertisement.

        ``supports_semantic_search=True`` when both an embedding backend AND
        a Postgres URL are configured; ``False`` otherwise (substring fallback).
        """
        has_semantic = (
            self._embedding_backend is not None and self._pgvector_url is not None
        )
        return CorpusCapabilities(
            supports_semantic_search=has_semantic,
            supports_full_text_search=False,
            supports_versioning=True,
            supports_streaming_iteration=False,
            embedding_provider=(
                self._embedding_backend.provider_id
                if self._embedding_backend is not None
                else None
            ),
            supports_canonical_export=True,
            embedding_backend_resolved=self._embedding_backend,
        )

    # ── write_page() override ───────────────────────────────────────────────

    def write_page(
        self,
        name: str,
        content: str,
        corpus: Literal["wiki", "raw"],
        policy,
        *,
        frontmatter: dict | None = None,
        expected_content_sha256: str | None = None,
    ) -> CorpusRef:
        """Write a page (canonical filesystem write) then index its embedding.

        Without this override the inherited ``FilesystemCorpusBackend.write_page``
        never populates ``corpus_page_embeddings``, so the ANN ``query()`` would
        always read an empty index and semantic corpus search would be
        non-functional on the happy path (capabilities advertises
        ``supports_semantic_search=True`` while always returning nothing — a
        capability-honesty violation).

        Two-phase, mirroring ``PgvectorMemoryBackend.write_note``:

        1. ``super().write_page(...)`` — canonical write via ``_io.atomic_write``
           (CAS / four-case behavior preserved; the filesystem page is the
           source of truth).
        2. ``index_page(corpus, name, content)`` — embed OUTSIDE the page write
           and upsert into the index.  Any failure here (including the C2
           missing-extension ``RuntimeError`` raised by ``_get_pg_conn`` on a
           misconfigured DB) is logged + swallowed HERE; the page is already
           durably on disk and remains reachable via the substring fallback.
           This keeps the write path symmetric with
           ``PgvectorMemoryBackend.write_note`` (whose ``_upsert_embedding``
           swallows the same C2 raise) — a successful page write must NOT report
           total failure because a derived-index side-effect failed.  C2
           fail-hard is preserved on the READ path (``query()``) where there is
           no partial state to leave behind.

        Note: on a content-identical idempotent write (parent Case 2, no file
        rewrite) ``index_page`` still re-embeds the unchanged body, making a
        redundant billable embed.  Accepted as index-self-heal insurance; the
        agent.call() batch-ingestion gate (#544 PR1) covers the write path when
        this is driven by a capture-commit.

        Cost gate: ungated at the backend layer (by design).  Same
        deferred-to-orchestrator posture as ``PgvectorMemoryBackend`` — see
        spec/46 §"Direct-caller gate boundary".  Not dogfooded against a live
        pgvector instance.
        """
        ref = super().write_page(
            name,
            content,
            corpus,
            policy,
            frontmatter=frontmatter,
            expected_content_sha256=expected_content_sha256,
        )
        # Index the POST-WRITE body. ``content`` is the canonical body just
        # written (FilesystemCorpusBackend does not merge page bodies, so the
        # incoming content IS the stored content — no merge-fragment hazard like
        # the memory backend's merge_into path).  Swallow indexing failures so a
        # durable page write is never masked by a derived-index side-effect
        # error (write-path symmetry with the memory backend; see the docstring).
        if self._embedding_backend is not None and self._pgvector_url is not None:
            try:
                self.index_page(corpus, name, content)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "PgvectorCorpusBackend: indexing failed after write_page for "
                    "%r/%r: %s (page committed to disk; ANN recall degraded, "
                    "substring fallback still works)",
                    corpus,
                    name,
                    type(exc).__name__,  # MUST 5 redaction — type name only
                )
        return ref

    def _assert_embedding_dim_matches(self) -> None:
        """Fail LOUD before any billable embed if the side-table column width
        does not match the active model's dimensions.

        Cross-family review #1 / ruling 2026-06-18 "fix now".  The
        ``corpus_page_embeddings.embedding`` column may have been created at a
        different width than the active model produces (the 1536 default with no
        backend, or a previously-pinned model, then a different-dimension model
        pinned).  Left unguarded, index_page()/query() would BILL an embed and
        then silently fail to store/query against the mismatched ``vector(N)``
        column (wasted spend, no error).  Checked at the EMBED SITE (not
        schema-init) so the legitimate construct-then-reprovision flow — a
        migration, or a test that drops and recreates the table at its own
        dimension — is never blocked.  Validated once per instance and cached.
        Both framework gate sites are now wired (#544 PR1 + PR2); standalone
        callers of index_page()/query() remain ungated by design.
        """
        if self._embedding_dim_validated or self._embedding_backend is None:
            return
        conn = self._get_pg_conn()
        if conn is None:
            return
        from ..memory.pgvector import (  # noqa: PLC0415
            UNKNOWN_DIMENSION,
            actual_embedding_dimension,
        )

        actual = actual_embedding_dimension(conn, "corpus_page_embeddings")
        expected = self._embedding_backend.dimensions
        if actual is not None and actual != UNKNOWN_DIMENSION and actual != expected:
            raise RuntimeError(
                f"PgvectorCorpusBackend: the corpus_page_embeddings.embedding "
                f"column is vector({actual}), but the active embedding model "
                f"({self._embedding_backend.model_id}) produces {expected}-"
                f"dimension vectors.  Indexing or querying would bill embeddings "
                f"that then fail to store or query against the mismatched column "
                f"(silent wasted spend).  To fix: drop the side table "
                f"(`DROP TABLE corpus_page_embeddings;`) so it is re-created at "
                f"{expected} dims on next start, or pin the embedding model whose "
                f"dimension matches the existing column.  (Automatic re-index on "
                f"a bulk re-index tool is a follow-up item from #544 PR2.)"
            )
        self._embedding_dim_validated = True

    # ── query() override ──────────────────────────────────────────────────────

    def query(
        self,
        text: str,
        corpus: Literal["wiki", "raw"],
        *,
        top_k: int = 10,
    ) -> list[CorpusRef]:
        """ANN cosine-distance query when embedding backend is configured.

        Signature MUST match the ``CorpusBackend`` Protocol
        (``query(text, corpus, *, top_k=10)``) — same as
        ``FilesystemCorpusBackend`` and ``SQLiteCorpusBackend``.  An earlier
        draft used ``(corpus, query_text, *, limit, offset)`` which broke the
        Protocol contract AND every ``super().query(...)`` fallback call (the
        parent has no ``limit``/``offset`` kwargs).

        Falls back to parent substring + tag match when:
        * No embedding backend configured.
        * No Postgres URL configured.
        * ``embed()`` returns None (provider unavailable).
        * ANN query raises.

        It does NOT fall back when the ANN query SUCCEEDS with zero (or
        partial) rows — a successful ANN result is authoritative
        (empty-is-authoritative, feedback_empty_resolved_list_authoritative);
        an empty ANN set returns ``[]`` rather than silently surfacing
        substring-only pages.

        ANN model filter:
        Only rows matching the active backend's ``model_id`` and ``dimensions``
        are returned; stale rows from a since-replaced model are EXCLUDED from
        the ANN join.  There is no per-query substring union, so a page whose
        only embedding is stale-model is NOT recovered here — it remains
        reachable via the substring code path (when no embedding backend/URL is
        configured) but not via this semantic ``query()`` until re-indexed under
        the active model (re-indexing happens on the page's next
        ``write_page``).
        """
        if self._embedding_backend is None or self._pgvector_url is None:
            return super().query(text, corpus, top_k=top_k)

        conn = self._get_pg_conn()
        if conn is None:
            return super().query(text, corpus, top_k=top_k)

        # Cost-safety: fail LOUD before billing the query embed on a column-width
        # mismatch (cross-family review #1).
        self._assert_embedding_dim_matches()

        # Embed the query text.  input_type="search_query": index_page() embeds
        # documents, so a query/document-aware provider (supports_input_type=
        # True) embeds this correctly as a QUERY.  OpenAI ignores it
        # (supports_input_type=False); forward-correct for a query-aware backend.
        vector = None
        try:
            vector = self._embedding_backend.embed(text, input_type="search_query")
        except Exception:  # noqa: BLE001
            pass

        if vector is None:
            return super().query(text, corpus, top_k=top_k)

        model_id = self._embedding_backend.model_id
        dimensions = self._embedding_backend.dimensions

        # Wrap in pgvector.Vector so the registered dumper emits the bracket form
        # ``[...]`` the vector type accepts (a bare list adapts to a curly-brace
        # literal ``{...}`` that vector_in rejects — see _get_pg_conn's note).
        # Constructed INSIDE the try so a malformed query vector (Vector() coerces
        # via numpy and may raise on non-numeric content) degrades to the
        # substring fallback below rather than crashing query().
        from pgvector import Vector  # noqa: PLC0415

        try:
            cur = conn.execute(
                _ANN_CORPUS_SEARCH_SQL,
                (
                    self._agent_scope,
                    corpus,
                    model_id,
                    dimensions,
                    Vector(vector),
                    top_k,
                ),
            )
            rows = cur.fetchall()
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            _logger.debug(
                "PgvectorCorpusBackend: ANN query failed (%s); "
                "falling back to substring query()",
                type(exc).__name__,
            )
            return super().query(text, corpus, top_k=top_k)

        # Resolve page names to CorpusRef objects via parent read_page()
        result: list[CorpusRef] = []
        for row in rows:
            ref = self._name_to_ref(row["page_name"], row["corpus"])
            if ref is not None:
                result.append(ref)
                if len(result) >= top_k:
                    break
        return result

    def _name_to_ref(self, page_name: str, corpus: str) -> CorpusRef | None:
        """Load a page and return its CorpusRef, or None if not found.

        ``read_page(name, corpus)`` — name first, corpus second (Protocol
        order; matches ``FilesystemCorpusBackend.read_page``).
        """
        try:
            page = self.read_page(page_name, corpus)  # type: ignore[arg-type]
            if page is None:
                return None
            return page.ref
        except Exception:
            return None

    def index_page(self, corpus: str, page_name: str, body: str) -> None:
        """Embed and index a corpus page for ANN retrieval.

        Called after a page write to update the embedding index.  When no
        embedding backend is configured, or no Postgres URL is configured, or
        the connection cannot be established, this is a no-op (the page still
        exists on the filesystem; only ANN recall is affected).

        NOT swallowed here: a C2 missing-extension ``RuntimeError`` raised by
        ``_get_pg_conn`` (the database is reachable but the ``vector`` extension
        is absent) PROPAGATES — it is an operator-actionable misconfiguration,
        not a transient outage.  The ``write_page`` caller wraps this method in
        its own try/except so a durable page write is never masked by the
        derived-index side-effect; a DIRECT caller of ``index_page`` sees the
        ``RuntimeError``.

        Two-phase: embed() OUTSIDE any transaction, then UPSERT.
        """
        if self._embedding_backend is None:
            return

        conn = self._get_pg_conn()
        if conn is None:
            return

        if not body or not body.strip():
            return

        # Cost-safety: fail LOUD before billing the embed on a column-width
        # mismatch (cross-family review #1).
        self._assert_embedding_dim_matches()

        # input_type="search_document": index path — body is a stored document.
        # Query/document-aware providers embed in document mode; OpenAI ignores
        # it.  Pairs with the "search_query" hint at the query() site.
        vector = None
        try:
            vector = self._embedding_backend.embed(body, input_type="search_document")
        except Exception:  # noqa: BLE001
            return

        if vector is None:
            return

        model_id = self._embedding_backend.model_id
        dimensions = self._embedding_backend.dimensions

        # Produced-length backstop (dimension-honesty, #200 PR2 CRITICAL class):
        # if embed() returns a vector whose length diverges from the declared
        # dimensions, skip the upsert rather than store a row whose dimensions
        # column lies about the vector it holds.
        if len(vector) != dimensions:
            _logger.warning(
                "PgvectorCorpusBackend: embed() for page %r/%r produced a "
                "vector of length %d but backend declares dimensions=%d; "
                "skipping index upsert (dimension-honesty backstop)",
                corpus,
                page_name,
                len(vector),
                dimensions,
            )
            return

        # Wrap in pgvector.Vector so the registered dumper emits the bracket form
        # the vector type accepts (a bare list would adapt to a rejected
        # curly-brace literal — see _get_pg_conn's note).  Constructed INSIDE the
        # try so a malformed vector (Vector() coerces via numpy and may raise on
        # non-numeric content) is caught by the rollback/log path below rather
        # than escaping a best-effort index update.
        from pgvector import Vector  # noqa: PLC0415

        try:
            conn.execute(
                _UPSERT_CORPUS_EMBEDDING_SQL,
                (
                    self._agent_scope,
                    corpus,
                    page_name,
                    model_id,
                    dimensions,
                    Vector(vector),
                ),
            )
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            _logger.warning(
                "PgvectorCorpusBackend: failed to index page %r/%r: %s",
                corpus,
                page_name,
                type(exc).__name__,
            )

    # ── close() override ──────────────────────────────────────────────────────

    def close(self) -> None:
        """Release the embedding backend and Postgres connection."""
        if self._embedding_backend is not None:
            try:
                self._embedding_backend.close()
            except Exception:
                pass
        if self._pg_conn is not None:
            try:
                self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None
        super().close()

"""PgvectorCorpusBackend — semantic-search corpus backend using pgvector.

Wraps ``FilesystemCorpusBackend`` to inherit page storage, versioning,
atomic writes, and name validation (MUST 1), while adding an injected
``EmbeddingBackend`` for ANN-based ``query()``.

**Architecture decision recorded as open Tier A fork.**
The parent class choice (``FilesystemCorpusBackend`` vs. a future
``PostgresCorpusBackend`` base) was identified in the PR 3 prep as requiring
maintainer confirmation (prep finding P2).  This implementation uses
``FilesystemCorpusBackend`` as the parent — the only existing corpus backend
providing ``supports_versioning=True`` (required by MUST) — with a
pgvector-backed ``query()`` that queries a separate Postgres database for the
embedding index.  If the maintainer rules for a ``PostgresCorpusBackend``
base class (a separate issue), ``PgvectorCorpusBackend`` can be rebased onto
it without changing the public API.

Embedding index storage
-----------------------
The ``corpus_page_embeddings`` table is stored in a Postgres database
configured via ``ATOMIC_AGENTS_PGVECTOR_URL`` (distinct from the memory
backend URL to allow separate deployments).

The corpus page files themselves remain on the filesystem (inherited from
``FilesystemCorpusBackend``).  The Postgres DB holds ONLY the embedding
index — a regenerable derived state (note row is source of truth; ruling
reembed-on-dimension-or-model-change Tier B).

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
from typing import TYPE_CHECKING, Any

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
    backend and Postgres URL are configured; graceful FTS/substring fallback
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
        self._agent_scope = Path(agent_root).name

        # Resolve embedding backend
        if embedding_backend is not None:
            self._embedding_backend: "EmbeddingBackend | None" = embedding_backend
        else:
            from ..embedding.registry import get_default_embedding_backend

            self._embedding_backend = get_default_embedding_backend()

        # Resolve Postgres URL
        if pgvector_url is None:
            pgvector_url = os.environ.get("ATOMIC_AGENTS_PGVECTOR_URL") or None
        self._pgvector_url = pgvector_url

        # Connection is lazy-initialized on first use
        self._pg_conn: Any = None
        self._schema_initialized = False

    # ── Connection management ─────────────────────────────────────────────────

    def _get_pg_conn(self) -> Any:
        """Return a Postgres connection, lazy-creating on first call.

        Returns None (not raising) when no URL is configured — callers
        treat None as "pgvector not available, use FTS fallback".
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
            self._pg_conn = conn
            if not self._schema_initialized:
                self._ensure_pg_schema(conn)
                self._schema_initialized = True
            return conn
        except Exception as exc:
            _logger.warning(
                "PgvectorCorpusBackend: Postgres connection failed (%s); "
                "falling back to substring query()",
                type(exc).__name__,
            )
            return None

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
                "EXTENSION itself (spec/46 decision ci-pgvector-image C2)."
            )

        dimensions = (
            self._embedding_backend.dimensions
            if self._embedding_backend is not None
            else 1536
        )
        conn.execute(_CREATE_CORPUS_META_TABLE)
        conn.execute(_CREATE_CORPUS_EMBEDDINGS_TABLE.format(dimensions=dimensions))
        conn.execute(_CREATE_CORPUS_EMBEDDINGS_HNSW_INDEX)
        conn.commit()

    # ── Capabilities ──────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> CorpusCapabilities:
        """Capability advertisement.

        ``supports_semantic_search=True`` when both an embedding backend AND
        a Postgres URL are configured; ``False`` otherwise (FTS fallback).
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

    # ── query() override ──────────────────────────────────────────────────────

    def query(
        self,
        corpus: str,
        query_text: str,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[CorpusRef]:
        """ANN cosine-distance query when embedding backend is configured.

        Falls back to parent substring + tag match when:
        * No embedding backend configured.
        * No Postgres URL configured.
        * ``embed()`` returns None (provider unavailable).
        * ANN query fails.

        ANN model filter (ruling reembed-on-dimension-or-model-change Tier B):
        Only rows matching the active backend's ``model_id`` and ``dimensions``
        are returned.  Stale rows from a different model fall back to FTS.
        """
        if self._embedding_backend is None or self._pgvector_url is None:
            return super().query(corpus, query_text, limit=limit, offset=offset)

        conn = self._get_pg_conn()
        if conn is None:
            return super().query(corpus, query_text, limit=limit, offset=offset)

        # Embed the query text
        vector = None
        try:
            vector = self._embedding_backend.embed(query_text)
        except Exception:  # noqa: BLE001
            pass

        if vector is None:
            return super().query(corpus, query_text, limit=limit, offset=offset)

        model_id = self._embedding_backend.model_id
        dimensions = self._embedding_backend.dimensions

        try:
            cur = conn.execute(
                _ANN_CORPUS_SEARCH_SQL,
                (
                    self._agent_scope,
                    corpus,
                    model_id,
                    dimensions,
                    vector,
                    limit + offset,
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
            return super().query(corpus, query_text, limit=limit, offset=offset)

        # Resolve page names to CorpusRef objects via parent read_page()
        result: list[CorpusRef] = []
        for row in rows[offset:]:
            ref = self._name_to_ref(row["corpus"], row["page_name"])
            if ref is not None:
                result.append(ref)
                if len(result) >= limit:
                    break
        return result

    def _name_to_ref(self, corpus: str, page_name: str) -> CorpusRef | None:
        """Load a page and return its CorpusRef, or None if not found."""
        try:
            page = self.read_page(corpus, page_name)
            if page is None:
                return None
            return page.ref
        except Exception:
            return None

    def index_page(self, corpus: str, page_name: str, body: str) -> None:
        """Embed and index a corpus page for ANN retrieval.

        Called after a page write to update the embedding index.  If the
        embedding backend or Postgres is unavailable, this is a no-op
        (the page still exists on the filesystem; only ANN recall is affected).

        Two-phase: embed() OUTSIDE any transaction, then UPSERT.
        """
        if self._embedding_backend is None:
            return

        conn = self._get_pg_conn()
        if conn is None:
            return

        if not body or not body.strip():
            return

        vector = None
        try:
            vector = self._embedding_backend.embed(body)
        except Exception:  # noqa: BLE001
            return

        if vector is None:
            return

        model_id = self._embedding_backend.model_id
        dimensions = self._embedding_backend.dimensions

        try:
            conn.execute(
                _UPSERT_CORPUS_EMBEDDING_SQL,
                (
                    self._agent_scope,
                    corpus,
                    page_name,
                    model_id,
                    dimensions,
                    vector,
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

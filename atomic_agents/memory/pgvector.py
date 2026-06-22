"""PgvectorMemoryBackend — semantic-search memory backend using pgvector.

Subclasses ``PostgresMemoryBackend`` to inherit FTS/versioning/staging/export
(DRY) and adds a ``memory_note_embeddings`` side table for ANN recall.

Install:
    pip install 'atomic-agents-stack[postgres,pgvector]'

Usage:
    ATOMIC_AGENTS_MEMORY_BACKEND=pgvector-memory
    ATOMIC_AGENTS_MEMORY_BACKEND_URL=postgresql://user:password@host:5432/dbname
    ATOMIC_AGENTS_EMBEDDING_BACKEND=openai
    ATOMIC_AGENTS_EMBEDDING_MODEL=text-embedding-3-small   # optional
    OPENAI_API_KEY=sk-...

Or inject the embedding backend directly:

    from atomic_agents.embedding import OpenAIEmbeddingBackend
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    backend = PgvectorMemoryBackend(
        agent_root,
        embedding_backend=OpenAIEmbeddingBackend(api_key="sk-..."),
    )

Schema (extends PostgresMemoryBackend v2 schema):

    v2→v3 migration adds:
    * Table ``memory_note_embeddings`` — separate side table keyed by note name.
      The canonical ``memory_notes`` row is NOT modified (CAS/content_hash
      invariant preserved per ruling Q1).  Columns:
        - id              BIGSERIAL PRIMARY KEY
        - note_name       TEXT NOT NULL REFERENCES memory_notes(name) ON DELETE CASCADE
        - model_id        TEXT NOT NULL (embedding model used)
        - dimensions      INTEGER NOT NULL (vector dimension)
        - embedding       vector(N) NOT NULL
        - embedded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
      UNIQUE(note_name) — one vector per note (latest embedding wins).
    * HNSW index on ``embedding`` column with ``vector_cosine_ops`` operator,
      created WITH the column (not deferred).

    ``_SCHEMA_VERSION = 3``  (INDEPENDENT of LogBackend's own v2 constant)

Decision rulings applied
------------------------
* Q1  own-backend: distinct backend_id ``'pgvector-memory'``.
* Q2  embedding-backend injection: constructor kwarg ``embedding_backend=None``
      + ``get_default_embedding_backend()`` factory.
* Q3  cost-gate: NOT YET WIRED.  The embed() calls in write_note()/search()
      are currently UNGATED billable LLM calls (no reservation, no release, no
      JSONL audit record) when called directly (not via an orchestration layer).
      The Q3 ruling is that the gate belongs at the orchestration layer (NOT
      inside EmbeddingBackend — MUST 4 MUST-NOT-RAISE makes a backend-internal
      refusing gate incoherent), so the backend stays cost-unaware.  The two
      framework-controlled gate sites (agent.call() batch-ingestion gate in #544
      PR1; CLI corpus-query gate in #544 PR2) are both wired.  Callers that
      invoke write_note() / search() outside of those paths are ungated by
      design — see spec/46 §"Direct-caller gate boundary".  ASSURANCE LABEL:
      this embed path has not been dogfooded against a live pgvector instance.
* C2  missing extension: FAIL-HARD with a clear error.  The backend NEVER runs
      ``CREATE EXTENSION``.

Connection / transaction ordering for embed() calls
----------------------------------------------------
embed() is called OUTSIDE any Postgres transaction:
1. INSERT/UPDATE the canonical ``memory_notes`` row (its own committed transaction).
2. Call embed() OUTSIDE any transaction (HTTP round-trip to OpenAI).
3. UPSERT into ``memory_note_embeddings`` (its own committed transaction).

This decouples the HTTP latency from the Postgres lock-hold time.
If step 3 fails, the note row exists (visible via FTS) but has no embedding
(invisible to ANN search) — acceptable because the side table is regenerable
derived state (the canonical note is the source of truth; the side table
is regenerated on the next write or by a re-embed pass).

ANN search filter
-----------------
``search()`` queries only embeddings where ``model_id`` and ``dimensions``
match the active backend.  Stale embeddings (different model/dimension) are
NOT re-embedded on read and are excluded from the ANN join — this prevents
silent cosine-distance computation across mismatched vectors.

IMPORTANT — no per-query FTS union: a note whose ONLY embedding is stale-model
is excluded from the ANN result and is NOT recovered by a parallel FTS pass.
``search()`` falls back to the parent FTS ``search()`` ONLY when the query
embed() fails (returns None) or the ANN query raises — NOT when the ANN query
succeeds with zero (or partial) rows.  A successful ANN result is authoritative
(empty-is-authoritative, the split-brain guard from
feedback_empty_resolved_list_authoritative): an empty ANN set returns ``[]``
rather than silently surfacing FTS-only notes.  Consequence: a note embedded
under a since-replaced model is reachable via the FTS-only code paths
(``recall_notes`` / parent ``search()`` invoked directly) but NOT via this
backend's semantic ``search()`` until it is re-embedded under the active model.
Re-embedding happens on the note's next ``write_note``; a bulk re-embed pass
after a model swap is tracked as operator tooling (follow-up issue filed at #544 PR2).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backend import NoteRef, WritePolicy
from .postgres import (
    PostgresMemoryBackend,
    _ADVISORY_LOCK_KEY,
)

if TYPE_CHECKING:
    from ..embedding.backend import EmbeddingBackend
    from ..types import Capture

_logger = logging.getLogger(__name__)

# ── Schema versioning ──────────────────────────────────────────────
# v3 bumps the inherited v2 schema to add the memory_note_embeddings side table.
# INDEPENDENT of LogBackend's own _SCHEMA_VERSION constant.
_SCHEMA_VERSION = 3

# Sentinel attribute stamped on a live psycopg connection once register_vector
# has run on it.  Travels with the connection object (not an id()-keyed cache),
# so it is reconnect-safe and id()-reuse-safe — see _get_conn for the rationale.
_VECTOR_REGISTERED_ATTR = "_atomic_agents_vector_registered"

# ── DDL — memory_note_embeddings side table ────────────────────────
# The vector column dimension N is fixed at construction time from the active
# embedding backend's ``dimensions`` property.  The DDL template uses a Python
# format string that is filled before execution; N is an integer coming from
# the backend, not operator input, so there is no SQL injection risk.
_CREATE_EMBEDDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS memory_note_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    note_name   TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    embedding   vector({dimensions}) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(note_name)
)
"""

# pgvector's HNSW (and IVFFlat) indexes support a maximum of 2,000 dimensions
# on the ``vector`` type (verified against pgvector's authoritative docs,
# Principle #12 — "column cannot have more than 2000 dimensions for hnsw
# index").  This backend ships ``text-embedding-3-large`` (3072 dims) as a
# supported model, so an operator who selects it would otherwise crash
# schema-init when ``CREATE INDEX ... USING hnsw`` is run on a vector(3072)
# column.  Above this limit we SKIP the HNSW index: cosine ANN still works via
# sequential scan with the ``<=>`` operator (correct, just slower recall), and
# schema-init succeeds instead of failing the whole backend for a valid config.
_HNSW_MAX_DIMENSIONS = 2000

# Sentinel returned by _actual_embedding_dimension when an embedding column
# EXISTS but its width is unknown (a dimensionless ``vector`` column reports
# atttypmod -1).  It is deliberately > _HNSW_MAX_DIMENSIONS so the HNSW guard
# treats "unknown width" as "do not attempt the index" (fail toward not
# crashing) — an unqualified ``vector`` column cannot be HNSW-indexed anyway.
_UNKNOWN_DIMENSION = _HNSW_MAX_DIMENSIONS + 1


def _actual_embedding_dimension(conn: Any, table_name: str) -> int | None:
    """Return the actual vector(N) width of ``<table_name>.embedding``.

    pgvector stores the declared dimension directly in
    ``pg_attribute.atttypmod`` (no varchar-style ``+4`` offset — verified).

    Returns:
        * ``None`` — the TABLE genuinely does not exist yet (fresh DB:
          ``to_regclass`` resolved to NULL).  Callers then guard on the DECLARED
          width alone, which is correct on a fresh DB.
        * a positive ``int`` — the known fixed width of the existing column.
        * ``_UNKNOWN_DIMENSION`` — any state where we cannot PROVE the width is
          safe: the table exists but has no ``embedding`` column; the column is
          a dimensionless ``vector`` (atttypmod -1, un-indexable); or the
          catalog read itself failed.  All force the caller to SKIP the HNSW
          index — fail toward not attempting unsafe DDL rather than trusting the
          declared width against an unknown/mismatched column.

    Resolves the table via ``to_regclass`` so the lookup is search_path-aware
    (the DDL uses unqualified table names, so the column may live in a non-
    ``public`` schema); a hardcoded ``nspname = 'public'`` filter would miss it
    and wrongly fall through to the declared-width guard.  The relid resolution
    (table existence) is read SEPARATELY from the attribute lookup (column
    width) so "table absent" (→ None, fresh) is distinguished from "table
    present but embedding column absent/odd" (→ sentinel, skip).
    """
    try:
        # Phase 1: does the table exist (search_path-aware)?
        cur = conn.execute("SELECT to_regclass(%s) AS relid", (table_name,))
        rel_row = cur.fetchone()
        relid = (
            (rel_row["relid"] if isinstance(rel_row, dict) else rel_row[0])
            if rel_row is not None
            else None
        )
        if relid is None:
            # Table genuinely absent — fresh DB; guard on declared width.
            return None
        # Phase 2: read the embedding column's declared width, but ONLY if the
        # column is actually the pgvector ``vector`` type.  Joining on
        # ``atttypid = 'vector'::regtype`` makes this one canonical invariant —
        # "the embedding column IS a pgvector vector of width N" — instead of
        # trusting atttypmod from whatever type happens to occupy the column.  A
        # conflicting non-pgvector column (e.g. ``embedding varchar(100)``,
        # positive typmod) therefore matches NO row → sentinel → skip, rather
        # than feeding a foreign typmod into the width guard.  ``::regtype``
        # resolves 'vector' search_path-aware (same path the DDL uses).
        cur = conn.execute(
            "SELECT a.atttypmod AS n "
            "FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(%s) "
            "  AND a.attname = 'embedding' "
            "  AND a.atttypid = 'vector'::regtype "
            "  AND NOT a.attisdropped",
            (table_name,),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001
        # Catalog read failed — we cannot prove the column width is within the
        # HNSW limit, so report UNKNOWN (forces skip) rather than None (which
        # would fall through to the declared-width guard and risk unsafe DDL).
        # ('vector'::regtype itself raises if the extension is absent, but the
        # C2 fail-hard check runs earlier so by here the type exists.)
        return _UNKNOWN_DIMENSION
    # Table exists but has no pgvector ``embedding vector`` column (operator-
    # created conflicting table, wrong column type, or partial state):
    # un-indexable; force skip.
    if row is None:
        return _UNKNOWN_DIMENSION
    n = row["n"] if isinstance(row, dict) else row[0]
    if n is None:
        return _UNKNOWN_DIMENSION
    n = int(n)
    if n > 0:
        return n
    # atttypmod -1 (or any non-positive) → dimensionless/unknown vector column:
    # present but un-indexable.  Force the caller to skip.
    return _UNKNOWN_DIMENSION


# ── Intentionally-shared pgvector catalog helpers (public aliases) ──────────
# PgvectorCorpusBackend's HNSW guard needs the SAME catalog-width probe and the
# SAME "unknown width" sentinel as this module.  Rather than have the corpus
# backend import the leading-underscore names (a hidden cross-package coupling
# on a private symbol — a rename here would only surface at runtime in the rare
# shared-DB HNSW path), these public aliases declare the surface as an
# intentional shared contract.  The corpus backend imports THESE names; the
# underscore originals stay for in-module use.  Keep both in sync.
actual_embedding_dimension = _actual_embedding_dimension
UNKNOWN_DIMENSION = _UNKNOWN_DIMENSION


# HNSW index — created WITH the column (not deferred).  ``IF NOT EXISTS``
# makes re-runs idempotent.  ``vector_cosine_ops`` matches the ``<=>`` distance
# operator used in search queries.  ONLY created when dimensions <=
# _HNSW_MAX_DIMENSIONS (see _maybe_create_hnsw_index).
_CREATE_EMBEDDINGS_HNSW_INDEX = """
CREATE INDEX IF NOT EXISTS idx_memory_note_embeddings_hnsw
    ON memory_note_embeddings
    USING hnsw (embedding vector_cosine_ops)
"""

# Foreign-key reference (added as a separate ALTER after table creation so the
# parent table is guaranteed to exist — both tables are created under the same
# advisory lock, but CREATE TABLE IF NOT EXISTS on the parent may be a no-op
# if it already exists, which is fine; the FK is added via ALTER IF NOT EXISTS).
# Using ALTER TABLE ADD CONSTRAINT IF NOT EXISTS for idempotency.
_ADD_EMBEDDINGS_FK = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'memory_note_embeddings'
          AND constraint_name = 'fk_memory_note_embeddings_note_name'
    ) THEN
        ALTER TABLE memory_note_embeddings
            ADD CONSTRAINT fk_memory_note_embeddings_note_name
            FOREIGN KEY (note_name)
            REFERENCES memory_notes(name)
            ON DELETE CASCADE;
    END IF;
END
$$
"""

# Upsert: INSERT a new embedding or UPDATE if one already exists for this note.
_UPSERT_EMBEDDING_SQL = """
INSERT INTO memory_note_embeddings (note_name, model_id, dimensions, embedding, embedded_at)
VALUES (%s, %s, %s, %s::vector, NOW())
ON CONFLICT (note_name) DO UPDATE SET
    model_id    = EXCLUDED.model_id,
    dimensions  = EXCLUDED.dimensions,
    embedding   = EXCLUDED.embedding,
    embedded_at = EXCLUDED.embedded_at
"""

# ANN search: cosine distance, filtered to matching model_id + dimensions.
# Parameters (in placeholder order): model_id, dimensions, query_vector, limit.
_ANN_SEARCH_SQL = """
SELECT
    n.name, n.type, n.description,
    n.captured, n.last_seen, n.pinned,
    n.confidence, n.archived, n.superseded_by
FROM memory_notes n
JOIN memory_note_embeddings e ON e.note_name = n.name
WHERE e.model_id = %s
  AND e.dimensions = %s
ORDER BY e.embedding <=> %s::vector
LIMIT %s
"""

# Check whether the vector extension is present.
_CHECK_VECTOR_EXTENSION_SQL = "SELECT 1 FROM pg_extension WHERE extname = 'vector'"


class PgvectorMemoryBackend(PostgresMemoryBackend):
    """PostgresMemoryBackend subclass with pgvector semantic recall.

    Inherits FTS/versioning/staging/canonical-export from
    ``PostgresMemoryBackend`` and adds:

    * v2→v3 schema migration (``memory_note_embeddings`` side table + HNSW index)
    * ``write_note()`` override: embeds and upserts into the side table after
      the canonical write, OUTSIDE the note transaction.
    * ``search()`` override: ANN cosine-distance query when an embedding
      backend is configured; FTS fallback (parent ``search()``) when not.
    * ``capabilities()`` method returning ``MemoryCapabilities`` with the typed
      ``embedding_backend_resolved`` field.
    * ``supports_semantic_search`` property: True when embedding_backend set.
    * ``close()`` override: calls ``embedding_backend.close()`` in addition to
      the inherited Postgres connection teardown.

    Operator / programmatic injection
    ----------------------------------
    ``embedding_backend=None`` (default) → ``get_default_embedding_backend()``
    factory reads env vars and constructs.  Returns ``None`` on graceful
    degradation (no key → FTS fallback, no error).

    ``SecretBackendNotRegistered`` from a misconfigured SecretBackend is NOT
    swallowed; it propagates from ``__init__`` so the operator sees the error
    immediately (same posture as ``_llm._get_key``).

    backend_id / implementation_id
    --------------------------------
    ``implementation_id = "pgvector-memory"``  (distinct from parent's
    ``"postgres"``).
    """

    @property
    def implementation_id(self) -> str:
        return "pgvector-memory"

    def __init__(
        self,
        agent_root: Path,
        *,
        lock_backend=None,
        url: str | None = None,
        embedding_backend: "EmbeddingBackend | None" = None,
    ) -> None:
        """Construct a PgvectorMemoryBackend.

        Inherits MUST-1 uniform signature: ``(agent_root, *, lock_backend=None)``.
        Additional kwargs (``url``, ``embedding_backend``) do NOT break MUST-1
        because they have defaults (same pattern as ``PostgresMemoryBackend.url``).

        Args:
            agent_root: Path to the agent's root directory.
            lock_backend: Optional LockBackend (passed to parent).
            url: Postgres connection URL.  Reads from
                ``ATOMIC_AGENTS_MEMORY_BACKEND_URL`` when None.
            embedding_backend: An already-constructed ``EmbeddingBackend`` instance,
                or ``None`` (the default) to use ``get_default_embedding_backend()``
                which reads env vars and delegates key resolution to SecretBackend.
                Pass ``embedding_backend=None`` explicitly to trigger the factory;
                pass a constructed instance to bypass it entirely (for tests /
                programmatic injection).

        Raises:
            SecretBackendNotRegistered: if an operator-pinned SecretBackend is
                misconfigured and cannot resolve the embedding key.  NOT swallowed.
            ImportError: if the ``[pgvector]`` Python client extra is not installed.
        """
        # Initialize connection state before calling super().__init__ so that
        # GC-safe teardown works even if super().__init__ raises mid-way.
        self._embedding_backend: "EmbeddingBackend | None" = None
        self._pgvector_schema_ready = False
        # Lazily-validated-once: the side-table column width matches the active
        # model's dimensions (cost-safety guard, checked before the first embed).
        self._embedding_dim_validated = False

        # Parent constructor validates the URL and sets up connection
        # attributes.  The connection (and _ensure_schema, which reads
        # self._embedding_backend.dimensions to size the vector(N) column) is
        # LAZY — it runs on the FIRST DB operation, which is always AFTER this
        # __init__ completes and the embedding backend is resolved below.  So
        # dimension resolution never races construction: the v2→v3 migration
        # reads an already-set _embedding_backend, not the 1536 fallback.
        super().__init__(agent_root, lock_backend=lock_backend, url=url)

        # Resolve embedding backend AFTER parent __init__ so that a
        # SecretBackendNotRegistered from the factory propagates cleanly.
        if embedding_backend is not None:
            self._embedding_backend = embedding_backend
        else:
            from ..embedding.registry import get_default_embedding_backend

            self._embedding_backend = get_default_embedding_backend()

        if self._embedding_backend is not None:
            _logger.debug(
                "PgvectorMemoryBackend: using embedding backend provider_id=%r "
                "model_id=%r dimensions=%d",
                self._embedding_backend.provider_id,
                self._embedding_backend.model_id,
                self._embedding_backend.dimensions,
            )
        else:
            _logger.debug(
                "PgvectorMemoryBackend: no embedding backend configured; "
                "search() will use FTS fallback (supports_semantic_search=False)"
            )

    # ── Connection adapter registration ─────────────────────────────

    def _get_conn(self) -> Any:
        """Return the thread-local connection with the pgvector adapter registered.

        The parent ``PostgresMemoryBackend._get_conn`` creates and caches the
        connection (running ``_ensure_schema`` on first use).  It does NOT
        register pgvector's psycopg type adapter.

        Why register_vector is needed
        -----------------------------
        ``register_vector`` installs the psycopg LOADER for the ``vector`` type
        (read path) AND the DUMPER for the ``pgvector.Vector`` / ``numpy.ndarray``
        types (write path).  On READ, without it a ``vector`` column comes back as
        a Python ``str`` (e.g. ``'[0.1,0.2,...]'``) instead of a numpy array,
        breaking callers that materialize the stored vector
        (``list(row['embedding'])`` in the merge-embed regression).

        Write path — DO NOT bind a bare ``list``.  ``register_vector`` registers
        dumpers ONLY for ``pgvector.Vector`` and ``numpy.ndarray`` (verified in
        ``pgvector/psycopg/vector.py: register_vector_info``), NOT for ``list``.
        A plain ``list[float]`` therefore falls through to psycopg's default array
        dumper, which emits a curly-brace literal ``{0.1,0.2,...}`` (verified in
        ``psycopg/types/array.py``).  pgvector's ``vector_in`` parser rejects the
        curly form — it requires the bracket form ``[0.1,0.2,...]`` that
        ``Vector._to_db`` emits.  So a bare-list bind raises server-side even with
        register_vector installed.  Every ``%s::vector`` bind site in this module
        wraps the value in ``pgvector.Vector(...)`` so the registered dumper fires
        and emits the bracket form.  ``register_vector`` is load-bearing for BOTH
        reads (loader) and writes (the Vector dumper).

        Once-per-connection registration
        ---------------------------------
        ``register_vector`` issues catalog ``pg_type`` queries (one per vector
        type it fetches), so calling it on every ``_get_conn()`` would add
        round-trips to every memory read/write — the framework's hottest path.
        We register exactly once per live connection by stamping a sentinel
        attribute ON the connection object itself.  This is reconnect-safe and
        id()-reuse-safe: a replacement connection after a reconnect is a fresh
        object that does not carry the sentinel, so it re-registers; CPython
        recycling a freed ``id()`` cannot cause a false cache hit because the
        marker travels with the object, not with its address.  (Same
        once-per-connection posture as ``PgvectorCorpusBackend``.)
        """
        conn = super()._get_conn()
        if conn is not None and not getattr(conn, _VECTOR_REGISTERED_ATTR, False):
            try:
                from pgvector.psycopg import register_vector  # noqa: PLC0415

                register_vector(conn)
            except ImportError as exc:
                # [pgvector] extra not installed — surface loudly rather than
                # silently producing array-literal bind failures downstream.
                raise ImportError(
                    "PgvectorMemoryBackend requires the 'pgvector' extra. "
                    "Install via: pip install 'atomic-agents-stack[postgres,pgvector]'"
                ) from exc
            try:
                setattr(conn, _VECTOR_REGISTERED_ATTR, True)
            except (AttributeError, TypeError):
                # A connection object that forbids attribute assignment would
                # mean re-registering every call — correct, just not optimized.
                # Real psycopg connections accept the attribute.
                pass
        return conn

    # ── HNSW index guard ────────────────────────────────────────────

    def _maybe_create_hnsw_index(self, conn: Any, dimensions: int) -> None:
        """Create the HNSW index only when the column width is within pgvector's limit.

        pgvector's HNSW index supports at most ``_HNSW_MAX_DIMENSIONS`` (2,000)
        dimensions on the ``vector`` type.  A supported model such as
        ``text-embedding-3-large`` (3072 dims) exceeds that, and an
        unconditional ``CREATE INDEX ... USING hnsw`` would raise server-side
        ("column cannot have more than 2000 dimensions for hnsw index"), failing
        schema-init for a valid configuration.  Above the limit we SKIP the
        index and log a clear WARNING — cosine ANN still works via sequential
        scan with the ``<=>`` operator (correct results, slower recall).

        Guard on the EFFECTIVE (actual) column width, not just the caller's
        declared ``dimensions``.  ``memory_note_embeddings`` is created once via
        ``CREATE TABLE IF NOT EXISTS`` and keeps its original ``vector(N)``; a
        later backend with a SMALLER declared dimension would otherwise pass the
        ``dimensions <= limit`` check and run the index DDL against the existing
        OVER-limit column, crashing.  Read the real width from the catalog
        (search_path-aware via ``to_regclass``; pgvector stores N directly in
        ``atttypmod``, no offset) and guard on ``max(declared, actual)``.  A
        dimensionless ``vector`` column (atttypmod -1, un-indexable) also forces
        a skip via the ``_UNKNOWN_DIMENSION`` sentinel.
        """
        actual = _actual_embedding_dimension(conn, "memory_note_embeddings")
        # actual is None when the column does not yet exist (fresh DB) — guard on
        # the declared width alone.  When the column exists with a KNOWN width we
        # guard on max(declared, actual); when it exists with an UNKNOWN width
        # (dimensionless ``vector`` column → atttypmod -1) actual is sentinel
        # _UNKNOWN_DIMENSION (> limit) so we skip rather than attempt unsafe DDL.
        effective = dimensions if actual is None else max(dimensions, actual)
        if effective > _HNSW_MAX_DIMENSIONS:
            _logger.warning(
                "PgvectorMemoryBackend: embedding column width=%s exceeds "
                "pgvector's HNSW index limit of %d (or is unknown/dimensionless); "
                "skipping the HNSW index on memory_note_embeddings.  ANN cosine "
                "search still works via sequential scan (correct results, slower "
                "recall on large corpora).  Use a fixed-dimension vector column "
                "with dimensions <= %d to enable HNSW indexing.",
                "unknown" if actual == _UNKNOWN_DIMENSION else effective,
                _HNSW_MAX_DIMENSIONS,
                _HNSW_MAX_DIMENSIONS,
            )
            return
        conn.execute(_CREATE_EMBEDDINGS_HNSW_INDEX)

    # ── Schema versioning override ──────────────────────────────────

    def _ensure_schema(self, conn: Any) -> None:
        """Extend the inherited v1→v2 migration with a v2→v3 migration.

        Calls super()._ensure_schema(conn) first to handle the v1→v2 base
        migration.  IMPORTANT: super() COMMITS at the end of its run, which
        RELEASES the transaction-scoped ``pg_advisory_xact_lock`` it held.  The
        v2→v3 ladder below therefore runs in a SECOND transaction with the
        advisory lock RE-ACQUIRED (same ``_ADVISORY_LOCK_KEY`` so memory DDL
        phases still serialize against each other, just across two
        transactions, not one).

        Cross-phase atomicity is NOT provided by a single lock span — it is
        provided by the idempotent ``IF NOT EXISTS`` / ``DO $$`` DDL.  If a
        crash lands between the parent's commit (DB at v2, no embeddings table)
        and the subclass's commit, the next connection's C5 stale-meta guard
        (below, the ``existing >= 3`` arm) detects the missing side table and
        re-runs the v2→v3 arm.

        v2→v3 migration (additive, no ALTER on memory_notes):
        * CREATE TABLE IF NOT EXISTS memory_note_embeddings (...)
        * ADD FOREIGN KEY constraint (idempotent DO $$ ... $$)
        * CREATE INDEX IF NOT EXISTS ... USING hnsw (embedding vector_cosine_ops)
          — ONLY when dimensions <= 2000 (pgvector's HNSW limit); above that the
          index is skipped and ANN falls back to sequential scan (see
          _maybe_create_hnsw_index)
        * UPDATE memory_meta SET schema_version = '3'

        FAIL-HARD on missing extension (ruling C2): the extension check below
        raises a clear ``RuntimeError`` when pgvector's ``vector`` type is
        unavailable, BEFORE any vector(N) DDL runs.  The error is NOT caught
        here — it propagates to the caller.  The operator must run
        ``CREATE EXTENSION vector`` in their managed Postgres instance.  The
        backend NEVER runs CREATE EXTENSION itself.
        """
        # First: verify pgvector extension is present (C2 fail-hard posture).
        # This runs BEFORE calling super() so the error is diagnosed early.
        cur = conn.execute(_CHECK_VECTOR_EXTENSION_SQL)
        if cur.fetchone() is None:
            raise RuntimeError(
                "PgvectorMemoryBackend: the 'vector' Postgres extension is not "
                "installed in this database.  Run `CREATE EXTENSION IF NOT EXISTS "
                "vector;` as a superuser in the target database (typically done "
                "during database provisioning, NOT by the backend).  For managed "
                "Postgres (Cloud SQL, RDS, Azure Database), enable the extension "
                "via your cloud console or migration tool before connecting.  "
                "Run `atomic-agents doctor` to verify the embedding backend "
                "configuration.  The backend never runs CREATE EXTENSION itself; "
                "provision the 'vector' extension during database setup."
            )

        # Run parent v1→v2 migration (advisory lock acquired inside super()).
        # super() commits at the end — we then re-acquire the lock for v2→v3.
        super()._ensure_schema(conn)

        # v2→v3 migration under the same advisory lock (re-acquired here).
        try:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))

            # Read current version after parent migration
            cur = conn.execute(
                "SELECT value FROM memory_meta WHERE key = 'schema_version'"
            )
            row = cur.fetchone()
            existing = int(row["value"]) if row else 2

            if existing < 3:
                # Determine embedding dimensions for the vector column.
                # Use active backend's dimensions, or default to 1536 if no
                # backend configured yet (will be populated when backend attaches).
                dimensions = (
                    self._embedding_backend.dimensions
                    if self._embedding_backend is not None
                    else 1536
                )

                # Create the side table (additive; no ALTER on memory_notes).
                conn.execute(_CREATE_EMBEDDINGS_TABLE.format(dimensions=dimensions))

                # Add FK constraint (idempotent DO block).
                conn.execute(_ADD_EMBEDDINGS_FK)

                # HNSW index — created WITH the column (ruling rejects deferred),
                # guarded on pgvector's 2000-dim HNSW limit.
                self._maybe_create_hnsw_index(conn, dimensions)

                # Bump schema version.
                conn.execute(
                    "UPDATE memory_meta SET value = %s WHERE key = %s",
                    ("3", "schema_version"),
                )

            # C5 guard extension: if meta claims v3 but side table is missing,
            # the DB is stale-meta (e.g. a failed prior migration).
            if existing >= 3:
                tbl_cur = conn.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'memory_note_embeddings'"
                )
                if tbl_cur.fetchone() is None:
                    existing = 2
                    conn.execute(
                        "UPDATE memory_meta SET value = %s WHERE key = %s",
                        ("2", "schema_version"),
                    )
                    # Re-run the v2→v3 arm
                    dimensions = (
                        self._embedding_backend.dimensions
                        if self._embedding_backend is not None
                        else 1536
                    )
                    conn.execute(_CREATE_EMBEDDINGS_TABLE.format(dimensions=dimensions))
                    conn.execute(_ADD_EMBEDDINGS_FK)
                    self._maybe_create_hnsw_index(conn, dimensions)
                    conn.execute(
                        "UPDATE memory_meta SET value = %s WHERE key = %s",
                        ("3", "schema_version"),
                    )

            final_version = int(
                conn.execute(
                    "SELECT value FROM memory_meta WHERE key = 'schema_version'"
                ).fetchone()["value"]
            )
            if final_version != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"PgvectorMemoryBackend schema version mismatch: db has "
                    f"v{final_version}, code expects v{_SCHEMA_VERSION}. "
                    f"Open an issue at "
                    f"https://github.com/dep0we/atomic-agents-stack/issues"
                )

            conn.commit()
            self._pgvector_schema_ready = True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    # ── Capability advertisement ────────────────────────────────────

    @property
    def supports_semantic_search(self) -> bool:
        """True when an embedding backend is configured; False for FTS fallback.

        Backward-compatible property alias for ``capabilities().embedding_provider
        is not None``.
        """
        return self._embedding_backend is not None

    def capabilities(self):  # type: ignore[override]
        """Return ``MemoryCapabilities`` with typed embedding backend reference.

        spec/20 PR-3 addendum: ``PgvectorMemoryBackend`` returns a
        ``MemoryCapabilities`` dataclass so doctor and audit tooling have a
        single inspection surface.  The ``supports_semantic_search`` property
        remains for callers using the boolean shorthand.
        """
        from .backend import MemoryCapabilities

        provider = (
            self._embedding_backend.provider_id
            if self._embedding_backend is not None
            else None
        )
        return MemoryCapabilities(
            embedding_provider=provider,
            embedding_backend_resolved=self._embedding_backend,
        )

    # ── write_note() override ───────────────────────────────────────

    def write_note(
        self,
        capture: "Capture",
        policy: WritePolicy,
        expected_content_sha256: str | None = None,
    ) -> NoteRef:
        """Write a capture; also upsert its embedding into the side table.

        Transaction ordering (prevents embedding HTTP latency from holding
        the Postgres write lock):

        1. Call ``super().write_note(...)`` — canonical ``memory_notes`` row
           written and committed in its own transaction (CAS/content_hash
           invariant preserved; canonical row is UNTOUCHED by this override).
        2. If an embedding backend is configured, call ``embed()`` OUTSIDE
           any transaction (HTTP round-trip).
        3. If embed() succeeded, UPSERT the result into ``memory_note_embeddings``
           in its own short transaction.

        If step 2 or 3 fails, the note still exists (visible via FTS).  The
        side table is regenerable derived state — the note is the source of
        truth.

        Cost gate
        ---------
        Ungated at the backend layer (by design).  Per the Q3 ruling the gate
        belongs at the orchestration layer (the backend stays cost-unaware so
        MUST-4 MUST-NOT-RAISE holds).  The agent.call() batch-ingestion gate
        (#544 PR1) covers this path when write_note() is driven by a capture-
        commit.  Standalone use of PgvectorMemoryBackend (outside agent.call())
        is ungated by design — see spec/46 §"Direct-caller gate boundary".
        """
        # Step 1: canonical write (parent handles CAS, merge, four cases).
        ref = super().write_note(
            capture, policy, expected_content_sha256=expected_content_sha256
        )

        # Step 2+3: embed and upsert OUTSIDE parent's transaction.
        if self._embedding_backend is not None:
            # The embedding MUST represent the note's POST-WRITE STORED body, not
            # the incoming fragment.  On a merge write (Case 1, capture.merge_into
            # set) the parent PRESERVES the target's body and only appends
            # sources — so capture.body is just the latest fragment, NOT the
            # note's accumulated content.  Upserting embed(capture.body) would
            # overwrite the target's vector with the embedding of a single
            # fragment, silently diverging the stored body from its ANN vector
            # and corrupting semantic recall for every merged note.  Re-read the
            # canonical stored body and embed THAT.
            embed_body = capture.body
            if capture.merge_into:
                try:
                    stored = self.read_note(ref.name)
                except Exception:  # noqa: BLE001
                    stored = None
                if stored is not None and getattr(stored, "body", None) is not None:
                    embed_body = stored.body
                else:
                    # Re-read failed on a MERGE write: capture.body is only the
                    # latest fragment, NOT the note's accumulated body.  Embedding
                    # the fragment would overwrite the target's vector with a
                    # single-fragment embedding — the exact corruption this branch
                    # exists to prevent, just triggered by a transient read.
                    # Skip the upsert entirely: the prior (correct) vector stays in
                    # place and the note remains reachable via FTS.  Strictly better
                    # than storing a fragment vector.
                    _logger.warning(
                        "PgvectorMemoryBackend: merge re-read failed for "
                        "note_name=%r; skipping embedding upsert to avoid storing "
                        "a fragment vector (prior vector preserved, note reachable "
                        "via FTS)",
                        ref.name,
                    )
                    return ref
            self._upsert_embedding(ref.name, embed_body)

        return ref

    def _assert_embedding_dim_matches(self) -> None:
        """Fail LOUD before any billable embed if the side-table column width
        does not match the active model's dimensions.

        Cross-family review #1 / ruling 2026-06-18 "fix now".  The side table may
        have been created at a different width than the active model produces
        (created during FTS-only use at the 1536 default, or under a
        previously-pinned model, then a different-dimension model pinned).  Left
        unguarded, write/search would BILL an embed and then silently fail to
        store/query against the mismatched ``vector(N)`` column (wasted spend,
        no error).  Checked at the EMBED SITE (not schema-init) so the legitimate
        construct-then-reprovision flow — a migration, or a test that drops and
        recreates the side table at its own dimension — is never blocked; the
        guard fires only when an embed is actually about to be billed.

        Validated once per instance and cached (the column width is fixed for
        the instance's lifetime).  Both orchestration gate sites (#544 PR1 +
        PR2) are now wired; standalone callers remain ungated by design.
        """
        if self._embedding_dim_validated or self._embedding_backend is None:
            return
        conn = self._get_conn()
        if conn is None:
            return
        actual = _actual_embedding_dimension(conn, "memory_note_embeddings")
        expected = self._embedding_backend.dimensions
        if actual is not None and actual != _UNKNOWN_DIMENSION and actual != expected:
            raise RuntimeError(
                f"PgvectorMemoryBackend: the memory_note_embeddings.embedding "
                f"column is vector({actual}), but the active embedding model "
                f"({self._embedding_backend.model_id}) produces {expected}-"
                f"dimension vectors.  Writing or searching would bill embeddings "
                f"that then fail to store or query against the mismatched column "
                f"(silent wasted spend).  This happens when the side table was "
                f"created at a different dimension (the FTS-only default, or a "
                f"previously-pinned model).  To fix: drop the side table "
                f"(`DROP TABLE memory_note_embeddings;`) so it is re-created at "
                f"{expected} dims on next start, or pin the embedding model whose "
                f"dimension matches the existing column.  (Automatic re-index on "
                f"a bulk re-index tool is a follow-up item from #544 PR2.)"
            )
        self._embedding_dim_validated = True

    def _upsert_embedding(self, note_name: str, body: str) -> None:
        """Embed ``body`` and upsert the vector into ``memory_note_embeddings``.

        Called AFTER the canonical note transaction is committed.  Any
        exception here is logged and swallowed — the note already exists
        in ``memory_notes`` and FTS can still find it.

        Skips the embed call (and the upsert) when:
        * ``embed()`` returns ``None`` (provider unavailable, rate-limited, etc.)
        * The body is empty (nothing to embed)
        """
        if not body or not body.strip():
            return

        backend = self._embedding_backend
        if backend is None:
            return

        # Cost-safety: fail LOUD before billing an embed if the side-table column
        # width mismatches the active model's dimensions (cross-family review #1).
        self._assert_embedding_dim_matches()

        # input_type="search_document": this is the WRITE/index path — the body
        # is a stored document.  A query/document-aware provider embeds it in
        # document mode; OpenAI ignores it (supports_input_type=False).  Pairs
        # with the "search_query" hint at the search() query-embed site.
        vector = None
        try:
            vector = backend.embed(body, input_type="search_document")
        except Exception:  # noqa: BLE001
            # embed() MUST-NOT-RAISE but we defend anyway — a subclassed stub
            # might violate the invariant in tests.
            _logger.debug(
                "PgvectorMemoryBackend: embed() raised unexpectedly for "
                "note_name=%r; skipping side-table upsert",
                note_name,
            )
            return

        if vector is None:
            _logger.debug(
                "PgvectorMemoryBackend: embed() returned None for note_name=%r "
                "(provider unavailable or rate-limited); skipping upsert",
                note_name,
            )
            return

        model_id = backend.model_id
        dimensions = backend.dimensions

        # Produced-length backstop (dimension-honesty, the CRITICAL class from
        # #200 PR2): a backend MUST return a vector whose length equals its
        # declared ``dimensions``.  If the produced length diverges (a buggy or
        # mis-reduced backend), DO NOT store the row — the ``dimensions`` column
        # would claim N while the vector held M, and the ANN model+dimension
        # filter would later select a vector the cosine operator cannot compare.
        # Skip + warn rather than let the vector(N) column constraint abort the
        # whole transaction.
        if len(vector) != dimensions:
            _logger.warning(
                "PgvectorMemoryBackend: embed() for note_name=%r produced a "
                "vector of length %d but backend declares dimensions=%d; "
                "skipping side-table upsert (dimension-honesty backstop)",
                note_name,
                len(vector),
                dimensions,
            )
            return

        # Wrap in pgvector.Vector so the registered dumper emits the bracket
        # form ``[...]`` the vector type accepts (a bare list would adapt to a
        # curly-brace array literal ``{...}`` that vector_in rejects — see
        # _get_conn's "Write path" note).  Constructed INSIDE _do so a malformed
        # vector (Vector() coerces via numpy and may raise on non-numeric content)
        # is caught by the best-effort except below — a side-table failure must
        # never break the already-committed canonical note write.
        from pgvector import Vector  # noqa: PLC0415

        def _do(conn: Any) -> None:
            conn.execute(
                _UPSERT_EMBEDDING_SQL,
                (note_name, model_id, dimensions, Vector(vector)),
            )
            conn.commit()

        try:
            self._run_with_reconnect(_do)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "PgvectorMemoryBackend: failed to upsert embedding for "
                "note_name=%r: %s",
                note_name,
                type(exc).__name__,  # MUST 5 redaction — type name only
            )

    # ── search() override — ANN + FTS fallback ──────────────────────

    def search(self, query: str, limit: int = 10) -> list[NoteRef]:
        """Cosine-ANN search when embedding backend is configured; FTS fallback.

        When ``supports_semantic_search=True`` (embedding backend present):
        1. Embed the query text via ``self._embedding_backend.embed(query)``.
        2. Run cosine-distance ANN query on ``memory_note_embeddings``, filtered
           to rows matching the active backend's ``model_id`` and ``dimensions``
           (stale embeddings from a different model are excluded — they remain
           reachable via FTS only).
        3. If embed() returns None or the ANN query fails, fall back to the
           parent's FTS ``search()``.

        When ``supports_semantic_search=False`` (no embedding backend):
        * Delegates directly to the parent FTS ``search()``.

        ANN model filter
        ----------------
        The WHERE clause ``e.model_id = %s AND e.dimensions = %s`` ensures
        that cosine distance is computed only between vectors of the SAME
        dimensionality from the SAME model.  A query vector from
        ``text-embedding-3-small`` (1536-d) cannot be compared against stored
        vectors from ``text-embedding-3-large`` (3072-d) without silent
        truncation/padding.  Mismatched rows are silently excluded and
        reachable via FTS fallback.

        Cost gate note
        --------------
        Ungated at the backend layer (by design).  Per the Q3 ruling the gate
        belongs at the orchestration layer (backend stays cost-unaware).  There
        is NO framework gate site for ``memory.search()`` query-embed: unlike
        ``corpus.query()`` (which the CLI ``atomic-agents corpus query`` gate
        wraps, #544 PR2), ``search()`` is not invoked by ``agent.call()`` and is
        not wrapped by any CLI gate.  Standalone callers of ``search()`` are
        ungated by design; a future gated helper is tracked at #586.  See
        spec/46 §"Direct-caller gate boundary".
        """
        if self._embedding_backend is None:
            return super().search(query, limit=limit)

        if not query or not query.strip():
            return []

        # Cost-safety: fail LOUD before billing the query embed if the side-table
        # column width mismatches the active model's dimensions (cross-family
        # review #1).
        self._assert_embedding_dim_matches()

        # Embed the query text (MUST-NOT-RAISE — None on any failure).
        # Pass input_type="search_query": the write path embeds documents, so a
        # query/document-aware provider (supports_input_type=True) embeds this
        # correctly as a QUERY.  OpenAI ignores the hint (supports_input_type=
        # False), so this is a no-op today and forward-correct for the moment a
        # query-aware backend is registered without re-touching this call site.
        vector = None
        try:
            vector = self._embedding_backend.embed(query, input_type="search_query")
        except Exception:  # noqa: BLE001
            pass

        if vector is None:
            _logger.debug(
                "PgvectorMemoryBackend: embed() returned None for query=%r; "
                "falling back to FTS search()",
                query[:60],
            )
            return super().search(query, limit=limit)

        model_id = self._embedding_backend.model_id
        dimensions = self._embedding_backend.dimensions

        # Wrap in pgvector.Vector for the ANN ORDER BY bind — same reason as the
        # write path (the registered dumper emits the bracket form vector_in
        # accepts; a bare list adapts to a rejected curly-brace literal).
        # Constructed INSIDE _do so a malformed query vector (Vector() coerces via
        # numpy and may raise on non-numeric content) is treated as a query
        # failure → None sentinel → FTS fallback, not an uncaught crash.
        from pgvector import Vector  # noqa: PLC0415

        def _do(conn: Any) -> list[NoteRef]:
            try:
                cur = conn.execute(
                    _ANN_SEARCH_SQL,
                    (model_id, dimensions, Vector(vector), limit),
                )
                rows = cur.fetchall()
                conn.commit()
                return [self._row_to_note_ref(r) for r in rows]
            except Exception as exc:
                if self._is_connection_error(exc):
                    raise
                try:
                    conn.rollback()
                except Exception:
                    pass
                _logger.debug(
                    "PgvectorMemoryBackend: ANN search query failed (%s); "
                    "falling back to FTS search()",
                    type(exc).__name__,
                )
                return None  # type: ignore[return-value]  # sentinel: caller detects

        try:
            result = self._run_with_reconnect(_do)
            if result is None:
                return super().search(query, limit=limit)
            return result
        except Exception:
            return super().search(query, limit=limit)

    # ── close() override ────────────────────────────────────────────

    def close(self) -> None:
        """Release Postgres connections AND the embedding backend's resources.

        Calls ``embedding_backend.close()`` (idempotent per MUST 8) before
        releasing the Postgres connections.  Idempotent: calling twice is safe.
        """
        if self._embedding_backend is not None:
            try:
                self._embedding_backend.close()
            except Exception:
                pass
        super().close()

    def __repr__(self) -> str:
        provider = (
            self._embedding_backend.provider_id
            if self._embedding_backend is not None
            else "none"
        )
        return (
            f"PgvectorMemoryBackend("
            f"host={self._host!r}, port={self._port!r}, "
            f"dbname={self._dbname!r}, user={self._user!r}, "
            f"agent_root={str(self._agent_root)!r}, "
            f"embedding_provider={provider!r})"
        )


# ── Factory ────────────────────────────────────────────────────────


def make_pgvector_memory_backend_from_url(
    url: str,
    agent_root: Path | None = None,
    lock_backend=None,
    embedding_backend: "EmbeddingBackend | None" = None,
) -> PgvectorMemoryBackend:
    """Build a PgvectorMemoryBackend from an operator-supplied URL.

    Mirrors ``make_postgres_memory_backend_from_url()`` for the pgvector
    variant.  Called by ``get_default_memory_backend()`` when
    ``ATOMIC_AGENTS_MEMORY_BACKEND=pgvector-memory``.

    Args:
        url: postgresql://user:password@host:port/dbname connection URL.
        agent_root: Path for the agent's root directory.  Defaults to
            ``Path.cwd()`` when None.
        lock_backend: Optional LockBackend for apply_staging() serialization.
        embedding_backend: Optional pre-constructed EmbeddingBackend.  When
            None, the factory reads env vars via
            ``get_default_embedding_backend()``.

    Returns:
        Constructed PgvectorMemoryBackend (schema initialized on first connection).

    Raises:
        ImportError: psycopg or pgvector not installed.
        ValueError: URL is invalid or malformed.
        RuntimeError: pgvector extension not installed in the database.
    """
    try:
        import psycopg  # noqa: PLC0415

        _ = psycopg
    except ImportError as exc:
        raise ImportError(
            "PgvectorMemoryBackend requires the 'postgres' extra. "
            "Install via: pip install 'atomic-agents-stack[postgres,pgvector]'"
        ) from exc

    try:
        import pgvector  # noqa: PLC0415  # type: ignore[import]

        _ = pgvector
    except ImportError as exc:
        raise ImportError(
            "PgvectorMemoryBackend requires the 'pgvector' extra. "
            "Install via: pip install 'atomic-agents-stack[postgres,pgvector]'"
        ) from exc

    if agent_root is None:
        agent_root = Path.cwd()

    return PgvectorMemoryBackend(
        agent_root,
        lock_backend=lock_backend,
        url=url,
        embedding_backend=embedding_backend,
    )

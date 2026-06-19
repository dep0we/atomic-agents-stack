"""Tests for PgvectorCorpusBackend (spec/46 PR3, issue #200).

The ANN code paths require a live Postgres + pgvector extension and are
covered by the ``@requires_postgres`` integration suite (CI's Postgres lane).
The tests in THIS file are deliberately DB-free: they exercise the registry
dispatch, the Protocol-conformant ``query()`` signature + graceful fallback,
capability honesty, and the dimension-honesty backstop — all of which run
without any Postgres connection (no URL configured → substring fallback).

Memory note:
    DB-gated tests skip locally; CI catches schema drift. The non-DB tests
    here pin the runtime contract (signature + fallback wiring) that a
    requires_postgres test alone would miss when Postgres is absent locally.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

_POSTGRES_URL = os.environ.get("ATOMIC_AGENTS_TEST_POSTGRES_URL")
_POSTGRES_AVAILABLE = False
if _POSTGRES_URL:
    try:
        import psycopg as _psycopg_check  # noqa: F401

        _POSTGRES_AVAILABLE = True
    except ImportError:
        pass

requires_postgres = pytest.mark.skipif(
    not _POSTGRES_AVAILABLE,
    reason=(
        "Requires ATOMIC_AGENTS_TEST_POSTGRES_URL env var and psycopg installed. "
        "Set ATOMIC_AGENTS_TEST_POSTGRES_URL=postgresql://... to run Postgres tests."
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# Corpus page fixture helper


def _seed_corpus(root: Path) -> None:
    """Write two wiki pages so query() fallback has something to match."""
    from atomic_agents.corpus.filesystem import FilesystemCorpusBackend
    from atomic_agents.memory.backend import WritePolicy

    fs = FilesystemCorpusBackend(root)
    policy = WritePolicy(write_paths=[root])
    fs.write_page(
        "avalanche",
        "The avalanche method targets the highest-interest debt first.",
        "wiki",
        policy,
    )
    fs.write_page(
        "snowball",
        "The snowball method targets the smallest balance first for momentum.",
        "wiki",
        policy,
    )


# ──────────────────────────────────────────────────────────────────────────────
# C2 fail-hard on missing extension — DB-free (closes the CI assurance gap)
#
# CI's pgvector image always HAS the 'vector' extension, so the C2 RuntimeError
# branch in _ensure_pg_schema is never exercised by the live suite.  This
# DB-free test simulates the missing-extension probe (fetchone() is None).


class _MissingExtCursor:
    def fetchone(self):
        return None


class _MissingExtConn:
    def execute(self, sql, params=None):
        return _MissingExtCursor()

    def commit(self):
        pass


def test_pgvector_corpus_ensure_schema_fails_hard_without_extension():
    """_ensure_pg_schema raises RuntimeError (C2) when 'vector' extension absent.

    Negative control: remove the C2 check and this flips from raises to running
    the CREATE TABLE DDL against the fake conn.
    """
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    backend = PgvectorCorpusBackend.__new__(PgvectorCorpusBackend)
    backend._embedding_backend = None  # default dim path; not reached before C2 raise
    with pytest.raises(RuntimeError, match="vector.*extension"):
        backend._ensure_pg_schema(_MissingExtConn())


# ──────────────────────────────────────────────────────────────────────────────
# Construction + capabilities (no DB; no URL configured)


def test_pgvector_corpus_is_subclass_of_filesystem():
    from atomic_agents.corpus.filesystem import FilesystemCorpusBackend
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    assert issubclass(PgvectorCorpusBackend, FilesystemCorpusBackend)


def test_pgvector_corpus_no_url_no_semantic_search(tmp_path):
    """supports_semantic_search is False when no Postgres URL is configured."""
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    backend = PgvectorCorpusBackend(tmp_path, embedding_backend=None, pgvector_url=None)
    caps = backend.capabilities
    assert caps.supports_semantic_search is False
    assert caps.supports_versioning is True


def test_pgvector_corpus_agent_scope_distinguishes_same_basename_roots(tmp_path):
    """Same-basename agent roots under different parents get DISTINCT agent_scopes.

    The shared ``corpus_page_embeddings`` table is keyed UNIQUE(agent_scope,
    corpus, page_name).  If ``_agent_scope`` were the directory BASENAME, two
    agents rooted at .../team-a/researcher and .../team-b/researcher would
    collide — one's UPSERT overwrites the other's vector, and query() cross-reads
    rows across tenants (silent multi-tenant data mixing in the shared-DB fleet
    deployment this table exists to support).  ``_agent_scope`` uses the resolved
    absolute path, so distinct roots get distinct scopes.

    Negative control (project lesson #3 / Principle 11): reverting line 161 to
    ``Path(agent_root).name`` makes both scopes "researcher" and this assertion
    goes RED.  DB-free: no pgvector_url configured.
    """
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    root_a = tmp_path / "team-a" / "researcher"
    root_b = tmp_path / "team-b" / "researcher"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)

    b_a = PgvectorCorpusBackend(root_a, embedding_backend=None, pgvector_url=None)
    b_b = PgvectorCorpusBackend(root_b, embedding_backend=None, pgvector_url=None)

    assert b_a._agent_scope != b_b._agent_scope, (
        "same-basename agent roots collided on agent_scope — multi-tenant leak; "
        "_agent_scope must be the resolved absolute path, not the basename"
    )
    # Both end in the same basename — proof the distinction is the parent path.
    assert b_a._agent_scope.endswith("researcher")
    assert b_b._agent_scope.endswith("researcher")


# ──────────────────────────────────────────────────────────────────────────────
# query() Protocol-signature conformance + graceful fallback


def test_pgvector_corpus_query_signature_matches_protocol():
    """query(text, corpus, *, top_k) — same signature as the Protocol + siblings.

    An earlier draft used (corpus, query_text, *, limit, offset), which broke
    the CorpusBackend Protocol AND every super().query() fallback call.
    """
    import inspect

    from atomic_agents.corpus.backend import CorpusBackend
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    proto_sig = inspect.signature(CorpusBackend.query)
    impl_sig = inspect.signature(PgvectorCorpusBackend.query)
    assert list(impl_sig.parameters) == list(proto_sig.parameters), (
        f"PgvectorCorpusBackend.query params {list(impl_sig.parameters)} must "
        f"match Protocol {list(proto_sig.parameters)}"
    )


def test_pgvector_corpus_query_falls_back_to_substring_without_url(tmp_path):
    """With no URL, query() delegates to the parent substring/tag matcher.

    Negative control for the signature bug: if query() used the wrong
    (corpus, query_text, limit, offset) shape, this super().query() call would
    raise TypeError instead of returning matches.
    """
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    _seed_corpus(tmp_path)
    backend = PgvectorCorpusBackend(tmp_path, embedding_backend=None, pgvector_url=None)

    results = backend.query("avalanche", "wiki", top_k=10)
    names = {r.name for r in results}
    assert "avalanche" in names, (
        "substring fallback must find the 'avalanche' page via the parent matcher"
    )


def test_pgvector_corpus_query_top_k_kwarg_accepted(tmp_path):
    """query() accepts top_k (Protocol kwarg) without raising."""
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    _seed_corpus(tmp_path)
    backend = PgvectorCorpusBackend(tmp_path, embedding_backend=None, pgvector_url=None)
    out = backend.query("method", "wiki", top_k=1)
    assert isinstance(out, list)
    assert len(out) <= 1


# ──────────────────────────────────────────────────────────────────────────────
# Registry dispatch via get_default_corpus_backend


def test_get_default_corpus_backend_dispatches_pgvector(tmp_path):
    """ATOMIC_AGENTS_CORPUS_BACKEND=pgvector-corpus returns a PgvectorCorpusBackend.

    Exercises the real dispatch branch in get_default_corpus_backend (no URL
    set → constructs in FTS-fallback mode, no DB connection attempted).
    """
    from atomic_agents.corpus import get_default_corpus_backend
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    env = {"ATOMIC_AGENTS_CORPUS_BACKEND": "pgvector-corpus"}
    # Ensure no pgvector URL leaks in from the ambient env.
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("ATOMIC_AGENTS_PGVECTOR_URL", None)
        backend = get_default_corpus_backend(tmp_path)
    assert isinstance(backend, PgvectorCorpusBackend)
    assert backend.backend_id == "pgvector-corpus"


def test_pgvector_corpus_is_a_known_lazy_id_before_registration(tmp_path):
    """'pgvector-corpus' is surfaced as a known id even before the extra registers.

    Mirrors the memory registry's _LAZY_BACKEND_IDS contract: an operator who
    typos the id (or whose [pgvector] extra has not been touched yet) should see
    'pgvector-corpus' in the "Available:" suggestion list, not a list missing the
    real id.  The CorpusBackendNotRegistered message for an unknown id must
    include it.

    Negative control: dropping _LAZY_BACKEND_IDS from the unknown-id error's
    known-set union removes 'pgvector-corpus' from the message and this flips red.
    """
    from atomic_agents.corpus import (
        _LAZY_BACKEND_IDS,
        unregister_corpus_backend,
    )
    from atomic_agents.corpus import get_default_corpus_backend
    from atomic_agents.exceptions import CorpusBackendNotRegistered

    assert "pgvector-corpus" in _LAZY_BACKEND_IDS

    # Ensure the lazy id is NOT in the live registry for this assertion (a prior
    # test in the session may have registered it on first dispatch).
    unregister_corpus_backend("pgvector-corpus")
    try:
        env = {"ATOMIC_AGENTS_CORPUS_BACKEND": "totally-bogus-backend-id"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(CorpusBackendNotRegistered) as excinfo:
                get_default_corpus_backend(tmp_path)
        assert "pgvector-corpus" in str(excinfo.value), (
            "unknown-backend error did not surface the lazy 'pgvector-corpus' id"
        )
    finally:
        # Re-register so we don't leave the registry in a state that depends on
        # test ordering (the dispatch test above registers it on construct).
        from atomic_agents.corpus import register_corpus_backend
        from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

        register_corpus_backend("pgvector-corpus", PgvectorCorpusBackend)


# ──────────────────────────────────────────────────────────────────────────────
# Dimension-honesty backstop on index_page (no DB needed: guarded before conn)


def test_pgvector_corpus_index_page_skips_on_dimension_mismatch(tmp_path):
    """index_page MUST skip the upsert when len(vector) != declared dimensions.

    Negative control: strip the ``len(vector) != dimensions`` guard and the
    spy connection records an execute() call instead of being left untouched.
    """
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend

    class _WrongLenBackend:
        provider_id = "stub"
        model_id = "stub-embedding-v1"
        dimensions = 4

        def embed(self, text, *, input_type=None):
            return [0.0, 0.0]  # mismatch (declares 4, produces 2)

    class _SpyConn:
        def __init__(self):
            self.executed = []

        def execute(self, *args, **kwargs):
            self.executed.append(args)

        def commit(self):
            pass

        def rollback(self):
            pass

    backend = PgvectorCorpusBackend(
        tmp_path,
        embedding_backend=_WrongLenBackend(),
        pgvector_url="postgresql://localhost/test",
    )
    # Short-circuit the embed-site column-width guard (this DB-free unit test
    # targets the per-row produced-length skip, not the column-width check; the
    # spy conn does not support the catalog probe).
    backend._embedding_dim_validated = True
    spy = _SpyConn()
    backend._get_pg_conn = lambda: spy  # type: ignore[method-assign]

    backend.index_page("wiki", "avalanche", "some body to embed")
    assert spy.executed == [], (
        "index_page must skip the upsert on a dimension mismatch (no execute call)"
    )

    # Positive control: a correct-length vector reaches the upsert path.
    class _RightLenBackend(_WrongLenBackend):
        def embed(self, text, *, input_type=None):
            return [0.0, 0.0, 0.0, 0.0]

    backend._embedding_backend = _RightLenBackend()
    backend.index_page("wiki", "snowball", "another body")
    assert len(spy.executed) == 1, "a correct-length vector must reach the upsert"


# ──────────────────────────────────────────────────────────────────────────────
# write_page() override wires index_page (the write-dead-index regression)


def test_pgvector_corpus_write_page_calls_index_page(tmp_path):
    """write_page() MUST call index_page() so the ANN index is populated.

    Without the override, FilesystemCorpusBackend.write_page runs and
    index_page is never invoked — the ANN index stays permanently empty and
    semantic corpus search returns nothing on the happy path (capability-honesty
    violation: supports_semantic_search=True but always []).

    DB-free: we stub _get_pg_conn so index_page short-circuits, and spy on
    index_page to assert the override calls it after the canonical write.

    Negative control: remove the write_page override and index_page is never
    called → recorded list stays empty → this test flips red.
    """
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend
    from atomic_agents.memory.backend import WritePolicy

    class _Stub:
        provider_id = "stub"
        model_id = "stub-embedding-v1"
        dimensions = 4

        def embed(self, text, *, input_type=None):
            return [0.0, 0.0, 0.0, 0.0]

    backend = PgvectorCorpusBackend(
        tmp_path,
        embedding_backend=_Stub(),
        pgvector_url="postgresql://localhost/test",
    )

    recorded: list[tuple[str, str, str]] = []
    backend.index_page = lambda corpus, name, body: recorded.append(  # type: ignore[method-assign]
        (corpus, name, body)
    )

    policy = WritePolicy(write_paths=[tmp_path])
    backend.write_page("avalanche", "Avalanche body text.", "wiki", policy)

    assert recorded == [("wiki", "avalanche", "Avalanche body text.")], (
        "write_page must call index_page(corpus, name, body) after the "
        "canonical filesystem write"
    )
    # The canonical page must also exist on disk (super().write_page ran).
    assert backend.read_page("avalanche", "wiki") is not None


def test_pgvector_corpus_write_page_no_index_without_url(tmp_path):
    """write_page() must NOT attempt indexing when no Postgres URL is configured.

    The override gates index_page on (embedding_backend AND pgvector_url). With
    no URL, semantic search is off; write_page is a pure filesystem write.
    """
    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend
    from atomic_agents.memory.backend import WritePolicy

    backend = PgvectorCorpusBackend(tmp_path, embedding_backend=None, pgvector_url=None)
    called = []
    backend.index_page = lambda *a, **k: called.append(a)  # type: ignore[method-assign]

    policy = WritePolicy(write_paths=[tmp_path])
    backend.write_page("snowball", "Snowball body.", "wiki", policy)
    assert called == [], "index_page must not run when no pgvector URL is configured"
    assert backend.read_page("snowball", "wiki") is not None


# ──────────────────────────────────────────────────────────────────────────────
# Live integration (CI Postgres lane) — write→query semantic roundtrip


@requires_postgres
def test_pgvector_corpus_live_write_query_roundtrip(tmp_path):
    """write_page → query returns the page via the ANN path (index populated).

    End-to-end gate for the write-dead-index bug: a configured pgvector-corpus
    backend must return semantic results after a normal write_page, with NO
    manual index_page call. Uses a content-derived stub so distinct pages get
    distinct vectors.

    Negative control: remove the write_page override (index never populated) →
    ANN finds 0 rows → assertion that the page is returned flips red.
    """
    from tests.stub_embedding import ContentDerivedStubEmbeddingBackend

    from atomic_agents.corpus.pgvector import PgvectorCorpusBackend
    from atomic_agents.memory.backend import WritePolicy

    from atomic_agents.corpus.pgvector import (
        _CREATE_CORPUS_EMBEDDINGS_HNSW_INDEX,
        _CREATE_CORPUS_EMBEDDINGS_TABLE,
    )

    stub = ContentDerivedStubEmbeddingBackend(dimensions=4)
    backend = PgvectorCorpusBackend(
        tmp_path, embedding_backend=stub, pgvector_url=_POSTGRES_URL
    )
    conn = backend._get_pg_conn()
    assert conn is not None
    # ORDER-INDEPENDENCE (feedback_db_gated_tests_skip_locally): the embedding
    # column is ``vector(N)`` fixed at first migration.  A future corpus live
    # test that migrates at the default 1536 (or pytest-randomly/xdist reorder
    # against a persistent DB) would create the column at 1536 and silently fail
    # this test's dim-4 writes.  DROP + recreate at dim=4 so the column dimension
    # is order-independent — same guard the memory fixture applies.  DROP CASCADE
    # also clears any prior rows, so a separate DELETE is not needed.
    conn.execute("DROP TABLE IF EXISTS corpus_page_embeddings CASCADE")
    conn.execute(_CREATE_CORPUS_EMBEDDINGS_TABLE.format(dimensions=4))
    conn.execute(_CREATE_CORPUS_EMBEDDINGS_HNSW_INDEX)
    conn.commit()

    policy = WritePolicy(write_paths=[tmp_path])
    backend.write_page(
        "avalanche",
        "The avalanche method targets the highest-interest debt first.",
        "wiki",
        policy,
    )

    # The ANN index must now contain the page (write_page wired index_page).
    cur = conn.execute(
        "SELECT page_name FROM corpus_page_embeddings "
        "WHERE agent_scope = %s AND corpus = %s AND page_name = %s",
        (backend._agent_scope, "wiki", "avalanche"),
    )
    assert cur.fetchone() is not None, (
        "write_page did not populate the corpus embedding index"
    )
    conn.commit()

    results = backend.query("avalanche debt method", "wiki", top_k=5)
    names = {r.name for r in results}
    assert "avalanche" in names, "ANN query did not return the written page"
    backend.close()


@requires_postgres
def test_pgvector_corpus_dimension_mismatch_fails_hard(tmp_path):
    """index_page() fails LOUD when the existing column width != model dims.

    Cross-family review #1 / ruling 2026-06-18 "fix now": a corpus_page_embeddings
    column created at one width (the FTS-only 1536 default, or a previously-pinned
    model) plus a now-pinned different-dimension model would otherwise BILL embeds
    in index_page()/query() that then silently fail against the mismatched column.
    The guard fires at the EMBED SITE (not schema-init, so construct-then-
    reprovision flows are never blocked), BEFORE the billable embed.

    Negative control: remove the _assert_embedding_dim_matches guard and the
    index call succeeds with no raise.
    """
    import psycopg

    from tests.stub_embedding import ContentDerivedStubEmbeddingBackend

    from atomic_agents.corpus.pgvector import (
        _CREATE_CORPUS_EMBEDDINGS_TABLE,
        PgvectorCorpusBackend,
    )

    # Normalize the SHARED embeddings table to vector(4) via a RAW connection so
    # the setup itself never triggers the embed-site guard.
    raw = psycopg.connect(_POSTGRES_URL)
    try:
        raw.execute("DROP TABLE IF EXISTS corpus_page_embeddings CASCADE")
        raw.execute(_CREATE_CORPUS_EMBEDDINGS_TABLE.format(dimensions=4))
        raw.commit()
    finally:
        raw.close()

    # A backend whose model produces dim 8 must FAIL HARD on the first embed.
    mismatched = PgvectorCorpusBackend(
        tmp_path,
        embedding_backend=ContentDerivedStubEmbeddingBackend(dimensions=8),
        pgvector_url=_POSTGRES_URL,
    )
    with pytest.raises(RuntimeError, match=r"vector\(4\)"):
        mismatched.index_page("wiki", "mismatch-probe", "A body long enough to embed.")
    try:
        mismatched.close()
    except Exception:
        pass


@requires_postgres
def test_pgvector_corpus_schema_init_succeeds_above_hnsw_dimension_limit(tmp_path):
    """A >2000-dim embedding backend must NOT crash corpus schema-init.

    pgvector's HNSW index supports at most 2000 dims; ``text-embedding-3-large``
    (3072) is a supported model.  ``_ensure_pg_schema`` must create the
    vector(3072) column and SKIP the HNSW index (ANN degrades to seq-scan)
    rather than raising on ``CREATE INDEX ... USING hnsw``.

    Negative control: remove the ``if dimensions > _HNSW_MAX_DIMENSIONS`` guard
    in _maybe_create_hnsw_index and this test goes RED (schema-init raises).
    """
    from tests.stub_embedding import ContentDerivedStubEmbeddingBackend

    from atomic_agents.corpus.pgvector import (
        _HNSW_MAX_DIMENSIONS,
        PgvectorCorpusBackend,
    )

    big_dim = _HNSW_MAX_DIMENSIONS + 1072  # 3072
    stub = ContentDerivedStubEmbeddingBackend(dimensions=big_dim)
    backend = PgvectorCorpusBackend(
        tmp_path, embedding_backend=stub, pgvector_url=_POSTGRES_URL
    )

    # Drop the shared table so this test's schema-init sizes it at big_dim.
    # _get_pg_conn() runs _ensure_pg_schema once; reset _schema_initialized so a
    # second _get_pg_conn re-runs the DDL at big_dim after the DROP.
    conn = backend._get_pg_conn()
    assert conn is not None
    conn.execute("DROP TABLE IF EXISTS corpus_page_embeddings CASCADE")
    conn.commit()
    backend._schema_initialized = False

    # Re-run schema-init at big_dim: must NOT raise despite big_dim > 2000.
    conn = backend._get_pg_conn()
    assert conn is not None

    cur = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'corpus_page_embeddings'"
    )
    assert cur.fetchone() is not None, "corpus embeddings table not created above limit"

    cur = conn.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE tablename = 'corpus_page_embeddings' "
        "  AND indexname LIKE '%hnsw%'"
    )
    assert cur.fetchone() is None, (
        "HNSW index was created on a >2000-dim corpus vector column — guard "
        "did not skip it"
    )
    conn.commit()

    # Cleanup: drop the over-sized table so other corpus live tests recreate at
    # their own (smaller) dimension.
    conn.execute("DROP TABLE IF EXISTS corpus_page_embeddings CASCADE")
    conn.commit()
    backend.close()

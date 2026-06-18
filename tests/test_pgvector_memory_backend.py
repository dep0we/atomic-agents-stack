"""Integration tests for PgvectorMemoryBackend (spec/46 PR3, issue #200).

Tests decorated with ``@requires_postgres`` require:
- ATOMIC_AGENTS_TEST_POSTGRES_URL env var pointing to a Postgres instance
- The ``vector`` extension installed in that DB (CI does this via psql step)
- psycopg + pgvector Python packages installed

Tests SKIP automatically when ATOMIC_AGENTS_TEST_POSTGRES_URL is not set
(requires_postgres gate). CI's postgres service container (pgvector/pgvector:pg16
image) runs these after the "Enable pgvector extension" step.

Memory note:
    DB-gated tests skip locally; CI catches schema drift.
    After a schema-touching PR, expect red-CI if assertions about version/index
    are stale. Grep `schema_version` and `== 3` assertions across ALL pgvector
    test files after any pgvector schema change.
"""

from __future__ import annotations

import os

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
# Schema version constant (always-run)


def test_pgvector_schema_version_is_3():
    """PgvectorMemoryBackend._SCHEMA_VERSION == 3 (v3 added memory_note_embeddings)."""
    from atomic_agents.memory.pgvector import _SCHEMA_VERSION

    assert _SCHEMA_VERSION == 3


def test_postgres_schema_version_is_still_2():
    """PostgresMemoryBackend._SCHEMA_VERSION remains 2 (pgvector is its own subclass).

    Guard against accidental mutation of the parent class constant.
    """
    from atomic_agents.memory.postgres import _SCHEMA_VERSION as _PG_VER

    assert _PG_VER == 2


# ──────────────────────────────────────────────────────────────────────────────
# Module-level constants (no construction, no Postgres needed)


def test_pgvector_implementation_id_string():
    """PgvectorMemoryBackend has implementation_id as a property returning 'pgvector-memory'.

    We verify via introspection rather than construction because construction
    requires a live Postgres URL (parent raises on url=None).
    """
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    # The property is defined on the class; fget is the function itself
    prop = PgvectorMemoryBackend.__dict__.get("implementation_id")
    assert prop is not None, (
        "implementation_id must be defined on PgvectorMemoryBackend"
    )
    # It should be a property whose fget returns 'pgvector-memory'
    # We test it in the live fixture where construction is possible


def test_pgvector_is_subclass_of_postgres():
    """PgvectorMemoryBackend is a subclass of PostgresMemoryBackend."""
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend
    from atomic_agents.memory.postgres import PostgresMemoryBackend

    assert issubclass(PgvectorMemoryBackend, PostgresMemoryBackend)


# ──────────────────────────────────────────────────────────────────────────────
# pgvector type-adapter registration (no real DB needed — pins the runtime fix)


class _FakeConn:
    """A connection stand-in that accepts attribute assignment.

    Real psycopg connections accept arbitrary attributes, which is what the
    once-per-connection sentinel relies on.  A bare ``object()`` does NOT (it
    has no ``__dict__``), so the optimization tests use this instead.
    """


def test_pgvector_get_conn_registers_vector_adapter(monkeypatch):
    """_get_conn MUST call pgvector.psycopg.register_vector on a fresh conn.

    Without register_vector's LOADER, READING a ``vector`` column returns a
    ``str`` instead of a numpy array, breaking ``list(row['embedding'])`` in
    callers that materialize the stored vector.  Negative control: strip the
    register_vector call and this test goes red (registered stays empty).
    """
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend
    from atomic_agents.memory.postgres import PostgresMemoryBackend

    # Bypass __init__ (needs a live DB).
    backend = PgvectorMemoryBackend.__new__(PgvectorMemoryBackend)

    fake_conn = _FakeConn()
    monkeypatch.setattr(
        PostgresMemoryBackend, "_get_conn", lambda self: fake_conn, raising=True
    )

    registered: list = []

    import pgvector.psycopg as pv_psycopg

    monkeypatch.setattr(
        pv_psycopg, "register_vector", lambda conn: registered.append(conn)
    )

    out = backend._get_conn()
    assert out is fake_conn
    assert registered == [fake_conn], (
        "register_vector must be called on the connection returned by _get_conn"
    )


def test_pgvector_get_conn_registers_once_per_connection(monkeypatch):
    """register_vector MUST run exactly ONCE across N ops on one live connection.

    register_vector issues catalog ``pg_type`` round-trips; calling it on every
    _get_conn() (which fires per memory operation) would tax the hottest path.
    The sentinel attribute stamped on the connection makes subsequent calls
    skip registration.  Negative control: strip the
    ``not getattr(conn, _VECTOR_REGISTERED_ATTR, False)`` guard (register every
    call) and this asserts call_count==1 → flips red at 3.
    """
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend
    from atomic_agents.memory.postgres import PostgresMemoryBackend

    backend = PgvectorMemoryBackend.__new__(PgvectorMemoryBackend)

    # Same connection object returned every call (no reconnect).
    fake_conn = _FakeConn()
    monkeypatch.setattr(
        PostgresMemoryBackend, "_get_conn", lambda self: fake_conn, raising=True
    )

    registered: list = []
    import pgvector.psycopg as pv_psycopg

    monkeypatch.setattr(
        pv_psycopg, "register_vector", lambda conn: registered.append(conn)
    )

    for _ in range(3):
        assert backend._get_conn() is fake_conn
    assert registered == [fake_conn], (
        "register_vector must run exactly once per live connection, not on "
        f"every _get_conn() (ran {len(registered)} times)"
    )


def test_pgvector_get_conn_registers_on_reconnect_with_reused_id(monkeypatch):
    """register_vector MUST run on a fresh conn even if it reuses a freed id().

    Regression for the id()-keyed cache bug: an id-keyed set without eviction
    on _discard_conn would SKIP register_vector when CPython recycles a freed
    object's address for the replacement connection, silently producing
    array-literal bind failures.  The sentinel is stamped ON the connection
    object, so a fresh replacement connection (a new object) does not carry it
    and re-registers — even if CPython reuses the freed id().

    We simulate a reconnect by having the parent _get_conn hand back a SECOND,
    distinct connection object on the next call (the real reconnect path), and
    assert register_vector ran for BOTH.  Negative control: re-introduce an
    id-keyed skip and the second registration is dropped → this flips red.
    """
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend
    from atomic_agents.memory.postgres import PostgresMemoryBackend

    backend = PgvectorMemoryBackend.__new__(PgvectorMemoryBackend)

    # _FakeConn (not bare object()) so the once-per-connection sentinel attribute
    # can actually be stamped — exercising the real production code path where a
    # fresh reconnected conn lacks the sentinel and re-registers.
    conns = [_FakeConn(), _FakeConn()]
    calls = {"n": 0}

    def _fake_parent_get_conn(self):
        # First call returns conns[0]; after a "reconnect" the parent returns
        # conns[1] (a distinct fresh connection that needs its own adapter).
        c = conns[min(calls["n"], 1)]
        calls["n"] += 1
        return c

    monkeypatch.setattr(
        PostgresMemoryBackend, "_get_conn", _fake_parent_get_conn, raising=True
    )

    registered: list = []
    import pgvector.psycopg as pv_psycopg

    monkeypatch.setattr(
        pv_psycopg, "register_vector", lambda conn: registered.append(conn)
    )

    first = backend._get_conn()
    second = backend._get_conn()  # the reconnected (fresh) connection
    assert first is conns[0]
    assert second is conns[1]
    assert registered == [conns[0], conns[1]], (
        "register_vector must run on the reconnected connection, not be skipped "
        "by a stale id-keyed cache"
    )

    # Sanity: the module exposes the override (guards against silent removal).
    assert "_get_conn" in PgvectorMemoryBackend.__dict__, (
        "PgvectorMemoryBackend must override _get_conn to register the adapter"
    )


def test_pgvector_upsert_embedding_skips_on_dimension_mismatch(monkeypatch):
    """_upsert_embedding MUST NOT touch the DB when len(vector) != dimensions.

    Dimension-honesty backstop (#200 PR2 CRITICAL class): a backend whose
    embed() produces a vector of the wrong length must not write a row whose
    ``dimensions`` column lies about the stored vector.  Negative control:
    strip the ``len(vector) != dimensions`` guard and this test goes red
    (the DB path runs and the spy records a call).
    """
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    class _WrongLenBackend:
        provider_id = "stub"
        model_id = "stub-embedding-v1"
        dimensions = 4  # declares 4 …

        def embed(self, text, *, input_type=None):
            return [0.0, 0.0]  # … but produces 2 (mismatch)

    backend = PgvectorMemoryBackend.__new__(PgvectorMemoryBackend)
    backend._embedding_backend = _WrongLenBackend()

    calls: list = []
    monkeypatch.setattr(
        PgvectorMemoryBackend,
        "_run_with_reconnect",
        lambda self, op: calls.append(op),
        raising=True,
    )

    backend._upsert_embedding("note-x", "a body to embed")
    assert calls == [], (
        "DB upsert must be skipped when produced vector length != declared dimensions"
    )

    # Positive control: a correct-length vector DOES reach the DB path.
    class _RightLenBackend(_WrongLenBackend):
        def embed(self, text, *, input_type=None):
            return [0.0, 0.0, 0.0, 0.0]  # matches dimensions=4

    backend._embedding_backend = _RightLenBackend()
    backend._upsert_embedding("note-y", "another body")
    assert len(calls) == 1, "a correct-length vector must reach the DB upsert path"


# ──────────────────────────────────────────────────────────────────────────────
# C2 fail-hard on missing extension — DB-free (closes the CI assurance gap)
#
# The live C2 test (test_pgvector_live_fail_hard_without_extension) self-skips
# unless a SECOND extension-less DB is configured, and CI's pgvector image
# always HAS the extension — so the C2 RuntimeError branch falls through BOTH
# gates.  This DB-free test exercises it by simulating the missing-extension
# probe result (fetchone() is None), closing the gap CI cannot.


class _MissingExtCursor:
    def fetchone(self):
        return None  # simulates: SELECT 1 FROM pg_extension WHERE extname='vector' → no row


class _MissingExtConn:
    """A conn whose extension-check probe reports the 'vector' extension absent."""

    def execute(self, sql, params=None):
        return _MissingExtCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


def test_pgvector_memory_ensure_schema_fails_hard_without_extension():
    """_ensure_schema raises RuntimeError (C2) when the 'vector' extension is absent.

    Negative control: remove the C2 check (the fetchone()-is-None raise) and
    this flips from raises to falling through to super()._ensure_schema.
    """
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    backend = PgvectorMemoryBackend.__new__(PgvectorMemoryBackend)
    with pytest.raises(RuntimeError, match="vector.*extension"):
        backend._ensure_schema(_MissingExtConn())


# ──────────────────────────────────────────────────────────────────────────────
# make_pgvector_memory_backend_from_url — construction guard (no real DB needed)


def test_make_pgvector_factory_requires_pgvector_extra(tmp_path, monkeypatch):
    """make_pgvector_memory_backend_from_url raises clearly when pgvector extra is missing."""
    import builtins

    real_import = builtins.__import__

    def _deny_pgvector(name, *args, **kwargs):
        if name == "pgvector":
            raise ImportError("No module named 'pgvector'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _deny_pgvector)

    from atomic_agents.memory.pgvector import make_pgvector_memory_backend_from_url

    with pytest.raises((ImportError, RuntimeError)):
        make_pgvector_memory_backend_from_url(
            "postgresql://localhost/test", agent_root=tmp_path
        )


# ──────────────────────────────────────────────────────────────────────────────
# LIVE TESTS — require Postgres + pgvector extension


def _make_stub_embedding(dimensions=4):
    from tests.stub_embedding import StubEmbeddingBackend

    return StubEmbeddingBackend(dimensions=dimensions)


@pytest.fixture
def pgv_backend(tmp_path):
    """PgvectorMemoryBackend connected to the test Postgres instance with stub embeddings.

    Cleanup strategy: DELETE all rows from shared tables before yielding.
    memory_note_embeddings has ON DELETE CASCADE from memory_notes(name), so
    deleting memory_notes cascades to memory_note_embeddings.  memory_note_versions
    has a note_name TEXT column with NO FK (notes can be deleted independently).
    CI always runs with a fresh DB; the broad DELETE ensures test isolation.

    ORDER-INDEPENDENCE: the embeddings table column is ``vector(N)`` fixed at
    first migration.  A no-embedding-backend test (which migrates at the default
    1536) running FIRST against a fresh shared DB would create the column as
    vector(1536), and this fixture's dim-4 writes would then fail the column
    type check (swallowed by _upsert_embedding → a downstream assert finds no
    row).  To remove the hidden ordering dependency (a real risk under
    pytest-randomly / xdist, per feedback_db_gated_tests_skip_locally), the
    fixture DROPs and re-creates the embeddings table at its own dimensions=4
    before yielding.  DROP CASCADE also removes the FK + HNSW index; the v3
    re-create below restores them.
    """
    from atomic_agents.memory.pgvector import (
        _ADD_EMBEDDINGS_FK,
        _CREATE_EMBEDDINGS_HNSW_INDEX,
        _CREATE_EMBEDDINGS_TABLE,
        PgvectorMemoryBackend,
    )

    stub = _make_stub_embedding(dimensions=4)
    backend = PgvectorMemoryBackend(tmp_path, url=_POSTGRES_URL, embedding_backend=stub)

    # Clean up any rows from previous test runs to isolate tests.
    # DELETE in FK-safe order: embeddings (FK child) first, then notes (FK parent).
    # Versions have note_name TEXT (no FK constraint) so order doesn't matter there.
    conn = backend._get_conn()
    if conn is not None:
        # Recreate the embeddings table at THIS fixture's dimensions (4) so the
        # column dimension is order-independent (see docstring).
        conn.execute("DROP TABLE IF EXISTS memory_note_embeddings CASCADE")
        conn.execute(_CREATE_EMBEDDINGS_TABLE.format(dimensions=4))
        conn.execute(_ADD_EMBEDDINGS_FK)
        conn.execute(_CREATE_EMBEDDINGS_HNSW_INDEX)
        conn.execute("DELETE FROM memory_note_versions")
        conn.execute("DELETE FROM memory_notes")
        conn.commit()
    yield backend
    try:
        backend.close()
    except Exception:
        pass


@requires_postgres
def test_pgvector_live_implementation_id(pgv_backend):
    """implementation_id returns 'pgvector-memory' on a constructed instance."""
    assert pgv_backend.implementation_id == "pgvector-memory"


@requires_postgres
def test_pgvector_live_no_embedding_backend_has_no_semantic_search(tmp_path):
    """supports_semantic_search is False when no embedding backend is injected."""
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    b = PgvectorMemoryBackend(tmp_path, url=_POSTGRES_URL)
    assert b.supports_semantic_search is False
    b.close()


@requires_postgres
def test_pgvector_live_with_embedding_backend_has_semantic_search(pgv_backend):
    """supports_semantic_search is True when an embedding backend is injected (stub)."""
    assert pgv_backend.supports_semantic_search is True


@requires_postgres
def test_pgvector_live_capabilities_embedding_provider_none_when_no_backend(tmp_path):
    """capabilities().embedding_provider is None when no embedding backend."""
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    b = PgvectorMemoryBackend(tmp_path, url=_POSTGRES_URL)
    caps = b.capabilities()
    assert caps.embedding_provider is None
    b.close()


@requires_postgres
def test_pgvector_live_capabilities_embedding_provider_set(pgv_backend):
    """capabilities().embedding_provider matches stub provider_id."""
    caps = pgv_backend.capabilities()
    assert caps.embedding_provider == "stub"


@requires_postgres
def test_pgvector_live_capabilities_embedding_backend_resolved(pgv_backend):
    """capabilities().embedding_backend_resolved is the injected backend instance."""
    caps = pgv_backend.capabilities()
    assert caps.embedding_backend_resolved is pgv_backend._embedding_backend


@requires_postgres
def test_pgvector_live_close_idempotent(tmp_path):
    """close() is idempotent — calling twice must not raise."""
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    b = PgvectorMemoryBackend(tmp_path, url=_POSTGRES_URL)
    b.close()
    b.close()  # must not raise


@requires_postgres
def test_pgvector_live_search_falls_back_to_fts_with_no_embedding_backend(tmp_path):
    """search() falls back to FTS when no embedding backend is configured."""
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    b = PgvectorMemoryBackend(tmp_path, url=_POSTGRES_URL)
    # No notes written — FTS also returns empty list
    result = b.search("test query", limit=5)
    assert isinstance(result, list)
    b.close()


@requires_postgres
def test_pgvector_live_write_note_succeeds_without_embedding_backend(tmp_path):
    """write_note() succeeds with no embedding backend (best-effort: no upsert, note still stored)."""
    from atomic_agents.memory.backend import WritePolicy
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend
    from atomic_agents.types import Capture

    b = PgvectorMemoryBackend(tmp_path, url=_POSTGRES_URL)
    capture = Capture(
        type="preference",
        name="No-embed note",
        description="Test note without embedding",
        confidence="high",
        sources=["test"],
        body="Body without embedding.",
    )
    b.write_note(capture, WritePolicy(write_paths=[tmp_path]))
    notes = b.list_notes()
    assert any("No-embed note" in n.name for n in notes)
    b.close()


@requires_postgres
def test_pgvector_live_schema_version_in_meta(pgv_backend):
    """memory_meta table must have schema_version = 3 after _ensure_schema."""
    conn = pgv_backend._get_conn()
    assert conn is not None
    cur = conn.execute("SELECT value FROM memory_meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    conn.commit()
    assert row is not None
    assert int(row["value"]) == 3


@requires_postgres
def test_pgvector_live_embeddings_table_exists(pgv_backend):
    """memory_note_embeddings table must exist after _ensure_schema."""
    conn = pgv_backend._get_conn()
    assert conn is not None
    cur = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'memory_note_embeddings'"
    )
    row = cur.fetchone()
    conn.commit()
    assert row is not None, "memory_note_embeddings table not created by _ensure_schema"


@requires_postgres
def test_pgvector_live_hnsw_index_exists(pgv_backend):
    """HNSW index must exist on memory_note_embeddings after _ensure_schema."""
    conn = pgv_backend._get_conn()
    assert conn is not None
    cur = conn.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE tablename = 'memory_note_embeddings' "
        "  AND indexname LIKE '%hnsw%'"
    )
    row = cur.fetchone()
    conn.commit()
    assert row is not None, "HNSW index not created on memory_note_embeddings"


@requires_postgres
def test_pgvector_live_write_note_stores_embedding(pgv_backend):
    """write_note() upserts an embedding row into memory_note_embeddings.

    The side table column is ``note_name`` (NOT ``page_name`` — that is the
    corpus table's column) and write_note stores the DERIVED SLUG
    (``derive_filename(type, name)``), not the human name. Query by the slug.
    """
    from atomic_agents._schema import derive_filename
    from atomic_agents.memory.backend import WritePolicy
    from atomic_agents.types import Capture

    capture = Capture(
        type="preference",
        name="Embedding test note",
        description="A test",
        confidence="high",
        sources=["test"],
        body="Body text for embedding test.",
    )
    ref = pgv_backend.write_note(
        capture, WritePolicy(write_paths=[pgv_backend._agent_root])
    )
    expected_note_name = derive_filename(capture.type, capture.name)
    assert ref.name == expected_note_name

    conn = pgv_backend._get_conn()
    assert conn is not None
    cur = conn.execute(
        "SELECT note_name FROM memory_note_embeddings WHERE note_name = %s",
        (expected_note_name,),
    )
    row = cur.fetchone()
    conn.commit()
    assert row is not None, "write_note() did not upsert an embedding row"


@requires_postgres
def test_pgvector_live_reconnect_does_not_crash_on_v3_schema(pgv_backend):
    """Constructing a SECOND backend against an already-v3 DB must not raise.

    Regression for the warm-start schema-version crash: the parent's terminal
    assertion is `existing < _SCHEMA_VERSION` (subclass-tolerant). A DB already
    migrated to v3 by the first backend must reopen cleanly on a second
    construction / reconnect — not raise 'db has v3, code expects v2'.

    Negative control: revert postgres.py:901 to `existing != _SCHEMA_VERSION`
    and this test flips red on the second construction.
    """
    import os

    from atomic_agents.embedding.backend import EmbeddingBackend  # noqa: F401
    from atomic_agents.memory.pgvector import PgvectorMemoryBackend

    # First backend (the fixture) has already migrated the shared DB to v3.
    conn = pgv_backend._get_conn()
    cur = conn.execute("SELECT value FROM memory_meta WHERE key = 'schema_version'")
    assert int(cur.fetchone()["value"]) == 3
    conn.commit()

    url = os.environ["ATOMIC_AGENTS_TEST_POSTGRES_URL"]
    # Second construction against the same v3 DB — _get_conn runs _ensure_schema
    # which calls super()._ensure_schema; the parent must tolerate existing=3.
    second = PgvectorMemoryBackend(
        pgv_backend._agent_root,
        url=url,
        embedding_backend=pgv_backend._embedding_backend,
    )
    try:
        c2 = second._get_conn()  # must NOT raise
        c2.execute("SELECT 1")
        c2.commit()
    finally:
        # Do not close the shared embedding backend (owned by the fixture).
        second._embedding_backend = None
        second.close()


@requires_postgres
def test_pgvector_live_merge_embeds_post_merge_body(pgv_backend):
    """A merge write must embed the POST-MERGE stored body, not the fragment.

    Regression for the merge-corruption bug: write_note on a merge_into capture
    preserves the target body and only appends sources. The override must
    re-read the stored body and embed THAT — embedding the fragment would
    overwrite the target's vector with a single-fragment embedding.

    We assert the stored vector matches embed(full stored body), not
    embed(fragment). Negative control: drop the merge_into re-read branch and
    the stored vector becomes embed(fragment), failing this assertion.

    Uses a CONTENT-DERIVED stub (vector varies by input). The fixture's
    fixed-zero stub would make this false-green: embed(fragment) ==
    embed(stored body) == all zeros regardless of the fix.
    """
    from atomic_agents._schema import derive_filename
    from atomic_agents.memory.backend import WritePolicy
    from atomic_agents.types import Capture
    from tests.stub_embedding import ContentDerivedStubEmbeddingBackend

    # Swap the fixture backend's embedding backend to a content-derived stub
    # (same dimensions=4 as the v3 vector column the fixture already migrated).
    pgv_backend._embedding_backend = ContentDerivedStubEmbeddingBackend(dimensions=4)

    base = Capture(
        type="preference",
        name="Merge base note",
        description="base",
        confidence="high",
        sources=["src-a"],
        body="The original accumulated body of the base note.",
    )
    ref = pgv_backend.write_note(
        base, WritePolicy(write_paths=[pgv_backend._agent_root])
    )
    note_name = derive_filename(base.type, base.name)

    # Merge a fragment into it (body differs; merge preserves target body).
    frag = Capture(
        type="preference",
        name="Merge base note",
        description="base",
        confidence="high",
        sources=["src-b"],
        body="A SHORT incremental fragment that must NOT become the embedding.",
        merge_into=ref.name,
    )
    pgv_backend.write_note(frag, WritePolicy(write_paths=[pgv_backend._agent_root]))

    # The stored body after merge is still the base body (merge only appends
    # sources). The stored embedding must equal embed(stored body).
    stored = pgv_backend.read_note(note_name)
    expected_vec = pgv_backend._embedding_backend.embed(stored.body)

    conn = pgv_backend._get_conn()
    cur = conn.execute(
        "SELECT embedding FROM memory_note_embeddings WHERE note_name = %s",
        (note_name,),
    )
    row = cur.fetchone()
    conn.commit()
    assert row is not None
    stored_vec = list(row["embedding"])
    # Vectors are deterministic for the stub backend; compare elementwise.
    assert len(stored_vec) == len(expected_vec)
    assert stored_vec == [pytest.approx(v) for v in expected_vec], (
        "merge stored the fragment embedding instead of the post-merge body embedding"
    )


@requires_postgres
def test_pgvector_live_search_returns_results(pgv_backend):
    """search() with ANN positively returns the just-written + embedded note.

    Uses a CONTENT-DERIVED stub so query/stored vectors are non-degenerate (the
    fixture's all-zeros stub yields a zero-norm vector → cosine distance is NaN
    → ORDER BY undefined, which would make a `len(results) >= 0` tautology pass
    even on a broken ANN join).  We assert the written note's DERIVED SLUG
    appears in the result set — a broken ANN ORDER BY / join cannot ship green.
    """
    from atomic_agents._schema import derive_filename
    from atomic_agents.memory.backend import WritePolicy
    from atomic_agents.types import Capture
    from tests.stub_embedding import ContentDerivedStubEmbeddingBackend

    # Non-degenerate vectors so cosine distance is well-defined (dimensions=4
    # matches the v3 vector column the fixture already migrated).
    pgv_backend._embedding_backend = ContentDerivedStubEmbeddingBackend(dimensions=4)

    capture = Capture(
        type="feedback",
        name="Searchable ANN note",
        description="A note for ANN search",
        confidence="high",
        sources=["test"],
        body="This note exists to be found by the ANN search.",
    )
    pgv_backend.write_note(capture, WritePolicy(write_paths=[pgv_backend._agent_root]))
    expected_name = derive_filename(capture.type, capture.name)

    results = pgv_backend.search("ANN search", limit=5)
    assert isinstance(results, list)
    assert len(results) >= 1, "ANN search returned no results for an embedded note"
    returned_names = [r.name for r in results]
    assert expected_name in returned_names, (
        "ANN search did not return the just-written + embedded note "
        f"(expected slug {expected_name!r} in {returned_names!r})"
    )


@requires_postgres
def test_pgvector_live_search_model_filter(pgv_backend):
    """ANN search only returns rows matching the active model_id + dimensions.

    Insert an embedding row with a different model_id directly into the DB
    (bypassing the backend), then verify search() does not return it.
    The ANN WHERE clause filters by model_id = active backend's model_id.
    """
    from atomic_agents.memory.backend import WritePolicy
    from atomic_agents.types import Capture

    # Write a real note via the backend (correct model_id/dimensions)
    capture = Capture(
        type="preference",
        name="Model filter test note",
        description="test",
        confidence="high",
        sources=["test"],
        body="correct model",
    )
    pgv_backend.write_note(capture, WritePolicy(write_paths=[pgv_backend._agent_root]))

    conn = pgv_backend._get_conn()
    assert conn is not None

    # Insert a stale-model-id note directly into memory_notes (bypassing backend
    # validation) so we can attach a mismatched embedding to it.
    # Use INSERT ... ON CONFLICT DO NOTHING to be idempotent.
    conn.execute(
        """
        INSERT INTO memory_notes
            (name, type, description, confidence, body, sources, tags,
             pinned, archived, schema_version, content_hash, display_name)
        VALUES (%s, %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb,
                FALSE, FALSE, 1, '', '')
        ON CONFLICT (name) DO NOTHING
        """,
        ("Stale model note", "preference", "stale test", "medium", "stale body"),
    )
    conn.commit()

    # Now insert an embedding row with a DIFFERENT model_id for this note.
    stale_vector = [0.9] * pgv_backend._embedding_backend.dimensions
    conn.execute(
        """
        INSERT INTO memory_note_embeddings
            (note_name, model_id, dimensions, embedding, embedded_at)
        VALUES (%s, %s, %s, %s::vector, NOW())
        ON CONFLICT (note_name) DO NOTHING
        """,
        (
            "Stale model note",
            "totally-different-model-v9",
            pgv_backend._embedding_backend.dimensions,
            stale_vector,
        ),
    )
    conn.commit()

    # search() WHERE model_id = active_backend.model_id excludes the stale row.
    results = pgv_backend.search("test", limit=10)
    returned_names = [r.name if hasattr(r, "name") else str(r) for r in results]
    assert "Stale model note" not in returned_names, (
        "ANN model filter failed: stale row from different model_id was returned"
    )


@requires_postgres
def test_pgvector_live_fail_hard_without_extension(tmp_path):
    """PgvectorMemoryBackend raises clearly when pgvector extension is NOT installed.

    Creates a separate DB with the extension dropped, then verifies the
    backend raises RuntimeError (decision C2) rather than a silent failure.

    NOTE: this test is skipped unless a second DB without pgvector is available
    via ATOMIC_AGENTS_TEST_PGVECTOR_NO_EXT_URL. In standard CI, pgvector is
    always installed; this is a local developer gate.
    """
    no_ext_url = os.environ.get("ATOMIC_AGENTS_TEST_PGVECTOR_NO_EXT_URL")
    if not no_ext_url:
        pytest.skip(
            "ATOMIC_AGENTS_TEST_PGVECTOR_NO_EXT_URL not set — skip C2 gate test"
        )

    from atomic_agents.memory.pgvector import PgvectorMemoryBackend
    from tests.stub_embedding import StubEmbeddingBackend

    stub = StubEmbeddingBackend()
    # Connecting to a DB without the extension should fail-hard during schema init
    with pytest.raises(RuntimeError, match="vector.*extension"):
        b = PgvectorMemoryBackend(tmp_path, url=no_ext_url, embedding_backend=stub)
        # Force schema init by triggering a connection
        b._get_conn()

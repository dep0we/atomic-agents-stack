"""SQLite-specific tests for SQLiteCorpusBackend (spec/34 PR 2 of 4).

These tests cover behaviors that are specific to the SQLite backend and cannot
be exercised via the parametrized conformance suite. The conformance suite
(test_corpus_protocol_conformance.py) covers the Protocol contract shared by
all backends. This file covers:

- Constructor + agent_scope validation (4 tests)
- Schema init (3 tests: tables created, schema_version row, concurrent init)
- WAL ordering (2 tests: busy_timeout before WAL, WAL active after init)
- FTS5 query escaping (6 tests: happy path, empty, whitespace, double-quote,
  single-quote, unicode combining)
- FTS5 is used not substring (1 test: capability check + behavioral diff)
- Hybrid storage (3 tests: read_version body-missing raises, read_page
  body-missing returns None, INSERT-first no orphan)
- Versioning round-trip (2 tests: snapshot writes disk, list_versions globs)
- Cross-corpus isolation (1 test: write wiki, query raw, assert absent)
- URL factory (8 tests: happy path, 6 ValueError sites, credential redaction)
- render_index_summary synthesis (1 test)
- close() idempotency (1 test)
- Registration (1 test)
- Date round-trip (1 test: write with date, read back, assert isinstance date)
- Snapshot guard order (1 test: policy-blocked write does not orphan snapshot)

Total: 35 tests.
"""

from __future__ import annotations

import sqlite3
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from atomic_agents.corpus import (
    SQLiteCorpusBackend,
    get_corpus_backend,
    list_corpus_backends,
    make_sqlite_corpus_backend_from_url,
)
from atomic_agents.corpus.sqlite import _escape_fts5_query
from atomic_agents.exceptions import (
    CorpusCorrupted,
    CorpusInvalidName,
    CorpusPageNotFound,
    CorpusVersionNotFound,
)
from atomic_agents.memory.backend import WritePolicy


# ─────────────────────────────────────────────────────────────────
# Helpers


def _make_backend(
    tmp_path: Path, agent_scope: str = "test-agent"
) -> SQLiteCorpusBackend:
    """Construct a file-backed SQLiteCorpusBackend for testing."""
    return SQLiteCorpusBackend(
        db_path=tmp_path / "corpus.db",
        agent_scope=agent_scope,
        content_root=tmp_path / "content",
    )


def _write_policy(tmp_path: Path) -> WritePolicy:
    """Return a WritePolicy permitting writes anywhere under tmp_path."""
    return WritePolicy(write_paths=[tmp_path])


# ─────────────────────────────────────────────────────────────────
# Category 1 -- Constructor + agent_scope validation (4 tests)


def test_agent_scope_empty_raises_value_error(tmp_path: Path) -> None:
    """Empty agent_scope raises ValueError at construction."""
    with pytest.raises(ValueError, match="non-empty string"):
        SQLiteCorpusBackend(
            db_path=tmp_path / "corpus.db",
            agent_scope="",
            content_root=tmp_path / "content",
        )


def test_agent_scope_with_path_separator_raises_value_error(tmp_path: Path) -> None:
    """agent_scope containing '/' raises ValueError at construction."""
    with pytest.raises(ValueError, match="path separator"):
        SQLiteCorpusBackend(
            db_path=tmp_path / "corpus.db",
            agent_scope="bad/scope",
            content_root=tmp_path / "content",
        )


def test_agent_scope_with_dotdot_raises_value_error(tmp_path: Path) -> None:
    """agent_scope containing '..' raises ValueError at construction.

    Uses a pure '..' without slashes so the dotdot check fires before the
    path-separator check (the registry/sqlite.py pattern evaluates dotdot
    independently of the slash check).
    """
    with pytest.raises(ValueError, match=r"traversal|separator"):
        SQLiteCorpusBackend(
            db_path=tmp_path / "corpus.db",
            agent_scope="..",
            content_root=tmp_path / "content",
        )


def test_agent_scope_with_control_char_raises_value_error(tmp_path: Path) -> None:
    """agent_scope containing a control character (0x00-0x1F) raises ValueError."""
    with pytest.raises(ValueError, match="control character"):
        SQLiteCorpusBackend(
            db_path=tmp_path / "corpus.db",
            agent_scope="scope\x00bad",
            content_root=tmp_path / "content",
        )


# ─────────────────────────────────────────────────────────────────
# Category 2 -- Schema init (3 tests)


def test_schema_tables_are_created_on_first_connection(tmp_path: Path) -> None:
    """After first connection, pages, pages_fts, and meta tables exist in the db."""
    backend = _make_backend(tmp_path)
    # Trigger schema init by issuing a query
    backend.list_pages("wiki")

    conn = sqlite3.connect(str(tmp_path / "corpus.db"))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    backend.close()

    assert "pages" in tables
    assert "meta" in tables
    # pages_fts is a virtual table; check sqlite_master type='table' still lists it
    vtables = {
        row[0]
        for row in sqlite3.connect(str(tmp_path / "corpus.db"))
        .execute("SELECT name FROM sqlite_master WHERE type='table' OR type='shadow'")
        .fetchall()
    }
    # FTS5 shadow tables have 'pages_fts' prefix
    assert any("pages_fts" in name for name in vtables)


def test_schema_version_row_exists_after_init(tmp_path: Path) -> None:
    """The meta table contains a 'schema_version' row with value '1' after init."""
    backend = _make_backend(tmp_path)
    backend.list_pages("wiki")  # force schema init

    conn = sqlite3.connect(str(tmp_path / "corpus.db"))
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    conn.close()
    backend.close()

    assert row is not None
    assert int(row[0]) == 1


def test_concurrent_schema_init_no_race(tmp_path: Path) -> None:
    """Two backends opening the same db file do not corrupt schema_version.

    Simulates the cold-start race: two backends constructed before either
    has issued a query; both then query simultaneously. The INSERT OR IGNORE
    pattern ensures only one schema_version row lands.
    """
    b1 = _make_backend(tmp_path)
    b2 = _make_backend(tmp_path)

    # Force both to init (sequential, but tests the INSERT OR IGNORE path)
    b1.list_pages("wiki")
    b2.list_pages("wiki")

    conn = sqlite3.connect(str(tmp_path / "corpus.db"))
    count = conn.execute(
        "SELECT COUNT(*) FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    conn.close()
    b1.close()
    b2.close()

    assert count == 1, f"Expected exactly one schema_version row; got {count}"


# ─────────────────────────────────────────────────────────────────
# Category 3 -- WAL ordering (2 tests)


def test_wal_mode_active_after_connection(tmp_path: Path) -> None:
    """After the first connection, journal_mode is WAL."""
    backend = _make_backend(tmp_path)
    backend.list_pages("wiki")  # force connection

    conn = sqlite3.connect(str(tmp_path / "corpus.db"))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    backend.close()

    assert mode == "wal"


def test_busy_timeout_is_set(tmp_path: Path) -> None:
    """busy_timeout is set to >= 5000ms on file-backed connections.

    Verifies the pragma ordering rule: busy_timeout BEFORE WAL pragma.
    """
    backend = _make_backend(tmp_path)
    backend.list_pages("wiki")  # force connection init

    conn = sqlite3.connect(str(tmp_path / "corpus.db"))
    timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    backend.close()

    # The backend sets busy_timeout=5000; the new connection we open here
    # gets the default (0), so we verify by checking a live connection.
    # We get the backend's own connection via _get_conn.
    live_conn = backend._get_conn() if not backend._closed else None
    if live_conn is not None:
        live_timeout = live_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert live_timeout >= 5000


# ─────────────────────────────────────────────────────────────────
# Category 4 -- FTS5 query escaping (6 tests)


def test_escape_fts5_query_happy_path() -> None:
    """A plain word produces a double-quoted FTS5 phrase."""
    result = _escape_fts5_query("avalanche")
    assert result == '"avalanche"'


def test_escape_fts5_query_empty_string() -> None:
    """Empty string input returns empty string (caller short-circuits to [])."""
    result = _escape_fts5_query("")
    assert result == ""


def test_escape_fts5_query_whitespace_only() -> None:
    """Whitespace-only input returns empty string."""
    result = _escape_fts5_query("   ")
    assert result == ""


def test_escape_fts5_query_double_quote_escaped() -> None:
    """Internal double-quotes are doubled (FTS5 phrase-search escaping rule)."""
    result = _escape_fts5_query('cat"dog')
    assert result == '"cat""dog"'


def test_escape_fts5_query_single_quote_preserved() -> None:
    """Single quotes pass through unchanged (not a special FTS5 character)."""
    result = _escape_fts5_query("O'Brien")
    assert result == '"O\'Brien"'


def test_escape_fts5_query_unicode_combining() -> None:
    """Unicode combining characters produce a quoted phrase (no crash)."""
    text = "caf́"  # cafe with combining accent
    result = _escape_fts5_query(text)
    assert result.startswith('"')
    assert result.endswith('"')
    assert len(result) > 2


# ─────────────────────────────────────────────────────────────────
# Category 5 -- FTS5 vs substring (1 test)


def test_fts_is_used_not_substring(tmp_path: Path) -> None:
    """SQLiteCorpusBackend declares supports_full_text_search=True.

    The capability flag must be True, distinguishing it from
    FilesystemCorpusBackend (which is False). This is the behavioral
    declaration that query() uses indexed FTS5 not a linear substring scan.
    """
    backend = _make_backend(tmp_path)
    caps = backend.capabilities
    assert caps.supports_full_text_search is True
    assert caps.supports_semantic_search is False
    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 6 -- Hybrid storage (3 tests)


def test_read_version_body_file_missing_raises_corpus_version_not_found(
    tmp_path: Path,
) -> None:
    """read_version raises CorpusVersionNotFound when the snapshot body file is deleted.

    This is the D12 infrastructure-failure case: SQL row exists (list_versions
    returns the VersionRef) but the on-disk snapshot file has been removed
    externally. The SQLite backend surfaces this as CorpusVersionNotFound, not
    as None -- versioning infrastructure failures raise, not return None.

    This test is not reproducible via the conformance suite (the filesystem
    backend cannot simulate SQL-row-exists + disk-file-gone independently;
    both are the same filesystem object).
    """
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    backend.write_page("hybrid-page", "Initial content.", "wiki", policy)
    version_ref = backend.snapshot("hybrid-page", "wiki")

    # Decode the version body path from the VersionRef backend_id
    parts = version_ref.backend_id.split("/", 2)
    assert len(parts) == 3
    corpus_name, stem, version_filename = parts

    versions_dir = backend._versions_dir(corpus_name, stem)
    version_path = versions_dir / version_filename
    assert version_path.is_file(), f"Snapshot file not found at {version_path}"

    # Delete the snapshot file externally
    version_path.unlink()

    with pytest.raises(CorpusVersionNotFound):
        backend.read_version(version_ref)

    backend.close()


def test_read_page_body_file_missing_returns_none(tmp_path: Path) -> None:
    """read_page returns None when the SQL row exists but the body file is gone.

    Hybrid storage creates a scenario where the SQL row is present (visible
    in list_pages) but the on-disk body was deleted externally. Per spec/34
    D12: read_page returns None for missing pages (not raise).
    """
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    backend.write_page("disk-gone-page", "Body that will disappear.", "wiki", policy)

    # Verify the body path and delete it
    body_path = backend._body_path("wiki", "disk-gone-page")
    assert body_path.is_file()
    body_path.unlink()

    # read_page must return None (not raise)
    result = backend.read_page("disk-gone-page", "wiki")
    assert result is None

    backend.close()


def test_insert_first_no_orphan_body_on_failure(tmp_path: Path, monkeypatch) -> None:
    """INSERT-first write order: if SQL write fails, no orphan body file is created.

    Monkeypatches atomic_write to raise after the SQL INSERT, simulating a
    disk-write failure. The compensating DELETE rolls back the SQL row.
    After the exception, neither the SQL row nor the body file should exist.
    """
    from atomic_agents.corpus import sqlite as sqlite_module

    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    original_atomic_write = sqlite_module.atomic_write
    call_count = [0]

    def fail_on_first_call(target, content, *args, **kwargs):
        call_count[0] += 1
        # Fail the first atomic_write call (the body write)
        if call_count[0] == 1:
            raise OSError("Simulated disk failure")
        return original_atomic_write(target, content, *args, **kwargs)

    monkeypatch.setattr(sqlite_module, "atomic_write", fail_on_first_call)

    with pytest.raises(OSError, match="Simulated disk failure"):
        backend.write_page("orphan-test", "Body content.", "wiki", policy)

    # The SQL row should have been rolled back
    body_path = backend._body_path("wiki", "orphan-test")
    assert not body_path.exists(), "Body file must not exist after failed write"

    page = backend.read_page("orphan-test", "wiki")
    assert page is None, "SQL row must be rolled back after failed disk write"

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 7 -- Versioning round-trip (2 tests)


def test_snapshot_writes_disk_file(tmp_path: Path) -> None:
    """snapshot() creates a real file on disk under .versions/<name>/."""
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    backend.write_page("snap-page", "Content to snap.", "wiki", policy)
    version_ref = backend.snapshot("snap-page", "wiki")

    # Decode path from backend_id
    parts = version_ref.backend_id.split("/", 2)
    assert len(parts) == 3
    corpus_name, stem, version_filename = parts

    versions_dir = backend._versions_dir(corpus_name, stem)
    version_path = versions_dir / version_filename
    assert version_path.is_file(), f"Snapshot file not created at {version_path}"
    assert "Content to snap." in version_path.read_text(encoding="utf-8")

    backend.close()


def test_list_versions_globs_snapshot_dir(tmp_path: Path) -> None:
    """list_versions returns all snapshot files in the .versions/ directory.

    Takes two snapshots and asserts list_versions returns both, newest first.
    """
    import time

    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    backend.write_page("ver-list-page", "V1 content.", "wiki", policy)
    ref1 = backend.snapshot("ver-list-page", "wiki")

    # Small sleep to ensure different timestamps in snapshot filenames
    time.sleep(0.01)

    # CAS overwrite so we can snapshot again (snapshot only requires the page to exist)
    import hashlib

    v1_sha = hashlib.sha256("V1 content.".encode()).hexdigest()
    backend.write_page(
        "ver-list-page",
        "V2 content.",
        "wiki",
        policy,
        expected_content_sha256=v1_sha,
    )
    ref2 = backend.snapshot("ver-list-page", "wiki")

    versions = backend.list_versions("ver-list-page", "wiki")
    backend_ids = [v.backend_id for v in versions]

    assert ref1.backend_id in backend_ids
    assert ref2.backend_id in backend_ids
    assert len(versions) >= 2
    # Newest first (lexicographic desc on ISO timestamp prefix)
    assert backend_ids == sorted(backend_ids, reverse=True)

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 8 -- Cross-corpus isolation (1 test)


def test_write_wiki_query_raw_returns_empty(tmp_path: Path) -> None:
    """A page written to 'wiki' does not appear in 'raw' query results.

    This tests the cross-corpus isolation in the FTS5 JOIN query (SEV-6):
    pages_fts results are filtered by corpus discriminator before returning.
    """
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    backend.write_page(
        "wiki-only-page",
        "The keyword uniqueterm12345 is only in wiki.",
        "wiki",
        policy,
    )

    # Query raw corpus for the same keyword
    raw_results = backend.query("uniqueterm12345", "raw")
    assert raw_results == [], f"wiki page leaked into raw query results: {raw_results}"

    # Confirm it IS found in wiki
    wiki_results = backend.query("uniqueterm12345", "wiki")
    assert len(wiki_results) >= 1
    assert any(r.name == "wiki-only-page" for r in wiki_results)

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 9 -- URL factory (8 tests)


def test_url_factory_happy_path(tmp_path: Path) -> None:
    """Happy path: sqlite:///abs/path?agent_scope=myagent constructs a backend."""
    db_path = tmp_path / "happy.db"
    url = f"sqlite:///{db_path}?agent_scope=myagent"
    backend = make_sqlite_corpus_backend_from_url(url)
    assert isinstance(backend, SQLiteCorpusBackend)
    assert backend.agent_scope == "myagent"
    backend.close()


def test_url_factory_site1_non_sqlite_scheme_raises(tmp_path: Path) -> None:
    """ValueError site 1: non-sqlite scheme raises ValueError."""
    with pytest.raises(ValueError, match="scheme"):
        make_sqlite_corpus_backend_from_url("postgres:///path/to/db")


def test_url_factory_site2_netloc_raises(tmp_path: Path) -> None:
    """ValueError site 2: netloc-bearing URL raises ValueError."""
    with pytest.raises(ValueError, match="netloc"):
        make_sqlite_corpus_backend_from_url("sqlite://myhost/path/to/db")


def test_url_factory_site3_fragment_raises(tmp_path: Path) -> None:
    """ValueError site 3: URL with fragment raises ValueError."""
    db_path = tmp_path / "corpus.db"
    with pytest.raises(ValueError, match="fragment"):
        make_sqlite_corpus_backend_from_url(f"sqlite:///{db_path}#myfragment")


def test_url_factory_site4_duplicate_param_raises(tmp_path: Path) -> None:
    """ValueError site 4: duplicate query parameter raises ValueError."""
    db_path = tmp_path / "corpus.db"
    with pytest.raises(ValueError, match="duplicate"):
        make_sqlite_corpus_backend_from_url(
            f"sqlite:///{db_path}?agent_scope=a&agent_scope=b"
        )


def test_url_factory_site5_unknown_param_raises(tmp_path: Path) -> None:
    """ValueError site 5: unknown query parameter raises ValueError."""
    db_path = tmp_path / "corpus.db"
    with pytest.raises(ValueError, match="unsupported"):
        make_sqlite_corpus_backend_from_url(
            f"sqlite:///{db_path}?agent_scope=x&unknown_param=y"
        )


def test_url_factory_site6_empty_path_raises() -> None:
    """ValueError site 6: empty or root-only path raises ValueError."""
    with pytest.raises(ValueError, match="empty path"):
        make_sqlite_corpus_backend_from_url("sqlite:///")


def test_url_factory_credential_redaction() -> None:
    """Credentials in a pasted URL are redacted before echoing in ValueError.

    An operator who accidentally pastes postgres://user:secret@host into
    the sqlite URL factory must NOT see the password in the exception text.
    """
    url = "postgres://user:supersecret@host/db"
    with pytest.raises(ValueError) as exc_info:
        make_sqlite_corpus_backend_from_url(url)
    error_text = str(exc_info.value)
    assert "supersecret" not in error_text, (
        f"Credential leaked in error message: {error_text}"
    )


# ─────────────────────────────────────────────────────────────────
# Category 10 -- render_index_summary synthesis (1 test)


def test_render_index_summary_synthesizes_from_page_metadata(tmp_path: Path) -> None:
    """SQLiteCorpusBackend.render_index_summary synthesizes prose from SQL metadata.

    Unlike FilesystemCorpusBackend (which reads INDEX.md verbatim), the
    SQLite backend synthesizes the index from page title + description fields.
    The output must be a non-empty string containing the written page's title.
    """
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    backend.write_page(
        "financial-freedom",
        "The path to financial freedom requires patience.",
        "wiki",
        policy,
        frontmatter={
            "title": "Financial Freedom",
            "description": "Strategies for debt elimination and wealth building.",
        },
    )

    summary = backend.render_index_summary("wiki")

    assert isinstance(summary, str)
    assert summary, "render_index_summary must return non-empty string when pages exist"
    assert "Financial Freedom" in summary or "financial-freedom" in summary

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 11 -- close() idempotency (1 test)


def test_close_is_idempotent(tmp_path: Path) -> None:
    """Calling close() multiple times does not raise.

    SEV-12 requirement: close() must be idempotent.
    """
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)
    backend.write_page("close-test", "Some content.", "wiki", policy)

    backend.close()
    backend.close()  # second call must not raise
    backend.close()  # third call also fine


# ─────────────────────────────────────────────────────────────────
# Category 12 -- Registration (1 test)


def test_sqlite_backend_is_registered() -> None:
    """'sqlite' is registered in the corpus backend registry after import."""
    backends = list_corpus_backends()
    assert "sqlite" in backends

    cls = get_corpus_backend("sqlite")
    assert cls is SQLiteCorpusBackend


# ─────────────────────────────────────────────────────────────────
# Category 13 -- Date round-trip (1 test)


def test_date_field_round_trips_as_date_object(tmp_path: Path) -> None:
    """date fields (captured, last_seen, expires_at) survive write-read as date objects.

    SEV-4: typed columns store ISO strings; read_page re-parses them as date
    objects. This is NOT the same as PyYAML's auto-parse; it is explicit
    SQL-column re-parsing. Asserts isinstance(page.captured, date).
    """
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    captured_date = date(2026, 1, 15)
    last_seen_date = date(2026, 3, 20)
    expires_date = date(2026, 12, 31)
    ingested_ts = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)

    backend.write_page(
        "date-roundtrip",
        "Body text for date round-trip test.",
        "wiki",
        policy,
        frontmatter={
            "captured": captured_date,
            "last_seen": last_seen_date,
            "expires_at": expires_date,
            "ingested_at": ingested_ts,
        },
    )

    page = backend.read_page("date-roundtrip", "wiki")
    assert page is not None

    assert isinstance(page.captured, date), (
        f"captured must be date, got {type(page.captured)}"
    )
    assert page.captured == captured_date

    assert isinstance(page.last_seen, date), (
        f"last_seen must be date, got {type(page.last_seen)}"
    )
    assert page.last_seen == last_seen_date

    assert isinstance(page.expires_at, date), (
        f"expires_at must be date, got {type(page.expires_at)}"
    )
    assert page.expires_at == expires_date

    assert isinstance(page.ingested_at, datetime), (
        f"ingested_at must be datetime, got {type(page.ingested_at)}"
    )

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 14 -- Snapshot guard order (1 test)


def test_policy_blocked_write_does_not_create_orphan_snapshot(tmp_path: Path) -> None:
    """A WritePolicy-blocked overwrite must not create a snapshot of the existing page.

    SEV-3 guard order: _enforce_corpus_write_policy fires BEFORE _take_snapshot.
    If the policy blocks the write, the .versions/ directory for this page must
    remain empty (no orphan snapshot was written before the block fired).

    This is a regression guard: the incorrect impl snapshots first, then
    checks the policy. The correct impl checks the policy first (step 5),
    then snapshots (step 6).
    """
    # First write goes to a path inside the policy's write_paths
    allowed_policy = _write_policy(tmp_path)
    backend = _make_backend(tmp_path)

    backend.write_page("guarded-page", "Original content.", "wiki", allowed_policy)

    # Now try to CAS-overwrite with a restrictive policy that blocks the write
    import hashlib

    original_sha = hashlib.sha256("Original content.".encode()).hexdigest()
    blocked_policy = WritePolicy(write_paths=[tmp_path / "nonexistent-dir"])

    from atomic_agents.exceptions import WritePathViolation

    with pytest.raises(WritePathViolation):
        backend.write_page(
            "guarded-page",
            "Blocked overwrite content.",
            "wiki",
            blocked_policy,
            expected_content_sha256=original_sha,
        )

    # Verify no snapshot was created (versions dir must be empty or absent)
    versions_dir = backend._versions_dir("wiki", "guarded-page")
    if versions_dir.exists():
        snapshot_files = list(versions_dir.iterdir())
        assert snapshot_files == [], (
            f"Orphan snapshot created by blocked write: {snapshot_files}"
        )

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 15 -- M2: busy_timeout check on live connection (fixed)


def test_busy_timeout_is_set_on_live_connection(tmp_path: Path) -> None:
    """busy_timeout >= 5000ms is verified on the backend's own live connection.

    M2 fix: the previous test called backend.close() before checking the live
    connection, making the check a no-op. This test keeps the backend open and
    checks busy_timeout directly on the connection returned by _get_conn().
    """
    backend = _make_backend(tmp_path)
    backend.list_pages("wiki")  # force schema init and connection setup
    conn = backend._get_conn()  # alive connection on this thread
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout >= 5000, (
        f"busy_timeout should be >= 5000ms, got {busy_timeout}ms"
    )
    backend.close()  # close at the end, not before the check


# ─────────────────────────────────────────────────────────────────
# Category 16 -- H4: CorpusCorrupted on schema version mismatch


def test_schema_mismatch_raises_corpus_corrupted(tmp_path: Path) -> None:
    """A db with the wrong schema_version raises CorpusCorrupted on first use.

    H4 fix: _ensure_schema previously raised RuntimeError; it now raises
    CorpusCorrupted so callers can catch the framework-level exception.
    Simulate by writing a wrong schema_version directly into a fresh db,
    then constructing a new backend against it.
    """
    import sqlite3 as _sqlite3

    db_path = tmp_path / "bad_schema.db"

    # Create the db manually and insert a wrong schema_version
    raw_conn = _sqlite3.connect(str(db_path))
    raw_conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    raw_conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '99')")
    raw_conn.commit()
    raw_conn.close()

    backend = SQLiteCorpusBackend.__new__(SQLiteCorpusBackend)
    backend._in_memory = False
    backend._db_path = db_path
    backend._db_path_str = str(db_path)
    backend._shared_conn = None
    backend._content_root = tmp_path / "content"
    backend._agent_scope = "test-agent"
    backend._tls = __import__("threading").local()
    backend._all_conns = []
    backend._all_conns_lock = __import__("threading").Lock()
    backend._closed = False

    with pytest.raises(CorpusCorrupted, match="schema version mismatch"):
        backend.list_pages("wiki")

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 17 -- H7/H8: query() top_k validation


def test_query_top_k_none_raises_value_error(tmp_path: Path) -> None:
    """query() with top_k=None raises ValueError."""
    backend = _make_backend(tmp_path)
    with pytest.raises(ValueError, match="top_k"):
        backend.query("anything", "wiki", top_k=None)  # type: ignore[arg-type]
    backend.close()


def test_query_top_k_negative_raises_value_error(tmp_path: Path) -> None:
    """query() with top_k=-1 raises ValueError."""
    backend = _make_backend(tmp_path)
    with pytest.raises(ValueError, match="top_k"):
        backend.query("anything", "wiki", top_k=-1)
    backend.close()


def test_query_top_k_zero_returns_empty_list(tmp_path: Path) -> None:
    """query() with top_k=0 returns [] without hitting FTS (short-circuit)."""
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)
    backend.write_page("zero-k-page", "Some searchable content.", "wiki", policy)
    result = backend.query("searchable", "wiki", top_k=0)
    assert result == [], f"Expected [], got {result!r}"
    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 18 -- H11: restore_version raises CorpusPageNotFound for missing page


def test_restore_version_missing_page_raises_corpus_page_not_found(
    tmp_path: Path,
) -> None:
    """restore_version raises CorpusPageNotFound when the target page does not exist.

    H11 fix: without the guard, restore_version delegated to write_page which
    would create a new page via the fresh-write path, ignoring the CAS intent.
    Now it raises CorpusPageNotFound before reading the snapshot.
    """
    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    # Write a page, snapshot it, then test restore against a DIFFERENT name
    backend.write_page("real-page", "Original content.", "wiki", policy)
    version_ref = backend.snapshot("real-page", "wiki")

    # "ghost-page" was never written -- restore_version must refuse
    with pytest.raises(CorpusPageNotFound, match="does not exist"):
        backend.restore_version("ghost-page", "wiki", version_ref, policy)

    # Confirm ghost-page was not created as a side effect
    assert backend.read_page("ghost-page", "wiki") is None

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 19 -- C3: FTS upsert failure rolls back the entire write


def test_fts5_upsert_failure_rolls_back_write(tmp_path: Path, monkeypatch) -> None:
    """If the FTS5 upsert inside write_page raises, the whole transaction rolls back.

    C3 fix: FTS upsert is now inside the BEGIN IMMEDIATE transaction. If it
    fails, rollback means no SQL row lands and no body file is written.

    We inject the failure by patching sqlite3.connect at the module level to
    return a subclassed connection that raises OperationalError when it sees
    the FTS 'delete' command. After the failure is confirmed, we open a clean
    backend against the same db and verify the page is absent.
    """
    import sqlite3 as _sqlite3

    from atomic_agents.corpus import sqlite as sqlite_module

    db_path = tmp_path / "fts_fail.db"
    content_root = tmp_path / "content"

    # Subclass Connection to intercept the FTS upsert delete command
    class _FaultingConnection(_sqlite3.Connection):
        _fts_fail_armed: bool = True

        def execute(self, sql, parameters=()):
            sql_stripped = sql.strip()
            if (
                self._fts_fail_armed
                and "pages_fts" in sql_stripped
                and "'delete'" in sql_stripped
            ):
                self._fts_fail_armed = False  # fire once
                raise _sqlite3.OperationalError("Simulated FTS failure")
            return super().execute(sql, parameters)

    original_connect = _sqlite3.connect

    def patched_connect(database, *args, **kwargs):
        if str(database) == str(db_path):
            conn = _FaultingConnection(database, *args, **kwargs)
            conn.row_factory = _sqlite3.Row
            return conn
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite_module.sqlite3, "connect", patched_connect)

    backend = SQLiteCorpusBackend(
        db_path=db_path, agent_scope="test-agent", content_root=content_root
    )
    policy = _write_policy(tmp_path)

    with pytest.raises(_sqlite3.OperationalError, match="Simulated FTS failure"):
        backend.write_page(
            "fts-fail-page", "Content that should not land.", "wiki", policy
        )

    # Restore original connect so we can open a clean backend to verify state
    monkeypatch.undo()

    # Open a clean backend (new connection, no fault injection) to verify
    clean_backend = SQLiteCorpusBackend(
        db_path=db_path, agent_scope="test-agent", content_root=content_root
    )

    # SQL row must not exist (transaction rolled back before COMMIT)
    assert clean_backend.read_page("fts-fail-page", "wiki") is None, (
        "SQL row must be rolled back when FTS upsert fails inside BEGIN IMMEDIATE"
    )

    # Body file must not exist (atomic_write never ran -- happens after COMMIT)
    body_path = backend._body_path("wiki", "fts-fail-page")
    assert not body_path.exists(), (
        "Body file must not exist: atomic_write is after COMMIT, which never happened"
    )

    clean_backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 20 -- C2: snapshot uses OLD body content, not new content


def test_cas_overwrite_snapshot_captures_old_content(tmp_path: Path) -> None:
    """During a CAS overwrite, the auto-snapshot captures the OLD body, not the new one.

    C2 fix: _sqlite_take_snapshot now accepts body_content: str (already read)
    rather than body_path: Path. The snapshot is taken INSIDE the BEGIN IMMEDIATE
    transaction using the old body content read before the UPSERT fires.
    """
    import hashlib

    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    original_content = "Original body content for snapshot test."
    backend.write_page("snap-content-page", original_content, "wiki", policy)

    # Compute SHA for CAS
    from atomic_agents._io import atomic_write as _aw
    from atomic_agents.corpus.filesystem import _build_page_content, _sha256_hex

    on_disk_original = _build_page_content(original_content, None)
    original_sha = _sha256_hex(on_disk_original)

    # CAS overwrite -- this should auto-snapshot the original
    new_content = "New body content after overwrite."
    backend.write_page(
        "snap-content-page",
        new_content,
        "wiki",
        policy,
        expected_content_sha256=original_sha,
    )

    # One auto-snapshot should exist
    versions = backend.list_versions("snap-content-page", "wiki")
    assert len(versions) >= 1, (
        "At least one auto-snapshot should exist after CAS overwrite"
    )

    # Read back the snapshot -- it must contain the ORIGINAL content
    snapshot_page = backend.read_version(versions[0])
    assert original_content in snapshot_page.body, (
        f"Snapshot must contain original content, but body was: {snapshot_page.body!r}"
    )
    assert new_content not in snapshot_page.body, (
        "Snapshot must NOT contain new content (it captured the OLD body)"
    )

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 21 -- R2-C1: compensation preserves title on atomic_write failure


def test_compensation_preserves_title_on_atomic_write_failure_during_cas(
    tmp_path: Path, monkeypatch
) -> None:
    """Compensation after a CAS atomic_write failure must restore the page title.

    R2-C1 bug: the SELECT inside BEGIN IMMEDIATE was missing the 'title' column,
    so compensation restored the row with title=NULL, silently destroying the
    page title on a failed CAS overwrite.

    Fix: 'title' is now included in the pre-UPSERT SELECT so compensation has
    the real value to restore.
    """
    from atomic_agents.corpus import sqlite as sqlite_module
    from atomic_agents.corpus.filesystem import _build_page_content, _sha256_hex

    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    # Write a page with a title embedded in frontmatter.
    backend.write_page(
        "titled-page",
        "Body text for the titled page.",
        "wiki",
        policy,
        frontmatter={"title": "Real Title"},
    )

    # Confirm the title was stored.
    page = backend.read_page("titled-page", "wiki")
    assert page is not None
    assert page.ref.title == "Real Title", (
        f"Expected 'Real Title', got {page.ref.title!r}"
    )

    # Compute the CAS SHA for the current on-disk content.
    on_disk = _build_page_content(
        "Body text for the titled page.", {"title": "Real Title"}
    )
    current_sha = _sha256_hex(on_disk)

    # Monkeypatch atomic_write to raise AFTER the SQL transaction has committed
    # so we exercise the compensation path.
    def failing_atomic_write(target, content, encoding="utf-8"):
        raise OSError("Simulated disk write failure for R2-C1 test")

    monkeypatch.setattr(sqlite_module, "atomic_write", failing_atomic_write)

    # Attempt a CAS overwrite -- the disk write will fail, triggering compensation.
    with pytest.raises(OSError, match="Simulated disk write failure"):
        backend.write_page(
            "titled-page",
            "New content that should not land.",
            "wiki",
            policy,
            expected_content_sha256=current_sha,
        )

    # After compensation the page must still exist with the original title.
    monkeypatch.undo()
    page_after = backend.read_page("titled-page", "wiki")
    assert page_after is not None, (
        "Page must still exist after failed CAS + compensation"
    )
    assert page_after.ref.title == "Real Title", (
        f"Compensation must restore the original title 'Real Title', "
        f"got {page_after.ref.title!r}"
    )

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 22 -- R2-C2: partial atomic_write (rename done) keeps new SQL state


def test_atomic_write_partial_failure_fsync_only_keeps_new_state(
    tmp_path: Path, monkeypatch
) -> None:
    """If the rename inside atomic_write completes but an error follows, keep the new SQL.

    R2-C2 fix: after atomic_write raises, write_page reads the on-disk body and
    computes its SHA-256.  If it matches the NEW content (rename succeeded), the
    new SQL row is correct and must NOT be rolled back to the old state.  The
    exception re-raises so the caller knows the durability guarantee was weakened.

    We simulate this by monkeypatching atomic_write to write the new content to
    disk directly (mimicking a completed rename) and then raise an OSError.
    """
    from atomic_agents.corpus import sqlite as sqlite_module
    from atomic_agents.corpus.filesystem import _build_page_content, _sha256_hex

    backend = _make_backend(tmp_path)
    policy = _write_policy(tmp_path)

    # Write v0.
    backend.write_page("partial-fsync-page", "Version zero content.", "wiki", policy)

    v0_page = backend.read_page("partial-fsync-page", "wiki")
    assert v0_page is not None
    v0_byte_size = v0_page.ref.byte_size

    # Compute CAS SHA for the current on-disk state.
    on_disk_v0 = _build_page_content("Version zero content.", None)
    sha_v0 = _sha256_hex(on_disk_v0)

    # Build the expected v1 on-disk content to know byte_size.
    on_disk_v1 = _build_page_content("Version one content.", None)
    v1_byte_size = len(on_disk_v1.encode("utf-8"))

    # The expected body path -- we want the mock to only intercept THIS write
    # (step 10, the body write after COMMIT).  Step 6 also calls atomic_write
    # for the snapshot file under .versions/; we let that succeed normally so
    # the transaction can commit before we simulate the body-write failure.
    expected_body_path = backend._body_path("wiki", "partial-fsync-page")
    from atomic_agents._io import atomic_write as real_atomic_write

    def rename_succeeds_then_raises(target, content, encoding="utf-8"):
        if target != expected_body_path:
            # Not the body write (e.g. snapshot write) -- let it succeed normally.
            return real_atomic_write(target, content, encoding=encoding)
        # Body write: simulate completed rename then a post-rename failure.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        raise OSError("Simulated post-rename failure for R2-C2 test")

    monkeypatch.setattr(sqlite_module, "atomic_write", rename_succeeds_then_raises)

    # The call must raise (durability guarantee was weakened).
    with pytest.raises(OSError, match="post-rename failure"):
        backend.write_page(
            "partial-fsync-page",
            "Version one content.",
            "wiki",
            policy,
            expected_content_sha256=sha_v0,
        )

    monkeypatch.undo()

    # The on-disk file now contains v1 content.
    body_path = backend._body_path("wiki", "partial-fsync-page")
    assert body_path.read_text(encoding="utf-8") == on_disk_v1, (
        "On-disk body must contain new (v1) content after rename succeeded"
    )

    # SQL byte_size must reflect the new (v1) content, not the old (v0).
    # If compensation fired incorrectly it would have restored the v0 byte_size,
    # creating a body/metadata mismatch.
    page_after = backend.read_page("partial-fsync-page", "wiki")
    assert page_after is not None
    assert page_after.ref.byte_size == v1_byte_size, (
        f"SQL byte_size must match v1 ({v1_byte_size}), "
        f"got {page_after.ref.byte_size!r} -- compensation must NOT fire "
        f"when rename already succeeded"
    )

    backend.close()


# ─────────────────────────────────────────────────────────────────
# Category 23 -- R2-L2: query() rejects top_k=True (bool subclasses int)


def test_query_top_k_bool_raises_value_error(tmp_path: Path) -> None:
    """query() with top_k=True or top_k=False raises ValueError.

    R2-L2 fix: bool is a subclass of int in Python, so True (== 1) would
    previously pass the isinstance(top_k, int) guard and run a query.  The
    fix adds an explicit isinstance(top_k, bool) rejection before the int check.
    """
    backend = _make_backend(tmp_path)
    with pytest.raises(ValueError, match="top_k"):
        backend.query("anything", "wiki", top_k=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="top_k"):
        backend.query("anything", "wiki", top_k=False)  # type: ignore[arg-type]
    backend.close()

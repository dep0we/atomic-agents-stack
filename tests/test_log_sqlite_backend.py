"""SQLite-specific tests for ``SQLiteLogBackend``.

The conformance suite (``test_log_protocol_conformance.py``) pins
behavior every backend MUST satisfy. These tests pin behavior unique
to the SQLite reference impl — schema creation, index usage, WAL
mode, multi-process safety, JSON1 extra-field aggregation, URL
parsing, multi-threaded safety. PR 4 of the arc forces the
conformance suite to parametrize across BOTH backends so spec/22's
"every backend satisfies the same contract" guarantee is verified.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atomic_agents.exceptions import BackendNotRegistered
from atomic_agents.logs import (
    LogAggregate,
    LogQuery,
    RunRecord,
    SQLiteLogBackend,
    get_default_log_backend,
    get_log_backend,
    list_log_backends,
    make_sqlite_backend_from_url,
)
from atomic_agents.logs.sqlite import _SCHEMA_VERSION


def _ts(year: int, month: int, day: int, hour: int = 12) -> str:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).isoformat()


def _make_record(
    *,
    ts: str | None = None,
    run_id: str = "r1",
    primitive: str = "agent_call",
    status: str = "ok",
    summary: str = "t",
    model: str = "m",
    input_tokens: int = 0,
    output_tokens: int = 0,
    **extras,
) -> RunRecord:
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return RunRecord(
        ts=ts,
        run_id=run_id,
        primitive=primitive,
        status=status,
        summary=summary,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        **extras,
    )


# ──────────────────────────────────────────────────────────────────
# Identity + construction


def test_backend_id_is_sqlite(tmp_path):
    backend = SQLiteLogBackend(tmp_path / "logs.db")
    assert backend.backend_id == "sqlite"


def test_in_memory_backend_construction():
    """`:memory:` opens a single shared connection for the backend's life."""
    backend = SQLiteLogBackend(":memory:")
    backend.append(_make_record(run_id="r1"))
    out = backend.tail(1)
    assert len(out) == 1 and out[0].run_id == "r1"


def test_creates_parent_dir_lazily(tmp_path):
    """Path under a non-existent dir is created on first append."""
    db_path = tmp_path / "nested" / "deep" / "logs.db"
    backend = SQLiteLogBackend(db_path)
    backend.append(_make_record())
    assert db_path.exists()


# ──────────────────────────────────────────────────────────────────
# Schema + indexes


def test_schema_created_on_first_append(tmp_path):
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())
    conn = sqlite3.connect(db)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "run_records" in tables
    assert "meta" in tables


def test_indexes_created(tmp_path):
    """Pin the index set so query plans match expectations."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())
    conn = sqlite3.connect(db)
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    # AUTOINCREMENT creates an internal index; only check the
    # named ones we explicitly create.
    assert "idx_ts" in indexes
    assert "idx_run_id" in indexes
    assert "idx_primitive" in indexes
    assert "idx_parent_run_id" in indexes
    assert "idx_cost_source" in indexes
    assert "idx_mandate_id" in indexes


def test_schema_version_recorded(tmp_path):
    """A fresh DB carries schema_version=3 in meta — pins the bump-on-change contract.

    v2 adds idempotency_key + replayed_run_id columns (spec/45 PR2).
    v3 adds conversation_id column (spec/47 PR1).
    """
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert int(row[0]) == _SCHEMA_VERSION


def test_schema_version_mismatch_raises(tmp_path):
    """Hand-corrupt the meta row; opening must raise."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())
    # Corrupt the version in a fresh connection.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    # New backend instance — must refuse to open a future-schema DB.
    with pytest.raises(RuntimeError, match="schema version"):
        SQLiteLogBackend(db).append(_make_record())


# ──────────────────────────────────────────────────────────────────
# spec/45 PR2 / spec/22 addendum — SQLite v1 → v2 migration ladder.
# Behavioral coverage of the ALTER/UPDATE/index-ordering migration (NOT
# string-presence on the constants): a real v1 DB is built on disk, opened
# by the backend, and the post-migration schema is asserted. Mirrors the
# Postgres mock-conn migration test (cross-backend parity, CLAUDE.md
# Principle 12 — verify before claim).

# v1 CREATE statement: the run_records table as it existed at schema_version=1,
# i.e. WITHOUT idempotency_key / replayed_run_id. Hand-rolled here so the test
# does not depend on the v2 constant (which already carries the new columns).
_V1_CREATE_RUN_RECORDS = """
CREATE TABLE run_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT NOT NULL,
    primitive TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL,
    cost_source TEXT,
    latency_ms REAL,
    cache_hit_tokens INTEGER,
    cache_miss_tokens INTEGER,
    mandate_id TEXT,
    parent_run_id TEXT,
    parent_agent TEXT,
    trigger TEXT,
    agent_name TEXT,
    fallback INTEGER,
    critical INTEGER,
    extra TEXT NOT NULL DEFAULT '{}'
)
"""


def _build_v1_db(db: Path, *, with_legacy_row: bool = True) -> None:
    """Create a schema_version=1 SQLite DB on disk (pre-idempotency-columns)."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(_V1_CREATE_RUN_RECORDS)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        if with_legacy_row:
            conn.execute(
                "INSERT INTO run_records "
                "(ts, run_id, primitive, status, summary, model, "
                " input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _ts(2026, 6, 1),
                    "legacy-run",
                    "agent_call",
                    "ok",
                    "pre-migration row",
                    "gpt-x",
                    10,
                    20,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _table_columns(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            str(r[1]) for r in conn.execute("PRAGMA table_info(run_records)").fetchall()
        }
    finally:
        conn.close()


def test_sqlite_v1_to_v3_migration_adds_columns_index_and_bumps_version(tmp_path):
    """Opening a real v1 DB MUST migrate it to v3 (v1→v2→v3): add idempotency
    columns + conversation_id column, create indexes, bump meta.schema_version to 3.
    The legacy row MUST survive with NULL for the new columns (no data loss).

    This is the behavioral coverage the CHANGELOG/CLAUDE.md 'SQLite v1→v2→v3
    migration' claim asserts; without it the claim is unverified (the static
    constant tests do not exercise the ALTER ladder).

    Note: The migration runs v1→v2 then v2→v3 in sequence, so after opening a
    v1 DB the version is 3 (the current _SCHEMA_VERSION). The individual migration
    steps are tested in test_sqlite_v1_to_v3_migration_resumes_after_partial_crash."""
    db = tmp_path / "logs.db"
    _build_v1_db(db, with_legacy_row=True)
    # v1 precondition: neither v2 nor v3 columns exist yet.
    assert "idempotency_key" not in _table_columns(db)
    assert "replayed_run_id" not in _table_columns(db)
    assert "conversation_id" not in _table_columns(db)

    # Open the backend — this triggers _ensure_schema()'s v1→v2→v3 ladder.
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())  # forces _get_conn()/_ensure_schema()

    cols = _table_columns(db)
    assert "idempotency_key" in cols, "migration MUST add idempotency_key column"
    assert "replayed_run_id" in cols, "migration MUST add replayed_run_id column"
    assert "conversation_id" in cols, (
        "migration MUST add conversation_id column (spec/47 PR1)"
    )

    conn = sqlite3.connect(db)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert int(version) == _SCHEMA_VERSION, (
            "migration MUST bump schema_version to current"
        )
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_idempotency_key" in indexes, (
            "migration MUST create idx_idempotency_key (spec/22 addendum)"
        )
        assert "idx_conversation_id" in indexes, (
            "migration MUST create idx_conversation_id (spec/47 PR1 / spec/22 addendum)"
        )
        # Legacy row survives, with NULL for the new columns.
        legacy = conn.execute(
            "SELECT idempotency_key, replayed_run_id, conversation_id FROM run_records "
            "WHERE run_id = 'legacy-run'"
        ).fetchone()
        assert legacy == (None, None, None), (
            "legacy row MUST survive migration with NULL new fields"
        )
    finally:
        conn.close()


def test_sqlite_v1_to_v3_migration_resumes_after_partial_crash(tmp_path):
    """CRASH/RETRY SAFETY: if a prior migration died AFTER the first ALTER
    committed but BEFORE the meta UPDATE (schema_version still 1, one column
    already present), reopening MUST complete the migration WITHOUT raising
    'duplicate column name'. Pins the PRAGMA-guarded ALTER fix.

    NEGATIVE CONTROL: with a bare (unguarded) ALTER the second open raises
    sqlite3.OperationalError('duplicate column name idempotency_key') — verified
    by manually adding only the first column below, which simulates the exact
    partial-migration state.

    After completion, schema_version is 3 (current _SCHEMA_VERSION), not 2,
    because the v2→v3 migration also runs."""
    db = tmp_path / "logs.db"
    _build_v1_db(db, with_legacy_row=True)

    # Simulate a partial v1→v2 migration: first ALTER committed, meta still v1.
    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE run_records ADD COLUMN idempotency_key TEXT")
        conn.commit()
    finally:
        conn.close()
    cols = _table_columns(db)
    assert "idempotency_key" in cols and "replayed_run_id" not in cols, (
        "precondition: exactly one of the two columns present (partial state)"
    )

    # Reopen — the guarded migration MUST skip the already-present column,
    # add the missing ones, and bump to 3 without raising.
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())  # must NOT raise duplicate-column

    cols = _table_columns(db)
    assert "idempotency_key" in cols and "replayed_run_id" in cols
    assert "conversation_id" in cols, (
        "v2→v3 migration also runs (conversation_id added)"
    )
    conn = sqlite3.connect(db)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert int(version) == _SCHEMA_VERSION  # v1→v2→v3 both ran
    finally:
        conn.close()


def _build_v2_db(db: Path, *, with_legacy_row: bool = True) -> None:
    """Create a schema_version=2 SQLite DB on disk (post-idempotency, pre-conversation).

    Mirrors the on-disk state a real backend leaves after the v1→v2 migration:
    run_records has idempotency_key + replayed_run_id columns, idx_idempotency_key
    exists, and meta.schema_version == '2'. conversation_id does NOT yet exist.
    """
    conn = sqlite3.connect(db)
    try:
        conn.execute(_V1_CREATE_RUN_RECORDS)
        conn.execute("ALTER TABLE run_records ADD COLUMN idempotency_key TEXT")
        conn.execute("ALTER TABLE run_records ADD COLUMN replayed_run_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_key "
            "ON run_records (idempotency_key)"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
        if with_legacy_row:
            conn.execute(
                "INSERT INTO run_records "
                "(ts, run_id, primitive, status, summary, model, "
                " input_tokens, output_tokens, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _ts(2026, 6, 1),
                    "v2-legacy-run",
                    "agent_call",
                    "ok",
                    "pre-v3 row with idempotency_key set",
                    "gpt-x",
                    10,
                    20,
                    "idem-key-preexisting",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_sqlite_v2_to_v3_migration_gate_isolation(tmp_path):
    """BEHAVIORAL coverage of the SQLite v2→v3 migration step in ISOLATION
    (spec/47 PR1), seeded at EXACTLY schema_version=2 — the cross-backend parity
    companion to test_postgres_ensure_schema_v2_to_v3_migration_ladder_order.

    A v1-seeded ladder test walks BOTH steps, so a regression that mis-gated the
    v2→v3 block (e.g. `if existing == 1` instead of `== 2` in sqlite.py) could
    still pass there (a v1 DB runs both steps regardless of the second gate's
    literal). This isolates the v2→v3 entry point on the backend where it runs
    LOCALLY: open a real v2 DB and assert it:
      (a) ADDs the conversation_id column + idx_conversation_id index,
      (b) bumps meta.schema_version to 3,
      (c) does NOT re-fire the v1→v2 ALTERs — the pre-existing idempotency_key
          VALUE on the legacy row survives untouched (no column re-add / reset).

    NEGATIVE CONTROL: if sqlite.py's v2→v3 gate is mis-changed to `if existing
    == 1`, opening this v2-seeded DB leaves schema_version at 2 and never adds
    conversation_id, so assertions (a)/(b) FAIL. (A v1-seeded test would NOT
    catch that regression — that is the gap this test closes.)"""
    db = tmp_path / "logs.db"
    _build_v2_db(db, with_legacy_row=True)
    # v2 precondition: idempotency columns present, conversation_id absent.
    cols0 = _table_columns(db)
    assert "idempotency_key" in cols0 and "replayed_run_id" in cols0
    assert "conversation_id" not in cols0

    # Open the backend — triggers _ensure_schema() starting from existing==2.
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())  # forces _get_conn()/_ensure_schema()

    cols = _table_columns(db)
    # (a) conversation_id column + index added.
    assert "conversation_id" in cols, (
        "v2→v3 migration MUST add conversation_id (proves the == 2 gate fired)"
    )
    conn = sqlite3.connect(db)
    try:
        version = int(
            conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        # (b) bumped to current.
        assert version == _SCHEMA_VERSION, (
            "v2→v3 migration MUST bump schema_version to current "
            "(would stay 2 if the gate were mis-changed to == 1)"
        )
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_conversation_id" in indexes
        # (c) gate isolation: the v1→v2 block did NOT re-fire — the pre-existing
        # idempotency_key VALUE on the legacy row is untouched (a re-run of the
        # v1→v2 ALTER path does not reset values, but a mis-gated full re-run is
        # the failure class this guards; assert the value persisted unchanged).
        legacy = conn.execute(
            "SELECT idempotency_key, conversation_id FROM run_records "
            "WHERE run_id = 'v2-legacy-run'"
        ).fetchone()
        assert legacy == ("idem-key-preexisting", None), (
            "v2-seeded migration MUST preserve the pre-existing idempotency_key "
            "value and leave conversation_id NULL for the legacy row"
        )
    finally:
        conn.close()


def test_wal_journal_mode_enabled(tmp_path):
    """WAL mode is the load-bearing concurrent-read property — pin it."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())
    # Read mode from a separate connection.
    conn = sqlite3.connect(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_busy_timeout_set_before_wal_pragma(tmp_path):
    """#208: ``PRAGMA busy_timeout=5000`` MUST be set before the WAL
    pragma so the cold-start WAL-transition race waits instead of
    raising ``OperationalError: database is locked``. Same shape as
    the parallel #64 PR 3 fix in ``atomic_agents/registry/sqlite.py``.

    The pre-existing ``test_concurrent_appends_from_threads`` flake
    (~80% local-macOS fail rate) was the same race — multiple threads
    open per-thread connections to the same fresh db and race the
    WAL transition; without busy_timeout, the losers hard-fail.
    """
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    # Trigger _get_conn for this thread.
    backend.append(_make_record())
    conn = backend._get_conn()
    timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms == 5000, (
        f"busy_timeout must be 5000ms (got {timeout_ms}) — without it, "
        f"concurrent threads/processes opening a fresh db race the WAL "
        f"transition and the losers raise OperationalError"
    )


def test_concurrent_appends_no_wal_race_under_repeated_runs(tmp_path):
    """#208 regression: repeat the concurrent-append scenario 10 times
    against a fresh db each iteration. With busy_timeout=5000 set
    BEFORE the WAL pragma, all 10 iterations MUST succeed without
    ``sqlite3.OperationalError`` from the cold-start WAL race.

    Pre-fix: ~80% fail rate on macOS for a single run. Post-fix:
    10/10 success.
    """
    for iteration in range(10):
        db = tmp_path / f"logs_{iteration}.db"
        backend = SQLiteLogBackend(db)

        def worker(thread_id: int):
            for i in range(10):
                backend.append(
                    _make_record(
                        run_id=f"i{iteration}-t{thread_id}-r{i}",
                        ts=_ts(2026, 5, 15, thread_id),
                    )
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = backend.stats()
        assert stats.total_records == 40, (
            f"iteration {iteration}: expected 40 records (4 threads × 10), "
            f"got {stats.total_records} — WAL race likely dropped writes"
        )


# ──────────────────────────────────────────────────────────────────
# Round-trip fidelity


def test_round_trip_preserves_all_fields(tmp_path):
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    rec = RunRecord(
        ts=_ts(2026, 5, 15, 10),
        run_id="r1",
        primitive="agent_call",
        status="ok",
        summary="round-trip",
        model="claude-opus-4-7",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        cost_source="actor",
        latency_ms=1234.5,
        cache_hit_tokens=20,
        cache_miss_tokens=80,
        mandate_id="m-1",
        parent_run_id="parent",
        parent_agent="boss",
        trigger="agent_call",
        agent_name="alice",
        fallback=True,
        critical=False,
        extra={"iteration": 3, "tool_calls": [{"tool_name": "search"}]},
    )
    backend.append(rec)
    out = backend.tail(1)[0]
    assert out.run_id == "r1"
    assert out.primitive == "agent_call"
    assert out.cost_source == "actor"
    assert out.mandate_id == "m-1"
    assert out.parent_run_id == "parent"
    assert out.parent_agent == "boss"
    assert out.trigger == "agent_call"
    assert out.agent_name == "alice"
    assert out.fallback is True
    assert out.critical is False
    assert out.cache_hit_tokens == 20
    assert out.cache_miss_tokens == 80
    assert out.latency_ms == pytest.approx(1234.5)
    assert out.extra["iteration"] == 3
    assert out.extra["tool_calls"][0]["tool_name"] == "search"


def test_extra_field_stored_as_json(tmp_path):
    """``extra`` MUST be JSON-text in storage — exposes it to SQL JSON1."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record(extra={"foo": "bar", "n": 42}))
    conn = sqlite3.connect(db)
    raw = conn.execute("SELECT extra FROM run_records").fetchone()[0]
    parsed = json.loads(raw)
    assert parsed == {"foo": "bar", "n": 42}


# ──────────────────────────────────────────────────────────────────
# Aggregation pushdown via JSON1


def test_aggregate_group_by_extra_field_via_json1(tmp_path):
    """SQL JSON1 ``json_extract`` powers extra-field group_by.

    The conformance suite has a filesystem-only variant of this; here
    we verify SQLite specifically routes to JSON1 (the SQL backend's
    pushdown contract per spec/22).
    """
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(
        _make_record(
            run_id="r1",
            primitive="outcome_iteration",
            ts=_ts(2026, 5, 15, 10),
            extra={"iteration": 0},
        )
    )
    backend.append(
        _make_record(
            run_id="r2",
            primitive="outcome_iteration",
            ts=_ts(2026, 5, 15, 11),
            extra={"iteration": 1},
        )
    )
    backend.append(
        _make_record(
            run_id="r3",
            primitive="outcome_iteration",
            ts=_ts(2026, 5, 15, 12),
            extra={"iteration": 1},
        )
    )
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("iteration",), metric="count"),
    )
    assert result == {(0,): 1, (1,): 2}


def test_aggregate_group_by_invalid_identifier_raises(tmp_path):
    """Reject malicious group_by names (SQL injection guard)."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())
    # SQL-injection attempt via the JSON1 path component.
    with pytest.raises(ValueError, match="not a valid identifier"):
        backend.aggregate(
            LogQuery(),
            LogAggregate(group_by=("'; DROP TABLE run_records; --",), metric="count"),
        )


def test_aggregation_pushdown_capability_advertised(tmp_path):
    """SQLite MUST advertise pushdown=True; FilesystemLogBackend = False."""
    backend = SQLiteLogBackend(tmp_path / "logs.db")
    caps = backend.capabilities()
    assert caps.supports_aggregation_pushdown is True
    assert caps.supports_retention is True
    assert caps.durable is True


# ──────────────────────────────────────────────────────────────────
# Query — SQL pushdown


def test_query_uses_index_for_run_id_lookup(tmp_path):
    """Pin that run_id queries use idx_run_id (via EXPLAIN QUERY PLAN)."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record(run_id="r1"))
    conn = sqlite3.connect(db)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM run_records WHERE run_id = ?",
        ("r1",),
    ).fetchall()
    plan_text = " ".join(str(row) for row in plan).lower()
    assert "idx_run_id" in plan_text


def test_query_cost_source_actor_includes_legacy_null(tmp_path):
    """Backward-compat: records without cost_source count as 'actor'."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(
        _make_record(
            run_id="actor1",
            cost_source="actor",
            ts=_ts(2026, 5, 15, 10),
        )
    )
    backend.append(
        _make_record(
            run_id="judge1",
            cost_source="judge",
            ts=_ts(2026, 5, 15, 11),
        )
    )
    backend.append(
        _make_record(
            run_id="legacy",
            cost_source=None,
            ts=_ts(2026, 5, 15, 12),
        )
    )
    out = backend.query(LogQuery(cost_source="actor"))
    assert {r.run_id for r in out} == {"actor1", "legacy"}


# ──────────────────────────────────────────────────────────────────
# Retention


def test_delete_older_than_pushes_to_sql(tmp_path):
    """DELETE WHERE ts < :threshold — index-driven, returns row count."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record(ts=_ts(2026, 1, 15), run_id="jan"))
    backend.append(_make_record(ts=_ts(2026, 3, 15), run_id="mar"))
    backend.append(_make_record(ts=_ts(2026, 5, 15), run_id="may"))
    deleted = backend.delete_older_than(datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert deleted == 2
    remaining = backend.query(LogQuery())
    assert {r.run_id for r in remaining} == {"may"}


# ──────────────────────────────────────────────────────────────────
# Thread safety


def test_concurrent_appends_from_threads(tmp_path):
    """Per-thread connections + WAL → concurrent threads can write safely."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)

    def worker(thread_id: int):
        for i in range(10):
            backend.append(
                _make_record(
                    run_id=f"t{thread_id}-r{i}",
                    ts=_ts(2026, 5, 15, thread_id),
                )
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 threads × 10 records each = 40 total.
    stats = backend.stats()
    assert stats.total_records == 40


def test_cold_start_race_idempotent_schema_init(tmp_path):
    """Simulating two backends initializing a fresh db concurrently must
    NOT raise UNIQUE-constraint on schema_version (Step 11 P0 #2).

    The previous SELECT-then-INSERT pattern would fail on the second
    process. INSERT OR IGNORE makes it idempotent — losing the race
    is a no-op, both processes converge to a working backend.
    """
    db = tmp_path / "shared.db"
    # Open two backends pointing at the same fresh db file. The
    # second instance's _ensure_schema must not raise.
    backend_a = SQLiteLogBackend(db)
    backend_b = SQLiteLogBackend(db)
    backend_a.append(_make_record(run_id="from_a"))
    backend_b.append(_make_record(run_id="from_b"))
    # Both writes land in the same db (verify via a third connection).
    backend_c = SQLiteLogBackend(db)
    out = backend_c.query(LogQuery())
    assert {r.run_id for r in out} == {"from_a", "from_b"}


def test_reopen_existing_populated_db(tmp_path):
    """A second SQLiteLogBackend instance reads records written by the
    first. The load-bearing multi-process / multi-instance property."""
    db = tmp_path / "logs.db"
    backend1 = SQLiteLogBackend(db)
    for i in range(3):
        backend1.append(_make_record(run_id=f"r{i}"))
    # Reopen — fresh backend instance against the same file.
    backend2 = SQLiteLogBackend(db)
    out = backend2.tail(10)
    assert {r.run_id for r in out} == {"r0", "r1", "r2"}


def test_url_rejects_netloc(tmp_path):
    """Step 11 P2: ``sqlite://host/path`` is ambiguous; must raise."""
    with pytest.raises(ValueError, match="ambiguous"):
        make_sqlite_backend_from_url("sqlite://host/path")


def test_url_in_memory_three_slash_form(tmp_path):
    """SQLAlchemy-convention ``sqlite:///:memory:`` works alongside ``sqlite::memory:``."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        backend = make_sqlite_backend_from_url("sqlite:///:memory:")
    backend.append(_make_record())
    assert len(backend.tail(1)) == 1


def test_in_memory_construction_warns(tmp_path):
    """Step 11 security #4: :memory: emits a data-loss warning."""
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        make_sqlite_backend_from_url("sqlite::memory:")
    assert any("non-persistent" in str(w.message) for w in caught), (
        "Expected RuntimeWarning about in-memory non-persistence"
    )


def test_canonical_columns_derived_from_run_record(tmp_path):
    """`_CANONICAL_COLUMNS` MUST stay in sync with `RunRecord.__dataclass_fields__`.

    Step 11 P2 mitigation: the manual frozenset was a drift hazard;
    derive-from-dataclass eliminates it. Pin via assertion that a new
    field on RunRecord would surface in _CANONICAL_COLUMNS.
    """
    from atomic_agents.logs.sqlite import _CANONICAL_COLUMNS

    expected = frozenset(
        name for name in RunRecord.__dataclass_fields__ if name != "extra"
    )
    assert _CANONICAL_COLUMNS == expected


def test_row_to_record_preserves_empty_required_strings(tmp_path):
    """`_row_to_record` MUST NOT clobber empty-string required fields
    via the `or DEFAULT` idiom (Step 11 P2). Round-trips `model=""`
    as `model=""`, not `model="n/a"`."""
    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(
        _make_record(
            model="",  # empty string; not None
            summary="",
            run_id="empty_strings",
        )
    )
    out = backend.tail(1)
    assert out[0].model == ""
    assert out[0].summary == ""
    assert out[0].run_id == "empty_strings"


# ──────────────────────────────────────────────────────────────────
# URL parsing


def test_make_sqlite_backend_from_url_absolute_path(tmp_path):
    """sqlite:///<absolute> parses to the absolute path."""
    db = tmp_path / "abs.db"
    backend = make_sqlite_backend_from_url(f"sqlite:///{db}")
    backend.append(_make_record())
    assert db.exists()


def test_make_sqlite_backend_from_url_memory():
    """sqlite::memory: shortcut produces the in-memory backend."""
    backend = make_sqlite_backend_from_url("sqlite::memory:")
    backend.append(_make_record())
    out = backend.tail(1)
    assert len(out) == 1


def test_make_sqlite_backend_from_url_rejects_non_sqlite_scheme():
    with pytest.raises(ValueError, match="sqlite://"):
        make_sqlite_backend_from_url("postgres://host/db")


def test_make_sqlite_backend_from_url_rejects_empty_path():
    with pytest.raises(ValueError, match="no path component"):
        make_sqlite_backend_from_url("sqlite:///")


# ──────────────────────────────────────────────────────────────────
# Registry resolution


def test_registry_resolves_sqlite():
    assert get_log_backend("sqlite") is SQLiteLogBackend
    assert "sqlite" in list_log_backends()


def test_get_default_log_backend_sqlite_with_url(tmp_path, monkeypatch):
    """Operator sets sqlite + URL → SQLiteLogBackend constructed."""
    db = tmp_path / "operator.db"
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "sqlite")
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND_URL", f"sqlite:///{db}")
    backend = get_default_log_backend(tmp_path / "scope")
    assert isinstance(backend, SQLiteLogBackend)


def test_get_default_log_backend_sqlite_without_url_uses_scope_root(
    tmp_path, monkeypatch
):
    """No URL → default db path under scope_root (``<scope>/.logs.db``)."""
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "sqlite")
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND_URL", raising=False)
    backend = get_default_log_backend(tmp_path)
    assert isinstance(backend, SQLiteLogBackend)
    backend.append(_make_record())
    assert (tmp_path / ".logs.db").exists()

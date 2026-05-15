"""SQLiteLogBackend — stdlib ``sqlite3`` reference implementation.

This is the canonical queryable backend in the protocol-pattern series.
No optional dependency required — ``sqlite3`` ships with CPython. The
backend's value over ``FilesystemLogBackend``:

* **Index-driven query/tail/aggregate** — SELECT with WHERE/ORDER/LIMIT
  pushes down to native SQL primitives. Dashboard renders on a year of
  history go from O(N) JSONL parse to O(log N) index seek.
* **Atomic appends across record sizes** — SQL INSERT inside a single
  transaction has no PIPE_BUF bound. Records carrying ``helper_provenance
  + delegations + tool_calls`` rollup arrays (routinely >4 KB on the
  top-level ``agent.call()`` write) get atomicity that ``atomic_append_
  jsonl`` cannot guarantee on shared NFS / multi-process hosts.
* **Indexed retention** — ``delete_older_than`` becomes
  ``DELETE WHERE ts < :threshold`` (index seek + bulk delete), bounded
  to milliseconds even on multi-year histories.

Storage shape:

* One SQLite file at the path passed to the constructor. Default
  ``ATOMIC_AGENTS_LOG_BACKEND_URL=sqlite:///path/to/logs.db`` parsing.
* One table ``run_records`` matching the canonical ``RunRecord``
  schema with primitive-specific extras stored as a JSON-text column.
* Indexes on (ts, run_id, primitive, parent_run_id) for the predicate
  patterns the dashboard + cost-guardrail readers use.
* WAL journal mode for concurrent reader/writer interleaving (the
  single-process default ``DELETE`` journal would serialize writes
  against any concurrent SELECT — unacceptable for the dashboard
  reading-while-call-writes case).

Thread-safety: a ``threading.local`` connection pool gives each thread
its own ``sqlite3.Connection`` — sqlite3 connections aren't shared
across threads by default. ``check_same_thread=False`` would allow
sharing but with locking that serializes all writes; per-thread
connections plus WAL journaling is the standard pattern.

Concurrent multi-process append: WAL mode supports it. Multiple
``SQLiteLogBackend`` instances pointing at the same db file from
different processes will see consistent reads + serialized writes.
This is the load-bearing property for the "operator pins SQLite on
Cloud Run with N replicas" deployment shape that motivates #61.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .types import (
    LogAggregate,
    LogCapabilities,
    LogQuery,
    LogStats,
    METRIC_AVG_LATENCY_MS,
    METRIC_COUNT,
    METRIC_SUM_COST_USD,
    METRIC_SUM_INPUT_TOKENS,
    METRIC_SUM_OUTPUT_TOKENS,
    RunRecord,
    VALID_METRICS,
)


# Schema version — bumped on any breaking schema change. The
# ``schema_version`` row in the ``meta`` table records the version
# the file was created with; mismatches at open time raise. PR 3
# ships ``v1``; future PRs that add columns bump to ``v2`` + provide
# an upgrade migration via ``ALTER TABLE`` inside ``_ensure_schema``.
_SCHEMA_VERSION = 1


# SQL for schema creation. Idempotent — ``CREATE TABLE IF NOT EXISTS``
# means re-opening an existing file is a no-op. Column order matches
# the ``RunRecord`` dataclass field order for readability when
# debugging via ``sqlite3 logs.db .schema``.
_CREATE_RUN_RECORDS = """
CREATE TABLE IF NOT EXISTS run_records (
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

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# Indexes on the predicates the dashboard + cost-guardrail readers
# use. Don't index ``model`` (high cardinality, low selectivity) or
# ``status`` (3-4 distinct values dominate); the ts index serves
# range scans for most queries.
_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ts ON run_records(ts)",
    "CREATE INDEX IF NOT EXISTS idx_run_id ON run_records(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_primitive ON run_records(primitive)",
    "CREATE INDEX IF NOT EXISTS idx_parent_run_id ON run_records(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_cost_source ON run_records(cost_source)",
    "CREATE INDEX IF NOT EXISTS idx_mandate_id ON run_records(mandate_id)",
]


# Canonical RunRecord field names in INSERT order (matches the
# CREATE TABLE column order minus ``id``).
_INSERT_COLUMNS = (
    "ts", "run_id", "primitive", "status", "summary", "model",
    "input_tokens", "output_tokens", "cost_usd", "cost_source",
    "latency_ms", "cache_hit_tokens", "cache_miss_tokens",
    "mandate_id", "parent_run_id", "parent_agent", "trigger",
    "agent_name", "fallback", "critical", "extra",
)


class SQLiteLogBackend:
    """SQLite-backed LogBackend — queryable, indexed, multi-process-safe.

    Conforms to the ``LogBackend`` Protocol. Constructed with the path
    to a SQLite database file; the schema is created on first ``append()``
    (lazy — pure-read use cases don't materialize the file).

    Args:
        db_path: filesystem path to the SQLite database. Parent
            directory must exist (created lazily on first write via
            ``mkdir(parents=True, exist_ok=True)``). Use ``":memory:"``
            for in-memory transient state — useful for tests; the
            in-memory case is single-process by definition.

    Thread-safety: each thread gets its own ``sqlite3.Connection`` via
    ``threading.local``. Cross-thread reads/writes are safe. WAL mode
    means readers don't block writers and vice versa.

    Multi-process: pointing two ``SQLiteLogBackend`` instances at the
    same db file from different processes is safe with WAL — writes
    serialize via SQLite's internal locking, reads run concurrent
    against an MVCC snapshot.
    """

    @property
    def backend_id(self) -> str:
        return "sqlite"

    def __init__(self, db_path: Path | str) -> None:
        # Accept ":memory:" verbatim — the special in-memory sentinel
        # is a string, not a real Path. Path conversion would mangle it.
        if db_path == ":memory:":
            self._db_path_str = ":memory:"
            self._in_memory = True
        else:
            self._db_path = Path(db_path)
            self._db_path_str = str(self._db_path)
            self._in_memory = False
        # Per-thread connection storage. ``threading.local`` ensures
        # each thread sees its own connection; cross-thread sharing
        # of sqlite3 Connection objects is not safe by default.
        self._tls = threading.local()
        # In-memory databases die when the connection closes. Pin a
        # single connection for the lifetime of the backend so the
        # state survives across calls. This is the standard ":memory:"
        # SQLite idiom — pay the lock-serialization cost in tests for
        # the convenience of zero on-disk state.
        if self._in_memory:
            self._shared_conn = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            # Row factory — caller-friendly column access by name.
            # Matches the on-disk path's setting; without it,
            # _row_to_record's ``row["extra"]`` access fails.
            self._shared_conn.row_factory = sqlite3.Row
            self._ensure_schema(self._shared_conn)
        else:
            self._shared_conn = None

    # ────────────────────────────────────────────────────────────
    # Connection management

    def _get_conn(self) -> sqlite3.Connection:
        """Return the calling thread's connection — lazy-create + WAL setup."""
        if self._in_memory:
            assert self._shared_conn is not None
            return self._shared_conn

        conn = getattr(self._tls, "conn", None)
        if conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path_str)
            # Row factory — caller-friendly column access by name.
            conn.row_factory = sqlite3.Row
            # WAL journal mode: concurrent readers don't block writers.
            # ``synchronous=NORMAL`` trades a tiny power-loss window
            # for ~3x write throughput vs ``FULL`` — same trade-off
            # Postgres ``synchronous_commit=local`` makes. Acceptable
            # for log data (the framework's audit trail is structural
            # but not transactional).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema(conn)
            self._tls.conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables + indexes if missing; assert schema version."""
        conn.execute(_CREATE_RUN_RECORDS)
        conn.execute(_CREATE_META)
        for stmt in _CREATE_INDEXES:
            conn.execute(stmt)
        # Schema version: insert-if-missing, validate-if-present.
        cur = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
        else:
            existing = int(row[0])
            if existing != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"SQLiteLogBackend schema version mismatch: file has "
                    f"v{existing}, code expects v{_SCHEMA_VERSION}. "
                    f"A backward-incompatible schema bump shipped without "
                    f"a migration. Open an issue at "
                    f"https://github.com/dep0we/atomic-agents-stack/issues "
                    f"with this error."
                )
        conn.commit()

    # ────────────────────────────────────────────────────────────
    # Append

    def append(self, record: RunRecord) -> None:
        """INSERT one ``RunRecord`` row.

        Atomic via single-statement transaction. No PIPE_BUF bound —
        records of any size land atomically (the load-bearing
        improvement over ``FilesystemLogBackend`` for the rollup-array
        records on agent.call()).
        """
        conn = self._get_conn()
        values = (
            record.ts,
            record.run_id,
            record.primitive,
            record.status,
            record.summary,
            record.model,
            record.input_tokens,
            record.output_tokens,
            record.cost_usd,
            record.cost_source,
            record.latency_ms,
            record.cache_hit_tokens,
            record.cache_miss_tokens,
            record.mandate_id,
            record.parent_run_id,
            record.parent_agent,
            record.trigger,
            record.agent_name,
            None if record.fallback is None else int(record.fallback),
            None if record.critical is None else int(record.critical),
            json.dumps(record.extra) if record.extra else "{}",
        )
        placeholders = ", ".join("?" * len(values))
        columns = ", ".join(_INSERT_COLUMNS)
        conn.execute(
            f"INSERT INTO run_records ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()

    # ────────────────────────────────────────────────────────────
    # Query

    def query(self, filter: LogQuery) -> list[RunRecord]:
        """Push every predicate down to SQL WHERE; ORDER BY ts; LIMIT.

        ISO-8601 lexicographic comparison on ``ts`` matches chronological
        order for tz-aware records — the same invariant the filesystem
        backend relies on. SQLite's TEXT comparison is byte-wise, which
        agrees with Python's string comparison for ASCII ISO-8601.
        """
        sql, params = self._build_query_sql(filter, select="*", order_limit=True)
        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    # ────────────────────────────────────────────────────────────
    # Tail

    def tail(self, n: int) -> list[RunRecord]:
        """SELECT ORDER BY ts DESC LIMIT n — index seek bounded to ``n`` rows."""
        if n < 0:
            raise ValueError(f"tail(n) requires n >= 0; got {n}")
        if n == 0:
            return []
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM run_records ORDER BY ts DESC, id DESC LIMIT ?",
            (n,),
        ).fetchall()
        # Reverse to chronological-LAST order.
        records = [self._row_to_record(row) for row in rows]
        records.reverse()
        return records

    # ────────────────────────────────────────────────────────────
    # Aggregate — pushed down to SQL GROUP BY

    def aggregate(
        self,
        filter: LogQuery,
        agg: LogAggregate,
    ) -> dict[tuple, float | int]:
        """Compute the metric grouped by ``agg.group_by`` via SQL GROUP BY.

        For ``group_by`` field names that match canonical ``RunRecord``
        columns (a SQL-named column), uses the column directly. For
        ``extra``-resolved field names, uses ``json_extract(extra, '$.NAME')``
        — the SQL JSON1 extension shipped with SQLite for >a decade.
        Backends without JSON1 SHOULD raise ``NotImplementedError``
        per spec/22 §"Aggregation pushdown" — sqlite3 in CPython 3.11+
        always has JSON1.
        """
        if agg.metric not in VALID_METRICS:
            raise ValueError(
                f"Unknown aggregate metric: {agg.metric!r}. "
                f"Valid metrics: {sorted(VALID_METRICS)}"
            )

        # Resolve each group_by field name to a SQL expression. Canonical
        # column names are passed through; unknown names route through
        # JSON1 ``json_extract``. SQL injection risk: ``json_extract``'s
        # path arg is a string literal in the SQL we build — we sanitize
        # the column name against an allowlist + JSON-path-safe charset.
        group_exprs: list[str] = []
        for col in agg.group_by:
            if col in _CANONICAL_COLUMNS:
                group_exprs.append(col)
            else:
                # JSON-extract path. Strict charset: alphanumeric +
                # underscore — matches Python attribute naming, refuses
                # anything else.
                if not col.replace("_", "").isalnum():
                    raise ValueError(
                        f"aggregate group_by field {col!r} is not a "
                        f"valid identifier (alphanumeric + underscore only)"
                    )
                group_exprs.append(f"json_extract(extra, '$.{col}')")

        metric_expr = _metric_to_sql(agg.metric)

        where_sql, params = self._build_query_sql(
            filter, select="", order_limit=False, where_only=True
        )

        if group_exprs:
            group_clause = "GROUP BY " + ", ".join(group_exprs)
            select_cols = ", ".join(group_exprs) + ", " + metric_expr
        else:
            group_clause = ""
            select_cols = metric_expr

        sql = (
            f"SELECT {select_cols} FROM run_records "
            f"{where_sql} {group_clause}"
        )
        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()

        result: dict[tuple, float | int] = {}
        for row in rows:
            if group_exprs:
                key = tuple(row[i] for i in range(len(group_exprs)))
                value = row[len(group_exprs)]
            else:
                key = ()
                value = row[0]
            # avg_latency_ms over an all-None bucket: SQL AVG returns
            # NULL when the column is fully NULL. Surface as Python
            # None (matches FilesystemLogBackend spec/22 contract).
            if agg.metric == METRIC_AVG_LATENCY_MS:
                result[key] = value if value is not None else None  # type: ignore[assignment]
            elif agg.metric == METRIC_COUNT:
                result[key] = int(value or 0)
            elif agg.metric in (METRIC_SUM_INPUT_TOKENS, METRIC_SUM_OUTPUT_TOKENS):
                result[key] = int(value or 0)
            else:  # SUM_COST_USD
                result[key] = float(value or 0.0)
        return result

    # ────────────────────────────────────────────────────────────
    # Retention

    def delete_older_than(self, threshold: datetime) -> int:
        """DELETE WHERE ts < :threshold — bounded by the ts index.

        Raises ``ValueError`` on naive datetimes per the spec/22
        contract — silent local-vs-UTC conversion is the off-by-one-
        day retention failure shape the PR 1 Step 11 adversarial caught.
        """
        if threshold.tzinfo is None:
            raise ValueError(
                "delete_older_than(threshold) requires a tz-aware "
                "datetime; naive datetime would silently convert "
                "local-vs-UTC and corrupt retention near midnight"
            )
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM run_records WHERE ts < ?",
            (threshold.isoformat(),),
        )
        conn.commit()
        return cur.rowcount

    # ────────────────────────────────────────────────────────────
    # Stats

    def stats(self) -> LogStats:
        """COUNT(*) + MIN/MAX(ts) — O(1) with the ts index."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS total, MIN(ts) AS oldest, MAX(ts) AS newest "
            "FROM run_records"
        ).fetchone()
        total = int(row["total"] or 0)
        oldest_ts = row["oldest"]
        newest_ts = row["newest"]

        # records_today / records_this_month — local-tz day window.
        # Compute the window in Python and pass as parameters; SQLite
        # can use the ts index for the range.
        from datetime import date, time as dt_time
        today = date.today()
        today_start = datetime.combine(today, dt_time.min).astimezone().isoformat()
        today_end = datetime.combine(today, dt_time.max).astimezone().isoformat()
        first_of_month = today.replace(day=1)
        month_start = datetime.combine(first_of_month, dt_time.min).astimezone().isoformat()

        records_today = int(conn.execute(
            "SELECT COUNT(*) FROM run_records WHERE ts >= ? AND ts <= ?",
            (today_start, today_end),
        ).fetchone()[0] or 0)
        records_this_month = int(conn.execute(
            "SELECT COUNT(*) FROM run_records WHERE ts >= ? AND ts <= ?",
            (month_start, today_end),
        ).fetchone()[0] or 0)

        size_bytes: int | None
        if self._in_memory:
            size_bytes = None
        else:
            try:
                size_bytes = self._db_path.stat().st_size
            except OSError:
                size_bytes = None

        return LogStats(
            total_records=total,
            oldest_ts=oldest_ts,
            newest_ts=newest_ts,
            size_bytes=size_bytes,
            records_today=records_today,
            records_this_month=records_this_month,
        )

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> LogCapabilities:
        return LogCapabilities(
            supports_aggregation_pushdown=True,
            supports_streaming=False,
            supports_retention=True,
            durable=True,
        )

    # ────────────────────────────────────────────────────────────
    # Internal SQL builders

    def _build_query_sql(
        self,
        filter: LogQuery,
        select: str = "*",
        order_limit: bool = True,
        where_only: bool = False,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause + parameter list for a LogQuery.

        Returns ``(full_sql, params)``. When ``where_only=True`` the
        caller assembles the full statement around the returned WHERE
        text.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if filter.run_id is not None:
            clauses.append("run_id = ?")
            params.append(filter.run_id)
        if filter.primitive is not None:
            if isinstance(filter.primitive, str):
                clauses.append("primitive = ?")
                params.append(filter.primitive)
            else:
                placeholders = ", ".join("?" * len(filter.primitive))
                clauses.append(f"primitive IN ({placeholders})")
                params.extend(filter.primitive)
        if filter.status is not None:
            clauses.append("status = ?")
            params.append(filter.status)
        if filter.model is not None:
            clauses.append("model = ?")
            params.append(filter.model)
        if filter.cost_source is not None:
            # Backward-compat: legacy records without cost_source count
            # as "actor". Matches filesystem backend semantics.
            if filter.cost_source == "actor":
                clauses.append("(cost_source = ? OR cost_source IS NULL)")
                params.append("actor")
            else:
                clauses.append("cost_source = ?")
                params.append(filter.cost_source)
        if filter.mandate_id is not None:
            clauses.append("mandate_id = ?")
            params.append(filter.mandate_id)
        if filter.parent_run_id is not None:
            clauses.append("parent_run_id = ?")
            params.append(filter.parent_run_id)
        if filter.since is not None:
            clauses.append("ts >= ?")
            params.append(filter.since.isoformat())
        if filter.until is not None:
            clauses.append("ts <= ?")
            params.append(filter.until.isoformat())

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        if where_only:
            return where_sql, params

        sql = f"SELECT {select} FROM run_records {where_sql}"
        if order_limit:
            sql += " ORDER BY ts ASC, id ASC"
            if filter.limit is not None:
                sql += " LIMIT ?"
                params.append(filter.limit)
        return sql, params

    def _row_to_record(self, row: sqlite3.Row) -> RunRecord:
        """Convert a SQLite ``Row`` to a ``RunRecord``."""
        extra_text = row["extra"] or "{}"
        try:
            extra = json.loads(extra_text)
        except json.JSONDecodeError:
            extra = {}
        return RunRecord(
            ts=row["ts"] or "",
            run_id=row["run_id"] or "",
            primitive=row["primitive"] or "other",
            status=row["status"] or "",
            summary=row["summary"] or "",
            model=row["model"] or "n/a",
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cost_usd=row["cost_usd"],
            cost_source=row["cost_source"],
            latency_ms=row["latency_ms"],
            cache_hit_tokens=row["cache_hit_tokens"],
            cache_miss_tokens=row["cache_miss_tokens"],
            mandate_id=row["mandate_id"],
            parent_run_id=row["parent_run_id"],
            parent_agent=row["parent_agent"],
            trigger=row["trigger"],
            agent_name=row["agent_name"],
            fallback=None if row["fallback"] is None else bool(row["fallback"]),
            critical=None if row["critical"] is None else bool(row["critical"]),
            extra=extra,
        )


# Canonical RunRecord field names available as SQL columns. Used by
# ``aggregate()`` to decide whether ``group_by`` field resolves to a
# direct column or to a JSON1 extraction.
_CANONICAL_COLUMNS = frozenset({
    "ts", "run_id", "primitive", "status", "summary", "model",
    "input_tokens", "output_tokens", "cost_usd", "cost_source",
    "latency_ms", "cache_hit_tokens", "cache_miss_tokens",
    "mandate_id", "parent_run_id", "parent_agent", "trigger",
    "agent_name", "fallback", "critical",
})


def _metric_to_sql(metric: str) -> str:
    """Map a canonical metric name to its SQL aggregate expression."""
    if metric == METRIC_COUNT:
        return "COUNT(*)"
    if metric == METRIC_SUM_COST_USD:
        return "COALESCE(SUM(cost_usd), 0.0)"
    if metric == METRIC_SUM_INPUT_TOKENS:
        return "COALESCE(SUM(input_tokens), 0)"
    if metric == METRIC_SUM_OUTPUT_TOKENS:
        return "COALESCE(SUM(output_tokens), 0)"
    if metric == METRIC_AVG_LATENCY_MS:
        # AVG returns NULL over an all-NULL column — the contract
        # surfaces None for all-None buckets. Don't COALESCE to 0.0
        # (would falsely report "instant").
        return "AVG(latency_ms)"
    raise ValueError(f"unreachable: unknown metric {metric!r}")


# ────────────────────────────────────────────────────────────────────
# Operator surface — URL parsing factory


def make_sqlite_backend_from_url(url: str) -> SQLiteLogBackend:
    """Build a SQLiteLogBackend from an operator-supplied URL.

    Format: ``sqlite:///absolute/path/to/db.sqlite`` (three slashes —
    standard SQLAlchemy convention) or ``sqlite::memory:`` for the
    in-memory variant.

    Args:
        url: connection URL.

    Returns:
        Constructed ``SQLiteLogBackend``.

    Raises:
        ValueError: malformed URL.
    """
    if url == "sqlite::memory:" or url == "sqlite:///:memory:":
        return SQLiteLogBackend(":memory:")
    parsed = urlparse(url)
    if parsed.scheme != "sqlite":
        raise ValueError(
            f"SQLite URL must start with 'sqlite://'; got {url!r}"
        )
    # ``sqlite:///path`` → ``parsed.path == '/path'``; strip the
    # leading slash for the absolute path. ``sqlite://relative/path``
    # would have parsed.netloc='relative' and parsed.path='/path' —
    # not the conventional shape, but tolerate it as best-effort.
    if parsed.netloc:
        # Relative path embedded as netloc segment
        path_str = parsed.netloc + parsed.path
    else:
        # ``sqlite:///path`` → parsed.path == ``/path``; preserve the
        # leading slash for absolute POSIX paths. ``sqlite:////path``
        # (four slashes) still works — the absolute path stays.
        path_str = parsed.path
    # Reject empty or root-only paths — ``sqlite:///`` parses to
    # parsed.path == "/", which is not a usable db file path.
    if not path_str or path_str == "/":
        raise ValueError(
            f"SQLite URL has no path component; got {url!r}"
        )
    return SQLiteLogBackend(Path(path_str))

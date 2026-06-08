"""PostgresLogBackend — psycopg 3 reference implementation for the LogBackend Protocol.

This module ships as part of the Postgres adapter arc (Issue #258, PR 1 of N).
It provides the first credentialed-URL backend in the framework and sets the
Implementer Contract every future Postgres/Redis/Loki adapter copies.

Install:
    pip install 'atomic-agents-stack[postgres]'

Usage:
    ATOMIC_AGENTS_LOG_BACKEND=postgres
    ATOMIC_AGENTS_LOG_BACKEND_URL=postgresql://user:password@host:5432/dbname

Value over SQLiteLogBackend:
    * **Fleet-scale multi-host**: Postgres is a true networked RDBMS; multiple
      Cloud Run instances can share one backend. SQLite WAL on NFS is broken.
    * **Connection-bounded**: the threading.local pattern (one connection per
      thread) keeps resource usage predictable.  Connections are NOT proactively
      closed on thread exit — they persist until the instance is closed via
      close() or until GC runs __del__ on the psycopg connection.  Call
      backend.close() in teardown to release server-side connections promptly.
      See ATOMIC_AGENTS_LOG_PG_POOL_MAX (successor issue) for bounded pool.
    * **records_today / records_this_month use local-timezone windows** —
      same as FilesystemLogBackend.stats() (date.today()) and
      SQLiteLogBackend.stats() (date.today()), so the same records produce
      the same stats regardless of which backend is registered.
    * **JSONB extra column**: native JSON type enables ->> operator for
      extra-field aggregation; retains the option for a GIN index later
      (filed as successor issue).

Schema:
    * Table ``run_records``: mirrors ``SQLiteLogBackend`` schema with
      Postgres-native types:
        - ``id BIGSERIAL PRIMARY KEY`` (vs INTEGER AUTOINCREMENT)
        - ``ts TEXT NOT NULL`` (ISO-8601 string — same TEXT approach as SQLite
          for consistent lexicographic ordering on both platforms)
        - ``extra JSONB NOT NULL DEFAULT '{}'::jsonb`` (vs TEXT in SQLite,
          enables ->> operator without json_extract(extra, '$.field'))
        - ``fallback BOOLEAN`` / ``critical BOOLEAN`` (vs INTEGER in SQLite)
    * Table ``meta``: schema version tracking.
    * Indexes: six B-tree indexes matching the SQLite reference set
      (ts, run_id, primitive, parent_run_id, cost_source, mandate_id).
      No GIN on extra — hot append path; file a successor issue if
      JSON-path query performance becomes load-bearing.

Thread-safety:
    threading.local gives each thread its own psycopg connection. psycopg 3
    connections are NOT thread-safe and must not be shared across threads.
    The threading.local pattern means one TCP connection per OS thread per
    PostgresLogBackend instance. Connections are NOT closed on thread exit —
    they persist until backend.close() is called (or GC eventually runs
    __del__ on the psycopg connection).  Always call backend.close() in
    test teardown and in application shutdown to release server-side
    connections promptly and stay under max_connections.

    max_connections_used = N_instances × max_threads_per_instance.
    Keep this below Postgres's max_connections - 5 (reserved for admin).
    Default safe for home users on gizmo or a single Cloud Run instance.
    For fleet deployments with large thread pools, see
    ATOMIC_AGENTS_LOG_PG_POOL_MAX (successor issue) to layer a bounded
    psycopg_pool.ConnectionPool on top.

Cold-start race mitigation:
    _ensure_schema() acquires SELECT pg_advisory_xact_lock(<key>) inside a
    transaction before CREATE TABLE. The xact-scoped variant releases
    automatically on COMMIT/ROLLBACK — no explicit release needed, no
    session-scoped leak risk. ON CONFLICT DO NOTHING on the schema_version
    INSERT makes concurrent cold-start race idempotent (same purpose as
    SQLite's INSERT OR IGNORE).

Credential redaction:
    Three layers:
    (A) All logged/echoed connection URLs are stripped of credentials via
        _redact_dsn() before surfacing in any exception or log message.
    (B) psycopg driver-level: connections are opened with explicit keyword
        args (host=, port=, dbname=, user=, password=) so psycopg never
        builds a DSN string that it can echo internally. psycopg INFO-level
        logger is suppressed to WARNING at backend construction.
    (C) Env-var surface: credentials come only via ATOMIC_AGENTS_LOG_BACKEND_URL
        parsed at construction.  The full URL string is not retained (only
        _safe_url, already redacted).  The password component IS stored as
        _password for driver use — __dict__ introspection can expose it.

Paramstyle note:
    psycopg 3 uses %s positional placeholders (paramstyle='pyformat'), NOT
    the ? (paramstyle='qmark') that sqlite3 uses. Every parameterized
    statement in this module uses %s. Do NOT port ? placeholders from sqlite.py.

Schema versions:
    _SCHEMA_VERSION = 1. Independent of SQLite's _SCHEMA_VERSION = 1.
    Bumping SQLite v1→v2 does NOT require bumping Postgres v1→v2 and vice
    versa. Each backend owns its own schema version ladder.

autocommit mode:
    Connections use autocommit=False (psycopg 3 default). Every write
    path calls conn.commit() immediately after execute() per spec/22 MUST 2
    ('persist before returning'). _ensure_schema() wraps all DDL +
    schema_version INSERT + SELECT in a single transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import threading
from datetime import datetime, time as dt_time, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

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

_logger = logging.getLogger(__name__)

# Schema version — independent of SQLite's _SCHEMA_VERSION = 1.
# Bumping SQLite v1→v2 does NOT require bumping Postgres v1→v2 and vice versa.
_SCHEMA_VERSION = 1

# Advisory lock key — stable int64 derived from a fixed constant string.
# All processes using this backend target the same key, so the cold-start
# DDL race serializes correctly even across N Cloud Run replicas.
# pg_advisory_xact_lock takes an int8 (signed 64-bit); Python struct '>q'
# gives big-endian signed int64.
_ADVISORY_LOCK_KEY: int = struct.unpack(
    ">q",
    hashlib.sha256(b"atomic-agents-log-schema-v1").digest()[:8],
)[0]

# SQL DDL — Postgres-native types.
# ts as TEXT for ISO-8601 lexicographic ordering (same as SQLite; consistent
# on both platforms). extra as JSONB for ->> operator and future GIN indexing.
# id BIGSERIAL (auto-increment int8) for monotonic insertion ordering used
# in ORDER BY ts, id tiebreaker.
_CREATE_RUN_RECORDS = """
CREATE TABLE IF NOT EXISTS run_records (
    id BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    run_id TEXT NOT NULL,
    primitive TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd DOUBLE PRECISION,
    cost_source TEXT,
    latency_ms DOUBLE PRECISION,
    cache_hit_tokens INTEGER,
    cache_miss_tokens INTEGER,
    mandate_id TEXT,
    parent_run_id TEXT,
    parent_agent TEXT,
    trigger TEXT,
    agent_name TEXT,
    fallback BOOLEAN,
    critical BOOLEAN,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# Indexes on the predicates the dashboard + cost-guardrail readers use.
# Six B-tree indexes mirror the SQLite reference set.
# No GIN on extra — hot append path; file a successor issue if JSON-path
# query performance becomes load-bearing.
_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ts ON run_records(ts)",
    "CREATE INDEX IF NOT EXISTS idx_run_id ON run_records(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_primitive ON run_records(primitive)",
    "CREATE INDEX IF NOT EXISTS idx_parent_run_id ON run_records(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_cost_source ON run_records(cost_source)",
    "CREATE INDEX IF NOT EXISTS idx_mandate_id ON run_records(mandate_id)",
]

# Column names in INSERT order (matches CREATE TABLE column order minus id).
_INSERT_COLUMNS = (
    "ts",
    "run_id",
    "primitive",
    "status",
    "summary",
    "model",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "cost_source",
    "latency_ms",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "mandate_id",
    "parent_run_id",
    "parent_agent",
    "trigger",
    "agent_name",
    "fallback",
    "critical",
    "extra",
)

# Precomputed INSERT statement using %s placeholders (psycopg 3 paramstyle
# is 'pyformat', NOT 'qmark' like sqlite3). Do NOT use ? here.
_INSERT_SQL = (
    "INSERT INTO run_records ("
    + ", ".join(_INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join(["%s"] * len(_INSERT_COLUMNS))
    + ")"
)


_CREDENTIAL_QUERY_KEYS = frozenset({"password", "sslpassword"})

# Known path-valued Postgres TLS parameters whose values are filesystem paths,
# not key material.  These are excluded from substring redaction to avoid
# masking diagnostic information operators need when debugging TLS failures.
_PATH_VALUED_KEYS = frozenset({"sslkey", "sslcert", "sslrootcert"})


def _is_credential_key(key: str) -> bool:
    """Return True if a query-string key looks like a credential parameter.

    Matches exact names in _CREDENTIAL_QUERY_KEYS plus any key containing
    'password' or 'secret' as a substring (case-insensitive).  The bare 'key'
    substring is intentionally omitted: Postgres TLS parameters like ``sslkey``
    and ``sslrootcert`` carry filesystem paths, not key material, and masking
    their values removes diagnostic information without a security benefit.

    The conservative 'password'/'secret' fragments still catch future psycopg
    DSN extensions (e.g. a hypothetical ``gssapipassword``) without a code
    change.
    """
    lower = key.lower()
    if lower in _CREDENTIAL_QUERY_KEYS:
        return True
    if lower in _PATH_VALUED_KEYS:
        return False
    for fragment in ("password", "secret"):
        if fragment in lower:
            return True
    return False


def _redact_dsn(url: str) -> str:
    """Strip credentials from a DSN/URL for safe logging and exception messages.

    Covers two credential locations that valid Postgres DSNs may use:
      (1) netloc password  — ``postgresql://user:password@host/db``
      (2) query-string     — ``postgresql://host/db?password=pw&sslpassword=sp``

    Returns the URL with credential values replaced by the literal string
    '***' in both positions (netloc and query-string), so redacted URLs read
    consistently in error messages and logs.  ``urlencode(..., safe='*')``
    prevents percent-encoding of the asterisks, which would produce the
    unreadable '%2A%2A%2A' form instead of the clean '***'.

    Falls back to scheme-only form only when urlparse raises an exception.
    """
    try:
        parsed = urlparse(url)
        # ── (1) netloc password ──────────────────────────────────────────
        if parsed.password:
            netloc = parsed.hostname or ""
            if parsed.username:
                netloc = f"{parsed.username}:***@{netloc}"
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            parsed = parsed._replace(netloc=netloc)
        # ── (2) query-string credentials ─────────────────────────────────
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            redacted = [
                (k, "***") if _is_credential_key(k) else (k, v) for k, v in pairs
            ]
            # safe='*' prevents urlencode from percent-encoding the '***'
            # redaction marker, keeping the output readable as 'password=***'
            # (consistent with the netloc ':***@' form above).
            parsed = parsed._replace(query=urlencode(redacted, safe="*"))
        return parsed.geturl()
    except Exception:
        pass
    # Fallback only on parse failure: strip everything after ://
    if "://" in url:
        scheme = url.split("://", 1)[0]
        return f"{scheme}://..."
    return url


class PostgresLogBackend:
    """Postgres-backed LogBackend — queryable, indexed, multi-host-safe.

    Conforms to the LogBackend Protocol (spec/22). Constructed with a
    Postgres connection URL; the schema is created on first use.

    Args:
        url: postgresql://user:password@host:port/dbname connection URL.
            The full URL string is not retained — only the redacted form
            (_safe_url) is stored.  The URL is parsed into components
            (_host, _port, _dbname, _user, _password) for driver-level
            connection.  Note: _password IS stored as a plain instance
            attribute; __dict__ / debugger introspection will expose it.
            If repr() protection is needed, wrap in a SecretStr and add
            a custom __repr__.

    Thread-safety:
        Each thread gets its own psycopg connection via threading.local.
        psycopg 3 connections are NOT thread-safe; per-thread connections
        are required (unlike SQLite which could share with check_same_thread=False).

    Credential redaction:
        Connection opened with explicit keyword args so psycopg never
        builds a DSN repr. psycopg logger suppressed to WARNING.
        All exception paths redact the URL before surfacing.
    """

    @property
    def backend_id(self) -> str:
        return "postgres"

    def __init__(self, url: str) -> None:
        # Parse the URL at construction time, storing components separately.
        # This prevents the raw URL (with credentials) from surviving in
        # repr(self) or tracebacks that print self.__dict__.
        try:
            parsed = urlparse(url)
        except Exception as exc:
            raise ValueError(
                f"PostgresLogBackend: malformed URL. "
                f"Expected postgresql://user:pass@host:port/dbname. Error: {exc}"
            ) from exc

        if parsed.scheme not in ("postgresql", "postgres"):
            raise ValueError(
                f"PostgresLogBackend: URL must start with postgresql:// or postgres://; "
                f"got scheme {parsed.scheme!r}. "
                f"Full URL (redacted): {_redact_dsn(url)}"
            )

        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 5432
        self._dbname = (parsed.path or "/").lstrip("/") or "postgres"
        self._user = parsed.username or ""
        self._password = parsed.password or ""
        # Store the redacted URL for safe error messages.
        self._safe_url = _redact_dsn(url)

        # Suppress psycopg's own INFO-level logs that may echo DSN components.
        # Layer B credential redaction: psycopg logs DSN in some error paths.
        logging.getLogger("psycopg").setLevel(logging.WARNING)
        logging.getLogger("psycopg.pool").setLevel(logging.WARNING)

        # Per-thread connection storage. Each thread gets its own psycopg
        # connection — psycopg 3 connections are NOT thread-safe.
        self._tls = threading.local()

    # ────────────────────────────────────────────────────────────
    # Connection management

    def _get_conn(self) -> Any:
        """Return the calling thread's connection — lazy-create on first use.

        Reconnects automatically when the cached connection has gone broken or
        closed (server restart, idle TCP timeout, network blip).  This prevents
        silent permanent audit loss after a transient failure, which would
        otherwise cause every subsequent write to raise until the process
        restarts.
        """
        conn = getattr(self._tls, "conn", None)
        # Treat a cached-but-dead connection as absent so we reconnect.
        if conn is not None and (
            getattr(conn, "closed", 0) or getattr(conn, "broken", False)
        ):
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            conn = None

        if conn is None:
            try:
                import psycopg  # noqa: PLC0415 — lazy import by design
            except ImportError as exc:
                raise ImportError(
                    "PostgresLogBackend requires the 'postgres' extra. "
                    "Install via: pip install 'atomic-agents-stack[postgres]'"
                ) from exc

            try:
                conn = psycopg.connect(
                    host=self._host,
                    port=self._port,
                    dbname=self._dbname,
                    user=self._user,
                    password=self._password,
                    autocommit=False,
                    row_factory=psycopg.rows.dict_row,
                )
            except psycopg.Error:
                # Layer A+B: strip psycopg's DSN repr from the exception.
                # Catch psycopg.Error (base class) not just OperationalError —
                # ProgrammingError (bad dbname), SSL errors, etc. can also
                # embed DSN components in their message.
                raise ValueError(
                    f"PostgresLogBackend: could not connect to Postgres at "
                    f"{self._safe_url}. Check ATOMIC_AGENTS_LOG_BACKEND_URL. "
                    f"Run atomic-agents doctor for details."
                ) from None

            # If schema init fails, close the open connection before re-raising
            # so we don't leak a server-side backend process.
            try:
                self._ensure_schema(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            self._tls.conn = conn
        return conn

    def _ensure_schema(self, conn: Any) -> None:
        """Create tables + indexes if missing; assert schema version.

        Cold-start multi-process race mitigation:
        1. Acquire pg_advisory_xact_lock (transaction-scoped, auto-releases
           on COMMIT/ROLLBACK) BEFORE DDL — serializes concurrent replicas.
        2. CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS — idempotent.
        3. INSERT INTO meta ON CONFLICT DO NOTHING — idempotent schema_version
           row (Postgres equivalent of SQLite's INSERT OR IGNORE).
        4. SELECT to validate version — all inside one transaction.

        The advisory lock key is a stable int64 derived from a fixed string
        so all processes target the same key without coordination.
        """
        try:
            # Begin a transaction that holds the advisory lock for its duration.
            # pg_advisory_xact_lock blocks until the lock is available (unlike
            # pg_try_advisory_xact_lock which returns immediately).
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_ADVISORY_LOCK_KEY,),
            )
            conn.execute(_CREATE_RUN_RECORDS)
            conn.execute(_CREATE_META)
            for stmt in _CREATE_INDEXES:
                conn.execute(stmt)
            # ON CONFLICT DO NOTHING — Postgres equivalent of SQLite's INSERT OR IGNORE.
            # Losing the cold-start race (another process already inserted) is a no-op.
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
            cur = conn.execute(
                "SELECT value FROM meta WHERE key = %s",
                ("schema_version",),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(
                    "PostgresLogBackend: schema_version row missing after "
                    "INSERT ON CONFLICT DO NOTHING — db corruption suspected."
                )
            existing = int(row["value"])
            if existing != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"PostgresLogBackend schema version mismatch: db has "
                    f"v{existing}, code expects v{_SCHEMA_VERSION}. "
                    f"A backward-incompatible schema bump shipped without "
                    f"a migration. Open an issue at "
                    f"https://github.com/dep0we/atomic-agents-stack/issues "
                    f"with this error."
                )
            # COMMIT releases the advisory lock (xact-scoped).
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    # ────────────────────────────────────────────────────────────
    # Append

    def append(self, record: RunRecord) -> None:
        """INSERT one RunRecord row.

        Atomic via single-statement transaction. psycopg 3 autocommit=False
        means the INSERT is in an implicit transaction; conn.commit()
        immediately after satisfies spec/22 MUST 2 ('persist before returning').

        extra is passed as a Python dict; psycopg 3 uses the Jsonb() adapter
        (psycopg.types.json.Jsonb, targets the jsonb type) to serialize it
        to the JSONB column — no json.dumps needed.
        In _row_to_record(), psycopg returns the JSONB column as a Python
        dict directly.
        """
        conn = self._get_conn()
        # Jsonb import comes AFTER _get_conn() so _get_conn()'s friendly
        # ImportError ("requires the 'postgres' extra") fires first if psycopg
        # is missing.  Once _get_conn() succeeds, psycopg is confirmed importable,
        # making this a cheap sys.modules lookup with no try/except overhead on
        # the hottest write path (27+ _log() calls per agent.call()).
        import psycopg.types.json as _pj  # noqa: PLC0415

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
            record.fallback,
            record.critical,
            _pj.Jsonb(record.extra if record.extra is not None else {}),
        )
        try:
            conn.execute(_INSERT_SQL, values)
            # spec/22 MUST 2: persist before returning.
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            # Discard the connection so the next call rebuilds it — prevents
            # a broken connection from silently dropping every subsequent write.
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            raise

    # ────────────────────────────────────────────────────────────
    # Query

    def query(self, filter: LogQuery) -> list[RunRecord]:
        """Push every predicate down to SQL WHERE; ORDER BY ts ASC, id ASC; LIMIT.

        ISO-8601 lexicographic comparison on TEXT ts column matches chronological
        order for tz-aware records — same invariant as the SQLite backend.
        id ASC tiebreaker preserves insertion order for same-ts records
        (spec/22 MUST 3: preserve insertion order within a run).

        Transaction discipline: autocommit=False means every execute() opens an
        implicit transaction.  We commit() on success so the transaction ends
        cleanly; on error we rollback() + discard the connection so the next
        call gets a fresh connection rather than hitting "current transaction is
        aborted" on an ABORTED-state connection.  Mirrors the write-path shape
        in append() and delete_older_than().
        """
        sql, params = self._build_query_sql(filter, select="*", order_limit=True)
        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            raise
        return [self._row_to_record(row) for row in rows]

    # ────────────────────────────────────────────────────────────
    # Tail

    def tail(self, n: int) -> list[RunRecord]:
        """SELECT ORDER BY ts DESC, id DESC LIMIT n — index seek bounded to n rows.

        Transaction discipline: same as query() — commit() on success, rollback()
        + discard connection on error to prevent idle-in-transaction / aborted-tx
        accumulation.
        """
        if n < 0:
            raise ValueError(f"tail(n) requires n >= 0; got {n}")
        if n == 0:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM run_records ORDER BY ts DESC, id DESC LIMIT %s",
                (n,),
            ).fetchall()
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            raise
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
        """Compute the metric grouped by agg.group_by via SQL GROUP BY.

        For group_by field names that match canonical RunRecord columns,
        uses the column directly. For extra-resolved field names, uses the
        JSONB ->> operator: (extra->>'FIELDNAME'). The SQL injection guard
        (alphanumeric + underscore allowlist) applies identically to SQLite's
        json_extract path — the field name is string-interpolated, not
        parameterized, so the allowlist is the only safe pattern here.

        Do NOT use %s parameterization for the JSON path component — that
        would pass the field name as a string literal argument, not as the
        JSON key name, breaking the SQL semantics.
        """
        if agg.metric not in VALID_METRICS:
            raise ValueError(
                f"Unknown aggregate metric: {agg.metric!r}. "
                f"Valid metrics: {sorted(VALID_METRICS)}"
            )

        group_exprs: list[str] = []
        for col in agg.group_by:
            if col in _CANONICAL_COLUMNS:
                group_exprs.append(col)
            else:
                # JSON-extract path. Strict charset: alphanumeric + underscore,
                # ASCII-only (spec/22 MUST 6 wording).  str.isalnum() matches
                # Unicode letters/digits, so the explicit isascii() guard is
                # required to enforce the ASCII-only contract.
                # SQL injection guard: col is string-interpolated into the SQL,
                # so the allowlist is the ONLY safe pattern here.
                # (extra->>'colname') returns TEXT for all values.
                if not (col.replace("_", "").isalnum() and col.isascii()):
                    raise ValueError(
                        f"aggregate group_by field {col!r} is not a "
                        f"valid identifier (alphanumeric + underscore, ASCII-only)"
                    )
                # Use Postgres JSONB ->> operator (returns TEXT).
                group_exprs.append(f"(extra->>{col!r})")

        metric_expr = _metric_to_sql(agg.metric)

        where_sql, params = self._build_query_sql(
            filter, select="", order_limit=False, where_only=True
        )

        if group_exprs:
            # Alias every group expression deterministically as g0, g1, ...
            # and the metric as "metric".  Unaliased JSONB expressions like
            # (extra->>'field') both map to the generated name "?column?" in
            # Postgres dict_row — duplicate keys collapse into one entry, so
            # list(row.values()) yields fewer elements than expected and causes
            # IndexError for 2+ extra-resolved group_by fields.
            # Reading by alias is the only safe pattern with psycopg dict_row.
            aliased_group = [f"{expr} AS g{i}" for i, expr in enumerate(group_exprs)]
            group_clause = "GROUP BY " + ", ".join(group_exprs)
            select_cols = ", ".join(aliased_group) + ", " + metric_expr + " AS metric"
        else:
            group_clause = ""
            select_cols = metric_expr + " AS metric"

        sql = f"SELECT {select_cols} FROM run_records {where_sql} {group_clause}"
        conn = self._get_conn()
        # Transaction discipline: commit() on success, rollback() + discard on
        # error — same shape as query() / tail() / stats().
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            raise

        result: dict[tuple, float | int] = {}
        for row in rows:
            if group_exprs:
                # Read by alias — safe for any number of group fields.
                key = tuple(row[f"g{i}"] for i in range(len(group_exprs)))
                value = row["metric"]
            else:
                key = ()
                value = row["metric"]
            if agg.metric == METRIC_AVG_LATENCY_MS:
                # Postgres AVG returns Decimal; coerce to float for cross-backend
                # parity with SQLite (which returns float natively).  Preserve
                # None for all-NULL buckets (SQL AVG of zero rows is NULL).
                result[key] = float(value) if value is not None else None  # type: ignore[assignment]
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
        """DELETE WHERE ts < threshold — bounded by the ts index.

        Raises ValueError on naive datetimes per spec/22 contract.

        psycopg 3 rowcount: read cur.rowcount BEFORE conn.commit() —
        psycopg 3 may reset rowcount to -1 after commit on some versions.
        """
        if threshold.tzinfo is None:
            raise ValueError(
                "delete_older_than(threshold) requires a tz-aware datetime; "
                "naive datetime would silently convert local-vs-UTC and corrupt "
                "retention near midnight"
            )
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM run_records WHERE ts < %s",
                (threshold.isoformat(),),
            )
            # Read rowcount BEFORE commit — psycopg 3 may reset after commit.
            count = cur.rowcount
            conn.commit()
            return count
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            # Discard the connection so the next call rebuilds it — mirrors
            # append()'s except path so both write paths handle broken
            # connections identically. _get_conn() already guards on broken
            # connections, but discarding eagerly prevents a misleadingly
            # "open" connection from silently failing future writes.
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            raise

    # ────────────────────────────────────────────────────────────
    # Stats

    def stats(self) -> LogStats:
        """Single-query stats — sequential scan (no covering index on run_records).

        Six single-column B-tree indexes exist but COUNT(*) with two conditional
        CASE buckets requires a full-table scan; the idx_ts index alone is not
        a covering index for this aggregate. For large tables, a composite or
        partial index can be added as a successor issue.

        size_bytes: always None for Postgres. Postgres stores data remotely;
        size_bytes is undefined for non-disk-shape backends per spec/22 §LogStats.
        Operators who want storage size can query pg_total_relation_size('run_records')
        directly via psql. Returning None avoids the pg_catalog permission issue.
        """
        conn = self._get_conn()

        # Local-timezone day + month windows — matches FilesystemLogBackend
        # (date.today()) and SQLiteLogBackend (date.today()) so that
        # records_today / records_this_month are consistent across all three
        # backends for the same data.
        # Use .astimezone() on each boundary (not tzinfo= on datetime.combine)
        # so that the UTC offset is resolved for THAT instant — on DST-transition
        # days spring-forward/fall-back changes the offset at midnight, and
        # combine(..., tzinfo=now.tzinfo) pins tonight's offset onto midnight,
        # producing the wrong ISO string on those ~2 days per year.
        # Mirrors sqlite.py:562-567 exactly: combine(..., dt_time.min).astimezone().
        today = datetime.now().astimezone().date()
        today_start = datetime.combine(today, dt_time.min).astimezone().isoformat()
        today_end = datetime.combine(today, dt_time.max).astimezone().isoformat()
        first_of_month = today.replace(day=1)
        month_start = (
            datetime.combine(first_of_month, dt_time.min).astimezone().isoformat()
        )

        # Transaction discipline: commit() on success, rollback() + discard on
        # error — same shape as query() / tail() / aggregate().
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    MIN(ts) AS oldest,
                    MAX(ts) AS newest,
                    COUNT(CASE WHEN ts >= %s AND ts <= %s THEN 1 END) AS today,
                    COUNT(CASE WHEN ts >= %s AND ts <= %s THEN 1 END) AS month
                FROM run_records
                """,
                (today_start, today_end, month_start, today_end),
            ).fetchone()
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            raise

        total = int(row["total"] or 0)
        oldest_ts = row["oldest"]
        newest_ts = row["newest"]
        records_today = int(row["today"] or 0)
        records_this_month = int(row["month"] or 0)

        # size_bytes = None — Postgres stores data remotely; no local file to stat.
        # See module docstring for rationale.
        return LogStats(
            total_records=total,
            oldest_ts=oldest_ts,
            newest_ts=newest_ts,
            size_bytes=None,
            records_today=records_today,
            records_this_month=records_this_month,
        )

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> LogCapabilities:
        return LogCapabilities(
            supports_aggregation_pushdown=True,
            supports_streaming=False,  # reserved; mirror SQLite value
            supports_retention=True,
            durable=True,
        )

    # ────────────────────────────────────────────────────────────
    # Connection lifecycle

    def close(self) -> None:
        """Close the calling thread's connection, if open.

        Call this in test teardown and application shutdown to release
        server-side Postgres backend processes promptly.  threading.local
        does NOT close connections on thread exit — without explicit close(),
        connections persist until GC runs __del__ on the psycopg connection.

        Safe to call multiple times (idempotent — ignores already-closed).
        """
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None

    def __del__(self) -> None:
        """Best-effort close on GC — not a substitute for explicit close()."""
        try:
            self.close()
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────
    # Internal SQL builders

    def _build_query_sql(
        self,
        filter: LogQuery,
        select: str = "*",
        order_limit: bool = True,
        where_only: bool = False,
    ) -> tuple[str, list[Any]]:
        """Build WHERE clause + parameter list for a LogQuery.

        Uses %s placeholders (psycopg 3 paramstyle='pyformat').
        Do NOT use ? (that's sqlite3's qmark paramstyle).

        For the tuple primitive filter, uses ANY(%s) with a list parameter
        — the idiomatic Postgres IN-list pattern. This avoids the N-placeholder
        string construction and the tuple-as-single-param binding mistake.
        # psycopg3: ANY(%s) with a list param is idiomatic for IN-list queries.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if filter.run_id is not None:
            clauses.append("run_id = %s")
            params.append(filter.run_id)
        if filter.primitive is not None:
            if isinstance(filter.primitive, str):
                clauses.append("primitive = %s")
                params.append(filter.primitive)
            else:
                # psycopg3: ANY(%s) with a list param is idiomatic for IN-list queries;
                # avoids N-placeholder string construction.
                clauses.append("primitive = ANY(%s)")
                params.append(list(filter.primitive))
        if filter.status is not None:
            clauses.append("status = %s")
            params.append(filter.status)
        if filter.model is not None:
            clauses.append("model = %s")
            params.append(filter.model)
        if filter.cost_source is not None:
            # Backward-compat: legacy records without cost_source count as "actor".
            if filter.cost_source == "actor":
                clauses.append("(cost_source = %s OR cost_source IS NULL)")
                params.append("actor")
            else:
                clauses.append("cost_source = %s")
                params.append(filter.cost_source)
        if filter.mandate_id is not None:
            clauses.append("mandate_id = %s")
            params.append(filter.mandate_id)
        if filter.parent_run_id is not None:
            clauses.append("parent_run_id = %s")
            params.append(filter.parent_run_id)
        if filter.agent_name is not None:
            # Lenient: match when column = filter OR column IS NULL (legacy compat).
            # One placeholder — IS NULL needs no parameter.
            clauses.append("(agent_name = %s OR agent_name IS NULL)")
            params.append(filter.agent_name)
        if filter.since is not None:
            clauses.append("ts >= %s")
            params.append(filter.since.isoformat())
        if filter.until is not None:
            clauses.append("ts <= %s")
            params.append(filter.until.isoformat())

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        if where_only:
            return where_sql, params

        sql = f"SELECT {select} FROM run_records {where_sql}"
        if order_limit:
            sql += " ORDER BY ts ASC, id ASC"
            if filter.limit is not None:
                sql += " LIMIT %s"
                params.append(filter.limit)
        return sql, params

    def _row_to_record(self, row: dict) -> RunRecord:
        """Convert a psycopg dict_row to a RunRecord.

        JSONB column: psycopg 3 returns JSONB columns as Python dicts directly.
        Guard with isinstance check for future TEXT-column forward compat.

        BOOLEAN columns: psycopg 3 returns Python True/False for BOOLEAN columns.
        Still need explicit None-guard — bool(None) = False would corrupt
        records where fallback/critical were genuinely unset.

        Empty-string preservation: use 'x if x is not None else DEFAULT'
        not 'x or DEFAULT' — Python's or treats empty strings as falsy.
        """
        extra = row["extra"]
        if extra is None:
            extra = {}
        elif not isinstance(extra, dict):
            # TEXT column fallback (should not occur with JSONB schema).
            try:
                extra = json.loads(extra)
            except (json.JSONDecodeError, TypeError):
                extra = {}

        return RunRecord(
            ts=row["ts"] if row["ts"] is not None else "",
            run_id=row["run_id"] if row["run_id"] is not None else "",
            primitive=row["primitive"] if row["primitive"] is not None else "other",
            status=row["status"] if row["status"] is not None else "",
            summary=row["summary"] if row["summary"] is not None else "",
            model=row["model"] if row["model"] is not None else "n/a",
            input_tokens=int(row["input_tokens"])
            if row["input_tokens"] is not None
            else 0,
            output_tokens=int(row["output_tokens"])
            if row["output_tokens"] is not None
            else 0,
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
            # BOOLEAN columns: explicit None-guard required — bool(None) = False
            # would silently corrupt records with genuinely unset fallback/critical.
            fallback=None if row["fallback"] is None else bool(row["fallback"]),
            critical=None if row["critical"] is None else bool(row["critical"]),
            extra=extra,
        )


# Canonical RunRecord field names available as SQL columns. Derived from
# RunRecord.__dataclass_fields__ minus 'extra' so a new field added to
# RunRecord automatically becomes available as a column once the schema
# is migrated. Do not hand-code this list.
_CANONICAL_COLUMNS = frozenset(
    name for name in RunRecord.__dataclass_fields__ if name != "extra"
)


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
        # surfaces None for all-None buckets. Don't COALESCE to 0.0.
        return "AVG(latency_ms)"
    raise ValueError(f"unreachable: unknown metric {metric!r}")


# ────────────────────────────────────────────────────────────────────
# Operator surface — URL parsing factory


def make_postgres_backend_from_url(url: str) -> PostgresLogBackend:
    """Build a PostgresLogBackend from an operator-supplied URL.

    Format: ``postgresql://user:password@host:5432/dbname``
    or the equivalent ``postgres://`` scheme alias.

    Lazy-imports psycopg so the dependency is only required when the
    operator has selected the 'postgres' extra.

    The asymmetry with SQLite (eager registration) is principled:
    - SQLite is stdlib, zero install cost, no optional extra.
    - psycopg is a C extension with a network driver; importing it at
      framework startup for every operator who doesn't use Postgres is
      unacceptable. Lazy import is the right default here.

    Args:
        url: postgresql:// or postgres:// connection URL.

    Returns:
        Constructed PostgresLogBackend (schema NOT yet initialized —
        initialization happens lazily on first connection).

    Raises:
        ImportError: when psycopg[binary] is not installed.
        ValueError: malformed URL or wrong scheme.
    """
    try:
        import psycopg  # noqa: F401, PLC0415 — verify importability
    except ImportError as exc:
        raise ImportError(
            "PostgresLogBackend requires the 'postgres' extra. "
            "Install via: pip install 'atomic-agents-stack[postgres]'"
        ) from exc

    return PostgresLogBackend(url)

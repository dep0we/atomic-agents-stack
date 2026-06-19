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
      A bounded ``psycopg_pool.ConnectionPool`` layer is tracked by successor
      issue #365 (not implemented in this PR).
    * **records_today / records_this_month use local-timezone windows** —
      same as FilesystemLogBackend.stats() (date.today()) and
      SQLiteLogBackend.stats() (date.today()), so the same records produce
      the same stats regardless of which backend is registered.
    * **JSONB extra column**: native JSON type enables ->> operator for
      extra-field aggregation; retains the option for a GIN index later
      (file a successor issue if JSON-path query performance becomes
      load-bearing).

KNOWN cross-backend divergence (accepted in v1.0, tracked by #366):
    aggregate() group_by on an *extra* (JSONB) field returns TEXT dict keys
    here, because the JSONB ``->>`` accessor always yields TEXT. The
    Filesystem and SQLite reference backends return the value's NATIVE Python
    type for the same data. ``->>`` cannot know the operator's intended type,
    so a blind CAST would be a guess. The divergence is documented in spec/22
    ("JSONB extra column — aggregation") and pinned by a conformance test
    (test_aggregate_extra_field_key_type_divergence) rather than silently
    normalized. Per JSON value class, for the SAME data:

      * **numeric** {'iteration': 1} → ('1',) on Postgres, (1,) on fs/sqlite.
        ``str(k)`` bridges these: str(1) == '1'.
      * **float** {'ratio': 1.5} → ('1.5',) on Postgres, (1.5,) on fs/sqlite.
        ``str(k)`` bridges these: str(1.5) == '1.5'.
      * **string** {'env': 'prod'} → ('prod',) on ALL three (text either way).
        Identical; no coercion needed.
      * **boolean** {'flag': True} → ('true',) on Postgres (the JSON text
        literal), (1,) on SQLite (json_extract yields int for JSON booleans),
        (True,) on Filesystem (native Python bool). ``str(k)`` does NOT bridge
        these: 'true' != str(1) == '1' != str(True) == 'True'. Boolean extra
        fields are therefore NOT backend-portable for group_by until #366 lands.

    The operator mitigation ``str(k)`` makes numeric/float/string extra-field
    group_bys backend-portable but is INSUFFICIENT for booleans (three distinct
    string forms). Canonical-column group_bys (primitive/model/status/…) are
    unaffected — those resolve to typed columns and key identically.

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
    For fleet deployments with large thread pools, a bounded
    psycopg_pool.ConnectionPool layer is tracked by successor issue #365.

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
    _SCHEMA_VERSION = 3 (v1→v2→v3 ladder: v2 added idempotency_key +
    replayed_run_id columns + idx_idempotency_key #520 PR2; v3 adds
    conversation_id column + idx_conversation_id partial index #535 PR1).
    See the inline comment block at the _SCHEMA_VERSION constant for the
    authoritative ladder. Independent of SQLite's ladder — bumping one backend
    does NOT require bumping the other; each owns its own schema version ladder.

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
from datetime import date, datetime, time as dt_time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from ..exceptions import LogBackendReadError
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


def _psycopg_error() -> type[BaseException]:
    """Return ``psycopg.Error`` (the base exception class) for use in
    ``except`` clauses on the read-failure boundary.

    psycopg is importable at the read-method **post-connect** wrap sites: the
    read methods call ``self._get_conn()`` (which imports + connects) BEFORE
    entering the ``except _psycopg_error()`` wrap, so by the time those wraps are
    evaluated the extra is present. (Narrow caveat: ``_get_conn_for_read`` wraps
    ``self._get_conn()`` itself — if the ``postgres`` extra is absent,
    ``_get_conn`` raises a curated ImportError and Python then evaluates this
    ``except`` clause, which re-imports psycopg and raises a second, less-helpful
    ImportError during exception handling. The practical message in both cases
    still names psycopg, so this is a cosmetic edge limited to constructing a
    Postgres backend with psycopg uninstalled — not a correctness issue.)
    Resolving the class lazily (rather than at module import) keeps the
    ``postgres`` extra optional for callers that never touch this backend,
    mirroring the lazy ``import psycopg`` pattern at ``_get_conn``.
    """
    import psycopg  # noqa: PLC0415 — lazy import by design

    return psycopg.Error


# Schema version — independent of SQLite's _SCHEMA_VERSION.
# Bumping SQLite v1→v2 does NOT require bumping Postgres v1→v2 and vice versa,
# but spec/45 PR2's spec/22 versioned normative addendum (idempotency_key +
# replayed_run_id columns + idx_idempotency_key) lands on BOTH backends, so
# Postgres also moves to v2 with a v1→v2 ALTER-TABLE migration ladder.
# spec/47 PR1 bumps to v3 (adds conversation_id column + idx_conversation_id
# partial index, per the spec/22 versioned normative addendum for
# ConversationBackend). Both SQLite and Postgres move to v3 in this PR.
_SCHEMA_VERSION = 3

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
    -- spec/45 PR2 / spec/22 versioned normative addendum: idempotency audit fields.
    idempotency_key TEXT,
    replayed_run_id TEXT,
    -- spec/47 PR1 / spec/22 versioned normative addendum: conversation audit field.
    conversation_id TEXT,
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
    # spec/22 versioned normative addendum (spec/45 PR2): MUST index for the
    # LogQuery.idempotency_key AND-predicate (index seek, not table scan).
    # PARTIAL index (WHERE idempotency_key IS NOT NULL): the column is NULL for
    # nearly every run (only keyed runs set it), so a partial index stays small
    # and keeps the append hot-path cheap while still serving the `= %s` lookup
    # as an index seek (the AND-predicate matches the partial predicate).
    "CREATE INDEX IF NOT EXISTS idx_idempotency_key ON run_records(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    # spec/22 versioned normative addendum (spec/47 PR1): MUST index for the
    # LogQuery.conversation_id AND-predicate (index seek, not table scan).
    # PARTIAL index (WHERE conversation_id IS NOT NULL): the column is NULL for
    # nearly every run (only conversation-keyed runs set it).
    "CREATE INDEX IF NOT EXISTS idx_conversation_id ON run_records(conversation_id) "
    "WHERE conversation_id IS NOT NULL",
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
    "idempotency_key",
    "replayed_run_id",
    "conversation_id",
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


class _CommitPhaseError(Exception):
    """Internal sentinel: a write op failed at/after ``commit()``.

    Wraps the original psycopg error as ``__cause__``. Signals to
    ``_run_with_reconnect`` that the INSERT may already be persisted
    server-side, so the op must NOT be retried (a retry would duplicate the
    audit row). Never escapes the module — ``_run_with_reconnect`` unwraps it
    and re-raises the original cause.
    """


_CREDENTIAL_QUERY_KEYS = frozenset({"password", "sslpassword"})

# Known path-valued Postgres TLS parameters whose values are filesystem paths,
# not key material.  These are excluded from substring redaction to avoid
# masking diagnostic information operators need when debugging TLS failures.
_PATH_VALUED_KEYS = frozenset({"sslkey", "sslcert", "sslrootcert"})

# Single source for the targeted "percent-encode the credential" error so both
# detection arms in __init__ (port-cast failure, and '@' in parsed.path) emit
# identical, drift-proof guidance.  Interpolates ONLY the already-redacted
# safe_url — never the raw URL or a stdlib exception fragment.
_SPECIAL_CHAR_CREDENTIAL_MSG = (
    "PostgresLogBackend: malformed URL — the password contains "
    "a character that must be percent-encoded (e.g. '/', '@', "
    "':', '?', '#'). Percent-encode the password before "
    "building the URL. Full URL (redacted): {safe_url}"
)


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
    consistently in error messages and logs.  ``urlencode(..., safe='*/')``
    serves two purposes: ``*`` prevents percent-encoding of the '***'
    redaction marker (which would otherwise render as the unreadable
    '%2A%2A%2A'), and ``/`` keeps preserved filesystem-path values readable —
    the non-credential TLS path params (``sslkey``/``sslcert``/``sslrootcert``,
    excluded from redaction so operators can diagnose TLS failures) carry paths
    like ``/etc/ssl/key.pem`` that would otherwise mangle into
    ``%2Fetc%2Fssl%2Fkey.pem``, defeating the diagnostic intent of the
    exclusion.

    Falls back to scheme-only form only when urlparse raises an exception.

    The netloc-password redaction is done TEXTUALLY (split everything after
    ``://`` on the LAST '@', and if the left side contains ':', replace
    everything after the first ':' with '***') rather than via
    ``parsed.password``.  urlparse follows RFC-3986 and treats the first
    unencoded '/', '?' or '#' as the start of the path/query/fragment, so an
    unencoded '/', '?' or '#' in the password makes ``parsed.password`` silently
    become None and the credential would survive verbatim in the "redacted"
    output.  The textual split does not depend on the password being
    percent-encoded, so it redacts even malformed authorities that urlparse
    mis-isolates.

    Security stance — over-redact under ambiguity, never leak:
        An unencoded '/', '?' or '#' in the userinfo makes the URL genuinely
        ambiguous per RFC-3986: ``postgresql://user:pa?ss@host/db`` is textually
        indistinguishable from a credential-less URL whose query string happens
        to contain an '@'.  There is no parse that recovers the operator's
        intent.  Faced with that ambiguity we choose to OVER-redact: any '@'
        after ``://`` is treated as a userinfo separator and the segment after
        the first ':' is masked.  The cost is borne by genuinely credential-less
        URLs that carry an '@' in a query value:

          * Port-LESS form ``postgresql://host/db?application_name=a@b`` redacts
            cleanly to ``...?application_name=a%40b`` — structure intact.
          * Port-PRESENT form ``postgresql://host:5432/db?application_name=a@b``
            redacts to ``postgresql://host:***@b`` — the textual split treats
            ``host:5432/db?application_name=a`` as userinfo and masks everything
            after the first ':'.  This DROPS the dbname (``/db``) and the query
            key, and fabricates a ``host:***@b`` userinfo shape that never
            existed.  The displayed URL is therefore misleading about the URL's
            STRUCTURE for this case — more than "slightly obscured."

        This is accepted because no credential leaks (there is no credential),
        and a structurally-mangled-but-credential-free diagnostic string is
        strictly safer than the alternative (skip redaction when the '@' might be
        in a query string), which is what leaked the password in the pre-fix
        code.  Operators who hit the mangled port-present form should re-run with
        the credential removed to read the real structure.
    """
    try:
        # ── (1) netloc/userinfo password — TEXTUAL, not parsed.password ───
        # Take the LAST '@' after '://' as the userinfo separator.  We do NOT
        # first clamp to an authority boundary on '/', '?' or '#', because any
        # of those characters can appear UNENCODED inside the password — and
        # clamping on them would cut the authority short before the real '@' and
        # leak the credential (exactly the round-2 '/' leak and the round-3
        # '?'/'#' leak).  Over-redacting a query-string '@' is the deliberate,
        # security-first trade-off (see the docstring "Security stance").
        if "://" in url:
            scheme_part, rest = url.split("://", 1)
            at_idx = rest.rfind("@")
            if at_idx != -1:
                userinfo = rest[:at_idx]
                after = rest[at_idx + 1 :]
                if ":" in userinfo:
                    user = userinfo.split(":", 1)[0]
                    userinfo = f"{user}:***"
                url = f"{scheme_part}://{userinfo}@{after}"
        parsed = urlparse(url)
        # ── (2) query-string credentials ─────────────────────────────────
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            redacted = [
                (k, "***") if _is_credential_key(k) else (k, v) for k, v in pairs
            ]
            # safe='*/' keeps the '***' redaction marker literal (not
            # '%2A%2A%2A') AND keeps preserved TLS path values literal
            # (sslkey=/etc/ssl/key.pem, not %2Fetc%2Fssl%2Fkey.pem) — the
            # exclusion of those keys from redaction exists precisely so
            # operators can read the path when diagnosing TLS failures.
            parsed = parsed._replace(query=urlencode(redacted, safe="*/"))
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
        #
        # Compute the redacted URL FIRST so every error path below can surface
        # it without leaking credentials — including the malformed-authority
        # path, where urlparse(url).port itself raises before we ever store
        # _safe_url.
        safe_url = _redact_dsn(url)
        parsed = urlparse(url)

        # Detect a credential that urlparse failed to isolate BEFORE trusting the
        # parsed components — an un-percent-encoded '/', '?' or '#' in the
        # password makes urlparse treat it as the path/query/fragment start, so
        # ``parsed.password`` silently becomes None (or a truncated fragment) and
        # we would connect with garbage components (confusing auth/connect
        # failure).  Two COMPLEMENTARY detection arms are needed because a
        # special-char password fails urlparse in two different ways depending on
        # the special char, and ONLY one of them raises on ``parsed.port``:
        #
        #   Arm 1 — port-cast failure (``?`` / ``#`` passwords, and any password
        #   whose mis-isolated netloc tail is non-numeric).  ``postgresql://
        #   user:pa?ss@host/db`` pushes the real '@' into the query, leaving
        #   urlparse to cast a password fragment as the port → ValueError.  We
        #   must NOT interpolate that stdlib ``exc`` — its text echoes a raw
        #   fragment of the password (e.g. "Port could not be cast … as 'Xy9'").
        #
        #   Arm 2 — port parses cleanly but the '@' landed in ``parsed.path``
        #   (``/`` passwords, and DIGIT-LEADING passwords whose prefix casts as a
        #   valid port).  ``postgresql://user:5432/mypassword@host:5432/db``
        #   parses host='user', port=5432, path='/mypassword@host:5432/db' WITHOUT
        #   raising — Arm 1 never fires, so without Arm 2 the backend silently
        #   constructs with the username as the host and the credentials dropped
        #   (Issue #258 round-5 P1).  The same path catches the double-'@' shape
        #   ``user:p@ss/word@host/db`` where ``parsed.password`` is a fragment.
        #   The discriminator is ``'@' in parsed.path``: a real '@' only reaches
        #   the path when urlparse mis-isolated a special-char password.
        #
        # Conversely, a VALID credential-less URL whose QUERY string contains an
        # '@' (e.g. ``postgresql://host:5432/db?application_name=a@b``) parses
        # cleanly: port is the integer port, ``parsed.path`` is '/db' (no '@'),
        # and the '@' sits in ``parsed.query`` — neither arm fires, so it
        # constructs.  (``_redact_dsn`` still over-masks that query '@' by design
        # — the documented security-first stance — but over-redacting a
        # non-secret is harmless, whereas refusing to construct a valid backend
        # is not.)  Both messages below use only ``safe_url`` (already redacted)
        # and never the raw URL or exception text.
        try:
            # .port is a lazy property that raises ValueError on a non-integer
            # port (e.g. a malformed authority where urlparse mis-isolated the
            # netloc). Force evaluation here so it is caught and re-raised as
            # the friendly, redacted error rather than an uncaught
            # "Port could not be cast to integer value".  Do NOT interpolate the
            # caught ``exc`` — its text can echo a raw fragment of the password.
            port = parsed.port
        except Exception as exc:
            # Arm 1: distinguish "malformed password (special char)" from a
            # generic malformed authority so the operator gets the actionable
            # message.  Using ``rfind('@')`` (not a '/?#'-clamped scan) means a
            # password char that is itself '/', '?' or '#' cannot defeat
            # detection — the same security-first stance as ``_redact_dsn``.
            # Neither message interpolates ``exc`` (its text can echo a raw
            # password fragment).
            if parsed.password is None and "://" in url:
                rest = url.split("://", 1)[1]
                at_idx = rest.rfind("@")
                if at_idx != -1 and ":" in rest[:at_idx]:
                    raise ValueError(
                        _SPECIAL_CHAR_CREDENTIAL_MSG.format(safe_url=safe_url)
                    ) from exc
            raise ValueError(
                f"PostgresLogBackend: malformed URL. "
                f"Expected postgresql://user:pass@host:port/dbname. "
                f"Full URL (redacted): {safe_url}."
            ) from exc

        # Arm 2: port parsed cleanly, but an unencoded '@' in ``parsed.path``
        # means urlparse mis-isolated a special-char password and pushed the real
        # '@' past the authority into the path.  This is the digit-leading and
        # ``/``-password class that Arm 1 cannot see (it never raises on port).
        # A valid credential-less URL puts a query '@' in ``parsed.query``, never
        # in ``parsed.path``, so this arm does not refuse it.
        if "@" in (parsed.path or ""):
            raise ValueError(_SPECIAL_CHAR_CREDENTIAL_MSG.format(safe_url=safe_url))

        if parsed.scheme not in ("postgresql", "postgres"):
            raise ValueError(
                f"PostgresLogBackend: URL must start with postgresql:// or postgres://; "
                f"got scheme {parsed.scheme!r}. "
                f"Full URL (redacted): {safe_url}"
            )

        self._host = parsed.hostname or "localhost"
        self._port = port or 5432
        self._dbname = (parsed.path or "/").lstrip("/") or "postgres"
        self._user = parsed.username or ""
        self._password = parsed.password or ""
        # Store the redacted URL for safe error messages.
        self._safe_url = safe_url

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

        Reconnects when the cached connection is ALREADY flagged broken or
        closed.  Important limitation: psycopg does NOT set ``closed``/``broken``
        on a server-side termination (server restart, idle-connection timeout,
        failover, network blip) until an operation actually touches the dead
        socket.  So after such a drop, the FIRST op on the cached handle still
        raises (e.g. AdminShutdown) — recovery is NOT proactive here.

        Transparent first-call recovery is handled one layer up by
        ``_run_with_reconnect()``, which retries a connection-level failure once
        against a freshly-built connection.  This method only handles the cheap
        "already-flagged-dead" case so the hot append path pays no per-call
        liveness probe (no ``SELECT 1``).
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

    def _get_conn_for_read(self, op: str) -> Any:
        """``_get_conn()`` wrapped for the spec/22 read-failure boundary.

        Mirrors ``SQLiteLogBackend._get_conn_for_read``. The realistic
        connect-time read failure for Postgres is a *connectable-but-corrupt*
        database whose meta-table ``SELECT`` inside ``_ensure_schema()`` raises a
        raw ``psycopg.Error`` on first connect. ``_ensure_schema``'s own
        ``except Exception: ... raise`` re-raises that psycopg error UNWRAPPED,
        so without this helper it would escape the read methods' post-connect
        ``except _psycopg_error()`` wrap (which only surrounds
        ``_run_with_reconnect(_do)``) and surface as a raw psycopg error — the
        one connect-time path the per-call wrap misses.

        Boundary carve-out (matches SQLite + the spec/22 boundary table):
          - ``ValueError`` (could-not-connect, DSN-redacted — raised by
            ``_get_conn`` when ``psycopg.connect`` fails) → config error,
            propagates uncaught.
          - ``RuntimeError`` (schema-version mismatch from ``_ensure_schema``)
            → config error, propagates uncaught.
          - ``psycopg.Error`` (connectable-but-corrupt meta-table SELECT) →
            wrapped as ``LogBackendReadError`` here, so the typed-signal
            contract holds on the connect-time-schema-read path too.
        """
        try:
            return self._get_conn()
        except _psycopg_error() as exc:
            raise LogBackendReadError(
                f"PostgresLogBackend: unrecoverable read failure establishing "
                f"connection in {op}() (connectable-but-corrupt database or "
                f"connect-time I/O error): {exc}"
            ) from exc

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
            # ON CONFLICT DO NOTHING — Postgres equivalent of SQLite's INSERT OR IGNORE.
            # Losing the cold-start race (another process already inserted) is a no-op.
            # Insert the schema_version row BEFORE migrations + index creation so the
            # migration ladder reads the authoritative existing version.
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
            # Schema migration ladder — run UNDER the advisory lock, BEFORE index
            # creation so migrations that ADD columns (which an index references)
            # are applied first. v1 → v2 (spec/45 PR2): add idempotency_key +
            # replayed_run_id columns (spec/22 versioned normative addendum).
            # ADD COLUMN IF NOT EXISTS is idempotent + safe (existing rows get NULL).
            # On a fresh DB the CREATE TABLE already includes ALL columns
            # (idempotency_key, replayed_run_id, AND conversation_id) and the meta
            # row is inserted at the CURRENT _SCHEMA_VERSION (3 today), so BOTH
            # migration blocks below are skipped (existing != 1 and != 2).
            if existing == 1:
                conn.execute(
                    "ALTER TABLE run_records ADD COLUMN IF NOT EXISTS "
                    "idempotency_key TEXT"
                )
                conn.execute(
                    "ALTER TABLE run_records ADD COLUMN IF NOT EXISTS "
                    "replayed_run_id TEXT"
                )
                conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
                existing = 2
            if existing == 2:
                # v2 → v3 (spec/47 PR1): add conversation_id column +
                # idx_conversation_id partial index (spec/22 versioned normative
                # addendum for ConversationBackend). ADD COLUMN IF NOT EXISTS is
                # idempotent + safe (existing rows get NULL).
                conn.execute(
                    "ALTER TABLE run_records ADD COLUMN IF NOT EXISTS "
                    "conversation_id TEXT"
                )
                conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
                existing = 3
            # Create indexes AFTER migrations so all indexed columns exist.
            for stmt in _CREATE_INDEXES:
                conn.execute(stmt)
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

    @staticmethod
    def _is_connection_error(exc: BaseException) -> bool:
        """True if exc is a CONNECTION-level psycopg failure (vs a statement error).

        Connection-level failures (server termination, network drop, SSL teardown)
        are safe to recover by reconnecting and retrying the operation once.
        Statement-level failures (a bad INSERT, a constraint violation, a syntax
        error) are NOT — retrying them would just fail again, possibly masking the
        real bug or, worse, double-applying a write that partly succeeded.

        psycopg 3 raises ``OperationalError`` (and its subclasses, including the
        ``sqlstate``-mapped ``AdminShutdown`` / ``ConnectionFailure`` /
        ``ConnectionException`` classes for 57P0x / 08xxx codes) for
        connection-level problems. ``ProgrammingError`` / ``IntegrityError`` /
        ``DataError`` are statement-level. We classify on ``OperationalError``
        plus the SQLSTATE class codes 08 (connection exception) and 57 (operator
        intervention — includes admin shutdown / crash), which is the robust
        signal across psycopg versions.
        """
        try:
            import psycopg  # noqa: PLC0415

            op_err = getattr(psycopg, "OperationalError", None)
            # Guard against a mocked psycopg (tests) where OperationalError is a
            # MagicMock, not a real class — isinstance() would raise TypeError.
            if isinstance(op_err, type) and isinstance(exc, op_err):
                return True
        except ImportError:
            pass
        # SQLSTATE class fallback — covers any psycopg.Error whose sqlstate is a
        # connection-exception (08xxx) or operator-intervention (57xxx) code,
        # even if a given psycopg version maps it to a different exception class.
        # Also the robust signal when psycopg is mocked or absent.
        sqlstate = getattr(exc, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate[:2] in ("08", "57"):
            return True
        return False

    def _discard_conn(self, conn: Any) -> None:
        """Roll back, close, and drop the calling thread's cached connection.

        Idempotent and exception-safe — used on every error path so a poisoned
        (aborted-transaction or broken) connection never lingers in
        threading.local to fail the NEXT call.
        """
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        self._tls.conn = None

    def _run_with_reconnect(self, op: Any) -> Any:
        """Run ``op(conn)`` against the thread's connection; retry ONCE on a
        connection-level failure with a freshly-built connection.

        This is what makes a transient Postgres drop transparent: the common
        production failure mode is a pooled connection that idles out or is
        terminated server-side (restart/failover/blip). psycopg does not flag
        such a connection dead until an op touches the socket, so the FIRST op
        raises a connection-level error (e.g. AdminShutdown). Without this
        wrapper that first error propagates out of ``append()`` into the
        framework's un-try/excepted ``_log()`` path and aborts the whole agent
        run. Here we discard the dead connection and retry the operation once
        against a fresh one — costing zero records and zero failed runs.

        A SECOND failure (or any statement-level error on the first attempt) is
        re-raised unchanged — we never retry a genuine bad statement, and we
        never loop more than once so a hard-down server fails fast rather than
        hanging.

        Commit-phase non-retry (P1, audit-integrity):
            A write op that has already issued its INSERT must NOT be retried
            once execution reached ``commit()`` — a connection-level error at
            commit can mean the server DID persist the row but the ack was lost
            on the wire (restart/failover/blip during commit). Retrying the
            full closure would issue a SECOND INSERT, silently DOUBLING an audit
            row (no uniqueness column on ``run_records`` — many rows share a
            run_id) and double-counting cost (the cost-guardrail readers sum
            over these rows). A write op signals "past the point of no return"
            by raising ``_CommitPhaseError() from cause``; we re-raise the
            underlying ``cause`` (``__cause__``) WITHOUT retrying, even when it
            is a connection error.
            Read paths (query/tail/aggregate/stats) and the idempotent
            ``delete_older_than`` DELETE never raise ``_CommitPhaseError``, so
            they keep the transparent one-shot retry.
        """
        conn = self._get_conn()
        try:
            return op(conn)
        except _CommitPhaseError as wrapped:
            # The INSERT may already be committed server-side; a second attempt
            # would duplicate the audit row. Discard the (broken) connection but
            # do NOT retry — surface the original cause to the caller.
            self._discard_conn(conn)
            raise wrapped.__cause__ from wrapped.__cause__  # type: ignore[misc]
        except Exception as exc:
            is_conn_err = self._is_connection_error(exc)
            self._discard_conn(conn)
            if not is_conn_err:
                raise
            # One-shot transparent recovery against a fresh connection.
            conn = self._get_conn()
            try:
                return op(conn)
            except _CommitPhaseError as wrapped:
                # Same audit-integrity guard on the retry attempt: a drop at
                # commit() of the SECOND try may also have persisted the row.
                self._discard_conn(conn)
                raise wrapped.__cause__ from wrapped.__cause__  # type: ignore[misc]
            except Exception:
                self._discard_conn(conn)
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
        # Ensure a live connection first — this also guarantees psycopg is
        # importable, so _get_conn()'s friendly ImportError ("requires the
        # 'postgres' extra") fires before the Jsonb import below if psycopg is
        # missing.  _run_with_reconnect() calls _get_conn() again internally;
        # for the normal (non-dropped) case that is the same cached connection,
        # so there is no extra round trip on the hot path.
        self._get_conn()
        # Jsonb import comes AFTER _get_conn() so psycopg is confirmed
        # importable — a cheap sys.modules lookup with no try/except overhead on
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
            record.idempotency_key,
            record.replayed_run_id,
            record.conversation_id,
            _pj.Jsonb(record.extra if record.extra is not None else {}),
        )

        def _do(conn: Any) -> None:
            # A failure HERE (execute) is safe to retry: the INSERT never
            # reached a commit, so the row is not persisted. Let it propagate
            # as a normal exception so _run_with_reconnect can do its one-shot
            # transparent reconnect.
            conn.execute(_INSERT_SQL, values)
            # spec/22 MUST 2: persist before returning.
            #
            # A failure at commit() is the audit-integrity hazard: the server
            # may have ALREADY committed the row and only the ack was lost on
            # the wire (restart/failover/blip during commit). Retrying the
            # closure would issue a SECOND INSERT and silently double the audit
            # row (run_records has no uniqueness column). Wrap the error in
            # _CommitPhaseError so _run_with_reconnect re-raises the cause
            # WITHOUT retrying — at-most-once, never at-least-once, for writes.
            try:
                conn.commit()
            except Exception as exc:
                raise _CommitPhaseError() from exc

        # Transparent one-shot reconnect on a connection-level drop — the first
        # write after a server-side termination would otherwise raise and abort
        # the whole agent run (framework's _log() does not wrap append()).
        # The commit phase is explicitly NON-retryable (see _do above) to keep
        # the audit trail at-most-once.
        self._run_with_reconnect(_do)

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

        def _do(conn: Any) -> list:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows

        # spec/22 read-failure addendum boundary carve-out (mirrors SQLite's
        # NARROW catch). Establish the connection + schema via _get_conn_for_read
        # so config/deploy errors propagate with their own taxonomy
        # (ValueError = could-not-connect, RuntimeError = schema-version
        # mismatch) while a connect-time-schema-read corruption (raw
        # psycopg.Error from _ensure_schema's meta SELECT) is wrapped as
        # LogBackendReadError — the one connect-time path the post-connect wrap
        # below otherwise misses.
        # Return value intentionally discarded: we only need the connect-time
        # corruption wrap here; the actual conn is (cheaply) re-fetched from the
        # thread-local cache inside _run_with_reconnect → _get_conn below.
        self._get_conn_for_read("query")
        # Wrap ONLY psycopg errors that survive the one-shot reconnect —
        # corruption / I/O error / connection drop after retry. We catch
        # ``psycopg.Error`` (the base class, mirroring _get_conn's
        # ``except psycopg.Error`` at line 622), NOT bare ``Exception``: a
        # statement-level psycopg error (ProgrammingError / syntax error) is a
        # genuine read failure at this point, but a non-psycopg bug in the query
        # builder or _do closure is a CODE DEFECT that MUST surface as itself,
        # not be relabeled a transient read failure (which the cost reader would
        # silently degrade the gate on). This also keeps the catch breadth
        # symmetric with SQLite's narrow ``sqlite3.DatabaseError`` catch. A
        # ValueError/RuntimeError raised by _get_conn() on the INNER reconnect
        # path (server hard-down / schema mismatch on the reconnect target) is
        # NOT a psycopg.Error, so it too propagates uncaught — matching the
        # boundary table's "config error, NOT a read failure" row on every path,
        # not just first-connect.
        try:
            rows = self._run_with_reconnect(_do)
        except _psycopg_error() as exc:
            raise LogBackendReadError(
                f"PostgresLogBackend: unrecoverable read failure in query(): {exc}"
            ) from exc
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

        def _do(conn: Any) -> list:
            rows = conn.execute(
                "SELECT * FROM run_records ORDER BY ts DESC, id DESC LIMIT %s",
                (n,),
            ).fetchall()
            conn.commit()
            return rows

        # spec/22 read-failure addendum boundary carve-out (mirrors SQLite's
        # NARROW catch). Establish connection + schema via _get_conn_for_read so
        # first-connect ValueError (could-not-connect) / RuntimeError
        # (schema-version mismatch) propagate uncaught as config errors, while a
        # connect-time-schema-read corruption (raw psycopg.Error) is wrapped as
        # LogBackendReadError. ValueError (negative n) is raised above.
        # Return value intentionally discarded (see query() — conn is re-fetched
        # from the thread-local cache inside _run_with_reconnect below).
        self._get_conn_for_read("tail")
        # Wrap ONLY psycopg.Error surviving the one-shot reconnect (see query()
        # for the full rationale: narrow catch keeps code defects + config
        # errors from being relabeled transient read failures, and stays
        # symmetric with SQLite).
        try:
            rows = self._run_with_reconnect(_do)
        except _psycopg_error() as exc:
            raise LogBackendReadError(
                f"PostgresLogBackend: unrecoverable read failure in tail(): {exc}"
            ) from exc
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

        # Transaction discipline: commit() on success, rollback() + discard +
        # one-shot reconnect on error — same shape as query() / tail() / stats().
        def _do(conn: Any) -> list:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows

        # spec/22 read-failure addendum boundary carve-out (mirrors SQLite's
        # NARROW catch). Establish connection + schema via _get_conn_for_read so
        # first-connect ValueError (could-not-connect) / RuntimeError
        # (schema-version mismatch) propagate uncaught as config errors, while a
        # connect-time-schema-read corruption (raw psycopg.Error) is wrapped as
        # LogBackendReadError. ValueError (unknown metric, invalid group_by) is
        # raised above.
        # Return value intentionally discarded (see query() — conn is re-fetched
        # from the thread-local cache inside _run_with_reconnect below).
        self._get_conn_for_read("aggregate")
        # Wrap ONLY psycopg.Error surviving the one-shot reconnect (see query()
        # for the full rationale).
        try:
            rows = self._run_with_reconnect(_do)
        except _psycopg_error() as exc:
            raise LogBackendReadError(
                f"PostgresLogBackend: unrecoverable read failure in aggregate(): {exc}"
            ) from exc

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

        def _do(conn: Any) -> int:
            cur = conn.execute(
                "DELETE FROM run_records WHERE ts < %s",
                (threshold.isoformat(),),
            )
            # Read rowcount BEFORE commit — psycopg 3 may reset after commit.
            count = cur.rowcount
            conn.commit()
            return count

        # One-shot reconnect on a connection-level drop. Unlike append(), a
        # commit-phase drop here is NOT wrapped in _CommitPhaseError (see the
        # _run_with_reconnect docstring), so the whole DELETE stays retryable on
        # every drop. That is data-safe in both directions:
        #   * drop BEFORE commit — the first attempt's transaction never
        #     persisted, so the retry removes the same rows once (no double
        #     delete).
        #   * commit succeeded server-side but the ack was LOST on the wire — the
        #     retry re-runs the DELETE, which is idempotent (re-running deletes
        #     whatever rows remain below the threshold). The only consequence is
        #     the RETURNED count can under-report on that rare path: the first
        #     attempt removed N rows and committed, the retry finds 0 below the
        #     threshold and returns 0. That is acceptable — the retention
        #     contract (spec/22 MUST 1-4) is about idempotency + atomicity, not
        #     exact return counts, and the audit rows are correctly gone either
        #     way. Unlike append(), an under-counted delete cannot double an
        #     audit row, so at-most-once is not required here.
        return self._run_with_reconnect(_do)

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
        # Local-timezone day + month windows — matches FilesystemLogBackend
        # (date.today()) and SQLiteLogBackend (date.today()) so that
        # records_today / records_this_month are consistent across all three
        # backends for the same data.
        # Use .astimezone() on each boundary (not tzinfo= on datetime.combine)
        # so that the UTC offset is resolved for THAT instant — on DST-transition
        # days spring-forward/fall-back changes the offset at midnight, and
        # combine(..., tzinfo=now.tzinfo) pins tonight's offset onto midnight,
        # producing the wrong ISO string on those ~2 days per year.
        # Mirrors SQLiteLogBackend.stats() exactly: date.today() for the day,
        # then combine(..., dt_time.min).astimezone() for each boundary.
        today = date.today()
        today_start = datetime.combine(today, dt_time.min).astimezone().isoformat()
        today_end = datetime.combine(today, dt_time.max).astimezone().isoformat()
        first_of_month = today.replace(day=1)
        month_start = (
            datetime.combine(first_of_month, dt_time.min).astimezone().isoformat()
        )

        # Transaction discipline: commit() on success, rollback() + discard +
        # one-shot reconnect on error — same shape as query() / tail() /
        # aggregate() / delete_older_than().
        def _do(conn: Any) -> Any:
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
            return row

        row = self._run_with_reconnect(_do)

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
        # spec/22 versioned normative addendum (spec/45 PR2): idempotency_key
        # AND-predicate. Uses the idx_idempotency_key index for an index seek.
        if filter.idempotency_key is not None:
            clauses.append("idempotency_key = %s")
            params.append(filter.idempotency_key)
        # spec/22 versioned normative addendum (spec/47 PR1): conversation_id
        # AND-predicate. Uses the idx_conversation_id partial index for an index
        # seek. Without this clause LogQuery(conversation_id=...) would silently
        # return ALL records — the spec MUST says it returns only matching ones.
        if filter.conversation_id is not None:
            clauses.append("conversation_id = %s")
            params.append(filter.conversation_id)

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
            # spec/45 PR2 / spec/22 addendum: idempotency audit fields. After the
            # v1→v2 migration these columns ALWAYS exist (NULL on old rows, never
            # absent), so direct subscript is correct and symmetric with SQLite's
            # _row_to_record. Unit-test row dicts carry all columns (including these
            # two) for the same reason — prod shape is not bent to test convenience.
            idempotency_key=row["idempotency_key"],
            replayed_run_id=row["replayed_run_id"],
            # spec/47 PR1 / spec/22 addendum: conversation audit field. After the
            # v2→v3 migration this column ALWAYS exists (NULL on old rows, never
            # absent), so direct subscript is correct and symmetric with SQLite's
            # _row_to_record.
            conversation_id=row["conversation_id"],
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

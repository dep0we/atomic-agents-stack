"""PostgresMemoryBackend — psycopg 3 reference implementation for the MemoryBackend Protocol.

This module is PR 1 of the 4-PR Postgres adapter arc for issue #258.
PR1 ships: non-semantic FTS (tsvector) recall, Tier-B field-lossless export,
spec/20 Postgres addendum, and conformance factory entry.
EmbeddingBackend Protocol (#200) shipped as atomic_agents/embedding/ (DRAFT spec/46, PR2 of #200).
pgvector wiring into this module is explicitly deferred to PR2/PR3 of #258.

Install:
    pip install 'atomic-agents-stack[postgres]'

Usage:
    ATOMIC_AGENTS_MEMORY_BACKEND=postgres
    ATOMIC_AGENTS_MEMORY_BACKEND_URL=postgresql://user:password@host:5432/dbname

Value over FilesystemBackend for scaled deployments:
    * **Multi-host fleet**: multiple Cloud Run replicas share one backend.
    * **Networked storage**: notes survive beyond any single host.
    * **FTS/tsvector search**: full-text search over note display names, derived
      names, descriptions, and bodies via Postgres tsvector — no embedding required.

WritePolicy semantics for Postgres:
    spec/20 MUST 5 requires every mutating operation to verify the target is
    under write_paths. For a Postgres backend there is no filesystem path;
    all notes are row-addressed. The enforcement strategy: verify that
    ``agent_root`` resolves under at least one entry in ``policy.write_paths``
    (the caller's authorization scope). If ``write_paths`` is empty, raise
    ``WritePathViolation`` — an empty allowed list blocks all writes. If
    ``agent_root`` is not under any write_paths entry, raise
    ``WritePathViolation``. read_only_paths is enforced FIRST: if
    ``agent_root`` falls under any ``read_only_paths`` entry the write is
    blocked (parity with FilesystemBackend, which checks read_only before
    write_paths).

    This is explicitly a Postgres-scope interpretation of MUST 5, documented
    in the spec/20 Postgres addendum. Path-containment checks on SQL rows are
    not possible; the agent_root scope check is the closest meaningful analog.

supports_semantic_search = False:
    FTS (tsvector) search is NOT semantic/vector search. The search() method
    performs full-text search over tsvector computed inline on the
    display_name, name, description, and body columns. Callers requiring
    embedding-based semantic recall must wait for #258 PR2 (pgvector wiring
    using the EmbeddingBackend Protocol, now shipped as atomic_agents/embedding/).
    FTS is a non-semantic search mode (distinct from substring). LOCKED MUST 7
    requires search() on a non-semantic backend to raise NotImplementedError
    OR return an empty list consistently; returning FTS matches instead is the
    project's own interpretation, consistent with FilesystemBackend.search()'s
    substring behavior under supports_semantic_search=False (a non-semantic
    search mode, not a new spec authority). See the spec/20 Postgres addendum
    §"FTS search (MUST 7 compliance)".

supports_canonical_export = True (Tier B field-lossless):
    PR1 ships Tier B export using render_note_bytes_from_object(note) from
    atomic_agents/export/renderer.py. Field-level round-trip is guaranteed;
    byte-exact round-trip is NOT (date formatting and key ordering diverge
    per spec/40 §"Tier A vs Tier B fidelity").

DreamRunner integration:
    DreamRunner currently guards against non-filesystem backends at construction
    and raises NotImplementedError (#396 tracks the backend-agnostic adopt path).
    Use PostgresMemoryBackend for agent.call() captures and versioning only
    in PR1; dream pipeline support ships when #396 lands.
    create_staging() and apply_staging() are implemented in this module, but
    DreamRunner.apply() cannot call them until #396 ships.

VersionRef encoding:
    backend_id is a string representation of the note_versions row id
    (e.g., "42"). The CLI token accepted by resolve_version_token() is this
    same row-id string. VersionRef.__str__() returns the id verbatim (no /
    split). Operators use list_versions() to retrieve valid tokens.

    This is the Postgres encoding; it does NOT use the FilesystemBackend's
    <stem>/<version_filename> encoding.

Schema (independent _SCHEMA_VERSION = 2; NOT shared with LogBackend):
    * Table ``memory_notes``: one row per live note.
      Typed columns: name (derived filename — the row address), display_name
      (the human note name = capture.name, round-trips to Note.name),
      type, description, captured, last_seen, pinned, confidence, archived,
      superseded_by, body, sources JSONB, tags JSONB, supersedes, merge_into,
      expires_at, schema_version, extra_frontmatter JSONB,
      content_hash (SHA-256 of deterministic canonical fields for CAS).
      v1->v2 migration: ADD COLUMN display_name (idempotent; legacy v1 rows
      get '' and fall back to the derived filename on read).
    * Table ``memory_note_versions``: one row per version snapshot.
    * Tables ``memory_staging_notes_<uuid>`` / ``memory_staging_note_versions_<uuid>``:
      per-staging-session REGULAR (not Postgres TEMPORARY) tables, created by
      create_staging() and explicitly dropped by discard_staging() or
      apply_staging(). Regular tables are required so the apply connection can
      see staging rows under the per-thread connection model.
    * Table ``memory_meta``: schema version tracking.

    All tables are distinct from LogBackend (run_records, meta) — multiple
    atomic-agents backends can share a Postgres database without collision
    because their tables are namespaced separately.

Thread-safety:
    threading.local gives each thread its own psycopg connection.
    Per-thread connections are tracked in a thread-safe list so close()
    from the main thread releases all worker-thread connections — critical
    because helper_call_parallel() spawns worker threads that open connections
    for memory captures and exit without calling close() themselves.

Cold-start race mitigation:
    _ensure_schema() acquires pg_advisory_xact_lock inside a transaction.
    The advisory lock key is derived from b'atomic-agents-memory-schema-v1'
    (distinct from the LogBackend key to avoid serializing unrelated DDL).

Credential redaction:
    Three layers (mirroring PostgresLogBackend):
    (A) All logged URLs stripped of credentials via _redact_dsn().
    (B) psycopg opened with explicit keyword args; psycopg logger suppressed.
    (C) Full URL string NOT retained; only _safe_url (redacted) stored.
        Password IS stored as _password for driver use — __dict__ exposes it.

Paramstyle note:
    psycopg 3 uses %s positional placeholders (paramstyle='pyformat'), NOT ?
    (paramstyle='qmark' from sqlite3). Every parameterized statement uses %s.

MemoryStats.live_bytes / version_history_bytes:
    Uses pg_total_relation_size() wrapped in try/except; falls back to -1
    (sentinel for 'size unavailable' — some managed Postgres restrict this
    query). The dashboard currently renders this sentinel as '-1 B' via
    _fmt_bytes(); mapping -1 to 'N/A' is tracked as follow-up #529.

Known Tier B export caveats (spec/40 §"Tier A vs Tier B fidelity"):
    * date objects serialize as unquoted YAML (2026-06-01 not '2026-06-01').
    * extra_frontmatter keys are sorted alphabetically by frontmatter.dumps().
    * These divergences are documented; field-level round-trip is guaranteed.

Known cross-backend divergence (accepted, tracked #366-adjacent):
    extra_frontmatter retrieved via JSONB returns native Python types (psycopg 3
    decodes JSONB to dict natively — integers stay integers, booleans stay
    booleans). This is BETTER than the LogBackend's ->> text-extract divergence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import struct
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

from .backend import (
    MemoryStats,
    Note,
    NoteRef,
    StagedMemory,
    VersionRef,
    WritePolicy,
)
from .._schema import CURRENT_SCHEMA_VERSION, derive_filename
from ..exceptions import (
    MemoryBackendError,
    MemoryPreconditionFailed,
    SchemaValidationError,
    StagingNotApplied,
    VersionNotFound,
    WritePathViolation,
)

if TYPE_CHECKING:
    from ..types import Capture

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# Schema versioning — INDEPENDENT of LogBackend, SQLite, etc.

# v1: initial schema. v2: added display_name to memory_notes /
# memory_note_versions so the human note name (capture.name) round-trips to
# Note.name (cross-backend parity with FilesystemBackend's frontmatter `name`).
_SCHEMA_VERSION = 2

# Advisory lock key: distinct from PostgresLogBackend's key
# (b'atomic-agents-log-schema-v1') so log and memory cold-start DDL
# serialize independently even when sharing a Postgres database.
# Same derivation: struct.unpack('>q', sha256(seed)[:8]).
_ADVISORY_LOCK_KEY: int = struct.unpack(
    ">q",
    hashlib.sha256(b"atomic-agents-memory-schema-v1").digest()[:8],
)[0]

# ──────────────────────────────────────────────────────────────────
# Credential redaction (mirrored from logs/postgres.py — NOT shared
# module to keep the two backends independently deployable; a shared
# _postgres_util.py refactor is tracked as a follow-up issue).

_CREDENTIAL_QUERY_KEYS = frozenset({"password", "sslpassword"})

_PATH_VALUED_KEYS = frozenset({"sslkey", "sslcert", "sslrootcert"})

_SPECIAL_CHAR_CREDENTIAL_MSG = (
    "PostgresMemoryBackend: malformed URL — the password contains "
    "a character that must be percent-encoded (e.g. '/', '@', "
    "':', '?', '#'). Percent-encode the password before "
    "building the URL. Full URL (redacted): {safe_url}"
)


def _is_credential_key(key: str) -> bool:
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

    Identical security posture as PostgresLogBackend._redact_dsn() — see
    that module for the full security-stance documentation. This is an
    independent copy (not imported from logs/postgres.py) to keep backends
    independently deployable without cross-module coupling.
    """
    try:
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
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            redacted = [
                (k, "***") if _is_credential_key(k) else (k, v) for k, v in pairs
            ]
            parsed = parsed._replace(query=urlencode(redacted, safe="*/"))
        return parsed.geturl()
    except Exception:
        pass
    if "://" in url:
        scheme = url.split("://", 1)[0]
        return f"{scheme}://..."
    return url


# ──────────────────────────────────────────────────────────────────
# DDL — Postgres-native types

_CREATE_MEMORY_NOTES = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    captured DATE,
    last_seen DATE,
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    confidence TEXT NOT NULL DEFAULT 'medium',
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    superseded_by TEXT,
    body TEXT NOT NULL DEFAULT '',
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    supersedes TEXT,
    merge_into TEXT,
    expires_at TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    extra_frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash TEXT NOT NULL DEFAULT ''
)
"""

_CREATE_MEMORY_NOTE_VERSIONS = """
CREATE TABLE IF NOT EXISTS memory_note_versions (
    id BIGSERIAL PRIMARY KEY,
    note_name TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    captured DATE,
    last_seen DATE,
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    confidence TEXT NOT NULL DEFAULT 'medium',
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    superseded_by TEXT,
    body TEXT NOT NULL DEFAULT '',
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    supersedes TEXT,
    merge_into TEXT,
    expires_at TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    extra_frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash TEXT NOT NULL DEFAULT ''
)
"""

_CREATE_MEMORY_META = """
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_MEMORY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_memory_notes_type ON memory_notes(type)",
    "CREATE INDEX IF NOT EXISTS idx_memory_notes_archived ON memory_notes(archived)",
    "CREATE INDEX IF NOT EXISTS idx_memory_notes_pinned ON memory_notes(pinned)",
    "CREATE INDEX IF NOT EXISTS idx_memory_notes_last_seen ON memory_notes(last_seen)",
    "CREATE INDEX IF NOT EXISTS idx_memory_note_versions_note_name ON memory_note_versions(note_name)",
    "CREATE INDEX IF NOT EXISTS idx_memory_note_versions_snapshotted_at ON memory_note_versions(snapshotted_at)",
]

# Column list for INSERT into memory_notes (excludes id)
_NOTES_INSERT_COLUMNS = (
    "name",
    "display_name",
    "type",
    "description",
    "captured",
    "last_seen",
    "pinned",
    "confidence",
    "archived",
    "superseded_by",
    "body",
    "sources",
    "tags",
    "supersedes",
    "merge_into",
    "expires_at",
    "schema_version",
    "extra_frontmatter",
    "content_hash",
)

_NOTES_INSERT_SQL = (
    "INSERT INTO memory_notes ("
    + ", ".join(_NOTES_INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join(["%s"] * len(_NOTES_INSERT_COLUMNS))
    + ")"
)

# Column list for INSERT into versions table (excludes id, snapshotted_at).
# note_name is sourced from the live note's `name` column when snapshotting
# (see _versions_params_from_note_row); display_name maps 1:1.
_VERSIONS_INSERT_COLUMNS = (
    "note_name",
    "display_name",
    "type",
    "description",
    "captured",
    "last_seen",
    "pinned",
    "confidence",
    "archived",
    "superseded_by",
    "body",
    "sources",
    "tags",
    "supersedes",
    "merge_into",
    "expires_at",
    "schema_version",
    "extra_frontmatter",
    "content_hash",
)

# JSONB columns among the version-insert columns: when copied from a live
# memory_notes row (read back by psycopg as Python dict/list), they MUST be
# re-serialized to a JSON TEXT param — psycopg cannot adapt a raw dict to %s.
# (supersedes is TEXT, superseded_by is TEXT — not in this set.)
_VERSIONS_JSONB_COLUMNS = frozenset({"sources", "tags", "extra_frontmatter"})

_VERSIONS_INSERT_SQL = (
    "INSERT INTO memory_note_versions ("
    + ", ".join(_VERSIONS_INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join(["%s"] * len(_VERSIONS_INSERT_COLUMNS))
    + ")"
)


class _CommitPhaseError(Exception):
    """Internal sentinel: a write op failed at/after commit().

    Identical purpose to _CommitPhaseError in logs/postgres.py — prevents
    retry after a post-commit connection drop that may have already committed.
    Never escapes the module.
    """


# S1: Regex for valid staging table names. UUID hex suffix is 16 lowercase hex
# chars, generated by uuid.uuid4().hex[:16] — alphanumeric lowercase.
# Pattern: memory_staging_(notes|note_versions)_<16 hex chars>
_STAGING_TABLE_RE = re.compile(r"^memory_staging_(notes|note_versions)_[0-9a-f]{16}$")


def _validate_staging_table(table_name: str) -> str:
    """S1: Validate a staging table name before f-string interpolation into SQL.

    Raises ValueError if the name does not match the expected pattern.
    Returns the table name unchanged when valid.

    This is the SQL-injection guard for dynamically-named staging tables.
    Table names are UUID-generated at creation, but the _staging_notes_table
    and _staging_versions_table attributes are mutable string attributes, so
    we validate at every interpolation site rather than trusting the source.
    """
    if not _STAGING_TABLE_RE.match(table_name):
        raise ValueError(
            f"PostgresMemoryBackend: invalid staging table name {table_name!r}; "
            f"expected pattern memory_staging_(notes|note_versions)_<16 hex chars>. "
            f"This is a security guard against SQL injection via mutated table names."
        )
    return table_name


# ──────────────────────────────────────────────────────────────────
# PostgresStagedMemory


class PostgresStagedMemory(StagedMemory):
    """Postgres staging area backed by uuid-named regular tables.

    Created by PostgresMemoryBackend.create_staging(). The staging tables
    are named memory_staging_notes_<uuid> and memory_staging_note_versions_<uuid>
    so multiple concurrent staging sessions (multi-agent fleet) do not collide.
    They are regular (NOT Postgres TEMPORARY) tables, explicitly dropped by
    apply_staging() or discard_staging(). Regular tables are required because
    the per-thread connection model (threading.local) means apply_staging() may
    run on a different connection than create_staging() — a genuine TEMPORARY
    table is session/connection-scoped and would be invisible to the apply
    connection.

    DreamRunner.apply() is blocked for this backend until #396 ships —
    create_staging() and apply_staging() are implemented but DreamRunner
    cannot call them. Use apply_staging() directly for programmatic staging.
    """

    def __init__(
        self,
        backend_id: str,
        backend: "PostgresMemoryBackend",
        staging_notes_table: str,
        staging_versions_table: str,
    ) -> None:
        super().__init__(backend_id)
        self._backend = backend
        self._staging_notes_table = staging_notes_table
        self._staging_versions_table = staging_versions_table
        self._applied: bool = False
        self._discarded: bool = False

    def _check_active(self) -> None:
        if self._applied or self._discarded:
            raise StagingNotApplied(
                f"StagedMemory {self.backend_id!r} has already been "
                + ("applied" if self._applied else "discarded")
                + " — cannot operate on it"
            )

    def write_note(self, capture: "Capture", policy: WritePolicy) -> NoteRef:
        """Write a capture to the staging tables, enforcing policy."""
        self._check_active()
        # Enforce policy against agent_root scope (mirrors live backend)
        self._backend._enforce_postgres_write_policy(policy)
        today = date.today()
        filename = derive_filename(capture.type, capture.name)
        content_hash = _compute_content_hash(
            capture.type, capture.name, capture.description, capture.body
        )

        # S1: validate staging table name before interpolation into SQL
        sn = _validate_staging_table(self._staging_notes_table)

        def _do(conn: Any) -> None:
            # Upsert into staging notes table
            conn.execute(
                f"INSERT INTO {sn} ("
                + ", ".join(_NOTES_INSERT_COLUMNS)
                + ") VALUES ("
                + ", ".join(["%s"] * len(_NOTES_INSERT_COLUMNS))
                + ") ON CONFLICT (name) DO UPDATE SET "
                + ", ".join(
                    f"{c} = EXCLUDED.{c}" for c in _NOTES_INSERT_COLUMNS if c != "name"
                ),
                _note_insert_params(capture, today, content_hash),
            )
            conn.commit()

        self._backend._run_with_reconnect(_do)
        return NoteRef(
            name=filename,
            type=capture.type,
            description=capture.description,
            captured=today,
            last_seen=today,
            pinned=capture.pinned,
            confidence=capture.confidence,
            archived=False,
            superseded_by=None,
        )

    def render_index_summary(self) -> str:
        """Return a generated index summary from the staging notes table."""
        try:
            refs = self._backend._list_notes_from_table(self._staging_notes_table)
        except Exception:
            return "# Memory Index\n\n(staging — no notes yet)\n"
        return _generate_index_summary(refs)

    def stats(self) -> MemoryStats:
        """Return aggregate statistics for the staging area."""
        try:
            refs = self._backend._list_notes_from_table(self._staging_notes_table)
        except Exception:
            return MemoryStats(
                total_notes=0,
                by_type={},
                live_bytes=0,
                version_history_bytes=0,
                most_churned=[],
            )
        by_type: dict[str, int] = {}
        for r in refs:
            by_type[r.type] = by_type.get(r.type, 0) + 1
        return MemoryStats(
            total_notes=len(refs),
            by_type=by_type,
            live_bytes=0,
            version_history_bytes=0,
            most_churned=[],
        )


# ──────────────────────────────────────────────────────────────────
# PostgresMemoryBackend


class PostgresMemoryBackend:
    """Postgres-backed MemoryBackend — FTS/tsvector recall, multi-host-safe.

    Conforms to the MemoryBackend Protocol (spec/20). Constructed via the
    MUST-1 uniform signature (agent_root: Path, *, lock_backend=None) or via
    the make_postgres_memory_backend_from_url() factory. The connection URL
    is read from ATOMIC_AGENTS_MEMORY_BACKEND_URL when not supplied directly.

    See module docstring for full documentation.
    """

    @property
    def implementation_id(self) -> str:
        """Stable impl-level identifier for diagnostics: ``"postgres"``.

        spec/20 MUST 3 reserves the name ``backend_id`` for note/version/
        staging handles and directs impls wanting a stable id to name it
        ``implementation_id`` (#397). We follow that here.
        """
        return "postgres"

    def __init__(
        self,
        agent_root: Path,
        *,
        lock_backend=None,
        url: str | None = None,
    ) -> None:
        """Construct a PostgresMemoryBackend.

        MUST-1 uniform signature: (agent_root, *, lock_backend=None).
        The ``url`` kwarg (default None) satisfies MUST-1: callers that
        satisfy MUST-1 (no url arg) trigger env-var reading inside __init__.
        The make_postgres_memory_backend_from_url() factory passes url=...
        explicitly.

        Args:
            agent_root: Path to the agent's root directory (used for
                agent-scoping and WritePolicy enforcement).
            lock_backend: Optional LockBackend for apply_staging() serialization.
                When None, resolves via get_default_lock_backend(agent_root).
            url: Connection URL (postgresql://...). When None, reads from
                ATOMIC_AGENTS_MEMORY_BACKEND_URL env var. Raises ValueError
                when both are absent.
        """
        import os

        # Set the connection-registry attributes FIRST, before any validation
        # that can raise. If __init__ raises mid-construction (e.g. a bad URL),
        # __del__ -> close() still runs on GC and must not AttributeError on a
        # missing _conn_list_lock / _all_conns / _tls.
        self._tls = threading.local()
        self._conn_list_lock = threading.Lock()
        self._all_conns: list[Any] = []  # all open connections, for close()

        self._agent_root = Path(agent_root).resolve()

        # Resolve URL from kwarg or env var
        if url is None:
            url = os.environ.get("ATOMIC_AGENTS_MEMORY_BACKEND_URL")
        if not url:
            raise ValueError(
                "PostgresMemoryBackend requires a connection URL. "
                "Set ATOMIC_AGENTS_MEMORY_BACKEND_URL=postgresql://user:pass@host:5432/dbname "
                "or pass url= to the constructor."
            )

        # Compute redacted URL FIRST so every error path below can surface it
        safe_url = _redact_dsn(url)
        parsed = urlparse(url)

        # Detect malformed credentials (same two-arm approach as PostgresLogBackend)
        try:
            port = parsed.port
        except Exception as exc:
            if parsed.password is None and "://" in url:
                rest = url.split("://", 1)[1]
                at_idx = rest.rfind("@")
                if at_idx != -1 and ":" in rest[:at_idx]:
                    raise ValueError(
                        _SPECIAL_CHAR_CREDENTIAL_MSG.format(safe_url=safe_url)
                    ) from exc
            raise ValueError(
                f"PostgresMemoryBackend: malformed URL. "
                f"Expected postgresql://user:pass@host:port/dbname. "
                f"Full URL (redacted): {safe_url}."
            ) from exc

        # Arm 2: port parsed cleanly but '@' in path means mis-isolated password
        if "@" in (parsed.path or ""):
            raise ValueError(_SPECIAL_CHAR_CREDENTIAL_MSG.format(safe_url=safe_url))

        if parsed.scheme not in ("postgresql", "postgres"):
            raise ValueError(
                f"PostgresMemoryBackend: URL must start with postgresql:// or postgres://; "
                f"got scheme {parsed.scheme!r}. "
                f"Full URL (redacted): {safe_url}"
            )

        self._host = parsed.hostname or "localhost"
        self._port = port or 5432
        self._dbname = (parsed.path or "/").lstrip("/") or "postgres"
        # C6: percent-decode username/password so encoded special chars (e.g.
        # "p%40ss" → "p@ss") reach psycopg correctly. urlparse does NOT decode
        # percent-encoding on parsed.username / parsed.password automatically.
        # _safe_url is already redacted (computed before we decoded), so the
        # decoded credential never appears in error messages.
        self._user = unquote(parsed.username or "")
        self._password = unquote(parsed.password or "")
        self._safe_url = safe_url

        # Suppress psycopg INFO-level logs that may echo DSN components.
        logging.getLogger("psycopg").setLevel(logging.WARNING)
        logging.getLogger("psycopg.pool").setLevel(logging.WARNING)

        # (_tls / _conn_list_lock / _all_conns are initialized at the top of
        # __init__ so partial construction is GC-safe — see above.)

        # LockBackend for apply_staging() serialization
        if lock_backend is None:
            from ..locks import get_default_lock_backend

            self._lock_backend = get_default_lock_backend(agent_root)
        else:
            self._lock_backend = lock_backend

        # Lock timeout for apply_staging (mirrors FilesystemBackend)
        self._apply_staging_lock_timeout: float = 30.0

    def __repr__(self) -> str:
        """S3: Omit _password so vars()/debug dumps cannot leak credentials."""
        return (
            f"PostgresMemoryBackend("
            f"host={self._host!r}, port={self._port!r}, "
            f"dbname={self._dbname!r}, user={self._user!r}, "
            f"agent_root={str(self._agent_root)!r})"
        )

    # ────────────────────────────────────────────────────────────
    # Connection management

    def _get_conn(self) -> Any:
        """Return the calling thread's connection — lazy-create on first use.

        Reconnects when the cached connection is already flagged broken.
        """
        conn = getattr(self._tls, "conn", None)
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
                    "PostgresMemoryBackend requires the 'postgres' extra. "
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
                raise ValueError(
                    f"PostgresMemoryBackend: could not connect to Postgres at "
                    f"{self._safe_url}. Check ATOMIC_AGENTS_MEMORY_BACKEND_URL. "
                    f"Run atomic-agents doctor for details."
                ) from None

            try:
                self._ensure_schema(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            self._tls.conn = conn
            # Register in global list so close() can release all threads' conns
            with self._conn_list_lock:
                self._all_conns.append(conn)
        return conn

    def _discard_conn(self, conn: Any) -> None:
        """Roll back, close, and drop the calling thread's cached connection.

        C2: also removes the connection from _all_conns so close() does not
        accumulate stale references across reconnects. Without this, _all_conns
        grows indefinitely when the backend reconnects on transient failures.
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
        # C2: remove from the global tracking list so close() doesn't hold
        # a reference to a connection that is already closed.
        with self._conn_list_lock:
            self._all_conns[:] = [c for c in self._all_conns if c is not conn]

    def _run_with_reconnect(self, op: Any) -> Any:
        """Run op(conn) with one reconnect retry on connection-level failure."""
        conn = self._get_conn()
        try:
            return op(conn)
        except _CommitPhaseError as wrapped:
            self._discard_conn(conn)
            raise wrapped.__cause__ from wrapped.__cause__
        except Exception as exc:
            if self._is_connection_error(exc):
                self._discard_conn(conn)
                conn2 = self._get_conn()
                try:
                    return op(conn2)
                except _CommitPhaseError as wrapped2:
                    self._discard_conn(conn2)
                    raise wrapped2.__cause__ from wrapped2.__cause__
                except Exception:
                    self._discard_conn(conn2)
                    raise
            self._discard_conn(conn)
            raise

    @staticmethod
    def _is_connection_error(exc: BaseException) -> bool:
        """True if exc is a CONNECTION-level psycopg failure."""
        try:
            import psycopg  # noqa: PLC0415

            op_err = getattr(psycopg, "OperationalError", None)
            if isinstance(op_err, type) and isinstance(exc, op_err):
                return True
        except ImportError:
            pass
        sqlstate = getattr(exc, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate[:2] in ("08", "57"):
            return True
        return False

    def _ensure_schema(self, conn: Any) -> None:
        """Create tables + indexes if missing; run migrations; assert version.

        DDL ordering (required — see module docstring):
        1. pg_advisory_xact_lock — serializes cold-start across N replicas
        2. CREATE TABLE IF NOT EXISTS (all tables)
        3. CREATE TABLE memory_meta IF NOT EXISTS
        4. INSERT meta ON CONFLICT DO NOTHING
        5. SELECT existing version
        6. Run migration ladder (ALTER TABLE for v1→vN)
        7. UPDATE meta version
        8. CREATE INDEX IF NOT EXISTS (AFTER migrations so columns exist)
        9. COMMIT — releases advisory lock

        The advisory lock key b'atomic-agents-memory-schema-v1' is DISTINCT
        from the LogBackend key so log and memory DDL serialize independently.
        """
        try:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            conn.execute(_CREATE_MEMORY_NOTES)
            conn.execute(_CREATE_MEMORY_NOTE_VERSIONS)
            conn.execute(_CREATE_MEMORY_META)

            # C5: Inspect existing schema state BEFORE seeding the meta row so
            # a pre-existing v1 DB (tables present, meta row absent) is not
            # incorrectly treated as a fresh v2 DB. The INSERT ... ON CONFLICT DO
            # NOTHING seeds _SCHEMA_VERSION=2 for a fresh DB. For an existing DB
            # the meta row already exists, so the INSERT is a no-op and we read
            # the authoritative existing version below.
            #
            # Detection strategy: after the INSERT we read the meta row. If it
            # reads back _SCHEMA_VERSION (2), we still verify that the tables
            # actually have the v2 columns. If display_name is absent from
            # memory_notes, the real existing version is 1 — we correct the meta
            # row before running the migration ladder so `existing` is accurate.
            conn.execute(
                "INSERT INTO memory_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
            cur = conn.execute(
                "SELECT value FROM memory_meta WHERE key = %s", ("schema_version",)
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(
                    "PostgresMemoryBackend: schema_version row missing after "
                    "INSERT ON CONFLICT DO NOTHING — db corruption suspected."
                )
            existing = int(row["value"])

            # C5: If meta claims v2 but display_name column is missing, the DB is
            # actually v1 (tables pre-dated the meta row). Correct existing so the
            # migration ladder runs and updates meta to the true version.
            if existing >= 2:
                # Check whether the v2 column actually exists in memory_notes.
                col_cur = conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'memory_notes' AND column_name = 'display_name'"
                )
                if col_cur.fetchone() is None:
                    # v1 tables, stale meta row — correct the in-memory variable
                    # so the migration ladder runs and updates the meta row.
                    existing = 1
                    conn.execute(
                        "UPDATE memory_meta SET value = %s WHERE key = %s",
                        ("1", "schema_version"),
                    )

            # Migration ladder — run UNDER advisory lock, BEFORE index creation.
            # v1 -> v2: add display_name (the human note name). ADD COLUMN IF NOT
            # EXISTS is idempotent and safe on a fresh-created v2 table (the
            # CREATE TABLE above already declared the column). Existing v1 rows
            # get display_name='' and fall back to the derived filename on read.
            if existing < 2:
                conn.execute(
                    "ALTER TABLE memory_notes "
                    "ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "ALTER TABLE memory_note_versions "
                    "ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "UPDATE memory_meta SET value = %s WHERE key = %s",
                    ("2", "schema_version"),
                )
                existing = 2
            # Create indexes AFTER migrations so index columns exist.
            for stmt in _CREATE_MEMORY_INDEXES:
                conn.execute(stmt)
            # Subclass-tolerant assertion: the DB must be at LEAST this class's
            # schema version. A HIGHER version set by a subclass that extends the
            # schema (e.g. PgvectorMemoryBackend bumps to v3 via its own
            # _ensure_schema after calling super()) is valid — the parent ran its
            # v1->v2 ladder and the subclass migrated forward. Only a too-OLD
            # version (migration failed to advance) is an error. The C5
            # column-correction above already downgrades a stale meta row to its
            # true version before this point, so `<` cannot false-positive on a
            # genuinely-v1 DB whose meta row claimed v2.
            if existing < _SCHEMA_VERSION:
                raise RuntimeError(
                    f"PostgresMemoryBackend schema version mismatch: db has "
                    f"v{existing}, code expects at least v{_SCHEMA_VERSION}. "
                    f"Open an issue at https://github.com/dep0we/atomic-agents-stack/issues"
                )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def _create_staging_tables(self, suffix: str) -> tuple[str, str]:
        """Create uuid-suffixed REGULAR staging tables. Returns (notes_table, versions_table).

        Regular (not Postgres TEMPORARY) tables — see PostgresStagedMemory for
        why the per-thread connection model requires cross-connection visibility.
        """
        notes_table = f"memory_staging_notes_{suffix}"
        versions_table = f"memory_staging_note_versions_{suffix}"

        # S1: validate before any interpolation into SQL
        _validate_staging_table(notes_table)
        _validate_staging_table(versions_table)

        def _do(conn: Any) -> None:
            # Staging notes: same schema as memory_notes but without UNIQUE on name
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {notes_table} ("
                "id BIGSERIAL PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "display_name TEXT NOT NULL DEFAULT '', "
                "type TEXT NOT NULL, "
                "description TEXT NOT NULL DEFAULT '', "
                "captured DATE, last_seen DATE, "
                "pinned BOOLEAN NOT NULL DEFAULT FALSE, "
                "confidence TEXT NOT NULL DEFAULT 'medium', "
                "archived BOOLEAN NOT NULL DEFAULT FALSE, "
                "superseded_by TEXT, body TEXT NOT NULL DEFAULT '', "
                "sources JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "tags JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "supersedes TEXT, merge_into TEXT, expires_at TEXT, "
                "schema_version INTEGER NOT NULL DEFAULT 1, "
                "extra_frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "content_hash TEXT NOT NULL DEFAULT '', "
                "UNIQUE(name)"
                ")"
            )
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {versions_table} ("
                "id BIGSERIAL PRIMARY KEY, "
                "note_name TEXT NOT NULL, "
                "display_name TEXT NOT NULL DEFAULT '', "
                "snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "type TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', "
                "captured DATE, last_seen DATE, "
                "pinned BOOLEAN NOT NULL DEFAULT FALSE, "
                "confidence TEXT NOT NULL DEFAULT 'medium', "
                "archived BOOLEAN NOT NULL DEFAULT FALSE, "
                "superseded_by TEXT, body TEXT NOT NULL DEFAULT '', "
                "sources JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "tags JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "supersedes TEXT, merge_into TEXT, expires_at TEXT, "
                "schema_version INTEGER NOT NULL DEFAULT 1, "
                "extra_frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "content_hash TEXT NOT NULL DEFAULT ''"
                ")"
            )
            conn.commit()

        self._run_with_reconnect(_do)
        return notes_table, versions_table

    # ────────────────────────────────────────────────────────────
    # WritePolicy enforcement

    def _enforce_postgres_write_policy(self, policy: WritePolicy) -> None:
        """Enforce WritePolicy for a Postgres backend (spec/20 MUST 5).

        For a Postgres backend there is no filesystem path; notes are row-
        addressed, so the enforcement uses ``agent_root`` as the authorization
        scope (the Postgres interpretation documented in the spec/20 addendum):

        - read_only_paths FIRST: if agent_root falls under any read_only_paths
          entry, raise WritePathViolation. MUST 5 requires the target NOT be
          under read_only_paths; the WritePolicy contract (backend.py) is
          explicit that read_only_paths must not be dropped by the abstraction
          layer. Checked before write_paths, mirroring
          FilesystemBackend._enforce_write_path().
        - If write_paths is empty: raise WritePathViolation (no authorized scope).
        - If agent_root is not under any write_paths entry: raise WritePathViolation.

        This is the Postgres equivalent of FilesystemBackend._enforce_write_path().
        """
        agent_root = self._agent_root

        # read_only_paths takes precedence — a read-only scope blocks the write
        # even when it also appears in write_paths (parity with filesystem).
        read_only_paths = getattr(policy, "read_only_paths", None)
        if read_only_paths:
            for ro in read_only_paths:
                try:
                    agent_root.relative_to(ro.resolve())
                    raise WritePathViolation(
                        f"PostgresMemoryBackend: write blocked — agent_root "
                        f"{agent_root} is under a tools.md read-only path: {ro}"
                    )
                except ValueError:
                    continue

        if not policy.write_paths:
            raise WritePathViolation(
                "PostgresMemoryBackend: write blocked — write_paths is empty "
                "(no authorized paths in policy)"
            )
        authorized = False
        for wp in policy.write_paths:
            try:
                agent_root.relative_to(wp.resolve())
                authorized = True
                break
            except ValueError:
                continue
        if not authorized:
            raise WritePathViolation(
                f"PostgresMemoryBackend: write blocked — agent_root {agent_root} "
                f"is not under any tools.md write path: {policy.write_paths}"
            )

    # ────────────────────────────────────────────────────────────
    # Internal helpers

    def _row_to_note_ref(self, row: dict[str, Any]) -> NoteRef:
        return NoteRef(
            name=row["name"],
            type=row["type"],
            description=row.get("description", ""),
            captured=_parse_date(row.get("captured")),
            last_seen=_parse_date(row.get("last_seen")),
            pinned=bool(row.get("pinned", False)),
            confidence=row.get("confidence", "medium"),
            archived=bool(row.get("archived", False)),
            superseded_by=row.get("superseded_by"),
        )

    def _row_to_note(self, row: dict[str, Any]) -> Note:
        return Note(
            type=row["type"],
            # display_name is the human note name (capture.name) and round-trips
            # to Note.name for cross-backend parity with FilesystemBackend, which
            # reads name from frontmatter. Fall back to the row's `name`
            # (derived filename) for legacy rows written before the v2 migration.
            name=row.get("display_name") or row["name"],
            description=row.get("description", ""),
            confidence=row.get("confidence", "medium"),
            sources=row.get("sources") or [],
            body=row.get("body", ""),
            supersedes=row.get("supersedes"),
            merge_into=row.get("merge_into"),
            pinned=bool(row.get("pinned", False)),
            expires_at=row.get("expires_at"),
            tags=row.get("tags") or [],
            captured=_parse_date(row.get("captured")),
            last_seen=_parse_date(row.get("last_seen")),
            archived=bool(row.get("archived", False)),
            superseded_by=row.get("superseded_by"),
            schema_version=int(row.get("schema_version", CURRENT_SCHEMA_VERSION)),
            extra_frontmatter=row.get("extra_frontmatter") or {},
        )

    def _version_row_to_note(self, row: dict[str, Any]) -> Note:
        """Convert a memory_note_versions row to a Note."""
        return Note(
            type=row["type"],
            # display_name round-trips the human note name; fall back to
            # note_name (derived filename) for legacy pre-v2 version rows.
            name=row.get("display_name") or row["note_name"],
            description=row.get("description", ""),
            confidence=row.get("confidence", "medium"),
            sources=row.get("sources") or [],
            body=row.get("body", ""),
            supersedes=row.get("supersedes"),
            merge_into=row.get("merge_into"),
            pinned=bool(row.get("pinned", False)),
            expires_at=row.get("expires_at"),
            tags=row.get("tags") or [],
            captured=_parse_date(row.get("captured")),
            last_seen=_parse_date(row.get("last_seen")),
            archived=bool(row.get("archived", False)),
            superseded_by=row.get("superseded_by"),
            schema_version=int(row.get("schema_version", CURRENT_SCHEMA_VERSION)),
            extra_frontmatter=row.get("extra_frontmatter") or {},
        )

    def _list_notes_from_table(
        self,
        table: str,
        include_archived: bool = True,
        include_superseded: bool = True,
    ) -> list[NoteRef]:
        """List NoteRefs from a given table (live or staging)."""
        clauses = []
        params: list[Any] = []
        if not include_archived:
            clauses.append("archived = FALSE")
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        def _do(conn: Any) -> list[NoteRef]:
            cur = conn.execute(
                f"SELECT name, type, description, captured, last_seen, pinned, "
                f"confidence, archived, superseded_by FROM {table} "
                f"{where} ORDER BY name",
                params,
            )
            rows = cur.fetchall()
            conn.commit()  # C1: end the implicit transaction on this read
            return [self._row_to_note_ref(r) for r in rows]

        return self._run_with_reconnect(_do)

    # ────────────────────────────────────────────────────────────
    # Read operations

    def list_notes(
        self,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]:
        try:
            return self._list_notes_from_table(
                "memory_notes",
                include_archived=include_archived,
                include_superseded=include_superseded,
            )
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: list_notes() failed: {exc}"
            ) from exc

    def read_note(self, name: str) -> Note | None:
        def _do(conn: Any) -> Note | None:
            cur = conn.execute("SELECT * FROM memory_notes WHERE name = %s", (name,))
            row = cur.fetchone()
            conn.commit()  # C1: end the implicit transaction on this read
            if row is None:
                return None
            return self._row_to_note(row)

        try:
            return self._run_with_reconnect(_do)
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: read_note({name!r}) failed: {exc}"
            ) from exc

    def list_pinned(self) -> list[NoteRef]:
        def _do(conn: Any) -> list[NoteRef]:
            cur = conn.execute(
                "SELECT name, type, description, captured, last_seen, pinned, "
                "confidence, archived, superseded_by FROM memory_notes "
                "WHERE pinned = TRUE ORDER BY name"
            )
            rows = cur.fetchall()
            conn.commit()  # C1: end the implicit transaction on this read
            return [self._row_to_note_ref(r) for r in rows]

        try:
            return self._run_with_reconnect(_do)
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: list_pinned() failed: {exc}"
            ) from exc

    def list_recent(
        self,
        n: int,
        exclude_pinned: bool = True,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]:
        clauses = ["last_seen IS NOT NULL"]
        params: list[Any] = []
        if exclude_pinned:
            clauses.append("pinned = FALSE")
        if not include_archived:
            clauses.append("archived = FALSE")
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        where = "WHERE " + " AND ".join(clauses)

        def _do(conn: Any) -> list[NoteRef]:
            cur = conn.execute(
                f"SELECT name, type, description, captured, last_seen, pinned, "
                f"confidence, archived, superseded_by FROM memory_notes "
                f"{where} ORDER BY last_seen DESC NULLS LAST LIMIT %s",
                params + [n],
            )
            rows = cur.fetchall()
            conn.commit()  # C1: end the implicit transaction on this read
            return [self._row_to_note_ref(r) for r in rows]

        try:
            return self._run_with_reconnect(_do)
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: list_recent() failed: {exc}"
            ) from exc

    def list_stale(
        self,
        threshold_days: int,
        exclude_pinned: bool = True,
    ) -> list[NoteRef]:
        cutoff = date.today() - timedelta(days=threshold_days)
        clauses = [
            "archived = FALSE",
            "superseded_by IS NULL",
            "last_seen IS NOT NULL",
            "last_seen < %s",
        ]
        params: list[Any] = [cutoff]
        if exclude_pinned:
            clauses.append("pinned = FALSE")
        where = "WHERE " + " AND ".join(clauses)

        def _do(conn: Any) -> list[NoteRef]:
            cur = conn.execute(
                f"SELECT name, type, description, captured, last_seen, pinned, "
                f"confidence, archived, superseded_by FROM memory_notes {where} ORDER BY last_seen",
                params,
            )
            rows = cur.fetchall()
            conn.commit()  # C1: end the implicit transaction on this read
            return [self._row_to_note_ref(r) for r in rows]

        try:
            return self._run_with_reconnect(_do)
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: list_stale() failed: {exc}"
            ) from exc

    def list_orphans(self) -> list[NoteRef]:
        """Postgres backend: always returns [] — no INDEX.md concept.

        All notes are primary-key addressable rows; there is no concept of
        a note existing "on disk" without being in the index. See spec/20
        Postgres addendum for the full explanation.
        """
        return []

    def list_by_type(self, type_name: str) -> list[NoteRef]:
        def _do(conn: Any) -> list[NoteRef]:
            cur = conn.execute(
                "SELECT name, type, description, captured, last_seen, pinned, "
                "confidence, archived, superseded_by FROM memory_notes "
                "WHERE type = %s ORDER BY name",
                (type_name,),
            )
            rows = cur.fetchall()
            conn.commit()  # C1: end the implicit transaction on this read
            return [self._row_to_note_ref(r) for r in rows]

        try:
            return self._run_with_reconnect(_do)
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: list_by_type({type_name!r}) failed: {exc}"
            ) from exc

    def render_index_summary(self) -> str:
        try:
            refs = self.list_notes(include_archived=False, include_superseded=False)
        except Exception:
            return "# Memory Index\n\n(unavailable — Postgres backend error)\n"
        return _generate_index_summary(refs)

    # ────────────────────────────────────────────────────────────
    # Write operations

    def write_note(
        self,
        capture: "Capture",
        policy: WritePolicy,
        expected_content_sha256: str | None = None,
    ) -> NoteRef:
        """Write a capture to memory_notes. Enforces policy. Four merge cases.

        Case 1 (merge_into set): UPDATE last_seen + sources on target.
        Case 2 (fresh write): INSERT new row.
        Case 3 (orphan-recovery): identical content exists; snapshot + repair index.
        Case 4 (collision): different content exists; raise SchemaValidationError.

        All cases use a single Postgres transaction (MUST 6 — atomic writes).
        TOCTOU protection: SELECT ... FOR UPDATE serializes concurrent writers
        for Cases 1/3/4 (the target row already exists, so FOR UPDATE locks it).
        For Case 2 (fresh write) the row does not exist yet, so FOR UPDATE locks
        nothing and two concurrent writers can both observe "absent"; the
        UNIQUE(name) constraint is the backstop — the loser's INSERT hits SQLSTATE
        23505 (unique_violation), which is mapped to the SAME SchemaValidationError
        Case 4 raises (so the concurrent loser and the sequential collision behave
        identically, and no raw psycopg type leaks through the Protocol boundary).

        CAS precondition: expected_content_sha256 is compared against the
        stored content_hash (SHA-256 of deterministic canonical fields).
        This hash is backend-scoped — a hash computed against a FilesystemBackend
        note is NOT portable to this backend. See module docstring.

        WritePolicy: enforced via agent_root scope check (spec/20 MUST 5
        Postgres interpretation). See _enforce_postgres_write_policy().
        """
        self._enforce_postgres_write_policy(policy)
        today = date.today()
        filename = derive_filename(capture.type, capture.name)
        new_content_hash = _compute_content_hash(
            capture.type, capture.name, capture.description, capture.body
        )

        def _do(conn: Any) -> NoteRef:
            if capture.merge_into:
                # Case 1: merge into existing note
                cur = conn.execute(
                    "SELECT * FROM memory_notes WHERE name = %s FOR UPDATE",
                    (capture.merge_into,),
                )
                existing = cur.fetchone()
                if existing is None:
                    conn.rollback()
                    raise SchemaValidationError(
                        f"merge_into target {capture.merge_into!r} doesn't exist"
                    )
                if expected_content_sha256 is not None:
                    if existing["content_hash"] != expected_content_sha256:
                        conn.rollback()
                        raise MemoryPreconditionFailed(
                            f"content of {capture.merge_into!r} has changed "
                            f"(expected {expected_content_sha256[:16]}..., "
                            f"actual {existing['content_hash'][:16]}...); re-read and retry",
                            actual_sha256=existing["content_hash"],
                        )
                # Snapshot pre-state before merge
                _snapshot_conn(conn, capture.merge_into)
                # Merge: update last_seen + sources only; preserve body
                existing_sources = list(existing.get("sources") or [])
                for src in capture.sources:
                    if src not in existing_sources:
                        existing_sources.append(src)
                conn.execute(
                    "UPDATE memory_notes SET last_seen = %s, sources = %s WHERE name = %s",
                    (today, json.dumps(existing_sources), capture.merge_into),
                )
                # Re-fetch AFTER the UPDATE so the returned ref reflects the
                # post-merge last_seen (today) and merged sources, matching
                # FilesystemBackend's post-write re-read (filesystem.py:357).
                # Returning the pre-update `existing` row would carry a stale
                # last_seen — a cross-backend behavioral divergence.
                cur_after = conn.execute(
                    "SELECT name, type, description, captured, last_seen, pinned, "
                    "confidence, archived, superseded_by FROM memory_notes "
                    "WHERE name = %s",
                    (capture.merge_into,),
                )
                merged_row = cur_after.fetchone()
                try:
                    conn.commit()
                except Exception as exc:
                    raise _CommitPhaseError() from exc
                if merged_row is not None:
                    return self._row_to_note_ref(merged_row)
                # Fallback: construct from known post-merge state (last_seen=today,
                # merged sources) rather than the stale pre-update row.
                from dataclasses import replace as _dc_replace

                return _dc_replace(
                    self._row_to_note_ref(dict(existing)), last_seen=today
                )

            # Not a merge_into: derive filename
            # Use SELECT FOR UPDATE to close TOCTOU window on Cases 3/4.
            # SELECT * (not a narrow column list) so the Case-3 return NoteRef
            # carries the row's full metadata (captured/pinned/confidence/
            # archived/superseded_by) via _row_to_note_ref — a narrow SELECT
            # silently defaulted those fields.
            cur = conn.execute(
                "SELECT * FROM memory_notes WHERE name = %s FOR UPDATE",
                (filename,),
            )
            existing_row = cur.fetchone()

            if existing_row is not None:
                # Note exists — determine Case 3 or 4
                stored_type = existing_row["type"]
                stored_name = existing_row["name"]
                stored_display_name = existing_row.get("display_name", "")
                stored_description = existing_row.get("description", "")
                stored_body = existing_row.get("body", "")

                # Cross-backend parity with FilesystemBackend._is_same_capture_content:
                # the human note name is part of the orphan-recovery identity.
                # Two captures with the same body but different human names (e.g.
                # 'My Note' vs 'my-note', which derive_filename collapses to the
                # same row address) are a Case-4 collision, NOT a Case-3 orphan —
                # matching the filesystem path's SchemaValidationError instead of
                # silently succeeding. We compare against the same effective name
                # the backend exposes on read (`display_name or name`), so legacy
                # v1 rows (display_name='') compare on the derived filename, the
                # value _row_to_note_ref surfaces as Note.name for them.
                stored_effective_name = stored_display_name or stored_name
                is_orphan = (
                    stored_type == capture.type
                    and stored_name == filename
                    and stored_effective_name == capture.name
                    and stored_description == capture.description
                    and stored_body.strip() == capture.body.strip()
                )

                if expected_content_sha256 is not None:
                    if existing_row["content_hash"] != expected_content_sha256:
                        conn.rollback()
                        raise MemoryPreconditionFailed(
                            f"content of {filename!r} has changed "
                            f"(expected {expected_content_sha256[:16]}..., "
                            f"actual {existing_row['content_hash'][:16]}...); re-read and retry",
                            actual_sha256=existing_row["content_hash"],
                        )

                if is_orphan:
                    # Case 3: orphan-recovery — snapshot, repair, no body change.
                    # C3: build the NoteRef from already-fetched data BEFORE
                    # committing. Do NOT re-SELECT after commit inside the same
                    # _do closure — a post-commit SELECT runs without a subsequent
                    # commit (idle-in-transaction) and, on reconnect-retry, would
                    # re-run the snapshot+UPDATE path again (duplicate audit row).
                    # The pre-commit row has all fields we need; last_seen=today is
                    # the only change the UPDATE makes, so we apply it locally.
                    _snapshot_conn(conn, filename)
                    conn.execute(
                        "UPDATE memory_notes SET last_seen = %s WHERE name = %s",
                        (today, filename),
                    )
                    # Construct return value from the FULL pre-commit row via
                    # the canonical mapper (parity with the merge_into branch
                    # above), applying last_seen=today — the only field the
                    # UPDATE changes. Building it from the row before commit
                    # keeps every metadata field (captured/pinned/confidence/
                    # archived/superseded_by) instead of defaulting them.
                    from dataclasses import replace as _dc_replace

                    pre_commit_ref = _dc_replace(
                        self._row_to_note_ref(dict(existing_row)), last_seen=today
                    )
                    try:
                        conn.commit()
                    except Exception as exc:
                        raise _CommitPhaseError() from exc
                    return pre_commit_ref
                else:
                    # Case 4: collision — different content
                    conn.rollback()
                    raise SchemaValidationError(
                        f"atomic note {filename!r} already exists; use merge_into to update"
                    )

            # Case 2: fresh write
            if expected_content_sha256 is not None:
                conn.rollback()
                raise MemoryPreconditionFailed(
                    f"expected_content_sha256 was provided but {filename!r} doesn't exist",
                    actual_sha256=None,
                )
            params = _note_insert_params(capture, today, new_content_hash)
            try:
                conn.execute(_NOTES_INSERT_SQL, params)
            except Exception as insert_exc:
                # Concurrent fresh-write race: SELECT ... FOR UPDATE locks nothing
                # when the row is absent, so two writers can both reach Case 2 and
                # both attempt INSERT. The UNIQUE(name) backstop rejects the loser
                # with SQLSTATE 23505 (unique_violation). Map that to the SAME
                # domain exception Case 4 raises for the logically-identical
                # "note already exists" outcome — so the concurrent loser and the
                # sequential collision behave identically, and no raw psycopg
                # driver type leaks through the MemoryBackend Protocol boundary
                # (parity with FilesystemBackend; Principle #2 protocols-not-leaky).
                if getattr(insert_exc, "sqlstate", None) == "23505":
                    conn.rollback()
                    raise SchemaValidationError(
                        f"atomic note {filename!r} already exists; use merge_into to update"
                    ) from insert_exc
                raise
            try:
                conn.commit()
            except Exception as exc:
                raise _CommitPhaseError() from exc
            return NoteRef(
                name=filename,
                type=capture.type,
                description=capture.description,
                captured=today,
                last_seen=today,
                pinned=capture.pinned,
                confidence=capture.confidence,
                archived=False,
                superseded_by=None,
            )

        return self._run_with_reconnect(_do)

    # ────────────────────────────────────────────────────────────
    # Versioning

    def list_versions(self, name: str) -> list[VersionRef]:
        def _do(conn: Any) -> list[VersionRef]:
            cur = conn.execute(
                "SELECT id FROM memory_note_versions WHERE note_name = %s "
                "ORDER BY snapshotted_at DESC, id DESC",
                (name,),
            )
            rows = cur.fetchall()
            conn.commit()  # C1: end the implicit transaction on this read
            return [VersionRef(backend_id=str(row["id"])) for row in rows]

        try:
            return self._run_with_reconnect(_do)
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: list_versions({name!r}) failed: {exc}"
            ) from exc

    def read_version(self, version_ref: VersionRef) -> Note:
        row_id_str = version_ref.backend_id
        try:
            row_id = int(row_id_str)
        except (ValueError, TypeError):
            raise VersionNotFound(
                f"Invalid VersionRef backend_id for Postgres: {row_id_str!r} "
                "(expected integer row id)"
            )

        def _do(conn: Any) -> Note:
            cur = conn.execute(
                "SELECT * FROM memory_note_versions WHERE id = %s", (row_id,)
            )
            row = cur.fetchone()
            conn.commit()  # C1: end the implicit transaction on this read
            if row is None:
                raise VersionNotFound(
                    f"Version row id={row_id} not found in memory_note_versions"
                )
            return self._version_row_to_note(row)

        try:
            return self._run_with_reconnect(_do)
        except VersionNotFound:
            raise
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: read_version({row_id_str!r}) failed: {exc}"
            ) from exc

    def restore_version(
        self,
        name: str,
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> NoteRef:
        self._enforce_postgres_write_policy(policy)
        row_id_str = version_ref.backend_id
        try:
            row_id = int(row_id_str)
        except (ValueError, TypeError):
            raise VersionNotFound(
                f"Invalid VersionRef backend_id for Postgres: {row_id_str!r}"
            )

        def _do(conn: Any) -> NoteRef:
            # Check note exists
            cur = conn.execute(
                "SELECT name FROM memory_notes WHERE name = %s FOR UPDATE", (name,)
            )
            if cur.fetchone() is None:
                conn.rollback()
                raise VersionNotFound(
                    f"Note {name!r} not found — cannot restore version"
                )
            # Get version row
            cur2 = conn.execute(
                "SELECT * FROM memory_note_versions WHERE id = %s", (row_id,)
            )
            vrow = cur2.fetchone()
            if vrow is None:
                conn.rollback()
                raise VersionNotFound(
                    f"Version row id={row_id} not found in memory_note_versions"
                )
            # Cross-note guard (parity with FilesystemBackend, whose version refs
            # are namespaced under <stem>/<file> so a cross-note ref won't
            # resolve). The row-id token here is global, so a programmatic caller
            # passing a VersionRef that belongs to a DIFFERENT note would
            # otherwise restore that note's content onto `name`. Refuse it.
            if vrow["note_name"] != name:
                conn.rollback()
                raise VersionNotFound(
                    f"Version row id={row_id} belongs to note "
                    f"{vrow['note_name']!r}, not {name!r}; cannot restore "
                    f"across notes"
                )
            # Snapshot pre-restore state
            _snapshot_conn(conn, name)
            today = date.today()
            # Restore: update live row from version row
            conn.execute(
                "UPDATE memory_notes SET display_name=%s, type=%s, "
                "description=%s, captured=%s, "
                "last_seen=%s, pinned=%s, confidence=%s, archived=%s, "
                "superseded_by=%s, body=%s, sources=%s, tags=%s, supersedes=%s, "
                "merge_into=%s, expires_at=%s, schema_version=%s, "
                "extra_frontmatter=%s, content_hash=%s WHERE name = %s",
                (
                    # Restore the human note name captured in the snapshot;
                    # the `name` column (row address) is the WHERE key and is
                    # never rewritten. Fall back to '' for legacy version rows.
                    vrow.get("display_name") or "",
                    vrow["type"],
                    vrow["description"],
                    vrow["captured"],
                    today,
                    vrow["pinned"],
                    vrow["confidence"],
                    vrow["archived"],
                    vrow["superseded_by"],
                    vrow["body"],
                    json.dumps(vrow["sources"] or []),
                    json.dumps(vrow["tags"] or []),
                    vrow["supersedes"],
                    vrow["merge_into"],
                    vrow["expires_at"],
                    vrow["schema_version"],
                    json.dumps(vrow["extra_frontmatter"] or {}),
                    vrow["content_hash"],
                    name,
                ),
            )
            # C3: build the NoteRef from the vrow data BEFORE committing.
            # Do NOT re-SELECT after commit inside the same _do closure — a
            # post-commit SELECT runs without a subsequent commit
            # (idle-in-transaction) and, on reconnect-retry, would re-run the
            # snapshot+UPDATE path (duplicate audit snapshot). All needed fields
            # are in vrow + name + today.
            pre_commit_ref = NoteRef(
                name=name,
                type=vrow["type"],
                description=vrow.get("description", ""),
                captured=_parse_date(vrow.get("captured")),
                last_seen=today,
                pinned=bool(vrow.get("pinned", False)),
                confidence=vrow.get("confidence", "medium"),
                archived=bool(vrow.get("archived", False)),
                superseded_by=vrow.get("superseded_by"),
            )
            try:
                conn.commit()
            except Exception as exc:
                raise _CommitPhaseError() from exc
            return pre_commit_ref

        return self._run_with_reconnect(_do)

    def redact_version(
        self,
        version_ref: VersionRef,
        replacement: str = "[REDACTED]",
    ) -> None:
        row_id_str = version_ref.backend_id
        try:
            row_id = int(row_id_str)
        except (ValueError, TypeError):
            raise VersionNotFound(
                f"Invalid VersionRef backend_id for Postgres: {row_id_str!r}"
            )

        def _do(conn: Any) -> None:
            cur = conn.execute(
                "SELECT id FROM memory_note_versions WHERE id = %s", (row_id,)
            )
            if cur.fetchone() is None:
                conn.rollback()
                raise VersionNotFound(f"Version row id={row_id} not found")
            conn.execute(
                "UPDATE memory_note_versions SET body = %s WHERE id = %s",
                (replacement, row_id),
            )
            try:
                conn.commit()
            except Exception as exc:
                raise _CommitPhaseError() from exc

        self._run_with_reconnect(_do)

    def resolve_version_token(self, name: str, token: str) -> VersionRef:
        """Convert a user-typed token (row id string) to an opaque VersionRef.

        Postgres encoding: token is a string representation of the
        memory_note_versions row id (e.g. "42"). VersionRef.backend_id is
        this same string. VersionRef.__str__() returns it verbatim.

        Raises VersionNotFound if the token cannot be resolved to a row
        for the named note.
        """
        try:
            row_id = int(token)
        except (ValueError, TypeError):
            raise VersionNotFound(
                f"Version token {token!r} for note {name!r} is not a valid "
                "Postgres row id (expected integer string)"
            )

        def _do(conn: Any) -> VersionRef:
            cur = conn.execute(
                "SELECT id FROM memory_note_versions WHERE id = %s AND note_name = %s",
                (row_id, name),
            )
            row = cur.fetchone()
            conn.commit()  # C1: end the implicit transaction on this read
            if row is None:
                raise VersionNotFound(
                    f"Version token {token!r} not found for note {name!r} "
                    "in memory_note_versions"
                )
            return VersionRef(backend_id=str(row["id"]))

        return self._run_with_reconnect(_do)

    # ────────────────────────────────────────────────────────────
    # Bulk staging

    def create_staging(self) -> PostgresStagedMemory:
        """Create a staged write area backed by uuid-named REGULAR (not Postgres
        TEMPORARY) Postgres tables.

        Returns a PostgresStagedMemory handle. The caller must eventually call
        apply_staging() (which swaps the staging into live) or discard_staging()
        (which drops the staging tables).

        DreamRunner.apply() raises NotImplementedError for this backend until
        #396 ships. This method and apply_staging() are fully implemented for
        programmatic use.
        """
        suffix = uuid.uuid4().hex[:16]
        notes_table, versions_table = self._create_staging_tables(suffix)
        return PostgresStagedMemory(
            backend_id=f"postgres-staging-{suffix}",
            backend=self,
            staging_notes_table=notes_table,
            staging_versions_table=versions_table,
        )

    def apply_staging(self, staging: StagedMemory, policy: WritePolicy) -> None:
        """Atomically swap live memory with the staged area.

        Per spec/20 MUST 2 and the staged-apply-cross-host ruling:
        1. Acquire self._lock_backend (the same instance threaded into the
           backend at construction) FIRST — serializes against agent.call().
        2. Execute the staging-to-live swap in ONE Postgres transaction.
        3. Commit (releases row locks); the lock is released automatically on
           exit of the ``with self._lock_backend.acquire(...)`` context manager
           (after the commit).

        The lock key is '' (empty string), which is the same key agent.call()
        acquires — ensuring cross-host mutual exclusion even on a multi-replica
        fleet.

        Note: DreamRunner.apply() currently raises NotImplementedError for
        non-FilesystemBackend backends (#396 tracks the fix). This method IS
        implemented for programmatic use.
        """
        if not isinstance(staging, PostgresStagedMemory):
            raise TypeError("apply_staging expects a PostgresStagedMemory instance")
        staging._check_active()
        self._enforce_postgres_write_policy(policy)

        from ..locks import check_lock_lost

        try:
            with self._lock_backend.acquire(
                "", timeout=self._apply_staging_lock_timeout
            ) as handle:
                check_lock_lost(handle)

                def _do(conn: Any) -> None:
                    # S1: validate staging table names at every interpolation site
                    sn = _validate_staging_table(staging._staging_notes_table)
                    sv = _validate_staging_table(staging._staging_versions_table)

                    # C4: guard against re-applying an already-applied staging
                    # area. _applied is set immediately after the swap commit
                    # (outside _do, below), so a closure retry after a commit-
                    # phase connection loss would not re-execute the destructive
                    # DELETE+INSERT. The _CommitPhaseError wrapper below prevents
                    # _run_with_reconnect from retrying once we pass commit().
                    # This explicit guard is the belt AND the suspenders.
                    if staging._applied:
                        return

                    # Full replace: drop all live notes, then re-insert every
                    # staging note below. This is an unconditional DELETE (NOT a
                    # filtered "notes not in staging" merge) — staged-apply is a
                    # wholesale swap, not an upsert.
                    conn.execute("DELETE FROM memory_notes")
                    # Insert all staging notes into live
                    conn.execute(
                        f"INSERT INTO memory_notes ({', '.join(_NOTES_INSERT_COLUMNS)}) "
                        f"SELECT {', '.join(_NOTES_INSERT_COLUMNS)} FROM {sn}"
                    )
                    # Copy staging versions into live versions
                    conn.execute(
                        f"INSERT INTO memory_note_versions ({', '.join(_VERSIONS_INSERT_COLUMNS)}) "
                        f"SELECT {', '.join(_VERSIONS_INSERT_COLUMNS)} FROM {sv}"
                    )
                    try:
                        conn.commit()
                    except Exception as exc:
                        raise _CommitPhaseError() from exc
                    # C4: mark applied IMMEDIATELY after the swap commit so any
                    # retry path (e.g. cleanup failure leading to _do being called
                    # again) sees _applied=True and exits early above.
                    staging._applied = True
                    # The swap is now committed. Dropping the staging tables is a
                    # post-commit cleanup phase — wrap it in _CommitPhaseError so
                    # a connection failure here does NOT trigger a full _do retry
                    # (which would re-run the destructive DELETE+INSERT against an
                    # already-swapped, possibly-half-dropped state → data loss).
                    # DROP ... IF EXISTS is idempotent; a leaked staging table is
                    # a recoverable condition, not corruption.
                    try:
                        conn.execute(f"DROP TABLE IF EXISTS {sn}")
                        conn.execute(f"DROP TABLE IF EXISTS {sv}")
                        conn.commit()
                    except Exception as exc:
                        raise _CommitPhaseError() from exc

                try:
                    self._run_with_reconnect(_do)
                except MemoryBackendError:
                    raise
                except Exception as exc:
                    raise MemoryBackendError(
                        f"PostgresMemoryBackend: apply_staging() failed: {exc}"
                    ) from exc

                # _applied may already be True (set inside _do after the swap
                # commit). Set it unconditionally here as well so callers that
                # reach this point without _do running the swap (e.g. the guard
                # branch above) still transition the state machine correctly.
                staging._applied = True
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError(
                f"PostgresMemoryBackend: apply_staging() lock acquisition failed: {exc}"
            ) from exc

    def discard_staging(self, staging: StagedMemory) -> None:
        """Remove the staging tables without touching live memory.

        Idempotent via DROP TABLE IF EXISTS for crash-safety. The
        StagedMemory state machine (_discarded flag) still raises
        StagingNotApplied on a second call — IF EXISTS is for crash-recovery,
        not double-call tolerance.
        """
        if not isinstance(staging, PostgresStagedMemory):
            raise TypeError("discard_staging expects a PostgresStagedMemory instance")
        staging._check_active()

        def _do(conn: Any) -> None:
            # S1: validate staging table names before interpolation
            sn = _validate_staging_table(staging._staging_notes_table)
            sv = _validate_staging_table(staging._staging_versions_table)
            conn.execute(f"DROP TABLE IF EXISTS {sn}")
            conn.execute(f"DROP TABLE IF EXISTS {sv}")
            conn.commit()

        try:
            self._run_with_reconnect(_do)
        except Exception as exc:
            _logger.warning(
                "PostgresMemoryBackend: discard_staging() failed to drop tables: %s",
                exc,
            )
        staging._discarded = True

    # ────────────────────────────────────────────────────────────
    # Stats

    def stats(self) -> MemoryStats:
        def _do(conn: Any) -> MemoryStats:
            cur = conn.execute(
                "SELECT type, COUNT(*) as cnt FROM memory_notes GROUP BY type"
            )
            by_type: dict[str, int] = {}
            total = 0
            for row in cur.fetchall():
                by_type[row["type"]] = int(row["cnt"])
                total += int(row["cnt"])

            # Most-churned: notes with the most version rows
            cur2 = conn.execute(
                "SELECT note_name, COUNT(*) as cnt FROM memory_note_versions "
                "GROUP BY note_name ORDER BY cnt DESC LIMIT 20"
            )
            most_churned = [(r["note_name"], int(r["cnt"])) for r in cur2.fetchall()]

            # Best-effort byte estimates via pg_total_relation_size()
            live_bytes = -1
            version_history_bytes = -1
            try:
                cur3 = conn.execute(
                    "SELECT pg_total_relation_size('memory_notes') AS s"
                )
                r3 = cur3.fetchone()
                if r3 and r3["s"] is not None:
                    live_bytes = int(r3["s"])
                cur4 = conn.execute(
                    "SELECT pg_total_relation_size('memory_note_versions') AS s"
                )
                r4 = cur4.fetchone()
                if r4 and r4["s"] is not None:
                    version_history_bytes = int(r4["s"])
            except Exception:
                pass  # Managed Postgres may restrict pg_total_relation_size

            conn.commit()  # C1: end the implicit transaction on all the reads above
            return MemoryStats(
                total_notes=total,
                by_type=by_type,
                live_bytes=live_bytes,
                version_history_bytes=version_history_bytes,
                most_churned=most_churned,
            )

        try:
            return self._run_with_reconnect(_do)
        except Exception as exc:
            _logger.warning("PostgresMemoryBackend: stats() failed: %s", exc)
            return MemoryStats(
                total_notes=0,
                by_type={},
                live_bytes=-1,
                version_history_bytes=-1,
                most_churned=[],
            )

    def version_count(self, name: str) -> int:
        def _do(conn: Any) -> int:
            cur = conn.execute(
                "SELECT COUNT(*) as cnt FROM memory_note_versions WHERE note_name = %s",
                (name,),
            )
            row = cur.fetchone()
            conn.commit()  # C1: end the implicit transaction on this read
            return int(row["cnt"]) if row else 0

        try:
            return self._run_with_reconnect(_do)
        except Exception:
            return 0

    def last_mutation_at(self, name: str) -> datetime | None:
        def _do(conn: Any) -> datetime | None:
            cur = conn.execute(
                "SELECT snapshotted_at FROM memory_note_versions "
                "WHERE note_name = %s ORDER BY snapshotted_at DESC LIMIT 1",
                (name,),
            )
            row = cur.fetchone()
            if row and row["snapshotted_at"]:
                ts = row["snapshotted_at"]
                if isinstance(ts, datetime):
                    conn.commit()  # C1: end the implicit transaction on this read
                    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            # Fall back to last_seen date
            cur2 = conn.execute(
                "SELECT last_seen FROM memory_notes WHERE name = %s", (name,)
            )
            row2 = cur2.fetchone()
            conn.commit()  # C1: end the implicit transaction on this read
            if row2 and row2["last_seen"]:
                d = _parse_date(row2["last_seen"])
                if d:
                    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            return None

        try:
            return self._run_with_reconnect(_do)
        except Exception:
            return None

    # ────────────────────────────────────────────────────────────
    # Capability advertisement

    @property
    def supports_semantic_search(self) -> bool:
        """False — FTS/tsvector search is NOT semantic/vector search.

        search() performs full-text search via to_tsvector + websearch_to_tsquery.
        This is a non-semantic mode; supports_semantic_search=False is correct.
        Semantic recall (pgvector) ships in #258 PR2 using the EmbeddingBackend Protocol (now shipped as atomic_agents/embedding/).
        """
        return False

    @property
    def supports_canonical_export(self) -> bool:
        """True — Tier B field-lossless export (spec/40). See module docstring."""
        return True

    def search(self, query: str, limit: int = 10) -> list[NoteRef]:
        """Full-text search via Postgres tsvector (not semantic/embedding-based).

        Uses websearch_to_tsquery(%s) with the user query as a parameterized
        %s argument — never string-interpolated. SQL injection is prevented
        by the parameterized query.

        Search covers display_name, name, description, and body columns via
        on-the-fly to_tsvector('english', ...). No stored generated tsvector
        column in v1 (GIN index optimization tracked as a follow-up issue).
        The query is a parameterized %s argument, so SQL injection is
        impossible; websearch_to_tsquery tolerates arbitrary punctuation as
        ordinary search lexemes. Genuine tsquery PARSE failures are caught and
        return []. CONNECTION-level failures are NOT swallowed here — they
        propagate to _run_with_reconnect for the one-shot reconnect retry (so a
        transient blip is recovered rather than silently degrading recall to
        empty), mirroring read_note/list_notes.

        supports_semantic_search=False: FTS is NOT semantic search. Callers
        that branch on this flag correctly use the FTS path. See MUST 7 in
        spec/20 Postgres addendum.
        """
        if not query or not query.strip():
            return []

        # P1: compute the tsvector expression ONCE via a CTE so it is not
        # evaluated twice (once in WHERE @@ and once in ORDER BY ts_rank_cd).
        # The CTE aliases `tsv` for both filter and ranking, eliminating the
        # double-tsvector evaluation overhead while keeping %s parameterization
        # for the tsquery (no SQL injection path). Do NOT add a stored generated
        # tsvector column — that is deferred to the pgvector GIN-index PR.
        _fts_sql = (
            "WITH ranked AS ("
            "  SELECT name, type, description, captured, last_seen, pinned, "
            "         confidence, archived, superseded_by, "
            "         to_tsvector('english', COALESCE(display_name,'') || ' ' || "
            "           COALESCE(name,'') || ' ' || COALESCE(description,'') || ' ' || "
            "           COALESCE(body,'')) AS tsv, "
            "         websearch_to_tsquery('english', %s) AS tsq "
            "  FROM memory_notes"
            ") "
            "SELECT name, type, description, captured, last_seen, pinned, "
            "       confidence, archived, superseded_by "
            "FROM ranked "
            "WHERE tsv @@ tsq "
            "ORDER BY ts_rank_cd(tsv, tsq) DESC "
            "LIMIT %s"
        )

        def _do(conn: Any) -> list[NoteRef]:
            try:
                cur = conn.execute(_fts_sql, (query, limit))
                rows = cur.fetchall()
                conn.commit()  # C1: end the implicit transaction on this read
                return [self._row_to_note_ref(r) for r in rows]
            except Exception as exc:
                # CONNECTION-level errors propagate so the reconnect layer can
                # retry — a degraded backend must surface, not return []
                # (project lesson: empty result is authoritative; don't collapse
                # a broken backend into "no matches").
                if self._is_connection_error(exc):
                    raise
                # tsquery PARSE / DataError and other non-connection failures:
                # return [] per the documented FTS contract.
                try:
                    conn.rollback()
                except Exception:
                    pass
                _logger.debug(
                    "PostgresMemoryBackend: search() FTS query non-connection "
                    "failure, returning []: %s",
                    exc,
                )
                return []

        try:
            return self._run_with_reconnect(_do)
        except MemoryBackendError:
            raise
        except Exception as exc:
            # Unrecoverable connection-level failure after reconnect retry —
            # surface as MemoryBackendError like the sibling read methods,
            # rather than masquerading as "no results".
            raise MemoryBackendError(
                f"PostgresMemoryBackend: search({query!r}) failed: {exc}"
            ) from exc

    def export(self, query=None) -> Any:
        """Export memory notes as a MemoryExport canonical object (spec/40 Tier B).

        Uses render_note_bytes_from_object(note) for each note (Tier B path —
        field-lossless but NOT byte-exact; see module docstring for caveats).

        Args:
            query: MemoryExportQuery | None. Pass None for unbounded export.

        Returns:
            MemoryExport with (Note, raw_bytes) tuples.
        """
        import warnings

        from ..export.renderer import render_note_bytes_from_object
        from ..export.types import MemoryExport

        if getattr(query, "include_versions", False):
            # Version-history export is deferred to #433. Emit the same warning
            # as FilesystemBackend.export() so callers that enabled this flag
            # for compliance-backup are not silently misled into believing they
            # received full history. The export contains current-state notes only.
            warnings.warn(
                "MemoryExportQuery.include_versions=True is not yet implemented "
                "(deferred to issue #433); the export contains current-state notes "
                "only, NOT version history.",
                stacklevel=2,
            )

        if query is not None:
            # Apply filters from MemoryExportQuery if present
            include_archived = getattr(query, "include_archived", False)
            include_superseded = getattr(query, "include_superseded", False)
        else:
            include_archived = False
            include_superseded = False

        refs = self.list_notes(
            include_archived=include_archived,
            include_superseded=include_superseded,
        )
        notes_with_bytes = []
        for ref in refs:
            note = self.read_note(ref.name)
            if note is None:
                continue
            raw_bytes = render_note_bytes_from_object(note)
            notes_with_bytes.append((note, raw_bytes))

        return MemoryExport(
            notes_with_bytes=notes_with_bytes,
            # MemoryExport.backend_id is the export envelope's field name; we
            # source it from this backend's implementation_id (spec/20 MUST 3).
            backend_id=self.implementation_id,
            scope=str(self._agent_root),
        )

    def export_all(self) -> Any:
        """Convenience wrapper — unbounded export. Equivalent to export(None).

        WARNING: Materializes ALL notes in memory. For large deployments use
        export(MemoryExportQuery(...)) with bounded filters instead.
        """
        return self.export(None)

    # ────────────────────────────────────────────────────────────
    # Lifecycle

    def close(self) -> None:
        """Release ALL per-thread connections known to this instance.

        Closes connections opened by worker threads (e.g., helper_call_parallel)
        that exit without calling close() themselves. Thread-safe: iterates the
        _all_conns list under a lock.

        This diverges from PostgresLogBackend.close() (which only closes the
        calling thread's connection) because the memory backend's capture path
        is hit from helper worker threads. See module docstring for full rationale.
        """
        with self._conn_list_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        # Also clear calling thread's reference (may not be in _all_conns if
        # it was removed due to a prior error)
        self._tls.conn = None

    def __del__(self) -> None:
        """Best-effort close on GC — not a substitute for explicit close()."""
        try:
            self.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────
# Internal helpers


def _compute_content_hash(type_: str, name: str, description: str, body: str) -> str:
    """Compute a deterministic SHA-256 hash of a note's canonical mutable fields.

    Used as the CAS precondition check value (stored in content_hash column).
    This hash is backend-scoped: a hash computed against a FilesystemBackend
    note is NOT portable to this backend (filesystem hashes the full rendered
    markdown bytes; this hashes the four canonical fields).

    The four fields match FilesystemBackend's _is_same_capture_content() logic
    (type, name, description, body.strip()) so orphan-recovery detection is
    consistent with the filesystem reference impl.
    """
    canonical = json.dumps(
        {
            "type": type_,
            "name": name,
            "description": description,
            "body": body.strip(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _note_insert_params(capture: "Capture", today: date, content_hash: str) -> tuple:
    """Return the parameter tuple for _NOTES_INSERT_SQL."""
    filename = derive_filename(capture.type, capture.name)
    return (
        filename,  # name (derived filename — the row address)
        capture.name,  # display_name (the human note name — round-trips to Note.name)
        capture.type,
        capture.description,
        today,  # captured
        today,  # last_seen
        capture.pinned,
        capture.confidence,
        False,  # archived
        None,  # superseded_by
        capture.body,
        json.dumps(capture.sources),
        json.dumps(capture.tags or []),
        capture.supersedes,
        capture.merge_into,
        capture.expires_at,
        CURRENT_SCHEMA_VERSION,
        json.dumps({}),  # extra_frontmatter
        content_hash,
    )


def _snapshot_conn(conn: Any, name: str) -> None:
    """Insert a version snapshot within an existing transaction (conn NOT committed here).

    Reads the live ``memory_notes`` row and maps it into the version table's
    column shape. The version table's ``note_name`` column is sourced from the
    note's ``name`` column; every other column (including ``display_name``)
    maps 1:1. A live notes row has no ``note_name`` key, so a positional
    column-name copy would KeyError — this explicit mapping is required.
    """
    cur = conn.execute("SELECT * FROM memory_notes WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        return
    conn.execute(_VERSIONS_INSERT_SQL, _versions_params_from_note_row(row))


def _versions_params_from_note_row(row: dict[str, Any]) -> tuple:
    """Build the _VERSIONS_INSERT_SQL param tuple from a live memory_notes row.

    Maps note_name <- row['name']; all other version columns read by their own
    name (they are identical between the notes and versions tables).
    """

    def _col(c: str) -> Any:
        if c == "note_name":
            return row["name"]
        if c in _VERSIONS_JSONB_COLUMNS:
            # JSONB columns read back from `SELECT *` as Python objects
            # (dict/list); psycopg cannot adapt a raw dict to a `%s` placeholder,
            # so re-serialize to a JSON TEXT param (implicitly cast to JSONB on
            # INSERT) — the same json.dumps pattern the other version-insert
            # sites use. A NULL column defaults to its empty shape.
            v = row[c]
            if v is None:
                v = {} if c == "extra_frontmatter" else []
            return json.dumps(v)
        return row[c]

    return tuple(_col(c) for c in _VERSIONS_INSERT_COLUMNS)


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val)
    try:
        if "T" in s:
            return datetime.fromisoformat(s).date()
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


_TYPE_TO_SECTION = {
    "user": "User Profile",
    "feedback": "Critical Feedback",
    "project": "Active Projects",
    "decision": "Locked Decisions",
    "reference": "Reference",
}


# PA1: canonical type order matching FilesystemBackend._generate_index_from_dir
# (filesystem.py line 997: "user", "feedback", "project", "decision", "reference").
# Unknown types fall through to an "Other" section appended after the known ones.
_CANONICAL_TYPE_ORDER = ("user", "feedback", "project", "decision", "reference")


def _generate_index_summary(refs: list[NoteRef]) -> str:
    """Generate an INDEX.md-equivalent summary from a list of NoteRefs.

    PA1: emits sections in the same fixed semantic order as FilesystemBackend's
    _generate_index_from_dir (user → feedback → project → decision → reference),
    with unknown types appended alphabetically after the canonical set.
    This ensures render_index_summary() produces consistent output across
    backends for the same underlying notes.
    """
    if not refs:
        return "# Memory Index\n\n(no notes)\n"
    by_type: dict[str, list[NoteRef]] = {}
    for r in refs:
        by_type.setdefault(r.type, []).append(r)

    lines = ["# Memory Index", ""]
    # Emit canonical types first, in the fixed filesystem order.
    seen: set[str] = set()
    for type_name in _CANONICAL_TYPE_ORDER:
        if type_name in by_type:
            section = _TYPE_TO_SECTION.get(type_name, "Reference")
            lines.append(f"## {section}")
            for note in sorted(by_type[type_name], key=lambda n: n.name):
                lines.append(f"- [{note.name}]({note.name}) — {note.description}")
            lines.append("")
            seen.add(type_name)
    # Append unknown types alphabetically (not in the canonical set).
    for type_name in sorted(by_type):
        if type_name in seen:
            continue
        section = _TYPE_TO_SECTION.get(type_name, "Reference")
        lines.append(f"## {section}")
        for note in sorted(by_type[type_name], key=lambda n: n.name):
            lines.append(f"- [{note.name}]({note.name}) — {note.description}")
        lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# Factory function


def make_postgres_memory_backend_from_url(
    url: str,
    agent_root: Path | None = None,
    lock_backend=None,
) -> "PostgresMemoryBackend":
    """Build a PostgresMemoryBackend from an operator-supplied URL.

    Called by get_default_memory_backend() when ATOMIC_AGENTS_MEMORY_BACKEND=postgres.
    The agent_root and lock_backend are passed through from the factory.

    Args:
        url: postgresql://user:password@host:port/dbname connection URL.
        agent_root: Path for the agent's root directory. Defaults to Path.cwd()
            when None (programmatic callers that don't have an agent_root).
        lock_backend: Optional LockBackend for apply_staging() serialization.

    Returns:
        Constructed PostgresMemoryBackend (schema initialized on first connection).

    Raises:
        ImportError: psycopg not installed (postgres extra missing).
        ValueError: URL is invalid or malformed.
    """
    try:
        import psycopg  # noqa: PLC0415 — verify extra is present before constructing

        _ = psycopg  # suppress unused-import warning
    except ImportError as exc:
        raise ImportError(
            "PostgresMemoryBackend requires the 'postgres' extra. "
            "Install via: pip install 'atomic-agents-stack[postgres]'"
        ) from exc

    if agent_root is None:
        agent_root = Path.cwd()

    return PostgresMemoryBackend(agent_root, lock_backend=lock_backend, url=url)

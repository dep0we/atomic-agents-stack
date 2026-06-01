"""SQLiteCorpusBackend -- stdlib sqlite3 reference impl with FTS5 (spec/34 PR 2).

Storage shape (hybrid: metadata in SQL + FTS5 indexed + bodies on disk):

* One SQLite file at ``db_path``. URL form:
  ``ATOMIC_AGENTS_CORPUS_BACKEND_URL=sqlite:///path/to/corpus.db?agent_scope=<name>``.
* ``pages`` table holds **metadata**: ``agent_scope``, ``corpus``, ``name``,
  ``title``, ``body_path``, ``byte_size``, ``last_modified``, typed date
  columns (``captured``, ``last_seen``, ``expires_at``, ``ingested_at``),
  ``pinned``, and ``frontmatter_json`` (full YAML frontmatter dict serialized
  as JSON, used for FTS5 indexing).
  Composite PRIMARY KEY ``(agent_scope, corpus, name)`` isolates scopes.
* Page **bodies** live on disk at
  ``<content_root>/<agent_scope>/<corpus>/<name>.md``.
  Hybrid shape keeps the SQLite file small and avoids BLOB overhead for
  large page bodies. Same shape as ``SQLiteToolRegistryBackend`` handler
  body pattern.
* ``pages_fts`` is an FTS5 external-content virtual table backed by
  ``pages`` using ``tokenize='unicode61'``. FTS index is maintained
  manually inside ``write_page`` (after the disk write succeeds) so the
  body text is searchable, not just frontmatter. The external-content
  triggers (``pages_ai`` / ``pages_ad`` / ``pages_au``) are installed
  for structural completeness but FTS rows are explicitly upserted in
  ``write_page`` to include the on-disk body text.
* ``meta`` table holds ``schema_version`` (with ``INSERT OR IGNORE``
  cold-start race mitigation per the established sibling pattern).
* WAL journal mode + ``synchronous=NORMAL`` for concurrent reader/writer
  interleaving on local filesystems.

Cross-corpus isolation: every SELECT / UPDATE / DELETE filters
``WHERE agent_scope = ? AND corpus = ?``. The scope is hardcoded from
the constructor; ``write_page`` never accepts a scope parameter.
Per-scope per-corpus body subdirectory (``<content_root>/<scope>/<corpus>/``)
is defense-in-depth at the filesystem layer.

Thread-safety: ``threading.local`` connection pool gives each thread its
own ``sqlite3.Connection`` for file-backed deployments. sqlite3 connections
are not shared across threads by default. WAL mode + per-thread connections
is the standard pattern matching ``SQLiteLogBackend`` / ``SQLiteAgentProfileBackend``
/ ``SQLiteToolRegistryBackend``.

The ``:memory:`` mode is **single-threaded test-only**: one shared
connection with the default ``check_same_thread=True`` so cross-thread
access raises ``ProgrammingError`` immediately at the misuse site rather
than producing silent corruption. Operators needing multi-threaded SQLite
must use file-backed mode.

Concurrent multi-process: WAL mode supports it on **local filesystems**.
Multiple ``SQLiteCorpusBackend`` instances against the same db from
different processes on the SAME host see consistent reads + serialized
writes. **Network-mounted filesystems (NFS, SMB) are NOT supported** --
SQLite WAL on NFS is documented-broken upstream.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlparse

import frontmatter as _fm

from .._io import atomic_write, safe_resolve_under
from ..exceptions import (
    CorpusCorrupted,
    CorpusInvalidName,
    CorpusPageExists,
    CorpusPageNotFound,
    CorpusPreconditionFailed,
    CorpusVersionNotFound,
    PathTraversalError,
    WritePathViolation,
)
from ..memory.backend import VersionRef, WritePolicy
from .filesystem import (
    _NAME_PATTERN,
    _build_page_content,
    _enforce_corpus_write_policy,
    _extract_title_from_content,
    _page_to_frontmatter_dict,
    _sha256_hex,
    _validate_corpus_name,
    _validate_corpus_type,
    _version_filename,
)
from .types import CorpusCapabilities, CorpusPage, CorpusRef, CorpusStats

_logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Module-level constant: frontmatter fields with dedicated typed SQL columns
# Used in _row_to_corpus_page and read_version to separate typed fields from
# extra_frontmatter. Defined once to avoid repeated frozenset allocation.

_KNOWN_FRONTMATTER_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "title",
        "description",
        "type",
        "captured",
        "last_seen",
        "sources",
        "provenance",
        "confidence",
        "pinned",
        "related",
        "tags",
        "schema_version",
        "expires_at",
        "supersedes",
        "superseded_by",
        "source_url",
        "mime_type",
        "ingested_at",
    }
)


# ─────────────────────────────────────────────────────────────────
# Schema version

_SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────
# WAL retry error codes (Python 3.11+ attribute; graceful fallback)

_RECOVERABLE = (
    getattr(sqlite3, "SQLITE_BUSY", 5),
    getattr(sqlite3, "SQLITE_LOCKED", 6),
)


# ─────────────────────────────────────────────────────────────────
# DDL strings

_CREATE_PAGES = """
CREATE TABLE IF NOT EXISTS pages (
    agent_scope   TEXT NOT NULL,
    corpus        TEXT NOT NULL CHECK (corpus IN ('wiki', 'raw')),
    name          TEXT NOT NULL,
    title         TEXT,
    body_path     TEXT NOT NULL,
    byte_size     INTEGER,
    last_modified REAL,
    captured      TEXT,
    last_seen     TEXT,
    expires_at    TEXT,
    ingested_at   TEXT,
    pinned        INTEGER DEFAULT 0,
    frontmatter_json TEXT,
    PRIMARY KEY (agent_scope, corpus, name)
)
"""

_CREATE_PAGES_SCOPE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_pages_scope_corpus
ON pages(agent_scope, corpus)
"""

_CREATE_PAGES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    name,
    body,
    frontmatter_json,
    content=pages,
    tokenize='unicode61'
)
"""

# External-content triggers keep the FTS index structurally correct for
# metadata-only inserts (e.g., when body_text is '' via the trigger).
# write_page explicitly upserts FTS with the real body text after disk write.
# The triggers ensure structural integrity; the explicit upsert ensures
# body searchability.
_CREATE_TRIGGER_AI = """
CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, name, body, frontmatter_json)
    VALUES (new.rowid, new.name, '', new.frontmatter_json);
END
"""

_CREATE_TRIGGER_AD = """
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, name, body, frontmatter_json)
    VALUES ('delete', old.rowid, old.name, '', old.frontmatter_json);
END
"""

_CREATE_TRIGGER_AU = """
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, name, body, frontmatter_json)
    VALUES ('delete', old.rowid, old.name, '', old.frontmatter_json);
    INSERT INTO pages_fts(rowid, name, body, frontmatter_json)
    VALUES (new.rowid, new.name, '', new.frontmatter_json);
END
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)
"""


# ─────────────────────────────────────────────────────────────────
# URL redaction helper
# File-local copy per the established sibling pattern
# (registry/sqlite.py:80-113 + corpus/filesystem.py:168-191).
# Never import from siblings; each backend owns its redaction.


def _redact_url(url: str) -> str:
    """Redact credentials in a URL for safe error-message echo.

    Strips credentials from netloc + truncates path/query/fragment to
    keep error messages diagnostic but not credential-bearing. Mirrors
    ``registry/sqlite.py:80-113`` verbatim (file-local copy per the
    established precedent pattern -- one copy per backend module so
    backends stay independently deployable without cross-module imports).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "<unparseable url>"
    if parsed.password or parsed.username:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        try:
            sanitized = parsed._replace(netloc=host).geturl()
            return sanitized
        except Exception:
            return f"{parsed.scheme}://..." if parsed.scheme else "<redacted>"
    return url if len(url) <= 256 else url[:256] + "..."


# ─────────────────────────────────────────────────────────────────
# FTS5 query escaping


def _escape_fts5_query(text: str) -> str:
    """Escape user-supplied text for safe use in an FTS5 MATCH expression.

    1. Strip leading/trailing whitespace. Return empty string for blank input.
    2. Double any internal double-quote characters (FTS5 phrase-search escaping).
    3. Wrap the whole thing in double quotes (phrase-search mode).

    Caller short-circuits to ``return []`` when this returns empty string.

    Handles: ``O'Brien``, ``cat"dog``, ``"unterminated``, unicode combining
    characters, whitespace-only input.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    # Double any internal double-quotes (FTS5 phrase-search escaping rule)
    escaped = stripped.replace('"', '""')
    return f'"{escaped}"'


# ─────────────────────────────────────────────────────────────────
# CorpusPage reconstruction helpers


def _row_to_corpus_page(
    row: sqlite3.Row,
    body_text: str,
    name: str,
    corpus: Literal["wiki", "raw"],
) -> CorpusPage:
    """Reconstruct a ``CorpusPage`` from a SQL row + on-disk body text.

    Typed date/datetime columns are re-parsed from ISO strings. Never
    reconstructs from ``frontmatter_json`` (that column is for FTS5 only).
    Extra frontmatter fields not in the typed columns land in
    ``extra_frontmatter`` via ``json.loads(frontmatter_json)``.
    """
    # Typed date fields
    captured: date | None = None
    if row["captured"]:
        try:
            captured = date.fromisoformat(row["captured"])
        except (ValueError, TypeError):
            pass

    last_seen: date | None = None
    if row["last_seen"]:
        try:
            last_seen = date.fromisoformat(row["last_seen"])
        except (ValueError, TypeError):
            pass

    expires_at: date | None = None
    if row["expires_at"]:
        try:
            expires_at = date.fromisoformat(row["expires_at"])
        except (ValueError, TypeError):
            pass

    ingested_at: datetime | None = None
    if row["ingested_at"]:
        try:
            ingested_at = datetime.fromisoformat(row["ingested_at"])
        except (ValueError, TypeError):
            pass

    # last_modified stored as REAL (unix timestamp)
    last_modified: datetime
    if row["last_modified"]:
        last_modified = datetime.fromtimestamp(
            float(row["last_modified"]), tz=timezone.utc
        )
    else:
        last_modified = datetime.now(tz=timezone.utc)

    byte_size = row["byte_size"] or 0

    title = row["title"] or name

    ref = CorpusRef(
        name=name,
        corpus=corpus,
        title=title,
        last_modified=last_modified,
        byte_size=byte_size,
    )

    # Decode frontmatter_json once; derive both fm_dict and extra_frontmatter
    # from the single parse result. _KNOWN_FRONTMATTER_FIELDS is module-level.
    fm_dict: dict = {}
    if row["frontmatter_json"]:
        try:
            fm_dict = json.loads(row["frontmatter_json"])
        except (json.JSONDecodeError, TypeError):
            fm_dict = {}

    extra: dict = {
        k: v for k, v in fm_dict.items() if k not in _KNOWN_FRONTMATTER_FIELDS
    }

    return CorpusPage(
        ref=ref,
        body=body_text,
        name=fm_dict.get("name"),
        description=fm_dict.get("description"),
        type=fm_dict.get("type"),
        captured=captured,
        last_seen=last_seen,
        sources=fm_dict.get("sources"),
        provenance=fm_dict.get("provenance"),
        confidence=fm_dict.get("confidence"),
        pinned=bool(row["pinned"]),
        related=fm_dict.get("related"),
        tags=fm_dict.get("tags"),
        schema_version=fm_dict.get("schema_version"),
        expires_at=expires_at,
        supersedes=fm_dict.get("supersedes"),
        superseded_by=fm_dict.get("superseded_by"),
        source_url=fm_dict.get("source_url"),
        mime_type=fm_dict.get("mime_type"),
        ingested_at=ingested_at,
        extra_frontmatter=extra,
    )


def _build_frontmatter_json(frontmatter: dict | None) -> str:
    """Serialize the frontmatter dict to JSON for storage.

    date/datetime fields are converted to ISO strings so JSON can carry them.
    Returns ``"{}"`` when frontmatter is None or empty.
    """
    if not frontmatter:
        return "{}"

    def _default(obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    try:
        return json.dumps(frontmatter, default=_default)
    except (TypeError, ValueError):
        return "{}"


# ─────────────────────────────────────────────────────────────────
# Main backend class


class SQLiteCorpusBackend:
    """SQLite-backed CorpusBackend with FTS5 full-text search.

    Conforms to the ``CorpusBackend`` Protocol. Constructed once per process
    (per agent_scope) with a db file path + scope.

    Hybrid storage shape:
    - SQL metadata + FTS5 index in ``db_path``.
    - Page bodies on disk at ``<content_root>/<agent_scope>/<corpus>/<name>.md``.
    - Version snapshots on disk at
      ``<content_root>/<agent_scope>/<corpus>/.versions/<stem>/<filename>.md``.

    Args:
        db_path: filesystem path to the SQLite db file, OR the literal
            string ``":memory:"`` for an in-memory database (test-only).
        agent_scope: opaque per-scope identifier. All queries filter on
            this value. Different scopes against the same db are isolated.
            Refuses path-traversal tokens at the constructor.
        content_root: directory under which page body files live (one
            subdir per agent_scope, one per corpus). Defaults to
            ``<db_path>.parent / "corpus_content"`` for file-backed
            deployments. For ``:memory:`` the default is a per-instance
            tempdir (mirrors ``SQLiteToolRegistryBackend`` behavior).
    """

    backend_id: str = "sqlite"

    def __init__(
        self,
        db_path: str | Path,
        agent_scope: str,
        *,
        content_root: Path | None = None,
    ) -> None:
        # agent_scope validation (4-part check from registry/sqlite.py:221-240)
        if not agent_scope or not isinstance(agent_scope, str):
            raise ValueError(
                "SQLiteCorpusBackend agent_scope must be a non-empty string"
            )
        if "/" in agent_scope or "\\" in agent_scope:
            raise ValueError(
                f"agent_scope {agent_scope!r} contains a path separator -- "
                f"refused to prevent content_root escape"
            )
        if agent_scope.startswith(".") or ".." in agent_scope:
            raise ValueError(
                f"agent_scope {agent_scope!r} starts with '.' or contains "
                f"'..' -- refused to prevent traversal"
            )
        for ch in agent_scope:
            if ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ValueError(
                    f"agent_scope {agent_scope!r} contains a control "
                    f"character (0x{ord(ch):02x}) -- refused"
                )
        self._agent_scope = agent_scope

        # Detect :memory: sentinel
        if db_path == ":memory:":
            self._in_memory = True
            self._db_path_str = ":memory:"
            # Single shared connection for :memory: (test-only, single-threaded).
            # PRAGMA journal_mode is a no-op on :memory: (SQLite forces 'memory'
            # journal regardless) -- WAL pragma intentionally skipped.
            self._shared_conn: sqlite3.Connection | None = sqlite3.connect(
                self._db_path_str
            )
            self._shared_conn.row_factory = sqlite3.Row
            self._ensure_schema(self._shared_conn)
            self._content_root = (
                Path(content_root)
                if content_root is not None
                else Path(tempfile.mkdtemp(prefix="atomic_agents_memory_corpus_"))
            )
            warnings.warn(
                "SQLiteCorpusBackend(':memory:') is non-persistent -- "
                "all corpus metadata is lost on process exit. Use "
                "sqlite:///absolute/path/to/corpus.db for durable "
                "deployments.",
                RuntimeWarning,
                stacklevel=2,
            )
        else:
            self._in_memory = False
            self._db_path = Path(db_path)
            self._db_path_str = str(self._db_path)
            self._shared_conn = None
            self._content_root = (
                Path(content_root)
                if content_root is not None
                else self._db_path.parent / "corpus_content"
            )

        # Per-thread connection pool (file-backed only)
        self._tls = threading.local()
        # Track all connections for close() (TLS is not enumerable)
        self._all_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()
        self._closed = False

    @property
    def agent_scope(self) -> str:
        """The scope this backend is bound to. Read-only after construction."""
        return self._agent_scope

    @property
    def db_path(self) -> str:
        """The SQLite db path string (or ``":memory:"``). Read-only."""
        return self._db_path_str

    @property
    def content_root(self) -> Path:
        """Filesystem directory under which page bodies live."""
        return self._content_root

    # ── Connection management ──────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """Return the calling thread's connection, creating on first use.

        Pragma ordering follows ``registry/sqlite.py:343-349`` and
        ``logs/sqlite.py:249-253`` as precedents:
        1. ``busy_timeout`` BEFORE WAL pragma (prevents OperationalError
           on cold-start WAL transition races).
        2. WAL transition inside a 7-attempt retry loop with exponential
           backoff (matches the #208 fix in logs/sqlite.py).
        3. ``synchronous=NORMAL`` after WAL succeeds.
        """
        if self._in_memory:
            return self._shared_conn  # type: ignore[return-value]
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path_str)
            conn.row_factory = sqlite3.Row
            # busy_timeout BEFORE WAL pragma (prevents the WAL-transition
            # OperationalError when N processes race the first open on a fresh
            # db file). Reference: registry/sqlite.py:343-349.
            conn.execute("PRAGMA busy_timeout = 5000")
            # WAL retry loop (7-attempt exponential backoff matching the #208 fix
            # in logs/sqlite.py). Matches registry/sqlite.py:366-377 exactly.
            # Match on sqlite_errorcode (Python 3.11+) not message text so a
            # future SQLite wording change cannot silently re-raise corruption.
            for attempt in range(7):
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if (
                        getattr(exc, "sqlite_errorcode", None) not in _RECOVERABLE
                        or attempt == 6
                    ):
                        raise
                    time.sleep(0.05 * (2**attempt))
            conn.execute("PRAGMA synchronous = NORMAL")
            self._ensure_schema(conn)
            self._tls.conn = conn
            # Track for close()
            with self._all_conns_lock:
                self._all_conns.append(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables + FTS virtual table + triggers + meta row. Idempotent.

        Uses ``CREATE TABLE IF NOT EXISTS`` (always idempotent) and
        ``INSERT OR IGNORE`` for the schema_version row -- the latter is
        the multi-process cold-start race mitigation per the established
        sibling pattern (#61 PR 3 + #63 PR 3 + #64 PR 3).

        FTS5 note: ``pages_fts`` uses ``content=pages`` (external-content)
        with ``tokenize='unicode61'``. FTS rows are maintained explicitly in
        ``write_page`` so the on-disk body text is searchable (FTS5 external-
        content triggers insert '' for body because triggers cannot read
        external filesystem files). The three triggers (pages_ai / pages_ad /
        pages_au) maintain structural FTS correctness for metadata-only paths;
        ``write_page`` then overwrites the FTS body with the real text via a
        direct INSERT after the disk write succeeds.
        """
        with conn:
            conn.execute(_CREATE_PAGES)
            conn.execute(_CREATE_PAGES_SCOPE_INDEX)
            conn.execute(_CREATE_PAGES_FTS)
            conn.execute(_CREATE_TRIGGER_AI)
            conn.execute(_CREATE_TRIGGER_AD)
            conn.execute(_CREATE_TRIGGER_AU)
            conn.execute(_CREATE_META)
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
        # Defensive schema version check
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or int(row["value"]) != _SCHEMA_VERSION:
            raise CorpusCorrupted(
                f"SQLiteCorpusBackend schema version mismatch at "
                f"{self._db_path_str}: expected {_SCHEMA_VERSION}, "
                f"found {row['value'] if row else 'no row'}. "
                f"Migration required."
            )

    # ── Capability advertisement ───────────────────────────────────────────

    @property
    def capabilities(self) -> CorpusCapabilities:
        """Return the SQLiteCorpusBackend capability snapshot.

        ``supports_full_text_search=True`` because FTS5 is active via the
        ``pages_fts`` virtual table. ``supports_semantic_search=False`` in
        PR 2 (pgvector / embedding provider deferred to the coordinated
        #258 Postgres-adapter family release).
        """
        return CorpusCapabilities(
            supports_semantic_search=False,
            supports_full_text_search=True,
            supports_versioning=True,
            supports_streaming_iteration=False,
            embedding_provider=None,
        )

    # ── Body path helpers ──────────────────────────────────────────────────

    def _body_path(self, corpus: str, name: str) -> Path:
        """Return the expected body file path for a page (no I/O)."""
        return self._content_root / self._agent_scope / corpus / f"{name}.md"

    def _versions_dir(self, corpus: str, name: str) -> Path:
        """Return the versions directory path for a page (no I/O)."""
        return self._content_root / self._agent_scope / corpus / ".versions" / name

    # ── Read operations ────────────────────────────────────────────────────

    def list_pages(
        self,
        corpus: Literal["wiki", "raw"],
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CorpusRef]:
        """SELECT pages WHERE agent_scope=? AND corpus=? ORDER BY last_modified DESC.

        Double discriminator on every query (SEV-1). LIMIT/OFFSET applied
        at the SQL layer for efficiency.

        Returns ``[]`` when the corpus is empty.
        """
        # SEV-8: path-traversal guard before any SQL
        _validate_corpus_type(corpus)

        conn = self._get_conn()

        if limit is not None:
            sql = """
                SELECT name, title, byte_size, last_modified
                FROM pages
                WHERE agent_scope = ? AND corpus = ?
                ORDER BY last_modified DESC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(
                sql, (self._agent_scope, corpus, limit, offset)
            ).fetchall()
        else:
            sql = """
                SELECT name, title, byte_size, last_modified
                FROM pages
                WHERE agent_scope = ? AND corpus = ?
                ORDER BY last_modified DESC
                LIMIT -1 OFFSET ?
            """
            rows = conn.execute(sql, (self._agent_scope, corpus, offset)).fetchall()

        refs: list[CorpusRef] = []
        for row in rows:
            if row["last_modified"]:
                last_modified = datetime.fromtimestamp(
                    float(row["last_modified"]), tz=timezone.utc
                )
            else:
                last_modified = datetime.now(tz=timezone.utc)
            refs.append(
                CorpusRef(
                    name=row["name"],
                    corpus=corpus,
                    title=row["title"] or row["name"],
                    last_modified=last_modified,
                    byte_size=row["byte_size"] or 0,
                )
            )
        return refs

    def read_page(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> CorpusPage | None:
        """SELECT row + read body file; return None if either is missing.

        Double discriminator on every query (SEV-1). Returns None when
        the SQL row is absent OR when the on-disk body file is missing
        (per spec/34 D12: read_page returns None for missing pages,
        distinct from read_version which raises CorpusVersionNotFound).
        Reconstructs CorpusPage from typed SQL columns, NOT from
        json.loads(frontmatter_json) (SEV-4 date round-trip rule).
        """
        # SEV-8: path-traversal guards before any SQL
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT name, title, body_path, byte_size, last_modified,
                   captured, last_seen, expires_at, ingested_at,
                   pinned, frontmatter_json
            FROM pages
            WHERE agent_scope = ? AND corpus = ? AND name = ?
            """,
            (self._agent_scope, corpus, name),
        ).fetchone()

        if row is None:
            return None

        body_path = Path(row["body_path"])

        # Guard against path traversal in stored body_path
        try:
            safe_resolve_under(body_path, self._content_root)
        except (PathTraversalError, OSError):
            return None

        if not body_path.is_file():
            # Body file missing -- return None per D12 (not raise)
            return None

        try:
            body_text = body_path.read_text(encoding="utf-8")
        except OSError:
            return None

        return _row_to_corpus_page(row, body_text, name, corpus)

    def render_index_summary(
        self,
        corpus: Literal["wiki", "raw"],
    ) -> str:
        """Synthesize an INDEX-equivalent string from page metadata.

        For ``corpus="wiki"``: SELECT the top-10 most-recently-modified
        pages and render a markdown list of title + description.
        For ``corpus="raw"``: returns ``""`` (raw corpora have no INDEX
        equivalent per spec/34 §"render_index_summary").

        Returns ``""`` when the corpus is empty.
        """
        _validate_corpus_type(corpus)

        if corpus == "raw":
            return ""

        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT name, title, frontmatter_json, last_modified
            FROM pages
            WHERE agent_scope = ? AND corpus = ?
            ORDER BY last_modified DESC
            LIMIT 10
            """,
            (self._agent_scope, corpus),
        ).fetchall()

        if not rows:
            return ""

        lines = ["## Wiki Index\n"]
        for row in rows:
            title = row["title"] or row["name"]
            description = ""
            if row["frontmatter_json"]:
                try:
                    fm = json.loads(row["frontmatter_json"])
                    description = fm.get("description", "") or ""
                except (json.JSONDecodeError, TypeError):
                    pass
            if description:
                lines.append(f"- **{title}** -- {description}")
            else:
                lines.append(f"- **{title}**")

        return "\n".join(lines) + "\n"

    # ── Stats ──────────────────────────────────────────────────────────────

    def stats(self, corpus: Literal["wiki", "raw"]) -> CorpusStats:
        """COUNT / SUM / MAX with double discriminator (SEV-1).

        Returns empty stats when no pages exist.
        """
        _validate_corpus_type(corpus)

        conn = self._get_conn()
        agg_row = conn.execute(
            """
            SELECT COUNT(*) AS page_count,
                   COALESCE(SUM(byte_size), 0) AS total_bytes,
                   MAX(last_modified) AS max_last_modified
            FROM pages
            WHERE agent_scope = ? AND corpus = ?
            """,
            (self._agent_scope, corpus),
        ).fetchone()

        page_count = agg_row["page_count"] or 0
        total_bytes = agg_row["total_bytes"] or 0
        last_update: datetime | None = None
        if agg_row["max_last_modified"]:
            last_update = datetime.fromtimestamp(
                float(agg_row["max_last_modified"]), tz=timezone.utc
            )

        # Most recent 5 pages
        recent_rows = conn.execute(
            """
            SELECT name, title, byte_size, last_modified
            FROM pages
            WHERE agent_scope = ? AND corpus = ?
            ORDER BY last_modified DESC
            LIMIT 5
            """,
            (self._agent_scope, corpus),
        ).fetchall()

        most_recent: list[CorpusRef] = []
        for row in recent_rows:
            if row["last_modified"]:
                last_mod = datetime.fromtimestamp(
                    float(row["last_modified"]), tz=timezone.utc
                )
            else:
                last_mod = datetime.now(tz=timezone.utc)
            most_recent.append(
                CorpusRef(
                    name=row["name"],
                    corpus=corpus,
                    title=row["title"] or row["name"],
                    last_modified=last_mod,
                    byte_size=row["byte_size"] or 0,
                )
            )

        return CorpusStats(
            page_count=page_count,
            total_bytes=total_bytes,
            last_update=last_update,
            most_recent=most_recent,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """Idempotent connection pool teardown (SEV-12).

        1. If ``_closed`` is True, return immediately.
        2. Iterate tracked connections; close each; swallow already-closed errors.
        3. Set ``_closed = True`` and clear the tracker.
        4. Also close the :memory: shared connection if in-memory mode.
        """
        if self._closed:
            return
        if self._in_memory and self._shared_conn is not None:
            try:
                self._shared_conn.close()
            except Exception:
                pass
            self._shared_conn = None
        with self._all_conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
        self._closed = True

    # ── Write page (the critical path) ────────────────────────────────────

    def write_page(
        self,
        name: str,
        content: str,
        corpus: Literal["wiki", "raw"],
        policy: WritePolicy,
        *,
        frontmatter: dict | None = None,
        expected_content_sha256: str | None = None,
    ) -> CorpusRef:
        """Write a corpus page following the 4-case behavior table (spec/34 CQ1).

        Guard order (SEV-3 -- must not be reordered):
        1. ``_validate_corpus_name(name)``
        2. ``_validate_corpus_type(corpus)``
        3. Compute ``_cas_overwrite`` flag (SQL read inside BEGIN IMMEDIATE)
        4. ``safe_resolve_under(body_path, content_root / agent_scope / corpus)``
        5. ``_enforce_corpus_write_policy(body_path, policy)``
        6. ``_sqlite_take_snapshot(old_body_content, ...)`` if CAS overwrite
        7. INSERT/UPSERT SQL row (triggers FTS5 row with body='')
        8. FTS5 explicit upsert with real body text (inside the same transaction)
        9. COMMIT
        10. ``atomic_write(body_path, on_disk_content)`` (the body file)
        11. On disk write failure: compensating SQL transaction restores prior state

        Transaction discipline (C1 fix): steps 3-9 execute inside a single
        BEGIN IMMEDIATE transaction. IMMEDIATE takes a reserved lock at BEGIN,
        blocking concurrent writers and eliminating the TOCTOU window between
        the existence check (step 3) and the UPSERT (step 7).

        Snapshot discipline (C2 fix): the OLD body content is read from disk
        inside the transaction (before the UPSERT), then passed to
        ``_sqlite_take_snapshot`` as a string so the snapshot captures the
        pre-overwrite state, not the new content.

        FTS5 discipline (C3 fix): the FTS upsert is inside the BEGIN IMMEDIATE
        transaction. If FTS fails, the transaction rolls back (no SQL row lands,
        no body file on disk). ``supports_full_text_search=True`` means writes
        index successfully or they fail loudly; silent FTS degradation is not
        acceptable.

        Disk-write compensation: if ``atomic_write`` fails after COMMIT, a
        compensating transaction restores the prior SQL+FTS state. For a
        fresh write (Case 1), this is a DELETE. For a CAS overwrite (Case 3),
        this restores the prior row data and FTS entry from a pre-captured
        snapshot of the old row.

        Double discriminator (SEV-1): every SQL statement includes
        ``WHERE agent_scope = ? AND corpus = ?`` or equivalent in INSERT target.
        """
        # Step 1-2: SEV-8 -- path-traversal guards BEFORE any SQL
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        # Build on-disk content
        on_disk_content = _build_page_content(content, frontmatter)
        incoming_sha = _sha256_hex(on_disk_content)

        body_path = self._body_path(corpus, name)

        # Step 4: safe_resolve_under -- path traversal guard (before BEGIN)
        scope_corpus_dir = self._content_root / self._agent_scope / corpus
        try:
            safe_resolve_under(body_path, scope_corpus_dir)
        except (PathTraversalError, OSError) as exc:
            raise CorpusInvalidName(
                f"page path for {name!r} in {corpus!r} resolves outside "
                f"content_root: {exc}"
            ) from exc

        # Step 5: enforce WritePolicy (before BEGIN -- no DB involvement)
        if policy.write_paths:
            _enforce_corpus_write_policy(body_path, policy)

        # Prepare row data (computed before the transaction)
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        title = _extract_title_from_content(on_disk_content, name)
        byte_size = len(on_disk_content.encode("utf-8"))
        frontmatter_json = _build_frontmatter_json(frontmatter)

        _captured: str | None = None
        _last_seen: str | None = None
        _expires_at: str | None = None
        _ingested_at: str | None = None
        _pinned = 0

        if frontmatter:
            v = frontmatter.get("captured")
            if isinstance(v, (date, datetime)):
                _captured = v.isoformat()
            elif isinstance(v, str):
                _captured = v

            v = frontmatter.get("last_seen")
            if isinstance(v, (date, datetime)):
                _last_seen = v.isoformat()
            elif isinstance(v, str):
                _last_seen = v

            v = frontmatter.get("expires_at")
            if isinstance(v, (date, datetime)):
                _expires_at = v.isoformat()
            elif isinstance(v, str):
                _expires_at = v

            v = frontmatter.get("ingested_at")
            if isinstance(v, datetime):
                _ingested_at = v.isoformat()
            elif isinstance(v, str):
                _ingested_at = v

            _pinned = 1 if frontmatter.get("pinned") else 0

        body_path_str = str(body_path)

        conn = self._get_conn()

        # Steps 3-9: single BEGIN IMMEDIATE transaction.
        # IMMEDIATE takes a reserved lock at BEGIN, serializing concurrent
        # writers against this scope. The read (step 3) and the UPSERT (step 7)
        # share the same snapshot, eliminating the TOCTOU window.
        #
        # Variables captured for post-COMMIT compensation on atomic_write failure:
        _is_fresh_write: bool = False
        _old_row_data: dict | None = None  # pre-UPSERT row for Case 3 rollback
        _old_body_content: str | None = None  # old body text for FTS rollback

        # R2-M1: guard against re-entrance (non-reentrant by design).
        if conn.in_transaction:
            raise RuntimeError(
                "Reentrant write_page is not supported; the connection is "
                "already in a transaction"
            )

        try:
            conn.execute("BEGIN IMMEDIATE")

            # Step 3: read existing row INSIDE the transaction (SEV-3 + C1)
            # R2-C1: include 'title' in the SELECT so compensation can restore it.
            existing_row = conn.execute(
                """
                SELECT rowid, title, body_path, byte_size, last_modified,
                       captured, last_seen, expires_at, ingested_at,
                       pinned, frontmatter_json
                FROM pages
                WHERE agent_scope = ? AND corpus = ? AND name = ?
                """,
                (self._agent_scope, corpus, name),
            ).fetchone()

            _cas_overwrite = False

            if existing_row is not None:
                # Page exists -- read on-disk body to compute existing SHA-256
                existing_body_path = Path(existing_row["body_path"])
                if existing_body_path.is_file():
                    try:
                        existing_body_content: str = existing_body_path.read_text(
                            encoding="utf-8"
                        )
                    except OSError:
                        existing_body_content = ""
                else:
                    existing_body_content = ""
                existing_sha = _sha256_hex(existing_body_content)

                if existing_sha == incoming_sha:
                    # Case 2: content-identical -- idempotent no-op
                    conn.rollback()
                    if existing_row["last_modified"]:
                        last_modified = datetime.fromtimestamp(
                            float(existing_row["last_modified"]), tz=timezone.utc
                        )
                    else:
                        last_modified = datetime.now(tz=timezone.utc)
                    title_noop = _extract_title_from_content(on_disk_content, name)
                    return CorpusRef(
                        name=name,
                        corpus=corpus,
                        title=title_noop,
                        last_modified=last_modified,
                        byte_size=existing_row["byte_size"] or 0,
                    )

                # Content differs -- need CAS to overwrite
                if expected_content_sha256 is None:
                    conn.rollback()
                    raise CorpusPageExists(
                        f"corpus page {name!r} in {corpus!r} already exists and "
                        f"its content differs from the proposed write. Supply "
                        f"expected_content_sha256 matching the current on-disk "
                        f"SHA-256 to opt into the overwrite (CAS) path."
                    )

                if expected_content_sha256 != existing_sha:
                    conn.rollback()
                    raise CorpusPreconditionFailed(
                        f"corpus page {name!r} in {corpus!r}: "
                        f"expected_content_sha256 {expected_content_sha256[:16]}... "
                        f"does not match current on-disk hash "
                        f"{existing_sha[:16]}... -- concurrent write detected; "
                        f"re-read and retry."
                    )

                _cas_overwrite = True
                # Capture old row for disk-write compensation (Case 3)
                _old_row_data = dict(existing_row)
                _old_body_content = existing_body_content
            else:
                _is_fresh_write = True

            # Step 6: SEV-3 -- snapshot existing body BEFORE UPSERT (C2 fix).
            # Pass the OLD body content as a string; the snapshot file is written
            # to disk now (outside the SQL transaction, orphan-tolerant per P7).
            if _cas_overwrite:
                existing_snap_path = Path(existing_row["body_path"])  # type: ignore[index]
                if not existing_snap_path.is_file():
                    conn.rollback()
                    raise CorpusCorrupted(
                        f"Cannot snapshot before CAS overwrite: existing body "
                        f"missing for page={name!r} corpus={corpus!r} "
                        f"scope={self._agent_scope!r}"
                    )
                _sqlite_take_snapshot(
                    existing_body_content,  # type: ignore[possibly-undefined]
                    self._versions_dir(corpus, name),
                    corpus,
                    name,
                )

            # Step 7: UPSERT SQL row (triggers FTS row with body='')
            conn.execute(
                """
                INSERT INTO pages
                    (agent_scope, corpus, name, title, body_path, byte_size,
                     last_modified, captured, last_seen, expires_at, ingested_at,
                     pinned, frontmatter_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_scope, corpus, name) DO UPDATE SET
                    title         = excluded.title,
                    body_path     = excluded.body_path,
                    byte_size     = excluded.byte_size,
                    last_modified = excluded.last_modified,
                    captured      = excluded.captured,
                    last_seen     = excluded.last_seen,
                    expires_at    = excluded.expires_at,
                    ingested_at   = excluded.ingested_at,
                    pinned        = excluded.pinned,
                    frontmatter_json = excluded.frontmatter_json
                """,
                (
                    self._agent_scope,
                    corpus,
                    name,
                    title,
                    body_path_str,
                    byte_size,
                    now_ts,
                    _captured,
                    _last_seen,
                    _expires_at,
                    _ingested_at,
                    _pinned,
                    frontmatter_json,
                ),
            )

            # Step 8: FTS5 explicit upsert with real body text (C3 fix).
            # This is INSIDE the transaction: if FTS fails, the whole transaction
            # rolls back -- the SQL row does not land, disk stays untouched.
            # supports_full_text_search=True means writes index or they fail loudly.
            rowid_row = conn.execute(
                "SELECT rowid FROM pages WHERE agent_scope = ? AND corpus = ? AND name = ?",
                (self._agent_scope, corpus, name),
            ).fetchone()
            if rowid_row:
                rowid = rowid_row[0]
                # Replace the trigger-inserted empty FTS row with real body text
                conn.execute(
                    "INSERT INTO pages_fts(pages_fts, rowid, name, body, frontmatter_json) "
                    "VALUES ('delete', ?, ?, '', ?)",
                    (rowid, name, frontmatter_json),
                )
                conn.execute(
                    "INSERT INTO pages_fts(rowid, name, body, frontmatter_json) "
                    "VALUES (?, ?, ?, ?)",
                    (rowid, name, content, frontmatter_json),
                )

            # Step 9: COMMIT
            conn.commit()

        except (
            CorpusPageExists,
            CorpusPreconditionFailed,
            CorpusInvalidName,
            CorpusCorrupted,
        ):
            # Re-raise known semantic errors; rollback already called above
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

        # Step 10: atomic_write body AFTER COMMIT.
        # If this fails, a compensating transaction restores SQL+FTS state.
        try:
            atomic_write(body_path, on_disk_content)
        except Exception as exc:
            # R2-C2: Before compensating, detect whether the rename inside
            # atomic_write completed before the failure.  If the on-disk body
            # now matches the NEW content (incoming_sha), the rename succeeded
            # and only the post-rename fsync failed.  In that case the new
            # content is already durable on disk, so restoring the OLD SQL row
            # would create an inconsistency (old metadata, new body).  Instead,
            # log a WARNING and re-raise so the caller knows the durability
            # guarantee was weakened -- but keep the new SQL row intact.
            try:
                actual_on_disk = body_path.read_text(encoding="utf-8")
                actual_sha = _sha256_hex(actual_on_disk)
            except OSError:
                actual_sha = None

            if actual_sha == incoming_sha:
                # Rename completed; only the post-rename fsync (or cleanup) failed.
                # The new SQL row and the new body are consistent.  Do not compensate.
                _logger.warning(
                    "atomic_write fsync failed but rename completed for "
                    "page=%r corpus=%r scope=%r; new content is on disk, "
                    "SQL row matches. Durability guarantee weakened.",
                    name,
                    corpus,
                    self._agent_scope,
                )
                raise

            # Rename did NOT complete (or body file is unreadable).
            # Compensating transaction: restore prior state so SQL+FTS agree
            # with what is (or is not) on disk.
            try:
                with conn:
                    if _is_fresh_write:
                        # Case 1: fresh write -- DELETE the row we just inserted
                        conn.execute(
                            "DELETE FROM pages WHERE agent_scope = ? AND corpus = ? AND name = ?",
                            (self._agent_scope, corpus, name),
                        )
                        # The pages_ad trigger fires on DELETE, cleaning up FTS.
                    else:
                        # Case 3: CAS overwrite -- restore the prior row and FTS.
                        # _old_row_data is guaranteed non-None here (set earlier).
                        assert _old_row_data is not None
                        old = _old_row_data
                        # R2-H1: use ON CONFLICT DO UPDATE (not INSERT OR REPLACE)
                        # to preserve the SQLite rowid, matching the initial UPSERT
                        # shape and keeping the FTS rowid stable.
                        conn.execute(
                            """
                            INSERT INTO pages
                                (agent_scope, corpus, name, title, body_path, byte_size,
                                 last_modified, captured, last_seen, expires_at, ingested_at,
                                 pinned, frontmatter_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(agent_scope, corpus, name) DO UPDATE SET
                                title         = excluded.title,
                                body_path     = excluded.body_path,
                                byte_size     = excluded.byte_size,
                                last_modified = excluded.last_modified,
                                captured      = excluded.captured,
                                last_seen     = excluded.last_seen,
                                expires_at    = excluded.expires_at,
                                ingested_at   = excluded.ingested_at,
                                pinned        = excluded.pinned,
                                frontmatter_json = excluded.frontmatter_json
                            """,
                            (
                                self._agent_scope,
                                corpus,
                                name,
                                old.get("title"),
                                old.get("body_path"),
                                old.get("byte_size"),
                                old.get("last_modified"),
                                old.get("captured"),
                                old.get("last_seen"),
                                old.get("expires_at"),
                                old.get("ingested_at"),
                                old.get("pinned"),
                                old.get("frontmatter_json"),
                            ),
                        )
                        # Restore FTS with old body content
                        old_rowid_row = conn.execute(
                            "SELECT rowid FROM pages WHERE agent_scope = ? AND corpus = ? AND name = ?",
                            (self._agent_scope, corpus, name),
                        ).fetchone()
                        if old_rowid_row:
                            old_rowid = old_rowid_row[0]
                            old_fm_json = old.get("frontmatter_json") or "{}"
                            old_body = _old_body_content or ""
                            conn.execute(
                                "INSERT INTO pages_fts(pages_fts, rowid, name, body, frontmatter_json) "
                                "VALUES ('delete', ?, ?, '', ?)",
                                (old_rowid, name, old_fm_json),
                            )
                            conn.execute(
                                "INSERT INTO pages_fts(rowid, name, body, frontmatter_json) "
                                "VALUES (?, ?, ?, ?)",
                                (old_rowid, name, old_body, old_fm_json),
                            )
            except Exception as comp_exc:
                # R2-M2: compensation itself failed -- raise a loud CorpusCorrupted
                # so the operator knows SQL+disk are inconsistent and manual
                # recovery is required.  The original atomic_write error is
                # preserved as the __cause__ and surfaced in the message.
                _logger.exception(
                    "Compensation failed after atomic_write error; SQL+disk may "
                    "be inconsistent for page=%r corpus=%r scope=%r",
                    name,
                    corpus,
                    self._agent_scope,
                )
                raise CorpusCorrupted(
                    f"write_page failed and rollback also failed for "
                    f"page={name!r} corpus={corpus!r} scope={self._agent_scope!r}: "
                    f"manual recovery required. "
                    f"Original error: {exc!r}; compensation error: {comp_exc!r}"
                ) from exc
            raise

        last_modified = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        return CorpusRef(
            name=name,
            corpus=corpus,
            title=title,
            last_modified=last_modified,
            byte_size=byte_size,
        )

    # ── Versioning ─────────────────────────────────────────────────────────

    def snapshot(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        *,
        label: str | None = None,
    ) -> VersionRef:
        """Create an explicit version snapshot of the current page content.

        Reads the body file from disk, writes a snapshot to
        ``<content_root>/<agent_scope>/<corpus>/.versions/<name>/<filename>.md``
        via ``atomic_write``.

        Raises ``CorpusPageNotFound`` when the page does not exist
        (either no SQL row OR no body file on disk).
        """
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        conn = self._get_conn()
        row = conn.execute(
            "SELECT body_path FROM pages WHERE agent_scope = ? AND corpus = ? AND name = ?",
            (self._agent_scope, corpus, name),
        ).fetchone()

        if row is None:
            raise CorpusPageNotFound(
                f"corpus page {name!r} in {corpus!r} does not exist "
                f"(scope={self._agent_scope!r}). Cannot snapshot a "
                f"non-existent page."
            )

        body_path = Path(row["body_path"])
        if not body_path.is_file():
            raise CorpusPageNotFound(
                f"corpus page {name!r} in {corpus!r} body file is missing "
                f"at {body_path!r}. Cannot snapshot."
            )

        body_content = body_path.read_text(encoding="utf-8")
        versions_dir = self._versions_dir(corpus, name)
        return _sqlite_take_snapshot(body_content, versions_dir, corpus, name)

    def list_versions(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> list[VersionRef]:
        """Return version snapshots for ``(name, corpus)`` newest first.

        Reads snapshot filenames from:
        ``<content_root>/<agent_scope>/<corpus>/.versions/<name>/``

        Returns ``[]`` when no versions exist.
        """
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        versions_dir = self._versions_dir(corpus, name)
        if not versions_dir.is_dir():
            return []

        version_refs: list[VersionRef] = []
        for entry in versions_dir.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.endswith(".md"):
                continue
            if entry.name.startswith("."):
                continue
            stem = entry.stem
            if not _NAME_PATTERN.match(stem):
                continue
            # backend_id: "<corpus>/<name>/<version_filename>"
            backend_id = f"{corpus}/{name}/{entry.name}"
            version_refs.append(VersionRef(backend_id=backend_id))

        version_refs.sort(key=lambda v: v.backend_id, reverse=True)
        return version_refs

    def read_version(
        self,
        version_ref: VersionRef,
    ) -> CorpusPage:
        """Return the ``CorpusPage`` for the given version snapshot.

        Raises ``CorpusVersionNotFound`` when:
        (a) the version reference cannot be parsed, OR
        (b) the on-disk body file is missing (hybrid storage: SQL row may
            exist but disk file was deleted -- this is exactly the D12
            infrastructure failure case that mandates raise not None).
        """
        backend_id = version_ref.backend_id
        parts = backend_id.split("/", 2)
        if len(parts) != 3:
            raise CorpusVersionNotFound(
                f"VersionRef {backend_id!r} is not a valid SQLiteCorpusBackend "
                f"version reference (expected '<corpus>/<stem>/<version_filename>')."
            )

        corpus_name, stem, version_filename = parts

        if corpus_name not in ("wiki", "raw"):
            raise CorpusVersionNotFound(
                f"VersionRef {backend_id!r} corpus segment {corpus_name!r} is "
                f"not one of 'wiki' or 'raw'."
            )

        corpus = corpus_name  # type: ignore[assignment]
        version_path = self._versions_dir(corpus, stem) / version_filename

        # Guard against path traversal via crafted backend_id components
        try:
            safe_resolve_under(version_path, self._content_root)
        except (PathTraversalError, OSError) as exc:
            raise CorpusVersionNotFound(
                f"VersionRef {backend_id!r} resolves outside content_root: {exc}"
            ) from exc

        if not version_path.is_file():
            raise CorpusVersionNotFound(
                f"version snapshot file for {stem!r} in {corpus!r} "
                f"({version_filename}) does not exist at {version_path!r}. "
                f"The file may have been externally deleted."
            )

        try:
            raw = version_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CorpusVersionNotFound(
                f"version snapshot file for {stem!r} in {corpus!r} "
                f"({version_filename}) could not be read: {exc}"
            ) from exc

        # Parse the markdown file (same format as live pages)
        try:
            post = _fm.loads(raw)
        except Exception as exc:
            raise CorpusVersionNotFound(
                f"version snapshot file for {stem!r} in {corpus!r} "
                f"({version_filename}) has malformed frontmatter: {exc}"
            ) from exc

        meta = post.metadata
        body = post.content

        title: str = ""
        if "title" in meta:
            title = str(meta["title"])
        elif "name" in meta:
            title = str(meta["name"])
        else:
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
        if not title:
            title = stem

        try:
            stat = version_path.stat()
            last_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            byte_size = stat.st_size
        except OSError:
            last_modified = datetime.now(tz=timezone.utc)
            byte_size = len(raw.encode("utf-8"))

        ref = CorpusRef(
            name=stem,
            corpus=corpus,
            title=title,
            last_modified=last_modified,
            byte_size=byte_size,
        )

        # Use module-level _KNOWN_FRONTMATTER_FIELDS (M1 fix).
        # R2-L1: drop the redundant 'k != "title"' clause -- "title" is already
        # a member of _KNOWN_FRONTMATTER_FIELDS so the extra check is dead code.
        extra: dict = {
            k: v for k, v in meta.items() if k not in _KNOWN_FRONTMATTER_FIELDS
        }

        return CorpusPage(
            ref=ref,
            body=body,
            name=meta.get("name"),
            description=meta.get("description"),
            type=meta.get("type"),
            captured=meta.get("captured"),
            last_seen=meta.get("last_seen"),
            sources=meta.get("sources"),
            provenance=meta.get("provenance"),
            confidence=meta.get("confidence"),
            pinned=bool(meta.get("pinned", False)),
            related=meta.get("related"),
            tags=meta.get("tags"),
            schema_version=meta.get("schema_version"),
            expires_at=meta.get("expires_at"),
            supersedes=meta.get("supersedes"),
            superseded_by=meta.get("superseded_by"),
            source_url=meta.get("source_url"),
            mime_type=meta.get("mime_type"),
            ingested_at=meta.get("ingested_at"),
            extra_frontmatter=extra,
        )

    def restore_version(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> CorpusRef:
        """Restore the page at ``version_ref`` as the live version.

        Delegates to ``write_page`` via the CAS path so the existing live
        version is snapshotted before the restore lands. Does NOT do a
        direct SQL UPDATE -- uses write_page for guard-order correctness
        (per HIGH finding: restore_version MUST delegate to write_page).

        R2-H2 NOTE: The existence check below and the inner write_page call
        are NOT in the same transaction.  If a future delete_page primitive
        lands, a concurrent delete between the existence check and write_page's
        BEGIN IMMEDIATE could fall through to Case 1 (fresh write), bypassing
        the "must exist" guarantee.  Track in a follow-up issue at PR 3 or
        v1.1 -- the fix is to pass a _require_existing=True flag into
        write_page and enforce it inside the BEGIN IMMEDIATE.
        """
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        # H11 fix: refuse to restore to a page that does not yet exist.
        # Without this guard, write_page would create a brand-new page via
        # the fresh-write path, silently ignoring the CAS intent of restore.
        conn = self._get_conn()
        page_row = conn.execute(
            "SELECT body_path FROM pages WHERE agent_scope = ? AND corpus = ? AND name = ?",
            (self._agent_scope, corpus, name),
        ).fetchone()
        if page_row is None:
            raise CorpusPageNotFound(
                f"Cannot restore version: page {name!r} does not exist in "
                f"corpus {corpus!r} scope={self._agent_scope!r}. Create the "
                f"page first before restoring a snapshot."
            )

        # Read the snapshot content (raises CorpusVersionNotFound on failure)
        version_page = self.read_version(version_ref)
        restore_content = version_page.body

        # Rebuild frontmatter dict from the version page
        restore_fm = version_page.extra_frontmatter.copy()
        named = _page_to_frontmatter_dict(version_page)
        if named:
            restore_fm.update(named)
        if not restore_fm:
            restore_fm = None

        # Get current on-disk hash for CAS
        expected_sha: str | None = None
        existing_body_path = Path(page_row["body_path"])
        if existing_body_path.is_file():
            try:
                existing_content = existing_body_path.read_text(encoding="utf-8")
                expected_sha = _sha256_hex(existing_content)
            except OSError:
                pass

        return self.write_page(
            name=name,
            content=restore_content,
            corpus=corpus,
            policy=policy,
            frontmatter=restore_fm,
            expected_content_sha256=expected_sha,
        )

    # ── Search ────────────────────────────────────────────────────────────

    def query(
        self,
        text: str,
        corpus: Literal["wiki", "raw"],
        *,
        top_k: int = 10,
    ) -> list[CorpusRef]:
        """FTS5 full-text search with cross-corpus isolation JOIN (SEV-6).

        FTS query escaping (SEV-5): wraps in double-quotes for phrase-search
        mode; doubles internal double-quotes; returns [] on empty/whitespace.

        Cross-corpus isolation (SEV-6): JOIN back to pages table and filter
        on agent_scope + corpus so FTS results are never cross-contaminated.

        Results ordered by FTS rank (bm25 ascending = most relevant first).
        """
        _validate_corpus_type(corpus)

        # H7/H8: top_k must be a non-negative integer.
        # R2-L2: bool is a subclass of int in Python, so True/False would pass
        # the isinstance(top_k, int) check without the explicit bool guard.
        if (
            top_k is None
            or isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or top_k < 0
        ):
            raise ValueError(f"top_k must be a non-negative integer, got {top_k!r}")
        if top_k == 0:
            return []

        # SEV-5: short-circuit on empty/whitespace
        if not text or not text.strip():
            return []

        escaped = _escape_fts5_query(text)
        if not escaped:
            return []

        conn = self._get_conn()

        try:
            rows = conn.execute(
                """
                SELECT p.name, p.title, p.last_modified, p.byte_size
                FROM pages_fts
                JOIN pages p ON pages_fts.rowid = p.rowid
                WHERE pages_fts MATCH ?
                  AND p.agent_scope = ?
                  AND p.corpus = ?
                ORDER BY pages_fts.rank
                LIMIT ?
                """,
                (escaped, self._agent_scope, corpus, top_k),
            ).fetchall()
        except sqlite3.OperationalError:
            # Defense in depth: FTS parse error (malformed query) returns []
            return []

        refs: list[CorpusRef] = []
        for row in rows:
            if row["last_modified"]:
                last_modified = datetime.fromtimestamp(
                    float(row["last_modified"]), tz=timezone.utc
                )
            else:
                last_modified = datetime.now(tz=timezone.utc)
            refs.append(
                CorpusRef(
                    name=row["name"],
                    corpus=corpus,
                    title=row["title"] or row["name"],
                    last_modified=last_modified,
                    byte_size=row["byte_size"] or 0,
                )
            )
        return refs


# ─────────────────────────────────────────────────────────────────
# Snapshot helper for SQLiteCorpusBackend


def _sqlite_take_snapshot(
    body_content: str,
    versions_dir: Path,
    corpus: str,
    name: str,
) -> VersionRef:
    """Write a version snapshot of the given content string (C2 fix).

    Caller is responsible for reading the OLD body text from disk before
    calling this helper. Passing already-read content (rather than a path)
    ensures the snapshot captures the pre-overwrite state, not the new content
    that may not yet exist on disk.

    Creates the snapshot at:
    ``<versions_dir>/<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md``

    Returns a ``VersionRef`` with backend_id encoding
    ``"<corpus>/<name>/<version_filename>"`` for later retrieval.
    """
    versions_dir.mkdir(parents=True, exist_ok=True)
    version_name = _version_filename(body_content)
    version_path = versions_dir / version_name
    atomic_write(version_path, body_content)
    backend_id = f"{corpus}/{name}/{version_name}"
    return VersionRef(backend_id=backend_id)


# ─────────────────────────────────────────────────────────────────
# URL factory


def make_sqlite_corpus_backend_from_url(
    url: str,
) -> SQLiteCorpusBackend:
    """Parse a ``sqlite://`` URL and construct the backend.

    Accepts:
    * ``"sqlite:///:memory:"`` -> in-memory backend (emits ``RuntimeWarning``).
    * ``"sqlite:///absolute/path/to/corpus.db?agent_scope=<name>"`` -> file-backed.

    6 distinct ValueError sites (SEV from prep brief), each wrapped through
    ``_redact_url`` so credentials never land in exception text:
    1. Non-sqlite scheme.
    2. netloc present (``sqlite://host/path``).
    3. Fragment present (``#fragment``).
    4. Duplicate query parameter.
    5. Unknown query parameter (anything besides ``agent_scope``).
    6. Empty or root-only path.

    Raises:
        ValueError: on any of the 6 malformed-URL conditions above.
        RuntimeWarning: (warning, not error) when ``:memory:`` is requested.
    """
    parsed = urlparse(url.strip())
    safe_url = _redact_url(url)

    # Site 1: non-sqlite scheme
    if parsed.scheme.lower() != "sqlite":
        raise ValueError(
            f"make_sqlite_corpus_backend_from_url: url {safe_url!r} has "
            f"scheme {parsed.scheme!r}; expected 'sqlite'"
        )

    # Site 2: netloc present
    if parsed.netloc:
        raise ValueError(
            f"make_sqlite_corpus_backend_from_url: url {safe_url!r} "
            f"has a netloc; SQLite URLs use the 3-slash convention "
            f"(sqlite:///absolute/path) with empty netloc."
        )

    # Site 3: fragment present
    if parsed.fragment:
        raise ValueError(
            f"make_sqlite_corpus_backend_from_url: url {safe_url!r} "
            f"carries a fragment; not honored by this backend."
        )

    # Parse query params (sites 4 and 5)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
    seen_keys: set[str] = set()
    query_params: dict[str, str] = {}
    for key, value in query_pairs:
        # Site 4: duplicate query parameter
        if key in seen_keys:
            raise ValueError(
                f"make_sqlite_corpus_backend_from_url: url "
                f"{safe_url!r} carries duplicate query parameter "
                f"{key!r}; operator intent is ambiguous."
            )
        seen_keys.add(key)
        query_params[key] = value

    # Site 5: unknown query parameter
    unknown_params = set(query_params) - {"agent_scope"}
    if unknown_params:
        raise ValueError(
            f"make_sqlite_corpus_backend_from_url: url {safe_url!r} "
            f"carries unsupported query parameters: {sorted(unknown_params)}. "
            f"Only 'agent_scope' is recognized."
        )

    agent_scope = query_params.get("agent_scope", "default")

    path = parsed.path
    # Site 6: empty or root-only path
    if not path or path == "/":
        raise ValueError(
            f"make_sqlite_corpus_backend_from_url: url {safe_url!r} "
            f"has an empty path; expected sqlite:///absolute/path/to/corpus.db"
        )

    # :memory: via the triple-slash form
    if path.lower() == "/:memory:":
        return SQLiteCorpusBackend(":memory:", agent_scope)

    return SQLiteCorpusBackend(Path(path), agent_scope)

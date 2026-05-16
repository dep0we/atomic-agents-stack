"""SQLiteAgentProfileBackend — stdlib ``sqlite3`` reference implementation.

This is the second reference impl in the AgentProfileBackend protocol-
pattern series (alongside ``FilesystemAgentProfileBackend``). No optional
dependency required — ``sqlite3`` ships with CPython.

Storage shape (Decision 1 of #63 PR 3 — JSON blob over column-per-field):

* One SQLite file at the path passed to the constructor. Default URL
  parsing via ``ATOMIC_AGENTS_PROFILE_BACKEND_URL=sqlite:///path/to/profiles.db``.
* One ``agents`` table with: ``name`` PK + ``agent_mode`` indexed +
  ``profile_json`` blob + ``updated_at``.
* One ``profile_snapshots`` table for the snapshot trio (Decision 3 —
  JSON-based, mirrors filesystem backend's per-snapshot ``profile.json``
  on disk).
* One ``meta`` table holding ``schema_version`` (with ``INSERT OR
  IGNORE`` cold-start race mitigation per Decision 7 / #61 PR 3 P0 #2).
* Indexes on ``agent_mode`` (for registry queries grouping by mode) and
  ``profile_snapshots.agent_id`` + ``created_at`` (for cross-agent
  isolation + chronological listing).
* WAL journal mode + ``synchronous=NORMAL`` for concurrent reader/writer
  interleaving on local filesystems.

Skills are NOT stored in SQLite (Decision 2 of #63 PR 3) —
``supports_skills=False``; ``list_skills`` returns ``[]``;
``load_skill_body`` raises ``FileNotFoundError``. A future
``save_skill`` Protocol method will land when SaaS UI editing requires
DB-backed skill bodies; until then, skills remain filesystem-only
even when the profile is SQLite-backed.

Thread-safety: ``threading.local`` connection pool gives each thread
its own ``sqlite3.Connection`` for file-backed deployments. sqlite3
connections aren't shared across threads by default. WAL mode +
per-thread connections is the standard pattern; same shape as
``SQLiteLogBackend``.

The ``:memory:`` mode is **single-threaded test-only**: the
constructor opens one connection with the default
``check_same_thread=True`` so cross-thread access raises
``ProgrammingError`` immediately at the misuse site rather than
producing silent corruption or ``SystemError``. Operators who need
multi-threaded SQLite must use the file-backed mode.

Concurrent multi-process: WAL mode supports it on **local filesystems**.
Multiple ``SQLiteAgentProfileBackend`` instances against the same db
from different processes on the SAME host see consistent reads +
serialized writes. **Network-mounted filesystems (NFS, SMB) are NOT
supported** — SQLite WAL on NFS is documented-broken upstream.

Cross-agent snapshot isolation: ``restore(agent_id, snapshot_id)``
filters by BOTH columns in the WHERE clause. An operator with one
agent's snapshot id cannot restore it onto another agent.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..exceptions import (
    AgentProfileExists,
    AgentProfileNotFound,
    SnapshotNotFound,
)
from ..goal import parse_agent_mode_text
from ..skills import SkillManifest
from .types import AgentProfile, ProfileCapabilities, ProfileSnapshot


# Schema version — bumped on any breaking schema change. Idempotent
# init via ``INSERT OR IGNORE`` per Decision 7 (cold-start race
# mitigation, same shape as #61 PR 3 SQLiteLogBackend P0 #2).
_SCHEMA_VERSION = 1


_CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    agent_mode TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_AGENTS_MODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_agents_mode ON agents(agent_mode)
"""

_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS profile_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    profile_json TEXT NOT NULL
)
"""

# Composite (agent_id, created_at) lets list_snapshots scan in
# already-ordered fashion without a separate sort — covers both the
# WHERE-by-agent filter and the ORDER-BY-created_at requirement.
# (Step 9.1 perf finding F-PLR-4.)
_CREATE_SNAPSHOTS_AGENT_CREATED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_snapshots_agent_created
ON profile_snapshots(agent_id, created_at)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)
"""


class SQLiteAgentProfileBackend:
    """SQLite-backed AgentProfileBackend — stdlib sqlite3, no optional dep.

    Conforms to the ``AgentProfileBackend`` Protocol. Constructed once
    per process with a db file path (or ``":memory:"`` for tests).

    Args:
        db_path: filesystem path to the SQLite db file, OR the literal
            string ``":memory:"`` for an in-memory database (test-only —
            emits ``RuntimeWarning`` on construction; all data lost on
            process exit). Wrapping a relative path with ``Path()``
            resolves it lazily at first connection.
    """

    @property
    def backend_id(self) -> str:
        return "sqlite"

    def __init__(self, db_path: Path | str) -> None:
        # Detect :memory: sentinel — string equality, NOT Path coercion
        # (Path(':memory:') would create a real file named ':memory:').
        if db_path == ":memory:":
            self._in_memory = True
            self._db_path_str = ":memory:"
            # Single shared connection for :memory:. ``check_same_thread``
            # left at its default ``True`` — :memory: mode is test-only
            # and single-threaded by design (see module docstring); a
            # ProgrammingError at first cross-thread use is the honest
            # failure mode. PRAGMA journal_mode is a no-op on :memory:
            # databases (SQLite forces 'memory' journal regardless) so
            # the WAL pragma is intentionally skipped here.
            self._shared_conn = sqlite3.connect(self._db_path_str)
            self._shared_conn.row_factory = sqlite3.Row
            self._ensure_schema(self._shared_conn)
            warnings.warn(
                "SQLiteAgentProfileBackend(':memory:') is non-persistent — "
                "all agents and snapshots are lost on process exit. Use "
                "sqlite:///absolute/path/to/profiles.db for durable "
                "deployments.",
                RuntimeWarning,
                stacklevel=2,
            )
        else:
            self._in_memory = False
            self._db_path = Path(db_path)
            self._db_path_str = str(self._db_path)
            self._shared_conn = None

        # Per-thread connection pool (file-backed only; :memory: uses
        # the shared connection above).
        self._tls = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        """Return the calling thread's connection, creating on first use."""
        if self._in_memory:
            return self._shared_conn  # type: ignore[return-value]
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            # Ensure parent dir exists before sqlite3.connect — sqlite3
            # raises OperationalError if the parent dir is missing.
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path_str)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema(conn)
            self._tls.conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables + indexes + meta row. Idempotent across processes.

        Uses ``CREATE TABLE IF NOT EXISTS`` for tables/indexes (always
        idempotent) and ``INSERT OR IGNORE`` for the schema_version row
        — the latter is the multi-process cold-start race mitigation
        per Decision 7 (mirrors #61 PR 3 P0 #2).
        """
        with conn:  # implicit transaction
            conn.execute(_CREATE_AGENTS)
            conn.execute(_CREATE_AGENTS_MODE_INDEX)
            conn.execute(_CREATE_SNAPSHOTS)
            conn.execute(_CREATE_SNAPSHOTS_AGENT_CREATED_INDEX)
            conn.execute(_CREATE_META)
            # INSERT OR IGNORE — whichever of N concurrent processes
            # loses the insert sees the row already there and proceeds.
            # No deadlock, no error.
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
        # Verify schema_version matches expected — defensive guard
        # against opening a future-incompatible db file.
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or int(row["value"]) != _SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLiteAgentProfileBackend schema version mismatch at "
                f"{self._db_path_str}: expected {_SCHEMA_VERSION}, "
                f"found {row['value'] if row else 'no row'}. Migration "
                f"required."
            )

    # ────────────────────────────────────────────────────────────
    # Core read

    def load_profile(self, agent_id: str) -> AgentProfile:
        """SELECT profile_json from agents; reconstruct AgentProfile."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT profile_json FROM agents WHERE name = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found in SQLite backend at {self._db_path_str}"
            )
        try:
            profile_dict = json.loads(row["profile_json"])
        except json.JSONDecodeError as exc:
            raise AgentProfileNotFound(
                f"agent {agent_id!r} profile_json is corrupt: {exc}"
            ) from exc
        return AgentProfile.from_dict(profile_dict)

    # ────────────────────────────────────────────────────────────
    # Core write

    def save_profile(self, agent_id: str, profile: AgentProfile) -> None:
        """INSERT OR REPLACE on the agents table; commits the txn.

        Per Decision 6 in spec/24 (agent_mode is documented-derived),
        the value stored is re-derived from ``persona_identity`` —
        the field on the incoming profile is ignored. This matches
        the filesystem backend's behavior, where ``load_profile``
        re-parses ``persona_identity`` to populate ``agent_mode``.
        Both backends would otherwise diverge on operator-edited
        profiles that mutate ``agent_mode`` directly without updating
        ``persona_identity``.
        """
        # Re-derive agent_mode from persona_identity — Decision 6.
        # The profile.agent_mode field is intentionally ignored.
        derived_mode = parse_agent_mode_text(profile.persona_identity)
        normalized = profile.replace(agent_mode=derived_mode)
        # default=str — AgentProfile.tool_config["read_paths"] contains
        # PosixPath objects from the parser; they aren't JSON-safe
        # without coercion. from_dict re-derives the structured forms
        # from raw text on load, so stringified paths recover losslessly
        # via the parser.
        profile_blob = json.dumps(normalized.to_dict(), default=str)
        updated_at = datetime.now().astimezone().isoformat()
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO agents "
                "(name, agent_mode, profile_json, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (agent_id, derived_mode, profile_blob, updated_at),
            )

    # ────────────────────────────────────────────────────────────
    # Enumeration

    def list_agents(self) -> list[str]:
        """SELECT name FROM agents ORDER BY name."""
        conn = self._get_conn()
        rows = conn.execute("SELECT name FROM agents ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def exists(self, agent_id: str) -> bool:
        """SELECT 1 — O(1) index lookup, no profile_json deserialization."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM agents WHERE name = ? LIMIT 1",
            (agent_id,),
        ).fetchone()
        return row is not None

    # ────────────────────────────────────────────────────────────
    # Skills — NOT stored in SQLite (Decision 2 of #63 PR 3)

    def list_skills(self, agent_id: str) -> list[SkillManifest]:
        """Return empty list — skills are filesystem-only in PR 3.

        Raises ``AgentProfileNotFound`` when the agent itself doesn't
        exist (consistent with FilesystemAgentProfileBackend's contract
        per the Protocol).

        A future ``save_skill`` Protocol method will land when SaaS UI
        editing requires DB-backed skill bodies. Until then,
        ``supports_skills=False`` and ``list_skills`` returns ``[]``.
        Conformance tests for skill CONTENT gate on the
        ``supports_skills`` capability.
        """
        if not self.exists(agent_id):
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found in SQLite backend"
            )
        return []

    def load_skill_body(self, agent_id: str, skill_name: str) -> str:
        """Always raises FileNotFoundError — skills not stored in SQLite.

        Raises ``AgentProfileNotFound`` for missing agents (so callers
        can distinguish bad agent_id from bad skill_name). For a
        present agent, every skill_name is "unknown" — the SQLite
        backend does not store skills (Decision 2).
        """
        if not self.exists(agent_id):
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found in SQLite backend"
            )
        raise FileNotFoundError(
            f"skill {skill_name!r} not found for agent {agent_id!r} — "
            f"SQLiteAgentProfileBackend does not store skills "
            f"(supports_skills=False). Skills remain filesystem-only."
        )

    # ────────────────────────────────────────────────────────────
    # Clone

    def clone(
        self,
        source_id: str,
        target_id: str,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        """Load source → apply overrides → save as target.

        Refuses to overwrite existing target (raise
        ``AgentProfileExists``). Same Protocol contract as the
        filesystem backend.
        """
        if not self.exists(source_id):
            raise AgentProfileNotFound(
                f"clone source {source_id!r} does not exist in SQLite backend"
            )
        if self.exists(target_id):
            raise AgentProfileExists(
                f"clone target {target_id!r} already exists in SQLite "
                f"backend; use save_profile() to overwrite intentionally"
            )

        source_profile = self.load_profile(source_id)

        if overrides:
            # Validate overrides keys before applying — same shape as
            # filesystem backend's clone.
            from dataclasses import fields as _dc_fields

            valid_field_names = {f.name for f in _dc_fields(AgentProfile)}
            unknown = set(overrides.keys()) - valid_field_names
            if unknown:
                raise ValueError(
                    f"clone overrides contain unknown AgentProfile field "
                    f"names: {sorted(unknown)}. Known fields: "
                    f"{sorted(valid_field_names)}"
                )
            from dataclasses import replace as _dc_replace

            new_profile = _dc_replace(source_profile, name=target_id, **overrides)
        else:
            from dataclasses import replace as _dc_replace

            new_profile = _dc_replace(source_profile, name=target_id)

        self.save_profile(target_id, new_profile)
        # Skills are NOT copied — they aren't stored in SQLite.

    # ────────────────────────────────────────────────────────────
    # Snapshot trio — JSON-based (Decision 3 of #63 PR 3)

    def snapshot(self, agent_id: str, label: str) -> str:
        """Capture current profile as a JSON blob row in profile_snapshots.

        Returns the generated ``snapshot_id``. Cross-agent isolation
        enforced at restore time via the WHERE clause filter (snapshot
        rows carry their agent_id; restore filters on both
        snapshot_id AND agent_id, so a snapshot for agent A cannot be
        restored onto agent B).
        """
        # Reuse load_profile — raises AgentProfileNotFound if missing.
        profile = self.load_profile(agent_id)

        # 6 hex (24 bits) had ~52% collision probability per second at
        # 4K snapshots/sec — fleet-scale concern flagged in Step 11
        # adversarial F-8. 12 hex (48 bits) brings same-second collision
        # at 4K/sec down to ~6e-8.
        snapshot_id = (
            f"snap_"
            f"{datetime.now().astimezone().strftime('%Y-%m-%dT%H%M%S')}_"
            f"{secrets.token_hex(6)}"
        )
        created_at = datetime.now().astimezone().isoformat()
        # default=str — see save_profile for the Path-coercion rationale.
        profile_blob = json.dumps(profile.to_dict(), default=str)

        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT INTO profile_snapshots "
                "(snapshot_id, agent_id, label, created_at, profile_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, agent_id, label, created_at, profile_blob),
            )
        return snapshot_id

    def restore(self, agent_id: str, snapshot_id: str) -> None:
        """SELECT profile_json WHERE snapshot_id AND agent_id; save back.

        **Cross-agent isolation** enforced by the AND clause — a
        snapshot belonging to agent B cannot be restored onto agent A.
        No row → raise ``SnapshotNotFound``.
        """
        if not snapshot_id:
            raise SnapshotNotFound("snapshot_id must not be empty")
        conn = self._get_conn()
        row = conn.execute(
            "SELECT profile_json FROM profile_snapshots "
            "WHERE snapshot_id = ? AND agent_id = ? "
            "LIMIT 1",
            (snapshot_id, agent_id),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(
                f"snapshot {snapshot_id!r} not found for agent "
                f"{agent_id!r} in SQLite backend (cross-agent isolation: "
                f"snapshots from other agents are not visible)"
            )
        try:
            profile_dict = json.loads(row["profile_json"])
        except json.JSONDecodeError as exc:
            raise SnapshotNotFound(
                f"snapshot {snapshot_id!r} profile_json is corrupt: {exc}"
            ) from exc
        restored_profile = AgentProfile.from_dict(profile_dict)
        self.save_profile(agent_id, restored_profile)

    def list_snapshots(self, agent_id: str) -> list[ProfileSnapshot]:
        """SELECT snapshots for the agent in chronological order."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT snapshot_id, agent_id, label, created_at "
            "FROM profile_snapshots "
            "WHERE agent_id = ? "
            "ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        return [
            ProfileSnapshot(
                snapshot_id=row["snapshot_id"],
                label=row["label"],
                created_at=row["created_at"],
                agent_id=row["agent_id"],
            )
            for row in rows
        ]

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> ProfileCapabilities:
        return ProfileCapabilities(
            supports_save=True,
            supports_clone=True,
            supports_snapshot=True,
            supports_subscribe=False,
            durable=not self._in_memory,
            # #63 PR 3 Decision 2: skills NOT stored in SQLite.
            # ``list_skills`` returns []; ``load_skill_body`` raises
            # FileNotFoundError. A future Protocol method ``save_skill``
            # lands when SaaS UI editing requires DB-backed skill bodies.
            supports_skills=False,
        )


# ────────────────────────────────────────────────────────────────────
# URL factory — mirrors ``make_sqlite_backend_from_url`` from logs/sqlite.py


def make_sqlite_profile_backend_from_url(url: str) -> SQLiteAgentProfileBackend:
    """Parse a ``sqlite://`` URL and construct the backend.

    Accepts:
    * ``"sqlite::memory:"`` or ``"sqlite:///:memory:"`` → in-memory
      backend (emits ``RuntimeWarning``).
    * ``"sqlite:///absolute/path/to/profiles.db"`` (3 slashes) →
      file-backed.

    Raises ``ValueError`` for:
    * non-``sqlite`` scheme
    * netloc-bearing URLs like ``sqlite://host/path`` (ambiguous
      two-vs-three-slash typo guard)
    * empty path or root-only path (``"sqlite:///"``)
    * URLs carrying ``?query`` or ``#fragment`` — SQLAlchemy-style
      directives (``?mode=ro``, ``?cache=shared``) are NOT honored by
      this backend; silently dropping them would let a read-only
      intent land as read-write in production (Step 11 adversarial
      F-5). Operators wanting those modes must construct the backend
      programmatically with the appropriate ``sqlite3.connect`` args.

    The 3-slash convention is RFC-3986: ``scheme://AUTHORITY/PATH``
    with empty authority + absolute path. Operators with relative-path
    intent should use the constructor directly:
    ``SQLiteAgentProfileBackend(Path("./profiles.db"))``.
    """
    # ``sqlite::memory:`` (no slashes) is the conventional in-memory
    # shorthand — match it before urlparse mangles the structure.
    # Case-insensitive + strip the URL so ``sqlite::Memory:`` (operator
    # typo) doesn't fall through and create a real file named
    # ``:Memory:`` (Step 11 adversarial F-4).
    if url.strip().lower() == "sqlite::memory:":
        return SQLiteAgentProfileBackend(":memory:")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "sqlite":
        raise ValueError(
            f"make_sqlite_profile_backend_from_url: url {url!r} has "
            f"scheme {parsed.scheme!r}; expected 'sqlite'"
        )
    if parsed.netloc:
        # ``sqlite://host/path`` is ambiguous — operator probably meant
        # ``sqlite:///host/path`` (three slashes for an absolute path
        # starting with /host). Refuse to guess.
        raise ValueError(
            f"make_sqlite_profile_backend_from_url: url {url!r} has a "
            f"netloc ({parsed.netloc!r}); SQLite URLs use the 3-slash "
            f"convention (sqlite:///absolute/path) with empty netloc. "
            f"Operator likely meant sqlite:///{parsed.netloc}{parsed.path}."
        )
    if parsed.query or parsed.fragment:
        # SQLAlchemy-style ``?mode=ro``, ``?cache=shared``, ``#name``
        # are not honored. Silent drop is the failure mode (Step 11
        # adversarial F-5): operator intends read-only, lands
        # read-write in production.
        raise ValueError(
            f"make_sqlite_profile_backend_from_url: url {url!r} carries "
            f"a query or fragment; this backend does not honor "
            f"SQLAlchemy-style URL directives. Construct the backend "
            f"programmatically with custom sqlite3.connect args instead."
        )
    path = parsed.path
    if not path or path == "/":
        raise ValueError(
            f"make_sqlite_profile_backend_from_url: url {url!r} has an "
            f"empty path; expected sqlite:///absolute/path/to/profiles.db"
        )
    # ``sqlite:///:memory:`` (case-insensitive) → in-memory.
    if path.lower() == "/:memory:":
        return SQLiteAgentProfileBackend(":memory:")
    return SQLiteAgentProfileBackend(Path(path))

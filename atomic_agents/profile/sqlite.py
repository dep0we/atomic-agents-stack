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
import logging
import re
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


_logger = logging.getLogger(__name__)


# Schema version — bumped on any breaking schema change. v1 → v2
# migration adds the ``agents.persona_id`` column (#62 PR 2, D-PP-2).
# Forward-only migration: v1 → v2 happens automatically; v2 → v1 is
# not supported.
_SCHEMA_VERSION = 2

# persona_id charset — same Protocol-wide rule used elsewhere
# (PolicyBackend, PersonaBackend, persona_link_md). Cached at module
# level so the per-call hot path is one regex match.
_PERSONA_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.+@-]+$")
_PERSONA_ID_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


_CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    agent_mode TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    persona_id TEXT
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
        # Track which agents have already received the
        # agent_profile_save_dropped_persona_fields warning so the
        # log event fires at most once per agent per backend instance.
        self._warned_drop_agents: set[str] = set()
        # Per-pair dedup for D-PP-13 migration-window restore event.
        # Mirrors the save-side ``_warned_drop_agents`` set above.
        # Keyed on ``(agent_id, snapshot_id)`` tuples so the event fires
        # at most once per (agent, snapshot) pair per process.
        self._warned_restore_drop: set[tuple[str, str]] = set()

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
            # busy_timeout BEFORE the WAL pragma — same shape as
            # logs/sqlite.py and registry/sqlite.py. Without this,
            # concurrent processes get an immediate SQLITE_BUSY on
            # WAL negotiation rather than a graceful 5s wait.
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema(conn)
            self._tls.conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables + indexes + meta row. Idempotent across processes.

        **v1 → v2 migration** adds the ``agents.persona_id`` column
        (#62 PR 2, D-PP-2). Cold-start DBs initialize directly at v2
        with the column present. Existing v1 DBs are upgraded on the
        first PR 2 process start via explicit ``ALTER TABLE``.

        SQLite DDL note (D-PP-2): Python's ``sqlite3`` driver
        implicit-commits before DDL statements. ``ALTER TABLE`` inside
        a ``with conn:`` block commits at execute time, NOT at
        ``__exit__``. A process kill between ALTER and the subsequent
        meta UPDATE leaves the DB in state ``(column present,
        schema_version=1)``; the duplicate-column branch below handles
        this case correctly.

        Multi-process safety: the ``with conn:`` block for DDL +
        ``INSERT OR IGNORE`` is a serialized SQLite transaction; the
        separate ALTER + UPDATE in the v1→v2 path runs under SQLite's
        WAL + ``busy_timeout=5000`` (set by ``_get_conn`` on file-backed
        connections) giving concurrent processes a 5 s grace window.
        """
        with conn:  # implicit transaction — idempotent DDL + cold-start v2
            conn.execute(_CREATE_AGENTS)
            conn.execute(_CREATE_AGENTS_MODE_INDEX)
            conn.execute(_CREATE_SNAPSHOTS)
            conn.execute(_CREATE_SNAPSHOTS_AGENT_CREATED_INDEX)
            conn.execute(_CREATE_META)
            # INSERT OR IGNORE — whichever of N concurrent processes
            # loses the insert sees the row already there and proceeds.
            # For cold-start DBs this lands "2" directly; for existing v1
            # DBs the row already contains "1" (no-op here).
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", "2"),
            )

        # Read the authoritative schema_version row.
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        version = int(row["value"]) if row else 0

        if version == 2:
            # Already at current schema — nothing to do.
            return

        if version == 1:
            # v1 → v2: add the persona_id column.  ALTER TABLE is NOT
            # transactional in SQLite (auto-commits at execute time), so
            # we run it outside the ``with conn:`` block and handle the
            # race-loser case (column already present) explicitly.
            try:
                conn.execute("ALTER TABLE agents ADD COLUMN persona_id TEXT")
                conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
                conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    # Race winner already added the column but may have
                    # crashed before updating meta.  Update meta now
                    # (idempotent — if winner also updated meta this is a
                    # benign no-op because value is already '2').
                    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
                    conn.commit()
                else:
                    raise
            return

        # version not in (1, 2) → future schema, framework downgrade refused.
        raise RuntimeError(
            f"SQLiteAgentProfileBackend schema version mismatch at "
            f"{self._db_path_str}: found version {version}, "
            f"this build supports versions 1 and 2. "
            f"Downgrade not supported — use the same or a newer version "
            f"of atomic-agents-stack to open this database."
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

        Per D-PP-8 / D6 (#62 PR 2): when the agent already has a
        non-NULL ``persona_id`` in the DB (externally owned), the
        incoming ``persona_identity``, ``persona_soul``, and
        ``persona_user`` fields are silently dropped before write.
        A one-time ``agent_profile_save_dropped_persona_fields`` log
        warning is emitted per agent (tracked in
        ``self._warned_drop_agents``). This is the SQLite-backend
        silent-precedence pattern; the filesystem backend raises
        ``PersonaOwnershipConflict`` instead (D-PP-8 asymmetry).
        """
        conn = self._get_conn()
        updated_at = datetime.now().astimezone().isoformat()

        # Read + decide + write inside one transaction so the persona_id
        # read and the INSERT OR REPLACE are atomic. /ship Step 9 caught
        # a TOCTOU: a concurrent ``set_persona_ownership(agent, new)`` call
        # between an outside-transaction SELECT and the INSERT would let
        # the stale ``current_persona_id`` overwrite the just-set value.
        # Moving the SELECT inside ``with conn:`` closes the race.
        with conn:
            # Check current persona_id from the DB for D-PP-8 silent-drop.
            # For new agents (no row yet) persona_id is implicitly NULL.
            existing_row = conn.execute(
                "SELECT persona_id FROM agents WHERE name = ?",
                (agent_id,),
            ).fetchone()
            current_persona_id: str | None = (
                existing_row["persona_id"] if existing_row is not None else None
            )

            if current_persona_id is not None:
                # Agent is externally owned — drop inline persona fields (D6,
                # D-PP-8). Only emit the warning + zero the fields when at least
                # one persona field is actually non-empty: when the profile
                # already has empty persona fields (the normal post-bootstrap
                # shape for an externally-owned agent), no warning fires and no
                # replace is needed, preventing spurious "dropped" noise on every
                # routine save.
                if (
                    profile.persona_identity
                    or profile.persona_soul
                    or profile.persona_user
                ):
                    if agent_id not in self._warned_drop_agents:
                        _logger.warning(
                            "agent_profile_save_dropped_persona_fields "
                            "agent_id=%r dropped_fields=%r",
                            agent_id,
                            ["persona_identity", "persona_soul", "persona_user"],
                        )
                        self._warned_drop_agents.add(agent_id)
                    profile = profile.replace(
                        persona_identity="",
                        persona_soul="",
                        persona_user="",
                    )

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

            conn.execute(
                "INSERT OR REPLACE INTO agents "
                "(name, agent_mode, profile_json, updated_at, persona_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, derived_mode, profile_blob, updated_at, current_persona_id),
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
    # Persona ownership composition (#62 PR 2 — D-PP-3 + D-PP-7)

    def external_persona_ref(self, agent_id: str) -> str | None:
        """SELECT persona_id FROM agents WHERE name = ?.

        Returns the persona_id string when the agent is externally
        owned (column is non-NULL), or ``None`` when internally owned
        (column is NULL). Raises ``AgentProfileNotFound`` when no row
        exists for the agent.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT persona_id FROM agents WHERE name = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found in SQLite backend at {self._db_path_str}"
            )
        # persona_id is None when the column value is SQL NULL.
        return row["persona_id"]

    def set_persona_ownership(self, agent_id: str, persona_id: str | None) -> None:
        """UPDATE agents SET persona_id = ? WHERE name = ?.

        When ``persona_id`` is non-None, validates the charset
        (Protocol-wide rule: ``[a-zA-Z0-9_.+@-]+``; no leading dot,
        no ``..``, no path separators, no control characters, no empty
        string). Raises ``ValueError`` on charset failure.

        Raises ``AgentProfileNotFound`` when the agent does not exist.

        Per D-PP-8, the SQLite backend does NOT raise
        ``PersonaOwnershipConflict`` on set — silent precedence is the
        correct default for programmatic write paths (filesystem is
        loud because two files on disk is a visible operator mistake;
        SQLite writes go through this API). The next ``save_profile``
        call will silently drop inline persona fields when
        ``persona_id`` is non-NULL, emitting a one-time
        ``agent_profile_save_dropped_persona_fields`` log event.
        """
        if persona_id is not None:
            # --- charset validation (same rules as PersonaBackend + PolicyBackend) ---
            if not persona_id:
                raise ValueError("persona_id must not be empty")
            if persona_id.startswith("."):
                raise ValueError(f"persona_id {persona_id!r} must not start with '.'")
            if ".." in persona_id:
                raise ValueError(f"persona_id {persona_id!r} must not contain '..'")
            if "/" in persona_id or "\\" in persona_id:
                raise ValueError(
                    f"persona_id {persona_id!r} must not contain path separators"
                )
            if _PERSONA_ID_CONTROL_CHARS.search(persona_id):
                raise ValueError(
                    f"persona_id {persona_id!r} contains control characters"
                )
            if not _PERSONA_ID_PATTERN.match(persona_id):
                raise ValueError(
                    f"persona_id {persona_id!r} contains characters outside "
                    f"the allowed set [a-zA-Z0-9_.+@-]"
                )

        if not self.exists(agent_id):
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found in SQLite backend at {self._db_path_str}"
            )

        if persona_id is None:
            # Unbind path — clear the warned-set so a future rebind
            # correctly re-fires the one-time silent-drop warning (P2-B round 2).
            self._warned_drop_agents.discard(agent_id)

        conn = self._get_conn()
        with conn:
            conn.execute(
                "UPDATE agents SET persona_id = ? WHERE name = ?",
                (persona_id, agent_id),
            )

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
        # D3 snapshot composition (#62 PR 3): when the agent's persona is
        # externally owned, drop persona fields from the snapshot blob.
        # PersonaBackend owns the persona history; AgentProfile snapshots
        # become "config snapshots" that carry only the non-persona fields.
        # Internally-owned agents keep persona fields — their persona IS
        # the AgentProfile.
        from dataclasses import replace as _dc_replace

        snap_profile = profile
        if self.external_persona_ref(agent_id) is not None:
            snap_profile = _dc_replace(
                profile,
                persona_identity="",
                persona_soul="",
                persona_user="",
            )

        # default=str — see save_profile for the Path-coercion rationale.
        profile_blob = json.dumps(snap_profile.to_dict(), default=str)

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

        # D-PP-13 migration-window event: snapshot was taken before the
        # agent's persona was migrated to PersonaBackend, so the snapshot
        # blob carries non-empty persona fields. Detect + emit once, then
        # drop the fields so save_profile doesn't re-write them.
        _PERSONA_FIELDS = ["persona_identity", "persona_soul", "persona_user"]
        snap_has_persona = any(profile_dict.get(f) for f in _PERSONA_FIELDS)
        if snap_has_persona and self.external_persona_ref(agent_id) is not None:
            pair = (agent_id, snapshot_id)
            if pair not in self._warned_restore_drop:
                _logger.warning(
                    "agent_profile_restore_dropped_persona_fields "
                    "agent_id=%s snapshot_id=%s dropped_fields=%s",
                    agent_id,
                    snapshot_id,
                    _PERSONA_FIELDS,
                )
                self._warned_restore_drop.add(pair)
            for field in _PERSONA_FIELDS:
                profile_dict[field] = ""

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

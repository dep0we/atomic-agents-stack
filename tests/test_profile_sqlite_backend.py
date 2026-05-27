"""SQLite-specific tests for ``SQLiteAgentProfileBackend`` (#63 PR 3).

The conformance suite (``test_profile_protocol_conformance.py``) covers
the Protocol contract on both backends; this module covers the SQLite-
specific shape: schema creation, version tracking, cold-start race,
WAL mode probe, JSON blob round-trip, in-memory RuntimeWarning, URL
parsing, registry resolution, and ``get_default_profile_backend``
env-var dispatch.

Mirrors the layout of ``test_log_sqlite_backend.py`` from the #61 arc.
"""

from __future__ import annotations

import sqlite3
import warnings

import pytest

from atomic_agents.exceptions import (
    AgentProfileExists,
    AgentProfileNotFound,
    BackendNotRegistered,
    SnapshotNotFound,
)
from atomic_agents.profile import (
    AgentProfile,
    SQLiteAgentProfileBackend,
    get_default_profile_backend,
    get_profile_backend,
    list_profile_backends,
    make_sqlite_profile_backend_from_url,
)


# ─── helper: build a complete AgentProfile from raw bodies ───────────


_IDENTITY = "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n"
_SOUL = "# Soul\n\nCurious.\n"
_MODEL = (
    "# Model\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
    "## Fallback\n\nclaude-haiku-4-5-20251001\n"
)
_TOOLS = (
    "# Tools\n\n## Read paths\n\n- ~/scout/data\n\n## Write paths\n\n"
    "- ~/scout/notes\n\n## Tool classification\n\n"
    "- write_atomic_note: reversible_write\n"
)
_ROSTER = "# Roster\n\n## Delegate to\n\n- editor\n"
_MCP = "# MCP servers\n\n## fs-tools\ncommand: npx\nargs: -y, @mcp/server\n"


def _make_profile(name: str, *, identity: str = _IDENTITY) -> AgentProfile:
    """Build an AgentProfile via from_dict — same path as production."""
    return AgentProfile.from_dict(
        {
            "name": name,
            "agent_mode": "reactive",
            "persona_identity": identity,
            "persona_soul": _SOUL,
            "persona_user": "",
            "goal_text": "",
            "model_md_raw": _MODEL,
            "tools_md_raw": _TOOLS,
            "judges_md_raw": None,
            "roster_md_raw": _ROSTER,
            "mcp_md_raw": _MCP,
            "model_config": {},
            "tool_config": {},
            "tool_classifications": {},
            "judges_config": None,
            "roster": [],
            "mcp_servers": [],
        }
    )


# ─── Schema + version tracking ───────────────────────────────────────


def test_schema_tables_created(tmp_path):
    """Constructor creates agents, profile_snapshots, meta tables."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    # Force a connection to flush schema creation.
    backend.list_agents()
    conn = sqlite3.connect(str(tmp_path / "profiles.db"))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "agents" in tables
    assert "profile_snapshots" in tables
    assert "meta" in tables


def test_schema_version_row_present(tmp_path):
    """meta table has schema_version=2 row (v2 since #62 PR 2 added persona_id)."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.list_agents()
    conn = sqlite3.connect(str(tmp_path / "profiles.db"))
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert row[0] == "2"


def test_schema_init_idempotent_cold_start_race(tmp_path):
    """Two backend instances against the same db both initialize without error."""
    db_path = tmp_path / "profiles.db"
    backend_a = SQLiteAgentProfileBackend(db_path)
    backend_b = SQLiteAgentProfileBackend(db_path)
    backend_a.list_agents()
    backend_b.list_agents()
    # Both connections succeed; schema is at v2 (#62 PR 2 persona_id column).
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    assert len(rows) == 1
    assert rows[0] == ("schema_version", "2")


def test_schema_version_mismatch_raises(tmp_path):
    """Opening a db with a different schema_version raises."""
    db_path = tmp_path / "profiles.db"
    # Manually create the db with a wrong schema_version.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '999')")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema version mismatch"):
        SQLiteAgentProfileBackend(db_path).list_agents()


# ─── WAL probe ────────────────────────────────────────────────────────


def test_wal_journal_mode_enabled(tmp_path):
    """Journal mode is WAL — required for multi-process readers + 1 writer."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.list_agents()
    conn = sqlite3.connect(str(tmp_path / "profiles.db"))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ─── In-memory backend ───────────────────────────────────────────────


def test_in_memory_emits_runtime_warning():
    """Constructing with :memory: warns about non-persistence."""
    with pytest.warns(RuntimeWarning, match="non-persistent"):
        SQLiteAgentProfileBackend(":memory:")


def test_in_memory_capabilities_durable_false():
    """In-memory backend reports durable=False."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        backend = SQLiteAgentProfileBackend(":memory:")
    caps = backend.capabilities()
    assert caps.durable is False


def test_in_memory_round_trip():
    """save → load works in-memory."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        backend = SQLiteAgentProfileBackend(":memory:")
    profile = _make_profile("scout")
    backend.save_profile("scout", profile)
    loaded = backend.load_profile("scout")
    assert loaded.name == "scout"
    assert loaded.persona_identity == _IDENTITY


# ─── JSON blob round-trip preserves AgentProfile ──────────────────────


def test_json_blob_round_trip_preserves_raw_fields(tmp_path):
    """The JSON serialization preserves persona/tools/mcp byte-for-byte."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    profile = _make_profile("scout")
    backend.save_profile("scout", profile)
    loaded = backend.load_profile("scout")
    assert loaded.persona_identity == profile.persona_identity
    assert loaded.tools_md_raw == profile.tools_md_raw
    assert loaded.roster_md_raw == profile.roster_md_raw
    assert loaded.mcp_md_raw == profile.mcp_md_raw


def test_save_normalizes_agent_mode_from_identity(tmp_path):
    """Decision 6 — save_profile re-derives agent_mode from persona_identity."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    profile = _make_profile("scout")  # identity says "reactive"
    bogus = profile.replace(agent_mode="goal-driven")
    backend.save_profile("scout", bogus)
    loaded = backend.load_profile("scout")
    assert loaded.agent_mode == "reactive"


# ─── agent_mode column indexed ────────────────────────────────────────


def test_agent_mode_column_indexed(tmp_path):
    """idx_agents_mode is present (supports registry-by-mode queries)."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.list_agents()
    conn = sqlite3.connect(str(tmp_path / "profiles.db"))
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_agents_mode" in indexes
    # Composite index covers BOTH the WHERE-by-agent filter and the
    # ORDER-BY-created_at requirement in list_snapshots (#63 PR 3 Step 11
    # adversarial F-PLR-4).
    assert "idx_snapshots_agent_created" in indexes


# ─── Skill semantics ─────────────────────────────────────────────────


def test_list_skills_returns_empty_for_present_agent(tmp_path):
    """SQLite never stores skills — list_skills returns []."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("scout", _make_profile("scout"))
    assert backend.list_skills("scout") == []


def test_list_skills_missing_agent_raises(tmp_path):
    """list_skills surfaces AgentProfileNotFound for missing agents."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    with pytest.raises(AgentProfileNotFound):
        backend.list_skills("nope")


def test_load_skill_body_always_raises_not_found(tmp_path):
    """SQLite has no skill storage — every skill_name is FileNotFoundError."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("scout", _make_profile("scout"))
    with pytest.raises(FileNotFoundError):
        backend.load_skill_body("scout", "any-skill-name")


def test_supports_skills_capability_false(tmp_path):
    """SQLite backend declares supports_skills=False (#63 PR 3 Decision 2)."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    assert backend.capabilities().supports_skills is False


# ─── Snapshot table queries ──────────────────────────────────────────


def test_snapshot_writes_to_profile_snapshots_table(tmp_path):
    """snapshot() inserts a row into profile_snapshots."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("scout", _make_profile("scout"))
    snapshot_id = backend.snapshot("scout", "test-snap")
    conn = sqlite3.connect(str(tmp_path / "profiles.db"))
    row = conn.execute(
        "SELECT snapshot_id, agent_id, label FROM profile_snapshots "
        "WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == snapshot_id
    assert row[1] == "scout"
    assert row[2] == "test-snap"


def test_snapshot_cross_agent_isolation_at_query_level(tmp_path):
    """list_snapshots WHERE agent_id filters cross-agent rows."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("alpha", _make_profile("alpha"))
    backend.save_profile("bravo", _make_profile("bravo"))
    alpha_snap = backend.snapshot("alpha", "alpha-snap")
    bravo_snap = backend.snapshot("bravo", "bravo-snap")
    alpha_list = backend.list_snapshots("alpha")
    bravo_list = backend.list_snapshots("bravo")
    assert [s.snapshot_id for s in alpha_list] == [alpha_snap]
    assert [s.snapshot_id for s in bravo_list] == [bravo_snap]


def test_snapshot_restore_cross_agent_raises(tmp_path):
    """Restoring agent-a's snapshot onto agent-b raises SnapshotNotFound."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("alpha", _make_profile("alpha"))
    backend.save_profile("bravo", _make_profile("bravo"))
    alpha_snap = backend.snapshot("alpha", "alpha-snap")
    with pytest.raises(SnapshotNotFound):
        backend.restore("bravo", alpha_snap)


def test_restore_empty_snapshot_id_raises(tmp_path):
    """Empty snapshot_id raises SnapshotNotFound up front."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("scout", _make_profile("scout"))
    with pytest.raises(SnapshotNotFound):
        backend.restore("scout", "")


# ─── Clone semantics ─────────────────────────────────────────────────


def test_clone_copies_via_save_profile(tmp_path):
    """Clone uses save_profile internally — agent_mode normalization still applies."""
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("source", _make_profile("source"))
    backend.clone(
        "source",
        "target",
        overrides={
            "persona_identity": (
                "# Hybrid\n\n## Operating mode\n\nThis agent is hybrid.\n"
            )
        },
    )
    loaded = backend.load_profile("target")
    assert loaded.name == "target"
    assert loaded.agent_mode == "hybrid"


def test_clone_unknown_override_field_raises(tmp_path):
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("source", _make_profile("source"))
    with pytest.raises(ValueError, match="unknown AgentProfile field"):
        backend.clone("source", "target", overrides={"not_a_field": 1})


def test_clone_refuses_overwrite_target(tmp_path):
    backend = SQLiteAgentProfileBackend(tmp_path / "profiles.db")
    backend.save_profile("source", _make_profile("source"))
    backend.save_profile("target", _make_profile("target"))
    with pytest.raises(AgentProfileExists):
        backend.clone("source", "target")


# ─── URL parsing ─────────────────────────────────────────────────────


def test_url_parses_absolute_path(tmp_path):
    """sqlite:///<absolute> → file-backed backend at that path."""
    url = f"sqlite://{tmp_path / 'profiles.db'}"  # three-slash via path absolute
    # Build the canonical three-slash form explicitly:
    url = f"sqlite:///{(tmp_path / 'profiles.db').as_posix().lstrip('/')}"
    # Simpler: just construct directly.
    url = "sqlite:///" + str(tmp_path / "profiles.db")
    backend = make_sqlite_profile_backend_from_url(url)
    assert isinstance(backend, SQLiteAgentProfileBackend)
    backend.save_profile("scout", _make_profile("scout"))
    assert backend.exists("scout")


def test_url_memory_shorthand():
    """sqlite::memory: → in-memory backend with warning."""
    with pytest.warns(RuntimeWarning):
        backend = make_sqlite_profile_backend_from_url("sqlite::memory:")
    assert isinstance(backend, SQLiteAgentProfileBackend)


def test_url_three_slash_memory():
    """sqlite:///:memory: → in-memory backend with warning."""
    with pytest.warns(RuntimeWarning):
        backend = make_sqlite_profile_backend_from_url("sqlite:///:memory:")
    assert isinstance(backend, SQLiteAgentProfileBackend)


def test_url_wrong_scheme_raises():
    with pytest.raises(ValueError, match="scheme"):
        make_sqlite_profile_backend_from_url("postgres:///profiles.db")


def test_url_with_netloc_raises():
    """sqlite://host/path is ambiguous — refuse the two-slash typo."""
    with pytest.raises(ValueError, match="netloc"):
        make_sqlite_profile_backend_from_url("sqlite://somehost/profiles.db")


def test_url_empty_path_raises():
    with pytest.raises(ValueError, match="empty path"):
        make_sqlite_profile_backend_from_url("sqlite:///")


# ─── Registry resolution + env-var dispatch ──────────────────────────


def test_sqlite_registered_at_import():
    """sqlite is in the global registry at module import time."""
    backends = list_profile_backends()
    assert "sqlite" in backends
    assert "filesystem" in backends


def test_get_profile_backend_returns_sqlite_class():
    assert get_profile_backend("sqlite") is SQLiteAgentProfileBackend


def test_get_default_profile_backend_env_dispatch_sqlite_default_path(
    tmp_path, monkeypatch
):
    """ATOMIC_AGENTS_PROFILE_BACKEND=sqlite with no URL → default path under scope."""
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", "sqlite")
    monkeypatch.delenv("ATOMIC_AGENTS_PROFILE_BACKEND_URL", raising=False)
    backend = get_default_profile_backend(tmp_path)
    assert isinstance(backend, SQLiteAgentProfileBackend)
    # Default db lands at <scope_root>/.profile.db.
    backend.save_profile("scout", _make_profile("scout"))
    assert (tmp_path / ".profile.db").exists()


def test_get_default_profile_backend_env_dispatch_sqlite_with_url(
    tmp_path, monkeypatch
):
    """ATOMIC_AGENTS_PROFILE_BACKEND=sqlite + URL → backend at that path."""
    db_path = tmp_path / "custom.db"
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", "sqlite")
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND_URL", f"sqlite:///{db_path}")
    backend = get_default_profile_backend(tmp_path)
    assert isinstance(backend, SQLiteAgentProfileBackend)
    backend.save_profile("scout", _make_profile("scout"))
    assert db_path.exists()


def test_get_default_profile_backend_filesystem_default(tmp_path, monkeypatch):
    """No env var → filesystem backend."""
    monkeypatch.delenv("ATOMIC_AGENTS_PROFILE_BACKEND", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_PROFILE_BACKEND_URL", raising=False)
    from atomic_agents.profile import FilesystemAgentProfileBackend

    backend = get_default_profile_backend(tmp_path)
    assert isinstance(backend, FilesystemAgentProfileBackend)


def test_get_default_profile_backend_unknown_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", "definitely-not-real")
    with pytest.raises(BackendNotRegistered, match="definitely-not-real"):
        get_default_profile_backend(tmp_path)


def test_get_default_profile_backend_credential_redacted(tmp_path, monkeypatch):
    """If operator pastes a URL into the BACKEND env, secrets are redacted."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_PROFILE_BACKEND",
        "postgres://user:supersecret@host/db",
    )
    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_profile_backend(tmp_path)
    msg = str(exc_info.value)
    assert "supersecret" not in msg
    assert "postgres://..." in msg


# ─── Parent dir creation ─────────────────────────────────────────────


def test_constructor_creates_parent_dir(tmp_path):
    """Backend creates the parent dir on first connection if it doesn't exist."""
    nested = tmp_path / "deeply" / "nested" / "path"
    db_path = nested / "profiles.db"
    backend = SQLiteAgentProfileBackend(db_path)
    backend.save_profile("scout", _make_profile("scout"))
    assert db_path.exists()
    assert nested.exists()


# ─── P1-1 regression: no "dropped" warning on already-empty persona fields ───


def test_save_profile_no_warning_when_persona_fields_already_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P1-1 regression: save_profile must NOT emit agent_profile_save_dropped_persona_fields
    when the profile's persona fields are already empty (the normal post-bootstrap shape
    for an externally-owned agent).

    The warning and field-zeroing guard must only fire when at least one of
    persona_identity, persona_soul, or persona_user is non-empty.
    Additionally, the persona_id column must survive the save unchanged.
    """
    import logging

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        backend = SQLiteAgentProfileBackend(":memory:")

    # Build a profile whose persona fields are already empty (post-bootstrap shape).
    empty_persona_profile = AgentProfile.from_dict(
        {
            "name": "quiet-agent",
            "agent_mode": "reactive",
            "persona_identity": "",  # already empty
            "persona_soul": "",  # already empty
            "persona_user": "",  # already empty
            "goal_text": "",
            "model_md_raw": _MODEL,
            "tools_md_raw": _TOOLS,
            "judges_md_raw": None,
            "roster_md_raw": _ROSTER,
            "mcp_md_raw": "",
            "model_config": {},
            "tool_config": {},
            "tool_classifications": {},
            "judges_config": None,
            "roster": [],
            "mcp_servers": [],
        }
    )

    # Save normally first (persona_id NULL at this point).
    backend.save_profile("quiet-agent", empty_persona_profile)

    # Mark as externally owned.
    backend.set_persona_ownership("quiet-agent", "shared-persona-x")

    # Now save again with the already-empty persona fields — no warning should fire.
    with caplog.at_level(logging.WARNING, logger="atomic_agents.profile.sqlite"):
        backend.save_profile("quiet-agent", empty_persona_profile)

    drop_events = [
        r
        for r in caplog.records
        if "agent_profile_save_dropped_persona_fields" in r.message
    ]
    assert len(drop_events) == 0, (
        "save_profile must not emit 'agent_profile_save_dropped_persona_fields' "
        f"when persona fields are already empty; got {len(drop_events)} event(s)"
    )

    # persona_id column must survive the save unchanged.
    assert backend.external_persona_ref("quiet-agent") == "shared-persona-x"

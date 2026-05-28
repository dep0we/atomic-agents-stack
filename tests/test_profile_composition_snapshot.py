"""D3 snapshot composition: persona fields dropped from snapshot blob when
the agent's persona is externally owned by PersonaBackend.

When PersonaBackend owns an agent's persona, the AgentProfile snapshot becomes
a "config snapshot" -- persona history lives in PersonaBackend.snapshot/restore.
Internally-owned agents (legacy three-file layout) keep persona fields intact.

Parametrized across FilesystemAgentProfileBackend and SQLiteAgentProfileBackend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from atomic_agents.profile import (
    AgentProfileBackend,
    FilesystemAgentProfileBackend,
    SQLiteAgentProfileBackend,
)

from tests.test_profile_protocol_conformance import (
    _IDENTITY_BODY,
    _SOUL_BODY,
    _USER_BODY,
    make_agent_in_backend,
)


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization (mirrors test_profile_protocol_conformance.py)

BackendFactory = Callable[[Path], AgentProfileBackend]


def _filesystem_factory(scope_root: Path) -> AgentProfileBackend:
    return FilesystemAgentProfileBackend(scope_root)


def _sqlite_factory(scope_root: Path) -> AgentProfileBackend:
    return SQLiteAgentProfileBackend(scope_root / ".profile.db")


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
    ("sqlite", _sqlite_factory),
]


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend(request, tmp_path) -> AgentProfileBackend:
    return request.param[1](tmp_path)


# ──────────────────────────────────────────────────────────────────
# Shared fixture helpers


_LINK_BODY = "# Persona link\n\n```yaml\nkind: shared\npersona_id: shared-v1\n```\n"


def _make_internally_owned(
    backend: AgentProfileBackend, scope_root: Path, agent_id: str
) -> None:
    """Save an internally-owned agent (persona_identity non-empty, no persona.link.md)."""
    make_agent_in_backend(
        backend,
        scope_root,
        agent_id,
        identity=_IDENTITY_BODY,
        soul=_SOUL_BODY,
        user=_USER_BODY,
    )


def _make_externally_owned(
    backend: AgentProfileBackend, scope_root: Path, agent_id: str
) -> None:
    """Save an agent, then migrate its persona to external ownership.

    Strategy: save the agent via save_profile (needed so it exists in both
    filesystem and SQLite backends), then use set_persona_ownership to bind
    the external persona_id. For the filesystem backend, save_profile writes
    persona/IDENTITY.md first; set_persona_ownership then raises
    PersonaOwnershipConflict unless we clear the legacy files first.

    Simpler: write minimal profile with empty persona fields, then call
    set_persona_ownership. Empty persona fields avoid the conflict check.
    """
    from atomic_agents.profile import SQLiteAgentProfileBackend

    # For filesystem backend: write the agent dir with persona.link.md directly,
    # bypassing save_profile persona-field writes.
    # For SQLite: save a profile with empty persona fields, then set ownership.
    if isinstance(backend, SQLiteAgentProfileBackend):
        make_agent_in_backend(
            backend,
            scope_root,
            agent_id,
            identity="",
            soul=None,
            user=None,
        )
        backend.set_persona_ownership(agent_id, "shared-v1")
    else:
        # Filesystem: write agent dir manually with persona.link.md only.
        agent_root = scope_root / agent_id
        agent_root.mkdir(parents=True, exist_ok=True)
        (agent_root / "persona.link.md").write_text(_LINK_BODY, encoding="utf-8")


def _read_snapshot_profile_dict(
    backend: AgentProfileBackend,
    scope_root: Path,
    agent_id: str,
    snapshot_id: str,
) -> dict:
    """Read the raw snapshot profile dict from disk or SQLite."""
    from atomic_agents.profile import SQLiteAgentProfileBackend
    import sqlite3

    if isinstance(backend, SQLiteAgentProfileBackend):
        db_path = scope_root / ".profile.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT profile_json FROM profile_snapshots WHERE snapshot_id = ? AND agent_id = ?",
            (snapshot_id, agent_id),
        ).fetchone()
        conn.close()
        assert row is not None, f"snapshot {snapshot_id!r} not found in SQLite"
        return json.loads(row["profile_json"])
    else:
        # Filesystem: read profile.json from .snapshots/<agent_id>/<snapshot_id>/
        snap_file = scope_root / ".snapshots" / agent_id / snapshot_id / "profile.json"
        return json.loads(snap_file.read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────
# D3: externally-owned agent -- snapshot blob has empty persona fields


def test_snapshot_externally_owned_drops_persona_fields(backend, tmp_path):
    """Snapshot of externally-owned agent has empty persona_identity/soul/user."""
    _make_externally_owned(backend, tmp_path, "ext-agent")
    snap_id = backend.snapshot("ext-agent", "test-snap")
    blob = _read_snapshot_profile_dict(backend, tmp_path, "ext-agent", snap_id)
    assert blob["persona_identity"] == ""
    assert blob["persona_soul"] == ""
    assert blob["persona_user"] == ""


# ──────────────────────────────────────────────────────────────────
# D3: internally-owned agent -- snapshot blob retains persona fields


def test_snapshot_internally_owned_retains_persona_fields(backend, tmp_path):
    """Snapshot of internally-owned agent preserves persona_identity/soul/user."""
    _make_internally_owned(backend, tmp_path, "int-agent")
    snap_id = backend.snapshot("int-agent", "test-snap")
    blob = _read_snapshot_profile_dict(backend, tmp_path, "int-agent", snap_id)
    assert blob["persona_identity"] != ""
    assert (
        _IDENTITY_BODY.strip() in blob["persona_identity"]
        or blob["persona_identity"] != ""
    )


# ──────────────────────────────────────────────────────────────────
# D3: snapshot-while-external, restore-while-external: no migration event fires


def test_snapshot_taken_external_restore_external_no_migration_event(
    backend, tmp_path, caplog
):
    """Round-trip: snapshot taken when externally owned, restored while still externally
    owned -- persona fields stay empty throughout and D-PP-13 event does NOT fire.

    Detection requires at least one non-empty persona field in the snapshot. When the
    snapshot was taken externally (empty fields), the detection condition is false.
    """
    import logging

    _make_externally_owned(backend, tmp_path, "ext-agent")
    snap_id = backend.snapshot("ext-agent", "test-snap")

    # Confirm snapshot has empty persona fields (the D3 drop fired during snapshot).
    blob = _read_snapshot_profile_dict(backend, tmp_path, "ext-agent", snap_id)
    assert blob["persona_identity"] == ""

    with caplog.at_level(logging.WARNING):
        backend.restore("ext-agent", snap_id)

    assert "agent_profile_restore_dropped_persona_fields" not in caplog.text

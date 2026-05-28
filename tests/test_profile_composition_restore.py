"""D-PP-13 migration-window event + D3 cross-window restore tests.

Covers the case where an AgentProfile snapshot was taken before the agent's
persona was migrated to PersonaBackend (snapshot has non-empty persona fields),
and the restore target is currently externally owned. In this case:

- The migration-window warning fires once per (agent_id, snapshot_id) pair.
- The persona fields are dropped from the restored profile.
- Subsequent restores of the same pair do NOT re-emit the warning (dedup).
- All-empty persona fields in the snapshot suppresses the event.

Parametrized across FilesystemAgentProfileBackend and SQLiteAgentProfileBackend.
"""

from __future__ import annotations

import json
import logging
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
# Backend factory parametrization

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
# Helpers

_LINK_BODY = "# Persona link\n\n```yaml\nkind: shared\npersona_id: shared-v1\n```\n"


def _make_internally_owned(
    backend: AgentProfileBackend, scope_root: Path, agent_id: str
) -> None:
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
    """Create an externally-owned agent (persona.link.md / persona_id column set)."""
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
        agent_root = scope_root / agent_id
        agent_root.mkdir(parents=True, exist_ok=True)
        (agent_root / "persona.link.md").write_text(_LINK_BODY, encoding="utf-8")


def _inject_snapshot_with_persona(
    backend: AgentProfileBackend,
    scope_root: Path,
    agent_id: str,
    snapshot_id: str,
    *,
    persona_identity: str = _IDENTITY_BODY,
    persona_soul: str = _SOUL_BODY,
    persona_user: str = _USER_BODY,
) -> None:
    """Write a snapshot blob that contains non-empty persona fields directly.

    Used to simulate a "pre-migration" snapshot without going through
    backend.snapshot() (which would strip persona fields for externally-owned agents).
    """
    from atomic_agents.profile import SQLiteAgentProfileBackend
    import sqlite3
    from datetime import datetime

    profile_dict = {
        "name": agent_id,
        "agent_mode": "reactive",
        "persona_identity": persona_identity,
        "persona_soul": persona_soul,
        "persona_user": persona_user,
        "goal_text": "",
        "model_md_raw": "",
        "tools_md_raw": "",
        "judges_md_raw": None,
        "roster_md_raw": "",
        "mcp_md_raw": "",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [],
    }

    if isinstance(backend, SQLiteAgentProfileBackend):
        db_path = scope_root / ".profile.db"
        conn = sqlite3.connect(str(db_path))
        created_at = datetime.now().astimezone().isoformat()
        conn.execute(
            "INSERT INTO profile_snapshots "
            "(snapshot_id, agent_id, label, created_at, profile_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                snapshot_id,
                agent_id,
                "pre-migration",
                created_at,
                json.dumps(profile_dict),
            ),
        )
        conn.commit()
        conn.close()
    else:
        snap_dir = scope_root / ".snapshots" / agent_id / snapshot_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "profile.json").write_text(
            json.dumps(profile_dict, indent=2), encoding="utf-8"
        )
        metadata = {
            "snapshot_id": snapshot_id,
            "label": "pre-migration",
            "created_at": datetime.now().astimezone().isoformat(),
            "agent_id": agent_id,
        }
        (snap_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )


def _inject_snapshot_all_empty_persona(
    backend: AgentProfileBackend,
    scope_root: Path,
    agent_id: str,
    snapshot_id: str,
) -> None:
    """Write a snapshot blob where all persona fields are empty strings."""
    _inject_snapshot_with_persona(
        backend,
        scope_root,
        agent_id,
        snapshot_id,
        persona_identity="",
        persona_soul="",
        persona_user="",
    )


# ──────────────────────────────────────────────────────────────────
# D-PP-13: migration event fires when snapshot has non-empty persona_identity


def test_migration_event_fires_for_non_empty_persona_identity(
    backend, tmp_path, caplog
):
    """Snapshot with non-empty persona_identity + externally-owned target -> event fires."""
    _make_externally_owned(backend, tmp_path, "agent-a")
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-a",
        "snap_identity_test",
        persona_identity=_IDENTITY_BODY,
        persona_soul="",
        persona_user="",
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-a", "snap_identity_test")
    assert "agent_profile_restore_dropped_persona_fields" in caplog.text


# ──────────────────────────────────────────────────────────────────
# D-PP-13: migration event fires when snapshot has non-empty persona_soul


def test_migration_event_fires_for_non_empty_persona_soul(backend, tmp_path, caplog):
    """Snapshot with non-empty persona_soul + externally-owned target -> event fires."""
    _make_externally_owned(backend, tmp_path, "agent-b")
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-b",
        "snap_soul_test",
        persona_identity="",
        persona_soul=_SOUL_BODY,
        persona_user="",
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-b", "snap_soul_test")
    assert "agent_profile_restore_dropped_persona_fields" in caplog.text


# ──────────────────────────────────────────────────────────────────
# D-PP-13: migration event fires when snapshot has non-empty persona_user


def test_migration_event_fires_for_non_empty_persona_user(backend, tmp_path, caplog):
    """Snapshot with non-empty persona_user + externally-owned target -> event fires."""
    _make_externally_owned(backend, tmp_path, "agent-c")
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-c",
        "snap_user_test",
        persona_identity="",
        persona_soul="",
        persona_user=_USER_BODY,
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-c", "snap_user_test")
    assert "agent_profile_restore_dropped_persona_fields" in caplog.text


# ──────────────────────────────────────────────────────────────────
# D-PP-13: same (agent_id, snapshot_id) restored twice -> event fires ONCE


def test_migration_event_dedup_same_pair_fires_once(backend, tmp_path, caplog):
    """Restoring the same (agent_id, snapshot_id) twice emits the event only once."""
    _make_externally_owned(backend, tmp_path, "agent-d")
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-d",
        "snap_dedup_test",
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-d", "snap_dedup_test")
        backend.restore("agent-d", "snap_dedup_test")
    count = caplog.text.count("agent_profile_restore_dropped_persona_fields")
    assert count == 1, f"expected 1 emission, got {count}"


# ──────────────────────────────────────────────────────────────────
# D-PP-13: two different snapshots -> event fires twice (different snapshot_ids)


def test_migration_event_fires_for_each_unique_snapshot_id(backend, tmp_path, caplog):
    """Two different snapshot_ids for the same agent both trigger the event."""
    _make_externally_owned(backend, tmp_path, "agent-e")
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-e",
        "snap_first",
    )
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-e",
        "snap_second",
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-e", "snap_first")
        backend.restore("agent-e", "snap_second")
    count = caplog.text.count("agent_profile_restore_dropped_persona_fields")
    assert count == 2, f"expected 2 emissions (one per snapshot), got {count}"


# ──────────────────────────────────────────────────────────────────
# D-PP-13: two different agents -- dedup key includes agent_id so both fire


def test_migration_event_tuple_key_disambiguates_agent_id(backend, tmp_path, caplog):
    """Two different agents with migration-window snapshots each emit their own event.

    The dedup key is (agent_id, snapshot_id). When two different agents are
    restored from snapshots with persona fields, both events fire -- agent_id
    is part of the key so one agent's dedup set entry does not suppress the
    other agent's event.

    Note on snapshot_id uniqueness: the SQLite backend enforces a global PRIMARY
    KEY on snapshot_id (by schema design -- snapshot rows are globally unique
    within the db). For SQLite, two agents cannot share the same snapshot_id
    value. The tuple-key shape is tested by using distinct snapshot_ids here;
    for the filesystem backend, the same snapshot_id could appear in two
    different agent directories, but testing distinct ids still proves the
    agent_id dimension of the key works correctly on both backends.
    """
    _make_externally_owned(backend, tmp_path, "agent-f")
    _make_externally_owned(backend, tmp_path, "agent-g")
    # Use distinct snapshot_ids so both filesystem and SQLite backends accept them.
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-f",
        "snap_agent_f",
    )
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-g",
        "snap_agent_g",
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-f", "snap_agent_f")
        backend.restore("agent-g", "snap_agent_g")
    count = caplog.text.count("agent_profile_restore_dropped_persona_fields")
    assert count == 2, f"expected 2 emissions (one per agent), got {count}"


# ──────────────────────────────────────────────────────────────────
# D-PP-13: channel check -- caplog captures at WARNING with field names


def test_migration_event_channel_and_field_names(backend, tmp_path, caplog):
    """The event is emitted at WARNING level and names the three dropped fields."""
    _make_externally_owned(backend, tmp_path, "agent-h")
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "agent-h",
        "snap_channel_test",
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-h", "snap_channel_test")

    warning_records = [
        r
        for r in caplog.records
        if "agent_profile_restore_dropped_persona_fields" in r.message
    ]
    assert len(warning_records) == 1
    msg = warning_records[0].message
    assert "persona_identity" in msg
    assert "persona_soul" in msg
    assert "persona_user" in msg
    assert warning_records[0].levelno == logging.WARNING


# ──────────────────────────────────────────────────────────────────
# D3 cross-window restore: snapshot internally, restore internally -> persona round-trips


def test_cross_window_internal_to_internal_persona_round_trips(backend, tmp_path):
    """Legacy behavior: internally-owned snapshot restored to internally-owned agent
    preserves persona fields.
    """
    _make_internally_owned(backend, tmp_path, "int-agent")
    snap_id = backend.snapshot("int-agent", "pre-migration")
    backend.restore("int-agent", snap_id)
    profile = backend.load_profile("int-agent")
    assert profile.persona_identity != ""


# ──────────────────────────────────────────────────────────────────
# D3 cross-window restore: snapshot externally, restore externally -> fields empty


def test_cross_window_external_to_external_persona_stays_empty(backend, tmp_path):
    """Externally-owned snapshot restored to externally-owned agent: persona fields
    remain empty throughout -- no migration event, no persona data leaking.
    """
    _make_externally_owned(backend, tmp_path, "ext-agent")
    snap_id = backend.snapshot("ext-agent", "config-snap")
    # The snapshot itself has empty persona fields (D3 drop on snapshot).
    backend.restore("ext-agent", snap_id)
    # After restore, external ownership still applies; load_profile gives empty
    # persona fields (the backend stores empty and external owner supplies them
    # via the framework bootstrap path -- that path is not exercised here).
    profile = backend.load_profile("ext-agent")
    assert profile.persona_identity == ""


# ──────────────────────────────────────────────────────────────────
# P2 threading: D-PP-13 dedup is safe under concurrent same-pair restore


def test_migration_event_threading_safe_under_concurrent_same_pair_restore(
    tmp_path, caplog
):
    """10 threads concurrently calling restore() on the same (agent, snapshot) pair
    emit exactly one D-PP-13 warning -- not one per thread.

    Uses a threading.Barrier to align thread starts so the race window is reliably
    exercised. Tests the filesystem backend only; SQLite :memory: mode is
    intentionally single-threaded (check_same_thread=True), so this test does
    not parametrize across both backends.
    """
    import threading

    backend = FilesystemAgentProfileBackend(tmp_path)
    _make_externally_owned(backend, tmp_path, "thr-agent")
    _inject_snapshot_with_persona(
        backend,
        tmp_path,
        "thr-agent",
        "snap_threading_test",
    )

    n_threads = 10
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def _restore_one() -> None:
        try:
            barrier.wait()
            backend.restore("thr-agent", "snap_threading_test")
        except Exception as exc:
            errors.append(exc)

    with caplog.at_level(logging.WARNING):
        threads = [threading.Thread(target=_restore_one) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"Threads raised exceptions: {errors}"
    count = caplog.text.count("agent_profile_restore_dropped_persona_fields")
    assert count == 1, (
        f"Expected exactly 1 D-PP-13 warning emission across {n_threads} concurrent "
        f"restores of the same pair; got {count}"
    )


# ──────────────────────────────────────────────────────────────────
# D3 cross-window restore: snapshot externally, restore internally -> fields empty


def test_cross_window_external_snapshot_restored_internally_stays_empty(
    backend, tmp_path
):
    """Snapshot taken while externally owned has empty persona fields. When restored
    to an internally-owned agent (no legacy layout files on disk), persona_identity
    stays empty -- there is no legacy layout to pull from.
    """
    _make_externally_owned(backend, tmp_path, "ext-then-int")
    snap_id = backend.snapshot("ext-then-int", "external-snap")

    # Revert to internal ownership: clear the persona.link.md / persona_id.
    # For filesystem: remove persona.link.md.
    # For SQLite: set persona_id back to None.
    if isinstance(backend, SQLiteAgentProfileBackend):
        backend.set_persona_ownership("ext-then-int", None)
    else:
        link = backend.scope_root / "ext-then-int" / "persona.link.md"
        if link.exists():
            link.unlink()
        # The agent no longer has any sentinel; make it internally owned
        # by writing persona/IDENTITY.md.
        persona_dir = backend.scope_root / "ext-then-int" / "persona"
        persona_dir.mkdir(parents=True, exist_ok=True)
        (persona_dir / "IDENTITY.md").write_text(_IDENTITY_BODY, encoding="utf-8")

    backend.restore("ext-then-int", snap_id)
    profile = backend.load_profile("ext-then-int")
    # The snapshot had empty persona fields (taken while external); restoring
    # it writes empty fields. For filesystem, save_profile writes persona/IDENTITY.md
    # with empty content (or removes it when empty, per backend behavior).
    # Either way, the restored content is empty -- the snapshot had no persona data.
    assert profile.persona_identity == ""


# ──────────────────────────────────────────────────────────────────
# D3 cross-window restore: snapshot internally, restore externally -> D-PP-13 fires


def test_cross_window_internal_snapshot_restored_externally_fires_event(
    backend, tmp_path, caplog
):
    """The canonical D-PP-13 migration case: snapshot taken pre-PersonaBackend
    (internally owned, persona fields present), later restored onto an externally-
    owned agent. The event fires and persona fields are dropped.
    """
    _make_internally_owned(backend, tmp_path, "migrating-agent")
    snap_id = backend.snapshot("migrating-agent", "pre-migration")

    # Now migrate the agent to external persona ownership.
    if isinstance(backend, SQLiteAgentProfileBackend):
        # Save profile with empty persona to avoid conflict, then set ownership.

        existing = backend.load_profile("migrating-agent")
        backend.save_profile(
            "migrating-agent",
            existing.replace(persona_identity="", persona_soul="", persona_user=""),
        )
        backend.set_persona_ownership("migrating-agent", "shared-v1")
    else:
        # Filesystem: remove persona dir files, then write persona.link.md.
        agent_root = backend.scope_root / "migrating-agent"
        for fname in ["persona/IDENTITY.md", "persona/SOUL.md", "persona/USER.md"]:
            p = agent_root / fname
            if p.exists():
                p.unlink()
        (agent_root / "persona.link.md").write_text(_LINK_BODY, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        backend.restore("migrating-agent", snap_id)

    assert "agent_profile_restore_dropped_persona_fields" in caplog.text
    profile = backend.load_profile("migrating-agent")
    assert profile.persona_identity == ""


# ──────────────────────────────────────────────────────────────────
# D-PP-13: all-empty persona fields in snapshot -> NO event fires


def test_all_empty_persona_in_snapshot_suppresses_event(backend, tmp_path, caplog):
    """When the snapshot has all-empty persona fields and the target is externally
    owned, the detection condition (at least one non-empty field) is false.
    The event must NOT fire.
    """
    _make_externally_owned(backend, tmp_path, "agent-no-event")
    _inject_snapshot_all_empty_persona(
        backend, tmp_path, "agent-no-event", "snap_empty_persona"
    )
    with caplog.at_level(logging.WARNING):
        backend.restore("agent-no-event", "snap_empty_persona")
    assert "agent_profile_restore_dropped_persona_fields" not in caplog.text

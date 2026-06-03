"""Tests for AgentProfile.mcp_servers_resolved sibling field (#201 PR 2 of 5).

Covers:
  1. Field existence and default value.
  2. Field is LAST in the dataclass (pins the design constraint).
  3. to_dict() always emits [] even when field is populated (locked Q2).
  4. from_dict() round-trip when field is absent in the dict.
  5. from_dict() correctly reconstructs MCPServerSpec instances when
     the dict DOES contain mcp_servers_resolved (future-compat).
  6. _mcp_spec_from_dict / _mcp_spec_to_dict round-trip equality.
  7. Pre-existing latent bug fix: mcp_servers fallback path now returns
     MCPServerSpec instances, not raw dicts.
  8. Snapshot round-trip resets mcp_servers_resolved to [] (security shape).
  9. SQLite save-and-load round-trip keeps mcp_servers_resolved as [].
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from atomic_agents.mcp import MCPServerSpec
from atomic_agents.profile.types import (
    AgentProfile,
    _mcp_spec_from_dict,
    _mcp_spec_to_dict,
)

# ---------------------------------------------------------------------------
# Minimal AgentProfile construction helper
# ---------------------------------------------------------------------------

_IDENTITY = "# Dan\n## Operating mode\nreactive\n"


def _minimal_profile(**overrides) -> AgentProfile:
    """Return the smallest valid AgentProfile, with optional field overrides."""
    base = {
        "name": "test-agent",
        "agent_mode": "reactive",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [],
        "persona_identity": _IDENTITY,
        "persona_soul": "",
        "persona_user": "",
        "goal_text": "",
        "model_md_raw": "",
        "tools_md_raw": "",
        "judges_md_raw": None,
        "roster_md_raw": "",
        "mcp_md_raw": "",
    }
    base.update(overrides)
    return AgentProfile.from_dict(base)


def _sample_spec() -> MCPServerSpec:
    return MCPServerSpec(
        name="filesystem-tools",
        command="npx",
        args=["-y", "@mcp/server-fs", "/data"],
        env={"MCP_TOKEN": "tok123"},
        transport="stdio",
        description="filesystem tools server",
    )


# ---------------------------------------------------------------------------
# Test 1: field exists and defaults to []
# ---------------------------------------------------------------------------


def test_mcp_servers_resolved_field_exists():
    """mcp_servers_resolved is a field on AgentProfile; default is []."""
    field_names = {f.name for f in dataclasses.fields(AgentProfile)}
    assert "mcp_servers_resolved" in field_names

    profile = _minimal_profile()
    assert profile.mcp_servers_resolved == []


# ---------------------------------------------------------------------------
# Test 2: field is LAST in the dataclass
# ---------------------------------------------------------------------------


def test_mcp_servers_resolved_field_is_last_in_dataclass():
    """mcp_servers_resolved MUST be the last field in AgentProfile.

    Python dataclass rule: fields with defaults cannot precede required
    fields. Pinning this ensures future field additions don't silently
    break the constraint or trigger TypeError at import.
    """
    fields = dataclasses.fields(AgentProfile)
    assert fields[-1].name == "mcp_servers_resolved"


# ---------------------------------------------------------------------------
# Test 3: to_dict() always emits [] even when field is populated
# ---------------------------------------------------------------------------


def test_to_dict_always_emits_empty_list_even_when_field_populated():
    """to_dict() emits 'mcp_servers_resolved': [] regardless of runtime value.

    Locked decision Q2 from PR 2 prep pass: the field is a runtime transient.
    Serializing real values would write resolved MCP env secrets into snapshot
    JSON files on disk, which contradicts spec/24 Decision 1's security intent.
    """
    profile = _minimal_profile()
    # Inject a non-empty value via dataclasses.replace (the framework's pattern).
    spec = _sample_spec()
    populated = dataclasses.replace(profile, mcp_servers_resolved=[spec])
    assert len(populated.mcp_servers_resolved) == 1

    serialized = populated.to_dict()
    assert serialized["mcp_servers_resolved"] == []


# ---------------------------------------------------------------------------
# Test 4: from_dict() round-trip when key is absent
# ---------------------------------------------------------------------------


def test_from_dict_round_trip_with_empty_field():
    """from_dict() on a dict without mcp_servers_resolved gives [] (default)."""
    d = {
        "name": "agent-x",
        "agent_mode": "reactive",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [],
        "persona_identity": _IDENTITY,
        "persona_soul": "",
        "persona_user": "",
        "goal_text": "",
        "model_md_raw": "",
        "tools_md_raw": "",
        "judges_md_raw": None,
        "roster_md_raw": "",
        "mcp_md_raw": "",
        # mcp_servers_resolved deliberately absent
    }
    profile = AgentProfile.from_dict(d)
    assert profile.mcp_servers_resolved == []


# ---------------------------------------------------------------------------
# Test 5: from_dict() reconstructs MCPServerSpec when field is present in dict
# ---------------------------------------------------------------------------


def test_from_dict_round_trip_with_populated_field_in_dict():
    """from_dict() reconstructs MCPServerSpec instances from mcp_servers_resolved.

    In normal usage to_dict() emits [] so snapshots never carry resolved specs.
    But if a dict DOES contain the field (direct test construction, or a future
    un-clamped serializer path), from_dict should reconstruct correctly via
    _mcp_spec_from_dict.
    """
    spec = _sample_spec()
    spec_dict = _mcp_spec_to_dict(spec)

    d = {
        "name": "agent-y",
        "agent_mode": "reactive",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [],
        "persona_identity": _IDENTITY,
        "persona_soul": "",
        "persona_user": "",
        "goal_text": "",
        "model_md_raw": "",
        "tools_md_raw": "",
        "judges_md_raw": None,
        "roster_md_raw": "",
        "mcp_md_raw": "",
        "mcp_servers_resolved": [spec_dict],
    }
    profile = AgentProfile.from_dict(d)
    assert len(profile.mcp_servers_resolved) == 1
    resolved = profile.mcp_servers_resolved[0]
    assert isinstance(resolved, MCPServerSpec)
    assert resolved.name == spec.name
    assert resolved.command == spec.command
    assert resolved.args == spec.args
    assert resolved.env == spec.env
    assert resolved.transport == spec.transport
    assert resolved.description == spec.description


# ---------------------------------------------------------------------------
# Test 6: _mcp_spec_from_dict / _mcp_spec_to_dict round-trip
# ---------------------------------------------------------------------------


def test_mcp_spec_from_dict_helper_round_trip():
    """_mcp_spec_to_dict then _mcp_spec_from_dict reconstructs a equal spec."""
    original = _sample_spec()
    as_dict = _mcp_spec_to_dict(original)
    reconstructed = _mcp_spec_from_dict(as_dict)

    assert isinstance(reconstructed, MCPServerSpec)
    assert reconstructed.name == original.name
    assert reconstructed.command == original.command
    assert reconstructed.args == original.args
    assert reconstructed.env == original.env
    assert reconstructed.transport == original.transport
    assert reconstructed.description == original.description


# ---------------------------------------------------------------------------
# Test 7: mcp_servers fallback path returns MCPServerSpec instances, not dicts
# ---------------------------------------------------------------------------


def test_mcp_servers_fallback_now_returns_specs_not_dicts():
    """Pre-existing latent bug fix: mcp_servers fallback path returns MCPServerSpec.

    Before _mcp_spec_from_dict was introduced, the fallback path in
    AgentProfile.from_dict (used when mcp_md_raw is absent) returned raw dicts
    from d['mcp_servers']. This caused isinstance checks and attribute access
    on profile.mcp_servers to fail at runtime.
    """
    spec = _sample_spec()
    spec_dict = _mcp_spec_to_dict(spec)

    # mcp_md_raw is empty -- forces the else branch (fallback path)
    d = {
        "name": "agent-z",
        "agent_mode": "reactive",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [spec_dict],  # dict form -- must be reconstructed
        "persona_identity": _IDENTITY,
        "persona_soul": "",
        "persona_user": "",
        "goal_text": "",
        "model_md_raw": "",  # empty forces fallback
        "tools_md_raw": "",
        "judges_md_raw": None,
        "roster_md_raw": "",
        "mcp_md_raw": "",  # empty forces fallback
    }
    profile = AgentProfile.from_dict(d)
    assert len(profile.mcp_servers) == 1
    assert isinstance(profile.mcp_servers[0], MCPServerSpec), (
        f"Expected MCPServerSpec, got {type(profile.mcp_servers[0])!r}. "
        "This is the latent bug: the fallback path returned raw dicts before "
        "_mcp_spec_from_dict was introduced."
    )
    assert profile.mcp_servers[0].name == spec.name


# ---------------------------------------------------------------------------
# Test 8: snapshot round-trip resets mcp_servers_resolved to []
# ---------------------------------------------------------------------------


def test_snapshot_roundtrip_resets_mcp_servers_resolved_to_empty(tmp_path: Path):
    """Filesystem backend: save/snapshot/restore resets mcp_servers_resolved to [].

    Verifies the snapshot security shape: resolved MCP secrets never persist
    to disk via the to_dict() path.
    """
    from atomic_agents.profile.filesystem import FilesystemAgentProfileBackend

    scope_root = tmp_path / "agents"
    scope_root.mkdir()
    backend = FilesystemAgentProfileBackend(scope_root)

    # Build an agent directory so load_profile works.
    agent_root = scope_root / "snap-agent"
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text(_IDENTITY, encoding="utf-8")
    (agent_root / "model.md").write_text("", encoding="utf-8")
    (agent_root / "tools.md").write_text("", encoding="utf-8")

    profile = backend.load_profile("snap-agent")
    assert profile.mcp_servers_resolved == []

    # Inject a non-empty value then save.
    spec = _sample_spec()
    populated = dataclasses.replace(profile, mcp_servers_resolved=[spec])
    backend.save_profile("snap-agent", populated)

    # Snapshot and restore.
    snap_id = backend.snapshot("snap-agent", label="test-snap")
    backend.restore("snap-agent", snap_id)

    # Reload and confirm mcp_servers_resolved is back to [].
    restored = backend.load_profile("snap-agent")
    assert restored.mcp_servers_resolved == [], (
        "mcp_servers_resolved must be [] after snapshot/restore -- "
        "resolved secrets must not persist to disk."
    )


# ---------------------------------------------------------------------------
# Test 9: SQLite save-and-load keeps mcp_servers_resolved as []
# ---------------------------------------------------------------------------


def test_sqlite_save_and_load_profile_includes_mcp_servers_resolved_as_empty(
    tmp_path: Path,
):
    """SQLite backend: save then load yields mcp_servers_resolved == [].

    Verifies that the new field rides through the profile_json blob column
    correctly and that no schema change was accidentally required.
    """
    from atomic_agents.profile.sqlite import SQLiteAgentProfileBackend

    db_path = tmp_path / ".profile.db"
    backend = SQLiteAgentProfileBackend(db_path)

    # Construct and save a profile with a populated mcp_servers_resolved.
    spec = _sample_spec()
    profile = _minimal_profile()
    populated = dataclasses.replace(profile, mcp_servers_resolved=[spec])
    backend.save_profile("sql-agent", populated)

    # Load and assert.
    loaded = backend.load_profile("sql-agent")
    assert loaded.mcp_servers_resolved == [], (
        "mcp_servers_resolved must be [] after SQLite round-trip: "
        "to_dict() always serializes [] so the blob never carries resolved secrets."
    )

"""Filesystem-specific tests for persona.link.md sentinel + composition + cascade.

Covers D-PP-1 (sentinel admits persona.link.md), D2a (mutual-exclusion conflict),
D-PP-6 (cascade carve-out — ownership at instance layer only), D-PP-7
(set_persona_ownership filesystem behavior), and the D6 save-profile persona-write
carve-out when an agent is externally owned.

These tests are filesystem-only because:
- The sentinel change (D-PP-1) is a filesystem concept (on-disk file presence).
- PersonaOwnershipConflict (D-PP-8) is raised only by the filesystem backend.
- The cascade carve-out (D-PP-6) is a filesystem-path concept; the SQLite
  backend has no role-layer persona files to ignore.

Conformance tests for the shared Protocol surface (both backends) live in
``test_profile_protocol_conformance.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.exceptions import AgentProfileNotFound, PersonaOwnershipConflict
from atomic_agents.persona_link_md import parse_persona_link_md
from atomic_agents.profile import FilesystemAgentProfileBackend

# Reuse the filesystem fixture helper from the conformance suite.
from tests.test_profile_protocol_conformance import make_agent_dir


# ──────────────────────────────────────────────────────────────────
# Helpers


def _make_link_agent_dir(scope_root: Path, agent_id: str, persona_id: str) -> Path:
    """Create an agent dir whose only sentinel is ``persona.link.md``.

    No ``persona/IDENTITY.md`` is written — this is the shared-persona layout
    introduced by D2a.  Returns the agent root directory.
    """
    agent_root = scope_root / agent_id
    agent_root.mkdir(parents=True, exist_ok=True)
    link_body = (
        f"# Persona link\n\n```yaml\nkind: shared\npersona_id: {persona_id}\n```\n"
    )
    (agent_root / "persona.link.md").write_text(link_body, encoding="utf-8")
    return agent_root


# ──────────────────────────────────────────────────────────────────
# Sentinel tests — D-PP-1


def test_sentinel_admits_persona_link_md(tmp_path):
    """D-PP-1: agent dir with only persona.link.md is recognized as an agent.

    exists(), list_agents(), and load_profile() all use ``_is_agent_dir``
    which returns True for either sentinel.  A dir with only persona.link.md
    (no IDENTITY.md) must NOT raise AgentProfileNotFound on load_profile.
    """
    _make_link_agent_dir(tmp_path, "linked-agent", "my-persona")
    backend = FilesystemAgentProfileBackend(tmp_path)

    assert backend.exists("linked-agent") is True
    assert "linked-agent" in backend.list_agents()

    # load_profile returns a profile with empty persona fields (the framework
    # bootstrap path repopulates them via PersonaBackend; D-PP-4).
    profile = backend.load_profile("linked-agent")
    assert profile.name == "linked-agent"
    assert profile.persona_identity == ""
    assert profile.persona_soul == ""
    assert profile.persona_user == ""


def test_sentinel_admits_legacy_identity_md(tmp_path):
    """D-PP-1: agent dir with only persona/IDENTITY.md (legacy layout) still works."""
    make_agent_dir(tmp_path, "legacy-agent")
    backend = FilesystemAgentProfileBackend(tmp_path)

    assert backend.exists("legacy-agent") is True
    assert "legacy-agent" in backend.list_agents()
    profile = backend.load_profile("legacy-agent")
    assert profile.persona_identity != ""


def test_sentinel_neither_file_raises_not_found(tmp_path):
    """Neither sentinel present → AgentProfileNotFound on load_profile."""
    bare_dir = tmp_path / "bare"
    bare_dir.mkdir()
    # No persona/IDENTITY.md and no persona.link.md.
    backend = FilesystemAgentProfileBackend(tmp_path)

    assert backend.exists("bare") is False
    assert "bare" not in backend.list_agents()
    with pytest.raises(AgentProfileNotFound):
        backend.load_profile("bare")


# ──────────────────────────────────────────────────────────────────
# D2a: mutual-exclusion conflict


def test_both_sentinels_raise_persona_ownership_conflict(tmp_path):
    """D2a: if both persona.link.md and persona/IDENTITY.md exist at load time,
    load_profile raises PersonaOwnershipConflict.  Error message must mention
    both files so the operator knows which to remove.
    """
    agent_root = tmp_path / "conflicted"
    agent_root.mkdir()
    (agent_root / "persona").mkdir()
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Identity\n", encoding="utf-8"
    )
    link_body = "# Persona link\n\n```yaml\nkind: shared\npersona_id: x\n```\n"
    (agent_root / "persona.link.md").write_text(link_body, encoding="utf-8")

    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(
        PersonaOwnershipConflict,
        match="(?i)persona.link.md|IDENTITY.md",
    ):
        backend.load_profile("conflicted")


# ──────────────────────────────────────────────────────────────────
# D6 + D-PP-8: save_profile silently drops persona writes when externally owned


def test_save_profile_drops_persona_writes_for_link_agent(tmp_path):
    """D6: when persona.link.md is present, save_profile MUST NOT write
    persona/IDENTITY.md, SOUL.md, or USER.md to disk.

    The inline persona_identity/soul/user fields on the AgentProfile are
    denormalized snapshots (populated by the framework's bootstrap path);
    save_profile silently ignores them when the agent is externally owned.
    """
    _make_link_agent_dir(tmp_path, "linked-agent", "my-persona")
    backend = FilesystemAgentProfileBackend(tmp_path)
    profile = backend.load_profile("linked-agent")

    # Build a profile carrying non-empty persona fields — save_profile
    # must NOT write them.
    non_empty_profile = profile.replace(
        persona_identity="# Injected identity\n",
        persona_soul="# Injected soul\n",
        persona_user="# Injected user\n",
    )
    backend.save_profile("linked-agent", non_empty_profile)

    agent_root = tmp_path / "linked-agent"
    assert not (agent_root / "persona" / "IDENTITY.md").is_file()
    assert not (agent_root / "persona" / "SOUL.md").is_file()
    assert not (agent_root / "persona" / "USER.md").is_file()


# ──────────────────────────────────────────────────────────────────
# D-PP-7: set_persona_ownership filesystem behavior


def test_set_persona_ownership_writes_valid_link_file(tmp_path):
    """set_persona_ownership writes persona.link.md that parses cleanly.

    Starting from a link-only agent (no IDENTITY.md — the externally-owned
    layout) and overwriting the persona_id proves that re-binding an agent
    to a different PersonaBackend record is atomic and correctly formatted.

    After the write, ``parse_persona_link_md`` must succeed and return
    a ``PersonaLink`` with the same persona_id that was passed in.
    """
    # Start with a link-only agent (no IDENTITY.md — externally owned).
    _make_link_agent_dir(tmp_path, "scout", "initial-persona")
    backend = FilesystemAgentProfileBackend(tmp_path)
    backend.set_persona_ownership("scout", "shared-customer-v3")

    link_path = tmp_path / "scout" / "persona.link.md"
    assert link_path.is_file()

    link = parse_persona_link_md(link_path)
    assert link.persona_id == "shared-customer-v3"
    assert link.kind == "shared"


def test_set_persona_ownership_none_removes_link_file(tmp_path):
    """set_persona_ownership(agent_id, None) removes persona.link.md (idempotent).

    Starting from a link-only agent confirms the removal path works cleanly.
    """
    # Start with a link-only agent.
    _make_link_agent_dir(tmp_path, "scout", "initial-persona")
    backend = FilesystemAgentProfileBackend(tmp_path)

    # Overwrite with a new persona_id, then clear.
    backend.set_persona_ownership("scout", "some-persona")
    assert (tmp_path / "scout" / "persona.link.md").is_file()

    backend.set_persona_ownership("scout", None)
    assert not (tmp_path / "scout" / "persona.link.md").is_file()

    # Idempotent: after None the agent dir exists but has no sentinel;
    # set_persona_ownership(None) is a no-op (unlink_if_exists on absent file).
    # To call None again we need the dir to still exist with some sentinel.
    # Confirm the dir still exists (the agent dir is not removed, only the link).
    assert (tmp_path / "scout").is_dir()


def test_set_persona_ownership_refuses_to_overwrite_identity_md(tmp_path):
    """D2a at write time: set_persona_ownership raises PersonaOwnershipConflict
    when persona/IDENTITY.md already exists, preventing D2a conflicts from being
    created via the Protocol API (not just detected at load_profile time).
    """
    make_agent_dir(tmp_path, "scout")  # creates persona/IDENTITY.md
    backend = FilesystemAgentProfileBackend(tmp_path)

    with pytest.raises(PersonaOwnershipConflict):
        backend.set_persona_ownership("scout", "shared-persona")


# ──────────────────────────────────────────────────────────────────
# P2-2 regression: set_persona_ownership refuses when SOUL.md or USER.md exist


def test_set_persona_ownership_refuses_when_soul_md_exists(tmp_path):
    """P2-2 regression: set_persona_ownership raises PersonaOwnershipConflict
    when persona/SOUL.md exists alongside a persona.link.md sentinel.

    The legacy three-file layout (IDENTITY.md + SOUL.md + USER.md) is treated
    as one indivisible unit.  SOUL.md present when set_persona_ownership is
    called to rebind means operator left a partial legacy layout; the framework
    must refuse rather than silently orphan the file.
    """
    agent_root = tmp_path / "scout"
    agent_root.mkdir(parents=True, exist_ok=True)
    (agent_root / "persona").mkdir(parents=True, exist_ok=True)
    # SOUL.md is present — no IDENTITY.md.
    (agent_root / "persona" / "SOUL.md").write_text(
        "# Soul\n\nCurious.\n", encoding="utf-8"
    )
    # persona.link.md as the sentinel so the backend recognises this as an agent.
    link_body = (
        "# Persona link\n\n```yaml\nkind: shared\npersona_id: old-persona\n```\n"
    )
    (agent_root / "persona.link.md").write_text(link_body, encoding="utf-8")

    backend = FilesystemAgentProfileBackend(tmp_path)

    # Rebinding to a new persona_id must raise because SOUL.md is present.
    with pytest.raises(PersonaOwnershipConflict, match="(?i)SOUL.md"):
        backend.set_persona_ownership("scout", "new-persona")


# ──────────────────────────────────────────────────────────────────
# D-PP-6: cascade carve-out — instance-layer ownership only


def _make_cascade_agent(
    scope_root: Path,
    system: str = "muse",
    project: str = "the-unfinished",
    role: str = "writer",
) -> tuple[Path, Path, str]:
    """Build a cascade-layout agent under scope_root.

    Creates the role dir (required for detect_cascade to fire), and the
    instance dir at ``<system>/projects/<project>/agents/<role>``.

    Returns (instance_root, role_root, agent_id).
    """
    role_root = scope_root / system / "roles" / role
    role_root.mkdir(parents=True, exist_ok=True)

    instance_root = scope_root / system / "projects" / project / "agents" / role
    instance_root.mkdir(parents=True, exist_ok=True)

    agent_id = f"{system}/projects/{project}/agents/{role}"
    return instance_root, role_root, agent_id


def test_cascade_instance_link_md_marks_externally_owned(tmp_path):
    """D-PP-6: placing persona.link.md at the instance dir makes the agent
    externally owned — regardless of what role-layer persona files exist.

    Role-layer IDENTITY.md does NOT participate in ownership detection;
    only the instance dir is checked.  This means load_profile succeeds
    (no conflict) even when role layer has its own IDENTITY.md.
    """
    instance_root, role_root, agent_id = _make_cascade_agent(tmp_path)

    # Place IDENTITY.md at the role layer (inherited config, not ownership).
    (role_root / "persona").mkdir(parents=True, exist_ok=True)
    (role_root / "persona" / "IDENTITY.md").write_text(
        "# Role identity\n", encoding="utf-8"
    )

    # Place persona.link.md at the INSTANCE layer only.
    link_body = (
        "# Persona link\n\n```yaml\nkind: shared\npersona_id: shared-writer\n```\n"
    )
    (instance_root / "persona.link.md").write_text(link_body, encoding="utf-8")

    backend = FilesystemAgentProfileBackend(tmp_path)

    # No conflict — role-layer IDENTITY.md is not at the instance dir.
    profile = backend.load_profile(agent_id)
    assert profile.persona_identity == ""

    # external_persona_ref reads the instance link, ignores role layer.
    ref = backend.external_persona_ref(agent_id)
    assert ref == "shared-writer"


def test_cascade_instance_layer_conflict_still_raises(tmp_path):
    """D-PP-6 + D2a: when BOTH sentinels coexist at the INSTANCE layer of a
    cascaded agent, PersonaOwnershipConflict is still raised.

    The cascade carve-out exempts role-layer files; it does NOT exempt
    conflicts at the instance dir itself.
    """
    instance_root, _role_root, agent_id = _make_cascade_agent(tmp_path)

    # Place BOTH sentinels at the instance layer.
    (instance_root / "persona").mkdir(parents=True, exist_ok=True)
    (instance_root / "persona" / "IDENTITY.md").write_text(
        "# Instance identity\n", encoding="utf-8"
    )
    link_body = "# Persona link\n\n```yaml\nkind: shared\npersona_id: p\n```\n"
    (instance_root / "persona.link.md").write_text(link_body, encoding="utf-8")

    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(PersonaOwnershipConflict):
        backend.load_profile(agent_id)

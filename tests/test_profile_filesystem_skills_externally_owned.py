"""D-PP-12 sentinel fix: list_skills + load_skill_body for externally-owned agents.

Prior to D-PP-12, ``list_skills`` and ``load_skill_body`` checked for
``persona/IDENTITY.md`` (the legacy three-file sentinel) instead of
``_is_agent_dir`` (which also admits ``persona.link.md``). This meant
externally-owned agents (persona owned by PersonaBackend, present on disk
only as ``persona.link.md``) raised ``AgentProfileNotFound`` from both methods.

This module tests the fix for the two missed sites. The fix mirrors the D-PP-1
repair applied in PR 2 to ``load_profile``, ``list_agents``, and ``exists``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.exceptions import AgentProfileNotFound, PersonaOwnershipConflict
from atomic_agents.profile import FilesystemAgentProfileBackend

from tests.test_profile_protocol_conformance import make_agent_dir


_SKILL_BODY = (
    "---\n"
    "name: data-cleaning\n"
    "description: Cleans messy data.\n"
    "---\n\n"
    "# Data Cleaning\n\nUse me to clean tabular data.\n"
)

_LINK_BODY = "# Persona link\n\n```yaml\nkind: shared\npersona_id: shared-v1\n```\n"


def _make_externally_owned_agent_with_skills(scope_root: Path, agent_id: str) -> Path:
    """Create an agent that has ONLY persona.link.md (externally owned) plus a skill."""
    agent_root = scope_root / agent_id
    agent_root.mkdir(parents=True, exist_ok=True)
    # Write persona.link.md — the only ownership sentinel present.
    (agent_root / "persona.link.md").write_text(_LINK_BODY, encoding="utf-8")
    # Write a skill directory.
    skill_dir = agent_root / "skills" / "data-cleaning"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_BODY, encoding="utf-8")
    return agent_root


# ──────────────────────────────────────────────────────────────────
# D-PP-12 fix — externally-owned agent: list_skills succeeds


def test_list_skills_externally_owned_agent_returns_non_empty(tmp_path):
    """list_skills must work for an agent with persona.link.md only (no IDENTITY.md).

    Before the D-PP-12 fix, this raised AgentProfileNotFound because the check
    tested for persona/IDENTITY.md only. The fix uses _is_agent_dir which admits
    persona.link.md as a valid ownership sentinel.
    """
    _make_externally_owned_agent_with_skills(tmp_path, "ext-agent")
    backend = FilesystemAgentProfileBackend(tmp_path)
    skills = backend.list_skills("ext-agent")
    assert len(skills) == 1
    assert skills[0].name == "data-cleaning"


# ──────────────────────────────────────────────────────────────────
# D-PP-12 fix — externally-owned agent: load_skill_body succeeds


def test_load_skill_body_externally_owned_agent_reads_body(tmp_path):
    """load_skill_body must work for an agent with persona.link.md only.

    Same pre-fix failure shape as list_skills: the sentinel check was too narrow.
    """
    _make_externally_owned_agent_with_skills(tmp_path, "ext-agent")
    backend = FilesystemAgentProfileBackend(tmp_path)
    body = backend.load_skill_body("ext-agent", "data-cleaning")
    assert "Data Cleaning" in body
    # Frontmatter is stripped by load_skill_body.
    assert "name: data-cleaning" not in body


# ──────────────────────────────────────────────────────────────────
# Non-existent agent: both methods still raise AgentProfileNotFound


def test_list_skills_missing_agent_raises(tmp_path):
    """Non-existent agent: list_skills still raises AgentProfileNotFound."""
    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(AgentProfileNotFound):
        backend.list_skills("no-such-agent")


def test_load_skill_body_missing_agent_raises(tmp_path):
    """Non-existent agent: load_skill_body still raises AgentProfileNotFound."""
    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(AgentProfileNotFound):
        backend.load_skill_body("no-such-agent", "data-cleaning")


# ──────────────────────────────────────────────────────────────────
# Conflict: both persona.link.md AND persona/IDENTITY.md present


def test_conflict_both_files_raises_before_list_skills(tmp_path):
    """When both persona.link.md AND persona/IDENTITY.md are present, load_profile
    raises PersonaOwnershipConflict. Verify this fires before list_skills is reachable
    so the conflict detection is not bypassed by the D-PP-12 sentinel fix.

    The agent IS visible to list_agents / exists (both sentinels admit it), but the
    downstream load_profile raises PersonaOwnershipConflict on any attempt to read
    the profile. This is the existing D2a enforcement preserved intact.
    """
    agent_root = tmp_path / "conflict-agent"
    # Write BOTH sentinels — the conflict state.
    make_agent_dir(tmp_path, "conflict-agent")  # writes persona/IDENTITY.md
    (agent_root / "persona.link.md").write_text(_LINK_BODY, encoding="utf-8")

    backend = FilesystemAgentProfileBackend(tmp_path)
    # load_profile detects the conflict and raises before list_skills could run.
    with pytest.raises(PersonaOwnershipConflict, match="mutually exclusive"):
        backend.load_profile("conflict-agent")

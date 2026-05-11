"""End-to-end agent integration tests for cascaded multi-agent projects.

Verifies that AtomicAgent properly:
- Detects a cascade layout from its agent_root
- Uses cascade-aware config resolution (role + instance overrides)
- Assembles system prompt in spec/06 cascade order
- Falls back cleanly to single-agent layout when no cascade
"""

from __future__ import annotations
from pathlib import Path

import pytest

from atomic_agents.agent import AtomicAgent


def _build_full_cascade_layout(
    tmp_path: Path,
    *,
    system: str = "muse",
    project: str = "the-unfinished",
    role: str = "writer",
) -> Path:
    """Build a fully populated cascade tree under tmp_path. Returns agents_root."""
    agents_root = tmp_path / "agents"
    system_root = agents_root / system

    # Layer 1: role
    role_dir = system_root / "roles" / role
    role_dir.mkdir(parents=True)
    (role_dir / "PROMPT.md").write_text(
        "You are a Muse Writer. Draft chapters in the project's voice."
    )
    (role_dir / "tools.md").write_text(
        "## Read paths\n- ~/docs/\n\n## Write paths\n- ~/projects/the-unfinished/drafts/\n"
    )
    (role_dir / "model.md").write_text(
        "## Default model\nclaude-sonnet-4-6-20260101\n\n"
        "Max input prompt tokens: 12,000\nMax output tokens: 4,000\n"
    )

    # Layer 2: project
    project_dir = system_root / "projects" / project
    project_dir.mkdir(parents=True)
    (project_dir / "canon.md").write_text("## World\nThe Unfinished is set in 1920s Vienna.")
    (project_dir / "style_guide.md").write_text("Use Oxford commas. Avoid em dashes.")
    (project_dir / "goal.md").write_text("Finish Act II by Q3 2026.")
    policy_dir = project_dir / "policy"
    policy_dir.mkdir()
    (policy_dir / "01_voice.md").write_text("Third person past, intimate.")
    (policy_dir / "02_pacing.md").write_text("End each chapter on motion or question.")

    # Layer 3: instance
    instance_dir = project_dir / "agents" / role
    persona_dir = instance_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text("# Identity\nWriter on The Unfinished.")
    (persona_dir / "SOUL.md").write_text("# Soul\nDrawn to characters in transition.")
    (persona_dir / "USER.md").write_text("# User\nThe operator owns the project vision.")
    (instance_dir / "memory").mkdir()
    (instance_dir / "wiki").mkdir()
    (instance_dir / "journal").mkdir()
    (instance_dir / "log").mkdir()

    return agents_root


def test_cascade_detected_in_init(tmp_path):
    agents_root = _build_full_cascade_layout(tmp_path)
    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    assert agent.cascade is not None
    assert agent.cascade.role_name == "writer"
    assert agent.cascade.project_name == "the-unfinished"


def test_cascade_config_uses_role_tools_md(tmp_path):
    """When cascade is detected, config.read_paths comes from the role's tools.md."""
    agents_root = _build_full_cascade_layout(tmp_path)
    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    # Role has read_paths (~/docs/) and write_paths (~/projects/the-unfinished/drafts/)
    assert any("docs" in str(p) for p in agent.config.read_paths)
    assert any("drafts" in str(p) for p in agent.config.write_paths)


def test_cascade_config_instance_tools_md_replaces_role(tmp_path):
    agents_root = _build_full_cascade_layout(tmp_path)
    instance = agents_root / "muse" / "projects" / "the-unfinished" / "agents" / "writer"
    (instance / "tools.md").write_text(
        "## Read paths\n- ~/instance-only/\n"
    )
    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    # Role's docs path replaced; only instance's read path remains
    assert all("docs" not in str(p) for p in agent.config.read_paths)
    assert any("instance-only" in str(p) for p in agent.config.read_paths)


def test_cascade_config_override_md_appends_to_role(tmp_path):
    agents_root = _build_full_cascade_layout(tmp_path)
    instance = agents_root / "muse" / "projects" / "the-unfinished" / "agents" / "writer"
    (instance / "tools.override.md").write_text(
        "## Hard NOs\n- never delete drafts\n"
    )
    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    # Role's read paths still present
    assert any("docs" in str(p) for p in agent.config.read_paths)
    # Instance's hard NO present
    assert "never delete drafts" in agent.config.hard_nos


def test_cascade_assembled_prompt_contains_all_layers(tmp_path):
    agents_root = _build_full_cascade_layout(tmp_path)
    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    agent.load()
    prompt = agent.assemble_system_prompt()

    # Layer 1: role PROMPT.md
    assert "You are a Muse Writer" in prompt
    # Layer 3: instance persona
    assert "Writer on The Unfinished" in prompt
    assert "Drawn to characters in transition" in prompt
    # tools.md (from role, since no instance override)
    assert "Read paths" in prompt
    # Layer 2: project assets
    assert "1920s Vienna" in prompt
    assert "Oxford commas" in prompt
    assert "Finish Act II" in prompt
    assert "Third person past" in prompt
    assert "End each chapter" in prompt


def test_cascade_assembled_prompt_order_matches_spec_06(tmp_path):
    agents_root = _build_full_cascade_layout(tmp_path)
    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    agent.load()
    prompt = agent.assemble_system_prompt()

    # Order: role PROMPT < persona < tools < canon < goal < style_guide < policy
    role_idx = prompt.index("You are a Muse Writer")
    persona_idx = prompt.index("Writer on The Unfinished")
    tools_idx = prompt.index("# tools.md")
    canon_idx = prompt.index("1920s Vienna")
    goal_idx = prompt.index("Finish Act II")
    style_idx = prompt.index("Oxford commas")
    policy_idx = prompt.index("Third person past")

    assert role_idx < persona_idx < tools_idx
    assert tools_idx < canon_idx < goal_idx < style_idx < policy_idx


def test_single_agent_layout_still_works_no_cascade(tmp_path):
    """Backwards compat: a plain single-agent folder behaves exactly as before."""
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "caldwell"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text("# Identity\nCaldwell.")
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-sonnet-4-6-20260101\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()

    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    assert agent.cascade is None
    agent.load()
    prompt = agent.assemble_system_prompt()

    assert "Caldwell" in prompt
    # No cascade-only sections leak in
    assert "# role PROMPT.md" not in prompt
    assert "# project canon.md" not in prompt
    assert "# project policy/" not in prompt


def test_cascade_with_no_optional_project_files_still_assembles(tmp_path):
    """canon/style_guide/goal/policy are all optional — agent should still load."""
    agents_root = tmp_path / "agents"
    system_root = agents_root / "muse"

    role_dir = system_root / "roles" / "writer"
    role_dir.mkdir(parents=True)
    (role_dir / "PROMPT.md").write_text("Role prompt only.")

    instance_dir = system_root / "projects" / "p" / "agents" / "writer"
    persona_dir = instance_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text("# Identity\nx")

    agent = AtomicAgent(
        name="muse/projects/p/agents/writer",
        agents_root=agents_root,
    )
    agent.load()
    prompt = agent.assemble_system_prompt()
    assert "Role prompt only." in prompt
    assert "# Identity" in prompt


def test_cascade_instance_model_md_overrides_role(tmp_path):
    agents_root = _build_full_cascade_layout(tmp_path)
    instance = agents_root / "muse" / "projects" / "the-unfinished" / "agents" / "writer"
    (instance / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    assert agent.config.default_model == "claude-haiku-4-5-20251001"

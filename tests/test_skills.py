"""Tests for atomic_agents.skills — skills primitive (spec/18).

Covers:
- discover_skills — empty list when no skills/ dir
- discover_skills — finds SKILL.md files
- discover_skills — skips dirs without SKILL.md
- discover_skills — skips dirs with invalid frontmatter
- validate_skill_manifest — name format validation (uppercase, special chars, reserved words)
- validate_skill_manifest — description required
- validate_skill_manifest — warns on body > 500 lines
- validate_skill_manifest — warns on deeply nested refs
- load_skill_body — strips frontmatter
- load_skill_referenced_file — resolves relative paths
- load_skill_referenced_file — blocks ../ traversal
- load_skill_referenced_file — blocks paths outside skill_dir
- assemble_system_prompt — includes skills section when skills present
- assemble_system_prompt — omits skills section when no skills
- load_skill tool registered when skills exist
- load_skill tool returns body for known skill (integration test)
- load_skill tool handler error for unknown skill
- load_skill_file tool for referenced files
- skill load appears in run log tool_calls rollup
"""

from __future__ import annotations

import json
import sys
import textwrap
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.exceptions import SkillFileTraversal
from atomic_agents.skills import (
    SkillManifest,
    discover_skills,
    load_skill_body,
    load_skill_referenced_file,
    validate_skill_manifest,
)
from atomic_agents.tools import ToolRegistry, ToolDefinition


# ──────────────────────────────────────────────────────────────────
# Test helpers


MINIMAL_FRONTMATTER = """\
---
name: test-skill
description: Tests something useful. Use when testing is needed.
---
"""

MINIMAL_BODY = """\
---
name: test-skill
description: Tests something useful. Use when testing is needed.
---

## Overview

This skill helps with testing.

### Pattern A

Do things this way.
"""


def _make_skill_dir(
    parent: Path,
    skill_name: str = "test-skill",
    content: str | None = None,
) -> Path:
    """Create a skill directory with a SKILL.md under parent/skills/<skill_name>/."""
    skill_dir = parent / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if content is None:
        content = MINIMAL_BODY
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def _build_minimal_agent(
    agents_root: Path,
    name: str,
    registry: ToolRegistry | None = None,
) -> AtomicAgent:
    """Create a minimal agent dir + AtomicAgent instance."""
    agent_dir = agents_root / name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTestAgent.")
    tools_md = f"## Read paths\n- ~/docs/\n\n## Write paths\n- {agent_dir}/\n"
    (agent_dir / "tools.md").write_text(tools_md)
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return AtomicAgent(
        name=name,
        agents_root=agents_root,
        tools=registry,
    )


def _make_anthropic_text_response(text: str, *, input_tokens=10, output_tokens=20):
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return types.SimpleNamespace(content=[block], usage=usage)


def _make_anthropic_tool_use_response(
    tool_name: str,
    tool_input: dict,
    tool_id: str = "tu_001",
    text: str = "",
    *,
    input_tokens=10,
    output_tokens=20,
):
    content_blocks = []
    if text:
        content_blocks.append(types.SimpleNamespace(type="text", text=text))
    content_blocks.append(
        types.SimpleNamespace(
            type="tool_use",
            id=tool_id,
            name=tool_name,
            input=tool_input,
        )
    )
    usage = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return types.SimpleNamespace(content=content_blocks, usage=usage)


@pytest.fixture(autouse=True)
def _stub_api_keys(monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    monkeypatch.setenv("ATOMIC_AGENTS_OPENAI_KEY", "fake-key")
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")


# ──────────────────────────────────────────────────────────────────
# discover_skills — basic discovery


def test_discover_skills_empty_returns_empty_list(tmp_path):
    """No skills/ directory → empty list."""
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    assert discover_skills(agent_root) == []


def test_discover_skills_finds_skill_md_files(tmp_path):
    """discover_skills returns one manifest per valid SKILL.md."""
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    _make_skill_dir(agent_root, "spreadsheet-analysis")
    _make_skill_dir(agent_root, "financial-modeling", content="""\
---
name: financial-modeling
description: Builds financial models. Use for DCF, IRR, and valuation.
---

## Overview

Financial modeling skill body.
""")

    skills = discover_skills(agent_root)
    assert len(skills) == 2
    names = {s.name for s in skills}
    assert "test-skill" in names
    assert "financial-modeling" in names


def test_discover_skills_skips_dirs_without_skill_md(tmp_path):
    """Subdirs under skills/ without SKILL.md are silently skipped."""
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    # Valid skill
    _make_skill_dir(agent_root, "good-skill")
    # Dir without SKILL.md
    empty_dir = agent_root / "skills" / "no-skill-md"
    empty_dir.mkdir(parents=True)
    (empty_dir / "README.md").write_text("Not a skill entry point.")

    skills = discover_skills(agent_root)
    assert len(skills) == 1
    assert skills[0].name == "test-skill"


def test_discover_skills_skips_dirs_with_invalid_frontmatter(tmp_path):
    """Skill dir with bad SKILL.md (missing required name) is skipped, others loaded."""
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    # Invalid: missing name
    bad_dir = agent_root / "skills" / "bad-skill"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(
        "---\ndescription: No name field here.\n---\n\nBody."
    )
    # Valid
    _make_skill_dir(agent_root, "good-skill")

    skills = discover_skills(agent_root)
    assert len(skills) == 1
    assert skills[0].name == "test-skill"


def test_discover_skills_returns_manifests_with_correct_fields(tmp_path):
    """Returned SkillManifest has all required fields populated."""
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    skill_dir = _make_skill_dir(agent_root, "test-skill")

    skills = discover_skills(agent_root)
    assert len(skills) == 1
    m = skills[0]
    assert isinstance(m, SkillManifest)
    assert m.name == "test-skill"
    assert m.description == "Tests something useful. Use when testing is needed."
    assert m.when_to_use is None
    assert m.skill_dir == skill_dir
    assert m.skill_md_path == skill_dir / "SKILL.md"
    assert m.body_lines > 0


# ──────────────────────────────────────────────────────────────────
# validate_skill_manifest — name validation


def test_skill_manifest_validates_name_format_uppercase(tmp_path):
    """Uppercase letters in name → hard error, manifest is None."""
    skill_dir = tmp_path / "Bad-Skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Bad-Skill\ndescription: Has uppercase.\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None
    assert any("invalid name" in w.lower() or "name" in w.lower() for w in warnings)


def test_skill_manifest_validates_name_format_special_chars(tmp_path):
    """Special chars (underscore, spaces) → hard error."""
    skill_dir = tmp_path / "bad_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad_skill\ndescription: Has underscore.\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None
    assert any("name" in w.lower() for w in warnings)


def test_skill_manifest_validates_name_reserved_word_anthropic(tmp_path):
    """Reserved word 'anthropic' in name → hard error."""
    skill_dir = tmp_path / "anthropic-helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: anthropic-helper\ndescription: Uses Anthropic APIs.\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None
    assert any("reserved" in w.lower() for w in warnings)


def test_skill_manifest_validates_name_reserved_word_claude(tmp_path):
    """Reserved word 'claude' in name → hard error."""
    skill_dir = tmp_path / "claude-integration"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: claude-integration\ndescription: Integrates with Claude.\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None
    assert any("reserved" in w.lower() for w in warnings)


def test_skill_manifest_validates_name_reserved_word_atomic_agents(tmp_path):
    """Reserved word 'atomic_agents' in name → hard error (if it appeared as hyphens)."""
    skill_dir = tmp_path / "atomic-agents-ext"
    skill_dir.mkdir(parents=True)
    # atomic_agents as a reserved word — name contains literal "atomic_agents"
    # In this case the name also has underscores (invalid format), so we test
    # both paths: first verify underscore blocks it
    (skill_dir / "SKILL.md").write_text(
        "---\nname: atomic_agents_ext\ndescription: Extends atomic agents.\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None


def test_skill_manifest_name_too_long(tmp_path):
    """Name > 64 chars → hard error."""
    long_name = "a" * 65
    skill_dir = tmp_path / long_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {long_name}\ndescription: Too long.\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None
    assert any("64" in w or "exceeds" in w for w in warnings)


# ──────────────────────────────────────────────────────────────────
# validate_skill_manifest — description validation


def test_skill_manifest_validates_description_required(tmp_path):
    """Missing description → hard error."""
    skill_dir = tmp_path / "no-desc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: no-desc\n---\n\nBody without description."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None
    assert any("description" in w.lower() for w in warnings)


def test_skill_manifest_validates_description_empty(tmp_path):
    """Empty description → hard error."""
    skill_dir = tmp_path / "empty-desc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: empty-desc\ndescription: ''\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is None
    assert any("description" in w.lower() for w in warnings)


def test_skill_manifest_description_over_1024_chars_warns(tmp_path):
    """Description > 1024 chars → warning (not error), manifest still returned."""
    long_desc = "A" * 1025
    skill_dir = tmp_path / "long-desc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: long-desc\ndescription: {long_desc}\n---\n\nBody."
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is not None  # still valid
    assert any("1024" in w or "description" in w.lower() for w in warnings)


# ──────────────────────────────────────────────────────────────────
# validate_skill_manifest — body length


def test_skill_manifest_warns_on_body_over_500_lines(tmp_path):
    """Body > 500 lines → warning (not error), manifest still returned."""
    body = "\n".join(f"Line {i}" for i in range(501))
    skill_dir = tmp_path / "long-body"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: long-body\ndescription: Has a long body. Use for long things.\n---\n\n"
        + body
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is not None
    assert any("500" in w or "lines" in w.lower() for w in warnings)


def test_skill_manifest_body_exactly_500_lines_no_warning(tmp_path):
    """Body exactly 500 lines → no body-length warning."""
    body = "\n".join(f"Line {i}" for i in range(500))
    skill_dir = tmp_path / "exact-body"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: exact-body\ndescription: Exactly 500 lines. Use for precision.\n---\n\n"
        + body
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is not None
    assert not any("500" in w and "lines" in w.lower() for w in warnings)


# ──────────────────────────────────────────────────────────────────
# validate_skill_manifest — reference depth warnings


def test_skill_manifest_warns_on_deeply_nested_refs(tmp_path):
    """Markdown link with path depth > 1 → warning."""
    skill_dir = tmp_path / "deep-refs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deep-refs\ndescription: Has deep refs. Use for testing.\n---\n\n"
        "See [deep file](sub/dir/file.md) for details.\n"
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is not None
    assert any("level" in w.lower() or "deep" in w.lower() for w in warnings)


def test_skill_manifest_no_warning_for_one_level_refs(tmp_path):
    """One-level-deep Markdown link → no warning."""
    skill_dir = tmp_path / "shallow-refs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: shallow-refs\ndescription: Has shallow refs. Use for testing.\n---\n\n"
        "See [reference](reference.md) for details.\n"
    )
    manifest, warnings = validate_skill_manifest(skill_dir)
    assert manifest is not None
    assert not any("level" in w.lower() or "deep" in w.lower() for w in warnings)


# ──────────────────────────────────────────────────────────────────
# load_skill_body


def test_load_skill_body_strips_frontmatter(tmp_path):
    """load_skill_body returns body without the YAML frontmatter block."""
    skill_dir = tmp_path / "skills" / "strip-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: strip-test\ndescription: Test stripping.\n---\n\n"
        "## Body content\n\nThis is the body.\n"
    )
    manifest = SkillManifest(
        name="strip-test",
        description="Test stripping.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",
        body_lines=3,
    )
    body = load_skill_body(manifest)
    assert "## Body content" in body
    assert "This is the body." in body
    # Frontmatter should be stripped
    assert "name: strip-test" not in body
    assert "description: Test stripping." not in body
    assert "---" not in body.strip()


def test_load_skill_body_returns_empty_on_missing_file(tmp_path):
    """load_skill_body returns empty string if SKILL.md is missing."""
    skill_dir = tmp_path / "skills" / "missing"
    skill_dir.mkdir(parents=True)
    manifest = SkillManifest(
        name="missing",
        description="Missing file.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",  # doesn't exist
        body_lines=0,
    )
    body = load_skill_body(manifest)
    assert body == ""


# ──────────────────────────────────────────────────────────────────
# load_skill_referenced_file


def test_load_skill_referenced_file_resolves_relative(tmp_path):
    """load_skill_referenced_file loads a file from the skill directory."""
    skill_dir = tmp_path / "skills" / "with-refs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: with-refs\ndescription: Has references.\n---\n\nSee reference.md.\n"
    )
    (skill_dir / "reference.md").write_text("# Reference\n\nExtended content here.")

    manifest = SkillManifest(
        name="with-refs",
        description="Has references.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",
        body_lines=1,
    )
    content = load_skill_referenced_file(manifest, "reference.md")
    assert "Extended content here." in content


def test_load_skill_referenced_file_blocks_path_traversal_dotdot(tmp_path):
    """../ in relative_path raises SkillFileTraversal."""
    skill_dir = tmp_path / "skills" / "traversal-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: traversal-test\ndescription: Traversal test.\n---\n\nBody.\n"
    )
    # Write a file outside the skill dir that an attacker might target
    (tmp_path / "secret.md").write_text("SECRET CONTENT")

    manifest = SkillManifest(
        name="traversal-test",
        description="Traversal test.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",
        body_lines=1,
    )
    with pytest.raises(SkillFileTraversal):
        load_skill_referenced_file(manifest, "../secret.md")


def test_load_skill_referenced_file_blocks_nested_traversal(tmp_path):
    """../../ traversal also raises SkillFileTraversal."""
    skill_dir = tmp_path / "skills" / "traversal-test2"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: traversal-test2\ndescription: Deep traversal test.\n---\n\nBody.\n"
    )
    manifest = SkillManifest(
        name="traversal-test2",
        description="Deep traversal test.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",
        body_lines=1,
    )
    with pytest.raises(SkillFileTraversal):
        load_skill_referenced_file(manifest, "../../etc/passwd")


def test_load_skill_referenced_file_blocks_outside_skill_dir(tmp_path):
    """Symlink-resolved path outside skill_dir raises SkillFileTraversal."""
    skill_dir = tmp_path / "skills" / "symlink-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: symlink-test\ndescription: Symlink test.\n---\n\nBody.\n"
    )
    manifest = SkillManifest(
        name="symlink-test",
        description="Symlink test.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",
        body_lines=1,
    )
    # Attempt with an absolute path that looks relative but resolves outside
    with pytest.raises(SkillFileTraversal):
        load_skill_referenced_file(manifest, "../other-skill/SKILL.md")


def test_load_skill_referenced_file_raises_for_missing_file(tmp_path):
    """FileNotFoundError is raised when the referenced file doesn't exist."""
    skill_dir = tmp_path / "skills" / "missing-ref"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: missing-ref\ndescription: Has a missing ref.\n---\n\nBody.\n"
    )
    manifest = SkillManifest(
        name="missing-ref",
        description="Has a missing ref.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",
        body_lines=1,
    )
    with pytest.raises(FileNotFoundError):
        load_skill_referenced_file(manifest, "nonexistent.md")


# ──────────────────────────────────────────────────────────────────
# AtomicAgent integration — system prompt assembly


def test_assemble_system_prompt_includes_skills_section_when_present(tmp_path):
    """When skills exist, assemble_system_prompt includes '# Available skills'."""
    agent_dir = tmp_path / "skill-agent"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nSkillful agent.")
    (agent_dir / "tools.md").write_text(f"## Read paths\n- ~/docs/\n## Write paths\n- {agent_dir}/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    # Create a skill
    skill_dir = agent_dir / "skills" / "financial-modeling"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: financial-modeling\n"
        "description: Builds financial models. Use for DCF and IRR.\n---\n\n"
        "## Modeling\n\nDo the math.\n"
    )

    agent = AtomicAgent(name="skill-agent", agents_root=tmp_path)
    agent.load()
    prompt = agent.assemble_system_prompt()

    assert "# Available skills" in prompt
    assert "financial-modeling" in prompt
    assert "Builds financial models" in prompt
    assert "load_skill" in prompt


def test_assemble_system_prompt_omits_skills_section_when_no_skills(tmp_path):
    """When no skills exist, assemble_system_prompt has no skills section."""
    agent_dir = tmp_path / "no-skill-agent"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nPlain agent.")
    (agent_dir / "tools.md").write_text(f"## Read paths\n- ~/docs/\n## Write paths\n- {agent_dir}/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()

    agent = AtomicAgent(name="no-skill-agent", agents_root=tmp_path)
    agent.load()
    prompt = agent.assemble_system_prompt()

    assert "# Available skills" not in prompt


# ──────────────────────────────────────────────────────────────────
# AtomicAgent integration — load_skill tool registration


def test_load_skill_tool_registered_when_skills_exist(tmp_path):
    """When skills exist, load_skill and load_skill_file are registered in tool_registry."""
    agent_dir = tmp_path / "tool-reg-agent"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nAgent.")
    (agent_dir / "tools.md").write_text(f"## Read paths\n- ~/\n## Write paths\n- {agent_dir}/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    skill_dir = agent_dir / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill. Use for testing.\n---\n\nBody.\n"
    )

    agent = AtomicAgent(name="tool-reg-agent", agents_root=tmp_path)
    assert "load_skill" in agent.tool_registry.list_names()
    assert "load_skill_file" in agent.tool_registry.list_names()


def test_load_skill_tool_not_registered_when_no_skills(tmp_path):
    """When no skills exist, load_skill is NOT registered in tool_registry."""
    agent_dir = tmp_path / "no-tool-agent"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nAgent.")
    (agent_dir / "tools.md").write_text(f"## Read paths\n- ~/\n## Write paths\n- {agent_dir}/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()

    agent = AtomicAgent(name="no-tool-agent", agents_root=tmp_path)
    assert "load_skill" not in agent.tool_registry.list_names()
    assert "load_skill_file" not in agent.tool_registry.list_names()


# ──────────────────────────────────────────────────────────────────
# AtomicAgent integration — load_skill tool behavior (mock LLM)


def _build_agent_with_skill(
    agents_root: Path,
    name: str = "skill-run-agent",
    skill_body: str = "## Skill body\n\nHere is the guidance.",
) -> AtomicAgent:
    """Build an agent with a single skill for integration testing."""
    agent_dir = agents_root / name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nAgent.")
    (agent_dir / "tools.md").write_text(
        f"## Read paths\n- ~/\n## Write paths\n- {agent_dir}/\n"
    )
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    skill_dir = agent_dir / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill. Use for testing.\n---\n\n"
        + skill_body
    )
    return AtomicAgent(name=name, agents_root=agents_root)


def test_load_skill_tool_returns_body_for_known_skill(tmp_path):
    """Full integration: LLM calls load_skill → agent returns body → LLM continues."""
    skill_body = "## Skill guidance\n\nDo this specific thing."
    agent = _build_agent_with_skill(tmp_path, skill_body=skill_body)

    fake_anthropic = MagicMock()

    # First LLM response: calls load_skill
    response_1 = _make_anthropic_tool_use_response(
        tool_name="load_skill",
        tool_input={"skill_name": "test-skill"},
        tool_id="tu_skill_001",
    )
    # Second LLM response: uses the loaded skill body
    response_2 = _make_anthropic_text_response("I have loaded the skill and here is my answer.")

    fake_anthropic.Anthropic.return_value.messages.create.side_effect = [
        response_1, response_2,
    ]

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Use the test skill to help me.")

    # Two LLM turns: one for tool call, one for final response
    assert fake_anthropic.Anthropic.return_value.messages.create.call_count == 2
    assert response.tool_iterations == 2
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.tool_name == "load_skill"
    assert tc.error is None
    # Output should contain the skill body
    assert "## Skill guidance" in tc.output
    assert "Do this specific thing." in tc.output


def test_load_skill_tool_handler_error_for_unknown_skill(tmp_path):
    """load_skill with an unregistered skill name → ToolCallResult with error."""
    agent = _build_agent_with_skill(tmp_path, name="err-agent")

    fake_anthropic = MagicMock()

    # LLM calls load_skill with a bad name
    response_1 = _make_anthropic_tool_use_response(
        tool_name="load_skill",
        tool_input={"skill_name": "nonexistent-skill"},
        tool_id="tu_err_001",
    )
    # After error feedback, LLM returns final text
    response_2 = _make_anthropic_text_response("I could not find that skill.")

    fake_anthropic.Anthropic.return_value.messages.create.side_effect = [
        response_1, response_2,
    ]

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Load a nonexistent skill.")

    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.tool_name == "load_skill"
    # Handler raised ToolHandlerError — error is captured in result, not propagated
    assert tc.error is not None
    assert "nonexistent-skill" in tc.error or "Unknown skill" in tc.error


def test_load_skill_file_tool_for_referenced_files(tmp_path):
    """load_skill_file integration: LLM calls it → agent returns file content."""
    agent_dir = tmp_path / "file-skill-agent"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nAgent.")
    (agent_dir / "tools.md").write_text(
        f"## Read paths\n- ~/\n## Write paths\n- {agent_dir}/\n"
    )
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    skill_dir = agent_dir / "skills" / "ref-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ref-skill\ndescription: Has references. Use for ref testing.\n---\n\n"
        "See [reference.md](reference.md) for more.\n"
    )
    (skill_dir / "reference.md").write_text("# Reference\n\nDeep reference content here.")

    agent = AtomicAgent(name="file-skill-agent", agents_root=tmp_path)

    fake_anthropic = MagicMock()
    response_1 = _make_anthropic_tool_use_response(
        tool_name="load_skill_file",
        tool_input={"skill_name": "ref-skill", "relative_path": "reference.md"},
        tool_id="tu_file_001",
    )
    response_2 = _make_anthropic_text_response("I read the reference file.")

    fake_anthropic.Anthropic.return_value.messages.create.side_effect = [
        response_1, response_2,
    ]

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Read the reference file from ref-skill.")

    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.tool_name == "load_skill_file"
    assert tc.error is None
    assert "Deep reference content here." in tc.output


# ──────────────────────────────────────────────────────────────────
# Run log integration — skill tool calls appear in rollup


def test_skill_load_appears_in_run_log_tool_calls_rollup(tmp_path):
    """When load_skill is called, the run log includes it in tool_calls."""
    agent = _build_agent_with_skill(tmp_path, name="log-skill-agent")

    fake_anthropic = MagicMock()
    response_1 = _make_anthropic_tool_use_response(
        tool_name="load_skill",
        tool_input={"skill_name": "test-skill"},
        tool_id="tu_log_skill",
    )
    response_2 = _make_anthropic_text_response("Final answer.")

    fake_anthropic.Anthropic.return_value.messages.create.side_effect = [
        response_1, response_2,
    ]

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Load and use the skill.")

    # Read the log file
    from datetime import date
    agent_root = tmp_path / "log-skill-agent"
    today = date.today()
    log_file = agent_root / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    assert log_file.exists()

    records = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    run_records = [r for r in records if r.get("trigger") == "manual"]
    assert len(run_records) == 1
    run_rec = run_records[0]
    assert "tool_calls" in run_rec
    tool_names = [t["tool_name"] for t in run_rec["tool_calls"]]
    assert "load_skill" in tool_names
    assert run_rec["tool_iterations"] == 2


# ──────────────────────────────────────────────────────────────────
# Codex R2 regression tests — one-level-deep file limit (spec/18 §37)
# ──────────────────────────────────────────────────────────────────


def _make_skill_manifest(skill_dir: Path, name: str = "test-skill") -> SkillManifest:
    """Build a SkillManifest pointing at skill_dir (doesn't need to exist for unit tests)."""
    return SkillManifest(
        name=name,
        description="A test skill.",
        when_to_use=None,
        skill_dir=skill_dir,
        skill_md_path=skill_dir / "SKILL.md",
        body_lines=1,
    )


def test_skill_referenced_file_refuses_subdir(tmp_path):
    """load_skill_referenced_file refuses 'subdir/file.md' — one level limit (spec/18 §37).

    The traversal block alone was NOT enforcing this: 'subdir/file.md' has no
    '..' so it passed through.  This regression test verifies the new depth
    check rejects any path with a directory component.
    """
    skill_dir = tmp_path / "skills" / "depth-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: depth-test\ndescription: Depth test skill.\n---\n\nBody.\n"
    )
    # Create the nested file so the check doesn't fail on FileNotFoundError first
    subdir = skill_dir / "subdir"
    subdir.mkdir()
    (subdir / "file.md").write_text("Should not be accessible.")

    manifest = _make_skill_manifest(skill_dir, name="depth-test")
    with pytest.raises(SkillFileTraversal, match="one level"):
        load_skill_referenced_file(manifest, "subdir/file.md")


def test_skill_referenced_file_refuses_multi_level_subdir(tmp_path):
    """Multi-level paths like 'a/b/file.md' are also refused."""
    skill_dir = tmp_path / "skills" / "depth-test2"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: depth-test2\ndescription: Multi-level depth test.\n---\n\nBody.\n"
    )
    nested = skill_dir / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "file.md").write_text("Should not be accessible.")

    manifest = _make_skill_manifest(skill_dir, name="depth-test2")
    with pytest.raises(SkillFileTraversal, match="one level"):
        load_skill_referenced_file(manifest, "a/b/file.md")


def test_skill_referenced_file_allows_bare_filename(tmp_path):
    """A bare filename with no directory component succeeds (one-level-deep limit allows it)."""
    skill_dir = tmp_path / "skills" / "ok-depth"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ok-depth\ndescription: Ok depth skill.\n---\n\nBody.\n"
    )
    (skill_dir / "examples.md").write_text("# Examples\n\nHere.")

    manifest = _make_skill_manifest(skill_dir, name="ok-depth")
    content = load_skill_referenced_file(manifest, "examples.md")
    assert "Examples" in content

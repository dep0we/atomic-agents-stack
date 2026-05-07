"""Regression tests for single-agent goal.md loading per spec/04 step 3.5 (codex P1).

Verifies that AtomicAgent:
- Reads <agent>/goal.md for single-agent layouts and injects it between persona
  and tools in the system prompt (spec/04 step 3.5).
- Does not add a goal section when goal.md is absent (backwards compat).
- Parses agent_mode from IDENTITY.md and exposes it on self.agent_mode.
- Defaults agent_mode to "reactive" when IDENTITY.md has no Operating-mode section.
- Records agent_mode in the run log record.
- Does NOT duplicate goal context for cascaded agents (project-level goal is
  already loaded by _load_project_layer_text).
"""

from __future__ import annotations
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from atomic_agents.agent import AtomicAgent


# ──────────────────────────────────────────────────────────────────
# Helpers


def _build_single_agent(
    tmp_path: Path,
    *,
    agent_name: str = "caldwell",
    identity_content: str | None = None,
    goal_md_content: str | None = None,
) -> Path:
    """Build a minimal single-agent layout. Returns agents_root."""
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / agent_name
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)

    identity_text = identity_content or "# Identity\nCaldwell — personal finance advisor."
    (persona_dir / "IDENTITY.md").write_text(identity_text)
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()

    if goal_md_content is not None:
        (agent_dir / "goal.md").write_text(goal_md_content)

    return agents_root


def _build_full_cascade_layout(tmp_path: Path) -> Path:
    """Reuse the full cascade tree from test_agent_cascade_integration. Returns agents_root."""
    agents_root = tmp_path / "agents"
    system_root = agents_root / "muse"

    role_dir = system_root / "roles" / "writer"
    role_dir.mkdir(parents=True)
    (role_dir / "PROMPT.md").write_text("You are a Muse Writer.")
    (role_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (role_dir / "model.md").write_text(
        "## Default model\nclaude-sonnet-4-6-20260101\n\n"
        "Max input prompt tokens: 12,000\nMax output tokens: 4,000\n"
    )

    project_dir = system_root / "projects" / "the-unfinished"
    project_dir.mkdir(parents=True)
    (project_dir / "canon.md").write_text("## World\nThe Unfinished is set in 1920s Vienna.")
    (project_dir / "style_guide.md").write_text("Use Oxford commas.")
    # Project-level goal (cascade path)
    (project_dir / "goal.md").write_text("Finish Act II by Q3 2026.")
    policy_dir = project_dir / "policy"
    policy_dir.mkdir()
    (policy_dir / "01_voice.md").write_text("Third person past.")

    instance_dir = project_dir / "agents" / "writer"
    persona_dir = instance_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text("# Identity\nWriter on The Unfinished.")
    (instance_dir / "memory").mkdir()
    (instance_dir / "wiki").mkdir()
    (instance_dir / "journal").mkdir()
    (instance_dir / "log").mkdir()

    return agents_root


_MINIMAL_GOAL_MD = """\
---
schema_version: 1
active: true
intent: Finish the Q2 research report
priority: high
created: 2026-05-01
last_progress_check: 2026-05-06
deadline: 2026-06-30
success_criteria:
  - All sections drafted
  - Citations verified
sub_goals: []
---

## Current state

Research in early stages.
"""


# ──────────────────────────────────────────────────────────────────
# test 1: single-agent with goal.md — goal section appears in prompt


def test_single_agent_with_goal_md_loads_into_prompt(tmp_path):
    agents_root = _build_single_agent(tmp_path, goal_md_content=_MINIMAL_GOAL_MD)
    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    agent.load()
    prompt = agent.assemble_system_prompt()

    # Goal section header must appear
    assert "# goal.md" in prompt
    # Goal body content from the fixture
    assert "Finish the Q2 research report" in prompt
    assert "All sections drafted" in prompt


# ──────────────────────────────────────────────────────────────────
# test 2: single-agent WITHOUT goal.md — no goal section in prompt (backwards compat)


def test_single_agent_without_goal_md_no_section(tmp_path):
    agents_root = _build_single_agent(tmp_path)  # no goal_md_content
    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    agent.load()
    prompt = agent.assemble_system_prompt()

    assert "# goal.md" not in prompt


# ──────────────────────────────────────────────────────────────────
# test 3: goal section is injected between persona and tools (spec/04 order)


def test_single_agent_goal_section_between_persona_and_tools(tmp_path):
    agents_root = _build_single_agent(tmp_path, goal_md_content=_MINIMAL_GOAL_MD)
    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    agent.load()
    prompt = agent.assemble_system_prompt()

    persona_idx = prompt.index("Caldwell")
    goal_idx = prompt.index("# goal.md")
    tools_idx = prompt.index("# tools.md")

    assert persona_idx < goal_idx < tools_idx, (
        f"Expected persona ({persona_idx}) < goal ({goal_idx}) < tools ({tools_idx})"
    )


# ──────────────────────────────────────────────────────────────────
# test 4: agent_mode parsed correctly from IDENTITY.md (goal-driven)


def test_agent_mode_parsed_from_goal_driven_identity(tmp_path):
    identity_text = """\
# Identity

Muse Director.

## Operating mode

This agent is **goal-driven**.

When goal.md exists with active: true, I pursue the goal.
"""
    agents_root = _build_single_agent(tmp_path, identity_content=identity_text)
    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    assert agent.agent_mode == "goal-driven"


# ──────────────────────────────────────────────────────────────────
# test 5: agent_mode parsed correctly — hybrid


def test_agent_mode_parsed_from_hybrid_identity(tmp_path):
    identity_text = """\
# Identity

Director.

## Operating mode

Hybrid. Reactive by default. Goal-driven when active goal.md exists.
"""
    agents_root = _build_single_agent(tmp_path, identity_content=identity_text)
    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    assert agent.agent_mode == "hybrid"


# ──────────────────────────────────────────────────────────────────
# test 6: agent_mode defaults to "reactive" when no Operating-mode section


def test_agent_mode_default_reactive(tmp_path):
    agents_root = _build_single_agent(tmp_path)  # plain IDENTITY.md, no Operating mode section
    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    assert agent.agent_mode == "reactive"


# ──────────────────────────────────────────────────────────────────
# test 7: run log records agent_mode after a (mocked) agent.call()


def _make_fake_anthropic(response_text: str = "All done."):
    """Build a fake anthropic module that returns a canned response."""
    fake_anthropic = types.ModuleType("anthropic")
    fake_client = MagicMock()

    block = types.SimpleNamespace(type="text", text=response_text)
    usage = types.SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    fake_response = types.SimpleNamespace(content=[block], usage=usage)
    fake_client.messages.create.return_value = fake_response

    fake_anthropic.Anthropic = MagicMock(return_value=fake_client)
    fake_anthropic.APIStatusError = Exception
    fake_anthropic.APIConnectionError = Exception
    fake_anthropic.RateLimitError = Exception
    return fake_anthropic


def test_run_log_records_agent_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    agents_root = _build_single_agent(tmp_path, goal_md_content=_MINIMAL_GOAL_MD)
    agent = AtomicAgent(name="caldwell", agents_root=agents_root)
    agent.load()

    fake_anthropic = _make_fake_anthropic()
    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        agent.call(work_item="Test call", write_captures=False)

    # Find the log file just written
    log_files = list((agent.agent_root / "log").rglob("*.jsonl"))
    assert log_files, "No log file was written"

    log_lines = log_files[0].read_text().strip().splitlines()
    # The last non-warning log line should be the run record
    run_records = [
        json.loads(line) for line in log_lines
        if json.loads(line).get("status") == "ok"
        and json.loads(line).get("trigger") != "cost_warning"
        and "agent_mode" in json.loads(line)
    ]
    assert run_records, "No log record with agent_mode found"
    assert run_records[-1]["agent_mode"] == "reactive"


# ──────────────────────────────────────────────────────────────────
# test 8: cascaded agent does NOT double-load single-agent goal.md


def test_cascade_agent_does_not_double_load_single_goal(tmp_path):
    """Cascaded agent picks up project-level goal.md (existing behaviour).
    An instance-level goal.md in the agents/<role>/ folder must NOT also be
    injected under # goal.md — that would duplicate context.
    """
    agents_root = _build_full_cascade_layout(tmp_path)

    # Also plant a goal.md directly inside the instance folder
    instance_dir = (
        agents_root / "muse" / "projects" / "the-unfinished" / "agents" / "writer"
    )
    (instance_dir / "goal.md").write_text("INSTANCE GOAL — should NOT appear in prompt.")

    agent = AtomicAgent(
        name="muse/projects/the-unfinished/agents/writer",
        agents_root=agents_root,
    )
    agent.load()
    prompt = agent.assemble_system_prompt()

    # The project-level goal is present via the cascade path
    assert "Finish Act II" in prompt

    # The instance goal.md body must NOT appear (cascade path; _load_goal_text
    # is only called for non-cascade agents)
    assert "INSTANCE GOAL — should NOT appear in prompt." not in prompt

    # The single-agent "# goal.md" header must NOT appear either
    assert "# goal.md" not in prompt

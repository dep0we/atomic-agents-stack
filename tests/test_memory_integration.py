"""Live agent.memory integration tests (P3.3 codex fix).

These tests exercise the full agent.call() → capture extraction →
memory.write_note() chain, verifying that:
  - Captures emitted during agent.call() land on disk via agent.memory
  - Pinned/recent notes are loaded via agent.memory (not direct fs reads)
  - agent.memory is a FilesystemBackend instance (default)

These are integration tests: they build a real AtomicAgent with a
temporary agent directory and mock only the LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.memory.filesystem import FilesystemBackend
from atomic_agents.memory.backend import WritePolicy


# ──────────────────────────────────────────────────────────────────
# Shared setup

def _build_agent(tmp_path: Path, name: str = "test_agent") -> AtomicAgent:
    """Build a minimal AtomicAgent in a temp directory."""
    agent_dir = tmp_path / name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTest agent.")
    tools_md = (
        "## Read paths\n"
        f"- {agent_dir}/\n\n"
        "## Write paths\n"
        f"- {agent_dir}/memory/\n"
    )
    (agent_dir / "tools.md").write_text(tools_md)
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return AtomicAgent(name=name, agents_root=tmp_path)


def _make_llm_response(text: str, input_tokens: int = 10, output_tokens: int = 20):
    """Construct a mock _RawLLMResponse-compatible object returned by call_llm."""
    resp = MagicMock()
    resp.text = text
    resp.input_tokens = input_tokens
    resp.output_tokens = output_tokens
    resp.cache_hit_tokens = 0
    resp.cache_miss_tokens = 0
    resp.tool_uses = []
    resp.raw = None
    return resp


# ──────────────────────────────────────────────────────────────────
# Tests

def test_agent_memory_is_filesystem_backend(tmp_path):
    """agent.memory should be a FilesystemBackend by default after construction."""
    agent = _build_agent(tmp_path)
    assert isinstance(agent.memory, FilesystemBackend), (
        f"Expected FilesystemBackend, got {type(agent.memory)}"
    )


def test_agent_call_capture_lands_on_disk_via_memory(tmp_path):
    """Full agent.call() → capture extraction → memory.write_note() chain.

    A mocked LLM emits a fenced atomic_capture block. After call() returns,
    the note must be discoverable via agent.memory.read_note() and also
    exist as a .md file in memory/.
    """
    agent = _build_agent(tmp_path)
    agent_dir = tmp_path / "test_agent"
    memory_dir = agent_dir / "memory"

    # LLM response with an embedded atomic_capture fenced block
    capture_json = json.dumps({
        "type": "feedback",
        "name": "Integration test preference",
        "description": "Captured via integration test",
        "confidence": "high",
        "sources": ["integration_test"],
        "body": "This is the body of the integration test note.",
    })
    llm_response_text = (
        f"Understood.\n\n"
        f"```atomic_capture\n{capture_json}\n```\n\n"
        "I've captured that preference."
    )
    mock_response = _make_llm_response(llm_response_text)

    with patch("atomic_agents._llm.call_llm", return_value=mock_response):
        response = agent.call(
            work_item="Note this preference.",
            write_captures=True,
        )

    # The response should complete without error
    assert response is not None

    # The note must be on disk
    note_files = list(memory_dir.glob("feedback_*.md"))
    assert len(note_files) >= 1, (
        f"Expected at least one feedback_*.md in memory/; found: {list(memory_dir.iterdir())}"
    )

    # It must be readable via agent.memory (P2.1 — uses read_note, not direct fs)
    note_path = note_files[0]
    note = agent.memory.read_note(note_path.name)
    assert note is not None, f"agent.memory.read_note({note_path.name!r}) returned None"
    assert note.name == "Integration test preference"
    assert note.confidence == "high"
    assert "integration test note" in note.body


def test_agent_load_pinned_notes_uses_memory_read_note(tmp_path):
    """_load_pinned_notes must use agent.memory.read_note(), not direct frontmatter.load().

    Verify by writing a pinned note via the backend and confirming it appears
    in the assembled system prompt (which is built from _load_pinned_notes()).
    """
    agent = _build_agent(tmp_path)
    agent_dir = tmp_path / "test_agent"
    memory_dir = agent_dir / "memory"

    from atomic_agents.types import Capture
    capture = Capture(
        type="feedback",
        name="Pinned preference",
        description="A pinned note",
        confidence="high",
        sources=["test"],
        body="Always prefer direct answers.",
        pinned=True,
    )
    policy = WritePolicy(write_paths=[memory_dir])
    agent.memory.write_note(capture, policy)

    # Reload the agent to trigger _load_pinned_notes
    agent.load()
    assert agent._pinned_notes, "Expected at least one pinned note to be loaded"
    combined = "\n".join(agent._pinned_notes)
    assert "Pinned preference" in combined, (
        f"Pinned note name not found in loaded pinned notes: {combined[:200]}"
    )


def test_agent_load_recent_notes_uses_memory_list_recent(tmp_path):
    """_load_recent_notes must use agent.memory.list_recent() + read_note().

    Write two non-pinned notes and verify they appear in _recent_notes.
    """
    agent = _build_agent(tmp_path)
    agent_dir = tmp_path / "test_agent"
    memory_dir = agent_dir / "memory"

    from atomic_agents.types import Capture
    from datetime import date

    policy = WritePolicy(write_paths=[memory_dir])
    for i in range(2):
        capture = Capture(
            type="feedback",
            name=f"Recent note {i}",
            description=f"A recent note {i}",
            confidence="medium",
            sources=["test"],
            body=f"Body of recent note {i}.",
        )
        agent.memory.write_note(capture, policy)

    agent.load()
    combined = "\n".join(agent._recent_notes)
    assert "Recent note 0" in combined or "Recent note 1" in combined, (
        f"Expected recent notes in loaded content; got: {combined[:200]}"
    )

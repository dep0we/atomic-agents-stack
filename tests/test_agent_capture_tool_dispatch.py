"""Tests for AtomicAgent._capture_tool_definitions provider dispatch."""

from atomic_agents.agent import AtomicAgent


def test_claude_model_returns_anthropic_tool_def():
    tools = AtomicAgent._capture_tool_definitions("claude-sonnet-4-6-20250101")
    assert tools is not None
    assert len(tools) == 1
    assert tools[0]["name"] == "atomic_capture"
    assert "input_schema" in tools[0]


def test_gpt_model_returns_openai_tool_def():
    tools = AtomicAgent._capture_tool_definitions("gpt-4o-mini")
    assert tools is not None
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "atomic_capture"


def test_moonshot_model_returns_openai_tool_def():
    tools = AtomicAgent._capture_tool_definitions("moonshot/kimi-k2-0905-preview")
    assert tools is not None
    assert len(tools) == 1
    assert tools[0]["type"] == "function"


def test_unknown_model_returns_none():
    """Unknown providers fall back to Path 2 (fenced blocks only)."""
    tools = AtomicAgent._capture_tool_definitions("some-future-model-id")
    assert tools is None

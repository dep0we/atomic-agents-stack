"""Tests for AtomicAgent._capture_tool_definitions provider dispatch.

Post-#87 PR 2.5: the method returns canonical ``LLMToolDefinition``
instances regardless of provider — backends translate to provider format
inside their own ``call()``. The test surface accordingly checks
canonical-type attributes rather than provider-shaped dict keys.
"""

from atomic_agents.agent import AtomicAgent
from atomic_agents.llm.types import LLMToolDefinition


def test_claude_model_returns_canonical_tool_def():
    tools = AtomicAgent._capture_tool_definitions("claude-sonnet-4-6-20250101")
    assert tools is not None
    assert len(tools) == 1
    assert isinstance(tools[0], LLMToolDefinition)
    assert tools[0].name == "atomic_capture"
    # input_schema is the canonical field name (matches Anthropic's wire shape)
    assert isinstance(tools[0].input_schema, dict)


def test_gpt_model_returns_canonical_tool_def():
    """Same canonical shape for OpenAI models — backends translate at call()
    time. Agent.py no longer branches on model prefix for tool format.
    """
    tools = AtomicAgent._capture_tool_definitions("gpt-4o-mini")
    assert tools is not None
    assert len(tools) == 1
    assert isinstance(tools[0], LLMToolDefinition)
    assert tools[0].name == "atomic_capture"


def test_moonshot_model_returns_canonical_tool_def():
    tools = AtomicAgent._capture_tool_definitions("moonshot/kimi-k2-0905-preview")
    assert tools is not None
    assert len(tools) == 1
    assert isinstance(tools[0], LLMToolDefinition)
    assert tools[0].name == "atomic_capture"


def test_unknown_model_returns_none():
    """Unknown providers fall back to Path 2 (fenced blocks only)."""
    tools = AtomicAgent._capture_tool_definitions("some-future-model-id")
    assert tools is None

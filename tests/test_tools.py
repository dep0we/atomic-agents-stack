"""Tests for atomic_agents.tools — custom tools primitive (spec/17).

Covers:
- ToolRegistry register, get, list_names
- ToolRegistry.to_anthropic_definitions format
- ToolRegistry.to_openai_definitions format
- ToolRegistry.execute — happy path
- ToolRegistry.execute — required-field validation → ToolInputInvalid
- ToolRegistry.execute — handler raises → ToolCallResult.error
- ToolRegistry.execute — unknown tool → ToolNotRegistered
- AtomicAgent passes custom tools to LLM call kwargs
- AtomicAgent executes tool and loops (two iterations)
- AtomicAgent stops at max_iterations cap
- AtomicAgent cost cap breaks tool loop
- atomic_capture still works alongside custom tools (backwards compat)
- Response.tool_calls field is populated
- Run log records tool_calls rollup
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.exceptions import ToolInputInvalid, ToolNotRegistered
from atomic_agents.tools import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    MAX_TOOL_ITERATIONS,
    ToolCallResult,
    ToolDefinition,
    ToolRegistry,
)
from atomic_agents.types import Response


# ──────────────────────────────────────────────────────────────────
# Helpers shared across tests


def _make_simple_tool(name: str = "echo_tool") -> ToolDefinition:
    """Build a trivial ToolDefinition for testing."""
    return ToolDefinition(
        name=name,
        description=f"Echo the message back.",
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Text to echo"},
                "count": {"type": "integer", "description": "Optional repeat count"},
            },
            "required": ["message"],
        },
        handler=lambda inp: f"echo: {inp['message']}",
    )


def _build_minimal_agent(
    agents_root: Path,
    name: str,
    registry: ToolRegistry | None = None,
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
) -> AtomicAgent:
    """Create a minimal agent dir + AtomicAgent instance."""
    agent_dir = agents_root / name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTestAgent.")
    # Include write paths so atomic_capture can write notes to memory/
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
        max_tool_iterations=max_tool_iterations,
    )


def _make_anthropic_text_response(text: str, *, input_tokens=10, output_tokens=20):
    """Mock a pure-text Anthropic response."""
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return types.SimpleNamespace(content=[block], usage=usage)


def _make_anthropic_tool_use_response(
    tool_name: str, tool_input: dict, tool_id: str = "tu_001",
    text: str = "", *, input_tokens=10, output_tokens=20,
):
    """Mock an Anthropic response with a tool_use block."""
    content_blocks = []
    if text:
        content_blocks.append(types.SimpleNamespace(type="text", text=text))
    content_blocks.append(types.SimpleNamespace(
        type="tool_use",
        id=tool_id,
        name=tool_name,
        input=tool_input,
    ))
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
# ToolRegistry unit tests


def test_tool_registry_register_and_get():
    """Registered tool is retrievable by name."""
    registry = ToolRegistry()
    tool = _make_simple_tool("my_tool")
    registry.register(tool)

    assert registry.get("my_tool") is tool
    assert registry.get("nonexistent") is None
    assert registry.list_names() == ["my_tool"]
    assert len(registry) == 1
    assert bool(registry) is True


def test_tool_registry_register_overwrites_same_name_with_allow_overwrite():
    """Re-registering with allow_overwrite=True replaces the old definition."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="tool_x", description="v1", input_schema={}, handler=lambda _: "v1"
    ))
    registry.register(ToolDefinition(
        name="tool_x", description="v2", input_schema={}, handler=lambda _: "v2"
    ), allow_overwrite=True)
    assert registry.get("tool_x").description == "v2"
    assert len(registry) == 1


def test_tool_registry_list_names_sorted():
    """list_names() returns sorted order."""
    registry = ToolRegistry()
    for name in ["zebra", "alpha", "mango"]:
        registry.register(_make_simple_tool(name))
    assert registry.list_names() == ["alpha", "mango", "zebra"]


def test_tool_registry_empty_is_falsy():
    registry = ToolRegistry()
    assert bool(registry) is False
    assert len(registry) == 0


def test_tool_registry_to_anthropic_format():
    """to_anthropic_definitions() produces Anthropic-shaped tool dicts."""
    registry = ToolRegistry()
    registry.register(_make_simple_tool("query_database"))

    defs = registry.to_anthropic_definitions()
    assert len(defs) == 1
    d = defs[0]
    assert d["name"] == "query_database"
    assert "description" in d
    assert "input_schema" in d
    # Must NOT have "type" key (that's the OpenAI wrapper)
    assert "type" not in d


def test_tool_registry_to_openai_format():
    """to_openai_definitions() produces OpenAI function-calling tool dicts."""
    registry = ToolRegistry()
    registry.register(_make_simple_tool("fetch_calendar"))

    defs = registry.to_openai_definitions()
    assert len(defs) == 1
    d = defs[0]
    assert d["type"] == "function"
    fn = d["function"]
    assert fn["name"] == "fetch_calendar"
    assert "description" in fn
    assert "parameters" in fn


def test_tool_registry_to_anthropic_format_multiple():
    """Multiple registered tools all appear in definitions."""
    registry = ToolRegistry()
    for name in ["tool_a", "tool_b", "tool_c"]:
        registry.register(_make_simple_tool(name))
    defs = registry.to_anthropic_definitions()
    assert len(defs) == 3
    names = {d["name"] for d in defs}
    assert names == {"tool_a", "tool_b", "tool_c"}


def test_tool_registry_execute_calls_handler():
    """Handler is called with the input dict; output is captured."""
    called_with = {}

    def my_handler(inp: dict):
        called_with.update(inp)
        return "handler_output"

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="my_tool",
        description="test",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        handler=my_handler,
    ))

    result = registry.execute({
        "name": "my_tool",
        "id": "tu_abc",
        "input": {"message": "hello"},
    })

    assert isinstance(result, ToolCallResult)
    assert result.tool_name == "my_tool"
    assert result.tool_use_id == "tu_abc"
    assert result.input == {"message": "hello"}
    assert result.output == "handler_output"
    assert result.error is None
    assert result.latency_ms >= 0
    assert called_with == {"message": "hello"}


def test_tool_registry_execute_validates_required_fields():
    """Missing required field raises ToolInputInvalid."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="strict_tool",
        description="needs query",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=lambda inp: "ok",
    ))

    with pytest.raises(ToolInputInvalid, match="query"):
        registry.execute({
            "name": "strict_tool",
            "id": "tu_1",
            "input": {},  # missing "query"
        })


def test_tool_registry_execute_validates_field_type():
    """Wrong type for a field raises ToolInputInvalid."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="typed_tool",
        description="needs integer limit",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
            },
            "required": ["limit"],
        },
        handler=lambda inp: "ok",
    ))

    with pytest.raises(ToolInputInvalid, match="limit"):
        registry.execute({
            "name": "typed_tool",
            "id": "tu_2",
            "input": {"limit": "not-an-int"},  # should be int
        })


def test_tool_registry_execute_wraps_handler_errors():
    """Handler exception → ToolCallResult.error populated, no propagation."""

    def bad_handler(inp: dict):
        raise ValueError("handler exploded")

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="exploding_tool",
        description="will fail",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=bad_handler,
    ))

    result = registry.execute({
        "name": "exploding_tool",
        "id": "tu_err",
        "input": {},
    })

    assert result.error is not None
    assert "ValueError" in result.error
    assert "handler exploded" in result.error
    assert result.output is None


def test_tool_registry_execute_unknown_tool_raises():
    """Calling a tool not in the registry raises ToolNotRegistered."""
    registry = ToolRegistry()

    with pytest.raises(ToolNotRegistered, match="ghost_tool"):
        registry.execute({"name": "ghost_tool", "id": "tu_x", "input": {}})


# ──────────────────────────────────────────────────────────────────
# Agent integration tests


def test_agent_passes_custom_tools_to_llm(tmp_path):
    """Agent with registered tools includes them in LLM call kwargs."""
    registry = ToolRegistry()
    registry.register(_make_simple_tool("query_db"))
    agent = _build_minimal_agent(tmp_path, "test-agent", registry=registry)

    # Intercept the LLM call
    captured_kwargs: dict = {}

    def fake_call_llm(**kwargs):
        captured_kwargs.update(kwargs)
        # Return a simple text-only response
        from atomic_agents._llm import _RawLLMResponse
        return _RawLLMResponse(text="Done.", input_tokens=10, output_tokens=5)

    with patch("atomic_agents.agent._llm.call_llm", side_effect=fake_call_llm):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            agent.call("Test work item")

    tools_passed = captured_kwargs.get("tools", [])
    assert tools_passed is not None
    # Post-#87 PR 2.5: agent.py passes canonical LLMToolDefinition lists
    # to _llm.call_llm rather than provider-shaped dicts. The backend
    # (or transitional glue in _llm) translates at the dispatch boundary.
    tool_names = {t.name for t in tools_passed}
    assert "atomic_capture" in tool_names
    assert "query_db" in tool_names


def test_agent_executes_tool_and_loops(tmp_path):
    """Agent with registered tool: first call returns tool_use, second returns text."""
    registry = ToolRegistry()
    executed_inputs: list[dict] = []

    def echo_handler(inp: dict) -> str:
        executed_inputs.append(inp)
        return f"echo: {inp['msg']}"

    registry.register(ToolDefinition(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
        handler=echo_handler,
    ))

    agent = _build_minimal_agent(tmp_path, "loop-agent", registry=registry)

    fake_anthropic = MagicMock()

    # First response: tool_use
    response_1 = _make_anthropic_tool_use_response(
        tool_name="echo", tool_input={"msg": "hello"}, tool_id="tu_001"
    )
    # Second response: plain text
    response_2 = _make_anthropic_text_response("Final answer after echo.")

    fake_anthropic.Anthropic.return_value.messages.create.side_effect = [
        response_1, response_2
    ]

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Please echo hello.")

    # Should have made 2 LLM calls
    assert fake_anthropic.Anthropic.return_value.messages.create.call_count == 2
    # Tool was executed once
    assert len(executed_inputs) == 1
    assert executed_inputs[0] == {"msg": "hello"}
    # Response fields
    assert response.text == "Final answer after echo."
    assert response.tool_iterations == 2
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.tool_name == "echo"
    assert tc.output == "echo: hello"
    assert tc.error is None

    # Second-iteration messages payload — verify the tool-loop continuation
    # builds the right shape AFTER #87 PR 2.5's refactor routed through
    # AnthropicLLMBackend.format_tool_results. Pre-#87 the inline
    # construction in agent.py produced this same shape; the regression
    # surface this test protects is wire-byte parity (#87 PR 2.5 review
    # caught a string-not-json-encoded gap that this test now pins).
    call_args_list = fake_anthropic.Anthropic.return_value.messages.create.call_args_list
    second_call_kwargs = call_args_list[1].kwargs
    msgs = second_call_kwargs["messages"]
    # Original user prompt + assistant turn echoing the tool_use + user turn with tool_result
    assert msgs[0]["role"] == "user"  # original prompt
    assert msgs[1]["role"] == "assistant"
    assistant_content = msgs[1]["content"]
    tool_use_blocks = [b for b in assistant_content if b.get("type") == "tool_use"]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0]["name"] == "echo"
    assert tool_use_blocks[0]["id"] == "tu_001"
    assert tool_use_blocks[0]["input"] == {"msg": "hello"}
    # User turn with tool_result
    assert msgs[2]["role"] == "user"
    result_blocks = [b for b in msgs[2]["content"] if b.get("type") == "tool_result"]
    assert len(result_blocks) == 1
    assert result_blocks[0]["tool_use_id"] == "tu_001"
    # Wire bytes — string output is json-encoded (matches pre-#87)
    assert result_blocks[0]["content"] == '"echo: hello"'


def test_agent_max_iterations_caps_loop(tmp_path):
    """When LLM always returns tool_use, loop stops after max_iterations."""
    registry = ToolRegistry()
    call_count = [0]

    def counter_handler(inp: dict) -> str:
        call_count[0] += 1
        return f"call {call_count[0]}"

    registry.register(ToolDefinition(
        name="counter",
        description="Count calls",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=counter_handler,
    ))

    max_iters = 3
    agent = _build_minimal_agent(
        tmp_path, "max-iter-agent", registry=registry, max_tool_iterations=max_iters
    )

    fake_anthropic = MagicMock()

    # Always return tool_use
    def always_tool_use(*args, **kwargs):
        return _make_anthropic_tool_use_response(
            tool_name="counter", tool_input={}, tool_id=f"tu_{call_count[0]:03d}"
        )

    fake_anthropic.Anthropic.return_value.messages.create.side_effect = always_tool_use

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Keep calling the counter.")

    # Should have stopped at max_iterations
    assert fake_anthropic.Anthropic.return_value.messages.create.call_count == max_iters
    assert response.tool_iterations == max_iters
    assert response.tool_iterations_maxed is True
    # Tool was called max_iters times
    assert call_count[0] == max_iters


def test_agent_cost_cap_breaks_tool_loop(tmp_path):
    """When cost cap is hit mid-loop, agent returns with skipped=True."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="cheap_tool",
        description="Does nothing expensive.",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda inp: "ok",
    ))

    agent = _build_minimal_agent(tmp_path, "cap-agent", registry=registry)
    # Enable tight cost guardrails: $0.00001 daily cap
    from atomic_agents.types import AgentConfig
    from pathlib import Path
    agent.config = AgentConfig(
        default_model="claude-haiku-4-5-20251001",
        fallback_model=None,
        cost_guardrails_enabled=True,
        daily_cap_usd=0.00001,  # extremely tight
        monthly_cap_usd=1.0,
        daily_cap_action="skip",
    )

    fake_anthropic = MagicMock()
    # First response: tool_use (iteration 1 — succeeds before cap check)
    response_1 = _make_anthropic_tool_use_response(
        tool_name="cheap_tool", tool_input={}, tool_id="tu_001"
    )
    fake_anthropic.Anthropic.return_value.messages.create.return_value = response_1

    # Simulate that after iteration 1, the daily cost has been pushed over cap
    # by patching _costs.sum_cost_for_period
    call_seq = [0]

    def fake_sum_cost(log_dir, period, *args, **kwargs):
        call_seq[0] += 1
        # Return over-cap after the first guardrail check (which is the pre-check
        # for iteration 2)
        if call_seq[0] > 2:  # first 2 calls are the initial check
            return 1.0  # over the $0.00001 cap
        return 0.0

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent._costs.sum_cost_for_period", side_effect=fake_sum_cost):
            with patch("atomic_agents.agent.AgentLock") as mock_lock:
                mock_lock.return_value.acquire.return_value = None
                mock_lock.return_value.release.return_value = None
                response = agent.call("Run until cap.")

    # Should have stopped early (first LLM call got through, then cap hit)
    assert response.skipped is True
    assert "cost cap hit" in response.skip_reason


def test_agent_atomic_capture_still_works_alongside_custom_tools(tmp_path):
    """atomic_capture continues to work via existing capture path alongside custom tools."""
    registry = ToolRegistry()
    registry.register(_make_simple_tool("side_tool"))

    agent = _build_minimal_agent(tmp_path, "compat-agent", registry=registry)

    # Build a capture block in the response text (Path 2 fenced block)
    capture_text = '''Here is the result.

```atomic_capture
{
  "type": "feedback",
  "name": "Test capture via compat test",
  "description": "Ensures atomic_capture coexists with custom tools",
  "confidence": "high",
  "sources": ["test_run_001"],
  "body": "Custom tools + atomic_capture work together."
}
```
'''

    fake_anthropic = MagicMock()
    # Single text response with embedded capture (no tool_use — atomic_capture via Path 2)
    response_1 = _make_anthropic_text_response(capture_text)
    fake_anthropic.Anthropic.return_value.messages.create.return_value = response_1

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Please remember something.")

    # Capture should have been extracted
    assert len(response.captures) == 1
    assert response.captures[0].name == "Test capture via compat test"
    # No custom tool calls
    assert response.tool_calls == []
    assert response.tool_iterations == 1


def test_response_includes_tool_calls_field(tmp_path):
    """Response dataclass has tool_calls, tool_iterations, tool_iterations_maxed fields."""
    registry = ToolRegistry()
    registry.register(_make_simple_tool("ping"))
    agent = _build_minimal_agent(tmp_path, "fields-agent", registry=registry)

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value.messages.create.return_value = (
        _make_anthropic_text_response("Hello.")
    )

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Ping.")

    # Fields exist and have correct defaults for a no-tool-call run
    assert hasattr(response, "tool_calls")
    assert hasattr(response, "tool_iterations")
    assert hasattr(response, "tool_iterations_maxed")
    assert response.tool_calls == []
    assert response.tool_iterations == 1
    assert response.tool_iterations_maxed is False


def test_run_log_records_tool_calls_rollup(tmp_path):
    """When tools are called, run log record includes tool_calls summary."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="db_query",
        description="Query database.",
        input_schema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        handler=lambda inp: f"result for: {inp['q']}",
    ))

    agent = _build_minimal_agent(tmp_path, "log-agent", registry=registry)

    fake_anthropic = MagicMock()
    resp1 = _make_anthropic_tool_use_response(
        tool_name="db_query", tool_input={"q": "SELECT 1"}, tool_id="tu_log_01"
    )
    resp2 = _make_anthropic_text_response("Got result.")
    fake_anthropic.Anthropic.return_value.messages.create.side_effect = [resp1, resp2]

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with patch("atomic_agents.agent.AgentLock") as mock_lock:
            mock_lock.return_value.acquire.return_value = None
            mock_lock.return_value.release.return_value = None
            response = agent.call("Run a query.")

    # Find the run log record (not the tool_call log line)
    import json as _json
    from datetime import date

    log_dir = tmp_path / "log-agent" / "log"
    today = date.today()
    log_file = log_dir / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    assert log_file.exists()

    records = [_json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    # The final run record should have tool_calls
    run_records = [r for r in records if r.get("trigger") == "manual"]
    assert len(run_records) == 1
    run_rec = run_records[0]
    assert "tool_calls" in run_rec
    assert run_rec["tool_calls"][0]["tool_name"] == "db_query"
    assert run_rec["tool_iterations"] == 2


def test_agent_with_no_tools_registry_defaults(tmp_path):
    """Agent without tools= arg has an empty ToolRegistry (backwards compat)."""
    agent = _build_minimal_agent(tmp_path, "no-tools-agent", registry=None)
    assert isinstance(agent.tool_registry, ToolRegistry)
    assert len(agent.tool_registry) == 0


def test_max_tool_iterations_clamped(tmp_path):
    """max_tool_iterations is clamped to [1, MAX_TOOL_ITERATIONS]."""
    agent_low = _build_minimal_agent(tmp_path, "low-agent", max_tool_iterations=0)
    assert agent_low.max_tool_iterations == 1

    agent_high = _build_minimal_agent(tmp_path, "high-agent", max_tool_iterations=9999)
    assert agent_high.max_tool_iterations == MAX_TOOL_ITERATIONS


# ──────────────────────────────────────────────────────────────────
# New regression tests — codex MCP review findings (M3, M5)

# M5 — collision detection
def test_tool_registry_register_raises_on_collision():
    """Registering the same tool name twice raises ToolNameCollision by default."""
    from atomic_agents.exceptions import ToolNameCollision

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="foo_tool", description="v1", input_schema={}, handler=lambda _: "v1"
    ))
    with pytest.raises(ToolNameCollision, match="foo_tool"):
        registry.register(ToolDefinition(
            name="foo_tool", description="v2", input_schema={}, handler=lambda _: "v2"
        ))
    # Original registration should still be intact
    assert registry.get("foo_tool").description == "v1"


def test_tool_registry_register_allow_overwrite_replaces():
    """allow_overwrite=True silently replaces the existing registration."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="foo_tool", description="v1", input_schema={}, handler=lambda _: "v1"
    ))
    registry.register(ToolDefinition(
        name="foo_tool", description="v2", input_schema={}, handler=lambda _: "v2"
    ), allow_overwrite=True)
    assert registry.get("foo_tool").description == "v2"
    assert len(registry) == 1


# M3 — ToolRegistry.unregister
def test_tool_registry_unregister_removes_tool():
    """unregister() removes a registered tool and returns True."""
    registry = ToolRegistry()
    registry.register(_make_simple_tool("my_tool"))
    assert registry.get("my_tool") is not None

    result = registry.unregister("my_tool")
    assert result is True
    assert registry.get("my_tool") is None
    assert "my_tool" not in registry.list_names()


def test_tool_registry_unregister_missing_returns_false():
    """unregister() on a non-existent tool returns False (idempotent)."""
    registry = ToolRegistry()
    result = registry.unregister("nonexistent")
    assert result is False


def test_tool_registry_unregister_twice_is_idempotent():
    """Calling unregister() twice on the same name does not raise."""
    registry = ToolRegistry()
    registry.register(_make_simple_tool("tool_to_remove"))
    registry.unregister("tool_to_remove")
    result = registry.unregister("tool_to_remove")  # second call
    assert result is False

"""End-to-end regression tests for #87 PR 3's preferred_provider threading.

Pins the contract: ``model.md provider:`` → ``AgentConfig.provider`` →
``_llm.call_llm(preferred_provider=...)`` → ``find_backend_for_model(
preferred_provider=...)``. Both the main LLM call AND the tool-loop
continuation must thread the preference; otherwise iteration 2 of a
multi-turn loop crashes with ``AmbiguousBackendError`` mid-flight when
two backends claim the same model id.

Opus subagent caught the missing tool-loop threading during PR 3 review
(Finding 1). The bug was latent today (no two registered backends
overlap claims) but live the moment a third-party backend registers.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.llm import (
    _registry,
    find_backend_for_model,
    register_llm_backend,
    unregister_llm_backend,
)
from atomic_agents.llm import _RawLLMResponse
from atomic_agents.llm.types import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)


# ──────────────────────────────────────────────────────────────────


class _ProvenanceBackend:
    """Backend that records which provider_id handled each call.

    Used to assert that the operator's ``provider:`` preference is
    honored across BOTH the main LLM dispatch AND the tool-loop
    continuation — i.e., the preference threads all the way through
    agent.py.
    """

    def __init__(self, provider_id: str, model_prefix: str, record: list) -> None:
        self._provider_id = provider_id
        self._prefix = model_prefix
        self._record = record

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def supports_model(self, model_id: str) -> bool:
        return model_id.startswith(self._prefix)

    def capabilities(self, model_id: str) -> LLMCapabilities:
        return LLMCapabilities(
            tools=True, tool_results=True, cache_control=False, streaming=False,
            vision=False, max_input_tokens=128_000, max_output_tokens=4_096,
            usage_reporting=True, structured_output=False,
        )

    def pricing(self, model_id: str) -> PricingInfo | None:
        return None

    def count_tokens(self, system_prompt, messages, tools=None) -> int:
        return 1

    def call(self, model, system_prompt, messages, max_tokens, temperature,
             tools=None, cache_directives=None) -> _RawLLMResponse:
        self._record.append(("call", self._provider_id, model))
        return _RawLLMResponse(text="ok", input_tokens=1, output_tokens=1)

    def format_tool_results(self, tool_uses, tool_results, assistant_text="") -> list[dict]:
        self._record.append(("format_tool_results", self._provider_id))
        return [{"role": "user", "content": "stub continuation"}]


@pytest.fixture(autouse=True)
def _stub_api_keys(monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    monkeypatch.setenv("ATOMIC_AGENTS_OPENAI_KEY", "fake-key")


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Save/restore the registry state around each test in this file —
    the tests below register fake backends that overlap claims, which
    would corrupt the lazy-init defaults if not cleaned up.
    """
    from atomic_agents import llm as _llm_pkg
    saved = dict(_llm_pkg._registry)
    saved_flag = _llm_pkg._DEFAULTS_REGISTERED
    _llm_pkg._registry.clear()
    _llm_pkg._DEFAULTS_REGISTERED = True
    yield
    _llm_pkg._registry.clear()
    _llm_pkg._registry.update(saved)
    _llm_pkg._DEFAULTS_REGISTERED = saved_flag


# ──────────────────────────────────────────────────────────────────


def test_find_backend_with_preferred_provider_resolves_correctly():
    """Sanity check of the registry primitive: two backends claim
    ``gpt-5``; ``preferred_provider="azure-openai"`` wins.
    """
    record: list = []
    register_llm_backend(_ProvenanceBackend("openai", "gpt-", record))
    register_llm_backend(_ProvenanceBackend("azure-openai", "gpt-", record))
    b = find_backend_for_model("gpt-5", preferred_provider="azure-openai")
    assert b.provider_id == "azure-openai"


def _build_minimal_agent(tmp_path, name, model_text):
    """Create a minimal agent dir with custom model.md content."""
    agent_dir = tmp_path / name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTestAgent.")
    (agent_dir / "tools.md").write_text(
        f"## Read paths\n- ~/docs/\n\n## Write paths\n- {agent_dir}/\n"
    )
    (agent_dir / "model.md").write_text(model_text)
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return AtomicAgent(name=name, agents_root=tmp_path)


def test_agent_provider_field_threads_to_call_llm_main_loop(tmp_path):
    """When two backends both claim ``gpt-5`` and ``model.md`` sets
    ``provider: azure-openai``, the first ``call_llm`` lands on the
    azure-openai backend (the openai one would also match but the
    preference disambiguates).
    """
    record: list = []
    register_llm_backend(_ProvenanceBackend("openai", "gpt-", record))
    register_llm_backend(_ProvenanceBackend("azure-openai", "gpt-", record))

    agent = _build_minimal_agent(
        tmp_path, "azure-pinned",
        model_text="## Default model\n\ngpt-5\n\nprovider: azure-openai\n",
    )
    assert agent.config.provider == "azure-openai"

    with patch.object(agent, "lock_backend") as mock_lock:
        mock_lock.acquire.return_value = MagicMock()  # fake LockHandle (#60 PR 2)
        mock_lock.release.return_value = None
        agent.call("test")

    # First (and only) call resolves to azure-openai, not openai
    calls = [r for r in record if r[0] == "call"]
    assert len(calls) == 1
    assert calls[0][1] == "azure-openai"


def test_agent_provider_field_threads_to_tool_loop_continuation(tmp_path):
    """Regression test for #87 PR 3 review Finding 1.

    The original PR threaded ``preferred_provider`` to ``_llm.call_llm``
    (so iteration 1 worked) but NOT to ``find_backend_for_model`` inside
    ``_build_tool_loop_messages``. Iteration 2 of a multi-turn tool
    loop then raised ``AmbiguousBackendError`` mid-flight.

    This test registers two backends overlapping on ``gpt-5``, sets
    ``provider: azure-openai``, drives a two-iteration tool loop, and
    asserts both ``call`` AND ``format_tool_results`` are dispatched to
    the same (preferred) backend.
    """
    record: list = []
    # Backend behavior: first call returns a tool_use (drives loop into
    # iteration 2); second call returns plain text (loop terminates).
    # `format_tool_results` returns a synthetic continuation that the
    # second call_llm consumes.
    call_count = [0]

    class _TwoIterBackend(_ProvenanceBackend):
        def call(self, model, system_prompt, messages, max_tokens, temperature,
                 tools=None, cache_directives=None) -> _RawLLMResponse:
            self._record.append(("call", self._provider_id, model))
            call_count[0] += 1
            if call_count[0] == 1:
                return _RawLLMResponse(
                    text="invoking tool",
                    input_tokens=1, output_tokens=1,
                    tool_uses=[{"id": "tc_1", "name": "atomic_capture",
                                "input": {"type": "feedback", "name": "x", "body": "y"}}],
                )
            return _RawLLMResponse(text="done", input_tokens=1, output_tokens=1)

    # Use a custom tool (not atomic_capture) so the tool loop actually
    # iterates — atomic_capture results route through the capture path,
    # not _build_tool_loop_messages.
    class _TwoIterToolBackend(_ProvenanceBackend):
        def call(self, model, system_prompt, messages, max_tokens, temperature,
                 tools=None, cache_directives=None) -> _RawLLMResponse:
            self._record.append(("call", self._provider_id, model))
            call_count[0] += 1
            if call_count[0] == 1:
                return _RawLLMResponse(
                    text="",
                    input_tokens=1, output_tokens=1,
                    tool_uses=[{"id": "tc_1", "name": "echo",
                                "input": {"msg": "hello"}}],
                )
            return _RawLLMResponse(text="done", input_tokens=1, output_tokens=1)

    register_llm_backend(_TwoIterToolBackend("openai", "gpt-", record))
    register_llm_backend(_TwoIterToolBackend("azure-openai", "gpt-", record))

    # Register a custom tool the loop can dispatch on
    from atomic_agents.tools import ToolDefinition, ToolRegistry
    tool_registry = ToolRegistry()
    tool_registry.register(ToolDefinition(
        name="echo",
        description="Echo back",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        handler=lambda inp: f"echo: {inp['msg']}",
    ))

    agent_dir = tmp_path / "loop-azure"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTestAgent.")
    (agent_dir / "tools.md").write_text(
        f"## Read paths\n- ~/docs/\n\n## Write paths\n- {agent_dir}/\n"
    )
    (agent_dir / "model.md").write_text(
        "## Default model\n\ngpt-5\n\nprovider: azure-openai\n"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    agent = AtomicAgent(name="loop-azure", agents_root=tmp_path, tools=tool_registry)

    with patch.object(agent, "lock_backend") as mock_lock:
        mock_lock.acquire.return_value = MagicMock()  # fake LockHandle (#60 PR 2)
        mock_lock.release.return_value = None
        # Pre-PR-3-Finding-1-fix: this raised AmbiguousBackendError.
        # Post-fix: completes cleanly with both calls + the loop
        # continuation routed to azure-openai.
        agent.call("test")

    calls = [r for r in record if r[0] == "call"]
    fmt_calls = [r for r in record if r[0] == "format_tool_results"]
    # Both LLM calls AND the tool-loop continuation went to azure-openai
    assert all(c[1] == "azure-openai" for c in calls)
    assert all(c[1] == "azure-openai" for c in fmt_calls)
    # And: format_tool_results was called exactly once (one tool-loop
    # iteration's worth of continuation)
    assert len(fmt_calls) == 1


def test_hybrid_tool_def_dict_resolves_openai_branch():
    """Per Opus subagent review Finding 6: a dict with BOTH ``type:
    "function"`` AND a top-level ``name`` triggers the OpenAI branch (the
    more specific shape wins). Pin this so future refactors of
    ``_to_canonical_tool_defs`` don't silently invert the precedence.
    """
    from atomic_agents._llm import _to_canonical_tool_defs

    hybrid = [{
        "type": "function",
        "name": "outer_anthropic_style_name",  # decoy
        "function": {
            "name": "inner_openai_style_name",  # this one wins
            "description": "search",
            "parameters": {"type": "object"},
        },
    }]
    canonical = _to_canonical_tool_defs(hybrid)
    assert len(canonical) == 1
    assert canonical[0].name == "inner_openai_style_name"

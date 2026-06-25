"""Conftest for advisor test suite — no-LLM-spend enforcement guard (spec/53 MUST 10).

Any LLMBackend construction during advisor tests raises RuntimeError. The
advisor's load-bearing guarantee is no LLM is ever CONSTRUCTED and no LLM CALL
is ever made on any advisor code path — NOT that the eval/agent/dream module
objects are absent from sys.modules (importing any atomic_agents submodule runs
the package __init__, which eagerly imports agent.py — see spec/53 §2 NOTE).
This guard is construction-time, so it catches an accidental path that would
build an LLMBackend; it does not (and cannot) prevent module loading.

The guard patches every concrete LLMBackend.__init__ that has its own __init__
and is registered in atomic_agents/llm/__init__.py. MoonshotLLMBackend is a
factory over OpenAICompatibleLLMBackend and is covered transitively. If a new
concrete backend with its own __init__ is added to atomic_agents/llm/, add it
to llm_backends below.
"""

from __future__ import annotations

import importlib

import pytest


def _llm_guard(*args, **kwargs):
    raise RuntimeError(
        "LLMBackend must not be constructed in advisor tests (spec/53 MUST 10). "
        "The advisor is a pure-compute module — no LLM spend allowed. "
        "Check for an accidental import of agent.py, eval.py, or tuning.py."
    )


# Use a session-scoped fixture with a manually-managed MonkeyPatch so we can
# match module scope without bumping into the function-scoped monkeypatch limit.
@pytest.fixture(autouse=True, scope="module")
def no_llm_in_advisor():
    """Module-scoped autouse fixture: fail if any LLMBackend is instantiated."""
    mp = pytest.MonkeyPatch()

    llm_backends = [
        ("atomic_agents.llm.anthropic", "AnthropicLLMBackend"),
        ("atomic_agents.llm.openai_compat", "OpenAICompatibleLLMBackend"),
        ("atomic_agents.llm.vertex_gemini", "VertexGeminiLLMBackend"),
    ]
    for module_path, cls_name in llm_backends:
        try:
            mod = importlib.import_module(module_path)
            if hasattr(mod, cls_name):
                mp.setattr(getattr(mod, cls_name), "__init__", _llm_guard)
        except ImportError:
            pass

    yield

    mp.undo()

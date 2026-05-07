"""Tests for helper provenance preservation per spec/10 Wave 8.

Covers:
- helper_call accepts a `sources` parameter
- system prompt builder includes citation instructions when sources are given
- HelperResult echoes `sources` and reports `provenance_preserved`
- log record includes sources + provenance flag
- detector heuristic: bracketed citations, inline phrases, source-name mention
- helper_call_parallel accepts shared `sources` or per-prompt `sources_per_prompt`
"""

from __future__ import annotations
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents import _llm
from atomic_agents.agent import AtomicAgent
from atomic_agents.types import HelperResult


# ──────────────────────────────────────────────────────────────────
# Fixtures: build a minimal agent on disk so AtomicAgent loads


def _build_minimal_agent(tmp_path: Path) -> Path:
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "tester"
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nTester.")
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return agents_root


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    agents_root = _build_minimal_agent(tmp_path)
    return AtomicAgent(name="tester", agents_root=agents_root)


def _make_anthropic_resp(text: str, *, input_tokens=10, output_tokens=20):
    """Mock Anthropic SDK response shape."""
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return types.SimpleNamespace(content=[block], usage=usage)


# ──────────────────────────────────────────────────────────────────
# System prompt builder


def test_build_helper_system_prompt_empty_when_no_sources(agent):
    assert agent._build_helper_system_prompt([]) == ""


def test_build_helper_system_prompt_includes_provenance_instruction(agent):
    sp = agent._build_helper_system_prompt(["~/docs/cpa-memo.md"])
    assert "cite the location" in sp
    assert "~/docs/cpa-memo.md" in sp
    assert "Sources you are working from:" in sp


def test_build_helper_system_prompt_lists_each_source(agent):
    sources = ["~/docs/a.md", "~/docs/b.md", "~/docs/c.md"]
    sp = agent._build_helper_system_prompt(sources)
    for s in sources:
        assert s in sp
    # Each source on its own bullet line
    assert sp.count("- ~/docs/") == 3


# ──────────────────────────────────────────────────────────────────
# Provenance detector heuristic


def test_detect_provenance_returns_true_when_no_sources(agent):
    """Nothing to preserve — vacuously true."""
    assert agent._detect_provenance("plain prose", []) is True


def test_detect_provenance_false_when_text_empty_with_sources(agent):
    assert agent._detect_provenance("", ["src.md"]) is False
    assert agent._detect_provenance("   ", ["src.md"]) is False


def test_detect_provenance_bracketed_section_citation(agent):
    text = "Q3 timing flagged for review [§2, p3]."
    assert agent._detect_provenance(text, ["~/docs/cpa-memo.md"]) is True


def test_detect_provenance_bracketed_page_citation(agent):
    text = "Federal bracket changes minimal [page 5]."
    assert agent._detect_provenance(text, ["~/docs/cpa-memo.md"]) is True


def test_detect_provenance_paragraph_citation(agent):
    text = "Schedule unchanged [paragraph 2]."
    assert agent._detect_provenance(text, ["src.md"]) is True


def test_detect_provenance_inline_according_to_phrase(agent):
    text = "According to the memo, the schedule is unchanged."
    assert agent._detect_provenance(text, ["~/docs/memo.md"]) is True


def test_detect_provenance_section_symbol_inline(agent):
    text = "The recommendation in §3 is to maintain quarterly schedule."
    assert agent._detect_provenance(text, ["doc.md"]) is True


def test_detect_provenance_source_basename_mention(agent):
    """Output explicitly names the source — counts as attribution."""
    text = "The cpa-memo recommends quarterly payments."
    assert agent._detect_provenance(text, ["~/docs/finance/cpa-memo.md"]) is True


def test_detect_provenance_returns_false_for_uncited_prose(agent):
    """No brackets, no inline markers, no source names → False."""
    text = "Quarterly tax payments should continue as planned. The federal brackets are stable."
    assert agent._detect_provenance(text, ["~/docs/cpa-memo.md"]) is False


def test_detect_provenance_short_source_basename_not_false_positive(agent):
    """A 2-char source stem shouldn't false-positive on common letter pairs."""
    text = "An ordinary sentence with no citation markers at all."
    assert agent._detect_provenance(text, ["a.md", "ab.md"]) is False


# ──────────────────────────────────────────────────────────────────
# helper_call integration


def test_helper_call_without_sources_unchanged_behavior(agent):
    """Backwards compat — calling without sources behaves like v0.1."""
    resp = _make_anthropic_resp("Plain helper output.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = agent.helper_call(prompt="test", summary="t1")

    assert isinstance(result, HelperResult)
    assert result.text == "Plain helper output."
    assert result.sources == []
    assert result.provenance_preserved is True  # vacuously, no sources

    # Verify system_prompt was empty (no sources passed)
    _, call_kwargs = fake_client.messages.create.call_args
    assert call_kwargs["system"] == [{"type": "text", "text": ""}]


def test_helper_call_with_sources_passes_provenance_prompt(agent):
    """sources= triggers the provenance system prompt."""
    resp = _make_anthropic_resp("Q3 timing [§2, p3]. Brackets stable [§3].")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = agent.helper_call(
            prompt="Summarize this memo",
            sources=["~/docs/cpa-memo.md"],
        )

    assert result.sources == ["~/docs/cpa-memo.md"]
    assert result.provenance_preserved is True

    _, call_kwargs = fake_client.messages.create.call_args
    sys_text = call_kwargs["system"][0]["text"]
    assert "cite the location" in sys_text
    assert "~/docs/cpa-memo.md" in sys_text


def test_helper_call_logs_sources_and_provenance(agent):
    """Run record JSONL contains sources and provenance_preserved fields."""
    resp = _make_anthropic_resp("Plain prose with no citation.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        agent.helper_call(
            prompt="Summarize",
            sources=["~/docs/memo.md"],
            summary="summarize memo",
        )

    # Find today's log
    from datetime import date
    today = date.today()
    log_path = (
        agent.agent_root / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    lines = log_path.read_text().strip().splitlines()
    helper_records = [json.loads(L) for L in lines if json.loads(L).get("trigger") == "helper"]
    assert len(helper_records) == 1
    rec = helper_records[0]
    assert rec["sources"] == ["~/docs/memo.md"]
    assert rec["provenance_preserved"] is False  # output was plain prose
    assert rec["parent_agent"] == "tester"


def test_helper_call_no_sources_no_extra_log_fields(agent):
    """Backwards compat: log shape unchanged when sources isn't passed."""
    resp = _make_anthropic_resp("Plain output.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        agent.helper_call(prompt="test", summary="x")

    from datetime import date
    today = date.today()
    log_path = (
        agent.agent_root / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    rec = json.loads(log_path.read_text().strip().splitlines()[0])
    assert "sources" not in rec
    assert "provenance_preserved" not in rec


def test_helper_call_provenance_loss_flagged(agent):
    """Sources passed but output has no citation → provenance_preserved=False."""
    resp = _make_anthropic_resp("The recommendation is to wait until next quarter.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = agent.helper_call(
            prompt="x",
            sources=["~/docs/cpa-memo.md"],
        )
    assert result.provenance_preserved is False


# ──────────────────────────────────────────────────────────────────
# helper_call_parallel


def test_helper_call_parallel_shared_sources_apply_to_each(agent):
    """sources= broadcasts to every prompt in the batch."""
    resp = _make_anthropic_resp("Per memo [§1].")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        results = agent.helper_call_parallel(
            prompts=["Summarize", "Extract risks", "List action items"],
            sources=["~/docs/memo.md"],
            max_concurrent=1,
        )

    assert len(results) == 3
    for r in results:
        assert r.sources == ["~/docs/memo.md"]
        assert r.provenance_preserved is True


def test_helper_call_parallel_per_prompt_sources(agent):
    """sources_per_prompt= aligns 1:1 with prompts (different docs per call)."""
    resp = _make_anthropic_resp("According to the doc, X.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        results = agent.helper_call_parallel(
            prompts=["sum 1", "sum 2", "sum 3"],
            sources_per_prompt=[
                ["~/docs/a.md"],
                ["~/docs/b.md"],
                ["~/docs/c.md"],
            ],
            max_concurrent=1,
        )

    assert results[0].sources == ["~/docs/a.md"]
    assert results[1].sources == ["~/docs/b.md"]
    assert results[2].sources == ["~/docs/c.md"]


def test_helper_call_parallel_rejects_both_sources_args(agent):
    """sources and sources_per_prompt are mutually exclusive."""
    with pytest.raises(ValueError, match="not both"):
        agent.helper_call_parallel(
            prompts=["a", "b"],
            sources=["~/docs/x.md"],
            sources_per_prompt=[["~/docs/y.md"], ["~/docs/z.md"]],
        )


def test_helper_call_parallel_rejects_misaligned_sources_per_prompt(agent):
    """sources_per_prompt must have same length as prompts."""
    with pytest.raises(ValueError, match="expected"):
        agent.helper_call_parallel(
            prompts=["a", "b", "c"],
            sources_per_prompt=[["~/docs/y.md"]],  # length 1 != 3
        )


def test_helper_call_parallel_no_sources_unchanged_behavior(agent):
    """Calling without any sources still works (backwards compat)."""
    resp = _make_anthropic_resp("ok")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        results = agent.helper_call_parallel(
            prompts=["a", "b"],
            max_concurrent=1,
        )

    assert len(results) == 2
    for r in results:
        assert r.sources == []
        assert r.provenance_preserved is True

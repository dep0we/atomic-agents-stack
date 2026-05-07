"""Tests for atomic_agents.eval."""

from __future__ import annotations
import json
import os
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import frontmatter
import pytest

from atomic_agents.eval import (
    EvalRunner,
    EvalTest,
    EvalResult,
    _extract_sections,
    _provider_available,
)
from atomic_agents.exceptions import (
    AtomicAgentsError,
    NoJudgeAvailable,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures — set up a minimal agent vault with rubric + judge + golden tests

@pytest.fixture
def agent_vault(tmp_path):
    """Build a minimal agent folder with persona, tools, model, evals."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "testagent"

    # Persona
    persona = agent_root / "persona"
    persona.mkdir(parents=True)
    (persona / "IDENTITY.md").write_text("# IDENTITY\n\nI am the test agent.")
    (persona / "SOUL.md").write_text("# SOUL\n\nDirect.")
    (persona / "USER.md").write_text("# USER\n\nThe operator likes brevity.")

    # tools.md
    (agent_root / "tools.md").write_text(
        "# TOOLS\n\n## Read paths\n- " + str(agent_root) + "\n\n## Write paths\n- " + str(agent_root) + "\n"
    )

    # model.md
    (agent_root / "model.md").write_text(
        "# MODEL\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
        "## Fallback\n\nclaude-haiku-4-5-20251001\n"
    )

    # evals/rubric.md
    evals = agent_root / "evals"
    evals.mkdir()
    rubric_text = """---
schema_version: 1
agent: testagent
weights:
  persona_fidelity: 50
  output_quality: 50
threshold_pass: 4.0
---

# Test rubric

## persona_fidelity (50%)
- 5 = perfect
- 1 = bad

## output_quality (50%)
- 5 = correct
- 1 = wrong

## Hard fails
- HF1 — leaks secrets
"""
    (evals / "rubric.md").write_text(rubric_text)

    # evals/judge.md
    judge_text = """---
schema_version: 1
agent: testagent
recommended_judge:
  cross_family:
    - gpt-5
  same_family_fallback:
    - claude-haiku-4-5-20251001
strict_mode: true
audit_sample_pct: 0.10
---

You are a strict judge. Output ONLY valid JSON.

Rubric:
{rubric}

Test input:
{test_input}

Expected:
{expected_behavior}

Pass criteria:
{pass_criteria}

Agent response:
{agent_response}

Trajectory: {trajectory}

Score now in JSON format with keys per rubric dimension.
"""
    (evals / "judge.md").write_text(judge_text)

    # evals/golden/happy/001_test.md
    golden = evals / "golden" / "happy"
    golden.mkdir(parents=True)
    test_text = """---
schema_version: 1
agent: testagent
category: happy
test_id: 001_basic_test
created: 2026-05-06
---

# A basic test

## Setup

Standard runtime load.

## Input

What is 2+2?

## Expected behavior

Should answer 4.

## Pass criteria

- persona_fidelity ≥ 4
- output_quality ≥ 4

## Notes

Test note.
"""
    (golden / "001_basic_test.md").write_text(test_text)

    return agents_root, "testagent"


# ──────────────────────────────────────────────────────────────────
# Discovery + parsing tests

def test_extract_sections_basic():
    body = """## Section A
Content A line 1
Content A line 2

## Section B
Content B
"""
    sections = _extract_sections(body)
    assert "Section A" in sections
    assert "Section B" in sections
    assert "Content A line 1" in sections["Section A"]
    assert "Content B" in sections["Section B"]


def test_extract_sections_handles_no_sections():
    sections = _extract_sections("No sections here, just prose.")
    assert sections == {}


def test_eval_runner_loads_rubric_and_judge(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    assert runner.weights == {"persona_fidelity": 50.0, "output_quality": 50.0}
    assert runner.threshold_pass == 4.0
    assert "gpt-5" in runner.judge_cross_family
    assert "claude-haiku-4-5-20251001" in runner.judge_same_family_fallback
    assert runner.strict_mode is True


def test_eval_runner_missing_evals_dir_raises(tmp_path):
    agents_root = tmp_path / "agents"
    (agents_root / "noevals").mkdir(parents=True)
    with pytest.raises(AtomicAgentsError, match="evals/"):
        EvalRunner(agents_root, "noevals")


def test_eval_runner_missing_rubric_raises(tmp_path):
    agents_root = tmp_path / "agents"
    evals = agents_root / "myagent" / "evals"
    evals.mkdir(parents=True)
    with pytest.raises(AtomicAgentsError, match="Rubric not found"):
        EvalRunner(agents_root, "myagent")


def test_discover_tests_all(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    tests = runner.discover_tests()
    assert len(tests) == 1
    assert tests[0].test_id == "001_basic_test"
    assert tests[0].category == "happy"


def test_discover_tests_by_category(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    happy = runner.discover_tests(category="happy")
    assert len(happy) == 1
    edge = runner.discover_tests(category="edge")
    assert len(edge) == 0


def test_discover_tests_by_id(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    matched = runner.discover_tests(test_id="001_basic")
    assert len(matched) == 1
    assert matched[0].test_id == "001_basic_test"


def test_test_parsing_extracts_sections(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    tests = runner.discover_tests()
    t = tests[0]
    assert "What is 2+2" in t.input
    assert "Should answer 4" in t.expected_behavior
    assert "persona_fidelity" in t.pass_criteria


# ──────────────────────────────────────────────────────────────────
# Judge selection

def test_pick_judge_avoids_self_judge(agent_vault, monkeypatch):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    # Pretend Anthropic + OpenAI are both available
    monkeypatch.setattr(
        "atomic_agents.eval._provider_available", lambda m: True
    )

    # Agent uses claude-sonnet — judge should pick gpt-5 (cross-family)
    judge = runner.pick_judge_model("claude-sonnet-4-6-20260101")
    assert judge == "gpt-5"


def test_pick_judge_falls_back_when_cross_family_unavailable(agent_vault, monkeypatch):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    # Pretend only claude is available
    def fake_avail(m):
        return m.startswith("claude-")
    monkeypatch.setattr("atomic_agents.eval._provider_available", fake_avail)

    # Cross-family (gpt-5) unavailable; falls back to same-family haiku
    judge = runner.pick_judge_model("claude-sonnet-4-6-20260101")
    assert judge == "claude-haiku-4-5-20251001"


def test_pick_judge_raises_when_nothing_available(agent_vault, monkeypatch):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    monkeypatch.setattr("atomic_agents.eval._provider_available", lambda m: False)
    with pytest.raises(NoJudgeAvailable):
        runner.pick_judge_model("claude-sonnet-4-6-20260101")


# ──────────────────────────────────────────────────────────────────
# Score parsing + verdict computation

def test_parse_judge_response_valid_json(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    text = '{"persona_fidelity": {"score": 5, "justification": "perfect"}}'
    parsed = runner._parse_judge_response(text)
    assert parsed["persona_fidelity"]["score"] == 5


def test_parse_judge_response_strips_code_fence(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    text = '```json\n{"score": 4}\n```'
    parsed = runner._parse_judge_response(text)
    assert parsed["score"] == 4


def test_parse_judge_response_raises_on_invalid_json(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    with pytest.raises(json.JSONDecodeError):
        runner._parse_judge_response("not json at all")


def test_compute_weighted_score_basic(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    scores = {
        "persona_fidelity": {"score": 5, "justification": "x"},
        "output_quality": {"score": 4, "justification": "y"},
    }
    # 50% × 5 + 50% × 4 = 4.5
    weighted = runner._compute_weighted_score(scores)
    assert weighted == 4.5


def test_compute_weighted_score_handles_missing_dimensions(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    scores = {"persona_fidelity": {"score": 5}}  # output_quality missing
    # Only persona_fidelity contributes (weight 50%) → 5.0 (since weight_sum is also 50)
    weighted = runner._compute_weighted_score(scores)
    assert weighted == 5.0


def test_compute_weighted_score_zero_when_no_scores(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    assert runner._compute_weighted_score({}) == 0.0


# ──────────────────────────────────────────────────────────────────
# End-to-end test (mocked LLM calls)

def test_run_test_end_to_end_pass(agent_vault, monkeypatch):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    # Mock both LLM calls — agent and judge
    agent_response_mock = MagicMock()
    agent_response_mock.text = "The answer is 4."
    agent_response_mock.model = "claude-sonnet-4-6-20260101"
    agent_response_mock.input_tokens = 100
    agent_response_mock.output_tokens = 10
    agent_response_mock.cost_usd = 0.001
    agent_response_mock.skipped = False

    judge_response_mock = MagicMock()
    judge_response_mock.text = json.dumps({
        "persona_fidelity": {"score": 5, "justification": "stayed in character"},
        "output_quality": {"score": 5, "justification": "math correct"},
        "hard_fails": [],
        "overall": {"justification": "good run"},
    })
    judge_response_mock.input_tokens = 200
    judge_response_mock.output_tokens = 50

    with patch("atomic_agents.eval.AtomicAgent") as MockAgent:
        # AtomicAgent(...) returns instance whose .call() returns agent_response_mock
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_response_mock
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.eval._llm.call_llm", return_value=judge_response_mock):
            with patch("atomic_agents.eval._provider_available", return_value=True):
                result = runner.run_test("001_basic_test")

    assert result.verdict == "pass"
    assert result.weighted_score == 5.0
    assert result.hard_fails == []
    assert result.scores == {"persona_fidelity": 5, "output_quality": 5}


def test_run_test_hard_fail_overrides_score(agent_vault, monkeypatch):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    agent_resp = MagicMock(text="x", model="claude-sonnet-4-6-20260101",
                            input_tokens=100, output_tokens=10, cost_usd=0.001, skipped=False)
    judge_resp = MagicMock(
        text=json.dumps({
            "persona_fidelity": {"score": 5},
            "output_quality": {"score": 5},
            "hard_fails": ["HF1"],
            "overall": {"justification": "leaked secrets"},
        }),
        input_tokens=200, output_tokens=50,
    )

    with patch("atomic_agents.eval.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        MockAgent.return_value = mock_instance
        with patch("atomic_agents.eval._llm.call_llm", return_value=judge_resp):
            with patch("atomic_agents.eval._provider_available", return_value=True):
                result = runner.run_test("001_basic_test")

    # Despite scores=5/5, hard fail forces verdict to fail
    assert result.verdict == "fail"
    assert "HF1" in result.hard_fails
    assert result.weighted_score == 5.0  # weighted score still computed


def test_run_test_judge_malformed_json_retries_then_fails(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    agent_resp = MagicMock(text="x", model="claude-sonnet-4-6-20260101",
                            input_tokens=100, output_tokens=10, cost_usd=0.001, skipped=False)
    bad_judge = MagicMock(text="this is not JSON", input_tokens=200, output_tokens=10)

    with patch("atomic_agents.eval.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        MockAgent.return_value = mock_instance
        # Both judge calls return bad output
        with patch("atomic_agents.eval._llm.call_llm", return_value=bad_judge):
            with patch("atomic_agents.eval._provider_available", return_value=True):
                result = runner.run_test("001_basic_test")

    assert result.verdict == "judge_error"
    assert "malformed JSON" in result.overall_justification


def test_run_test_agent_skipped(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    skipped_resp = MagicMock(skipped=True, skip_reason="cost cap hit",
                              model="claude-sonnet-4-6-20260101", text="")

    with patch("atomic_agents.eval.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = skipped_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        MockAgent.return_value = mock_instance
        result = runner.run_test("001_basic_test")

    assert result.verdict == "judge_error"
    assert "skipped" in result.overall_justification.lower()


def test_run_suite_writes_jsonl_and_response(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)

    agent_resp = MagicMock(text="The answer is 4.", model="claude-sonnet-4-6-20260101",
                            input_tokens=100, output_tokens=10, cost_usd=0.001, skipped=False)
    judge_resp = MagicMock(
        text=json.dumps({
            "persona_fidelity": {"score": 5, "justification": "good"},
            "output_quality": {"score": 5, "justification": "right"},
            "hard_fails": [],
            "overall": {"justification": "pass"},
        }),
        input_tokens=200, output_tokens=50,
    )

    with patch("atomic_agents.eval.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        MockAgent.return_value = mock_instance
        with patch("atomic_agents.eval._llm.call_llm", return_value=judge_resp):
            with patch("atomic_agents.eval._provider_available", return_value=True):
                results = runner.run_suite()

    assert results.tests_run == 1
    assert results.tests_passed == 1
    assert results.avg_weighted_score == 5.0

    # Verify JSONL was written
    runs_dir = agents_root / agent_name / "evals" / "runs"
    log_files = list(runs_dir.glob("*.jsonl"))
    assert len(log_files) == 1
    line = log_files[0].read_text().strip()
    parsed = json.loads(line)
    assert parsed["test_id"] == "001_basic_test"
    assert parsed["verdict"] == "pass"

    # Verify response was written
    response_files = list((runs_dir / "responses").glob("*.txt"))
    assert len(response_files) == 1
    assert "The answer is 4" in response_files[0].read_text()


def test_run_suite_no_tests_returns_empty(agent_vault):
    agents_root, agent_name = agent_vault
    runner = EvalRunner(agents_root, agent_name)
    results = runner.run_suite(category="adversarial")  # no adversarial tests in fixture
    assert results.tests_run == 0
    assert results.tests_passed == 0


# ──────────────────────────────────────────────────────────────────
# Provider availability

def test_provider_available_env_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _provider_available("claude-opus-4-7-20260101") is True


def test_provider_available_unknown_model_returns_false():
    assert _provider_available("unknown-model-xyz") is False


def test_provider_available_no_key_returns_false(monkeypatch, tmp_path):
    # Ensure no env keys
    for v in ["ANTHROPIC_API_KEY", "ATOMIC_AGENTS_ANTHROPIC_KEY"]:
        monkeypatch.delenv(v, raising=False)
    # Point HOME at empty tmp so config file doesn't exist
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # On macOS, Keychain might still respond — patch out subprocess
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: MagicMock(stdout="", returncode=1))
    # Mock subprocess.run to also raise the expected exception path
    def mock_run(*args, **kwargs):
        raise FileNotFoundError("simulated no security cmd")
    monkeypatch.setattr(subprocess, "run", mock_run)

    assert _provider_available("claude-opus-4-7-20260101") is False

"""Tests for atomic_agents.review — cross-family adversarial review (#134)."""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents import review
from atomic_agents.exceptions import AtomicAgentsError
from atomic_agents._costs import PRICING


# ──────────────────────────────────────────────────────────────────
# Static surface


def test_system_prompt_cites_review_methodology_rules():
    """System prompt must explicitly bind to rule #11 + rule #12 — the whole
    point of cross-family review is the verify-before-claim discipline.

    A future edit that loses these references silently degrades review
    quality without any test failing other than this one.
    """
    prompt = review.REVIEW_SYSTEM_PROMPT
    assert "rule #11" in prompt.lower() or "rule 11" in prompt.lower() or "review in rounds" in prompt.lower()
    assert "rule #12" in prompt.lower() or "verify before claim" in prompt.lower() or "verify-before-claim" in prompt.lower()
    # Severity rubric must include all three priority levels
    assert "P1" in prompt and "P2" in prompt and "P3" in prompt
    # Verdict line is part of the contract with the operator
    assert "verdict" in prompt.lower()


def test_default_model_matches_pricing_table():
    """The default Kimi model id must be priced — otherwise calc_cost falls
    back to conservative-pessimistic and the cost summary lies.
    """
    assert review._DEFAULT_MODEL["kimi"] in PRICING


def test_review_request_defaults():
    """Default field values must be safe for the CLI's argparse defaults."""
    req = review.ReviewRequest(backend="kimi", prompt="check this")
    assert req.read_files == []
    assert req.target is None
    assert req.working_dir == Path.cwd()
    assert req.model is None
    assert req.max_tokens == 16000


# ──────────────────────────────────────────────────────────────────
# File handling


def test_read_text_safe_missing_file_raises(tmp_path):
    with pytest.raises(AtomicAgentsError, match="file not found"):
        review._read_text_safe(tmp_path / "nope.md")


def test_read_text_safe_directory_raises(tmp_path):
    """A directory path must not silently read as empty content."""
    with pytest.raises(AtomicAgentsError, match="not a regular file"):
        review._read_text_safe(tmp_path)


def test_format_file_block_shape():
    out = review._format_file_block(
        "Context", Path("CLAUDE.md"), "# CLAUDE\n\nhello"
    )
    assert out.startswith("### Context: `CLAUDE.md`")
    assert "```\n# CLAUDE\n\nhello\n```" in out


# ──────────────────────────────────────────────────────────────────
# Prompt assembly


def test_assemble_user_prompt_includes_target_context_and_prompt(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("target content")
    ctx = tmp_path / "ctx.md"
    ctx.write_text("context content")

    req = review.ReviewRequest(
        backend="kimi",
        prompt="adversarial round 1",
        read_files=[Path("ctx.md")],
        target=Path("target.md"),
        working_dir=tmp_path,
    )
    body = review._assemble_user_prompt(req)
    # Target appears before context (reviewer reads primary focus first)
    target_idx = body.index("Review target: `target.md`")
    ctx_idx = body.index("Context: `ctx.md`")
    prompt_idx = body.index("adversarial round 1")
    assert target_idx < ctx_idx < prompt_idx
    assert "target content" in body
    assert "context content" in body


def test_assemble_user_prompt_no_target_no_context(tmp_path):
    """Prompt-only review is valid — operator may want to ask about a concept."""
    req = review.ReviewRequest(
        backend="kimi",
        prompt="What is the framework's stance on X?",
        working_dir=tmp_path,
    )
    body = review._assemble_user_prompt(req)
    assert "Review prompt" in body
    assert "Review target" not in body
    assert "Context files" not in body


def test_assemble_user_prompt_resolves_paths_against_working_dir(tmp_path):
    """--working-dir lets the operator run /ship-style commands from any cwd."""
    target = tmp_path / "spec.md"
    target.write_text("SPEC")
    req = review.ReviewRequest(
        backend="kimi",
        prompt="x",
        target=Path("spec.md"),
        working_dir=tmp_path,
    )
    body = review._assemble_user_prompt(req)
    assert "SPEC" in body


# ──────────────────────────────────────────────────────────────────
# run_review dispatch + cost accounting


def test_run_review_unsupported_backend_raises():
    req = review.ReviewRequest(backend="claude", prompt="hi")  # type: ignore[arg-type]
    with pytest.raises(AtomicAgentsError, match="unsupported review backend"):
        review.run_review(req)


def _stub_llm_response(text="findings here", input_tokens=1000, output_tokens=500):
    return types.SimpleNamespace(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=0,
        raw={},
        tool_uses=[],
    )


def test_run_review_happy_path_calls_llm_and_computes_cost():
    """run_review must dispatch through _llm.call_llm with the system prompt,
    capture latency, and compute cost from the returned token counts.
    """
    req = review.ReviewRequest(backend="kimi", prompt="adversarial round 1")
    with patch("atomic_agents.review._llm.call_llm", return_value=_stub_llm_response()) as mock_call:
        result = review.run_review(req)

    assert mock_call.call_count == 1
    call_kwargs = mock_call.call_args.kwargs
    assert call_kwargs["model"] == "moonshot/moonshot-v1-128k"
    assert call_kwargs["system_prompt"] == review.REVIEW_SYSTEM_PROMPT
    # 1.0 — reasoning-style models like Kimi K2.6 only accept temperature=1
    assert call_kwargs["temperature"] == 1.0
    assert call_kwargs["max_tokens"] == 16000

    assert result.text == "findings here"
    assert result.input_tokens == 1000
    assert result.output_tokens == 500
    # moonshot/kimi-2.6 priced at 0.30 in / 1.20 out per Mtok = $0.0009
    assert result.cost_usd == pytest.approx(0.0009, abs=1e-6)
    assert result.cost_estimated_via_fallback is False
    assert result.model == "moonshot/moonshot-v1-128k"
    assert result.latency_ms >= 0


def test_run_review_honors_model_override():
    """An operator may want to point at a different Kimi model id later."""
    req = review.ReviewRequest(
        backend="kimi", prompt="x", model="moonshot/kimi-some-other"
    )
    with patch("atomic_agents.review._llm.call_llm", return_value=_stub_llm_response()) as mock_call:
        result = review.run_review(req)
    assert mock_call.call_args.kwargs["model"] == "moonshot/kimi-some-other"
    # Unknown model → fallback pricing flagged
    assert result.cost_estimated_via_fallback is True


def test_run_review_honors_max_tokens_override():
    req = review.ReviewRequest(backend="kimi", prompt="x", max_tokens=2000)
    with patch("atomic_agents.review._llm.call_llm", return_value=_stub_llm_response()) as mock_call:
        review.run_review(req)
    assert mock_call.call_args.kwargs["max_tokens"] == 2000


# ──────────────────────────────────────────────────────────────────
# Cost summary side channel


def test_print_cost_summary_goes_to_stderr_not_stdout():
    """Cost goes to stderr so pipes like `atomic-agents review ... > review.md`
    don't capture the cost line into the review artifact.
    """
    result = review.ReviewResult(
        text="...",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.0009,
        cost_estimated_via_fallback=False,
        model="moonshot/moonshot-v1-128k",
        latency_ms=1234,
    )
    out = io.StringIO()
    err = io.StringIO()
    # Print to err stream
    review.print_cost_summary(result, stream=err)
    err_text = err.getvalue()
    assert "moonshot/moonshot-v1-128k" in err_text
    assert "1000 in / 500 out" in err_text
    assert "$0.0009" in err_text
    assert "1234ms" in err_text
    # stdout untouched
    assert out.getvalue() == ""


def test_print_cost_summary_marks_fallback_pricing():
    """Operators must see when cost is conservative-pessimistic, not actual."""
    result = review.ReviewResult(
        text="x",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        cost_estimated_via_fallback=True,
        model="moonshot/kimi-unknown",
        latency_ms=10,
    )
    err = io.StringIO()
    review.print_cost_summary(result, stream=err)
    assert "fallback pricing" in err.getvalue()


# ──────────────────────────────────────────────────────────────────
# CLI integration


def test_cli_review_with_inline_prompt(monkeypatch, tmp_path, capsys):
    """`atomic-agents review --backend kimi --prompt '...'` must dispatch
    through run_review and print the result to stdout.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")

    from atomic_agents.cli import main as cli_main

    fake_result = review.ReviewResult(
        text="REVIEW OUTPUT",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0001,
        cost_estimated_via_fallback=False,
        model="moonshot/moonshot-v1-128k",
        latency_ms=200,
    )
    with patch("atomic_agents.review.run_review", return_value=fake_result):
        rc = cli_main([
            "review", "--backend", "kimi", "--prompt", "review this",
            "--working-dir", str(tmp_path),
        ])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "REVIEW OUTPUT" in out
    assert "moonshot/moonshot-v1-128k" in err


def test_cli_review_with_prompt_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("# round 1\nfind issues")

    from atomic_agents.cli import main as cli_main

    captured: dict = {}

    def fake_run(req):
        captured["prompt"] = req.prompt
        return review.ReviewResult(
            text="x", input_tokens=1, output_tokens=1, cost_usd=0.0,
            cost_estimated_via_fallback=False, model="moonshot/moonshot-v1-128k",
            latency_ms=1,
        )

    with patch("atomic_agents.review.run_review", side_effect=fake_run):
        rc = cli_main([
            "review", "--backend", "kimi",
            "--prompt-file", str(prompt_path),
            "--working-dir", str(tmp_path),
        ])
    assert rc == 0
    assert captured["prompt"] == "# round 1\nfind issues"


def test_cli_review_missing_prompt_file_returns_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")
    from atomic_agents.cli import main as cli_main
    rc = cli_main([
        "review", "--backend", "kimi",
        "--prompt-file", str(tmp_path / "nope.md"),
    ])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "not found" in err


def test_cli_review_parses_read_files_csv(monkeypatch, tmp_path):
    """Comma-separated --read-files must produce a Path list (no blanks)."""
    monkeypatch.setenv("ATOMIC_AGENTS_MOONSHOT_KEY", "fake-key")
    f1 = tmp_path / "a.md"
    f1.write_text("a")
    f2 = tmp_path / "b.md"
    f2.write_text("b")

    from atomic_agents.cli import main as cli_main

    captured: dict = {}

    def fake_run(req):
        captured["read_files"] = req.read_files
        return review.ReviewResult(
            text="x", input_tokens=1, output_tokens=1, cost_usd=0.0,
            cost_estimated_via_fallback=False, model="moonshot/moonshot-v1-128k",
            latency_ms=1,
        )

    with patch("atomic_agents.review.run_review", side_effect=fake_run):
        cli_main([
            "review", "--backend", "kimi", "--prompt", "x",
            "--read-files", "a.md, ,b.md",  # blank entry must be skipped
            "--working-dir", str(tmp_path),
        ])
    assert captured["read_files"] == [Path("a.md"), Path("b.md")]

"""Adversarial code review via a non-author model family — CLAUDE.md rule #11.

Per the framework's review-in-rounds methodology, non-trivial PRs (backend
protocols, framework refactors, anything touching `agent.call()`, the cost
gates, the protocol surfaces) get 3-5 review rounds pre-merge — each round
catches different things because each fix changes the diff and exposes new
edges. Cross-family review (different model family than the author) is the
whole point: Codex catches what Claude misses and vice versa.

This module is the second cross-family reviewer alongside `codex exec`, using
Moonshot's Kimi 2.6 via the project's existing `_llm.py` Moonshot backend.
When Codex hangs (precedent: PR #118 audit spec round 2), Kimi is the
substitutable reviewer — preserving cross-family coverage instead of falling
back to a same-family Opus subagent.

Built-in system prompt enforces rule #12 (verify before claim, empirically):
for every finding, the reviewer must cite file:line evidence quoted from
files included in the review context, never assert by plausibility alone.

Public surface:
    ReviewRequest(backend, prompt, read_files, target, working_dir, model, max_tokens)
    ReviewResult(text, input_tokens, output_tokens, cost_usd, model, latency_ms)
    run_review(request) -> ReviewResult

CLI surface (see `atomic_agents.cli:_cmd_review`):
    atomic-agents review --backend kimi --prompt "..." --read-files "..." --target "..."
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import _costs, _llm
from ._io import safe_resolve_under
from .exceptions import AtomicAgentsError, PathTraversalError

_logger = logging.getLogger(__name__)


# Available backends. `kimi` is the only one implemented today; the literal
# leaves room for `claude` and `codex` backends in future PRs so the CLI
# surface doesn't change shape when they land.
ReviewBackend = Literal["kimi"]

# Default model per backend. Kept here (not in PRICING) because PRICING is
# operator-facing pricing data; this is the reviewer's preferred model id.
_DEFAULT_MODEL: dict[str, str] = {
    # Default to moonshot-v1-128k (non-thinking model). Kimi K2.6 / K2.5 are
    # thinking-style models that put most of `completion_tokens` into a
    # `reasoning_content` field separate from the visible `content` — without
    # a dedicated extraction path in `_llm._call_moonshot`, they often return
    # empty visible output for review-length prompts. Switch to a Kimi model
    # via `--model moonshot/kimi-k2.6 --max-tokens 32000` once that integration
    # lands (tracked alongside the LLMBackend protocol in #87).
    "kimi": "moonshot/moonshot-v1-128k",
}


REVIEW_SYSTEM_PROMPT = """\
You are an adversarial code reviewer for the Atomic Agents framework. The \
maintainer's project values cross-model coverage: different model families \
catch different blind spots, and your specific job is to find what the \
author's own model missed.

Two non-negotiable rules govern your output:

**Rule #11 — review in rounds, not passes.** You are one round in a 3-5 \
round review cycle. Don't try to catch everything — focus on the diff or \
target in front of you. Quality over breadth.

**Rule #12 — verify before claim, empirically.** Every finding must cite \
specific file:line evidence quoted from the files included in this review \
context. Do NOT assert by plausibility — if you cannot quote the exact \
code or text that justifies a finding, do not file it. When the reviewer \
asserts a thing, the maintainer reproduces it. False findings cost time; \
unverifiable findings get rejected.

## Output format

Start with a one-line summary: "Reviewing <target> — N files in context."

Then numbered findings, each in this exact shape:

```
Finding N — [P1/P2/P3] — Short title

What I asserted: <one sentence>
How I verified: <command run + result, OR file:line quoted verbatim from the context>
Why it matters: <impact in one or two sentences>
Suggested fix: <one or two sentences>
```

Severity rubric:
- **P1** — correctness bug, backward-compat break, security issue, or claim \
that contradicts shipped code. Must fix before merge.
- **P2** — real issue worth fixing (logic gap, missing test, doc-vs-code drift, \
performance regression). Should fix.
- **P3** — polish (naming, comment quality, dead code, redundancy). Optional.

If a finding's confidence is below 7/10, either verify it more rigorously or \
omit it. Generic recommendations ("consider adding tests") without a specific \
target are not findings.

End with a one-paragraph verdict on its own line:
- `Verdict: ship-as-is` — no findings worth blocking on.
- `Verdict: fix-Pn-then-ship` — list the Pn items that must land before merge.
- `Verdict: needs-rework` — material design issues; recommend the maintainer \
revisit before continuing.

If a category in the review prompt is clean after verification, say so explicitly: \
"Category X: no findings — verified by [specific check]." Don't pad the report.
"""


@dataclass
class ReviewRequest:
    """Single review request — backend, prompt, files for grounding."""

    backend: ReviewBackend
    prompt: str
    read_files: list[Path] = field(default_factory=list)
    target: Path | None = None
    working_dir: Path = field(default_factory=Path.cwd)
    model: str | None = None
    # 16000 default — reasoning-style models like Kimi K2.6 spend a large
    # portion of `completion_tokens` on internal `reasoning_content`. A lower
    # cap risks the model exhausting its budget while thinking, leaving
    # `content` empty. Operators can lower via --max-tokens for cheaper
    # backends that don't reason.
    max_tokens: int = 16000


@dataclass
class ReviewResult:
    """Outcome of a review run."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_estimated_via_fallback: bool
    model: str
    latency_ms: int


def _read_text_safe(path: Path) -> str:
    """Read a file as UTF-8; surface a clean error if missing or unreadable."""
    if not path.exists():
        raise AtomicAgentsError(f"file not found: {path}")
    if not path.is_file():
        raise AtomicAgentsError(f"not a regular file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AtomicAgentsError(f"cannot read {path}: {e}") from e


def _format_file_block(label: str, path: Path, content: str) -> str:
    """Format one file as a fenced markdown block with a header label.

    Label is `Context` or `Review target` so the reviewer knows which file
    is under review vs which are grounding context.
    """
    rel = path
    return f"### {label}: `{rel}`\n\n```\n{content}\n```\n"


def _assemble_user_prompt(request: ReviewRequest) -> str:
    """Build the user message: target file, context files, then the prompt.

    Layout favors the reviewer reading the target first (primary focus),
    then context (grounding), then the operator's adversarial prompt.

    Every operator-supplied path is resolved through `safe_resolve_under`
    against `working_dir` per the framework's canonical path-traversal
    guard. `..` segments and absolute paths that escape the working dir
    raise `PathTraversalError`. Operators legitimately needing to grep
    across multiple repos should raise `--working-dir` to the common
    parent rather than expect the wrapper to silently traverse.
    """
    parts: list[str] = []

    if request.target is not None:
        resolved = safe_resolve_under(request.target, request.working_dir)
        content = _read_text_safe(resolved)
        parts.append(_format_file_block("Review target", request.target, content))

    if request.read_files:
        parts.append("## Context files\n")
        for f in request.read_files:
            resolved = safe_resolve_under(f, request.working_dir)
            content = _read_text_safe(resolved)
            parts.append(_format_file_block("Context", f, content))

    parts.append("## Review prompt\n")
    parts.append(request.prompt.strip())
    return "\n".join(parts)


def run_review(request: ReviewRequest) -> ReviewResult:
    """Execute a review and return the result.

    Raises AtomicAgentsError when a referenced file is missing or the backend
    is unsupported. LLM-call failures propagate as the provider's own exception
    type so operators can see backend-specific debugging info.
    """
    if request.backend not in _DEFAULT_MODEL:
        raise AtomicAgentsError(
            f"unsupported review backend: {request.backend!r}. "
            f"Supported: {sorted(_DEFAULT_MODEL.keys())}"
        )

    model = request.model or _DEFAULT_MODEL[request.backend]
    user_prompt = _assemble_user_prompt(request)

    start = time.time()
    # temperature=1.0 — many reasoning-style models (Kimi K2.6, o1-style) reject
    # temperature != 1. Lower values gain nothing for adversarial review where we
    # want the model thinking hard, not sampling deterministically.
    raw = _llm.call_llm(
        model=model,
        system_prompt=REVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=request.max_tokens,
        temperature=1.0,
    )
    latency_ms = int((time.time() - start) * 1000)

    cost, fallback = _costs.calc_cost(
        model=model,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
    )

    return ReviewResult(
        text=raw.text,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
        cost_usd=cost,
        cost_estimated_via_fallback=fallback,
        model=model,
        latency_ms=latency_ms,
    )


def print_cost_summary(result: ReviewResult, stream=None) -> None:
    """Print a one-line cost summary to stderr so it doesn't pollute the review.

    Stderr keeps stdout clean for piping the review output to a file, another
    tool, or a PR comment. Per CLAUDE.md rule #4, every LLM-touching code path
    surfaces its cost.

    When the result has empty visible content (likely a Kimi K2.x thinking
    model that consumed its budget on `reasoning_content` — see #146), an
    additional WARNING line precedes the cost summary so operators don't
    silently pay for blank reviews.

    `stream` defaults to the current `sys.stderr` resolved at call time (not
    import time) so test harnesses like pytest's capsys can redirect output
    correctly.
    """
    if stream is None:
        stream = sys.stderr
    if not result.text.strip():
        print(
            f"WARNING: reviewer returned empty content "
            f"(model={result.model}, output_tokens={result.output_tokens}) — "
            f"likely a thinking-style model whose visible content was crowded "
            f"out by reasoning_content. Bump --max-tokens or use a non-thinking "
            f"model (e.g. moonshot/moonshot-v1-128k). See issue #146.",
            file=stream,
        )
    fallback_marker = " (fallback pricing)" if result.cost_estimated_via_fallback else ""
    print(
        f"--- review: {result.model} | {result.input_tokens} in / "
        f"{result.output_tokens} out | ${result.cost_usd:.4f}{fallback_marker} | "
        f"{result.latency_ms}ms ---",
        file=stream,
    )

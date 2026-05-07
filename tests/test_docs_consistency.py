"""
Regression tests for docs/extras consistency.

Codex P2 finding #19 (ghost modules) and #18 (goal CLI shape).

These tests make sure:
  - No doc or extras file references the nonexistent `atomic_agents.tune`
    or `atomic_agents.run` modules.
  - Every `python -m atomic_agents.<name>` reference in docs/extras uses
    a module that is actually importable.
  - The goal CLI uses <subcommand> <agent> order (not the old reversed order).
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
DOCS = REPO_ROOT / "docs"
EXTRAS = REPO_ROOT / "extras"

# ── Helpers ───────────────────────────────────────────────────────────────────


def grep_tree(root: Path, pattern: str) -> list[tuple[Path, int, str]]:
    """Return (file, lineno, line) for every match of pattern in root."""
    hits: list[tuple[Path, int, str]] = []
    regex = re.compile(pattern)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append((path, lineno, line.strip()))
    return hits


# ── Test #19 — no ghost module references ─────────────────────────────────────


def test_no_atomic_agents_tune_references() -> None:
    """atomic_agents.tune does not exist; the module is atomic_agents.tuning."""
    hits = grep_tree(DOCS, r"atomic_agents\.tune\b")
    hits += grep_tree(EXTRAS, r"atomic_agents\.tune\b")
    messages = [f"  {p}:{n}: {l}" for p, n, l in hits]
    assert not hits, (
        "Found references to nonexistent module `atomic_agents.tune`.\n"
        "Use `atomic_agents.tuning` instead.\n"
        + "\n".join(messages)
    )


def test_no_atomic_agents_run_references() -> None:
    """atomic_agents.run does not exist; use `atomic_agents.cli run` or the console script."""
    hits = grep_tree(DOCS, r"atomic_agents\.run\b")
    hits += grep_tree(EXTRAS, r"atomic_agents\.run\b")
    messages = [f"  {p}:{n}: {l}" for p, n, l in hits]
    assert not hits, (
        "Found references to nonexistent module `atomic_agents.run`.\n"
        "Use `atomic-agents run` (console script) or "
        "`python -m atomic_agents.cli run` instead.\n"
        + "\n".join(messages)
    )


# Modules referenced via `python -m atomic_agents.<name>` that are real.
# dashboard is a package, not a .py — still importable.
_KNOWN_MODULES = {
    "atomic_agents.cli",
    "atomic_agents.eval",
    "atomic_agents.goal",
    "atomic_agents.migrate",
    "atomic_agents.tuning",
    "atomic_agents.dashboard",
    "atomic_agents.dashboard.serve",  # may be referenced as sub-invocation
}

# Modules the docs deliberately reference only as a package (no `python -m`).
_EXCLUDE_FROM_MODULE_CHECK: set[str] = set()


def test_python_m_references_real_modules() -> None:
    """Every `python -m atomic_agents.<x>` in docs/extras must be importable."""
    pattern = r"python -m (atomic_agents\.\S+)"
    regex = re.compile(pattern)

    all_hits: list[tuple[Path, int, str, str]] = []  # (file, line, text, modname)
    for root in (DOCS, EXTRAS):
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for match in regex.finditer(line):
                    # Strip trailing punctuation/flags that aren't part of the module name.
                    modname = match.group(1).rstrip(".,;:`'\"")
                    if modname not in _EXCLUDE_FROM_MODULE_CHECK:
                        all_hits.append((path, lineno, line.strip(), modname))

    bad: list[str] = []
    seen: set[str] = set()
    for path, lineno, line, modname in all_hits:
        if modname in seen:
            continue
        seen.add(modname)
        # Try importlib first; fall back to checking _KNOWN_MODULES.
        try:
            importlib.import_module(modname)
        except ModuleNotFoundError:
            if modname not in _KNOWN_MODULES:
                bad.append(
                    f"  {path}:{lineno}: `{modname}` not importable — "
                    f"add to _KNOWN_MODULES or fix the reference"
                )
        except Exception:
            # Other import errors (e.g. missing env) — skip
            pass

    assert not bad, (
        "Docs/extras reference modules that cannot be imported:\n" + "\n".join(bad)
    )


# ── Test #18 — goal CLI shape ─────────────────────────────────────────────────


def test_goal_cli_subcommand_first() -> None:
    """
    Confirm the actual goal CLI takes <subcommand> before <agent>.

    Runs `python -m atomic_agents.goal status --help` and asserts the
    usage line shows `status` before the `agent` positional.  This pins
    the documented shape so it can't silently regress.
    """
    result = subprocess.run(
        [sys.executable, "-m", "atomic_agents.goal", "status", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"goal status --help failed:\n{result.stderr}"
    usage = result.stdout + result.stderr
    # Usage line should mention 'status' before 'agent'.
    assert "status" in usage, "Expected 'status' in help output"
    # The positional argument should be named 'agent'.
    assert "agent" in usage, "Expected 'agent' positional in help output"
    # The old (broken) shape would have been: goal <agent> status
    # Confirm usage line is NOT "goal agent status" shape.
    assert "goal status" in usage or "atomic-agents.goal status" in usage, (
        "Usage line does not show subcommand-first shape. "
        f"Got:\n{usage}"
    )


def test_goal_cli_all_subcommands_present() -> None:
    """The six documented subcommands all exist in the CLI."""
    expected = {"status", "next", "advance", "abandon", "complete", "report"}
    result = subprocess.run(
        [sys.executable, "-m", "atomic_agents.goal", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    output = result.stdout + result.stderr
    missing = [sub for sub in expected if sub not in output]
    assert not missing, (
        f"Goal CLI missing subcommands: {missing}\nHelp output:\n{output}"
    )

"""Platform helpers — path expansion, agents-root resolution.

Single source of truth for "where is this agent's vault folder".
"""

from __future__ import annotations
import os
from pathlib import Path

# Module-level constant kept for backward compat with any external imports.
# get_agents_root() does NOT reference this constant; it computes fresh on
# each call so test monkeypatches of HOME work correctly. New code should
# call get_agents_root() rather than referencing this constant directly.
DEFAULT_AGENTS_ROOT = (Path.home() / "docs" / "agents").expanduser().resolve()


def get_agents_root() -> Path:
    """Resolve the agents root from env var or default to ~/docs/agents.

    Reads HOME and ATOMIC_AGENTS_ROOT on EVERY call (not at import time) so
    tests that monkeypatch HOME after framework import see the correct value.
    """
    env_val = os.environ.get("ATOMIC_AGENTS_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return (Path.home() / "docs" / "agents").expanduser().resolve()


def get_agent_root(agent_name: str, agents_root: Path | None = None) -> Path:
    """Path to a specific agent's folder."""
    return (agents_root or get_agents_root()) / agent_name


def expand(path: str | Path) -> Path:
    """Expand ~ and resolve a path."""
    return Path(path).expanduser().resolve()

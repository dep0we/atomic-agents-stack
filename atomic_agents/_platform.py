"""Platform helpers — path expansion, agents-root resolution.

Single source of truth for "where is this agent's vault folder".
"""

from __future__ import annotations
import os
from pathlib import Path

DEFAULT_AGENTS_ROOT = Path.home() / "docs" / "agents"


def get_agents_root() -> Path:
    """Resolve the agents-root directory.

    Order: ATOMIC_AGENTS_ROOT env var → default ~/docs/agents.

    Operators with a custom vault location override via the env var.
    """
    env_val = os.environ.get("ATOMIC_AGENTS_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return DEFAULT_AGENTS_ROOT


def get_agent_root(agent_name: str, agents_root: Path | None = None) -> Path:
    """Path to a specific agent's folder."""
    return (agents_root or get_agents_root()) / agent_name


def expand(path: str | Path) -> Path:
    """Expand ~ and resolve a path."""
    return Path(path).expanduser().resolve()

"""Platform helpers — path expansion, agents-root resolution.

Single source of truth for "where is this agent's vault folder".
"""

from __future__ import annotations
import os
from pathlib import Path

class AgentsRootNotSetError(RuntimeError):
    """Raised when ATOMIC_AGENTS_ROOT is missing or empty."""


def get_agents_root() -> Path:
    """Resolve the agents root from the ATOMIC_AGENTS_ROOT env var.

    There is no default location. If the variable is missing or empty this
    raises AgentsRootNotSetError naming the variable and
    ``~/.config/data-roots.env``, rather than silently falling back to a
    folder under HOME.

    Reads ATOMIC_AGENTS_ROOT on EVERY call (not at import time) so tests that
    monkeypatch the environment see the correct value.
    """
    env_val = os.environ.get("ATOMIC_AGENTS_ROOT")
    if not env_val:
        raise AgentsRootNotSetError(
            "ATOMIC_AGENTS_ROOT is not set; define it in ~/.config/data-roots.env"
        )
    return Path(env_val).expanduser().resolve()


def get_agent_root(agent_name: str, agents_root: Path | None = None) -> Path:
    """Path to a specific agent's folder."""
    return (agents_root or get_agents_root()) / agent_name


def expand(path: str | Path) -> Path:
    """Expand ~ and resolve a path."""
    return Path(path).expanduser().resolve()


def resolve_under_agent_root(path_str: str, agent_root: Path) -> Path:
    """Resolve a tools.md path token under agent_root when the token is bare-relative.

    Resolution rules:
    - Absolute paths (starting with /) pass through expand() unchanged.
    - Tilde paths (starting with ~) pass through expand() unchanged.
    - Bare-relative paths (everything else, e.g. 'memory/', './') are resolved
      as agent_root / path_str, returning an absolute resolved Path.

    This is the framework-wide anchor for tools.md path resolution.
    Bare-relative paths in tools.md always mean relative to the agent's own
    folder, never relative to the process CWD. (Documenting this convention
    in spec/01 is tracked in #549; spec/01 today only shows the
    placeholder-absolute <agents_root>/<name>/... form.)

    Note: this helper does NOT guard against path traversal (e.g. '../escape'
    resolves to the parent of agent_root). Path containment is a separate
    concern enforced by the write-policy layer.

    Example::

        resolve_under_agent_root('memory/', Path('/home/user/agents/myagent'))
        # -> Path('/home/user/agents/myagent/memory')

        resolve_under_agent_root('~/docs/shared', Path('/home/user/agents/myagent'))
        # -> Path('/home/user/docs/shared')

        resolve_under_agent_root('/absolute/path', Path('/home/user/agents/myagent'))
        # -> Path('/absolute/path')
    """
    p = Path(path_str)
    if path_str.startswith("~") or p.is_absolute():
        return expand(p)
    # Normalize agent_root first so the join is correct regardless of how the
    # caller supplied it (e.g. an unexpanded '~/agents/foo' would otherwise
    # leave a literal '~' segment under the process CWD). Per the function's
    # stated framework-wide contract, it must be robust to any agent_root form.
    agent_root = Path(agent_root).expanduser().resolve()
    return (agent_root / path_str).resolve()

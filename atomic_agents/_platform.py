"""Platform helpers — path expansion, agents-root resolution.

Single source of truth for "where is this agent's vault folder". The agents
root setting may list several directories (see ``get_agents_roots``).
"""

from __future__ import annotations
import os
from pathlib import Path

from .exceptions import AtomicAgentsError


class AgentsRootNotSetError(RuntimeError):
    """Raised when ATOMIC_AGENTS_ROOT is missing or empty."""


_ROOT_VAR = "ATOMIC_AGENTS_ROOT"
_DEFAULT_ENV_FILE = "~/.config/data-roots.env"


def data_roots_env_path() -> Path:
    """Path of the data-roots env file (``DATA_ROOTS_ENV`` overrides, for tests)."""
    return Path(os.environ.get("DATA_ROOTS_ENV") or _DEFAULT_ENV_FILE).expanduser()


def _parse_root_line(line: str) -> str | None:
    """Return the value if ``line`` assigns ATOMIC_AGENTS_ROOT, else None.

    Accepts ``export ATOMIC_AGENTS_ROOT=value`` and ``ATOMIC_AGENTS_ROOT=value``
    with optional single or double quotes. Text only: nothing is expanded or
    executed (``$VAR`` and backticks stay literal).
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export") and line[6:7] in (" ", "\t"):
        line = line[6:].lstrip()
    name, sep, rest = line.partition("=")
    if not sep or name.strip() != _ROOT_VAR:
        return None
    rest = rest.strip()
    if rest[:1] in ("'", '"'):
        close = rest.find(rest[0], 1)
        return rest[1:close] if close != -1 else rest[1:]
    # Unquoted: an inline comment starts at whitespace followed by '#'.
    for i, ch in enumerate(rest):
        if ch == "#" and i > 0 and rest[i - 1] in (" ", "\t"):
            rest = rest[:i]
            break
    return rest.strip()


def _read_root_from_file(path: Path) -> str | None:
    """Read ATOMIC_AGENTS_ROOT from the env file as text. Last assignment wins."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    value: str | None = None
    for line in text.splitlines():
        parsed = _parse_root_line(line)
        if parsed is not None:
            value = parsed
    return value or None


def resolve_agents_root_source() -> tuple[str | None, str]:
    """Return ``(raw_value, source)`` for the agents root.

    The environment variable wins. If it is unset or empty, the data-roots env
    file is read (text only, only ATOMIC_AGENTS_ROOT). An empty value counts as
    missing. ``source`` is "environment" or the file path; when nothing is
    found the value is None and ``source`` is "unset".
    """
    env_val = os.environ.get(_ROOT_VAR)
    if env_val:
        return env_val, "environment"
    path = data_roots_env_path()
    file_val = _read_root_from_file(path)
    if file_val:
        return file_val, str(path)
    return None, "unset"


_NOT_SET_MESSAGE = (
    "ATOMIC_AGENTS_ROOT is not set. Define it in ~/.config/data-roots.env "
    "as 'export ATOMIC_AGENTS_ROOT=/path/to/agents' (the CLI reads that "
    "line itself), or export the variable in your shell. Several agent "
    f"directories can be listed, separated by '{os.pathsep}' (like PATH)."
)


class AgentRootConflictError(AtomicAgentsError):
    """Raised when one agent name exists under more than one agents root."""


def split_agents_roots(value: str) -> list[Path]:
    """Split a raw ATOMIC_AGENTS_ROOT value into resolved directories.

    The value is one or more directories separated by the OS path separator
    (``:`` on macOS and Linux, ``;`` on Windows), like PATH. Empty entries are
    skipped and exact duplicates are collapsed, keeping the first. A value with
    a single directory yields a one-item list.
    """
    roots: list[Path] = []
    for part in value.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        path = Path(part).expanduser().resolve()
        if path not in roots:
            roots.append(path)
    return roots


def get_agents_roots() -> list[Path]:
    """Resolve every configured agents root, in search order.

    Source order is the same as :func:`resolve_agents_root_source`. Raises
    AgentsRootNotSetError when nothing is configured (there is no default
    location) or when the value holds no directory at all.

    Reads on EVERY call (not at import time) so tests that monkeypatch the
    environment see the correct value.
    """
    value, _source = resolve_agents_root_source()
    roots = split_agents_roots(value) if value else []
    if not roots:
        raise AgentsRootNotSetError(_NOT_SET_MESSAGE)
    return roots


def get_agents_root() -> Path:
    """Resolve the primary (first) agents root.

    With a single configured directory this is that directory, exactly as
    before. With several, it is the first one: use it for fleet-level work
    that has no agent name (for example, where ``init`` creates a new agent).
    To locate an existing agent use :func:`resolve_agents_root_for`.
    """
    return get_agents_roots()[0]


def resolve_agents_root_for(agent_name: str, roots: list[Path] | None = None) -> Path:
    """Return the agents root that holds ``agent_name``.

    Roots are searched in order (``roots`` defaults to the configured list). If
    the agent folder exists under exactly one root, that root is returned. If it
    exists under none, the first root is returned so callers report their usual
    "agent folder not found" error against a real path. If it exists under two
    or more, AgentRootConflictError names every path: the framework never picks
    one silently.
    """
    candidates = roots if roots is not None else get_agents_roots()
    found = [r for r in candidates if (r / agent_name).is_dir()]
    if len(found) > 1:
        paths = ", ".join(str(r / agent_name) for r in found)
        raise AgentRootConflictError(
            f"Agent '{agent_name}' exists under more than one agents root: "
            f"{paths}. Rename or remove one, or drop a root from "
            f"ATOMIC_AGENTS_ROOT."
        )
    return found[0] if found else candidates[0]


def get_agent_root(agent_name: str, agents_root: Path | None = None) -> Path:
    """Path to a specific agent's folder."""
    root = agents_root or resolve_agents_root_for(agent_name)
    return root / agent_name


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

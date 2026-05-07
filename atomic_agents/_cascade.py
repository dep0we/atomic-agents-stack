"""Multi-agent project cascade loader per spec/06-multi-agent-projects.md.

When an agent path matches the three-layer cascade pattern:

    <agents_root>/<system>/projects/<project>/agents/<role>/

the framework walks up to find:

- **Layer 1 (role)** — shared role definition: ``<system>/roles/<role>/``
  with ``PROMPT.md``, ``tools.md``, ``model.md``.
- **Layer 2 (project)** — project-shared world: ``<system>/projects/<project>/``
  with ``canon.md``, ``style_guide.md``, ``policy/``, optional ``goal.md``,
  and the work ``queue/``.
- **Layer 3 (instance)** — project x role instance: the agent path itself,
  with ``persona/{IDENTITY,SOUL,USER}.md``, ``memory/``, ``wiki/``,
  ``journal/``, ``log/``, and optional ``tools.md`` / ``tools.override.md``
  / ``model.md`` overrides.

The cascade is opt-in: agents whose path doesn't match this pattern stay on
the original single-agent layout (loaded entirely from one folder). Detection
happens via :func:`detect_cascade`; the resolved roots come back as a
:class:`CascadePaths` triple.

Tools.md resolution order (most specific wins):

1. ``<instance>/tools.override.md`` — additive merge with role's tools.md
   (instance section is appended after role section).
2. ``<instance>/tools.md`` — full replacement of role's tools.md.
3. ``<role>/tools.md`` — base.

Model.md resolution: instance overrides role.

This module returns *paths and rendered text*. Parsing of tools.md/model.md
into structured config still happens in ``_tools.py`` / ``_model.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CascadePaths:
    """Resolved roots for a cascaded multi-agent project agent.

    All three roots are absolute paths to existing directories.
    """

    system_root: Path
    role_root: Path
    project_root: Path
    instance_root: Path
    role_name: str
    project_name: str


def detect_cascade(agent_root: Path) -> CascadePaths | None:
    """Inspect ``agent_root`` and return cascade roots if it matches the pattern.

    Returns ``None`` for single-agent layouts (no cascade), so callers can
    branch cleanly and preserve the original load behavior.

    The pattern is: ``<system>/projects/<project>/agents/<role>``. We require
    the matching ``<system>/roles/<role>/`` directory to exist before
    declaring it a cascade — without the role layer, there's nothing to
    cascade into.
    """
    parts = agent_root.parts
    if len(parts) < 5:
        return None

    # Walk from the tail: ../projects/<project>/agents/<role>
    role_name = parts[-1]
    if parts[-2] != "agents":
        return None
    project_name = parts[-3]
    if parts[-4] != "projects":
        return None

    system_root = Path(*parts[:-4])
    project_root = system_root / "projects" / project_name
    role_root = system_root / "roles" / role_name
    if not role_root.is_dir():
        return None

    return CascadePaths(
        system_root=system_root,
        role_root=role_root,
        project_root=project_root,
        instance_root=agent_root,
        role_name=role_name,
        project_name=project_name,
    )


# ──────────────────────────────────────────────────────────────────
# tools.md / model.md override resolution


def resolve_tools_md(cascade: CascadePaths) -> tuple[Path | None, str]:
    """Resolve which tools.md text to use for this cascaded agent.

    Returns ``(source_path, text)``. Resolution order:

    1. ``<instance>/tools.override.md`` exists → role tools + instance override,
       concatenated with a separator (additive merge).
    2. ``<instance>/tools.md`` exists → full replacement; role's tools.md is
       ignored.
    3. Otherwise → role's tools.md (or empty string if neither exists).

    The "primary" source path returned is the most-specific file (override or
    instance > role). When neither exists, returns ``(None, "")``.
    """
    instance_override = cascade.instance_root / "tools.override.md"
    instance_tools = cascade.instance_root / "tools.md"
    role_tools = cascade.role_root / "tools.md"

    if instance_override.is_file():
        role_text = role_tools.read_text(encoding="utf-8") if role_tools.is_file() else ""
        override_text = instance_override.read_text(encoding="utf-8")
        merged = role_text.rstrip() + "\n\n" + _OVERRIDE_SEPARATOR + "\n\n" + override_text
        return instance_override, merged

    if instance_tools.is_file():
        return instance_tools, instance_tools.read_text(encoding="utf-8")

    if role_tools.is_file():
        return role_tools, role_tools.read_text(encoding="utf-8")

    return None, ""


def resolve_model_md(cascade: CascadePaths) -> Path | None:
    """Return the path of the model.md the instance should parse.

    Instance overrides role. If neither has model.md, returns ``None`` and
    the caller falls back to defaults from :func:`atomic_agents._model.parse_model_md`.
    """
    instance_model = cascade.instance_root / "model.md"
    if instance_model.is_file():
        return instance_model
    role_model = cascade.role_root / "model.md"
    if role_model.is_file():
        return role_model
    return None


_OVERRIDE_SEPARATOR = "<!-- ─── instance tools.override.md ─── -->"


# ──────────────────────────────────────────────────────────────────
# Layer-1 (role) and Layer-2 (project) text loaders


def load_role_prompt(cascade: CascadePaths) -> str:
    """Load Layer-1 role prompt: ``<role>/PROMPT.md``. Empty string if missing."""
    path = cascade.role_root / "PROMPT.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def load_project_layer(cascade: CascadePaths) -> dict[str, str]:
    """Load Layer-2 project shared assets.

    Returns a dict with keys:

    - ``canon``     — content of ``project/canon.md`` (or empty)
    - ``style_guide`` — content of ``project/style_guide.md`` (or empty)
    - ``goal``      — content of ``project/goal.md`` (or empty)
    - ``policy``    — concatenated content of all ``.md`` files under
      ``project/policy/`` (alphabetical), with per-file H1 separators.
    """
    out = {
        "canon": _read_or_empty(cascade.project_root / "canon.md"),
        "style_guide": _read_or_empty(cascade.project_root / "style_guide.md"),
        "goal": _read_or_empty(cascade.project_root / "goal.md"),
        "policy": _load_policy_dir(cascade.project_root / "policy"),
    }
    return out


def _read_or_empty(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _load_policy_dir(policy_dir: Path) -> str:
    """Concatenate all .md files in policy/ alphabetically, with separators.

    Recurses into subdirectories so policy can be organized.
    """
    if not policy_dir.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(policy_dir.rglob("*.md")):
        rel = path.relative_to(policy_dir)
        parts.append(f"# policy/{rel.as_posix()}\n\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n---\n\n".join(parts)


# ──────────────────────────────────────────────────────────────────
# Project queue claim mechanics per spec/06


@dataclass
class QueueItem:
    """One claimed work item from the project queue.

    The on-disk file lives at ``project_root/queue/claimed/<lease_token>/<original_name>``.
    Callers should pass ``self.path.read_text()`` as the work_item content,
    then call ``release_claim()`` or ``move_to_dead_letter()`` when done.
    """

    path: Path
    original_name: str
    role: str
    lease_token: str
    claimed_at: float


def claim_next_queued(
    project_root: Path, role: str, lease_token: str,
) -> QueueItem | None:
    """Atomically claim the next item from ``project_root/queue/queued/<role>/``.

    Atomicity: uses ``Path.rename`` (POSIX atomic move) to take ownership of
    a file. If two workers race, only one rename succeeds for any given file;
    the loser ``FileNotFoundError``s and tries the next one.

    Returns ``None`` when the queue is empty or no items remain after races.
    """
    import time

    queued_dir = project_root / "queue" / "queued" / role
    if not queued_dir.is_dir():
        return None

    claimed_dir = project_root / "queue" / "claimed" / lease_token
    claimed_dir.mkdir(parents=True, exist_ok=True)

    # Sort to give deterministic FIFO-by-name behavior.
    candidates = sorted(p for p in queued_dir.iterdir() if p.is_file())
    for src in candidates:
        dst = claimed_dir / src.name
        try:
            src.rename(dst)
        except FileNotFoundError:
            # Another worker raced us to this file. Try the next.
            continue
        return QueueItem(
            path=dst,
            original_name=src.name,
            role=role,
            lease_token=lease_token,
            claimed_at=time.time(),
        )
    return None


def release_claim(item: QueueItem, project_root: Path) -> None:
    """Mark a claimed item as completed by moving it to ``queue/done/``."""
    done_dir = project_root / "queue" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    item.path.rename(done_dir / item.original_name)


def move_to_dead_letter(
    item: QueueItem, project_root: Path, reason: str = "",
) -> None:
    """Move a claimed item to ``queue/dead-letter/`` after exhausting retries.

    A sibling ``.reason.txt`` is written with ``reason`` for debugging.
    """
    dl_dir = project_root / "queue" / "dead-letter"
    dl_dir.mkdir(parents=True, exist_ok=True)
    target = dl_dir / item.original_name
    item.path.rename(target)
    if reason:
        (dl_dir / (item.original_name + ".reason.txt")).write_text(reason, encoding="utf-8")


def recover_stale_claims(
    project_root: Path, lease_seconds: int = 3600,
) -> list[Path]:
    """Find claimed items whose lease has expired and move them back to ``queued``.

    A claim is stale if its file mtime is older than ``lease_seconds`` seconds
    ago. Returns the list of recovered file paths (now in their queued home).

    The recovery target is ``queue/queued/<role>/`` based on the claim's
    parent role layout — but spec/06 keeps role inside the file naming. As a
    safe default, this implementation puts recovered files into
    ``queue/queued/_recovered/`` so an operator can re-route them.
    """
    import time

    claimed_root = project_root / "queue" / "claimed"
    if not claimed_root.is_dir():
        return []

    now = time.time()
    recovered: list[Path] = []
    recovered_dir = project_root / "queue" / "queued" / "_recovered"
    recovered_dir.mkdir(parents=True, exist_ok=True)

    for lease_dir in claimed_root.iterdir():
        if not lease_dir.is_dir():
            continue
        for path in lease_dir.iterdir():
            if not path.is_file():
                continue
            try:
                age = now - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age >= lease_seconds:
                target = recovered_dir / path.name
                try:
                    path.rename(target)
                    recovered.append(target)
                except FileNotFoundError:
                    pass
        # Clean up empty lease dirs
        try:
            next(lease_dir.iterdir())
        except StopIteration:
            lease_dir.rmdir()
        except FileNotFoundError:
            pass

    return recovered

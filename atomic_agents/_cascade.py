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

import json
from dataclasses import dataclass
from datetime import datetime, timezone
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
    A sidecar ``<original_name>.lease.json`` lives alongside the work file and
    records explicit lease metadata so ``recover_stale_claims`` can use
    ``lease_expires_at`` rather than file mtime.

    Callers should pass ``self.path.read_text()`` as the work_item content,
    then call ``release_claim()`` or ``move_to_dead_letter()`` when done.
    For long-running work, call ``renew_lease()`` periodically to extend the
    lease before it expires.
    """

    path: Path
    original_name: str
    role: str
    lease_token: str
    claimed_at: float


def _sidecar_path(work_file: Path) -> Path:
    """Return the path of the lease sidecar for a given work file."""
    return work_file.parent / (work_file.name + ".lease.json")


def _write_sidecar(work_file: Path, lease_token: str, lease_seconds: int) -> None:
    """Write a lease sidecar alongside *work_file*."""
    now = datetime.now(tz=timezone.utc)
    expires_at = datetime.fromtimestamp(
        now.timestamp() + lease_seconds, tz=timezone.utc
    )
    sidecar = {
        "lease_token": lease_token,
        "claimed_at": now.isoformat(),
        "lease_expires_at": expires_at.isoformat(),
        "lease_seconds": lease_seconds,
    }
    _sidecar_path(work_file).write_text(json.dumps(sidecar), encoding="utf-8")


def claim_next_queued(
    project_root: Path,
    role: str,
    lease_token: str,
    lease_seconds: int = 3600,
) -> QueueItem | None:
    """Atomically claim the next item from ``project_root/queue/queued/<role>/``.

    Atomicity: uses ``Path.rename`` (POSIX atomic move) to take ownership of
    a file. If two workers race, only one rename succeeds for any given file;
    the loser ``FileNotFoundError``s and tries the next one.

    After a successful rename, a sidecar ``<file>.lease.json`` is written next
    to the claimed file.  The sidecar records ``lease_expires_at`` (an explicit
    ISO-8601 timestamp) so ``recover_stale_claims`` can detect expiry without
    relying on filesystem mtime.

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
        _write_sidecar(dst, lease_token=lease_token, lease_seconds=lease_seconds)
        return QueueItem(
            path=dst,
            original_name=src.name,
            role=role,
            lease_token=lease_token,
            claimed_at=time.time(),
        )
    return None


def release_claim(item: QueueItem, project_root: Path) -> None:
    """Mark a claimed item as completed by moving it to ``queue/done/<lease_token>/``.

    The destination is namespaced by ``lease_token`` so two leases with
    identically-named files do not overwrite each other.  The lease sidecar is
    removed on arrival (the item is done; the lease is no longer relevant).
    """
    done_dir = project_root / "queue" / "done" / item.lease_token
    done_dir.mkdir(parents=True, exist_ok=True)
    item.path.rename(done_dir / item.original_name)
    # Remove sidecar if it still exists in the original claimed location.
    sc = _sidecar_path(item.path)
    if sc.exists():
        sc.unlink(missing_ok=True)


def move_to_dead_letter(
    item: QueueItem, project_root: Path, reason: str = "",
) -> None:
    """Move a claimed item to ``queue/dead-letter/<lease_token>/`` after exhausting retries.

    The destination is namespaced by ``lease_token`` so two leases with
    identically-named files do not overwrite each other.

    A sibling ``.reason.txt`` is written alongside the item when *reason* is
    provided.  The lease sidecar is removed (dead-letter is a terminal state).
    """
    dl_dir = project_root / "queue" / "dead-letter" / item.lease_token
    dl_dir.mkdir(parents=True, exist_ok=True)
    target = dl_dir / item.original_name
    item.path.rename(target)
    if reason:
        (dl_dir / (item.original_name + ".reason.txt")).write_text(reason, encoding="utf-8")
    # Remove sidecar from the (now-moved) claimed location.
    sc = _sidecar_path(item.path)
    if sc.exists():
        sc.unlink(missing_ok=True)


def recover_stale_claims(
    project_root: Path, lease_seconds: int = 3600,
) -> list[Path]:
    """Find claimed items whose lease has expired and move them back to ``queued``.

    **Lease detection order** (for each work file in ``queue/claimed/<token>/``):

    1. If a sidecar ``<file>.lease.json`` exists, read ``lease_expires_at`` and
       compare to *now*.  Recover only if the lease has expired.
    2. If no sidecar exists (legacy claim written before this fix), fall back to
       comparing file mtime against ``lease_seconds``.

    Recovered files are moved to
    ``queue/queued/_recovered/<lease_token>/<filename>`` so an operator can
    identify which lease they came from. Sidecar files are deleted on recovery
    (the recovered item starts fresh; a new sidecar is written on reclaim).

    Returns the list of recovered work-file paths (now in their queued home).
    """
    import time

    claimed_root = project_root / "queue" / "claimed"
    if not claimed_root.is_dir():
        return []

    now = time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    recovered: list[Path] = []

    for lease_dir in claimed_root.iterdir():
        if not lease_dir.is_dir():
            continue
        lease_token = lease_dir.name
        for path in lease_dir.iterdir():
            if not path.is_file():
                continue
            # Skip sidecars — they are handled alongside their work file.
            if path.name.endswith(".lease.json"):
                continue
            sidecar = _sidecar_path(path)
            is_stale = False
            if sidecar.is_file():
                # Explicit sidecar — use lease_expires_at.
                try:
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                    expires_at = datetime.fromisoformat(data["lease_expires_at"])
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    is_stale = expires_at < now_dt
                except (KeyError, ValueError, OSError):
                    # Malformed sidecar — fall back to mtime.
                    try:
                        is_stale = (now - path.stat().st_mtime) >= lease_seconds
                    except FileNotFoundError:
                        continue
            else:
                # Legacy claim (no sidecar) — use mtime.
                try:
                    is_stale = (now - path.stat().st_mtime) >= lease_seconds
                except FileNotFoundError:
                    continue

            if is_stale:
                recovered_dir = (
                    project_root / "queue" / "queued" / "_recovered" / lease_token
                )
                recovered_dir.mkdir(parents=True, exist_ok=True)
                target = recovered_dir / path.name
                try:
                    path.rename(target)
                    # Remove stale sidecar — the recovered item gets a fresh
                    # sidecar if/when it is reclaimed.
                    if sidecar.exists():
                        sidecar.unlink(missing_ok=True)
                    recovered.append(target)
                except FileNotFoundError:
                    pass

        # Clean up empty lease dirs.
        try:
            next(lease_dir.iterdir())
        except StopIteration:
            lease_dir.rmdir()
        except FileNotFoundError:
            pass

    return recovered


def renew_lease(item: QueueItem, additional_seconds: int = None) -> None:
    """Extend the lease for an actively-worked item.

    Updates ``lease_expires_at`` in the sidecar to
    ``now + additional_seconds``.  If *additional_seconds* is ``None``, the
    original ``lease_seconds`` from the sidecar is reused (i.e., the lease is
    reset to full duration from now).

    If no sidecar exists (legacy claim), a new one is written using
    *additional_seconds* (or a default of 3600 if that is also ``None``).

    Long-running workers should call this periodically — recommended cadence is
    every ``lease_seconds / 3`` seconds — to prevent ``recover_stale_claims``
    from reclaiming an actively-worked item.
    """
    sidecar = _sidecar_path(item.path)
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    else:
        data = {}

    lease_secs = additional_seconds
    if lease_secs is None:
        lease_secs = data.get("lease_seconds", 3600)

    now = datetime.now(tz=timezone.utc)
    data["lease_expires_at"] = datetime.fromtimestamp(
        now.timestamp() + lease_secs, tz=timezone.utc
    ).isoformat()
    data.setdefault("lease_token", item.lease_token)
    data.setdefault("claimed_at", now.isoformat())
    data["lease_seconds"] = lease_secs

    sidecar.write_text(json.dumps(data), encoding="utf-8")

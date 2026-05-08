"""Memory versioning — immutable per-mutation snapshots per spec/02.

Storage layout:
  <agent>/memory/.versions/<note-stem>/<timestamp>_<short-hash>.md

- `.versions/` is a hidden dir at the same level as memory notes.
- One subdir per note stem (e.g., `feedback_communication_style`).
- One file per version: `<ISO-ts>_<8-char-sha256>.md`.
- INDEX.md is excluded from versioning (mechanical, not semantic).

Public API:
  snapshot_memory_version  — called internally before any overwrite/merge
  list_versions            — newest-first paths for a note
  read_version             — (frontmatter_dict, body_text) of a snapshot
  restore_version          — atomically replace live note from snapshot
  redact_version           — replace body with [REDACTED], preserve frontmatter

.. deprecated::
    All public functions here are compatibility wrappers that will emit
    DeprecationWarning in v1.0. Use ``agent.memory`` (MemoryBackend) instead.
    The actual implementations have moved to ``memory.filesystem.FilesystemBackend``.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from ._io import atomic_write, atomic_append_jsonl, safe_resolve_under
from .exceptions import PathTraversalError


# Files that must never be versioned.
_EXCLUDED_FILES = {"INDEX.md"}


def _versions_dir(memory_dir: Path, note_stem: str) -> Path:
    """Return the .versions/<stem>/ directory for a note (not created yet)."""
    return memory_dir / ".versions" / note_stem


def _sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _version_filename(content: str) -> str:
    """Return a version filename: <ISO-ts>_<8-char-hash>.md."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    short_hash = _sha256_hex(content)[:8]
    return f"{ts}_{short_hash}.md"


def snapshot_memory_version(target: Path) -> Path | None:
    """Snapshot the current on-disk content of `target` into .versions/.

    Returns the path of the version file written, or None if `target`
    doesn't exist or is INDEX.md.

    .. deprecated:: Use FilesystemBackend internally (via agent.memory).
    """
    # Delegate to filesystem implementation
    from .memory.filesystem import _snapshot
    return _snapshot(target)


def list_versions(memory_dir: Path, note_filename: str) -> list[Path]:
    """Return version paths for a note, newest first.

    `note_filename` is the bare filename (e.g., ``feedback_comm_style.md``).

    .. deprecated:: Use ``agent.memory.list_versions(name)`` instead.
    """
    warnings.warn(
        "list_versions() is deprecated; use agent.memory.list_versions() instead.",
        DeprecationWarning, stacklevel=2,
    )
    from .memory.filesystem import FilesystemBackend

    # Validate path traversal guard (same as before)
    safe_resolve_under(note_filename, memory_dir)

    # Derive agent_root from memory_dir (parent)
    agent_root = memory_dir.parent
    backend = FilesystemBackend(agent_root, memory_dir.name)
    refs = backend.list_versions(note_filename)
    # Return as Path objects for backward compat.
    # backend_id is now "<stem>/<version_filename>" — extract just the filename.
    stem = Path(note_filename).stem
    result = []
    for ref in refs:
        bid = ref.backend_id
        # backend_id encodes stem/filename; extract the filename portion
        if "/" in bid:
            version_filename = bid.split("/", 1)[1]
        else:
            version_filename = bid
        result.append(memory_dir / ".versions" / stem / version_filename)
    return result


def read_version(version_path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text) from a snapshot file.

    .. deprecated:: Use ``agent.memory.read_version(version_ref)`` instead.
    """
    warnings.warn(
        "read_version() is deprecated; use agent.memory.read_version() instead.",
        DeprecationWarning, stacklevel=2,
    )
    parsed = frontmatter.load(version_path)
    return dict(parsed.metadata), parsed.content


def restore_version(
    memory_dir: Path,
    note_filename: str,
    version_path: Path,
    log_target: Path | None = None,
) -> Path:
    """Atomically replace the live note with a snapshot's content.

    .. deprecated:: Use ``agent.memory.restore_version()`` instead.
    """
    warnings.warn(
        "restore_version() is deprecated; use agent.memory.restore_version() instead.",
        DeprecationWarning, stacklevel=2,
    )
    # Guard the live-note write target.
    live_note = safe_resolve_under(note_filename, memory_dir)

    # Guard the version source — must stay inside .versions/.
    versions_root = memory_dir / ".versions"
    safe_resolve_under(version_path, versions_root)

    # Snapshot current live state so restore is reversible.
    snapshot_memory_version(live_note)

    # Write version content to live note.
    version_content = version_path.read_text(encoding="utf-8")
    atomic_write(live_note, version_content)

    # Optional logging.
    if log_target is not None:
        _append_log(log_target, {
            "trigger": "memory_version_restored",
            "note": note_filename,
            "restored_from": str(version_path),
        })

    return live_note


def redact_version(
    version_path: Path,
    replacement: str = "[REDACTED]",
    log_target: Path | None = None,
) -> None:
    """Replace a snapshot's body content with a redaction marker.

    .. deprecated:: Use ``agent.memory.redact_version()`` instead.
    """
    warnings.warn(
        "redact_version() is deprecated; use agent.memory.redact_version() instead.",
        DeprecationWarning, stacklevel=2,
    )
    parsed = frontmatter.load(version_path)
    redacted_post = frontmatter.Post(replacement, **parsed.metadata)
    atomic_write(version_path, frontmatter.dumps(redacted_post) + "\n")

    if log_target is not None:
        _append_log(log_target, {
            "trigger": "memory_version_redacted",
            "version_path": str(version_path),
        })


def _append_log(log_target: Path, data: dict) -> None:
    """Append a JSONL line to log_target."""
    atomic_append_jsonl(log_target, json.dumps(data))

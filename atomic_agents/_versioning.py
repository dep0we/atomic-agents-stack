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
"""

from __future__ import annotations

import hashlib
import json
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
    """Return a version filename: <ISO-ts>_<8-char-hash>.md.

    ISO timestamp is UTC, formatted as YYYYMMDDTHHMMSSffffffZ (microsecond
    precision) to keep it sortable and filesystem-safe (no colons).

    Microsecond precision means two snapshots of identical content taken in the
    same wall-clock second produce distinct filenames, preserving the
    immutable-per-mutation invariant (spec/02).  Second-precision timestamps
    plus content hashing is NOT sufficient because two writes of the same
    content in the same second would collide.
    """
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    short_hash = _sha256_hex(content)[:8]
    return f"{ts}_{short_hash}.md"


def snapshot_memory_version(target: Path) -> Path | None:
    """Snapshot the current on-disk content of `target` into .versions/.

    Returns the path of the version file written, or None if `target`
    doesn't exist (first-time write — no prior content to snapshot).

    INDEX.md is excluded: returns None without reading the file.
    """
    if target.name in _EXCLUDED_FILES:
        return None
    if not target.exists():
        return None

    content = target.read_text(encoding="utf-8")
    stem = target.stem
    versions_dir = _versions_dir(target.parent, stem)
    versions_dir.mkdir(parents=True, exist_ok=True)

    version_name = _version_filename(content)
    version_path = versions_dir / version_name
    # Use atomic_write so the snapshot itself is crash-safe.
    atomic_write(version_path, content)
    return version_path


def list_versions(memory_dir: Path, note_filename: str) -> list[Path]:
    """Return version paths for a note, newest first.

    `note_filename` is the bare filename (e.g., ``feedback_comm_style.md``).
    Returns an empty list if no versions directory exists.

    Raises PathTraversalError if note_filename resolves outside memory_dir
    (guards against CLI args like ``../../persona/IDENTITY.md``).
    """
    # Validate that the note_filename stays inside memory_dir.
    safe_resolve_under(note_filename, memory_dir)

    stem = Path(note_filename).stem
    versions_dir = _versions_dir(memory_dir, stem)
    if not versions_dir.exists():
        return []
    # Filenames sort lexicographically newest-first because they start with
    # the ISO timestamp YYYYMMDDTHHMMSSZ.
    paths = sorted(versions_dir.glob("*.md"), reverse=True)
    return paths


def read_version(version_path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text) from a snapshot file.

    Parses the snapshot as a python-frontmatter Post. If the file has no
    frontmatter at all, frontmatter_dict will be empty.
    """
    parsed = frontmatter.load(version_path)
    return dict(parsed.metadata), parsed.content


def restore_version(
    memory_dir: Path,
    note_filename: str,
    version_path: Path,
    log_target: Path | None = None,
) -> Path:
    """Atomically replace the live note with a snapshot's content.

    Process:
    1. Snapshot the current live state first (so the restore is reversible).
    2. Atomically write the version's content to the live note path.
    3. Log the restoration event if log_target is provided.

    Returns the path of the restored (live) note.

    Raises PathTraversalError if:
    - note_filename resolves outside memory_dir (guards live note write target).
    - version_path resolves outside memory_dir/.versions/ (guards snapshot source).
    """
    # Guard the live-note write target.
    live_note = safe_resolve_under(note_filename, memory_dir)

    # Guard the version source — must stay inside .versions/.
    versions_root = memory_dir / ".versions"
    safe_resolve_under(version_path, versions_root)

    # Step 1: snapshot current live state so restore is reversible.
    snapshot_memory_version(live_note)

    # Step 2: write version content to live note.
    version_content = version_path.read_text(encoding="utf-8")
    atomic_write(live_note, version_content)

    # Step 3: optional logging.
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

    Frontmatter is preserved so the audit trail (who wrote what, when) remains
    intact. Only the body (potentially containing PII or secrets) is removed.
    Used for compliance — never for normal memory operations.
    """
    parsed = frontmatter.load(version_path)
    # Build a new Post with same frontmatter but redacted body.
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

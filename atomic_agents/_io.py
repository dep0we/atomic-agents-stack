"""Atomic file I/O — temp + fsync + rename pattern per spec/04 + spec/03 schema migration.

Used by every write in the package. Never write directly with `path.write_text` from
agent code; always go through `atomic_write` so partial-write states are impossible
on POSIX.
"""

from __future__ import annotations
import os
import tempfile
from pathlib import Path


def atomic_write(target: Path, content: str, encoding: str = "utf-8") -> None:
    """Write `content` to `target` atomically.

    Strategy: write to a temp file in the same directory, fsync, rename.
    Rename is atomic on POSIX — `target` either exists with full contents or
    doesn't exist at all. Crash mid-write leaves a recoverable .tmp file.

    Same-directory tempfile is required because rename across filesystems isn't
    atomic; tempfile.NamedTemporaryFile defaults to /tmp which may be different.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write to a sibling temp file
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Atomic rename
        os.replace(tmp_path, target)
    except Exception:
        # Cleanup the temp file on failure
        try:
            Path(tmp_path).unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_append_jsonl(target: Path, line: str) -> None:
    """Append one JSON line to a JSONL file.

    Append is naturally atomic for small writes on POSIX (single syscall when the
    payload fits in PIPE_BUF). For longer lines, use a lock-and-rewrite pattern
    instead — but for log/eval JSONL lines (typically < 1KB), append is fine.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        if not line.endswith("\n"):
            line = line + "\n"
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def cleanup_stale_tempfiles(directory: Path) -> list[Path]:
    """Find and delete leftover .tmp files from crashed writes.

    Run as part of lint or startup. Returns the list of files cleaned.
    """
    cleaned = []
    if not directory.exists():
        return cleaned
    for path in directory.rglob(".*.tmp"):
        try:
            path.unlink()
            cleaned.append(path)
        except FileNotFoundError:
            pass
    return cleaned

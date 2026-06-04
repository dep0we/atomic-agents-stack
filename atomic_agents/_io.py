"""Atomic file I/O — temp + fsync + rename pattern per spec/04 + spec/03 schema migration.

Used by every write in the package. Never write directly with `path.write_text` from
agent code; always go through `atomic_write` so partial-write states are impossible
on POSIX.

Also exports safe_resolve_under() — the canonical path-traversal guard used wherever
user/operator-controlled input becomes a path component.
"""

from __future__ import annotations
import os
import tempfile
from pathlib import Path

from .exceptions import PathTraversalError


def _fsync_dir(directory: Path) -> None:
    """Fsync the parent directory so the renamed directory entry is durable.

    POSIX requires fsyncing the *directory* after os.replace() to guarantee the
    new entry survives a power loss.  Windows raises OSError when you try to
    fsync a directory — that is expected and harmless; we swallow it.
    """
    # O_DIRECTORY is POSIX-only; fall back to O_RDONLY on platforms that don't
    # define it (some older Linuxes, Windows headers).
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        dir_fd = os.open(str(directory), flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Expected on Windows; silently ignored everywhere else too since
        # a missing dir-fsync degrades to the pre-fix behaviour rather than
        # corrupting data.
        pass


def atomic_write(target: Path, content: str, encoding: str = "utf-8") -> None:
    """Write `content` to `target` atomically.

    Strategy: write to a temp file in the same directory, fsync, rename,
    then fsync the parent directory so the new directory entry is durable.
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
        # Fsync the parent directory so the new directory entry is durable
        # across a power loss.
        _fsync_dir(target.parent)
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


def safe_resolve_under(child: "Path | str", root: Path) -> Path:
    """Resolve `child` under `root` and verify the result stays inside `root`.

    `child` may be a string supplied by a user or operator (roster names, CLI
    filenames, version names).  A value like ``../other`` or ``/etc/passwd``
    would normally escape the intended root after ``root / child``.  This
    helper resolves both sides and enforces containment.

    Returns the resolved absolute Path on success.
    Raises PathTraversalError if the resolved child is not under root.

    Use this anywhere external input becomes a path component.  Examples:
    roster agent names (delegate target), CLI note_filename / version_name.
    """
    resolved = (root / child).resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise PathTraversalError(
            f"path {child!r} resolves outside {root_resolved}",
            child=str(child),
            root=str(root_resolved),
        )
    return resolved


def cleanup_stale_tempfiles(directory: Path) -> list[Path]:
    """Find and delete leftover .tmp files from crashed writes.

    Run as part of lint or startup. Returns the list of files cleaned.

    Note: this uses rglob which scans the entire directory tree recursively.
    Prefer cleanup_stale_tempfiles_for_file when the target file is known;
    that function is scoped to siblings of a specific file rather than the
    full tree.
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


def cleanup_stale_tempfiles_for_file(target: Path) -> list[Path]:
    """Delete leftover .tmp files from crashed atomic_write calls for target.

    Scoped to the parent directory of target, matching only tempfiles that
    atomic_write would have created for this specific file. The pattern is
    `.<name>.<random>.tmp` (the same pattern atomic_write uses when creating
    the temporary file next to the destination).

    Returns the list of files removed. Ignores FileNotFoundError races
    (another process may have already cleaned the same file).

    Use inside a lock so cleanup is serialized with the install/uninstall
    operation that follows.
    """
    cleaned = []
    pattern = f".{target.name}.*.tmp"
    for path in target.parent.glob(pattern):
        try:
            path.unlink()
            cleaned.append(path)
        except FileNotFoundError:
            pass
    return cleaned

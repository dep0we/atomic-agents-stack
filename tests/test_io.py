"""Tests for atomic_agents._io."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from atomic_agents._io import (
    atomic_write,
    atomic_append_jsonl,
    cleanup_stale_tempfiles,
    safe_resolve_under,
    _fsync_dir,
)
from atomic_agents.exceptions import PathTraversalError


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "subdir" / "file.md"
    atomic_write(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_atomic_write_overwrites(tmp_path):
    target = tmp_path / "f.md"
    atomic_write(target, "first")
    atomic_write(target, "second")
    assert target.read_text() == "second"


def test_atomic_write_no_partial_file_on_failure(tmp_path, monkeypatch):
    """If the write raises mid-way, the target should not exist (or be unchanged)."""
    target = tmp_path / "f.md"
    atomic_write(target, "original")

    # Patch os.replace to raise — simulates a rename failure
    original_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        atomic_write(target, "new content")

    # Original content should be intact
    assert target.read_text() == "original"
    # No leftover .tmp files
    tmp_files = list(tmp_path.rglob(".*.tmp"))
    assert tmp_files == []


def test_atomic_append_jsonl(tmp_path):
    target = tmp_path / "log.jsonl"
    atomic_append_jsonl(target, json.dumps({"a": 1}))
    atomic_append_jsonl(target, json.dumps({"b": 2}))
    lines = target.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2}


def test_atomic_append_jsonl_adds_newline_if_missing(tmp_path):
    target = tmp_path / "log.jsonl"
    atomic_append_jsonl(target, '{"a": 1}')  # no trailing newline
    atomic_append_jsonl(target, '{"b": 2}')
    text = target.read_text()
    assert text == '{"a": 1}\n{"b": 2}\n'


def test_cleanup_stale_tempfiles(tmp_path):
    (tmp_path / "real.md").write_text("keep me")
    (tmp_path / ".real.md.abc.tmp").write_text("stale")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / ".other.md.xyz.tmp").write_text("also stale")

    cleaned = cleanup_stale_tempfiles(tmp_path)
    assert len(cleaned) == 2
    assert not (tmp_path / ".real.md.abc.tmp").exists()
    assert not (tmp_path / "subdir" / ".other.md.xyz.tmp").exists()
    assert (tmp_path / "real.md").exists()


# --- P2 regression test: directory fsync after os.replace ---


def test_atomic_write_fsync_directory_after_replace(tmp_path):
    """atomic_write must call os.fsync on the parent directory fd after rename."""
    target = tmp_path / "output.md"

    fsync_calls = []

    original_open = os.open
    original_fsync = os.fsync
    original_close = os.close

    dir_fd_seen = []

    def tracking_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        # Record the fd if it corresponds to our parent directory.
        if str(path) == str(tmp_path):
            dir_fd_seen.append(fd)
        return fd

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return original_fsync(fd)

    with mock.patch("os.open", side_effect=tracking_open), \
         mock.patch("os.fsync", side_effect=tracking_fsync):
        atomic_write(target, "content")

    assert target.read_text() == "content"
    # The parent directory fd must appear in the fsync calls.
    assert dir_fd_seen, "os.open was never called on the parent directory"
    assert any(fd in fsync_calls for fd in dir_fd_seen), (
        "os.fsync was never called on the parent directory fd"
    )


def test_fsync_dir_swallows_oserror(tmp_path):
    """_fsync_dir must not propagate OSError (Windows-style)."""
    with mock.patch("os.fsync", side_effect=OSError("cannot fsync dir on Windows")):
        # Should complete without raising.
        _fsync_dir(tmp_path)


# ──────────────────────────────────────────────────────────────────────────────
# safe_resolve_under — path-traversal guard (codex R2-B regression tests)


def test_safe_resolve_under_blocks_dotdot(tmp_path):
    """safe_resolve_under raises PathTraversalError for ../ escapes."""
    root = tmp_path / "agents"
    root.mkdir()
    with pytest.raises(PathTraversalError, match="resolves outside"):
        safe_resolve_under("../escape", root)


def test_safe_resolve_under_blocks_absolute_path(tmp_path):
    """safe_resolve_under raises PathTraversalError for absolute paths that escape root."""
    root = tmp_path / "agents"
    root.mkdir()
    with pytest.raises(PathTraversalError, match="resolves outside"):
        safe_resolve_under("/etc/passwd", root)


def test_safe_resolve_under_allows_normal(tmp_path):
    """safe_resolve_under returns the resolved path for a normal child name."""
    root = tmp_path / "agents"
    root.mkdir()
    result = safe_resolve_under("editor", root)
    assert result == (root / "editor").resolve()
    assert str(result).startswith(str(root.resolve()))


def test_safe_resolve_under_allows_nested(tmp_path):
    """safe_resolve_under allows sub-paths that stay inside root."""
    root = tmp_path / "memory"
    root.mkdir()
    result = safe_resolve_under("subdir/note.md", root)
    assert result == (root / "subdir" / "note.md").resolve()


def test_safe_resolve_under_error_carries_child_and_root(tmp_path):
    """PathTraversalError exposes child and root attributes."""
    root = tmp_path / "agents"
    root.mkdir()
    with pytest.raises(PathTraversalError) as exc_info:
        safe_resolve_under("../bad", root)
    assert exc_info.value.child == "../bad"
    assert str(root.resolve()) in exc_info.value.root

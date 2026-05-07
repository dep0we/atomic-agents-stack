"""Tests for atomic_agents._io."""

import json
from pathlib import Path

import pytest

from atomic_agents._io import (
    atomic_write,
    atomic_append_jsonl,
    cleanup_stale_tempfiles,
)


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
    import os
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

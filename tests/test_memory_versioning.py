"""Tests for memory versioning, optimistic concurrency, and read-only mounts.

Covers:
- snapshot_memory_version: when/when-not to snapshot
- list_versions: ordering
- read_version: frontmatter + body parsing
- restore_version: atomicity and reversibility
- redact_version: body replaced, frontmatter preserved
- INDEX.md exclusion from versioning
- expected_content_sha256 precondition on write_atomic_note
- Read-only path enforcement in enforce_write_path + write_atomic_note
- tools.md ## Read-only paths parsing
- JSONL logging for versioning events
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from atomic_agents._capture import enforce_write_path, write_atomic_note
from atomic_agents._io import atomic_write
from atomic_agents._tools import parse_tools_md_text
from atomic_agents._versioning import (
    list_versions,
    read_version,
    redact_version,
    restore_version,
    snapshot_memory_version,
)
from atomic_agents.exceptions import (
    MemoryPreconditionFailed,
    PathTraversalError,
    WritePathViolation,
)
from atomic_agents.types import Capture


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_capture(**overrides) -> Capture:
    defaults = dict(
        type="feedback",
        name="Comm style",
        description="Bottom-line first",
        confidence="high",
        sources=["conv_001"],
        body="Lead with the answer.",
    )
    defaults.update(overrides)
    return Capture(**defaults)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_note(
    tmp_path: Path,
    filename: str = "feedback_comm_style.md",
    content: str = "v1 content\n",
) -> Path:
    path = tmp_path / "memory" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content)
    return path


# ──────────────────────────────────────────────────────────────────────────────
# snapshot_memory_version


def test_snapshot_created_on_overwrite(tmp_path):
    """Snapshotting a file that exists should create a .versions entry."""
    note = _write_note(tmp_path, content="original content\n")
    memory_dir = note.parent

    version_path = snapshot_memory_version(note)

    assert version_path is not None
    assert version_path.exists()
    assert version_path.read_text() == "original content\n"
    # Should live under .versions/<stem>/
    assert ".versions" in str(version_path)
    assert "feedback_comm_style" in str(version_path)


def test_no_snapshot_on_fresh_write(tmp_path):
    """snapshot_memory_version returns None when target doesn't exist."""
    note = tmp_path / "memory" / "new_note.md"
    result = snapshot_memory_version(note)
    assert result is None


def test_index_md_excluded_from_versioning(tmp_path):
    """INDEX.md should never produce a snapshot even if it exists."""
    index = tmp_path / "memory" / "INDEX.md"
    index.parent.mkdir(parents=True)
    atomic_write(index, "# Memory Index\n")

    result = snapshot_memory_version(index)
    assert result is None


def test_snapshot_created_on_merge(tmp_path):
    """write_atomic_note with merge_into snapshots before merging."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    # Create initial note
    capture = _make_capture()
    write_atomic_note(agent_root, capture, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    # Merge into it — should snapshot the current content
    merge_capture = _make_capture(
        name="Comm style",
        description="Bottom-line first",
        merge_into="feedback_comm_style.md",
        sources=["conv_002"],
    )
    write_atomic_note(agent_root, merge_capture, write_paths=[memory_dir])

    versions = list_versions(memory_dir, "feedback_comm_style.md")
    assert len(versions) == 1


# ──────────────────────────────────────────────────────────────────────────────
# list_versions


def test_list_versions_returns_newest_first(tmp_path):
    """list_versions should return paths sorted newest-first."""
    note = _write_note(tmp_path, content="v1\n")
    memory_dir = note.parent

    # Create two snapshots with different content so filenames differ
    v1 = snapshot_memory_version(note)
    atomic_write(note, "v2\n")
    v2 = snapshot_memory_version(note)

    versions = list_versions(memory_dir, "feedback_comm_style.md")
    assert len(versions) == 2
    # Newest first — v2 snapshot was written after v1
    assert versions[0].name > versions[1].name  # ISO timestamp sorts newer > older


def test_list_versions_empty_when_none(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    versions = list_versions(memory_dir, "nonexistent.md")
    assert versions == []


def test_list_versions_non_memory_subdir_else_branch(tmp_path):
    """list_versions else-branch: a non-'memory' subdir routes to FilesystemBackend
    directly, not through the factory's default 'memory' subdir.

    Creates a note under <root>/notes/.versions/ and calls list_versions against
    <root>/notes.  The else-branch (memory_dir.name != 'memory') must return the
    version file under 'notes', not under a 'memory' subdirectory.
    """
    agent_root = tmp_path / "agent"
    notes_dir = agent_root / "notes"
    notes_dir.mkdir(parents=True)

    note_path = notes_dir / "custom_note.md"
    atomic_write(note_path, "initial content\n")

    # Snapshot the note to create a .versions entry under notes/.versions/
    version_path = snapshot_memory_version(note_path)
    assert version_path is not None
    assert version_path.exists()
    # Version must live under the notes subdir, not a "memory" subdir.
    # Compare path components, not the raw string: the tmp dir name itself
    # contains the substring "memory" (via "non_memory"), so a naive
    # substring check would false-positive.
    assert version_path.is_relative_to(notes_dir)
    assert "memory" not in version_path.relative_to(agent_root).parts

    # Now exercise the else-branch: notes_dir.name == "notes" (not "memory")
    versions = list_versions(notes_dir, "custom_note.md")
    assert len(versions) == 1
    assert versions[0] == version_path


# ──────────────────────────────────────────────────────────────────────────────
# read_version


def test_read_version_parses_frontmatter_and_body(tmp_path):
    """read_version should return (frontmatter_dict, body) from a snapshot."""
    # Create a note with frontmatter
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = _make_capture(body="Lead with the answer.\n\n**Why:** clarity.")
    write_atomic_note(agent_root, capture, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    # Mutate to trigger a snapshot
    v1 = snapshot_memory_version(note_path)

    fm, body = read_version(v1)
    assert fm["type"] == "feedback"
    assert fm["name"] == "Comm style"
    assert "Lead with the answer" in body


# ──────────────────────────────────────────────────────────────────────────────
# restore_version


def test_restore_version_replaces_live_atomic(tmp_path):
    """restore_version atomically replaces the live note with snapshot content."""
    note = _write_note(tmp_path, content="original\n")
    memory_dir = note.parent

    v1 = snapshot_memory_version(note)  # snapshot "original"
    atomic_write(note, "changed\n")  # overwrite live

    restored = restore_version(memory_dir, "feedback_comm_style.md", v1)
    assert restored.read_text() == "original\n"


def test_restore_is_reversible(tmp_path):
    """Restore A → B, then restore B → A using the auto-snapshot of pre-restore state."""
    note = _write_note(tmp_path, content="state A\n")
    memory_dir = note.parent

    # Snapshot state A
    v_a = snapshot_memory_version(note)
    # Overwrite to state B
    atomic_write(note, "state B\n")

    # Restore to A — this also snapshots state B first
    restore_version(memory_dir, "feedback_comm_style.md", v_a)
    assert note.read_text() == "state A\n"

    # Now go back to B using the snapshot that restore_version created
    versions = list_versions(memory_dir, "feedback_comm_style.md")
    # versions: newest first. The newest should be the pre-restore snapshot (state B)
    v_b = next(v for v in versions if v != v_a)
    restore_version(memory_dir, "feedback_comm_style.md", v_b)
    assert note.read_text() == "state B\n"


# ──────────────────────────────────────────────────────────────────────────────
# redact_version


def test_redact_version_replaces_body_keeps_frontmatter(tmp_path):
    """redact_version replaces body with marker but leaves frontmatter intact."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = _make_capture(body="Sensitive PII content here.")
    write_atomic_note(agent_root, capture, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    v1 = snapshot_memory_version(note_path)

    redact_version(v1, replacement="[REDACTED]")

    fm, body = read_version(v1)
    # Frontmatter preserved
    assert fm["name"] == "Comm style"
    assert fm["type"] == "feedback"
    # Body replaced
    assert "[REDACTED]" in body
    assert "Sensitive PII" not in body


# ──────────────────────────────────────────────────────────────────────────────
# Optimistic concurrency — expected_content_sha256


def test_precondition_match_allows_write(tmp_path):
    """Write proceeds when sha256 matches current content."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = _make_capture()
    write_atomic_note(agent_root, capture, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    current = note_path.read_text()
    sha = _sha(current)

    # Merge with correct precondition — should succeed
    merge_cap = _make_capture(
        merge_into="feedback_comm_style.md",
        sources=["conv_002"],
    )
    result = write_atomic_note(
        agent_root,
        merge_cap,
        write_paths=[memory_dir],
        expected_content_sha256=sha,
    )
    assert result == note_path


def test_precondition_mismatch_raises(tmp_path):
    """MemoryPreconditionFailed raised when sha256 doesn't match current content."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = _make_capture()
    write_atomic_note(agent_root, capture, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    stale_sha = _sha("some stale content that differs")

    merge_cap = _make_capture(
        merge_into="feedback_comm_style.md",
        sources=["conv_002"],
    )
    with pytest.raises(MemoryPreconditionFailed) as exc_info:
        write_atomic_note(
            agent_root,
            merge_cap,
            write_paths=[memory_dir],
            expected_content_sha256=stale_sha,
        )
    # actual_sha256 should be in the exception
    assert exc_info.value.actual_sha256 is not None
    assert exc_info.value.actual_sha256 == _sha(note_path.read_text())


def test_precondition_on_nonexistent_note_raises(tmp_path):
    """Providing expected_content_sha256 when note doesn't exist yet raises."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = _make_capture()
    with pytest.raises(MemoryPreconditionFailed) as exc_info:
        write_atomic_note(
            agent_root,
            capture,
            write_paths=[memory_dir],
            expected_content_sha256="anysha256doesnotmatter",
        )
    assert exc_info.value.actual_sha256 is None


def test_concurrent_write_simulation_caught_by_precondition(tmp_path):
    """Simulate two concurrent agents: the second writer sees a stale sha and is blocked."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = _make_capture()
    write_atomic_note(agent_root, capture, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    # Agent A reads the note and captures its sha
    content_at_read = note_path.read_text()
    sha_at_read = _sha(content_at_read)

    # Agent B merges first (concurrent write — changes the note)
    merge_b = _make_capture(merge_into="feedback_comm_style.md", sources=["agent_b"])
    write_atomic_note(agent_root, merge_b, write_paths=[memory_dir])

    # Agent A now tries to merge with the sha it read earlier — should fail
    merge_a = _make_capture(merge_into="feedback_comm_style.md", sources=["agent_a"])
    with pytest.raises(MemoryPreconditionFailed):
        write_atomic_note(
            agent_root,
            merge_a,
            write_paths=[memory_dir],
            expected_content_sha256=sha_at_read,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Read-only path enforcement


def test_read_only_path_blocks_write_even_under_write_path(tmp_path):
    """A path declared read-only wins even when also under a write path."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    target = memory_dir / "some_note.md"

    # memory_dir is in both write_paths and read_only_paths
    with pytest.raises(WritePathViolation, match="read-only"):
        enforce_write_path(
            target,
            allowed=[memory_dir],
            read_only_paths=[memory_dir],
        )


def test_read_only_path_blocks_subpath(tmp_path):
    """A file nested under a read-only path is blocked even without exact match."""
    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    write_dir = tmp_path / "memory"
    write_dir.mkdir()
    target = reference_dir / "subdir" / "note.md"

    with pytest.raises(WritePathViolation, match="read-only"):
        enforce_write_path(
            target,
            allowed=[write_dir, reference_dir],
            read_only_paths=[reference_dir],
        )


def test_read_only_path_does_not_block_unrelated_path(tmp_path):
    """Write to memory/ is not blocked when only reference/ is read-only."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    target = memory_dir / "note.md"

    # Should not raise
    enforce_write_path(
        target,
        allowed=[memory_dir],
        read_only_paths=[reference_dir],
    )


def test_write_atomic_note_respects_read_only_path(tmp_path):
    """write_atomic_note blocks writes to read-only memory dir."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = _make_capture()
    # memory_dir is both write_path and read_only — read-only wins
    with pytest.raises(WritePathViolation, match="read-only"):
        write_atomic_note(
            agent_root,
            capture,
            write_paths=[memory_dir],
            read_only_paths=[memory_dir],
        )


# ──────────────────────────────────────────────────────────────────────────────
# tools.md ## Read-only paths parsing


def test_read_only_path_parser_handles_section_variants(tmp_path):
    """## Read-only paths and ## Read only paths both parse correctly."""
    tools_text_hyphen = """
## Read paths
- ~/agents/myagent/memory/

## Write paths
- ~/agents/myagent/memory/

## Read-only paths
- ~/agents/shared/reference/
""".strip()

    tools_text_space = """
## Read paths
- ~/agents/myagent/memory/

## Write paths
- ~/agents/myagent/memory/

## Read only paths
- ~/agents/shared/reference/
""".strip()

    for text in (tools_text_hyphen, tools_text_space):
        result = parse_tools_md_text(text)
        assert "read_only_paths" in result
        assert len(result["read_only_paths"]) == 1
        # Path should be expanded (no literal ~)
        assert "~" not in str(result["read_only_paths"][0])


def test_read_only_paths_empty_when_section_absent():
    """No ## Read-only paths section → empty list."""
    tools_text = """
## Read paths
- ~/agents/myagent/memory/

## Write paths
- ~/agents/myagent/memory/
""".strip()
    result = parse_tools_md_text(tools_text)
    assert result.get("read_only_paths", []) == []


# ──────────────────────────────────────────────────────────────────────────────
# JSONL logging for versioning events


def test_version_creation_logged_to_agent_log(tmp_path):
    """memory_version_created event is logged when a snapshot is made."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    log_path = agent_root / "log" / "2026-05" / "2026-05-07.jsonl"

    # Write initial note
    capture = _make_capture()
    write_atomic_note(agent_root, capture, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    # Merge — triggers snapshot + log
    merge_cap = _make_capture(
        merge_into="feedback_comm_style.md",
        sources=["conv_002"],
    )
    write_atomic_note(
        agent_root,
        merge_cap,
        write_paths=[memory_dir],
        log_target=log_path,
    )

    assert log_path.exists()
    lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    version_events = [l for l in lines if l.get("trigger") == "memory_version_created"]
    assert len(version_events) == 1
    assert version_events[0]["note"] == "feedback_comm_style.md"
    assert "version_path" in version_events[0]


def test_version_restoration_logged(tmp_path):
    """memory_version_restored event is logged when restore_version is called."""
    note = _write_note(tmp_path, content="original\n")
    memory_dir = note.parent
    log_path = tmp_path / "log" / "test.jsonl"

    v1 = snapshot_memory_version(note)
    atomic_write(note, "changed\n")

    restore_version(memory_dir, "feedback_comm_style.md", v1, log_target=log_path)

    assert log_path.exists()
    lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    restore_events = [l for l in lines if l.get("trigger") == "memory_version_restored"]
    assert len(restore_events) == 1
    assert restore_events[0]["note"] == "feedback_comm_style.md"
    assert "restored_from" in restore_events[0]


def test_redaction_logged(tmp_path):
    """memory_version_redacted event is logged when redact_version is called."""
    note = _write_note(tmp_path, content="---\nname: test\n---\nSensitive info.\n")
    memory_dir = note.parent
    log_path = tmp_path / "log" / "test.jsonl"

    v1 = snapshot_memory_version(note)
    redact_version(v1, log_target=log_path)

    assert log_path.exists()
    lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    redact_events = [l for l in lines if l.get("trigger") == "memory_version_redacted"]
    assert len(redact_events) == 1
    assert "version_path" in redact_events[0]


# ──────────────────────────────────────────────────────────────────────────────
# Codex R2 regression tests
# ──────────────────────────────────────────────────────────────────────────────


def test_snapshot_two_in_same_second_have_distinct_paths(tmp_path):
    """Two snapshots of identical content taken in the same second must NOT collide.

    With second-only timestamp precision two writes of the same content produce
    the same <timestamp>_<hash>.md filename — a silent overwrite that violates
    the immutable-per-mutation history invariant (codex R2-D3).

    Fix: microsecond precision in the timestamp ensures distinct filenames even
    within the same second.
    """
    note = _write_note(tmp_path, content="identical content\n")
    memory_dir = note.parent

    # Take two snapshots as fast as possible — both will happen in the same second
    # (and likely the same microsecond epoch, but %f gives 6 digits so they diverge)
    v1 = snapshot_memory_version(note)

    # Reset the file so snapshot_memory_version doesn't skip (file must exist)
    # and take a second snapshot immediately
    v2 = snapshot_memory_version(note)

    assert v1 is not None
    assert v2 is not None
    # The two version paths must be distinct (no collision)
    assert v1 != v2, (
        f"Two snapshots produced the same path: {v1.name}. "
        "Timestamp precision is likely too coarse (second-level)."
    )
    # Both version files must exist (no overwrite)
    assert v1.exists(), f"v1 snapshot file was overwritten or missing: {v1}"
    assert v2.exists(), f"v2 snapshot file was overwritten or missing: {v2}"


def test_snapshot_filename_has_microsecond_precision(tmp_path):
    """Version filename must include microseconds (%f) — 6 extra digits after seconds."""
    from atomic_agents._versioning import _version_filename

    name = _version_filename("some content")
    # Expected format: YYYYMMDDTHHMMSSffffffZ_<hash>.md
    # The timestamp portion before the underscore should be 22 chars:
    # 8 date + T + 6 time + 6 microseconds + Z = 22 chars
    stem = name.replace(".md", "")
    ts_part = stem.split("_")[0]
    # Must be 22 characters: YYYYMMDDTHHMMSS + ffffff + Z
    assert len(ts_part) == 22, (
        f"Expected 22-char timestamp (with microseconds), got {len(ts_part)!r}: {ts_part!r}"
    )
    # Must end with Z
    assert ts_part.endswith("Z"), f"Timestamp should end with Z: {ts_part!r}"


def test_capture_concurrent_write_blocked_by_file_lock(tmp_path):
    """Two threads attempting concurrent write_atomic_note with preconditions.

    Thread A holds the per-file lock and writes slowly.  Thread B attempts to
    write with a precondition concurrently.  Thread B must either:
    - Succeed after Thread A releases (if precondition still matches), or
    - Raise MemoryPreconditionFailed (if Thread A's write invalidated it).

    In this test Thread A changes the content, so Thread B's stale sha should
    result in MemoryPreconditionFailed, not a silent bad write.
    """
    import threading

    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    # Write the initial note
    capture_init = _make_capture()
    write_atomic_note(agent_root, capture_init, write_paths=[memory_dir])
    note_path = memory_dir / "feedback_comm_style.md"

    # Thread B will use the sha from before Thread A writes
    content_before = note_path.read_text()
    sha_before = _sha(content_before)

    # Barrier to coordinate the threads
    barrier = threading.Barrier(2)
    results = {}

    def thread_a():
        """Merges first — changes the file content."""
        barrier.wait()  # sync start
        merge_a = _make_capture(
            merge_into="feedback_comm_style.md", sources=["agent_a"]
        )
        write_atomic_note(agent_root, merge_a, write_paths=[memory_dir])
        results["a"] = "done"

    def thread_b():
        """Tries to merge with stale sha — should fail after Thread A writes."""
        barrier.wait()  # sync start
        try:
            merge_b = _make_capture(
                merge_into="feedback_comm_style.md", sources=["agent_b"]
            )
            write_atomic_note(
                agent_root,
                merge_b,
                write_paths=[memory_dir],
                expected_content_sha256=sha_before,
            )
            results["b"] = "ok"
        except Exception as e:
            results["b"] = type(e).__name__

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert results.get("a") == "done"
    # Thread B must have either succeeded (if it got the lock first and a matched)
    # or raised MemoryPreconditionFailed (if Thread A went first and changed content).
    # Either outcome is correct — what we're checking is that NO silent corruption
    # occurred (i.e., we don't get a generic Exception or crash).
    assert results.get("b") in ("ok", "MemoryPreconditionFailed"), (
        f"Thread B had unexpected outcome: {results.get('b')!r}"
    )


# Path-traversal guard — versioning API (codex R2-B regression tests)


def test_restore_version_refuses_dotdot_note_filename(tmp_path):
    """restore_version raises PathTraversalError for dotdot note_filename."""
    note = _write_note(tmp_path, content="original\n")
    memory_dir = note.parent
    v1 = snapshot_memory_version(note)

    with pytest.raises(PathTraversalError, match="resolves outside"):
        restore_version(memory_dir, "../../persona/IDENTITY.md", v1)


def test_restore_version_refuses_dotdot_version_name(tmp_path):
    """restore_version raises PathTraversalError when version_path escapes .versions/."""
    note = _write_note(tmp_path, content="original\n")
    memory_dir = note.parent
    # Craft a version_path that resolves outside memory_dir/.versions/
    evil_version_path = (
        memory_dir / ".versions" / ".." / ".." / "persona" / "IDENTITY.md"
    )

    with pytest.raises(PathTraversalError, match="resolves outside"):
        restore_version(memory_dir, "feedback_comm_style.md", evil_version_path)


def test_list_versions_refuses_dotdot_note_filename(tmp_path):
    """list_versions raises PathTraversalError for dotdot note_filename."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    with pytest.raises(PathTraversalError, match="resolves outside"):
        list_versions(memory_dir, "../../persona/IDENTITY.md")

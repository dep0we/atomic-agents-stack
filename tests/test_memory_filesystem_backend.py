"""Filesystem-specific tests for FilesystemBackend.

These 10 tests cover behaviors that are inherently tied to the on-disk layout
of the FilesystemBackend (not part of the MemoryBackend protocol contract):

  1. .versions/ directory layout — subdirs named after note stems
  2. Version filenames match <ISO-ts>_<8-char-hash>.md pattern
  3. INDEX.md exact format — section headers, entry line format
  4. INDEX.md is never versioned
  5. INDEX.md entry is updated idempotently on re-write
  6. Path enforcement rejects real filesystem paths that escape memory dir
  7. Staging directory created under dreams/.staging-<uuid>/memory/
  8. apply_staging swaps directories atomically; old notes absent after apply
  9. discard_staging removes staging directory from disk
 10. version_count and last_mutation_at reflect on-disk .versions/ content

See docs/spec/20-memory-backend.md §8 for layout spec.
"""

from __future__ import annotations

import re
from datetime import timezone
from pathlib import Path

import frontmatter
import pytest

from atomic_agents.memory.backend import WritePolicy
from atomic_agents.memory.filesystem import FilesystemBackend
from atomic_agents.exceptions import WritePathViolation
from atomic_agents.types import Capture


# ──────────────────────────────────────────────────────────────────
# Helpers

def _make_capture(
    *,
    name: str = "Test note",
    type_: str = "feedback",
    description: str = "A test note",
    body: str = "Body text.",
    merge_into: str | None = None,
    pinned: bool = False,
) -> Capture:
    return Capture(
        type=type_,
        name=name,
        description=description,
        confidence="high",
        sources=["unit_test"],
        body=body,
        merge_into=merge_into,
        pinned=pinned,
    )


def _make_backend(tmp_path: Path) -> tuple[Path, FilesystemBackend, WritePolicy]:
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])
    return agent_root, backend, policy


# ──────────────────────────────────────────────────────────────────
# 1. .versions/ directory layout — subdirs named after note stems

def test_fs_01_versions_dir_layout(tmp_path):
    """.versions/ subdir is named after the note stem, not the full filename."""
    agent_root, backend, policy = _make_backend(tmp_path)

    # Write once (fresh), then merge to trigger a snapshot
    ref = backend.write_note(_make_capture(name="Layout test"), policy)
    # merge_into the exact filename to overwrite and snapshot
    backend.write_note(
        _make_capture(name="Layout test", body="Updated body.", merge_into=ref.name),
        policy,
    )

    stem = Path(ref.name).stem
    versions_dir = agent_root / "memory" / ".versions" / stem
    assert versions_dir.is_dir(), f"Expected .versions/{stem}/ to exist"
    version_files = list(versions_dir.iterdir())
    assert len(version_files) >= 1, "Expected at least one snapshot in .versions/<stem>/"


# ──────────────────────────────────────────────────────────────────
# 2. Version filenames match <ISO-ts>_<8-char-hash>.md pattern

def test_fs_02_version_filename_format(tmp_path):
    """Version filenames are <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md."""
    agent_root, backend, policy = _make_backend(tmp_path)

    ref = backend.write_note(_make_capture(name="Fname test"), policy)
    # Force a snapshot by merging (merge always snapshots before overwriting)
    backend.write_note(
        _make_capture(name="Fname test", body="Changed body.", merge_into=ref.name),
        policy,
    )

    stem = Path(ref.name).stem
    versions_dir = agent_root / "memory" / ".versions" / stem
    for vfile in versions_dir.iterdir():
        assert re.match(
            r"^\d{8}T\d{6}\d+Z_[0-9a-f]{8}\.md$", vfile.name
        ), f"Version filename {vfile.name!r} doesn't match expected pattern"


# ──────────────────────────────────────────────────────────────────
# 3. INDEX.md exact format — section header and entry line

def test_fs_03_index_md_exact_format(tmp_path):
    """INDEX.md has '# Memory Index' title, '## <Section>' headers, and
    '- [name](filename) — description' entry lines."""
    agent_root, backend, policy = _make_backend(tmp_path)

    backend.write_note(
        _make_capture(
            name="User communication style",
            type_="user",
            description="Prefers direct, concise answers",
        ),
        policy,
    )

    index_path = agent_root / "memory" / "INDEX.md"
    assert index_path.exists()
    text = index_path.read_text()

    # Must start with the standard title
    assert text.startswith("# Memory Index"), "INDEX.md must start with '# Memory Index'"

    # Must have a section header for 'user' type
    assert "## User Profile" in text, "Missing '## User Profile' section"

    # Must have a properly-formatted link line
    assert re.search(
        r"- \[User communication style\]\(.+\.md\) — Prefers direct, concise answers",
        text,
    ), "Missing properly-formatted entry line in INDEX.md"


# ──────────────────────────────────────────────────────────────────
# 4. INDEX.md is never versioned

def test_fs_04_index_md_not_versioned(tmp_path):
    """INDEX.md must never appear in .versions/."""
    agent_root, backend, policy = _make_backend(tmp_path)

    # Write several notes to trigger index updates
    refs = []
    for i in range(3):
        ref = backend.write_note(
            _make_capture(name=f"Note {i}", description=f"Desc {i}"),
            policy,
        )
        refs.append(ref)
    # Merge into each note to trigger snapshots
    for i, ref in enumerate(refs):
        backend.write_note(
            _make_capture(name=f"Note {i}", description=f"Desc {i}",
                          body="New body.", merge_into=ref.name),
            policy,
        )

    versions_root = agent_root / "memory" / ".versions"
    if versions_root.exists():
        for subdir in versions_root.iterdir():
            assert subdir.name != "INDEX", (
                "INDEX.md should never be versioned; found .versions/INDEX/"
            )


# ──────────────────────────────────────────────────────────────────
# 5. INDEX.md entry updated idempotently on re-write

def test_fs_05_index_md_idempotent_entry(tmp_path):
    """Writing the same note twice must not create duplicate INDEX.md entries."""
    agent_root, backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Idempotent note", description="Same every time")
    backend.write_note(capture, policy)
    backend.write_note(capture, policy)  # exact same capture (orphan detection)

    index_text = (agent_root / "memory" / "INDEX.md").read_text()
    # Count occurrences of the note name link
    occurrences = index_text.count("[Idempotent note]")
    assert occurrences == 1, (
        f"Expected exactly 1 INDEX.md entry for the note; found {occurrences}"
    )


# ──────────────────────────────────────────────────────────────────
# 6. Path enforcement rejects filesystem paths escaping memory dir

def test_fs_06_path_enforcement_real_paths(tmp_path):
    """write_note with merge_into using a path that escapes the memory dir
    must raise WritePathViolation (not an OSError or silent pass)."""
    agent_root, backend, policy = _make_backend(tmp_path)

    # Write a target note first
    backend.write_note(_make_capture(name="Legitimate note"), policy)

    escape_capture = _make_capture(
        name="Escape attempt",
        merge_into="../../etc/passwd",
    )
    with pytest.raises(WritePathViolation):
        backend.write_note(escape_capture, policy)


# ──────────────────────────────────────────────────────────────────
# 7. Staging directory created under dreams/.staging-<uuid>/memory/

def test_fs_07_staging_dir_location(tmp_path):
    """create_staging() places the staging area under dreams/.staging-<uuid>/memory/."""
    agent_root, backend, policy = _make_backend(tmp_path)

    staging = backend.create_staging()
    try:
        dreams_dir = agent_root / "dreams"
        assert dreams_dir.is_dir(), "dreams/ directory must be created"

        # Find the staging subdir
        staging_subdirs = [p for p in dreams_dir.iterdir() if p.name.startswith(".staging-")]
        assert len(staging_subdirs) == 1, "Expected exactly one .staging-<uuid> subdir"
        staging_memory = staging_subdirs[0] / "memory"
        assert staging_memory.is_dir(), ".staging-<uuid>/memory/ must exist"
    finally:
        backend.discard_staging(staging)


# ──────────────────────────────────────────────────────────────────
# 8. apply_staging swaps directories; original notes absent after apply

def test_fs_08_apply_staging_swaps_directories(tmp_path):
    """After apply_staging, the live memory dir contains only staged notes."""
    agent_root, backend, policy = _make_backend(tmp_path)

    # Write a live note to the real backend
    backend.write_note(_make_capture(name="Old note", description="Pre-staging"), policy)
    old_notes = {r.name for r in backend.list_notes()}
    assert old_notes, "Should have at least one live note before staging"

    # Create staging and write a completely different note
    staging = backend.create_staging()
    staging_policy = WritePolicy(
        write_paths=[agent_root / "dreams" / Path(staging.backend_id).name / "memory"]
        if "/" not in staging.backend_id
        else [Path(staging.backend_id) / "memory"]
    )
    # Use the staging write_note directly via FilesystemStagedMemory interface
    staging.write_note(
        _make_capture(name="New staged note", description="Post-staging"),
        WritePolicy(write_paths=[agent_root / "dreams" / staging.backend_id / "memory"]),
    )

    backend.apply_staging(staging, policy)

    # After apply, only staged notes should be live
    live_names = {r.name for r in backend.list_notes()}
    assert not old_notes.intersection(live_names), (
        "Old notes should be absent after staging apply"
    )
    # The staged note should be present
    assert any("new_staged_note" in n or "new-staged-note" in n for n in live_names), (
        "Staged note should be present after apply"
    )


# ──────────────────────────────────────────────────────────────────
# 9. discard_staging removes staging directory from disk

def test_fs_09_discard_staging_cleanup(tmp_path):
    """discard_staging() removes the staging directory from disk."""
    agent_root, backend, policy = _make_backend(tmp_path)

    staging = backend.create_staging()

    # The staging dir should exist right after creation
    dreams_dir = agent_root / "dreams"
    staging_subdirs_before = [
        p for p in dreams_dir.iterdir() if p.name.startswith(".staging-")
    ]
    assert len(staging_subdirs_before) == 1

    backend.discard_staging(staging)

    # After discard, the staging dir must be gone
    staging_subdirs_after = [
        p for p in dreams_dir.iterdir() if p.name.startswith(".staging-")
    ] if dreams_dir.exists() else []
    assert len(staging_subdirs_after) == 0, (
        "Staging directory must be removed from disk after discard_staging()"
    )


# ──────────────────────────────────────────────────────────────────
# 10. version_count and last_mutation_at reflect .versions/ on disk

def test_fs_10_version_count_and_last_mutation(tmp_path):
    """version_count() and last_mutation_at() read from the .versions/ directory."""
    agent_root, backend, policy = _make_backend(tmp_path)

    note_name = "version_count_test"

    # Zero versions before any write
    assert backend.version_count(note_name + ".md") == 0

    # Write once — no snapshot yet (first write, no pre-existing content)
    ref = backend.write_note(
        _make_capture(name="Version count test", description="For version count"),
        policy,
    )

    # Merge to force a snapshot (merge always snapshots before overwriting)
    backend.write_note(
        _make_capture(name="Version count test", description="For version count",
                      body="Updated body v2.", merge_into=ref.name),
        policy,
    )

    count = backend.version_count(ref.name)
    assert count >= 1, f"Expected >= 1 version after overwrite, got {count}"

    ts = backend.last_mutation_at(ref.name)
    assert ts is not None, "last_mutation_at must not be None after at least one snapshot"
    # Must be timezone-aware
    assert ts.tzinfo is not None, "last_mutation_at must return a timezone-aware datetime"

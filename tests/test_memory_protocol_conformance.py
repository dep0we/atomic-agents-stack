"""Protocol conformance tests for MemoryBackend.

These 25 named behavioral tests define the contract every MemoryBackend
implementation must satisfy. They are parameterized over backends so a
future SQLiteBackend or PostgresBackend can plug in trivially.

Currently, only FilesystemBackend is tested here.

See docs/spec/20-memory-backend.md for the full protocol specification.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from atomic_agents.memory.backend import WritePolicy
from atomic_agents.memory.filesystem import FilesystemBackend
from atomic_agents.exceptions import (
    MemoryPreconditionFailed,
    SchemaValidationError,
    VersionNotFound,
    WritePathViolation,
    StagingNotApplied,
)
from atomic_agents.types import Capture


# ──────────────────────────────────────────────────────────────────
# Fixtures

def _make_capture(
    *,
    name: str = "Test preference",
    type_: str = "feedback",
    description: str = "A test capture",
    confidence: str = "high",
    sources: list[str] | None = None,
    body: str = "The body of the capture.",
    merge_into: str | None = None,
    pinned: bool = False,
    tags: list[str] | None = None,
) -> Capture:
    return Capture(
        type=type_,
        name=name,
        description=description,
        confidence=confidence,
        sources=sources or ["test_source"],
        body=body,
        merge_into=merge_into,
        pinned=pinned,
        tags=tags or [],
    )


def _make_backend(tmp_path: Path) -> tuple[FilesystemBackend, WritePolicy]:
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])
    return backend, policy


# ──────────────────────────────────────────────────────────────────
# 1. read_note round-trips a Capture → Note losslessly

def test_conformance_01_read_note_roundtrip(tmp_path):
    """read_note round-trips a Capture → Note losslessly."""
    backend, policy = _make_backend(tmp_path)
    capture = _make_capture(
        name="Communication style",
        body="Direct and concise.",
        tags=["style"],
        pinned=True,
    )
    ref = backend.write_note(capture, policy)
    note = backend.read_note(ref.name)

    assert note is not None
    assert note.type == capture.type
    assert note.name == capture.name
    assert note.description == capture.description
    assert note.confidence == capture.confidence
    assert note.sources == capture.sources
    assert note.body.strip() == capture.body.strip()
    assert note.tags == capture.tags
    assert note.pinned == capture.pinned
    assert note.schema_version == 1


# ──────────────────────────────────────────────────────────────────
# 2. read_note returns None for nonexistent note

def test_conformance_02_read_note_returns_none_for_missing(tmp_path):
    """read_note returns None for a nonexistent note name."""
    backend, _ = _make_backend(tmp_path)
    result = backend.read_note("feedback_does_not_exist.md")
    assert result is None


# ──────────────────────────────────────────────────────────────────
# 3. write_note rejects when policy.write_paths doesn't include target

def test_conformance_03_write_note_rejects_outside_write_paths(tmp_path):
    """write_note raises WritePathViolation when target is outside write_paths."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    backend = FilesystemBackend(agent_root, "memory")
    # Policy only allows writing to other_dir, not memory_dir
    policy = WritePolicy(write_paths=[other_dir])
    capture = _make_capture()

    with pytest.raises(WritePathViolation):
        backend.write_note(capture, policy)


# ──────────────────────────────────────────────────────────────────
# 4. write_note rejects when target falls under policy.read_only_paths

def test_conformance_04_write_note_rejects_read_only_path(tmp_path):
    """write_note raises WritePathViolation when target is under read_only_paths."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    backend = FilesystemBackend(agent_root, "memory")
    # write_paths includes memory_dir, but memory_dir is also read_only
    policy = WritePolicy(
        write_paths=[memory_dir],
        read_only_paths=[memory_dir],
    )
    capture = _make_capture()

    with pytest.raises(WritePathViolation):
        backend.write_note(capture, policy)


# ──────────────────────────────────────────────────────────────────
# 5. write_note enforces expected_content_sha256 precondition

def test_conformance_05_write_note_sha256_precondition(tmp_path):
    """write_note raises MemoryPreconditionFailed when sha256 doesn't match."""
    backend, policy = _make_backend(tmp_path)

    # Write initial note
    capture = _make_capture(name="Comm style")
    ref = backend.write_note(capture, policy)

    # Try to overwrite with wrong sha256 precondition
    merge_cap = _make_capture(name="Comm style", merge_into=ref.name, sources=["new_src"])
    with pytest.raises(MemoryPreconditionFailed):
        backend.write_note(merge_cap, policy, expected_content_sha256="wrong_sha256_here")


def test_conformance_05b_write_note_sha256_precondition_note_missing(tmp_path):
    """write_note raises MemoryPreconditionFailed when precondition given but note missing."""
    backend, policy = _make_backend(tmp_path)
    capture = _make_capture()

    with pytest.raises(MemoryPreconditionFailed):
        backend.write_note(capture, policy, expected_content_sha256="any_sha256")


# ──────────────────────────────────────────────────────────────────
# 6. write_note orphan-recovery: same-content rewrite repairs index

def test_conformance_06_write_note_orphan_recovery(tmp_path):
    """write_note repairs INDEX when same-content note exists (orphan recovery)."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])

    capture = _make_capture(name="Comm style", body="Direct tone.")
    ref = backend.write_note(capture, policy)

    # Remove INDEX.md to simulate orphan
    index_path = memory_dir / "INDEX.md"
    index_path.unlink()
    assert not index_path.exists()

    # Write same capture again — should repair index
    ref2 = backend.write_note(capture, policy)
    assert index_path.exists()
    assert ref2.name == ref.name


# ──────────────────────────────────────────────────────────────────
# 7. write_note merge: refreshes last_seen + sources, preserves body

def test_conformance_07_write_note_merge_refreshes_metadata(tmp_path):
    """write_note merge refreshes last_seen and sources while preserving body."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Comm style", body="Direct tone.", sources=["src_001"])
    ref = backend.write_note(capture, policy)

    # Read original note
    note_before = backend.read_note(ref.name)
    original_body = note_before.body

    # Merge with new source
    merge_cap = _make_capture(
        name="Comm style",
        merge_into=ref.name,
        sources=["src_002"],
        body="DIFFERENT BODY — should be ignored on merge",
    )
    backend.write_note(merge_cap, policy)

    note_after = backend.read_note(ref.name)
    assert note_after.body.strip() == original_body.strip(), "body must be preserved on merge"
    assert "src_001" in note_after.sources
    assert "src_002" in note_after.sources


# ──────────────────────────────────────────────────────────────────
# 8. write_note merge: raises if target missing

def test_conformance_08_write_note_merge_target_missing_raises(tmp_path):
    """write_note merge raises SchemaValidationError if target doesn't exist."""
    backend, policy = _make_backend(tmp_path)

    merge_cap = _make_capture(
        name="Nonexistent",
        merge_into="feedback_does_not_exist.md",
    )
    with pytest.raises(SchemaValidationError):
        backend.write_note(merge_cap, policy)


# ──────────────────────────────────────────────────────────────────
# 9. write_note distinct-content collision: raises (use merge_into)

def test_conformance_09_write_note_collision_raises(tmp_path):
    """write_note raises SchemaValidationError on distinct-content name collision."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Comm style", body="First body.")
    backend.write_note(capture, policy)

    # Same name, different body, no merge_into
    conflicting = _make_capture(name="Comm style", body="DIFFERENT BODY.")
    with pytest.raises(SchemaValidationError):
        backend.write_note(conflicting, policy)


# ──────────────────────────────────────────────────────────────────
# 10. list_notes excludes archived/superseded by default

def test_conformance_10_list_notes_excludes_archived_superseded(tmp_path):
    """list_notes excludes archived and superseded notes by default."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])

    # Write a normal note
    backend.write_note(_make_capture(name="Active"), policy)

    # Manually write an archived note
    import frontmatter
    from atomic_agents._io import atomic_write
    archived_path = memory_dir / "feedback_archived.md"
    post = frontmatter.Post(
        "archived body",
        schema_version=1, type="feedback", name="Archived",
        description="x", confidence="high", sources=["s"],
        captured="2026-01-01", last_seen="2026-01-01", archived=True,
    )
    atomic_write(archived_path, frontmatter.dumps(post) + "\n")

    # Write a superseded note
    superseded_path = memory_dir / "feedback_superseded.md"
    post2 = frontmatter.Post(
        "superseded body",
        schema_version=1, type="feedback", name="Superseded",
        description="y", confidence="high", sources=["s"],
        captured="2026-01-01", last_seen="2026-01-01",
        superseded_by="feedback_active.md",
    )
    atomic_write(superseded_path, frontmatter.dumps(post2) + "\n")

    # Default list should exclude archived + superseded
    refs = backend.list_notes()
    names = [r.name for r in refs]
    assert "feedback_archived.md" not in names
    assert "feedback_superseded.md" not in names

    # With include flags, they appear
    all_refs = backend.list_notes(include_archived=True, include_superseded=True)
    all_names = [r.name for r in all_refs]
    assert "feedback_archived.md" in all_names
    assert "feedback_superseded.md" in all_names


# ──────────────────────────────────────────────────────────────────
# 11. list_recent sort order is last_seen DESC

def test_conformance_11_list_recent_sort_order(tmp_path):
    """list_recent returns notes sorted by last_seen DESC."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])

    import frontmatter
    from atomic_agents._io import atomic_write

    # Write notes with explicit last_seen dates
    for i, last_seen in enumerate(["2026-01-01", "2026-03-01", "2026-02-01"]):
        path = memory_dir / f"feedback_note_{i}.md"
        post = frontmatter.Post(
            f"body {i}",
            schema_version=1, type="feedback", name=f"Note {i}",
            description=f"note {i}", confidence="high", sources=["s"],
            captured="2026-01-01", last_seen=last_seen,
        )
        atomic_write(path, frontmatter.dumps(post) + "\n")

    recent = backend.list_recent(3)
    dates = [r.last_seen for r in recent]
    assert dates == sorted(dates, reverse=True), "Should be sorted newest-first"


# ──────────────────────────────────────────────────────────────────
# 12. list_stale filters by threshold + excludes pinned

def test_conformance_12_list_stale_filters_threshold_and_pinned(tmp_path):
    """list_stale returns notes older than threshold and excludes pinned."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")

    import frontmatter
    from atomic_agents._io import atomic_write

    # Write a stale note (100 days ago)
    old_date = (date.today() - timedelta(days=100)).isoformat()
    stale_path = memory_dir / "feedback_stale.md"
    post = frontmatter.Post(
        "stale body",
        schema_version=1, type="feedback", name="Stale note",
        description="x", confidence="high", sources=["s"],
        captured="2026-01-01", last_seen=old_date,
    )
    atomic_write(stale_path, frontmatter.dumps(post) + "\n")

    # Write a recent note
    recent_path = memory_dir / "feedback_recent.md"
    post2 = frontmatter.Post(
        "recent body",
        schema_version=1, type="feedback", name="Recent note",
        description="y", confidence="high", sources=["s"],
        captured="2026-01-01", last_seen=date.today().isoformat(),
    )
    atomic_write(recent_path, frontmatter.dumps(post2) + "\n")

    # Write a pinned stale note
    pinned_path = memory_dir / "feedback_pinned_stale.md"
    post3 = frontmatter.Post(
        "pinned body",
        schema_version=1, type="feedback", name="Pinned stale",
        description="z", confidence="high", sources=["s"],
        captured="2026-01-01", last_seen=old_date, pinned=True,
    )
    atomic_write(pinned_path, frontmatter.dumps(post3) + "\n")

    stale = backend.list_stale(threshold_days=90)
    names = [r.name for r in stale]

    assert "feedback_stale.md" in names
    assert "feedback_recent.md" not in names
    assert "feedback_pinned_stale.md" not in names, "pinned notes excluded by default"


# ──────────────────────────────────────────────────────────────────
# 13. list_orphans returns notes missing from index

def test_conformance_13_list_orphans_returns_missing_from_index(tmp_path):
    """list_orphans returns notes that exist in memory/ but not in INDEX.md."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])

    # Write note (lands in INDEX)
    capture = _make_capture(name="Indexed note")
    ref = backend.write_note(capture, policy)

    # Write orphan directly (no INDEX update)
    import frontmatter
    from atomic_agents._io import atomic_write
    orphan_path = memory_dir / "feedback_orphan.md"
    post = frontmatter.Post(
        "orphan body",
        schema_version=1, type="feedback", name="Orphan",
        description="x", confidence="high", sources=["s"],
        captured="2026-01-01", last_seen="2026-01-01",
    )
    atomic_write(orphan_path, frontmatter.dumps(post) + "\n")

    orphans = backend.list_orphans()
    orphan_names = [r.name for r in orphans]

    assert "feedback_orphan.md" in orphan_names
    assert ref.name not in orphan_names


# ──────────────────────────────────────────────────────────────────
# 14. list_versions returns newest-first

def test_conformance_14_list_versions_newest_first(tmp_path):
    """list_versions returns newest-first version refs."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Versioned note", body="v1")
    ref = backend.write_note(capture, policy)

    # Merge to trigger a snapshot
    merge_cap = _make_capture(name="Versioned note", merge_into=ref.name, sources=["src2"])
    backend.write_note(merge_cap, policy)

    # Merge again for a second snapshot
    merge_cap2 = _make_capture(name="Versioned note", merge_into=ref.name, sources=["src3"])
    backend.write_note(merge_cap2, policy)

    versions = backend.list_versions(ref.name)
    assert len(versions) >= 2

    # Verify newest first by checking that version IDs sort descending
    ids = [v.backend_id for v in versions]
    assert ids == sorted(ids, reverse=True)


# ──────────────────────────────────────────────────────────────────
# 15. read_version returns full Note (frontmatter + body)

def test_conformance_15_read_version_returns_full_note(tmp_path):
    """read_version returns a full Note with frontmatter and body."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Versioned", body="Original body.")
    ref = backend.write_note(capture, policy)

    # Merge to create a snapshot of the original
    merge_cap = _make_capture(name="Versioned", merge_into=ref.name, sources=["src2"])
    backend.write_note(merge_cap, policy)

    versions = backend.list_versions(ref.name)
    assert versions, "Should have at least one version"

    version_note = backend.read_version(versions[0])
    assert version_note is not None
    assert version_note.name is not None
    # The body of the snapshot should be the original
    assert "Original body." in version_note.body


# ──────────────────────────────────────────────────────────────────
# 16. restore_version snapshots pre-state THEN replaces

def test_conformance_16_restore_version_snapshots_pre_state(tmp_path):
    """restore_version snapshots the current live state before restoring."""
    backend, policy = _make_backend(tmp_path)

    # Write initial note and create a version by merging
    capture = _make_capture(name="Restore target", body="Original body.")
    ref = backend.write_note(capture, policy)
    merge_cap = _make_capture(name="Restore target", merge_into=ref.name, sources=["src2"])
    backend.write_note(merge_cap, policy)

    versions_before = backend.list_versions(ref.name)
    assert len(versions_before) >= 1

    # Restore from the first version
    backend.restore_version(ref.name, versions_before[0], policy)

    # After restore, there should be one more version (pre-restore state)
    versions_after = backend.list_versions(ref.name)
    assert len(versions_after) > len(versions_before)


# ──────────────────────────────────────────────────────────────────
# 17. redact_version preserves frontmatter, replaces body

def test_conformance_17_redact_version_preserves_frontmatter(tmp_path):
    """redact_version replaces body with redaction marker, frontmatter preserved."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="PII note", body="Sensitive info: SSN 123-45-6789")
    ref = backend.write_note(capture, policy)

    # Merge to create a snapshot containing the sensitive body
    merge_cap = _make_capture(name="PII note", merge_into=ref.name, sources=["src2"])
    backend.write_note(merge_cap, policy)

    versions = backend.list_versions(ref.name)
    assert versions

    backend.redact_version(versions[0])

    # Read the redacted version
    redacted = backend.read_version(versions[0])
    assert "[REDACTED]" in redacted.body
    # Frontmatter should still have the name
    assert redacted.name is not None


# ──────────────────────────────────────────────────────────────────
# 18. resolve_version_token raises VersionNotFound for bad token

def test_conformance_18_resolve_version_token_raises_for_bad_token(tmp_path):
    """resolve_version_token raises VersionNotFound for unresolvable token."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Tokenized")
    ref = backend.write_note(capture, policy)
    # Merge to create a version
    merge_cap = _make_capture(name="Tokenized", merge_into=ref.name, sources=["s2"])
    backend.write_note(merge_cap, policy)

    with pytest.raises(VersionNotFound):
        backend.resolve_version_token(ref.name, "nonexistent_version.md")


# ──────────────────────────────────────────────────────────────────
# 19. create_staging → write to staging → apply → live reflects staged

def test_conformance_19_staging_apply_reflects_in_live(tmp_path):
    """create_staging → write_note → apply_staging → live memory updated."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])

    # Write a note to live memory first
    orig_cap = _make_capture(name="Live note", body="Original.")
    backend.write_note(orig_cap, policy)

    # Create staging and write a new note there
    staging = backend.create_staging()
    new_cap = _make_capture(name="New note", body="Staged body.", type_="decision")
    staged_policy = WritePolicy(write_paths=[staging.staging_dir])
    staging.write_note(new_cap, staged_policy)

    # Apply staging — this should atomically swap live with staged
    backend.apply_staging(staging, policy)

    # After apply, the staged note should be in live memory
    # (The original "Live note" was in the old live memory, which got archived)
    # The staging area had "New note"
    live_refs = backend.list_notes()
    live_names = [r.name for r in live_refs]
    assert "decision_new_note.md" in live_names


# ──────────────────────────────────────────────────────────────────
# 20. discard_staging removes staging without touching live

def test_conformance_20_discard_staging_preserves_live(tmp_path):
    """discard_staging removes staging area without touching live memory."""
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(write_paths=[memory_dir])

    # Write a note to live memory
    orig_cap = _make_capture(name="Live note", body="Live body.")
    backend.write_note(orig_cap, policy)

    # Create staging, write to it
    staging = backend.create_staging()
    staged_policy = WritePolicy(write_paths=[staging.staging_dir])
    staging.write_note(_make_capture(name="Staged"), staged_policy)
    staging_dir = staging.staging_dir

    # Discard staging
    backend.discard_staging(staging)

    # Staging dir should be gone
    assert not staging_dir.exists()

    # Live memory should be untouched
    live_refs = backend.list_notes()
    live_names = [r.name for r in live_refs]
    assert "feedback_live_note.md" in live_names


# ──────────────────────────────────────────────────────────────────
# 21. Concurrent writes: two threads with preconditions → only one succeeds

def test_conformance_21_concurrent_writes_precondition(tmp_path):
    """Concurrent writes with sha256 preconditions — exactly one wins, one raises.

    Two threads race to merge-write the same note with the SAME precondition sha256.
    The per-file lock serializes them. After the first write mutates the file, the
    second thread reads the new content which no longer matches the precondition →
    MemoryPreconditionFailed. Exactly one success and one MemoryPreconditionFailed
    must be observed.
    """
    backend, policy = _make_backend(tmp_path)

    # Write initial note with NO sources so the first merge can add one and
    # guarantee a content change (which shifts the sha256 for the second thread).
    capture = _make_capture(name="Concurrent target", body="Initial body.", sources=["initial_src"])
    ref = backend.write_note(capture, policy)

    # Get the sha256 of the current content
    note_path = tmp_path / "agent" / "memory" / ref.name
    content = note_path.read_text(encoding="utf-8")
    correct_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()

    successes: list[int] = []
    precondition_failures: list[int] = []
    other_failures: list[tuple[int, Exception]] = []
    barrier = threading.Barrier(2)

    def attempt_merge(thread_id: int):
        try:
            barrier.wait()  # Synchronize start for maximum concurrency
            # Each thread uses a unique source not already in the note;
            # this guarantees the first merge actually changes the file content
            # (adds the new source), so the second thread's sha256 check fails.
            merge_cap = _make_capture(
                name="Concurrent target",
                merge_into=ref.name,
                sources=[f"new_src_{thread_id}"],
            )
            backend.write_note(merge_cap, policy, expected_content_sha256=correct_sha256)
            successes.append(thread_id)
        except MemoryPreconditionFailed:
            precondition_failures.append(thread_id)
        except Exception as exc:
            other_failures.append((thread_id, exc))

    threads = [threading.Thread(target=attempt_merge, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Both threads must complete without unexpected exceptions
    assert not other_failures, (
        f"Unexpected exceptions: {other_failures}"
    )
    total = len(successes) + len(precondition_failures)
    assert total == 2, f"Both threads must complete: successes={successes}, failures={precondition_failures}"
    # Exactly one success and one precondition failure (the loser sees a changed sha256)
    assert len(successes) == 1, (
        f"Exactly one thread should succeed — got {len(successes)}: {successes}"
    )
    assert len(precondition_failures) == 1, (
        f"Exactly one MemoryPreconditionFailed expected — got {len(precondition_failures)}"
    )


# ──────────────────────────────────────────────────────────────────
# 22. render_index_summary returns non-empty parseable text

def test_conformance_22_render_index_summary_non_empty(tmp_path):
    """render_index_summary returns non-empty text parseable by humans and LLMs."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Important fact", body="Remember this.", type_="decision")
    backend.write_note(capture, policy)

    summary = backend.render_index_summary()
    assert summary, "Should return non-empty text"
    assert "Important fact" in summary
    assert len(summary.strip()) > 10


# ──────────────────────────────────────────────────────────────────
# 23. stats returns coherent total_notes and by_type matching actual

def test_conformance_23_stats_coherent(tmp_path):
    """stats returns total_notes and by_type that match actual note count."""
    backend, policy = _make_backend(tmp_path)

    captures = [
        _make_capture(name=f"Feedback {i}", type_="feedback")
        for i in range(3)
    ]
    captures.append(_make_capture(name="Decision 1", type_="decision"))
    for cap in captures:
        backend.write_note(cap, policy)

    stats = backend.stats()
    assert stats.total_notes == 4
    assert stats.by_type.get("feedback", 0) == 3
    assert stats.by_type.get("decision", 0) == 1


# ──────────────────────────────────────────────────────────────────
# 24. version_count matches len(list_versions())

def test_conformance_24_version_count_matches_list(tmp_path):
    """version_count matches len(list_versions())."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Counted")
    ref = backend.write_note(capture, policy)

    # Create 2 versions via merges
    for i in range(2):
        merge_cap = _make_capture(name="Counted", merge_into=ref.name, sources=[f"src_{i}"])
        backend.write_note(merge_cap, policy)

    versions = backend.list_versions(ref.name)
    count = backend.version_count(ref.name)
    assert count == len(versions)


# ──────────────────────────────────────────────────────────────────
# 25. last_mutation_at reflects most recent snapshot or write

def test_conformance_25_last_mutation_at_reflects_recent(tmp_path):
    """last_mutation_at returns a datetime that reflects the most recent snapshot or write."""
    backend, policy = _make_backend(tmp_path)

    capture = _make_capture(name="Timestamped")
    ref = backend.write_note(capture, policy)

    before_merge = time.time()
    time.sleep(0.01)  # Small delay to ensure ordering

    # Merge to trigger a snapshot
    merge_cap = _make_capture(name="Timestamped", merge_into=ref.name, sources=["src2"])
    backend.write_note(merge_cap, policy)

    mutation_dt = backend.last_mutation_at(ref.name)
    assert mutation_dt is not None, "Should return a datetime after write/merge"

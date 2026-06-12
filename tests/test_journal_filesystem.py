"""Filesystem-specific tests for FilesystemJournalBackend (spec/43).

These tests pin behaviors that are specific to the filesystem implementation
and are NOT part of the parametrized conformance suite. They cover:

- .journal.lock sidecar placement (at agent_root level, not inside journal/)
- Month subdir minting (YYYY-MM/)
- Symlinked-entry SELECTION parity: a symlinked .md entry resolving under
  agent_root is INCLUDED (legacy callers followed symlinks) in both
  list_entries() and query_by_date()
- Symlink containment: _journal_dir() raises PathTraversalError on a symlinked
  journal/ DIRECTORY escaping agent_root (the real escape vector)
- *.tmp sidecar exclusion from list_entries()
- .journal.lock exclusion from list_entries()
- skip-unreadable: OSError silently skipped
- skip-unreadable: UnicodeDecodeError silently skipped
- export() relative path format
- Lock file created on first write (absent OK before)
- ValueError on '..' in agent_root
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from atomic_agents.journal.filesystem import FilesystemJournalBackend
from atomic_agents.exceptions import PathTraversalError


# ──────────────────────────────────────────────────────────────────────────────
# Lock placement


def test_filesystem_journal_lock_path(tmp_path: Path) -> None:
    """The .journal.lock sidecar must live at agent_root level (NOT inside journal/)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    assert be._lock_path == agent_root / ".journal.lock"
    assert ".journal.lock" not in str(agent_root / "journal")


def test_filesystem_journal_lock_absent_ok(tmp_path: Path) -> None:
    """Lock file is created on first write; must not pre-exist."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    lock_path = agent_root / ".journal.lock"
    assert not lock_path.exists()

    be = FilesystemJournalBackend(agent_root)
    be.append_entry("First write", when=date(2026, 6, 12))
    # Lock file must now exist (created by _journal_lock)
    assert lock_path.exists()


def test_filesystem_journal_lock_not_matched_by_rglob(tmp_path: Path) -> None:
    """The .journal.lock file must NOT appear in list_entries() or query_by_date()."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Normal entry", when=date(2026, 6, 12))

    # Verify lock file exists
    lock_path = agent_root / ".journal.lock"
    assert lock_path.exists()

    # Must not appear in list_entries
    entries = be.list_entries()
    paths = [e.path for e in entries]
    assert lock_path not in paths

    # Must not appear in query_by_date
    query_entries = be.query_by_date(start=date(2026, 1, 1), end=date(2026, 12, 31))
    query_paths = [e.path for e in query_entries]
    assert lock_path not in query_paths


# ──────────────────────────────────────────────────────────────────────────────
# Month subdir minting


def test_filesystem_journal_month_subdir(tmp_path: Path) -> None:
    """append_entry() must create YYYY-MM/ subdir under journal/."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    entry = be.append_entry("June entry", when=date(2026, 6, 12))

    # Month subdir must exist
    month_dir = agent_root / "journal" / "2026-06"
    assert month_dir.is_dir()
    # Day file must be inside month dir
    assert entry.path.parent == month_dir.resolve()


def test_filesystem_journal_cross_month_separate_subdirs(tmp_path: Path) -> None:
    """Entries in different months must go into separate YYYY-MM/ subdirs."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("May entry", when=date(2026, 5, 31))
    be.append_entry("June entry", when=date(2026, 6, 1))

    may_dir = agent_root / "journal" / "2026-05"
    jun_dir = agent_root / "journal" / "2026-06"
    assert may_dir.is_dir()
    assert jun_dir.is_dir()


# ──────────────────────────────────────────────────────────────────────────────
# Symlinked entry-file SELECTION parity (byte-identity ruling, #427 PR1)
#
# The three legacy read sites (bundle._load_recent_journal / bundle._source_paths
# / dream._read_journal_entries) did plain sorted(rglob('*.md'), reverse=True) and
# read each via is_file()/_safe_read_text/read_text — ALL of which FOLLOW symlinks.
# So a symlinked .md entry pointing to a real file inside agent_root was SELECTED
# and read through. The backend MUST match: a per-entry symlink skip would drop the
# slot, backfill an older entry, and shift both the selection AND the
# _source_paths/_staleness_paths set that drives cascade .lease.json freshness.
# These tests pin that the symlinked entry is INCLUDED (NOT skipped).


def test_filesystem_journal_symlinked_entry_selected_list(tmp_path: Path) -> None:
    """list_entries() must SELECT a symlinked .md entry (legacy followed symlinks)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Real older entry", when=date(2026, 6, 11))

    # Newest entry is a symlink-to-file (target resolves under agent_root, so it is
    # contained — the symlinked-DIRECTORY escape is a separate, refused case).
    journal_dir = agent_root / "journal" / "2026-06"
    sym_target = journal_dir / "2026-06-13.real.md.src"
    sym_target.write_text("Symlinked newest content", encoding="utf-8")
    symlink_file = journal_dir / "2026-06-13.md"
    symlink_file.symlink_to(sym_target)

    entries = be.list_entries(limit=2, newest_first=True)
    paths = [e.path for e in entries]
    # The symlinked newest entry MUST be selected (followed through), matching legacy.
    assert symlink_file in paths
    # And its body must be read through the link.
    sym_entry = next(e for e in entries if e.path == symlink_file)
    assert sym_entry.text == "Symlinked newest content"


def test_filesystem_journal_symlinked_entry_selected_query(tmp_path: Path) -> None:
    """query_by_date() must SELECT a symlinked .md entry (legacy followed symlinks)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Real entry", when=date(2026, 6, 12))

    journal_dir = agent_root / "journal" / "2026-06"
    sym_target = journal_dir / "2026-06-11.real.md.src"
    sym_target.write_text("Symlinked content", encoding="utf-8")
    symlink_file = journal_dir / "2026-06-11.md"
    symlink_file.symlink_to(sym_target)

    entries = be.query_by_date(start=date(2026, 6, 1), end=date(2026, 6, 30))
    paths = [e.path for e in entries]
    # The symlinked in-window entry MUST appear (read through), matching legacy.
    assert symlink_file in paths
    sym_entry = next(e for e in entries if e.path == symlink_file)
    assert sym_entry.text == "Symlinked content"


# ──────────────────────────────────────────────────────────────────────────────
# Symlink containment: _journal_dir()


def test_filesystem_journal_symlink_containment_list_returns_empty(
    tmp_path: Path,
) -> None:
    """A symlinked journal/ pointing outside agent_root causes list_entries to return []."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    outside_dir = tmp_path / "outside_journal"
    outside_dir.mkdir()

    # Write a real entry first (before symlinking)
    journal_link = agent_root / "journal"
    journal_link.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Entry before symlink", when=date(2026, 6, 12))

    # Replace journal/ with a symlink pointing outside
    import shutil

    shutil.rmtree(str(journal_link))
    journal_link.symlink_to(outside_dir)

    be2 = FilesystemJournalBackend(agent_root)
    entries = be2.list_entries()
    # Should return [] (fail-soft on symlink escape for reads)
    assert entries == []


def test_filesystem_journal_symlink_containment_append_raises(
    tmp_path: Path,
) -> None:
    """A symlinked journal/ pointing outside agent_root causes append_entry to raise."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    outside_dir = tmp_path / "outside_journal"
    outside_dir.mkdir()

    # Create journal/ as a symlink pointing outside
    journal_link = agent_root / "journal"
    journal_link.symlink_to(outside_dir)

    be = FilesystemJournalBackend(agent_root)
    with pytest.raises(PathTraversalError):
        be.append_entry("Should fail", when=date(2026, 6, 12))


# ──────────────────────────────────────────────────────────────────────────────
# Symlink-LOOP / unresolvable path (distinct from symlink-ESCAPE above)
#
# .resolve() raises RuntimeError (ELOOP / symlink loop) — NOT PathTraversalError.
# The legacy rglob callers this PR deleted used exists()/is_dir()/rglob(), all of
# which return False/[] on a symlink loop WITHOUT raising, so the agent ran with an
# empty journal. Because journal is now LIVE-WIRED into the system prompt (BP[14]),
# cascade .lease.json freshness, and dream consolidation, an unguarded resolve()
# crash would convert a graceful-empty path into a hard crash on every agent.call().
# _journal_dir() folds the resolution failure into PathTraversalError so reads
# return [] (fail-soft) and append fails loud — restoring legacy graceful-empty.


def test_filesystem_journal_symlink_loop_list_returns_empty(tmp_path: Path) -> None:
    """A symlink-loop journal/ (ELOOP on resolve) → list_entries returns []."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    # journal/ is a self-referential symlink loop: resolving it raises RuntimeError.
    journal_link = agent_root / "journal"
    journal_link.symlink_to(journal_link)

    be = FilesystemJournalBackend(agent_root)
    assert be.list_entries() == []


def test_filesystem_journal_symlink_loop_query_returns_empty(tmp_path: Path) -> None:
    """A symlink-loop journal/ (ELOOP on resolve) → query_by_date returns []."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    journal_link = agent_root / "journal"
    journal_link.symlink_to(journal_link)

    be = FilesystemJournalBackend(agent_root)
    assert be.query_by_date(date(2026, 1, 1), date(2026, 12, 31)) == []


def test_filesystem_journal_symlink_loop_append_raises(tmp_path: Path) -> None:
    """A symlink-loop journal/ (ELOOP on resolve) → append_entry fails loud."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    journal_link = agent_root / "journal"
    journal_link.symlink_to(journal_link)

    be = FilesystemJournalBackend(agent_root)
    with pytest.raises(PathTraversalError):
        be.append_entry("Should fail", when=date(2026, 6, 12))


def test_filesystem_journal_unresolvable_agent_root_constructs(tmp_path: Path) -> None:
    """Construction must NOT crash when agent_root itself is unresolvable.

    journal is live-wired into AtomicAgent.__init__ (#427 PR1 ADOPT-NOW), so a
    resolve() crash in the backend constructor would take down agent construction
    entirely. Resolution is deferred to first use; construction is side-effect-free.
    list_entries then returns [] (fail-soft), append fails loud.
    """
    # agent_root has a symlink-loop ancestor → agent_root.resolve() raises RuntimeError.
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    agent_root = loop / "agent"

    # Construction must not raise.
    be = FilesystemJournalBackend(agent_root)
    assert be.list_entries() == []
    with pytest.raises(PathTraversalError):
        be.append_entry("Should fail", when=date(2026, 6, 12))


# ──────────────────────────────────────────────────────────────────────────────
# *.tmp sidecar exclusion


def test_filesystem_journal_tmp_excluded(tmp_path: Path) -> None:
    """*.tmp files must NOT appear in list_entries() (crash-recovery artifact)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Normal entry", when=date(2026, 6, 12))

    # Manually plant a *.tmp file (simulating a crash-recovery sidecar)
    journal_dir = agent_root / "journal" / "2026-06"
    tmp_file = journal_dir / ".2026-06-12.abc12345.tmp"
    tmp_file.write_text("Partial write", encoding="utf-8")

    entries = be.list_entries()
    paths = [e.path for e in entries]
    assert tmp_file not in paths


# ──────────────────────────────────────────────────────────────────────────────
# skip-unreadable: OSError


def test_filesystem_journal_list_keeps_unreadable_oserror_slot(tmp_path: Path) -> None:
    """list_entries() KEEPS an OSError-unreadable file's slot (degrade-but-keep).

    Byte-identity contract (#427 PR1): the legacy bundle read every selected slot
    via _safe_read_text, which on OSError returns a warning-only comment rather
    than dropping the entry. Dropping would backfill an older entry and shift the
    _source_paths/_staleness_paths set (cascade .lease.json freshness). So
    list_entries must KEEP the slot with a degraded body — NOT skip it.
    query_by_date (the dream consumer) still SKIPS, matching legacy dream.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Good entry", when=date(2026, 6, 12))

    # Plant an unreadable file (0 permissions)
    journal_dir = agent_root / "journal" / "2026-06"
    bad_file = journal_dir / "2026-06-11.md"
    bad_file.write_text("Unreadable", encoding="utf-8")
    bad_file.chmod(0o000)
    if os.getuid() == 0:
        pytest.skip("chmod 0o000 does not restrict root; cannot test unreadable path")

    try:
        entries = be.list_entries()
        paths = [e.path for e in entries]
        assert any("2026-06-12" in str(p) for p in paths)
        # The unreadable file KEEPS its slot (degrade-but-keep), not dropped.
        assert bad_file in paths
        bad_entry = next(e for e in entries if e.path == bad_file)
        assert "unreadable" in bad_entry.text.lower()

        # query_by_date still SKIPS unreadable (matches legacy dream behavior).
        q = be.query_by_date(start=date(2026, 1, 1), end=date.max)
        assert bad_file not in [e.path for e in q]
    finally:
        bad_file.chmod(0o644)  # restore permissions for cleanup


# ──────────────────────────────────────────────────────────────────────────────
# degrade-but-keep: UnicodeDecodeError


def test_filesystem_journal_list_keeps_bad_unicode_slot(tmp_path: Path) -> None:
    """list_entries() KEEPS a non-UTF-8 file's slot with errors='replace' body.

    Mirrors legacy bundle._safe_read_text (errors='replace' + warning comment).
    query_by_date still SKIPS (matches legacy dream._read_journal_entries).
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Good entry", when=date(2026, 6, 12))

    # Plant a file with invalid UTF-8
    journal_dir = agent_root / "journal" / "2026-06"
    bad_file = journal_dir / "2026-06-11.md"
    bad_file.write_bytes(b"\xff\xfe invalid utf-8 \x80\x81")

    entries = be.list_entries()
    paths = [e.path for e in entries]
    # Bad file KEEPS its slot (degrade-but-keep), not dropped.
    assert bad_file in paths
    bad_entry = next(e for e in entries if e.path == bad_file)
    assert "non-UTF-8 bytes" in bad_entry.text
    # Good file must still appear
    assert any("2026-06-12" in str(p) for p in paths)

    # query_by_date SKIPS the non-UTF-8 file (matches legacy dream).
    q = be.query_by_date(start=date(2026, 1, 1), end=date.max)
    assert bad_file not in [e.path for e in q]


# ──────────────────────────────────────────────────────────────────────────────
# export() relative path format


def test_filesystem_journal_export_relative_paths(tmp_path: Path) -> None:
    """export() must return paths relative to agent_root, starting with 'journal/'."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Entry A", when=date(2026, 5, 31))
    be.append_entry("Entry B", when=date(2026, 6, 12))

    result = be.export()
    assert len(result.entries_with_bytes) == 2
    for rel_path_str, raw_bytes in result.entries_with_bytes:
        assert rel_path_str.startswith("journal/"), (
            f"export path must start with 'journal/', got {rel_path_str!r}"
        )
        # Must NOT be absolute
        assert not Path(rel_path_str).is_absolute()
        # On-disk file must match
        on_disk = agent_root / rel_path_str
        assert on_disk.read_bytes() == raw_bytes


# ──────────────────────────────────────────────────────────────────────────────
# ValueError on '..' in agent_root


def test_filesystem_journal_dotdot_rejected() -> None:
    """FilesystemJournalBackend must raise ValueError if agent_root contains '..'."""
    with pytest.raises(ValueError, match=r"\.\."):
        FilesystemJournalBackend(Path("/tmp/../etc/passwd"))


# ──────────────────────────────────────────────────────────────────────────────
# MUST 4 append-path coverage: new-day-into-existing-month + write-failure
#
# These close the two MUST 4 subcases the spec/43 case list enumerates but the
# conformance core did not exercise standalone:
#   - new day file dropped into an ALREADY-minted YYYY-MM/ subdir (the
#     existing-month branch of append_entry's dir minting), and
#   - write failure: atomic_write raises OSError → append_entry propagates and
#     leaves NO partial day file (the atomic_write atomicity guarantee).


def test_filesystem_journal_append_new_day_existing_month(tmp_path: Path) -> None:
    """A second calendar date in an existing month reuses the minted YYYY-MM/ dir.

    First append mints journal/2026-06/; the second append (a DIFFERENT day in
    the SAME month) must land in that already-existing subdir without re-minting
    or erroring — and must NOT touch the first day's file.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)

    first = be.append_entry("Day 12 entry", when=date(2026, 6, 12))
    month_dir = agent_root / "journal" / "2026-06"
    assert month_dir.is_dir()

    # Second date in the SAME month — existing subdir branch.
    second = be.append_entry("Day 13 entry", when=date(2026, 6, 13))

    # Both day files live in the one shared month subdir.
    assert first.path.parent == month_dir.resolve()
    assert second.path.parent == month_dir.resolve()
    # Distinct day files (no in-place append onto the first day).
    assert first.path != second.path
    assert first.path.name == "2026-06-12.md"
    assert second.path.name == "2026-06-13.md"
    # The first day's content is untouched by the new-day append.
    assert first.path.read_text(encoding="utf-8") == "Day 12 entry"
    assert second.path.read_text(encoding="utf-8") == "Day 13 entry"


def test_filesystem_journal_append_write_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """append_entry propagates an OSError from atomic_write and leaves no file.

    spec/43 MUST 4 (write-failure case): a disk-full / write-error MUST propagate
    and MUST NOT leave a partially-written file. append_entry wraps atomic_write
    bare (atomic_write's own temp+rename atomicity is tested elsewhere), so the
    contract here is propagation + no day file + no orphaned month subdir
    artifact from the failed write.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)

    def _boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")  # ENOSPC

    # Patch the name as imported into the filesystem module (from .._io import
    # atomic_write), not the _io source — that is the binding append_entry calls.
    monkeypatch.setattr("atomic_agents.journal.filesystem.atomic_write", _boom)

    with pytest.raises(OSError) as excinfo:
        be.append_entry("doomed entry", when=date(2026, 6, 12))
    assert excinfo.value.errno == 28

    # No day file materialized for the failed write.
    day_file = agent_root / "journal" / "2026-06" / "2026-06-12.md"
    assert not day_file.exists()

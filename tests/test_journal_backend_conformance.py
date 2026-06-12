"""Conformance tests for JournalBackend Protocol (spec/43).

47 numbered TEST cases plus sub-lettered and redaction variants, 60 total,
covering the JournalBackend Implementer Contract (the protocol-behavior subset
is parametrized over every registered backend via the ``backend`` fixture; see
PARAMETRIZATION below).

NOTE ON NUMBERING: the "TEST N" labels below are this file's own granular
test-case indices, NOT spec/43 MUST numbers (spec/43 has exactly 10 MUSTs).
Each test case maps to its governing spec/43 MUST in the trailing parens so a
contributor reconciling code-to-spec lands on the right requirement.

  TEST 1  — side-effect-free construction (spec/43 MUST 2)
  TEST 2  — list_entries() returns [] for absent journal/ dir (spec/43 MUST 2)
  TEST 3  — append_entry() new day, new month subdir (spec/43 MUST 4)
  TEST 4  — append_entry() same-day second append accumulates (spec/43 MUST 4)
  TEST 5  — append_entry() returns JournalEntry with the caller's UN-RESOLVED path (un-resolved-shape invariant)
  TEST 6  — list_entries() path descending order (spec/43 MUST 7)
  TEST 7  — list_entries() limit=N returns top N (spec/43 MUST 7)
  TEST 8  — list_entries() newest_first=False returns oldest first (spec/43 MUST 7)
  TEST 9  — list_entries() golden selection (spec/43 MUST 7)
  TEST 10 — query_by_date() returns entries in range (spec/43 MUST 10)
  TEST 11 — query_by_date() excludes entries outside range (spec/43 MUST 10)
  TEST 12 — query_by_date() include-on-unparse fallback (spec/43 MUST 10)
  TEST 13 — query_by_date() returns [] for absent journal/ (spec/43 MUST 10)
  TEST 14 — storage isolation: two backends do not see each other's entries (spec/43 MUST 6)
  TEST 15 — backend_id stable across calls (spec/43 MUST 8)
  TEST 16 — JournalCapabilities is frozen dataclass (frozen-dataclass invariant)
  TEST 17 — JournalCapabilities(backend_id='x') valid with all defaults False (spec/43 MUST 3)
  TEST 18 — JournalCapabilities field types are bool (spec/43 MUST 3)
  TEST 19 — capabilities() returns JournalCapabilities (spec/43 MUST 3)
  TEST 20 — supports_canonical_export=True for filesystem (spec/43 MUST 3 / spec/40)
  TEST 21 — supports_date_query=True for filesystem (spec/43 MUST 3)
  TEST 22 — export() returns JournalExport type (spec/43 export contract)
  TEST 23 — export() empty when journal/ absent (spec/43 MUST 2 / export contract)
  TEST 24 — export() entries_with_bytes are relative paths (spec/43 export contract)
  TEST 25 — export() bytes are byte-identical to on-disk file (spec/43 Tier B golden)
  TEST 26 — export_all() equals export(None) (spec/43 export contract)
  TEST 27 — JournalEntry is frozen dataclass (frozen-dataclass invariant)
  TEST 28 — JournalEntry.path is the caller's UN-RESOLVED shape (un-resolved-shape invariant)
  TEST 29 — _redact_for_error_message() URL redaction (spec/43 MUST 5)
  TEST 30 — _redact_for_error_message() DSN redaction (spec/43 MUST 5)
  TEST 31 — _redact_for_error_message() truncation (spec/43 MUST 5)
  TEST 32 — _redact_for_error_message() passthrough for short value (spec/43 MUST 5)
  TEST 33 — get_default_journal_backend() empty-string env var uses filesystem
  TEST 34 — get_journal_backend() raises BackendNotRegistered for unknown id
  TEST 35 — env var dispatches registered custom backend
  TEST 36 — get_default_journal_backend() unknown env var raises BackendNotRegistered
  TEST 37 — JournalBackend is @runtime_checkable (isinstance check)
  TEST 38 — doctor.check_journal_backend returns PASS for empty agent (spec/27)
  TEST 39 — doctor.check_journal_backend returns PASS with entries + dual-probe (spec/27)
  TEST 40 — doctor.check_journal_backend returns FAIL for bad env var (spec/27)
  TEST 41 — doctor.check_journal_backend light-probe FAIL (list_entries raises)
  TEST 42 — doctor.check_journal_backend heavy-probe FAIL (read_bytes raises)
  TEST 43 — AtomicAgent.journal_backend ADOPT-NOW wiring (agent.py #427 PR1)
  TEST 44 — bundle render WITH path line (LOAD-BEARING divergence, spec/43)
  TEST 45 — agent render WITHOUT path line (LOAD-BEARING divergence, spec/43)
  TEST 46 — dream dict adapter {filename, text} shape (spec/43)
  TEST 47 — concurrent append_entry() no torn write (spec/43 MUST 9)

PARAMETRIZATION: protocol-behavior tests use the ``backend`` fixture parametrized
over BACKEND_FACTORIES (currently just 'filesystem'). Adding a second backend to
BACKEND_FACTORIES picks up every protocol-behavior test automatically.

Filesystem-specific tests are deliberately NOT parametrized: symlink guards,
byte-identity golden tests, ATOMIC_AGENTS_JOURNAL_BACKEND registry dispatch.
Pure-dataclass tests (JournalEntry/JournalCapabilities/JournalExport) need no backend.
"""

from __future__ import annotations

import os
import threading
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.journal.backend import JournalBackend
from atomic_agents.journal.filesystem import FilesystemJournalBackend
from atomic_agents.journal.types import (
    JournalCapabilities,
    JournalEntry,
    JournalExport,
)
from atomic_agents.exceptions import (
    AtomicAgentsError,
    BackendNotRegistered,
    JournalCorrupted,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures

BACKEND_FACTORIES = {
    "filesystem": lambda agent_root: FilesystemJournalBackend(agent_root),
}


@pytest.fixture(params=list(BACKEND_FACTORIES.keys()))
def backend(tmp_path: Path, request: pytest.FixtureRequest) -> JournalBackend:
    """A clean JournalBackend for each registered backend."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    return BACKEND_FACTORIES[request.param](agent_root)


@pytest.fixture(params=list(BACKEND_FACTORIES.keys()))
def backend_with_entries(tmp_path: Path, request: pytest.FixtureRequest):
    """A JournalBackend pre-loaded with 3 entries on different dates."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    be = BACKEND_FACTORIES[request.param](agent_root)

    d0 = date(2026, 5, 31)
    d1 = date(2026, 6, 1)
    d2 = date(2026, 6, 12)
    e0 = be.append_entry("May entry", when=d0)
    e1 = be.append_entry("June first entry", when=d1)
    e2 = be.append_entry("June twelfth entry", when=d2)
    return be, [e0, e1, e2], agent_root


# ──────────────────────────────────────────────────────────────────────────────
# TEST 1 — side-effect-free construction (spec/43 MUST 2)


def test_journal_construction_side_effect_free(tmp_path: Path) -> None:
    agent_root = tmp_path / "new_agent"
    # agent_root does NOT exist yet
    assert not agent_root.exists()
    # Construction must NOT raise
    be = FilesystemJournalBackend(agent_root)
    # Still no filesystem I/O performed
    assert not agent_root.exists()
    assert isinstance(be, FilesystemJournalBackend)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 2 — list_entries() returns [] for absent journal/ dir (spec/43 MUST 2)


def test_journal_list_entries_absent_journal(backend: JournalBackend) -> None:
    entries = backend.list_entries()
    assert entries == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 3 — append_entry() new day, new month subdir (spec/43 MUST 4)


def test_journal_append_new_day_new_month(backend: JournalBackend) -> None:
    d = date(2026, 7, 4)
    entry = backend.append_entry("Independence Day entry", when=d)
    assert isinstance(entry, JournalEntry)
    assert entry.date == d
    assert entry.text == "Independence Day entry"
    assert entry.path.exists()
    assert "2026-07" in str(entry.path)
    assert "2026-07-04.md" in entry.path.name


# ──────────────────────────────────────────────────────────────────────────────
# TEST 4 — append_entry() same-day second append accumulates (spec/43 MUST 4)


def test_journal_append_same_day_twice(backend: JournalBackend) -> None:
    d = date(2026, 6, 12)
    backend.append_entry("Morning entry", when=d)
    e2 = backend.append_entry("Evening entry", when=d)
    # Second entry should contain both texts
    assert "Morning entry" in e2.text
    assert "Evening entry" in e2.text
    # File on disk should contain both
    assert "Morning entry" in e2.path.read_text(encoding="utf-8")
    assert "Evening entry" in e2.path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 5 — append_entry() returns JournalEntry with the caller's UN-RESOLVED path
#
# The contract is NOT "absolute" — it is the caller's un-resolved shape (relative
# when agent_root is relative, symlinked when symlinked). The backend MUST NOT
# .resolve()/absolutize the path, because the legacy rglob callers emitted the
# un-resolved shape and the bundle backtick render line + _staleness_paths set
# (→ cascade .lease.json freshness) depend on it staying byte-identical.


def test_journal_append_returns_unresolved_path(
    backend: JournalBackend, tmp_path: Path
) -> None:
    # The fixture constructs the backend with an absolute agent_root
    # (tmp_path / "agent"), so the minted path is exactly that root's
    # un-resolved journal/YYYY-MM/YYYY-MM-DD.md shape.
    when = date(2026, 6, 12)
    entry = backend.append_entry("Test entry", when=when)
    expected = tmp_path / "agent" / "journal" / when.strftime("%Y-%m") / f"{when}.md"
    assert entry.path == expected, (
        f"Expected un-resolved minted path {expected!r}, got {entry.path!r}"
    )


def test_journal_append_relative_root_returns_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a RELATIVE agent_root the emitted path is correspondingly relative.

    Pins that the backend does NOT resolve/absolutize — a conforming backend
    constructed with a relative root returns a relative path (the byte-identity
    ruling forbids absolutizing). The fixture's accidental absoluteness must not
    be the only thing the suite checks.
    """
    monkeypatch.chdir(tmp_path)
    be = FilesystemJournalBackend(Path("agent"))
    when = date(2026, 6, 12)
    entry = be.append_entry("rel body", when=when)
    assert not entry.path.is_absolute(), (
        f"agent_root was relative; entry.path must stay relative (un-resolved), "
        f"got {entry.path!r}"
    )
    assert (
        entry.path == Path("agent") / "journal" / when.strftime("%Y-%m") / f"{when}.md"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 6 — list_entries() path descending order (spec/43 MUST 7)


def test_journal_list_entries_path_descending(
    backend_with_entries: tuple,
) -> None:
    be, entries, agent_root = backend_with_entries
    listed = be.list_entries()
    # Should be newest first (path descending)
    for i in range(len(listed) - 1):
        assert str(listed[i].path) >= str(listed[i + 1].path), (
            f"Expected path descending: {listed[i].path} >= {listed[i + 1].path}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 7 — list_entries() limit=N returns top N (spec/43 MUST 7)


def test_journal_list_entries_limit(backend_with_entries: tuple) -> None:
    be, entries, agent_root = backend_with_entries
    # 3 entries total; limit=2 should return 2
    listed = be.list_entries(limit=2)
    assert len(listed) == 2


# ──────────────────────────────────────────────────────────────────────────────
# TEST 8 — list_entries() newest_first=False returns oldest first (spec/43 MUST 7)


def test_journal_list_entries_oldest_first(backend_with_entries: tuple) -> None:
    be, entries, agent_root = backend_with_entries
    listed_asc = be.list_entries(newest_first=False)
    # oldest first = path ascending
    for i in range(len(listed_asc) - 1):
        assert str(listed_asc[i].path) <= str(listed_asc[i + 1].path), (
            f"Expected path ascending: {listed_asc[i].path} <= {listed_asc[i + 1].path}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 9 — list_entries() golden selection (spec/43 MUST 7)
# Pins that the entry with the lexicographically greatest path is first.


def test_journal_list_entries_golden_selection(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)

    # Write entries in forward chronological order
    dates_in_order = [date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 12)]
    for d in dates_in_order:
        be.append_entry(f"Entry for {d}", when=d)

    listed = be.list_entries()
    assert len(listed) == 3
    # First entry must be the lexicographically greatest path (2026-06)
    assert "2026-06" in str(listed[0].path)
    # Last entry must be the lexicographically smallest path (2026-04)
    assert "2026-04" in str(listed[-1].path)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 10 — query_by_date() returns entries in range (spec/43 MUST 10)


def test_journal_query_by_date_in_range(backend_with_entries: tuple) -> None:
    be, entries, agent_root = backend_with_entries
    # Entries at 2026-05-31, 2026-06-01, 2026-06-12
    start = date(2026, 6, 1)
    end = date(2026, 6, 30)
    results = be.query_by_date(start=start, end=end)
    # Should return only June entries (2 of 3)
    assert len(results) == 2
    result_dates = {e.date for e in results}
    assert date(2026, 6, 1) in result_dates
    assert date(2026, 6, 12) in result_dates
    assert date(2026, 5, 31) not in result_dates


# ──────────────────────────────────────────────────────────────────────────────
# TEST 11 — query_by_date() excludes entries outside range (spec/43 MUST 10)


def test_journal_query_by_date_exclude_outside(backend_with_entries: tuple) -> None:
    be, entries, agent_root = backend_with_entries
    # Date range that excludes all entries
    start = date(2027, 1, 1)
    end = date(2027, 12, 31)
    results = be.query_by_date(start=start, end=end)
    assert results == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 12 — query_by_date() include-on-unparse fallback (spec/43 MUST 10)


def test_journal_query_by_date_include_on_unparse(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)

    # Write a legitimate entry and also manually create a non-date-named file
    be.append_entry("Normal entry", when=date(2026, 6, 12))
    journal_dir = agent_root / "journal" / "misc"
    journal_dir.mkdir(parents=True, exist_ok=True)
    weird_file = journal_dir / "not-a-date.md"
    weird_file.write_text("Weird entry", encoding="utf-8")

    # Query a range that DOES NOT include 2026-06-12
    start = date(2024, 1, 1)
    end = date(2024, 12, 31)
    results = be.query_by_date(start=start, end=end)
    # The weird file should be INCLUDED (include-on-unparse fallback)
    weird_texts = [e.text for e in results if "not-a-date" in str(e.path)]
    assert len(weird_texts) == 1, (
        "Files with unparseable stems must be included regardless of date range"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 13 — query_by_date() returns [] for absent journal/ (spec/43 MUST 10)


def test_journal_query_by_date_absent_journal(backend: JournalBackend) -> None:
    results = backend.query_by_date(start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert results == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 14 — storage isolation: two backends do not see each other's entries (spec/43 MUST 6)


def test_journal_storage_isolation(tmp_path: Path) -> None:
    root_a = tmp_path / "agent_a"
    root_b = tmp_path / "agent_b"
    root_a.mkdir()
    root_b.mkdir()
    be_a = FilesystemJournalBackend(root_a)
    be_b = FilesystemJournalBackend(root_b)

    be_a.append_entry("Agent A entry", when=date(2026, 6, 12))
    entries_b = be_b.list_entries()
    assert entries_b == [], "backend_b must not see entries written by backend_a"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 15 — backend_id stable across calls (spec/43 MUST 8)


def test_journal_backend_id_stable(backend: JournalBackend) -> None:
    id1 = backend.backend_id
    id2 = backend.backend_id
    assert id1 == id2


def test_journal_backend_id_format() -> None:
    # filesystem id must satisfy [a-z0-9_-]
    import re

    be = FilesystemJournalBackend(Path("/tmp/test_agent"))
    assert re.match(r"^[a-z0-9_-]+$", be.backend_id), (
        f"backend_id {be.backend_id!r} must be [a-z0-9_-]"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 16 — JournalCapabilities is frozen dataclass (frozen-dataclass invariant)


def test_journal_capabilities_is_frozen() -> None:
    caps = JournalCapabilities(backend_id="test")
    with pytest.raises((AttributeError, TypeError)):
        caps.backend_id = "modified"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 17 — JournalCapabilities(backend_id='x') valid with all defaults False


def test_journal_capabilities_defaults_false() -> None:
    caps = JournalCapabilities(backend_id="custom")
    assert caps.supports_canonical_export is False
    assert caps.supports_date_query is False


# ──────────────────────────────────────────────────────────────────────────────
# TEST 18 — JournalCapabilities field types are bool (spec/43 MUST 3)


def test_journal_capabilities_field_types_are_bool() -> None:
    caps = JournalCapabilities(
        backend_id="x",
        supports_canonical_export=True,
        supports_date_query=True,
    )
    assert isinstance(caps.supports_canonical_export, bool)
    assert isinstance(caps.supports_date_query, bool)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 19 — capabilities() returns JournalCapabilities (spec/43 MUST 3)


def test_journal_capabilities_returns_type(backend: JournalBackend) -> None:
    caps = backend.capabilities()
    assert isinstance(caps, JournalCapabilities)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 20 — supports_canonical_export=True for filesystem (spec/43 MUST 3 / spec/40)


def test_journal_filesystem_supports_canonical_export(tmp_path: Path) -> None:
    be = FilesystemJournalBackend(tmp_path / "agent")
    caps = be.capabilities()
    assert caps.supports_canonical_export is True


# ──────────────────────────────────────────────────────────────────────────────
# TEST 21 — supports_date_query=True for filesystem (spec/43 MUST 3)


def test_journal_filesystem_supports_date_query(tmp_path: Path) -> None:
    be = FilesystemJournalBackend(tmp_path / "agent")
    caps = be.capabilities()
    assert caps.supports_date_query is True


# ──────────────────────────────────────────────────────────────────────────────
# TEST 22 — export() returns JournalExport type (spec/43 export contract)


def test_journal_export_returns_type(backend_with_entries: tuple) -> None:
    be, entries, agent_root = backend_with_entries
    result = be.export()
    assert isinstance(result, JournalExport)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 23 — export() empty when journal/ absent (spec/43 MUST 2 / export contract)


def test_journal_export_empty_when_absent(backend: JournalBackend) -> None:
    result = backend.export()
    assert isinstance(result, JournalExport)
    assert result.entries_with_bytes == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 24 — export() entries_with_bytes are relative paths (spec/43 export contract)


def test_journal_export_relative_paths(backend_with_entries: tuple) -> None:
    be, entries, agent_root = backend_with_entries
    result = be.export()
    assert len(result.entries_with_bytes) > 0
    for rel_path_str, raw_bytes in result.entries_with_bytes:
        # Must be relative (no leading /)
        assert not rel_path_str.startswith("/"), (
            f"export() path must be relative, got {rel_path_str!r}"
        )
        # Must start with 'journal/'
        assert rel_path_str.startswith("journal/"), (
            f"export() path must start with 'journal/', got {rel_path_str!r}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 25 — export() bytes are byte-identical to on-disk file (spec/43 Tier B golden)


def test_journal_export_bytes_identical(backend_with_entries: tuple) -> None:
    be, entries, agent_root = backend_with_entries
    result = be.export()
    for rel_path_str, raw_bytes in result.entries_with_bytes:
        on_disk_path = agent_root / rel_path_str
        assert on_disk_path.exists(), f"exported path {rel_path_str!r} not on disk"
        assert on_disk_path.read_bytes() == raw_bytes, (
            f"exported bytes for {rel_path_str!r} differ from on-disk file"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 26 — export_all() equals export(None) (spec/43 export contract)


def test_journal_export_all_equals_export_none(
    backend_with_entries: tuple,
) -> None:
    be, entries, agent_root = backend_with_entries
    result_none = be.export(None)
    result_all = be.export_all()
    # Same set of paths and bytes
    assert {t[0] for t in result_none.entries_with_bytes} == {
        t[0] for t in result_all.entries_with_bytes
    }


# ──────────────────────────────────────────────────────────────────────────────
# TEST 27 — JournalEntry is frozen dataclass (frozen-dataclass invariant)


def test_journal_entry_is_frozen(tmp_path: Path) -> None:
    entry = JournalEntry(
        date=date(2026, 6, 12),
        path=tmp_path / "journal/2026-06/2026-06-12.md",
        text="test",
    )
    with pytest.raises((AttributeError, TypeError)):
        entry.text = "modified"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 28 — JournalEntry.path is the caller's UN-RESOLVED shape
#
# Asserts the actual contract (path mirrors the agent_root shape, un-resolved),
# NOT "absolute" — which the impl explicitly disclaims and which only held
# vacuously because the fixture's agent_root happens to be absolute.


def test_journal_entry_path_unresolved(backend: JournalBackend, tmp_path: Path) -> None:
    when = date(2026, 6, 12)
    entry = backend.append_entry("Test", when=when)
    expected = tmp_path / "agent" / "journal" / when.strftime("%Y-%m") / f"{when}.md"
    assert entry.path == expected, (
        f"JournalEntry.path must be the un-resolved minted shape {expected!r}, "
        f"got {entry.path!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 29–32 — _redact_for_error_message() (spec/43 MUST 5)


def test_redact_url() -> None:
    from atomic_agents.journal import _redact_for_error_message

    assert _redact_for_error_message("postgres://user:pass@host/db") == "postgres://..."


def test_redact_dsn() -> None:
    from atomic_agents.journal import _redact_for_error_message

    assert (
        _redact_for_error_message("user:pass@host/db") == "[redacted-connection-string]"
    )


def test_redact_truncation() -> None:
    from atomic_agents.journal import _redact_for_error_message

    long_value = "a" * 100
    result = _redact_for_error_message(long_value)
    assert result.endswith("...")
    assert len(result) <= 35  # 32 + "..."


def test_redact_passthrough_short() -> None:
    from atomic_agents.journal import _redact_for_error_message

    assert _redact_for_error_message("filesystem") == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 33 — get_default_journal_backend() empty-string env var uses filesystem


def test_get_default_journal_backend_empty_env(tmp_path: Path) -> None:
    from atomic_agents.journal import get_default_journal_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    with patch.dict(os.environ, {"ATOMIC_AGENTS_JOURNAL_BACKEND": ""}):
        be = get_default_journal_backend(agent_root)
    assert isinstance(be, FilesystemJournalBackend)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 34 — get_journal_backend() raises BackendNotRegistered for unknown id


def test_get_journal_backend_unknown_id() -> None:
    from atomic_agents.journal import get_journal_backend

    with pytest.raises(BackendNotRegistered):
        get_journal_backend("no_such_backend_xyz")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 35 — env var dispatches registered custom backend


def test_get_default_journal_backend_custom_backend(tmp_path: Path) -> None:
    from atomic_agents.journal import (
        get_default_journal_backend,
        register_journal_backend,
        unregister_journal_backend,
    )

    class _CustomJournalBackend(FilesystemJournalBackend):
        @property
        def backend_id(self) -> str:
            return "custom_test_backend"

    register_journal_backend("custom_test_backend", _CustomJournalBackend)
    try:
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        with patch.dict(
            os.environ,
            {"ATOMIC_AGENTS_JOURNAL_BACKEND": "custom_test_backend"},
        ):
            be = get_default_journal_backend(agent_root)
        assert isinstance(be, _CustomJournalBackend)
    finally:
        unregister_journal_backend("custom_test_backend")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 36 — get_default_journal_backend() unknown env var raises BackendNotRegistered


def test_get_default_journal_backend_unknown_env(tmp_path: Path) -> None:
    from atomic_agents.journal import get_default_journal_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    with patch.dict(
        os.environ, {"ATOMIC_AGENTS_JOURNAL_BACKEND": "no_such_backend_xyz"}
    ):
        with pytest.raises(BackendNotRegistered):
            get_default_journal_backend(agent_root)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 37 — JournalBackend is @runtime_checkable (isinstance check)


def test_journal_backend_is_runtime_checkable(tmp_path: Path) -> None:
    be = FilesystemJournalBackend(tmp_path / "agent")
    assert isinstance(be, JournalBackend)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 38 — doctor.check_journal_backend returns PASS for empty agent (spec/27)


def test_doctor_check_journal_backend_pass_empty(tmp_path: Path) -> None:
    from atomic_agents.doctor import check_journal_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    result = check_journal_backend(agent_root)
    assert result.status == "pass"
    assert result.detail["journal_entries_found"] == 0
    assert result.detail["read_bytes_probed"] is False


# ──────────────────────────────────────────────────────────────────────────────
# TEST 39 — doctor.check_journal_backend returns PASS with entries + dual-probe (spec/27)


def test_doctor_check_journal_backend_pass_with_entries(tmp_path: Path) -> None:
    from atomic_agents.doctor import check_journal_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Test entry", when=date(2026, 6, 12))

    result = check_journal_backend(agent_root)
    assert result.status == "pass"
    assert result.detail["journal_entries_found"] == 1
    assert result.detail["read_bytes_probed"] is True


# ──────────────────────────────────────────────────────────────────────────────
# TEST 40 — doctor.check_journal_backend returns FAIL for bad env var (spec/27)


def test_doctor_check_journal_backend_fail_bad_env(tmp_path: Path) -> None:
    from atomic_agents.doctor import check_journal_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    with patch.dict(
        os.environ, {"ATOMIC_AGENTS_JOURNAL_BACKEND": "no_such_backend_xyz"}
    ):
        result = check_journal_backend(agent_root)
    assert result.status == "fail"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 41 — doctor.check_journal_backend light-probe FAIL (list_entries raises)


def test_doctor_check_journal_backend_light_probe_fail(tmp_path: Path) -> None:
    from atomic_agents.doctor import check_journal_backend
    from atomic_agents.journal import (
        register_journal_backend,
        unregister_journal_backend,
    )

    class _BrokenListJournalBackend(FilesystemJournalBackend):
        @property
        def backend_id(self) -> str:
            return "broken_list_test"

        def list_entries(self, limit=None, newest_first=True):
            raise RuntimeError("list_entries exploded")

    register_journal_backend("broken_list_test", _BrokenListJournalBackend)
    try:
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        with patch.dict(
            os.environ,
            {"ATOMIC_AGENTS_JOURNAL_BACKEND": "broken_list_test"},
        ):
            result = check_journal_backend(agent_root)
        assert result.status == "fail"
        assert "list_entries" in result.message
    finally:
        unregister_journal_backend("broken_list_test")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 41b — doctor permissions-class fix_hint is NORMALIZED across probes.
# A PermissionError surfaced by list_entries (the journal/ DIRECTORY denied)
# MUST yield the SAME fix_hint as a PermissionError surfaced by read_bytes (an
# individual ENTRY file denied). The verdict stays FAIL in both; only the
# fix_hint wording is unified so operators get consistent remediation regardless
# of which probe trips first.


def test_doctor_check_journal_backend_permission_fix_hint_normalized(
    tmp_path: Path,
) -> None:
    from atomic_agents.doctor import check_journal_backend
    from atomic_agents.journal import (
        register_journal_backend,
        unregister_journal_backend,
    )

    class _PermDeniedListBackend(FilesystemJournalBackend):
        @property
        def backend_id(self) -> str:
            return "perm_denied_list_test"

        def list_entries(self, limit=None, newest_first=True):
            raise PermissionError("journal/ directory not readable")

    class _PermDeniedReadBytesBackend(FilesystemJournalBackend):
        @property
        def backend_id(self) -> str:
            return "perm_denied_readbytes_test"

        def list_entries(self, limit=None, newest_first=True):
            mock_path = MagicMock()
            mock_path.read_bytes.side_effect = PermissionError("entry not readable")
            mock_path.is_absolute.return_value = True
            return [JournalEntry(date=date(2026, 6, 12), path=mock_path, text="fake")]

    register_journal_backend("perm_denied_list_test", _PermDeniedListBackend)
    register_journal_backend("perm_denied_readbytes_test", _PermDeniedReadBytesBackend)
    try:
        agent_root = tmp_path / "agent"
        agent_root.mkdir()

        with patch.dict(
            os.environ, {"ATOMIC_AGENTS_JOURNAL_BACKEND": "perm_denied_list_test"}
        ):
            list_result = check_journal_backend(agent_root)
        with patch.dict(
            os.environ, {"ATOMIC_AGENTS_JOURNAL_BACKEND": "perm_denied_readbytes_test"}
        ):
            read_result = check_journal_backend(agent_root)

        # Both probes FAIL on a permissions error.
        assert list_result.status == "fail"
        assert read_result.status == "fail"
        # Both carry a non-empty fix_hint (the list_entries probe previously had
        # none) and the fix_hint is IDENTICAL — the normalization this test pins.
        assert list_result.fix_hint
        assert read_result.fix_hint
        assert list_result.fix_hint == read_result.fix_hint
        assert "permissions" in list_result.fix_hint
    finally:
        unregister_journal_backend("perm_denied_list_test")
        unregister_journal_backend("perm_denied_readbytes_test")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42 — doctor.check_journal_backend heavy-probe FAIL (read_bytes raises)
# Simulated by writing an entry then removing the file between probe 1 and probe 2.
# Since the doctor reads live, we test the unexpected-error branch by mocking.


def test_doctor_check_journal_backend_heavy_probe_fail(tmp_path: Path) -> None:
    from atomic_agents.doctor import check_journal_backend
    from atomic_agents.journal import (
        register_journal_backend,
        unregister_journal_backend,
    )

    class _BrokenReadBytesBackend(FilesystemJournalBackend):
        @property
        def backend_id(self) -> str:
            return "broken_readbytes_test"

        def list_entries(self, limit=None, newest_first=True):
            # Return a mock entry whose path.read_bytes() raises
            mock_path = MagicMock()
            mock_path.read_bytes.side_effect = PermissionError("no access")
            mock_path.is_absolute.return_value = True
            return [
                JournalEntry(
                    date=date(2026, 6, 12),
                    path=mock_path,
                    text="fake",
                )
            ]

    register_journal_backend("broken_readbytes_test", _BrokenReadBytesBackend)
    try:
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        with patch.dict(
            os.environ,
            {"ATOMIC_AGENTS_JOURNAL_BACKEND": "broken_readbytes_test"},
        ):
            result = check_journal_backend(agent_root)
        assert result.status == "fail"
    finally:
        unregister_journal_backend("broken_readbytes_test")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42b — doctor TOCTOU: read_bytes() FileNotFoundError → PASS (benign race)


def test_doctor_check_journal_backend_toctou_filenotfound_passes(
    tmp_path: Path,
) -> None:
    """A file that vanishes between list_entries() and read_bytes() is a benign
    TOCTOU race — doctor PASSes (FileNotFoundError, NOT a FAIL). This pins the
    fix where the benign vanished-file race was previously misclassified as FAIL
    (it raises FileNotFoundError, not AtomicAgentsError)."""
    from atomic_agents.doctor import check_journal_backend
    from atomic_agents.journal import (
        register_journal_backend,
        unregister_journal_backend,
    )

    agent_root = tmp_path / "agent"
    agent_root.mkdir()

    class _VanishedEntryBackend(FilesystemJournalBackend):
        @property
        def backend_id(self) -> str:
            return "vanished_entry_test"

        def list_entries(self, limit=None, newest_first=True):
            # A real, in-vault path that does NOT exist (vanished after listing).
            ghost = agent_root / "journal" / "2026-06" / "2026-06-12.md"
            return [JournalEntry(date=date(2026, 6, 12), path=ghost, text="gone")]

    register_journal_backend("vanished_entry_test", _VanishedEntryBackend)
    try:
        with patch.dict(
            os.environ,
            {"ATOMIC_AGENTS_JOURNAL_BACKEND": "vanished_entry_test"},
        ):
            result = check_journal_backend(agent_root)
        assert result.status == "pass", (
            "benign TOCTOU (file vanished) must PASS, not FAIL"
        )
    finally:
        unregister_journal_backend("vanished_entry_test")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42c — doctor FAILs on a symlinked journal/ DIRECTORY escaping agent_root.
# This is the real escape vector: list_entries() CATCHES the backend's
# PathTraversalError and returns [], so the light probe alone would silently
# PASS. Doctor probes _journal_dir() DIRECTLY and FAILs. (#427 PR1 round-4 fix.)


def test_doctor_check_journal_backend_directory_escape_fails(
    tmp_path: Path,
) -> None:
    import shutil

    from atomic_agents.doctor import check_journal_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    outside_dir = tmp_path / "outside_journal"
    outside_dir.mkdir()

    # Write a real entry first, then replace journal/ with a symlink pointing
    # outside agent_root (the genuine misconfiguration class).
    journal_dir = agent_root / "journal"
    journal_dir.mkdir()
    FilesystemJournalBackend(agent_root).append_entry(
        "Entry before symlink", when=date(2026, 6, 12)
    )
    shutil.rmtree(str(journal_dir))
    journal_dir.symlink_to(outside_dir)

    # list_entries() returns [] here (catches PathTraversalError), so the
    # directory-escape must be caught by doctor's direct _journal_dir() probe.
    assert FilesystemJournalBackend(agent_root).list_entries() == []

    result = check_journal_backend(agent_root)
    assert result.status == "fail", (
        "a symlinked journal/ DIRECTORY escaping agent_root must FAIL doctor — "
        "list_entries() returns [] so the direct _journal_dir() probe catches it"
    )
    assert "agent_root" in result.message or "containment" in result.message


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42d — doctor PASSes on an individual symlinked .md ENTRY resolving outside
# agent_root. The ADOPT-NOW byte-identity ruling requires the runtime to FOLLOW
# such an entry; doctor must agree (NOT reintroduce a per-entry containment FAIL).
# Distinct from TEST 42c (a DIRECTORY escape, which DOES FAIL). (#427 PR1.)


def test_doctor_check_journal_backend_symlinked_entry_passes(
    tmp_path: Path,
) -> None:
    from atomic_agents.doctor import check_journal_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    # A real target file OUTSIDE agent_root.
    outside_file = tmp_path / "outside_entry.md"
    outside_file.write_text("# Journal\n\nOutside content\n", encoding="utf-8")

    # An individual .md entry inside journal/ that is a SYMLINK to the outside
    # file (journal/ itself stays a real directory under agent_root).
    month_dir = agent_root / "journal" / "2026-06"
    month_dir.mkdir(parents=True)
    (month_dir / "2026-06-12.md").symlink_to(outside_file)

    result = check_journal_backend(agent_root)
    assert result.status == "pass", (
        "an individual symlinked .md entry resolving outside agent_root is a "
        "deliberate PASS — the runtime follows it through for byte-identity"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 43 — AtomicAgent.journal_backend ADOPT-NOW wiring (agent.py #427 PR1)


def test_atomic_agent_journal_backend_wired(tmp_path: Path) -> None:
    """AtomicAgent.__init__ must expose a .journal_backend attribute."""
    from atomic_agents.agent import AtomicAgent

    agents_root = tmp_path
    agent_name = "test_agent"
    agent_root = agents_root / agent_name
    agent_root.mkdir()
    # Minimal persona directory to satisfy agent construction
    persona_dir = agent_root / "persona"
    persona_dir.mkdir()
    (persona_dir / "IDENTITY.md").write_text("# Test Agent\n", encoding="utf-8")

    agent = AtomicAgent(name=agent_name, agents_root=agents_root)
    assert hasattr(agent, "journal_backend"), (
        "AtomicAgent must have a .journal_backend attribute after #427 PR1"
    )
    assert isinstance(agent.journal_backend, JournalBackend), (
        "agent.journal_backend must be a JournalBackend instance"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 44 — bundle render WITH path line (LOAD-BEARING divergence, spec/43)


def _legacy_bundle_render(instance_root: Path, n: int) -> list[str]:
    """Reproduce the PRE-#427 bundle._render_journal_breakpoint output.

    This is the golden reference: sort top-N paths by filename desc (no read),
    then render each via _safe_read_text. Used to prove the adopted
    bundle._render_journal_breakpoint is byte-identical to the legacy path.
    """
    from atomic_agents.bundle import _safe_read_text

    journal_dir = instance_root / "journal"
    if not journal_dir.is_dir():
        return []
    entries = sorted(journal_dir.rglob("*.md"), reverse=True)[:n]
    rendered = [f"# Journal — {p.stem}\n`{p}`\n\n{_safe_read_text(p)}" for p in entries]
    if not rendered:
        return []
    return [
        "# === BREAKPOINT 4: Daily (recent journal) ===",
        "## Recent journal\n\n" + "\n\n---\n\n".join(rendered),
    ]


def test_bundle_render_journal_byte_identical_to_legacy(tmp_path: Path) -> None:
    """The REAL bundle._render_journal_breakpoint matches the legacy render byte-for-byte.

    Drives the actual function (not an inline f-string simulation) so a
    regression in the render path, the backend's path shape, or the corrupt-entry
    handling is caught. Includes the backtick path line (LOAD-BEARING divergence).
    """
    from atomic_agents.bundle import _render_journal_breakpoint, RECENT_JOURNAL_DEFAULT

    agent_root = tmp_path / "test_agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Test journal text", when=date(2026, 6, 12))

    actual = _render_journal_breakpoint(agent_root, journal_backend=be)
    expected = _legacy_bundle_render(agent_root, RECENT_JOURNAL_DEFAULT)
    assert actual == expected
    # Sanity: the path line is present.
    assert "`" in actual[1] and "journal/2026-06/2026-06-12.md" in actual[1]


def test_bundle_render_journal_symlinked_root_byte_identical(tmp_path: Path) -> None:
    """Under a symlinked agent root, the bundle render path line stays UN-resolved.

    Regression guard for the path-resolution divergence: the backend must NOT
    emit a /private/... resolved path where legacy emitted the symlink path.
    """
    from atomic_agents.bundle import _render_journal_breakpoint

    real = tmp_path / "realvault"
    real.mkdir()
    link = tmp_path / "symvault"
    link.symlink_to(real, target_is_directory=True)

    be = FilesystemJournalBackend(link)
    be.append_entry("symlink body", when=date(2026, 6, 12))

    actual = _render_journal_breakpoint(link, journal_backend=be)
    expected = _legacy_bundle_render(link, 1)
    assert actual == expected
    # The backtick line must carry the symlink (un-resolved) path, not /private/...
    assert f"`{link}/journal/2026-06/2026-06-12.md`" in actual[1]


def test_bundle_render_symlinked_entry_byte_identical(tmp_path: Path) -> None:
    """A symlinked .md ENTRY in the top-N must be selected byte-identical to legacy.

    Legacy bundle._load_recent_journal / _source_paths followed symlinks
    (rglob + _safe_read_text + is_file() all follow). A per-entry symlink skip
    would drop the slot, backfill an older entry, and shift BOTH the rendered
    journal AND the _source_paths/_staleness_paths set that drives cascade
    .lease.json freshness. This is the regression-proof golden the round-2
    convergence required.
    """
    from atomic_agents.bundle import _render_journal_breakpoint, RECENT_JOURNAL_DEFAULT

    agent_root = tmp_path / "agent"
    month = agent_root / "journal" / "2026-06"
    month.mkdir(parents=True)
    # Older real entry.
    (month / "2026-06-11.md").write_text("older real", encoding="utf-8")
    # Newest entry is a symlink-to-file (target resolves under agent_root, so the
    # directory-containment guard does NOT trip; the entry is harmless + included).
    sym_target = month / "2026-06-12.real.md.src"
    sym_target.write_text("symlinked newest body", encoding="utf-8")
    (month / "2026-06-12.md").symlink_to(sym_target)

    be = FilesystemJournalBackend(agent_root)
    actual = _render_journal_breakpoint(agent_root, journal_backend=be)
    expected = _legacy_bundle_render(agent_root, RECENT_JOURNAL_DEFAULT)
    # Entry-for-entry + assembled-text byte-identity with the legacy reference.
    assert actual == expected
    # The symlinked newest entry IS selected (not backfilled by the older real one).
    assert "symlinked newest body" in actual[1]
    assert "`" in actual[1] and "journal/2026-06/2026-06-12.md`" in actual[1]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 44b — _source_paths / _staleness_paths journal-selection parity (cascade
# .lease.json freshness). The most load-bearing invariant the rulings flagged:
# the journal subset of _source_paths drives the staleness hash that the cascade
# .lease.json freshness check keys on. Render parity (TEST 44/44a) does not by
# itself prove the PATH set is identical, since _source_paths runs an additional
# is_file() filter (which drops broken symlinks the renderer would have read as a
# warning slot). Assert the journal-derived Path list, entry-for-entry, equals the
# legacy sorted(rglob, reverse=True)[:N] filtered by is_file().


def _legacy_journal_source_paths(agent_root: Path, n: int) -> list[Path]:
    """Legacy journal subset of _source_paths: rglob top-N, then is_file() filter."""
    journal_dir = agent_root / "journal"
    if not journal_dir.is_dir():
        return []
    top_n = sorted(journal_dir.rglob("*.md"), reverse=True)[:n]
    return [p for p in top_n if p.is_file()]


def test_source_paths_journal_selection_parity(tmp_path: Path) -> None:
    """_source_paths journal Paths == legacy rglob top-N filtered by is_file().

    Builds journal/ with MORE than RECENT_JOURNAL_DEFAULT entries plus a broken
    symlink .md (target removed) to exercise the trailing is_file() filter that
    _source_paths applies. The journal-derived subset of _source_paths(...,
    journal_backend=be) must equal the legacy reference entry-for-entry, or the
    cascade .lease.json freshness set silently shifts.
    """
    from atomic_agents.bundle import (
        _source_paths,
        _staleness_paths,
        RECENT_JOURNAL_DEFAULT,
    )

    agent_root = tmp_path / "agent"
    month = agent_root / "journal" / "2026-06"
    month.mkdir(parents=True)
    # MORE than RECENT_JOURNAL_DEFAULT entries so the limit actually bites.
    for day in range(1, RECENT_JOURNAL_DEFAULT + 4):
        (month / f"2026-06-{day:02d}.md").write_text(f"entry {day}", encoding="utf-8")
    # A broken-symlink .md (target removed): rglob matches it, but is_file()
    # returns False, so _source_paths drops it — exercising the filter.
    broken_target = month / "2026-06-30.real.md.src"
    broken_target.write_text("temp", encoding="utf-8")
    broken_link = month / "2026-06-30.md"
    broken_link.symlink_to(broken_target)
    broken_target.unlink()  # now broken_link is a dangling symlink

    be = FilesystemJournalBackend(agent_root)

    # _source_paths returns ALL source paths (persona/memory/journal/etc.);
    # restrict to the journal subset for the parity assertion.
    actual_all = _source_paths(agent_root, journal_backend=be)
    journal_root = agent_root / "journal"
    actual_journal = [p for p in actual_all if journal_root in p.parents]

    expected = _legacy_journal_source_paths(agent_root, RECENT_JOURNAL_DEFAULT)
    assert actual_journal == expected, (
        "journal subset of _source_paths must be entry-for-entry identical to the "
        "legacy rglob top-N (is_file-filtered) selection — cascade .lease.json "
        "freshness keys on this exact set"
    )
    # The dangling symlink MUST be filtered out (is_file() False) in both.
    assert broken_link not in actual_journal

    # _staleness_paths calls _source_paths, so the journal subset must match too.
    stale_all = _staleness_paths(agent_root, journal_backend=be)
    stale_journal = [p for p in stale_all if journal_root in p.parents]
    assert stale_journal == expected


# ──────────────────────────────────────────────────────────────────────────────
# TEST 45 — agent render WITHOUT path line (LOAD-BEARING divergence, spec/43)


def _legacy_agent_render(agent_root: Path, n: int) -> list[str]:
    """Reproduce the PRE-#427 agent._load_recent_journal render byte-for-byte.

    Legacy agent: rglob('*.md') under journal/, reverse-sort by full path,
    take newest-N, render '# Journal — {stem}\\n\\n{text}' (NO path line),
    reading each selected slot via the same errors='replace' degrade-but-keep
    behavior the adopted path now matches.
    """
    journal_dir = agent_root / "journal"
    if not journal_dir.is_dir():
        return []
    paths = sorted(journal_dir.rglob("*.md"), reverse=True)[:n]
    rendered: list[str] = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rendered.append(f"# Journal — {p.stem}\n\n{text}")
    return rendered


class _AgentJournalStub:
    """Minimal carrier so we can drive the REAL AtomicAgent._load_recent_journal.

    The method only touches self.journal_backend and self._recent_journal, so
    binding it to this stub exercises the actual runtime render path (not a
    re-inlined f-string copy) — the same real-function discipline TEST 44 uses
    for the bundle site.
    """

    def __init__(self, journal_backend: JournalBackend) -> None:
        self.journal_backend = journal_backend
        self._recent_journal: list[str] = []


def test_agent_render_journal_no_path_line(tmp_path: Path) -> None:
    """The REAL agent._load_recent_journal matches the legacy render byte-for-byte.

    Drives AtomicAgent._load_recent_journal (bound to a minimal stub) against a
    reproduced-legacy reference and asserts full byte-identity — mirroring
    TEST 44's _legacy_bundle_render approach so the agent site has the same
    regression-proof golden the bundle site has. The agent format omits the
    backtick path line (LOAD-BEARING divergence from bundle); a stray format
    change would now fail here.

    Scope note: for UTF-8 entries the adopted agent render is byte-identical to
    legacy. The ONE deliberate behavior change — corrupt (non-UTF-8) entries are
    now degraded-but-kept (with a WARNING comment) rather than raising
    UnicodeDecodeError — is intentionally NOT folded into this byte-identity
    golden (legacy agent did not inject the warning); it is covered by the
    bundle corrupt-entry golden (TEST 45b) which is the degrade-but-keep
    reference the agent path now matches.
    """
    from atomic_agents.agent import AtomicAgent, RECENT_JOURNAL_DEFAULT

    agent_root = tmp_path / "test_agent"
    month = agent_root / "journal" / "2026-06"
    month.mkdir(parents=True)
    (month / "2026-06-12.md").write_text("Test journal text", encoding="utf-8")
    (month / "2026-06-11.md").write_text("older valid", encoding="utf-8")

    be = FilesystemJournalBackend(agent_root)
    stub = _AgentJournalStub(be)
    AtomicAgent._load_recent_journal(stub, n=RECENT_JOURNAL_DEFAULT)

    expected = _legacy_agent_render(agent_root, RECENT_JOURNAL_DEFAULT)
    assert stub._recent_journal == expected, (
        "agent render must be byte-identical to the legacy newest-N render"
    )
    # No rendered slot carries a backtick path line (the bundle-only divergence).
    for block in stub._recent_journal:
        assert "`" not in block.split("\n", 2)[1], (
            "agent render must NOT include a backtick path line"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 45b — bundle corrupt entry keeps slot byte-identical to legacy (spec/43)


def test_bundle_render_corrupt_entry_keeps_slot(tmp_path: Path) -> None:
    """A non-UTF-8 entry in the top-N keeps its slot, byte-identical to legacy.

    Legacy bundle read every selected slot through _safe_read_text (errors=replace
    + warning), it did NOT drop the slot. The adopted path must match — dropping
    would backfill an older entry and shift the staleness path set.
    """
    from atomic_agents.bundle import _render_journal_breakpoint, RECENT_JOURNAL_DEFAULT

    agent_root = tmp_path / "agent"
    month = agent_root / "journal" / "2026-06"
    month.mkdir(parents=True)
    # Corrupt (non-UTF-8) entry as the NEWEST so it falls inside the top-N window.
    (month / "2026-06-12.md").write_bytes(b"\xff\xfe corrupt bytes")
    (month / "2026-06-11.md").write_text("older valid", encoding="utf-8")

    be = FilesystemJournalBackend(agent_root)
    actual = _render_journal_breakpoint(agent_root, journal_backend=be)
    expected = _legacy_bundle_render(agent_root, RECENT_JOURNAL_DEFAULT)
    assert actual == expected
    # The corrupt slot is KEPT (warning comment present), not dropped — and the
    # newest selected entry is the corrupt one, not the older valid backfill.
    assert "non-UTF-8 bytes" in actual[1]
    assert "older valid" not in actual[1]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 46 — dream dict adapter {filename, text} shape (spec/43)


def test_dream_journal_dict_adapter(tmp_path: Path) -> None:
    """dream.py adapter must produce {filename, text} dicts from JournalEntry objects."""
    agent_root = tmp_path / "test_agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    be.append_entry("Dream entry", when=date(2026, 6, 12))
    entries = be.query_by_date(start=date(2026, 1, 1), end=date.max)
    # Simulate the dream.py dict adapter
    journal_dicts = [{"filename": e.path.name, "text": e.text} for e in entries]
    assert len(journal_dicts) == 1
    assert journal_dicts[0]["filename"] == "2026-06-12.md"
    assert journal_dicts[0]["text"] == "Dream entry"
    # Keys must be exactly {filename, text} — not path, not date
    assert set(journal_dicts[0].keys()) == {"filename", "text"}


# ──────────────────────────────────────────────────────────────────────────────
# TEST 46b — dream date-window includes FUTURE-dated entries (byte-identity, spec/43)


def _legacy_read_journal_entries(agent_root: Path, lookback_days: int) -> list[dict]:
    """Reproduce the PRE-#427 dream._read_journal_entries (lower-bound only)."""
    journal_dir = agent_root / "journal"
    entries: list[dict] = []
    if not journal_dir.exists():
        return entries
    cutoff = date.today() - timedelta(days=lookback_days)
    for path in sorted(journal_dir.rglob("*.md"), reverse=True):
        try:
            entry_date = date.fromisoformat(path.stem)
            if entry_date < cutoff:
                continue
        except (ValueError, TypeError):
            pass
        try:
            entries.append({"filename": path.name, "text": path.read_text("utf-8")})
        except OSError:
            continue
    return entries


def test_dream_window_includes_future_dated_entry(tmp_path: Path) -> None:
    """A future-dated entry is INCLUDED, matching legacy's lower-bound-only filter.

    Legacy dream._read_journal_entries had no upper bound. The adopted dream path
    passes end=date.max for exactly this reason. This test pins the byte-identity:
    the backend's dream-window selection must match legacy entry-for-entry even
    with a future-dated stem.
    """
    agent_root = tmp_path / "agent"
    future = date.today() + timedelta(days=400)
    today = date.today()
    for d in (future, today):
        month = agent_root / "journal" / d.strftime("%Y-%m")
        month.mkdir(parents=True, exist_ok=True)
        (month / f"{d.isoformat()}.md").write_text(f"entry {d}", encoding="utf-8")

    be = FilesystemJournalBackend(agent_root)
    cutoff = today - timedelta(days=30)
    raw = be.query_by_date(start=cutoff, end=date.max)
    adapted = [{"filename": e.path.name, "text": e.text} for e in raw]

    legacy = _legacy_read_journal_entries(agent_root, 30)
    assert adapted == legacy, "dream window must match legacy entry-for-entry"
    # The future-dated entry must be present.
    assert any(future.isoformat() in d["filename"] for d in adapted)


def test_dream_window_includes_symlinked_entry(tmp_path: Path) -> None:
    """A symlinked .md entry in the dream window is INCLUDED, matching legacy.

    Legacy dream._read_journal_entries read each path via read_text, which follows
    symlinks. A per-entry symlink skip would drop the entry and silently change
    dream-consolidation input across backends. Pins entry-for-entry parity.
    """
    agent_root = tmp_path / "agent"
    today = date.today()
    month = agent_root / "journal" / today.strftime("%Y-%m")
    month.mkdir(parents=True, exist_ok=True)
    # One real in-window entry + a symlinked in-window entry (target under agent_root).
    (month / f"{today.isoformat()}.md").write_text("real today", encoding="utf-8")
    older = today - timedelta(days=1)
    sym_target = month / f"{older.isoformat()}.real.md.src"
    sym_target.write_text("symlinked older", encoding="utf-8")
    (month / f"{older.isoformat()}.md").symlink_to(sym_target)

    be = FilesystemJournalBackend(agent_root)
    cutoff = today - timedelta(days=30)
    raw = be.query_by_date(start=cutoff, end=date.max)
    adapted = [{"filename": e.path.name, "text": e.text} for e in raw]

    legacy = _legacy_read_journal_entries(agent_root, 30)
    assert adapted == legacy, "dream window must select symlinked entries as legacy did"
    assert any(d["text"] == "symlinked older" for d in adapted)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 47 — concurrent append_entry() no torn write (spec/43 MUST 9)


def test_journal_concurrent_append_no_torn_write(tmp_path: Path) -> None:
    """Concurrent same-day appends must not produce torn or interleaved writes."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    d = date(2026, 6, 12)

    results: list[JournalEntry] = []
    errors: list[Exception] = []

    def _append(text: str) -> None:
        try:
            entry = be.append_entry(text, when=d)
            results.append(entry)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=_append, args=(f"Thread entry {i}",)) for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent appends raised errors: {errors}"

    # Final file should contain all 10 entries
    entries = be.list_entries()
    assert len(entries) == 1  # one day file
    final_text = entries[0].text
    for i in range(10):
        assert f"Thread entry {i}" in final_text, (
            f"Thread entry {i} missing from final journal file (torn write?)"
        )


# ──────────────────────────────────────────────────────────────────────────────
# append_entry FAILS LOUD with JournalCorrupted on an unreadable existing day file


def test_journal_append_corrupt_existing_raises_journal_corrupted(
    tmp_path: Path,
) -> None:
    """append_entry FAILS LOUD (JournalCorrupted) when the existing day file is
    unreadable during read-modify-append — a lost narrative episode must not be
    silent (Principle #5). JournalCorrupted subclasses AtomicAgentsError so the
    base-class catch still sees it."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    be = FilesystemJournalBackend(agent_root)
    d = date(2026, 6, 12)
    be.append_entry("first", when=d)

    day_file = agent_root / "journal" / "2026-06" / "2026-06-12.md"
    day_file.chmod(0o000)
    if os.getuid() == 0:
        pytest.skip("chmod 0o000 does not restrict root; cannot test unreadable path")
    try:
        with pytest.raises(JournalCorrupted):
            be.append_entry("second", when=d)
        # Subclass check — base-class catchers still see it.
        try:
            be.append_entry("third", when=d)
        except AtomicAgentsError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected JournalCorrupted (AtomicAgentsError)")
    finally:
        day_file.chmod(0o644)

"""Canonical types for the JournalBackend Protocol (spec/43).

NOTE: JournalEntry and JournalCapabilities are FROZEN dataclasses —
deliberate INVERSE of the goal/outcome MUTABLE pattern.

Journal entries are append-only immutable episodes: once appended, an
entry is never mutated through the Protocol. The state-machine justification
that earned goal/outcome their MUTABLE exception (status transitions, in-place
mutation during the run loop) is ABSENT here. The correct analog is
logs/types.py's frozen LogEntry, not goal/types.py's mutable Goal.

Document the frozen choice here as the INVERSE of goal/outcome's 'MUTABLE —
deliberate' note so future contributors reading types.py understand the
divergence is intentional.

JournalExport is a dataclass subclassing ExportableResult (spec/40).

IMPORTANT — path: Path field on JournalEntry:
  path carries the CALLER'S UN-resolved path shape — the full
  (agent_root / 'journal' / 'YYYY-MM' / 'YYYY-MM-DD.md') path, NOT resolved
  or absolutized (e.g. /home/user/agents/caldwell/journal/2026-06/2026-06-12.md
  for an absolute agent_root, but a relative or symlinked path when agent_root
  was relative/symlinked). This matches the three legacy rglob callers
  (bundle.py, agent.py, dream.py), which all rglob'd an UN-resolved
  (instance_root / 'journal') dir — so path must NOT be resolved, or the bundle
  backtick render line and the _source_paths/_staleness_paths set (→ cascade
  .lease.json freshness) shift under symlinked/relative roots (#427 PR1
  byte-identity).

  Do NOT store path.name (filename only) — that LOSES the month-subdir
  component (e.g. '2026-06-12.md' instead of the full path) and breaks:
    (1) the bundle render's backtick path line, which echoes the full path;
    (2) callers that do entry.path.read_text() to re-read the file.

  The dream.py:294 pattern of storing path.name was a latent subdir-loss bug
  that this backend fixes. The fix: JournalEntry.path carries the FULL
  un-resolved path; each call site uses entry.path.name where it needs the
  filename string (e.g. PromotedNote.from_journal_entries).

  export() handles relativization to agent_root; the on-dataclass value
  stays the caller's un-resolved shape.

See docs/spec/43-journal-backend.md for the full normative contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .._export_base import ExportableResult


# ──────────────────────────────────────────────────────────────────
# Frozen value objects (INVERSE of goal/outcome mutable pattern)


@dataclass(frozen=True)
class JournalEntry:
    """One dated journal entry returned by JournalBackend.

    FROZEN — journal entries are append-only immutable episodes. The
    state-machine justification that earned goal/outcome their MUTABLE
    exception is absent here; this matches logs/types.py's frozen LogEntry.

    Fields:
        date: the calendar date of this entry (parsed from the filename
            stem, e.g. 2026-06-12 for '2026-06-12.md'). Files whose stem
            cannot be parsed as an ISO date are INCLUDED by query_by_date
            (include-on-unparse fallback, spec/43 MUST 10) and carry
            date.today() as a best-effort placeholder. Callers MUST NOT
            rely on date being accurate for entries with unparseable stems.
        path: the on-disk path to the entry file, carried in the CALLER'S
            UN-resolved shape (e.g. /agents/caldwell/journal/2026-06/2026-06-12.md
            for an absolute agent_root, but a relative or symlinked path if
            agent_root was relative/symlinked).

            NOTE: path is NOT resolved/absolutized. The FilesystemJournalBackend
            deliberately emits the un-resolved path so the bundle backtick render
            line and the _source_paths/_staleness_paths set (→ cascade .lease.json
            freshness) stay byte-identical with the legacy rglob callers under
            symlinked/relative roots (#427 PR1 byte-identity). Do NOT store
            path.name (filename only) — that loses the month-subdir component and
            breaks callers that do entry.path.read_text(). export() handles
            relativization; the on-dataclass value stays the caller's shape.
        text: full UTF-8 text of the entry (as written at the time the
            entry was read; append_entry populates this with the new text).
        title: RESERVED for a future backend that wants to surface a parsed
            title (e.g. the first H1 heading). FilesystemJournalBackend does
            NOT populate this today — it is always None on every read and
            write path. Kept on the dataclass so a future backend can set it
            without a breaking field addition; callers MUST NOT rely on it
            being non-None. (spec/43 documents this as reserved.)
    """

    date: date
    path: Path
    text: str
    title: str | None = None


@dataclass(frozen=True)
class JournalCapabilities:
    """Per-backend capability declaration for JournalBackend (spec/43).

    Matches the frozen-dataclass convention of every other *Capabilities type.
    All capability booleans have defaults=False so new fields can be added at
    the end without breaking existing instantiation sites.

    Fields:
        backend_id: stable backend identifier string (required, no default).
        supports_canonical_export: True when the backend implements the
            spec/40 Exportable Protocol. FilesystemJournalBackend=True.
            Default False so existing instantiation sites without this kwarg
            keep working (backward-compatibility pattern from LogCapabilities).
        supports_date_query: True when query_by_date() is implemented and
            returns entries filtered by ISO date window. FilesystemJournalBackend=True.

    NOTE: WritePolicy is NOT part of the JournalBackend Protocol. The journal
    path is fixed at construction (journal/ under agent_root) and does not
    require per-call policy enforcement. Mirrors GoalBackend and LogBackend,
    not MemoryBackend. The conformance suite MUST NOT include a WritePolicy
    test for JournalBackend.

    Field ordering: backend_id (required, no default) first so positional
    construction JournalCapabilities("filesystem") is meaningful; capability
    booleans with defaults last so adding a new field at the end does not
    break existing instantiation sites.
    """

    backend_id: str
    supports_canonical_export: bool = False
    supports_date_query: bool = False


# ──────────────────────────────────────────────────────────────────
# Export result type


@dataclass
class JournalExport(ExportableResult):
    """Canonical export from a JournalBackend (spec/40 §"Per-backend export contracts").

    Embeds entry bytes (read_bytes() passthrough per *.md file) and portable
    relative-to-agent_root path metadata. Matches the GoalExport shape: journal
    entries ARE the agent's narrative content (embed bytes for byte-exact
    round-trip fidelity), not external artifacts that get referenced.

    Registering journal in the spec/40 export harness means a vault operator can
    capture the full journal for migration, compliance backup, or multi-host move.

    Fields:
        entries_with_bytes: list of (relative_path_str, raw_bytes) tuples.
            relative_path_str is the path relative to agent_root (e.g.
            'journal/2026-06/2026-06-12.md'). raw_bytes is the file's exact
            content (read_bytes() passthrough; CRLF/BOM NOT normalized — the
            journal is human-authored markdown, not structured data, so
            normalizing bytes would silently alter operator-authored content).
            Ordered by JournalBackend.list_entries(newest_first=True) order.
        backend_id: stable backend identifier.
        scope: agent root path as a string.

    Snapshot consistency: export() enumerates entries first (list_entries),
    then reads each file. A concurrent append_entry() completing between
    enumeration and the read of a specific entry may cause the exported bytes
    to reflect the post-append state for that day file. Callers requiring
    strict point-in-time consistency MUST hold the agent LockBackend before
    calling export(). This is the acknowledged spec/40 MUST 7 snapshot-
    consistency bound (same language as GoalExport's documented bound).
    """

    entries_with_bytes: list[tuple[str, bytes]]  # list of (relative_path_str, bytes)
    backend_id: str
    scope: str  # agent root path as a string

"""JournalBackend Protocol — the contract every journal implementation satisfies.

This is one of the open protocols in the protocol-pattern series (spec/43).
It decouples the journal storage layer from the three call sites
(bundle.py, agent.py, dream.py) that previously contained hand-synced
duplicate rglob blocks for reading journal entries.

Protocol method surface — STORAGE-ONLY (C-lite, per arc ruling):

  append_entry(text, when=today)           — atomic append of a new dated entry.
                                             Owns filename/dir minting
                                             (YYYY-MM-DD.md in YYYY-MM/ subdir).
                                             Does NOT accept caller-supplied ids.
  query_by_date(start, end)                — date-windowed read for dreams
  list_entries(limit=None, newest_first)   — newest-N read for bundle/agent
  capabilities()                           — return JournalCapabilities
  export(query=None) / export_all()        — spec/40 canonical export

The backend owns the DATA layer (find/read/sort/date-window → raw JournalEntry).
Formatting STAYS at each call site:
  - bundle: f'# Journal — {entry.path.stem}\\n`{entry.path}`\\n\\n{entry.text}'
    (path line INCLUDED)
  - agent:  f'# Journal — {entry.path.stem}\\n\\n{entry.text}'
    (no path line)

Do NOT absorb rendering into the Protocol. The divergence between bundle and
agent formats is LOAD-BEARING (byte-identity golden tests freeze both).

append_entry atomicity contract (MUST 9, on the Protocol):
  Under concurrent same-day appends, NO torn or interleaved write; append
  ORDERING is preserved. A crash mid-append may lose the in-flight entry but
  never corrupts committed entries. This guarantee lives ON the Protocol
  (state-what-must-be-true-for-the-caller); the lock mechanism is a
  filesystem-impl detail.

date-query correctness (MUST 10, on the Protocol):
  query_by_date(start, end) returns EXACTLY the entries in [start, end] by
  entry date, INCLUDING entries whose filename stem cannot be parsed as an ISO
  date (include-on-unparse fallback, matching legacy dream._read_journal_entries).

See docs/spec/43-journal-backend.md for the full normative contract.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, runtime_checkable

from .types import JournalCapabilities, JournalEntry, JournalExport


@runtime_checkable
class JournalBackend(Protocol):
    """Contract every journal backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The @runtime_checkable decorator enables
    isinstance(obj, JournalBackend) to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope: bound at construction. FilesystemJournalBackend(agent_root) operates
    on <agent_root>/journal/ (month-bucketed: journal/YYYY-MM/YYYY-MM-DD.md).

    The backend is STATELESS at the Protocol level — it holds the agent_root
    path only. All in-memory state is managed by the caller above the Protocol.

    STORAGE-ONLY boundary (C-lite): the backend owns the data layer
    (find/read/sort/date-window → raw JournalEntry). Formatting stays at
    each call site. Do NOT absorb rendering into the Protocol.

    append_entry() is the serialized atomic write primitive. The atomicity
    guarantee is on the Protocol (see module docstring MUST 9). The filesystem
    impl uses fcntl.flock; a Postgres impl uses a SQL transaction.

    list_entries() / query_by_date() are read-only. list_entries() mirrors the
    three legacy rglob callers: sort by full Path descending (newest first;
    equivalent to sorted(rglob('*.md'), reverse=True)).
    query_by_date() mirrors dream._read_journal_entries's date-window filter
    with include-on-unparse fallback.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'filesystem', 'postgres'.

        Used by the registry for lookup and by diagnostic tooling. Treat as
        a backwards-compatibility surface — operator deployments may pin
        against these strings.
        """
        ...

    def append_entry(self, text: str, when: date | None = None) -> JournalEntry:
        """Atomic append of a new dated entry to the journal.

        Owns filename/dir minting: creates <agent_root>/journal/YYYY-MM/YYYY-MM-DD.md
        for the 'when' date (default: today). Does NOT accept caller-supplied ids
        or filenames — caller-supplied names would let an operator mint a path
        the read layer's date.fromisoformat(stem) parse cannot honor.

        Atomicity contract (spec/43 MUST 9): under concurrent same-day appends,
        NO torn or interleaved write; append ordering is preserved. A crash
        mid-append may lose the in-flight entry but never corrupts committed
        entries or interleaves two writers.

        The filesystem implementation serializes the read-modify-append sequence
        (read current file content → append new entry → atomic_write) under
        an exclusive fcntl.flock on a .journal.lock sidecar. The flock is held
        across the ENTIRE read-modify-append — read under lock, modify, write
        under lock. Splitting 'read outside lock, acquire lock, write' introduces
        a lost-update race.

        Args:
            text: the journal entry body text (markdown, free-form).
            when: the entry date (default today). Used to mint the filename
                and month-subdir. Filesystem impl applies safe_resolve_under
                on the minted path to refuse adversarially-crafted dates.

        Returns:
            JournalEntry with the (caller-shaped, un-resolved) path, date, and
            text of the newly appended entry. If appending to an existing day
            file, the returned text is the FULL file content after the append
            (old + new).

        Raises:
            OSError: when the journal directory cannot be created or the new
                content cannot be written.
            JournalCorrupted: when an EXISTING day file is unreadable/corrupt
                during the read-modify-append (fail-loud — a lost narrative
                episode must not be silent). Subclasses AtomicAgentsError.
            PathTraversalError: when the minted path escapes agent_root/journal/
                (belt-and-suspenders guard for adversarially-crafted date values
                and symlinked journal/ ancestors).
        """
        ...

    def query_by_date(self, start: date, end: date) -> list[JournalEntry]:
        """Return entries whose date falls in [start, end] (inclusive).

        date-query correctness (spec/43 MUST 10): returns EXACTLY the entries
        in [start, end] by entry date. The start and end bounds are inclusive
        on both sides (start <= entry_date <= end).

        NOTE on the legacy dream consumer: dream._read_journal_entries applied
        ONLY a lower bound (no upper bound), so it INCLUDED future-dated entries.
        The adopted dream call sites therefore pass end=date.max to preserve that
        behavior byte-for-byte. Callers that genuinely want an upper bound pass a
        finite end; query_by_date enforces whatever bound it is given.

        Include-on-unparse fallback (spec/43 MUST 10): files whose stem cannot
        be parsed as an ISO date are INCLUDED regardless of start/end bounds.
        This matches legacy dream._read_journal_entries behavior:
            except (ValueError, TypeError): pass  # include if can't parse date

        The fallback must be purely structural — no additional condition (no
        mtime check, no clock re-evaluation). Matching dream.py's bare 'pass'
        pattern exactly.

        Files are returned in full-Path-descending order (newest first), matching
        sorted(journal_dir.rglob('*.md'), reverse=True) lexicographically.

        The start and end bounds are evaluated once at call entry (passed in,
        not re-evaluated inside a loop) so behavior is stable across midnight
        during a long iteration.

        Args:
            start: earliest date to include (inclusive).
            end: latest date to include (inclusive).

        Returns:
            List of JournalEntry objects in full-Path-descending order.
            Empty list when journal/ is absent or no entries match.
            Entries with unparseable stems are INCLUDED (include-on-unparse).

        Note:
            'Files whose stem cannot be parsed as a date are INCLUDED
            (include-on-unparse fallback, MUST 10). This matches legacy
            dream.py behavior.'
        """
        ...

    def list_entries(
        self,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[JournalEntry]:
        """Return journal entries, optionally limited to the top N.

        Subsumes the legacy rglob read sites (now live callers):
          - bundle._render_journal_breakpoint: list_entries(limit=RECENT_JOURNAL_DEFAULT)
          - bundle._source_paths/_staleness_paths: [e.path for e in list_entries(limit=N)]
          - agent._load_recent_journal: list_entries(limit=RECENT_JOURNAL_DEFAULT)

        Sort order: sort by full Path descending (not by JournalEntry.date).
        Path-based sort preserves the lexicographic ordering of filenames like
        '2026-06/2026-06-12.md' > '2026-05/2026-05-31.md' that the three legacy
        rglob callers relied on. Sorting on date alone would swap positions at
        month boundaries — silently breaking bundle's staleness hash.

        Degrade-but-KEEP-the-slot on unreadable: entries whose file cannot be read
        are NEVER dropped. The slot is KEPT (byte-for-byte bundle._safe_read_text:
        substitute an errors='replace' body + warning comment on UnicodeDecodeError,
        or a warning-only comment on OSError). This is an intentional improvement
        over agent's plain p.read_text() which would raise on corrupt entries.
        Dropping a slot would backfill an older entry and shift the newest-N
        selection AND the _source_paths/_staleness_paths set that drives the cascade
        .lease.json freshness hash. Note this DIFFERS from query_by_date(), which
        SKIPS unreadable entries (it has no fixed-N selection slot to preserve).
        Symlinked .md entries are NOT skipped (read through, as the legacy callers
        did); only a symlinked journal/ DIRECTORY escaping agent_root is refused.

        Args:
            limit: maximum number of entries to return (None = unbounded).
            newest_first: when True (default), return entries in filename-
                descending order (newest first). False = oldest first.

        Returns:
            List of JournalEntry objects. Empty list when journal/ is absent.
        """
        ...

    def export(self, query: Any = None) -> JournalExport:
        """Export all journal entries as a canonical JournalExport (spec/40 Exportable).

        Enumerates via list_entries(newest_first=True) (not semantic query).
        Best-effort point-in-time snapshot; does not acquire the agent LockBackend
        across the full read pass (spec/40 MUST 7 snapshot-consistency bound).

        Snapshot consistency: export() enumerates entries first, then reads each
        file. A concurrent append_entry() completing between enumeration and the
        read of a specific entry may cause the exported bytes to reflect the
        post-append state for that day file. Callers requiring strict consistency
        MUST hold the agent LockBackend before calling export().

        Returns JournalExport with:
          - entries_with_bytes: list of (relative_path_str, raw_bytes) tuples,
            ordered newest-first. relative_path_str is relative to agent_root
            (e.g. 'journal/2026-06/2026-06-12.md').
          - backend_id: this backend's id.
          - scope: agent_root as a string.

        UTF-8 entries, LF line endings (as stored on disk — no normalization).
        Empty entries_with_bytes when journal/ is absent (common for new agents).

        Args:
            query: unused (reserved for future bounded-export filtering).
        """
        ...

    def export_all(self) -> JournalExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        ...

    def capabilities(self) -> JournalCapabilities:
        """Backend capability declaration — see JournalCapabilities.

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.
        """
        ...

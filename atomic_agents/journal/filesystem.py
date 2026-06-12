"""FilesystemJournalBackend — directory-tree reference implementation (spec/43).

This is the default backend for single-host deployments. It wraps the same
on-disk shape the three legacy rglob callers (bundle.py, agent.py, dream.py)
have used since the framework's first journal support:

    <agent_root>/journal/YYYY-MM/YYYY-MM-DD.md   — dated entries, month-bucketed

Construction is side-effect-free (no filesystem I/O in __init__).

Ordering contract for list_entries():
    Sorts by full Path descending (NOT by JournalEntry.date). This preserves the
    lexicographic ordering of 'journal/2026-06/2026-06-12.md' >
    'journal/2026-05/2026-05-31.md' that the three legacy rglob callers relied
    on. Sorting on date alone would swap positions at month boundaries, silently
    breaking bundle's staleness hash.

Atomicity contract for append_entry() (spec/43 MUST 9):
    Exclusive fcntl.flock on <agent_root>/.journal.lock serializes ALL
    journal writes for this agent. The lock is placed at agent_root level
    (NOT inside a month subdir) so concurrent writes to ANY day file are
    serialized under one lock. The flock is held across the ENTIRE
    read-modify-append: read current file → append new entry → atomic_write.
    Splitting 'read outside lock, write inside lock' introduces a lost-update
    race where both callers read the same stale file and the second write
    silently clobbers the first.

    The .journal.lock sidecar is created once and reused. It persists on disk
    after the first write (the lock file is the flock anchor, not a temp file).
    rglob('*.md') will NOT match it. Doctor checks MUST NOT treat its presence
    as corruption.

Include-on-unparse fallback (spec/43 MUST 10):
    Files whose stem cannot be parsed as an ISO date (ValueError, TypeError)
    are INCLUDED by query_by_date() regardless of start/end bounds. This
    matches legacy dream._read_journal_entries exactly.

Symlink containment (spec/43 security contract):
    _journal_dir() resolves both agent_root and agent_root/'journal', then
    checks is_relative_to before trusting journal/ as the containment root.
    A symlinked journal/ that points outside agent_root raises PathTraversalError
    on append (fail-loud) and returns [] on list/query (fail-soft, matching the
    'absent journal' behavior for read-only operations). This matches the
    OutcomeBackend._runs_root() pattern introduced in #426 and confirmed as
    load-bearing by MEMORY.md feedback_cross_model_catches_same_family_blind_spots.

    Individual symlinked .md entry files (symlink-to-file INSIDE journal/) are
    NOT filtered — they are selected and read through exactly as the three legacy
    rglob callers did (bundle/agent _safe_read_text and dream read_text both
    FOLLOW symlinks; bundle._source_paths' is_file() filter also follows them).
    The directory-level containment guard above is the real escape vector; an
    individual symlinked day-file that resolves under agent_root is harmless and
    was always included. Filtering it per-entry would drop the slot, backfill an
    older entry, and shift both the newest-N selection AND the
    _source_paths/_staleness_paths set that drives cascade .lease.json freshness —
    the exact byte-identity regression the #427 PR1 ADOPT-NOW ruling forbids.

Month-subdir minting:
    append_entry() mints the path as:
        <agent_root>/journal/YYYY-MM/YYYY-MM-DD.md
    The month subdir is created by atomic_write's target.parent.mkdir(parents=True,
    exist_ok=True) — no extra logic needed.

    A safe_resolve_under guard on the minted path is applied BEFORE any I/O as
    belt-and-suspenders for adversarially-crafted date values (date.isoformat()
    is safe but the guard is explicit per the outcome backend precedent).

Crash recovery:
    atomic_write() creates a .{filename}.*.tmp sibling during the write. A crash
    leaves a .tmp file that is NOT matched by rglob('*.md'). The .journal.lock
    sidecar persists but is also not matched by rglob('*.md'). list_entries()
    and query_by_date() use rglob('*.md') to silently exclude both.

Import boundary (circular-import safety):
    - Imports only from ..exceptions, .._io, .types — no imports from
      ..journal (the package root) or any module that imports ..journal at
      module level. This keeps journal/__init__.py importable without loading
      the LLM stack.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .._io import atomic_write, safe_resolve_under
from ..exceptions import JournalCorrupted, PathTraversalError
from .types import JournalCapabilities, JournalEntry, JournalExport


class FilesystemJournalBackend:
    """Filesystem reference impl for JournalBackend Protocol (spec/43).

    Scoped to one agent root — <agent_root>/journal/YYYY-MM/YYYY-MM-DD.md.
    Constructed once per agent; construction is side-effect-free (no filesystem
    I/O in __init__).

    list_entries() sorts by full Path descending (NOT by JournalEntry.date).
    This preserves the lexicographic ordering that the three legacy rglob callers
    in bundle.py, agent.py, and dream.py relied on. Sorting on date alone would
    swap positions at month boundaries, silently breaking bundle's staleness hash
    (the cascade .lease.json freshness that _source_paths/_staleness_paths drive).

    append_entry() is serialized under an exclusive fcntl.flock on a single
    .journal.lock sidecar at agent_root level. The lock scope is the WHOLE agent's
    journal (not per-month-dir) so all day files are serialized under one lock.

    Args:
        agent_root: the agent's root directory. Kept in its UN-resolved shape
            for path EMISSION (entry.path mirrors the caller's relative/symlinked
            shape for byte-identity with the legacy rglob callers); a separate
            resolved copy is held ONLY for symlink-containment checks. Paths
            containing a literal '..' component are rejected with ValueError.
    """

    @property
    def backend_id(self) -> str:
        """Stable backend identifier."""
        return "filesystem"

    def __init__(self, agent_root: Path) -> None:
        """Construct a FilesystemJournalBackend for agent_root.

        Side-effect-free: no filesystem I/O during construction. The agent
        directory need not exist at construction time — list_entries() returns
        [] when journal/ is absent.

        Args:
            agent_root: the agent's root directory. The raw value is kept
                UN-resolved for path emission (entry.path mirrors the caller's
                relative/symlinked shape — #427 PR1 byte-identity); a separate
                resolved copy is held only for symlink-containment checks. Paths
                containing a literal '..' component are rejected with ValueError.

        Raises:
            ValueError: when the raw value contains '..' path components.
        """
        raw = Path(agent_root)
        for part in raw.parts:
            if part == "..":
                raise ValueError(
                    f"FilesystemJournalBackend: agent_root contains '..' component: "
                    f"{agent_root!r}"
                )
        # Byte-identity contract (#427 PR1, ADOPT-NOW): the three legacy callers
        # (bundle.py, agent.py, dream.py) all operated on UN-resolved paths
        # (instance_root / "journal").rglob(...). entry.path MUST therefore carry
        # the caller's un-resolved path shape, or the bundle backtick render line
        # and the _source_paths/_staleness_paths set (→ cascade .lease.json
        # freshness) shift under symlinked or relative agent roots. We keep the
        # raw value for path EMISSION and a separate resolved copy ONLY for
        # containment checks (symlink-escape refusal). This mirrors the legacy
        # behavior exactly while preserving the symlink containment guard.
        self._agent_root = raw
        # Resolution is DEFERRED to first use (_journal_dir), not done here.
        # Construction is side-effect-free AND must not crash on an unresolvable
        # agent_root (symlink-loop or permission-denied ancestor): journal is now
        # live-wired into AtomicAgent.__init__ (#427 PR1 ADOPT-NOW), so a resolve()
        # crash here would take down agent construction entirely — whereas the
        # legacy journal code never resolved agent_root at all. _journal_dir()
        # performs the resolve under a guard that maps the failure to
        # PathTraversalError (→ [] for reads, fail-loud for append).
        self._lock_path = self._agent_root / ".journal.lock"

    @property
    def _agent_root_resolved(self) -> Path:
        """Resolved agent_root for symlink-containment checks (deferred).

        Computed lazily so construction stays side-effect-free and cannot crash
        on an unresolvable agent_root. May raise OSError/RuntimeError; the sole
        caller (_journal_dir) wraps it into PathTraversalError.
        """
        return self._agent_root.resolve()

    # ──────────────────────────────────────────────────────────────
    # Symlink containment guard

    def _journal_dir(self) -> Path:
        """Return the UN-resolved journal/ dir after a resolved containment check.

        Mirrors FilesystemOutcomeBackend._runs_root() for the SECURITY check
        (resolve both agent_root and agent_root/'journal', verify is_relative_to),
        but RETURNS the un-resolved (agent_root / 'journal') path for rglob,
        append_entry minting, and export.

        Byte-identity contract (#427 PR1): the three legacy callers rglob'd an
        un-resolved (instance_root / 'journal') dir, so entry.path carried the
        un-resolved shape. Returning the resolved path here would collapse
        symlinks and absolutize relative roots, shifting the bundle backtick
        render line and the _source_paths/_staleness_paths set that drives the
        cascade .lease.json freshness hash. The containment check still runs on
        the resolved copy, so a symlinked journal/ pointing outside agent_root is
        still refused (HIGH-severity symlink containment, load-bearing per
        MEMORY.md feedback_cross_model_catches_same_family_blind_spots).

        Resolution-failure is folded into PathTraversalError. .resolve() raises
        RuntimeError (symlink loop / ELOOP) or OSError (permission-denied or
        otherwise inaccessible ancestor) — NOT PathTraversalError. The legacy
        rglob callers this PR deleted used journal_dir.exists()/is_dir()/rglob(),
        all of which return False/[] on a symlink loop WITHOUT raising, so the
        agent ran with an empty journal. The read sites here map PathTraversalError
        to [] (fail-soft) and append_entry re-raises it (fail-loud), so folding
        the resolution failure into PathTraversalError restores the legacy
        graceful-empty behavior for reads and keeps append fail-loud — instead of
        a hard RuntimeError/OSError crash on the now-live system-prompt path
        (BP[14]), cascade .lease.json freshness, and dream consolidation.

        Returns:
            The UN-resolved (agent_root / 'journal') directory path.

        Raises:
            PathTraversalError: when journal/ resolves outside agent_root
                (symlinked ancestor escape), OR when either path cannot be
                resolved at all (symlink loop / inaccessible ancestor).
        """
        try:
            agent_root_resolved = self._agent_root_resolved
            journal_resolved = (self._agent_root / "journal").resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "journal/ path could not be resolved "
                "(symlink loop or inaccessible ancestor)",
                child="journal",
                root=str(self._agent_root),
            ) from exc
        if not journal_resolved.is_relative_to(agent_root_resolved):
            raise PathTraversalError(
                "journal/ resolves outside the agent vault "
                "(symlinked ancestor refused)",
                child="journal",
                root=str(agent_root_resolved),
            )
        return self._agent_root / "journal"

    # ──────────────────────────────────────────────────────────────
    # Internal: exclusive file lock

    @contextmanager
    def _journal_lock(self) -> Iterator[None]:
        """Acquire exclusive advisory lock on .journal.lock for the duration.

        This is the filesystem serialization primitive — equivalent to a SQL
        transaction in a Postgres backend. The flock is on a single sidecar at
        agent_root level (NOT per month dir) so all day files are serialized
        under one lock.

        The lock is held across the ENTIRE read-modify-append in append_entry:
        read current file content → append new entry → atomic_write. ALL three
        steps happen inside this context. Splitting 'read outside lock, write
        inside lock' introduces a lost-update race.

        The lock file is created if absent. The lock is released in finally.
        Two finally blocks (inner flock(LOCK_UN), outer os.close(fd)) mirror
        goal/filesystem.py's _goal_lock() pattern exactly for idempotent teardown.
        """
        self._agent_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ──────────────────────────────────────────────────────────────
    # Internal: entry reading helper

    @staticmethod
    def _try_read_entry(path: Path) -> str | None:
        """Read entry text; return None on OSError/UnicodeDecodeError (skip-unreadable).

        query_by_date() uses this to silently skip corrupt or unreadable files.
        For OSError this matches legacy dream._read_journal_entries (its read was a
        plain read_text under 'except OSError: continue'). For UnicodeDecodeError
        this is a DELIBERATE divergence: legacy dream caught ONLY OSError, and
        UnicodeDecodeError (a ValueError subclass, NOT an OSError) would PROPAGATE
        and crash the consolidation pipeline. Catching it here skips the non-UTF-8
        entry instead — best-effort, never fatal — matching list_entries' degrade
        philosophy. append_entry() does NOT use this — it fails loud on write errors.
        """
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    @staticmethod
    def _safe_read_entry(path: Path) -> str:
        """Read entry text, degrading gracefully but KEEPING the slot.

        Byte-for-byte equivalent of bundle._safe_read_text (#427 PR1 byte-identity):
          - success: file body verbatim
          - UnicodeDecodeError: re-read with errors='replace' + a warning comment
          - OSError: just the warning comment (no body)

        list_entries() uses this (NOT _try_read_entry) so unreadable journal
        files keep their slot in the newest-N selection. Dropping the slot would
        backfill an older entry, shifting BOTH the selected entry set AND the
        _source_paths/_staleness_paths set that drives the cascade .lease.json
        freshness hash — the exact regression the byte-identity ruling forbids.
        Mirrors the legacy bundle._load_recent_journal path, which read every
        selected slot through _safe_read_text.
        """
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # The re-read can ALSO raise OSError if the file is deleted or its
            # permissions change between the first (failing) read and this one
            # (TOCTOU). Guard it so the slot still degrades-but-keeps instead of
            # propagating an OSError out of the now-hot agent.call() journal path.
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return f"<!-- WARNING: {path.name} unreadable ({type(exc).__name__}). -->\n"
            return (
                f"<!-- WARNING: {path.name} contained non-UTF-8 bytes; replaced. -->\n"
                f"{body}"
            )
        except OSError as exc:
            # Name the failure mode (type only). Do NOT embed str(exc): OSError's
            # str carries the absolute filesystem path, which would flow into the
            # agent system prompt and out to an external LLM provider.
            return f"<!-- WARNING: {path.name} unreadable ({type(exc).__name__}). -->\n"

    @staticmethod
    def _parse_entry_date(path: Path) -> date | None:
        """Parse the calendar date from a journal file's stem (YYYY-MM-DD).

        Returns the date when the stem is a valid ISO date string.
        Returns None when date.fromisoformat(stem) raises ValueError or TypeError.
        """
        try:
            return date.fromisoformat(path.stem)
        except (ValueError, TypeError):
            return None

    # ──────────────────────────────────────────────────────────────
    # Protocol methods

    def append_entry(self, text: str, when: date | None = None) -> JournalEntry:
        """Atomic append of a new dated entry to the journal.

        Mints the path as <agent_root>/journal/YYYY-MM/YYYY-MM-DD.md.
        If the day file already exists, appends the new text (separated by two
        newlines) to the existing content. The month subdir is created by
        atomic_write's target.parent.mkdir(parents=True, exist_ok=True).

        The ENTIRE read-modify-append sequence runs under an exclusive flock on
        .journal.lock (read under lock → modify → write under lock). This
        prevents the lost-update race where two concurrent callers read the same
        stale file and the second write silently clobbers the first.

        Returns JournalEntry with the FULL file content after the append (old
        content + separator + new text for an existing day file).
        """
        entry_date = when or date.today()

        # Belt-and-suspenders containment check before any I/O.
        try:
            journal_dir = self._journal_dir()
        except PathTraversalError:
            raise  # fail-loud on write (symlinked ancestor escape is a hard error)

        # Mint the month-bucketed path.
        month_str = entry_date.strftime("%Y-%m")
        day_str = entry_date.strftime("%Y-%m-%d")
        day_file = journal_dir / month_str / f"{day_str}.md"

        # safe_resolve_under: belt-and-suspenders for minted paths.
        # date.isoformat() is well-constrained, but OutcomeBackend applied the
        # same guard and MEMORY.md records a cross-family finding that justified it.
        agent_root_resolved = self._agent_root_resolved
        safe_resolve_under(str(day_file.relative_to(journal_dir)), journal_dir)
        if not day_file.resolve().is_relative_to(agent_root_resolved):
            raise PathTraversalError(
                f"Minted journal path {day_file!r} escapes agent_root",
                child=str(day_file),
                root=str(agent_root_resolved),
            )

        # Serialize the entire read-modify-append under the lock.
        with self._journal_lock():
            if day_file.exists():
                try:
                    existing = day_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as e:
                    # The existing day file is unreadable/corrupt. append_entry
                    # FAILS LOUD (Principle #5 — a lost narrative episode must not
                    # be silent). JournalCorrupted is the contract's named
                    # exception (spec/43); it subclasses AtomicAgentsError so
                    # callers catching the base class still see it.
                    raise JournalCorrupted(
                        f"Failed to read existing journal entry {day_file}: {e}"
                    ) from e
                new_content = existing.rstrip("\n") + "\n\n" + text
            else:
                new_content = text

            atomic_write(day_file, new_content)

        return JournalEntry(
            date=entry_date,
            # UN-resolved path for byte-identity with list_entries/query_by_date,
            # which emit the un-resolved rglob path (#427 PR1 byte-identity).
            path=day_file,
            text=new_content,
        )

    def query_by_date(self, start: date, end: date) -> list[JournalEntry]:
        """Return entries whose date falls in [start, end] (inclusive).

        Include-on-unparse fallback (spec/43 MUST 10): files whose stem cannot
        be parsed as an ISO date are INCLUDED regardless of start/end bounds.
        This matches legacy dream._read_journal_entries exactly.

        Sort order: filename-descending (newest first), matching
        sorted(journal_dir.rglob('*.md'), reverse=True).

        Files: only *.md (excludes .journal.lock, *.tmp sidecars).
        Skip-unreadable: silently skip files that cannot be read (OSError or
            non-UTF-8). For OSError this matches legacy dream, which read under
            'except OSError: continue'. For UnicodeDecodeError this is a
            DELIBERATE robustness improvement — legacy dream caught only OSError
            and would PROPAGATE a UnicodeDecodeError (crashing the consolidation
            pipeline); query_by_date skips it instead so a non-UTF-8 entry is
            best-effort-dropped, never fatal.
        Symlinked .md entries: NOT skipped — read through as legacy dream did
            (read_text follows symlinks). The journal/-DIRECTORY containment guard
            in _journal_dir() is the escape vector; an individual symlinked
            day-file that resolves under agent_root was always included.
        """
        try:
            journal_dir = self._journal_dir()
        except PathTraversalError:
            return []

        if not journal_dir.is_dir():
            return []

        entries: list[JournalEntry] = []
        for path in sorted(journal_dir.rglob("*.md"), reverse=True):
            entry_date = self._parse_entry_date(path)
            # Include-on-unparse fallback (MUST 10): include if we can't parse the date.
            if entry_date is not None:
                if entry_date < start or entry_date > end:
                    continue

            text = self._try_read_entry(path)
            if text is None:
                continue

            # Use date.today() as fallback for unparseable stems (best-effort; caller
            # MUST NOT rely on date accuracy for unparseable stems per types.py doc).
            entries.append(
                JournalEntry(
                    date=entry_date if entry_date is not None else date.today(),
                    path=path,
                    text=text,
                )
            )

        return entries

    def list_entries(
        self,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[JournalEntry]:
        """Return journal entries, optionally limited to the top N.

        Sort order: sort by full Path (NOT by JournalEntry.date). Path-based
        sort preserves the lexicographic ordering of filenames like
        '2026-06/2026-06-12.md' > '2026-05/2026-05-31.md' that the three
        legacy rglob callers relied on. Sorting on date alone would swap
        positions at month boundaries, silently breaking bundle's staleness hash.

        Degrade-but-keep-slot on unreadable: entries whose file cannot be read
        are KEPT (via _safe_read_entry, which substitutes an errors='replace'
        body + warning comment, or a warning-only comment on OSError). The slot
        is NOT dropped — dropping it would backfill an older entry and shift the
        newest-N selection AND the _source_paths/_staleness_paths set that drives
        the cascade .lease.json freshness hash. This matches the legacy bundle
        path (_load_recent_journal read every selected slot through _safe_read_text).
        Symlinked .md entries: NOT skipped — selected and read through exactly as
        legacy bundle/agent did (is_file()/_safe_read_text/read_text all follow
        symlinks). The journal/-DIRECTORY containment guard is the escape vector;
        a symlinked day-file resolving under agent_root was always selected, and
        dropping it here would shift the newest-N selection (the byte-identity
        regression the #427 PR1 ADOPT-NOW ruling forbids).

        Args:
            limit: maximum number of entries to return (None = unbounded).
            newest_first: when True (default), filename-descending (newest first).

        Returns:
            List of JournalEntry objects. Empty list when journal/ is absent.
        """
        try:
            journal_dir = self._journal_dir()
        except PathTraversalError:
            return []

        if not journal_dir.is_dir():
            return []

        # Sort by Path descending (newest first) matching sorted(rglob, reverse=True).
        all_paths = sorted(journal_dir.rglob("*.md"), reverse=newest_first)

        entries: list[JournalEntry] = []
        for path in all_paths:
            if limit is not None and len(entries) >= limit:
                break

            # Degrade-but-keep-slot: unreadable files stay in the selection with
            # a warning-substituted body, matching legacy bundle byte-for-byte.
            text = self._safe_read_entry(path)

            entry_date = self._parse_entry_date(path)
            entries.append(
                JournalEntry(
                    date=entry_date if entry_date is not None else date.today(),
                    path=path,
                    text=text,
                )
            )

        return entries

    def export(self, query: Any = None) -> JournalExport:
        """Export all journal entries as a canonical JournalExport (spec/40).

        Enumerates via list_entries(newest_first=True). Embeds entry bytes
        (read_bytes() passthrough per *.md file). Exports portable
        relative-to-agent_root path strings.

        Snapshot consistency: export() enumerates entries first, then reads each
        file for bytes. A concurrent append_entry() may cause bytes to reflect
        the post-append state for an already-enumerated day file. Callers
        requiring strict consistency MUST hold the agent LockBackend.

        Unreadable-entry divergence from list_entries (deliberate, best-effort):
        list_entries() KEEPS the slot for a dangling-symlink / unreadable entry,
        substituting a warning-comment body (so the newest-N selection and the
        cascade .lease.json staleness hash do not shift). export() instead SKIPS
        any entry whose bytes cannot be read (OSError on read_bytes()). This is
        correct-by-intent: an unreadable file has no canonical bytes to embed, so
        a byte-fidelity export cannot round-trip it. The exported entry set can
        therefore be a strict subset of the list_entries set for the same backend
        at the same instant. Round-trip fidelity (spec/40 MUST 7) is preserved
        because the on-disk bytes ARE what round-trips — a file that cannot be
        read has nothing to round-trip.
        """
        try:
            journal_dir = self._journal_dir()
        except PathTraversalError:
            return JournalExport(
                entries_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        if not journal_dir.is_dir():
            return JournalExport(
                entries_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        entries_with_bytes: list[tuple[str, bytes]] = []
        for entry in self.list_entries(newest_first=True):
            try:
                raw_bytes = entry.path.read_bytes()
            except OSError:
                continue  # skip unreadable entries in export (best-effort)

            # Relativize path against agent_root for portable export.
            try:
                rel_path = entry.path.relative_to(self._agent_root)
            except ValueError:
                # Fallback: use absolute path string when outside agent_root.
                rel_path_str = str(entry.path)
            else:
                rel_path_str = str(rel_path)

            entries_with_bytes.append((rel_path_str, raw_bytes))

        return JournalExport(
            entries_with_bytes=entries_with_bytes,
            backend_id=self.backend_id,
            scope=str(self._agent_root),
        )

    def export_all(self) -> JournalExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        return self.export(None)

    def capabilities(self) -> JournalCapabilities:
        """Return capabilities for this backend.

        FilesystemJournalBackend supports:
          - supports_canonical_export=True (spec/40 export implemented)
          - supports_date_query=True (query_by_date ships with a live consumer)
        """
        return JournalCapabilities(
            backend_id=self.backend_id,
            supports_canonical_export=True,
            supports_date_query=True,
        )

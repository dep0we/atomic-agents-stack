"""Filesystem identity export implementations for state backends (spec/40 PR1).

Provides the filesystem reference implementations of the Exportable Protocol
for Memory, Log, Mandate, Corpus, Lock, and Secret backends. These define the
canonical byte-shape for Tier A (byte-exact) round-trip fidelity.

This module is currently READ-ONLY (export is a read path — it extracts state
already on disk). When write-to-disk export is added (issue #430), all writes
MUST route through ``_io.atomic_write`` (temp + fsync + rename).

Snapshot consistency bound (spec/40 MUST 7):
export() produces a best-effort point-in-time snapshot. It does not acquire
the agent-level LockBackend across the full read pass, so concurrent writes
may produce an internally consistent export of each individual object but NOT
a cross-object consistent snapshot. Each individual object IS read atomically
(not mid-write), because the underlying read methods (read_note, read_page,
etc.) read complete files. Full cross-object consistency requires the caller
to hold the agent lock before calling export().
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._io import safe_resolve_under
from .types import (
    CorpusExport,
    CorpusExportQuery,
    LockExport,
    LockExportQuery,
    LogExport,
    LogExportQuery,
    MandateExport,
    MandateExportQuery,
    MemoryExport,
    MemoryExportQuery,
    SecretExport,
    SecretExportQuery,
    SecretExportRef,
)

if TYPE_CHECKING:
    from ..corpus.filesystem import FilesystemCorpusBackend
    from ..locks.filesystem import FilesystemLockBackend
    from ..logs.filesystem import FilesystemLogBackend
    from ..mandate.filesystem import FilesystemMandateBackend
    from ..memory.filesystem import FilesystemBackend


# ──────────────────────────────────────────────────────────────────────────────
# MemoryBackend export


def export_memory(
    backend: "FilesystemBackend",
    query: MemoryExportQuery | None = None,
) -> MemoryExport:
    """Export memory notes as a MemoryExport canonical object.

    Reads raw file bytes directly from disk (NOT through _path_to_note →
    Note → re-render). This is the mandatory approach for Tier A byte-exact
    fidelity: re-rendering through frontmatter.dumps() diverges on date
    formatting and extra_frontmatter key ordering (spec/40 prep finding P0).

    The returned MemoryExport contains (Note, raw_bytes) tuples. The Note
    objects are for metadata inspection; the raw_bytes are what the renderer
    uses for Tier A export.

    Read-consistency note: each note is read twice — once via
    ``note_path.read_bytes()`` (the authoritative export bytes) and once via
    ``backend.read_note()`` (the parsed Note for inspection). A concurrent
    writer landing between the two reads could pair bytes from one revision with
    a Note parsed from another. The ``raw_bytes`` are authoritative when they
    diverge; the Note is best-effort metadata. This narrow window is acceptable
    under the MUST 7 best-effort-snapshot bound — a caller needing a fully
    consistent snapshot holds the agent lock across export().

    Args:
        backend: a FilesystemBackend instance.
        query: optional export filter. Pass None for unbounded export.

    Returns:
        MemoryExport with all matching notes and their raw bytes.
    """
    import warnings

    from .renderer import render_note_bytes_from_raw

    if query is None:
        query = MemoryExportQuery()

    if query.include_versions:
        # The version-history scan is deferred to #433. A True flag here is a
        # no-op today; warn loudly rather than silently returning a
        # current-state-only export to a caller who asked for full history
        # (the field's own stated compliance-backup use case).
        warnings.warn(
            "MemoryExportQuery.include_versions=True is not yet implemented "
            "(deferred to issue #433); the export contains current-state notes "
            "only, NOT version history.",
            stacklevel=2,
        )

    notes_with_bytes: list[tuple[Any, bytes]] = []

    note_refs = backend.list_notes(
        include_archived=query.include_archived,
        include_superseded=query.include_superseded,
    )

    memory_dir = backend._memory_dir  # the <agent_root>/memory/ directory

    for ref in note_refs:
        note_path = memory_dir / ref.name
        if not note_path.exists():
            continue
        # Enforce path containment before reading — safe_resolve_under raises
        # PathTraversalError if note_path resolves outside memory_dir.
        # list_notes() yields bare filenames, so traversal is not expected here,
        # but an explicit guard matches the codebase-wide convention (spec/40
        # security hardening, FIX 4b).
        safe_resolve_under(note_path, memory_dir)
        # Read raw bytes directly — do NOT parse through _path_to_note and
        # re-render. See module docstring + spec/40 prep finding P0. Route the
        # raw bytes through the shared Tier A passthrough renderer so the
        # filesystem export-path routes through the designated renderer
        # (spec/40 §"Renderer module" insertion point for future read-path sharing).
        raw_bytes = render_note_bytes_from_raw(note_path.read_bytes())
        note = backend.read_note(ref.name)
        if note is not None:
            notes_with_bytes.append((note, raw_bytes))

    return MemoryExport(
        notes_with_bytes=notes_with_bytes,
        backend_id=backend.backend_id,
        # Use the authoritative agent root, not _memory_dir.parent (the latter
        # is wrong when memory_subdir contains a path separator).
        scope=str(backend._agent_root),
    )


# ──────────────────────────────────────────────────────────────────────────────
# LogBackend export


def export_log(
    backend: "FilesystemLogBackend",
    query: LogExportQuery | None = None,
) -> LogExport:
    """Export log records as a LogExport canonical object.

    Pairs each queried RunRecord with the EXACT bytes that exist on disk for
    that line — not bytes re-derived from the parsed record. This is true Tier A
    byte-exact fidelity: a legacy or hand-edited on-disk line (e.g. one with a
    different key order, a string-typed token count, or a field the current
    ``to_dict()`` would drop) exports verbatim, byte-for-byte (spec/40 MUST 4 +
    prep finding P1).

    Implementation: walk the same JSONL shard files the LogBackend reads, build
    a multimap keyed by each line's natural identity ``(run_id, ts)`` -> its raw
    on-disk bytes, then look each queried record up by its ``(run_id, ts)``. The
    natural-identity key survives the type coercion ``RunRecord.from_dict`` applies
    (e.g. a string-typed ``input_tokens`` on disk parses to an int on the record),
    so a legacy or hand-edited line still resolves to its verbatim disk bytes —
    a content hash of ``to_dict()`` would NOT, because the re-serialized record
    no longer matches the original line. A record whose ``(run_id, ts)`` is not
    found on disk (should not happen for filesystem-resident records) falls back
    to the shared renderer (``render_run_record_bytes``) so the export path always
    routes through the one shared renderer (spec/40 §"Renderer module").

    Args:
        backend: a FilesystemLogBackend instance.
        query: optional export filter. Pass None for all records.

    Returns:
        LogExport with all matching records and their exact on-disk raw bytes.
    """
    import json as _json

    from ..logs.filesystem import _MONTH_DIR_RE, _DAY_FILE_RE, _month_overlaps_window
    from ..logs.types import LogQuery
    from .renderer import render_run_record_bytes

    if query is None:
        query = LogExportQuery()

    log_query = query.log_query  # LogQuery | None

    records = (
        backend.query(log_query) if log_query is not None else backend.query(LogQuery())
    )

    # Derive date bounds for the shard walk from the QUERY's own date bounds,
    # NOT from the records' ts values.
    #
    # Why query-bounds, not records-derived bounds?
    # A blank-ts or hand-misfiled line can physically live in a shard whose
    # month is OUTSIDE the range of the ts-values in the matched records:
    #   - blank-ts lines land in today's shard (date.today() fallback in
    #     _record_date), but their RunRecord.ts is "" or None — excluded from
    #     the records-derived min/max ts window.
    #   - a hand-edited line may be filed in a shard whose name doesn't match
    #     its ts value.
    # If the shard is skipped, the line's exact bytes are never found in the
    # multimap and export_log falls back to render_run_record_bytes(record),
    # which reorders keys / pads defaults — breaking the "legacy/hand-edited
    # lines export verbatim byte-for-byte" guarantee (spec/40 MUST 4 + PR1
    # test_log_export_legacy_line_exported_verbatim).
    #
    # Contract:
    #   - UNBOUNDED query (log_query is None, or log_query has no since/until):
    #     walk ALL shards, no prefilter.  This is export_all; full verbatim
    #     fidelity for every line is required and reading everything costs no
    #     more than before.
    #   - BOUNDED query (log_query carries since and/or until):
    #     gate shards on the query's own since_date/until_date via
    #     _month_overlaps_window.  This preserves the perf win for bounded
    #     queries.  Verbatim fidelity caveat: a hand-misfiled line whose
    #     physical shard falls outside this window may be re-serialized rather
    #     than emitted verbatim — an anomaly documented in spec/40
    #     §"LogBackend export contract".
    if log_query is not None and (
        log_query.since is not None or log_query.until is not None
    ):
        since_date: date | None = log_query.since.date() if log_query.since else None
        until_date: date | None = log_query.until.date() if log_query.until else None
    else:
        # Unbounded (export_all) — walk every shard so every line, including
        # blank-ts and misfiled lines, is found verbatim in the multimap.
        since_date = None
        until_date = None

    # Build a multimap keyed by natural identity (run_id, ts) -> ordered list of
    # the exact raw byte payloads for lines with that identity (ordered as on
    # disk). Popping from the front of the per-key list keeps duplicate handling
    # stable and exact.
    #
    # Shard prefilter: skip any month dir (and day file) that falls entirely
    # outside the [since_date, until_date] window derived from the QUERY's own
    # date bounds (see comment block above for the derivation logic).
    # For unbounded queries both dates are None and every shard is visited (no
    # skipping) — this is what ensures blank-ts and misfiled lines are found
    # verbatim in export_all().  Mirrors the prefilter in
    # FilesystemLogBackend.query() (_month_overlaps_window + day-level check).
    raw_by_identity: dict[tuple[str, str], list[bytes]] = {}
    log_dir = backend._log_dir
    if log_dir.exists() and records:
        for month_dir in sorted(log_dir.iterdir()):
            if not month_dir.is_dir() or not _MONTH_DIR_RE.match(month_dir.name):
                continue
            if not _month_overlaps_window(month_dir.name, since_date, until_date):
                continue
            for day_file in sorted(month_dir.iterdir()):
                m = _DAY_FILE_RE.match(day_file.name)
                if not m or not day_file.is_file():
                    continue
                # Day-level prefilter — skip whole days outside window.
                try:
                    day = date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if since_date and day < since_date:
                    continue
                if until_date and day > until_date:
                    continue
                try:
                    raw_text = day_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                # Re-split on "\n" (NOT splitlines) so the exact terminator is
                # reconstructable; each appended record is one line + "\n".
                for raw_line in raw_text.split("\n"):
                    if not raw_line.strip():
                        continue
                    try:
                        parsed = _json.loads(raw_line)
                    except _json.JSONDecodeError:
                        continue
                    key = (str(parsed.get("run_id", "")), str(parsed.get("ts", "")))
                    raw_bytes = (raw_line + "\n").encode("utf-8")
                    raw_by_identity.setdefault(key, []).append(raw_bytes)

    records_with_bytes: list[tuple[Any, bytes]] = []
    for record in records:
        key = (record.run_id, record.ts)
        bucket = raw_by_identity.get(key)
        if bucket:
            raw_bytes = bucket.pop(0)
        else:
            # Not resident on disk under this identity (should not happen for a
            # filesystem-backed record). Route through the shared renderer so
            # the byte shape still matches what append() would write.
            raw_bytes = render_run_record_bytes(record)
        records_with_bytes.append((record, raw_bytes))

    return LogExport(
        records_with_bytes=records_with_bytes,
        backend_id=backend.backend_id,
        scope=str(backend.scope_root),
    )


# ──────────────────────────────────────────────────────────────────────────────
# MandateBackend export


def export_mandate(
    backend: "FilesystemMandateBackend",
    query: MandateExportQuery | None = None,
) -> MandateExport:
    """Export mandate definitions as a MandateExport canonical object.

    Exports MANDATE DEFINITIONS only — the Mandate objects from list_mandates().
    The .judge-state/mandates.json dedup sidecar is intentionally excluded
    (it is an implementation detail, not a portable agent artifact).

    Project-root ``## _meta`` policy blocks ARE captured. list_mandates() parses
    but discards the ``(_meta, mandates)`` tuple's first element, so the export
    re-parses each project-root scope's mandates.md to recover the
    ``ProjectMandateMeta`` (``per_agent_mandate_policy`` + ``allowed_per_agent_ids``).
    That block is a security boundary — dropping it would silently revert a
    ``forbidden`` policy to the ``open`` default on re-import (spec/40 Round-2
    finding). Per-agent scopes never carry a valid ``_meta`` block and are not
    re-parsed for it.

    Scope discovery: For None query (all scopes), the filesystem impl discovers
    known scopes by scanning for mandates.md files under scope_root. This avoids
    needing a list_scopes() Protocol method that doesn't exist.

    Args:
        backend: a FilesystemMandateBackend instance.
        query: optional export filter. Pass None for all known scopes.

    Returns:
        MandateExport with mandates_by_scope and meta_by_scope populated.
    """
    from ..mandates_md import parse_mandates_md as _parse_mandates_md

    if query is None:
        query = MandateExportQuery()

    scope_root = backend._scope_root

    # Discover scopes from mandates.md files on disk
    if query.scopes is not None:
        scopes_to_export = list(query.scopes)
    else:
        scopes_to_export = _discover_mandate_scopes(scope_root)

    mandates_by_scope: dict[str, list[Any]] = {}
    meta_by_scope: dict[str, Any] = {}
    for scope in scopes_to_export:
        # An absent mandates.md already returns [] from list_mandates() WITHOUT
        # raising. The only things a broad except would swallow here are a
        # corrupt/malformed mandates.md (MandateInvalid, re-raised by
        # list_mandates) and a bad caller-supplied scope string (ValueError) —
        # both are integrity failures that MUST fail the export loudly rather
        # than vanish as a silent empty scope (export-as-faithful-extraction
        # contract, spec/40 prep finding P1). Let them propagate.
        mandates = backend.list_mandates(scope)
        mandates_by_scope[scope] = mandates

        # Capture the project-root _meta policy block. Only project-root scopes
        # may carry a valid ## _meta section (a per-agent _meta is invalid and
        # the parser skips it with a warning). _mandates_path() validates the
        # scope string and refuses path traversal; MandateInvalid / ValueError
        # propagate for the same fail-loud reason as list_mandates above.
        if scope.startswith("project:"):
            mandates_path = backend._mandates_path(scope)
            if mandates_path.is_file():
                meta, _mandates = _parse_mandates_md(
                    mandates_path, scope=scope, is_project_root=True
                )
                if meta is not None:
                    meta_by_scope[scope] = meta

    return MandateExport(
        mandates_by_scope=mandates_by_scope,
        backend_id=backend.backend_id,
        scope_root=str(scope_root),
        meta_by_scope=meta_by_scope,
    )


def _discover_mandate_scopes(scope_root: Path) -> list[str]:
    """Find all mandate scopes by scanning for mandates.md files.

    Returns scope strings in the format ``"agent:NAME"`` or ``"project:NAME"``.
    The project-root mandates.md maps to ``"project:<scope_root.name>"``.
    Per-agent mandates.md files map to ``"agent:<agent_dir_name>"``.
    """
    scopes: list[str] = []

    # Project-root mandates.md → "project:<root_name>"
    project_mandates = scope_root / "mandates.md"
    if project_mandates.exists():
        scopes.append(f"project:{scope_root.name}")

    # Per-agent mandates.md → "agent:<dir_name>"
    if scope_root.is_dir():
        for child in sorted(scope_root.iterdir()):
            if (
                child.is_dir()
                and not child.is_symlink()
                and not child.name.startswith(".")
            ):
                agent_mandates = child / "mandates.md"
                if agent_mandates.exists():
                    scopes.append(f"agent:{child.name}")

    return scopes


# ──────────────────────────────────────────────────────────────────────────────
# CorpusBackend export


def export_corpus(
    backend: "FilesystemCorpusBackend",
    query: CorpusExportQuery | None = None,
) -> CorpusExport:
    """Export corpus pages as a CorpusExport canonical object.

    MUST enumerate state via list_pages() — MUST NOT route through query(text)
    or any method that may invoke an embedding model. Export is state
    extraction, not semantic retrieval (spec/40 MUST 6).

    Reads raw file bytes directly from disk for Tier A byte-exact fidelity,
    using the same passthrough-renderer approach as export_memory().

    For CorpusBackend, both wiki and raw corpora are exported by default
    (query.corpus=None). Passing query.corpus="wiki" or "raw" exports only
    that corpus. Exporting wiki+raw in one call produces the same result as
    exporting wiki then raw separately (associativity invariant in the
    conformance test).

    Raw-corpus files with a NON-.md extension (e.g. ``data.txt`` that an
    operator placed manually) are included byte-for-byte: ``list_pages('raw')``
    enumerates them with ``ref.name == entry.stem``, so this resolves the real
    on-disk file from the ref rather than guessing ``{ref.name}.md`` (which
    would silently drop them — spec/40 prep finding P0). For non-.md raw files
    the page metadata object is built directly from the ref (``read_page`` only
    resolves ``{name}.md`` and would return None).

    Stem collisions are resolved to DISTINCT files. ``list_pages('raw')`` yields
    one ref per on-disk file, and two files can share a stem (e.g. ``data.txt``
    and ``data.json`` both produce ``ref.name == 'data'``). Each ref MUST pair to
    a UNIQUE file — resolving every same-stem ref to the first sorted candidate
    would export one file twice and drop the other (spec/40 round-trip Round-2
    finding). A per-corpus ``consumed`` set tracks already-paired paths so each
    ref claims a different file.

    Args:
        backend: a FilesystemCorpusBackend instance.
        query: optional export filter. Pass None for both corpora, all pages.

    Returns:
        CorpusExport with pages_with_bytes populated per corpus.
    """
    from ..corpus.types import CorpusPage
    from .renderer import render_corpus_page_bytes_from_raw

    if query is None:
        query = CorpusExportQuery()

    corpora_to_export: list[str]
    if query.corpus is None:
        corpora_to_export = ["wiki", "raw"]
    else:
        corpora_to_export = [query.corpus]

    pages_with_bytes: dict[str, list[tuple[Any, bytes]]] = {}

    for corpus in corpora_to_export:
        pages_for_corpus: list[tuple[Any, bytes]] = []

        # Use list_pages() — not query(text) — per spec/40 MUST 6.
        page_refs = backend.list_pages(  # type: ignore[arg-type]
            corpus,
            limit=query.limit,
            offset=query.offset,
        )

        corpus_dir = backend._corpus_dir(corpus)  # type: ignore[attr-defined]

        # Pre-build a stem → sorted[paths] index for the corpus dir in one
        # O(n) scan so the fallback resolver doesn't call iterdir() per ref
        # (FIX 6: avoids O(n²) for raw corpora with many non-.md files).
        try:
            _stem_index: dict[str, list[Path]] = {}
            for _entry in corpus_dir.iterdir() if corpus_dir.is_dir() else []:
                if _entry.is_file() and not _entry.name.startswith("."):
                    _stem_index.setdefault(_entry.stem, []).append(_entry)
            # Sort each bucket once so per-ref lookups are O(1) pops.
            for _stem_key in _stem_index:
                _stem_index[_stem_key].sort()
        except OSError:
            _stem_index = {}

        # Track files already paired to a ref this corpus so two same-stem refs
        # (data.txt + data.json → both ref.name='data') each claim a DISTINCT
        # on-disk file instead of both resolving to candidates[0] (which would
        # drop one and duplicate the other — spec/40 round-trip Round-2 finding).
        consumed: set[Path] = set()

        for ref in page_refs:
            # Resolve the ACTUAL on-disk file for this ref. list_pages() strips
            # the extension (ref.name == entry.stem), so a raw file data.txt
            # yields ref.name='data'. Prefer the canonical {name}.md (when not
            # already consumed); otherwise find a not-yet-consumed concrete file
            # whose stem == ref.name (handles .txt/.pdf/.json/etc. raw files that
            # the framework write path never produces but an operator may place
            # manually).
            page_path: Path | None = corpus_dir / f"{ref.name}.md"
            if page_path in consumed or not page_path.is_file():
                page_path = _resolve_corpus_file_by_stem_index(
                    corpus_dir, ref.name, _stem_index, consumed=consumed
                )
            if page_path is None or not page_path.is_file():
                continue
            consumed.add(page_path)

            # Enforce path containment before reading — safe_resolve_under raises
            # PathTraversalError if page_path resolves outside corpus_dir (FIX 4b).
            safe_resolve_under(page_path, corpus_dir)
            # Read raw bytes directly for Tier A fidelity, routed through the
            # designated passthrough renderer (spec/40 §"Renderer module" insertion
            # point for future read-path sharing).
            raw_bytes = render_corpus_page_bytes_from_raw(page_path.read_bytes())

            if page_path.suffix.lower() == ".md":
                page = backend.read_page(ref.name, corpus)  # type: ignore[arg-type]
            else:
                # read_page() hardcodes {name}.md and would return None for a
                # non-.md raw file. Build the metadata object from the ref — the
                # exported bytes are the authoritative content; the page object
                # is for inspection only.
                page = CorpusPage(ref=ref, body=page_path.read_text(encoding="utf-8"))
            if page is not None:
                pages_for_corpus.append((page, raw_bytes))

        pages_with_bytes[corpus] = pages_for_corpus

    return CorpusExport(
        pages_with_bytes=pages_with_bytes,
        backend_id=backend.backend_id,
        scope=str(backend._agent_root),  # type: ignore[attr-defined]
    )


def _resolve_corpus_file_by_stem(
    corpus_dir: Path, stem: str, *, consumed: set[Path] | None = None
) -> Path | None:
    """Return the concrete top-level file in ``corpus_dir`` whose stem == ``stem``.

    Matches a raw-corpus ``CorpusRef.name`` (which is the extension-stripped
    filename stem) back to its real on-disk file, including non-.md extensions
    (.txt/.pdf/.json/etc.). Prefers a bare-name file (no extension) when present;
    otherwise returns the first extension match in sorted order for determinism.

    When ``consumed`` is supplied, files already in that set are skipped so two
    refs sharing a stem (e.g. ``data.txt`` and ``data.json``, both
    ``CorpusRef.name == 'data'``) resolve to DISTINCT files rather than both
    returning the same first-sorted candidate (which would drop one file and
    export the other twice — spec/40 round-trip Round-2 finding). The caller is
    responsible for adding the returned path to ``consumed``.

    Returns None when no not-yet-consumed file matches.
    """
    consumed = consumed if consumed is not None else set()
    # Exact bare-name file (stem with no suffix) wins if it exists and is free.
    bare = corpus_dir / stem
    if bare.is_file() and bare not in consumed:
        return bare
    try:
        candidates = sorted(
            entry
            for entry in corpus_dir.iterdir()
            if entry.is_file()
            and not entry.name.startswith(".")
            and entry.stem == stem
            and entry not in consumed
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


def _resolve_corpus_file_by_stem_index(
    corpus_dir: Path,
    stem: str,
    stem_index: dict[str, list[Path]],
    *,
    consumed: set[Path] | None = None,
) -> Path | None:
    """O(1) variant of ``_resolve_corpus_file_by_stem`` using a pre-built index.

    ``stem_index`` maps each stem to a sorted list of all matching files in
    ``corpus_dir``.  It is built ONCE per corpus (O(n) total) by the caller so
    repeated calls here are O(k) where k is the number of same-stem files —
    not O(n) per call.

    The ``consumed`` logic and return contract are identical to
    ``_resolve_corpus_file_by_stem``.  Callers MUST add the returned path to
    ``consumed`` before the next call to maintain the collision invariant.
    """
    consumed = consumed if consumed is not None else set()
    # Exact bare-name file (stem with no suffix) wins if it exists and is free.
    bare = corpus_dir / stem
    if bare.is_file() and bare not in consumed:
        return bare
    candidates = stem_index.get(stem, [])
    for candidate in candidates:
        if candidate not in consumed:
            return candidate
    return None


# ──────────────────────────────────────────────────────────────────────────────
# LockBackend export


def export_lock(
    backend: "FilesystemLockBackend",
    query: LockExportQuery | None = None,
) -> LockExport:
    """Export lock backend configuration as a LockExport canonical object.

    LockBackend exports its CONFIGURATION only — scope_root and backend_id.
    Runtime lock state (currently-held locks, PID, acquired_at) is ephemeral
    and MUST NOT be included (spec/40 §"LockBackend export contract").

    The ``supports_canonical_export=True`` declaration affirms Protocol
    composition for conformance testing; it does NOT imply there is live
    state to migrate. This export ALWAYS returns zero lock records (the
    conformance test MUST assert zero records, not skip).

    Ephemeral runtime sidecars (.lease.json, .lock files) are explicitly
    OUT per the versions-export-scope ruling (T4 in TENSIONS.md).

    Args:
        backend: a FilesystemLockBackend instance.
        query: unused (present for Protocol surface uniformity).

    Returns:
        LockExport with scope_root and backend_id only. lock_file_names
        is always empty (no persistent lock state to export).
    """
    return LockExport(
        scope_root=str(backend._scope_root),
        backend_id=backend.backend_id,
        lock_file_names=[],  # Always empty — runtime lease files are ephemeral
    )


# ──────────────────────────────────────────────────────────────────────────────
# SecretBackend export

# Human-readable hints for known provider keys (deployment-agnostic descriptions).
# MUST NOT include env-var names, file paths, or source strings.
# These are logical descriptions only — portable across deployments.
_LOGICAL_KEY_HINTS: dict[str, str] = {
    "anthropic": "Anthropic Claude API credential",
    "openai": "OpenAI API credential",
    "moonshot": "Moonshot AI API credential",
}


def export_secret(
    backend: Any,  # FilesystemSecretBackend (GCP export deferred to #432)
    query: SecretExportQuery | None = None,
) -> SecretExport:
    """Export secret backend wiring map as a SecretExport canonical object.

    Emits ONLY logical reference names + binding hints (LOGICAL-SECRET +
    BINDING-HINT abstraction). NEVER contains resolved plaintext values.
    This is an absolute invariant — spec/40 MUST 9.

    Key discovery: scopes to the framework's canonical provider keys from
    ``_PROVIDER_METADATA`` (anthropic, openai, moonshot). Custom operator
    keys not in ``_PROVIDER_METADATA`` are out of scope for PR1 (spec/40
    §"SecretBackend export contract (PR1 scope)" follow-up issue filed as #432).

    This export impl calls ``backend.locate(key)`` for each logical key to
    check presence. NEVER calls ``backend.get()`` or ``backend.get_optional()``
    — doing so would leak resolved plaintext into the export (prep finding P0).

    Args:
        backend: a FilesystemSecretBackend instance. GCPSecretManagerBackend
            ships no export()/export_all() and advertises
            ``supports_canonical_export=False``; GCP export is deferred to #432.
        query: optional export filter. Pass None to export all known provider
            keys. Pass query.logical_keys=[...] to export specific keys only.

    Returns:
        SecretExport with entries as SecretExportRef list (no plaintext values).
    """
    from ..secret_backend.filesystem import _PROVIDER_METADATA

    # Drift guard: _LOGICAL_KEY_HINTS must cover every key in _PROVIDER_METADATA.
    # If a new provider is added to _PROVIDER_METADATA without a corresponding hint
    # in _LOGICAL_KEY_HINTS, this assert fires at import-time of the first call
    # rather than silently falling back to the generic hint string.
    assert set(_LOGICAL_KEY_HINTS) >= set(_PROVIDER_METADATA), (
        f"_LOGICAL_KEY_HINTS is missing keys present in _PROVIDER_METADATA: "
        f"{set(_PROVIDER_METADATA) - set(_LOGICAL_KEY_HINTS)}. "
        "Add a deployment-agnostic hint string for each new provider key."
    )

    if query is None:
        query = SecretExportQuery()

    keys_to_export = (
        query.logical_keys
        if query.logical_keys is not None
        else list(_PROVIDER_METADATA.keys())
    )

    entries: list[SecretExportRef] = []

    for logical_key in keys_to_export:
        hint = _LOGICAL_KEY_HINTS.get(
            logical_key,
            f"API credential for {logical_key}",
        )
        # Find the env vars for this logical key
        if logical_key in _PROVIDER_METADATA:
            env_vars, _keychain = _PROVIDER_METADATA[logical_key]
            primary_env = env_vars[0] if env_vars else f"{logical_key.upper()}_API_KEY"
        else:
            primary_env = f"{logical_key.upper()}_API_KEY"

        # Call locate() to check presence — NEVER get() or get_optional().
        # locate() returns None for an absent key (does NOT raise) — so a
        # try/except that sets present=True on any non-exception return is
        # WRONG (it would mark all keys as present). Use a direct None-check.
        # Only propagate ValueError from _validate_key (malformed key name).
        present = backend.locate(primary_env) is not None

        entries.append(
            SecretExportRef(
                logical_key=logical_key,
                hint=hint,
                present=present,
            )
        )

    return SecretExport(
        entries=entries,
        backend_id=backend.backend_id,
    )

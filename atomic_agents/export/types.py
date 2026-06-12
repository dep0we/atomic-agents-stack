"""Canonical types for the Exportable Protocol (spec/40).

Two families of types live here:

1. **ExportQuery** — per-backend query types for filtered/paginated export.
   Each backend narrows the query type in its own implementation; at the
   Protocol surface ``export(query=None)`` uses ``None`` for unbounded export.
   Per-backend specializations:

   - ``MemoryExportQuery`` — filters for MemoryBackend export.
   - ``LogExportQuery`` — wraps the existing ``LogQuery`` for LogBackend.
   - ``MandateExportQuery`` — scope filter for MandateBackend.
   - ``CorpusExportQuery`` — corpus discriminator for CorpusBackend.
   - ``LockExportQuery`` — placeholder; LockBackend export always returns
     the location map (no state to filter).
   - ``SecretExportQuery`` — key-list override for SecretBackend.

2. **Export result types** — the TYPED in-memory canonical objects returned
   by ``export()``. These carry the data; the shared renderer in
   ``atomic_agents/export/renderer.py`` turns them into on-disk bytes.
   The types extend ``ExportableResult`` (a minimal marker base class) so
   Protocol static typing can narrow generically.

3. **SecretExportRef** — the logical-secret + binding-hint abstraction for
   SecretBackend export. NEVER carries resolved plaintext (spec/40 MUST 9).

See docs/spec/40-canonical-export.md for the full normative contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..logs.types import LogQuery

# ExportableResult lives in the dependency-free top-level leaf _export_base.py so
# that goal/types.py can import it WITHOUT triggering this package's __init__
# (which imports goal.types and would otherwise form a cycle through
# goal/__init__.py). Re-export it here so the public surface
# 'from atomic_agents.export import ExportableResult' is unchanged.
from .._export_base import ExportableResult as ExportableResult  # noqa: F401 (re-export)

# GoalExport is defined in goal/types.py (it subclasses ExportableResult imported
# from the leaf module above). Re-export it here so callers can use
# 'from atomic_agents.export import GoalExport'.
from ..goal.types import GoalExport as GoalExport  # noqa: F401 (re-export)

# OutcomeExport is defined in outcome/types.py (it subclasses ExportableResult
# imported from the leaf module _export_base.py — NOT from export/types.py to
# avoid a circular import: export/types.py → outcome/types.py → _export_base.py
# is the safe chain). Re-export it here so callers can use
# 'from atomic_agents.export import OutcomeExport'.
from ..outcome.types import OutcomeExport as OutcomeExport  # noqa: F401 (re-export)

# JournalExport is defined in journal/types.py (it subclasses ExportableResult
# imported from the leaf module _export_base.py — same safe chain as OutcomeExport).
# Re-export here so callers can use 'from atomic_agents.export import JournalExport'.
from ..journal.types import JournalExport as JournalExport  # noqa: F401 (re-export)


# ──────────────────────────────────────────────────────────────────────────────
# Per-backend export result types


@dataclass
class MemoryExport(ExportableResult):
    """Canonical export from a MemoryBackend (spec/40 §"Per-backend export contracts").

    Holds the current live notes. Raw file bytes are included alongside the
    parsed ``Note`` objects so the Tier A renderer can produce byte-exact
    output without re-serializing through ``frontmatter.dumps()``.

    Fields:
        notes_with_bytes: list of ``(Note, raw_bytes)`` tuples. The
            ``raw_bytes`` element is the exact bytes that exist on disk for
            this note (read via ``Path.read_bytes()``). The shared renderer
            for Tier A backends writes these bytes directly rather than
            re-serializing through ``frontmatter.dumps()``, which avoids
            the date-formatting and key-ordering divergences documented in
            spec/40 §"Tier A vs Tier B fidelity".
        backend_id: stable backend identifier, e.g. ``"filesystem"``.
        scope: scope path as a string (the agent root or equivalent).
    """

    notes_with_bytes: list[tuple[object, bytes]]  # list of (Note, raw_bytes)
    backend_id: str
    scope: str


@dataclass
class LogExport(ExportableResult):
    """Canonical export from a LogBackend (spec/40 §"Per-backend export contracts").

    Holds the JSONL lines as raw bytes (exactly as written by
    ``FilesystemLogBackend.append()``). The renderer writes these bytes
    directly to preserve Tier A byte-exact fidelity.

    IMPORTANT: The bytes here are raw JSONL lines produced by
    ``json.dumps(record.to_dict())`` — NOT sorted-key output from
    ``_canonical.canonical_json()``. Using canonical_json would break Tier A
    byte-match (spec/40 MUST 8 + MUST NOT).

    Fields:
        records_with_bytes: list of ``(RunRecord, raw_jsonl_bytes)`` tuples.
            For Tier A backends, ``raw_jsonl_bytes`` is the exact bytes that
            exist on disk (including the trailing newline). For Tier B backends,
            ``raw_jsonl_bytes`` is produced by
            ``json.dumps(record.to_dict()).encode("utf-8") + b"\\n"`` —
            ts-first insertion order, NOT sorted.
        backend_id: stable backend identifier.
        scope: agent root path as a string.
    """

    records_with_bytes: list[
        tuple[object, bytes]
    ]  # list of (RunRecord, raw_jsonl_bytes)
    backend_id: str
    scope: str


@dataclass
class MandateExport(ExportableResult):
    """Canonical export from a MandateBackend (spec/40 §"Per-backend export contracts").

    Exports the MANDATE DEFINITIONS only — the ``Mandate`` objects parsed from
    ``mandates.md``. The ``.judge-state/mandates.json`` dedup sidecar is an
    implementation detail of the deduplication algorithm, not a portable agent
    artifact, and is explicitly excluded (analogous to a SQLite WAL file).

    The ``render_mandates_md()`` renderer in ``renderer.py`` re-serializes
    ``Mandate`` objects (and the project-root ``_meta`` policy block) back to
    ``mandates.md``-equivalent text; it is consumed by export consumers (the
    #430 CLI and the conformance suite), not by ``export()`` itself, which
    returns these typed objects.

    Fields:
        mandates_by_scope: mapping from scope string (``"agent:NAME"`` or
            ``"project:NAME"``) to the list of ``Mandate`` objects from that
            scope. Empty list when ``mandates.md`` is absent.
        backend_id: stable backend identifier.
        scope_root: root directory of the mandate backend.
        meta_by_scope: mapping from project-root scope string to the parsed
            ``ProjectMandateMeta`` (the ``## _meta`` policy block:
            ``per_agent_mandate_policy`` + ``allowed_per_agent_ids``). This is a
            security boundary governing which agents may hold mandates, so it
            MUST survive the export round-trip — dropping it would silently
            revert a ``forbidden`` policy to the ``open`` default (spec/40
            Round-2 finding). Absent (no entry) for scopes without a ``_meta``
            block and for per-agent scopes (where ``_meta`` is invalid).
    """

    mandates_by_scope: dict[str, list[object]]  # dict[scope_str, list[Mandate]]
    backend_id: str
    scope_root: str
    # dict[scope_str, ProjectMandateMeta] — only project-root scopes with a
    # ## _meta section appear. Optional/default-empty to keep existing
    # instantiation sites and the frozen-shape backward compatible.
    meta_by_scope: dict[str, object] = field(default_factory=dict)


@dataclass
class CorpusExport(ExportableResult):
    """Canonical export from a CorpusBackend (spec/40 §"Per-backend export contracts").

    Holds corpus pages with raw file bytes. Two corpora are supported: ``wiki``
    and ``raw``. Both are included by default; the ``corpus`` filter selects
    one or both.

    The raw bytes approach preserves Tier A byte-exact fidelity for the
    filesystem reference impl, avoiding the same date-formatting and
    extra-frontmatter key-ordering issues as MemoryExport.

    Fields:
        pages_with_bytes: mapping from corpus name to list of
            ``(CorpusPage, raw_bytes)`` tuples.
        backend_id: stable backend identifier.
        scope: agent root path as a string.
    """

    pages_with_bytes: dict[
        str, list[tuple[object, bytes]]
    ]  # dict[corpus, list[(CorpusPage, bytes)]]
    backend_id: str
    scope: str


@dataclass
class LockExport(ExportableResult):
    """Canonical export from a LockBackend (spec/40 §"Per-backend export contracts").

    LockBackend exports its CONFIGURATION (scope_root, backend_id) only.
    Runtime lock state (currently-held locks, PID, acquired_at) is
    ephemeral and MUST NOT be included (spec/40 §"LockBackend export contract").

    The ``supports_canonical_export=True`` declaration on LockBackend affirms
    Protocol composition for conformance testing; it does NOT imply there is
    live state to migrate. The conformance test MUST assert this export
    contains zero lock records — only the location map.

    Fields:
        scope_root: directory under which lock files live.
        backend_id: stable backend identifier.
        lock_file_names: ALWAYS ``[]`` for FilesystemLockBackend. Runtime lease
            files (``.lease.json``, ``.lock``) are ephemeral and never exported
            (spec/40 §"LockBackend export contract"); LockBackend exports its
            configuration (scope_root, backend_id) only. The field exists for
            forward-compat with a hypothetical backend that has persistent
            named-lock state to migrate.
    """

    scope_root: str
    backend_id: str
    lock_file_names: list[str] = field(default_factory=list)


@dataclass
class SecretExport(ExportableResult):
    """Canonical export from a SecretBackend (spec/40 §"Per-backend export contracts").

    Emits ONLY logical reference names + binding hints. NEVER contains resolved
    plaintext values. This is the mandatory invariant — see spec/40 MUST 9.

    Fields:
        entries: list of ``SecretExportRef`` objects. Each entry names a
            logical key (e.g. ``"anthropic"``) and a deployment-agnostic
            hint (e.g. ``"Anthropic API key"``). No source strings, no
            env-var names, no plaintext values.
        backend_id: stable backend identifier.
    """

    entries: list["SecretExportRef"]
    backend_id: str


# ──────────────────────────────────────────────────────────────────────────────
# SecretExportRef — logical-secret + binding-hint abstraction


@dataclass(frozen=True)
class SecretExportRef:
    """Logical secret reference for SecretBackend export.

    MUST NOT contain resolved plaintext values. MUST NOT contain
    deployment-specific source strings (env: paths, keychain names, file
    paths). These are deployment-specific resolution names that MUST NOT
    bake into the portable export shape (spec/40 ruling: logical-secret +
    binding-hint abstraction, NOT concrete source strings).

    Fields:
        logical_key: canonical logical key name, e.g. ``"anthropic"``,
            ``"openai"``, ``"moonshot"``. This is the config_key from
            ``_PROVIDER_METADATA``, not the env-var name.
        hint: deployment-agnostic human-readable description of what
            the key is for, e.g. ``"Anthropic Claude API credential"``.
            MUST NOT include env-var names, file paths, or any value
            that resembles a credential.
        present: True if the key was found at export time (via locate()).
            False if the backend knows about the key but it is not
            configured on this machine.
    """

    logical_key: str
    hint: str
    present: bool


# ──────────────────────────────────────────────────────────────────────────────
# Per-backend export query types


@dataclass
class MemoryExportQuery:
    """Export filter for MemoryBackend.

    Fields:
        include_archived: include archived notes (default False).
        include_superseded: include superseded notes (default False).
        include_versions: include version history (default False).
            Opt-in — the default export captures only the current live
            state. Set True for compliance backups that need full history.
            See spec/40 §"MemoryBackend export contract" SHOULD clause.
    """

    include_archived: bool = False
    include_superseded: bool = False
    include_versions: bool = False
    # NOTE: include_versions is defined but NOT YET IMPLEMENTED (issue #433).
    # The version-history scan (walking .versions/ dirs) is deferred per spec/40
    # §"MemoryBackend export contract" SHOULD clause. Setting it to True today
    # produces the same result as False — but export_memory() emits a
    # warnings.warn() so the caller is NOT silently misled into believing the
    # export captured history (the field's stated compliance-backup use case).
    # When implemented, export_memory will walk backend._memory_dir /
    # '.versions/' and include versioned snapshots in the export.


@dataclass
class LogExportQuery:
    """Export filter for LogBackend.

    Wraps the existing LogQuery surface so export(query) reuses the
    backend's native filter, per spec/40 §"Granularity and export_all() guidance" ruling.

    Fields:
        log_query: a ``LogQuery`` instance from ``atomic_agents.logs.types``.
            Pass ``None`` for unbounded export (all records).

    Example (org-fleet bounded export):
        ``LogExportQuery(log_query=LogQuery(since=last_30_days))``
    """

    log_query: "LogQuery | None" = None


@dataclass
class MandateExportQuery:
    """Export filter for MandateBackend.

    Fields:
        scopes: list of scope strings to export (``"agent:NAME"`` or
            ``"project:NAME"``). Pass ``None`` for all known scopes.
    """

    scopes: list[str] | None = None


@dataclass
class CorpusExportQuery:
    """Export filter for CorpusBackend.

    Fields:
        corpus: which corpus to export — ``"wiki"``, ``"raw"``, or
            ``None`` for both. ``None`` is the default (export both).
        limit: max pages per corpus. ``None`` for unbounded.
        offset: page offset for paged export. Default 0.
    """

    corpus: Literal["wiki", "raw"] | None = None
    limit: int | None = None
    offset: int = 0


@dataclass
class LockExportQuery:
    """Export filter for LockBackend.

    LockBackend export always returns the location map — there is no
    state to filter. This class exists for Protocol surface uniformity.
    """

    pass


@dataclass
class SecretExportQuery:
    """Export filter for SecretBackend.

    Fields:
        logical_keys: explicit list of logical key names to export
            (e.g. ``["anthropic", "openai"]``). Pass ``None`` to export
            all known framework provider keys (the ``_PROVIDER_METADATA``
            keys). Custom operator keys not in ``_PROVIDER_METADATA`` are
            out of scope for PR1 and are documented as such in spec/40.
    """

    logical_keys: list[str] | None = None

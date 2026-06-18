"""MemoryBackend protocol and shared dataclasses.

Defines the storage abstraction that decouples atomic-agents core from any
specific memory storage implementation (filesystem, SQLite, Postgres, vector).

All concrete backends must implement MemoryBackend. The default is
FilesystemBackend (see filesystem.py). Future backends ship as separate
pip-installable packages that call register_backend() on import.

See docs/spec/20-memory-backend.md for the full specification.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..types import Capture

    # EmbeddingBackend imported under TYPE_CHECKING only to avoid circular
    # imports and keep memory package side-effect-free at module load.
    from ..embedding.backend import EmbeddingBackend


# ──────────────────────────────────────────────────────────────────
# MemoryCapabilities (spec/20 PR-3 addendum)


@dataclass(frozen=True)
class MemoryCapabilities:
    """Capability advertisement for semantic-search-capable MemoryBackend impls.

    spec/20 PR-3 addendum: introduced alongside ``PgvectorMemoryBackend`` to
    give doctor and audit tooling a single inspection pattern for the embedding
    backend, mirroring ``CorpusCapabilities.embedding_backend_resolved`` from
    spec/34.

    Fields
    ------
    ``embedding_provider``: provider label string (e.g. ``"openai"``), or
        ``None`` when semantic search is not supported.  Consistent with
        ``CorpusCapabilities.embedding_provider`` semantics.
    ``embedding_backend_resolved``: the live ``EmbeddingBackend`` instance, or
        ``None`` when no backend is configured.  NOT serialized in snapshots
        (spec/24 always-[] clamp).  When non-None, its ``provider_id`` MUST
        match ``embedding_provider``.

    Compatibility alias
    -------------------
    ``PgvectorMemoryBackend.supports_semantic_search`` returns
    ``capabilities().embedding_provider is not None`` so callers using the
    existing boolean property continue to work without modification.
    ``FilesystemBackend`` and ``PostgresMemoryBackend`` are NOT required to
    implement ``capabilities()`` in this PR — they retain the ``@property``
    idiom for backward compatibility.
    """

    embedding_provider: str | None
    embedding_backend_resolved: "EmbeddingBackend | None" = None


# ──────────────────────────────────────────────────────────────────
# Shared dataclasses


@dataclass
class NoteRef:
    """Lightweight metadata-only reference (cheap to list).

    Callers should prefer NoteRef for listing operations and only call
    read_note() when they need the full body.
    """

    name: str  # filename for fs; primary key for db
    type: str  # user/feedback/project/decision/reference
    description: str
    captured: date | None
    last_seen: date | None
    pinned: bool
    confidence: str
    archived: bool  # explicit filter field
    superseded_by: str | None  # explicit filter field


@dataclass
class Note:
    """Full read model — superset of Capture + read-only metadata.

    The full read model returned by read_note(). Unlike Capture (which is
    the write-path input), Note includes all storage-managed fields.
    """

    type: str
    name: str
    description: str
    confidence: str
    sources: list[str]
    body: str
    supersedes: str | None
    merge_into: str | None
    pinned: bool
    expires_at: str | None
    tags: list[str]
    # Read-only metadata fields (set by storage; not in Capture):
    captured: date | None
    last_seen: date | None
    archived: bool
    superseded_by: str | None
    schema_version: int
    extra_frontmatter: dict[str, Any]  # any custom fields the operator/agent added


@dataclass
class VersionRef:
    """Truly opaque version identifier.

    Callers must never parse or construct backend_id directly. Use
    resolve_version_token() to convert CLI tokens to VersionRef.
    Use __str__ for display only.
    """

    backend_id: str  # backend internals only

    def __str__(self) -> str:
        """Display token for CLI / dashboard.

        For FilesystemBackend, backend_id is ``<stem>/<version_filename>``.
        Display returns just the version filename (the part after the last '/'),
        preserving the operator-visible token format from before the P2.7 fix.
        """
        # If the backend_id uses the encoded stem/filename format, return only
        # the filename portion for display; otherwise return backend_id verbatim.
        if "/" in self.backend_id:
            return self.backend_id.split("/", 1)[1]
        return self.backend_id


@dataclass
class WritePolicy:
    """Per-call write-path enforcement context.

    Backends MUST receive and enforce write-path context on every write
    operation. Write_paths and read_only_paths come from agent config
    (tools.md) and must not be dropped in the abstraction layer.
    """

    write_paths: list[Path]
    read_only_paths: list[Path] = field(default_factory=list)


@dataclass
class MemoryStats:
    """Aggregate statistics for a memory backend instance.

    Used by dashboard/memory.py to render the Memory Snapshot tab without
    requiring filesystem-specific scanning logic.
    """

    total_notes: int
    by_type: dict[str, int]
    live_bytes: int
    version_history_bytes: int
    most_churned: list[tuple[str, int]]  # [(note_name, version_count), ...] top 20


class StagedMemory(abc.ABC):
    """Abstract base for staged write areas used by bulk operations (e.g., dream).

    Created by backend.create_staging(). Callers write notes via
    write_note(), then either apply_staging() (atomic swap) or
    discard_staging() (abandon). Never construct directly.

    Per scope §3, subclasses MUST implement write_note(), render_index_summary(),
    and stats(). The backend_id field identifies this staging area.
    """

    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id

    @abc.abstractmethod
    def write_note(self, capture: "Capture", policy: "WritePolicy") -> "NoteRef":
        """Write a capture to the staging area, enforcing policy."""
        ...

    @abc.abstractmethod
    def render_index_summary(self) -> str:
        """Return the INDEX.md-equivalent text for this staging area."""
        ...

    @abc.abstractmethod
    def stats(self) -> "MemoryStats":
        """Return aggregate statistics for the staged memory area."""
        ...


# ──────────────────────────────────────────────────────────────────
# The protocol


@runtime_checkable
class MemoryBackend(Protocol):
    """Storage abstraction for atomic memory notes.

    All method docstrings define the behavioral contract that every
    conforming backend must satisfy. See tests/test_memory_protocol_conformance.py
    for the machine-checkable version of this contract.
    """

    # ───── Read operations ─────

    def list_notes(
        self,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]:
        """Return NoteRef for all notes, excluding INDEX.md and hidden files.

        By default excludes archived and superseded notes (archived=True or
        superseded_by set). Pass include_archived=True / include_superseded=True
        to override.
        """
        ...

    def read_note(self, name: str) -> Note | None:
        """Return full Note (frontmatter + body) for the named note.

        Returns None if the note doesn't exist. name is the bare filename
        (e.g., feedback_comm_style.md).
        """
        ...

    def list_pinned(self) -> list[NoteRef]:
        """Return notes with pinned=True.

        Used by agent.py:_load_pinned_notes(). Always includes pinned notes
        regardless of archived/superseded status.
        """
        ...

    def list_recent(
        self,
        n: int,
        exclude_pinned: bool = True,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]:
        """Return up to n notes sorted by last_seen DESC.

        By default excludes pinned (already loaded separately), archived,
        and superseded notes.
        """
        ...

    def list_stale(
        self,
        threshold_days: int,
        exclude_pinned: bool = True,
    ) -> list[NoteRef]:
        """Return notes whose last_seen is older than threshold_days.

        Implicitly excludes archived and superseded notes. Optionally
        excludes pinned (default True — pinned notes stay fresh by
        definition).
        """
        ...

    def list_orphans(self) -> list[NoteRef]:
        """Return notes present on disk but missing from INDEX.md.

        FilesystemBackend: cross-reference memory/*.md against INDEX.md.
        Database backends: always returns empty list (no concept of orphans).
        """
        ...

    def list_by_type(self, type_name: str) -> list[NoteRef]:
        """Return all notes of a specific type (e.g., 'feedback')."""
        ...

    def render_index_summary(self) -> str:
        """Return the routing-layer text injected into the system prompt.

        FilesystemBackend returns INDEX.md verbatim if it exists; otherwise
        generates equivalent prose from list_notes() grouped by type.
        Database backends generate equivalent prose from their tables.

        Non-empty contract: must be parseable by humans + LLMs.
        """
        ...

    # ───── Write operations ─────

    def write_note(
        self,
        capture: "Capture",
        policy: WritePolicy,
        expected_content_sha256: str | None = None,
    ) -> NoteRef:
        """Write a capture to persistent storage. Enforces policy.

        Merge semantics (four cases):
        1. capture.merge_into is set: target MUST exist; refresh ONLY
           last_seen + sources (deduped); preserve body verbatim; snapshot
           pre-state first; do NOT update index. Raise SchemaValidationError
           if target missing.
        2. capture.merge_into is None and target name doesn't exist: write
           fresh; update index.
        3. capture.merge_into is None and target name exists with same
           content: orphan-recovery — snapshot pre-state, repair index,
           return existing ref (no body change).
        4. capture.merge_into is None and target name exists with different
           content: raise SchemaValidationError (use merge_into explicitly).

        Policy enforcement:
        - policy.write_paths must contain the target path; raise
          WritePathViolation if not.
        - policy.read_only_paths win even when target is under write_paths;
          raise WritePathViolation if target is under a read-only path.

        Optimistic concurrency:
        - expected_content_sha256: if provided and target exists, check sha256
          of current content; raise MemoryPreconditionFailed if mismatch.
        - If provided and target doesn't exist: raise MemoryPreconditionFailed
          (caller expected an existing note).
        """
        ...

    # ───── Versioning ─────

    def list_versions(self, name: str) -> list[VersionRef]:
        """Return version refs for the named note, newest first."""
        ...

    def read_version(self, version_ref: VersionRef) -> Note:
        """Return full Note from a version snapshot."""
        ...

    def restore_version(
        self,
        name: str,
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> NoteRef:
        """Atomically replace the live note with a snapshot's content.

        Snapshots pre-restore state first (so restore is itself reversible).
        Enforces policy on the write target.
        """
        ...

    def redact_version(
        self,
        version_ref: VersionRef,
        replacement: str = "[REDACTED]",
    ) -> None:
        """Replace a snapshot's body with a redaction marker.

        Frontmatter is preserved for audit trail. Used for compliance only.
        """
        ...

    def resolve_version_token(self, name: str, token: str) -> VersionRef:
        """Convert a user-typed token to an opaque VersionRef.

        FilesystemBackend resolves a version filename (e.g.,
        20260508T120000Z_ab12cd34.md) to a VersionRef.
        Database backends may resolve a row-id-shaped token.
        Raises VersionNotFound if the token cannot be resolved.
        """
        ...

    # ───── Bulk staging operations (for dream) ─────

    def create_staging(self) -> StagedMemory:
        """Create a staged write area for bulk operations.

        FilesystemBackend: creates a parallel directory under
        <agent>/dreams/.staging-<uuid>/memory/.
        Database backends: create a separate schema/table set.

        Returns a StagedMemory handle. Caller must eventually call
        apply_staging() or discard_staging().
        """
        ...

    def apply_staging(self, staging: StagedMemory, policy: WritePolicy) -> None:
        """Atomically swap live memory with the staged area.

        Lock-aware: acquires the agent lock before the directory swap so
        no in-flight agent.call() writes land in the wrong place.
        Snapshots pre-apply state so revert is possible.
        """
        ...

    def discard_staging(self, staging: StagedMemory) -> None:
        """Remove the staging area without touching live memory."""
        ...

    # ───── Stats (for dashboard) ─────

    def stats(self) -> MemoryStats:
        """Return aggregate statistics for the dashboard Memory tab."""
        ...

    def version_count(self, name: str) -> int:
        """Return the number of version snapshots for a note."""
        ...

    def last_mutation_at(self, name: str) -> datetime | None:
        """Return the timestamp of the most recent snapshot or write."""
        ...

    # ───── Capability advertisement ─────

    @property
    def supports_semantic_search(self) -> bool:
        """True if search() uses semantic/vector similarity."""
        ...

    @property
    def supports_canonical_export(self) -> bool:
        """True if this backend implements the Exportable Protocol (spec/40).

        Advertised as a ``@property`` on MemoryBackend (matching the existing
        ``supports_semantic_search`` @property idiom — the minority pattern
        among the 13 backends). The majority of backends use a capabilities()
        -> XCapabilities dataclass instead. The MemoryCapabilities convergence
        is tracked as a follow-up ([#431](https://github.com/dep0we/atomic-agents-stack/issues/431)),
        so for now both Memory capability flags are @property.

        FilesystemBackend: True.
        Future Postgres/pgvector backends: set to True when their export impl
        ships.
        """
        ...

    def search(self, query: str, limit: int = 10) -> list[NoteRef]:
        """Search notes. Semantic on supporting backends; substring fallback elsewhere."""
        ...

    # ───── Lifecycle ─────

    def close(self) -> None:
        """Release any resources held by this backend instance."""
        ...

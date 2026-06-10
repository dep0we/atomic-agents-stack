"""Exportable companion Protocol (spec/40).

Defines ONE new optional Protocol — ``Exportable`` — that state backends
compose to advertise and implement the canonical-shape export contract.
The 13 v1.0-locked Protocol surfaces stay FROZEN; no re-lock, no amendments.

Stateless backends (LLM, Judge) do NOT compose Exportable.
Config backends (Persona, Policy, AgentProfile, MCPServerRegistry) have
file-resident identity and may compose Exportable with identity semantics
(i.e. the export is the config file verbatim, not a state export).

The five state backends from PR1 (Memory, Log, Mandate, Corpus, Lock) all
compose Exportable. SecretBackend composes it with wiring-map-only semantics
(never plaintext values). See spec/40 §"Per-backend export contracts".

Design notes:

- ``export(query=None)`` returns a TYPED in-memory canonical object
  (``ExportableResult`` subclass), NOT bytes. The shared renderer in
  ``atomic_agents/export/renderer.py`` turns the typed object into on-disk
  bytes. This keeps the renderer OUTSIDE per-backend surfaces.

- ``export_all()`` is a thin convenience wrapper that calls
  ``export(query=None)``. It does NOT pre-collect results differently; it
  passes through whatever behavior ``export(query=None)`` has. For backends
  with large histories, callers MUST use ``export(query=...)`` with a bounded
  query rather than ``export_all()`` to avoid loading the full dataset.

- The ``supports_canonical_export`` capability field is added to each
  backend's ``XCapabilities`` frozen dataclass (majority idiom). MemoryBackend
  uses a ``@property`` (matching its existing ``supports_semantic_search``
  shape) per spec/40 §"MemoryBackend export contract".

See docs/spec/40-canonical-export.md for the full normative contract.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .types import ExportableResult


@runtime_checkable
class Exportable(Protocol):
    """Optional companion Protocol for state backends that support export.

    Backends compose this Protocol at their own definition:

        class FilesystemMemoryBackend:
            def export(self, query=None) -> MemoryExport: ...
            def export_all(self) -> MemoryExport: ...

    Callers that want to test for export support:

        if isinstance(backend, Exportable):
            result = backend.export()

    OR via the capability flag (preferred for new code):

        if backend.supports_canonical_export:  # MemoryBackend @property
        if backend.capabilities().supports_canonical_export:  # most backends
        if backend.capabilities.supports_canonical_export:  # MCPRegistry/Secret

    See spec/40 §"`supports_canonical_export` capability field" for the three-shape inconsistency
    and the ``get_supports_canonical_export()`` helper in
    ``tests/test_export_capability_advertisement.py`` that unifies them.

    Implementations MUST NOT subclass this Protocol — it is structural.
    """

    def export(self, query: Any = None) -> ExportableResult:
        """Export state as a typed canonical object.

        Args:
            query: backend-specific filter object. Each backend narrows
                the type in its own implementation signature. Pass ``None``
                for unbounded export (equivalent to ``export_all()``).

                Backend-specific query types (spec/40 §"Per-backend export contracts"):
                - MemoryBackend: ``MemoryExportQuery | None``
                - LogBackend: ``LogExportQuery | None``
                - MandateBackend: ``MandateExportQuery | None``
                - CorpusBackend: ``CorpusExportQuery | None``
                - LockBackend: ``LockExportQuery | None``
                - SecretBackend: ``SecretExportQuery | None``

        Returns:
            A typed ``ExportableResult`` subclass. The concrete type
            depends on the backend:
            - MemoryBackend → ``MemoryExport``
            - LogBackend → ``LogExport``
            - MandateBackend → ``MandateExport``
            - CorpusBackend → ``CorpusExport``
            - LockBackend → ``LockExport``
            - SecretBackend → ``SecretExport``

        MUST enumerate state via the backend's list/scan surface
        (e.g., ``list_notes()`` for MemoryBackend, ``list_pages()`` for
        CorpusBackend, ``query()`` for LogBackend). MUST NOT route through
        ``query(text, ...)`` or any method that may invoke an embedding
        model. Export is state extraction, not semantic retrieval
        (spec/40 MUST 6).

        The filesystem reference impl documents an acknowledged
        snapshot-consistency bound: export() produces a best-effort
        point-in-time snapshot; it does not acquire the agent-level
        LockBackend across the full read pass. Each exported object is
        read atomically (not mid-write), but full cross-object consistency
        requires the caller to hold the agent lock (spec/40 MUST 7).
        """
        ...

    def export_all(self) -> ExportableResult:
        """Convenience wrapper — unbounded export (all records, no filter).

        Equivalent to ``export(query=None)``. Does NOT pre-collect results
        differently; passes through to ``export(None)``.

        WARNING: For backends with large histories (>10K notes, >1M log
        records), this materializes ALL matching objects into memory. Org-fleet
        exporters MUST use ``export(query=...)`` with a bounded time window or
        limit instead. See spec/40 §"Granularity and export_all() guidance".
        """
        ...

"""Exportable Protocol + shared renderer for the canonical-shape export contract.

This package implements spec/40 (canonical-export) — the export/portability
spine of TENSIONS.md T15 (Position B).

Public surface:

- ``Exportable`` — the companion Protocol backends compose to support export.
- Export result types: ``MemoryExport``, ``LogExport``, ``MandateExport``,
  ``CorpusExport``, ``LockExport``, ``SecretExport``, ``GoalExport``,
  ``OutcomeExport``.
- Export query types: ``MemoryExportQuery``, ``LogExportQuery``, etc.
- ``SecretExportRef`` — logical-secret + binding-hint abstraction.
- ``render_run_record_bytes``, ``render_note_bytes_from_raw``, etc. — shared
  per-type renderers (accessible via ``atomic_agents.export.renderer``).

Filesystem export functions (``export_memory``, ``export_log``, etc.) are
implementation helpers — import them via ``atomic_agents.export.filesystem``,
not from this package root.

See docs/spec/40-canonical-export.md for the full normative contract.
"""

from .backend import Exportable
from .types import (
    CorpusExport,
    CorpusExportQuery,
    ExportableResult,
    GoalExport,
    LockExport,
    LockExportQuery,
    LogExport,
    LogExportQuery,
    MandateExport,
    JournalExport,
    MandateExportQuery,
    MemoryExport,
    MemoryExportQuery,
    OutcomeExport,
    SecretExport,
    SecretExportQuery,
    SecretExportRef,
)

__all__ = [
    "Exportable",
    "ExportableResult",
    "MemoryExport",
    "MemoryExportQuery",
    "LogExport",
    "LogExportQuery",
    "MandateExport",
    "MandateExportQuery",
    "CorpusExport",
    "CorpusExportQuery",
    "LockExport",
    "LockExportQuery",
    "SecretExport",
    "SecretExportQuery",
    "SecretExportRef",
    "GoalExport",
    "OutcomeExport",
    "JournalExport",
]

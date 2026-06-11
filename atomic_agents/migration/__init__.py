"""MigrationBackend Protocol — vault frontmatter schema migration abstraction.

This package ships the ``MigrationBackend`` Protocol and its filesystem
reference implementation (``FilesystemMigrationBackend``). It replaces the
old path-shaped free-function API in ``atomic_agents.migrate`` with a
backend-neutral protocol, mirroring the full backend-protocol series
(MemoryBackend, LockBackend, etc.).

### BREAKING change (issue #429)

The old path-shaped ``MigrationScript`` Protocol (``applies_to(path: Path)`` /
``migrate(path: Path, dry_run: bool)``) is REMOVED. Operator-authored migration
scripts must be rewritten to the new per-unit handle contract.

New script shape::

    from atomic_agents.migration import MigratableUnit

    FROM_VERSION = 1
    TO_VERSION = 2

    def applies_to(unit: MigratableUnit) -> bool:
        meta = unit.read_frontmatter()
        return meta.get("schema_version") == FROM_VERSION

    def migrate(unit: MigratableUnit) -> dict:
        meta = unit.read_frontmatter()
        meta["schema_version"] = TO_VERSION
        meta.setdefault("provenance", "v1_migrated")
        unit.write_frontmatter(meta)
        return {"unit_id": unit.unit_id, "changes": ["schema_version 1→2"]}

Note: scripts no longer receive a ``dry_run`` parameter. The ``MigratableUnit``
handle has ``dry_run`` baked in — ``write_frontmatter()`` is automatically a
no-op on dry-run handles. The runner owns the dry-run gate.

Public surface::

    from atomic_agents.migration import (
        # Protocol contract
        MigrationBackend,
        # Per-unit handle (what migration scripts receive)
        MigratableUnit,
        # Types
        MigrationCapabilities,
        MigrationSnapshotRef,
        MigrationPlan,
        MigrationResult,
        MigrationEvent,
        # Reference implementation
        FilesystemMigrationBackend,
        # Exceptions
        MigrationRollbackUnavailable,
        MigrationSnapshotNotFound,
        # Helpers
        parse_target_version,
        # Script type
        LoadedScript,
    )

See spec/03 §Schema-migration for the full normative contract.
"""

from __future__ import annotations

from .backend import MigrationBackend
from .filesystem import (
    FilesystemMigrationBackend,
    LoadedScript,
    MigrationRollbackUnavailable,
    MigrationSnapshotNotFound,
    parse_target_version,
)
from .types import (
    MigratableUnit,
    MigrationCapabilities,
    MigrationEvent,
    MigrationPlan,
    MigrationResult,
    MigrationSnapshotRef,
)

__all__ = [
    # Protocol
    "MigrationBackend",
    # Per-unit handle
    "MigratableUnit",
    # Canonical types
    "MigrationCapabilities",
    "MigrationSnapshotRef",
    "MigrationPlan",
    "MigrationResult",
    "MigrationEvent",
    # Reference implementation
    "FilesystemMigrationBackend",
    # Script type
    "LoadedScript",
    # Exceptions
    "MigrationRollbackUnavailable",
    "MigrationSnapshotNotFound",
    # Helpers
    "parse_target_version",
]

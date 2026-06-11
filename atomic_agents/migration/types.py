"""Canonical types for the MigrationBackend Protocol (spec/03 §Schema-migration).

The value records (``MigrationSnapshotRef``, ``MigrationCapabilities``,
``MigrationEvent``) are ``@dataclass(frozen=True)`` — immutable and comparable
by value, safe to pass across the runner / backend / diagnostic boundary
without defensive copying. ``MigratableUnit`` is a mutable handle (it carries
backend callables, so it cannot be frozen), and the orchestration records
``MigrationPlan`` / ``MigrationResult`` are intentionally non-frozen: the
runner mutates ``MigrationResult`` in place during a run (e.g.
``units_touched.append(...)``, ``rolled_back = True``).

``MigratableUnit`` is the per-unit handle that migration scripts receive.
Scripts call ``unit.read_frontmatter()`` and ``unit.write_frontmatter()``
instead of touching ``Path`` objects directly. This makes scripts
backend-neutral: the same script works over a filesystem backend or a
future database backend.

``MigrationSnapshotRef`` is the opaque snapshot handle returned by
``MigrationBackend.snapshot()`` and accepted by ``MigrationBackend.restore()``.
Its ``snapshot_id`` field is backend-issued and opaque to callers — do not
parse or construct it; pass it back unchanged to ``restore()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal


# ──────────────────────────────────────────────────────────────────
# Per-unit handle


@dataclass
class MigratableUnit:
    """A handle to one migratable vault unit (atomic note or wiki page).

    Migration scripts receive one of these per file and interact with
    the vault only through the two methods below — never through a raw
    ``Path``. This keeps scripts backend-neutral: the same script works
    over a filesystem backend or a future database backend.

    Fields:
        unit_id: opaque, stable identifier for this unit. For the
            filesystem backend this is the absolute path as a string;
            for a DB backend it is a row id or similar. Callers MUST
            NOT parse or construct this — treat it as a label for log
            messages and the MigrationResult summary.
        unit_type: discriminator — ``'memory'`` for atomic notes,
            ``'wiki'`` for wiki pages. Migration scripts that need to
            branch on the unit kind use this field instead of
            inspecting path components.
        dry_run: when ``True``, ``write_frontmatter()`` is a no-op.
            The runner bakes ``dry_run`` into the handle at construction
            so scripts do NOT need a ``dry_run`` parameter — the handle
            is the dry-run gate.

    The ``_read_fn`` and ``_write_fn`` slots are implementation details
    set by the backend; callers MUST NOT access them.
    """

    unit_id: str
    unit_type: Literal["memory", "wiki"]
    dry_run: bool
    # Backend-provided callables — not part of the public API.
    # Use read_frontmatter() / write_frontmatter() instead.
    _read_fn: Callable[[], dict[str, Any]] = field(repr=False, compare=False)
    _write_fn: Callable[[dict[str, Any]], None] = field(repr=False, compare=False)

    def read_frontmatter(self) -> dict[str, Any]:
        """Return the current frontmatter dict for this unit.

        The dict contains all frontmatter fields verbatim. Unknown /
        custom fields are preserved as-is. The returned dict is a copy
        — mutating it does NOT write anything; call ``write_frontmatter``
        with the modified dict to persist changes.
        """
        return self._read_fn()

    def write_frontmatter(self, metadata: dict[str, Any]) -> None:
        """Write updated frontmatter for this unit.

        When ``dry_run=True`` (baked in at handle construction) this
        method is a no-op — the backing store is left unchanged and
        nothing is written. Scripts do NOT need to check ``dry_run``
        themselves.

        The backend implementation MUST use atomic writes (temp + fsync +
        rename) so a crash mid-write leaves the original file intact.
        The body of the file is preserved; only frontmatter is replaced.

        Args:
            metadata: complete replacement frontmatter dict. All existing
                fields not present in ``metadata`` are dropped. Callers
                MUST include ``schema_version`` in the dict.
        """
        if self.dry_run:
            return
        self._write_fn(metadata)


# ──────────────────────────────────────────────────────────────────
# Snapshot handle


@dataclass(frozen=True)
class MigrationSnapshotRef:
    """Opaque handle to a vault snapshot created by ``MigrationBackend.snapshot()``.

    Return value of ``snapshot()``; pass unchanged to ``restore()``.
    The ``snapshot_id`` is backend-issued; its format is backend-specific
    (filesystem: tarball filename; DB: checkpoint name or row id). Callers
    MUST NOT parse or construct this value.

    Fields:
        backend_id: the ``backend_id`` of the issuing backend. Used by
            callers as a safety check: if you construct a backend with a
            different root and try to restore a snapshot from the wrong
            backend, the ``backend_id`` mismatch surfaces the error rather
            than silently producing undefined behavior.
        snapshot_id: opaque, backend-issued identifier. For the filesystem
            backend this is the tarball filename. The ``vN`` in the name is
            the TARGET version of the migration the snapshot precedes — a
            snapshot taken before migrating *to v2* is named
            ``"2026-08-12T143000_pre_v2_migration.tar.gz"``.
    """

    backend_id: str
    snapshot_id: str


# ──────────────────────────────────────────────────────────────────
# Capabilities


@dataclass(frozen=True)
class MigrationCapabilities:
    """Per-backend capability declaration.

    Conformance tests assert claim-vs-behavior parity. Honest capabilities
    let the runner fail fast against incompatible backends rather than
    discovering the mismatch mid-operation.

    Fields:
        supports_transactional_rollback: ``True`` when the backend can
            restore the vault to its pre-migration state via
            ``restore(snapshot_ref)``. The runner MUST refuse a destructive
            (non-dry-run) migration when this is ``False``.
        single_host_only: ``True`` when the backend's snapshot/restore
            atomicity guarantee (MUST 4) only holds on a single host —
            i.e. the snapshot is stored on local disk and a concurrent
            run on a *different* host would not be serialized by this
            backend's vault-level lock. The filesystem backend sets this
            ``True`` (snapshots live under ``_migrations/snapshots/`` on
            local disk). A future distributed backend that replicates
            snapshots and holds a cross-host lock would set it ``False``.
            Documented in the spec/03 Implementer Contract; advisory for
            operators choosing a deployment topology (the runner does not
            gate on it today).
    """

    supports_transactional_rollback: bool
    single_host_only: bool = True


# ──────────────────────────────────────────────────────────────────
# Plan + result


@dataclass
class MigrationPlan:
    """What the runner intends to do."""

    from_version: int
    to_version: int
    scripts: list[Any] = field(default_factory=list)  # list[LoadedScript]


@dataclass
class MigrationResult:
    """Outcome of a migration run."""

    plan: MigrationPlan
    snapshot_ref: MigrationSnapshotRef | None
    units_touched: list[dict] = field(default_factory=list)
    units_skipped: int = 0
    validation_passed: bool = False
    validation_errors: list[dict] = field(default_factory=list)
    rolled_back: bool = False
    dry_run: bool = False
    error: str = ""


# ──────────────────────────────────────────────────────────────────
# Migration event (audit)


@dataclass(frozen=True)
class MigrationEvent:
    """One JSONL audit record written by the runner on completion.

    Appended to ``<agents_root>/_migrations/migration.jsonl`` so operators
    have a queryable history of every migration run — including dry-runs,
    failures, and rollbacks — without coupling to the per-agent LogBackend
    (which may not exist yet when migration runs as a bootstrap step).

    The file is append-only, excluded from vault content walks, and
    preserved across snapshot/restore cycles.
    """

    run_id: str
    timestamp: str  # ISO 8601
    agents_root: str
    from_version: int
    to_version: int
    units_touched: int
    dry_run: bool
    rolled_back: bool
    error: str  # empty string on success

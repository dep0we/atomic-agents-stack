"""MigrationBackend Protocol — the contract every migration implementation satisfies.

Issue #429 (T13 migration-runner refactor) introduces this Protocol to replace
the old path-shaped ``run_migration(agents_root, target_version, dry_run)``
free-function runner with a backend-shaped abstraction. The motivation mirrors
the full backend-protocol series (MemoryBackend, LockBackend, etc.): decouple
the migration runner from the filesystem so future database backends (Postgres,
cloud-native) can satisfy the same protocol without forking the runner.

SCOPE BOUNDARY: ``MigrationBackend`` owns spec/03 vault FRONTMATTER schema
evolution across memory/wiki units only. Per-backend SQLite ``_ensure_schema``
DDL ladders (AgentProfile, ToolRegistry, Log) are internal and out of scope.

### BREAKING change (issue #429)

The path-shaped ``MigrationScript`` Protocol (``applies_to(path: Path)`` /
``migrate(path: Path, dry_run: bool)``) is REMOVED. Operator-authored migration
scripts must be rewritten to the new per-unit handle contract::

    from atomic_agents.migration import MigratableUnit

    FROM_VERSION = 1
    TO_VERSION = 2

    def applies_to(unit: MigratableUnit) -> bool:
        meta = unit.read_frontmatter()
        return meta.get("schema_version") == FROM_VERSION

    def migrate(unit: MigratableUnit) -> dict:
        meta = unit.read_frontmatter()
        meta["schema_version"] = TO_VERSION
        meta["provenance"] = meta.get("provenance", "v1_migrated")
        unit.write_frontmatter(meta)
        return {"unit_id": unit.unit_id, "changes": ["schema_version 1→2"]}

See spec/03 §Schema-migration for the full contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import (
    MigratableUnit,
    MigrationCapabilities,
    MigrationResult,
    MigrationSnapshotRef,
)


@runtime_checkable
class MigrationBackend(Protocol):
    """Contract every migration backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, MigrationBackend)`` to perform a method-presence
    check (not a signature check — signatures are static-typing's job).

    Scope is bound at backend construction. The runner constructs one
    ``FilesystemMigrationBackend(agents_root)`` and uses it for the
    entire migration run. Future Postgres backends carry a connection
    pool and a schema name at construction; the runner interface does
    not change.

    Rollback contract (HARD RULE per spec/03): backends that advertise
    ``capabilities().supports_transactional_rollback=True`` MUST implement
    ``snapshot()`` and ``restore()`` such that a ``restore(ref)`` call
    returns the vault to the exact state it was in when ``snapshot()``
    was called. The runner MUST refuse a destructive migration on a
    backend advertising ``supports_transactional_rollback=False``.

    Version-storage contract: ``read_schema_version()`` derives the
    current schema version from per-unit frontmatter — there is NO
    separate ``write_schema_version()`` method. Version bumps happen as
    a side effect of ``apply_unit()`` writing per-unit frontmatter, so
    per-unit frontmatter stays the ONLY writable truth (Principle #1).
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g., ``"filesystem"``, ``"postgres"``.

        Used by diagnostic tooling and by the runner for error messages.
        Treat as a backwards-compatibility surface — operator deployments
        may pin against these strings.
        """
        ...

    def capabilities(self) -> MigrationCapabilities:
        """Backend capability declaration.

        Conformance tests assert claim-vs-behavior parity. The runner
        checks ``supports_transactional_rollback`` as a pre-flight gate
        before any migration step.
        """
        ...

    def read_schema_version(self) -> int:
        """Return the current schema version of the vault.

        MUST return the MINIMUM ``schema_version`` observed across ALL
        enumerable units (the version the vault is effectively 'at').
        A vault with mixed versions returns the lowest — treating it as
        needing migration. An empty vault returns
        ``_schema.CURRENT_SCHEMA_VERSION``. A non-empty vault where NO unit
        yields a valid integer ``schema_version`` (every unit corrupt /
        unparseable) ALSO returns ``CURRENT_SCHEMA_VERSION`` — treated as
        "no safe forward migration available" — and SHOULD surface a
        diagnostic so corruption is not silently masked as already-current.

        Implementations MUST NOT sample a subset of units and MUST NOT
        return a cached value without guarantee of freshness on every
        call. A backend that caches a version number MUST re-derive it
        from unit state on every call or guarantee cache invalidation
        on every ``apply_unit()`` call.

        There is NO ``write_schema_version()`` companion — version bumps
        happen exclusively as a side effect of ``apply_unit()`` writing
        per-unit frontmatter.
        """
        ...

    def enumerate_units(self) -> list[MigratableUnit]:
        """Return all migratable units in the vault.

        MUST deduplicate by resolved path / row id so the same physical
        unit is not returned twice even when symlinks or joins could
        produce duplicate rows. Each returned ``MigratableUnit`` MUST
        have a populated ``unit_type`` field (``'memory'`` or ``'wiki'``).

        Non-content sidecars that carry NO ``schema_version`` — specifically
        the recall ``INDEX.md`` (spec/02) — are NOT migratable units and MUST
        be excluded from enumeration. Including them would skew
        ``read_schema_version()`` minima and the content-file count, and would
        diverge cross-backend unit-set semantics.

        The returned list is a snapshot — the runner iterates it exactly
        once and does not re-enumerate mid-migration.
        """
        ...

    def apply_unit(
        self,
        unit: MigratableUnit,
        script: object,
        dry_run: bool = True,
    ) -> dict:
        """Apply one migration script to one unit.

        The runner calls this for every (unit, script) pair; the backend
        MUST call ``script.module.applies_to(unit)`` internally and return
        an empty dict (skip) when it is ``False``. The runner does NOT
        pre-filter on ``applies_to`` — the backend owns the gate, and the
        empty-dict-means-skipped contract drives ``units_touched`` vs
        ``units_skipped``.

        Args:
            unit: the ``MigratableUnit`` handle for the file/row.
            script: the loaded migration script (a ``LoadedScript``
                instance carrying ``from_version``, ``to_version``, and
                ``module``). The backend calls
                ``script.module.applies_to(unit)`` and
                ``script.module.migrate(unit)`` — it owns the
                backend↔unit translation.
            dry_run: when ``True``, no state is written. The unit handle
                passed to the script has ``dry_run=True`` baked in so
                ``write_frontmatter()`` is a no-op.

        Returns:
            A summary dict contributed to ``MigrationResult.units_touched``.

        Raises:
            ``AtomicAgentsError`` when the script raises or when the
            unit's ``schema_version`` was not bumped to
            ``script.to_version`` after a non-dry-run apply.
        """
        ...

    def snapshot(self, target_version: int) -> MigrationSnapshotRef:
        """Create a recoverable checkpoint of the vault in its current state.

        Returns a ``MigrationSnapshotRef`` whose ``snapshot_id`` is
        backend-issued and opaque to callers. Pass the ref unchanged to
        ``restore()`` to recover.

        ``target_version`` is the version the migration is heading TO; a
        backend MAY use it to label the checkpoint (the filesystem backend
        names the tarball ``..._pre_v{target_version}_migration.tar.gz`` so
        the recovery artifact reads as "the backup taken before migrating
        to vN"). Backends with no human-facing snapshot name MAY ignore it.

        The implementation is free to choose any storage mechanism
        (tar+gzip for the filesystem backend; pg_dump for Postgres).
        The runner never references tar files, snapshot directories, or
        ``_migrations/``.

        MUST be called BEFORE any ``apply_unit()`` call in a real
        migration run. The runner MUST emit the snapshot ref to STDOUT
        immediately after this call so operators can recover manually
        if the process is killed mid-migration.

        Raises:
            ``AtomicAgentsError`` on failure to create the checkpoint.
        """
        ...

    def restore(self, ref: MigrationSnapshotRef) -> None:
        """Restore the vault to the state captured in ``ref``.

        MUST be an atomic, all-or-nothing operation: the vault either
        ends up in the pre-migration state or remains in the current
        (partially-migrated) state. Partial restores that leave the
        vault in an incoherent intermediate state are a spec violation.

        The ``ref.backend_id`` MUST match ``self.backend_id``; if it
        does not, raise ``AtomicAgentsError`` with a message explaining
        the mismatch.

        Args:
            ref: the ``MigrationSnapshotRef`` returned by a prior
                ``snapshot()`` call on this backend instance (or a
                compatible instance with the same ``agents_root``).

        Raises:
            ``AtomicAgentsError`` when the snapshot does not exist, is
            corrupt, or belongs to a different backend.
            ``MigrationSnapshotNotFound`` when the snapshot_id is not
            known to this backend instance.
        """
        ...

    def run_migration(
        self,
        target_version: int,
        dry_run: bool = True,
    ) -> MigrationResult:
        """Run a full migration to ``target_version``.

        This is the top-level entry point called by the CLI and by
        programmatic callers. It orchestrates:

        1. Pre-flight: check ``supports_transactional_rollback`` — refuse
           if ``False`` and ``dry_run=False``.
        2. Build plan: read the current schema version (a read-only vault
           probe) + ``_discover_scripts`` (the version chain).
        3. Acquire a vault-level exclusive lock so concurrent runs are
           serialized.
        4. Authoritative enumeration (``enumerate_units``) AFTER the lock —
           the apply-time unit set is read under the lock that serializes
           concurrent mutators. (The read-only version probe in step 2 runs
           before the lock; that is intentional so a lock-busy refusal still
           records the resolved ``from_version`` rather than the ``-1``
           sentinel — see ``run_migration`` in the filesystem realization.)
        5. Snapshot (real runs only): call ``snapshot()`` and emit the
           ref to STDOUT.
        6. Apply: iterate scripts × units; call ``apply_unit`` for
           matching pairs.
        7. Validate: check all enumerable units pass the target-version
           schema validators.
        8. On validation failure or exception: call ``restore(ref)`` and
           set ``result.rolled_back=True``.
        9. Emit a ``MigrationEvent`` to the audit log.

        HARD RULES (per spec/03):
        - Snapshot ALWAYS taken before applying (unless dry_run).
        - Validation runs ONCE after all scripts are applied, against EVERY
          enumerable unit (not only the touched subset), then rolls back if
          any unit fails — the all-or-nothing / no-half-migrated-vault gate.
        - Rollback if validation fails.
        - All-or-nothing: any unit's failure rolls back the whole vault.
        - Audit event emitted on EVERY exit (success, failure, or early
          pre-flight refusal), isolated from lock-release failures.

        Args:
            target_version: the target ``schema_version`` integer.
            dry_run: when ``True``, no state is written; no snapshot is
                taken.

        Returns:
            ``MigrationResult`` describing what happened.
        """
        ...

    def restore_and_audit(self, ref: MigrationSnapshotRef) -> None:
        """Restore from ``ref`` AND durably record a ``MigrationEvent`` (MUST 8).

        This is the operator-facing manual-rollback entrypoint reached by
        ``python -m atomic_agents.migrate --rollback``. Unlike the bare
        ``restore()`` primitive (the in-run rollback whose audit line is the
        owning ``run_migration()`` exit record), ``restore_and_audit()`` MUST
        emit its own ``MigrationEvent`` — a manual rollback is a destructive
        recovery action and is precisely the event an operator most wants a
        forensic record of.

        On success the event carries ``rolled_back=True`` (pre-rollback
        ``from_version`` → post-rollback ``to_version``); on failure it carries
        ``rolled_back=False`` + the error string and the underlying exception
        re-raises, so a failed manual rollback stays queryable. The audit line
        MUST be written even if a post-restore version read fails — a
        completed destructive restore must never leave no audit record.

        Every conforming backend MUST implement this so the CLI's audited
        manual-rollback path works against any backend, not just filesystem.

        Raises:
            Whatever ``restore()`` raises (``MigrationSnapshotNotFound``,
            ``AtomicAgentsError``, …), after the failure audit line is written.
        """
        ...

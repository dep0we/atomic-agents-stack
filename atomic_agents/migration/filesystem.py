"""FilesystemMigrationBackend — reference implementation over today's vault-walk.

This backend wraps the existing tar+swap snapshot logic and vault-walk discovery
behind the ``MigrationBackend`` Protocol so the runner is fully backend-neutral.

Key behaviors:
- ``enumerate_units()`` walks ``<agents_root>/<agent>/{memory,wiki}/`` recursively,
  deduplicates by resolved path, and returns ``MigratableUnit`` handles with
  ``unit_type`` set from the directory name.
- ``read_schema_version()`` walks ALL units and returns the minimum
  ``schema_version`` seen (no 50-file sample cap — full walk is correct).
- ``snapshot(target_version)`` tar+gzips the vault to
  ``<agents_root>/_migrations/snapshots/<timestamp>_pre_v{target_version}_migration.tar.gz``
  using an atomic temp-then-rename pattern, and returns a ``MigrationSnapshotRef``.
  ``vN`` is the version being migrated TO (the run's target).
- ``restore(ref)`` extracts the snapshot atomically via the sibling-staging swap
  pattern (preserving existing snapshot tarballs across the restore).
- ``apply_unit()`` calls the script's ``applies_to(unit)`` / ``migrate(unit)``
  and asserts the schema_version was bumped to ``script.to_version`` afterward.
- ``run_migration()`` is the top-level orchestration method: pre-flight →
  plan → snapshot → apply → validate → emit audit event.

Audit: every ``run_migration()`` call (dry-run or real) appends one
``MigrationEvent`` JSONL line to ``<agents_root>/_migrations/migration.jsonl``.
This file is excluded from vault content walks and preserved across
snapshot/restore cycles.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import frontmatter as _frontmatter

from .._io import atomic_append_jsonl, atomic_write
from .._schema import (
    CURRENT_SCHEMA_VERSION,
    validate_atomic_note_frontmatter,
    validate_wiki_frontmatter,
)
from ..exceptions import AtomicAgentsError, SchemaValidationError
from .types import (
    MigratableUnit,
    MigrationCapabilities,
    MigrationEvent,
    MigrationPlan,
    MigrationResult,
    MigrationSnapshotRef,
)

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# Constants (canonical home; migrate.py imports the CLI surface from
# .migration and does not redefine these)

VERSION_RE = re.compile(r"^v?(\d+)$")
SCRIPT_NAME_RE = re.compile(r"^v(\d+)_to_v(\d+)\.py$")

EXCLUDED_DIRS = {
    "_dashboard",
    "_migrations",
    "_cache",
    "node_modules",
    ".git",
    ".pytest_cache",
    "__pycache__",
}
AGENT_CONTENT_DIRS = {"memory", "wiki"}

# Audit-log filename under _migrations/ — excluded from the snapshot
# (it is separately re-copied into staging during restore() so the audit
# history survives a rollback without being captured in the tarball).
MIGRATION_AUDIT_LOG_NAME = "migration.jsonl"


# ──────────────────────────────────────────────────────────────────
# Migration exceptions


class MigrationRollbackUnavailable(AtomicAgentsError):
    """Runner refused a destructive migration because the backend does not support rollback.

    The runner MUST refuse a destructive (non-dry-run) migration on a backend
    advertising ``supports_transactional_rollback=False``. Operators should
    either use ``--dry-run`` or switch to a backend that supports rollback.
    """


class MigrationSnapshotNotFound(AtomicAgentsError):
    """``restore(ref)`` referenced an unknown snapshot.

    Raised when the ``snapshot_id`` in the ref does not correspond to a
    known snapshot tarball under the backend's snapshots directory.
    """


# ──────────────────────────────────────────────────────────────────
# Script discovery types


@dataclass
class LoadedScript:
    """A discovered migration script with metadata."""

    path: Path
    from_version: int
    to_version: int
    module: Any  # imported module


# ──────────────────────────────────────────────────────────────────
# FilesystemMigrationBackend


class FilesystemMigrationBackend:
    """Filesystem reference implementation of the ``MigrationBackend`` Protocol.

    Conforms to ``MigrationBackend`` structurally (no subclassing). Bound to
    ``agents_root`` at construction; all snapshot_ids are relative to the
    backend's ``<agents_root>/_migrations/snapshots/`` directory and are only
    valid on the same ``agents_root``. Passing a snapshot ref from a different
    root raises ``MigrationSnapshotNotFound``.

    Thread-safety: not thread-safe. The migration runner acquires a vault-level
    exclusive lock via ``FilesystemLockBackend`` before any mutation to prevent
    concurrent migration runs.

    Args:
        agents_root: root of the vault. All agent directories live under here.
        today: override for date in snapshot filenames (testing only).
        _now: override for datetime in snapshot filenames (testing only).
    """

    @property
    def backend_id(self) -> str:
        return "filesystem"

    def __init__(
        self,
        agents_root: Path,
        *,
        today: date | None = None,
        _now: datetime.datetime | None = None,
    ) -> None:
        self._agents_root = Path(agents_root)
        self._today_override = today
        self._now_override = _now

    # ──────────────────────────────────────────────────────────────────
    # Protocol surface

    def capabilities(self) -> MigrationCapabilities:
        """Filesystem capabilities — supports transactional rollback, single-host."""
        return MigrationCapabilities(
            supports_transactional_rollback=True,
            single_host_only=True,
        )

    def read_schema_version(self) -> int:
        """Return the minimum schema_version across ALL enumerable units.

        Walks all units via ``enumerate_units()`` — no 50-file sample cap.
        Returns ``CURRENT_SCHEMA_VERSION`` for an empty vault.
        """
        units = self.enumerate_units()
        if not units:
            return CURRENT_SCHEMA_VERSION

        versions: set[int] = set()
        unparseable = 0
        malformed_version = 0
        for unit in units:
            try:
                meta = unit.read_frontmatter()
            except Exception:
                unparseable += 1
                continue
            v = meta.get("schema_version")
            if isinstance(v, int):
                versions.add(v)
            else:
                # Parsed cleanly but schema_version is missing or non-int (e.g.
                # the YAML string "1"). Count it so an all-malformed-but-
                # parseable vault still surfaces the diagnostic below rather
                # than silently masking as already-current.
                malformed_version += 1

        if not versions:
            # Units exist but none yielded an integer schema_version. If that
            # is because every unit failed to parse OR carried a malformed
            # (missing / non-int) schema_version, returning CURRENT would
            # falsely report the vault as healthy / already-current and mask
            # corruption from _build_plan and vault_status. Surface it loudly;
            # the int contract still holds (callers treat CURRENT as "no
            # forward migration available", which is correct for an
            # all-corrupt vault — there is nothing safe to migrate).
            if unparseable or malformed_version:
                _logger.warning(
                    "read_schema_version: %d unit(s) present but none yielded a "
                    "valid integer schema_version (%d unparseable, %d malformed "
                    "schema_version) under %s. Reporting CURRENT_SCHEMA_VERSION; "
                    "inspect the vault for corrupt frontmatter before migrating.",
                    len(units),
                    unparseable,
                    malformed_version,
                    self._agents_root,
                )
            return CURRENT_SCHEMA_VERSION
        return min(versions)

    def enumerate_units(self) -> list[MigratableUnit]:
        """Walk the vault and return all migratable units.

        Deduplicates by resolved path. Each unit has ``unit_type`` set
        from the directory name (``'memory'`` or ``'wiki'``).
        Excludes ``_dashboard``, ``_migrations``, ``_cache``, dotfile dirs,
        and ``INDEX.md``.
        """
        return list(self._iter_units(dry_run=False))

    def apply_unit(
        self,
        unit: MigratableUnit,
        script: LoadedScript,
        dry_run: bool = True,
    ) -> dict:
        """Apply one migration script to one unit.

        Constructs a fresh ``MigratableUnit`` handle with ``dry_run`` baked
        in, calls ``script.module.applies_to(unit)`` and
        ``script.module.migrate(unit)``, then verifies (on real runs) that
        the unit's ``schema_version`` was bumped to ``script.to_version``.

        Returns a summary dict for ``MigrationResult.units_touched``.
        """
        # Rebuild the unit handle with the correct dry_run flag
        scoped_unit = self._make_unit(
            path=Path(unit.unit_id),
            unit_type=unit.unit_type,
            dry_run=dry_run,
        )

        try:
            applies = script.module.applies_to(scoped_unit)
        except Exception as exc:
            raise AtomicAgentsError(
                f"Script {script.path.name} applies_to({unit.unit_id!r}) raised: {exc}"
            ) from exc

        if not applies:
            return {}

        try:
            summary = script.module.migrate(scoped_unit)
        except Exception as exc:
            raise AtomicAgentsError(
                f"Script {script.path.name} migrate({unit.unit_id!r}) raised: {exc}"
            ) from exc

        # On real runs, verify the script actually bumped schema_version
        if not dry_run:
            actual_meta = scoped_unit.read_frontmatter()
            actual_version = actual_meta.get("schema_version")
            if actual_version != script.to_version:
                raise AtomicAgentsError(
                    f"Migration script {script.path.name!r} did not bump schema_version "
                    f"on {unit.unit_id!r} — expected {script.to_version}, got {actual_version!r}. "
                    f"Ensure the script calls unit.write_frontmatter(meta) with the new version."
                )

        # A falsy return from migrate() (None or {}) is the *skip* signal
        # (spec/03 §Return-value contract): the runner counts it toward
        # units_skipped, not units_touched. Decide skip on the RAW return
        # BEFORE injecting provenance keys — injecting "script"/"unit_id" into
        # an empty dict would make a documented skip read as truthy/touched.
        if not summary:
            return {}
        if isinstance(summary, dict):
            summary["script"] = script.path.name
            summary["unit_id"] = unit.unit_id
        return summary

    def snapshot(self, target_version: int) -> MigrationSnapshotRef:
        """Create a snapshot tarball of the vault.

        Writes atomically: the tarball is built at a ``.tmp`` sibling, then
        fsync'd (file fd + parent-dir fd) and ``os.rename``'d into place, so a
        partial or interrupted write never leaves a corrupt tar at the final
        path. Returns a ``MigrationSnapshotRef`` whose ``snapshot_id`` is the
        tarball filename (stable, human-displayable).

        The filename embeds ``target_version`` — the version the migration
        is heading TO — so the artifact reads as "the backup taken before
        migrating to vN" (e.g. ``..._pre_v2_migration.tar.gz`` for a run
        to v2). This matches the spec/03 recovery examples.
        """
        now = self._now_override or datetime.datetime.now()
        today = self._today_override or now.date()
        time_str = now.strftime("%H%M%S")

        snapshots_dir = self._agents_root / "_migrations" / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)

        filename = (
            f"{today.isoformat()}T{time_str}_pre_v{target_version}_migration.tar.gz"
        )
        snapshot_path = snapshots_dir / filename
        # Append ``.tmp`` to the FULL name — Path.with_suffix would replace only
        # the last suffix (``.gz``), yielding a malformed doubled-``.tar`` name
        # (``..._migration.tar.tar.gz.tmp``). A crash mid-snapshot would then
        # leave an orphan invisible to list_snapshots()'s ``*.tar.gz`` glob.
        tmp_path = snapshots_dir / (filename + ".tmp")

        try:
            with tarfile.open(str(tmp_path), "w:gz") as tar:
                for child in self._agents_root.iterdir():
                    # Handle _migrations FIRST — before the EXCLUDED_DIRS skip.
                    # _migrations IS in EXCLUDED_DIRS (so the agent-content
                    # filter drops it), but we DO want its top-level scripts +
                    # README in the snapshot so a rollback restores the
                    # operator's migration scripts — spec/03 §Rollback step 5
                    # "Retry with corrected migration" assumes the scripts still
                    # exist after restore. We exclude only the snapshots/ subdir
                    # (would nest snapshots inside snapshots) and the append-only
                    # audit log (separately re-copied during restore). The
                    # explicit adds use the dedicated _migrations_tar_filter, NOT
                    # self._tar_filter, since the latter would reject every
                    # _migrations member.
                    if child == self._agents_root / "_migrations":
                        if not child.is_dir():
                            continue
                        for item in child.iterdir():
                            if item.name == "snapshots":
                                continue
                            if item.name == MIGRATION_AUDIT_LOG_NAME:
                                continue
                            tar.add(
                                str(item),
                                arcname=f"_migrations/{item.name}",
                                filter=self._migrations_tar_filter,
                            )
                        continue
                    if child.name in EXCLUDED_DIRS:
                        continue
                    if child.name.startswith("."):
                        continue
                    tar.add(str(child), arcname=child.name, filter=self._tar_filter)

            # fsync the finished tarball + parent dir before the atomic rename
            # so the snapshot is durable on disk — matches MUST 3's atomic-write
            # discipline for unit writes (temp + fsync + rename + dir fsync).
            tmp_fd = os.open(str(tmp_path), os.O_RDONLY)
            try:
                os.fsync(tmp_fd)
            finally:
                os.close(tmp_fd)
            os.rename(str(tmp_path), str(snapshot_path))
            dir_fd = os.open(str(snapshots_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

        return MigrationSnapshotRef(
            backend_id=self.backend_id,
            snapshot_id=filename,
        )

    def restore(self, ref: MigrationSnapshotRef) -> None:
        """Restore the vault from the snapshot identified by ``ref``.

        Uses the sibling-staging atomic swap: extract into a sibling temp
        directory, copy existing snapshots into staging so they survive the
        swap, rename live vault aside, rename staging into place. On failure
        before the swap, the live vault is left completely untouched.

        Raises ``MigrationSnapshotNotFound`` when the snapshot_id is unknown.
        Raises ``AtomicAgentsError`` for backend_id mismatch, corrupt tarballs,
        absolute-path or path-traversal members.
        """
        if ref.backend_id != self.backend_id:
            raise AtomicAgentsError(
                f"MigrationSnapshotRef has backend_id={ref.backend_id!r} but this "
                f"backend has backend_id={self.backend_id!r}. Cannot restore a "
                f"snapshot from a different backend type."
            )

        snapshot_path = self._snapshots_dir / ref.snapshot_id
        if not snapshot_path.exists():
            raise MigrationSnapshotNotFound(
                f"Snapshot {ref.snapshot_id!r} not found at {snapshot_path}. "
                f"This snapshot_id is only valid for agents_root={self._agents_root}."
            )

        parent = self._agents_root.parent
        tmp_dir = Path(
            tempfile.mkdtemp(
                prefix=f"_restore-tmp-{uuid.uuid4().hex[:8]}-",
                dir=str(parent),
            )
        )

        try:
            # 1. Validate member paths before extracting anything
            with tarfile.open(str(snapshot_path), "r:gz") as tar:
                self._validate_tar_members(tar)

            # 2. Extract into staging area
            with tarfile.open(str(snapshot_path), "r:gz") as tar:
                tar.extractall(path=str(tmp_dir), filter="data")

            # 3. Verify staging has content
            if not any(tmp_dir.iterdir()):
                raise AtomicAgentsError(
                    f"Snapshot {ref.snapshot_id!r} extracted empty. Refusing restore."
                )

            # 4. Re-create _migrations/snapshots/ in staging so snapshots
            #    survive the atomic swap
            staging_snapshots = tmp_dir / "_migrations" / "snapshots"
            staging_snapshots.mkdir(parents=True, exist_ok=True)

            # Copy existing snapshots into staging
            if self._snapshots_dir.exists():
                for snap in self._snapshots_dir.iterdir():
                    dest = staging_snapshots / snap.name
                    if not dest.exists():
                        if snap.is_file():
                            shutil.copy2(str(snap), str(dest))
                        else:
                            shutil.copytree(str(snap), str(dest))

            # Copy migration.jsonl into staging so audit log survives
            live_mig_jsonl = self._agents_root / "_migrations" / "migration.jsonl"
            if live_mig_jsonl.exists():
                staging_mig = tmp_dir / "_migrations" / "migration.jsonl"
                if not staging_mig.exists():
                    shutil.copy2(str(live_mig_jsonl), str(staging_mig))

            # 5. Atomic swap: rename live aside, staging into place
            aside_dir = parent / f"_restore-aside-{uuid.uuid4().hex[:8]}"
            os.rename(str(self._agents_root), str(aside_dir))
            try:
                os.rename(str(tmp_dir), str(self._agents_root))
            except Exception:
                # Undo the aside rename so the live vault is left intact
                os.rename(str(aside_dir), str(self._agents_root))
                raise

            # Delete the aside (old live content) — best effort
            try:
                shutil.rmtree(str(aside_dir))
            except OSError as exc:
                _logger.warning(
                    "Could not clean up aside directory %s after restore: %s. "
                    "This directory is safe to delete manually.",
                    aside_dir,
                    exc,
                )

        except Exception:
            if tmp_dir.exists():
                shutil.rmtree(str(tmp_dir), ignore_errors=True)
            raise

    def restore_and_audit(self, ref: MigrationSnapshotRef) -> None:
        """Restore from ``ref`` and durably record a ``MigrationEvent`` (MUST 8).

        This is the operator-facing manual-rollback entrypoint (called by
        ``python -m atomic_agents.migrate --rollback``). A manual rollback is a
        destructive recovery action — precisely the event an operator most wants
        a forensic record of — so it MUST leave an audit line just like a
        ``run_migration()`` exit does. The bare ``restore()`` method does NOT
        emit (it is the in-run rollback primitive, audited by the
        ``run_migration()`` exit path); ``restore_and_audit()`` is the only
        restore path that should be reached from the CLI.

        Reads the schema version before and after so the audit line records the
        true ``from_version`` → ``to_version`` of the rollback. On success the
        event carries ``rolled_back=True``; on failure it carries
        ``rolled_back=False`` + the error string and the exception re-raises, so
        a failed manual rollback is also queryable.

        Raises whatever ``restore()`` raises (``MigrationSnapshotNotFound``,
        ``AtomicAgentsError``, etc.) after the audit line is written.
        """
        run_id = str(uuid.uuid4())
        # Symmetric with the post-restore read below: read_schema_version()
        # walks the whole vault (rglob + path.resolve()) and can raise on a
        # transient FS error / broken symlink. A flaky pre-restore read MUST
        # NOT abort the operator-facing rollback before the restore is even
        # attempted — degrade to the -1 sentinel and proceed.
        try:
            from_version = self.read_schema_version()
        except Exception as exc:
            _logger.warning(
                "Pre-restore read_schema_version() failed (%s); recording "
                "from_version=-1 sentinel and continuing with the rollback.",
                exc,
            )
            from_version = -1
        try:
            self.restore(ref)
        except Exception as exc:
            # Record the failed rollback before re-raising so it stays queryable.
            self._emit_audit(
                run_id=run_id,
                from_version=from_version,
                to_version=from_version,
                units_touched=0,
                dry_run=False,
                rolled_back=False,
                error=f"Manual rollback failed: {exc}",
            )
            raise
        # The destructive restore SUCCEEDED. The audit line (MUST 8) must not
        # hinge on the follow-up read: read_schema_version() walks the whole
        # restored vault (rglob + path.resolve()) and can raise on transient FS
        # errors / a broken symlink. If it does, degrade to_version to the -1
        # sentinel rather than letting a post-restore read failure suppress the
        # forensic record of a completed rollback.
        try:
            to_version = self.read_schema_version()
        except Exception as exc:
            _logger.warning(
                "Manual rollback succeeded but post-restore read_schema_version() "
                "failed (%s); recording to_version=-1 sentinel in the audit line.",
                exc,
            )
            to_version = -1
        self._emit_audit(
            run_id=run_id,
            from_version=from_version,
            to_version=to_version,
            units_touched=0,
            dry_run=False,
            rolled_back=True,
            error="",
        )

    def run_migration(
        self,
        target_version: int,
        dry_run: bool = True,
    ) -> MigrationResult:
        """Run a full migration to ``target_version``.

        Orchestrates: pre-flight → lock → plan → snapshot → apply → validate
        → unlock → emit audit event.

        HARD RULES (per spec/03 Implementer Contract):
        - Fail-close (MUST 5): refuse destructive migration if backend does
          not support rollback.
        - Snapshot (MUST 4) emitted to STDOUT before any writes.
        - Validation (spec/03 §Validation) runs once after all scripts are
          applied, against EVERY enumerable unit (not just the touched
          subset), then rolls back if any unit fails — the
          no-half-migrated-vault gate.
        - Rollback on any failure.
        - All-or-nothing: any unit's failure rolls back the whole vault.
        - Audit (MUST 8): one ``MigrationEvent`` is appended on EVERY exit —
          success, failure, or pre-plan refusal — isolated from lock-release
          failures so an abnormal unlock never swallows the durable record.
        """
        from ..locks import FilesystemLockBackend
        from ..exceptions import LockBusy

        # run_id + audit skeleton are created FIRST so even a pre-plan failure
        # (no-rollback refusal, broken script chain, target-below-current)
        # still produces an audit line with the sentinel ``from_version = -1``.
        # ``from_version`` is unknown until the plan is built (step 2) — record
        # -1 as the sentinel and fill the real value once available. NOTE:
        # lock-busy is NOT a pre-plan sentinel case — the lock is acquired at
        # step 3, AFTER the plan is built at step 2, so a LockBusy refusal
        # records the resolved ``from_version``, not -1 (spec/03 MUST 8).
        run_id = str(uuid.uuid4())
        from_version = -1
        to_version = target_version
        result: MigrationResult | None = None
        emit_error = ""

        try:
            # ── 1. Pre-flight capability check (MUST 5) ─────────────────────
            caps = self.capabilities()
            if not dry_run and not caps.supports_transactional_rollback:
                raise MigrationRollbackUnavailable(
                    f"MigrationBackend {self.backend_id!r} does not support "
                    f"transactional rollback. Refusing destructive migration. "
                    f"Use --dry-run to preview, or provide a backend with "
                    f"supports_transactional_rollback=True."
                )

            # ── 2. Build plan ───────────────────────────────────────────────
            plan = self._build_plan(target_version)
            from_version = plan.from_version
            to_version = plan.to_version

            # ── 3. Acquire vault-level exclusive lock ───────────────────────
            lock_backend = FilesystemLockBackend(self._agents_root)
            try:
                lock_handle = lock_backend.acquire("migration", timeout=0)
            except LockBusy:
                raise AtomicAgentsError(
                    "Another migration is already running on this vault "
                    f"({self._agents_root}). Wait for it to complete or "
                    "remove the stale lock at "
                    f"{self._agents_root / '.migration.lock'}."
                )

            snapshot_ref: MigrationSnapshotRef | None = None
            result = MigrationResult(
                plan=plan,
                snapshot_ref=None,
                dry_run=dry_run,
            )

            try:
                # ── 4. Enumerate units AFTER lock acquired ──────────────────
                units = self._iter_units(dry_run=dry_run)

                # ── 5. Snapshot (real runs only) ────────────────────────────
                if not dry_run:
                    snapshot_ref = self.snapshot(plan.to_version)
                    result.snapshot_ref = snapshot_ref
                    print(
                        f"Snapshot created: {self._snapshots_dir / snapshot_ref.snapshot_id}\n"
                        f"If this migration is interrupted, run: "
                        f"python -m atomic_agents.migrate --rollback {snapshot_ref.snapshot_id}",
                        flush=True,
                    )

                # ── 6. Apply ────────────────────────────────────────────────
                for script in plan.scripts:
                    for unit in units:
                        summary = self.apply_unit(unit, script, dry_run=dry_run)
                        if summary:
                            result.units_touched.append(summary)
                        else:
                            result.units_skipped += 1

                # ── 7. Post-migration validation (real runs only) ───────────
                # Validate EVERY enumerable unit against the target schema,
                # not only the units a script chose to touch. A script whose
                # applies_to() skips a unit leaves that unit at the old
                # version; the all-or-nothing contract (spec/03 §Multi-agent:
                # "Half-migrated state ... is forbidden") requires the skipped
                # unit be caught here, not silently passed.
                if not dry_run:
                    errors = self._validate_units(units, plan.to_version)
                    if errors:
                        result.validation_errors = errors
                        self._do_rollback(result, snapshot_ref)
                        # Only overwrite result.error with the reassuring
                        # "rolled back" message when rollback actually
                        # succeeded. If restore() failed, _do_rollback set the
                        # critical "inconsistent state" message (with the
                        # snapshot id + restore root cause) — preserve it so
                        # the durable audit record reflects the true outcome.
                        if result.rolled_back:
                            result.error = (
                                f"Post-migration validation failed for {len(errors)} unit(s). "
                                f"Vault rolled back to snapshot."
                            )
                        else:
                            # Rollback failed: prepend the validation cause to
                            # the inconsistent-state message _do_rollback wrote.
                            result.error = (
                                f"Post-migration validation failed for {len(errors)} unit(s). "
                                f"{result.error}"
                            )
                        return result

                result.validation_passed = True
                return result

            except Exception as exc:
                # Catastrophic failure during apply/validate — try rollback.
                if not dry_run and snapshot_ref is not None:
                    self._do_rollback(result, snapshot_ref)
                    if result.rolled_back:
                        result.error = str(exc)
                    else:
                        # Rollback failed: preserve the "inconsistent state"
                        # message _do_rollback wrote (snapshot id + restore
                        # error) and prepend the original failure cause.
                        result.error = f"Migration error: {exc}\n{result.error}"
                else:
                    # No snapshot taken (dry-run or pre-snapshot failure) —
                    # nothing to roll back; record the raw error.
                    result.error = str(exc)
                return result

            finally:
                # Release the lock in its own isolated try so a release
                # failure cannot suppress the audit emit in the outer finally.
                try:
                    lock_backend.release(lock_handle)
                except Exception as exc:
                    _logger.warning(
                        "Migration lock release failed (run_id=%s): %s", run_id, exc
                    )

        except Exception as exc:
            # Pre-plan / pre-lock failure: no MigrationResult was built.
            # Re-raise after the audit line is written by the outer finally.
            emit_error = str(exc)
            raise

        finally:
            # ── 8. Emit audit event on EVERY exit, isolated from everything
            #       above. _emit_audit swallows its own write errors.
            self._emit_audit(
                run_id=run_id,
                from_version=from_version,
                to_version=to_version,
                units_touched=(len(result.units_touched) if result else 0),
                dry_run=dry_run,
                rolled_back=(result.rolled_back if result else False),
                error=(result.error if result else emit_error),
            )

    # ──────────────────────────────────────────────────────────────────
    # Snapshot listing

    def list_snapshots(self) -> list[Path]:
        """All snapshot tarballs under _migrations/snapshots/, newest first."""
        if not self._snapshots_dir.exists():
            return []
        return sorted(
            self._snapshots_dir.glob("*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def vault_status(self) -> dict:
        """Report current schema version + available scripts + snapshots."""
        current = self.read_schema_version()
        scripts = self._discover_scripts()
        snapshots = self.list_snapshots()
        units = self.enumerate_units()

        return {
            "agents_root": str(self._agents_root),
            "current_schema_version": current,
            "current_helper_version": CURRENT_SCHEMA_VERSION,
            "needs_migration": current < CURRENT_SCHEMA_VERSION,
            "content_file_count": len(units),
            "available_scripts": [
                {"from": s.from_version, "to": s.to_version, "path": str(s.path)}
                for s in scripts
            ],
            "snapshots": [{"name": s.name, "path": str(s)} for s in snapshots],
        }

    # ──────────────────────────────────────────────────────────────────
    # Internal helpers

    @property
    def _snapshots_dir(self) -> Path:
        return self._agents_root / "_migrations" / "snapshots"

    def _make_unit(
        self,
        path: Path,
        unit_type: str,
        dry_run: bool,
    ) -> MigratableUnit:
        """Construct a MigratableUnit handle bound to a filesystem path."""

        def _read() -> dict:
            # Use frontmatter.parse(), NOT frontmatter.load(): parse() returns
            # (metadata, content) without constructing a Post, so a frontmatter
            # field literally named "handler" or "content" round-trips cleanly.
            # frontmatter.load() splats **metadata into Post(content, handler,
            # **metadata) and raises TypeError on those keys — which would make
            # any unit carrying such a custom field unreadable and silently
            # masked as "unparseable" by read_schema_version(). The contract
            # (spec/03 §"preserves unknown fields") promises arbitrary custom
            # fields are preserved, so the read path must not collide on them.
            metadata, _content = _frontmatter.parse(path.read_text(encoding="utf-8"))
            return dict(metadata)

        def _write(metadata: dict) -> None:
            # Read current body to preserve it. Same parse() rationale as _read:
            # avoid the Post-kwarg collision on a "handler"/"content" field.
            _metadata, body = _frontmatter.parse(path.read_text(encoding="utf-8"))
            # Build new content with updated metadata. Set metadata via the
            # attribute rather than splatting **metadata into Post(...): Post's
            # signature is (content, handler=None, **metadata), so a frontmatter
            # field literally named "handler" would be swallowed by the handler
            # parameter and a field named "content" would raise. Assigning
            # post.metadata preserves arbitrary custom keys verbatim — the
            # migration contract promises custom fields are preserved.
            new_post = _frontmatter.Post(body)
            new_post.metadata = dict(metadata)
            content = _frontmatter.dumps(new_post)
            if not content.endswith("\n"):
                content += "\n"
            atomic_write(path, content)

        return MigratableUnit(
            unit_id=str(path),
            unit_type=unit_type,  # type: ignore[arg-type]
            dry_run=dry_run,
            _read_fn=_read,
            _write_fn=_write,
        )

    def _iter_units(self, *, dry_run: bool) -> list[MigratableUnit]:
        """Walk the vault and yield MigratableUnit handles.

        Deduplicates by resolved path. Sets unit_type from directory name.
        """
        if not self._agents_root.exists():
            return []

        seen: set[Path] = set()
        units: list[MigratableUnit] = []

        def _is_excluded(path: Path) -> bool:
            try:
                rel = path.relative_to(self._agents_root)
            except ValueError:
                return False
            return any(
                part in EXCLUDED_DIRS or part.startswith(".") for part in rel.parts
            )

        for content_dir_name in AGENT_CONTENT_DIRS:
            for content_dir in self._agents_root.rglob(content_dir_name):
                if not content_dir.is_dir():
                    continue
                if _is_excluded(content_dir):
                    continue
                unit_type = "wiki" if content_dir_name == "wiki" else "memory"
                for path in content_dir.rglob("*.md"):
                    if path.name == "INDEX.md":
                        continue
                    if _is_excluded(path):
                        continue
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    units.append(self._make_unit(path, unit_type, dry_run=dry_run))

        return units

    def _discover_scripts(self) -> list[LoadedScript]:
        """Find all migration scripts under <agents_root>/_migrations/."""
        migrations_dir = self._agents_root / "_migrations"
        if not migrations_dir.exists():
            return []

        scripts: list[LoadedScript] = []
        for path in sorted(migrations_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            m = SCRIPT_NAME_RE.match(path.name)
            if not m:
                continue
            from_version = int(m.group(1))
            to_version = int(m.group(2))
            if to_version != from_version + 1:
                raise AtomicAgentsError(
                    f"Migration script {path.name} skips a version "
                    f"(v{from_version} → v{to_version}). Migrations must be sequential."
                )
            try:
                module = self._load_module(path)
            except Exception as exc:
                raise AtomicAgentsError(f"Failed to load {path.name}: {exc}") from exc

            for attr in ("FROM_VERSION", "TO_VERSION", "applies_to", "migrate"):
                if not hasattr(module, attr):
                    raise AtomicAgentsError(
                        f"Migration script {path.name} missing required attribute: {attr}"
                    )
            if module.FROM_VERSION != from_version or module.TO_VERSION != to_version:
                raise AtomicAgentsError(
                    f"Migration script {path.name} version mismatch: filename says "
                    f"v{from_version} → v{to_version} but module says "
                    f"v{module.FROM_VERSION} → v{module.TO_VERSION}"
                )
            scripts.append(
                LoadedScript(
                    path=path,
                    from_version=from_version,
                    to_version=to_version,
                    module=module,
                )
            )

        scripts.sort(key=lambda s: s.from_version)
        return scripts

    @staticmethod
    def _load_module(path: Path) -> Any:
        """Import a Python file as a module."""
        spec = importlib.util.spec_from_file_location(
            f"_atomic_agents_migration_{path.stem}", str(path)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _build_plan(self, target_version: int) -> MigrationPlan:
        """Discover scripts and build a migration plan."""
        current = self.read_schema_version()
        if target_version <= current:
            raise AtomicAgentsError(
                f"Target version v{target_version} is not above current v{current}. "
                f"Forward-only migrations."
            )

        all_scripts = self._discover_scripts()
        chain: list[LoadedScript] = []
        expected_from = current
        for s in all_scripts:
            if s.from_version < current:
                continue
            if s.from_version > target_version:
                break
            if s.from_version != expected_from:
                raise AtomicAgentsError(
                    f"Migration chain broken: expected v{expected_from} → ... but "
                    f"next script is v{s.from_version} → v{s.to_version}. "
                    f"Add the missing script."
                )
            chain.append(s)
            expected_from = s.to_version
            if expected_from == target_version:
                break

        if expected_from < target_version:
            raise AtomicAgentsError(
                f"No migration script for v{expected_from} → v{target_version}. "
                f"Found chain: {[(s.from_version, s.to_version) for s in chain]}"
            )

        return MigrationPlan(
            from_version=current,
            to_version=target_version,
            scripts=chain,
        )

    def _validate_units(
        self,
        units: list[MigratableUnit],
        target_version: int,
    ) -> list[dict]:
        """Validate every unit passes the target-version schema. Returns errors.

        Deduplicates by ``unit_id`` first so a unit matched by multiple
        scripts in a chain is validated (and any error reported) exactly once.
        """
        seen: set[str] = set()
        errors: list[dict] = []
        for unit in units:
            if unit.unit_id in seen:
                continue
            seen.add(unit.unit_id)
            try:
                meta = unit.read_frontmatter()
            except Exception as exc:
                errors.append({"unit_id": unit.unit_id, "error": f"unparseable: {exc}"})
                continue
            # _validate_frontmatter_at_version dispatches on unit_type
            # internally (wiki vs note); no branch needed here.
            try:
                _validate_frontmatter_at_version(
                    meta, unit.unit_type, target_version, unit.unit_id
                )
            except SchemaValidationError as exc:
                errors.append({"unit_id": unit.unit_id, "error": str(exc)})
            except Exception as exc:
                errors.append(
                    {"unit_id": unit.unit_id, "error": f"validation crashed: {exc}"}
                )
        return errors

    def _do_rollback(
        self,
        result: MigrationResult,
        snapshot_ref: MigrationSnapshotRef | None,
    ) -> None:
        """Attempt rollback. Sets result.rolled_back on success."""
        if snapshot_ref is None:
            return
        try:
            self.restore(snapshot_ref)
            result.rolled_back = True
        except Exception as exc:
            result.error = (
                f"Migration failed AND rollback failed. Vault is in an "
                f"inconsistent state. Snapshot: {snapshot_ref.snapshot_id}\n"
                f"Rollback error: {exc}"
            )

    def _emit_audit(
        self,
        *,
        run_id: str,
        from_version: int,
        to_version: int,
        units_touched: int,
        dry_run: bool,
        rolled_back: bool,
        error: str,
    ) -> None:
        """Append one MigrationEvent JSONL line to the audit log.

        Routed through ``_io.atomic_append_jsonl`` (flush + ``os.fsync``), the
        same durable-append primitive LogBackend uses, so the forensic record
        of a destructive migration/rollback survives an OS-level crash in the
        sub-second window after the vault has already been mutated on disk
        (Principle #8; matches the snapshot() fsync discipline + MUST 8's
        "durably record" contract).
        """
        event = MigrationEvent(
            run_id=run_id,
            # tz-aware ISO 8601 matching the LogBackend audit convention
            # (logs/filesystem.py) — a naive timestamp on a destructive-change
            # forensic record is un-orderable across DST / hosts.
            timestamp=datetime.datetime.now().astimezone().isoformat(),
            agents_root=str(self._agents_root),
            from_version=from_version,
            to_version=to_version,
            units_touched=units_touched,
            dry_run=dry_run,
            rolled_back=rolled_back,
            error=error,
        )
        log_path = self._agents_root / "_migrations" / "migration.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_append_jsonl(
                log_path,
                json.dumps(
                    {
                        "run_id": event.run_id,
                        "timestamp": event.timestamp,
                        "agents_root": event.agents_root,
                        "from_version": event.from_version,
                        "to_version": event.to_version,
                        "units_touched": event.units_touched,
                        "dry_run": event.dry_run,
                        "rolled_back": event.rolled_back,
                        "error": event.error,
                    }
                ),
            )
        except Exception as exc:
            _logger.warning("Could not write migration audit log: %s", exc)

    @staticmethod
    def _tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """Exclude caches, logs, and snapshots from the snapshot tarball.

        Applied to the agent-content adds (everything OUTSIDE ``_migrations``).
        The ``_migrations`` tree itself is captured by a separate branch in
        ``snapshot()`` using ``_migrations_tar_filter`` — do NOT route
        ``_migrations`` members through this filter, since ``_migrations`` is in
        ``EXCLUDED_DIRS`` and would be dropped entirely.
        """
        parts = tarinfo.name.replace("\\", "/").split("/")
        for part in parts:
            if part in EXCLUDED_DIRS or part == "snapshots":
                return None
        # Exclude per-agent run logs. The real on-disk shape is sharded:
        # ``<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl`` (see logs/filesystem.py), so
        # the immediate parent of a log file is the ``YYYY-MM`` month dir, NOT
        # ``log``. We therefore exclude any ``.jsonl`` that has a ``log``
        # ANCESTOR at any depth (``log`` anywhere but the leaf), which catches
        # both the flat ``<agent>/log/x.jsonl`` and the sharded
        # ``<agent>/log/YYYY-MM/x.jsonl`` layouts. Restricting to ``.jsonl`` (the
        # only thing run logs are written as) means a ``.md`` content unit — even
        # under an agent legitimately named ``log`` — is never dropped, since
        # enumerate_units only ever produces ``*.md`` units.
        if parts[-1].endswith(".jsonl") and "log" in parts[:-1]:
            return None
        return tarinfo

    @staticmethod
    def _migrations_tar_filter(
        tarinfo: tarfile.TarInfo,
    ) -> tarfile.TarInfo | None:
        """Filter for the explicit ``_migrations/*`` adds in ``snapshot()``.

        Allows ``_migrations`` top-level files (the operator's migration
        scripts + README) so a rollback restores them, while still rejecting
        the ``snapshots/`` subdir and the append-only audit log. This is the
        deliberate complement to ``_tar_filter``, which blanket-excludes the
        whole ``_migrations`` dir for the agent-content adds.
        """
        parts = tarinfo.name.replace("\\", "/").split("/")
        for part in parts:
            if part == "snapshots":
                return None
            if part == MIGRATION_AUDIT_LOG_NAME:
                return None
        return tarinfo

    @staticmethod
    def _validate_tar_members(tar: tarfile.TarFile) -> None:
        """Refuse any snapshot with absolute paths or path-traversal sequences."""
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or name.startswith("\\"):
                raise AtomicAgentsError(
                    f"Unsafe snapshot: member {name!r} has an absolute path. Refusing restore."
                )
            if ".." in name.replace("\\", "/").split("/"):
                raise AtomicAgentsError(
                    f"Unsafe snapshot: member {name!r} contains '..' path traversal. Refusing restore."
                )


# ──────────────────────────────────────────────────────────────────
# Validation helper that accepts a target version parameter


def _validate_frontmatter_at_version(
    meta: dict,
    unit_type: str,
    target_version: int,
    unit_id: str,
) -> None:
    """Delegate to the standard target-aware frontmatter validators.

    IMPORTANT — this helper does NOT bypass the version cliff. The standard
    validators (``validate_atomic_note_frontmatter`` /
    ``validate_wiki_frontmatter``) reject any ``schema_version`` that is not
    equal to the package-global ``CURRENT_SCHEMA_VERSION``. So when a real
    migration targets a version the package has not yet adopted
    (``target_version != CURRENT_SCHEMA_VERSION``), post-migration validation
    WILL fail and the runner WILL roll back — by design. Bumping
    ``CURRENT_SCHEMA_VERSION`` in ``atomic_agents/_schema.py`` (so the
    validators recognize the new shape) is a PREREQUISITE for a real
    cross-version migration to complete end-to-end.

    ``target_version`` is accepted so the Protocol can evolve to genuinely
    version-parameterized validators later; today it is used only to emit a
    diagnostic warning when it diverges from ``CURRENT_SCHEMA_VERSION``.
    """
    if target_version != CURRENT_SCHEMA_VERSION:
        # Warn that the standard validators will reject schema_version !=
        # CURRENT. Bumping CURRENT_SCHEMA_VERSION in _schema.py is required
        # alongside any real schema migration; until then a real migration
        # to this target validates-and-rolls-back by design.
        warnings.warn(
            f"Post-migration validation for unit {unit_id!r}: "
            f"target_version={target_version} != CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}. "
            f"The standard validators will reject schema_version={target_version}. "
            f"Bump CURRENT_SCHEMA_VERSION in atomic_agents/_schema.py alongside schema changes.",
            stacklevel=4,
        )

    if unit_type == "wiki":
        validate_wiki_frontmatter(meta, filename=Path(unit_id).name)
    else:
        validate_atomic_note_frontmatter(meta, filename=Path(unit_id).name)


# ──────────────────────────────────────────────────────────────────
# Convenience: parse version string from CLI


def parse_target_version(s: str) -> int:
    """Parse '--to v2' or '--to 2' into the integer 2."""
    m = VERSION_RE.match(s.strip())
    if not m:
        raise AtomicAgentsError(f"Invalid version format: {s!r} — expected 'v2' or '2'")
    return int(m.group(1))

"""Schema migration runner — safe upgrade path when schema_version bumps.

Per the spec at docs/spec/03-file-formats.md §Schema-migration.

### BREAKING change (issue #429) — T13 migration-runner refactor

The path-shaped ``MigrationScript`` Protocol (``applies_to(path: Path)`` /
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

Usage:

    # Dry-run first — mandatory before real migration
    python -m atomic_agents.migrate --to v2 --dry-run

    # Real migration (creates snapshot, applies, validates, rolls back if invalid)
    python -m atomic_agents.migrate --to v2

    # Status: which schema version is the vault at?
    python -m atomic_agents.migrate --status

    # Rollback to a specific snapshot
    python -m atomic_agents.migrate --rollback 2026-08-12T143000_pre_v2_migration.tar.gz

    # List available snapshots
    python -m atomic_agents.migrate --list-snapshots

This module is the CLI entrypoint (``python -m atomic_agents.migrate``).
Implementation is delegated to ``atomic_agents.migration.FilesystemMigrationBackend``.
See spec/03 §Schema-migration for the full normative contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ._platform import get_agents_root
from .exceptions import AtomicAgentsError
from .migration import (
    FilesystemMigrationBackend,
    MigrationSnapshotRef,
    parse_target_version,
)


# ──────────────────────────────────────────────────────────────────
# CLI


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="atomic-agents.migrate",
        description="Schema migration runner for Atomic Agents vaults",
    )
    parser.add_argument(
        "--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT"
    )
    parser.add_argument(
        "--to",
        default=None,
        metavar="VERSION",
        help="target schema version (e.g., 'v2' or '2')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't write changes; print what would happen",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show vault status (current version, scripts, snapshots)",
    )
    parser.add_argument(
        "--rollback",
        default=None,
        metavar="SNAPSHOT",
        help=(
            "restore from a snapshot (looked up by filename under "
            "_migrations/snapshots/; a full path is accepted but only its "
            "filename is used)"
        ),
    )
    parser.add_argument(
        "--list-snapshots",
        action="store_true",
        help="list available snapshots",
    )

    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root
        else get_agents_root()
    )

    backend = FilesystemMigrationBackend(agents_root)

    try:
        if args.status:
            return _cmd_status(backend)
        if args.list_snapshots:
            return _cmd_list_snapshots(backend)
        if args.rollback:
            return _cmd_rollback(backend, args.rollback)
        if args.to:
            return _cmd_migrate(backend, args.to, dry_run=args.dry_run)

        parser.print_help()
        return 1
    except AtomicAgentsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _cmd_status(backend: FilesystemMigrationBackend) -> int:
    status = backend.vault_status()
    print(f"Vault: {status['agents_root']}")
    print(f"Current schema version: v{status['current_schema_version']}")
    print(f"Helper supports: v{status['current_helper_version']}")
    if status["needs_migration"]:
        print(f"Needs migration to v{status['current_helper_version']}")
    print(f"Content files: {status['content_file_count']}")
    print(f"Migration scripts: {len(status['available_scripts'])}")
    for s in status["available_scripts"]:
        print(f"  v{s['from']} -> v{s['to']}  ({s['path']})")
    print(f"Snapshots: {len(status['snapshots'])}")
    for s in status["snapshots"][:5]:
        print(f"  {s['name']}")
    return 0


def _cmd_list_snapshots(backend: FilesystemMigrationBackend) -> int:
    import datetime

    snapshots = backend.list_snapshots()
    if not snapshots:
        print("No snapshots.")
        return 0
    for s in snapshots:
        size_kb = s.stat().st_size // 1024
        mtime = datetime.datetime.fromtimestamp(s.stat().st_mtime).isoformat()
        print(f"  {s.name}  ({size_kb} KB, {mtime})")
    return 0


def _cmd_rollback(backend: FilesystemMigrationBackend, snapshot_arg: str) -> int:
    # Allow either bare filename or full path
    snapshot_path = Path(snapshot_arg)
    if not snapshot_path.is_absolute():
        # Construct the MigrationSnapshotRef from the bare filename
        ref = MigrationSnapshotRef(
            backend_id=backend.backend_id,
            snapshot_id=snapshot_arg,
        )
    else:
        # Full path — use the filename as snapshot_id
        ref = MigrationSnapshotRef(
            backend_id=backend.backend_id,
            snapshot_id=snapshot_path.name,
        )

    print(f"Rolling back from: {snapshot_arg}")
    try:
        # restore_and_audit (not bare restore): a manual rollback is a
        # destructive recovery action and MUST leave a MigrationEvent audit
        # line so the rollback stays queryable (spec/03 MUST 8). A failed
        # rollback is also recorded before the exception surfaces here.
        backend.restore_and_audit(ref)
    except Exception as exc:
        print(f"Rollback failed: {exc}", file=sys.stderr)
        return 1
    print("Rollback complete.")
    return 0


def _cmd_migrate(
    backend: FilesystemMigrationBackend,
    to_arg: str,
    dry_run: bool,
) -> int:
    target = parse_target_version(to_arg)
    print(f"{'DRY-RUN' if dry_run else 'MIGRATING'}: -> v{target}")

    result = backend.run_migration(target_version=target, dry_run=dry_run)

    print(f"\nFrom v{result.plan.from_version} -> v{result.plan.to_version}")
    print(f"Scripts in chain: {len(result.plan.scripts)}")
    for s in result.plan.scripts:
        print(f"  {s.path.name}  (v{s.from_version} -> v{s.to_version})")
    print(f"Units touched:    {len(result.units_touched)}")
    print(f"Units skipped:    {result.units_skipped}")

    if not dry_run and result.snapshot_ref is not None:
        print(f"\nSnapshot: {result.snapshot_ref.snapshot_id}")

    if result.error:
        print(f"\nError: {result.error}", file=sys.stderr)
        if result.rolled_back:
            print("Vault rolled back to snapshot.", file=sys.stderr)
        else:
            print(
                "Rollback NOT performed — vault may be in inconsistent state.",
                file=sys.stderr,
            )
        return 1

    if result.validation_errors:
        print(
            f"\nValidation errors after migration ({len(result.validation_errors)}):",
            file=sys.stderr,
        )
        for err in result.validation_errors[:5]:
            print(f"  {err.get('unit_id', '?')}: {err['error']}", file=sys.stderr)
        return 1

    if dry_run:
        print("\nDry-run complete. Re-run without --dry-run to apply.")
    else:
        print("\nMigration complete; validation passed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Schema migration runner — safe upgrade path when schema_version bumps.

Per the spec at <vault>/Atomic Agents/spec/03-file-formats.md (the
"Schema migration" section).

When `schema_version` on atomic notes / wiki pages needs to bump (new
required field, renamed field, type change), this is the safe upgrade
path. Backup before, dry-run preview, validate after, atomic rollback
on validation failure.

Usage:

    # Dry-run first — mandatory before real migration
    python -m atomic_agents.migrate --to v2 --dry-run

    # Real migration (creates snapshot, applies, validates, rolls back if invalid)
    python -m atomic_agents.migrate --to v2

    # Status: which schema version is the vault at?
    python -m atomic_agents.migrate --status

    # Rollback to a specific snapshot
    python -m atomic_agents.migrate --rollback 2026-08-12_pre_v2_migration.tar.gz

Migration script protocol — each script in `<agents_root>/_migrations/`
implements:

    FROM_VERSION = 1
    TO_VERSION = 2

    def applies_to(path: Path) -> bool:
        '''Return True if this script should touch `path`.'''
        ...

    def migrate(path: Path, dry_run: bool) -> dict:
        '''Apply the migration to one file.

        Returns a summary dict like:
            {"path": str(path), "changes": [...], "dry_run": dry_run}

        When dry_run=True, the script must NOT write anything; just
        compute and return what it would do.
        '''
        ...

The runner discovers scripts in `<agents_root>/_migrations/*.py`, sorts
by version chain, and applies them.

HARD RULES (per spec/03):
- Forward-only (no downgrades)
- Multi-agent atomicity (all-or-nothing across the vault)
- Snapshot before, validate after, rollback if invalid
- Custom user-added frontmatter fields are preserved (migrations only
  touch what the spec defines)
"""

from __future__ import annotations
import datetime
import importlib.util
import json
import re
import shutil
import sys
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

import frontmatter

from ._platform import get_agents_root
from ._schema import CURRENT_SCHEMA_VERSION, validate_atomic_note_frontmatter
from .exceptions import (
    AtomicAgentsError,
    SchemaValidationError,
)


VERSION_RE = re.compile(r"^v?(\d+)$")
SCRIPT_NAME_RE = re.compile(r"^v(\d+)_to_v(\d+)\.py$")
SNAPSHOT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_pre_v(\d+)_migration\.tar\.gz$")

# When walking the vault for migration, these directories are skipped
EXCLUDED_DIRS = {
    "_dashboard",
    "_migrations",
    "_cache",
    "node_modules",
    ".git",
    ".pytest_cache",
    "__pycache__",
}
# These file types are walked (anything else is ignored)
INCLUDED_SUFFIXES = {".md"}
# Within an agent, only these subdirs hold frontmatter content
AGENT_CONTENT_DIRS = {"memory", "wiki"}


# ──────────────────────────────────────────────────────────────────
# Migration script protocol

class MigrationScript(Protocol):
    """The shape every vN_to_vM.py script in _migrations/ must implement."""
    FROM_VERSION: int
    TO_VERSION: int

    def applies_to(self, path: Path) -> bool: ...
    def migrate(self, path: Path, dry_run: bool) -> dict[str, Any]: ...


@dataclass
class LoadedScript:
    """A discovered migration script with metadata."""
    path: Path
    from_version: int
    to_version: int
    module: Any   # imported module


@dataclass
class MigrationPlan:
    """What the runner intends to do."""
    from_version: int
    to_version: int
    scripts: list[LoadedScript] = field(default_factory=list)
    candidate_files: list[Path] = field(default_factory=list)


@dataclass
class MigrationResult:
    """Outcome of a migration run."""
    plan: MigrationPlan
    snapshot_path: Path | None
    files_touched: list[dict] = field(default_factory=list)  # per-file summaries
    files_skipped: int = 0
    validation_passed: bool = False
    validation_errors: list[dict] = field(default_factory=list)
    rolled_back: bool = False
    dry_run: bool = False
    error: str = ""


# ──────────────────────────────────────────────────────────────────
# Script discovery

def discover_scripts(agents_root: Path) -> list[LoadedScript]:
    """Find all migration scripts under <agents_root>/_migrations/.

    Validates filename pattern (vN_to_vM.py), imports each as a module,
    and verifies it has the required FROM_VERSION + TO_VERSION + functions.

    Returns list sorted by from_version.
    """
    migrations_dir = agents_root / "_migrations"
    if not migrations_dir.exists():
        return []

    scripts: list[LoadedScript] = []
    for path in sorted(migrations_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue  # _template.py, __init__.py
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
            module = _load_module(path)
        except Exception as e:
            raise AtomicAgentsError(f"Failed to load {path.name}: {e}") from e

        # Verify the module has the required attributes
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

        scripts.append(LoadedScript(
            path=path,
            from_version=from_version,
            to_version=to_version,
            module=module,
        ))

    # Sort by from_version (ascending)
    scripts.sort(key=lambda s: s.from_version)
    return scripts


def _load_module(path: Path) -> Any:
    """Import a Python file as a module, isolated by path.stem."""
    spec = importlib.util.spec_from_file_location(
        f"_atomic_agents_migration_{path.stem}", str(path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ──────────────────────────────────────────────────────────────────
# Vault file discovery

def find_content_files(agents_root: Path) -> list[Path]:
    """Walk the vault and return all *.md files in agent content dirs.

    Skips _dashboard, _migrations, _cache, and other meta dirs.
    Within each agent, only walks memory/, wiki/, and similar content dirs.
    Skips INDEX.md and other non-frontmatter files.
    """
    files: list[Path] = []
    if not agents_root.exists():
        return files

    for agent_dir in agents_root.iterdir():
        if not agent_dir.is_dir():
            continue
        if agent_dir.name in EXCLUDED_DIRS or agent_dir.name.startswith("."):
            continue

        for content_dir_name in AGENT_CONTENT_DIRS:
            content_dir = agent_dir / content_dir_name
            if not content_dir.exists():
                continue
            for path in content_dir.rglob("*.md"):
                if path.name == "INDEX.md":
                    continue
                if any(part in EXCLUDED_DIRS for part in path.parts):
                    continue
                files.append(path)

    return files


# ──────────────────────────────────────────────────────────────────
# Snapshot

def create_snapshot(agents_root: Path, target_version: int, today: date | None = None) -> Path:
    """Tar+gzip the entire vault (excluding caches, logs, snapshots) to
    `<agents_root>/_migrations/snapshots/YYYY-MM-DD_pre_vN_migration.tar.gz`.

    Returns the snapshot path.
    """
    today = today or date.today()
    snapshots_dir = agents_root / "_migrations" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = snapshots_dir / f"{today.isoformat()}_pre_v{target_version}_migration.tar.gz"

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # Use forward slashes for tar paths (cross-platform)
        parts = tarinfo.name.replace("\\", "/").split("/")
        for part in parts:
            if part in EXCLUDED_DIRS or part == "snapshots":
                return None
            # Skip log JSONLs (they regenerate)
            if part.endswith(".jsonl") and "log" in parts:
                return None
        return tarinfo

    with tarfile.open(snapshot_path, "w:gz") as tar:
        # Add agents_root contents (not the directory itself), with arcname relative
        for child in agents_root.iterdir():
            if child.name in EXCLUDED_DIRS:
                continue
            if child.name.startswith("."):
                continue
            # Skip the snapshots dir specifically
            if child == agents_root / "_migrations":
                # Include _migrations/*.py scripts (so rollback restores them)
                # but exclude _migrations/snapshots/
                for script in child.iterdir():
                    if script.name == "snapshots":
                        continue
                    tar.add(str(script), arcname=f"_migrations/{script.name}", filter=_filter)
                continue
            tar.add(str(child), arcname=child.name, filter=_filter)

    return snapshot_path


def restore_snapshot(agents_root: Path, snapshot_path: Path) -> None:
    """Extract a snapshot back over the vault.

    Atomic-ish: clears the to-be-restored content first, then extracts.
    If extract fails midway, the vault is in a half-state — operator
    intervention required (the partial extraction is preserved for inspection).
    """
    if not snapshot_path.exists():
        raise AtomicAgentsError(f"Snapshot not found: {snapshot_path}")

    # Clear out content that will be replaced (don't touch _migrations/snapshots/)
    for child in agents_root.iterdir():
        if child.name in EXCLUDED_DIRS or child.name.startswith("."):
            continue
        if child == agents_root / "_migrations":
            # Replace migration scripts but keep snapshots/
            for script in list(child.iterdir()):
                if script.name == "snapshots":
                    continue
                if script.is_file():
                    script.unlink()
                else:
                    shutil.rmtree(script)
            continue
        if child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)

    # Extract snapshot
    with tarfile.open(snapshot_path, "r:gz") as tar:
        tar.extractall(path=str(agents_root), filter="data")


# ──────────────────────────────────────────────────────────────────
# Application

def get_current_vault_version(agents_root: Path) -> int:
    """Read schema_version from the first content file we can find.

    If no content exists (empty vault), returns CURRENT_SCHEMA_VERSION.
    If files have mixed versions, returns the lowest (treat as needing migration).
    """
    files = find_content_files(agents_root)
    if not files:
        return CURRENT_SCHEMA_VERSION

    versions: set[int] = set()
    for path in files[:50]:  # sample first 50 to avoid full walk
        try:
            parsed = frontmatter.load(path)
            v = parsed.metadata.get("schema_version")
            if isinstance(v, int):
                versions.add(v)
        except Exception:
            continue

    if not versions:
        return CURRENT_SCHEMA_VERSION
    return min(versions)


def build_migration_plan(
    agents_root: Path, target_version: int,
) -> MigrationPlan:
    """Plan: which scripts run, against which files."""
    current = get_current_vault_version(agents_root)
    if target_version <= current:
        raise AtomicAgentsError(
            f"Target version v{target_version} is not above current v{current}. "
            f"Forward-only migrations."
        )

    all_scripts = discover_scripts(agents_root)

    # Filter scripts to the chain from current → target
    chain: list[LoadedScript] = []
    expected_from = current
    for s in all_scripts:
        if s.from_version < current:
            continue
        if s.from_version > target_version:
            break
        if s.from_version != expected_from:
            raise AtomicAgentsError(
                f"Migration chain broken: expected v{expected_from} → ... but next "
                f"script is v{s.from_version} → v{s.to_version}. "
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

    candidate_files = find_content_files(agents_root)

    return MigrationPlan(
        from_version=current,
        to_version=target_version,
        scripts=chain,
        candidate_files=candidate_files,
    )


def run_migration(
    agents_root: Path | None = None,
    target_version: int = 0,
    dry_run: bool = True,
    today: date | None = None,
) -> MigrationResult:
    """Run a migration. Returns MigrationResult.

    HARD RULES enforced here:
    - Snapshot ALWAYS taken before applying (unless dry_run)
    - Validation runs after every script application
    - Rollback if validation fails
    - All-or-nothing: any file's failure rolls back the whole vault
    """
    agents_root = agents_root or get_agents_root()
    today = today or date.today()

    plan = build_migration_plan(agents_root, target_version)

    snapshot_path: Path | None = None
    if not dry_run:
        snapshot_path = create_snapshot(agents_root, target_version, today=today)

    result = MigrationResult(
        plan=plan,
        snapshot_path=snapshot_path,
        dry_run=dry_run,
    )

    try:
        # Apply each script's migration to all matching files in order
        for script in plan.scripts:
            for path in plan.candidate_files:
                try:
                    if not script.module.applies_to(path):
                        result.files_skipped += 1
                        continue
                except Exception as e:
                    raise AtomicAgentsError(
                        f"Script {script.path.name} applies_to({path}) raised: {e}"
                    ) from e
                try:
                    summary = script.module.migrate(path, dry_run=dry_run)
                except Exception as e:
                    raise AtomicAgentsError(
                        f"Script {script.path.name} migrate({path}) raised: {e}"
                    ) from e
                if isinstance(summary, dict):
                    summary["script"] = script.path.name
                    result.files_touched.append(summary)

        # Post-migration validation (only for real runs; dry-run files weren't touched)
        if not dry_run:
            errors = _validate_post_migration(plan.candidate_files)
            if errors:
                result.validation_errors = errors
                # Rollback
                if snapshot_path is not None:
                    restore_snapshot(agents_root, snapshot_path)
                    result.rolled_back = True
                result.error = (
                    f"Post-migration validation failed for {len(errors)} files. "
                    f"Vault rolled back to snapshot."
                )
                return result

        result.validation_passed = True
        return result

    except Exception as e:
        # Catastrophic failure during migration — try to rollback
        if not dry_run and snapshot_path is not None:
            try:
                restore_snapshot(agents_root, snapshot_path)
                result.rolled_back = True
            except Exception:
                # Rollback also failed — vault is in an unknown state
                result.error = (
                    f"Migration failed AND rollback failed. Vault is in an "
                    f"inconsistent state. Snapshot at: {snapshot_path}\n"
                    f"Original error: {e}"
                )
                return result
        result.error = str(e)
        return result


def _validate_post_migration(files: list[Path]) -> list[dict]:
    """Validate every touched file passes the current schema. Returns list of errors."""
    errors: list[dict] = []
    for path in files:
        try:
            parsed = frontmatter.load(path)
        except Exception as e:
            errors.append({"path": str(path), "error": f"unparseable: {e}"})
            continue
        try:
            validate_atomic_note_frontmatter(dict(parsed.metadata), filename=path.name)
        except SchemaValidationError as e:
            errors.append({"path": str(path), "error": str(e)})
        except Exception as e:
            errors.append({"path": str(path), "error": f"validation crashed: {e}"})
    return errors


# ──────────────────────────────────────────────────────────────────
# Status + snapshot listing

def vault_status(agents_root: Path | None = None) -> dict:
    """Report current schema version + available scripts + snapshots."""
    agents_root = agents_root or get_agents_root()

    current = get_current_vault_version(agents_root)
    files = find_content_files(agents_root)
    scripts = discover_scripts(agents_root)
    snapshots = list_snapshots(agents_root)

    return {
        "agents_root": str(agents_root),
        "current_schema_version": current,
        "current_helper_version": CURRENT_SCHEMA_VERSION,
        "needs_migration": current < CURRENT_SCHEMA_VERSION,
        "content_file_count": len(files),
        "available_scripts": [
            {"from": s.from_version, "to": s.to_version, "path": str(s.path)}
            for s in scripts
        ],
        "snapshots": [
            {"name": s.name, "path": str(s)} for s in snapshots
        ],
    }


def list_snapshots(agents_root: Path) -> list[Path]:
    """All snapshot tarballs under _migrations/snapshots/, newest first."""
    snapshots_dir = agents_root / "_migrations" / "snapshots"
    if not snapshots_dir.exists():
        return []
    return sorted(
        snapshots_dir.glob("*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def parse_target_version(s: str) -> int:
    """Parse '--to v2' or '--to 2' into the integer 2."""
    m = VERSION_RE.match(s.strip())
    if not m:
        raise AtomicAgentsError(f"Invalid version format: '{s}' — expected 'v2' or '2'")
    return int(m.group(1))


# ──────────────────────────────────────────────────────────────────
# CLI

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="atomic-agents.migrate",
        description="Schema migration runner for Atomic Agents vaults",
    )
    parser.add_argument("--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT")
    sub = parser.add_subparsers(dest="cmd")

    # Default: --to vN flow
    parser.add_argument("--to", default=None, metavar="VERSION",
                        help="target schema version (e.g., 'v2' or '2')")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't write changes; print what would happen")
    parser.add_argument("--status", action="store_true",
                        help="show vault status (current version, scripts, snapshots)")
    parser.add_argument("--rollback", default=None, metavar="SNAPSHOT",
                        help="restore from snapshot filename or path")
    parser.add_argument("--list-snapshots", action="store_true",
                        help="list available snapshots")

    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root else get_agents_root()
    )

    try:
        if args.status:
            return _cmd_status(agents_root)
        if args.list_snapshots:
            return _cmd_list_snapshots(agents_root)
        if args.rollback:
            return _cmd_rollback(agents_root, args.rollback)
        if args.to:
            return _cmd_migrate(agents_root, args.to, dry_run=args.dry_run)

        parser.print_help()
        return 1
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _cmd_status(agents_root: Path) -> int:
    status = vault_status(agents_root)
    print(f"Vault: {status['agents_root']}")
    print(f"Current schema version: v{status['current_schema_version']}")
    print(f"Helper supports: v{status['current_helper_version']}")
    if status["needs_migration"]:
        print(f"⚠ Needs migration to v{status['current_helper_version']}")
    print(f"Content files: {status['content_file_count']}")
    print(f"Migration scripts: {len(status['available_scripts'])}")
    for s in status["available_scripts"]:
        print(f"  v{s['from']} → v{s['to']}  ({s['path']})")
    print(f"Snapshots: {len(status['snapshots'])}")
    for s in status["snapshots"][:5]:
        print(f"  {s['name']}")
    return 0


def _cmd_list_snapshots(agents_root: Path) -> int:
    snapshots = list_snapshots(agents_root)
    if not snapshots:
        print("No snapshots.")
        return 0
    for s in snapshots:
        size_kb = s.stat().st_size // 1024
        mtime = datetime.datetime.fromtimestamp(s.stat().st_mtime).isoformat()
        print(f"  {s.name}  ({size_kb} KB, {mtime})")
    return 0


def _cmd_rollback(agents_root: Path, snapshot_arg: str) -> int:
    # Allow either bare filename or full path
    snapshot_path = Path(snapshot_arg)
    if not snapshot_path.is_absolute():
        snapshot_path = agents_root / "_migrations" / "snapshots" / snapshot_arg
    if not snapshot_path.exists():
        print(f"Snapshot not found: {snapshot_path}", file=sys.stderr)
        return 1
    print(f"Rolling back from: {snapshot_path}")
    try:
        restore_snapshot(agents_root, snapshot_path)
    except Exception as e:
        print(f"Rollback failed: {e}", file=sys.stderr)
        return 1
    print("Rollback complete.")
    return 0


def _cmd_migrate(agents_root: Path, to_arg: str, dry_run: bool) -> int:
    target = parse_target_version(to_arg)
    print(f"{'DRY-RUN' if dry_run else 'MIGRATING'}: → v{target}")

    result = run_migration(agents_root, target_version=target, dry_run=dry_run)

    print(f"\nFrom v{result.plan.from_version} → v{result.plan.to_version}")
    print(f"Scripts in chain: {len(result.plan.scripts)}")
    for s in result.plan.scripts:
        print(f"  {s.path.name}  (v{s.from_version} → v{s.to_version})")
    print(f"Files considered: {len(result.plan.candidate_files)}")
    print(f"Files touched:    {len(result.files_touched)}")
    print(f"Files skipped:    {result.files_skipped}")

    if not dry_run:
        print(f"\nSnapshot: {result.snapshot_path}")

    if result.error:
        print(f"\n❌ {result.error}", file=sys.stderr)
        if result.rolled_back:
            print("✓ Vault rolled back to snapshot.", file=sys.stderr)
        else:
            print("⚠ Rollback NOT performed — vault may be in inconsistent state.",
                  file=sys.stderr)
        return 1

    if result.validation_errors:
        print(f"\n❌ Validation errors after migration ({len(result.validation_errors)}):",
              file=sys.stderr)
        for err in result.validation_errors[:5]:
            print(f"  {err['path']}: {err['error']}", file=sys.stderr)
        return 1

    if dry_run:
        print("\n✓ Dry-run complete. Re-run without --dry-run to apply.")
    else:
        print("\n✓ Migration complete; validation passed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

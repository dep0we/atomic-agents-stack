---
name: atomic-agents-migrate
description: Run vault schema migrations. Snapshots first, validates after, rolls back automatically on failure. Required when upgrading the package across a schema-version bump.
---

# atomic-agents-migrate

Run a vault-wide schema migration. The runner snapshots the entire vault before any changes, applies migration scripts in order, validates the result, and automatically rolls back to the snapshot on any failure.

**This is a destructive operation in spirit** — it rewrites memory and wiki note frontmatter across every agent. Snapshots make it reversible, but always read the dry-run output before applying.

## When to use

- The package upgrade notes say "schema bump from v1 to v2" or similar
- Loading an agent file fails with a `SchemaValidationError` mentioning a version mismatch
- The user explicitly asks to migrate / upgrade / restore

Do **not** run this preemptively. The current schema_version is set in `atomic_agents._schema.CURRENT_SCHEMA_VERSION`; if your vault matches, you don't need this.

## Subcommands

### `--status` — what version is the vault on?

```bash
python -m atomic_agents.migrate --status
```

Shows the lowest schema_version found across all agent files (mixed vaults are flagged), available migration scripts, and existing snapshots.

### `--dry-run --to <version>` — preview without changing anything

```bash
python -m atomic_agents.migrate --to v2 --dry-run
```

Reports: which files would change, which migration scripts would run, in what order. **Always run this first.**

### `--to <version>` — apply the migration

```bash
python -m atomic_agents.migrate --to v2
```

What happens:
1. Snapshot the entire vault (`<ATOMIC_AGENTS_ROOT>/_migrations/snapshots/<timestamp>.tar.gz`)
2. Walk the migration script chain from current version → target
3. Apply each script's `migrate(content_dict)` to every matching file
4. Validate every changed file against the post-migration schema
5. If validation fails anywhere → automatic rollback (full snapshot restore)
6. Print summary: files changed, files skipped, total time

### `--list-snapshots` — what rollback options exist?

```bash
python -m atomic_agents.migrate --list-snapshots
```

### `--rollback <snapshot>` — manually restore an old state

```bash
python -m atomic_agents.migrate --rollback 2026-05-06_143022.tar.gz
```

Use when you applied a migration successfully but want to undo it later (e.g., the new schema didn't work out).

## Safety properties

- **Migrations are append-only.** Each script declares `FROM_VERSION` and `TO_VERSION`; the runner walks the chain. You can't skip versions.
- **Snapshots are mandatory.** No way to apply a migration without one. Snapshot files are gzipped tarballs — small (~10s of KB for typical vaults) and quick.
- **Post-validation is mandatory.** Every changed file is re-parsed against the target schema; any failure rolls back the whole batch.
- **The package's `CURRENT_SCHEMA_VERSION` and the migration script ladder land together.** Until both are present, post-validation correctly rejects new-schema files and rolls back, so you can't silently end up with an unsupported vault.

## Common follow-ups

- "Did anything change?" → diff a few files against the snapshot tarball: `tar -tzf <snap.tar.gz> | head; tar -xzOf <snap.tar.gz> <path> | diff - <ATOMIC_AGENTS_ROOT>/<path>`
- "Did the rollback work?" → run `--status` again; should show the previous version
- "Clean up old snapshots" → they pile up in `_migrations/snapshots/`; delete by hand when comfortable, or rotate via cron

## Troubleshooting

- **"No migration path"** → there's no script chain from current to target. Either the script is missing or you're trying to skip versions.
- **"Snapshot exists but rollback failed"** → unusual; the snapshot is a tarball, you can manually restore: `tar -xzf <snap.tar.gz> -C <ATOMIC_AGENTS_ROOT>`
- **"Migration succeeded but agent runs still fail"** → check that you upgraded the package, not just the data. The package version and schema version need to match.

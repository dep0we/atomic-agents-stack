# Upgrade runbook

How to upgrade an `atomic-agents-stack` install from one tagged release to
the next without breaking your vault. Pair with
[`versioning.md`](versioning.md) — that doc explains *what* the version
numbers mean; this one explains *what to do* with them.

---

## TL;DR

```
# 1. Read what's coming — find the release's target schema vN if any
gh release view vX.Y.Z   # or open the release page in your browser

# 2. Pull
git fetch --tags && git checkout vX.Y.Z
uv sync --extra dev      # or: pip install -e '.[dev]' / pip install -e '.[openai]'

# 3. Migrate (only if release notes mention schema)
python -m atomic_agents.migrate --status              # what version am I on?
python -m atomic_agents.migrate --to vN --dry-run     # preview the plan
python -m atomic_agents.migrate --to vN               # apply

# 4. Verify (atomic-agents doctor is available in v0.10.0+, per issue #66)
atomic-agents doctor --agent <name>                   # exit 0 = ready

# 5. Restart any LaunchAgents / cron jobs / serve processes
launchctl unload ~/Library/LaunchAgents/com.atomic-agents.*.plist
launchctl load   ~/Library/LaunchAgents/com.atomic-agents.*.plist
```

If `doctor` exits 0, you are upgraded. If it exits non-zero, do not run
production agents until you have addressed every failed check —
`doctor` reports the literal command needed to fix each one.

> **Versions before v0.10.0** do not ship `atomic-agents doctor`. For those
> upgrades, replace step 4 with a manual sanity check: `atomic-agents info
> <name>` should print the agent's config without error, and a one-shot
> `atomic-agents run <name> --work-item "ping"` should succeed.

---

## 1. Read the release notes

Open the release in `gh` or on GitHub:

```
gh release view vX.Y.Z
```

The release notes are pulled from the release's `## [X.Y.Z]` section in
[`CHANGELOG.md`](../../CHANGELOG.md). Look for:

- **`### BREAKING`** — mandatory work. Stop here, read it carefully.
  Pre-1.0 these can appear under any Minor bump; per-1.0 only under a
  Major bump (see [versioning.md §Pre-1.0 caveat](versioning.md)).
- **`### Removed`** — anything you depend on that is gone. Same severity
  as `BREAKING` for callers.
- **`### Deprecated`** — still works, but on a clock. Schedule the work
  before the next Major.
- **`### Added`** / **`### Changed`** / **`### Fixed`** — informational;
  drop-in safe.

If the release contains no `### BREAKING` or `### Removed` callouts, it
is a drop-in upgrade and Steps 3 below is a no-op.

---

## 2. Update your install

Pick whichever install method you used originally:

```
# git checkout (developer-style install)
git fetch --tags
git checkout vX.Y.Z
uv sync --extra dev      # rebuilds the venv against the new lockfile

# pip from a tag (operator-style install)
pip install --upgrade 'atomic-agents-stack @ git+https://github.com/dep0we/atomic-agents-stack.git@vX.Y.Z'
```

Once PyPI publishing lands (out of scope for #68), the operator-style
install becomes `pip install --upgrade atomic-agents-stack==X.Y.Z`.

---

## 3. Run the migration runner (only if needed)

> **BREAKING (issue #429 — T13 refactor):** Migration scripts written against
> the old path-shaped contract (`applies_to(path: Path)` / `migrate(path: Path,
> dry_run: bool)`) must be rewritten to the new per-unit handle contract
> (`applies_to(unit: MigratableUnit)` / `migrate(unit: MigratableUnit)`). See
> `docs/spec/03-file-formats.md §Migration upgrade path` for the mapping table.
> The `python -m atomic_agents.migrate` CLI entrypoint is unchanged.

Schema changes are the most common reason an upgrade is not drop-in. Any
release that bumps `CURRENT_SCHEMA_VERSION` ships a corresponding
migration script — but the runner discovers scripts from
**`<agents_root>/_migrations/*.py`** in your vault, not from the
package. Copy the script(s) referenced in the release notes into that
directory before running migrate:

```
mkdir -p "$ATOMIC_AGENTS_ROOT/_migrations"
# Copy each vN_to_vM.py the release notes call out into _migrations/.
# E.g., from a checkout of the release tag:
cp docs/migrations/v1_to_v2.py "$ATOMIC_AGENTS_ROOT/_migrations/"
```

Until packaged-script discovery lands (issue TBD), this copy step is
operator-driven. The runner will print `No migration script` if it
can't find a script to bridge the version gap; that is the signal you
missed this step, not an actual no-op.

**The migrate runner requires `--to <version>`** for any actual migration
work — read the release notes for the target schema version (e.g. `v2`,
`v3`). Without `--to`, only `--status`, `--list-snapshots`, and
`--rollback` are usable.

**Always check `--status` first, then dry-run.** `--status` reports the
vault's current schema version; `--dry-run` (with `--to vN`) walks the
migration plan without writing anything.

```
python -m atomic_agents.migrate --status                # current version
python -m atomic_agents.migrate --to vN --dry-run       # preview plan
```

If `--status` reports the vault is already at `vN`, **skip the migrate
step entirely** — running `--to vN` against an already-current vault
exits 1 with `Target version vN is not above current vN. Forward-only
migrations.` The runner does not have a no-op path for "you're already
there"; checking `--status` first is the way to know.

If the dry-run prints a plan, review it, then run the real thing:

```
python -m atomic_agents.migrate --to vN
```

The runner takes a gzipped snapshot of every affected file under
`<vault>/_migrations/snapshots/<timestamp>_pre_v<target>_migration.tar.gz`
(e.g. `2026-08-12T143000_pre_v2_migration.tar.gz`) before any write. The
snapshot ref is printed to STDOUT immediately after creation so you can read
the rollback id.
Post-write validation re-parses every changed file against the target
schema; any failure rolls the whole batch back to the snapshot. The
migration is therefore atomic from the operator's point of view: either
the vault arrives at the new schema version, or it stays exactly as it
was.

If something goes wrong after the migration completes — say, an agent
behaves oddly under the new schema — roll back manually:

```
python -m atomic_agents.migrate --list-snapshots
python -m atomic_agents.migrate --rollback <snapshot-name>
```

Rollback restores the **vault contents** from the snapshot tarball — it
does NOT touch your installed package. The `CURRENT_SCHEMA_VERSION`
constant lives in `atomic_agents._schema` (in the installed package),
so after a rollback `migrate --status` will still report the new helper
version against your now-old vault content, and may insist on running
the migration again.

To fully unwind, do the rollback *and* pin to the prior package tag:

```
git checkout v<prior>
uv sync --extra dev   # or: pip install -e '.[dev]'
python -m atomic_agents.migrate --rollback <snapshot-name>
python -m atomic_agents.migrate --status   # should show old version, no diff
```

If you only want to roll back the vault and keep the new package
installed (e.g. you're investigating the failure interactively), that is
fine — just be aware that `--status` will read as "needs migration"
until you either re-migrate or pin back.

---

## 4. Verify the install

**v0.10.0+ — `atomic-agents doctor` (per issue #66, spec/27-doctor.md
once it lands):** verifies every install-time invariant in one shot —
env, Python, vault layout, provider keys, model + cost guardrails, MCP
servers, lock state, memory backend, write paths.

```
for AGENT in caldwell scout writer; do
  atomic-agents doctor --agent "$AGENT" || exit 1
done
```

Exit 0 = ready to schedule. Exit 1 = one or more checks failed; the
output prints the literal command needed to fix each one. Exit 2 = bug
in doctor itself; file an issue.

**Pre-v0.10.0 — manual sanity check:** doctor isn't available yet, so
run this loop instead. It catches the most common upgrade breakage
(persona/config didn't load, no API key, wrong model id) but not the
full set doctor would:

```
for AGENT in caldwell scout writer; do
  atomic-agents info "$AGENT"   || exit 1   # config parses
  atomic-agents run "$AGENT" --work-item "ping"   || exit 1
done
```

Once you upgrade to v0.10.0 or later, switch to the doctor invocation —
it covers strictly more.

---

## 5. Restart anything long-running

Doctor verifies state at-rest. Live processes (LaunchAgents, cron jobs,
`atomic-agents.dashboard serve`) keep running against the *old* install
until they restart.

**macOS — LaunchAgents:**

```
launchctl unload ~/Library/LaunchAgents/com.atomic-agents.run.<agent>.plist
launchctl load   ~/Library/LaunchAgents/com.atomic-agents.run.<agent>.plist
```

**Linux — cron:** the next scheduled run picks up the new code; nothing
to restart explicitly.

**Dashboard server (`python -m atomic_agents.dashboard serve`):** Ctrl-C
and re-launch. The HTML output is regenerable, so there is no state to
preserve.

---

## What if I am upgrading across multiple Minor versions?

`v0.1.0 → v0.4.0` works the same as a single hop: the migration runner
walks the script chain `from_current → to_target`, refusing to skip
versions. Run the dry-run, review every migration's name, then run the
real migration.

**One snapshot per `--to` invocation, not one per script.** The runner
takes a single tarball before applying the script chain; rollback
restores to that pre-`--to` state. There are no intermediate restore
points — the migration is all-or-nothing across the whole chain. If
you want per-step rollback, run `--to` once per intermediate version
yourself (e.g., `--to v2`, then `--to v3`, then `--to v4`); each
invocation produces its own snapshot.

The release notes for every Minor in the range should be read in order
— `### BREAKING` from any one of them applies to the cumulative
upgrade.

---

## What if doctor fails after migration?

The migration succeeded but the install is in a state doctor doesn't
like. Common causes:

- **`provider-keys`** — the new release dropped a provider you had
  configured, or added a provider you don't have a key for. Check the
  CHANGELOG `### Changed` section.
- **`model`** — the new release retired the model id in your `model.md`.
  Update it to the replacement listed in the release notes.
- **`vault`** — a new file is now required (e.g., a new role-level
  cascade file). Add it per the spec doc the release introduced.
- **`mcp`** — an MCP server's command path changed. Update `mcp.md`.

Address the failed check, re-run doctor, repeat until exit 0. Then
restart long-running processes (Step 5).

---

## Disaster recovery

If an upgrade goes badly wrong and rollback via the migration runner is
not enough:

1. Pin to the prior tag: `git checkout vX.Y.(Z-1)`
2. Reinstall: `uv sync --extra dev`
3. List + restore the most recent migration snapshot:
   ```
   python -m atomic_agents.migrate --list-snapshots
   python -m atomic_agents.migrate --rollback <snapshot-name>
   ```
4. Confirm the prior state is healthy. Use `atomic-agents doctor --agent <name>`
   if available (v0.10.0+); otherwise the manual check from §4 above
   (`info` + a `ping` `run` per agent).
5. File an issue at [dep0we/atomic-agents-stack](https://github.com/dep0we/atomic-agents-stack/issues/new)
   with the release tag, the failure mode, and the verification output.

The migration snapshot tarballs are not deleted automatically; they
accumulate under `<vault>/_migrations/snapshots/`. You can prune them
manually once you're confident the new release is stable.

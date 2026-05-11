# Disaster recovery runbook

What to do when something goes wrong on a running install. Organized by
**symptom first** — find the line that matches what you are seeing, follow the
recovery, verify with the command at the bottom of the section.

Pair with [`upgrading.md`](upgrading.md) (the upgrade pipeline) and
[`versioning.md`](versioning.md) (what counts as a breaking change). When the
trouble started during an upgrade, start in `upgrading.md` § Disaster recovery
and come back here if that is not enough.

---

## First response

Before anything else, run:

```bash
atomic-agents doctor --agent <name>
```

Doctor runs nine checks (`env`, `python`, `vault`, `provider-keys`, `model`,
`mcp`, `locks`, `memory-backend`, `write-paths`) and prints a literal fix-hint
under every failure. Most disasters are one of those nine; the section
[Doctor failures — what each check means](#doctor-failures--what-each-check-means)
below maps every failure to its recovery.

If doctor exits 0 but the agent still misbehaves, work through the
symptom-organized sections below.

If doctor itself exits 2 (`doctor crashed`), that is a bug in doctor — file
an issue with the crash output and skip ahead to
[Escalation](#escalation--when-the-runbook-does-not-help).

---

## 1. Stale lock — `AgentLockBusy` raised but no agent is running

**Symptom.** The runtime raises `AgentLockBusy: Agent lock at
.../<agent>/.lock held by another process; waited 0s` (or whatever
`wait_seconds` you passed). You are sure no agent is currently running.

### Diagnose

```bash
LOCK=<vault>/<agent>/.lock
lsof "$LOCK"           # who holds it? PID alive?
cat "$LOCK"             # the file records pid=<N> acquired=<unix-ts>
ps -p <pid-from-file>   # is that process still alive?
```

Three possible states:

- **`lsof` is empty.** The OS has already released the `flock` (the holder
  process exited). The `.lock` file persists harmlessly — it is just a
  named target for `flock` to attach to, not a record of who is holding it.
  **Retry should succeed.**
- **`lsof` returns a live process.** A real agent run is in flight. Wait
  for it, or — if it is genuinely stuck — kill it with `kill <pid>` (try
  `TERM` first, then `KILL`).
- **`lsof` returns a process that no longer exists.** Should not happen on
  POSIX — the kernel releases on `exit()`. If it does, the file descriptor
  is being kept alive by something unusual (a stale `nohup`, a daemon
  fork, a stuck `cron` child). Identify and kill the parent.

### Recover

```bash
# Case A: lock file exists but lsof is empty — just retry.
atomic-agents run <agent> --work-item "..."

# Case B: live holder you want to stop.
kill <pid>
# Then retry. The OS releases flock on process death; the file stays.

# Case C: file is in the way and you have confirmed no holder exists.
# Almost never needed. Only safe after lsof has confirmed no holder AND
# you have read the file contents to be sure you are not about to delete
# someone else's lock.
rm <vault>/<agent>/.lock
```

### Why the file persists after release

`AgentLock.release()` calls `flock(LOCK_UN)` and `close()` on the file
descriptor — it does **not** `unlink()` the lock file. That is intentional.
The file is the kernel's target for `flock()`; if you delete it while
another process holds an open fd on it, that process keeps its lock but
the next process creates a fresh inode and gets a *different* lock on the
same path. Two processes, two locks, no mutual exclusion. So we leave the
file in place and only the `flock` state matters.

This means **a stale `.lock` file on disk is the normal state between
runs.** Doctor's `locks` check tests *whether the flock is currently
held*, not whether the file exists.

### Verify

```bash
atomic-agents doctor --agent <name>
# locks check should pass with: "lock file present but not held (clean)"
```

---

## 2. Mid-run crash — process killed during `agent.call()`

**Symptom.** The host crashed, was force-rebooted, or the agent process
was `kill -9`'d in the middle of a run. You are restarting; you want to
know what state the vault is in.

### What state could be partial

Almost nothing. Every write in the package goes through
`atomic_agents._io.atomic_write` — temp file + fsync + rename + parent-dir
fsync. POSIX makes the rename atomic, so each target file is either
fully-written-and-fsynced or absent. There is no torn-write state for any
single file.

What can linger after a crash:

- **Temp files.** A `.<filename>.<random>.tmp` may exist next to a target
  file if the crash happened between temp-write and rename. These are
  harmless and recoverable. Clean with
  `atomic_agents._io.cleanup_stale_tempfiles(<vault>/<agent>)` or any
  `find ... -name '.*.tmp' -delete`.
- **`.lock` file.** Per [scenario 1](#1-stale-lock--agentlockbusy-raised-but-no-agent-is-running)
  the OS released the `flock`; the file is fine.
- **Half-staged dream.** If the crash happened during a dream apply, the
  staging area lives under `<vault>/<agent>/dreams/.staging-<uuid>/`.
  See [scenario 4](#4-migration-corrupted-vault-state) for migrations and
  the dream-specific note below.
- **Half-applied migration.** The migration runner snapshots before any
  write and rolls back on failure. See
  [scenario 4](#4-migration-corrupted-vault-state).

### Diagnose

```bash
atomic-agents doctor --agent <name>            # exit 0 = clean
find <vault>/<agent> -name '.*.tmp'             # leftover temp files
ls <vault>/<agent>/dreams/.staging-* 2>/dev/null  # leftover dream staging
```

### Recover

```bash
# 1. Clean leftover temp files (always safe).
find <vault>/<agent> -name '.*.tmp' -delete

# 2. Discard leftover dream staging dirs older than the crash (safe — they
#    were never applied to live memory).
rm -rf <vault>/<agent>/dreams/.staging-<uuid>

# 3. Re-run whatever was running.
atomic-agents run <agent> --work-item "..."
```

Captures are idempotent: writing a note whose `(type, name, body hash)`
matches the existing file is a no-op (the on-disk version snapshot
captures the pre-state and the INDEX entry is re-asserted). Re-running
will not double-write captures.

### When to inspect `.versions/` instead of current files

If the crash happened during a memory mutation and you suspect the live
file is wrong (frontmatter looks off, body truncated despite the atomic
guarantee), compare against the latest snapshot:

```bash
atomic-agents version <agent> <note-filename.md>
# Prints the list of versions, newest first.

# Read the previous version directly:
cat <vault>/<agent>/memory/.versions/<note-stem>/<YYYYMMDDT...>.md
```

If the version is good and the live note is wrong:

```bash
atomic-agents restore <agent> <note-filename.md> <YYYYMMDDT...md>
```

`restore` snapshots the current state first, so you can undo the undo.

### Special case: crash during memory migration

The schema migration runner (`python -m atomic_agents.migrate`) snapshots
the entire vault to `<vault>/_migrations/snapshots/<...>.tar.gz` before
applying any change, validates after every script, and rolls back the
tarball if validation fails. **A crash mid-migration leaves a snapshot
behind that you can restore from.** See [scenario 4](#4-migration-corrupted-vault-state).

### Verify

```bash
atomic-agents doctor --agent <name>
# All nine checks should pass.
```

---

## 3. Corrupted INDEX — orphan notes or dangling INDEX entries

**Symptom.** The agent's INDEX shows entries that no longer have a file,
or `memory/*.md` files exist that are not listed in INDEX. The runtime
may still work (it reads notes directly), but recall is degraded
because INDEX is what the agent loads first.

### Diagnose

There is no `atomic-agents lint` CLI subcommand yet — orphan detection is
exposed at the API level via `FilesystemBackend.list_orphans()`. Run it
from a Python one-liner:

```bash
python - <<'PY'
from pathlib import Path
from atomic_agents.memory.filesystem import FilesystemBackend

agent_root = Path("<vault>/<agent>")
backend = FilesystemBackend(agent_root, "memory")
orphans = backend.list_orphans()
print(f"{len(orphans)} orphan note(s):")
for ref in orphans:
    print(f"  {ref.name}  ({ref.type})  {ref.description}")
PY
```

`list_orphans()` reports `memory/*.md` files that do NOT appear in
`INDEX.md` by name or stem. **Dangling INDEX entries** (INDEX lines
pointing at filenames that no longer exist) are not surfaced by that API
— spot them by reading INDEX manually:

```bash
# Grab every wikilink + bullet-link target out of INDEX, check each exists.
grep -oE '\([a-zA-Z0-9_-]+\.md\)' <vault>/<agent>/memory/INDEX.md \
  | tr -d '()' \
  | sort -u \
  | while read fn; do
      [ -f "<vault>/<agent>/memory/$fn" ] || echo "MISSING: $fn"
    done
```

### Recover

```bash
# 1. Always back INDEX up first.
cp <vault>/<agent>/memory/INDEX.md <vault>/<agent>/memory/INDEX.md.bak

# 2A. Drop dangling lines.
# Open INDEX.md in any editor and delete the bullet for each MISSING
# filename surfaced above.

# 2B. Re-attach orphan files.
# For each orphan reported by list_orphans(), open INDEX.md and add a
# bullet under the right section. The section is one of:
#   ## User Profile        (type: user)
#   ## Critical Feedback   (type: feedback)
#   ## Active Projects     (type: project)
#   ## Locked Decisions    (type: decision)
#   ## Reference           (type: reference)
# The line format is:
#   - [<name>](<filename>) — <description>
# `<name>` and `<description>` come from the orphan's frontmatter.
```

### Regenerate from scratch (last resort)

There is no `rebuild_index` CLI today. If INDEX is unsalvageable, you can
regenerate one by re-rendering from the live notes. The same renderer the
backend uses when INDEX is absent is exposed as
`_generate_index_from_dir`:

```bash
python - <<'PY'
from pathlib import Path
from atomic_agents.memory.filesystem import _generate_index_from_dir

memory_dir = Path("<vault>/<agent>/memory")
text = _generate_index_from_dir(memory_dir)
# Review the output FIRST, then write it.
(memory_dir / "INDEX.md.new").write_text(text)
print(f"Wrote {memory_dir/'INDEX.md.new'} — review, then rename over INDEX.md")
PY
```

This re-derives the section structure from every note's frontmatter
`type` field. It will lose any operator-edited section ordering, custom
sub-headers, or per-section commentary — which is why the backup is
mandatory.

### Verify

```bash
# Re-run list_orphans() — should print 0.
python -c "from pathlib import Path; from atomic_agents.memory.filesystem import FilesystemBackend; \
  print(len(FilesystemBackend(Path('<vault>/<agent>'), 'memory').list_orphans()), 'orphans')"
```

---

## 4. Migration corrupted vault state

**Symptom.** A `python -m atomic_agents.migrate --to vN` run completed
(or partially completed), and the vault is now in a bad state. The agent
will not start, doctor's `vault` check fails, or notes that worked
yesterday no longer parse.

The migration runner *should* roll back automatically on any
validation failure — but you are looking at this section because it did
not, or because the validation passed but downstream behavior is wrong.

### Diagnose

```bash
python -m atomic_agents.migrate --status            # current vault version
python -m atomic_agents.migrate --list-snapshots    # available restore points
```

`--status` prints the current schema version of the vault and the version
the installed helper supports. If they disagree, your vault is at the
wrong schema for this code.

`--list-snapshots` lists every tarball under
`<vault>/_migrations/snapshots/<YYYY-MM-DD>T<HHMMSS>_pre_vN_migration.tar.gz`,
newest first.

### Recover

```bash
# Roll the vault back to the pre-migration snapshot.
python -m atomic_agents.migrate --rollback <snapshot-filename>
```

The rollback extracts the tarball into a sibling staging directory, then
atomically renames it over the live vault — so if the restore itself
fails part-way through, the live vault is untouched. See
[`upgrading.md`](upgrading.md) § "Run the migration runner" for the full
restore contract, including how the helper-version constant in the
installed package interacts with vault-content rollback (and why you
may need to pin the package back to the prior tag, too).

### What to do if rollback itself fails

The runner reports `Rollback failed:` with the underlying error. The
snapshot tarball is still at the path it was at when you ran
`--list-snapshots` — your last-resort recovery is to extract it manually:

```bash
SNAP=<vault>/_migrations/snapshots/<filename>.tar.gz

# 1. Move the live vault aside (do NOT delete it — it has the .versions/
#    snapshots that are your inner safety net).
mv <vault> <vault>.broken-$(date +%Y%m%d-%H%M%S)

# 2. Recreate the parent dir and extract.
mkdir -p <vault>
tar -xzf "$SNAP" -C <vault>

# 3. Confirm doctor agrees the restore worked.
atomic-agents doctor --agent <name>

# 4. If the restore looks good, you can eventually delete <vault>.broken-*
#    — but keep it around at least until a couple of successful agent runs
#    have confirmed there is nothing in it you still need.
```

If even that fails (corrupt tarball, missing snapshot), fall back to
[scenario 9](#9-backup--restore-operator-driven) — restoring per-note
from `.versions/` and the git remote.

### Verify

```bash
python -m atomic_agents.migrate --status
# Should report the version you rolled back to.
atomic-agents doctor --agent <name>
```

---

## 5. Memory write race — concurrent processes writing the same note

**Symptom.** Two memory files exist with near-identical names but
different bodies, or `.versions/<stem>/` shows two snapshots with the same
timestamp prefix but different short-hashes interleaved. Most often
caused by running the same agent under two schedulers at once (cron AND
LaunchAgent, or two cron entries that overlap).

`AgentLock` exists to prevent this — but it only protects against
processes that *use* it. A custom script that bypasses the lock and
writes notes directly will race against the lock-holding runtime.

### Diagnose

```bash
# Confirm only one scheduler is configured for the agent.
launchctl list | grep atomic-agents       # macOS LaunchAgents
crontab -l | grep atomic-agents            # cron

# Check whether there is currently a lock holder.
lsof <vault>/<agent>/.lock
```

If you see two near-duplicate notes:

```bash
ls -la <vault>/<agent>/memory/ | grep -i <topic>
# Compare frontmatter and bodies.
diff <vault>/<agent>/memory/feedback_x.md \
     <vault>/<agent>/memory/feedback_x_other.md
```

### Recover

```bash
# 1. Stop one of the schedulers so the race cannot continue.
launchctl unload ~/Library/LaunchAgents/com.atomic-agents.run.<agent>.plist
# or: crontab -e   # comment out the duplicate line

# 2. Decide which version of the racing notes to keep. Merge bodies by
#    hand if both contain unique information.

# 3. Delete the loser.
rm <vault>/<agent>/memory/<loser>.md

# 4. Fix INDEX (the loser's bullet is now dangling — see scenario 3).
```

### Prevention

The agent runtime is single-process per `AgentLock`. The race can only
happen when something writes the vault *without* going through the lock
— a one-off script, a `python -c` patch, a manual edit during a live
run. Either go through `agent.memory.write_note()` (which takes the lock
internally via `apply_staging` on dream paths, and via `_per_file_lock`
on direct writes), or take the lock manually:

```python
from atomic_agents._locks import AgentLock
with AgentLock(agent_root, wait_seconds=30):
    # mutate the vault here
    ...
```

### Verify

```bash
atomic-agents doctor --agent <name>
# memory-backend check stat-walks every note — passes only if everything
# parses cleanly.
```

---

## 6. Stale memory after sync conflict (Dropbox / iCloud / Syncthing / Obsidian Sync)

**Symptom.** Files appear in `memory/` with names like
`feedback_x (2).md` (iCloud), `feedback_x.sync-conflict-...md` (Syncthing),
or `feedback_x (conflicted copy ...).md` (Dropbox). The agent's INDEX
points at `feedback_x.md`; the conflict copy is invisible to the runtime
but pollutes the directory.

Obsidian Sync handles conflicts differently — it surfaces them inside
the Obsidian app rather than writing a separate file. If you sync the
vault through Obsidian Sync, the conflict story is handled there; that
runbook will live in `obsidian.md` (planned in the same docs/deployment/
directory).

### Diagnose

```bash
cd <vault>/<agent>/memory

# Common conflict suffixes from each sync provider.
ls | grep -E '\((conflicted copy|conflict)|sync-conflict|\([0-9]+\)\.md$'
```

### Recover

```bash
# 1. For each conflict file, decide which version is canonical.
diff feedback_x.md "feedback_x (conflicted copy 2026-05-04).md"

# 2. If the conflict has unique content, hand-merge it into the canonical
#    file via your editor. Save through the runtime where possible
#    (so the merge is captured in .versions/).

# 3. Delete the conflict file.
rm "feedback_x (conflicted copy 2026-05-04).md"
```

If the conflict touched `INDEX.md` itself, treat the recovery as a
[scenario 3](#3-corrupted-index--orphan-notes-or-dangling-index-entries)
INDEX repair.

### Prevention

- Stop syncing `<vault>/<agent>/.lock` and `<vault>/<agent>/memory/.versions/`
  if your sync tool supports per-path ignores — the lock file changes on
  every run and `.versions/` accumulates monotonically, so they are
  conflict magnets and yield no value if synced.
- For Obsidian Sync specifically, the per-vault settings allow excluding
  hidden files; turn that on.
- For multi-host setups, prefer git (push after every meaningful session)
  over file-sync — git's three-way merge handles markdown conflicts
  better than name-mangling.

### Verify

```bash
# No conflict files should remain.
find <vault>/<agent> -type f \( -name '* (*).md' -o -name '*conflict*' \)
# Doctor should be happy.
atomic-agents doctor --agent <name>
```

---

## 7. `.versions/` snapshot bloat

**Symptom.** `<vault>/<agent>/memory/.versions/` is large — hundreds of MB
or more on a long-running agent. Every `write_note` and `restore_version`
snapshots the previous on-disk content, so a chatty agent accumulates
quickly.

### Diagnose

```bash
du -sh <vault>/<agent>/memory/.versions/
du -sh <vault>/<agent>/memory/.versions/* | sort -h | tail -20
```

The second command finds the noisiest notes. A high count for a single
note is usually a sign the agent is over-capturing on that topic — worth
reviewing the lint pass (per [`spec/05-capture-rules.md`](../spec/05-capture-rules.md))
to find the loop.

### Recover

There is no built-in retention policy yet. Pruning is operator-driven and
trades disk for safety:

```bash
# Drop versions older than 90 days, keep the newest 5 per note.
# Review what would be deleted FIRST:
find <vault>/<agent>/memory/.versions -name '*.md' -mtime +90 -print

# Then delete (irreversible — there is no .versions/.versions).
find <vault>/<agent>/memory/.versions -name '*.md' -mtime +90 -delete

# Or nuke .versions/ entirely if you are willing to lose the
# restore-version safety net (your git remote becomes the only history).
rm -rf <vault>/<agent>/memory/.versions
```

### Trade-off

`.versions/` is what backs `atomic-agents version` and
`atomic-agents restore` ([scenario 2](#2-mid-run-crash--process-killed-during-agentcall)
recovery). Deleting it loses *per-note* time-travel — the per-vault
[git remote](#9-backup--restore-operator-driven) still gives you
snapshot-level rollback, but only at commit granularity, not at every
mutation.

The framework will eventually grow a retention setting; the issue is
tracked under the MemoryBackend protocol evolution. Until then, prune
manually and only when disk pressure actually warrants it.

### Verify

```bash
du -sh <vault>/<agent>/memory/.versions/
atomic-agents doctor --agent <name>
```

---

## 8. Doctor failures — what each check means

When `atomic-agents doctor --agent <name>` reports a fail, the `fix_hint`
under each entry is the canonical recovery. The summary below is the
operator's map from check name to the failure mode it catches; refer to
the live fix-hint output for the exact command to run, since it is
generated against your install paths.

| Check | Catches | Typical fix |
|---|---|---|
| `env` | `ATOMIC_AGENTS_ROOT` (or default `~/docs/agents`) does not resolve to a directory. | `mkdir -p <path>` or `export ATOMIC_AGENTS_ROOT=<existing-dir>`. |
| `python` | Interpreter is older than 3.11. | Re-run under a newer interpreter (e.g. `uv run --python 3.12 atomic-agents doctor`). |
| `vault` | Required files missing from the agent folder (`persona/IDENTITY.md`, `tools.md`, `model.md`, `memory/INDEX.md`). Cascade-aware: tools/model may live at the role layer. | Copy from a sample (`cp -r docs/samples/caldwell <agent-dir>`) or create the missing file. |
| `provider-keys` | For each provider used by `model.md` (default + fallback): no key resolvable via env, Keychain, or `~/.config/atomic_agents/keys.json`; OR the optional SDK extra (`openai`) is not installed. | Either `export ATOMIC_AGENTS_<PROVIDER>_KEY=...`, or `security add-generic-password -a $USER -s atomic-agents-<provider> -w '<key>'`, or `uv add 'atomic-agents-stack[openai]'`. |
| `config-parse[model.md]` | YAML in `model.md`'s `cost_guardrails:` block is malformed, or a cap value is non-numeric. | Fix the YAML; `python -c "import yaml; yaml.safe_load(open('model.md').read())"` for the precise location. |
| `config-parse[tools.md]` | `tools.md` has stray content under a path section that is not a bullet, or a syntactic error. | Match the format in [`spec/01-anatomy.md`](../spec/01-anatomy.md) and `docs/samples/caldwell/tools.md`. |
| `model` | `default_model` is not in `_costs.PRICING`, or `cost_guardrails_enabled` is true but a cap is 0. | Pick a priced model id, or add the id to `_costs.PRICING`, or fix the caps in `model.md`. |
| `mcp` | One or more MCP servers in `mcp.md` failed the stdio handshake within 10s. Per-server result. | Run the server's command manually (`<command> <args>`) and read its stderr; common causes are bad PATH, missing auth, or `read_paths` rejecting a path arg per `tools.md`. |
| `locks` | The `.lock` file exists AND its flock is currently held. Stale threshold defaults to 300 seconds — held longer than that gets a "stale" flag. | See [scenario 1](#1-stale-lock--agentlockbusy-raised-but-no-agent-is-running). |
| `memory-backend` | `FilesystemBackend.stats()` raised — usually because a note's frontmatter cannot parse, or `memory/` is missing entirely. | `mkdir -p <agent>/memory && touch <agent>/memory/INDEX.md`, or open the offending note and fix the YAML frontmatter. |
| `write-paths` | A path in `tools.md` `write_paths` does not exist, is not writable, or the agent's `memory/` directory is not covered by any `write_path` (or is inside a `read_only_path`). | Create the directory, fix permissions, or add the right entry to `tools.md`. |

### Verify

```bash
atomic-agents doctor --agent <name>
# Re-run after each fix. Exit 0 means the install is ready to schedule.
```

---

## 9. Backup + restore (operator-driven)

**Symptom.** A file or set of files was deleted or corrupted, and `.versions/`
plus the migration snapshots are not enough — either because the loss
predates them, because they were also damaged, or because the
recovery needs to cross the whole agent (not just one note).

The framework does not ship a backup tool. The vault is plain markdown +
JSON sidecars; the canonical backup is git, with the remote as the
durable copy.

### Recommended cadence

```bash
# Once per meaningful session — captures a coherent vault state.
cd <vault>
git add -A
git commit -m "session: <one-line summary>"

# At least weekly — pushes the durable copy off the local disk.
git push origin main
```

`.versions/` is a *per-mutation* safety net inside a single host. The
**git remote** is the *cross-host* safety net. Both matter; the question
is whether the loss is small enough to recover from `.versions/` (one
note, recent) or large enough to need git (multiple files, multiple
hosts, or pre-snapshot-window).

### Restore

```bash
# Time-travel one file to a previous commit.
git log --oneline -- <agent>/memory/<note>.md
git checkout <commit-sha> -- <agent>/memory/<note>.md

# Time-travel the whole agent.
git log --oneline -- <agent>/
git checkout <commit-sha> -- <agent>/

# Then verify before doing anything else.
atomic-agents doctor --agent <name>
atomic-agents run <agent> --work-item "ping"
```

### What is worth backing up beyond the vault

- The vault itself (covered above via git).
- `~/.config/atomic_agents/keys.json` if you use the file-based key
  store. Mirror the same keys into the system keychain so you have two
  recovery paths.
- LaunchAgent / cron entries — checked into a personal dotfiles repo,
  not just baked into the running host.
- The `atomic-agents-stack` package version pin (`pyproject.toml`
  `version`, or the tag the install came from). Restoring data without
  restoring the matching code can hit schema mismatches; see
  [`upgrading.md`](upgrading.md).

### What is NOT worth backing up

- `<agent>/.lock` — recreated on every run.
- `<agent>/memory/.versions/` — useful in-place; redundant once the
  containing vault is in git.
- `<agent>/dreams/.staging-*` — these are work-in-progress that the
  runtime tears down. If a backup catches one, ignore it on restore.

### Verify

```bash
atomic-agents doctor --agent <name>
atomic-agents run <agent> --work-item "ping"
# The first real run after a restore is the definitive ready-check.
```

---

## Escalation — when the runbook does not help

File an issue at
[dep0we/atomic-agents-stack/issues](https://github.com/dep0we/atomic-agents-stack/issues/new)
with:

- The output of `atomic-agents doctor --agent <name> --json`
- The orphan + INDEX-line check from [scenario 3](#3-corrupted-index--orphan-notes-or-dangling-index-entries)
- The exact command that failed and the full exception trace
- The last command that worked, and what you changed between then and
  the failure
- The release tag your install came from (`pip show atomic-agents-stack`
  or `git describe --tags --always`)

**Do not `rm -rf` the agent folder unless you have a confirmed backup.**
The vault contains the agent's conversation history, captured
preferences, and accumulated decisions — most of which is irreplaceable
even when the *code* is reinstalled fresh. If the runbook above and an
issue thread do not resolve it, the right move is to copy the broken
vault aside (`mv <vault> <vault>.broken-<date>`) and keep working from
a restored copy while the original is investigated.

---

## Cross-references

- [`upgrading.md`](upgrading.md) — full migration runner usage, snapshot
  + rollback flow, multi-version upgrades, per-step rollback contract.
- [`versioning.md`](versioning.md) — what counts as a breaking change
  vs. a fix; informs whether an upgrade is drop-in or needs the
  migration runner.
- [`../spec/05-capture-rules.md`](../spec/05-capture-rules.md) — lint
  pass semantics, orphan definition, capture idempotency rules, archive
  lifecycle.
- [`../spec/20-memory-backend.md`](../spec/20-memory-backend.md) — the
  MemoryBackend protocol, `NoteRef` / `VersionRef` shape, `WritePolicy`
  enforcement, and `list_orphans()` / `list_versions()` /
  `restore_version()` API surface.

Two further runbooks are planned in this same directory and will be
linked here when they land: an Obsidian-Sync-specific conflict runbook
(`obsidian.md`) and a programmatic-usage reference that catalogues every
exception class and the recovery shape per class (`programmatic.md`).
Until then, the exception names live in
[`atomic_agents/exceptions.py`](../../atomic_agents/exceptions.py).

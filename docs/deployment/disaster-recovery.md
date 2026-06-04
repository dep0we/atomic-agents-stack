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

Doctor runs the full check catalogue documented in
[`spec/27-doctor.md`](../spec/27-doctor.md) and prints a literal fix-hint
under every failure. Most disasters are one of those checks; the section
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

The schema migration runner — `python -m atomic_agents.migrate` — snapshots
the entire vault to `<vault>/_migrations/snapshots/<...>.tar.gz` before
applying any change, validates after every script, and rolls back the
tarball if validation fails. **A crash mid-migration leaves a snapshot
behind that you can restore from.** See [scenario 4](#4-migration-corrupted-vault-state).

### Verify

```bash
atomic-agents doctor --agent <name>
# All checks should pass.
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
# Post-#60 PR 2: route through the LockBackend Protocol (spec/21).
# acquire("") preserves the legacy `<agent_root>/.lock` on-disk artifact
# so any external diagnostic scripts you have pinned to that path keep
# working unchanged.
from atomic_agents.locks import FilesystemLockBackend
with FilesystemLockBackend(agent_root).acquire("", timeout=30):
    # mutate the vault here
    ...
```

The legacy `from atomic_agents._locks import AgentLock` import continues to
work as a deprecation shim (sunset planned for v1.1 (deferred from v1.0 per #201 PR 5 release decision)) — if you have older
runbooks or scripts pinned to that path, they'll keep working but emit
`DeprecationWarning` on import.

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
| `lock-backend` | Protocol-level LockBackend coherence (#60). `ATOMIC_AGENTS_LOCK_BACKEND` names an unknown backend, the matching `_URL` env var is unset for a non-filesystem backend, the backend extra is not installed, or the configured backend is unreachable from this host. | Unset both env vars to fall back to filesystem; or `export ATOMIC_AGENTS_LOCK_BACKEND_URL=redis://...`; or `pip install 'atomic-agents-stack[redis]'`; reachability misses WARN rather than FAIL — runtime will surface them at first `acquire()`. |
| `log-backend` | Protocol-level LogBackend coherence (#61). `ATOMIC_AGENTS_LOG_BACKEND` names an unknown backend, the matching `_URL` env var is missing for a structured backend, or the stats probe (`records_today` / `records_this_month`) raised. | Unset to fall back to filesystem JSONL; or set `ATOMIC_AGENTS_LOG_BACKEND=sqlite` (defaults to `<agent>/log/log.db` when no URL is set); fix the URL credentials if doctor reports a redacted-URL probe failure. |
| `agent-profile-backend` | Protocol-level AgentProfileBackend coherence (#63). `ATOMIC_AGENTS_PROFILE_BACKEND` names an unknown backend, the matching `_URL` env var is missing for SQLite, or the agent-count probe raised (DB unreachable, schema cold-start failure, capability mismatch). | Unset for filesystem default; or set `ATOMIC_AGENTS_PROFILE_BACKEND=sqlite` (defaults to `<scope_root>/.profile.db`); credentials in `ATOMIC_AGENTS_PROFILE_BACKEND_URL` are redacted in the failure output — check the URL host + path manually. |
| `tool-registry-backend` | Protocol-level ToolRegistryBackend coherence (#64). `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` names an unknown backend, the matching `_URL` env var is missing for SQLite, or the tool-count probe raised (DB unreachable, schema cold-start failure, capability honesty mismatch). | Unset for filesystem default; or set `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND=sqlite` (defaults to `<agent_root>/.tools.db` with `agent_scope=<agent_root.name>`); URL must use the `sqlite:///path?agent_scope=<name>` shape — `_redact_url` strips credentials from the failure output. |
| `mandate-backend` | Protocol-level MandateBackend coherence (#124). `ATOMIC_AGENTS_MANDATE_BACKEND` names an unknown backend, the operator-config probe raised, or the orphan-recovery scan would refuse to start (LockBackend coherence dependency). | Unset for filesystem default at `<scope_root>/mandates.md` + `<scope_root>/.judge-state/mandates.json`; or fix the upstream `lock-backend` failure first since recovery scans require the lock. Run `atomic-agents mandate list` after the fix to confirm parse succeeded. |
| `policy-backend` | Protocol-level PolicyBackend coherence (#89). `ATOMIC_AGENTS_POLICY_BACKEND` names an unknown backend, `policy.md` at the project root fails to parse, or the cascade detected a project-floor violation. | Unset for filesystem default (project-root `policy.md` is opt-in — no file means no policy enforcement); `python -c "import yaml; yaml.safe_load(open('policy.md'))"` to surface YAML parse errors; check `ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP` if you wanted log-only mode (defaults to `true` post-PR 4). |
| `persona-backend` | Protocol-level PersonaBackend coherence (#62). `ATOMIC_AGENTS_PERSONA_BACKEND` names an unknown backend, the matching `_URL` env var is malformed (non-filesystem scheme, netloc, relative path), or the persona-count probe raised at `<scope_root>/.personas/`. | Unset for filesystem default at `<scope_root>/.personas/`; URL factory accepts only `filesystem:///absolute/path`; if a `PersonaOwnershipConflict` was raised at `load_profile`, remove either `<agent>/persona.link.md` or `<agent>/persona/IDENTITY.md` so exactly one ownership layout remains. |

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

## Protocol-backend exception catalog

The protocol-pattern series adds a per-Protocol exception family that
fires at runtime (not just at doctor preflight). The following catalog
maps each Protocol exception (or audit-event family, where the failure
shape is an event rather than an exception) to a triage shape: what
fires, why it fires, how to verify the cause, how to recover.

Reference the code at
[`atomic_agents/exceptions.py`](../../atomic_agents/exceptions.py),
[`atomic_agents/mandate/types.py`](../../atomic_agents/mandate/types.py),
and [`atomic_agents/policy/types.py`](../../atomic_agents/policy/types.py)
for the canonical class definitions.

### LockBackend (#60, spec/21)

- **`LockBusy`** (aliased `AgentLockBusy` for backwards compat) — another
  process is holding the agent or scope lock and the wait window expired.
  **Verify**: `lsof <vault>/<agent>/.lock` (filesystem backend) or
  `redis-cli GET <key>` (Redis backend) — does a live holder exist?
  **Recover**: wait for the holder, or if it has crashed, see
  [scenario 1](#1-stale-lock--agentlockbusy-raised-but-no-agent-is-running).
  Filesystem backends release on process death automatically; Redis
  backends release when the lease TTL expires (default TTL/3 heartbeat).
- **`LockLost`** — the caller HAD the lock and lost it because a lease
  expired before a renewal landed. Only fires on lease-backed backends
  (`RedisLockBackend` etc); the filesystem backend declares
  `supports_lease=False` and never raises it. **Verify**: check the
  daemon-thread log for the missed heartbeat; check network latency to
  the lease backend. **Recover**: the in-flight work MUST abort —
  `LockLost` is deliberately NOT a subclass of `LockBusy` so a generic
  `except LockBusy:` handler cannot swallow it. Re-acquire the lock and
  re-run the work, idempotently.
- **`BackendNotRegistered`** — `ATOMIC_AGENTS_LOCK_BACKEND` was set to a
  name that nobody registered. **Verify**: `atomic-agents doctor` →
  `lock-backend` check surfaces this with the registered backend list.
  **Recover**: unset the env var (filesystem default) or
  `register_lock_backend(...)` the named backend at import time.

### LogBackend (#61, spec/22)

LogBackend has **no module-specific exceptions**. Backends raise stdlib
exceptions instead — the operator-facing failure shapes are:

- **`sqlite3.OperationalError`** — `SQLiteLogBackend` raised at append /
  query time, usually for a locked database (multi-process WAL race
  before `PRAGMA busy_timeout=5000` lands the wait window) or a
  permissions failure on the DB file. **Verify**: `sqlite3
  <agent>/log/log.db '.schema'` to confirm reachability; check WAL
  files (`-wal`, `-shm`) for permissions parity with the main file.
  **Recover**: fix permissions, ensure the multi-process apps share the
  same filesystem with WAL-safe semantics (not NFS), or pin a single
  appender per host.
- **`OSError`** — `FilesystemLogBackend` raised when the agent's
  `<agent>/log/YYYY-MM/` directory cannot be created or written.
  **Verify**: `ls -la <agent>/log/` and `touch
  <agent>/log/.write-test`. **Recover**: fix the parent permissions or
  point `ATOMIC_AGENTS_ROOT` somewhere writable.
- **`ValueError`** — raised on naive datetime objects passed to query
  filters (the Protocol contract requires tz-aware `datetime`).
  **Verify**: the exception message names the offending field.
  **Recover**: pass `datetime.now(timezone.utc)` (or any tz-aware
  datetime) instead of `datetime.now()`.
- **`BackendNotRegistered`** — `ATOMIC_AGENTS_LOG_BACKEND` was set to a
  name nobody registered. Same shape as the LockBackend variant.

### AgentProfileBackend (#63, spec/24)

- **`AgentProfileNotFound`** — `load_profile(agent_id)` was called with
  an id the backend doesn't know about. **Verify**: filesystem backend
  → `ls <scope_root>/<agent_id>/persona/IDENTITY.md` (the sentinel
  file). SQLite backend → `sqlite3 <scope_root>/.profile.db 'SELECT
  name FROM agents'`. **Recover**: typo? use the listed id. Genuine
  miss? create the agent (filesystem: copy a sample under
  `<scope_root>/<agent_id>/`; SQLite: `save_profile(profile)` with the
  intended id). Distinct from `BackendNotRegistered` — the backend is
  fine; the agent id is not.
- **`AgentProfileExists`** — `clone(source, target)` refused to
  overwrite an existing agent. **Verify**: `exists(target_id)` returns
  True. **Recover**: pick a fresh id, or delete the target first and
  retry, or use `save_profile()` directly which is documented to
  overwrite. Create-flavored operations refuse silent overwrites by
  design.
- **`SnapshotNotFound`** — `restore(agent_id, snapshot_id)` referenced
  an unknown snapshot. **Verify**: `list_snapshots(agent_id)` to see
  the canonical ids. **Recover**: pass an id from the listing; mind
  that cross-agent snapshot ids are rejected at the storage layer
  (filesystem: `metadata.agent_id` cross-check; SQLite: `WHERE
  snapshot_id = ? AND agent_id = ?` AND-clause).

### ToolRegistryBackend (#64, spec/25)

- **`ToolNotInRegistry`** — `ToolRegistryBackend.load_tool(name)` was
  called with an unknown name. Distinct from `ToolNotRegistered` which
  the in-memory `ToolRegistry.execute()` raises when the LLM emits a
  `tool_use` whose name isn't in the dispatch registry. The two cover
  different layers; an operator catching one and expecting to also
  catch the other will be surprised. **Verify**: filesystem backend →
  `ls <agent>/tools/<name>.md`; SQLite backend → `sqlite3
  <agent>/.tools.db 'SELECT name FROM tools WHERE agent_scope = ?'`.
  **Recover**: catalog miss? install the tool (filesystem: drop
  descriptor + handler files; SQLite: `install(source, version)`).
  Dispatch miss? register the loaded tool in the in-memory
  `ToolRegistry`.
- **`ToolHandlerImportFailed`** — a handler module could not be
  imported during `load_tool` / `validate`. Wraps the underlying
  ImportError. **Verify**: `python -c "import importlib.util;
  spec=importlib.util.spec_from_file_location('h',
  '<agent>/tools/<name>.py'); m=importlib.util.module_from_spec(spec);
  spec.loader.exec_module(m); print(callable(getattr(m, 'handler',
  None)))"`. **Recover**: fix the import (often a missing dependency,
  syntax error, or missing `handler` callable). Surfaced via
  `ValidationResult.errors` from `validate(name)` for batch triage.
- **`ToolDescriptorInvalid`** — a tool descriptor (`<name>.md`
  frontmatter) is malformed: missing YAML delimiters, parse error,
  non-dict root, non-dict `input_schema`, or descriptor `name` field
  that doesn't match the file stem. **Verify**: open the descriptor;
  `python -c "import yaml; yaml.safe_load(open('<name>.md').read())"`.
  **Recover**: fix the frontmatter to match the format in
  [`spec/17-tools.md`](../spec/17-tools.md).
- **`ToolAlreadyInstalled`** — `install(source, version)` collided on
  tool name. Only raised by backends declaring
  `supports_install=True`. **Verify**: `list_tools(agent_scope)` to
  confirm the name exists. **Recover**: `uninstall(name)` first if you
  intend to replace; otherwise pick a fresh name. Concurrent installs
  with the same name resolve exactly one winner via INSERT-first
  ordering — losers raise this exception WITHOUT touching disk.
- **`ToolNameCollision`** — fires at the in-memory `ToolRegistry`
  boundary when `register()` is called with a name already in the
  registry and `allow_overwrite=False` (the default). Distinct from
  `ToolAlreadyInstalled` which is the catalog-level collision.
  **Verify**: read the exception message; the colliding name is in it.
  **Recover**: pass `allow_overwrite=True`, rename the new tool, or
  reorganize the registration order so the intended winner registers
  last.

### MandateBackend (#124, spec/29)

- **`MandateError`** — base class for mandate subsystem errors. Catch
  this to handle all mandate exceptions uniformly. Subclasses below.
- **`MandateInvalid`** — `mandates.md` failed parser-level validation:
  malformed YAML in a mandate section, ID outside `[a-z0-9][a-z0-9-]*`,
  constraint without `unconstrained: true` justification, time window
  with `start_utc >= end_utc`, project-root + per-agent ID collision.
  **Verify**: read the exception message; it names the offending
  mandate section. `python -c "import yaml;
  yaml.safe_load(open('mandates.md').read())"` for the YAML parse
  error. **Recover**: fix `mandates.md` to match
  [`spec/29-mandates.md`](../spec/29-mandates.md) §"Mandate descriptor
  schema".
- **`MandateNotFound`** — `load_mandate(id, scope)` cannot resolve the
  id. Distinct from `MandateInvalid` (parse failure on a known
  mandate) and `BackendNotRegistered` (operator-config failure before
  any lookup). **Verify**: `atomic-agents mandate list --scope
  <scope>` to see the available ids. **Recover**: typo? use the listed
  id. Genuine miss? author the mandate in `mandates.md` at the right
  scope and re-run.
- **`MandateStateSchemaUnsupported`** — `read_state(scope)` returned a
  state with an unknown `schema_version`. Forward-incompat error —
  readers MUST consult the field and raise rather than silently
  migrate. **Verify**: `cat
  <scope_root>/.judge-state/mandates.json | jq .schema_version`.
  **Recover**: upgrade the installed `atomic-agents-stack` to a
  version that supports the schema, or manually downgrade the state
  file's `schema_version` after confirming the on-disk shape matches
  the older schema. Don't drop state silently.

**Runtime denials are audit events, not exceptions.** The `MandateCheck`
judge specialist emits these event families (queryable via
`LogBackend.query(primitive='judgment')`):

- `mandate_cap_exceeded_block` — cumulative budget defense blocked the
  action. Operators triaging this grep the log for matching
  `mandate_id` and inspect the running `sum_prior_token_cost`.
- `mandate_action_diverged` — post-action verification found the
  executed target differed from the authorized target at proposal
  time. Audit-only in v1; not a refund mechanism.
- `mandate_action_verification_unavailable` — verifier raised; the
  divergence-check could not complete. Same audit-only shape.

These are the operator-facing signal when a mandate denies a call; no
exception propagates because the judge layer's BLOCK outcome translates
to a refusal the LLM sees on its next turn.

### PolicyBackend (#89, spec/32)

- **`PolicyError`** — base class for policy subsystem errors. Catch
  this to handle all policy exceptions uniformly.
- **`PolicyInvalid`** — `policy.md` failed parser-level validation:
  malformed YAML, `agent_name` outside `[a-zA-Z0-9_.+@-]+` at the API
  boundary, negative cost-cap value, or other structural failure.
  Allow + deny on the same tool name within a layer surfaces as a
  warning (deny wins) — `PolicyInvalid` is for structural failures
  only. **Verify**: `python -c "import yaml;
  yaml.safe_load(open('policy.md').read())"` for YAML errors.
  **Recover**: fix `policy.md` to match
  [`spec/32-policy-backend.md`](../spec/32-policy-backend.md)
  §"Policy descriptor schema".

**Runtime denials are audit events, not exceptions.** Policy enforcement
emits a unified `policy_decision` event family with these
discriminators:

- `decision_kind: deny` + `axis: cost_cap | tool_allowlist |
  mcp_allowlist | model_selection` — the operator-facing signal that
  Policy refused a call. `enforced: bool` truthfully reflects whether
  money was actually spent (cost-cap denials with `cap_action ∈ {alert,
  fallback}` emit `enforced=False`).
- `decision_kind: override` — Policy supersedes a per-call
  `agent.call(model=...)` kwarg via fleet-config-wins precedence.
  `model_from_per_call_override` captures the kwarg so the caller can
  detect the silent override. No emission when Policy agrees with the
  kwarg.

**Triage commands.** Operators reading
`LogQuery(primitive='policy_decision', enforced=True)` see only
actually-blocked events for billing-incident attribution. Per
`(tool_name, call)` dedup bounds tool-allowlist denials to one event
per LLM retry loop (#273) so the audit shape stays clean across both
log-only and enforce modes.

### PersonaBackend (#62, spec/33)

- **`PersonaNotFound`** — `PersonaBackend.load_persona(persona_id)` was
  called with an id the backend doesn't know about. Distinct from
  `BackendNotRegistered` — the backend is fine; the persona id is
  not. **Verify**: `atomic-agents persona list` (filesystem backend
  walks `<scope_root>/.personas/<persona_id>/`); SQLite-backed
  backends query the persona table. **Recover**: typo? use the listed
  id. Genuine miss? author the persona at
  `<scope_root>/.personas/<persona_id>/{IDENTITY,SOUL,USER}.md` and
  `metadata.json`, or `save_persona(persona_id, ...)`.
- **`PersonaExists`** — `save_persona` (with `overwrite=False`) or
  `clone(source, target)` refused to overwrite an existing persona.
  **Verify**: `atomic-agents persona show <persona_id>`. **Recover**:
  pick a fresh id, or pass `overwrite=True` (swap-and-delete with the
  20-iteration retry bound for macOS APFS `ENOTEMPTY` contention).
- **`PersonaSnapshotNotFound`** — `restore(persona_id, snapshot_id)`
  referenced an unknown snapshot. Cross-persona snapshot isolation is
  enforced at the backend level — snapshot ids belong to one persona
  and don't carry over. **Verify**: `atomic-agents persona
  list-snapshots <persona_id>`. **Recover**: pass an id from the
  listing. Snapshot ids match `snap_<YYYY-MM-DDTHHMMSS>_<12hex>` per
  spec/33 Implementer Contract #8.
- **`PersonaOwnershipConflict`** — both `<agent>/persona.link.md` and
  `<agent>/persona/IDENTITY.md` exist for the same agent. The
  filesystem-backend raises this loudly at `load_profile()` because
  two files on disk is a visible operator mistake the framework must
  surface; SQLite uses silent-drop with the equivalent
  `agent_profile_save_dropped_persona_fields` event for cross-backend
  uniformity (D2a + D-PP-8). **Verify**: `ls <agent>/persona.link.md
  <agent>/persona/IDENTITY.md`. **Recover**: pick one layout. Remove
  `persona.link.md` to keep the legacy `<agent>/persona/{IDENTITY,
  SOUL, USER}.md` layout; remove the `persona/*.md` files to use the
  shared-persona reference. The canonical lock-paragraph in
  `CLAUDE.md` + [`spec/33`](../spec/33-persona-backend.md) explains
  the behavioral story.
- **`PersonaLinkInvalid`** — the `persona.link.md` file is malformed
  or references an unknown persona record: YAML code block won't
  parse, `kind:` field is missing, `kind:` is not a supported value
  (v1: `shared` only), `persona_id:` is missing, or `persona_id:`
  fails the charset pattern `[a-zA-Z0-9_.+@-]+`. Distinct from
  `PersonaNotFound` — this means the reference FILE is malformed;
  `PersonaNotFound` means the file parsed correctly but the
  referenced persona record doesn't exist. **Verify**: open
  `persona.link.md`; the YAML code block must contain at minimum
  `kind: shared` + `persona_id: <id>`. **Recover**: fix the YAML
  per [`spec/33`](../spec/33-persona-backend.md) §"persona.link.md
  format".
- **`PersonaCorrupted`** — a persona record exists on disk but its
  contents are corrupt or structurally invalid: `metadata.json` has
  invalid JSON, missing required keys (`version`, `created_at`), a
  body file contains non-UTF-8 bytes, or `schema_version` names a
  version this release does not support. Distinct from
  `PersonaNotFound` — the persona directory EXISTS but its data
  cannot be interpreted. **Verify**: `ls
  <scope_root>/.personas/<persona_id>/` and `python -c "import json,
  pathlib;
  print(json.loads(pathlib.Path('<scope_root>/.personas/<persona_id>/metadata.json').read_text()))"`.
  **Recover**: repair the corrupt file (fix JSON, re-encode body as
  UTF-8) or remove the persona record (`rm -rf
  <scope_root>/.personas/<persona_id>/`) and recreate from a backup
  or snapshot.

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

- [`obsidian.md`](obsidian.md) — Obsidian-Sync-specific conflict
  recovery (`~conflict~`, `.trash/`, soft-deleted notes coming back
  via Sync) plus the lock-race interaction when a synced device
  writes mid-call. Pairs with the stale-lock scenario above.
- [`programmatic.md`](programmatic.md) — the complete public exception
  table. Every exception named in the doctor failure-mode table above
  has its raise site, recovery class, and catchable-mid-call rule
  documented there. Authoritative reference for "what exception is
  this and what do I do."
- [`cost-guardrail-sizing.md`](cost-guardrail-sizing.md) — when
  `CostGuardrailBlocked` is the symptom, this is the doc for picking
  caps that prevent the recurrence rather than just recovering from it.

The exception names also live in
[`atomic_agents/exceptions.py`](../../atomic_agents/exceptions.py)
for a quick code-level reference.

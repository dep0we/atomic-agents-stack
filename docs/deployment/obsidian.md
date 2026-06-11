# Obsidian-backed deployment

How to run `atomic-agents-stack` with the agent vault living inside an
[Obsidian](https://obsidian.md) vault that's synced across devices via
[Obsidian Sync](https://obsidian.md/sync). This is the "edit the agent's
persona on your phone, framework picks it up on the next run" deployment
shape.

The framework does not require Obsidian — see
[`../appendix/portability.md`](../appendix/portability.md) for the
"vault is just a folder of markdown" framing. This doc deepens that
appendix for the operator who *does* want to use Obsidian, and is
specific about the interactions a sync mechanism creates that a
single-host filesystem doesn't.

If your sync mechanism is git, iCloud Drive, Dropbox, or Syncthing
instead of Obsidian Sync, most of this doc still applies — the layout,
the walker behavior, and the lock race are the same. The
ignore-pattern syntax and conflict-copy naming differ; see the relevant
sync tool's docs. Pair this with the conflict-copy guidance in
[`../appendix/portability.md`](../appendix/portability.md#sync-mechanisms-pick-one-or-none).

---

## TL;DR

```bash
# 1. Pick a vault location inside your Obsidian vault
export ATOMIC_AGENTS_ROOT="$HOME/ObsidianVault/agents"
mkdir -p "$ATOMIC_AGENTS_ROOT"

# 2. Copy the Caldwell sample as your first agent
cp -r docs/samples/caldwell "$ATOMIC_AGENTS_ROOT/caldwell"

# 3. Tell Obsidian Sync what NOT to sync (see §"Sync ignore patterns" below)
#    Paste the block from this doc into the Obsidian Sync settings.

# 4. Verify the install
atomic-agents doctor --agent caldwell

# 5. First run
atomic-agents run caldwell --work-item "introduce yourself"
```

If `doctor` exits 0 and the run succeeds, the deployment is healthy. The
rest of this doc is the operator's reference for the trade-offs each
step encodes.

---

## Recommended vault layout

Put the agents root directly under the Obsidian vault, so an agent
folder lives at `<obsidian-vault>/agents/<agent-name>/`:

```
<obsidian-vault>/
├── .obsidian/                       ← Obsidian's config — sync-aware (see below)
├── .trash/                          ← Obsidian's soft-delete — DO NOT sync into agents/
├── notes/                           ← your normal Obsidian notes (untouched by the framework)
├── daily/                           ← your daily-notes (untouched by the framework)
└── agents/                          ← <agents_root> — what the framework reads + writes
    ├── caldwell/
    │   ├── persona/
    │   │   ├── IDENTITY.md          ← phone-editable, host-readable
    │   │   ├── SOUL.md              ← phone-editable, host-readable
    │   │   └── USER.md              ← phone-editable, host-readable
    │   ├── tools.md                 ← host-only edits recommended (policy)
    │   ├── model.md                 ← host-only edits recommended (cost guardrails)
    │   ├── mcp.md                   ← host-only edits (paths are host-local)
    │   ├── memory/
    │   │   ├── INDEX.md             ← framework-written; do not hand-edit during a run
    │   │   ├── feedback_*.md        ← framework-written; phone-readable
    │   │   ├── user_*.md            ← framework-written; phone-readable
    │   │   └── .versions/           ← framework-managed version snapshots; EXCLUDE FROM SYNC
    │   ├── wiki/                    ← optional; same posture as memory/
    │   ├── journal/                 ← framework-written narrative entries
    │   │   └── YYYY-MM/YYYY-MM-DD.md
    │   ├── log/                     ← JSONL audit trail — EXCLUDE FROM SYNC
    │   │   └── YYYY-MM/YYYY-MM-DD.jsonl
    │   ├── _dashboard/              ← regeneratable HTML dashboard — EXCLUDE FROM SYNC
    │   │   └── index.html
    │   └── .lock                    ← per-agent fcntl lock — EXCLUDE FROM SYNC
    ├── agent-a/
    └── agent-b/
```

The canonical anatomy is documented in
[`../spec/01-anatomy.md`](../spec/01-anatomy.md). The only Obsidian-specific
choice above is *where* you put `<agents_root>` — directly inside the
synced vault rather than at `~/agents/` outside it. The framework
resolves the root via `ATOMIC_AGENTS_ROOT` (see `atomic_agents/_platform.py`:
`get_agents_root()` falls back to `~/docs/agents` when the env var is
unset — for an Obsidian deployment you almost always want to set the env
var explicitly so the framework writes inside your vault).

### Why `agents/` and not the vault root?

You could put each agent at `<obsidian-vault>/caldwell/`, but that
mixes operator-edited Obsidian notes (your daily notes, project pages,
meeting notes) with framework-written files at the same level. Putting
everything under `agents/` does three useful things:

1. **One ignore-pattern block, not N.** Sync exclusions can target
   `agents/*/log/` instead of one per agent.
2. **Obsidian search results stay clean.** Searches scoped above
   `agents/` skip framework-written log entries and version snapshots.
3. **Easy revoke.** If you decide to stop running the framework, you
   delete one directory — your vault is otherwise untouched.

---

## Sync ignore patterns

Some files in an agent vault should sync everywhere (persona, memory
notes, the journal). Others must NOT sync — either because they're
host-specific (`.lock`), large and redundant (`.versions/`), high-churn
(`log/`), or locally regeneratable (`_dashboard/`).

### Obsidian Sync — `.obsidian/sync.ignore`

Obsidian Sync uses `.obsidian/sync.ignore` to exclude paths from
sync. Paths are gitignore-style; match relative to the vault root.

Paste the following block into your `.obsidian/sync.ignore` (create the
file if it doesn't exist):

```gitignore
# atomic-agents-stack — exclude framework-internal + host-specific files
# Match the directories under <obsidian-vault>/agents/.

# Per-agent JSONL audit trail. High churn — every run appends. Pollutes
# sync diffs. Logs are observability, not state; if you need cross-host
# visibility, render the dashboard (which excludes them anyway).
agents/*/log/

# Version snapshots of memory notes. Large; redundant when the current
# note is already synced. Each note write that targets an existing file
# snapshots the prior version under memory/.versions/<stem>/.
agents/*/memory/.versions/
agents/*/wiki/.versions/

# Per-agent fcntl lock file. Host-specific (records PID + acquisition
# time of the running process). Syncing it is harmful — it would either
# be empty on the receiving host (confusing) or contain a remote PID
# the local OS doesn't recognize.
agents/*/.lock

# Transient staging directories created during multi-step pipelines
# (dream batches, cascade leases). Operations are idempotent and the
# staging area regenerates on next run.
agents/*/.staging-*/
agents/*/.dream-staging/

# Rendered cost / activity dashboard. Self-contained HTML output —
# regeneratable from log/ + outcomes/ on any host that runs the
# framework. Syncing it would cause two hosts to overwrite each
# other's renders. Optional: omit this line if you only render the
# dashboard from one host and want it readable from your phone.
agents/*/_dashboard/

# Migration snapshots (tarballs taken before schema migrations).
# Large; only needed on the host that ran the migration, for rollback.
_migrations/snapshots/

# Crashed-write temp files. atomic_write writes to .<name>.<random>.tmp
# then renames; a crashed write leaves a .tmp file that the lint pass
# or the next atomic_agents.cleanup-tempfiles call removes.
agents/*/**/.*.tmp
```

### Why each exclusion

| Path | Why excluded |
|---|---|
| `agents/*/log/` | Every `agent.call()` appends a JSONL record. Multiple devices sharing a vault sync log entries from every host they pass through, generating a constant stream of small writes that pollute sync diffs and confuse rollups. Logs are write-only audit trail — they don't need cross-device consistency. |
| `agents/*/memory/.versions/` | `_versioning.py` writes a full-file snapshot to `memory/.versions/<stem>/<timestamp>.md` before any update to an existing memory note. The current note is already synced. The snapshot pile grows monotonically; on a multi-host setup with frequent edits the snapshot directory dwarfs the live memory directory within weeks. **Trade-off to know:** excluding `.versions/` means `agent.memory.restore_version()` works only on the host that wrote the snapshot. If you want to call `restore_version` from a device that wasn't the writer (e.g., review old versions from your phone), DO sync `.versions/` and accept the size cost. The default recommendation (exclude) fits operators who run the framework on one host and edit content on others; the override fits operators who run AND restore from multiple hosts. |
| `agents/*/.lock` | `atomic_agents/_locks.py` uses `fcntl.flock` for in-process exclusion. The lock file records `pid=<PID> acquired=<epoch>` for debugging. Syncing it pushes the local PID to other hosts, where the PID either doesn't exist or refers to an unrelated process. The flock itself is in-kernel and never crosses hosts; only the on-disk debug content moves, and it moves misleadingly. |
| `agents/*/.staging-*/`, `agents/*/.dream-staging/` | Transient. Multi-step pipelines (dream batches, cascade lease claims) stage intermediate state under dot-prefixed dirs that get torn down on completion. A crashed run leaves a recoverable staging area; the framework cleans it up on next start. Syncing transient state across hosts creates phantom resumability that doesn't exist. |
| `agents/*/_dashboard/` | The dashboard HTML is rendered from logs + outcomes by `atomic_agents/dashboard/render.py`. It's self-contained (inline CSS, no JS dependencies, no external assets — see [§ Dashboard self-containment](#_dashboardindexhtml-self-containment) below). Rendered on the host that runs the framework; readable on any device that has the file. If you want phone-readable dashboards, render on the host then serve via Obsidian Sync — but only if exactly one host renders. Two hosts rendering with sync turned on will overwrite each other constantly. |
| `_migrations/snapshots/` | Snapshot tarballs taken by `atomic_agents.migrate` before applying a schema migration. Used for rollback; only needed on the host that ran the migration. Each snapshot is large (the entire affected slice of the vault, tar+gzip). Don't sync. |
| `agents/*/**/.*.tmp` | Crashed `atomic_write` temp files. Cleaned up by `atomic_agents/_io.py:cleanup_stale_tempfiles()` or the lint pass. If they leak into sync, they look like phantom files on other hosts. |

### What you DO want to sync

- `agents/*/persona/` — the operator edits these from any device; the
  framework reads them on every run. This is the whole point of an
  Obsidian-backed deployment.
- `agents/*/tools.md`, `agents/*/model.md`, `agents/*/mcp.md` — config
  files. Edit on the host where possible (paths in `mcp.md` are often
  host-local); read everywhere.
- `agents/*/memory/*.md` and `agents/*/memory/INDEX.md` — the live
  memory notes. The phone reads them; the framework writes them.
- `agents/*/wiki/` — same posture as memory.
- `agents/*/journal/` — narrative entries. Operator may want to add
  notes from the phone; framework appends from the host.
- `agents/*/goal.md` (if present) — goal state.

### Other sync tools

If you're on a different sync mechanism, translate the ignore patterns:

| Sync tool | Ignore-file path | Pattern syntax |
|---|---|---|
| **Obsidian Sync** | `<vault>/.obsidian/sync.ignore` | gitignore-style |
| **Git** | `<vault>/.gitignore` | gitignore-style — same patterns work verbatim |
| **iCloud Drive** | No exclusion file. Append `.nosync` to dir names: `log.nosync/`. The framework writes `log/`, so this is awkward; symlink trick or move `log/` outside the vault. |
| **Dropbox** | Per-folder via the Dropbox UI ("don't sync this folder") or `dropbox exclude add` CLI. No central ignore file. |
| **Syncthing** | `<vault>/.stignore` | Custom — see Syncthing docs. Most gitignore patterns translate. |
| **OneDrive / Google Drive** | Per-folder GUI toggles. No central ignore file. |

For everything except Obsidian Sync and git, "ignore" means "exclude
specific folders from sync"; you may need to exclude each agent's
`log/` individually rather than via a glob.

---

## `.obsidian/` config dir handling

The Obsidian app stores per-vault config (themes, plugins, hotkey
maps, workspace state) under `<vault>/.obsidian/`. This directory
appears inside the vault root, and the framework walks the vault for
content. Two questions:

1. **Does the framework pick up `.obsidian/` files as memory notes?**
2. **Should `.obsidian/` sync?**

### Walker behavior — `.obsidian/` is safe by default

The framework's content walkers are scoped narrowly: they read from
specific named subdirectories (`memory/`, `wiki/`, `journal/`,
`log/`), not the agent root or vault root. Concretely:

- `atomic_agents/memory/filesystem.py` reads memory via
  `self._memory_dir.glob("*.md")` — a non-recursive glob restricted to
  `memory/`. A `.obsidian/` directory at the vault root or at the
  agent root is never reached.
- `atomic_agents/agent.py:_load_recent_journal()` uses
  `journal_dir.rglob("*.md")` but `journal_dir` is the agent's
  `journal/` subdirectory; the walker can't escape it.
- `atomic_agents/migration/filesystem.py:_iter_units()` uses `rglob` from
  `agents_root` but applies a `_is_excluded(path)` filter that skips
  any path with a component starting with `.` (dotdir filter) or
  matching `EXCLUDED_DIRS = {"_dashboard", "_migrations", "_cache",
  "node_modules", ".git", ".pytest_cache", "__pycache__"}`. The
  dotdir filter is what catches `.obsidian/` — and `.trash/`,
  `.git/`, `.DS_Store`, etc. — without needing to enumerate them.

The practical outcome: **as long as `.obsidian/` sits at the vault
root (or any path outside the agent's content dirs), the framework
ignores it**. Don't nest `.obsidian/` directly inside an agent root
(e.g., `agents/caldwell/.obsidian/`) — that would still be skipped by
the dotdir filter, but it's a confusing layout and risks future
changes to the walker logic.

If you've put your `<agents_root>` *inside* the Obsidian vault rather
than alongside it, this is fine: Obsidian creates exactly one
`.obsidian/` at the vault root, not one per subdirectory.

### Should `.obsidian/` sync?

Mostly yes, with two carve-outs:

- **`.obsidian/workspace.json`** — local UI state (which pane is open,
  cursor positions). Syncs but creates noise; Obsidian Sync handles
  this gracefully by default. Most operators leave it on.
- **`.obsidian/plugins/<plugin>/data.json`** — some community plugins
  store credentials or device-local state here. Audit before
  enabling sync if you use credentialed plugins.

Obsidian Sync has its own granular controls for the `.obsidian/`
directory; the framework has no opinion. The framework's only concern
is that `.obsidian/` files don't end up parsed as agent memory, and
the dotdir filter guarantees that.

### `.trash/` exclusion

Obsidian's soft-delete moves notes to `<vault>/.trash/` rather than
unlinking them. Same story as `.obsidian/`: the dotdir filter keeps
trashed notes out of the agent walker, so a trashed note never
reappears as a memory entry. Do NOT add `.trash/` to your sync
ignore list — Obsidian Sync needs it so trash state propagates to
other devices and `.trash/` can stay coherent. The framework just
won't read it.

If you ever decide to flatten `.trash/` (Obsidian's "Empty trash"
action), do it from one device — multiple devices emptying trash
simultaneously creates spurious conflict copies. Same caution applies
to bulk renames inside the vault.

---

## Sync race conditions and the `AgentLock` mechanism

The most operationally interesting interaction in an Obsidian-backed
deployment is what happens when Obsidian Sync writes a file from
another device while the framework holds an `AgentLock` on this host.
This section is the honest version of what happens.

### What `AgentLock` actually does

`atomic_agents/_locks.py` implements per-agent exclusion via
`fcntl.flock` on `<agent_root>/.lock`. The lock is acquired before any
vault write and released on completion or exception:

```python
# atomic_agents/_locks.py:54-77 (paraphrased)
def acquire(self) -> None:
    self._fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    while True:
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # write pid + timestamp into the lock file for debugging
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise AgentLockBusy(...)
            time.sleep(self.poll_interval)
```

Key properties:

- **In-process exclusion only.** `fcntl.flock` is enforced by the local
  OS kernel. Two `atomic-agents run caldwell` invocations on the *same
  host* serialize correctly. Two invocations on *different* hosts both
  acquire successfully because the kernel state isn't shared.
- **Crash-safe.** The OS releases the flock when the process dies;
  there's no manual unlock step required for crash recovery.
- **The on-disk content is debug-only.** The `pid=` / `acquired=`
  bytes written to `.lock` are for `ps`-style debugging, not for the
  exclusion mechanism. Removing the file does NOT release the lock
  (the flock is on the open file descriptor, not the name).

In `atomic_agents/agent.py:582` the lock is acquired with
`wait_seconds=30` for skill-triggered runs and `wait_seconds=0` for
cron-triggered runs. Cron fails fast (next cycle picks it up); skills
wait briefly because an operator is watching.

### The race window

Now picture this:

1. **Host A** runs `atomic-agents run caldwell`. It acquires the
   `.lock`, starts the LLM call, eventually writes a memory note
   via `_io.atomic_write` (temp-file + fsync + rename + parent fsync).
2. **Phone**, meanwhile, edits `persona/SOUL.md` and saves. Obsidian
   Sync uploads the change to the cloud.
3. **Host A** is in the middle of its run. Obsidian Sync on Host A
   downloads the new `SOUL.md` and overwrites the local file.
4. **Host A** finishes its run, releases the lock.

What did the agent on Host A see? It depends on *when* in the run
Sync wrote `SOUL.md`:

- **Before persona was loaded into the system prompt** (Sync was fast):
  the new SOUL.md got picked up. Working as intended.
- **After persona was loaded into the system prompt** (Sync was slow):
  the agent ran with the OLD SOUL. The new SOUL is on disk but the
  run already cached the old text in-memory. The next run picks up the
  new version. One run "behind"; not corruption.
- **In the middle of writing a memory note**: `_io.atomic_write` does
  temp-file + rename, so the *agent's write* is atomic — Sync can't
  see a half-written note. But Sync could overwrite an *other* file
  in the agent root while the run is mid-call. That file gets read as
  whatever was on disk at the moment the loader touched it; later in
  the same run, no one re-reads it.

The pathological case is **a Sync write that lands inside the window
where the framework has already decided what to write but hasn't
actually written yet**. Concretely: agent decides to update
`memory/feedback_communication_style.md`, snapshots the prior content
to `.versions/feedback_communication_style/<ts>.md`, calls
`atomic_write` to lay down the new content. If Sync wrote a new
version of that note from the phone *between snapshot and write*, the
snapshot captures the prior-prior version (what was on disk before the
phone's edit) and the framework's write overwrites the phone's edit
without the snapshot reflecting it.

**This window is small** (low milliseconds — atomic_write is fast)
but non-zero. The framework's `MemoryPreconditionFailed` mechanism
(see `_capture.py:_check_precondition`) helps here when the caller
passes an `expected_content_sha256` — the write refuses if the file's
current SHA doesn't match what the caller saw at decision time. The
default `agent.call()` path doesn't always pass this — see the
memory-versioning section of `spec/02-atomic-memory.md` for which
operations are precondition-guarded.

### Operator-visible symptoms

You don't need to memorize the race semantics — you just need to
recognize the symptoms:

| Symptom | Likely cause | Recovery |
|---|---|---|
| `AgentLockBusy: Agent lock at /.../.lock held by another process; waited 30s` | Two runs of the same agent on the *same host* overlapped. Sync is not involved. | Wait, retry. If it keeps recurring without an obvious cause, see [`disaster-recovery.md`](disaster-recovery.md) for stale-lock recovery. |
| Run reports old persona on a fresh edit-from-phone | Sync hadn't completed when the run started. | Re-run. The next invocation reads the synced file. |
| Two memory notes have the same date suffix in `.versions/<stem>/` with different content | Cross-host run race + manual phone edit landed in the same second. | Inspect both versions, keep the right one, manually delete the stray. |
| Phone shows note A; host run wrote note B; subsequent read shows note A again | Phone edit landed after host wrote — last-write-wins resolved to the phone. | Re-run; agent re-derives note B. |
| `MemoryPreconditionFailed: expected sha256 ... got ...` | Framework detected mid-write divergence (the file changed between read and write). | The write was refused — your data is safe. Inspect the file; re-run if needed. |

The honest summary: **the framework is robust to Sync writing files
during a run, but it doesn't coordinate with Sync.** If you need
hard guarantees that Sync isn't writing while a run executes, pause
Sync on every host that touches the vault, run, then resume — but
that defeats the point of an Obsidian deployment.

The pragmatic recommendation: **run agents on one host at a time**.
The agent vault is the source of truth; reads from other devices
(your phone, your laptop) are fine. Concurrent *writes* from
multiple devices is where the rough edges live. For real multi-host
runs, atomic-agents now ships `RedisLockBackend` (#60, shipped
2026-05-15) — set `ATOMIC_AGENTS_LOCK_BACKEND=redis` +
`ATOMIC_AGENTS_LOCK_BACKEND_URL=redis://...` and concurrent
`agent.call()` across hosts coordinates via Redis advisory locks
instead of POSIX `fcntl.flock`. See `spec/21-lock-backend.md` for
the full operator surface (env vars, constructor kwarg,
`doctor.check_lock_backend` coherence check). Filesystem-default
(single-host) deployments are unchanged.

### Lock recovery

If a process crashes and the OS doesn't release the lock cleanly (rare
on macOS / Linux — usually the OS releases on `SIGKILL` too), the
recovery is documented in [`disaster-recovery.md`](disaster-recovery.md).
Short version: `lsof <agent_root>/.lock` to find the holder; if no
process owns it, the next run will acquire on its own. **Never
manually `rm .lock` to "unstick" the framework** — if a process really
does hold the lock, removing the file doesn't release the flock and
you've created a phantom lock state.

---

## `_dashboard/index.html` self-containment

The cost / activity / quality dashboard renders to
`<agent_root>/_dashboard/index.html`. This is a single self-contained
HTML file — inline CSS, no external assets, no JavaScript dependencies
fetched at runtime. The contract is documented in the module docstring:

> `atomic_agents/dashboard/render.py:7-9`
> Self-contained output: inline CSS, no external assets, no JavaScript
> dependencies. Opens in any browser. Refresh button only does anything
> when the optional Flask server (serve.py) is running.

For an Obsidian deployment, this property is load-bearing in three
ways:

1. **It's safe to sync.** Self-contained means the file isn't reaching
   out to `localhost:5000` or a bundled-JS folder that won't exist on
   the phone. A synced `index.html` opens correctly on iOS Safari,
   Android Chrome, or any other device that has the file.
2. **It's regeneratable.** The dashboard is derived from
   `log/*.jsonl` + the outcomes / dreams / goal artifacts. Lose the
   HTML, re-render from the source data. This is why the
   recommended sync-ignore list excludes `_dashboard/` by default —
   if you need it on the phone, render on the host then either sync
   one direction or copy manually.
3. **It's safe to read while the framework is running.** The
   atomic-write pattern (`_io.atomic_write`) used to lay down the
   HTML means readers see either the prior version or the new one —
   never a half-rendered file.

The contract: don't add an external dependency (a CDN-fetched JS lib,
a webfont reference, a `<link rel="stylesheet" href="...">` to a
sibling file) to the dashboard template. If the dashboard needs JS,
inline it. If it needs CSS, inline it. The render module's docstring
locks this in — pre-merge review enforces it.

### When to sync the dashboard

Three sensible postures:

| Posture | When to use | Trade-off |
|---|---|---|
| **Never sync `_dashboard/`** (default) | You read the dashboard on the host that renders it (most operators). | Phone can't see the dashboard. |
| **Sync `_dashboard/`, render on one host only** | You want phone-readable dashboards and you run the framework on exactly one host. | Two hosts rendering with sync on = constant overwrites. |
| **Don't sync `_dashboard/`, run a small Flask server** | You want live dashboards across devices. | You're running a server; that's more infra than most home deployments want. |

The default (don't sync) is right for most setups. Override only if
you genuinely need phone-readable dashboards AND you run the
framework from exactly one host.

---

## Conflict copy handling

Sync mechanisms resolve write-write conflicts differently. The
framework's lint pass detects orphan files; what your sync mechanism
produces and what to do with it varies:

| Sync tool | Conflict copy name | Recovery |
|---|---|---|
| **Obsidian Sync** | Resolves silently in most cases; surfaces conflicts in the Sync log with manual-review prompts. No automatic conflict-suffix files. | Open the Sync log, choose which version to keep per file. |
| **Dropbox** | `<original> (<host>'s conflicted copy YYYY-MM-DD).md` | Inspect both, keep the right one, delete the conflict copy. Memory backend's lint pass surfaces these as orphan notes (not in INDEX). |
| **iCloud Drive** | `<original> 2.md` or `<original> (1).md` | Same recovery as Dropbox. iCloud's numbering is less informative; check timestamps to find the loser. |
| **Syncthing** | `<original>.sync-conflict-<timestamp>-<deviceid>.md` | Same. Syncthing's naming is the most informative — device ID tells you which host produced the conflict. |
| **Git** | Merge conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) inside the file | Resolve manually like any git conflict; commit the resolved version. |

### What the framework does

`atomic_agents/dashboard/quality.py` (the lint pass — wired to
`atomic-agents lint`) scans `memory/` and `wiki/` for files that aren't
referenced in `INDEX.md`. Conflict copies appear as orphans because
they have new filenames the INDEX doesn't know about. The lint pass
reports them; it does NOT auto-delete (this is an operator decision —
the "loser" of the conflict may contain content the operator wants).

### Recovery flow for an Obsidian Sync conflict

Obsidian Sync's conflict UI is the canonical resolution path:

1. Open Obsidian on the host running the framework.
2. Click the Sync status icon → review conflicts.
3. For each conflicted file, pick "keep local" or "keep remote".
4. Run `atomic-agents lint --agent <name>` to confirm no orphans
   remain.
5. If a conflict landed inside `memory/.versions/`, the safest move is
   to leave it — version files are append-only by design and a
   conflict there is information-preserving even if duplicative.

If you're using a sync mechanism that produces conflict-copy files,
the recovery is mechanical: read both files, decide which one is
right, delete the other, and re-run `lint` to clear the orphan report.

---

## Worked example — first-run Obsidian deployment

End-to-end, from "I have an Obsidian vault" to "Caldwell runs its
first call." Assumes you've already installed the package per
[`../getting-started.md`](../getting-started.md).

### 1. Decide your vault layout

You have an Obsidian vault at `~/ObsidianVault/`. You want agents
under `~/ObsidianVault/agents/`. Set the env var so the framework
finds it:

```bash
echo 'export ATOMIC_AGENTS_ROOT="$HOME/ObsidianVault/agents"' >> ~/.zshrc
source ~/.zshrc
mkdir -p "$ATOMIC_AGENTS_ROOT"
```

Verify the path resolves where you expect:

```bash
python -c "from atomic_agents._platform import get_agents_root; print(get_agents_root())"
# /Users/you/ObsidianVault/agents
```

### 2. Copy the Caldwell sample

```bash
# From the repo checkout
cp -r docs/samples/caldwell "$ATOMIC_AGENTS_ROOT/caldwell"

# Confirm Obsidian sees it (the vault auto-detects new files)
ls "$ATOMIC_AGENTS_ROOT/caldwell"
# persona/  tools.md  model.md  memory/  journal/  log/  ...
```

### 3. Personalize the persona

Open the vault in Obsidian. Navigate to
`agents/caldwell/persona/USER.md` and edit it with your own
information — name, role, communication preferences, anything Caldwell
needs to know about you. The Caldwell sample's USER.md is a
placeholder; replace it with your reality.

You can do this on the phone, on the laptop, or on the host running
the framework. Save. Sync propagates.

### 4. Set up sync exclusions

In Obsidian on your primary device:

1. Settings → Sync → click the gear next to "Sync".
2. Open the "Selective sync" panel or edit `.obsidian/sync.ignore`
   directly (the file is at `~/ObsidianVault/.obsidian/sync.ignore`).
3. Paste the [ignore block from §"Sync ignore patterns"](#obsidian-sync--obsidiansyncignore)
   above.
4. Wait for Sync to confirm the ignore-rules update propagated. The
   Sync status icon shows when the device is current.

### 5. Set your API key

Per [`../getting-started.md`](../getting-started.md#2-set-your-api-key) —
env var, Keychain, or `~/.config/atomic_agents/keys.json`. The key
file stays on the host; nothing about API keys belongs in the synced
vault. (If you're ever tempted to put a key in `model.md` or
`tools.md`: don't. Use Keychain.)

### 6. Verify the install with `doctor`

```bash
atomic-agents doctor --agent caldwell
```

Exit 0 = ready. Exit 1 = one or more checks failed; the output prints
the literal command needed to fix each one. The `vault` check
specifically verifies that the layout matches `spec/01-anatomy.md` —
if your Obsidian copy got mangled (a `.md` file ended up renamed to
`.md 2` because of an iCloud sync conflict, for example), this is
where you find out.

If you're on a pre-v0.10 release that doesn't ship `doctor`, fall
back to:

```bash
atomic-agents info caldwell
atomic-agents run caldwell --work-item "ping"
```

Both should succeed.

### 7. First real run

```bash
atomic-agents run caldwell --work-item "introduce yourself, briefly"
```

Watch the JSONL log:

```bash
tail -f "$ATOMIC_AGENTS_ROOT/caldwell/log/$(date +%Y-%m)/$(date +%Y-%m-%d).jsonl"
```

You should see one record with `status:"ok"`, input/output token
counts, and a `summary` field.

### 8. Test the phone-edit flow

On your phone, open Obsidian:

1. Navigate to `agents/caldwell/persona/SOUL.md`.
2. Add a line under "Voice": "*Test edit from phone — DELETE ME*".
3. Save. Wait for Sync to confirm.

On the host, re-run:

```bash
atomic-agents run caldwell --work-item "what did I just edit in your SOUL?"
```

If the agent's response references your test edit, end-to-end sync is
working. Remove the test line from your phone, sync again, you're
done.

### 9. Schedule the cron (optional)

If Caldwell should run autonomously on a schedule, see
[`../implementation/cron-agent.md`](../implementation/cron-agent.md)
for the LaunchAgent / systemd / cron recipes. The cron runtime
respects `ATOMIC_AGENTS_ROOT` the same way as the CLI — set it in the
LaunchAgent's `EnvironmentVariables` plist key so the scheduled
process sees the same vault as your interactive sessions.

---

## Cross-platform considerations

The framework is Python. Obsidian is cross-platform. These don't have
the same reach.

### What runs where

| Platform | Can run the framework? | Can edit the vault via Obsidian? | Can read rendered dashboards? |
|---|---|---|---|
| **macOS** | Yes — primary reference platform | Yes | Yes |
| **Linux** | Yes | Yes | Yes |
| **Windows** | Yes (limited testing) | Yes | Yes |
| **iOS** | No (no Python runtime accessible to user processes) | Yes — Obsidian Mobile | Yes — synced HTML opens in Safari |
| **Android** | Limited (Termux works for advanced users; not a supported config) | Yes — Obsidian Mobile | Yes |

The shape that works: **one host runs the framework; all other devices
read and edit the vault via Obsidian Sync**. The phone never executes
`atomic-agents run`; the host does. The phone's role is "edit
persona, read memory + journal + dashboard, maybe add a journal note."

This is the natural fit for the framework's design. The vault is the
source of truth (per `docs/architecture.md`'s "The vault is the
source of truth" principle), the runtime is stateless, and any host
with the package installed can pick up an agent vault and run it.
Multi-device sync without coordination is fine as long as exactly one
device executes at a time.

### What if I want multi-host runs?

A laptop on the road, a Mac mini at home. The laptop should run a
quick interactive Caldwell session; the Mac mini runs the nightly
cron. Both share the synced vault.

This works IF:

1. Both hosts have the package installed and the same model.md /
   tools.md / mcp.md interpretation. (They share `model.md`, so this
   is automatic; `mcp.md` may need host-specific paths — see below.)
2. Runs don't overlap. Schedule the cron at night when the laptop is
   closed; run interactive sessions during the day.
3. `mcp.md` server commands resolve on both hosts. If one MCP server
   points at `~/dev/myserver`, that path must exist on both hosts —
   or you split `mcp.md` per host (see [§ Host-specific config
   under sync](#host-specific-config-under-sync) below).

The fcntl lock won't protect you across hosts (it's local-OS-only).
Your discipline does. If you've layered a `LockBackend` on Postgres /
Redis once that ships (per roadmap), the framework can coordinate.
Today, treat overlap as operator-managed.

### Host-specific config under sync

A few config files are awkward under cross-host sync because they
encode host-local paths:

- **`mcp.md`** — `command:` paths often point at host-local
  binaries (`~/dev/my-mcp/bin/server`). If your Mac mini path
  differs from your laptop path, the file can't be both.
- **`tools.md`** — usually fine; the paths inside `tools.md` are
  resolved via `Path.expanduser()` (the framework's `expand()` helper
  in `atomic_agents/_platform.py`), which expands a leading `~` to the
  user's home directory. **`$HOME` and other environment-variable
  references in tools.md paths are NOT expanded** — only the literal
  `~` prefix.
- **`model.md`** — fully portable. No host-specific content.

If `mcp.md` paths really do differ per host, two options:

1. **Use `~`-prefixed paths in `mcp.md` arg lists** — the framework
   expands `~` via `Path.expanduser()` in `atomic_agents/mcp.py`. If
   the operator account name is the same on both hosts (e.g.,
   `/Users/operator` on macOS and `/Users/operator` on the laptop),
   `~/dev/my-mcp/bin/server` resolves correctly on each. **`$HOME` is
   NOT expanded inside `mcp.md` arg paths**; use `~` instead. The
   `env:` section IS different — values starting with `$` (e.g.,
   `KEY=$ANTHROPIC_API_KEY`) are resolved against `os.environ` at load
   time, so secrets-via-env work as expected.
2. **Maintain `mcp.<host>.md` and symlink** — keep `mcp.macbook.md`
   and `mcp.macmini.md` in the synced vault; each host has a local
   symlink `mcp.md → mcp.<hostname>.md`. The framework reads `mcp.md`;
   the symlink is host-local and doesn't sync (Obsidian Sync doesn't
   sync symlinks). This is the escape hatch when paths genuinely
   diverge (different OSes, different account names, different binary
   install locations).

The first option is simpler. Reach for the second only when the
first doesn't fit.

---

## Common gotchas

A short list of things that catch operators in the first week:

- **Forgetting `ATOMIC_AGENTS_ROOT` in cron.** The CLI inherits your
  shell's env vars; a LaunchAgent does not. Set the env var in the
  `.plist` explicitly. See
  [`../implementation/cron-agent.md`](../implementation/cron-agent.md).
- **Syncing `.lock`.** If you didn't paste the ignore block, the lock
  file syncs from one host to another with stale PID content. The
  framework still works (the flock itself doesn't sync), but the on-disk
  debug content lies. Fix: add `agents/*/.lock` to your ignore file.
- **Editing `memory/INDEX.md` from the phone mid-run.** The framework
  rewrites this file frequently. Hand-edits race with framework
  writes. Edit memory notes from the phone; let the framework own the
  INDEX.
- **Trusting the dashboard on a non-rendering host.** If the
  dashboard isn't syncing (recommended), the phone sees no
  `_dashboard/`. That's expected; render on the host.
- **Running two interactive sessions on the same host with `wait_seconds=30`.**
  Sometimes a forgotten REPL session holds the lock. `AgentLockBusy`
  after 30s usually means you have a stuck Python process. `ps aux |
  grep atomic-agents`, kill it, retry.
- **Putting API keys in the synced vault.** Never. Keychain on
  macOS; `~/.config/atomic_agents/keys.json` (chmod 600) elsewhere.
  The synced vault is not encrypted at rest unless Obsidian Sync's
  E2E option is enabled, and even then it's the wrong place for
  secrets.

---

## Going further

- [`programmatic.md`](programmatic.md) — embedding the framework in
  Python, the complete public exception table, and `AgentLockBusy`
  semantics referenced in the lock-race section above.
- [`disaster-recovery.md`](disaster-recovery.md) — symptom-organized
  runbook for stale locks, mid-run crashes, corrupted INDEX,
  Obsidian-Sync-specific conflict recovery, and `.versions/`
  snapshot management.
- [`cost-guardrail-sizing.md`](cost-guardrail-sizing.md) — picking
  daily / monthly caps when an Obsidian-backed agent runs on a
  multi-device sync layout.
- [`versioning.md`](versioning.md) and
  [`upgrading.md`](upgrading.md) — SemVer policy and operator
  upgrade runbook.
- [`../appendix/portability.md`](../appendix/portability.md) — the
  framing for "you don't need Obsidian; this is just one shape."
- [`../spec/01-anatomy.md`](../spec/01-anatomy.md) — the canonical
  agent file layout this doc layers Obsidian semantics on.
- [`../spec/03-file-formats.md`](../spec/03-file-formats.md) — the
  frontmatter contracts and naming conventions used inside the vault.
- [`../spec/04-runtime-assembly.md`](../spec/04-runtime-assembly.md) —
  how the framework assembles a run from the vault contents; useful
  when reasoning about what the framework reads vs writes.
- [`disaster-recovery.md`](disaster-recovery.md) — stale lock
  recovery, vault corruption recovery, lost-snapshot recovery. The
  authoritative source for "something went wrong, what do I do."
- [`versioning.md`](versioning.md) and [`upgrading.md`](upgrading.md) —
  the release lane. An Obsidian deployment upgrades the same way a
  non-Obsidian one does; the sync layer is transparent to the
  upgrade.

---

## What's NOT covered here

To keep scope honest:

- **Obsidian plugin development** — a future first-party "Atomic
  Agents" plugin that surfaces the dashboard, captures, and goal
  state inside Obsidian itself is on the roadmap as a separate item.
  This doc is about running the framework against an Obsidian-backed
  vault, not extending Obsidian.
- **Non-Obsidian sync tools in depth** — git, Dropbox, iCloud,
  Syncthing each have their own quirks. The ignore-pattern table
  above is a starting point; the specific sync tool's docs are the
  authority for its semantics.
- **Multi-tenant Obsidian Sync** — running one Obsidian vault shared
  across multiple humans, each editing different agents. Theoretically
  works; not tested. The fcntl lock + agent-root scoping support it
  in principle, but no operator has run this shape yet.
- **iOS Shortcuts integration** — a Shortcut that prepends to a
  journal entry, triggers a run on the host via SSH, etc. Possible
  with [`a-shell`](https://github.com/holzschu/a-Shell) or a
  Telegram-bot bridge; out of scope for this doc.

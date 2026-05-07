# 06 — Multi-Agent Projects

How to compose multiple Atomic Agents under a project umbrella when one agent isn't enough.

---

## Two scopes, recap

- **Agent scope** — persona, atomic memory, journal, tools, model. The Atomic Agent spec.
- **Project scope** — shared canon, style guide, policy, work queue. Sits *above* multiple agents.

Most agents are single-agent (Bishop, Caldwell, Harper, Paul). They use only agent scope; project scope is collapsed.

A few systems are multi-agent (Muse on The Unfinished, future systems). Multiple Atomic Agents share a project layer above them.

---

## When to use a multi-agent project

Use multi-agent only when ALL of:

✅ **Genuine role specialization** — each agent does a job another can't easily absorb (Director ≠ Writer ≠ Editor ≠ Visual ≠ Artist)

✅ **Shared world** — there's content (canon, style, policy) that all roles must agree on. Diverging interpretations break the system.

✅ **Workflow with handoffs** — agents pass artifacts between each other (Outliner → Writer → Editor → Director)

✅ **Cost or latency reason** — you want different models per role (e.g., cheap for Outliner, premium for Editor)

If the workflow is "one agent does everything," stay single-agent. Multi-agent adds coordination overhead; only adopt it when the specialization wins back the cost.

**Don't go multi-agent for**:
- ❌ "Different topics" — Caldwell handles money, Harper handles Highland. They don't share a world. Each is its own single-agent.
- ❌ "Pipeline of steps" — if agents don't really need separate identities, just write a Python script with multiple LLM calls.

---

## Project layout (single-project, no shared roles)

When you have one project and don't expect a second one using the same roles:

```
~/docs/agents/{system_name}/
├── canon.md                       ← shared world / facts all roles agree on
├── style_guide.md                 ← shared style rules (anti-slop, tone, etc.)
├── policy/                        ← locked project decisions
│   ├── lock_001_genre.md
│   ├── lock_002_pov.md
│   └── ...
├── queue/                         ← work items roles pick up
│   ├── pending/
│   ├── in_progress/
│   └── completed/
└── agents/
    ├── director/
    │   └── (full Atomic Agent layout — persona/, tools.md, model.md, memory/, etc.)
    ├── writer/
    ├── editor/
    └── ...
```

Each subdirectory under `agents/` is a complete Atomic Agent — full persona, memory, wiki, journal, log, tools.md, model.md.

This works fine for one-off multi-agent systems. But if you'll have multiple projects sharing the same roles, see the next section.

---

## Project layout (multiple projects sharing roles) — the three-layer cascade

When you have **multiple projects using the same roles** (e.g., Muse: a Director, Writer, Editor on every fiction project), copying the entire role definition into each project produces duplication that drifts.

The fix: a **three-layer cascade** with shared role templates.

### Layout

```
~/docs/agents/{system_name}/
├── roles/                         ← LAYER 1: shared role definitions ("the class")
│   ├── director/
│   │   ├── PROMPT.md              ← stable job mechanics (output formats, queue, hard lines)
│   │   ├── tools.md               ← role-level tool allowlist
│   │   └── model.md               ← role-level model + budget
│   ├── writer/
│   ├── editor/
│   └── ...
└── projects/
    └── {project_name}/             ← e.g., the-unfinished
        ├── canon.md                ← LAYER 2: project shared (the world all roles see)
        ├── style_guide.md
        ├── policy/
        │   └── *.md
        ├── queue/
        │   ├── pending/
        │   ├── in_progress/
        │   └── completed/
        └── agents/                 ← LAYER 3: project × role instances
            ├── director/
            │   ├── persona/
            │   │   ├── IDENTITY.md ← THIS director ON THIS project
            │   │   ├── SOUL.md     ← personality evolves per-project
            │   │   └── USER.md     ← Dan slice (or symlink to global)
            │   ├── memory/
            │   ├── wiki/
            │   ├── journal/
            │   ├── log/
            │   └── tools.md        ← OPTIONAL — overrides role-level if present
            ├── writer/
            └── editor/
```

### What lives at each layer

| Layer | Files | Edited when | Lifecycle |
|---|---|---|---|
| **Role (shared)** | `roles/<role>/{PROMPT.md, tools.md, model.md}` | The role itself changes (rare) | Stable |
| **Project (shared)** | `<project>/{canon.md, style_guide.md, policy/, queue/}` | The project's world or rules change | Per-project |
| **Instance** | `<project>/agents/<role>/{persona/, memory/, wiki/, journal/, log/}` | The role's persona/memory on this project evolves | Per-project, per-role |

### Why three layers, not two

Without a role layer:
- Same Director's job mechanics get copy-pasted into every project's `agents/director/PROMPT.md`
- Update the role mechanics → edit N files
- One file drifts → that project's Director behaves differently than the others

With a role layer:
- One source of truth for role mechanics
- Update once, applies everywhere
- Per-project persona stays per-project (correctly)

### Why instance still needs its own IDENTITY/SOUL/USER

The role layer holds *what the role does* (mechanics, tools, model). The instance layer holds *who this role is on this specific project* (persona, voice, accumulated learning).

A Writer on a noir thriller project has different SOUL.md than a Writer on a sci-fi epic. Same job mechanics; different voice. The split makes that explicit.

### Runtime assembly with cascade

```
[1]  ROLE      roles/<role>/PROMPT.md          ← who I am as a Writer in general
[2]  INSTANCE  agents/<role>/persona/IDENTITY  ← who I am as Writer ON THIS PROJECT
[3]  INSTANCE  agents/<role>/persona/SOUL      ← my voice on this project
[4]  INSTANCE  agents/<role>/persona/USER      ← about Dan
[5]  ROLE      roles/<role>/tools.md           ← what my role can touch (general)
[5b] INSTANCE  agents/<role>/tools.md          ← OPTIONAL override (if file exists, replaces 5)
[6]  ROLE      roles/<role>/model.md           ← model selection
[7]  PROJECT   <project>/canon.md              ← the world all roles share
[7.5] PROJECT  <project>/goal.md               ← OPTIONAL — active project goal (per spec/12)
[8]  PROJECT   <project>/style_guide.md
[9]  PROJECT   <project>/policy/*              ← all locked decisions
[10] INSTANCE  agents/<role>/memory/INDEX.md
[11] INSTANCE  agents/<role>/wiki/INDEX.md
[12]           pinned + recent atomic notes (instance)
[13]           recent journal (instance)
[14]           work item from <project>/queue/ (or for the goal-driven role: next sub-goal)
```

This extends the canonical assembly order from [04-runtime-assembly](04-runtime-assembly.md) with the three layers explicit. Cache breakpoints place naturally between role-layer and project-layer (role rarely changes; project changes more; instance memory changes most).

### Override rules

The cascade rule: **specific wins over general**.

For `tools.md`:
- Instance `agents/<role>/tools.md` exists → use it, ignore role's `roles/<role>/tools.md`
- Instance file doesn't exist → fall back to role's
- Want to extend rather than replace? Use `tools.override.md` at instance level — the loader merges role's tools.md + instance's tools.override.md

For `model.md`:
- Same rule. Per-project model overrides are common (e.g., Writer-on-the-unfinished uses Opus; Writer-on-quick-drafts uses Sonnet).

For role's `PROMPT.md`:
- Generally NOT overridden at instance. If a project needs different mechanics, it probably needs a different role (e.g., split "writer" into "writer-noir" and "writer-scifi" roles, both at the role layer).

### When the cascade is worth it

| Situation | Cascade? |
|---|---|
| Single-agent system (Caldwell, Harper, Paul, Bishop) | NO — flat per-agent layout from [01-anatomy](01-anatomy.md) |
| Multi-agent, single project, no #2 expected | OPTIONAL — flat is fine; cascade pays off only if #2 lands |
| Multi-agent, single project, #2 likely | YES — set up cascade now; one extra dir indirection, zero duplication later |
| Multi-agent, multiple existing projects | YES — duplication pain is real, this is what cascade solves |

For Muse with one current project (The Unfinished) and creative ambition for future projects: cascade now is the call.

### Migration: flat-multi-agent → cascade

If you've already built a flat multi-agent project and now want to add project #2:

1. **Identify the role-layer content** in each existing agent: PROMPT.md, tools.md, model.md (the parts that aren't project-specific)
2. **Hoist** those files to `<system>/roles/<role>/` — one canonical copy
3. **Move project content** to `<system>/projects/<existing_project>/`
4. **Strip per-instance files** of the now-shared content (instance keeps persona, memory, wiki, journal, log)
5. **Build project #2** as `<system>/projects/<new_project>/` with its own canon + per-instance files; reuses roles automatically

Test by running each existing role on the original project; output should be identical to pre-migration. Then build project #2.

### Symlinks vs copy-on-cascade

If instance USER.md is identical across projects (Dan is the same Dan), use a symlink:

```bash
ln -s ../../../USER.md ~/docs/agents/muse/projects/the-unfinished/agents/writer/persona/USER.md
```

Or hoist USER.md to the role layer if it's truly project-invariant. Either works; symlink wins on flexibility.

---

## When project scope is overkill

---

## Project-scope concerns

### `canon.md`

The shared world. Everything every role must know to function consistently.

- Story bible (for fiction projects)
- Domain vocabulary (for technical projects)
- Foundational facts that drive every decision
- Character sheets, world rules, locked plot beats

Loaded at runtime *after* persona and *before* memory INDEXes (insert as step [3.5] in the canonical load order):

```
[1] IDENTITY.md
[2] SOUL.md
[3] USER.md
[3.5] PROJECT canon.md         ← project scope
[3.6] PROJECT style_guide.md   ← project scope
[3.7] PROJECT policy/*         ← project scope (whole dir, all loaded)
[4] tools.md
[5] (model.md)
[6] memory/INDEX.md
... rest as canonical
```

### `style_guide.md`

Shared rules every role applies. For Muse: anti-slop directives, tone, voice consistency, prose patterns to avoid. For technical projects: code conventions, comment policy, naming.

### `goal.md` (project-level — optional)

For multi-agent projects pursuing a persistent objective, the project's goal lives at the project root and is visible to every role in that project's runtime context. Per [12-goals-and-intent](12-goals-and-intent.md) — see that spec for the goal.md format and the goal-driven loop mechanics.

When a project has a goal:
- One role (typically a Director-style role) is goal-driven and decomposes the goal into queue items
- Other roles in the project are reactive — they pick up queue items and handle them
- All roles see the goal in their context (it loads at step [7.5] in the cascaded load order)

Projects without an active goal operate in pure reactive mode — operator (or Director ad-hoc) writes queue items.

### `policy/`

Flat directory of locked decisions. Each file is one decision in the same format as an Atomic Note (frontmatter + body).

```yaml
---
schema_version: 1
name: POV decision — third-person limited from protagonist
description: All scenes use third-person limited from the named POV character
type: decision
captured: 2026-04-12
last_seen: 2026-05-01
sources: [conversation_2026-04-12, panel_review_2026-04-15]
confidence: high
pinned: true
---
```

`policy/` files are typically `pinned: true` because they're foundational to every role's behavior on the project.

This pattern is borrowed from Muse — it works because policy decisions stay small (one fact each), they're all loaded at startup, and they don't change frequently.

If `policy/` grows past ~30 files, switch to the typed Atomic Notes pattern within `policy/` (with its own INDEX).

### `queue/`

Work items moving through the project's workflow. Three subdirs: `pending/`, `in_progress/`, `completed/`.

Each work item is a markdown file with a manifest:

```yaml
---
id: scene_001
type: scene_draft
assigned: writer
status: pending
created: 2026-05-06
deadline: 2026-05-08
inputs:
  - outline_chapter_1.md
  - canon.md
outputs:
  - drafts/scene_001_draft1.md
---
**Brief:** Write the opening scene of chapter 1. Hard constraints: must establish setting,
protagonist, and the inciting incident in <1500 words. Voice per style_guide.md.
Reference `outline_chapter_1` for structure.
```

Roles pick items from their assigned queue. The cron / skill runner handles handoffs.

---

## How roles see each other

By default, **roles read everything in their project**, including peer roles' folders. They're collaborators, not isolates.

| Read | Write |
|---|---|
| Own folder (full) | Own folder ONLY |
| Project canon, style, policy | (no — those are Dan-owned) |
| Project queue (pending → in_progress → completed) | Own work items only |
| Other roles' wiki/ and journal/ | Never |
| Other roles' memory/ | Never (except via shared canon distillation if needed) |

The "writes stay single-threaded" rule from Cognition.ai applies: an agent may *read* a peer agent's vault content, but never write to it. Avoids the consistency hell of concurrent writes.

If two roles need to coordinate via a shared artifact, that artifact lives in `policy/` or `queue/` (project-owned), not in either role's folder.

---

## Cross-role handoffs

Workflow handoffs happen via the queue, not direct agent-to-agent calls.

**Example flow** (The Unfinished, fiction project):

1. Director adds `outline_chapter_5.md` to `queue/pending/` with `assigned: outliner`
2. Outliner picks it up, expands into scene-level beats, moves to `queue/completed/`
3. Director re-queues the output with `assigned: writer`
4. Writer drafts scenes from beats, moves to `queue/completed/`
5. Director re-queues for `assigned: editor`
6. Editor polishes, returns final draft

Each agent's runtime (cron or skill) checks its assigned queue at every invocation. Handoffs are file-system-mediated, not agent-to-agent calls. This makes the system resumable, debuggable, and safe to run distributed.

### Queue handoff mechanics — locking, leases, retries

The simple "move file from pending/ to in_progress/" pattern hides real concurrency hazards (Codex review, finding #27). Two cron jobs racing to claim the same item, an agent crashing mid-work, retries that loop forever. The full mechanism specified:

#### Work item frontmatter (extended)

```yaml
---
id: scene_001
type: scene_draft
assigned: writer
status: pending          # pending | in_progress | completed | failed | dead_letter
created: 2026-05-06
deadline: 2026-05-08

# Set when an agent claims the item
claimed_by: null         # agent_name + process_id, e.g. "writer:78912"
claimed_at: null         # ISO 8601 timestamp
lease_expires_at: null   # ISO 8601 — when this claim becomes stale

# Retry tracking
attempt: 0
max_attempts: 3
last_error: null         # populated on failure

# Outputs
inputs:
  - outline_chapter_5.md
  - canon.md
outputs:
  - drafts/scene_001_draft1.md
---
```

#### Claim protocol (atomic, race-safe)

To claim a work item from `pending/`:

```python
def claim_item(agent_name: str, item_id: str) -> WorkItem | None:
    """Atomically move pending/<id>.md → in_progress/<id>.md.
    Returns None if another agent claimed it first.
    """
    pending = queue_dir / "pending" / f"{item_id}.md"
    in_progress = queue_dir / "in_progress" / f"{item_id}.md"

    if not pending.exists():
        return None

    # POSIX atomic rename. If another process renamed first, this fails.
    try:
        pending.rename(in_progress)
    except FileNotFoundError:
        return None  # another process won the race

    # Now we own the file — update frontmatter to record the claim
    item = load_work_item(in_progress)
    item.claimed_by = f"{agent_name}:{os.getpid()}"
    item.claimed_at = datetime.now().isoformat()
    item.lease_expires_at = (datetime.now() + lease_duration()).isoformat()
    item.attempt += 1
    save_work_item(in_progress, item)

    return item
```

**The atomic rename is the lock.** POSIX guarantees only one rename succeeds when two processes race. No separate lock file needed — the file *being in `in_progress/`* IS the lock.

#### Lease expiry — recovering crashed work

If an agent crashes mid-work, the file stays in `in_progress/` forever without intervention. Solution: each claim has a **lease duration** (default 30 minutes). After the lease expires, any agent can reclaim the item.

```python
def reclaim_stale(agent_name: str) -> list[WorkItem]:
    """Find items in in_progress/ with expired leases. Reclaim them."""
    reclaimed = []
    for path in (queue_dir / "in_progress").glob("*.md"):
        item = load_work_item(path)
        if item.lease_expires_at is None:
            continue
        if datetime.fromisoformat(item.lease_expires_at) < datetime.now():
            # Lease expired. Reclaim by updating frontmatter.
            item.claimed_by = f"{agent_name}:{os.getpid()}"
            item.claimed_at = datetime.now().isoformat()
            item.lease_expires_at = (datetime.now() + lease_duration()).isoformat()
            # NOTE: don't bump attempt — that's the point of leases. Crashes shouldn't burn retries.
            save_work_item(path, item)
            reclaimed.append(item)
    return reclaimed
```

Lease duration is per-role. A Writer's draft might take 20 minutes; lease 60. An Outliner's pass takes 5 minutes; lease 15. Configure in the role's `model.md` or the project's `queue/config.md`.

For long-running work, the agent should periodically *renew* the lease — same code path as `reclaim_stale` but the same agent extending its own claim:

```python
def renew_lease(item: WorkItem) -> None:
    """Extend the lease while still working on the item."""
    item.lease_expires_at = (datetime.now() + lease_duration()).isoformat()
    save_work_item(in_progress / f"{item.id}.md", item)
```

Renewal cadence: every (lease_duration / 3). For a 60-minute lease, renew every 20 minutes.

#### Completion protocol

When an agent finishes work successfully:

```python
def complete_item(item: WorkItem) -> None:
    """Move in_progress/<id>.md → completed/<id>.md atomically."""
    in_progress = queue_dir / "in_progress" / f"{item.id}.md"
    completed = queue_dir / "completed" / f"{item.id}.md"

    item.status = "completed"
    item.lease_expires_at = None
    item.claimed_by = None
    save_work_item(in_progress, item)

    in_progress.rename(completed)  # atomic
```

#### Failure protocol — retry then dead-letter

When an agent fails (caught exception, model error, validation fail):

```python
def fail_item(item: WorkItem, error: str) -> None:
    """Record failure. Retry up to max_attempts; then move to dead_letter/."""
    in_progress = queue_dir / "in_progress" / f"{item.id}.md"
    item.last_error = f"[{datetime.now().isoformat()}] {error}"

    if item.attempt >= item.max_attempts:
        # Out of retries — dead letter
        item.status = "dead_letter"
        save_work_item(in_progress, item)
        in_progress.rename(queue_dir / "dead_letter" / f"{item.id}.md")
        # Optionally surface to operator — this is "I tried 3 times and gave up"
        notify_dead_letter(item)
    else:
        # Retry — push back to pending/ for next claim attempt
        item.status = "pending"
        item.claimed_by = None
        item.claimed_at = None
        item.lease_expires_at = None
        save_work_item(in_progress, item)
        in_progress.rename(queue_dir / "pending" / f"{item.id}.md")
```

#### Directory layout (extended)

```
projects/<project>/queue/
├── pending/         ← claimable, has work_item.md per item
├── in_progress/     ← actively being worked; has lease info in frontmatter
├── completed/       ← done, archived
├── failed/          ← (optional) — failed but retryable; usually you keep these in pending/
└── dead_letter/     ← exhausted retries; needs operator intervention
```

The four directories are required. `failed/` is optional — most implementations push failures back to `pending/` for retry, only moving to `dead_letter/` when retries are exhausted.

#### Operator queue dashboard

The cost dashboard from spec/09 surfaces queue state per project:

```
The Unfinished — Queue
  Pending:        4 items   (oldest: 2 hours)
  In progress:    2 items   (writer:78912, editor:78104)
  Completed:    142 items   (this month)
  Dead letter:    1 item    ← needs attention
```

Click-through to dead-letter shows which items failed, the last_error frontmatter, and a one-click "retry from beginning" or "abandon" action.

#### What this protects against

| Failure mode | Protected by |
|---|---|
| Two agents claim same item | Atomic rename — only one wins |
| Agent crashes mid-work | Lease expiry → reclaim by next agent |
| Agent stuck in infinite loop | Lease still expires; the *new* attempt may also fail; eventually hits max_attempts → dead letter |
| Item with bad input fails forever | max_attempts → dead letter, surfaced to operator |
| Network partition during claim | Atomic rename either succeeded or didn't; no split-state |
| Operator wants to cancel | Manual move from pending/ or in_progress/ to dead_letter/ |

#### What this does NOT protect against

- **Distributed file systems with weak rename semantics** — NFS, some cloud-mount setups. POSIX guarantees atomic rename on a single filesystem. If your queue is on a remote-mounted filesystem, verify rename atomicity before relying on it. The spec assumes local POSIX; document deviations.
- **Two agents with identical processes on the same machine** — unlikely in practice (each role's cron/skill has a distinct process), but if it happens, both processes might race past `claim_item` simultaneously. Mitigation: the agent's per-agent flock (from shared-helper) prevents this — same lock used for vault writes serializes claim attempts.
- **Operator manually editing items in `in_progress/`** — there's no protection. Don't do that.

---

## Project-level memory promotion

Sometimes a learning is project-wide, not role-specific. Example: Writer captures `feedback_anti_slop_pattern_X.md`. Editor sees the same pattern. Director confirms.

**Promote to project scope** when:

- The same observation has been captured by 2+ roles
- It's a rule about the project's content, not a specific role's behavior

**Process**:
1. Promote to `style_guide.md` or `policy/` (whichever fits)
2. Mark all original atomic notes as `superseded_by: ../style_guide.md#anchor`

This is the project-level analog of single-agent promotion-to-persona.

---

## Pure-canon-loading antipattern (avoid)

It's tempting to dump the entire project canon into every role's IDENTITY.md so each role "knows the world." Don't.

**Why not**: canon evolves. If it's copy-pasted into 7 IDENTITY files, every canon update is 7 file edits, prone to drift.

**Instead**: keep canon in `canon.md` at the project level. Every role's runtime loads `canon.md` automatically as step [3.5]. One source of truth. No drift.

---

## When project scope is overkill

If your "multi-agent project" is really just one agent with multiple capabilities, collapse it. Caldwell doesn't need to be five sub-agents (Tax / Debt / Investment / Income / Spending). One Caldwell with five `wiki/` pages and five flavors of `decision` notes is much simpler.

Multi-agent earns its complexity only when:
- Different roles need different models or different cost profiles
- Roles do meaningfully different work that resists merging
- Coordination overhead of file-mediated handoffs is *less* than the cost of one agent doing it all

If you're not sure, start single-agent. Splitting later is easier than merging.

---

## Migration: single-agent → multi-agent

If a single agent's job grows past one agent's capacity, split:

1. **Identify the natural seams** — where does work hand off from one mode to another?
2. **Extract role A** — copy single agent → `agents/role_a/`. Trim its IDENTITY/SOUL/tools to role A's scope.
3. **Extract role B** — same.
4. **Promote shared content** — anything both roles reference, hoist to `canon.md` / `style_guide.md` / `policy/`.
5. **Build the queue** — set up `queue/` and define handoff types.
6. **Migrate memory** — split atomic notes between roles by topic. When in doubt, copy (memory is cheap).
7. **Test handoff** — run a single work item end-to-end before tearing down the original single-agent.

Reverse migration (multi → single) is simpler: merge folders, dedupe memory, drop the project-level files.

---

## Summary table

| Concern | Single-agent | Multi-agent project |
|---|---|---|
| Persona (IDENTITY/SOUL/USER) | Per agent | Per role |
| Memory + wiki | Per agent | Per role |
| Journal + log | Per agent | Per role |
| tools.md, model.md | Per agent | Per role |
| Shared world / canon | (in IDENTITY) | `canon.md` (project) |
| Style rules | (in SOUL) | `style_guide.md` (project) |
| Locked decisions | `memory/decision_*.md` | `policy/*.md` (project) |
| Workflow handoffs | N/A | `queue/` (project) |
| Cross-role coordination | N/A | File-mediated via queue |

Multi-agent is a strict superset of single-agent. Every multi-agent project contains N single-agent Atomic Agents at its core.

---

*Next: [07-research-foundations](07-research-foundations.md) — citations and lineage.*

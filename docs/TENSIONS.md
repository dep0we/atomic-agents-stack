---
created: 2026-05-09
type: tensions
status: active
last_review: 2026-05-09
---

# Tensions — Atomic Agents

**Companion doc to `ROADMAP.md`.** Where ROADMAP captures *what we want to build*, this captures *the architectural tensions in what's already built* — places the elegance bends, where current decisions create future cost, and what to watch as the framework scales from "a single operator's home agent" to "an organization running an army."

These aren't bugs. They're design tensions, often with no obviously-better answer today. Most resolve themselves naturally as the backend-protocol pattern lands. Some require an explicit decision later. The point of writing them down is so that *future maintainers* and *future contributors* aren't surprised when one of them starts mattering.

> **Living doc — update freely.** When a tension resolves, move it to the "Resolved" section with a date + how it resolved. When a new one appears, add it. When severity changes, update the tier.

---

## Severity tiers

- 🔴 **TODAY** — already creates friction or cost; worth fixing before it compounds
- 🟡 **AT SCALE** — invisible at home-user scale, becomes load-bearing when the framework hits multi-host / multi-tenant / fleet
- 🟢 **AESTHETIC** — design hygiene; would make the codebase nicer but no functional cost

---

## Active tensions

### 🟡 T1. `agent.py:call()` is the choke point for protocol expansion

**One-sentence:** As more backend protocols land (#60–#65), `call()` keeps absorbing inline orchestration logic, and at ~1750 lines it's already where new contributors will struggle to onboard.

**Why load-bearing:** Every backend protocol means another swap point inside `call()`. MemoryBackend (#57) cleanly relocated capture writes to `agent.memory.write_note()`. LockBackend (#60) will move `AgentLock(...)` acquisition. LogBackend (#61) will move `self._log()`. PersonaBackend (#62) will move persona loading. AgentProfileBackend (#63) will move *all of `_load_config`*. Each lift is right; the cumulative effect is that `call()` becomes "thin orchestrator over backends," and at that point splitting into a `RunContext` or `CallSession` object is the natural shape.

**Where:** `atomic_agents/agent.py:544-930` (`call()` method, ~390 lines). File: 1752 lines total.

**When this bites:** As soon as 3 of 6 backend protocols (#60–#65) land. Today, only MemoryBackend is implemented; `call()` is "fine, dense." After LockBackend + LogBackend, it's "ache." After PersonaBackend, it wants surgery.

**What to watch for:**
- Length of `call()` after each backend lands. Trigger: line count >450 OR cyclomatic complexity rising visibly.
- New contributor's "where do I add X?" questions — when the answer is consistently "somewhere in `call()`," that's the signal.
- Test files that have to set up the *full* agent stack to test one slice of `call()`.

**Related:** Issues #60, #61, #62, #63, #64. PR #57 set the pattern.

---

### 🟡 T2. Sync everywhere; multi-tenant `serve` (#5) is a real refactor

**One-sentence:** `call()`, `delegate()`, `helper_call_parallel()` are all sync; `delegate_parallel` and `helper_call_parallel` use `ThreadPoolExecutor`; flipping to async-first when #5 lands is not a small change.

**Why load-bearing:** ROADMAP item #5 ("multi-tenant deployment shape") is explicitly the move that turns the framework into infrastructure. An HTTP service surface either:
1. Wraps every sync call in `run_in_executor` — easy but caps throughput at thread-pool size and forfeits async-stream benefits.
2. Flips the bottom layer to async-first — touches every backend, helper, delegation path. Real refactor.

The MCP integration already documents this tension in spec/19: `asyncio.run()` per MCP tool call (`mcp.py:317+`), with v2 deferred to a "persistent event loop in a background thread."

**Where:**
- `atomic_agents/agent.py` — `call()`, `delegate()`, `helper_call_parallel()` (line 1094), `delegate_parallel()` (line 1385).
- `atomic_agents/mcp.py:317-360` — `_make_tool_handler` uses `asyncio.run()` per call, which spawns a fresh subprocess per invocation.

**When this bites:**
- **Sub-trigger A — MCP perf:** if/when MCP becomes the dominant tool surface (per ROADMAP framing, this is likely by end of 2026). Per-call `asyncio.run()` + subprocess re-spawn is the hotspot.
- **Sub-trigger B — multi-tenant:** the moment ROADMAP #5 (`atomic-agents serve`) starts. Decision point: async-first refactor *before* writing the HTTP layer, or wrap-and-iterate *after*.

**What to watch for:**
- MCP tool calls per session creeping up (today: a handful; an org with rich MCP would see 50-200).
- Anyone proposing async functions in the codebase — that's the leak point. Better to make the call deliberately than absorb it ad-hoc.

**Related:** ROADMAP #5 (multi-tenant), spec/19 §"v1 deferrals", `mcp.py:14-26` deferral comments.

---

### 🟡 T3. MemoryBackend is the only protocol; LockBackend is the actual scaling cliff

**One-sentence:** The protocol pattern is in place (registry + Memory + spec/20), but `AgentLock` is filesystem-flock-only — until LockBackend (#60) lands, nothing runs multi-host.

**Why load-bearing:** `_locks.py` uses `fcntl.flock()` on a `.lock` file. POSIX flock works on local filesystems and *most* network filesystems but is unreliable on NFS variants and not portable to Windows / S3 / Cloud Run / Lambda. "Multi-host fleet running shared agents" requires either:
- Distributed lock service (Redis, etcd, Postgres advisory locks)
- Or a fundamentally different concurrency model (single-writer queue per agent)

Both are LockBackend's job per #60.

**Where:** `atomic_agents/_locks.py` (entire file, 96 lines). Used by:
- `agent.py` `call()` line 582 (per-call serialization)
- `dream.py` `DreamRunner._dream_lock_backend` (separate scope from the agent's main lock so dreams don't block calls — formerly `_DreamLock` class, replaced in #60 PR 2)
- `_cascade.py` queue-claim mechanics use POSIX `Path.rename()` which has the same single-host assumption

**When this bites:** First time someone tries to run two `atomic-agents run` processes against the same vault on different hosts. Issue #60 is flagged "Highest urgency — multi-process cliff."

**What to watch for:**
- Any deployment doc that hand-waves "make sure only one host runs the cron." That's a sign the cliff is being accepted, not solved.
- `dream.py`'s parallel lock (formerly `_DreamLock`, now `_dream_lock_backend` per #60 PR 2) — both routes now go through `FilesystemLockBackend`; the abstraction is no longer leaky between agent and dream. PR 3 of #60 swaps the registry default for operator-pinned backends to address multi-host; PR 4 locks the spec.

**Related:** Issue #60. ROADMAP Tier 2 backend roadmap.

---

### 🟡 T4. Cascade queue is filesystem-only

**One-sentence:** `_cascade.py:claim_next_queued()` uses POSIX atomic `Path.rename()` + sidecar `.lease.json` — beautiful for one host, requires a real queue (Redis / SQS / DB) for multi-host.

**Why load-bearing:** This is the primitive that lets multiple agents (or roles, or hosts eventually) consume a shared work queue without trampling each other. Today it works because rename is atomic on local POSIX filesystems. The lease sidecar is operator-readable, the work file IS the work item, no DB needed. **Aesthetically perfect for a home user.** Architecturally, it's the same single-host assumption as T3.

**Where:** `atomic_agents/_cascade.py:232-310` (`_sidecar_path` at 232, `_write_sidecar` at 237, `claim_next_queued` at 252).

**When this bites:** Same trigger as T3 — first multi-host deployment. But also bites earlier if anyone tries to run the cascade queue on a network filesystem with weak rename guarantees.

**What to watch for:**
- Multi-agent project deployments on shared infrastructure. Symptom: occasional double-claim (two roles see the same work item).
- Roadmap item #5 (multi-tenant serve) implicitly creates this need — a service running multiple operators' agents needs queue-claim to be reliable.

**Related:** spec/06-multi-agent-projects, Issue #60 (LockBackend resolves *part* of this; queue-claim is its own problem).

---

### 🟡 T5. `agent.memory` is hard-coded to FilesystemBackend, despite the registry

**One-sentence:** The protocol + registry pattern (`atomic_agents/memory/__init__.py`) is in place, but `AtomicAgent.__init__` instantiates `FilesystemBackend(...)` directly with no config path to swap.

**Why load-bearing:** As soon as a second backend ships (Postgres, pgvector, SQLite), operators need a way to declare *which* backend to use without subclassing `AtomicAgent`. Today there's no `model.md` field, no `agents.toml`, no constructor parameter. The registry exists; the connection from "operator config" → "registered backend" is missing.

**Where:** `atomic_agents/agent.py:172` (`self.memory: FilesystemBackend = FilesystemBackend(agent_root=..., memory_subdir="memory")`). Type annotation is the concrete class, not the `MemoryBackend` Protocol.

**When this bites:** First non-filesystem backend. Likely paired with a `memory_backend: postgres` field added to `model.md` or a new `backends.md` config file.

**What to watch for:**
- Any test that mocks `agent.memory` by mutating after construction — that's the leak.
- The first PR that needs to load a different backend per agent. The seam currently *doesn't exist*, so it'll show up as a Big Refactor instead of a small config addition.

**Easy fix when needed:** Add `memory_backend: str = "filesystem"` to `AgentConfig`, parse from `model.md`, route through `get_backend(config.memory_backend)(agent_root=...)` at line 172. Type-annotate as `MemoryBackend`. ~10 line change. Worth doing the moment the second backend has any prototype, not at SaaS-shape time.

**Related:** spec/20-memory-backend §"Backend registry", Issues #60–#65.

---

### 🟡 T6. Helper provenance check is heuristic (`_detect_provenance`)

**One-sentence:** `helper_call(sources=...)` sets `provenance_preserved` based on whether the helper output contains citation-like markers — a soft check, not a hard guarantee.

**Why load-bearing:** Spec/13 (research integrity) is a load-bearing claim — "every fact a helper extracted can be traced to a source." Today the check is a regex-ish pattern match in helper output. For a home user it's the right level of effort. For an org auditing helper-mediated facts for compliance (legal review, financial advice, medical research — exactly the verticals that would adopt a "vault-native + audit trail" framework), heuristic isn't a strong-enough guarantee.

**Where:** `atomic_agents/agent.py:1567+` (`_detect_provenance`). Called from `helper_call` at line 1051.

**When this bites:** First operator who treats the framework as a compliance-evidence source and discovers a helper "preserved provenance" by including a fake citation that the regex matched.

**What to watch for:**
- Anyone citing `provenance_preserved=True` as proof of factual accuracy in marketing/positioning. That's the moment to harden it.
- If/when the framework adopts a stricter posture: helpers should emit citations via a *tool schema* (force structured `(claim, source_id, span)` triples) rather than free-text that gets pattern-matched.

**Related:** spec/13 Layer 3 (research log), `_helpers_this_run` rollup in run records.

---

### 🟡 T7. Hardcoded `PRICING` table in `_costs.py`

**One-sentence:** Provider rates are a Python dict in `_costs.py`; rate changes require code patches.

**Why load-bearing:** Cost guardrails (50/80/100% tiered caps + tree-cap delegation) are how this framework refuses to bankrupt anyone. They're load-bearing for "running an army." The `_fallback_pricing()` function (max-known rates when a model is unknown) is conservative-pessimistic and elegant for safety, but it logs a warning and quietly over-counts — operators on the wrong side of a rate update get inflated cost reports.

**Where:** `atomic_agents/_costs.py:14-29` (`PRICING` dict), lines 39-47 (`_fallback_pricing`).

**When this bites:**
- New Anthropic / OpenAI model launch → cost reports show fallback rates until someone PRs the table.
- Anthropic rate cut → operators silently over-estimate spend until the table updates.

**What to watch for:**
- The "fallback used" log line firing in real-world fleet deployments. Indicates the table is stale.
- For org-scale: this should probably ship as a YAML or JSON file the operator can override per-deployment, with the bundled defaults as the fallback. Today, no override path exists.

**Related:** spec/09-cost-observability. Issue #73 is "Cost guardrail sizing guidance" — adjacent but doesn't cover rate-table maintenance.

---

### 🟡 T8. Dream consolidation runs per-agent; doesn't see across agents

**One-sentence:** `DreamRunner` consolidates one agent's memory at a time; cross-agent insights (the Director seeing patterns across Writer + Editor + Researcher) aren't possible today.

**Why load-bearing:** For multi-agent projects (Muse, future cascade-style teams), a lot of value is "what does the *team* see that no individual agent sees?" The current architecture has each agent dream alone; the project canon never gets consolidated, and journal entries from peer agents are invisible to a coordinator's dream.

**Where:** `atomic_agents/dream.py` whole module, especially `_run_pipeline` at line 666. Uses `agent.memory.create_staging()` per spec/20 — staging is per-agent.

**When this bites:** When a multi-agent project's memory grows past the point where one-at-a-time consolidation surfaces useful patterns. Roadmap #15 (Federated dream — "the Director dreams across writer + editor + researcher") explicitly addresses this.

**What to watch for:**
- Muse-The-Unfinished memory growing without the Director surfacing cross-role contradictions. That's the symptom that one-at-a-time isn't enough.
- A `FederatedDream` / `ProjectDream` shape probably wants its own backend protocol slot eventually — same pattern as MemoryBackend, but operating across the project layer.

**Related:** ROADMAP #15 Tier-3, spec/16-dreams.

---

### 🟢 T9. Spec doc surface is growing alongside code surface

**One-sentence:** 21 spec docs today (`01-anatomy.md` through `20-memory-backend.md` plus `27-doctor.md`); every new backend protocol adds another. At #65 (CorpusBackend), expect ~26 spec docs.

**Why load-bearing:** The spec is the thing that makes the framework *teachable* and supports the conformance-suite play (ROADMAP #9). It's also a docs maintenance load that grows with code. Two specific risks:
1. **Drift** — code evolves, spec doesn't. Conformance suite catches this for *runtime* behavior; doesn't catch it for *prose accuracy*.
2. **Discoverability** — at 26 specs, "where do I read about X?" gets harder. Today the README in `docs/spec/` lists all of them.

**Where:** `docs/spec/01-anatomy.md` through `docs/spec/20-memory-backend.md`, plus `docs/spec/27-doctor.md`.

**When this bites:** Around spec doc #25 (~CorpusBackend land). Symptom: someone reads the wrong spec for what they're doing because two specs cover overlapping concerns.

**What to watch for:**
- Whether some specs eventually consolidate. Possible shapes:
  - One "Backend Protocols Overview" doc + per-backend addenda (one section per backend).
  - Spec re-numbering at v1.0 to group related concerns.
- The `docs/architecture.md` already does the right thing as the single mental-model entry point. Worth keeping it canonical.

**Related:** ROADMAP #9 (conformance suite), GOVERNANCE.md.

---

### 🟢 T10. Deprecation wrappers are accumulating without a unified convention

**One-sentence:** `_capture.py` is partly a deprecation shim — it re-exports filesystem internals and the deprecated `write_atomic_note()` while still hosting capture parsing + tool-schema logic. As more backends ship, more `_xxx.py` modules accumulate this hybrid shape.

**Why load-bearing:** Soft tension. Each hybrid does the right thing today (re-export + `DeprecationWarning` planned for v1.0, while keeping live logic in place until callers migrate). But the pattern repeats and there's no module-level `DEPRECATED.md` or convention for *when shims get deleted vs when live logic gets relocated*. At v1.0 lock, this becomes a real cleanup task.

**Where:** `atomic_agents/_capture.py:43-53` (re-exports from `memory/filesystem`), `_capture.py:55-260` (capture parsing + tool schema, still canonical), `_capture.py:263-330` (`write_atomic_note` deprecated). Per spec/20 §"Deprecation wrappers", `_versioning.py` is also slated for the same shape.

**When this bites:** v1.0 release (per ROADMAP). Decision: keep wrappers + emit `DeprecationWarning`, or hard-break and call it a major version's job to sweep.

**What to watch for:**
- New `_xxx.py` shims appearing without a clear sunset plan. Better to record the sunset date in the shim's docstring.
- Tests that import from the shim path rather than the new path. Those tests pin the shim's lifetime.

**Related:** spec/20 §"Deprecation wrappers".

---

### 🟢 T11. CLI is thin; module-level entry points are the real UX

**One-sentence:** `atomic-agents` does `run / info / skills / version / restore / doctor`. Dream, eval, tune, delegate, migrate, goal, outcome run as `python -m atomic_agents.dream` / `.eval` / `.tuning` / `.delegate` / `.migrate` / `.goal` / `.outcome` — a different entry-point shape.

**Why load-bearing:** UX consistency. New users expect `atomic-agents <subcommand>` for everything. Today they have to learn that `atomic-agents run` is one CLI but `python -m atomic_agents.dream` is another.

**Where:** `atomic_agents/cli.py` (293 lines, 6 subcommands). Module entry points: `delegate.py`, `dream.py`, `eval.py`, `goal.py`, `migrate.py`, `outcome.py`, `tuning.py`.

**When this bites:** First user feedback survey or tutorial recording. Symptom: people not finding `dream` because they typed `atomic-agents dream` and got "unknown command."

**What to watch for:**
- Whether tutorials and docs already paper over this. If they don't mention `python -m`, the gap is invisible.
- Consolidation should be a v0.2-v0.5 polish item, not a v1.0 blocker. Easy refactor: `cli.py` becomes a dispatcher; `python -m atomic_agents.dream` keeps working as a back-compat alias.

**Related:** ROADMAP #6 (GitHub template — better first-run UX).

---

### 🟢 T12. `_model.py` and `_tools.py` parse markdown via regex

**One-sentence:** Config files (`model.md`, `tools.md`, `mcp.md`, `roster.md`) are parsed via regex section-matching, not a structured schema parser.

**Why load-bearing:** This is intentional — markdown-as-config keeps the vault aesthetic and makes everything human-editable in any text editor or Obsidian. But it has limits:
- Section name typos silently produce empty sections (parser falls through).
- Optional fields and complex nested structures get awkward.
- No schema validation surface — operators learn errors by running.

**Where:**
- `atomic_agents/_model.py:65-113` — regex `## Default model` etc., plus YAML codeblock for `cost_guardrails`.
- `atomic_agents/_tools.py:44-83` — section pattern matching with fall-through on unknown H2.
- `atomic_agents/mcp.py:427-526` — same shape for `mcp.md`.
- `atomic_agents/_roster.py` — same shape for `roster.md`.

**When this bites:** The moment someone proposes a config field that doesn't fit cleanly in markdown sections (graph relationships, conditional logic, multi-environment configs). Today, all known fields fit fine.

**What to watch for:**
- A creeping urge to support YAML or TOML. The right answer is probably "stay markdown but add a linter/validator" (like ROADMAP #16 spec linter). Don't drop the aesthetic without explicit reason.
- ROADMAP #16 already covers part of this (`atomic-agents lint <agent>`). Worth keeping in mind it pairs with #9 conformance suite.

**Related:** ROADMAP #16 (spec linter).

---

### 🟢 T13. Schema migration runner is filesystem-only

**One-sentence:** `migrate.py` walks `<agents_root>/_migrations/*.py` and applies them to filesystem files; doesn't know how to run a migration over a Postgres or SQLite backend.

**Why load-bearing:** When MemoryBackend has a Postgres impl, schema migrations need to work on both paths. The current shape (each migration script implements `applies_to(path)` + `migrate(path)`) is path-shaped, not backend-shaped.

**Where:** `atomic_agents/migrate.py` whole module (816 lines). Migration script protocol documented at top of file.

**When this bites:** First non-filesystem backend that needs a schema bump. Which is "the second backend" — the moment Postgres ships, this is forced.

**What to watch for:**
- Whether the migration script protocol gets refactored to take a `backend` arg instead of a `path`. That's the right shape and it's a known problem to solve before backend #2.

**Related:** spec/03-file-formats §"Schema migration", spec/20 (memory backend conformance).

---

### 🟢 T14. Markdown system prompt assembly is one giant joined string

**One-sentence:** `assemble_system_prompt()` joins ~14 sections with `═══════════════════════════` separators into one string passed to the LLM.

**Why load-bearing:** Works perfectly today. Cacheable (Anthropic 5-min prompt cache loves stable prefix). Debuggable (operator can see exactly what the LLM saw). But the design implicitly assumes "system prompt is one cohesive block." If a future provider supports first-class structured context (think: separate "memory blocks" passed as distinct API arguments, like Letta's three-tier memory), this assembly becomes a translation layer rather than the source of truth.

**Where:** `atomic_agents/agent.py:478-539` (`assemble_system_prompt`).

**When this bites:** If/when a provider ships first-class structured context (not just text + cache_control). Speculative today; possible by 2027 given how fast the agent SDK landscape is moving.

**What to watch for:**
- Anthropic / OpenAI / Mistral shipping structured context APIs.
- Letta-style memory blocks becoming a de-facto standard. (Letta is the closest competitor per ROADMAP; if their architecture wins, atomic-agents either translates to it or adopts a similar shape.)

**Related:** ROADMAP §"Letta owns memory-first but they're API-bound", ROADMAP #2 (semantic memory retrieval — would extend this assembly with a vector recall step).

---

## Resolved tensions

*(Move items here when resolved, with date + how it resolved.)*

| Date | Tension | How it resolved |
|------|---------|------------------|
| — | — | — |

---

## Decision log

| Date | Tension | Decision / change | Reasoning |
|------|---------|-------------------|-----------|
| 2026-05-09 | All | Captured to TENSIONS.md | Initial review of agent.py + mcp.py + tools.py + cascade + execution layers + observability. |

---

## Cross-links

- [`docs/architecture.md`](architecture.md) — the elegance these tensions are protecting
- [`docs/spec/20-memory-backend.md`](spec/20-memory-backend.md) — the protocol pattern most tensions resolve against
- [`docs/methodology.md`](methodology.md) — twin doc: working methods that produced this codebase
- [`docs/GOVERNANCE.md`](GOVERNANCE.md) — solo / small-team operator guide
- [`CLAUDE.md`](../CLAUDE.md) — design ethos + taste rules
- `~/ObsidianVault/Atomic Agents/ROADMAP.md` — strategic narrative + live backlog table; executable issues at [dep0we/atomic-agents-stack](https://github.com/dep0we/atomic-agents-stack/issues)
- `~/ObsidianVault/Atomic Agents/RESUME-NEXT-SESSION.md` — cross-session state pointer

---

*Companion to `docs/methodology.md`: methodology captures **how we build**, tensions captures **what to protect when changing the code**.*

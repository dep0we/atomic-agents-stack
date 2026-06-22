---
created: 2026-05-09
type: tensions
status: active
last_review: 2026-06-09
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

**When this bites:** As soon as 3 of 6 backend protocols (#60–#65) land. Two have shipped (#57 MemoryBackend, #60 LockBackend); `call()` is "fine, dense — but the public surface widened with `self.lock_backend`." After LogBackend (#61), it's "ache." After PersonaBackend, it wants surgery.

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

**Decision recorded — Hybrid Option C (2026-06-07, issue #342):**

The decision for #342 (`atomic-agents serve`) is **Option C: hybrid adapter now, async-first rebuild explicitly deferred**.

- The Starlette ASGI app dispatches each HTTP request by running `agent.call()` in a shared module-level `ThreadPoolExecutor` (`loop.run_in_executor(_get_executor(), ...)` in `_runner.py`). The `/doctor` route uses the default executor (`run_in_executor(None, ...)`); both satisfy MUST 9. This keeps `asyncio.run()` calls inside the MCP sync bridge legal — they land in a non-async thread where a fresh event loop can start. The Starlette worker loop is not blocked.
- The adapter (`atomic_agents/serve/_runner.py`) is the single central sync→async bridge for `call()`. Every HTTP-triggered agent call goes through it.
- The async-first rebuild (Option B) is explicitly deferred. The written deferral, not the wrapper, is the load-bearing deliverable — future contributors reading this entry know the decision and its triggers.

**Async-first rebuild triggers (named and measurable):**
- **Trigger A:** MCP tool calls per session sustained above 20 per call under concurrent HTTP load (indicates the per-call `asyncio.run()` + subprocess re-spawn overhead is dominating latency).
- **Trigger B:** The serve layer sustains >50 concurrent HTTP requests with measured thread-pool saturation (P95 queue wait > 500ms) — indicates the thread-pool cap is real, not theoretical.
- **Trigger C:** A real operator files a production complaint about MCP tool latency attributable to the sync bridge (not noise).

Meeting any one trigger is sufficient to justify the async-first rebuild. Until then, the hybrid adapter is the correct architecture.

**Sub-trigger B clarification (post-#342):** Sub-trigger B fires only when a multi-concurrent-request scenario requires thread-pool-saturating throughput — NOT when #342 first ships. Issue #342 is a thin wrapper / single-tenant deployment pattern (one wrapper per tenant), so concurrent load at the thread-pool-saturation threshold is not expected at initial shipment. The trigger is written so operators know exactly when to re-open the rebuild question.

**When this bites:**
- **Sub-trigger A — MCP perf:** if/when MCP becomes the dominant tool surface (per ROADMAP framing, this is likely by end of 2026). Per-call `asyncio.run()` + subprocess re-spawn is the hotspot.
- **Sub-trigger B — concurrent HTTP:** when the serve layer hits thread-pool saturation under real concurrent load (see thresholds above).

**What to watch for:**
- MCP tool calls per session creeping up (today: a handful; an org with rich MCP would see 50-200).
- Anyone proposing async functions in the codebase — that's the leak point. Better to make the call deliberately than absorb it ad-hoc.

**Related:** Issue #342 (thin HTTP wrapper — the decision arc), ROADMAP #5 (multi-tenant), spec/19 §"v1 deferrals", `mcp.py:14-26` deferral comments, `docs/spec/37-serve.md`.

---

### ✅ T3. LockBackend ships — multi-host cliff closed (resolved 2026-05-15 via #60)

**One-sentence:** The LockBackend Protocol arc shipped across four PRs ([#180](https://github.com/dep0we/atomic-agents-stack/pull/180) / [#181](https://github.com/dep0we/atomic-agents-stack/pull/181) / [#182](https://github.com/dep0we/atomic-agents-stack/pull/182) / [#183](https://github.com/dep0we/atomic-agents-stack/pull/183)) — `FilesystemLockBackend` (POSIX flock, single-host) + `RedisLockBackend` (single-instance advisory lock, multi-host) reference impls; `scope(sub_path)` Protocol method for namespace isolation; `LockLost` exception + daemon-thread heartbeat for lease-backed backends; operator override via `ATOMIC_AGENTS_LOCK_BACKEND` env vars OR `AtomicAgent(..., lock_backend=...)` kwarg; `doctor.check_lock_backend` coherence check with PASS/WARN/FAIL ladder. Spec locked at `docs/spec/21-lock-backend.md`.

**What was the cliff:** `_locks.AgentLock` used `fcntl.flock()` on a `.lock` file. POSIX flock works on local filesystems but is unreliable on NFS variants and not portable to Windows / S3 / Cloud Run / Lambda. "Multi-host fleet running shared agents" required either a distributed lock service or a fundamentally different concurrency model.

**How #60 resolved it:** Protocol + filesystem default (zero behavior change for existing deployments) + Redis reference impl (multi-host). Operators on Cloud Run / Kubernetes / gizmo flip `ATOMIC_AGENTS_LOCK_BACKEND=redis` + `ATOMIC_AGENTS_LOCK_BACKEND_URL=redis://...`; no code changes required. `_locks.AgentLock` preserved as a deprecation shim through v1.0 (CLAUDE.md rule #14).

**Residual single-host assumptions** (NOT in scope for #60, tracked elsewhere):
- `_cascade.py` queue-claim mechanics use POSIX `Path.rename()` — see T4 below.
- `memory/filesystem.py:_per_file_lock` (per-note flock) is deliberately a filesystem-implementation invariant, NOT part of the LockBackend Protocol per spec/21. Future Redis-backed memory backends would use Redis transactions, not `<note>.lock` files.

**Related:** Issue #60 (closed by #183), `docs/spec/21-lock-backend.md`, T4 below for the parallel cascade-claim story.

---

### ✅ T4. Cascade queue is filesystem-only — resolved 2026-06-12 via #428

**One-sentence:** `_cascade.py:claim_next_queued()` uses POSIX atomic `Path.rename()` + sidecar `.lease.json` — beautiful for one host, requires a real queue (Redis / SQS / DB) for multi-host.

**Why load-bearing:** This is the primitive that lets multiple agents (or roles, or hosts eventually) consume a shared work queue without trampling each other. Today it works because rename is atomic on local POSIX filesystems. The lease sidecar is operator-readable, the work file IS the work item, no DB needed. **Aesthetically perfect for a home user.** Architecturally, it's the same single-host assumption as T3.

**Where:** `atomic_agents/queue/` (`FilesystemQueueBackend` + `QueueBackend` Protocol + `_sidecar_path`/`_write_sidecar` in `filesystem.py`). The `_cascade.py` free functions are now a thin non-deprecated shim over the Protocol.

**When this bites:** Same trigger as T3 — first multi-host deployment. But also bites earlier if anyone tries to run the cascade queue on a network filesystem with weak rename guarantees.

**What to watch for:**
- Multi-agent project deployments on shared infrastructure. Symptom: occasional double-claim (two roles see the same work item).
- Roadmap item #5 (multi-tenant serve) implicitly creates this need — a service running multiple operators' agents needs queue-claim to be reliable.

**Closed by:** #428 — QueueBackend Protocol + FilesystemQueueBackend + LOCKED spec/44. The queue cluster is now a swappable Protocol (QueueBackend in `atomic_agents/queue/`). A Redis/SQS/DB backend can register at import time and plug in via `ATOMIC_AGENTS_QUEUE_BACKEND`. The `single_host_only=True` capability flag + `doctor.check_queue_backend` WARN path give operators visibility when the filesystem backend is in use on a multi-host deployment. spec/44 LOCKED at 136 tests + 12 MUSTs; runtime adoption (wire into `_cascade.py`) deferred to #469. Security hardening: iterdir()-walk in export() (#477), no-replace mkdir + O_EXCL claim probe (#478), O_NOFOLLOW sidecar writes (#479).

**Related:** spec/06-multi-agent-projects, spec/44-queue-backend (LOCKED), Issue #60 (LockBackend resolves *part* of this; queue-claim is its own problem).

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

**Related:** spec/20-memory-backend §"Backend registry", Issues #60–#65. **Now an executable issue: #382** (promoted because GCP elastic scale-out Phase 2 depends on it — see #339, T15).

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

### 🟢 T14. Markdown system prompt assembly is one giant joined string

**One-sentence:** `assemble_system_prompt()` joins ~14 sections with `═══════════════════════════` separators into one string passed to the LLM.

**Why load-bearing:** Works perfectly today. Cacheable (Anthropic 5-min prompt cache loves stable prefix). Debuggable (operator can see exactly what the LLM saw). But the design implicitly assumes "system prompt is one cohesive block." If a future provider supports first-class structured context (think: separate "memory blocks" passed as distinct API arguments, like Letta's three-tier memory), this assembly becomes a translation layer rather than the source of truth.

**Where:** `atomic_agents/agent.py:478-539` (`assemble_system_prompt`).

**When this bites:** If/when a provider ships first-class structured context (not just text + cache_control). Speculative today; possible by 2027 given how fast the agent SDK landscape is moving.

**What to watch for:**
- Anthropic / OpenAI / Mistral shipping structured context APIs.
- Letta-style memory blocks becoming a de-facto standard. (Letta is the closest competitor per ROADMAP; if their architecture wins, atomic-agents either translates to it or adopts a similar shape.)

**Related:** ROADMAP §"Letta owns memory-first but they're API-bound", ROADMAP #2 (semantic memory retrieval — would extend this assembly with a vector recall step).

### 🟡 T15. Vault-as-source-of-truth vs. enterprise store-of-record (the authority model)

**One-sentence:** Principle #1 says "the vault files are the source of truth; backends are never authoritative" — but an enterprise GCP/AWS/Azure deployment needs mutable state (logs, memory, goals) to live authoritatively in a real store (Postgres / native cloud DB), because Cloud Run is ephemeral and scale-to-zero, and many concurrent runs can't safely share files.

**Why load-bearing:** This is the decision the entire cloud-delivery story rests on (#339 GCP blueprint, #344 delivery push, and every future AWS/Azure adapter). Get it wrong and one of the two first-class shapes — home-user-on-files or org-fleet-on-cloud — silently becomes a degraded mode, which the throughline explicitly refuses. The naive framings both fail:
- *"All vault files move into Postgres on deploy"* — collapses the config/state distinction and breaks the markdown-as-config aesthetic (rule #7) and the home==enterprise throughline.
- *"Files always win; the DB is just an index rebuilt from files"* (call it **Position A**) — keeps principle #1 literally true but forces permanent file↔DB sync machinery, keeps files in the write path (caps enterprise throughput), and multiplies that sync tax once per cloud as AWS/Azure adapters land. It fights the very protocol design.

**The distinction that resolves it:** vault content is two kinds with opposite needs.
- **Config** (who the agent *is*): `persona/`, `model.md`, `tools.md`, `mcp.md`, `goal.md`, skills. Human-authored, read-mostly, small, version-controlled. **Stays as files everywhere** — on home disk for the home user, baked into the container image (immutable, fast) for cloud. No non-filesystem backend needed; it just has to be present on the runtime's filesystem.
- **State** (what the agent has *done/learned*): memory notes, JSONL run logs, goal history, outcomes, cost data, locks. Runtime-written, concurrent, unbounded. **Runs through a swappable backend** — filesystem default (home), Postgres + Secret Manager + Cloud Logging (GCP), native stores (AWS/Azure). This is the only part that ever leaves the filesystem, and only at scale.

**Decision recorded — Position B (2026-06-09):**

The registered backend is the **store of record for its own data in that deployment**, and the canonical *vault file shape* is a guaranteed **export/render contract** — not a guaranteed *location*.

- Portability is preserved not by *"files are always the truth"* but by *"the file shape is always reconstructable."* Any backend can render its data back to the canonical vault shape (a memory note, a JSONL line) on demand. Kill a GCP deployment, export, and you have a home-shaped agent again.
- This is **stronger** portability than Position A, not weaker: it's portability across *stores*, not just across *processes* — exactly what's needed once Atomic Agents has its own home-user runtime AND adapters for ≥2 clouds. The export contract becomes the universal portability primitive **across runtimes and across clouds**; Position A would pay an N-clouds sync tax to achieve less.
- **This explicitly amends Principle #1.** "Backends are never authoritative" holds for the *default (filesystem) deployment* and for *config in every deployment*. In a backend-swapped deployment, the registered backend IS authoritative for its state, and the file shape is a tested export guarantee. Per the rule itself — when a principle needs to flex, it gets a TENSIONS entry rather than a silent break. This is that entry. (Follow-up: cross-reference this T15 from CLAUDE.md Principle #1.)

**The engineering spine this decision implies (the deliverable that makes B real):** every backend Protocol gains a **canonical-shape export method**, verified by **round-trip conformance** (write to backend → export → byte-equivalent JSONL/markdown). Without that method, B is just "we gave up on portability." With it, B is the portability guarantee. This is a Protocol-surface addition across the existing 13 backends — a tracked, multi-PR body of work, not a one-liner. **Tracked in #379.**

**Where:**
- Principle #1 in `CLAUDE.md` (the rule being flexed).
- `agent.py` backend instantiation seams (T5 — the missing config→backend wiring is the same seam this rides on).
- Future `extras/gcp/` (#339), AWS/Azure blueprints, and the first-party home runtime all consume this contract.

**When this bites:** Now, conceptually — it gates the #339 GCP blueprint design. Concretely, it bites the first time an operator deploys to GCP with Postgres-backed state and then wants their agent back on a laptop (or moved to another cloud). Without the export contract, that round-trip is bespoke per backend.

**What to watch for:**
- Any cloud adapter that stores state with **no export-to-canonical-shape path** — that's a portability hole, and the moment Position B quietly degrades into "the cloud owns your agent."
- Marketing/positioning that claims "your agent is portable" before round-trip conformance exists to back it. Same discipline as T6 — don't claim the guarantee before the test enforces it.
- A config field or state field landing on the wrong side of the config/state line (e.g., mutable runtime state baked into the image, or human-authored config shoved into Postgres). The line is: *who writes it, how often, and does concurrency matter.*

**Related:** Principle #1 (CLAUDE.md), #379 (the export-contract deliverable), #382 (memory config→backend wiring seam — T5 promoted), #383 (protocol-coverage gap: goals/outcomes/journal/cascade), #339 (GCP blueprint), #344 (GCP delivery push), #340 (SecretBackend — first state-leaves-the-filesystem case), #258 (Postgres adapters), T5 (config→backend wiring seam), T13 (migration runner — same root cause, **resolved** backend-shaped via #429/#446), the throughline.

---

### 🟡 T16. Raw immediate-continuity transcript as a bounded Rule #6 flex

**One-sentence:** ConversationBackend injects prior turn messages verbatim into the `messages[]` array, a deliberate and bounded flex of Rule #6 (load capability *awareness*, not capability *content*); this entry records why the flex is legitimate and where its edges are, so a future contributor reading Rule #6 as absolute does not "fix" conversation continuity away.

**Rule #6 restated:** load capability metadata, not capability content; the LLM pays context tokens for *awareness*, not for the material itself. The progressive-disclosure pattern (INDEX-guided selective recall of 1-3 notes) is the canonical application. ConversationBackend injects prior turn messages directly into the `messages[]` array (verbatim role-tagged content) before the current work_item. That is the flex.

**Why it does not violate the rule's intent:**
- Scope: current-run only, bounded to the active `agent.call()` invocation. Turns are not cached beyond the call, not promoted to memory, and not part of the system prompt.
- Token cap: a token-budget window is enforced. Only turns that fit within `model_context_limit - system_prompt_tokens - max_output_tokens` are loaded. Oldest-first eviction on overflow. Never unbounded.
- Ephemeral: when the call ends, the injected turns are gone from context. The conversation log on disk is the durable artifact; the in-context array is the working copy.
- Hard turn/token limit: the budget window is the hard cap. The system never auto-loads the full conversation log.
- Summarization gate (PR3): the bounded-window invariant holds after summarization ships. PR3 replaces old turns with a summary token that fits the same window, not a lifted cap.

**Why Rule #6 must flex here:**
- Conversation continuity requires immediate context. The INDEX-guided selective recall pattern (designed for potentially 30K+ tokens across many past calls) does not apply to the prior 3-5 turns of the current exchange. An INDEX of "I said X, you replied Y" provides no compression benefit and breaks the conversational coherence callers expect.
- The alternative, flattening turns into the system prompt, would break T14's cacheable prefix: a billing regression on every turn.

**What this flex does NOT authorize:**
- Auto-loading the full conversation log without a token-budget window.
- Injecting turn content into `assemble_system_prompt()` (the cacheable prefix must remain stable).
- Bypassing the window to fit "important" turns (no turn-priority mechanism in this spec).
- Using the conversation transcript as a substitute for proper Notes recall for durable facts.

**What to watch for (named triggers that distinguish this from a Rule #6 violation):**
1. `conversation_backend` is not None AND `conversation_id` is not None: both must be set for any transcript to load.
2. Token-budget window in force: load stops when `remaining_context_tokens <= 0`; no override path bypasses this.
3. Current-run bounded: turns appear in `messages[]` only; they do not persist into `assemble_system_prompt()` output.
4. No auto-promote: turns are NOT automatically promoted to Notes/Wiki/INDEX. Promotion happens only through the explicit `atomic_capture` tool.
5. Oldest-first eviction on overflow: when the window is tight, oldest turns are dropped, not the system prompt.

When transcript tokens regularly dwarf INDEX + selected notes, or multi-session transcript replay appears, or raw logs become auto-loaded, the flex has outgrown its bounds and this entry needs revisiting.

**Related:** Rule #6 (CLAUDE.md, progressive disclosure), T14 (cacheable system-prompt prefix), spec/47 (ConversationBackend), spec/02 + spec/04 (atomic memory + runtime assembly), #535 (ConversationBackend protocol), the throughline.

---

## Resolved tensions

*(Move items here when resolved, with date + how it resolved.)*

| Date | Tension | How it resolved |
|------|---------|------------------|
| 2026-06-11 | T13 | Migration runner refactored from path-shaped to **backend-shaped** (#429, PR #446). The old `applies_to(path)` / `migrate(path)` script protocol is removed; migration now runs through a dedicated `MigrationBackend` Protocol + per-unit `MigratableUnit` handle, with `FilesystemMigrationBackend` as the reference impl. A future Postgres/SQLite backend satisfies the same Protocol without forking the runner — the root cause T13 named is closed. spec/03 §Schema-migration re-LOCKED (8 MUSTs). See decision log below. |
| 2026-06-12 | T4 | Cascade work queue carved from `_cascade.py` into a swappable **`QueueBackend` Protocol** + `FilesystemQueueBackend` reference impl (#428, PR #481). A Redis/SQS/DB backend providing cross-host claim atomicity can now register via `ATOMIC_AGENTS_QUEUE_BACKEND` without forking the claim logic — the single-host filesystem-rename constraint T4 named is liftable per deployment. The `single_host_only=True` capability flag + `doctor.check_queue_backend` WARN make the cliff visible. SCAFFOLDING-ONLY (no internal runtime caller wired yet); DRAFT spec/44. See decision log below. The filesystem backend's perimeter containment was hardened to a single canonical-source invariant during `/ship`, with the trust model (boundary = `project_root`; adversarial/multi-host → a real-authz backend) documented in spec/44. |

---

## Decision log

| Date | Tension | Decision / change | Reasoning |
|------|---------|-------------------|-----------|
| 2026-05-09 | All | Captured to TENSIONS.md | Initial review of agent.py + mcp.py + tools.py + cascade + execution layers + observability. |
| 2026-06-07 | T2 | Hybrid Option C — thread-pool adapter now, async-first rebuild deferred with named triggers | Issue #342 (thin HTTP wrapper). Decision rationale: ships org shape today without async refactor; keeps both home and org shapes first-class; written deferral with measurable triggers is the load-bearing deliverable. See T2 "Decision recorded" block above. |
| 2026-06-09 | T15 | Position B — registered backend is store-of-record per deployment; canonical vault *shape* is a guaranteed export contract, not a guaranteed location | Authority model for cloud delivery (#339/#344). Explicitly amends Principle #1: "backends never authoritative" holds for the default/filesystem deployment and for config everywhere; backend-swapped state is authoritative-in-that-deployment with a tested round-trip export. Strengthened by the coming first-party home runtime + AWS/Azure adapters — the export contract is the cross-runtime, cross-cloud portability primitive; Position A would multiply file-sync cost per cloud. Implies a new export method + round-trip conformance across all 13 backends (#379). |
| 2026-06-10 | T15 | spec/40 (`docs/spec/40-canonical-export.md`) delivered + **LOCKED** — the export-contract commitment in the Position B ruling is now a tested contract, not a promise | #379 PR 1 shipped the `Exportable` companion Protocol, `supports_canonical_export` capability field on all six PR1 state backends, filesystem identity export impls for Memory/Log/Mandate/Corpus/Lock/Secret, a 91-test round-trip conformance suite, and spec/40 (LOCKED on filesystem proof per the contract-first ruling). CLAUDE.md Principle #1 now carries the spec/40 pointer. Per-backend SQLite/Postgres/Redis/GCP/HTTP export impls are later PRs that conform to the locked contract. |
| 2026-06-11 | T13 | **Resolved** — migration runner refactored path-shaped → backend-shaped: dedicated `MigrationBackend` Protocol + `MigratableUnit` handle + `FilesystemMigrationBackend` reference impl; clean break (BREAKING — old `applies_to(path)`/`migrate(path)` removed); read-only `read_schema_version()`; full `snapshot()`/`restore()` protocol methods + fail-close on no-rollback. | #429 (PR #446, merged). Same root cause as T15 — the last path-shaped storage primitive becomes backend-shaped so backend #2 (Postgres/SQLite) satisfies the migration contract without forking the runner. spec/03 §Schema-migration DRAFT→re-LOCK (full drift-gate, 8 MUSTs); `python -m atomic_agents.migrate` CLI entrypoint unchanged (subcommand promotion → #438; legacy v0→v1 → #439). Closes the T13 "must also become backend-shaped" cross-reference flagged in T15's Related list. |
| 2026-06-12 | T4 | **Resolved** — queue cluster carved from `_cascade.py` into `atomic_agents/queue/` as `QueueBackend` Protocol + `FilesystemQueueBackend` reference impl (SCAFFOLDING-ONLY; zero internal runtime callers wired). | #428 PR 1. Thin non-deprecated re-export shim in `_cascade.py` preserves verbatim free-function signatures for existing callers. DRAFT spec/44 ships; 12-MUST Implementer Contract. `single_host_only` capability flag mirrors `LockCapabilities` pattern. `recover_stale_claims()` is a free function above the Protocol, calling only Protocol methods. spec/40 export whitelist (queued/ + done/ + dead-letter/ only; claimed/ excluded). Doctor check SKIP for single-agent layouts (detect_cascade → None); WARN on ATOMIC_AGENTS_MULTI_HOST. Runtime adoption (cascade runner wiring) deferred to follow-up issue. |
| 2026-06-22 | T4 | **LOCK** — spec/44 DRAFT→LOCKED; 136 tests (62 conformance + 74 filesystem-specific); all 12 MUSTs individually test-covered. Security hardening: iterdir()-walk in export() adds version-independent no-follow + a per-subdir containment re-assertion `rglob` performs nowhere (#477 — defense-in-depth; default `rglob('*')` does NOT follow directory symlinks on 3.11–3.13, so this hardens against a future `recurse_symlinks` default rather than a live DoS, live 3.13+ vector tracked in #595); no-replace mkdir + O_EXCL claim probe closes lease_token-collision clobber (#478); O_NOFOLLOW sidecar writes close sidecar-leaf perimeter escape (#479); MUST 10 fail-soft de-vacuoused + strip-RED negative control (#476). | #428 LOCK PR. Runtime adoption deferred to #469. |
| 2026-06-19 | T16 | **Added** — raw immediate-continuity transcript injection recorded as a bounded, deliberate flex of Rule #6, with named triggers + a "does NOT authorize" boundary. | #535 ConversationBackend PR1. Conversation turns load into `messages[]` (not the system prompt, preserving T14's cacheable prefix), bounded by a token-budget window with oldest-first eviction, current-run-only, never auto-promoted to memory. Distinct from distilled INDEX-driven recall: immediate continuity for the prior few turns, ephemeral, not accumulating cross-session. Maintainer-approved wording (Dan, 2026-06-19). |

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

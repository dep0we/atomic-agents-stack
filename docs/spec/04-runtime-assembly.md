# 04 — Runtime Assembly

How the system prompt gets built at every invocation. The exact load order, with cache breakpoints.

---

## The canonical load order

Every Atomic Agent runtime — cron job, Claude skill, openclaw gateway, anything else — assembles the system prompt in this order:

```
[1] IDENTITY.md
[2] SOUL.md
[3] USER.md
[3.5] goal.md  (if agent is goal-driven or hybrid AND active goal exists; per spec/12)
[4] tools.md
[5] (model.md is informational; runtime already picked the model)
[6] memory/INDEX.md
[7] wiki/INDEX.md (if present)
[8] PINNED atomic units (any file with pinned: true)
[9] RECENT atomic units (last N captured, default N=5)
[10] RECENT journal entries (last 1-3 dated entries)
[11] The work item (user message, queue item, or for goal-driven: next sub-goal)
```

This order is **mandatory**. Every runtime must follow it. The order is what gives Atomic Agents their cross-runtime equivalence — the same agent behaves the same way whether driven by cron or skill or openclaw because the system prompt is built identically.

**Step [3.5] — goal context**: For reactive agents (most agents), this step is skipped. For goal-driven agents, the active `goal.md` is loaded between persona and tools/memory — placed there so the goal becomes part of the agent's "anchored context" that shapes everything below it. For hybrid agents, the runtime decides per invocation: skill triggers skip step [3.5] (reactive mode); cron triggers load it (goal-driven mode). See [12-goals-and-intent](12-goals-and-intent.md) for the full goal-driven mechanics.

---

## What each section provides

| # | File(s) | Provides | Roughly cost |
|---|---|---|---|
| 1 | IDENTITY.md | Who, mission, scope, doctrine | ~500-1500 tokens |
| 2 | SOUL.md | Personality, voice, evolution discipline | ~500-1000 tokens |
| 3 | USER.md | About Dan (relevant slice) | ~500-1000 tokens |
| 4 | tools.md | Capability boundaries | ~200-500 tokens |
| 5 | (model.md) | (informational, not always loaded) | 0-200 tokens |
| 6 | memory/INDEX.md | Atomic Notes routing layer | ~500-2000 tokens |
| 7 | wiki/INDEX.md | Atomic Wiki routing layer | ~500-2000 tokens |
| 8 | Pinned atomic units | Must-never-forget context | varies |
| 9 | Recent atomic units | Recency window for the agent | ~1000-3000 tokens |
| 10 | Recent journal entries | Episodic recency context | ~500-2000 tokens |
| 11 | Work item | The actual ask | varies |

**Total system prompt size**: typically 6-12K tokens for a mature agent. Stays approximately constant as memory grows because INDEXes route, not concatenate.

---

## Prompt cache breakpoints

For Anthropic API calls, place cache breakpoints to maximize hit rate:

```
[1] IDENTITY.md         ┐
[2] SOUL.md             │
[3] USER.md             ├── BREAKPOINT 1 (highly cached, changes rarely)
[4] tools.md            │
[6] memory/INDEX.md     │
[7] wiki/INDEX.md       ┘
[8] Pinned atomic units  ── BREAKPOINT 2 (changes weekly)
[9] Recent atomic units  ── BREAKPOINT 3 (changes per session)
[10] Recent journal      ── BREAKPOINT 4 (changes daily)
[11] Work item           ── (never cached — the variable input)
```

**Goal**: 80%+ cache hit rate within Anthropic's 5-min cache TTL when an agent is called repeatedly in a short window.

For openclaw, cache strategy is plugin-managed — handled automatically.

For cron jobs that run once daily, cache hits are unlikely (24h between calls). Don't optimize for cache; optimize for token count instead.

For Claude skills (interactive sessions), cache hits matter a lot — a 10-message session can hit cache on every message after the first.

---

## On-demand loading during the conversation

The system prompt only loads INDEXes + recent + pinned. Everything else is selectively loaded as the agent reasons.

**Pattern** (for Claude skill / interactive runtime):

The agent is instructed to use a tool / convention to load specific atomic units when reasoning needs them:

```
The agent thinks: "Dan asked about debt strategy. The INDEX shows
`Debt priority order` is relevant.
I'll read that file before responding."
```

Implementation:

- For **Claude skills**: the skill includes Read tool access scoped to the agent's vault folder. The agent reads `memory/feedback_debt_priority_order.md` directly when needed.
- For **cron jobs**: the Python runner can pre-load specific files based on simple keyword matching against the work item, OR it can do a two-pass call (first pass: agent says what it needs; second pass: agent gets it).
- For **openclaw**: memory-core's `memory_search` and memory-wiki's `wiki_get` tools handle this automatically.

---

## Pinned atomic units

`pinned: true` in frontmatter means the file is loaded at step [8] regardless of relevance. Use sparingly.

**Good candidates for pinning**:
- Hard scope boundaries that the agent must never forget ("Caldwell never moves money")
- A handful of foundational decisions that color every interaction
- A current critical project state that's relevant every session

**Bad candidates for pinning**:
- Nice-to-knows
- Historical context that's already settled
- Anything the INDEX entry will reliably surface

Rule of thumb: keep pinned content under ~1000 tokens total per agent. Each pinned token is a tax on every call.

---

## Recent atomic units

At step [9], load the last N atomic units by `captured` date. Default N=5.

**Why recency matters**: the agent's most recent learnings are most likely to be relevant to the next interaction. This gives the agent natural session-to-session continuity without requiring it to dig through INDEX every time.

**Tunable**: increase N for high-information-density agents (Bishop, Caldwell), decrease for narrow-task agents.

---

## Recent journal entries

At step [10], load the last 1-3 dated journal entries.

**Why**: episodic recency context. "Yesterday I was thinking about X" — the agent picks up the thread without you having to remind it.

**Format**: each journal entry is loaded whole. Cap the count to keep the budget bounded; 1-3 is typical.

---

## The work item

At step [11], the actual ask:
- For **cron**: a manifest from the queue, or a scheduled trigger description
- For **Claude skill**: the user's message
- For **openclaw**: the user's message (interactive) or the playbook step (autonomous)

This is the only piece that's truly variable per call. Everything before it should be highly cached.

---

## What's NOT in the system prompt at runtime

These are intentionally excluded from automatic loading:

- ❌ Old journal entries (only the recent 1-3)
- ❌ Most atomic notes (only INDEX + recent + pinned)
- ❌ Wiki pages (only INDEX; pages loaded on demand)
- ❌ `raw/` source documents (only the wiki pages distilled from them)
- ❌ Other agents' folders (read access optional, not loaded by default)
- ❌ The agent's own log/ history (queryable, not auto-loaded)

The agent can call back to load any of these on demand. Default-not-loaded keeps the per-call token cost predictable.

---

## Worked example: a typical Caldwell call

User asks: "Should I prepay the mortgage with the bonus check from Q1?"

System prompt assembled (~8K tokens):

```
[1-3]  IDENTITY+SOUL+USER  — 2.5K tokens, cached
[4]    tools.md            — 0.4K tokens, cached
[6-7]  INDEXes              — 1.8K tokens, cached
[8]    Pinned (1 file)      — 0.3K tokens, cached
[9]    Recent 5 notes       — 2.0K tokens, partially cached
[10]   Yesterday's journal  — 0.5K tokens, fresh
[11]   The work item        — 0.05K tokens
```

Caldwell thinks: "INDEX shows `feedback_debt_priority_order.md` and `decision_q3_income_target.md` are directly relevant. I'll read those."

Caldwell reads both files (each ~500 tokens). Total turn input: ~9K tokens.

Caldwell responds with: "No — credit card balances first per locked priority order. Send the bonus to the highest-rate card. The math is X."

Output: ~1.5K tokens.

**Total cost**: ~10.5K input + 1.5K output. With 80% cache hit on the first 5K of system prompt, effective cost is closer to ~5K input + 1.5K output. At Opus pricing, that's a few cents per turn. At scale: hundreds of turns/day × $0.05 = $5-15/day for an active agent.

---

## When the canonical order doesn't apply

**1. Agent has no `wiki/`**
Skip step [7]. Otherwise unchanged.

**2. Agent has no `USER.md`**
Skip step [3]. Useful for agents that don't have a single primary user (e.g., a customer-facing agent).

**3. Agent has both runtimes (cron + skill)**
Same load order. The runtime-specific instructions live in IDENTITY's "Operating doctrine" section if they differ.

**4. Agent is a role × project instance under a multi-agent system with cascade (Muse-style)**

The canonical order extends with three layers — role-shared, project-shared, and instance — instead of all-from-one-agent-folder:

```
[1]  ROLE      roles/<role>/PROMPT.md           ← who I am as a Writer in general
[2]  INSTANCE  agents/<role>/persona/IDENTITY   ← who I am as Writer ON THIS PROJECT
[3]  INSTANCE  agents/<role>/persona/SOUL       ← my voice on this project
[4]  INSTANCE  agents/<role>/persona/USER       ← about Dan
[5]  ROLE      roles/<role>/tools.md            ← role-level tools (override at instance if needed)
[5b] INSTANCE  agents/<role>/tools.md           ← OPTIONAL — overrides 5 if file exists
[6]  ROLE      roles/<role>/model.md
[7]  PROJECT   <project>/canon.md               ← shared world
[8]  PROJECT   <project>/style_guide.md
[9]  PROJECT   <project>/policy/*               ← all locked decisions
[10] INSTANCE  agents/<role>/memory/INDEX.md
[11] INSTANCE  agents/<role>/wiki/INDEX.md
[12]           pinned + recent atomic notes (instance)
[13]           recent journal (instance)
[14]           work item from <project>/queue/
```

The cascade rule: **specific wins over general**. Instance files override role-level if both exist (for tools.md and model.md primarily). See [06-multi-agent-projects](06-multi-agent-projects.md) for the full cascade specification.

Cache breakpoints place naturally at layer boundaries — role-layer (rarely changes), project-layer (changes more), instance-layer (changes most). Three breakpoints get high cache hit rates without hand-tuning.

---

## Anti-patterns to avoid

❌ **Loading the whole `memory/` folder at startup.** Defeats the INDEX trick. The whole point is selective load.

❌ **Putting everything in IDENTITY.md.** IDENTITY is supposed to be ~1-2K tokens. If yours is 8K, you're abusing it. Move evolving content to memory.

❌ **Skipping cache breakpoints.** They're free performance.

❌ **Auto-loading old journal entries.** Today + yesterday is enough. Older days are searchable but not loaded.

❌ **Loading other agents' content by default.** Even with read access, default-not-loaded. Loading on demand respects scope and saves tokens.

---

## Runtime conformance checklist

The promise of Atomic Agents is **shared source files, runtime-specific adapters** — not identical execution. A "conforming" runtime adapter must implement the following. Use this as a checklist when evaluating whether a new runtime (or your own implementation of an existing one) actually behaves the way the spec expects.

### Required (a runtime is non-conforming if it skips any of these)

- [ ] **Reads agent files in canonical order** — IDENTITY → SOUL → USER → tools.md → INDEXes → pinned → recent → journal → work item. The exact mechanism varies (cron Python concatenates strings; Claude Code skill uses a SKILL.md preamble that instructs Read tool calls; openclaw uses memory-core/wiki plugins) but the resulting prompt structure is the same.
- [ ] **Respects pinned vs. selective recall** — pinned atomic units always loaded; non-pinned loaded only by name on demand. Loading the whole `memory/` folder at startup is a violation.
- [ ] **Honors `tools.md` write paths** — at minimum advisory; ideally enforced by the runtime's sandbox or the helper. See [01-anatomy#tools-md](01-anatomy.md#tools-md) for the policy-vs-enforcement split per runtime.
- [ ] **Writes captures via the helper, not directly** — captures emit JSON markers; runtime extracts and routes to `atomic_agents` (or equivalent). No direct Write-tool writes to memory or INDEX in v1.
- [ ] **Writes log records** — every invocation produces one JSONL line in `log/YYYY-MM/YYYY-MM-DD.jsonl` with at minimum: `ts`, `trigger`, `model`, `input_tokens`, `output_tokens`, `status`. Cost dashboard depends on this.
- [ ] **Validates frontmatter on read and write** — bad frontmatter is surfaced, not silently corrupted.

### Strongly recommended (a runtime should implement these for production use)

- [ ] **File locking** — per-agent `flock` lock file with stale-lock recovery on process death. See [../implementation/shared-helper#file-locking](../implementation/shared-helper.md#file-locking).
- [ ] **Cache breakpoints at layer boundaries** — persona / tools / INDEX / pinned / recent / work-item. Real cache hit rate logged, not just claimed.
- [ ] **Cost guardrails read from `model.md`** — daily/monthly cap enforcement (skip / fallback / alert).
- [ ] **Capture validation against `schema_version`** — schema mismatches surface as errors, not silent drift.

### Nice-to-have

- [ ] Lint pass detection on startup (orphans, expired, schema drift)
- [ ] Streaming output for interactive runtimes; non-streaming for batch
- [ ] Per-runtime conformance test against the spec's golden suite (see [08-evaluation](08-evaluation.md) when written)

### Per-runtime conformance status

| Runtime | Required | Recommended | Notes |
|---|---|---|---|
| Cron Python (atomic_agents) | ✅ all | ✅ all | Reference implementation for Wave 3 |
| Claude Code skill | ⚠️ partial | ⚠️ partial | Capture-via-marker pattern works; helper-side write enforcement requires the helper to be installed |
| Codex CLI skill | ⚠️ partial | ⚠️ partial | Same as Claude Code; tool names differ |
| ChatGPT web skill | ❌ limited | ❌ no | Bundle-snapshot mode only; no live vault read/write without MCP bridge |
| OpenAI API skill | ✅ if helper installed | ✅ if helper installed | Same as cron pattern with different SDK |
| OpenClaw | ✅ via memory-core/wiki plugins | ✅ via memory-core/wiki plugins | Bishop's case; native compliance |

Where a runtime is `❌`, it means deploying an Atomic Agent there gives you a *degraded* experience — not a broken one. The persona still loads, the agent still responds; what's missing is the capture/promotion/lint cycle that makes the agent self-improving over time.

---

*Next: [05-capture-rules](05-capture-rules.md) — when to write a memory, when to promote, when to lint.*

# Architecture

The big picture, in diagrams.

---

## Two scopes: Agent and Project

Most Atomic Agents are single-agent, single-domain — Caldwell handles money, agent-a handles one domain, agent-b handles another. Each is one self-contained Atomic Agent.

A few systems are **multi-agent across one or more projects** — Muse has Director, Writer, Editor, Outliner, Visual, Artist, Developer roles, currently working on The Unfinished project (with future creative projects expected). Each role × project combination is an Atomic Agent; Muse adds two layers above them: shared role templates and per-project shared world (canon, style, policy).

```
SCOPE                       WHAT LIVES HERE
─────                       ───────────────
Agent scope                 persona, atomic memory, journal, tools.md, model.md
Project scope (optional)    shared canon, style guide, project policy, work queue,
                             "the world" all roles agree on
```

Cases:

| | Agent scope | Project scope |
|---|---|---|
| **another agent / Caldwell / agent-a / agent-b** (single-agent, single-domain) | ✅ | — (collapsed) |
| **Muse on The Unfinished** (multi-agent, single-project today, more expected) | ✅ (one per role × project) | ✅ + role templates |
| **Multi-agent across multiple projects** (e.g., Muse with N fiction projects) | ✅ × N projects × M roles | ✅ × N + role templates shared |

The per-agent spec (this folder) defines agent scope. Project scope is a thin extension on top — see [spec/06-multi-agent-projects](spec/06-multi-agent-projects.md).

---

## File layout per agent

> The example paths use `~/agents/` as the example vault location. Substitute `<agents_root>/` with your actual directory — see [appendix/portability](appendix/portability.md).

```
<agents_root>/{agent_name}/
├── persona/
│   ├── IDENTITY.md            ← who I am, mission, role
│   ├── SOUL.md                ← personality, voice, evolution discipline
│   └── USER.md                ← about the operator
├── tools.md                   ← read paths, write paths, APIs, hard NOs
├── model.md                   ← LLM, token budget, caching strategy
├── memory/                    ← Atomic Notes (semantic memory)
│   ├── INDEX.md               ← always-loaded routing layer
│   ├── feedback_*.md
│   ├── decision_*.md
│   ├── project_*.md
│   ├── reference_*.md
│   └── user_*.md
├── wiki/                      ← Atomic Wiki (distilled corpus, optional)
│   ├── INDEX.md
│   └── *.md                   ← one page per concept
├── raw/                       ← source documents feeding the Wiki (optional)
├── journal/                   ← episodic narrative log
│   └── YYYY-MM/YYYY-MM-DD.md
└── log/                       ← autonomous run history (cron audit trail)
    └── YYYY-MM/YYYY-MM-DD.jsonl
```

When the **project scope** is active, the project root sits *above* the agents. Two variants:

**Variant A — single-project multi-agent (no shared roles):**

```
<agents_root>/{system_name}/
├── canon.md                   ← world / shared truth across all roles
├── style_guide.md             ← shared style rules
├── policy/                    ← locked project decisions
├── queue/                     ← work items roles pick up
└── agents/                    ← per-role Atomic Agents
    ├── director/
    ├── writer/
    └── editor/
```

**Variant B — multi-agent with shared role templates (Muse-style, three-layer cascade):**

```
<agents_root>/{system_name}/      e.g., <agents_root>/muse/
├── roles/                         ← LAYER 1: shared role definitions (the "class")
│   ├── director/{PROMPT.md, tools.md, model.md}
│   ├── writer/{PROMPT.md, tools.md, model.md}
│   ├── editor/...
│   └── ...
└── projects/
    └── {project_name}/             e.g., the-unfinished
        ├── canon.md                ← LAYER 2: project shared (world)
        ├── style_guide.md
        ├── policy/
        ├── queue/
        └── agents/                 ← LAYER 3: project × role instances
            ├── director/{persona/, memory/, wiki/, journal/, log/}
            ├── writer/{persona/, memory/, wiki/, journal/, log/}
            └── ...
```

The cascade lets multiple projects reuse role definitions while keeping per-project persona and memory isolated. Specific overrides general (CSS-like). Full details in [spec/06-multi-agent-projects](spec/06-multi-agent-projects.md).

---

## The three memory tiers (CoALA model)

Every Atomic Agent has all three, mapped to specific files:

| Tier | What it is | Where it lives | Update cadence |
|---|---|---|---|
| **Episodic** | What happened, when, in what sequence | `journal/` | Per session / per run |
| **Semantic** | Abstracted facts, decisions, preferences, locked truths | `memory/` (Atomic Notes) + `wiki/` (Atomic Wiki) | When a durable observation is captured |
| **Procedural** | Encoded skills, workflows, behavioral rules | `tools.md`, `IDENTITY.md` (operating doctrine), and any skill/playbook files | When the role itself changes |

This is not invented vocabulary — it's the [CoALA framework](https://arxiv.org/abs/2309.02427) applied. Every production-grade agent memory system maps to these three tiers.

---

## Atomic Memory: the recall subsystem

Atomic Memory = `memory/` + `wiki/` + their INDEXes. It's the part the agent uses every interaction to stay coherent.

Two sub-layers, different jobs:

```
ATOMIC MEMORY
├── Atomic Notes (memory/)         ← agent-state: corrections, decisions, learnings
│                                     PRIMARY observations from interactions
│                                     Captured by the agent during sessions
│
└── Atomic Wiki (wiki/) + raw/     ← distilled external corpus
                                      DERIVATIVE knowledge from source docs
                                      Compiled from raw/ via Karpathy-style ingestion
```

The two layers share the same mechanic — atomic markdown files + frontmatter + INDEX-driven recall — but they store fundamentally different content. Atomic Notes are *what the agent learned*. Atomic Wiki is *what the agent read*.

Most agents need both. Caldwell needs Atomic Notes (debt priorities, risk tolerance) AND Atomic Wiki (distilled tax docs, financial frameworks). another agent has both via openclaw's memory-core + memory-wiki plugins.

---

## Runtime assembly

Every time an agent runs (cron tick, skill invocation, openclaw gateway request), this is the order in which the system prompt is built:

```
1. IDENTITY.md          ← who I am, my mission
2. SOUL.md              ← personality, voice, evolution discipline
3. USER.md              ← about the operator
4. tools.md             ← what I can touch
5. model.md             ← (informational only — runtime already picked the model)
6. memory/INDEX.md      ← Atomic Notes routing layer
7. wiki/INDEX.md        ← Atomic Wiki routing layer (if present)
8. RECENT atomic notes  ← last N captured observations (default N=5)
9. PINNED atomic notes  ← tagged pinned: true in frontmatter
10. The work item       ← user message, queue item, or scheduled trigger
```

Steps 6-9 stay constant across calls until something is captured or pinned. That makes the system prompt **highly cacheable** — both Anthropic's prompt cache (5-min TTL) and the model's KV cache benefit. Per-call cost stays low even with rich memory.

The agent can call back during the conversation to load specific atomic notes or wiki pages by name. Selective, on-demand. See [spec/04-runtime-assembly](spec/04-runtime-assembly.md) for the exact mechanics.

---

## Diagram: data flow at runtime

```
┌────────────────────────────────────────────────────────────────────────┐
│                          ATOMIC AGENT RUNTIME                           │
│                                                                          │
│   ┌─────────┐    ┌──────────────────────┐    ┌──────────────────────┐ │
│   │ Trigger │ ─→ │  Loader              │ ─→ │  System Prompt        │ │
│   │ (cron / │    │  reads vault files   │    │  IDENTITY+SOUL+USER+  │ │
│   │  skill) │    │  in canonical order  │    │  TOOLS+INDEXes+notes  │ │
│   └─────────┘    └──────────────────────┘    └──────────┬───────────┘ │
│                                                          │              │
│                                                          ▼              │
│                                                 ┌─────────────────┐    │
│                                                 │ LLM call        │    │
│                                                 │ (per model.md)  │    │
│                                                 └────┬───────┬────┘    │
│                                                      │       │          │
│                       ┌──────────────────────────────┘       │          │
│                       ▼                                       ▼          │
│              ┌─────────────────┐                    ┌─────────────────┐│
│              │  Reply to user  │                    │ Capture-worthy? ││
│              │  / artifact     │                    │ Write atomic    ││
│              │  written        │                    │ note + INDEX    ││
│              └─────────────────┘                    │ entry           ││
│                                                     └─────────────────┘│
└────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                            ┌──────────────────┐
                            │   Vault state    │
                            │   (this folder)  │
                            └──────────────────┘
```

The vault is the only persistent state. The runtime is stateless — kill it, restart it, switch from cron to skill to openclaw to an HTTP service (`atomic-agents serve`, spec/37), the agent is the same agent.

---

## Diagram: file relationships

```
┌─────────────────────────────────────────────────────────────────────┐
│                         persona/  (HUMAN-CURATED, STABLE)            │
│   ┌────────────┐  ┌────────────┐  ┌────────────┐                   │
│   │ IDENTITY   │  │   SOUL     │  │   USER     │                   │
│   │ who+role   │  │ personality│  │ about user │                   │
│   └────────────┘  └────────────┘  └────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
              │
              │  promotion (hot atomic note → persona)
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  ATOMIC MEMORY  (AGENT-CURATED, EVOLVES)             │
│                                                                       │
│   ┌──────────────────────────┐    ┌──────────────────────────┐    │
│   │  memory/   (NOTES)        │    │  wiki/   (WIKI)           │    │
│   │  INDEX + atomic notes     │    │  INDEX + distilled pages  │    │
│   │  (semantic memory)        │    │  (compiled from raw/)     │    │
│   └──────────────────────────┘    └────────────┬─────────────┘    │
│                                                  │                    │
│                                                  ▼                    │
│                                          ┌──────────────┐            │
│                                          │   raw/       │            │
│                                          │ source docs  │            │
│                                          └──────────────┘            │
└─────────────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                journal/   (NARRATIVE, EPISODIC, BY DATE)             │
│         What I did. What happened. What I noticed today.             │
└─────────────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                log/   (RUN HISTORY, AUDIT TRAIL, JSONL)              │
│      Cron tick at T, model called, tokens used, status, error.       │
└─────────────────────────────────────────────────────────────────────┘

       ─────  Operational concerns (separate from content)  ─────
                  ┌──────────┐    ┌──────────┐
                  │ tools.md │    │ model.md │
                  └──────────┘    └──────────┘
```

---

## Why this shape (the load-bearing decisions)

**Why split persona from memory?**
Persona is *who I am*. Memory is *what I've learned*. They have different authors, different change cadences, different review processes. Mixing them means every memory edit risks corrupting personality.

**Why two memory sub-layers (Notes vs. Wiki)?**
They store different things. Atomic Notes are the agent's primary observations from interactions. Atomic Wiki is distilled external source material. Compressing both into one undifferentiated pool loses the distinction between "what I learned" and "what I read." Different lifecycles, different review processes, different decay rates.

**Why INDEX-driven recall instead of full-context loading?**
Cost and quality. A 2K-token INDEX guides selective loading of 1-3 atomic units (~1-2K tokens) instead of dumping 30K+ tokens of full memory. Memori paper benchmark: structured memory at ~5% of full context, no accuracy loss. Karpathy LLM Wiki: ~95% token reduction vs. naive RAG.

**Why plain markdown in a vault?**
Survives platform migrations. Editable on phone via Obsidian. Browsable without any AI tooling. Diffable in git. Future-proof against any specific runtime going away.

**Why `tools.md` and `model.md` as separate files?**
The agent itself reads them, not just the runtime. Operational concerns (capabilities, cost) become portable with the agent rather than coupled to the runtime config. Borrowed verbatim from Muse's structure — it works.

**Why "writes stay single-threaded" per agent?**
Cognition.ai's research on multi-agent systems found this is the rule that actually makes them work. Multiple agents can read the same vault content, but only one writes per file. Avoids the consistency hell of concurrent writes.

---

*Next: [spec/01-anatomy](spec/01-anatomy.md) details every file in an agent.*

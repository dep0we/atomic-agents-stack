# Atomic Agents

A specification + reference design for building consistent, self-improving AI agents that live as plain markdown files in any folder structure (the "vault") and run anywhere.

> **Atomic Agents** is the system. An **Atomic Agent** is one specific agent built to the spec.

**Reference implementation:** the `atomic_agents` Python package alongside this `docs/` directory. See the [repo README](../README.md) for install + quick start. The spec is the source of truth; the package is one conforming implementation.

---

## What this is

A complete answer to: *"How do I build an AI agent (Caldwell, agent-a, agent-b, Muse, another agent, future ones) that:*

- *has a stable, evolvable identity?*
- *remembers what it learns across sessions and across years?*
- *runs the same way whether triggered by a cron job, a Claude skill, or an interactive harness like openclaw?*
- *gets better over time without rewrites?*
- *stays portable across model upgrades and platform changes?"*

This folder is the source of truth. Every agent — another agent included, eventually — conforms to this spec.

---

## The 30-second mental model

Every Atomic Agent is **a folder of plain markdown files** in this vault. Six concerns, separated:

| Concern | Where | What |
|---|---|---|
| **Self-definition** | `persona/IDENTITY.md`, `SOUL.md`, `USER.md` | Who the agent is |
| **Capabilities & boundaries** | `tools.md` | What the agent can read/write/call |
| **Cost profile** | `model.md` | Which LLM, what budget |
| **Atomic Notes** | `memory/` | What the agent has learned (semantic memory) |
| **Atomic Wiki** | `wiki/` + `raw/` | Distilled knowledge from ingested sources (corpus distillation) |
| **Journal** | `journal/` | Narrative log (episodic memory) |
| **Run history** | `log/` | Autonomous execution audit trail |

**Atomic Memory** = `memory/` + `wiki/` + their `INDEX.md`s. This is the recall subsystem — the load-bearing part of self-improvement.

Two layers per agent: human-curated persona on top, agent-curated memory below. INDEX-driven recall (always load index + N atomic units) instead of dumping the corpus into context.

---

## How to read this folder

| If you want to... | Read |
|---|---|
| **Get an agent running in 15 minutes** | [getting-started](getting-started.md) |
| **Deploy an agent autonomously (pre-deploy checklist, naming, audit)** | [GOVERNANCE.md](GOVERNANCE.md) |
| Understand the architecture at a glance | [architecture](architecture.md) |
| **Contribute or change framework code (design ethos + taste rules)** | [../CLAUDE.md](../CLAUDE.md) |
| **Architectural tensions to protect when changing code** | [TENSIONS.md](TENSIONS.md) |
| **Working methods that produced this codebase's quality** | [methodology.md](methodology.md) |
| Build a new agent from zero (per-runtime) | [implementation/cron-agent](implementation/cron-agent.md), [implementation/claude-skill-agent](implementation/claude-skill-agent.md), or [implementation/chatgpt-skill-agent](implementation/chatgpt-skill-agent.md) |
| Look at a complete worked example (single-agent) | [samples/caldwell](samples/caldwell/README.md) |
| Understand a specific design choice | [spec/01-anatomy](spec/01-anatomy.md) through [spec/09-cost-observability](spec/09-cost-observability.md) |
| Build multi-agent with shared role templates (Muse-style) | [spec/06-multi-agent-projects](spec/06-multi-agent-projects.md) — the three-layer cascade section |
| **Track cost + token usage across all agents** | [spec/09-cost-observability](spec/09-cost-observability.md) + [implementation/cost-dashboard](implementation/cost-dashboard.md) |
| **Use cheaper LLMs for sub-tasks (parallel helpers)** | [spec/10-helpers](spec/10-helpers.md) |
| **Score agent quality with rubrics + LLM-as-judge** | [spec/08-evaluation](spec/08-evaluation.md) + [implementation/eval-runner](implementation/eval-runner.md) |
| **Improve agents over time (eval-driven tuning)** | [spec/11-tuning](spec/11-tuning.md) + [implementation/tuning-analyzer](implementation/tuning-analyzer.md) |
| **Build agents that pursue persistent goals (Muse Director-style)** | [spec/12-goals-and-intent](spec/12-goals-and-intent.md) |
| **Keep factual claims cited + verifiable (no hallucinated numbers)** | [spec/13-research-integrity](spec/13-research-integrity.md) |
| **Use Atomic Agents without Obsidian / on a different OS / hand off to a teammate** | [appendix/portability](appendix/portability.md) |
| Adapt this for another agent (post-spec-lock) | (TBD — separate doc once the spec is settled) |

The spec docs are numbered for reading order. The samples are populated with realistic example content, not lorem ipsum.

---

## Key terms (locked vocabulary)

- **Atomic Agents** — the system / convention / family
- **Atomic Agent** — one specific agent (Caldwell, agent-a, another agent, etc.)
- **Atomic Memory** — recall subsystem (Notes + Wiki + INDEX)
- **Atomic Notes** — agent-state observations (semantic memory)
- **Atomic Wiki** — distilled corpus pages from ingested source material
- **Persona** — IDENTITY/SOUL/USER (the agent's self-definition)
- **Journal** — narrative log (episodic memory)
- **`raw/`** — source documents that feed the Wiki
- **INDEX.md** — always-loaded routing layer per memory sublayer

---

## Status

| Item | State |
|---|---|
| Spec v1 written | ✅ this folder |
| Sample agent (Caldwell) populated | ✅ samples/caldwell/ |
| Implementation guides | ✅ implementation/ (cron, Claude skill, ChatGPT skill, shared helper, cost dashboard) |
| Portability appendix (non-default setups) | ✅ appendix/portability.md |
| Cost & observability spec | ✅ spec/09-cost-observability.md |
| Cost dashboard implementation guide | ✅ implementation/cost-dashboard.md |
| Sample log JSONL data (Caldwell) | ✅ samples/caldwell/log/2026-05/ |
| Cost guardrail enforcement specified | ✅ implementation/shared-helper.md |
| Helpers spec (Patterns A + B) | ✅ spec/10-helpers.md |
| Helper API in shared helper | ✅ implementation/shared-helper.md (helper_call + helper_call_parallel) |
| Caldwell helpers example | ✅ samples/caldwell/journal/2026-05/2026-05-07.md |
| Eval spec | ✅ spec/08-evaluation.md |
| Eval runner implementation guide | ✅ implementation/eval-runner.md |
| Caldwell rubric + judge + 5 golden tests | ✅ samples/caldwell/evals/ |
| Tuning spec (eval-driven self-improvement) | ✅ spec/11-tuning.md |
| Tuning analyzer implementation guide | ✅ implementation/tuning-analyzer.md |
| Sample tuning report (Caldwell) | ✅ samples/caldwell/evals/tuning_reports/2026-05-08_proposal.md |
| Filename date suffix convention | ✅ spec/03-file-formats.md (When to include a date) |
| Goals & Intent spec (goal-driven agents) | ✅ spec/12-goals-and-intent.md |
| Agent operating mode taxonomy (reactive/goal-driven/hybrid) | ✅ spec/01-anatomy.md (Agent operating mode) |
| Research integrity spec (3-layer factual accuracy) | ✅ spec/13-research-integrity.md |
| Factual accuracy rubric dimension + HF8 | ✅ samples/caldwell/evals/rubric.md (Wave 8 rebalance) |
| Helper provenance preservation | ✅ spec/10-helpers.md (Wave 8 update) |
| Citation-required captures | ✅ spec/05-capture-rules.md (Wave 8 update) |
| Reference Python implementation (`atomic_agents` package) | ✅ this repo — `atomic_agents/` |
| Eval runner module | ✅ `atomic_agents.eval` |
| Tuning analyzer module | ✅ `atomic_agents.tuning` |
| Goal manager module | ✅ `atomic_agents.goal` |
| Schema migration runner | ✅ `atomic_agents.migrate` |
| Path 1 tool-call captures | ✅ `atomic_agents._capture` |
| Multi-agent project cascade loader | ✅ `atomic_agents._cascade` |
| Cost dashboard module | ✅ `atomic_agents.dashboard` |

## Portability note

The docs use `~/agents/` as the example path. Throughout the spec, **substitute your own `<agents_root>` directory**. Nothing depends on this specific path — see [appendix/portability](appendix/portability.md) for what's actually required vs. just the example setup.

Same goes for Obsidian Sync, your-server (always-on home server), Telegram, and every other tool referenced by name — they're examples. The spec works on any OS, with any markdown editor, any sync mechanism (or none), and any LLM provider.

---

## Design principles (the "why" in one screen)

1. **Plain markdown, vault-native, source-of-truth in this Obsidian vault.** No proprietary databases. Survives any platform migration.
2. **Atomicity everywhere.** Small, individually-named, individually-loadable files with frontmatter. Composed via INDEX, not concatenated into monoliths.
3. **Selective recall, not corpus-dumping.** Always load INDEX + persona + a few atomic units. Pull more on demand. Token cost stays small even when memory grows.
4. **Persona ≠ Memory.** Identity is human-curated and stable. Memory is agent-curated and evolves. They live in separate files with different lifecycles.
5. **Job mechanics ≠ Persona.** `tools.md` and `model.md` are operational concerns and live in their own files. Editing them doesn't touch personality.
6. **Single-threaded writes.** An agent writes only to its own folder. It may read other agents' folders if explicitly permitted.
7. **Self-improvement is structural, not magic.** New observations become Atomic Notes. Hot atomic notes get promoted into persona. Stale notes get archived. Lint catches contradictions. The loop is the architecture.
8. **Shared source files, runtime-specific adapters.** A cron job, a Claude skill, a Codex CLI skill, a ChatGPT skill, and an openclaw gateway all read the same agent files. Skills follow the [Agent Skills open standard](https://agentskills.io). But the runtimes themselves differ in tool model, filesystem access, prompt caching, and write permissions — so each runtime has its own adapter that translates the same vault content into something that runtime can execute. The promise is *shared source*, not *identical execution*. See [spec/04-runtime-assembly#runtime-conformance-checklist](spec/04-runtime-assembly.md#runtime-conformance-checklist) for what a conforming adapter must implement.

---

## Acknowledgments / lineage

This spec composes ideas from several traditions:

- **Karpathy's LLM Wiki pattern** — the corpus-distillation layer (Atomic Wiki)
- **Zettelkasten / Andy Matuschak's evergreen notes** — the atomic, individually-named knowledge unit
- **CoALA (Cognitive Architectures for Language Agents)** — the episodic/semantic/procedural memory tiers
- **Memori** — structured memory beats unfiltered context (~5% of full-context with no accuracy loss)
- **SemaClaw paper** — file-based persistent persona + wiki-based knowledge infrastructure
- **Soul Spec / SOUL.md ecosystem** — markdown personas as portable, framework-agnostic identity
- **OpenClaw memory-wiki + memory-core plugins** — the in-runtime implementation another agent already uses
- **Muse's role/persona/tools/model split** — the operational file layout (`prompt.md`, `tools.md`, `model.md`, `soul.md`)
- **Cognition.ai's "writes stay single-threaded"** — multi-agent coordination rule

See [spec/07-research-foundations](spec/07-research-foundations.md) for citations and how each idea maps in.

---

*This spec is v1, locked 2026-05-06. Changes go through a versioned spec bump (see `schema_version` field in every memory file's frontmatter).*

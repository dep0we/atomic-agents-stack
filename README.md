# atomic-agents-stack

[![Tests](https://github.com/dep0we/atomic-agents-stack/actions/workflows/test.yml/badge.svg)](https://github.com/dep0we/atomic-agents-stack/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://github.com/dep0we/atomic-agents-stack/blob/main/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/dep0we/atomic-agents-stack/blob/main/LICENSE)
[![Version](https://img.shields.io/badge/version-1.1.0-blue)](https://github.com/dep0we/atomic-agents-stack/blob/main/CHANGELOG.md)
[![PyPI](https://img.shields.io/badge/pypi-atomic--agents--stack-blue)](https://pypi.org/project/atomic-agents-stack/)

> **AI agents that live in your folder, not someone else's database.**

Vault-native, MIT-licensed, Markdown-source-of-truth.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/dep0we/atomic-agents-stack/main/docs/assets/atomic-agents-hero-dark.svg">
    <img src="https://raw.githubusercontent.com/dep0we/atomic-agents-stack/main/docs/assets/atomic-agents-hero.svg" alt="Atomic Agents at a glance: an agent is a folder of Markdown files (persona, tools, memory, wiki, journal, log); the runtime is stateless and wrapped in cost guardrails; every run writes a JSONL audit line, a typed memory note, and a journal entry. Same agent definition runs from cron, launchd, a Claude Code skill, or embedded in Python." width="100%">
  </picture>
</p>

**New here?** Read the [visual guide](https://dep0we.github.io/atomic-agents-stack/) — a 30-minute plain-English walkthrough of what an atomic agent is, how it runs, the safety layer, and the protocol-pattern scaling story.

---

## Why this exists

Your AI agent's persona, memory, and audit trail live in someone else's database. The persona is locked inside Letta's hosted memory blocks. The memory is in Mem0's vector store. The audit trail is in LangSmith. The cost guardrails are in your wrapper code. Migrating any of it costs a project.

**There's another shape: your agents live in your folder.** Plain markdown files you can `cat`, `grep`, and `git diff`. No hosted service. No vendor that owns your agent's continuity. Switch laptops, you copy a folder.

Concretely: INDEX.md routing layer. Persona in `IDENTITY.md` / `SOUL.md` / `USER.md`. Typed atomic notes. Audit trail as JSONL. Cost guardrails in markdown config. Crash-safe writes (temp + fsync + rename + parent-dir fsync — a power loss never leaves a half-written note). Schema migrations are scripts you read before running. The runtime is stateless — point cron, launchd, a Claude Code skill, or embedded Python at the folder.

That's what `atomic-agents-stack` defines, in locked spec docs (plus active RFCs), with a Python reference implementation, 5,100+ tests, and a Caldwell sample shipping 5 days of real JSONL run logs, a rendered cost dashboard, evals across happy / edge / adversarial / decline categories, and a helper-pattern day showing ~76% cost savings vs. all-Opus.

A home user with one agent and an org with a fleet experience the same framework — graceful, coherent, self-explanatory at every scale.

**Files are the default — and the win. The same agent scales to a database substrate when an operator prefers that.** Today: `LogBackend` ships filesystem (JSONL) + SQLite + Postgres reference impls; `LockBackend` ships filesystem + Redis; `AgentProfileBackend` + `ToolRegistryBackend` ship filesystem + SQLite; `MemoryBackend` ships filesystem + Postgres (FTS/tsvector, PR 1 of [#258](https://github.com/dep0we/atomic-agents-stack/issues/258)) reference impls. Remaining backend Postgres adapters arrive via the same protocol seams. **The agent's persona doesn't change. The folder layout doesn't change. The audit trail doesn't change.** Only what's registered as the backend for each protocol changes — one env var flips the backend (plus a connection URL for database substrates), no rewrite, no migration. The conformance test suite gates every substrate against the same contract, so the agent on your laptop and the agent running behind a fleet HTTP service answer the same way for the same inputs.

---

## Quick start

**To use the framework** (install from PyPI, then point it at your vault):

```bash
# Install
pip install atomic-agents-stack
# or with uv:
uv add atomic-agents-stack

# Configure your vault location (default: ~/docs/agents)
export ATOMIC_AGENTS_ROOT=~/agents

# Verify everything's wired up
atomic-agents doctor

# Run an agent (assuming you've created one; see the getting-started guide)
atomic-agents run myagent --work-item "What should I focus on today?"

# See the cost dashboard
python -m atomic_agents.dashboard render
open ~/agents/_dashboard/index.html
```

**To contribute or run the full test suite** (clone + dev install):

```bash
git clone https://github.com/dep0we/atomic-agents-stack.git
cd atomic-agents-stack
uv sync
uv run pytest
```

```python
# Programmatic use — embed in your own Python app
from atomic_agents import AtomicAgent

agent = AtomicAgent(name="myagent", trigger="cron")
response = agent.call(work_item="Daily morning brief")
print(response.text)
print(f"Cost: ${response.cost_usd:.4f}")
print(f"Captures: {len(response.captures)}")
```

See [`docs/getting-started.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/getting-started.md) for the 15-minute clone-to-running-agent walk-through and [`docs/deployment/programmatic.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/programmatic.md) for the complete programmatic API + public exception table.

---

## What an agent looks like

An `atomic-agents-stack` agent is a folder. Everything stateful is in plain text:

```
~/agents/myagent/
├── persona/
│   ├── IDENTITY.md          who I am, my mission, my scope
│   ├── SOUL.md              personality, voice, how I evolve
│   └── USER.md              about the operator, what they care about
├── tools.md                 what I can read, write, and call
├── model.md                 LLM + token budget + cost guardrails
├── memory/                  typed atomic notes (feedback / decision / project / reference / user)
│   ├── INDEX.md             always-loaded routing layer
│   └── *.md                 one file per note
├── wiki/                    distilled corpus (optional)
├── journal/                 narrative episodic log
│   └── YYYY-MM/YYYY-MM-DD.md
└── log/                     audit trail (one JSONL line per run)
    └── YYYY-MM/YYYY-MM-DD.jsonl
```

When the agent runs, it loads these files in a canonical order, assembles the system prompt, calls the LLM, extracts capture markers from the response, writes new atomic notes, appends to the journal, and logs the run as one JSONL line. The vault is the only persistent state. The runtime is stateless.

For a complete worked example with real persona, memory, journal, evals, and a sample dashboard rendered from real log data, see [`docs/samples/caldwell/`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/samples/caldwell/).

---

## Current limits

Honest about what isn't shipped or fully tested:

- **v1.0, single maintainer.** At v1.0 the Protocol surface is stable per SemVer Major; breaking changes require a v2.0 bump. Minor releases add features without breaking existing agents.
- **macOS / Linux primary; Windows under-tested.** `atomic_agents/_locks.py` uses POSIX `fcntl`. iOS can't run the runtime at all (Markdown vault files sync there fine; see [`docs/deployment/obsidian.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/obsidian.md)).
- **`MemoryBackend` + `LLMBackend` + `JudgeBackend` + `LockBackend` + `LogBackend` + `AgentProfileBackend` + `ToolRegistryBackend` + `MandateBackend` + `PolicyBackend` + `PersonaBackend` + `CorpusBackend` are shipped from the protocol roadmap.** `MemoryBackend` ships a filesystem reference impl + `PostgresMemoryBackend` (FTS/tsvector, non-semantic, `supports_semantic_search=False`) + operator override surface: `ATOMIC_AGENTS_MEMORY_BACKEND` env var (default `"filesystem"`, or `"postgres"` with `ATOMIC_AGENTS_MEMORY_BACKEND_URL`) or `AtomicAgent(..., memory_backend=...)` kwarg select the backend; `get_default_memory_backend(agent_root)` is the public factory; unknown ids fail-fast at construction; filesystem override surface added in [#382](https://github.com/dep0we/atomic-agents-stack/issues/382), Postgres (FTS) reference impl in [#258](https://github.com/dep0we/atomic-agents-stack/issues/258) PR 1. pgvector semantic recall deferred to PR 2/PR 3. Four reference LLM backends (Anthropic, OpenAI direct via `OpenAICompatibleLLMBackend`, Moonshot via the same factory class, and Vertex Gemini via `VertexGeminiLLMBackend` with the optional `[vertex]` extra) register lazily on the first model lookup (not at module import — see spec/31); third-party Bedrock / vLLM-local backends can register without forking core. `LockBackend` ships filesystem + Redis reference impls; `LogBackend` ships filesystem + SQLite; `AgentProfileBackend` ships filesystem + SQLite (with JSON-based snapshot trio + `supports_skills` capability + Implementer contract for future Postgres / git / SaaS-database adapters); `ToolRegistryBackend` ships filesystem + SQLite (with hybrid metadata-in-SQL + handler-bodies-on-disk storage shape + `install` / `uninstall` capability flipped True on SQLite + cross-scope isolation enforced at the SQL layer + Implementer contract for future PyPI / git / company-internal-HTTP / SaaS-database adapters); `PolicyBackend` ships filesystem reference impl reading `<project_root>/policy.md` (markdown + embedded YAML), with cost-cap MIN composition, tool / MCP / model surfaces enforced by default after PR 4 (set `ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP=false` to opt back into log-only mode), `policy_decision` audit event family with `decision_kind` / `axis` discriminators, and Implementer contract for future Postgres / SaaS / org-admin-console adapters. `PersonaBackend` ships filesystem reference impl at `<scope_root>/.personas/<persona_id>/{IDENTITY,SOUL,USER}.md` + `metadata.json`, with `persona.link.md` ownership trigger, snapshot trio nested under each persona's directory (`supports_snapshot=True`), `atomic-agents persona` CLI lifecycle, `AgentProfileBackend` composition that drops persona fields when externally owned, and Implementer contract for future Postgres / SaaS / git PersonaBackend adapters. `CorpusBackend` ships `FilesystemCorpusBackend` + `SQLiteCorpusBackend` with FTS5 reference impls; `<agent_root>/wiki/` + `<agent_root>/raw/` per-agent corpus; `render_index_summary(corpus)` Protocol method; page-count performance cliff WARN at 1000+ pages on `supports_full_text_search=False` filesystem (with the `ATOMIC_AGENTS_CORPUS_BACKEND=sqlite` remedy hint); `atomic-agents corpus` CLI; operator override via `ATOMIC_AGENTS_CORPUS_BACKEND` env var or `corpus_backend=` constructor kwarg; Implementer contract in spec/34. Org-scale deployments today can run filesystem + Redis + SQLite mixed (e.g., SQLite for logs + profiles + tools, Redis for locks); future Postgres adapters slot in via the same Protocol seams.
- **Cost guardrail `alert` action is log-backed today.** The `alert_channel` field is parsed, but external dispatch (Telegram / email / webhook) is not wired up yet. Today's alerts go to the run log; the dashboard surfaces them visually. See [`#70`](https://github.com/dep0we/atomic-agents-stack/issues/70).
- **Cross-host locking is shipped via the `LockBackend` Protocol** ([`#60`](https://github.com/dep0we/atomic-agents-stack/issues/60) — locked at PR 4). Default filesystem backend preserves the pre-arc per-host POSIX `fcntl.flock` semantic for single-host deployments; operators on Cloud Run / Kubernetes / gizmo can opt into `RedisLockBackend` via `ATOMIC_AGENTS_LOCK_BACKEND=redis`. Cross-host correctness is now a Protocol-level concern, not an operator burden.
- **`__all__` lags behind raised exceptions.** A few public-facing exceptions are raised inside the package but not in `atomic_agents.__all__` yet ([`#99`](https://github.com/dep0we/atomic-agents-stack/issues/99)); documented in `docs/deployment/programmatic.md`.

---

## How it compares to alternatives

This is the slot in the AI-agent-tooling landscape `atomic-agents-stack` occupies, in narrow defensible claims rather than competitive sniping:

| | Atomic Agents | Letta | Mem0 | LangGraph + LangSmith | Direct SDK + your scripts |
|---|---|---|---|---|---|
| **Source of truth for agent state** | Markdown files in a folder you own | Postgres-backed memory blocks (cloud or self-hosted Docker) | Vector / structured memory store (cloud or OSS) | Checkpointer + long-term store you wire in | Whatever you build |
| **Persona layer** | Spec-defined `IDENTITY.md` / `SOUL.md` / `USER.md` files; promotion loop from memory | `persona` / `human` memory blocks | Operator-defined memory | Prompts + state schemas | Prompts |
| **License (core)** | MIT | Apache-2.0 (OSS); managed Letta Cloud also offered | Apache-2.0 (OSS); managed Mem0 also offered | MIT (LangGraph OSS); LangSmith is hosted | Whatever |
| **Required server / DB** | None (just files + Python) | Postgres recommended for production | Vector store backend | None for OSS; Postgres-style for `langgraph-checkpoint-postgres` | None |
| **Audit trail** | JSONL per run with `parent_run_id` rollups; helper + delegate + tool + capture lines all link back | Dashboards in Letta UI / cloud | Mem0 dashboards | LangSmith (hosted) | Build it |
| **Cost guardrails** | First-class — daily / monthly caps, threshold warnings, fallback action, `critical=True` override, tree-cap across delegates | Per their pricing model | Per their pricing model | Not built into core OSS | Build it |
| **Multi-agent coordination** | Role × project cascade defined in spec/06 | Multi-agent shared memory blocks | Agent-shared memory pools | LangGraph: graph-based orchestration (more flexible) | Build it |
| **Numbered, locked spec** | 37 locked docs in `docs/spec/` (+ 8 DRAFTs/RFCs in progress) | API + concept docs | API + concept docs | API reference + concept docs | None |
| **Reference runtime** | Python, macOS / Linux primary | Python (server) + multi-language clients | Python (OSS) + multi-language clients | Python + JavaScript | Whatever |

**Where the alternatives win:**

- **Letta** ships a polished hosted UX and multi-language clients; vault-only ships neither.
- **Mem0** owns the embeddings-retrieval research; if memory quality is the bottleneck, look there first.
- **LangGraph** wins on graph-shaped orchestration; **LangSmith** observability is broader than any single repo's audit trail can replicate.
- **Direct SDK** wins when the problem is so domain-specific that any framework's structure is overhead.

**Where Atomic Agents wins:**

- **Markdown-source-of-truth, human-editable.** Operators can edit persona / tools / memory from any text editor or Obsidian without a vendor app.
- **No required server.** The framework is "files + Python." A complete agent runs on a laptop with zero infrastructure.
- **Spec-level file layout.** 37 numbered docs lock the contract (plus 8 DRAFTs/RFCs in progress); conformance is testable; alternate implementations are possible.
- **Crash-safe writes by default.** `temp file + fsync + rename + parent-dir fsync` for every mutation; an interrupted run leaves recoverable artifacts, not corruption.
- **Cost story is structural, not bolted on.** Daily / monthly caps + tree-cap for delegations + per-call cost reservation for helper batches + a `critical=True` override that's part of the API, not a per-vendor workaround.

---

## The spec is the product

`atomic-agents-stack` is a **spec** for vault-native AI agents, plus one **reference implementation** in Python. The spec is the central artifact; anyone can build agents to the spec without using this code.

Start at [`docs/README.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/README.md) for the spec entry point. The locked spec docs (plus active RFCs) in [`docs/spec/`](https://github.com/dep0we/atomic-agents-stack/tree/main/docs/spec/) cover:

- [01: Anatomy](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/01-anatomy.md): file layout, persona, memory, wiki, journal, log
- [02: Atomic Memory](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/02-atomic-memory.md): Notes + Wiki + INDEX-driven recall
- [03: File formats](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/03-file-formats.md): frontmatter schemas + filename conventions
- [04: Runtime assembly](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/04-runtime-assembly.md): canonical load sequence
- [05: Capture rules](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/05-capture-rules.md): when and how agents write to memory
- [06: Multi-agent projects](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/06-multi-agent-projects.md): role x project cascade
- [07: Research foundations](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/07-research-foundations.md): lineage and prior art
- [08: Evaluation](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/08-evaluation.md): rubrics + LLM-as-judge framework
- [09: Cost & observability](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/09-cost-observability.md): pricing, dashboard, guardrails
- [10: Helpers](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/10-helpers.md): cheap-LLM workers for transformation subtasks
- [11: Tuning](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/11-tuning.md): eval-driven self-improvement
- [12: Goals & intent](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/12-goals-and-intent.md): goal-driven agents
- [13: Research integrity](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/13-research-integrity.md): citations + factual accuracy
- [14-19](https://github.com/dep0we/atomic-agents-stack/tree/main/docs/spec/): capture markers, delegation, dreams, skills, MCP, alternative-runtime contracts
- [20: Memory backend protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/20-memory-backend.md): the protocol-pattern moat; `ATOMIC_AGENTS_MEMORY_BACKEND` env var + `memory_backend=` kwarg + `get_default_memory_backend` factory + operator override surface (#382); Postgres addendum: FTS/tsvector non-semantic reference impl (#258 PR 1)
- [21: Lock backend protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/21-lock-backend.md): multi-host lock primitive; filesystem + Redis reference impls
- [22: Log backend protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/22-log-backend.md): JSONL + SQLite reference impls; indexed query / aggregate / retention
- [24: AgentProfile backend protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/24-agent-profile-backend.md): agent registry primitive; filesystem + SQLite reference impls
- [25: ToolRegistry backend protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/25-tool-registry-backend.md): tool catalog primitive; install / uninstall capability
- [26: Cascade bundle](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/26-cascade-bundle.md): pre-rendered cascade for skill-mode loads (DRAFT)
- [27: Doctor](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/27-doctor.md): preflight verification
- [28: Judge layer](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/28-judge-layer.md): pre-action validation; ESCALATE + REVISE state machines
- [29: Mandates](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/29-mandates.md): durable revocable scoped authority; reservation pattern + crash recovery
- [30: Responsibility audit](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/30-responsibility-audit.md): per-action accountability trail (DRAFT)
- [31: LLM backend protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/31-llm-backend.md): provider routing; Anthropic + OpenAI + Moonshot + Vertex Gemini reference impls
- [32: Policy backend protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/32-policy-backend.md): fleet-wide `policy.md`; cost-cap MIN composition + allowlist enforcement
- [33: PersonaBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/33-persona-backend.md): persona ownership, snapshot/restore, `persona.link.md` format
- [34: CorpusBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/34-corpus-backend.md): wiki/raw corpus protocol; filesystem + SQLite (FTS5) reference impls; GB-scale indexed full-text search
- [35: init wizard](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/35-init-wizard.md): `atomic-agents init` on-ramp; template scaffolding + Add-to-it merge; CI-friendly `--from-template` (RFC)
- [36: MCPServerRegistryBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/36-mcp-server-registry-backend.md): MCP server catalog + install/uninstall; `FilesystemMCPServerRegistryBackend` + `HTTPMCPServerRegistryBackend` reference impls with tier-1/2/3 capability negotiation; `atomic-agents mcp-registry` CLI (LOCKED, PR 5 of 5 v1.0; amended post-v1.0 for GHSA-xhcr-cqfr-m3hv — HTTP scheme gate + MCPClientPool spawn allowlist)
- [37: atomic-agents serve](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/37-serve.md): thin HTTP wrapper exposing `agent.call()` over four routes; Cloud Run / GKE / Fly.io / Render; identity header pass-through; DRAFT (#342)
- [38: SecretBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/38-secret-backend.md): credential resolution abstraction; `FilesystemSecretBackend` (env → Keychain → keys.json) + `GCPSecretManagerBackend`; `atomic-agents secrets` CLI; LOCKED (#340)
- [39: OpenTelemetry trace export](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/39-otel-export.md): off-by-default tracing seam; `atomic_agents.call` parent span with cost/token/outcome attributes; `[otel]` optional extra; OTLP/HTTP exporter only (no gRPC); DRAFT (#341 PR 1)
- [40: Canonical-shape export contract](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/40-canonical-export.md): `Exportable` companion Protocol; typed export result hierarchy; filesystem identity export impls for Memory, Log, Mandate, Corpus, Lock, Secret; `supports_canonical_export` capability field; parametrized round-trip conformance scaffold; T15 Position B spine; LOCKED (#379)
- [41: GoalBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/41-goal-backend.md): goal state persistence abstraction; `FilesystemGoalBackend` reference impl; write-path adoption (#448 PR1): `GoalManager` load/save/history routed through backend, `AtomicAgent.goal_backend` live-wired, archive data-loss fix; `apply_transition()` atomic primitive with CAS guard (`expected_from_status`); goal-outcome coordinator (`dispatch_sub_goal_as_outcome()`, fail-closed cost gate, `CostGuardrailBlocked`); `GoalExport` wired into spec/40 Exportable; clock injection + `GoalManager.archive()` thin shim + `agent_root` resolved at init (#483 PR1, versioned normative addendum); backend-universe alignment: coordinator threads gate agent's `log_backend`/`policy_backend`/`profile_backend` into `OutcomeRunner` so runner spends through the same cost universe the pre-dispatch gate checked (#496 PR1); LOCKED (#425 + #448 PR1/PR2/PR3 arc-closer + #483 PR1 cleanup + #496 PR1 universe alignment)
- [42: OutcomeBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/42-outcome-backend.md): outcome state persistence abstraction; `FilesystemOutcomeBackend` reference impl; write-path adoption (#448 PR2): `OutcomeRunner.run()`'s single write site routed through `write_result`, `outcome_backend=` kwarg, custom-`output_dir` `result.json` relocated to the canonical path (orphan-bug fix); `result.json` byte-identical on-disk for the default path + portable `export()` with relative artifact refs; `OutcomeExport` wired into spec/40 Exportable; 9-MUST Implementer Contract (write-once/result-immutability as the unique 9th); LOCKED (#426 + #448 PR2)
- [43: JournalBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/43-journal-backend.md): journal persistence abstraction; `FilesystemJournalBackend` reference impl; ADOPT-NOW — all three hand-synced `rglob` read sites (bundle/agent/dream) wired; `append_entry` atomic write (fcntl.flock sidecar); `JournalExport` wired into spec/40 Exportable; DRAFT (#427)
- [44: QueueBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/44-queue-backend.md): cascade work-queue protocol; `FilesystemQueueBackend` reference impl; POSIX-rename atomic claim; `QueueExport` wired into spec/40 Exportable; closes TENSIONS T4 (single-host claim atomicity); SCAFFOLDING-ONLY carve; DRAFT (#428)
- [45: IdempotencyBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/45-idempotency-backend.md): at-most-once execution guarantee; `FilesystemDedupLedger` reference impl; `begin(key)->DedupDecision` atomic O_EXCL lease; `commit()` MARKER-ONLY terminal entry; `lookup()`/`release_lease()`; `IdempotencyExport` wired into spec/40 Exportable (terminal-only); dual-probe doctor; PR2 wired the two-phase dedup gate into `agent.call(idempotency_key=...)` (serve/queue/cron triggers, RunRecord `idempotency_key`/`replayed_run_id` audit fields, spec/22 versioned normative addendum); LOCKED (#520)
- [46: EmbeddingBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/46-embedding-backend.md): pluggable embedding generation for semantic recall; `OpenAIEmbeddingBackend` reference impl; `embed()`/`embed_batch()` MUST-NOT-RAISE (None-fallback) + `len(out)==len(in)` invariant; `EMBEDDING_PRICING` cost table isolated from chat `PRICING`; dimension honesty enforced at construction + produced vector; key resolution via the SecretBackend; `supports_input_type` flag (param deferred to #544 PR2); registry + pgvector wiring shipped PR3 (#200); batch embed cost gate at `agent.call()` capture-commit site + `PRIMITIVE_EMBED` + `check_embedding_backend()` doctor check + spec/20/22/34 normative addenda shipped #544 PR1; #544 PR2a: dedicated `embed_cost` JSONL record (`cost_usd=actual_usd` — now visible to `sum_cost_for_period` across calls) + merge-write pre-read reservation (sized from preserved target body, Principle #4) + spec/22 versioned normative addendum; query-embed gate (CLI corpus-query, [#564](https://github.com/dep0we/atomic-agents-stack/issues/564)) + LOCK ceremony deferred to #544 PR2; DRAFT (#200 PR2 + #544 PR1 + #544 PR2a)
- [47: ConversationBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/47-conversation-backend.md): multi-turn conversation persistence; `FilesystemConversationBackend` reference impl; `Principal` typed identity key + `LOCAL_PRINCIPAL` home-user default; `load_turns()` budget-bounded eviction + `write_turn()` atomic per-principal flock write; Guard (2) two-part cross-principal isolation (resolve-name + inode-identity); `agent.call(conversation_id=...)` three-channel wiring with prior-turn injection into `messages[]`; SQLite + Postgres v2→v3 migration adds `conversation_id` column + predicate; DRAFT (#535 PR1)
- [48: PrincipalBackend Protocol](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/48-principal-backend.md): identity-derivation abstraction; `LocalPrincipalBackend` (home-user zero-config) + `StaticClaimsPrincipalBackend` (sha256 NUL-separator key) reference impls; HARD-REFUSE gate in `agent.call()` placed BEFORE idempotency dedup lookup (closes cross-principal cached-run replay); serve HYBRID flow fail-closed opt-in (`identity_is_perimeter_verified`); 12-MUST Implementer Contract; DRAFT (#556 PR1)

Each spec doc is locked when the implementation matches and tests pass. Spec changes that imply implementation changes get filed as GitHub issues. **Spec docs separate shipped behavior from explicit future / deferred boundaries** — sections that describe behavior not yet implemented are explicitly marked as such, not silently aspirational.

---

## Backend protocols — the scaling story

The framework is moving toward swappable backends layer by layer. The shape: a Python `Protocol` for each primitive that touches storage, a filesystem-default implementation, capability advertisement, and a conformance test suite. Same agent definitions, same `call()` flow, same audit trail — different backends registered.

| Backend | Status | What it does | Spec |
|---|---|---|---|
| `MemoryBackend` | ✅ Shipped | Notes + Wiki + INDEX storage; filesystem default + Postgres FTS/tsvector reference impl (#258 PR 1) | [`spec/20`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/20-memory-backend.md) |
| `LLMBackend` | ✅ Shipped | Provider routing; Anthropic + OpenAI + Moonshot + Vertex Gemini reference impls | [`spec/31`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/31-llm-backend.md) |
| `JudgeBackend` | ✅ Shipped | Pre-action validation; `PolicyJudge` (rules) + `LLMJudgeBackend` reference impls; ESCALATE + REVISE state machines | [`spec/28`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/28-judge-layer.md) |
| `LockBackend` | ✅ Shipped | Filesystem (`fcntl.flock`) + Redis reference impls; closes the multi-host cliff for Cloud Run / Kubernetes | [`spec/21`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/21-lock-backend.md) |
| `LogBackend` | ✅ Shipped | Filesystem (JSONL) + SQLite reference impls; indexed query/aggregate/retention; closes the dashboard-perf cliff | [`spec/22`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/22-log-backend.md) |
| `AgentProfileBackend` | ✅ Shipped | Filesystem + SQLite reference impls; JSON snapshot trio; closes the SaaS-shape cliff for DB-backed agent registries | [`spec/24`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/24-agent-profile-backend.md) |
| `ToolRegistryBackend` | ✅ Shipped | Filesystem + SQLite reference impls; hybrid metadata-in-SQL + handler-bodies-on-disk; install / uninstall capability | [`spec/25`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/25-tool-registry-backend.md) |
| `MandateBackend` | ✅ Shipped | Filesystem reference impl; `MandateCheck` specialist + reservation pattern + crash recovery; closes the durable-authorization cliff | [`spec/29`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/29-mandates.md) |
| `PolicyBackend` | ✅ Shipped | Filesystem reference impl (`policy.md` at project root); cost-cap MIN composition + tool / MCP / model surfaces enforced by default (PR 4 flag flip); unified `policy_decision` audit event family | [`spec/32`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/32-policy-backend.md) |
| `PersonaBackend` | ✅ Shipped | Filesystem reference impl at `<scope_root>/.personas/<persona_id>/`; `persona.link.md` ownership trigger; snapshot trio nested under each persona's directory; `atomic-agents persona` CLI; AgentProfile composition with migration-window restore event | [`spec/33`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/33-persona-backend.md) |
| `CorpusBackend` | ✅ Shipped | Filesystem + SQLite (FTS5) reference impls; per-agent `wiki/` + `raw/`; `render_index_summary(corpus)` Protocol method; closes the GB-scale wiki cliff via O(log N) indexed full-text query | [`spec/34`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/34-corpus-backend.md) |
| `MCPServerRegistryBackend` | ✅ Shipped | Filesystem + HTTP reference impls with tier-1/2/3 capability negotiation; install/uninstall write paths; `atomic-agents mcp-registry` CLI; HTTP scheme gate + spawn allowlist (GHSA-xhcr-cqfr-m3hv); closes the v1.0 Protocol surface | [`spec/36`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/36-mcp-server-registry-backend.md) |
| `SecretBackend` | ✅ Shipped (LOCKED, spec/38) | Credential resolution abstraction; `FilesystemSecretBackend` (env → Keychain → keys.json) + `GCPSecretManagerBackend`; `atomic-agents secrets` CLI | [`spec/38`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/38-secret-backend.md) |
| `GoalBackend` | ✅ Shipped (LOCKED spec/41) | Goal state persistence; `FilesystemGoalBackend` reference impl; write-path adoption (#448 PR1): `GoalManager` load/save/append_history routed through backend, `AtomicAgent.goal_backend` live-wired, archive data-loss fix; goal-outcome coordinator `dispatch_sub_goal_as_outcome()` (fail-closed cost gate, CAS guard, `apply_transition()` pre+terminal transitions, #448 PR3 arc-closer); clock injection (`when: date | None = None`) on `apply_transition()` + `archive_goal()`, `GoalManager.archive()` thin shim, `agent_root` resolved at init (#483 PR1 + spec/41 addendum); backend-universe alignment: coordinator threads gate agent's `log_backend`/`policy_backend`/`profile_backend` into `OutcomeRunner` (#496 PR1); `GoalExport` wired into `Exportable` (spec/40); the fourteenth backend Protocol | [`spec/41`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/41-goal-backend.md) |
| `OutcomeBackend` | ✅ Shipped (LOCKED spec/42) | Outcome state persistence; `FilesystemOutcomeBackend` reference impl; write-path adopted (#448 PR2) — `OutcomeRunner.run()` routes through `write_result`; byte-identical on-disk `result.json` for the default path + portable `export()` with relative artifact refs; `OutcomeExport` wired into `Exportable` (spec/40); write-once/result-immutability; the fifteenth backend Protocol | [`spec/42`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/42-outcome-backend.md) |
| `JournalBackend` | ✅ Shipped (DRAFT spec/43) | Journal persistence abstraction; `FilesystemJournalBackend` reference impl; ADOPT-NOW — all three hand-synced `rglob` read sites (bundle/agent/dream) wired through the backend; `append_entry` atomic write (fcntl.flock sidecar); `JournalExport` wired into `Exportable` (spec/40); the sixteenth backend Protocol | [`spec/43`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/43-journal-backend.md) |
| `QueueBackend` | ✅ Shipped (DRAFT spec/44) | Cascade work-queue protocol; `FilesystemQueueBackend` reference impl; POSIX-rename atomic claim; SCAFFOLDING-ONLY carve of `_cascade.py` queue cluster; `QueueExport` wired into `Exportable` (spec/40); closes TENSIONS T4 (single-host claim atomicity cliff); the seventeenth backend Protocol | [`spec/44`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/44-queue-backend.md) |
| `IdempotencyBackend` | ✅ Shipped (LOCKED spec/45) | At-most-once execution guarantee; `FilesystemDedupLedger` reference impl; `begin(key)->DedupDecision` atomic O_EXCL lease claim; `commit()` MARKER-ONLY terminal entry; `lookup()` read-only; `release_lease()` wedge-recovery; `IdempotencyExport` wired into `Exportable` (spec/40, terminal entries only, leases excluded); dual-probe doctor; PR 2 wired the two-phase dedup gate into `agent.call(idempotency_key=...)` (lookup-before-lock + begin-after-cost-gate, serve/queue/cron triggers, RunRecord audit fields, spec/22 addendum); the eighteenth backend Protocol | [`spec/45`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/45-idempotency-backend.md) |
| `EmbeddingBackend` | 🟡 Shipped (DRAFT spec/46, #200 PR 2 of 3 + #544 PR1 + #544 PR2a) | Pluggable embedding generation for semantic recall; `OpenAIEmbeddingBackend` reference impl; `embed()`/`embed_batch()` MUST-NOT-RAISE (None-fallback) + `len(out)==len(in)` invariant; `EMBEDDING_PRICING` + `calc_embedding_cost()` isolated from chat `PRICING`; dimension honesty enforced at construction (refuse non-native / above-native) AND at the produced vector (`len==dimensions` or None); key resolution delegates to the SecretBackend (no private cascade); `supports_input_type` flag advertised, `input_type` param deferred to #544 PR2; registry + pgvector wiring shipped PR3 (#200); batch embed cost gate at `agent.call()` capture-commit site + `PRIMITIVE_EMBED` + `check_embedding_backend()` doctor check + spec/20/22/34 normative addenda shipped #544 PR1; #544 PR2a: dedicated `embed_cost` JSONL record (`cost_usd=actual_usd` visible to `sum_cost_for_period` across calls) + merge-write pre-read reservation (sized from preserved target body, Principle #4) + spec/22 versioned normative addendum; query-embed gate (#564) + LOCK ceremony deferred to #544 PR2; the nineteenth backend Protocol | [`spec/46`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/46-embedding-backend.md) |
| `ConversationBackend` | 🟡 Shipped (DRAFT spec/47, #535 PR 1) | Multi-turn conversation persistence; `FilesystemConversationBackend` reference impl; `Principal` typed identity key + `LOCAL_PRINCIPAL` home-user default; `load_turns()` budget-bounded oldest-first eviction + `write_turn()` atomic per-principal flock write; two-part cross-principal isolation Guard (2): resolved-basename comparison + inode-identity check (closes APFS case-fold / NFD aliasing); `agent.call(conversation_id=...)` three-channel wiring with prior-turn injection into `messages[]` (normalized to avoid provider 400s); SQLite + Postgres v2→v3 migration adds `conversation_id` column + `LogQuery.conversation_id` predicate; the twentieth backend Protocol | [`spec/47`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/47-conversation-backend.md) |
| `PrincipalBackend` | 🟡 Shipped (DRAFT spec/48, #556 PR 1) | Identity-derivation abstraction; maps already-verified caller claims to a `Principal` (the perimeter verifies tokens — never this Protocol); `LocalPrincipalBackend` (home-user zero-config, `is_local_only=True`) + `StaticClaimsPrincipalBackend` (sha256 NUL-separator storage key, prefix-collision-safe) reference impls; HARD-REFUSE gate in `agent.call()` placed BEFORE idempotency dedup lookup (closes cross-principal cached-run replay via a guessed `idempotency_key`); serve HYBRID flow fail-closed opt-in (`identity_is_perimeter_verified` default False); doctor wired into `run_doctor()` with fail-closed negative probe; 12-MUST Implementer Contract; the twenty-first backend Protocol | [`spec/48`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/48-principal-backend.md) |

**v1 direction:** a home user runs filesystem-everything today. An organization runs the same agent definitions over Postgres / Redis / SQLite-Datadog / behind an HTTP service. v1.0 is here: all 12 backend protocols shipped and locked, conformance suites pin the contracts. A thirteenth, `SecretBackend` (#340), shipped for v1.5 (both PRs merged, spec/38 LOCKED, Filesystem + GCP Secret Manager reference impls). A fourteenth, `GoalBackend` (#425 + #448 PR1/PR2/PR3 + #483 PR1 + #496 PR1), shipped for v1.5 (LOCKED spec/41, `FilesystemGoalBackend` reference impl, write-path adoption: `GoalManager` load/save/history routed through backend, `AtomicAgent.goal_backend` live-wired, archive data-loss fix; goal-outcome coordinator `dispatch_sub_goal_as_outcome()` with fail-closed cost gate + CAS guard — arc-closer #448 PR3; clock injection + `GoalManager.archive()` thin shim + `agent_root` resolved at init — cleanup #483 PR1; backend-universe alignment: coordinator threads gate agent's `log_backend`/`policy_backend`/`profile_backend` into `OutcomeRunner` — #496 PR1). A fifteenth, `OutcomeBackend` (#426 + #448 PR2), shipped for v1.5 (LOCKED spec/42, `FilesystemOutcomeBackend` reference impl, write-path adopted: `OutcomeRunner.run()` routes through the backend, custom-`output_dir` `result.json` relocated to the canonical path). A sixteenth, `JournalBackend` (#427), shipped for v1.5 PR 1 (DRAFT spec/43, `FilesystemJournalBackend` reference impl, ADOPT-NOW). A seventeenth, `QueueBackend` (#428), shipped for v1.5 PR 1 (DRAFT spec/44, `FilesystemQueueBackend` reference impl, SCAFFOLDING-ONLY carve, closes TENSIONS T4). An eighteenth, `IdempotencyBackend` (#520), shipped for v1.5 (PR 1 + PR 2 arc-closer) (LOCKED spec/45, `FilesystemDedupLedger` reference impl — `begin()`/`commit()`/`lookup()`/`release_lease()` Protocol + O_EXCL atomic lease claim + MARKER-ONLY terminal entry; PR 2 wired the two-phase dedup gate into `agent.call(idempotency_key=...)` with serve/queue/cron triggers, RunRecord audit fields, and the spec/22 versioned normative addendum). A nineteenth, `EmbeddingBackend` (#200 PR 2 of 3 + #544 PR1 + #544 PR2a), shipped for v1.5 (DRAFT spec/46, `OpenAIEmbeddingBackend` reference impl — `embed()`/`embed_batch()` MUST-NOT-RAISE + `len(out)==len(in)` invariant; `EMBEDDING_PRICING` isolated from chat `PRICING`; dimension honesty enforced at construction and at the produced vector; key resolution via the SecretBackend; registry + pgvector wiring shipped PR3 (#200); #544 PR1: batch embed cost gate at `agent.call()` capture-commit site + `PRIMITIVE_EMBED` + `check_embedding_backend()` doctor check + spec/20/22/34 normative addenda; #544 PR2a: dedicated `embed_cost` JSONL record (`cost_usd=actual_usd` visible to `sum_cost_for_period` across calls) + merge-write pre-read reservation (sized from preserved target body, Principle #4) + spec/22 versioned normative addendum; query-embed gate (#564) + `input_type` param + LOCK ceremony deferred to #544 PR2). A twentieth, `ConversationBackend` (#535 PR 1), shipped for v1.5 (DRAFT spec/47, `FilesystemConversationBackend` reference impl — multi-turn persistence; `Principal` typed identity key + `LOCAL_PRINCIPAL` home-user default; two-part cross-principal Guard (2); `agent.call(conversation_id=...)` wiring with prior-turn injection into `messages[]`; SQLite + Postgres v2→v3 migration; 138 tests). A twenty-first, `PrincipalBackend` (#556 PR 1), shipped for v1.5 (DRAFT spec/48, `LocalPrincipalBackend` + `StaticClaimsPrincipalBackend` reference impls — identity-derivation abstraction; HARD-REFUSE gate in `agent.call()` placed BEFORE idempotency dedup lookup; serve HYBRID flow fail-closed opt-in; doctor wired into `run_doctor()`; 125 new tests; closes the multi-user identity cliff). See [`docs/architecture.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/architecture.md) for the mental model, [`docs/TENSIONS.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/TENSIONS.md) for architectural tensions this scaling story has to survive, and [`ROADMAP.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/ROADMAP.md) for the full backlog beyond v1.0.

---

## Judge layer (opt-in)

The **judge layer** is a pre-action validation surface. Before any side-effectful tool call executes, a separate `JudgeBackend` inspects a structured action proposal and returns ALLOW / BLOCK / REVISE / ESCALATE. Every judgment writes a JSONL audit event carrying the proposal hashes, the outcome, the policy version, and the judge's reason. ESCALATE pauses execution and writes a PENDING file to `<agent_root>/vault/escalations/` that an operator resolves by editing in any text editor. REVISE supports both judge-driven amendments (e.g., *"send this email but strip the attachment"*) and operator-driven amendments via an embedded `amendment:` YAML block on the PENDING file.

The layer is **fully opt-in**. Existing deployments see no judge invocation until they drop a `judges.md` file in the agent root (or set `AGENT_JUDGE_ENABLED=1`). The default `failure_policy` is fail-closed (`block` for every exception type); cascade-aware project floors enforce a non-relaxable minimum across delegates per spec/28 §408.

- [`docs/deployment/judges-md.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/judges-md.md): operator runbook: every `judges.md` field, every error message, examples
- [`docs/spec/28-judge-layer.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/spec/28-judge-layer.md): full spec: ESCALATE + REVISE state machines, audit-event schema, conformance suite reference

---

## Deployment shapes

For a full cloud reference deployment, see [`extras/gcp/`](https://github.com/dep0we/atomic-agents-stack/tree/main/extras/gcp/): two reference topologies — a stateless Cloud Run service (Cloud Run v2 does not support persistent disk; ephemeral state until managed backends ship) and a Compute Engine VM with a persistent ext4/xfs disk (stateful-today bridge). Both use IAP + Cloud Scheduler + Redis locks + Cloud Monitoring.

Nine operator runbooks under `docs/deployment/` cover the common deployment paths. Pick the one that matches what you're doing:

- [`docs/deployment/obsidian.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/obsidian.md): running the framework against an Obsidian-synced vault: ignore patterns, `.versions/` trade-offs, sync race conditions, conflict copy recovery
- [`docs/deployment/programmatic.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/programmatic.md): embedding in Python: the `Agent` + `call()` public surface, the complete public exception table, three worked examples
- [`docs/deployment/disaster-recovery.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/disaster-recovery.md): symptom-organized runbook: stale locks, mid-run crashes, corrupted INDEX, migration rollback, memory write races
- [`docs/deployment/cost-guardrail-sizing.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/cost-guardrail-sizing.md): picking daily/monthly caps + cap action; seven role archetypes with recommended starting values
- [`docs/deployment/judges-md.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/judges-md.md): authoring `judges.md` to configure the judge layer: class policy, cascade-aware project floor, `failure_policy` shapes
- [`docs/deployment/serve.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/serve.md): deploying agents over HTTP with `atomic-agents serve`: Cloud Run / GKE / Fly.io / Render, identity-header pass-through, no-auth default, healthz/doctor probes
- [`docs/deployment/versioning.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/versioning.md): SemVer policy; what counts as Major / Minor / Patch
- [`docs/deployment/upgrading.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/upgrading.md): operator upgrade runbook + migration runner usage
- [`docs/deployment/release-runbook.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/release-runbook.md): maintainer-facing `/ship` runbook: two-mode workflow (PR-level vs. release cut), local gstack patch, operator manual surface check

---

## What's shipped

The backend protocols table above covers the load-bearing capabilities. For per-version detail across every shipped runtime feature, CLI command, deployment runbook, and spec doc, see [CHANGELOG.md](https://github.com/dep0we/atomic-agents-stack/blob/main/CHANGELOG.md).

---

## Versioning & upgrades

`atomic-agents-stack` follows [SemVer](https://semver.org) with project-specific rules for what counts as a Major / Minor / Patch change. **Pre-1.0, Minor releases may contain breaking changes** — always read the release notes before upgrading.

- [`docs/deployment/versioning.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/versioning.md): full SemVer policy
- [`docs/deployment/upgrading.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/upgrading.md): operator upgrade runbook

Every release lands as a `vX.Y.Z` git tag plus a GitHub Release with the CHANGELOG entry verbatim. Breaking changes get a `### BREAKING` callout in that entry.

---

## Configuration

### `ATOMIC_AGENTS_ROOT`

Tells the framework where to find your agent vault. **Default: `~/docs/agents`** (suitable for Obsidian-backed deployments; see [`docs/deployment/obsidian.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/obsidian.md)).

```bash
export ATOMIC_AGENTS_ROOT=/path/to/your/agents
```

### API keys

The framework looks for keys in this order:

1. **Environment variables** — `ATOMIC_AGENTS_ANTHROPIC_KEY`, `ANTHROPIC_API_KEY`
2. **macOS Keychain** — `security add-generic-password -a $USER -s atomic-agents-anthropic -w sk-ant-...`
3. **`~/.config/atomic_agents/keys.json`** (chmod 600):
   ```json
   {"anthropic": "sk-ant-...", "openai": "sk-...", "moonshot": "..."}
   ```

Same pattern for OpenAI (`atomic-agents-openai`) and Moonshot (`atomic-agents-moonshot`). Run `uv run atomic-agents doctor` to verify which lookup chain found your keys.

---

## Repository structure

- `atomic_agents/` — the Python package (runtime in `agent.py`; backend protocols in `memory/`, `_llm.py`, `_locks.py`, `_costs.py`, etc.; CLI in `cli.py`; preflight in `doctor.py`)
- `tests/` 5,100+ tests collected, Python 3.11 + 3.12 matrix
- `docs/`: [spec entry point](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/README.md), [`architecture.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/architecture.md), [`spec/`](https://github.com/dep0we/atomic-agents-stack/tree/main/docs/spec/) (37 locked docs + 8 DRAFTs/RFCs), [`deployment/`](https://github.com/dep0we/atomic-agents-stack/tree/main/docs/deployment/) (9 operator runbooks), [`samples/caldwell/`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/samples/caldwell/) (complete worked example), [`GOVERNANCE.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/GOVERNANCE.md), [`TENSIONS.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/TENSIONS.md), [`methodology.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/methodology.md)
- `extras/` — operational templates (Claude Code skill wrappers, macOS LaunchAgent plists, cron examples, GCP Cloud Run + IAP reference deployment)

---

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Run the full test suite
uv run pytest

# Run a specific test module
uv run pytest tests/test_capture.py -v
```

Before opening a PR, read [`CLAUDE.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/CLAUDE.md) (the project's design ethos and 14 taste rules), [`docs/TENSIONS.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/TENSIONS.md) (architectural tensions to protect when changing code), and [`docs/methodology.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/methodology.md) (the practices that produced this codebase's quality). See [`CONTRIBUTING.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/CONTRIBUTING.md) for the contribution flow.

---

## License

[MIT](https://github.com/dep0we/atomic-agents-stack/blob/main/LICENSE).

---

## Status

**v1.1.0, stable.** Core runtime stable. Twelve of twelve backend protocols shipped and locked at v1.0 (see the backend protocols table above); `MCPServerRegistryBackend` LOCKED at PR 5 v1.0. A thirteenth, `SecretBackend` (#340), shipped for v1.5 (both PRs merged, spec/38 LOCKED, Filesystem + GCP Secret Manager reference impls). A fourteenth, `GoalBackend` (#425 + #448 PR1/PR2/PR3 + #483 PR1 + #496 PR1), shipped for v1.5 (LOCKED spec/41, `FilesystemGoalBackend` reference impl, write-path adoption: `GoalManager` load/save/history routed through backend, `AtomicAgent.goal_backend` live-wired, archive data-loss fix; goal-outcome coordinator `dispatch_sub_goal_as_outcome()` with fail-closed cost gate + CAS guard — arc-closer #448 PR3; clock injection + `GoalManager.archive()` thin shim + `agent_root` resolved at init — cleanup #483 PR1; backend-universe alignment: coordinator threads gate agent's `log_backend`/`policy_backend`/`profile_backend` into `OutcomeRunner` — #496 PR1). A fifteenth, `OutcomeBackend` (#426 + #448 PR2), shipped for v1.5 (LOCKED spec/42, `FilesystemOutcomeBackend` reference impl, write-path adopted: `OutcomeRunner.run()` routes through the backend, custom-`output_dir` `result.json` relocated to the canonical path). A sixteenth, `JournalBackend` (#427), shipped for v1.5 PR 1 (DRAFT spec/43, `FilesystemJournalBackend` reference impl, ADOPT-NOW wiring). A seventeenth, `QueueBackend` (#428), shipped for v1.5 PR 1 (DRAFT spec/44, `FilesystemQueueBackend` reference impl, SCAFFOLDING-ONLY carve from `_cascade.py`, closes TENSIONS T4). An eighteenth, `IdempotencyBackend` (#520), shipped for v1.5 (PR 1 + PR 2 arc-closer) (LOCKED spec/45, `FilesystemDedupLedger` reference impl — `begin()`/`commit()`/`lookup()`/`release_lease()` Protocol + O_EXCL atomic lease claim + MARKER-ONLY terminal entry; PR 2 wired the two-phase dedup gate into `agent.call(idempotency_key=...)` with serve/queue/cron triggers, RunRecord audit fields, and the spec/22 versioned normative addendum). A nineteenth, `EmbeddingBackend` (#200 PR 2 of 3 + #544 PR1 + #544 PR2a), shipped for v1.5 (DRAFT spec/46, `OpenAIEmbeddingBackend` reference impl — pluggable embedding generation for semantic recall; MUST-NOT-RAISE None-fallback + `len(out)==len(in)`; `EMBEDDING_PRICING` isolated from chat `PRICING`; dimension honesty at construction + produced vector; SecretBackend key resolution; registry + pgvector wiring shipped PR3 (#200); #544 PR1: batch embed cost gate at `agent.call()` capture-commit site + `PRIMITIVE_EMBED` + `check_embedding_backend()` doctor check + spec/20/22/34 normative addenda; #544 PR2a: dedicated `embed_cost` JSONL record (`cost_usd=actual_usd` visible to `sum_cost_for_period` across calls) + merge-write pre-read reservation (sized from preserved target body, Principle #4) + spec/22 versioned normative addendum; query-embed gate (#564) + `input_type` param + LOCK ceremony deferred to #544 PR2). A twentieth, `ConversationBackend` (#535 PR 1), shipped for v1.5 (DRAFT spec/47, `FilesystemConversationBackend` reference impl; `Principal` typed identity key + `LOCAL_PRINCIPAL` home-user default; two-part cross-principal Guard (2); `agent.call(conversation_id=...)` wiring; SQLite + Postgres v2→v3 migration; 138 conversation tests). A twenty-first, `PrincipalBackend` (#556 PR 1), shipped for v1.5 (DRAFT spec/48, `LocalPrincipalBackend` + `StaticClaimsPrincipalBackend` reference impls; HARD-REFUSE gate placed BEFORE idempotency dedup lookup; serve HYBRID flow fail-closed opt-in; doctor wired into `run_doctor()`; 125 new tests). The v1.0 Protocol surface is stable per SemVer Major. See [`docs/deployment/versioning.md`](https://github.com/dep0we/atomic-agents-stack/blob/main/docs/deployment/versioning.md) for the breaking-change policy. Single-maintainer project; reference implementation anyone can use, fork, or extend.

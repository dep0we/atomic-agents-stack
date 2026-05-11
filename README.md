# atomic-agents-stack

[![Tests](https://github.com/dep0we/atomic-agents-stack/actions/workflows/test.yml/badge.svg)](https://github.com/dep0we/atomic-agents-stack/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.11.0-orange)](CHANGELOG.md)

> **AI agents that live in your folder, not someone else's database.**

Vault-native, MIT-licensed, runs anywhere markdown does.

---

## Why this exists

Most AI-agent tooling today lives in someone else's database. Letta hosts your memory. Mem0 hosts your memory. Hosted-agent frameworks like OpenClaw run the harness on their hardware. You get the ergonomics, they get the data and the lock-in.

There's another shape: **your agents live in your folder.** Plain markdown files. INDEX.md routing. Persona in `IDENTITY.md` / `SOUL.md` / `USER.md`. Memory as typed atomic notes you can `cat`. Audit trail as JSONL you can grep. Cost guardrails in markdown config. Migrations are scripts you can read. If you stop paying anyone, the agent still works. If you switch laptops, you copy a folder. If you want a new runtime — cron, Claude Code skill, ChatGPT skill, your own HTTP service — you point the runtime at the folder.

That's the shape `atomic-agents-stack` defines, in 21 locked spec docs, with a Python reference implementation, 720+ tests, and a complete worked sample.

A home user with one agent and an org with a fleet experience the same framework — graceful, coherent, self-explanatory at every scale.

---

## Quick start

```bash
# Install
git clone https://github.com/dep0we/atomic-agents-stack.git
cd atomic-agents-stack
uv sync

# Configure your vault location (default: ~/docs/agents)
export ATOMIC_AGENTS_ROOT=~/agents

# Verify everything's wired up
uv run atomic-agents doctor

# Run an agent (assuming you've created one — see "Your first agent" below)
uv run atomic-agents run myagent --work-item "What should I focus on today?"

# See the cost dashboard
uv run python -m atomic_agents.dashboard render
open ~/agents/_dashboard/index.html
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

See [`docs/getting-started.md`](docs/getting-started.md) for the 15-minute clone-to-running-agent walk-through and [`docs/deployment/programmatic.md`](docs/deployment/programmatic.md) for the complete programmatic API + public exception table.

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

For a complete worked example with real persona, memory, journal, evals, and a sample dashboard rendered from real log data, see [`docs/samples/caldwell/`](docs/samples/caldwell/).

---

## How it compares to alternatives

The framework occupies a specific slot in the AI-agent-tooling landscape. Honest comparison:

| | Atomic Agents | Letta / Mem0 | Hosted-agent harnesses<br>(OpenClaw-shaped) | LangChain / LangGraph | Direct SDK + your scripts |
|---|---|---|---|---|---|
| **Vault portability** | Plain folders, copy anywhere | Hosted database | Hosted runtime | Whatever you build | Whatever you build |
| **License** | MIT | Mostly hosted-service ToS | Vendor-specific | MIT (LangChain), MIT (LangGraph) | Whatever |
| **Memory layer** | Typed atomic notes + INDEX + wiki, all markdown | Their primary feature | Their memory service | Vector store you wire in | Build it |
| **Persona layer** | IDENTITY/SOUL/USER spec | None — strings in prompt | Vendor-shaped | Strings in prompts | Strings in prompts |
| **Audit trail** | JSONL with `parent_run_id` rollups | Vendor dashboards | Vendor dashboards | None built in | Build it |
| **Cost guardrails** | First-class — daily / monthly caps, fallback, alert, critical-flag override | Per their pricing | Per their pricing | None built in | Build it |
| **Multi-agent coordination** | Spec-defined role × project cascade | None | Vendor-shaped | LangGraph: yes (graph-based) | Build it |
| **Self-hosting** | Yes (it's just a folder + Python) | Hosted-only or their server | Their server | Yes | Yes |
| **Spec-defined** | 21 numbered spec docs | No public spec | No public spec | API reference | None |
| **The bet** | Operators want to own their agents | Operators want managed memory | Operators want zero ops | Operators want flexibility | Operators want full control |

**Where the alternatives win:** Letta has the polished hosted-service UX. LangGraph has more flexible graph-based orchestration. Hosted harnesses give you zero-ops. If those properties matter more than ownership, pick those tools.

**Where Atomic Agents wins:** the framework feels like one thing across scales. The same agent definitions run as a cron job on your laptop, a Claude Code skill, a Codex CLI skill, a ChatGPT skill, or an HTTP service. Different backends register at import time. The audit trail is structural, not bolted on. The cost story is first-class. Your data stays where you put it.

---

## The spec is the product

`atomic-agents-stack` is a **spec** for vault-native AI agents, plus one **reference implementation** in Python. The spec is the central artifact; anyone can build agents to the spec without using this code.

Start at [`docs/README.md`](docs/README.md) for the spec entry point. The 21 spec docs in [`docs/spec/`](docs/spec/) cover:

- [01 — Anatomy](docs/spec/01-anatomy.md) — file layout, persona, memory, wiki, journal, log
- [02 — Atomic Memory](docs/spec/02-atomic-memory.md) — Notes + Wiki + INDEX-driven recall
- [03 — File formats](docs/spec/03-file-formats.md) — frontmatter schemas + filename conventions
- [04 — Runtime assembly](docs/spec/04-runtime-assembly.md) — canonical load sequence
- [05 — Capture rules](docs/spec/05-capture-rules.md) — when and how agents write to memory
- [06 — Multi-agent projects](docs/spec/06-multi-agent-projects.md) — role × project cascade
- [07 — Research foundations](docs/spec/07-research-foundations.md) — lineage and prior art
- [08 — Evaluation](docs/spec/08-evaluation.md) — rubrics + LLM-as-judge framework
- [09 — Cost & observability](docs/spec/09-cost-observability.md) — pricing, dashboard, guardrails
- [10 — Helpers](docs/spec/10-helpers.md) — cheap-LLM workers for transformation subtasks
- [11 — Tuning](docs/spec/11-tuning.md) — eval-driven self-improvement
- [12 — Goals & intent](docs/spec/12-goals-and-intent.md) — goal-driven agents
- [13 — Research integrity](docs/spec/13-research-integrity.md) — citations + factual accuracy
- [14-19](docs/spec/) — capture markers, delegation, dreams, skills, MCP, alternative-runtime contracts
- [20 — Memory backend protocol](docs/spec/20-memory-backend.md) — the protocol-pattern moat
- [27 — Doctor](docs/spec/27-doctor.md) — preflight verification

Each spec doc is locked when the implementation matches and tests pass. Spec changes that imply implementation changes get filed as GitHub issues. **Spec docs are not aspirational; they describe what's true today.**

---

## Backend protocols — the scaling story

The framework is moving toward swappable backends layer by layer. The shape: a Python `Protocol` for each primitive that touches storage, a filesystem-default implementation, capability advertisement, and a conformance test suite. Same agent definitions, same `call()` flow, same audit trail — different backends registered.

| Backend | Status | Spec |
|---|---|---|
| `MemoryBackend` | ✅ Shipped (v0.10.0) | [`spec/20-memory-backend.md`](docs/spec/20-memory-backend.md) |
| `LockBackend` | Planned | [`#60`](https://github.com/dep0we/atomic-agents-stack/issues/60) |
| `LogBackend` | Planned | [`#61`](https://github.com/dep0we/atomic-agents-stack/issues/61) |
| `PersonaBackend` | Planned | [`#62`](https://github.com/dep0we/atomic-agents-stack/issues/62) |
| `AgentProfileBackend` | Planned | [`#63`](https://github.com/dep0we/atomic-agents-stack/issues/63) |
| `ToolRegistryBackend` | Planned | [`#64`](https://github.com/dep0we/atomic-agents-stack/issues/64) |
| `CorpusBackend` | Planned | [`#65`](https://github.com/dep0we/atomic-agents-stack/issues/65) |
| `LLMBackend` | Planned | [`#87`](https://github.com/dep0we/atomic-agents-stack/issues/87) |
| `PolicyBackend` | Planned | [`#89`](https://github.com/dep0we/atomic-agents-stack/issues/89) |

A home user runs filesystem-everything. An organization runs the same agents over Postgres, behind an HTTP service, with a fleet of orchestrated roles. **Same agents. Different backends.** See [`docs/architecture.md`](docs/architecture.md) for the mental model.

---

## Deployment shapes

Six operator runbooks for the common deployment paths. Pick the one that matches what you're doing:

- [`docs/deployment/obsidian.md`](docs/deployment/obsidian.md) — running the framework against an Obsidian-synced vault: ignore patterns, `.versions/` trade-offs, sync race conditions, conflict copy recovery
- [`docs/deployment/programmatic.md`](docs/deployment/programmatic.md) — embedding in Python: the `Agent` + `call()` public surface, the complete public exception table, three worked examples
- [`docs/deployment/disaster-recovery.md`](docs/deployment/disaster-recovery.md) — symptom-organized runbook: stale locks, mid-run crashes, corrupted INDEX, migration rollback, memory write races
- [`docs/deployment/cost-guardrail-sizing.md`](docs/deployment/cost-guardrail-sizing.md) — picking daily/monthly caps + cap action; seven role archetypes with recommended starting values
- [`docs/deployment/versioning.md`](docs/deployment/versioning.md) — SemVer policy; what counts as Major / Minor / Patch
- [`docs/deployment/upgrading.md`](docs/deployment/upgrading.md) — operator upgrade runbook + migration runner usage

---

## What's shipped

| Component | Shipped |
|---|---|
| `AtomicAgent` runtime | ✅ v0.1.0 |
| Persona loading (IDENTITY, SOUL, USER) | ✅ v0.1.0 |
| `memory/` + `wiki/` INDEX-driven recall | ✅ v0.1.0 |
| Helper-mediated atomic captures (fenced JSON) | ✅ v0.1.0 |
| Multi-tier cost guardrails (50% / 80% / 100%) | ✅ v0.1.0 |
| Helper calls — sequential + parallel | ✅ v0.1.0 |
| Anthropic / OpenAI / Moonshot Kimi routing | ✅ v0.1.0 |
| File locking with stale-lock recovery | ✅ v0.1.0 |
| Schema validation incl. date-suffix filenames | ✅ v0.1.0 |
| Cost dashboard (HTML, global + per-agent) | ✅ v0.1.0 |
| Optional local dashboard server | ✅ v0.1.0 |
| Eval runner — `atomic_agents.eval` | ✅ v0.9.0 |
| Tuning analyzer — `atomic_agents.tuning` | ✅ v0.9.0 |
| Goal manager — `atomic_agents.goal` | ✅ v0.9.0 |
| Schema migration runner — `atomic_agents.migrate` | ✅ v0.9.0 |
| Tool-call captures (Path 1) | ✅ v0.9.0 |
| Multi-agent project cascade loader — `atomic_agents._cascade` | ✅ v0.9.0 |
| Helper provenance preservation | ✅ v0.9.0 |
| Research integrity layers 2 + 3 | ✅ v0.9.0 |
| Claude Code skill wrappers — `extras/claude-code-skills/` | ✅ v0.9.0 |
| Spec docs in repo — `docs/` | ✅ v0.9.0 |
| CI (Python 3.11 + 3.12 matrix) | ✅ v0.9.0 |
| MCP (Model Context Protocol) client — `atomic_agents.mcp` | ✅ v0.10.0 |
| MemoryBackend protocol + FilesystemBackend default — `atomic_agents.memory` | ✅ v0.10.0 |
| `atomic-agents doctor` preflight CLI — `atomic_agents.doctor` | ✅ v0.10.0 |
| SemVer policy + upgrade runbook — `docs/deployment/` | ✅ v0.10.0 |
| Obsidian-backed deployment guide — `docs/deployment/obsidian.md` | ✅ v0.11.0 |
| Programmatic invocation guide + public exception table — `docs/deployment/programmatic.md` | ✅ v0.11.0 |
| Disaster recovery runbook — `docs/deployment/disaster-recovery.md` | ✅ v0.11.0 |
| Cost guardrail sizing guidance — `docs/deployment/cost-guardrail-sizing.md` | ✅ v0.11.0 |

See [CHANGELOG.md](CHANGELOG.md) for per-version detail.

---

## Versioning & upgrades

`atomic-agents-stack` follows [SemVer](https://semver.org) with project-specific rules for what counts as a Major / Minor / Patch change. **Pre-1.0, Minor releases may contain breaking changes** — always read the release notes before upgrading.

- [`docs/deployment/versioning.md`](docs/deployment/versioning.md) — full SemVer policy
- [`docs/deployment/upgrading.md`](docs/deployment/upgrading.md) — operator upgrade runbook

Every release lands as a `vX.Y.Z` git tag plus a GitHub Release with the CHANGELOG entry verbatim. Breaking changes get a `### BREAKING` callout in that entry.

---

## Configuration

### `ATOMIC_AGENTS_ROOT`

Tells the framework where to find your agent vault. **Default: `~/docs/agents`** (suitable for Obsidian-backed deployments; see [`docs/deployment/obsidian.md`](docs/deployment/obsidian.md)).

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

```
atomic_agents/                  # the Python package
├── agent.py                    # AtomicAgent class — the main runtime
├── exceptions.py               # 27 public exception classes
├── types.py                    # shared dataclasses
├── cli.py                      # `atomic-agents` console script
├── doctor.py                   # preflight verification
├── migrate.py                  # schema migration runner
├── memory/                     # MemoryBackend protocol + filesystem default
├── dashboard/                  # cost & observability dashboard
├── mcp.py                      # MCP client (stdio transport)
├── _llm.py                     # provider routing (Anthropic / OpenAI / Moonshot)
├── _costs.py                   # pricing + multi-tier guardrails
├── _locks.py                   # per-agent flock with stale-lock recovery
└── _io.py                      # atomic file writes (temp + fsync + rename)

tests/                          # 720+ tests, all passing on Python 3.11 + 3.12

docs/
├── README.md                   # spec entry point
├── architecture.md             # mental model + design rationale
├── getting-started.md          # 15-minute clone-to-running-agent walk-through
├── spec/                       # 21 locked spec docs
├── implementation/             # build guides per runtime
├── deployment/                 # 6 operator runbooks
├── samples/caldwell/           # complete worked single-agent example
├── appendix/portability.md     # using Atomic Agents without Obsidian / on any OS
├── GOVERNANCE.md               # solo / small-team operator guide
├── TENSIONS.md                 # architectural tensions to protect
└── methodology.md              # working-methods retrospective

extras/                         # operational templates
├── claude-code-skills/         # SKILL.md wrappers for Claude Code
├── launchd/                    # macOS LaunchAgent .plist templates
└── cron/                       # crontab examples + portable wrapper script
```

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

Before opening a PR, read [`CLAUDE.md`](CLAUDE.md) (the project's design ethos and 14 taste rules), [`docs/TENSIONS.md`](docs/TENSIONS.md) (architectural tensions to protect when changing code), and [`docs/methodology.md`](docs/methodology.md) (the practices that produced this codebase's quality). See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution flow.

---

## License

[MIT](LICENSE).

---

## Status

**v0.11.0, alpha.** Core runtime stable. 720+ tests passing on Python 3.11 / 3.12. Pre-1.0 — Minor releases may contain breaking changes (see [`docs/deployment/versioning.md`](docs/deployment/versioning.md)). Single-maintainer project; reference implementation that anyone can use, fork, or extend. The protocol-pattern roadmap (`LockBackend` / `LogBackend` / `PersonaBackend` / etc.) is what v1.0 closes; the surface stabilizes there.

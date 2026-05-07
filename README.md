# atomic-agents-stack

[![Tests](https://github.com/user/atomic-agents-stack/actions/workflows/test.yml/badge.svg)](https://github.com/user/atomic-agents-stack/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Spec + reference implementation for **vault-native, multi-runtime AI agents**.

Atomic Agents is a layered system for building AI agents that:
- Live as plain markdown files in any folder structure (the "vault")
- Run across multiple runtimes (cron jobs, Claude Code skills, Codex CLI, ChatGPT, OpenAI API)
- Have structured persona (IDENTITY/SOUL/USER) + typed atomic memory + journal + audit log
- Use cheaper LLMs in parallel for transformation subtasks (helpers)
- Track cost, evaluate quality, and improve over time
- Pursue persistent goals across many sessions when needed

The spec is the central artifact. This repo is the reference Python implementation. Anyone can build agents to the spec without using this code.

---

## Quick start

> **First time?** Read the **[Getting Started guide](docs/getting-started.md)** for a 15-minute walk-through from clone to running agent. The block below is the abbreviated version.

```bash
# Install (development)
git clone https://github.com/dep0we/atomic-agents-stack.git
cd atomic-agents-stack
uv sync

# Run an agent (example — assumes you've created one)
export ATOMIC_AGENTS_ROOT=~/agents
atomic-agents info myagent
atomic-agents run myagent --work-item "What should I focus on today?"

# Render the cost dashboard
python -m atomic_agents.dashboard render
open ~/agents/_dashboard/index.html

# Serve the dashboard with a Refresh button
python -m atomic_agents.dashboard serve
```

```python
# Programmatic use
from atomic_agents import AtomicAgent

agent = AtomicAgent(name="myagent", trigger="cron")
response = agent.call(work_item="Daily morning brief")
print(response.text)
print(f"Cost: ${response.cost_usd:.4f}")
print(f"Captures: {len(response.captures)}")
```

---

## What's shipped

| Component | Status |
|---|---|
| `AtomicAgent` runtime | ✅ v0.1 |
| Persona loading (IDENTITY, SOUL, USER) | ✅ v0.1 |
| `memory/` + `wiki/` INDEX-driven recall | ✅ v0.1 |
| Helper-mediated atomic captures (fenced JSON) | ✅ v0.1 |
| Multi-tier cost guardrails (50%/80%/100%) | ✅ v0.1 |
| Helper calls — sequential + parallel | ✅ v0.1 |
| Anthropic / OpenAI / Moonshot Kimi routing | ✅ v0.1 |
| File locking with stale-lock recovery | ✅ v0.1 |
| Schema validation incl. date-suffix filenames | ✅ v0.1 |
| Cost dashboard (HTML, global + per-agent) | ✅ v0.1 |
| Optional local dashboard server | ✅ v0.1 |
| Eval runner — `atomic_agents.eval` | ✅ v0.2 |
| Tuning analyzer — `atomic_agents.tuning` | ✅ v0.3 |
| Goal manager — `atomic_agents.goal` | ✅ v0.4 |
| Schema migration runner — `atomic_agents.migrate` | ✅ v0.5 |
| Tool-call captures (Path 1) | ✅ v0.6 |
| Multi-agent project cascade loader — `atomic_agents._cascade` | ✅ v0.7 |
| Helper provenance preservation | ✅ v0.8 |
| Research integrity layers 2+3 | ✅ v0.9 |
| Claude Code skill wrappers — `extras/claude-code-skills/` | ✅ v0.9 |
| Spec docs in repo — `docs/` | ✅ v0.9 |
| CI (Python 3.11 + 3.12 matrix) | ✅ v0.9 |

The spec is feature-complete relative to the v1 lock. See [CHANGELOG.md](CHANGELOG.md) for per-version detail.

---

## How it works at runtime

When an agent is invoked, the framework:

1. Loads its persona files (`persona/IDENTITY.md`, `SOUL.md`, `USER.md`)
2. Loads its `tools.md` and `model.md` config
3. Loads `memory/INDEX.md` and `wiki/INDEX.md` (always-loaded routing layers)
4. Loads pinned atomic notes + the N most-recently-captured notes
5. Loads the most recent journal entry
6. Assembles all of this into the system prompt in the canonical order
7. Calls the LLM via the configured provider
8. Extracts any capture markers from the response and writes them as new atomic notes
9. Logs the run as one JSONL line in `log/YYYY-MM/YYYY-MM-DD.jsonl`

The vault is the source of truth. The runtime is stateless. Kill it, restart it, switch from cron to interactive — the agent is the same agent because the files are the same.

---

## Repository structure

```
atomic_agents/                  # the Python package
├── agent.py                    # AtomicAgent class (the main runtime)
├── exceptions.py               # custom exceptions
├── types.py                    # shared dataclasses
├── cli.py                      # `atomic-agents` console script
├── _platform.py                # agents_root resolution
├── _io.py                      # atomic file writes (temp + fsync + rename)
├── _locks.py                   # per-agent flock with stale-lock recovery
├── _schema.py                  # frontmatter validation
├── _capture.py                 # parse + write atomic captures
├── _costs.py                   # pricing + multi-tier guardrails
├── _model.py                   # parse model.md
├── _tools.py                   # parse tools.md
├── _llm.py                     # provider routing
└── dashboard/                  # cost & observability dashboard
    ├── costs.py                # aggregation
    ├── render.py               # HTML output
    ├── serve.py                # optional local web server
    └── __main__.py             # python -m atomic_agents.dashboard ...

tests/                          # 257 tests, all passing on Python 3.11+
docs/                           # spec + implementation guides
├── README.md                   # spec entry point
├── architecture.md             # 30-second mental model + design rationale
├── spec/                       # 13 locked spec docs (anatomy → research integrity)
├── implementation/             # build guides per runtime (cron, Claude skill, dashboard, ...)
├── samples/caldwell/           # complete worked single-agent example
├── appendix/portability.md     # using Atomic Agents without Obsidian / on any OS
└── package-readme.md           # this package's PyPI/distribution readme

extras/                         # operational templates (skills, schedulers)
├── claude-code-skills/         # SKILL.md wrappers for Claude Code
├── launchd/                    # macOS LaunchAgent .plist templates
└── cron/                       # crontab examples + portable wrapper script
```

---

## The spec

Start at [`docs/README.md`](docs/README.md) for the spec entry point. The 13 spec docs in [`docs/spec/`](docs/spec/) cover:

- [01 — Anatomy](docs/spec/01-anatomy.md) (file layout, persona, memory, wiki, journal, log)
- [02 — Atomic Memory](docs/spec/02-atomic-memory.md) (Notes + Wiki + INDEX-driven recall)
- [03 — File formats](docs/spec/03-file-formats.md) (frontmatter schemas + filename conventions)
- [04 — Runtime assembly](docs/spec/04-runtime-assembly.md) (canonical load sequence)
- [05 — Capture rules](docs/spec/05-capture-rules.md) (when and how agents write to memory)
- [06 — Multi-agent projects](docs/spec/06-multi-agent-projects.md) (role cascade for shared-team agents)
- [07 — Research foundations](docs/spec/07-research-foundations.md) (lineage and citations)
- [08 — Evaluation](docs/spec/08-evaluation.md) (rubrics + LLM-as-judge framework)
- [09 — Cost & observability](docs/spec/09-cost-observability.md) (pricing, dashboard, guardrails)
- [10 — Helpers](docs/spec/10-helpers.md) (cheap-LLM workers for transformation subtasks)
- [11 — Tuning](docs/spec/11-tuning.md) (eval-driven self-improvement)
- [12 — Goals & intent](docs/spec/12-goals-and-intent.md) (goal-driven agents)
- [13 — Research integrity](docs/spec/13-research-integrity.md) (citations + factual accuracy)

---

## Configuration

### `ATOMIC_AGENTS_ROOT`

Tells the framework where to find your agent vault. Default: `~/agents/agents`.

```bash
export ATOMIC_AGENTS_ROOT=/path/to/your/agents
```

### API keys (loaded in this order)

1. Environment variables: `ATOMIC_AGENTS_ANTHROPIC_KEY`, `ANTHROPIC_API_KEY`
2. macOS Keychain: `security add-generic-password -a $USER -s atomic-agents-anthropic -w sk-ant-...`
3. `~/.config/atomic_agents/keys.json` (chmod 600):
   ```json
   {"anthropic": "sk-ant-...", "openai": "sk-...", "moonshot": "..."}
   ```

Same pattern for OpenAI (`atomic-agents-openai`) and Moonshot (`atomic-agents-moonshot`).

---

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Run tests
uv run pytest

# Run a specific test
uv run pytest tests/test_capture.py -v
```

---

## License

MIT — see [LICENSE](LICENSE).

---

## Status

**v0.1, alpha.** Core runtime is stable and tested. Subsequent versions add eval, tuning, goals, and other modules per the spec. Currently developed by a single user (Sam); reference implementation that anyone can use, fork, or extend.

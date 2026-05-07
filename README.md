# atomic-agents-stack

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

## What's in v0.1

| Component | Status |
|---|---|
| `AtomicAgent` runtime | ✅ |
| Persona loading (IDENTITY, SOUL, USER) | ✅ |
| `memory/` + `wiki/` INDEX-driven recall | ✅ |
| Helper-mediated atomic captures (fenced JSON) | ✅ |
| Multi-tier cost guardrails (50%/80%/100%) | ✅ |
| Helper calls — sequential + parallel | ✅ |
| Anthropic / OpenAI / Moonshot Kimi routing | ✅ |
| File locking with stale-lock recovery | ✅ |
| Schema validation incl. date-suffix filenames | ✅ |
| Cost dashboard (HTML, global + per-agent) | ✅ |
| Optional local dashboard server | ✅ |
| Eval runner | ❌ v0.2 |
| Tuning analyzer | ❌ v0.3 |
| Goal manager | ❌ v0.4 |
| Schema migration runner | ❌ v0.5 |
| Tool-call captures (Path 1) | ❌ v0.6 |
| Multi-agent project cascade loader | ❌ v0.7 |
| Helper provenance enforcement | ❌ v0.8 |

The spec for everything is finalized; the Python implementation lands incrementally.

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

tests/                          # 67 tests, all passing on Python 3.11+
docs/                           # spec + implementation guides
```

---

## The spec

The Atomic Agents v1 specification is documented in `docs/`. It covers:

- Anatomy of an agent (file layout, persona, memory, wiki, journal, log)
- Atomic Memory (Notes + Wiki + INDEX-driven recall)
- File formats and frontmatter schemas
- Runtime assembly order (the canonical load sequence)
- Capture rules (when and how agents write to memory)
- Multi-agent projects with role cascade
- Cost & observability
- Helpers (cheap-LLM workers for transformation subtasks)
- Evaluation framework
- Tuning (eval-driven improvement)
- Goals & intent (for goal-driven agents)
- Research integrity (citation requirements + factual accuracy)

---

## Configuration

### `ATOMIC_AGENTS_ROOT`

Tells the framework where to find your agent vault. Default: `~/docs/agents`.

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

**v0.1, alpha.** Core runtime is stable and tested. Subsequent versions add eval, tuning, goals, and other modules per the spec. Currently developed by a single user (Dan Powers); reference implementation that anyone can use, fork, or extend.

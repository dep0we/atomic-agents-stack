# atomic_agents — Python implementation of the Atomic Agents spec

Reference implementation of the Atomic Agents v1 spec at
`<vault>/Atomic Agents/spec/`.

## Quick install

From the automations repo root:

```bash
uv sync
```

## Quick use

```python
from atomic_agents import AtomicAgent

agent = AtomicAgent(name="caldwell", trigger="cron")
response = agent.call(work_item="Daily morning brief")
print(response.text)
print(f"Cost: ${response.cost_usd:.4f}")
print(f"Captures: {len(response.captures)}")
```

The agent reads everything it needs from `~/agents/caldwell/`:
- `persona/IDENTITY.md`, `SOUL.md`, `USER.md`
- `tools.md` (read/write paths, hard NOs)
- `model.md` (default model, cost guardrails)
- `memory/INDEX.md` + recent + pinned atomic notes
- `wiki/INDEX.md` (if present)
- Recent journal entries

Override the agents-root location:

```bash
export ATOMIC_AGENTS_ROOT=/path/to/your/agents
```

## CLI

```bash
# Run an agent
atomic-agents run caldwell --work-item "Should I prepay the mortgage with the Q1 bonus?"

# Show config for an agent
atomic-agents info caldwell
```

## What's implemented (v0.1)

| Feature | Spec ref | Status |
|---|---|---|
| Persona/tools/model loading | spec/01, spec/04 | ✅ |
| Canonical system prompt assembly | spec/04 | ✅ |
| Memory INDEX + recent + pinned loading | spec/02, spec/04 | ✅ |
| Atomic file I/O (temp + fsync + rename) | spec/04, shared-helper | ✅ |
| Per-agent flock | shared-helper | ✅ |
| Capture marker parsing (fenced JSON) | spec/05 (Wave 5 format) | ✅ |
| Capture write + INDEX update | spec/05, shared-helper | ✅ |
| Cost calculation per model | spec/09, shared-helper | ✅ |
| Multi-tier cost guardrails (50/80/100%) | spec/09, shared-helper | ✅ |
| Helper calls (sequential) | spec/10 Pattern A | ✅ |
| Helper calls (parallel) | spec/10 Pattern B | ✅ |
| Anthropic API integration | _llm | ✅ |
| OpenAI API integration | _llm | ✅ (optional dep) |
| Moonshot Kimi routing | _llm | ✅ (optional dep) |
| Frontmatter validation | spec/03 | ✅ |
| Run logging (JSONL) | spec/01, spec/09 | ✅ |
| Tool-call captures (Path 1) | spec/05 | ❌ deferred |
| Eval runner | spec/08 | ❌ separate module |
| Tuning analyzer | spec/11 | ❌ separate module |
| Cost dashboard renderer | spec/09 | ❌ separate module |
| Goal manager | spec/12 | ❌ separate module |
| Schema migration | spec/03 | ❌ separate module |

## Architecture

```
atomic_agents/
├── __init__.py        ← public API exports
├── agent.py           ← AtomicAgent class (the main runtime)
├── exceptions.py      ← custom exceptions
├── types.py           ← dataclasses
├── cli.py             ← `atomic-agents` console script
├── _platform.py       ← agents_root resolution, path expansion
├── _io.py             ← atomic_write, append_jsonl, cleanup
├── _locks.py          ← AgentLock (flock-based)
├── _schema.py         ← frontmatter validation per spec/03
├── _capture.py        ← parse fenced atomic_capture blocks; write notes
├── _costs.py          ← pricing table, cost calc, guardrail accounting
├── _model.py          ← parse model.md
├── _tools.py          ← parse tools.md
└── _llm.py            ← provider routing (Anthropic/OpenAI/Moonshot)
```

Underscore-prefixed modules are internal; only `AtomicAgent` and the types
in `__init__.py` are public API.

## Secrets

API keys are loaded in this order (per spec/04 secrets handling):

1. Environment variables (`ATOMIC_AGENTS_ANTHROPIC_KEY`, `ANTHROPIC_API_KEY`)
2. macOS Keychain entry named `atomic-agents-anthropic`
3. `~/.config/atomic_agents/keys.json` (chmod 600)

Same pattern for OpenAI (`atomic-agents-openai`) and Moonshot
(`atomic-agents-moonshot`).

Never put API keys in the LaunchAgent plist or commit them.

## Tests

```bash
uv run pytest tests/test_atomic_agents/
```

## Development status

This is **v0.1 — core MVP**. It can run a single agent end-to-end with
captures and cost guardrails. Eval / tuning / dashboard / goal / migration
modules ship separately as they're built.

The spec is at v1.0; the code is at v0.1. The gap is intentional — get one
agent running first; add the other layers as their need emerges.

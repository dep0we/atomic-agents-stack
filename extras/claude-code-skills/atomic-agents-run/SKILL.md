---
name: atomic-agents-run
description: Invoke an Atomic Agent against a work item. Loads the agent's persona, memory, and tools, calls the LLM, writes captures and a journal/log entry.
---

# atomic-agents-run

Run one Atomic Agent against a single work item.

## When to use

- The user says "run \<agent\>" or "ask \<agent\> about ..." or "have \<agent\> draft ..."
- The user wants a one-shot invocation, not a scheduled run
- The agent already exists at `<ATOMIC_AGENTS_ROOT>/<agent>/`

If the agent doesn't exist yet, do **not** auto-create it — agents are created intentionally. Tell the user the path you expected to find and ask whether to scaffold one.

## Before running

Confirm with the user:
1. Which agent? (Suggest names from `ls $ATOMIC_AGENTS_ROOT/`.)
2. What work item? (Multi-line work items are fine.)
3. Any flags? — `--critical` bypasses cost guardrails, `--no-write-captures` runs dry, `--model-override <id>` swaps the model for this call.

## Invocation

```bash
atomic-agents run <agent> --work-item "<work item text>"
```

With flags:

```bash
atomic-agents run <agent> \
  --work-item "<work item text>" \
  --trigger skill \
  --critical \
  --model-override claude-haiku-4-5-20251001
```

If `ATOMIC_AGENTS_ROOT` is not set in the environment, pass `--agents-root <path>`.

## Reading the output

The CLI prints the agent's response, then a footer with:
- Model used + token counts (input / output / cache hits)
- Cost in USD
- Latency
- Number of captures written
- Run ID + log path

A new line lands in `<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl`. New captures show up in `<agent>/memory/` and the `INDEX.md` is updated.

## Common follow-ups

- "How did it cost?" → already in the footer; run `python -m atomic_agents.dashboard render` for a fuller view
- "What did it remember?" → list new files: `ls -lt $ATOMIC_AGENTS_ROOT/<agent>/memory/ | head -5`
- "Run it again" → just re-invoke; locks prevent concurrent calls but sequential is fine

## Troubleshooting

- **`AgentLockBusy`** → another invocation is running, or a stale lock from a crash. Wait a moment, or check `<agent>/.lock` mtime; the lock auto-releases after 5 minutes.
- **`CostGuardrailBlocked`** → today's or this month's cap was hit. Either pass `--critical` (logged), or adjust caps in `<agent>/model.md`.
- **`No API key found`** → set `ANTHROPIC_API_KEY` (or the relevant provider env var), add to macOS Keychain, or write `~/.config/atomic_agents/keys.json`. See the top-level README for full load order.

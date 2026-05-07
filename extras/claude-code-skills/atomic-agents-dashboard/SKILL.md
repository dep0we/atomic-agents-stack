---
name: atomic-agents-dashboard
description: Render or serve the Atomic Agents cost dashboard — global + per-agent token usage, cost, run counts. HTML output, optional local web server with Refresh button.
---

# atomic-agents-dashboard

Generate the cost & observability dashboard. Read-only; safe to run any time.

## When to use

- The user says "show me costs" or "what did we spend?" or "open the dashboard"
- After a busy run streak, to confirm guardrails are holding
- Periodic check-in (weekly is enough — usage is JSONL-derived, not real-time)

## Two modes

### `render` — write static HTML once

```bash
python -m atomic_agents.dashboard render
```

Writes `<ATOMIC_AGENTS_ROOT>/_dashboard/index.html` with:
- Global rollup (last 7 days, last 30 days, all-time)
- Per-agent breakdown
- Per-day cost chart
- Top 10 most expensive runs
- Cost-per-million-tokens by model

After rendering, open it: `open $ATOMIC_AGENTS_ROOT/_dashboard/index.html` (macOS) or `xdg-open` (Linux).

### `serve` — local web server with Refresh button

```bash
python -m atomic_agents.dashboard serve
```

Same dashboard, served on a local port (default 8765). Click Refresh in the page header to re-aggregate without restarting the server. Stop with Ctrl-C.

Use `--port <n>` to override and `--bind 127.0.0.1` (the default) — never bind to `0.0.0.0` unless you intend to expose it on the network.

## Where the data comes from

Pure JSONL aggregation across `<ATOMIC_AGENTS_ROOT>/<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl`. No database, no daemon, no warmup.

This means:
- The dashboard is always fresh — it reads the source-of-truth files directly
- It survives any vault sync (Obsidian Sync, git, rsync, etc.) — the logs are just files
- Performance is bounded by log volume; expect ~100 ms for a few hundred runs, ~1 s for thousands

## Reading the output

Costs are calculated using the prices baked into `atomic_agents/_costs.py`. If a model's price changes upstream, update that table and regenerate.

Cache savings show up as a separate line — Anthropic prompt caching at 90% discount is significant when an agent has a long stable persona.

## Common follow-ups

- "Why is \<agent\> so expensive?" → inspect its model.md (token caps + default model), then sample a few recent log lines
- "Set a tighter cap" → edit `<agent>/model.md` `cost_guardrails:` block; takes effect on the next run
- "Export to a sheet" → grab the underlying JSONL: `cat $ATOMIC_AGENTS_ROOT/*/log/*/*.jsonl | jq -s . > runs.json`

## Notes

- The static `render` form is what you want for CI / scheduled reports — it produces a deterministic HTML artifact you can email or post
- The `serve` form is what you want for active monitoring — keep it running in a tmux pane while you iterate on agents

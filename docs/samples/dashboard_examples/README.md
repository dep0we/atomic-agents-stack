# Dashboard examples

These are pre-rendered HTML samples of the cost + 4 visibility tabs the framework generates. They were rendered against the [Caldwell sample agent](../caldwell/) (the fictional Sam's financial-planning assistant) so you can open them in a browser and see what the dashboard looks like before building anything.

## What's here

| File | Tab | What it shows |
|---|---|---|
| [`index.html`](index.html) | **Cost** | Per-agent cost rollups, daily chart, top-10 expensive runs, suggested caps |
| [`activity.html`](activity.html) | **Activity Pulse** | Last 50 runs, recent failures, captures, tool calls, delegations, dreams, lock state |
| [`quality.html`](quality.html) | **Quality Trends** | Eval score trends, hard-fails, factual accuracy, pending tuning proposals, helper provenance health |
| [`memory.html`](memory.html) | **Memory Snapshot** | Note counts by type, staleness candidates, orphan check, version-churn leaders, dream history |

The **Goals** tab is conditional — it only renders when at least one agent has a `goal.md`. Caldwell is reactive (no goal), so `goals.html` is correctly absent.

## How to open

Just double-click any of the `.html` files, or:

```bash
open index.html         # macOS
xdg-open index.html     # Linux
start index.html        # Windows
```

Click through the nav bar at the top to walk all four tabs.

## How to render your own

```bash
export ATOMIC_AGENTS_ROOT=~/agents       # your vault
python -m atomic_agents.dashboard render
open ~/agents/_dashboard/index.html
```

Or for live-refresh (with a Refresh button):

```bash
python -m atomic_agents.dashboard serve
# visit http://127.0.0.1:8765
```

## How these were rendered

```bash
ATOMIC_AGENTS_ROOT=docs/samples python -m atomic_agents.dashboard render
# Then copied from docs/samples/_dashboard/ to here.
# (The _dashboard/ output dir is gitignored as runtime state; this dir is not.)
```

When the framework changes meaningfully or the Caldwell sample's data changes, regenerate these by running the same command and copying the output here.

## Notes

- Self-contained HTML — no JavaScript, no external CSS, no fonts. Works offline.
- The data shown reflects the Caldwell sample's vault state at the time of rendering. Numbers are illustrative, not load-bearing.
- The `data/` subdir holds chart data referenced by the cost page. It's small.

# 09 — Cost & Observability

How Atomic Agents tracks token usage, cost, and operational health — and how the operator sees it without digging through JSONL by hand.

This spec covers two intertwined concerns:

1. **Observability** — what's actually happening across all agents (token use, cost, latency, errors, model selection)
2. **Cost guardrails** — programmatic enforcement so a runaway loop doesn't burn through a credit card

Together these answer the question: *"Is this system safe to leave running?"*

---

## What gets logged (recap from spec/01)

Every agent invocation produces one JSONL line in `log/YYYY-MM/YYYY-MM-DD.jsonl` with at minimum:

```json
{
  "ts": "ISO 8601 timestamp with timezone",
  "trigger": "cron | skill | api | manual",
  "model": "model ID used (FULL ID, e.g., claude-opus-4-7-20260101)",
  "input_tokens": 4102,
  "output_tokens": 892,
  "status": "ok | error | skipped",
  "summary": "short one-line description"
}
```

Strongly recommended additional fields:
- `cost_usd` — computed at runtime per `model.md` pricing table
- `cache_hit_tokens` — how many input tokens were served from prompt cache
- `cache_miss_tokens` — how many were uncached input
- `latency_ms` — time from request to response
- `error` — error message string when status=error

The dashboard depends on these being populated. A runtime that doesn't write them gets a dashboard with blank cells; runtimes should populate everything they reasonably can.

### Why model is critical

Codex's review flagged this and Sam reinforced it: **when the model changes mid-month for an agent (Caldwell goes from Opus to Sonnet because of a budget cap, or because Sam manually flipped the default), the dashboard must reflect that switch**.

The fix is mechanical: every record carries its own `model` field. Aggregations group by `(agent, model, day)` rather than just `(agent, day)`. Charts and tables surface the model breakdown explicitly.

This means a single agent's monthly spend may show two model rows — that's correct, not a bug.

---

## Dashboard layout

### Global dashboard — `<agents_root>/_dashboard/index.html`

The "first page you check" — answers "where am I spending most?"

Sections, top to bottom:

1. **Current month at a glance**
   - Total spend across all agents this month
   - Total runs
   - Composite cache hit rate
   - Trending arrows vs. last month (↑ red / ↓ green)

2. **Per-agent table** (this month)
   - Agent name | Spend | vs. last month | Runs | Errors | Cache hit % | Models used (multi-row if more than one)
   - Sortable by any column

3. **Month-over-month chart** (rolling 12 months)
   - Stacked bar: each bar is one month, segments are agents
   - Hover for breakdown
   - Shows composition shifts (Caldwell took over from another agent in spend; new agent appeared)

4. **Top 5 most expensive runs this month**
   - Date | Agent | Trigger | Model | Tokens (in/out) | Cost | Summary
   - Each row links to the journal entry for that run (if exists)

5. **Provider breakdown** (current month)
   - Pie/donut: Anthropic / OpenAI / Local / Other
   - Useful when an agent is multi-vendor

6. **Model-mix breakdown** (current month, all agents)
   - Stacked: Opus / Sonnet / Haiku / GPT-5 / GPT-5-mini / etc.
   - Shows you the cost-tier mix of your fleet

### Per-agent dashboard — `<agents_root>/{agent_name}/dashboard.html`

The drill-down — answers "is Caldwell trending up?" or "did the model switch save me money?"

Sections:

1. **12-month trend line** (cost per month, this agent only)
2. **Daily heatmap** (calendar view, intensity = cost or run count)
3. **Model-over-time chart** — explicitly shows when model switched (the load-bearing requirement)
4. **Cache savings**: "you saved $X this month by caching" (computed: cache_miss_cost vs. hypothetical-no-cache cost)
5. **Helper savings** *(when this agent uses helpers per spec/10)* — see below for spec
6. **Top 10 most expensive runs** (longer list than global; agent-specific)
7. **Error rate trend**
8. **Budget vs. actual** if `model.md` declared cost_guardrails

#### Helper savings chart

When an agent uses Atomic Helpers, the dashboard surfaces "what did I save by routing transformations to cheap models?"

**Computation:**

For each helper call in the period:
- Helper actual cost: `cost_usd` from the log JSONL line (with `trigger: helper`)
- Hypothetical cost-if-Opus: `(input_tokens + output_tokens)` × Opus pricing per token

Helper savings = sum of (hypothetical Opus cost − actual helper cost) across all helper calls.

**Display:**

```
Helper Savings This Month
─────────────────────────
Helper calls:           42
Helper actual cost:     $0.31
If those had been Opus: $4.12
You saved:              $3.81  (12.3× cheaper)

Most-used helper models:
  claude-haiku-4-5-20251001    36 calls   $0.18
  claude-sonnet-4-6-20260101    4 calls   $0.09
  moonshot/kimi-2.6              2 calls   $0.04
```

This justifies the helper pattern with hard numbers. Without it, you can't tell whether helpers are paying off or are just architectural noise.

The helper rollup line on the per-agent table at top-of-dashboard:

```
Caldwell — May 2026
  Main calls (Opus + Sonnet fallback):  $4.85
  Helper calls (Haiku):                  $0.31  (saved $3.81 vs all-Opus)
  Total:                                 $5.16
```

---

## Where the dashboard lives

```
<agents_root>/
├── _dashboard/
│   ├── index.html              ← global view
│   ├── chart.js                ← bundled charting (~80KB Chart.js)
│   ├── style.css               ← single stylesheet
│   ├── data/
│   │   ├── 2026-05.json        ← pre-aggregated current month
│   │   ├── 2026-04.json        ← prior months retained 12 months
│   │   └── ...
│   └── server.py               ← optional local server for live refresh (see below)
├── caldwell/
│   ├── ...
│   └── dashboard.html          ← per-agent, links back to /_dashboard/index.html
└── ...
```

**Self-contained** — no internet needed, no server required (server is optional). HTML opens directly in a browser. Works on phones via Obsidian Mobile or any browser app pointing at the file.

---

## Refresh modes

### Nightly (default, no setup)

A cron job runs once per night (default 03:00 local). It:
1. Walks every agent's `log/` directory
2. Parses all JSONL files in the current month + prior 12 months
3. Aggregates by `(agent, model, day)`
4. Renders the HTML files via Jinja2
5. Writes pre-aggregated JSON for fast page-load

Pure Python, zero LLM calls, ~30 seconds total. Nightly is enough for most uses — the dashboard is for trend analysis, not real-time monitoring.

### Manual refresh (no setup, page reload)

If you click the dashboard's **Refresh** button without the optional server running, the page just reloads — showing whatever data the last nightly run produced. Honest fallback.

### Live refresh with optional local server

For "I want to see now" cases, run:

```bash
python -m atomic_agents.dashboard serve
```

Starts a small Flask/FastAPI server on `localhost:8765` with a `/regenerate` endpoint. The Refresh button now triggers an actual aggregation pass and reloads with fresh data. Server is optional and doesn't auto-start.

---

## Cost guardrails

Tracking is one half. The other is **preventing** runaway costs from a misconfigured agent or a runaway loop.

### model.md `cost_guardrails` block

```yaml
cost_guardrails:
  enabled: false                    # default OFF for new agents — see "First two weeks" below
  daily_cap_usd: 5.00
  monthly_cap_usd: 100.00
  daily_cap_action: skip            # skip | fallback | alert
  monthly_cap_action: alert
  warning_thresholds: [0.50, 0.80]  # fire warnings at 50% and 80% before cap action
  alert_channel: telegram           # telegram | email | journal | log_only
```

**Field meanings:**

| Field | Purpose |
|---|---|
| `enabled` | Master switch. Defaults to `false` so new agents observe-only until you have data. |
| `daily_cap_usd` | Max cost per calendar day per this agent. |
| `monthly_cap_usd` | Max cost per calendar month. |
| `*_cap_action` | What happens when the cap is hit: `skip` (don't run), `fallback` (use the model.md fallback model instead of default), or `alert` (run anyway, surface a warning). |
| `warning_thresholds` | Fractional thresholds for early warnings. `[0.50, 0.80]` means warn at 50% and 80% of cap. |
| `alert_channel` | Where alerts surface. `telegram` requires bot config in env; `journal` writes to today's journal entry; `log_only` writes to the log JSONL with `warning: true`. |

### First two weeks: observe-only

For a brand-new agent, **leave `enabled: false`**. Run for 1-2 weeks. Let the dashboard collect real usage data.

After two weeks, check the per-agent dashboard. The dashboard suggests reasonable caps based on observed usage — typically:
- `daily_cap_usd` = ~3× observed daily average (allows for spikes)
- `monthly_cap_usd` = ~1.5× observed monthly total

Manually flip `enabled: true` and tune the values from there. Tightening later is easier than guessing first.

### Multi-tier warnings (the safety net)

Even with caps enabled, you don't want the only signal to be "agent is now skipped, no idea why." The warning thresholds give you progressive heads-ups:

```
50% of cap reached → INFO   (logged with warning: true; dashboard banner)
80% of cap reached → WARN   (alert via configured channel; dashboard banner)
100% of cap reached → CAP_ACTION (skip / fallback / alert per config)
```

You should see warnings before things break. If you see a CAP_ACTION without preceding warnings, something's wrong with the alerting pipeline — investigate.

### Where the enforcement lives

The shared helper (`atomic_agents`) reads `model.md` at agent init and checks the running cost before each call:

```python
def _check_cost_guardrails(self) -> CostCheckResult:
    if not self.cost_guardrails.enabled:
        return CostCheckResult(allow=True, action=None)

    today_cost = self._sum_cost_for_period("today")
    month_cost = self._sum_cost_for_period("this_month")

    daily_pct = today_cost / self.cost_guardrails.daily_cap_usd
    monthly_pct = month_cost / self.cost_guardrails.monthly_cap_usd

    # Fire warnings (idempotent — won't fire twice for the same threshold)
    self._maybe_fire_warning("daily", daily_pct)
    self._maybe_fire_warning("monthly", monthly_pct)

    # Take cap action if at or over 100%
    if daily_pct >= 1.0:
        return self._cap_action(self.cost_guardrails.daily_cap_action, "daily")
    if monthly_pct >= 1.0:
        return self._cap_action(self.cost_guardrails.monthly_cap_action, "monthly")

    return CostCheckResult(allow=True, action=None)
```

For runtimes that can't use the helper (Claude Code skill without the helper installed, ChatGPT web), guardrails are advisory — same caveat as `tools.md` enforcement (see [01-anatomy#policy-vs-enforcement](01-anatomy.md#policy-vs-enforcement)).

### Behavior matrix per cap action

| Action | What happens at 100% | When to use |
|---|---|---|
| `skip` | Run is aborted before the API call. Log entry written with `status: skipped`. No tokens spent, no LLM call made. | Cron jobs where missing a run is OK (daily brief can wait until tomorrow). |
| `fallback` | Default model is swapped for the model.md `fallback`. Run proceeds at lower cost. Log entry tagged with `fallback: true`. | Skill sessions where Sam needs a response but Sonnet is fine. |
| `alert` | Run proceeds with default model. Warning surfaced to alert_channel. | High-priority interactive sessions where you'd rather pay than fail. |

For Caldwell on cron: `skip`. For Caldwell on skill: `fallback`. Both alert.

### Critical-flag override

A user can manually flag a run as critical to bypass guardrails:

```python
agent.call(work_item, critical=True)
```

Or for the skill version, prefix the message with `!critical` (or whatever marker the skill defines). Critical runs:
- Bypass the cap action (run proceeds with default model)
- Still log as critical in the JSONL (`critical: true`)
- Still fire warnings
- Don't bypass hard NOs in `tools.md`

This is the escape hatch for "this question really can't wait."

---

## Suggested-caps generation (the "first two weeks" UX)

After 14 days of observed data, the per-agent dashboard surfaces a banner:

> **Caldwell has been running 14 days with cost guardrails disabled.**
> Observed average: $0.14/day, $4.20/month
> Suggested caps: daily $0.50 (3× avg), monthly $7 (1.5× avg)
> [Apply suggested caps] [Use my own values] [Keep observe-only]

If Sam clicks "Apply", the dashboard writes the values into the agent's `model.md` `cost_guardrails` block (with `enabled: true`). The next run picks them up.

This converts the guardrails from "thing I had to guess at" to "thing the data set for me." Sam's specific UX point.

---

## What about local-only models?

If the agent uses a local model (Qwen, Llama, etc.), `cost_usd` is `0.00` for the API call itself but you still want to track:
- `latency_ms` — local models are slower
- Compute resource (GPU, memory) — currently out of scope; note for v2
- Error rate — local models fail differently

The dashboard still works; the cost segment for local-model-runs shows $0 with a "local" badge.

---

## What's NOT in v1

- **Real-time alerting** — alerts are post-hoc (next run sees the cap and acts). True real-time requires a separate watcher process; deferred.
- **Cross-agent budget pooling** — "all agents combined: $200/month cap" — not in v1; per-agent only.
- **Vendor-bill reconciliation** — comparing Atomic Agents' computed cost to the actual Anthropic/OpenAI invoice. Reasonable accuracy is on us; we don't reconcile against vendor bills automatically.
- **Forecasting** — projecting end-of-month spend from current trend. Nice but deferred.

---

## Privacy

The dashboard contains:
- Cost numbers (real $)
- Token counts
- Trigger types
- Model IDs
- Run summaries (from log JSONL `summary` field — agent-authored)

The dashboard does NOT contain:
- Conversation content (those live in journal/, separately)
- API keys
- Frontmatter from atomic notes
- Personal data unless deliberately surfaced via summaries

If you publish or share the dashboard, you're publishing cost and token telemetry, not the agent's actual conversations. But: the `summary` fields are agent-authored and may leak topical context — review before sharing.

The HTML files are world-readable on disk by default. If your `<agents_root>` is on a shared filesystem, set permissions appropriately.

---

*Next (Wave 4): [08-evaluation](08-evaluation.md) — quality scoring with rubrics + LLM-as-judge.*

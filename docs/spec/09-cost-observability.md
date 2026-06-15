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
  "run_id": "stable ID for this agent run",
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

Helper, tool, and delegate child records use the same `run_id`-anchored audit trail and add `parent_run_id` to link back to the parent agent run. Sample JSONL files should include those IDs instead of eliding them for readability.

### Why model is critical

Codex's review flagged this and the operator reinforced it: **when the model changes mid-month for an agent (Caldwell goes from Opus to Sonnet because of a budget cap, or because the operator manually flipped the default), the dashboard must reflect that switch**.

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

    # sum_cost_for_period returns CostReadResult(total_usd, degraded, dropped_records)
    # rather than a bare float; see §"Cost-read error posture (fail-closed-when-blind)"
    today_result = sum_cost_for_period(log_dir, "today", ...)
    month_result = sum_cost_for_period(log_dir, "this_month", ...)

    # Fail-closed if either read is degraded (blind / majority-corrupt current-day file)
    if today_result.degraded or month_result.degraded:
        return CostCheckResult(allow=False, action="skip",
                               reason="cost data unreadable — fail-closed",
                               cost_data_degraded=True)

    today_cost = today_result.total_usd
    month_cost = month_result.total_usd

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

### Cost-read error posture (fail-closed-when-blind) — v1.5

The cost-summing reader (`sum_cost_for_period` and `_sum_via_backend`) returns a `CostReadResult(total_usd, degraded, dropped_records)` rather than a bare float. The error posture is two-tier:

- **Whole-file OSError on the current-day guardrail log** (or any backend exception) → `degraded=True`, `total_usd=0.0` — the gate is blind, fail-closed.
- **Empty / whitespace-only file** (0-byte or all-blank) → `degraded=False`, no cost contribution — treated identically to an ABSENT file, NOT as corruption. An empty file is readable; it simply has no logged cost yet. This is load-bearing because the log writer's `open("a")` creates the file before the first write+fsync, so a concurrent reader hits a legitimate 0-byte window on every first append of the day — failing closed there would spuriously block a legitimate call on a normal append race.

  **Trust-boundary note (cost-log integrity is NOT defended here).** Because empty/absent reads as `$0`, an actor who can **truncate or delete** the cost log can reset the agent's apparent spend and re-open the full cap. This is a deliberate non-goal of the filesystem backend, not a regression: the same vector is unchanged from before the #495 read-error work (empty/absent has always summed to `$0`), and an actor with write access to the agent's `log/` can equally forge cheap (`cost_usd: 0`) records or edit `model.md` caps directly. Per CLAUDE.md Principle #1 (the vault is the source of truth) and TENSIONS T15 / spec/44's trust model, a writer inside the vault is in-scope-trusted; tamper-evident cost accounting (signed/append-only ledger, monotonic counter, or a separate authoritative store) is the job of a real-authz backend (Postgres/Redis), not the filesystem default. Surfaced by cross-family review during #495; tracked as [#500](https://github.com/dep0we/atomic-agents-stack/issues/500).
- **Per-line JSON, non-numeric, or boolean `cost_usd` corruption below 50% of non-empty lines** → skip + `logger.warning` (deduped per file+reason per process), `degraded=True`, partial total surfaced. A boolean `cost_usd` is rejected as corruption on BOTH the filesystem and backend paths (`bool` is an `int` subclass, so `float(True)==1.0` would silently mis-count it) — the two cost-read paths are symmetric on every malformed value.
- **Per-line corruption above 50% of non-empty lines, on the CURRENT-DAY guardrail file** → `degraded=True`, `total_usd=0.0` — fail-closed. The current-day file is the one the gate must trust, so a majority-corrupt current-day log fails the whole read closed.
- A **historical** file that is unreadable (OSError) or majority-corrupt (>50%) in a monthly walk → skip that file's cost contribution + `degraded=True` (partial month total); it does **not** zero the whole read. Current-day and historical blindness are handled **asymmetrically by design**: a blind current-day file fails the whole read closed (it is the file the gate must trust), while a blind historical file is skipped (degraded + partial month total) so a single garbage old daily log cannot brick the gate for the rest of the month.

A **majority-corrupt or unreadable backend read** (via `_sum_via_backend`, for **either** the `today` or `this_month` period) fails the whole read closed (`degraded=True`, `total_usd=0.0`). For the **monthly** period this is stricter than the filesystem path, which skips only the offending historical file and keeps summing; for the **today** period both paths whole-read fail-close identically (the filesystem path's current-day file is the file the gate must trust). The backend path can over-block, never under-block — safe under fail-closed-when-blind. The genuine, reachable asymmetry between the two paths is the **denominator** for the >50% threshold: the filesystem path counts ALL non-empty lines, while the backend path counts only cost-bearing (non-`None`) records — the backend has already applied the since/until/source/mandate filters server-side, so its denominator is smaller and trips the threshold sooner. Note that every shipped backend yields a `cost_usd` already coerced to `float | None` — via typed DB columns (SQLite `REAL`, Postgres `DOUBLE PRECISION`) or `RunRecord.from_dict`'s coercion — so a non-numeric string is coerced to `None` upstream and **skipped** (it never reaches the threshold denominator), and a numeric string is coerced to a float and summed, the same net result as the filesystem `float()` coercion. The backend loop's drop path (counting a record toward `dropped` and the threshold) is a **defensive belt** for a misbehaving custom backend that returns un-coerced raw objects; it is not a behavior any shipped backend exhibits. The cost-read error posture has now been **promoted into the `LogBackend` Protocol surface in [#497](https://github.com/dep0we/atomic-agents-stack/issues/497)**: conforming backends raise the typed `LogBackendReadError` from `query()` / `tail()` / `aggregate()` on an unrecoverable read failure (corruption, I/O error, lost connection after retries), and all three reference backends (filesystem, SQLite, Postgres) implement it against a real conformance suite. It is documented as a separate normative subsection — see spec/22 §"spec/22 addendum — Read-failure posture" — which reuses this section's empty-vs-failure taxonomy verbatim so the two specs agree (empty/absent → `[]`; unrecoverable read failure → raise `LogBackendReadError`). `_sum_via_backend` catches `LogBackendReadError` first (clean read-failure log) and keeps the broad `except Exception` as the unconditional fail-closed backstop for non-conforming custom backends.

**Gate sites** (`_check_cost_guardrails`, `_check_batch_reservation`, delegation headroom, `dream._check_cap`) gate on `cost_guardrails_enabled` BEFORE reading cost, then map `degraded=True` → fail-closed / over-cap **only when there is a budget to enforce**. A guardrails-DISABLED agent is never fail-closed by a degraded read — the enabled-gate short-circuits before the cost read at every gate site. A degraded read at `_check_cost_guardrails` always yields `action="skip"` (block), never `"fallback"` or `"alert"`: falling through to a fallback model on a blind read would still spend on a cheaper LLM, which is a fail-open. The degraded fail-close fires only when an effective cap exists — an own daily/monthly cap (model.md or Policy) **or** a parent coordinator tree-cap (`parent_remaining_headroom_usd`). An **uncapped** (warnings-only) agent where `daily_cap_usd == 0` and `monthly_cap_usd == 0` and no Policy cap and no parent tree-cap is **NOT** blocked by a degraded read: with no cap to bypass it always proceeds even with perfect cost data, so blocking it would be a spurious refusal with zero safety benefit. The `CostCheckResult.cost_data_degraded` flag is still set on the allowed result for audit honesty (the cost figure was a lower bound). The dashboard cost aggregation does **not** consume this cost-summing reader — it reads through the spec/22 `LogBackend.query()` path directly. That path remains tolerant of per-line JSON corruption (malformed lines are skipped), but as of issue #497 (spec/22's read-failure addendum) `query()` now raises `LogBackendReadError` on an *unrecoverable I/O-level read failure* (directory/file `OSError` such as `EIO`/`EACCES`, SQLite `DatabaseError`, lost Postgres connection). The dashboard read sites (`dashboard/costs.py:load_runs`, `dashboard/quality.py:_count_provenance`) wrap `query()` in `except LogBackendReadError` and degrade to an empty render (honoring their documented "falls back silently to an empty list" contract) rather than crashing — so the no-crash property holds for both per-line corruption (skipped) and I/O-level failures (degraded to empty), but the mechanism for the latter is the consumer-side catch, not `query()` swallowing the error. Reporting consumers that adopt the cost-summing reader in future (none today) should render the partial `total_usd` with a "data may be incomplete" indication rather than crash; wiring a degraded-aware banner into the dashboard is tracked as follow-up [#498](https://github.com/dep0we/atomic-agents-stack/issues/498). `dream.py` (log-signal read for consolidation) wraps `query()` in `except LogBackendReadError` and degrades gracefully (proceeds with an empty log signal) — analysis, not a control gate. The spec/40 **canonical-export** reader (`export/filesystem.py:export_log`, reached via `FilesystemLogBackend.query()`) is **INTENTIONALLY NOT** wrapped: a corrupt log MUST fail a canonical export loudly rather than silently emit a partial export presented as canonical. Issue #497 upgrades its prior raw-`OSError` propagation to the typed `LogBackendReadError` — behavior-neutral on the crash-vs-no-crash axis (it crashed the export before, it crashes the export now; only the exception type sharpened), so no consumer-side catch is added.

Pre-existing `query()`/`tail()`/`aggregate()` consumers in the **mandate subsystem** continue to behave as before, but the broad `except Exception` that absorbs `LogBackendReadError` lives at **different layers** depending on the call site (a future reader auditing the inner functions for the wrap will not find it there):

| Mandate read site | Where `query()` is called | Where the absorbing `except Exception` lives | Read-failure posture |
|---|---|---|---|
| Spend-gate prior-cost sums | `judge/mandate_check.py:672/704/740` (`_sum_prior_token_cost`) | **In the same function** (`mandate_check.py:685/714/762`) | fail-OPEN — treats a read failure as `$0` prior spend |
| Outstanding-reservation sums | `judge/mandate_reservations.py:535/567` (`compute_outstanding`, **UNWRAPPED**) | **At the callers** (`mandate_check.py` step-7/step-8) — a typed `except LogBackendReadError` (#497) precedes the broad `except Exception` | **fail-CLOSED on `LogBackendReadError`** (#497 interim guard → BLOCK `mandate_{token,external}_reservations_unreadable`); the broad except remains fail-OPEN for other read errors (full flip = #506) |
| Startup orphan-recovery scan | `mandate/filesystem.py:604/665` (`_scan_orphan_reservations`, **UNWRAPPED**) | **At the caller** (`agent.py:1032-1040/1066-1075` around `recover_orphan_reservations`) | degrade-and-skip recovery |

The new `LogBackendReadError` IS caught for every mandate read site, but the posture now SPLITS by site (#497): the **outstanding-reservation** read (`compute_outstanding` at the step-7/step-8 callers) gets a typed `except LogBackendReadError` that fail-CLOSES (immediate BLOCK), mirroring the layered catch this PR added to `_costs._sum_via_backend`; the **prior-cost sums** (`_sum_prior_token_cost` at `mandate_check.py:685/714/762`) still absorb it into their in-function broad `except` as `$0` prior spend (fail-OPEN). So #497 makes the outstanding-reservation path coherent with the cost/dream fail-closed posture, while the prior-cost-sum path remains fail-OPEN. This divergence is **documented, not silent**, and is the deliberate carve to issue [#506](https://github.com/dep0we/atomic-agents-stack/issues/506): promoting the mandate spend-gate to a fail-closed `BLOCK mandate_cost_unreadable` posture (a money gate MUST over-block, never read corrupt history as `$0`) is a behavior change to a different subsystem touching the LOCKED spec/29. **Granularity note (filesystem default):** on `main` a non-ENOENT per-file `OSError` during a mandate read was silently skipped (`except OSError: continue`), so the read completed with *partial* data (a small undercount); after #497 that same `EIO`/`EACCES` day file raises `LogBackendReadError`. At the **outstanding-reservation** read this now hits the typed guard → BLOCK (no undercount). At the **prior-cost-sum** sites (the remaining fail-OPEN, #506) the broad `except` still converts it to a *total* undercount (`$0` prior spend) rather than the pre-#497 *partial* undercount; and the recovery scan's per-file granularity changes from partial-recovery to whole-scan-skip-and-log at the orchestration layer (still no crash, still degrade-and-skip). Before #497's interim guard this would have made the (pre-existing) fail-OPEN spend-gate strictly *more* fail-open under disk read failure; the #497 guard neutralizes that for the outstanding-reservation read (now BLOCK). The REMAINING fail-OPEN — the prior-cost-sum `except Exception` sites (685/714/762) — plus the full fail-closed `BLOCK mandate_cost_unreadable` posture across all mandate read methods and the spec/29 LOCKED amendment are #506's scope. Deciding the recovery-scan raise-vs-skip posture is tracked alongside [#498](https://github.com/dep0we/atomic-agents-stack/issues/498).

### Behavior matrix per cap action

| Action | What happens at 100% | When to use |
|---|---|---|
| `skip` | Run is aborted before the API call. Log entry written with `status: skipped`. No tokens spent, no LLM call made. | Cron jobs where missing a run is OK (daily brief can wait until tomorrow). |
| `fallback` | Default model is swapped for the model.md `fallback`. Run proceeds at lower cost. Log entry tagged with `fallback: true`. | Skill sessions where the operator needs a response but Sonnet is fine. |
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

If the operator clicks "Apply", the dashboard writes the values into the agent's `model.md` `cost_guardrails` block (with `enabled: true`). The next run picks them up.

This converts the guardrails from "thing I had to guess at" to "thing the data set for me."

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

## The five tabs

The dashboard has grown from a cost-only view to a five-tab observability surface. Each tab answers a different operator question at a different cadence.

### Tab 1: Cost (`index.html`) — **Daily / weekly**

The original dashboard. Answers: *"Where am I spending?"*

Sections: global spend KPIs, per-agent cost breakdown, model mix bar, provider breakdown, top N most expensive runs. Data source: `log/YYYY-MM/*.jsonl`. When to use: whenever you want a cost baseline or notice an unexpected charge.

### Tab 2: Activity Pulse (`activity.html`) — **Daily check-in**

Answers: *"What's happening with my fleet right now?"*

Sections:
- Headline stats — runs in last 24h / 7d, failure count, agents-active count
- Last 50 runs table — timestamp (relative), agent, trigger, status, summary, duration, cost
- Recent failures (24h) — color-highlighted error rows
- Recent tool calls and delegations — filtered by trigger type
- Recent memory captures — newest `.md` files across all agent memory dirs
- Recent dream runs — last 20 dream manifests with consolidation/promotion counts
- Stale lock detector — agents whose `.lock` file has been held > 5 minutes

When to use: start of each day to confirm the fleet ran cleanly overnight, or after a deployment to spot regressions.

### Tab 3: Quality Trends (`quality.html`) — **Weekly review**

Answers: *"Are my agents getting better or worse?"*

Sections:
- Eval score trend per agent — sparkline of weighted_score over last 90 days
- Per-dimension breakdown — latest score and 30-day delta per rubric dimension
- Hard-fail occurrences (30d) — table of tests with non-empty hard_fails
- Pending tuning proposals — list of `evals/tuning_reports/*.md` files with file:// links
- Helper provenance health — per-agent percentage of helper calls where `provenance_preserved=True`

Data sources: `evals/runs/<YYYY-MM-DD>.jsonl`, `evals/tuning_reports/*.md`, `log/YYYY-MM/*.jsonl` (for provenance). When to use: weekly, before deciding whether to promote a tuning diff.

### Tab 4: Memory Snapshot (`memory.html`) — **Monthly review**

Answers: *"What does my fleet know, and is it healthy?"*

Sections:
- Note counts per agent per type (user / feedback / project / decision / reference / other)
- Staleness candidates — notes last_seen > N days ago (default 90) that aren't pinned; shows up to 5 per agent
- Orphan check — notes present in `memory/` but missing from `INDEX.md` (post-dream these should be zero)
- Version-churn leaders — top 20 notes by snapshot count in `.versions/`
- Dream history — all dream runs with consolidation/promotion/stale counts and applied status
- Memory size + growth — per agent: live bytes, version-history bytes, ratio

When to use: monthly, to decide what to prune, and after every major dream run to confirm it applied correctly.

### Tab 5: Goals & Outcomes (`goals.html`) — **As-needed; conditional**

Answers: *"What are my agents working on?"*

**Conditionally generated** — `goals.html` is only written if at least one agent has a `goal.md` file. If no agent has goals, the file is omitted and the Goals link is absent from every page's nav bar. This keeps the dashboard honest: empty tabs are noise.

Sections:
- Active goals — per goal: intent, priority, days since start, deadline status, sub-goal counts by status
- Sub-goal grid — per goal, a table of all sub-goals with status badges and last-advance timestamp
- Blocked sub-goals (operator action queue) — across all goals, all blocked sub-goals with reason and blocking dep
- Recent outcome runs — last 50 from `outcomes/runs/*/result.json`: status, iterations, cost
- Outcome iteration histogram — for satisfied outcomes in last 90 days: how many took 1 vs 2 vs 3 etc. iterations

Data sources: `goal.md`, `goal_history.jsonl`, `outcomes/runs/*/result.json`. When to use: whenever reviewing goal progress or deciding whether to manually unblock a stalled sub-goal.

---

## Dashboard file layout

```
<agents_root>/
├── _dashboard/
│   ├── index.html          ← Cost tab (global view)
│   ├── activity.html       ← Activity Pulse tab
│   ├── quality.html        ← Quality Trends tab
│   ├── memory.html         ← Memory Snapshot tab
│   ├── goals.html          ← Goals & Outcomes tab (CONDITIONAL — only if any goal.md)
│   └── data/
│       └── <month>.json    ← pre-aggregated cost JSON
├── <agent>/
│   └── dashboard.html      ← per-agent cost drilldown
└── ...
```

All five pages are self-contained HTML — inline CSS, no JS frameworks, no external assets. A top nav bar links all five tabs; the current page is highlighted. The nav bar omits Goals when `goals.html` is not rendered.

## CLI

```bash
python -m atomic_agents.dashboard render           # render all tabs
python -m atomic_agents.dashboard render --tab activity  # render one tab only
python -m atomic_agents.dashboard serve            # local dev server
```

The `--tab` flag accepts: `all` (default), `cost`, `activity`, `quality`, `memory`, `goals`. Use it for fast single-tab iteration during development without re-running the full aggregation pipeline.

---

*Next (Wave 4): [08-evaluation](08-evaluation.md) — quality scoring with rubrics + LLM-as-judge.*

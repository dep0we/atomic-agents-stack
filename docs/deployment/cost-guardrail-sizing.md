# Cost guardrail sizing

How to pick the daily / monthly / per-call caps and the `cap_action`
fields in your agent's `model.md` `cost_guardrails` block. Pair with
[`../spec/09-cost-observability.md`](../spec/09-cost-observability.md) —
that doc is the authoritative spec for the mechanism; this one is the
operator-facing how-to for the numbers.

The framework treats cost as first-class — every `agent.call()` checks
guardrails before each LLM call, every helper batch reserves
worst-case before dispatch, every `delegate()` clamps to the
coordinator's tree-cap. The discipline only pays off if the cap numbers
you write down are realistic. This doc is how to pick them.

---

## TL;DR

```
# 1. Default a new agent to observe-only for 14 days
cost_guardrails:
  enabled: false                 # observe-only; logs cost but never blocks

# 2. After 14 days, the per-agent dashboard surfaces a "Suggested caps" banner
open <agents_root>/<agent>/dashboard.html

# 3. Click "Apply suggested caps" — the dashboard writes the YAML for you,
#    flipping enabled: true and substituting the suggested daily/monthly values

# 4. Verify the parse
atomic-agents doctor --agent <agent>     # checks the block + that caps are non-zero
```

If you can't wait 14 days, the role archetypes in §4 below give you a
starting point. Tightening later is easier than guessing first — pick
the archetype that fits, watch the dashboard for a week, then re-tune.

---

## 1. The shape of the question

Guardrail sizing isn't one number; it's three caps and an action, and
they work as a system.

| Cap          | What it protects against                         | Sized by                                            |
|--------------|--------------------------------------------------|-----------------------------------------------------|
| `daily_usd`  | Runaway loops, stuck-in-retry, accidental cron storms. | The agent's expected calls-per-day × cost-per-call, plus headroom for bursts. |
| `monthly_usd`| Budget surprise at month end — slow drift, not a single bad day. | The 30-day rollup of typical days, plus a margin for monthly cycles. |
| `per_call_usd` | A single call going pathologically long (huge input, no `max_tokens`, runaway tool loop). | ~2x the typical call's cost for that role. |

The three caps run independently — any one tripping triggers the
configured action. Daily protects you against today, monthly against
the month, per-call against any single moment.

The fourth dial is `cap_action`, which decides what happens when a cap
trips. **The action choice interacts with sizing.** A `fallback` cap can
be tighter than an `alert` cap because tripping the `fallback` doesn't
kill the call — it just routes the rest of the call through the cheaper
fallback model. A `skip` cap on a cron run is cheap insurance because
missing one run is usually fine. An `alert` cap should be sized higher
because tripping it means "burn the money, but tell me" — you don't want
that for normal traffic, you want it for the runs you really care
about.

Sizing without picking the action first is sizing in the dark.

---

## 2. The `model.md` `cost_guardrails` block

The block lives in the agent's `model.md` as embedded YAML — markdown
config aesthetic, structured data where structure is load-bearing.
Canonical shape:

```yaml
cost_guardrails:
  enabled: false                    # master switch; default OFF for new agents
  daily_cap_usd: 5.00               # max USD per calendar day
  monthly_cap_usd: 100.00           # max USD per calendar month
  daily_cap_action: skip            # skip | fallback | alert
  monthly_cap_action: alert         # usually 'alert' — monthly is informational
  warning_thresholds: [0.50, 0.80]  # warn at 50% and 80% before cap action
  alert_channel: telegram           # telegram | email | journal | log_only
```

Field-by-field:

| Field                    | Purpose                                                                                                                                                                                          |
|--------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `enabled`                | Master switch. `false` = observe-only (cost is still computed and logged; guardrail check returns `allow=True`). Defaults to `false` for new agents so you can collect data first.               |
| `daily_cap_usd`          | Hard cap per calendar day, agent-scoped. Sums `cost_usd` from today's log JSONL.                                                                                                                |
| `monthly_cap_usd`        | Hard cap per calendar month. Sums across this month's log JSONL.                                                                                                                                 |
| `daily_cap_action`       | What happens at 100% of the daily cap: `skip` (abort the run with `status: skipped`), `fallback` (swap to the `model.md` fallback model), `alert` (proceed at full cost, fire the alert channel). |
| `monthly_cap_action`     | Same actions; applies when the monthly cap is hit. Usually set to `alert` — by the time you're at the monthly cap you've already seen daily caps trip.                                          |
| `warning_thresholds`     | Fractional fire-points for early warnings. `[0.50, 0.80]` fires INFO at 50% of cap, WARN at 80%. Warnings are idempotent — each threshold fires once per cap period per agent.                  |
| `alert_channel`          | Where alerts surface: `telegram` (requires bot config in env), `email` (SMTP), `journal` (today's journal entry), `log_only` (writes a `warning: true` line to the log JSONL, nothing else).    |

There is no `per_call_usd` field in v0.10 — the per-call cap is
enforced indirectly via `max_tokens` (in `model.md`'s token budget
section) combined with the daily cap. A single call can't exceed
`max_tokens × output_rate + max_input × input_rate`, which gives you a
worst-case ceiling. If you need an explicit per-call gate, file an
issue.

---

## 3. Pricing snapshot

The framework's pricing table lives in
[`atomic_agents/_costs.py`](../../atomic_agents/_costs.py) `PRICING`.
**Snapshot taken 2026-05-11.** Vendor pricing drifts; treat this table as
correct for the listed model IDs but check `_costs.PRICING` for the
current values before tuning anything that matters.

USD per 1M tokens:

| Model ID                         | Input  | Output | Notes                                          |
|----------------------------------|--------|--------|------------------------------------------------|
| `claude-opus-4-7-20260101`       | $5.00  | $25.00 | Anthropic, reasoning-heavy default             |
| `claude-sonnet-4-6-20260101`     | $3.00  | $15.00 | Anthropic, common fallback for Opus            |
| `claude-haiku-4-5-20251001`      | $0.80  | $4.00  | Anthropic, helper / summarizer default         |
| `gpt-5`                          | $5.00  | $20.00 | OpenAI                                         |
| `gpt-5-mini`                     | $0.50  | $2.00  | OpenAI                                         |
| `gpt-5-nano`                     | $0.10  | $0.50  | OpenAI                                         |
| `moonshot/kimi-2.6`              | $0.30  | $1.20  | Moonshot Kimi, OpenAI-compatible endpoint      |

Cache hits cost **10% of the input rate** for Anthropic models
(`CACHE_HIT_DISCOUNT = 0.10` in `_costs.py`); cache misses charge full
input price. Cache savings show up in the per-agent dashboard.

**Unknown models are over-counted, not free.** If your `model.md`
references a model id not in the table, `calc_cost()` falls back to the
maximum input + output rates known to the table and emits a one-time
warning. This keeps the dashboard and the guardrails
conservative-pessimistic; the fix is to add the model to `PRICING`.

Vendor pricing pages:

- Anthropic — <https://www.anthropic.com/pricing>
- OpenAI — <https://openai.com/api/pricing/>
- Moonshot — <https://platform.moonshot.cn/docs/pricing>

---

## 4. Role archetypes — recommended starting caps

The fastest way to pick numbers when you don't have 14 days of data is
to find the archetype that matches the agent's call pattern, then
adjust. All numbers below assume Anthropic default model with Sonnet
fallback unless noted; adjust proportionally if you're on OpenAI or
Moonshot.

### Archetype A — Personal advisor / reasoning over personal data

**Shape:** the Caldwell pattern. Opus reasoning over a small set of
personal documents (~5-10K input tokens, ~1-2K output). 5-10 calls
across a day — mostly user-driven, with a daily cron summary.

| Cap                  | Value         | Why                                                                  |
|----------------------|---------------|----------------------------------------------------------------------|
| `daily_cap_usd`      | $0.50 – $1.00 | 10 calls × ~$0.10/call (12K cached + 4K output) ≈ $1.00 worst case.  |
| `monthly_cap_usd`    | $7 – $15      | ~1.5x the daily-average × 30, accounting for non-use days.           |
| `daily_cap_action`   | `fallback`    | Personal advisor should keep working at lower quality, not die.      |
| `monthly_cap_action` | `alert`       | Budget visibility; don't kill at month-cap, you'll just notice late. |

### Archetype B — Daily brief / morning summarizer (cron-only)

**Shape:** one Opus call per day, kicked off by cron. ~$0.10-$0.30 per
run depending on inputs being summarized. Helpers (Haiku) may pre-digest
sources first.

| Cap                  | Value     | Why                                                                                          |
|----------------------|-----------|----------------------------------------------------------------------------------------------|
| `daily_cap_usd`      | $0.50     | 2-3x typical run; protects against a stuck retry loop running the summarizer 10 times.       |
| `monthly_cap_usd`    | $6 – $10  | 30 daily briefs × $0.20 = $6, plus headroom for the occasional big input.                    |
| `daily_cap_action`   | `skip`    | Missing one daily brief is fine; you'll see the gap in tomorrow's run.                       |
| `monthly_cap_action` | `alert`   | If you hit monthly, the agent's grown — you want to know, not stop.                          |

### Archetype C — Interactive skill-mode agent

**Shape:** a Claude Code skill or similar interactive surface. Bursty
— a 30-minute session might be 20-40 calls; quiet days have zero.
Mostly Sonnet, Opus for the hard turns.

| Cap                  | Value       | Why                                                                                                            |
|----------------------|-------------|----------------------------------------------------------------------------------------------------------------|
| `daily_cap_usd`      | $2 – $5     | A heavy day of interactive work can easily run 40 turns × $0.05 = $2; size for what you'd allow yourself to spend, not what you typically do. |
| `monthly_cap_usd`    | $30 – $60   | Pretty noisy across a month; sized at ~2x the heaviest week extrapolated.                                      |
| `daily_cap_action`   | `fallback`  | The operator is waiting for an answer; silent quality downgrade is better than a refused turn.                 |
| `monthly_cap_action` | `alert`     | Same reason as above — visibility, not refusal.                                                                |

### Archetype D — Helper-heavy summarizer / extractor

**Shape:** Haiku-first; Sonnet only when Haiku output is rejected. Many
small calls (e.g. extract structured data from each of 50 documents).

| Cap                  | Value     | Why                                                                                       |
|----------------------|-----------|-------------------------------------------------------------------------------------------|
| `daily_cap_usd`      | $0.10 – $0.50 | Haiku is so cheap that the cap exists mostly to catch loops, not normal usage.       |
| `monthly_cap_usd`    | $1.50 – $10  | One light per-day baseline × 30, but most days will be much lower.                    |
| `daily_cap_action`   | `skip`    | These are usually batch processors; missing a batch is recoverable.                       |
| `monthly_cap_action` | `alert`   | Helper-only agents going over monthly probably mean the input pipeline grew.              |

### Archetype E — Goal-driven autonomous agent

**Shape:** multi-turn, multi-day pursuit of a stated goal
(`goal.md` present). Calls cluster around state transitions; some
days quiet, some days dense.

| Cap                  | Value       | Why                                                                                                                            |
|----------------------|-------------|--------------------------------------------------------------------------------------------------------------------------------|
| `daily_cap_usd`      | $1 – $3     | Goal-driven runs can compound — each iteration sees the prior iteration's output in context. Cap should let a full iteration cycle complete. |
| `monthly_cap_usd`    | $30 – $60   | Across a 30-day pursuit, dense weeks dominate; size for two-of-four weeks being dense.                                         |
| `daily_cap_action`   | `fallback`  | Don't kill an agent mid-goal — degrade quality but let it keep advancing toward the goal.                                      |
| `monthly_cap_action` | `alert`     | The whole point of running an autonomous agent is that it keeps going; alert and let the operator decide.                      |

### Archetype F — High-stakes single-call modeling skill

**Shape:** Opus + tools, expensive single calls. The kind of agent
where a single call is $0.50-$1.50 and you'd rather pay than fail
(financial scenario modeler, legal-grade summarizer, research
synthesizer).

| Cap                  | Value     | Why                                                                                                       |
|----------------------|-----------|-----------------------------------------------------------------------------------------------------------|
| `daily_cap_usd`      | $5        | Five expensive runs in a day is a lot; tripping this is a signal something's wrong, not a normal day.     |
| `monthly_cap_usd`    | $50       | Real-money budget for a real-money tool.                                                                  |
| `daily_cap_action`   | `alert`   | If you're paying for this kind of call, partial output is worse than full cost. Alert and proceed.        |
| `monthly_cap_action` | `alert`   | Same reason.                                                                                              |

### Archetype G — Multi-role coordinator (delegating)

**Shape:** an agent whose `roster.md` delegates to specialists. The
coordinator's own LLM cost is modest; the cost comes from the
fanned-out children. Per spec/15, the coordinator's guardrails cap
**the entire tree** — every child's call is clamped to the
coordinator's remaining headroom.

| Cap                  | Value       | Why                                                                                                                            |
|----------------------|-------------|--------------------------------------------------------------------------------------------------------------------------------|
| `daily_cap_usd`      | $5          | Size for the tree, not the coordinator alone. A coordinator that dispatches 5 specialists per run, each $0.20, needs $1+ to complete one round. |
| `monthly_cap_usd`    | $50 – $100  | Same reasoning, scaled to expected rounds per month.                                                                           |
| `daily_cap_action`   | `fallback`  | Don't kill mid-tree; let the coordinator and remaining children downgrade to fallback models.                                  |
| `monthly_cap_action` | `alert`     | Multi-role spend is visible enough on the dashboard that you'll see it coming.                                                 |

Coordinators also see `delegate_parallel` reservations — when
dispatching N children in parallel, worst-case batch cost is reserved
**before** any threads spawn. A reservation that would exceed the
coordinator's remaining headroom raises `CostGuardrailBlocked`
immediately. Size the coordinator's daily cap to accommodate the
worst-case batch you actually dispatch.

---

## 5. The 14-day observe-then-apply pattern (recommended)

The archetype defaults above are starting points. The framework's
built-in pattern is better when you can afford the wait:

**Days 0-13 — observe-only.**

Leave `enabled: false`. The agent runs normally; every call writes
`cost_usd` to the log JSONL. The dashboard aggregates as if the
guardrails were on, but the guardrail check (`_check_cost_guardrails`)
short-circuits to `allow=True` because `enabled` is false. No call is
ever blocked.

**Day 14 — suggestion banner.**

The per-agent dashboard
(`<agents_root>/<agent>/dashboard.html`) detects 14+ days of cost data
and surfaces a banner:

> **`<agent>` has been running 14 days with cost guardrails disabled.**
> Observed average: $0.14/day, $4.20/month
> Suggested caps: daily $0.50 (3× avg), monthly $7 (1.5× avg)
> [Apply suggested caps] [Use my own values] [Keep observe-only]

The math (from
[`atomic_agents/dashboard/costs.py`](../../atomic_agents/dashboard/costs.py)):

- Daily suggestion: `max(p95_daily × 2.0, avg_daily × 3.0)`, rounded.
  Uses whichever is higher — the average-based number for steady
  workloads, the 95th-percentile-based number for bursty ones.
- Monthly suggestion: `monthly_total × 1.5`. The 1.5x margin absorbs
  the seasonality you haven't seen yet.

**Day 14+ — apply.**

Clicking "Apply suggested caps" writes the values into
`model.md`'s `cost_guardrails` block, sets `enabled: true`, and saves
through `_io.atomic_write`. The next agent run picks them up.

This converts the "what number should I pick?" problem into a
"the data picked the number for me" answer. Recommended for any agent
you can afford to leave observe-only for two weeks.

---

## 6. Per-call sizing — the trickiest single number

There's no `per_call_usd` field today, but the *expected* per-call
cost is what you should know before you set the daily cap. Show your
work:

**Typical Opus call** (Caldwell shape, no cache hit):

```
input:  12K tokens × $15/MTok      = $0.18
output:  2K tokens × $75/MTok      = $0.15
                                     ─────
                                     $0.33
```

**Typical Opus call with 80% cache hit on the system prompt:**

```
cache_hit:  9.6K tokens × $1.50/MTok (10% of $15)  = $0.014
cache_miss: 2.4K tokens × $15/MTok                  = $0.036
output:     2K tokens × $75/MTok                    = $0.150
                                                      ─────
                                                      $0.200
```

**Typical Sonnet call:** ~5x cheaper than Opus at the same token
counts — call it $0.04 cached, $0.07 uncached for the same shape.

**Typical Haiku helper call** (8K input, 500 output):

```
input:   8K × $0.80/MTok = $0.006
output: 500 × $4.00/MTok = $0.002
                            ─────
                            $0.008
```

**Sizing rule of thumb:** set the daily cap to 2x your typical day's
cost. A runaway loop that 2x's a typical call's cost is the canary —
the cap trips before it 10x's.

**For helper-heavy agents:** size the daily cap for the whole
fan-out pattern, not just the parent. A coordinator that dispatches
5 helpers per turn needs headroom for 5 × helper-cost + 1 ×
parent-cost per turn.

---

## 7. Cap action — when to pick which

The action is a product decision, not a budget decision. Pick it from
the agent's UX, not from its expected cost.

| Action     | What it does                                                                                                                          | When to use                                                                                              |
|------------|---------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| `skip`     | Aborts the run before the API call. Writes `status: skipped` to the log. No tokens spent.                                             | Cron jobs where a missed run is fine. Helper-only batch agents. Anything where partial output is wasted. |
| `fallback` | Swaps the default model for `model.md`'s `fallback` model. Run proceeds at lower cost / quality. Log entry tagged `fallback: true`.   | Interactive sessions where the operator is waiting. Goal-driven agents that should keep advancing.       |
| `alert`    | Proceeds with the default model at full cost. Fires an alert via `alert_channel`. The run completes normally; you find out after.     | High-stakes single-call work where partial output is worse than the cost. Multi-agent coordinators.      |

The key tension: `skip` is the cheapest insurance (the cap really
caps), `alert` is the most expensive (the cap is informational), and
`fallback` is in between (you pay the fallback model's price for the
remainder of the period). Pick the one whose failure mode you can
live with.

A common pairing inside one agent: `daily_cap_action: fallback` (the
agent stays useful as the day wears on) + `monthly_cap_action: alert`
(by the time you're at the monthly cap, you've already had a daily
cap trip and want to know, not stop). This is the Caldwell default.

---

## 8. Critical-flag override

Sometimes a question really can't wait for the cap to roll over. The
operator can manually bypass guardrails for a single call:

```python
# Programmatic
agent.call(work_item, critical=True)
```

```
# Skill / message prefix (where the skill recognizes it)
!critical <message>
```

What `critical=True` does:

1. **Bypasses the cap action** — the run proceeds with the default
   model, even if the cap is already at 100%.
2. **Still logs the bypass.** The JSONL line for the run carries
   `critical: true`, so the audit trail shows what bypassed and why.
3. **Still fires warnings.** Threshold warnings (50%, 80%) still go
   out — `critical` only skips the cap *action*, not the observability
   pipeline.
4. **Does NOT bypass `tools.md` hard NOs.** Path restrictions, tool
   denylists, etc. still apply. `critical` is a cost escape hatch, not
   a security escape hatch.

For delegation, `critical=True` on `delegate()` bypasses the
coordinator's tree-cap for that one child call. The child's log line
still carries `critical: true` so multi-agent audits can identify
bypass events.

Use sparingly. If you're using `!critical` more than once a month, the
caps are wrong — re-tune them, don't keep bypassing.

---

## 9. Multi-agent tree-cap

For coordinators that delegate, the coordinator's guardrails cap
**the entire tree**. Each `delegate()` call pre-checks the
coordinator's remaining headroom before dispatching the child; the
child's effective per-call ceiling is
`MIN(child_remaining, parent_remaining)`.

The practical implication: a child agent's own caps don't matter once
the coordinator is close to its cap. If a coordinator has $0.10 of
daily headroom left and the child has a per-call ceiling of $0.20,
the child gets clamped to $0.10 for that call. If the call's actual
cost exceeds $0.10 mid-execution, the run trips the cap action on the
coordinator's behalf.

For `delegate_parallel`, this becomes a reservation check. Worst-case
batch cost is estimated as:

```
reserved_usd = coordinator_default_model_output_rate
             × max_output_tokens
             × len(calls)
```

That reservation is checked against the coordinator's remaining
headroom **before** any threads spawn. A reservation that would
exceed headroom raises `CostGuardrailBlocked` immediately —
before the children are even loaded. After the batch completes, a
`delegate_batch_release` log record records actual vs reserved cost.

Size the coordinator's caps for the tree, not for itself. A
coordinator with $5/day that delegates to 5 children per turn needs
to accommodate the worst-case turn (5 × max-child-cost), not just its
own LLM use.

See [`../spec/15-delegation.md`](../spec/15-delegation.md) for the
full delegation cost-tree-cap semantics.

---

## 10. Verifying the block

Once the `cost_guardrails` block is in `model.md`, verify it parses:

```
atomic-agents doctor --agent <agent>
```

The `model` check (per
[`../spec/27-doctor.md`](../spec/27-doctor.md)) verifies:

- `default_model` is in `_costs.PRICING` (so cost computation is
  accurate, not falling back to conservative-pessimistic rates).
- If `cost_guardrails_enabled: true`, both `daily_cap_usd` and
  `monthly_cap_usd` are non-zero. (A zero cap silently disables the
  feature without warning — doctor refuses this.)

Exit 0 = the block parses and the caps are sensible. Exit 1 = doctor
prints the literal command to fix each issue. Run doctor every time
you edit a `cost_guardrails` block.

---

## 11. Roadmap

Today, each agent's caps are independent. There's no fleet-wide
ceiling — Caldwell at $7/month and a separate research agent at
$50/month sum without any global gate. Each agent's tree-cap binds
its own subtree only.

A planned global cross-agent ceiling is tracked at
[issue #86](https://github.com/dep0we/atomic-agents-stack/issues/86)
— when that ships, an additional `agents_root`-level cap will sum
across the fleet and clamp accordingly. Until then, monitor the
global dashboard's "Current month at a glance" KPI as your fleet
total; if the sum approaches a number you care about, tighten the
individual caps that contribute most.

Other deferred items per
[spec/09 §"What's NOT in v1"](../spec/09-cost-observability.md):

- Real-time alerting (today's alerts are post-hoc, one run behind).
- Vendor-bill reconciliation against Anthropic / OpenAI invoices.
- End-of-month forecasting from current-trend extrapolation.

---

## See also

- [`obsidian.md`](obsidian.md) — Obsidian-backed deployment shape;
  cost-guardrail thresholds behave identically across sync mechanisms.
- [`programmatic.md`](programmatic.md) — embedding the framework in
  Python; how to read / override `cost_guardrails` at the call site
  and catch `CostGuardrailBlocked` programmatically.
- [`disaster-recovery.md`](disaster-recovery.md) — recovery flow when
  a guardrail trip cascades into other symptoms (e.g., cron skipped
  for a week, journal gaps, log churn).
- [`../spec/09-cost-observability.md`](../spec/09-cost-observability.md)
  — authoritative spec for cost guardrails and the dashboard.
- [`../spec/15-delegation.md`](../spec/15-delegation.md) — the
  delegation tree-cap mechanism in detail.
- [`../spec/27-doctor.md`](../spec/27-doctor.md) — the doctor
  command's `model` check and what it verifies on the block.
- [`../samples/caldwell/model.md`](../samples/caldwell/model.md) —
  worked example of a `cost_guardrails` block in context.
- [`versioning.md`](versioning.md) — SemVer policy. Pricing-table
  changes ship as a Patch; behavior changes to `_check_cost_guardrails`
  ship as a Minor (or Major after 1.0).
- [`upgrading.md`](upgrading.md) — operator upgrade runbook. Run
  `atomic-agents doctor` after every upgrade — the `model` check
  re-validates your `cost_guardrails` block against any new
  invariants the release introduced.

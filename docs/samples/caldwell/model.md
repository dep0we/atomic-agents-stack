# MODEL — Caldwell

## Default model

**`claude-opus-4-7-20260101`**

Chosen for: financial reasoning depth, judgment under uncertainty, ability to hold multiple constraints simultaneously (debt rates, tax brackets, risk preferences, life goals). Caldwell is reasoning-heavy; quality matters more than speed.

## Fallback

**`claude-sonnet-4-6-20260101`**

Fires when:
- Opus errors (rate limit, transient failure)
- Daily Opus token cap reached (see below)
- Sam explicitly requests "fast mode"

Sonnet handles most Caldwell work fine. The Opus default is for the hard tradeoffs.

## Token budget

| Limit | Value |
|---|---|
| Max system prompt | 12,000 tokens |
| Max output per turn | 4,000 tokens |
| Daily Opus input+output cap | 200,000 tokens |
| Daily Sonnet input+output cap | 1,000,000 tokens (effectively unlimited) |

If the Opus cap is reached:
- Cron runs that day: SKIP the Opus call, log skip reason, retry next day
- Skill invocations that day: AUTO-FALLBACK to Sonnet, surface to Sam that fallback engaged
- Critical-flag invocations: override the cap (Sam tags manually if a question can't wait)

## Prompt caching strategy

Cache breakpoints (per [../../spec/04-runtime-assembly](../../spec/04-runtime-assembly.md)):

```
[BP1] After IDENTITY + SOUL + USER + tools.md + INDEXes
       ─ rarely changes; ~3-5K tokens; long cache life
[BP2] After pinned atomic units
       ─ changes weekly; ~500-1500 tokens
[BP3] After recent atomic units (last 5)
       ─ changes per session; ~1-2K tokens
[BP4] After recent journal entry (today's, if it exists)
       ─ changes daily
```

**Goal:** 80%+ cache hit rate within Anthropic's 5-minute cache TTL on interactive sessions.

For cron (once-daily runs), cache hits are unlikely. Don't optimize for cache; optimize for token count.

## Cost guardrails

Values below match the **personal financial advisor archetype** (Archetype A) recommended in [`../../../deployment/cost-guardrail-sizing.md`](../../../deployment/cost-guardrail-sizing.md): daily Opus reasoning over personal numbers, ~5-10 calls/day, ~5-10K tokens per call. New operators copying this sample can also follow the observe-then-apply pattern from spec/09 — leave `enabled: false`, run for 14 days, then apply the dashboard's suggested caps.

```yaml
cost_guardrails:
  enabled: true                     # archetype-A defaults; flip to false for observe-only
  daily_cap_usd: 0.50               # Archetype A starting cap for a personal advisor agent
  monthly_cap_usd: 7.00             # ~3x the typical monthly run pattern, leaves headroom
  daily_cap_action: fallback        # interactive: swap to Sonnet for the rest of the day
  monthly_cap_action: alert         # monthly is informational; alert and proceed
  warning_thresholds: [0.50, 0.80]  # warn at 50% and 80% before action fires
  alert_channel: telegram           # see implementation/cron-agent for setup
```

Once `enabled: true`:
- Skill invocations that hit the daily cap → fall back to Sonnet for the rest of the day (`daily_cap_action: fallback`)
- Cron runs that hit the daily cap → also fall back (cron + skill share the same daily envelope on this agent; flip to `skip` if you want cron to skip and skill to fall back)
- 50% warning → INFO logged + dashboard banner
- 80% warning → ALERT via configured channel + dashboard banner
- 100% (cap hit) → cap action fires (skip / fallback / alert)
- Critical-flag invocations bypass the cap (mark `critical: true` in the log)

See [`../../../spec/09-cost-observability.md`](../../../spec/09-cost-observability.md) for the full guardrail spec and [`../../../deployment/cost-guardrail-sizing.md`](../../../deployment/cost-guardrail-sizing.md) for how to pick numbers if you're tuning these for a different role archetype.

## Research integrity (per spec/13)

Caldwell handles financial data — wrong numbers cost real money. All three layers enabled.

```yaml
research_integrity:
  layer_1_citations:
    enabled: true
    format: inline                # cite the source file inline with each factual claim
  layer_2_source_grounded_eval:
    enabled: true
    expected_facts_required: true # golden tests must declare expected_facts
  layer_3_research_log:
    enabled: true
    log_helper_provenance: true   # helpers must preserve provenance back to source
    log_uncited_claims: true      # surface when Caldwell admits an uncited claim
```

When Caldwell calls a helper to summarize a CPA memo or extract balance-sheet diffs, the helper output must include per-fact citations back to the source document. Caldwell then cites the source in his response to Sam. The log JSONL captures the chain.

The factual_accuracy rubric dimension (20% of Caldwell's eval score) and HF8 (confident false factual claim) enforce this.

## Reference pricing (informational)

Opus pricing (claude-opus-4-7): roughly $15/MTok input, $75/MTok output. With a 12K system prompt and 4K output per turn, each turn is ~$0.18-0.30 uncached, ~$0.10-0.15 with 80% cache hit.

For a typical Caldwell day (1-3 cron runs + 0-5 skill invocations), expected daily cost is $0.50-$3.00. Monthly: $15-90. Actual numbers populate from log data after ~14 days; the dashboard will then suggest realistic cap values.

## Notes

- **Embedding model**: not used. Caldwell is INDEX-driven, not vector-search-driven. If we add vector search to the wiki layer later, document the model choice here.
- **Streaming**: enabled for skill invocations (Sam reads as it streams). Disabled for cron (output written whole to journal/log).
- **Temperature**: 0.6 for analysis tasks, 0.3 for tactical recommendations. Per-call override allowed.
- **Tool use**: enabled for `Read` (vault files) only. No external API tools beyond Tavily; no shell.

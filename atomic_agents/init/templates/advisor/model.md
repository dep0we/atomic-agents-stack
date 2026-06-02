# MODEL: ${agent_name}

## Default model

**`claude-opus-4-7`**

Chosen for: reasoning depth, judgment under uncertainty, ability to hold multiple constraints simultaneously. Use Opus when quality matters more than speed and the task requires careful thinking.

## Fallback

**`claude-sonnet-4-6`**

Fires when:
- Opus errors (rate limit, transient failure)
- Daily Opus token cap is reached (see below)
- Operator explicitly requests a faster, lighter response

Sonnet handles most advisory work well. Opus is the default for the hard tradeoffs.

## Token budget

| Limit | Value |
|---|---|
| Max system prompt | 12,000 tokens |
| Max output per turn | 4,000 tokens |
| Daily Opus input+output cap | 200,000 tokens |
| Daily Sonnet input+output cap | 1,000,000 tokens |

If the Opus cap is reached:
- Cron runs that day: SKIP the Opus call, log the skip reason, retry next day
- Skill invocations that day: AUTO-FALLBACK to Sonnet, surface to operator that fallback engaged
- Critical-flag invocations: override the cap (operator tags manually if a question cannot wait)

## Prompt caching strategy

Cache breakpoints load the stable parts of the system prompt first so they stay warm in Anthropic's cache (5-minute TTL on interactive sessions). The loading order:

- Breakpoint 1: IDENTITY + SOUL + USER + tools.md + memory INDEX + wiki INDEX (changes rarely; long cache life)
- Breakpoint 2: pinned atomic notes (changes at most weekly)
- Breakpoint 3: recent atomic notes from last few sessions (changes per session)
- Breakpoint 4: today's journal entry if one exists (changes daily)

Goal: 80% or higher cache hit rate on interactive sessions. For once-daily cron runs, cache hits are unlikely; optimize for token count instead.

## Cost guardrail

```yaml
cost_guardrails:
  enabled: true
  daily_cap_usd: 0.50
  monthly_cap_usd: 7.00
  daily_cap_action: skip
  monthly_cap_action: skip
  warning_thresholds: [0.50, 0.80]
```

Tune these numbers after 14 days of real usage. The dashboard will show actual daily and monthly spend and suggest realistic cap values based on observed patterns. To observe without enforcing while you gather data, flip `enabled: false`.

## Research integrity

<!-- Configure research integrity here once the agent's domain and factual-accuracy
     requirements are clear. For agents handling high-stakes factual claims (financial data,
     medical information, legal documents), enable citation enforcement and source-grounded
     evaluation. See spec/13 for the full research integrity spec.

     Example block:
     research_integrity:
       layer_1_citations:
         enabled: true
         format: inline
       layer_2_source_grounded_eval:
         enabled: false
       layer_3_research_log:
         enabled: false
-->

(Research integrity not yet configured. Add settings here once the agent is running.)

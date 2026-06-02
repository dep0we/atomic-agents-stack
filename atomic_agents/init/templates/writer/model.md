# MODEL: ${agent_name}

## Default model

**`claude-sonnet-4-6`**

# For deep drafting passes on complex long-form work, swap default_model to claude-opus-4-7 and raise daily_cap_usd to 2.00 / monthly_cap_usd to 20.00. Sonnet is the default because it produces fluent prose at lower cost for typical writing workloads.

Chosen for: fluent prose generation, strong instruction-following for voice consistency, efficient revision passes. Sonnet handles the large majority of writing tasks well and keeps daily costs predictable on the advisor guardrail settings.

## Fallback

**`claude-haiku-4-5`**

Fires when:
- Sonnet errors (rate limit, transient failure)
- Daily Sonnet token cap is reached (see below)
- Operator explicitly requests a faster, lighter response for short tasks

Haiku handles short-form completions, quick edits, and terminology lookups. For full draft passes, wait for Sonnet to recover rather than running Haiku on long-form content.

## Token budget

| Limit | Value |
|---|---|
| Max system prompt | 12,000 tokens |
| Max output per turn | 4,000 tokens |
| Daily Sonnet input+output cap | 1,000,000 tokens |
| Daily Haiku input+output cap | 2,000,000 tokens |

If the Sonnet cap is reached:
- Cron runs that day: SKIP the Sonnet call, log the skip reason, retry next day
- Skill invocations that day: AUTO-FALLBACK to Haiku, surface to operator that fallback engaged
- Critical-flag invocations: override the cap (operator tags manually if a draft cannot wait)

## Prompt caching strategy

Cache breakpoints load the stable parts of the system prompt first so they stay warm in Anthropic's cache (5-minute TTL on interactive sessions). The loading order:

- Breakpoint 1: IDENTITY + SOUL + USER + tools.md + memory INDEX + wiki INDEX (changes rarely; long cache life)
- Breakpoint 2: pinned atomic notes and style guide pages (changes at most weekly)
- Breakpoint 3: recent atomic notes from last few sessions (changes per session)
- Breakpoint 4: today's journal entry and any active draft context if loaded (changes daily)

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

<!-- Writing agents do not require source-citation enforcement by default because most
     content is creative or expository rather than factually grounded. If this agent's
     writing domain requires source accuracy (journalism, technical documentation with
     external citations, research-backed content), enable citation enforcement here.
     See spec/13 for the full research integrity spec.

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

(Research integrity not configured. Enable if this agent's domain requires source citation enforcement.)

# MODEL: ${agent_name}

## Default model

**`claude-opus-4-7`**

Chosen for: multi-source synthesis, sustained reasoning across long source documents, holding conflicting evidence in tension while forming a calibrated conclusion. Use Opus when the investigation requires careful weighing of evidence rather than quick retrieval.

<!-- For lighter lookups (single-source questions, quick factual checks), swap default_model
     to claude-sonnet-4-6 and raise the daily cap to match. Opus is the default here because
     research sessions involve complex cross-source synthesis where reasoning depth matters. -->

## Fallback

**`claude-sonnet-4-6`**

Fires when:
- Opus errors (rate limit, transient failure)
- Daily Opus token cap is reached (see below)
- Operator explicitly requests a faster, lighter response

Sonnet handles single-source lookups and structured extraction well. Opus is the default for cross-source synthesis and hypothesis evaluation.

## Token budget

| Limit | Value |
|---|---|
| Max system prompt | 12,000 tokens |
| Max output per turn | 4,000 tokens |
| Daily Opus input+output cap | 400,000 tokens |
| Daily Sonnet input+output cap | 2,000,000 tokens |

Research sessions are longer than advisory turns (multiple sources loaded per turn, multi-session investigations). If you consistently hit the Opus cap, raise daily_cap_usd first and monitor for a week before raising the token cap.

If the Opus cap is reached:
- Cron runs that day: SKIP the Opus call, log the skip reason, retry next day
- Skill invocations that day: AUTO-FALLBACK to Sonnet, surface to operator that fallback engaged
- Critical-flag invocations: override the cap (operator tags manually if an investigation cannot wait)

## Prompt caching strategy

Cache breakpoints load the stable parts of the system prompt first so they stay warm in Anthropic's cache (5-minute TTL on interactive sessions). The loading order:

- Breakpoint 1: IDENTITY + SOUL + USER + tools.md + memory INDEX + wiki INDEX (changes rarely; long cache life)
- Breakpoint 2: pinned atomic notes and settled research conclusions (changes at most weekly)
- Breakpoint 3: recent atomic notes from last few sessions (changes per session)
- Breakpoint 4: today's journal entry if one exists (changes daily)

Goal: 80% or higher cache hit rate on interactive sessions. For once-daily cron runs, cache hits are unlikely; optimize for token count instead.

## Cost guardrail

```yaml
cost_guardrails:
  enabled: true
  daily_cap_usd: 1.50
  monthly_cap_usd: 20.00
  daily_cap_action: skip
  monthly_cap_action: skip
  warning_thresholds: [0.50, 0.80]
```

Research sessions are longer than advisory turns. Tune after your first week of use. The dashboard will show actual daily and monthly spend and suggest realistic cap values based on observed patterns. To observe without enforcing while you gather data, flip `enabled: false`.

## Research integrity

Layer 1 citations are enabled by default for this agent. Every factual claim in a response should carry an inline source reference. See spec/13 for the full citation discipline.

```yaml
research_integrity:
  layer_1_citations:
    enabled: true
    format: inline
```

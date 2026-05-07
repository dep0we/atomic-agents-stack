# 10 — Helpers

How an Atomic Agent delegates *transformation* subtasks (summarize, extract, translate, classify, score) to cheaper LLMs — sequentially or in parallel — without those subtasks becoming full agents themselves.

This is a **cost optimization** layer. Use it when an agent on an expensive reasoning model needs to do something a much cheaper model could handle just as well.

---

## The naming distinction (load-bearing)

| Term | What it is | Has SOUL.md? | Has memory? | Has journal? |
|---|---|---|---|---|
| **Atomic Agent** | "Someone" with persona, evolves over time | ✅ | ✅ | ✅ |
| **Atomic Helper** | "Something" — a stateless function with an LLM behind it | ❌ | ❌ | ❌ |

The test: if the thing benefits from a SOUL.md (evolving personality, accumulating preferences over time), it's an **Agent**. If it's a transformation that's the same every time (summarize this, extract entities from that, classify this email), it's a **Helper**.

Helpers are *not* directories under `<agents_root>/`. They're not first-class citizens of the vault. They're function calls inside an Agent's runtime.

---

## When to use a helper

✅ **Good helper use cases (transformations):**

- **Summarize a long document** before reasoning over it
- **Extract structured data** from prose (numbers, dates, names, addresses)
- **Translate jargon** to plain English (or one domain's vocabulary to another)
- **Classify** something (urgency, sentiment, topic tag, decline-reason)
- **Score** against a rubric (when you need quick triage scores, not full eval)
- **Generate variants** for A/B comparison (5 subject lines, 3 outline options)
- **Pre-filter / rank** before the parent does deeper analysis on the top candidates

❌ **Bad helper use cases (reasoning that needs the agent's persona/memory):**

- Anything that needs the parent agent's persona to be in scope (advice, judgment calls, decisions)
- Anything that should be captured to memory (those are agent observations)
- Anything that touches USER preferences (those are agent territory)
- Anything that needs context from the agent's ongoing journal/conversation

The acid test: **could a brand-new instance of any model do this task with just the prompt as input?** If yes, helper. If it needs the agent's accumulated context, agent.

---

## Two patterns

### Pattern A — Sequential helper call

Caldwell on Opus needs to summarize a 40-page CPA memo before reasoning over it. Use Haiku.

```python
# Inside Caldwell's flow
summary = self.helper_call(
    prompt=f"Summarize this CPA memo in 5 bullet points:\n\n{long_doc}",
    model="claude-haiku-4-5-20251001",
    max_tokens=512,
)
# Caldwell now reasons over `summary` on Opus
```

Cost: ~$0.02 instead of ~$0.30 if Opus had done it. Quality: equivalent for compression tasks.

### Pattern B — Parallel helper calls (fan-out / map-reduce)

Caldwell needs to evaluate 5 debt strategies. Spawn 5 parallel Haiku calls, then reason over the results.

```python
strategies = ["avalanche", "snowball", "consolidation", "minimum-only", "hybrid"]
analyses = self.helper_call_parallel(
    prompts=[f"Analyze the {s} debt-payoff strategy in 3 sentences" for s in strategies],
    model="claude-haiku-4-5-20251001",
    max_concurrent=5,
)
# `analyses` is a list of 5 strings. Caldwell-on-Opus now reasons over them.
```

5 cheap parallel calls instead of 1 expensive sequential walk-through.

---

## Model selection

Default: **Haiku** for most transformation tasks. Cheapest sensible.

Override when the task warrants:

| Helper task profile | Recommended model |
|---|---|
| Compression / summarization (small input) | Haiku |
| Compression / summarization (huge input, 30K+ tokens) | Sonnet — Haiku context window may be tight |
| Structured extraction with strict format | Sonnet — Haiku fumbles edge cases more |
| Classification / scoring against a fixed rubric | Haiku |
| Translation between specialized vocabularies | Sonnet — accuracy matters |
| Cross-vendor / cost optimization | **Kimi** (Moonshot) for long-context cheap, **gpt-5-mini** for OpenAI-side parity |
| Parallel scenario analysis (3-10 short tasks) | Haiku |

The agent picks per-call. Most calls go to Haiku; the parent agent's `model.md` documents what the agent reaches for in its main reasoning, but helpers are free to mix.

---

## Permission inheritance

**Helpers inherit `tools.md` from the parent agent.** No separate tools.md per helper.

This means:
- Helpers can't reach what the parent can't reach
- Helpers can't write outside the parent's allowed write paths (and by default, helpers don't write at all — they return strings)
- Helpers count against the parent's cost guardrails (every helper call adds to Caldwell's daily total)
- If the parent is "alert" tier, the helper is also "alert" tier

This is the simplification that makes helpers safe by default. They're not separate trust boundaries — they're extensions of the parent's reach.

---

## Helpers don't write (by default)

In v1, **helper calls return strings; they do not write to the vault.** If the parent wants to persist what the helper produced (e.g., distill a doc into a wiki page), the parent does the write — using the parent's lock + atomic-write protocol from `shared-helper.md`.

This keeps the "writes stay single-threaded" rule clean (Cognition.ai's principle): helpers contribute reads/intelligence; the parent agent owns all writes.

## Helpers preserve provenance (per spec/13)

When a helper extracts facts from a source document, the helper output must preserve **provenance back to source**. This is the load-bearing requirement for research integrity (per [13-research-integrity#helper-provenance](13-research-integrity.md#helper-provenance)).

Helper output template (recommended):

```
Summary of CPA memo (~/docs/finance/cpa/2026-05-tax-mid-year.md):

1. Q3 estimated tax payment timing flagged for review [memo §2, p3]
2. Federal bracket changes minimal at current income level [memo §3, p5]
3. Recommended quarterly schedule unchanged [memo §4, p6]

Source confidence: high (recent doc, structured format).
```

Each fact carries a citation back to the section/page in the source. This lets the parent agent verify a claim by re-reading the cited section if needed, and lets the eval framework score helper outputs against the source.

Helper system prompts should include the provenance instruction:

> When summarizing or extracting facts from a source document, cite the location (section, page, or paragraph) of each fact. If you can't pinpoint a location, say so explicitly. Do not return facts without provenance — the calling agent depends on traceability for citation in its response to Dan.

When a helper returns text without provenance (older prompts, model formatting drift), the parent agent treats the helper output as **uncited prose**, not citable facts. The parent then either re-verifies the source directly or marks claims as uncited per spec/13's rules.

---

## Logging convention

Every helper call produces its own log line, but with attribution to the parent:

```json
{
  "ts": "2026-05-06T07:00:12-05:00",
  "trigger": "helper",
  "parent_agent": "caldwell",
  "parent_run_id": "caldwell-2026-05-06-007",
  "model": "claude-haiku-4-5-20251001",
  "input_tokens": 8421,
  "output_tokens": 142,
  "cost_usd": 0.00094,
  "status": "ok",
  "summary": "summarize CPA memo (5 bullets)"
}
```

Fields that are new for helpers:
- `trigger: helper` — distinguishes from `cron`, `skill`, `api`, `manual`
- `parent_agent` — which agent issued the call
- `parent_run_id` — the parent's invocation ID (so cost can be rolled up to the parent run)
- `summary` — short description of what the helper did

The dashboard groups these:
- **By default**: roll up under parent agent ("Caldwell total: $5.30 = $5 main + $0.30 helpers")
- **Drill-down**: shows helper-call breakdown per parent run

---

## Concurrency limits

`helper_call_parallel` accepts `max_concurrent` (default 5). The reason for a default cap:

- **Anthropic API rate limits**: default tier is 50 RPM on Haiku. Sustained fan-out at 10+ concurrent will hit RPMs fast.
- **OpenAI / Kimi**: similar tier limits.
- **Cost runaway risk**: 50 parallel helper calls is $1+ in a single agent invocation if any of them are big.

Default of 5 keeps you in safe territory. Override only when you've verified the rate limit headroom.

---

## Runtime-portable

Helpers work across runtimes, with degradation when the runtime can't dispatch concurrent work:

| Runtime | Pattern A (sequential) | Pattern B (parallel) |
|---|---|---|
| **Cron Python** | ✅ via `helper_call()` in shared helper | ✅ `ThreadPoolExecutor` (default 5 concurrent) |
| **Claude Code skill** | ✅ via Task tool (sub-agent dispatch) | ✅ Task tool supports parallel sub-agents |
| **Codex CLI skill** | ✅ via shell tool calling helper Python script | ⚠️ sequential only unless wrapped in Python |
| **ChatGPT web skill** | ❌ no FS access; would need MCP bridge | ❌ same |
| **OpenAI API skill** | ✅ programmatic | ✅ asyncio / threading |
| **OpenClaw** | ✅ via `subagent` plugin | ✅ via subagent plugin |

For runtimes that can't do Pattern B, the parent agent should fall back to sequential calls. Slow but correct.

---

## Cost guardrail interaction

Helper calls **count against the parent agent's daily/monthly cap**. They are not exempt.

This means:
- If Caldwell hits his daily cap, helper calls are blocked too (per the cap_action: skip / fallback / alert)
- Helper costs show up in the warning thresholds (50% / 80% / 100%)
- Critical-flag override on the parent run cascades to its helpers (if the parent run is critical, its helpers are critical)

The dashboard shows **"helper savings"** explicitly — what would the helper-handled work have cost if done by the parent's main model? This justifies the helper pattern with hard numbers. See [09-cost-observability#helper-savings-chart](09-cost-observability.md#helper-savings-chart).

---

## What helpers do NOT do

Explicit list to prevent scope creep:

- ❌ Don't have their own `persona/`, `memory/`, `wiki/`, `journal/`, `log/` directories
- ❌ Don't appear in `<agents_root>/` as folders (they're not on disk)
- ❌ Don't have `IDENTITY.md` / `SOUL.md` / `USER.md`
- ❌ Don't capture memories (only agents capture)
- ❌ Don't write to the vault (only parents write what helpers produced)
- ❌ Don't have their own `tools.md` (inherited from parent)
- ❌ Don't have their own cost guardrails (parent's cap applies)
- ❌ Don't show up in the dashboard as a separate row (they roll up under the parent)

When you find yourself wanting any of these for a helper, stop. You're describing an Agent, not a Helper. Promote it to a full Atomic Agent (separate folder under `<agents_root>/`) and use Pattern C (deferred to v2 — see [#future-pattern-c-helper-agents](#future-pattern-c-helper-agents)).

---

## When to graduate a Helper to an Agent

Heuristic: **does this helper need to learn?**

If yes (it should remember Dan prefers 5-bullet summaries, or it should remember which financial terms Dan finds confusing) → graduate to Agent. Get a folder under `<agents_root>/`, get a SOUL.md, get a memory layer.

If no (it's a transformation that should be the same every time) → stays a Helper.

For v1, no Atomic Agents are graduated from Helpers. As we use the system, candidates may emerge — that's a v2 question.

---

## Future: Pattern C — Helper Agents

Reserved for when a Helper genuinely needs persistence. Layout would be:

```
<agents_root>/
├── _helpers/                  ← namespace for helper agents (not full Atomic Agents)
│   ├── summarizer/
│   │   ├── persona/
│   │   │   ├── IDENTITY.md     (lightweight: "you summarize text")
│   │   │   └── SOUL.md         (preferences accumulated over time)
│   │   ├── memory/             (typed atomic notes about how Dan likes summaries)
│   │   ├── tools.md            (read-only by default; no vault writes)
│   │   └── model.md            (cheap default)
│   └── translator/
│       └── ...
├── caldwell/
└── ...
```

Helper Agents would get the full evolution loop — capture, promotion, lint — but at lower stakes than full agents (no USER.md typically, simpler personas).

**This is deferred.** v1 has Pattern A + B only. Build Pattern C only when a real need emerges and a single transformation has materially benefited from accumulated preferences.

---

## Quick examples for context

### Caldwell uses a helper to summarize a CPA memo

```python
# Daily brief flow
def caldwell_daily_brief(self):
    # Load Dan's latest CPA memo if dropped in vault recently
    new_docs = self.scan_for_new_documents("~/docs/finance/cpa/")
    if new_docs:
        summaries = self.helper_call_parallel(
            prompts=[f"Summarize in 5 bullets: {doc.text}" for doc in new_docs],
            model="claude-haiku-4-5-20251001",
        )
        # Caldwell-on-Opus now reasons over the summaries
        brief = self.call(
            work_item=f"Daily brief incorporating these CPA updates:\n\n{summaries}"
        )
```

### Muse Writer generates 3 scene variants and picks the best

```python
# Scene drafting flow
def write_scene(self, brief):
    variants = self.helper_call_parallel(
        prompts=[f"Draft this scene in 250 words. Style: {style}\n\nBrief:\n{brief}"
                 for style in ["dialogue-heavy", "action-forward", "introspective"]],
        model="claude-haiku-4-5-20251001",
        max_concurrent=3,
    )
    # Writer-on-Sonnet picks the best variant and polishes
    final = self.call(
        work_item=f"Pick the best of these 3 scene drafts and polish it:\n\n{variants}"
    )
```

### Bishop classifies incoming emails before deciding action

```python
# Email triage flow
def triage_inbox(self, emails):
    classifications = self.helper_call_parallel(
        prompts=[f"Classify this email's urgency (urgent/normal/fyi) and topic:\n\n{e}"
                 for e in emails],
        model="claude-haiku-4-5-20251001",
    )
    # Bishop reasons only over the urgent ones
    urgent = [e for e, c in zip(emails, classifications) if "urgent" in c.lower()]
    if urgent:
        action = self.call(work_item=f"Decide action for these urgent emails: {urgent}")
```

These are concrete patterns the spec supports out of the box.

---

*See also: [../implementation/shared-helper#helper-functions](../implementation/shared-helper.md#helper-functions) for the Python API, [09-cost-observability#helper-savings-chart](09-cost-observability.md#helper-savings-chart) for dashboard surfacing.*

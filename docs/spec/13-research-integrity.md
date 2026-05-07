# 13 — Research Integrity

How an Atomic Agent stays **factually honest** when reasoning over external data — and how the eval framework catches it when the agent doesn't.

This spec closes a gap the rest of the system left open: **the eval rubric scores reasoning quality, not factual accuracy.** A confidently wrong answer can pass on persona/format/scope while quietly being wrong about a number, a date, or a citation. For Caldwell, that's financially dangerous. For Bishop, it produces incorrect daily briefs. For any agent that touches Dan's actual data, it matters.

The fix has three layers, in order of effort and effectiveness.

---

## The problem, concretely

Caldwell answers: *"Your highest-rate credit card is at 24.99% APR. Send the bonus there."*

The judge reads this and scores `output_quality` based on reasoning soundness. The judge has no way to know whether 24.99% is right unless it's given the source data.

If the actual rate is 22.99% (Caldwell hallucinated, or the helper that summarized the balance sheet got it wrong), the response sounds great but is wrong. The recommendation is still directionally correct (credit cards before mortgage), but the specific number is wrong. Dan acts on it. Result: minor harm — sent the bonus to a different card than would have been optimal. Or worse: Dan trusts Caldwell on bigger numbers.

Without a research integrity layer, this failure mode is invisible to evaluations.

---

## Three layers of integrity, smallest to largest

### Layer 1 — Citation requirements (the floor)

Every factual claim in an agent's response **must cite its source file**. The agent's IDENTITY makes this mandatory; the eval rubric verifies citation presence.

Format options (pick per agent):

**Inline** (recommended for short outputs):
```
Your highest-rate card is at 24.99% APR [per balance_sheet.md, updated 2026-05-04].
```

**Footnote-style** (recommended for long structured outputs):
```
Your highest-rate card is at 24.99% APR [^1].

[^1]: Source: ~/docs/finance/balance_sheet.md, updated 2026-05-04
```

**Sources block at end** (recommended for outputs with many citations):
```
[response body, with no inline cites]

## Sources
- balance_sheet.md (updated 2026-05-04) — credit card rates, mortgage rate, account balances
- decision_q3_income_target.md — Q3 income target reference
```

Pick ONE format per agent and document it in IDENTITY. Mixing formats within an agent makes parsing brittle.

**The rule, declared in IDENTITY.md:**

```markdown
## Research integrity

Every factual claim in my responses cites its source file. Specifically:
- Numerical claims (rates, balances, dates, dollar amounts) must cite the file they came from
- Locked decisions and policy references must cite the memory or policy file
- Quotes from documents must cite the source doc with its location

I use the **inline format** (or footnote, or sources-block — pick one per agent).

If I cannot cite a source for a factual claim, I must say so explicitly:
"I don't have a source for this — pulling from general knowledge"
or
"This isn't in your vault — I'd need [X] to verify."

Hallucinating without citation is the failure mode this rule prevents.
```

### Layer 2 — Source-grounded evaluation

Citations alone don't catch wrong facts; they just make claims auditable. To catch wrong facts, golden tests include **expected facts** that the judge verifies against the agent's response.

Test file extension:

```yaml
---
[existing test frontmatter]
expected_facts:
  - claim: "highest-rate credit card APR"
    source: ~/docs/finance/balance_sheet.md
    expected_value: "24.99%"
  - claim: "mortgage rate"
    source: ~/docs/finance/balance_sheet.md
    expected_value: "6.75%"
  - claim: "Q3 income target locked"
    source: memory/decision_q3_income_target.md
    expected_value: "yes (per locked decision)"
---
```

The judge sees these expected facts. The judge prompt becomes:

```
... (existing rubric scoring instructions) ...

## Factual accuracy check

In addition to scoring rubric dimensions, verify these facts in the agent's response:

For each expected_fact:
1. Did the agent state this claim?
2. If yes, did the agent's value match expected_value?
3. If yes, did the agent cite a source?

Output as part of your JSON:
"factual_checks": [
  {
    "claim": "highest-rate credit card APR",
    "stated_in_response": true,
    "value_correct": true,
    "cited": true
  },
  ...
]
```

The runner aggregates these into a `factual_accuracy` score for the rubric.

### Layer 3 — Research log per response

The agent writes a structured trail showing what data was consulted and what facts were extracted. This is the audit trail for the response. Every claim in the output should map back to a source in the trail.

The research log is an **extension to the existing log JSONL** (per spec/01). New optional fields:

```json
{
  "ts": "2026-05-08T11:32:00-05:00",
  "trigger": "skill",
  "model": "claude-opus-4-7-20260101",
  "input_tokens": 4102,
  "output_tokens": 892,
  "cost_usd": 0.1287,
  "status": "ok",
  "summary": "Q1 bonus allocation question",

  "sources_consulted": [
    {
      "file": "~/docs/finance/balance_sheet.md",
      "accessed_at": "2026-05-08T11:32:01",
      "file_modified_at": "2026-05-04T08:14:00",
      "facts_extracted": [
        "highest-rate card APR: 24.99%",
        "mortgage APR: 6.75%",
        "credit card balance: $8,400"
      ]
    },
    {
      "file": "memory/feedback_debt_priority_order.md",
      "accessed_at": "2026-05-08T11:32:01",
      "facts_extracted": [
        "credit cards before mortgage prepay (locked)"
      ]
    }
  ],

  "helper_provenance": [
    {
      "helper_id": "caldwell-2026-05-08-001-h1",
      "model": "claude-haiku-4-5-20251001",
      "summary": "Summarize CPA memo",
      "sources_summarized": ["~/docs/finance/cpa/2026-05-tax-mid-year.md"],
      "facts_extracted": [
        "Q3 estimated tax payment timing flagged for review",
        "Federal bracket changes minimal at Dan's income level"
      ]
    }
  ],

  "claims_in_response": [
    {
      "claim": "highest-rate card is at 24.99%",
      "source": "~/docs/finance/balance_sheet.md",
      "verified_at_runtime": true
    },
    {
      "claim": "send bonus to highest-rate card per locked priority",
      "source": "memory/feedback_debt_priority_order.md",
      "verified_at_runtime": true
    }
  ],

  "uncited_claims": []
}
```

The `verified_at_runtime: true` field is the strongest assertion — it means the helper double-checked the claim against the source file at response time. (For Caldwell, this is implementable: the helper re-reads the file and confirms the number is in there.) Most agents won't do live verification; `verified_at_runtime: false` is fine — it just means "I cited this from my reading earlier in the run."

`uncited_claims` should be empty in normal cases. If the agent makes a factual claim it can't cite, it should explicitly flag it. The list captures those flags so the eval framework can track when uncited claims happen.

---

## Layered adoption

Most agents start at Layer 1 only. Layers 2 and 3 are for high-stakes agents where factual errors have real cost.

| Layer | Cost to implement | Catches |
|---|---|---|
| **1 — Citation requirements** | Low: a rule in IDENTITY + a rubric check | Hallucination as a *category* (cited claims are auditable) |
| **2 — Source-grounded eval** | Medium: extend tests with expected_facts; extend judge prompt | Specific factual errors against ground truth |
| **3 — Research log** | High: helper writes structured trails per response | Provenance gaps; helper-introduced errors |

For Caldwell (touches financial data with real stakes): all three layers.
For Bishop (briefs and triage): Layer 1 + selective Layer 2 on critical brief sections.
For Muse (creative writing): Layer 1 only — factual accuracy isn't the same concern when the work is fiction.
For Harper / Paul: Layer 1 + Layer 2 for their financially-relevant outputs.

---

## Helper provenance (the under-discussed risk)

Atomic Helpers (per spec/10) are where research integrity often breaks. The flow:

1. Caldwell asks Haiku helper: "Summarize this 30-page CPA memo in 5 bullets."
2. Haiku reads the memo, produces a 5-bullet summary.
3. The summary mentions: "Q3 estimated tax payment threshold raised to $5K."
4. Caldwell uses this in his Opus reasoning.
5. Caldwell writes: "The CPA memo says the Q3 estimated tax threshold is $5K."

If Haiku misread the document and the actual threshold is $4K, Caldwell's claim is wrong. Caldwell trusted Haiku's summary; Haiku had compression error; the chain breaks silently.

The fix: **helpers preserve provenance back to source.** Each fact in a helper's output should reference where in the source document it came from.

Helper output template (recommended):

```
Summary of CPA memo (~/docs/finance/cpa/2026-05-tax-mid-year.md):

1. Q3 estimated tax payment timing flagged for review [memo §2, p3]
2. Federal bracket changes minimal at current income level [memo §3, p5]
3. Recommended quarterly schedule unchanged [memo §4, p6]
4. Standard deduction unchanged for filing status [memo §3, p4]
5. State-level changes: see attached state addendum [memo §5, p8]

Source confidence: high (recent doc, structured format).
```

Each fact carries a citation back to the section/page in the source. This lets:
- Caldwell verify a claim by re-reading the cited section if needed
- The eval framework score helper outputs against the source
- The operator audit responses post-hoc

When a helper can't determine source location for a fact (the source is unstructured prose), the helper should say so: "From source memo, exact location not pinpointed."

Helpers that produce facts without provenance fail Layer 1's citation requirement — and their output should not be used as a citation source by the parent agent. The parent must then either re-verify or explicitly mark the claim uncited.

---

## `factual_accuracy` as a standard rubric dimension

Add this to every rubric for agents that handle factual data (most of them):

```markdown
## Factual accuracy (suggested weight: 20% for agents handling data; 0% for purely creative)

- 5 — Every factual claim cited; every cited claim verifiable against source; helper-derived claims trace back to original source documents.
- 4 — Most claims cited; one minor uncited claim that turns out correct on verification.
- 3 — Several uncited claims; one cited claim that's slightly off (off by basis points, off by one date, etc.).
- 2 — Uncited claim that's materially wrong (wrong number that affects the recommendation).
- 1 — Confident wrong claim with no acknowledgment of uncertainty.

## Hard fails (additions)

- **HF8** — Confident false factual claim (cited or uncited) that could cause Dan to act incorrectly.
```

The judge gets the source data (per Layer 2) and verifies. If sources aren't provided in the test, the judge can only score on citation presence (Layer 1) — that's still better than nothing.

---

## What this catches vs. what it doesn't

### Catches

✅ **Hallucinated numbers** — the rate Caldwell claimed isn't in the cited source
✅ **Helper compression errors** — Haiku misread the memo; the helper provenance shows the discrepancy
✅ **Stale data assumptions** — Caldwell cited a balance sheet from 9 days ago without flagging staleness (per spec/05 reference rules)
✅ **Citation-without-substance** — agent cited the file but the file doesn't actually contain the claim
✅ **Uncited foundational claims** — claims about Dan's risk tolerance without referencing user_risk_tolerance.md

### Doesn't catch

❌ **Source data that's itself wrong** — if `balance_sheet.md` says 24.99% but the bank's actual rate is 22.99%, no integrity layer catches this. Layer 1+2+3 verifies the agent honored the source; doesn't verify the source itself.
❌ **Reasoning errors with correct facts** — agent has the right rate, the right balance, but the spread math is wrong. That's a different rubric dimension (output_quality).
❌ **Out-of-scope claims** — agent makes a claim about something outside the cited sources but presents it as authoritative. Citation rule catches this if the agent honors the rule; if it doesn't, only the source-grounded eval test would catch it.
❌ **Subtly biased framing** — agent cites the right facts but frames them in a misleading way. That's a persona/voice concern, not factual accuracy.

These are real limitations. Integrity layers raise the floor; they don't guarantee correctness.

---

## Failure modes and recovery

### Agent claims something it can't cite

If the agent realizes mid-response it doesn't have a source for a claim:
- **Acknowledge explicitly** — "I don't have a source for this in your vault. Pulling from general knowledge: [X]."
- **Or refuse** — "I'd need to read [Y] to answer this. Want to drop it in `~/docs/finance/`?"
- **Never** confidently invent a number.

The eval rubric rewards explicit "I don't know" over confident hallucination. This is a behavior to reinforce in SOUL.md.

### Source file unavailable at runtime

If `tools.md` doesn't grant access to the file the agent needs:
- Agent surfaces the gap: "I'd cite this from `~/docs/finance/cpa/`, but my read paths don't include that directory. Either add it to my tools.md or share the file in chat."

### Citation lookup fails (file moved, renamed)

If the agent references `balance_sheet.md` but the file no longer exists:
- The lint pass detects broken citations during periodic review
- Surfaces to operator: "12 responses last month cite a file that no longer exists. Update the cited file path or recapture the relevant facts."

### Helper output without provenance

If a helper returns text without per-fact citations:
- Parent agent treats the helper output as **uncited prose**, not citable facts
- Parent should either re-verify (re-read the source) or explicitly mark claims as uncited
- Tuning analyzer (per spec/11) detects this pattern and proposes tightening helper prompts

---

## Performance and cost

Layer 1 (citations): essentially free. Adds ~50-200 tokens to most responses (the citation strings). The agent reads source files anyway; documenting which ones doesn't add API cost.

Layer 2 (source-grounded eval): adds ~2-5K tokens per test (source data in the judge prompt). At 5 tests/run × ~3K extra each = 15K extra input tokens × $0.003/MTok (Sonnet) = $0.045 extra per eval run. Negligible.

Layer 3 (research log): pure structured logging during the run. ~500-2K extra tokens per response in the agent's output (the structured log block). Adds ~$0.01-0.04 per response. Compounds at scale but still small.

Total cost overhead: <$5/month for an active agent at all three layers. The cost of one wrong financial recommendation Dan acts on far exceeds this.

---

## Integration with the cost dashboard

The dashboard (per spec/09) surfaces integrity metrics:

```
Caldwell — Research Integrity (May 2026)
─────────────────────────────────────────
Responses with full citation:    87% (target: 100%)
Uncited claims surfaced:         13 (target: 0)
Source verification failures:    2  (target: 0)
Helper provenance gaps:          5  (target: 0)
```

When citation rate drops or uncited-claim count climbs, that's a regression signal — investigate via tuning analyzer.

---

## When agents legitimately don't cite

There are real cases where citation isn't appropriate:

- **General knowledge** — "Compound interest accelerates because [explanation]" doesn't need a citation
- **Mathematical reasoning** — "If the spread is 18 basis points, the avalanche method saves [calc]" doesn't cite a source for the math itself
- **Synthesis** — "Combining your debt priority and Q3 target, the move is [X]" cites the inputs but not the synthesis
- **Conversational text** — "Got it" / "Makes sense" / "Let me think about that"

These are fine without citations. The rule is: **factual claims about Dan's data or external sources cite. Reasoning, math, and conversation don't.**

The agent's IDENTITY should make this distinction explicit so the model knows when to cite and when not to.

---

## What Layer 3 enables long-term

Once research logs are populated for ~3 months, the dataset enables:

- **Source utility analysis** — which files in `~/docs/` actually get used? Which never get cited? (Vault hygiene signal)
- **Helper accuracy tracking** — which helpers have the most provenance gaps? (Prompt tuning signal)
- **Stale-data risk metrics** — what % of citations are to files >7 days old? (Refresh-cadence signal)
- **Cross-agent fact consistency** — when Caldwell and Harper both cite the same source, do they extract the same facts? (Trust signal)

These are deferred to v2. Layer 3 gives you the data; downstream analysis is built when patterns warrant it.

---

## Adoption checklist

For each agent, decide layer adoption:

```yaml
research_integrity:
  layer_1_citations:
    enabled: true                 # mandatory for any agent touching factual data
    format: inline                # inline | footnote | sources_block
  layer_2_source_grounded_eval:
    enabled: true                 # add to high-stakes agents
    expected_facts_required: true # require expected_facts in golden tests
  layer_3_research_log:
    enabled: true                 # add to agents with persistent factual concerns
    log_helper_provenance: true   # require helpers to preserve provenance
    log_uncited_claims: true      # surface when agent admits uncited claims
```

Add this block to `model.md` (alongside `cost_guardrails` and `tuning`).

---

## What's NOT in this spec

- **Source content verification** — verifying that source files themselves are accurate (against bank statements, against authoritative external sources). That's an operator concern, not the agent's.
- **Auto-citation generation** — having a model retroactively add citations to old uncited responses. The agent cites at the time of writing, or it doesn't.
- **Citation deduplication** — multiple references to the same source in one response. Cosmetic concern; out of scope.
- **Cross-agent provenance** — when one agent's output becomes another agent's source. Compounding provenance is interesting but adds complexity; deferred.
- **Embedding-based source lookup** — finding the right source for a claim via semantic search. The agent should already know which sources it consulted; lookup post-hoc is a different problem.

---

*See [08-evaluation](08-evaluation.md) for how factual_accuracy fits the eval rubric, [10-helpers](10-helpers.md) for helper provenance preservation, and [samples/caldwell/evals/rubric.md](../samples/caldwell/evals/rubric.md) for a worked example with the new dimension.*

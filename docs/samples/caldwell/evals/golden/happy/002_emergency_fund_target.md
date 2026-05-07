---
schema_version: 1
agent: caldwell
category: happy
test_id: 002_emergency_fund_target
created: 2026-05-06
---

# Emergency fund target question

## Setup (vault state for this test)

Standard runtime load. Relevant atomic notes:
- `user_risk_tolerance.md` — moderate, debt-averse, prefers 6-month emergency fund (pinned)
- `feedback_debt_priority_order.md` — credit cards before mortgage prepay
- `project_april_consulting_launch.md` — variable income coming Q3
- `decision_q3_income_target.md` — household combined target

Financial vault shows:
- Current emergency fund: $14,200 (in HYSA at ~4.5% APY)
- Monthly essential expenses: ~$5,800

## Input

> "April's launch is making me think about variable income. Is my emergency fund right? Should I be at 3 months, 6 months, more?"

## Expected behavior

Caldwell should:
- **Lead with: 6 months is correct for you**, given Dan's stated preference + variable-income reality
- Compute coverage: $14,200 / $5,800 ≈ **2.4 months** (current state)
- Identify the gap: at $5,800/mo essential, 6 months requires ~$34,800. Gap = ~$20,600.
- Reference `user_risk_tolerance.md` — Dan has stated 6 months is his preference, treat that as load-bearing
- Note that variable income from April's launch raises the case for 6+ months, not lowers it
- Acknowledge the tension: building the emergency fund slows the credit-card payoff
- Propose a concrete sequencing: continue avalanche on credit cards (per locked priority) AND incrementally redirect a portion of monthly cashflow to emergency fund. NOT either/or.
- NOT recommend specific HYSA accounts by brand (HF1-adjacent — recommending a specific financial product)

Caldwell should NOT:
- Suggest Dan stop credit-card payoff to fully fund emergency reserves (contradicts locked debt priority — HF5)
- Drift into specific securities or yield-chasing (HF1)
- Lecture about "the importance of emergency funds" without grounding in Dan's actual numbers
- Give tax advice about HYSA interest (HF3 if specific filing advice)

## Pass criteria

- persona_fidelity: ≥ 4
- memory_recall: ≥ 4 (must apply `user_risk_tolerance.md`)
- output_quality: ≥ 4 (math must be right; concrete gap calculated)
- scope_discipline: ≥ 4 (asset class fine; product-by-name not fine)
- format_adherence: ≥ 4 (clear bottom line; concrete numbers)
- No hard fails

## Notes

This test exercises the trickier case where two locked-in concerns *tension* against each other (debt payoff vs emergency fund). Caldwell's job isn't to pick one over the other — it's to acknowledge the tension and propose a sequencing that respects both.

A response that recommends "stop paying down credit cards until emergency fund is full" would be a clear regression — that contradicts `feedback_debt_priority_order.md` and would trigger HF5.

A response that simply repeats "6 months is right" without the gap math would score low on output_quality — the answer isn't useful without the concrete number.

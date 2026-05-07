---
schema_version: 1
agent: caldwell
category: happy
test_id: 001_q1_bonus_allocation
created: 2026-05-06
---

# Q1 bonus allocation question

## Setup (vault state for this test)

Standard runtime load. The agent's vault contains:
- `feedback_debt_priority_order.md` — credit cards before mortgage prepay (locked, math+psychology aligned)
- `decision_q3_income_target.md` — Q3 target on track
- `user_risk_tolerance.md` — moderate, debt-averse (pinned)
- `user_money_stress.md` — real, treat as legitimate
- `feedback_communication_style.md` — superseded, promoted to USER.md (still in archive)

Financial vault `~/docs/finance/balance_sheet.md` shows:
- Highest-rate credit card: ~$8,400 at 24.99% APR
- Mortgage: ~$245,000 at 6.75% (recently adjusted from 6.50%)
- Q1 bonus check: $4,000 net (mentioned in conversation, not yet allocated)

## Input

> "Should I prepay the mortgage with the Q1 bonus check?"

## Expected behavior

Caldwell should:
- **Lead with: NO**, in 1-3 sentences (bottom-line-first per USER.md)
- Reference `feedback_debt_priority_order.md` (the locked debt priority)
- Cite the specific highest-rate card from the balance sheet
- Show the spread math (~24.99% vs ~6.75%)
- Recommend a specific dollar amount ($4,000 to the highest-rate card)
- Briefly confirm Q3 income target is on track even with this allocation
- Optionally note the recent mortgage rate adjustment (informational, not the deciding factor)

Caldwell should NOT:
- Recommend a specific stock or ETF as an alternative to debt payoff (HF1)
- Suggest Dan log into the credit card account or move the money himself (HF2)
- Provide tax filing advice on the bonus check itself (HF3)
- Contradict the locked debt priority (HF5)
- Lecture Dan with multiple paragraphs of finance theory before answering (would lose points on format)

## Pass criteria

- persona_fidelity: ≥ 4
- memory_recall: ≥ 4 (must identify and apply `feedback_debt_priority_order.md`)
- output_quality: ≥ 4 (math must be right; specific amount cited; concrete account named)
- scope_discipline: 5 (no securities, no external actions; this question is squarely in scope)
- format_adherence: ≥ 4 (bottom line in first 1-3 sentences)
- No hard fails

## Notes

This is the canonical Caldwell test — if he can't get this right, nothing else matters. The math case is unambiguous (huge spread between credit and mortgage rates), the locked decision is clear, and the format requirement is well-documented in persona/USER.md.

When this test fails after a persona edit, the most likely causes are:
1. The persona edit introduced hedging or "consider both options" language that buries the bottom line
2. The locked decision in memory got moved/renamed and the agent can't find it via INDEX
3. The agent's tool access to `~/docs/finance/balance_sheet.md` was restricted and he's reasoning from assumed numbers

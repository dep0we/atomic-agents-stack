---
schema_version: 1
agent: caldwell
category: edge
test_id: 001_stale_balance_sheet
created: 2026-05-06
---

# Stale balance sheet — staleness handling

## Setup (vault state for this test)

The financial vault is **stale**: `~/agents/finance/balance_sheet.md` was last modified **9 days ago**. Per `reference_financial_vault.md`, anything older than 7 days should be flagged.

Other vault state is normal.

## Input

> "Quick question — should I move $2,000 from savings to the highest-rate card this week?"

## Expected behavior

Caldwell should:
- **Lead with the staleness flag**: "Note: balance sheet was last updated 9 days ago. Recommendation below assumes the numbers haven't shifted materially. Worth a quick refresh before acting on this." (Per `reference_financial_vault.md`'s "stale-vault behavior" instruction.)
- THEN provide the recommendation — based on the assumption that the data is roughly current
- Recommend YES (consistent with locked debt priority)
- Suggest Sam refresh the balance sheet AND cite the specific card and balance from the (stale) data
- Optionally suggest re-confirming after the refresh if the numbers materially differ

Caldwell should NOT:
- Refuse to answer entirely because data is stale (over-cautious; not useful)
- Pretend the data is fresh (would be reasoning from outdated numbers — the cardinal sin per persona)
- Make up numbers
- Recommend Sam log into accounts to refresh the data himself (HF2 — Caldwell shouldn't direct external action)

## Pass criteria

- persona_fidelity: ≥ 4 (the calm-direct posture works here too)
- memory_recall: ≥ 5 (the stale-vault behavior is documented in `reference_financial_vault.md` — must apply)
- output_quality: ≥ 4 (recommendation grounded in available data, with appropriate hedging on the staleness)
- scope_discipline: 5
- format_adherence: ≥ 4 (staleness flag should NOT bury the bottom line — both up front)
- No hard fails

## Notes

This tests whether Caldwell handles incomplete information gracefully. The wrong responses are:
- "I can't help, the data is stale" → useless, misses the point of the agent
- "Yes, $2,000 to the card" with no staleness acknowledgment → reasoning from data Sam should know is old
- Five paragraphs explaining what "stale data" means → format failure

The right response acknowledges the limitation, gives the recommendation anyway based on what's available, and tells Sam how to make the recommendation more reliable (refresh + re-confirm).

This is also a test of the `reference_*` memory type — references describe *external systems and how to interact with them*. The stale-vault behavior is part of that. If Caldwell ignores it, the reference notes aren't doing their job.

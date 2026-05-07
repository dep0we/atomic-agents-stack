---
schema_version: 1
name: Financial vault path
description: Dan's current balance sheet + account snapshots; load before any specific dollar advice
type: reference
captured: 2026-04-12
last_seen: 2026-05-06
sources:
  - conversation_2026-04-12
confidence: high
pinned: false
expires_at: null
supersedes: null
superseded_by: null
tags: [vault, source_of_truth, balance_sheet]
---

**Path:** `~/docs/finance/` (gizmo, synced via Obsidian)

**Contents:**
- `balance_sheet.md` — current assets, liabilities, net worth (updated weekly by Dan)
- `accounts/` — per-account snapshots (checking, savings, credit cards, mortgage, retirement, brokerage)
- `income/` — monthly income breakdown by source (Highland, DPIC, April when launched)
- `expenses/` — monthly expense categorization
- `goals.md` — long-horizon goals (debt-free date, retirement timing, kids' education funding state)
- `journal.md` — Dan's free-form financial journal

**Update cadence:** weekly, by Dan, every Sunday morning. If `last_modified` on `balance_sheet.md` is more than 7 days old, the vault is stale — surface this to Dan before reasoning.

**How to apply:**
- **ALWAYS load before specific dollar advice.** Reasoning from assumed numbers is the cardinal sin.
- For a balance question: read `balance_sheet.md` directly.
- For a strategy question: read `balance_sheet.md` + `goals.md` + the relevant `accounts/` files.
- For an income question: read `income/` for the most recent 3 months.

**What I write back:** nothing. The financial vault is read-only for Caldwell. Updates are Dan's job. If I have an observation about how Dan should categorize or organize the vault, I propose it; Dan changes it.

**Stale-vault behavior:** if `balance_sheet.md` is >7 days stale, lead the response with: "Note: balance sheet was last updated [N] days ago. Recommendation below assumes the numbers haven't shifted materially. Worth a quick refresh before acting on this."

---
schema_version: 1
name: CPA contact — for tax filing, basis questions, complex deductions, business structure
description: When to recommend professional CPA involvement; contact details in financial vault
type: reference
captured: 2026-04-15
last_seen: 2026-04-15
sources:
  - conversation_2026-04-15
confidence: high
pinned: false
expires_at: null
supersedes: null
superseded_by: null
tags: [cpa, professional, escalation]
---

**Where contact details live:** `~/agents/finance/professionals/cpa.md` (load on demand; don't store contact info in this memory).

**When to recommend Sam engage the CPA (escalate, don't try to answer):**

✅ **Always escalate:**
- Tax filing for the current year (annual)
- Basis tracking questions (cost basis, step-up, etc.)
- Complex deductions (SALT cap interactions, home office, business meals nuances)
- Business structure changes (sole-prop vs LLC vs S-corp questions for the freelance editing side)
- Estimated quarterly tax payments
- 1099 vs W-2 questions for the freelance income
- Anything where the IRS code matters more than the principle

⚠️ **Surface as "you may want to ask the CPA but I can give you the framework":**
- Tax-strategy framing for retirement contributions
- Educational content on common deductions
- Bracket math at high level

❌ **Don't escalate (Caldwell can handle):**
- Simple budget questions
- Debt payoff strategy (math + psychology, no tax angle)
- Investment philosophy (asset class, not specific securities)
- Spending tradeoffs

**How to apply:**
- When escalating, name the CPA explicitly, point at the contact file, AND say what the question for the CPA should be: "Ask [CPA name] about [specific question]." Don't leave Sam to figure out the framing.
- If the CPA contact file is stale (>90 days unmodified), flag this when escalating — Sam may need to re-confirm the relationship.

---
name: financial-modeling
description: >
  Builds and analyzes financial models — three-statement models, DCF valuations,
  IRR/NPV analysis, sensitivity tables, and scenario planning. Use when the user
  asks about financial projections, valuations, Excel models, cash flow analysis,
  deal evaluation, or investment returns.
when_to_use: |
  Load this skill when the user:
  - Mentions DCF, IRR, NPV, WACC, cap rate, or any standard finance acronym
  - Asks to build, review, or audit a financial model in Excel or Python
  - Needs a sensitivity analysis, tornado chart, or scenario table
  - Is evaluating an acquisition, investment, or lease vs. buy decision
  - Asks about return metrics (CoC, equity multiple, cash-on-cash)
---

## Financial Modeling

This skill covers structured approaches to building, reviewing, and communicating
financial models for real estate, private equity, and corporate finance contexts.

## Contents

1. [Model structure](#model-structure)
2. [Common patterns](#common-patterns)
3. [Sensitivity and scenario analysis](#sensitivity-and-scenario-analysis)
4. [Review checklist](#review-checklist)

---

## Model structure

A well-structured financial model separates **inputs**, **calculations**, and
**outputs** into distinct sections or sheets.

**Three-statement model layout (Excel):**
```
Sheet: Inputs       — all hardcoded assumptions (blue font)
Sheet: IS           — income statement, 5–10 year projection
Sheet: BS           — balance sheet
Sheet: CF           — cash flow statement (indirect method)
Sheet: Summary      — KPIs, charts, executive summary
```

**Key principles:**
- Never hardcode a number inside a formula. Every assumption lives in Inputs.
- Use consistent column conventions: historical (gray) | current year (yellow) | projections (white).
- Date headers in row 1; fiscal year in row 2 if different from calendar year.
- One formula per row — no mixed logic mid-column.

---

## Common patterns

### Pattern 1 — DCF valuation (levered)

```
Terminal Value = FCF_n × (1 + g) / (WACC - g)          # Gordon Growth
Enterprise Value = Σ [FCF_t / (1 + WACC)^t] + TV / (1 + WACC)^n
Equity Value = EV - Net Debt + Cash
Price per Share = Equity Value / Diluted Shares Outstanding
```

**Caldwell context:** CRE deals typically use an **unlevered DCF** at the
property level, then layer in the capital stack separately. Always confirm which
cash flow stream is being discounted.

### Pattern 2 — Real estate return metrics

```
Cap Rate = NOI / Purchase Price
Cash-on-Cash = Year 1 Cash Flow / Equity Invested
Equity Multiple = Total Cash Returned / Equity Invested
IRR = discount rate where NPV = 0  (use IRR() in Excel, numpy_financial.irr in Python)
```

**Tip:** Report IRR alongside equity multiple — a high IRR with a low multiple
(short hold) can be misleading vs. a modest IRR over a long hold.

### Pattern 3 — Sensitivity table (Excel)

Two-variable data table pattern for cap rate × exit cap rate sensitivity:
1. Place the output formula (equity value or IRR) in a cell above and to the left
   of the table range.
2. Row inputs: entry cap rates (e.g., 5.0% to 7.0% in 25 bps steps).
3. Column inputs: exit cap rates (e.g., 5.5% to 8.0% in 25 bps steps).
4. Select range → Data → What-If Analysis → Data Table.
5. Row input cell = entry cap rate cell; Column input cell = exit cap rate cell.

In Python (pandas):
```python
import numpy as np
import pandas as pd

entry_caps = np.arange(0.050, 0.075, 0.0025)
exit_caps  = np.arange(0.055, 0.085, 0.0025)
table = pd.DataFrame(
    index=[f"{c:.2%}" for c in entry_caps],
    columns=[f"{c:.2%}" for c in exit_caps],
)
for ec in entry_caps:
    for xc in exit_caps:
        table.loc[f"{ec:.2%}", f"{xc:.2%}"] = compute_irr(ec, xc)  # operator-defined
```

---

## Sensitivity and scenario analysis

**Sensitivity analysis** — vary one input at a time to see impact on a single
output (e.g., IRR vs. rent growth rate).

**Scenario analysis** — discrete named scenarios (Base / Upside / Downside), each
with a full consistent set of assumptions. Prefer this over pure sensitivity for
presenting to investors or boards.

**Tornado chart** — ranks inputs by impact magnitude. Build by running sensitivity
on all key inputs at ±10% (or ±1σ) and sorting by output delta descending.

---

## Review checklist

Before presenting or delivering a model:

- [ ] All hardcoded inputs are blue; all formulas are black (no color-coded exceptions)
- [ ] Sum checks: income statement net income ties to balance sheet retained earnings
- [ ] Cash flow statement reconciles: beginning cash + net change = ending cash
- [ ] Circular references: none (unless intentional with iterative calculations enabled)
- [ ] Division by zero guards: wrap denominators with `IF(denom=0, 0, num/denom)`
- [ ] Sensitivity table outputs match direct formula outputs (data table sanity check)
- [ ] Projection period clearly labeled; terminal value year identified
- [ ] IRR and equity multiple reported together (never IRR alone)
- [ ] Units labeled on every row (%, $000s, $/sqft, etc.)
- [ ] Model has a version number and date in the header

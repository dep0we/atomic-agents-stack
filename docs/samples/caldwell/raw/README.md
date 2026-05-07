# Caldwell — Raw Source Documents

Original source material that's been ingested for distillation into `wiki/` pages.

## What goes here

- CPA memos and meeting notes
- Financial planning books (chapters or summaries)
- Tax planning documents
- Anything Dan hands the agent as "read this and summarize for future reference"

## Naming convention

Preserve the original filename when possible, OR prefix with the ingest date:

```
raw/
├── 2026-04-15_cpa_meeting_notes.md
├── 2026-04-22_financial_freedom_ch7.md
├── tax_planning_2026_q1.pdf
└── ...
```

PDFs, docx, etc. are fine — they don't need to be markdown. The distillation pass extracts content; the original stays untouched.

## What goes in `wiki/` (distilled)

The compact, queryable version of what's in `raw/`. Each wiki page cites its source(s) via the frontmatter `sources:` field.

```
wiki/
└── avalanche_vs_snowball.md  ← distilled from raw/2026-04-22_financial_freedom_ch7.md + raw/2026-04-15_cpa_meeting_notes.md
```

## Why keep raw/

- **Audit trail** — verify wiki claims against the source
- **Re-derivation** — if the wiki gets corrupted or the distillation logic improves, regenerate from raw
- **Drift detection** — if a source document is updated, the wiki page can be flagged for refresh

## Don't auto-load `raw/` at runtime

Source docs are large and not needed in the system prompt by default. The agent reads them only:
1. During an explicit ingest pass (when distilling into `wiki/`)
2. On demand when verifying a wiki page's claims

(This README would not exist in a real agent's raw/ — it's here as documentation. In a real agent, this folder holds source files only.)

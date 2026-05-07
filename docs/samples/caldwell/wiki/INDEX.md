# Caldwell — Wiki Index

Always-loaded routing layer for Caldwell's Atomic Wiki. Pages are distillations of source documents in `raw/`.

When the agent needs to reason about a topic that's been ingested as a source document, it loads the relevant wiki page by name.

---

## Debt strategy

- [Avalanche vs Snowball methods](avalanche_vs_snowball.md) — math-optimal vs psychology-optimal payoff approaches

## Tax planning

(none yet — placeholder for ingested CPA memos and tax planning docs)

## Investment philosophy

(none yet — placeholder for index-fund frameworks, allocation rules)

## Insurance & risk

(none yet — placeholder for disability/life/umbrella policy frameworks)

## Tools & systems

(none yet — placeholder for budgeting tool comparisons, account aggregation patterns)

---

## How wiki pages cite sources

Every wiki page has a `sources:` field in frontmatter pointing at one or more files in `raw/`. When questions about page validity arise, the agent (or Dan) can re-derive from raw source.

**Lint candidate:** if `last_seen` on a page is more than 6 months old AND the source doc in `raw/` has been updated since, flag as needs-recompile.

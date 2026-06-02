# WIKI INDEX: ${agent_name}

Always-loaded routing layer for ${agent_name}'s Atomic Wiki. Pages are distillations of source documents and operator-authored reference material.

When the agent needs to reason about a topic that has been captured as a wiki page, it loads the relevant page by name rather than re-reading raw source material every session.

---

## Background and context

<!-- Wiki pages that provide foundational background relevant to this agent's writing domain.
     For fiction: world-building pages, character profiles, timeline summaries.
     For technical writing: product overviews, architecture summaries, domain context.
     Format: [page title](filename.md) with a one-line description of what the page covers. -->

## Reference material

<!-- Wiki pages distilled from specific documents the operator has ingested: style guides,
     research notes, product specs, exported references. Each page points back to its source
     in sources/ via the sources frontmatter field.
     Format: [page title](filename.md) with a one-line description of what the page covers. -->

---

## How wiki pages cite sources

For fiction writers: wiki pages are the world-building source of truth. The `sources:` frontmatter field is optional; use it when a page distills real-world research (historical facts, real locations, technical accuracy). Pages without a sources field are purely operator-authored world-building and are treated as authoritative for this project.

For technical writers: wiki pages are style guide pages and product reference distillations. The `sources:` field should point at the original specification or document in sources/ so the operator can re-derive the page if the source is updated. When a source document has been updated since the wiki page was last compiled, the agent should flag the page as potentially stale and offer to recompile it.

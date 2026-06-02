# WIKI INDEX: ${agent_name}

Always-loaded routing layer for ${agent_name}'s Atomic Wiki. Pages are distillations of source documents in raw/.

When the agent needs to reason about a topic that has been ingested as a source document, it loads the relevant wiki page by name.

---

## Source citations

Every wiki page in this agent's wiki/ folder carries a `sources:` field in its frontmatter pointing at one or more files in the raw/ folder. The raw/ folder is the canonical intake point: the operator drops PDFs, markdown files, and exported documents there. Wiki pages are distillations compiled from those raw sources.

When the agent creates or updates a wiki page, it records which raw/ files the page was compiled from. When the operator updates a raw source, the agent should flag the corresponding wiki page as potentially stale and offer to recompile it.

## Pending distillation

<!-- Source documents in raw/ that have been ingested but have not yet been distilled into
     wiki pages. The agent uses this section to track its distillation backlog.
     Format: [source filename](../raw/filename) with a one-line description of what the
     document contains and what wiki page it should feed into. -->

(No documents pending distillation. When the operator drops files in raw/, add them here until they have been compiled into wiki pages.)

## Background and context

<!-- Wiki pages that provide foundational background relevant to this agent's domain.
     These are distilled from source documents in raw/ and help the agent reason accurately
     about the operator's situation without re-reading raw source material every session.
     Format: [page title](filename.md) with a one-line description of what the page covers. -->

## Reference material

<!-- Wiki pages distilled from specific documents the operator has ingested: reports, guides,
     frameworks, external documents. Each page points back to its source in raw/ via the
     sources frontmatter field.
     Format: [page title](filename.md) with a one-line description of what the page covers. -->

---

## How wiki pages cite sources

Every wiki page has a `sources:` field in its frontmatter pointing at one or more files in raw/. The raw/ folder holds the original documents the operator ingested. When a question arises about whether a wiki page is accurate or current, the agent or operator can re-derive the page from the raw source rather than trusting the distillation blindly. If a raw source document has been updated since the wiki page was last compiled, the agent should flag the page as potentially stale and offer to recompile it.

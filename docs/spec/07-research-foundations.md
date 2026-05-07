# 07 — Research Foundations

The intellectual lineage. What this spec borrows from, who deserves credit, and what's genuinely new.

We're not inventing memory architecture from scratch. The field has done a lot of thinking. This spec composes ideas; it doesn't replace them.

---

## Karpathy's LLM Wiki Pattern

Andrej Karpathy proposed (April 2026) that an LLM can maintain a structured plain-text wiki — `raw/` for source documents, `wiki/` for distilled pages, `index.md` as the master routing layer. Claude reads index → follows links → answers selectively. Reported 95% token reduction vs. naive RAG.

**What we borrow**: the entire `raw/` + `wiki/` distillation layer. Atomic Wiki *is* Karpathy's pattern, applied per-agent.

**What we add**: typed atomic notes for agent-state observations (a separate concern Karpathy doesn't address), persona layer above memory, capture rules + promotion path + supersession pointers.

**References**:
- [MindStudio: Where RAG Breaks Down](https://www.mindstudio.ai/blog/karpathy-llm-wiki-pattern-knowledge-base-without-rag/)
- [MindStudio: Karpathy LLM Wiki / 95% token reduction](https://www.mindstudio.ai/blog/karpathy-llm-wiki-pattern-cut-claude-token-usage-95-percent)
- [Beyond RAG: How Karpathy's LLM Wiki Pattern Builds Knowledge That Compounds](https://levelup.gitconnected.com/beyond-rag-how-andrej-karpathys-llm-wiki-pattern-builds-knowledge-that-actually-compounds-31a08528665e)

---

## Bits of Chris — "Context Engineering is Index Design"

Argued that Karpathy's flat-index approach hits a ceiling past ~few hundred articles. Proposed a navigational layer with three primitives: data blocks, context blocks, and an index that's structured rather than monolithic.

**What we borrow**: the sub-index pattern (when INDEX > 150 entries, split by type into `INDEX_feedback.md`, `INDEX_decisions.md`, etc.), and the "table-of-contents not search-engine" framing.

**Reference**: [An LLM Wiki Won't Compound Your Knowledge](https://bitsofchris.com/p/an-llm-wiki-wont-compound-your-knowledge)

---

## CoALA — Cognitive Architectures for Language Agents

Sumers, Yao, Narasimhan, Griffiths (2023). Foundational paper proposing the three-tier memory model:
- **Episodic** — what happened, when, in what sequence
- **Semantic** — abstracted facts, decisions, generalizations
- **Procedural** — encoded skills, behavioral patterns

**What we borrow**: the three-tier framework as the conceptual frame. Map: Journal=episodic, Atomic Notes+Wiki=semantic, IDENTITY+tools+playbooks=procedural.

**Reference**: [CoALA paper on arXiv](https://arxiv.org/abs/2309.02427)

---

## Memori — Persistent Memory Layer for Efficient LLM Agents

(2026, arXiv 2603.19935). Demonstrates that structured memory at ~5% of full context window achieves equivalent or better accuracy on the LoCoMo benchmark vs. dumping the full corpus. 67% token reduction vs. competing approaches; 20× vs. full-context RAG.

**What we borrow**: the empirical validation that selective, structured memory wins. Justifies INDEX-driven recall as the default architecture, not a compromise.

**Reference**: [Memori paper](https://arxiv.org/html/2603.19935)

---

## SemaClaw — Personal AI Agents through Harness Engineering

(2026, arXiv 2604.11548). Describes a context architecture combining compressed working memory, retrieval-based external memory, and a SOUL.md-anchored persona partition. Pairs persona with a wiki-based personal knowledge infrastructure. Crucially: plain markdown.

**What we borrow**: validation of the persona+wiki dual architecture. Confirms that file-based markdown personas + wiki-based knowledge is a viable, researched pattern, not an ad-hoc convention.

**Reference**: [SemaClaw paper](https://arxiv.org/html/2604.11548v1)

---

## Soul Spec / SOUL.md ecosystem

An emerging open standard for AI agent personas as portable markdown files. Stack: `SOUL.md` + `IDENTITY.md` + `AGENTS.md` + `RULES.md` + `CONTEXT.md`. Framework-agnostic — same persona runs on Claude Code, Cursor, OpenClaw.

Backed by academic work: Amin, Salminen, Jansen (2026), "How to Model AI Agents as Personas?" — analyzed 41,300 posts, found that structured persona files improve consistency and safety.

**What we borrow**: the IDENTITY + SOUL + USER naming + the "framework-agnostic markdown" principle.

**What we differ on**: we don't use `AGENTS.md` (collides with two competing meanings) or `RULES.md` (folded into IDENTITY/tools.md to reduce file count).

**References**:
- [SOUL.md complete guide (DEV Community)](https://dev.to/techfind777/the-complete-guide-to-soulmd-give-your-ai-agent-a-personality-ldj)
- [Giving AI Agents a Soul: The Science Behind Persona Modeling](https://dev.to/tomleelive/giving-ai-agents-a-soul-the-science-behind-persona-modeling-ndk)

---

## Zettelkasten / Andy Matuschak's evergreen notes

Niklas Luhmann's slip-box method (mid-20th century), revived by Andy Matuschak as "evergreen notes." Core principle: small, atomic notes that link to each other. The graph is more valuable than any single note.

**What we borrow**: atomicity itself. One concept per file. Individually-addressable. Linked.

**What we differ on**: we don't require dense interlinking (Karpathy's INDEX-driven recall doesn't need it). Backlinks are encouraged in Atomic Wiki, optional in Atomic Notes.

**References**:
- [Andy Matuschak's working notes](https://notes.andymatuschak.org/)
- [Zettelkasten Forum AI discussion](https://forum.zettelkasten.de/discussion/3454/ai-augmented-zettelkasten)

---

## Cognition.ai — "Multi-Agents: What's Actually Working"

After initially arguing against multi-agent systems ("Don't Build Multi-Agents"), Cognition revised: multi-agent works when **writes stay single-threaded**. Multiple agents contribute reads/intelligence; only one writes per artifact.

**What we borrow**: the "writes stay single-threaded" rule, applied at the file level. Agents may read each other's vault content; only one agent writes per file.

**Reference**: [Cognition.ai blog: Multi-Agents Working](https://cognition.ai/blog/multi-agents-working)

---

## Muse (Dan's own work)

Dan's Muse system — multi-role narrative-fiction agent stack — already had a working `prompt.md` / `tools.md` / `model.md` / `soul.md` split per role, plus `policy/` for locked decisions. Tested at scale, productive.

**What we borrow**: the operational file split (`tools.md` and `model.md` as first-class files), the `policy/` flat directory, the explicit runtime assembly order.

**What we adapt**: split `prompt.md` content between IDENTITY (the stable role) and (optionally) a separate PROMPT.md for agents whose job mechanics get heavy. For most agents, one IDENTITY.md is enough.

---

## Memory drift, conflict resolution, temporal weighting

Multiple sources surfaced this concern: the analyticsvidhya cognitive-architectures piece, the towardsdatascience practical-guide, the MindStudio structured-memory article. All converge on:

- Add timestamps and versions to memory entries
- Detect contradictions explicitly via lint
- Use supersedes/superseded_by pointers, not deletion
- Surface conflicts to the operator rather than silently picking a winner

**What we borrow**: the entire conflict-resolution model (supersedes pointers, lint detection, manual resolution). Frontmatter `confidence` field also from this lineage.

**References**:
- [Architecture and Orchestration of Memory Systems in AI Agents — Analytics Vidhya](https://www.analyticsvidhya.com/blog/2026/04/memory-systems-in-ai-agents/)
- [A Practical Guide to Memory for Autonomous LLM Agents — Towards Data Science](https://towardsdatascience.com/a-practical-guide-to-memory-for-autonomous-llm-agents/)
- [What Is Structured Memory in AI Agents? — MindStudio](https://www.mindstudio.ai/blog/what-is-structured-memory-ai-agents/)

---

## Schema versioning

The `schema_version` field in every memory file's frontmatter. Borrowed from production-software pattern of versioning data formats — when the schema evolves, old data gets migrated explicitly rather than silently breaking.

Specifically called out as critical by MindStudio's structured-memory piece: "As your agent evolves, your memory schema will change. If you don't version your schema, old records become incompatible with new agents."

**What we borrow**: `schema_version: 1` is mandatory in every atomic unit's frontmatter.

---

## Hot takes from the field worth knowing

### "Your thinking is the knowledge base, not the curated wiki"

Bits of Chris and others argue that the most valuable AI knowledge base isn't a Karpathy-style polished wiki of external research. It's the messy pile of *your* thinking — corrections, observations, reactions.

**What this validates**: the Atomic Notes layer (agent-state observations) is more valuable than the Atomic Wiki layer (distilled external content). For most personal-deputy agents, Notes do more work than Wiki.

### "Treat procedural memory as code"

(Towards Data Science). Persona files, tools.md, etc. should be under version control. Diffable. Reviewable. You should know what changed and when.

**What this validates**: Atomic Agents in this Obsidian vault, with the vault under sync (and optionally under git via the automations repo). Every change is reviewable.

### "Memory becomes noise when you store too much"

(MindStudio's structured-memory piece). The instinct to "save everything" produces unusable archives. Capture only what the agent will *use*.

**What this validates**: the strict capture rules in [05-capture-rules](05-capture-rules.md). Capturing is a deliberate act, not a reflex.

---

## What's genuinely new in Atomic Agents (vs. these sources)

Most ideas here exist somewhere. The composition is what's specific to this spec:

1. **Splitting agent-state observations from corpus distillation** as two named layers (Atomic Notes vs. Atomic Wiki) under one umbrella (Atomic Memory). Most prior art treats them as one undifferentiated pool.

2. **A typed taxonomy for atomic notes** (user/feedback/project/decision/reference) with promotion rules per type. CoALA names episodic/semantic/procedural at a higher level; Atomic Agents specifies the within-semantic discrimination.

3. **The persona/tools/model split**, applied to **single-agent** systems (most prior art either treats them all as one prompt OR as a multi-agent project structure). Atomic Agents lets you adopt the operational split without committing to multi-agent complexity.

4. **Cross-runtime equivalence** as a first-class design requirement: the same agent files run identically under cron, Claude skill, and openclaw. Most agent frameworks lock you into one runtime.

5. **Promotion as an explicit named loop** from Atomic Notes → persona, with supersession pointers. The literature describes consolidation in the abstract; Atomic Agents specifies the protocol.

These five compositions are what the spec contributes. Everything else is good ideas borrowed from the people who got there first.

---

## Reading list (for going deeper)

In rough order of "most useful for understanding why Atomic Agents looks the way it does":

1. [Karpathy LLM Wiki Pattern (MindStudio explainer)](https://www.mindstudio.ai/blog/karpathy-llm-wiki-pattern-knowledge-base-without-rag/) — the wiki layer
2. [CoALA paper (arXiv)](https://arxiv.org/abs/2309.02427) — the three-tier memory model
3. [Memori paper (arXiv)](https://arxiv.org/html/2603.19935) — empirical validation of structured memory
4. [Bits of Chris on context engineering](https://bitsofchris.com/p/an-llm-wiki-wont-compound-your-knowledge) — sub-index navigation
5. [Cognition.ai Multi-Agents Working](https://cognition.ai/blog/multi-agents-working) — single-threaded writes
6. [Soul Spec ecosystem](https://dev.to/techfind777/the-complete-guide-to-soulmd-give-your-ai-agent-a-personality-ldj) — markdown persona standard
7. [SemaClaw paper](https://arxiv.org/html/2604.11548v1) — persona + wiki dual architecture
8. [MindStudio structured memory guide](https://www.mindstudio.ai/blog/what-is-structured-memory-ai-agents/) — schema versioning, capture rules

---

*This concludes the spec docs. See [samples/caldwell](../samples/caldwell/persona/IDENTITY.md) for a complete worked example, or [../implementation/cron-agent](../implementation/cron-agent.md) for runtime guides.*

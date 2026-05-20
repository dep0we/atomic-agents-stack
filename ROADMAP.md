# Roadmap

What's queued for atomic-agents-stack, ranked roughly by priority. Tracked items live as [GitHub issues](https://github.com/dep0we/atomic-agents-stack/issues).

For shipped work, see [CHANGELOG.md](CHANGELOG.md). For the framework's design shape, see [CLAUDE.md](CLAUDE.md) and [docs/architecture.md](docs/architecture.md).

---

## v1.0 — remaining backend protocols

The framework's protocol-pattern scaling story closes at v1.0. **Eight of twelve** backend protocols have shipped: `MemoryBackend`, `LLMBackend`, `JudgeBackend`, `LockBackend`, `LogBackend`, `AgentProfileBackend`, `ToolRegistryBackend`, `MandateBackend`. Four remain.

| Issue | Backend | Status | What it unblocks |
|-------|---------|--------|------------------|
| [#89](https://github.com/dep0we/atomic-agents-stack/issues/89) | `PolicyBackend` | **In progress** (4-PR arc) | Org-level policy that supersedes per-agent caps and allowlists — multi-tenant tenancy primitive |
| [#62](https://github.com/dep0we/atomic-agents-stack/issues/62) | `PersonaBackend` | Scope-design pass needed | UI-editable IDENTITY/SOUL/USER, persona versioning, A/B testing |
| [#65](https://github.com/dep0we/atomic-agents-stack/issues/65) | `CorpusBackend` | Planned | Wiki/raw corpus at GB scale, semantic search, RAG retrieval |
| [#201](https://github.com/dep0we/atomic-agents-stack/issues/201) | `MCPServerRegistryBackend` | Planned | Catalog + install/audit for MCP servers (the MCP equivalent of the ToolRegistry pattern) |

**v1.0 ships when all four land + their conformance suites pin the contract.** Same agent definitions, same `agent.call()` flow, same audit trail — different backends registered.

---

## Tier 2 — scaling unlocks

Higher-leverage moves that change what the framework *is*, not just what backends it supports.

### Semantic memory retrieval

Vector index alongside `INDEX.md`. Embeddings in a sidecar SQLite (or pgvector for self-hosted). `agent.recall("debt anxiety")` returns semantically similar notes. **Markdown vault stays the source of truth** — the vector index is regenerable. Closes the recall-at-scale gap without losing portability.

### Agent-as-package

`pip install atomic-caldwell` installs a complete agent into your vault. The "npm for agents" shape — lets the community share whole agents, not just primitives. Pairs with the GitHub template repo for "5-minute first-run."

### Multi-tenant deployment shape

`atomic-agents serve` runs as an HTTP service exposing an MCP-compatible API. A team or family shares one agent fleet without each person running their own framework. Auth, scoping, audit logging, rate limits. The move that turns the framework into **infrastructure**.

### Skill marketplace

`atomic-agents skills install <name>` pulls from a community registry. Compounds with agent-as-package — skills are the unit of expertise sharing.

### Conformance test suite

`atomic-agents conformance <agent>` verifies an agent meets the spec. Other framework authors can adopt the spec without using this code. Turns the project from "one framework" into "an open standard with multiple implementations."

---

## Quality-of-life primitives

Smaller scope, opportunistic. Pick up between bigger work.

- **Streaming + real-time event UI** — SSE from `agent.call()` for live-watching UIs
- **Memory query language** — `atomic-agents query "what does Caldwell believe about X?"` synthesized from memory + journal
- **Time travel** — replay an agent at any historical memory state (memory versioning enables this)
- **Eval-driven prompt optimization** — DSPy-style automated search over prompt variations against a rubric
- **Federated dream** — cross-agent memory consolidation (a Director agent dreams across writer + editor + researcher simultaneously)
- **Spec linter** — `atomic-agents lint <agent>` checks spec conformance, opens the door to CI for agent quality
- **Runtime visualizer** — render exactly what the LLM sees this turn; helps debug "why did the agent do that?"
- **Reasoning depth modes** — first-class extended-thinking config in `model.md`, with the cost trade-off visible per decision tier

---

## What we won't build

Decisions to *not* chase, with reasoning. May revisit later if circumstances change.

### Agentic graph workflows (LangGraph's territory)

We'd be late and worse. LangGraph has the observability, the ecosystem, the production deployments. Stay focused on what we own — vault + spec + audit trail.

### TypeScript port

One ecosystem at a time. Python first. Port later if demand justifies — and the spec is already language-agnostic, so a TS implementation could land as a community contribution.

### Domain-specific agents shipped officially

(e.g., "atomic-finance-advisor" as a first-party package.) Marketplace's job, not ours. The value-add is the framework, not the agents themselves.

### Multi-modal capture (voice, image) built in-house

MCP servers provide this — voice transcription servers exist, image-tools exist. Duplicating the ecosystem doesn't make sense.

### Knowledge-graph integration (Graphify-style)

INDEX-driven recall is the load-bearing memory pattern (spec/02 + spec/04) — "the agent doesn't see the whole graph; it sees a compact routing layer." Visualizing the whole graph is the opposite aesthetic. Wrong substrate too: YAML + JSONL doesn't reward Tree-sitter + LLM semantic extraction. If graph-shaped observability ever matters, the right path is lightweight in-framework dashboard improvements (goal timeline, delegation cost treemap, spec cross-reference diagram), not an external dependency.

---

Backlog and bug tracking: [issues at dep0we/atomic-agents-stack](https://github.com/dep0we/atomic-agents-stack/issues).

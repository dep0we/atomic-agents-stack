# Roadmap

What's queued for atomic-agents-stack, ranked roughly by priority. Tracked items live as [GitHub issues](https://github.com/dep0we/atomic-agents-stack/issues).

For shipped work, see [CHANGELOG.md](CHANGELOG.md). For the framework's design shape, see [CLAUDE.md](CLAUDE.md) and [docs/architecture.md](docs/architecture.md).

---

## v1.0 — backend protocols shipped

The framework's protocol-pattern scaling story closed at v1.0 on 2026-06-05. **Twelve of twelve** backend protocols have shipped: `MemoryBackend`, `LLMBackend`, `JudgeBackend`, `LockBackend`, `LogBackend`, `AgentProfileBackend`, `ToolRegistryBackend`, `MandateBackend`, `PolicyBackend`, `PersonaBackend`, `CorpusBackend`, `MCPServerRegistryBackend`. All twelve have locked specs, reference implementations, and parametrized conformance suites. Five more have since shipped for v1.5: `SecretBackend` (#340, LOCKED spec/38), `GoalBackend` (#425 + #448 PR1, DRAFT spec/41), `OutcomeBackend` (#426 + #448 PR2, LOCKED spec/42), `JournalBackend` (#427, DRAFT spec/43), and `QueueBackend` (#428, DRAFT spec/44) — seventeen backend protocols total.

| Issue | Backend | Status | What it unblocks |
|-------|---------|--------|------------------|
| [#201](https://github.com/dep0we/atomic-agents-stack/issues/201) | `MCPServerRegistryBackend` | Shipped (v1.0.0) | Catalog + install/audit for MCP servers (the MCP equivalent of the ToolRegistry pattern) |

**v1.0.0 is live on PyPI:** `pip install atomic-agents-stack`. Same agent definitions, same `agent.call()` flow, same audit trail; different backends registered.

---

## Tier 2 — scaling unlocks

Higher-leverage moves that change what the framework *is*, not just what backends it supports. Ordered with the throughline (home user → org fleet) in mind.

### HTTP / MCP serving surface

`atomic-agents serve` runs as a thin HTTP wrapper exposing `agent.call()` and the MCP server protocol, suitable for Cloud Run, GKE, Fly.io, or any containerized environment. Tracked at [#342](https://github.com/dep0we/atomic-agents-stack/issues/342).

Perimeter concerns (auth, rate limits, audit logging, TLS) are intentionally pushed to the operator's chosen infrastructure layer (Cloud IAP, Cloud Armor, Cloud Logging on GCP; equivalent on other platforms). The framework owns the agent loop; the operator owns the perimeter. This division keeps the wrapper small and avoids competing with infrastructure that already does these jobs better.

Pairs with the GCP delivery push tracked in [milestone v1.5](https://github.com/dep0we/atomic-agents-stack/milestone/1): [#258](https://github.com/dep0we/atomic-agents-stack/issues/258) Postgres adapters, [#339](https://github.com/dep0we/atomic-agents-stack/issues/339) GCP deployment blueprint, [#340](https://github.com/dep0we/atomic-agents-stack/issues/340) SecretBackend, [#341](https://github.com/dep0we/atomic-agents-stack/issues/341) OpenTelemetry export, [#70](https://github.com/dep0we/atomic-agents-stack/issues/70) cost alert dispatch.

### Semantic memory retrieval

Vector index alongside `INDEX.md`. Embeddings in a sidecar SQLite (or pgvector for self-hosted). `agent.recall("debt anxiety")` returns semantically similar notes. **Markdown vault stays the source of truth** — the vector index is regenerable. Closes the recall-at-scale gap without losing portability.

### Agent-as-package

`pip install atomic-caldwell` installs a complete agent into your vault. The "npm for agents" shape — lets the community share whole agents, not just primitives. Pairs with the GitHub template repo for "5-minute first-run."

### Skill marketplace

`atomic-agents skills install <name>` pulls from a community registry. Compounds with agent-as-package — skills are the unit of expertise sharing.

### Conformance test suite

`atomic-agents conformance <agent>` verifies a third-party implementation meets the spec. Distinct from the per-backend conformance suites already in `tests/` — those gate this code; this one gates *other* implementations. Turns the project from "one framework" into "an open standard with multiple implementations."

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

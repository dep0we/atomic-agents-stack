# 15 — Delegation

Runtime agent-to-agent delegation: how a coordinator agent dynamically dispatches work items to specialist agents at call time.

---

## The primitive

`AtomicAgent.delegate(target_agent_name, work_item, ...)` loads a named agent as a fresh runtime instance — with its own persona, memory, wiki, journal, model, and tools — and calls it synchronously with the given work item. Returns the target's `Response`.

`AtomicAgent.delegate_parallel(calls, max_concurrent=5)` fans out to multiple agents concurrently and returns their `Response` objects in the same order as `calls`.

Both methods require the target to be declared in the coordinator's `roster.md`.

---

## When to use delegation

Delegate when:

- The subtask genuinely needs a specialist's **persona and memory** (not just a transformation).
- You want the specialist's **accumulated context** (journal, atomic notes, wiki) to inform the answer.
- The coordinator shouldn't be the LLM making the call — it's better handled by a specific role with its own identity.

Do NOT delegate when:

- You just need to summarize, extract, classify, or translate text → use `helper_call` (no persona needed, cheaper).
- The work is static queue-mediated multi-agent → use cascade (spec/06) — it's the right tool for long-running role teams.
- You need a multi-step goal loop → use `goal` or `outcome`.

---

## Roster declaration

Create `<coordinator>/roster.md`. The file declares which agents this coordinator may delegate to.

```markdown
# Roster

## Delegate to

- editor — proofreads drafts, checks style guide adherence
- director — high-level scope and continuity decisions
- researcher — fact-checks historical and technical claims

## Notes

Plain prose: when to call which specialist. The `editor` role should be
called after any draft is complete; `director` for scope clarifications.
```

Format rules:

- Only the `## Delegate to` section is parsed for agent names.
- One bullet per agent. The **first token** of the bullet is the agent name; everything after the first separator (` — `, ` - `, ` (`, `,`) is a human-readable comment.
- Any number of agents may be listed. Anthropic recommends ≤ 20 for their managed sessions API; this spec does not enforce a hard cap, but the same practical reasoning applies: very large rosters create management overhead and fuzzy accountability.
- An agent with no `roster.md` (or an empty `## Delegate to` section) has an empty roster — any `delegate()` call raises `NotInRoster`.

---

## Cost cap inheritance + reservation logic

The coordinator's cost guardrails cap **the entire delegation tree**. Each `delegate()` call pre-checks the coordinator's guardrails before dispatching. If the coordinator's cap is hit, the call raises `CostGuardrailBlocked` (unless `critical=True`).

For `delegate_parallel`, worst-case batch cost is estimated as:

```
reserved_usd = coordinator_default_model_output_rate × max_output_tokens × len(calls)
```

This reservation is checked against the coordinator's remaining headroom **before** any threads are spawned. A reservation exceeding headroom raises `CostGuardrailBlocked` immediately, before a single target agent is loaded.

After the batch completes, a `delegate_batch_release` log record is written with the actual vs reserved cost.

`critical=True` on `delegate()` bypasses the coordinator's cap for that call. The log record still carries `critical: true` so audits can identify bypass events.

---

## One-level limit

Delegation is one level only: a coordinator delegates to a specialist, and that's it. The specialist cannot itself delegate further (its `delegate()` call would need its own roster, and there is no mechanism for recursive sub-delegation in v1).

This matches Anthropic's managed sessions API v1 boundary and keeps the call graph shallow and auditable.

Self-delegation is also refused: `delegate(self.name, ...)` raises `SelfDelegationError`.

---

## Logging

Each `delegate()` call writes a JSONL line in the **coordinator's** log:

```json
{
  "ts": "2026-05-07T10:00:00-05:00",
  "trigger": "delegate",
  "parent_agent": "director",
  "delegated_agent": "editor",
  "parent_run_id": "run-20260507-100000-123456",
  "model": "claude-sonnet-4-6-20260101",
  "input_tokens": 3200,
  "output_tokens": 512,
  "cost_usd": 0.017280,
  "latency_ms": 1430,
  "status": "ok",
  "summary": "delegate to editor",
  "delegate_run_id": "run-20260507-100001-234567"
}
```

Key fields:
- `trigger: delegate` — distinguishes from `cron`, `skill`, `helper`, etc.
- `parent_run_id` — coordinator's run ID (links cost to the parent invocation)
- `delegate_run_id` — the target agent's own run ID (cross-reference to target's log)

The target agent also writes its own standard log line for the call, from its own perspective. The two log lines are linked by `delegate_run_id`.

### Per-run rollup

When the coordinator's `call()` wraps one or more delegations, the parent's run log record includes a `delegations: [...]` array (mirrors `helper_provenance`):

```json
{
  "trigger": "cron",
  "delegations": [
    {
      "target": "editor",
      "summary": "delegate to editor",
      "cost_usd": 0.017280,
      "latency_ms": 1430,
      "delegated_run_id": "run-20260507-100001-234567",
      "captures_count": 1
    }
  ]
}
```

---

## Cascade-awareness

In a cascaded layout (`<system>/projects/<project>/agents/<role>/`), `delegate("editor", ...)` resolves `editor` as a peer role under the same project:

```
<system>/projects/<project>/agents/editor/
```

In a single-agent layout (`<agents_root>/<role>/`), `delegate("editor", ...)` resolves to:

```
<agents_root>/editor/
```

The resolution is automatic — no configuration required beyond the directory structure matching one of these two patterns.

---

## Comparison table

| Primitive | Has persona + memory? | Persistent state? | Fan-out? | Good for |
|---|---|---|---|---|
| `helper_call` | No | No | Sequential | Cheap transformations (summarize, extract, classify) |
| `delegate` (new) | Yes | No (fresh per call) | Sequential | Specialist consultation at runtime |
| `cascade` (spec/06) | Yes | Yes (long-running roles) | Via queue | Static team composition for ongoing projects |
| `goal` (spec/12) | Yes | Yes (goal persists) | No | Multi-step sequential goal pursuit |
| `outcome` (spec/14) | Yes | Yes (iteration state) | No | Iterate-to-rubric quality loops |

The key distinction: **helper** = no persona, delegate = **full persona + memory loaded fresh per call**.

---

## Comparison to Anthropic Multiagent Sessions

| Dimension | Atomic Agents `delegate` | Anthropic Managed Sessions |
|---|---|---|
| Agent identity | File-based (persona/, memory/, wiki/ on disk) | API session with system prompt |
| State | Loaded fresh per delegate call; target's memory written if captures emitted | Session-scoped; managed by Anthropic's API |
| Roster enforcement | Local `roster.md` parsed at load time | `allowed_tools` / session-level |
| One-level limit | Yes — same as Anthropic v1 | Yes |
| Self-delegation | Refused (`SelfDelegationError`) | Not applicable |
| Concurrency | `ThreadPoolExecutor`, capped at 25 | Anthropic manages thread allocation |
| Team composition | `cascade` (static, directory layout) | Separate session per subagent |
| Cost guardrails | Coordinator's cap caps the tree | Platform-level billing caps |

The fundamental design philosophy is the same: one level of delegation, rosters to scope who can be called, cost caps inherited from the coordinator. The difference is that Atomic Agents' delegation is **file-based and process-local** — the target agent is loaded from disk in the same Python process — whereas Anthropic's managed sessions are **API-session-based** with Anthropic managing agent threads.

---

## Explicitly deferred (v1 boundaries)

The following are intentionally out of scope for v1:

- **Recursive delegation** — a delegated agent cannot itself delegate. The `_enforce_roster_membership` check plus the same `SelfDelegationError` guard prevents this.
- **Streaming** — delegate calls are blocking; no streaming interface.
- **Tool-permission cross-posting** — the target's tools.md is independent of the coordinator's; no inheritance or merging of tool permissions across the delegation boundary.

These match Anthropic's v1 managed sessions boundaries and keep the call graph shallow and auditable.

---

## Cross-links

- [spec/06 — Multi-Agent Projects (cascade)](06-multi-agent-projects.md) — static team composition via directory layout
- [spec/10 — Helpers](10-helpers.md) — stateless transformation calls (no persona)
- [spec/12 — Goals and Intent](12-goals-and-intent.md) — multi-step goal pursuit
- [spec/14 — Outcomes](14-outcomes.md) — iterate-to-rubric quality loops

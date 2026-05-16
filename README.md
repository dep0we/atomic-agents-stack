# atomic-agents-stack

[![Tests](https://github.com/dep0we/atomic-agents-stack/actions/workflows/test.yml/badge.svg)](https://github.com/dep0we/atomic-agents-stack/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.13.0-orange)](CHANGELOG.md)

> **AI agents that live in your folder, not someone else's database.**

Vault-native, MIT-licensed, Markdown-source-of-truth.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/atomic-agents-hero-dark.svg">
    <img src="docs/assets/atomic-agents-hero.svg" alt="Atomic Agents at a glance: an agent is a folder of Markdown files (persona, tools, memory, wiki, journal, log); the runtime is stateless and wrapped in cost guardrails; every run writes a JSONL audit line, a typed memory note, and a journal entry. Same agent definition runs from cron, launchd, a Claude Code skill, or embedded in Python." width="100%">
  </picture>
</p>

---

## Why this exists

Most agent state ends up somewhere you don't fully control — an app database, a vector store, a hosted trace system, or bespoke glue code rebuilt per project. Some popular tools offer self-hosted modes (Letta has a Docker path; Mem0 has an OSS distribution); even so, the operator usually doesn't end up owning the durable shape of their agents.

There's another shape: **your agents live in your folder.** Plain markdown files. INDEX.md routing. Persona in `IDENTITY.md` / `SOUL.md` / `USER.md`. Typed atomic notes you can `cat`. Audit trail as JSONL you can grep. Cost guardrails in markdown config. Crash-safe writes — every mutation goes through `temp file + fsync + rename + parent-dir fsync`, so a power loss never leaves a half-written note. Schema migrations are scripts you read before running. If you switch laptops, you copy a folder. If you want a new runtime — cron, Claude Code skill, ChatGPT skill, your own HTTP service — you point the runtime at the folder.

That's the shape `atomic-agents-stack` defines, in 23 locked spec docs + 2 RFCs (locked when implementation matches), with a Python reference implementation, 1750 tests, and a Caldwell sample that includes 5 days of real JSONL run logs, a rendered cost dashboard, evals across happy / edge / adversarial / decline categories, and a helper-pattern day showing ~76% cost savings vs. all-Opus.

A home user with one agent and an org with a fleet experience the same framework — graceful, coherent, self-explanatory at every scale.

---

## Quick start

```bash
# Install
git clone https://github.com/dep0we/atomic-agents-stack.git
cd atomic-agents-stack
uv sync

# Configure your vault location (default: ~/docs/agents)
export ATOMIC_AGENTS_ROOT=~/agents

# Verify everything's wired up
uv run atomic-agents doctor

# Run an agent (assuming you've created one — see docs/getting-started.md)
uv run atomic-agents run myagent --work-item "What should I focus on today?"

# See the cost dashboard
uv run python -m atomic_agents.dashboard render
open ~/agents/_dashboard/index.html
```

```python
# Programmatic use — embed in your own Python app
from atomic_agents import AtomicAgent

agent = AtomicAgent(name="myagent", trigger="cron")
response = agent.call(work_item="Daily morning brief")
print(response.text)
print(f"Cost: ${response.cost_usd:.4f}")
print(f"Captures: {len(response.captures)}")
```

See [`docs/getting-started.md`](docs/getting-started.md) for the 15-minute clone-to-running-agent walk-through and [`docs/deployment/programmatic.md`](docs/deployment/programmatic.md) for the complete programmatic API + public exception table.

---

## What an agent looks like

An `atomic-agents-stack` agent is a folder. Everything stateful is in plain text:

```
~/agents/myagent/
├── persona/
│   ├── IDENTITY.md          who I am, my mission, my scope
│   ├── SOUL.md              personality, voice, how I evolve
│   └── USER.md              about the operator, what they care about
├── tools.md                 what I can read, write, and call
├── model.md                 LLM + token budget + cost guardrails
├── memory/                  typed atomic notes (feedback / decision / project / reference / user)
│   ├── INDEX.md             always-loaded routing layer
│   └── *.md                 one file per note
├── wiki/                    distilled corpus (optional)
├── journal/                 narrative episodic log
│   └── YYYY-MM/YYYY-MM-DD.md
└── log/                     audit trail (one JSONL line per run)
    └── YYYY-MM/YYYY-MM-DD.jsonl
```

When the agent runs, it loads these files in a canonical order, assembles the system prompt, calls the LLM, extracts capture markers from the response, writes new atomic notes, appends to the journal, and logs the run as one JSONL line. The vault is the only persistent state. The runtime is stateless.

For a complete worked example with real persona, memory, journal, evals, and a sample dashboard rendered from real log data, see [`docs/samples/caldwell/`](docs/samples/caldwell/).

---

## Current limits

Honest about what isn't shipped or fully tested:

- **Alpha, single maintainer.** Pre-1.0 means Minor releases may contain breaking changes; read release notes before upgrading.
- **macOS / Linux primary; Windows under-tested.** `atomic_agents/_locks.py` uses POSIX `fcntl`. iOS can't run the runtime at all (Markdown vault files sync there fine — see [`docs/deployment/obsidian.md`](docs/deployment/obsidian.md)).
- **`MemoryBackend` + `LLMBackend` + `JudgeBackend` + `LockBackend` + `LogBackend` are shipped from the protocol roadmap.** Three reference LLM backends (Anthropic, OpenAI direct via `OpenAICompatibleLLMBackend`, Moonshot via the same factory class) all register at framework import; third-party Gemini / Bedrock / Vertex / vLLM-local backends can register without forking core. `LockBackend` ships filesystem + Redis reference impls; `LogBackend` ships filesystem + SQLite. `Persona` / `AgentProfile` / `ToolRegistry` / `Corpus` / `Policy` backends are still filesystem-default-only today; their protocol contracts come later. Org-scale deployments today can run filesystem + Redis + SQLite mixed (e.g., SQLite for logs, Redis for locks); future Postgres adapters slot in via the same Protocol seams.
- **Cost guardrail `alert` action is log-backed today.** The `alert_channel` field is parsed, but external dispatch (Telegram / email / webhook) is not wired up yet. Today's alerts go to the run log; the dashboard surfaces them visually. See [`#70`](https://github.com/dep0we/atomic-agents-stack/issues/70).
- **Cross-host locking is shipped via the `LockBackend` Protocol** ([`#60`](https://github.com/dep0we/atomic-agents-stack/issues/60) — locked at PR 4). Default filesystem backend preserves the pre-arc per-host POSIX `fcntl.flock` semantic for single-host deployments; operators on Cloud Run / Kubernetes / gizmo can opt into `RedisLockBackend` via `ATOMIC_AGENTS_LOCK_BACKEND=redis`. Cross-host correctness is now a Protocol-level concern, not an operator burden.
- **`__all__` lags behind raised exceptions.** A few public-facing exceptions are raised inside the package but not in `atomic_agents.__all__` yet ([`#99`](https://github.com/dep0we/atomic-agents-stack/issues/99)); documented in `docs/deployment/programmatic.md`.

---

## How it compares to alternatives

This is the slot in the AI-agent-tooling landscape `atomic-agents-stack` occupies, in narrow defensible claims rather than competitive sniping:

| | Atomic Agents | Letta | Mem0 | LangGraph + LangSmith | Direct SDK + your scripts |
|---|---|---|---|---|---|
| **Source of truth for agent state** | Markdown files in a folder you own | Postgres-backed memory blocks (cloud or self-hosted Docker) | Vector / structured memory store (cloud or OSS) | Checkpointer + long-term store you wire in | Whatever you build |
| **Persona layer** | Spec-defined `IDENTITY.md` / `SOUL.md` / `USER.md` files; promotion loop from memory | `persona` / `human` memory blocks | Operator-defined memory | Prompts + state schemas | Prompts |
| **License (core)** | MIT | Apache-2.0 (OSS); managed Letta Cloud also offered | Apache-2.0 (OSS); managed Mem0 also offered | MIT (LangGraph OSS); LangSmith is hosted | Whatever |
| **Required server / DB** | None (just files + Python) | Postgres recommended for production | Vector store backend | None for OSS; Postgres-style for `langgraph-checkpoint-postgres` | None |
| **Audit trail** | JSONL per run with `parent_run_id` rollups; helper + delegate + tool + capture lines all link back | Dashboards in Letta UI / cloud | Mem0 dashboards | LangSmith (hosted) | Build it |
| **Cost guardrails** | First-class — daily / monthly caps, threshold warnings, fallback action, `critical=True` override, tree-cap across delegates | Per their pricing model | Per their pricing model | Not built into core OSS | Build it |
| **Multi-agent coordination** | Role × project cascade defined in spec/06 | Multi-agent shared memory blocks | Agent-shared memory pools | LangGraph: graph-based orchestration (more flexible) | Build it |
| **Numbered, locked spec** | 23 docs in `docs/spec/` (+ 2 RFCs) | API + concept docs | API + concept docs | API reference + concept docs | None |
| **Reference runtime** | Python, macOS / Linux primary | Python (server) + multi-language clients | Python (OSS) + multi-language clients | Python + JavaScript | Whatever |

**Where the alternatives win:**

- **Letta** has the polished hosted-service UX, multi-language clients, and a more mature multi-agent shared-memory primitive.
- **Mem0** has stronger memory-retrieval optimization (embeddings + retrieval research is their core focus); if memory quality is the bottleneck, evaluate them directly.
- **LangGraph** has more flexible graph-based orchestration and the **LangSmith** observability stack is broader than any single project's audit trail can replicate.
- **Direct SDK** wins when your problem is so domain-specific that any framework's structure is overhead.

**Where Atomic Agents wins:**

- **Markdown-source-of-truth, human-editable.** Operators can edit persona / tools / memory from any text editor or Obsidian without a vendor app.
- **No required server.** The framework is "files + Python." A complete agent runs on a laptop with zero infrastructure.
- **Spec-level file layout.** 23 numbered docs lock the contract (plus 2 RFCs in progress); conformance is testable; alternate implementations are possible.
- **Crash-safe writes by default.** `temp file + fsync + rename + parent-dir fsync` for every mutation; an interrupted run leaves recoverable artifacts, not corruption.
- **Cost story is structural, not bolted on.** Daily / monthly caps + tree-cap for delegations + per-call cost reservation for helper batches + a `critical=True` override that's part of the API, not a per-vendor workaround.

---

## The spec is the product

`atomic-agents-stack` is a **spec** for vault-native AI agents, plus one **reference implementation** in Python. The spec is the central artifact; anyone can build agents to the spec without using this code.

Start at [`docs/README.md`](docs/README.md) for the spec entry point. The 23 locked spec docs (plus 2 RFCs) in [`docs/spec/`](docs/spec/) cover:

- [01 — Anatomy](docs/spec/01-anatomy.md) — file layout, persona, memory, wiki, journal, log
- [02 — Atomic Memory](docs/spec/02-atomic-memory.md) — Notes + Wiki + INDEX-driven recall
- [03 — File formats](docs/spec/03-file-formats.md) — frontmatter schemas + filename conventions
- [04 — Runtime assembly](docs/spec/04-runtime-assembly.md) — canonical load sequence
- [05 — Capture rules](docs/spec/05-capture-rules.md) — when and how agents write to memory
- [06 — Multi-agent projects](docs/spec/06-multi-agent-projects.md) — role × project cascade
- [07 — Research foundations](docs/spec/07-research-foundations.md) — lineage and prior art
- [08 — Evaluation](docs/spec/08-evaluation.md) — rubrics + LLM-as-judge framework
- [09 — Cost & observability](docs/spec/09-cost-observability.md) — pricing, dashboard, guardrails
- [10 — Helpers](docs/spec/10-helpers.md) — cheap-LLM workers for transformation subtasks
- [11 — Tuning](docs/spec/11-tuning.md) — eval-driven self-improvement
- [12 — Goals & intent](docs/spec/12-goals-and-intent.md) — goal-driven agents
- [13 — Research integrity](docs/spec/13-research-integrity.md) — citations + factual accuracy
- [14-19](docs/spec/) — capture markers, delegation, dreams, skills, MCP, alternative-runtime contracts
- [20 — Memory backend protocol](docs/spec/20-memory-backend.md) — the protocol-pattern moat
- [27 — Doctor](docs/spec/27-doctor.md) — preflight verification

Each spec doc is locked when the implementation matches and tests pass. Spec changes that imply implementation changes get filed as GitHub issues. **Spec docs separate shipped behavior from explicit future / deferred boundaries** — sections that describe behavior not yet implemented are explicitly marked as such, not silently aspirational.

---

## Backend protocols — the scaling story

The framework is moving toward swappable backends layer by layer. The shape: a Python `Protocol` for each primitive that touches storage, a filesystem-default implementation, capability advertisement, and a conformance test suite. Same agent definitions, same `call()` flow, same audit trail — different backends registered.

| Backend | Status | Spec |
|---|---|---|
| `MemoryBackend` | ✅ Shipped (v0.10.0) | [`spec/20-memory-backend.md`](docs/spec/20-memory-backend.md) |
| `LLMBackend` | ✅ Shipped (v0.13.0) | [`spec/31-llm-backend.md`](docs/spec/31-llm-backend.md) |
| `JudgeBackend` | ✅ Locked at [#112](https://github.com/dep0we/atomic-agents-stack/issues/112) PR 4. `PolicyJudge` + `LLMJudgeBackend` reference impls; opt-in dispatch + `atomic_action` proposal marker; two-judge ensemble + per-judge audit events; `judges.md` operator config + cascade-aware project floor; ESCALATE state machine (PENDING-file writer + operator resolution polling + auto-decide timeout + inline Approved execution); REVISE state machine (judge-driven amendment + second-judgment cycle + operator `### Revised by <op>` with embedded amendment YAML + class-upgrade re-judge gate). Conformance suite (`tests/test_judge_protocol_conformance.py`) gates the spec lock. PR 5a ([#112](https://github.com/dep0we/atomic-agents-stack/issues/112) unreleased): `escalation.fallback_on_timeout` widens to per-class dict form; auto-decide resolves policy from PENDING frontmatter `action_class`. PR 5b ([#112](https://github.com/dep0we/atomic-agents-stack/issues/112) unreleased): strict JSON-Schema validation of amended `tool_arguments` via opt-in `[validation]` extra (`validation: strict` in `judges.md`); concludes the #112 arc-with-amendments | [`spec/28-judge-layer.md`](docs/spec/28-judge-layer.md) |
| `LockBackend` | ✅ Locked at [#60](https://github.com/dep0we/atomic-agents-stack/issues/60) PR 4. `FilesystemLockBackend` (POSIX `fcntl.flock` advisory) + `RedisLockBackend` (single-instance Redis advisory lock + atomic Lua release/renew + daemon heartbeat at TTL/3 + `LockLost` surfacing) reference impls. `scope(sub_path)` Protocol method lets operators pass ONE backend; framework re-scopes for dream + memory paths. Operator override via `ATOMIC_AGENTS_LOCK_BACKEND` + `ATOMIC_AGENTS_LOCK_BACKEND_URL` env vars OR `AtomicAgent(..., lock_backend=...)` constructor kwarg. `doctor.check_lock_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + credential-redacted URL output. `_locks.AgentLock` preserved as a deprecation shim (sunset v1.0). Closes the multi-host cliff: operators on Cloud Run / Kubernetes / gizmo can now run atomic-agents against a Redis advisory-lock backend instead of per-host POSIX `fcntl.flock`. Conformance suite parametrizes across both backends. | [`spec/21-lock-backend.md`](docs/spec/21-lock-backend.md) |
| `LogBackend` | ✅ Locked at [#61](https://github.com/dep0we/atomic-agents-stack/issues/61) PR 4. `FilesystemLogBackend` (JSONL-on-disk; preserves the legacy `<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl` artifact byte-for-byte via `_io.atomic_append_jsonl`) + `SQLiteLogBackend` (stdlib `sqlite3`, no optional extra; six indexes; WAL journal mode + per-thread connections for multi-process append safety on local filesystems; aggregation pushdown via SQL `GROUP BY` + SQLite JSON1 `json_extract` for `extra`-field group_bys; index-driven `delete_older_than`; idempotent `INSERT OR IGNORE` cold-start init) reference impls. `LogQuery.agent_name` filter for shared-backend cross-agent isolation with lenient match for legacy records (records without `agent_name` match any filter — filesystem per-agent-dir scoping is the natural isolation primitive). Operator override via `ATOMIC_AGENTS_LOG_BACKEND` + `ATOMIC_AGENTS_LOG_BACKEND_URL` env vars OR `AtomicAgent(..., log_backend=...)` / `OutcomeRunner(..., log_backend=...)` / `DreamRunner(..., log_backend=...)` constructor kwargs (always wins; threads through to internal sub-agents). `doctor.check_log_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + stats probe (records_today / records_this_month) + URL-credential redaction. Implementer contract for queryable backends documented in spec/22 §"Implementer contract for queryable backends" — future Postgres / Datadog / Loki / Cloud Logging adapters mirror the SQLite reference's shape. Closes the dashboard-perf cliff: operators on Cloud Run / Kubernetes can pin SQLite for O(log N) indexed query/aggregate/retention. Conformance suite parametrizes across both backends. | [`spec/22-log-backend.md`](docs/spec/22-log-backend.md) |
| `PersonaBackend` | Planned | [`#62`](https://github.com/dep0we/atomic-agents-stack/issues/62) |
| `AgentProfileBackend` | 🟡 In progress — PR 3 of 4 landed ([#63](https://github.com/dep0we/atomic-agents-stack/issues/63)): `FilesystemAgentProfileBackend` + `SQLiteAgentProfileBackend` reference impls, snapshot trio (JSON-based), parametrized conformance suite. PR 4 locks spec/24 + adds the implementer contract. | [`spec/24-agent-profile-backend.md`](docs/spec/24-agent-profile-backend.md) |
| `ToolRegistryBackend` | Planned | [`#64`](https://github.com/dep0we/atomic-agents-stack/issues/64) |
| `CorpusBackend` | Planned | [`#65`](https://github.com/dep0we/atomic-agents-stack/issues/65) |
| `PolicyBackend` | Planned | [`#89`](https://github.com/dep0we/atomic-agents-stack/issues/89) |

**v1 direction:** a home user runs filesystem-everything (today). An organization runs the same agent definitions over Postgres / Redis / SQLite-Datadog / behind an HTTP service, with a fleet of orchestrated roles — *once the remaining backend protocols ship*. Today, **five** backend protocols are non-filesystem-default-ready (`MemoryBackend`, `LLMBackend`, `JudgeBackend`, `LockBackend`, `LogBackend`); the four remaining (`Persona`, `AgentProfile`, `ToolRegistry`, `Corpus`) are roadmap. See [`docs/architecture.md`](docs/architecture.md) for the mental model and [`docs/TENSIONS.md`](docs/TENSIONS.md) for the architectural tensions this scaling story has to survive.

---

## Judge layer (opt-in)

The **judge layer** is a pre-action validation surface. Before any side-effectful tool call executes, a separate `JudgeBackend` inspects a structured action proposal — built from the LLM's `tool_use` block plus an `atomic_action` side-channel marker the actor emits in the same turn — and returns ALLOW / BLOCK / REVISE / ESCALATE. Every judgment writes a JSONL audit event carrying the proposal hashes, the outcome, the policy version, and the judge's reason. The full design is in [`docs/spec/28-judge-layer.md`](docs/spec/28-judge-layer.md); this README section is the orientation.

The layer is **fully opt-in**. Existing deployments see no judge invocation until they drop a `judges.md` file in the agent root (or set `AGENT_JUDGE_ENABLED=1` for the hardcoded defaults).

### Minimum-viable `judges.md`

```markdown
# Judges — <agent>

```yaml
backend: rules
class_policy:
  read_only: bypass
  reversible_write: allow_with_audit
  external_side_effect: judge_required
  high_risk: escalate
```
```

That single file opts the agent into the two-judge ensemble: `PolicyJudge` (rule-engine, microseconds, always-on) then `LLMJudgeBackend` (OpenAI `gpt-5-nano` by default, lazy-skipped if no `OPENAI_API_KEY` resolves so Claude-only deployments aren't blocked).

### The four class policies

Every tool call is classified into one of four action classes per `tools.md`. The `class_policy` block in `judges.md` says what the framework does with each class:

| Policy              | What it means                                                                                                                                   |
|---------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| `bypass`            | Skip the judge entirely. No proposal, no judgment event, no audit line.                                                                          |
| `allow_with_audit`  | Run the judge; **always allow**; write the full judgment event to the audit trail. Surface for "I want to see the judge's opinion without it gating actions yet." |
| `judge_required`    | Run the judge; its outcome is enforced. BLOCK refuses; ALLOW executes; REVISE/ESCALATE follow spec/28 semantics.                                 |
| `escalate`          | Pause for operator approval. Framework writes a PENDING file to `vault/escalations/<class>/<proposal_id>.md`; `agent.call()` returns `Response(deferred=True, escalation_queue_ids=[...])`; operator writes a resolution block; the next `call()` (or an explicit `agent.poll_escalations()`) reads the decision and executes Approved actions inline. See [Escalation queue](#escalation-queue) below. |

Strictness ordering: `bypass < allow_with_audit < judge_required < escalate`. Unspecified classes default-fill to safe values (`read_only: bypass`, everything else `judge_required` or `escalate` for `high_risk`).

### Cascade-aware project floor

In multi-agent project layouts, `<project>/judges.md` is the **non-relaxable floor** per spec/28 §408. A delegate's own `<agent>/judges.md` may strengthen per-class policy but cannot weaken it below the floor; attempts raise `JudgePolicyInvalid` at load. Classes the delegate didn't override inherit the floor's value (visible in `ClassPolicySnapshot.source` as `"floor"` vs `"judges.md"` vs `"default"`).

This pattern lets a project lead drop one `judges.md` at the project root that guarantees a minimum-policy floor across every agent in the project, with each agent free to strengthen further.

### `failure_policy` — fail-closed by default

When a judge errors (timeout, budget exhausted, malformed proposal), the framework consults `failure_policy` to decide the enforcement outcome. **Default is `block` for every exception type** — operators must explicitly opt into looser behavior.

Two shapes are accepted. **Flat** (one fallback for every class):

```yaml
failure_policy:
  JudgeUnavailable: block
  JudgeBudgetExhausted: block
```

**Nested per-class** (different fallback per `(action_class, exception)` pair):

```yaml
failure_policy:
  read_only:
    JudgeUnavailable: allow      # read_only actions tolerate judge outages
  high_risk:
    JudgeUnavailable: escalate   # high_risk actions escalate to operator
```

The parser auto-detects the shape — top-level keys are either exception names (flat) or action class names (nested). Mixing shapes raises `JudgePolicyInvalid`.

### Escalation queue

When the judge ensemble returns ESCALATE — or when `class_policy.<X>=escalate` synthesizes the outcome at the framework layer, or when a judge exception is mapped to `escalate` via `failure_policy` — the action is paused for operator review. The framework writes a PENDING file to `<agent_root>/vault/escalations/<action_class>/<proposal_id>.md` (atomic, frontmatter + full `ActionProposal` serialized as fenced YAML + the judge's reason), and `agent.call()` returns `Response(deferred=True, escalation_queue_ids=[id1, id2, ...])`. The actor's run terminates immediately — ALLOWed tool_uses in the same turn still execute and their results land in `Response.tool_calls`, but the multi-turn loop does not continue.

The operator resolves a PENDING by editing the file in any text editor (Obsidian, vim, VS Code) and writing exactly one resolution block at the bottom. Header grammar is **strict** — h3 + exact-case verb + the literal word `by` + a non-empty operator name:

```
### Approved by alice
resolved_at: 2026-05-13T09:14:22Z
note: Reviewed — sender list is correct, attachment is the public report.
```

The five resolution verbs are `Approved`, `Denied`, `Redacted`, `Revised` (PR 3c — operator amends the proposal with an embedded `amendment:` YAML block; see [Revise — judge- and operator-driven amendments](#revise--judge--and-operator-driven-amendments) below), and `Auto-decided` (the framework writes the auto-decide block itself when `escalation.auto_decide_after_seconds` elapses). Header typos surface as doctor warnings rather than silent denials.

On the next `agent.call()` (or when an operator runs `agent.poll_escalations()` directly), the framework throttle-checks (`escalation.resolution_poll_cycle_seconds`, default 60s), scans the queue, re-verifies the proposal body's `arguments_hash` (mismatch → `proposal_body_tampered`, refuses execution), and for Approved resolutions re-verifies `tool_definition_hash` against the current tool registry before executing the bound action inline. Concurrent pollers race a `.<proposal_id>.resolved-emitted` sidecar via `O_CREAT|O_EXCL` for exactly-once audit emit; auto-decide writes use a sha256 compare-and-swap so an operator edit-in-progress always wins. See [`docs/deployment/judges-md.md`](docs/deployment/judges-md.md#escalation-queue) for the full grammar, audit shape (`enforcement_action`, `synthesis_source`, `triggered_by`), and the `approved_stale_tool_definition` refusal path.

### Revise — judge- and operator-driven amendments

`REVISE` is the third outcome the framework acts on (after `ALLOW` and `BLOCK`; `ESCALATE` covered above). Two cycles ship in PR 3c:

**Judge-driven.** An ensemble judge returns `Judgment(outcome=REVISE, amendment=ProposalAmendment(judge_note=..., tool_arguments=..., ...))` — for instance, *"send this email but strip the attachment"* or *"open the PR as draft, not for merge."* The framework merges the amendment with the original proposal (recomputing `classification` from the possibly-new `tool_name`, carrying `reason` + `authorization` verbatim per spec/28:271, appending evidence non-destructively), re-validates against the tool registry + write-path policy, and runs a **second judgment** against the amended proposal through the same ensemble. Bounded at `max_revise_iterations=1` per spec/28:276 — the second judgment must return `ALLOW` to execute; `BLOCK` refuses; `REVISE` produces `revise_loop_exhausted_blocked`. Operators see this path as a sequence of audit events (`revise_pending_second_judgment` → `revise_executed`) — no PENDING file is written.

**Operator-driven.** Operators resolving an ESCALATEd `### Revised by <op>` block with an embedded `amendment:` YAML payload follow the same primitives. The framework parses the YAML, applies the amendment, and **gates on the recomputed classification** (NOT the PENDING file's original `action_class`): `high_risk` after amendment → fresh ensemble re-judgment; other classes → schema/policy validation alone is sufficient. This means an operator who swaps `tool_name` to upgrade `reversible_write` → `high_risk` cannot skip the second-judgment eyes by phrasing the original proposal as a lower class. `re_judged: bool` on the executed audit event reports whether the ensemble re-ran; it is framework-set, not operator-supplied. Invalid amendments (missing YAML, malformed YAML, unknown fields, unknown tool, non-dict args, write-path violation) emit `operator_revise_invalid_amendment` and refuse execution. See [`docs/deployment/judges-md.md`](docs/deployment/judges-md.md#operator-revise-resolution) for the full block format, amendment field table, class-upgrade gate semantics, and audit shape.

PR 3c shipped the weakened-validation default (tool registration + dict shape + `arguments_hash` recompute). PR 5b (unreleased) closes spec/28:274 with an opt-in strict path: install `pip install 'atomic-agents-stack[validation]'` and set `validation: strict` in `judges.md` to run `jsonschema.validate(args, registered.input_schema)` after the weakened checks. The default remains `weakened` for any `judges.md` that omits the field — operators see no behavior change on upgrade.

### See also

- [`docs/deployment/judges-md.md`](docs/deployment/judges-md.md) — the full operator runbook for `judges.md` (every field, every error message, examples).
- [`docs/spec/28-judge-layer.md`](docs/spec/28-judge-layer.md) — locked at #112 PR 4: ESCALATE + REVISE state machines, audit-event schema, specialist composition, failure-mode catalog, conformance suite reference. PR 5a amends §"Escalation queue" with per-class `fallback_on_timeout` examples and parser rules. PR 5b amends §"REVISE state machine" with the strict JSON-Schema validation path and the `[validation]` extra install gate.

---

## Deployment shapes

Eight operator runbooks for the common deployment paths. Pick the one that matches what you're doing:

- [`docs/deployment/obsidian.md`](docs/deployment/obsidian.md) — running the framework against an Obsidian-synced vault: ignore patterns, `.versions/` trade-offs, sync race conditions, conflict copy recovery
- [`docs/deployment/programmatic.md`](docs/deployment/programmatic.md) — embedding in Python: the `Agent` + `call()` public surface, the complete public exception table, three worked examples
- [`docs/deployment/disaster-recovery.md`](docs/deployment/disaster-recovery.md) — symptom-organized runbook: stale locks, mid-run crashes, corrupted INDEX, migration rollback, memory write races
- [`docs/deployment/cost-guardrail-sizing.md`](docs/deployment/cost-guardrail-sizing.md) — picking daily/monthly caps + cap action; seven role archetypes with recommended starting values
- [`docs/deployment/judges-md.md`](docs/deployment/judges-md.md) — authoring `judges.md` to configure the judge layer: class policy, cascade-aware project floor, `failure_policy` shapes
- [`docs/deployment/versioning.md`](docs/deployment/versioning.md) — SemVer policy; what counts as Major / Minor / Patch
- [`docs/deployment/upgrading.md`](docs/deployment/upgrading.md) — operator upgrade runbook + migration runner usage
- [`docs/deployment/release-runbook.md`](docs/deployment/release-runbook.md) — maintainer-facing `/ship` runbook: two-mode workflow (PR-level vs. release cut), local gstack patch, operator manual surface check

---

## What's shipped

| Component | Shipped |
|---|---|
| `AtomicAgent` runtime | ✅ v0.1.0 |
| Persona loading (IDENTITY, SOUL, USER) | ✅ v0.1.0 |
| `memory/` + `wiki/` INDEX-driven recall | ✅ v0.1.0 |
| Helper-mediated atomic captures (fenced JSON) | ✅ v0.1.0 |
| Multi-tier cost guardrails (50% / 80% / 100%) | ✅ v0.1.0 |
| Helper calls — sequential + parallel | ✅ v0.1.0 |
| Anthropic / OpenAI / Moonshot Kimi routing | ✅ v0.1.0 |
| File locking with stale-lock recovery | ✅ v0.1.0 |
| Schema validation incl. date-suffix filenames | ✅ v0.1.0 |
| Cost dashboard (HTML, global + per-agent) | ✅ v0.1.0 |
| Optional local dashboard server | ✅ v0.1.0 |
| Eval runner — `atomic_agents.eval` | ✅ v0.9.0 |
| Tuning analyzer — `atomic_agents.tuning` | ✅ v0.9.0 |
| Goal manager — `atomic_agents.goal` | ✅ v0.9.0 |
| Schema migration runner — `atomic_agents.migrate` | ✅ v0.9.0 |
| Tool-call captures (Path 1) | ✅ v0.9.0 |
| Multi-agent project cascade loader — `atomic_agents._cascade` | ✅ v0.9.0 |
| Helper provenance preservation | ✅ v0.9.0 |
| Research integrity layers 2 + 3 | ✅ v0.9.0 |
| Claude Code skill wrappers — `extras/claude-code-skills/` | ✅ v0.9.0 |
| Spec docs in repo — `docs/` | ✅ v0.9.0 |
| CI (Python 3.11 + 3.12 matrix) | ✅ v0.9.0 |
| MCP (Model Context Protocol) client — `atomic_agents.mcp` | ✅ v0.10.0 |
| MemoryBackend protocol + FilesystemBackend default — `atomic_agents.memory` | ✅ v0.10.0 |
| `atomic-agents doctor` preflight CLI — `atomic_agents.doctor` | ✅ v0.10.0 |
| SemVer policy + upgrade runbook — `docs/deployment/` | ✅ v0.10.0 |
| Obsidian-backed deployment guide — `docs/deployment/obsidian.md` | ✅ v0.11.0 |
| Programmatic invocation guide + public exception table — `docs/deployment/programmatic.md` | ✅ v0.11.0 |
| Disaster recovery runbook — `docs/deployment/disaster-recovery.md` | ✅ v0.11.0 |
| Cost guardrail sizing guidance — `docs/deployment/cost-guardrail-sizing.md` | ✅ v0.11.0 |
| LLMBackend protocol + Anthropic/OpenAI/Moonshot reference impls — `atomic_agents.llm` | ✅ v0.13.0 |

See [CHANGELOG.md](CHANGELOG.md) for per-version detail.

---

## Versioning & upgrades

`atomic-agents-stack` follows [SemVer](https://semver.org) with project-specific rules for what counts as a Major / Minor / Patch change. **Pre-1.0, Minor releases may contain breaking changes** — always read the release notes before upgrading.

- [`docs/deployment/versioning.md`](docs/deployment/versioning.md) — full SemVer policy
- [`docs/deployment/upgrading.md`](docs/deployment/upgrading.md) — operator upgrade runbook

Every release lands as a `vX.Y.Z` git tag plus a GitHub Release with the CHANGELOG entry verbatim. Breaking changes get a `### BREAKING` callout in that entry.

---

## Configuration

### `ATOMIC_AGENTS_ROOT`

Tells the framework where to find your agent vault. **Default: `~/docs/agents`** (suitable for Obsidian-backed deployments; see [`docs/deployment/obsidian.md`](docs/deployment/obsidian.md)).

```bash
export ATOMIC_AGENTS_ROOT=/path/to/your/agents
```

### API keys

The framework looks for keys in this order:

1. **Environment variables** — `ATOMIC_AGENTS_ANTHROPIC_KEY`, `ANTHROPIC_API_KEY`
2. **macOS Keychain** — `security add-generic-password -a $USER -s atomic-agents-anthropic -w sk-ant-...`
3. **`~/.config/atomic_agents/keys.json`** (chmod 600):
   ```json
   {"anthropic": "sk-ant-...", "openai": "sk-...", "moonshot": "..."}
   ```

Same pattern for OpenAI (`atomic-agents-openai`) and Moonshot (`atomic-agents-moonshot`). Run `uv run atomic-agents doctor` to verify which lookup chain found your keys.

---

## Repository structure

```
atomic_agents/                  # the Python package
├── agent.py                    # AtomicAgent class — the main runtime
├── exceptions.py               # 27 public exception classes
├── types.py                    # shared dataclasses
├── cli.py                      # `atomic-agents` console script
├── doctor.py                   # preflight verification
├── migrate.py                  # schema migration runner
├── memory/                     # MemoryBackend protocol + filesystem default
├── dashboard/                  # cost & observability dashboard
├── mcp.py                      # MCP client (stdio transport)
├── _llm.py                     # provider routing (Anthropic / OpenAI / Moonshot)
├── _costs.py                   # pricing + multi-tier guardrails
├── _locks.py                   # per-agent flock with stale-lock recovery
└── _io.py                      # atomic file writes (temp + fsync + rename)

tests/                          # 1750 tests, all passing on Python 3.11 + 3.12

docs/
├── README.md                   # spec entry point
├── architecture.md             # mental model + design rationale
├── getting-started.md          # 15-minute clone-to-running-agent walk-through
├── spec/                       # 23 locked spec docs + 2 RFCs
├── implementation/             # build guides per runtime
├── deployment/                 # 8 operator runbooks (Obsidian, programmatic, disaster-recovery, cost-guardrail-sizing, judges-md, versioning, upgrading, release-runbook)
├── samples/caldwell/           # complete worked single-agent example
├── appendix/portability.md     # using Atomic Agents without Obsidian / on any OS
├── GOVERNANCE.md               # solo / small-team operator guide
├── TENSIONS.md                 # architectural tensions to protect
└── methodology.md              # working-methods retrospective

extras/                         # operational templates
├── claude-code-skills/         # SKILL.md wrappers for Claude Code
├── launchd/                    # macOS LaunchAgent .plist templates
└── cron/                       # crontab examples + portable wrapper script
```

---

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Run the full test suite
uv run pytest

# Run a specific test module
uv run pytest tests/test_capture.py -v
```

Before opening a PR, read [`CLAUDE.md`](CLAUDE.md) (the project's design ethos and 14 taste rules), [`docs/TENSIONS.md`](docs/TENSIONS.md) (architectural tensions to protect when changing code), and [`docs/methodology.md`](docs/methodology.md) (the practices that produced this codebase's quality). See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution flow.

---

## License

[MIT](LICENSE).

---

## Status

**v0.13.0, alpha.** Core runtime stable. 1750 tests passing on Python 3.11 / 3.12. **Five backend protocols shipped**: `MemoryBackend`, `LLMBackend`, `JudgeBackend` (locked at [#112](https://github.com/dep0we/atomic-agents-stack/issues/112) PR 4 with `tests/test_judge_protocol_conformance.py` parametrized over `PolicyJudge` + `LLMJudgeBackend`; PR 5a adds per-class `escalation.fallback_on_timeout`; PR 5b adds strict JSON-Schema validation of amended `tool_arguments` via the opt-in `[validation]` extra and concludes the #112 arc — both unreleased), `LockBackend` (locked at [#60](https://github.com/dep0we/atomic-agents-stack/issues/60) PR 4 with `FilesystemLockBackend` + `RedisLockBackend` reference impls and parametrized conformance), and `LogBackend` (locked at [#61](https://github.com/dep0we/atomic-agents-stack/issues/61) PR 4 with `FilesystemLogBackend` + `SQLiteLogBackend` reference impls, `LogQuery.agent_name` cross-agent isolation filter, and parametrized conformance). **`AgentProfileBackend` arc ([#63](https://github.com/dep0we/atomic-agents-stack/issues/63)) is 3 of 4 PRs landed (unreleased)**: `FilesystemAgentProfileBackend` + `SQLiteAgentProfileBackend` reference impls, JSON-based snapshot trio implementation, parametrized conformance suite, `supports_skills` capability dimension. PR 4 locks spec/24 + adds the implementer contract for registry-backed backends. The judge layer is opt-in — existing deployments see no judge invocation unless `judges.md` is in the agent root or `AGENT_JUDGE_ENABLED=1` is set. The remaining protocol-pattern roadmap (`PersonaBackend` / `ToolRegistryBackend` / `CorpusBackend`) is what v1.0 closes; the surface stabilizes there. Pre-1.0 — Minor releases may contain breaking changes (see [`docs/deployment/versioning.md`](docs/deployment/versioning.md)). Single-maintainer project; reference implementation that anyone can use, fork, or extend.

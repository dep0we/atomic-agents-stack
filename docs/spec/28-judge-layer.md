# spec/28 — Judge Layer

> Status: **RFC** (origin: #110). This spec describes a planned surface, not current
> behavior. It is the design hypothesis the maintainer is committing to before
> implementation. Sections will be revised based on adversarial review. The
> spec locks (drops the RFC marker) when the first reference implementation
> ships and the conformance suite passes.
>
> Cross-links: spec/01 (anatomy), spec/03 (file formats), spec/04 (runtime assembly), spec/05 (capture rules), spec/08 (evaluation), spec/13 (research integrity), spec/15 (delegation), spec/17 (tools), spec/19 (MCP), spec/20 (memory backend — protocol pattern template)
>
> Related backends: PolicyBackend (#89), LLMBackend (#87), LogBackend (#61)

## RFC vs locked spec — what this convention means

The framework's normal spec discipline (rule #10) is *spec describes what is true today*. This document is an exception: it is the agreed-on design for a surface that does not exist yet. Treating it as a locked spec would be dishonest (the implementation is not in the codebase to verify against); skipping the spec entirely would be worse (the design has too many load-bearing decisions to defer to commit time).

Convention for RFC specs:

- The `Status: **RFC**` marker above is required at the top.
- Each non-trivial decision states the **decided** answer in the body, not in a deferred "open question." Open questions are reserved for things genuinely below the threshold of needing resolution before implementation begins.
- The spec is revised in rounds (cross-model adversarial review) before merge. Revisions are captured in the spec itself, not in spec-shaped issue comments.
- When the first reference implementation ships, the doctor (`spec/27`) verifies the spec matches the code, the `Status` marker drops to `implemented`, and any drift between spec and code becomes a follow-up issue per rule #10.

This convention applies only to *new framework surfaces large enough to require design before code*. Most spec docs continue to describe shipped behavior.

## Overview

The **judge layer** is a pre-action validation surface. Before any side-effectful tool call executes, a separate `JudgeBackend` inspects a structured **action proposal** assembled from the actor's tool_use block plus actor-supplied side-channel fields, returns one of four outcomes — **allow / block / revise / escalate** — and writes a **judgment event** to the audit trail.

The judge raises `tools.md` from advisory-in-some-runtimes to **gated-at-runtime when the judge is enabled**. In runtimes without the helper or with `judges.md` absent, `tools.md` remains advisory exactly as today (per `spec/01` §"Policy vs enforcement"). The judge does not change the runtime story for runtimes the framework cannot reach.

## Why this exists

Atomic Agents today has:

- `tools.md` as policy — but advisory in some runtimes
- The eval framework (`spec/08`) — judges agent **output quality after the fact**, not pre-action
- Cost guardrails with `critical=True` override — gates spend, not behavior
- Audit trail with `parent_run_id` rollups — records what happened, doesn't gate what happens next
- Memory→persona promotion (`spec/05`) — operator confirms before agent-generated memory becomes instruction-grade

There is no pre-action validation between actor and side-effectful tool call. The actor decides; the tool runs. As deployments move from a home user with one agent to an organization running a fleet, that unvalidated path becomes the structurally weakest link — the place where a misinterpreted instruction, a stale memory, or a specification-gaming actor produces an externally-visible mistake.

The judge fixes this by making the boundary first-class for runtimes that can carry the helper.

## Semantics

```
Actor (LLM)                Framework            Judge (LLM | rules)            Tool
   │                          │                       │                          │
   │── emits tool_use ───────▶│                       │                          │
   │── side-channel:          │                       │                          │
   │   reason / evidence ─────▶│                       │                          │
   │   authorization          │                       │                          │
   │                          │── assembles proposal──▶│                          │
   │                          │── binds tool_hash +    │                          │
   │                          │   args_hash +          │                          │
   │                          │   tool_call_id         │                          │
   │                          │                       │── inspects + decides     │
   │                          │                       │                          │
   │                          │◀── allow ─────────────│                          │
   │                          │── executes EXACT       │                          │
   │                          │   bound args ─────────────────────────────────▶│
   │                          │◀── result ────────────────────────────────────│
   │                                                                          
   │                          │◀── block(reason) ─────│   (no execution)
   │                                                                          
   │                          │◀── revise(amended) ───│                          │
   │                          │── re-validates amended:                          │
   │                          │   schema, write paths,                           │
   │                          │   class non-escalation,                          │
   │                          │   second judgment ──────▶│                       │
   │                          │◀── allow (or block) ──│                          │
   │                          │── executes amended ───────────────────────────▶│
   │                                                                          
   │                          │◀── escalate(queue_id)─│                          │
   │                          │── writes PENDING state │                          │
   │                          │   to escalation queue                            │
   │                          │── returns deferred result                        │
   │                          │   (cron context: waits or pauses;                │
   │                          │    interactive: returns to caller)               │
   │                          │── on operator decision:                          │
   │                          │   emits RESOLVED event                           │
   │                          │   executes (allow) | drops (block) | re-judges   │
```

Each judgment writes a JSONL judgment event carrying `parent_run_id`, the proposal, the decision, the reason, the model + policy version that judged it, the bound tool/arguments hashes, and the latency + token cost incurred.

## Action proposal

The proposal is assembled by the **framework** from the LLM's `tool_use` block plus structured **side-channel fields** the actor emits in the same turn (mirroring the `atomic_capture` marker pattern per `spec/05`). The judge inspects the proposal — never raw tool arguments outside the proposal binding.

Why this split: pure framework introspection loses the actor's *reason* (which is the most important judgment input). Pure actor-builds-proposal adds latency and a failure mode (actor refuses to build a coherent proposal). The split lets the framework guarantee proposal-execution binding (TOCTOU defense) while the actor commits to reason/evidence/authorization in writing.

```python
@dataclass(frozen=True)
class ActionProposal:
    # Framework-introspected (from the tool_use block + runtime context)
    tool_name: str                          # the tool name being proposed
    tool_arguments: dict[str, Any]          # the proposed tool input (verbatim from tool_use)
    tool_call_id: str                       # unique id from the LLM provider
    tool_definition_hash: str               # sha256 of the registered tool definition
    arguments_hash: str                     # sha256 of canonicalized tool_arguments
    classification: ActionClass             # read_only | reversible_write | external_side_effect | high_risk
    classification_source: str              # "tools.md" | "mcp.md" | "default_unknown"
    actor_agent: str                        # the agent that proposed (may be a delegate)
    actor_run_id: str                       # the actor's run_id
    actor_model_id: str | None              # model that emitted the tool_use
    delegate_chain: list[str]               # coordinator → ... → actor, empty if not delegated
    loaded_skills: list[SkillRef]           # skills active when the proposal was made
    mcp_server: str | None                  # mcp server name if tool came from MCP, else None

    # Actor-supplied (via side-channel marker; required for side-effectful classes)
    reason: str                             # why the actor is proposing this
    evidence: list[Evidence]                # supporting memory/conversation refs
    authorization: Authorization            # who granted it, scope
    expected_consequence: str               # plain language
    reversibility: Reversibility            # reversible | reversible_with_artifact | irreversible
    rollback_path: str | None               # how to undo, if reversible
    target_audience: str | None             # "internal" | "external:<surface>" — for privacy judges

    # Framework-set after assembly
    proposal_id: str                        # unique id; primary key for judgment events
    proposal_ts: str                        # ISO-8601


@dataclass(frozen=True)
class Evidence:
    source: str                             # note name, conversation ref, skill name
    source_hash: str | None                 # sha256 of source content at time of citing
    claim: str                              # what this source supports
    provenance: Provenance                  # see memory provenance section


@dataclass(frozen=True)
class Authorization:
    granted_by: str                         # "operator" | "policy" | "delegated_from:<agent>"
    scope: str                              # plain language description
    granted_at: str                         # ISO-8601 of when granted
    expires_at: str | None                  # ISO-8601 if time-bounded


@dataclass(frozen=True)
class SkillRef:
    name: str
    file_hash: str                          # sha256 of the skill file at load time
```

For `read_only` actions, actor-supplied fields are not required (the judge bypasses by default; see Action classification). For all other classes, missing side-channel fields default the judgment to `BLOCK` with reason `proposal_incomplete` — the actor must declare intent to act.

### Proposal binding (TOCTOU defense)

The framework binds the proposal to the exact tool call via `tool_call_id`, `tool_definition_hash`, and `arguments_hash`. When the judge returns `ALLOW`, the framework executes the **bound** arguments — not arguments the actor might emit in a follow-up turn, not arguments mutated by another handler. If the tool definition has changed between proposal and execution (rare, but possible during hot-reload), the framework re-runs the judge against the new definition.

This closes the proposal/execution mismatch failure mode: judge approves args X, runtime executes args Y. The hashes are recorded in the judgment event for audit.

## Four-outcome model

```python
class JudgmentOutcome(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REVISE = "revise"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Judgment:
    outcome: JudgmentOutcome
    reason: str
    amended_proposal: ActionProposal | None  # set on REVISE; must satisfy revise constraints
    escalation_queue_id: str | None          # set on ESCALATE
    judge_id: str
    policy_version: str                      # sha256 of tools.md + judges.md at decision time
    model_id: str | None
    latency_ms: int
    cost_usd: float | None
```

### Allow

Proceed with the **bound** action. The actor's run continues; the multi-turn loop incorporates the tool result on the next turn.

### Block

Refuse the action. The actor receives the reason via tool_result; the multi-turn loop continues; the actor may propose a *different* action in a subsequent turn. Blocks are the strongest signal — operators expect them to be rare and reasoned. The judge's reason flows back to the actor as ground truth ("you proposed X; this is refused because Y; consider Z").

`BLOCK` is distinct from `REVISE` in shape: block returns no amended proposal; the actor must re-author from scratch. Revise hands the actor a pre-amended proposal that the framework re-validates before execution.

### Revise

The judge returns an amended proposal. The amended proposal **must** satisfy:

1. **Schema validation**: amended `tool_arguments` validates against the tool's registered JSON schema.
2. **Policy re-check**: amended args still pass `tools.md` write-path enforcement and any rule-engine specialist judges.
3. **Class non-escalation**: amended `classification` is `<=` the original (an `external_side_effect` proposal cannot be revised into a `high_risk` action).
4. **Preserved actor fields**: `reason`, `evidence`, `authorization` are preserved from the original; the judge may add to them but not rewrite.
5. **Second judgment**: the amended proposal passes through a fresh judgment cycle (typically the same backend, possibly a different specialist) before execution. The second judgment must return `ALLOW`; `REVISE` on a revised proposal returns `BLOCK` (no infinite revise loops).

The framework records both the original and the amended proposal in the judgment event. Operators can audit what the judge changed.

Revise is the empirically most-useful outcome for production failure modes that are not "agent did something forbidden" but "agent did something directionally right but unsafe as written." Common revisions:

- Strip an attachment from an email send
- Lower a spend limit before proceeding
- Open a PR as draft, not for merge

### Escalate

Pause the action. The framework writes a **PENDING** escalation record to `vault/escalations/<class>/<proposal_id>.md` containing the full proposal and the judge's reason. The actor's run returns with `Response.deferred=True` and the escalation_queue_id. Subsequent operator resolution writes a **RESOLVED** event linked to the PENDING record (escalation is modeled as a state machine, not a single verdict).

Resolution paths:

- **Operator approves** → framework executes the bound action; RESOLVED event records `approved` outcome
- **Operator blocks** → no execution; RESOLVED event records `denied` outcome
- **Operator revises** → operator supplies amended proposal; framework executes amended; RESOLVED event records `revised` outcome
- **Auto-decide timeout** → after `auto_decide_after` (per `judges.md`), framework applies the `fallback` policy (default `block` for `high_risk`, configurable per class)

The escalation queue is append-only at the framework level. Operators may resolve a PENDING by writing a RESOLVED event; they cannot delete a PENDING record without leaving a `redacted` marker (mirrors `spec/03` version redaction semantics).

## Where the judge sits in `agent.call()`

The judge runs **after the LLM emits a tool_use block** and **before the framework dispatches to the tool handler**. It is a strict pre-action gate.

```
agent.call() multi-turn loop:

  Iteration N:
    1. _check_cost_guardrails()                        # existing — actor budget
    2. _llm.call_llm()                                 # existing
    3. parse tool_uses + atomic_capture markers        # existing
    4. for each tool_use that is side-effectful:
         a. framework.assemble_proposal(               # NEW
              tool_use,
              actor_side_channel,
              runtime_context,
            )
         b. _check_cost_guardrails(judge_budget)       # NEW — judge budget gate
         c. judge.evaluate(proposal)                   # NEW
         d. record judgment event (JSONL)              # NEW
         e. branch on judgment.outcome:
              ALLOW    → execute bound args
              BLOCK    → skip; emit tool_result with reason
              REVISE   → re-validate amended; second judgment; execute if ALLOW
              ESCALATE → write PENDING; return Response with deferred=True
    5. process atomic_capture (per atomic_capture policy; see below)
    6. build follow-up message                         # existing
    7. continue loop                                   # existing
```

### `atomic_capture` interaction

`atomic_capture` writes to the agent's own memory, not external surfaces. It is a side effect in the data-flow sense but **not** a custom-tool side effect — per `spec/17`, it is handled separately from the tool registry.

Default: the judge does **not** gate `atomic_capture`. Memory writes remain governed by existing capture rules (`spec/05`) and write-path enforcement.

Opt-in: operators may set `judge_captures: true` in `judges.md` to route `atomic_capture` markers through the judge. This is intended for privacy-sensitive vaults where the operator wants pre-write validation that the captured content does not include credentials, PII, or content sourced from `read_only` paths that should not propagate to memory. The proposal classification for captures is always `reversible_write` (the memory layer supports `restore_version`); the proposal's `tool_name` is `atomic_capture` and `tool_arguments` is the capture marker payload.

## Delegation interaction (`spec/15`)

Atomic Agents delegation is one-level (coordinator → specialist; specialists do not delegate further). The judge layer respects this boundary:

- **Each agent has its own `judges.md`**. A coordinator's judge does not implicitly run against a delegate's actions. This matches the one-level principle and the `tools.md` non-inheritance rule from `spec/15`.
- **`delegate_chain` is recorded** in every proposal. The coordinator's run_id appears in the delegate's proposal `delegate_chain` field; the judge can read it to make policy decisions ("delegate of coordinator X is allowed to send emails; delegate of coordinator Y is not").
- **Authorization can flow**. A delegate's `authorization.granted_by` may be `"delegated_from:<coordinator_agent>"` with `scope` describing what the coordinator authorized. The delegate's judge inspects authorization shape as it would for any source.
- **Escalations bubble**. A PENDING escalation in a delegate's vault triggers an `escalation_propagated` event in the coordinator's run record so coordinator-level dashboards see fleet-wide pending escalations.

Operators who want a single judge policy across coordinator + delegates can put a shared `judges.md` in the project root and symlink (per `spec/06` multi-agent project patterns). The framework reads `judges.md` from the agent's own directory; symlinks resolve normally.

## MCP tool classification (`spec/19`)

MCP tools are discovered dynamically per server. `tools.md` may not list every MCP tool by name. The classification source is:

1. **Per-tool entry in `tools.md`** if present (highest precedence)
2. **Per-server default in `mcp.md`** if the server declares one (new field: `default_action_class`)
3. **Fallback**: `external_side_effect` for unknown tools

`mcp.md` gains an optional per-server section for classification:

```markdown
## github

server: npx -y @modelcontextprotocol/server-github
env:
  GITHUB_TOKEN: <secret>
default_action_class: external_side_effect

## tool overrides
read_repository_info: read_only
read_file: read_only
create_pull_request: high_risk
```

The judge's `JudgmentContext` includes `mcp_server`, so MCP-aware specialist judges can apply server-specific policies (e.g., GitHub PRs to `main` always escalate, regardless of class).

Read-only MCP tools (`search`, `read_*`, `list_*`) are the common case where defaulting to `external_side_effect` creates noise. The expected operator workflow: run with the default once, observe which MCP tools triggered the judge, declare overrides in `mcp.md` to mark them `read_only`. The doctor (`spec/27`) gains a `check_mcp_tool_classification` check that flags tools without explicit classification after first use.

## `JudgeBackend` protocol

Following the MemoryBackend template (`spec/20`).

### Module layout

```
atomic_agents/judge/
├── __init__.py        # registry: register_backend() / get_backend()
├── backend.py         # JudgeBackend Protocol + all dataclasses + exception taxonomy
├── proposal.py        # framework-side proposal assembly
├── llm.py             # LLMJudgeBackend (default)
└── rules.py           # RuleEngineJudgeBackend (composable specialist)
```

### Protocol surface

```python
@runtime_checkable
class JudgeBackend(Protocol):

    def evaluate(
        self,
        proposal: ActionProposal,
        context: JudgmentContext,
    ) -> Judgment: ...

    def supported_outcomes(self) -> set[JudgmentOutcome]: ...
    def supports_read_audit(self) -> bool: ...
    def supports_specialist_composition(self) -> bool: ...

    @property
    def judge_id(self) -> str: ...
    @property
    def policy_version(self) -> str: ...

    def close(self) -> None: ...
```

`JudgmentContext` is a runtime-assembly digest (per `spec/04`) scoped to the judge:

```python
@dataclass(frozen=True)
class JudgmentContext:
    agent_name: str
    persona_digest: PersonaDigest         # IDENTITY + SOUL + USER excerpt
    tools_md_entry: ToolPolicyEntry        # the tool's tools.md section
    judges_md: JudgesConfig                # parsed judges.md
    recent_runs: list[RunSummary]          # last N runs of this agent
    cited_notes: list[Note]                # the evidence the actor cited
    delegate_chain: list[str]              # mirror of proposal.delegate_chain
    loaded_skills: list[SkillRef]          # mirror of proposal.loaded_skills
```

The context is what the judge sees; the proposal is what the judge decides on. Splitting them keeps `evaluate` deterministic given the same (proposal, context) pair — important for the conformance suite.

### Capability advertisement

- `supported_outcomes()` — set of outcomes this backend can return. Pure rule engines may return only `{ALLOW, BLOCK}`. LLM judges typically return `{ALLOW, BLOCK, REVISE, ESCALATE}`. The runtime falls back from unsupported outcomes (e.g., if a judge that doesn't support `REVISE` would have revised, it `BLOCK`s with a reason instead).
- `supports_read_audit()` — whether the backend can be invoked on read-only actions for audit (without blocking).
- `supports_specialist_composition()` — whether multiple instances of this backend can compose in an `EnsembleJudge`.

### Exception taxonomy

```python
class JudgeError(Exception): pass
class JudgeUnavailable(JudgeError): pass              # backend cannot respond (timeout, network, provider outage)
class JudgePolicyInvalid(JudgeError): pass            # judges.md or tools.md cannot be parsed
class JudgeBudgetExhausted(JudgeError): pass          # cost cap hit
class JudgeProposalInvalid(JudgeError): pass          # proposal missing required fields for class
class JudgeAmendedProposalRejected(JudgeError): pass  # REVISE's amended proposal failed re-validation
```

Each exception type maps to a default judgment outcome via `judges.md` `failure_policy`:

```
failure_policy:
  JudgeUnavailable:      block        # fail-closed by default
  JudgeBudgetExhausted:  block
  JudgePolicyInvalid:    block
  JudgeProposalInvalid:  block
  JudgeAmendedProposalRejected: block
```

Operators may override per-exception-type per-class. Default is fail-closed (block) for all exceptions — the safety property is the point.

### Conformance suite

Mirrors `spec/20`'s suite shape. ~25 tests covering:

- `evaluate` returns a valid `Judgment` for each outcome in `supported_outcomes()`
- `evaluate` does not mutate the proposal (idempotency)
- Latency bounded by configurable timeout; timeout → `JudgeUnavailable`
- Concurrent `evaluate` calls do not corrupt state
- `policy_version` changes when policy source changes
- `JudgmentContext` mutation does not affect prior judgments
- Schema-invalid amended proposal → `JudgeAmendedProposalRejected`
- Class non-escalation enforced (revise cannot promote to `high_risk`)
- Second judgment on revised proposal cannot itself revise (no infinite loops)
- Exception taxonomy maps to outcomes per `failure_policy`
- Audit JSONL includes `tool_definition_hash`, `arguments_hash`, `tool_call_id`
- Read-audit mode bypasses block but still writes judgment event
- Escalation writes PENDING file with full proposal
- Resolution writes RESOLVED event linked by `proposal_id`
- `close()` is idempotent

Per-backend tests cover backend-specific behavior (LLM prompt formatting, rule-engine matching, ensemble voting).

### Registration

```python
from atomic_agents.judge import register_backend, get_backend

register_backend("llm", LLMJudgeBackend)
register_backend("rules", RuleEngineJudgeBackend)

# In agent runtime:
backend = get_backend(judges_config.backend_name)
```

Filesystem-default registration happens at import time. Third-party backends register via `register_backend()` in their own import path.

## Default reference implementation: `LLMJudgeBackend`

The default judge wraps `LLMBackend` (#87) with a system prompt assembled from `tools.md` + `judges.md` + the agent's persona digest. The user-turn payload is the structured `ActionProposal`. The model returns a structured `Judgment` (via tool-use response with a fixed `judgment` tool).

```python
class LLMJudgeBackend:
    def __init__(
        self,
        llm: LLMBackend,
        policy_source: Path,                # path to judges.md
        tools_source: Path,                 # path to tools.md
        model_id: str = "claude-haiku-4-5",
        timeout_ms: int = 5000,
        max_revise_iterations: int = 1,
    ): ...
```

Default model is the cheapest model that maintains judge quality in the eval suite. Operators override per-agent or per-action-class. `max_revise_iterations` is bounded at 1 by spec to enforce the "no infinite revise" rule.

### Why LLM-default, not rule-engine-default

Pure rule engines are deterministic and fast but cannot evaluate evidence quality, reversibility claims, or the alignment between stated reason and proposed arguments. LLMs close the gap between policy-as-prose and policy-as-runtime-check. Rule engines remain the right backend for clearly-deterministic decisions (e.g., `block if target_path not in write_paths`) and are intended to compose with the LLM judge via specialist composition.

## Action classification

Four classes, declared per-tool in `tools.md` (custom tools) or `mcp.md` (MCP tools):

| Class | Examples | Default judge policy |
|---|---|---|
| `read_only` | `read_file`, `search_notes`, `list_directory` | Bypass judge; no proposal required |
| `reversible_write` | `write_note(staged)`, `create_draft` | Judge optional; default-allow with audit |
| `external_side_effect` | `send_email`, `post_message`, `create_pr` | Judge required; default policy is judge-decides |
| `high_risk` | `delete_files`, `force_push`, `production_deploy` | Judge required; default policy is escalate |

Class is the strongest input to outcome selection. Tools without an explicit class default to `external_side_effect` (safest classification for "we don't know"). Operators promote unknowns to `read_only` after observing them, via `tools.md` or `mcp.md`.

## Specialist judges

A single monolithic judge may have weaker performance than composed specialists, each evaluating one axis. This is a design hypothesis, not an established result — the eval suite is what will measure it. The composition pattern exists so operators with the appetite to test it can do so without forking core.

Specialist axes:

- **AuthorizationJudge** — does the action have valid authorization scope?
- **PrivacyJudge** — does the action expose memory/skills marked sensitive to an external surface?
- **PolicyJudge** — does the action match `tools.md` write paths, allowlists, deny rules?
- **ReversibilityJudge** — does the actor's claimed reversibility match reality?
- **QualityJudge** — is the action coherent with the stated reason and evidence?
- **SecurityJudge** — does the action expose credentials, tokens, or paths outside the agent's scope?

Composition pattern: each specialist returns its own `Judgment`; an `EnsembleJudge` aggregates per declared policy:

```
any_block_blocks    → if any specialist blocks, ensemble blocks
any_escalate_escalates → if any specialist escalates, ensemble escalates
majority_revise     → if majority revises (with consistent amendments), ensemble revises
default             → allow
```

The default reference implementation ships with **PolicyJudge** (rule-engine, deterministic) + a single LLM-backed catch-all judge for the other axes. Full specialist composition is opt-in.

## Memory provenance integration

The judge consumes evidence; evidence cites memory notes; memory notes need provenance the judge can trust. Extend the existing note frontmatter (per `spec/03`):

```yaml
---
type: feedback
name: feedback-comm-style
provenance: legacy | observed | inferred | generated | confirmed | disputed | superseded
sources:
  - conversation_2026-04-12
  - rubric.md
confidence: high | medium | low
...
---
```

| Provenance | Treatable as | Notes |
|---|---|---|
| `legacy` | Unknown — judge weights as `inferred` and emits a `legacy_provenance_used` warning | Default for pre-existing notes |
| `observed` | Evidence — agent saw this in a source document | New writes default here |
| `inferred` | Weak evidence — agent reasoned from observations | Agent-set |
| `generated` | Weak evidence — agent proposed this as a lesson | Agent-set |
| `confirmed` | Instruction-grade — operator confirmed as authoritative | Operator-set only |
| `disputed` | NOT evidence — conflicts with another note; surfaced for resolution | Either set |
| `superseded` | NOT evidence — replaced by a newer note | Auto-set by `restore_version` |

The judge weights cited evidence by provenance. Crucially: **a `generated` note is not treated as instruction.** The agent does not get to teach itself a rule and then cite that rule as authorization for an action — the operator must `confirm` it first.

This extends the memory→persona promotion discipline (`spec/05`) from one specific lifecycle event to every memory read at runtime.

### Legacy migration

Notes without `provenance` default to **`legacy`**, not `observed`. The judge treats `legacy` as weak evidence and emits `legacy_provenance_used` warnings in the dashboard. Operators run a one-time `atomic-agents migrate-provenance` lint (added with the implementation) to walk legacy notes and confirm provenance per note. The migration is opt-in; deployments that never enable the judge never need to run it.

## `judges.md` operator config

Per-agent operator config lives in markdown, matching the framework's config aesthetic (rule #7). The parser recognizes specific `## <section>` headings; values within sections use the embedded-YAML convention already established for `model.md` (`spec/04`).

```markdown
# Judges — Caldwell

## Default judge

backend: llm
model: claude-haiku-4-5
timeout_ms: 5000
budget:
  daily_usd: 0.50
  monthly_usd: 10.00

## Class policy

read_only: bypass
reversible_write: allow_with_audit
external_side_effect: judge_required
high_risk: escalate

## Specialist composition

# Optional. Omit this section to use the default catch-all LLM judge.
policy: rules           # PolicyJudge runs first, fast
authorization: llm
privacy: llm
reversibility: llm
quality: llm
security: rules

aggregation: any_block_blocks

## Escalation queue

destination: vault/escalations/
operator_notification: email | webhook | none
auto_decide_after: 24h
fallback: block

## Failure policy

JudgeUnavailable:      block
JudgeBudgetExhausted:  block
JudgePolicyInvalid:    block
JudgeProposalInvalid:  block
JudgeAmendedProposalRejected: block

## Audit

judge_captures: false
read_audit_mode: false
```

### Parser rules

- Section headings (`## <name>`) are recognized as listed. Unknown headings are skipped with a `doctor` warning.
- Required sections: `Default judge`, `Class policy`. Missing required section → `JudgePolicyInvalid`.
- Values within sections are parsed as YAML. Invalid YAML → `JudgePolicyInvalid`.
- Class policy values are an enum: `bypass | allow_with_audit | judge_required | escalate`. Unknown value → `JudgePolicyInvalid`.
- Specialist composition is optional. Absent section → default catch-all LLM judge.
- Failure policy section is optional. Absent → all exceptions default to `block` (fail-closed).
- Per-tool overrides (rare, advanced) live in `tools.md`'s per-tool sections, not in `judges.md`.

Pure-YAML config files are refused per rule #7. Embedded YAML inside markdown is acceptable for structured fields. Markdown sections carry the same aesthetic as `tools.md` and `model.md`.

## Cost treatment

Per rule #4, every code path that calls an LLM has a cost gate. The judge is no exception.

- Judge calls flow through `_check_cost_guardrails` like any other LLM call
- `judges.md` declares a separate budget (`daily_usd`, `monthly_usd`) — judge spend is not subtracted from the agent's actor budget by default
- **`critical=True` does not bypass the judge.** Critical actions are eligible for `ALLOW` from the judge but the judge **always runs**. The audit trail is the point, and critical actions are exactly the ones that most need recording. This resolves the cost/critical tension explicitly: cost cap on the *actor* may be overridden by `critical=True`; cost cap on the *judge* cannot — it raises `JudgeBudgetExhausted` which the `failure_policy` resolves (default block)
- Specialist composition multiplies cost — operators see the multiplication in the dashboard before opting in

The discipline: judgment is expensive; the cost is the price of the safety property; operators decide how much they want to pay.

## Audit shape

Each judgment writes a JSONL line to the run log, carrying `parent_run_id` linking back to the actor's run. The current filesystem behavior:

```json
{
  "event": "judgment",
  "run_id": "judgment_20260512T143052_abc12345",
  "parent_run_id": "agent_20260512T143050_def67890",
  "proposal_id": "proposal_20260512T143052_xyz98765",
  "agent": "caldwell",
  "judge_id": "default-llm",
  "policy_version": "tools.md@sha256:...+judges.md@sha256:...",
  "proposal": { ... ActionProposal ... },
  "judgment": { ... Judgment ... },
  "binding": {
    "tool_call_id": "...",
    "tool_definition_hash": "sha256:...",
    "arguments_hash": "sha256:..."
  },
  "latency_ms": 412,
  "cost_usd": 0.00073,
  "ts": "2026-05-12T14:30:52Z"
}
```

The parent run record rolls up judgments inline (same shape as `helper_provenance`, `delegations`, `tool_calls`), so a single read of the agent's run record shows everything the actor and judge did, together.

**LogBackend (#61) is future work.** When LogBackend lands, the JSONL shape above remains as the filesystem-default `LogBackend` implementation; other backends translate it to their substrate without changing the framework-side audit invariants.

## Eval suite for judges

Per `spec/08`, the framework already has a rubric-driven eval surface for agent outputs. The judge needs its own, scoped to judgment quality. Judge eval cases live in `evals/judges/<case>.md` (under the existing `evals/` anatomy from `spec/01`, not a new top-level directory).

| Case | Measures |
|---|---|
| Known-good proposals → outcome=`ALLOW` | False-block rate |
| Known-bad proposals → outcome=`BLOCK` | False-allow rate (most dangerous) |
| Borderline proposals → outcome=`REVISE` or `ESCALATE` | Calibration |
| Policy-violation proposals | Policy adherence |
| Authorization-scope mismatches | Authorization rigor |
| Specification-gaming proposals (well-phrased but harmful) | Adversarial robustness |
| Latency under p95 budget | Latency SLA |
| Cost under daily budget | Cost SLA |
| Judge-unavailable scenarios | Fail-closed compliance |
| Amended proposals → re-validation | Revise rigor |

The eval suite is what makes the judge non-aspirational. A judge with no eval suite is a judge whose quality is opinion, not measurement.

## Failure modes & mitigations

### Proposal/execution mismatch (TOCTOU)

**Risk**: actor proposes safe args; judge allows; runtime executes different args (mutation between proposal and execution, hot-reload, handler interpretation drift).

**Mitigation**: every judgment binds to `tool_call_id`, `tool_definition_hash`, `arguments_hash`. Execution path uses the **bound** values, not whatever is in the actor's message stream at execution time. Hash mismatch at execution → re-judge.

### Judge unavailable

**Risk**: judge backend cannot respond (network failure, LLM provider outage, invalid structured judgment, queue write failure, timeout).

**Mitigation**: `failure_policy` in `judges.md` declares per-exception-type behavior. Default is fail-closed (block) for all five exception types. Operators may override per-class. The doctor's `check_judge_health` reports recent `JudgeUnavailable` rate.

### Correlated judgment

**Risk**: actor and judge share model family, training data, or biases — the judge approves what the actor would have approved itself.

**Mitigation**: default judge is a different model family than the default actor (e.g., Anthropic actor + OpenAI judge, or vice versa). Specialist composition adds rule-engine judges for clearly-deterministic axes. Operators may opt into ensemble composition for `high_risk` classes.

### Specification gaming

**Risk**: actor learns to phrase proposals that pass the judge regardless of underlying intent.

**Mitigation**: the eval suite includes adversarial cases. Judge outcomes are sampled by an offline reviewer (operator or downstream evaluator) at a configurable rate. Specification-gaming patterns flagged by the reviewer become new eval cases.

### Escalation drift

**Risk**: escalations land in front of operators; operators rubber-stamp them; the escalation surface decays into noise.

**Mitigation**: dashboard tracks operator approval rate per escalation class. When approval rate > 95% for an entire class, the dashboard flags it ("consider promoting this class to allow or revise"). When approval rate < 60%, the dashboard flags it ("consider promoting this class to block").

The escalation queue is append-only at the framework level. Operators cannot delete PENDING records without leaving a `redacted` marker. Rubber-stamping is visible.

### Latency / cost

**Risk**: judge calls double the per-action cost and add a turn of latency. For interactive agents this is a UX problem.

**Mitigation**: read-only actions bypass the judge by default. Rule-engine specialists handle deterministic axes in microseconds. The LLM judge runs once per side-effectful action, not per LLM turn. Per-class budgets cap exposure. Operators see the cost in the dashboard before opting in.

### Policy drift

**Risk**: `tools.md` and the judge's effective policy diverge over time — operators edit `tools.md`, the judge's prompt template doesn't pick up the change, decisions drift from declared policy.

**Mitigation**: `policy_version` is a hash of `tools.md + judges.md`; every judgment records the version it judged under. The dashboard surfaces stale-policy warnings when the judge's effective policy lags the file by > 1 hour. The doctor (`spec/27`) gains a `check_judge_policy_sync` check.

## Backward compatibility

Per rule #14, the judge layer is **opt-in**. Existing deployments continue to operate with no `judges.md` present; the runtime defaults to bypass-judge for all tool classes (equivalent to today's behavior).

When an operator adds `judges.md`, the framework registers the configured judge and begins evaluating. Per-class defaults are conservative (read-only bypasses; everything else is judge-decides) so a partial `judges.md` does not over-block.

The framework will not auto-enable the judge in a future minor release. Enabling it is an operator decision; the upgrade runbook documents how.

## Out of scope

This spec describes the **what** and the **where**. It does not pin:

- Concrete protocol method signatures beyond `evaluate` and capability advertisement — refined in the implementation PR
- The reference LLM prompt template — refined in the implementation PR
- The escalation queue's review UI — separate implementation issue
- Cross-agent (fleet-wide) judge policies — see PolicyBackend (#89)
- Streaming judgment for long-running actions — deferred until streaming use cases appear

## Open questions

These are *genuinely* below the threshold of needing resolution before implementation begins. Each has a tentative answer captured here; the implementation PR may revise either way without a spec re-review.

1. **Should there be a per-tool budget override?** Today `judges.md` declares an agent-level budget. Operators may want a per-class budget (`high_risk` gets $X/month; everything else shares $Y). **Tentative**: per-class budgets in `judges.md` v2.
2. **How does the judge interact with the dream pipeline?** Dreams run outside `agent.call()` and may produce capture markers without the runtime's tool-use loop. **Tentative**: dream pipeline reuses the same `judge_captures` switch from `judges.md`; the dream runner respects it identically to the live runtime.

## References

- `docs/spec/01-anatomy.md` §"Policy vs enforcement"
- `docs/spec/03-file-formats.md` (frontmatter schema; provenance extension; redaction semantics)
- `docs/spec/04-runtime-assembly.md` (digest assembly; reused for `JudgmentContext`)
- `docs/spec/05-capture-rules.md` (capture-marker discipline; mirrored for action proposals; `judge_captures` integration)
- `docs/spec/06-multi-agent-projects.md` (symlink patterns; shared `judges.md`)
- `docs/spec/08-evaluation.md` (LLM-as-judge for output quality; eval suite shape extended for judges)
- `docs/spec/13-research-integrity.md` (citation discipline; reused for evidence)
- `docs/spec/15-delegation.md` (one-level delegation boundary; delegate_chain field)
- `docs/spec/17-tools.md` (tools.md policy; action classification source)
- `docs/spec/19-mcp.md` (mcp.md; `default_action_class` field; MCP tool classification)
- `docs/spec/20-memory-backend.md` (Protocol pattern template; mirrored for JudgeBackend)
- `docs/spec/27-doctor.md` (extends with `check_judge_health`, `check_judge_policy_sync`, `check_mcp_tool_classification`)
- #87 LLMBackend (judge calls flow through this)
- #89 PolicyBackend (org-scale policy; composes with judges)
- #61 LogBackend (judgment events; same protocol)
- #110 RFC (this spec's origin)

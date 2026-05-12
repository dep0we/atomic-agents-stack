# spec/28 — Judge Layer

> Status: **planned** (RFC: #110)
> Cross-links: spec/01 (anatomy), spec/05 (capture rules), spec/08 (evaluation), spec/13 (research integrity), spec/17 (tools)
> Related backends: PolicyBackend (#89), LLMBackend (#87), LogBackend (#61)

## Overview

The **judge layer** is a pre-action validation surface. Before any side-effectful tool call executes, a separate `JudgeBackend` inspects a structured **action proposal** emitted by the actor, returns one of four outcomes — **allow / block / revise / escalate** — and writes a **judgment event** to the audit trail.

The judge is what makes `tools.md` enforceable at runtime, not merely advisory.

This spec describes a planned surface. It is not yet implemented. Implementation is tracked by follow-up issues filed after this spec merges.

## Why this exists

Atomic Agents today has:

- `tools.md` as policy — but **advisory in some runtimes** (per `spec/01-anatomy.md` §"Policy vs enforcement")
- The eval framework (`spec/08-evaluation.md`) — judges agent **output quality after the fact**, not pre-action
- Cost guardrails with `critical=True` override — gates spend, not behavior
- Audit trail with `parent_run_id` rollups — records what happened, doesn't gate what happens next
- Memory→persona promotion (`spec/05-capture-rules.md`) — operator confirms before agent-generated memory becomes instruction-grade

There is no pre-action validation between actor and side-effectful tool call. The actor decides; the tool runs. As deployments move from a home user with one agent to an organization running a fleet, that unvalidated path becomes the structurally weakest link — the place where a misinterpreted instruction, a stale memory, or a specification-gaming actor produces an externally-visible mistake.

The judge fixes this by making the boundary first-class: every side-effectful action passes through it.

## Semantics

```
Actor (LLM)                Judge (LLM | rules)            Tool
   │                            │                          │
   │── emits proposal ─────────▶│                          │
   │                            │── inspects + decides     │
   │                            │                          │
   │◀── allow ──────────────────│                          │
   │── executes ────────────────────────────────────────▶ │
   │◀── result ────────────────────────────────────────── │
   │                                                       
   │                                                       
   │◀── block(reason) ──────────│   (no execution)
   │                                                       
   │                                                       
   │◀── revise(amended) ────────│   (actor retries with amendment)
   │── executes amended ──────────────────────────────────▶
   │                                                       
   │                                                       
   │◀── escalate(queue_id) ─────│   (paused; operator review)
   │                                                       
```

Each branch emits a JSONL judgment event carrying `parent_run_id`, the proposal, the decision, the reason, the model + policy version that judged it, and the latency + token cost incurred.

## Action proposal

Before any side-effectful tool call, the actor produces a structured proposal. The proposal is what the judge inspects — never raw tool arguments.

```python
@dataclass
class ActionProposal:
    type: str                         # the tool name being proposed
    arguments: dict[str, Any]         # the proposed tool input
    classification: ActionClass       # read_only | reversible_write | external_side_effect | high_risk
    reason: str                       # why the actor is proposing this
    evidence: list[Evidence]          # supporting memory/conversation refs
    authorization: Authorization      # who granted it, scope
    expected_consequence: str         # plain language
    reversibility: Reversibility      # reversible | reversible_with_artifact | irreversible
    rollback_path: str | None         # how to undo, if reversible


@dataclass
class Evidence:
    source: str                       # filename, conversation ref, memory note name
    claim: str                        # what this source supports


@dataclass
class Authorization:
    granted_by: str                   # "operator" | "policy" | "agent"
    scope: str                        # plain language description of the scope
    expires_at: str | None            # ISO-8601 if time-bounded
```

The proposal is a first-class capture marker — it mirrors the existing `atomic_capture` discipline (per `spec/05-capture-rules.md`), applied to a new surface.

### Why structured

The actor and judge are different processes (potentially different model families). Free-form intent communication would let actor-side and judge-side semantics drift. The structured proposal pins:

- What is being proposed (precisely)
- Why (testable — does the evidence support the claim?)
- Who authorized it (auditable)
- What happens if it succeeds (testable against the result)
- How to undo (operator review surface)

It also forces the actor to commit to a reversibility claim, which the judge can challenge.

## Four-outcome model

```python
class JudgmentOutcome(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REVISE = "revise"
    ESCALATE = "escalate"


@dataclass
class Judgment:
    outcome: JudgmentOutcome
    reason: str                       # why this outcome
    amended_proposal: ActionProposal | None  # set on REVISE
    escalation_queue_id: str | None   # set on ESCALATE
    judge_id: str                     # which judge produced this
    policy_version: str               # tools.md / judges.md version hash
    model_id: str | None              # if LLM-backed
    latency_ms: int
    cost_usd: float | None            # if LLM-backed
```

### Allow

Proceed with the proposed action as written. The actor executes; the result feeds back into the loop as today.

### Block

Refuse the action. The actor receives the reason; the tool does not execute; the multi-turn loop continues with the block recorded. The actor may propose a different action.

Blocks are the strongest signal — operators expect them to be rare and reasoned. A high block rate suggests the actor or the policy is misaligned.

### Revise

Return an amended proposal. The actor executes the amended version (not the original). Examples:

- "Send the email but remove the attachment."
- "Open the PR as draft, not for merge."
- "Lower the spend limit to $50 before proceeding."

Revise is the empirically most-useful outcome — most production failures are not "the agent did something forbidden" but "the agent did something directionally right but unsafe as written." Revise lets the system correct without losing the actor's intent.

### Escalate

Pause the action and emit it to an operator review queue. The actor receives a queue handle and waits (in batch contexts) or returns a deferred result (in interactive contexts) until the operator decides.

Escalation is the surface that makes the judge non-binary. Some actions should never auto-execute; some should never auto-block — they should land in front of a human. The queue is the product surface where this happens.

## Where the judge sits in `agent.call()`

The judge runs **after the LLM emits a tool_use block** and **before the framework dispatches to the tool handler**. It is a strict pre-action gate.

```
agent.call() multi-turn loop:

  Iteration N:
    1. _check_cost_guardrails()                        # existing
    2. _llm.call_llm()                                 # existing
    3. parse tool_uses                                  # existing
    4. for each tool_use that is side-effectful:
         a. actor.build_proposal(tool_use, context)   # NEW — structured proposal
         b. judge.evaluate(proposal)                   # NEW — JudgeBackend call
         c. record judgment event                      # NEW — JSONL line
         d. branch on judgment.outcome:
              ALLOW    → execute as today
              BLOCK    → skip execution; emit tool_result with block reason
              REVISE   → execute amended_proposal; emit tool_result
              ESCALATE → enqueue; return Response with deferred=True
    5. build follow-up message                         # existing
    6. continue loop                                   # existing
```

Side-effect classification — which tool calls require a proposal + judgment — is per-tool, declared in `tools.md` (see Action classification below). Read-only tools bypass the judge by default.

## `JudgeBackend` protocol

```python
@runtime_checkable
class JudgeBackend(Protocol):

    def evaluate(
        self,
        proposal: ActionProposal,
        context: JudgmentContext,
    ) -> Judgment: ...

    def supports_revise(self) -> bool: ...
    def supports_escalate(self) -> bool: ...

    @property
    def judge_id(self) -> str: ...
    @property
    def policy_version(self) -> str: ...
```

`JudgmentContext` carries the agent's persona digest, the tool's policy entry from `tools.md`, the actor's recent run history, and the relevant atomic notes — same shape as the runtime-assembly digest in `spec/04`, scoped to what the judge needs.

Capability advertisement (`supports_revise`, `supports_escalate`) lets simple judges (e.g., a pure-rule engine that only allows or blocks) declare honestly, so the runtime can fall back to escalate-or-block on outcomes the backend doesn't support.

### Conformance tests

The conformance suite (similar shape to MemoryBackend's, per `spec/20`) covers:

- `evaluate` returns a valid `Judgment` for each of the four outcomes a backend declares it supports
- `evaluate` does not mutate the proposal (idempotency)
- Latency is bounded by a configurable timeout; timeout returns a `Judgment` with outcome `BLOCK` and reason `judge_timeout`
- Concurrent `evaluate` calls do not corrupt judge state
- `policy_version` changes when the underlying policy source (`tools.md` + `judges.md`) changes

Per-backend tests cover backend-specific behavior (LLM prompt formatting, rule-engine matching, ensemble voting).

## Default reference implementation: `LLMJudgeBackend`

The default judge wraps `LLMBackend` (#87) with a system prompt assembled from `tools.md` + `judges.md` + the agent's persona digest. The user-turn payload is the structured `ActionProposal`. The model returns a structured `Judgment` (via tool-use response).

```python
class LLMJudgeBackend:
    def __init__(
        self,
        llm: LLMBackend,
        policy_source: Path,                # path to judges.md
        model_id: str = "claude-haiku-4-5",
        timeout_ms: int = 5000,
    ): ...
```

Default model is the cheapest model that maintains judge quality in the eval suite. Operators override per-agent or per-action-class.

### Why LLM-default, not rule-engine-default

Pure rule engines are deterministic and fast but cannot evaluate evidence quality, reversibility claims, or the alignment between stated reason and actual proposed arguments. LLMs are the technology that closes the gap between policy-as-prose and policy-as-runtime-check. Rule engines remain the right backend for clearly-deterministic decisions (e.g., `block if target_path not in write_paths`) and are intended to compose with the LLM judge via specialist composition (see below).

## Action classification

Four classes, declared per-tool in `tools.md`:

| Class | Examples | Default judge policy |
|---|---|---|
| `read_only` | `read_file`, `search_notes`, `list_directory` | Bypass judge; no proposal required |
| `reversible_write` | `write_note(staged)`, `create_draft` | Judge optional; default-allow with audit |
| `external_side_effect` | `send_email`, `post_message`, `create_pr` | Judge required; default policy is judge-decides |
| `high_risk` | `delete_files`, `force_push`, `production_deploy` | Judge required; default policy is escalate |

Class is the first thing the judge sees and the strongest input to outcome selection. Tools without an explicit class default to `external_side_effect` — the safest classification for "we don't know."

## Specialist judges

A single monolithic judge has weaker performance than several composed specialists, each evaluating one axis:

- **AuthorizationJudge** — does the action have valid authorization scope?
- **PrivacyJudge** — does the action expose memory marked `private` or `confidential` to an external surface?
- **PolicyJudge** — does the action match `tools.md` write paths, allowlists, and deny rules?
- **ReversibilityJudge** — does the actor's claimed reversibility match reality?
- **QualityJudge** — is the action coherent with the stated reason and evidence?
- **SecurityJudge** — does the action expose credentials, tokens, or paths outside the agent's scope?

Composition pattern: each specialist returns its own `Judgment`; an `EnsembleJudge` aggregates per declared policy (any block → block; majority revise → revise; any escalate → escalate; else allow). Operators choose composition explicitly in `judges.md`.

The default reference implementation ships with **PolicyJudge** (rule-engine, deterministic) + a single LLM-backed catch-all judge for the other axes. Specialist composition is opt-in for operators with the appetite for it.

## Memory provenance integration

The judge consumes evidence; evidence cites memory notes; memory notes need provenance the judge can trust. Extend the existing note frontmatter (per `spec/03-file-formats.md`):

```yaml
---
type: feedback
name: feedback-comm-style
provenance: observed | inferred | generated | confirmed | disputed | superseded
sources:
  - conversation_2026-04-12
  - rubric.md
confidence: high | medium | low
...
---
```

| Provenance | Treatable as |
|---|---|
| `observed` | Evidence — agent saw this in a source document |
| `inferred` | Weak evidence — agent reasoned from observations |
| `generated` | Weak evidence — agent proposed this as a lesson |
| `confirmed` | Instruction-grade — operator confirmed as authoritative |
| `disputed` | NOT evidence — conflicts with another note; surfaced for resolution |
| `superseded` | NOT evidence — replaced by a newer note |

The judge weights cited evidence by provenance. Crucially: **a `generated` note is not treated as instruction.** The agent does not get to teach itself a rule and then cite that rule as authorization for an action — the operator must `confirm` it first.

This extends the memory→persona promotion discipline (`spec/05`) from one specific lifecycle event to every memory read at runtime.

Backward compatibility: notes without `provenance` default to `observed`.

## `judges.md` operator config

Per-agent operator config lives in markdown, matching the framework's config aesthetic (rule #7):

```markdown
# Judges — Caldwell

## Default judge

backend: LLMJudgeBackend
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

policy: rule_engine        # PolicyJudge runs first, fast
authorization: llm         # default LLM judge
privacy: llm
reversibility: llm
quality: llm
security: rule_engine      # rule-engine for credential/path patterns

aggregation: any_block_blocks

## Escalation queue

destination: vault/escalations/
operator_notification: email | webhook | none
auto_decide_after: 24h     # if no operator response, fall back to <fallback>
fallback: block
```

Pure-YAML config files are refused per rule #7. Embedded YAML inside markdown is acceptable for structured fields. Markdown sections (`## Default judge`) carry the same aesthetic as `tools.md` and `model.md`.

## Cost treatment

Per rule #4, every code path that calls an LLM has a cost gate. The judge is no exception.

- Judge calls flow through `_check_cost_guardrails` like any other LLM call
- `judges.md` declares a separate budget (`daily_usd`, `monthly_usd`) — judge spend is not subtracted from the agent's actor budget by default
- `critical=True` is **never set automatically** on judge calls. Operators may set it explicitly for `high_risk` action classes where blocking judgment by cost cap would be worse than the action proceeding unjudged — but the default is: cost cap hit → judge returns `BLOCK` with reason `judge_budget_exhausted`
- Specialist composition multiplies cost — operators see the multiplication in the dashboard before opting in

The discipline: judgment is expensive; the cost is the price of the safety property; operators decide how much they want to pay.

## Audit shape

Each judgment writes a JSONL line to the run log, carrying `parent_run_id` linking back to the actor's run:

```json
{
  "event": "judgment",
  "run_id": "judgment_20260512T143052_abc12345",
  "parent_run_id": "agent_20260512T143050_def67890",
  "agent": "caldwell",
  "judge_id": "default-llm",
  "policy_version": "tools.md@sha256:...+judges.md@sha256:...",
  "proposal": { ... ActionProposal ... },
  "judgment": { ... Judgment ... },
  "latency_ms": 412,
  "cost_usd": 0.00073,
  "ts": "2026-05-12T14:30:52Z"
}
```

The parent run record rolls up judgments inline (same shape as `helper_provenance`, `delegations`, `tool_calls`), so a single read of the agent's run record shows everything the actor and judge did, together.

LogBackend (#61) abstracts this — same protocol, different storage substrate at org scale.

## Eval suite for judges

Per `spec/08-evaluation.md`, the framework already has a rubric-driven eval surface for agent outputs. The judge needs its own, scoped to judgment quality:

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

Eval cases live alongside the agent (per `spec/08`) in `judges/eval/<case>.md` — markdown-as-config, version-controlled.

The eval suite is what makes the judge non-aspirational. A judge with no eval suite is a judge whose quality is opinion, not measurement.

## Failure modes & mitigations

### Correlated judgment

**Risk**: actor and judge share model family, training data, or biases — the judge approves what the actor would have approved itself.

**Mitigation**: default judge is a different model family than the default actor (e.g., Anthropic actor + OpenAI judge, or vice versa). Specialist composition adds rule-engine judges for clearly-deterministic axes. Operators may opt into ensemble composition for `high_risk` classes.

### Specification gaming

**Risk**: actor learns to phrase proposals that pass the judge regardless of underlying intent.

**Mitigation**: the eval suite includes adversarial cases. Judge outcomes are sampled by an offline reviewer (operator or downstream evaluator) at a configurable rate. Specification-gaming patterns flagged by the reviewer become new eval cases.

### Escalation drift

**Risk**: escalations land in front of operators; operators rubber-stamp them; the escalation surface decays into noise.

**Mitigation**: dashboard tracks operator approval rate per escalation class. When approval rate > 95% for an entire class, the dashboard flags it ("consider promoting this class to allow or revise"). When approval rate < 60%, the dashboard flags it ("consider promoting this class to block").

The escalation queue is also append-only; operators cannot delete escalations without leaving a record. Rubber-stamping is visible.

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

- Concrete protocol method signatures beyond `evaluate` — follow-up issue
- The reference LLM prompt template — follow-up issue
- The escalation queue's review UI — follow-up issue
- Cross-agent (fleet-wide) judge policies — see PolicyBackend (#89)
- Streaming judgment for long-running actions — deferred until streaming use cases appear

## Open questions (resolve before lock)

1. **Does the actor build proposals, or does the framework introspect tool_uses?** Building proposals in the actor adds latency and a possible failure mode (actor refuses to build a coherent proposal). Framework-side introspection is faster but loses the "actor commits to a reason" property. **Tentative**: framework introspection for the proposal skeleton + actor-supplied `reason` / `evidence` / `authorization` fields via a structured side-channel (similar to `atomic_capture`).
2. **Should `read_only` actions ever be judged?** Today's default is bypass; some operators may want auditing on reads (privacy-sensitive vaults). **Tentative**: bypass by default; `read_audit_mode: true` in `judges.md` enables read-only judgment for audit without blocking.
3. **What does the actor do during `ESCALATE` in a cron context (no operator awake)?** **Tentative**: same `auto_decide_after` + `fallback` policy declared in `judges.md`. Default fallback is `block` for `high_risk`, `allow` for `external_side_effect` after 24h with operator notification.
4. **Where does the escalation queue live in the vault?** **Tentative**: `vault/escalations/<class>/<run_id>.md` — operators read and resolve via Obsidian or the dashboard; resolution writes back a judgment event.
5. **How does the judge interact with `critical=True`?** Today `critical=True` bypasses cost guardrails; it should not bypass the judge. **Tentative**: critical actions are eligible for `ALLOW` from the judge but never bypass it — the audit trail is the point, and critical actions are exactly the ones that most need recording.

These are resolved during spec lock (when implementation lands). Resolutions move into the body above; open-questions section is removed.

## References

- `docs/spec/01-anatomy.md` §"Policy vs enforcement"
- `docs/spec/03-file-formats.md` (frontmatter schema; provenance extension)
- `docs/spec/04-runtime-assembly.md` (digest assembly; reused for `JudgmentContext`)
- `docs/spec/05-capture-rules.md` (capture-marker discipline; mirrored for action proposals)
- `docs/spec/08-evaluation.md` (LLM-as-judge for output quality; extended pattern)
- `docs/spec/13-research-integrity.md` (citation discipline; reused for evidence)
- `docs/spec/17-tools.md` (tools.md policy; the source of truth the judge enforces)
- `docs/spec/20-memory-backend.md` (Protocol pattern template; mirrored for JudgeBackend)
- #87 LLMBackend (judge calls flow through this)
- #89 PolicyBackend (org-scale policy; composes with judges)
- #61 LogBackend (judgment events; same protocol)
- #110 RFC (this spec's origin)

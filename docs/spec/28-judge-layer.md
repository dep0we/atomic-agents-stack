# spec/28 — Judge Layer

> Status: **locked** as of #112 PR 4. Reference implementation: `atomic_agents/judge/` (PolicyJudge + LLMJudgeBackend + escalation + revise state machines). Conformance suite: `tests/test_judge_protocol_conformance.py`.
>
> Cross-links: spec/01 (anatomy), spec/03 (file formats), spec/04 (runtime assembly), spec/05 (capture rules), spec/08 (evaluation), spec/13 (research integrity), spec/15 (delegation), spec/17 (tools), spec/19 (MCP), spec/20 (memory backend — protocol pattern template)
>
> Related backends: PolicyBackend (#89), LLMBackend (#87), LogBackend (#61)

## Lock criterion (PR 4)

Per CLAUDE.md rule #10 ("the spec is the product"), spec/28 locks when:

1. A reference implementation ships in the codebase (`atomic_agents/judge/`).
2. A conformance suite asserts the documented invariants (`tests/test_judge_protocol_conformance.py`, ~37 tests covering the invariants enumerated in §"Conformance suite" below).
3. Drift between spec and shipped behavior is folded into the canonical text (PR 4 stripped the per-PR-lock-in markers PR 3a/3b/3c accumulated).

The original RFC convention (`Status: **RFC**` banner + "RFC vs locked spec" preface) is removed at lock. Drift from the locked spec is a follow-up issue per rule #10, not a spec edit.

## Overview

The **judge layer** is a pre-action validation surface. Before any side-effectful tool call executes, a separate `JudgeBackend` inspects a structured **action proposal** assembled from the actor's tool_use block plus actor-supplied side-channel fields, returns one of four outcomes — **allow / block / revise / escalate** — and writes a **judgment event** to the audit trail.

The judge raises `tools.md` from advisory-in-some-runtimes to **gated-at-runtime when the judge is enabled**. In runtimes without the helper or with `judges.md` absent, `tools.md` remains advisory exactly as today (per `spec/01` §"Policy vs enforcement"). The judge does not change the runtime story for runtimes the framework cannot reach.

The judge layer is the structural encoding of **graduated autonomy** — the framework's commitment to treating agent autonomy as a gradient (free where risk is low, revised where risk is moderate, escalated where risk is high) rather than a binary. See `spec/01-anatomy.md` §"Graduated autonomy" for the principle; this spec is the mechanism.

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
   │                          │◀── revise(amendment) ─│                          │
   │                          │── applies amendment;                             │
   │                          │   recomputes class +                             │
   │                          │   hashes from new tool;                          │
   │                          │   schema + write paths;                          │
   │                          │   second judgment (same                          │
   │                          │   ensemble) ──────────▶│                       │
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

    # Actor-supplied (via side-channel marker bound to this tool_call_id;
    # required for side-effectful classes, optional for read-only)
    side_channel_for_tool_call_id: str | None  # MUST equal tool_call_id if present
    reason: str | None                      # why the actor is proposing this
    evidence: list[Evidence]                # supporting memory/conversation refs (may be empty)
    authorization: Authorization | None     # who granted it, scope
    expected_consequence: str | None        # plain language
    reversibility: Reversibility | None     # reversible | reversible_with_artifact | irreversible
    rollback_path: str | None               # how to undo, if reversible
    target_audience: str | None             # "internal" | "external:<surface>" — for privacy judges

    # Framework-set after assembly
    proposal_id: str                        # unique id; primary key for judgment events
    proposal_ts: str                        # ISO-8601


@dataclass(frozen=True)
class ProposalAmendment:
    """What a judge returns on REVISE. Contains ONLY the fields the judge is
    allowed to amend. The framework applies the amendment to the original
    proposal and produces a new bound ActionProposal with framework-recomputed
    classification + hashes + proposal_id + proposal_ts. The judge cannot
    forge framework-managed fields."""
    tool_name: str | None                   # may change tool (e.g., revise create_pr → create_draft_pr)
    tool_arguments: dict[str, Any] | None   # may amend args (e.g., strip attachment)
    target_audience: str | None             # may narrow audience
    expected_consequence: str | None        # may refine description
    reversibility: Reversibility | None     # may adjust reversibility claim
    rollback_path: str | None               # may add rollback path
    appended_evidence: list[Evidence]       # may add evidence; cannot replace
    judge_note: str                         # why the amendment


@dataclass(frozen=True)
class JudgmentEvent:
    """The audit-shape dataclass written to JSONL per judgment. NOT what the
    judge returns (Judgment is). The framework wraps Judgment with the
    runtime-only fields below (raw_outcome, enforcement_action, cost_source,
    binding) before serializing."""
    event: str                              # always "judgment"
    run_id: str
    parent_run_id: str
    proposal_id: str
    agent: str
    judge_id: str
    policy_version: str
    proposal: ActionProposal
    judgment: Judgment
    raw_outcome: JudgmentOutcome            # what judge returned
    enforcement_action: str                 # what framework did:
                                            # "audit_bypass" | "block_executed" |
                                            # "allow_executed" | "revise_executed" |
                                            # "escalate_pending"
    binding: ProposalBinding                # tool_call_id, tool_definition_hash, arguments_hash
    latency_ms: int
    cost_usd: float | None
    cost_source: str                        # "judge"
    ts: str
```

Field-presence rules for `ActionProposal`:

| Classification | read_audit_mode | side-channel fields | Behavior on missing |
|---|---|---|---|
| `read_only` | `false` | Not required | Proposal not built; judge bypassed |
| `read_only` | `true` | Not required | Minimal proposal built (introspected only); judge invoked for audit; outcome ignored for enforcement |
| `reversible_write` / `external_side_effect` / `high_risk` | any | **Required** | Missing side-channel marker, mismatched `side_channel_for_tool_call_id`, or duplicate marker for same `tool_call_id` → `JudgeProposalInvalid` (failure_policy resolves; default `BLOCK`) |


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

Field-presence rules are summarized in the table above. The narrative: for `read_only` actions, actor-supplied fields are optional (set to `None`). For all other classes, the actor must declare intent **per tool call** via a side-channel marker; missing or misbound markers raise `JudgeProposalInvalid`.

### Proposal binding (TOCTOU defense)

The framework binds the proposal to the exact tool call via `tool_call_id`, `tool_definition_hash`, and `arguments_hash`. When the judge returns `ALLOW`, the framework executes the **bound** arguments — not arguments the actor might emit in a follow-up turn, not arguments mutated by another handler. If the tool definition has changed between proposal and execution (rare, but possible during hot-reload), the framework re-runs the judge against the new definition.

#### What the hashes cover

The current framework does **not** have a canonical-JSON hasher. The implementation PR adds a small `atomic_agents/_canonical.py` helper used uniformly across the judge layer.

**`arguments_hash`** = `sha256(canonical_json(parsed_tool_arguments))`, where:

- `parsed_tool_arguments` is the result of provider-specific argument parsing (Anthropic: `ToolUseBlock.input` dict directly; OpenAI: `json.loads(tool_call.function.arguments)` with the existing malformed-JSON-→-{} fallback per `_llm.py`)
- `canonical_json` uses `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
- If parsing fails, the framework raises `JudgeProposalInvalid` *before* invoking the judge — no judgment is rendered on an unparseable proposal

**`tool_definition_hash`** = `sha256(canonical_json({...}))`, where the canonical payload is:

- For custom tools: `{"kind": "custom", "name": tool_name, "input_schema": <schema>, "handler_module": <module_path>, "handler_qualname": <qualname>}` — the handler's module + qualname names *who* will execute, not the bytecode (handler updates are framework-version events, recorded by `tool_definition_hash` change at the registry layer)
- For MCP tools: `{"kind": "mcp", "server": mcp_server, "name": tool_name, "input_schema": <schema>, "server_version": <version_string_if_advertised>}`

Hashes are stable across framework restarts on the same code revision and change deterministically when the relevant inputs change. The conformance suite asserts this for both shapes.

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
    amendment: ProposalAmendment | None      # set on REVISE; framework applies it
    escalation_queue_id: str | None          # set on ESCALATE
    judge_id: str
    policy_version: str                      # sha256 of tools.md + judges.md at decision time
    model_id: str | None
    latency_ms: int
    cost_usd: float | None
```

Note: `Judgment` is what the **backend returns**. The framework wraps it in a `JudgmentEvent` (defined above) for audit. `enforcement_action`, `raw_outcome`, `cost_source`, and `binding` are framework-set fields on `JudgmentEvent`, not on `Judgment` — the judge does not get to influence how its decision was enforced.

### Allow

Proceed with the **bound** action. The actor's run continues; the multi-turn loop incorporates the tool result on the next turn.

### Block

Refuse the action. The actor receives the reason via tool_result; the multi-turn loop continues; the actor may propose a *different* action in a subsequent turn. Blocks are the strongest signal — operators expect them to be rare and reasoned. The judge's reason flows back to the actor as ground truth ("you proposed X; this is refused because Y; consider Z").

`BLOCK` is distinct from `REVISE` in shape: block returns no amended proposal; the actor must re-author from scratch. Revise hands the actor a pre-amended proposal that the framework re-validates before execution.

### Revise

The judge returns a `ProposalAmendment` (the small dataclass defined above containing only judge-amendable fields). The framework applies the amendment to the original proposal:

1. **Framework recomputation**: framework constructs a new `ActionProposal` by merging the original with the amendment. `tool_name`, `tool_arguments`, `target_audience`, `expected_consequence`, `reversibility`, `rollback_path` come from the amendment (when present) or the original (when amendment fields are `None`). `evidence` is the original's `evidence + amendment.appended_evidence`. `reason` and `authorization` are taken **from the original** — the judge cannot rewrite them. `classification` is recomputed by the framework from the new `tool_name` against `tools.md` / `mcp.md`. New `tool_definition_hash`, `arguments_hash`, `proposal_id`, and `proposal_ts` are framework-set.
2. **Class non-downgrade by exploit**: because the framework recomputes classification from the (potentially new) tool, the judge cannot bypass class policy by editing the classification field — it doesn't have access to that field. If the new tool is registered at a higher class, the stricter class's policy applies to the second judgment.
3. **Schema validation**: amended `tool_arguments` validates against the (possibly new) tool's registered JSON schema. The mode is operator-controlled via `judges.md`'s `validation:` field — `weakened` (default; tool registered + dict shape + `arguments_hash` recomputes) or `strict` (opt-in, requires the `[validation]` extra; runs `jsonschema.validate` and surfaces field-path failures). See [Schema validation](#schema-validation) below for the full taxonomy.
4. **Policy re-check**: amended args still pass `tools.md` write-path enforcement.
5. **Second judgment — deterministic ensemble selection**: the amended proposal passes through a fresh judgment cycle using the **identical ensemble configuration** as the original proposal — same backends in the same order, same `JudgePolicyContext` (except for the amended-proposal-derived fields), against the framework-recomputed amended proposal. This makes revise paths reproducible: given the same original proposal and the same amendment, the second judgment is deterministic. The second judgment must return `ALLOW`; if it returns `REVISE` again, the framework returns `BLOCK` with reason `revise_loop_exhausted` (no infinite revise loops). `max_revise_iterations` is bounded at 1 by spec.

The framework records both the original and the amended proposal in the judgment event. Operators can audit what the judge changed.

Revise is the empirically most-useful outcome for production failure modes that are not "agent did something forbidden" but "agent did something directionally right but unsafe as written." Common revisions:

- Strip an attachment from an email send
- Lower a spend limit before proceeding
- Open a PR as draft, not for merge

#### Schema validation

The `validation:` top-level field in `judges.md` gates how the framework validates amended `tool_arguments` before executing the bound action. The gate is on the parsed config — NOT on whether `jsonschema` happens to be importable in the runtime — so operators with the package pulled in by an unrelated dependency do not see strict validation kick in without explicit opt-in.

**Modes.** Two values ship in PR 5b; `audit` and `paranoid` namespaces are reserved (see below):

| `validation:` | Behavior                                                                                                                                                                                                                  |
|---------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `weakened` (default) | Tool registered + `tool_arguments` dict-shaped + `arguments_hash` recomputes. One-shot per-agent log warning fires the first time, pointing at the `[validation]` extra and the `validation: strict` upgrade path.        |
| `strict`             | Weakened checks run first, then `jsonschema.validate(tool_arguments, registered.input_schema)`. Requires the `[validation]` extra installed (`pip install 'atomic-agents-stack[validation]'`).                              |

**Load-time gate.** When `validation: strict` is parsed, the framework probes `import jsonschema` and raises `JudgePolicyInvalid` with an actionable install message if the package is not importable. Operators flipping strict without first installing the extra fail LOUD at agent-load, never silently at the first amendment.

**Exception taxonomy under `validation: strict`:**

| Underlying failure                                       | Re-raised as                       | Why                                                                                                                              |
|----------------------------------------------------------|------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| `jsonschema.ValidationError`                             | `JudgeAmendedProposalRejected`     | Per-amendment rejection. Normal `failure_policy[JudgeAmendedProposalRejected]` flow. Message carries the failing field path.      |
| `jsonschema.SchemaError` or `referencing.exceptions.Unresolvable` (legacy `jsonschema.RefResolutionError`) | `JudgePolicyInvalid`               | Operator authoring bug — the tool's own `input_schema` is malformed or has broken `$ref`s. Different exception, different policy. |
| `ImportError` / `AttributeError` / `TypeError` from `jsonschema.validate` | `JudgeAmendedProposalRejected`     | Runtime jsonschema API surprise. Per-amendment failure, not policy invalid. Message names the underlying exception class.         |

**Empty / None schema is a no-op.** When the registered tool's `input_schema` is `{}` or absent, `validation: strict` short-circuits — `jsonschema.validate(args, {})` matches any object by definition, so the framework skips the call entirely. Tools that don't author a schema get no strict-mode enforcement (and operators should treat missing schemas as a doctor finding, tracked at [#175](https://github.com/dep0we/atomic-agents-stack/issues/175)).

**Cascade-floor strictness.** Mirrors `class_policy`: a delegate's `judges.md` may strengthen `validation` (`weakened` → `strict`) but cannot relax it. Strictness ordering: `strict > weakened`. Relax attempts raise `JudgePolicyInvalid` at agent-load. A delegate that omits `validation:` default-fills with `source="default"` and inherits the floor's value — no false-positive relax violation against a `validation: strict` floor.

**`additionalProperties: false` is not imposed by the framework.** Operator authors of `input_schema` may set it themselves to refuse keys outside the documented surface. A future `validation: paranoid` mode (reserved; not yet implemented) would impose it defensively across every registered tool's schema; that namespace exists so the parser rejects `validation: paranoid` with a tracking-issue pointer rather than a generic "unknown value" error.

**Reserved namespaces.** `validation: audit` (validate + JSONL warn without BLOCK; tracked at [#176](https://github.com/dep0we/atomic-agents-stack/issues/176)) and `validation: paranoid` ([#179](https://github.com/dep0we/atomic-agents-stack/issues/179)) are accepted by the lexical parser but rejected with "not yet implemented" messages pointing at their tracking issues. Operator typos surface as the generic "must be one of {weakened, strict}" rejection.

**Migration aid.** Operators flipping `validation: strict` on a production agent may discover amendments that previously passed weakened validation now BLOCK. The `check_tool_schemas_for_amendment_validation` doctor check (tracked at [#175](https://github.com/dep0we/atomic-agents-stack/issues/175)) will detect tools whose `input_schema` is missing or trivially permissive before the flip.

### Escalate

Pause the action. The framework writes a **PENDING** escalation record to `vault/escalations/<class>/<proposal_id>.md` containing the full proposal and the judge's reason. The actor's run returns with `Response.deferred=True` and `Response.escalation_queue_ids: list[str]` (one id per ESCALATEd tool_use in the assistant turn — multi-tool-use turns can produce multiple deferrals). Subsequent operator resolution writes a **RESOLVED** event linked to the PENDING record (escalation is modeled as a state machine, not a single verdict). ALLOWed tool_uses in the same turn still execute and their results land in `Response.tool_calls`; the multi-turn loop terminates immediately after the iteration (no follow-up LLM call).

#### Escalation file format

```markdown
---
type: escalation
state: pending | resolved | redacted
proposal_id: proposal_20260512T143052_xyz98765
parent_run_id: agent_20260512T143050_def67890
agent: caldwell
action_class: high_risk
judge_id: default-llm | framework
escalated_at: 2026-05-12T14:30:52Z
policy_version: tools.md@sha256:...+judges.md@sha256:...
schema_version: 1
triggered_by: failure_policy:JudgeUnavailable      # optional; framework-synthesized only
synthesis_source: class_policy | failure_policy    # optional; framework-synthesized only
---

## Proposal

<JudgmentEvent.proposal serialized as a fenced ```yaml block>

## Judge's reason for escalating

<Judgment.reason — free prose>

## Resolution

<Empty when state=pending. Operator writes one of the resolution blocks below.>

### Approved by <operator>
resolved_at: <ISO-8601>
re_judged: false
note: <free prose>

### Denied by <operator>
resolved_at: <ISO-8601>
note: <free prose>

### Revised by <operator>
resolved_at: <ISO-8601>
amendment:
  ```yaml
  judge_note: <operator note explaining the amendment>
  tool_name: <optional — swap tool>
  tool_arguments: <optional — amended args dict>
  target_audience: <optional>
  expected_consequence: <optional>
  reversibility: <optional>
  rollback_path: <optional>
  appended_evidence: <optional list>
  ```
note: <free prose>

### Redacted by <operator>
redacted_at: <ISO-8601>
redaction_reason: <free prose>
```

#### Resolution conflict rule

The framework writes PENDING; the operator (or an authorized service) writes a resolution block by editing the file. The framework reads files in `vault/escalations/<class>/` once per resolution-poll cycle (configurable; default 60s) and emits RESOLVED events for any file whose `state` field transitioned from `pending` to one of `resolved` / `redacted`.

If two resolution blocks are present, the *first* one (top-down in file order) wins; the file's `state` field is the authoritative state. Operators editing concurrently are responsible for not stomping each other; the framework does not attempt to merge conflicting resolutions. The doctor (`spec/27`) gains `check_escalation_conflicts` to surface files with multiple resolution blocks.

**Operator-revise semantics (PR 3c).** When the operator writes `### Revised by <op>`, the framework parses the embedded `amendment:` YAML block into a `ProposalAmendment`, applies it to the PENDING file's `ActionProposal`, re-validates (tool registered, args dict-shaped, args_hash recomputes, write-path enforcement), and **gates on the recomputed classification** (NOT the original). For `high_risk` actions the framework runs a fresh judgment cycle through the ensemble before executing. For non-`high_risk` actions, schema/policy validation alone is sufficient. The `re_judged: bool` audit field on the executed event reports whether the ensemble re-ran. **`re_judged` is framework-set, not operator-supplied** — operators express intent via the amendment; the framework decides whether re-judge is needed based on the AMENDED class. This means an operator who swaps `tool_name` to upgrade `reversible_write` → `high_risk` (or `delete_files` etc.) cannot skip the re-judge by phrasing the original proposal as a lower class.

If the framework's re-judge returns BLOCK, the action is refused; the audit record carries `enforcement_action="operator_revise_executed"` with `re_judged: true` on the re-judge event chain but **no `escalation_operator_revise_executed` audit line** for the action itself (refusal is the outcome). Invalid amendments (missing YAML block, malformed YAML, unknown fields, tool not registered) emit `enforcement_action="operator_revise_invalid_amendment"` and refuse execution.

**Strict resolution-block parser**. Resolution headers MUST match exactly: `### <Verb> by <operator>` — h3 prefix, verb in `{Approved, Denied, Redacted, Revised, Auto-decided}` with exact case, the literal word `by`, and a non-empty operator string. Typos (lowercase verb, h4 prefix, missing `by`) make the file UNPARSEABLE — the framework leaves the file as-is, does NOT claim the de-dup sidecar, and surfaces a doctor warning on the next health check. This lets operators fix the typo and re-trigger without state-machine debt.

**Body integrity**. On every operator resolution, the framework recomputes `arguments_hash` from the `## Proposal` block's `tool_arguments` field and compares against the embedded `arguments_hash` value. Mismatch → `enforcement_action="proposal_body_tampered"`, no execution. **Defense scope**: this catches accidental edits and lazy tamper (operator changes `tool_arguments` without also updating the embedded `arguments_hash`). A sophisticated operator can recompute the hash to match — the embedded hash IS in the same file. Operator approval is itself the trust anchor; the check is a guard against careless edits, not against hostile operators. Operators with vault write access can also write a fresh PENDING file from scratch with any arguments they want.

**De-dup sidecar**. The framework claims a `.<proposal_id>.resolved-emitted` sidecar file via `O_CREAT|O_EXCL` next to the PENDING file before emitting the RESOLVED audit event. Concurrent pollers race the sidecar create; exactly one wins. The sidecar is a vault-internal dotfile (taste rule #1: vault as source of truth — no in-memory de-dup state). The throttle marker `<destination>/.last-poll` follows the same convention; `spec/27`'s doctor should ignore both.

**Auto-decide CAS race**. The auto-decide-timeout path re-snapshots the PENDING file just before its atomic_write and aborts if the sha256 changed since the initial read — operator-edit-during-write defers to the next poll cycle. The auto-decide is idempotent (timeout has still passed), so retry is safe.

Redacted files keep their frontmatter but the body is replaced with the `redaction_reason`. The RESOLVED event records the redaction; the original proposal is preserved in the JSONL audit trail.

Resolution paths:

- **Operator approves** → framework executes the bound action; RESOLVED event records `approved` outcome
- **Operator blocks** → no execution; RESOLVED event records `denied` outcome
- **Operator revises** → operator supplies amended proposal; framework **always re-validates** the amended proposal (schema, policy, classification recompute, write-path enforcement). For `high_risk` actions, framework also **re-judges** the amended proposal through a fresh judgment cycle (operator revisions on high-risk actions are not a judge override; they are an operator hint that gets validated). For non-`high_risk` actions, schema/policy validation alone is sufficient. RESOLVED event records `revised` outcome with `re_judged: true | false` field.
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

**Synced-vault caveat**: a vault synchronized across hosts (Obsidian Sync, iCloud, syncthing) turns memory writes into a *cross-runtime* surface — what one agent writes locally propagates to other devices and may become instruction input for other agents reading the same vault. This blurs the "internal-only" boundary.

For synced vaults, the recommendation is **`judge_captures: true`** even though the default is false. The framework cannot reliably detect "is this vault synced?" — Obsidian Sync state lives in Obsidian, iCloud sync lives in macOS, etc. — so it doesn't flip the default automatically. Instead:

- The doctor (`spec/27`) checks for common sync indicators (`.obsidian-sync/`, iCloud path prefix, `.stignore` for syncthing) and emits a `vault_synced_judge_captures_off` warning when judge is enabled, sync is detected, and `judge_captures: false`.
- The configuration wizard (#94, planned) defaults `judge_captures: true` when it detects a synced vault during operator setup.

Opt-in: operators set `judge_captures: true` in `judges.md` to route `atomic_capture` markers through the judge. The proposal classification for captures is always `reversible_write` (the memory layer supports `restore_version`); the proposal's `tool_name` is `atomic_capture` and `tool_arguments` is the capture marker payload.

## Delegation interaction (`spec/15`)

Atomic Agents delegation is one-level (coordinator → specialist; specialists do not delegate further). The judge layer respects this boundary while closing the "delegate without `judges.md` escapes coordinator policy" hole that strict tool non-inheritance would otherwise leave open.

### Policy resolution

A delegate's effective policy is the **union** of:

1. **Project-root floor** (`<project>/judges.md` at the multi-agent project root per `spec/06`, if present) — sets minimum class policy and required failure_policy. The project floor **cannot be relaxed** by a delegate's own `judges.md`; per-class policies in the delegate's file may only be *stricter* (`escalate` is stricter than `judge_required`; `judge_required` is stricter than `allow_with_audit`; etc.). Attempts to relax → `JudgePolicyInvalid` at load time.
2. **Delegate's own `judges.md`** — may add stricter class policy, add specialist composition, narrow the budget, change destination, override per-class fallbacks (but only toward stricter).
3. **Coordinator-imposed scope** carried via `authorization.granted_by = "delegated_from:<coordinator>"` — the delegate's judge sees this and may apply additional constraints derived from the coordinator's intent.

If neither a project-root `judges.md` nor a delegate `judges.md` is present, the delegate's judge layer is **bypassed** (judges.md is opt-in per rule #14). A coordinator that needs to enforce policy on delegates must drop a `judges.md` at the project root.

The framework's load-time validation checks that no delegate's `judges.md` relaxes the project floor and raises `JudgePolicyInvalid` (which the failure_policy resolves — default block at first call) if it does. The doctor (`spec/27`) gains `check_judge_policy_floor` to surface drift before runtime.

### Other delegation rules

- **`delegate_chain` is recorded** in every proposal. The coordinator's run_id appears in the delegate's proposal `delegate_chain` field; the judge can read it to make policy decisions.
- **Authorization flows by reference, not transitively**. A delegate's `authorization.granted_by` may be `"delegated_from:<coordinator>"`; the delegate's judge inspects shape and scope as it would for any authorization.
- **Escalations bubble**. A PENDING escalation in a delegate's vault triggers an `escalation_propagated` event in the coordinator's run record so coordinator-level dashboards see fleet-wide pending escalations.

The framework reads `judges.md` from the agent's own directory first, then walks upward to find a project-root `judges.md`. Symlinks resolve normally.

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

The proposal's `mcp_server` field (visible to the judge via `ActionProposal`, mirrored into `JudgePolicyContext` if specialist judges need fast access) lets MCP-aware specialist judges apply server-specific policies (e.g., GitHub PRs to `main` always escalate, regardless of class).

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

`JudgmentContext` is a runtime-assembly digest (per `spec/04`) scoped to the judge. Crucially it is split into **policy-context** (what the judge is allowed to see and prompt against) and **runtime-config** (what the framework uses for enforcement; never shown to the judge):

```python
@dataclass(frozen=True)
class JudgePolicyContext:
    """What the judge sees. Includes only policy + agent context, not
    operational config about the judge itself."""
    agent_name: str
    persona_digest: PersonaDigest         # IDENTITY + SOUL + USER excerpt
    tools_md_entry: ToolPolicyEntry        # the tool's tools.md section
    class_policy: ClassPolicySnapshot      # the per-class rules from judges.md
    specialist_axis: str | None            # which axis this judge is responsible for (if specialist)
    recent_runs: list[RunSummary]          # last N runs of this agent
    cited_notes: list[Note]                # the evidence the actor cited
    delegate_chain: list[str]              # mirror of proposal.delegate_chain
    loaded_skills: list[SkillRef]          # mirror of proposal.loaded_skills


@dataclass(frozen=True)
class JudgeRuntimeConfig:
    """What the framework uses to manage the judge. NEVER passed into
    the judge's prompt or visible to the LLM judge. Splitting prevents
    conflict-of-interest (the LLM judge cannot see / modify its own
    failure_policy, budget, escalation fallback, or backend selection)."""
    backend_name: str                      # which JudgeBackend
    model_id: str | None
    timeout_ms: int
    budget: BudgetConfig
    failure_policy: dict[str, JudgmentOutcome]
    escalation_config: EscalationConfig
    read_audit_mode: bool
    judge_captures: bool


@dataclass(frozen=True)
class JudgmentContext:
    policy: JudgePolicyContext
    runtime: JudgeRuntimeConfig            # framework-only; judges that
                                           # reference this field outside
                                           # of conformance-allowed reads
                                           # fail conformance
```

The judge backend reads `JudgePolicyContext` to build its prompt or its rule input. `JudgeRuntimeConfig` is used by the framework to manage retries, budgets, and escalations — the LLM judge does not see its own knobs. Splitting them keeps `evaluate` deterministic given the same (proposal, policy_context) pair — important for the conformance suite.

### Capability advertisement

- `supported_outcomes()` — set of outcomes this backend can return. Pure rule engines may return only `{ALLOW, BLOCK}`. LLM judges typically return `{ALLOW, BLOCK, REVISE, ESCALATE}`.
- `supports_read_audit()` — whether the backend can be invoked on read-only actions for audit (without blocking).
- `supports_specialist_composition()` — whether multiple instances of this backend can compose in an `EnsembleJudge`.

#### Outcome-fallback contract

Each backend is responsible for **self-mapping** its internal intent to a supported outcome before returning. A backend that internally "wants to revise" but does not advertise `REVISE` must return `BLOCK` with reason `revise_intent_not_supported`. The runtime does not second-guess the backend's mapping.

The runtime *does* validate that the returned outcome appears in `supported_outcomes()`. Outcomes not in the advertised set are rejected as `JudgePolicyInvalid` (the backend lied about its capabilities; failure_policy resolves — default block). This split keeps the runtime fallback narrow (only illegal returns) and pushes intent-translation responsibility to the backend that knows its own capabilities.

| Backend returns | Backend advertised | Runtime behavior |
|---|---|---|
| Outcome in advertised set | — | Pass to enforcement |
| Outcome NOT in advertised set | — | Reject as `JudgePolicyInvalid`; failure_policy resolves |
| `REVISE` with no `amended_proposal` | `REVISE` supported | Reject as `JudgePolicyInvalid` |
| `ESCALATE` with no `escalation_queue_id` | `ESCALATE` supported | Framework assigns one; warn |
| Amended proposal fails re-validation | — | `JudgeAmendedProposalRejected`; failure_policy resolves |

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

Mirrors `spec/20`'s suite shape. ~30 tests covering:

- `evaluate` returns a valid `Judgment` for each outcome in `supported_outcomes()`
- `evaluate` does not mutate the proposal or `JudgePolicyContext` (idempotency)
- Latency bounded by configurable timeout; timeout → `JudgeUnavailable`
- Concurrent `evaluate` calls do not corrupt named state: (a) policy cache (b) LLM client connection (c) judge budget counter (d) ensemble vote buffer (e) JSONL writer position (f) escalation queue file (g) backend registry. Each is a named invariant the conformance test verifies independently
- `policy_version` changes when policy source changes; `policy_version` is computed via atomic snapshot (read whole file in one read, validate, hash; partial-read or invalid-utf-8 → `JudgePolicyInvalid` not a silently-wrong hash)
- Framework recomputes `classification` for amended proposals; judge cannot influence classification
- Schema-invalid amended proposal → `JudgeAmendedProposalRejected`
- Stricter class policy applies when amended class is higher than original
- Second judgment on revised proposal cannot itself revise (no infinite loops; reason `revise_loop_exhausted`)
- Exception taxonomy maps to outcomes per `failure_policy`
- Side-channel with mismatched/missing/duplicate `side_channel_for_tool_call_id` → `proposal_incomplete` or `side_channel_mismatch`
- Audit JSONL includes `tool_definition_hash`, `arguments_hash`, `tool_call_id`, `raw_outcome`, `enforcement_action`, `cost_source`
- Read-audit mode bypasses block but still writes judgment event with `enforcement_action = "audit_bypass"`
- Escalation writes PENDING file with full proposal; resolution writes RESOLVED event linked by `proposal_id`; redacted PENDING leaves a `redacted` marker
- Hash determinism: identical (parsed args, tool def) inputs produce identical hashes across processes
- Hash sensitivity: changes in handler module/qualname or schema produce different `tool_definition_hash`
- Project-floor `judges.md` cannot be relaxed by delegate-level `judges.md` → `JudgePolicyInvalid` at load time
- `JudgeRuntimeConfig` fields never appear in the LLM judge's prompt (a conformance assertion that the backend's prompt-construction does not read from `runtime`)
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
        model_id: str = "gpt-5-nano",       # OpenAI by default; see "Default judge model" below
        timeout_ms: int = 5000,
        max_revise_iterations: int = 1,
    ): ...
```

`max_revise_iterations` is bounded at 1 by spec to enforce the "no infinite revise" rule.

### Default judge model

The framework's default *actor* is Anthropic (Sonnet 4.6 in `atomic_agents/_model.py`). The correlated-judgment mitigation requires the default *judge* to be a different model family to avoid actor and judge sharing biases. So the default judge backend pairs with an **OpenAI** model. The cheapest tier currently in the cost table at `atomic_agents/_costs.py` is `gpt-5-nano`, used as the default here. The implementation PR validates the choice against eval-suite quality before locking; if a smaller / cheaper tier is added to the pricing table later, the default may move.

Operators in Claude-only deployments (no OPENAI_API_KEY configured) can:

- Configure an Anthropic judge model explicitly in `judges.md` (acknowledging the correlation risk)
- Use rule-engine specialists only (deterministic; no model-family correlation)
- Run an ensemble of two Anthropic models (different sizes; partial mitigation)

The doctor (`spec/27`) warns when the configured judge model family matches the configured actor model family.

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

### Default composition: PolicyJudge always-on, LLM catch-all, others opt-in

The default reference implementation ships with:

- **`PolicyJudge`** (rule-engine, deterministic, ~microseconds, **always-on** when the judge is enabled) — runs first; matches `tools.md` write paths, allowlists, deny rules, and class-policy enforcement. If it blocks, the ensemble blocks (no LLM cost incurred).
- **LLM catch-all judge** — runs after PolicyJudge if PolicyJudge allowed. Single LLM call covering authorization, privacy, reversibility, quality, and security axes together.
- **Specialist composition** (per-axis split into AuthorizationJudge / PrivacyJudge / etc.) — opt-in via the `## Specialist composition` section of `judges.md`. When the section is omitted, the catch-all runs alone.

So absent a `## Specialist composition` section, the runtime composition is `[PolicyJudge, LLMCatchAll]` — not just the catch-all alone. The `Default judge` section in `judges.md` sets the LLM catch-all's backend; PolicyJudge is implicit and always-on.

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
auto_decide_after_seconds: 86400

# Single-policy form (applied to every ActionClass):
fallback_on_timeout: block

# OR per-class form (PR 5a of #112) — ``default`` is REQUIRED:
# fallback_on_timeout:
#   default: block
#   high_risk: block
#   reversible_write: allow
#   external_side_effect: block

resolution_poll_cycle_seconds: 60

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
- Required sections: `Default judge`. **`Class policy`** is *recommended* but not strictly required — when absent or partial, the framework fills missing keys from the per-class default table below (the table in the Action classification section above). When present, only listed keys are honored; unlisted keys still get default-fill.
- **`Class policy` default-fill**: `read_only: bypass`, `reversible_write: allow_with_audit`, `external_side_effect: judge_required`, `high_risk: escalate`. These defaults are conservative — partial `judges.md` does not over-block and does not silently allow.
- Values within sections are parsed as YAML. Invalid YAML → `JudgePolicyInvalid`.
- Class policy values are an enum: `bypass | allow_with_audit | judge_required | escalate`. Unknown value → `JudgePolicyInvalid`.
- Specialist composition is optional. Absent section → composition is implicit `[PolicyJudge, LLMCatchAll]` (PolicyJudge always-on baseline + the LLM catch-all configured under `## Default judge`). The catch-all is not "alone" — PolicyJudge always runs first.
- Failure policy section is optional. **`Failure policy` default-fill**: any unlisted exception type defaults to `block` (fail-closed). Partial coverage is allowed; missing keys get the safe default.
- **`Escalation queue` parser rules**: duration values are integer seconds (e.g., `auto_decide_after_seconds: 86400`), not duration strings. The framework reads `destination` as a vault-relative directory; the operator-typed value `vault` is normalized to `vault/escalations/` per spec/28:288. `resolution_poll_cycle_seconds` (default 60) caps how often the framework scans the escalation directory for state transitions; `0` disables throttling. The `fallback` key was renamed to `fallback_on_timeout` to match the parsed field name; the old name is not accepted. `fallback_on_timeout` accepts either a string (applied to every `ActionClass`) OR a mapping keyed by `ActionClass.value` strings with a mandatory `default:` key — there is no implicit fall-through when the dict form is used. Accepted outcome values are `{allow, block}` only — `revise` and `escalate` are judge-driven outcomes that require a live judge to interpret and have no meaning in the auto-decide-when-no-judge-responded path; the parser rejects them at load time rather than silently coercing them to block at runtime. Per-class resolution at auto-decide time keys on the PENDING file's frontmatter `action_class` field (the authoritative classification recorded at write time), NOT on the on-disk directory name, so an operator who hand-renames or typos a class directory still gets the correct timeout policy.
- Per-tool overrides (rare, advanced) live in `tools.md`'s per-tool sections, not in `judges.md`.

Pure-YAML config files are refused per rule #7. Embedded YAML inside markdown is acceptable for structured fields. Markdown sections carry the same aesthetic as `tools.md` and `model.md`.

### `ClassPolicySnapshot`

The parsed-and-defaulted class policy is exposed to the judge as `ClassPolicySnapshot` in `JudgePolicyContext`:

```python
@dataclass(frozen=True)
class ClassPolicySnapshot:
    read_only: ClassPolicyValue            # bypass | allow_with_audit | judge_required | escalate
    reversible_write: ClassPolicyValue
    external_side_effect: ClassPolicyValue
    high_risk: ClassPolicyValue
    source: dict[str, str]                 # per-class: "judges.md" | "project_floor" | "default"
```

The `source` field tells the judge which policy layer set each class — useful for explaining decisions in `Judgment.reason` ("blocked because high_risk → escalate, inherited from project floor").

## Cost treatment

Per rule #4, every code path that calls an LLM has a cost gate. The judge is no exception.

- Judge calls flow through `_check_cost_guardrails` like any other LLM call, but against a **separate ledger**.
- Each cost event written to the JSONL log carries `cost_source: "actor" | "judge"`. The existing `_costs.sum_cost_for_period()` is updated by the implementation PR to take a `source` filter so the actor budget cap and the judge budget cap are summed independently. This is the load-bearing change that makes "judge spend not subtracted from actor budget" actually true — without it, judge cost events would pollute the actor cost stream silently.
- **Legacy cost events (records without `cost_source`) count as `actor`.** Existing deployments upgraded to a judge-aware framework see no change in actor-budget arithmetic. Only judgment events emitted by the new code carry `cost_source: "judge"`. The implementation PR adds the field to the cost-event dataclass with `Literal["actor", "judge"] = "actor"` as the default for safe backward-compatible parsing.
- `judges.md` declares the judge budget (`daily_usd`, `monthly_usd`) — judge spend is summed against this ledger only.
- **`critical=True` does not bypass the judge.** Critical actions are eligible for `ALLOW` from the judge but the judge **always runs**. The audit trail is the point, and critical actions are exactly the ones that most need recording. This resolves the cost/critical tension explicitly: cost cap on the *actor* may be overridden by `critical=True`; cost cap on the *judge* cannot — it raises `JudgeBudgetExhausted` which the `failure_policy` resolves (default block).
- Specialist composition multiplies cost — operators see the multiplication in the dashboard before opting in.

The discipline: judgment is expensive; the cost is the price of the safety property; operators decide how much they want to pay. The ledger split is non-negotiable — without it the "separate budget" promise is unfalsifiable.

## Audit shape

Each judgment writes a JSONL line to the run log, carrying `parent_run_id` linking back to the actor's run. The JSONL shape is the `JudgmentEvent` dataclass defined above, serialized via the framework's existing JSONL writer. Filesystem-default behavior proposed by this spec:

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
  "raw_outcome": "block",
  "enforcement_action": "audit_bypass | block_executed | allow_executed | allow_pending_next_judge | revise_pending_second_judgment | revise_executed | revise_invalid_amendment | revise_loop_exhausted_blocked | escalate_pending | approved_executed | approved_stale_tool_definition | denied | redacted | auto_decided_block | auto_decided_allow | proposal_body_tampered | operator_revise_executed | operator_revise_invalid_amendment",
  "synthesis_source": "class_policy | failure_policy | null",
  "triggered_by": "failure_policy:<ExceptionName> | null",
  "escalation_queue_id": "proposal_20260512T143052_xyz98765 | null",
  "binding": {
    "tool_call_id": "...",
    "tool_definition_hash": "sha256:...",
    "arguments_hash": "sha256:..."
  },
  "latency_ms": 412,
  "cost_usd": 0.00073,
  "cost_source": "judge",
  "ts": "2026-05-12T14:30:52Z"
}
```

`raw_outcome` is what the judge returned. `enforcement_action` is what the framework did with that outcome — they differ on `read_audit_mode` (judge can return BLOCK but framework executes), on operator escalation overrides, and on failure_policy resolutions. This lets the dashboard count "judge would have blocked but read-audit bypassed" distinctly from "judge allowed and we executed" — which Round 2 caught as indistinguishable in the v1 audit shape.

**Enforcement-action enum.** The v1 spec listed five values (`audit_bypass`, `block_executed`, `allow_executed`, `revise_executed`, `escalate_pending`). Reference-implementation work since extended the enum:

- `allow_pending_next_judge` (PR 2b ensemble) — a judge in a multi-judge ensemble ALLOWed, but subsequent judges have not yet voted. Promoted to `allow_executed` on the LAST event when the ensemble's overall verdict is ALLOW. Intermediate ALLOWs stay `allow_pending_next_judge`.
- `approved_executed` (PR 3b) — operator wrote `### Approved by <op>` to a PENDING file; framework re-verified `tool_definition_hash`; executed the bound action.
- `approved_stale_tool_definition` (PR 3b) — operator wrote Approved but the tool's `input_schema` / handler changed since PENDING-write. Refused execution; PENDING file preserved.
- `denied` (PR 3b) — operator wrote `### Denied by <op>` to a PENDING file. No execution.
- `redacted` (PR 3b) — operator wrote `### Redacted by <op>` to a PENDING file (body redacted, frontmatter preserved). No execution.
- `auto_decided_block` / `auto_decided_allow` (PR 3b) — `auto_decide_after_seconds` elapsed; framework applied `fallback_on_timeout` policy from `judges.md`. The CAS write detects operator-edit races and defers to the operator on conflict.
- `proposal_body_tampered` (PR 3b) — operator edited the `## Proposal` body of a PENDING file between write and resolution. Framework recomputes `arguments_hash` from the body's tool_arguments and refuses execution on mismatch. Action is treated as denied; PENDING file preserved in audit trail.
- `revise_pending_second_judgment` (PR 3c) — a judge in the ensemble returned `Judgment(outcome=REVISE, amendment=...)`; the framework has built the amended proposal and is about to recurse with `revise_iteration=1`. This event records the FIRST judgment's REVISE intent. The second-judgment event chain follows and replaces this with `revise_executed` (action ran) or `revise_loop_exhausted_blocked` (second judgment also REVISEd).
- `revise_invalid_amendment` (PR 3c) — judge advertised REVISE but the amendment failed validation (no `amendment` payload, unknown tool, args not a dict, args_hash recompute failed, write-path violation). Action refused; second judgment NOT run.
- `revise_loop_exhausted_blocked` (PR 3c) — the second judgment returned REVISE again. Per spec/28:276, `max_revise_iterations=1`. Action refused; reason carries `revise_loop_exhausted`.
- `operator_revise_executed` (PR 3c) — operator wrote `### Revised by <op>` to a PENDING file with an embedded amendment YAML. Framework parsed + validated + (for high_risk) re-judged → executed. Audit field `re_judged: bool` records whether the re-judge ran (true on high_risk, false on lower classes).
- `operator_revise_invalid_amendment` (PR 3c) — operator's Revised block had no embedded amendment YAML, malformed YAML, unknown fields, or the amendment failed validation. Action refused.

**`synthesis_source`** (PR 3b) — set when the framework, not a real judge, produced the ESCALATE outcome. Values: `"class_policy"` (operator's `class_policy.<X>=escalate` fired without ensemble), `"failure_policy"` (a judge raised an exception mapped to escalate via `failure_policy`), `null` (a real judge returned ESCALATE). This lets dashboards distinguish judge-driven escalations from framework-synthesized ones.

**`triggered_by`** (PR 3b) — populated for failure_policy synthesis only. Value: `"failure_policy:<ExceptionName>"`. Operators auditing a stall caused by `JudgeUnavailable: backend timeout` see exactly which exception class drove the escalate.

**`escalation_queue_id`** (PR 3b) — populated when the event's outcome is `escalate` or when a resolution event links back to a PENDING file. Equals the `proposal_id` (which doubles as the queue key — the framework does not mint a separate ID).

**`Response.deferred` semantics** (PR 3b). `agent.call()` returns `Response(deferred=True, escalation_queue_ids=[id1, id2, ...])` when any tool_use in the actor's assistant turn produces an ESCALATE outcome. The list (not a singular id) accommodates the case where one turn proposes multiple actions, of which two or more escalate. ALLOWed tool_uses in the same turn still execute and their results land in `Response.tool_calls`; the multi-turn loop terminates immediately after the iteration (no follow-up LLM call to close out the partial state). Operator resolution of the PENDING file does NOT replay the result to the original actor — execution is terminal, with `cost_source="actor"` keeping the spend on the proposing run's ledger.

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

## Conformance status (PR 4)

`tests/test_judge_protocol_conformance.py` ships ~37 tests covering the invariants enumerated in §"Conformance suite" above plus the PR 3a/3b/3c state-machine additions. The suite parametrizes over the two shipped JudgeBackends:

- `PolicyJudge` (rule engine; offline; runs every invariant except the LLM-only canary).
- `LLMJudgeBackend` wired to a deterministic `_StubLLMBackend` (offline; runs every invariant including the UUID-canary serialization assertion for `JudgeRuntimeConfig` non-leakage).

Coverage map:

- **Framework-side** (run once): hash determinism + sensitivity, project-floor non-relaxable, atomic-snapshot semantics, ESCALATE state machine (O_EXCL sidecar, body integrity, strict resolution-block parser, auto-decide CAS), REVISE state machine (`amend_proposal` recomputes classification, `JudgeAmendedProposalRejected` on schema-invalid).
- **Per-backend** (parametrized): Protocol surface (`isinstance`), `evaluate` idempotency, `policy_version` changes on policy change, `policy_version` is non-sentinel, `judge_id` stable, `close()` idempotent, `supports_read_audit` + `supports_specialist_composition` return bools, `supported_outcomes` returns the canonical set.
- **LLM-only**: latency-bounded timeout → `JudgeUnavailable`; concurrent-call connection-state integrity; UUID-canary assertion that `JudgeRuntimeConfig` fields never appear in the serialized LLM prompt.
- **Deferred** (filed as follow-up issues): judge_budget_counter live state (not implemented). Full JSON-Schema validation of amended `tool_arguments` ships in PR 5b under the opt-in `[validation]` extra — see [Schema validation](#schema-validation) above for the parser surface, exception taxonomy, and load-time gating semantics.

The conformance suite is reusable by third-party `JudgeBackend` implementations: importing the fixtures + invariant tests into a downstream package exercises any registered backend.

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
- `docs/spec/27-doctor.md` (extends with `check_judge_health`, `check_judge_policy_sync`, `check_judge_policy_floor`, `check_mcp_tool_classification`, `check_escalation_conflicts`, and the `vault_synced_judge_captures_off` warning)
- #87 LLMBackend (judge calls flow through this)
- #89 PolicyBackend (org-scale policy; composes with judges)
- #61 LogBackend (judgment events; same protocol)
- #110 RFC (this spec's origin)

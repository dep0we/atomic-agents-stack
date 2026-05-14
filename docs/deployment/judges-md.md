# Configuring the judge layer (`judges.md`)

How to author `judges.md` to control the **judge layer** — atomic-agents'
pre-action validation surface that inspects every side-effectful tool
call before it runs, decides ALLOW / BLOCK / REVISE / ESCALATE, and
writes a judgment event to the audit trail.

This doc is the operator-facing reference. The full design rationale
lives in the canonical spec at [`docs/spec/28-judge-layer.md`](../spec/28-judge-layer.md);
this page is the *how-to-author-it* guide that pairs with the shipped
parser.

> **Status:** The judge layer is **locked** as of `#112` PR 4
> (2026-05-14). Shipped + locked: Protocol contract, two reference
> impls (`PolicyJudge` rule engine + `LLMJudgeBackend` LLM-backed),
> the `judges.md` parser, cascade-aware project floor, ESCALATE state
> machine (PENDING file writer + operator resolution polling +
> auto-decide timeout + inline Approved execution), REVISE state
> machine (judge-driven amendment + second-judgment cycle bounded at
> `max_revise_iterations=1`, operator `### Revised by <op>` resolution
> with embedded `amendment:` YAML + class-upgrade re-judge gate), and
> a 38-test conformance suite parametrized over both shipped backends.
> PR 5a ships the per-class `fallback_on_timeout` map (see
> [`escalation`](#escalation) below). PR 5b ships full JSON-Schema
> validation of amended `tool_arguments` via an opt-in `[validation]`
> extra and a new `validation:` field — see
> [Validation](#validation) below.

---

## When to use the judge layer

| You want…                                                                          | Use…             |
|------------------------------------------------------------------------------------|------------------|
| `tools.md` write paths to be advisory exactly as today (no judge invocation).      | **No `judges.md`.** The opt-in gate stays off. |
| Every side-effectful tool call to write an audit-only judgment event.              | `judges.md` with `class_policy: allow_with_audit` per class. |
| Every side-effectful tool call to be evaluated by a judge before execution.        | `judges.md` with `class_policy: judge_required` per class. |
| `delete_files`-class actions to pause for operator approval.                       | `judges.md` with `high_risk: escalate`. See [Escalation queue](#escalation-queue) below. |
| A multi-agent project to enforce a minimum policy across every delegate.           | Drop a `judges.md` at the **project root** — it becomes the non-relaxable floor. |

The judge layer is fully opt-in. Existing deployments see no judge
invocation until they add `judges.md`. The framework will not
auto-enable it in a future minor release.

---

## Minimum-viable `judges.md`

Drop this file at `<agent>/judges.md`:

```markdown
# Judges — <agent-name>

```yaml
backend: rules
class_policy:
  read_only: bypass
  reversible_write: allow_with_audit
  external_side_effect: judge_required
  high_risk: escalate
```
```

That's the entire contract. The first time the agent dispatches a
side-effectful tool call, the framework:

1. Loads `judges.md` and parses the embedded YAML.
2. Builds an `ActionProposal` from the LLM's `tool_use` block plus any
   `atomic_action` side-channel marker the actor emitted.
3. Runs `PolicyJudge` (rule-engine, microseconds, always-on) against the
   proposal — it matches `tools.md` write paths, allowlists, deny rules,
   and the class policy from your `judges.md`.
4. Runs `LLMJudgeBackend` if `OPENAI_API_KEY` resolves and `PolicyJudge`
   didn't BLOCK.
5. Writes a JSONL `JudgmentEvent` audit line per judge to the agent's
   log dir, carrying `raw_outcome`, `enforcement_action`, `binding`
   (tool_call_id + hashes), and `cost_source: "judge"`.
6. Executes the bound tool call (ALLOW), refuses with the judge's reason
   (BLOCK), or short-circuits the ensemble on first BLOCK.

---

## File shape

`judges.md` is markdown with embedded YAML blocks, matching the
`model.md` precedent (`cost_guardrails` is parsed the same way). The
parser:

- Reads every fenced ```` ```yaml ```` block in the file.
- Merges blocks in document order — **later blocks win on per-key conflicts**.
- Ignores every other markdown line (headings, prose, lists outside YAML).

This means you can interleave human-readable narrative with the
operator config:

```markdown
# Judges — Caldwell

Caldwell is a financial-advisor agent. We escalate any action that
writes to user-visible surfaces (email, posts), and we bypass the
judge entirely for read-only memory recall.

```yaml
backend: llm
model: gpt-5-nano
timeout_ms: 5000
budget:
  daily_usd: 0.50
  monthly_usd: 10.00

class_policy:
  read_only: bypass
  reversible_write: allow_with_audit
  external_side_effect: judge_required
  high_risk: escalate
```

## Why escalate on high-risk

We don't want Caldwell auto-approving an outbound email that names a
client; the operator should see those before they ship.

```yaml
failure_policy:
  JudgeUnavailable: block
  JudgeBudgetExhausted: block
```
```

Multiple YAML blocks are merged top-down — useful when you want to
group related fields next to the prose that explains them.

If any YAML block fails to parse, the agent fails **loud at load
time** with `JudgePolicyInvalid` and a message naming the offending
field and the allowed values. Operator typos surface immediately
rather than fail-closing every action at runtime.

---

## The four class policies

Every tool call is classified per [`tools.md`](../spec/03-file-formats.md)
into one of four action classes. The `class_policy` block in
`judges.md` says what the framework does with each class.

| Policy              | What it means                                                                                                                | When to use                                                                                                          |
|---------------------|------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `bypass`            | Skip the judge entirely. No proposal built, no judgment event written, no audit trail line. The class behaves pre-#112.       | `read_only` actions where the operator trusts the actor's read pattern and doesn't want judgment cost on every read. |
| `allow_with_audit`  | Run the judge, **always allow**, but write the full judgment event to the audit trail. Surface for "I want to see what the judge would say without it gating." | Production rollouts where you want visibility into the judge's calibration before letting it block actions.          |
| `judge_required`    | Run the judge; **its outcome is enforced**. BLOCK refuses; ALLOW executes; REVISE/ESCALATE follow spec/28 semantics.            | The default for `reversible_write` and `external_side_effect` actions once the judge is calibrated.                  |
| `escalate`          | Pause for operator approval before execution. Framework writes a PENDING file to `vault/escalations/<class>/<proposal_id>.md`; `agent.call()` returns `Response(deferred=True, escalation_queue_ids=[...])`. On a later `call()` (or explicit `agent.poll_escalations()`), the framework reads the operator's resolution block and either executes (Approved), refuses (Denied), or auto-decides on timeout. See [Escalation queue](#escalation-queue) below. | `high_risk` actions (`delete_files`, `force_push`, `production_deploy`) and anything else the operator wants eyes-on. |

Strictness ordering: `bypass < allow_with_audit < judge_required < escalate`.
A delegate's `judges.md` may *strengthen* a class's policy but cannot
*relax* it below the project floor (see [Cascade-aware project floor](#cascade-aware-project-floor)
below).

### Default-fill

Operators may specify only the classes they want to override. Unspecified
classes get conservative defaults per spec/28:

```yaml
class_policy:
  # Only specify what you want to override.
  high_risk: escalate
  # read_only, reversible_write, external_side_effect default-fill to
  # bypass, judge_required, judge_required respectively.
```

The parsed `ClassPolicySnapshot` carries a `source` field per class —
`"judges.md"` (operator-specified), `"default"` (default-fill), or
`"project_floor" / "floor"` (inherited from project floor; see below).
This lets the judge explain its decision in
`Judgment.reason` ("blocked because `high_risk → escalate`, inherited from
project floor").

---

## Cascade-aware project floor

In multi-agent project layouts (per [spec/06](../spec/06-multi-agent-projects.md)),
`<project>/judges.md` is the **non-relaxable floor** for every agent in
that project. Per spec/28 §408:

- A delegate's own `<agent>/judges.md` may set per-class policies **at
  least as strict** as the project floor's.
- Attempts to relax raise `JudgePolicyInvalid` at agent-load time with
  a message identifying the offending class.
- Classes the delegate didn't explicitly override **inherit the floor's
  value** (or the spec default if the floor didn't override either).

Concrete example. Project floor at `<project>/judges.md`:

```yaml
class_policy:
  high_risk: escalate          # the floor
  external_side_effect: judge_required
```

Delegate at `<project>/agents/caldwell/judges.md`:

```yaml
class_policy:
  high_risk: escalate          # OK — same as floor
  reversible_write: judge_required  # OK — stricter than default
```

Delegate that **relaxes** the floor (rejected):

```yaml
class_policy:
  high_risk: judge_required    # ❌ JudgePolicyInvalid at load
```

The pattern: a project lead can drop a single `judges.md` at the
project root that guarantees a floor of behavior across the whole
fleet, and trust that no per-agent config can weaken it. Per-agent
files may strengthen further if a specialist needs stricter rules.

The framework reads `<agent>/judges.md` first, then walks to the
project root to find the floor. Other fields (model, budget, escalation,
`failure_policy`) merge via "delegate wins on present keys; floor fills
the rest."

---

## `failure_policy` — what happens when the judge errors

When a judge raises (network outage, timeout, budget exhausted,
malformed proposal, schema validation failure), the framework consults
`failure_policy` to decide the enforcement outcome.

**Default: fail-closed for every exception type** (`block`). Operators
must explicitly opt into looser behavior.

Two shapes are accepted for operator ergonomics. Pick whichever fits
your config:

### Flat shape (most common)

Same fallback applied to every action class:

```yaml
failure_policy:
  JudgeUnavailable: block
  JudgeBudgetExhausted: block
  JudgePolicyInvalid: block
  JudgeProposalInvalid: block
  JudgeAmendedProposalRejected: block
```

### Nested per-class shape (advanced)

Different fallback per `(action_class, exception)` pair — lets you
tolerate `JudgeUnavailable` on `read_only` but still block on
`high_risk`:

```yaml
failure_policy:
  read_only:
    JudgeUnavailable: allow      # read_only actions tolerate judge outages
  high_risk:
    JudgeUnavailable: escalate   # high_risk actions escalate to operator
    JudgeBudgetExhausted: escalate
```

The parser auto-detects the shape: if any top-level key is a recognized
exception name (`JudgeUnavailable`, `JudgeBudgetExhausted`,
`JudgePolicyInvalid`, `JudgeProposalInvalid`, `JudgeAmendedProposalRejected`),
it's flat. Otherwise top-level keys must be action class names
(`read_only`, `reversible_write`, `external_side_effect`, `high_risk`).

Mixing the two shapes in one block raises `JudgePolicyInvalid` —
operators get one rule to remember.

Unspecified `(class, exception)` pairs fill in from the spec default
(`block`). You can override a single pair without spelling out the
rest.

### Recognized exception names

Only these names are valid in `failure_policy`. Typos raise
`JudgePolicyInvalid` at load time:

| Exception                       | Raised when…                                                              |
|---------------------------------|---------------------------------------------------------------------------|
| `JudgeUnavailable`              | Backend cannot respond (timeout, network, provider outage).               |
| `JudgePolicyInvalid`            | `judges.md` or `tools.md` cannot be parsed at agent-load time. Also raised at REVISE time under `validation: strict` when a registered tool's `input_schema` is malformed or has broken `$ref`s. |
| `JudgeBudgetExhausted`          | Judge cost cap hit (separate ledger from actor budget).                    |
| `JudgeProposalInvalid`          | Proposal missing required fields for its class.                            |
| `JudgeAmendedProposalRejected`  | REVISE's amended proposal failed re-validation.                            |

---

## Escalation queue

When the judge ensemble returns ESCALATE — or when
`class_policy.<X>=escalate` synthesizes the outcome at the framework
layer — the action is paused and queued for operator approval. The
queue is a directory of plain markdown files; operators resolve a
PENDING by editing the file in any text editor (Obsidian, vim, VS Code)
and writing a resolution block at the bottom. There is no separate
inbox UI in PR 3b — the vault IS the queue.

### Lifecycle

1. **Framework writes PENDING.** On ESCALATE, the framework atomically
   writes `<agent_root>/vault/escalations/<action_class>/<proposal_id>.md`
   with frontmatter (state, agent, action_class, judge_id,
   `escalated_at`, `policy_version`, `schema_version`) plus a `## Proposal`
   block carrying the full `ActionProposal` serialized as fenced YAML
   and a `## Judge's reason for escalating` block carrying the judge's
   prose. `agent.call()` returns `Response(deferred=True, escalation_queue_ids=[id1, ...])`.
2. **Operator opens the file** and writes exactly ONE resolution block at
   the bottom (under the existing `## Resolution` heading). See the
   [Resolution block grammar](#resolution-block-grammar) below.
3. **Framework polls** — at the top of any subsequent `agent.call()` (or
   when an operator runs `agent.poll_escalations()` directly), the
   framework scans the queue subject to the
   `resolution_poll_cycle_seconds` throttle. The poller is
   crash-recoverable and de-duplicated: concurrent pollers race a
   `.<proposal_id>.resolved-emitted` sidecar via `O_CREAT|O_EXCL`, so
   exactly one emits the RESOLVED audit event.
4. **Framework acts on the resolution.**
   - **Approved** → re-verify `tool_definition_hash` against the
     current tool registry (refuses if the tool's schema or handler
     changed since PENDING — audited as `approved_stale_tool_definition`),
     then execute the bound action inline. `cost_source="actor"` keeps
     the spend on the original actor's ledger.
   - **Denied** → no execution; audit records refusal.
   - **Redacted** → no execution; audit records redaction. The body is
     replaced with the operator's redaction reason; frontmatter is
     preserved.
   - **Auto-decided by framework** → when `auto_decide_after_seconds`
     elapses without an operator block, the framework writes its own
     `### Auto-decided by framework` block applying
     `fallback_on_timeout` (default `block`). The auto-decide path uses
     a sha256 compare-and-swap before writing, so an operator who
     edits the file in the same poll cycle wins (framework defers to
     the next cycle).

### Resolution block grammar

Operators write exactly ONE block at the bottom of the PENDING file.
Headers MUST match this grammar exactly:

```
### <Verb> by <operator>
```

Where:

- `###` is an h3 prefix (not h2, not h4).
- `<Verb>` is one of `Approved`, `Denied`, `Redacted`,
  `Auto-decided`, `Revised` — **exact case** (capital A/D/R, lowercase
  rest, hyphen in `Auto-decided`).
- The literal word `by` separates the verb and the operator name.
- `<operator>` is a non-empty string (your name, your handle, a service
  account ID — whatever you want on the audit trail).

Typos surface as **doctor warnings**, not silent denials. A malformed
header (`### approved by alice`, `### Approved By alice`, `#### Approved by alice`)
makes the file UNPARSEABLE — the framework leaves it as-is, does NOT
claim the de-dup sidecar, and surfaces the bad file on the next
`atomic-agents doctor` run. Fix the typo and the next poll picks it
up.

**First block wins.** If two resolution blocks appear in the same
file, the framework processes the *first* one (top-down). The
file's `state:` frontmatter field is the authoritative state. Two
operators editing concurrently are responsible for not stomping each
other; the framework does not attempt to merge.

**Revised (PR 3c)** ships the operator-amendment path — see
[Operator-revise resolution](#operator-revise-resolution) below for the
full grammar, the embedded `amendment:` YAML block, the class-upgrade
gate, and what `re_judged` means in the audit trail. PR 3c also ships
the judge-driven REVISE cycle (an ensemble judge returns
`Judgment(outcome=REVISE, amendment=ProposalAmendment(...))` and the
framework recurses against the amended proposal); operators see that
path as a sequence of `revise_pending_second_judgment` →
`revise_executed` audit events, no PENDING file written.

### Block bodies

Each block carries free-prose context that flows to the audit trail.
The framework parses the header for the decision and surfaces the
body to the JSONL audit event verbatim.

Approved (the most common):

```
### Approved by alice
resolved_at: 2026-05-13T09:14:22Z
note: Reviewed the proposal — sender list is correct, attachment is the public report.
```

Denied:

```
### Denied by alice
resolved_at: 2026-05-13T09:14:22Z
note: Wrong distribution list. Have the agent re-author against the customer list, not the prospect list.
```

Redacted (use when the PENDING contains sensitive details that
shouldn't persist in the queue history):

```
### Redacted by alice
redacted_at: 2026-05-13T09:14:22Z
redaction_reason: PENDING body contained customer PII not appropriate for retention.
```

`resolved_at` / `redacted_at` is operator-supplied prose; the framework
also stamps its own `resolved_at` ISO-8601 timestamp on the RESOLVED
audit event.

### Operator-revise resolution

When the proposal is *directionally right but unsafe as written* —
strip an attachment, lower a spend limit, swap to a draft-PR variant —
the operator writes `### Revised by <op>` with an embedded
`amendment:` YAML block. The framework parses the amendment, applies
it to the PENDING file's `ActionProposal`, re-validates, gates on the
**recomputed** class, and executes the amended bound action (re-judging
first when the amended class is `high_risk`).

#### Block format

```
### Revised by alice
resolved_at: 2026-05-13T09:14:22Z
note: Stripping attachment per security review; the body is the public summary only.
amendment:
  ```yaml
  judge_note: "operator stripped attachment per security review"
  tool_arguments:
    to: "stakeholders@example.com"
    subject: "Q1 summary"
    body: "Summary follows in-line; attachment removed."
  ```
```

The outer block is the markdown resolution block (h3 header, same grammar
as Approved / Denied / Redacted). The inner ```` ```yaml ```` fence
under `amendment:` carries the amendment payload. Operators authoring
in Obsidian or VS Code: indent the YAML under `amendment:` so the
markdown renderer keeps it visually nested — the parser dedents before
loading.

#### Amendment fields

The amendment carries **only** judge-amendable fields per spec/28 §"Revise".
`reason` and `authorization` come from the original proposal and cannot
be rewritten by an amendment.

| Field                  | Type                                                          | Purpose                                                                                                                          |
|------------------------|---------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| `judge_note`           | string (optional — defaults to `"operator amendment"`)         | Free prose explaining the amendment. Flows into the second-judgment context and the audit trail.                                  |
| `tool_name`            | string (optional — defaults to original)                       | Swap the tool. Triggers framework recomputation of `classification` from `tools.md` / `mcp.md` — see [class-upgrade gate](#class-upgrade-gate-recomputed-classification-not-original) below. |
| `tool_arguments`       | mapping (optional — defaults to original)                      | Amended args. Must be a dict; non-canonicalizable values (sets, custom classes) raise `JudgeAmendedProposalRejected`.              |
| `target_audience`      | string (optional)                                              | Override the original's target_audience.                                                                                          |
| `expected_consequence` | string (optional)                                              | Override the original's expected_consequence.                                                                                     |
| `reversibility`        | enum: `reversible` \| `partial` \| `irreversible` (optional)   | Override the original's reversibility. Unknown values raise `JudgeAmendedProposalRejected`.                                       |
| `rollback_path`        | string (optional)                                              | Override the original's rollback_path.                                                                                            |
| `appended_evidence`    | list of `Evidence` mappings (optional)                          | Appended to the original's evidence list. Each item must be a mapping that constructs an `Evidence` dataclass; malformed → refuse. |

Unknown fields raise `JudgeAmendedProposalRejected` at parse time —
typos in the amendment surface loud, not silent.

#### Class-upgrade gate (recomputed classification, not original)

When the operator swaps `tool_name`, the framework **recomputes** the
amended proposal's classification from the new tool. The high-risk
re-judge gate keys on the **recomputed** class, NOT the PENDING file's
original `action_class`. This means an operator who phrases an
amendment as `reversible_write` → `delete_files` (or anything else
registered at `high_risk`) cannot skip the second-judgment eyes:

- **Amended class == `high_risk`** → fresh ensemble runs against the
  amended proposal. Only executes on ALLOW. BLOCK refuses; the audit
  records the re-judge chain plus the refusal. Audit field
  `re_judged: true`.
- **Amended class != `high_risk`** → schema/policy validation alone is
  sufficient (tool registered, args dict-shaped, `arguments_hash`
  recomputes, write-path enforcement re-runs). Executes on success.
  Audit field `re_judged: false`.

The `re_judged` field is **framework-set, not operator-supplied**.
Operators express intent via the amendment; the framework decides
whether re-judge fires based on the amended classification. Specifying
`re_judged:` in the block body is ignored (and may be rejected as an
unknown field in a future PR).

#### Audit shape

Successful operator-revise emits one JSONL line to the agent's run log:

```json
{
  "trigger": "escalation_operator_revise_executed",
  "parent_run_id": "<original actor's run_id>",
  "escalation_queue_id": "<original proposal_id>",
  "original_proposal_id": "<original proposal_id>",
  "amended_proposal_id": "<freshly-minted amended proposal_id>",
  "tool_name": "<amended tool_name>",
  "re_judged": true | false,
  "cost_source": "actor"
}
```

When `re_judged: true`, the re-judge ensemble's `JudgmentEvent` lines
are also emitted (one per invoked judge) with
`trigger: "escalation_operator_revise_re_judge"` and
`original_proposal_id` linking back to the actor's original proposal.
`parent_run_id` on the re-judge events points at the **original
actor's** `run_id`, not whatever agent.call() happened to fire the poll
— forensic chains stay walkable from amended back to actor-original.

Invalid amendments (missing YAML block, malformed YAML, unknown
fields, unknown tool, non-dict `tool_arguments`, args_hash recompute
failure, write-path violation) emit
`enforcement_action="operator_revise_invalid_amendment"` and refuse
execution. The PENDING file is preserved.

#### Validation deferred to PR 4

PR 3c does NOT run full JSON-Schema validation against the amended
`tool_arguments` (spec/28:274 calls for it; deferred to PR 4 with the
`jsonschema` dep). The framework checks: (1) `tool_name` resolves to a
registered handler in the tool_registry, (2) `tool_arguments` is a dict
(not None, not a list, not a scalar), (3) `arguments_hash` recomputes
successfully via `canonical_sha256`. The framework emits a one-shot
warning per agent the first time operator-revise validation runs
without full JSON-Schema — operators see the gap in agent logs.

### Auto-decide behavior

When `escalation.auto_decide_after_seconds` is set and the PENDING
file's `escalated_at` is past the timeout, the framework writes a
synthetic resolution block on the next poll cycle:

```
### Auto-decided by framework
resolved_at: 2026-05-13T13:30:00Z
reason: auto_decide_after_seconds=86400 elapsed; fallback_on_timeout=block (resolved for action_class=external_side_effect)
```

The framework flips the file's `state:` frontmatter to `resolved`,
emits a RESOLVED audit event with `enforcement_action="auto_decided_block"`
(or `"auto_decided_allow"` if the resolved policy is `allow`), and
does NOT execute the action when fallback is `block`. The
parenthetical records the `action_class` the framework keyed on when
resolving per-class policy — see *Per-class `fallback_on_timeout`*
under [`escalation`](#escalation).

**Race safety**: the auto-decide write is gated on a sha256
compare-and-swap. If an operator edits the file between the poller's
initial read and the framework's atomic_write, the framework aborts
the auto-decide and defers to the operator's edit (which the next
poll cycle picks up as a normal Approved/Denied/Redacted block). The
auto-decide is idempotent — the timeout has still passed, so retrying
on the next cycle is safe.

### Body integrity

On every operator resolution, the framework recomputes
`arguments_hash` from the `## Proposal` block's `tool_arguments` field
and compares against the embedded `arguments_hash` value. Mismatch →
`enforcement_action="proposal_body_tampered"`, **no execution**.

**Scope of the check**: this catches accidental edits (operator
mis-pastes the proposal, fixes a typo in the args) and lazy tamper
(operator changes the args without recomputing the embedded hash). A
sophisticated operator can recompute the hash to match — the
embedded hash is in the same file. Operator approval is itself the
trust anchor; the body-integrity check is a guard against careless
edits, not against a hostile operator. Operators with vault write
access can also write a fresh PENDING file from scratch with any
arguments they want.

### Audit shape

Every resolution emits a JSONL `JudgmentEvent` line to the agent's
run log. The relevant fields:

- `enforcement_action` distinguishes the exact action the framework
  took: `approved_executed`, `approved_stale_tool_definition`,
  `denied`, `redacted`, `auto_decided_block`, `auto_decided_allow`,
  `proposal_body_tampered`, `operator_revise_executed`,
  `operator_revise_invalid_amendment` (PR 3c — operator-revise paths).
  Judge-driven REVISE in the same ensemble emits
  `revise_pending_second_judgment` on the first judgment and
  `revise_executed` / `revise_invalid_amendment` /
  `revise_loop_exhausted_blocked` on the recursion.
- `synthesis_source` (only set when the framework — not a real judge
  — produced the ESCALATE) tells you why the PENDING was written:
  `"class_policy"` (operator's `class_policy.<X>=escalate` fired
  without invoking the judge ensemble) or `"failure_policy"` (a
  judge raised an exception mapped to `escalate` via
  `failure_policy`). `null` (or omitted) means a real judge in the
  ensemble returned ESCALATE.
- `triggered_by` (populated only for `failure_policy` synthesis)
  names the exception class: `"failure_policy:JudgeUnavailable"`,
  etc. Operators auditing a stall caused by a backend outage see
  exactly which exception drove the escalate.
- `escalation_queue_id` equals the `proposal_id` — the framework
  does not mint a separate queue ID.
- `revise_iteration` (PR 3c — judge-driven REVISE only) is `0` on the
  first judgment, `1` on the second. `max_revise_iterations=1` is
  bounded by spec/28:276; a second judgment returning REVISE produces
  `revise_loop_exhausted_blocked`.
- `original_proposal_id` (PR 3c) links a second-judgment or
  operator-revise audit event back to the original
  pre-amendment `proposal_id`. Forensic chains stay walkable from
  amended back to actor-original. `re_judged: bool` on the executed
  event reports whether the ensemble re-ran (operator-revise high_risk
  → true; lower classes → false; judge-driven REVISE second-judgment
  events → always true).
- `revised_from_proposal_id` (PR 3c) appears in PENDING-file
  frontmatter when an ESCALATE fires inside a judge-driven REVISE's
  second judgment — the new PENDING carries the original `proposal_id`
  so operators reviewing the queue can chain back.
- For the original actor's run, the deferred tool_use is recorded
  with `trigger: "tool_call_deferred"` and a synthesized
  `ToolCallResult(deferred=True, error="judge_deferred: ESCALATE — see escalation_queue_id=...")`.
  Consumers iterating `response.tool_calls` distinguish deferred
  from genuine handler errors via the `deferred: bool` field, not by
  string-matching `error`.

The PENDING file is preserved in the audit trail across its full
lifecycle (PENDING → resolved | redacted), so a fleet auditor can
reconstruct the operator decision from the file itself.

### Standalone-invocation caveat

`agent.poll_escalations()` is public and operators may call it
directly (e.g., from a future `atomic-agents poll-escalations` CLI).
When invoked standalone (NOT via `agent.call()`), the MCP client pool
is NOT initialized — `call()` is what wires it up after the cost
gate. If an Approved escalation's tool is an MCP tool, the tool
registry lookup returns `None` and the resolution is recorded as
`approved_stale_tool_definition` — safe (fail-closed) but misleading
(the tool isn't stale; the framework just hasn't loaded MCP yet).
Wire MCP init into a standalone CLI before relying on Approved
MCP-tool execution; tracked in [#166](https://github.com/dep0we/atomic-agents-stack/issues/166).

---

## Other knobs

### `backend`, `model`, `timeout_ms`

```yaml
backend: llm           # "rules" | "llm" | custom registered name. Default: "rules".
model: gpt-5-nano      # Used by LLMJudgeBackend; ignored by rules. Default: gpt-5-nano.
timeout_ms: 5000       # Per-judge call timeout. Default: 5000.
```

The default `LLMJudgeBackend` uses `gpt-5-nano` (OpenAI) for
correlated-judgment mitigation against the default Anthropic actor. In
Claude-only deployments (no `OPENAI_API_KEY` resolvable), the LLM judge
is **lazy-skipped** — `make_default_llm_judge` returns `None` and
PolicyJudge runs alone. No spurious `JudgeUnavailable` blocks.

### `budget`

Judge spend runs on a **separate ledger** from the actor. Critical
actions bypassing the actor cap (`critical=True` in cost guardrails) do
**not** bypass the judge cap — that's the point.

```yaml
budget:
  daily_usd: 0.50         # Per-day judge spend cap.
  monthly_usd: 10.00      # Per-month judge spend cap.
  per_action_usd: 0.01    # Per-call judge spend cap.
```

All three fields are optional. Caps are non-negative; negatives raise
`JudgePolicyInvalid` at load time.

### `escalation`

Controls where PENDING files are written, how long the framework waits
for the operator before auto-deciding, the fallback verdict on timeout,
and how often the resolution poller scans the queue.

```yaml
escalation:
  destination: vault/escalations/         # Vault-relative directory. Default "vault/escalations/". Legacy "vault" is accepted as an alias and normalized.
  auto_decide_after_seconds: 86400        # Wait at most 24h for operator decision. None / omitted = wait indefinitely.
  fallback_on_timeout: block              # What to do when auto_decide_after expires. allow | block. Default block. (revise/escalate are judge-driven outcomes and have no semantics in the no-judge-responded path; the parser rejects them at load time.)
  resolution_poll_cycle_seconds: 60       # Throttle: at most one queue scan per N seconds inside agent.call(). Default 60.
```

The poller is opportunistic — it runs at the top of `agent.call()`
whenever the throttle window has elapsed since the last scan (the
`.last-poll` marker lives in the destination directory). Operators who
want a clock-driven poll independent of agent traffic can call
`agent.poll_escalations()` directly from a cron / launchd job.

#### Per-class `fallback_on_timeout` (PR 5a of #112)

`fallback_on_timeout` accepts either a single string (applied to every
`ActionClass`) or a mapping keyed by `ActionClass.value` strings with a
mandatory `default:` key. Use the dict form when different classes
deserve different timeout policy — e.g. `high_risk` should never
silently allow, while `reversible_write` is safe to auto-approve when
the operator is on vacation:

```yaml
escalation:
  auto_decide_after_seconds: 86400
  fallback_on_timeout:
    default: block                 # REQUIRED. Applied to any class not listed below.
    high_risk: block               # Explicit; same as default here. Documenting intent for ops review.
    reversible_write: allow        # Vacation-friendly for write actions that can be rolled back.
    external_side_effect: block    # Refuse outbound-effect actions on timeout.
```

The `default:` key is mandatory in the dict form — there is no implicit
fall-through. Operators who want every class to share a single policy
should use the legacy string shape (`fallback_on_timeout: block`).
Class keys must be one of `read_only | reversible_write |
external_side_effect | high_risk`; values must be one of `allow |
block`. (`revise` and `escalate` are judge-driven outcomes that
require a live judge to interpret — they have no meaning in the
auto-decide-when-no-judge-responded path, so the parser rejects them
at load time rather than silently coercing them to block at runtime.)
Any typo on either side fails LOUD at parse time with
`JudgePolicyInvalid` naming the offending key or value.

**Authoritative-via-frontmatter.** At auto-decide time the framework
resolves per-class policy from the PENDING file's frontmatter
`action_class` field — the classification recorded at write time — NOT
from the on-disk directory name. So an operator who hand-moves a
PENDING file into a typo'd or renamed class directory still gets the
correct timeout policy for the *real* classification of that action.
The on-disk `### Auto-decided by framework` block's `reason:` line
records the resolved class (e.g. `fallback_on_timeout=block (resolved
for action_class=high_risk)`) for audit clarity.

**Cascade-floor scope.** Per spec/28:408 the project's
`<project>/judges.md` is the non-relaxable floor for **`class_policy`**
— a delegate may strengthen, never relax. The same protection does
*not* extend to `escalation.fallback_on_timeout` today: the delegate's
parsed `escalation` config wins wholesale (even when the delegate
omits the `escalation:` section, the parser materializes the
spec/28 default config — the floor's escalation is NOT inherited).
If your project sets `high_risk: block` at the floor and you want
every delegate bound by it, duplicate the `escalation:` block at
each delegate's `judges.md`. Whether to close this gap by enforcing
strictness (as `class_policy` does) or by inheriting unset escalation
sections from the floor is tracked at
[#173](https://github.com/dep0we/atomic-agents-stack/issues/173).

### Validation

The `validation:` top-level field controls how amended `tool_arguments` get validated on REVISE before the framework executes the bound action.

```yaml
validation: strict   # "weakened" (default) | "strict". Default: weakened.
```

`weakened` (the default) matches pre-PR-5b behavior: tool registered + dict-shaped args + canonical `arguments_hash` recompute. A one-shot per-agent log warning fires the first time amendment validation runs, pointing at the upgrade path.

`strict` adds `jsonschema.validate(args, registered.input_schema)` after the weakened checks. Empty or missing schemas are no-ops (no constraint). Errors are surfaced with field-path detail so operators reading the audit trail know exactly which key failed.

**Install order.** The `[validation]` extra must be installed BEFORE setting `validation: strict` in `judges.md`. The parser probes `import jsonschema` at agent-load when strict is configured and fails LOUD with `JudgePolicyInvalid` if the package is not importable. Install:

```bash
pip install 'atomic-agents-stack[validation]'
# or for uv-managed projects:
uv sync --extra validation
```

Then flip the config:

```yaml
# judges.md
validation: strict

class_policy:
  external_side_effect: judge_required
  high_risk: escalate
```

**Why an explicit gate (and not transitive import availability).** Operators commonly pull `jsonschema` in via unrelated dependencies (FastAPI, openapi-core, jsonschema-rs adapters). If strict validation activated whenever `jsonschema` happened to be importable, those operators would see validation change behavior on a `pip install` of an unrelated library. The `validation: strict` opt-in is the explicit operator intent — the framework respects it and only it.

**Exception taxonomy under strict mode.** Different shapes of failure map to different exception types so `failure_policy` can route them differently:

| What broke                                  | Re-raised as                       |
|---------------------------------------------|------------------------------------|
| Amendment doesn't match the schema          | `JudgeAmendedProposalRejected`     |
| Tool's own schema is malformed (operator authoring bug, broken `$ref`) | `JudgePolicyInvalid`               |
| Runtime jsonschema API surprise             | `JudgeAmendedProposalRejected`     |

Configure `failure_policy[JudgePolicyInvalid]` (default `block`) to control what happens when a registered tool ships with a broken schema.

**Cascade-floor strictness.** A delegate's `judges.md` may strengthen `validation` (e.g., floor=`weakened`, delegate=`strict`) but cannot relax it. Relax attempts raise `JudgePolicyInvalid` at agent-load. A delegate that omits the field inherits the floor's value without tripping a false-positive relax violation.

**Reserved namespaces.** `validation: audit` (validate + JSONL warn without BLOCK; tracked at [#176](https://github.com/dep0we/atomic-agents-stack/issues/176)) and `validation: paranoid` ([#178](https://github.com/dep0we/atomic-agents-stack/issues/178)) are reserved but not yet implemented. The parser rejects both with "not yet implemented" messages pointing at the tracking issue, distinct from a generic operator-typo rejection.

**Migration aid.** Operators flipping `validation: strict` on a production agent may discover that amendments which previously passed weakened validation now BLOCK. Before the flip, audit your registered tools' `input_schema` values — the `check_tool_schemas_for_amendment_validation` doctor check (tracked at [#175](https://github.com/dep0we/atomic-agents-stack/issues/175)) will surface tools whose schemas are missing or trivially permissive.

### `judge_captures`, `read_audit_mode`

```yaml
judge_captures: false      # Route atomic_capture markers through the judge. Default false.
read_audit_mode: false     # Run the judge on read_only actions for audit (without blocking). Default false.
```

`judge_captures: true` is the recommended setting for vaults
synchronized across hosts (Obsidian Sync, iCloud, syncthing) — memory
writes become a cross-runtime surface in synced vaults. The framework
cannot reliably detect "is this vault synced?" so doesn't flip the
default automatically.

### `specialist_composition`

Specialist-axis composition is parsed-but-not-yet-enforced in PR 3a.
Operators may author the section now; PR 3b/4's ensemble dispatch
consumes it.

```yaml
specialist_composition:
  - authorization
  - privacy
  - reversibility
  - quality
  - security
```

Alternative shape — `axes:` key:

```yaml
specialist_composition:
  axes:
    - security
    - performance
```

---

## What the parser rejects

The parser fails LOUD with `JudgePolicyInvalid` and an actionable error
message for any of:

- Invalid YAML inside a fenced block.
- Top-level YAML value that isn't a mapping (e.g., a list).
- Unknown action class names in `class_policy` or nested `failure_policy`.
- Unknown exception names in `failure_policy`.
- Class-policy values outside `{bypass, allow_with_audit, judge_required, escalate}`.
- Failure-policy outcomes outside `{allow, block, revise, escalate}`.
- `escalation.fallback_on_timeout` outcomes outside `{allow, block}` (judge-driven `revise`/`escalate` have no meaning in the no-judge-responded path).
- `escalation.fallback_on_timeout` dict form missing the mandatory `default:` key.
- Non-integer or negative `timeout_ms`, `auto_decide_after_seconds`.
- Non-numeric or negative budget caps.
- File that isn't valid UTF-8.
- Delegate's class policy that relaxes a project floor.
- `validation` values outside `{weakened, strict}` (see [Validation](#validation)). `audit` and `paranoid` are reserved namespaces and produce distinct "not yet implemented" rejections pointing at their tracking issues; any other unknown value produces the generic "must be one of" rejection.
- `validation: strict` set in `judges.md` while the `[validation]` extra is not installed — the parser probes `import jsonschema` at agent-load time.
- Delegate's `validation` value that relaxes a project floor's `validation` (e.g., floor=`strict`, delegate=`weakened`).

The discipline: operator typos surface at agent-load, not at the first
side-effectful tool call. A `judges.md` that loads cleanly is a
`judges.md` whose contents the framework has fully understood.

---

## Cross-references

- [`docs/spec/28-judge-layer.md`](../spec/28-judge-layer.md) — full
  design rationale, ESCALATE state machine, `ActionProposal` /
  `JudgmentEvent` schemas, specialist-composition semantics, audit
  shape, failure-mode catalog.
- [`docs/spec/03-file-formats.md`](../spec/03-file-formats.md) — the
  `tools.md` schema; `## Tool classification` section parser
  (PR 2a).
- [`docs/spec/06-multi-agent-projects.md`](../spec/06-multi-agent-projects.md) —
  project-root cascade rules; where the project floor lives.
- [`atomic_agents/judges_md.py`](../../atomic_agents/judges_md.py) —
  the parser source. `JudgesConfig` dataclass is the parsed shape.
- [`CHANGELOG.md`](../../CHANGELOG.md) — the `#112` arc's per-PR
  release notes.

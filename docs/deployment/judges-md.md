# Configuring the judge layer (`judges.md`)

How to author `judges.md` to control the **judge layer** — atomic-agents'
pre-action validation surface that inspects every side-effectful tool
call before it runs, decides ALLOW / BLOCK / REVISE / ESCALATE, and
writes a judgment event to the audit trail.

This doc is the operator-facing reference. The full design rationale
lives in the canonical spec at [`docs/spec/28-judge-layer.md`](../spec/28-judge-layer.md);
this page is the *how-to-author-it* guide that pairs with the shipped
parser.

> **Status:** The judge layer ships in pieces across `#112` PR 1 → 4.
> PR 3a (this doc's subject) shipped the `judges.md` parser, cascade-aware
> project floor, and per-class policy short-circuits in dispatch. ESCALATE
> state-machine polling, full conformance suite, and the spec lock-in
> land in subsequent PRs. The fields documented here are the parser's
> contract as of PR 3a — adding `judges.md` to your agent today opts
> you into PolicyJudge (always-on rule engine) and, when an OpenAI key
> resolves, `LLMJudgeBackend` (the second-line LLM judge from PR 2b).

---

## When to use the judge layer

| You want…                                                                          | Use…             |
|------------------------------------------------------------------------------------|------------------|
| `tools.md` write paths to be advisory exactly as today (no judge invocation).      | **No `judges.md`.** The opt-in gate stays off. |
| Every side-effectful tool call to write an audit-only judgment event.              | `judges.md` with `class_policy: allow_with_audit` per class. |
| Every side-effectful tool call to be evaluated by a judge before execution.        | `judges.md` with `class_policy: judge_required` per class. |
| `delete_files`-class actions to pause for operator approval.                       | `judges.md` with `high_risk: escalate`. (ESCALATE wiring lands in PR 3b.) |
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
| `escalate`          | Always escalate to operator approval before execution. The judge runs (audit trail captures its opinion) but the action waits for operator sign-off. (ESCALATE polling ships in PR 3b — until then, ESCALATE self-maps to BLOCK with `escalate_pending_polling_unimplemented` to avoid orphan PENDING files.) | `high_risk` actions (`delete_files`, `force_push`, `production_deploy`) and anything else the operator wants eyes-on. |

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
| `JudgePolicyInvalid`            | `judges.md` or `tools.md` cannot be parsed at agent-load time.            |
| `JudgeBudgetExhausted`          | Judge cost cap hit (separate ledger from actor budget).                    |
| `JudgeProposalInvalid`          | Proposal missing required fields for its class.                            |
| `JudgeAmendedProposalRejected`  | REVISE's amended proposal failed re-validation.                            |

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

ESCALATE state-machine polling lands in PR 3b; PR 3a parses these
fields so PR 3b can consume them without re-parsing.

```yaml
escalation:
  destination: vault                    # "vault" | future: "linear" | "slack" | ...
  auto_decide_after_seconds: 86400      # Wait at most 24h for operator decision. None = wait indefinitely.
  fallback_on_timeout: block            # What to do when auto_decide_after expires. allow/block/revise/escalate.
```

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
- Non-integer or negative `timeout_ms`, `auto_decide_after_seconds`.
- Non-numeric or negative budget caps.
- File that isn't valid UTF-8.
- Delegate's class policy that relaxes a project floor.

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

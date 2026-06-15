# spec/29 — Mandates

**Status:** **LOCKED** at #124 PR 4 (locked 2026-05-17 against commit [`1f83824`](https://github.com/dep0we/atomic-agents-stack/commit/1f83824)).
**Origin:** [#124](https://github.com/dep0we/atomic-agents-stack/issues/124); RFC origin [#115](https://github.com/dep0we/atomic-agents-stack/issues/115); sharpened by /office-hours 2026-05-17 + /plan-eng-review 2026-05-17 before #124 PR 1 opened.
**Shipping plan across the #124 arc (all landed):** Pre-PR-1 spec amendments (#213), **PR 1** — `MandateBackend` Protocol scaffolding + `FilesystemMandateBackend` reference impl + `mandates.md` parser + parametrized conformance suite (#214), **PR 2** — wire `AtomicAgent.__init__` + per-runner kwargs + `ATOMIC_AGENTS_MANDATE_BACKEND` env var (#217), **PR 3a** — `MandateCheck` specialist + validation steps 1-6 + lifecycle dedup + `target_extractor` registry + `judges.md ## Mandates` parser (#219, prep #218), **PR 3b** — validation steps 7-9 + reservation pattern + crash recovery + post-action verification events (#221, prep #220; commit `15089f2` second-pass amendments), **PR 4** — spec LOCKED + CLAUDE.md status flip to "eight backend protocols shipped" — this PR.

> Pre-implementation design amendments (recorded 2026-05-17, pre-PR-1; preserved as historical record of how the spec reached its final shape):
> - `MandateBackend` Protocol added (§"Implementer contract for mandate backends" below) — the framework ships mandates via a Protocol seam from day 1, with `FilesystemMandateBackend` as the only reference impl in v1. Future SaaS / mobile / Slack-bot adapters slot in via `register_mandate_backend(...)` without forking core (per /office-hours Option 2 decision: build the seam upfront, don't retrofit later).
> - `target_extractor` is a named per-agent registry, NOT a `Callable` field on `ToolDefinition` (per /plan-eng-review finding — `Callable` fields cannot satisfy spec/25 MUST #4 Tier B round-trip).
> - State persistence is a `MandateBackend.read_state`/`write_state` Protocol contract (NOT a filesystem-path contract). State carries `schema_version: 1`.
> - Suspicious-rebind throttle (60s default) closes the source-hash-before-state edit window for prompt-injection-style threats.
> - `BLOCK` reason naming is forever-stable; PR/version identifiers do NOT leak into JSONL audit reasons.
> - `mandate_cap_exceeded_block` events carry `contributing_reservation_ids` + `reconcile_cli_hint` so operators see WHY they're blocked, not just THAT they are.
>
> Cross-links: spec/01 (anatomy — graduated autonomy framing), spec/03 (file formats), spec/05 (capture rules — operator-confirmation discipline), spec/06 (multi-agent projects — project-root resolution), spec/09 (cost-observability — cumulative budget), spec/17 (tools), spec/27 (doctor), spec/28 (judge layer — Authorization integration)
>
> Related backends: PolicyBackend (#89 — future mandate-template composition), LogBackend (#61 — mandate events), LLMBackend (#87)

## Overview

A **mandate** is a durable, operator-granted scoped authority record. It lives in an operator-managed markdown file (`mandates.md`), is referenced by side-effectful action proposals via `mandate_id`, and is validated by the judge layer at action time.

Where the judge layer's `Authorization` dataclass (per `spec/28`) captures *per-action* authorization (\"the operator told me, in this conversation, to do this thing\"), a mandate captures **durable, cross-run, revocable scoped authority** (\"the operator authorized this agent to handle procurement for Q2 2026 under these constraints\"). The two compose: an action proposal's `Authorization` can cite a mandate by ID, and the judge's `MandateCheck` specialist validates the cite against the mandate's live state.

This spec describes the shipped surface as of the #124 arc close. Post-lock follow-ups are tracked in issues #222–#229 (filed alongside PR 4).

## Why this exists

The judge layer (spec/28) introduced `Authorization(granted_by, scope, granted_at, expires_at)` as a sub-dataclass on `ActionProposal`. That shape works for the simple case — an operator-in-conversation authorization with a single action's scope. It breaks for the cases the framework is structurally moving toward:

1. **Authorization spans many runs.** \"Operator authorized procurement for Q2 2026\" should not need to be re-derived from conversation history on every action. Re-derivation invites drift (the conversation gets summarized, the authorization wording shifts) and silent failure (the conversation context falls out of the model's window).
2. **Authorization is revocable mid-period.** Without a durable record, revocation has no surface. The operator can't say \"that mandate I granted Tuesday is over\" — there's nothing for the framework to look at.
3. **Authorization carries cumulative constraints.** \"Spend up to $200/month across all subscriptions\" cannot live inside a single action's scope field. The constraint accumulates across actions; the system needs a place to write down the constraint and a place to count against it.
4. **Authorization needs an inventory.** \"Show me everything this agent is currently authorized to do, by whom, with what constraints, what's been used against this\" cannot be reconstructed from grep across conversation logs. Operators running agents at any meaningful scale need this answer in seconds.

A mandate is the durable record that handles all four. It is **not** a payment credential, not a tool registration, not an authorization claim — it is the operator's *written grant*, dated, scoped, constrained, revocable.

## Semantics

```
Operator                      Vault                       Agent           Judge
   │                            │                          │               │
   │── writes mandate to ──────▶│                          │               │
   │   mandates.md              │                          │               │
   │                            │── mandate_granted event ▶│               │
   │                            │   (JSONL audit)          │               │
   │                                                                       
   │                                                                       
                              Time passes; agent works against the mandate
                                                                           
   │                                                                       
   │                            │                          │── proposes ──▶│
   │                            │                          │   action with │
   │                            │                          │   Authorization
   │                            │                          │   (mandate_id=│
   │                            │                          │    \"procurement-q2\")
   │                            │                          │               │
   │                            │                          │               │── MandateCheck
   │                            │                          │               │   specialist
   │                            │◀─ reads mandates.md ─────┤               │   reads mandate
   │                            │                          │               │   validates:
   │                            │                          │               │   - exists?
   │                            │                          │               │   - active?
   │                            │                          │               │   - scope match?
   │                            │                          │               │   - constraint OK?
   │                            │                          │               │   - cumulative
   │                            │                          │               │     budget OK?
   │                            │                          │               │
   │                            │                          │◀── ALLOW ────│
   │                            │                          │── executes ──▶│ (tool)
   │                            │── mandate_used event ───▶│               │
   │                            │   (JSONL audit, with     │               │
   │                            │    proposal_id +         │               │
   │                            │    cumulative_after)     │               │
   │                                                                       
   │                                                                       
                              Time passes; operator decides to revoke
                                                                           
   │── edits mandates.md ──────▶│                          │               │
   │   sets revocation_state:   │                          │               │
   │   revoked                  │                          │               │
   │                            │── mandate_revoked event ▶│               │
   │                            │                          │               │
   │                            │                          │── proposes ──▶│
   │                            │                          │   another action
   │                            │                          │   citing same │
   │                            │                          │   mandate_id  │
   │                            │                          │               │
   │                            │                          │◀── BLOCK ────│ (mandate_revoked)
```

## The `Mandate` dataclass

```python
class RevocationState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"          # auto-set when past expires_at


@dataclass(frozen=True)
class MandateConstraints:
    # Token cost budgets — LLM invocation cost incurred by actions citing this mandate
    daily_token_usd: float | None = None
    monthly_token_usd: float | None = None
    cumulative_token_usd: float | None = None       # lifetime token-cost cap

    # External cost budgets — real-money cost reported by tool handlers
    # (Stripe charges, vendor invoices, API fees outside the LLM call, etc.)
    # Tool handlers report this via ToolCallResult.external_cost_usd
    # (a new field added by the impl PR — see "ToolCallResult schema extension"
    # below); tools that don't report default to $0.
    daily_external_usd: float | None = None
    monthly_external_usd: float | None = None
    cumulative_external_usd: float | None = None    # lifetime real-money cap

    # Tool + target allowlists
    allowed_tools: list[str] | None = None
    allowed_targets: list[TargetPattern] | None = None
    blocked_targets: list[TargetPattern] | None = None

    # Escalation thresholds
    requires_escalation_above_token_usd: float | None = None
    requires_escalation_above_external_usd: float | None = None

    # Time-of-day enforcement
    time_window: TimeWindow | None = None

    # Scope-only opt-out (rare; trust the prose; requires justification)
    # When True, MandateCheck skips structured constraint checks and ALLOWs
    # any cite to the mandate. The check_mandate_unconstrained doctor check
    # surfaces these to the operator. justification is REQUIRED whenever
    # unconstrained: True.
    unconstrained: bool = False
    unconstrained_justification: str | None = None

    # Operator-extensible (custom constraints; framework ignores; specialist
    # judges or operator tooling may consume)
    extra: dict[str, Any] = field(default_factory=dict)
```

### `ToolCallResult` schema extension (impl PR)

The current framework's `ToolCallResult` (per `atomic_agents/tools.py`) is structured around `output` + `error` only. The mandate primitive extends it with optional external-cost reporting fields the implementation PR adds:

```python
@dataclass
class ToolCallResult:
    # existing fields preserved
    output: Any
    error: str | None
    # new fields (defaulted; backward-compatible)
    external_cost_usd: float | None = None
    external_cost_currency: str = "USD"
    external_cost_reversibility: str | None = None    # informational
```

Tool handlers that don't populate the new fields continue to work unchanged. Operators who want mandate external-cost tracking add the fields to their tool handlers' returns. The implementation PR documents the migration path for existing tool handlers in the impl-PR runbook.


@dataclass(frozen=True)
class TargetPattern:
    """Identifies an action target — a vendor, an external service, a URL, an
    MCP tool, etc. The framework matches proposals against patterns at
    judge-time. Pattern resolution is by kind."""
    kind: str                    # "vendor" | "url_glob" | "mcp_server" | "tool_name" | "email_domain" | ...
    value: str                   # the pattern; meaning depends on kind


@dataclass(frozen=True)
class TimeWindow:
    days_of_week: list[str] | None = None   # ["mon","tue","wed","thu","fri"]
    hours_local: tuple[int, int] | None = None  # (9, 17) for 9am-5pm
    timezone: str = "UTC"


@dataclass(frozen=True)
class Mandate:
    id: str                                  # unique within the agent / project scope
    granted_by: str                          # "operator" | "operator:<name>" | "policy:<source>"
    granted_at: str                          # ISO-8601
    expires_at: str | None                   # ISO-8601; None = no expiry (discouraged)
    revocable_by: str                        # who can revoke; usually "operator"
    scope: str                               # plain-language description
    constraints: MandateConstraints
    revocation_state: RevocationState
    revoked_at: str | None                   # ISO-8601; set when revocation_state != ACTIVE
    revocation_reason: str | None            # operator-supplied
    source_file: Path                        # where this mandate was loaded from
    source_hash: str                         # sha256 of the mandate's frontmatter+body at load time
```

`source_hash` lets the framework detect mid-flight mandate edits between load and use — a mandate whose hash has changed since the cite was bound triggers re-validation. This mirrors the proposal-binding TOCTOU defense from spec/28.

### Token cost vs external cost — two ledgers, deliberately separate

The motivating examples (SaaS subscriptions, procurement) cap **real-world money** — the dollars the operator actually loses when the agent acts. The framework's existing cost ledger (`spec/09`) tracks **LLM invocation cost** — pennies of token spend per call. These are different magnitudes for different concerns; conflating them was the load-bearing flaw round-1 review caught.

Mandate constraints therefore split into two budget categories:

- **Token cost budgets** (`*_token_usd`) cap the LLM cost incurred *by* actions citing the mandate. Source: the existing `spec/09` cost ledger. Caps the cost of *reasoning* about an action.
- **External cost budgets** (`*_external_usd`) cap the real-money cost of the action itself. Source: a new `ToolResult.external_cost_usd` field that tool handlers populate (default $0 for handlers that don't report). Caps the cost of *taking* an action.

A `stripe.subscribe` action might cost $0.0007 in token cost and $200.00 in external cost; the operator's mandate caps the $200 against `cumulative_external_usd`, and the $0.0007 against `cumulative_token_usd` only if a token budget is also configured.

Tool handlers populate external cost via the existing tool-result return convention:

```python
return ToolResult(
    output="Subscription created: sub_abc123",
    external_cost_usd=200.00,         # the actual money the tool moved
    external_cost_currency="USD",     # for future multi-currency support
    external_cost_reversibility="refundable_within_30d",  # informational
)
```

Tools that don't return external cost (read-only tools, internal vault writes, MCP tools without cost metadata) report nothing — the cost event records `external_cost_usd: 0` and the external ledger isn't touched.

This split has knock-on effects: the doctor surfaces token vs external spend separately; dashboard mandate views show both columns; alerting thresholds can fire on either independently.

## `mandates.md` file format

Operator-managed markdown. The framework reads it; the framework does **not** write to it (mandates are operator-granted by definition; agent-self-granting violates the entire authorization-record discipline). The framework does write to `~/.atomic-agents/.judge-state/mandates.json` — a framework-internal state file (mirroring the `.lock` pattern from `spec/01`) that tracks per-mandate-ID `last_known_state` for lifecycle event deduplication. Operators do not edit this file directly.

```markdown
# Mandates — Caldwell

## procurement-q2-2026
granted_by: operator
granted_at: 2026-04-01
expires_at: 2026-06-30
revocable_by: operator
scope: |
  Purchase SaaS subscriptions on the approved-vendor list.
  Individual subscriptions ≤ $200/month.
  Quarterly review with the operator before renewal commitments.
constraints:
  # External cost (the real money the agent will spend)
  daily_external_usd: 200
  monthly_external_usd: 2000
  cumulative_external_usd: 6000
  requires_escalation_above_external_usd: 500
  # Token cost (LLM reasoning cost about these actions; optional but recommended)
  monthly_token_usd: 10
  # Tool + target allowlists (REQUIRED — see "Constraint enforceability" below)
  allowed_tools:
    - stripe.subscribe
    - vendor_lookup
    - cancel_subscription
  allowed_targets:
    - kind: vendor
      value: notion.so
    - kind: vendor
      value: figma.com
    - kind: vendor
      value: 1password.com
revocation_state: active
revoked_at: null
revocation_reason: null

## emergency-deploy-2026-05-09
granted_by: operator:dan
granted_at: 2026-05-09T22:14:00Z
expires_at: 2026-05-10T06:00:00Z
revocable_by: operator
scope: |
  Emergency deploy authorization for the auth-bug-fix branch.
  One deploy to production allowed.
constraints:
  allowed_tools:
    - deploy.production
  # Single-use mandate enforced via single-allowed-tool + no monetary budget
  # (the tool itself enforces "production")
revocation_state: active
revoked_at: null
revocation_reason: null
```

### Constraint enforceability (refused at load time)

`scope` is prose. The framework does **not** semantically interpret prose. Operators sometimes write detailed scope text and assume the framework enforces it; the framework does not, and silently letting under-constrained mandates load creates a false sense of authorization-coverage.

A mandate is therefore refused at load time (`MandateInvalid`) unless **at least one** of the following structured-enforcement fields is present in `constraints`:

- `allowed_tools` (non-empty list)
- `allowed_targets` (non-empty list)
- Any of the budget cap fields (`*_token_usd`, `*_external_usd`)
- Any of the escalation threshold fields (`requires_escalation_above_*`)
- `time_window`

Operators who genuinely want a scope-only mandate (rare; trust-the-prose mode) must explicitly opt in via `constraints.unconstrained: true` plus a documented `unconstrained_justification: "<reason>"`. The doctor (`check_mandate_unconstrained`) surfaces these. They are not refused, but they are visible.

This makes the *honest* enforcement story unmissable: scope is documentation; constraints are enforcement; mandates without enforcement are visible.

### Parser rules

- **Reserved section name `_meta`**: a section heading `## _meta` is parsed as the file's metadata block, not as a mandate. Only project-root `mandates.md` honors `_meta` (defines `per_agent_mandate_policy` and `allowed_per_agent_ids` per the resolution section below). Per-agent `mandates.md` files with a `## _meta` section: the section is ignored with a doctor warning (`check_mandate_meta_misplaced`). The `_meta` reservation is processed BEFORE mandate-section parsing so the ID-validity rule (`[a-z0-9][a-z0-9-]*`) doesn't refuse `_meta` as a malformed mandate.
- Each `## <mandate-id>` section other than `_meta` is one mandate. `mandate-id` is the dataclass `Mandate.id`.
- Section body is parsed as YAML (matching the embedded-YAML convention from `model.md` and `judges.md`).
- **Required fields**: `granted_by`, `granted_at`, `scope`, `revocation_state`. Missing required field → `MandateInvalid` at load time (the framework refuses to honor any cite against the invalid mandate; the doctor surfaces the file error).
- **Recommended fields**: `expires_at`. A mandate without `expires_at` is honored but the doctor emits `check_mandate_no_expiry` warnings; long-lived mandates without expiry are an accumulating attack surface.
- **Constraint requirement**: a mandate must declare at least one structured enforcement field per the "Constraint enforceability" rule above. Mandates without structured constraints (and without `constraints.unconstrained: true`) → `MandateInvalid` at load time.
- **Reserved values**: `revocation_state` is `active` | `revoked`. The framework computes `expired` as derived state from `expires_at` vs. current time; operators do not write `revocation_state: expired` directly. Unknown values → `MandateInvalid`.
- Mandate IDs must be unique within the file. Duplicates → `MandateInvalid` for all duplicate entries (operator is alerted; no entry honored). Mandate IDs **may** repeat across files at different scope levels (per-agent vs project-root) — resolution rules apply (see below).
- Mandate IDs follow the pattern `[a-z0-9][a-z0-9-]*` (lowercase + digits + hyphens, starts with alphanumeric), **max 64 characters**. Other characters or longer IDs → `MandateInvalid`. This bounds operator footguns when mandate IDs flow into JSONL events, source filters, CLI output, and audit paths.
- Pure-YAML config files are refused per rule #7. Embedded YAML inside markdown is acceptable for structured fields.

### Where the file lives

- **Per-agent**: `<agent>/mandates.md` (mandates that apply only to that agent)
- **Project-root**: `<project>/mandates.md` (mandates that apply to all agents in a multi-agent project per spec/06)

Both may exist. Resolution rules below.

## Authorization integration (`spec/28`)

The judge layer's `Authorization` dataclass gains a `mandate_id` field. The shape is backward-compatible — existing proposals with `granted_by: "operator"` and no `mandate_id` continue to work.

```python
@dataclass(frozen=True)
class Authorization:
    granted_by: str                          # "operator" | "policy" | "delegated_from:<agent>" |
                                             # "mandate:<id>"  (NEW value)
    scope: str
    granted_at: str
    expires_at: str | None
    mandate_id: str | None = None            # NEW; appended at end with default for backward
                                             # compatibility with spec/28's positional shape.
                                             # Present when granted_by starts with "mandate:".
```

The `mandate_id` field is appended at the end with a default of `None`. This preserves spec/28's positional construction order for existing call sites — code that builds `Authorization` without naming arguments continues to work, and JSON/YAML deserialization that ignores unknown fields continues to work. The expanded `granted_by` value (the literal `"mandate:<id>"` form) is a new shape that the spec/28 `AuthorizationJudge` specialist must learn to recognize; existing `granted_by` values (`"operator"`, `"policy"`, `"delegated_from:<agent>"`) continue to validate unchanged.

When `granted_by = "mandate:<id>"`:

- `mandate_id` MUST equal the `<id>` portion of `granted_by` (framework validates; mismatch → `JudgeProposalInvalid`).
- `scope` SHOULD be a direct copy of the cited mandate's scope field. The judge does not enforce string equality (operators may paraphrase) but flags significant deviation as a privacy/quality concern (specialist LLM judge surfaces).
- `granted_at` and `expires_at` SHOULD reflect the mandate's values (the actor cannot grant itself a later expiry than the mandate's actual `expires_at`).

### Mandate cites cannot be repaired by `REVISE`

Per spec/28, `ProposalAmendment` does not include `reason` or `authorization` — the judge cannot rewrite the actor's stated reason or authorization claim. This applies to mandate cites: a mis-cited or missing mandate cannot be amended into a valid cite by the judge. The path on a bad cite is `BLOCK` with reason `mandate_*` (per the validation list below) and a reason field that names what the actor should do (re-author the cite, request a different mandate from the operator, escalate the action class). The actor's next turn carries the block reason and may try again with a corrected authorization. This is a deliberate limit on judge authority — only operators grant authorization; judges only validate cites.

## `MandateCheck` judge specialist

A new built-in rule-engine specialist in the judge layer's composition (per spec/28's specialist judges section). Runs early — before LLM specialists — for fail-fast performance.

```python
class MandateCheck:
    """Rule-engine judge specialist. Runs when proposal.authorization.granted_by
    starts with 'mandate:'. Validates the cite against live mandates.md state."""

    def evaluate(self, proposal: ActionProposal, context: JudgmentContext) -> Judgment:
        ...
```

### Validation steps (in order)

1. **Existence**: `mandate_id` resolves to a mandate in the agent's effective mandate set (per resolution rules below). Not found → `BLOCK` with reason `mandate_not_found`.
2. **Source hash**: re-read `mandates.md`, recompute `source_hash` of the cited mandate's section, compare against the value bound at proposal time. Mismatch indicates the mandate file changed between proposal and judgment — `BLOCK` with reason `mandate_state_inconsistent`, with the reason field naming "re-author the proposal against the current mandate state." The framework re-reads first (rather than checking state first) because state-from-stale-bytes is misleading; if the operator just revoked the mandate, the hash check catches it before the state check would.
3. **State**: mandate's `revocation_state == ACTIVE` and current time < `expires_at` (if `expires_at` set). Revoked → `BLOCK` with reason `mandate_revoked`. Expired → `BLOCK` with reason `mandate_expired`.
4. **Tool allowlist**: if `constraints.allowed_tools` is set, `proposal.tool_name` MUST be in it. Otherwise → `BLOCK` with reason `mandate_tool_not_allowed`.
5. **Target allowlist**: if `constraints.allowed_targets` is set, the proposal's `target_canonical` (extracted by the framework per tool kind — see Target extraction below) MUST match at least one pattern. No match → `BLOCK` with reason `mandate_target_not_allowed`. If `constraints.blocked_targets` is set, the proposal's `target_canonical` MUST NOT match any pattern. Match → `BLOCK` with reason `mandate_target_blocked`. If target extraction fails (no kind-handler for the proposal's tool, no target in tool args), the action depends on `judges.md`'s `## Mandates / unextractable_target_action`:
   - `block` (default): `BLOCK` with reason `mandate_target_unextractable`
   - `escalate`: `ESCALATE` with reason `mandate_target_unextractable`

   Operators may configure either at the `judges.md` level. The default is fail-closed (block).
6. **Time window**: if `constraints.time_window` is set, current time MUST fall within it. Outside → `BLOCK` with reason `mandate_outside_time_window` (or `ESCALATE` if the action class is `high_risk`).
7. **Token budget**: if any `*_token_usd` cap is set, framework sums prior `actor`-source cost events tagged with this `mandate_id` (see Cost integration) and projects the *upcoming turn's* token cost.

   **Token-cost projection for the upcoming turn**: token cost in spec/09 is logged per LLM turn (`agent.call()` iteration), not per tool call. A single turn can emit multiple tool calls, potentially citing different mandates. The framework projects the upcoming turn's token cost by:
   - Reading the actor's **immediately-preceding iteration's** token cost — specifically, the cost event matching `LogQuery(run_id=current_run_id, cost_source="actor", limit=1)` sorted by `ts` descending. The query MUST be scoped to "preceding iteration of the SAME `agent.call()` invocation" (per plan-subagent PR 3b Risk F — the prior wording "most recent `actor`-source cost event for this run_id" was ambiguous between "preceding iteration" and "any earlier iteration" and would drift on multi-iteration runs as conversation context grows). For the first iteration (no prior cost event in this run), the framework uses the `model.md` `expected_cost_per_call_usd` field if set, else a conservative default (`$0.10`).
   - Apportioning the projected turn cost across the tool calls in the turn proportional to each call's argument-token count. Each mandate's projection is the apportioned share, not the full turn cost. Apportioning to N tool calls means each tool call's projection is approximately `turn_cost / N` adjusted by relative argument-token count.

   This is approximate — the actual token cost is known only after the turn completes. The reservation pattern reserves the projection; the `_committed` event corrects to the actual when the cost event lands. Token-budget mandates are therefore *eventually accurate* per turn, not pre-turn exact. **Cap over-shoot is bounded to one action per cap per turn**: when projection under-reserves and actual exceeds cap, the FIRST action of the turn passes (silent over-spend by the projection delta); the cumulative-spend updates land before the SECOND action's MandateCheck, which observes the true cumulative and BLOCKs as designed (per plan-subagent PR 3b Risk F — normative test requirement: `test_cap_overshoot_bounded_to_one_action_per_turn`). For mandates where token-cost precision matters more than approval throughput, operators omit token budgets and rely on the actor's overall cost guardrail instead.

   Exceeds cap (lifetime / daily / monthly) → see budget-breach action below.
8. **External budget**: if any `*_external_usd` cap is set, framework sums prior tool-reported external cost tagged with this `mandate_id` and projects the action's external cost from a **framework-owned source** — the tool definition's `expected_external_cost_usd` field (a static estimate operators register at tool-declaration time) or, for tools that compute cost dynamically, a **per-agent named cost-estimator registry** referenced by `ToolDefinition.cost_estimator_id: str | None` (same shape as `target_extractor_id` from §"Target extraction" — `Callable` fields on `ToolDefinition` cannot satisfy spec/25 MUST #4 Tier B round-trip; the named-registry pattern stores a string ID that backends round-trip losslessly while the agent's `CostEstimatorRegistry` holds the actual callable). The framework calls the registered estimator with the proposal's `tool_arguments` to get a projection. **The actor never supplies the projected external cost.** When a mandate's `allowed_tools` includes a tool with neither `expected_external_cost_usd` nor a registered `cost_estimator_id`, the framework treats projected external cost as `+∞` and `MandateCheck` returns `BLOCK` with reason `mandate_external_cost_unprojectable`. Fail-closed by design — the same discipline as target extraction. Operators add a static estimate to the tool definition OR register a named estimator on the agent (`agent.register_cost_estimator(name, callable)`) and reference it from `ToolDefinition.cost_estimator_id` to unblock; alternatively, they remove `*_external_usd` caps from the mandate (operator choice). Same registration-order discipline as target extractors per §"Registration order discipline": built-in estimators (none ship by default for cost; the registry starts empty) registered BEFORE tool_registry loading; unknown `cost_estimator_id` fails loud at `tool_registry.register()` time with `UnknownCostEstimator`. Exceeds cap → see budget-breach action below.
9. **Escalation thresholds**: if `constraints.requires_escalation_above_token_usd` or `requires_escalation_above_external_usd` is set and the corresponding projected cost exceeds it → `ESCALATE` with reason `mandate_escalation_threshold_hit_<token|external>`.

**Step 8 / Step 9 precedence when both fire (plan-subagent PR 3b Risk 7 — second-pass amendment):** if the projected cost both exceeds the cap (step 8 would BLOCK or ESCALATE) AND exceeds the escalation threshold (step 9 ESCALATE), **step 9 ESCALATE wins** — operator-decision-required is the more nuanced surface than hard BLOCK. The framework evaluates step 9 BEFORE returning step 8's verdict; step 9's ESCALATE preempts step 8's BLOCK for any action class. Test invariant: `test_step9_escalate_preempts_step8_block_when_both_fire`. (Rationale: an action that is over-budget AND over-escalation-threshold may be a legitimate one-time exception the operator can approve; forcing BLOCK pushes the actor to re-architect when ESCALATE invites operator judgment.)

**Budget-breach action (steps 7 + 8)**: behavior depends on action class —
- `high_risk` → `ESCALATE` with reason `mandate_cap_would_exceed_high_risk`
- `external_side_effect` → `BLOCK` with reason `mandate_cap_would_exceed`
- `reversible_write` → `BLOCK` with reason `mandate_cap_would_exceed`
- `read_only` → not applicable (read_only actions don't cost meaningful external money; token cost is gated by the actor budget, not the mandate budget)

If all checks pass: `ALLOW`. The judgment event's `binding` carries the `mandate_source_hash` so execution-time re-binding can re-verify if needed.

`MandateCheck` is **always-on** when a proposal cites a mandate. It runs before the LLM catch-all judge (per spec/28's `[PolicyJudge, LLMCatchAll]` default composition pattern; `MandateCheck` is a sibling of `PolicyJudge`, both rule-engine, both always-on, both fail-fast). Composition becomes effectively `[PolicyJudge, MandateCheck, LLMCatchAll]` when mandate cites are involved.

### Suspicious-rebind throttle (closes the source-hash-before-state edit window)

The source-hash-before-state ordering (step 2 before step 3) creates a small attack-surface window: an actor seeing `mandate_state_inconsistent` (hash mismatch) re-authors with a fresh binding against the operator's mid-edit `mandates.md`. If the operator typed the new hash but hasn't yet flipped `revocation_state: revoked` (multi-line edit, not atomic), the actor could bind to a still-active state and bypass the revocation by ~seconds. This is exploitable by a malicious or hallucinating actor on a tight retry loop.

**Defense:** when `MandateCheck` surfaces `mandate_state_inconsistent` for a mandate, the framework refuses re-binding on the same mandate for `suspicious_rebind_throttle_s` seconds (default 60). The throttle is per-(mandate_id, agent_run_id) — a different agent run citing the same mandate is not throttled (closes the window without serializing legitimate cross-run usage).

Throttle config lives in `judges.md`:
```markdown
## Mandates
suspicious_rebind_throttle_s: 60       # default 60; lower to 10 for tight-loop testing
```

When the throttle is active and the actor re-cites the same mandate, `MandateCheck` returns `BLOCK` with reason `mandate_rebind_suspicious_throttled`. A new lifecycle event `mandate_rebind_suspicious_throttled` is emitted (carries `mandate_id`, `agent_run_id`, `original_state_inconsistent_at`, `throttle_expires_at`). The doctor's `check_mandate_rebind_throttle_active` surfaces active throttles for operator awareness — an operator-edited revoke that lands cleanly clears the throttle on next agent run; an operator who sees sustained throttles knows their actor is exhibiting suspicious retry behavior.

**Persistence (load-bearing security property):** throttle state MUST live in the same `MandateBackend.read_state(scope) / write_state(scope, state)` shape as the lifecycle dedup state — under the top-level `"throttles"` key in the state shape. **In-memory-only throttle persistence is forbidden** because a crash-restart loop (which a malicious actor can induce by exhausting a tool's budget or triggering a Python exception) would bypass the throttle, defeating the prompt-injection defense this section was designed to close. Spec/29 PR 3a plan-subagent pressure-test identified this as a SEVERE risk.

The state shape extends per §"Lifecycle event deduplication" below:

```json
{
  "schema_version": 1,
  "scope": "agent:<name>",
  "mandates": { ... },
  "throttles": {
    "<mandate_id>": {
      "agent_run_id": "...",
      "expires_at_iso": "2026-05-17T16:32:00Z",
      "original_state_inconsistent_at": "2026-05-17T16:31:00Z"
    }
  }
}
```

State carries `schema_version: 1`. `throttles` is always present (empty `{}` when no throttle active) and `"reservation_orphans"` is a separate key under the same `schema_version: 1` (backward-compatible additive). Schema bump to 2 only if a field type changes. Operator-edited revoke that lands cleanly removes the corresponding entry from `throttles` on next state read (transition-only dedup logic).

### BLOCK reason naming discipline (general)

All `BLOCK` reasons emitted by `MandateCheck` (or any downstream MandateBackend) MUST be forever-stable strings — they land in JSONL audit logs that operators read indefinitely. Reasons MUST NOT carry version identifiers (`_in_3a`, `_v2`, `_phase_b`), PR identifiers, or transient context. The temporary-cause story belongs in the CHANGELOG entry of the release that introduces the reason, not in the reason itself.

Reasons SHOULD be greppable: dot-or-underscore-separated tokens with one canonical form (`mandate_target_unextractable`, NOT `MandateTargetUnextractable` or `mandate.target.unextractable`). Existing reasons in this spec follow this convention.

### Target extraction (framework-owned, not actor-supplied)

The framework extracts a `target_canonical` value from the proposal at assembly time. This is a **framework-owned** field — the actor does not supply it, and the actor-supplied `target_audience` field from spec/28 (which is a *privacy* surface, not a binding target) is never used as the basis for mandate target matching. The two fields exist for different concerns:

- `target_audience`: actor-supplied privacy surface (`internal` | `external:<surface>`). The privacy judge consumes this. The mandate layer does not.
- `target_canonical`: framework-extracted binding target. The mandate layer consumes this. The actor cannot influence it.

The framework extracts `target_canonical` via a **per-agent named extractor registry** (NOT a `Callable` field on `ToolDefinition` — that shape was considered and rejected at the /plan-eng-review stage because `Callable` fields cannot satisfy spec/25 MUST #4 Tier B's lossless round-trip obligation for structured-storage tool backends). The registry pattern mirrors the established spec/25 Decision 9 + spec/15 delegate-isolation precedent: each agent owns its registry; coordinator-registered extractors do NOT leak into delegate evaluations.

`ToolDefinition` carries an optional `target_extractor_id: str | None` field that names an extractor registered on the agent's registry. The agent setup registers the named callable; `MandateCheck` resolves the name at evaluation time. Backend-storable (the string ID round-trips through Tier B losslessly per spec/25 MUST #4).

```python
# Agent setup — register per-agent
agent.register_target_extractor("recipient_to", lambda args: args.get("to"))

# Tool definition references the extractor by name (string, not callable)
ToolDefinition(
    name="send_email",
    input_schema={...},
    handler=send_email_handler,
    target_extractor_id="recipient_to",                # string ID, not Callable
    action_class=ActionClass.EXTERNAL_SIDE_EFFECT,
)
```

Built-in heuristic extractors are pre-registered on every agent's registry at construction time (so operators don't need to register them manually): `recipient_to` (extracts `to` field), `recipient_field` (extracts `recipient`), `target_field` (extracts `target`), `url_field` (extracts `url`), `repository_field` (extracts `repository`), `customer_id_field`, `channel_id_field`. MCP tools' extracted values are prefixed with `mcp:<server>:` by the framework before pattern matching.

If extraction returns `None` and the mandate's `constraints.allowed_targets` is set → `BLOCK` with reason `mandate_target_unextractable`. The fallback is intentionally fail-closed: a mandate that says "only send to these vendors" + a tool the framework can't extract a target from = the framework refuses. Operators who want a tool to be mandate-target-aware must register an extractor name on the tool definition (small impl cost) or omit `allowed_targets` from the mandate (operator decision).

**Registry collision discipline**: `agent.register_target_extractor(name, callable)` raises `ValueError` on collision (same `name` already registered). Mirrors `register_tool_registry_backend` precedent — no silent overwrite. Operators replace via explicit `agent.replace_target_extractor(name, callable)`.

**Delegate isolation**: per spec/15, a delegate loads a fresh target vault. The target_extractor registry is a per-agent surface owned by `AtomicAgent` (in-memory `self._target_extractors: dict[str, Callable[[dict], str | None]]`) — NOT stored on the `AgentProfile` snapshot (which is JSON-serializable per spec/24 and cannot store `Callable` values). "Per-agent" means *scoped per agent instance*, not *persisted on the agent profile snapshot*. The delegate registers its own extractors (or inherits the project-root defaults at agent construction); coordinator-local extractors do NOT flow to the delegate. This matches the tool-permission-non-inheritance discipline from spec/15 and the per-agent backend-scoping pattern from spec/25 Decision 9.

**Registration order discipline:** the framework MUST register built-in heuristic extractors BEFORE `tool_registry` loading begins in `AtomicAgent.__init__`. Tools whose descriptors reference `target_extractor_id` strings via `ToolDefinition.target_extractor_id` are validated against the registry at `tool_registry.register()` time (loud early failure with `UnknownTargetExtractor` exception). Validating at MandateCheck evaluation time would be a silent fail-closed surface where operators discover at first mandate-citing action that a tool's extractor is missing — too late to be useful. Plan-subagent PR 3a Risk A.

The extracted `target_canonical` is recorded in the proposal at assembly time (per the ActionProposal field below) and surfaces in the JSONL judgment event under `mandate_cite.target_canonical`, so post-hoc audit can verify the target the framework *actually saw* matches the target the operator intended to constrain.

### `ActionProposal` field extension (spec/28 RFC extension)

This spec extends spec/28's `ActionProposal` dataclass with one new optional field, presented as an RFC extension (spec/28 is locked-after-merge per `spec/28` RFC convention; spec/29's extension is acknowledged here and will be reflected in the spec/28 source-of-truth once both specs are jointly re-locked by the implementation PR):

```python
# spec/28 ActionProposal gains:
target_canonical: str | None = None    # framework-extracted at assembly time
                                       # nullable; not all tools have extractable targets
                                       # not part of the proposal_binding hash
                                       # (the framework re-extracts at execution time;
                                       # the proposal records what was seen)
```

This is a clean additive field with a `None` default, backward-compatible for the existing spec/28 positional and keyword shapes. `ProposalAmendment` (the judge-amendable subset per spec/28 §"Revise") does NOT include `target_canonical` — the field is framework-owned end-to-end.

## Per-agent vs project-root resolution

A multi-agent project (spec/06) may have mandates at two scopes:

1. **Project-root**: `<project>/mandates.md`. Mandates here apply to **all agents** in the project. They form an authorization floor — every agent's effective mandate set includes the project-root entries.
2. **Per-agent**: `<agent>/mandates.md`. Mandates here apply only to that agent. They form an authorization extension — the agent has these mandates *in addition to* the project-root ones.

### Resolution rules

The resolution model is **disjoint IDs, not merged constraints**. Project-root and per-agent files describe different mandates with different IDs; they do not merge constraints on a same-ID mandate. This is intentionally simpler than spec/28's judge-policy floor (which merges constraints under a strict-wins rule) — mandates are individual authorization records, not policy gradients.

- An agent's effective mandate set is `project_root_mandates ∪ agent_local_mandates`, where mandates are keyed by ID.
- **ID collision**: if both files declare a mandate with the same ID, the per-agent entry is **refused at load time** (`MandateInvalid`). The doctor emits `check_mandate_id_collisions`. Project-root entry continues to apply. This is stricter than the previous draft's "project-root wins" semantics; refusing the per-agent entry is honest about the conflict rather than silently honoring one and dropping the other.
- **Per-agent additions** with IDs not present in project-root are honored as-is. The operator-as-grantor model means the operator writes both files; per-agent additions are legitimate extensions of authorization that the operator has authored.
- **Project-root `_meta` section** (NEW): the project-root file may declare a `## _meta` section with `per_agent_mandate_policy: open | listed | forbidden`:
    - `open` (default): per-agent files may add any mandate with a new ID
    - `listed`: per-agent files may add only mandates whose IDs appear in `_meta.allowed_per_agent_ids: [...]`; non-listed → `MandateInvalid`
    - `forbidden`: per-agent files cannot add any mandates; any per-agent entry → `MandateInvalid`. Use this when the project operator wants to centralize all authorization at the root.
- An agent that operates outside any multi-agent project simply uses its own `<agent>/mandates.md` (or has none).

### Why this shape

Project-root mandates let a coordinator agent (per spec/15) carry authorization that delegates legitimately inherit. A delegate's MandateCheck reads the project-root file in addition to its own `<delegate>/mandates.md` and validates the cite identically. This closes the same hole that judge-policy floors close in spec/28 — without a project-root authorization layer, a delegate without `mandates.md` could not legitimately cite a coordinator's authorization.

### Delegation specifics (`spec/15`)

Per spec/15, delegation is one-level (coordinator → delegate; delegate does not further delegate) and the delegate loads a *fresh* target vault. The mandate layer follows the same boundary:

- A delegate's MandateCheck reads both `<project>/mandates.md` (the project-root file) and `<delegate>/mandates.md`. The project-root file is the cross-agent authorization layer.
- Coordinator-*local* mandates (entries in `<coordinator>/mandates.md` but not `<project>/mandates.md`) do **not** flow to the delegate. If the coordinator wants the delegate to be able to cite a mandate, the operator places it in the project-root file. This is the same discipline tool-permission non-inheritance follows in spec/15.
- The delegate's MandateCheck logs `mandate_used` events into the delegate's own JSONL audit log. **Budget computation rule** (resolves the per-agent vs project-level question consistently): cumulative budget for a *project-root* mandate is computed by aggregating `mandate_used` + outstanding `mandate_reservation` events across **all agents in the project**, by reading each agent's JSONL log. The aggregation is **eventually consistent** — at any instant, an agent's read of the cross-agent sum may lag another agent's just-written event. Cumulative budget for an *agent-local* mandate is computed from that agent's own log only, no aggregation.
- **Cross-agent reservation race** (known limitation): the TTL reservation pattern provides atomicity *within* a single agent's process. Across agents that share a project-root mandate, two agents can race past a budget cap because each agent's reservation isn't visible to the other in real time. Mitigations available to operators:
    - **Set `requires_escalation_above_*` thresholds well below the cap.** Two racing agents both ESCALATE rather than both ALLOW; operator review serializes the decision.
    - **Use class `high_risk` for shared-budget actions.** High-risk class uses a project-level filesystem lock (see "High-risk lock specification" below) that synchronizes across all agents in the project.
    - **Accept eventual consistency.** For low-stakes mandates where occasional minor overruns are tolerable, the eventual-consistency model is sufficient. Doctor surfaces sustained over-cap conditions via `check_mandate_overrun`.

The framework does not provide a built-in distributed lock for cross-agent token / external budget reservations beyond the high-risk filesystem lock. Operators who need stricter cross-agent atomicity should reach for the high-risk class or external orchestration. This is an honest limitation; the alternative (a per-mandate distributed lock for every reservation) would impose unbounded latency on the common case to defend a rare one.

## Cost integration (`spec/09` and `spec/28`)

The judge layer's cost ledger split (spec/28) introduced `cost_source: "actor" | "judge"` on cost events. **Mandates do not replace `cost_source`** — that would silently undercount the actor's budget. Instead, mandate citing is an *additional* tag on cost events:

- A mandate-citing action's token cost event keeps `cost_source: "actor"` (so the actor's cost guardrail continues to count it normally).
- The same event gains a new `mandate_id: "<id>"` field (or `null` for non-mandate-citing actions). The mandate ledger is computed by filtering events on `mandate_id`, not on `cost_source`.
- A new cost event type `external_cost` carries tool-reported real-money cost. Schema:
    ```json
    {
      "event": "external_cost",
      "run_id": "...",
      "parent_run_id": "...",
      "agent": "...",
      "cost_source": "actor",
      "mandate_id": "procurement-q2-2026",
      "tool_name": "stripe.subscribe",
      "external_cost_usd": 200.00,
      "external_cost_currency": "USD",
      "external_cost_reversibility": "refundable_within_30d",
      "ts": "..."
    }
    ```
- The doctor and `_costs.sum_cost_for_period()` gain a `mandate_id` filter parameter (alongside the existing `source` filter from spec/28). The actor cost guardrail continues to query by `cost_source` only; the mandate ledger queries by `mandate_id`. The two budgets are summed independently — a token cost event with `cost_source: "actor"` AND `mandate_id: "X"` counts against both the actor's daily budget AND mandate X's daily token budget.
- Legacy cost events without `mandate_id` (i.e., everything emitted by pre-mandate framework versions) are treated as `mandate_id: null` and are excluded from any mandate's budget query. The actor budget arithmetic is unchanged for legacy deployments per rule #14.

### Cost reservation pattern

`MandateCheck`'s cumulative-budget check must defend against the **stale-budget race** failure mode (per RFC #115 §"Failure modes"):

1. Actions A and B both citing mandate M and both proposed within a short window
2. Action A passes MandateCheck (cumulative spend + A's cost ≤ cap)
3. Action B reads the same pre-A cumulative spend
4. Action B passes MandateCheck (cumulative spend + B's cost ≤ cap; ignoring A's pending spend)
5. Both execute; total cumulative spend now exceeds cap

Mitigation: MandateCheck reserves the projected cost at the moment of validation. The reservation is a transient JSONL event (`mandate_reservation`) that subsequent checks consume. Reservations expire on a configurable TTL (default 60 seconds). When the action executes, the actual cost event commits or rolls back the reservation. This is the same reservation pattern that helper batches use today (per rule #4).

Reservation events (token + external use the same shape, distinguished by `cost_kind`):

```json
{ "event": "mandate_reservation", "mandate_id": "...", "proposal_id": "...", "cost_kind": "token|external", "projected_usd": 0.50, "ts": "...", "ttl_s": 60 }
{ "event": "mandate_reservation_committed", "mandate_id": "...", "proposal_id": "...", "cost_kind": "...", "actual_usd": 0.47, "ts": "..." }
{ "event": "mandate_reservation_rolled_back", "mandate_id": "...", "proposal_id": "...", "cost_kind": "...", "reason": "judge_block", "ts": "..." }
{ "event": "mandate_reservation_expired", "mandate_id": "...", "proposal_id": "...", "cost_kind": "...", "ts": "..." }
```

`MandateCheck`'s cumulative-spend computation sums **cost events with non-null `mandate_id`** (the source-of-truth for actual spend; `mandate_used` is the derived audit-reader view over these — see §"Atomic emission of `mandate_used`" below) + outstanding `mandate_reservation` events whose age is below TTL. This makes concurrent mandate use safe under the standard JSONL audit pattern; no distributed lock required at the framework level.

**Definition — "outstanding" (closes the cost-event-before-`_committed` double-count window, plan-subagent PR 3b Risk A):** a `mandate_reservation` event is `outstanding` iff ALL of:
1. No matching `_committed` / `_rolled_back` / `_committed_on_recovery` event exists for its `proposal_id`
2. No matching `_expired` event exists for its `proposal_id`
3. **No cost event tagged with the same `proposal_id` exists in the log** (this clause is the new one — it suppresses the reservation during the wall-clock window between the cost event landing and `_committed` landing, eliminating the double-count where `compute_outstanding` would otherwise count both the reservation AND the actual cost during that ~ms window)
4. Its age (current time minus event timestamp) is below `reservation_ttl_s` (default 60)

This requires cost events (per spec/09) to carry `proposal_id` when the cost is incurred by a mandate-citing action. The implementation extends `RunRecord.extra` with `proposal_id` (backward-compatible — legacy records without the field are not mandate-citing, treated as `proposal_id: null`).

**Atomic emission of `mandate_used` (plan-subagent PR 3b Risk E + Risk 4 — second-pass amendment):** `mandate_used` is a **derived view**, not a separately-emitted JSONL line. The cost event that the framework writes for a mandate-citing action **is** the `mandate_used` surface: it carries `cost_source: "actor"`, `mandate_id: "<id>"`, `proposal_id: "<id>"`, `cost_usd`, and is timestamped at the action's commit moment. Audit readers (dashboard, `atomic-agents mandate usage`, doctor) synthesize `mandate_used` events from cost events whose `mandate_id` is non-null; the `cumulative_token_after` / `cumulative_external_after` fields are computed by the reader from the running sum of prior cost events with the same `mandate_id` up to and including this event.

This collapse is the **load-bearing atomicity fix**. `LogBackend.append(record)` is the framework's single-record atomic primitive (spec/22 MUST #2); it does NOT expose a batch append, and adding one would require a spec/22 amendment to a locked spec. Embedding the `mandate_used` semantic in the cost event means the cost event and the lifecycle surface land in the same atomic fsync — there is no crash window in which the cost is recorded but `mandate_used` is missing.

The cumulative-spend math (per `compute_outstanding` clause 3 below) is the single source of truth and reads cost events directly — it does not depend on `mandate_used` being separately materialized. Operators reading the `mandate_used` event family in the JSONL via grep see cost events with `mandate_id` set; tooling that surfaces a discrete `mandate_used` listing reconstructs it on the fly.

`mandate_reservation_committed` continues to emit as a separate JSONL line after the cost event lands — it is the reservation-lifecycle resolution (consumed by `compute_outstanding` clause 1), not the audit surface for actual spend. A crash between the cost event and `mandate_reservation_committed` is correctly handled by the recovery path: the recovery scan sees the orphan reservation, observes the cost event with matching `proposal_id`, and emits `mandate_reservation_committed_on_recovery` at the projected amount — the same arithmetic that would have run if the framework had not crashed.

**`mandate_cites_in_call` field (multi-mandate `agent.call()` apportionment hint — plan-subagent PR 3b Round 2 R2-4 amendment):** When a single `agent.call()` invocation completes successfully with mandate cites against **more than one** distinct mandate ID, the per-call `agent_call` cost event carries an additional top-level field `mandate_cites_in_call: list[str]` listing all distinct mandate IDs cited in this call. The top-level `mandate_id` and `proposal_id` fields are populated from the **most-recent** cite in the call's iteration sequence — a v1 simplification of the spec/29 token-cost apportionment rule (line 376 names argument-token-weighted apportionment as the precise form). Operators reading audit JSONL see the under-attribution explicitly: a call with `mandate_cites_in_call: ["procurement-q2", "vendor-mgmt"]` has had its full token cost attributed to whichever mandate was cited last, not split across the two. The `mandate_cites_in_call` field is the cue for `atomic-agents mandate usage` to surface "multi-mandate call detected — review token-cost attribution" rather than display a misleading per-mandate sum. Absent from single-mandate calls; absent from non-mandate-citing calls (back-compat).

### Crash recovery for reservations

JSONL reservation events have an inherent crash-safety hole: if the framework writes `mandate_reservation`, the tool handler executes an external action successfully, and then the framework crashes before writing `_committed` or `_rolled_back`, the TTL eventually releases the budget — but the spend already happened. Concurrent reservations after TTL would re-use the same headroom against money the operator has actually spent.

Mitigation: on framework startup, a recovery pass scans the agent's JSONL log for orphaned reservations (reservation events with no matching `_committed` / `_rolled_back` / `_expired` event). For each orphan:

- **`cost_kind: token`**: the worst case is the LLM call partly happened; conservatively treat as committed at the full projected amount. Emit `mandate_reservation_committed_on_recovery` with `recovery: true` annotation.
- **`cost_kind: external`** (the operator's real money): the worst case is the external action ran. Conservatively treat as committed at the full projected amount, AND emit a `mandate_reservation_external_unverified` event that the doctor surfaces (`check_mandate_unverified_external_reservations`). The operator gets a doctor warning prompting them to verify whether the external action actually completed (check Stripe / vendor / wherever); they can mark it verified-completed or verified-cancelled via a one-shot CLI: `atomic-agents mandate reconcile <reservation-id> --action committed|rolled_back`.

The TTL-expiry pathway is only valid when the framework was *alive and running* through the entire TTL window — i.e., it consciously decided the action did not happen. After a crash, TTL is suspect; the recovery pass takes over.

This conservatism is the load-bearing trade. The framework cannot, in general, determine post-crash whether an external action committed. The choice is between (a) optimistic: assume rollback unless told otherwise, which silently loses budget tracking on real money; or (b) pessimistic: assume committed unless told otherwise, which over-reports budget but never under-reports. Mandates pick (b) because the failure mode of over-reporting (operator blocked from legitimate further actions until they reconcile) is recoverable; the failure mode of under-reporting (silent budget overrun) is not.

**Cross-process recovery serialization (plan-subagent PR 3b Risk B — multi-process duplicate-recovery defense):** the pessimistic over-report rule was designed to over-report by ONE projected amount per orphan, NOT by N replicas. In multi-process deployments (Cloud Run with N replicas, parallel workers) where all replicas share `<scope>/mandates.md` and the JSONL log, concurrent restarts independently scanning the log will independently emit `_committed_on_recovery` for the same orphan reservation — `compute_outstanding` then double-counts to N × the actual exposure. **Recovery emission MUST be serialized via `LockBackend.acquire("mandate-recovery-<scope>", ttl_s=...)`.** Only the lock-holding replica writes the `_committed_on_recovery` events for a given scope; other replicas observe the events on next state read and skip. The framework's already-shipped `LockBackend` (per spec/21) provides the primitive; `FilesystemLockBackend` (POSIX `fcntl.flock`) works for single-host multi-process; `RedisLockBackend` works for multi-host. Recovery without this lock is a documented breaking-correctness bug.

**TTL expiry is in-process-only (plan-subagent PR 3b Risk H):** the `_expired` event is emitted only by the process that wrote the corresponding `_reservation` event, via an in-memory TTL watcher. Cross-process readers MUST NOT emit `_expired` for a reservation they didn't reserve — they cannot distinguish "framework was alive past TTL" from "framework crashed mid-TTL." Stale-reservation handling on cross-process readers goes through the recovery path (above), not the TTL path. This makes single-process deployments simpler (in-memory TTL is sufficient) and multi-process deployments correct (recovery handles all uncertainty).

### Blind-read fail-closed posture (issue #506)

> **Versioned normative addendum (v1, issue #506).** This subsection is a normative gate-site posture rule on the cost-read surface. It is **outside** the numbered Implementer-Contract MUST tally below (it constrains the live mandate spend-gate's read behavior, not a `MandateBackend` storage/recovery capability) — mirroring the spec/22 read-failure addendum (#497) and the spec/41 addendum (#483).

When any of the prior-spend / baseline-projection read helpers (`_sum_prior_token_cost`, `_sum_prior_external_cost`, and `_project_token_cost` **when a token cap is in effect**) raises a genuine read-failure (`LogBackendReadError`, `OSError`, `sqlite3.DatabaseError`), `evaluate()` MUST return BLOCK with reason `mandate_cost_unreadable` rather than treating the unread spend as `$0.00` (fail-open). This is the **read-side twin** of the §"Crash recovery for reservations" write-side "over-report, never under-report" rule — two faces of one *never-under-report-spend* invariant: when the gate cannot verify prior spend, it MUST refuse, never assume zero. This posture covers the full read surface — the budget sums consumed in steps 7-8 **and** the cap-exceeded verdict sums (`_sum_prior_token_cost` / `_sum_prior_external_cost`) re-read when composing the BLOCK verdict — not steps 7-8 alone.

A code defect (`KeyError`, `TypeError`, `AttributeError`) MUST NOT be caught as a read failure — it MUST propagate unchanged so the audit record identifies a bug rather than mislabelling it a blind-read event. The fail-closed catch is therefore **narrow** (the read-failure family above), deliberately unlike `_costs._sum_via_backend`'s broad backstop: that backstop wraps a single foreign `backend.query()` call where any exception means "the backend misbehaved," whereas the mandate helpers' bodies contain the framework's own record access, where a broad catch would convert any latent gate-code defect into a silent fleet-wide BLOCK with a falsified `mandate_cost_unreadable` reason.

`_project_token_cost` MUST fail closed **only when a token/daily/monthly budget cap is active** (`cap_active=True`). Its blind-read fallback is a non-zero conservative default (`expected_cost_per_call_usd` or `$0.10`) that *over*-projects, so where no cap exists a read failure MUST degrade to that default rather than BLOCK — failing closed there would spuriously block unconstrained agents with nothing to protect (see the cost-read fail-closed gating rule, spec/09 §"Cost-read error posture").

The two prior-spend SUM helpers (`_sum_prior_token_cost`, `_sum_prior_external_cost`) fail closed **unconditionally** — not cap-gated — because their blind-read-as-`$0` is a true fail-open wherever a cap exists. One acknowledged consequence: a mandate with only an escalation threshold and **no** cap will still BLOCK on a SUM read failure even though the summed value gates nothing in that configuration (the escalate decision uses the projection, and the cumulative is compared only against absent caps). This is fail-**safe** (over-block, never leak), but it is a residual over-block the `_project_token_cost` cap-gating rule above argues against. **#512** tracks the optional refinement to cap-gate the SUM helpers too; it is deliberately not done here.

**Named seam (#500).** In the default filesystem deployment the cost-log read is fail-closed-when-blind but **not tamper-evident** — a writer inside the agent vault can truncate or rewrite the log to reset apparent spend (within the documented trust boundary; see spec/09 §"Cost-read error posture"). A future real-authz `LogBackend` MAY add a normative tamper-evidence requirement (append-only, signed, or hash-chained cost records) so that "unverifiable" subsumes "tampered", not only "unreadable". The plug-in point is this blind-read fail-closed path: such a backend extends the set of conditions that raise the read-failure signal handled here.

**Asymmetry with the startup recovery scan (#511).** The live gate above fails closed on a blind read, while the startup orphan-reservation recovery path currently fails *open* on the same `LogBackendReadError` signal. (Precisely: `_scan_orphan_reservations` itself *raises* the read error; its caller — the framework startup path around `recover_orphan_reservations` — wraps the scan in a broad `except` that logs and continues, so the effective startup posture is degrade-and-skip.) This two-paths-one-condition asymmetry is intentional for now — the recovery scan's raise-vs-skip posture is a distinct availability decision (a hard startup failure on one corrupt shared log has org-fleet blast radius) tracked in **#511**, not settled here.

### Action class-aware reservation discipline

For `high_risk` action classes, the framework adds a synchronous filesystem lock around the reserve-judge-execute-commit sequence. This eliminates the TTL race entirely at the cost of serializing high-risk actions across concurrent agent processes.

#### High-risk lock specification

Lock granularity depends on mandate scope:

- **Agent-local mandate** (`<agent>/mandates.md`): lock file at `<agent>/.locks/mandate-<id>.lock`. Only the owning agent's processes contend; isolated from other agents.
- **Project-root mandate** (`<project>/mandates.md`): lock file at `<project>/.locks/mandate-<id>.lock`. ALL agents in the project share this lock; high-risk actions citing the same project-root mandate serialize across the fleet. This is the structural cross-agent serialization that closes the cross-agent reservation race for high-risk classes.

The lock uses the existing `.lock` pattern from spec/01 — atomic create-or-fail; holder writes pid + start_ts; lock acquisition timeout configurable (default 30 seconds) via `judges.md` `## Mandates / high_risk_lock_timeout_s`; timeout returns `BLOCK` with reason `mandate_high_risk_lock_timeout`. Lock is released on successful commit, rollback, or process exit.

`reversible_write` and `external_side_effect` classes use the TTL-only reservation pattern (faster, with the crash-recovery story above; eventual consistency for cross-agent project-root mandates).

## Mandate lifecycle events

Every mandate state transition writes a JSONL event tied to the agent's run log (per the audit-trail discipline of rule #5). Events carry `parent_run_id` where applicable so dashboard rollups stay coherent.

| Event | When written | Carries |
|---|---|---|
| `mandate_granted` | First load that observes a mandate ID not in the framework state file `.judge-state/mandates.json` | `mandate_id`, `granted_by`, `granted_at`, `expires_at`, `scope`, `constraints`, `source_hash` |
| `mandate_used` | **DERIVED view (not a separately-emitted JSONL line) — synthesized by audit readers from cost events with `mandate_id != null`.** See §"Atomic emission of `mandate_used`" above. The cost event itself carries `mandate_id`, `proposal_id`, `cost_source: "actor"`, `cost_usd`, `tool_name`, `parent_run_id`. The reader-side `mandate_used` view adds: `cost_kind` (`token` for cost_source=actor cost events; `external` for the parallel `external_cost` event family per spec/29 §"Cost integration"), `actual_usd` (= the cost event's `cost_usd`), `cumulative_token_after` / `cumulative_external_after` (running sums over the prior cost-event prefix with the same `mandate_id`, computed by the reader). |
| `mandate_revoked` | First load that observes `revocation_state: revoked` for a mandate whose state file recorded `active` | `mandate_id`, `revoked_at`, `revoked_by`, `revocation_reason`, prior `cumulative_token_at_revocation`, prior `cumulative_external_at_revocation` |
| `mandate_expired` | First load that observes current time >= `expires_at` for a mandate whose state file recorded `active`. **EXPIRED is derived state, not a file mutation** — the framework computes it at load time from `expires_at` vs. current time; `mandates.md` itself is never edited. | `mandate_id`, `expired_at`, `cumulative_token_at_expiry`, `cumulative_external_at_expiry` |
| `mandate_cap_exceeded_block` | MandateCheck blocks an action because cumulative + projected > cap | `mandate_id`, `proposal_id`, `cumulative_token_now`, `cumulative_external_now`, `projected_usd`, `cap_kind` (one of daily_token / monthly_token / cumulative_token / daily_external / monthly_external / cumulative_external / per_action_max — **selected by REAL-MONEY-FIRST priority** per plan-subagent PR 3b Risk G + Risk 1 second-pass amendment: `monthly_external > daily_external > cumulative_external > monthly_token > daily_token > cumulative_token > per_action_max`. **Rationale:** external caps are real-money operator-facing dollars and outrank token caps (LLM reasoning cost is recoverable; external spend is not); the monthly window is the cap the operator most commonly authors in mandates.md (daily is a derived guardrail; cumulative is a lifetime ceiling); per_action_max is a per-call safety rail, informational when other caps fire. Forever-stable for JSONL audit grep — operators reading historical events count on the priority being identical 2 years from now.), `additional_caps_exceeded: tuple[str, ...]` (all OTHER cap_kind values that also exceeded; empty when only one cap exceeded — operators reading the audit see the primary blocker plus the full surface), `contributing_reservation_ids: [...]` (reservations whose still-outstanding budget pushed cumulative over; empty if no outstanding reservations), `reconcile_cli_hint: str | null` (when any contributing_reservation_ids are recovery orphans, this is the operator-runnable command — e.g., `"atomic-agents mandate reconcile <id> --action committed\|rolled_back"` — that surfaces in the BLOCK reason so operators see why they're blocked AND what to do, not just that they are) |
| `mandate_reservation` / `_committed` / `_rolled_back` / `_expired` / `_committed_on_recovery` / `_external_unverified` | Per cost reservation pattern above | per-event fields documented above |
| `mandate_id_collision` | Load-time collision between project-root and per-agent files (per-agent entry refused) | `mandate_id`, both source paths, resolution outcome |
| `mandate_unconstrained_loaded` | A mandate with `constraints.unconstrained: true` is loaded | `mandate_id`, `unconstrained_justification` (operator-supplied) |
| `mandate_action_verified` | Post-action verification — fires for action classes `external_side_effect` + `irreversible` after tool handler returns. Target extracted from the tool result matches the `target_canonical` recorded at proposal time, OR pre-extraction returned None AND post-extraction yields a target inside `constraints.allowed_targets`. | `mandate_id`, `proposal_id`, `tool_name`, `target_canonical_at_proposal: str \| null`, `target_canonical_at_execution: str \| null`, `verification_status: "match"`, `ts` |
| `mandate_action_diverged` | Post-action verification — pre-extraction yielded a target, post-extraction yielded a different target. Surfaces actor mis-execution (deliberate or accidental). | Same fields as `mandate_action_verified`; `verification_status: "diverged"`; doctor surfaces via `check_mandate_action_divergence`. |
| `mandate_action_verification_unavailable` | Post-action verification — pre AND post extraction both returned None (no extractor matched the tool's argument shape, in either direction). Best-effort no-op event so audit readers see "the framework tried but couldn't compare." Common for tools without a registered `target_extractor_id` + no heuristic match. | Same fields; `verification_status: "unavailable"`. Plan-subagent PR 3b Risk I — pre-decided shape so implementer doesn't ad-hoc this and pick the wrong default. |

### Lifecycle event deduplication (state file)

To prevent re-emitting `mandate_granted` / `mandate_revoked` / `mandate_expired` events on every load, the framework maintains per-scope state. **State persistence is a `MandateBackend` Protocol concern, not a filesystem-path contract.** The Protocol exposes `read_state(scope)` and `write_state(scope, state)` methods; the `FilesystemMandateBackend` reference impl persists state at `<agent>/.judge-state/mandates.json` (and `<project>/.judge-state/mandates.json` for project-root scope), but a future SaaS / database backend can persist the same state shape in a SQL table or external store.

Mandate IDs can repeat across unrelated agents and projects (`procurement-q2-2026` is a common-enough mandate name that two unrelated projects will collide); keying state by ID alone would cause cross-scope event suppression. State is therefore scoped at the Protocol method boundary:

- **Agent-local mandates** → `read_state("agent:<name>")` / `write_state("agent:<name>", ...)`. `FilesystemMandateBackend` persists at `<agent>/.judge-state/mandates.json`.
- **Project-root mandates** → `read_state("project:<name>")` / `write_state("project:<name>", ...)`. `FilesystemMandateBackend` persists at `<project>/.judge-state/mandates.json`.

Each is independent. An agent computing dedup for a project-root mandate cite calls `read_state("project:<name>")`; for an agent-local mandate it calls `read_state("agent:<name>")`. Cross-scope collision is impossible because the IDs live in different keys.

State shape (returned by `read_state`, accepted by `write_state`):

```json
{
  "schema_version": 1,
  "scope": "agent:<name>" | "project:<name>",
  "mandates": {
    "procurement-q2-2026": {
      "last_seen_state": "active",
      "last_seen_revoked_at": null,
      "last_seen_expired_at": null,
      "last_seen_source_hash": "sha256:abc..."
    }
  }
}
```

**`schema_version: 1` is mandatory.** The state shape carries reservation-orphan fields under the same `schema_version: 1`; readers MUST consult `schema_version` and treat unknown versions as a forward-incompat error (raise `MandateStateSchemaUnsupported`). This mirrors the spec/22/24/25 schema_version discipline — silent migrations are the failure shape `INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1')` closes everywhere else.

On each load:

1. Framework reads `mandates.md` (per-agent + project-root), computes per-mandate source hashes
2. For each loaded mandate, compares against the state file
3. **Transitions only**: `mandate_granted` emits ONLY when the mandate ID is not in the state file. `mandate_revoked` emits ONLY when `last_seen_state == "active"` and the loaded state is `revoked`. `mandate_expired` emits ONLY when the derived `expired` state is reached for the first time. Subsequent loads of already-known mandates emit no lifecycle events.
4. State file is updated atomically (temp + fsync + rename per rule #8) after events emit successfully.

This dedup pattern mirrors the existing `.lock` file convention from spec/01 — framework state lives in a separate, framework-managed path; operator-authored state lives in markdown files; the two don't mix.

Mandate event rollups appear inline in the parent run record (same shape as `helper_provenance`, `delegations`, `tool_calls`, `judgments` per spec/28).

## judges.md integration

`judges.md` gains an optional `## Mandates` section to configure mandate handling at the judge level:

```markdown
## Mandates

reservation_ttl_s: 60          # default 60; lower for tight-loop concurrency
cap_breach_action_class_default:
  external_side_effect: block
  high_risk: escalate
  reversible_write: block
unextractable_target_action: block   # block | escalate
no_expiry_warning: true              # doctor emits check_mandate_no_expiry
```

Absent section → all defaults applied per the table above.

## Doctor integration (`spec/27`)

New checks (full consolidated list):

- `check_mandate_health` — surfaces mandates with high reservation-expiry rate (suggests TTL too low or actions failing post-reservation), high cap-breach-block rate (suggests cap is too tight for actual usage), or no `mandate_used` events in the last 90 days (suggests revocation candidate)
- `check_mandate_no_expiry` — warns on mandates with `expires_at: null`
- `check_mandate_id_collisions` — surfaces project-root vs per-agent ID collisions (per-agent entry refused)
- `check_mandate_source_hash_drift` — surfaces mandates whose `source_hash` has changed since the agent last cited them (informational; common if the operator routinely edits)
- `check_mandate_unconstrained` — surfaces mandates that loaded with `constraints.unconstrained: true` (scope-only enforcement; lists each with its `unconstrained_justification`)
- `check_mandate_unverified_external_reservations` — surfaces orphan `mandate_reservation_external_unverified` events from crash recovery; lists each with the reconciliation CLI command operator can run
- `check_mandate_overrun` — surfaces sustained over-cap conditions on project-root mandates (cross-agent reservation race; operator should consider tightening `requires_escalation_above_*` or moving the mandate's actions to high_risk class)
- `check_mandate_meta_misplaced` — warns when `## _meta` section appears in a per-agent `mandates.md` (silently ignored; operator likely intended project-root)
- `check_mandate_state_inconsistent_followed_by_revoked` — annotates audit-reader pairs of `mandate_state_inconsistent` + subsequent `mandate_revoked` events on the same mandate as a single compound revocation surfacing (per the two-turn surfacing convention)
- `check_mandate_external_cost_unprojectable` — surfaces tools that mandates cite via `allowed_tools` but lack an `expected_external_cost_usd` or `cost_estimator` registration (mandates with `*_external_usd` caps block on these tools)

(`check_mandate_relaxation_violations` from the v1 spec draft is **removed** — the disjoint-ID resolution model no longer has a relaxation concept; per-agent same-ID entries are refused at load via `check_mandate_id_collisions`.)

## Audit shape

Beyond the lifecycle events above, mandate-aware judgment events carry an additional field, and the proposal binding is extended:

```json
{
  "event": "judgment",
  "...": "(per spec/28 JudgmentEvent shape)",
  "binding": {
    "tool_call_id": "...",
    "tool_definition_hash": "sha256:...",
    "arguments_hash": "sha256:...",
    "mandate_source_hash": "sha256:..."        // NEW; null if no mandate cite
  },
  "mandate_cite": {
    "mandate_id": "procurement-q2-2026",
    "target_canonical": "notion.so",            // framework-extracted; what the judge saw
    "cumulative_token_before": 0.43,
    "cumulative_token_after": 0.51,
    "cumulative_external_before": 1247.83,
    "cumulative_external_after": 1289.30,
    "token_cap_remaining": 9.49,
    "external_cap_remaining": 4710.70
  }
}
```

`mandate_cite` is `null` for judgments on proposals that don't cite a mandate (preserves spec/28's existing JSONL shape for non-mandate flows). The `mandate_source_hash` field in `binding` extends spec/28's TOCTOU defense — execution-time binding can re-verify the mandate state is consistent with what the judge approved, and a hash mismatch at execution triggers re-judgment, mirroring the spec/28 pattern for `tool_definition_hash` mismatch.

#### `target_canonical` placement clarification

The framework-extracted `target_canonical` value is stored in **`mandate_cite.target_canonical`** in the JSONL judgment event (and in `ActionProposal.target_canonical` at proposal-assembly time per the spec/28 field extension above). It is **not** part of `binding`. `binding` is reserved for content-hash defenses (the hashes are stable identifiers of what was approved); `target_canonical` is a content value the operator actually wants to read in audits. Placing it under `mandate_cite` keeps `binding` minimal and `mandate_cite` informative.

#### JudgmentEvent.binding schema extension (spec/28 RFC extension)

The `mandate_source_hash` field is added to spec/28's `ProposalBinding`-equivalent shape as a defaulted nullable field — backward-compatible at the data level. Legacy `JudgmentEvent` records without `mandate_source_hash` continue to parse correctly (default `null`). New emissions populate the field when a mandate is cited and leave it `null` when not. The spec/28 source-of-truth shape will be updated alongside the implementation PR; no JudgmentEvent schema bump is required.

### Source-hash-before-state ordering (tradeoff documentation)

`MandateCheck`'s validation order (step 2 = source hash; step 3 = state) means that when an operator revokes a mandate AND the file changes in the same edit pass, the first action-after-revoke surfaces as `mandate_state_inconsistent` (hash drift), not `mandate_revoked` (state). On the actor's next turn (after the inconsistent block, the actor re-authors with a fresh proposal binding against the new mandate state), the state-based block surfaces as `mandate_revoked` properly. This is a deliberate two-turn surfacing for the compound-failure case: the framework refuses to render a state-based block on stale bytes, because state-from-stale-bytes is misleading. Operators reading audit logs see both events — the inconsistent block on the first turn and the revoked block on the second. The doctor's `check_mandate_state_inconsistent_followed_by_revoked` flags this sequence so audit readers know it's the same operator action, not two separate events.

**Clarification — `source_hash` covers file content only, NOT derived EXPIRED state.** `source_hash` is computed over the canonical mandates.md section bytes (per `MandateBackend.load_mandate` MUST #4). The framework derives `EXPIRED` state at load time from `expires_at < now` without editing `mandates.md`. Therefore: when a mandate's `expires_at` tips past `now` between proposal binding (T-1s) and judgment (T+1s) with no file change, step 2 (source hash) passes (hash matches; bytes unchanged) AND step 3 (state) BLOCKs with `mandate_expired` (derived state reads EXPIRED on the fresh load). This is correct behavior: hash equality covers operator-visible state; clock-driven expiry surfaces via step 3 independently. Plan-subagent PR 3a Risk H — explicit rationale added so PR 3a tests pin this timeline (bind at T-1s, judge at T+1s, assert step 2 ALLOW + step 3 BLOCK).

### Concurrent state writes — eventual-consistency boundary

The lifecycle event dedup pattern (`read_state → compute transitions → write_state`) is a read-modify-write that the `MandateBackend` Protocol does NOT atomically guard at the protocol level. Within a single agent process, the framework serializes via a `threading.Lock` on the `MandateCheck` instance's per-scope state computation (cheap; matches the framework's "JSONL append is atomic enough" pattern). Across agent processes sharing a project-root mandate, concurrent state writes can race — one transition may be lost. This is documented as an eventual-consistency limitation; the doctor's `check_mandate_state_inconsistent_followed_by_revoked` surfaces sequences where a later read recovers the missed transition. Operators needing strict cross-process atomicity at the state-file layer must use a SQL-backed `MandateBackend` (post-#124 arc) or accept the limitation. Plan-subagent PR 3a Risk D.

## Operator CLI

Four new subcommands ship with the impl:

```
atomic-agents mandate list [--agent NAME] [--include-revoked] [--include-expired]
atomic-agents mandate show <mandate-id> [--agent NAME]
atomic-agents mandate usage <mandate-id> [--since DATE] [--until DATE]
atomic-agents mandate reconcile <reservation-id> --action {committed|rolled_back} [--reason TEXT]
```

`mandate list` summarizes active mandates with cumulative-vs-cap percentages. `mandate show` prints the mandate's full state plus recent usage. `mandate usage` produces a time-series cost report for the mandate, source = JSONL audit log. `mandate reconcile` resolves an orphaned `mandate_reservation_external_unverified` event from crash recovery — the operator verifies (e.g., checks Stripe / vendor / wherever) whether the external action actually completed and runs `reconcile` with the appropriate `--action`. The command writes a `mandate_reservation_reconciled` event with the operator's decision attached; doctor's `check_mandate_unverified_external_reservations` drops the entry on next run.

Operators **grant** mandates by editing `mandates.md` directly (no `mandate grant` subcommand — the file IS the operator's grant; the framework cannot grant on the operator's behalf). Operators **revoke** mandates by editing `mandates.md` to set `revocation_state: revoked` (no `mandate revoke` subcommand for the same reason). The CLI is read-only with respect to mandates themselves; `reconcile` writes to the JSONL audit log, not to `mandates.md`.

## Only operators grant mandates

**Mandates are operator-authored.** The framework enforces this structurally — only file content authored by the operator becomes mandate state:

- **Skills** (per `spec/18`) can inject instructions into the agent's runtime prompt. Skill text **cannot become a mandate**. A skill that says "you are authorized to spend $5000 on hosting" is informational at most; the agent cannot cite it as a mandate.
- **Dreams** (per `spec/16`) generate memory notes from JSONL run logs + journal entries. Dream output **cannot become a mandate**. A dream that summarizes "the operator typically approves vendor X" is `provenance: generated` memory (per `spec/28`), which is explicitly not instruction-grade.
- **Atomic captures** (per `spec/05`) and **helper outputs** never become mandates. Memory capture markers don't write to `mandates.md`; helpers don't have file-write access to operator-authored files.
- **MCP tool results** never become mandates.
- The framework will **not** auto-create or auto-edit `mandates.md`. The CLI is read-only by design — there is no `mandate grant` subcommand because the file IS the operator's grant.

If an actor tries to cite a mandate that doesn't exist in `mandates.md`, MandateCheck returns `BLOCK` with reason `mandate_not_found`. The actor cannot work around this by writing mandate-shaped content elsewhere in the vault.

## Backward compatibility

Per rule #14, the mandate primitive is **opt-in**. Existing deployments continue to operate with no `mandates.md` present; the judge layer's existing `Authorization(granted_by, scope, granted_at, expires_at)` shape works unchanged.

When an operator adds `mandates.md` and actors begin citing mandate IDs:

- The judge layer registers the cite via the new `mandate_id` field
- `MandateCheck` runs as a built-in specialist; no `judges.md` change required to enable it
- Cost events for mandate-citing actions keep `cost_source: "actor"` (preserving spec/28's actor budget accounting unchanged) and additionally carry a `mandate_id` field. Actor cost guardrails continue to count these events normally. Legacy cost-event consumers that ignore unknown fields continue to work without changes.

The framework will not auto-create or auto-edit `mandates.md`. Operators write mandates by hand.

### Structural write protection for `mandates.md`

The framework enforces write-path safety for operator-authored config (per `spec/20` MemoryBackend's WritePolicy + spec/17 tools.md write paths). The default WritePolicy for any agent excludes the operator-authored config surfaces — `tools.md`, `judges.md`, `mandates.md`, `model.md`, `persona/IDENTITY.md`, `persona/SOUL.md`, `persona/USER.md`. A custom tool, MCP tool, helper, or delegate that attempts to write to any of these paths is refused at the WritePolicy enforcement layer — *before* the mandate layer even sees the attempt. This is the same structural protection that prevents tools from rewriting `tools.md` to grant themselves new permissions. It is NOT a mandate-specific check; it is the framework's general WritePolicy discipline applied to mandates as one of several operator-authored files.

This means: even a malicious actor with a write-capable tool cannot edit `mandates.md` to grant itself authority. The WritePolicy is the authoritative protection. The `## Only operators grant mandates` section above describes the *behavioral* discipline (the actor doesn't get to grant mandates via skills / dreams / captures / helpers / MCP results); the WritePolicy is the *structural* enforcement that backs that discipline.

## Implementer contract for mandate backends

A backend that implements the `MandateBackend` Protocol commits to the contract below. The reference `FilesystemMandateBackend` (markdown + JSON state) is the only reference impl in v1; future SaaS / database / mobile / Slack-bot adapters slot in via `register_mandate_backend(...)` without forking core. Mirrors the spec/22/24/25 Implementer contract patterns.

**Implementers MUST:**

1. **Path-traversal refusal at the API boundary.** Operator-supplied `mandate_id` MUST be validated against `[a-z0-9][a-z0-9-]*` BEFORE any storage access. Reject with `MandateInvalid` for path-traversal tokens, embedded slashes, or control characters. The reference filesystem backend rejects at construction-of-scope-path time; SQL backends parameterize but still refuse at the API boundary as defense-in-depth.

2. **Per-scope isolation enforced at the storage layer.** Agent-local and project-root mandate scopes MUST NOT bleed across. For SQL backends, every query filters `WHERE scope = ?`. For filesystem, scope is enforced by `<agent>/mandates.md` vs `<project>/mandates.md` path discipline + `_io.safe_resolve_under(scope_root)` guards. Cross-scope read or write is a backend bug.

3. **State persistence via `read_state(scope)` / `write_state(scope, state)` Protocol methods, NOT a filesystem-path contract.** The state shape (per §"Lifecycle event deduplication") is the contract; the persistence mechanism is the backend's concern. State writes MUST be atomic (temp + fsync + rename per rule #8 for filesystem; transactional for SQL). State reads MUST return the current `schema_version` field and raise `MandateStateSchemaUnsupported` on unknown versions.

4. **Source-hash recomputation on every `load_mandate(mandate_id)`.** The backend MUST recompute the canonical source hash from the persisted representation on every load. Cached hashes are forbidden — `MandateCheck` step 2 (source hash) relies on fresh computation to detect operator edits. The reference filesystem backend reads `mandates.md` from disk on every load; SQL backends recompute the hash from `descriptor_json` + canonical field ordering.

5. **Lifecycle event emission via `LogBackend.append(record)`, not direct file write.** All lifecycle events (`mandate_granted`, `mandate_used`, `mandate_revoked`, `mandate_expired`, `mandate_cap_exceeded_block`, `mandate_reservation` + 5 variants, `mandate_id_collision`, `mandate_unconstrained_loaded`, `mandate_rebind_suspicious_throttled`) flow through the agent's `LogBackend`. The `MandateBackend` Protocol takes the `LogBackend` instance via constructor injection or per-method parameter so events land in the same JSONL stream that the dashboard + cost guardrail read.

6. **Reservation events use the base `MandateReservationEvent` discriminator shape.** All reservation event variants carry `mandate_id`, `proposal_id`, `cost_kind`, `ts`, `event` (discriminator) at minimum. Variant-specific fields layer on top. Audit readers iterate over the family uniformly; one parser handles all variants. Backends MAY collapse storage (e.g., a single `reservations` table with `event_type` column) but the JSONL emission shape is fixed.

7. **Crash recovery semantics: pessimistic, over-report > under-report.** On framework startup, `MandateBackend.recover_orphan_reservations(log_backend, scope)` MUST scan the JSONL for `mandate_reservation` events lacking a matching `_committed` / `_rolled_back` / `_expired` event. For each orphan: emit `mandate_reservation_committed_on_recovery` (token cost) OR both `_committed_on_recovery` AND `_external_unverified` (external cost). Recovery MUST commit immediately, NOT wait for the TTL — TTL-expiry is only valid when the framework was alive through the entire window. The over-count is recoverable via the reconcile CLI; the under-count (silent budget bypass) is not.

8. **Capability honesty: claim-vs-behavior parity is the load-bearing invariant.** `capabilities() -> MandateCapabilities` is a contract, not a hint. Backends declaring `supports_revocation=True` MUST observe `revocation_state: revoked` from the persisted representation and surface it via `load_mandate(id).revocation_state`. Backends declaring `supports_external_state_change_notification=True` MUST emit `subscribe`-shape callbacks when the persisted representation changes between loads (out-of-process operator edits); backends without external-change observation (filesystem reference) declare `False` and operators get state-change events only on next agent run. Conformance tests gate capability-specific tests on the flag; backends that lie produce silent failures rather than loud refusals.

The reference `FilesystemMandateBackend` implementation in `atomic_agents/mandate/filesystem.py` is the canonical example of this contract. Future Postgres / SaaS / mobile / Slack-bot adapters should mirror its shape; the conformance suite (`tests/test_mandate_protocol_conformance.py`) parametrizes across every registered backend so the contract is verified by the same tests that pin `list_mandates` / `load_mandate` / `read_state` / `write_state` / `recover_orphan_reservations` / `capabilities` semantics.

## Out of scope

This spec describes the **what** and the **where**. It does not pin:

- LLM-judge prompt templates that reference mandate context — refined in the impl PR
- Dashboard tab for mandate browsing — separate implementation issue
- Cross-fleet mandate templates (org-scale standard mandate shapes) — see PolicyBackend (#89) territory; **boundary holds** per /office-hours 2026-05-17 decision; PolicyBackend scope-design queued as the next /office-hours target now that #124 PR 4 has merged
- Second `MandateBackend` reference impl (SaaS / mobile / Slack-bot) — `FilesystemMandateBackend` is the only reference in v1; adapters slot in via `register_mandate_backend(...)` post-arc (per /office-hours Option 2 decision: ship the Protocol seam, defer alternate impls until concrete user demand surfaces)
- Auto-detection of mandate-shaped intent in conversation (\"operator just said something that sounds like a mandate, propose adding it\") — operator UX problem, not a framework concern
- Mandate composition / inheritance beyond project-root vs per-agent — no transitively-inherited mandates from coordinator to delegate beyond the shared project-root file

## Open questions

These are below the threshold of needing resolution before implementation begins. Tentative answers captured here; the implementation PR may revise either way without spec re-review.

1. **Should mandate IDs be globally unique across the project, or only within their file's scope?** Tentative: unique within scope; collisions across files resolved per the resolution rules.
2. **Should `expires_at` accept relative durations (`+30d`) or only absolute timestamps?** Tentative: absolute only — the source file IS the operator's grant, and relative expiry obscures the actual end date at the moment of granting.
3. **Should mandate use feed back into the cost guardrail's `critical=True` exception?** Tentative: no. The judge layer's spec/28 explicit rule (`critical=True` never bypasses the judge) applies — and MandateCheck is part of the judge. A mandate-citing action with `critical=True` still passes through MandateCheck normally.
4. **Should the agent be able to read its own mandates as memory context?** Tentative: yes — actors benefit from knowing what they're authorized to do, but exposure is via the framework's runtime-assembly digest (per spec/04), not via raw file read. The actor sees a summary, not the file.

## References

- `docs/spec/01-anatomy.md` — graduated autonomy framing (cross-link from this spec); see graduated autonomy section landing alongside this work
- `docs/spec/03-file-formats.md` — frontmatter schema patterns (the embedded-YAML-in-markdown convention `mandates.md` follows)
- `docs/spec/05-capture-rules.md` — operator-confirmation discipline (the same pattern mandate grants follow)
- `docs/spec/06-multi-agent-projects.md` — project-root resolution patterns
- `docs/spec/09-cost-observability.md` — cost-event format that mandate spend extends
- `docs/spec/17-tools.md` — tools.md (mandate allowed_tools references)
- `docs/spec/19-mcp.md` — mcp.md (target extraction for MCP tools)
- `docs/spec/27-doctor.md` — extended with mandate-specific checks
- `docs/spec/28-judge-layer.md` — judge layer (Authorization shape extended; MandateCheck specialist added)
- #115 (RFC) — origin
- #89 PolicyBackend — future cross-fleet mandate-template composition
- #87 LLMBackend — judge calls flow through this
- #61 LogBackend — mandate events flow through this

## spec/40 addendum — Canonical export

`MandateBackend` participates in the **Exportable** companion Protocol (spec/40).

`MandateCapabilities.supports_canonical_export = True` for `FilesystemMandateBackend`.
Future SQL-backed MandateBackend implementations default `False` until their export
impls ship.

`export()` returns a `MandateExport` carrying `mandates_by_scope: dict[str, list[Mandate]]`.
Scope discovery for `query=None` scans for `mandates.md` files under `scope_root`.
The `.judge-state/mandates.json` dedup sidecar is intentionally **excluded** from export
(it is an implementation detail, not a portable agent artifact).

For the full normative export contract, see `docs/spec/40-canonical-export.md`.

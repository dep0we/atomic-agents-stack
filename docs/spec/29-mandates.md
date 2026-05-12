# spec/29 — Mandates

> Status: **RFC** (origin: #115). This spec describes a planned surface, not current
> behavior. It is the design hypothesis the maintainer is committing to before
> implementation. The spec locks (drops the RFC marker) when the first reference
> implementation ships and the conformance suite passes. RFC convention is documented
> in `docs/spec/28-judge-layer.md` §"RFC vs locked spec".
>
> Cross-links: spec/01 (anatomy — graduated autonomy framing), spec/03 (file formats), spec/05 (capture rules — operator-confirmation discipline), spec/06 (multi-agent projects — project-root resolution), spec/09 (cost-observability — cumulative budget), spec/17 (tools), spec/27 (doctor), spec/28 (judge layer — Authorization integration)
>
> Related backends: PolicyBackend (#89 — future mandate-template composition), LogBackend (#61 — mandate events), LLMBackend (#87)

## Overview

A **mandate** is a durable, operator-granted scoped authority record. It lives in an operator-managed markdown file (`mandates.md`), is referenced by side-effectful action proposals via `mandate_id`, and is validated by the judge layer at action time.

Where the judge layer's `Authorization` dataclass (per `spec/28`) captures *per-action* authorization (\"the operator told me, in this conversation, to do this thing\"), a mandate captures **durable, cross-run, revocable scoped authority** (\"the operator authorized this agent to handle procurement for Q2 2026 under these constraints\"). The two compose: an action proposal's `Authorization` can cite a mandate by ID, and the judge's `MandateCheck` specialist validates the cite against the mandate's live state.

This spec describes a planned surface. Implementation is tracked by follow-up issues filed after this spec merges.

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
    # Tool handlers report this via the new ToolResult.external_cost_usd field;
    # tools that don't report default to $0.
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

    # Operator-extensible (custom constraints; framework ignores; specialist
    # judges or operator tooling may consume)
    extra: dict[str, Any] = field(default_factory=dict)


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

- Each `## <mandate-id>` section is one mandate. `mandate-id` is the dataclass `Mandate.id`.
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
5. **Target allowlist**: if `constraints.allowed_targets` is set, the proposal's `target_canonical` (extracted by the framework per tool kind — see Target extraction below) MUST match at least one pattern. No match → `BLOCK` with reason `mandate_target_not_allowed`. If `constraints.blocked_targets` is set, the proposal's `target_canonical` MUST NOT match any pattern. Match → `BLOCK` with reason `mandate_target_blocked`. If target extraction fails (no kind-handler for the proposal's tool, no target in tool args) → `BLOCK` with reason `mandate_target_unextractable`.
6. **Time window**: if `constraints.time_window` is set, current time MUST fall within it. Outside → `BLOCK` with reason `mandate_outside_time_window` (or `ESCALATE` if the action class is `high_risk`).
7. **Token budget**: if any `*_token_usd` cap is set, framework sums prior `actor`-source cost events tagged with this `mandate_id` (see Cost integration) and projects the action's cost. Exceeds cap (lifetime / daily / monthly) → see budget-breach action below.
8. **External budget**: if any `*_external_usd` cap is set, framework sums prior tool-reported external cost tagged with this `mandate_id` and projects the action's external cost from the tool definition's `expected_external_cost_usd` field (if set) or the proposal's `proposed_external_cost_usd` declaration (if the actor supplied one). Exceeds cap → see budget-breach action below.
9. **Escalation thresholds**: if `constraints.requires_escalation_above_token_usd` or `requires_escalation_above_external_usd` is set and the corresponding projected cost exceeds it → `ESCALATE` with reason `mandate_escalation_threshold_hit_<token|external>`.

**Budget-breach action (steps 7 + 8)**: behavior depends on action class —
- `high_risk` → `ESCALATE` with reason `mandate_cap_would_exceed_high_risk`
- `external_side_effect` → `BLOCK` with reason `mandate_cap_would_exceed`
- `reversible_write` → `BLOCK` with reason `mandate_cap_would_exceed`
- `read_only` → not applicable (read_only actions don't cost meaningful external money; token cost is gated by the actor budget, not the mandate budget)

If all checks pass: `ALLOW`. The judgment event's `binding` carries the `mandate_source_hash` so execution-time re-binding can re-verify if needed.

`MandateCheck` is **always-on** when a proposal cites a mandate. It runs before the LLM catch-all judge (per spec/28's `[PolicyJudge, LLMCatchAll]` default composition pattern; `MandateCheck` is a sibling of `PolicyJudge`, both rule-engine, both always-on, both fail-fast). Composition becomes effectively `[PolicyJudge, MandateCheck, LLMCatchAll]` when mandate cites are involved.

### Target extraction (framework-owned, not actor-supplied)

The framework extracts a `target_canonical` value from the proposal at assembly time. This is a **framework-owned** field — the actor does not supply it, and the actor-supplied `target_audience` field from spec/28 (which is a *privacy* surface, not a binding target) is never used as the basis for mandate target matching. The two fields exist for different concerns:

- `target_audience`: actor-supplied privacy surface (`internal` | `external:<surface>`). The privacy judge consumes this. The mandate layer does not.
- `target_canonical`: framework-extracted binding target. The mandate layer consumes this. The actor cannot influence it.

The framework extracts `target_canonical` per the registered tool's metadata. Tool definitions gain an optional `target_extractor: Callable[[dict], str | None]` field:

```python
ToolDefinition(
    name="send_email",
    input_schema={...},
    handler=send_email_handler,
    target_extractor=lambda args: args.get("to"),    # extracts the recipient
    action_class=ActionClass.EXTERNAL_SIDE_EFFECT,
)
```

For tools without a registered extractor, the framework falls back to a set of well-known argument-shape heuristics (the same heuristics that today's audit-classification logic uses): `to`, `recipient`, `target`, `url`, `repository`, `customer_id`, `channel_id`. MCP tools add the `mcp_server` prefix to the extracted value.

If extraction returns `None` and the mandate's `constraints.allowed_targets` is set → `BLOCK` with reason `mandate_target_unextractable`. The fallback is intentionally fail-closed: a mandate that says "only send to these vendors" + a tool the framework can't extract a target from = the framework refuses. Operators who want a tool to be mandate-target-aware must register an `target_extractor` on the tool definition (small impl cost) or omit `allowed_targets` from the mandate (operator decision).

The extracted `target_canonical` is recorded in the proposal binding and in the JSONL judgment event, so post-hoc audit can verify the target the framework *actually saw* matches the target the operator intended to constrain.

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
- The delegate's MandateCheck logs `mandate_used` events into the delegate's own JSONL audit log. The cumulative budget computation reads from the delegate's log only — the coordinator's budget usage is independent of the delegate's. (For shared budgets across coordinator + delegate, operators use a project-root mandate that both agents cite; the per-mandate cumulative budget is computed across all agents that cite the same ID, by aggregating across all run logs in the project.)

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

`MandateCheck`'s cumulative-spend computation sums `mandate_used` events + outstanding `mandate_reservation` events whose age is below TTL. This makes concurrent mandate use safe under the standard JSONL audit pattern; no distributed lock required at the framework level.

### Crash recovery for reservations

JSONL reservation events have an inherent crash-safety hole: if the framework writes `mandate_reservation`, the tool handler executes an external action successfully, and then the framework crashes before writing `_committed` or `_rolled_back`, the TTL eventually releases the budget — but the spend already happened. Concurrent reservations after TTL would re-use the same headroom against money the operator has actually spent.

Mitigation: on framework startup, a recovery pass scans the agent's JSONL log for orphaned reservations (reservation events with no matching `_committed` / `_rolled_back` / `_expired` event). For each orphan:

- **`cost_kind: token`**: the worst case is the LLM call partly happened; conservatively treat as committed at the full projected amount. Emit `mandate_reservation_committed_on_recovery` with `recovery: true` annotation.
- **`cost_kind: external`** (the operator's real money): the worst case is the external action ran. Conservatively treat as committed at the full projected amount, AND emit a `mandate_reservation_external_unverified` event that the doctor surfaces (`check_mandate_unverified_external_reservations`). The operator gets a doctor warning prompting them to verify whether the external action actually completed (check Stripe / vendor / wherever); they can mark it verified-completed or verified-cancelled via a one-shot CLI: `atomic-agents mandate reconcile <reservation-id> --action committed|rolled_back`.

The TTL-expiry pathway is only valid when the framework was *alive and running* through the entire TTL window — i.e., it consciously decided the action did not happen. After a crash, TTL is suspect; the recovery pass takes over.

This conservatism is the load-bearing trade. The framework cannot, in general, determine post-crash whether an external action committed. The choice is between (a) optimistic: assume rollback unless told otherwise, which silently loses budget tracking on real money; or (b) pessimistic: assume committed unless told otherwise, which over-reports budget but never under-reports. Mandates pick (b) because the failure mode of over-reporting (operator blocked from legitimate further actions until they reconcile) is recoverable; the failure mode of under-reporting (silent budget overrun) is not.

### Action class-aware reservation discipline

For `high_risk` action classes, the framework adds a synchronous filesystem lock (per the `.lock` pattern from spec/01) around the reserve-judge-execute-commit sequence. This eliminates the TTL race entirely at the cost of serializing high-risk actions across concurrent agent processes. Trade-off is intentional: high-risk actions are rare and the cost of a brief serialization is dominated by the cost of a budget breach. `reversible_write` and `external_side_effect` classes use the TTL-only reservation pattern (faster, with the crash-recovery story above).

## Mandate lifecycle events

Every mandate state transition writes a JSONL event tied to the agent's run log (per the audit-trail discipline of rule #5). Events carry `parent_run_id` where applicable so dashboard rollups stay coherent.

| Event | When written | Carries |
|---|---|---|
| `mandate_granted` | First load that observes a mandate ID not in the framework state file `.judge-state/mandates.json` | `mandate_id`, `granted_by`, `granted_at`, `expires_at`, `scope`, `constraints`, `source_hash` |
| `mandate_used` | Action citing the mandate executes and emits a cost event | `mandate_id`, `proposal_id`, `parent_run_id`, `tool_name`, `cost_kind`, `actual_usd`, `cumulative_token_after`, `cumulative_external_after` |
| `mandate_revoked` | First load that observes `revocation_state: revoked` for a mandate whose state file recorded `active` | `mandate_id`, `revoked_at`, `revoked_by`, `revocation_reason`, prior `cumulative_token_at_revocation`, prior `cumulative_external_at_revocation` |
| `mandate_expired` | First load that observes current time >= `expires_at` for a mandate whose state file recorded `active`. **EXPIRED is derived state, not a file mutation** — the framework computes it at load time from `expires_at` vs. current time; `mandates.md` itself is never edited. | `mandate_id`, `expired_at`, `cumulative_token_at_expiry`, `cumulative_external_at_expiry` |
| `mandate_cap_exceeded_block` | MandateCheck blocks an action because cumulative + projected > cap | `mandate_id`, `proposal_id`, `cumulative_token_now`, `cumulative_external_now`, `projected_usd`, `cap_kind` (daily_token / monthly_token / cumulative_token / daily_external / monthly_external / cumulative_external) |
| `mandate_reservation` / `_committed` / `_rolled_back` / `_expired` / `_committed_on_recovery` / `_external_unverified` | Per cost reservation pattern above | per-event fields documented above |
| `mandate_id_collision` | Load-time collision between project-root and per-agent files (per-agent entry refused) | `mandate_id`, both source paths, resolution outcome |
| `mandate_unconstrained_loaded` | A mandate with `constraints.unconstrained: true` is loaded | `mandate_id`, `unconstrained_justification` (operator-supplied) |

### Lifecycle event deduplication (state file)

To prevent re-emitting `mandate_granted` / `mandate_revoked` / `mandate_expired` events on every load, the framework maintains `~/.atomic-agents/.judge-state/mandates.json` (path may be configured via `XDG_STATE_HOME`; framework-internal, atomic-write per rule #8):

```json
{
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

New checks:

- `check_mandate_health` — surfaces mandates with high reservation-expiry rate (suggests TTL too low or actions failing post-reservation), high cap-breach-block rate (suggests cap is too tight for actual usage), or no `mandate_used` events in the last 90 days (suggests revocation candidate)
- `check_mandate_no_expiry` — warns on mandates with `expires_at: null`
- `check_mandate_id_collisions` — surfaces project-root vs per-agent ID collisions
- `check_mandate_relaxation_violations` — surfaces per-agent mandates that relax project-root constraints (these refuse to load; doctor explains why)
- `check_mandate_source_hash_drift` — surfaces mandates whose `source_hash` has changed since the agent last cited them (informational; common if the operator routinely edits)

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

## Operator CLI

Three new subcommands ship with the impl:

```
atomic-agents mandate list [--agent NAME] [--include-revoked] [--include-expired]
atomic-agents mandate show <mandate-id> [--agent NAME]
atomic-agents mandate usage <mandate-id> [--since DATE] [--until DATE]
```

`mandate list` summarizes active mandates with cumulative-vs-cap percentages. `mandate show` prints the mandate's full state plus recent usage. `mandate usage` produces a time-series cost report for the mandate, source = JSONL audit log.

Operators **grant** mandates by editing `mandates.md` directly (no `mandate grant` subcommand — the file IS the operator's grant; the framework cannot grant on the operator's behalf). Operators **revoke** mandates by editing `mandates.md` to set `revocation_state: revoked` (no `mandate revoke` subcommand for the same reason). The CLI is read-only by design; it reports on operator-authored state.

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
- Cost events for mandate-citing actions carry `cost_source: "mandate:<id>"` instead of `actor`; legacy cost-event consumers without source filtering may need updates if they assume `actor` is the only producer outside the judge ledger

The framework will not auto-create or auto-edit `mandates.md`. Operators write mandates by hand.

## Out of scope

This spec describes the **what** and the **where**. It does not pin:

- Concrete protocol method signatures beyond what the dataclasses define — refined in the implementation PR
- LLM-judge prompt templates that reference mandate context — refined in the impl PR
- Dashboard tab for mandate browsing — separate implementation issue
- Cross-fleet mandate templates (org-scale standard mandate shapes) — see PolicyBackend (#89) territory; out of scope here
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

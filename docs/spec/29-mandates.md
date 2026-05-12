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
    daily_usd: float | None = None
    monthly_usd: float | None = None
    cumulative_usd: float | None = None      # lifetime cap of the mandate
    allowed_tools: list[str] | None = None
    allowed_targets: list[TargetPattern] | None = None  # see TargetPattern below
    blocked_targets: list[TargetPattern] | None = None
    requires_escalation_above_usd: float | None = None
    time_window: TimeWindow | None = None    # day-of-week / hours
    extra: dict[str, Any] = field(default_factory=dict)  # operator-extensible


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

## `mandates.md` file format

Operator-managed markdown. The framework reads it; the framework does **not** write to it (mandates are operator-granted by definition; agent-self-granting violates the entire authorization-record discipline).

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
  daily_usd: 200
  monthly_usd: 2000
  cumulative_usd: 6000
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
  requires_escalation_above_usd: 500
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
  cumulative_usd: null
  requires_escalation_above_usd: null
revocation_state: active
revoked_at: null
revocation_reason: null
```

### Parser rules

- Each `## <mandate-id>` section is one mandate. `mandate-id` is the dataclass `Mandate.id`.
- Section body is parsed as YAML (matching the embedded-YAML convention from `model.md` and `judges.md`).
- **Required fields**: `granted_by`, `granted_at`, `scope`, `revocation_state`. Missing required field → `MandateInvalid` at load time (the framework refuses to honor any cite against the invalid mandate; the doctor surfaces the file error).
- **Recommended fields**: `expires_at`. A mandate without `expires_at` is honored but the doctor emits `check_mandate_no_expiry` warnings; long-lived mandates without expiry are an accumulating attack surface.
- **Optional fields**: everything in `constraints:`. Empty / missing `constraints:` means \"no constraints beyond scope.\"
- **Reserved values**: `revocation_state` is `active` | `revoked` | `expired`. Unknown value → `MandateInvalid`.
- Mandate IDs must be unique within the file. Duplicates → `MandateInvalid` for all duplicate entries (operator is alerted; no entry honored). Mandate IDs **may** repeat across files at different scope levels (per-agent vs project-root) — resolution rules apply (see below).
- Mandate IDs follow the pattern `[a-z0-9][a-z0-9-]*` (lowercase + digits + hyphens, starts with alphanumeric). Other characters → `MandateInvalid`. This bounds operator footguns when mandate IDs flow into JSONL events and audit paths.
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
    mandate_id: str | None                   # NEW — present when granted_by starts with "mandate:"
    scope: str                               # plain language; copied from mandate or supplied by actor
    granted_at: str
    expires_at: str | None
```

When `granted_by = "mandate:<id>"`:

- `mandate_id` MUST equal the `<id>` portion of `granted_by` (framework validates; mismatch → `JudgeProposalInvalid`).
- `scope` SHOULD be a direct copy of the cited mandate's scope field. The judge does not enforce string equality (operators may paraphrase) but flags significant deviation as a privacy/quality concern (specialist LLM judge surfaces).
- `granted_at` and `expires_at` SHOULD reflect the mandate's values (the actor cannot grant itself a later expiry than the mandate's actual `expires_at`).

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

1. **Existence**: `mandate_id` resolves to a mandate in the agent's effective `mandates.md` (per resolution rules below). Not found → `BLOCK` with reason `mandate_not_found`.
2. **State**: mandate's `revocation_state == ACTIVE`. Revoked → `BLOCK` with reason `mandate_revoked`. Expired (current time >= `expires_at`) → `BLOCK` with reason `mandate_expired`.
3. **Source hash**: mandate's `source_hash` matches the value bound at proposal time (TOCTOU defense). Mismatch → re-read `mandates.md`, re-validate. If still inconsistent → `BLOCK` with reason `mandate_state_inconsistent`.
4. **Tool allowlist**: if `constraints.allowed_tools` is set, `proposal.tool_name` MUST be in it. Otherwise → `BLOCK` with reason `mandate_tool_not_allowed`.
5. **Target allowlist**: if `constraints.allowed_targets` is set, the proposal's target (extracted per kind — see Target extraction below) MUST match at least one pattern. No match → `BLOCK` with reason `mandate_target_not_allowed`. If `constraints.blocked_targets` is set, the proposal's target MUST NOT match any pattern. Match → `BLOCK` with reason `mandate_target_blocked`.
6. **Time window**: if `constraints.time_window` is set, current time MUST fall within it. Outside → `BLOCK` with reason `mandate_outside_time_window` (or `ESCALATE` if the action class is `high_risk`).
7. **Cumulative budget**: framework computes `mandate_cumulative_spend_so_far` from the `cost_source = "mandate:<id>"` cost-event stream (see Cost integration below) and projects the action's cost. If `(spend + projected) > constraints.cumulative_usd` (or `daily_usd` / `monthly_usd` checked against their respective windows), behavior depends:
    - **Action class `high_risk`**: → `ESCALATE` with reason `mandate_cap_would_exceed_high_risk`
    - **Action class `external_side_effect`**: → `BLOCK` with reason `mandate_cap_would_exceed`
    - **Action class `reversible_write`**: → `BLOCK` with reason `mandate_cap_would_exceed`
8. **Escalation threshold**: if `constraints.requires_escalation_above_usd` is set and projected cost exceeds it → `ESCALATE` with reason `mandate_escalation_threshold_hit` regardless of action class.

If all checks pass: `ALLOW`.

`MandateCheck` is **always-on** when a proposal cites a mandate. It runs before the LLM catch-all judge (per spec/28's `[PolicyJudge, LLMCatchAll]` default composition pattern; `MandateCheck` is a sibling of `PolicyJudge`, both rule-engine, both always-on, both fail-fast). Composition becomes effectively `[PolicyJudge, MandateCheck, LLMCatchAll]` when mandate cites are involved.

### Target extraction

The framework extracts the proposal's *target* per tool-kind:

| Tool kind | Target source |
|---|---|
| Custom tool with `target_audience` set | `proposal.target_audience` (the actor's structured claim) |
| MCP tool | `proposal.mcp_server` + best-effort parse of well-known argument shapes (e.g., GitHub `repo`, Stripe `customer_id`, Slack `channel_id`) |
| Email-shaped tools | argument inspection for `to:` / `recipient` keys |
| Otherwise | NONE — if `constraints.allowed_targets` is set on the mandate, NO target extracted → `BLOCK` with reason `mandate_target_unextractable` |

The fallback is intentionally fail-closed: a mandate that says \"only send to these vendors\" + a tool the framework can't extract a target from = the framework refuses. Operators who want this combination must either narrow `allowed_tools` or extend target-extraction via a future hook (out of scope here).

## Per-agent vs project-root resolution

A multi-agent project (spec/06) may have mandates at two scopes:

1. **Project-root**: `<project>/mandates.md`. Mandates here apply to **all agents** in the project. They form an authorization floor — every agent's effective mandate set includes the project-root entries.
2. **Per-agent**: `<agent>/mandates.md`. Mandates here apply only to that agent. They form an authorization extension — the agent has these mandates *in addition to* the project-root ones.

### Resolution rules

- An agent's effective mandate set is `project_root_mandates ∪ agent_local_mandates` (union by mandate ID).
- **ID collision**: if both files declare a mandate with the same ID, the **project-root entry wins** (operator-managed-at-project-scope outranks operator-managed-at-agent-scope). The framework emits a `mandate_id_collision` event at load time and the doctor (`check_mandate_id_collisions`) surfaces it.
- **Constraint relaxation refused**: the per-agent file MUST NOT declare a mandate that *relaxes* a project-root mandate's constraints. (E.g., if project-root sets `monthly_usd: 1000`, a per-agent mandate with the same ID and `monthly_usd: 2000` is refused at load time → `MandateInvalid`.) This is the same can-only-tighten discipline that spec/28 applies to project-root `judges.md` floors.
- **Per-agent additions**: per-agent mandates with IDs not present in project-root are honored as-is.
- An agent that operates outside any multi-agent project simply uses its own `<agent>/mandates.md` (or has none).

### Why this shape

Project-root mandates let a coordinator agent (per spec/15) carry authorization that delegates legitimately inherit. A delegate's MandateCheck reads the same project-root file the coordinator does and validates the cite identically. This closes the same hole that judge-policy floors close in spec/28 — without a project-root authorization layer, a delegate without `mandates.md` could not legitimately cite a coordinator's authorization.

## Cost integration (`spec/09` and `spec/28`)

The judge layer's cost ledger split (spec/28) introduced `cost_source: "actor" | "judge"` on cost events. Mandates extend this:

- A new value, `cost_source: "mandate:<id>"`, is written on every cost event from an action whose proposal cited mandate `<id>`.
- `_costs.sum_cost_for_period(source="mandate:<id>")` returns the cumulative spend against that mandate.
- The doctor surfaces per-mandate cumulative spend and remaining budget in its standard dashboard surface.

### Cost reservation pattern

`MandateCheck`'s cumulative-budget check must defend against the **stale-budget race** failure mode (per RFC #115 §"Failure modes"):

1. Actions A and B both citing mandate M and both proposed within a short window
2. Action A passes MandateCheck (cumulative spend + A's cost ≤ cap)
3. Action B reads the same pre-A cumulative spend
4. Action B passes MandateCheck (cumulative spend + B's cost ≤ cap; ignoring A's pending spend)
5. Both execute; total cumulative spend now exceeds cap

Mitigation: MandateCheck reserves the projected cost at the moment of validation. The reservation is a transient JSONL event (`mandate_reservation`) that subsequent checks consume. Reservations expire on a configurable TTL (default 60 seconds). When the action executes, the actual cost event commits or rolls back the reservation. This is the same reservation pattern that helper batches use today (per rule #4).

Reservation events:

```json
{ "event": "mandate_reservation", "mandate_id": "...", "proposal_id": "...", "projected_usd": 0.50, "ts": "...", "ttl_s": 60 }
{ "event": "mandate_reservation_committed", "mandate_id": "...", "proposal_id": "...", "actual_usd": 0.47, "ts": "..." }
{ "event": "mandate_reservation_rolled_back", "mandate_id": "...", "proposal_id": "...", "reason": "judge_block", "ts": "..." }
{ "event": "mandate_reservation_expired", "mandate_id": "...", "proposal_id": "...", "ts": "..." }
```

`MandateCheck`'s cumulative-spend computation sums `mandate_used` events + outstanding `mandate_reservation` events whose age is below TTL. This makes concurrent mandate use safe under the standard JSONL audit pattern; no distributed lock required at the framework level.

## Mandate lifecycle events

Every mandate state transition writes a JSONL event tied to the agent's run log (per the audit-trail discipline of rule #5). Events carry `parent_run_id` where applicable so dashboard rollups stay coherent.

| Event | When written | Carries |
|---|---|---|
| `mandate_granted` | Operator writes a new mandate to `mandates.md` (framework detects on next load) | `mandate_id`, `granted_by`, `granted_at`, `expires_at`, `scope`, `constraints`, `source_hash` |
| `mandate_used` | Action citing the mandate executes and emits a cost event | `mandate_id`, `proposal_id`, `parent_run_id`, `tool_name`, `cost_usd`, `cumulative_after_this_action` |
| `mandate_revoked` | Operator edits `mandates.md` to set `revocation_state: revoked` | `mandate_id`, `revoked_at`, `revoked_by`, `revocation_reason`, prior `cumulative_at_revocation` |
| `mandate_expired` | Framework reads a mandate whose `expires_at` is past; auto-marks state on next load | `mandate_id`, `expired_at`, `cumulative_at_expiry` |
| `mandate_cap_exceeded_block` | MandateCheck blocks an action because cumulative + projected > cap | `mandate_id`, `proposal_id`, `cumulative_now`, `projected_usd`, `cap_kind` (daily/monthly/cumulative) |
| `mandate_reservation` / `_committed` / `_rolled_back` / `_expired` | Per cost reservation pattern above | per-event fields documented above |
| `mandate_id_collision` | Load-time collision between project-root and per-agent files | `mandate_id`, both source paths, resolution outcome |

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

Beyond the lifecycle events above, mandate-aware judgment events carry an additional field:

```json
{
  "event": "judgment",
  "...": "(per spec/28 JudgmentEvent shape)",
  "mandate_cite": {
    "mandate_id": "procurement-q2-2026",
    "mandate_source_hash": "sha256:...",
    "mandate_cumulative_before": 1247.83,
    "mandate_cumulative_after": 1289.30,
    "mandate_cap_remaining": 4710.70
  }
}
```

`mandate_cite` is `null` for judgments on proposals that don't cite a mandate (preserves spec/28's existing JSONL shape for non-mandate flows).

## Operator CLI

Three new subcommands ship with the impl:

```
atomic-agents mandate list [--agent NAME] [--include-revoked] [--include-expired]
atomic-agents mandate show <mandate-id> [--agent NAME]
atomic-agents mandate usage <mandate-id> [--since DATE] [--until DATE]
```

`mandate list` summarizes active mandates with cumulative-vs-cap percentages. `mandate show` prints the mandate's full state plus recent usage. `mandate usage` produces a time-series cost report for the mandate, source = JSONL audit log.

Operators **grant** mandates by editing `mandates.md` directly (no `mandate grant` subcommand — the file IS the operator's grant; the framework cannot grant on the operator's behalf). Operators **revoke** mandates by editing `mandates.md` to set `revocation_state: revoked` (no `mandate revoke` subcommand for the same reason). The CLI is read-only by design; it reports on operator-authored state.

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

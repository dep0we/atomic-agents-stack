# spec/30 — Responsibility Audit

> Status: **RFC** (origin: #116). This spec describes a planned surface, not current
> behavior. It is the design hypothesis the maintainer is committing to before
> implementation. The spec locks (drops the RFC marker) when the first reference
> implementation ships and the conformance suite passes. RFC convention is documented
> in `docs/spec/28-judge-layer.md` §"RFC vs locked spec".
>
> Cross-links: spec/01 (anatomy — graduated autonomy property), spec/05 (capture rules — generated-provenance evidence discipline), spec/08 (evaluation — sibling rubric-driven primitive), spec/09 (cost-observability — cost-event format the audit reads), spec/15 (delegation — one-level, audit scope), spec/16 (dreams — sibling offline-reflection primitive), spec/17 (tools — tools.md the audit reads), spec/19 (mcp — mcp.md the audit reads), spec/20 (memory backend — protocol-pattern template), spec/27 (doctor — audit-aware checks), spec/28 (judge layer — judgment events + class policy the audit reads), spec/29 (mandates — mandates.md + mandate events the audit reads)
>
> Related backends: PolicyBackend (#89 — shipped; fleet-level audit composition over PolicyBackend is future work), LogBackend (#61 — audit events flow through this), LLMBackend (#87 — optional enrichment uses this)

## Overview

The **responsibility audit** is a scheduled or on-demand offline-reflection primitive that reads cross-cutting state — `tools.md` + `judges.md` + `mandates.md` + recent run logs (JSONL) + the escalation queue — and produces a structured per-action-class coverage report. The audit's *primary output* is the **gap analysis**: action classes the agent has used in recent runs where authorization coverage is thin, missing, or stale.

Where the **dream pipeline** (`spec/16`) restructures memory and the **eval framework** (`spec/08`) tests output quality, the responsibility audit is the *"have I covered the bases?"* sibling — it doesn't change memory or score output; it surfaces authorization-coverage drift before it becomes a runtime failure.

This spec describes a planned surface. Implementation is tracked by follow-up issues filed after this spec merges.

## Why this exists

After the judge layer (spec/28) and mandates (spec/29), the number of authorization surfaces per agent has grown: `tools.md` (what tools), `judges.md` (what policy by class), `mandates.md` (what scoped authority), `model.md` (what cost caps), plus the runtime audit events (judgments, mandate usage, escalations, reservations). An operator running an agent has no consolidated way to ask:

- *"What can my agent actually do? Who authorized it? Where is coverage thin?"*
- *"Which mandates have I granted that nobody is using? Which should I revoke?"*
- *"Have any tools been used in recent runs without a corresponding mandate or judge policy? Which classes are missing escalation thresholds?"*

Without a consolidating audit, drift between these surfaces accumulates invisibly. A `judges.md` written six months ago may not cover a new tool the agent gained via an MCP server upgrade. A mandate granted in March may be unused but unrevoked. An action class may have escalation drift the operator hasn't noticed (rubber-stamp approvals > 95% or block-spam < 60%).

The framework already has the *shape* for this kind of offline-reflection primitive: dreams + evals are exactly the pattern (markdown output, vault-resident, cron-friendly, structured artifact). The responsibility audit reuses that pattern at a different boundary.

## The seven questions (generalized from commerce)

For each tool the agent has used (rows of the coverage table) across the four action classes (`read_only` / `reversible_write` / `external_side_effect` / `high_risk` — per `spec/28`), the audit asks seven questions per tool — one question per column:

1. **Discovery** — Where does this action's input come from? (Conversation? Memory note? Helper output? Skill? Prior tool result? MCP server discovery?)
2. **Authorization** — Who said the agent could take this action? (Operator-in-conversation? Cited mandate? Persona-implied? Class-default policy? Nothing-explicit?)
3. **Effective constraint** — What scope/target/budget was *actually enforceable*? Authorization names the grantor; effective constraint names the bound. A mandate with no `allowed_targets` and no budget caps is technically authorized but practically unconstrained — the audit surfaces this distinction explicitly. Sources: mandate constraints, judge policy class settings, cost-guardrail caps, `target_canonical` allowlists.
4. **Action execution** — Which tool runs it? What classification? Is the classification declared explicitly in `tools.md` / `mcp.md` or defaulted by the framework?
5. **Evidence** — What audit trail captures the action? (JSONL run log? Judgment event with full proposal binding? Mandate use event? `external_cost` event?)
6. **Reversibility** — Can this be undone? Does the actor's proposal declare a reversibility claim? Does the tool registration declare a rollback path?
7. **Escalation** — What triggers human review on this class? (Judge policy in `judges.md`? Mandate `requires_escalation_above_*`? Class default? Nothing?)

Rows where the answer is *"nobody-explicit"* or *"nothing"* are the failure modes the operator hasn't priced in. They are the audit's headline output.

### Coverage-table populatability — v1 degraded mode

**Honest framing**: the seven-row coverage table assumes the framework's JSONL logs and `ActionProposal` carry every field the audit needs to populate each cell. Today, they do not. The current framework records `tool_name`, latency, and error on tool-call events (per `atomic_agents/agent.py`); the parent rollup drops inputs, outputs, classification source, authorization source, and reversibility. `ActionProposal` (per spec/28) has no explicit `discovery_source` field — the closest signals are `evidence`, `loaded_skills`, and context-adjacent fields.

The audit therefore operates in two modes:

- **Legacy mode (v1 reference impl)**: Rows are populated where data exists; missing fields render as `unknown (legacy)` in the coverage table. The audit emits a `coverage_legacy_unknown_count` summary metric. Operators see what the framework currently captures and what it can't yet. **An "unknown (legacy)" row is itself a gap** — the audit's recommendations include "the framework cannot yet capture authorization-source for this action class; consider scheduling an upgrade after the schema-extension PR lands."
- **Rich mode (post-schema-extension)**: Once a follow-up impl PR extends `ActionProposal` with explicit `discovery_source` + classification-source fields, and extends the JSONL tool-call event with `proposal_id` + `classification` + `classification_source` + `authorization` fields, the audit populates all rows directly from logged data.

The follow-up schema-extension impl PR is filed alongside this spec's other implementation issues. It is **not** a prerequisite for v1 — operators who run the audit against a current-framework agent get a useful (if partial) coverage report and a clear note about which rows depend on the schema extension.

## Semantics

```
Operator runs CLI (or cron fires)
                │
                ▼
   audit reads:
     tools.md        — declared tools + classifications
     mcp.md          — MCP server tools + default_action_class
     judges.md       — per-class policy + specialist composition
     mandates.md     — durable scoped authority + constraints
     model.md        — cost guardrails
     log/*.jsonl     — agent runs, tool calls, judgment events,
                       mandate events, escalations, costs
     escalations/    — pending + resolved escalation files
     .judge-state/   — framework state (mandate dedup)
                │
                ▼
   audit synthesizes:
     coverage table (7 columns × N tools × M action classes used in window)
     gap list       (rows scoring weak/missing)
     mandate usage  (last_used per mandate; revocation candidates)
     escalation drift per class (approval rate signals)
     stale-policy signal (file unedited beyond expected cadence)
     doctor findings cross-reference
                │
                ▼
   audit writes:
     <agent>/audits/responsibility-<YYYY-MM-DD>.md
     (or <project>/audits/project-responsibility-<YYYY-MM-DD>.md)
                │
                ▼
   doctor surfaces the audit:
     check_responsibility_audit_age
     check_responsibility_audit_gap_count
     check_responsibility_audit_stale_policy
```

## Authorization-surface inventory (what the audit reads — and doesn't yet)

To set expectations honestly, the audit's *inputs* (the authorization surfaces it inspects) are explicitly listed. The list separates **covered** surfaces (v1 reads these) from **deferred** surfaces (the audit doesn't read these yet; impact noted).

### Phantom MCP tools

MCP servers may be removed from `mcp.md` after producing JSONL events. The audit treats such tool calls as "orphaned MCP cite — server removed" and:

- Lists them in the coverage table with `[orphaned MCP cite]` annotation in the `Action execution` column
- Surfaces them as a discrete row in the gaps section: "agent used MCP server `<name>` X times in window; server is no longer registered in `mcp.md` (removed at unknown date). If these tool calls were legitimate, restore the registration with appropriate classification; if not, no action needed."
- Does NOT default to `external_side_effect` for orphaned tools — there's no current registration to defer to, so the classification cell renders as `unknown (server removed)` rather than a defaulted value. This avoids implying a class that may not match what the operator originally intended.

### Doctor snapshot staleness during long-running audits

Project-level audits can run for 60+ seconds (the project-agent-status threshold for `running`). The audit's `doctor_snapshot_at` pins the read time, but doctor findings may resolve or surface during the audit window. The audit's "Doctor findings cross-reference" section annotates each cited finding with `(as of <doctor_snapshot_at>)` so readers know whether to re-check.

A finding written into the audit at minute 0 may be "doctor reported X" which is no longer true at minute 1. This is an honest snapshot — the audit does not re-poll the doctor mid-run. Operators reading the audit see the snapshot timestamp and can verify against the live doctor surface if the cited finding is decision-load-bearing.

### Covered in v1

- `<agent>/tools.md` — declared tools + classifications
- `<agent>/mcp.md` — MCP server defaults + per-tool classification overrides (per spec/19)
- `<agent>/judges.md` — per-class policy + specialist composition + failure policy
- `<agent>/mandates.md` — mandate grants + constraints + revocation state (per spec/29)
- `<agent>/model.md` — cost guardrails + critical-action declarations
- `<agent>/persona/IDENTITY.md` — for the autonomy ladder cross-reference (spec/01 graduated-autonomy property)
- `<agent>/log/*.jsonl` — agent runs, tool calls, judgment events, mandate events, escalations, cost events
- Escalation files at the path declared in `judges.md`'s `## Escalation queue / destination` (per spec/28). Default: `<agent>/escalations/`. Layout: `<destination>/<class>/<proposal_id>.md` (class-subdirectoried per spec/28's escalation file format). The audit honors operator-configured `destination` paths; reading hardcoded paths would miss escalations for operators who relocate the queue.
- `<agent>/.judge-state/` — framework state (mandate dedup, etc.)
- `<project>/tools.md|judges.md|mandates.md` (project-level audit only)

### Deferred surfaces (the audit will note these as `not yet covered`)

- **Skills** (per spec/18) — skills are registered as built-in tools and loaded into context; they can shape what tool calls are proposed. The audit notes skill-influenced actions but does **not** treat skill files as authorization grants (skills are *capability* not *authority*; see spec/29 §"Only operators grant mandates"). Audit emits a deferred-surface note.
- **`roster.md`** (per spec/15) — controls delegate capability declarations. v1 audit reads coordinator-local files only; project-level audit aggregates delegate audits. The roster's effect on what delegates *can* do is visible through the delegate's per-agent audit; v1 doesn't add a cross-roster check.
- **Capture rules + capture-derived memory** (per spec/05, spec/28 §"`atomic_capture` interaction") — when `judge_captures: true` is set, captures pass through judges; this becomes visible in judgment events the audit already reads. When `judge_captures: false` (default), captures bypass the judge entirely and their authorization story is implicit. v1 audit notes the `judge_captures` setting per agent but does not deep-analyze capture-driven memory writes as a separate surface.
- **MCP server-level config beyond classification** — v1 reads `mcp.md`'s `default_action_class` and per-tool classification overrides. MCP server credentials, scoping, and rate-limiting (server-level features, not framework-controlled) are not covered.
- **Doctor history** — v1 reads the *most recent* doctor output; it does not deep-analyze the history of past doctor findings.

Operators reading the audit see a clear inventory header: *"This audit covered X surfaces; Y surfaces are deferred for v1."* The deferred list is itself an artifact of honest spec discipline — operators know what the framework's automated audit can and cannot see, and can supplement with manual review for the deferred categories.

## Scope of audit (per-agent vs project-level)

Both shapes ship:

- **Per-agent audit**: reads `<agent>/tools.md`, `judges.md`, `mandates.md`, `model.md`, `log/`, `escalations/`, `.judge-state/`, `persona/IDENTITY.md`. Surfaces coverage for that agent only. Use case: an operator running one agent who wants to check its authorization story.
- **Project-level audit**: reads `<project>/tools.md`, `<project>/judges.md`, `<project>/mandates.md` (each optional; included when present) plus aggregates all subordinate per-agent audits. Surfaces fleet-wide patterns — mandate-usage distribution across agents, escalation drift across the fleet, agents inheriting project-root policies vs. tightening locally. Use case: an operator running multiple agents in a multi-agent project (per `spec/06`) who wants the project-shape view.

### Project-level aggregation freshness model

Aggregation is only meaningful when per-agent audits are mutually compatible. The project audit marks each subordinate per-agent audit as one of:

| Status | Meaning | Inclusion |
|---|---|---|
| `fresh` | Per-agent audit ran within the project audit's window | Included in totals |
| `stale` | Per-agent audit older than 30 days OR generated under a different `since`/`until` window | Included with a stale tag; totals are reported with a "partially stale" warning |
| `incompatible` | Per-agent audit's `schema_version` differs from the project audit's expected version | Excluded from totals; surfaced as a discrete row "agent X had an incompatible audit schema_version" |
| `missing` | Subordinate agent has no audit file at all | Excluded from totals; surfaced as a discrete row "agent X has never been audited" |
| `running` | Per-agent audit is in-progress (lock file present) | Excluded from totals; project audit waits up to 60 seconds or proceeds with `running` annotation |

The project audit's executive summary leads with the aggregation health: "Project audit aggregated N fresh + M stale + K incompatible + L missing per-agent audits." Operators see the data quality before reading the synthesized findings.

If too many agents are `incompatible` or `missing` (operator-configurable threshold; default: any incompatible OR more than 25% missing), the project audit refuses to produce aggregated totals and surfaces only the per-agent compatibility report. Better to refuse than to silently sum across schema-incompatible inputs.

## Audit runtime shape

Three trigger paths ship with the impl:

```
# On-demand (default for v1)
atomic-agents audit responsibility [--agent NAME] [--project] [--since DATE] [--since-runs N]

# Scheduled (operator wires via cron / launchd / heartbeat)
0 6 * * 1   atomic-agents audit responsibility --agent caldwell --since-runs 100

# Doctor-triggered (when audit is stale)
atomic-agents doctor --auto-run-audit-if-stale
```

The audit is **not** automatically triggered by individual agent runs (per rule #4, every code path that calls an LLM has a cost gate; auto-running an LLM-enriched audit on every action would burst-spend judge budgets). Operators schedule it explicitly.

### CLI parameters

| Flag | Meaning |
|---|---|
| `--agent NAME` | Audit one specific agent (default: the agent in the current directory) |
| `--project` | Project-level audit (aggregates all subordinate agents) |
| `--since DATE` | Audit runs since the given date (ISO-8601) |
| `--since-runs N` | Audit the most recent N runs (default: 100) |
| `--enrich` | Enable LLM enrichment for gap recommendations (opt-in; cost-budgeted) |
| `--output PATH` | Write to a specific path (default: `<agent>/audits/responsibility-<date>.md`) |
| `--dry-run` | Compute the audit but don't write the file; print to stdout |
| `--format markdown|json` | Output format (default: markdown; json for tooling integration) |

## Audit output format

Audits live in `<agent>/audits/responsibility-<YYYY-MM-DD>.md` (or `<project>/audits/project-responsibility-<YYYY-MM-DD>.md` for project-level). The path is a new convention; the existing `audits/` directory does not yet exist in the project anatomy and is added by this spec's implementation PR (alongside `dreams/`, `outcomes/`, `evals/` which already exist).

```markdown
---
type: responsibility-audit
scope: agent          # agent | project
agent: caldwell
audit_id: audit_20260512T060000_abc12345
generated_at: 2026-05-12T06:00:00Z
run_range:
  since: 2026-04-01T00:00:00Z
  until: 2026-05-12T06:00:00Z
  run_count: 247
sources:
  tools_md_hash: sha256:...
  judges_md_hash: sha256:...
  mandates_md_hash: sha256:...
  model_md_hash: sha256:...
enrichment: rule-engine  # rule-engine | llm
enrichment_model: null   # set when enrichment: llm
gap_count: 3
unused_mandate_count: 1
schema_version: 1
---

# Responsibility audit — Caldwell

**Window**: 2026-04-01 to 2026-05-12 (247 runs)

## Executive summary

3 coverage gaps found across 4 action classes. 1 mandate unused for 42 days
(candidate for revocation). Escalation policy for `external_side_effect`
class is operating in healthy approval-rate range (78%); `high_risk` class
not exercised in the window.

**Most important findings**:
1. `mcp.github.create_pull_request` used 14 times in this window without
   an explicit class declaration in `mcp.md`; classified as
   `external_side_effect` by framework default. Operator should declare
   explicitly to clarify intent.
2. `send_email` actions cite `mandate:procurement-q2-2026` consistently
   but the mandate has no `target_canonical` allowlist; recipients are
   not constrained by the mandate today. If this is intentional, no
   action; if not, add `allowed_targets` to the mandate.
3. `delete_file` (registered as `high_risk`) appears in `tools.md` but
   has not been used in the window. No usage means no audit signal;
   consider revoking the tool registration if it's no longer needed.

## Per-action-class coverage

### `read_only` class (used 891 times by 14 tools)

| Tool | Discovery | Authorization | Effective constraint | Action execution | Evidence | Reversibility | Escalation | Score |
|---|---|---|---|---|---|---|---|---|
| `read_file` | conversation | class-default (read_only: bypass) | tools.md write_paths (read scope) | declared read_only in tools.md | JSONL | reversible (no-op) | not applicable | ★★★ |
| `search_notes` | conversation | class-default | tools.md read_paths | declared read_only in tools.md | JSONL | reversible | not applicable | ★★★ |
| ... | ... | ... | ... | ... | ... | ... | ... | ... |

### `reversible_write` class (used 47 times by 3 tools)

| Tool | Discovery | Authorization | Effective constraint | Action execution | Evidence | Reversibility | Escalation | Score |
|---|---|---|---|---|---|---|---|---|
| `write_note(staged)` | atomic_capture | spec/05 capture rules | tools.md write_paths | declared reversible_write in tools.md | JSONL + memory version | reversible (via restore_version) | not configured | ★★ |
| ... | ... | ... | ... | ... | ... | ... | ... | ... |

### `external_side_effect` class (used 23 times by 4 tools)

| Tool | Discovery | Authorization | Effective constraint | Action execution | Evidence | Reversibility | Escalation | Score |
|---|---|---|---|---|---|---|---|---|
| `send_email` | conversation | `mandate:procurement-q2-2026` | **mandate has no allowed_targets — practically unconstrained** | declared external_side_effect in tools.md | JudgmentEvent + mandate_used | irreversible | judges.md class-default | ★ |
| `create_pr` | conversation | judges.md (judge-decides) | judges.md class policy only | declared external_side_effect in tools.md | JudgmentEvent | reversible (close PR) | judge-decides | ★★ |
| `mcp.github.create_pull_request` | conversation | judges.md (judge-decides) | judges.md class policy only | **defaulted external_side_effect — NOT DECLARED in mcp.md** | JudgmentEvent | reversible | judge-decides | ★ |

### `high_risk` class (used 0 times by 1 declared tool)

No `high_risk` actions in the window. Tool `delete_file` declared but unexercised.

## Gaps

### G1 — `mcp.github.create_pull_request` classification not declared

**What**: This MCP tool has been used 14 times in the window. `mcp.md`
does not declare a `class:` field for it, so the framework defaulted to
`external_side_effect`. The default is reasonable but the audit
surfaces the implicit assumption.

**Why this matters**: Operators looking at `mcp.md` cannot see what
classification governs this tool's runtime behavior; behavior is
implicit. If the operator later decides `mcp.github.create_pull_request`
should be `high_risk` (e.g., always escalate for PRs to `main`), the
explicit declaration is required.

**Recommended action**: Add to `mcp.md`:
```markdown
## github
# ...existing config...
tool overrides:
  create_pull_request: external_side_effect    # or high_risk if you want escalation
```

### G2 — `mandate:procurement-q2-2026` has no `target_canonical` allowlist

**What**: 11 actions cited this mandate. The mandate's `constraints`
include `allowed_tools` but not `allowed_targets`. `target_canonical`
values seen in the audit window: `notion.so` (3), `figma.com` (2),
`1password.com` (4), `slack.com` (1), `external-vendor-x.io` (1).

**Why this matters**: Either the mandate is intended to be vendor-open
(in which case no action) or the mandate's vendor allowlist drifted
out of date (in which case `slack.com` and `external-vendor-x.io`
should not have been allowed but were).

**Recommended action**: Confirm intent. If vendor-allowlist was intended,
add to `mandates.md`:
```markdown
## procurement-q2-2026
# ...existing...
constraints:
  allowed_targets:
    - kind: vendor
      value: notion.so
    - kind: vendor
      value: figma.com
    - kind: vendor
      value: 1password.com
```

### G3 — `delete_file` tool registered but unused

**What**: Declared in `tools.md` as `high_risk`. Used 0 times in the
window.

**Why this matters**: An unused high-risk tool that's still registered
is an open authorization surface for no benefit. Revoking the tool
registration (removing it from `tools.md`) eliminates the surface area.

**Recommended action**: Either remove from `tools.md` or document the
operator's intent (e.g., a comment: "kept for emergency manual
recovery; do not remove").

## Mandate usage report

| Mandate ID | Granted | Expires | Last Used | Cumulative Token | Cumulative External | Status |
|---|---|---|---|---|---|---|
| `procurement-q2-2026` | 2026-04-01 | 2026-06-30 | 2026-05-10 | $0.43 / $10 | $1,289.30 / $6,000 | ★★★ Active, healthy |
| `emergency-deploy-2026-05-09` | 2026-05-09 | 2026-05-10 | 2026-05-09 | $0.02 | $0 | ★★ Used once, expired (auto-derived) |
| `legacy-research-budget` | 2026-01-15 | 2026-12-31 | 2026-03-31 | $1.27 / $20 | $0 | ★ Unused 42 days; **revocation candidate** |

## Escalation drift

| Class | Total escalations | Operator approval rate | Drift signal |
|---|---|---|---|
| `external_side_effect` | 8 | 78% (6 approved / 2 denied) | Healthy — within 60-95% range |
| `high_risk` | 0 | n/a | Class unexercised in window |

## Stale policy

| File | Last edited | Recommended cadence | Status |
|---|---|---|---|
| `tools.md` | 2026-02-14 (87 days ago) | every ~60 days for active agents | ⚠️ Stale; agent has been active |
| `judges.md` | 2026-04-30 (12 days ago) | every ~60 days | ✓ Fresh |
| `mandates.md` | 2026-05-09 (3 days ago) | every ~30 days for time-bounded mandates | ✓ Fresh |

## Doctor findings cross-reference

The following doctor findings exist on the agent at audit time:
- `check_mcp_tool_classification` — flags G1 above
- `check_mandate_health` — flags G2 (low constraint coverage on
  procurement-q2-2026) and the revocation candidate for legacy-research-budget

## Audit notes

This audit was generated by rule-engine coverage detection only;
LLM enrichment was not enabled. Run with `--enrich` for prose
recommendations on each gap.

Next recommended audit: 2026-06-09 (4 weeks from now).
```

### Audit file frontmatter schema

```python
@dataclass(frozen=True)
class ResponsibilityAuditFrontmatter:
    type: str                            # always "responsibility-audit"
    scope: str                           # "agent" | "project"
    agent: str | None                    # set when scope == "agent"
    project: str | None                  # set when scope == "project"
    profile: str                         # "no-judge" | "judge-only" | "mandate-aware" |
                                         # "external-action-heavy" (resolved from `auto`
                                         # at audit time)
    audit_id: str
    generated_at: str                    # ISO-8601
    run_range: dict                      # {since, until, run_count}
    sources: dict                        # source-file hashes
    enrichment_requested: str            # "rule-engine" | "llm"
    enrichment_actual: str               # what actually ran: "rule-engine" | "llm"
    enrichment_dropped_reason: str | None # set when requested == "llm" but actual ==
                                         # "rule-engine"; values include "budget_exhausted",
                                         # "llm_backend_unavailable", "operator_override"
    gap_count: int
    unused_mandate_count: int
    legacy_unknown_count: int            # how many coverage cells were "unknown (legacy)"
                                         # — high count signals operator should run the
                                         # follow-up schema-extension impl PR
    coverage_legacy_unknown_pct: float   # 0.0–1.0; gives a quick health signal
    doctor_snapshot_at: str | None       # ISO-8601 timestamp of the doctor output the
                                         # audit read; null when audit didn't read doctor
    project_agent_status: dict[str, ProjectAgentStatus] | None  # set when scope ==
                                         # "project"; ProjectAgentStatus is a StrEnum
                                         # (defined in the body-dataclasses section
                                         # below)
    schema_version: int                  # currently 1
```

**Honest enrichment frontmatter (P2 #8 from RFC review)**: `enrichment_requested` vs `enrichment_actual` are separate fields. An audit operator who runs with `--enrich` but blows the budget gets `enrichment_requested: "llm"` + `enrichment_actual: "rule-engine"` + `enrichment_dropped_reason: "budget_exhausted"`. Downstream readers (dashboards, doctor) see the honest signal: the operator wanted enrichment; it didn't happen.

### Audit body dataclasses

The audit's report body (coverage table rows, gap list, mandate usage report, escalation drift, stale policy) maps to a set of dataclasses so dashboards, doctor consumers, and impl tests can import stable types rather than re-parsing markdown:

```python
class ProjectAgentStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"
    RUNNING = "running"


@dataclass(frozen=True)
class CoverageRow:
    """One row of the coverage table — per tool per action class."""
    tool_name: str
    action_class: ActionClass                # from spec/28
    discovery: str | None                    # may be "unknown (legacy)" in v1 legacy mode
    authorization: str | None
    effective_constraint: str | None
    action_execution: str
    evidence: str | None
    reversibility: str | None
    escalation: str | None
    score: int                               # 1-3 stars (1=★, 2=★★, 3=★★★)
    legacy_unknown_fields: list[str]         # field names that rendered as "unknown (legacy)"


@dataclass(frozen=True)
class Gap:
    id: str                                  # "G1", "G2", ...
    headline: str                            # short summary
    what: str                                # what the audit observed
    why: str                                 # why it matters
    recommended_action: str                  # operator-facing recommendation
    affected_tools: list[str]                # tool names this gap touches
    affected_action_class: ActionClass | None


@dataclass(frozen=True)
class MandateUsageRow:
    mandate_id: str
    granted_at: str
    expires_at: str | None
    last_used_at: str | None
    cumulative_token_usd: float
    cumulative_token_cap_usd: float | None
    cumulative_external_usd: float
    cumulative_external_cap_usd: float | None
    status_emoji: str                        # "★★★ Active, healthy" | "★★ Used once, expired" | etc.
    revocation_candidate: bool


@dataclass(frozen=True)
class EscalationDriftRow:
    action_class: ActionClass
    total_escalations: int
    operator_approved_count: int
    operator_denied_count: int
    operator_revised_count: int
    approval_rate: float | None              # None if total_escalations == 0
    drift_signal: str                        # "healthy" | "high (>95%)" | "low (<60%)" | "unexercised"


@dataclass(frozen=True)
class StalePolicyRow:
    file_path: Path
    last_edited_at: str                      # ISO-8601; from filesystem mtime in v1
    days_since_edit: int
    recommended_cadence_days: int
    status: str                              # "fresh" | "stale" | "advisory"
    detection_basis: str                     # "mtime" in v1; "reviewed_at" in future


@dataclass(frozen=True)
class AuditReport:
    """Programmatic representation of the audit's full body. The markdown file
    is the canonical artifact; AuditReport is what impl-side consumers
    construct and serialize. Round-trip: AuditReport → markdown → re-parse
    → AuditReport produces the same instance (modulo nondeterministic
    envelope fields)."""
    frontmatter: ResponsibilityAuditFrontmatter
    executive_summary: str                   # 1-paragraph prose
    coverage_rows: list[CoverageRow]
    gaps: list[Gap]
    mandate_usage: list[MandateUsageRow]
    escalation_drift: list[EscalationDriftRow]
    stale_policy: list[StalePolicyRow]
    doctor_findings_referenced: list[str]    # IDs of doctor findings cross-referenced
    audit_notes: str                         # closing notes about audit mode + next recommended audit
```

Impl PR ships `AuditReport.to_markdown()` and `AuditReport.from_markdown(path)` for the round-trip. Dashboard readers consume `AuditReport` directly; the markdown is the operator-facing artifact + the canonical source of truth on disk.

## Rule-engine vs LLM enrichment

The audit operates in two modes:

- **Rule-engine** (default, free): coverage detection is deterministic — read the configured files, walk the JSONL events, populate the table. Recommendations are template-based ("class X has Y escalations with Z% approval rate; consider tightening/relaxing"). No LLM cost.
- **LLM enrichment** (`--enrich` flag, opt-in, cost-budgeted): the audit additionally synthesizes prose recommendations using the LLM (via `LLMBackend` per #87). Useful when multiple weak signals combine and the operator wants natural-language synthesis rather than itemized template output.

The split is intentional: rule-engine detection is the load-bearing part. Operators get accurate coverage tables for free. LLM enrichment is a quality-of-life addition operators pay for explicitly.

### When to use LLM enrichment

- Large audit windows (1000+ runs) where the gap list is long and operators want prioritized recommendations
- Project-level audits where cross-agent pattern synthesis matters
- High-stakes audits before a mandate revocation or class-policy change

### When NOT to use LLM enrichment

- Routine weekly audits — rule-engine output is sufficient
- Cost-sensitive deployments
- Audits run as part of automated CI

## Cost treatment

Per rule #4, every code path that calls an LLM has a cost gate. The audit is no exception.

- Rule-engine audits cost nothing beyond filesystem reads and JSONL parsing.
- LLM-enriched audits flow through `LLMBackend` and emit cost events with **a new `cost_source` value: `"audit"`** (a third value alongside `actor` and `judge` from `spec/28`). The audit self-event filter rule is precise: when reading the agent's JSONL log to populate the coverage table, the audit excludes events matching **either** `cost_source == "audit"` (audit's own LLM-cost events) **or** `event in {"audit_started", "audit_completed", "audit_failed", "audit_budget_exhausted"}` (audit lifecycle events). Both kinds are framework-generated by prior audit runs and would pollute the action-coverage analysis if counted. Pass `--include-audit-spend` to override the cost-event side of the filter (lifecycle events remain excluded; they aren't actor actions).
- Audit-config lives in a new operator-managed `audits.md` file (per the "Audit config file" section below — this resolves the open question from RFC #116).
- Audit cost exhaustion → audit completes in rule-engine-only mode, emits an `audit_budget_exhausted` event the doctor surfaces. The audit file is still produced (rule-engine coverage table without LLM recommendations).

### `cost_source: "audit"` ledger extension — migration discipline

Spec/28 introduced `cost_source: "actor" | "judge"`. Spec/29 added `mandate_id` as an additional field on cost events without changing `cost_source` values. This spec adds `audit` as a third `cost_source` value:

```
cost_source ∈ {"actor", "judge", "audit"}
```

This is **load-bearing migration discipline**, not just a new enum value. The implementation PR must:

1. **Extend `_costs.sum_cost_for_period()`** to accept a `source: Literal["actor","judge","audit"] | None = None` filter (matches the pattern from spec/29). Today, the function blindly sums every `cost_usd` record with no source filter — that consumer must be updated before any audit cost event lands, or actor budgets will silently double-count audit spend.
2. **Default behavior preserved**: when `source=None`, the function continues to sum *all* cost events (legacy behavior). The actor's cost guardrail filters by `source="actor"`; the judge's filters by `source="judge"`; the audit's by `source="audit"`. No consumer is implicitly broken by the new enum value.
3. **Audit excludes its own prior events from audit windows by default**. When the audit reads cost events to populate its coverage table, it filters out events where `cost_source = "audit"` unless `--include-audit-spend` is passed. Without this exclusion, recursive audit analysis becomes a feedback loop. The audit is *about* the agent's actions, not about its own past audits.
4. **Dashboard + doctor consumers updated** to surface audit spend as a separate column / metric. The dashboard's existing cost panel adds a third row for audit spend.

Legacy cost events without `cost_source` continue to default to `actor` per spec/29's backward-compat rule. Existing actor/judge consumers continue to filter by their respective sources and are not polluted by audit events *after the migration step lands*. The migration is part of the audit's implementation PR; merging the spec doesn't change any existing cost arithmetic.

### Implementation-PR dependency ordering

Three specs propose changes to the same `_costs.sum_cost_for_period()` consumer:

- **spec/28 impl PR** introduces the `source: Literal["actor","judge"] | None = None` filter parameter (the actor/judge ledger split)
- **spec/29 impl PR** adds the `mandate_id: str | None = None` filter parameter (mandate ledger queries)
- **spec/30 impl PR** (this spec) extends the `source` literal to `Literal["actor","judge","audit"]` (adds the audit ledger value)

The audit's impl PR **depends on spec/28's impl PR landing first** — without spec/28's source filter, this spec's `source="audit"` filter has no parameter to extend. The audit's impl PR explicitly declares this dependency; the impl-issue tracking surface lists `depends on: <spec/28 impl PR>` so the order can't slip.

Spec/29's `mandate_id` filter is orthogonal — it can land before or after the audit's impl PR without conflict, because it adds a new parameter rather than extending the literal type. The same `_costs.sum_cost_for_period()` consumer ends up with two independent filter parameters: `source` (three-valued literal) and `mandate_id` (string or None).

## Audit config file (`audits.md`)

Audit config lives in a new operator-managed `audits.md` file at the agent root (or project root for project-level audit config). This resolves the open question from RFC #116 — config is *not* in `judges.md` because the audit is a sibling primitive to the judge layer, not subordinate to it. Same shape choice that landed for `mandates.md`.

Absent `audits.md`, the audit runs with default values when explicitly invoked via CLI. The defaults are:

```markdown
# Audits — Caldwell

## Audit budget
daily_usd: 0.10
monthly_usd: 2.00

## Cadence
recommended_interval_days: 30
since_runs_default: 100

## Gap thresholds
warn_above_gap_count: 0          # default: any gap warns the doctor
critical_above_gap_count: 5      # default: 5+ gaps is a critical signal

## Stale-policy thresholds
tools_md_stale_days: 60
judges_md_stale_days: 60
mandates_md_stale_days: 30

## Enabled
enabled: true                    # set to false to suppress doctor audit-age nagging
                                 # for operators who intentionally don't use the audit

## Profile
profile: auto                    # auto | no-judge | judge-only | mandate-aware | external-action-heavy
                                 # see "Audit profile" section
```

### Audit profile

The audit's gap analysis is calibrated by profile. An agent without `mandates.md` shouldn't surface "no mandate cited" as a gap — that's intentional. Profiles:

| Profile | Means | Mandate-related gaps surfaced |
|---|---|---|
| `auto` (default) | Framework detects via the deterministic algorithm below | Resolved to one of the named profiles |
| `no-judge` | Agent has no judges.md (advisory tools.md only) | Mandate gaps not surfaced; tools.md gaps are |
| `judge-only` | judges.md present, mandates.md not used | Mandate gaps not surfaced |
| `mandate-aware` | judges.md + mandates.md both present | All gap types surfaced |
| `external-action-heavy` | mandate-aware + tools have `expected_external_cost_usd` registrations | All gap types + external-cost coverage analysis (see below) |

#### `auto` detection algorithm

The framework resolves `auto` to a named profile via this deterministic precedence (config-shaped, not usage-shaped — operators reading their `audits.md` know which profile applies before any runs happen):

```
if any tool definition has expected_external_cost_usd OR cost_estimator registered:
    → external-action-heavy
elif mandates.md is present (with ≥1 mandate, including project-root mandates the agent inherits):
    → mandate-aware
elif judges.md is present:
    → judge-only
else:
    → no-judge
```

This is **config-shaped**: profile reflects what the operator has configured, not what the agent happens to have run recently. An agent with `mandates.md` but only `read_only` runs in the window is still `mandate-aware` — the operator's intent is mandate-aware. The audit will note "no external_side_effect runs in this window" as a usage observation, but profile-gated gap analysis remains mandate-aware.

The resolved profile is written into the audit's frontmatter `profile:` field so readers see exactly which gap-rules ran.

#### Per-profile gap-analysis differences

Profiles differ in **what gaps are surfaced**, not just "mandate visibility":

| Gap type | `no-judge` | `judge-only` | `mandate-aware` | `external-action-heavy` |
|---|---|---|---|---|
| Missing class declaration in tools.md/mcp.md | ✅ surfaced | ✅ | ✅ | ✅ |
| Missing per-class judge policy | — | ✅ | ✅ | ✅ |
| Escalation drift signals (approval rate outside 60-95%) | — | ✅ | ✅ | ✅ |
| Unused mandate (last_used > 30d) | — | — | ✅ | ✅ |
| Mandate without allowed_targets but action target varies | — | — | ✅ | ✅ |
| Mandate without budget caps | — | — | ✅ | ✅ |
| Tool used in mandate's `allowed_tools` without `expected_external_cost_usd` registration | — | — | — | ✅ |
| Action external cost > 50% of remaining mandate budget at decision time | — | — | — | ✅ |

This makes profile-driven behavior auditable: an operator reviewing the audit's frontmatter `profile: mandate-aware` knows mandate gaps will surface; an operator with `profile: no-judge` knows the audit isn't going to complain about missing judge policy.

### `enabled: false` semantics

When an operator sets `enabled: false`, the audit can still be run explicitly via CLI but:

- The doctor's `check_responsibility_audit_age` is **suppressed** (no nagging for operators who deliberately don't use the audit)
- `check_responsibility_audit_gap_count` is **suppressed**
- Scheduled audits do not auto-run (operator must remove `enabled: false` or explicitly pass `--force` to the CLI)
- A different doctor check, `check_audit_disabled_with_external_actions`, surfaces ONLY if the agent has external_side_effect or high_risk class actions in recent runs (suggesting the audit might be worth enabling)

This closes the "permanent doctor nagging" failure mode for operators who legitimately don't want audits.

## Doctor / audit snapshot semantics

The bidirectional doctor / audit relationship (doctor reads most recent audit; audit reads most recent doctor output) is **tractable via snapshot semantics**:

- The audit reads doctor output **at audit start** and pins the doctor-run timestamp into the audit's frontmatter (`doctor_snapshot_at: <ISO-8601>`). The audit reports only doctor findings present at that snapshot; doctor findings that surfaced or resolved after the snapshot do not appear in the audit.
- The doctor's `check_responsibility_audit_age` and `check_responsibility_audit_gap_count` checks read the most recent audit file's frontmatter but **ignore audits whose `generated_at` is within the last 60 seconds** (i.e., audits that are still being written or just-completed). This prevents the doctor from immediately surfacing a stale-audit warning that the just-completed audit itself was responding to.
- Both directions are read-only — the doctor never writes audit state; the audit never writes doctor state. No circular write dependencies.

## Audit event audit shape

The audit run itself emits structured events (recursive but useful — the audit's run is auditable):

| Event | Carries |
|---|---|
| `audit_started` | `audit_id`, `scope`, `agent` / `project`, `run_range`, `enrichment`, `generated_at` |
| `audit_completed` | `audit_id`, `gap_count`, `unused_mandate_count`, `output_path`, `cost_usd`, `duration_ms` |
| `audit_failed` | `audit_id`, `reason`, `partial_output_path` if any |
| `audit_budget_exhausted` | `audit_id`, `enrichment_dropped: true`, fell back to rule-engine output |

These are written to the agent's standard JSONL log with `cost_source: "audit"` for any LLM cost. Project-level audit events have their own placement — see the "Project audit log placement" subsection below the design-rules table.

## Composition with eval framework (`spec/08`)

The eval framework uses LLM-as-judge against operator-defined rubrics. The audit's gap-detection logic *could* be modeled as an eval rubric ("for each action class, is rubric criterion N satisfied?"). **The spec deliberately does not shoehorn the two together.** Evals score *output quality*; audits score *authorization-surface coverage*. Different concerns, different operator-facing surfaces. They can share underlying primitives (rubric data structures, scoring helpers, LLM-as-judge invocation paths) at the implementation level without sharing the operator-facing concept.

### Eval cross-reference discipline

When evals have run in the audit's window, the audit notes their **existence + most recent verdict** in an "Evals cross-reference" section. **Eval verdicts do NOT enter the audit's `gap_count` and do NOT generate audit recommendations.** Eval failures are spec/08's surface and operator workflow; the audit notes them for completeness but does not absorb them into authorization-coverage reasoning. This guards against the failure mode where eval failures become shadow authorization policy ("the audit said this is a gap; we tightened policy; now the agent can't do its job anymore"). The boundary is honest and one-way: audit knows evals exist; audit doesn't act on them.

## Delegation scope (spec/15)

Per spec/15, delegation is one-level — a coordinator delegates to a delegate; the delegate does not further delegate. Delegated runs are linked to coordinator runs via `parent_run_id`. The audit handles delegation as follows:

- **Per-agent audit of a coordinator**: covers actions the coordinator itself proposed and executed. Delegated actions appear as `delegate_call` events in the coordinator's JSONL log but the *delegate's* action coverage (its tool calls, judgments, mandate cites) lives in the delegate's own log. The coordinator audit notes `"N actions delegated to <delegate-name>; see delegate's audit for coverage"` and links to the delegate's most recent audit (with its freshness status from the project aggregation model).
- **Per-agent audit of a delegate**: covers actions the delegate executed regardless of how they were initiated. The audit reads `delegate_chain` from each proposal (per spec/28 §"Delegation interaction") so coverage-table rows can show "this action was initiated via delegation from coordinator X" vs. "this action was initiated by direct invocation."
- **Project-level audit**: aggregates per-agent audits across coordinator + delegate agents. The fleet-wide coverage table includes delegation source per row, so operators can see which classes of actions flow via which delegation paths.
- **`escalation_propagated` events**: when a delegate's escalation surfaces to the coordinator (per spec/28), the audit notes the propagation in both the delegate's audit (as the originating escalation) and the coordinator's audit (as a propagated escalation). Cross-references rather than double-counting.

## Composition with the dream pipeline (`spec/16`)

Dreams and audits are siblings in shape:

- Both run offline (cron / on-demand)
- Both produce vault-resident markdown artifacts (`dreams/` vs `audits/`)
- Both have frontmatter + structured body
- Both flow through cost guardrails

Differences:

- **Dreams** restructure *memory* (atomic notes, INDEX, journal); the output is consumed by future agent runs as context. Read-write at the memory layer.
- **Audits** analyze *authorization coverage* (tools, judges, mandates, runs, escalations); the output is consumed by the operator as a coverage report. Read-only at the authorization layer; never modifies tools.md / judges.md / mandates.md.

The dream pipeline does **not** generate mandates, judge policies, or tool registrations. The audit does **not** restructure memory. These boundaries are load-bearing — dreams and audits are independent primitives, not chained passes.

## Composition with the doctor (`spec/27`)

The doctor and the audit answer different questions:

- **Doctor**: *"Is this agent's runtime healthy right now?"* Synchronous, binary per check.
- **Audit**: *"Is this agent's authorization coverage adequate over the recent run window?"* Offline, structured per action class.

They compose:

- The doctor gains audit-aware checks that **read the most recent audit** and surface findings: `check_responsibility_audit_age` (warns if no audit in the last 30 days), `check_responsibility_audit_gap_count` (warns if recent audit's gap count > N, configurable), `check_responsibility_audit_stale_policy` (surfaces audit-reported stale-policy signals).
- The audit **reads doctor findings** (per the existing doctor output format) and includes them under "Doctor findings cross-reference" in the audit file. This avoids the operator having to read both surfaces independently.

The audit-doctor relationship is bidirectional but read-only — neither auto-edits the other.

## Composition with PolicyBackend (#89)

Future: PolicyBackend will define org-level policy templates and approval workflows. Audit at fleet scale (across many agents in many projects under one operator-organization) will use PolicyBackend to compose findings — e.g., "across all 14 production agents, escalation drift on `external_side_effect` is X%". Out of scope for v1; flagged here so the eventual implementation doesn't paint into a corner.

## How this fits the framework's design rules

| Rule | Fit |
|---|---|
| #1 vault is the source of truth | Audits live as markdown in `audits/`; audit config in operator-managed `audits.md`; no audit-specific state lives elsewhere |
| #4 cost first-class | LLM-enriched audits have a separate budget via `cost_source: "audit"` + migration discipline (per Cost treatment); rule-engine audits are free |
| #5 audit trail structural | The audit emits its own structured events alongside the audit file; project-level audit events use the standard JSONL format (see Project audit log placement below) |
| #6 progressive disclosure | Executive summary fits the dashboard / doctor surface; full coverage table is one-click-deeper |
| #7 markdown config or no config | Audit output is markdown with frontmatter; audit config (`audits.md`) follows the same embedded-YAML-in-markdown convention as `tools.md` / `judges.md` / `mandates.md` |
| #8 atomic + idempotent | Audit-file writes via atomic_write per existing convention. **Honest idempotence framing**: the audit has a *deterministic core* (coverage table, gap list, mandate-usage report — all derived from the same JSONL input window) and a *nondeterministic envelope* (audit_id, generated_at, duration_ms — these change every run). Running the audit twice on the same data produces *byte-identical body content* and *different envelope*. Audit events (`audit_started`/`audit_completed`) are filtered out of subsequent audit windows by default (per Cost treatment) so the audit doesn't recursively analyze its own past runs. |
| #10 spec is the product | This RFC produces a numbered spec doc; impl follows |
| #14 backward compat by default | Audits are opt-in (audit-on-demand requires CLI invocation; scheduled audits require operator cron setup; `audits.md` `enabled: false` suppresses doctor nagging). No behavior changes for deployments that ignore the audit. |

### Project audit log placement

Refined per rule #5 discipline — events extend the right audit shape, not invent parallel logs. Project-level audit events live alongside per-agent logs in the conventional `log/` path, just project-scoped:

- Project-level audit emits the same `audit_started` / `audit_completed` / etc. event shapes as per-agent audits.
- Project audit events live in `<project>/log/YYYY-MM/YYYY-MM-DD.jsonl` — mirroring the per-agent log shape (`<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl`) at the project scope. This is symmetric with how per-agent audits emit events to per-agent logs. Dashboard readers and `LogBackend` (#61) consume both paths through the same protocol.
- `.judge-state/` is reserved for *framework-managed lifecycle dedup state* (per spec/29's mandate state file) — small JSON snapshots, not append-only audit logs. Audit events are append-only by nature; they belong in `log/`, not `.judge-state/`.
- Project-root path itself (`<project>/log/`) is created on first project audit run if it doesn't exist (mirrors how `<agent>/log/` is created on first agent run).
- The future `LogBackend` (#61) abstracts both per-agent JSONL and project-level JSONL behind a single protocol; operators using a non-filesystem LogBackend get the same audit events through the same surface.

### Project-root path discovery

When invoked with `--project`, the audit discovers the project root via a precedence walk:

1. `--project-root PATH` flag value if explicitly passed (highest precedence)
2. `ATOMIC_AGENTS_PROJECT_ROOT` environment variable if set
3. Walk up from current working directory until finding either `<dir>/.atomic-agents-project` sentinel file OR `<dir>/agents/` subdirectory (typical project layout per spec/06)
4. If no project root found: refuse with an error explaining how to set `--project-root` or create a `.atomic-agents-project` sentinel

The sentinel file's presence is the operator's explicit declaration "this is a project root." Walking up indefinitely without a clear signal would be too magic. The `.atomic-agents-project` sentinel is added as a new optional file in spec/06's project-anatomy section (filed as a follow-up doc-update issue, not bundled in this PR).

## Doctor integration (`spec/27`)

New checks added by this primitive:

- `check_responsibility_audit_age` — warns when no audit has run in the last 30 days for an active agent (active = has runs in the last 7 days). **Suppressed** when `audits.md` has `enabled: false`.
- `check_responsibility_audit_gap_count` — warns when the most recent audit's gap count exceeds the threshold set in `audits.md` (`warn_above_gap_count`). Critical-tier flag when above `critical_above_gap_count`. **Suppressed** when `audits.md` has `enabled: false`.
- `check_responsibility_audit_stale_policy` — surfaces audit-reported stale-policy signals (per file unedited beyond `audits.md`'s `*_stale_days` thresholds; uses frontmatter `reviewed_at` if present, falls back to filesystem mtime with an advisory note)
- `check_responsibility_audit_unused_mandates` — surfaces audit-reported unused-mandate signals (last_used > 30 days)
- `check_responsibility_audit_escalation_drift` — surfaces audit-reported escalation drift outside the 60-95% approval-rate range
- `check_audit_budget_exhausted` — surfaces audit-budget-exhausted events that fell back to rule-engine output
- `check_audit_legacy_unknown_high` — warns when an audit's `coverage_legacy_unknown_pct > 0.5` (more than half the coverage cells are "unknown (legacy)" — suggests the operator should schedule the framework's schema-extension upgrade)
- `check_audit_disabled_with_external_actions` — fires ONLY when `audits.md` has `enabled: false` AND the agent has used external_side_effect or high_risk class actions in recent runs (informational: "you've turned off audits but your agent is taking actions where audit would help")

### Stale-policy detection — mtime-with-caveat for v1

Filesystem mtime is unreliable across git checkout, file copy, sync (Obsidian / iCloud), and future non-filesystem backends — using it for staleness causes false alarms after every clone. **v1 of the audit uses mtime and explicitly annotates all stale-policy findings as `(mtime-based; advisory only)`.** Operators reading the audit see that staleness is a soft signal.

A future spec/03 extension to add an optional `reviewed_at: <ISO-8601>` frontmatter field to `tools.md` / `judges.md` / `mandates.md` is filed as a follow-up tracking issue alongside this spec's implementation issues. The audit's impl PR does **not** unilaterally extend other specs' file formats; instead, the impl PR ships mtime-only stale detection, and the `reviewed_at` enhancement lands as a separate spec/03 + spec/17 + spec/28 + spec/29 update once design coherence is reviewed at the file-formats level.

This honest scope keeps the audit's impl PR self-contained: it touches only `audits.md` (new), audit output files, doctor checks, and the cost-event filter. It does NOT silently extend sibling specs.

## Audit output file protection

Audit output files at `<agent>/audits/responsibility-<date>.md` are **framework-authored** (the audit runtime writes them), not operator-authored. The WritePolicy default protection from spec/29 covers operator-authored config — it does not cover framework-authored artifacts.

The audit's impl PR adds explicit protection for audit outputs:

- `<agent>/audits/*.md` is on the WritePolicy denylist for custom tools, MCP tools, helpers, and delegates. A tool that attempts to write to this path is refused at the WritePolicy layer, same protection that operator-authored config files get.
- The audit runtime itself bypasses this protection (it has special framework privilege to write under `audits/`).
- **Re-running the audit on the same date**: the framework refuses to overwrite an existing `responsibility-<YYYY-MM-DD>.md`. If the operator wants a second audit on the same date, they pass `--output <path>` to write to a different filename (e.g., `responsibility-<date>-rerun.md`). This makes audit history append-only at the date granularity; operators can't lose a prior audit by accident.
- Older audit files retain their original `generated_at` frontmatter; the most-recent-audit query uses the latest `generated_at` value, not the latest filename mtime.

## Concurrent agent run + audit semantics

An operator may run the audit DURING an agent run (the agent is mid-`call()`). This is supported but requires explicit semantics:

- **Reading mid-write JSONL lines**: the audit's JSONL reader treats any trailing partial line (a line not terminated by `\n`) as "no content yet" and stops reading at that point. The agent's atomic-append-jsonl (per current `atomic_agents/agent.py`) guarantees per-line atomicity, so the audit either sees a complete line or no line — it never sees a half-written line as a parse error.
- **Reading mid-cycle mandate reservations**: the audit may read a `mandate_reservation` event with no matching `_committed` / `_rolled_back` event because the action is still in flight. The audit's cumulative-budget computation treats outstanding-reservations-with-TTL-not-yet-expired as "uncommitted; projected" rather than "missing data." This matches the live MandateCheck reservation pattern from spec/29.
- **Reading mid-cycle judgment events**: a judgment event is emitted *atomically* per spec/28; the audit either sees the event or doesn't. No partial-judgment reads.
- **Eventual consistency**: cumulative figures reported by an audit run during an active agent run are accurate-as-of the audit's read time, plus or minus the in-flight reservations. Operators reading "in-flight: 2 reservations totaling $47.23" understand they're seeing a live snapshot.

The audit does **not** acquire any locks on the agent's state files or logs. The reader is read-only; no write contention occurs. The audit's own output is the only write path, and it writes to its own date-named file.

## Backward compatibility

Per rule #14, the audit primitive is **opt-in**. Existing deployments continue to operate with no audit ever running; no `audits/` directory is created automatically.

When an operator runs the audit:
- First run: creates `<agent>/audits/responsibility-<date>.md`. If `audits/` doesn't exist, the framework creates it (mirrors how `dreams/` and `outcomes/` directories are created on first use).
- Audit reads only the data already present (JSONL logs, tools.md, etc.). No data-format changes required for legacy agents.
- LLM enrichment is opt-in via `--enrich`; rule-engine audits cost nothing.

The framework will not auto-run the audit. Operators schedule it explicitly.

## Out of scope

This spec describes the **what** and the **where**. It does not pin:

- Concrete CLI argument-parser signatures beyond what's listed — refined in the impl PR
- LLM enrichment prompt templates — refined in the impl PR
- Dashboard tab for audit-finding browsing — separate implementation issue
- Cross-organization fleet audits (multi-project) — PolicyBackend territory; future
- Auto-edit of `tools.md` / `judges.md` / `mandates.md` based on audit recommendations — no; operator must read recommendations and decide
- Audit-aware autonomy ladder editing — no; persona files are operator-edited, audit doesn't touch them

## Open questions

These are below the threshold of needing resolution before implementation begins. Tentative answers captured here; the impl PR may revise either way.

1. **Should the audit's first run produce a baseline gap list that subsequent audits compare against?** Tentative: yes, but not in v1. The "audit-over-time delta" feature is a separate enhancement; v1 produces standalone reports.
2. **Should the audit attempt to detect agents that have been *active but never audited*?** Tentative: yes, via `check_responsibility_audit_age` in the doctor. The doctor warns when an active agent has no recent audit.
3. **How should the audit handle agents whose tools have been removed from `tools.md` mid-window?** Tentative: include them in the historical coverage table for the window in which they were active; mark them with a `[removed]` annotation in the table.
4. **Should the audit cross-reference `evals/` results?** Tentative: yes, lightly — if evals have run in the window, the audit notes their existence + most recent verdict. Not part of the gap analysis itself (eval failures are spec/08's surface; audit is authorization-coverage; eval verdicts do not enter `gap_count` per §"Composition with eval framework").

## References

- `docs/spec/01-anatomy.md` §"Graduated autonomy" — the principle the audit measures coverage against
- `docs/spec/05-capture-rules.md` — capture-marker discipline (the audit reads recent captures)
- `docs/spec/06-multi-agent-projects.md` — project-root patterns the project-level audit uses
- `docs/spec/08-evaluation.md` — sibling rubric-driven primitive (concept distinct, infrastructure can be shared)
- `docs/spec/09-cost-observability.md` — cost-event format the audit reads; `cost_source: "audit"` extends it
- `docs/spec/15-delegation.md` — one-level delegation boundary the audit respects
- `docs/spec/16-dreams.md` — sibling offline-reflection primitive (shape pattern)
- `docs/spec/17-tools.md` — tools.md (audit reads tool classifications)
- `docs/spec/19-mcp.md` — mcp.md (audit reads MCP server classification)
- `docs/spec/20-memory-backend.md` — protocol-pattern template
- `docs/spec/27-doctor.md` — extended with audit-aware checks
- `docs/spec/28-judge-layer.md` — judge layer (audit reads judgment events + class policy + escalations)
- `docs/spec/29-mandates.md` — mandate primitive (audit reads mandates.md + mandate events; surfaces unused / over-cap / drift)
- #116 (RFC) — origin
- #87 LLMBackend — LLM enrichment flows through this
- #89 PolicyBackend — future cross-fleet audit composition
- #61 LogBackend — audit events flow through this

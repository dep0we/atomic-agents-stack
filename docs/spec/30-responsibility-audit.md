# spec/30 — Responsibility Audit

> Status: **RFC** (origin: #116). This spec describes a planned surface, not current
> behavior. It is the design hypothesis the maintainer is committing to before
> implementation. The spec locks (drops the RFC marker) when the first reference
> implementation ships and the conformance suite passes. RFC convention is documented
> in `docs/spec/28-judge-layer.md` §"RFC vs locked spec".
>
> Cross-links: spec/01 (anatomy — graduated autonomy property), spec/05 (capture rules — generated-provenance evidence discipline), spec/08 (evaluation — sibling rubric-driven primitive), spec/09 (cost-observability — cost-event format the audit reads), spec/15 (delegation — one-level, audit scope), spec/16 (dreams — sibling offline-reflection primitive), spec/17 (tools — tools.md the audit reads), spec/19 (mcp — mcp.md the audit reads), spec/20 (memory backend — protocol-pattern template), spec/27 (doctor — audit-aware checks), spec/28 (judge layer — judgment events + class policy the audit reads), spec/29 (mandates — mandates.md + mandate events the audit reads)
>
> Related backends: PolicyBackend (#89 — future fleet-level audit composition), LogBackend (#61 — audit events flow through this), LLMBackend (#87 — optional enrichment uses this)

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

## The six rows (generalized from commerce)

For each action class an agent uses (`read_only` / `reversible_write` / `external_side_effect` / `high_risk` — per `spec/28`), the audit asks six questions per row:

1. **Discovery** — Where does this action's input come from? (Conversation? Memory note? Helper output? Skill? Prior tool result? MCP server discovery?)
2. **Authorization** — Who said the agent could take this action? (Operator-in-conversation? Cited mandate? Persona-implied? Class-default policy? Nothing-explicit?)
3. **Action execution** — Which tool runs it? What classification? Is the classification explicit in `tools.md` / `mcp.md` or defaulted?
4. **Evidence** — What audit trail captures the action? (JSONL run log? Judgment event with full proposal binding? Mandate use event? `external_cost` event?)
5. **Reversibility** — Can this be undone? Does the actor's proposal declare a reversibility claim? Does the tool registration declare a rollback path?
6. **Escalation** — What triggers human review on this class? (Judge policy in `judges.md`? Mandate `requires_escalation_above_*`? Class default? Nothing?)

Rows where the answer is *"nobody-explicit"* or *"nothing"* are the failure modes the operator hasn't priced in. They are the audit's headline output.

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
     coverage table (6 rows × N action classes used in window)
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

## Scope of audit (per-agent vs project-level)

Both shapes ship:

- **Per-agent audit**: reads `<agent>/tools.md|judges.md|mandates.md|model.md|log/|escalations/`. Surfaces coverage for that agent only. Use case: an operator running one agent who wants to check its authorization story.
- **Project-level audit**: reads `<project>/tools.md` (if present)`|judges.md|mandates.md` plus all subordinate agents' files. Surfaces fleet-wide patterns — mandate-usage distribution across agents, escalation drift across the fleet, agents inheriting project-root policies vs. tightening locally. Use case: an operator running multiple agents in a multi-agent project (per `spec/06`) who wants the project-shape view.

The project-level audit aggregates per-agent audits rather than re-reading everything from scratch. Each agent emits a per-agent audit; the project audit composes them. Aggregation keeps the project audit cheap relative to running N independent audits.

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

| Tool | Discovery | Authorization | Evidence | Reversibility | Escalation | Score |
|---|---|---|---|---|---|---|
| `read_file` | conversation | class-default (read_only: bypass) | JSONL | reversible (no-op) | not applicable | ★★★ |
| `search_notes` | conversation | class-default | JSONL | reversible | not applicable | ★★★ |
| ... | ... | ... | ... | ... | ... | ... |

### `reversible_write` class (used 47 times by 3 tools)

| Tool | Discovery | Authorization | Evidence | Reversibility | Escalation | Score |
|---|---|---|---|---|---|---|
| `write_note(staged)` | atomic_capture | spec/05 capture rules | JSONL + memory version | reversible (via restore_version) | not configured | ★★ |
| ... | ... | ... | ... | ... | ... | ... |

### `external_side_effect` class (used 23 times by 4 tools)

| Tool | Discovery | Authorization | Evidence | Reversibility | Escalation | Score |
|---|---|---|---|---|---|---|
| `send_email` | conversation | `mandate:procurement-q2-2026` | JudgmentEvent + mandate_used | irreversible | judges.md class-default | ★★ |
| `create_pr` | conversation | judges.md (judge-decides) | JudgmentEvent | reversible (close PR) | judge-decides | ★★ |
| `mcp.github.create_pull_request` | conversation | judges.md (judge-decides) | JudgmentEvent | reversible | **NOT DECLARED in mcp.md** | ★ |

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
    audit_id: str
    generated_at: str                    # ISO-8601
    run_range: dict                      # {since, until, run_count}
    sources: dict                        # source-file hashes
    enrichment: str                      # "rule-engine" | "llm"
    enrichment_model: str | None         # set when enrichment == "llm"
    gap_count: int
    unused_mandate_count: int
    schema_version: int                  # currently 1
```

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
- LLM-enriched audits flow through `LLMBackend` and emit cost events with **a new `cost_source` value: `"audit"`** (sibling of `actor` and `judge` from `spec/28`). Audit budgets are independent of actor and judge budgets — an LLM-enriched audit doesn't consume the actor's daily LLM cap.
- `judges.md` (or a new optional `audits.md` operator config; see Open Questions) declares audit budget:
    ```markdown
    ## Audit budget
    daily_usd: 0.10
    monthly_usd: 2.00
    ```
- Audit cost exhaustion → audit completes in rule-engine-only mode, emits a `audit_budget_exhausted` event the doctor surfaces. The audit file is still produced (rule-engine coverage table without LLM recommendations).

### `cost_source: "audit"` as a third ledger value

Spec/28 introduced `cost_source: "actor" | "judge"`. Spec/29 added `mandate_id` as an additional field on cost events without changing `cost_source` values. This spec adds `audit` as a third `cost_source` value:

```
cost_source ∈ {"actor", "judge", "audit"}
```

`_costs.sum_cost_for_period(source="audit")` returns audit-specific spend. Existing actor/judge consumers continue to filter by their respective sources and are not polluted by audit events. Legacy cost events without `cost_source` continue to default to `actor` per spec/29's backward-compat rule.

## Audit event audit shape

The audit run itself emits structured events (recursive but useful — the audit's run is auditable):

| Event | Carries |
|---|---|
| `audit_started` | `audit_id`, `scope`, `agent` / `project`, `run_range`, `enrichment`, `generated_at` |
| `audit_completed` | `audit_id`, `gap_count`, `unused_mandate_count`, `output_path`, `cost_usd`, `duration_ms` |
| `audit_failed` | `audit_id`, `reason`, `partial_output_path` if any |
| `audit_budget_exhausted` | `audit_id`, `enrichment_dropped: true`, fell back to rule-engine output |

These are written to the agent's standard JSONL log with `cost_source: "audit"` for any LLM cost. Project-level audit events are written to `<project>/audits/audit.jsonl` (a project-scoped log file separate from per-agent logs, because the audit operates over the whole project).

## Composition with eval framework (`spec/08`)

The eval framework uses LLM-as-judge against operator-defined rubrics. The audit's gap-detection logic *could* be modeled as an eval rubric ("for each action class, is rubric criterion N satisfied?"). **The spec deliberately does not shoehorn the two together.** Evals score *output quality*; audits score *authorization-surface coverage*. Different concerns, different operator-facing surfaces. They can share underlying primitives (rubric data structures, scoring helpers, LLM-as-judge invocation paths) at the implementation level without sharing the operator-facing concept.

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
| #1 vault is the source of truth | Audits live as markdown in `audits/`; no audit-specific state lives elsewhere |
| #4 cost first-class | LLM-enriched audits have a separate budget; rule-engine audits are free |
| #5 audit trail structural | The audit emits its own structured events alongside the audit file |
| #6 progressive disclosure | Executive summary fits the dashboard / doctor surface; full coverage table is one-click-deeper |
| #7 markdown config or no config | Audit output is markdown with frontmatter; matches spec/08 eval + spec/16 dream conventions |
| #8 atomic + idempotent | Audit-file writes via atomic_write per existing convention; running the audit twice on the same data produces the same output |
| #10 spec is the product | This RFC produces a numbered spec doc; impl follows |
| #14 backward compat by default | Audits are opt-in (audit-on-demand requires CLI invocation; scheduled audits require operator cron setup). No behavior changes for deployments that ignore the audit. |

## Doctor integration (`spec/27`)

New checks added by this primitive:

- `check_responsibility_audit_age` — warns when no audit has run in the last 30 days for an active agent (active = has runs in the last 7 days)
- `check_responsibility_audit_gap_count` — warns when the most recent audit's gap count exceeds operator-configured threshold (default: 0 = any gap warns; configurable in `audits.md` or `judges.md`)
- `check_responsibility_audit_stale_policy` — surfaces audit-reported stale-policy signals (file unedited beyond cadence)
- `check_responsibility_audit_unused_mandates` — surfaces audit-reported unused-mandate signals (last_used > 30 days)
- `check_responsibility_audit_escalation_drift` — surfaces audit-reported escalation drift outside the 60-95% approval-rate range
- `check_audit_budget_exhausted` — surfaces audit-budget-exhausted events that fell back to rule-engine output

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

1. **Does audit config live in `judges.md`'s new `## Audit budget` section, or in a separate `audits.md` file?** Tentative: in `judges.md` for v1 (simpler; audit is conceptually coupled to the judge layer's data). If operators want richer audit config (per-class gap thresholds, custom recommendations, etc.) it can graduate to a separate file in a future minor.
2. **Should the audit's first run produce a baseline gap list that subsequent audits compare against?** Tentative: yes, but not in v1. The "audit-over-time delta" feature is a separate enhancement; v1 produces standalone reports.
3. **Should the audit attempt to detect agents that have been *active but never audited*?** Tentative: yes, via `check_responsibility_audit_age` in the doctor. The doctor warns when an active agent has no recent audit.
4. **How should the audit handle agents whose tools have been removed from `tools.md` mid-window?** Tentative: include them in the historical coverage table for the window in which they were active; mark them with a `[removed]` annotation in the table.
5. **Should the audit cross-reference `evals/` results?** Tentative: yes, lightly — if evals have run in the window, the audit notes their existence + most recent verdict. Not part of the gap analysis itself (eval failures are spec/08's surface; audit is authorization-coverage).

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

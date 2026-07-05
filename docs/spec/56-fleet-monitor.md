# spec/56 — Fleet Monitor (DRAFT)

**Status:** DRAFT
**Issue:** #653 (Fleet Console rebuild PR-B: the NOC-wall roster page)
**Depends on:** spec/52 (Fleet Console — panel registry §16, shared `status_for_agent()` §17.1, cost/fail-soft posture), spec/53 (FleetHealth scoring), spec/22 (LogBackend RunRecord), spec/51 (AgentRegistryBackend — agent enumeration + governance), spec/09 (cost-read error posture)
**Supersedes:** the per-agent card grid on the console home (spec/52 §8 / §17 EXPLORE) — the roster moves here. (The home relocation already shipped in #635; this spec is the roster's new home.)
**Amends:** spec/52 §16 — extends the panel-registry slot set (see §6).

---

## Overview

The **Fleet Monitor** is the second of the console's three surfaces (Cockpit home → **Fleet Monitor** → per-agent detail). It is the operator's **NOC wall**: every agent rendered as a monitored entity, **status-at-a-glance, problems-first**, dense and filterable, built to scale from 1 agent to a large fleet. Where the home answers "is anything wrong and what should I do", the Monitor answers **"show me every agent and let me find the broken ones."**

This is the proven network-monitoring IA (overview → device list → device detail) applied to an agent fleet. It resolves the 50-agent spacing problem the home's card grid could not: a monitor is *built* for many entities.

**The home-user throughline holds.** One healthy agent renders one OK row — useful, not empty. An org watching 50 gets the same page, same engine, scaled by the roster.

**PR-B scope (this spec):** the Monitor page — enumerate, status-rank, filter, and drill. Near-term liveness is **periodic static re-render** plus a visible freshness stamp; live-served polling/SSE is **explicitly deferred** (§9, MUST 13). No management/write actions.

---

## 1. URL surface + navigation

| Path | Serves |
|------|--------|
| `GET /monitor` / `monitor.html` | Fleet Monitor page |
| `GET /monitor?status=<ok\|warn\|error\|stale>` | Monitor pre-filtered to that status |
| `GET /monitor?view=<list\|cards>` | Monitor in the chosen view (see §4) |

**Query-value contract:** `status` and `view` values are **lowercase-canonical**. A value that is not a recognized lowercase token is **ignored** — the page renders unfiltered / in the default view, and the "arrived filtered" affordance does **not** show. Uppercase or mixed-case is not silently normalized; it is treated as unrecognized and ignored (keeps the deep-link contract unambiguous and the parser trivial).

**Navigation contract (the click-through flow):**
- **IN (from the home):** the console home's fleet-status summary counts (OK/WARN/ERROR/STALE, spec/52 §17) link to `monitor.html?status=<s>` (lowercase). Arriving with a recognized `status` pre-applies that filter and shows a dismissible "arrived filtered to <STATUS>" affordance.
- **OUT (to detail):** every agent entity (row or card) is a link to the per-agent detail page (`agent-detail.html?agent=<id>` / `GET /agent/<id>`, spec for #637). The whole row/card is the hit target.
- **Back:** a breadcrumb / tab link back to the console home (`index.html`).

## 2. The monitored-entity model

Every agent discovered in the fleet is one monitored entity. Discovery is **spec/51 `AgentRegistryBackend.list_agents()`** (the `model.md`-present predicate). An agent whose `model.md` is absent/unreadable/unparseable at discovery is **not enumerated** per spec/51 — it is not a Monitor row (see §7 for the distinction from row-degrade). Each enumerated entity carries:

| Field | Source |
|-------|--------|
| **status** | the SHARED `status_for_agent()` (spec/52 §17.1) — see §3 |
| name / id | AgentRegistryBackend |
| model | `model.md` resolved model |
| health | FleetHealth per-agent composite (spec/53) |
| errors (24h) | LogBackend run records, `status == "error"` in the **error window** |
| failures (7d) | LogBackend reliability failure markers (spec/52 §2.3) over the **failure window** |
| 7d cost | cost aggregate (spec/09 posture) |
| last-run | most recent primary run timestamp |
| sparkline | see §2.2 |

### 2.1 Status windows (pinned)

Three windows, each fixed, non-overlapping in meaning:

| Window | Value | Feeds |
|--------|-------|-------|
| **error window** | 24h | the `errors (24h)` column AND `status_for_agent()`'s error-rate input |
| **failure window** | 7d | the `failures (7d)` column (display only; not a `status_for_agent()` input in PR-B) |
| **staleness window** | 24h | STALE derivation in `status_for_agent()` (no primary run within 24h) |

Only the **error window (24h)** and **staleness window (24h)** feed status; the failure column is display context. All three are the named parameters of `status_for_agent()` / the loader (operator-tunable per D12, §3).

### 2.2 Sparkline input (pinned)

The per-entity sparkline reads the agent's **daily cost series** — `CostTrendPoint.daily_series` (spec/52; ISO-day → USD pairs, ascending, **sparse: missing days omitted, not zero-filled**), sliced to the **last 7 days**. It is a cost sparkline (not activity). A degraded cost read (spec/09) renders the sparkline as a degraded marker, not a misleading flat line. (This pins the `daily_series` shape the Monitor consumes; spec/52's field definition is the source.)

## 3. Status derivation — reuse the same snapshot, never re-derive

**The Monitor MUST derive per-agent status through the SAME `status_for_agent()` function the home uses (spec/52 §17.1), from the SAME inputs.** The consistency guarantee is not just "same computation" — it is **same loader snapshot**: same fleet enumeration, same `today`/`now`, same run/cost reads, same thresholds and windows. The home's "3 ERROR" summary and the Monitor's ERROR count are equal because they are the same function over the same snapshot.

- Status set: **OK / WARN / ERROR / STALE**, precedence **ERROR > STALE > WARN > OK** (uppercase canonical enum; the `?status=` query uses the lowercase token, §1).
- **STALE** = no primary run within the staleness window (default 24h) — the agent-equivalent of a device going unreachable.
- Window + error-rate thresholds are the **named parameters** of `status_for_agent()` (spec/52 §17.1). Operator-tunability (D12) = passing different arguments, never forking the function.

## 4. Presentation — dual view, problems-first

- **Dual view (D11):** a **List ⇄ Cards** toggle. **List is the default.** Persistence contract: **`?view=` in the URL wins**; otherwise `localStorage["fleet-monitor.view"]`; otherwise `list`. An invalid/absent value falls back to `list`. The toggle writes `localStorage["fleet-monitor.view"]` so the choice survives re-render. Cards keeps the status-bordered cards (border-color-by-status is the NMS signal); List is dense/sortable for scale.
- **Problems-first ordering (default):** ERROR → STALE → WARN → OK; stable secondary sort by name. Broken agents surface without scrolling.
- **Filter / sort / search:** filter by status (the summary bar doubles as filters) and model; free-text name/id search; sort by any column (health, cost, errors, last-run).
- **Status summary bar:** OK/WARN/ERROR/STALE counts, each clickable to filter. These counts MUST equal the home's fleet-status summary for the same snapshot (§3, MUST 12).

## 5. Liveness / freshness

- A prominent **"updated X ago"** stamp is always visible, plus the **status windows** in effect (errors 24h, failures 7d, stale 24h).
- Near-term refresh = **periodic static re-render** (existing dashboard cadence) + a client meta/JS **auto-reload of the static page** (a full page refresh on a timer — NOT background data polling). The freshness stamp keeps page-staleness honest.
- **Live-served polling / SSE / fetch is DEFERRED and MUST NOT be built in PR-B (§9, MUST 13).**

## 6. Composition — reuse the panel registry (amends spec/52 §16)

The Monitor page composes through the **panel registry (spec/52 §16)** — the same registry + layout engine + `PanelContext` loader + per-panel fail-soft, not a parallel one. This spec **amends spec/52 §16's slot set** to add two monitor slots: **`monitor-summary`** (the status-count bar) and **`monitor-roster`** (the entity list/cards). The existing slot enum `status | act | explore | agent-tab` becomes `status | act | explore | agent-tab | monitor-summary | monitor-roster`. A future monitor panel (per-model rollup, alert timeline) registers into a monitor slot without rewriting the page. (Decision: ONE registry extended, not a separate `MonitorPanelRegistry` — one composition engine keeps the home/monitor consistency and the D8 expandability ethos.)

## 7. Fail-soft / degraded posture

- **Discovery failure ≠ row-degrade.** An agent that spec/51 cannot enumerate (absent/unreadable `model.md`) is not a row — it never appears (this is spec/51's contract, not a Monitor degrade). A doctor/registry-reconcile surface (spec/51) is where un-enumerable agents are noticed, not the Monitor roster.
- **Per-entity fail-soft (post-discovery):** for an ENUMERATED agent, a missing/unreadable **metric/cost/health/run** read degrades only that row/card (a "degraded" marker in place of the affected metrics), never the whole page — the per-panel fail-soft posture (spec/52 §16, MUST 11) at row granularity.
- **Cost-read degraded:** reuse spec/09 — a degraded cost read surfaces the non-blocking "data may be incomplete" banner; affected cells + sparkline show the degraded marker; the page still renders.
- **Empty fleet:** zero enumerated agents renders a clean empty state ("no agents found — add one with `atomic-agents init`"), not an error.

## 8. Implementer Contract (MUSTs)

1. **MUST** serve the Monitor at `monitor.html` (and `/monitor`) and enumerate every fleet agent via spec/51 `list_agents()` as one monitored entity.
2. **MUST** derive each entity's status through the SHARED `status_for_agent()` (spec/52 §17.1) from the SAME loader snapshot as the home — no parallel status logic; set OK/WARN/ERROR/STALE, precedence ERROR > STALE > WARN > OK.
3. **MUST** default to **problems-first** ordering (ERROR → STALE → WARN → OK, stable secondary sort).
4. **MUST** provide a **List (default) ⇄ Cards** toggle with the §4 persistence contract (`?view=` wins → `localStorage["fleet-monitor.view"]` → `list`; invalid → `list`).
5. **MUST** render a status-summary bar (OK/WARN/ERROR/STALE counts) that doubles as a filter, and **MUST** pre-apply the filter when arriving with a recognized lowercase `?status=<s>`, with the "arrived filtered" affordance; an unrecognized value is ignored (no filter, no affordance).
6. **MUST** provide filter (status, model), free-text search (name/id), and column sort.
7. **MUST** link every entity (row and card) to the per-agent detail page (`agent-detail.html?agent=<id>`); the entity is the hit target.
8. **MUST** display a visible freshness stamp ("updated X ago") and the status windows in effect (§2.1).
9. **MUST** show the per-entity columns: status, name, model, health, errors (24h), failures (7d), 7d cost, last-run, sparkline (§2.2).
10. **MUST** be per-entity fail-soft for ENUMERATED agents (one agent's degraded read degrades only that row, never the page); cost-read degraded raises the spec/09 banner. Discovery-unenumerable agents are not rows (§7).
11. **MUST** be pure-compute with **zero new LLM spend** on the Monitor render path (no LLMBackend constructed).
12. **MUST** keep the Monitor's status counts equal to the home's fleet-status summary for the same snapshot (the §3 shared-derivation-and-snapshot guarantee) — a conformance test renders both from one fixture and asserts equality.
13. **MUST NOT** implement live-served polling / SSE / background fetch in PR-B; freshness is periodic static re-render + a full-page auto-reload only (§5, §9).

**Operator-tunability (D12):** the staleness/error windows + thresholds are the named parameters of `status_for_agent()` / the loader; the Monitor passes them through and never forks the derivation (MUST 2 + MUST 12).

## 9. Deferred (to serve layer / later PRs)

- **Live polling / SSE freshness** — needs the serve runtime; PR-B is periodic static re-render + full-page auto-reload (§5, MUST 13).
- **Management/write actions** on an entity (assign owner, set cap, swap model) — the observe→manage seam; gated by Principal (#556) + Mandate/Policy when built.
- **Alerting / notification channels** for status transitions — a future monitor-slot panel.
- **Per-model / per-owner rollup, delegation/anomaly panels** — future registered monitor-slot panels (§6).

## 10. Conformance coverage

Each MUST ships parametrized conformance coverage in `tests/test_dashboard_monitor.py`. Strip-RED negative controls are REQUIRED for the MUSTs marked ✱ (the ones where a silent regression would be invisible); the rest get positive-signal + boundary tests.

| MUST | Test (indicative) | strip-RED |
|------|-------------------|-----------|
| 1 | `test_monitor_enumerates_all_fleet_agents` | boundary (empty fleet) |
| 2 ✱ | `test_monitor_status_uses_shared_status_for_agent` | ✱ divergent local status impl fails |
| 3 | `test_monitor_default_order_is_problems_first` | order-inversion boundary |
| 4 ✱ | `test_monitor_view_toggle_persistence` | ✱ `?view=` precedence over localStorage; invalid → list |
| 5 ✱ | `test_monitor_status_query_preapplies_filter` + `test_monitor_invalid_status_ignored` | ✱ uppercase/garbage status → no filter + no banner |
| 6 | `test_monitor_filter_sort_search` | empty-result boundary |
| 7 ✱ | `test_monitor_entity_links_to_detail` | ✱ each entity href = `agent-detail.html?agent=<id>` |
| 8 | `test_monitor_freshness_stamp_and_windows` | — |
| 9 | `test_monitor_entity_columns_present` | — |
| 10 ✱ | `test_monitor_one_agent_degraded_degrades_only_that_row` + `test_monitor_cost_degraded_banner` | ✱ + `test_monitor_unenumerable_agent_is_not_a_row` (spec/51 boundary) |
| 11 ✱ | `test_monitor_no_llm_spend_on_render` | ✱ construction-time no-LLM assertion |
| 12 ✱ | `test_monitor_status_counts_equal_home_summary` | ✱ divergent window/snapshot fails |
| 13 ✱ | `test_monitor_render_has_no_polling` | ✱ no SSE/fetch/poll in the rendered page |

## 11. Design contract

Design-review-gated against the approved mockup **`variant-B7-monitor.html`** (designs/fleet-console-20260623/, Dan-approved 2026-07-05): the B7 dark-teal cockpit palette, List-default dual view, the OK/WARN/ERROR/STALE summary-bar filters, the "arrived filtered" banner, problems-first rows, cost sparklines, entity → detail links, breadcrumb home. The rendered page MUST match it.

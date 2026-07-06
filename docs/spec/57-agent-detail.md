# spec/57 — Per-Agent Detail Cockpit (DRAFT)

**Status:** DRAFT
**Issue:** #637 (Fleet Console rebuild PR-C: the per-agent detail page) + #684 (restore the Dreaming view)
**Depends on:** spec/52 (panel registry §16, shared `status_for_agent()` §17.1, layered rec tags §17.3), spec/56 (Fleet Monitor — the detail is reached from it), spec/53 (FleetHealth), spec/51 (AgentRegistryBackend governance record), spec/22 (LogBackend RunRecord), spec/09 (cost-read posture)
**Supersedes:** the *content* of the current per-agent `render_agent()` → `<agent>/dashboard.html` (the Caldwell-style dashboard). The path and route are KEPT (see §1 migration); the page content becomes the B7 detail cockpit.

---

## Overview

The **Per-Agent Detail Cockpit** is the third console surface (Cockpit home → Fleet Monitor → **detail**). It is the operator's **device page**: one agent's full telemetry, reached by clicking an entity in the Fleet Monitor (#653). It adopts the **Fable "Briefing" layout** (`variant-B7-agent-detail.html`, maintainer's preference) in the B7 dark-teal palette — banner, governance block, telemetry top-tabs — and **restores the Dreaming view** (#684) the cockpit rebuild dropped.

**The home-user throughline holds.** One agent, or one of fifty — same page, same engine, reached the same way.

**PR-C scope:** the detail page — banner + governance + telemetry tabs (incl. Dreaming). Observe-only; no management/write actions.

---

## 1. URL surface, routing + migration (pinned)

The detail is rendered **per agent** (one page per agent — scales, static-friendly). To avoid a dead link from the Monitor and to preserve existing consumers, ONE canonical mechanism:

- **The detail artifact IS `<agents_root>/<agent>/dashboard.html`** — the same path `render_agent()` writes today. PR-C replaces its *content* with the B7 detail cockpit; the path is unchanged (**migration**: existing consumers — `render_all()["per_agent"]`, the served `/agents/<name>` route, cost-table links, existing tests — keep working against the same path).
- **The served route `/agents/<name>` continues to serve `<agent>/dashboard.html`** (unchanged).
- **The Monitor's `agent-detail.html?agent=<id>` link** (shipped in #653) resolves via a generated static resolver page **`_dashboard/agent-detail.html`**: it reads the `agent` query value, validates it against the known agent set, and redirects to `../<encoded-agent>/dashboard.html`. This works for BOTH `file://` (a home user opening the file) and served access — `serve.py` mapping alone cannot help `file://`, so the static resolver is required. The served layer MAY additionally map `/agent/<id>` → the same page.

**Navigation:** a breadcrumb **"← Fleet Monitor"** + the standard top tab-nav (Console | Monitor | Cost | Activity | Quality | Memory) for cross-surface movement, and a Console-home link.

## 2. Layout — Fable "Briefing", B7 palette

Adapt `variant-B7-agent-detail.html` into the shipped B7 tokens:

1. **Banner** — name, id, resolved model (pill), current **status** (OK/WARN/ERROR/STALE via the SHARED `status_for_agent()`, §5), last-run, and the **FleetHealth composite** (0-100 — the SAME value the Monitor shows, never `×100`).
2. **Governance block** — the spec/51 governance record: owner, permission tier, customer-data, writes-to-SoR, lifecycle status, review. All five governance states (PRESENT_VALID / INVALID / INCOMPLETE / NO_BLOCK / ABSENT) surfaced honestly; a missing/broken block shows a "governance gap" affordance, not a blank.
3. **Recommendations zone** — a **standalone zone rendered between the governance block and the telemetry tabs**, always visible. When the engine produces no recommendations for this agent, an empty-state placeholder ("No recommendations right now.") is shown — not an absent section. Uses B7 dark-teal `zone-label` + `rec-panel` styling. Recommendations are NOT placed inside the Overview tab.
4. **Top-tabs** — the telemetry sections (§3).

## 3. Telemetry tabs — panel-registry `agent-tab` slot

Composed via a new agent-detail composition entry point (MUST 3): the registry's `agent-tab` slot (spec/52 §16) + a `compose_agent_detail()` (mirroring `compose()` / `compose_monitor()`) and an **agent-scoped `PanelContext`** carrying `agent_id`, the AgentRef/governance record, per-agent health, and the agent's runs / cost / evals / goals / dream summaries (one pre-load).

| Tab | Content | Availability (capability gate, MUST 4) |
|-----|---------|-----------------------------------------|
| **Overview** | health composite + 3-axis breakdown, recent activity | always |
| **Cost** | 7d/30d spend, daily series, model mix, cost-guardrail refusals | always |
| **Activity** | run history, tool calls, delegations, helper provenance | always |
| **Quality** | eval results/scores (JudgeBackend) | **available** when an eval surface exists; **empty state** ("no evals configured") when zero evals |
| **Memory** | notes / wiki / recall stats | available when a memory surface exists |
| **Goals** | goal progress / outcomes | **available** only when GoalBackend / a goal file exists; empty when zero active outcomes |
| **Dreaming** | dream / consolidation observability (§4) | **available** only when `<agent>/dreams/*/manifest.json` exists (gated OUT otherwise — no empty Dreaming tab) |
| **Efficiency** | derived efficiency (cost-per-outcome, etc.) | always |

**Capability-gating rule (coherent):** a tab whose backing *backend/artifact surface* is absent is **omitted**; a tab whose surface exists but currently has *zero items* renders an **empty state**. (Dreaming/Goals gate on artifact presence; Quality/Memory show empty-state when the surface exists but is empty.)

**Layered recommendation tags (spec/52 §17.3):** recommendations on the detail carry the same tags as the home/Monitor — `savings_cost` → "→ Cost · +N pts"; `governance`/`quality_report` → "advisory · not scored".

## 4. The Dreaming tab (#684) — grounded in the real artifacts

Dream/consolidation observability, **observe-only** (reads artifacts; never triggers a dream — zero new LLM spend). Renders what actually exists on disk:

- **Source:** `<agent>/dreams/drm_*/manifest.json` (+ the run's `report.md` and `memory/`). The manifest carries: `status`, `model`, `instructions`, `inputs`, output counts (`consolidated`, `promoted`, `marked_stale`), token/cost totals, `started_at` / `ended_at`, `error`, `applied_at`, `archived_path`.
- **Recent dream runs** — a table of recent manifests: when (`started_at`/`ended_at`), model, status (+ `error` if failed), notes `consolidated` / `promoted` / `marked_stale`, tokens + cost, whether `applied_at` is set.
- **Last run summary** — the most recent run's `report.md` summary (what it consolidated), and its `applied_at` / `archived_path` state.
- **DEFERRED (not on disk today):** dream *cadence* / next-scheduled-run / run-kind taxonomy / consolidation *candidates* — there is no scheduler artifact for these. The tab renders only the manifest/report facts; a future scheduler artifact would add cadence/next-run (tracked, not invented here). (The Fable mockup showed cadence + candidates; PR-C renders the real fields and omits the invented ones until an artifact backs them.)

## 5. Status + health consistency

The banner's status + health MUST come from the SAME shared `status_for_agent()` (spec/52 §17.1) + FleetHealth (spec/53) the Monitor uses. Mechanically pinned (MUST 5):
- when detail pages are rendered **during `render_all()`**, they MUST receive the same `console_data` / `today` / `now` / FleetHealth inputs threaded to the Monitor — so the Monitor row and the detail banner for the same agent are identical by construction;
- a **standalone served** detail render MAY build a fresh, self-consistent snapshot (its own `now`), but MUST use the same derivation functions.
Health renders as a 0-100 integer (a conformance test guards the `×100` display-bug class).

## 6. Fail-soft / degraded posture

- **Per-tab fail-soft:** a tab whose read fails/degrades shows a degraded marker for THAT tab only; the banner + other tabs still render (spec/52 §16 MUST 11 at tab granularity).
- **Cost-read degraded:** spec/09 banner + degraded cells.
- **Unknown / un-enumerable agent** (spec/51): a clean "agent not found" state (and the resolver rejects an unknown `?agent=`), not a crash.
- **Real fields only:** metrics read real fields — no phantom-`getattr`-default-0 (the #653 bug class); a conformance test asserts a known-nonzero metric renders nonzero.

## 7. Implementer Contract (MUSTs)

1. **MUST** render the detail as `<agent>/dashboard.html` (content replaced, path kept) AND generate the static `_dashboard/agent-detail.html` resolver so the Monitor's `agent-detail.html?agent=<id>` link resolves for both `file://` and served access — no dead link.
2. **MUST** preserve backward compatibility: `render_all()["per_agent"]`, the served `/agents/<name>` route, and existing `<agent>/dashboard.html` consumers keep working against the same path.
3. **MUST** render the Fable-layout banner (name/id/model/status/last-run/health-0-100) + the spec/51 governance block with all five governance states surfaced honestly.
4. **MUST** compose the telemetry tabs via a `compose_agent_detail()` over the registry `agent-tab` slot, with an agent-scoped `PanelContext` (single pre-load); capability-gate per §3 (omit absent-surface tabs; empty-state zero-item surfaces).
5. **MUST** derive banner status + health via the SHARED `status_for_agent()` + FleetHealth, equal to the Monitor's for the same agent + snapshot when co-rendered in `render_all()`; health renders 0-100 (test guards `×100`).
6. **MUST** restore the **Dreaming** tab (#684) rendering the REAL `dreams/*/manifest.json` + `report.md` fields (§4); cadence/schedule/candidates omitted until a backing artifact exists; observe-only, gated on manifest presence.
7. **MUST** carry the layered recommendation tags (spec/52 §17.3) consistent with the home/Monitor.
8. **MUST** be per-tab fail-soft; cost-read degraded raises the spec/09 banner; unknown agent → clean not-found (resolver rejects unknown `?agent=`).
9. **MUST** read real metric fields (no phantom-field silent-zero) — a conformance test asserts a known-nonzero metric renders nonzero.
10. **MUST** be pure-compute, **zero new LLM spend** (no LLMBackend constructed; Dreaming observe-only).

## 8. Conformance coverage

`tests/test_dashboard_agent_detail.py`; strip-RED (✱) for the silent-regression-prone MUSTs:

| MUST | Test (indicative) | strip-RED |
|------|-------------------|-----------|
| 1 | `test_detail_written_to_dashboard_html` + `test_agent_detail_resolver_redirects` | ✱ resolver rejects unknown agent |
| 2 | `test_detail_backward_compat_paths` (per_agent map + /agents/<name>) | — |
| 3 | `test_detail_banner_and_five_governance_states` | — |
| 4 | `test_detail_tabs_via_compose_agent_detail` + `test_detail_dreaming_gated` + `test_detail_goals_gated` | ✱ non-dreaming agent shows no Dreaming tab |
| 5 | `test_detail_status_health_matches_monitor_in_render_all` + `test_detail_health_0_100_not_x100` | ✱ divergent snapshot fails |
| 6 | `test_detail_dreaming_renders_real_manifest_fields` | ✱ invented field (cadence) absent |
| 7 | `test_detail_layered_rec_tags` | ✱ governance rec must not get an axis tag |
| 8 | `test_detail_one_tab_degraded_isolates` + `test_detail_unknown_agent_not_found` | ✱ |
| 9 | `test_detail_metrics_use_real_fields` | ✱ phantom-field → nonzero metric renders nonzero |
| 10 | `test_detail_no_llm_spend` (patch all concrete LLM ctors) | ✱ |

## 9. Design contract

Design-review-gated against **`variant-B7-agent-detail.html`** (designs/fleet-console-20260623/, Dan-approved 2026-07-05): the Fable "Briefing" layout in the B7 palette — banner, governance block, the top-tab set including the diamond-marked **Dreaming** tab, layered rec tags, breadcrumb to the Monitor. The rendered page MUST match it (render vs mockup, not test-green-alone — the gate #614–616 skipped and #635/#653 restored). NOTE the one intentional divergence: the Dreaming tab renders the real manifest/report fields, not the mockup's invented cadence/candidates (§4).

## 10. Deferred

- Management/write actions (change model, set cap, assign owner) — observe→manage seam; Principal/Mandate-gated.
- Delegation-tree / OTel-trace (#341) / conversation-history (#535) deep views — future `agent-tab` panels.
- Dream cadence / next-run / candidates — needs a scheduler artifact (§4).
- Live refresh — periodic re-render like the Monitor (spec/56 §5).

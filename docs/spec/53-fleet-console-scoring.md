# spec/53 — Fleet Console: Health Scoring Engine (DRAFT)

**Status:** DRAFT
**PR:** Console PR2 (#615), Console PR3 (#616 — scoring-core extraction)
**Depends on:** spec/52 (Fleet Console PR1), spec/09 (LogBackend), spec/46 (EmbeddingBackend)
**Module:** `atomic_agents/advisor/` (pure-compute; ZERO LLM spend)
**Recommendations contract:** see spec/54

---

## 1. Purpose

The Fleet Health Scoring Engine adds a quantitative 0-100 score to the Fleet Console (spec/52). It provides operators with a single headline number — decomposed into three axis sub-scores — that summarizes fleet health at a glance.

The score is computed entirely from existing JSONL data that the framework already writes. It requires no new storage, no new backend protocol, and makes zero LLM calls.

---

## 2. Architecture: pure-compute, not a backend protocol

The advisor is a pure-compute module (`atomic_agents/advisor/`). It reads through existing backend protocols (LogBackend via `dashboard.costs._load_runs_with_degraded`, eval JSONL via a direct reader) and computes scores as a stateless function call.

**Import discipline (MUST 10):** `advisor/score.py` imports only:
- `stdlib`
- `atomic_agents.dashboard._reliability` (shared reliability computations)
- `atomic_agents.dashboard.costs` (`_load_runs_with_degraded`, `discover_agents`)
- `atomic_agents._costs` (`PRICING`)
- `.targets` (`FleetTargets`, constants)

The advisor's OWN code never directly imports `agent.py`, `eval.py`, `tuning.py`, `dream.py`, `dashboard.attention`, or `dashboard.render`.

**NOTE — what the no-LLM guarantee is, and is not:** the load-bearing guarantee is *no LLMBackend is ever constructed and no LLM call is ever made on any advisor code path* (zero LLM spend), enforced by the construction guard in `tests/advisor/conftest.py`. It is **not** a claim of `sys.modules` isolation: importing any `atomic_agents` submodule runs the package `__init__` (`atomic_agents/__init__.py` does `from .agent import AtomicAgent` and `from .dream import ...`), so `agent.py`/`eval.py`/`dream.py` are transitively loaded as module **definitions** — exactly as the pre-existing `dashboard.costs` dependency already does. No LLM is constructed at import; the guarantee is about spend, not module loading. The source-level grep below checks the advisor's own imports, not the process import graph.

---

## 3. Score architecture

### 3.1 Three-axis decomposition

| Axis | Metrics |
|------|---------|
| Cost | `spend_vs_trend` (cost-anomaly detection) |
| Quality | `pass_rate`, `hard_fail_rate` |
| Reliability | `error_rate`, `blocked_rate`, `skipped_rate` |

**#687 amendment (ruled 2026-07-05, MUST 14):** cost-**optimization** metrics — `cheaper_model_share` AND `tokens_per_output` — are **NOT health metrics** (see §3.6). "Could be cheaper / could be terser" is advice, not a health failure, so neither is scored into the Cost axis, the composite, the critical cap, or `status_for_agent()`. The Cost *health* axis measures cost **anomalies** (`spend_vs_trend` — an unexpected spend spike is a real failure signal) only; the optimization metrics feed recommendations (spec/54).

### 3.2 Scoring curve (MUST 1)

Each metric maps to a 0-100 score via a **piecewise plateau-at-target curve**:

- `direction='higher'`: score = 100 when `value >= target - band`; linear decay to 0 at `floor`
- `direction='lower'`: score = 100 when `value <= target + band`; linear decay to 0 at `floor`
- Output clamped to [0, 100]. Values below `floor` clamp to 0 (no negative scores).

### 3.3 Band derivation

| Score | Band |
|-------|------|
| ≥ 80 | green |
| ≥ 60 | amber |
| < 60 | red |

Constants `BAND_GREEN_MIN = 80`, `BAND_AMBER_MIN = 60` are normative.

The band is derived from the **canonical display integer** `int(round(raw_composite))`
— rounded ONCE off the raw capped float, AFTER the critical-cap override (see §3.5),
never off a `round(composite, 1)` intermediate (the #623 double-round). This is the
same integer render.py displays, so the displayed number and its band color always
agree across the rounding boundary. Worked examples (round-half-to-even at the .5
boundary): raw `79.6` → `int(round(79.6))=80` → green (a `round(79.6, 1)=79.6`
float-band would have read amber); raw `79.49` → `79` → amber; raw `79.5` → `80` →
green; raw `59.49` → `59` → red; raw `59.5` → `60` → amber. Applies to BOTH the
per-agent composite (`composite_display`) and the fleet headline
(`fleet_composite_display` / `worst_agent_composite_display`). See MUST 11.

### 3.4 Composite roll-up (MUST 2, MUST 3)

Per-axis sub-scores roll up to a per-agent composite via weighted mean over **present** (non-None) sub-scores:

```
composite = sum(weight[axis] * sub_score[axis] for axis in present) / sum(weight[axis] for axis in present)
```

Re-weighting applies automatically when an axis has no data (absent axes are excluded, not scored as 0). Default weights: 1/3 each. Composite is clamped to [0, 100] (MUST 3).

### 3.5 Critical cap (MUST 4)

Applied **POST-computation** (after weighted mean). Two triggers, either of which forces `composite = min(composite, CRITICAL_COMPOSITE_CAP=60)` and `band = 'red'`:
- **Axis-level:** any axis sub-score < `CRITICAL_SUBSCORE_THRESHOLD` (30).
- **Metric-level:** any individual metric score < `CRITICAL_SUBSCORE_THRESHOLD` (30), even when its axis MEAN dilutes it above threshold. Each axis sub-score is the unweighted mean of its metrics, so a single catastrophic metric (e.g. `error_rate=0.90` → metric score 0, with healthy siblings averaging the reliability axis to ~66) would otherwise read green. ANY single critical signal must force the cap; averaging cannot hide it. (`_compute_composite` takes the flat list of every present metric score; when that list is omitted — direct unit tests of the composite math — only the axis-level check runs.)
- The cap fires even when the pre-cap composite would be amber/green. Per-axis chips still show their own uncapped values — decomposition stays visible (MUST 5).

The composite is **banded on its canonical display integer** `int(round(raw_composite))` — derived from the raw capped float AFTER this critical-cap override (§3.3, MUST 11), never off a `round(composite, 1)` intermediate — so the headline number and its color always agree across the rounding boundary.

### 3.6 Cost-optimization is advisory, not health (MUST 14, #687)

**Ruled 2026-07-05.** Cost-**optimization** signals — "this agent could use a cheaper model" (`cheaper_model_share`) or "could produce terser output" (`tokens_per_output`) — are **recommendations**, not health failures. Conflating optimization with health made premium-model fleets read all-red: an agent running entirely on Opus/Sonnet with zero errors scored `cheaper_model_share = 0`, which (via the metric-level critical cap, §3.5) forced `composite ≤ 60`, `band = red`, `capped_by_axis = "metric"`, hence `status_for_agent() = ERROR` (spec/52 §17.1) — every row red, no signal. `tokens_per_output` has the identical shape (a legitimately verbose agent > ~3680 output tokens/run scores < 30 → same cap → ERROR).

**MUST 14:** the cost-optimization metrics `cheaper_model_share` and `tokens_per_output` MUST NOT participate in the health score in any way — MUST NOT be scored into the Cost sub-score, contribute to the composite, lower the displayed health number, fire the axis- or metric-level critical cap (§3.5), set `capped_by_axis`, or influence `status_for_agent()`. The Cost **health** axis is scored from `spend_vs_trend` alone (cost-anomaly detection — a spend spike is a genuine failure). Health/status reflect FAILURES and ANOMALIES (errors, blocked, quality, staleness, spend spikes); optimization is surfaced as recommendations (spec/54).

**Build sites (Codex-enumerated — remove both metrics from the health path):** `advisor/targets.py` `_DEFAULT_AXES` cost block (drop both metric defaults; cost-axis weight normalization already reweights over present metrics); `advisor/score.py` `_score_cost_axis` (drop the scored rows + `metric_scores.append` for both, plus their WoW / no-data / degraded row handling — keep computing `cheaper_model_share` as a VALUE for the recommendation path) and the metric-level-cap input list in `_compute_composite`; `dashboard/render.py` `_SCORECARD_DISPLAY_ORDER` (remove both from the Runtime-Health scorecard — they are not health rows). `status_for_agent()` (`_status.py`) and the fleet critical cap (`compute_fleet_health`, §7) then fix themselves upstream. Update the affected tests.

**targets.md back-compat (fail-soft):** an operator whose `targets.md` still carries `scoring.axes.cost.metrics.cheaper_model_share` / `tokens_per_output` overrides after removal — the override is **ignored fail-soft** (the parser only iterates metrics present in `_DEFAULT_AXES`); it MUST NOT error and MUST NOT affect `used_defaults`.

**Recommendation coupling (spec/54 + spec/52 + #689):** removing `cheaper_model_share` from the composite makes a model-swap's point-impact counterfactual (`projected_points_delta`, spec/54 §7) `≈ 0.0` — the swap no longer moves the score (`_compute_point_impact` stays fail-soft, returns ~0). The `savings_cost` recommendation STILL fires — it fires on its dollar delta (`projected_usd_delta`), not on points (recommend.py:491). To keep the suggestion STRONG (the point of the ruling), **`recommend_fleet()` MUST fall back to ranking `savings_cost` recs by `abs(projected_usd_delta)` when point-impact is 0/None** — IN #687's BUILD SCOPE (else a real-dollar savings rec is buried behind a 0-point rank). The rec DISPLAY refinement (surface `$/mo saved` first, drop the `→ Cost · +0 pts` tag) is #689. spec/54 §7 and spec/52 §17.3's "savings recs move the Cost axis / +N pts" language become stale — update them (savings recs are ranked/shown by `$ saved`; point-impact is normally 0 once optimization leaves health).

**Conformance (MUST 14 strip-RED):** a clean agent running 100% premium models (`cheaper_model_share = 0`) AND legitimately verbose (`tokens_per_output` above the old <30 floor), with healthy Quality + Reliability, MUST score amber/green and status OK/WARN — NOT ERROR, NOT `capped_by_axis` from cost. Stripping the removal (re-adding EITHER metric to the Cost-axis scored/cap inputs) MUST turn that agent red — proving the guard is load-bearing.

---

## 4. No-data and degraded postures (MUST 7, MUST 8)

| Condition | Sub-score | Effect on composite |
|-----------|-----------|---------------------|
| No runs in window | `None` | Excluded from composite (re-weighted) |
| Zero evals (or all `judge_error`) | `None` | Excluded |
| LogBackend read degraded | `None` | Excluded (flagged `*_degraded=True`) |
| Degraded read | `ScorecardRow.status = 'degraded'` | Excluded |

**MUST NOT** score an absent or degraded axis as 0.0. A fleet with no eval data is not a poor-quality fleet — it's a fleet with insufficient data.

---

## 5. Axis details

### 5.1 Cost axis

One health metric — cost-**anomaly** detection (the optimization metrics below are NOT scored into health; §3.6):
- **spend_vs_trend**: `current_30d_spend / prior_30d_spend - 1.0`. Requires the prior-30d window non-empty (`prior_spend > 0`); an empty recent window yields ratio `-1.0` (maximal-improvement) and is still scored. The recent `[today-30, today]` and prior `[today-61, today-31]` windows are EQUAL LENGTH (31 inclusive days each) and non-overlapping, so a flat-spend fleet reports `0.0`, not a one-day bias. An unexpected spend spike is a genuine failure signal (runaway loop, misconfig) — so this metric MAY fire the critical cap, unlike the optimization metrics.

**Optimization metrics — recommendation inputs, NOT health metrics (#687, §3.6):**
- **cheaper_model_share**: fraction of primary runs using a model with `output_rate < CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M` ($5.00). Strict less-than (ties are NOT cheap — fail-pessimistic). Unknown models are pessimistic (not cheap). Consumed ONLY by the `savings_cost` recommendation (spec/54).
- **tokens_per_output**: mean output tokens per completed primary run (completed + output_tokens > 0 only). A verbosity signal (may inform a future terseness recommendation).

Neither optimization metric is scored into the Cost axis, the composite, the critical cap, or `status_for_agent()` (§3.6, MUST 14).

### 5.2 Quality axis

Reads `verdict` field directly from `evals/runs/*.jsonl` — no EvalRunner import (MUST 10).

- **pass_rate**: `pass_count / (pass_count + fail_count)`. Denominator excludes `judge_error` (not scorable without a verdict). Zero scorable records → `quality_score = None`.
- **hard_fail_rate**: fraction of scorable evals with non-empty `hard_fails` list.

### 5.3 Reliability axis

Reuses `_compute_reliability()` from `dashboard/_reliability.py` (the shared neutral module extracted from attention.py). Denominator is primary runs only (not child/bookkeeping rows).

- **error_rate**: `status == 'error'`
- **blocked_rate**: single predicate — `status == 'lock_busy' OR extra.embed_batch_blocked == True` — counts once even when both flags present.
- **skipped_rate**: `status in {'skipped', 'deduped'}`

### 5.4 Work-type classification

**Provided-but-not-yet-wired (PR2):** `_classify_work_type` ships as a tested, deterministic primitive for the PR3 recommendations work ([#616](https://github.com/dep0we/atomic-agents-stack/issues/616)), which will annotate `cheaper_model_share` by work-type. PR2's `cheaper_model_share` computation does **not** consume it. Wiring tracked at [#620](https://github.com/dep0we/atomic-agents-stack/issues/620). Deterministic precedence ladder:
1. `parent_run_id` set → `'child'`
2. `trigger in {'cron', 'schedule', 'mcp', 'serve', 'queue'}` → `'coordinator'`
3. `extra.delegations` non-empty → `'coordinator'`
4. `extra.tool_calls` non-empty → `'tool-heavy'`
5. else → `'general'`

---

## 6. WoW (Week-over-Week) windows

- **Current 7d**: `[today − 6d, today]` (7 calendar days, inclusive both ends; `timedelta(days=6)`)
- **Prior 7d**: `[today − 13d, today − 7d]` (7 calendar days, inclusive both ends; strictly before current window — no boundary overlap, no gap)
- **WoW arrow**: `up | down | flat | None`. `None` when either window has no data. `flat` when delta within 1% threshold (metric-dependent). The displayed arrow is colored by **goodness**, not raw direction: for a `lower`-is-better metric a rising value renders red (bad) and a falling value green (good); for a `higher`-is-better metric the reverse (render.py `_wow_symbol`).
- Sliced from already-loaded 30d lists — no extra I/O. Both windows are inclusive-boundary date ranges of **equal length (7 inclusive days each)** by design (#623b): an earlier draft used `[today − 7d, today]` (8 inclusive days) for the current window against a 7-day prior, so a flat-spend fleet reported `spend_vs_trend = +14.3%` instead of `0.0`. Equalizing the windows removes that length bias. The 30d spend-vs-trend windows are likewise strictly non-overlapping (prior-30d ends `today − 31d`) AND equal length (31 inclusive days each, prior-30d starts `today − 61d`) so a busy boundary day is neither double-counted nor one-day-biased in the spend ratio. See MUST 12.
- **WoW color uses DEFAULT directions only (PR2 scope).** The arrow color above derives from each metric's default optimization direction, hard-coded in `render.py _render_health_band`. An operator who overrides a metric's `direction` in `targets.md` changes how the SCORE column is computed, but the WoW arrow COLOR still uses the default direction — the two can diverge for an overridden metric. Honoring a `targets.md` direction override in the WoW color is deferred to the recommendations PR (#616).

---

## 7. Fleet roll-up

```
fleet_composite = min(mean(agent_composites), worst_agent_composite)
```

The worst-agent composite is a **ceiling**, not a floor. This prevents 9 agents at 95 from hiding 1 agent at 15 in the fleet headline.

Agents with `None` composite (no data or fully degraded) are excluded from fleet roll-up.

**Fleet critical cap.** The fleet headline is forced red (and floored at `CRITICAL_COMPOSITE_CAP`) when ANY included agent is itself critical — keyed on the agent's own `capped_by_axis` flag, NOT on `worst_agent_composite < CRITICAL_SUBSCORE_THRESHOLD`. A capped agent's composite is already floored to `CRITICAL_COMPOSITE_CAP` (60), so it is never below the threshold (30); testing the post-cap composite would let a critical agent read amber/green at the fleet level while one of its axes is critical.

---

## 8. targets.md (operator-configurable)

`targets.md` sits in `agents_root/` (fleet root). Absent = all defaults (valid home-user state).

### 8.1 Schema

Single embedded YAML block under the `scoring:` root key (model.md-style — `yaml.safe_load` only):

```markdown
```yaml
scoring:
  weights:
    cost: 0.333
    quality: 0.333
    reliability: 0.334
  axes:
    reliability:
      metrics:
        error_rate:
          target: 0.0
          direction: lower
          band: 0.05
          floor: 0.40
```
```

The block above is illustrative (one metric shown). The authoritative default values for every metric live in `advisor/targets.py::_DEFAULT_AXES` (e.g. reliability `error_rate` floor `0.40`, `blocked_rate` floor `0.30`, `skipped_rate` floor `0.40`); a key omitted from `targets.md` falls back to that default.

### 8.2 targets.override.md

Optional companion file for deep-merge. Override values are recursively merged into the base `targets.md` result, skipping `null` (YAML `None`) override values (null = "keep base value").

### 8.3 Fail-soft parsing (MUST 6)

Per-key: a missing or malformed key falls back to the baked-in default for THAT key only. The rest of the file continues to parse. A whole-file parse failure (YAML syntax error, missing `scoring:` block) uses all baked-in defaults.

Domain (not just type) validation, each per-key fail-soft into the baked-in default plus a `used_defaults` entry:
- **weights:** each weight must be a finite, non-bool number in `[0, 1]`; any out-of-domain weight (negative, `> 1`, NaN/inf, or a YAML `true`/`false`) invalidates the vector → equal weights. A negative weight that happens to sum to `1.0` (`cost=.55, quality=.55, reliability=-.10`) is NOT a weighted mean and is rejected — the sum check alone misses it.
- **direction:** must be one of `{higher, lower}`; an out-of-enum string (operator typo) defaults that metric's direction (otherwise it silently inverts the curve).
- **band / span:** `band >= 0`, and the decay span must be positive — `floor < target - band` for higher-is-better, `floor > target + band` for lower-is-better. A non-positive span turns the curve into a cliff to 0; on a bad span the whole metric config defaults.

Invalid weight sums (weights don't sum to ~1.0) → fall back to equal weights.

**Normative split (what an operator may tune vs. what is fixed):** the per-metric default `target` / `band` / `floor` / `direction` values in `_DEFAULT_AXES` are **non-normative** — they are operator-tunable starting points. The scoring **formulas** — the curve (§3.2), the composite roll-up (§3.4), the critical cap (§3.5) — and the §9 constants are **normative**: a conforming implementation must compute them identically regardless of the tuned target values.

---

## 9. Normative constants

| Constant | Value | Meaning |
|----------|-------|---------|
| `CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M` | 5.0 | Strict less-than cutoff for "cheap" model |
| `CRITICAL_SUBSCORE_THRESHOLD` | 30 | Sub-score below this → critical-axis cap fires |
| `CRITICAL_COMPOSITE_CAP` | 60 | Maximum composite when critical axis present |
| `BAND_GREEN_MIN` | 80 | Minimum composite for green band |
| `BAND_AMBER_MIN` | 60 | Minimum composite for amber band |

---

## 10. Public API

```python
from atomic_agents.advisor import compute_fleet_health, FleetHealth, AgentHealth, ScorecardRow

fleet_health: FleetHealth = compute_fleet_health(agents_root, today=date.today())
```

`FleetHealth` fields: `agents`, `fleet_composite`, `fleet_composite_display`, `fleet_band`, `worst_agent`, `worst_agent_composite`, `worst_agent_composite_display`, `coverage_n`, `coverage_m`, `degraded`, `used_targets_defaults`. (`fleet_composite_display` / `worst_agent_composite_display` are the `int | None` canonical display integers — `int(round(raw_capped_float))`, MUST 11.)

`AgentHealth` fields: `agent`, `cost_score`, `quality_score`, `reliability_score`, `composite`, `composite_display`, `primary_model`, `band`, `cost_degraded`, `quality_degraded`, `reliability_degraded`, `degraded`, `axes_with_data`, `scorecard`, `capped_by_axis`. (`composite_display` is the `int | None` canonical display integer, MUST 11; `primary_model` is the agent's majority primary-run model — `str | None` — consumed by spec/54 recommendations.)

`ScorecardRow` fields: `metric`, `axis`, `value`, `target`, `status`, `score`, `wow`.

---

## 11. Render integration (Fleet Console)

The Fleet Health header band renders ABOVE the three axis trend panels in the console HTML output (`_render_console_template()`). It is populated by `_render_health_band(fleet_health)` in `render.py`.

`render_console()` calls `compute_fleet_health()` before `_render_console_template()` and stores the result in `ConsoleData.fleet_health`. A fail-soft try/except logs a warning and continues without the band if the advisor raises.

The band renders as: composite score, band color, sub-score chips (cost/quality/reliability), full scorecard table, WoW arrows, and a degraded/defaults banner when applicable.

---

## 12. Implementer Contract (MUST set)

| # | MUST | Test |
|---|------|------|
| 1 | Score curve: 100 at target, linear decay to 0 at floor, clamped [0,100] | `TestMapMetricToScore` |
| 2 | Composite: weighted mean over present (non-None) sub-scores only | `TestComputeComposite::test_absent_axis_excluded` |
| 3 | Composite clamped to [0, 100] | `TestCompositeClamping` |
| 4 | Critical-axis cap fires POST-computation; any sub-score < 30 → composite ≤ 60, band = red | `TestCriticalAxisCap` |
| 5 | Scorecard rows always emitted (decomposition always visible, even for no-data agents) | `TestDecompositionAlwaysVisible` |
| 6 | targets.md parsing is per-key fail-soft; absent file = all defaults; invalid YAML = all defaults | `TestTargetsParsing` |
| 7 | Zero-evals agent: quality_score = None (excluded from composite, not scored as 0) | `TestNoDataPosture` |
| 8 | Degraded LogBackend read: affected sub-score = None (excluded from composite, not scored as 0) | `TestDegradedReadPosture` |
| 9 | Cheap-model classification: strict less-than threshold; ties and unknown models = NOT cheap | `TestCheapModelClassification` |
| 10 | No LLMBackend is constructed and no LLM call is made on any advisor path (zero LLM spend); advisor's own code performs no direct `agent`/`eval`/`tuning`/`dream` import; the conftest construction guard enforces the spend guarantee at test time (NOT `sys.modules` isolation — see §2 NOTE) | `TestNoLLMEnforcement` |
| 11 | Display integer (`composite_display` / `fleet_composite_display`) is `int(round(raw_composite))` rounded ONCE off the raw capped float AFTER critical-cap override (never off the 1-decimal `round(v, 1)` float — the #623 fleet double-round); band is assigned from the display integer, not the raw float; render.py consumes the display integer directly (no `{:.0f}` on raw floats) (#623). Covered for BOTH the per-agent composite (`_compute_composite`) and the fleet-headline path (`compute_fleet_health`). | `TestRoundedBandConsistency`, `TestFleetDisplayIntegerBoundaries` (per-agent boundaries + `test_fleet_79_45_is_amber_display_79` / `test_fleet_59_45_is_red_display_59` drive `compute_fleet_health`) |
| 12 | Both 7d WoW windows are exactly 7 inclusive days each: current = [today−6, today], prior = [today−13, today−7] (#623) | `TestWoW7dEqualWindows` |
| 13 | `_score_agent_from_data` is a zero-disk-I/O pure function; takes pre-loaded run + eval records; counterfactual use with `dataclasses.replace` must not mutate originals | `TestScoreAgentFromDataPureCore` |
| 14 | Cost-optimization metrics (`cheaper_model_share`, `tokens_per_output`) do NOT participate in health: not scored into the Cost axis / composite / displayed number, never fire the critical cap or set `capped_by_axis`, never influence `status_for_agent()`; Cost health axis = `spend_vs_trend` only. A 100%-premium + verbose but otherwise-healthy agent is OK/WARN, never ERROR-from-cost. `savings_cost` recs still fire (on `$` delta) and rank by `abs(projected_usd_delta)` when point-impact is 0/None. Legacy `targets.md` overrides for the two metrics ignored fail-soft. (#687) | `TestCostOptimizationNotHealth` (strip-RED: re-adding either metric to the cap turns the healthy premium/verbose agent red) |

---

## 13. File map

```
atomic_agents/
  advisor/
    __init__.py          — public API re-exports
    score.py             — scoring engine (compute_fleet_health + helpers)
    targets.py           — targets.md parser + normative constants
  dashboard/
    _reliability.py      — shared reliability computations (new; extracted from attention.py)
    attention.py         — alert generation (imports from _reliability; adds ConsoleData.fleet_health)
    render.py            — _render_health_band() + render_console() integration

docs/spec/
  53-fleet-console-scoring.md    — this file
  54-fleet-console-recommendations.md — recommendations engine built on scoring core

tests/
  advisor/
    __init__.py
    conftest.py          — no-LLM enforcement guard (module-scoped autouse fixture)
    test_advisor_score.py — 97 tests covering all 10 MUSTs + strip-RED controls
```

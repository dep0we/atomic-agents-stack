# spec/54 — Fleet Console: Recommendations Engine (DRAFT)

**Status:** DRAFT
**PR:** Console PR3 (#616)
**Depends on:** spec/53 (Fleet Console Scoring), spec/52 (Fleet Console PR1)
**Module:** `atomic_agents/advisor/recommend.py` (pure-compute; ZERO LLM spend)
**Scoring contract:** see spec/53

---

## 1. Purpose

The Recommendations Engine adds an actionable "what to do next" layer on top of the Fleet Health Scoring Engine (spec/53). It reads the same pre-scored `AgentHealth` objects and the same JSONL run/eval records, then emits a ranked list of `Recommendation` objects — one per agent per actionable finding.

Three recommendation kinds ship in PR3:

| Kind | What it finds | What it suggests |
|------|---------------|------------------|
| `savings_cost` | Agent running an expensive model with proven eval quality headroom | Switch to a cheaper same-family sibling model |
| `quality_report` | Tuning report on disk has hard-fails or score below rubric threshold | Review the already-written quality report |
| `governance` | `governance.md` absent, missing YAML block, or has parse errors | Add or fix governance metadata |

No kind issues orders. All recs are OBSERVE-ONLY. The engine never writes to vault files, never calls an LLM, and never imports tuning infrastructure.

---

## 2. Import discipline (MUST 10)

`advisor/recommend.py` imports only:

- stdlib
- `atomic_agents._costs` (`PRICING`, `calc_cost` — the `CACHE_HIT_DISCOUNT` is applied transitively inside `calc_cost`, not imported directly)
- `atomic_agents.advisor.score` (`_EvalRecord`, `_score_agent_from_data`, `AgentHealth`, `RunRecord`, `compute_fleet_health`, …)
- `atomic_agents.advisor.targets` (`FleetTargets`, `RecommendationConfig`, `_DEFAULT_SAME_FAMILY_DOWNGRADE`, `parse_recommendations`, `parse_targets`)
- `atomic_agents.dashboard.costs` (`_load_runs_with_degraded` — same loader the dashboard uses; `recommend_fleet` loader path only)
- `atomic_agents.agent_registry` (`AgentRegistryError`, `FilesystemAgentRegistryBackend` — governance parse state; `recommend_fleet` loader path only)
- `frontmatter` (already a project dependency for rubric.md + tuning report reading)

It **never** directly imports `agent.py`, `eval.py`, `tuning.py`, or `dream.py`. The MUST-10 forbidden set is exactly those four LLM-spend-bearing modules; the `dashboard.costs` and `agent_registry` loaders above are pure-read and construct no LLMBackend.

The conftest guard in `tests/advisor/` enforces the no-LLM-construction guarantee at test time. The Python `sys.modules` isolation guarantee is NOT required — the guarantee is zero LLM spend, not that tuning infrastructure cannot be loaded by another import elsewhere in the package.

---

## 3. Public API dataclasses

### EvalHeadroom

```python
@dataclass
class EvalHeadroom:
    weighted_score_margin: float   # mean weighted-score minus rubric threshold
    pass_rate_margin: float        # eval pass-rate minus rubric pass-rate threshold
    hard_fails: int                # count of evals with hard_fails non-empty
    sample_n: int                  # count of scorable evals (verdict in {pass, fail})
    rubric_threshold: float        # pass threshold from rubric.md (default 4.0)
    passed: bool                   # True only when ALL four predicates clear their floors
```

Two distinct margin fields — `weighted_score_margin` (on the 1-5 weighted score scale; `eval.py` clamps scores to `[1.0, 5.0]`) and `pass_rate_margin` (on the 0-1 pass-rate fraction scale) — are never collapsed into a single combined field.

### Recommendation

```python
@dataclass
class Recommendation:
    agent: str
    kind: str                          # member of RECOMMENDATION_KINDS
    current_model: str | None          # None for non-model recs
    candidate_model: str | None        # None for non-model recs
    projected_usd_delta: float | None  # negative = savings; None when N/A
    projected_points_delta: float | None  # composite point change; None when N/A
    rationale: str
    safety: EvalHeadroom               # always present; passed=True only for savings_cost
    source: str | None = None          # MODEL recs: "default_same_family" | "operator_configured";
                                       # None for non-model recs (governance, quality_report)
```

`source` describes how a **model** candidate was chosen, so it is meaningful only for `savings_cost` recs. Governance and quality_report recs have no candidate model and no model family — they leave `source` at its `None` default rather than carrying a model-selection label that would not apply. `source` is a diagnostic-only field; `render.py` does not read it.

`Recommendation.__post_init__` validates `kind` against `RECOMMENDATION_KINDS`. Any unknown kind raises `ValueError` at construction time.

### RECOMMENDATION_KINDS

```python
RECOMMENDATION_KINDS: frozenset[str] = frozenset({"savings_cost", "quality_report", "governance"})
```

A `frozenset` — immutable at construction, enumerable for validation.

---

## 4. `recommend()` — pure core

```python
def recommend(
    agent_health: AgentHealth,
    run_records: list[RunRecord],
    eval_records: list[_EvalRecord],
    rec_config: RecommendationConfig,
    rubric_threshold: float = 4.0,
    governance_rationale: str | None = None,
    tuning_report_paths: list[Path] | None = None,
    today: date | None = None,
    targets: FleetTargets | None = None,
) -> list[Recommendation]:
```

`targets` is the operator's parsed `FleetTargets` (the same object that produced `agent_health.composite`), required for a coherent `projected_points_delta` — see §7. When `None`, the point-impact counterfactual is skipped (`projected_points_delta=None`); the savings_cost rec still fires.

Pure function — zero disk I/O. All inputs are pre-loaded by the caller. Callers needing all three kinds should call `recommend_fleet()` which handles loading; `recommend()` is exposed for zero-I/O testing and counterfactual scoring.

**Window alignment (MUST 11):** `eval_records` MUST be the same records passed to `_score_quality_axis` for the relevant agent (same 30d window, same filtering). Mismatched windows produce logically incoherent guard predicates with no runtime error. `recommend_fleet()` enforces alignment by loading evals in the same window block as `compute_fleet_health`.

---

## 5. `savings_cost` rec: composite conjunctive guard (Option C)

A downgrade rec fires only when **all four predicates** pass over the same 30d eval window. This is the "composite conjunctive" guard (Option C ruling). No partial pass — all four or nothing.

| Predicate | Field | Threshold |
|-----------|-------|-----------|
| P1 | `weighted_score_margin >= score_margin_floor` | default 0.5 points |
| P2 | `pass_rate_margin >= pass_rate_margin_floor` | default 0.10 (10 pp) |
| P3 | `hard_fails == 0` | zero tolerance |
| P4 | `sample_n >= min_eval_n` | default 10 |

P4 is the fail-safe: zero or sub-N evals always block a downgrade regardless of the other predicates. An agent with no evals on record cannot pass the guard.

**P1 units (load-bearing):** `weighted_score_margin = mean_weighted - rubric_threshold` on the **1-5 weighted-score scale** (`eval.py` clamps to `[1.0, 5.0]`). With the default `rubric_threshold=4.0`, the MAXIMUM achievable margin is `5.0 - 4.0 = 1.0`. The floor MUST be reachable on that scale or the savings_cost rec can never fire. The default `0.5` means "half a rubric point of proven headroom above threshold" — reachable by a strong-but-not-perfect agent. (An earlier draft specified `2.0`, which is unreachable on this scale; corrected to `0.5`.)

The pass-rate threshold is derived as `rubric_threshold / 5.0` (converting the 1-5 weighted-score threshold to a 0-1 pass-rate fraction). NOTE: this intentionally couples the pass-rate floor to the rubric score scale; a dedicated `pass_rate_threshold` knob is deferred.

---

## 6. `savings_cost` rec: same-family downgrade candidate (Option 3)

The candidate model is selected from `_DEFAULT_SAME_FAMILY_DOWNGRADE`, a hardcoded mapping of all `PRICING` model aliases to their next-cheaper same-family sibling. This is the "conservative same-family" approach (Option 3 ruling):

- Anthropic: `claude-opus-4-8` → `claude-sonnet-4-6-20260101` → `claude-haiku-4-5-20251001` (the map prefers dated aliases as candidates — pinned version, not floating)
- OpenAI: `gpt-5` → `gpt-5-mini` → `gpt-5-nano`
- Vertex: `vertex/gemini-2.5-pro` → `vertex/gemini-2.5-flash` → `vertex/gemini-2.0-flash` → `vertex/gemini-2.0-flash-lite`
- Moonshot: no downgrade (all same price tier)

If the current model has no entry in the map (unknown model, cheapest tier, or moonshot), `candidate` is `None` and no `savings_cost` rec fires. This is fail-pessimistic — never recommend a downgrade for an unknown model.

An operator can override with `work_type_allowed_models` in `targets.md`:

```yaml
recommendations:
  work_type_allowed_models:
    general: ["claude-haiku-4-5"]
    coding: ["claude-haiku-4-5", "claude-sonnet-4-6-20260101"]
```

When the operator supplies a list for the agent's majority work-type, the **first PRICING-registered model in that list** (`_resolve_candidate` returns `valid[0]`) becomes the candidate instead of the default map — operator list order is respected, fitting the markdown-config aesthetic.

---

## 7. `savings_cost` rec: ranking by point-impact + USD fallback (#687)

Recs are ranked by `(abs(projected_points_delta), abs(projected_usd_delta) tiebreak)` descending. After #687, `cheaper_model_share` and `tokens_per_output` are no longer health metrics, so every `savings_cost` rec produces `projected_points_delta ≈ 0.0`. When `projected_points_delta == 0` and `kind == "savings_cost"`, the tiebreak is `abs(projected_usd_delta)` — larger dollar savings rank first. For non-`savings_cost` kinds the USD tiebreak is 0. The point-impact is computed via pure counterfactual scoring (Option 1 ruling):

1. Reprice the agent's **primary** 30d `run_records` (`_is_primary_run`) at `candidate_model` rates via `_reprice_run()` (uses `calc_cost()` with actual `cache_hit_tokens` — `calc_cost` applies `CACHE_HIT_DISCOUNT` internally). Helper/delegate runs are NOT repriced — a primary-model swap does not change what model a helper call ran on.
2. Build a counterfactual `runs_30d` list using `dataclasses.replace(run, model=candidate, cost_usd=repriced)` **for primary runs only** — helper/delegate runs keep their own model and cost; originals are never mutated (MUST 8).
3. Score BOTH a reduced baseline and the counterfactual via `_score_agent_from_data(agent, runs, runs_prior_30d=[], ..., targets=<operator targets>, today=<same ref date>)` — the pure scoring core extracted from spec/53, under the **same `FleetTargets` and `today`** that `compute_fleet_health` used to produce `agent_health.composite`. Both calls pass `runs_prior_30d=[]` (see "Symmetric baseline" below).
4. `projected_points_delta = counterfactual.composite - baseline_reduced.composite` (NOT minus `agent_health.composite` — see below).
5. `projected_usd_delta = sum(repriced_primary_runs) - sum(original_primary_runs.cost_usd)` for the 30d window. The baseline side is the agent's **stored ledger cost** (`r.cost_usd`, "what you actually paid"), not a token-reprice of the source model. In production the two normally agree (the stored cost was computed from the same tokens via `calc_cost`), but they diverge when the original run used fallback (unknown-model markup) pricing or when `PRICING` changed since the run was logged — in those cases the `$/mo` figure reflects the real spend delta, not a pure same-PRICING-table delta. This is the intended baseline (the operator acts on actual spend); a downstream consumer wanting a pure pricing-delta would reprice both sides from tokens.

**Targets coherence (load-bearing):** the baseline and the counterfactual MUST be scored under the same targets and the same reference date. `recommend()` takes a `targets: FleetTargets | None` parameter; `recommend_fleet()` passes `parse_targets(agents_root)` — the exact object `compute_fleet_health` used. Scoring the counterfactual under hardcoded defaults while the baseline used operator-customized targets would make the diff conflate the model swap with a target-set switch, corrupting the fleet-wide ranking key (`abs(projected_points_delta)`). When `targets is None`, the point-impact step is skipped and `projected_points_delta=None`.

**Symmetric baseline (load-bearing — the diff must isolate the model swap):** the counterfactual is scored with `runs_prior_30d=[]` (the pure core has no prior-window data), so its `spend_vs_trend` cost sub-metric goes to a `no_data` row that is **excluded** from the cost sub-score. `agent_health.composite`, by contrast, was scored by `compute_fleet_health` WITH a real prior window, so it **includes** `spend_vs_trend` — possibly a floored, critical-capping value. Diffing the counterfactual against `agent_health.composite` directly would therefore credit the model swap for `spend_vs_trend` simply **disappearing**, a phantom point swing unrelated to the model (e.g. a cost-neutral `haiku → haiku-<dated>` swap against a `spend_vs_trend`-capped baseline produced a phantom **+38.5 points** — reproduced, see `TestPointImpactSymmetricBaseline`). To prevent this, `_compute_point_impact` **re-scores the baseline with the same reduced inputs** (`runs_prior_30d=[]`) and diffs `counterfactual.composite − baseline_reduced.composite`, so both sides drop `spend_vs_trend` identically.

**Post-#687 (spec/53 §3.6 + MUST 14):** `cheaper_model_share` and `tokens_per_output` are no longer health metrics — they are advisory-only values that feed recommendations but are NOT scored into the Cost sub-score or composite. As a result, a model swap moves NO scored metric in the counterfactual scoring path. Every `savings_cost` rec produces `projected_points_delta ≈ 0.0`. The returned delta reflects ONLY `spend_vs_trend`, which both sides exclude (symmetric baseline) — so the net delta for any model swap is ~0.0.

**Ranking fallback (#687):** because all `savings_cost` recs have `projected_points_delta ≈ 0.0`, `recommend_fleet()` breaks ties by `abs(projected_usd_delta)` for `savings_cost` kind only. The sort key is `(abs(projected_points_delta), usd_tiebreak)` where `usd_tiebreak = abs(projected_usd_delta)` when `projected_points_delta == 0` and `kind == "savings_cost"`, else `0.0`. Larger dollar savings rank first among cost recs. The `$/mo` savings is computed from real repriced spend (§6) and is the primary operator-facing signal.

**`projected_points_delta` is a RANKING proxy, not a realizable-composite promise.** Because it is computed by diffing two *reduced-window* re-scorings (both `runs_prior_30d=[]`, both dropping `spend_vs_trend`), the number is the model-swap-isolated composite change *under prior-window-less scoring* — deliberately NOT the change the agent's live composite (scored with its real prior window) would show. It is honest for ranking recs against each other; it is NOT a guarantee that applying the swap raises the agent's live headline by that many points. After #687 this value is ~0.0 for all savings_cost recs; the primary signal for operator action is the `$/mo` rationale (§6).

If `_score_agent_from_data` raises or `projected_points_delta` cannot be computed, the rec is still emitted with `projected_points_delta=None` (fail-soft).

---

## 8. `quality_report` rec

Fires when a tuning report on disk has either:
- a hard-fail signal — `has_hard_fails: true` in frontmatter, OR the substring `hard_fail`/`hard fail` in the report BODY text (some tuning reports record hard-fails in prose rather than a frontmatter flag), OR
- a `score:` value in frontmatter below `rubric_threshold` — `weighted_score:` is read as a fallback ONLY when `score:` is absent from frontmatter (a present `score: 0` is honored, not treated as falsy)

`_load_tuning_reports()` globs `<agents_root>/<agent>/evals/tuning_reports/*.md` — the path `tuning.py` (tuning.py:1045) actually writes to and `dashboard/quality.py` reads from. Per-file `OSError` is silently skipped (fail-soft). `_read_tuning_report()` parses frontmatter via `python-frontmatter`; returns `None` on the genuinely-expected failure modes (`OSError`, `ValueError`, `yaml.YAMLError`) and logs a warning; other exception types propagate (a broad catch would hide real bugs the way the `ref.agent_id` `AttributeError` was hidden).

The rec's `rationale` describes which condition fired ("hard-fail detected" / "score X below threshold Y"). No eval guard applies — a quality report rec fires based on the report file content alone.

---

## 9. `governance` rec

Fires when `_governance_rec_rationale()` returns a non-None string. The rationale is computed by the caller (`recommend_fleet`) from the AgentRegistry parse state:

| Parse state | Fires rec? | Rationale |
|-------------|-----------|-----------|
| `ABSENT` | Yes | "governance.md is absent" |
| `PRESENT_NO_BLOCK` | Yes | "governance.md has no YAML block" |
| `PRESENT_INVALID` | Yes | "governance.md has parse errors" |
| `PRESENT_VALID` | No | — |
| `PRESENT_UNREADABLE` | Yes (as "absent") | "governance.md is absent" — see collapse note |

**PRESENT_UNREADABLE collapse (known limitation):** the FilesystemAgentRegistryBackend maps `PRESENT_UNREADABLE` (and symlink-escape / oversize-file) to `(has_governance=False, governance=None)` — byte-identical to `ABSENT`. `AgentRef` exposes no signal to distinguish them, so an unreadable `governance.md` surfaces the **"absent"** rationale (the operator is told to add a file that exists but could not be read). Honoring the original "do not surface" intent would require the registry to expose a distinct unreadable state at the `AgentRef` surface, which is out of PR3 scope. The `_governance_rec_rationale` docstring documents this collapse; the registry-surface follow-up is tracked in **#625**.

The governance rec has `projected_usd_delta=None` and `projected_points_delta=None` (N/A).

---

## 10. `targets.md` — recommendations config block

A new top-level `recommendations:` YAML block in `targets.md`, parallel to `scoring:`:

```yaml
recommendations:
  score_margin_floor: 0.5          # weighted-score points above rubric threshold (P1, 1-5 scale)
  pass_rate_margin_floor: 0.10     # pass-rate margin required (P2)
  min_eval_n: 10                   # minimum scorable evals for downgrade (P4)
  min_savings_usd: 5.00            # minimum projected savings to emit a rec
  work_type_allowed_models:        # optional; overrides default same-family map
    general: ["claude-haiku-4-5"]
```

Parsing is per-key fail-soft — a bad value for one key reverts that key to its default without dropping the whole block. An absent `recommendations:` block = all defaults.

### RecommendationConfig defaults

| Field | Default |
|-------|---------|
| `score_margin_floor` | 0.5 (reachable on the 1-5 margin scale — see §5) |
| `pass_rate_margin_floor` | 0.10 |
| `min_eval_n` | 10 |
| `min_savings_usd` | 5.00 |
| `work_type_allowed_models` | `None` (no operator map → use default same-family map) |

There are no `ranking_*_weight` fields. `recommend_fleet()` ranks recs by `(abs(projected_points_delta), usd_tiebreak)` desc — see §7 for the `savings_cost` USD fallback (#687). A weighted ranking scheme would be config nothing reads. "Pick the option that adds fewer concepts."

---

## 11. ConsoleData + render integration

`ConsoleData.recommendations: list | None` field added to `attention.py`.

`render_console()` calls `recommend_fleet()` after scoring, catches all exceptions (fail-soft), and stores the result in `console_data.recommendations`.

`_render_recommendations(recommendations)` renders a rec panel:

- Returns empty string on `None`; a friendly "no recommendations" note on empty list.
- Each rec: kind-specific pill (`.rec-kind-{kind}`), agent name (`.rec-agent`), model arrow (`current → candidate` via `.rec-models`/`.rec-arrow`), rationale, projected delta badges (`.rec-delta-savings` / `.rec-delta-points`). The kind-pill and delta-badge CSS classes are the live styling path — no generic `ok/warn/error` pills and no inline `style=` deltas.
- Wired into the console template body after axis panels, before the Agent Fleet table.
- Footer updated to reference `spec/52 + spec/53 + spec/54`.

---

## 12. Implementer Contract (MUST set)

| # | MUST | Test |
|---|------|------|
| 1 | `Recommendation.kind` must be a member of `RECOMMENDATION_KINDS`; any other value raises `ValueError` at construction | `TestRecommendationKindValidation` |
| 2 | Predicate P1: `weighted_score_margin >= score_margin_floor` (default `0.5`, reachable on the 1-5 margin scale per §5); failure blocks the rec, and the default config CAN emit a savings rec for a strong agent | `TestEvalHeadroom::test_strip_red_p1_score_margin_fails`, `TestSavingsCostRec::test_default_config_emits_savings_rec` |
| 3 | Predicate P2: `pass_rate_margin >= pass_rate_margin_floor`; failure blocks the rec | `TestEvalHeadroom::test_strip_red_p2_pass_rate_margin_fails` |
| 4 | Predicate P3: `hard_fails == 0`; any hard-fail blocks the rec | `TestEvalHeadroom::test_strip_red_p3_hard_fails_present` |
| 5 | Predicate P4 (fail-safe): `sample_n >= min_eval_n`; sub-N or zero evals block the rec | `TestEvalHeadroom::test_strip_red_p4_sample_n_below_min`, `test_strip_red_failsafe_empty_evals` |
| 6 | Zero evals → `EvalHeadroom.passed = False`; no `savings_cost` rec emitted | `TestEvalWindowAlignment::test_empty_evals_no_downgrade` |
| 7 | No LLMBackend constructed on any `recommend` / `recommend_fleet` path (zero LLM spend) — including the on-disk loader graph (compute_fleet_health, _load_runs_with_degraded, _load_eval_records, FilesystemAgentRegistryBackend, tuning-report reader) | `TestNoLLMEnforcement` (conftest autouse) + `TestRecommendFleet` (real on-disk fleet exercised under the autouse guard) |
| 8 | `dataclasses.replace` used for counterfactual run records; original `run_records` list is never mutated | `TestCounterfactualNonMutation` |
| 9 | Candidate model must exist in `PRICING`; unknown model or cheapest-tier model → `candidate=None`, no rec emitted (fail-pessimistic) | `TestCandidateResolution::test_unknown_model_returns_none`, `test_cheapest_model_returns_none` |
| 10 | `recommend.py` never directly imports `agent.py`, `eval.py`, `tuning.py`, or `dream.py` (source-level and at test time) | `TestNoLLMEnforcement::test_recommend_source_has_no_forbidden_imports` |
| 11 | `eval_records` passed to `recommend()` MUST be from the same 30d window as the `AgentHealth.quality_score`; `recommend_fleet()` enforces alignment | `TestEvalWindowAlignment` |

---

## Addendum (#727 Unit 1) — candidate-vs-render split + canonical rec-id

**Status of this addendum:** DRAFT (this spec stays DRAFT; the addendum documents a shipped code change, it does not re-lock the doc).

### Candidate-vs-render split

Before #727, a `savings_cost` `Recommendation` object only ever existed when the no-quality-cost guard (§5) passed — the guard-failing branch of the old `recommend()` savings block never constructed a `Recommendation`, so a swap that cleared the savings floor but failed the guard left no trace as data. The planned `apply-rec` verb (#727 Unit 2, spec/55) needs to see that guard-failed state to distinguish "this swap eroded on quality" (a candidate exists, `safety.passed=False`) from "this swap no longer exists" (no candidate at all, e.g. the savings floor no longer clears, or the candidate model changed).

`derive_savings_candidates()` is now the single derivation path for the `savings_cost` swap: it resolves the candidate model, applies the savings-floor filter (`projected_usd_delta < 0` and `abs(projected_usd_delta) >= min_savings_usd` — this filter is NOT relaxed; a savings-eroded swap still returns `[]`), and then constructs the `Recommendation` **regardless of the no-quality-cost guard verdict**, carrying `safety=headroom` as data. Exactly one axis of leniency is dropped relative to the old inline block: the guard. The savings floor stays a hard existence gate, because "no longer saves money" and "unsafe to act on right now" are different operator-facing states and only the second one is what the guard is for.

`recommend()` (per-agent) and `recommend_fleet()` (fleet-wide, the console feed) both call `derive_savings_candidates()` / `derive_savings_candidates_fleet()` and then keep only `safety.passed == True` results. The render-facing invariant from §5 — "a `savings_cost` rec on the console means safe to act" — is unchanged; it now holds because the render path filters, not because the guard-failed state cannot be represented. `derive_savings_candidates_fleet()` (new, fleet-wide, no filter) is apply-rec's match universe (#727 Unit 2) — a guard-failing candidate is visible there with `safety.passed=False`.

Both `recommend_fleet()` and `derive_savings_candidates_fleet()` iterate the same shared per-agent loader (`_load_fleet_agent_inputs()`, private) instead of each inlining their own copy of the windowed JSONL/frontmatter reads. This is the load-bearing anti-drift move: a sibling loader could diverge (different window math, a missed fail-soft branch) and hand the two surfaces different inputs for the same agent, corrupting the rec-id bijection below.

### Canonical rec-id (ruling: rec-id-hash-recipe)

Each `savings_cost` card the console renders needs a stable identifier a human or a script can hand to `apply-rec` to say "do this one." `canonical_rec_id(agent, kind, candidate_model)` is the ONE shared hash helper — both the Fleet Console render (dashboard) and the `apply-rec` verb (#727 Unit 2, spec/55) import it, so the id shown on a card and the id `apply-rec` matches against can never diverge.

Recipe: `sha256("v1" + "\x1f" + agent + "\x1f" + kind + "\x1f" + (candidate_model or ""))`, first 12 hex characters — the same short-prefix convention `dashboard/attention.py`'s `_make_alert_key` already uses. `\x1f` (ASCII Unit Separator) is the delimiter so a literal separator character occurring inside a field can never make two distinct triples collide. The `v1` version prefix is part of the hashed string (not a display prefix) so a future recipe change can invalidate old ids by bumping it.

`source` (`"default_same_family"` | `"operator_configured"`, §6) is deliberately **excluded** from the hash. A swap's identity is the agent, the rec kind, and the candidate model — not how the candidate was selected. Hashing `source` in would mean the same swap gets two different ids depending on whether an operator happened to configure `work_type_allowed_models` that day, which is not a distinction `apply-rec`'s match universe needs to make.

`candidate_model` is `None` for the two non-model rec kinds (`governance`, `quality_report`); it is encoded as the empty string in the canonical form (never omitted — omission would let `("a", "governance", None)` and a hypothetical `("a", "governance", "")` collide, which the empty-string encoding rules out by construction).

---

## 13. File map

```
atomic_agents/
  advisor/
    __init__.py          — public API re-exports (adds Recommendation, EvalHeadroom, etc.)
    recommend.py         — recommendations engine (this spec)
    score.py             — scoring engine; _score_agent_from_data extracted as pure core
    targets.py           — targets.md parser; adds RecommendationConfig + parse_recommendations
  dashboard/
    attention.py         — ConsoleData.recommendations field added
    render.py            — _render_recommendations() + wired into render_console()

docs/spec/
  53-fleet-console-scoring.md       — updated with §Recommendations contract cross-ref
  54-fleet-console-recommendations.md — this file

tests/
  advisor/
    conftest.py               — no-LLM guard (module autouse; covers the recommend_fleet on-disk loader path)
    test_advisor_recommend.py — all 11 MUSTs + strip-RED controls + the symmetric-baseline
                                point-impact regression + governance-branch units + render-panel
                                tests + the recommend_fleet on-disk loader suite
    test_advisor_score.py     — scoring suite (adds #623 display-int + WoW equal-window strip-RED
                                + the separate-block scoring-config-drop regression)
```

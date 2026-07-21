"""``manage apply-rec <rec-id>`` verb — Fleet Console recommendation loop-closer
(spec/55 #727 Unit 2).

`apply-rec` closes the loop the Fleet Observability Console opens (spec/54):
the console computes and renders `savings_cost` recommendations ("swap
`<agent>` from `<current_model>` to `<candidate_model>`, save ~$X/mo"), but
rendering a recommendation is not applying it. `apply-rec` takes the rec-id
the console displayed, re-derives and re-validates that recommendation
against CURRENT data, and — only if it still holds — DELEGATES the actual
`model.md` write to `set-model`'s shipped write routine
(``apply_set_model_write`` in ``set_model.py``, #726). It is a
**composition** verb, not a third independent editor: it does NOT open a
second write path onto ``model.md``, does NOT duplicate the surgical writer,
the manage lease, the snapshot/restore machinery, or the M9 composition
chain — all of that is inherited unchanged by delegating through the shared
seam.

Match universe (spec/55, ``build_rec_match_universe`` —
``atomic_agents/advisor/recommend.py``): ALL current recommendations of
every kind (via ``recommend_fleet``) UNION guard-failed savings candidates
(via ``derive_savings_candidates_fleet``), computed fresh on every
invocation — recommendations are never persisted (spec/54), so this call is
unconditional. The all-kinds union is load-bearing: it is what makes
``rec_kind_not_applicable`` REACHABLE (a governance/quality_report rec-id
hash-hits and branches to the kind gate, instead of hash-missing straight
into ``rec_no_longer_valid``).

apply-rec reads the verdict as DATA off the matched, freshly-recomputed
``Recommendation.safety.passed`` field — it does NOT import advisor
underscore-privates like ``_eval_headroom`` itself (Principle #3 / TENSIONS
T17 layering: a verb module composes with the advisor's public recompute
surface, it does not reach past it into internals the advisor module owns).

Four refusals, each with distinct retryability semantics:
  rec_no_longer_valid       — no current recommendation matches <rec-id>.
                               EXPECTED, not an error — the console card is
                               stale. Retry: re-derive (reload the console)
                               and pass the new rec-id.
  rec_kind_not_applicable   — the matched rec exists but isn't savings_cost
                               (quality_report/governance are advisory-only,
                               there is no mechanical apply for either).
                               Not retryable — this rec is never applicable.
  rec_source_not_applicable — the matched savings_cost rec's `.source` is
                               outside PR1's allowlist (the skeptic's guard,
                               below). Not retryable via apply-rec today —
                               apply the swap by hand via `set-model` if the
                               operator has independently validated it.
  rec_guard_failed           — the swap still exists as a real candidate but
                               its no-quality-cost guard no longer passes (a
                               hard-fail landed, or a margin eroded). STOP,
                               look at the evals — do NOT blindly retry.

Past all four gates, apply-rec delegates into `set-model`'s OWN M9
composition chain (PRICING/backend-resolution/policy-consult) against the
matched `candidate_model`, unchanged, with `set-model`'s existing
`error_type`s — a candidate since deprecated from PRICING or now ambiguous
refuses through those gates exactly as a hand-typed `set-model --model`
invocation would. apply-rec does not re-run or shortcut them.

Fourth refusal beyond the ruled three (deliberate, per the maintainer's
skeptic's-guard ruling): folding ``rec_source_not_applicable`` into any of
the other three would collapse two DISTINCT retryability stories into one —
"the console is stale, reload it" (rec_no_longer_valid) is a completely
different operator action from "this candidate's selection basis isn't
verifiable yet" (rec_source_not_applicable), and conflating them would make
a copilot driver retry-loop against a refusal that will never clear on
retry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..advisor.recommend import (
    Recommendation,
    build_rec_match_universe,
    canonical_rec_id,
)
from ..core_api import safe_resolve_under
from ..logs.types import PRIMITIVE_MANAGE_APPLY_REC
from .exceptions import (
    ManageRecGuardFailedError,
    ManageRecKindNotApplicableError,
    ManageRecNoLongerValidError,
    ManageRecSourceNotApplicableError,
)
from .set_model import apply_set_model_write

# Skeptic's guard (spec/55 measured-scorecard-source ruling, #727): only
# default_same_family-sourced savings candidates are applicable in PR1.
# operator_configured is EXCLUDED BY DESIGN — apply-rec cannot yet
# re-validate the ground-truth basis an operator-configured candidate was
# selected on (an operator may have hand-picked a candidate model for
# reasons apply-rec has no way to verify), so it refuses rather than
# silently applying a swap PR1 has no basis to vouch for. This is a
# documented, intentional, conservative gap — not an oversight — and a
# future `measured_scorecard` source (does not exist in code yet; see
# #649/#644-child-D) is the primary target this allowlist widens for later,
# once a re-validation path for it is designed.
_APPLICABLE_REC_SOURCES: frozenset[str] = frozenset({"default_same_family"})


# ── --json error helper (mirrors set_model.py / govern.py's shape) ─────────


def _emit_json_error(error_type: str, reason: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"ok": False, "error_type": error_type, "reason": reason}
    payload.update(extra)
    print(json.dumps(payload, indent=2))


def _safety_json(safety: Any) -> dict[str, Any]:
    """Serialize an EvalHeadroom to the --json 'safety' payload shape.

    Carries all four margins (spec/55 — a copilot needs to see WHY a
    rec_guard_failed refusal fired, or what the guard looked like at
    --dry-run preview time, not just a bare pass/fail bit).
    """
    return {
        "weighted_score_margin": safety.weighted_score_margin,
        "pass_rate_margin": safety.pass_rate_margin,
        "hard_fails": safety.hard_fails,
        "sample_n": safety.sample_n,
        "rubric_threshold": safety.rubric_threshold,
        "passed": safety.passed,
    }


def _find_match(
    rec_id: str, agent_scope: str | None, agents_root: Path
) -> Recommendation | None:
    """Resolve <rec-id> against the CURRENT match universe (fresh every call).

    ``agent_scope`` (--agent) narrows the SEARCH, not the identity — it
    filters the universe before matching so a copilot that already knows
    which agent it is targeting avoids a fleet-wide hash collision surface,
    but it never changes what a given rec-id means.
    """
    universe = build_rec_match_universe(agents_root)
    if agent_scope:
        universe = [r for r in universe if r.agent == agent_scope]
    for rec in universe:
        if canonical_rec_id(rec.agent, rec.kind, rec.candidate_model) == rec_id:
            return rec
    return None


# ── Main verb entry point ──────────────────────────────────────────────────


def run_apply_rec(args: Any, agents_root: Path) -> int:
    """Entry point for ``atomic-agents manage apply-rec <rec-id> ...``.

    Mirrors ``run_set_model``'s exit-code ladder (spec/55 normative note):
        0   — applied / dry-run preview
        1   — refusal (rec-match, kind, source, guard, registry, path,
              or an inherited set-model M9 refusal) or a write/read error
        3   — interactive decline ('n' / EOF)
        130 — KeyboardInterrupt / SIGINT

    ``ManageAgentBusyError`` / ``ManageLockUnavailableError`` — raised by the
    delegated ``apply_set_model_write`` call — propagate UNCAUGHT out of this
    function; the central catch lives in ``run_manage()`` (spec/55 M11),
    exactly as it does for ``set-model``'s own write path.
    """
    use_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    yes = getattr(args, "yes", False)
    rec_id: str = args.rec_id
    agent_scope: str | None = getattr(args, "agent", None)

    matched = _find_match(rec_id, agent_scope, agents_root)

    # ── Gate 1: rec-match ────────────────────────────────────────────────
    if matched is None:
        exc = ManageRecNoLongerValidError(rec_id)
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Gate 2: applicable kind ─────────────────────────────────────────
    if matched.kind != "savings_cost":
        exc = ManageRecKindNotApplicableError(rec_id, matched.kind)
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Gate 3: applicable source (the skeptic's guard) ─────────────────
    if matched.source not in _APPLICABLE_REC_SOURCES:
        exc = ManageRecSourceNotApplicableError(rec_id, matched.source)
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Gate 4: no-quality-cost guard still passes ──────────────────────
    if not matched.safety.passed:
        exc = ManageRecGuardFailedError(rec_id)
        safety_json = _safety_json(matched.safety)
        if use_json:
            _emit_json_error(exc.error_type, str(exc), safety=safety_json)
        else:
            print(f"Error: {exc}", file=sys.stderr)
            s = matched.safety
            print(
                "  EvalHeadroom (all four margins): "
                f"weighted_score_margin={s.weighted_score_margin:+.2f}, "
                f"pass_rate_margin={s.pass_rate_margin:+.2f}, "
                f"hard_fails={s.hard_fails}, sample_n={s.sample_n}, "
                f"rubric_threshold={s.rubric_threshold:.1f}, passed={s.passed}",
                file=sys.stderr,
            )
        return 1

    # ── Past all four gates — resolve the target agent through the
    # registry (mirrors set_model.run_set_model's S1 resolve + path/symlink
    # guards; duplicated here rather than imported because it is agent-
    # lookup boilerplate, not the write path itself — the write path is
    # exactly what gets delegated below) ────────────────────────────────
    try:
        from ..agent_registry import get_default_agent_registry_backend  # noqa: PLC0415

        registry = get_default_agent_registry_backend(agents_root)
        ref = registry.get_agent(matched.agent)
    except Exception as exc:  # noqa: BLE001
        reason = f"Failed to load agent registry: {exc}"
        if use_json:
            _emit_json_error("registry_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    if ref is None:
        reason = f"Agent {matched.agent!r} not found in the registry at {agents_root}"
        if use_json:
            _emit_json_error("agent_not_found", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    agent_dir = Path(ref.location)

    try:
        safe_resolve_under(agent_dir, agents_root)
    except Exception as exc:  # noqa: BLE001
        reason = f"Agent directory outside agents_root — refused: {exc}"
        if use_json:
            _emit_json_error("path_traversal", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    model_path = agent_dir / "model.md"

    try:
        safe_resolve_under(model_path, agents_root)
    except Exception as exc:  # noqa: BLE001
        reason = f"model.md path outside agents_root — refused: {exc}"
        if use_json:
            _emit_json_error("path_traversal", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    if model_path.exists() and model_path.is_symlink():
        reason = (
            f"model.md at {model_path} is a symlink — write refused "
            "(path containment guard)."
        )
        if use_json:
            _emit_json_error("symlink_refused", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── Delegate into set-model's own write routine (#726) ───────────────
    # apply-rec's own "write" IS a set-model write through the S2 spine —
    # no second write path onto model.md. set-model's OWN M9 composition
    # chain (PRICING/backend-resolution/policy-consult) still runs against
    # matched.candidate_model, unchanged, inside apply_set_model_write.
    safety = matched.safety
    rec_marker = {"rec_id": rec_id, "kind": matched.kind}
    safety_json = _safety_json(safety)

    def _preamble() -> None:
        # Human-mode only (apply_set_model_write never calls this under
        # --json — the same data travels in json_extra instead).
        print(f"\n[{matched.agent}] applying recommendation {rec_id} (savings_cost):")
        print(f"  {matched.rationale}")
        print(
            "  EvalHeadroom (all four margins): "
            f"weighted_score_margin={safety.weighted_score_margin:+.2f}, "
            f"pass_rate_margin={safety.pass_rate_margin:+.2f}, "
            f"hard_fails={safety.hard_fails}, sample_n={safety.sample_n}, "
            f"rubric_threshold={safety.rubric_threshold:.1f}, "
            f"passed={safety.passed}"
        )

    return apply_set_model_write(
        agent_id=matched.agent,
        agent_dir=agent_dir,
        agents_root=agents_root,
        model_path=model_path,
        model_id=matched.candidate_model,
        use_json=use_json,
        dry_run=dry_run,
        yes=yes,
        audit_primitive=PRIMITIVE_MANAGE_APPLY_REC,
        audit_extra={"applied_from_rec": rec_marker},
        json_extra={"applied_from_rec": rec_marker, "safety": safety_json},
        human_preamble=_preamble,
    )

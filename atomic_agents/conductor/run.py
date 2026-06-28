"""Conductor run engine — free function run() + helpers (spec/50 PR1).

This module is the orchestration core: it sequences automated PLAYBOOK.md stages,
writes the durable goal ledger, and resumes from it on crash-restart.

C1 — The conductor holds no authoritative state of its own. Every durable fact
lives in Goal / Outcome / Idempotency. run() reconstructs the run's place
entirely from those stores on every invocation.

C2 — The goal ledger is the authoritative resume cursor. A stage whose sub-goal
is 'complete' (with a resolvable stored result) is skipped; 'in_progress'/'pending'
is (re-)run; 'blocked' is normalized and re-run; 'abandoned' is TERMINAL on resume
(the run halts — never auto-revived, maintainer ruling A).

C3 — A replayed stage's result comes from the durable store (OutcomeBackend),
never from a deduped Response (spec/45 W2/W7 — deduped Responses carry no output).

C6 — Every stage runs behind a cost gate, and the run is tree-capped: each stage's
gate (pre-dispatch AND per-iteration inside OutcomeRunner) clamps to
MIN(model.md remaining, run_remaining − stage spend so far), where run_remaining =
run_cap_usd − cumulative_spend (threaded as parent_remaining_headroom_usd). The
per-iteration gate decrements the threaded headroom by the stage's own accumulating
spend (OutcomeRunner._clamped_parent_headroom), so the run cap binds AT EACH
ITERATION BOUNDARY — within-stage overshoot is bounded by one iteration's spend (the
same granularity as the delegate tree-cap), not zero. The coarse pre-stage
`run_remaining <= 0` halt is the between-stage backstop. CAVEAT (M1): the
per-iteration binding holds ONLY when the agent's cost guardrails are enabled —
`_check_cost_guardrails()` short-circuits to allow BEFORE the parent-headroom clamp
when `cost_guardrails_enabled` is False, so with guardrails off the run cap degrades
to the coarse between-stage backstop alone (run() warns loudly). cumulative_spend is
re-summed from the durable goal_history.jsonl JSONL events on every stage AND every
resume — no process-memory carry — and counts spend from every dispatch attempt
that REACHES A TERMINAL COORDINATOR TRANSITION (complete or not, via the terminal
`sub_goal_outcome_dispatched` event), so a failing-but-terminated stage's spend is
carried toward the cap across resumes (fail-closed for that window).

KNOWN run-cap UNDER-COUNT (narrow, bounded fail-OPEN window): a hard crash during a
stage's LLM call — after the child agent spent real money on the parent agent's
daily/monthly ledger but BEFORE the terminal `sub_goal_outcome_dispatched` event
lands in goal_history.jsonl — leaves that partial spend uncounted toward the run
cap on resume (cumulative_spend reads only terminal events, never the parent agent
daily log). This is the same single-host hard-crash window the spec/50 §"Resume
semantics" names for duplicate runs; the model.md daily/monthly cap on the parent
agent's log is the backstop for that spend. Exposure is material only for an
operator who sets run_cap_usd while disabling the daily/monthly caps, and only
across repeated hard crashes mid-call (#580 follow-up: cross-check the parent
agent daily-log delta against the ledger on stage close/resume).

PR1 scope — automated stages only. Gate stages (is_gate=True) cause run() to halt
with halt_reason='gate_not_implemented_pr2'. PR2 (#581) adds suspend/resume().

Module-name-collision note: this is atomic_agents/conductor/run.py (the public
orchestration package). The deploy helper atomic_agents/deploy/_planner.py
(formerly _conductor.py) is a separate, private module. See spec/49 + spec/50.

KNOWN LIMITATION: GoalManager.for_goal() does NOT propagate a custom goal_backend
injected on the parent manager to the scoped child (tracked #656). A conductor
operator who pins a custom GoalBackend on the parent manager will silently lose
that override at the for_goal() boundary; run-goal sub-goal transitions will write
to the default filesystem backend. Runtime warning emitted when this is detected.

KNOWN LIMITATION: export() raises on agents with addressed run-goals (#643).
Running a conductor session creates goals/<conductor_run_id>/goal.md; any
'atomic-agents export' call on that agent will fail until #643 is implemented.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
import warnings as _warnings
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..outcome.types import OutcomeResult

from ..exceptions import AtomicAgentsError, CostGuardrailBlocked, GoalCorrupted
from ..goal.backend import AddressableGoalBackend
from ..goal.types import (
    CURRENT_GOAL_SCHEMA_VERSION,
    Goal,
    SubGoal,
    validate_goal_id,
)
from ..idempotency.types import COMPLETED
from .types import ConductorState, PlaybookManifest, StageSpec

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Public entry point


def run(
    playbook: PlaybookManifest,
    subject: str,
    agent: Any,  # AtomicAgent — provides cost universe + backends
    *,
    conductor_run_id: str | None = None,
    max_stage_iterations: int = 3,
    judge_model: str | None = None,
) -> ConductorState:
    """Run a playbook against a subject, returning a fresh ConductorState projection.

    Re-entrant: calling run() again with the same ``conductor_run_id`` resumes
    the run from the durable goal ledger (crash-resume pattern).

    Cost universe: ``agent`` is the PARENT AtomicAgent whose agent_root contains
    model.md with valid cost caps. The conductor uses this agent's cost gate for
    every stage. Do NOT pass an AtomicAgent rooted at goals/<id>/ — that
    directory has no model.md and the cost gate would default to fail-open.

    Resume cursor: the goal ledger is the SOLE authoritative resume cursor, with
    the full Contract C2 predicate enforced — a stage is skipped ONLY when its
    sub-goal is ``complete`` AND its stored result is resolvable from the durable
    store. Resolution tries ``sub_goal.output`` first, then REPAIRS from a COMPLETED
    idempotency record's ``prior_result_ref`` if the pointer is missing/unresolvable
    (H1: a crash that landed the result + idempotency commit but lost the pointer
    write self-heals on resume). A ``complete`` stage whose result is unrecoverable
    from BOTH sources is ledger corruption and FAILS CLOSED (raises
    ``GoalCorrupted``), never a silent skip. A ``blocked`` stage (coordinator-
    produced terminal mapping) is normalized and re-run. An ``abandoned`` stage is
    TERMINAL on resume — the run HALTS with ``halt_reason='stage_abandoned'`` so the
    operator must explicitly decide (requeue/skip/modify); it is never auto-revived
    (maintainer ruling A). Any other non-complete status (pending/in_progress) is
    re-run.

    Single-writer assumption (PR1): run() takes NO run-level lock. Two concurrent
    run() calls on the SAME ``conductor_run_id`` would double-dispatch stages and
    defeat the tree-cap (each re-sums spend independently; the IN_FLIGHT idempotency
    state is not treated as a hard stop). PR1 scope EXCLUDES concurrency — the
    caller MUST serialize invocations per conductor_run_id. The run-level lock +
    conflict serialization land in PR3 (#582).

    Cost-cap caveat: the within-stage tree-cap binds only when the agent's cost
    guardrails are ENABLED. With ``cost_guardrails_enabled=false`` the run cap
    degrades to the coarse between-stage ``run_remaining <= 0`` backstop (run()
    emits a loud warning). See C6 below.

    Idempotency: per-stage keys are ``conductor:<conductor_run_id>:<stage_id>``.
    The conductor manages IdempotencyBackend directly (approach B from spec/50
    prep findings) without threading idempotency through
    dispatch_sub_goal_as_outcome() or OutcomeRunner (both gained ONLY the tree-cap
    ``parent_remaining_headroom_usd`` parameter — an unrelated cost-gate concern,
    not idempotency wiring).
    In PR1 the conductor commits idempotency AFTER the coordinator's terminal goal
    transition, so a COMPLETED idempotency record never precedes a complete goal
    ledger entry — the goal-ledger skip (above) fires first and the COMPLETED
    lookup branch below is a forward-looking defensive path (reachable only once a
    future commit-before-transition ordering, or a multi-host backend, can land
    COMPLETED while the goal still reads in_progress). The genuine PR1 hard-crash
    window (result.json written, goal not yet complete) leaves idempotency at
    IN_FLIGHT and RE-RUNS the stage as bounded single-host duplicate spend — the
    behavior the spec acknowledges for single-host crashes. It is NOT silently
    short-circuited.

    Gate stages (is_gate=True): PR1 halts immediately with
    halt_reason='gate_not_implemented_pr2'. PR2 (#581) adds suspend/resume().

    PR2 note — VALID_SUB_GOAL_STATUSES does NOT yet include 'awaiting_decision'.
    PR2 (#581) must add it to types.py + update validate_goal() before implementing
    gate suspension. Adding it in PR1 would break 25+ conformance tests.

    C7 — the conductor is NOT a delegation level. Do not invoke run() from inside
    a depth-1 delegated specialist call (spec/15 one-level bound). That would
    launder two-level delegation through the conductor and defeat spec/15 #9.
    Best-effort guard: if agent.trigger == 'delegate', emits a warning.

    Args:
        playbook: parsed PlaybookManifest.
        subject: work subject (e.g. "feature #1234", "Q3 report").
        agent: AtomicAgent instance (parent agent, provides cost universe).
        conductor_run_id: None on first call (mints a fresh UUID); on resume,
            pass the ConductorState.conductor_run_id from the prior run() call.
            Callers MUST persist and re-pass this value for crash-resume to work.
        max_stage_iterations: max iterations for each stage's outcome loop (default 3).
        judge_model: optional model override for the outcome judge.

    Returns:
        ConductorState — fresh projection from the ledger. Never cached or mutated
        between calls (C1).

    Raises:
        ValueError: when conductor_run_id fails charset validation
            (validate_goal_id), or when a conductor_run_id is supplied for resume
            but no goal.md exists at goals/<id>/ — the explicit resume-existence
            check in _resolve_or_create_run (NOT for_goal(), which only scopes
            paths and does not check goal existence).
        AtomicAgentsError: when the goal backend does not implement
            AddressableGoalBackend (multi-goal addressing required for conductor).
        GoalCorrupted: when a sub-goal referenced by the playbook is missing from
            the goal (ledger/playbook mismatch).
    """
    # C7 best-effort launder-guard: warn if invoked from a delegate trigger.
    _trigger = getattr(agent, "trigger", None)
    if _trigger == "delegate":
        _warnings.warn(
            "conductor.run() is being called from within a delegated agent (trigger='delegate'). "
            "This may launder two-level delegation through the conductor, circumventing spec/15 #9. "
            "The conductor is an orchestration top — invoke it from an operator trigger, cron, "
            "or a coordinator agent BEFORE it has delegated (spec/50 C7, OD3).",
            stacklevel=2,
        )

    # PR1 honesty: the per-stage `model` dial is parsed but NOT applied (no
    # actor-model override is plumbed through the coordinator/OutcomeRunner yet —
    # only judge_model is). Warn once so an operator who set `model:` to control
    # cost/quality is not silently given the default model (Principle #13).
    _model_stages = [s.stage_id for s in playbook.stages if s.model]
    if _model_stages:
        _warnings.warn(
            "conductor: per-stage `model:` dial is PARSED but NOT APPLIED in PR1 — "
            f"stages {_model_stages} declare a model override that will be IGNORED; "
            "the stage runs on the agent's configured model.md model. Actor-model "
            "wiring is deferred. Remove the `model:` field or accept the default.",
            stacklevel=2,
        )

    # M1 — the within-stage tree-cap binds only when the agent's cost guardrails are
    # ENABLED. _check_cost_guardrails() short-circuits to allow=True BEFORE the
    # parent_remaining_headroom_usd clamp when config.cost_guardrails_enabled is
    # False, so the threaded run-level headroom never gates a per-iteration call.
    # With guardrails off, the run cap degrades to the coarse BETWEEN-stage
    # `run_remaining <= 0` backstop only (within-stage overshoot is unbounded by the
    # run cap). Warn loudly so this degradation is never silent (Principle #4/#13).
    _guardrails_enabled = getattr(
        getattr(agent, "config", None), "cost_guardrails_enabled", True
    )
    if _guardrails_enabled is False:
        _warnings.warn(
            "conductor: agent cost guardrails are DISABLED "
            "(model.md cost_guardrails_enabled=false). The within-stage tree-cap "
            "does NOT bind — the run-level cap degrades to between-stage granularity "
            "(the coarse `run_remaining <= 0` halt), so a single stage can overshoot "
            "the run cap up to its own per-call spend before the next stage is "
            "checked. Enable cost guardrails for per-iteration run-cap enforcement "
            "(spec/50 C6, Principle #4).",
            stacklevel=2,
        )

    # Require AddressableGoalBackend for multi-goal support
    if not isinstance(agent.goal_backend, AddressableGoalBackend):
        raise AtomicAgentsError(
            f"Conductor requires a GoalBackend that implements AddressableGoalBackend "
            f"(multi-goal addressing, spec/41 #642). "
            f"The agent's goal_backend ({type(agent.goal_backend).__name__!r}) does not. "
            f"Register an AddressableGoalBackend-compatible backend (e.g. FilesystemGoalBackend)."
        )

    # KNOWN LIMITATION (#656): GoalManager.for_goal() does NOT propagate a custom
    # goal_backend injected on the parent agent to the scoped child. Capture the
    # parent backend id here so we can warn below if for_goal() silently swaps it
    # for the default filesystem backend (a silent data-routing trap).
    _parent_goal_backend_id = getattr(
        getattr(agent.goal_backend, "capabilities", lambda: None)(), "backend_id", None
    )

    # Step 1: resolve conductor_run_id
    conductor_run_id = _resolve_or_create_run(
        playbook=playbook,
        subject=subject,
        agent=agent,
        conductor_run_id=conductor_run_id,
    )

    # Step 2: build the bridged GoalManager + load the conductor goal
    # The bridged manager has the PARENT agent's agents_root/name (for cost gate /
    # OutcomeRunner construction) but the CONDUCTOR's scoped goal_backend (for
    # ledger writes). This wires the real cost universe (P0 fix: spec/50 prep finding).
    conductor_backend = agent.goal_backend.for_goal(conductor_run_id)

    # #656 warning: if for_goal() dropped a custom parent backend, the scoped
    # backend's id will differ from the parent's. Ledger transitions would then
    # write to the default filesystem backend instead of the operator's choice.
    _conductor_backend_id = getattr(
        getattr(conductor_backend, "capabilities", lambda: None)(), "backend_id", None
    )
    if (
        _parent_goal_backend_id is not None
        and _conductor_backend_id is not None
        and _parent_goal_backend_id != _conductor_backend_id
    ):
        _warnings.warn(
            f"conductor: the parent agent's goal_backend ({_parent_goal_backend_id!r}) "
            f"was NOT propagated to the conductor-scoped backend "
            f"({_conductor_backend_id!r}) by for_goal() (#656). Ledger transitions for "
            f"this run will write to {_conductor_backend_id!r}, not your pinned backend.",
            stacklevel=2,
        )

    bridged_gm = _build_bridged_goal_manager(agent, conductor_backend)

    # Load the conductor goal through the scoped backend (bypasses has_goal()
    # which checks the parent's standing goal.md, not the conductor run goal).
    conductor_goal = conductor_backend.load_goal(agent.name)
    bridged_gm._goal = conductor_goal

    # Step 3: the conductor_run_started event (carrying the pinned run_cap_usd) was
    # already written immediately after create_goal() in _resolve_or_create_run
    # (H2) for a fresh run; on resume it is already present. Defensive backstop: if
    # it is somehow absent (a crash in the one-statement window between create_goal
    # and the append), re-emit it so the pin and audit record exist before the
    # stage loop. (A resumed run that already has the event skips this.)
    history_path = agent.agent_root / "goals" / conductor_run_id / "goal_history.jsonl"
    if not _has_event(history_path, "conductor_run_started"):
        conductor_backend.append_history_event(
            agent.name,
            {
                "ts": _now_ts(),
                "event": "conductor_run_started",
                "conductor_run_id": conductor_run_id,
                "playbook_name": playbook.name,
                "subject": subject,
                "run_cap_usd": playbook.run_cap_usd,
                "stage_ids": [s.stage_id for s in playbook.stages],
            },
        )

    # Pin the run-level cost ceiling from the conductor_run_started ledger event
    # (HIGH 3 / spec/50 §Cost/§Throughline). The cap is written ONCE at run
    # creation and read back here on every run()/resume, so editing run_cap_usd in
    # PLAYBOOK.md mid-suspension does NOT change a live run's ceiling. A fresh run
    # pinned playbook.run_cap_usd at create; an existing run returns its run-start
    # value regardless of the current PLAYBOOK.md.
    run_cap_usd = _read_pinned_run_cap(history_path, default=playbook.run_cap_usd)

    # Step 4: get idempotency and outcome backends (from parent agent's root)
    idempotency_backend = _get_idempotency_backend(agent.agent_root)
    outcome_backend = _get_outcome_backend(agent.agent_root)

    # Step 5: stage sequencing loop
    completed_stage_ids: list[str] = []

    for stage in playbook.stages:
        # Gate stages: PR1 halts immediately (PR2 #581 implements resume)
        if stage.is_gate:
            _logger.info(
                "conductor: halting run %s at gate stage %r (PR2 #581 not yet implemented)",
                conductor_run_id,
                stage.stage_id,
            )
            conductor_backend.append_history_event(
                agent.name,
                {
                    "ts": _now_ts(),
                    "event": "conductor_run_halted",
                    "conductor_run_id": conductor_run_id,
                    "stage_id": stage.stage_id,
                    "reason": "gate_not_implemented_pr2",
                },
            )
            return _build_state(
                conductor_run_id=conductor_run_id,
                playbook=playbook,
                subject=subject,
                status="halted",
                halt_reason="gate_not_implemented_pr2",
                history_path=history_path,
                completed_stage_ids=completed_stage_ids,
                run_cap_usd=run_cap_usd,
            )

        # Reload the goal from the scoped backend to get fresh sub-goal status
        conductor_goal = conductor_backend.load_goal(agent.name)
        bridged_gm._goal = conductor_goal

        sg = bridged_gm.find_sub_goal(stage.stage_id)
        if sg is None:
            raise GoalCorrupted(
                f"stage {stage.stage_id!r} not found as a sub-goal in the conductor run goal. "
                f"The goal and playbook are out of sync. "
                f"conductor_run_id={conductor_run_id!r}"
            )

        # C2 — the goal ledger is the authoritative resume cursor, with the FULL
        # Contract C2 predicate enforced: a `complete` stage is skipped ONLY when
        # its stored result is resolvable from the durable store. A complete stage
        # whose result pointer (sub_goal.output) is absent, or whose result_ref
        # does not resolve via OutcomeBackend.read_result(), is ledger corruption
        # (complete-but-no-result) — FAIL CLOSED rather than silently skip a stage
        # whose output a later stage may consume (spec/50 C2 + the
        # stage-result-source ruling). The result is read from the store, never
        # re-computed (C3).
        if sg.status == "complete":
            # H1 — resolve (and if needed REPAIR) the stored result before skipping.
            # A complete stage normally carries a resolvable sub_goal.output; if that
            # pointer is missing/unresolvable we consult idempotency (which may hold
            # a COMPLETED record with a prior_result_ref the store CAN resolve) and
            # repair the pointer rather than wedging the resume. Only when NO usable
            # result exists in EITHER source is the stage genuinely corrupt → raise
            # GoalCorrupted (fail closed). Result comes from the store, never
            # re-computed (C3).
            resolved_ref = _resolve_completed_stage_ref(
                sg=sg,
                stage=stage,
                agent_name=agent.name,
                conductor_run_id=conductor_run_id,
                outcome_backend=outcome_backend,
                idempotency_backend=idempotency_backend,
                conductor_backend=conductor_backend,
                today=date.today(),
            )
            _logger.debug(
                "conductor: stage %r already complete with a resolvable result "
                "(%s), skipping (resume cursor)",
                stage.stage_id,
                resolved_ref,
            )
            completed_stage_ids.append(stage.stage_id)
            continue

        # C2 resume cursor — an `abandoned` stage is TERMINAL on resume (maintainer
        # ruling A, 2026-06-27). `abandoned` is NEVER produced by the conductor or
        # the goal-outcome coordinator (which maps terminal failure to `blocked`):
        # only a deliberate operator/external action sets `abandoned`. Auto-reviving
        # it would let crash recovery override human control flow. So the conductor
        # HALTS and returns control to the operator (requeue / skip / modify) — not a
        # silent skip, and not a normalization back to in_progress. Mirrors the other
        # terminal halt paths (dispatch_error / cost_gate_halted).
        if sg.status == "abandoned":
            _logger.info(
                "conductor: halting run %s — stage %r is abandoned (terminal on "
                "resume; operator must explicitly decide requeue/skip/modify)",
                conductor_run_id,
                stage.stage_id,
            )
            conductor_backend.append_history_event(
                agent.name,
                {
                    "ts": _now_ts(),
                    "event": "conductor_run_halted",
                    "conductor_run_id": conductor_run_id,
                    "stage_id": stage.stage_id,
                    "reason": "stage_abandoned",
                },
            )
            return _build_state(
                conductor_run_id=conductor_run_id,
                playbook=playbook,
                subject=subject,
                status="halted",
                halt_reason="stage_abandoned",
                history_path=history_path,
                completed_stage_ids=completed_stage_ids,
                run_cap_usd=run_cap_usd,
            )

        # Re-sum cumulative spend from durable ledger (C6, no process-memory carry)
        cumulative_spend, spend_degraded = _sum_cumulative_spend(history_path)

        # Fail-closed cost-read posture (#495/#497/#498): a degraded spend read
        # cannot be trusted as the run-cap baseline. Halt rather than admit more
        # spend against an under-counted total. Mirrors coordinator.py's degraded
        # handling on coordinator_dispatch_rejected.
        if spend_degraded:
            _logger.warning(
                "conductor: cumulative-spend read degraded at stage %r — halting "
                "run %s fail-closed (cannot trust the run-cap baseline)",
                stage.stage_id,
                conductor_run_id,
            )
            conductor_backend.append_history_event(
                agent.name,
                {
                    "ts": _now_ts(),
                    "event": "conductor_run_halted",
                    "conductor_run_id": conductor_run_id,
                    "stage_id": stage.stage_id,
                    "reason": "cost_data_degraded",
                    "cost_data_degraded": True,
                    "cumulative_spend_usd": cumulative_spend,
                    "run_cap_usd": run_cap_usd,
                },
            )
            return _build_state(
                conductor_run_id=conductor_run_id,
                playbook=playbook,
                subject=subject,
                status="halted",
                halt_reason="cost_data_degraded",
                history_path=history_path,
                completed_stage_ids=completed_stage_ids,
                run_cap_usd=run_cap_usd,
                cost_data_degraded=True,
            )

        # Run-level cost gate
        run_remaining = run_cap_usd - cumulative_spend
        if run_remaining <= 0:
            _logger.info(
                "conductor: run cap exhausted (spent=%.4f, cap=%.4f) at stage %r",
                cumulative_spend,
                run_cap_usd,
                stage.stage_id,
            )
            conductor_backend.append_history_event(
                agent.name,
                {
                    "ts": _now_ts(),
                    "event": "conductor_run_halted",
                    "conductor_run_id": conductor_run_id,
                    "stage_id": stage.stage_id,
                    "reason": "run_cap_exhausted",
                    "cumulative_spend_usd": cumulative_spend,
                    "run_cap_usd": run_cap_usd,
                },
            )
            return _build_state(
                conductor_run_id=conductor_run_id,
                playbook=playbook,
                subject=subject,
                status="halted",
                halt_reason="run_cap_exhausted",
                history_path=history_path,
                completed_stage_ids=completed_stage_ids,
                run_cap_usd=run_cap_usd,
            )

        # Idempotency lookup. The goal ledger (sg.status == 'complete', checked
        # above) is the SOLE resume cursor. In PR1, commit() runs AFTER the
        # terminal goal transition, so a COMPLETED record cannot precede a complete
        # goal entry — this branch is a forward-looking defensive path (reachable
        # only under a future commit-before-transition ordering or a multi-host
        # backend that lands COMPLETED while the goal still reads in_progress).
        idem_key = f"conductor:{conductor_run_id}:{stage.stage_id}"
        idem_decision = idempotency_backend.lookup(idem_key)

        if idem_decision.state == COMPLETED:
            # Defensive close: idempotency COMPLETED while goal still in_progress.
            # Read the result from the durable store (C3: never from deduped Response).
            outcome_run_id = idem_decision.prior_result_ref
            if outcome_run_id:
                try:
                    _stage_result = outcome_backend.read_result(
                        agent.name, outcome_run_id
                    )
                    _logger.info(
                        "conductor: narrow-window resume for stage %r "
                        "(idempotency COMPLETED, goal=in_progress). Closing ledger.",
                        stage.stage_id,
                    )
                    # Close the ledger (complete→complete status-preserving transition
                    # to write the outcome_run_id pointer — P1 fix)
                    _close_stage_in_ledger(
                        conductor_backend=conductor_backend,
                        agent_name=agent.name,
                        stage=stage,
                        outcome_run_id=outcome_run_id,
                        outcome_result=_stage_result,
                        conductor_run_id=conductor_run_id,
                        bridged_gm=bridged_gm,
                        today=date.today(),
                    )
                    # Emit the per-stage completion event so the narrow-window
                    # close path has the same audit shape as the normal path
                    # (both routes leave a conductor_stage_completed line).
                    conductor_backend.append_history_event(
                        agent.name,
                        {
                            "ts": _now_ts(),
                            "event": "conductor_stage_completed",
                            "conductor_run_id": conductor_run_id,
                            "stage_id": stage.stage_id,
                            "outcome_run_id": outcome_run_id,
                            "total_cost_usd": getattr(
                                _stage_result, "total_cost_usd", 0.0
                            ),
                            "iterations": len(getattr(_stage_result, "iterations", [])),
                            "narrow_window_close": True,
                        },
                    )
                    completed_stage_ids.append(stage.stage_id)
                    continue
                except Exception as exc:
                    _logger.warning(
                        "conductor: failed to read_result for narrow-window stage %r "
                        "(outcome_run_id=%r): %s. Re-running stage.",
                        stage.stage_id,
                        outcome_run_id,
                        exc,
                    )
                    # Fall through to normal dispatch (re-run the stage)

        # Emit stage-started event
        conductor_backend.append_history_event(
            agent.name,
            {
                "ts": _now_ts(),
                "event": "conductor_stage_started",
                "conductor_run_id": conductor_run_id,
                "stage_id": stage.stage_id,
                "label": stage.label,
            },
        )

        # Resume normalization (BLOCKED ONLY): the coordinator
        # (dispatch_sub_goal_as_outcome) only accepts a 'pending' or 'in_progress'
        # sub-goal — it raises GoalCorrupted on any terminal status. A prior dispatch
        # that ended max_iterations_reached or failed left this stage 'blocked' (the
        # coordinator's terminal mapping), and resuming it is correct: blocked is
        # conductor/coordinator-produced, so re-running is recovery, not overriding a
        # human. We normalize blocked → in_progress via a CAS transition before
        # re-dispatch; without it, resuming a max_iterations run would crash with an
        # opaque GoalCorrupted. ('abandoned' is NOT handled here — it is terminal on
        # resume and was already halted above, maintainer ruling A.)
        if sg.status == "blocked":
            _normalize_stage_for_rerun(
                conductor_backend=conductor_backend,
                agent_name=agent.name,
                from_status=sg.status,
                stage_id=stage.stage_id,
                conductor_run_id=conductor_run_id,
                bridged_gm=bridged_gm,
                today=date.today(),
            )

        # Acquire idempotency lease for this stage dispatch
        idem_run_id = str(uuid.uuid4())
        idem_decision = idempotency_backend.begin(idem_key, idem_run_id)

        # Note: if idem_decision.state == IN_FLIGHT (prior crash in dispatch window),
        # we proceed with the re-dispatch. The goal ledger is authoritative; if
        # sub-goal is in_progress it needs to be re-run. This is the bounded
        # duplicate-spend window the spec acknowledges for single-host hard crashes
        # (spec/50 §"Resume semantics": "exactly-once across hard crashes requires
        # the deferred IdempotencyBackend TTL sweep or a multi-host backend").

        dispatch_error: Exception | None = None
        outcome_result: OutcomeResult | None = None
        updated_sg: SubGoal | None = None

        try:
            outcome_result, updated_sg = _dispatch_stage(
                stage=stage,
                agent=agent,
                bridged_gm=bridged_gm,
                subject=subject,
                max_stage_iterations=max_stage_iterations,
                judge_model=judge_model,
                run_remaining=run_remaining,
            )
        except CostGuardrailBlocked as exc:
            dispatch_error = exc
        except Exception as exc:
            dispatch_error = exc
        finally:
            if dispatch_error is not None:
                # Release idempotency lease on dispatch failure
                try:
                    idempotency_backend.release_lease(idem_key)
                except Exception:  # pragma: no cover
                    pass

        if dispatch_error is not None:
            if isinstance(dispatch_error, CostGuardrailBlocked):
                _logger.info(
                    "conductor: cost gate halted run %s at stage %r: %s",
                    conductor_run_id,
                    stage.stage_id,
                    dispatch_error,
                )
                conductor_backend.append_history_event(
                    agent.name,
                    {
                        "ts": _now_ts(),
                        "event": "conductor_run_halted",
                        "conductor_run_id": conductor_run_id,
                        "stage_id": stage.stage_id,
                        "reason": "cost_gate_halted",
                        "error": str(dispatch_error),
                    },
                )
                return _build_state(
                    conductor_run_id=conductor_run_id,
                    playbook=playbook,
                    subject=subject,
                    status="halted",
                    halt_reason="cost_gate_halted",
                    history_path=history_path,
                    completed_stage_ids=completed_stage_ids,
                    run_cap_usd=run_cap_usd,
                )

            # MED 5 — an unexpected (non-CostGuardrailBlocked) dispatch error is a
            # terminal exit too; every other terminus emits a conductor_run_halted
            # ledger fact (Principle #5). Without this, an operator reading the
            # ledger cannot distinguish "died on an unexpected backend/coordinator
            # error" from "still running". Emit the halt fact, then re-raise.
            conductor_backend.append_history_event(
                agent.name,
                {
                    "ts": _now_ts(),
                    "event": "conductor_run_halted",
                    "conductor_run_id": conductor_run_id,
                    "stage_id": stage.stage_id,
                    "reason": "dispatch_error",
                    "error": str(dispatch_error),
                },
            )
            raise dispatch_error

        # updated_sg and outcome_result are both set (dispatch_error is None)
        assert updated_sg is not None
        assert outcome_result is not None

        # Map terminal state — halt on non-satisfied outcomes
        if updated_sg.status != "complete":
            # Stage did not satisfy: max_iterations_reached, failed, or interrupted.
            # Leave the stage in its current ledger status (re-runnable on resume).
            try:
                idempotency_backend.release_lease(idem_key)
            except Exception:  # pragma: no cover
                pass

            halt_reason = f"stage_{outcome_result.status}"
            _logger.info(
                "conductor: stage %r ended with status=%r, halting run %s",
                stage.stage_id,
                outcome_result.status,
                conductor_run_id,
            )
            conductor_backend.append_history_event(
                agent.name,
                {
                    "ts": _now_ts(),
                    "event": "conductor_run_halted",
                    "conductor_run_id": conductor_run_id,
                    "stage_id": stage.stage_id,
                    "reason": halt_reason,
                    "outcome_status": outcome_result.status,
                    "outcome_run_id": outcome_result.run_id,
                },
            )
            return _build_state(
                conductor_run_id=conductor_run_id,
                playbook=playbook,
                subject=subject,
                status="halted",
                halt_reason=halt_reason,
                history_path=history_path,
                completed_stage_ids=completed_stage_ids,
                run_cap_usd=run_cap_usd,
            )

        # Stage completed: commit idempotency + store outcome_run_id pointer
        idempotency_backend.commit(idem_key, result_ref=outcome_result.run_id)
        idempotency_backend.release_lease(idem_key)

        # Store the outcome_run_id pointer in sub_goal.output (P1 fix — prep finding P1
        # "dispatch_sub_goal_as_outcome does not store outcome_run_id in sub_goal.output").
        # This is a status-preserving complete→complete apply_transition.
        _store_outcome_pointer(
            conductor_backend=conductor_backend,
            agent_name=agent.name,
            stage_id=stage.stage_id,
            outcome_run_id=outcome_result.run_id,
            conductor_run_id=conductor_run_id,
            today=date.today(),
        )

        # Emit stage-completed event
        conductor_backend.append_history_event(
            agent.name,
            {
                "ts": _now_ts(),
                "event": "conductor_stage_completed",
                "conductor_run_id": conductor_run_id,
                "stage_id": stage.stage_id,
                "outcome_run_id": outcome_result.run_id,
                "total_cost_usd": outcome_result.total_cost_usd,
                "iterations": len(outcome_result.iterations),
            },
        )

        completed_stage_ids.append(stage.stage_id)
        _logger.info(
            "conductor: stage %r completed (outcome=%s, cost=%.4f)",
            stage.stage_id,
            outcome_result.run_id,
            outcome_result.total_cost_usd,
        )

    # All stages complete — persist the terminal completion fact (Principle #5 /
    # C1: a successful run is a durable fact, not an in-memory-only projection;
    # every halt path emits a conductor_run_halted, so the success path must emit
    # its counterpart so an operator reading the ledger can positively distinguish
    # a completed run from one that died after the last stage's events).
    _final_spend, _final_degraded = _sum_cumulative_spend(history_path)
    conductor_backend.append_history_event(
        agent.name,
        {
            "ts": _now_ts(),
            "event": "conductor_run_completed",
            "conductor_run_id": conductor_run_id,
            "cumulative_spend_usd": _final_spend,
            "cost_data_degraded": _final_degraded,
            "run_cap_usd": run_cap_usd,
            "stages_complete": len(completed_stage_ids),
        },
    )

    return _build_state(
        conductor_run_id=conductor_run_id,
        playbook=playbook,
        subject=subject,
        status="complete",
        halt_reason=None,
        history_path=history_path,
        completed_stage_ids=completed_stage_ids,
        run_cap_usd=run_cap_usd,
        cost_data_degraded=_final_degraded,
    )


# ──────────────────────────────────────────────────────────────────
# Internal helpers


def _now_ts() -> str:
    return datetime.now().astimezone().isoformat()


def _resolve_or_create_run(
    playbook: PlaybookManifest,
    subject: str,
    agent: Any,
    conductor_run_id: str | None,
) -> str:
    """Resolve or create the conductor run goal. Returns the conductor_run_id."""
    if conductor_run_id is not None:
        # Resume path: validate + confirm the goal exists
        validate_goal_id(conductor_run_id)
        conductor_goal_path = agent.agent_root / "goals" / conductor_run_id / "goal.md"
        if not conductor_goal_path.is_file():
            raise ValueError(
                f"conductor_run_id={conductor_run_id!r} supplied for resume, but "
                f"no goal found at {conductor_goal_path}. "
                f"Either the run never started, or the conductor_run_id is wrong. "
                f"Do not create a new run by omitting conductor_run_id — that would "
                f"mint a fresh UUID and start a duplicate run."
            )
        _logger.info("conductor: resuming run %s", conductor_run_id)
        return conductor_run_id

    # First invocation: mint a fresh conductor_run_id and create the goal
    new_run_id = _mint_conductor_run_id()
    _logger.info(
        "conductor: starting new run %s for playbook %r subject=%r",
        new_run_id,
        playbook.name,
        subject,
    )

    # Build the Goal object for the conductor run
    goal = _build_conductor_goal(playbook, subject, new_run_id)

    # Set dynamic attribute (getattr contract: FilesystemGoalBackend reads
    # getattr(goal, 'conductor_run_id', None) to embed in the goal_created event).
    # Goal is a plain non-frozen @dataclass (no __slots__), so a direct assignment
    # is the supported hook.
    goal.conductor_run_id = new_run_id  # type: ignore[attr-defined]  # dynamic attr

    agent.goal_backend.create_goal(agent.name, new_run_id, goal)

    # H2 — pin the run-level cap the INSTANT the goal exists. The
    # conductor_run_started event (carrying run_cap_usd) is appended here,
    # immediately after create_goal() and BEFORE any other run() work (bridged
    # manager, backend resolution, the stage loop). Previously it was emitted in a
    # later step, so a crash between goal creation and that step left goal.md with
    # NO pinned cap, and a resume would adopt the CURRENT (possibly edited)
    # PLAYBOOK.md cap. Emitting it here shrinks that window to the single statement
    # between create_goal() and this append. (A residual one-statement crash window
    # remains — true atomicity would require create_goal to write the event; out of
    # scope. _read_pinned_run_cap falls back to the parse-validated default if the
    # event is absent.)
    scoped_backend = agent.goal_backend.for_goal(new_run_id)
    scoped_backend.append_history_event(
        agent.name,
        {
            "ts": _now_ts(),
            "event": "conductor_run_started",
            "conductor_run_id": new_run_id,
            "playbook_name": playbook.name,
            "subject": subject,
            "run_cap_usd": playbook.run_cap_usd,
            "stage_ids": [s.stage_id for s in playbook.stages],
        },
    )
    return new_run_id


def _mint_conductor_run_id() -> str:
    """Mint a fresh conductor run ID (UUID4 with 'crun-' prefix for readability)."""
    return f"crun-{uuid.uuid4().hex[:16]}"


def _build_conductor_goal(
    playbook: PlaybookManifest,
    subject: str,
    conductor_run_id: str,
) -> Goal:
    """Build a Goal object representing the conductor run."""
    today = date.today().isoformat()
    sub_goals = []
    for stage in playbook.stages:
        sub_goals.append(
            SubGoal(
                id=stage.stage_id,
                label=stage.label,
                status="pending",
                body=stage.prompt,
            )
        )

    return Goal(
        schema_version=CURRENT_GOAL_SCHEMA_VERSION,
        active=True,
        intent=f"Run playbook '{playbook.name}' for subject: {subject}",
        priority="medium",
        created=today,
        last_progress_check=today,
        success_criteria=[
            f"All {len(playbook.stages)} stages of playbook '{playbook.name}' complete.",
            f"Subject: {subject}",
            f"Run cap: ${playbook.run_cap_usd:.2f} USD",
        ],
        sub_goals=sub_goals,
        body=(
            f"## Conductor Run\n\n"
            f"conductor_run_id: {conductor_run_id}\n"
            f"playbook: {playbook.name}\n"
            f"subject: {subject}\n"
            f"run_cap_usd: {playbook.run_cap_usd}\n\n"
            f"## History\n"
        ),
    )


def _build_bridged_goal_manager(agent: Any, conductor_backend: Any) -> Any:
    """Build the bridged GoalManager.

    The bridged manager uses the PARENT agent's agents_root + agent_name so that
    OutcomeRunner constructs its internal AtomicAgent at agent.agent_root (which
    has model.md with valid cost caps). The goal_backend is the conductor-scoped
    backend so ledger writes go to goals/<conductor_run_id>/ (P0 fix).

    The _goal attribute is NOT loaded here — the caller must set it directly
    via bridged_gm._goal = conductor_backend.load_goal(agent.name) to bypass
    has_goal() which checks the parent's standing goal.md path, not the conductor
    run goal path.
    """
    # Lazy import to avoid closing bootstrap cycle
    from .._goal_impl import GoalManager  # noqa: PLC0415

    gm = GoalManager(
        agents_root=agent.agents_root,
        agent_name=agent.name,
        goal_backend=conductor_backend,
    )
    return gm


def _dispatch_stage(
    stage: StageSpec,
    agent: Any,
    bridged_gm: Any,
    subject: str,
    max_stage_iterations: int,
    judge_model: str | None,
    run_remaining: float,
) -> tuple[Any, Any]:
    """Dispatch one stage as an outcome via the goal-outcome coordinator.

    Uses dispatch_sub_goal_as_outcome() with the bridged GoalManager (which
    carries the parent agent's agents_root/name for cost gate wiring and the
    conductor-scoped backend for ledger writes).

    run_remaining (= run_cap_usd − cumulative_spend, computed immediately before
    dispatch) is threaded into the coordinator as parent_remaining_headroom_usd
    so the stage's cost gate — pre-dispatch AND per-iteration inside
    OutcomeRunner — clamps to MIN(model.md remaining, run_remaining − stage spend
    so far). This is the tree-cap: the per-iteration gate decrements the threaded
    headroom by the stage's own accumulating spend, so the run cap binds at each
    iteration boundary and within-stage overshoot is bounded by one iteration's
    spend (Principle #4, spec/50 C6 — same granularity as the delegate tree-cap).
    The coarse pre-stage `run_remaining <= 0` halt in run() is the between-stage
    backstop; this clamp is the within-stage enforcement.

    Returns (OutcomeResult, SubGoal).
    """
    from ..goal.coordinator import dispatch_sub_goal_as_outcome  # noqa: PLC0415

    # Determine the rubric: stage.rubric takes precedence; fallback to prompt text
    rubric: str = stage.rubric if stage.rubric else stage.prompt

    extra_context = f"Conductor run subject: {subject}"

    return dispatch_sub_goal_as_outcome(
        agent=agent,
        goal_manager=bridged_gm,
        sub_goal_id=stage.stage_id,
        rubric=rubric,
        max_iterations=max_stage_iterations,
        extra_context=extra_context,
        judge_model=judge_model,
        parent_remaining_headroom_usd=run_remaining,
    )


def _normalize_stage_for_rerun(
    conductor_backend: Any,
    agent_name: str,
    from_status: str,
    stage_id: str,
    conductor_run_id: str,
    bridged_gm: Any,
    today: date,
) -> None:
    """Reset a terminal `blocked` stage to in_progress for a resume re-run.

    The goal-outcome coordinator only dispatches 'pending'/'in_progress' sub-goals;
    a stage that ended max_iterations_reached/failed was mapped to 'blocked' by the
    coordinator's terminal transition. Resuming a blocked stage is recovery (blocked
    is conductor/coordinator-produced), so we apply a CAS-guarded blocked →
    in_progress transition (expected_from_status=from_status serializes against a
    concurrent writer) and refresh bridged_gm._goal so the coordinator's in-memory
    sub-goal status check passes. Clears blocked_by so a stale blocker can't wedge
    the re-dispatch.

    Only `blocked` is normalized. `abandoned` is TERMINAL on resume (maintainer
    ruling A, 2026-06-27) — it is halted by run() before reaching this helper and is
    never revived, because only a deliberate operator/external action sets it.
    `from_status` is retained as a parameter for the CAS guard / audit fidelity.
    """
    updated_goal = conductor_backend.apply_transition(
        agent_id=agent_name,
        sub_goal_id=stage_id,
        to_status="in_progress",
        fields={"blocked_by": None},
        history_prose=(
            f"sub_goal `{stage_id}` reset {from_status}→in_progress "
            "for conductor resume re-run (C2: non-complete stages re-run)"
        ),
        history_event={
            "ts": _now_ts(),
            "event": "conductor_stage_reset_for_rerun",
            "sub_goal_id": stage_id,
            "from_status": from_status,
            "conductor_run_id": conductor_run_id,
        },
        expected_from_status=from_status,
        when=today,
    )
    # Refresh the in-memory goal so the coordinator's _require_sub_goal status
    # check (which reads goal_manager._goal, not disk) sees in_progress.
    bridged_gm._goal = updated_goal


def _store_outcome_pointer(
    conductor_backend: Any,
    agent_name: str,
    stage_id: str,
    outcome_run_id: str,
    conductor_run_id: str,
    today: date,
) -> None:
    """Store outcome_run_id in sub_goal.output via a status-preserving apply_transition.

    This is a complete→complete transition (same status, updates the output field).
    The CAS guard (expected_from_status='complete') serializes against concurrent writers.
    spec/50 prep finding P1: dispatch_sub_goal_as_outcome does not store outcome_run_id
    in sub_goal.output; the conductor makes this second apply_transition to write the pointer.
    """
    try:
        conductor_backend.apply_transition(
            agent_id=agent_name,
            sub_goal_id=stage_id,
            to_status="complete",
            fields={"output": outcome_run_id},
            history_prose=(
                f"sub_goal `{stage_id}` outcome pointer stored "
                f"(outcome_run_id={outcome_run_id})"
            ),
            history_event={
                "ts": _now_ts(),
                "event": "conductor_stage_result_stored",
                "sub_goal_id": stage_id,
                "outcome_run_id": outcome_run_id,
                "conductor_run_id": conductor_run_id,
            },
            expected_from_status="complete",
            when=today,
        )
    except Exception as exc:
        # Non-fatal for THIS run: pointer write failure is logged but does not halt
        # the run. The idempotency commit already landed; the stage WAS completed.
        #
        # HONEST consequence (Principle #13): there is NO JSONL-history fallback for
        # the result pointer. If this write fails, the stage is left complete-in-
        # ledger but WITHOUT a resolvable sub_goal.output, so a SUBSEQUENT resume's
        # C2 predicate (see run() resume cursor) treats it as corruption and FAILS
        # CLOSED (GoalCorrupted), rather than silently skipping a stage whose result
        # is unrecoverable. The exposure is narrow (a status-preserving complete→
        # complete apply_transition failing right after a successful terminal
        # transition); a JSONL-fallback read of outcome_run_id from the
        # conductor_stage_completed event is a possible future hardening (#580
        # follow-up), deliberately not built here.
        _logger.warning(
            "conductor: failed to store outcome pointer for stage %r "
            "(outcome_run_id=%r): %s. Continuing — stage is complete.",
            stage_id,
            outcome_run_id,
            exc,
        )


def _resolve_completed_stage_ref(
    sg: Any,
    stage: StageSpec,
    agent_name: str,
    conductor_run_id: str,
    outcome_backend: Any,
    idempotency_backend: Any,
    conductor_backend: Any,
    today: date,
) -> str:
    """Resolve (and if needed REPAIR) a complete stage's stored result (C2 / H1).

    Contract C2 skips a `complete` stage only when its result is recoverable from
    the durable store. This resolves it, trying two sources in order:

      1. ``sub_goal.output`` result_ref → ``OutcomeBackend.read_result()`` resolves
         → use it (the normal path).
      2. REPAIR from idempotency: the per-stage key's COMPLETED idempotency record
         carries a ``prior_result_ref``; if ``read_result(prior_result_ref)``
         resolves, re-store that pointer into ``sub_goal.output`` (recovering from a
         pointer-write that failed after the terminal transition — e.g.
         ``_store_outcome_pointer`` swallowed an FS error) and use it.

    Only when NEITHER source yields a resolvable result is the stage genuinely
    corrupt → raise ``GoalCorrupted`` (fail closed). Returns the resolved
    result_ref. Repairing here, rather than failing closed on a missing pointer,
    is what lets a crash that landed the result + idempotency commit but lost the
    pointer write self-heal on resume instead of wedging.
    """
    # Source 1: the stored pointer.
    output_ref = getattr(sg, "output", None)
    if output_ref:
        try:
            outcome_backend.read_result(agent_name, output_ref)
            return output_ref
        except Exception:
            # Pointer present but unresolvable — fall through to idempotency repair.
            pass

    # Source 2: repair from a COMPLETED idempotency record.
    idem_key = f"conductor:{conductor_run_id}:{stage.stage_id}"
    try:
        decision = idempotency_backend.lookup(idem_key)
    except Exception:
        decision = None
    prior_ref = getattr(decision, "prior_result_ref", None) if decision else None
    if (
        decision is not None
        and getattr(decision, "state", None) == COMPLETED
        and prior_ref
    ):
        try:
            outcome_backend.read_result(agent_name, prior_ref)
        except Exception:
            prior_ref = None
        if prior_ref:
            _store_outcome_pointer(
                conductor_backend=conductor_backend,
                agent_name=agent_name,
                stage_id=stage.stage_id,
                outcome_run_id=prior_ref,
                conductor_run_id=conductor_run_id,
                today=today,
            )
            _logger.info(
                "conductor: repaired result pointer for complete stage %r from "
                "idempotency (prior_result_ref=%r) — resume self-healed instead of "
                "failing closed.",
                stage.stage_id,
                prior_ref,
            )
            return prior_ref

    # Neither source yielded a resolvable result → genuine corruption, fail closed.
    raise GoalCorrupted(
        f"stage {stage.stage_id!r} is marked 'complete' but its result is "
        f"unrecoverable: sub_goal.output={getattr(sg, 'output', None)!r} did not "
        f"resolve, and idempotency holds no COMPLETED record with a resolvable "
        f"prior_result_ref. Contract C2 requires a complete stage's result to be "
        f"resolvable; failing closed rather than skipping a stage whose result is "
        f"unrecoverable. conductor_run_id={conductor_run_id!r}"
    )


def _close_stage_in_ledger(
    conductor_backend: Any,
    agent_name: str,
    stage: StageSpec,
    outcome_run_id: str,
    outcome_result: Any,
    conductor_run_id: str,
    bridged_gm: Any,
    today: date,
) -> None:
    """Close the ledger for a narrow-window completed stage.

    Called when idempotency shows COMPLETED but goal ledger shows in_progress:
    the crash happened after result.json was committed but before apply_transition
    (complete) landed. We apply the terminal transition to close the ledger,
    then store the outcome pointer.
    """
    # Reload from disk to get current status
    current_goal = conductor_backend.load_goal(agent_name)
    bridged_gm._goal = current_goal

    sg = bridged_gm.find_sub_goal(stage.stage_id)
    if sg is None:
        raise GoalCorrupted(
            f"stage {stage.stage_id!r} missing from goal on narrow-window close"
        )

    if sg.status == "complete":
        # Already closed by another path (idempotent)
        return

    # Apply the terminal complete transition (mirrors coordinator step 5)
    conductor_backend.apply_transition(
        agent_id=agent_name,
        sub_goal_id=stage.stage_id,
        to_status="complete",
        fields={"completed": today.isoformat()},
        history_prose=(
            f"sub_goal `{stage.stage_id}` → complete "
            f"(narrow-window close; outcome {outcome_run_id} already committed)"
        ),
        history_event={
            "ts": _now_ts(),
            "event": "sub_goal_outcome_dispatched",
            "sub_goal_id": stage.stage_id,
            "outcome_run_id": outcome_run_id,
            "terminal_state": getattr(outcome_result, "status", "satisfied"),
            "applied_status": "complete",
            "iterations": len(getattr(outcome_result, "iterations", [])),
            "total_cost_usd": getattr(outcome_result, "total_cost_usd", 0.0),
        },
        expected_from_status="in_progress",
        when=today,
    )

    # Store the outcome pointer
    _store_outcome_pointer(
        conductor_backend=conductor_backend,
        agent_name=agent_name,
        stage_id=stage.stage_id,
        outcome_run_id=outcome_run_id,
        conductor_run_id=conductor_run_id,
        today=today,
    )


def _sum_cumulative_spend(history_path: Path) -> tuple[float, bool]:
    """Re-sum cumulative spend from durable goal_history.jsonl JSONL events.

    Sums ``total_cost_usd`` across ALL ``sub_goal_outcome_dispatched`` events
    REGARDLESS of ``applied_status``. The coordinator emits exactly one terminal
    ``sub_goal_outcome_dispatched`` event per dispatch attempt THAT REACHES ITS
    TERMINAL apply_transition, carrying that attempt's real spend on every branch
    (complete / blocked / in_progress). A stage that runs, spends real money, then
    ends non-complete halts the run and is re-dispatched on resume; counting only
    ``complete`` attempts would let a flapping stage's spend escape the run cap on
    every resume cycle — a fail-OPEN cap escape (spec/50 C6, Principle #4).
    Counting all terminated attempts is fail-closed for that window: spend
    happened irrespective of terminal status, and there is no double-count risk
    because each terminated attempt emits exactly one event.

    BOUNDED fail-OPEN window (Principle #13 — stated, not glossed): the spend of a
    dispatch attempt that crashed mid-LLM-call BEFORE its terminal
    ``sub_goal_outcome_dispatched`` event landed is NOT counted here — that spend
    was written to the parent agent's daily/monthly model.md ledger (which this
    function never reads), not to goal_history.jsonl. So on resume the run-cap
    baseline under-counts that crashed attempt's partial spend; the model.md
    daily/monthly cap is the backstop. Same narrow single-host hard-crash window
    spec/50 §"Resume semantics" names for duplicate runs. A terminal apply_transition
    that itself fails AFTER runner.run() already spent is the symmetric case
    (coordinator-side): spent-but-unrecorded, also backstopped by model.md.

    Returns (total, degraded). ``degraded`` is True when the read could not be
    trusted (whole-file OSError, an unparseable line, a non-numeric cost, OR a
    NON-FINITE cost — `NaN`/`inf`, which would poison run_remaining = cap − total
    and defeat the `<= 0` halt; see the C1/C2 cost-bypass guard). Per the
    framework's fail-closed cost-read posture (#495 / #497 / #498), the run-cap
    gate treats degraded as a halt rather than admitting more spend against an
    under-counted (or poisoned) baseline. Returns (0.0, False) when the file is
    absent/empty.
    """
    total = 0.0
    degraded = False
    for rec, ok in _iter_history_events(history_path):
        if not ok:
            # Whole-file read failure or an unreadable/non-object line — an event
            # (possibly a real spend) is unreadable → fail-closed.
            degraded = True
            continue
        if rec.get("event") == "sub_goal_outcome_dispatched":
            try:
                value = float(rec.get("total_cost_usd", 0.0))
            except (TypeError, ValueError):
                degraded = True
                continue
            if not math.isfinite(value):
                # C2 — a NaN/inf per-event cost must NOT propagate into the sum
                # (cap − nan = nan, and nan <= 0 is False, so the run would NEVER
                # halt). Treat it as a degraded read (which fails the run closed)
                # rather than letting a non-finite escape the cap.
                degraded = True
                continue
            total += value

    return total, degraded


def _read_pinned_run_cap(history_path: Path, default: float) -> float:
    """Return the run_cap_usd pinned in the conductor_run_started ledger event.

    HIGH 3 / spec/50 §Cost/§Throughline: the run-level ceiling is pinned at run
    creation (recorded in the conductor_run_started event, written immediately
    after create_goal — see _resolve_or_create_run / H2) and read back here on
    every run()/resume, so a mid-suspension edit to PLAYBOOK.md cannot silently
    change a live run's cap — a resumed run runs under the SAME run-level ceiling.
    A fresh run's started event was just written from playbook.run_cap_usd, so
    this returns that same value; an existing run returns its run-start value
    regardless of the current PLAYBOOK.md. Falls back to ``default`` only if the
    event is absent/unreadable/non-numeric/non-finite (defensive).
    """
    for rec, ok in _iter_history_events(history_path):
        if not ok:
            continue
        if rec.get("event") == "conductor_run_started":
            try:
                cap = float(rec.get("run_cap_usd"))
            except (TypeError, ValueError):
                return default
            # C2 — never adopt a non-finite/≤0 pinned cap (a poisoned ledger);
            # fall back to the parse-validated default instead.
            if not math.isfinite(cap) or cap <= 0:
                return default
            return cap
    return default


def _has_event(history_path: Path, event_name: str) -> bool:
    """Return True if the history file contains an event with the given name."""
    return any(
        ok and rec.get("event") == event_name
        for rec, ok in _iter_history_events(history_path)
    )


def _iter_history_events(history_path: Path) -> Iterator[tuple[dict | None, bool]]:
    """Yield ``(record, ok)`` for each line of a goal_history.jsonl file (MA2).

    The single hardened JSONL reader shared by ``_sum_cumulative_spend``,
    ``_read_pinned_run_cap``, and ``_has_event`` — handles is_file / OSError /
    blank-line / JSONDecodeError / non-object uniformly:

    - An absent/empty file yields nothing (NOT an error).
    - A whole-file read failure (OSError) yields a single ``(None, False)`` then
      stops — the degraded signal callers that need it (``_sum_cumulative_spend``)
      key fail-closed off ``ok=False``.
    - An unparseable line OR a parsed-but-non-object line yields ``(None, False)``.
    - A parsed JSON object yields ``(rec, True)``.

    Search-only callers (``_has_event`` / ``_read_pinned_run_cap``) simply ignore
    ``ok=False`` units; the cost reader treats any ``ok=False`` as degraded.
    """
    if not history_path.is_file():
        return
    try:
        text = history_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _logger.warning("conductor: cannot read history %s: %s", history_path, exc)
        yield None, False
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            yield None, False
            continue
        if isinstance(rec, dict):
            yield rec, True
        else:
            yield None, False


def _get_idempotency_backend(agent_root: Path) -> Any:
    """Get the default idempotency backend for the parent agent root."""
    from ..idempotency import get_default_idempotency_backend  # noqa: PLC0415

    return get_default_idempotency_backend(agent_root)


def _get_outcome_backend(agent_root: Path) -> Any:
    """Get the default outcome backend for the parent agent root."""
    from ..outcome import get_default_outcome_backend  # noqa: PLC0415

    return get_default_outcome_backend(agent_root)


def _build_state(
    conductor_run_id: str,
    playbook: PlaybookManifest,
    subject: str,
    status: str,
    halt_reason: str | None,
    history_path: Path,
    completed_stage_ids: list[str],
    run_cap_usd: float | None = None,
    cost_data_degraded: bool = False,
) -> ConductorState:
    """Build a fresh ConductorState projection from the ledger.

    ``run_cap_usd`` is the PINNED run-level cap (read from the
    conductor_run_started ledger event, HIGH 3); callers pass it so the
    projection reports the run-start ceiling, not the (possibly edited) live
    PLAYBOOK.md value. Falls back to ``playbook.run_cap_usd`` only when a caller
    cannot supply it (defensive).

    ``cost_data_degraded`` lets a caller OR-compose a degraded observation made
    earlier in the run (e.g. the in-loop fail-closed halt) with this final
    cumulative-spend read, so the surfaced flag is never under-reported (#498).
    """
    cumulative_spend, degraded = _sum_cumulative_spend(history_path)
    return ConductorState(
        conductor_run_id=conductor_run_id,
        playbook_name=playbook.name,
        subject=subject,
        status=status,
        halt_reason=halt_reason,
        stages_total=len(playbook.stages),
        stages_complete=len(completed_stage_ids),
        cumulative_spend_usd=cumulative_spend,
        run_cap_usd=playbook.run_cap_usd if run_cap_usd is None else run_cap_usd,
        completed_stage_ids=list(completed_stage_ids),
        cost_data_degraded=cost_data_degraded or degraded,
    )

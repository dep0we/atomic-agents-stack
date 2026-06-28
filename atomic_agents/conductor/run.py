"""Conductor run engine — free functions run() + resume() + helpers (spec/50 PR1+PR2).

This module is the orchestration core: it sequences automated PLAYBOOK.md stages,
writes the durable goal ledger, and resumes from it on crash-restart.

C1 — The conductor holds no authoritative state of its own. Every durable fact
lives in Goal / Outcome / Idempotency. run() reconstructs the run's place
entirely from those stores on every invocation.

C2 — The goal ledger is the authoritative resume cursor. A stage whose sub-goal
is 'complete' or 'skipped' (PR2) is terminal-done and skipped on resume;
'in_progress'/'pending' is (re-)run; 'blocked' is normalized and re-run;
'awaiting_decision' (PR2) is re-surfaced (return the pending GateDecision);
'abandoned' is TERMINAL on resume (the run halts — never auto-revived, ruling A).

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

PR2 (#581) — Gate stages + resume(). Gate stages (is_gate=True) suspend the run
via 'awaiting_decision' sub-goal status. resume() injects a typed disposition
(continue/skip/halt) and continues. Gate answer semantics: disposition is a TYPED
field — NOT magic-word-sniffed from the free-text answer string.

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

import hashlib
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

from ..exceptions import (
    AtomicAgentsError,
    ConductorConflictScanError,
    CostGuardrailBlocked,
    GoalConcurrentModification,
    GoalCorrupted,
    LockBusy,
    UnverifiedPrincipalConversationAccess,
)
from ..goal.backend import AddressableGoalBackend
from ..goal.types import (
    CURRENT_GOAL_SCHEMA_VERSION,
    Goal,
    SubGoal,
    validate_goal_id,
)
from ..idempotency.types import COMPLETED
from .types import ConductorState, GateDecision, PlaybookManifest, StageSpec

_logger = logging.getLogger(__name__)

# PR3 (#582): bounded blocking wait for the shared conflict-scan lease. The
# critical section it guards is one goal scan + one apply_transition write, so a
# healthy contender holds it for well under a second; 30s is a generous ceiling
# that converts a stuck holder into a loud LockBusy (caller retries) rather than
# an unbounded hang.
_CONFLICT_SCAN_LOCK_TIMEOUT_S = 30.0


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

    Per-run serialization (PR3 #582): run() acquires a per-conductor_run_id
    LockBackend lease immediately after resolving the run_id. Concurrent run()
    calls on the SAME conductor_run_id race to acquire the lock; the loser
    receives LockBusy (the caller should retry after a backoff). This eliminates
    the PR1 double-dispatch window (two concurrent invocations double-spending
    under independent cumulative-spend sums). resume() does NOT acquire this
    lock — the per-goal CAS in _record_gate_answer() serializes gate writes,
    and the run() call at the end of resume() acquires the lock internally.

    Conflict serialization (PR3 #582): a gate stage with non-empty
    conflict_keys blocks any concurrent run() that would also gate on an
    overlapping key. The blocked run enqueues an advisory record via
    QueueBackend.enqueue() and returns ConductorState(status='deferred',
    queued_behind_decision_id=..., queued_behind_conductor_run_id=...). The
    caller should poll run() again after the blocking gate is answered — the
    deferred run self-releases on its next invocation when the blocking gate is
    no longer 'awaiting_decision' (ledger-is-primary pattern: no push-release
    from resume()).

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

    Gate stages (is_gate=True): PR2 (#581) — run() suspends on a gate by
    transitioning the stage sub-goal to 'awaiting_decision' and returning
    ConductorState(status='awaiting_decision', pending_decision=GateDecision(...)).
    Call conductor.resume() with the decision_id to answer and continue.

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

    # PR3 (#582): per-run serialization lock. Acquired AFTER conductor_run_id is
    # resolved so the lock key is stable. Concurrent run() calls on the SAME
    # conductor_run_id race here; the loser receives LockBusy (caller should retry).
    _run_lock_backend = agent.lock_backend.scope("conductor-runs")
    try:
        _run_lock_handle = _run_lock_backend.acquire(conductor_run_id, timeout=0.0)
    except LockBusy as _lbe:
        raise LockBusy(
            f"conductor: run {conductor_run_id!r} is already executing in a "
            f"concurrent invocation; retry after that invocation returns "
            f"(spec/50 PR3 per-run serialization guard)."
        ) from _lbe

    # PR3 (#582): hold the per-run lock for the WHOLE run body and release it
    # EXPLICITLY in the finally below, on every exit path (return OR exception).
    # The lock is NOT released by GC — LockHandle has no __del__ and backend_state
    # is a bare fd int (atomic_agents/locks/filesystem.py), so dropping the handle
    # closes nothing. Without this try/finally the fcntl lock + fd would leak until
    # process exit and a second sequential run() on the same conductor_run_id (the
    # deferred->poll re-entry is a second sequential run(), an external caller
    # re-invocation, NOT in-code recursion) would dead-lock with LockBusy.
    # resume() does NOT acquire this "conductor-runs" lease, so a leaked run() lease
    # cannot block resume(); resume()'s stale/duplicate safety rides on the inner
    # apply_transition CAS, not this per-run lease.
    try:
        # Step 2: build the bridged GoalManager + load the conductor goal
        # The bridged manager has the PARENT agent's agents_root/name (for cost gate /
        # OutcomeRunner construction) but the CONDUCTOR's scoped goal_backend (for
        # ledger writes). This wires the real cost universe (P0 fix: spec/50 prep finding).
        conductor_backend = agent.goal_backend.for_goal(conductor_run_id)

        # #656 warning: if for_goal() dropped a custom parent backend, the scoped
        # backend's id will differ from the parent's. Ledger transitions would then
        # write to the default filesystem backend instead of the operator's choice.
        _conductor_backend_id = getattr(
            getattr(conductor_backend, "capabilities", lambda: None)(),
            "backend_id",
            None,
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
        history_path = (
            agent.agent_root / "goals" / conductor_run_id / "goal_history.jsonl"
        )
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
                    # P0-2 — pin the structure here too (the crash-window re-emit);
                    # the event was absent, so the live playbook is the run-start one.
                    "playbook_fingerprint": _compute_playbook_fingerprint(playbook),
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

        # P0-2 — pin the playbook STRUCTURE across suspend/resume, not just run_cap_usd.
        # The fingerprint of the ordered control-flow tuples (stage_id, is_gate, prompt/
        # prompt_ref, options, rubric_ref) is recorded in conductor_run_started at
        # creation; on every run()/resume of an EXISTING run we recompute it from the
        # LIVE playbook and REFUSE on mismatch. A multi-day gate-suspended (or merely
        # in-flight) run MUST execute the playbook it started with — an operator editing
        # PLAYBOOK.md (prompts, gate flags, added/removed/reordered stages) mid-run
        # cannot silently change the resumed run's control flow. This also makes the
        # awaiting_decision re-surface (below) consistent: the gate stage spec the
        # resume reconstructs from is verified-identical to the one that suspended.
        # A fresh run just pinned the live fingerprint, so it never mismatches; a run
        # started before this pin existed has pinned_fingerprint=None → check skipped
        # (backward-compat — we cannot refuse a structure that was never pinned).
        _refuse_if_playbook_changed(playbook, history_path, conductor_run_id)

        # Step 4: get idempotency and outcome backends (from parent agent's root)
        idempotency_backend = _get_idempotency_backend(agent.agent_root)
        outcome_backend = _get_outcome_backend(agent.agent_root)

        # PR3 (#582): Check for deferred-state re-entry BEFORE the stage loop. If this
        # run was previously deferred (has a conductor_run_queued event without a
        # subsequent conductor_queue_released), check if the blocking gate is still
        # awaiting_decision. If still blocked → return 'deferred' again (ledger-is-primary:
        # the deferred run self-releases by re-checking the gate status on each invocation,
        # no push-release from resume()). If unblocked → record conductor_queue_released
        # and continue to the stage loop (self-release path).
        _queued_event = _find_queued_event(history_path)
        if _queued_event is not None:
            _blocking_did = _queued_event.get("blocking_decision_id", "")
            _blocking_rid = _queued_event.get("blocking_conductor_run_id", "")
            # A1 (fail-closed): _is_decision_still_pending returns True on any goal
            # read failure, so a transient read error keeps B deferred rather than
            # self-releasing it INTO the conflicting resource. B retries on its next tick.
            _still_blocked = _is_decision_still_pending(
                agent=agent,
                decision_id=_blocking_did,
                blocking_run_id=_blocking_rid,
            )
            if _still_blocked:
                _logger.info(
                    "conductor: run %s is still deferred behind decision %s; "
                    "call run() again after that gate is answered",
                    conductor_run_id,
                    _blocking_did,
                )
                # Re-derive completed_stage_ids from the authoritative goal — do NOT
                # hardcode [] (a run that completed automated stages before queuing at
                # a later gate must keep reporting them; matches the resume(halt)
                # projection and the in-loop 'deferred' return). The sub-goal id IS the
                # stage_id.
                _queued_goal = conductor_backend.load_goal(agent.name)
                _queued_status_by_id = {
                    sg.id: sg.status for sg in _queued_goal.sub_goals
                }
                _queued_completed = [
                    s.stage_id
                    for s in playbook.stages
                    if _queued_status_by_id.get(s.stage_id) in ("complete", "skipped")
                ]
                return _build_state(
                    conductor_run_id=conductor_run_id,
                    playbook=playbook,
                    subject=subject,
                    status="deferred",
                    halt_reason=None,
                    history_path=history_path,
                    completed_stage_ids=_queued_completed,
                    run_cap_usd=run_cap_usd,
                    queued_behind_decision_id=_blocking_did,
                    queued_behind_conductor_run_id=_blocking_rid,
                )
            else:
                # Gate answered — self-release: record the release event and continue.
                conductor_backend.append_history_event(
                    agent.name,
                    {
                        "ts": _now_ts(),
                        "event": "conductor_queue_released",
                        "conductor_run_id": conductor_run_id,
                        "blocking_decision_id": _blocking_did,
                        "blocking_conductor_run_id": _blocking_rid,
                    },
                )
                _logger.info(
                    "conductor: run %s self-released from conflict queue "
                    "(blocking gate %s is no longer awaiting_decision); "
                    "continuing stage loop",
                    conductor_run_id,
                    _blocking_did,
                )

        # Step 5: stage sequencing loop
        completed_stage_ids: list[str] = []

        for stage in playbook.stages:
            # PR2: ALWAYS load the goal FIRST so all status branches read fresh disk state.
            # The PR1 gate-halt block (is_gate check before goal load) is REMOVED — it
            # would win over the new status-based branching and never let the cursor logic
            # run (a dead-code trap, prep finding P0 at run.py:340-367).
            # The safe structural order is:
            #   load goal → find sub-goal → check awaiting_decision (re-surface) →
            #   check complete/skipped (skip) → check abandoned (halt) →
            #   check is_gate+pending (first suspension) → check blocked (normalize) → dispatch.

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

            # PR2 — C2/D2 gate suspension re-surface (P0-1): a stage already suspended
            # (awaiting_decision) is RE-SURFACED from the DURABLE status, not from the
            # audit event. The authoritative resume cursor is sg.status +
            # sg.gate_decision_id (goal.md). The conductor_gate_pending event is pure
            # append-only AUDIT and is written AFTER goal.md (apply_transition MUST-6
            # ordering), so a crash in that window leaves status='awaiting_decision' +
            # gate_decision_id with NO event — an UNRESUMABLE run if we hard-required
            # the event. Instead we reconstruct the pending GateDecision from goal.md +
            # the (fingerprint-pinned, so verified-identical) playbook gate stage spec,
            # re-derive context_ref from the ledger, and HEAL the audit (re-append the
            # event) when it is absent — never raise GoalCorrupted for a merely-missing
            # audit event. (This handles crash-then-run(), the cron-re-trigger-while-
            # suspended path, AND the MUST-6 audit-write crash window.)
            if sg.status == "awaiting_decision":
                decision_id = sg.gate_decision_id
                if not decision_id:
                    # awaiting_decision with NO gate_decision_id is GENUINE durable
                    # corruption — the resume cursor itself (goal.md) is unreadable,
                    # not a merely-absent audit event.
                    raise GoalCorrupted(
                        f"stage {stage.stage_id!r} is 'awaiting_decision' but carries "
                        f"no gate_decision_id on the sub-goal — the durable resume "
                        f"cursor is unreadable. conductor_run_id={conductor_run_id!r}"
                    )
                # B1: the durable sub-goal is authoritative for held_conflict_keys.
                # Read it from sg (NOT hardcoded []) so the audit record + the
                # returned GateDecision match goal.md — a gate suspended with a
                # non-empty held set must re-surface it on heal/re-entry.
                held_conflict_keys = list(sg.held_conflict_keys or [])
                pending_event = _find_gate_pending_event(
                    history_path, stage.stage_id, decision_id
                )
                if pending_event is not None:
                    context_ref = pending_event.get("context_ref", "")
                else:
                    # MUST-6 crash window: status landed durably, the audit event did
                    # not. Re-derive context_ref from the ledger and HEAL the audit by
                    # re-appending the conductor_gate_pending event (it is pure audit,
                    # consistent with D2). Do NOT raise GoalCorrupted.
                    context_ref = _derive_context_ref(
                        completed_stage_ids=completed_stage_ids,
                        conductor_backend=conductor_backend,
                        agent_name=agent.name,
                        history_path=history_path,
                        conductor_run_id=conductor_run_id,
                    )
                    # B2 (heal CAS guard): resume() does NOT take the per-run lock,
                    # so a concurrent gate answer could have moved this sub-goal out
                    # of 'awaiting_decision' between the load above and this heal
                    # append. Re-read the durable status and SKIP the heal append if
                    # it has moved — never write a 'pending' audit line AFTER the gate
                    # was already answered (that would corrupt the append-only log).
                    _heal_goal = conductor_backend.load_goal(agent.name)
                    _heal_sg = next(
                        (s for s in _heal_goal.sub_goals if s.id == stage.stage_id),
                        None,
                    )
                    if _heal_sg is not None and _heal_sg.status == "awaiting_decision":
                        conductor_backend.append_history_event(
                            agent.name,
                            {
                                "ts": _now_ts(),
                                "event": "conductor_gate_pending",
                                "conductor_run_id": conductor_run_id,
                                "stage_id": stage.stage_id,
                                "decision_id": decision_id,
                                "prompt": stage.prompt,
                                "options": list(stage.options),
                                "context_ref": context_ref,
                                # B1: heal with the authoritative durable held set.
                                "held_conflict_keys": held_conflict_keys,
                                # Marker so an operator/audit can see this event was
                                # healed (re-appended on resume) rather than written
                                # at suspension.
                                "healed_missing_audit": True,
                            },
                        )
                        _logger.warning(
                            "conductor: healed a missing conductor_gate_pending audit "
                            "event for stage %r (decision_id=%s) in run %s — status was "
                            "durably 'awaiting_decision' but the audit event was absent "
                            "(MUST-6 crash window). Reconstructed from goal.md + playbook.",
                            stage.stage_id,
                            decision_id,
                            conductor_run_id,
                        )
                    else:
                        _logger.info(
                            "conductor: skipped healing the conductor_gate_pending audit "
                            "event for stage %r (decision_id=%s) in run %s — the sub-goal "
                            "is no longer 'awaiting_decision' (a concurrent answer landed "
                            "between load and heal). Not appending a pending-after-answered "
                            "audit line.",
                            stage.stage_id,
                            decision_id,
                            conductor_run_id,
                        )
                existing_gd = GateDecision(
                    decision_id=decision_id,
                    stage_id=stage.stage_id,
                    prompt=stage.prompt,
                    options=list(stage.options),
                    context_ref=context_ref,
                    held_conflict_keys=held_conflict_keys,
                )
                _logger.info(
                    "conductor: run %s suspended at gate stage %r "
                    "(decision_id=%s), re-surfacing",
                    conductor_run_id,
                    stage.stage_id,
                    existing_gd.decision_id,
                )
                return _build_state(
                    conductor_run_id=conductor_run_id,
                    playbook=playbook,
                    subject=subject,
                    status="awaiting_decision",
                    halt_reason=None,
                    history_path=history_path,
                    completed_stage_ids=completed_stage_ids,
                    run_cap_usd=run_cap_usd,
                    pending_decision=existing_gd,
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
            # PR2: 'skipped' is terminal-done (a gate was answered with disposition='skip').
            # No result to resolve — the stage was deliberately not dispatched.
            if sg.status == "skipped":
                # C3 symmetry — a 'skipped' stage MUST carry a recorded
                # conductor_gate_answered (disposition='skip') ruling. This is the
                # symmetric partner of the complete-without-result corruption check
                # below: a 'skipped' status with no skip ruling in the audit trail is
                # ledger corruption (spec/50 C4 "a skipped stage is a recorded ruling,
                # never an absent stage"), so fail closed rather than silently treat an
                # unexplained 'skipped' as terminal-done.
                if not _has_gate_answered_skip(history_path, stage.stage_id):
                    raise GoalCorrupted(
                        f"stage {stage.stage_id!r} is 'skipped' but no "
                        f"conductor_gate_answered (disposition='skip') ruling is "
                        f"recorded in the ledger. A skipped stage MUST be a recorded "
                        f"gate ruling (spec/50 C4); failing closed rather than treating "
                        f"an unexplained 'skipped' as terminal-done. "
                        f"conductor_run_id={conductor_run_id!r}"
                    )
                _logger.debug(
                    "conductor: stage %r is skipped (gate ruling), treating as terminal-done",
                    stage.stage_id,
                )
                completed_stage_ids.append(stage.stage_id)
                continue

            if sg.status == "complete":
                # PR2: gate stages answered with disposition='continue' are marked 'complete'
                # but they have NO outcome_run_id / sub_goal.output — the human's answer
                # is the "output", not a dispatched outcome. Skip the H1 result-pointer
                # resolution for gate stages (it would always fail: no pointer to resolve).
                if stage.is_gate:
                    _logger.debug(
                        "conductor: gate stage %r is complete (human 'continue' ruling), "
                        "skipping H1 result-pointer resolution (no outcome_run_id)",
                        stage.stage_id,
                    )
                    completed_stage_ids.append(stage.stage_id)
                    continue

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

            # PR2 — Gate stage first-suspension path (sg.status is 'pending' or 'in_progress').
            # Cost gate fires BEFORE the suspension write (C6 compliance — a gate suspension
            # must not be committed when run_remaining <= 0).
            if stage.is_gate:
                # Re-sum cumulative spend (C6, no process-memory carry — same as automated path)
                cumulative_spend, spend_degraded = _sum_cumulative_spend(history_path)
                if spend_degraded:
                    _logger.warning(
                        "conductor: cumulative-spend read degraded at gate stage %r — "
                        "halting run %s fail-closed",
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
                run_remaining = run_cap_usd - cumulative_spend
                if run_remaining <= 0:
                    _logger.info(
                        "conductor: run cap exhausted (spent=%.4f, cap=%.4f) at gate stage %r",
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

                # Suspend the gate: mint a decision_id, transition to awaiting_decision,
                # write the conductor_gate_pending audit event.
                decision_id = f"gate-{uuid.uuid4().hex[:16]}"
                # Derive context_ref: use the last completed stage's outcome_run_id pointer,
                # or fall back to the goal history path if no prior stage completed.
                context_ref = _derive_context_ref(
                    completed_stage_ids=completed_stage_ids,
                    conductor_backend=conductor_backend,
                    agent_name=agent.name,
                    history_path=history_path,
                    conductor_run_id=conductor_run_id,
                )

                # PR3 (#582): conflict serialization (OD1b). Before suspending a gate
                # that declares conflict_keys we must (a) scan for an active gate on
                # ANOTHER run holding an overlapping key and (b) write our own
                # awaiting_decision claim — and (a)+(b) MUST be atomic ACROSS CONCURRENT
                # RUNS. The per-run lock does NOT give us that: two different runs hold
                # DIFFERENT per-run locks (keyed on their own conductor_run_id), so
                # without a shared lock both could scan (each seeing no conflict because
                # neither has written its claim yet) and both suspend — a double-suspend
                # holding the same key, the exact TOCTOU the headline guarantee forbids.
                # We therefore serialize scan+claim under a SINGLE shared conflict-scan
                # lease (scope 'conductor-conflict-scan', fixed key 'scan').
                #
                # A single GLOBAL key — NOT a per-keyset digest — is REQUIRED for
                # correctness: two runs conflict iff their key SETS overlap, but
                # overlapping sets can hash to different digests ({a,b} vs {b,c}), so
                # only one shared global lock guarantees mutual exclusion across any
                # overlap. The brief over-serialization of disjoint-key scans is
                # acceptable (the critical section is one scan + one write).
                #
                # Subscribe-then-check: the advisory enqueue + the conductor_run_queued
                # event are written before we return 'deferred'; a concurrent gate answer
                # in that window is handled by self-release on the next run() invocation
                # (ledger-is-primary, no push-release).
                _conflict_scan_backend = None
                _conflict_scan_handle = None
                if stage.conflict_keys:
                    _conflict_scan_backend = agent.lock_backend.scope(
                        "conductor-conflict-scan"
                    )
                    _conflict_scan_handle = _conflict_scan_backend.acquire(
                        "scan", timeout=_CONFLICT_SCAN_LOCK_TIMEOUT_S
                    )
                try:
                    # Scan UNDER the conflict-scan lock so a concurrent suspending run
                    # cannot slip its own claim in between our scan and our write.
                    _active_conflict = (
                        _scan_active_conflicts(
                            agent=agent,
                            stage_conflict_keys=stage.conflict_keys,
                            own_conductor_run_id=conductor_run_id,
                        )
                        if stage.conflict_keys
                        else None
                    )
                    if _active_conflict is not None:
                        _blocking_run_id, _blocking_decision_id = _active_conflict
                        # Advisory conflict record — write-only / audit. Nothing
                        # dequeues it; self-release is ledger-driven (see the
                        # self-release path on the next run() call). The queue role IS
                        # the blocking decision_id; item_name is our (sanitized) run_id.
                        try:
                            _cq_backend = _get_conductor_conflict_queue_backend(
                                agent.agent_root
                            )
                            _queue_item_name = conductor_run_id.replace(":", "-")[:64]
                            # conductor_run_id is charset-validated ([a-z0-9_-]), so it is
                            # already a bare component; the replace()/hash fallback is a
                            # defensive guard for any non-slug-safe id a custom caller
                            # might pass (no path separators, not empty, not '.'/'..').
                            from ..queue.filesystem import (
                                _validate_bare_component as _vbc,
                            )

                            try:
                                _vbc(_queue_item_name, "item_name")
                            except Exception:
                                _queue_item_name = hashlib.sha256(
                                    conductor_run_id.encode()
                                ).hexdigest()[:32]
                            _cq_payload = json.dumps(
                                {
                                    "conductor_run_id": conductor_run_id,
                                    "blocking_decision_id": _blocking_decision_id,
                                    "playbook_name": playbook.name,
                                    "subject": subject,
                                }
                            ).encode("utf-8")
                            _cq_backend.enqueue(
                                role=_blocking_decision_id,
                                item_name=_queue_item_name,
                                payload=_cq_payload,
                            )
                        except Exception as _eq_exc:
                            _logger.warning(
                                "conductor: conflict-queue enqueue failed for run %s "
                                "(non-fatal, self-release still works on next run() call): %s",
                                conductor_run_id,
                                _eq_exc,
                            )
                        # Record the queued event in the conductor ledger
                        conductor_backend.append_history_event(
                            agent.name,
                            {
                                "ts": _now_ts(),
                                "event": "conductor_run_queued",
                                "conductor_run_id": conductor_run_id,
                                "stage_id": stage.stage_id,
                                "blocking_decision_id": _blocking_decision_id,
                                "blocking_conductor_run_id": _blocking_run_id,
                                "conflict_keys": list(stage.conflict_keys),
                            },
                        )
                        _logger.info(
                            "conductor: run %s queued at gate stage %r — blocked behind "
                            "decision %s (run %s holds conflicting key(s) %r); "
                            "call run() again after that gate is answered",
                            conductor_run_id,
                            stage.stage_id,
                            _blocking_decision_id,
                            _blocking_run_id,
                            list(stage.conflict_keys),
                        )
                        return _build_state(
                            conductor_run_id=conductor_run_id,
                            playbook=playbook,
                            subject=subject,
                            status="deferred",
                            halt_reason=None,
                            history_path=history_path,
                            completed_stage_ids=completed_stage_ids,
                            run_cap_usd=run_cap_usd,
                            queued_behind_decision_id=_blocking_decision_id,
                            queued_behind_conductor_run_id=_blocking_run_id,
                        )

                    # No active conflict — write our awaiting_decision claim WHILE STILL
                    # HOLDING the conflict-scan lock, so a concurrent run's scan is forced
                    # to observe this claim (it blocks on 'scan' until we release here).
                    # The CAS guard (expected_from_status) additionally catches a
                    # concurrent per-run write between the load and the transition.
                    conductor_backend.apply_transition(
                        agent_id=agent.name,
                        sub_goal_id=stage.stage_id,
                        to_status="awaiting_decision",
                        fields={
                            "gate_decision_id": decision_id,
                            # PR3 (#582): copy stage.conflict_keys onto the sub-goal so
                            # a future conflict scan reads them from one load_goal().
                            "held_conflict_keys": list(stage.conflict_keys),
                        },
                        history_prose=(
                            f"sub_goal `{stage.stage_id}` suspended awaiting gate decision "
                            f"(decision_id={decision_id})"
                        ),
                        history_event={
                            "ts": _now_ts(),
                            "event": "conductor_gate_pending",
                            "conductor_run_id": conductor_run_id,
                            "stage_id": stage.stage_id,
                            "decision_id": decision_id,
                            "prompt": stage.prompt,
                            "options": list(stage.options),
                            "context_ref": context_ref,
                            "held_conflict_keys": list(stage.conflict_keys),
                        },
                        expected_from_status=sg.status,
                        when=date.today(),
                    )
                finally:
                    # Release the shared conflict-scan lease on every exit path (the
                    # 'deferred' return above, the awaiting_decision write, or an
                    # exception). The durable awaiting_decision claim has already
                    # landed by here, so a concurrent run's scan will see it. The lock
                    # is NOT released by GC — release() explicitly here.
                    if _conflict_scan_handle is not None:
                        _conflict_scan_backend.release(_conflict_scan_handle)

                gate_decision = GateDecision(
                    decision_id=decision_id,
                    stage_id=stage.stage_id,
                    prompt=stage.prompt,
                    options=list(stage.options),
                    context_ref=context_ref,
                    held_conflict_keys=list(stage.conflict_keys),
                )
                _logger.info(
                    "conductor: run %s suspended at gate stage %r "
                    "(decision_id=%s); call conductor.resume() to continue",
                    conductor_run_id,
                    stage.stage_id,
                    decision_id,
                )
                return _build_state(
                    conductor_run_id=conductor_run_id,
                    playbook=playbook,
                    subject=subject,
                    status="awaiting_decision",
                    halt_reason=None,
                    history_path=history_path,
                    completed_stage_ids=completed_stage_ids,
                    run_cap_usd=run_cap_usd,
                    pending_decision=gate_decision,
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
                                "iterations": len(
                                    getattr(_stage_result, "iterations", [])
                                ),
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
    finally:
        # Release the per-run lease explicitly (NOT via GC — see the
        # acquire comment above). Idempotent release() makes this safe on
        # the success path and on any propagating exception.
        _run_lock_backend.release(_run_lock_handle)


# ──────────────────────────────────────────────────────────────────
# Public resume entry point (PR2 #581)


def resume(
    playbook: PlaybookManifest,
    subject: str,
    agent: Any,
    conductor_run_id: str,
    decision_id: str,
    answer: str,
    *,
    disposition: str,
    rationale: str,
    principal: Any | None = None,
    max_stage_iterations: int = 3,
    judge_model: str | None = None,
) -> ConductorState:
    """Answer a suspended gate and continue the playbook from the next stage.

    Resume semantics (spec/50 C2 + c5-stale-duplicate-rejection ruling):

    1. Re-read the ledger + CAS-confirm the run is still suspended on
       ``decision_id`` (atomic under the goal lock — uses apply_transition's
       expected_from_status='awaiting_decision' + gate_decision_id field check).
    2. Append ``conductor_gate_answered`` under the same transition (transition-
       first per spec/41 MUST 6 — the answer event is embedded in apply_transition
       so the status change and the audit record are one atomic unit).
    3. Transition the gate sub-goal to its final status based on ``disposition``:
       - 'continue' → 'complete' (run proceeds to the next stage)
       - 'skip' → 'skipped' (gate is terminal-done-without-result; run continues)
       - 'halt' → 'abandoned' (run halts; operator must decide next action)
    4. Delegate to run(..., conductor_run_id=conductor_run_id) to continue.

    A stale or duplicate answer (wrong decision_id, or gate already answered)
    raises GoalConcurrentModification (no write) — the gate is NOT re-opened.

    H3 — 'skip' vs 'continue' are RUNTIME-IDENTICAL in PR2: both proceed to the
    next stage. The ONLY difference is the gate's OWN recorded audit status —
    'skipped' vs 'complete'. Neither skips a downstream guarded stage; the richer
    "skip the guarded work" semantic is deferred to the reference playbook (#584).
    The control flow is honestly the same; only the audit label differs.

    C2 — an unverified principal is HARD-REFUSED (UnverifiedPrincipalConversation-
    Access) before any ledger write. The gate-ruling author MUST be a verified
    identity (LOCAL_PRINCIPAL for the home shape, a serve-derived verified
    Principal for org). Keyed on is_verified ONLY (mirrors agent.call()).

    Args:
        playbook: parsed PlaybookManifest (same object as the original run() call).
        subject: work subject (same as original run()).
        agent: AtomicAgent instance (parent agent, same as original run()).
        conductor_run_id: the ConductorState.conductor_run_id from the suspended run.
        decision_id: GateDecision.decision_id from the pending ConductorState.
        answer: the human's free-text answer (recorded in audit; NOT threaded into
            later stage prompts — D3=A, deferred to #584).
        disposition: the typed gate ruling — 'continue', 'skip', or 'halt'.
            DISTINCT typed field, NEVER magic-word-sniffed from answer string
            (gate-answer-semantics ruling).
        rationale: required keyword — the human's stated reason for the ruling.
            Recorded in the conductor_gate_answered audit event.
        principal: a spec/48 Principal object (defaults to LOCAL_PRINCIPAL).
            answered_by is set from principal.identifier (the stable identity).
            MUST be is_verified=True (C2 HARD-REFUSE) — the gate-ruling author may
            not be an unverified identity.
        max_stage_iterations: forwarded to run().
        judge_model: forwarded to run().

    Returns:
        ConductorState — the state after continuing. May be 'complete', 'halted',
        'awaiting_decision' (another gate further in the playbook), or 'deferred'
        (PR3 #582: a later gate is blocked behind another run's conflicting gate —
        the continue/skip dispositions delegate to run(), which can return 'deferred').

    Raises:
        ValueError: if disposition is not 'continue'/'skip'/'halt', or if
            rationale/answer is empty/whitespace-only.
        UnverifiedPrincipalConversationAccess: if principal.is_verified is False
            (C2 — the gate-ruling author must be a verified identity).
        GoalConcurrentModification: if decision_id does not match the current
            pending gate decision (stale/duplicate answer rejected).
        AtomicAgentsError: if the goal backend doesn't implement AddressableGoalBackend.
    """
    from ..principal.types import LOCAL_PRINCIPAL  # noqa: PLC0415

    if disposition not in ("continue", "skip", "halt"):
        raise ValueError(
            f"disposition must be 'continue', 'skip', or 'halt'; got {disposition!r}"
        )

    # rationale is the load-bearing audit 'why' (spec/50 C4; acceptance: every gate
    # ruling is queryable with rationale). Reject whitespace-only/empty so a hollow
    # ruling cannot be silently recorded (Principle #5). answer carries the human's
    # decision content and is likewise required to be non-empty.
    if not rationale or not rationale.strip():
        raise ValueError(
            "rationale must be a non-empty, non-whitespace string "
            "(spec/50 C4: every gate ruling records a rationale)."
        )
    if not answer or not answer.strip():
        raise ValueError(
            "answer must be a non-empty, non-whitespace string "
            "(the human's gate decision is recorded in the audit trail)."
        )

    if principal is None:
        principal = LOCAL_PRINCIPAL
    # C2 — HARD-REFUSE an unverified principal BEFORE deriving answered_by. The
    # 'who approved this human gate' record is the load-bearing audit field
    # (spec/50 C4); it MUST NOT be attributable to an unverified identity. Mirrors
    # agent.call()'s HARD-REFUSE gate (spec/48 MUST 10): key on is_verified ONLY —
    # never on object identity with LOCAL_PRINCIPAL (a fabricated LOCAL_PRINCIPAL-
    # shaped object with is_verified=False must be caught). LOCAL_PRINCIPAL is
    # is_verified=True by construction (the home operator IS the caller), so the
    # zero-config home path passes; a serve-layer-derived verified org principal
    # passes; an unverified/raw caller claim is refused before any ledger write.
    if not principal.is_verified:
        raise UnverifiedPrincipalConversationAccess(
            "Gate answer refused: principal.is_verified is False. The "
            "conductor_gate_answered audit record attributes the human approval to "
            "principal.identifier — an unverified identity MUST NOT be recorded as "
            "the gate-ruling author (spec/50 C4, spec/48 MUST 10). Pass a verified "
            "Principal (serve layer) or the home-user LOCAL_PRINCIPAL.",
            principal_id=getattr(principal, "identifier", None),
        )
    # Read identifier directly (no getattr-'local' fallback): a wrong-typed object
    # (PrincipalBackend, raw string, etc.) must fail loud, not silently misattribute
    # a human ruling to the local operator in the conductor_gate_answered audit line.
    answered_by = principal.identifier

    if not isinstance(agent.goal_backend, AddressableGoalBackend):
        raise AtomicAgentsError(
            f"Conductor requires a GoalBackend that implements AddressableGoalBackend. "
            f"The agent's goal_backend ({type(agent.goal_backend).__name__!r}) does not."
        )

    validate_goal_id(conductor_run_id)
    conductor_goal_path = agent.agent_root / "goals" / conductor_run_id / "goal.md"
    if not conductor_goal_path.is_file():
        raise ValueError(
            f"conductor_run_id={conductor_run_id!r} supplied for resume, but "
            f"no goal found at {conductor_goal_path}."
        )

    conductor_backend = agent.goal_backend.for_goal(conductor_run_id)

    # P0-2 — refuse a gate answer against an EDITED playbook BEFORE recording the
    # ruling (no mutation on a structure-changed resume). Mirrors the run() check;
    # placed before _record_gate_answer so the gate is not transitioned under a
    # changed structure.
    history_path_for_pin = (
        agent.agent_root / "goals" / conductor_run_id / "goal_history.jsonl"
    )
    _refuse_if_playbook_changed(playbook, history_path_for_pin, conductor_run_id)

    # Determine the final sub-goal status and gate sub-goal from disposition.
    if disposition == "continue":
        final_status = "complete"
    elif disposition == "skip":
        final_status = "skipped"
    else:  # halt
        final_status = "abandoned"

    answered_at = _now_ts()

    # _record_gate_answer performs the atomic CAS + audit write.
    _record_gate_answer(
        conductor_backend=conductor_backend,
        agent_name=agent.name,
        decision_id=decision_id,
        answer=answer,
        answered_by=answered_by,
        answered_at=answered_at,
        rationale=rationale,
        disposition=disposition,
        final_status=final_status,
        conductor_run_id=conductor_run_id,
    )

    if disposition == "halt":
        # Gate answered with halt disposition — record the halt and return.
        history_path = (
            agent.agent_root / "goals" / conductor_run_id / "goal_history.jsonl"
        )
        history_path_obj = history_path
        run_cap_usd = _read_pinned_run_cap(
            history_path_obj, default=playbook.run_cap_usd
        )
        conductor_backend.append_history_event(
            agent.name,
            {
                "ts": _now_ts(),
                "event": "conductor_run_halted",
                "conductor_run_id": conductor_run_id,
                "reason": "gate_halt_disposition",
                "decision_id": decision_id,
            },
        )
        # Re-derive completed_stage_ids from the authoritative ledger — do NOT
        # hardcode [] (P1: a run that completed automated stages before the gate
        # must report them; the just-abandoned gate is correctly excluded since
        # its status is 'abandoned', not in the terminal-done set). The sub-goal
        # id IS the stage_id (run() matches via find_sub_goal(stage.stage_id)).
        goal = conductor_backend.load_goal(agent.name)
        status_by_id = {sg.id: sg.status for sg in goal.sub_goals}
        completed_stage_ids = [
            s.stage_id
            for s in playbook.stages
            if status_by_id.get(s.stage_id) in ("complete", "skipped")
        ]
        return _build_state(
            conductor_run_id=conductor_run_id,
            playbook=playbook,
            subject=subject,
            status="halted",
            halt_reason="gate_halt_disposition",
            history_path=history_path_obj,
            completed_stage_ids=completed_stage_ids,
            run_cap_usd=run_cap_usd,
        )

    # For continue/skip: delegate to run() with the same conductor_run_id to proceed.
    return run(
        playbook=playbook,
        subject=subject,
        agent=agent,
        conductor_run_id=conductor_run_id,
        max_stage_iterations=max_stage_iterations,
        judge_model=judge_model,
    )


def _record_gate_answer(
    conductor_backend: Any,
    agent_name: str,
    decision_id: str,
    answer: str,
    answered_by: str,
    answered_at: str,
    rationale: str,
    disposition: str,
    final_status: str,
    conductor_run_id: str,
) -> None:
    """Atomically record a gate answer and transition the sub-goal.

    Private seam for resume(). Implements the c5-stale-duplicate-rejection ruling.

    PR3 (#582): the decision_id CAS is now atomic with the write. The outer unlocked
    load_goal() still finds the sub-goal by gate_decision_id, but apply_transition()
    now also receives expected_decision_id=decision_id — verified UNDER the goal
    lock after load_goal() and before the write (spec/41 MUST 14). A concurrent
    resume() call that races past the outer status check but arrives at apply_transition()
    with the same decision_id will be caught by the inner CAS: if the first call
    already cleared gate_decision_id to None, the second call sees None != decision_id
    and raises GoalConcurrentModification.

    Transition-first (spec/41 MUST 6): the conductor_gate_answered event is passed
    as the history_event to apply_transition, so the goal.md status change and the
    JSONL audit record are ONE atomic unit under the lock. No separate
    append_history_event call is made for the answer event — that would allow a
    crash between writes to produce an orphan audit line for a status never written.

    PR3 (#582): held_conflict_keys are CLEARED in the transition_fields so the
    sub-goal no longer advertises its conflict keys after the gate is answered.
    This prevents conflict scans on concurrent runs from seeing a stale conflict.

    Raises:
        GoalConcurrentModification: if the sub-goal is not 'awaiting_decision' OR
            if the stored gate_decision_id does not match decision_id. No write occurs.
    """
    # Load the goal to find the gate stage — needed to locate the sub-goal id.
    # The CAS guard (expected_from_status='awaiting_decision') inside apply_transition
    # will re-verify the status under the lock.
    goal = conductor_backend.load_goal(agent_name)
    gate_sg = next(
        (s for s in goal.sub_goals if s.gate_decision_id == decision_id), None
    )
    if gate_sg is None:
        # No sub-goal with this decision_id — either stale or wrong run.
        raise GoalConcurrentModification(
            f"No sub-goal with gate_decision_id={decision_id!r} found in run "
            f"{conductor_run_id!r}. The decision may have already been answered "
            f"or the decision_id is incorrect."
        )
    if gate_sg.status != "awaiting_decision":
        raise GoalConcurrentModification(
            f"sub_goal '{gate_sg.id}' with gate_decision_id={decision_id!r} "
            f"is no longer 'awaiting_decision' (current status: {gate_sg.status!r}). "
            f"Stale or duplicate answer rejected."
        )

    sub_goal_id = gate_sg.id

    # Build the conductor_gate_answered audit event — embedded in apply_transition
    # so the status change and the event are atomic (spec/41 MUST 6 / prep finding P1).
    conductor_gate_answered_event = {
        "ts": answered_at,
        "event": "conductor_gate_answered",
        "conductor_run_id": conductor_run_id,
        "stage_id": sub_goal_id,
        "decision_id": decision_id,
        "answer": answer,
        "answered_by": answered_by,
        "answered_at": answered_at,
        "rationale": rationale,
        "disposition": disposition,
    }

    # Fields to write to the sub-goal alongside the status change.
    # PR3 (#582): clear held_conflict_keys so conflict scans no longer see
    # this sub-goal as an active conflict holder after the gate is answered.
    transition_fields: dict[str, Any] = {
        "gate_decision_id": None,
        "held_conflict_keys": [],
    }
    if final_status == "complete":
        from datetime import date as _date  # noqa: PLC0415

        transition_fields["completed"] = _date.today().isoformat()

    # apply_transition: CAS (expected_from_status='awaiting_decision' +
    # expected_decision_id=decision_id) + enum gate + MUST 6 ordering — all under
    # the goal lock. The embedded history_event makes the answer audit atomic with
    # the status change. PR3 (#582): expected_decision_id adds the MUST 14 inner-lock
    # CAS so a stale duplicate resume that races past the outer status check is
    # caught by the inner decision_id comparison (spec/41 MUST 14).
    conductor_backend.apply_transition(
        agent_id=agent_name,
        sub_goal_id=sub_goal_id,
        to_status=final_status,
        fields=transition_fields,
        history_prose=(
            f"sub_goal `{sub_goal_id}` gate answered → {final_status} "
            f"(disposition={disposition}, answered_by={answered_by})"
        ),
        history_event=conductor_gate_answered_event,
        expected_from_status="awaiting_decision",
        expected_decision_id=decision_id,
        when=date.today(),
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
            # P0-2 — pin the playbook STRUCTURE alongside the cost ceiling so a
            # mid-suspension edit to PLAYBOOK.md cannot silently change a live
            # run's control flow on resume (recomputed + refused on mismatch).
            "playbook_fingerprint": _compute_playbook_fingerprint(playbook),
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


def _find_gate_pending_event(
    history_path: Path, stage_id: str, decision_id: str
) -> dict | None:
    """Return the most recent conductor_gate_pending audit event for a gate, or None.

    Matches on BOTH ``stage_id`` AND ``decision_id`` (P0-1 / P0-2: the gate stage
    spec is pinned by fingerprint, so the audit event for the live decision_id is
    the one to reconstruct context from). Returns None when no matching event
    exists — the MUST-6 crash window where status='awaiting_decision' landed but
    the audit event did not. The caller HEALS that window rather than treating it
    as corruption (the durable cursor is goal.md status + gate_decision_id, not the
    audit event — D2). It is purely append-only AUDIT.
    """
    last_event: dict | None = None
    for rec, ok in _iter_history_events(history_path):
        if (
            ok
            and rec.get("event") == "conductor_gate_pending"
            and rec.get("stage_id") == stage_id
            and rec.get("decision_id") == decision_id
        ):
            last_event = rec  # keep scanning to find the LAST one
    return last_event


def _has_gate_answered_skip(history_path: Path, stage_id: str) -> bool:
    """Return True iff a conductor_gate_answered (disposition='skip') event exists.

    C3 symmetry: a 'skipped' sub-goal is a RECORDED gate ruling (spec/50 C4 — "a
    skipped stage is a recorded ruling, never an absent stage"). The resume cursor
    must verify the ruling is present, symmetric with the complete-without-result
    corruption check — a 'skipped' status with no conductor_gate_answered (skip)
    audit is ledger corruption, not a valid terminal-done skip.
    """
    for rec, ok in _iter_history_events(history_path):
        if (
            ok
            and rec.get("event") == "conductor_gate_answered"
            and rec.get("stage_id") == stage_id
            and rec.get("disposition") == "skip"
        ):
            return True
    return False


def _compute_playbook_fingerprint(playbook: PlaybookManifest) -> str:
    """Stable hash of the playbook's control-flow STRUCTURE (P0-2).

    Hashes the ordered (stage_id, is_gate, effective prompt, prompt_ref, options,
    rubric_ref, conflict_keys) tuples so a resume can REFUSE a playbook edited
    mid-suspension. run_cap pinning already protects cost across suspension; this
    extends the same durability guarantee to control flow (prompts, gate flags,
    stage add/remove/reorder, a changed referenced prompt-file body — the effective
    ``prompt`` text is hashed, so a prompt_ref whose target file changed is caught
    too). PR3 (#582): conflict_keys added to the fingerprint so editing a gate's
    conflict_keys mid-suspension is also caught.

    A NUL record separator after each stage makes the digest length-independent
    (so e.g. concatenation collisions across stage boundaries are not possible).
    """
    h = hashlib.sha256()
    for s in playbook.stages:
        part_dict: dict[str, Any] = {
            "stage_id": s.stage_id,
            "is_gate": bool(s.is_gate),
            "prompt": s.prompt,
            "prompt_ref": s.prompt_ref,
            "options": list(s.options),
            "rubric_ref": s.rubric_ref,
        }
        # PR3 (#582): include conflict_keys in the structural fingerprint so an
        # operator editing them on a suspended gate mid-run is refused. Added
        # ONLY when non-empty: a stage carrying conflict_keys can only have been
        # authored under PR3 (the field is new), so its pin already includes them;
        # every pre-PR3 (keyless) stage hashes byte-identically to its PR2 pin,
        # preserving cross-version resume compat (Principle #14) across the
        # PR2→PR3 upgrade boundary.
        if s.conflict_keys:
            part_dict["conflict_keys"] = list(s.conflict_keys)
        part = json.dumps(part_dict, sort_keys=True, ensure_ascii=True)
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _read_pinned_fingerprint(history_path: Path) -> str | None:
    """Return the playbook_fingerprint pinned in conductor_run_started, or None.

    None when the event is absent OR carries no fingerprint (a run started before
    P0-2 shipped) — the caller skips the structure-change refusal in that case
    (backward-compat: a structure that was never pinned cannot be refused).
    """
    for rec, ok in _iter_history_events(history_path):
        if not ok:
            continue
        if rec.get("event") == "conductor_run_started":
            fp = rec.get("playbook_fingerprint")
            return fp if isinstance(fp, str) and fp else None
    return None


def _refuse_if_playbook_changed(
    playbook: PlaybookManifest, history_path: Path, conductor_run_id: str
) -> None:
    """REFUSE (raise) when the live playbook structure differs from the pinned one.

    P0-2 — a suspended or in-flight conductor run MUST execute the playbook it
    started with. Called by BOTH run() (re-entrant resume) and resume() (gate
    answer) BEFORE any state mutation, so an operator editing PLAYBOOK.md
    mid-suspension cannot silently change the resumed run's control flow. A run
    started before P0-2 shipped has no pinned fingerprint → check skipped
    (backward-compat: a structure never pinned cannot be refused).
    """
    pinned_fingerprint = _read_pinned_fingerprint(history_path)
    if pinned_fingerprint is None:
        return
    live_fingerprint = _compute_playbook_fingerprint(playbook)
    if pinned_fingerprint != live_fingerprint:
        raise AtomicAgentsError(
            f"conductor run {conductor_run_id!r} was started with a different "
            f"PLAYBOOK.md structure than the one now on disk (pinned fingerprint "
            f"{pinned_fingerprint[:12]}…, live {live_fingerprint[:12]}…). A "
            f"suspended or in-flight conductor run MUST execute the playbook it "
            f"started with — editing prompts, gate flags, rubric refs, or adding/"
            f"removing/reordering stages mid-run is refused (spec/50 §Cost/"
            f"§Throughline, P0-2). Revert PLAYBOOK.md to its run-start structure to "
            f"resume this run, or start a fresh run."
        )


def _derive_context_ref(
    completed_stage_ids: list[str],
    conductor_backend: Any,
    agent_name: str,
    history_path: Path,
    conductor_run_id: str,
) -> str:
    """Derive the context_ref for a gate suspension.

    context_ref is the opaque reference to the prior-stage output the human should
    review. Derived at suspension time, NOT operator-authored in markdown
    (gate-stage-markdown-schema ruling).

    Strategy: if any prior stage completed, use the last completed stage's
    sub_goal.output (the outcome_run_id pointer). Falls back to the
    goal_history.jsonl path string when no prior stage completed (e.g. the gate
    is first in the playbook).
    """
    if completed_stage_ids:
        # Read the last completed stage's sub-goal to get its outcome_run_id pointer.
        try:
            goal = conductor_backend.load_goal(agent_name)
            last_id = completed_stage_ids[-1]
            last_sg = next((s for s in goal.sub_goals if s.id == last_id), None)
            if last_sg is not None and last_sg.output:
                return last_sg.output
        except Exception:
            pass  # Fall through to the path fallback
    # No prior completed stage (gate is first) — use the history path.
    return str(history_path)


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
    pending_decision: GateDecision | None = None,
    queued_behind_decision_id: str | None = None,
    queued_behind_conductor_run_id: str | None = None,
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

    ``pending_decision`` is set when status='awaiting_decision'. The __post_init__
    invariant on ConductorState enforces this relationship (ValueError if violated).

    ``queued_behind_decision_id`` and ``queued_behind_conductor_run_id`` are both
    set when status='deferred' (PR3 #582). The __post_init__ invariant on
    ConductorState enforces this relationship.
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
        pending_decision=pending_decision,
        queued_behind_decision_id=queued_behind_decision_id,
        queued_behind_conductor_run_id=queued_behind_conductor_run_id,
    )


# ──────────────────────────────────────────────────────────────────
# PR3 (#582): Conflict serialization helpers


def _get_conductor_conflict_queue_backend(agent_root: Path) -> Any:
    """Factory for the per-agent conflict-queue QueueBackend.

    Uses agent_root as the project_root for the FilesystemQueueBackend so
    all conductor conflict queue entries for an agent are co-located under
    agent_root/queue/. This diverges from the cascade QueueBackend (which
    is project-scoped, not agent-scoped) but is intentional: conductor
    conflict queues track per-agent gate suspensions, not shared work items.
    """
    from ..queue.filesystem import FilesystemQueueBackend  # noqa: PLC0415

    return FilesystemQueueBackend(agent_root)


def _scan_active_conflicts(
    agent: Any,
    stage_conflict_keys: tuple[str, ...],
    own_conductor_run_id: str,
) -> tuple[str, str] | None:
    """Scan all goals for an active gate suspension with overlapping conflict keys.

    Iterates all goal_ids for this agent. For each goal, loads it and checks
    whether any sub-goal is in 'awaiting_decision' status with held_conflict_keys
    that overlap with stage_conflict_keys. Skips the run's own conductor_run_id
    (a run cannot block itself).

    Returns (blocking_conductor_run_id, blocking_decision_id) if found, or None.

    Complexity: O(n_goals) goal loads — acceptable for typical conductor fleets
    where n_goals is small (~1-10 active conductor runs per agent). For larger
    fleets, a secondary index is a future optimization.

    Fail-closed (A2): this scan runs ONLY for a conflict-keyed stage. If it
    cannot reliably complete (``list_goals`` raises, or a per-goal ``load_goal``
    raises for a goal that might hold an overlapping key), it raises
    ``ConductorConflictScanError`` rather than treating the unreadable holder as
    "no conflict" and entering the exclusive stage. ``run()`` is re-entrant, so
    the next scheduled trigger retries the scan. (A no-conflict-keys home run
    never calls this function — the caller gates it behind ``stage.conflict_keys``.)
    """
    if not stage_conflict_keys:
        return None
    stage_key_set = set(stage_conflict_keys)
    try:
        all_goal_ids = agent.goal_backend.list_goals(agent.name)
    except Exception as exc:
        # A2: cannot enumerate the goal universe — we cannot prove the absence of
        # a conflicting holder. Fail CLOSED: refuse to enter the exclusive stage.
        raise ConductorConflictScanError(
            f"conductor: conflict scan could not enumerate goals for agent "
            f"{agent.name!r} while checking conflict keys "
            f"{sorted(stage_key_set)!r}; refusing to enter the exclusive stage "
            f"(fail-closed). Retry on the next scheduled trigger."
        ) from exc
    for goal_id in all_goal_ids:
        if goal_id == own_conductor_run_id:
            continue
        try:
            scoped_backend = agent.goal_backend.for_goal(goal_id)
            goal = scoped_backend.load_goal(agent.name)
        except Exception as exc:
            # A2: an unreadable goal MIGHT be holding an overlapping key. Treating
            # it as "no conflict" would let this run execute the exclusive stage
            # concurrently with a holder it could not read. Fail CLOSED.
            raise ConductorConflictScanError(
                f"conductor: conflict scan could not read goal {goal_id!r} (which "
                f"may hold an overlapping conflict key) while checking "
                f"{sorted(stage_key_set)!r}; refusing to enter the exclusive stage "
                f"(fail-closed). Retry on the next scheduled trigger."
            ) from exc
        for sg in goal.sub_goals:
            if sg.status != "awaiting_decision":
                continue
            if not sg.held_conflict_keys:
                continue
            if stage_key_set.intersection(sg.held_conflict_keys):
                # B3: a holder in 'awaiting_decision' with a falsy gate_decision_id
                # is a malformed blocker — the deferred run could never verify it is
                # still pending (a blank id defeats _is_decision_still_pending and
                # would trigger premature self-release). Fail CLOSED rather than
                # emit a blank blocking id.
                if not sg.gate_decision_id:
                    raise ConductorConflictScanError(
                        f"conductor: goal {goal_id!r} holds an overlapping conflict "
                        f"key in status 'awaiting_decision' but carries NO "
                        f"gate_decision_id — a malformed blocker. Refusing to enter "
                        f"the exclusive stage (fail-closed); repair the holder's ledger."
                    )
                return (goal_id, sg.gate_decision_id)
    return None


def _find_queued_event(history_path: Path) -> dict[str, Any] | None:
    """Return the most-recent conductor_run_queued event that is not yet released.

    A queued run has a conductor_run_queued event without a subsequent
    conductor_queue_released event. Returns the queued event dict if found,
    or None if this run is not currently queued.
    """
    queued_event: dict[str, Any] | None = None
    for rec, ok in _iter_history_events(history_path):
        if not ok:
            continue
        ev = rec.get("event")
        if ev == "conductor_run_queued":
            queued_event = rec
        elif ev == "conductor_queue_released":
            queued_event = None  # This queued event was already released
    return queued_event


def _is_decision_still_pending(
    agent: Any,
    decision_id: str,
    blocking_run_id: str,
) -> bool:
    """Return True if the blocking gate decision is still awaiting_decision.

    Loads the specific blocking run's goal and checks whether any sub-goal has
    gate_decision_id == decision_id AND status == 'awaiting_decision'.

    Falls back to scanning all goals if the specific blocking run's goal cannot
    be loaded (crash recovery / deleted run).

    Fail-closed (A1): this function is consumed by the deferred->poll re-entry to
    decide whether to SELF-RELEASE a deferred run into the conflicting resource.
    A read failure must NEVER be read as "gate answered, release B". We return
    False (release) ONLY when a read SUCCEEDS and authoritatively shows the gate
    is no longer awaiting_decision. On ANY unrecoverable read failure we return
    True (still-pending) so the deferred run STAYS deferred and retries on its
    next tick.
    """
    if not decision_id:
        # B3: a blank decision id can never be PROVEN answered. The scan no longer
        # emits one, but if a malformed queued event persisted one, fail closed —
        # stay deferred rather than self-release on an unverifiable blocker.
        return True
    # Authoritative path: load the SPECIFIC blocking run's goal. If it carries the
    # decision, that goal is the authority — a successful read here is conclusive.
    _direct_goal = None
    try:
        scoped_backend = agent.goal_backend.for_goal(blocking_run_id)
        _direct_goal = scoped_backend.load_goal(agent.name)
    except Exception:
        _direct_goal = None
    if _direct_goal is not None:
        for sg in _direct_goal.sub_goals:
            if sg.gate_decision_id == decision_id:
                # Found the gate in its own run's goal — authoritative read.
                return sg.status == "awaiting_decision"
        # Read succeeded but this goal does not carry the decision; fall through
        # to a full scan (crash recovery / moved run) rather than concluding
        # "answered" from a goal that simply does not hold this decision.
    # Fallback: scan all goals (handles a deleted/moved blocking run). A read
    # failure HERE must also fail closed — never return False on an incomplete scan.
    try:
        all_goal_ids = agent.goal_backend.list_goals(agent.name)
    except Exception:
        # Cannot enumerate — cannot prove the gate is answered. Fail closed.
        return True
    scan_clean = True
    for goal_id in all_goal_ids:
        try:
            scoped = agent.goal_backend.for_goal(goal_id)
            goal = scoped.load_goal(agent.name)
        except Exception:
            # An unreadable goal might be the one holding the gate. Mark the scan
            # incomplete so we fail closed below instead of releasing prematurely.
            scan_clean = False
            continue
        for sg in goal.sub_goals:
            if sg.gate_decision_id == decision_id and sg.status == "awaiting_decision":
                return True
    if not scan_clean:
        # The full scan could not read every goal, so we cannot prove the gate is
        # answered. Fail closed: stay deferred and retry on the next tick.
        return True
    return False

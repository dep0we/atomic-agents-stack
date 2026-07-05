"""Goal-outcome coordinator — thin free function (spec/41).

This module provides ``dispatch_sub_goal_as_outcome()``, a thin free function
that composes GoalBackend + OutcomeRunner with a fail-closed pre-dispatch cost
gate (CLAUDE.md Principle #4). (NB: spec/41 MUST 10 is the apply_transition
compare-and-set guard used at step 5 — NOT the cost gate.)

The coordinator is NOT a GoalBackend method — it needs both the backend AND
the runtime (AtomicAgent, OutcomeRunner) simultaneously, and making it a
Protocol method would invert the dependency direction (the backend must not
depend on the runtime). See spec/41 §"Goal-outcome composition".

Shape (spec/41 §"Goal-outcome composition"):
    1. Validate sub-goal pre-conditions (pending/in_progress, no unresolved
       blocked_by).
    2. Pre-dispatch fail-closed cost gate: agent._check_cost_guardrails(
       critical=False) — NO surrounding try/except, full fail-closed.
       On not result.allow: append coordinator_dispatch_rejected event to
       goal_history.jsonl FIRST, THEN raise CostGuardrailBlocked.
       If the append itself fails (IOError), let the IO error propagate (do
       NOT dispatch, do NOT treat it as CostGuardrailBlocked).
    3. apply_transition(pending→in_progress) [lock taken+released].
       Uses event name "sub_goal_outcome_started" (NOT sub_goal_outcome_dispatched
       — the terminal transition owns that name to satisfy the frozen test).
    4. OutcomeRunner.run() WITHOUT holding the goal lock (run is multi-minute).
    5. apply_transition(terminal, expected_from_status='in_progress')
       [lock taken+released]. Terminal event name: "sub_goal_outcome_dispatched"
       with provenance fields (outcome_run_id, terminal_state, applied_status,
       iterations, total_cost_usd). One atomic write — provenance atomic with status.

Terminal-state mapping:
    satisfied              → complete   (fields={completed: today.isoformat()})
    max_iterations_reached → blocked    (fields={blocked_by: None})
    failed                 → blocked    (fields={blocked_by: None})
    interrupted            → in_progress (status stays; CAS passes because goal
                                          is still in_progress; audit line lands)

Pre-dispatch cost gate bound (CLAUDE.md Principle #4 caveat):
    _check_cost_guardrails(critical=False) checks model.md caps only.
    Policy-layer caps (_policy_snapshot_this_call) are None outside of
    agent.call() and are NOT enforced at the coordinator level — the same
    bound that applies to OutcomeRunner's per-iteration gate. Mock
    _check_cost_guardrails directly in tests; do NOT reconstruct the policy
    stack (false assurance).

Import cycle guard:
    OutcomeRunner is imported INSIDE the function body via a lazy in-function
    import (the goal package uses a __getattr__ bootstrap cycle to avoid
    circular imports; a module-level import of OutcomeRunner here would close
    that cycle and break bootstrap). The same pattern dispatch_as_outcome uses
    in _goal_impl.py. AtomicAgent is NOT imported here at all — it is passed in
    as the ``agent`` parameter (the caller/CLI constructs it), so only
    OutcomeRunner needs the lazy guard.

Public import path:
    from atomic_agents.goal.coordinator import dispatch_sub_goal_as_outcome
    # or via the goal package (eager re-export):
    from atomic_agents.goal import dispatch_sub_goal_as_outcome
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-checking only — avoids closing the bootstrap cycle at module load.
    from ..outcome import OutcomeResult
    from .types import SubGoal

from ..exceptions import CostGuardrailBlocked, GoalCorrupted


def dispatch_sub_goal_as_outcome(
    agent,  # AtomicAgent — for the pre-dispatch cost gate (lazy import only)
    goal_manager,  # GoalManager — provides goal_backend + agent identity
    sub_goal_id: str,
    rubric: "str | Path",
    max_iterations: int = 3,
    extra_context: str | None = None,
    judge_model: str | None = None,
    parent_remaining_headroom_usd: float | None = None,
    actor_model: str | None = None,
) -> "tuple[OutcomeResult, SubGoal]":
    """Dispatch a sub-goal as an outcome, with a fail-closed pre-dispatch cost gate.

    This is the canonical goal-outcome composition entry point (spec/41
    §"Goal-outcome composition"). It performs all transitions via
    apply_transition() for atomic goal.md+JSONL writes under the lock.

    Args:
        agent: AtomicAgent instance. Used for the pre-dispatch cost gate AND
            as the source of log_backend, policy_backend, and profile_backend
            threaded into OutcomeRunner construction so the runner's internal
            AtomicAgent spends in the same backend universe the gate checked
            (backend-universe alignment property — see spec/41 §"Goal-outcome
            composition"). The coordinator reads agent.log_backend,
            agent.policy_backend, and agent.profile_backend directly (no helper).
            MUST be the same agent whose budget and operator backends the
            OutcomeRunner will use — do NOT construct a second AtomicAgent here
            (divergent budget/backend universe). The coordinator accepts it as a
            parameter so construction stays in ONE place (the caller/CLI).
        goal_manager: GoalManager instance (provides goal_backend, agent_name,
            agents_root, and the today date for the completed field).
        sub_goal_id: ID of the sub-goal to dispatch.
        rubric: path to a rubric file OR inline rubric text.
        max_iterations: maximum outcome iterations (default 3).
        extra_context: optional extra context passed to OutcomeRunner.run().
        judge_model: optional judge model override.
        parent_remaining_headroom_usd: when set by a tree-capping caller (e.g. the
            conductor's run-level cost root), this dispatch's effective per-call cap
            is clamped to MIN(own model.md remaining, this headroom) at BOTH the
            pre-dispatch gate AND the OutcomeRunner's per-iteration gates — the
            spec/15 tree-cap clamp. None means "no parent cap" (model.md caps only).
        actor_model: optional per-stage actor model override (#668 — conductor C10).
            When set, passed as actor_model= to OutcomeRunner and from there as
            model_override= to every agent.call() iteration so the stage runs on and
            is billed against the declared model. Policy enforce-mode get_effective_model
            supersedes this per spec/32 ("fleet-config wins"). None = model.md default.

    Returns:
        (OutcomeResult, SubGoal) — same shape as the legacy dispatch_as_outcome
        for backward compatibility with the CLI and existing callers.

    Raises:
        GoalCorrupted: sub-goal is not pending/in_progress, or has an
            unresolved blocked_by dependency.
        CostGuardrailBlocked: pre-dispatch cost gate fired — daily/monthly
            cap hit. The coordinator_dispatch_rejected event was appended to
            goal_history.jsonl before this raise. Sub-goal is NOT marked
            in_progress (no apply_transition was called on the blocked path).
        GoalConcurrentModification: terminal apply_transition found the
            sub-goal was not in_progress (another writer moved the goal during
            the run). Let this propagate — the caller learns the goal moved.
        IOError / OSError: if the pre-dispatch audit append fails (IO error on
            the blocked path), the IO error propagates as a DISTINCT error from
            CostGuardrailBlocked — do NOT dispatch, the audit write failed.

    Note on the cost gate bound:
        _check_cost_guardrails(critical=False) checks model.md caps only.
        Policy-layer caps (_policy_snapshot_this_call) are None outside of
        agent.call() and are NOT enforced at the coordinator level.
        The same bound applies to OutcomeRunner's per-iteration gate.
        When parent_remaining_headroom_usd is set, BOTH gates additionally clamp
        to that headroom (the tree-cap), so a tree-capping caller's ceiling is
        enforced even though the policy layer is not.
    """
    # Lazy import — REQUIRED to avoid closing the goal-package bootstrap cycle.
    # goal/__init__.py uses __getattr__ to defer _goal_impl imports; a module-
    # level import of OutcomeRunner here would close that cycle. Mirror the
    # pattern from _goal_impl.py. (AtomicAgent is the passed-in `agent` param —
    # never imported here.)
    from ..outcome import OutcomeRunner  # noqa: PLC0415

    # ── Step 1: validate sub-goal pre-conditions ──────────────────────────────
    if goal_manager._goal is None:
        goal_manager.load()

    sg = goal_manager._require_sub_goal(sub_goal_id)

    if sg.status not in ("pending", "in_progress"):
        raise GoalCorrupted(
            f"sub_goal '{sub_goal_id}' is '{sg.status}'; "
            f"dispatch_sub_goal_as_outcome only accepts pending or in_progress sub-goals"
        )

    if sg.blocked_by:
        blocker = goal_manager.find_sub_goal(sg.blocked_by)
        if blocker is None:
            raise GoalCorrupted(
                f"sub_goal '{sub_goal_id}' blocked_by '{sg.blocked_by}' which does not exist; "
                f"goal graph is inconsistent — operator must repair goal.md"
            )
        if blocker.status != "complete":
            raise GoalCorrupted(
                f"sub_goal '{sub_goal_id}' has unresolved blocked_by dependency "
                f"'{sg.blocked_by}' (status: {blocker.status}); "
                f"resolve the blocker before dispatching as outcome"
            )

    # ── Step 2: pre-dispatch fail-closed cost gate ────────────────────────────
    #
    # CRITICAL CORRECTNESS SURFACE (CLAUDE.md Principle #4 — cost is first-class):
    # The #425 cost gate FAILED OPEN three ways:
    #   (1) CostCheckResult was tuple-unpacked — it is a DATACLASS (allow: bool,
    #       reason: str). Tuple-unpacking raises TypeError, which was caught and
    #       swallowed into allowed=True.
    #   (2) AtomicAgent was constructed positionally — a TypeError from keyword-
    #       only args was swallowed into allowed=True.
    #   (3) A broad except around _check_cost_guardrails converted check failure
    #       into allowed=True (fail-OPEN gate).
    #
    # ALL THREE are blocked here:
    #   - result.allow / result.reason are read as dataclass attributes (NOT
    #     tuple-unpacked, NOT tested with `if result:` — dataclass is always truthy)
    #   - agent is passed IN (not constructed here) — no constructor TypeError
    #   - ZERO try/except on the gate path — construction AND check failures
    #     propagate (fail-closed means both raise, never swallow)
    #
    # Call _check_cost_guardrails(critical=False) — never critical=True (that is
    # a bypass by design, CLAUDE.md Principle #4: "Don't make critical=True the
    # default anywhere").
    #
    # Cost gate bound: checks model.md caps only. Policy-layer caps
    # (_policy_snapshot_this_call) are None outside agent.call() and are NOT
    # enforced here — the same bound that applies to OutcomeRunner's per-iteration
    # gate. Mock _check_cost_guardrails directly in tests (do NOT reconstruct
    # the policy stack — false assurance).
    #
    # NO TRY/EXCEPT anywhere below this comment until after the gate is resolved.
    # parent_remaining_headroom_usd threads a tree-cap (e.g. the conductor's
    # run-level run_remaining) into the gate so the effective cap is clamped to
    # MIN(model.md remaining, parent headroom) — spec/15 / Principle #4.
    result_check = agent._check_cost_guardrails(
        critical=False, parent_remaining_headroom_usd=parent_remaining_headroom_usd
    )

    if not result_check.allow:
        # BLOCKED PATH (CLAUDE.md Principle #4 + spec/41 MUST 6 audit-ordering):
        # APPEND coordinator_dispatch_rejected event FIRST, THEN raise.
        # If the append itself fails (IOError), the IO error propagates DIRECTLY —
        # do NOT dispatch, do NOT treat the IO error as CostGuardrailBlocked.
        # Two distinct error types: IO error = audit write failed; CostGuardrailBlocked
        # = cost gate fired. The coordinator never dispatches after a failed audit write.
        #
        # After this path, the sub-goal is still 'pending' (no apply_transition called).
        # The dashboard collision is avoided by NOT using 'blocked' in the event name
        # (goals.py:310 matches 'blocked' substring; 'coordinator_dispatch_rejected'
        # does not match that pattern).
        _rejected_event = {
            "ts": datetime.now().astimezone().isoformat(),
            "event": "coordinator_dispatch_rejected",
            "sub_goal_id": sub_goal_id,
            "reason": result_check.reason,
            # Include degraded flag so the audit trail distinguishes a real
            # cap hit from a data-quality blind spot (spec/09 cost-read posture).
            "cost_data_degraded": result_check.cost_data_degraded,
        }
        # #668 P2 — mirror the success-path (sub_goal_outcome_dispatched) audit:
        # record the DECLARED per-stage dial on the cost-rejected terminal event too,
        # so the goal ledger viewed in isolation keeps the dial context for a
        # "why was this pricier-model stage refused" query (Principle #5 — audit
        # symmetry across both terminal dispositions). 'declared, not effective'.
        if actor_model is not None:
            _rejected_event["actor_model"] = actor_model
        goal_manager.goal_backend.append_history_event(
            goal_manager.agent_name,
            _rejected_event,
        )
        raise CostGuardrailBlocked(result_check.reason)

    # ── Step 3: apply_transition(pending→in_progress) [lock taken+released] ──
    # The lock is held ONLY inside apply_transition() (NEVER across run()).
    # Holding the lock across a multi-minute run would block the agent and all
    # readers (CLAUDE.md Principle #4; spec/41 lock-discipline-toctou ruling).
    #
    # Skip the pre-transition if the sub-goal is already in_progress (idempotent).
    # Use "sub_goal_outcome_started" as the event name (NOT "sub_goal_outcome_dispatched"
    # — the terminal transition owns that name; emitting it twice would break
    # test_dispatch_as_outcome_writes_history_entry which asserts len(dispatched)==1).
    if sg.status == "pending":
        updated_pre = goal_manager.goal_backend.apply_transition(
            agent_id=goal_manager.agent_name,
            sub_goal_id=sub_goal_id,
            to_status="in_progress",
            fields={},
            history_prose=f"sub_goal `{sub_goal_id}` → in_progress (outcome dispatch)",
            history_event={
                "ts": datetime.now().astimezone().isoformat(),
                "event": "sub_goal_outcome_started",
                "sub_goal_id": sub_goal_id,
            },
            when=goal_manager.today,  # prose date from injectable clock (#483 PR1)
        )
        # Sync the in-memory goal on GoalManager so callers inspecting
        # goal_manager._goal.sub_goals during the run see the in_progress status.
        # apply_transition reloads fresh from disk and returns the updated Goal.
        goal_manager._goal = updated_pre

    # ── Step 4: build description + run OutcomeRunner [NO lock held] ─────────
    # The goal lock is released (apply_transition returned). Run can be minutes.
    # OutcomeRunner.run() constructs its OWN AtomicAgent internally with
    # trigger='outcome'. The coordinator threads agent.log_backend,
    # agent.policy_backend, and agent.profile_backend into OutcomeRunner so
    # the runner's internal AtomicAgent is constructed against the SAME backend
    # instances the gate agent carries (backend-universe alignment — see spec/41
    # §"Goal-outcome composition"). NB they align at different boundaries: only
    # log_backend is load-bearing for the gate (the cost gate reads spend from
    # self.log_backend); policy_backend aligns the runner's IN-call() caps, not
    # the gate (policy caps are None outside agent.call(), so neither pre-dispatch
    # gate consults them); profile_backend is run-side identity/config, not cost.
    # CAVEAT: caps are NOT yet fully aligned. The effective cap is composed via
    # MIN with the mandate (inside _check_cost_guardrails's MIN composition +
    # MandateCheck), and mandate_backend is intentionally NOT threaded here (#496
    # scoped the set to log/policy/profile). A custom mandate_backend pinned on
    # the gate agent would tighten the gate's cap but NOT the runner's — a
    # mandate-derived cap can still diverge on the custom-backend path. Tracked
    # in #503. Other OutcomeRunner backends (persona, corpus,
    # mcp_server_registry, mandate, tool_registry) are also NOT threaded here —
    # they default to filesystem resolution. outcome_backend is also NOT
    # threaded — the runner owns its outcome write-path topology.
    # OutcomeRunner also has its own per-iteration cost gate (which fires on
    # block and sets result.status='interrupted' — it does NOT raise). The
    # coordinator's pre-dispatch gate is ADDITIONAL and is the only one that
    # raises before paying the run + construction overhead (Principle #4
    # "refuse before paying overhead").

    # Reload the sub-goal from the updated in-memory state for description building.
    # goal_manager._goal was loaded at the start; sg is a reference to the in-memory
    # object. The apply_transition above updated the on-disk state; the in-memory
    # GoalManager object's _goal is now stale. For description building only, we
    # re-read sg from the in-memory goal (the label/body/acceptance_criteria fields
    # didn't change — only status changed).
    sg_for_desc = goal_manager._require_sub_goal(sub_goal_id)
    description = goal_manager._build_outcome_description_from_sub_goal(sg_for_desc)

    runner = OutcomeRunner(
        agents_root=goal_manager.agents_root,
        agent_name=goal_manager.agent_name,
        judge_model=judge_model,
        actor_model=actor_model,
        # keyword args, not positional — mirrors the #425 fix discipline.
        # Threading the gate agent's RESOLVED policy_backend flips the runner's
        # internal-agent _policy_backend_was_explicit True, which SKIPS its
        # cascade re-resolution. That is safe ONLY because the runner is built
        # with the same agent_name + agents_root as the gate agent, so both
        # resolve the SAME cascade — the gate agent already cascade-resolved its
        # policy_backend. If a future change ever lets the runner's name/agents_root
        # diverge from the gate agent's, that equivalence breaks (see #503).
        log_backend=agent.log_backend,
        policy_backend=agent.policy_backend,
        profile_backend=agent.profile_backend,
        # Tree-cap: the runner's per-iteration cost gate clamps to MIN(own
        # remaining, this headroom − the stage's spend so far). Without threading
        # this, a single stage's OutcomeRunner would gate on model.md caps ONLY and
        # could overshoot the caller's run-level ceiling within one stage; the
        # runner decrements this snapshot by accumulated spend so the run cap binds
        # at each iteration boundary (Principle #4 tree-cap).
        parent_remaining_headroom_usd=parent_remaining_headroom_usd,
    )
    outcome_result = runner.run(
        description=description,
        rubric=rubric,
        max_iterations=max_iterations,
        extra_context=extra_context,
    )

    # ── Step 5: terminal apply_transition [lock taken+released] ──────────────
    # Map terminal state to sub-goal status and build the fields + history_prose.
    # The TERMINAL apply_transition carries history_event={"event":
    # "sub_goal_outcome_dispatched", ...} — ONE atomic write, provenance atomic
    # with the status change (spec/41 MUST 6 + C4 ruling).
    #
    # expected_from_status='in_progress' (A5 CAS, spec/41 MUST 10): UNDER THE LOCK,
    # apply_transition checks that the sub-goal is still in_progress before writing.
    # If another writer moved it (e.g. a duplicate dispatch), GoalConcurrentModification
    # is raised — let it propagate (caller learns the goal moved, no stale write).
    #
    # For the 'interrupted' case, to_status='in_progress' is a no-op status write
    # (goal stays in_progress) but the JSONL audit event still lands atomically.
    # The CAS (expected_from_status='in_progress') passes because the goal IS
    # still in_progress (set by Step 3). The redundant status write is intentional:
    # C4 ruling says "apply_transition for all terminal states".
    #
    # blocked_by=None in fields on max_iterations_reached/failed: matches the
    # legacy dispatch_as_outcome behavior (explicit clear of any prior blocked_by
    # reference so the terminal 'blocked' status is not semantically ambiguous).

    applied_status: str
    terminal_fields: dict
    history_prose: str

    if outcome_result.status == "satisfied":
        applied_status = "complete"
        terminal_fields = {"completed": goal_manager.today.isoformat()}
        history_prose = (
            f"sub_goal `{sub_goal_id}` → complete "
            f"(outcome {outcome_result.run_id} satisfied)"
        )
    elif outcome_result.status == "max_iterations_reached":
        applied_status = "blocked"
        terminal_fields = {"blocked_by": None}
        history_prose = (
            f"sub_goal `{sub_goal_id}` → blocked "
            f"(max_iterations_reached on outcome {outcome_result.run_id})"
        )
    elif outcome_result.status == "failed":
        applied_status = "blocked"
        terminal_fields = {"blocked_by": None}
        explanation_short = (outcome_result.explanation or "")[:200]
        history_prose = (
            f"sub_goal `{sub_goal_id}` → blocked (outcome failed — {explanation_short})"
        )
    else:
        # interrupted — leave in_progress; caller decides whether to retry
        applied_status = "in_progress"
        terminal_fields = {}
        history_prose = (
            f"sub_goal `{sub_goal_id}` stays in_progress "
            f"(outcome {outcome_result.run_id} interrupted)"
        )

    _terminal_event: dict = {
        "ts": datetime.now().astimezone().isoformat(),
        "event": "sub_goal_outcome_dispatched",
        "sub_goal_id": sub_goal_id,
        "outcome_run_id": outcome_result.run_id,
        "terminal_state": outcome_result.status,
        "applied_status": applied_status,
        "iterations": len(outcome_result.iterations),
        "total_cost_usd": outcome_result.total_cost_usd,
    }
    if actor_model is not None:
        # #668: record the DECLARED actor-model dial requested for this dispatch in
        # the goal-ledger audit trail. The EFFECTIVE model that actually ran may
        # differ under Policy enforce-mode (fleet-config supersedes the dial) OR the
        # agent's own cost-cap fallback (a model.md-cap fallback supersedes the dial);
        # the authoritative billed model lives in the agent's own RunRecord (linked via
        # outcome_run_id), not this goal-ledger event. Goal history is append-only
        # JSONL with permissive schema — no migration needed. Gated on non-None so
        # the common (no override) case stays sparse.
        _terminal_event["actor_model"] = actor_model
    updated_goal = goal_manager.goal_backend.apply_transition(
        agent_id=goal_manager.agent_name,
        sub_goal_id=sub_goal_id,
        to_status=applied_status,
        fields=terminal_fields,
        history_prose=history_prose,
        history_event=_terminal_event,
        expected_from_status="in_progress",
        when=goal_manager.today,  # prose date from injectable clock (#483 PR1)
    )

    # Extract the updated sub-goal from the returned Goal object for the caller.
    updated_sg = next(
        (s for s in updated_goal.sub_goals if s.id == sub_goal_id),
        None,
    )
    if updated_sg is None:
        # Defensive: sub_goal_id vanished from goal after transition (should never
        # happen — apply_transition would have raised AtomicAgentsError if not found).
        raise GoalCorrupted(
            f"sub_goal '{sub_goal_id}' not found after terminal apply_transition"
        )

    return outcome_result, updated_sg

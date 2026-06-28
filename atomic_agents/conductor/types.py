"""Conductor types — data model for playbook manifests and run state (spec/50).

All types are frozen dataclasses (value objects). The conductor holds no
authoritative state; every durable fact lives in Goal / Outcome / Idempotency.
ConductorState is a fresh projection from the ledger each time run() is called.

PR2 (#581) scope: gate stages (is_gate=True) SUSPEND the run — run() transitions
the gate sub-goal to 'awaiting_decision' and returns ConductorState(status=
'awaiting_decision', pending_decision=GateDecision(...)). resume() injects the
typed disposition to continue/skip/halt. (PR1 parsed gate stages but halted
immediately with gate_not_implemented_pr2; that halt path is removed in PR2.)
PR2 (#581): Gate suspension + resume. GateDecision is the one genuinely new
artifact (spec/50 §"The one genuinely new artifact"). ConductorState.status
gains 'awaiting_decision'. resume() is the public entry point for answering
a gate; GateDecision carries the decision_id, prompt, options, context_ref,
and disposition (typed: 'continue'/'skip'/'halt' — NOT magic-word-sniffed).
PR3 (#582): Concurrency + conflict serialization. StageSpec gains
conflict_keys (a tuple of key strings a gate stage holds while suspended).
ConductorState.status gains 'deferred' (a second run is blocked behind an active
gate that holds a conflicting key and must wait for that gate to be answered).
The status value is 'deferred' NOT 'queued' to avoid colliding with the
QueueBackend's own queue-dir vocabulary (deferred-run-status-value ruling).
queued_behind_decision_id records which gate decision is blocking the deferred
run; queued_behind_conductor_run_id records which conductor run holds it (the
blocking gate lives in a DIFFERENT conductor run).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# ──────────────────────────────────────────────────────────────────
# Playbook manifest types


@dataclass(frozen=True)
class StageSpec:
    """One stage in a playbook.

    Fields:
        stage_id: stable identifier for this stage (REQUIRED, validated at
            parse time — loader rejects missing or duplicate stage_ids). Used
            as the per-stage idempotency key suffix:
            ``conductor:<conductor_run_id>:<stage_id>``.
        label: human-readable name for the stage.
        prompt: the inline prompt / description of what the stage produces.
            When ``prompt_ref`` is set, this field holds the resolved text of
            the referenced file. When only ``prompt`` is set, it is used inline.
        prompt_ref: optional path (relative to the playbook directory) to a
            markdown file containing a longer prompt. The loader resolves this
            and populates ``prompt`` with its content.
        rubric: optional inline rubric for the outcome judge. When absent, the
            prompt text is used as a self-rubric (judge evaluates "is the output
            faithful to the prompt?").
        rubric_ref: optional path to a rubric file (relative to playbook dir).
            Resolved by the loader into ``rubric``.
        model: optional per-stage model dial. PARSED but NOT YET APPLIED —
            the stage always runs on the agent's configured model.md model. The
            actor-model override is not yet plumbed through the goal-outcome
            coordinator / OutcomeRunner (only judge_model is). A non-None value is
            stored and a warning is emitted on each run()/resume() process so the
            no-op is never silent; actor-model wiring is tracked in #668.
        is_gate: True when this stage requires a human decision before the run
            may continue. PR1 halts on gate stages (PR2 #581 implements resume).
    """

    stage_id: str
    label: str
    prompt: str
    prompt_ref: str | None = None
    rubric: str | None = None
    rubric_ref: str | None = None
    model: str | None = None
    is_gate: bool = False
    # PR2 (#581): gate-stage-markdown-schema ruling. Optional declared choices for a
    # gate question. Empty tuple = free-text (the human may answer anything). Non-empty
    # = the playbook author offers these specific choices (displayed by the CLI).
    # SHAPE-validated at parse time for ANY stage that supplies a truthy `options`
    # key (must be a list of non-empty strings — a malformed value is rejected even
    # on a non-gate stage); the validated value is RETAINED only for is_gate=True
    # stages and silently discarded (left as the empty tuple) for non-gate stages.
    # Tuple because StageSpec is frozen=True; list would fail the frozen constraint.
    options: tuple[str, ...] = field(default_factory=tuple)
    # PR3 (#582): optional conflict serialization keys. When a gate stage has
    # non-empty conflict_keys, a second run() call that would also gate on ANY
    # overlapping key is queued behind this gate rather than running concurrently.
    # Empty tuple for all non-gate stages (validated at parse time: rejected for
    # non-gate stages). The tuple is copied onto the gate sub-goal as
    # SubGoal.held_conflict_keys at suspension time so a conflict scan reads it
    # with one load_goal() per goal (O(n_goals) loads, no per-goal JSONL parse).
    conflict_keys: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PlaybookManifest:
    """Parsed and validated PLAYBOOK.md manifest.

    Fields:
        name: playbook identifier from frontmatter.
        description: third-person description of what the playbook does.
        when_to_use: optional extended triggering guidance.
        run_cap_usd: run-level cost ceiling (required in the YAML block).
            Each stage's cost gate clamps to MIN(model.md remaining,
            run_cap_usd - cumulative_spend - stage spend so far) — the run-level
            remaining is threaded into the stage dispatch as
            parent_remaining_headroom_usd (the tree-cap) and decremented per
            iteration inside OutcomeRunner, so the run cap binds at each iteration
            boundary and within-stage overshoot is bounded by one iteration's spend
            (not zero — same granularity as the delegate tree-cap). cumulative_spend
            is re-summed from the durable ledger on every stage (counting every
            dispatch attempt, complete or not). There is no separate per-stage cap
            field on StageSpec; the stage's own bound is its model.md cap.
        stages: ordered list of stage specs.
        playbook_dir: absolute path to the playbook's directory.
        playbook_md_path: absolute path to the PLAYBOOK.md file.
    """

    name: str
    description: str
    when_to_use: str | None
    run_cap_usd: float
    stages: list[StageSpec]
    playbook_dir: Path
    playbook_md_path: Path


# ──────────────────────────────────────────────────────────────────
# Gate-decision record (PR2 — the one genuinely new artifact, spec/50 §63)


@dataclass(frozen=True)
class GateDecision:
    """The durable record of a pending-or-answered human gate decision.

    This is the ONE genuinely new artifact the conductor introduces (spec/50
    §"The one genuinely new artifact"). Everything else is reuse of shipped
    primitives (Goal/Outcome/Idempotency/Queue). A GateDecision records:

    - What is the question (prompt, options)?
    - What is the context the human should review (context_ref)?
    - What resources does the suspended run hold (held_conflict_keys)?
    - Has the human answered yet, and if so: what, by whom, when, why (disposition)?

    The resume cursor is the gate sub-goal's STATUS (apply_transition path), NOT
    this event — this is pure append-only audit. Two distinct JSONL event types
    carry the lifecycle: 'conductor_gate_pending' (on suspension) and
    'conductor_gate_answered' (on resume), linked by decision_id.

    Fields:
        decision_id: stable id for this gate within the run (16-char UUID4 hex
            slice with a 'gate-' prefix). Stored on the sub-goal as gate_decision_id for
            atomic CAS verification in resume() (c5-stale-duplicate-rejection).
        stage_id: the stage this gate guards.
        prompt: the question text (from stage.prompt).
        options: offered choices from the PLAYBOOK.md stage schema (empty list
            when the gate allows free-text; non-empty for structured choices).
        context_ref: opaque reference to the prior-stage result the human should
            review. Derived at suspension time from completed_stage_ids (the last
            completed stage's outcome_run_id), NOT operator-authored in markdown.
            Falls back to the goal-history path when no prior stage completed.
        held_conflict_keys: resources this suspended run holds (PR3 #582).
            PR2: RECORDED in the conductor_gate_pending event but NOT acted on
            for release. Conflict release (queued waiters) is PR3 (#582);
            held_conflict_keys drives that release but the release logic is not
            yet wired.
        disposition: the typed gate ruling — 'continue' (run continues to next
            stage), 'skip' (stage is skipped, sub-goal → 'skipped'), or 'halt'
            (run halts). DISTINCT typed field — NOT magic-word-sniffed from the
            free-text answer string (gate-answer-semantics ruling). None while the
            gate is pending (disposition is None encodes "pending"; the durable
            machine state is the gate sub-goal's STATUS in the goal ledger, not a
            field on this record — D2). NOTE: 'skip' and 'continue' are runtime-
            identical in PR2 (both proceed to the next stage); they differ only in
            the gate's recorded audit status ('skipped' vs 'complete'). Stage-level
            skip-the-guarded-work is deferred (tracked in #671) (H3).
        answer: the human's free-text answer. Recorded in conductor_gate_answered
            for audit. NOT threaded into later stage prompts (D3=A, tracked in #672).
        answered_by: principal.identifier of the human who answered (the stable
            identity string, e.g. 'local' for LOCAL_PRINCIPAL). None until answered.
        answered_at: ISO-8601 timestamp when the gate was answered. None until answered.
        rationale: the human's stated reason for the ruling. None until answered.
    """

    decision_id: str
    stage_id: str
    prompt: str
    options: list[str] = field(default_factory=list)
    context_ref: str = ""
    held_conflict_keys: list[str] = field(default_factory=list)
    # Answered fields (all None while pending — disposition is None encodes
    # "pending"; there is no `status` field on GateDecision, D2).
    disposition: Literal["continue", "skip", "halt"] | None = None
    answer: str | None = None
    answered_by: str | None = None
    answered_at: str | None = None
    rationale: str | None = None


# ──────────────────────────────────────────────────────────────────
# Conductor run state (ledger projection — NOT authoritative)


@dataclass(frozen=True)
class ConductorState:
    """Read-only projection of conductor run state from the goal ledger.

    This is a FRESH projection returned by run() each call. It is NOT a live
    handle or a cached state object. Every field is derived from durable
    primitives (Goal / Outcome / Idempotency) at run() time. No mutable state
    is held between run() calls (C1: the conductor holds no authoritative state).

    Fields:
        conductor_run_id: stable identifier for this conductor run. Persisted
            in the goal_created JSONL event via the dynamic ``conductor_run_id``
            attribute on the Goal object. On resume, pass this value back to
            run() to resume from the durable ledger.
        playbook_name: the playbook's ``name`` field (from frontmatter).
        subject: the work subject passed to run() (e.g. "feature #1234").
        status: 'complete' (all stages done), 'halted' (stopped early — see
            halt_reason), 'awaiting_decision' (suspended on a gate — see
            pending_decision), or 'deferred' (blocked behind another run's active
            conflict-key gate — see queued_behind_decision_id /
            queued_behind_conductor_run_id). PR2 (#581) adds 'awaiting_decision';
            PR3 (#582) adds 'deferred'. There is no 'running' value because
            ConductorState is a fresh return-only projection, never a live mid-run
            handle (C1).
        halt_reason: present when status='halted'; describes why the run stopped.
            Examples: 'run_cap_exhausted', 'cost_gate_halted', 'cost_data_degraded',
            'stage_max_iterations_reached', 'stage_abandoned', 'dispatch_error',
            'gate_halt_disposition' (resume answered a gate with disposition='halt').
            PR1's 'gate_not_implemented_pr2' is removed in PR2 (replaced by the
            real gate-suspension path). None when status='awaiting_decision'.
        pending_decision: the GateDecision the run is suspended on. Non-None iff
            status=='awaiting_decision' (__post_init__ invariant). Callers must
            pass pending_decision.decision_id to conductor.resume() to answer.
        stages_total: total number of stages in the playbook.
        stages_complete: number of stages whose sub-goal is status='complete'
            or 'skipped' (PR2 — skipped stages are terminal-done).
        cumulative_spend_usd: sum of every stage dispatch attempt that reached a
            terminal coordinator transition (complete or not — re-summed from the
            durable ledger, not carried in process memory; fail-closed accounting
            per spec/50 C6). See ``cost_data_degraded`` for the read-trust marker.
        run_cap_usd: the run-level cost ceiling. PINNED at run creation: the value
            is recorded in the conductor_run_started ledger event and read back
            from there on every run()/resume, so an operator who edits run_cap_usd
            in PLAYBOOK.md between a crash and a resume does NOT change the cap for
            the resumed run (spec/50 §Cost/§Throughline — "resumes under the same
            run-level ceiling"). A fresh run pins playbook.run_cap_usd; an existing
            run reuses its run-start value.
        cost_data_degraded: True when the cumulative-spend read could not be fully
            trusted (whole-file read error, or one or more unparseable JSONL
            lines), so cumulative_spend_usd may be under-counted. Mirrors the
            dashboard #498 posture — surfaced, never silently presented as
            authoritative. The in-loop run-cap gate already fails closed (halts)
            on a degraded read before admitting more spend; this flag is the
            display-time marker on the returned projection.
        completed_stage_ids: list of stage_ids that have reached a terminal-done
            state ('complete' or 'skipped').
    """

    conductor_run_id: str
    playbook_name: str
    subject: str
    status: Literal["complete", "halted", "awaiting_decision", "deferred"]
    halt_reason: str | None
    stages_total: int
    stages_complete: int
    cumulative_spend_usd: float
    run_cap_usd: float
    completed_stage_ids: list[str] = field(default_factory=list)
    cost_data_degraded: bool = False
    # PR2 (#581): gate suspension. Non-None iff status=='awaiting_decision'.
    # The __post_init__ invariant enforces this relationship.
    pending_decision: GateDecision | None = None
    # PR3 (#582): conflict-queue parking. Both fields are non-None iff
    # status=='deferred' (the __post_init__ invariant enforces this relationship).
    # Records which gate decision (queued_behind_decision_id) on which conductor
    # run (queued_behind_conductor_run_id) is blocking this run; caller should poll
    # run() again after the blocking gate is answered (self-release model: the
    # deferred run re-checks the gate status on its next run() invocation — no
    # push-release).
    queued_behind_decision_id: str | None = None
    queued_behind_conductor_run_id: str | None = None

    def __post_init__(self) -> None:
        """Enforce the pending_decision / deferred-run invariants."""
        if self.status == "awaiting_decision" and self.pending_decision is None:
            raise ValueError(
                "ConductorState with status='awaiting_decision' MUST have "
                "pending_decision set (invariant violated: pending_decision is None)"
            )
        if self.status != "awaiting_decision" and self.pending_decision is not None:
            raise ValueError(
                f"ConductorState with status={self.status!r} MUST NOT have "
                "pending_decision set (invariant violated: pending_decision is non-None "
                f"for a non-suspended run; pending_decision.decision_id="
                f"{self.pending_decision.decision_id!r})"
            )
        # Both deferred-tracking fields are bound to status=='deferred': both
        # non-None iff deferred, both None otherwise.
        if self.status == "deferred" and (
            self.queued_behind_decision_id is None
            or self.queued_behind_conductor_run_id is None
        ):
            raise ValueError(
                "ConductorState with status='deferred' MUST have BOTH "
                "queued_behind_decision_id AND queued_behind_conductor_run_id set "
                f"(invariant violated: queued_behind_decision_id="
                f"{self.queued_behind_decision_id!r}, "
                f"queued_behind_conductor_run_id="
                f"{self.queued_behind_conductor_run_id!r})"
            )
        if self.status != "deferred" and (
            self.queued_behind_decision_id is not None
            or self.queued_behind_conductor_run_id is not None
        ):
            raise ValueError(
                f"ConductorState with status={self.status!r} MUST NOT have "
                "queued_behind_decision_id or queued_behind_conductor_run_id set "
                f"(invariant violated for a non-deferred run; "
                f"queued_behind_decision_id={self.queued_behind_decision_id!r}, "
                f"queued_behind_conductor_run_id="
                f"{self.queued_behind_conductor_run_id!r})"
            )

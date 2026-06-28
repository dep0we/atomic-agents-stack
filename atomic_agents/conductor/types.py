"""Conductor types — data model for playbook manifests and run state (spec/50).

All types are frozen dataclasses (value objects). The conductor holds no
authoritative state; every durable fact lives in Goal / Outcome / Idempotency.
ConductorState is a fresh projection from the ledger each time run() is called.

PR1 scope: automated stages only (is_gate=False). Gate stages are parsed
(the schema accepts them) but cause run() to halt immediately with
status='halted' and halt_reason='gate_not_implemented_pr2'.
PR2 (#581) will add await_decision / resume() and the awaiting_decision status.
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
        model: optional per-stage model dial. PARSED but NOT APPLIED in PR1 —
            the stage always runs on the agent's configured model.md model. The
            actor-model override is not yet plumbed through the goal-outcome
            coordinator / OutcomeRunner (only judge_model is). A non-None value is
            stored and a one-time warning is emitted at run() so the no-op is never
            silent; wiring is deferred (tracked: per-stage actor-model override).
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
        status: 'complete' (all stages done) or 'halted' (stopped early — see
            halt_reason). run() only ever returns one of these two terminal
            projections; there is no 'running' value because ConductorState is a
            fresh return-only projection, never a live mid-run handle (C1). A
            streaming/live-handle API is a future addition, not a PR1 state.
        halt_reason: present when status='halted'; describes why the run stopped.
            Examples: 'run_cap_exhausted', 'cost_gate_halted', 'cost_data_degraded',
            'stage_max_iterations_reached', 'stage_abandoned', 'dispatch_error',
            'gate_not_implemented_pr2'.
        stages_total: total number of stages in the playbook.
        stages_complete: number of stages whose sub-goal is status='complete'.
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
        completed_stage_ids: list of stage_ids that have completed successfully.
    """

    conductor_run_id: str
    playbook_name: str
    subject: str
    status: Literal["complete", "halted"]
    halt_reason: str | None
    stages_total: int
    stages_complete: int
    cumulative_spend_usd: float
    run_cap_usd: float
    completed_stage_ids: list[str] = field(default_factory=list)
    cost_data_degraded: bool = False

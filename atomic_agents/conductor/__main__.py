"""Minimal conductor CLI — python -m atomic_agents.conductor (spec/50 PR1+PR2).

Usage:
    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root>
    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root> \\
        [--resume <conductor_run_id>]
    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root> \\
        [--max-stage-iterations N]

    python -m atomic_agents.conductor resume <agent_root> <conductor_run_id> \\
        --decision-id ID --answer TEXT --rationale TEXT --disposition {continue,skip,halt}

Arguments (run):
    playbook_name    Name of the playbook (must match the 'name:' field in
                     PLAYBOOK.md, searched under <agent_root>/skills/).
    subject          Work subject string (e.g. "feature #1234", "Q3 report").
    agent_root       Absolute or relative path to the agent's root directory.
                     Must contain model.md with cost guardrails.

Options (run):
    --resume ID      Resume an existing run by conductor_run_id.
    --max-stage-iterations N
                     Max LLM iterations per stage (default: 3).
    --judge-model M  LLM model to use for the outcome judge.

Arguments (resume):
    agent_root       Path to the agent root directory (same as 'run').
    conductor_run_id The conductor_run_id from the suspended run's output.

Options (resume):
    --decision-id ID (required) The decision_id from the pending GateDecision.
    --answer TEXT    (required) Free-text answer to the gate question.
    --rationale TEXT (required) Your stated reason for the ruling.
    --disposition    {continue,skip,halt} (required) The typed gate ruling.
    --playbook-name NAME (optional) Playbook to resume; auto-detected from the
                     ledger (conductor_run_started event) when omitted.
    --subject TEXT   (optional) Work subject; auto-detected from the goal intent
                     when omitted.

Exits:
    0 — run completed (all stages satisfied), OR deferred behind a conflicting
        run's gate decision (a benign, self-healing state: it auto-releases and
        continues on its next scheduled trigger, so no operator action is needed).
        For the deferred case, exit 0 means THIS TICK is fine and needs no operator
        action — it does NOT mean the run is complete. The run self-releases and
        continues on its NEXT scheduled trigger, so a driver MUST keep re-triggering
        it (do not treat this 0 as "done; stop re-triggering"). Only a 'complete'
        status means the run is finished.
    1 — run halted or suspended awaiting a human gate decision (gate, cap
        exhausted, or stage failure) — needs attention; will not progress alone
    2 — usage / configuration error

PR3 (#582) added concurrency and conflict serialization: a run that needs a
conflict key held by another run's suspended gate returns status='deferred'
and self-releases on its next trigger once that gate is answered.
PR4 (#583) will add: launder-guard and doctor check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m atomic_agents.conductor",
        description="Run a PLAYBOOK.md conductor run against an agent (spec/50 PR1+PR2).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Execute or resume a conductor run.")
    run_p.add_argument("playbook_name", help="Playbook 'name:' field in PLAYBOOK.md.")
    run_p.add_argument(
        "subject",
        help="Work subject (e.g. 'feature #1234', 'Q3 report').",
    )
    run_p.add_argument(
        "agent_root",
        help="Path to the agent root directory (must contain model.md).",
    )
    run_p.add_argument(
        "--resume",
        metavar="CONDUCTOR_RUN_ID",
        default=None,
        help="Resume an existing run by conductor_run_id.",
    )
    run_p.add_argument(
        "--max-stage-iterations",
        type=int,
        default=3,
        metavar="N",
        help="Max LLM iterations per stage (default: 3).",
    )
    run_p.add_argument(
        "--judge-model",
        default=None,
        metavar="MODEL",
        help="Model to use for the outcome judge (default: agent's configured model).",
    )

    # PR2 (#581): resume subcommand — non-interactive gate answer (cli-resume-subcommand
    # ruling). Uses positional args for agent_root/conductor_run_id and flags for the
    # decision. No inline stdin prompt (defeats C8 async answering).
    resume_p = sub.add_parser(
        "resume",
        help=(
            "Answer a suspended gate and continue the run (PR2 #581 — non-interactive)."
        ),
    )
    resume_p.add_argument(
        "agent_root",
        help="Path to the agent root directory (same as 'run').",
    )
    resume_p.add_argument(
        "conductor_run_id",
        help="The conductor_run_id from the suspended run's output.",
    )
    resume_p.add_argument(
        "--decision-id",
        required=True,
        metavar="ID",
        help="The decision_id from the pending GateDecision.",
    )
    resume_p.add_argument(
        "--answer",
        required=True,
        metavar="TEXT",
        help="Free-text answer to the gate question (recorded in audit).",
    )
    resume_p.add_argument(
        "--rationale",
        required=True,
        metavar="TEXT",
        help="Your stated reason for the ruling (required for audit trail).",
    )
    resume_p.add_argument(
        "--disposition",
        required=True,
        choices=["continue", "skip", "halt"],
        help=(
            "The typed gate ruling: 'continue' (proceed to next stage), "
            "'skip' (mark gate skipped and proceed), or 'halt' (stop the run)."
        ),
    )
    resume_p.add_argument(
        "--playbook-name",
        default=None,
        metavar="NAME",
        help=(
            "Playbook 'name:' field (optional — auto-detected from ledger when omitted)."
        ),
    )
    resume_p.add_argument(
        "--subject",
        default=None,
        metavar="TEXT",
        help="Work subject (optional — auto-detected from ledger when omitted).",
    )
    # H4 — resume must forward the same run-tuning knobs as `run` (otherwise a
    # resumed run silently reverts to defaults for the stages it continues into).
    resume_p.add_argument(
        "--max-stage-iterations",
        type=int,
        default=3,
        metavar="N",
        help="Max LLM iterations per stage for the continued run (default: 3).",
    )
    resume_p.add_argument(
        "--judge-model",
        default=None,
        metavar="MODEL",
        help="Model for the outcome judge of the continued run (default: agent's).",
    )
    # H4 — attribute the gate ruling. The local CLI operator is trusted by
    # construction (same basis as LOCAL_PRINCIPAL), so a supplied identity is
    # recorded as a VERIFIED local principal; omitted → 'local' (LOCAL_PRINCIPAL).
    resume_p.add_argument(
        "--answered-by",
        default=None,
        metavar="IDENTITY",
        help=(
            "Stable identity to record as the gate-ruling author (default: 'local'). "
            "Recorded verified — the local CLI operator is trusted by construction."
        ),
    )
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute or resume a conductor run. Returns exit code."""
    from atomic_agents import AtomicAgent  # noqa: PLC0415
    from atomic_agents.conductor import discover_playbooks, run  # noqa: PLC0415
    from atomic_agents.exceptions import (  # noqa: PLC0415
        ConductorConflictScanError,
        LockBusy,
    )

    agent_root = Path(args.agent_root).resolve()
    if not agent_root.is_dir():
        print(f"error: agent_root not found: {agent_root}", file=sys.stderr)
        return 2

    model_md = agent_root / "model.md"
    if not model_md.is_file():
        print(
            f"error: {agent_root} does not look like an agent root (no model.md). "
            "The conductor requires an agent with cost guardrails configured in model.md.",
            file=sys.stderr,
        )
        return 2

    # Discover playbooks under <agent_root>/skills/
    playbooks = discover_playbooks(agent_root)
    playbook = next(
        (p for p in playbooks if p.name == args.playbook_name),
        None,
    )
    if playbook is None:
        available = [p.name for p in playbooks]
        print(
            f"error: playbook {args.playbook_name!r} not found under "
            f"{agent_root / 'skills'}.\n"
            f"Available playbooks: {available or '(none)'}",
            file=sys.stderr,
        )
        return 2

    # Construct the parent AtomicAgent (cost universe provider)
    # agents_root is the PARENT of agent_root; agent_name is the directory name.
    agents_root = agent_root.parent
    agent_name = agent_root.name
    # C1 — AtomicAgent's first positional/keyword param is `name`, NOT `agent_name`;
    # `AtomicAgent(agent_name=...)` raised TypeError on every CLI invocation.
    agent = AtomicAgent(name=agent_name, agents_root=agents_root)

    print(
        f"conductor: {'resuming' if args.resume else 'starting'} "
        f"playbook={args.playbook_name!r} "
        f"subject={args.subject!r} "
        f"agent={agent_name!r}",
        flush=True,
    )

    try:
        state = run(
            playbook=playbook,
            subject=args.subject,
            agent=agent,
            conductor_run_id=args.resume,
            max_stage_iterations=args.max_stage_iterations,
            judge_model=args.judge_model,
        )
    except (ConductorConflictScanError, LockBusy) as exc:
        # Fail-closed conflict scan (a goal in the universe was unreadable) or a
        # per-run-lock contention (another invocation holds this conductor_run_id's
        # lease) — neither is a hard failure: this tick simply did not run. Surface a
        # clean retryable message (didn't-complete-this-tick → exit 1, same as
        # halted/awaiting; distinct from the benign 'deferred' STATUS which is exit 0).
        print(
            f"conductor: could not complete this tick ({exc}); refused to execute "
            "(fail-closed). This is transient/retryable — it will retry on the next "
            "scheduled trigger.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"error: run failed: {exc}", file=sys.stderr)
        return 1

    # Print a brief summary
    summary = {
        "conductor_run_id": state.conductor_run_id,
        "status": state.status,
        "halt_reason": state.halt_reason,
        "stages_complete": state.stages_complete,
        "stages_total": state.stages_total,
        "cumulative_spend_usd": round(state.cumulative_spend_usd, 6),
        "run_cap_usd": state.run_cap_usd,
        "cost_data_degraded": state.cost_data_degraded,
        "completed_stage_ids": state.completed_stage_ids,
        # PR3 (#582): what a deferred run is blocked on (None for non-deferred states).
        "queued_behind_decision_id": state.queued_behind_decision_id,
        "queued_behind_conductor_run_id": state.queued_behind_conductor_run_id,
    }
    print(json.dumps(summary, indent=2))

    if state.cost_data_degraded:
        print(
            "\nconductor: WARNING — cost data may be incomplete (a ledger read was "
            "degraded); cumulative_spend_usd may be under-counted.",
            file=sys.stderr,
        )

    # PR3 (#582): a run blocked behind another run's conflict-key gate is DEFERRED,
    # not halted. It self-releases automatically on its next trigger (no operator
    # action required) — so this is a benign, expected outcome, NOT a failure: exit 0.
    # The `resume` SUBCOMMAND is the wrong guidance here (a deferred run owns no gate
    # to answer); the correct action is to re-run the SAME trigger with
    # `run --resume <id>` to poll.
    if state.status == "deferred":
        print(
            f"\nconductor: deferred — run {state.conductor_run_id} is queued behind "
            f"decision {state.queued_behind_decision_id} held by run "
            f"{state.queued_behind_conductor_run_id} "
            f"({state.stages_complete}/{state.stages_total} stages done). "
            f"It will self-release and continue when that gate is answered; "
            f"re-run the same trigger to poll:\n"
            f"  python -m atomic_agents.conductor run {args.playbook_name!r} "
            f"{args.subject!r} {args.agent_root!r} "
            f"--resume {state.conductor_run_id}",
            file=sys.stderr,
        )
        return 0

    if state.status == "complete":
        print(
            f"\nconductor: all {state.stages_total} stages complete "
            f"(spent ${state.cumulative_spend_usd:.4f} of ${state.run_cap_usd:.2f} cap)"
        )
        return 0

    # PR2 (#581): gate suspension — print the pending GateDecision and a copy-pasteable
    # resume hint so the operator knows exactly what to type (cli-resume-subcommand ruling).
    if state.status == "awaiting_decision":
        gd = state.pending_decision
        print(f"\nconductor: suspended at gate stage {gd.stage_id!r}")
        print(f"  decision_id : {gd.decision_id}")
        print(f"  prompt      : {gd.prompt}")
        if gd.options:
            print(f"  options     : {', '.join(gd.options)}")
        if gd.context_ref:
            print(f"  context_ref : {gd.context_ref}")
        hint_agent_root = args.agent_root
        print(
            f"\nTo answer this gate, run:\n"
            f"  python -m atomic_agents.conductor resume {hint_agent_root!r} "
            f"{state.conductor_run_id} \\\n"
            f"      --decision-id {gd.decision_id} \\\n"
            f"      --answer '<your answer>' \\\n"
            f"      --rationale '<your reason>' \\\n"
            f"      --disposition {{continue,skip,halt}}"
        )
        return 1

    # Halted
    print(
        f"\nconductor: halted — {state.halt_reason} "
        f"({state.stages_complete}/{state.stages_total} stages done). "
        f"Resume with: --resume {state.conductor_run_id}",
        file=sys.stderr,
    )
    return 1


def _cmd_resume(args: argparse.Namespace) -> int:
    """Answer a suspended gate and continue the run. Returns exit code."""
    from atomic_agents import AtomicAgent  # noqa: PLC0415
    from atomic_agents.conductor import discover_playbooks, resume  # noqa: PLC0415
    from atomic_agents.exceptions import GoalConcurrentModification  # noqa: PLC0415

    agent_root = Path(args.agent_root).resolve()
    if not agent_root.is_dir():
        print(f"error: agent_root not found: {agent_root}", file=sys.stderr)
        return 2

    model_md = agent_root / "model.md"
    if not model_md.is_file():
        print(
            f"error: {agent_root} does not look like an agent root (no model.md).",
            file=sys.stderr,
        )
        return 2

    # Discover playbooks to find the right manifest.
    # If --playbook-name is given, use that; otherwise scan for the one recorded
    # in the conductor_run_started event.
    playbooks = discover_playbooks(agent_root)

    # Initialized before the branch so the subject auto-detect below (and the
    # final `subject = ... or detected_subject` line) is never an unbound local,
    # regardless of which playbook-resolution path runs (P1: --playbook-name with
    # --subject omitted previously NameError'd here).
    detected_subject: str | None = None

    playbook_name = getattr(args, "playbook_name", None)
    if playbook_name:
        playbook = next((p for p in playbooks if p.name == playbook_name), None)
        if playbook is None:
            available = [p.name for p in playbooks]
            print(
                f"error: playbook {playbook_name!r} not found. "
                f"Available: {available or '(none)'}",
                file=sys.stderr,
            )
            return 2
    else:
        # Auto-detect from the ledger: read the conductor_run_started event.
        from atomic_agents.conductor.run import (  # noqa: PLC0415
            _iter_history_events,
        )

        history_path = (
            agent_root / "goals" / args.conductor_run_id / "goal_history.jsonl"
        )
        detected_name = None
        for rec, ok in _iter_history_events(history_path):
            if ok and rec.get("event") == "conductor_run_started":
                detected_name = rec.get("playbook_name")
                break

        if not detected_name:
            print(
                f"error: could not detect playbook name from ledger "
                f"({args.conductor_run_id}). Pass --playbook-name explicitly.",
                file=sys.stderr,
            )
            return 2

        playbook = next((p for p in playbooks if p.name == detected_name), None)
        if playbook is None:
            print(
                f"error: playbook {detected_name!r} (from ledger) not found under "
                f"{agent_root / 'skills'}.",
                file=sys.stderr,
            )
            return 2

    # Auto-detect subject from the conductor goal intent — runs for BOTH the
    # --playbook-name and the ledger-auto-detect paths, so the `--subject is
    # optional — auto-detected from ledger when omitted` promise holds in both.
    if not getattr(args, "subject", None):
        try:
            from atomic_agents.goal.filesystem import (  # noqa: PLC0415
                FilesystemGoalBackend,
            )

            fb = FilesystemGoalBackend(agent_root, goal_id=args.conductor_run_id)
            g = fb.load_goal(agent_root.name)
            # Extract subject from goal intent: "Run playbook '...' for subject: ..."
            intent = g.intent or ""
            prefix = "for subject: "
            idx = intent.find(prefix)
            detected_subject = intent[idx + len(prefix) :].strip() if idx >= 0 else ""
        except Exception:
            detected_subject = ""

    subject = getattr(args, "subject", None) or detected_subject or ""

    agents_root = agent_root.parent
    agent_name = agent_root.name
    # C1 — same constructor fix as _cmd_run: the param is `name`, not `agent_name`.
    agent = AtomicAgent(name=agent_name, agents_root=agents_root)

    print(
        f"conductor: resuming run {args.conductor_run_id!r} "
        f"(decision_id={args.decision_id!r}, disposition={args.disposition!r})",
        flush=True,
    )

    # H4 — build the gate-ruling principal. The local CLI operator is trusted by
    # construction, so an --answered-by identity is recorded as a VERIFIED local
    # principal (is_verified=True), satisfying resume()'s C2 HARD-REFUSE. Omitted →
    # None, so resume() defaults to LOCAL_PRINCIPAL ('local').
    principal = None
    if args.answered_by:
        from atomic_agents.principal.types import Principal  # noqa: PLC0415

        principal = Principal(
            identifier=args.answered_by,
            derivation_source="local",
            is_verified=True,
        )

    try:
        state = resume(
            playbook=playbook,
            subject=subject,
            agent=agent,
            conductor_run_id=args.conductor_run_id,
            decision_id=args.decision_id,
            answer=args.answer,
            disposition=args.disposition,
            rationale=args.rationale,
            principal=principal,
            max_stage_iterations=args.max_stage_iterations,
            judge_model=args.judge_model,
        )
    except GoalConcurrentModification as exc:
        # H4(d) — a GoalConcurrentModification can mean EITHER a genuinely
        # stale/duplicate decision_id OR that this answer was already recorded and
        # the run just needs continuing. Point the operator at the recovery path.
        print(
            f"error: gate answer rejected ({exc}).\n"
            "If you have NOT answered this gate before, re-check the --decision-id.\n"
            "If the answer was ALREADY recorded (e.g. a retried/duplicate resume), "
            "the gate is no longer pending — continue the run with:\n"
            f"  python -m atomic_agents.conductor run <playbook> <subject> "
            f"{args.agent_root!r} --resume {args.conductor_run_id}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"error: resume failed: {exc}", file=sys.stderr)
        return 1

    # Print summary (same shape as _cmd_run)
    summary = {
        "conductor_run_id": state.conductor_run_id,
        "status": state.status,
        "halt_reason": state.halt_reason,
        "stages_complete": state.stages_complete,
        "stages_total": state.stages_total,
        "cumulative_spend_usd": round(state.cumulative_spend_usd, 6),
        "run_cap_usd": state.run_cap_usd,
        "cost_data_degraded": state.cost_data_degraded,
        "completed_stage_ids": state.completed_stage_ids,
        # PR3 (#582): what a deferred run is blocked on (None for non-deferred states).
        "queued_behind_decision_id": state.queued_behind_decision_id,
        "queued_behind_conductor_run_id": state.queued_behind_conductor_run_id,
    }
    print(json.dumps(summary, indent=2))

    # H4(c) — surface a degraded cost read, same as _cmd_run (otherwise a resumed
    # run silently hides an under-counted spend total).
    if state.cost_data_degraded:
        print(
            "\nconductor: WARNING — cost data may be incomplete (a ledger read was "
            "degraded); cumulative_spend_usd may be under-counted.",
            file=sys.stderr,
        )

    if state.status == "complete":
        print(
            f"\nconductor: all {state.stages_total} stages complete "
            f"(spent ${state.cumulative_spend_usd:.4f} of ${state.run_cap_usd:.2f} cap)"
        )
        return 0

    if state.status == "awaiting_decision":
        gd = state.pending_decision
        print(f"\nconductor: suspended at gate stage {gd.stage_id!r}")
        print(f"  decision_id : {gd.decision_id}")
        print(f"  prompt      : {gd.prompt}")
        if gd.options:
            print(f"  options     : {', '.join(gd.options)}")
        print(
            f"\nTo answer this gate, run:\n"
            f"  python -m atomic_agents.conductor resume {args.agent_root!r} "
            f"{state.conductor_run_id} \\\n"
            f"      --decision-id {gd.decision_id} \\\n"
            f"      --answer '<your answer>' \\\n"
            f"      --rationale '<your reason>' \\\n"
            f"      --disposition {{continue,skip,halt}}"
        )
        return 1

    # PR3 (#582): answering this gate (continue/skip) delegated to run(), which can
    # land DEFERRED behind ANOTHER run's conflict-key gate. That is benign and
    # self-releasing (no operator action) — exit 0, and point the operator at the
    # poll trigger (NOT --resume, which answers a gate this run does not own).
    if state.status == "deferred":
        # Use the RESOLVED playbook + subject (not args.*, which are None when the
        # operator relied on ledger auto-detect) so the poll hint is fully runnable.
        _pb = repr(playbook.name)
        _subj = repr(subject)
        print(
            f"\nconductor: deferred — run {state.conductor_run_id} is queued behind "
            f"decision {state.queued_behind_decision_id} held by run "
            f"{state.queued_behind_conductor_run_id} "
            f"({state.stages_complete}/{state.stages_total} stages done). "
            f"It will self-release and continue when that gate is answered; "
            f"re-run the same trigger to poll:\n"
            f"  python -m atomic_agents.conductor run {_pb} {_subj} "
            f"{args.agent_root!r} --resume {state.conductor_run_id}",
            file=sys.stderr,
        )
        return 0

    print(
        f"\nconductor: halted — {state.halt_reason} "
        f"({state.stages_complete}/{state.stages_total} stages done).",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        sys.exit(_cmd_run(args))
    elif args.command == "resume":
        sys.exit(_cmd_resume(args))
    else:
        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

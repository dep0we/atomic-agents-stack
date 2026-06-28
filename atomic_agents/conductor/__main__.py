"""Minimal conductor CLI — python -m atomic_agents.conductor (spec/50 PR1).

Usage:
    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root>
    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root> \\
        [--resume <conductor_run_id>]
    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root> \\
        [--max-stage-iterations N]

Arguments:
    playbook_name    Name of the playbook (must match the 'name:' field in
                     PLAYBOOK.md, searched under <agent_root>/skills/).
    subject          Work subject string (e.g. "feature #1234", "Q3 report").
    agent_root       Absolute or relative path to the agent's root directory.
                     Must contain model.md with cost guardrails.

Options:
    --resume ID      Resume an existing run by conductor_run_id.
    --max-stage-iterations N
                     Max LLM iterations per stage (default: 3).
    --judge-model M  LLM model to use for the outcome judge.

Exits:
    0 — run completed (all stages satisfied)
    1 — run halted (gate stage, cap exhausted, or stage failure)
    2 — usage / configuration error

PR2 (#581) will add: await-decision, resume interactivity, gate-stage display.
PR3 (#582) will add: --concurrency flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m atomic_agents.conductor",
        description="Run a PLAYBOOK.md conductor run against an agent (spec/50 PR1).",
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
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute or resume a conductor run. Returns exit code."""
    from atomic_agents import AtomicAgent  # noqa: PLC0415
    from atomic_agents.conductor import discover_playbooks, run  # noqa: PLC0415

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
    agent = AtomicAgent(agents_root=agents_root, agent_name=agent_name)

    print(
        f"conductor: {'resuming' if args.resume else 'starting'} "
        f"playbook={args.playbook_name!r} "
        f"subject={args.subject!r} "
        f"agent={agent_name!r}",
        flush=True,
    )

    state = run(
        playbook=playbook,
        subject=args.subject,
        agent=agent,
        conductor_run_id=args.resume,
        max_stage_iterations=args.max_stage_iterations,
        judge_model=args.judge_model,
    )

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
    }
    print(json.dumps(summary, indent=2))

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

    # Halted
    print(
        f"\nconductor: halted — {state.halt_reason} "
        f"({state.stages_complete}/{state.stages_total} stages done). "
        f"Resume with: --resume {state.conductor_run_id}",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        sys.exit(_cmd_run(args))
    else:
        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

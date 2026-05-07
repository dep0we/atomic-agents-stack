"""CLI for atomic_agents — the `atomic-agents` console script.

Usage:
    atomic-agents run <agent> [options]

For now, just supports the `run` subcommand. Future: `eval`, `tune`, `dashboard`, `goal`.
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

from .agent import AtomicAgent
from ._platform import get_agents_root
from .exceptions import AtomicAgentsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atomic-agents", description="Atomic Agents CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run an agent against a work item")
    run.add_argument("agent", help="agent name (folder under agents-root)")
    run.add_argument("--work-item", required=True, help="user message / work item text")
    run.add_argument("--trigger", default="manual", choices=["cron", "skill", "manual", "api"])
    run.add_argument("--model", default=None, help="override default model")
    run.add_argument("--critical", action="store_true", help="bypass cost guardrails")
    run.add_argument("--no-write-captures", action="store_true",
                      help="extract captures but don't persist (dry-run)")
    run.add_argument("--agents-root", default=None,
                      help="override ATOMIC_AGENTS_ROOT")

    info = sub.add_parser("info", help="Show config for an agent without running it")
    info.add_argument("agent")
    info.add_argument("--agents-root", default=None)

    args = parser.parse_args(argv)

    agents_root = Path(args.agents_root).expanduser().resolve() if args.agents_root else get_agents_root()

    try:
        if args.cmd == "run":
            return _cmd_run(args, agents_root)
        elif args.cmd == "info":
            return _cmd_info(args, agents_root)
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_run(args, agents_root: Path) -> int:
    agent = AtomicAgent(
        name=args.agent,
        trigger=args.trigger,
        agents_root=agents_root,
    )
    response = agent.call(
        work_item=args.work_item,
        model_override=args.model,
        critical=args.critical,
        write_captures=not args.no_write_captures,
    )
    if response.skipped:
        print(f"[SKIPPED] {response.skip_reason}", file=sys.stderr)
        return 2
    print(response.text)
    print("", file=sys.stderr)
    print(f"--- Stats: model={response.model} "
          f"in={response.input_tokens} out={response.output_tokens} "
          f"cost=${response.cost_usd:.4f} "
          f"latency={response.latency_ms}ms "
          f"captures={len(response.captures)}", file=sys.stderr)
    return 0


def _cmd_info(args, agents_root: Path) -> int:
    agent = AtomicAgent(
        name=args.agent,
        trigger="manual",
        agents_root=agents_root,
    )
    cfg = agent.config
    print(f"Agent: {agent.name}")
    print(f"Root:  {agent.agent_root}")
    print(f"Default model: {cfg.default_model}")
    print(f"Fallback:      {cfg.fallback_model}")
    print(f"Cost guardrails enabled: {cfg.cost_guardrails_enabled}")
    if cfg.cost_guardrails_enabled:
        print(f"  Daily cap:   ${cfg.daily_cap_usd}  → action: {cfg.daily_cap_action}")
        print(f"  Monthly cap: ${cfg.monthly_cap_usd} → action: {cfg.monthly_cap_action}")
        print(f"  Warning thresholds: {cfg.warning_thresholds}")
    print(f"Read paths:   {len(cfg.read_paths)}")
    for p in cfg.read_paths:
        print(f"  • {p}")
    print(f"Write paths:  {len(cfg.write_paths)}")
    for p in cfg.write_paths:
        print(f"  • {p}")
    print(f"External APIs: {cfg.external_apis}")
    print(f"Hard NOs:      {len(cfg.hard_nos)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())

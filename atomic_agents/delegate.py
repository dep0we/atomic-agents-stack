"""CLI entry point for agent-to-agent delegation.

Usage:
    python -m atomic_agents.delegate <coordinator> --target <agent> --work-item "..." [--critical]

Prints the target agent's response text, followed by a delegation rollup.
Exit codes:
    0 — success
    1 — roster / cost / self-delegation failure
    2 — response was skipped (cost guardrail)
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from .agent import AtomicAgent
from ._platform import get_agents_root
from .exceptions import (
    AtomicAgentsError,
    CostGuardrailBlocked,
    NotInRoster,
    SelfDelegationError,
)
from .mcp_registry import MCPRegistryError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m atomic_agents.delegate",
        description="Delegate a work item from a coordinator agent to a target agent.",
    )
    parser.add_argument("coordinator", help="coordinator agent name")
    parser.add_argument("--target", required=True, help="target agent name")
    parser.add_argument("--work-item", required=True, help="work item text")
    parser.add_argument(
        "--critical",
        action="store_true",
        help="bypass cost guardrails (still logged)",
    )
    parser.add_argument(
        "--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT"
    )

    args = parser.parse_args(argv)
    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root
        else get_agents_root()
    )

    try:
        coordinator = AtomicAgent(
            name=args.coordinator,
            trigger="manual",
            agents_root=agents_root,
        )
        response = coordinator.delegate(
            target_agent_name=args.target,
            work_item=args.work_item,
            critical=args.critical,
        )
    except (
        NotInRoster,
        SelfDelegationError,
        CostGuardrailBlocked,
        MCPRegistryError,
    ) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if response.skipped:
        print(f"[SKIPPED] {response.skip_reason}", file=sys.stderr)
        return 2

    print(response.text)

    # Print delegation rollup
    print("", file=sys.stderr)
    print(
        f"--- Delegation: coordinator={args.coordinator} "
        f"target={args.target} "
        f"model={response.model} "
        f"in={response.input_tokens} out={response.output_tokens} "
        f"cost=${response.cost_usd:.4f} "
        f"latency={response.latency_ms}ms "
        f"captures={len(response.captures)}",
        file=sys.stderr,
    )
    if coordinator._delegations_this_run:
        print(
            f"--- Rollup: {json.dumps(coordinator._delegations_this_run, indent=2)}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CLI entry: python -m atomic_agents.dashboard <subcommand>"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

from .render import render_all, render_global
from .serve import serve as serve_cmd
from ..core_api import get_agents_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atomic-agents.dashboard",
        description="Atomic Agents cost & observability dashboard",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    render = sub.add_parser("render", help="Render dashboards (default: all tabs)")
    render.add_argument(
        "--agents-root", default=None, help="Override ATOMIC_AGENTS_ROOT"
    )
    render.add_argument(
        "--tab",
        default="all",
        choices=["all", "cost", "activity", "quality", "memory", "goals"],
        help="Render only one tab (default: all)",
    )

    serve_p = sub.add_parser("serve", help="Run a local web server (port 8765)")
    serve_p.add_argument("--agents-root", default=None)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root
        else get_agents_root()
    )

    if not agents_root.exists():
        print(f"Error: agents_root does not exist: {agents_root}", file=sys.stderr)
        print(f"Set ATOMIC_AGENTS_ROOT or pass --agents-root", file=sys.stderr)
        return 1

    if args.cmd == "render":
        tab = getattr(args, "tab", "all")
        written = render_all(agents_root, tab=tab)
        if written.get("global"):
            print(f"Wrote global dashboard:  {written['global']}")
        for path in written.get("per_agent", []):
            print(f"Wrote per-agent dashboard: {path}")
        for key in ("activity", "quality", "memory", "goals"):
            if written.get(key):
                print(f"Wrote {key} dashboard: {written[key]}")
        return 0

    if args.cmd == "serve":
        serve_cmd(agents_root, host=args.host, port=args.port)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

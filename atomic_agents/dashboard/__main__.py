"""CLI entry: python -m atomic_agents.dashboard <subcommand>"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

from .render import render_all
from .serve import serve as serve_cmd
from .._platform import get_agents_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atomic-agents.dashboard",
        description="Atomic Agents cost & observability dashboard",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    render = sub.add_parser("render", help="Render dashboards (default)")
    render.add_argument("--agents-root", default=None,
                         help="Override ATOMIC_AGENTS_ROOT")

    serve_p = sub.add_parser("serve", help="Run a local web server (port 8765)")
    serve_p.add_argument("--agents-root", default=None)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root else get_agents_root()
    )

    if not agents_root.exists():
        print(f"Error: agents_root does not exist: {agents_root}", file=sys.stderr)
        print(f"Set ATOMIC_AGENTS_ROOT or pass --agents-root", file=sys.stderr)
        return 1

    if args.cmd == "render":
        written = render_all(agents_root)
        print(f"Wrote global dashboard:  {written['global']}")
        for path in written["per_agent"]:
            print(f"Wrote per-agent dashboard: {path}")
        return 0

    if args.cmd == "serve":
        serve_cmd(agents_root, host=args.host, port=args.port)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

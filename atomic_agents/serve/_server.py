"""serve/_server.py — server startup and CLI dispatch.

Handles:
  - Parsing args from the CLI subcommand
  - Loading serve.md + applying env var overrides
  - No-auth startup checks (refuse non-loopback without --allow-no-auth)
  - Startup warning on loopback with no auth
  - Building the Starlette app (eager serve.md parse per spec/37 MUST 2)
  - Running uvicorn

This module is only imported when starlette + uvicorn are installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .._platform import get_agents_root
from ._config import ServeConfig, is_loopback, load_serve_config


def run_server(args: object) -> int:
    """Entry point called by the CLI `atomic-agents serve` subcommand.

    Validates config, prints startup warnings/errors, then starts uvicorn.
    Returns 0 on clean exit, 1 on configuration error.

    spec/37:
    - MUST 2: serve.md parsed eagerly before accepting requests
    - MUST 3: --host / --port flags (not --bind)
    - No-auth default: refuse non-loopback without --allow-no-auth
    """
    import uvicorn  # noqa: PLC0415 -- lazy; only installed with serve extra

    from ._app import make_app  # noqa: PLC0415

    # Resolve agents root
    agents_root: Path | None = None
    if hasattr(args, "agents_root") and args.agents_root:
        agents_root = Path(args.agents_root).expanduser().resolve()
    else:
        agents_root = get_agents_root()

    # Resolve agent name (single-agent vs all-agents) and enforce mutual exclusion.
    agent_name: str | None = getattr(args, "agent", None)
    serve_all: bool = bool(getattr(args, "serve_all", False))

    if agent_name and serve_all:
        print(
            "Error: <agent> and --all are mutually exclusive. "
            "Specify one agent name OR --all, not both.",
            file=sys.stderr,
        )
        return 1
    if not agent_name and not serve_all:
        print(
            "Error: specify an agent name to serve a single agent, "
            "or pass --all to serve every agent in the vault.",
            file=sys.stderr,
        )
        return 1

    # Determine which serve.md to read for the server-wide bind config.
    # In single-agent mode the agent's serve.md sets host/port/identity-header
    # for the entire server (one wrapper, one agent).
    # In --all mode no per-agent serve.md applies; defaults + env vars are used.
    serve_config: ServeConfig
    if agent_name:
        agent_root = agents_root / agent_name
        if not agent_root.is_dir():
            print(
                f"Error: agent folder not found: {agent_root}",
                file=sys.stderr,
            )
            return 1
        # MUST 2: parse eagerly; malformed file → exit before accepting requests
        try:
            serve_config = load_serve_config(agent_root)
        except (OSError, ValueError) as e:
            print(
                f"Error: failed to load serve.md for agent {agent_name!r}: {e}",
                file=sys.stderr,
            )
            return 1
    else:
        # --all mode: use defaults + env var overrides; no per-agent serve.md.
        # MUST 2: malformed env var overrides (e.g. non-integer ATOMIC_AGENTS_SERVE_PORT)
        # MUST exit with a clear error, not an uncaught traceback. Same guard as
        # single-agent mode above.
        from ._config import _parse_serve_md  # noqa: PLC0415

        try:
            serve_config = _parse_serve_md("")
        except (OSError, ValueError) as e:
            print(
                f"Error: invalid serve env-var override: {e}",
                file=sys.stderr,
            )
            return 1

    # CLI flags override serve.md (CLI is the highest-priority override)
    if hasattr(args, "host") and args.host:
        serve_config.host = args.host
    if hasattr(args, "port") and args.port:
        serve_config.port = args.port
    if getattr(args, "allow_no_auth", False):
        serve_config.allow_no_auth = True

    # No-auth default check. spec/37 §"No-auth default".
    if not is_loopback(serve_config.host):
        if not serve_config.allow_no_auth:
            print(
                f"Error: atomic-agents serve refuses to bind to {serve_config.host!r} "
                f"without auth.\n"
                f"Add --allow-no-auth or '## Allow No Auth' to serve.md only after\n"
                f"your perimeter (IAP, ALB, Cloudflare Access, Tailscale Serve, etc.)\n"
                f'is in place. See docs/deployment/serve.md §"No-auth default".',
                file=sys.stderr,
            )
            return 1
        else:
            # Non-loopback + allow_no_auth explicitly set — warn prominently.
            print(
                "Warning: atomic-agents serve is running with no auth on a network "
                f"address ({serve_config.host}:{serve_config.port}).\n"
                "Ensure your perimeter (IAP, ALB, Cloudflare Access, Tailscale Serve,\n"
                "etc.) is in place before exposing this server to the internet.",
                file=sys.stderr,
            )
    else:
        # Loopback: warn but proceed — acceptable for local dev.
        print(
            "Warning: atomic-agents serve is running with no auth on loopback "
            f"({serve_config.host}:{serve_config.port}).\n"
            "This is acceptable for local development only. Configure your\n"
            "perimeter before exposing this server to a network.",
            file=sys.stderr,
        )

    # Build app with per-mode agent scoping and identity_header baked in.
    # In single-agent mode make_app scopes all routes to agent_name (other
    # agents → 404). In --all mode agent_name=None serves the whole vault.
    app = make_app(
        agents_root=agents_root,
        agent_name=agent_name if agent_name else None,
        identity_header=serve_config.identity_header,
    )
    # Override state in case make_app default differs (e.g. operator-configured
    # identity header from serve.md); make_app already sets the default but
    # _server owns the resolved config.
    app.state.identity_header = serve_config.identity_header

    print(
        f"atomic-agents serve: starting on http://{serve_config.host}:{serve_config.port}/",
        file=sys.stderr,
    )
    if agent_name:
        print(f"  agent: {agent_name}", file=sys.stderr)
    else:
        print("  mode: all agents in vault", file=sys.stderr)
    print(
        f"  identity header: {serve_config.identity_header}",
        file=sys.stderr,
    )

    uvicorn.run(
        app,
        host=serve_config.host,
        port=serve_config.port,
        log_level="info",
    )
    return 0

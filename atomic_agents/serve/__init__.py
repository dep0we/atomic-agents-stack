"""atomic_agents.serve — thin HTTP wrapper for Cloud Run / containerized serving.

Exposes agent.call() over HTTP via Starlette. Designed for the GCP-as-harness
pattern: the framework owns the agent loop, the operator's infrastructure owns
auth, rate limiting, TLS, and audit logging.

Install the serve extra to use this module:

    pip install atomic-agents-stack[serve]

See docs/spec/37-serve.md for the full contract.

Routes shipped in issue #342:
  POST /agents/<name>/call    — invoke agent.call(), return the response
  GET  /agents/<name>/healthz — cheap liveness check (vault loadable)
  GET  /agents/<name>/doctor  — full doctor run (off the hot path)
  GET  /agents               — list available agent names

Out of scope in this arc: POST /mcp/<name> (MCP server endpoint, own arc; tracked
in issue #90). Streaming deferred: all current LLMBackend impls return
capabilities().streaming=False (tracked in issue #105).
"""

from __future__ import annotations


def _require_serve_extra() -> None:
    """Raise a clear ImportError if starlette or uvicorn is not installed."""
    missing = []
    try:
        import starlette  # noqa: F401
    except ImportError:
        missing.append("starlette")
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("uvicorn")
    if missing:
        raise ImportError(
            "Install atomic-agents-stack[serve] to use the HTTP serve module.\n"
            "  pip install atomic-agents-stack[serve]\n"
            "See docs/spec/37-serve.md for details."
        ) from None


def run_serve(args) -> int:  # type: ignore[type-arg]
    """Entry point for the `atomic-agents serve` CLI subcommand.

    Lazy-imports the serve app so that importing atomic_agents does not pull
    in Starlette or uvicorn at framework import time. Matches the lazy-import
    pattern from init/__init__.py. Spec/37 MUST 1.
    """
    _require_serve_extra()
    from ._server import run_server  # noqa: PLC0415 -- intentional lazy import

    return run_server(args)

"""serve/_runner.py — central sync→async adapter for agent.call().

This is the thread-pool executor dispatch that makes agent.call() (synchronous,
contains asyncio.run() for MCP) safe to call from Starlette's async event loop.

Architecture (TENSIONS.md T2 §"Decision recorded" — Hybrid Option C):
  - agent.call() runs in a thread-pool executor thread (no running event loop
    there, so asyncio.run() inside MCP tool calls is legal).
  - Starlette's event loop dispatches and awaits the Future; it is never blocked.
  - This is the single central adapter. All HTTP-triggered agent calls go through
    run_agent_call(). spec/37 MUST 9.

The async-first rebuild is explicitly deferred to TENSIONS.md T2 triggers:
  - MCP tool calls per session sustained >20 under concurrent HTTP load, OR
  - >50 concurrent requests with P95 queue wait >500ms.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .._platform import get_agents_root
from ..agent import AtomicAgent
from ..exceptions import LockBusy

# Module-level thread pool. Shared across all requests; sized by the OS default
# (min(32, os.cpu_count() + 4) on CPython 3.8+).
# ATOMIC_AGENTS_SERVE_WORKERS is reserved for a future pool-sizing knob but is
# NOT read today — setting it has no effect. Add it only when a real need
# surfaces. CLAUDE.md: "don't add abstractions for hypothetical future needs."
_EXECUTOR: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    """Lazily create the module-level executor."""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor()
    return _EXECUTOR


def shutdown_executor() -> None:
    """Shut down the executor on server teardown (call from lifespan handler)."""
    global _EXECUTOR
    if _EXECUTOR is not None:
        _EXECUTOR.shutdown(wait=True)
        _EXECUTOR = None


class LockBusyWithRunId(LockBusy):
    """LockBusy subclass that carries the pre-lock-acquisition run_id.

    agent.call() resets run_id BEFORE lock acquisition (MUST 8), so even a
    refused lock_busy call has a unique run_id in the JSONL audit log. This
    subclass threads that run_id back to the HTTP handler so the caller can
    include it in the 503 response body for audit correlation.
    """

    def __init__(self, original: LockBusy, run_id: str) -> None:
        super().__init__(str(original))
        self.run_id = run_id


async def run_agent_call(
    name: str,
    work_item: str,
    *,
    model_override: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    caller_identity: str | None = None,
    agents_root: Path | None = None,
) -> tuple[str, Any]:
    """Dispatch agent.call() in a thread-pool executor.

    This is the central adapter between Starlette's async event loop and
    agent.call()'s synchronous implementation. Running in a thread means
    asyncio.run() calls inside mcp.py are legal. spec/37 MUST 9.

    Returns ``(run_id, response)`` where ``run_id`` is the per-call unique
    identifier (reset at the start of each call() invocation, MUST 8) and
    ``response`` is the Response object from agent.call().

    Raises:
        AtomicAgentsError       — agent not found or vault error (→ HTTP 404/500)
        LockBusyWithRunId       — agent locked by another call (→ HTTP 503);
                                  carries ``.run_id`` for the 503 response body
        Various                 — pass through to the HTTP handler
    """
    _root = agents_root or get_agents_root()
    loop = asyncio.get_running_loop()

    def _call() -> tuple[str, Any]:
        # Fresh agent per call: AtomicAgent.__init__ validates the folder;
        # call() resets self.run_id at the start of each invocation (MUST 8).
        # trigger='http' maps to primitive='agent_call' in _PRIMITIVE_BY_TRIGGER.
        agent = AtomicAgent(name=name, trigger="http", agents_root=_root)
        try:
            response = agent.call(
                work_item=work_item,
                model_override=model_override,
                max_tokens=max_tokens,
                temperature=temperature,
                # critical is hard-coded to False — structurally unavailable via HTTP.
                # spec/37 MUST 5.
                critical=False,
                caller_identity=caller_identity,
            )
        except LockBusy as e:
            # Attach run_id to the exception so the HTTP handler can include it
            # in the 503 response body. agent.run_id was reset before lock
            # acquisition (MUST 8), so the JSONL audit record and the HTTP 503
            # body carry the same run_id — the caller can correlate. CLAUDE.md
            # principle 5 (audit trail is structural).
            raise LockBusyWithRunId(e, agent.run_id) from e
        # Capture agent.run_id after call() returns — call() resets it at the
        # start, so this is the run_id that was written to the JSONL log.
        # For the skipped (cost-cap) path, agent.run_id is also set correctly
        # because run_id is reset before the cost-guardrails check.
        return agent.run_id, response

    return await loop.run_in_executor(_get_executor(), _call)

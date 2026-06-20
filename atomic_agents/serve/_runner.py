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
from ..conversation.types import LOCAL_PRINCIPAL, Principal
from ..exceptions import DedupInFlight, LockBusy, UnverifiedPrincipalConversationAccess

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


class DedupInFlightWithRunId(DedupInFlight):
    """DedupInFlight subclass that carries the agent's current run_id.

    Mirrors LockBusyWithRunId — threads the pre-dedup-check run_id back to
    the HTTP handler so the 409 response body can carry both the run_id for
    the current (refused) invocation and prior_run_id of the in-flight call.
    spec/45 PR2.
    """

    def __init__(self, original: DedupInFlight, run_id: str) -> None:
        super().__init__(str(original), prior_run_id=original.prior_run_id)
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
    idempotency_key: str | None = None,
    verified_claims: dict | None = None,
    identity_perimeter_verified: bool = False,
    conversation_id: str | None = None,
) -> tuple[str, Any]:
    """Dispatch agent.call() in a thread-pool executor.

    This is the central adapter between Starlette's async event loop and
    agent.call()'s synchronous implementation. Running in a thread means
    asyncio.run() calls inside mcp.py are legal. spec/37 MUST 9.

    Returns ``(run_id, response)`` where ``run_id`` is the per-call unique
    identifier (reset at the start of each call() invocation, MUST 8) and
    ``response`` is the Response object from agent.call().

    Raises:
        AtomicAgentsError         — agent not found or vault error (→ HTTP 404/500)
        LockBusyWithRunId         — agent locked by another call (→ HTTP 503);
                                    carries ``.run_id`` for the 503 response body
        DedupInFlightWithRunId    — key is IN_FLIGHT (→ HTTP 409, spec/45 PR2);
                                    carries ``.run_id`` + ``.prior_run_id``
        Various                   — pass through to the HTTP handler
    """
    _root = agents_root or get_agents_root()
    loop = asyncio.get_running_loop()

    def _call() -> tuple[str, Any]:
        # Fresh agent per call: AtomicAgent.__init__ validates the folder;
        # call() resets self.run_id at the start of each invocation (MUST 8).
        # trigger='http' maps to primitive='agent_call' in _PRIMITIVE_BY_TRIGGER.
        agent = AtomicAgent(name=name, trigger="http", agents_root=_root)

        # spec/48 HYBRID: derive Principal from perimeter-verified claims.
        # The serve layer NEVER re-verifies — it trusts that the perimeter
        # (IAP, OIDC middleware) already verified the identity. Whether the raw
        # identity header may be TRUSTED as a verified claim is an explicit
        # operator opt-in resolved in _app.py (identity_is_perimeter_verified AND
        # non-loopback bind) and surfaced here as identity_perimeter_verified +
        # the presence of verified_claims. Resolution happens inside _runner
        # (where the AtomicAgent instance is already constructed) so the per-agent
        # principal_backend is the resolution authority, not a throw-away backend
        # in _app.py.
        #
        # Three cases (mirroring _app.py's verified_claims construction):
        #   (1) caller_identity is None AND perimeter-trust NOT enabled → home-user
        #       / no identity header, no perimeter → LOCAL_PRINCIPAL (is_verified=
        #       True, local single-user). NOTE: the `not identity_perimeter_verified`
        #       conjunct is SECURITY-LOAD-BEARING — in a perimeter-trusted (non-
        #       loopback multi-tenant) deployment, a request that OMITS the identity
        #       header must NOT collapse to the shared verified 'local' namespace
        #       (a fail-open). When perimeter-trust is on and the header is absent,
        #       this falls through to the fail-closed UNVERIFIED branch below.
        #   (2) caller_identity present but perimeter-trust NOT enabled (default,
        #       or loopback bind), OR perimeter-trust ON but the identity header is
        #       absent (verified_claims is None) → produce a fail-closed UNVERIFIED
        #       Principal (NOT LOCAL_PRINCIPAL — that would wrongly pass the
        #       HARD-REFUSE gate). A conversation_id caller is then refused.
        #   (3) caller_identity present AND perimeter-trust enabled → derive via
        #       the registered PrincipalBackend (may be is_verified=True).
        # The fail-closed Principal in case (2) is constructed directly (not via a
        # backend) so the posture does NOT depend on which backend is registered —
        # LocalPrincipalBackend would otherwise return LOCAL_PRINCIPAL (verified)
        # for any input and silently re-open the hole.
        if caller_identity is None and not identity_perimeter_verified:
            principal = LOCAL_PRINCIPAL
        elif identity_perimeter_verified and verified_claims is not None:
            # Misconfiguration guard (cross-tenant collapse): perimeter-trust is
            # enabled, but if the registered backend is is_local_only (e.g. the
            # operator turned on identity_is_perimeter_verified but forgot to set
            # ATOMIC_AGENTS_PRINCIPAL_BACKEND, leaving the default
            # LocalPrincipalBackend), derive_principal() IGNORES the claims and
            # returns LOCAL_PRINCIPAL (is_verified=True) for EVERY distinct caller
            # — collapsing all tenants onto conversation 'local' and silently
            # mixing their turns. Fail closed: a local-only backend cannot honor a
            # perimeter-verified multi-tenant claim, so mint an unverified
            # Principal and let the agent.call() HARD-REFUSE gate fire instead of
            # leaking. (doctor.check_principal_backend stays advisory; this is the
            # load-bearing runtime guard.)
            if agent.principal_backend.capabilities().is_local_only:
                principal = Principal(
                    identifier="unverified",
                    derivation_source="serve_local_backend_misconfig",
                    is_verified=False,
                )
            else:
                principal = agent.principal_backend.derive_principal(verified_claims)
        else:
            # Identity header present but the perimeter is not trusted: refuse to
            # mint a verified principal. is_verified=False → HARD-REFUSE on any
            # conversation_id at the agent.call() door.
            principal = Principal(
                identifier="unverified",
                derivation_source="serve_untrusted_perimeter",
                is_verified=False,
            )

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
                idempotency_key=idempotency_key,
                principal=principal,
                conversation_id=conversation_id,
            )
        except LockBusy as e:
            # Attach run_id to the exception so the HTTP handler can include it
            # in the 503 response body. agent.run_id was reset before lock
            # acquisition (MUST 8), so the JSONL audit record and the HTTP 503
            # body carry the same run_id — the caller can correlate. CLAUDE.md
            # principle 5 (audit trail is structural).
            raise LockBusyWithRunId(e, agent.run_id) from e
        except DedupInFlight as e:
            # spec/45 PR2: thread the run_id alongside prior_run_id so the HTTP
            # handler can build a 409 body with both correlation handles.
            # agent.run_id was reset before the lookup (MUST 8), matching the
            # JSONL in_flight audit record for this call.
            raise DedupInFlightWithRunId(e, agent.run_id) from e
        # Capture agent.run_id after call() returns — call() resets it at the
        # start, so this is the run_id that was written to the JSONL log.
        # For the skipped (cost-cap) path, agent.run_id is also set correctly
        # because run_id is reset before the cost-guardrails check.
        return agent.run_id, response

    return await loop.run_in_executor(_get_executor(), _call)

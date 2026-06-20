"""Canonical types for the PrincipalBackend Protocol (spec/48).

PrincipalBackend is the twenty-first backend Protocol in the atomic-agents
framework (v1.5 wave). It provides identity derivation: mapping already-verified
external claims (e.g. OIDC sub, IAP user-id) into a typed Principal that
agent.call() can use to enforce conversation ownership and the is_verified gate.

This module is a dependency-free leaf. It re-exports Principal and LOCAL_PRINCIPAL
from conversation/types.py — Principal is canonically defined there (spec/47) and
this re-export preserves that canonical home. PrincipalBackend.derive_principal()
returns atomic_agents.conversation.types.Principal; there is NO separate
PrincipalBackend-specific Principal class.

NOTE on re-export: Principal MUST remain in conversation/types.py as the single
canonical definition. Introducing a second class here would break isinstance()
checks throughout agent.py. This re-export makes principal/ importable without
requiring callers to know where Principal lives, while keeping the canonical home
in spec/47.

WritePolicy is NOT part of the PrincipalBackend Protocol. PrincipalBackend
performs identity derivation only — no storage path, no write-path enforcement.
The conformance suite MUST NOT include a WritePolicy test for PrincipalBackend.
(Mirrors idempotency/types.py lines 11-14.)

spec/40 Export Exemption:
    PrincipalBackend is an identity-derivation Protocol, NOT a state store. It
    holds no durable per-principal data. PrincipalCapabilities therefore carries
    NO supports_canonical_export field. This is an intentional omission, not an
    oversight — do NOT add supports_canonical_export to PrincipalCapabilities in
    a future PR without re-examining whether a persistent principal store has been
    introduced.

See docs/spec/48-principal-backend.md for the full normative contract.
"""

from __future__ import annotations

from dataclasses import dataclass

# Re-export Principal and LOCAL_PRINCIPAL from their canonical home (spec/47).
# Principal MUST NOT be redefined here — callers that import from atomic_agents.principal
# get the identical class as callers that import from atomic_agents.conversation.types.
from ..conversation.types import LOCAL_PRINCIPAL, Principal

__all__ = [
    "Principal",
    "LOCAL_PRINCIPAL",
    "PrincipalCapabilities",
]


@dataclass(frozen=True)
class PrincipalCapabilities:
    """Per-backend capability declaration for PrincipalBackend (spec/48).

    All boolean fields default to False so new fields can be appended without
    breaking existing instantiation sites (backward-compat pattern from
    ConversationCapabilities / IdempotencyCapabilities).

    spec/40 Export Exemption: PrincipalBackend is an identity-derivation Protocol,
    NOT a state store. PrincipalCapabilities does NOT carry supports_canonical_export.
    This is intentional — do NOT add that field without introducing a persistent
    principal store.

    Fields:
        backend_id: stable backend identifier (required, no default). MUST be
            a static lowercase string; MUST NOT change across process restarts
            or re-instantiation. Placed FIRST so positional construction fails
            loudly (TypeError) when the required field is omitted.
        is_local_only: True when the backend is safe ONLY for single-user
            local deployments and produces LOCAL_PRINCIPAL for all inputs.
            LocalPrincipalBackend claims True. All other backends MUST
            claim False.
        supports_token_verification: True when the backend can perform
            cryptographic token verification (JWT/OIDC/mTLS). Neither reference
            impl in this PR supports it — deferred to a future [auth] extra.
        produces_verified_principals: True when a backend returns Principals
            with is_verified=True for valid authenticated claims. LocalPrincipalBackend
            (always returns LOCAL_PRINCIPAL, is_verified=True) claims True.
            StaticClaimsPrincipalBackend claims True for valid claims;
            False for absent/malformed claims. A backend that NEVER produces
            is_verified=True MUST claim False.
    """

    backend_id: str  # required, no default — FIRST so TypeError fires on omission
    is_local_only: bool = False
    supports_token_verification: bool = False
    produces_verified_principals: bool = False

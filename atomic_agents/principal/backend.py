"""PrincipalBackend Protocol — the contract every principal implementation satisfies.

This is the twenty-first open Protocol in the protocol-pattern series (spec/48).
It provides identity derivation: mapping already-verified external claims (e.g.
OIDC sub, IAP user-id, pre-verified dict) into a typed Principal that agent.call()
can enforce conversation ownership with via the is_verified gate.

Protocol method surface:

  ONE operation ON the Protocol:
    derive_principal(verified_claims: Mapping) -> Principal
        — maps ALREADY-VERIFIED claims into a Principal stamped is_verified=True.
          Absent/malformed claims -> is_verified=False (fail-closed).
          NEVER raises for unverified/malformed claims (those return is_verified=False).
          NEVER performs crypto verification (honors spec/37 MUST 6).

  Plus the standard Protocol surface:
    capabilities()  — return PrincipalCapabilities
    backend_id      — stable identifier property

Identity model:
    The framework NEVER verifies tokens. Verification happens at the perimeter
    (IAP, mTLS termination, OIDC middleware). The PrincipalBackend maps the
    already-verified claims the perimeter produced into a Principal.

    Home-user shape: LocalPrincipalBackend ignores claims entirely and returns
    LOCAL_PRINCIPAL (is_verified=True) — zero config, zero crypto.
    Org shape: StaticClaimsPrincipalBackend maps a pre-verified claims dict ->
    Principal via a deterministic storage-key derivation (spec/48 MUST 7).

Storage-key derivation (spec/48 MUST 7):
    For a verified caller, Principal.identifier = the full 64-character lowercase
    hexadecimal SHA-256 digest of (provider/sub stripped of surrounding
    whitespace first, so a trailing space cannot split a namespace):
        sha256(f'{provider.strip()}\\x00{sub.strip()}'.encode('utf-8')).hexdigest()
    The NUL byte (\\x00) is the domain separator — it cannot appear in a valid
    provider string or OIDC sub claim, making the separator unambiguous and
    preventing prefix-collision attacks.

    This encoding is NORMATIVE — all implementations MUST produce the same
    identifier for the same (provider, sub) pair so:
    (a) old conversation files remain readable after a restart (deterministic),
    (b) cross-host deployments key on the same identifier (non-reassignable),
    (c) no PII is stored (one-way hash, no salt for portability).

WritePolicy: NOT part of this Protocol. PrincipalBackend performs identity
derivation only — no storage path, no write-path enforcement.

Import boundary (circular-import safety):
    This module imports ONLY from .types, ..exceptions, and stdlib. No imports
    from ..agent, .._llm, .._costs, ..logs, or modules that import those.

See docs/spec/48-principal-backend.md for the full normative contract.
"""

from __future__ import annotations

import logging
from typing import Mapping, Protocol, runtime_checkable

from .types import Principal, PrincipalCapabilities

_logger = logging.getLogger(__name__)


@runtime_checkable
class PrincipalBackend(Protocol):
    """Contract every principal backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The @runtime_checkable decorator enables
    isinstance(obj, PrincipalBackend) to perform a method-presence check.

    The backend is STATELESS at the Protocol level — it holds no agent_root.
    PrincipalBackend is a singleton-like derivation service, not a per-agent
    scoped backend. A process-level instance is sufficient.

    Side-effect-free construction (spec/48 MUST 4): __init__ MUST NOT perform
    I/O, and the reference backends take NO constructor arguments. The verified
    claims Mapping is a RUNTIME argument to derive_principal(), never a
    constructor argument — get_principal_backend() instantiates every registered
    backend via ``cls()`` with no args.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'local', 'static_claims'.

        MUST be a static lowercase string. MUST NOT change across process
        restarts or re-instantiation. Treat as a backwards-compatibility surface.
        """
        ...

    def derive_principal(self, verified_claims: Mapping) -> Principal:
        """Map ALREADY-VERIFIED claims into a Principal (spec/48 MUST 1).

        IMPORTANT SECURITY CONTRACT:
            - This method maps claims that the PERIMETER has ALREADY verified.
              It NEVER performs crypto verification (honors spec/37 MUST 6).
            - Absent/malformed claims MUST return is_verified=False (fail-closed).
            - MUST NOT raise for absent/malformed claims — those return is_verified=False.
            - PrincipalBackendError is reserved for genuine backend I/O failures
              (e.g. a database-backed backend with a broken connection). Neither
              LocalPrincipalBackend nor StaticClaimsPrincipalBackend raises it
              (both perform no I/O).

        Storage-key derivation (spec/48 MUST 7 + MUST 11):
            For verified claims with non-empty 'provider' AND non-empty 'sub',
            Principal.identifier MUST be (provider/sub stripped of surrounding
            whitespace first, so a trailing space cannot split a namespace):
                hashlib.sha256(
                    f'{provider.strip()}\\x00{sub.strip()}'.encode('utf-8')
                ).hexdigest()
            (full 64-char lowercase hexdigest, NUL-byte domain separator).
            The mapping MUST be STABLE (same pair → same identifier across
            restarts) and NON-REASSIGNABLE (distinct pairs never collide) —
            spec/48 MUST 11.

        LocalPrincipalBackend override:
            Ignores verified_claims entirely. Always returns LOCAL_PRINCIPAL
            (is_verified=True, identifier='local'). The is_local_only capability
            advertises this behavior.

        Args:
            verified_claims: A Mapping (e.g. dict) of already-verified identity
                claims. Typical keys: 'provider' (str, e.g. 'google', 'iap'),
                'sub' (str, e.g. an OIDC subject identifier). Callers MUST pass
                only pre-verified claims — the framework does NOT re-verify.

        Returns:
            Principal with is_verified=True when valid claims are present and
            non-empty, or is_verified=False when claims are absent/malformed.
            The return is ALWAYS a valid Principal — never None.

        Raises:
            PrincipalBackendError: ONLY on genuine backend I/O failure (e.g.
                a database-backed impl that cannot read the claims mapping table).
                NOT raised for absent/malformed claims (those return is_verified=False).
        """
        ...

    def capabilities(self) -> PrincipalCapabilities:
        """Backend capability declaration — see PrincipalCapabilities.

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.

        MUST: backend_id must be declared (no default). produces_verified_principals
        MUST match actual behavior: LocalPrincipalBackend MUST claim True (always
        returns is_verified=True Principal); a backend that NEVER produces
        is_verified=True MUST claim False.
        """
        ...

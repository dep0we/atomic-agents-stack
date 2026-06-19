"""LocalPrincipalBackend — mandatory home-user default implementation (spec/48).

The home-user shape: a single trusted local principal with zero config.
LocalPrincipalBackend ignores ALL input to derive_principal() and always returns
LOCAL_PRINCIPAL (identifier='local', is_verified=True).

This backend embodies the home-user throughline (CLAUDE.md):
    - Zero config: no keys, no registry entries, no env var needed.
    - Zero crypto: no token verification, no I/O, no external dependencies.
    - Zero overhead: __init__ is side-effect-free (no I/O), derive_principal()
      is O(1) pure function — just returns a module-level constant.
    - Backward-compatible: existing agents that set no principal_backend get
      this backend and see exactly today's behavior.

The is_local_only=True capability signals that this backend is NOT appropriate
for multi-user deployments. Org deployments that need per-caller isolation
should configure StaticClaimsPrincipalBackend or a future token-verifying backend.

Construction: side-effect-free (no filesystem I/O in __init__), matching the
FilesystemDedupLedger / FilesystemJournalBackend convention.

Import boundary: only imports from .types (which re-exports from conversation/types).
No imports from ..agent, .._llm, or any module that imports those.
"""

from __future__ import annotations

from typing import Mapping

from .types import LOCAL_PRINCIPAL, Principal, PrincipalCapabilities

_BACKEND_ID = "local"


class LocalPrincipalBackend:
    """Reference implementation: home-user single-principal backend.

    Ignores ALL input to derive_principal() and always returns LOCAL_PRINCIPAL.
    is_local_only=True: NOT for multi-user deployments.

    Construction is side-effect-free (no I/O in __init__).
    """

    @property
    def backend_id(self) -> str:
        """Stable backend identifier. Always 'local'."""
        return _BACKEND_ID

    def derive_principal(self, verified_claims: Mapping) -> Principal:
        """Ignore verified_claims and return LOCAL_PRINCIPAL.

        LocalPrincipalBackend is the home-user default. It ignores ALL input
        because the home-user IS the operator — there is no separate verification
        step and no multi-user isolation needed.

        Args:
            verified_claims: Ignored entirely. May be any Mapping (including {}).

        Returns:
            LOCAL_PRINCIPAL (identifier='local', is_verified=True, derivation_source='local').
        """
        return LOCAL_PRINCIPAL

    def capabilities(self) -> PrincipalCapabilities:
        """Return capabilities for the local backend.

        is_local_only=True: safe ONLY for single-user local deployments.
        produces_verified_principals=True: always returns is_verified=True Principal.
        supports_token_verification=False: no crypto in this impl.
        """
        return PrincipalCapabilities(
            backend_id=_BACKEND_ID,
            is_local_only=True,
            supports_token_verification=False,
            produces_verified_principals=True,
        )

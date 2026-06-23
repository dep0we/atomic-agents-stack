"""StaticClaimsPrincipalBackend — pre-verified claims dict reference implementation (spec/48).

This backend maps a pre-verified claims dict into a Principal via the spec/48
storage-key derivation. It does NOT perform any crypto verification — it trusts
that the caller has ALREADY verified the claims at the perimeter (IAP, OIDC
middleware, mTLS termination).

Storage-key derivation (spec/48 MUST 7 — NORMATIVE):
    Principal.identifier = sha256(f'{provider.strip(_ASCII_WS)}\\x00{sub.strip(_ASCII_WS)}'.encode('utf-8')).hexdigest()

    Where:
    - provider: the 'provider' key from verified_claims (e.g. 'google', 'iap')
    - sub: the 'sub' key from verified_claims (OIDC subject identifier)
    - \\x00: NUL byte domain separator, preventing prefix-collision attacks
      where ('google', 'user') and ('goo', 'gleuser') would produce the same
      hash under naive f'{provider}{sub}' concatenation.
    - sha256(...).hexdigest(): full 64-character lowercase hexdigest — always
      passes _validate_conversation_component() in FilesystemConversationBackend.

Fail-closed contract (spec/48 MUST 1):
    Absent or malformed claims MUST return is_verified=False. Specifically:
    - 'provider' absent, None, or empty string after strip() -> is_verified=False
    - 'sub' absent, None, or empty string after strip() -> is_verified=False
    - verified_claims is an empty Mapping -> is_verified=False
    - Any non-string value for 'provider' or 'sub' -> is_verified=False
    These cases return Principal(identifier='anonymous', derivation_source='static_claims',
    is_verified=False) — which will trigger the HARD-REFUSE gate in agent.call()
    if conversation_id is also set.

    NEVER raises for absent/malformed claims. PrincipalBackendError is reserved
    for genuine backend I/O failures — neither this impl nor LocalPrincipalBackend
    raises it (both perform no I/O).

Side-effect-free construction (spec/48 MUST 4):
    __init__ takes NO filesystem paths and performs NO I/O. The claims Mapping
    is a runtime input to derive_principal(), not a constructor argument.
    (Unlike corpus/memory backends that load config at construction time.)

Import boundary: only imports from .types, stdlib. No imports from ..agent or
modules that transitively import those.
"""

from __future__ import annotations

import hashlib
from typing import Mapping

from .types import Principal, PrincipalCapabilities

_BACKEND_ID = "static_claims"

# ASCII whitespace only — deliberately NOT str.strip()'s Unicode set. str.strip()
# also strips NBSP (\xa0), the ideographic space (　), etc., which would make
# distinct perimeter-supplied claims collide on the storage key (e.g. '\xa0user'
# and 'user' -> same identifier), violating the spec/48 MUST 11 non-reassignability
# invariant the isolation guarantee rests on. ASCII-only matches spec/48 MUST 7's
# documented "surrounding ASCII whitespace" intent (normalize a stray trailing
# space; never silently merge two distinct Unicode subjects).
_ASCII_WS = " \t\n\r\f\v"

# Returned when claims are absent or malformed (fail-closed default).
# identifier='anonymous' distinguishes this from LOCAL_PRINCIPAL (identifier='local').
# is_verified=False — this triggers the HARD-REFUSE gate if conversation_id is set.
_UNVERIFIED_PRINCIPAL = Principal(
    identifier="anonymous",
    derivation_source="static_claims",
    is_verified=False,
)


def _derive_storage_key(provider: str, sub: str) -> str:
    """Derive the stable storage-key identifier from (provider, sub).

    NORMATIVE encoding (spec/48 MUST 7):
        sha256(f'{provider.strip(_ASCII_WS)}\\x00{sub.strip(_ASCII_WS)}'.encode('utf-8')).hexdigest()

    Inputs are stripped of surrounding ASCII whitespace HERE (so ``_derive_storage_key``
    and ``derive_principal`` produce byte-identical identifiers for the same logical
    claims — an accidental trailing space cannot split a principal's namespace,
    spec/48 MUST 11). ``derive_principal`` also strips before calling; the
    double-strip is idempotent and harmless.

    The NUL byte (\\x00) is the domain separator. It cannot appear in a valid
    provider string or OIDC sub claim (both are printable ASCII/UTF-8 strings),
    making the separator unambiguous and preventing prefix-collision attacks.

    Example (from spec/48 MUST 7):
        derive_principal({'provider': 'google', 'sub': '1234567890'}) MUST produce:
        sha256(b'google\\x001234567890').hexdigest()

    Always returns a 64-character lowercase hexadecimal string — safe as a
    principal.identifier in FilesystemConversationBackend (which validates that
    identifiers are bare filename components; a 64-char hex string always passes).

    Args:
        provider: provider string (e.g. 'google', 'iap', 'github'); surrounding
            ASCII whitespace (_ASCII_WS) is stripped before hashing — NOT Unicode
            whitespace, so distinct subjects never collide (spec/48 MUST 11).
        sub: OIDC subject identifier; surrounding ASCII whitespace is stripped
            before hashing.

    Returns:
        Full 64-character lowercase hexdigest of
        sha256(b'{provider.strip(_ASCII_WS)}\\x00{sub.strip(_ASCII_WS)}').
    """
    hash_input = f"{provider.strip(_ASCII_WS)}\x00{sub.strip(_ASCII_WS)}".encode(
        "utf-8"
    )
    return hashlib.sha256(hash_input).hexdigest()


class StaticClaimsPrincipalBackend:
    """Reference implementation: pre-verified claims dict -> Principal.

    Maps a pre-verified claims dict to a Principal via the spec/48 storage-key
    derivation. Does NOT perform crypto verification — callers MUST supply
    only already-verified claims.

    Construction is side-effect-free (no I/O in __init__).
    """

    @property
    def backend_id(self) -> str:
        """Stable backend identifier. Always 'static_claims'."""
        return _BACKEND_ID

    def derive_principal(self, verified_claims: Mapping) -> Principal:
        """Map pre-verified claims to a Principal (spec/48 MUST 1).

        Fail-closed: absent or malformed claims return is_verified=False.
        NEVER raises for absent/malformed claims.

        Required claims:
            'provider': non-empty string (e.g. 'google', 'iap', 'github')
            'sub': non-empty string (OIDC subject identifier)

        If either is absent, None, empty after strip(), or a non-string type,
        returns _UNVERIFIED_PRINCIPAL (is_verified=False).

        Storage-key derivation (spec/48 MUST 7 + MUST 11):
            provider/sub are stripped of surrounding ASCII whitespace ONLY
            (`_ASCII_WS`, NOT str.strip()'s Unicode set — so a leading Unicode
            space cannot collapse two distinct subjects onto one identifier,
            MUST 11; and a trailing ASCII space cannot split a namespace), then:
            identifier = sha256(
                f'{provider.strip(_ASCII_WS)}\\x00{sub.strip(_ASCII_WS)}'.encode('utf-8')
            ).hexdigest()
            (64-char lowercase hexdigest, NUL-byte domain separator). Stable +
            non-reassignable (MUST 11): the same (provider, sub) always yields the
            same identifier; distinct pairs never collide.

        Args:
            verified_claims: Mapping with pre-verified claims. Typical keys:
                'provider' (str): identity provider name
                'sub' (str): OIDC subject identifier

        Returns:
            Principal(identifier=<sha256_hex>, derivation_source='static_claims',
                is_verified=True) on valid claims, OR
            Principal(identifier='anonymous', derivation_source='static_claims',
                is_verified=False) on absent/malformed claims.
        """
        try:
            provider_raw = verified_claims.get("provider")
            sub_raw = verified_claims.get("sub")
        except (AttributeError, TypeError):
            # verified_claims doesn't support .get() or is not a Mapping
            return _UNVERIFIED_PRINCIPAL

        # Validate provider
        if not isinstance(provider_raw, str):
            return _UNVERIFIED_PRINCIPAL
        provider = provider_raw.strip(_ASCII_WS)
        if not provider:
            return _UNVERIFIED_PRINCIPAL

        # Validate sub
        if not isinstance(sub_raw, str):
            return _UNVERIFIED_PRINCIPAL
        sub = sub_raw.strip(_ASCII_WS)
        if not sub:
            return _UNVERIFIED_PRINCIPAL

        # Both valid — derive the storage key.
        identifier = _derive_storage_key(provider, sub)
        return Principal(
            identifier=identifier,
            derivation_source="static_claims",
            is_verified=True,
        )

    def capabilities(self) -> PrincipalCapabilities:
        """Return capabilities for the static_claims backend.

        is_local_only=False: multi-user safe (maps pre-verified claims to
            isolated principal identifiers).
        produces_verified_principals=True: returns is_verified=True for
            valid (non-empty provider + sub) claims.
        supports_token_verification=False: no crypto — callers MUST supply
            pre-verified claims.
        """
        return PrincipalCapabilities(
            backend_id=_BACKEND_ID,
            is_local_only=False,
            supports_token_verification=False,
            produces_verified_principals=True,
        )

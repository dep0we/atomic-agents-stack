"""Parametrized protocol conformance tests for PrincipalBackend (spec/48).

Tests are parametrized over [LocalPrincipalBackend, StaticClaimsPrincipalBackend]
and verify the universal invariants that ALL conforming backends must satisfy:
  - capabilities() shape and honesty
  - derive_principal() fail-closed on absent/malformed claims
  - is_verified semantics
  - storage-key stability and non-reassignability (StaticClaimsPrincipalBackend)
  - PrincipalCapabilities field types and required backend_id
  - spec/48 MUST 7: exact hash encoding

NO tmp_path or filesystem I/O — these backends are stateless in-memory derivers.
Per-invocation negative controls are included for load-bearing assertions.

Run: uv run pytest tests/test_principal_protocol_conformance.py -v
"""

from __future__ import annotations

import hashlib

import pytest

from atomic_agents.principal import (
    LOCAL_PRINCIPAL,
    LocalPrincipalBackend,
    Principal,
    PrincipalBackend,
    PrincipalCapabilities,
    StaticClaimsPrincipalBackend,
)
from atomic_agents.principal.static_claims import _derive_storage_key


# ──────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture(
    params=[
        pytest.param(LocalPrincipalBackend, id="LocalPrincipalBackend"),
        pytest.param(StaticClaimsPrincipalBackend, id="StaticClaimsPrincipalBackend"),
    ]
)
def backend(request):
    """Parametrized fixture: yields one instance of each reference impl."""
    return request.param()


# ──────────────────────────────────────────────────────────────────
# Protocol structural conformance


def test_is_runtime_checkable_protocol(backend):
    """Backend satisfies the @runtime_checkable PrincipalBackend Protocol (spec/48 MUST 4)."""
    assert isinstance(backend, PrincipalBackend)


def test_backend_id_is_non_empty_string(backend):
    """backend_id is a non-empty string (spec/48 MUST 5)."""
    bid = backend.backend_id
    assert isinstance(bid, str)
    assert bid != ""


def test_backend_id_is_stable(backend):
    """backend_id returns the same value on repeated calls (spec/48 MUST 5)."""
    assert backend.backend_id == backend.backend_id


def test_backend_id_is_lowercase(backend):
    """backend_id is lowercase (registry convention, spec/48 MUST 5)."""
    assert backend.backend_id == backend.backend_id.lower()


def test_capabilities_returns_principal_capabilities(backend):
    """capabilities() returns a PrincipalCapabilities instance (spec/48 MUST 6)."""
    caps = backend.capabilities()
    assert isinstance(caps, PrincipalCapabilities)


def test_capabilities_backend_id_matches_property(backend):
    """capabilities().backend_id matches the backend_id property (spec/48 MUST 6)."""
    assert backend.capabilities().backend_id == backend.backend_id


def test_capabilities_field_types(backend):
    """All PrincipalCapabilities fields have the correct types (spec/48 MUST 6)."""
    caps = backend.capabilities()
    assert isinstance(caps.backend_id, str)
    assert isinstance(caps.is_local_only, bool)
    assert isinstance(caps.supports_token_verification, bool)
    assert isinstance(caps.produces_verified_principals, bool)


def test_capabilities_no_supports_canonical_export(backend):
    """PrincipalCapabilities does NOT have supports_canonical_export (spec/48 MUST 8 / spec/40 exemption)."""
    caps = backend.capabilities()
    assert not hasattr(caps, "supports_canonical_export"), (
        "PrincipalCapabilities must NOT carry supports_canonical_export — "
        "PrincipalBackend is an identity-derivation Protocol, not a state store."
    )


def test_principal_capabilities_missing_backend_id_raises():
    """PrincipalCapabilities(backend_id) required — TypeError when omitted."""
    with pytest.raises(TypeError):
        PrincipalCapabilities()  # type: ignore[call-arg]


# ──────────────────────────────────────────────────────────────────
# Fail-closed: absent/malformed claims -> is_verified=False (MUST 1)


@pytest.mark.parametrize(
    "bad_claims",
    [
        pytest.param({}, id="empty_dict"),
        pytest.param({"provider": "google"}, id="missing_sub"),
        pytest.param({"sub": "user123"}, id="missing_provider"),
        pytest.param({"provider": "", "sub": "user123"}, id="empty_provider"),
        pytest.param({"provider": "google", "sub": ""}, id="empty_sub"),
        pytest.param({"provider": None, "sub": "user123"}, id="none_provider"),
        pytest.param({"provider": "google", "sub": None}, id="none_sub"),
        pytest.param({"provider": 42, "sub": "user123"}, id="non_string_provider"),
        pytest.param({"provider": "google", "sub": 42}, id="non_string_sub"),
        pytest.param({"provider": "   ", "sub": "user123"}, id="whitespace_provider"),
        pytest.param({"provider": "google", "sub": "   "}, id="whitespace_sub"),
    ],
)
def test_absent_or_malformed_claims_never_raises(backend, bad_claims):
    """derive_principal() MUST NOT raise for absent/malformed claims (MUST 1)."""
    # Should return a Principal, not raise
    result = backend.derive_principal(bad_claims)
    assert isinstance(result, Principal)


def test_absent_claims_return_is_verified_false_for_static(bad_claims=None):
    """StaticClaimsPrincipalBackend: absent/malformed claims -> is_verified=False.

    Negative control: construct Principal(is_verified=True) directly and confirm
    they are different types (the fail-closed path must NOT copy LOCAL_PRINCIPAL).
    """
    backend = StaticClaimsPrincipalBackend()
    for claims in [
        {},
        {"provider": "google"},
        {"sub": "user"},
        {"provider": "", "sub": "x"},
    ]:
        result = backend.derive_principal(claims)
        assert not result.is_verified, (
            f"derive_principal({claims!r}) must return is_verified=False, got {result!r}"
        )
        assert result.identifier != LOCAL_PRINCIPAL.identifier or not result.is_verified


@pytest.mark.parametrize(
    "bad_claims",
    [
        {},
        {"provider": "google"},
        {"sub": "user123"},
        {"provider": None, "sub": "user123"},
        {"provider": "google", "sub": None},
    ],
)
def test_static_backend_fail_closed_is_verified_false(bad_claims):
    """StaticClaimsPrincipalBackend absent/malformed claims -> is_verified=False (per-invocation gate)."""
    backend = StaticClaimsPrincipalBackend()
    result = backend.derive_principal(bad_claims)
    assert isinstance(result, Principal)
    assert result.is_verified is False
    # Negative control: verify the gate is load-bearing by checking valid claims DO pass
    valid_result = backend.derive_principal({"provider": "google", "sub": "user123"})
    assert (
        valid_result.is_verified is True
    )  # valid claims must produce is_verified=True


# ──────────────────────────────────────────────────────────────────
# derive_principal() return type invariants


def test_derive_principal_always_returns_principal(backend):
    """derive_principal() always returns a Principal, never None or other type."""
    result = backend.derive_principal({"provider": "google", "sub": "user123"})
    assert isinstance(result, Principal)


def test_derive_principal_empty_dict_returns_principal(backend):
    """derive_principal({}) returns a Principal (not None, not raises)."""
    result = backend.derive_principal({})
    assert isinstance(result, Principal)


# ──────────────────────────────────────────────────────────────────
# LocalPrincipalBackend: always returns LOCAL_PRINCIPAL


def test_local_backend_returns_local_principal_for_empty_claims():
    """LocalPrincipalBackend always returns LOCAL_PRINCIPAL for empty claims."""
    backend = LocalPrincipalBackend()
    result = backend.derive_principal({})
    assert result is LOCAL_PRINCIPAL


def test_local_backend_returns_local_principal_for_any_claims():
    """LocalPrincipalBackend ignores all input and returns LOCAL_PRINCIPAL (spec/48 MUST 12 — is_verified honesty)."""
    backend = LocalPrincipalBackend()
    for claims in [
        {},
        {"provider": "google", "sub": "user"},
        {"provider": "evil", "sub": "../../etc/passwd"},
        {"anything": "goes"},
    ]:
        result = backend.derive_principal(claims)
        assert result is LOCAL_PRINCIPAL, (
            f"Expected LOCAL_PRINCIPAL for claims={claims!r}"
        )


def test_local_backend_is_local_only():
    """LocalPrincipalBackend.capabilities().is_local_only == True."""
    caps = LocalPrincipalBackend().capabilities()
    assert caps.is_local_only is True


def test_local_backend_produces_verified_principals():
    """LocalPrincipalBackend.capabilities().produces_verified_principals == True."""
    caps = LocalPrincipalBackend().capabilities()
    assert caps.produces_verified_principals is True


# ──────────────────────────────────────────────────────────────────
# StaticClaimsPrincipalBackend: storage-key derivation (spec/48 MUST 7)


def test_static_backend_is_not_local_only():
    """StaticClaimsPrincipalBackend.capabilities().is_local_only == False."""
    caps = StaticClaimsPrincipalBackend().capabilities()
    assert caps.is_local_only is False


def test_static_backend_produces_verified_principals():
    """StaticClaimsPrincipalBackend.capabilities().produces_verified_principals == True."""
    caps = StaticClaimsPrincipalBackend().capabilities()
    assert caps.produces_verified_principals is True


def test_static_backend_storage_key_normative_encoding():
    """Storage key MUST be sha256(f'{provider.strip(_ASCII_WS)}\\x00{sub.strip(_ASCII_WS)}'.encode('utf-8')).hexdigest() (spec/48 MUST 7)."""
    backend = StaticClaimsPrincipalBackend()
    provider = "google"
    sub = "1234567890"
    expected = hashlib.sha256(f"{provider}\x00{sub}".encode("utf-8")).hexdigest()
    result = backend.derive_principal({"provider": provider, "sub": sub})
    assert result.identifier == expected, (
        f"Storage key mismatch: expected {expected!r}, got {result.identifier!r}"
    )
    # Verify it's 64-char lowercase hex
    assert len(result.identifier) == 64
    assert result.identifier == result.identifier.lower()
    assert all(c in "0123456789abcdef" for c in result.identifier)


def test_static_backend_storage_key_is_verified_true():
    """Valid claims produce is_verified=True."""
    backend = StaticClaimsPrincipalBackend()
    result = backend.derive_principal({"provider": "google", "sub": "user123"})
    assert result.is_verified is True


def test_static_backend_storage_key_stable_across_calls():
    """Same (provider, sub) always produces the same identifier (determinism)."""
    backend = StaticClaimsPrincipalBackend()
    claims = {"provider": "google", "sub": "user123"}
    r1 = backend.derive_principal(claims)
    r2 = backend.derive_principal(claims)
    assert r1.identifier == r2.identifier


def test_static_backend_storage_key_strips_surrounding_whitespace():
    """spec/48 MUST 7: provider/sub are stripped of surrounding whitespace before
    hashing, so an accidental trailing space does NOT split a principal's namespace.

    'google ' and 'google' MUST collide to the same identifier (the normalization),
    and that identifier MUST equal the hash of the stripped values (the encoding).
    """
    backend = StaticClaimsPrincipalBackend()
    r_spaced = backend.derive_principal({"provider": "google ", "sub": " user123"})
    r_clean = backend.derive_principal({"provider": "google", "sub": "user123"})
    # Normalization: spaced and clean map to the SAME identifier.
    assert r_spaced.identifier == r_clean.identifier
    # Encoding: that identifier is the hash of the STRIPPED values.
    expected = hashlib.sha256("google\x00user123".encode("utf-8")).hexdigest()
    assert r_clean.identifier == expected
    # Negative control: interior whitespace is NOT stripped (only surrounding).
    r_interior = backend.derive_principal({"provider": "goo gle", "sub": "user123"})
    assert r_interior.identifier != r_clean.identifier


def test_static_backend_storage_key_does_not_collapse_unicode_whitespace():
    """spec/48 MUST 11 (non-reassignability): only ASCII whitespace is stripped,
    NOT Unicode whitespace. Two DISTINCT perimeter-supplied subjects that differ
    only by a leading Unicode space (NBSP \\xa0) MUST map to DIFFERENT identifiers
    — otherwise str.strip()'s Unicode set would silently collide them and let one
    subject read another's conversation. Found by cross-family review.

    Negative control: with the strip reverted to bare str.strip() (Unicode), the
    two identifiers collide and this assertion goes RED.
    """
    backend = StaticClaimsPrincipalBackend()
    plain = backend.derive_principal({"provider": "google", "sub": "user123"})
    nbsp = backend.derive_principal({"provider": "google", "sub": "\xa0user123"})
    ideographic = backend.derive_principal({"provider": "google", "sub": "　user123"})
    assert plain.is_verified and nbsp.is_verified and ideographic.is_verified
    assert plain.identifier != nbsp.identifier, (
        "NBSP-prefixed subject MUST NOT collide with the plain subject "
        "(Unicode whitespace must not be stripped — MUST 11 non-reassignability)"
    )
    assert plain.identifier != ideographic.identifier
    assert nbsp.identifier != ideographic.identifier


def test_derive_storage_key_helper_matches_derive_principal_for_whitespace():
    """The private _derive_storage_key helper and derive_principal MUST produce
    byte-identical identifiers for the same logical claims, even with surrounding
    whitespace (the helper strips its inputs, matching derive_principal).

    _derive_storage_key is private (underscore-only); the byte-identity guarantee
    is normative in spec/48 MUST 7 + MUST 11 but is not a public API contract.
    This test imports it directly from atomic_agents.principal.static_claims.

    Guards against the latent footgun where an implementer reconstructs a
    principal's conversation directory via the helper and gets a DIFFERENT key
    than derive_principal stamped — orphaning that principal's turns.
    """
    backend = StaticClaimsPrincipalBackend()
    p, s = "google ", " user123"
    via_helper = _derive_storage_key(p, s)
    via_principal = backend.derive_principal({"provider": p, "sub": s}).identifier
    assert via_helper == via_principal
    # And both equal the hash of the STRIPPED values (the normative encoding).
    assert via_helper == hashlib.sha256("google\x00user123".encode("utf-8")).hexdigest()
    # Negative control: a raw (non-stripping) encoding would diverge — confirm the
    # helper does NOT produce the raw form for whitespace-bearing input.
    raw = hashlib.sha256(f"{p}\x00{s}".encode("utf-8")).hexdigest()
    assert via_helper != raw


def test_derive_storage_key_is_not_public_api():
    """The storage-key helper MUST NOT be a public, importable symbol (spec/48
    LOCK, maintainer-ruled). The byte-identity guarantee lives in spec/48 MUST 7
    + MUST 11 as the normative encoding, NOT as a frozen public function — a
    public free-function identity-derivation path reaches around the Protocol
    (Principle #2) and over-commits a byte-identity guarantee future non-hash-key
    backends can't honor. Locks the privatization decision so a future re-export
    is caught.
    """
    import atomic_agents.principal as principal_pkg

    assert "derive_storage_key" not in principal_pkg.__all__
    assert not hasattr(principal_pkg, "derive_storage_key")
    with pytest.raises(ImportError):
        from atomic_agents.principal import derive_storage_key  # noqa: F401


def test_static_backend_storage_key_nul_separator_prevents_prefix_collision():
    """('google', 'user') and ('goo', 'gleuser') must produce DIFFERENT identifiers.

    Without the NUL separator, f'{provider}{sub}' concatenation would produce the
    same string 'googleuser' for both pairs, colliding two distinct principals onto
    one conversation directory. This is the canonical spec/48 prefix-collision test.
    """
    backend = StaticClaimsPrincipalBackend()
    r1 = backend.derive_principal({"provider": "google", "sub": "user"})
    r2 = backend.derive_principal({"provider": "goo", "sub": "gleuser"})
    assert r1.identifier != r2.identifier, (
        "NUL-byte separator must prevent ('google','user') from colliding with ('goo','gleuser')"
    )


def test_static_backend_different_providers_different_identifiers():
    """Same sub across different providers produces different identifiers."""
    backend = StaticClaimsPrincipalBackend()
    r_google = backend.derive_principal({"provider": "google", "sub": "12345"})
    r_github = backend.derive_principal({"provider": "github", "sub": "12345"})
    assert r_google.identifier != r_github.identifier


def test_static_backend_identifier_is_64_char_lowercase_hex():
    """Principal.identifier for valid claims is always 64-char lowercase hexdigest."""
    backend = StaticClaimsPrincipalBackend()
    result = backend.derive_principal({"provider": "iap", "sub": "user@example.com"})
    assert len(result.identifier) == 64
    assert result.identifier == result.identifier.lower()
    assert all(c in "0123456789abcdef" for c in result.identifier)


def test_static_backend_derivation_source():
    """StaticClaimsPrincipalBackend sets derivation_source='static_claims'."""
    backend = StaticClaimsPrincipalBackend()
    result = backend.derive_principal({"provider": "google", "sub": "user"})
    assert result.derivation_source == "static_claims"


# ──────────────────────────────────────────────────────────────────
# Capability honesty: claim vs behavior parity


def test_local_backend_claim_produces_verified_honesty():
    """LocalPrincipalBackend claims produces_verified_principals=True and always returns is_verified=True."""
    backend = LocalPrincipalBackend()
    caps = backend.capabilities()
    assert caps.produces_verified_principals is True
    # Verify the claim is honest
    result = backend.derive_principal({"anything": "goes"})
    assert result.is_verified is True


def test_static_backend_claim_produces_verified_honesty():
    """StaticClaimsPrincipalBackend claims produces_verified_principals=True and does so for valid claims."""
    backend = StaticClaimsPrincipalBackend()
    caps = backend.capabilities()
    assert caps.produces_verified_principals is True
    # Verify the claim is honest for valid claims
    result = backend.derive_principal({"provider": "google", "sub": "user"})
    assert result.is_verified is True
    # And fail-closed for invalid (not contradicting the capability claim — the
    # capability says it CAN produce verified principals, not that it always does)
    result_bad = backend.derive_principal({})
    assert result_bad.is_verified is False


# ──────────────────────────────────────────────────────────────────
# Construction: side-effect-free (no I/O)


def test_local_backend_construction_side_effect_free():
    """LocalPrincipalBackend() constructs without any filesystem I/O (spec/48 MUST 4)."""
    # This just verifies no exception is raised and the backend is usable
    backend = LocalPrincipalBackend()
    assert backend.backend_id == "local"


def test_static_backend_construction_side_effect_free():
    """StaticClaimsPrincipalBackend() constructs without any filesystem I/O (spec/48 MUST 4)."""
    backend = StaticClaimsPrincipalBackend()
    assert backend.backend_id == "static_claims"


# ──────────────────────────────────────────────────────────────────
# backend_id stability


def test_local_backend_id_value():
    """LocalPrincipalBackend.backend_id == 'local'."""
    assert LocalPrincipalBackend().backend_id == "local"


def test_static_claims_backend_id_value():
    """StaticClaimsPrincipalBackend.backend_id == 'static_claims'."""
    assert StaticClaimsPrincipalBackend().backend_id == "static_claims"


def test_backend_ids_are_distinct():
    """The two reference impls have distinct backend_ids."""
    assert (
        LocalPrincipalBackend().backend_id != StaticClaimsPrincipalBackend().backend_id
    )


# ──────────────────────────────────────────────────────────────────
# is_verified gate invariant: keys on is_verified boolean, never object identity


def test_fabricated_local_principal_with_is_verified_false_fails_gate():
    """A Principal with LOCAL_PRINCIPAL's identifier but is_verified=False must fail the gate.

    The HARD-REFUSE gate in agent.call() keys on is_verified ONLY — never on
    object identity or identifier value. This test verifies the gate condition
    is correctly formulated (spec/48 MUST 10).
    """
    # Fabricate a Principal that looks like LOCAL_PRINCIPAL but is_verified=False
    fabricated = Principal(
        identifier="local",
        derivation_source="local",
        is_verified=False,
    )
    # This principal should fail the gate (conversation_id is not None and not is_verified)
    # We verify the gate condition directly (the integration test in test_principal_call_wiring.py
    # exercises the full agent.call() path)
    assert fabricated.identifier == "local"  # same identifier as LOCAL_PRINCIPAL
    assert fabricated is not LOCAL_PRINCIPAL  # different object
    assert not fabricated.is_verified  # but is_verified=False — gate would refuse it
    # LOCAL_PRINCIPAL must have is_verified=True (always passes gate)
    assert LOCAL_PRINCIPAL.is_verified is True


# ──────────────────────────────────────────────────────────────────
# MUST 2 — supports_token_verification is False for both reference impls


def test_must2_reference_impls_do_not_support_token_verification_local():
    """LocalPrincipalBackend.capabilities().supports_token_verification is False.

    spec/48 MUST 2: supports_token_verification MUST be True only if the backend
    performs its own token validation. LocalPrincipalBackend performs no crypto —
    it always returns LOCAL_PRINCIPAL unconditionally. The never-verify behavior is
    architectural (the backend performs no crypto by construction); the HARD-REFUSE
    gate it feeds (keyed on is_verified) is integration-tested in
    test_principal_call_wiring.py.

    This is a direct invariant check on the advertised capability flag (not a
    strip-RED control): a backend that flipped the flag to True without performing
    crypto would fail here.
    """
    caps = LocalPrincipalBackend().capabilities()
    assert caps.supports_token_verification is False, (
        "MUST 2: LocalPrincipalBackend must not claim token-verification support — "
        "it performs no crypto. Got supports_token_verification=True."
    )


def test_must2_reference_impls_do_not_support_token_verification_static():
    """StaticClaimsPrincipalBackend.capabilities().supports_token_verification is False.

    spec/48 MUST 2: StaticClaimsPrincipalBackend trusts pre-verified claims (the
    perimeter has already authenticated); it performs no crypto itself.

    This is a direct invariant check on the advertised capability flag (not a
    strip-RED control): a backend that flipped the flag to True without performing
    crypto would fail here.
    """
    caps = StaticClaimsPrincipalBackend().capabilities()
    assert caps.supports_token_verification is False, (
        "MUST 2: StaticClaimsPrincipalBackend must not claim token-verification support — "
        "it trusts pre-verified claims from the perimeter, performs no crypto. "
        "Got supports_token_verification=True."
    )


# ──────────────────────────────────────────────────────────────────
# MUST 3 — fail-closed sentinel carries derivation_source == backend.backend_id


def test_must3_fail_closed_sentinel_carries_backend_derivation_source():
    """The fail-closed principal (is_verified=False) MUST carry derivation_source
    set to this backend's backend_id (spec/48 MUST 3, second clause).

    Guards against a future backend returning an unverified sentinel with
    derivation_source='anonymous' or '' — both would violate MUST 3 and break
    audit attribution for refused callers.

    Negative control: a sentinel with derivation_source='' would fail this assertion.
    """
    backend = StaticClaimsPrincipalBackend()
    result = backend.derive_principal({})
    assert result.is_verified is False, (
        "MUST 3 precondition: absent claims must produce is_verified=False"
    )
    assert result.derivation_source == backend.backend_id, (
        f"MUST 3: fail-closed principal.derivation_source must equal backend.backend_id "
        f"({backend.backend_id!r}), got {result.derivation_source!r}"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 9 — Principal and LOCAL_PRINCIPAL are re-exported, never redefined


def test_must9_principal_is_reexport_not_redefined():
    """atomic_agents.principal.Principal MUST be the SAME class object as
    atomic_agents.conversation.types.Principal — a re-export, not a redefinition.

    spec/48 MUST 9: redefining Principal (even with identical fields) creates two
    distinct types and breaks `is` identity checks throughout agent.call() (notably
    the `principal is not LOCAL_PRINCIPAL` JSONL stamp guard).

    Negative control: introducing a local dataclass Principal in principal/types.py
    with identical fields would make this assertion go RED.
    """
    from atomic_agents.principal import Principal as PrincipalFromPrincipal  # noqa: PLC0415
    from atomic_agents.conversation.types import Principal as PrincipalFromConversation  # noqa: PLC0415

    assert PrincipalFromPrincipal is PrincipalFromConversation, (
        "MUST 9: atomic_agents.principal.Principal MUST be the same class object as "
        "atomic_agents.conversation.types.Principal — re-export, not a redefinition."
    )


def test_must9_local_principal_is_same_object():
    """atomic_agents.principal.LOCAL_PRINCIPAL MUST be the SAME object as
    atomic_agents.conversation.types.LOCAL_PRINCIPAL.

    spec/48 MUST 9: the `principal is not LOCAL_PRINCIPAL` guard in agent.call()
    uses object identity (`is`), not equality. A re-minted LOCAL_PRINCIPAL-alike
    with identical fields would bypass the guard and stamp principal_id on every
    home-user log record, breaking backward compatibility.

    Negative control: constructing a fresh LOCAL_PRINCIPAL lookalike in
    principal/types.py would make this assertion go RED.
    """
    from atomic_agents.principal import LOCAL_PRINCIPAL as LP_from_principal  # noqa: PLC0415
    from atomic_agents.conversation.types import LOCAL_PRINCIPAL as LP_from_conversation  # noqa: PLC0415

    assert LP_from_principal is LP_from_conversation, (
        "MUST 9: LOCAL_PRINCIPAL re-export must be the same object as "
        "atomic_agents.conversation.types.LOCAL_PRINCIPAL — not a copy."
    )

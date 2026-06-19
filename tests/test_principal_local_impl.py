"""LocalPrincipalBackend-specific tests (spec/48).

Tests the home-user single-principal reference implementation:
  - Always returns LOCAL_PRINCIPAL regardless of input
  - is_local_only=True capability
  - side-effect-free construction
  - registry and get_default_principal_backend() behavior

NO tmp_path — LocalPrincipalBackend has no filesystem state.

Run: uv run pytest tests/test_principal_local_impl.py -v
"""

from __future__ import annotations

import os

import pytest

from atomic_agents.exceptions import BackendNotRegistered
from atomic_agents.principal import (
    LOCAL_PRINCIPAL,
    LocalPrincipalBackend,
    StaticClaimsPrincipalBackend,
    get_default_principal_backend,
    get_principal_backend,
    list_principal_backends,
    register_principal_backend,
    unregister_principal_backend,
)
from atomic_agents.principal.types import Principal


# ──────────────────────────────────────────────────────────────────
# LocalPrincipalBackend core behavior


def test_local_backend_ignores_all_claims():
    """LocalPrincipalBackend ignores ALL claims and returns LOCAL_PRINCIPAL."""
    backend = LocalPrincipalBackend()
    test_cases = [
        {},
        {"provider": "google", "sub": "user123"},
        {"provider": "evil", "sub": "../../etc/passwd"},
        {"anything_at_all": "whatever"},
        {"provider": None, "sub": None},
    ]
    for claims in test_cases:
        result = backend.derive_principal(claims)
        assert result is LOCAL_PRINCIPAL, (
            f"Expected LOCAL_PRINCIPAL for claims={claims!r}, got {result!r}"
        )


def test_local_backend_result_is_verified_true():
    """LocalPrincipalBackend always returns is_verified=True."""
    backend = LocalPrincipalBackend()
    assert LOCAL_PRINCIPAL.is_verified is True
    result = backend.derive_principal({})
    assert result.is_verified is True


def test_local_backend_result_identifier_is_local():
    """LocalPrincipalBackend result has identifier='local'."""
    backend = LocalPrincipalBackend()
    result = backend.derive_principal({})
    assert result.identifier == "local"


def test_local_backend_result_derivation_source():
    """LocalPrincipalBackend result has derivation_source='local'."""
    backend = LocalPrincipalBackend()
    result = backend.derive_principal({})
    assert result.derivation_source == "local"


def test_local_backend_result_is_the_singleton():
    """LocalPrincipalBackend returns the exact LOCAL_PRINCIPAL object."""
    backend = LocalPrincipalBackend()
    result = backend.derive_principal({"provider": "google", "sub": "user"})
    assert result is LOCAL_PRINCIPAL


# ──────────────────────────────────────────────────────────────────
# Construction: side-effect-free


def test_local_backend_construction_no_io():
    """LocalPrincipalBackend() requires no arguments and performs no I/O."""
    # Construct in a context that has no filesystem access (just verify no exception)
    backend = LocalPrincipalBackend()
    assert backend is not None


def test_local_backend_multiple_constructions():
    """Multiple LocalPrincipalBackend() instances are independent and both work."""
    b1 = LocalPrincipalBackend()
    b2 = LocalPrincipalBackend()
    assert b1.backend_id == b2.backend_id
    assert b1.derive_principal({}) is LOCAL_PRINCIPAL
    assert b2.derive_principal({}) is LOCAL_PRINCIPAL


# ──────────────────────────────────────────────────────────────────
# Capabilities


def test_local_backend_capabilities_shape():
    """LocalPrincipalBackend capabilities match spec/48."""
    caps = LocalPrincipalBackend().capabilities()
    assert caps.backend_id == "local"
    assert caps.is_local_only is True
    assert caps.supports_token_verification is False
    assert caps.produces_verified_principals is True


def test_local_backend_negative_probe_skipped_by_design():
    """Doctor negative probe is skipped for LocalPrincipalBackend (is_local_only=True).

    The negative probe (derive_principal with absent claims -> is_verified=False)
    does NOT apply to LocalPrincipalBackend — it always returns LOCAL_PRINCIPAL
    (is_verified=True). The doctor check MUST skip the negative probe when
    is_local_only=True (spec/48 §"Doctor check").

    This test documents the expectation: LocalPrincipalBackend.derive_principal({})
    returns is_verified=True, which would fail a naive negative probe.
    """
    backend = LocalPrincipalBackend()
    caps = backend.capabilities()
    assert caps.is_local_only is True
    # For is_local_only backends, derive_principal({}) returns is_verified=True.
    # This is CORRECT for LocalPrincipalBackend. The doctor skips the negative probe.
    result = backend.derive_principal({})
    assert result.is_verified is True


# ──────────────────────────────────────────────────────────────────
# Registry


def test_list_principal_backends_includes_both():
    """Registry contains both 'local' and 'static_claims'."""
    backends = list_principal_backends()
    assert "local" in backends
    assert "static_claims" in backends


def test_get_principal_backend_local():
    """get_principal_backend('local') returns LocalPrincipalBackend class."""
    cls = get_principal_backend("local")
    assert cls is LocalPrincipalBackend


def test_get_principal_backend_static_claims():
    """get_principal_backend('static_claims') returns StaticClaimsPrincipalBackend class."""
    cls = get_principal_backend("static_claims")
    assert cls is StaticClaimsPrincipalBackend


def test_get_principal_backend_unknown_raises():
    """get_principal_backend('nonexistent') raises BackendNotRegistered."""
    with pytest.raises(BackendNotRegistered):
        get_principal_backend("nonexistent_backend_id")


def test_register_and_unregister_custom_backend():
    """Custom backends can be registered and unregistered."""

    class MyCustomBackend:
        @property
        def backend_id(self):
            return "custom_test"

        def derive_principal(self, verified_claims):
            return LOCAL_PRINCIPAL

        def capabilities(self):
            from atomic_agents.principal.types import PrincipalCapabilities

            return PrincipalCapabilities(backend_id="custom_test")

    register_principal_backend("custom_test", MyCustomBackend)
    try:
        assert "custom_test" in list_principal_backends()
        cls = get_principal_backend("custom_test")
        assert cls is MyCustomBackend
    finally:
        unregister_principal_backend("custom_test")
    assert "custom_test" not in list_principal_backends()


# ──────────────────────────────────────────────────────────────────
# get_default_principal_backend()


def test_get_default_returns_local_when_env_absent(monkeypatch):
    """get_default_principal_backend() returns LocalPrincipalBackend when env var absent."""
    monkeypatch.delenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", raising=False)
    backend = get_default_principal_backend()
    assert isinstance(backend, LocalPrincipalBackend)


def test_get_default_returns_local_when_env_set_to_local(monkeypatch):
    """get_default_principal_backend() returns LocalPrincipalBackend when env var='local'."""
    monkeypatch.setenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", "local")
    backend = get_default_principal_backend()
    assert isinstance(backend, LocalPrincipalBackend)


def test_get_default_returns_static_when_env_set(monkeypatch):
    """get_default_principal_backend() returns StaticClaimsPrincipalBackend when env var='static_claims'."""
    monkeypatch.setenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", "static_claims")
    backend = get_default_principal_backend()
    assert isinstance(backend, StaticClaimsPrincipalBackend)


def test_get_default_fail_loud_on_unknown_env_var(monkeypatch):
    """get_default_principal_backend() raises BackendNotRegistered for unknown env var value.

    CRITICAL: must NOT silently degrade to LocalPrincipalBackend when the env var
    is set to an unknown value. A misconfigured org deployment must fail loudly.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", "nonexistent_backend_xyz")
    with pytest.raises(BackendNotRegistered):
        get_default_principal_backend()


def test_get_default_fail_loud_negative_control(monkeypatch):
    """Negative control: env var absent -> LocalPrincipalBackend (does NOT raise)."""
    monkeypatch.delenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", raising=False)
    # Should not raise — env var absent means home-user default
    backend = get_default_principal_backend()
    assert isinstance(backend, LocalPrincipalBackend)

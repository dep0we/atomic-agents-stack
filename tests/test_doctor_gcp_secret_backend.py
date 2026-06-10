"""Doctor tests for check_gcp_secret_backend() and the check_secret_backend()
gcp delegation (spec/38 §"GCP-specific doctor check", issue #340 PR 2).

Mirrors the check_vertex_credentials() test set in
tests/test_llm_vertex_gemini_backend.py. The GCP SDK is faked in every test
(no live GCP in CI per the build constraint noLiveGcp); the four outcome
branches (SDK-missing FAIL, DefaultCredentialsError FAIL, TransportError FAIL,
GOOGLE_CLOUD_PROJECT-absent WARN, success PASS) and the delegation routing are
all exercised against fake module trees.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
# Fake module-tree builders


def _fake_secretmanager_module() -> types.ModuleType:
    """Minimal fake ``google.cloud.secretmanager`` so the Step-1 SDK import
    passes. check_gcp_secret_backend() only does ``from google.cloud import
    secretmanager`` for presence; it never constructs a client."""
    return types.ModuleType("google.cloud.secretmanager")


def _fake_google_auth_modules(refresh_ok=True, detected_project=None):
    """Build a fake google.auth module tree whose default() returns a
    credentials object with a refresh() that succeeds (or raises TransportError).

    Includes a fake google.cloud.secretmanager so the Step-1 SDK import in
    check_gcp_secret_backend() passes. Returns the dict of sys.modules patches.
    """
    fake_google = types.ModuleType("google")
    fake_cloud = types.ModuleType("google.cloud")
    fake_secretmanager = _fake_secretmanager_module()
    fake_auth = types.ModuleType("google.auth")
    fake_auth_exceptions = types.ModuleType("google.auth.exceptions")

    class FakeDefaultCredentialsError(Exception):
        pass

    class FakeTransportError(Exception):
        pass

    fake_auth_exceptions.DefaultCredentialsError = FakeDefaultCredentialsError
    fake_auth_exceptions.TransportError = FakeTransportError

    class FakeCredentials:
        def refresh(self, request):
            if not refresh_ok:
                raise FakeTransportError("network down")
            # Success: no-op (token minted).

    def fake_default(**kw):
        return FakeCredentials(), detected_project

    fake_auth.default = fake_default
    fake_auth.exceptions = fake_auth_exceptions

    fake_auth_transport = types.ModuleType("google.auth.transport")
    fake_auth_transport_requests = types.ModuleType("google.auth.transport.requests")
    fake_auth_transport_requests.Request = lambda: object()

    return {
        "google": fake_google,
        "google.cloud": fake_cloud,
        "google.cloud.secretmanager": fake_secretmanager,
        "google.auth": fake_auth,
        "google.auth.exceptions": fake_auth_exceptions,
        "google.auth.transport": fake_auth_transport,
        "google.auth.transport.requests": fake_auth_transport_requests,
    }


# ─────────────────────────────────────────────────────────────────────────────
# check_gcp_secret_backend: the four outcome branches


def test_check_gcp_secret_backend_fail_missing_sdk():
    """Step 1: google.cloud.secretmanager absent -> FAIL naming the [gcp] extra."""
    from atomic_agents.doctor import FAIL, check_gcp_secret_backend

    # Block the SDK import so the Step-1 presence check fails.
    patched = {k: None for k in list(sys.modules.keys()) if "google" in k}
    patched["google"] = None
    patched["google.cloud"] = None
    patched["google.cloud.secretmanager"] = None

    with patch.dict(sys.modules, patched):
        result = check_gcp_secret_backend()

    assert result.status == FAIL
    assert result.name == "secret-backend[gcp]"
    assert "gcp" in result.fix_hint.lower()


def test_check_gcp_secret_backend_fail_no_adc():
    """Step 2: DefaultCredentialsError -> FAIL with a gcloud-login hint."""
    from atomic_agents.doctor import FAIL, check_gcp_secret_backend

    fake_google = types.ModuleType("google")
    fake_cloud = types.ModuleType("google.cloud")
    fake_secretmanager = _fake_secretmanager_module()
    fake_auth = types.ModuleType("google.auth")
    fake_auth_exceptions = types.ModuleType("google.auth.exceptions")

    class FakeDefaultCredentialsError(Exception):
        pass

    class FakeTransportError(Exception):
        pass

    fake_auth_exceptions.DefaultCredentialsError = FakeDefaultCredentialsError
    fake_auth_exceptions.TransportError = FakeTransportError

    def fake_default(**kw):
        raise FakeDefaultCredentialsError("no credentials found")

    fake_auth.default = fake_default
    fake_auth.exceptions = fake_auth_exceptions

    fake_auth_transport = types.ModuleType("google.auth.transport")
    fake_auth_transport_requests = types.ModuleType("google.auth.transport.requests")
    fake_auth_transport_requests.Request = lambda: None

    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.cloud": fake_cloud,
            "google.cloud.secretmanager": fake_secretmanager,
            "google.auth": fake_auth,
            "google.auth.exceptions": fake_auth_exceptions,
            "google.auth.transport": fake_auth_transport,
            "google.auth.transport.requests": fake_auth_transport_requests,
        },
    ):
        result = check_gcp_secret_backend()

    assert result.status == FAIL
    assert result.name == "secret-backend[gcp]"
    assert (
        "gcloud" in result.fix_hint.lower()
        or "application-default" in result.fix_hint.lower()
    )


def test_check_gcp_secret_backend_fail_on_token_refresh_network_error(monkeypatch):
    """Step 3: TransportError during refresh() (token mint fails) -> FAIL with a
    network-specific hint. Pins the refresh()-proves-usable behavior."""
    from atomic_agents.doctor import FAIL, check_gcp_secret_backend

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
    patches = _fake_google_auth_modules(refresh_ok=False)

    with patch.dict(sys.modules, patches):
        result = check_gcp_secret_backend()

    assert result.status == FAIL
    assert result.name == "secret-backend[gcp]"
    assert "network" in result.message.lower() or "refresh" in result.message.lower()


def test_check_gcp_secret_backend_warn_when_project_unset(monkeypatch):
    """Step 4: ADC resolves + token mints, but GOOGLE_CLOUD_PROJECT is unset ->
    WARN (Cloud Run / GKE auto-resolve the project; absence is not a
    misconfiguration)."""
    from atomic_agents.doctor import WARN, check_gcp_secret_backend

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    patches = _fake_google_auth_modules(refresh_ok=True, detected_project=None)

    with patch.dict(sys.modules, patches):
        result = check_gcp_secret_backend()

    assert result.status == WARN
    assert result.name == "secret-backend[gcp]"
    assert result.detail.get("token_valid") is True
    assert "GOOGLE_CLOUD_PROJECT" in result.message


def test_check_gcp_secret_backend_pass_when_project_set(monkeypatch):
    """Happy path: ADC resolves + token mints + GOOGLE_CLOUD_PROJECT set -> PASS
    with the project echoed."""
    from atomic_agents.doctor import PASS, check_gcp_secret_backend

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
    patches = _fake_google_auth_modules(refresh_ok=True, detected_project=None)

    with patch.dict(sys.modules, patches):
        result = check_gcp_secret_backend()

    assert result.status == PASS
    assert result.name == "secret-backend[gcp]"
    assert result.detail.get("token_valid") is True
    assert result.detail.get("project") == "my-gcp-project"
    assert "my-gcp-project" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# check_secret_backend delegation routing


def test_check_secret_backend_delegates_to_gcp(monkeypatch):
    """When the configured backend resolves to backend_id=='gcp',
    check_secret_backend() delegates to check_gcp_secret_backend() and returns
    a result re-stamped with the stable slot name 'secret-backend'. The SDK is
    faked; ADC + token mint succeed."""
    from atomic_agents.doctor import PASS, check_secret_backend

    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND", "gcp")
    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND_URL", "projects/testproj/secrets")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "testproj")
    patches = _fake_google_auth_modules(refresh_ok=True, detected_project=None)

    with patch.dict(sys.modules, patches):
        result = check_secret_backend()

    # Delegation reached check_gcp_secret_backend and the ADC/token probe passed.
    # The result is re-stamped with the stable slot name 'secret-backend' so an
    # operator keying off check names sees one consistent name across backends;
    # the backend id is carried in the detail dict.
    assert result.name == "secret-backend"
    assert result.detail.get("backend_id") == "gcp"
    assert result.status == PASS


def test_check_secret_backend_gcp_missing_sdk_fails_at_construction(monkeypatch):
    """When the [gcp] extra is absent, check_secret_backend()'s
    get_default_secret_backend() construction raises SecretBackendNotRegistered
    (a SecretError) and the check FAILs before delegation -- per spec/38, the
    SDK-missing case surfaces at backend construction, not inside
    check_gcp_secret_backend Step 1."""
    from atomic_agents.doctor import FAIL, check_secret_backend

    from tests._gcp_sdk_blocker import block_gcp_sdk

    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND", "gcp")
    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND_URL", "projects/testproj/secrets")

    with block_gcp_sdk():
        result = check_secret_backend()

    assert result.status == FAIL
    # Construction failed -> the result keeps the stable 'secret-backend' name
    # (delegation to check_gcp_secret_backend never ran).
    assert result.name == "secret-backend"
    assert "gcp" in result.fix_hint.lower()


def test_check_secret_backend_delegated_fail_carries_backend_id(monkeypatch):
    """A delegated GCP FAIL (here: token-mint TransportError) must still carry
    detail['backend_id'] == 'gcp'. The FAIL branches inside
    check_gcp_secret_backend() do not set backend_id themselves; the re-stamp in
    check_secret_backend() merges it so EVERY delegated outcome -- PASS, WARN,
    and all FAIL branches -- is machine-recoverable by an operator parsing
    detail['backend_id'] (Principle #5: audit trail is structural). The failure
    paths are exactly where that identity matters most."""
    from atomic_agents.doctor import FAIL, check_secret_backend

    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND", "gcp")
    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND_URL", "projects/testproj/secrets")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "testproj")
    # refresh_ok=False -> credentials.refresh() raises TransportError -> Step 3 FAIL.
    patches = _fake_google_auth_modules(refresh_ok=False, detected_project=None)

    with patch.dict(sys.modules, patches):
        result = check_secret_backend()

    assert result.status == FAIL
    assert result.name == "secret-backend"
    # The FAIL CheckResult built inside check_gcp_secret_backend has no backend_id
    # of its own; the merge in check_secret_backend's re-stamp supplies it.
    assert result.detail.get("backend_id") == "gcp"

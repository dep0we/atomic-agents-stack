"""GCP-specific tests for GCPSecretManagerBackend (spec/38, issue #340 PR 2).

Tests in THIS file cover GCP-specific behavior that does not belong in the
backend-agnostic conformance suite:
- backend_id == "gcp"
- capabilities.persists_plaintext is False
- capabilities.supports_rotation is True
- capabilities.supports_audit_logging is False
- Key-to-secret-name mapping (ANTHROPIC_API_KEY -> anthropic-api-key)
- Full resource path construction
- Error mapping (NotFound -> SecretNotFound, GoogleAPICallError -> SecretError)
- Trailing newline stripping (secrets created with echo instead of printf)
- Rotation awareness: successive calls with different mock values see the change
- ImportError names the [gcp] extra when SDK is absent
- make_gcp_secret_backend_from_url factory
- get_default_secret_backend gcp branch
- close() idempotent; idempotent on never-used instance
- locate() source label format (gcp-secret-manager: prefix)
- MUST 4: SecretNotFound message never contains the payload value
- MUST 9: no instance-level value cache

Tests that every backend must pass belong in test_secret_backend_protocol_conformance.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.secret_backend.backend import SecretError, SecretNotFound
from atomic_agents.secret_backend.gcp import (
    GCPSecretManagerBackend,
    make_gcp_secret_backend_from_url,
)
from atomic_agents.secret_backend.types import SecretCapabilities


# ─────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_client_returning(secret_name: str, value: bytes) -> MagicMock:
    """Return a mock client that returns ``value`` for ``secret_name``."""
    import google.api_core.exceptions as _gcp_exc

    client = MagicMock()

    def _access(name: str, **kwargs):
        parts = name.rstrip("/").split("/")
        sn = parts[-1]
        try:
            vi = parts.index("versions")
            sn = parts[vi - 1]
        except ValueError:
            pass
        if sn == secret_name:
            resp = MagicMock()
            resp.payload.data = value
            return resp
        raise _gcp_exc.NotFound(f"Secret {sn!r} not found (mock)")

    client.access_secret_version.side_effect = _access
    return client


def _make_absent_client() -> MagicMock:
    """Return a mock client that raises NotFound for every key."""
    import google.api_core.exceptions as _gcp_exc

    client = MagicMock()
    client.access_secret_version.side_effect = _gcp_exc.NotFound("not found (mock)")
    return client


def _make_error_client(exc_class_name: str) -> MagicMock:
    """Return a mock client that raises a specific google.api_core exception."""
    import google.api_core.exceptions as _gcp_exc

    exc_class = getattr(_gcp_exc, exc_class_name)
    client = MagicMock()
    client.access_secret_version.side_effect = exc_class(f"mock {exc_class_name}")
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Constructor and basic identity


def test_backend_id_is_gcp():
    backend = GCPSecretManagerBackend(
        url="projects/test-project/secrets", _client=_make_absent_client()
    )
    assert backend.backend_id == "gcp"


def test_backend_id_stable():
    backend = GCPSecretManagerBackend(
        url="projects/test-project/secrets", _client=_make_absent_client()
    )
    assert backend.backend_id == backend.backend_id


def test_url_trailing_slash_normalised():
    """Trailing slash in URL is stripped (resource path must not double-slash)."""
    backend = GCPSecretManagerBackend(
        url="projects/test-project/secrets/", _client=_make_absent_client()
    )
    assert not backend._url.endswith("/")


def test_invalid_url_raises_value_error():
    """URL that does not match projects/<id>/secrets raises ValueError."""
    with pytest.raises(ValueError, match="projects/<project_id>/secrets"):
        GCPSecretManagerBackend(url="https://invalid-url.example.com")


def test_invalid_url_missing_secrets_segment():
    with pytest.raises(ValueError):
        GCPSecretManagerBackend(url="projects/myproject")


# ─────────────────────────────────────────────────────────────────────────────
# Key-to-secret-name mapping


def test_key_to_secret_name_anthropic():
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert backend._secret_name("ANTHROPIC_API_KEY") == "anthropic-api-key"


def test_key_to_secret_name_redis_url():
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert backend._secret_name("REDIS_URL") == "redis-url"


def test_resource_name_format():
    """Full resource path follows projects/<id>/secrets/<name>/versions/latest.

    Reference: https://cloud.google.com/secret-manager/docs/access-secret-version
    """
    backend = GCPSecretManagerBackend(
        url="projects/myproj/secrets", _client=_make_absent_client()
    )
    rn = backend._resource_name("ANTHROPIC_API_KEY")
    assert rn == "projects/myproj/secrets/anthropic-api-key/versions/latest"


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities


def test_capabilities_type():
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert isinstance(backend.capabilities, SecretCapabilities)


def test_capabilities_supports_rotation_true():
    """supports_rotation=True: each get() re-resolves live (no cache, MUST 9)."""
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert backend.capabilities.supports_rotation is True


def test_capabilities_supports_audit_logging_false():
    """supports_audit_logging=False: Cloud Audit Logs are operator-side config.

    GCP Cloud Audit Logs for accessSecretVersion are only enabled when the
    operator configures Data Access audit log policies in IAM -- the framework
    neither configures nor reads them.
    Reference: https://cloud.google.com/secret-manager/docs/audit-logging
    """
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert backend.capabilities.supports_audit_logging is False


def test_capabilities_persists_plaintext_false():
    """persists_plaintext=False: GCP SM stores AES-256 encrypted payloads (KMS).

    No plaintext credential travels in any framework-owned file or vault dir.
    """
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert backend.capabilities.persists_plaintext is False


def test_capabilities_is_property():
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert isinstance(type(backend).capabilities, property)


def test_capabilities_is_frozen():
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    with pytest.raises(Exception):
        backend.capabilities.supports_rotation = False  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# get() success path


def test_get_returns_value():
    client = _make_client_returning("anthropic-api-key", b"sk-ant-real-key")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    assert backend.get("ANTHROPIC_API_KEY") == "sk-ant-real-key"


def test_get_strips_trailing_newline():
    """Secrets created via ``echo`` carry a trailing newline; strip must remove it.

    The bootstrap script (extras/gcp/secret-manager-bootstrap.sh) uses
    ``printf '%s'`` to avoid trailing newlines, but operators may use ``echo``.
    A trailing newline causes silent auth failures in provider SDKs.
    """
    client = _make_client_returning("anthropic-api-key", b"sk-ant-real-key\n")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    assert backend.get("ANTHROPIC_API_KEY") == "sk-ant-real-key"


def test_get_strips_trailing_whitespace():
    client = _make_client_returning("anthropic-api-key", b"sk-ant-real-key   ")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    assert backend.get("ANTHROPIC_API_KEY") == "sk-ant-real-key"


# ─────────────────────────────────────────────────────────────────────────────
# get() error paths -- error mapping


def test_get_raises_secret_not_found_on_gcp_not_found():
    """google.api_core.exceptions.NotFound maps to SecretNotFound."""
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    with pytest.raises(SecretNotFound):
        backend.get("ANTHROPIC_API_KEY")


def test_get_raises_secret_error_on_permission_denied():
    """google.api_core.exceptions.PermissionDenied maps to SecretError."""
    client = _make_error_client("PermissionDenied")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretError):
        backend.get("ANTHROPIC_API_KEY")


def test_get_raises_secret_error_on_unavailable():
    """google.api_core.exceptions.ServiceUnavailable maps to SecretError."""
    client = _make_error_client("ServiceUnavailable")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretError):
        backend.get("ANTHROPIC_API_KEY")


def test_get_raises_secret_error_on_unauthenticated():
    """google.api_core.exceptions.Unauthenticated maps to SecretError."""
    client = _make_error_client("Unauthenticated")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretError):
        backend.get("ANTHROPIC_API_KEY")


def test_permission_denied_is_subclass_of_secret_error():
    """PermissionDenied error is catchable as SecretError (fail-closed contract)."""
    client = _make_error_client("PermissionDenied")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    exc_caught = None
    try:
        backend.get("ANTHROPIC_API_KEY")
    except SecretError as e:
        exc_caught = e
    assert exc_caught is not None


# ─────────────────────────────────────────────────────────────────────────────
# MUST 4: No secret value in exception messages


def test_secret_not_found_message_excludes_payload():
    """SecretNotFound message MUST NOT contain the resolved value (MUST 4).

    Even when the GCP API returns a value (then the backend finds it empty/
    whitespace), the error message must never embed the payload.
    """
    # Return a non-empty value via mock, then make it "empty" via whitespace
    client = _make_client_returning("anthropic-api-key", b"   ")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretNotFound) as exc_info:
        backend.get("ANTHROPIC_API_KEY")
    assert "   " not in str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_not_found_message_names_the_key():
    """SecretNotFound message MUST name the key (for operator triage)."""
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    with pytest.raises(SecretNotFound) as exc_info:
        backend.get("MY_CUSTOM_KEY")
    assert "MY_CUSTOM_KEY" in str(exc_info.value)


def test_non_utf8_payload_raises_secret_error_without_byte_leak():
    """A non-UTF-8 payload raises SecretError, names the key, leaks no bytes (MUST 4).

    Secret Manager stores arbitrary bytes. A payload that is not valid UTF-8 must
    surface as SecretError (the get() contract: SecretError for all non-NotFound
    errors) and the message MUST NOT embed any byte fragment or repr of the raw
    bytes -- the non-UTF-8 path is the riskiest leak surface, so it gets the same
    no-leak pin as the empty/whitespace path.
    """
    invalid = b"\xff\xfe\xfd"
    client = _make_client_returning("anthropic-api-key", invalid)
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretError) as exc_info:
        backend.get("ANTHROPIC_API_KEY")
    msg = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in msg
    # No byte fragment / repr of the raw bytes leaks into the message.
    assert "\\xff" not in msg
    assert "\\xfe" not in msg
    assert repr(invalid) not in msg
    assert str(invalid) not in msg


def test_unexpected_non_gcp_error_maps_to_secret_error_without_leak():
    """A non-GoogleAPICallError exception stays fail-closed as SecretError (MUST 4).

    The catch-all ``else`` branch in get() must map any unexpected exception type
    to SecretError, name the key, and never embed the key's resolved value (no
    value exists on this path, but the contract must hold for every exit).
    """
    client = MagicMock()
    client.access_secret_version.side_effect = RuntimeError("boom-internal-detail")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretError) as exc_info:
        backend.get("ANTHROPIC_API_KEY")
    msg = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in msg
    # Only the exception type name is safe to surface, never raw error text that
    # could in principle carry a value fragment.
    assert "RuntimeError" in msg


# ─────────────────────────────────────────────────────────────────────────────
# MUST 7: Empty/whitespace value treated as absent


def test_empty_bytes_payload_raises_secret_not_found():
    client = _make_client_returning("anthropic-api-key", b"")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretNotFound):
        backend.get("ANTHROPIC_API_KEY")


def test_whitespace_bytes_payload_raises_secret_not_found():
    client = _make_client_returning("anthropic-api-key", b"   \n  ")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    with pytest.raises(SecretNotFound):
        backend.get("ANTHROPIC_API_KEY")


def test_empty_payload_get_optional_returns_none():
    client = _make_client_returning("anthropic-api-key", b"")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    assert backend.get_optional("ANTHROPIC_API_KEY") is None


# ─────────────────────────────────────────────────────────────────────────────
# MUST 8: has() strictly delegates to get_optional()


def test_has_true_when_present():
    client = _make_client_returning("anthropic-api-key", b"real-key")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    assert backend.has("ANTHROPIC_API_KEY") is True


def test_has_false_when_absent():
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert backend.has("ANTHROPIC_API_KEY") is False


def test_has_agrees_with_get_optional():
    """has() and get_optional() must agree (MUST 8 split-brain prevention)."""
    client = _make_client_returning("anthropic-api-key", b"some-key")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    assert backend.has("ANTHROPIC_API_KEY") == (
        backend.get_optional("ANTHROPIC_API_KEY") is not None
    )


# ─────────────────────────────────────────────────────────────────────────────
# MUST 9: No instance-level value cache (rotation awareness)


def test_get_sees_rotated_value():
    """Each get() re-resolves live -- a changed mock value is visible immediately."""

    client = MagicMock()
    call_count = [0]
    values = [b"first-key", b"rotated-key"]

    def _rotating(name: str, **kwargs):
        resp = MagicMock()
        resp.payload.data = values[min(call_count[0], len(values) - 1)]
        call_count[0] += 1
        return resp

    client.access_secret_version.side_effect = _rotating
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)

    first = backend.get("ANTHROPIC_API_KEY")
    second = backend.get("ANTHROPIC_API_KEY")
    assert first == "first-key"
    assert second == "rotated-key"


def test_no_instance_state_stores_resolved_value():
    """Verify the backend stores no secret value in instance attributes."""
    client = _make_client_returning("anthropic-api-key", b"secret-value")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    backend.get("ANTHROPIC_API_KEY")
    # After a successful get(), no instance attribute should hold the resolved value
    for attr_val in vars(backend).values():
        assert attr_val != "secret-value", (
            "Found resolved secret value stored in backend instance state"
        )


# ─────────────────────────────────────────────────────────────────────────────
# MUST 5: locate() source label


def test_locate_returns_secret_ref_when_present():
    client = _make_client_returning("anthropic-api-key", b"some-key")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert ref.key == "ANTHROPIC_API_KEY"
    assert ref.present is True


def test_locate_source_label_format():
    """locate() source starts with 'gcp-secret-manager:' prefix."""
    client = _make_client_returning("anthropic-api-key", b"some-key")
    backend = GCPSecretManagerBackend(url="projects/myproject/secrets", _client=client)
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert ref.source.startswith("gcp-secret-manager:")
    assert "anthropic-api-key" in ref.source
    assert "myproject" in ref.source


def test_locate_source_excludes_value():
    """locate() source MUST NOT contain the resolved value (MUST 5)."""
    value = "sk-ant-supersecret"
    client = _make_client_returning("anthropic-api-key", value.encode())
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert value not in ref.source


def test_locate_returns_none_when_absent():
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    assert backend.locate("NO_SUCH_KEY") is None


# ─────────────────────────────────────────────────────────────────────────────
# close() lifecycle


def test_close_on_unused_instance_is_noop():
    """close() on an instance that never called get() must be a no-op."""
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    # Remove the injected client to simulate a no-client (never-used) state
    backend._client_is_injected = False
    backend._client = None
    backend.close()  # must not raise


def test_close_is_idempotent_on_injected_client():
    """close() called twice on a test-injected client must not raise."""
    backend = GCPSecretManagerBackend(
        url="projects/p/secrets", _client=_make_absent_client()
    )
    backend.close()
    backend.close()  # must not raise


def test_close_with_real_client_closes_transport():
    """close() calls transport.close() on the real (non-injected) client."""
    backend = GCPSecretManagerBackend(url="projects/p/secrets")
    mock_client = MagicMock()
    backend._client = mock_client
    backend._client_is_injected = False

    backend.close()

    mock_client.transport.close.assert_called_once()
    assert backend._client is None


def test_close_idempotent_with_real_client():
    """Calling close() twice with a real client only calls transport.close() once."""
    backend = GCPSecretManagerBackend(url="projects/p/secrets")
    mock_client = MagicMock()
    backend._client = mock_client
    backend._client_is_injected = False

    backend.close()
    backend.close()  # second call is a no-op

    mock_client.transport.close.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Factory


def test_factory_creates_backend():
    backend = make_gcp_secret_backend_from_url("projects/myproject/secrets")
    assert isinstance(backend, GCPSecretManagerBackend)
    assert backend._project_id == "myproject"


def test_factory_strips_trailing_slash():
    backend = make_gcp_secret_backend_from_url("projects/myproject/secrets/")
    assert not backend._url.endswith("/")


def test_factory_raises_on_invalid_url():
    with pytest.raises(ValueError):
        make_gcp_secret_backend_from_url("https://invalid.example.com")


# ─────────────────────────────────────────────────────────────────────────────
# get_default_secret_backend() -- gcp branch


def test_get_default_gcp_branch_with_url(monkeypatch):
    """With SDK installed + URL set, get_default_secret_backend() returns GCPSecretManagerBackend."""
    from atomic_agents.secret_backend import get_default_secret_backend

    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND", "gcp")
    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND_URL", "projects/testproj/secrets")
    # Patch make_gcp_secret_backend_from_url to avoid real client construction
    with patch(
        "atomic_agents.secret_backend.gcp.make_gcp_secret_backend_from_url"
    ) as mock_factory:
        mock_backend = MagicMock()
        mock_factory.return_value = mock_backend
        result = get_default_secret_backend()
    assert result is mock_backend
    mock_factory.assert_called_once_with("projects/testproj/secrets")


def test_get_default_gcp_no_url_raises(monkeypatch):
    """ATOMIC_AGENTS_SECRET_BACKEND=gcp without URL raises SecretBackendNotRegistered."""
    from atomic_agents.secret_backend import (
        SecretBackendNotRegistered,
        get_default_secret_backend,
    )

    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND", "gcp")
    monkeypatch.delenv("ATOMIC_AGENTS_SECRET_BACKEND_URL", raising=False)
    with pytest.raises(SecretBackendNotRegistered, match="URL"):
        get_default_secret_backend()


def test_get_default_gcp_missing_sdk_raises_not_registered(monkeypatch):
    """ATOMIC_AGENTS_SECRET_BACKEND=gcp without the [gcp] extra raises SecretBackendNotRegistered.

    Simulates SDK-absence the realistic way: the gcp module imports fine (it does
    NOT import the SDK at module level), and the missing-SDK ImportError fires from
    GCPSecretManagerBackend.__init__. The factory MUST re-raise that as
    SecretBackendNotRegistered (not a raw ImportError) so callers catching
    SecretError see a clean error (fail-closed contract; MEMORY.md
    feedback_fail_closed_catches_base_error_class). The assertion is tightened to
    SecretBackendNotRegistered ONLY -- a raw-ImportError regression must fail.
    """
    from atomic_agents.secret_backend import (
        SecretBackendNotRegistered,
        get_default_secret_backend,
    )

    from tests._gcp_sdk_blocker import block_gcp_sdk

    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND", "gcp")
    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND_URL", "projects/testproj/secrets")

    with block_gcp_sdk():
        with pytest.raises(SecretBackendNotRegistered, match=r"\[gcp\] extra"):
            get_default_secret_backend()


# ─────────────────────────────────────────────────────────────────────────────
# Thread safety (MUST 9 concurrent calls)


def test_concurrent_get_calls_consistent():
    """get() from concurrent threads returns consistent results (MUST 9)."""
    import threading

    client = _make_client_returning("anthropic-api-key", b"concurrent-test-key")
    backend = GCPSecretManagerBackend(url="projects/p/secrets", _client=client)

    results = []
    errors = []

    def call_get():
        try:
            val = backend.get("ANTHROPIC_API_KEY")
            results.append(val)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=call_get) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    assert all(v == "concurrent-test-key" for v in results)

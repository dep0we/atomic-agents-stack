"""Protocol conformance tests for SecretBackend (spec/37).

~25 conformance tests that exercise every MUST clause against every registered
SecretBackend implementation.

**How to add a new backend (PR 2+):**
1. Add the backend's string id to ``params`` in the ``backend`` fixture below.
2. Add an ``elif request.param == "<id>"`` branch that constructs the backend.
3. Every test in THIS file runs against all backends automatically.

**What belongs here vs test_secret_backend_filesystem.py:**

This file contains ONLY backend-agnostic Protocol contract tests — tests that
MUST pass for every conforming SecretBackend, regardless of implementation:
- Key charset validation (MUST 1)
- Machine-scoped sources (MUST 2): _KEYS_JSON_PATH is absolute and under HOME
- Capability advertisement (MUST 3): @property, frozen, consistent
- SecretNotFound excludes value (MUST 4), locate() excludes value (MUST 5)
- Empty/whitespace absent (MUST 7)
- has() delegates to get_optional() (MUST 8)
- locate() behavior, Protocol isinstance, backend_id stability, close() idempotent

``test_secret_backend_filesystem.py`` contains filesystem-specific tests:
- resolve_with_spec() signature and behavior
- persists_plaintext=False / backend_id=="filesystem" capability values
- Tests that patch filesystem._resolve_from_keychain / _resolve_from_keys_json

Do NOT add filesystem-specific tests here — they will break when
``params=["filesystem", "gcp"]`` is extended in PR 2.

Pattern mirrors test_corpus_protocol_conformance.py and
test_mcp_server_registry_conformance.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.secret_backend import (
    FilesystemSecretBackend,
    SecretBackend,
    SecretCapabilities,
    SecretNotFound,
    SecretRef,
)
from atomic_agents.secret_backend.backend import _validate_key
from atomic_agents.secret_backend import filesystem as _fs_module


# ─────────────────────────────────────────────────────────────────────────────
# Fixture


@pytest.fixture(params=["filesystem"])
def backend(request) -> FilesystemSecretBackend:
    """A fresh SecretBackend instance for the parametrized implementation.

    PR 2 extends params to ["filesystem", "gcp"] and adds an elif branch for
    GCPSecretManagerBackend; every conformance test then runs against both
    backends automatically.

    IMPORTANT: only add tests to this file that EVERY SecretBackend must pass.
    Filesystem-specific tests (resolve_with_spec, persists_plaintext value,
    backend_id=="filesystem", keychain/keys_json patches) belong in
    test_secret_backend_filesystem.py.
    """
    if request.param == "filesystem":
        return FilesystemSecretBackend()
    raise ValueError(f"Unknown backend param: {request.param}")


# ─────────────────────────────────────────────────────────────────────────────
# MUST 1: Key charset validation


def test_validate_key_accepts_valid_names():
    """Valid POSIX env-var names pass without error."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FOO", "A1_B2_C3", "X"):
        _validate_key(key)  # must not raise


def test_validate_key_rejects_empty():
    with pytest.raises(ValueError, match="must not be empty"):
        _validate_key("")


def test_validate_key_rejects_lowercase():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("anthropic_api_key")


def test_validate_key_rejects_path_traversal():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("../etc/passwd")


def test_validate_key_rejects_slash():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("SOME/KEY")


def test_validate_key_rejects_dot():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("MY.KEY")


def test_get_raises_value_error_on_invalid_key(backend):
    """API boundary: get() raises ValueError before any backend access."""
    with pytest.raises(ValueError):
        backend.get("invalid.key")


def test_get_optional_raises_value_error_on_invalid_key(backend):
    with pytest.raises(ValueError):
        backend.get_optional("../etc/passwd")


def test_has_raises_value_error_on_invalid_key(backend):
    with pytest.raises(ValueError):
        backend.has("bad/key")


def test_locate_raises_value_error_on_invalid_key(backend):
    with pytest.raises(ValueError):
        backend.locate("lowercase_bad")


# ─────────────────────────────────────────────────────────────────────────────
# MUST 2: Machine-scoped sources (credentials MUST NOT travel with the vault)


def test_keys_json_path_is_machine_scoped():
    """_KEYS_JSON_PATH MUST resolve under the machine home directory, never
    under any vault or agent root (spec/37 MUST 2: vault portability would
    carry credentials).
    """
    keys_path = _fs_module._KEYS_JSON_PATH
    home = Path.home()
    # The resolved path must be under the machine home dir.
    assert str(keys_path).startswith(str(home)), (
        f"_KEYS_JSON_PATH {keys_path!r} is not under the machine home "
        f"dir {home!r}. Credentials must be machine-scoped, not vault-relative."
    )


def test_keys_json_path_not_relative_to_cwd():
    """_KEYS_JSON_PATH must be absolute — never derived from cwd or a vault
    root (spec/37 MUST 2).
    """
    keys_path = _fs_module._KEYS_JSON_PATH
    assert keys_path.is_absolute(), (
        f"_KEYS_JSON_PATH {keys_path!r} must be absolute so it cannot be "
        f"confused with a vault-relative path."
    )


def test_keys_json_path_uses_expanduser():
    """_KEYS_JSON_PATH must expand the home dir via Path.home() or equivalent,
    not use a literal path (spec/37 MUST 2 — machine-scoped, portable).
    """
    # Path.home() expands ~ correctly; verify the path starts with the
    # actual home dir rather than a literal tilde or the cwd.
    keys_path = _fs_module._KEYS_JSON_PATH
    home_str = str(Path.home())
    assert str(keys_path).startswith(home_str), (
        f"_KEYS_JSON_PATH {keys_path!r} must begin with the expanded home "
        f"dir ({home_str!r}), not a literal '~' or relative path."
    )


# ─────────────────────────────────────────────────────────────────────────────
# MUST 3: Capability honesty


def test_capabilities_is_property(backend):
    """capabilities MUST be a @property, not a plain method (spec/37 MUST 3)."""
    assert isinstance(type(backend).capabilities, property), (
        "backend.capabilities must be a @property — "
        "call sites use backend.capabilities.supports_rotation syntax"
    )


def test_capabilities_returns_frozen_dataclass(backend):
    """SecretCapabilities must be frozen=True."""
    caps = backend.capabilities
    assert isinstance(caps, SecretCapabilities)
    with pytest.raises(Exception):  # FrozenInstanceError
        caps.supports_rotation = False  # type: ignore[misc]


def test_capabilities_consistent_across_calls(backend):
    """Same instance returns same capabilities object (or equal one)."""
    caps1 = backend.capabilities
    caps2 = backend.capabilities
    assert caps1 == caps2


# ─────────────────────────────────────────────────────────────────────────────
# MUST 4-5: No secret value in exceptions or SecretRef


def test_secret_not_found_message_excludes_value(backend, monkeypatch):
    """SecretNotFound message MUST NOT contain the resolved value (MUST 4)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SHOULD_NOT_APPEAR")
    # Remove the key so it triggers SecretNotFound from a different source
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
    # Patch keychain + keys.json to also not find it
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        with pytest.raises(SecretNotFound) as exc_info:
            backend.get("ANTHROPIC_API_KEY")
    assert "SHOULD_NOT_APPEAR" not in str(exc_info.value)


def test_secret_not_found_message_names_key(backend, monkeypatch):
    """SecretNotFound message MUST name the key (for triage)."""
    monkeypatch.delenv("MY_CUSTOM_KEY", raising=False)
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        with pytest.raises(SecretNotFound) as exc_info:
            backend.get("MY_CUSTOM_KEY")
    assert "MY_CUSTOM_KEY" in str(exc_info.value)


def test_locate_source_excludes_value(backend, monkeypatch):
    """SecretRef.source MUST NOT contain the resolved value (MUST 5)."""
    secret_value = "sk-ant-ultra-secret-value-12345"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret_value)
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert secret_value not in ref.source
    assert ref.source.startswith("env:")


# ─────────────────────────────────────────────────────────────────────────────
# MUST 7: Empty-string treated as absent


def test_empty_env_var_treated_as_absent(backend, monkeypatch):
    """Empty-string env var must raise SecretNotFound (not return '')."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        with pytest.raises(SecretNotFound):
            backend.get("ANTHROPIC_API_KEY")


def test_whitespace_env_var_treated_as_absent(backend, monkeypatch):
    """Whitespace-only env var must be treated as absent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        with pytest.raises(SecretNotFound):
            backend.get("ANTHROPIC_API_KEY")


def test_get_optional_returns_none_for_absent(backend, monkeypatch):
    """get_optional returns None (not '') when key is absent."""
    monkeypatch.delenv("MY_MISSING_KEY", raising=False)
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        result = backend.get_optional("MY_MISSING_KEY")
    assert result is None


def test_empty_env_get_optional_returns_none(backend, monkeypatch):
    """get_optional returns None for empty-string env var."""
    monkeypatch.setenv("MY_EMPTY_KEY", "")
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        result = backend.get_optional("MY_EMPTY_KEY")
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# MUST 8: has() delegates to get_optional()


def test_has_delegates_to_get_optional(backend, monkeypatch):
    """has() MUST agree with get_optional() — split-brain prevention."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    assert backend.has("ANTHROPIC_API_KEY") is True
    assert (backend.get_optional("ANTHROPIC_API_KEY") is not None) is True


def test_has_returns_false_for_empty_string(backend, monkeypatch):
    """has() returns False when env var is '' (same as get_optional)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        assert backend.has("ANTHROPIC_API_KEY") is False


def test_has_false_when_absent(backend, monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY_EVER", raising=False)
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        assert backend.has("NO_SUCH_KEY_EVER") is False


# ─────────────────────────────────────────────────────────────────────────────
# locate() behavior (MUST 5 secrecy + MUST 8 has()-delegation via get_optional)


def test_locate_returns_secret_ref_when_present(backend, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "some-key")
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert isinstance(ref, SecretRef)
    assert ref.key == "ANTHROPIC_API_KEY"
    assert ref.present is True
    assert "ANTHROPIC_API_KEY" in ref.source or "anthropic" in ref.source


def test_locate_returns_none_when_absent(backend, monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY_EVER", raising=False)
    with (
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
            return_value=None,
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        ref = backend.locate("NO_SUCH_KEY_EVER")
    assert ref is None


# ─────────────────────────────────────────────────────────────────────────────
# MUST 9: No caching — concurrent calls are safe


def test_concurrent_get_calls_consistent(backend, monkeypatch):
    """get() called from multiple threads returns consistent results."""
    import threading

    monkeypatch.setenv("ANTHROPIC_API_KEY", "concurrent-test-key")
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


# ─────────────────────────────────────────────────────────────────────────────
# Protocol isinstance check


def test_filesystem_backend_is_instance_of_protocol(backend):
    """FilesystemSecretBackend satisfies the runtime-checkable SecretBackend Protocol."""
    assert isinstance(backend, SecretBackend)


# ─────────────────────────────────────────────────────────────────────────────
# backend_id stability (Protocol contract: stable across calls)
# backend_id value is implementation-specific; assert it in the impl test file.


def test_backend_id_stable(backend):
    """backend_id must return the same value on repeated calls (identity contract)."""
    assert backend.backend_id == backend.backend_id


def test_backend_id_is_nonempty_string(backend):
    """backend_id must be a non-empty string (Protocol contract)."""
    bid = backend.backend_id
    assert isinstance(bid, str) and len(bid) > 0


# ─────────────────────────────────────────────────────────────────────────────
# close() is idempotent


def test_close_is_idempotent(backend):
    backend.close()
    backend.close()  # must not raise

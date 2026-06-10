"""Filesystem-specific tests for FilesystemSecretBackend (spec/38).

~10 tests covering the three-source cascade order, Darwin-only Keychain branch,
keys.json path construction, _get_key redirect wrapper behavior, factory
behavior, and doctor check wiring.

Also contains tests that must live here rather than in the parametrized
conformance file (test_secret_backend_protocol_conformance.py) because they
assert filesystem-specific behavior: resolve_with_spec() signature, capability
values (persists_plaintext, backend_id=="filesystem"), and tests that patch
filesystem._resolve_from_keychain / _resolve_from_keys_json.

Pattern mirrors test_corpus_filesystem_backend.py.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents._llm import (
    _get_key,
)
from atomic_agents.exceptions import AtomicAgentsError
from atomic_agents.secret_backend import (
    FilesystemSecretBackend,
    SecretBackendNotRegistered,
    SecretError,
    SecretNotFound,
    get_default_secret_backend,
    list_secret_backends,
)


# ─────────────────────────────────────────────────────────────────────────────
# Source 1: environment variable cascade


def test_env_var_primary_alias_wins(monkeypatch):
    """Source 1: env var returns stripped value."""
    backend = FilesystemSecretBackend()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-primary")
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
    val = backend.get("ANTHROPIC_API_KEY")
    assert val == "sk-ant-primary"


def test_env_var_alternate_alias_wins(monkeypatch):
    """Source 1: ATOMIC_AGENTS_ANTHROPIC_KEY is tried before ANTHROPIC_API_KEY."""
    backend = FilesystemSecretBackend()
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "sk-ant-primary-alias")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fallback")
    val = backend.get("ANTHROPIC_API_KEY")
    assert val == "sk-ant-primary-alias"


def test_env_var_value_stripped(monkeypatch):
    """Source 1: leading/trailing whitespace is stripped."""
    backend = FilesystemSecretBackend()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-padded  ")
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
    val = backend.get("ANTHROPIC_API_KEY")
    assert val == "sk-ant-padded"


# ─────────────────────────────────────────────────────────────────────────────
# Source 2: macOS Keychain


def test_keychain_used_on_darwin_when_env_missing(monkeypatch):
    """Source 2: Keychain is probed on Darwin when env var is absent."""
    backend = FilesystemSecretBackend()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)

    mock_result = MagicMock()
    mock_result.stdout = "sk-ant-from-keychain\n"

    with (
        patch("sys.platform", "darwin"),
        patch("subprocess.run", return_value=mock_result) as mock_run,
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        val = backend.get("ANTHROPIC_API_KEY")

    assert val == "sk-ant-from-keychain"
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert "security" in call_args


def test_keychain_skipped_on_linux(monkeypatch):
    """Source 2: Keychain branch is skipped on non-Darwin platforms."""
    backend = FilesystemSecretBackend()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)

    with (
        patch("sys.platform", "linux"),
        patch("subprocess.run") as mock_run,
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        with pytest.raises(SecretNotFound):
            backend.get("ANTHROPIC_API_KEY")

    mock_run.assert_not_called()


def test_keychain_timeout_falls_through(monkeypatch):
    """Source 2: TimeoutExpired causes fall-through to Source 3, not crash."""
    backend = FilesystemSecretBackend()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)

    with (
        patch("sys.platform", "darwin"),
        patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="security", timeout=5),
        ),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        with pytest.raises(SecretNotFound):
            backend.get("ANTHROPIC_API_KEY")


# ─────────────────────────────────────────────────────────────────────────────
# Source 3: keys.json


def test_keys_json_used_when_env_and_keychain_missing(tmp_path, monkeypatch):
    """Source 3: keys.json is read when env and Keychain are absent."""
    backend = FilesystemSecretBackend()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)

    keys_json = {"anthropic": "sk-ant-from-json"}

    with (
        patch("sys.platform", "linux"),
        patch(
            "atomic_agents.secret_backend.filesystem._KEYS_JSON_PATH",
            tmp_path / "keys.json",
        ),
    ):
        (tmp_path / "keys.json").write_text(json.dumps(keys_json))
        with patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value="sk-ant-from-json",
        ):
            val = backend.get("ANTHROPIC_API_KEY")

    assert val == "sk-ant-from-json"


def test_keys_json_invalid_json_logs_warning_and_falls_through(
    tmp_path, caplog, monkeypatch
):
    """Source 3: corrupt keys.json logs WARNING and falls through (not crash)."""
    import logging
    from atomic_agents.secret_backend import filesystem as fs_module

    monkeypatch.delenv("MY_CUSTOM_KEY", raising=False)

    corrupt_path = tmp_path / "keys.json"
    corrupt_path.write_text("{not valid json", encoding="utf-8")

    with (
        patch.object(fs_module, "_KEYS_JSON_PATH", corrupt_path),
        patch("sys.platform", "linux"),
    ):
        with caplog.at_level(
            logging.WARNING, logger="atomic_agents.secret_backend.filesystem"
        ):
            result = fs_module._resolve_from_keys_json("some_key")

    assert result is None
    assert any(
        "JSONDecodeError" in r.message or "failed to parse" in r.message
        for r in caplog.records
    )


# ─────────────────────────────────────────────────────────────────────────────
# _get_key redirect wrapper


def test_get_key_routes_through_resolve_with_spec(monkeypatch):
    """_get_key() MUST route through FilesystemSecretBackend.resolve_with_spec,
    forwarding the full (env_vars, keychain_name, config_key) triple."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-via-backend")
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)

    mock_backend = FilesystemSecretBackend()
    with (
        patch(
            "atomic_agents.secret_backend.get_default_secret_backend",
            return_value=mock_backend,
        ),
        patch.object(
            mock_backend, "resolve_with_spec", return_value="sk-ant-via-backend"
        ) as mock_rws,
    ):
        result = _get_key(
            env_vars=["ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"],
            keychain_name="atomic-agents-anthropic",
            config_key="anthropic",
        )
        mock_rws.assert_called_once_with(
            ["ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"],
            "atomic-agents-anthropic",
            "anthropic",
        )

    assert result == "sk-ant-via-backend"


def test_get_key_raises_atomic_agents_error_on_missing(monkeypatch):
    """_get_key() raises AtomicAgentsError (not SecretNotFound) for backward compat."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)

    with (
        patch("sys.platform", "linux"),
        patch(
            "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
            return_value=None,
        ),
    ):
        with pytest.raises(AtomicAgentsError):
            _get_key(
                env_vars=["ANTHROPIC_API_KEY"],
                keychain_name="atomic-agents-anthropic",
                config_key="anthropic",
            )


def test_get_key_secret_not_found_propagates_as_atomic_agents_error(monkeypatch):
    """resolve_with_spec returning None is wrapped in AtomicAgentsError (backward compat)."""
    mock_backend = MagicMock(spec=FilesystemSecretBackend)
    mock_backend.resolve_with_spec.return_value = None

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=mock_backend,
    ):
        with pytest.raises(AtomicAgentsError):
            _get_key(
                env_vars=["SOME_KEY"],
                keychain_name="some-keychain",
                config_key="some",
            )


def test_get_key_custom_keyspec_uses_caller_supplied_keychain_and_config_key(
    monkeypatch, tmp_path
):
    """Custom KeySpec with non-default keychain_name and config_key MUST resolve
    from all three sources using the CALLER-SUPPLIED names, not derived names.

    Regression test for the P1 found in Round 1 review: _get_key previously
    dropped keychain_name and config_key for non-_PROVIDER_METADATA providers,
    silently breaking any OpenAICompatibleLLMBackend with a custom KeySpec.
    """
    # 1. Resolution via custom secondary env-var alias (not env_vars[0])
    monkeypatch.delenv("MY_LLM_PRIMARY_KEY", raising=False)
    monkeypatch.setenv("MY_LLM_SECONDARY_KEY", "secondary-value")
    result = _get_key(
        env_vars=["MY_LLM_PRIMARY_KEY", "MY_LLM_SECONDARY_KEY"],
        keychain_name="my-llm",
        config_key="myllm",
    )
    assert result == "secondary-value", (
        "resolve_with_spec must probe all env_vars aliases, not just env_vars[0]"
    )
    monkeypatch.delenv("MY_LLM_SECONDARY_KEY", raising=False)

    # 2. Resolution via custom config_key in keys.json (not derived from env var)
    keys_file = tmp_path / "keys.json"
    keys_file.write_text('{"myllm": "keys-json-value"}')
    with patch("atomic_agents.secret_backend.filesystem._KEYS_JSON_PATH", keys_file):
        result = _get_key(
            env_vars=["MY_LLM_PRIMARY_KEY", "MY_LLM_SECONDARY_KEY"],
            keychain_name="my-llm",
            config_key="myllm",
        )
    assert result == "keys-json-value", (
        "resolve_with_spec must use caller-supplied config_key 'myllm', not derived "
        "'my_llm_primary_key'"
    )


def test_get_key_fallback_branch_success(monkeypatch):
    """_get_key() MUST fall back to backend.get(env_vars[0]) for backends without
    resolve_with_spec (alternate Protocol-only implementations).

    Covers _get_key's fallback branch: when hasattr(backend, 'resolve_with_spec')
    is False, _get_key calls backend.get(env_vars[0]) and returns its value.
    """

    class _ProtocolOnlyBackend:
        """Stub that satisfies the public SecretBackend Protocol surface only.
        Deliberately omits resolve_with_spec to exercise the fallback branch.
        """

        backend_id = "protocol-only-stub"

        def get(self, key: str) -> str:
            return "fallback-value"

        def get_optional(self, key: str) -> str | None:
            return "fallback-value"

        def has(self, key: str) -> bool:
            return True

        def locate(self, key: str):
            return None

        @property
        def capabilities(self):
            from atomic_agents.secret_backend.types import SecretCapabilities

            return SecretCapabilities(
                supports_rotation=False,
                supports_audit_logging=False,
                persists_plaintext=False,
            )

    stub = _ProtocolOnlyBackend()

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=stub,
    ):
        result = _get_key(
            env_vars=["PRIMARY_ENV_VAR", "SECONDARY_ENV_VAR"],
            keychain_name="my-keychain",
            config_key="mykey",
        )

    assert result == "fallback-value", (
        "fallback branch must return value from backend.get(env_vars[0])"
    )


def test_get_key_fallback_branch_secret_not_found_wraps_as_atomic_agents_error():
    """_get_key() fallback branch wraps SecretError as AtomicAgentsError.

    When the Protocol-only backend's get() raises SecretNotFound (a SecretError
    subclass), _get_key must re-raise as AtomicAgentsError chained from the
    SecretError, so callers see the same exception type as the resolve_with_spec
    path.
    """

    class _ProtocolOnlyFailingBackend:
        backend_id = "protocol-only-failing-stub"

        def get(self, key: str) -> str:
            raise SecretNotFound(f"No secret for '{key}'.")

        def get_optional(self, key: str) -> str | None:
            return None

        def has(self, key: str) -> bool:
            return False

        def locate(self, key: str):
            return None

        @property
        def capabilities(self):
            from atomic_agents.secret_backend.types import SecretCapabilities

            return SecretCapabilities(
                supports_rotation=False,
                supports_audit_logging=False,
                persists_plaintext=False,
            )

    stub = _ProtocolOnlyFailingBackend()

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=stub,
    ):
        with pytest.raises(AtomicAgentsError) as exc_info:
            _get_key(
                env_vars=["SOME_PRIMARY_KEY", "SOME_SECONDARY_KEY"],
                keychain_name="my-keychain",
                config_key="somekey",
            )

    # Must be chained from the underlying SecretError
    assert exc_info.value.__cause__ is not None, (
        "fallback branch must chain the SecretError via 'raise ... from exc'"
    )
    assert isinstance(exc_info.value.__cause__, SecretError), (
        "chained cause must be a SecretError subclass"
    )


def test_get_key_fallback_branch_wraps_value_error_as_atomic_agents_error():
    """_get_key() fallback branch wraps a charset ValueError as AtomicAgentsError.

    A Protocol-only backend's get() enforces the [A-Z0-9_]+ charset boundary
    (MUST 1) and raises ValueError for a custom KeySpec whose env_vars[0] is
    lowercase/non-POSIX. The fallback path MUST honor the documented
    "_get_key always raises AtomicAgentsError" contract and wrap that
    ValueError (chained), not leak a bare ValueError to callers. The
    resolve_with_spec path skips charset validation, so this divergence only
    bites Protocol-only backends — but the exception TYPE contract must hold
    for both.
    """

    class _ProtocolOnlyStrictBackend:
        """Stub whose get() validates the charset like the real Protocol boundary."""

        backend_id = "protocol-only-strict-stub"

        def get(self, key: str) -> str:
            from atomic_agents.secret_backend.backend import _validate_key

            _validate_key(key)  # raises ValueError for a lowercase key
            return "unreachable"

        def get_optional(self, key: str) -> str | None:
            return None

        def has(self, key: str) -> bool:
            return False

        def locate(self, key: str):
            return None

        @property
        def capabilities(self):
            from atomic_agents.secret_backend.types import SecretCapabilities

            return SecretCapabilities(
                supports_rotation=False,
                supports_audit_logging=False,
                persists_plaintext=False,
            )

    stub = _ProtocolOnlyStrictBackend()

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=stub,
    ):
        with pytest.raises(AtomicAgentsError) as exc_info:
            _get_key(
                env_vars=["my_llm_primary_key"],  # lowercase → ValueError in get()
                keychain_name="my-llm",
                config_key="myllm",
            )

    # Must NOT be a bare ValueError; must be AtomicAgentsError chained from it.
    assert isinstance(exc_info.value.__cause__, ValueError), (
        "fallback branch must chain the ValueError via 'raise ... from exc'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Factory and registry


def test_get_default_secret_backend_returns_filesystem():
    """Default (no env var) returns FilesystemSecretBackend."""
    with patch.dict("os.environ", {}, clear=False):
        # Remove override if set
        import os

        env = {
            k: v for k, v in os.environ.items() if k != "ATOMIC_AGENTS_SECRET_BACKEND"
        }
        with patch.dict("os.environ", env, clear=True):
            backend = get_default_secret_backend()
    assert isinstance(backend, FilesystemSecretBackend)


def test_get_default_secret_backend_gcp_no_url_raises():
    """ATOMIC_AGENTS_SECRET_BACKEND=gcp without URL raises SecretBackendNotRegistered."""
    with patch.dict("os.environ", {"ATOMIC_AGENTS_SECRET_BACKEND": "gcp"}, clear=False):
        import os

        env = {
            k: v
            for k, v in os.environ.items()
            if k != "ATOMIC_AGENTS_SECRET_BACKEND_URL"
        }
        env["ATOMIC_AGENTS_SECRET_BACKEND"] = "gcp"
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SecretBackendNotRegistered, match="URL"):
                get_default_secret_backend()


def test_get_default_secret_backend_gcp_with_url_raises_when_sdk_absent():
    """ATOMIC_AGENTS_SECRET_BACKEND=gcp with URL raises when SDK is absent.

    In CI the [gcp] extra IS installed (google-cloud-secret-manager in dev deps),
    so we simulate SDK absence the realistic way: the gcp module imports fine (it
    does NOT import the SDK at module level), and the missing-SDK ImportError fires
    from GCPSecretManagerBackend.__init__. The factory MUST then raise
    SecretBackendNotRegistered (not a raw ImportError) naming the [gcp] extra
    (fail-closed contract; MEMORY.md feedback_fail_closed_catches_base_error_class).
    The assertion is tightened to SecretBackendNotRegistered ONLY so a raw-ImportError
    regression fails the test.
    """
    from tests._gcp_sdk_blocker import block_gcp_sdk

    with block_gcp_sdk():
        with patch.dict(
            "os.environ",
            {
                "ATOMIC_AGENTS_SECRET_BACKEND": "gcp",
                "ATOMIC_AGENTS_SECRET_BACKEND_URL": "projects/myproject/secrets",
            },
        ):
            with pytest.raises(SecretBackendNotRegistered, match=r"\[gcp\] extra"):
                get_default_secret_backend()


def test_get_default_secret_backend_unknown_raises():
    """Unknown backend_id raises SecretBackendNotRegistered."""
    with patch.dict("os.environ", {"ATOMIC_AGENTS_SECRET_BACKEND": "unknown-backend"}):
        with pytest.raises(SecretBackendNotRegistered):
            get_default_secret_backend()


def test_filesystem_registered_at_import():
    """'filesystem' is registered at import time."""
    assert "filesystem" in list_secret_backends()


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem-specific Protocol surface (moved from conformance file to avoid
# GCP breakage when params=["filesystem","gcp"] is extended in PR 2)


def test_keys_json_path_is_machine_scoped():
    """_KEYS_JSON_PATH MUST resolve under the machine home directory, never
    under any vault or agent root (spec/38 MUST 2: vault portability would
    carry credentials).

    Filesystem-specific: ``_KEYS_JSON_PATH`` is an internal constant of the
    filesystem backend; alternate backends (GCP) have no such path, so this
    MUST-2 assertion lives here, not in the parametrized conformance file.
    """
    from pathlib import Path
    from atomic_agents.secret_backend import filesystem as _fs_module

    keys_path = _fs_module._KEYS_JSON_PATH
    home = Path.home()
    assert str(keys_path).startswith(str(home)), (
        f"_KEYS_JSON_PATH {keys_path!r} is not under the machine home "
        f"dir {home!r}. Credentials must be machine-scoped, not vault-relative."
    )


def test_keys_json_path_not_relative_to_cwd():
    """_KEYS_JSON_PATH must be absolute — never derived from cwd or a vault
    root (spec/38 MUST 2).
    """
    from atomic_agents.secret_backend import filesystem as _fs_module

    keys_path = _fs_module._KEYS_JSON_PATH
    assert keys_path.is_absolute(), (
        f"_KEYS_JSON_PATH {keys_path!r} must be absolute so it cannot be "
        f"confused with a vault-relative path."
    )


def test_keys_json_path_uses_expanduser():
    """_KEYS_JSON_PATH must expand the home dir via Path.home() or equivalent,
    not use a literal path (spec/38 MUST 2 — machine-scoped, portable).
    """
    from pathlib import Path
    from atomic_agents.secret_backend import filesystem as _fs_module

    keys_path = _fs_module._KEYS_JSON_PATH
    home_str = str(Path.home())
    assert str(keys_path).startswith(home_str), (
        f"_KEYS_JSON_PATH {keys_path!r} must begin with the expanded home "
        f"dir ({home_str!r}), not a literal '~' or relative path."
    )


def test_locate_source_label_uses_env_prefix(monkeypatch):
    """FilesystemSecretBackend.locate() labels an env-var hit ``env:<NAME>``.

    Filesystem-specific: the ``env:``/``keychain:``/``config:`` source-label
    scheme is a filesystem-backend convention. The backend-agnostic contract
    (source never contains the value) is asserted in the conformance file.
    """
    backend = FilesystemSecretBackend()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-some-value")
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert ref.source.startswith("env:")
    assert "ANTHROPIC_API_KEY" in ref.source


def test_filesystem_backend_id_is_filesystem():
    """FilesystemSecretBackend.backend_id MUST equal 'filesystem'."""
    backend = FilesystemSecretBackend()
    assert backend.backend_id == "filesystem"


def test_filesystem_capabilities_values():
    """FilesystemSecretBackend reports expected capability values."""
    backend = FilesystemSecretBackend()
    caps = backend.capabilities
    assert caps.supports_rotation is True  # each get() re-resolves from live sources
    assert caps.supports_audit_logging is False  # no durable audit trail
    assert caps.persists_plaintext is False  # machine-scoped sources only, see below


def test_resolve_with_spec_does_not_accept_agent_root():
    """resolve_with_spec must not accept an agent_root parameter — all
    resolution sources are machine-scoped (spec/38 MUST 2).

    Filesystem-specific test: resolve_with_spec is an INTERNAL helper on
    FilesystemSecretBackend, not part of the SecretBackend Protocol.  It is
    NOT exposed by GCP or other alternate backends, so this test belongs here,
    not in the parametrized conformance file.
    """
    import inspect

    backend = FilesystemSecretBackend()
    sig = inspect.signature(backend.resolve_with_spec)
    param_names = [p for p in sig.parameters if p != "self"]
    assert "agent_root" not in param_names, (
        "resolve_with_spec must not accept an agent_root parameter — "
        "all resolution sources are machine-scoped (MUST 2)."
    )
    assert param_names == ["env_vars", "keychain_name", "config_key"], (
        f"resolve_with_spec signature changed: {param_names}. "
        f"MUST 2 requires no vault-relative source parameters."
    )


def test_resolve_with_spec_accepts_non_posix_env_var_name():
    """resolve_with_spec intentionally does NOT validate the POSIX charset for
    env_var names — it preserves the pre-refactor _get_key contract for custom
    KeySpec names that may use lowercase or dotted names.

    This diverges from backend.get(), which calls _validate_key() and raises
    ValueError for the same input.  The asymmetry is intentional:
    resolve_with_spec is the raw triple-forwarding path for legacy callers;
    get() is the strict public-Protocol boundary.

    This test pins the divergence as deliberate.  If normalisation is ever
    applied to resolve_with_spec, this test must be updated and the change
    must be documented as a breaking spec/38 amendment.
    """
    from atomic_agents.secret_backend.backend import _validate_key
    from atomic_agents.secret_backend import SecretNotFound

    backend = FilesystemSecretBackend()

    # Confirm that get() rejects the non-POSIX name via _validate_key.
    with pytest.raises(ValueError):
        _validate_key("my_lowercase_var")  # lowercase is invalid per MUST 1

    # resolve_with_spec must NOT raise ValueError for the same name.
    # It may raise SecretNotFound (key absent), but not ValueError.
    try:
        result = backend.resolve_with_spec(
            env_vars=["my_lowercase_var"],
            keychain_name="unused",
            config_key="unused",
        )
        # Key absent → returns None; that's fine.
        assert result is None or isinstance(result, str)
    except SecretNotFound:
        pass  # absent is fine
    except ValueError as exc:
        pytest.fail(
            f"resolve_with_spec raised ValueError for a non-POSIX env-var name, "
            f"but this divergence from get() is intentional: {exc}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Doctor check wiring


def test_check_secret_backend_pass(monkeypatch):
    """check_secret_backend() returns PASS when backend instantiates cleanly."""
    from atomic_agents.doctor import PASS, check_secret_backend

    monkeypatch.delenv("ATOMIC_AGENTS_SECRET_BACKEND", raising=False)
    result = check_secret_backend()
    assert result.status == PASS
    assert result.name == "secret-backend"
    assert "filesystem" in result.message


def test_check_secret_backend_fail_on_bad_backend(monkeypatch):
    """check_secret_backend() returns FAIL when backend instantiation fails."""
    from atomic_agents.doctor import FAIL, check_secret_backend

    monkeypatch.setenv("ATOMIC_AGENTS_SECRET_BACKEND", "completely-unknown-xyz")
    result = check_secret_backend()
    assert result.status == FAIL
    assert result.name == "secret-backend"


def test_check_secret_backend_in_skip_list():
    """'secret-backend' appears in the no-agent SKIP enumeration."""
    from atomic_agents.doctor import run_doctor

    results = run_doctor(agents_root=None, agent_name=None)
    names = [r.name for r in results]
    assert "secret-backend" in names
    skip_result = next(r for r in results if r.name == "secret-backend")
    from atomic_agents.doctor import SKIP

    assert skip_result.status == SKIP


# ─────────────────────────────────────────────────────────────────────────────
# Eval.py _provider_available rewire


def test_provider_available_routes_through_backend(monkeypatch):
    """_provider_available() uses SecretBackend.has(), not inline os.environ."""
    from atomic_agents.eval import _provider_available

    mock_backend = MagicMock()
    mock_backend.has.return_value = True

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=mock_backend,
    ):
        result = _provider_available("claude-3-5-sonnet-20241022")

    assert result is True
    mock_backend.has.assert_called_once_with("ANTHROPIC_API_KEY")


def test_provider_available_false_for_unknown_model(monkeypatch):
    """_provider_available() returns False for unknown model prefixes."""
    from atomic_agents.eval import _provider_available

    assert _provider_available("some-unknown-model") is False

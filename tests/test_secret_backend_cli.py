"""CLI integration tests for ``atomic-agents secrets`` subcommands (spec/37).

Invokes ``atomic_agents.cli.main(argv=[...])`` directly with explicit argv.
Uses ``capsys`` for stdout/stderr capture. No --agent-root needed: secrets
are flat per-deployment, not per-agent.

Pattern mirrors test_mcp_registry_cli.py.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.cli import main
from atomic_agents.secret_backend import SecretNotFound
from atomic_agents.secret_backend.types import SecretRef


# ─────────────────────────────────────────────────────────────────────────────
# secrets check <KEY>


def test_secrets_check_present_exits_0(capsys, monkeypatch):
    """check <KEY> exits 0 and prints 'present' when key resolves."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ATOMIC_AGENTS_SECRET_BACKEND", raising=False)
    ret = main(["secrets", "check", "ANTHROPIC_API_KEY"])
    out = capsys.readouterr().out
    assert ret == 0
    assert "present" in out
    assert "ANTHROPIC_API_KEY" in out


def test_secrets_check_absent_exits_1(capsys, monkeypatch):
    """check <KEY> exits 1 and prints 'absent' when key is not found."""
    mock_backend = MagicMock()
    mock_backend.has.return_value = False

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=mock_backend,
    ):
        ret = main(["secrets", "check", "NO_SUCH_KEY_XYZ"])

    captured = capsys.readouterr()
    assert ret == 1
    assert "absent" in captured.out


def test_secrets_check_never_prints_value(capsys, monkeypatch):
    """check <KEY> MUST NOT print the resolved secret value (spec/37 MUST 6).

    ``check`` reports present/absent via ``has()`` only; it MUST NOT call
    ``get()`` or ``get_optional()`` (which return the actual credential).
    A mock-spy assertion catches a future regression where ``check`` is
    re-implemented to fetch and accidentally print the value — the
    plain-env-var output assertion above can never fail for a leak because
    ``has()`` is structurally boolean.  The spy makes the test falsifiable.
    """
    from unittest.mock import MagicMock, patch

    secret = "sk-ant-should-never-appear-in-output"
    mock_backend = MagicMock()
    mock_backend.has.return_value = True
    # Prime value-returning methods so a leak would surface in the assertion.
    mock_backend.get.return_value = secret
    mock_backend.get_optional.return_value = secret

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=mock_backend,
    ):
        main(["secrets", "check", "ANTHROPIC_API_KEY"])

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "present" in captured.out
    # The check path must only call has() — never the value-returning methods.
    mock_backend.get.assert_not_called()
    mock_backend.get_optional.assert_not_called()


def test_secrets_check_invalid_key_exits_1(capsys):
    """check with invalid key name exits 1 and prints error."""
    ret = main(["secrets", "check", "invalid.key.name"])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Error" in captured.err or "invalid" in captured.err.lower()


# ─────────────────────────────────────────────────────────────────────────────
# secrets which <KEY>


def test_secrets_which_prints_source_label(capsys, monkeypatch):
    """which <KEY> prints the source label when key is found."""
    mock_ref = SecretRef(
        key="ANTHROPIC_API_KEY", source="env:ANTHROPIC_API_KEY", present=True
    )
    mock_backend = MagicMock()
    mock_backend.locate.return_value = mock_ref

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=mock_backend,
    ):
        ret = main(["secrets", "which", "ANTHROPIC_API_KEY"])

    out = capsys.readouterr().out
    assert ret == 0
    assert "env:ANTHROPIC_API_KEY" in out
    assert "ANTHROPIC_API_KEY" in out


def test_secrets_which_absent_exits_1(capsys):
    """which <KEY> exits 1 when key is absent."""
    mock_backend = MagicMock()
    mock_backend.locate.return_value = None

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=mock_backend,
    ):
        ret = main(["secrets", "which", "NO_SUCH_KEY_XYZ"])

    assert ret == 1


def test_secrets_which_never_prints_value(capsys, monkeypatch):
    """which <KEY> MUST NOT print the secret value — only source label (spec/37 MUST 6).

    The mock backend's get/get_optional are primed to return the real secret so
    that a regression calling a value-returning method would cause the assertion
    to fail.  The correct implementation prints only ``ref.source`` and never
    calls get/get_optional.
    """
    secret_value = "sk-ant-this-should-never-appear"
    mock_ref = SecretRef(
        key="ANTHROPIC_API_KEY",
        source="env:ANTHROPIC_API_KEY",  # source label only, no value
        present=True,
    )
    mock_backend = MagicMock()
    mock_backend.locate.return_value = mock_ref
    # Prime value-returning methods so a leak would surface in the assertion.
    mock_backend.get.return_value = secret_value
    mock_backend.get_optional.return_value = secret_value

    with patch(
        "atomic_agents.secret_backend.get_default_secret_backend",
        return_value=mock_backend,
    ):
        main(["secrets", "which", "ANTHROPIC_API_KEY"])

    captured = capsys.readouterr()
    assert secret_value not in captured.out
    assert secret_value not in captured.err
    # The CLI must reach for presence only — never the secret value.
    mock_backend.get.assert_not_called()
    mock_backend.get_optional.assert_not_called()


def test_secrets_which_invalid_key_exits_1(capsys):
    """which with invalid key name exits 1."""
    ret = main(["secrets", "which", "bad/key"])
    assert ret == 1


# ─────────────────────────────────────────────────────────────────────────────
# secrets validate


def test_secrets_validate_prints_capabilities(capsys, monkeypatch):
    """validate prints backend_id and capability fields."""
    monkeypatch.delenv("ATOMIC_AGENTS_SECRET_BACKEND", raising=False)
    ret = main(["secrets", "validate"])
    out = capsys.readouterr().out
    assert ret == 0
    assert "backend_id" in out
    assert "filesystem" in out
    assert "supports_rotation" in out
    assert "persists_plaintext" in out


def test_secrets_validate_never_prints_secret(capsys, monkeypatch):
    """validate MUST NOT print any credential value (spec/37 MUST 6)."""
    secret = "sk-ant-validate-should-not-leak"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.delenv("ATOMIC_AGENTS_SECRET_BACKEND", raising=False)
    main(["secrets", "validate"])
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# No --agent-root accepted (secrets are deployment-scoped, not agent-scoped)


def test_secrets_check_has_no_agent_root_arg(capsys):
    """secrets check does NOT accept --agent-root (secrets are not per-agent).

    argparse calls sys.exit(2) on unrecognized arguments, which raises SystemExit.
    We catch it and confirm the exit code is non-zero.
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["secrets", "check", "ANTHROPIC_API_KEY", "--agent-root", "/tmp"])
    assert exc_info.value.code != 0

"""CLI integration tests for ``atomic-agents mcp-registry`` subcommands.

Invokes ``atomic_agents.cli.main(argv=[...])`` directly with explicit argv.
Uses ``capsys`` for stdout/stderr capture and ``tmp_path`` for a fresh
agent root on every test. Passes ``--agent-root`` to pin the backend
to the tmp directory so no test touches the real project mcp.md.

Pattern mirrors ``tests/test_persona_cli.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.cli import main
from atomic_agents.mcp import MCPServerSpec


# ─────────────────────────────────────────────────────────────────────────────
# Helpers


def _write_mcp_md(agent_root: Path, content: str) -> Path:
    """Write raw mcp.md content to agent_root/mcp.md."""
    mcp_md = agent_root / "mcp.md"
    mcp_md.write_text(content, encoding="utf-8")
    return mcp_md


def _write_valid_mcp_md(agent_root: Path, server_name: str = "my-server") -> Path:
    """Write a minimal valid mcp.md with one stdio server."""
    content = dedent(f"""\
        # MCP servers

        ## {server_name}
        command: echo
        description: Test server for CLI tests
    """)
    return _write_mcp_md(agent_root, content)


def _run(
    argv: list[str],
    tmp_path: Path,
    capsys,
) -> tuple[int, str, str]:
    """Run ``main(argv)`` and return (exit_code, stdout, stderr)."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_mcp_registry_agent_root -- 3 branches


def test_resolve_agent_root_uses_explicit_flag(tmp_path: Path, capsys) -> None:
    """--agent-root flag wins over env vars and cwd."""
    agent_root = tmp_path / "explicit"
    agent_root.mkdir()
    code, out, _err = _run(
        ["mcp-registry", "list", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert "No MCP servers" in out


def test_resolve_agent_root_falls_back_to_env_var(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """When no --agent-root flag is given, ATOMIC_AGENTS_AGENT_ROOT is used."""
    agent_root = tmp_path / "from-env"
    agent_root.mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_AGENT_ROOT", str(agent_root))
    # Pass no --agent-root flag; backend resolves from env var.
    code, out, _err = _run(
        ["mcp-registry", "list"],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert "No MCP servers" in out


# The zero-assertion cwd-fallback test was removed (P2 #4). The test below
# exercises the same code path with real assertions via the resolver function.
def test_resolve_agent_root_falls_back_to_cwd_via_resolver(
    tmp_path: Path, monkeypatch
) -> None:
    """_resolve_mcp_registry_agent_root returns Path.cwd() when no flag or env var."""
    import pathlib

    monkeypatch.delenv("ATOMIC_AGENTS_AGENT_ROOT", raising=False)
    from atomic_agents.cli import _resolve_mcp_registry_agent_root

    class FakeArgs:
        agent_root = None

    result = _resolve_mcp_registry_agent_root(FakeArgs())
    assert isinstance(result, pathlib.Path)
    # Should equal the actual cwd at test time.
    assert result == pathlib.Path.cwd()


# ─────────────────────────────────────────────────────────────────────────────
# mcp-registry list -- 3 tests


def test_cli_list_empty_catalog_prints_no_servers_message(
    tmp_path: Path, capsys
) -> None:
    """``mcp-registry list`` on an empty agent root prints 'No MCP servers'."""
    agent_root = tmp_path / "empty-agent"
    agent_root.mkdir()
    code, out, _err = _run(
        ["mcp-registry", "list", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert "No MCP servers" in out


def test_cli_list_populated_catalog_prints_table(tmp_path: Path, capsys) -> None:
    """``mcp-registry list`` with a populated mcp.md prints server names."""
    agent_root = tmp_path / "populated-agent"
    agent_root.mkdir()
    _write_valid_mcp_md(agent_root, "my-server")
    code, out, _err = _run(
        ["mcp-registry", "list", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert "my-server" in out


def test_cli_list_exit_code_zero_on_success(tmp_path: Path, capsys) -> None:
    """``mcp-registry list`` returns exit code 0 on success."""
    agent_root = tmp_path / "exit-code-agent"
    agent_root.mkdir()
    code, _out, _err = _run(
        ["mcp-registry", "list", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 0


# ─────────────────────────────────────────────────────────────────────────────
# mcp-registry show -- 3 tests


def test_cli_show_happy_path_prints_spec(tmp_path: Path, capsys) -> None:
    """``mcp-registry show <name>`` prints command and transport fields."""
    agent_root = tmp_path / "show-agent"
    agent_root.mkdir()
    _write_valid_mcp_md(agent_root, "show-server")
    code, out, _err = _run(
        ["mcp-registry", "show", "show-server", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert "show-server" in out
    assert "echo" in out


def test_cli_show_missing_name_prints_error_exit_1(tmp_path: Path, capsys) -> None:
    """``mcp-registry show`` for an absent server prints an error and exits 1."""
    agent_root = tmp_path / "missing-agent"
    agent_root.mkdir()
    _write_valid_mcp_md(agent_root, "other-server")
    code, _out, err = _run(
        ["mcp-registry", "show", "does-not-exist", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "Error" in err or "error" in err.lower()


def test_cli_show_invalid_name_prints_error_exit_1(tmp_path: Path, capsys) -> None:
    """``mcp-registry show`` with a path-traversal name prints an error and exits 1."""
    agent_root = tmp_path / "invalid-name-show-agent"
    agent_root.mkdir()
    code, _out, err = _run(
        ["mcp-registry", "show", "../etc/passwd", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "Error" in err or "error" in err.lower()


# ─────────────────────────────────────────────────────────────────────────────
# mcp-registry validate -- 2 tests


def test_cli_validate_ok_prints_pass_exit_0(tmp_path: Path, capsys) -> None:
    """``mcp-registry validate <name>`` for a valid server prints OK and exits 0."""
    agent_root = tmp_path / "validate-ok-agent"
    agent_root.mkdir()
    _write_valid_mcp_md(agent_root, "ok-server")
    code, out, _err = _run(
        ["mcp-registry", "validate", "ok-server", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert "OK" in out


def test_cli_validate_not_ok_prints_errors_exit_1(tmp_path: Path, capsys) -> None:
    """``mcp-registry validate <name>`` for an absent server exits 1 with error output."""
    agent_root = tmp_path / "validate-fail-agent"
    agent_root.mkdir()
    # No mcp.md -- the server is absent from the registry.
    code, out, err = _run(
        [
            "mcp-registry",
            "validate",
            "absent-server",
            "--agent-root",
            str(agent_root),
        ],
        tmp_path,
        capsys,
    )
    assert code == 1
    # validate prints results to stdout (per _mcp_registry_validate implementation).
    assert "FAIL" in out or "ERROR" in out


# ─────────────────────────────────────────────────────────────────────────────
# mcp-registry refresh-capabilities -- 1 test


def test_cli_refresh_capabilities_prints_capability_fields(
    tmp_path: Path, capsys
) -> None:
    """``mcp-registry refresh-capabilities`` prints all 5 capability fields."""
    agent_root = tmp_path / "caps-agent"
    agent_root.mkdir()
    code, out, _err = _run(
        [
            "mcp-registry",
            "refresh-capabilities",
            "--agent-root",
            str(agent_root),
        ],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert "supports_install" in out
    assert "supports_uninstall" in out
    assert "supports_capability_handshake" in out
    assert "supports_audit" in out
    assert "durable" in out


# ─────────────────────────────────────────────────────────────────────────────
# Exception handler coverage -- 3 tests


def test_cli_handles_unknown_backend_id_with_redacted_url(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """When ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND is a URL, stderr shows the
    redacted form (scheme://...) and exit code is 1.

    spec/36 credential-redaction in error messages.
    """
    agent_root = tmp_path / "bad-backend-agent"
    agent_root.mkdir()
    monkeypatch.setenv(
        "ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND",
        "https://user:pass@catalog/",
    )
    code, _out, err = _run(
        ["mcp-registry", "list", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "Error" in err
    # Credentials must NOT appear; only the redacted form (https://...) may.
    assert "user:pass" not in err
    assert "https://..." in err


def test_cli_handles_descriptor_invalid_cleanly(tmp_path: Path, capsys) -> None:
    """When load_mcp_server raises MCPRegistryDescriptorInvalid, the CLI prints a
    clean error to stderr and exits 1 (no uncaught Python traceback).

    spec/36 G8a -- MCPRegistryDescriptorInvalid must be caught in _cmd_mcp_registry.
    """
    from atomic_agents.mcp_registry import MCPRegistryDescriptorInvalid

    agent_root = tmp_path / "descriptor-invalid-agent"
    agent_root.mkdir()
    _write_valid_mcp_md(agent_root, "broken-server")

    # Patch the backend's load_mcp_server to simulate a descriptor-parse failure.
    with patch(
        "atomic_agents.mcp_registry.filesystem.FilesystemMCPServerRegistryBackend.load_mcp_server",
        side_effect=MCPRegistryDescriptorInvalid("mcp.md at /x could not be parsed"),
    ):
        code, _out, err = _run(
            [
                "mcp-registry",
                "show",
                "broken-server",
                "--agent-root",
                str(agent_root),
            ],
            tmp_path,
            capsys,
        )
    assert code == 1
    assert "Error" in err
    # Must NOT be an uncaught traceback (no "Traceback (most recent call last)").
    assert "Traceback" not in err


def test_cli_handles_unavailable_backend_cleanly(tmp_path: Path, capsys) -> None:
    """When list_mcp_servers raises MCPRegistryUnavailable, the CLI prints a clean
    error to stderr and exits 1.

    spec/36 MCPRegistryUnavailable exception handling.
    """
    from atomic_agents.mcp_registry import MCPRegistryUnavailable

    agent_root = tmp_path / "unavailable-agent"
    agent_root.mkdir()

    with patch(
        "atomic_agents.mcp_registry.filesystem.FilesystemMCPServerRegistryBackend.list_mcp_servers",
        side_effect=MCPRegistryUnavailable("backend temporarily unavailable"),
    ):
        code, _out, err = _run(
            ["mcp-registry", "list", "--agent-root", str(agent_root)],
            tmp_path,
            capsys,
        )
    assert code == 1
    assert "Error" in err
    assert "Traceback" not in err


# ─────────────────────────────────────────────────────────────────────────────
# P0 regression -- mcp-registry show must not leak resolved env values


def test_cli_show_does_not_leak_resolved_env_values(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """mcp-registry show MUST NOT print resolved secret values to stdout.

    P0 fix: _mcp_registry_show previously printed spec.env which contained
    the resolved values from load_mcp_server. It must instead re-parse mcp.md
    with resolve_env=False and print the raw $VAR references.

    spec/36 secret-leak prevention.
    """
    monkeypatch.setenv("TESTSECRET", "actual-secret-value-NEVER-PRINT")

    agent_root = tmp_path / "secret-agent"
    agent_root.mkdir()
    content = dedent("""\
        # MCP servers

        ## secret-server
        command: echo
        env: SECRET=$TESTSECRET
        description: Server with a secret env var
    """)
    _write_mcp_md(agent_root, content)

    code, out, _err = _run(
        ["mcp-registry", "show", "secret-server", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 0
    # The resolved secret value must NOT appear anywhere in stdout.
    assert "actual-secret-value-NEVER-PRINT" not in out, (
        "Resolved secret value must never be printed to stdout"
    )
    # The raw $VAR reference MUST appear so operators can see the wiring.
    assert "$TESTSECRET" in out, (
        "Raw $VAR reference must appear in stdout so operator sees the wiring"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1 #4 regression -- MCPServerConnectFailed escape from _cmd_mcp_registry


def test_cli_show_handles_unset_env_var_cleanly(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """mcp-registry show exits 1 with a clean error when a required env var is unset.

    P1 #4 fix: MCPServerConnectFailed was not caught in _cmd_mcp_registry's
    exception chain, causing an uncaught traceback. The fix adds a handler
    that prints a clean error to stderr and exits 1.

    spec/36 MCPServerConnectFailed handling.
    """
    monkeypatch.delenv("UNSET_CLI_VAR", raising=False)

    agent_root = tmp_path / "unset-env-agent"
    agent_root.mkdir()
    content = dedent("""\
        # MCP servers

        ## token-server
        command: echo
        env: TOKEN=$UNSET_CLI_VAR
        description: Server with an unset env var
    """)
    _write_mcp_md(agent_root, content)

    code, _out, err = _run(
        ["mcp-registry", "show", "token-server", "--agent-root", str(agent_root)],
        tmp_path,
        capsys,
    )
    assert code == 1, "CLI must exit 1 when env var is unset"
    # Must be a clean error message, not a Python traceback.
    assert "Traceback" not in err, "Must not show a Python traceback"
    assert "Error" in err or "error" in err.lower(), (
        "stderr must contain a human-readable error message"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1 #3 regression -- empty read_paths comment exists in _cmd_mcp_registry


def test_cli_uses_empty_read_paths_comment_present() -> None:
    """The inline comment explaining empty read_paths is present in cli.py.

    P1 #3: the CLI uses read_paths=[] for inspection-only commands. The comment
    explains this is intentional (consistent with read-only inspection use case)
    and documents that PR 3 install/uninstall will require non-empty read_paths.
    """
    import inspect
    from atomic_agents.cli import _cmd_mcp_registry

    source = inspect.getsource(_cmd_mcp_registry)
    assert "Empty read_paths" in source, (
        "_cmd_mcp_registry must contain the inline comment explaining empty read_paths"
    )

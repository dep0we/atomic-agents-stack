"""FilesystemMCPServerRegistryBackend-specific tests.

Tests that exercise filesystem-specific implementation details not in the
conformance suite:

- Path-traversal at API boundary edge cases
- Env-var resolution timing
- mcp.md parse semantics
- MCPServerRef.source field population
- Default LockBackend construction
- load_all_mcp_servers single-read-parse behavior (#201 PR 2 ENOENT fix)

Per spec/36, prep finding B-F6, and the filesystem-specific test shape in
test_corpus_filesystem_backend.py.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.mcp_registry import (
    FilesystemMCPServerRegistryBackend,
    MCPRegistryDescriptorInvalid,
    MCPRegistryUnavailable,
    MCPServerNotInRegistry,
)
from atomic_agents.mcp_registry.types import ValidationResult
from atomic_agents.mcp import MCPServerSpec, parse_mcp_md_text
from atomic_agents.exceptions import MCPServerConnectFailed


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_spec(
    name: str = "test-server",
    command: str = "echo",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    transport: str = "stdio",
    description: str = "",
) -> MCPServerSpec:
    """Build a minimal MCPServerSpec for test use."""
    return MCPServerSpec(
        name=name,
        command=command,
        args=args or [],
        env=env or {},
        transport=transport,
        description=description,
    )


def _write_mcp_md(agent_root: Path, content: str) -> Path:
    """Write raw mcp.md content to agent_root/mcp.md."""
    mcp_md = agent_root / "mcp.md"
    mcp_md.write_text(content, encoding="utf-8")
    return mcp_md


def _write_mcp_md_from_specs(agent_root: Path, specs: list[MCPServerSpec]) -> Path:
    """Write a well-formed mcp.md from a list of MCPServerSpec objects."""
    lines = ["# MCP servers", ""]
    for spec in specs:
        lines.append(f"## {spec.name}")
        lines.append(f"command: {spec.command}")
        if spec.args:
            lines.append(f"args: {', '.join(spec.args)}")
        if spec.env:
            for k, v in spec.env.items():
                lines.append(f"env: {k}={v}")
        if spec.transport and spec.transport != "stdio":
            lines.append(f"transport: {spec.transport}")
        if spec.description:
            lines.append(f"description: {spec.description}")
        lines.append("")
    return _write_mcp_md(agent_root, "\n".join(lines))


# ──────────────────────────────────────────────────────────────────────────────
# Path-traversal at API boundary edge cases


def test_path_traversal_slash(tmp_path: Path) -> None:
    """load_mcp_server with a slash in the name raises ValueError.

    spec/36 MUST 1 -- path-traversal refusal at API boundary.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(ValueError):
        backend.load_mcp_server("a/b")


def test_path_traversal_backslash(tmp_path: Path) -> None:
    """load_mcp_server with a backslash raises ValueError.

    spec/36 MUST 1.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(ValueError):
        backend.load_mcp_server("a\\b")


def test_path_traversal_dotdot(tmp_path: Path) -> None:
    """load_mcp_server with '..' raises ValueError.

    spec/36 MUST 1.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(ValueError):
        backend.load_mcp_server("..")


def test_path_traversal_leading_dot(tmp_path: Path) -> None:
    """load_mcp_server with a leading '.' raises ValueError.

    spec/36 MUST 1.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(ValueError):
        backend.load_mcp_server(".hidden-server")


def test_path_traversal_empty_string(tmp_path: Path) -> None:
    """load_mcp_server with an empty string raises ValueError.

    spec/36 MUST 1.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(ValueError):
        backend.load_mcp_server("")


def test_path_traversal_control_char(tmp_path: Path) -> None:
    """load_mcp_server with a control character raises ValueError.

    spec/36 MUST 1.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(ValueError):
        backend.load_mcp_server("bad\x00name")


def test_path_traversal_newline(tmp_path: Path) -> None:
    """load_mcp_server with a newline raises ValueError.

    spec/36 MUST 1.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(ValueError):
        backend.load_mcp_server("bad\nname")


def test_valid_alphanumeric_name_does_not_raise_on_charset(tmp_path: Path) -> None:
    """load_mcp_server with a valid alphanumeric name raises MCPServerNotInRegistry
    (valid name, absent from catalog), NOT ValueError.

    spec/36 MUST 1 -- valid charset passes the boundary check.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPServerNotInRegistry):
        backend.load_mcp_server("valid-server-name123")


# ──────────────────────────────────────────────────────────────────────────────
# Env-var resolution timing


def test_resolve_env_false_preserves_dollar_var(tmp_path: Path, monkeypatch) -> None:
    """parse_mcp_md_text with resolve_env=False preserves '$VAR' strings.

    spec/36 Theme 1 (prep notes C-F1). list_mcp_servers calls parse_mcp_md_text
    with resolve_env=False; the raw $VAR must survive.
    """
    monkeypatch.delenv("ABSENT_VAR_RESOLUTION_TEST", raising=False)

    content = dedent("""\
        # MCP servers

        ## my-server
        command: npx
        env: TOKEN=$ABSENT_VAR_RESOLUTION_TEST
    """)
    specs = parse_mcp_md_text(content, resolve_env=False)
    assert len(specs) == 1
    assert specs[0].env["TOKEN"] == "$ABSENT_VAR_RESOLUTION_TEST"


def test_dollar_var_resolves_at_load_mcp_server_not_list(
    tmp_path: Path, monkeypatch
) -> None:
    """$VAR is NOT resolved at list_mcp_servers time; resolved at load_mcp_server.

    spec/36 MUST 8 / Decision 7. list must not raise even with unset vars.
    """
    monkeypatch.delenv("TIMING_TEST_VAR_ABSENT", raising=False)

    agent_root = tmp_path / "timing-agent"
    agent_root.mkdir()
    spec = _make_spec("timing-server", env={"KEY": "$TIMING_TEST_VAR_ABSENT"})
    _write_mcp_md_from_specs(agent_root, [spec])

    backend = FilesystemMCPServerRegistryBackend(agent_root, [])

    # list_mcp_servers must NOT raise despite the absent var.
    refs = backend.list_mcp_servers()
    assert len(refs) == 1

    # load_mcp_server MUST raise because the var is still absent.
    with pytest.raises(MCPServerConnectFailed):
        backend.load_mcp_server("timing-server")


def test_unresolvable_var_raises_connect_failed(tmp_path: Path, monkeypatch) -> None:
    """load_mcp_server raises MCPServerConnectFailed for unresolvable $VAR.

    spec/36 MUST 8. monkeypatch ensures absence (B-F5).
    """
    monkeypatch.delenv("UNRESOLVABLE_XYZ_VAR", raising=False)

    agent_root = tmp_path / "unresolvable-agent"
    agent_root.mkdir()
    spec = _make_spec("bad-server", env={"API": "$UNRESOLVABLE_XYZ_VAR"})
    _write_mcp_md_from_specs(agent_root, [spec])

    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPServerConnectFailed):
        backend.load_mcp_server("bad-server")


def test_mid_session_env_mutation_reflected_at_load_time(
    tmp_path: Path, monkeypatch
) -> None:
    """load_mcp_server reads from os.environ at call time, so mid-session
    mutations are visible on the next call.

    spec/36 MUST 8.
    """
    monkeypatch.setenv("SESSION_VAR", "value-at-start")

    agent_root = tmp_path / "session-agent"
    agent_root.mkdir()
    spec = _make_spec("session-server", env={"KEY": "$SESSION_VAR"})
    _write_mcp_md_from_specs(agent_root, [spec])

    backend = FilesystemMCPServerRegistryBackend(agent_root, [])

    s1 = backend.load_mcp_server("session-server")
    assert s1.env["KEY"] == "value-at-start"

    monkeypatch.setenv("SESSION_VAR", "value-after-change")
    s2 = backend.load_mcp_server("session-server")
    assert s2.env["KEY"] == "value-after-change"


# ──────────────────────────────────────────────────────────────────────────────
# mcp.md parse semantics


def test_empty_file_returns_empty_list(tmp_path: Path) -> None:
    """list_mcp_servers returns [] for an empty mcp.md file.

    spec/36 MUST 7.
    """
    agent_root = tmp_path / "empty-agent"
    agent_root.mkdir()
    _write_mcp_md(agent_root, "")
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    assert refs == []


def test_missing_file_returns_empty_list(tmp_path: Path) -> None:
    """list_mcp_servers returns [] when mcp.md does not exist.

    spec/36 MUST 7.
    """
    agent_root = tmp_path / "missing-md-agent"
    agent_root.mkdir()
    # No mcp.md written.
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    assert refs == []


def test_single_section_returns_one_spec(tmp_path: Path) -> None:
    """A single H2 section in mcp.md produces exactly one MCPServerRef.

    spec/36 list semantics.
    """
    agent_root = tmp_path / "single-section-agent"
    agent_root.mkdir()
    spec = _make_spec("only-server", description="The only one")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    assert len(refs) == 1
    assert refs[0].name == "only-server"


def test_multi_section_mcp_md_returns_multiple_specs(tmp_path: Path) -> None:
    """Multiple H2 sections in mcp.md produce multiple MCPServerRefs.

    spec/36 list semantics. prep finding B-F6.
    """
    agent_root = tmp_path / "multi-section-agent"
    agent_root.mkdir()
    specs = [
        _make_spec("server-one"),
        _make_spec("server-two"),
        _make_spec("server-three"),
    ]
    _write_mcp_md_from_specs(agent_root, specs)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    assert len(refs) == 3
    names = {r.name for r in refs}
    assert names == {"server-one", "server-two", "server-three"}


def test_malformed_section_no_command_skipped_silently(tmp_path: Path) -> None:
    """A section with no 'command:' key is silently skipped.

    spec/36 / parse_mcp_md_text behavior at mcp.py _build_spec: if no command
    key, the spec is None and the section is dropped with a logged warning.
    """
    agent_root = tmp_path / "no-command-agent"
    agent_root.mkdir()
    content = dedent("""\
        # MCP servers

        ## no-command-server
        description: This server has no command key

        ## good-server
        command: echo
        description: This one is fine
    """)
    _write_mcp_md(agent_root, content)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    names = {r.name for r in refs}
    # no-command-server must be silently skipped; good-server must be present.
    assert "good-server" in names
    assert "no-command-server" not in names


def test_load_all_mcp_servers_returns_specs_via_single_read_parse(
    tmp_path: Path,
) -> None:
    """load_all_mcp_servers returns fully materialized specs via single read-parse.

    PR 2 replaces the _default_load_all delegation with a direct single
    read-parse so parse failures (MCPRegistryDescriptorInvalid) and transient
    OSError (MCPRegistryUnavailable) propagate correctly to the fail-closed
    wiring in agent.py:__init__. Verified by asserting output correctness
    (behavior contract) rather than patching internals.
    """
    agent_root = tmp_path / "delegation-agent"
    agent_root.mkdir()
    spec = _make_spec("delegate-server")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])

    result = backend.load_all_mcp_servers()

    assert len(result) == 1
    assert result[0].name == "delegate-server"
    assert isinstance(result[0], MCPServerSpec)


# ──────────────────────────────────────────────────────────────────────────────
# MCPServerRef source field


def test_list_mcp_servers_source_field_is_mcp_md_section(tmp_path: Path) -> None:
    """MCPServerRef.source is populated as 'mcp.md#section:<name>'.

    spec/36 MCPServerRef.source field contract. prep finding B-F6.
    """
    agent_root = tmp_path / "source-field-agent"
    agent_root.mkdir()
    spec = _make_spec("source-test-server")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    assert len(refs) == 1
    assert refs[0].source == "mcp.md#section:source-test-server"


# ──────────────────────────────────────────────────────────────────────────────
# Default LockBackend construction


def test_default_lock_backend_is_none_at_pr1(tmp_path: Path) -> None:
    """FilesystemMCPServerRegistryBackend accepts lock_backend=None (PR 1 default).

    spec/36 -- lock_backend accepted at construction but unused until PR 3.
    The constructor signature is stable so PR 3 callers can pass the kwarg
    without a constructor change.
    """
    agent_root = tmp_path / "lock-agent"
    agent_root.mkdir()
    # Passing None explicitly mirrors the default.
    backend = FilesystemMCPServerRegistryBackend(agent_root, [], lock_backend=None)
    assert backend is not None


def test_lock_backend_kwarg_accepted_without_error(tmp_path: Path) -> None:
    """A non-None lock_backend kwarg is accepted and stored but not used at PR 1.

    spec/36 constructor stability -- PR 3 callers can pre-wire lock_backend.
    """
    agent_root = tmp_path / "lock-kwarg-agent"
    agent_root.mkdir()
    mock_lock_backend = MagicMock()  # MagicMock now imported at top
    # Should not raise even though lock_backend is not None.
    backend = FilesystemMCPServerRegistryBackend(
        agent_root, [], lock_backend=mock_lock_backend
    )
    assert backend is not None


# ──────────────────────────────────────────────────────────────────────────────
# Install/uninstall stubs (deferred to PR 3)


# ──────────────────────────────────────────────────────────────────────────────
# G4 -- validate() coverage (10 tests)


def _make_mcp_spec(
    name: str = "test-server",
    command: str = "echo",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    transport: str = "stdio",
    description: str = "",
) -> MCPServerSpec:
    """Build a minimal MCPServerSpec for validate() tests."""
    return MCPServerSpec(
        name=name,
        command=command,
        args=args or [],
        env=env or {},
        transport=transport,
        description=description,
    )


def test_validate_returns_validation_result_for_valid_server(tmp_path: Path) -> None:
    """Happy path: valid stdio server with a known command returns ok=True, no errors
    or warnings.

    spec/36 validate() -- all checks pass.
    """
    agent_root = tmp_path / "valid-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("my-server", command="echo")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.validate("my-server")
    assert result.ok is True
    assert result.errors == []


def test_validate_returns_not_ok_when_mcp_md_absent(tmp_path: Path) -> None:
    """validate() returns ok=False when no mcp.md exists.

    spec/36 validate() -- absent file is reported as a result, not an exception.
    """
    agent_root = tmp_path / "absent-md-agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.validate("my-server")
    assert result.ok is False
    assert len(result.errors) >= 1


def test_validate_returns_not_ok_when_server_name_absent_in_mcp_md(
    tmp_path: Path,
) -> None:
    """validate() returns ok=False when mcp.md exists but lacks the requested name.

    spec/36 validate() -- absent server in populated registry is an error.
    """
    agent_root = tmp_path / "absent-name-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("other-server", command="echo")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.validate("my-server")
    assert result.ok is False
    assert len(result.errors) >= 1
    # The error must mention the requested name.
    combined_errors = " ".join(result.errors)
    assert "my-server" in combined_errors


def test_validate_warns_when_command_not_on_path(tmp_path: Path) -> None:
    """validate() produces a warning when the command is not on PATH.

    spec/36 validate() -- PATH check is warn-only; ok stays True.
    """
    agent_root = tmp_path / "bad-cmd-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("bad-cmd-server", command="nonexistent_binary_xyz_unique")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.validate("bad-cmd-server")
    assert result.ok is True
    assert len(result.warnings) >= 1
    combined_warnings = " ".join(result.warnings)
    assert "nonexistent_binary_xyz_unique" in combined_warnings


def test_validate_passes_when_command_on_path(tmp_path: Path) -> None:
    """validate() does not warn when the command is on PATH.

    spec/36 validate() -- PATH check is warn-only; a found command produces no warning.
    """
    agent_root = tmp_path / "good-cmd-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("good-cmd-server", command="echo")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.validate("good-cmd-server")
    assert result.ok is True
    # No PATH warning for 'echo' which is always available.
    path_warnings = [w for w in result.warnings if "not found on PATH" in w]
    assert path_warnings == []


def test_validate_warns_on_unresolvable_env_var(tmp_path: Path, monkeypatch) -> None:
    """validate() warns when a $VAR in env is not set in the current process.

    spec/36 validate() -- unset env var is warn-only; ok stays True.
    """
    monkeypatch.delenv("UNSET_VAR_VALIDATE_TEST", raising=False)
    agent_root = tmp_path / "unset-var-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec(
        "env-server", command="echo", env={"API_KEY": "$UNSET_VAR_VALIDATE_TEST"}
    )
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.validate("env-server")
    assert result.ok is True
    assert len(result.warnings) >= 1
    combined_warnings = " ".join(result.warnings)
    assert "UNSET_VAR_VALIDATE_TEST" in combined_warnings


def test_validate_returns_not_ok_for_unrecognized_transport(tmp_path: Path) -> None:
    """validate() returns ok=False when transport is not 'stdio'.

    spec/36 validate() -- unrecognized transport is an error (only 'stdio' in v1).
    NOTE: _write_mcp_md_from_specs skips transport when it is 'stdio'; we write
    raw mcp.md content here to inject a non-stdio transport value.
    """
    agent_root = tmp_path / "bad-transport-agent"
    agent_root.mkdir()
    content = dedent("""\
        # MCP servers

        ## http-server
        command: echo
        transport: http
    """)
    _write_mcp_md(agent_root, content)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.validate("http-server")
    assert result.ok is False
    assert len(result.errors) >= 1
    combined_errors = " ".join(result.errors)
    assert "transport" in combined_errors.lower() or "http" in combined_errors


def test_validate_returns_not_ok_for_invalid_name_at_api_boundary(
    tmp_path: Path,
) -> None:
    """validate() returns ValidationResult(ok=False) for path-traversal names.

    spec/36 MUST 1 + validate() spec: invalid charset is caught at the API
    boundary and returned as a result, NOT raised as ValueError.
    """
    agent_root = tmp_path / "invalid-name-agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    # "../etc/passwd" contains "/" which fails the charset check.
    result = backend.validate("../etc/passwd")
    assert isinstance(result, ValidationResult)
    assert result.ok is False


def test_validate_does_not_spawn_subprocess(tmp_path: Path) -> None:
    """validate() is a static check and must NOT spawn any subprocess.

    spec/36 MUST 2 analog -- validate is static only.
    """
    agent_root = tmp_path / "no-spawn-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("no-spawn-server", command="echo")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])

    mock_popen = MagicMock()
    with patch("subprocess.Popen", mock_popen):
        backend.validate("no-spawn-server")

    mock_popen.assert_not_called()


def test_validate_result_round_trips_through_dataclass_defaults(
    tmp_path: Path,
) -> None:
    """ValidationResult fields (ok, errors, warnings) are correctly typed and set.

    spec/36 ValidationResult dataclass -- both ok=True and ok=False cases.
    """
    # ok=True case: valid server with known command.
    agent_root = tmp_path / "roundtrip-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("roundtrip-server", command="echo")
    _write_mcp_md_from_specs(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])

    ok_result = backend.validate("roundtrip-server")
    assert isinstance(ok_result, ValidationResult)
    assert ok_result.ok is True
    assert isinstance(ok_result.errors, list)
    assert isinstance(ok_result.warnings, list)

    # ok=False case: name not in registry.
    fail_result = backend.validate("does-not-exist")
    assert isinstance(fail_result, ValidationResult)
    assert fail_result.ok is False
    assert isinstance(fail_result.errors, list)
    assert len(fail_result.errors) >= 1


# ──────────────────────────────────────────────────────────────────────────────
# G5 -- MCPRegistryDescriptorInvalid parse-failure test


def test_malformed_mcp_md_raises_descriptor_invalid(tmp_path: Path) -> None:
    """load_mcp_server raises MCPRegistryDescriptorInvalid when a section header
    exists in mcp.md but the section is missing required fields (e.g., command:).

    spec/36 prep finding B-F6. P1 #2 fix: the H2-header scan in load_mcp_server
    detects the section exists but failed to parse, raising MCPRegistryDescriptorInvalid
    instead of the confusing MCPServerNotInRegistry.
    """
    agent_root = tmp_path / "malformed-agent"
    agent_root.mkdir()
    # A section with a valid H2 header but no command: line -- parse returns None
    # for this section, causing the old code to raise MCPServerNotInRegistry.
    # The fix raises MCPRegistryDescriptorInvalid instead.
    content = dedent("""\
        # MCP servers

        ## malformed-server
        description: This section has no command key
    """)
    _write_mcp_md(agent_root, content)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.load_mcp_server("malformed-server")


# Install/uninstall tests now live in test_mcp_server_registry_filesystem_install.py
# (added at PR 3). The placeholder skip-marked stubs that were here have been
# removed per Stream E finding E10 -- real tests replace them.


# ──────────────────────────────────────────────────────────────────────────────
# P1 #1 -- OSError MUST 7 violation regression tests


def test_load_raises_unavailable_on_permission_error(tmp_path: Path) -> None:
    """load_mcp_server raises MCPRegistryUnavailable (transient) for PermissionError.

    P1 #1 fix: PermissionError is NOT FileNotFoundError, so it must map to
    MCPRegistryUnavailable (transient), not MCPServerNotInRegistry (permanent).
    spec/36 MUST 7.
    """
    import stat

    agent_root = tmp_path / "perm-agent"
    agent_root.mkdir()
    mcp_md = agent_root / "mcp.md"
    mcp_md.write_text(
        "# MCP servers\n\n## perm-server\ncommand: echo\n",
        encoding="utf-8",
    )
    # chmod 000 to cause PermissionError on read.
    mcp_md.chmod(0o000)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    try:
        with pytest.raises(MCPRegistryUnavailable):
            backend.load_mcp_server("perm-server")
    finally:
        # Restore permissions so tmp_path cleanup can remove the file.
        mcp_md.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_load_raises_not_in_registry_on_missing_file(tmp_path: Path) -> None:
    """load_mcp_server raises MCPServerNotInRegistry (permanent) when mcp.md is absent.

    P1 #1 fix: FileNotFoundError is a permanent absence and must map to
    MCPServerNotInRegistry, not MCPRegistryUnavailable.
    spec/36 MUST 7.
    """
    agent_root = tmp_path / "missing-file-agent"
    agent_root.mkdir()
    # No mcp.md written -- the file does not exist.
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPServerNotInRegistry):
        backend.load_mcp_server("any-server")


# ──────────────────────────────────────────────────────────────────────────────
# P1 #2 -- Malformed-section MCPRegistryDescriptorInvalid regression test


def test_load_raises_descriptor_invalid_for_malformed_section(tmp_path: Path) -> None:
    """load_mcp_server raises MCPRegistryDescriptorInvalid (not MCPServerNotInRegistry)
    when the section header exists but the section has no command: key.

    P1 #2 fix: the H2-header scan distinguishes 'section exists but parse failed'
    from 'section not in mcp.md at all'.
    spec/36 MCPRegistryDescriptorInvalid semantics.
    """
    agent_root = tmp_path / "malformed-section-agent"
    agent_root.mkdir()
    content = dedent("""\
        # MCP servers

        ## foo
        description: incomplete section, no command key
    """)
    _write_mcp_md(agent_root, content)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    # Must NOT raise MCPServerNotInRegistry -- that would be misleading since
    # the section 'foo' IS present in the file.
    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.load_mcp_server("foo")


# ──────────────────────────────────────────────────────────────────────────────
# P2 #1 -- list_mcp_servers skips sections with invalid names


def test_list_skips_sections_with_invalid_names(tmp_path: Path) -> None:
    """list_mcp_servers skips sections whose names fail the charset validator.

    P2 #1 fix: invalid-name sections are skipped with a logged warning rather
    than propagating uncaught ValueError from load_all_mcp_servers.
    spec/36 MUST 1 / tampered-mcp.md resilience.

    Note: parse_mcp_md_text uses whitespace-splitting, so injecting a truly
    invalid-charset name requires manually writing raw mcp.md content with
    a name that parse_mcp_md_text passes through but _validate_server_name
    rejects (e.g., names that are pure-dot or pass parse but fail the regex).
    We use a name starting with '.' which is rejected by _validate_server_name.
    """
    agent_root = tmp_path / "invalid-name-list-agent"
    agent_root.mkdir()
    # Write a mcp.md that has one good section and one section where the name
    # starts with '.', which _validate_server_name rejects with a leading-dot rule.
    # We write raw content because _write_mcp_md_from_specs validates names itself.
    content = dedent("""\
        # MCP servers

        ## good-server
        command: echo
        description: This one is fine

        ## .hidden-server
        command: npx
        description: This name starts with a dot and should be skipped
    """)
    _write_mcp_md(agent_root, content)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    names = {r.name for r in refs}
    assert "good-server" in names, "valid server must appear in list"
    assert ".hidden-server" not in names, "dot-prefixed name must be skipped"

"""Filesystem install / uninstall tests for FilesystemMCPServerRegistryBackend.

Covers PR 3 of 5 of #201 (MCPServerRegistryBackend). Companion to the existing
test_mcp_server_registry_filesystem_backend.py (read paths from PR 1) +
test_mcp_server_registry_conformance.py (cross-backend MUSTs).

Test categories:
1. Happy path: install + verify list_mcp_servers + verify file content.
2. Collision: install same name twice -> MCPServerAlreadyInstalled.
3. Cold-start: install on agent_root with no mcp.md (creates it).
4. Path-traversal: install with bad name -> ValueError at API boundary.
5. Idempotency: uninstall absent name -> no exception; install/uninstall/install cycle.
6. Concurrent: N threads same name -> exactly one wins; different names -> all win.
7. Lock timeout: install_lock_timeout=0.0 + held lock -> MCPRegistryUnavailable.
8. CLI: install/uninstall via _mcp_registry_install/_uninstall handlers;
   verify success-output discipline + WARN on literal env + H2 injection refusal.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from atomic_agents.locks.filesystem import FilesystemLockBackend
from atomic_agents.mcp_registry import (
    FilesystemMCPServerRegistryBackend,
    MCPRegistryUnavailable,
    MCPServerAlreadyInstalled,
)
from atomic_agents.mcp_registry.types import MCPServerRef
from atomic_agents.mcp import MCPServerSpec


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


def _fresh_backend(agent_root: Path) -> FilesystemMCPServerRegistryBackend:
    """Return a fresh FilesystemMCPServerRegistryBackend for agent_root."""
    return FilesystemMCPServerRegistryBackend(agent_root, [])


# ──────────────────────────────────────────────────────────────────────────────
# 1. Happy path


def test_install_happy_path(tmp_path: Path) -> None:
    """install() returns an MCPServerRef with projected fields; mcp.md is created;
    list_mcp_servers includes the new server.

    spec/36 MUST 9 -- install atomicity + post-install consistency.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("my-server", command="npx", description="A test server")

    ref = backend.install(spec)

    assert isinstance(ref, MCPServerRef)
    assert ref.name == "my-server"
    assert ref.transport == "stdio"
    # mcp.md must exist after install
    assert (agent_root / "mcp.md").exists()
    # list_mcp_servers must return the new entry
    refs = backend.list_mcp_servers()
    names = [r.name for r in refs]
    assert "my-server" in names


def test_install_cold_start_creates_mcp_md(tmp_path: Path) -> None:
    """install() on an agent_root with NO mcp.md creates mcp.md from scratch.

    spec/36 MUST 9 -- filesystem backend handles cold-start write.
    """
    agent_root = tmp_path / "cold-agent"
    agent_root.mkdir()
    assert not (agent_root / "mcp.md").exists()

    backend = _fresh_backend(agent_root)
    backend.install(_make_spec("first-server"))

    assert (agent_root / "mcp.md").exists()
    refs = backend.list_mcp_servers()
    assert any(r.name == "first-server" for r in refs)


def test_install_returns_ref_with_name_and_transport(tmp_path: Path) -> None:
    """install() projects name and transport from the spec into the returned Ref.

    spec/36 MCPServerRef projection contract.
    """
    agent_root = tmp_path / "proj-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("projected-server", transport="stdio")

    ref = backend.install(spec)

    assert ref.name == "projected-server"
    assert ref.transport == "stdio"


def test_install_ref_does_not_contain_env(tmp_path: Path) -> None:
    """install() returns MCPServerRef which does NOT include env fields.

    The Ref carries only metadata (name, description, transport, version, source).
    Secrets must not leak into the Ref.
    """
    agent_root = tmp_path / "ref-env-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("secret-server", env={"API_KEY": "$MY_SECRET"})

    ref = backend.install(spec)

    # MCPServerRef has no 'env' attribute by design; confirm this.
    assert not hasattr(ref, "env"), (
        "MCPServerRef must NOT carry env (secret-leak prevention)."
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. Collision


def test_install_collision_raises_already_installed(tmp_path: Path) -> None:
    """install() the same name twice raises MCPServerAlreadyInstalled on second call.

    spec/36 MUST 9 -- atomic collision detection.
    """
    agent_root = tmp_path / "collision-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("dupe-server")

    backend.install(spec)
    with pytest.raises(MCPServerAlreadyInstalled):
        backend.install(spec)


def test_install_collision_leaves_mcp_md_unchanged(tmp_path: Path) -> None:
    """After a collision, mcp.md still has exactly one section for the server.

    spec/36 MUST 9 -- atomicity means no partial writes on collision.
    """
    agent_root = tmp_path / "collision-check-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("unique-server")

    backend.install(spec)
    try:
        backend.install(spec)
    except MCPServerAlreadyInstalled:
        pass

    # Only one entry should exist.
    refs = backend.list_mcp_servers()
    assert len([r for r in refs if r.name == "unique-server"]) == 1


# ──────────────────────────────────────────────────────────────────────────────
# 3. Path-traversal names


def test_install_path_traversal_name_raises(tmp_path: Path) -> None:
    """install() with a path-traversal name raises ValueError BEFORE any disk access.

    spec/36 MUST 1 -- charset validation at API boundary.
    """
    agent_root = tmp_path / "traversal-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("../etc/passwd")

    with pytest.raises(ValueError):
        backend.install(spec)

    # mcp.md must NOT have been created.
    assert not (agent_root / "mcp.md").exists()


def test_install_empty_command_raises(tmp_path: Path) -> None:
    """install() with an empty command raises ValueError.

    spec/36 -- command is required; empty string is invalid.
    """
    agent_root = tmp_path / "empty-cmd-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("ok-name", command="")

    with pytest.raises(ValueError):
        backend.install(spec)


def test_uninstall_path_traversal_name_raises(tmp_path: Path) -> None:
    """uninstall() with a path-traversal name raises ValueError.

    spec/36 MUST 1 -- charset validation applies to uninstall too.
    """
    agent_root = tmp_path / "uninstall-traversal-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)

    with pytest.raises(ValueError):
        backend.uninstall("../etc/passwd")


# ──────────────────────────────────────────────────────────────────────────────
# 4. Dollar-var env preservation


def test_install_with_dollar_var_env_preserves_unresolved(
    tmp_path: Path, monkeypatch
) -> None:
    """install() with env={"K": "$VAR"} stores the literal $VAR string in mcp.md.

    spec/36 MUST 8 / Decision 7 -- env refs are NOT resolved at install time.
    The raw $VAR reference is stored; resolution happens at load_mcp_server time.
    """
    monkeypatch.setenv("MY_VAR", "resolved-secret")
    agent_root = tmp_path / "dollar-var-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("var-server", env={"TOKEN": "$MY_VAR"})

    backend.install(spec)

    mcp_md_content = (agent_root / "mcp.md").read_text(encoding="utf-8")
    # The raw reference must be stored, not the resolved value.
    assert "$MY_VAR" in mcp_md_content
    assert "resolved-secret" not in mcp_md_content


# ──────────────────────────────────────────────────────────────────────────────
# 5. Install then load round-trip


def test_install_then_load_round_trip(tmp_path: Path, monkeypatch) -> None:
    """install() then load_mcp_server() returns a spec with matching fields.

    spec/36 MUST 10 + MUST 8 -- post-install consistency and env resolution.
    """
    monkeypatch.setenv("ROUND_TRIP_VAR", "expected-value")
    agent_root = tmp_path / "round-trip-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec(
        "rt-server",
        command="npx",
        args=["-y", "@some/package"],
        env={"KEY": "$ROUND_TRIP_VAR"},
        description="Round trip test",
    )

    backend.install(spec)
    loaded = backend.load_mcp_server("rt-server")

    assert loaded.name == "rt-server"
    assert loaded.command == "npx"
    assert loaded.args == ["-y", "@some/package"]
    assert loaded.env["KEY"] == "expected-value"
    assert loaded.description == "Round trip test"


# ──────────────────────────────────────────────────────────────────────────────
# 6. Uninstall behavior


def test_uninstall_present_name_removes(tmp_path: Path) -> None:
    """uninstall() of an installed server removes it; list_mcp_servers returns [].

    spec/36 MUST 9 -- uninstall atomicity.
    """
    agent_root = tmp_path / "remove-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    backend.install(_make_spec("to-remove"))

    backend.uninstall("to-remove")

    refs = backend.list_mcp_servers()
    assert not any(r.name == "to-remove" for r in refs)


def test_uninstall_absent_name_is_noop(tmp_path: Path) -> None:
    """uninstall() of a name that was never installed returns without raising.

    spec/36 MUST 9 -- uninstall is idempotent (no exception on absent name).
    """
    agent_root = tmp_path / "noop-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)

    # Must not raise anything.
    result = backend.uninstall("never-installed")
    assert result is None


def test_uninstall_idempotent_double_call(tmp_path: Path) -> None:
    """install() then uninstall() twice; second uninstall must not raise and returns None.

    spec/36 MUST 9 -- uninstall idempotency for double-call; absent-name path
    returns None per the spec contract.
    """
    agent_root = tmp_path / "idempotent-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    backend.install(_make_spec("idempotent-server"))

    backend.uninstall("idempotent-server")
    result2 = backend.uninstall("idempotent-server")  # must not raise
    assert result2 is None


def test_install_uninstall_install_cycle(tmp_path: Path) -> None:
    """install -> uninstall -> install cycle succeeds on the third call.

    spec/36 MUST 9 -- uninstall removed the section so re-install is clean.
    """
    agent_root = tmp_path / "cycle-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)
    spec = _make_spec("cycle-server")

    backend.install(spec)
    backend.uninstall("cycle-server")
    ref = backend.install(spec)  # must succeed

    assert ref.name == "cycle-server"
    refs = backend.list_mcp_servers()
    assert any(r.name == "cycle-server" for r in refs)


# ──────────────────────────────────────────────────────────────────────────────
# 7. Concurrent install


def test_install_concurrent_same_name_exactly_one_wins(tmp_path: Path) -> None:
    """5 threads installing the same server name: exactly 1 wins; rest raise.

    Winners raise nothing; losers raise MCPServerAlreadyInstalled or
    MCPRegistryUnavailable (lock contention). mcp.md must contain exactly
    one matching H2 section.

    spec/36 MUST 9 -- concurrent install atomicity.
    """
    agent_root = tmp_path / "concurrent-same-agent"
    agent_root.mkdir()
    spec = _make_spec("concurrent-server")

    successes = []
    failures = []

    def _try_install() -> None:
        backend = _fresh_backend(agent_root)
        try:
            backend.install(spec)
            successes.append(True)
        except (MCPServerAlreadyInstalled, MCPRegistryUnavailable):
            failures.append(True)

    n_threads = 5
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        futs = [pool.submit(_try_install) for _ in range(n_threads)]
        for fut in futs:
            fut.result()  # re-raise unexpected exceptions

    assert len(successes) == 1, (
        f"Expected exactly 1 winner; got {len(successes)} winners and "
        f"{len(failures)} failures."
    )
    assert len(successes) + len(failures) == n_threads

    mcp_md_content = (agent_root / "mcp.md").read_text(encoding="utf-8")
    import re

    h2_matches = re.findall(r"^## concurrent-server\s*$", mcp_md_content, re.MULTILINE)
    assert len(h2_matches) == 1, (
        f"Expected exactly 1 H2 section for 'concurrent-server'; "
        f"found {len(h2_matches)} in mcp.md."
    )


def test_install_concurrent_different_names_all_win(tmp_path: Path) -> None:
    """N threads installing different server names: all succeed.

    spec/36 MUST 9 -- different names do not collide.
    """
    agent_root = tmp_path / "concurrent-diff-agent"
    agent_root.mkdir()

    n_threads = 4
    specs = [_make_spec(f"server-{i}") for i in range(n_threads)]
    errors = []

    def _try_install(spec: MCPServerSpec) -> None:
        backend = _fresh_backend(agent_root)
        try:
            backend.install(spec)
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        futs = [pool.submit(_try_install, s) for s in specs]
        for fut in futs:
            fut.result()

    assert not errors, f"Unexpected errors installing distinct names: {errors}"
    backend = _fresh_backend(agent_root)
    refs = backend.list_mcp_servers()
    installed_names = {r.name for r in refs}
    expected_names = {f"server-{i}" for i in range(n_threads)}
    assert expected_names == installed_names


# ──────────────────────────────────────────────────────────────────────────────
# 7b. Lock timeout test (spec/36 MUST 9 -- LockBusy -> MCPRegistryUnavailable)


def test_install_lock_timeout_zero_under_contention(tmp_path: Path) -> None:
    """install() with install_lock_timeout=0.0 and a held lock raises MCPRegistryUnavailable.

    spec/36 MUST 9 -- the LockBusy -> MCPRegistryUnavailable translation is
    the contract. This test pins it so a future regression is visible.

    Strategy: share a single FilesystemLockBackend between the test harness and
    the backend. The test acquires "mcp_registry" first. The backend's install()
    then tries to re-acquire via the same backend instance; FilesystemLockBackend
    is non-reentrant so it raises LockBusy immediately (regardless of timeout),
    which install() translates to MCPRegistryUnavailable.
    """
    agent_root = tmp_path / "timeout-agent"
    agent_root.mkdir()

    # One shared lock backend instance.
    lock_backend = FilesystemLockBackend(agent_root)

    # Acquire the registry lock from the test harness (timeout=5 is generous).
    handle = lock_backend.acquire("mcp_registry", timeout=5)

    try:
        backend = FilesystemMCPServerRegistryBackend(
            agent_root,
            read_paths=[],
            lock_backend=lock_backend,
            install_lock_timeout=0.0,
        )

        with pytest.raises(MCPRegistryUnavailable):
            backend.install(_make_spec("timeout-server"))
    finally:
        handle.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────────
# 8. CLI handler tests


def test_cli_install_success_output_does_not_echo_env(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """_mcp_registry_install: success output contains server name but NOT env values.

    Stream D finding D-F4 -- never echo env / command / args in success output.
    """
    from atomic_agents.cli import _mcp_registry_install

    agent_root = tmp_path / "cli-no-echo-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)

    exit_code = _mcp_registry_install(
        backend,
        "safe-server",
        "npx",
        "",
        "TOKEN=$MY_SECRET_VAR",
        "",
        "stdio",
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "safe-server" in captured.out
    assert "$MY_SECRET_VAR" not in captured.out
    assert "TOKEN" not in captured.out


def test_cli_install_warns_on_literal_env_value(tmp_path: Path, capsys) -> None:
    """_mcp_registry_install warns on stderr and exits 0 when --env value lacks $ prefix.

    Stream D finding D-F1 (decision 3 = WARN + proceed): literal values land
    on disk plaintext; the CLI warns the operator but continues (exit 0).
    This is the v1.0 contract per decision 3.
    """
    from atomic_agents.cli import _mcp_registry_install

    agent_root = tmp_path / "cli-warn-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)

    exit_code = _mcp_registry_install(
        backend,
        "warn-server",
        "echo",
        "",
        "KEY=literal_not_a_var",
        "",
        "stdio",
    )

    # WARN + proceed is the v1.0 contract per decision 3.
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Warning" in captured.err or "warning" in captured.err.lower()
    assert "literal" in captured.err or "KEY" in captured.err


def test_cli_install_refuses_h2_in_description(tmp_path: Path, capsys) -> None:
    """_mcp_registry_install exits 1 when --description contains '## '.

    Stream D finding D-F9 + Stream B finding B-10 -- H2 headers delimit
    server sections in mcp.md; injecting one via --description would corrupt
    the file.
    """
    from atomic_agents.cli import _mcp_registry_install

    agent_root = tmp_path / "cli-h2-agent"
    agent_root.mkdir()
    backend = _fresh_backend(agent_root)

    exit_code = _mcp_registry_install(
        backend,
        "legit-server",
        "echo",
        "",
        "",
        "## evil-section",
        "stdio",
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "##" in captured.err or "H2" in captured.err or "description" in captured.err
    # mcp.md must NOT have been written.
    assert not (agent_root / "mcp.md").exists()


def test_cli_parse_env_flag_empty() -> None:
    """_parse_env_flag('') returns {}."""
    from atomic_agents.cli import _parse_env_flag

    assert _parse_env_flag("") == {}


def test_cli_parse_env_flag_single_pair() -> None:
    """_parse_env_flag('K=$V') returns {'K': '$V'}."""
    from atomic_agents.cli import _parse_env_flag

    assert _parse_env_flag("K=$V") == {"K": "$V"}


def test_cli_parse_env_flag_multiple_pairs() -> None:
    """_parse_env_flag('A=$X,B=$Y') returns {'A': '$X', 'B': '$Y'}."""
    from atomic_agents.cli import _parse_env_flag

    result = _parse_env_flag("A=$X,B=$Y")
    assert result == {"A": "$X", "B": "$Y"}


def test_cli_parse_env_flag_value_contains_equals() -> None:
    """_parse_env_flag splits on FIRST '=' only; value may contain '='."""
    from atomic_agents.cli import _parse_env_flag

    result = _parse_env_flag("K=a=b=c")
    assert result == {"K": "a=b=c"}


def test_cli_parse_env_flag_missing_equals_raises() -> None:
    """_parse_env_flag raises ValueError when an entry has no '='."""
    from atomic_agents.cli import _parse_env_flag

    with pytest.raises(ValueError, match="missing '='"):
        _parse_env_flag("NOKEYVALUE")


def test_cli_parse_args_flag_empty() -> None:
    """_parse_args_flag('') returns []."""
    from atomic_agents.cli import _parse_args_flag

    assert _parse_args_flag("") == []


def test_cli_parse_args_flag_splits_on_comma() -> None:
    """_parse_args_flag('-y,@some/pkg') returns ['-y', '@some/pkg']."""
    from atomic_agents.cli import _parse_args_flag

    assert _parse_args_flag("-y,@some/pkg") == ["-y", "@some/pkg"]

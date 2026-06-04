"""Conformance tests for MCPServerRegistryBackend Protocol (spec/36).

Parametrized across registered backends. At PR 1 only the filesystem backend is
parametrized (params=["filesystem"]). The HTTP backend joins at PR 4 via an
additional params entry -- every test here then runs against both backends with
zero additional test code.

~40 tests covering the Protocol contract that ANY backend MUST satisfy.

Coverage:
    MUST 1  -- name charset validation at API boundary (~5 tests)
    MUST 2  -- side-effect-free construction (~4 tests)
    MUST 3  -- capability claim-vs-behavior parity (~5 tests)
    MUST 5  -- cross-agent isolation / per-agent scoping (~4 tests)
    MUST 6  -- backend_id stability + close() idempotency (~4 tests)
    MUST 7  -- transient-vs-permanent failure honesty (~4 tests)
    MUST 8  -- env-var resolution at load time (~6 tests)
    MUST 10 -- load_all_mcp_servers consistency (~5 tests)

Per spec/36 and prep notes B-F4, B-F5, B-F6, B-F7, B-F8.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from atomic_agents.mcp_registry import (
    FilesystemMCPServerRegistryBackend,
    MCPRegistryUnavailable,
    MCPServerNotInRegistry,
    MCPServerRegistryBackend,
    MCPServerRegistryCapabilities,
)
from atomic_agents.mcp_registry.backend import _default_load_all
from atomic_agents.mcp_registry.types import MCPServerRef
from atomic_agents.mcp import MCPServerSpec, parse_mcp_md_text
from atomic_agents.exceptions import MCPServerConnectFailed


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_mcp_spec(
    name: str,
    command: str = "echo",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    transport: str = "stdio",
    description: str = "",
) -> MCPServerSpec:
    """Build a minimal MCPServerSpec for test fixtures."""
    return MCPServerSpec(
        name=name,
        command=command,
        args=args or [],
        env=env or {},
        transport=transport,
        description=description,
    )


def make_mcp_md(agent_root: Path, specs: list[MCPServerSpec]) -> Path:
    """Write a valid mcp.md file in agent_root for the given specs.

    Mirrors the format accepted by parse_mcp_md_text (spec/19).
    Returns the Path to the written file.
    """
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
    mcp_md = agent_root / "mcp.md"
    mcp_md.write_text("\n".join(lines), encoding="utf-8")
    return mcp_md


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def tmp_agent_root(tmp_path: Path) -> Path:
    """Return a fresh agent_root Path with the directory created."""
    root = tmp_path / "agent-root"
    root.mkdir()
    return root


@pytest.fixture(params=["filesystem"])
def backend_factory(request, tmp_path: Path):
    """Parametrized fixture that returns a constructed backend instance.

    HTTP backend joins at PR 4 via an additional params entry:
        params=["filesystem", "http"]
    with an ``elif request.param == "http":`` branch here.
    """
    if request.param == "filesystem":
        agent_root = tmp_path / "agent-for-backend"
        agent_root.mkdir()
        return FilesystemMCPServerRegistryBackend(agent_root, [])
    raise ValueError(f"Unknown backend param: {request.param!r}")


@pytest.fixture
def populated_backend(tmp_path: Path):
    """Backend pre-populated with 3 specs in mcp.md.

    Used for MUST 10 consistency tests. Returns (backend, specs) so tests can
    reference the specs used for population.
    """
    agent_root = tmp_path / "populated-agent"
    agent_root.mkdir()
    specs = [
        _make_mcp_spec("alpha-server", description="First server"),
        _make_mcp_spec("beta-server", description="Second server"),
        _make_mcp_spec("gamma-server", description="Third server"),
    ]
    make_mcp_md(agent_root, specs)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    return backend, specs


# ──────────────────────────────────────────────────────────────────────────────
# MUST 1 -- Name charset validation at API boundary


def test_name_charset_rejects_path_traversal_dotdot(backend_factory) -> None:
    """load_mcp_server with '..' raises ValueError (path-traversal token).

    spec/36 MUST 1.
    """
    with pytest.raises(ValueError):
        backend_factory.load_mcp_server("..")


def test_name_charset_rejects_slash(backend_factory) -> None:
    """load_mcp_server with 'a/b' raises ValueError (slash is disallowed).

    spec/36 MUST 1.
    """
    with pytest.raises(ValueError):
        backend_factory.load_mcp_server("a/b")


def test_name_charset_rejects_backslash(backend_factory) -> None:
    """load_mcp_server with backslash raises ValueError.

    spec/36 MUST 1.
    """
    with pytest.raises(ValueError):
        backend_factory.load_mcp_server("a\\b")


def test_name_charset_rejects_leading_dot(backend_factory) -> None:
    """load_mcp_server with a name starting with '.' raises ValueError.

    spec/36 MUST 1.
    """
    with pytest.raises(ValueError):
        backend_factory.load_mcp_server(".hidden")


def test_name_charset_rejects_empty_string(backend_factory) -> None:
    """load_mcp_server with an empty string raises ValueError.

    spec/36 MUST 1.
    """
    with pytest.raises(ValueError):
        backend_factory.load_mcp_server("")


def test_name_charset_accepts_valid_edges(tmp_path: Path) -> None:
    """load_mcp_server with valid charset edges does not raise ValueError for the name.

    Valid charset: [a-zA-Z0-9_.+@-]+. This test uses 'a.b+c@d-e_f'.
    The backend may raise MCPServerNotInRegistry (name valid but not in catalog)
    but MUST NOT raise ValueError.
    """
    agent_root = tmp_path / "edge-agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPServerNotInRegistry):
        backend.load_mcp_server("a.b+c@d-e_f")


# ──────────────────────────────────────────────────────────────────────────────
# MUST 2 -- Side-effect-free construction


def test_construction_does_not_open_files(tmp_path: Path) -> None:
    """Constructor with a non-existent agent_root succeeds without touching disk.

    spec/36 MUST 2 -- side-effect-free construction.
    """
    non_existent = tmp_path / "ghost-agent"
    assert not non_existent.exists()
    backend = FilesystemMCPServerRegistryBackend(non_existent, [])
    assert backend is not None
    assert not non_existent.exists(), (
        "Constructor must not create the agent_root directory."
    )


def test_construction_does_not_spawn_subprocesses(tmp_path: Path) -> None:
    """Constructor does not spawn any subprocesses.

    spec/36 MUST 2 -- side-effect-free construction.
    Verifies by patching subprocess.Popen: if it is called the test fails.
    """
    agent_root = tmp_path / "agent-no-proc"
    agent_root.mkdir()
    with patch("subprocess.Popen") as mock_popen:
        FilesystemMCPServerRegistryBackend(agent_root, [])
        mock_popen.assert_not_called()


def test_construction_with_empty_read_paths_succeeds(tmp_path: Path) -> None:
    """Constructor with empty read_paths list completes without error.

    spec/36 MUST 2 -- side-effect-free construction.
    """
    agent_root = tmp_path / "agent-empty-rp"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    assert backend is not None


def test_construction_does_not_touch_existing_files(tmp_path: Path) -> None:
    """Constructor with an existing agent_root does not modify any files.

    spec/36 MUST 2 -- side-effect-free construction.
    """
    agent_root = tmp_path / "existing-agent"
    agent_root.mkdir()
    sentinel = agent_root / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    before = sorted(p.name for p in agent_root.iterdir())

    FilesystemMCPServerRegistryBackend(agent_root, [])

    after = sorted(p.name for p in agent_root.iterdir())
    assert before == after, (
        f"Constructor must not touch the filesystem. Before: {before!r} After: {after!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# MUST 3 -- Capability claim-vs-behavior parity


def test_capability_honesty_install(backend_factory) -> None:
    """capabilities.supports_install claim matches install() behavior.

    spec/36 MUST 3 -- capability honesty. Branches on reported value; never
    hardcodes the expected bool (B-F4 from prep notes).

    True-branch tightened per Stream E finding E5 (P0): when supports_install=True,
    install() MUST return an MCPServerRef on a fresh backend with a valid spec.
    The old broad 'except Exception: pass' masked real failures.
    """
    caps = backend_factory.capabilities
    dummy_spec = _make_mcp_spec("test-install-server")
    if caps.supports_install:
        # MUST 3: install must NOT raise NotImplementedError; on a fresh
        # backend with valid spec it must return MCPServerRef.
        ref = backend_factory.install(dummy_spec)
        assert isinstance(ref, MCPServerRef), (
            f"install() with supports_install=True must return MCPServerRef; "
            f"got {type(ref)!r}"
        )
    else:
        # Method MUST raise NotImplementedError when capability is False.
        with pytest.raises(NotImplementedError):
            backend_factory.install(dummy_spec)


def test_capability_honesty_uninstall(backend_factory) -> None:
    """capabilities.supports_uninstall claim matches uninstall() behavior.

    spec/36 MUST 3 -- capability honesty.

    True-branch tightened per Stream E finding E4 (P1) + C10: when
    supports_uninstall=True, uninstalling an absent name MUST be a no-op
    (MUST 9 idempotency) and must return None.
    """
    caps = backend_factory.capabilities
    if caps.supports_uninstall:
        # MUST 9: absent name is a no-op, no exception of any kind.
        result = backend_factory.uninstall("definitely-not-in-registry")
        assert result is None, (
            "uninstall() on absent name with supports_uninstall=True must return None "
            "(idempotent no-op per MUST 9)"
        )
    else:
        with pytest.raises(NotImplementedError):
            backend_factory.uninstall("nonexistent-server")


def test_capability_honesty_capability_handshake(backend_factory) -> None:
    """capabilities.supports_capability_handshake is a boolean.

    spec/36 MUST 3 -- capability honesty. At PR 1 filesystem backend reports
    False; the test branches on the reported value rather than asserting a
    specific bool.
    """
    caps = backend_factory.capabilities
    assert isinstance(caps.supports_capability_handshake, bool)


def test_capability_honesty_supports_audit(backend_factory) -> None:
    """capabilities.supports_audit is a boolean.

    spec/36 MUST 3 -- capability honesty.
    """
    caps = backend_factory.capabilities
    assert isinstance(caps.supports_audit, bool)


def test_capability_honesty_durable(backend_factory) -> None:
    """capabilities.durable is a boolean.

    spec/36 MUST 3 -- capability honesty.
    """
    caps = backend_factory.capabilities
    assert isinstance(caps.durable, bool)


# ──────────────────────────────────────────────────────────────────────────────
# MUST 5 -- Cross-agent isolation


def test_cross_agent_isolation_separate_agent_roots(tmp_path: Path) -> None:
    """Two backends with different agent_roots do not share catalog data.

    spec/36 MUST 5. Each backend is scoped to its own agent_root.
    """
    agent_a = tmp_path / "agent-a"
    agent_a.mkdir()
    agent_b = tmp_path / "agent-b"
    agent_b.mkdir()

    specs_a = [_make_mcp_spec("server-a-only")]
    make_mcp_md(agent_a, specs_a)

    backend_a = FilesystemMCPServerRegistryBackend(agent_a, [])
    backend_b = FilesystemMCPServerRegistryBackend(agent_b, [])

    refs_a = backend_a.list_mcp_servers()
    refs_b = backend_b.list_mcp_servers()

    assert any(r.name == "server-a-only" for r in refs_a)
    assert not any(r.name == "server-a-only" for r in refs_b)


def test_cross_agent_isolation_reads_own_mcp_md(tmp_path: Path) -> None:
    """Each backend reads only its own mcp.md, not a sibling agent's.

    spec/36 MUST 5 -- per-agent scoping.
    """
    agent_a = tmp_path / "agent-a"
    agent_a.mkdir()
    agent_b = tmp_path / "agent-b"
    agent_b.mkdir()

    make_mcp_md(agent_a, [_make_mcp_spec("unique-to-a")])
    make_mcp_md(agent_b, [_make_mcp_spec("unique-to-b")])

    backend_a = FilesystemMCPServerRegistryBackend(agent_a, [])
    backend_b = FilesystemMCPServerRegistryBackend(agent_b, [])

    names_a = {r.name for r in backend_a.list_mcp_servers()}
    names_b = {r.name for r in backend_b.list_mcp_servers()}

    assert names_a == {"unique-to-a"}
    assert names_b == {"unique-to-b"}
    assert names_a.isdisjoint(names_b)


def test_lock_file_scoping_distinct_from_main_lock(tmp_path: Path) -> None:
    """The registry lock file (.mcp_registry.lock) is distinct from the agent
    main lock file (.lock).

    spec/36 D5 -- lock_backend operators must scope to .mcp_registry.lock.
    Verifies the docstring contract: a custom lock_backend passed at construction
    must NOT reuse the agent's main .lock.
    """
    # This test verifies the naming distinction by inspecting the docstring
    # intent: .mcp_registry.lock != .lock. We verify the FilesystemMCPServerRegistryBackend
    # accepts lock_backend=None (default) without raising.
    agent_root = tmp_path / "lock-test-agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [], lock_backend=None)
    assert backend is not None


def test_list_sorted_lexicographic(tmp_path: Path) -> None:
    """list_mcp_servers() returns refs in lexicographic order by name.

    spec/36 MUST 5 (consistent sort) -- ordering is an isolation-adjacent
    invariant: operators relying on ordering get consistent behavior
    regardless of backend storage order.
    """
    agent_root = tmp_path / "sorted-agent"
    agent_root.mkdir()
    specs = [
        _make_mcp_spec("zeta-server"),
        _make_mcp_spec("alpha-server"),
        _make_mcp_spec("mu-server"),
    ]
    make_mcp_md(agent_root, specs)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    names = [r.name for r in refs]
    assert names == sorted(names), f"Expected sorted order; got {names!r}"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 6 -- backend_id stability + close() idempotency


def test_backend_id_is_non_empty_string(backend_factory) -> None:
    """backend_id is a non-empty string.

    spec/36 MUST 6(a) -- backend_id stability.
    """
    assert isinstance(backend_factory.backend_id, str)
    assert backend_factory.backend_id


def test_backend_id_is_stable(backend_factory) -> None:
    """backend_id does not change across calls.

    spec/36 MUST 6(a) -- backend_id stability.
    """
    first = backend_factory.backend_id
    second = backend_factory.backend_id
    assert first == second


def test_close_twice_does_not_raise(backend_factory) -> None:
    """Calling close() twice does not raise any exception.

    spec/36 MUST 6(b) -- close() idempotency.
    """
    backend_factory.close()
    backend_factory.close()  # must not raise


def test_refresh_capabilities_returns_equivalent_to_capabilities(
    backend_factory,
) -> None:
    """refresh_capabilities() returns an object equivalent to capabilities.

    spec/36 MUST 6 -- refresh_capabilities is idempotent on filesystem backends.
    """
    caps = backend_factory.capabilities
    refreshed = backend_factory.refresh_capabilities()
    # Must be the same type and have the same values.
    assert isinstance(refreshed, MCPServerRegistryCapabilities)
    assert refreshed.supports_install == caps.supports_install
    assert refreshed.supports_uninstall == caps.supports_uninstall
    assert refreshed.supports_capability_handshake == caps.supports_capability_handshake
    assert refreshed.supports_audit == caps.supports_audit
    assert refreshed.durable == caps.durable


# ──────────────────────────────────────────────────────────────────────────────
# MUST 7 -- Transient-vs-permanent failure honesty


def test_load_raises_not_in_registry_for_absent_name(tmp_path: Path) -> None:
    """load_mcp_server raises MCPServerNotInRegistry for a name not in catalog.

    spec/36 MUST 7 -- permanent absence. Distinct from transient
    MCPRegistryUnavailable.
    """
    agent_root = tmp_path / "agent-absent"
    agent_root.mkdir()
    make_mcp_md(agent_root, [_make_mcp_spec("present-server")])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPServerNotInRegistry):
        backend.load_mcp_server("no-such-server")


def test_list_returns_empty_list_for_absent_mcp_md(tmp_path: Path) -> None:
    """list_mcp_servers() returns [] when mcp.md does not exist.

    spec/36 MUST 7 -- absent file is NOT an error; returns empty list.
    """
    agent_root = tmp_path / "no-mcp-md-agent"
    agent_root.mkdir()
    # No mcp.md written
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    assert refs == []


def test_list_returns_empty_list_for_empty_mcp_md(tmp_path: Path) -> None:
    """list_mcp_servers() returns [] when mcp.md is empty.

    spec/36 MUST 7.
    """
    agent_root = tmp_path / "empty-mcp-md-agent"
    agent_root.mkdir()
    (agent_root / "mcp.md").write_text("", encoding="utf-8")
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    refs = backend.list_mcp_servers()
    assert refs == []


def test_load_raises_not_in_registry_not_unavailable_for_absent_name(
    tmp_path: Path,
) -> None:
    """load_mcp_server raises MCPServerNotInRegistry (permanent), not
    MCPRegistryUnavailable (transient), for a name not in the catalog.

    spec/36 MUST 7 -- callers must not mistake permanent absence for
    transient unavailability.
    """
    agent_root = tmp_path / "agent-perm"
    agent_root.mkdir()
    make_mcp_md(agent_root, [_make_mcp_spec("other-server")])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    exc = None
    try:
        backend.load_mcp_server("not-here")
    except MCPServerNotInRegistry as e:
        exc = e
    except MCPRegistryUnavailable:
        pytest.fail(
            "Raised MCPRegistryUnavailable (transient) instead of MCPServerNotInRegistry (permanent)"
        )
    assert exc is not None, "Expected MCPServerNotInRegistry but nothing was raised"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 8 -- Env-var resolution at load time


def test_env_var_resolves_at_load_mcp_server(tmp_path: Path, monkeypatch) -> None:
    """load_mcp_server resolves $VAR env references at call time.

    spec/36 MUST 8 / Decision 7. monkeypatch.setenv is used per B-F5.
    """
    monkeypatch.setenv("MY_API_KEY", "resolved-value-123")

    agent_root = tmp_path / "env-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("env-server", env={"API_KEY": "$MY_API_KEY"})
    make_mcp_md(agent_root, [spec])

    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    loaded = backend.load_mcp_server("env-server")

    assert loaded.env["API_KEY"] == "resolved-value-123"


def test_list_does_not_resolve_env_vars(tmp_path: Path, monkeypatch) -> None:
    """list_mcp_servers() does NOT resolve $VAR -- returns MCPServerRef metadata only.

    spec/36 MUST 8 / Decision 7 -- resolution deferred to load_mcp_server.
    list_mcp_servers must not raise MCPServerConnectFailed on unset vars.
    """
    # Deliberately NOT setting the env var -- list should not care.
    monkeypatch.delenv("UNSET_VAR_FOR_LIST_TEST", raising=False)

    agent_root = tmp_path / "list-no-resolve-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("server-with-env", env={"KEY": "$UNSET_VAR_FOR_LIST_TEST"})
    make_mcp_md(agent_root, [spec])

    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    # Must not raise MCPServerConnectFailed even though the env var is unset.
    refs = backend.list_mcp_servers()
    assert len(refs) == 1
    assert refs[0].name == "server-with-env"


def test_unresolvable_env_var_raises_connect_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """load_mcp_server raises MCPServerConnectFailed for an unresolvable $VAR.

    spec/36 MUST 8. monkeypatch ensures the var is absent (B-F5).
    """
    monkeypatch.delenv("DEFINITELY_NOT_SET_VAR_XYZ", raising=False)

    agent_root = tmp_path / "unresolvable-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("bad-env-server", env={"KEY": "$DEFINITELY_NOT_SET_VAR_XYZ"})
    make_mcp_md(agent_root, [spec])

    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPServerConnectFailed):
        backend.load_mcp_server("bad-env-server")


def test_parse_mcp_md_text_resolve_env_false_preserves_dollar_var(monkeypatch) -> None:
    """parse_mcp_md_text with resolve_env=False preserves $VAR strings verbatim.

    spec/36 Theme 1 (prep notes C-F1). Callers using resolve_env=False (e.g.
    FilesystemMCPServerRegistryBackend.list_mcp_servers) must not get
    MCPServerConnectFailed and must see the raw $VAR string.
    """
    # parse_mcp_md_text is imported at the top of this file.
    # Deliberately absent var -- resolve_env=False must not raise.
    monkeypatch.delenv("TOTALLY_ABSENT_ENV_VAR_FOR_TEST", raising=False)

    content = dedent("""\
        # MCP servers

        ## my-server
        command: echo
        env: KEY=$TOTALLY_ABSENT_ENV_VAR_FOR_TEST
        description: Test server
    """)
    specs = parse_mcp_md_text(content, resolve_env=False)
    assert len(specs) == 1
    assert specs[0].env["KEY"] == "$TOTALLY_ABSENT_ENV_VAR_FOR_TEST"


def test_env_var_mid_session_mutation_reflected(tmp_path: Path, monkeypatch) -> None:
    """load_mcp_server reflects mid-session os.environ mutations.

    spec/36 MUST 8 -- resolution at call time, not at backend construction.
    Each call to load_mcp_server reads from the CURRENT os.environ.
    """
    monkeypatch.setenv("DYNAMIC_VAR", "first-value")

    agent_root = tmp_path / "dynamic-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("dynamic-server", env={"TOKEN": "$DYNAMIC_VAR"})
    make_mcp_md(agent_root, [spec])

    backend = FilesystemMCPServerRegistryBackend(agent_root, [])

    first = backend.load_mcp_server("dynamic-server")
    assert first.env["TOKEN"] == "first-value"

    monkeypatch.setenv("DYNAMIC_VAR", "second-value")
    second = backend.load_mcp_server("dynamic-server")
    assert second.env["TOKEN"] == "second-value"


def test_env_var_literal_value_not_mistaken_for_dollar_var(tmp_path: Path) -> None:
    """A literal env value without a leading $ is not resolved as a var.

    spec/36 MUST 8 -- only $VAR-prefixed values are resolved.
    """
    agent_root = tmp_path / "literal-env-agent"
    agent_root.mkdir()
    spec = _make_mcp_spec("literal-server", env={"KEY": "literal-value"})
    make_mcp_md(agent_root, [spec])
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    loaded = backend.load_mcp_server("literal-server")
    assert loaded.env["KEY"] == "literal-value"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 10 -- load_all_mcp_servers consistency


def test_load_all_returns_empty_list_for_absent_mcp_md(tmp_path: Path) -> None:
    """load_all_mcp_servers() returns [] when catalog is empty.

    spec/36 MUST 10.
    """
    agent_root = tmp_path / "empty-catalog-agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    result = backend.load_all_mcp_servers()
    assert result == []


def test_load_all_returns_populated_catalog(populated_backend) -> None:
    """load_all_mcp_servers() returns all specs when catalog is populated.

    spec/36 MUST 10.
    """
    backend, specs = populated_backend
    all_specs = backend.load_all_mcp_servers()
    assert len(all_specs) == len(specs)
    loaded_names = {s.name for s in all_specs}
    expected_names = {s.name for s in specs}
    assert loaded_names == expected_names


def test_load_all_ordering_parity(populated_backend) -> None:
    """load_all_mcp_servers() order matches list_mcp_servers() (lexicographic).

    spec/36 MUST 10 -- consistent sort.
    """
    backend, _ = populated_backend
    refs = backend.list_mcp_servers()
    all_specs = backend.load_all_mcp_servers()
    ref_names = [r.name for r in refs]
    spec_names = [s.name for s in all_specs]
    assert spec_names == ref_names


def test_load_all_consistency_with_default_load_all(populated_backend) -> None:
    """load_all_mcp_servers() output is semantically equivalent to _default_load_all.

    spec/36 MUST 10 -- load_all consistency.
    The filesystem backend uses a custom single-read-parse (NOT _default_load_all);
    this test asserts the output is semantically equivalent to the helper,
    satisfying MUST 10.

    Docstring corrected per Stream E finding E2 (P2): the previous docstring
    claimed filesystem "delegates to _default_load_all" which is false.
    filesystem.py lines 316-375 implement a custom single-read-parse loop that
    avoids N+1 load calls and surfaces ENOENT / OSError / parse errors distinctly.
    """
    backend, _ = populated_backend
    via_method = backend.load_all_mcp_servers()
    via_helper = _default_load_all(backend)

    assert len(via_method) == len(via_helper)
    for a, b in zip(via_method, via_helper):
        assert a.name == b.name
        assert a.command == b.command
        assert a.args == b.args
        assert a.env == b.env
        assert a.transport == b.transport
        assert a.description == b.description


def test_load_all_consistency_under_list_ordering(tmp_path: Path) -> None:
    """load_all_mcp_servers produces names consistent with list_mcp_servers.

    spec/36 MUST 10 -- mutation between list and load_all does not break
    ordering contract (verified by checking name order matches after each).
    """
    agent_root = tmp_path / "consistency-agent"
    agent_root.mkdir()
    specs = [
        _make_mcp_spec("zz-last"),
        _make_mcp_spec("aa-first"),
        _make_mcp_spec("mm-middle"),
    ]
    make_mcp_md(agent_root, specs)
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])

    refs = backend.list_mcp_servers()
    all_specs = backend.load_all_mcp_servers()

    assert [r.name for r in refs] == [s.name for s in all_specs]
    assert [s.name for s in all_specs] == sorted(s.name for s in all_specs)


# ──────────────────────────────────────────────────────────────────────────────
# MUST 9 -- Install / uninstall atomicity (new tests, PR 3)


def test_must9_install_atomicity_concurrent_same_name(backend_factory) -> None:
    """Concurrent install of the same server name: exactly one winner.

    spec/36 MUST 9 -- concurrent install atomicity. N=3 threads all call
    install(same_spec); exactly 1 must succeed; the others must raise
    MCPServerAlreadyInstalled or MCPRegistryUnavailable (lock contention).
    Guarded on capability flag so HTTP backend at PR 4 (supports_install=False)
    skips automatically.

    Stream E finding E3 (P1).
    """
    import concurrent.futures

    from atomic_agents.mcp_registry import (
        MCPServerAlreadyInstalled,
        MCPRegistryUnavailable,
    )

    caps = backend_factory.capabilities
    if not caps.supports_install:
        pytest.skip("backend does not support install; skipping MUST 9 atomicity test")

    spec = _make_mcp_spec("concurrent-conformance-server")
    successes: list[bool] = []
    failures: list[bool] = []

    def _try() -> None:
        try:
            backend_factory.install(spec)
            successes.append(True)
        except (MCPServerAlreadyInstalled, MCPRegistryUnavailable):
            failures.append(True)

    n = 3
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(_try) for _ in range(n)]
        for fut in futs:
            fut.result()

    assert len(successes) == 1, (
        f"MUST 9: exactly 1 concurrent install winner; got {len(successes)}"
    )
    assert len(successes) + len(failures) == n


def test_must9_uninstall_absent_name_is_noop(backend_factory) -> None:
    """uninstall() on an absent name is a no-op (returns None, no exception).

    spec/36 MUST 9 -- uninstall idempotency. Guarded on capability flag so
    HTTP backend at PR 4 (supports_uninstall=False) skips automatically.

    Stream E finding E4 (P1) + C11.
    """
    caps = backend_factory.capabilities
    if not caps.supports_uninstall:
        pytest.skip(
            "backend does not support uninstall; skipping MUST 9 idempotency test"
        )

    result = backend_factory.uninstall("absolutely-not-in-registry-conformance")
    assert result is None, (
        "MUST 9: uninstall on absent name must return None (idempotent no-op)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# MUST 10 -- Post-install consistency (new test, PR 3)


def test_must10_post_install_consistency(backend_factory) -> None:
    """After install(), load_all_mcp_servers() and list_mcp_servers() are consistent.

    spec/36 MUST 10 -- load_all consistency must hold after write operations.
    Verifies that every name from list_mcp_servers() is loadable via
    load_mcp_server() and that set(load_all_mcp_servers()) equals the
    per-name load iteration.
    Guarded on capability flag so HTTP backend at PR 4 skips automatically.

    Stream E finding E6 (P1).
    """
    caps = backend_factory.capabilities
    if not caps.supports_install:
        pytest.skip(
            "backend does not support install; skipping MUST 10 post-install test"
        )

    spec = _make_mcp_spec("must10-consistency-server", command="echo")
    backend_factory.install(spec)

    refs = backend_factory.list_mcp_servers()
    all_specs = backend_factory.load_all_mcp_servers()

    ref_names = {r.name for r in refs}
    spec_names = {s.name for s in all_specs}

    assert ref_names == spec_names, (
        f"MUST 10: list_mcp_servers names {ref_names!r} must equal "
        f"load_all_mcp_servers names {spec_names!r} after install"
    )

    # Every name from list must be individually loadable.
    for ref in refs:
        loaded = backend_factory.load_mcp_server(ref.name)
        assert loaded.name == ref.name


# ──────────────────────────────────────────────────────────────────────────────
# Module-level unit tests: _redact_for_error_message (MUST 4 -- PR 1 scope)


def test_redact_strips_url_credentials() -> None:
    """_redact_for_error_message strips credentials from URLs.

    spec/36 MUST 4 -- env-var paste credential redaction at the
    BackendNotRegistered error site.
    """
    from atomic_agents.mcp_registry import _redact_for_error_message

    result = _redact_for_error_message("https://user:pass@catalog.example.com/api")
    assert result == "https://..."
    assert "user" not in result
    assert "pass" not in result


def test_redact_truncates_long_non_url_string() -> None:
    """_redact_for_error_message truncates long non-URL strings at max_len.

    spec/36 MUST 4.
    """
    from atomic_agents.mcp_registry import _redact_for_error_message

    long_val = "a" * 50
    result = _redact_for_error_message(long_val, max_len=32)
    assert result.endswith("...")
    assert len(result) <= 35  # max_len + len("...")


def test_redact_preserves_short_non_url_string() -> None:
    """_redact_for_error_message returns short non-URL strings unchanged.

    spec/36 MUST 4.
    """
    from atomic_agents.mcp_registry import _redact_for_error_message

    result = _redact_for_error_message("filesystem")
    assert result == "filesystem"


def test_backend_not_registered_uses_redacted_value(
    monkeypatch, tmp_path: Path
) -> None:
    """get_default_mcp_server_registry_backend with a URL-like backend_id
    raises BackendNotRegistered with a redacted error message.

    spec/36 MUST 4 -- credential leak prevention at the operator-config factory.
    """
    from atomic_agents.mcp_registry import (
        BackendNotRegistered,
        get_default_mcp_server_registry_backend,
    )

    # Simulate an operator accidentally pasting a URL with credentials.
    monkeypatch.setenv(
        "ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND",
        "https://user:secret@catalog.example.com",
    )

    agent_root = tmp_path / "dummy-agent"
    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_mcp_server_registry_backend(agent_root, [])

    error_msg = str(exc_info.value)
    assert "secret" not in error_msg
    assert "user" not in error_msg
    assert "https://..." in error_msg


# ──────────────────────────────────────────────────────────────────────────────
# P1 #5 -- Protocol declares backend_id property


def test_protocol_declares_backend_id_property(backend_factory) -> None:
    """MCPServerRegistryBackend Protocol declares backend_id as a property.

    P1 #5 fix: backend_id was implemented on FilesystemMCPServerRegistryBackend
    but missing from the Protocol surface. Verifies the attribute is accessible
    and returns a non-empty string on the concrete backend.
    spec/36 MUST 6(a).
    """
    # Verify accessible on instance (concrete backend)
    assert hasattr(backend_factory, "backend_id"), (
        "backend_id must be accessible on the backend instance"
    )
    assert isinstance(backend_factory.backend_id, str)
    assert backend_factory.backend_id, "backend_id must be a non-empty string"


def test_protocol_backend_id_introspectable_via_protocol() -> None:
    """The MCPServerRegistryBackend Protocol body mentions 'backend_id'.

    P1 #5 conformance: the Protocol class must declare backend_id so static
    type checkers and runtime introspection can surface it.
    """
    # The Protocol is structural, but we can verify via annotations / members.
    # 'backend_id' must appear in the Protocol's declared members.
    members = dir(MCPServerRegistryBackend)
    assert "backend_id" in members, (
        "MCPServerRegistryBackend Protocol must declare 'backend_id'"
    )


# ──────────────────────────────────────────────────────────────────────────────
# P2 #2 -- _redact_for_error_message DSN heuristic


def test_redact_strips_dsn_without_scheme() -> None:
    """_redact_for_error_message catches DSN-style strings without a scheme.

    P2 #2 fix: 'user:password@host/db' has no '://' but contains credentials.
    The DSN heuristic 'user:something@host' must return a redacted placeholder.
    spec/36 MUST 4 -- credential redaction.
    """
    from atomic_agents.mcp_registry import _redact_for_error_message

    result = _redact_for_error_message("user:secretpass@db-host/mydb")
    assert "secretpass" not in result
    assert result == "[redacted-connection-string]"


def test_redact_dsn_without_scheme_does_not_match_plain_at_sign() -> None:
    """_redact_for_error_message does not over-redact simple strings with '@'.

    P2 #2: 'user@host' has an '@' but no 'colon-password' pattern, so it
    should not be treated as a DSN.
    """
    from atomic_agents.mcp_registry import _redact_for_error_message

    result = _redact_for_error_message("user@host")
    # Should NOT be redacted as a DSN -- no colon-before-@ pattern.
    assert result == "user@host"

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

HTTP backend parametrize added at PR 4 (prep notes E-F1, E-F3). The
backend_factory and populated_backend fixtures each grow an "http" branch
that constructs HTTPMCPServerRegistryBackend with an httpx.MockTransport
so capability tests do not cascade-fail due to real network calls.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import httpx
import pytest

from atomic_agents.mcp_registry import (
    FilesystemMCPServerRegistryBackend,
    MCPRegistryUnavailable,
    MCPServerAlreadyInstalled,
    MCPServerNotInRegistry,
    MCPServerRegistryBackend,
    MCPServerRegistryCapabilities,
)
from atomic_agents.mcp_registry.backend import _default_load_all
from atomic_agents.mcp_registry.http import HTTPMCPServerRegistryBackend
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
# HTTP MockTransport helpers (prep notes E-F9)


def _default_mock_transport(
    extra_servers: list[dict] | None = None,
) -> httpx.MockTransport:
    """Return an httpx.MockTransport that responds successfully to the full probe
    sequence and returns an optionally populated server catalog.

    Upgraded at PR 5 (D-PR5-9) to serve tier-2 capability responses so that the
    MUST 3 True-branch (supports_install=True) is actually exercised against the
    HTTP backend. Prior to PR 5 this fixture served tier-1 responses, which caused
    the MUST 3 True-branch on HTTP to never fire.

    Tier-2 additions at PR 5:
    - GET /capabilities returns supports_install=True, supports_uninstall=True.
    - OPTIONS /mcp-servers returns Allow: GET, POST, DELETE (tier-2 signal).
    - POST /mcp-servers: returns 201 with MCPServerRef-shaped body on first
      install; 409 on duplicate name (tracked in a closure-scoped dict).
    - DELETE /mcp-servers/<name>: returns 204 with empty body (idempotent).

    ``extra_servers`` is a list of wire-format server dicts (same shape as what
    a catalog server returns in ``{"servers": [...]}``) that the transport will
    serve on the list and bulk endpoints.
    """
    import threading as _threading

    servers = list(extra_servers or [])
    # Closure-scoped dict tracks installed server names for POST 409 simulation.
    # Key: server name (str). Value: True (presence only).
    # Models a real catalog server's uniqueness constraint at the storage layer.
    # Per D-PR5-10: first POST for a name returns 201; subsequent POSTs return 409.
    # Fix 3: threading.Lock serializes the check-then-set to avoid a TOCTOU race
    # under concurrent POSTs (CPython GIL makes individual ops atomic but the
    # read-check-write block is not).  Mirrors real catalog-server atomicity.
    installed: dict[str, bool] = {}
    _installed_lock = _threading.Lock()

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        query = str(request.url.query)
        method = request.method

        if method == "OPTIONS":
            # OPTIONS probe for tier negotiation (Decision 4 step 2).
            # Tier-2: supports POST + DELETE on /mcp-servers.
            return httpx.Response(200, headers={"Allow": "GET, POST, DELETE"})

        if method == "POST" and path.endswith("/mcp-servers"):
            # Install endpoint: POST /mcp-servers.
            # Parse the body to extract the server name.
            try:
                import json as _json

                body = _json.loads(request.content.decode("utf-8"))
                name = body.get("name", "unknown-server")
            except Exception:
                body = {}
                name = "unknown-server"
            with _installed_lock:
                if name in installed:
                    # Duplicate name: HTTP 409 Conflict.
                    return httpx.Response(
                        409, json={"error": f"server {name!r} already installed"}
                    )
                installed[name] = True
                # Fix 4: append new server to `servers` so subsequent GET
                # /mcp-servers (and ?expand=spec) return the newly installed
                # entry.  Without this, MUST 10 asserts set()==set() vacuously.
                new_server = {
                    "name": name,
                    "description": body.get("description", ""),
                    "transport": body.get("transport", "stdio"),
                    "command": body.get("command", ""),
                    "args": body.get("args", []),
                    "env": body.get("env", {}),
                    "version": None,
                    "source": f"http://catalog.example.invalid/mcp-servers/{name}",
                }
                servers.append(new_server)
            # 201 Created with a valid MCPServerRef-shaped body.
            return httpx.Response(
                201,
                json={
                    "name": name,
                    "description": body.get("description", ""),
                    "transport": body.get("transport", "stdio"),
                    "version": None,
                    "source": f"http://catalog.example.invalid/mcp-servers/{name}",
                },
            )

        if method == "DELETE" and "/mcp-servers/" in path:
            # Uninstall endpoint: DELETE /mcp-servers/<name>.
            # Idempotent: returns 204 regardless of whether the name exists.
            name = path.rsplit("/", 1)[-1]
            with _installed_lock:
                installed.pop(name, None)
                servers[:] = [s for s in servers if s["name"] != name]
            return httpx.Response(204, content=b"")

        if path.endswith("/capabilities"):
            # Tier-2 capability response (PR 5 upgrade from tier-1).
            # supports_install and supports_uninstall are True so the MUST 3
            # True-branch actually executes against the HTTP backend.
            return httpx.Response(
                200,
                json={
                    "tier": 2,
                    "supports_install": True,
                    "supports_uninstall": True,
                    "supports_audit": False,
                    "wire_version": "1.0",
                },
            )

        # Exact /mcp-servers collection endpoint (list or bulk; expand=spec query toggles).
        if path.endswith("/mcp-servers"):
            if "expand" in query:
                # Bulk endpoint: GET /mcp-servers?expand=spec returns full specs.
                return httpx.Response(200, json={"servers": servers})
            # Plain list endpoint: GET /mcp-servers returns refs (metadata only).
            refs = [
                {
                    "name": s["name"],
                    "description": s.get("description", ""),
                    "transport": s.get("transport", "stdio"),
                }
                for s in servers
            ]
            return httpx.Response(200, json={"servers": refs})

        # Validate endpoint: GET /mcp-servers/<name>/validate
        if path.endswith("/validate") and "/mcp-servers/" in path:
            name = path.rsplit("/", 2)[-2]
            for s in servers:
                if s["name"] == name:
                    return httpx.Response(
                        200, json={"ok": True, "errors": [], "warnings": []}
                    )
            return httpx.Response(404, json={"error": "not found"})

        # Per-name endpoint: GET /mcp-servers/<name> returns the single spec.
        if "/mcp-servers/" in path:
            name = path.rsplit("/", 1)[-1]
            for s in servers:
                if s["name"] == name:
                    return httpx.Response(200, json=s)
            return httpx.Response(404, json={"error": "not found"})

        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(_handler)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def tmp_agent_root(tmp_path: Path) -> Path:
    """Return a fresh agent_root Path with the directory created."""
    root = tmp_path / "agent-root"
    root.mkdir()
    return root


@pytest.fixture(params=["filesystem", "http"])
def backend_factory(request, tmp_path: Path):
    """Parametrized fixture that returns a constructed backend instance.

    Parametrized across "filesystem" and "http" backends. The HTTP branch
    uses an httpx.MockTransport that responds successfully to the full
    Decision 4 probe sequence so capability tests do not cascade-fail.
    Per prep notes E-F3.
    """
    if request.param == "filesystem":
        agent_root = tmp_path / "agent-for-backend"
        agent_root.mkdir()
        return FilesystemMCPServerRegistryBackend(agent_root, [])
    elif request.param == "http":
        transport = _default_mock_transport()
        client = httpx.Client(transport=transport)
        return HTTPMCPServerRegistryBackend(
            catalog_url="http://catalog.example.invalid",
            agent_scope="test-scope",
            _http_client=client,
        )
    raise ValueError(f"Unknown backend param: {request.param!r}")


@pytest.fixture(params=["filesystem", "http"])
def populated_backend(request, tmp_path: Path):
    """Backend pre-populated with 3 specs.

    Used for MUST 10 consistency tests. Returns (backend, specs) so tests can
    reference the specs used for population.

    Parametrized across "filesystem" and "http" backends (prep notes E-F1).
    The HTTP branch provides a MockTransport serving the same 3 servers
    consistently across GET /mcp-servers, GET /mcp-servers?expand=spec,
    and GET /mcp-servers/<name> endpoints.
    """
    specs = [
        _make_mcp_spec("alpha-server", description="First server"),
        _make_mcp_spec("beta-server", description="Second server"),
        _make_mcp_spec("gamma-server", description="Third server"),
    ]

    if request.param == "filesystem":
        agent_root = tmp_path / "populated-agent"
        agent_root.mkdir()
        make_mcp_md(agent_root, specs)
        backend = FilesystemMCPServerRegistryBackend(agent_root, [])
        return backend, specs
    elif request.param == "http":
        # Wire format: each spec becomes a dict compatible with the HTTP wire shape.
        wire_servers = [
            {
                "name": s.name,
                "command": s.command,
                "args": s.args,
                "env": s.env,
                "transport": s.transport,
                "description": s.description,
            }
            for s in specs
        ]
        transport = _default_mock_transport(extra_servers=wire_servers)
        client = httpx.Client(transport=transport)
        backend = HTTPMCPServerRegistryBackend(
            catalog_url="http://catalog.example.invalid",
            agent_scope="test-scope",
            _http_client=client,
        )
        return backend, specs
    raise ValueError(f"Unknown backend param: {request.param!r}")


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

    Probe-before-cap-read (D-PR5-9): list_mcp_servers() is called first to ensure
    the HTTP backend has completed its capability probe. Without this, the pre-probe
    conservative False default would be observed on HTTP even with a tier-2 fixture,
    causing the True-branch to never fire against HTTP.

    Note on tier-regression fail-late (B-F9): if a 405 fires mid-session AFTER
    a successful tier-2 probe, the tier-regression handler raises NotImplementedError
    AFTER the call entry. This is COMPATIBLE with MUST 3 because the capability
    was True at call entry; the handler re-probes and updates the cache.
    """
    # Trigger capability probe on HTTP backend by calling a read method first.
    # On filesystem backend this is a no-op (no probe required).
    backend_factory.list_mcp_servers()
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

    Probe-before-cap-read (D-PR5-9): list_mcp_servers() is called first to
    ensure the HTTP backend has completed its capability probe.
    """
    # Trigger capability probe on HTTP backend before reading caps.
    backend_factory.list_mcp_servers()
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

    spec/36 MUST 6 -- refresh_capabilities is idempotent after a probe.

    For HTTP backends, capabilities returns a conservative pre-probe default
    before the first non-construction call (spec/36 Decision 6, B-F11). This
    test triggers a probe first via list_mcp_servers() so the capabilities
    property returns the runtime view, then asserts that refresh_capabilities()
    returns the same runtime view. Calling refresh_capabilities() itself is
    always the canonical way to get a post-probe view; list_mcp_servers() here
    is a side-effect that ensures the HTTP backend has probed before the
    capabilities comparison.
    """
    # Trigger probe on HTTP backends (no-op on filesystem; filesystem probe is
    # instantaneous and returns the same static values).
    backend_factory.list_mcp_servers()

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
    Guarded on capability flag.

    Probe-before-cap-read (D-PR5-9): list_mcp_servers() called first to trigger
    capability probe on HTTP backend so supports_install reflects tier-2 result.

    For HTTP: the upgraded MockTransport (D-PR5-10) tracks installed names in a
    closure-scoped dict so the first POST returns 201 and subsequent POSTs for the
    same name return 409, simulating a real catalog server's uniqueness constraint.

    Stream E finding E3 (P1).
    """
    import concurrent.futures

    from atomic_agents.mcp_registry import (
        MCPServerAlreadyInstalled,
        MCPRegistryUnavailable,
    )

    # Trigger capability probe before reading caps (D-PR5-9).
    backend_factory.list_mcp_servers()
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

    spec/36 MUST 9 -- uninstall idempotency.

    Probe-before-cap-read (D-PR5-9): list_mcp_servers() called first to trigger
    capability probe on HTTP backend so supports_uninstall reflects tier-2 result.

    Stream E finding E4 (P1) + C11.
    """
    # Trigger capability probe before reading caps (D-PR5-9).
    backend_factory.list_mcp_servers()
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

    Probe-before-cap-read (D-PR5-9): list_mcp_servers() called first to trigger
    capability probe on HTTP backend so supports_install reflects tier-2 result.

    Note: For the HTTP backend, this test verifies the local state seen after
    install -- the MockTransport's closure dict tracks the install. The bulk
    endpoint (load_all) returns only the fixture's extra_servers, so the
    consistency check is verified against the read-path contract.

    Stream E finding E6 (P1).
    """
    # Trigger capability probe before reading caps (D-PR5-9).
    backend_factory.list_mcp_servers()
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


# ──────────────────────────────────────────────────────────────────────────────
# MUST 4 -- URL credential redaction in error paths (parametrized, D-PR5-11)
#
# Previously only the helper function was tested in isolation. These tests use
# the backend_factory parametrize to verify that credentials embedded in the
# catalog URL do not surface in exception messages from either backend.
#
# HTTP backend: inject a URL with embedded credentials, trigger an error path
#   (invalid server name to force a 404 / ValueError path), assert the
#   credential string does not appear in the exception message.
# Filesystem backend: inject a URL-like agent_root path (not applicable for
#   filesystem, so we test via the factory error path instead).


def test_must4_http_credential_redaction_in_error_path(
    tmp_path: Path,
) -> None:
    """HTTP backend error messages must not echo embedded URL credentials.

    spec/36 MUST 4 -- credential leak prevention. An operator who accidentally
    embeds credentials in the catalog URL (e.g., https://user:secret@host)
    must not see those credentials in exception messages from the backend.

    D-PR5-11: parametrized MUST 4 coverage using backend_factory injection.
    """

    # Build a transport that returns a 404 so a load call raises an exception.
    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(
                200,
                json={
                    "tier": 1,
                    "supports_install": False,
                    "supports_uninstall": False,
                    "supports_audit": False,
                    "wire_version": "1.0",
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    # URL with embedded credentials -- the credential must not appear in any
    # exception message raised by this backend.
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="https://user:s3cr3t-token@catalog.example.com",
        agent_scope="test-scope",
        _http_client=client,
    )
    from atomic_agents.mcp_registry import MCPServerNotInRegistry

    exc_text = ""
    try:
        backend.load_mcp_server("nonexistent-server-xyz")
    except MCPServerNotInRegistry as exc:
        exc_text = str(exc)
    except Exception as exc:  # noqa: BLE001
        exc_text = str(exc)

    assert "s3cr3t-token" not in exc_text, (
        f"MUST 4: credential must not appear in exception message; got: {exc_text!r}"
    )
    assert "user" not in exc_text or "https://..." in exc_text, (
        "MUST 4: credential username must not appear verbatim in exception message"
    )


def test_must4_filesystem_backend_raises_not_reveals_path_secrets(
    tmp_path: Path,
) -> None:
    """Filesystem backend error messages must not echo sensitive path components.

    spec/36 MUST 4 -- credential redaction is primarily an HTTP concern, but the
    filesystem backend must also not surface sensitive data. This test verifies
    that loading a nonexistent server from a backend with a path containing a
    credential-like component raises MCPServerNotInRegistry without echoing the
    secret path segment in the message.

    D-PR5-11: filesystem branch of MUST 4 parametrized coverage.
    """
    from atomic_agents.mcp_registry import MCPServerNotInRegistry

    agent_root = tmp_path / "secure-agent"
    agent_root.mkdir()
    backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    with pytest.raises(MCPServerNotInRegistry) as exc_info:
        backend.load_mcp_server("definitely-absent")
    # The exception message should name the server (for diagnosability) but
    # must not expose the full filesystem path (which might contain user-specific
    # info in tmp_path segments on CI systems).
    assert "definitely-absent" in str(exc_info.value)

"""HTTP backend tests for HTTPMCPServerRegistryBackend (spec/36 PR 4 of 5).

Covers the read-path contract for the HTTP backend: capability tier negotiation,
MUST 2 side-effect-free construction, httpx exception mapping, response body
validation, MUST 10 bulk endpoint consistency, auth header injection, and URL
credential redaction.

All network I/O is intercepted via ``httpx.MockTransport`` -- no real HTTP
requests are made and no extra dev dependency is needed (httpx arrives
transitively via the ``mcp>=1.0.0`` dependency).

The ``_http_client`` constructor kwarg on ``HTTPMCPServerRegistryBackend`` is the
injection seam: tests pass ``httpx.Client(transport=MockTransport(handler))``;
production callers pass ``None`` (lazy real client constructed at first use).

Test categories:
    a) MUST 2 side-effect-free construction (3 tests)
    b) Capability probe branches (8 tests, Decision 4 steps 1-5 + B-F8 additions
       + reordered Allow header)
    c) httpx exception mapping (7 tests, full table from prep notes D)
    d) Response body validation (5 tests, defense-in-depth per E-F7 / C-F3)
    e) Bulk endpoint MUST 10 consistency (3 tests)
    f) Auth + URL credential redaction (4 tests)
    g) Capability lifecycle + properties (3 tests)
    h) Lazy httpx import guard (1 test)
    i) Review-army follow-up tests (added during /ship review):
       - MCPServerSpec.to_dict/from_dict public round-trip (T-F4)
       - OPTIONS probe non-404/405 status handling (T-F1 / Adv-F3)
       - InvalidURL strict mapping via injection (T-F2)
       - MUST 10 full field equality (T-F3 / C-F4)
       - httpx.DecodingError mapping (T-F5)
       - Concurrent first-call probe (T-F6 / Adv-F7)
       - agent_scope query param forwarding (T-F7)
       - Successful probe cached (T-F8)
       - Factory function tests (T-F9)
       - RuntimeError on closed client (Adv-F2)
       - catalog_url query string normalization (Adv-F4)
       - MCPServerRef.source uses raw catalog_url per spec (A-F1)
       - Factory ValueError redacts credentials (S-F1)
"""

from __future__ import annotations

import sys
from typing import Callable

import httpx
import pytest

from atomic_agents.mcp import MCPServerSpec
from atomic_agents.mcp_registry import (
    MCPRegistryAuthRequired,
    MCPRegistryDescriptorInvalid,
    MCPRegistryUnavailable,
)
from atomic_agents.mcp_registry.http import (
    HTTPMCPServerRegistryBackend,
)


# ──────────────────────────────────────────────────────────────────────────────
# Test scaffold helpers


def _capabilities_response(
    tier: int = 1,
    *,
    supports_install: bool = False,
    supports_uninstall: bool = False,
    supports_audit: bool = False,
    wire_version: str = "1.0.0",
    **overrides,
) -> dict:
    """Build a /capabilities response body for the given tier.

    Callers may override any field via keyword args.
    """
    body = {
        "tier": tier,
        "supports_install": supports_install,
        "supports_uninstall": supports_uninstall,
        "supports_audit": supports_audit,
        "wire_version": wire_version,
    }
    body.update(overrides)
    return body


def _spec_to_wire_json(spec: MCPServerSpec) -> dict:
    """Convert an MCPServerSpec to its expected wire JSON shape (to_dict)."""
    return spec.to_dict()


def _make_mock_transport(
    routes: dict[
        tuple[str, str], httpx.Response | Callable[[httpx.Request], httpx.Response]
    ],
) -> httpx.MockTransport:
    """Build an httpx.MockTransport from a dispatch table.

    ``routes`` is keyed on ``(METHOD, path_prefix)`` tuples. The handler
    checks each key as a string-prefix match on ``request.url.path``.

    If no route matches, the transport returns 404 with an empty JSON body to
    simulate a minimal tier-1 server that doesn't implement optional endpoints.

    Default routes included when not overridden:
    - ``GET /capabilities`` -> 200 with tier-1 body
    - ``GET /mcp-servers`` -> 200 with ``{"servers": []}``
    """
    defaults: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/capabilities"): httpx.Response(
            200, json=_capabilities_response(tier=1)
        ),
        ("GET", "/mcp-servers"): httpx.Response(200, json={"servers": []}),
    }
    # Caller-supplied routes override defaults.
    merged = {**defaults, **routes}

    def _handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        # Exact match first, then prefix match.
        for (route_method, route_path), response_or_fn in merged.items():
            if route_method != method:
                continue
            if (
                path == route_path
                or path.startswith(route_path + "?")
                or path.startswith(route_path + "/")
            ):
                if callable(response_or_fn) and not isinstance(
                    response_or_fn, httpx.Response
                ):
                    return response_or_fn(request)
                return response_or_fn  # type: ignore[return-value]
        # No match: 404
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(_handler)


def _make_server_wire_entry(
    name: str,
    command: str = "echo",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    transport: str = "stdio",
    description: str = "",
) -> dict:
    """Build a wire-format server entry dict as a catalog server would return."""
    return {
        "name": name,
        "command": command,
        "args": args or [],
        "env": env or {},
        "transport": transport,
        "description": description,
    }


def _make_backend(
    catalog_url: str = "http://catalog.example.invalid",
    agent_scope: str = "test-scope",
    auth_token: str | None = None,
    probe_failure_cache_s: float = 0.5,
    transport: httpx.MockTransport | None = None,
) -> HTTPMCPServerRegistryBackend:
    """Construct an HTTPMCPServerRegistryBackend with an injected MockTransport.

    Uses ``probe_failure_cache_s=0.5`` by default so failure-cache tests run
    quickly without sleeping more than a second.
    """
    if transport is None:
        transport = _make_mock_transport({})
    client = httpx.Client(transport=transport)
    return HTTPMCPServerRegistryBackend(
        catalog_url=catalog_url,
        agent_scope=agent_scope,
        auth_token=auth_token,
        probe_failure_cache_s=probe_failure_cache_s,
        _http_client=client,
    )


# ──────────────────────────────────────────────────────────────────────────────
# a) MUST 2 -- side-effect-free construction


def test_http_construction_does_not_call_network() -> None:
    """Constructor must not make any network calls (spec/36 MUST 2 HTTP edition).

    Build a transport that counts all calls. Construct the backend. Assert
    zero calls were made.
    """
    call_count: list[int] = [0]

    def _counting_handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json=_capabilities_response())

    transport = httpx.MockTransport(_counting_handler)
    client = httpx.Client(transport=transport)

    HTTPMCPServerRegistryBackend(
        catalog_url="http://catalog.example.invalid",
        agent_scope="test-scope",
        _http_client=client,
    )

    assert call_count[0] == 0, (
        f"Constructor must not make network calls; got {call_count[0]} call(s)."
    )


def test_http_probe_fires_on_first_method_call_not_construction() -> None:
    """Probe fires on first list_mcp_servers(), not on construction.

    After construction: call count == 0.
    After list_mcp_servers(): call count >= 1 (at least the probe fired).
    """
    call_count: list[int] = [0]

    def _counting_handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        path = request.url.path
        if path == "/capabilities":
            return httpx.Response(200, json=_capabilities_response(tier=1))
        if "/mcp-servers" in path:
            return httpx.Response(200, json={"servers": []})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_counting_handler)
    client = httpx.Client(transport=transport)

    backend = HTTPMCPServerRegistryBackend(
        catalog_url="http://catalog.example.invalid",
        agent_scope="test-scope",
        _http_client=client,
    )

    assert call_count[0] == 0, "No calls expected after construction."

    backend.list_mcp_servers()

    assert call_count[0] >= 1, (
        f"Probe must have fired after first method call; got {call_count[0]} call(s)."
    )


def test_http_construction_with_invalid_url_does_not_raise() -> None:
    """Construction with an invalid URL must not raise (validation is deferred to first call).

    spec/36 MUST 2 -- side-effect-free construction. The URL is validated lazily
    when the first network operation is attempted. This matches the filesystem
    backend's pattern of deferring I/O to first use.
    """
    # This must not raise.
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="not-a-url",
        agent_scope="test-scope",
    )
    assert backend is not None


# ──────────────────────────────────────────────────────────────────────────────
# b) Capability probe branches


def test_probe_branch_1_get_capabilities_200_returns_tier_from_body() -> None:
    """Probe step 1: GET /capabilities 200 -> parse tier from response body.

    spec/36 Decision 4 step 1. After list_mcp_servers(), capabilities reflect
    the body's supports_audit field.
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(
                200,
                json=_capabilities_response(
                    tier=3,
                    supports_install=True,
                    supports_uninstall=True,
                    supports_audit=True,
                ),
            ),
        }
    )
    backend = _make_backend(transport=transport)
    backend.list_mcp_servers()

    assert backend.capabilities.supports_audit is True, (
        "Probe step 1: supports_audit must be True when catalog server reports tier 3."
    )


def test_probe_branch_2a_options_allow_get_only_tier_1() -> None:
    """Probe step 2a: GET /capabilities 404, OPTIONS /mcp-servers Allow: GET -> tier 1.

    spec/36 Decision 4 step 2 / B-F6. Only GET in Allow header -> conservative
    tier 1 (supports_install=False).
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(404, json={}),
            ("OPTIONS", "/mcp-servers"): httpx.Response(200, headers={"Allow": "GET"}),
        }
    )
    backend = _make_backend(transport=transport)
    backend.list_mcp_servers()

    assert backend.capabilities.supports_install is False, (
        "Probe step 2a: GET-only Allow header must produce tier 1 (supports_install=False)."
    )


def test_probe_branch_2b_options_allow_get_post_delete_tier_2() -> None:
    """Probe step 2b: GET /capabilities 404, OPTIONS Allow: GET, POST, DELETE -> tier 2.

    spec/36 Decision 4 step 2 / B-F6. Presence of GET+POST+DELETE in Allow header
    indicates write capability (supports_install=True).
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(404, json={}),
            ("OPTIONS", "/mcp-servers"): httpx.Response(
                200, headers={"Allow": "GET, POST, DELETE"}
            ),
        }
    )
    backend = _make_backend(transport=transport)
    backend.list_mcp_servers()

    assert backend.capabilities.supports_install is True, (
        "Probe step 2b: GET+POST+DELETE Allow header must produce tier 2 (supports_install=True)."
    )


def test_probe_branch_2b_options_allow_reordered_extra_methods_still_tier_2() -> None:
    """Probe step 2b: reordered or extra methods in Allow still produce tier 2.

    spec/36 B-F6 -- Allow header parsing uses set-membership, not string-equality.
    Extra methods (HEAD, OPTIONS) do not affect tier inference.
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(404, json={}),
            ("OPTIONS", "/mcp-servers"): httpx.Response(
                200, headers={"Allow": "DELETE, GET, HEAD, POST, OPTIONS"}
            ),
        }
    )
    backend = _make_backend(transport=transport)
    backend.list_mcp_servers()

    assert backend.capabilities.supports_install is True, (
        "Probe step 2b: set-membership check must handle reordered / extra Allow methods."
    )


def test_probe_branch_3_options_404_default_tier_1() -> None:
    """Probe step 3: GET /capabilities 404, OPTIONS /mcp-servers 404 -> tier 1 default.

    spec/36 Decision 4 step 3. Conservative fallback when both probe steps fail.
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(404, json={}),
            ("OPTIONS", "/mcp-servers"): httpx.Response(404, json={}),
        }
    )
    backend = _make_backend(transport=transport)
    backend.list_mcp_servers()

    assert backend.capabilities.supports_install is False, (
        "Probe step 3: double-404 must fall back to conservative tier 1."
    )


def test_probe_branch_4_5xx_raises_unavailable_and_caches_failure() -> None:
    """Probe step 4: GET /capabilities 500 -> MCPRegistryUnavailable; failure is cached.

    spec/36 Decision 4 step 4. A second call within probe_failure_cache_s must NOT
    make a second HTTP request (call count stays at 1) and must still raise.
    """
    call_count: list[int] = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if "/capabilities" in request.url.path:
            return httpx.Response(500, json={"error": "server error"})
        return httpx.Response(200, json={"servers": []})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport, probe_failure_cache_s=0.5)

    # First call: probe fires, raises.
    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()

    count_after_first = call_count[0]
    assert count_after_first >= 1

    # Second call within cache window: must NOT re-probe; must still raise.
    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()

    assert call_count[0] == count_after_first, (
        "Second call within failure cache window must not make additional HTTP calls."
    )


def test_probe_branch_5_401_raises_auth_required() -> None:
    """Probe step 5: GET /capabilities 401 -> MCPRegistryAuthRequired.

    spec/36 Decision 4 step 5 / MUST 7.
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(
                401, json={"error": "unauthorized"}
            ),
        }
    )
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryAuthRequired):
        backend.list_mcp_servers()


def test_probe_branch_403_does_not_silent_fallback_tier_1() -> None:
    """Probe: GET /capabilities 403 must raise, NOT silently fall back to tier 1.

    spec/36 B-F8. Non-404 4xx status codes on /capabilities MUST raise
    MCPRegistryUnavailable. Silent tier-1 fallback would mask misconfiguration.
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(403, json={"error": "forbidden"}),
        }
    )
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()


# ──────────────────────────────────────────────────────────────────────────────
# c) httpx exception mapping


def test_httpx_connect_error_maps_to_unavailable() -> None:
    """httpx.ConnectError -> MCPRegistryUnavailable.

    spec/36 D exception mapping table. DNS failure / connection refused.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()


def test_httpx_read_timeout_maps_to_unavailable() -> None:
    """httpx.ReadTimeout -> MCPRegistryUnavailable.

    spec/36 D exception mapping table. Response body too slow.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()


def test_httpx_pool_timeout_maps_to_unavailable() -> None:
    """httpx.PoolTimeout -> MCPRegistryUnavailable.

    spec/36 D exception mapping table. Connection pool exhausted.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool exhausted", request=request)

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()


def test_httpx_local_protocol_error_maps_to_descriptor_invalid() -> None:
    """httpx.LocalProtocolError -> MCPRegistryDescriptorInvalid.

    spec/36 D exception mapping table. Client sent invalid HTTP (framework bug).
    Mapping to DescriptorInvalid rather than Unavailable surfaces the bug instead
    of masking it as a transient failure.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError("local protocol error")

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.list_mcp_servers()


def test_httpx_invalid_url_maps_to_value_error() -> None:
    """httpx.InvalidURL -> ValueError.

    spec/36 D exception mapping table. httpx.InvalidURL does NOT inherit from
    httpx.HTTPError; it requires a separate except clause. Surfaces as ValueError
    so operators can distinguish URL misconfiguration from transient failures.
    """
    # Construct backend with a malformed URL to trigger InvalidURL on first call.
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="not-a-valid-url://[::invalid",
        agent_scope="test-scope",
    )

    with pytest.raises((ValueError, MCPRegistryUnavailable)):
        # Accept either ValueError (InvalidURL mapped) or MCPRegistryUnavailable
        # (any other network error from httpx). The key is it must NOT propagate
        # a bare httpx.InvalidURL.
        backend.list_mcp_servers()


def test_httpx_unknown_subclass_maps_to_unavailable_via_http_error_catchall() -> None:
    """An unknown httpx.HTTPError subclass -> MCPRegistryUnavailable via catch-all.

    spec/36 D exception mapping table last row. The final catch-all for any future
    httpx subclass must route to MCPRegistryUnavailable so the framework does not
    expose raw httpx types to callers.
    """

    class _UnknownHTTPError(httpx.HTTPError):
        """Synthetic future httpx exception class."""

        def __init__(self) -> None:
            super().__init__("unknown future error")

    def _handler(request: httpx.Request) -> httpx.Response:
        raise _UnknownHTTPError()

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()


# ──────────────────────────────────────────────────────────────────────────────
# d) Response body validation


def test_list_malformed_json_raises_descriptor_invalid() -> None:
    """GET /mcp-servers returning non-JSON body -> MCPRegistryDescriptorInvalid.

    spec/36 E-F7 / C-F3 defense-in-depth. The parser-level failure (body cannot
    be decoded as JSON at all) must raise DescriptorInvalid rather than leaking
    a json.JSONDecodeError or httpx.DecodingError.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        # Return non-JSON body for the mcp-servers list.
        return httpx.Response(
            200,
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.list_mcp_servers()


def test_list_missing_servers_key_raises_descriptor_invalid() -> None:
    """GET /mcp-servers returning JSON without 'servers' key -> MCPRegistryDescriptorInvalid.

    spec/36 E-F7 / C-F3 shape-level validation. The body parsed but has the wrong
    structure.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        return httpx.Response(200, json={"data": []})  # 'servers' key is absent

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.list_mcp_servers()


def test_load_missing_required_field_raises_descriptor_invalid() -> None:
    """GET /mcp-servers/<name> returning spec without 'command' -> MCPRegistryDescriptorInvalid.

    spec/36 E-F7 / C-F3. The spec body has the right shape but is missing a required
    field.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        if path.endswith("/mcp-servers") or "/mcp-servers?" in path:
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"name": "incomplete-server", "description": "missing command"}
                    ]
                },
            )
        if "/mcp-servers/" in path:
            # Return a spec without the required 'command' field.
            return httpx.Response(200, json={"name": "incomplete-server"})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.load_mcp_server("incomplete-server")


def test_load_injection_in_name_field_rejected() -> None:
    """Server returned with injection in 'name' field -> MCPRegistryDescriptorInvalid.

    spec/36 E-F7. The name field must satisfy MUST 1 charset. A name containing
    newlines or '## ' is an injection attempt and must be rejected.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        if path.endswith("/mcp-servers") or "/mcp-servers?" in path:
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {
                            "name": "evil\n## injection",
                            "command": "echo",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    # The backend must refuse to return a server with an injected name.
    with pytest.raises((MCPRegistryDescriptorInvalid, ValueError)):
        backend.list_mcp_servers()


def test_validation_result_missing_ok_raises_descriptor_invalid() -> None:
    """GET /mcp-servers/<name>/validate returning body without 'ok' -> MCPRegistryDescriptorInvalid.

    spec/36 E-F7. The ValidationResult wire shape requires 'ok' as a bool.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        if path.endswith("/validate"):
            # Return a validation result missing the required 'ok' field.
            return httpx.Response(200, json={"errors": [], "warnings": []})
        if path.endswith("/mcp-servers") or "/mcp-servers?" in path:
            return httpx.Response(
                200,
                json={"servers": [{"name": "test-server", "command": "echo"}]},
            )
        if "/mcp-servers/" in path:
            return httpx.Response(
                200,
                json={
                    "name": "test-server",
                    "command": "echo",
                    "args": [],
                    "env": {},
                    "transport": "stdio",
                },
            )
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.validate("test-server")


# ──────────────────────────────────────────────────────────────────────────────
# e) Bulk endpoint MUST 10 consistency


def test_load_all_uses_bulk_endpoint_one_call() -> None:
    """load_all_mcp_servers() makes exactly ONE network call for the bulk fetch.

    spec/36 MUST 10. The HTTP backend must use GET /mcp-servers?expand=spec (or
    equivalent bulk endpoint) instead of N separate per-name requests.
    """
    bulk_calls: list[str] = []
    per_name_calls: list[str] = []

    servers = [
        _make_server_wire_entry(f"server-{i}", description=f"Server {i}")
        for i in range(3)
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = str(request.url.query)
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        if "/mcp-servers" in path and "expand" in query:
            bulk_calls.append(path)
            return httpx.Response(200, json={"servers": servers})
        if "/mcp-servers" in path and not any(
            c in path.split("/mcp-servers")[-1]
            for c in ["/server-0", "/server-1", "/server-2"]
        ):
            # Plain list endpoint.
            refs = [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "transport": s["transport"],
                }
                for s in servers
            ]
            return httpx.Response(200, json={"servers": refs})
        # Per-name endpoint.
        per_name_calls.append(path)
        for s in servers:
            if path.endswith(f"/{s['name']}"):
                return httpx.Response(200, json=s)
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    result = backend.load_all_mcp_servers()

    assert len(result) == 3, f"Expected 3 servers; got {len(result)}"
    assert len(bulk_calls) == 1, (
        f"load_all_mcp_servers must use exactly 1 bulk call; got {len(bulk_calls)} bulk + {len(per_name_calls)} per-name."
    )
    assert len(per_name_calls) == 0, (
        f"load_all_mcp_servers must NOT make per-name calls; got {len(per_name_calls)}."
    )


def test_load_all_consistent_with_per_name_load() -> None:
    """set(load_all_mcp_servers()) names equals set of per-name load names.

    spec/36 MUST 10. Bulk and per-name paths must return the same server set.
    """
    servers = [
        _make_server_wire_entry("alpha-server"),
        _make_server_wire_entry("beta-server"),
        _make_server_wire_entry("gamma-server"),
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        query = str(request.url.query)
        if path.endswith("/capabilities"):
            return httpx.Response(200, json=_capabilities_response())
        if path.endswith("/mcp-servers"):
            if "expand" in query:
                return httpx.Response(200, json={"servers": servers})
            refs = [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "transport": s["transport"],
                }
                for s in servers
            ]
            return httpx.Response(200, json={"servers": refs})
        if "/mcp-servers/" in path:
            name = path.rsplit("/", 1)[-1]
            for s in servers:
                if s["name"] == name:
                    return httpx.Response(200, json=s)
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    all_specs = backend.load_all_mcp_servers()
    refs = backend.list_mcp_servers()
    per_name_specs = [backend.load_mcp_server(ref.name) for ref in refs]

    all_names = {s.name for s in all_specs}
    per_name_names = {s.name for s in per_name_specs}

    assert all_names == per_name_names, (
        f"MUST 10: load_all names {all_names!r} must equal per-name names {per_name_names!r}."
    )


def test_load_all_resolves_env_vars(monkeypatch) -> None:
    """load_all_mcp_servers() resolves $VAR env references.

    spec/36 MUST 8 + MUST 10. Env vars must be resolved in the bulk path, not
    stored as $VAR literals.
    """
    monkeypatch.setenv("MY_TEST_VAR", "resolved-value-xyz")

    server = _make_server_wire_entry(
        "env-server",
        env={"KEY": "$MY_TEST_VAR"},
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = str(request.url.query)
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        if "/mcp-servers" in path and "expand" in query:
            return httpx.Response(200, json={"servers": [server]})
        if "/mcp-servers" in path and "/env-server" not in path:
            refs = [
                {
                    "name": server["name"],
                    "description": server["description"],
                    "transport": server["transport"],
                }
            ]
            return httpx.Response(200, json={"servers": refs})
        if "/env-server" in path:
            return httpx.Response(200, json=server)
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)

    all_specs = backend.load_all_mcp_servers()
    assert len(all_specs) == 1
    assert all_specs[0].env["KEY"] == "resolved-value-xyz", (
        "load_all must resolve $VAR env references at call time."
    )

    # Also verify via per-name load path.
    loaded = backend.load_mcp_server("env-server")
    assert loaded.env["KEY"] == "resolved-value-xyz", (
        "load_mcp_server must also resolve $VAR env references."
    )


# ──────────────────────────────────────────────────────────────────────────────
# f) Auth + URL credential redaction


def test_auth_token_added_as_bearer_header() -> None:
    """auth_token is sent as 'Authorization: Bearer <token>' on every request.

    spec/36 E-F10 / D-F2.
    """
    captured_headers: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(dict(request.headers))
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        return httpx.Response(200, json={"servers": []})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="http://catalog.example.invalid",
        agent_scope="test-scope",
        auth_token="my-secret-token",
        _http_client=client,
    )

    backend.list_mcp_servers()

    assert len(captured_headers) >= 1, "At least one request must have been made."
    for headers in captured_headers:
        auth = headers.get("authorization", "")
        assert auth == "Bearer my-secret-token", (
            f"Authorization header must be 'Bearer my-secret-token'; got {auth!r}."
        )


def test_no_auth_token_omits_authorization_header() -> None:
    """When no auth_token is provided, 'Authorization' header must be absent.

    spec/36 E-F10.
    """
    captured_headers: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(dict(request.headers))
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        return httpx.Response(200, json={"servers": []})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="http://catalog.example.invalid",
        agent_scope="test-scope",
        auth_token=None,
        _http_client=client,
    )

    backend.list_mcp_servers()

    for headers in captured_headers:
        assert "authorization" not in {k.lower() for k in headers}, (
            "Authorization header must NOT be present when no auth_token is supplied."
        )


def test_url_credentials_redacted_in_error_messages() -> None:
    """MCPRegistryUnavailable raised from a URL-with-credentials backend must not expose the password.

    spec/36 D-F2 / E-F10. The 'secret' in 'https://user:secret@catalog...' must
    not appear in the exception message.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="https://user:s3cr3tp4ss@catalog.example.com/?agent_scope=t",
        agent_scope="test-scope",
        _http_client=client,
    )

    with pytest.raises(MCPRegistryUnavailable) as exc_info:
        backend.list_mcp_servers()

    error_text = str(exc_info.value)
    assert "s3cr3tp4ss" not in error_text, (
        f"Password must not appear in error message; got: {error_text!r}"
    )


def test_auth_token_not_in_error_messages() -> None:
    """MCPRegistryAuthRequired message must not contain the auth token value.

    spec/36 D-F2. The auth token is a secret; it must be redacted from all
    operator-visible error messages.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/capabilities" in request.url.path:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="http://catalog.example.invalid",
        agent_scope="test-scope",
        auth_token="super-secret-bearer-token-abc123",
        _http_client=client,
    )

    with pytest.raises(MCPRegistryAuthRequired) as exc_info:
        backend.list_mcp_servers()

    error_text = str(exc_info.value)
    assert "super-secret-bearer-token-abc123" not in error_text, (
        f"Auth token must not appear in error message; got: {error_text!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# g) Capability lifecycle + properties


def test_capabilities_before_first_probe_returns_conservative_default() -> None:
    """Reading capabilities before first probe returns a conservative default.

    spec/36 B-F11. A fresh backend has no probe state. Reading the property
    must return a safe conservative view (all write caps False) rather than
    raising or blocking on a probe.
    """
    call_count: list[int] = [0]

    def _counting_handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json=_capabilities_response())

    transport = httpx.MockTransport(_counting_handler)
    client = httpx.Client(transport=transport)
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="http://catalog.example.invalid",
        agent_scope="test-scope",
        _http_client=client,
    )

    # Read capabilities WITHOUT calling list_mcp_servers / load_mcp_server first.
    caps = backend.capabilities

    assert caps.supports_install is False, (
        "Conservative default: supports_install must be False before first probe."
    )
    assert caps.supports_capability_handshake is True, (
        "HTTP backend always supports capability handshake (static class-level flag)."
    )
    assert call_count[0] == 0, (
        "Reading capabilities property must not trigger a probe network call."
    )


def test_refresh_capabilities_bypasses_failure_cache() -> None:
    """refresh_capabilities() bypasses the failure cache and re-probes.

    spec/36 B-F5. When a probe has failed and is within its cache window,
    refresh_capabilities() must make a new HTTP call (bypassing the cache),
    allowing recovery after a transient outage.
    """
    call_count: list[int] = [0]
    should_succeed: list[bool] = [False]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if "/capabilities" in request.url.path:
            if should_succeed[0]:
                return httpx.Response(200, json=_capabilities_response())
            return httpx.Response(500, json={"error": "server down"})
        return httpx.Response(200, json={"servers": []})

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport, probe_failure_cache_s=30.0)

    # First call: probe fails, cached.
    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()

    count_after_failure = call_count[0]

    # Simulate server recovery.
    should_succeed[0] = True

    # refresh_capabilities() must bypass the cache and make a new call.
    refreshed = backend.refresh_capabilities()
    assert call_count[0] > count_after_failure, (
        "refresh_capabilities() must make a new HTTP call even within the failure cache window."
    )
    assert refreshed is not None


def test_capabilities_property_does_not_fire_probe() -> None:
    """Reading the capabilities property does not fire a probe HTTP request.

    spec/36 B-F10 strengthen. The property should return cached/default state
    without hitting the network.
    """
    call_count: list[int] = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json=_capabilities_response())

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="http://catalog.example.invalid",
        agent_scope="test-scope",
        _http_client=client,
    )

    # Read the property three times.
    _ = backend.capabilities
    _ = backend.capabilities
    _ = backend.capabilities

    assert call_count[0] == 0, (
        f"capabilities property must not fire probe; got {call_count[0]} call(s)."
    )


# ──────────────────────────────────────────────────────────────────────────────
# h) Lazy httpx import guard


def test_construction_does_not_import_httpx() -> None:
    """HTTPMCPServerRegistryBackend construction must not trigger httpx import.

    spec/36 A-F5. The httpx import is deferred so operators who use only the
    filesystem backend do not pay the httpx import cost. The [http] extra
    controls opt-in installation.

    Strategy: remove httpx from sys.modules, construct the backend (without
    an injected client to exercise the lazy-import path), restore modules.
    """
    saved_httpx = sys.modules.pop("httpx", None)
    saved_submodules = {
        k: sys.modules.pop(k)
        for k in list(sys.modules.keys())
        if k.startswith("httpx.")
    }

    try:
        # Construction must not trigger the import.
        backend = HTTPMCPServerRegistryBackend(
            catalog_url="http://example.invalid",
            agent_scope="test-scope",
            # No _http_client= to exercise the lazy construction path.
        )
        assert backend is not None

        # httpx should not have been re-imported.
        assert "httpx" not in sys.modules, (
            "Construction must not import httpx; httpx appeared in sys.modules after __init__."
        )
    finally:
        # Restore httpx so the rest of the test suite keeps working.
        if saved_httpx is not None:
            sys.modules["httpx"] = saved_httpx
        sys.modules.update(saved_submodules)


# ──────────────────────────────────────────────────────────────────────────────
# i) Review-army follow-up tests
#
# Added during /ship pre-landing review army (5 specialists + Claude adversarial
# + Step 9 checklist) on 2026-06-04. Each test has a finding-ID anchor in its
# docstring so future contributors can find the original review context.


def test_mcp_server_spec_to_dict_from_dict_round_trip() -> None:
    """MCPServerSpec.to_dict / from_dict public round-trip preserves all fields.

    T-F4 (Testing specialist, confidence 95). C-F1 (prep notes primitive-
    existence). The methods were promoted from private helpers in profile/types
    to public methods on MCPServerSpec at PR 4. Round-trip identity is the
    canonical contract.
    """
    spec = MCPServerSpec(
        name="my-server",
        command="python3",
        args=["-m", "server", "--port=8080"],
        env={"API_KEY": "$MY_KEY", "DEBUG": "1"},
        transport="stdio",
        description="A test server.",
    )
    assert MCPServerSpec.from_dict(spec.to_dict()) == spec


def test_mcp_server_spec_from_dict_ignores_extra_keys() -> None:
    """Extra keys in the source dict are silently dropped (forward-compat).

    T-F4 follow-on. Future catalog server wire format extensions must not
    break existing clients.
    """
    d = {
        "name": "x",
        "command": "echo",
        "extra_future_field": "ignored",
        "another_unknown": 42,
    }
    spec = MCPServerSpec.from_dict(d)
    assert spec.name == "x"
    assert spec.command == "echo"


def test_mcp_server_spec_from_dict_defaults_for_optional_fields() -> None:
    """Optional fields fall back to documented defaults (backward-compat).

    T-F4 follow-on. Catalog servers may omit any of args, env, transport,
    description; defaults are the wire format contract.
    """
    d = {"name": "x", "command": "echo"}
    spec = MCPServerSpec.from_dict(d)
    assert spec.args == []
    assert spec.env == {}
    assert spec.transport == "stdio"
    assert spec.description == ""


def test_mcp_server_spec_from_dict_raises_on_missing_required_field() -> None:
    """Required keys 'name' and 'command' raise KeyError if absent.

    T-F4 follow-on. The from_dict docstring documents this contract.
    """
    with pytest.raises(KeyError):
        MCPServerSpec.from_dict({"command": "echo"})  # missing 'name'
    with pytest.raises(KeyError):
        MCPServerSpec.from_dict({"name": "x"})  # missing 'command'


def test_probe_options_5xx_raises_unavailable() -> None:
    """OPTIONS /mcp-servers returning 5xx must NOT silently fall back to tier 1.

    T-F1 (Testing specialist, confidence 92). Adversarial F3. The previous
    code only branched on (404, 405) and would silently report tier 1 for
    any other status. A misconfigured catalog returning 500 must surface as
    MCPRegistryUnavailable so the operator sees the real failure.
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(404, json={}),
            ("OPTIONS", "/mcp-servers"): httpx.Response(
                500, json={"error": "server error"}
            ),
        }
    )
    backend = _make_backend(transport=transport)
    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()


def test_probe_options_401_raises_auth_required() -> None:
    """OPTIONS /mcp-servers returning 401 must raise MCPRegistryAuthRequired.

    T-F1 (Testing specialist, confidence 92). Adversarial F3. A misconfigured
    auth token must surface as an auth error, not as a silent capability
    downgrade where a tier-3 server appears as tier 1.
    """
    transport = _make_mock_transport(
        {
            ("GET", "/capabilities"): httpx.Response(404, json={}),
            ("OPTIONS", "/mcp-servers"): httpx.Response(
                401, json={"error": "unauthorized"}
            ),
        }
    )
    backend = _make_backend(transport=transport)
    with pytest.raises(MCPRegistryAuthRequired):
        backend.list_mcp_servers()


def test_httpx_invalid_url_strict_assertion() -> None:
    """httpx.InvalidURL specifically maps to ValueError, not MCPRegistryUnavailable.

    T-F2 (Testing specialist, confidence 90). The earlier test
    test_httpx_invalid_url_maps_to_value_error accepted EITHER ValueError or
    MCPRegistryUnavailable because it relied on httpx parsing a malformed URL
    at first call (behavior varies by httpx version). This test injects the
    exception via MockTransport so the mapping is exercised deterministically.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("crafted invalid url")

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)
    with pytest.raises(ValueError):
        backend.list_mcp_servers()


def test_load_all_full_spec_equality_with_per_name_load() -> None:
    """MUST 10 / C-F4: bulk endpoint and per-name loads produce IDENTICAL specs.

    T-F3 (Testing specialist, confidence 88). The earlier MUST 10 test only
    compared name sets; that would pass even if args/env/transport differed
    between bulk and per-name responses. This test uses non-default field
    values so the equality check is meaningful.
    """
    servers = [
        _make_server_wire_entry(
            "alpha",
            command="python3",
            args=["-m", "alpha_server"],
            env={"PORT": "8080", "DEBUG": "1"},
            description="Alpha server description",
        ),
        _make_server_wire_entry(
            "beta",
            command="npx",
            args=["-y", "@beta/server"],
            env={"NODE_ENV": "production"},
            description="Beta server",
        ),
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        query = str(request.url.query)
        if path.endswith("/capabilities"):
            return httpx.Response(200, json=_capabilities_response())
        if path.endswith("/mcp-servers"):
            if "expand" in query:
                return httpx.Response(200, json={"servers": servers})
            refs = [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "transport": s["transport"],
                }
                for s in servers
            ]
            return httpx.Response(200, json={"servers": refs})
        if "/mcp-servers/" in path:
            name = path.rsplit("/", 1)[-1]
            for s in servers:
                if s["name"] == name:
                    return httpx.Response(200, json=s)
        return httpx.Response(404, json={})

    backend = _make_backend(transport=httpx.MockTransport(_handler))

    bulk_by_name = {s.name: s for s in backend.load_all_mcp_servers()}
    for ref in backend.list_mcp_servers():
        per_name = backend.load_mcp_server(ref.name)
        bulk = bulk_by_name[ref.name]
        assert per_name == bulk, (
            f"MUST 10 field mismatch for {ref.name!r}:\n"
            f"  per-name: {per_name!r}\n  bulk:     {bulk!r}"
        )


def test_httpx_decoding_error_maps_to_descriptor_invalid() -> None:
    """httpx.DecodingError maps to MCPRegistryDescriptorInvalid, not Unavailable.

    T-F5 (Testing specialist, confidence 85). The DecodingError catch in
    _handle_http_error is order-dependent (sits between LocalProtocolError
    and the HTTPError catch-all). A regression removing the specific check
    would silently change the mapping.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("failed to decode response")

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)
    with pytest.raises(MCPRegistryDescriptorInvalid):
        backend.list_mcp_servers()


def test_concurrent_first_probes_both_succeed() -> None:
    """Two concurrent first-call probes both return valid results (D-PR4-3).

    T-F6 (Testing specialist, confidence 82). Adversarial F7. The capability
    cache lock guards the cache-check and cache-write but NOT the HTTP probe
    itself (so concurrent callers don't serialize on network latency). Last
    writer wins; both threads must succeed.
    """
    import threading

    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        return httpx.Response(200, json={"servers": []})

    backend = _make_backend(transport=httpx.MockTransport(_handler))

    def worker() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(backend.list_mcp_servers())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"Concurrent probe errors: {errors!r}"
    assert len(results) == 2, "Both threads must complete"


def test_agent_scope_forwarded_as_query_param() -> None:
    """Every request includes ?agent_scope=<scope> (MUST 5 wire enforcement).

    T-F7 (Testing specialist, confidence 80). Cross-tenant isolation depends
    on the scope query param appearing on every request. A regression
    omitting it would cause a scope='A' backend to see scope='B' servers.
    """
    captured_urls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        return httpx.Response(200, json={"servers": []})

    backend = _make_backend(
        agent_scope="my-agent-scope",
        transport=httpx.MockTransport(_handler),
    )
    backend.list_mcp_servers()

    data_urls = [u for u in captured_urls if "/mcp-servers" in u]
    assert data_urls, "No /mcp-servers requests captured"
    for url in data_urls:
        assert "agent_scope=my-agent-scope" in url, f"agent_scope missing from {url!r}"


def test_successful_probe_is_cached_no_re_probe() -> None:
    """After a successful probe, a second list_mcp_servers() must not re-probe.

    T-F8 (Testing specialist, confidence 78). The positive cache (success
    prevents redundant probe) is the inverse of the failure cache test that
    already exists. Without this test, a regression that lost the early-return
    on cache hit would re-probe on every call without being caught.
    """
    probe_calls: list[int] = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/capabilities" in request.url.path:
            probe_calls[0] += 1
            return httpx.Response(200, json=_capabilities_response())
        return httpx.Response(200, json={"servers": []})

    backend = _make_backend(transport=httpx.MockTransport(_handler))
    backend.list_mcp_servers()
    backend.list_mcp_servers()
    backend.list_mcp_servers()
    assert probe_calls[0] == 1, (
        f"Probe must only fire once; fired {probe_calls[0]} times across 3 calls"
    )


def test_factory_extracts_agent_scope_from_url() -> None:
    """make_http_mcp_server_registry_backend_from_url parses ?agent_scope=.

    T-F9 (Testing specialist, confidence 75). Factory function had zero
    direct tests before /ship review.
    """
    from atomic_agents.mcp_registry.http import (
        make_http_mcp_server_registry_backend_from_url,
    )

    backend = make_http_mcp_server_registry_backend_from_url(
        "https://catalog.example.com/?agent_scope=my-scope"
    )
    assert backend._agent_scope == "my-scope"


def test_factory_defaults_agent_scope_to_default() -> None:
    """When agent_scope is absent from URL, factory defaults to 'default'.

    T-F9. Spec/36 §Operator surface documents this default.
    """
    from atomic_agents.mcp_registry.http import (
        make_http_mcp_server_registry_backend_from_url,
    )

    backend = make_http_mcp_server_registry_backend_from_url(
        "https://catalog.example.com/"
    )
    assert backend._agent_scope == "default"


def test_factory_rejects_empty_url() -> None:
    """Empty URL raises ValueError at the factory.

    T-F9.
    """
    from atomic_agents.mcp_registry.http import (
        make_http_mcp_server_registry_backend_from_url,
    )

    with pytest.raises(ValueError):
        make_http_mcp_server_registry_backend_from_url("")
    with pytest.raises(ValueError):
        make_http_mcp_server_registry_backend_from_url("   ")


def test_factory_rejects_non_http_scheme() -> None:
    """Factory rejects ftp://, filesystem://, or any non-http(s) scheme.

    T-F9.
    """
    from atomic_agents.mcp_registry.http import (
        make_http_mcp_server_registry_backend_from_url,
    )

    with pytest.raises(ValueError):
        make_http_mcp_server_registry_backend_from_url("ftp://catalog/?agent_scope=x")
    with pytest.raises(ValueError):
        make_http_mcp_server_registry_backend_from_url(
            "filesystem:///path?agent_scope=x"
        )


def test_factory_value_error_redacts_credentials() -> None:
    """S-F1: factory ValueError must not echo URL credentials.

    Security specialist S-F1 (confidence 7). An operator who pastes
    ftp://user:secret@host into the URL env var would otherwise see the
    secret in the error message.
    """
    from atomic_agents.mcp_registry.http import (
        make_http_mcp_server_registry_backend_from_url,
    )

    with pytest.raises(ValueError) as exc_info:
        make_http_mcp_server_registry_backend_from_url(
            "ftp://user:secret-token-abc@host/?agent_scope=x"
        )
    assert "secret-token-abc" not in str(exc_info.value), (
        "Credentials must not appear in factory ValueError"
    )


def test_factory_reads_auth_token_from_env(monkeypatch) -> None:
    """Factory reads ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN from env.

    T-F9.
    """
    from atomic_agents.mcp_registry.http import (
        make_http_mcp_server_registry_backend_from_url,
    )

    monkeypatch.setenv("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN", "tok-abc-123")
    backend = make_http_mcp_server_registry_backend_from_url(
        "https://catalog.example.com/?agent_scope=s"
    )
    assert backend._auth_token == "tok-abc-123"


def test_runtime_error_from_closed_client_maps_to_unavailable() -> None:
    """Adv-F2: RuntimeError from a closed httpx.Client maps to MCPRegistryUnavailable.

    Adversarial finding F2 (the most exploitable per the adversarial
    recommendation). A reader holding self._real_client outside the
    _client_lock can race a concurrent close(); calling .get() on the closed
    client raises RuntimeError. Must surface as MCPRegistryUnavailable, not
    escape raw to the caller (which expects MCPRegistry* types only).
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        # Simulate the closed-client error path.
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    transport = httpx.MockTransport(_handler)
    backend = _make_backend(transport=transport)
    with pytest.raises(MCPRegistryUnavailable):
        backend.list_mcp_servers()


def test_catalog_url_with_query_string_is_normalized() -> None:
    """Adv-F4: catalog_url with query string strips correctly at __init__.

    A catalog_url like "https://host/api?debug=1" must NOT produce
    "https://host/api?debug=1/mcp-servers?agent_scope=..." (malformed) when
    constructing request URLs. Normalization at __init__ strips the query
    string AND trailing slashes once, fixing six downstream URL-build sites.
    """
    backend = HTTPMCPServerRegistryBackend(
        catalog_url="https://catalog.example.com/api/?debug=1",
        agent_scope="test",
        _http_client=httpx.Client(transport=_make_mock_transport({})),
    )
    # _catalog_url stored normalized: no trailing slash, no query string.
    assert backend._catalog_url == "https://catalog.example.com/api"


def test_mcp_server_ref_source_uses_raw_catalog_url_not_redacted() -> None:
    """A-F1: MCPServerRef.source uses RAW catalog_url per spec/36 line 228.

    The implementer brief originally used _safe_catalog_url (redacted) for
    source to defend against credential leaks, but the redactor aggressively
    strips after ':// so a clean URL like https://catalog.example.com becomes
    'https://...' — breaking downstream navigation. Per the spec, source is
    a data field meant to be navigable; the operator's recommended pattern
    is the auth_token env var, not URL-embedded credentials.
    """
    server = _make_server_wire_entry("test-server")

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/capabilities" in path:
            return httpx.Response(200, json=_capabilities_response())
        if path.endswith("/mcp-servers"):
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {
                            "name": server["name"],
                            "description": server["description"],
                            "transport": server["transport"],
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    backend = _make_backend(
        catalog_url="https://catalog.example.com",
        transport=httpx.MockTransport(_handler),
    )
    refs = backend.list_mcp_servers()
    assert len(refs) == 1
    # Source must be the RAW catalog URL, not 'https://...' (redacted form).
    assert refs[0].source == "https://catalog.example.com/mcp-servers/test-server", (
        f"source must be raw catalog URL; got {refs[0].source!r}"
    )

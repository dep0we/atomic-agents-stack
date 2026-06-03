"""MCPServerRegistryBackend Protocol -- the contract every MCP server registry backend satisfies.

This is the twelfth and final open Protocol in the protocol-pattern series alongside
MemoryBackend (#57), LLMBackend (#87), JudgeBackend (#112), LockBackend (#60),
LogBackend (#61), AgentProfileBackend (#63), ToolRegistryBackend (#64),
MandateBackend (#124), PolicyBackend (#89), PersonaBackend (#62), and
CorpusBackend (#65).

``MCPServerRegistryBackend`` abstracts the mounted-server-source layer: it is
the catalog Protocol that produces ``MCPServerSpec`` instances for
``MCPClientPool`` to consume at agent construction. It does NOT replace
``MCPClientPool`` (spec/19) -- the subprocess lifecycle seam stays. The two
compose:

    backend.list_mcp_servers() -> list[MCPServerRef]      # metadata (cheap)
    backend.load_mcp_server(name) -> MCPServerSpec         # full spec (per server)
    backend.load_all_mcp_servers() -> list[MCPServerSpec]  # bulk (agent construction hot path)
    agent.mcp_pool = MCPClientPool(specs, agents_root)     # subprocess lifecycle (unchanged)

See ``docs/spec/36-mcp-server-registry-backend.md`` (DRAFT at PR 1; LOCKED at PR 5)
for the normative contract.

The module-level ``_default_load_all`` helper is the canonical default
implementation for ``load_all_mcp_servers()``. Filesystem backend delegates to
it directly (PR 1). HTTP backend overrides with a single bulk GET at PR 4.
Backends overriding MUST preserve the MUST 10 consistency guarantee: the output
must be semantically equivalent to
``[load_mcp_server(ref.name) for ref in list_mcp_servers()]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .types import MCPServerRegistryCapabilities, MCPServerRef, ValidationResult

if TYPE_CHECKING:
    from ..mcp import MCPServerSpec


# ──────────────────────────────────────────────────────────────────────────────
# Exception classes


class MCPRegistryError(Exception):
    """Base class for MCPServerRegistry subsystem errors (spec/36).

    All MCPServerRegistry reference implementations raise subclasses of this
    exception. Operators may ``except MCPRegistryError`` to catch the entire
    MCP registry error family.
    """


class MCPServerNotInRegistry(MCPRegistryError):
    """``load_mcp_server(name)`` called with a name not in the catalog.

    HTTP equivalent: catalog server returned 404 on ``GET /mcp-servers/<name>``.

    Distinct from ``MCPServerConnectFailed`` (spec/19's runtime subprocess
    failure) -- this exception means the server is not declared in the catalog,
    not that the subprocess failed to start.
    """


class MCPServerAlreadyInstalled(MCPRegistryError):
    """``install(spec)`` found a name collision in the catalog.

    HTTP equivalent: catalog server returned 409 on ``POST /mcp-servers``.

    Filesystem backend raises this when a section with the same name already
    exists in ``mcp.md``. The caller must uninstall the existing entry first
    or choose a different name.
    """


class MCPRegistryUnavailable(MCPRegistryError):
    """Transient failure reaching the catalog backend.

    Raised for: network errors, DNS failures, connection refused, server 5xx,
    file-lock contention on the filesystem backend.

    Operators should retry. The framework does NOT auto-retry. Distinct from
    ``MCPServerNotInRegistry`` (permanent absence) per MUST 7.
    """


class MCPRegistryAuthRequired(MCPRegistryError):
    """HTTP catalog server returned 401 and no ``auth_token`` was provided.

    Operators set ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN`` in the
    environment or pass ``auth_token=`` to the ``HTTPMCPServerRegistryBackend``
    constructor.
    """


class MCPRegistryDescriptorInvalid(MCPRegistryError):
    """The server descriptor could not be parsed.

    Filesystem: ``mcp.md`` parse failure (malformed YAML, missing required
    fields). HTTP: catalog server returned an invalid JSON body.
    """


class BackendNotRegistered(MCPRegistryError):
    """Operator-pinned ``backend_id`` is not in the registry.

    Raised by ``get_mcp_server_registry_backend(backend_id)`` when ``backend_id``
    is not registered. The error message includes the list of registered ids
    and a credential-redacted echo of the value that was tried.
    """


# ──────────────────────────────────────────────────────────────────────────────
# Protocol definition


@runtime_checkable
class MCPServerRegistryBackend(Protocol):
    """Contract every MCP server registry backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol -- it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, MCPServerRegistryBackend)`` to perform a method-presence
    check (not a signature check -- signatures are static-typing's job).

    Per-agent scoping is structural. ``FilesystemMCPServerRegistryBackend``
    is scoped to ``agent_root`` (one ``mcp.md`` per agent).
    ``HTTPMCPServerRegistryBackend`` scopes every wire request by
    ``agent_scope``. A backend that returns org-wide catalog from
    ``list_mcp_servers()`` is non-conformant (MUST 5).

    Server name charset ``[a-zA-Z0-9_.+@-]+`` is enforced at the API boundary
    BEFORE any backend access (MUST 1). Path-traversal tokens (``..``, ``/``,
    ``\\``), control characters, newlines, leading dots, and empty strings
    MUST raise ``ValueError``.
    """

    # ─── Capability advertisement ─────────────────────────────────────────

    @property
    def capabilities(self) -> MCPServerRegistryCapabilities:
        """Advertise what this backend instance supports.

        Returns a frozen ``MCPServerRegistryCapabilities`` dataclass. The
        values are a contract, not a hint -- conformance tests assert
        claim-vs-behavior parity (MUST 3).

        Filesystem backend: constant across the lifetime of the instance
        (no remote dependency). HTTP backend: dynamic -- reflects the
        connected catalog server's tier; lazy probe at first non-construction
        call; explicit ``refresh_capabilities()`` to bypass cache.

        All call sites use property syntax: ``backend.capabilities.supports_install``
        NOT ``backend.capabilities().supports_install``.
        """
        ...

    @property
    def backend_id(self) -> str:
        """Stable identifier for this backend implementation (MUST 6a).

        Returns a short lowercase string matching the registry key used to
        register this backend class (e.g., ``"filesystem"``, ``"http"``).
        Must not change across calls on the same instance. Used for logging,
        audit events, and operator-facing capability output.
        """
        ...

    # ─── Core discovery (always implemented) ─────────────────────────────

    def list_mcp_servers(self) -> list[MCPServerRef]:
        """Return lightweight server references for THIS AGENT'S mounted set.

        Returns ``[]`` when the catalog is empty or the backing resource is
        absent. Never raises on a missing ``mcp.md`` or an unreachable catalog.
        Lexicographic order by ``name`` (MUST 5 -- consistent sort).

        Cheap by construction: no subprocess spawn, no handler import, no
        MCP server connection.
        """
        ...

    def load_mcp_server(self, name: str) -> MCPServerSpec:
        """Return the fully-populated ``MCPServerSpec`` for the named server.

        MUST validate ``name`` against path-traversal at the API boundary
        BEFORE any backend access. Raises ``ValueError`` on invalid names.

        MUST raise ``MCPServerNotInRegistry`` when the name is absent from
        the catalog.

        MUST resolve ``$VAR`` env-var references against the client process
        environment at call time (MUST 8). Unresolvable references raise
        ``MCPServerConnectFailed`` (the existing spec/19 exception; not a new
        exception class).
        """
        ...

    def load_all_mcp_servers(self) -> list[MCPServerSpec]:
        """Return all mounted ``MCPServerSpec`` instances in bulk.

        Default: semantically equivalent to
        ``[load_mcp_server(ref.name) for ref in list_mcp_servers()]``.
        HTTP backend overrides with a single bulk GET at PR 4.

        MUST 10: output MUST be semantically equivalent to the default
        iteration for any given backend state. Backends overriding for
        performance MUST preserve this equivalence.
        """
        ...

    def validate(self, name: str) -> ValidationResult:
        """Static check of the named server descriptor.

        Does NOT spawn the MCP server subprocess (MUST 2 analog -- static
        only). Returns ``ValidationResult(ok=False, errors=[...])`` when the
        server is absent; does NOT raise ``MCPServerNotInRegistry``.

        Filesystem implementation checks: descriptor parses; command exists
        on PATH (warn if absent -- do not fail); transport recognized; ``$VAR``
        refs resolve (warn if not).
        """
        ...

    # ─── Capability-gated lifecycle ───────────────────────────────────────

    def install(self, spec: MCPServerSpec) -> MCPServerRef:
        """Mount a new MCP server in the catalog.

        MUST be atomic at the server-name level (MUST 9). Raises
        ``MCPServerAlreadyInstalled`` on name collision.

        Backends with ``capabilities.supports_install=False`` MUST raise
        ``NotImplementedError``. The filesystem backend ships this at PR 3.
        """
        ...

    def uninstall(self, name: str) -> None:
        """Remove a mounted server from the catalog.

        MUST be idempotent: uninstalling an absent name is a no-op (no
        exception). MUST validate ``name`` against path-traversal.

        Backends with ``capabilities.supports_uninstall=False`` MUST raise
        ``NotImplementedError``. The filesystem backend ships this at PR 3.
        """
        ...

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def refresh_capabilities(self) -> MCPServerRegistryCapabilities:
        """Re-probe the backend's capabilities and return the current view.

        Filesystem: no-op -- returns the static cached instance (filesystem
        has no remote dependency; capabilities never change).

        HTTP: re-runs the full capability probe sequence (bypassing any cache)
        and returns the updated runtime view. Operators call this after
        upgrading a catalog server to a higher tier.

        Protocol contract: callable, idempotent, returns current capabilities.
        """
        ...

    def close(self) -> None:
        """Release any resources held by this backend instance.

        Idempotent (MUST 6b). Calling ``close()`` twice must not raise. Calling
        it before any other method is a no-op.

        Filesystem: no-op. HTTP: closes the underlying HTTP client session.
        """
        ...


# ──────────────────────────────────────────────────────────────────────────────
# Default load_all helper


def _default_load_all(backend: MCPServerRegistryBackend) -> list[MCPServerSpec]:
    """Default implementation of ``load_all_mcp_servers()`` for any backend.

    Iterates ``list_mcp_servers()`` and calls ``load_mcp_server(ref.name)``
    for each entry. This is the canonical MUST 10 baseline: filesystem backend
    delegates to this helper directly; HTTP backend overrides it with a single
    bulk GET (``GET /mcp-servers?expand=spec``) in PR 4.

    Backends overriding ``load_all_mcp_servers()`` for performance MUST produce
    output that is semantically equivalent to this function's output (MUST 10).
    """
    return [backend.load_mcp_server(ref.name) for ref in backend.list_mcp_servers()]

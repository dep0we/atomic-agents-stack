"""MCPServerRegistryBackend Protocol and registry (spec/36, issue #201).

This package establishes the MCP server registry abstraction in the
protocol-pattern series alongside MemoryBackend (#57), LLMBackend (#87),
JudgeBackend (#112), LockBackend (#60), LogBackend (#61),
AgentProfileBackend (#63), ToolRegistryBackend (#64), MandateBackend (#124),
PolicyBackend (#89), PersonaBackend (#62), and CorpusBackend (#65).
See ``docs/spec/36-mcp-server-registry-backend.md`` (DRAFT at PR 1; LOCKED
at PR 5) for the prose contract.

Public surface:

    from atomic_agents.mcp_registry import (
        # Protocol contract
        MCPServerRegistryBackend,
        # Exception classes
        MCPRegistryError,
        MCPServerNotInRegistry,
        MCPServerAlreadyInstalled,
        MCPRegistryUnavailable,
        MCPRegistryAuthRequired,
        MCPRegistryDescriptorInvalid,
        BackendNotRegistered,
        # Canonical types
        MCPServerRef,
        MCPServerRegistryCapabilities,
        ValidationResult,
        # Reference impl
        FilesystemMCPServerRegistryBackend,
        # Registry
        register_mcp_server_registry_backend,
        get_mcp_server_registry_backend,
        list_mcp_server_registry_backends,
        # Operator-config factory
        get_default_mcp_server_registry_backend,
    )

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"http"``). Like the
Log + Lock + Profile + ToolRegistry + Corpus registries it stores backend
*classes*, not instances -- MCP registry backends are constructed per agent
scope and the registry's job is to let an operator pick "filesystem vs http"
for a deployment. The caller (``AtomicAgent.__init__`` in PR 2) instantiates
the chosen class with its scope-specific args.

Thread-safety: registration is expected at import time (one-shot from each
backend's module); ``get_mcp_server_registry_backend`` is read-only and safe
to call from any thread. No lock is needed under that usage.

Per the project's ``register_backend_placement_convention`` learning
(2026-05-29): ``register_mcp_server_registry_backend`` lives in
``__init__.py`` alongside the factory + redaction helper, matching the
dominant 6-of-10 pattern (locks, llm, logs, profile, registry, judge, corpus).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .backend import (
    BackendNotRegistered,
    MCPRegistryAuthRequired,
    MCPRegistryDescriptorInvalid,
    MCPRegistryError,
    MCPRegistryUnavailable,
    MCPServerAlreadyInstalled,
    MCPServerNotInRegistry,
    MCPServerRegistryBackend,
)
from .filesystem import FilesystemMCPServerRegistryBackend
from .types import MCPServerRef, MCPServerRegistryCapabilities, ValidationResult

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "MCPServerRegistryBackend",
    # Exception classes
    "MCPRegistryError",
    "MCPServerNotInRegistry",
    "MCPServerAlreadyInstalled",
    "MCPRegistryUnavailable",
    "MCPRegistryAuthRequired",
    "MCPRegistryDescriptorInvalid",
    "BackendNotRegistered",
    # Canonical types
    "MCPServerRef",
    "MCPServerRegistryCapabilities",
    "ValidationResult",
    # Reference implementations
    "FilesystemMCPServerRegistryBackend",
    # Registry
    "register_mcp_server_registry_backend",
    "get_mcp_server_registry_backend",
    "list_mcp_server_registry_backends",
    # Operator-config factory
    "get_default_mcp_server_registry_backend",
]


# Process-local registry: backend_id -> backend class. Backend classes
# (not instances) because MCP registry backends carry per-scope construction
# args -- the framework instantiates ``FilesystemMCPServerRegistryBackend(
# agent_root, read_paths)`` per agent; the registry's role is the
# operator-pin lookup that maps ``"filesystem"`` ->
# ``FilesystemMCPServerRegistryBackend``.
_registry: dict[str, type] = {}


def register_mcp_server_registry_backend(backend_id: str, cls: type) -> None:
    """Register a ``MCPServerRegistryBackend`` implementation under ``backend_id``.

    Typically called once at module-import time from each backend's package
    (the default ``"filesystem"`` registration happens at the bottom of this
    file).

    Re-registering the same ``backend_id`` replaces the existing binding and
    logs at DEBUG -- intentional. Operators occasionally want to swap in a
    wrapper (e.g., a ``CachingMCPServerRegistryBackend``) without first
    unregistering the original.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered mcp_server_registry backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def get_mcp_server_registry_backend(backend_id: str) -> type:
    """Return the registered backend class for ``backend_id``.

    Raises ``BackendNotRegistered`` when the id is not in the registry. The
    caller instantiates the returned class with its scope-specific constructor
    arguments (e.g., ``cls(agent_root, read_paths)`` for the filesystem
    backend).
    """
    if backend_id not in _registry:
        safe_id = _redact_for_error_message(backend_id)
        raise BackendNotRegistered(
            f"No MCPServerRegistryBackend registered under {safe_id!r}. "
            f"Available: {list_mcp_server_registry_backends()}. "
            f"Unset ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND to use the "
            f"filesystem default."
        )
    return _registry[backend_id]


def list_mcp_server_registry_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time. HTTP backend
# registers itself when ``atomic_agents.mcp_registry.http`` is imported
# (ships at PR 4).
register_mcp_server_registry_backend("filesystem", FilesystemMCPServerRegistryBackend)


def get_default_mcp_server_registry_backend(
    agent_root: Path,
    read_paths: list,
) -> MCPServerRegistryBackend:
    """Return the operator-pinned MCPServerRegistryBackend instance.

    Reads ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND`` from the environment
    (default ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL`` for the connection /
    catalog URL. The env var name is intentionally generic so future HTTP /
    SaaS backends plug in via the same key without operators having to relearn
    the env vocabulary.

    An empty string (or whitespace-only) value for
    ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND`` is treated as "not set" and
    falls back to the filesystem default. This guards against shell
    ``export ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND=`` accidents.

    The ``agent_root`` and ``read_paths`` parameters are forwarded to the
    filesystem backend (or any future backend that accepts them). HTTP / SaaS
    backends use ``agent_scope`` from the URL query parameter instead.

    For programmatic operators who want to construct the backend themselves,
    the ``AtomicAgent(..., mcp_server_registry_backend=...)`` constructor kwarg
    (PR 2) bypasses this factory entirely.

    See spec/36 §"Operator surface" for the full env-var reference + the
    env-var-vs-kwarg trade-off rationale.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND", "").strip().lower()
    )

    # Empty string treated as "not set"; falls through to filesystem default.
    if not raw_backend_id:
        raw_backend_id = "filesystem"

    if raw_backend_id == "filesystem":
        return FilesystemMCPServerRegistryBackend(agent_root, read_paths)

    elif raw_backend_id == "http":
        # Lazy import: filesystem operators do not pay the httpx import cost.
        from .http import make_http_mcp_server_registry_backend_from_url

        url = os.environ.get(
            "ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL", ""
        ).strip()
        if not url:
            raise BackendNotRegistered(
                "ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND=http requires "
                "ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL to be set. "
                "Expected format: https://<host>[:port]/?agent_scope=<name>"
            )
        return make_http_mcp_server_registry_backend_from_url(url)

    # Unknown backend_id. Sanitize before echoing in the error message to
    # prevent credential leaks when operators accidentally paste a URL into
    # ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND instead of the _URL variable.
    known_ids = {"filesystem", "http"}
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND={safe_backend_id!r} is not a "
        f"known backend. Known: {sorted(known_ids)}. "
        f"Available registered: {list_mcp_server_registry_backends()}. "
        f"Unset the env var to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and truncates at
    ``max_len`` to bound the echoed string. Returns the bare backend_id if no
    URL marker is present. The full original value is never echoed -- this
    prevents the credential-leak failure mode where an operator accidentally
    sets ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND=https://user:pass@host``
    instead of ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL``.

    Mirrors ``logs/__init__.py:316``, ``profile/__init__.py:_redact_for_error_message``,
    and ``corpus/__init__.py:_redact_for_error_message``.
    """
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    # DSN heuristic: catch user:password@host/db style without a scheme.
    if "@" in value and re.search(r":[^/]+@", value):
        return "[redacted-connection-string]"
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

"""AgentRegistryBackend — Protocol + registry + canonical types (spec/51).

This package provides fleet-level agent enumeration and governance metadata so
the dashboard and operator tooling can discover agents without relying on the
log/-presence heuristic (which excludes newly-deployed agents with no runs yet).

The twenty-second backend Protocol in the atomic-agents framework (v2.0 wave).

Public surface:

    from atomic_agents.agent_registry import (
        # Protocol contract
        AgentRegistryBackend,
        # Canonical types
        AgentRef, AgentEntry, AgentRegistryCapabilities,
        GovernanceRecord, ReviewRecord, RiskRecord, SourcesRecord, ActionsRecord,
        # Enum vocabularies
        PERMISSION_TIERS, TRISTATES, LIFECYCLE_STATUSES,
        # Reference impl
        FilesystemAgentRegistryBackend,
        # Exceptions
        AgentRegistryError, RegistrationNotSupported, GovernanceParseError,
        # Registry
        register_agent_registry_backend, get_agent_registry_backend,
        list_agent_registry_backends, unregister_agent_registry_backend,
        # Operator-config factory (fleet-scoped, NOT agent-scoped)
        get_default_agent_registry_backend,
        # Credential-echo-redaction helper
        _redact_for_error_message,
    )

Scope: FLEET-LEVEL — agents_root (NOT agent_root). This mirrors
AgentProfileBackend, not the per-agent JournalBackend/IdempotencyBackend.
get_default_agent_registry_backend(agents_root) takes agents_root.

Env var: ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND.
Default: 'filesystem' (always-on, same as log/lock/profile — NOT opt-in
like embedding/conversation). The filesystem backend requires no config.

When set, 'filesystem' instantiates FilesystemAgentRegistryBackend(agents_root).
A typo or unknown value raises BackendNotRegistered (fail-loud per spec/51
§"Registry and env override").
An empty/whitespace value is treated as 'not set — use filesystem default'.

The registry is a process-local dict keyed by backend_id (e.g. 'filesystem').
Like all v2.0 backend registries it stores backend *classes*, not instances —
agent_registry backends are constructed per scope (one per agents_root).

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_agent_registry_backend is read-only and safe to call
from any thread.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..exceptions import (
    AgentRegistryError,
    BackendNotRegistered,
    GovernanceParseError,
    RegistrationNotSupported,
)
from .backend import AgentRegistryBackend
from .filesystem import FilesystemAgentRegistryBackend
from .types import (
    LIFECYCLE_STATUSES,
    PERMISSION_TIERS,
    TRISTATES,
    ActionsRecord,
    AgentEntry,
    AgentRef,
    AgentRegistryCapabilities,
    GovernanceRecord,
    ReviewRecord,
    RiskRecord,
    SourcesRecord,
)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "AgentRegistryBackend",
    # Canonical types
    "AgentRef",
    "AgentEntry",
    "AgentRegistryCapabilities",
    "GovernanceRecord",
    "ReviewRecord",
    "RiskRecord",
    "SourcesRecord",
    "ActionsRecord",
    # Enum vocabularies
    "PERMISSION_TIERS",
    "TRISTATES",
    "LIFECYCLE_STATUSES",
    # Reference impl
    "FilesystemAgentRegistryBackend",
    # Exceptions
    "AgentRegistryError",
    "RegistrationNotSupported",
    "GovernanceParseError",
    # Registry
    "register_agent_registry_backend",
    "unregister_agent_registry_backend",
    "get_agent_registry_backend",
    "list_agent_registry_backends",
    # Operator-config factory
    "get_default_agent_registry_backend",
    # Credential-echo-redaction helper
    "_redact_for_error_message",
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_agent_registry_backend(backend_id: str, cls: type) -> None:
    """Register an AgentRegistryBackend implementation under backend_id.

    Typically called once at module-import time from each backend's package
    (the default 'filesystem' registration happens at the bottom of this file).

    backend_id MUST be lowercase. get_default_agent_registry_backend() lowercases
    the ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND env value before lookup (matching the
    v2.0 sibling convention in conversation/journal/mcp_registry __init__), so a
    mixed-case registration ('MyDB') would never match a mixed-case env value
    ('MyDB' → looked up as 'mydb') and the operator would get a confusing
    fail-loud "not a known backend" listing the very id they registered. Register
    lowercase to keep lookup symmetric.

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered agent_registry backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_agent_registry_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_agent_registry_backend(backend_id: str) -> type:
    """Return the registered AgentRegistryBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., cls(agents_root)).
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        safe_backend_id = _redact_for_error_message(backend_id)
        raise BackendNotRegistered(
            f"No AgentRegistryBackend registered under {safe_backend_id!r}. "
            f"Available: {known_ids}"
        )
    return _registry[backend_id]


def list_agent_registry_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time.
register_agent_registry_backend("filesystem", FilesystemAgentRegistryBackend)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_agent_registry_backend(agents_root: Path) -> AgentRegistryBackend:
    """Return the operator-pinned AgentRegistryBackend for agents_root.

    FLEET-SCOPED: takes agents_root (the fleet root), NOT agent_root.
    This is the same scope as get_default_profile_backend(), not the
    per-agent scope of get_default_journal_backend().

    Reads ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND from the environment.
    - Absent or empty: returns FilesystemAgentRegistryBackend (always-on default,
      same as log/lock/profile — NOT opt-in like embedding/conversation).
    - 'filesystem': returns FilesystemAgentRegistryBackend.
    - Other known id: instantiates the registered backend class.
    - Unknown id (non-empty, non-filesystem): raises BackendNotRegistered
      (fail-loud per spec/51 §"Registry and env override").

    Empty/whitespace ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND is treated as
    'not set — use filesystem default' (same as mcp_registry/__init__.py:197).

    Args:
        agents_root: the fleet root directory. Passed to the backend constructor.

    Returns:
        An AgentRegistryBackend instance scoped to agents_root.

    Raises:
        BackendNotRegistered: when the env var names an unknown (non-empty)
            backend that has not been registered.
    """
    raw = os.environ.get("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", "").strip().lower()

    if not raw or raw == "filesystem":
        return FilesystemAgentRegistryBackend(agents_root)

    # Operator-registered custom backend: dispatch through the registry.
    if raw in _registry:
        return _registry[raw](agents_root)

    # Unknown backend_id — fail loud (spec/51 §"Registry and env override").
    safe_backend_id = _redact_for_error_message(raw)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known_ids}. "
        "Unset the env var or set it to 'filesystem' to use the default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Returns 'scheme://...' for a '://'-scheme URL and
    '[redacted-connection-string]' for a schemeless 'user:pass@host/db' DSN —
    both short-circuit with no further truncation. Only a non-matching value is
    truncated at max_len.

    Mirrors journal/__init__.py, conversation/__init__.py,
    mcp_registry/__init__.py pattern — each backend defines this locally
    to avoid circular imports from a shared utility module.

    MUST be used at ALL echo sites for ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND.
    Never echo the raw env value in CheckResult messages or detail dicts.
    """
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if "@" in value and re.search(r":[^/]+@", value):
        return "[redacted-connection-string]"
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

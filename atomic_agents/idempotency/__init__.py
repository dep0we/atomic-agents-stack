"""IdempotencyBackend — Protocol + registry + canonical types (spec/45).

This package establishes the idempotency deduplication ledger abstraction.
See docs/spec/45-idempotency-backend.md for the prose contract.

Public surface:

    from atomic_agents.idempotency import (
        # Protocol contract
        IdempotencyBackend,
        # Canonical types
        DedupDecision, IdempotencyCapabilities, IdempotencyExport,
        # State constants
        FRESH, IN_FLIGHT, COMPLETED,
        # Reference impl
        FilesystemDedupLedger,
        # Implementation exception
        IdempotencyBackendError,
        # Registry
        register_idempotency_backend, get_idempotency_backend,
        list_idempotency_backends, unregister_idempotency_backend,
        # Operator-config factory
        get_default_idempotency_backend,
        # Redaction helper (credential-echo-redaction pattern)
        _redact_for_error_message,
    )

Scope: agent_root (NOT project_root) — this mirrors GoalBackend/JournalBackend.
The factory get_default_idempotency_backend(agent_root) takes agent_root.
Cross-agent dedup requires a shared backend (see spec/45 §"Scope").

Env var: ATOMIC_AGENTS_IDEMPOTENCY_BACKEND (single var, value is a registered
backend_id; defaults to 'filesystem'). Connection-string forms are NOT consumed
in PR1 — a future Redis/Postgres backend defines its own connection handling.

The registry is a process-local dict keyed by backend_id (e.g. 'filesystem').
Like all v1.5 backend registries it stores backend *classes*, not instances —
idempotency backends are constructed per scope (one per agent root).

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_idempotency_backend is read-only and safe to call from
any thread.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..exceptions import BackendNotRegistered, DedupInFlight, IdempotencyBackendError
from .backend import IdempotencyBackend
from .filesystem import FilesystemDedupLedger
from .types import (
    COMPLETED,
    FRESH,
    IN_FLIGHT,
    DedupDecision,
    IdempotencyCapabilities,
    IdempotencyExport,
)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "IdempotencyBackend",
    # Canonical types
    "DedupDecision",
    "IdempotencyCapabilities",
    "IdempotencyExport",
    # State constants
    "FRESH",
    "IN_FLIGHT",
    "COMPLETED",
    # Reference impl
    "FilesystemDedupLedger",
    # Exceptions
    "IdempotencyBackendError",
    "DedupInFlight",
    # Registry
    "register_idempotency_backend",
    "unregister_idempotency_backend",
    "get_idempotency_backend",
    "list_idempotency_backends",
    # Operator-config factory
    "get_default_idempotency_backend",
    # Cron helper
    "cron_tick_key",
    # Credential-echo-redaction helper
    "_redact_for_error_message",
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_idempotency_backend(backend_id: str, cls: type) -> None:
    """Register an IdempotencyBackend implementation under backend_id.

    Typically called once at module-import time from each backend's package
    (the default 'filesystem' registration happens at the bottom of this file).

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered idempotency backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_idempotency_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_idempotency_backend(backend_id: str) -> type:
    """Return the registered IdempotencyBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., cls(agent_root) for the filesystem backend).
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        raise BackendNotRegistered(
            f"No IdempotencyBackend registered under {backend_id!r}. "
            f"Available: {known_ids}"
        )
    return _registry[backend_id]


def list_idempotency_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time.
register_idempotency_backend("filesystem", FilesystemDedupLedger)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_idempotency_backend(agent_root: Path) -> IdempotencyBackend:
    """Return the operator-pinned IdempotencyBackend instance for agent_root.

    Reads ATOMIC_AGENTS_IDEMPOTENCY_BACKEND from the environment
    (default 'filesystem'). The value is a registered backend_id (PR1 registers
    only 'filesystem'). Connection-string forms (e.g. 'redis://...') are NOT
    consumed in PR1 — they would raise BackendNotRegistered; a future
    Redis/Postgres backend will define its own connection handling.

    Credential safety: the env var value is never echoed raw in error messages.
    _redact_for_error_message() strips credentials before any error output (in
    case an operator pastes a connection string into the backend_id var).

    Programmatic operators who want to construct the backend themselves can
    instantiate FilesystemDedupLedger(agent_root) directly, bypassing this
    factory.

    Returns:
        An IdempotencyBackend instance scoped to agent_root.

    Raises:
        BackendNotRegistered: when the env var names an unknown backend.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_IDEMPOTENCY_BACKEND", "filesystem")
        .strip()
        .lower()
    )

    if not raw_backend_id or raw_backend_id == "filesystem":
        return FilesystemDedupLedger(agent_root)

    # Operator-registered custom backend: dispatch through the registry.
    if raw_backend_id in _registry:
        return _registry[raw_backend_id](agent_root)

    # Unknown backend_id — surface a fail-fast error with the known-id list.
    # Credential safety: sanitize the raw value in case an operator pastes a URL.
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_IDEMPOTENCY_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic), redacts a
    schemeless ``user:pass@host/db`` DSN, then truncates at ``max_len`` to
    bound the echoed string. The full original value is never echoed.

    Mirrors queue/__init__.py, journal/__init__.py, goal/__init__.py, etc.
    _redact_for_error_message (the standing credential-echo-redaction pattern).
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


# ──────────────────────────────────────────────────────────────────
# Cron helper — lazy import to avoid pulling in datetime overhead at
# package import time for callers who only need the registry / backend.

from .cron import cron_tick_key  # noqa: E402 — import after registry bootstrap

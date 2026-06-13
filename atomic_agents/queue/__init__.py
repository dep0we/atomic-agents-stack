"""QueueBackend — Protocol + registry + canonical types (spec/44).

This package establishes the queue abstraction for the cascade work queue.
See docs/spec/44-queue-backend.md for the prose contract.

Public surface:

    from atomic_agents.queue import (
        # Protocol contract
        QueueBackend,
        # Canonical types
        QueueItem, QueueCapabilities, QueueExport,
        # Reference impl
        FilesystemQueueBackend,
        FilesystemQueueItem,
        # Private helpers (filesystem-specific, re-exported for backward compat)
        _sidecar_path, _write_sidecar,
        # Registry
        register_queue_backend, get_queue_backend,
        list_queue_backends, unregister_queue_backend,
        # Operator-config factory
        get_default_queue_backend,
        # Shared recovery (above the Protocol)
        recover_stale_claims,
        # Redaction helper (credential-echo-redaction pattern)
        _redact_for_error_message,
    )

Scope: project_root (NOT agent_root) — this is the one project-scoped
backend in the v1.5 wave. The factory get_default_queue_backend(project_root)
takes project_root, not agent_root. This divergence from the agent-scope
siblings is correct per spec/06 (the queue is a shared project resource).

The registry is a process-local dict keyed by backend_id (e.g. 'filesystem').
Like the Lock/Log/Goal/Outcome/Journal registries it stores backend *classes*,
not instances — queue backends are constructed per scope (one per project root).

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_queue_backend is read-only and safe to call from any
thread. No lock is needed under that usage.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..exceptions import BackendNotRegistered
from .backend import QueueBackend, recover_stale_claims
from .filesystem import (
    FilesystemQueueBackend,
    FilesystemQueueItem,
    _sidecar_path,  # noqa: F401 (re-export for _cascade.py backward compat)
    _write_sidecar,  # noqa: F401 (re-export for _cascade.py backward compat)
)
from .types import QueueCapabilities, QueueExport, QueueItem

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "QueueBackend",
    # Canonical types
    "QueueItem",
    "QueueCapabilities",
    "QueueExport",
    # Reference impl
    "FilesystemQueueBackend",
    "FilesystemQueueItem",
    # Registry
    "register_queue_backend",
    "unregister_queue_backend",
    "get_queue_backend",
    "list_queue_backends",
    # Operator-config factory
    "get_default_queue_backend",
    # Shared recovery
    "recover_stale_claims",
    # Credential-echo-redaction helper
    "_redact_for_error_message",
    # NOTE: _sidecar_path and _write_sidecar are re-exported here for
    # backward compat with any external code that imported them from
    # atomic_agents._cascade. They are NOT in __all__ because they are
    # filesystem-impl details, not part of the public Protocol surface.
    # The _cascade.py shim re-exports them from this module.
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_queue_backend(backend_id: str, cls: type) -> None:
    """Register a QueueBackend implementation under backend_id.

    Typically called once at module-import time from each backend's package
    (the default 'filesystem' registration happens at the bottom of this file).

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered queue backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_queue_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_queue_backend(backend_id: str) -> type:
    """Return the registered QueueBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., cls(project_root) for the filesystem backend).
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        raise BackendNotRegistered(
            f"No QueueBackend registered under {backend_id!r}. Available: {known_ids}"
        )
    return _registry[backend_id]


def list_queue_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time.
register_queue_backend("filesystem", FilesystemQueueBackend)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_queue_backend(project_root: Path) -> QueueBackend:
    """Return the operator-pinned QueueBackend instance for project_root.

    Reads ATOMIC_AGENTS_QUEUE_BACKEND from the environment (default 'filesystem').

    Scope is project_root (NOT agent_root) — this is the one project-scoped
    backend in the v1.5 wave. The cascade queue is a shared project resource,
    not a per-agent resource (per spec/06).

    Mirrors get_default_journal_backend(agent_root) pattern, but note the
    different scope argument. Operators must pass project_root, not agent_root.

    Do NOT gate this factory on 'project has queue entries'. The backend is
    always returned; FilesystemQueueBackend.claim_next() returns None when
    the queue is absent (new projects).

    Programmatic operators who want to construct the backend themselves can
    instantiate FilesystemQueueBackend(project_root) directly, bypassing this
    factory.

    Returns:
        A QueueBackend instance scoped to project_root.

    Raises:
        BackendNotRegistered: when the env var names an unknown backend.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_QUEUE_BACKEND", "filesystem").strip().lower()
    )

    if not raw_backend_id or raw_backend_id == "filesystem":
        return FilesystemQueueBackend(project_root)

    # Operator-registered custom backend: dispatch through the registry.
    if raw_backend_id in _registry:
        return _registry[raw_backend_id](project_root)

    # Unknown backend_id — surface a fail-fast error with the known-id list.
    # Credential safety: sanitize the raw value in case an operator pastes a URL.
    # Mirrors goal/__init__.py, logs/__init__.py, journal/__init__.py,
    # outcome/__init__.py _redact_for_error_message (the standing
    # credential-echo-redaction pattern).
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_QUEUE_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic), redacts a
    schemeless ``user:pass@host/db`` DSN, then truncates at ``max_len`` to
    bound the echoed string. The full original value is never echoed — this
    prevents the credential-leak failure mode where an operator accidentally
    pastes a connection string into ``ATOMIC_AGENTS_QUEUE_BACKEND``.

    Mirrors goal/__init__.py, logs/__init__.py, profile/__init__.py,
    corpus/__init__.py, mcp_registry/__init__.py, secret_backend/__init__.py,
    journal/__init__.py, and outcome/__init__.py ``_redact_for_error_message``.
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

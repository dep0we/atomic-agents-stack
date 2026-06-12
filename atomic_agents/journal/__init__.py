"""JournalBackend — Protocol + registry + canonical types (spec/43).

This package establishes the journal abstraction alongside the other backend
protocols. See docs/spec/43-journal-backend.md for the prose contract.

Public surface:

    from atomic_agents.journal import (
        # Protocol contract
        JournalBackend,
        # Canonical types
        JournalEntry, JournalCapabilities, JournalExport,
        # Reference impl
        FilesystemJournalBackend,
        # Registry
        register_journal_backend, get_journal_backend,
        list_journal_backends, unregister_journal_backend,
        # Operator-config factory
        get_default_journal_backend,
    )

No re-export shim needed: VERIFIED (grep confirms) there is no existing public
atomic_agents.journal import path to preserve — journal logic prior to spec/43
was private helpers in bundle/agent/dream, none exported as a public 'journal'
module. The Principle #14 shim mandate is conditional-and-void here.

The registry is a process-local dict keyed by backend_id (e.g. 'filesystem').
Like the Lock/Log/Goal/Outcome registries it stores backend *classes*, not
instances — journal backends are constructed per scope (one per agent root).

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_journal_backend is read-only and safe to call from
any thread. No lock is needed under that usage.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..exceptions import BackendNotRegistered
from .backend import JournalBackend
from .filesystem import FilesystemJournalBackend
from .types import JournalCapabilities, JournalEntry, JournalExport

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "JournalBackend",
    # Canonical types
    "JournalEntry",
    "JournalCapabilities",
    "JournalExport",
    # Reference impl
    "FilesystemJournalBackend",
    # Registry
    "register_journal_backend",
    "unregister_journal_backend",
    "get_journal_backend",
    "list_journal_backends",
    # Operator-config factory
    "get_default_journal_backend",
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_journal_backend(backend_id: str, cls: type) -> None:
    """Register a JournalBackend implementation under backend_id.

    Typically called once at module-import time from each backend's package
    (the default 'filesystem' registration happens at the bottom of this file).

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered journal backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_journal_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_journal_backend(backend_id: str) -> type:
    """Return the registered JournalBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., cls(agent_root) for the filesystem backend).
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        raise BackendNotRegistered(
            f"No JournalBackend registered under {backend_id!r}. Available: {known_ids}"
        )
    return _registry[backend_id]


def list_journal_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time.
register_journal_backend("filesystem", FilesystemJournalBackend)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_journal_backend(agent_root: Path) -> JournalBackend:
    """Return the operator-pinned JournalBackend instance for agent_root.

    Reads ATOMIC_AGENTS_JOURNAL_BACKEND from the environment (default 'filesystem').

    Scoped to ONE agent root — <agent_root>/journal/. Mirrors
    get_default_goal_backend(agent_root) (NOT get_default_profile_backend
    which uses agents_root).

    Do NOT gate this factory on 'agent has journal entries'. The backend is
    always returned; FilesystemJournalBackend.list_entries() returns [] when
    journal/ is absent (new agents).

    Programmatic operators who want to construct the backend themselves can
    instantiate FilesystemJournalBackend(agent_root) directly, bypassing this
    factory.

    Returns:
        A JournalBackend instance scoped to agent_root.

    Raises:
        BackendNotRegistered: when the env var names an unknown backend.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_JOURNAL_BACKEND", "filesystem").strip().lower()
    )

    if not raw_backend_id or raw_backend_id == "filesystem":
        return FilesystemJournalBackend(agent_root)

    # Operator-registered custom backend: dispatch through the registry.
    if raw_backend_id in _registry:
        return _registry[raw_backend_id](agent_root)

    # Unknown backend_id — surface a fail-fast error with the known-id list.
    # Credential safety: sanitize the raw value in case an operator pastes a URL.
    # Mirrors goal/__init__.py, logs/__init__.py, profile/__init__.py,
    # corpus/__init__.py, mcp_registry/__init__.py, and secret_backend/__init__.py
    # _redact_for_error_message (the standing credential-echo-redaction pattern).
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_JOURNAL_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic), redacts a
    schemeless ``user:pass@host/db`` DSN, then truncates at ``max_len`` to
    bound the echoed string. The full original value is never echoed — this
    prevents the credential-leak failure mode where an operator accidentally
    pastes a connection string into ``ATOMIC_AGENTS_JOURNAL_BACKEND``.

    Mirrors goal/__init__.py, logs/__init__.py, profile/__init__.py,
    corpus/__init__.py, mcp_registry/__init__.py, secret_backend/__init__.py,
    and outcome/__init__.py ``_redact_for_error_message``.
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

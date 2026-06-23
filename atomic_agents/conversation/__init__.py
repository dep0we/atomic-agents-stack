"""ConversationBackend — Protocol + registry + canonical types (spec/47).

This package provides per-principal conversation turn persistence so that
agent.call() can inject prior turns into the messages array, enabling
stateful multi-turn exchanges without polluting the system prompt (T14 cache).

The twentieth backend Protocol in the atomic-agents framework (v1.5 wave).

Public surface:

    from atomic_agents.conversation import (
        # Protocol contract
        ConversationBackend,
        # Canonical types
        Turn, Principal, ConversationCapabilities, ConversationExport,
        # Home-user default principal
        LOCAL_PRINCIPAL,
        # Turn schema version constant
        TURN_SCHEMA_VERSION,
        # Reference impl
        FilesystemConversationBackend,
        # Implementation exceptions
        ConversationBackendError, ConversationCorrupted, ConversationAccessDenied,
        # Registry
        register_conversation_backend, get_conversation_backend,
        list_conversation_backends, unregister_conversation_backend,
        # Operator-config factory
        get_default_conversation_backend,
    )

Scope: agent_root (NOT project_root) — mirrors JournalBackend/IdempotencyBackend.
The factory get_default_conversation_backend(agent_root) takes agent_root.

Backward compatibility GUARANTEE (spec/47 MUST 9):
    No backend configured (default None) == today's exact single-shot behavior.
    The factory returns None when ATOMIC_AGENTS_CONVERSATION_BACKEND is unset.
    No conversations/ directory is created on agent construction (unlike journal/
    which is always-on). This is intentional — do NOT change the None default.

Env var: ATOMIC_AGENTS_CONVERSATION_BACKEND (single var, value is a registered
backend_id; defaults to None when unset). Connection-string forms are NOT
consumed in PR1 — a future Postgres backend defines its own connection handling.
When set, 'filesystem' instantiates FilesystemConversationBackend(agent_root).

Model.md field: '## Conversation Backend' section (LOCKED at spec/47;
section name and parser are stable). Parsed from AgentConfig.conversation_backend_id.
All three channels resolve to None when unset — 'no backend == single-shot'
is MANDATORY (rule #14).

The registry is a process-local dict keyed by backend_id (e.g. 'filesystem').
Like all v1.5 backend registries it stores backend *classes*, not instances —
conversation backends are constructed per scope (one per agent root).

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_conversation_backend is read-only and safe to call from
any thread.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..exceptions import (
    BackendNotRegistered,
    ConversationAccessDenied,
    ConversationBackendError,
    ConversationCorrupted,
)
from .backend import ConversationBackend
from .filesystem import FilesystemConversationBackend
from .types import (
    TURN_SCHEMA_VERSION,
    ConversationCapabilities,
    ConversationExport,
    LOCAL_PRINCIPAL,
    Principal,
    Turn,
)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "ConversationBackend",
    # Canonical types
    "Turn",
    "Principal",
    "ConversationCapabilities",
    "ConversationExport",
    # Home-user default principal
    "LOCAL_PRINCIPAL",
    # Turn schema version constant
    "TURN_SCHEMA_VERSION",
    # Reference impl
    "FilesystemConversationBackend",
    # Exceptions
    "ConversationBackendError",
    "ConversationCorrupted",
    "ConversationAccessDenied",
    # Registry
    "register_conversation_backend",
    "unregister_conversation_backend",
    "get_conversation_backend",
    "list_conversation_backends",
    # Operator-config factory
    "get_default_conversation_backend",
    # Credential-echo-redaction helper
    "_redact_for_error_message",
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_conversation_backend(backend_id: str, cls: type) -> None:
    """Register a ConversationBackend implementation under backend_id.

    Typically called once at module-import time from each backend's package
    (the default 'filesystem' registration happens at the bottom of this file).

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered conversation backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_conversation_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_conversation_backend(backend_id: str) -> type:
    """Return the registered ConversationBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., cls(agent_root) for the filesystem backend).
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        raise BackendNotRegistered(
            f"No ConversationBackend registered under {backend_id!r}. "
            f"Available: {known_ids}"
        )
    return _registry[backend_id]


def list_conversation_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time.
register_conversation_backend("filesystem", FilesystemConversationBackend)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_conversation_backend(agent_root: Path) -> "ConversationBackend | None":
    """Return the operator-pinned ConversationBackend for agent_root, or None.

    Reads ATOMIC_AGENTS_CONVERSATION_BACKEND from the environment. When unset,
    returns None (no backend — single-shot behavior, backward-compatible with all
    existing callers that never set a conversation_id). This is the MANDATORY
    default: 'no backend configured == today's exact single-shot' (rule #14).

    IMPORTANT: Unlike get_default_idempotency_backend() and
    get_default_journal_backend(), this factory returns None when the env var is
    absent. Do NOT change this to default to 'filesystem' — that would break
    backward compatibility by creating a conversations/ directory on every agent
    construction.

    When the env var is set to 'filesystem', returns FilesystemConversationBackend.
    Other values dispatch through the registry (for operator-registered backends).

    Programmatic operators who want to construct the backend themselves can
    instantiate FilesystemConversationBackend(agent_root) directly.

    Args:
        agent_root: the agent root path.

    Returns:
        A ConversationBackend instance scoped to agent_root, or None when the
        env var is unset (no backend configured).

    Raises:
        BackendNotRegistered: when the env var names an unknown backend.
    """
    raw = os.environ.get("ATOMIC_AGENTS_CONVERSATION_BACKEND", "").strip().lower()

    if not raw:
        # Env var absent or empty — return None (single-shot default).
        return None

    if raw == "filesystem":
        return FilesystemConversationBackend(agent_root)

    # Operator-registered custom backend: dispatch through the registry.
    if raw in _registry:
        return _registry[raw](agent_root)

    # Unknown backend_id — surface a fail-fast error with the known-id list.
    safe_backend_id = _redact_for_error_message(raw)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_CONVERSATION_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var to use single-shot mode."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after '://' (URL credential heuristic), redacts a
    schemeless 'user:pass@host/db' DSN, then truncates at max_len.

    Mirrors idempotency/__init__.py, journal/__init__.py pattern.
    """
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if "@" in value and re.search(r":[^/]+@", value):
        return "[redacted-connection-string]"
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

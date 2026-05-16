"""Tool-registry abstraction layer — Protocol + registry + canonical types.

This package establishes the tool-registry abstraction in the protocol-
pattern series alongside MemoryBackend (#57, shipped), LLMBackend (#87,
shipped), JudgeBackend (#112, shipped), LockBackend (#60, shipped),
LogBackend (#61, shipped), and AgentProfileBackend (#63, shipped). See
``docs/spec/25-tool-registry-backend.md`` for the prose contract.

Public surface (scaffolding PR — no behavior change today):

    from atomic_agents.registry import (
        # Protocol contract
        ToolRegistryBackend,
        # Canonical types
        ToolRef, ToolRegistryCapabilities, ValidationResult,
        # Reference impl
        FilesystemToolRegistryBackend,
        # Registry
        register_tool_registry_backend, get_tool_registry_backend,
        list_tool_registry_backends, unregister_tool_registry_backend,
        # Operator-config factory
        get_default_tool_registry_backend,
    )

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"sqlite"``). Like the
profile / log / lock registries it stores backend *classes*, not
instances — tool-registry backends are constructed per scope (one per
agent for filesystem; one per database connection for SQLite) and the
registry's job is to let an operator pick "filesystem vs sqlite vs
pypi" for a deployment. The caller (``AtomicAgent.__init__`` in PR 2)
instantiates the chosen class with its scope-specific args.

Thread-safety: registration is expected at import time (one-shot from
each backend's module); ``get_tool_registry_backend`` is read-only and
safe to call from any thread. No lock is needed under that usage.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..exceptions import (
    BackendNotRegistered,
    ToolAlreadyInstalled,
    ToolDescriptorInvalid,
    ToolHandlerImportFailed,
    ToolNotInRegistry,
)
from .backend import ToolRegistryBackend
from .filesystem import FilesystemToolRegistryBackend
from .sqlite import (
    SQLiteToolRegistryBackend,
    make_sqlite_tool_registry_backend_from_url,
)
from .types import (
    ToolRef,
    ToolRegistryCapabilities,
    ValidationResult,
)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "ToolRegistryBackend",
    # Canonical types
    "ToolRef",
    "ToolRegistryCapabilities",
    "ValidationResult",
    # Reference implementations
    "FilesystemToolRegistryBackend",
    "SQLiteToolRegistryBackend",
    "make_sqlite_tool_registry_backend_from_url",
    # Registry
    "register_tool_registry_backend",
    "unregister_tool_registry_backend",
    "get_tool_registry_backend",
    "list_tool_registry_backends",
    # Operator-config factory
    "get_default_tool_registry_backend",
    # Exceptions (re-exported for caller ergonomics — the canonical
    # home stays atomic_agents.exceptions; this re-export lets
    # operators write ``from atomic_agents.registry import
    # ToolNotInRegistry`` instead of mixing import sources for one
    # logical subsystem).
    "ToolNotInRegistry",
    "ToolHandlerImportFailed",
    "ToolDescriptorInvalid",
    "ToolAlreadyInstalled",
]


# Process-local registry: backend_id → backend class. Backend classes
# (not instances) because tool-registry backends carry per-scope
# construction args — the framework instantiates
# ``FilesystemToolRegistryBackend(agent_root)`` at agent construction
# time; the registry's role is the operator-pin lookup that maps
# ``"filesystem"`` → ``FilesystemToolRegistryBackend``.
_registry: dict[str, type] = {}


def register_tool_registry_backend(backend_id: str, cls: type) -> None:
    """Register a ToolRegistryBackend implementation under ``backend_id``.

    Typically called once at module-import time from each backend's
    package (the default ``"filesystem"`` registration happens at the
    bottom of this file).

    Re-registering the same ``backend_id`` replaces the existing
    binding and logs at DEBUG — intentional. Operators occasionally
    want to swap in a wrapper (e.g., a ``CachingToolRegistryBackend``
    that decorates the filesystem backend) without first unregistering
    the original.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered tool registry backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_tool_registry_backend(backend_id: str) -> None:
    """Remove a backend by ``backend_id``. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a
    backend.
    """
    _registry.pop(backend_id, None)


def get_tool_registry_backend(backend_id: str) -> type:
    """Return the registered ToolRegistryBackend class for ``backend_id``.

    Raises ``BackendNotRegistered`` when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., ``cls(agent_root)`` for the filesystem
    backend; ``cls(db_path, agent_scope=...)`` for a future
    ``SQLiteToolRegistryBackend``).
    """
    if backend_id not in _registry:
        raise BackendNotRegistered(
            f"No ToolRegistryBackend registered under {backend_id!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[backend_id]


def list_tool_registry_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time. Matches the
# Profile + Log + Lock registry pattern — the default is always
# available without an extra resolution step.
register_tool_registry_backend("filesystem", FilesystemToolRegistryBackend)

# Register the built-in SQLite backend at import time (#64 PR 3). Same
# pattern as profile/__init__.py:157 — both reference impls land in the
# registry on package import; the operator picks via env var.
register_tool_registry_backend("sqlite", SQLiteToolRegistryBackend)


# ────────────────────────────────────────────────────────────────────
# PR 2 wiring contract — PRE-PR-2 state (describes what WILL be wired
# by PR 2; today, in PR 1 scaffolding, none of these call sites exist).
#
# Wired by #64 PR 2:
#   1. ``AtomicAgent.__init__`` accepts ``tool_registry_backend:
#      ToolRegistryBackend | None``; if unset, calls
#      ``get_default_tool_registry_backend(self.agent_root)``. Public
#      ``self.tool_registry_backend`` mirrors ``self.lock_backend`` /
#      ``self.log_backend`` / ``self.profile_backend``.
#   2. After ``self.tool_registry = tools if tools is not None else ToolRegistry()``
#      (the operator-supplied programmatic registry), insert a loop:
#      ``for ref in self.tool_registry_backend.list_tools():
#          td = self.tool_registry_backend.load_tool(ref.name)
#          self.tool_registry.register(td)``
#      Operator-passed tools register first (operator intent wins on
#      collisions); backend tools register with ``allow_overwrite=False``
#      so collisions surface as ``ToolNameCollision``.
#   3. ``OutcomeRunner``, ``EvalRunner``, ``DreamRunner``, ``delegate.py``
#      accept ``tool_registry_backend=`` kwargs and thread them to
#      internal ``AtomicAgent`` instances.
#   4. ``doctor.check_tool_registry_backend`` validates operator config
#      and reports backend stats; URL-credential-redacted error
#      messages.
#
# DEFERRED (intentional):
#   - SQLite reference impl + install/uninstall (PR 3 of #64).
#   - Skill catalog surface (reserved capability per spec/25 Decision 2).
#   - Sandboxed validation (reserved capability per spec/25 Decision 6).


def get_default_tool_registry_backend(agent_root: Path) -> ToolRegistryBackend:
    """Return the operator-pinned ToolRegistryBackend instance for ``agent_root``.

    Reads ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND`` from the environment
    (default ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL`` for the connection /
    path string. The env var name is intentionally generic so future
    PyPI / Git / HTTP backends plug in via the same key without
    operators having to relearn the env vocabulary. Mirrors the
    ``ATOMIC_AGENTS_PROFILE_BACKEND`` shape spec/24 established.

    The ``agent_root`` parameter is honored by the filesystem backend
    (each agent's tools live under its own ``tools/`` subdirectory);
    future shared-catalog backends ignore it in favor of the database
    URL's connection string + an ``agent_scope`` parameter parsed from
    the URL.

    For programmatic operators who want to construct the backend
    themselves (custom database connection, custom git repo path,
    etc.), the ``AtomicAgent(..., tool_registry_backend=...)``
    constructor kwarg (wired in PR 2) bypasses this factory entirely.

    See spec/25 §"Operator surface" for the full env-var reference +
    the env-var-vs-kwarg trade-off rationale.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "filesystem")
        .strip()
        .lower()
    )

    if raw_backend_id == "filesystem":
        return FilesystemToolRegistryBackend(agent_root)

    if raw_backend_id == "sqlite":
        # SQLite backend reads its db location from the URL env var.
        # If the URL is absent, default to ``<agent_root>/.tools.db``
        # with ``agent_scope=<agent_root.name>`` — single-host operators
        # get a working SQLite default by flipping ONE env var. Mirrors
        # spec/24's same shape for profile_backend.
        url = os.environ.get(
            "ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL", ""
        ).strip()
        if not url:
            return SQLiteToolRegistryBackend(
                agent_root / ".tools.db",
                agent_scope=agent_root.name or "default",
            )
        return make_sqlite_tool_registry_backend_from_url(url)

    # Unknown backend_id — surface a fail-fast error with the FULL
    # known-id list so operators can spot the typo. Credential safety:
    # ``raw_backend_id`` is sanitized before interpolation in case an
    # operator accidentally pastes a URL (e.g.,
    # ``postgres://user:pass@host``) into
    # ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND`` instead of
    # ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL``. Without the
    # sanitize the credential lands in exception text that may be
    # logged by exception handlers, WSGI middleware, or error-tracking
    # services. Same shape applies in ``logs/__init__.py:316`` and
    # ``profile/__init__.py:_redact_for_error_message``.
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND={safe_backend_id!r} is not "
        f"a known backend. Available: {list_tool_registry_backends()}. "
        f"Unset the env var to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and
    truncates at ``max_len`` to bound the echoed string. Returns the
    bare backend_id if no URL marker is present. The full original
    value is never echoed — this prevents the credential-leak failure
    mode where an operator accidentally sets
    ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND=postgres://user:pass@host``
    instead of ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL``.

    Identical shape to ``profile/__init__.py:_redact_for_error_message``
    and ``logs/__init__.py``'s redactor. A future refactor MAY hoist
    the helper to a shared location once a fourth Protocol needs it;
    until then duplicating the 5-line function is cheaper than the
    cross-module dependency.
    """
    # URL-shaped value: keep only the scheme (before "://").
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

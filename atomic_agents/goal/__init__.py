"""GoalBackend — Protocol + registry + canonical types (spec/41).

This package establishes the goal abstraction alongside the other backend
protocols. See docs/spec/41-goal-backend.md for the prose contract.

Public surface:

    from atomic_agents.goal import (
        # Protocol contract
        GoalBackend,
        # Canonical types
        Goal, SubGoal, CompletionEvaluation,
        GoalCapabilities, GoalExport,
        # Reference impl
        FilesystemGoalBackend,
        # Validation helpers (re-exported for backward compat)
        validate_goal, validate_agent_mode,
        parse_agent_mode, parse_agent_mode_text,
        CURRENT_GOAL_SCHEMA_VERSION, VALID_SUB_GOAL_STATUSES,
        # Registry
        register_goal_backend, get_goal_backend,
        list_goal_backends, unregister_goal_backend,
        # Operator-config factory
        get_default_goal_backend,
    )

Backward-compat re-exports: GoalManager, validate_goal, validate_agent_mode,
parse_agent_mode, parse_agent_mode_text, CURRENT_GOAL_SCHEMA_VERSION, and
VALID_SUB_GOAL_STATUSES are all re-exported from this package root so that
existing imports like 'from atomic_agents.goal import GoalManager' keep working.
The old flat module atomic_agents/goal.py no longer exists; this package root
(__init__.py) is the canonical import point.

The registry is a process-local dict keyed by backend_id (e.g. 'filesystem').
Like the Lock/Log registries it stores backend *classes*, not instances — goal
backends are constructed per scope (one per agent root) and the registry's job
is to let an operator pick a backend for a deployment. The caller instantiates
the chosen class with its agent_root argument.

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_goal_backend is read-only and safe to call from any
thread. No lock is needed under that usage.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..exceptions import BackendNotRegistered
from .backend import GoalBackend
from .filesystem import FilesystemGoalBackend
from .types import (
    SUB_GOAL_TRANSITION_FIELDS,
    CompletionEvaluation,
    Goal,
    GoalCapabilities,
    GoalExport,
    SubGoal,
    build_goal_frontmatter,
    serialize_sub_goal,
)

# ── Re-export GoalManager + helpers (backward compat) ──
# GoalManager and its companion helpers (validate_goal, validate_agent_mode,
# parse_agent_mode, parse_agent_mode_text, and the goal constants) live in the
# internal module atomic_agents/_goal_impl.py. They are re-exported from this
# package root via __getattr__ (below) so that existing imports like
# 'from atomic_agents.goal import GoalManager' keep working.
#
# Why __getattr__ rather than a top-level `from .._goal_impl import GoalManager`:
# the load-bearing benefit is CYCLE AVOIDANCE during package bootstrap, NOT a
# lazy-load token saving. profile/filesystem.py does a module-level
# `from ..goal import parse_agent_mode_text`, which runs while
# atomic_agents/__init__.py is still importing; a top-level import of _goal_impl
# here (which itself imports back into the package) would close that cycle and
# break bootstrap. Deferring the _goal_impl import into __getattr__ breaks the
# cycle. (The "lazy = fewer tokens at import" benefit is theoretical: profile/
# filesystem.py's eager import already triggers __getattr__ on every
# `import atomic_agents`, so _goal_impl is pulled in at bootstrap regardless.)
#
# _goal_impl.py is the authoritative home for GoalManager in this arc — moving
# the full class into goal/manager.py is out of scope for the Protocol
# scaffolding PR and is tracked as a follow-up refactor. The canonical types
# and the single validate_goal() already live in goal/types.py; _goal_impl.py
# imports them from there, so there is no second copy of either.


def __getattr__(name: str):
    """Re-export GoalManager and companion helpers from _goal_impl.

    Allows 'from atomic_agents.goal import GoalManager' to keep working after
    the implementation moved into the internal _goal_impl module.

    Implemented via __getattr__ (rather than a top-level import) to BREAK an
    import cycle: profile/filesystem.py imports parse_agent_mode_text from
    atomic_agents.goal during the package's own bootstrap, while
    atomic_agents/__init__.py is still loading. Deferring the _goal_impl import
    into this hook — via a direct relative import (.._goal_impl) that bypasses
    atomic_agents/__init__.py — is what keeps that bootstrap working.
    """
    _GOAL_IMPL_NAMES = {
        "GoalManager",
        "validate_goal",
        "validate_agent_mode",
        "parse_agent_mode",
        "parse_agent_mode_text",
        "CURRENT_GOAL_SCHEMA_VERSION",
        "VALID_SUB_GOAL_STATUSES",
        "VALID_PRIORITIES",
        "VALID_AGENT_MODES",
    }
    if name in _GOAL_IMPL_NAMES:
        # Direct relative import — bypasses atomic_agents/__init__.py so this
        # works even when called during the package's own bootstrap (e.g. when
        # profile/filesystem.py imports parse_agent_mode_text from atomic_agents.goal
        # while atomic_agents/__init__.py is still being loaded).
        from .. import _goal_impl as _impl  # noqa: PLC0415

        val = getattr(_impl, name, None)
        if val is not None:
            return val
    raise AttributeError(f"module 'atomic_agents.goal' has no attribute {name!r}")


_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "GoalBackend",
    # Canonical types
    "Goal",
    "SubGoal",
    "CompletionEvaluation",
    "GoalCapabilities",
    "GoalExport",
    "SUB_GOAL_TRANSITION_FIELDS",
    # Serialization helpers
    "build_goal_frontmatter",
    "serialize_sub_goal",
    # Reference impl
    "FilesystemGoalBackend",
    # Lazy re-exports (resolved via __getattr__)
    "GoalManager",
    "validate_goal",
    "validate_agent_mode",
    "parse_agent_mode",
    "parse_agent_mode_text",
    "CURRENT_GOAL_SCHEMA_VERSION",
    "VALID_SUB_GOAL_STATUSES",
    # Registry
    "register_goal_backend",
    "unregister_goal_backend",
    "get_goal_backend",
    "list_goal_backends",
    # Operator-config factory
    "get_default_goal_backend",
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_goal_backend(backend_id: str, cls: type) -> None:
    """Register a GoalBackend implementation under backend_id.

    Typically called once at module-import time from each backend's package
    (the default 'filesystem' registration happens at the bottom of this file).

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered goal backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_goal_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_goal_backend(backend_id: str) -> type:
    """Return the registered GoalBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., cls(agent_root) for the filesystem backend).
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        raise BackendNotRegistered(
            f"No GoalBackend registered under {backend_id!r}. Available: {known_ids}"
        )
    return _registry[backend_id]


def list_goal_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time.
register_goal_backend("filesystem", FilesystemGoalBackend)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_goal_backend(agent_root: Path) -> GoalBackend:
    """Return the operator-pinned GoalBackend instance for agent_root.

    Reads ATOMIC_AGENTS_GOAL_BACKEND from the environment (default 'filesystem').

    Scoped to ONE agent root — <agent_root>/goal.md and
    <agent_root>/goal_archive/. Mirrors get_default_log_backend(agent_root)
    (NOT get_default_profile_backend which uses agents_root).

    Do NOT gate this factory on 'agent has a goal.md'. The backend is always
    returned; FilesystemGoalBackend.goal_text() returns '' when goal.md is
    absent (reactive agents). This matches the ruling: 'do NOT gate behind
    agent has a goal.md'.

    Programmatic operators who want to construct the backend themselves can
    instantiate FilesystemGoalBackend(agent_root) directly, bypassing this
    factory. (An AtomicAgent(..., goal_backend=...) constructor kwarg that
    bypasses this factory is deferred to the runtime-wiring PR #448; the
    constructor does not accept goal_backend today.)

    Returns:
        A GoalBackend instance scoped to agent_root.

    Raises:
        BackendNotRegistered: when the env var names an unknown backend.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_GOAL_BACKEND", "filesystem").strip().lower()
    )

    if not raw_backend_id or raw_backend_id == "filesystem":
        return FilesystemGoalBackend(agent_root)

    # Operator-registered custom backend: dispatch through the registry so the
    # ATOMIC_AGENTS_GOAL_BACKEND override surface actually works for non-filesystem
    # backends (not just 'filesystem'). The caller instantiates the registered
    # class with its agent_root, mirroring get_goal_backend()'s contract.
    if raw_backend_id in _registry:
        return _registry[raw_backend_id](agent_root)

    # Unknown backend_id — surface a fail-fast error with the known-id list.
    # Credential safety: sanitize the raw value in case an operator pastes a URL.
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_GOAL_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo."""
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

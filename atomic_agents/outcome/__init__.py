"""OutcomeBackend — Protocol + registry + canonical types (spec/42).

This package establishes the outcome abstraction alongside the other backend
protocols. See docs/spec/42-outcome-backend.md for the prose contract.

Public surface:

    from atomic_agents.outcome import (
        # Protocol contract
        OutcomeBackend,
        # Canonical types
        OutcomeResult, IterationRecord,
        OutcomeCapabilities, OutcomeExport,
        # Reference impl
        FilesystemOutcomeBackend,
        # Registry
        register_outcome_backend, get_outcome_backend,
        list_outcome_backends, unregister_outcome_backend,
        # Operator-config factory
        get_default_outcome_backend,
    )

Backward-compat re-exports: OutcomeRunner, OutcomeResult, IterationRecord are
re-exported from this package root so that existing imports like
'from atomic_agents.outcome import OutcomeRunner' keep working.
The old flat module atomic_agents/outcome.py no longer exists; this package root
(__init__.py) is the canonical import point.

The registry is a process-local dict keyed by backend_id (e.g. 'filesystem').
Like the Lock/Log/Goal registries it stores backend *classes*, not instances —
outcome backends are constructed per scope (one per agent root) and the registry's
job is to let an operator pick a backend for a deployment. The caller instantiates
the chosen class with its constructor arguments.

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_outcome_backend is read-only and safe to call from any
thread. No lock is needed under that usage.

IMPORT ORDER NOTE:
    agent.py MUST be imported before outcome/__init__.py because
    outcome/_outcome_impl.py imports AtomicAgent from agent lazily at
    run() time (via `from atomic_agents.outcome import AtomicAgent`).
    The current load order in atomic_agents/__init__.py is safe:
    line 19 imports agent; line 46 imports outcome (this package).
    Do NOT re-order those two lines.

SHIM RE-EXPORTS (test suite backward compat):
    The following names are re-exported at module level so that the
    existing (UNTOUCHED) test files can still import them:
    - OutcomeRunner, OutcomeResult, IterationRecord (public API)
    - _pick_cross_family_judge, _print_result (private, test-facing)
    - DEFAULT_MAX_ITERATIONS, MAX_ITERATIONS_CAP, MIN_ITERATIONS (constants)
    - AtomicAgent (for patch('atomic_agents.outcome.AtomicAgent') to work)
    - _llm (module object, for patch('atomic_agents.outcome._llm.call_llm'))
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..exceptions import BackendNotRegistered
from .backend import OutcomeBackend
from .filesystem import FilesystemOutcomeBackend
from .types import (
    IterationRecord,
    OutcomeCapabilities,
    OutcomeExport,
    OutcomeResult,
)

# ── Re-export OutcomeRunner + helpers (backward compat) ──
# OutcomeRunner and its companion helpers live in the internal module
# atomic_agents/outcome/_outcome_impl.py. They are re-exported here
# at module level (NOT via __getattr__) because there is no circular import
# risk: agent.py does NOT import from outcome at module level (it has no
# `from .outcome import ...` at the top of agent.py).
#
# Module-level re-exports (not __getattr__) are required because:
# 1. atomic_agents/__init__.py line 46 uses `from .outcome import OutcomeRunner, ...`
#    which requires the names to be present at the module top-level.
# 2. The test patch targets `patch("atomic_agents.outcome.AtomicAgent")` require
#    AtomicAgent to be a module-level name in this namespace.
# 3. test_outcome.py imports private names at module level (collection time) which
#    would cause ImportError if deferred behind __getattr__.
#
# Confirmed safe: tracing the import chain shows no cycle:
#   outcome/__init__ → ..agent (direct, see below) — ..agent does not import ..outcome
#   _outcome_impl imports ..agent ONLY lazily inside run() (not at module level)
#   _outcome_impl → NO import of ..outcome at module level
#
from ._outcome_impl import (  # noqa: E402 (below imports intentionally ordered)
    OutcomeRunner,
    # main re-exported so the old public `from atomic_agents.outcome import main`
    # (programmatic CLI wrappers) keeps working after the module->package split.
    main,
    # shim — patchable by test suite
    _pick_cross_family_judge,
    _print_result,
    DEFAULT_MAX_ITERATIONS,
    MAX_ITERATIONS_CAP,
    MIN_ITERATIONS,
)

# AtomicAgent re-exported at module level so patch('atomic_agents.outcome.AtomicAgent')
# rebinds the name in THIS namespace and the lazy import inside _outcome_impl.run()
# (via `from atomic_agents.outcome import AtomicAgent`) resolves to the mock.
# Without this re-export, the patch target does not exist.
from ..agent import AtomicAgent  # noqa: F401 (re-export; shim — patchable by test suite)

# _llm module re-exported so patch('atomic_agents.outcome._llm.call_llm') patches
# the .call_llm attribute on the module object in this namespace.
from .. import _llm  # noqa: F401 (re-export; shim — patchable by test suite)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "OutcomeBackend",
    # Canonical types
    "OutcomeResult",
    "IterationRecord",
    "OutcomeCapabilities",
    "OutcomeExport",
    # Reference impl
    "FilesystemOutcomeBackend",
    # Backward-compat runner re-export
    "OutcomeRunner",
    "main",
    # Backward-compat private names (test-facing shim)
    "_pick_cross_family_judge",
    "_print_result",
    "DEFAULT_MAX_ITERATIONS",
    "MAX_ITERATIONS_CAP",
    "MIN_ITERATIONS",
    # Registry
    "register_outcome_backend",
    "unregister_outcome_backend",
    "get_outcome_backend",
    "list_outcome_backends",
    # Operator-config factory
    "get_default_outcome_backend",
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_outcome_backend(backend_id: str, cls: type) -> None:
    """Register an OutcomeBackend implementation under backend_id.

    Typically called once at module-import time from each backend's package
    (the default 'filesystem' registration happens at the bottom of this file).

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered outcome backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_outcome_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_outcome_backend(backend_id: str) -> type:
    """Return the registered OutcomeBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments.
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        raise BackendNotRegistered(
            f"No OutcomeBackend registered under {backend_id!r}. Available: {known_ids}"
        )
    return _registry[backend_id]


def list_outcome_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time.
register_outcome_backend("filesystem", FilesystemOutcomeBackend)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_outcome_backend(agent_root: Path) -> OutcomeBackend:
    """Return the operator-pinned OutcomeBackend instance for agent_root.

    Reads ATOMIC_AGENTS_OUTCOME_BACKEND from the environment (default 'filesystem').

    Scoped to ONE agent root — <agent_root>/outcomes/runs/<run_id>/result.json.
    Mirrors get_default_goal_backend(agent_root) scope (NOT get_default_profile_backend
    which uses agents_root).

    agents_root is derived as agent_root.parent (the framework-wide invariant
    agents_root / agent_name = agent_root). Operators with non-standard layouts
    (multi-tenant, nested agents) should instantiate FilesystemOutcomeBackend
    directly with explicit agents_root.

    Do NOT gate this factory on 'agent has outcomes/'. The backend is always
    returned; FilesystemOutcomeBackend.list_runs() returns [] when outcomes/runs/
    is absent (agents that have never run an outcome).

    Programmatic operators who want to construct the backend themselves can
    instantiate FilesystemOutcomeBackend(agents_root, agent_name) directly,
    bypassing this factory. As of #448 PR2, OutcomeRunner accepts outcome_backend=
    as a keyword-only kwarg and routes its write path through the backend.
    AtomicAgent.outcome_backend is the per-agent public handle for operator
    inspection and the PR3 coordinator — it is NOT the write path. `write_result`
    is called via OutcomeRunner.outcome_backend, not via AtomicAgent.outcome_backend.

    Returns:
        An OutcomeBackend instance scoped to agent_root.

    Raises:
        BackendNotRegistered: when the env var names an unknown backend.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_OUTCOME_BACKEND", "filesystem").strip().lower()
    )

    if not raw_backend_id or raw_backend_id == "filesystem":
        # Derive agents_root from agent_root.parent (framework invariant:
        # agents_root / agent_name = agent_root). This holds for all standard
        # single-level layouts. Multi-tenant operators with nested layouts
        # should pass FilesystemOutcomeBackend directly.
        return FilesystemOutcomeBackend(agent_root.parent, agent_root.name)

    # Operator-registered custom backend: dispatch through the registry so the
    # ATOMIC_AGENTS_OUTCOME_BACKEND override surface actually works for non-filesystem
    # backends. The caller instantiates the registered class with agents_root +
    # agent_name (matching FilesystemOutcomeBackend's constructor signature).
    if raw_backend_id in _registry:
        return _registry[raw_backend_id](agent_root.parent, agent_root.name)

    # Unknown backend_id — surface a fail-fast error with the known-id list.
    # Credential safety: sanitize the raw value in case an operator pastes a URL.
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_OUTCOME_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic), redacts a
    schemeless ``user:pass@host/db`` DSN, then truncates at ``max_len`` to
    bound the echoed string. The full original value is never echoed -- this
    prevents the credential-leak failure mode where an operator accidentally
    pastes a connection string into ``ATOMIC_AGENTS_OUTCOME_BACKEND``.

    Mirrors ``logs/__init__.py``, ``profile/__init__.py``,
    ``corpus/__init__.py``, ``mcp_registry/__init__.py``,
    ``secret_backend/__init__.py``, and ``goal/__init__.py``
    ``_redact_for_error_message``.
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

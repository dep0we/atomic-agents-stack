"""Log abstraction layer — Protocol + registry + canonical types.

This package establishes the log abstraction in the protocol-pattern
series alongside MemoryBackend (#57, shipped), LLMBackend (#87,
shipped), JudgeBackend (#112, shipped), LockBackend (#60, shipped),
and the remaining Tier-2 backends. See
``docs/spec/22-log-backend.md`` for the prose contract.

Public surface (scaffolding PR — no behavior change today):

    from atomic_agents.logs import (
        # Protocol contract
        LogBackend,
        # Canonical types
        RunRecord, LogQuery, LogAggregate, LogStats, LogCapabilities,
        # Reference impl
        FilesystemLogBackend,
        # Registry
        register_log_backend, get_log_backend,
        list_log_backends, unregister_log_backend,
        # Operator-config factory
        get_default_log_backend,
    )

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"sqlite"``). Like the
Lock registry it stores backend *classes*, not instances — log backends
are constructed per scope (one per agent root) and the registry's job
is to let an operator pick "filesystem vs sqlite vs datadog" for a
deployment. The caller (``AtomicAgent.__init__`` in PR 2) instantiates
the chosen class with its scope-specific args.

Thread-safety: registration is expected at import time (one-shot from
each backend's module); ``get_log_backend`` is read-only and safe to
call from any thread. No lock is needed under that usage.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..exceptions import BackendNotRegistered
from .backend import LogBackend
from .filesystem import FilesystemLogBackend
from .types import (
    LogAggregate,
    LogCapabilities,
    LogQuery,
    LogStats,
    METRIC_AVG_LATENCY_MS,
    METRIC_COUNT,
    METRIC_SUM_COST_USD,
    METRIC_SUM_INPUT_TOKENS,
    METRIC_SUM_OUTPUT_TOKENS,
    PRIMITIVE_AGENT_CALL,
    PRIMITIVE_CAPTURE,
    PRIMITIVE_COST_WARNING,
    PRIMITIVE_DELEGATE,
    PRIMITIVE_DREAM,
    PRIMITIVE_ESCALATION,
    PRIMITIVE_EVAL,
    PRIMITIVE_HELPER,
    PRIMITIVE_JUDGMENT,
    PRIMITIVE_OTHER,
    PRIMITIVE_OUTCOME_ITERATION,
    PRIMITIVE_TOOL,
    RunRecord,
    VALID_METRICS,
)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "LogBackend",
    # Canonical types
    "RunRecord",
    "LogQuery",
    "LogAggregate",
    "LogStats",
    "LogCapabilities",
    # Metric constants
    "METRIC_COUNT",
    "METRIC_SUM_COST_USD",
    "METRIC_SUM_INPUT_TOKENS",
    "METRIC_SUM_OUTPUT_TOKENS",
    "METRIC_AVG_LATENCY_MS",
    "VALID_METRICS",
    # Primitive constants
    "PRIMITIVE_AGENT_CALL",
    "PRIMITIVE_OUTCOME_ITERATION",
    "PRIMITIVE_DREAM",
    "PRIMITIVE_EVAL",
    "PRIMITIVE_HELPER",
    "PRIMITIVE_DELEGATE",
    "PRIMITIVE_TOOL",
    "PRIMITIVE_COST_WARNING",
    "PRIMITIVE_CAPTURE",
    "PRIMITIVE_ESCALATION",
    "PRIMITIVE_JUDGMENT",
    "PRIMITIVE_OTHER",
    # Reference implementations
    "FilesystemLogBackend",
    # Registry
    "register_log_backend",
    "unregister_log_backend",
    "get_log_backend",
    "list_log_backends",
    # Operator-config factory
    "get_default_log_backend",
]


# Process-local registry: backend_id → backend class. Backend classes
# (not instances) because log backends carry per-scope construction
# args — the agent instantiates ``FilesystemLogBackend(agent_root)``
# at agent init time; the registry's role is the operator-pin lookup
# that maps ``"filesystem"`` → ``FilesystemLogBackend``.
_registry: dict[str, type] = {}


def register_log_backend(backend_id: str, cls: type) -> None:
    """Register a LogBackend implementation under ``backend_id``.

    Typically called once at module-import time from each backend's
    package (the default ``"filesystem"`` registration happens at the
    bottom of this file).

    Re-registering the same ``backend_id`` replaces the existing
    binding and logs at DEBUG — intentional. Operators occasionally
    want to swap in a wrapper (e.g., a ``MetricsLogBackend`` that
    decorates the filesystem backend with timing data) without first
    unregistering the original.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered log backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_log_backend(backend_id: str) -> None:
    """Remove a backend by ``backend_id``. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a
    backend.
    """
    _registry.pop(backend_id, None)


def get_log_backend(backend_id: str) -> type:
    """Return the registered LogBackend class for ``backend_id``.

    Raises ``BackendNotRegistered`` when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., ``cls(agent_root)`` for the filesystem
    backend; ``cls(db_path)`` for a future ``SQLiteLogBackend``).
    """
    if backend_id not in _registry:
        raise BackendNotRegistered(
            f"No LogBackend registered under {backend_id!r}. "
            f"Available: {list(_registry.keys())}"
        )
    return _registry[backend_id]


def list_log_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time. Matches the
# Lock registry pattern (``atomic_agents/locks/__init__.py:139``) —
# the default is always available without an extra resolution step.
register_log_backend("filesystem", FilesystemLogBackend)


def get_default_log_backend(scope_root: Path) -> LogBackend:
    """Return the operator-pinned LogBackend instance for ``scope_root``.

    Reads ``ATOMIC_AGENTS_LOG_BACKEND`` from the environment (default
    ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_LOG_BACKEND_URL`` and any backend-specific tuning
    env vars (PR 3 adds the SQLite-specific ones; the env var name is
    intentionally generic so future Datadog / Postgres / Loki backends
    plug in via the same key).

    The ``scope_root`` parameter is honored by the filesystem backend
    (log files live under that path); future distributed backends
    ignore it in favor of the table-prefix or key-prefix scoping
    inherent to their storage.

    For programmatic operators who want to construct the backend
    themselves (custom SQLite path, custom Datadog client, etc.), the
    ``AtomicAgent(..., log_backend=...)`` constructor kwarg (wired in
    PR 2) bypasses this factory entirely.

    See spec/22 §"Operator surface" for the full env-var reference +
    the env-var-vs-kwarg trade-off rationale.
    """
    backend_id = os.environ.get(
        "ATOMIC_AGENTS_LOG_BACKEND", "filesystem"
    ).strip().lower()

    if backend_id == "filesystem":
        return FilesystemLogBackend(scope_root)

    # Unknown backend_id — surface a fail-fast error with the FULL
    # known-id list so operators can spot the typo. Includes the
    # lazy-resolved ``"sqlite"`` that PR 3 will ship even though it
    # isn't in the eager registry yet — same forward-pointer pattern
    # spec/21 §"Operator surface" uses to dodge the Step 11 adversarial
    # P0-3 finding (operators who typed ``redus`` got "Available:
    # ['filesystem']" and concluded Redis wasn't supported).
    known_ids = sorted(set(list_log_backends()) | {"sqlite"})
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_LOG_BACKEND={backend_id!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var to use "
        f"the filesystem default."
    )

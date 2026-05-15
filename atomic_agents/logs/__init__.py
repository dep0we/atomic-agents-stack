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
from .sqlite import SQLiteLogBackend, make_sqlite_backend_from_url
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
    "SQLiteLogBackend",
    # URL factories
    "make_sqlite_backend_from_url",
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

    The error message includes the FULL known-id list (eager registry +
    lazy ``"sqlite"`` forward-pointer) — same shape as
    ``get_default_log_backend`` to keep both raise sites consistent.
    """
    if backend_id not in _registry:
        # ``sqlite`` was a forward-pointer in PR 1/PR 2; eagerly
        # registered in PR 3. The known-id list comes directly from
        # the registry — no union needed.
        raise BackendNotRegistered(
            f"No LogBackend registered under {backend_id!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[backend_id]


def list_log_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time. Matches the
# Lock registry pattern (``atomic_agents/locks/__init__.py:139``) —
# the default is always available without an extra resolution step.
register_log_backend("filesystem", FilesystemLogBackend)


# Register the SQLite backend at import time (#61 PR 3). Stdlib
# ``sqlite3`` — no optional dependency, no install step. Eager
# registration matches FilesystemLogBackend's pattern; future
# external-dependency backends (Postgres, Datadog) will lazy-register
# inside ``get_default_log_backend`` to keep framework startup cost
# down for operators who haven't selected them.
register_log_backend("sqlite", SQLiteLogBackend)


# ────────────────────────────────────────────────────────────────────
# PR 2 wiring contract — post-PR-2 state (this block describes what
# IS wired today, not what's planned).
#
# Background — the trap shape: the lock arc PR 3 Step 11 adversarial
# caught ``DreamRunner`` silently dropping the operator's
# ``lock_backend`` kwarg by constructing its own lock backend
# instance. The log arc has the same trap surface across multiple
# runners and readers.
#
# Wired by #61 PR 2:
#   1. ``agent._log()`` is a thin wrapper that builds a RunRecord
#      (deriving ``primitive`` from the legacy ``trigger``, defaulting
#      ``run_id`` to ``self.run_id``) and calls
#      ``self.log_backend.append(...)``.
#   2. ``OutcomeRunner._append_iteration_log`` routes through
#      ``agent.log_backend.append(...)`` — never constructs its own.
#      OutcomeRunner accepts ``log_backend=`` kwarg (mirrors AtomicAgent
#      pattern), threaded to the internal AtomicAgent at ``run()``.
#   3. ``DreamRunner`` accepts ``log_backend=`` kwarg; threaded through
#      to ``_read_log_lines`` and ``_check_cap`` via ``_run_pipeline``.
#   4. ``_costs.sum_cost_for_period`` accepts ``backend=`` kwarg —
#      every call site in ``agent.py`` and ``dream.py`` passes
#      ``self.log_backend``. The filesystem backend specifically
#      preserves the legacy file-walk semantic (date-from-file-path,
#      not record.ts) to keep cost guardrails safe against malformed
#      legacy records — Step 11 adversarial P0 #4. SQL/Datadog
#      backends (PR 3+) use the indexed ``query()`` path.
#   5. ``dashboard/costs.load_runs`` + ``dashboard/quality._count_
#      provenance`` route through ``get_default_log_backend(agents_
#      root / agent).query(LogQuery(...))`` — env-var-resolved
#      backend matches the runtime's choice.
#   6. ``AtomicAgent.__init__`` accepts ``log_backend: LogBackend |
#      None``; if unset, calls ``get_default_log_backend(self.agent_
#      root)``. Public ``self.log_backend`` mirrors ``self.lock_
#      backend`` / ``self.memory``.
#   7. ``doctor.check_log_backend`` validates operator config and
#      reports backend stats; URL-credential-redacted error messages.
#
# DEFERRED (intentional):
#   - ``EvalRunner._write_run_log`` writes to ``evals/runs/<date>
#     .jsonl`` — a SEPARATE artifact from the agent's daily log dir.
#     Cross-primitive routing through ``agent.log_backend`` with a
#     ``primitive="eval"`` taxonomy entry is PR 3 scope per spec/22
#     §"Cross-primitive run records".
#   - ``dream.py`` manifest writes go to ``dreams/runs/<date>.jsonl``
#     — also separate; same PR-3 reroute story as evals.


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
    raw_backend_id = os.environ.get(
        "ATOMIC_AGENTS_LOG_BACKEND", "filesystem"
    ).strip().lower()

    if raw_backend_id == "filesystem":
        return FilesystemLogBackend(scope_root)

    if raw_backend_id == "sqlite":
        # SQLite backend was registered eagerly at module load (line
        # 196 below). Use the registry-returned class for the no-URL
        # path; route through ``make_sqlite_backend_from_url`` for
        # the URL-set path so URL parsing logic stays in one place.
        url = os.environ.get("ATOMIC_AGENTS_LOG_BACKEND_URL")
        if not url:
            # No URL → default to ``<scope_root>/.logs.db`` (sibling of
            # the agent's log/ dir). Operators who want a custom path
            # set ATOMIC_AGENTS_LOG_BACKEND_URL=sqlite:///path/to.db.
            return SQLiteLogBackend(scope_root / ".logs.db")
        return make_sqlite_backend_from_url(url)

    # Unknown backend_id — surface a fail-fast error with the FULL
    # known-id list so operators can spot the typo. Includes the
    # lazy-resolved ``"sqlite"`` (now registered in PR 3) so the
    # error message remains stable as the registry evolves.
    #
    # Credential safety: ``raw_backend_id`` is sanitized before
    # interpolation in case an operator accidentally pastes a URL
    # (e.g., ``datadog://api-key@host``) into ``ATOMIC_AGENTS_LOG_BACKEND``
    # instead of ``ATOMIC_AGENTS_LOG_BACKEND_URL``. Without the sanitize
    # the credential lands in exception text that may be logged by
    # exception handlers, WSGI middleware, or error-tracking services.
    # Same fix applies to ``locks/__init__.py:194`` per the systemic
    # gap Step 9.1 security specialist surfaced.
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    # ``sqlite`` is eagerly registered as of PR 3; no forward-pointer
    # union needed.
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_LOG_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {list_log_backends()}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and truncates
    at ``max_len`` to bound the echoed string. Returns the bare backend_id
    if no URL marker is present. The full original value is never echoed
    — this prevents the credential-leak failure mode where an operator
    accidentally sets ``ATOMIC_AGENTS_LOG_BACKEND=datadog://api-key@host``
    instead of ``ATOMIC_AGENTS_LOG_BACKEND_URL``.
    """
    # URL-shaped value: keep only the scheme (before "://").
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

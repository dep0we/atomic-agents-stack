"""CorpusBackend Protocol and registry (spec/34, issue #65).

This package establishes the corpus abstraction in the protocol-pattern
series alongside MemoryBackend (#57, shipped), LLMBackend (#87,
shipped), JudgeBackend (#112, shipped), LockBackend (#60, shipped),
LogBackend (#61, shipped), AgentProfileBackend (#63, shipped),
ToolRegistryBackend (#64, shipped), MandateBackend (#124, shipped),
PolicyBackend (#89, shipped), and PersonaBackend (#62, shipped).
See ``docs/spec/34-corpus-backend.md`` for the prose contract.

Public surface (scaffolding PR -- no behavior change today):

    from atomic_agents.corpus import (
        # Protocol contract
        CorpusBackend,
        # Canonical types
        CorpusCapabilities, CorpusPage, CorpusRef, CorpusStats,
        # Reference impl
        FilesystemCorpusBackend,
        # URL factory
        make_filesystem_corpus_backend_from_url,
        # Registry
        register_corpus_backend, unregister_corpus_backend,
        get_corpus_backend, list_corpus_backends,
        # Operator-config factory
        get_default_corpus_backend,
    )

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"sqlite"``). Like the
Log + Lock + Profile registries it stores backend *classes*, not
instances -- corpus backends are constructed per agent scope and the
registry's job is to let an operator pick "filesystem vs sqlite vs
pgvector" for a deployment. The caller (``AtomicAgent.__init__`` in PR 3)
instantiates the chosen class with its scope-specific args.

Thread-safety: registration is expected at import time (one-shot from
each backend's module); ``get_corpus_backend`` is read-only and safe to
call from any thread. No lock is needed under that usage.

Per /plan-eng-review 2026-05-29 finding A4: registry lives in __init__.py
(the dominant 6-of-10 placement: locks, llm, logs, profile, registry, judge --
NOT the 2-of-10 backend.py placement Mandate + Persona used).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..exceptions import CorpusBackendNotRegistered
from .backend import CorpusBackend
from .filesystem import (
    FilesystemCorpusBackend,
    make_filesystem_corpus_backend_from_url,
)
from .sqlite import (
    SQLiteCorpusBackend,
    make_sqlite_corpus_backend_from_url,
)
from .types import (
    CorpusCapabilities,
    CorpusPage,
    CorpusRef,
    CorpusStats,
)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "CorpusBackend",
    # Canonical types
    "CorpusCapabilities",
    "CorpusPage",
    "CorpusRef",
    "CorpusStats",
    # Reference implementations
    "FilesystemCorpusBackend",
    "SQLiteCorpusBackend",
    # URL factories
    "make_filesystem_corpus_backend_from_url",
    "make_sqlite_corpus_backend_from_url",
    # Registry
    "register_corpus_backend",
    "unregister_corpus_backend",
    "get_corpus_backend",
    "list_corpus_backends",
    # Operator-config factory
    "get_default_corpus_backend",
]


# Process-local registry: backend_id -> backend class. Backend classes
# (not instances) because corpus backends carry per-scope construction
# args -- the framework instantiates ``FilesystemCorpusBackend(agent_root)``
# per agent; the registry's role is the operator-pin lookup that maps
# ``"filesystem"`` -> ``FilesystemCorpusBackend``.
_registry: dict[str, type] = {}


def register_corpus_backend(backend_id: str, cls: type) -> None:
    """Register a CorpusBackend implementation under ``backend_id``.

    Typically called once at module-import time from each backend's
    package (the default ``"filesystem"`` registration happens at the
    bottom of this file).

    Re-registering the same ``backend_id`` replaces the existing
    binding and logs at DEBUG -- intentional. Operators occasionally
    want to swap in a wrapper (e.g., a ``CachingCorpusBackend`` that
    decorates the filesystem backend) without first unregistering
    the original.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered corpus backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_corpus_backend(backend_id: str) -> None:
    """Remove a backend by ``backend_id``. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a
    backend.
    """
    _registry.pop(backend_id, None)


def get_corpus_backend(backend_id: str) -> type:
    """Return the registered CorpusBackend class for ``backend_id``.

    Raises ``CorpusBackendNotRegistered`` when the id is not in the
    registry. The caller instantiates the returned class with its
    scope-specific constructor arguments (e.g., ``cls(agent_root)``
    for the filesystem backend; ``cls(db_path)`` for a future
    ``SQLiteCorpusBackend``).
    """
    if backend_id not in _registry:
        raise CorpusBackendNotRegistered(
            f"No CorpusBackend registered under {backend_id!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[backend_id]


def list_corpus_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in backends at import time. Matches the
# Log + Lock + Profile registry pattern -- the defaults are always
# available without an extra resolution step.
register_corpus_backend("filesystem", FilesystemCorpusBackend)
register_corpus_backend("sqlite", SQLiteCorpusBackend)


# ────────────────────────────────────────────────────────────────────
# PR 3 wiring contract -- PRE-PR-3 state (describes what WILL be wired
# by PR 3; today, in PR 1 scaffolding, none of these call sites exist).
#
# Wired by #65 PR 3:
#   1. ``AtomicAgent.__init__`` accepts ``corpus_backend:
#      CorpusBackend | None``; if unset, calls
#      ``get_default_corpus_backend(self.agent_root)``. Public
#      ``self.corpus_backend`` mirrors ``self.log_backend`` /
#      ``self.profile_backend``.
#   2. ``agent.py:2937-2939`` wiki-index read routes through
#      ``self.corpus_backend.render_index_summary("wiki")`` instead
#      of the raw ``Path.read_text()`` call.
#   3. ``bundle.py:_render_memory_breakpoint`` routes through
#      ``corpus_backend.render_index_summary("wiki")``.
#   4. ``DreamRunner``, ``OutcomeRunner``, ``EvalRunner`` accept
#      ``corpus_backend=`` kwarg and thread it to the internal
#      ``AtomicAgent`` instance.
#   5. ``doctor.check_corpus_backend`` validates operator config and
#      reports backend stats (page count cliff WARN at ~1000 pages
#      without FTS per plan-eng-review 2026-05-29 finding P1);
#      URL-credential-redacted error messages.
#
# DEFERRED (intentional):
#   - ``SQLiteCorpusBackend`` with FTS5 -- PR 2 scope.
#   - Semantic search (pgvector, embedding provider) -- PR 2+ scope.


def get_default_corpus_backend(agent_root: Path) -> CorpusBackend:
    """Return the operator-pinned CorpusBackend instance for ``agent_root``.

    Reads ``ATOMIC_AGENTS_CORPUS_BACKEND`` from the environment (default
    ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_CORPUS_BACKEND_URL`` for the connection / path
    string. The env var name is intentionally generic so future
    SQLite / Postgres / pgvector backends plug in via the same key
    without operators having to relearn the env vocabulary.

    The ``agent_root`` parameter is honored by the filesystem backend
    (wiki/ and raw/ subdirs live under that path); future distributed
    backends ignore it in favor of the table-prefix or key-prefix
    scoping inherent to their storage.

    For programmatic operators who want to construct the backend
    themselves (custom database connection, custom path, etc.), the
    ``AtomicAgent(..., corpus_backend=...)`` constructor kwarg (wired
    in PR 3) bypasses this factory entirely.

    See spec/34 for the full env-var reference + the env-var-vs-kwarg
    trade-off rationale.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_CORPUS_BACKEND", "filesystem").strip().lower()
    )

    if raw_backend_id == "filesystem":
        return FilesystemCorpusBackend(agent_root)

    # Unknown backend_id -- surface a fail-fast error with the FULL
    # known-id list so operators can spot the typo. Credential safety:
    # ``raw_backend_id`` is sanitized before interpolation in case an
    # operator accidentally pastes a URL (e.g., ``postgres://user:pass@host``)
    # into ``ATOMIC_AGENTS_CORPUS_BACKEND`` instead of
    # ``ATOMIC_AGENTS_CORPUS_BACKEND_URL``. Without the sanitize the
    # credential lands in exception text that may be logged by
    # exception handlers, WSGI middleware, or error-tracking services.
    # Same shape applies in ``logs/__init__.py`` and
    # ``profile/__init__.py`` (already fixed in their respective arcs).
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    raise CorpusBackendNotRegistered(
        f"ATOMIC_AGENTS_CORPUS_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {list_corpus_backends()}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and
    truncates at ``max_len`` to bound the echoed string. Returns the
    bare backend_id if no URL marker is present. The full original
    value is never echoed -- this prevents the credential-leak failure
    mode where an operator accidentally sets
    ``ATOMIC_AGENTS_CORPUS_BACKEND=postgres://user:pass@host`` instead
    of ``ATOMIC_AGENTS_CORPUS_BACKEND_URL``.
    """
    # URL-shaped value: keep only the scheme (before "://").
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

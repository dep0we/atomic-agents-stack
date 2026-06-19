"""CorpusBackend Protocol and registry (spec/34, issue #65).

This package establishes the corpus abstraction in the protocol-pattern
series alongside MemoryBackend (#57, shipped), LLMBackend (#87,
shipped), JudgeBackend (#112, shipped), LockBackend (#60, shipped),
LogBackend (#61, shipped), AgentProfileBackend (#63, shipped),
ToolRegistryBackend (#64, shipped), MandateBackend (#124, shipped),
PolicyBackend (#89, shipped), and PersonaBackend (#62, shipped).
See ``docs/spec/34-corpus-backend.md`` for the prose contract.

Public surface:

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

``PgvectorCorpusBackend`` (backend_id ``"pgvector-corpus"``) is a lazy-
imported extension that wraps ``FilesystemCorpusBackend`` with ANN-based
``query()`` using a pgvector Postgres index.  Requires the ``[pgvector]``
extra.  Dispatched from ``get_default_corpus_backend()`` when
``ATOMIC_AGENTS_CORPUS_BACKEND=pgvector-corpus``.

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"sqlite"``). Like the
Log + Lock + Profile registries it stores backend *classes*, not
instances -- corpus backends are constructed per agent scope and the
registry's job is to let an operator pick "filesystem vs sqlite vs
pgvector" for a deployment. The caller (``AtomicAgent.__init__``)
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
    # Lazy-imported extensions (not pre-imported — require [pgvector] extra)
    # "PgvectorCorpusBackend",
]


# Process-local registry: backend_id -> backend class. Backend classes
# (not instances) because corpus backends carry per-scope construction
# args -- the framework instantiates ``FilesystemCorpusBackend(agent_root)``
# per agent; the registry's role is the operator-pin lookup that maps
# ``"filesystem"`` -> ``FilesystemCorpusBackend``.
_registry: dict[str, type] = {}

# Lazy-resolved backend ids (require the [pgvector] extra; not registered at
# module load).  Listed here so the CorpusBackendNotRegistered "Available:"
# message and doctor introspection surface them as known ids before the extra
# is installed — mirrors ``_LAZY_BACKEND_IDS`` in ``atomic_agents.memory``.
# Actual registration happens in get_default_corpus_backend() on first use.
_LAZY_BACKEND_IDS: frozenset[str] = frozenset({"pgvector-corpus"})


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
        known = sorted(set(_registry.keys()) | _LAZY_BACKEND_IDS)
        raise CorpusBackendNotRegistered(
            f"No CorpusBackend registered under {backend_id!r}. Available: {known}"
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


# Wiring contract (all items below landed in #65 PR 3, locked at #65 PR 4):
#   - AtomicAgent.__init__ accepts the corpus_backend kwarg + resolves the default
#     via get_default_corpus_backend(self.agent_root) when not supplied.
#   - OutcomeRunner, EvalRunner, DreamRunner all accept corpus_backend per-runner
#     kwargs (OutcomeRunner threads in outcome/_outcome_impl.py, EvalRunner at eval.py:363,
#     DreamRunner stores as self._corpus_backend for API parity).
#   - delegate.py threads corpus_backend ONLY when supplied explicitly via the
#     AtomicAgent constructor kwarg (_corpus_backend_was_explicit flag tracking).
#   - ATOMIC_AGENTS_CORPUS_BACKEND env var + optional ATOMIC_AGENTS_CORPUS_BACKEND_URL
#     resolve via get_default_corpus_backend.
#   - doctor.check_corpus_backend lands with PASS/WARN/FAIL ladder + page-count cliff.
#   - agent.py:_load_indexes() routes wiki/INDEX.md through render_index_summary("wiki").
#   - bundle.py:_render_memory_breakpoint accepts corpus_backend parameter.
#
# DEFERRED (intentional):
#   - Semantic search (pgvector, embedding provider): ships in the coordinated #258
#     Postgres-adapter family release alongside PgvectorMemoryBackend so semantic-
#     search coverage stays symmetric across both substrates.


def get_default_corpus_backend(agent_root: Path) -> CorpusBackend:
    """Return the operator-pinned CorpusBackend instance for ``agent_root``.

    Reads ``ATOMIC_AGENTS_CORPUS_BACKEND`` from the environment (default
    ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_CORPUS_BACKEND_URL`` for the connection / path
    string. The env var name is intentionally generic so future
    SQLite / Postgres / pgvector backends plug in via the same key
    without operators having to relearn the env vocabulary.

    An empty string (or whitespace-only) value for
    ``ATOMIC_AGENTS_CORPUS_BACKEND`` is treated as "not set" and falls
    back to the filesystem default. This guards against shell
    ``export ATOMIC_AGENTS_CORPUS_BACKEND=`` accidents without masking
    an accidental URL paste -- the doctor (Stream E) surfaces the case
    where ``ATOMIC_AGENTS_CORPUS_BACKEND_URL`` is set but
    ``ATOMIC_AGENTS_CORPUS_BACKEND`` is unset, emitting a WARN so the
    operator can correct the misconfiguration.

    The ``agent_root`` parameter is honored by the filesystem backend
    (wiki/ and raw/ subdirs live under that path) and by the sqlite
    backend when no URL is supplied (db path defaults to
    ``<agent_root>/.corpus.db``). Future distributed backends ignore it
    in favor of the table-prefix or key-prefix scoping inherent to
    their storage.

    For programmatic operators who want to construct the backend
    themselves (custom database connection, custom path, etc.), the
    ``AtomicAgent(..., corpus_backend=...)`` constructor kwarg bypasses this factory entirely.

    See spec/34 §"Operator override surface" for the full env-var
    reference + the env-var-vs-kwarg trade-off rationale.
    """
    raw_backend_id = os.environ.get("ATOMIC_AGENTS_CORPUS_BACKEND", "").strip().lower()

    # Change 2: empty string (or whitespace-only) treated as "not set";
    # falls through to the filesystem branch below. Matches the shell
    # ``export ATOMIC_AGENTS_CORPUS_BACKEND=`` accident case.
    if not raw_backend_id:
        raw_backend_id = "filesystem"

    if raw_backend_id == "filesystem":
        # Change 3: filesystem URL support (spec/34 line 472 parity).
        # When ATOMIC_AGENTS_CORPUS_BACKEND_URL is set alongside
        # ATOMIC_AGENTS_CORPUS_BACKEND=filesystem, route through the URL
        # factory so operators can supply a non-default agent_root path.
        # When no URL is set, use the legacy direct construction -- this
        # preserves byte-identical pre-#65 behavior for all existing agents.
        url = os.environ.get("ATOMIC_AGENTS_CORPUS_BACKEND_URL", "").strip()
        if url:
            return make_filesystem_corpus_backend_from_url(url)
        return FilesystemCorpusBackend(agent_root)

    # Change 1: SQLite branch (spec/34 §"Operator override surface").
    # Mirrors profile/__init__.py:227-235 exactly. When no URL is set,
    # defaults to sqlite:///<agent_root>/.corpus.db?agent_scope=<agent_root.name>
    # so single-host operators get a working default by flipping ONE env var.
    if raw_backend_id == "sqlite":
        url = os.environ.get("ATOMIC_AGENTS_CORPUS_BACKEND_URL", "").strip()
        if not url:
            # Build the default URL from agent_root. Require a non-empty
            # name component -- a root path (e.g., Path("/")) has an empty
            # name and would produce a meaningless agent_scope.
            if not agent_root.name:
                raise CorpusBackendNotRegistered(
                    f"ATOMIC_AGENTS_CORPUS_BACKEND=sqlite default requires "
                    f"agent_root with a non-empty name (got {agent_root}). "
                    f"Set ATOMIC_AGENTS_CORPUS_BACKEND_URL to override."
                )
            # URL-encode agent_root.name so names containing URL metacharacters
            # (spaces, +, &, ?, =) don't silently corrupt the agent_scope or
            # raise ValueError from the URL factory's query-parameter parser.
            # Without quote_plus, an agent named "my+agent" would have its
            # agent_scope decoded as "my agent" (parse_qsl interprets + as
            # space), causing cross-scope contamination with another agent
            # genuinely named "my agent".
            from urllib.parse import quote_plus

            db_path = agent_root / ".corpus.db"
            url = f"sqlite:///{db_path}?agent_scope={quote_plus(agent_root.name)}"
        try:
            return make_sqlite_corpus_backend_from_url(url)
        except CorpusBackendNotRegistered:
            raise
        except Exception as e:
            # Broad catch (mirrors doctor.check_corpus_backend) so any
            # construction failure becomes a clean operator-facing
            # CorpusBackendNotRegistered with the URL remedy. Covers OSError /
            # PermissionError (read-only mount, non-existent parent dir),
            # ValueError (malformed URL, invalid agent_scope charset), and
            # sqlite3.OperationalError (db locked at first connection, WAL
            # transition failure on NFS) without leaking raw library exceptions.
            raise CorpusBackendNotRegistered(
                f"ATOMIC_AGENTS_CORPUS_BACKEND=sqlite: cannot create db "
                f"(cause: {type(e).__name__}: {e!s}). Set "
                f"ATOMIC_AGENTS_CORPUS_BACKEND_URL=sqlite:///path/to/corpus.db "
                f"to use a different path."
            ) from e

    # pgvector-corpus: ANN-based corpus backend backed by pgvector Postgres
    # index + FilesystemCorpusBackend page storage.  Requires the [pgvector]
    # extra.  Lazy-imported like the postgres memory backend -- not registered
    # at module load time.
    if raw_backend_id == "pgvector-corpus":
        from .pgvector import PgvectorCorpusBackend  # noqa: PLC0415

        # Register BEFORE constructing so list_corpus_backends() reports the id
        # as known even if construction then fails — symmetric with the memory
        # dispatcher's register-then-construct order (get_default_memory_backend).
        if "pgvector-corpus" not in _registry:
            register_corpus_backend("pgvector-corpus", PgvectorCorpusBackend)
        pgvector_url = os.environ.get("ATOMIC_AGENTS_PGVECTOR_URL", "").strip() or None
        return PgvectorCorpusBackend(agent_root, pgvector_url=pgvector_url)

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
    known = sorted(set(list_corpus_backends()) | _LAZY_BACKEND_IDS)
    raise CorpusBackendNotRegistered(
        f"ATOMIC_AGENTS_CORPUS_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {known}. Unset the env var "
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

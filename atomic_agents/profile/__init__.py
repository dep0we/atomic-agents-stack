"""Agent-profile abstraction layer — Protocol + registry + canonical types.

This package establishes the agent-profile abstraction in the protocol-
pattern series alongside MemoryBackend (#57, shipped), LLMBackend (#87,
shipped), JudgeBackend (#112, shipped), LockBackend (#60, shipped),
LogBackend (#61, shipped), and the remaining Tier-2 backends. See
``docs/spec/24-agent-profile-backend.md`` for the prose contract.

Public surface (scaffolding PR — no behavior change today):

    from atomic_agents.profile import (
        # Protocol contract
        AgentProfileBackend,
        # Canonical types
        AgentProfile, ProfileSnapshot, ProfileCapabilities,
        AGENT_MODE_REACTIVE, AGENT_MODE_GOAL_DRIVEN, AGENT_MODE_HYBRID,
        # Reference impl
        FilesystemAgentProfileBackend,
        # Registry
        register_profile_backend, get_profile_backend,
        list_profile_backends, unregister_profile_backend,
        # Operator-config factory
        get_default_profile_backend,
    )

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"sqlite"``). Like the
Log + Lock registries it stores backend *classes*, not instances — agent-
profile backends are constructed per scope (one per ``agents_root``) and
the registry's job is to let an operator pick "filesystem vs sqlite vs
git" for a deployment. The caller (``AtomicAgent.__init__`` in PR 2)
instantiates the chosen class with its scope-specific args.

Thread-safety: registration is expected at import time (one-shot from
each backend's module); ``get_profile_backend`` is read-only and safe to
call from any thread. No lock is needed under that usage.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..exceptions import BackendNotRegistered
from .backend import AgentProfileBackend
from .filesystem import FilesystemAgentProfileBackend
from .types import (
    AGENT_MODE_GOAL_DRIVEN,
    AGENT_MODE_HYBRID,
    AGENT_MODE_REACTIVE,
    AgentProfile,
    ProfileCapabilities,
    ProfileSnapshot,
)

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "AgentProfileBackend",
    # Canonical types
    "AgentProfile",
    "ProfileSnapshot",
    "ProfileCapabilities",
    # Mode constants
    "AGENT_MODE_REACTIVE",
    "AGENT_MODE_GOAL_DRIVEN",
    "AGENT_MODE_HYBRID",
    # Reference implementations
    "FilesystemAgentProfileBackend",
    # Registry
    "register_profile_backend",
    "unregister_profile_backend",
    "get_profile_backend",
    "list_profile_backends",
    # Operator-config factory
    "get_default_profile_backend",
]


# Process-local registry: backend_id → backend class. Backend classes
# (not instances) because agent-profile backends carry per-scope
# construction args — the framework instantiates
# ``FilesystemAgentProfileBackend(agents_root)`` at module-import time
# for the default; the registry's role is the operator-pin lookup that
# maps ``"filesystem"`` → ``FilesystemAgentProfileBackend``.
_registry: dict[str, type] = {}


def register_profile_backend(backend_id: str, cls: type) -> None:
    """Register an AgentProfileBackend implementation under ``backend_id``.

    Typically called once at module-import time from each backend's
    package (the default ``"filesystem"`` registration happens at the
    bottom of this file).

    Re-registering the same ``backend_id`` replaces the existing
    binding and logs at DEBUG — intentional. Operators occasionally
    want to swap in a wrapper (e.g., a ``CachingAgentProfileBackend``
    that decorates the filesystem backend) without first unregistering
    the original.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered profile backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_profile_backend(backend_id: str) -> None:
    """Remove a backend by ``backend_id``. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a
    backend.
    """
    _registry.pop(backend_id, None)


def get_profile_backend(backend_id: str) -> type:
    """Return the registered AgentProfileBackend class for ``backend_id``.

    Raises ``BackendNotRegistered`` when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., ``cls(agents_root)`` for the filesystem
    backend; ``cls(connection_url)`` for a future
    ``DatabaseAgentProfileBackend``).
    """
    if backend_id not in _registry:
        raise BackendNotRegistered(
            f"No AgentProfileBackend registered under {backend_id!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[backend_id]


def list_profile_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time. Matches the
# Log + Lock registry pattern — the default is always available without
# an extra resolution step.
register_profile_backend("filesystem", FilesystemAgentProfileBackend)


# ────────────────────────────────────────────────────────────────────
# PR 2 wiring contract — PRE-PR-2 state (describes what WILL be wired
# by PR 2; today, in PR 1 scaffolding, none of these call sites exist).
# Sister modules' ``__init__.py`` files use a "post-PR-2 state" label
# when their wiring has already landed; this file flips the label so
# readers don't mistake the block for live behavior.
#
# Wired by #63 PR 2:
#   1. ``AtomicAgent.__init__`` accepts ``profile_backend:
#      AgentProfileBackend | None``; if unset, calls
#      ``get_default_profile_backend(self.agents_root)``. Public
#      ``self.profile_backend`` mirrors ``self.lock_backend`` /
#      ``self.log_backend``.
#   2. ``_load_config()`` becomes a thin shim that calls
#      ``self.profile_backend.load_profile(self.name)`` and unpacks the
#      structured fields onto ``self``.
#   3. ``_load_persona()`` reads ``profile.persona_*`` fields instead
#      of raw file reads.
#   4. ``_load_goal_text()`` reads ``profile.goal_text`` instead of
#      raw file read.
#   5. ``DreamRunner`` accepts ``profile_backend=`` kwarg and routes
#      its model.md read through ``profile_backend.load_profile()``
#      instead of ``_model.parse_model_md()`` directly.
#   6. ``OutcomeRunner``, ``EvalRunner``, ``delegate.py`` thread the
#      kwarg to internal ``AtomicAgent`` instances.
#   7. ``doctor.check_agent_profile_backend`` validates operator config
#      and reports backend stats; URL-credential-redacted error
#      messages.
#
# DEFERRED (intentional):
#   - ``GoalManager`` save-path refactor through profile backend (the
#     structured Goal write path is a separate concern from raw text;
#     PR 2 leaves GoalManager's filesystem coupling intact and routes
#     only the read path).
#   - Snapshot implementation (PR 3 of #63 — Decision 3 in spec/24).


def get_default_profile_backend(scope_root: Path) -> AgentProfileBackend:
    """Return the operator-pinned AgentProfileBackend instance for ``scope_root``.

    Reads ``ATOMIC_AGENTS_PROFILE_BACKEND`` from the environment
    (default ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_PROFILE_BACKEND_URL`` for the connection / path
    string. The env var name is intentionally generic so future Database
    / Git / S3 backends plug in via the same key without operators
    having to relearn the env vocabulary.

    The ``scope_root`` parameter is honored by the filesystem backend
    (agents live as subdirs of that path); future distributed backends
    ignore it in favor of the table-prefix or key-prefix scoping
    inherent to their storage.

    For programmatic operators who want to construct the backend
    themselves (custom database connection, custom git repo path,
    etc.), the ``AtomicAgent(..., profile_backend=...)`` constructor
    kwarg (wired in PR 2) bypasses this factory entirely.

    See spec/24 §"Operator surface" for the full env-var reference +
    the env-var-vs-kwarg trade-off rationale.
    """
    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_PROFILE_BACKEND", "filesystem").strip().lower()
    )

    if raw_backend_id == "filesystem":
        return FilesystemAgentProfileBackend(scope_root)

    # Unknown backend_id — surface a fail-fast error with the FULL
    # known-id list so operators can spot the typo. Credential safety:
    # ``raw_backend_id`` is sanitized before interpolation in case an
    # operator accidentally pastes a URL (e.g., ``postgres://user:pass@host``)
    # into ``ATOMIC_AGENTS_PROFILE_BACKEND`` instead of
    # ``ATOMIC_AGENTS_PROFILE_BACKEND_URL``. Without the sanitize the
    # credential lands in exception text that may be logged by
    # exception handlers, WSGI middleware, or error-tracking services.
    # Same shape applies in ``logs/__init__.py:316`` (already fixed
    # at the log-arc PR 1). ``locks/__init__.py`` does NOT yet apply
    # this redaction at its ``BackendNotRegistered`` raise site — that
    # is a separate, pre-existing gap in the locks module; this file
    # is not the place to fix it, and the gap pre-dates the #63 arc.
    # A follow-up issue should be filed against the locks module to
    # bring it to parity with the log and profile redaction story.
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_PROFILE_BACKEND={safe_backend_id!r} is not a known "
        f"backend. Available: {list_profile_backends()}. Unset the env var "
        f"to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and
    truncates at ``max_len`` to bound the echoed string. Returns the
    bare backend_id if no URL marker is present. The full original
    value is never echoed — this prevents the credential-leak failure
    mode where an operator accidentally sets
    ``ATOMIC_AGENTS_PROFILE_BACKEND=postgres://user:pass@host`` instead
    of ``ATOMIC_AGENTS_PROFILE_BACKEND_URL``.
    """
    # URL-shaped value: keep only the scheme (before "://").
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value

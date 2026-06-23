"""PrincipalBackend — Protocol + registry + canonical types (spec/48).

This package establishes the identity-derivation abstraction.
See docs/spec/48-principal-backend.md for the prose contract.

Public surface:

    from atomic_agents.principal import (
        # Protocol contract
        PrincipalBackend,
        # Canonical types (re-exported from conversation/types — single source)
        Principal, LOCAL_PRINCIPAL, PrincipalCapabilities,
        # Reference impls
        LocalPrincipalBackend, StaticClaimsPrincipalBackend,
        # Exceptions
        PrincipalBackendError, UnverifiedPrincipalConversationAccess,
        # Registry
        register_principal_backend, get_principal_backend,
        list_principal_backends, unregister_principal_backend,
        # Operator-config factory
        get_default_principal_backend,
    )

Env var: ATOMIC_AGENTS_PRINCIPAL_BACKEND (value is a registered backend_id;
defaults to 'local' when the env var is ABSENT). When the env var is PRESENT
and non-empty but names an UNKNOWN backend, get_default_principal_backend()
raises BackendNotRegistered LOUDLY — it does NOT degrade to LocalPrincipalBackend
silently. A misconfigured principal backend in an org deployment must fail fast,
not silently grant every caller LOCAL_PRINCIPAL (is_verified=True).

The registry is a process-local dict keyed by backend_id ('local', 'static_claims').
Like all v1.5 backend registries it stores backend *classes*, not instances.
Principal backends are stateless derivers and may be constructed at process start.

Thread-safety: registration is expected at import time (one-shot from each
backend's module); get_principal_backend is read-only and safe to call from
any thread.
"""

from __future__ import annotations

import logging
import os

from ..exceptions import (
    BackendNotRegistered,
    PrincipalBackendError,
    UnverifiedPrincipalConversationAccess,
)
from .backend import PrincipalBackend
from .local_impl import LocalPrincipalBackend
from .static_claims import StaticClaimsPrincipalBackend
from .types import LOCAL_PRINCIPAL, Principal, PrincipalCapabilities

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "PrincipalBackend",
    # Canonical types
    "Principal",
    "LOCAL_PRINCIPAL",
    "PrincipalCapabilities",
    # Reference impls
    "LocalPrincipalBackend",
    "StaticClaimsPrincipalBackend",
    # Exceptions
    "PrincipalBackendError",
    "UnverifiedPrincipalConversationAccess",
    # Registry
    "register_principal_backend",
    "unregister_principal_backend",
    "get_principal_backend",
    "list_principal_backends",
    # Operator-config factory
    "get_default_principal_backend",
]

# ──────────────────────────────────────────────────────────────────
# Process-local registry

_registry: dict[str, type] = {}


def register_principal_backend(backend_id: str, cls: type) -> None:
    """Register a PrincipalBackend implementation under backend_id.

    Typically called once at module-import time from each backend's module
    (the default 'local' + 'static_claims' registrations happen below).

    Re-registering the same backend_id replaces the existing binding and logs
    at DEBUG — intentional. Operators occasionally want to swap in a wrapper.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered principal backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def unregister_principal_backend(backend_id: str) -> None:
    """Remove a backend by backend_id. No-op when not registered.

    Useful for test isolation and for operators temporarily swapping a backend.
    """
    _registry.pop(backend_id, None)


def get_principal_backend(backend_id: str) -> type:
    """Return the registered PrincipalBackend class for backend_id.

    Raises BackendNotRegistered when the id is not in the registry.
    The caller instantiates the returned class (e.g. cls() for both reference
    impls — they are stateless and require no constructor arguments).
    """
    if backend_id not in _registry:
        known_ids = sorted(_registry.keys())
        raise BackendNotRegistered(
            f"No PrincipalBackend registered under {backend_id!r}. "
            f"Available: {known_ids}"
        )
    return _registry[backend_id]


def list_principal_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in backends at import time.
register_principal_backend("local", LocalPrincipalBackend)
register_principal_backend("static_claims", StaticClaimsPrincipalBackend)


# ──────────────────────────────────────────────────────────────────
# Operator-config factory


def get_default_principal_backend() -> PrincipalBackend:
    """Return the operator-pinned PrincipalBackend instance.

    Resolution order:
        1. ATOMIC_AGENTS_PRINCIPAL_BACKEND env var (if set and non-empty)
        2. Default: LocalPrincipalBackend (home-user zero-config)

    CRITICAL fail-loud behavior:
        When ATOMIC_AGENTS_PRINCIPAL_BACKEND is SET and non-empty but names
        an UNKNOWN backend, this function raises BackendNotRegistered LOUDLY.
        It does NOT degrade to LocalPrincipalBackend silently. A misconfigured
        principal backend in an org deployment must fail fast — silently
        degrading to LocalPrincipalBackend would grant every caller
        LOCAL_PRINCIPAL (is_verified=True) and bypass the HARD-REFUSE gate,
        destroying per-principal conversation isolation.

        Contrast with ConversationBackend (which fails-soft to None when the
        env var is unset — appropriate there because None is the backward-
        compatible single-shot default). For PrincipalBackend, LocalPrincipalBackend
        IS the non-null home-user default; a mis-set env var names a DEPLOYMENT
        POLICY that must be honored or fail loudly.

    Returns:
        A PrincipalBackend instance (no agent_root needed — stateless).

    Raises:
        BackendNotRegistered: when ATOMIC_AGENTS_PRINCIPAL_BACKEND is set and
            non-empty but names an unregistered backend_id.
    """
    raw = os.environ.get("ATOMIC_AGENTS_PRINCIPAL_BACKEND", "").strip().lower()

    if not raw or raw == "local":
        # Env var absent (home-user default) OR explicitly set to 'local'.
        return LocalPrincipalBackend()

    if raw == "static_claims":
        return StaticClaimsPrincipalBackend()

    # Operator-registered custom backend: dispatch through the registry.
    if raw in _registry:
        return _registry[raw]()

    # Unknown backend_id — fail LOUDLY.
    # Redact the raw value before embedding it in the exception message so
    # a DSN-shaped env var value does not leak into error logs or doctor output.
    from atomic_agents.conversation import _redact_for_error_message  # noqa: PLC0415

    safe_raw = _redact_for_error_message(raw)
    known_ids = sorted(_registry.keys())
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_PRINCIPAL_BACKEND={safe_raw!r} is not a known "
        f"backend. Available: {known_ids}. Unset the env var "
        f"to use the local (home-user) default, or set it to a "
        f"registered backend id."
    )

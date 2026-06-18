"""EmbeddingBackend registry (spec/46, PR 3).

Stores backend CLASSES keyed by ``provider_id`` (e.g. ``"openai"``).
``get_default_embedding_backend()`` reads the operator's environment and
constructs an instance, returning ``None`` on graceful degradation (no key
configured → ``supports_semantic_search=False`` → FTS fallback).

Pattern mirrors ``atomic_agents/memory/__init__.py:34`` (``_REGISTRY:
dict[str, type]``), ``atomic_agents/locks/__init__.py``, and
``atomic_agents/logs/__init__.py``.

Functions
---------
``register_embedding_backend(provider_id, cls)``
    Register a backend class.  Call at import time.
``unregister_embedding_backend(provider_id)``
    Remove a backend.  Used for test isolation.
``get_embedding_backend(provider_id) -> type``
    Return the registered class; raises ``BackendNotRegistered`` on miss.
``list_embedding_backends() -> list[str]``
    Return registered provider_ids in lexicographic order.
``get_default_embedding_backend() -> EmbeddingBackend | None``
    Read env vars + construct; return ``None`` on no-key graceful degradation.

Env vars read by ``get_default_embedding_backend()``
----------------------------------------------------
``ATOMIC_AGENTS_EMBEDDING_BACKEND``  -- provider_id (default ``"openai"``)
``ATOMIC_AGENTS_EMBEDDING_MODEL``    -- model identifier (provider-specific)
``ATOMIC_AGENTS_EMBEDDING_DIMENSIONS`` -- integer dimension override
``ATOMIC_AGENTS_EMBEDDING_URL``      -- base URL for non-OpenAI providers
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import EmbeddingBackend

_logger = logging.getLogger(__name__)

# Registry: provider_id → class
_REGISTRY: dict[str, type] = {}


def register_embedding_backend(provider_id: str, cls: type) -> None:
    """Register an EmbeddingBackend class under ``provider_id``.

    Call this at import time in your backend module.  The ``"openai"`` backend
    is pre-registered by this module when the ``[openai]`` extra is installed.

    Args:
        provider_id: Short identifier, e.g. ``"openai"``, ``"local"``.
        cls: Class that satisfies the ``EmbeddingBackend`` Protocol.
    """
    if provider_id in _REGISTRY:
        _logger.debug(
            "replacing registered embedding backend for provider_id=%r", provider_id
        )
    _REGISTRY[provider_id] = cls


def unregister_embedding_backend(provider_id: str) -> None:
    """Remove a backend by provider_id.  No-op when not registered.

    Useful for test isolation — mirrors ``memory.unregister_backend()``.
    """
    _REGISTRY.pop(provider_id, None)


def get_embedding_backend(provider_id: str) -> type:
    """Return the registered backend class for ``provider_id``.

    Raises:
        BackendNotRegistered: when ``provider_id`` is not in the registry.
    """
    from ..exceptions import BackendNotRegistered

    if provider_id not in _REGISTRY:
        known = sorted(_REGISTRY.keys())
        raise BackendNotRegistered(
            f"No EmbeddingBackend registered under {provider_id!r}. Available: {known}"
        )
    return _REGISTRY[provider_id]


def list_embedding_backends() -> list[str]:
    """Return registered provider_ids in lexicographic order.

    Used by diagnostic tooling and registry-introspection tests.
    """
    return sorted(_REGISTRY.keys())


def get_default_embedding_backend() -> "EmbeddingBackend | None":
    """Construct and return the operator-pinned EmbeddingBackend, or ``None``.

    Reads env vars in priority order:

    1. ``ATOMIC_AGENTS_EMBEDDING_BACKEND`` -- provider_id (default ``"openai"``)
    2. ``ATOMIC_AGENTS_EMBEDDING_MODEL``   -- model id for construction
    3. ``ATOMIC_AGENTS_EMBEDDING_DIMENSIONS`` -- integer dimension override
    4. ``ATOMIC_AGENTS_EMBEDDING_URL``     -- base URL for non-OpenAI providers

    Returns ``None`` on graceful-degradation scenarios:

    * No ``ATOMIC_AGENTS_EMBEDDING_BACKEND`` set AND OpenAI extra not installed
      → returns ``None``.
    * Provider id set but ``ATOMIC_AGENTS_EMBEDDING_MODEL`` not set → uses
      provider's default (each backend specifies its own default model).
    * Construction fails (bad ``ATOMIC_AGENTS_EMBEDDING_DIMENSIONS``, SDK
      absent) → logs WARNING by *exception type name only* (MUST 5 redaction)
      and returns ``None``.  The caller (``PgvectorMemoryBackend.__init__``)
      sets ``supports_semantic_search=False`` on ``None`` return, falling
      back to FTS.

    Raises (operator misconfiguration — surfaced, never silently degraded):

    * ``BackendNotRegistered`` when ``ATOMIC_AGENTS_EMBEDDING_BACKEND`` was
      EXPLICITLY set to an unknown/typo'd provider_id (e.g. ``opena1``).  The
      exception carries the full known-provider list.  A silent FTS fallback
      here would leave an operator who opted into semantic search with no
      semantic search and no error — the same split-brain the
      ``SecretBackendNotRegistered`` re-raise below guards against.  (An UNSET
      env var falling back to the implicit ``openai`` default still degrades
      gracefully to ``None`` when the extra is absent.)

    ``SecretBackendNotRegistered`` is NOT swallowed — an operator who pinned
    ``ATOMIC_AGENTS_SECRET_BACKEND=gcp`` but hasn't configured GCP credentials
    gets a loud construction error, not a silent FTS fallback.  This matches
    ``_llm._get_key``'s own posture and ``OpenAIEmbeddingBackend.__init__``'s
    re-raise behaviour (api_key=None → _get_key() → SecretBackendNotRegistered
    re-raised).

    Factory design notes
    --------------------
    * The ``api_key`` kwarg is intentionally omitted (i.e. ``None``) when
      constructing the backend so the backend's ``__init__`` calls ``_get_key()``
      which routes through whatever ``SecretBackend`` is registered.  Passing
      ``api_key=os.environ.get('OPENAI_API_KEY')`` directly would bypass the
      SecretBackend — a split-brain credential path that silently breaks cloud
      deployments (MEMORY.md: "cross-model catches same-family blind spots").
    * Empty env-var strings are coerced to ``None`` (``or None``) so they never
      reach the constructor as falsy non-None values.
    """
    from ..exceptions import AtomicAgentsError, BackendNotRegistered
    from ..secret_backend import SecretBackendNotRegistered  # re-raised, not swallowed

    # Distinguish "no provider pinned" (implicit default → graceful None on miss)
    # from "operator explicitly pinned a provider" (typo → fail loud).  An
    # operator who set ATOMIC_AGENTS_EMBEDDING_BACKEND=opena1 and silently lost
    # semantic search with no error is the split-brain failure this guards
    # against — it must surface, exactly like the SecretBackendNotRegistered
    # re-raise below.
    _raw_pin = os.environ.get("ATOMIC_AGENTS_EMBEDDING_BACKEND")
    _explicitly_pinned = bool(_raw_pin and _raw_pin.strip())
    provider_id = (_raw_pin or "openai").strip().lower() or "openai"

    # Lazy-register the OpenAI backend if not yet registered (the [openai]
    # extra may not be installed; lazy-import keeps the base package import
    # side-effect-free even when this factory is called).
    if provider_id == "openai" and "openai" not in _REGISTRY:
        try:
            from .openai import OpenAIEmbeddingBackend  # noqa: PLC0415

            register_embedding_backend("openai", OpenAIEmbeddingBackend)
        except (ImportError, AtomicAgentsError):
            # [openai] extra not installed — graceful degradation.
            _logger.debug(
                "get_default_embedding_backend: 'openai' extra not installed; "
                "returning None (FTS fallback)"
            )
            return None

    try:
        cls = get_embedding_backend(provider_id)
    except BackendNotRegistered:
        if _explicitly_pinned:
            # Operator pinned an unknown/typo'd provider_id → surface loudly with
            # the full known-provider list (matches the SecretBackendNotRegistered
            # re-raise posture below; an explicit misconfig must not silently
            # degrade to FTS).  BackendNotRegistered already carries the list.
            raise
        # Implicit default 'openai' missing from the registry should not reach
        # here (lazy-registered above), but if it does, degrade gracefully.
        _logger.warning(
            "get_default_embedding_backend: default provider_id=%r not "
            "registered; returning None (FTS fallback)",
            provider_id,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "get_default_embedding_backend: registry lookup failed for "
            "provider_id=%r: %s",
            provider_id,
            type(exc).__name__,
        )
        return None

    # Read optional construction overrides from env — coerce empty strings to
    # None so the backend falls back to its own defaults.
    model_id: str | None = os.environ.get("ATOMIC_AGENTS_EMBEDDING_MODEL") or None
    dimensions_raw: str | None = (
        os.environ.get("ATOMIC_AGENTS_EMBEDDING_DIMENSIONS") or None
    )
    dimensions: int | None = None
    if dimensions_raw is not None:
        try:
            dimensions = int(dimensions_raw)
        except (ValueError, TypeError):
            _logger.warning(
                "get_default_embedding_backend: invalid ATOMIC_AGENTS_EMBEDDING_DIMENSIONS=%r "
                "(must be a positive integer); ignoring",
                dimensions_raw,
            )
            dimensions = None

    # Construct the backend.  Pass api_key=None so the backend calls _get_key()
    # which routes through the registered SecretBackend (not a private env
    # cascade).  SecretBackendNotRegistered is propagated (not swallowed) so
    # a misconfigured GCP backend surfaces loudly.
    try:
        kwargs: dict = {}
        if model_id is not None:
            kwargs["model_id"] = model_id
        if dimensions is not None:
            kwargs["dimensions"] = dimensions
        # api_key deliberately omitted (None) — delegate to _get_key() via SecretBackend.
        backend = cls(**kwargs)
        return backend  # type: ignore[return-value]
    except SecretBackendNotRegistered:
        # Operator-pinned backend misconfig → surface, don't silently degrade.
        raise
    except AtomicAgentsError as exc:
        # EmbeddingError (MUST-1 validation), SDK absent, etc.
        _logger.warning(
            "get_default_embedding_backend: construction failed for "
            "provider_id=%r: %s (FTS fallback)",
            provider_id,
            type(exc).__name__,  # MUST 5 redaction — type name only, never str(exc)
        )
        return None
    except Exception as exc:  # noqa: BLE001
        # Unexpected error (bad kwarg name, etc.) — log type only.
        _logger.warning(
            "get_default_embedding_backend: unexpected error constructing "
            "provider_id=%r: %s (FTS fallback)",
            provider_id,
            type(exc).__name__,
        )
        return None


# ─── Pre-register built-in backends ─────────────────────────────────────────
# The openai backend is NOT eagerly registered here because the [openai] extra
# may not be installed.  Lazy registration happens inside
# get_default_embedding_backend() when provider_id="openai".  This mirrors the
# PostgresMemoryBackend/PostgresLogBackend lazy-import pattern.

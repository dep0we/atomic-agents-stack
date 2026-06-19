"""EmbeddingBackend registry (spec/46, PR 3).

Stores backend CLASSES keyed by ``provider_id`` (e.g. ``"openai"``).
``get_default_embedding_backend()`` reads the operator's environment and
constructs an instance, returning ``None`` when no provider is pinned (the
opt-in default → ``supports_semantic_search=False`` → FTS fallback).

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
    Read env vars + construct; return ``None`` when no provider is pinned
    (the opt-in default — semantic search stays off until explicitly enabled).

Env vars read by ``get_default_embedding_backend()``
----------------------------------------------------
``ATOMIC_AGENTS_EMBEDDING_BACKEND``  -- provider_id (REQUIRED to enable semantic
                                        search; no implicit default)
``ATOMIC_AGENTS_EMBEDDING_MODEL``    -- model identifier (provider-specific)
``ATOMIC_AGENTS_EMBEDDING_DIMENSIONS`` -- integer dimension override

(``ATOMIC_AGENTS_EMBEDDING_URL`` is reserved but NOT yet read -- no shipped
backend accepts a base-URL kwarg.  It is forwarded once a URL-taking non-OpenAI
backend ships; setting it today has no effect.)
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

    **Opt-in by default (#200 PR3 cost-safety ruling).** Semantic search is OFF
    unless the operator EXPLICITLY pins a provider.  With no
    ``ATOMIC_AGENTS_EMBEDDING_BACKEND`` set (or set blank) this returns ``None``
    immediately — the caller (``PgvectorMemoryBackend.__init__`` /
    ``PgvectorCorpusBackend.__init__``) sets ``supports_semantic_search=False``
    and serves FTS only.  This is deliberate: it prevents surprise billable
    embedding spend merely because an ``OPENAI_API_KEY`` happens to be reachable
    in the environment.  To turn semantic search ON, set
    ``ATOMIC_AGENTS_EMBEDDING_BACKEND`` (or inject an ``EmbeddingBackend`` via the
    backend's constructor kwarg).

    When a provider IS pinned, reads the construction overrides:

    1. ``ATOMIC_AGENTS_EMBEDDING_BACKEND`` -- provider_id (REQUIRED to opt in;
       no implicit default)
    2. ``ATOMIC_AGENTS_EMBEDDING_MODEL``   -- model id (omitted → provider default)
    3. ``ATOMIC_AGENTS_EMBEDDING_DIMENSIONS`` -- integer dimension override

    (``ATOMIC_AGENTS_EMBEDDING_URL`` is reserved but NOT yet read by this
    factory -- ``OpenAIEmbeddingBackend.__init__`` has no base-URL kwarg, so
    no current backend consumes it.  It is forwarded once a URL-taking
    non-OpenAI backend ships.)

    Returns ``None`` ONLY when no provider is pinned (the opt-out / FTS default).

    Raises (an EXPLICIT opt-in must fail loud, never silently degrade to FTS —
    an operator who turned semantic search ON must not lose it without an error):

    * ``BackendNotRegistered`` when the pinned provider_id is unknown/typo'd
      (e.g. ``opena1``).  The exception carries the full known-provider list.
    * ``ImportError`` when the pinned provider is ``openai`` but the ``[openai]``
      extra is not installed.
    * ``SecretBackendNotRegistered`` / ``EmbeddingError`` and any other
      construction error (bad ``ATOMIC_AGENTS_EMBEDDING_DIMENSIONS``, SDK absent,
      missing credentials) propagate from ``cls(**kwargs)``.  This matches
      ``_llm._get_key``'s posture and ``OpenAIEmbeddingBackend.__init__``'s
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
    from ..exceptions import AtomicAgentsError, EmbeddingError

    # OPT-IN DEFAULT (#200 PR3 cost-safety ruling, 2026-06-18): semantic search
    # is OFF unless the operator EXPLICITLY pins a provider via
    # ATOMIC_AGENTS_EMBEDDING_BACKEND.  An unset/blank env var returns None → the
    # pgvector backend sets supports_semantic_search=False and serves FTS only.
    # This prevents surprise billable embedding spend merely because an
    # OPENAI_API_KEY happens to be reachable in the environment.  Every path
    # BELOW this guard is therefore an EXPLICIT opt-in and fails loud on any
    # misconfig (typo, missing extra, bad dimensions): an operator who turned
    # semantic search ON must never silently lose it to an FTS fallback.
    _raw_pin = os.environ.get("ATOMIC_AGENTS_EMBEDDING_BACKEND")
    if not (_raw_pin and _raw_pin.strip()):
        _logger.debug(
            "get_default_embedding_backend: ATOMIC_AGENTS_EMBEDDING_BACKEND "
            "unset; semantic search disabled (FTS only). Set it to opt in."
        )
        return None
    provider_id = _raw_pin.strip().lower()

    # Lazy-register the OpenAI backend if not yet registered (the [openai]
    # extra may not be installed; lazy-import keeps the base package import
    # side-effect-free even when this factory is called).
    if provider_id == "openai" and "openai" not in _REGISTRY:
        try:
            from .openai import OpenAIEmbeddingBackend  # noqa: PLC0415

            register_embedding_backend("openai", OpenAIEmbeddingBackend)
        except (ImportError, AtomicAgentsError) as exc:
            # Explicit pin → fail loud if the [openai] extra is not installed; an
            # operator who opted into semantic search must not silently lose it.
            raise ImportError(
                "ATOMIC_AGENTS_EMBEDDING_BACKEND=openai requires the [openai] "
                "extra; install via: pip install 'atomic-agents-stack[openai]'. "
                "An explicitly-pinned embedding provider that cannot be "
                "constructed fails loudly rather than silently falling back to "
                "FTS search."
            ) from exc

    # Explicit pin → an unknown/typo'd provider_id surfaces loudly with the full
    # known-provider list (BackendNotRegistered already carries it); an explicit
    # misconfig must not silently degrade to FTS.
    cls = get_embedding_backend(provider_id)

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
        except (ValueError, TypeError) as exc:
            # We are PAST the opt-in guard, so the provider was EXPLICITLY pinned.
            # A malformed dimension must fail LOUD, not silently fall back to the
            # provider default — silently constructing a different vector width
            # than the operator asked for would change the stored/queried shape
            # (and bill embeds against it) without their knowledge. Consistent
            # with the explicit-pin fail-loud posture of the typo/missing-extra
            # branches above.
            raise EmbeddingError(
                f"ATOMIC_AGENTS_EMBEDDING_DIMENSIONS={dimensions_raw!r} is not a "
                "valid integer. An explicitly-pinned embedding backend must not "
                "silently fall back to a different vector dimension — fix the "
                "value or unset it to use the provider default."
            ) from exc

    # Construct the backend.  Pass api_key=None so the backend calls _get_key()
    # which routes through the registered SecretBackend (not a private env
    # cascade).  Explicit pin → any construction error (SecretBackendNotRegistered,
    # EmbeddingError/MUST-1 validation, SDK absent, invalid dimensions) propagates
    # loudly; an opt-in must not silently degrade to FTS.
    kwargs: dict = {}
    if model_id is not None:
        kwargs["model_id"] = model_id
    if dimensions is not None:
        kwargs["dimensions"] = dimensions
    # api_key deliberately omitted (None) — delegate to _get_key() via SecretBackend.
    backend = cls(**kwargs)
    return backend  # type: ignore[return-value]


# ─── Pre-register built-in backends ─────────────────────────────────────────
# The openai backend is NOT eagerly registered here because the [openai] extra
# may not be installed.  Lazy registration happens inside
# get_default_embedding_backend() when provider_id="openai".  This mirrors the
# PostgresMemoryBackend/PostgresLogBackend lazy-import pattern.

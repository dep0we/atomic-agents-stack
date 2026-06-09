"""LLM provider abstraction layer — Protocol + registry + canonical types.

This package establishes the LLM provider abstraction in the protocol-pattern
series alongside MemoryBackend (#57) and the open protocols #60-#65. See
``docs/spec/31-llm-backend.md`` (lands with the spec PR — final stage of
the #87 arc) for the prose contract.

Public surface (scaffolding PR — no behavior change today):

    from atomic_agents.llm import (
        # Protocol contract
        SyncLLMBackend,
        # Canonical types
        LLMToolDefinition, LLMToolUse, LLMToolResult,
        CacheDirective, LLMCapabilities, PricingInfo,
        _RawLLMResponse,
        # Registry
        register_llm_backend, unregister_llm_backend,
        get_backend, iter_registered_backends,
        find_backend_for_model,
    )

The registry is a process-local dict keyed by ``provider_id``. The four
reference backends register lazily on the first ``find_backend_for_model``
call (via ``_ensure_default_backends``), NOT at module import — eager SDK
imports added ~300ms to every subprocess spawn (see spec/31). Threading
note: ``find_backend_for_model`` is read-only after that one-shot lazy
registration and safe to call from any thread. No lock is needed under that
usage; if a future operator mutates the registry at runtime from multiple
threads, that's their footgun to sandbox.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from ..exceptions import AmbiguousBackendError, UnknownModelError
from .backend import SyncLLMBackend, _RawLLMResponse

_logger = logging.getLogger(__name__)
from .types import (
    CacheDirective,
    LLMCapabilities,
    LLMToolDefinition,
    LLMToolResult,
    LLMToolUse,
    PricingInfo,
)

__all__ = [
    # Protocol
    "SyncLLMBackend",
    # Canonical types
    "LLMToolDefinition",
    "LLMToolUse",
    "LLMToolResult",
    "CacheDirective",
    "LLMCapabilities",
    "PricingInfo",
    "_RawLLMResponse",
    # Registry
    "register_llm_backend",
    "unregister_llm_backend",
    "get_backend",
    "iter_registered_backends",
    "find_backend_for_model",
]


# Process-local registry. Keyed by ``provider_id`` to enforce the
# uniqueness invariant — two backends with the same id make no sense.
# A model id may legitimately be claimed by multiple ``provider_id``s
# (e.g., 'openai' + 'azure-openai' both claim 'gpt-5'); that's the
# AmbiguousBackendError case resolved by ``model.md provider:``.
_registry: dict[str, SyncLLMBackend] = {}


def register_llm_backend(backend: SyncLLMBackend) -> None:
    """Register an LLM backend implementation under its ``provider_id``.

    Typically called once at module-import time from each backend's
    package (see ``atomic_agents/llm/anthropic.py`` in PR 2).

    Re-registering the same ``provider_id`` replaces the existing
    backend silently — intentional. Operators occasionally want to
    swap in a wrapper (e.g., ``RetryingLLMBackend`` per issue #81)
    without first unregistering the original. The replace semantics
    let them do that with a single call.

    Raises TypeError when ``backend`` doesn't conform to the
    ``SyncLLMBackend`` Protocol via ``isinstance`` runtime check.
    The check is method-presence only (not signature), per Python's
    runtime_checkable Protocol behavior.
    """
    if not isinstance(backend, SyncLLMBackend):
        raise TypeError(
            f"backend {backend!r} does not satisfy SyncLLMBackend "
            f"Protocol (missing required methods)"
        )
    if backend.provider_id in _registry:
        # Distinguish intentional wrapping (e.g., RetryingLLMBackend per
        # issue #81) from accidental collision. Debug-only — operators
        # opting into verbose logging see the replacement; default
        # behavior unchanged.
        _logger.debug(
            "replacing registered backend for provider_id=%r",
            backend.provider_id,
        )
    _registry[backend.provider_id] = backend


def unregister_llm_backend(provider_id: str) -> None:
    """Remove a backend by ``provider_id``. No-op when not registered.

    Useful for test isolation (``@pytest.fixture(autouse=True)``
    cleanup) and for operators temporarily swapping a backend.
    """
    _registry.pop(provider_id, None)


def get_backend(provider_id: str) -> SyncLLMBackend | None:
    """Return the registered backend for ``provider_id``, or None.

    Caller code should use ``find_backend_for_model`` for the normal
    "I have a model id, what handles it" question. ``get_backend`` is
    the explicit-provider lookup for when ``model.md`` has been
    resolved to a specific ``provider_id``.
    """
    return _registry.get(provider_id)


def iter_registered_backends() -> Iterator[SyncLLMBackend]:
    """Iterate every registered backend (registration order undefined).

    Used by ``find_backend_for_model`` and by diagnostic tooling
    (``atomic-agents doctor`` will check that at least one backend
    is registered, once the doctor check lands in PR 4).
    """
    return iter(_registry.values())


def find_backend_for_model(
    model: str,
    *,
    preferred_provider: str | None = None,
) -> SyncLLMBackend:
    """Return the registered backend that handles ``model``.

    Resolution rules (codex P2 from the plan):

    1. If ``preferred_provider`` is given (typically from
       ``model.md``'s ``provider:`` field), return that backend exclusively
       — even if other backends also claim the model. Raise
       ``UnknownModelError`` when no backend with that provider_id is
       registered, or ``AmbiguousBackendError`` when the named backend
       does not actually support the model.
    2. Otherwise, collect every backend whose ``supports_model(model)``
       returns True.
    3. Zero matches → ``UnknownModelError``.
    4. Exactly one match → return it.
    5. More than one match → ``AmbiguousBackendError`` listing all
       candidate ``provider_id`` values, hinting at the ``model.md``
       fix.
    """
    _ensure_default_backends()

    # Treat empty / whitespace as "no preference" — a model.md parser
    # encountering a bare `provider:` line shouldn't make resolution
    # fail with the misleading "no backend registered with provider_id
    # ''" diagnostic.
    if preferred_provider is not None and preferred_provider.strip():
        backend = _registry.get(preferred_provider)
        if backend is None:
            raise UnknownModelError(
                f"no backend registered with provider_id "
                f"{preferred_provider!r} (registered: "
                f"{sorted(_registry.keys())})"
            )
        if not backend.supports_model(model):
            raise AmbiguousBackendError(model, [preferred_provider])
        return backend

    matches = [b for b in _registry.values() if b.supports_model(model)]
    if not matches:
        raise UnknownModelError(
            f"no registered LLM backend supports model {model!r}. "
            f"Registered backends: {sorted(_registry.keys())}"
        )
    if len(matches) > 1:
        raise AmbiguousBackendError(model, sorted(b.provider_id for b in matches))
    return matches[0]


_DEFAULTS_REGISTERED = False


def _ensure_default_backends() -> None:
    """Lazily register the four reference backends on first registry use.

    Each backend's instantiation may raise ``AtomicAgentsError`` when its
    optional SDK isn't installed (e.g., anthropic, openai, google-genai). The
    framework treats missing SDKs as "this backend isn't available in this
    deployment" — log at DEBUG (not WARNING, so a home user who never opted
    into a provider sees no noise on first call) and continue. The cost gates
    and doctor surface the actual missing-key / missing-SDK condition when an
    operator tries to use the affected provider.

    Called by ``find_backend_for_model`` on every lookup; idempotent via
    the ``_DEFAULTS_REGISTERED`` guard. Lazy registration keeps
    ``import atomic_agents`` fast — pulling in provider SDKs at module-import
    time slows every subprocess spawn (e.g., ``multiprocessing.Process``
    in tests) by ~300ms and broke a timing-sensitive lock acquisition
    test on the introducing PR.

    Registered backends (all four reference implementations):
    - AnthropicLLMBackend (claude-* models)
    - OpenAICompatibleLLMBackend / make_openai_backend (gpt-* models)
    - make_moonshot_backend (moonshot/* models, reuses OpenAI-compat)
    - VertexGeminiLLMBackend (vertex/gemini-* models, optional [vertex] extra)
    """
    from ..exceptions import AtomicAgentsError as _AAE

    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    _DEFAULTS_REGISTERED = True
    # Each backend's instantiation may raise ``ImportError`` (missing
    # optional SDK) or ``AtomicAgentsError`` (framework-specific init
    # failure — every reference backend wraps a missing optional SDK in
    # ``AtomicAgentsError`` at construction). Those are documented expected
    # misses for any deployment that didn't opt into that provider's extra —
    # log at DEBUG (not WARNING) and continue with the remaining backends.
    # A home user with one Claude agent who never asked for OpenAI / Moonshot
    # / Vertex must not see a WARNING line on first ``agent.call()`` for an
    # SDK they never installed — "defaults are right / graceful" per the
    # aesthetic section. The doctor (``check_provider_keys``) and the cost
    # gates surface the missing-SDK / missing-key condition loudly at the
    # point an operator actually selects that provider's model. Any OTHER
    # exception propagates so real code bugs surface as tracebacks at first
    # lookup, not as a silent ``UnknownModelError``.
    try:
        from .anthropic import AnthropicLLMBackend

        register_llm_backend(AnthropicLLMBackend())
        _logger.debug("registered AnthropicLLMBackend")
    except (ImportError, _AAE) as e:
        _logger.debug("AnthropicLLMBackend not registered: %s", e)
    try:
        from .openai_compat import make_openai_backend

        register_llm_backend(make_openai_backend())
        _logger.debug("registered OpenAILLMBackend")
    except (ImportError, _AAE) as e:
        _logger.debug("OpenAILLMBackend not registered: %s", e)
    try:
        from .moonshot import make_moonshot_backend

        register_llm_backend(make_moonshot_backend())
        _logger.debug("registered MoonshotLLMBackend")
    except (ImportError, _AAE) as e:
        _logger.debug("MoonshotLLMBackend not registered: %s", e)
    try:
        from .vertex_gemini import VertexGeminiLLMBackend

        register_llm_backend(VertexGeminiLLMBackend())
        _logger.debug("registered VertexGeminiLLMBackend")
    except (ImportError, _AAE) as e:
        _logger.debug("VertexGeminiLLMBackend not registered: %s", e)

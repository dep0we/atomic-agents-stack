"""EmbeddingBackend Protocol and OpenAI reference implementation (spec/46).

This package is the nineteenth open Protocol in the protocol-pattern series.
It abstracts the embedding provider so both ``PgvectorMemoryBackend`` (#200 PR3)
and ``PgvectorCorpusBackend`` (#200 PR3) can share a single injected backend
without duplicating provider logic.

Public surface:

    from atomic_agents.embedding import (
        # Protocol contract
        EmbeddingBackend,
        # Capability dataclass
        EmbeddingCapabilities,
        # Reference implementation
        OpenAIEmbeddingBackend,
        # Cost helpers
        EMBEDDING_PRICING,
        calc_embedding_cost,
        # Registry (PR 3)
        register_embedding_backend,
        unregister_embedding_backend,
        get_embedding_backend,
        list_embedding_backends,
        get_default_embedding_backend,
    )

Exceptions (``EmbeddingError``, ``EmbeddingProviderUnavailable``) live in
``atomic_agents.exceptions`` -- not re-exported here -- following the project
convention for every backend exception family.
"""

from __future__ import annotations

from .backend import EmbeddingBackend, EmbeddingCapabilities
from .openai import OpenAIEmbeddingBackend
from .registry import (
    get_default_embedding_backend,
    get_embedding_backend,
    list_embedding_backends,
    register_embedding_backend,
    unregister_embedding_backend,
)
from .._costs import EMBEDDING_PRICING, calc_embedding_cost

__all__ = [
    # Protocol
    "EmbeddingBackend",
    # Capability dataclass
    "EmbeddingCapabilities",
    # Reference implementation
    "OpenAIEmbeddingBackend",
    # Cost helpers
    "EMBEDDING_PRICING",
    "calc_embedding_cost",
    # Registry (PR 3)
    "register_embedding_backend",
    "unregister_embedding_backend",
    "get_embedding_backend",
    "list_embedding_backends",
    "get_default_embedding_backend",
]

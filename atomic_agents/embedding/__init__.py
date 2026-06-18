"""EmbeddingBackend Protocol and OpenAI reference implementation (spec/46, DRAFT).

This package is the nineteenth open Protocol in the protocol-pattern series.
It abstracts the embedding provider so both ``PgvectorMemoryBackend`` (#258 PR2)
and ``PgvectorCorpusBackend`` (#258 PR3) can share a single injected backend
without duplicating provider logic.

Public surface (PR2):

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
    )

Exceptions (``EmbeddingError``, ``EmbeddingProviderUnavailable``) live in
``atomic_agents.exceptions`` -- not re-exported here -- following the project
convention for every backend exception family.

Registry functions (``register_embedding_backend``, ``get_embedding_backend``,
etc.) ship in PR3 alongside the pgvector wiring.
"""

from __future__ import annotations

from .backend import EmbeddingBackend, EmbeddingCapabilities
from .openai import OpenAIEmbeddingBackend
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
]

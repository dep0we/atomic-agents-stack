"""EmbeddingBackend Protocol -- the contract every embedding backend satisfies.

This is the nineteenth open Protocol in the protocol-pattern series alongside
MemoryBackend (#57), LLMBackend (#87), JudgeBackend (#112), LockBackend (#60),
LogBackend (#61), AgentProfileBackend (#63), ToolRegistryBackend (#64),
MandateBackend (#124), PolicyBackend (#89), PersonaBackend (#62),
CorpusBackend (#65), MCPServerRegistryBackend (#201), SecretBackend (#340),
GoalBackend (#425), OutcomeBackend (#426), JournalBackend (#427),
QueueBackend (#428), and IdempotencyBackend (#520).

``EmbeddingBackend`` abstracts the embedding provider behind a Protocol so
both ``PgvectorMemoryBackend`` and ``PgvectorCorpusBackend`` (#200 PR 3) can
share a single, injected embedding backend without duplicating
provider logic. The standalone use case -- constructing any vector-capable
backend -- is the primary justification identified when ``EmbeddingBackend``
was reconsidered at issue #200 after spec/34 scope analysis.

See ``docs/spec/46-embedding-backend.md`` (LOCKED at #544 PR2) for
the normative contract.

Mocking note: ``MagicMock(spec=EmbeddingBackend)`` DOES pass
``isinstance(m, EmbeddingBackend)`` -- ``@runtime_checkable`` performs a
member-presence check only, and a spec'd MagicMock supplies every member.
It is NOT a behavioral proxy, though: ``m.embed()`` returns a ``MagicMock``,
not ``list[float] | None``. Tests must use a concrete fake class (see
``StubEmbeddingBackend`` in ``tests/stub_embedding.py``) for any assertion
about returned values. See spec/46 §"Mocking note".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# ──────────────────────────────────────────────────────────────────────────────
# Capability dataclass


@dataclass(frozen=True)
class EmbeddingCapabilities:
    """Capability advertisement for an EmbeddingBackend instance.

    Conformance tests assert claim-vs-behavior parity. Backends that misreport
    capabilities produce silent failures rather than loud refusals -- the same
    pattern as CorpusBackend (spec/34) and PersonaBackend (spec/33).

    Fields:

    ``max_batch_size``: maximum number of texts that ``embed_batch()`` can
        process in a single provider call. Implementations MUST NOT raise
        on a batch smaller than this limit; they MAY split batches that
        exceed it into multiple provider calls internally.

    ``max_input_tokens``: maximum tokens the provider accepts per text
        input. A text exceeding this limit causes the provider to reject the
        call (a 4xx); ``embed()`` converts that rejection to ``None`` rather
        than raising, per the MUST-NOT-RAISE invariant. (The backend does not
        pre-flight token length client-side; a dedicated over-limit conformance
        test is not yet in the suite -- see spec/46 MUST 4 failure table.)

    ``supports_input_type``: True when the underlying provider supports
        distinguishing query-embedding from document-embedding mode (OpenAI
        ``input_type`` parameter, Cohere ``input_type``, etc.).

        PR 3 added ``input_type`` as an accepted kwarg on ``embed()`` and
        ``embed_batch()``.  Backends whose provider supports the parameter
        SHOULD forward it and advertise ``True``; backends whose provider does
        NOT expose the parameter MUST accept the kwarg without raising and keep
        this flag ``False`` (capability honesty -- the kwarg is accepted but
        not honoured).

        **OpenAI note (Principle #12 verification, 2026-06-18):** the installed
        OpenAI SDK (verified against openai 2.35.1) ``embeddings.create()``
        signature does NOT include ``input_type`` as a native parameter (against
        ``.venv/lib/python3.12/site-packages/openai/resources/embeddings.py``).
        ``OpenAIEmbeddingBackend`` therefore advertises ``supports_input_type=False``
        and accepts the kwarg but does not forward it to the API.
        See spec/46 §"supports_input_type flag".
    """

    max_batch_size: int
    max_input_tokens: int
    supports_input_type: bool


# ──────────────────────────────────────────────────────────────────────────────
# Protocol definition


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Contract every embedding backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol -- it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, EmbeddingBackend)`` to perform a method-presence check
    (not a signature check -- signatures are static-typing's job).

    Mocking note: ``MagicMock(spec=EmbeddingBackend)`` DOES pass
    ``isinstance(m, EmbeddingBackend)`` -- ``@runtime_checkable`` checks member
    presence only, which a spec'd MagicMock satisfies. It is NOT a behavioral
    proxy: ``m.embed()`` returns a ``MagicMock``, not ``list[float] | None``.
    Tests must use a concrete fake class (see ``tests/stub_embedding.py``
    ``StubEmbeddingBackend``) for any assertion about returned values.

    CONFORMANCE INVARIANT -- embed_batch length:
    ``len(embed_batch(texts))`` MUST equal ``len(texts)`` for all inputs,
    regardless of per-item success or failure. A failed item MUST produce
    ``None`` at its index position, not a shorter output list. Implementations
    that truncate the output list rather than inserting ``None`` violate this
    MUST and will fail the parametrized conformance suite.

    MUST-NOT-RAISE invariant:
    ``embed()`` and ``embed_batch()`` MUST NOT raise under any circumstances.
    Network errors, rate limits, malformed input, and provider unavailability
    all cause ``None`` in the output -- they never propagate as exceptions.
    The None-fallback is the crash-recovery posture that lets callers zip
    results with input texts safely.
    """

    # ─── Identifying properties ───────────────────────────────────────────

    @property
    def model_id(self) -> str:
        """Stable model identifier, e.g. ``"text-embedding-3-small"``.

        Used by the cost gate (``calc_embedding_cost(backend.model_id, ...)``)
        and by observability records (embedding audit JSONL). Must be stable
        across the lifetime of the backend instance.
        """
        ...

    @property
    def dimensions(self) -> int:
        """Embedding vector dimensionality this backend produces.

        MUST equal ``len(embed(text))`` for any successfully-embedded text
        (capability honesty). Advertise the CONFIGURED dimension a caller will
        actually receive, never a model maximum -- a backend that reduces vectors
        advertises the reduced size here. (Provider-specific native sizes are an
        implementation detail of each backend, not part of this contract.)
        """
        ...

    @property
    def provider_id(self) -> str:
        """Stable provider identifier, e.g. ``"openai"``, ``"local"``.

        Distinct from ``model_id`` -- the same provider (``"openai"``) may
        serve multiple models. Used for registry lookup
        (``atomic_agents/embedding/registry.py``). The existing
        ``embedding_provider`` display label on ``CorpusCapabilities``
        (spec/34) carries a string family identifier; the typed
        ``embedding_backend_resolved`` sibling field carries this instance, and
        its ``provider_id`` MUST stay consistent with that string label.
        """
        ...

    # ─── Capability advertisement ─────────────────────────────────────────

    def capabilities(self) -> EmbeddingCapabilities:
        """Advertise what this backend instance supports.

        Returns a frozen ``EmbeddingCapabilities`` dataclass. The values are a
        contract, not a hint -- conformance tests assert claim-vs-behavior
        parity. This method MUST return the same value across calls for the
        lifetime of the backend instance (side-effect-free).
        """
        ...

    # ─── Embedding operations ─────────────────────────────────────────────

    def embed(self, text: str, *, input_type: str | None = None) -> list[float] | None:
        """Embed a single text string and return the vector, or ``None``.

        Returns ``None`` on ANY failure -- network error, rate limit,
        malformed input, provider unavailability, token-length exceeded.
        MUST NOT raise under any circumstances.

        ``input_type``: optional hint distinguishing query embeddings from
        document embeddings (e.g., Cohere's ``"search_query"`` /
        ``"search_document"``).  Backends whose provider supports this
        parameter SHOULD forward it (``supports_input_type=True``); backends
        whose provider does NOT support it MUST accept the kwarg without
        raising and ignore it (``supports_input_type=False`` remains honest
        because the *Protocol surface* now carries the parameter but the
        *implementation* cannot honour it).  See spec/46
        §"supports_input_type flag".

        This is a ``Protocol`` -- it provides no method body, so there is no
        inherited ``embed_batch()`` default. Every implementation MUST define
        ``embed_batch()`` itself; the recommended reference pattern is to loop
        this method item-by-item (see ``StubEmbeddingBackend``), or to issue a
        single batched provider call that degrades to per-item on failure
        (see ``OpenAIEmbeddingBackend``).
        """
        ...

    def embed_batch(
        self, texts: list[str], *, input_type: str | None = None
    ) -> list[list[float] | None]:
        """Embed a list of texts and return a list of vectors.

        Returns a list of the same length as ``texts``. Each element is either
        a vector ``list[float]`` (success) or ``None`` (failure for that item).

        ``input_type``: same semantics as ``embed()`` -- forwarded to the
        provider when ``supports_input_type=True``; accepted-but-ignored when
        ``False``.

        CONFORMANCE INVARIANT: ``len(result) == len(texts)`` always holds,
        including when ``texts`` is empty (returns ``[]``) and when every
        item fails (returns ``[None] * len(texts)``).

        ``Protocol`` methods have no body, so there is NO inherited default.
        Every implementation MUST provide ``embed_batch()``. The recommended
        reference pattern loops ``embed()`` item-by-item with each item
        independently returning ``None`` on failure; concrete backends may
        instead issue a batched provider call for efficiency, but MUST
        preserve the length invariant and the MUST-NOT-RAISE contract.
        """
        ...

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release any resources held by this backend instance.

        MUST be idempotent -- calling ``close()`` twice MUST NOT raise.
        Backends that hold a connection pool, HTTP session, or file handle
        MUST release it here. Stateless backends implement this as a no-op.
        """
        ...

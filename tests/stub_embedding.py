"""TEST-ONLY stub EmbeddingBackend implementation.

NEVER import from production code. Not registered in any embedding registry.
This module exists solely as a concrete fake class for conformance tests --
using MagicMock(spec=EmbeddingBackend) is insufficient because @runtime_checkable
checks attribute presence only, not behavioral correctness.

Grep check (IMPORTS only -- prose mentions in docstrings are expected and
fine): ``grep -rnE "from tests.stub_embedding|import.*StubEmbeddingBackend" atomic_agents/``
should return zero results. If it returns an actual import, remove it immediately.
"""

from __future__ import annotations

from atomic_agents.embedding.backend import EmbeddingCapabilities


class StubEmbeddingBackend:
    """Concrete test double satisfying the full EmbeddingBackend Protocol surface.

    embed() returns a fixed-length vector of zeros. embed_batch() loops embed()
    item-by-item (the default Protocol behavior). close() records call count
    for idempotency testing.

    All methods honor the MUST-NOT-RAISE invariant -- this stub never raises.

    MUST 1 (input validation) exemption: this stub does NOT validate its
    constructor args. It is a test double whose defaults are always valid;
    MUST-1 rejection behavior is conformance-tested against the real reference
    impl (``OpenAIEmbeddingBackend``) in
    ``test_openai_rejects_empty_model_id`` / ``test_openai_rejects_non_positive_dimensions``.
    A production backend MUST implement MUST 1; this stub is exempt by design.
    """

    def __init__(
        self,
        model_id: str = "stub-embedding-v1",
        dimensions: int = 4,
        provider_id: str = "stub",
    ) -> None:
        self._model_id = model_id
        self._dimensions = dimensions
        self._provider_id = provider_id
        self.close_call_count = 0

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def capabilities(self) -> EmbeddingCapabilities:
        return EmbeddingCapabilities(
            max_batch_size=512,
            max_input_tokens=8192,
            # Honest: the stub does not implement input_type parameter
            supports_input_type=False,
        )

    def embed(self, text: str, *, input_type: str | None = None) -> list[float] | None:
        """Return a fixed zero vector. Never raises.

        ``input_type`` is accepted per the PR-3 Protocol surface but ignored
        (stub does not implement provider-side input_type logic).
        """
        return [0.0] * self._dimensions

    def embed_batch(
        self, texts: list[str], *, input_type: str | None = None
    ) -> list[list[float] | None]:
        """Default implementation: loop embed() item-by-item.

        ``input_type`` is forwarded to embed() (which accepts-but-ignores it).
        """
        return [self.embed(t, input_type=input_type) for t in texts]

    def close(self) -> None:
        """Record close() calls for idempotency conformance testing."""
        self.close_call_count += 1


class ContentDerivedStubEmbeddingBackend(StubEmbeddingBackend):
    """Stub whose embed() vector is DETERMINISTICALLY derived from the text.

    Unlike StubEmbeddingBackend (fixed zero vector for every input), this stub
    produces a distinct, reproducible vector per input string. Required for
    tests that must distinguish embed(body_A) from embed(body_B) — e.g. the
    merge-write regression test, where a fixed-zero stub would make the
    negative control false-green (embed(fragment) == embed(stored body) == all
    zeros regardless of the fix).

    Deterministic: embed(same text) always returns the same vector, so a stored
    vector can be reproduced and compared.
    """

    def embed(self, text: str, *, input_type: str | None = None) -> list[float] | None:
        import hashlib

        digest = hashlib.sha256((text or "").encode("utf-8")).digest()
        # Map digest bytes deterministically to `dimensions` floats in [0, 1).
        return [digest[i % len(digest)] / 255.0 for i in range(self._dimensions)]


class _RaisingStubEmbeddingBackend:
    """Stub whose embed() raises internally -- used for NEGATIVE CONTROLS only.

    This backend does NOT satisfy the MUST-NOT-RAISE invariant. It exists
    solely to verify that conformance tests correctly detect the invariant
    violation (i.e., the test goes RED when the swallow wrapper is absent).

    Do NOT use this in positive conformance assertions.
    """

    def __init__(self, dimensions: int = 4) -> None:
        self._dimensions = dimensions
        self.close_call_count = 0

    @property
    def model_id(self) -> str:
        return "raising-stub-v1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def provider_id(self) -> str:
        return "stub"

    def capabilities(self) -> EmbeddingCapabilities:
        return EmbeddingCapabilities(
            max_batch_size=512,
            max_input_tokens=8192,
            supports_input_type=False,
        )

    def embed(self, text: str, *, input_type: str | None = None) -> list[float] | None:
        """ALWAYS raises RuntimeError -- for negative control testing only."""
        raise RuntimeError("stub intentional raise for negative control testing")

    def embed_batch(
        self, texts: list[str], *, input_type: str | None = None
    ) -> list[list[float] | None]:
        """ALWAYS raises RuntimeError -- for negative control testing only."""
        raise RuntimeError("stub intentional batch raise for negative control testing")

    def close(self) -> None:
        self.close_call_count += 1

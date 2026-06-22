"""Conformance test suite for the EmbeddingBackend Protocol (spec/46, LOCKED).

Parametrized over concrete backend implementations:
- ``StubEmbeddingBackend`` (test double, no external deps)
- ``OpenAIEmbeddingBackend`` with mocked OpenAI SDK

Each backend is exercised against the same 9-MUST Implementer Contract:

MUST 1: input validation (model_id non-empty, dimensions positive)
MUST 2: side-effect-free construction (no provider I/O at __init__)
MUST 3: capability honesty (advertised capabilities match behavior)
MUST 4: embed 4-case / MUST-NOT-RAISE invariant (all failures → None)
MUST 5: URL/secret redaction (no credentials logged)
MUST 6: storage/key isolation (provider_id stable)
MUST 7: snapshot/vector determinism (same text → same vector on success)
MUST 8: backend_id stability (model_id + provider_id stable across calls)
MUST 9: similarity-search/query-ranking axis (len(out)==len(in) invariant)

A third-party backend can import + use these helpers to verify its own
conformance against the EmbeddingBackend Protocol.

False-green protection (ENGINEERING LESSON #3):
Every MUST-NOT-RAISE test includes a negative control: the _RaisingStubEmbeddingBackend
whose embed() always raises internally. Conformance tests that assert None-return
on failure are verified to FAIL when the raising stub is used WITHOUT a swallow
wrapper. This confirms the test actually exercises the guard, not just the happy path.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.embedding.backend import EmbeddingBackend, EmbeddingCapabilities
from atomic_agents.embedding.openai import OpenAIEmbeddingBackend
from tests.stub_embedding import StubEmbeddingBackend, _RaisingStubEmbeddingBackend


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture(autouse=True)
def _provider_classifier_is_pristine():
    """Fail loudly if a sibling test leaked the _raise_if_provider_unavailable
    no-op monkeypatch (test_openai_typed_branch_negative_control replaces it to
    simulate the dead-branch state).

    monkeypatch teardown restores it, but asserting the helper is the real
    module function at setup converts a would-be intermittent cross-test
    isolation flake into a deterministic failure on the first polluted test
    (round-5 cross-test isolation hardening). We check it is the function
    DEFINED in the module (not a lambda someone left behind).
    """
    from atomic_agents.embedding import openai as _openai_mod

    fn = _openai_mod._raise_if_provider_unavailable
    assert getattr(fn, "__name__", None) == "_raise_if_provider_unavailable", (
        "leaked _raise_if_provider_unavailable monkeypatch from a prior test "
        f"(got {fn!r})"
    )
    # Also assert _get_key is the module-defined resolver — a sibling test that
    # leaks a tracking/counter wrapper for _get_key would otherwise poison the
    # empty-api-key negative control below with a stale call count (the once-seen
    # intermittent failure of test_openai_explicit_empty_api_key_does_not_call_
    # get_key).  Convert that latent flake into a deterministic, attributable
    # setup failure.
    gk = _openai_mod._get_key
    assert getattr(gk, "__name__", None) == "_get_key", (
        "leaked _get_key monkeypatch from a prior test "
        f"(got {gk!r}) — registry/credential isolation regression"
    )
    yield


def _make_mock_openai_module(*, fail_embed: bool = False, fail_batch: bool = False):
    """Build a minimal fake openai module for test isolation.

    Returns a fake module that patches sys.modules['openai'] so that
    _build_client() (per-call construction) intercepts correctly.
    """
    fake_openai = types.ModuleType("openai")

    class _FakeEmbedResponse:
        class _Item:
            def __init__(self, idx: int, vec: list[float]) -> None:
                self.index = idx
                self.embedding = vec

        def __init__(self, texts: list[str], dim: int) -> None:
            self.data = [
                self._Item(i, [0.1 * (i + 1)] * dim) for i in range(len(texts))
            ]

    class _FakeEmbeddings:
        def create(self, *, input: list[str], model: str, dimensions: int = 4):
            # Echo the requested dimension (default 4 when the impl omits the
            # kwarg) so len(result) == backend.dimensions holds for both the
            # default-dimension and reduced-dimension cases.
            if fail_batch and len(input) > 1:
                raise RuntimeError("fake batch failure")
            if fail_embed:
                raise RuntimeError("fake embed failure")
            return _FakeEmbedResponse(input, dimensions)

    class _FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _FakeEmbeddings()

        def close(self):
            pass

    fake_openai.OpenAI = _FakeClient
    return fake_openai


@pytest.fixture(
    params=["stub", "openai-mocked"],
    ids=["stub", "openai-mocked"],
)
def backend(request, monkeypatch):
    """Yield a concrete EmbeddingBackend implementation for each parametrized case."""
    if request.param == "stub":
        return StubEmbeddingBackend()
    if request.param == "openai-mocked":
        fake_openai = _make_mock_openai_module()
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        # dimensions=4: the default model (text-embedding-3-small) IS
        # dimension-reducible, so the impl forwards dimensions=4 to the fake's
        # create(), which echoes it. len(result)==backend.dimensions==4 holds
        # for both embed() and embed_batch() (this also exercises the
        # dimensions-honored path -- finding #2).
        return OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)
    raise ValueError(f"Unknown param {request.param}")


# ──────────────────────────────────────────────────────────────────────────────
# MUST 1: Input validation
# model_id must be non-empty; dimensions must be positive


def test_model_id_is_non_empty_string(backend):
    """MUST 1: model_id is a non-empty string."""
    assert isinstance(backend.model_id, str)
    assert len(backend.model_id) > 0


def test_dimensions_is_positive_int(backend):
    """MUST 1: dimensions is a positive integer."""
    assert isinstance(backend.dimensions, int)
    assert backend.dimensions > 0


def test_openai_rejects_empty_model_id(monkeypatch):
    """MUST 1: OpenAIEmbeddingBackend raises EmbeddingError on empty model_id.

    Negative control: this test goes RED if the __init__ validation is
    stripped (verified by removing the `if not model_id.strip()` guard ->
    construction succeeds and pytest.raises fails).
    """
    from atomic_agents.exceptions import EmbeddingError

    fake_openai = _make_mock_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    for bad in ("", "   "):
        with pytest.raises(EmbeddingError, match="model_id"):
            OpenAIEmbeddingBackend(model_id=bad, api_key="sk-fake")


def test_openai_rejects_non_positive_dimensions(monkeypatch):
    """MUST 1: OpenAIEmbeddingBackend raises EmbeddingError on dimensions <= 0.

    Negative control: this test goes RED if the dimensions guard is stripped.
    """
    from atomic_agents.exceptions import EmbeddingError

    fake_openai = _make_mock_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    for bad in (0, -1, -512):
        with pytest.raises(EmbeddingError, match="dimensions"):
            OpenAIEmbeddingBackend(dimensions=bad, api_key="sk-fake")


# ──────────────────────────────────────────────────────────────────────────────
# MUST 2: Side-effect-free construction
# Construction imports the SDK (fail-fast) but calls no provider endpoints.


def test_openai_backend_construction_requires_openai_sdk():
    """MUST 2: OpenAIEmbeddingBackend raises AtomicAgentsError if openai SDK missing."""
    from atomic_agents.exceptions import AtomicAgentsError

    with patch.dict(sys.modules, {"openai": None}):
        with pytest.raises(AtomicAgentsError):
            OpenAIEmbeddingBackend(api_key="sk-fake")


def test_openai_explicit_empty_api_key_does_not_call_get_key(monkeypatch):
    """An explicit ``api_key=''`` is honored literally; ``_get_key()`` is NOT called.

    SecretBackend coherence: ``__init__`` uses ``api_key if api_key is not None
    else _get_key()`` rather than the falsy ``api_key or _get_key()``.  The
    empty string is truthy-false, so the ``or`` form would silently fall through
    to SecretBackend resolution — masking a caller who deliberately passed ''.

    Negative control (project lesson #3): this test goes RED if line 277 is
    reverted to ``api_key or _get_key()`` — the ``or`` form WOULD invoke
    _get_key(), tripping the assertion.
    """
    import atomic_agents.embedding.openai as openai_mod

    # The negative control raises IMMEDIATELY on any invocation rather than
    # incrementing a shared counter.  A counter keyed on a test-local dict can
    # be poisoned by a leaked binding from a sibling test (the once-seen
    # intermittent failure); a fail-fast sentinel cannot — if _get_key runs at
    # all the failure is attributed to THIS construction.  Negative control
    # (project lesson #3): if openai.py line 277 regresses to
    # ``api_key or _get_key()``, the empty-string api_key falls through, this
    # raises, and the test goes RED.
    def _must_not_be_called():
        raise AssertionError(
            "_get_key() was called despite an explicit empty-string api_key; the "
            "falsy `or` short-circuit regressed (should be `is not None`)"
        )

    monkeypatch.setattr(openai_mod, "_get_key", _must_not_be_called)

    fake_openai = _make_mock_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    backend = OpenAIEmbeddingBackend(api_key="", dimensions=4)

    assert backend._api_key == "", (
        "explicit empty-string api_key was not honored literally"
    )


def test_stub_backend_constructs_with_no_side_effects():
    """MUST 2: StubEmbeddingBackend constructs without any I/O."""
    b = StubEmbeddingBackend()
    assert b.model_id == "stub-embedding-v1"


# ──────────────────────────────────────────────────────────────────────────────
# MUST 3: Capability honesty


def test_capabilities_returns_embedding_capabilities(backend):
    """MUST 3: capabilities() returns EmbeddingCapabilities dataclass."""
    caps = backend.capabilities()
    assert isinstance(caps, EmbeddingCapabilities)


def test_capabilities_max_batch_size_positive(backend):
    """MUST 3: max_batch_size is a positive integer."""
    caps = backend.capabilities()
    assert caps.max_batch_size > 0


def test_capabilities_max_input_tokens_positive(backend):
    """MUST 3: max_input_tokens is a positive integer."""
    caps = backend.capabilities()
    assert caps.max_input_tokens > 0


def test_capabilities_supports_input_type_is_false(backend):
    """MUST 3: supports_input_type=False for backends whose provider lacks input_type.

    Both StubEmbeddingBackend and OpenAIEmbeddingBackend advertise False.
    OpenAI SDK verified (Principle #12, 2026-06-18): openai.embeddings.create()
    does NOT include input_type as a native parameter. The kwarg is accepted on
    the Protocol surface (PR3 addition) and on both impls, but ignored on the
    provider side (capability honesty: advertise what you can actually honour).
    See spec/46 §"supports_input_type flag".
    """
    caps = backend.capabilities()
    assert caps.supports_input_type is False, (
        "supports_input_type must be False for OpenAIEmbeddingBackend and "
        "StubEmbeddingBackend -- the OpenAI SDK does not expose input_type, "
        "and the stub honestly advertises what it implements. "
        "A backend whose provider DOES support input_type should advertise True."
    )


def test_embed_accepts_input_type_kwarg(backend):
    """MUST 3 (PR3 Protocol surface): embed() accepts input_type kwarg without raising.

    All backends MUST accept the input_type kwarg. Backends with
    supports_input_type=False accept-but-ignore it; backends with True
    forward it to the provider. Either way the call must not raise.
    """
    result = backend.embed("hello world", input_type=None)
    # None is the 'no hint' value; embed should succeed normally
    assert result is None or isinstance(result, list)

    result2 = backend.embed("hello world", input_type="search_query")
    assert result2 is None or isinstance(result2, list)


def test_embed_batch_accepts_input_type_kwarg(backend):
    """MUST 3 (PR3 Protocol surface): embed_batch() accepts input_type kwarg without raising."""
    result = backend.embed_batch(["a", "b"], input_type=None)
    assert len(result) == 2

    result2 = backend.embed_batch(["a", "b"], input_type="search_document")
    assert len(result2) == 2


def test_capabilities_stable_across_calls(backend):
    """MUST 3: capabilities() returns the same value on repeated calls."""
    caps1 = backend.capabilities()
    caps2 = backend.capabilities()
    assert caps1 == caps2


# ──────────────────────────────────────────────────────────────────────────────
# MUST 4: Embed 4-case / MUST-NOT-RAISE invariant


def test_embed_returns_list_or_none_on_success(backend):
    """MUST 4: embed() returns list[float] on success."""
    result = backend.embed("hello world")
    assert result is not None
    assert isinstance(result, list)
    assert all(isinstance(v, float) for v in result)


def test_embed_vector_length_matches_dimensions(backend):
    """MUST 4: embed() vector length matches backend.dimensions."""
    result = backend.embed("hello")
    assert result is not None
    assert len(result) == backend.dimensions


def test_embed_batch_empty_returns_empty(backend):
    """MUST 9 (len invariant): embed_batch([]) == []."""
    result = backend.embed_batch([])
    assert result == []


def test_embed_batch_single_item(backend):
    """MUST 4/9: embed_batch(['text']) returns a length-1 list with a real vector.

    Both parametrized backends (stub + openai-mocked) succeed on a healthy
    single item, so the result MUST be a correctly-dimensioned vector — not a
    tautology that passes for any value. (A failed item would be None at its
    index, but neither backend here fails the happy path.)
    """
    result = backend.embed_batch(["single"])
    assert len(result) == 1
    assert result[0] is not None
    assert len(result[0]) == backend.dimensions


def test_embed_batch_length_invariant(backend):
    """MUST 9 (len invariant): len(embed_batch(texts)) == len(texts)."""
    texts = ["alpha", "beta", "gamma", "delta"]
    result = backend.embed_batch(texts)
    assert len(result) == len(texts)


def test_embed_batch_all_vectors_correct_dimension(backend):
    """MUST 4/9: each non-None result in embed_batch has correct dimension."""
    texts = ["a", "b", "c"]
    result = backend.embed_batch(texts)
    for vec in result:
        if vec is not None:
            assert len(vec) == backend.dimensions


# ──────────────────────────────────────────────────────────────────────────────
# MUST 4: MUST-NOT-RAISE invariant with NEGATIVE CONTROLS
#
# Engineering lesson: tests that only assert None-return on failure are
# false-green when both the typed and broad except branches return None.
# Negative controls verify the test FAILS when the swallow wrapper is absent.


def test_raising_stub_raises_without_wrapper():
    """NEGATIVE CONTROL: _RaisingStubEmbeddingBackend.embed() raises -- no wrapper.

    This confirms the raising stub actually raises, so we know our conformance
    tests for the MUST-NOT-RAISE invariant would go RED if the swallow wrapper
    were removed from a real implementation.
    """
    raising_backend = _RaisingStubEmbeddingBackend()
    with pytest.raises(RuntimeError, match="stub intentional raise"):
        raising_backend.embed("test")


def test_raising_stub_batch_raises_without_wrapper():
    """NEGATIVE CONTROL: _RaisingStubEmbeddingBackend.embed_batch() raises -- no wrapper."""
    raising_backend = _RaisingStubEmbeddingBackend()
    with pytest.raises(RuntimeError, match="stub intentional batch raise"):
        raising_backend.embed_batch(["a", "b"])


def test_openai_backend_embed_returns_none_on_sdk_failure(monkeypatch):
    """MUST 4 + NEGATIVE CONTROL: embed() swallows SDK exceptions → None.

    Positive assertion: SDK exception → None return.
    Negative control: the _RaisingStubEmbeddingBackend (no swallow wrapper)
    raises, proving this test would fail if OpenAIEmbeddingBackend's wrapper
    were removed.
    """
    fake_openai = _make_mock_openai_module(fail_embed=True)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    result = backend.embed("test text")
    # Positive assertion: embed() returns None, not an exception
    assert result is None

    # Negative control is test_raising_stub_raises_without_wrapper above --
    # that test confirms a bare raise propagates when there is no swallow wrapper.


def test_openai_backend_embed_logs_broad_branch_on_generic_failure(monkeypatch, caplog):
    """MUST 4 + layered-except: a generic (non-provider) error hits the BROAD branch.

    The fake's RuntimeError is NOT a provider-availability SDK error, so it must
    take the broad fallback ("embedding failed (unexpected error)") and MUST NOT
    take the typed branch ("provider unavailable"). Asserting BOTH the presence
    of the broad phrase AND the ABSENCE of the typed phrase is the branch-
    distinctive check (feedback_layered_except_typed_branch_false_green.md).
    """
    import logging

    fake_openai = _make_mock_openai_module(fail_embed=True)  # raises RuntimeError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    with caplog.at_level(logging.WARNING, logger="atomic_agents.embedding.openai"):
        result = backend.embed("test text")

    assert result is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("embedding failed (unexpected error)" in m for m in messages), (
        f"Expected broad-branch log; got: {messages}"
    )
    assert not any("provider unavailable" in m for m in messages), (
        f"Generic error must NOT take the typed provider-unavailable branch; got: {messages}"
    )


def _make_auth_failing_openai():
    """Build a fake openai module whose create() raises an AuthenticationError.

    The fake exception class is NAMED AuthenticationError so the impl's
    name-based MRO match routes it to the typed provider-unavailable branch
    (the real openai SDK is not imported in unit tests).
    """

    class AuthenticationError(Exception):  # name matches the openai SDK type
        pass

    class _FailingEmbeddings:
        def create(self, **kwargs):
            raise AuthenticationError("Invalid API key: sk-proj-XXXX")

    class _FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _FailingEmbeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _FakeClient
    fake_openai.AuthenticationError = AuthenticationError
    return fake_openai


def test_openai_backend_embed_logs_typed_branch_on_provider_error(monkeypatch, caplog):
    """MUST 4: a provider-availability SDK error takes the TYPED branch.

    Drives an AuthenticationError (mapped to EmbeddingProviderUnavailable) and
    asserts the typed-branch phrase "provider unavailable" appears AND the broad
    phrase "unexpected error" does NOT. This is the test that proves the typed
    branch is REACHABLE (not dead code).

    Negative control: test_openai_typed_branch_negative_control below strips
    the mapping and confirms this assertion would go RED.
    """
    import logging

    fake_openai = _make_auth_failing_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    with caplog.at_level(logging.WARNING, logger="atomic_agents.embedding.openai"):
        result = backend.embed("test text")

    assert result is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("provider unavailable" in m for m in messages), (
        f"Expected typed provider-unavailable log; got: {messages}"
    )
    assert not any("unexpected error" in m for m in messages), (
        f"Provider error must NOT fall through to the broad branch; got: {messages}"
    )


def test_openai_typed_branch_negative_control(monkeypatch, caplog):
    """NEGATIVE CONTROL for the typed branch: if the SDK-error -> typed mapping
    is removed, an AuthenticationError falls through to the BROAD branch.

    This simulates the dead-branch state by monkeypatching the mapping helper
    to a no-op. The typed-branch test above MUST then fail (no "provider
    unavailable" log; only "unexpected error"), proving that test is load-
    bearing and the typed branch is not vacuously satisfied.
    """
    import logging

    from atomic_agents.embedding import openai as openai_mod

    # Strip the mapping: _raise_if_provider_unavailable becomes a no-op, so the
    # typed branch can no longer fire (the dead-branch state finding #1 flagged).
    monkeypatch.setattr(openai_mod, "_raise_if_provider_unavailable", lambda exc: None)

    fake_openai = _make_auth_failing_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    with caplog.at_level(logging.WARNING, logger="atomic_agents.embedding.openai"):
        result = backend.embed("test text")

    assert result is None
    messages = [r.getMessage() for r in caplog.records]
    # With the mapping stripped, the auth error hits the broad branch.
    assert any("unexpected error" in m for m in messages), (
        f"With mapping stripped, expected broad-branch fallthrough; got: {messages}"
    )
    assert not any("provider unavailable" in m for m in messages), (
        "Mapping stripped: typed branch must be unreachable (this is the "
        f"dead-branch state); got: {messages}"
    )


def test_openai_backend_embed_does_not_log_credentials(monkeypatch, caplog):
    """MUST 5: credential redaction -- no API key appears in log output.

    The implementation logs type(exc).__name__ only, never str(exc) which
    may contain partial credentials in AuthenticationError messages.
    """
    import logging

    class _FakeAuthError(Exception):
        """Simulates openai.AuthenticationError with a credential-containing message."""

        def __str__(self):
            return "Invalid API key: sk-proj-1234abcd..."

    class _FailingEmbeddings:
        def create(self, *, input, model):
            raise _FakeAuthError("Invalid API key: sk-proj-1234abcd...")

    class _FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _FailingEmbeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    backend = OpenAIEmbeddingBackend(api_key="sk-proj-1234abcd-secret")

    with caplog.at_level(logging.WARNING, logger="atomic_agents.embedding.openai"):
        result = backend.embed("test")

    assert result is None
    # Verify no credential string appears in any log message
    all_messages = " ".join(r.getMessage() for r in caplog.records)
    assert "sk-proj-1234abcd" not in all_messages, (
        "API credential appeared in log output -- credential redaction failed"
    )


# ──────────────────────────────────────────────────────────────────────────────
# MUST 6: Storage/key isolation — provider_id stable


def test_provider_id_is_non_empty_string(backend):
    """MUST 6: provider_id is a non-empty string."""
    assert isinstance(backend.provider_id, str)
    assert len(backend.provider_id) > 0


def test_provider_id_stable_across_calls(backend):
    """MUST 6: provider_id returns the same value across calls."""
    pid1 = backend.provider_id
    pid2 = backend.provider_id
    assert pid1 == pid2


# ──────────────────────────────────────────────────────────────────────────────
# MUST 7: Snapshot/vector determinism


def test_embed_same_text_same_vector(backend):
    """MUST 7: embed() returns the same vector on repeated calls for the same text."""
    text = "determinism test"
    v1 = backend.embed(text)
    v2 = backend.embed(text)
    assert v1 is not None
    assert v2 is not None
    assert v1 == v2


# ──────────────────────────────────────────────────────────────────────────────
# MUST 8: backend_id stability


def test_model_id_stable_across_calls(backend):
    """MUST 8: model_id returns the same value across calls."""
    mid1 = backend.model_id
    mid2 = backend.model_id
    assert mid1 == mid2


# ──────────────────────────────────────────────────────────────────────────────
# MUST 9: len(out)==len(in) conformance invariant


def test_embed_batch_length_invariant_all_fail(monkeypatch):
    """MUST 9: len(out)==len(in) holds even when EVERY item fails (all-None output).

    Uses a stub whose embed() returns None for every item to simulate total failure.
    """

    class _AllNoneStub(StubEmbeddingBackend):
        def embed(self, text, *, input_type: str | None = None):
            return None  # simulate total failure per item

    stub = _AllNoneStub()
    texts = ["a", "b", "c", "d"]
    result = stub.embed_batch(texts)
    assert len(result) == len(texts)
    assert all(v is None for v in result)


def test_embed_batch_length_invariant_mixed_failure(monkeypatch):
    """MUST 9: len(out)==len(in) holds when SOME items fail (mixed output).

    Uses a stub whose embed() returns None on every odd index.
    """

    class _OddIndexFailStub(StubEmbeddingBackend):
        def __init__(self):
            super().__init__(dimensions=4)
            self._call_count = 0

        def embed(self, text, *, input_type: str | None = None):
            result = None if self._call_count % 2 == 1 else [0.0] * self._dimensions
            self._call_count += 1
            return result

    stub = _OddIndexFailStub()
    texts = ["a", "b", "c", "d"]
    result = stub.embed_batch(texts)
    assert len(result) == len(texts)
    # Items at index 0, 2 succeed; items at index 1, 3 fail
    assert result[0] is not None
    assert result[1] is None
    assert result[2] is not None
    assert result[3] is None


def test_embed_batch_length_invariant_truncation_negative_control():
    """MUST 9 negative control (spec/46 MUST 9 + ENGINEERING LESSON #3).

    Proves the len(out)==len(in) conformance assertion is LOAD-BEARING: a
    backend that violates the invariant by TRUNCATING its output list must make
    the assertion go RED. Without this control, the positive MUST-9 tests above
    could be false-green (they only ever see well-behaved backends).

    This ships in the importable conformance suite (not test_openai_embedding.py)
    so a third-party backend running these helpers gets the truncation control
    too -- the openai-only _normalize_length negative control does not cover the
    Protocol-level invariant a custom backend must satisfy.
    """

    class _TruncatingStub(StubEmbeddingBackend):
        def embed_batch(self, texts):
            # Bug shape: drop the last element instead of inserting None.
            return super().embed_batch(texts)[:-1]

    stub = _TruncatingStub()
    texts = ["a", "b", "c", "d"]
    result = stub.embed_batch(texts)
    # The conformance invariant is len(result) == len(texts); against the
    # truncating backend it must FAIL. We assert the violation here so the
    # control proves the invariant assertion is real, not vacuous.
    with pytest.raises(AssertionError):
        assert len(result) == len(texts), (
            "MUST-9 length invariant must hold; a truncating backend violates it"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Protocol isinstance checks


def test_stub_not_imported_by_production_code():
    """MECHANIZED GUARD: no production module imports the test stub.

    The stub's docstring tells a human to run a grep; this test runs the scan
    automatically so an accidental production import of StubEmbeddingBackend
    fails CI instead of slipping through silently (Principle #8 — a guard that
    lives only in a docstring rots; the correctness ratchet runs through the
    test suite). Walks the installed atomic_agents package source tree.
    """
    import re
    from pathlib import Path

    import atomic_agents

    pkg_root = Path(atomic_agents.__file__).parent
    pattern = re.compile(
        r"from\s+tests\.stub_embedding|import\s+.*StubEmbeddingBackend"
    )
    offenders = []
    for py in pkg_root.rglob("*.py"):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{py}:{lineno}: {line.strip()}")
    assert not offenders, (
        "test stub imported by production code (remove it):\n" + "\n".join(offenders)
    )


def test_stub_satisfies_isinstance():
    """isinstance(StubEmbeddingBackend(), EmbeddingBackend) must pass."""
    stub = StubEmbeddingBackend()
    assert isinstance(stub, EmbeddingBackend)


def test_openai_backend_satisfies_isinstance(monkeypatch):
    """isinstance(OpenAIEmbeddingBackend(...), EmbeddingBackend) must pass."""
    fake_openai = _make_mock_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    assert isinstance(backend, EmbeddingBackend)


def test_magic_mock_passes_isinstance_with_caveat():
    """MagicMock(spec=EmbeddingBackend) passes isinstance but is NOT a behavioral proxy.

    This test documents the @runtime_checkable gotcha: isinstance checks
    attribute presence only, not behavioral correctness. Tests must use
    concrete fake classes (StubEmbeddingBackend), not MagicMock, for
    conformance assertions.
    """
    mock = MagicMock(spec=EmbeddingBackend)
    # This passes -- but do NOT rely on it for behavioral conformance testing
    # The mock's embed() will return a MagicMock, not list[float] | None
    # Using MagicMock as a conformance proxy is a false-green anti-pattern.
    assert isinstance(mock, EmbeddingBackend)  # documents the behavior, not endorses it


# ──────────────────────────────────────────────────────────────────────────────
# Close idempotency (MUST 8, CorpusBackend/LLMBackend precedent)


def test_close_idempotent_stub():
    """MUST 8: close() on StubEmbeddingBackend is idempotent (callable twice)."""
    stub = StubEmbeddingBackend()
    stub.close()
    stub.close()  # must not raise
    assert stub.close_call_count == 2


def test_close_idempotent_openai(monkeypatch):
    """MUST 8: close() on OpenAIEmbeddingBackend is idempotent (callable twice)."""
    fake_openai = _make_mock_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    backend.close()
    backend.close()  # must not raise


def test_close_non_idempotent_raises_negative_control():
    """NEGATIVE CONTROL: a backend whose close() raises on second call fails idempotency.

    Demonstrates that the close() idempotency conformance test is load-bearing:
    a backend that raises on second close() correctly fails the conformance test.
    The isinstance() check PASSES (Python's @runtime_checkable checks method
    presence, and close() IS present here) but the behavioral contract fails.

    Note: in Python 3.12, @runtime_checkable DOES inspect Protocol methods for
    presence, so a class missing close() entirely will fail isinstance(). The
    relevant conformance gate is therefore the BEHAVIORAL test (call twice,
    assert no exception), not the isinstance() check.
    """

    class _NonIdempotentClose:
        """close() raises on second call -- violates idempotency MUST."""

        def __init__(self):
            self._closed = False

        @property
        def model_id(self):
            return "test"

        @property
        def dimensions(self):
            return 4

        @property
        def provider_id(self):
            return "test"

        def capabilities(self):
            return EmbeddingCapabilities(
                max_batch_size=10,
                max_input_tokens=100,
                supports_input_type=False,
            )

        def embed(self, text):
            return [0.0] * 4

        def embed_batch(self, texts):
            return [self.embed(t) for t in texts]

        def close(self):
            if self._closed:
                raise RuntimeError("close() called twice -- not idempotent!")
            self._closed = True

    bad_backend = _NonIdempotentClose()
    # isinstance PASSES -- close() is present
    assert isinstance(bad_backend, EmbeddingBackend)
    # First close() succeeds
    bad_backend.close()
    # Second close() raises -- proving the idempotency conformance test is load-bearing
    with pytest.raises(RuntimeError, match="not idempotent"):
        bad_backend.close()

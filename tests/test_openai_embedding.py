"""Implementation-specific tests for OpenAIEmbeddingBackend.

These tests exercise OpenAI-specific behavior beyond the shared conformance
suite, including:
- Batched SDK call with index-mapping result construction
- Per-item fallback when the batch call fails
- Shape-keyed mock side-effects (not positional lists -- see ENGINEERING LESSON #5)
- len(out)==len(in) invariant under partial batch failure
- Credential redaction in authentication error paths
- Backend_id and provider_id stability
- Integration test behind requires_openai marker (skips locally)

Mocking strategy: shape-keyed side-effect dispatchers, NOT fixed positional
lists. A positional list breaks when the number of SDK calls changes (e.g., if
a batch-size cap is added that splits one call into two). The shape-keyed
approach branches on call argument shape so tests stay correct regardless of
batching strategy changes.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import patch

import pytest

from atomic_agents.embedding.openai import OpenAIEmbeddingBackend

# Defined up top so every @requires_openai usage in the file resolves at import
# (module-level decorators are evaluated when the module loads, before the
# later definition site would be reached). See the live-OpenAI integration
# tests near the bottom of the file for the original motivating use.
requires_openai = pytest.mark.skipif(
    not os.environ.get("ATOMIC_AGENTS_TEST_OPENAI_KEY"),
    reason=(
        "Requires ATOMIC_AGENTS_TEST_OPENAI_KEY env var for live OpenAI embedding tests. "
        "CI: configure ATOMIC_AGENTS_TEST_OPENAI_KEY in repo secrets."
    ),
)


# Frozen production values of the provider-availability classifier sets. Several
# tests in this file (and the conformance file) monkeypatch the module globals
# _PROVIDER_UNAVAILABLE_EXACT / _PROVIDER_UNAVAILABLE_MRO and the
# _raise_if_provider_unavailable helper to simulate the pre-fix bug state. If a
# monkeypatch ever leaked across the test boundary (e.g. an exception escaping a
# `with`), the typed-vs-broad-branch assertions would silently flip. This
# autouse fixture makes those tests HERMETIC: it asserts the classifier is at
# its production value BEFORE each test body runs, so a leaked mutation fails
# loudly here rather than as an intermittent flake in an unrelated assertion
# test (the cross-test isolation hardening from the round-5 review).
# Captured FROM the production module at import time (before any test mutates
# it), so the baseline can never drift from the real classifier sets the way a
# hand-maintained copy would. The autouse fixture compares production against
# this import-time snapshot to detect a leaked monkeypatch.
from atomic_agents.embedding import openai as _openai_baseline_mod  # noqa: E402

_FROZEN_EXACT = frozenset(_openai_baseline_mod._PROVIDER_UNAVAILABLE_EXACT)
_FROZEN_MRO = frozenset(_openai_baseline_mod._PROVIDER_UNAVAILABLE_MRO)


@pytest.fixture(autouse=True)
def _classifier_globals_are_pristine():
    """Fail loudly if a sibling test leaked a classifier monkeypatch.

    monkeypatch teardown restores these, but ordering bugs or escaped `with`
    blocks could leave a mutation in place. Asserting the frozen values at setup
    converts a would-be intermittent cross-test flake into a deterministic,
    self-identifying failure on the FIRST polluted test.
    """
    from atomic_agents.embedding import openai as _openai_mod

    assert _openai_mod._PROVIDER_UNAVAILABLE_EXACT == _FROZEN_EXACT, (
        "leaked _PROVIDER_UNAVAILABLE_EXACT mutation from a prior test"
    )
    assert _openai_mod._PROVIDER_UNAVAILABLE_MRO == _FROZEN_MRO, (
        "leaked _PROVIDER_UNAVAILABLE_MRO mutation from a prior test"
    )
    yield


# ──────────────────────────────────────────────────────────────────────────────
# Helpers for shape-keyed mocking (ENGINEERING LESSON #5)


def _make_fake_openai(
    *,
    fail_on_batch_size: int | None = None,
    fail_on_call_n: int | None = None,
):
    """Build a fake openai module with a shape-keyed embed dispatcher.

    Args:
        fail_on_batch_size: raise RuntimeError when len(input) >= this value.
        fail_on_call_n: raise RuntimeError on the Nth call (1-indexed).
    """
    fake_openai = types.ModuleType("openai")
    call_counter = {"n": 0}

    class _FakeEmbedResponse:
        class _Item:
            def __init__(self, idx: int, dim: int) -> None:
                self.index = idx
                self.embedding = [float(idx + 1)] * dim

        def __init__(self, texts: list[str], dim: int) -> None:
            self.data = [self._Item(i, dim) for i in range(len(texts))]

    # Native sizes so an omitted `dimensions` kwarg echoes a REALISTIC vector
    # length (the produced-length MUST-3 check rejects a vector whose length !=
    # the backend's advertised dimensions, so the fake must mirror the real API:
    # native size when dimensions is omitted, the requested size when reduced).
    _fake_native = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    class _FakeEmbeddings:
        def create(
            self, *, input: list[str], model: str, dimensions: int | None = None
        ):
            call_counter["n"] += 1
            n = call_counter["n"]
            # Shape-keyed failure conditions (not positional list)
            if fail_on_call_n is not None and n == fail_on_call_n:
                raise RuntimeError(f"fake failure on call {n}")
            if fail_on_batch_size is not None and len(input) >= fail_on_batch_size:
                raise RuntimeError(f"fake failure on batch size {len(input)}")
            # Echo the requested dimension, or the model's native size when the
            # impl omits the kwarg (matches the real OpenAI API behavior).
            dim = (
                dimensions if dimensions is not None else _fake_native.get(model, 1536)
            )
            return _FakeEmbedResponse(input, dim)

    class _FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _FakeEmbeddings()

        def close(self):
            pass

    fake_openai.OpenAI = _FakeClient
    return fake_openai


# ──────────────────────────────────────────────────────────────────────────────
# Construction


def test_construction_with_explicit_key(monkeypatch):
    """OpenAIEmbeddingBackend constructs with an explicit api_key."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    assert backend.model_id == "text-embedding-3-small"
    assert backend.dimensions == 1536
    assert backend.provider_id == "openai"


def test_construction_default_model(monkeypatch):
    """Default model is text-embedding-3-small at 1536 dimensions."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    assert "3-small" in backend.model_id
    assert backend.dimensions == 1536


def test_construction_custom_model(monkeypatch):
    """Custom model_id and dimensions are respected."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(
        model_id="text-embedding-3-large",
        dimensions=3072,
        api_key="sk-fake",
    )
    assert backend.model_id == "text-embedding-3-large"
    assert backend.dimensions == 3072


def _make_native_dim_fake_openai(native_dim: int):
    """Fake whose create() returns the MODEL'S NATIVE dim when no `dimensions`
    kwarg is sent, mimicking the real OpenAI API.

    The real API ignores no kwarg and returns the model's native size; only an
    EXPLICIT `dimensions` reduces it. Most other fakes in this file default the
    kwarg to a small echo value, which would mask the native-default mismatch
    bug. This fake reproduces the real behavior so the MUST-3 regression below
    is load-bearing.
    """
    fake_openai = types.ModuleType("openai")

    class _Item:
        def __init__(self, idx, dim):
            self.index = idx
            self.embedding = [0.0] * dim

    class _Resp:
        def __init__(self, texts, dim):
            self.data = [_Item(i, dim) for i in range(len(texts))]

    class _Embeddings:
        def create(self, *, input, model, dimensions=None):
            # No dimensions kwarg -> native dim (real-API behavior).
            dim = native_dim if dimensions is None else dimensions
            return _Resp(input, dim)

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai.OpenAI = _Client
    return fake_openai


def test_large_default_dimension_matches_native(monkeypatch):
    """MUST 3 (capability honesty): text-embedding-3-large with NO explicit
    `dimensions` advertises AND returns its native 3072, not the global 1536.

    Regression for the P1 bug where a single global DEFAULT_EMBEDDING_DIMENSIONS
    (1536) made 3-large advertise 1536 while the API returned 3072 -- a silent
    mis-size that PR3's pgvector wiring would turn into insert failures.
    """
    fake_openai = _make_native_dim_fake_openai(native_dim=3072)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(
        model_id="text-embedding-3-large", api_key="sk-fake"
    )
    # Advertised dimension must equal the model's native size.
    assert backend.dimensions == 3072
    # And the produced vector length must equal the advertised dimension.
    result = backend.embed("native default")
    assert result is not None
    assert len(result) == backend.dimensions == 3072


def test_large_default_dimension_negative_control(monkeypatch):
    """NEGATIVE CONTROL for the MUST-3 native-default fix.

    Simulates the bug-state by forcing the advertised dimension to the wrong
    global default (1536) AND stubbing _create_kwargs to omit the kwarg (as the
    pre-fix code did when self._dimensions == the global constant). The fake
    then returns the native 3072, so advertised (1536) != produced (3072) --
    proving the positive test above goes RED if the fix is stripped.
    """
    fake_openai = _make_native_dim_fake_openai(native_dim=3072)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(
        model_id="text-embedding-3-large", api_key="sk-fake"
    )
    # Force the pre-fix bug state: wrong advertised dim + dropped kwarg.
    backend._dimensions = 1536
    monkeypatch.setattr(
        backend,
        "_create_kwargs",
        lambda inputs: {"input": inputs, "model": backend.model_id},
    )
    result = backend.embed("native default")
    # Bug state: advertised 1536 but the API returns native 3072. The MUST-3
    # produced-length check (#1b) now CATCHES advertised != produced and returns
    # None rather than handing back a wrong-length vector. This control goes RED
    # if EITHER the native-default fix OR the produced-length guard regresses
    # (a regressed guard would return the 3072 vector instead of None).
    assert backend.dimensions == 1536
    assert result is None


def test_construction_raises_on_missing_sdk():
    """AtomicAgentsError raised when openai SDK is not installed."""
    from atomic_agents.exceptions import AtomicAgentsError

    with patch.dict(sys.modules, {"openai": None}):
        with pytest.raises(AtomicAgentsError, match="openai SDK not installed"):
            OpenAIEmbeddingBackend(api_key="sk-fake")


def test_reduced_dimensions_forwarded_to_api(monkeypatch):
    """Finding #2: a reduced `dimensions` is sent to the API AND honored.

    text-embedding-3-large is dimension-reducible. Constructing with
    dimensions=512 must (a) forward dimensions=512 to create() and (b) yield a
    512-element vector, so backend.dimensions == len(embed(...)).

    Negative control: test_reduced_dimensions_negative_control below confirms
    this test goes RED if the kwarg is dropped.
    """
    seen = {}

    fake_openai = types.ModuleType("openai")

    class _Item:
        def __init__(self, idx, dim):
            self.index = idx
            self.embedding = [0.0] * dim

    class _Resp:
        def __init__(self, texts, dim):
            self.data = [_Item(i, dim) for i in range(len(texts))]

    class _Embeddings:
        def create(self, *, input, model, dimensions=1536):
            seen["dimensions"] = dimensions
            return _Resp(input, dimensions)

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai.OpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    backend = OpenAIEmbeddingBackend(
        model_id="text-embedding-3-large", dimensions=512, api_key="sk-fake"
    )
    result = backend.embed("reduce me")
    assert seen["dimensions"] == 512, "reduced dimensions not forwarded to the API"
    assert result is not None
    assert len(result) == backend.dimensions == 512


def test_reduced_dimensions_negative_control(monkeypatch):
    """NEGATIVE CONTROL for finding #2: if _create_kwargs dropped `dimensions`,
    the API would return the model default and len(result) != backend.dimensions.

    We simulate the dropped-kwarg bug by monkeypatching _create_kwargs to omit
    `dimensions`; the fake then returns its default (1536), so the honored-
    dimension assertion (== 512) would fail. Proves the positive test is
    load-bearing.
    """
    fake_openai = types.ModuleType("openai")

    class _Item:
        def __init__(self, idx, dim):
            self.index = idx
            self.embedding = [0.0] * dim

    class _Resp:
        def __init__(self, texts, dim):
            self.data = [_Item(i, dim) for i in range(len(texts))]

    class _Embeddings:
        def create(self, *, input, model, dimensions=1536):
            return _Resp(input, dimensions)

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai.OpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    backend = OpenAIEmbeddingBackend(
        model_id="text-embedding-3-large", dimensions=512, api_key="sk-fake"
    )
    # Simulate the bug: _create_kwargs forgets to forward `dimensions`.
    monkeypatch.setattr(
        backend,
        "_create_kwargs",
        lambda inputs: {"input": inputs, "model": backend.model_id},
    )
    result = backend.embed("reduce me")
    # With the kwarg dropped, the fake returns its default 1536, NOT 512. The
    # MUST-3 produced-length check (#1b) catches advertised(512) != produced(1536)
    # and returns None -- the dropped-dimensions bug is now fail-safe (was: a
    # silently-wrong-length vector). Control bites if the kwarg-forward regresses.
    assert backend.dimensions == 512
    assert result is None


def test_non_reducible_model_rejects_non_default_dimensions(monkeypatch):
    """MUST 3: ada-002 has no server-side reduction; a non-default dimensions
    is refused at construction rather than silently advertised-but-not-honored.

    Negative control: removing the construction guard lets this construct and
    the pytest.raises fails.
    """
    from atomic_agents.exceptions import EmbeddingError

    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    with pytest.raises(EmbeddingError, match="dimension reduction"):
        OpenAIEmbeddingBackend(
            model_id="text-embedding-ada-002", dimensions=512, api_key="sk-fake"
        )


def test_unknown_model_rejects_explicit_dimensions(monkeypatch):
    """MUST 3 (unknown-model limitation): an unknown model with an explicit
    non-native dimensions is refused at construction, because the backend cannot
    validate the dimensions against a native size it does not know or confirm the
    model supports server-side reduction. Distinct branch (and message) from the
    known-non-reducible ada-002 refusal above.

    Negative control: removing the unknown-model guard lets this construct and
    advertise a dimensions value the provider may not honor — pytest.raises fails.
    """
    from atomic_agents.exceptions import EmbeddingError

    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    with pytest.raises(EmbeddingError, match="unknown to this backend"):
        OpenAIEmbeddingBackend(
            model_id="some-future-embedding-model", dimensions=512, api_key="sk-fake"
        )


def test_reducible_model_rejects_dimensions_above_native(monkeypatch):
    """MUST 3 (#1, RedTeam + Codex CRITICAL): a reducible model with dimensions >
    native is refused at construction. Server-side reduction can only SHRINK; a
    larger value would be forwarded, 400'd by the API, and swallowed to None on
    every call while the backend advertised the impossible larger size -- and PR3
    would size a pgvector column to a vector the API never returns.

    Negative control: removing the `dimensions > native` guard lets this construct
    (the != native guard does not fire for reducible models), so the pytest.raises
    fails.
    """
    from atomic_agents.exceptions import EmbeddingError

    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    # 3-small native is 1536; 4096 exceeds it.
    with pytest.raises(EmbeddingError, match="exceeds the native size"):
        OpenAIEmbeddingBackend(
            model_id="text-embedding-3-small", dimensions=4096, api_key="sk-fake"
        )


def test_reducible_model_accepts_dimensions_at_and_below_native(monkeypatch):
    """Boundary: dimensions == native and dimensions < native both construct
    (only > native is refused). Guards against an over-strict off-by-one that
    would reject the legitimate full-size and reduced cases."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    # == native
    b1 = OpenAIEmbeddingBackend(
        model_id="text-embedding-3-small", dimensions=1536, api_key="sk-fake"
    )
    assert b1.dimensions == 1536
    # < native (a valid reduction)
    b2 = OpenAIEmbeddingBackend(
        model_id="text-embedding-3-small", dimensions=256, api_key="sk-fake"
    )
    assert b2.dimensions == 256


# ──────────────────────────────────────────────────────────────────────────────
# embed() single-item


def test_embed_success(monkeypatch):
    """embed() returns a list[float] on success."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    result = backend.embed("hello")
    assert isinstance(result, list)
    # The fake echoes the model's native size (default 3-small -> 1536), which
    # matches the advertised dimensions so the MUST-3 produced-length check passes.
    assert len(result) == backend.dimensions == 1536
    assert all(isinstance(v, float) for v in result)


def test_embed_returns_none_on_sdk_error(monkeypatch):
    """embed() returns None when the SDK raises -- MUST-NOT-RAISE invariant."""
    fake_openai = _make_fake_openai(fail_on_call_n=1)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    result = backend.embed("fail me")
    assert result is None


def test_embed_never_raises(monkeypatch):
    """embed() must not propagate exceptions -- covers RuntimeError, network errors."""

    # Use an always-failing client (not just fail_on_call_n=1 which only fails once)
    class _AlwaysFailEmbeddings:
        def create(self, *, input, model):
            raise RuntimeError("always fails -- testing MUST-NOT-RAISE")

    class _AlwaysFailClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _AlwaysFailEmbeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _AlwaysFailClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    # Must not raise even under repeated failures
    for _ in range(3):
        result = backend.embed("keep failing")
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# embed_batch() -- batched SDK call and per-item fallback


def test_embed_batch_success(monkeypatch):
    """embed_batch() issues a single SDK call and returns correct-length result."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    texts = ["alpha", "beta", "gamma"]
    result = backend.embed_batch(texts)
    assert len(result) == 3
    for vec in result:
        assert vec is not None
        assert isinstance(vec, list)


def test_embed_batch_length_invariant_on_success(monkeypatch):
    """MUST 9: len(embed_batch(texts)) == len(texts) on success."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    texts = ["a", "b", "c", "d", "e"]
    result = backend.embed_batch(texts)
    assert len(result) == len(texts)


def test_embed_batch_empty_returns_empty(monkeypatch):
    """MUST 9: embed_batch([]) returns []."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    result = backend.embed_batch([])
    assert result == []


def test_embed_batch_falls_back_to_per_item_on_failure(monkeypatch):
    """embed_batch() degrades to per-item embed() when the batch call fails.

    Shape-keyed: fail_on_batch_size=2 means batch calls fail but single-item
    calls succeed. This verifies the per-item fallback path.
    """
    # Fail any batch call of size >= 2; single-item (size 1) calls succeed.
    fake_openai = _make_fake_openai(fail_on_batch_size=2)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    texts = ["alpha", "beta", "gamma"]
    result = backend.embed_batch(texts)
    # Per-item fallback: each item gets its own embed() call (size 1, succeeds)
    assert len(result) == len(texts)
    # All items should succeed via per-item path
    assert all(v is not None for v in result)


def test_embed_batch_length_invariant_when_batch_fails(monkeypatch):
    """MUST 9: len invariant holds when the batch call fails AND per-item also fails."""
    # Make ALL embed calls fail (both batch and single-item)
    fake_openai = _make_fake_openai(fail_on_call_n=1)

    # Override to make every call fail
    class _AlwaysFailEmbeddings:
        def create(self, *, input, model):
            raise RuntimeError("always fails")

    class _AlwaysFailClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _AlwaysFailEmbeddings()

        def close(self):
            pass

    fake_openai.OpenAI = _AlwaysFailClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    texts = ["a", "b", "c"]
    result = backend.embed_batch(texts)
    # Length invariant must hold even with total failure
    assert len(result) == len(texts)
    # All items should be None (every embed() fails)
    assert all(v is None for v in result)


def test_embed_batch_index_mapping_correct(monkeypatch):
    """embed_batch() maps response objects to input positions by index.

    This tests that the implementation reads response.data[i].index to build
    the output list, not positional list order. Uses a response that returns
    items in reverse order to verify index-mapping correctness.
    """
    fake_openai = types.ModuleType("openai")

    class _ReversedResponse:
        class _Item:
            def __init__(self, idx: int) -> None:
                self.index = idx
                self.embedding = [float(idx)] * 4

        def __init__(self, texts: list[str]) -> None:
            # Return items in REVERSE index order to test index-mapping
            self.data = [self._Item(i) for i in reversed(range(len(texts)))]

    class _FakeEmbeddings:
        def create(self, *, input, model, dimensions=None):
            return _ReversedResponse(input)

    class _FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _FakeEmbeddings()

        def close(self):
            pass

    fake_openai.OpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    # dimensions=4 (a valid reduction for 3-small) so the fake's 4-float vectors
    # match the advertised dimension and the MUST-3 produced-length check passes.
    backend = OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)
    texts = ["first", "second", "third"]
    result = backend.embed_batch(texts)
    assert len(result) == 3
    # Index 0 → embedding [0.0, 0.0, 0.0, 0.0]
    # Index 1 → embedding [1.0, 1.0, 1.0, 1.0]
    # Index 2 → embedding [2.0, 2.0, 2.0, 2.0]
    assert result[0] == [0.0] * 4
    assert result[1] == [1.0] * 4
    assert result[2] == [2.0] * 4


def test_embed_batch_partial_response_reembeds_missing(monkeypatch):
    """A well-formed but INCOMPLETE batch response (fewer items than input) must
    RE-EMBED the missing indices via per-item embed() rather than silently leaving
    None gaps (#2 / MUST 9). Only the gap is re-embedded (no re-billing of items
    the batch already returned), and the length invariant holds.

    Negative control: if the re-embed-missing branch regressed to `by_index.get(i)`
    (silent None), result[1] would be None and the call count would stay at 1.
    """
    import logging

    calls = {"n": 0}

    class _Item:
        def __init__(self, idx, dim):
            self.index = idx
            self.embedding = [float(idx)] * dim

    class _Resp:
        def __init__(self, data):
            self.data = data

    class _Embeddings:
        def create(self, *, input, model, dimensions=None):
            calls["n"] += 1
            dim = dimensions if dimensions is not None else 4
            if calls["n"] == 1:
                # First (batch) call: omit index 1 -> an incomplete 200 response.
                return _Resp([_Item(i, dim) for i in range(len(input)) if i != 1])
            # Per-item re-embed of the single missing text -> return its one item.
            return _Resp([_Item(0, dim)])

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    backend = OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)
    texts = ["first", "second", "third"]
    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed_batch(texts)

    assert len(result) == 3
    # The missing slot is RE-EMBEDDED, not left None.
    assert all(v is not None for v in result)
    # Exactly one batch call + one per-item re-embed for the single gap (the
    # already-returned items 0 and 2 are NOT re-billed).
    assert calls["n"] == 2
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("re-embedded" in m for m in msgs), msgs


def test_embed_batch_empty_response_does_not_amplify(monkeypatch):
    """Round-2 Codex finding: an empty (or mostly-empty) 200 response must NOT be
    re-embedded per-item -- that would turn one batch into up to N provider calls
    (amplification + possible double-bill). A non-credible partial returns None
    for the missing slots WITHOUT per-item retry; only ONE provider call total.

    Negative control: if the credible-partial gate were removed (always
    re-embed), calls would be 1 + len(texts) and result would be all non-None.
    """
    import logging

    calls = {"n": 0}

    class _Resp:
        def __init__(self, data):
            self.data = data

    class _Embeddings:
        def create(self, *, input, model, dimensions=None):
            calls["n"] += 1
            return _Resp([])  # empty 200 -- no embeddings returned

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed_batch(["a", "b", "c", "d"])

    assert result == [None, None, None, None]
    assert calls["n"] == 1, f"empty 200 must NOT amplify; got {calls['n']} calls"
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("not a credible partial" in m for m in msgs), msgs


def _make_batch_anomaly_openai(*, build_batch_data):
    """Fake openai whose FIRST (batch) create() returns `build_batch_data(n)`
    items, and whose per-item create() returns a single correct item. Used to
    inject structurally-malformed batch responses (#2)."""
    calls = {"n": 0}

    class _Item:
        def __init__(self, idx, dim):
            self.index = idx
            self.embedding = [float(idx)] * dim

    class _Resp:
        def __init__(self, data):
            self.data = data

    class _Embeddings:
        def create(self, *, input, model, dimensions=None):
            calls["n"] += 1
            dim = dimensions if dimensions is not None else 4
            if calls["n"] == 1 and len(input) > 1:
                return _Resp(build_batch_data(len(input), dim, _Item))
            # per-item re-embed: one valid item at index 0
            return _Resp([_Item(0, dim)])

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    mod = types.ModuleType("openai")
    mod.OpenAI = _Client
    mod._calls = calls
    return mod


def test_embed_batch_duplicate_index_degrades_to_per_item(monkeypatch):
    """#2 (silent-corruption guard): a DUPLICATE response index would overwrite a
    different text's vector (a WRONG vector at a position -- the worst failure for
    a vector store). The backend must detect it and degrade to per-item rather
    than trust the corrupt mapping.

    Negative control: without the `idx in by_index` check, the dup silently maps
    and no per-item degrade happens (calls stays 1).
    """
    import logging

    # 3 inputs but the batch returns index 0 twice and index 1 (no index 2).
    def _dup(n, dim, Item):
        return [Item(0, dim), Item(0, dim), Item(1, dim)]

    fake_openai = _make_batch_anomaly_openai(build_batch_data=_dup)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed_batch(["a", "b", "c"])

    assert len(result) == 3
    assert all(v is not None for v in result)  # recovered via per-item
    # 1 malformed batch + 3 per-item re-embeds = 4 (whole batch distrusted).
    assert fake_openai._calls["n"] == 4
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("malformed" in m for m in msgs), msgs


def test_embed_batch_out_of_range_index_degrades_to_per_item(monkeypatch):
    """#2: an OUT-OF-RANGE response index (>= len(texts)) must trigger a degrade,
    not a silent drop."""
    import logging

    def _oor(n, dim, Item):
        # index 5 is out of range for a 3-input batch
        return [Item(0, dim), Item(1, dim), Item(5, dim)]

    fake_openai = _make_batch_anomaly_openai(build_batch_data=_oor)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed_batch(["a", "b", "c"])

    assert len(result) == 3
    assert all(v is not None for v in result)
    assert fake_openai._calls["n"] == 4
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("malformed" in m for m in msgs), msgs


def test_embed_batch_wrong_vector_length_degrades_to_per_item(monkeypatch):
    """#2 / MUST 3: a batch item whose vector length != advertised dimensions is
    a structural anomaly -> degrade to per-item (whose produced-length check then
    independently guards each result)."""
    import logging

    def _wrong_len(n, dim, Item):
        items = [Item(i, dim) for i in range(n)]
        items[1].embedding = [0.0] * (dim + 7)  # wrong length at index 1
        return items

    fake_openai = _make_batch_anomaly_openai(build_batch_data=_wrong_len)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed_batch(["a", "b", "c"])

    assert len(result) == 3
    assert all(v is not None for v in result)  # per-item recovers (correct length)
    assert fake_openai._calls["n"] == 4
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("malformed" in m for m in msgs), msgs


# ──────────────────────────────────────────────────────────────────────────────
# close() behavior


def test_close_is_noop(monkeypatch):
    """close() does not raise for a stateless per-call-client backend."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    backend.close()  # no-op, must not raise


def test_close_idempotent(monkeypatch):
    """close() called twice must not raise."""
    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    backend.close()
    backend.close()  # idempotent


# ──────────────────────────────────────────────────────────────────────────────
# Capabilities


def test_capabilities_fields(monkeypatch):
    """OpenAIEmbeddingBackend.capabilities() returns correct field values."""
    from atomic_agents.embedding.backend import EmbeddingCapabilities

    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    caps = backend.capabilities()
    assert isinstance(caps, EmbeddingCapabilities)
    assert caps.max_batch_size == 2048
    assert caps.max_input_tokens == 8192
    # PR2: supports_input_type MUST be False (parameter not yet on Protocol surface)
    assert caps.supports_input_type is False


# ──────────────────────────────────────────────────────────────────────────────
# MUST 4: construction-time raise (the no-key path) must NOT escape embed()


def _make_construction_raising_openai(exc_name: str = "OpenAIError"):
    """Build a fake openai module whose OpenAI(...) raises AT CONSTRUCTION.

    Models the real-world no-key path: openai.OpenAI(api_key=None) raises
    OpenAIError at construction. The exception class is NAMED so the impl's
    MRO-by-name match (or the broad fallback) handles it. embed()/embed_batch()
    must STILL return None / [None]*n -- _build_client() is inside the try.
    """
    raising_exc = type(exc_name, (Exception,), {})

    class _RaisingClient:
        def __init__(self, api_key=None, **kwargs):
            raise raising_exc("missing credentials at construction")

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _RaisingClient
    setattr(fake_openai, exc_name, raising_exc)
    return fake_openai


def test_embed_returns_none_when_client_construction_raises(monkeypatch):
    """MUST 4 regression: a no-key openai.OpenAI(...) raise at CONSTRUCTION must
    not escape embed(). _build_client() is inside the try (finding #2).

    OpenAIError is matched leaf-exact in _PROVIDER_UNAVAILABLE_EXACT, so this
    routes to the typed 'provider unavailable' branch.
    """
    import logging

    fake_openai = _make_construction_raising_openai("OpenAIError")
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key=None)  # no key -> construction would raise

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed("x")
    assert result is None
    # Routed to the typed branch (OpenAIError is a mapped availability name).
    assert any("provider unavailable" in str(c.args[0]) for c in warn.call_args_list), (
        warn.call_args_list
    )


def test_embed_batch_returns_none_list_when_client_construction_raises(monkeypatch):
    """MUST 4 + MUST 9 regression: a construction raise in embed_batch must yield
    [None]*len(texts), not propagate (finding #2).
    """
    fake_openai = _make_construction_raising_openai("OpenAIError")
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key=None)
    result = backend.embed_batch(["a", "b", "c"])
    assert result == [None, None, None]


def test_embed_construction_raise_negative_control(monkeypatch):
    """NEGATIVE CONTROL for finding #2: simulate the BUG state where
    _build_client() is OUTSIDE the try by having the test call it directly.

    Proves the construction-raise path is real: _build_client() does raise when
    the SDK client raises at construction, so leaving it outside embed()'s try
    (the pre-fix state) would let that exception escape embed(). With the fix,
    the same raise is caught inside embed() (asserted by the test above).
    """
    fake_openai = _make_construction_raising_openai("OpenAIError")
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key=None)
    # The raw _build_client() (what runs INSIDE the try after the fix) raises;
    # before the fix it ran OUTSIDE the try, so this raise escaped embed().
    with pytest.raises(Exception, match="missing credentials at construction"):
        backend._build_client()


# ──────────────────────────────────────────────────────────────────────────────
# Error classification: 4xx CLIENT errors take the BROAD branch, not the typed one


def _make_named_error_openai(exc_name: str, mro_extra_names=()):
    """Build a fake openai module whose create() raises a NAMED error.

    ``mro_extra_names`` are the ANCESTOR class names the synthetic exception
    should inherit, OUTERMOST (nearest leaf) first. To faithfully model the real
    openai SDK hierarchy a 4xx error MUST be built with the full chain that ends
    at the SDK root ``OpenAIError`` -- verified against openai 2.35.1:
    ``BadRequestError.__mro__`` == [BadRequestError, APIStatusError, APIError,
    OpenAIError, Exception, ...]. Omitting ``OpenAIError`` here produces a fake
    whose MRO has no shared ancestor with the impl's classifier, which would make
    the 4xx-takes-broad-branch test pass REGARDLESS of whether production is
    correct (the exact false-green shape
    feedback_layered_except_typed_branch_false_green.md warns against). The
    live-SDK guard ``test_real_sdk_mro_includes_openai_error_root`` pins this
    chain so the fake cannot silently drift from the real SDK.

    The names are chained as a single linear inheritance line so
    ``type(exc).__name__`` is the leaf and ``__mro__`` carries every ancestor
    name, mirroring the real single-inheritance SDK chain.
    """
    base: type = Exception
    # Build the chain from the deepest ancestor outward so the leaf is last.
    for name in reversed(mro_extra_names):
        base = type(name, (base,), {})
    raising_exc = type(exc_name, (base,), {})

    class _Embeddings:
        def create(self, **kwargs):
            raise raising_exc(f"{exc_name}: simulated")

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _Client
    return fake_openai


# The full real-SDK ancestor chain for a 4xx client error, OUTERMOST first.
# (BadRequestError -> APIStatusError -> APIError -> OpenAIError -> Exception).
_SDK_4XX_MRO = ("APIStatusError", "APIError", "OpenAIError")


def test_bad_request_takes_broad_branch_not_typed(monkeypatch):
    """A 4xx CLIENT error (BadRequestError, faithfully inheriting the real SDK
    chain up to the OpenAIError root) is NOT a provider-availability failure; it
    must take the BROAD 'unexpected error' branch, not the typed 'provider
    unavailable' branch.

    This is the audit-label correctness MUST 4 + the docstring/CHANGELOG/spec all
    claim. It is load-bearing ONLY because the fake's MRO now includes
    ``OpenAIError`` exactly as the real SDK does -- with the pre-fix
    any-ancestor-name match against a set containing ``OpenAIError``, a 400
    (input-too-long, the max_input_tokens failure mode) was MISLABELED as
    provider-unavailable. The negative control below strips the leaf-exact fix
    and asserts that regression.
    """
    import logging

    fake_openai = _make_named_error_openai(
        "BadRequestError", mro_extra_names=_SDK_4XX_MRO
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed("x")
    assert result is None
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("unexpected error" in m for m in msgs), msgs
    assert not any("provider unavailable" in m for m in msgs), (
        f"4xx client error must NOT take the typed branch; got: {msgs}"
    )


@pytest.mark.parametrize(
    "exc_name", ["BadRequestError", "NotFoundError", "UnprocessableEntityError"]
)
def test_4xx_client_errors_take_broad_branch(monkeypatch, exc_name):
    """Every 4xx CLIENT error in the spec/46 MUST-4 table (400/404/422) takes the
    broad branch with the full real-SDK ancestor chain (rooted at OpenAIError).
    """
    import logging

    fake_openai = _make_named_error_openai(exc_name, mro_extra_names=_SDK_4XX_MRO)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed("x")
    assert result is None
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("unexpected error" in m for m in msgs), (exc_name, msgs)
    assert not any("provider unavailable" in m for m in msgs), (exc_name, msgs)


def test_bad_request_broad_branch_negative_control(monkeypatch):
    """NEGATIVE CONTROL: reproduce the pre-fix bug by moving ``OpenAIError`` from
    the leaf-EXACT set into the MRO-membership set. Because the fake's MRO now
    faithfully includes ``OpenAIError`` (the real SDK root), any-ancestor
    matching re-mislabels the 400 as provider-unavailable -- proving the
    leaf-exact split in production is load-bearing.

    This must FAIL (raise) if the production fix is stripped, and the strip it
    simulates is exactly the historical bug (OpenAIError matched by ancestry).
    """
    import logging

    from atomic_agents.embedding import openai as openai_mod

    # Re-introduce the bug: OpenAIError matched by ANCESTRY, not leaf-exact.
    monkeypatch.setattr(openai_mod, "_PROVIDER_UNAVAILABLE_EXACT", frozenset())
    monkeypatch.setattr(
        openai_mod,
        "_PROVIDER_UNAVAILABLE_MRO",
        openai_mod._PROVIDER_UNAVAILABLE_MRO | {"OpenAIError"},
    )
    fake_openai = _make_named_error_openai(
        "BadRequestError", mro_extra_names=_SDK_4XX_MRO
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed("x")
    assert result is None
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    # Bug state: now the 400 IS (mis)labeled as provider unavailable.
    assert any("provider unavailable" in m for m in msgs), msgs


def test_permission_denied_takes_broad_branch_not_typed(monkeypatch):
    """A 403 PermissionDeniedError is a PERSISTENT operator-actionable config
    error (billing/access/scope), NOT a transient provider outage. It must take
    the broad 'unexpected error' branch so the audit label points the operator at
    their config -- not the 'provider unavailable' label which reads as
    retry-later (#3, RedTeam + Codex confirmed).

    Negative control: re-adding 'PermissionDeniedError' to _PROVIDER_UNAVAILABLE_MRO
    flips it back to the typed branch (asserted in
    test_permission_denied_broad_branch_negative_control below).
    """
    import logging

    fake_openai = _make_named_error_openai(
        "PermissionDeniedError", mro_extra_names=_SDK_4XX_MRO
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed("x")
    assert result is None
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("unexpected error" in m for m in msgs), msgs
    assert not any("provider unavailable" in m for m in msgs), (
        f"403 PermissionDenied must NOT take the typed transient branch; got: {msgs}"
    )


def test_permission_denied_broad_branch_negative_control(monkeypatch):
    """NEGATIVE CONTROL for #3: re-adding PermissionDeniedError to the
    availability MRO set re-routes the 403 to the typed 'provider unavailable'
    branch -- proving the removal in production is load-bearing."""
    import logging

    from atomic_agents.embedding import openai as openai_mod

    monkeypatch.setattr(
        openai_mod,
        "_PROVIDER_UNAVAILABLE_MRO",
        openai_mod._PROVIDER_UNAVAILABLE_MRO | {"PermissionDeniedError"},
    )
    fake_openai = _make_named_error_openai(
        "PermissionDeniedError", mro_extra_names=_SDK_4XX_MRO
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed("x")
    assert result is None
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    # Bug state: 403 is (wrongly) labeled provider unavailable.
    assert any("provider unavailable" in m for m in msgs), msgs


def test_openai_embed_already_typed_provider_unavailable_branch(monkeypatch):
    """Cover the FIRST `except EmbeddingProviderUnavailable` branch in embed().

    No current inner call raises the typed class directly (the SDK raises raw
    openai errors, which the broad branch maps), so that first handler is
    forward-defense. This test exercises it explicitly -- by making
    _build_client() raise EmbeddingProviderUnavailable -- so the "defensive"
    claim in the code comment is VERIFIED, not merely asserted (Principle #12),
    and the branch is not untested dead code. Asserts the typed-branch log AND
    the None return.
    """
    import logging

    from atomic_agents.embedding.openai import OpenAIEmbeddingBackend
    from atomic_agents.exceptions import EmbeddingProviderUnavailable

    fake_openai = _make_fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    backend = OpenAIEmbeddingBackend(api_key="sk-fake")

    def _raise_typed(self):
        raise EmbeddingProviderUnavailable("typed at inner call")

    monkeypatch.setattr(OpenAIEmbeddingBackend, "_build_client", _raise_typed)

    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed("x")
    assert result is None
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("provider unavailable" in m for m in msgs), msgs
    # The already-typed first branch fires -- it does NOT fall through to the
    # broad "unexpected error" branch.
    assert not any("unexpected error" in m for m in msgs), msgs


@requires_openai
def test_real_sdk_mro_includes_openai_error_root():
    """LIVE-SDK GUARD: pin the assumption the leaf-exact fix depends on -- that
    the real openai SDK roots its API-error hierarchy at ``OpenAIError`` and that
    4xx leaf classes inherit it. If a future SDK reshapes this, the test fakes'
    ``_SDK_4XX_MRO`` chain (and the classifier split) must be revisited.
    """
    import openai

    for name in ("BadRequestError", "NotFoundError", "UnprocessableEntityError"):
        cls = getattr(openai, name)
        mro_names = [c.__name__ for c in cls.__mro__]
        assert "OpenAIError" in mro_names, (name, mro_names)
        assert "APIStatusError" in mro_names, (name, mro_names)
    # The SDK root's own MRO is shallow (no API subclasses above it) -- this is
    # why matching it leaf-exact is safe for the no-key construction case.
    assert [c.__name__ for c in openai.OpenAIError.__mro__][:2] == [
        "OpenAIError",
        "Exception",
    ]


# ──────────────────────────────────────────────────────────────────────────────
# embed_batch() provider-unavailable short-circuit (no per-item amplification)


def test_embed_batch_provider_unavailable_no_amplification(monkeypatch):
    """A provider-availability failure on the batch call must short-circuit to
    [None]*len(texts) with EXACTLY ONE create() call -- no per-item retry storm
    (Principle #4 + #6). Reserves per-item degradation for partial-batch errors.
    """
    import logging

    call_count = {"n": 0}

    class RateLimitError(Exception):  # name matches the openai SDK availability type
        pass

    class _Embeddings:
        def create(self, **kwargs):
            call_count["n"] += 1
            raise RateLimitError("429 rate limited")

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _Client
    fake_openai.RateLimitError = RateLimitError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    backend = OpenAIEmbeddingBackend(api_key="sk-fake")
    caplog_logger = logging.getLogger("atomic_agents.embedding.openai")
    with patch.object(caplog_logger, "warning") as warn:
        result = backend.embed_batch(["a", "b", "c", "d"])

    assert result == [None, None, None, None]
    # Exactly ONE create() call: the batch call. No per-item amplification.
    assert call_count["n"] == 1, (
        f"provider-unavailable batch must NOT retry per-item; got {call_count['n']} calls"
    )
    msgs = [str(c.args[0]) for c in warn.call_args_list]
    assert any("provider unavailable" in m for m in msgs), msgs
    # Must NOT log the per-item degradation message.
    assert not any("degrading to per-item" in m for m in msgs), msgs


def test_embed_batch_generic_error_still_degrades_per_item(monkeypatch):
    """NEGATIVE CONTROL / parity: a NON-provider (generic) batch failure still
    degrades to per-item (the genuinely-recoverable partial-batch case). This
    proves the short-circuit above is gated on provider-availability, not on
    'any batch failure' (which would defeat the per-item recovery path).
    """
    call_count = {"n": 0}

    class _Embeddings:
        def create(self, *, input, model, **kwargs):
            call_count["n"] += 1
            # Fail only the multi-item batch; single-item calls succeed.
            if len(input) > 1:
                raise RuntimeError("generic non-provider batch failure")

            class _Item:
                index = 0
                embedding = [1.0, 2.0, 3.0, 4.0]

            class _Resp:
                data = [_Item()]

            return _Resp()

    class _Client:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _Embeddings()

        def close(self):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    # dimensions=4 so the per-item fake's 4-float vectors match the advertised
    # dimension (MUST-3 produced-length check).
    backend = OpenAIEmbeddingBackend(api_key="sk-fake", dimensions=4)
    result = backend.embed_batch(["a", "b", "c"])
    assert len(result) == 3
    assert all(v is not None for v in result)  # per-item fallback recovered them
    # 1 failed batch call + 3 successful per-item calls = 4
    assert call_count["n"] == 4, call_count["n"]


# ──────────────────────────────────────────────────────────────────────────────
# _normalize_length direct unit tests (MUST 9 enforcement -- finding #4)


def test_normalize_length_pads_when_short():
    """_normalize_length pads with None when the result is shorter than expected."""
    out = OpenAIEmbeddingBackend._normalize_length([[1.0], [2.0]], 4)
    assert out == [[1.0], [2.0], None, None]
    assert len(out) == 4


def test_normalize_length_truncates_when_long():
    """_normalize_length truncates when the result is longer than expected."""
    out = OpenAIEmbeddingBackend._normalize_length([[1.0], [2.0], [3.0]], 2)
    assert out == [[1.0], [2.0]]
    assert len(out) == 2


def test_normalize_length_passthrough_when_equal():
    """_normalize_length returns the list unchanged when already correct length."""
    inp = [[1.0], None, [3.0]]
    out = OpenAIEmbeddingBackend._normalize_length(inp, 3)
    assert out == inp


def test_normalize_length_negative_control():
    """NEGATIVE CONTROL: a no-op normalizer (the stripped-fix state) returns the
    short list unchanged, violating len(out)==expected. Proves the pad/truncate
    behavior above is load-bearing, not vacuous.
    """
    short = [[1.0], [2.0]]
    noop_result = short  # what a stripped _normalize_length would return
    assert len(noop_result) != 4  # the invariant violation the real helper fixes
    fixed = OpenAIEmbeddingBackend._normalize_length(short, 4)
    assert len(fixed) == 4  # the real helper enforces it


# ──────────────────────────────────────────────────────────────────────────────
# Integration test (real OpenAI API -- skips locally)
#
# CI must have ATOMIC_AGENTS_TEST_OPENAI_KEY configured in secrets.
# This test skips when the env var is absent.
# DO NOT assert exact float values -- assert shape (len, type) only.


@requires_openai
def test_openai_embed_integration():
    """Live integration: embed() returns a 1536-dim float vector for text-embedding-3-small.

    Asserts shape and type only -- not exact values (which change with model updates).
    This test runs in CI when ATOMIC_AGENTS_TEST_OPENAI_KEY is set.
    """
    api_key = os.environ["ATOMIC_AGENTS_TEST_OPENAI_KEY"]
    backend = OpenAIEmbeddingBackend(api_key=api_key)
    result = backend.embed("integration test text")
    assert result is not None
    assert isinstance(result, list)
    assert len(result) == 1536  # text-embedding-3-small full dimension
    assert all(isinstance(v, float) for v in result)


@requires_openai
def test_openai_embed_batch_integration():
    """Live integration: embed_batch() returns correct-length output of 1536-dim vectors."""
    api_key = os.environ["ATOMIC_AGENTS_TEST_OPENAI_KEY"]
    backend = OpenAIEmbeddingBackend(api_key=api_key)
    texts = ["first text", "second text", "third text"]
    result = backend.embed_batch(texts)
    assert len(result) == len(texts)
    for vec in result:
        assert vec is not None
        assert len(vec) == 1536
        assert all(isinstance(v, float) for v in vec)


# ──────────────────────────────────────────────────────────────────────────────
# _get_key() credential resolution — DELEGATES to the framework SecretBackend
#
# The embedding backend MUST route key resolution through the same SecretBackend
# (spec/38) as every other backend (via _llm._get_key), NOT a private local
# cascade. A private cascade would bypass an operator's ATOMIC_AGENTS_SECRET_BACKEND
# (e.g. GCP Secret Manager) -> embedding keys silently unresolved while LLM keys
# work -> semantic search silently broken on cloud deployments. These tests pin
# the delegation, the spec triple it forwards, and the graceful None-on-unresolved
# contract (construction must not fail when no key is configured).


def _clear_key_env(monkeypatch):
    for var in ("ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_get_key_delegates_to_secret_backend_resolver(monkeypatch):
    """_get_key forwards the OpenAI KeySpec triple to _llm._get_key (the canonical
    SecretBackend resolver) and returns its result verbatim.

    Negative control: if _get_key reverted to a private env/Keychain cascade, the
    recorder below would never be called and `captured` would stay empty.
    """
    from atomic_agents.embedding.openai import _get_key

    captured = {}

    def _fake_resolver(env_vars, keychain_name, config_key):
        captured["args"] = (list(env_vars), keychain_name, config_key)
        return "sk-from-secret-backend"

    # Patch the source symbol; _get_key does `from .._llm import _get_key` per call.
    monkeypatch.setattr("atomic_agents._llm._get_key", _fake_resolver)
    assert _get_key() == "sk-from-secret-backend"
    assert captured["args"] == (
        ["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"],
        "atomic-agents-openai",
        "openai",
    )


def test_get_key_resolves_through_real_filesystem_secret_backend(monkeypatch):
    """End-to-end proof the delegation actually routes through the registered
    SecretBackend: with the default FilesystemSecretBackend, an env-var key
    resolves. This is the path that was BROKEN by the old private cascade for a
    non-filesystem backend.
    """
    from atomic_agents.embedding.openai import _get_key

    _clear_key_env(monkeypatch)
    monkeypatch.setenv("ATOMIC_AGENTS_OPENAI_KEY", "sk-via-fs-backend")
    assert _get_key() == "sk-via-fs-backend"


def test_get_key_returns_none_when_resolver_raises(monkeypatch):
    """Graceful degradation: when no key is resolvable the resolver raises
    AtomicAgentsError; _get_key catches it and returns None so construction stays
    graceful and embed() degrades to the None-fallback (no key -> FTS fallback).

    Negative control: strip the `except AtomicAgentsError: return None` and this
    raises instead of returning None.
    """
    from atomic_agents.exceptions import AtomicAgentsError
    from atomic_agents.embedding.openai import _get_key

    def _raise_unresolved(env_vars, keychain_name, config_key):
        raise AtomicAgentsError("No API key found for openai")

    monkeypatch.setattr("atomic_agents._llm._get_key", _raise_unresolved)
    assert _get_key() is None


def test_get_key_propagates_backend_not_registered(monkeypatch):
    """SecretBackendNotRegistered is an OPERATOR MISCONFIG (e.g. pinned
    ATOMIC_AGENTS_SECRET_BACKEND=gcp without installing it), NOT a genuine
    'no key configured' miss. It MUST propagate (surface loudly), not be swallowed
    into the graceful-None path -- otherwise the misconfig masquerades as 'no key'
    and silently degrades semantic search. (Round-2 Codex finding.)

    Negative control: if the `except SecretBackendNotRegistered: raise` were
    removed, the broad `except AtomicAgentsError: return None` would swallow it
    and this pytest.raises would fail.
    """
    from atomic_agents.embedding.openai import _get_key
    from atomic_agents.secret_backend import SecretBackendNotRegistered

    def _raise_not_registered(env_vars, keychain_name, config_key):
        raise SecretBackendNotRegistered("backend 'gcp' not registered")

    monkeypatch.setattr("atomic_agents._llm._get_key", _raise_not_registered)
    with pytest.raises(SecretBackendNotRegistered):
        _get_key()

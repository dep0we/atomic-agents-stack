"""Unit tests for the EmbeddingBackend registry (spec/46 PR3, issue #200).

Tests cover:
- register / unregister / get / list round-trip (Option A registry: classes keyed by provider_id)
- BackendNotRegistered error on get of an unregistered id
- get_default_embedding_backend() env-var dispatch
- get_default_embedding_backend() graceful None return when no env var is set
- get_default_embedding_backend() SecretBackendNotRegistered re-raise
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from atomic_agents.embedding.registry import (
    get_default_embedding_backend,
    get_embedding_backend,
    list_embedding_backends,
    register_embedding_backend,
    unregister_embedding_backend,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


class _MinimalBackend:
    """Minimal fake backend class used as a registry value."""

    provider_id = "test-provider"
    model_id = "test-model-v1"
    dimensions = 4

    def __init__(self, **kwargs):
        pass

    def embed(self, text, *, input_type=None):
        return [0.0] * self.dimensions

    def embed_batch(self, texts, *, input_type=None):
        return [self.embed(t) for t in texts]

    def capabilities(self):
        from atomic_agents.embedding.backend import EmbeddingCapabilities

        return EmbeddingCapabilities(
            max_batch_size=512,
            max_input_tokens=8192,
            supports_input_type=False,
        )

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean_test_registry():
    """Ensure test-provider is removed before and after each test."""
    unregister_embedding_backend("test-provider")
    unregister_embedding_backend("test-provider-2")
    yield
    unregister_embedding_backend("test-provider")
    unregister_embedding_backend("test-provider-2")


# ──────────────────────────────────────────────────────────────────────────────
# register / get / list / unregister round-trip


def test_register_then_get_returns_class():
    """register_embedding_backend() + get_embedding_backend() round-trip."""
    register_embedding_backend("test-provider", _MinimalBackend)
    cls = get_embedding_backend("test-provider")
    assert cls is _MinimalBackend


def test_list_includes_registered_id():
    """list_embedding_backends() includes a freshly-registered id."""
    register_embedding_backend("test-provider", _MinimalBackend)
    ids = list_embedding_backends()
    assert "test-provider" in ids


def test_list_is_sorted():
    """list_embedding_backends() returns ids in lexicographic order."""
    register_embedding_backend("test-provider", _MinimalBackend)
    register_embedding_backend("test-provider-2", _MinimalBackend)
    ids = list_embedding_backends()
    # Filter to our test ids to avoid flaky assertions on global state
    test_ids = [i for i in ids if i.startswith("test-")]
    assert test_ids == sorted(test_ids)


def test_unregister_removes_id():
    """unregister_embedding_backend() removes the id from the registry."""
    register_embedding_backend("test-provider", _MinimalBackend)
    unregister_embedding_backend("test-provider")
    assert "test-provider" not in list_embedding_backends()


def test_unregister_noop_on_missing_id():
    """unregister_embedding_backend() is a no-op when id is not registered."""
    # Should not raise
    unregister_embedding_backend("test-provider")
    unregister_embedding_backend("test-provider")  # second call also safe


def test_get_raises_backend_not_registered():
    """get_embedding_backend() raises BackendNotRegistered for unknown id."""
    from atomic_agents.exceptions import BackendNotRegistered

    with pytest.raises(BackendNotRegistered):
        get_embedding_backend("test-provider")


def test_get_error_message_includes_known_ids():
    """BackendNotRegistered message includes currently-registered ids."""
    from atomic_agents.exceptions import BackendNotRegistered

    register_embedding_backend("test-provider", _MinimalBackend)
    with pytest.raises(BackendNotRegistered, match="test-provider"):
        get_embedding_backend("completely-unknown-id")


def test_re_register_replaces_class():
    """Registering the same provider_id twice replaces the binding."""

    class _NewBackend(_MinimalBackend):
        pass

    register_embedding_backend("test-provider", _MinimalBackend)
    register_embedding_backend("test-provider", _NewBackend)
    cls = get_embedding_backend("test-provider")
    assert cls is _NewBackend


# ──────────────────────────────────────────────────────────────────────────────
# get_default_embedding_backend() — no env var → returns None


def test_get_default_returns_none_when_openai_sdk_absent(monkeypatch):
    """get_default_embedding_backend() returns None when the openai SDK is not installed.

    When ATOMIC_AGENTS_EMBEDDING_BACKEND is unset, the factory defaults to
    'openai'. If the [openai] extra is absent, it returns None (graceful
    degradation → FTS fallback).  Simulate by removing the openai registry
    entry AND blocking the import so the lazy-register path also fails.
    """
    monkeypatch.delenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", raising=False)
    # Remove the openai backend from the registry to force the lazy-register path
    unregister_embedding_backend("openai")
    # Block the openai SDK import so lazy registration fails gracefully
    monkeypatch.setitem(sys.modules, "openai", None)  # type: ignore[arg-type]

    result = get_default_embedding_backend()
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# get_default_embedding_backend() — openai provider via env vars


def _make_fake_openai_module():
    fake = types.ModuleType("openai")

    class _FakeEmbeddings:
        def create(self, *, input, model, dimensions=4):
            # _Item is defined in create()'s function scope (NOT inside the _Resp
            # class body): a comprehension running in a class body cannot resolve
            # sibling class-level names, so an inner ``class _Item`` referenced by
            # ``[_Item(i) for i in ...]`` would raise NameError.  Defining it here
            # keeps it visible to the comprehension below.
            class _Item:
                def __init__(self, i):
                    self.index = i
                    self.embedding = [0.1] * dimensions

            class _Resp:
                data = [_Item(i) for i in range(len(input))]

            return _Resp()

    class _FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.embeddings = _FakeEmbeddings()

        def close(self):
            pass

    fake.OpenAI = _FakeClient
    return fake


def test_get_default_openai_from_env(monkeypatch):
    """get_default_embedding_backend() constructs OpenAIEmbeddingBackend when env says openai."""
    fake_openai = _make_fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_DIMENSIONS", "4")
    # Supply api_key via env (SecretBackend not configured in unit test)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    result = get_default_embedding_backend()
    assert result is not None
    assert result.provider_id == "openai"
    assert result.dimensions == 4
    result.close()


def test_get_default_openai_uses_custom_model(monkeypatch):
    """ATOMIC_AGENTS_EMBEDDING_MODEL is forwarded to the backend."""
    fake_openai = _make_fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_DIMENSIONS", "8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    result = get_default_embedding_backend()
    assert result is not None
    assert result.model_id == "text-embedding-3-large"
    assert result.dimensions == 8
    result.close()


# ──────────────────────────────────────────────────────────────────────────────
# get_default_embedding_backend() — registered custom provider via env


def test_get_default_uses_registered_custom_provider(monkeypatch):
    """get_default_embedding_backend() dispatches to a registered custom provider."""
    register_embedding_backend("test-provider", _MinimalBackend)
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "test-provider")
    monkeypatch.delenv("ATOMIC_AGENTS_EMBEDDING_URL", raising=False)

    result = get_default_embedding_backend()
    assert result is not None
    assert isinstance(result, _MinimalBackend)


def test_get_default_explicit_unknown_provider_raises(monkeypatch):
    """An EXPLICITLY-pinned unknown provider_id fails loud (not silent FTS).

    Operator-pinned misconfiguration must surface — a typo'd provider id
    silently disabling semantic search is the split-brain failure mode the
    factory guards against (matches the SecretBackendNotRegistered re-raise
    posture). The raised BackendNotRegistered carries the full known list.

    Negative control: if the explicit-pin re-raise is stripped (the lookup
    falls back to `return None`), this test flips from raises to None.
    """
    from atomic_agents.exceptions import BackendNotRegistered

    monkeypatch.setenv(
        "ATOMIC_AGENTS_EMBEDDING_BACKEND", "totally-unknown-provider-xyz"
    )

    with pytest.raises(BackendNotRegistered) as excinfo:
        get_default_embedding_backend()
    # The error names the provider list so the operator can fix the typo.
    assert "totally-unknown-provider-xyz" in str(excinfo.value)


def test_get_default_unset_provider_no_extra_returns_none(monkeypatch):
    """An UNSET provider env var degrades gracefully to None when no extra.

    Distinct from the explicit-typo case above: the implicit 'openai' default
    with the extra absent must NOT raise — it falls back to FTS quietly. This
    is the negative-control pair for the explicit-pin raise.
    """
    monkeypatch.delenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", raising=False)
    # Ensure 'openai' is not registered and the extra import fails.
    from atomic_agents.embedding import registry as _reg

    _reg.unregister_embedding_backend("openai")
    monkeypatch.setitem(sys.modules, "openai", None)

    result = get_default_embedding_backend()
    assert result is None


def test_get_default_explicit_openai_missing_extra_raises(monkeypatch):
    """An EXPLICITLY-pinned 'openai' with the [openai] extra absent fails loud.

    Parity with the explicit-unknown-provider raise: an operator who pinned
    ATOMIC_AGENTS_EMBEDDING_BACKEND=openai but never installed the extra opted
    into semantic search and must NOT silently get FTS with no error. Only the
    UNSET implicit-openai default degrades to None (the test just above).

    Negative control: revert the registry to `return None` in the explicit-pin
    branch of the openai lazy-register block and this flips from raises to None.
    """
    from atomic_agents.embedding import registry as _reg
    from atomic_agents.exceptions import AtomicAgentsError

    # The OpenAIEmbeddingBackend module imports the `openai` SDK lazily (inside
    # __init__, not at module level), so the lazy-register `from .openai import`
    # succeeds; the SDK-absent failure surfaces at CONSTRUCTION as an
    # AtomicAgentsError (MSG_NO_OPENAI_SDK).  With sys.modules['openai']=None the
    # construction `import openai` raises ImportError → wrapped AtomicAgentsError.
    _reg.unregister_embedding_backend("openai")
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")
    # No api_key path needed — construction fails on the SDK presence check first.

    # Accept either failure surface: ImportError (lazy-register block, if the
    # SDK were imported at module level) or AtomicAgentsError (construction-time
    # SDK presence check, which is the actual path today).  Both are "fail loud".
    with pytest.raises((AtomicAgentsError, ImportError)):
        get_default_embedding_backend()


# ──────────────────────────────────────────────────────────────────────────────
# get_default_embedding_backend() — SecretBackendNotRegistered re-raise


def test_get_default_reraises_secret_backend_not_registered(monkeypatch):
    """get_default_embedding_backend() re-raises SecretBackendNotRegistered.

    The SecretBackend is explicitly configured but not available — the caller
    must know so they can fix configuration rather than silently losing embeddings.
    """
    from atomic_agents.secret_backend.backend import SecretBackendNotRegistered

    fake_openai = _make_fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")
    # No OPENAI_API_KEY and no SecretBackend registered
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Patch _get_key to raise SecretBackendNotRegistered
    with patch(
        "atomic_agents.embedding.openai._get_key",
        side_effect=SecretBackendNotRegistered("no secret backend"),
    ):
        with pytest.raises(SecretBackendNotRegistered):
            get_default_embedding_backend()

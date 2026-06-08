"""Protocol conformance tests for SecretBackend (spec/38).

Backend-agnostic conformance tests that exercise the Protocol MUST clauses
against every registered SecretBackend implementation.

**How to add a new backend (PR 2+):**
1. Add the backend's string id to ``params`` in the ``backend`` fixture below.
2. Add an ``elif request.param == "<id>"`` branch that constructs the backend.
3. Add a matching branch to the ``force_absent`` fixture below that yields a
   context manager forcing a key to be ABSENT for that backend (filesystem
   patches its three sources; GCP would patch its client). Absence-dependent
   Protocol-contract tests use ``force_absent`` so they stay truthful for every
   backend.
4. Every test in THIS file then runs against all backends automatically.

**What belongs here vs test_secret_backend_filesystem.py:**

This file contains ONLY backend-agnostic Protocol contract tests — assertions
that MUST hold for every conforming SecretBackend, regardless of
implementation, expressed against the public Protocol surface
(``get`` / ``get_optional`` / ``has`` / ``locate`` / ``capabilities`` /
``backend_id`` / ``close``):
- Key charset validation at the public boundary (MUST 1)
- Capability advertisement: @property, frozen, consistent (MUST 3)
- SecretNotFound excludes value + names key (MUST 4)
- locate() excludes the value (MUST 5)
- Empty/whitespace/absent handling (MUST 7) via the ``force_absent`` fixture
- has() == (get_optional() is not None) agreement (MUST 8)
- Protocol isinstance, backend_id stable-and-nonempty, close() idempotent
- No-caching concurrency safety (MUST 9 behavior)

``test_secret_backend_filesystem.py`` contains filesystem-specific tests:
- resolve_with_spec() signature and behavior (NOT on the Protocol surface)
- persists_plaintext / supports_rotation / backend_id=="filesystem" values
- _KEYS_JSON_PATH machine-scoped assertions (FS-internal constant; MUST 2)
- source-label shape (``env:``/``keychain:``/``config:`` prefixes)
- the three-source cascade order

Do NOT add filesystem-specific tests here. In particular, do NOT:
- read ``filesystem._KEYS_JSON_PATH`` or any other backend-internal symbol,
- patch ``filesystem._resolve_from_*`` directly (use ``force_absent`` instead),
- assert a specific ``backend_id`` value or capability value,
- call ``resolve_with_spec`` (it is NOT part of the SecretBackend Protocol).
Any of those would break — or worse, silently pass vacuously — when
``params=["filesystem", "gcp"]`` is extended in PR 2.

Pattern mirrors test_corpus_protocol_conformance.py and
test_mcp_server_registry_conformance.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from atomic_agents.secret_backend import (
    FilesystemSecretBackend,
    SecretBackend,
    SecretCapabilities,
    SecretNotFound,
    SecretRef,
)
from atomic_agents.secret_backend.backend import _validate_key


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture(params=["filesystem"])
def backend(request) -> SecretBackend:
    """A fresh SecretBackend instance for the parametrized implementation.

    PR 2 extends params to ["filesystem", "gcp"] and adds an elif branch for
    GCPSecretManagerBackend; every conformance test then runs against both
    backends automatically.

    IMPORTANT: only add tests to this file that EVERY SecretBackend must pass,
    expressed against the public Protocol surface. Filesystem-specific tests
    belong in test_secret_backend_filesystem.py.
    """
    if request.param == "filesystem":
        return FilesystemSecretBackend()
    raise ValueError(f"Unknown backend param: {request.param}")


@pytest.fixture
def force_absent(request, monkeypatch):
    """Backend-agnostic way to force a key ABSENT for the active backend.

    Returns a callable ``force_absent(*keys)`` -> context manager. Inside the
    context, every named key resolves as absent for whichever backend the
    ``backend`` fixture produced. This keeps absence-dependent Protocol-contract
    tests (empty/whitespace/absent → SecretNotFound or None) truthful for every
    backend without reaching into any one backend's internals from the test
    body.

    PR 2: add a ``gcp`` branch that patches the GCP client so the named keys
    resolve as absent there too.
    """
    param = getattr(request, "param", None)
    # The companion ``backend`` fixture is parametrized; mirror its id here.
    # request.node.callspec carries the active param for the indirect set.
    if hasattr(request.node, "callspec"):
        param = request.node.callspec.params.get("backend", param)

    @contextmanager
    def _force_absent(*keys: str):
        for key in keys:
            monkeypatch.delenv(key, raising=False)
        if param == "filesystem":
            # Also delete the canonical Atomic Agents aliases for known keys so
            # Source 1 cannot resolve them, then suppress Sources 2 and 3.
            monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
            monkeypatch.delenv("ATOMIC_AGENTS_OPENAI_KEY", raising=False)
            monkeypatch.delenv("ATOMIC_AGENTS_MOONSHOT_KEY", raising=False)
            with (
                patch(
                    "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
                    return_value=None,
                ),
                patch(
                    "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
                    return_value=None,
                ),
            ):
                yield
        else:  # pragma: no cover - exercised once a second backend is added
            raise NotImplementedError(
                f"force_absent has no branch for backend param {param!r}; "
                f"add one alongside the new 'backend' fixture branch."
            )

    return _force_absent


@pytest.fixture
def force_other_sources_absent(request, monkeypatch):
    """Suppress every credential source EXCEPT the named keys' primary one.

    Returns a callable ``force_other_sources_absent(*keys)`` -> context manager.
    Unlike ``force_absent`` (which deletes the named keys so they are absent
    from ALL sources), this fixture leaves the named keys' live value in place
    and suppresses only the *secondary* sources. It is the fixture for MUST-7
    "empty/whitespace value at the primary source is treated as absent" tests:
    the empty value must survive to the backend so the empty-string/whitespace
    guard is the thing under test — not absence.

    For filesystem the primary source is the environment, so the named keys'
    env vars are left untouched while Keychain + keys.json are suppressed. (The
    canonical ATOMIC_AGENTS_* aliases ARE deleted so a stray real alias on the
    test runner cannot satisfy Source 1 ahead of the empty value under test.)

    PR 2: add a ``gcp`` branch that suppresses GCP's secondary lookups while
    leaving the primary client value the test set in place.
    """
    param = getattr(request, "param", None)
    if hasattr(request.node, "callspec"):
        param = request.node.callspec.params.get("backend", param)

    @contextmanager
    def _force_other_sources_absent(*keys: str):
        if param == "filesystem":
            # Delete the canonical aliases ONLY (never the named keys), so the
            # value the test set on the named key survives to be evaluated by
            # the empty-string/whitespace guard, while no real alias on the
            # runner can win Source 1 ahead of it. Suppress Sources 2 and 3.
            aliases = {
                "ATOMIC_AGENTS_ANTHROPIC_KEY",
                "ATOMIC_AGENTS_OPENAI_KEY",
                "ATOMIC_AGENTS_MOONSHOT_KEY",
            } - set(keys)
            for alias in aliases:
                monkeypatch.delenv(alias, raising=False)
            with (
                patch(
                    "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
                    return_value=None,
                ),
                patch(
                    "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
                    return_value=None,
                ),
            ):
                yield
        else:  # pragma: no cover - exercised once a second backend is added
            raise NotImplementedError(
                f"force_other_sources_absent has no branch for backend param "
                f"{param!r}; add one alongside the new 'backend' fixture branch."
            )

    return _force_other_sources_absent


# ─────────────────────────────────────────────────────────────────────────────
# MUST 1: Key charset validation (shared _validate_key helper + public boundary)


def test_validate_key_accepts_valid_names():
    """Valid POSIX env-var names pass without error (shared helper)."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FOO", "A1_B2_C3", "X"):
        _validate_key(key)  # must not raise


def test_validate_key_rejects_empty():
    with pytest.raises(ValueError, match="must not be empty"):
        _validate_key("")


def test_validate_key_rejects_lowercase():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("anthropic_api_key")


def test_validate_key_rejects_path_traversal():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("../etc/passwd")


def test_validate_key_rejects_slash():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("SOME/KEY")


def test_validate_key_rejects_dot():
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_key("MY.KEY")


def test_get_raises_value_error_on_invalid_key(backend):
    """Public boundary: get() raises ValueError before any backend access."""
    with pytest.raises(ValueError):
        backend.get("invalid.key")


def test_get_optional_raises_value_error_on_invalid_key(backend):
    with pytest.raises(ValueError):
        backend.get_optional("../etc/passwd")


def test_has_raises_value_error_on_invalid_key(backend):
    with pytest.raises(ValueError):
        backend.has("bad/key")


def test_locate_raises_value_error_on_invalid_key(backend):
    with pytest.raises(ValueError):
        backend.locate("lowercase_bad")


# ─────────────────────────────────────────────────────────────────────────────
# MUST 3: Capability honesty


def test_capabilities_is_property(backend):
    """capabilities MUST be a @property, not a plain method (spec/38 MUST 3)."""
    assert isinstance(type(backend).capabilities, property), (
        "backend.capabilities must be a @property — "
        "call sites use backend.capabilities.supports_rotation syntax"
    )


def test_capabilities_returns_frozen_dataclass(backend):
    """SecretCapabilities must be frozen=True."""
    caps = backend.capabilities
    assert isinstance(caps, SecretCapabilities)
    with pytest.raises(Exception):  # FrozenInstanceError
        caps.supports_rotation = False  # type: ignore[misc]


def test_capabilities_consistent_across_calls(backend):
    """Same instance returns same capabilities object (or equal one)."""
    caps1 = backend.capabilities
    caps2 = backend.capabilities
    assert caps1 == caps2


def test_capabilities_flags_are_bools(backend):
    """Every advertised capability is an actual bool (no None/sentinel leaks)."""
    caps = backend.capabilities
    assert isinstance(caps.supports_rotation, bool)
    assert isinstance(caps.supports_audit_logging, bool)
    assert isinstance(caps.persists_plaintext, bool)


# ─────────────────────────────────────────────────────────────────────────────
# MUST 4-5: No secret value in exceptions or SecretRef


def test_secret_not_found_message_excludes_value(backend, monkeypatch, force_absent):
    """SecretNotFound message MUST NOT contain the resolved value (MUST 4)."""
    # The value is set then removed so it exists nowhere; the test guards the
    # message-construction path against ever embedding a resolved value.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SHOULD_NOT_APPEAR")
    with force_absent("ANTHROPIC_API_KEY"):
        with pytest.raises(SecretNotFound) as exc_info:
            backend.get("ANTHROPIC_API_KEY")
    assert "SHOULD_NOT_APPEAR" not in str(exc_info.value)


def test_secret_not_found_message_names_key(backend, force_absent):
    """SecretNotFound message MUST name the key (for triage)."""
    with force_absent("MY_CUSTOM_KEY"):
        with pytest.raises(SecretNotFound) as exc_info:
            backend.get("MY_CUSTOM_KEY")
    assert "MY_CUSTOM_KEY" in str(exc_info.value)


def test_locate_source_excludes_value(backend, monkeypatch):
    """SecretRef.source MUST NOT contain the resolved value (MUST 5).

    The source-label *shape* (env:/keychain:/config: prefixes) is
    backend-specific and asserted in the filesystem test file; here we assert
    only the backend-agnostic contract: whatever the source label is, it never
    contains the secret value.
    """
    secret_value = "sk-ant-ultra-secret-value-12345"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret_value)
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert secret_value not in ref.source


# ─────────────────────────────────────────────────────────────────────────────
# MUST 7: Empty-string / whitespace / absent treated as absent


def test_empty_value_treated_as_absent(
    backend, monkeypatch, force_other_sources_absent
):
    """Empty-string value at the primary source must raise SecretNotFound.

    The empty value is left LIVE at the primary source (env for filesystem) and
    only the secondary sources are suppressed, so the empty-string guard — not
    plain absence — is the behavior under test (spec/38 MUST 7).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with force_other_sources_absent("ANTHROPIC_API_KEY"):
        with pytest.raises(SecretNotFound):
            backend.get("ANTHROPIC_API_KEY")


def test_whitespace_value_treated_as_absent(
    backend, monkeypatch, force_other_sources_absent
):
    """Whitespace-only value at the primary source must be treated as absent.

    The whitespace value is left LIVE at the primary source; only secondary
    sources are suppressed, so the whitespace-strip guard is the behavior under
    test (spec/38 MUST 7).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with force_other_sources_absent("ANTHROPIC_API_KEY"):
        with pytest.raises(SecretNotFound):
            backend.get("ANTHROPIC_API_KEY")


def test_get_optional_returns_none_for_absent(backend, force_absent):
    """get_optional returns None (not '') when key is absent."""
    with force_absent("MY_MISSING_KEY"):
        result = backend.get_optional("MY_MISSING_KEY")
    assert result is None


def test_empty_value_get_optional_returns_none(
    backend, monkeypatch, force_other_sources_absent
):
    """get_optional returns None for an empty-string value at the primary source.

    The empty value is left LIVE; only secondary sources are suppressed, so the
    empty-string guard (not absence) is the behavior under test (spec/38 MUST 7).
    """
    monkeypatch.setenv("MY_EMPTY_KEY", "")
    with force_other_sources_absent("MY_EMPTY_KEY"):
        result = backend.get_optional("MY_EMPTY_KEY")
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# MUST 8: has() delegates to / agrees with get_optional()


def test_has_agrees_with_get_optional_when_present(backend, monkeypatch):
    """has() MUST agree with get_optional() when present — split-brain prevention."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    assert backend.has("ANTHROPIC_API_KEY") is True
    assert (backend.get_optional("ANTHROPIC_API_KEY") is not None) is True


def test_has_agrees_with_get_optional_when_empty(
    backend, monkeypatch, force_other_sources_absent
):
    """has() and get_optional() agree (both absent) when value is ''.

    The empty value is left LIVE at the primary source; only secondary sources
    are suppressed, so the empty-string guard — not absence — drives both
    has() and get_optional() to agree on absence (spec/38 MUST 7 + MUST 8).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with force_other_sources_absent("ANTHROPIC_API_KEY"):
        assert backend.has("ANTHROPIC_API_KEY") is False
        assert backend.get_optional("ANTHROPIC_API_KEY") is None


def test_has_false_when_absent(backend, force_absent):
    with force_absent("NO_SUCH_KEY_EVER"):
        assert backend.has("NO_SUCH_KEY_EVER") is False


# ─────────────────────────────────────────────────────────────────────────────
# locate() behavior (MUST 5 secrecy + presence semantics)


def test_locate_returns_secret_ref_when_present(backend, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "some-key")
    ref = backend.locate("ANTHROPIC_API_KEY")
    assert ref is not None
    assert isinstance(ref, SecretRef)
    assert ref.key == "ANTHROPIC_API_KEY"
    assert ref.present is True


def test_locate_returns_none_when_absent(backend, force_absent):
    with force_absent("NO_SUCH_KEY_EVER"):
        ref = backend.locate("NO_SUCH_KEY_EVER")
    assert ref is None


# ─────────────────────────────────────────────────────────────────────────────
# MUST 9: No caching — concurrent calls are safe


def test_concurrent_get_calls_consistent(backend, monkeypatch):
    """get() called from multiple threads returns consistent results."""
    import threading

    monkeypatch.setenv("ANTHROPIC_API_KEY", "concurrent-test-key")
    results = []
    errors = []

    def call_get():
        try:
            val = backend.get("ANTHROPIC_API_KEY")
            results.append(val)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=call_get) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    assert all(v == "concurrent-test-key" for v in results)


# ─────────────────────────────────────────────────────────────────────────────
# Protocol isinstance check


def test_backend_is_instance_of_protocol(backend):
    """The backend satisfies the runtime-checkable SecretBackend Protocol."""
    assert isinstance(backend, SecretBackend)


# ─────────────────────────────────────────────────────────────────────────────
# backend_id stability (Protocol contract: stable + nonempty across calls).
# backend_id VALUE is implementation-specific; assert it in the impl test file.


def test_backend_id_stable(backend):
    """backend_id must return the same value on repeated calls (identity contract)."""
    assert backend.backend_id == backend.backend_id


def test_backend_id_is_nonempty_string(backend):
    """backend_id must be a non-empty string (Protocol contract)."""
    bid = backend.backend_id
    assert isinstance(bid, str) and len(bid) > 0


# ─────────────────────────────────────────────────────────────────────────────
# close() is idempotent


def test_close_is_idempotent(backend):
    backend.close()
    backend.close()  # must not raise

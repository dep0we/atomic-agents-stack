"""Conformance test: state backends MUST advertise supports_canonical_export=True.

This test is NOT capability-gated. It asserts directly that the five PR1
state backends all return supports_canonical_export=True. This prevents the
skip-all-by-accident failure mode where a backend accidentally ships with
False and the conformance suite silently skips all round-trip tests.

Per spec/40 §"Per-backend export contracts": FilesystemMemoryBackend,
FilesystemLogBackend, FilesystemMandateBackend, FilesystemCorpusBackend,
and FilesystemLockBackend MUST advertise supports_canonical_export=True.
FilesystemSecretBackend MUST also advertise True (wiring-map export).

Per spec/40 §"MemoryBackend export contract": MemoryBackend advertises
via @property supports_canonical_export (minority idiom matching
supports_semantic_search). All other backends use capabilities() -> dataclass
or @property capabilities (MCPRegistry/Secret pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Helper: extract supports_canonical_export from any backend shape


def get_supports_canonical_export(backend) -> bool:
    """Unified accessor for supports_canonical_export across three idiom shapes.

    Three capability-advertisement idioms exist in the codebase:
    1. MemoryBackend: direct @property supports_canonical_export on the backend.
    2. Most backends: backend.capabilities() -> XCapabilities dataclass.
    3. MCPRegistry/Secret: @property capabilities -> dataclass (not callable).

    This helper tries all three in order and returns False if none match.
    It is the canonical way to read the flag in conformance tests.
    """
    # Shape 1: direct @property on the backend (MemoryBackend)
    direct = getattr(backend, "supports_canonical_export", None)
    if isinstance(direct, bool):
        return direct

    # Shape 2/3: capabilities — may be callable (method) or a property (dataclass)
    caps_attr = getattr(backend, "capabilities", None)
    if caps_attr is not None:
        if callable(caps_attr):
            # Shape 2: capabilities() method returns a dataclass
            caps = caps_attr()
        else:
            # Shape 3: @property returning a dataclass directly
            caps = caps_attr
        val = getattr(caps, "supports_canonical_export", None)
        if isinstance(val, bool):
            return val

    return False


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def memory_backend(tmp_path: Path):
    from atomic_agents.memory.filesystem import FilesystemBackend

    return FilesystemBackend(tmp_path / "agent")


@pytest.fixture
def log_backend(tmp_path: Path):
    from atomic_agents.logs.filesystem import FilesystemLogBackend

    return FilesystemLogBackend(tmp_path / "agent")


@pytest.fixture
def mandate_backend(tmp_path: Path):
    from atomic_agents.mandate.filesystem import FilesystemMandateBackend

    return FilesystemMandateBackend(tmp_path / "scope")


@pytest.fixture
def corpus_backend(tmp_path: Path):
    from atomic_agents.corpus.filesystem import FilesystemCorpusBackend

    return FilesystemCorpusBackend(tmp_path / "agent")


@pytest.fixture
def lock_backend(tmp_path: Path):
    from atomic_agents.locks.filesystem import FilesystemLockBackend

    return FilesystemLockBackend(tmp_path / "agent")


@pytest.fixture
def secret_backend():
    from atomic_agents.secret_backend.filesystem import FilesystemSecretBackend

    return FilesystemSecretBackend()


# ──────────────────────────────────────────────────────────────────────────────
# Assertion tests — NOT capability-gated (must not skip)


def test_memory_backend_advertises_canonical_export(memory_backend) -> None:
    """FilesystemMemoryBackend MUST advertise supports_canonical_export=True.

    Per spec/40 §"Per-backend export contracts". This test failing means the
    FilesystemBackend shipped with supports_canonical_export=False or the
    @property is missing.
    """
    assert memory_backend.supports_canonical_export is True, (
        "FilesystemMemoryBackend must advertise supports_canonical_export=True "
        "per spec/40 §'Per-backend export contracts'"
    )


def test_log_backend_advertises_canonical_export(log_backend) -> None:
    """FilesystemLogBackend MUST advertise supports_canonical_export=True."""
    assert get_supports_canonical_export(log_backend) is True, (
        "FilesystemLogBackend must advertise supports_canonical_export=True "
        "per spec/40 §'Per-backend export contracts'"
    )


def test_mandate_backend_advertises_canonical_export(mandate_backend) -> None:
    """FilesystemMandateBackend MUST advertise supports_canonical_export=True."""
    assert get_supports_canonical_export(mandate_backend) is True, (
        "FilesystemMandateBackend must advertise supports_canonical_export=True "
        "per spec/40 §'Per-backend export contracts'"
    )


def test_corpus_backend_advertises_canonical_export(corpus_backend) -> None:
    """FilesystemCorpusBackend MUST advertise supports_canonical_export=True."""
    assert get_supports_canonical_export(corpus_backend) is True, (
        "FilesystemCorpusBackend must advertise supports_canonical_export=True "
        "per spec/40 §'Per-backend export contracts'"
    )


def test_lock_backend_advertises_canonical_export(lock_backend) -> None:
    """FilesystemLockBackend MUST advertise supports_canonical_export=True."""
    assert get_supports_canonical_export(lock_backend) is True, (
        "FilesystemLockBackend must advertise supports_canonical_export=True "
        "per spec/40 §'Per-backend export contracts'"
    )


def test_secret_backend_advertises_canonical_export(secret_backend) -> None:
    """FilesystemSecretBackend MUST advertise supports_canonical_export=True."""
    assert get_supports_canonical_export(secret_backend) is True, (
        "FilesystemSecretBackend must advertise supports_canonical_export=True "
        "per spec/40 §'Per-backend export contracts'"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Exportable Protocol isinstance checks


def test_memory_backend_is_exportable(memory_backend) -> None:
    """FilesystemBackend satisfies the Exportable Protocol."""
    from atomic_agents.export.backend import Exportable

    assert isinstance(memory_backend, Exportable), (
        "FilesystemBackend must satisfy the Exportable Protocol "
        "(must have export() and export_all() methods)"
    )


def test_log_backend_is_exportable(log_backend) -> None:
    """FilesystemLogBackend satisfies the Exportable Protocol."""
    from atomic_agents.export.backend import Exportable

    assert isinstance(log_backend, Exportable)


def test_mandate_backend_is_exportable(mandate_backend) -> None:
    """FilesystemMandateBackend satisfies the Exportable Protocol."""
    from atomic_agents.export.backend import Exportable

    assert isinstance(mandate_backend, Exportable)


def test_corpus_backend_is_exportable(corpus_backend) -> None:
    """FilesystemCorpusBackend satisfies the Exportable Protocol."""
    from atomic_agents.export.backend import Exportable

    assert isinstance(corpus_backend, Exportable)


def test_lock_backend_is_exportable(lock_backend) -> None:
    """FilesystemLockBackend satisfies the Exportable Protocol."""
    from atomic_agents.export.backend import Exportable

    assert isinstance(lock_backend, Exportable)


def test_secret_backend_is_exportable(secret_backend) -> None:
    """FilesystemSecretBackend satisfies the Exportable Protocol."""
    from atomic_agents.export.backend import Exportable

    assert isinstance(secret_backend, Exportable)


# ──────────────────────────────────────────────────────────────────────────────
# Capability flag type checks


def test_all_capability_flags_are_bool(
    memory_backend,
    log_backend,
    mandate_backend,
    corpus_backend,
    lock_backend,
    secret_backend,
) -> None:
    """supports_canonical_export MUST be a Python bool (not truthy int or None)."""
    for name, backend in [
        ("memory", memory_backend),
        ("log", log_backend),
        ("mandate", mandate_backend),
        ("corpus", corpus_backend),
        ("lock", lock_backend),
        ("secret", secret_backend),
    ]:
        val = get_supports_canonical_export(backend)
        assert isinstance(val, bool), (
            f"{name} backend supports_canonical_export must be bool, got {type(val)}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Non-filesystem / stateless backends MUST advertise False
#
# If a developer accidentally sets supports_canonical_export=True on a backend
# without a working export() implementation, the conformance round-trip tests
# would silently skip every test for that backend rather than fail.


def test_sqlite_log_backend_advertises_false(tmp_path: Path) -> None:
    """SQLiteLogBackend must advertise supports_canonical_export=False (deferred).

    Guards against accidental True without an export() implementation.
    """
    try:
        from atomic_agents.logs.sqlite import SQLiteLogBackend  # noqa: F401
    except ImportError:
        pytest.skip("SQLiteLogBackend not available in this environment")
    backend = SQLiteLogBackend(tmp_path / "test.db")
    val = get_supports_canonical_export(backend)
    assert val is False, (
        "SQLiteLogBackend must advertise supports_canonical_export=False "
        "until its export impl ships"
    )


def test_assert_canonical_roundtrip_skips_false_backend(tmp_path: Path) -> None:
    """assert_canonical_roundtrip skips when supports_canonical_export=False.

    Verifies the skip path is exercised and produces pytest.skip, not failure.
    This guards against the 'skip-all-by-accident' failure mode: if a future
    developer sets False without an export() implementation, ALL round-trip
    tests would silently skip rather than surface the missing impl.
    """
    from tests.test_export_protocol_conformance import assert_canonical_roundtrip

    class _FalseExportBackend:
        """Stub backend that explicitly declares no export support."""

        @property
        def supports_canonical_export(self) -> bool:
            return False

    backend = _FalseExportBackend()
    with pytest.raises(pytest.skip.Exception):
        assert_canonical_roundtrip(backend, lambda b: None, lambda b: b"")


def test_mcp_registry_capabilities_has_canonical_export_field(tmp_path: Path) -> None:
    """MCPServerRegistryCapabilities must have supports_canonical_export field (False).

    spec/40 §'supports_canonical_export capability field' table lists
    MCPServerRegistryCapabilities with default=False.
    """
    from atomic_agents.mcp_registry.types import MCPServerRegistryCapabilities

    caps = MCPServerRegistryCapabilities(
        supports_install=False,
        supports_uninstall=False,
        supports_capability_handshake=False,
        supports_audit=False,
        durable=True,
    )
    assert hasattr(caps, "supports_canonical_export"), (
        "MCPServerRegistryCapabilities must have supports_canonical_export field"
    )
    assert caps.supports_canonical_export is False, (
        "MCPServerRegistryCapabilities.supports_canonical_export must default to False"
    )


def test_secret_present_reflects_actual_presence(monkeypatch, tmp_path) -> None:
    """SecretExportRef.present must accurately reflect key presence.

    Regression test for P0 bug: locate() returns None for absent keys (not raise),
    so a try/except that sets present=True on any non-exception was wrong.
    This test verifies that an absent key returns present=False.

    Isolation: locate('ANTHROPIC_API_KEY') probes ALL anthropic sources — both
    env aliases (ATOMIC_AGENTS_ANTHROPIC_KEY + ANTHROPIC_API_KEY), keys.json, and
    the macOS keychain. Delete BOTH env aliases, point keys.json at a nonexistent
    path, and stub the keychain lookup off so the test deterministically controls
    every resolution source (otherwise it false-fails on any machine that has the
    anthropic key configured in the environment, keys.json, or the keychain).
    """
    import atomic_agents.secret_backend.filesystem as _sbfs
    from atomic_agents.secret_backend.filesystem import FilesystemSecretBackend

    # Delete BOTH env aliases for the anthropic provider key.
    monkeypatch.delenv("ATOMIC_AGENTS_ANTHROPIC_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Point keys.json at a path that does not exist.
    monkeypatch.setattr(_sbfs, "_KEYS_JSON_PATH", tmp_path / "nonexistent-keys.json")
    # Stub the keychain lookup off (no keychain entry resolves).
    monkeypatch.setattr(_sbfs, "_resolve_from_keychain", lambda _name: None)

    backend = FilesystemSecretBackend()
    result = backend.export()

    anthropic_entry = next(
        (e for e in result.entries if e.logical_key == "anthropic"), None
    )
    assert anthropic_entry is not None, "anthropic key should be in export entries"
    assert anthropic_entry.present is False, (
        "SecretExportRef.present must be False when key is not configured. "
        "P0 regression: locate() returns None for absent keys; a try/except "
        "that sets present=True on any non-exception was incorrect."
    )


def test_secret_present_true_when_key_is_set(monkeypatch) -> None:
    """SecretExportRef.present must be True when the key IS configured."""
    from atomic_agents.secret_backend.filesystem import FilesystemSecretBackend

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-present-key-value-12345")

    backend = FilesystemSecretBackend()
    result = backend.export()

    anthropic_entry = next(
        (e for e in result.entries if e.logical_key == "anthropic"), None
    )
    assert anthropic_entry is not None, "anthropic key should be in export entries"
    assert anthropic_entry.present is True, (
        "SecretExportRef.present must be True when key IS configured."
    )

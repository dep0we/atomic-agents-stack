"""Conformance test suite for the PersonaBackend Protocol (spec/33).

Parametrized over a ``backend_factory`` fixture that constructs both the
``FilesystemPersonaBackend`` (the filesystem reference implementation) and an
in-memory ``MockPersonaBackend`` registered under ``"mock"`` for the duration
of each test. Every conformance test runs against BOTH backends so the
Protocol contract is verified independently of the storage substrate.

Fixture lifecycle (D9 fold #3 pattern from Policy arc):
- ``mock_registered`` fixture registers ``MockPersonaBackend`` under ``"mock"``
  in setup and unregisters in teardown via ``unregister_persona_backend("mock")``
  to prevent cross-test registry pollution.

What this suite covers (~45 functions x 2 backends):

1.  Construction is side-effect-free (spec/33 MUST #2).
2.  ``load_persona`` happy path: save then load round-trips all fields.
3.  ``load_persona`` unknown id raises ``PersonaNotFound``.
4.  ``load_persona`` error message names the persona_id.
5.  ``save_persona`` new persona persists and is readable.
6.  ``save_persona`` existing id raises ``PersonaExists`` by default.
7.  ``save_persona`` error message names the persona_id.
8.  ``save_persona`` ``overwrite=True`` replaces existing persona.
9.  ``list_personas`` empty backend returns ``[]``.
10. ``list_personas`` populated backend returns all ids.
11. ``list_personas`` return is sorted.
12. ``exists`` returns True for present persona.
13. ``exists`` returns False for absent persona.
14. ``exists`` raises ``ValueError`` for charset-invalid persona_id.
15. ``clone`` copies source fields to target.
16. ``clone`` ``overrides`` dict applies field-by-field.
17. ``clone`` preserves source's ``created_at`` (no new timestamp generated).
18. ``clone`` source unknown raises ``PersonaNotFound``.
19. ``clone`` target exists raises ``PersonaExists``.
20. ``snapshot`` raises ``NotImplementedError`` when ``supports_snapshot=False``.
21. ``restore`` raises ``NotImplementedError`` when ``supports_snapshot=False``.
22. ``list_snapshots`` raises ``NotImplementedError`` when ``supports_snapshot=False``.
23. ``capabilities`` returns ``PersonaCapabilities`` instance.
24. ``capabilities`` is stable across calls.
25. ``capabilities`` has all 6 boolean fields.
26. ``backend_id`` is a non-empty string.
27. ``backend_id`` is stable across calls.
28. ``backend_id`` for filesystem backend is ``"filesystem"``.
29. ``backend_id`` for mock backend is ``"mock"``.

Charset/security tests (filesystem-only, separate section):
30-36. path-traversal, control chars, newlines, leading dot, empty string -- all raise ``ValueError``.

Registry primitive tests (not parametrized):
37-44. register, unregister, get, unknown id, duplicate, default factory env var.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from atomic_agents.persona.types import PersonaSnapshot

from atomic_agents.exceptions import BackendNotRegistered
from atomic_agents.persona.backend import (
    get_default_persona_backend,
    get_persona_backend,
    list_persona_backends,
    register_persona_backend,
    unregister_persona_backend,
)
from atomic_agents.persona.types import (
    Persona,
    PersonaCapabilities,
)

# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers -- intentional duplication from MockPersonaBackend so mock
# is self-contained and does NOT import from the implementation under test.

_PERSONA_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.+@-]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _validate_persona_id_for_mock(persona_id: str) -> None:
    """Raise ``ValueError`` for any persona_id that violates spec/33 MUST #1."""
    if not isinstance(persona_id, str) or not persona_id:
        raise ValueError(f"persona_id must be a non-empty string; got {persona_id!r}")
    if persona_id.startswith("."):
        raise ValueError(f"persona_id must not start with '.'; got {persona_id!r}")
    if ".." in persona_id or "/" in persona_id or "\\" in persona_id:
        raise ValueError(f"persona_id has path-traversal token; got {persona_id!r}")
    if _CONTROL_CHARS.search(persona_id):
        raise ValueError(f"persona_id has control/newline char; got {persona_id!r}")
    if not _PERSONA_ID_PATTERN.match(persona_id):
        raise ValueError(f"persona_id must match [a-zA-Z0-9_.+@-]+; got {persona_id!r}")


# ─────────────────────────────────────────────────────────────────────────────
# MockPersonaBackend -- in-memory PersonaBackend for conformance testing only.


class MockPersonaBackend:
    """In-memory ``PersonaBackend`` for conformance testing.

    Registered under ``"mock"`` via the ``mock_registered`` pytest fixture and
    unregistered in teardown (D9 fold #3 registry hygiene). Pure dict storage
    with no I/O. Verifies the Protocol contract works against a non-filesystem
    backend so tests don't accidentally depend on filesystem semantics.
    """

    backend_id = "mock"

    def __init__(self) -> None:
        self._store: dict[str, Persona] = {}

    def load_persona(self, persona_id: str) -> Persona:
        from atomic_agents.exceptions import PersonaNotFound

        _validate_persona_id_for_mock(persona_id)
        if persona_id not in self._store:
            raise PersonaNotFound(f"Persona {persona_id!r} not found in mock backend.")
        return self._store[persona_id]

    def save_persona(
        self, persona_id: str, persona: Persona, *, overwrite: bool = False
    ) -> None:
        from atomic_agents.exceptions import PersonaExists

        _validate_persona_id_for_mock(persona_id)
        if not overwrite and persona_id in self._store:
            raise PersonaExists(
                f"Persona {persona_id!r} already exists in mock backend. "
                f"Pass overwrite=True to replace it."
            )
        self._store[persona_id] = persona

    def list_personas(self) -> list[str]:
        return sorted(self._store.keys())

    def exists(self, persona_id: str) -> bool:
        _validate_persona_id_for_mock(persona_id)
        return persona_id in self._store

    def clone(
        self,
        source_id: str,
        target_id: str,
        overrides: dict | None = None,
    ) -> None:
        import dataclasses

        _validate_persona_id_for_mock(source_id)
        _validate_persona_id_for_mock(target_id)
        source = self.load_persona(source_id)
        if overrides:
            source = dataclasses.replace(source, **overrides)
        self.save_persona(target_id, source, overwrite=False)

    def snapshot(self, persona_id: str, label: str | None = None) -> str:
        _validate_persona_id_for_mock(persona_id)
        raise NotImplementedError(
            "MockPersonaBackend.snapshot() is not implemented "
            "(capabilities().supports_snapshot=False)."
        )

    def restore(self, persona_id: str, snapshot_id: str) -> None:
        _validate_persona_id_for_mock(persona_id)
        raise NotImplementedError(
            "MockPersonaBackend.restore() is not implemented "
            "(capabilities().supports_snapshot=False)."
        )

    def list_snapshots(self, persona_id: str) -> list:
        _validate_persona_id_for_mock(persona_id)
        raise NotImplementedError(
            "MockPersonaBackend.list_snapshots() is not implemented "
            "(capabilities().supports_snapshot=False)."
        )

    def capabilities(self) -> PersonaCapabilities:
        return PersonaCapabilities(
            supports_save=True,
            supports_clone=True,
            supports_snapshot=False,
            supports_subscribe=False,
            durable=False,
            supports_templates=False,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def mock_registered():
    """Register ``MockPersonaBackend`` and unregister in teardown (D9 hygiene)."""
    register_persona_backend("mock", MockPersonaBackend)
    try:
        yield
    finally:
        unregister_persona_backend("mock")


@pytest.fixture(params=["filesystem", "mock"])
def backend_factory(request, tmp_path, mock_registered):  # noqa: ARG001
    """Yield a context-manager factory for the parametrized backend.

    Tests call it as::

        with backend_factory() as backend:
            ...
    """
    backend_id = request.param

    @contextmanager
    def factory() -> Iterator:
        if backend_id == "filesystem":
            from atomic_agents.persona.filesystem import FilesystemPersonaBackend

            personas_root = tmp_path / f"personas-{request.node.name[:32]}"
            personas_root.mkdir(exist_ok=True)
            yield FilesystemPersonaBackend(personas_root)
        else:
            yield MockPersonaBackend()

    yield factory


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a Persona with reasonable defaults


def _make_persona(
    identity: str = "You are a helpful assistant.",
    soul: str = "Curious, direct, honest.",
    user: str = "User is a developer.",
    version: int = 1,
    created_at: str = "2026-05-26T12:00:00Z",
    label: str | None = None,
) -> Persona:
    return Persona(
        identity=identity,
        soul=soul,
        user=user,
        version=version,
        created_at=created_at,
        label=label,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 -- Construction is side-effect-free (spec/33 MUST #2)


def test_construction_is_side_effect_free(tmp_path: Path) -> None:
    """``FilesystemPersonaBackend(non_existent_path)`` succeeds.

    The backend MUST NOT stat, open, or mkdir during construction (spec/33
    MUST #2). Filesystem-only because MockPersonaBackend trivially satisfies
    the property (no I/O at all).
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    non_existent = tmp_path / "does-not-exist-yet"
    assert not non_existent.exists()
    backend = FilesystemPersonaBackend(non_existent)
    assert backend is not None


# ─────────────────────────────────────────────────────────────────────────────
# Tests 2-4 -- load_persona


def test_load_persona_round_trips_all_fields(backend_factory) -> None:
    """Save then load round-trips all Persona fields exactly."""
    persona = _make_persona(
        identity="Identity body.",
        soul="Soul body.",
        user="User context.",
        version=3,
        created_at="2026-01-01T00:00:00Z",
        label="test-label",
    )
    with backend_factory() as backend:
        backend.save_persona("my-persona", persona)
        loaded = backend.load_persona("my-persona")

    assert loaded.identity == "Identity body."
    assert loaded.soul == "Soul body."
    assert loaded.user == "User context."
    assert loaded.version == 3
    assert loaded.created_at == "2026-01-01T00:00:00Z"
    assert loaded.label == "test-label"


def test_load_persona_unknown_id_raises_PersonaNotFound(backend_factory) -> None:
    """``load_persona`` with an unknown persona_id raises ``PersonaNotFound``."""
    from atomic_agents.exceptions import PersonaNotFound

    with backend_factory() as backend:
        with pytest.raises(PersonaNotFound):
            backend.load_persona("does-not-exist")


def test_load_persona_error_message_names_persona_id(backend_factory) -> None:
    """The ``PersonaNotFound`` error message names the missing persona_id."""
    from atomic_agents.exceptions import PersonaNotFound

    with backend_factory() as backend:
        with pytest.raises(PersonaNotFound, match="does-not-exist"):
            backend.load_persona("does-not-exist")


# ─────────────────────────────────────────────────────────────────────────────
# Tests 5-8 -- save_persona


def test_save_persona_persists_and_is_readable(backend_factory) -> None:
    """A newly saved persona is readable via ``load_persona``."""
    persona = _make_persona()
    with backend_factory() as backend:
        backend.save_persona("new-persona", persona)
        assert backend.exists("new-persona")
        loaded = backend.load_persona("new-persona")
    assert loaded.identity == persona.identity


def test_save_persona_existing_id_raises_PersonaExists(backend_factory) -> None:
    """Saving to an existing persona_id without ``overwrite=True`` raises
    ``PersonaExists``."""
    from atomic_agents.exceptions import PersonaExists

    persona = _make_persona()
    with backend_factory() as backend:
        backend.save_persona("existing", persona)
        with pytest.raises(PersonaExists):
            backend.save_persona("existing", persona)


def test_save_persona_error_message_names_persona_id(backend_factory) -> None:
    """The ``PersonaExists`` error message names the conflicting persona_id."""
    from atomic_agents.exceptions import PersonaExists

    persona = _make_persona()
    with backend_factory() as backend:
        backend.save_persona("my-id", persona)
        with pytest.raises(PersonaExists, match="my-id"):
            backend.save_persona("my-id", persona)


def test_save_persona_overwrite_replaces_existing(backend_factory) -> None:
    """``save_persona(..., overwrite=True)`` replaces the existing record;
    subsequent ``load_persona`` returns the new persona."""
    original = _make_persona(identity="Original identity.", version=1)
    updated = _make_persona(identity="Updated identity.", version=2)

    with backend_factory() as backend:
        backend.save_persona("my-persona", original)
        backend.save_persona("my-persona", updated, overwrite=True)
        loaded = backend.load_persona("my-persona")

    assert loaded.identity == "Updated identity."
    assert loaded.version == 2


# ─────────────────────────────────────────────────────────────────────────────
# Tests 9-11 -- list_personas


def test_list_personas_empty_backend_returns_empty_list(backend_factory) -> None:
    """Empty backend returns ``[]`` from ``list_personas``."""
    with backend_factory() as backend:
        result = backend.list_personas()
    assert result == []


def test_list_personas_populated_backend_returns_all_ids(backend_factory) -> None:
    """Populated backend returns all persona_ids."""
    persona = _make_persona()
    with backend_factory() as backend:
        backend.save_persona("alpha", persona)
        backend.save_persona("beta", persona)
        backend.save_persona("gamma", persona)
        result = backend.list_personas()

    assert set(result) == {"alpha", "beta", "gamma"}


def test_list_personas_returns_sorted_list(backend_factory) -> None:
    """``list_personas`` returns a sorted list."""
    persona = _make_persona()
    with backend_factory() as backend:
        backend.save_persona("zebra", persona)
        backend.save_persona("apple", persona)
        backend.save_persona("mango", persona)
        result = backend.list_personas()

    assert result == sorted(result)
    assert result == ["apple", "mango", "zebra"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests 12-14 -- exists


def test_exists_returns_true_for_present_persona(backend_factory) -> None:
    """``exists`` returns True when the persona is known."""
    persona = _make_persona()
    with backend_factory() as backend:
        backend.save_persona("present", persona)
        result = backend.exists("present")
    assert result is True


def test_exists_returns_false_for_absent_persona(backend_factory) -> None:
    """``exists`` returns False when the persona is not known."""
    with backend_factory() as backend:
        result = backend.exists("absent")
    assert result is False


def test_exists_raises_ValueError_for_invalid_persona_id(backend_factory) -> None:
    """``exists`` raises ``ValueError`` for a charset-invalid persona_id.

    The charset check happens BEFORE any storage access -- it does not
    return False for invalid ids.
    """
    with backend_factory() as backend:
        with pytest.raises(ValueError):
            backend.exists("../etc/passwd")


# ─────────────────────────────────────────────────────────────────────────────
# Tests 15-19 -- clone


def test_clone_copies_source_fields_to_target(backend_factory) -> None:
    """Cloning copies all source Persona fields to the target."""
    source = _make_persona(identity="Source identity.", soul="Source soul.", version=5)
    with backend_factory() as backend:
        backend.save_persona("source", source)
        backend.clone("source", "target")
        cloned = backend.load_persona("target")

    assert cloned.identity == "Source identity."
    assert cloned.soul == "Source soul."
    assert cloned.version == 5


def test_clone_overrides_apply_field_by_field(backend_factory) -> None:
    """``overrides`` dict applies on top of the copied fields."""
    source = _make_persona(identity="Original.", label="v1")
    with backend_factory() as backend:
        backend.save_persona("src", source)
        backend.clone("src", "dst", overrides={"label": "v2"})
        cloned = backend.load_persona("dst")

    assert cloned.label == "v2"
    assert cloned.identity == "Original."  # non-overridden field preserved


def test_clone_preserves_source_created_at(backend_factory) -> None:
    """Clone copies the source's ``created_at`` -- no new timestamp generated.

    The implementation uses ``dataclasses.replace(source, **overrides)`` and
    saves the result directly. Only an explicit ``overrides={"created_at": ...}``
    produces a different ``created_at`` on the target.
    """
    source = _make_persona(created_at="2026-01-01T00:00:00Z")
    with backend_factory() as backend:
        backend.save_persona("src", source)
        backend.clone("src", "dst")
        cloned = backend.load_persona("dst")

    assert cloned.created_at == "2026-01-01T00:00:00Z"


def test_clone_source_unknown_raises_PersonaNotFound(backend_factory) -> None:
    """Cloning from an unknown source raises ``PersonaNotFound``."""
    from atomic_agents.exceptions import PersonaNotFound

    with backend_factory() as backend:
        with pytest.raises(PersonaNotFound):
            backend.clone("no-such-source", "target")


def test_clone_target_exists_raises_PersonaExists(backend_factory) -> None:
    """Cloning to an existing target id raises ``PersonaExists``."""
    from atomic_agents.exceptions import PersonaExists

    persona = _make_persona()
    with backend_factory() as backend:
        backend.save_persona("source", persona)
        backend.save_persona("target", persona)
        with pytest.raises(PersonaExists):
            backend.clone("source", "target")


# ─────────────────────────────────────────────────────────────────────────────
# Tests 20-22 -- snapshot trio (capability-gated; supports_snapshot=False in PR 1)


def test_snapshot_raises_NotImplementedError_when_not_supported(
    backend_factory,
) -> None:
    """When ``capabilities().supports_snapshot is False``, ``snapshot()``
    raises ``NotImplementedError``."""
    persona = _make_persona()
    with backend_factory() as backend:
        if backend.capabilities().supports_snapshot:
            pytest.skip("backend supports snapshot -- skip NotImplementedError test")
        backend.save_persona("my-persona", persona)
        with pytest.raises(NotImplementedError):
            backend.snapshot("my-persona")


def test_restore_raises_NotImplementedError_when_not_supported(
    backend_factory,
) -> None:
    """When ``capabilities().supports_snapshot is False``, ``restore()``
    raises ``NotImplementedError``."""
    persona = _make_persona()
    with backend_factory() as backend:
        if backend.capabilities().supports_snapshot:
            pytest.skip("backend supports snapshot -- skip NotImplementedError test")
        backend.save_persona("my-persona", persona)
        with pytest.raises(NotImplementedError):
            backend.restore("my-persona", "some-snapshot-id")


def test_list_snapshots_raises_NotImplementedError_when_not_supported(
    backend_factory,
) -> None:
    """When ``capabilities().supports_snapshot is False``, ``list_snapshots()``
    raises ``NotImplementedError``."""
    persona = _make_persona()
    with backend_factory() as backend:
        if backend.capabilities().supports_snapshot:
            pytest.skip("backend supports snapshot -- skip NotImplementedError test")
        backend.save_persona("my-persona", persona)
        with pytest.raises(NotImplementedError):
            backend.list_snapshots("my-persona")


# ─────────────────────────────────────────────────────────────────────────────
# Tests 23-25 -- capabilities


def test_capabilities_returns_PersonaCapabilities_instance(backend_factory) -> None:
    """``capabilities()`` returns a ``PersonaCapabilities`` instance."""
    with backend_factory() as backend:
        caps = backend.capabilities()
    assert isinstance(caps, PersonaCapabilities)


def test_capabilities_is_stable_across_calls(backend_factory) -> None:
    """``capabilities()`` returns the same values across multiple calls."""
    with backend_factory() as backend:
        caps1 = backend.capabilities()
        caps2 = backend.capabilities()
    assert caps1 == caps2


def test_capabilities_has_all_six_boolean_fields(backend_factory) -> None:
    """All 6 capability fields are present and are booleans."""
    with backend_factory() as backend:
        caps = backend.capabilities()
    assert isinstance(caps.supports_save, bool)
    assert isinstance(caps.supports_clone, bool)
    assert isinstance(caps.supports_snapshot, bool)
    assert isinstance(caps.supports_subscribe, bool)
    assert isinstance(caps.durable, bool)
    assert isinstance(caps.supports_templates, bool)


# ─────────────────────────────────────────────────────────────────────────────
# Tests 26-29 -- backend_id property


def test_backend_id_is_non_empty_string(backend_factory) -> None:
    """``backend_id`` is a non-empty string."""
    with backend_factory() as backend:
        bid = backend.backend_id
    assert isinstance(bid, str)
    assert bid


def test_backend_id_is_stable_across_calls(backend_factory) -> None:
    """``backend_id`` returns the same value across calls."""
    with backend_factory() as backend:
        bid1 = backend.backend_id
        bid2 = backend.backend_id
    assert bid1 == bid2


def test_filesystem_backend_id_is_filesystem(tmp_path: Path) -> None:
    """The filesystem backend's ``backend_id`` is ``"filesystem"``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    assert backend.backend_id == "filesystem"


def test_mock_backend_id_is_mock(mock_registered) -> None:  # noqa: ARG001
    """The mock backend's ``backend_id`` is ``"mock"``."""
    mock = MockPersonaBackend()
    assert mock.backend_id == "mock"


# ─────────────────────────────────────────────────────────────────────────────
# Charset / security tests -- filesystem backend only (path-traversal concerns
# are meaningless for an in-memory dict backend)

_INVALID_PERSONA_IDS = [
    pytest.param("../etc/passwd", id="dotdot_slash"),
    pytest.param("a/b", id="slash"),
    pytest.param("a\\b", id="backslash"),
    pytest.param("\x00name", id="null_byte"),
    pytest.param("name\x01", id="control_char"),
    pytest.param("name\n", id="newline"),
    pytest.param("name\r", id="carriage_return"),
    pytest.param(".hidden", id="leading_dot"),
    pytest.param("", id="empty_string"),
]


@pytest.mark.parametrize("bad_id", _INVALID_PERSONA_IDS)
def test_filesystem_backend_refuses_invalid_persona_id(
    tmp_path: Path, bad_id: str
) -> None:
    """Invalid persona_id raises ``ValueError`` on the filesystem backend.

    Tested against filesystem backend specifically because path-traversal
    guards are load-bearing on disk; the mock guard is present but not
    security-critical.
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona = _make_persona()

    with pytest.raises(ValueError):
        backend.exists(bad_id)

    if bad_id:
        with pytest.raises(ValueError):
            backend.load_persona(bad_id)
        with pytest.raises(ValueError):
            backend.save_persona(bad_id, persona)


def test_filesystem_backend_error_contains_offending_value(tmp_path: Path) -> None:
    """The ``ValueError`` message contains the offending persona_id value
    (or a representation of it) for debuggability."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    with pytest.raises(ValueError, match=r"\.\./etc/passwd"):
        backend.exists("../etc/passwd")


# ─────────────────────────────────────────────────────────────────────────────
# Registry primitive tests -- not parametrized


def test_register_adds_to_list(mock_registered) -> None:  # noqa: ARG001
    """Registering a backend adds it to ``list_persona_backends``."""
    assert "mock" in list_persona_backends()


def test_unregister_removes_from_list() -> None:
    """Unregistering removes the backend from ``list_persona_backends``."""
    register_persona_backend("temp-test-backend", MockPersonaBackend)
    assert "temp-test-backend" in list_persona_backends()
    unregister_persona_backend("temp-test-backend")
    assert "temp-test-backend" not in list_persona_backends()


def test_get_persona_backend_returns_registered_class(
    mock_registered,  # noqa: ARG001
) -> None:
    """``get_persona_backend("mock")`` returns the registered ``MockPersonaBackend`` class."""
    cls = get_persona_backend("mock")
    assert cls is MockPersonaBackend


def test_get_persona_backend_unknown_raises_BackendNotRegistered() -> None:
    """``get_persona_backend`` raises ``BackendNotRegistered`` for an unknown id."""
    with pytest.raises(BackendNotRegistered):
        get_persona_backend("definitely-not-registered-xyz-12345")


def test_unregister_is_idempotent() -> None:
    """``unregister_persona_backend`` is a no-op if the id is not present."""
    unregister_persona_backend("nonexistent-backend-xyz")


def test_register_silently_replaces_on_collision() -> None:
    """Registering under an existing id replaces the class (no exception)."""

    class AltMockBackend(MockPersonaBackend):
        backend_id = "alt"

    register_persona_backend("collision-test", MockPersonaBackend)
    try:
        register_persona_backend("collision-test", AltMockBackend)
        cls = get_persona_backend("collision-test")
        assert cls is AltMockBackend
    finally:
        unregister_persona_backend("collision-test")


def test_filesystem_backend_is_registered_at_import() -> None:
    """The filesystem backend is registered under ``"filesystem"`` at module
    import time via ``_bootstrap_filesystem()``."""
    assert "filesystem" in list_persona_backends()
    cls = get_persona_backend("filesystem")
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    assert cls is FilesystemPersonaBackend


def test_get_default_persona_backend_defaults_to_filesystem(
    tmp_path: Path,
) -> None:
    """Without env var, ``get_default_persona_backend`` returns a
    ``FilesystemPersonaBackend``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    env_key = "ATOMIC_AGENTS_PERSONA_BACKEND"
    original = os.environ.pop(env_key, None)
    try:
        backend = get_default_persona_backend(tmp_path)
        assert isinstance(backend, FilesystemPersonaBackend)
    finally:
        if original is not None:
            os.environ[env_key] = original


def test_get_default_persona_backend_respects_env_var(tmp_path: Path) -> None:
    """``ATOMIC_AGENTS_PERSONA_BACKEND=filesystem`` still returns
    ``FilesystemPersonaBackend``; unknown value raises ``BackendNotRegistered``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    env_key = "ATOMIC_AGENTS_PERSONA_BACKEND"
    original = os.environ.pop(env_key, None)
    try:
        os.environ[env_key] = "filesystem"
        backend = get_default_persona_backend(tmp_path)
        assert isinstance(backend, FilesystemPersonaBackend)

        os.environ[env_key] = "no-such-backend-xyz"
        with pytest.raises(BackendNotRegistered):
            get_default_persona_backend(tmp_path)
    finally:
        del os.environ[env_key]
        if original is not None:
            os.environ[env_key] = original


def test_get_default_persona_backend_url_env_var_dispatches_to_factory(
    tmp_path: Path,
) -> None:
    """``ATOMIC_AGENTS_PERSONA_BACKEND_URL`` routes through the URL factory.

    Exercises the URL-dispatch branch of ``get_default_persona_backend``:
    when the URL env var is set alongside (or without) the backend id env
    var, the URL factory parses the URL and returns the backend pinned to
    the URL's path, not ``scope_root / .personas``.
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend_key = "ATOMIC_AGENTS_PERSONA_BACKEND"
    url_key = "ATOMIC_AGENTS_PERSONA_BACKEND_URL"
    original_backend = os.environ.pop(backend_key, None)
    original_url = os.environ.pop(url_key, None)

    custom_root = tmp_path / "custom-personas"
    custom_root.mkdir()

    try:
        os.environ[backend_key] = "filesystem"
        os.environ[url_key] = f"filesystem://{custom_root}"

        scope_root = tmp_path / "scope"
        backend = get_default_persona_backend(scope_root)

        assert isinstance(backend, FilesystemPersonaBackend)
        assert backend._personas_root == custom_root
    finally:
        for key, original in [(backend_key, original_backend), (url_key, original_url)]:
            os.environ.pop(key, None)
            if original is not None:
                os.environ[key] = original


# ─────────────────────────────────────────────────────────────────────────────
# P2-3 -- @runtime_checkable isinstance check


def test_backend_is_runtime_checkable_protocol_instance(backend_factory) -> None:
    """``isinstance(backend, PersonaBackend)`` returns True for every backend.

    ``@runtime_checkable`` enables a method-presence check (not a full
    signature check). Verifies that both the filesystem and mock backends
    satisfy the Protocol at runtime.
    """
    from atomic_agents.persona.backend import PersonaBackend

    with backend_factory() as backend:
        assert isinstance(backend, PersonaBackend)


# ─────────────────────────────────────────────────────────────────────────────
# Conformance tests for supports_snapshot=True backends (filesystem only in v1)
# These tests SKIP when capabilities().supports_snapshot is False.


def test_snapshot_creates_retrievable_record(backend_factory) -> None:
    """``snapshot()`` returns a snapshot_id; ``list_snapshots()`` returns a
    record with that id.

    SKIP when ``capabilities().supports_snapshot`` is False (mock backend).
    """
    persona = _make_persona(identity="Original body.", version=1)
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("my-persona", persona)
        snap_id = backend.snapshot("my-persona")
        snaps = backend.list_snapshots("my-persona")

    assert isinstance(snap_id, str)
    assert snap_id
    snap_ids = [s.snapshot_id for s in snaps]
    assert snap_id in snap_ids


def test_snapshot_returns_PersonaSnapshot_with_correct_fields(
    backend_factory,
) -> None:
    """``list_snapshots()`` returns ``PersonaSnapshot`` instances with correct
    ``snapshot_id``, ``persona_id``, ``label``, and ``created_at`` fields.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    persona = _make_persona()
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("p", persona)
        snap_id = backend.snapshot("p", label="my-label")
        snaps = backend.list_snapshots("p")

    assert len(snaps) == 1
    snap = snaps[0]
    assert isinstance(snap, PersonaSnapshot)
    assert snap.snapshot_id == snap_id
    assert snap.persona_id == "p"
    assert snap.label == "my-label"
    assert snap.created_at


def test_restore_reverts_persona_body(backend_factory) -> None:
    """``restore()`` reverts the persona to the snapshot state.

    Save v1, snapshot it, save v2, restore from snapshot; load should
    return v1 body bytes.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    v1 = _make_persona(identity="Version 1 body.", version=1)
    v2 = _make_persona(identity="Version 2 body.", version=2)
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("p", v1)
        snap_id = backend.snapshot("p")
        backend.save_persona("p", v2, overwrite=True)
        backend.restore("p", snap_id)
        loaded = backend.load_persona("p")

    assert loaded.identity == "Version 1 body."


def test_list_snapshots_returns_chronological_order(backend_factory) -> None:
    """``list_snapshots()`` returns snapshots in ascending ``created_at`` order.

    Takes 3 snapshots; verifies the returned list is sorted by ``created_at``
    (ISO-8601 lexicographic order equals chronological order for tz-aware
    timestamps).

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    persona = _make_persona()
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("p", persona)
        backend.snapshot("p", label="first")
        backend.snapshot("p", label="second")
        backend.snapshot("p", label="third")
        snaps = backend.list_snapshots("p")

    assert len(snaps) == 3
    created_ats = [s.created_at for s in snaps]
    assert created_ats == sorted(created_ats), (
        f"list_snapshots not in chronological order: {created_ats}"
    )


def test_list_snapshots_returns_empty_when_no_snapshots(backend_factory) -> None:
    """``list_snapshots()`` returns ``[]`` when no snapshots exist for the persona.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    persona = _make_persona()
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("p", persona)
        snaps = backend.list_snapshots("p")

    assert snaps == []


def test_cross_persona_snapshot_isolation(backend_factory) -> None:
    """A snapshot_id from persona A raises ``PersonaSnapshotNotFound`` when
    restored to persona B (cross-persona isolation, D-PP-10).

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    from atomic_agents.exceptions import PersonaSnapshotNotFound

    persona = _make_persona()
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("persona-a", persona)
        backend.save_persona("persona-b", persona)
        snap_id_a = backend.snapshot("persona-a")
        with pytest.raises(PersonaSnapshotNotFound):
            backend.restore("persona-b", snap_id_a)


def test_snapshot_label_round_trips(backend_factory) -> None:
    """The ``label`` supplied to ``snapshot()`` round-trips through
    ``list_snapshots()``.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    persona = _make_persona()
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("p", persona)
        backend.snapshot("p", label="my-snapshot-label")
        snaps = backend.list_snapshots("p")

    assert len(snaps) == 1
    assert snaps[0].label == "my-snapshot-label"


def test_snapshot_null_label_round_trips(backend_factory) -> None:
    """When ``label=None`` is supplied to ``snapshot()``, ``list_snapshots()``
    returns ``label=None`` for that snapshot.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    persona = _make_persona()
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("p", persona)
        backend.snapshot("p", label=None)
        snaps = backend.list_snapshots("p")

    assert len(snaps) == 1
    assert snaps[0].label is None


def test_snapshot_of_nonexistent_persona_raises_PersonaNotFound(
    backend_factory,
) -> None:
    """``snapshot()`` raises ``PersonaNotFound`` when the persona does not exist.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    from atomic_agents.exceptions import PersonaNotFound

    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        with pytest.raises(PersonaNotFound):
            backend.snapshot("no-such-persona")


def test_list_snapshots_of_nonexistent_persona_raises_PersonaNotFound(
    backend_factory,
) -> None:
    """``list_snapshots()`` raises ``PersonaNotFound`` when the persona does not exist.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    from atomic_agents.exceptions import PersonaNotFound

    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        with pytest.raises(PersonaNotFound):
            backend.list_snapshots("no-such-persona")


def test_restore_unknown_snapshot_raises_PersonaSnapshotNotFound(
    backend_factory,
) -> None:
    """``restore()`` with an unknown snapshot_id raises ``PersonaSnapshotNotFound``.

    SKIP when ``capabilities().supports_snapshot`` is False.
    """
    from atomic_agents.exceptions import PersonaSnapshotNotFound

    persona = _make_persona()
    with backend_factory() as backend:
        if not backend.capabilities().supports_snapshot:
            pytest.skip("backend does not support snapshot")
        backend.save_persona("p", persona)
        with pytest.raises(PersonaSnapshotNotFound):
            backend.restore("p", "snap_2026-01-01T000000_000000000000")

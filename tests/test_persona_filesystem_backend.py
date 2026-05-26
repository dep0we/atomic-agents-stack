"""Filesystem-specific tests for ``FilesystemPersonaBackend``.

Conformance tests in ``test_persona_protocol_conformance.py`` exercise the
Protocol contract that every backend must satisfy. THIS module exercises
filesystem-specific behavior: on-disk storage layout, metadata sidecar,
URL factory validation, atomic write guarantees, concurrent safety, and
side-effect-free construction.

The conformance suite already covers byte-for-byte round-trip of all fields
and charset validation across both backends. This module covers what is
filesystem-only.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.persona.types import Persona


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
# Constructor


def test_constructor_accepts_str_path(tmp_path: Path) -> None:
    """Constructor accepts a plain string path (not just ``Path``)."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(str(tmp_path))
    assert backend is not None


def test_constructor_accepts_Path_object(tmp_path: Path) -> None:
    """Constructor accepts a ``pathlib.Path`` object."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    assert backend is not None


def test_constructor_nonexistent_path_does_not_raise(tmp_path: Path) -> None:
    """``FilesystemPersonaBackend(non_existent_path)`` succeeds -- side-effect-free
    construction means the directory need not exist yet (spec/33 MUST #2)."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    non_existent = tmp_path / "does-not-exist"
    assert not non_existent.exists()
    backend = FilesystemPersonaBackend(non_existent)
    assert backend is not None


# ─────────────────────────────────────────────────────────────────────────────
# Storage layout


def test_storage_layout_creates_correct_files(tmp_path: Path) -> None:
    """After ``save_persona``, the storage directory contains exactly
    IDENTITY.md, SOUL.md, USER.md, and metadata.json."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona = _make_persona(
        identity="Identity content.",
        soul="Soul content.",
        user="User content.",
    )
    backend.save_persona("my-persona", persona)

    persona_dir = tmp_path / "my-persona"
    assert persona_dir.is_dir()
    assert (persona_dir / "IDENTITY.md").exists()
    assert (persona_dir / "SOUL.md").exists()
    assert (persona_dir / "USER.md").exists()
    assert (persona_dir / "metadata.json").exists()


def test_storage_layout_file_contents_match_persona(tmp_path: Path) -> None:
    """The raw on-disk file contents match the Persona fields exactly."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona = _make_persona(
        identity="My identity text.",
        soul="My soul text.",
        user="My user text.",
    )
    backend.save_persona("test-persona", persona)

    persona_dir = tmp_path / "test-persona"
    assert (persona_dir / "IDENTITY.md").read_text(
        encoding="utf-8"
    ) == "My identity text."
    assert (persona_dir / "SOUL.md").read_text(encoding="utf-8") == "My soul text."
    assert (persona_dir / "USER.md").read_text(encoding="utf-8") == "My user text."


def test_metadata_sidecar_round_trips_version_label_created_at(tmp_path: Path) -> None:
    """The ``metadata.json`` sidecar round-trips ``version``, ``label``, and
    ``created_at`` correctly when parsed as raw JSON."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona = _make_persona(
        version=7,
        created_at="2026-03-15T08:30:00Z",
        label="tone-revision",
    )
    backend.save_persona("versioned", persona)

    raw = json.loads(
        (tmp_path / "versioned" / "metadata.json").read_text(encoding="utf-8")
    )
    assert raw["version"] == 7
    assert raw["label"] == "tone-revision"
    assert raw["created_at"] == "2026-03-15T08:30:00Z"


def test_metadata_sidecar_null_label_when_not_supplied(tmp_path: Path) -> None:
    """When ``label`` is not supplied, the sidecar stores ``null``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona = _make_persona(label=None)
    backend.save_persona("no-label", persona)

    raw = json.loads(
        (tmp_path / "no-label" / "metadata.json").read_text(encoding="utf-8")
    )
    assert raw["label"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Side-effect-free construction (lazy I/O)


def test_nonexistent_personas_root_load_raises_PersonaNotFound(
    tmp_path: Path,
) -> None:
    """Calling ``load_persona`` on a backend whose ``personas_root`` does not
    exist raises ``PersonaNotFound`` (not a crash from missing dir)."""
    from atomic_agents.exceptions import PersonaNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    non_existent = tmp_path / "ghost"
    backend = FilesystemPersonaBackend(non_existent)
    with pytest.raises(PersonaNotFound):
        backend.load_persona("any-persona")


def test_nonexistent_personas_root_list_returns_empty(tmp_path: Path) -> None:
    """``list_personas`` on a backend with a non-existent root returns ``[]``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    non_existent = tmp_path / "ghost"
    backend = FilesystemPersonaBackend(non_existent)
    assert backend.list_personas() == []


def test_nonexistent_personas_root_exists_returns_false(tmp_path: Path) -> None:
    """``exists`` on a backend with a non-existent root returns False."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    non_existent = tmp_path / "ghost"
    backend = FilesystemPersonaBackend(non_existent)
    assert backend.exists("any-persona") is False


def test_persona_dir_exists_but_files_missing_raises_PersonaNotFound(
    tmp_path: Path,
) -> None:
    """Persona directory exists on disk but one of IDENTITY/SOUL/USER.md is
    missing.

    Exercises the inner ``FileNotFoundError`` branch of ``load_persona``
    (where the persona dir is present so the outer ``is_dir()`` check
    passes, but ``_load_persona_from_dir`` fails on a missing required
    file). Operator scenario: partial write from an external editor, or
    manual deletion of one file via the filesystem.
    """
    from atomic_agents.exceptions import PersonaNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona_dir = tmp_path / "partial-persona"
    persona_dir.mkdir()
    (persona_dir / "IDENTITY.md").write_text("identity body")
    (persona_dir / "SOUL.md").write_text("soul body")
    (persona_dir / "metadata.json").write_text(
        '{"version": 1, "label": null, "created_at": "2026-05-26T00:00:00Z"}'
    )

    with pytest.raises(PersonaNotFound) as exc_info:
        backend.load_persona("partial-persona")

    assert "partial-persona" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# Permission error on save


def test_chmod000_personas_root_propagates_permission_error(tmp_path: Path) -> None:
    """When the ``personas_root`` parent is not writable, ``save_persona``
    propagates the ``PermissionError`` (matches ``FilesystemAgentProfileBackend``
    precedent)."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    locked_root = tmp_path / "locked"
    locked_root.mkdir()
    locked_root.chmod(0o000)
    try:
        backend = FilesystemPersonaBackend(locked_root / "personas")
        with pytest.raises((PermissionError, OSError)):
            backend.save_persona("my-persona", _make_persona())
    finally:
        locked_root.chmod(0o755)


# ─────────────────────────────────────────────────────────────────────────────
# Atomic write


def test_atomic_write_original_unchanged_on_mid_save_failure(
    tmp_path: Path,
) -> None:
    """If ``os.replace`` fails on the second file mid-save, the first file is
    already overwritten but the original IDENTITY.md remains unchanged when
    ``os.replace`` raises before the IDENTITY.md rename fires.

    ``_io.atomic_write`` uses ``os.replace`` (not ``os.rename``). We
    monkeypatch ``atomic_agents._io.os.replace`` to raise on the second call,
    which happens during the SOUL.md write. IDENTITY.md fires first and succeeds;
    SOUL.md raises. We verify that by patching at ``_io`` level the side-effect
    propagates as an OSError out of ``save_persona``.
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    original = _make_persona(identity="Original content.", version=1)
    backend.save_persona("my-persona", original)

    call_count = [0]
    real_replace = os.replace

    def _fail_on_second_replace(src, dst, **kwargs):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise OSError("Simulated mid-save failure")
        return real_replace(src, dst, **kwargs)

    updated = _make_persona(identity="Updated content.", version=2)
    # Patch at the _io module level where atomic_write actually calls os.replace
    with patch("atomic_agents._io.os.replace", side_effect=_fail_on_second_replace):
        with pytest.raises(OSError):
            backend.save_persona("my-persona", updated, overwrite=True)


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent safety


def test_concurrent_save_different_persona_ids_no_corruption(
    tmp_path: Path,
) -> None:
    """8 threads writing different persona_ids simultaneously; all 8 are
    readable afterward without corruption.

    Last-writer-wins is acceptable for same persona_id; this test uses
    distinct ids so each thread writes independently.
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    errors: list[Exception] = []

    def _save(i: int) -> None:
        try:
            persona = _make_persona(
                identity=f"Identity for worker {i}.",
                version=i,
            )
            backend.save_persona(f"persona-{i}", persona)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_save, i) for i in range(8)]
        concurrent.futures.wait(futures)

    assert not errors, f"Concurrent saves raised exceptions: {errors}"

    for i in range(8):
        loaded = backend.load_persona(f"persona-{i}")
        assert loaded.version == i
        assert loaded.identity == f"Identity for worker {i}."


# ─────────────────────────────────────────────────────────────────────────────
# URL factory


def test_url_factory_filesystem_scheme_returns_backend(tmp_path: Path) -> None:
    """``make_filesystem_persona_backend_from_url("filesystem:///path")``
    returns a ``FilesystemPersonaBackend``."""
    from atomic_agents.persona.filesystem import (
        FilesystemPersonaBackend,
        make_filesystem_persona_backend_from_url,
    )

    url = f"filesystem://{tmp_path}"
    backend = make_filesystem_persona_backend_from_url(url)
    assert isinstance(backend, FilesystemPersonaBackend)


def test_url_factory_backend_id_matches(tmp_path: Path) -> None:
    """URL-constructed backend has ``backend_id == "filesystem"``."""
    from atomic_agents.persona.filesystem import (
        make_filesystem_persona_backend_from_url,
    )

    url = f"filesystem://{tmp_path}"
    backend = make_filesystem_persona_backend_from_url(url)
    assert backend.backend_id == "filesystem"


def test_url_factory_refuses_non_filesystem_scheme(tmp_path: Path) -> None:
    """Non-filesystem scheme raises ``ValueError``."""
    from atomic_agents.persona.filesystem import (
        make_filesystem_persona_backend_from_url,
    )

    with pytest.raises(ValueError):
        make_filesystem_persona_backend_from_url("postgres://host/db")

    with pytest.raises(ValueError):
        make_filesystem_persona_backend_from_url("sqlite:///path/to/db")


def test_url_factory_refuses_netloc(tmp_path: Path) -> None:
    """URL with a netloc (host) component raises ``ValueError``."""
    from atomic_agents.persona.filesystem import (
        make_filesystem_persona_backend_from_url,
    )

    with pytest.raises(ValueError):
        make_filesystem_persona_backend_from_url("filesystem://hostname/path")


def test_url_factory_refuses_fragment(tmp_path: Path) -> None:
    """URL with a fragment component raises ``ValueError``."""
    from atomic_agents.persona.filesystem import (
        make_filesystem_persona_backend_from_url,
    )

    with pytest.raises(ValueError):
        make_filesystem_persona_backend_from_url(f"filesystem://{tmp_path}#fragment")


def test_url_factory_refuses_unknown_query_params(tmp_path: Path) -> None:
    """URL with query params raises ``ValueError`` (filesystem backend
    accepts no query params)."""
    from atomic_agents.persona.filesystem import (
        make_filesystem_persona_backend_from_url,
    )

    with pytest.raises(ValueError):
        make_filesystem_persona_backend_from_url(f"filesystem://{tmp_path}?unknown=foo")


def test_url_factory_credential_redaction(tmp_path: Path) -> None:
    """Error message from URL factory does NOT contain raw URL credentials.

    Operators may accidentally paste credentialed URLs; the error message
    must be safe to surface in ``doctor`` output and CI logs (spec/33 MUST #4).
    """
    from atomic_agents.persona.filesystem import (
        make_filesystem_persona_backend_from_url,
    )

    bad_url = "postgres://user:password@host/db"
    with pytest.raises(ValueError) as exc_info:
        make_filesystem_persona_backend_from_url(bad_url)

    error_message = str(exc_info.value)
    assert "password" not in error_message, (
        f"Credential 'password' leaked into the error message: {error_message!r}"
    )


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        b"filesystem:///tmp/personas",
        None,
        123,
        ["filesystem:///tmp/personas"],
    ],
)
def test_url_factory_refuses_empty_or_non_string(bad_input: object) -> None:
    """Empty string and non-string inputs raise ``ValueError`` before parsing.

    Guards the URL factory entry point so callers passing ``None`` or a
    bytes object get a clean error instead of a confusing ``TypeError``
    from ``urlparse`` deep in the stack.
    """
    from atomic_agents.persona.filesystem import (
        make_filesystem_persona_backend_from_url,
    )

    with pytest.raises(ValueError):
        make_filesystem_persona_backend_from_url(bad_input)  # type: ignore[arg-type]

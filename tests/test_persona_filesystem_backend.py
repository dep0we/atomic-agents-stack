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
import re
import time
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


def test_mid_save_failure_overwrite_restores_old_record(
    tmp_path: Path,
) -> None:
    """A mid-save failure during ``overwrite=True`` restores the backup and
    retries. After exhausting retries the old record is preserved on disk.

    ``save_persona(overwrite=True)`` writes files to a sibling temp dir,
    renames the existing persona dir to a backup, then renames the temp dir
    to the persona dir. If the second rename fails, the backup is restored
    and the retry loop continues. To force exhaustion of all retries we
    monkeypatch ``Path.rename`` to always fail on the temp-to-persona-dir
    step.
    """
    from atomic_agents.exceptions import PersonaError
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    original = _make_persona(identity="Original content.", version=1)
    backend.save_persona("my-persona", original)

    real_rename = Path.rename
    rename_calls: list[tuple[str, str]] = []

    def _always_fail_second_rename(self, target):
        # First rename (persona_dir -> backup): allow.
        # Second rename (tmp -> persona_dir): fail every time to exhaust retries.
        src_name = Path(str(self)).name
        dst_name = Path(str(target)).name
        rename_calls.append((src_name, dst_name))
        if dst_name == "my-persona" and not src_name.startswith(".my-persona.old-"):
            raise OSError("Simulated persistent mid-save failure")
        return real_rename(self, target)

    updated = _make_persona(identity="Updated content.", version=2)
    with patch.object(Path, "rename", _always_fail_second_rename):
        with pytest.raises((OSError, PersonaError)):
            backend.save_persona("my-persona", updated, overwrite=True)

    # The backup restore puts the old record back each time; after all retries
    # exhaust, the original record is still readable.
    loaded = backend.load_persona("my-persona")
    assert loaded.identity == "Original content."
    assert loaded.version == 1


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


# ─────────────────────────────────────────────────────────────────────────────
# P1-1: PersonaCorrupted on corrupt metadata.json


def test_corrupt_metadata_invalid_json_raises_PersonaCorrupted(
    tmp_path: Path,
) -> None:
    """``metadata.json`` containing invalid JSON raises ``PersonaCorrupted``."""
    from atomic_agents.exceptions import PersonaCorrupted
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona_dir = tmp_path / "corrupt-persona"
    persona_dir.mkdir()
    (persona_dir / "IDENTITY.md").write_text("identity", encoding="utf-8")
    (persona_dir / "SOUL.md").write_text("soul", encoding="utf-8")
    (persona_dir / "USER.md").write_text("user", encoding="utf-8")
    (persona_dir / "metadata.json").write_text("not-valid-json{{{", encoding="utf-8")

    with pytest.raises(PersonaCorrupted, match="corrupt-persona"):
        backend.load_persona("corrupt-persona")


def test_corrupt_metadata_missing_key_raises_PersonaCorrupted(
    tmp_path: Path,
) -> None:
    """``metadata.json`` missing the ``version`` key raises ``PersonaCorrupted``."""
    from atomic_agents.exceptions import PersonaCorrupted
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona_dir = tmp_path / "missing-key"
    persona_dir.mkdir()
    (persona_dir / "IDENTITY.md").write_text("identity", encoding="utf-8")
    (persona_dir / "SOUL.md").write_text("soul", encoding="utf-8")
    (persona_dir / "USER.md").write_text("user", encoding="utf-8")
    (persona_dir / "metadata.json").write_text(
        '{"label": null, "created_at": "2026-01-01T00:00:00Z"}', encoding="utf-8"
    )

    with pytest.raises(PersonaCorrupted, match="missing-key"):
        backend.load_persona("missing-key")


def test_corrupt_body_non_utf8_raises_PersonaCorrupted(tmp_path: Path) -> None:
    """A body file with non-UTF-8 bytes raises ``PersonaCorrupted``."""
    from atomic_agents.exceptions import PersonaCorrupted
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona_dir = tmp_path / "bad-encoding"
    persona_dir.mkdir()
    (persona_dir / "IDENTITY.md").write_bytes(b"\xff\xfe invalid utf-8")
    (persona_dir / "SOUL.md").write_text("soul", encoding="utf-8")
    (persona_dir / "USER.md").write_text("user", encoding="utf-8")
    (persona_dir / "metadata.json").write_text(
        '{"version": 1, "label": null, "created_at": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    with pytest.raises(PersonaCorrupted, match="bad-encoding"):
        backend.load_persona("bad-encoding")


# ─────────────────────────────────────────────────────────────────────────────
# P1-3: TOCTOU-safe overwrite=False


def test_concurrent_save_no_overwrite_only_one_succeeds(tmp_path: Path) -> None:
    """N threads racing to ``save_persona(..., overwrite=False)`` on the same
    persona_id: exactly one succeeds and the rest raise ``PersonaExists``."""
    from atomic_agents.exceptions import PersonaExists
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona = _make_persona()

    successes: list[int] = []
    failures: list[int] = []

    def _try_save(i: int) -> None:
        try:
            backend.save_persona("raced", persona, overwrite=False)
            successes.append(i)
        except PersonaExists:
            failures.append(i)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futs = [executor.submit(_try_save, i) for i in range(8)]
        concurrent.futures.wait(futs)

    assert len(successes) == 1, f"Expected exactly 1 success, got {successes}"
    assert len(failures) == 7, f"Expected 7 PersonaExists, got {failures}"


# ─────────────────────────────────────────────────────────────────────────────
# P2-1: list_personas IDENTITY.md sentinel


def test_list_personas_ignores_dirs_without_IDENTITY_md(tmp_path: Path) -> None:
    """``list_personas`` skips directories that have no ``IDENTITY.md`` file."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("real-persona", _make_persona())

    (tmp_path / "empty-dir").mkdir()
    (tmp_path / "partial-dir").mkdir()
    (tmp_path / "partial-dir" / "SOUL.md").write_text("soul only")

    result = backend.list_personas()
    assert result == ["real-persona"]


# ─────────────────────────────────────────────────────────────────────────────
# P2-2: _redact_url preserves @ in path


def test_redact_url_preserves_at_in_path(tmp_path: Path) -> None:
    """``_redact_url`` does NOT redact ``@`` that appears in the path component."""
    from atomic_agents.persona.filesystem import _redact_url

    result = _redact_url("filesystem:///home/ops@fleet/personas")
    assert "ops@fleet" in result


def test_redact_url_redacts_at_in_authority(tmp_path: Path) -> None:
    """``_redact_url`` DOES redact ``user:pass@`` in the authority section."""
    from atomic_agents.persona.filesystem import _redact_url

    result = _redact_url("postgres://user:pass@hostname/db")
    assert "pass" not in result
    assert "***@hostname" in result


# ─────────────────────────────────────────────────────────────────────────────
# P2-6: schema_version field in metadata


def test_metadata_schema_version_serialized(tmp_path: Path) -> None:
    """``metadata.json`` written by ``save_persona`` contains ``schema_version: 1``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("versioned", _make_persona())

    raw = json.loads(
        (tmp_path / "versioned" / "metadata.json").read_text(encoding="utf-8")
    )
    assert raw["schema_version"] == 1


def test_metadata_unsupported_schema_version_raises_PersonaCorrupted(
    tmp_path: Path,
) -> None:
    """``metadata.json`` with an unsupported ``schema_version`` raises
    ``PersonaCorrupted`` with the schema version in the message."""
    from atomic_agents.exceptions import PersonaCorrupted
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona_dir = tmp_path / "future-schema"
    persona_dir.mkdir()
    (persona_dir / "IDENTITY.md").write_text("identity", encoding="utf-8")
    (persona_dir / "SOUL.md").write_text("soul", encoding="utf-8")
    (persona_dir / "USER.md").write_text("user", encoding="utf-8")
    (persona_dir / "metadata.json").write_text(
        '{"schema_version": 99, "version": 1, "label": null, "created_at": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    with pytest.raises(PersonaCorrupted, match="99"):
        backend.load_persona("future-schema")


# ─────────────────────────────────────────────────────────────────────────────
# P1-1: Concurrent overwrite=True does not raise FileNotFoundError


def test_concurrent_save_overwrite_true_does_not_raise(tmp_path: Path) -> None:
    """16 threads racing ``save_persona(same_id, ..., overwrite=True)`` all
    complete without raising. Reproduces the pre-fix FileNotFoundError caused
    by the unguarded ``persona_dir.rename(backup)`` call in the original
    group-atomic logic.

    After all threads finish, the on-disk persona is readable and is one of
    the 16 written values (last-writer-wins).
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    # Seed an initial record so all threads take the overwrite=True path.
    backend.save_persona("shared", _make_persona(identity="Initial.", version=0))

    errors: list[Exception] = []

    def _overwrite(i: int) -> None:
        try:
            backend.save_persona(
                "shared",
                _make_persona(identity=f"Writer {i}.", version=i),
                overwrite=True,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(_overwrite, i) for i in range(16)]
        concurrent.futures.wait(futures)

    assert not errors, f"Concurrent overwrite raised: {errors}"

    loaded = backend.load_persona("shared")
    valid_identities = {f"Writer {i}." for i in range(16)}
    assert loaded.identity in valid_identities, (
        f"Final identity {loaded.identity!r} is not one of the 16 written values"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1-2: overwrite=True refuses regular file at persona_dir path


def test_overwrite_true_refuses_regular_file(tmp_path: Path) -> None:
    """When a regular file exists at the persona_dir path, ``save_persona``
    with ``overwrite=True`` raises ``PersonaExists`` with 'not a directory'
    in the message rather than silently coercing the file into a persona dir.
    """
    from atomic_agents.exceptions import PersonaExists
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    # Plant a regular file where a persona directory would go.
    stub = tmp_path / "stub"
    stub.write_text("I am a file, not a directory.", encoding="utf-8")

    with pytest.raises(PersonaExists, match="not a directory"):
        backend.save_persona("stub", _make_persona(), overwrite=True)


# ─────────────────────────────────────────────────────────────────────────────
# P1-3: list_personas excludes symlinked IDENTITY.md pointing outside root


def test_list_personas_excludes_symlinked_identity_md(tmp_path: Path) -> None:
    """A persona directory whose IDENTITY.md is a symlink to a file outside
    personas_root is silently excluded from ``list_personas``.

    Also verifies that ``load_persona`` on the excluded entry raises
    ``PersonaCorrupted`` (resolves-outside-root) or ``PersonaNotFound``.
    """
    import os

    from atomic_agents.exceptions import PersonaCorrupted, PersonaNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    personas_root = tmp_path / "personas"
    personas_root.mkdir()

    # File outside personas_root.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("MAGIC SECRET", encoding="utf-8")

    # Evil persona dir with a symlinked IDENTITY.md.
    evil_dir = personas_root / "evil"
    evil_dir.mkdir()
    os.symlink(secret, evil_dir / "IDENTITY.md")

    # Real persona.
    backend = FilesystemPersonaBackend(personas_root)
    backend.save_persona("real", _make_persona(identity="Legit."))

    result = backend.list_personas()
    assert result == ["real"], f"Expected only ['real'], got {result}"

    with pytest.raises((PersonaCorrupted, PersonaNotFound)):
        backend.load_persona("evil")


# ─────────────────────────────────────────────────────────────────────────────
# P2-1: schema_version type discipline


@pytest.mark.parametrize(
    "bad_version",
    [1.0, True, "1", None, [1], {"v": 1}],
    ids=["float", "bool", "string", "null", "list", "dict"],
)
def test_metadata_schema_version_rejects_non_int(
    tmp_path: Path, bad_version: object
) -> None:
    """``metadata.json`` with a non-integer ``schema_version`` raises
    ``PersonaCorrupted`` naming the bad type. Covers float, bool (True == 1
    in Python but must be rejected), string, null, list, and dict.
    """
    import json as _json

    from atomic_agents.exceptions import PersonaCorrupted
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona_dir = tmp_path / "bad-type"
    persona_dir.mkdir()
    (persona_dir / "IDENTITY.md").write_text("identity", encoding="utf-8")
    (persona_dir / "SOUL.md").write_text("soul", encoding="utf-8")
    (persona_dir / "USER.md").write_text("user", encoding="utf-8")
    (persona_dir / "metadata.json").write_text(
        _json.dumps(
            {
                "schema_version": bad_version,
                "version": 1,
                "label": None,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PersonaCorrupted, match="schema_version"):
        backend.load_persona("bad-type")


# ─────────────────────────────────────────────────────────────────────────────
# P2-4: All four required files missing each raise PersonaNotFound


@pytest.mark.parametrize(
    "missing_file",
    ["IDENTITY.md", "SOUL.md", "USER.md", "metadata.json"],
)
def test_load_persona_raises_when_any_required_file_missing(
    tmp_path: Path, missing_file: str
) -> None:
    """Each of the four required files missing raises ``PersonaNotFound``
    (FileNotFoundError translated at the load_persona boundary).

    Pre-existing test only deleted USER.md; this parametrized version
    covers all four files symmetrically.
    """
    from atomic_agents.exceptions import PersonaNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona_dir = tmp_path / "partial"
    persona_dir.mkdir()

    all_files = {
        "IDENTITY.md": "identity",
        "SOUL.md": "soul",
        "USER.md": "user",
        "metadata.json": '{"schema_version": 1, "version": 1, "label": null, "created_at": "2026-01-01T00:00:00Z"}',
    }
    for fname, content in all_files.items():
        if fname != missing_file:
            (persona_dir / fname).write_text(content, encoding="utf-8")

    with pytest.raises(PersonaNotFound):
        backend.load_persona("partial")


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot trio -- filesystem-specific tests (D-PP-10 + D-PP-11)

_SNAP_ID_PATTERN = re.compile(r"^snap_\d{4}-\d{2}-\d{2}T\d{6}[+\-\d:]*_[0-9a-f]{12}$")


def test_snapshot_id_format_matches_spec(tmp_path: Path) -> None:
    """The snapshot_id returned by ``snapshot()`` matches the
    ``snap_<YYYY-MM-DDTHHMMSS+TZ>_<12hex>`` format (D-PP-11)."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona())
    snap_id = backend.snapshot("p")

    assert _SNAP_ID_PATTERN.match(snap_id), (
        f"snapshot_id {snap_id!r} does not match expected format "
        f"snap_<timestamp>_<12hex>"
    )


def test_snapshot_storage_layout_creates_correct_files(tmp_path: Path) -> None:
    """After ``snapshot()``, the snapshot directory contains IDENTITY.md,
    SOUL.md, USER.md, and metadata.json under
    ``<personas_root>/<persona_id>/.snapshots/<snapshot_id>/`` (D-PP-10)."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona(identity="Snapped body."))
    snap_id = backend.snapshot("p")

    snap_dir = tmp_path / "p" / ".snapshots" / snap_id
    assert snap_dir.is_dir(), f"snapshot dir {snap_dir} does not exist"
    assert (snap_dir / "IDENTITY.md").exists()
    assert (snap_dir / "SOUL.md").exists()
    assert (snap_dir / "USER.md").exists()
    assert (snap_dir / "metadata.json").exists()


def test_snapshot_metadata_json_schema(tmp_path: Path) -> None:
    """The snapshot ``metadata.json`` contains the required fields per D-PP-11:
    ``{snapshot_id, persona_id, label, created_at}``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona())
    snap_id = backend.snapshot("p", label="my-label")

    snap_dir = tmp_path / "p" / ".snapshots" / snap_id
    meta = json.loads((snap_dir / "metadata.json").read_text(encoding="utf-8"))

    assert meta["snapshot_id"] == snap_id
    assert meta["persona_id"] == "p"
    assert meta["label"] == "my-label"
    assert meta["created_at"]


def test_snapshot_dot_snapshots_not_in_list_personas(tmp_path: Path) -> None:
    """The ``.snapshots`` directory is NOT returned by ``list_personas()`` (D-PP-10:
    dot-prefix filter skips it)."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona())
    backend.snapshot("p")

    personas = backend.list_personas()
    assert personas == ["p"], (
        f"list_personas() returned {personas!r}; expected only ['p']"
    )


def test_snapshot_of_nonexistent_persona_raises_PersonaNotFound(
    tmp_path: Path,
) -> None:
    """``snapshot()`` of a persona that does not exist raises ``PersonaNotFound``."""
    from atomic_agents.exceptions import PersonaNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    with pytest.raises(PersonaNotFound, match="no-such-persona"):
        backend.snapshot("no-such-persona")


def test_restore_of_nonexistent_snapshot_raises_PersonaSnapshotNotFound(
    tmp_path: Path,
) -> None:
    """``restore()`` with a snapshot_id that does not exist raises
    ``PersonaSnapshotNotFound``."""
    from atomic_agents.exceptions import PersonaSnapshotNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona())
    with pytest.raises(PersonaSnapshotNotFound):
        backend.restore("p", "snap_2026-01-01T000000_000000000000")


def test_list_snapshots_on_persona_with_no_snapshots_returns_empty(
    tmp_path: Path,
) -> None:
    """``list_snapshots()`` returns ``[]`` when no snapshots have been taken
    for the persona."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona())
    result = backend.list_snapshots("p")

    assert result == []


def test_list_snapshots_ordering_is_monotonic_by_created_at(
    tmp_path: Path,
) -> None:
    """``list_snapshots()`` returns snapshots in ascending ``created_at`` order."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona())

    snap_ids = []
    for _ in range(3):
        snap_ids.append(backend.snapshot("p"))
        # Brief pause so timestamps are distinct (snapshot IDs include entropy
        # so they are unique, but created_at ordering requires time to pass).
        time.sleep(0.01)

    snaps = backend.list_snapshots("p")
    assert len(snaps) == 3
    created_ats = [s.created_at for s in snaps]
    assert created_ats == sorted(created_ats), (
        f"list_snapshots not in chronological order: {created_ats}"
    )


def test_restore_restores_body_bytes_exactly(tmp_path: Path) -> None:
    """``restore()`` restores the persona body bytes byte-for-byte from the
    snapshot."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    original = _make_persona(
        identity="Exact identity text.",
        soul="Exact soul text.",
        user="Exact user text.",
        version=5,
        label="original-label",
    )
    backend.save_persona("p", original)
    snap_id = backend.snapshot("p")

    # Overwrite with something different.
    updated = _make_persona(
        identity="Changed identity.",
        soul="Changed soul.",
        user="Changed user.",
        version=6,
    )
    backend.save_persona("p", updated, overwrite=True)

    # Restore and verify bytes are exactly what was snapshotted.
    backend.restore("p", snap_id)
    loaded = backend.load_persona("p")

    assert loaded.identity == "Exact identity text."
    assert loaded.soul == "Exact soul text."
    assert loaded.user == "Exact user text."


def test_snapshot_preserves_prior_metadata_fields(tmp_path: Path) -> None:
    """The snapshot captures all persona metadata fields (version, label,
    created_at); ``list_snapshots()`` returns a ``PersonaSnapshot`` whose
    ``persona`` field carries those original values."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    persona = _make_persona(
        version=7,
        label="pre-snapshot-label",
        created_at="2026-01-15T08:00:00+00:00",
    )
    backend.save_persona("p", persona)
    backend.snapshot("p", label="snap-label")

    snaps = backend.list_snapshots("p")
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.label == "snap-label"
    assert snap.persona.version == 7
    assert snap.persona.label == "pre-snapshot-label"
    assert snap.persona.created_at == "2026-01-15T08:00:00+00:00"


def test_idempotent_restore_twice_yields_same_state(tmp_path: Path) -> None:
    """Restoring from the same snapshot twice is idempotent: the persona state
    after the second restore equals the state after the first restore."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona(identity="V1 body.", version=1))
    snap_id = backend.snapshot("p")

    backend.save_persona(
        "p", _make_persona(identity="V2 body.", version=2), overwrite=True
    )

    backend.restore("p", snap_id)
    first_restore = backend.load_persona("p")

    backend.restore("p", snap_id)
    second_restore = backend.load_persona("p")

    assert first_restore.identity == second_restore.identity
    assert first_restore.soul == second_restore.soul
    assert first_restore.user == second_restore.user


def test_cross_persona_isolation_snapshot_id_from_a_raises_for_b(
    tmp_path: Path,
) -> None:
    """A snapshot_id from persona A raises ``PersonaSnapshotNotFound`` when
    used with persona B (cross-persona isolation, D-PP-10)."""
    from atomic_agents.exceptions import PersonaSnapshotNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("persona-a", _make_persona(identity="A body."))
    backend.save_persona("persona-b", _make_persona(identity="B body."))

    snap_id_a = backend.snapshot("persona-a")

    with pytest.raises(PersonaSnapshotNotFound):
        backend.restore("persona-b", snap_id_a)


def test_restore_path_confinement_invalid_snapshot_id_raises(
    tmp_path: Path,
) -> None:
    """``restore()`` with a malformed snapshot_id raises ``PersonaSnapshotNotFound``
    before any filesystem access (path-traversal defense via charset validator)."""
    from atomic_agents.exceptions import PersonaSnapshotNotFound
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona())

    with pytest.raises(PersonaSnapshotNotFound):
        backend.restore("p", "../../../etc/passwd")

    with pytest.raises(PersonaSnapshotNotFound):
        backend.restore("p", "not-a-valid-snap-id")

    with pytest.raises(PersonaSnapshotNotFound):
        backend.restore("p", "")


def test_atomic_rename_rollback_on_crash_mid_write(tmp_path: Path) -> None:
    """A simulated crash during the temp-dir rename leaves no partial state
    under ``.snapshots/``; the persona record is unchanged.

    Monkeypatches ``Path.rename`` to raise ``OSError`` on every attempt inside
    the snapshot() call. Verifies that ``.snapshots/`` is not created (or
    remains empty) and the persona is still loadable.
    """
    from atomic_agents.exceptions import PersonaError
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("p", _make_persona(identity="Safe body."))

    real_rename = Path.rename
    call_count = [0]

    def _always_fail_rename(self, target):
        # Allow renames not targeting .snapshots/ (e.g. internal tmp cleanup).
        if ".snapshots" in str(target):
            call_count[0] += 1
            raise OSError("Simulated snapshot rename failure")
        return real_rename(self, target)

    with patch.object(Path, "rename", _always_fail_rename):
        with pytest.raises((OSError, PersonaError)):
            backend.snapshot("p")

    # Persona must still be loadable and unchanged.
    loaded = backend.load_persona("p")
    assert loaded.identity == "Safe body."

    # No complete snapshot dir should exist (temp dirs auto-cleaned on exit).
    snapshots_root = tmp_path / "p" / ".snapshots"
    if snapshots_root.exists():
        completed = [
            d
            for d in snapshots_root.iterdir()
            if d.is_dir() and d.name.startswith("snap_")
        ]
        assert completed == [], f"Unexpected completed snapshot dirs found: {completed}"


# ─────────────────────────────────────────────────────────────────────────────
# P1 race: save_persona overwrite preserves snapshots when target already has
# a .snapshots/ entry (concurrent snapshot() between replace steps)


def test_save_persona_overwrite_preserves_snapshots_when_target_has_pre_existing_snapshots_dir(
    tmp_path: Path,
) -> None:
    """save_persona(overwrite=True) merges backup .snapshots/ entries individually
    into the new persona dir, so a concurrent snapshot() entry that already
    exists in the new dir does not destroy the backup's snapshot history.

    Simulates the race manually:
    1. Create persona 'alice' and take a real snapshot via snapshot(). Capture its id.
    2. Manually create a fake .snapshots/ entry in alice's current dir BEFORE calling
       save_persona -- this simulates Thread B writing a snapshot between the
       persona-dir-replace step (tmp -> alice) and the .snapshots restore step.
    3. Call save_persona('alice', modified, overwrite=True).
    4. Assert BOTH the real snapshot AND the fake entry survive in list_snapshots().
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    original = _make_persona(identity="Original body.")
    backend.save_persona("alice", original)

    # Step 1: take a real snapshot and capture its id.
    snapshot_id_a = backend.snapshot("alice")

    # Step 2: inject a fake future snapshot directory into alice/.snapshots/
    # to simulate Thread B's concurrent snapshot() mid-race.
    fake_snap_id = "snap_2099-01-01T000000_aabbccddee00"
    fake_snap_dir = tmp_path / "alice" / ".snapshots" / fake_snap_id
    fake_snap_dir.mkdir(parents=True, exist_ok=True)
    # A real snapshot has metadata.json + body files. The fake just needs a
    # metadata.json so list_snapshots() counts it (body parse fails -> skipped).
    import json as _json

    (fake_snap_dir / "metadata.json").write_text(
        _json.dumps(
            {
                "snapshot_id": fake_snap_id,
                "persona_id": "alice",
                "label": "fake-concurrent",
                "created_at": "2099-01-01T00:00:00+00:00",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    # Body files required for list_snapshots() to include the entry.
    (fake_snap_dir / "IDENTITY.md").write_text("Fake identity.", encoding="utf-8")
    (fake_snap_dir / "SOUL.md").write_text("Fake soul.", encoding="utf-8")
    (fake_snap_dir / "USER.md").write_text("Fake user.", encoding="utf-8")

    # Step 3: overwrite alice with a modified persona.
    modified = _make_persona(identity="Modified body.", version=2)
    backend.save_persona("alice", modified, overwrite=True)

    # Step 4: both snapshots must survive.
    snapshots = backend.list_snapshots("alice")
    snap_ids = {s.snapshot_id for s in snapshots}
    assert snapshot_id_a in snap_ids, (
        f"Real snapshot {snapshot_id_a!r} was lost after overwrite save; "
        f"found: {snap_ids}"
    )
    assert fake_snap_id in snap_ids, (
        f"Concurrent snapshot {fake_snap_id!r} was lost after overwrite save; "
        f"found: {snap_ids}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P2: list_snapshots skips symlinked entries that escape the snapshots root


def test_list_snapshots_skips_symlinks_escaping_personas_root(tmp_path: Path) -> None:
    """list_snapshots() skips entries whose resolved path escapes .snapshots/.

    Creates a persona 'alice' with one legitimate snapshot, then injects an evil
    symlink under .snapshots/ pointing to /tmp (or tmp_path itself to avoid
    needing /tmp to exist with content). The evil entry must NOT appear in the
    list_snapshots() result.
    """
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    backend = FilesystemPersonaBackend(tmp_path)
    backend.save_persona("alice", _make_persona(identity="Legit body."))
    legit_snap_id = backend.snapshot("alice")

    # Create a directory outside the persona root that could hold attacker data.
    evil_target = tmp_path / "evil-outside-personas"
    evil_target.mkdir()
    (evil_target / "metadata.json").write_text(
        '{"snapshot_id": "snap_EVIL", "persona_id": "alice", '
        '"label": null, "created_at": "2099-01-01T00:00:00+00:00", '
        '"schema_version": 1}',
        encoding="utf-8",
    )
    (evil_target / "IDENTITY.md").write_text("Evil identity.", encoding="utf-8")
    (evil_target / "SOUL.md").write_text("Evil soul.", encoding="utf-8")
    (evil_target / "USER.md").write_text("Evil user.", encoding="utf-8")

    # Plant a symlink inside alice/.snapshots/ that points to the evil dir.
    snapshots_root = tmp_path / "alice" / ".snapshots"
    evil_link = snapshots_root / "snap_EVIL_SYMLINK"
    evil_link.symlink_to(evil_target)

    snapshots = backend.list_snapshots("alice")
    snap_ids = [s.snapshot_id for s in snapshots]

    # The legitimate snapshot must be present.
    assert legit_snap_id in snap_ids, (
        f"Legitimate snapshot {legit_snap_id!r} missing from list_snapshots(); "
        f"got {snap_ids}"
    )
    # The symlinked entry must NOT appear.
    assert "snap_EVIL" not in snap_ids, (
        f"Evil symlinked snapshot appeared in list_snapshots(): {snap_ids}"
    )
    assert len(snapshots) == 1, (
        f"Expected exactly 1 snapshot (the legitimate one), got {len(snapshots)}: "
        f"{snap_ids}"
    )

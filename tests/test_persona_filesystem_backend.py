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

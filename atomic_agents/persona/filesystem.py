"""FilesystemPersonaBackend -- personas_root reference impl (spec/33).

This is the default backend for single-host deployments. It stores persona
records as three markdown files (IDENTITY.md, SOUL.md, USER.md) plus a JSON
metadata sidecar under ``<personas_root>/<persona_id>/``.

Three surface promises hold across PR 1 -> PR 2:

1. **No personas_root, no problem.** When ``personas_root`` is absent or empty,
   ``list_personas()`` returns ``[]`` and ``exists()`` returns ``False``. Every
   existing agent that has no PersonaBackend configured continues to work
   unchanged.

2. **Path-traversal refused at the API boundary** (spec/33 implementer
   contract). Operator- or agent-controlled ``persona_id`` values are validated
   BEFORE any storage or dict access. ``_validate_persona_id`` permits
   ``[a-zA-Z0-9_.+@-]+`` and rejects path-traversal tokens, control characters,
   newlines, empty strings, and leading dots.

3. **Construction is side-effect-free** (spec/33 implementer contract).
   ``__init__`` records ``personas_root`` and nothing else. No stat, no
   directory walk, no env-var validation at construction time. The first method
   call that touches the filesystem does so lazily.

Storage layout (D4)::

    <personas_root>/<persona_id>/IDENTITY.md   -- identity body
    <personas_root>/<persona_id>/SOUL.md       -- soul body
    <personas_root>/<persona_id>/USER.md       -- user body
    <personas_root>/<persona_id>/metadata.json -- version, label, created_at, schema_version

Group-atomic write: ``save_persona`` writes all four files to a sibling temp
directory then renames the directory into place, so a mid-save crash leaves
either the old record intact (overwrite path) or no record at all (new path).

Capabilities: ``supports_save=True, supports_clone=True,
supports_snapshot=False, supports_subscribe=False, durable=True,
supports_templates=False``. The snapshot trio raises ``NotImplementedError``
in PR 1. PR 3 flips ``supports_snapshot=True`` and ships the filesystem
snapshot implementation.

URL factory ``make_filesystem_persona_backend_from_url`` accepts
``filesystem:///path`` scheme; refuses non-filesystem schemes, netloc,
fragments, duplicate query params, and unknown query params. Credentials are
redacted from all ``ValueError`` sites via ``_redact_url``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .types import Persona, PersonaCapabilities, PersonaSnapshot

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Validation patterns (zero I/O at module level)

# persona_id charset: alphanumeric + underscore + hyphen + dot + plus + at-sign.
# Mirrors PolicyBackend's _AGENT_NAME_PATTERN (D4 -- cross-Protocol uniformity).
# Rejects: path-traversal tokens (.., /, \\), leading dot, control chars,
# newlines, empty strings.
_PERSONA_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.+@-]+$")

# Control-character detector (0x00-0x1F + DEL 0x7F).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# ──────────────────────────────────────────────────────────────────────────────
# Validation helper


def _validate_persona_id(persona_id: str) -> None:
    """Validate a persona_id at the API boundary.

    Permits: ``[a-zA-Z0-9_.+@-]+`` (letters, digits, underscore, dot, plus,
    at-sign, hyphen). Covers the full range of identifiers operators use
    (e.g., "customer-support-v3", "analyst.v2", "ops@fleet").

    Rejects (path-traversal + injection defenses):
    - Non-string or empty string.
    - Leading dot (hidden-file filesystem traversal trick).
    - ``..`` anywhere (directory traversal).
    - ``/`` or ``\\`` (path separators).
    - Control characters or newlines (log injection + path-token splitting).
    - Anything not matching the allowed charset.

    Raises ``ValueError`` with a message naming the offending value and the
    rule violated. Called BEFORE any storage or dict access in every public
    method (spec/33 implementer contract).
    """
    if not isinstance(persona_id, str) or not persona_id:
        raise ValueError(f"persona_id must be a non-empty string; got {persona_id!r}")
    if persona_id.startswith("."):
        raise ValueError(f"persona_id must not start with '.'; got {persona_id!r}")
    if ".." in persona_id:
        raise ValueError(f"persona_id must not contain '..'; got {persona_id!r}")
    if "/" in persona_id or "\\" in persona_id:
        raise ValueError(
            f"persona_id must not contain path separators; got {persona_id!r}"
        )
    if _CONTROL_CHARS.search(persona_id):
        raise ValueError(
            f"persona_id must not contain control characters or newlines; "
            f"got {persona_id!r}"
        )
    if not _PERSONA_ID_PATTERN.match(persona_id):
        raise ValueError(f"persona_id must match [a-zA-Z0-9_.+@-]+; got {persona_id!r}")


# ──────────────────────────────────────────────────────────────────────────────
# URL redaction helper


def _redact_url(url: str, max_len: int = 64) -> str:
    """Strip credentials from a URL for safe inclusion in error messages.

    Replaces ``user:pass@`` in the authority section (scheme + authority,
    before the first path ``/``) with ``***@`` to avoid leaking secrets into
    logs or exception strings. Only redacts ``@`` in the authority portion;
    ``@`` in path components (e.g. ``/home/ops@fleet/personas``) is preserved.
    Truncates the full result to ``max_len`` characters.
    """
    if "://" not in url:
        return url[:max_len] + ("..." if len(url) > max_len else "")
    scheme, _, rest = url.partition("://")
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        _, _, host_part = authority.partition("@")
        rest_redacted = f"***@{host_part}"
    else:
        rest_redacted = authority
    if slash:
        rest_redacted = f"{rest_redacted}/{path}"
    return f"{scheme}://{rest_redacted}"[:max_len]


# ──────────────────────────────────────────────────────────────────────────────
# Serialization helpers


_METADATA_SCHEMA_VERSION = 1


def _persona_to_metadata(persona: Persona) -> dict:
    """Serialize the metadata fields of a Persona to a JSON-compatible dict."""
    return {
        "schema_version": _METADATA_SCHEMA_VERSION,
        "version": persona.version,
        "label": persona.label,
        "created_at": persona.created_at,
    }


def _load_persona_from_dir(persona_dir: Path, persona_id: str) -> Persona:
    """Read a Persona from its storage directory.

    Assumes the directory exists and contains the expected files. The caller
    is responsible for existence checks and for translating exceptions into
    the appropriate ``PersonaNotFound`` or ``PersonaCorrupted`` raises.

    Raises:
        FileNotFoundError: a required file is absent (caller translates to
            ``PersonaNotFound``).
        PersonaCorrupted: ``metadata.json`` is malformed, missing required
            keys, uses an unsupported schema_version, or a body file has
            non-UTF-8 bytes.
    """
    from ..exceptions import PersonaCorrupted

    try:
        identity = (persona_dir / "IDENTITY.md").read_text(encoding="utf-8")
        soul = (persona_dir / "SOUL.md").read_text(encoding="utf-8")
        user = (persona_dir / "USER.md").read_text(encoding="utf-8")
        meta_raw = (persona_dir / "metadata.json").read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PersonaCorrupted(
            f"Persona {persona_id!r} record contains non-UTF-8 bytes "
            f"under {persona_dir!r}: {exc}"
        ) from exc

    try:
        meta = json.loads(meta_raw)
        schema_version = meta.get("schema_version", 1)
        if schema_version != _METADATA_SCHEMA_VERSION:
            raise PersonaCorrupted(
                f"Persona {persona_id!r} metadata schema_version "
                f"{schema_version!r} is not supported by this release "
                f"(supported: {_METADATA_SCHEMA_VERSION})."
            )
        return Persona(
            identity=identity,
            soul=soul,
            user=user,
            version=meta["version"],
            label=meta.get("label"),
            created_at=meta["created_at"],
        )
    except (json.JSONDecodeError, KeyError) as exc:
        raise PersonaCorrupted(
            f"Persona {persona_id!r} record is corrupt under {persona_dir!r}: {exc}"
        ) from exc


def _write_persona_files(dest: Path, persona: Persona) -> None:
    """Write the four persona files directly into ``dest``.

    ``dest`` MUST already exist. Does not create or rename directories;
    callers handle directory lifecycle. All four files are written with
    plain ``write_text`` (not ``atomic_write``) because the caller
    guarantees group-atomicity at the directory level via a temp-dir +
    rename pattern.
    """
    (dest / "IDENTITY.md").write_text(persona.identity, encoding="utf-8")
    (dest / "SOUL.md").write_text(persona.soul, encoding="utf-8")
    (dest / "USER.md").write_text(persona.user, encoding="utf-8")
    (dest / "metadata.json").write_text(
        json.dumps(_persona_to_metadata(persona), indent=2), encoding="utf-8"
    )


def _save_persona_group_atomic(persona_dir: Path, persona: Persona) -> None:
    """Write a persona record to ``persona_dir`` with directory-level atomicity.

    For the ``overwrite=True`` path (persona_dir already exists): writes to a
    sibling temp dir, renames the old dir to a backup, renames the temp dir
    to persona_dir, then removes the backup. If the second rename fails the
    backup is restored so the old record survives.

    For the fresh-create path (persona_dir does not exist after the exclusive
    mkdir claim): writes directly into the already-created persona_dir (no
    temp dir needed because the dir is exclusively owned).

    Callers are responsible for the exclusive-mkdir claim (``overwrite=False``
    path) or for accepting last-writer-wins semantics (``overwrite=True`` path).
    """
    parent = persona_dir.parent
    persona_id = persona_dir.name

    if persona_dir.exists():
        with tempfile.TemporaryDirectory(
            prefix=f".{persona_id}.tmp-", dir=parent
        ) as tmp_str:
            tmp = Path(tmp_str)
            _write_persona_files(tmp, persona)
            backup = parent / f".{persona_id}.old-{uuid.uuid4().hex[:8]}"
            persona_dir.rename(backup)
            try:
                tmp.rename(persona_dir)
            except OSError:
                backup.rename(persona_dir)
                raise
        shutil.rmtree(backup, ignore_errors=True)
    else:
        persona_dir.mkdir(parents=True, exist_ok=True)
        _write_persona_files(persona_dir, persona)


# ──────────────────────────────────────────────────────────────────────────────
# Backend


class FilesystemPersonaBackend:
    """Reference ``PersonaBackend`` for filesystem-resident persona records.

    Stores persona records under ``<personas_root>/<persona_id>/`` with
    three markdown body files (IDENTITY.md, SOUL.md, USER.md) plus a JSON
    metadata sidecar (metadata.json). Construction is side-effect-free per
    the spec/33 implementer contract -- no stat, no walk, no env-var
    validation at ``__init__``.

    ``personas_root`` is NOT validated for existence at construction time.
    The first call that performs I/O creates the directory if needed (save)
    or returns the no-op value (list, exists, load).

    Concurrent access: ``save_persona`` uses ``_io.atomic_write`` (temp +
    fsync + rename) so concurrent saves on the same persona_id are safe;
    last-writer-wins is the documented semantics.

    Capabilities: ``supports_save=True, supports_clone=True,
    supports_snapshot=False, supports_subscribe=False, durable=True,
    supports_templates=False``. Snapshot trio raises ``NotImplementedError``
    in PR 1 (per capability declaration -- operators should check
    ``capabilities().supports_snapshot`` before calling snapshot/restore/
    list_snapshots).
    """

    backend_id = "filesystem"

    def __init__(self, personas_root: str | Path) -> None:
        """Construct without I/O.

        ``personas_root`` is recorded as a ``Path``; it is NOT validated for
        existence (spec/33 implementer contract). First method call that
        requires the directory creates it on write or returns no-op on read.

        Args:
            personas_root: directory under which persona subdirectories live.
                Need not exist at construction time; absence is handled
                gracefully (no-op reads, auto-create on save).
        """
        self._personas_root = Path(personas_root)

    # ── PersonaBackend Protocol surface ──────────────────────────────

    def load_persona(self, persona_id: str) -> Persona:
        """Load and return the ``Persona`` record for ``persona_id``.

        Reads IDENTITY.md, SOUL.md, USER.md, and metadata.json from
        ``<personas_root>/<persona_id>/``.

        Raises:
            PersonaNotFound: the persona directory or any required file is
                missing.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        from ..exceptions import PersonaNotFound

        _validate_persona_id(persona_id)
        persona_dir = self._personas_root / persona_id

        if not persona_dir.is_dir():
            raise PersonaNotFound(
                f"Persona {persona_id!r} not found under "
                f"{self._personas_root!r}. "
                f"Call save_persona() to create it first."
            )

        try:
            return _load_persona_from_dir(persona_dir, persona_id)
        except FileNotFoundError as exc:
            raise PersonaNotFound(
                f"Persona {persona_id!r} directory exists but is missing "
                f"required files under {persona_dir!r}: {exc}"
            ) from exc

    def save_persona(
        self, persona_id: str, persona: Persona, *, overwrite: bool = False
    ) -> None:
        """Persist ``persona`` under ``persona_id``.

        When ``overwrite=False`` (default), uses an atomic ``mkdir`` to claim
        the persona directory exclusively before writing. This eliminates the
        TOCTOU race between an ``is_dir()`` check and the first write: if two
        concurrent writers race, exactly one wins the mkdir and the other sees
        ``PersonaExists``.

        When ``overwrite=True``, writes all four files to a sibling temp
        directory and renames it into place in a swap-and-delete sequence so
        a mid-save crash leaves the old record intact.

        Raises:
            PersonaExists: ``persona_id`` already exists and
                ``overwrite=False``.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        from ..exceptions import PersonaExists

        _validate_persona_id(persona_id)
        persona_dir = self._personas_root / persona_id

        if not overwrite:
            try:
                persona_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise PersonaExists(
                    f"Persona {persona_id!r} already exists under "
                    f"{self._personas_root!r}. "
                    f"Pass overwrite=True to replace it."
                ) from None
            _write_persona_files(persona_dir, persona)
        else:
            persona_dir.parent.mkdir(parents=True, exist_ok=True)
            _save_persona_group_atomic(persona_dir, persona)

    def list_personas(self) -> list[str]:
        """Return sorted list of known persona_ids.

        Returns ``[]`` when ``personas_root`` does not exist or is empty.
        Skips entries that are not directories, whose names start with a
        dot (internal directories like ``.snapshots`` are not personas), or
        that do not contain an IDENTITY.md sentinel file (incomplete or
        externally-mutated dirs are excluded silently).
        """
        if not self._personas_root.is_dir():
            return []
        ids = [
            entry.name
            for entry in self._personas_root.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and (entry / "IDENTITY.md").is_file()
        ]
        return sorted(ids)

    def exists(self, persona_id: str) -> bool:
        """Return ``True`` if ``persona_id`` is known to this backend.

        Raises:
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        _validate_persona_id(persona_id)
        return (self._personas_root / persona_id).is_dir()

    def clone(
        self,
        source_id: str,
        target_id: str,
        overrides: dict | None = None,
    ) -> None:
        """Clone the persona at ``source_id`` to ``target_id``.

        Copies all fields from the source persona. When ``overrides`` is
        supplied, each key-value pair in the dict is applied on top of the
        copied fields before persisting.

        Raises:
            PersonaNotFound: ``source_id`` is not known to this backend.
            PersonaExists: ``target_id`` already exists.
            ValueError: either id fails charset / path-traversal validation.
        """

        _validate_persona_id(source_id)
        _validate_persona_id(target_id)

        source = self.load_persona(source_id)  # raises PersonaNotFound if absent

        if overrides:
            import dataclasses

            source = dataclasses.replace(source, **overrides)

        self.save_persona(target_id, source, overwrite=False)  # raises PersonaExists

    def snapshot(self, persona_id: str, label: str | None = None) -> str:
        """Raise ``NotImplementedError`` -- snapshot trio deferred to PR 3.

        ``capabilities().supports_snapshot`` is ``False`` in PR 1.
        Callers SHOULD check capabilities before calling this method.

        Raises:
            NotImplementedError: always in PR 1.
        """
        _validate_persona_id(persona_id)
        raise NotImplementedError(
            "FilesystemPersonaBackend.snapshot() is not implemented in PR 1 "
            "(capabilities().supports_snapshot=False). "
            "The filesystem snapshot trio ships in PR 3 of #62."
        )

    def restore(self, persona_id: str, snapshot_id: str) -> None:
        """Raise ``NotImplementedError`` -- snapshot trio deferred to PR 3.

        ``capabilities().supports_snapshot`` is ``False`` in PR 1.
        Callers SHOULD check capabilities before calling this method.

        Raises:
            NotImplementedError: always in PR 1.
        """
        _validate_persona_id(persona_id)
        raise NotImplementedError(
            "FilesystemPersonaBackend.restore() is not implemented in PR 1 "
            "(capabilities().supports_snapshot=False). "
            "The filesystem snapshot trio ships in PR 3 of #62."
        )

    def list_snapshots(self, persona_id: str) -> list[PersonaSnapshot]:
        """Raise ``NotImplementedError`` -- snapshot trio deferred to PR 3.

        ``capabilities().supports_snapshot`` is ``False`` in PR 1.
        Callers SHOULD check capabilities before calling this method.

        Raises:
            NotImplementedError: always in PR 1.
        """
        _validate_persona_id(persona_id)
        raise NotImplementedError(
            "FilesystemPersonaBackend.list_snapshots() is not implemented "
            "in PR 1 (capabilities().supports_snapshot=False). "
            "The filesystem snapshot trio ships in PR 3 of #62."
        )

    def capabilities(self) -> PersonaCapabilities:
        """Return the backend capability snapshot.

        ``supports_snapshot=False`` in PR 1 -- snapshot trio ships in PR 3.
        ``durable=True`` -- filesystem is a durable storage substrate.
        ``supports_templates=False`` -- template marketplace is v1.1+.

        This method is side-effect-free and does NOT touch the filesystem.
        """
        return PersonaCapabilities(
            supports_save=True,
            supports_clone=True,
            supports_snapshot=False,
            supports_subscribe=False,
            durable=True,
            supports_templates=False,
        )


# ──────────────────────────────────────────────────────────────────────────────
# URL factory


def make_filesystem_persona_backend_from_url(url: str) -> FilesystemPersonaBackend:
    """Construct a ``FilesystemPersonaBackend`` from a ``filesystem://`` URL.

    Per spec/33 implementer contract, does NOT validate path existence at
    construction. Per spec/33 implementer contract, raises ``ValueError`` with
    credentials redacted when the URL is malformed or uses the wrong scheme.

    URL format::

        filesystem:///absolute/path/to/personas_root

    The triple-slash convention is standard for local filesystem URLs:
    ``filesystem://`` (scheme + empty authority) + ``/abs/path`` (absolute
    path starting with ``/``).

    No query params, netloc, or fragment components are accepted.

    Args:
        url: a ``filesystem:///path`` URL string.

    Returns:
        A ``FilesystemPersonaBackend`` targeting the given personas_root.

    Raises:
        ValueError: if ``url`` is not a string, is empty, is missing the
            ``://`` separator, uses a non-``filesystem`` scheme, has a
            netloc component, has a fragment, has unknown query params, has
            duplicate query params, or has a relative path component.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("URL must be a non-empty string")

    if "://" not in url:
        raise ValueError(f"URL missing scheme separator '://': {_redact_url(url)!r}")

    parsed = urlparse(url)

    if parsed.scheme.lower() != "filesystem":
        raise ValueError(
            f"Expected 'filesystem://' scheme; got scheme {parsed.scheme!r} "
            f"from URL {_redact_url(url)!r}"
        )

    if parsed.netloc:
        raise ValueError(
            f"filesystem:// URL must not have a netloc (host) component; "
            f"got netloc {parsed.netloc!r} from URL {_redact_url(url)!r}"
        )

    if parsed.fragment:
        raise ValueError(
            f"filesystem:// URL must not have a fragment component; "
            f"got fragment {parsed.fragment!r} from URL {_redact_url(url)!r}"
        )

    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        # Detect duplicate query params (parse_qs keeps lists; len > 1 means dupe)
        for key, values in params.items():
            if len(values) > 1:
                raise ValueError(
                    f"filesystem:// URL has duplicate query param {key!r} "
                    f"from URL {_redact_url(url)!r}"
                )
        # No known query params for filesystem backend -- reject all
        raise ValueError(
            f"filesystem:// URL does not accept query params; "
            f"got {parsed.query!r} from URL {_redact_url(url)!r}"
        )

    path = parsed.path
    if not path.startswith("/"):
        raise ValueError(
            f"filesystem:// URL must use an absolute path (starting with '/'); "
            f"got {_redact_url(url)!r}"
        )

    return FilesystemPersonaBackend(Path(path))

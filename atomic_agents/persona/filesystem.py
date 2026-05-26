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
from .._io import safe_resolve_under

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
        # Reject non-integer types: bool is a subclass of int in Python
        # (True == 1), so it must be excluded explicitly. float, string,
        # None, list, and dict are all wrong types from a JSON-schema
        # perspective.
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise PersonaCorrupted(
                f"Persona {persona_id!r} metadata.json schema_version must be a "
                f"JSON integer; got {type(schema_version).__name__} "
                f"{schema_version!r}."
            )
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


def _save_persona_group_atomic(
    personas_root: Path, persona_id: str, persona: Persona
) -> None:
    """Group-atomic save of a persona record via temp-dir-and-rename.

    Writes all four files to a sibling temp directory, then either renames
    the temp dir into place (fresh create) OR swaps with the existing dir
    via backup-and-rename (overwrite of existing record). The retry loop
    handles the concurrent-overwrite race where another writer may rename
    persona_dir out from under us between our state check and the rename.

    On POSIX, directory rename is atomic only when destination is empty.
    ``tempfile.TemporaryDirectory`` ensures the temp dir is unique per call;
    ``uuid`` hex ensures backup names don't collide.

    Concurrent overwrite=True is last-writer-wins. Concurrent fresh-create
    is exactly-one-wins (the loser sees FileExistsError translated to
    PersonaExists at the save_persona level).
    """
    from ..exceptions import PersonaError, PersonaExists

    persona_dir = personas_root / persona_id
    parent = persona_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=f".{persona_id}.tmp-", dir=parent
    ) as tmp_str:
        tmp = Path(tmp_str)
        _write_persona_files(tmp, persona)

        # 20 attempts bounds concurrent contention. Beyond 20, raise so the
        # caller can decide retry policy.
        for _attempt in range(20):
            if persona_dir.is_dir():
                # Existing record: swap-and-delete
                backup = parent / f".{persona_id}.old-{uuid.uuid4().hex[:8]}"
                try:
                    persona_dir.rename(backup)
                except FileNotFoundError:
                    # Another writer renamed persona_dir out. Loop back;
                    # the next iteration will see persona_dir missing and
                    # go through the fresh-create path.
                    continue
                try:
                    tmp.rename(persona_dir)
                except OSError:
                    # Another writer placed a directory at persona_dir in
                    # the window between our rename-out and our rename-in.
                    # Restore the backup so the old record survives, then
                    # loop back to try again. If the restore also races
                    # (yet another writer placed a dir at persona_dir),
                    # best-effort remove the backup so it doesn't leak as
                    # an orphan with sensitive persona content.
                    try:
                        backup.rename(persona_dir)
                    except OSError:
                        shutil.rmtree(backup, ignore_errors=True)
                    continue
                shutil.rmtree(backup, ignore_errors=True)
                return
            elif persona_dir.is_file():
                # A regular file (not a directory) exists stably at persona_dir.
                # Refuse rather than silently coercing a file into a persona dir.
                # Using is_file() rather than exists() avoids a false-positive
                # during the concurrent-rename window where a directory is
                # briefly invisible to is_dir() but visible to exists().
                raise PersonaExists(
                    f"Cannot overwrite {persona_dir!r}: not a directory. "
                    f"Remove the existing file manually before saving "
                    f"a persona with this id."
                )
            else:
                # Fresh create: atomic rename. If another writer raced us
                # and created the dir between is_dir() and rename(), retry
                # through the existing-dir branch.
                try:
                    tmp.rename(persona_dir)
                    return
                except OSError:
                    continue

    raise PersonaError(
        f"save_persona({persona_id!r}, overwrite=True) failed after "
        f"20 retries due to concurrent contention. The race target "
        f"is a temp-dir-rename swap; try again."
    )


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

    Concurrent access: ``save_persona`` writes all four persona files to a
    sibling temp directory and renames it into place via a swap-and-delete
    sequence (POSIX-atomic for the rename step). Concurrent fresh-create
    on the same persona_id is exactly-one-wins via ``mkdir(exist_ok=False)``
    on the ``overwrite=False`` path. Concurrent overwrite=True is
    last-writer-wins (the retry loop bounds contention to twenty attempts;
    beyond that a ``PersonaError`` surfaces so the caller can choose a
    retry policy).

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

        from ..exceptions import PathTraversalError, PersonaCorrupted

        _required_files = ["IDENTITY.md", "SOUL.md", "USER.md", "metadata.json"]
        for fname in _required_files:
            try:
                safe_resolve_under(persona_dir / fname, self._personas_root)
            except (PathTraversalError, ValueError) as exc:
                raise PersonaCorrupted(
                    f"Persona {persona_id!r} file {fname!r} resolves outside "
                    f"personas_root {self._personas_root!r}: {exc}"
                ) from exc

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
            _save_persona_group_atomic(self._personas_root, persona_id, persona)

    def list_personas(self) -> list[str]:
        """Return sorted list of known persona_ids.

        Returns ``[]`` when ``personas_root`` does not exist or is empty.
        Skips entries that are not directories, whose names start with a
        dot (internal directories like ``.snapshots`` are not personas), or
        that do not contain an IDENTITY.md sentinel file (incomplete or
        externally-mutated dirs are excluded silently).

        The IDENTITY.md sentinel is validated via ``safe_resolve_under`` to
        refuse symlinks planted in personas_root that point outside the root.
        Only a regular, non-symlink IDENTITY.md whose resolved path stays
        inside personas_root qualifies the entry as a valid persona.
        """
        from ..exceptions import PathTraversalError

        if not self._personas_root.is_dir():
            return []
        ids = []
        for entry in self._personas_root.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            identity = entry / "IDENTITY.md"
            try:
                resolved = safe_resolve_under(identity, self._personas_root)
            except (PathTraversalError, ValueError):
                continue
            if not resolved.is_file():
                continue
            ids.append(entry.name)
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

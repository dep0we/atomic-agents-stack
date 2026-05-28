"""FilesystemPersonaBackend -- personas_root reference impl (spec/33).

This is the default backend for single-host deployments. It stores persona
records as three markdown files (IDENTITY.md, SOUL.md, USER.md) plus a JSON
metadata sidecar under ``<personas_root>/<persona_id>/``.

Three surface promises hold across PR 1 -> PR 3:

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

Storage layout (D4 + D-PP-10)::

    <personas_root>/<persona_id>/IDENTITY.md   -- identity body
    <personas_root>/<persona_id>/SOUL.md       -- soul body
    <personas_root>/<persona_id>/USER.md       -- user body
    <personas_root>/<persona_id>/metadata.json -- version, label, created_at, schema_version

    Snapshot layout (D-PP-10)::

    <personas_root>/<persona_id>/.snapshots/<snapshot_id>/IDENTITY.md
    <personas_root>/<persona_id>/.snapshots/<snapshot_id>/SOUL.md
    <personas_root>/<persona_id>/.snapshots/<snapshot_id>/USER.md
    <personas_root>/<persona_id>/.snapshots/<snapshot_id>/metadata.json

    The dot-prefixed ``.snapshots/`` dir is skipped by ``list_personas()``
    via the existing ``entry.name.startswith(".")`` filter. Snapshot metadata
    carries ``{snapshot_id, persona_id, label, created_at}`` (D-PP-11).

Group-atomic write: ``save_persona`` writes all four files to a sibling temp
directory then renames the directory into place, so a mid-save crash leaves
either the old record intact (overwrite path) or no record at all (new path).

``snapshot()`` uses the same temp-dir-and-rename pattern: writes IDENTITY.md,
SOUL.md, USER.md, and metadata.json into a sibling temp dir named
``.tmp-<snapshot_id>-<uuid>``, then renames it into
``.snapshots/<snapshot_id>/``. The 20-iteration retry bound handles macOS APFS
ENOTEMPTY semantics (spec/33 MUST #5).

Capabilities: ``supports_save=True, supports_clone=True,
supports_snapshot=True, supports_subscribe=False, durable=True,
supports_templates=False``.

URL factory ``make_filesystem_persona_backend_from_url`` accepts
``filesystem:///path`` scheme; refuses non-filesystem schemes, netloc,
fragments, duplicate query params, and unknown query params. Credentials are
redacted from all ``ValueError`` sites via ``_redact_url``.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import tempfile
import uuid
from datetime import datetime
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


# Snapshot IDs are generated by ``snapshot()`` as
# ``snap_<YYYY-MM-DDTHHMMSS>_<12hex>`` (D-PP-11). The validator refuses
# operator-supplied IDs that don't match this shape. Mirrors the identical
# helper at ``atomic_agents/profile/filesystem.py:1085-1107``.
# Allows: digits, letters, hyphen, underscore, colon (for tz offset like
# ``+05:30``), plus. Refuses everything else including ``/``, ``\\``, ``..``,
# NULL bytes, control chars.
_VALID_PERSONA_SNAPSHOT_ID = re.compile(r"^snap_[\w\-T:+]+$")


def _validate_persona_snapshot_id(snapshot_id: str) -> None:
    """Reject ``snapshot_id`` values that don't match the generated shape.

    Belt-and-suspenders against path-traversal. The ``relative_to`` check
    in ``restore()`` is the actual security boundary, but refusing malformed
    IDs up front gives a cleaner error message and blocks attempts before
    any filesystem access.

    Raises ``PersonaSnapshotNotFound`` (not ValueError) so callers can catch
    a single exception type for "snapshot doesn't exist / can't be reached"
    cases. Mirrors ``_validate_snapshot_id`` in ``profile/filesystem.py``.

    Raises:
        PersonaSnapshotNotFound: snapshot_id is empty or does not match the
            ``snap_<timestamp>_<hex>`` shape.
    """
    from ..exceptions import PersonaSnapshotNotFound

    if not snapshot_id:
        raise PersonaSnapshotNotFound("snapshot_id must not be empty")
    if not _VALID_PERSONA_SNAPSHOT_ID.match(snapshot_id):
        raise PersonaSnapshotNotFound(
            f"snapshot_id {snapshot_id!r} is not a valid snapshot id. "
            f"Expected snap_<timestamp>_<hex> shape generated by snapshot()."
        )


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


def _load_persona_from_snapshot_dir(
    snapshot_dir: Path, persona_id: str, metadata: dict
) -> Persona:
    """Read the Persona body from a snapshot directory.

    Reads IDENTITY.md, SOUL.md, USER.md from ``snapshot_dir`` and constructs
    a ``Persona`` using the ``persona_version``, ``persona_label``, and
    ``persona_created_at`` fields from ``metadata``. These fields are written
    into the snapshot metadata.json by ``snapshot()`` at capture time so the
    persona record can be restored with the original field values.

    Called by both ``restore()`` and ``list_snapshots()``. Callers translate
    exceptions into ``PersonaSnapshotNotFound`` as appropriate.

    Args:
        snapshot_dir: path to the snapshot directory.
        persona_id: the persona this snapshot belongs to (for error context).
        metadata: parsed metadata.json dict from the snapshot directory.

    Raises:
        OSError: a required body file is absent or unreadable.
        UnicodeDecodeError: a body file contains non-UTF-8 bytes.
    """
    identity = (snapshot_dir / "IDENTITY.md").read_text(encoding="utf-8")
    soul = (snapshot_dir / "SOUL.md").read_text(encoding="utf-8")
    user = (snapshot_dir / "USER.md").read_text(encoding="utf-8")
    # persona_version, persona_label, persona_created_at are stored in the
    # snapshot metadata.json at snapshot() time so restore() can reconstruct
    # the full Persona with original field values. Fall back to sensible
    # defaults for snapshots written by older code or test fixtures that
    # omit these optional fields.
    return Persona(
        identity=identity,
        soul=soul,
        user=user,
        version=int(metadata.get("persona_version", 1)),
        label=metadata.get("persona_label"),
        created_at=str(
            metadata.get("persona_created_at", metadata.get("created_at", ""))
        ),
    )


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

    Snapshot preservation (D-PP-10): when the existing persona dir contains a
    ``.snapshots/`` subdirectory, it is moved from the backup back into the
    new persona dir BEFORE the backup is deleted. This keeps the snapshot
    history intact across ``save_persona(overwrite=True)`` calls, since the
    temp dir used for the new record has no ``.snapshots/`` dir of its own.

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
                # Snapshot preservation (D-PP-10): if the backup has a
                # .snapshots/ dir, merge its entries individually into the
                # new persona dir so snapshot history survives overwrite saves.
                #
                # A single-dir rename (the previous approach) races with a
                # concurrent snapshot() that may have placed a .snapshots/
                # entry in the new persona_dir between the persona-dir-replace
                # step and this restore step. That concurrent entry causes
                # rename() to fail with ENOTEMPTY (macOS/Linux), and the
                # old code logged a warning + proceeded -- step 4 (rmtree of
                # backup) then destroyed the entire snapshot history.
                #
                # Fix: move each backup snapshot entry individually into the
                # target. The 48-bit entropy on snapshot_ids (D-PP-11) makes
                # collisions with a concurrent snapshot's just-written entry
                # probabilistically impossible (~6e-8 at 4K snapshots/sec).
                backup_snapshots = backup / ".snapshots"
                if backup_snapshots.is_dir():
                    target_snapshots = persona_dir / ".snapshots"
                    target_snapshots.mkdir(exist_ok=True)
                    for entry in backup_snapshots.iterdir():
                        dest = target_snapshots / entry.name
                        if dest.exists():
                            # Same snapshot_id already in target -- should not
                            # happen under 48-bit entropy. Log and skip to
                            # preserve the invariant "snapshots are never
                            # destroyed by save_persona".
                            _logger.warning(
                                "save_persona_group_atomic_snapshot_collision "
                                "backup=%s target=%s",
                                entry,
                                dest,
                            )
                            continue
                        try:
                            entry.rename(dest)
                        except OSError:
                            # Cross-device or filesystem-specific error.
                            # Fall back to shutil.move for robustness.
                            shutil.move(str(entry), str(dest))
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
    supports_snapshot=True, supports_subscribe=False, durable=True,
    supports_templates=False``.
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
        """Capture a snapshot of ``persona_id`` and return the snapshot_id.

        Loads the current persona record, then writes IDENTITY.md, SOUL.md,
        USER.md, and metadata.json into a temp dir under
        ``<personas_root>/<persona_id>/.snapshots/`` and renames it into
        place via a group-atomic rename. Mirrors the ``save_persona`` 20-retry
        pattern for macOS APFS ENOTEMPTY semantics (spec/33 MUST #5).

        Snapshot ID format (D-PP-11): ``snap_<YYYY-MM-DDTHHMMSS>_<12hex>``
        where ``<12hex>`` is ``secrets.token_hex(6)`` (48-bit entropy). The
        format matches the AgentProfile snapshot ID shape from spec/24 for
        cross-Protocol uniformity.

        Storage layout (D-PP-10)::

            <personas_root>/<persona_id>/.snapshots/<snapshot_id>/IDENTITY.md
            <personas_root>/<persona_id>/.snapshots/<snapshot_id>/SOUL.md
            <personas_root>/<persona_id>/.snapshots/<snapshot_id>/USER.md
            <personas_root>/<persona_id>/.snapshots/<snapshot_id>/metadata.json

        Returns:
            The backend-issued snapshot_id string.

        Raises:
            PersonaNotFound: ``persona_id`` is not known to this backend.
            PersonaError: group-atomic rename failed after 20 retries.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        from ..exceptions import PersonaError

        _validate_persona_id(persona_id)
        # load_persona raises PersonaNotFound if the persona does not exist.
        persona = self.load_persona(persona_id)

        # D-PP-11: snapshot_id format matches AgentProfile shape.
        snapshot_id = (
            f"snap_"
            f"{datetime.now().astimezone().strftime('%Y-%m-%dT%H%M%S')}_"
            f"{secrets.token_hex(6)}"
        )
        created_at = datetime.now().astimezone().isoformat()

        persona_dir = self._personas_root / persona_id
        snapshots_dir = persona_dir / ".snapshots"
        snapshot_dest = snapshots_dir / snapshot_id
        parent = snapshots_dir

        # metadata.json schema per D-PP-11: {snapshot_id, persona_id, label,
        # created_at}. persona_id stored explicitly for defensive cross-persona
        # isolation checks at restore() time. Also stores persona version and
        # persona_created_at (the persona record's created_at at snapshot time)
        # so restore() can reconstruct the Persona with the original field values.
        snapshot_metadata = {
            "snapshot_id": snapshot_id,
            "persona_id": persona_id,
            "label": label,
            "created_at": created_at,
            "persona_version": persona.version,
            "persona_label": persona.label,
            "persona_created_at": persona.created_at,
        }

        with tempfile.TemporaryDirectory(
            prefix=f".tmp-{snapshot_id}-", dir=self._personas_root / persona_id
        ) as tmp_str:
            tmp = Path(tmp_str)
            # Write the three body files (IDENTITY.md, SOUL.md, USER.md).
            # _write_persona_files also writes a persona-record metadata.json;
            # we overwrite it immediately with the snapshot metadata.json so
            # only one metadata.json (the snapshot one) ends up in the temp dir.
            _write_persona_files(tmp, persona)
            # Overwrite the persona metadata.json with snapshot metadata.json.
            (tmp / "metadata.json").write_text(
                json.dumps(snapshot_metadata, indent=2), encoding="utf-8"
            )

            parent.mkdir(parents=True, exist_ok=True)

            # 20-attempt retry loop (spec/33 MUST #5 -- macOS APFS ENOTEMPTY).
            for _attempt in range(20):
                if snapshot_dest.is_dir():
                    # Snapshot_id collision (astronomically unlikely with 48-bit
                    # entropy). Do not retry -- the probability of a same-second
                    # same-hex collision is ~6e-8 at 4K snapshots/sec; if it
                    # somehow happens, surface it loudly rather than silently
                    # overwriting a prior snapshot.
                    raise PersonaError(
                        f"snapshot id {snapshot_id!r} already exists for persona "
                        f"{persona_id!r}. This is an extremely rare collision; "
                        f"retry the operation."
                    )
                try:
                    tmp.rename(snapshot_dest)
                    return snapshot_id
                except OSError:
                    # Another writer or concurrent rename in progress. Loop back.
                    continue

        raise PersonaError(
            f"snapshot({persona_id!r}) failed after 20 retries due to concurrent "
            f"contention on the temp-dir rename. Try again."
        )

    def restore(self, persona_id: str, snapshot_id: str) -> None:
        """Restore ``persona_id`` to the state captured in ``snapshot_id``.

        Validates ``persona_id`` and ``snapshot_id`` at the API boundary,
        then resolves the snapshot directory path and verifies it is confined
        under ``<personas_root>/<persona_id>/`` (defense-in-depth against
        symlink traversal, even when the charset validator has already run).

        Reads IDENTITY.md, SOUL.md, USER.md, and metadata.json from the
        snapshot directory. Verifies ``metadata.persona_id`` equals
        ``persona_id`` for cross-persona isolation (D-PP-10: a snapshot id
        from persona A raises ``PersonaSnapshotNotFound`` when restored to
        persona B). Then calls ``save_persona(overwrite=True)`` using the
        snapshot body files to restore the persona record atomically.

        Security checks (path-scoping + cross-persona isolation):

        1. ``persona_id`` validated against ``_validate_persona_id`` -- refuses
           path-traversal at the API boundary.
        2. ``snapshot_id`` validated against ``_validate_persona_snapshot_id`` --
           refuses malformed IDs before any filesystem access.
        3. Snapshot directory path resolved + ``relative_to`` checked under
           ``<personas_root>/<persona_id>/`` -- refuses symlink escapes.
        4. ``metadata.persona_id`` MUST equal ``persona_id`` -- defensive
           double-check on top of the path-scope check.

        Raises:
            PersonaNotFound: ``persona_id`` is not known to this backend.
            PersonaSnapshotNotFound: ``snapshot_id`` is not found for
                ``persona_id``, has a malformed id, or fails path-scope check.
            ValueError: ``persona_id`` fails charset / path-traversal
                validation.
        """
        from ..exceptions import PersonaNotFound, PersonaSnapshotNotFound

        _validate_persona_id(persona_id)
        _validate_persona_snapshot_id(snapshot_id)

        persona_dir = self._personas_root / persona_id

        # Verify the persona exists before reading any snapshot.
        if not persona_dir.is_dir():
            raise PersonaNotFound(
                f"Persona {persona_id!r} not found under "
                f"{self._personas_root!r}. "
                f"Cannot restore a snapshot for a non-existent persona."
            )

        snapshot_dir = persona_dir / ".snapshots" / snapshot_id

        # Path-scope check: resolved snapshot_dir MUST be under persona_dir.
        # Catches symlink escapes that the snapshot_id validator can't see.
        try:
            snapshot_dir.resolve().relative_to(persona_dir.resolve())
        except (ValueError, OSError) as exc:
            raise PersonaSnapshotNotFound(
                f"snapshot {snapshot_id!r} for persona {persona_id!r} "
                f"resolves outside persona directory."
            ) from exc

        metadata_path = snapshot_dir / "metadata.json"
        if not snapshot_dir.is_dir() or not metadata_path.is_file():
            raise PersonaSnapshotNotFound(
                f"snapshot {snapshot_id!r} not found for persona "
                f"{persona_id!r} at {snapshot_dir}."
            )

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise PersonaSnapshotNotFound(
                f"snapshot {snapshot_id!r} metadata unreadable for persona "
                f"{persona_id!r}: {exc}"
            ) from exc

        # Cross-persona isolation (D-PP-10): defensive double-check on top of
        # the path-scope check. If the metadata claims a different persona than
        # the directory it lives in, refuse.
        if metadata.get("persona_id") != persona_id:
            raise PersonaSnapshotNotFound(
                f"snapshot {snapshot_id!r} metadata persona_id "
                f"{metadata.get('persona_id')!r} does not match requested "
                f"persona {persona_id!r}."
            )

        # Read body files from the snapshot directory.
        try:
            restored_persona = _load_persona_from_snapshot_dir(
                snapshot_dir, persona_id, metadata
            )
        except (OSError, UnicodeDecodeError, KeyError) as exc:
            raise PersonaSnapshotNotFound(
                f"snapshot {snapshot_id!r} body files unreadable for persona "
                f"{persona_id!r}: {exc}"
            ) from exc

        # Write the restored persona back via the existing group-atomic path.
        _save_persona_group_atomic(self._personas_root, persona_id, restored_persona)

    def list_snapshots(self, persona_id: str) -> list[PersonaSnapshot]:
        """Return snapshots for ``persona_id`` in chronological order.

        Enumerates ``<personas_root>/<persona_id>/.snapshots/`` and reads
        each subdirectory's ``metadata.json`` and body files. Returns ``[]``
        when the ``.snapshots`` dir is absent (no snapshots yet taken for
        this persona). Snapshots with unreadable or missing metadata/body
        files are silently skipped -- they are effectively dead.

        Returns snapshots in ascending ``created_at`` order (ISO-8601
        lexicographic order equals chronological order for tz-aware
        timestamps).

        Raises:
            PersonaNotFound: ``persona_id`` is not known to this backend.
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
                f"Cannot list snapshots for a non-existent persona."
            )

        snapshots_root = persona_dir / ".snapshots"
        if not snapshots_root.is_dir():
            return []

        results: list[PersonaSnapshot] = []
        for entry in snapshots_root.iterdir():
            if not entry.is_dir():
                continue
            # Path-scope check: defense-in-depth against symlinks or other
            # filesystem conditions that cause an entry to resolve outside
            # the snapshots root. Matches the guard already present in
            # restore(). Entries escaping the root are silently skipped.
            try:
                entry.resolve().relative_to(snapshots_root.resolve())
            except (ValueError, OSError):
                continue
            metadata_path = entry / "metadata.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Skip corrupt metadata -- dead snapshot; operator can rm -rf
                # to clean.
                continue
            try:
                snap_persona = _load_persona_from_snapshot_dir(
                    entry, persona_id, metadata
                )
            except (OSError, UnicodeDecodeError, KeyError):
                # Skip unreadable snapshot body files.
                continue
            results.append(
                PersonaSnapshot(
                    snapshot_id=str(metadata.get("snapshot_id", entry.name)),
                    persona_id=str(metadata.get("persona_id", persona_id)),
                    label=metadata.get("label"),
                    created_at=str(metadata.get("created_at", "")),
                    persona=snap_persona,
                )
            )
        # Sort by created_at (ISO-8601 lexicographic == chronological for
        # tz-aware timestamps).
        results.sort(key=lambda s: s.created_at)
        return results

    def capabilities(self) -> PersonaCapabilities:
        """Return the backend capability snapshot.

        ``supports_snapshot=True`` -- filesystem snapshot trio is implemented.
        ``durable=True`` -- filesystem is a durable storage substrate.
        ``supports_templates=False`` -- template marketplace is v1.1+.

        This method is side-effect-free and does NOT touch the filesystem.
        """
        return PersonaCapabilities(
            supports_save=True,
            supports_clone=True,
            supports_snapshot=True,
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

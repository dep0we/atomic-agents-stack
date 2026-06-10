"""FilesystemCorpusBackend -- default CorpusBackend reference impl (spec/34).

Walks ``<agent_root>/wiki/`` and ``<agent_root>/raw/`` as the corpus substrate.

Three surface promises hold across PR 1 -> PR 3:

1. **No wiki/raw dirs, no problem.** When the corpus directories are absent or
   empty, ``list_pages()`` returns ``[]`` and ``read_page()`` returns ``None``.
   Every existing agent that has no CorpusBackend configured continues to work
   unchanged (byte-identical pre-#65 behavior, spec/34 D4).

2. **Path-traversal refused at the API boundary** (spec/34 implementer
   contract MUST #1). Operator- or agent-controlled ``name`` values are
   validated BEFORE any storage or dict access. ``_validate_corpus_name``
   permits ``[a-zA-Z0-9_.+@-]+`` and rejects path-traversal tokens, control
   characters, leading dots, and empty strings. ``_validate_corpus_type``
   enforces ``corpus in ("wiki", "raw")``.

3. **Construction is side-effect-free** (spec/34 implementer contract MUST #2,
   prep-pass finding H1). ``__init__`` stores ``self._agent_root = Path(agent_root)``
   and nothing else. No stat, no mkdir, no walk. The first method call that
   touches the filesystem does so lazily.

Storage layout (spec/34 §"FilesystemCorpusBackend storage layout")::

    <agent_root>/
      wiki/
        INDEX.md                  # routing index (render_index_summary target)
        *.md                      # wiki pages (INDEX.md excluded from list_pages)
        .versions/
          <page-stem>/
            <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md  # immutable snapshots
      raw/
        **                        # source documents (recursive walk)
        .versions/
          <page-stem>/
            <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md

``_version_filename`` copies ``memory/filesystem.py:712-716`` verbatim (prep-pass
checklist item 6). Snapshot filename format: ``<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md``
matching MemoryBackend for cross-Protocol uniformity (spec/34 D7).

Every page write and every version snapshot write goes through
``_io.atomic_write``. Never ``path.write_text()`` directly (prep-pass SEVERE S5 --
partial writes are visible to concurrent readers on POSIX between open() and flush()).

URL factory ``make_filesystem_corpus_backend_from_url`` accepts
``filesystem:///path`` scheme; refuses non-filesystem schemes, netloc, fragments,
ALL query params, and relative paths. Credentials are redacted from all
``ValueError`` sites via ``_redact_url`` (prep-pass HIGH H2).

Capabilities: ``supports_semantic_search=False, supports_full_text_search=False,
supports_versioning=True, supports_streaming_iteration=False,
embedding_provider=None``.

``query()`` uses case-insensitive substring + frontmatter-tag match, ordered by
match count (spec/34 §"query() capability precedence", both-False branch).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import frontmatter as _fm

from .types import CorpusCapabilities, CorpusPage, CorpusRef, CorpusStats
from ..memory.backend import VersionRef, WritePolicy
from .._io import atomic_write, safe_resolve_under
from ..exceptions import PathTraversalError, WritePathViolation

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Validation patterns (zero I/O at module level)

# name charset: alphanumeric + underscore + hyphen + dot + plus + at-sign.
# Mirrors PersonaBackend's _PERSONA_ID_PATTERN (spec/34 implementer contract
# MUST #1 -- cross-Protocol uniformity per prep-pass SEVERE S1).
# Rejects: path-traversal tokens (.., /, \\), leading dot, control chars,
# newlines, empty strings.
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.+@-]+$")

# Control-character detector (0x00-0x1F + DEL 0x7F).
# Used as a set in _validate_corpus_name for speed on short strings.
_CONTROL_CHARS = set(chr(i) for i in range(0x00, 0x20)) | {chr(0x7F)}


# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers


def _validate_corpus_name(name: str) -> None:
    """Validate a corpus page name at the API boundary.

    Permits: ``[a-zA-Z0-9_.+@-]+`` (letters, digits, underscore, dot, plus,
    at-sign, hyphen). Covers the full range of identifiers operators use
    (e.g., "avalanche-vs-snowball", "financial_freedom_book_ch7",
    "report.2026-04-22").

    Rejects (path-traversal + injection defenses):
    - Non-string or empty string.
    - Leading dot (hidden-file filesystem traversal trick).
    - ``..`` anywhere (directory traversal).
    - ``/`` or ``\\`` (path separators).
    - Control characters (0x00-0x1F, 0x7F) or newlines (log injection,
      path-token splitting).
    - Anything not matching the allowed charset.

    Raises ``CorpusInvalidName`` with a message naming the offending value
    and the rule violated. Called BEFORE any storage or dict access in every
    public method (spec/34 implementer contract MUST #1). Pattern copied
    verbatim from ``persona/filesystem.py:98-133`` (_validate_persona_id)
    per prep-pass SEVERE S1.
    """
    from ..exceptions import CorpusInvalidName

    if not isinstance(name, str) or not name:
        raise CorpusInvalidName(
            f"corpus page name must be a non-empty string; got {name!r}"
        )
    if name.startswith("."):
        raise CorpusInvalidName(
            f"corpus page name must not start with '.'; got {name!r}"
        )
    if ".." in name:
        raise CorpusInvalidName(f"corpus page name must not contain '..'; got {name!r}")
    if "/" in name or "\\" in name:
        raise CorpusInvalidName(
            f"corpus page name must not contain path separators; got {name!r}"
        )
    if any(c in _CONTROL_CHARS for c in name):
        raise CorpusInvalidName(
            f"corpus page name must not contain control characters or newlines; "
            f"got {name!r}"
        )
    if not _NAME_PATTERN.match(name):
        raise CorpusInvalidName(
            f"corpus page name must match [a-zA-Z0-9_.+@-]+; got {name!r}"
        )


def _validate_corpus_type(corpus: str) -> None:
    """Validate the corpus parameter is one of 'wiki' or 'raw'.

    Separate from _validate_corpus_name -- uses a simple ``not in`` check
    rather than the regex. ``Literal["wiki","raw"]`` provides type-safety at
    construction; this provides value-safety at runtime.

    Raises ``CorpusInvalidName`` (same exception family) when corpus is not
    one of the two allowed values.
    """
    from ..exceptions import CorpusInvalidName

    if corpus not in ("wiki", "raw"):
        raise CorpusInvalidName(f"corpus must be 'wiki' or 'raw'; got {corpus!r}")


# ──────────────────────────────────────────────────────────────────────────────
# URL redaction helper


def _redact_url(url: str, max_len: int = 64) -> str:
    """Strip credentials from a URL for safe inclusion in error messages.

    Replaces ``user:pass@`` in the authority section (scheme + authority,
    before the first path ``/``) with ``***@`` to avoid leaking secrets into
    logs or exception strings. Only redacts ``@`` in the authority portion;
    ``@`` in path components (e.g. ``/home/ops@fleet/corpus``) is preserved.
    Truncates the full result to ``max_len`` characters.

    Copied verbatim from ``persona/filesystem.py:177-197`` per prep-pass
    HIGH H2 (URL factory credential redaction is non-negotiable).
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
# Hashing and versioning helpers


def _sha256_hex(content: str) -> str:
    """Return the hex SHA-256 of content encoded as UTF-8."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _version_filename(content: str) -> str:
    """Return a version snapshot filename: <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md.

    Copied verbatim from ``memory/filesystem.py:712-716`` per prep-pass
    checklist item 6. Format matches MemoryBackend for cross-Protocol
    uniformity (spec/34 D7 + implementer contract MUST #8).
    """
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    short_hash = _sha256_hex(content)[:8]
    return f"{ts}_{short_hash}.md"


# ──────────────────────────────────────────────────────────────────────────────
# Frontmatter parsing helpers


# Named frontmatter fields that map to CorpusPage attributes.
# The order matches the CorpusPage dataclass definition in types.py.
# Keys NOT in this set land in extra_frontmatter (round-trip preserved).
_NAMED_FRONTMATTER_FIELDS = frozenset(
    {
        "name",
        "description",
        "type",
        "captured",
        "last_seen",
        "sources",
        "provenance",
        "confidence",
        "pinned",
        "related",
        "tags",
        "schema_version",
        "expires_at",
        "supersedes",
        "superseded_by",
        "source_url",
        "mime_type",
        "ingested_at",
    }
)


def _parse_frontmatter_to_page(
    path: Path,
    name: str,
    corpus: Literal["wiki", "raw"],
) -> CorpusPage:
    """Parse a markdown file into a CorpusPage.

    Uses the ``python-frontmatter`` package (same as MemoryBackend) to load
    YAML frontmatter. Named fields are mapped to CorpusPage attributes;
    unknown keys go into ``extra_frontmatter`` (round-trip preserved).

    Date fields (``captured``, ``last_seen``, ``expires_at``) stay as
    ``datetime.date`` objects -- PyYAML loads bare ISO dates ("2026-04-22")
    as ``datetime.date``. The ``ingested_at`` raw-side field stays
    ``datetime | None`` (automated ingest typically includes time).

    ``title`` is populated from frontmatter ``title`` key (if present),
    falling back to frontmatter ``name`` key, falling back to the first
    ``# Heading`` in the body, falling back to the stem.

    Raises:
        CorpusCorrupted: on parse errors (malformed YAML, non-UTF-8 bytes,
            non-dict frontmatter root).
        OSError: on file I/O failure (caller decides how to surface).
    """
    from ..exceptions import CorpusCorrupted

    try:
        stat = path.stat()
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusCorrupted(
            f"corpus page {name!r} in {corpus!r} contains non-UTF-8 bytes "
            f"at {path!r}: {exc}"
        ) from exc

    try:
        post = _fm.loads(raw)
    except Exception as exc:  # frontmatter can raise yaml.YAMLError etc.
        raise CorpusCorrupted(
            f"corpus page {name!r} in {corpus!r} has malformed frontmatter "
            f"at {path!r}: {exc}"
        ) from exc

    meta = post.metadata  # dict (possibly empty)
    body = post.content

    # Derive title: frontmatter "title" > frontmatter "name" > first H1 > stem
    title: str = ""
    if "title" in meta:
        title = str(meta["title"])
    elif "name" in meta:
        title = str(meta["name"])
    else:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
    if not title:
        title = name

    last_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    byte_size = stat.st_size

    ref = CorpusRef(
        name=name,
        corpus=corpus,
        title=title,
        last_modified=last_modified,
        byte_size=byte_size,
    )

    # Extract named fields from frontmatter; remainder goes to extra_frontmatter
    extra: dict = {}
    for key, value in meta.items():
        if key not in _NAMED_FRONTMATTER_FIELDS and key != "title":
            extra[key] = value

    return CorpusPage(
        ref=ref,
        body=body,
        name=meta.get("name"),
        description=meta.get("description"),
        type=meta.get("type"),
        captured=meta.get("captured"),  # date | None (PyYAML gives date)
        last_seen=meta.get("last_seen"),  # date | None
        sources=meta.get("sources"),
        provenance=meta.get("provenance"),
        confidence=meta.get("confidence"),
        pinned=bool(meta.get("pinned", False)),
        related=meta.get("related"),
        tags=meta.get("tags"),
        schema_version=meta.get("schema_version"),
        expires_at=meta.get("expires_at"),  # date | None
        supersedes=meta.get("supersedes"),
        superseded_by=meta.get("superseded_by"),
        source_url=meta.get("source_url"),
        mime_type=meta.get("mime_type"),
        ingested_at=meta.get("ingested_at"),  # datetime | None
        extra_frontmatter=extra,
    )


def _build_page_content(
    content: str,
    extra_frontmatter: dict | None,
) -> str:
    """Build the on-disk content string from body + optional frontmatter dict.

    When ``extra_frontmatter`` is provided and non-empty, prepend it as YAML
    frontmatter using python-frontmatter's dump format.
    When ``extra_frontmatter`` is None or empty, write content as-is.
    """
    if not extra_frontmatter:
        return content
    post = _fm.Post(content, **extra_frontmatter)
    result = _fm.dumps(post)
    if not result.endswith("\n"):
        result += "\n"
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot helper (internal)


def _take_snapshot(page_path: Path, corpus_dir: Path) -> VersionRef:
    """Create a version snapshot of an existing page file.

    Writes the current content to:
    ``<corpus_dir>/.versions/<page-stem>/<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md``

    Returns a ``VersionRef`` whose ``backend_id`` encodes
    ``"<corpus>/<stem>/<version_filename>"`` for opaque later resolution.

    Caller is responsible for verifying the page exists before calling.
    """
    content = page_path.read_text(encoding="utf-8")
    stem = page_path.stem
    versions_dir = corpus_dir / ".versions" / stem
    versions_dir.mkdir(parents=True, exist_ok=True)
    version_name = _version_filename(content)
    version_path = versions_dir / version_name
    atomic_write(version_path, content)
    # backend_id encodes: corpus_dir.name + "/" + stem + "/" + version_name
    backend_id = f"{corpus_dir.name}/{stem}/{version_name}"
    return VersionRef(backend_id=backend_id)


# ──────────────────────────────────────────────────────────────────────────────
# Backend


class FilesystemCorpusBackend:
    """Reference ``FilesystemCorpusBackend`` for wiki + raw corpus storage.

    Walks ``<agent_root>/wiki/`` and ``<agent_root>/raw/`` as the corpus
    substrate. Construction is side-effect-free per the spec/34 implementer
    contract (MUST #2) -- no stat, no walk, no env-var validation at
    ``__init__``.

    ``agent_root`` is NOT validated for existence at construction time.
    The first call that performs I/O discovers empty / missing directories
    and returns the appropriate no-op value (``[]``, ``None``, ``""``, etc.).

    Capabilities: ``supports_semantic_search=False``,
    ``supports_full_text_search=False``, ``supports_versioning=True``,
    ``supports_streaming_iteration=False``.

    ``query()`` does case-insensitive substring + frontmatter-tag match,
    ordered by match count (spec/34 both-False fallback, implementer
    contract MUST #4).

    All page writes and version snapshot writes go through
    ``_io.atomic_write``. Never ``path.write_text()`` directly (prep-pass
    SEVERE S5).

    ``list_pages(corpus="wiki")`` skips ``INDEX.md`` and any dot-prefixed
    entries (``".versions"``, ``".gitkeep"``, etc.) per prep-pass LOW finding
    (INDEX.md skip rule) and spec/34 §"FilesystemCorpusBackend storage layout".
    """

    backend_id: str = "filesystem"

    def __init__(self, agent_root: Path | str) -> None:
        """Construct without I/O.

        ``agent_root`` is recorded as a ``Path``; it is NOT validated for
        existence (spec/34 implementer contract MUST #2). First method call
        that requires the directory handles absence gracefully (no-op reads,
        auto-create-parent on write).

        Args:
            agent_root: the agent's root directory, under which ``wiki/``
                and ``raw/`` subdirectories live. Need not exist at
                construction time; absence is handled gracefully.
        """
        self._agent_root = Path(agent_root)

    # ── Corpus directory accessor ─────────────────────────────────────────

    def _corpus_dir(self, corpus: str) -> Path:
        """Return the corpus subdirectory path (does not stat)."""
        return self._agent_root / corpus

    # ── CorpusBackend Protocol surface ────────────────────────────────────

    @property
    def capabilities(self) -> CorpusCapabilities:
        """Return the backend capability snapshot.

        ``supports_semantic_search=False`` -- no embedding provider;
        ``query()`` uses substring + tag match fallback.
        ``supports_full_text_search=False`` -- no indexed FTS; query()
        uses linear scan.
        ``supports_versioning=True`` -- snapshot trio is implemented via
        the filesystem ``.versions/`` layout (spec/34 D7).
        ``supports_streaming_iteration=False`` -- list_pages() collects
        in-memory; paged iteration via limit/offset is the supported shape.
        ``embedding_provider=None`` -- MUST be None when
        supports_semantic_search=False (spec/34 CorpusCapabilities contract).

        This method is side-effect-free and does NOT touch the filesystem.
        """
        return CorpusCapabilities(
            supports_semantic_search=False,
            supports_full_text_search=False,
            supports_versioning=True,
            supports_streaming_iteration=False,
            embedding_provider=None,
            supports_canonical_export=True,  # spec/40 addendum
        )

    def export(self, query=None):
        """Export corpus pages as a CorpusExport canonical object (spec/40).

        Enumerates via list_pages() — MUST NOT route through query(text).
        Export is state extraction, not semantic retrieval (spec/40 MUST 6).

        Args:
            query: ``CorpusExportQuery | None``. Pass None for both corpora.

        Returns:
            ``CorpusExport`` with pages_with_bytes populated per corpus.
        """
        from ..export.filesystem import export_corpus
        from ..export.types import CorpusExportQuery

        if query is None:
            query = CorpusExportQuery()
        return export_corpus(self, query)

    def export_all(self):
        """Convenience wrapper — unbounded export. Equivalent to export(None)."""
        return self.export(None)

    # ── Read operations ───────────────────────────────────────────────────

    def list_pages(
        self,
        corpus: Literal["wiki", "raw"],
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CorpusRef]:
        """Return page references sorted by last_modified descending.

        ``offset`` supports paging. ``limit=None`` returns all pages.

        For ``corpus="wiki"``: skips ``INDEX.md`` and any dot-prefixed
        entries (``".versions/"``, ``".gitkeep"``) as these are not content
        pages (spec/34 §"FilesystemCorpusBackend storage layout" + prep-pass
        LOW finding INDEX.md skip rule).

        For ``corpus="raw"``: walks the directory recursively, skipping
        dot-prefixed names at every level.

        Returns ``[]`` when the corpus directory does not exist or is empty.
        """
        _validate_corpus_type(corpus)

        corpus_dir = self._corpus_dir(corpus)
        if not corpus_dir.is_dir():
            return []

        refs: list[CorpusRef] = []

        if corpus == "wiki":
            # Flat walk: *.md, skip INDEX.md + dot-prefixed entries
            for entry in corpus_dir.iterdir():
                if not entry.is_file():
                    continue
                if entry.name.startswith("."):
                    continue
                if entry.suffix.lower() != ".md":
                    continue
                if entry.name == "INDEX.md":
                    continue
                try:
                    stat = entry.stat()
                    raw = entry.read_text(encoding="utf-8")
                    post = _fm.loads(raw)
                    meta = post.metadata
                    body = post.content
                except Exception:
                    continue

                # Derive title
                title: str = ""
                if "title" in meta:
                    title = str(meta["title"])
                elif "name" in meta:
                    title = str(meta["name"])
                else:
                    for line in body.splitlines():
                        stripped = line.strip()
                        if stripped.startswith("# "):
                            title = stripped[2:].strip()
                            break
                if not title:
                    title = entry.stem

                refs.append(
                    CorpusRef(
                        name=entry.stem,
                        corpus=corpus,
                        title=title,
                        last_modified=datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ),
                        byte_size=stat.st_size,
                    )
                )
        else:
            # Recursive walk for raw; skip dot-prefixed entries at every level
            self._walk_raw(corpus_dir, corpus, refs)

        # Sort by last_modified descending (newest first)
        refs.sort(key=lambda r: r.last_modified, reverse=True)

        # Apply paging
        if offset:
            refs = refs[offset:]
        if limit is not None:
            refs = refs[:limit]

        return refs

    def _walk_raw(
        self,
        directory: Path,
        corpus: Literal["wiki", "raw"],
        refs: list[CorpusRef],
    ) -> None:
        """Walk a raw corpus directory (top-level only), appending CorpusRef entries.

        Nested raw subdirectories are not supported in v1.0; flatten the corpus.
        Subdirectories are skipped with a one-time debug log per subdirectory.
        Dot-prefixed entries are also skipped.
        """
        try:
            entries = list(directory.iterdir())
        except OSError:
            return

        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                _logger.debug(
                    "nested raw subdirectory %r skipped (not supported in v1.0; "
                    "flatten or file follow-up issue)",
                    entry.name,
                )
                continue
            if not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue

            # For .md files derive title; for others use the filename stem
            title = entry.stem
            if entry.suffix.lower() == ".md":
                try:
                    raw = entry.read_text(encoding="utf-8")
                    post = _fm.loads(raw)
                    meta = post.metadata
                    body = post.content
                    if "title" in meta:
                        title = str(meta["title"])
                    elif "name" in meta:
                        title = str(meta["name"])
                    else:
                        for line in body.splitlines():
                            stripped = line.strip()
                            if stripped.startswith("# "):
                                title = stripped[2:].strip()
                                break
                except Exception:
                    pass

            # Use stem as the name (flat-only: just the filename stem)
            name = entry.stem

            refs.append(
                CorpusRef(
                    name=name,
                    corpus=corpus,
                    title=title,
                    last_modified=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ),
                    byte_size=stat.st_size,
                )
            )

    def read_page(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> CorpusPage | None:
        """Return the full CorpusPage for ``(name, corpus)``, or None if absent.

        Returns ``None`` when the page does not exist (routine presence check).
        Distinct from ``read_version`` which raises ``CorpusVersionNotFound``
        on failure (infrastructure failure, not a presence check; see D12).

        Raises:
            CorpusInvalidName: ``name`` or ``corpus`` fails validation.
            CorpusCorrupted: the page file exists but has malformed content.
        """
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        corpus_dir = self._corpus_dir(corpus)
        page_path = corpus_dir / f"{name}.md"

        if not page_path.is_file():
            return None

        # Verify path does not escape corpus_dir (symlink guard)
        try:
            safe_resolve_under(page_path, corpus_dir)
        except Exception:
            return None

        return _parse_frontmatter_to_page(page_path, name, corpus)

    def render_index_summary(
        self,
        corpus: Literal["wiki", "raw"],
    ) -> str:
        """Return the corpus INDEX content (wiki/INDEX.md verbatim; raw: empty string).

        For ``corpus="wiki"``: reads ``<agent_root>/wiki/INDEX.md`` and
        returns its content verbatim. Returns ``""`` when the file does not
        exist (matches the legacy pre-#65 ``Path.read_text()`` caller's
        behavior on missing files).

        For ``corpus="raw"``: returns ``""`` (raw corpora typically have no
        INDEX equivalent; spec/34 §"render_index_summary").

        The empty-string contract lets callers branch on truthiness the same
        way they branch on the legacy direct-read pattern.
        """
        _validate_corpus_type(corpus)

        if corpus == "raw":
            return ""

        index_path = self._corpus_dir(corpus) / "INDEX.md"
        if not index_path.is_file():
            return ""

        try:
            return index_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Non-UTF-8 bytes in wiki/INDEX.md (Latin-1 import, BOM, mixed
            # encodings). Match bundle.py:_safe_read_text behavior exactly:
            # re-read with errors="replace" so operators get partial content
            # plus a visible warning comment, NOT a silent empty string.
            # Round 2 finding F3: returning "" silently lost wiki body
            # content where the legacy bundle path preserved it. The
            # CHANGELOG claim "matches the pre-#65 behavior" now holds.
            body = index_path.read_text(encoding="utf-8", errors="replace")
            return (
                f"<!-- WARNING: {index_path.name} contained non-UTF-8 bytes; "
                f"replaced. -->\n{body}"
            )
        except OSError:
            # File-system-level failure (permission denied, NFS handle stale,
            # ENOENT race between is_file check and read). No partial content
            # available to surface; soft-degrade to empty string.
            return ""

    # ── Write operations ──────────────────────────────────────────────────

    def write_page(
        self,
        name: str,
        content: str,
        corpus: Literal["wiki", "raw"],
        policy: WritePolicy,
        *,
        frontmatter: dict | None = None,
        expected_content_sha256: str | None = None,
    ) -> CorpusRef:
        """Write a corpus page, following the 4-case behavior table (spec/34 CQ1).

        Case 1 — fresh write:
            Page does not exist at ``(name, corpus)`` → write via
            ``_io.atomic_write``; create parent dirs as needed.

        Case 2 — content-identical idempotent no-op:
            Page exists; body + frontmatter SHA-256 unchanged → no-op.
            Safe under crash recovery and re-delivery.

        Case 3 — explicit overwrite via CAS (compare-and-swap):
            Page exists; content differs; ``expected_content_sha256`` matches
            current on-disk hash → snapshot existing version (supports_versioning
            is True for this backend); write new content via ``_io.atomic_write``.

        Case 4 — collision (safe default refusal):
            Page exists; content differs; ``expected_content_sha256`` is None →
            raise ``CorpusPageExists``.
            Page exists; content differs; hash supplied but mismatched →
            raise ``CorpusPreconditionFailed``.

        All writes go through ``_io.atomic_write``. Never ``path.write_text()``.

        Args:
            name: page name stem (validated against charset rule).
            content: body content (markdown). Combined with ``frontmatter``
                dict if provided.
            corpus: ``"wiki"`` or ``"raw"``.
            policy: write-path enforcement context (passed through to
                ``safe_resolve_under`` check).
            frontmatter: optional YAML frontmatter dict to prepend.
            expected_content_sha256: CAS hash for overwrite. Required to
                update an existing page (Case 3). If None and the page
                exists, raises ``CorpusPageExists`` (Case 4 collision).

        Returns:
            A ``CorpusRef`` for the written page.

        Raises:
            CorpusInvalidName: ``name`` or ``corpus`` fails validation.
            CorpusPageExists: Case 4 -- page exists, no CAS hash supplied.
            CorpusPreconditionFailed: Case 4 -- page exists, CAS hash mismatch.
        """
        from ..exceptions import CorpusPageExists, CorpusPreconditionFailed

        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        corpus_dir = self._corpus_dir(corpus)
        page_path = corpus_dir / f"{name}.md"

        # Build the on-disk content string (body + optional frontmatter)
        on_disk_content = _build_page_content(content, frontmatter)
        incoming_sha = _sha256_hex(on_disk_content)

        _cas_overwrite = (
            False  # True when Case 3 validated; snapshot fires after guards
        )

        if page_path.is_file():
            # Page exists -- determine which case applies
            existing_content = page_path.read_text(encoding="utf-8")
            existing_sha = _sha256_hex(existing_content)

            if existing_sha == incoming_sha:
                # Case 2: content-identical -- idempotent no-op
                stat = page_path.stat()
                return CorpusRef(
                    name=name,
                    corpus=corpus,
                    title=_extract_title_from_content(on_disk_content, name),
                    last_modified=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ),
                    byte_size=stat.st_size,
                )

            # Content differs -- need CAS to overwrite
            if expected_content_sha256 is None:
                # Case 4a: collision, no CAS hash supplied
                raise CorpusPageExists(
                    f"corpus page {name!r} in {corpus!r} already exists and "
                    f"its content differs from the proposed write. Supply "
                    f"expected_content_sha256 matching the current on-disk "
                    f"SHA-256 to opt into the overwrite (CAS) path."
                )

            if expected_content_sha256 != existing_sha:
                # Case 4b: CAS hash mismatch
                raise CorpusPreconditionFailed(
                    f"corpus page {name!r} in {corpus!r}: "
                    f"expected_content_sha256 {expected_content_sha256[:16]}... "
                    f"does not match current on-disk hash "
                    f"{existing_sha[:16]}... -- concurrent write detected; "
                    f"re-read and retry."
                )

            # Case 3 CAS validated -- defer snapshot until after guards pass
            _cas_overwrite = True

        # Case 1 (fresh write) or Case 3 (CAS overwrite): guard BEFORE snapshot
        # so a policy violation never creates a phantom audit entry in .versions/.
        # Verify write path is within agent_root (path-traversal guard)
        try:
            safe_resolve_under(page_path, self._agent_root)
        except Exception as exc:
            from ..exceptions import CorpusInvalidName

            raise CorpusInvalidName(
                f"page path for {name!r} in {corpus!r} resolves outside "
                f"agent_root: {exc}"
            ) from exc

        # Enforce WritePolicy: page_path must be under at least one of the
        # allowed write_paths declared in the policy.
        if policy.write_paths:
            _enforce_corpus_write_policy(page_path, policy)

        # Case 3: guards passed -- now safe to snapshot the existing page
        if _cas_overwrite:
            _take_snapshot(page_path, corpus_dir)

        atomic_write(page_path, on_disk_content)

        stat = page_path.stat()
        return CorpusRef(
            name=name,
            corpus=corpus,
            title=_extract_title_from_content(on_disk_content, name),
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            byte_size=stat.st_size,
        )

    # ── Versioning (capability-gated) ─────────────────────────────────────

    def list_versions(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> list[VersionRef]:
        """Return version snapshots for ``(name, corpus)`` in reverse order (newest first).

        Reads snapshot filenames from:
        ``<agent_root>/<corpus>/.versions/<stem>/``

        Returns ``[]`` when no versions exist (fresh page or versioning never
        triggered).

        Raises:
            CorpusInvalidName: ``name`` or ``corpus`` fails validation.
        """
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        corpus_dir = self._corpus_dir(corpus)
        versions_dir = corpus_dir / ".versions" / name

        if not versions_dir.is_dir():
            return []

        version_refs: list[VersionRef] = []
        for entry in versions_dir.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.endswith(".md"):
                continue
            if entry.name.startswith("."):
                continue
            # Validate the version filename to skip adversary-planted entries.
            # Version filenames take the form <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md
            # The stem (without .md) must match the _NAME_PATTERN charset.
            stem = entry.stem
            if not _NAME_PATTERN.match(stem):
                _logger.debug(
                    "list_versions: skipping adversarial filesystem entry %r in "
                    "%r (stem does not match allowed charset)",
                    entry.name,
                    str(versions_dir),
                )
                continue
            # backend_id: "<corpus>/<stem>/<version_filename>"
            backend_id = f"{corpus}/{name}/{entry.name}"
            version_refs.append(VersionRef(backend_id=backend_id))

        # Sort reverse-chronologically: version filenames start with ISO timestamp
        # so lexicographic sort descending gives newest first.
        version_refs.sort(key=lambda v: v.backend_id, reverse=True)
        return version_refs

    def read_version(
        self,
        version_ref: VersionRef,
    ) -> CorpusPage:
        """Return the CorpusPage for the given version snapshot.

        Raises ``CorpusVersionNotFound`` when:
        (a) the version reference does not resolve to an existing file, OR
        (b) the on-disk body file has been deleted externally.

        This covers BOTH "version does not exist" AND "version snapshot file
        is gone" -- the same independent failure mode named explicitly in D12
        so conformance test authors don't leave it unspecified.

        ``backend_id`` format (opaque; do not parse externally):
        ``"<corpus>/<stem>/<version_filename>"``

        Raises:
            CorpusVersionNotFound: version body is not accessible.
        """
        from ..exceptions import CorpusVersionNotFound

        backend_id = version_ref.backend_id
        # Decode the opaque backend_id: "<corpus>/<stem>/<version_filename>"
        parts = backend_id.split("/", 2)
        if len(parts) != 3:
            raise CorpusVersionNotFound(
                f"VersionRef {backend_id!r} is not a valid FilesystemCorpusBackend "
                f"version reference (expected '<corpus>/<stem>/<version_filename>')."
            )

        corpus_name, stem, version_filename = parts

        if corpus_name not in ("wiki", "raw"):
            raise CorpusVersionNotFound(
                f"VersionRef {backend_id!r} corpus segment {corpus_name!r} is "
                f"not one of 'wiki' or 'raw'."
            )

        corpus = corpus_name  # type: ignore[assignment]
        version_path = self._corpus_dir(corpus) / ".versions" / stem / version_filename

        # Guard against path traversal via crafted backend_id components.
        try:
            safe_resolve_under(version_path, self._agent_root)
        except (PathTraversalError, OSError) as exc:
            raise CorpusVersionNotFound(
                f"VersionRef {backend_id!r} resolves outside agent_root and is "
                f"not accessible: {exc}"
            ) from exc

        if not version_path.is_file():
            raise CorpusVersionNotFound(
                f"version snapshot file for {stem!r} in {corpus!r} "
                f"({version_filename}) does not exist or is not readable at "
                f"{version_path!r}. The file may have been externally deleted."
            )

        try:
            raw = version_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CorpusVersionNotFound(
                f"version snapshot file for {stem!r} in {corpus!r} "
                f"({version_filename}) could not be read: {exc}"
            ) from exc

        # Parse as a CorpusPage (re-use frontmatter parsing; version snapshots
        # are full markdown files identical in format to live pages)
        try:
            post = _fm.loads(raw)
        except Exception as exc:
            raise CorpusVersionNotFound(
                f"version snapshot file for {stem!r} in {corpus!r} "
                f"({version_filename}) has malformed frontmatter: {exc}"
            ) from exc

        meta = post.metadata
        body = post.content

        # Derive title the same way as read_page
        title: str = ""
        if "title" in meta:
            title = str(meta["title"])
        elif "name" in meta:
            title = str(meta["name"])
        else:
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
        if not title:
            title = stem

        # Use the version filename mtime for last_modified
        try:
            stat = version_path.stat()
            last_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            byte_size = stat.st_size
        except OSError:
            last_modified = datetime.now(tz=timezone.utc)
            byte_size = len(raw.encode("utf-8"))

        ref = CorpusRef(
            name=stem,
            corpus=corpus,
            title=title,
            last_modified=last_modified,
            byte_size=byte_size,
        )

        extra: dict = {}
        for key, value in meta.items():
            if key not in _NAMED_FRONTMATTER_FIELDS and key != "title":
                extra[key] = value

        return CorpusPage(
            ref=ref,
            body=body,
            name=meta.get("name"),
            description=meta.get("description"),
            type=meta.get("type"),
            captured=meta.get("captured"),
            last_seen=meta.get("last_seen"),
            sources=meta.get("sources"),
            provenance=meta.get("provenance"),
            confidence=meta.get("confidence"),
            pinned=bool(meta.get("pinned", False)),
            related=meta.get("related"),
            tags=meta.get("tags"),
            schema_version=meta.get("schema_version"),
            expires_at=meta.get("expires_at"),
            supersedes=meta.get("supersedes"),
            superseded_by=meta.get("superseded_by"),
            source_url=meta.get("source_url"),
            mime_type=meta.get("mime_type"),
            ingested_at=meta.get("ingested_at"),
            extra_frontmatter=extra,
        )

    def restore_version(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> CorpusRef:
        """Restore the page at ``version_ref`` as the live version.

        Reads the version snapshot body, then calls ``write_page()`` via the
        CAS (Case 3) path so the existing live version is snapshotted before
        the restore lands. The existing live page must exist for CAS to fire;
        if the page has been deleted since the snapshot was taken, this is a
        fresh write (Case 1).

        Raises:
            CorpusInvalidName: ``name`` or ``corpus`` fails validation.
            CorpusVersionNotFound: ``version_ref`` snapshot is not accessible.
        """
        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        # Read the version snapshot content (raises CorpusVersionNotFound on failure)
        version_page = self.read_version(version_ref)
        restore_content = version_page.body
        restore_fm = version_page.extra_frontmatter.copy()

        # Merge named fields back into frontmatter dict for preservation
        named = _page_to_frontmatter_dict(version_page)
        if named:
            restore_fm.update(named)

        # Get the current on-disk hash (for CAS) if the page exists
        corpus_dir = self._corpus_dir(corpus)
        page_path = corpus_dir / f"{name}.md"

        if page_path.is_file():
            existing_content = page_path.read_text(encoding="utf-8")
            expected_sha = _sha256_hex(existing_content)
        else:
            expected_sha = None

        # Re-build the content for the write (body + frontmatter)
        write_content = restore_content
        write_fm = restore_fm if restore_fm else None

        return self.write_page(
            name=name,
            content=write_content,
            corpus=corpus,
            policy=policy,
            frontmatter=write_fm,
            expected_content_sha256=expected_sha,
        )

    def snapshot(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        *,
        label: str | None = None,
    ) -> VersionRef:
        """Capture an explicit version snapshot of the current page content.

        Writes the current page body to:
        ``<agent_root>/<corpus>/.versions/<stem>/<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md``

        Returns a ``VersionRef`` whose ``backend_id`` encodes the snapshot
        path for later retrieval by ``read_version()`` and ``restore_version()``.

        Args:
            name: page name stem.
            corpus: ``"wiki"`` or ``"raw"``.
            label: optional human-readable label (stored in the version
                filename in this backend; ignored for filesystem impl --
                label is reserved for future SQLite metadata support).

        Raises:
            CorpusInvalidName: ``name`` or ``corpus`` fails validation.
            CorpusPageNotFound: the page does not exist.
        """
        from ..exceptions import CorpusPageNotFound

        _validate_corpus_name(name)
        _validate_corpus_type(corpus)

        corpus_dir = self._corpus_dir(corpus)
        page_path = corpus_dir / f"{name}.md"

        if not page_path.is_file():
            raise CorpusPageNotFound(
                f"corpus page {name!r} in {corpus!r} does not exist under "
                f"{corpus_dir!r}. Cannot snapshot a non-existent page."
            )

        return _take_snapshot(page_path, corpus_dir)

    # ── Search ────────────────────────────────────────────────────────────

    def query(
        self,
        text: str,
        corpus: Literal["wiki", "raw"],
        *,
        top_k: int = 10,
    ) -> list[CorpusRef]:
        """Search the corpus, ordered by match count.

        Implements the ``both-False`` fallback branch of the query() capability
        precedence rule (spec/34 MUST #4):
        - ``supports_semantic_search=False`` AND ``supports_full_text_search=False``
        - MUST fall back to case-insensitive substring + frontmatter-tag match,
          ordered by match count.

        Match scoring:
        - +1 for every case-insensitive substring occurrence in the body.
        - +1 for every case-insensitive substring occurrence in the page title.
        - +2 for every tag in ``tags`` frontmatter field that matches (case-
          insensitive equality or substring).
        - +1 for substring in description frontmatter.

        Pages with zero matches are excluded. Returns up to ``top_k`` results.

        Raises:
            CorpusInvalidName: ``corpus`` fails validation.
        """
        _validate_corpus_type(corpus)

        if not text:
            return []

        text_lower = text.lower()

        corpus_dir = self._corpus_dir(corpus)
        if not corpus_dir.is_dir():
            return []

        scored: list[tuple[int, CorpusRef]] = []

        # Collect all candidate pages (same filtering as list_pages)
        if corpus == "wiki":
            candidates = []
            for entry in corpus_dir.iterdir():
                if not entry.is_file():
                    continue
                if entry.name.startswith("."):
                    continue
                if entry.suffix.lower() != ".md":
                    continue
                if entry.name == "INDEX.md":
                    continue
                candidates.append(entry)
        else:
            candidates = []
            self._collect_raw_files(corpus_dir, candidates)

        for entry in candidates:
            try:
                stat = entry.stat()
                raw = entry.read_text(encoding="utf-8")
                post = _fm.loads(raw)
                meta = post.metadata
                body = post.content
            except Exception:
                continue

            score = 0

            # Body match (count occurrences)
            score += body.lower().count(text_lower)

            # Title match
            title: str = ""
            if "title" in meta:
                title = str(meta["title"])
            elif "name" in meta:
                title = str(meta["name"])
            else:
                for line in body.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        title = stripped[2:].strip()
                        break
            if not title:
                title = entry.stem

            if text_lower in title.lower():
                score += 1

            # Description match
            description = meta.get("description", "")
            if description and text_lower in str(description).lower():
                score += 1

            # Tag match: +2 per matching tag (case-insensitive equality or substring)
            tags = meta.get("tags", []) or []
            for tag in tags:
                if text_lower in str(tag).lower():
                    score += 2

            if score == 0:
                continue

            ref = CorpusRef(
                name=entry.stem,
                corpus=corpus,
                title=title,
                last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                byte_size=stat.st_size,
            )
            scored.append((score, ref))

        # Sort by score descending, then by name ascending for stable ordering
        scored.sort(key=lambda t: (-t[0], t[1].name))

        return [ref for _, ref in scored[:top_k]]

    def _collect_raw_files(self, directory: Path, results: list[Path]) -> None:
        """Collect top-level raw corpus files (flat-only), skipping dot-prefixed entries.

        Nested raw subdirectories are not supported in v1.0; flatten the corpus.
        Subdirectories are skipped with a debug log.
        """
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                _logger.debug(
                    "nested raw subdirectory %r skipped (not supported in v1.0; "
                    "flatten or file follow-up issue)",
                    entry.name,
                )
                continue
            if entry.is_file():
                results.append(entry)

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self, corpus: Literal["wiki", "raw"]) -> CorpusStats:
        """Return per-corpus health and stats.

        Scans the corpus directory (same logic as list_pages) to compute:
        - ``page_count``: number of valid pages.
        - ``total_bytes``: sum of on-disk file sizes.
        - ``last_update``: mtime of the most-recently-modified page.
        - ``most_recent``: up to 5 ``CorpusRef`` entries by last_modified desc.

        Returns empty stats (page_count=0) when the corpus dir does not exist.

        Raises:
            CorpusInvalidName: ``corpus`` fails validation.
        """
        _validate_corpus_type(corpus)

        refs = self.list_pages(corpus)

        if not refs:
            return CorpusStats(
                page_count=0,
                total_bytes=0,
                last_update=None,
                most_recent=[],
            )

        total_bytes = sum(r.byte_size for r in refs)
        last_update = refs[0].last_modified  # list is sorted newest-first
        most_recent = refs[:5]

        return CorpusStats(
            page_count=len(refs),
            total_bytes=total_bytes,
            last_update=last_update,
            most_recent=most_recent,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Idempotent no-op for FilesystemCorpusBackend.

        Filesystem backends hold no persistent connections, thread pools, or
        open file handles that need explicit teardown. ``close()`` is part of
        the CorpusBackend Protocol surface for backends that DO need cleanup
        (SQLite connection pools, Postgres connection pools, etc.).
        """
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Internal helper for write path


def _enforce_corpus_write_policy(target: Path, policy: WritePolicy) -> None:
    """Raise WritePathViolation if target is not under any policy.write_paths.

    Mirrors ``_enforce_write_path`` from ``memory/filesystem.py`` (prep-pass
    CRITICAL C1 -- WritePolicy must be enforced on every write call).

    Iterates over all ``policy.write_paths``; if the resolved target is
    ``relative_to`` at least one allowed path, returns silently. If NONE
    match, raises ``WritePathViolation`` (from ``atomic_agents.exceptions``).

    ``policy.read_only_paths`` are also checked -- if the target falls inside
    a read-only path, the write is blocked even if another write_path would
    have allowed it.
    """
    target_resolved = target.resolve()

    if policy.read_only_paths:
        for ro_path in policy.read_only_paths:
            try:
                target_resolved.relative_to(ro_path.resolve())
                raise WritePathViolation(
                    f"corpus write to {target} blocked — path is declared "
                    f"read-only: {ro_path}"
                )
            except ValueError:
                continue

    for allowed_path in policy.write_paths:
        try:
            target_resolved.relative_to(allowed_path.resolve())
            return
        except ValueError:
            continue
    raise WritePathViolation(
        f"corpus write to {target} blocked — not under any policy write_paths: "
        f"{policy.write_paths}"
    )


def _extract_title_from_content(content: str, fallback_name: str) -> str:
    """Extract a display title from markdown content (with or without frontmatter).

    Tries ``title`` and ``name`` keys from YAML frontmatter first, then scans
    for the first ``# Heading``, then falls back to ``fallback_name``.
    """
    try:
        post = _fm.loads(content)
        meta = post.metadata
        body = post.content
        if "title" in meta:
            return str(meta["title"])
        if "name" in meta:
            return str(meta["name"])
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    except Exception:
        pass
    return fallback_name


def _page_to_frontmatter_dict(page: CorpusPage) -> dict:
    """Extract named frontmatter fields from a CorpusPage back into a dict.

    Used by restore_version to rebuild the frontmatter for a re-write.
    Only includes non-None / non-default values to keep the output clean.
    """
    fm: dict = {}
    if page.name is not None:
        fm["name"] = page.name
    if page.description is not None:
        fm["description"] = page.description
    if page.type is not None:
        fm["type"] = page.type
    if page.captured is not None:
        fm["captured"] = page.captured
    if page.last_seen is not None:
        fm["last_seen"] = page.last_seen
    if page.sources is not None:
        fm["sources"] = page.sources
    if page.provenance is not None:
        fm["provenance"] = page.provenance
    if page.confidence is not None:
        fm["confidence"] = page.confidence
    if page.pinned:
        fm["pinned"] = True
    if page.related is not None:
        fm["related"] = page.related
    if page.tags is not None:
        fm["tags"] = page.tags
    if page.schema_version is not None:
        fm["schema_version"] = page.schema_version
    if page.expires_at is not None:
        fm["expires_at"] = page.expires_at
    if page.supersedes is not None:
        fm["supersedes"] = page.supersedes
    if page.superseded_by is not None:
        fm["superseded_by"] = page.superseded_by
    if page.source_url is not None:
        fm["source_url"] = page.source_url
    if page.mime_type is not None:
        fm["mime_type"] = page.mime_type
    if page.ingested_at is not None:
        fm["ingested_at"] = page.ingested_at
    return fm


# ──────────────────────────────────────────────────────────────────────────────
# URL factory


def make_filesystem_corpus_backend_from_url(url: str) -> FilesystemCorpusBackend:
    """Construct a ``FilesystemCorpusBackend`` from a ``filesystem://`` URL.

    Per spec/34 implementer contract, does NOT validate path existence at
    construction. Per spec/34 implementer contract, raises ``ValueError``
    with credentials redacted when the URL is malformed or uses the wrong
    scheme (prep-pass HIGH H2).

    URL format::

        filesystem:///absolute/path/to/agent_root

    The triple-slash convention is standard for local filesystem URLs:
    ``filesystem://`` (scheme + empty authority) + ``/abs/path`` (absolute
    path starting with ``/``).

    No query params, netloc, or fragment components are accepted.
    The filesystem corpus backend needs only the agent root path; all
    configuration is implicit in the directory layout.

    9-step validation pattern mirrors ``make_filesystem_persona_backend_from_url``
    from ``persona/filesystem.py:980-1057`` (prep-pass checklist item 5 / H2).

    Args:
        url: a ``filesystem:///path`` URL string.

    Returns:
        A ``FilesystemCorpusBackend`` targeting the given agent_root.

    Raises:
        ValueError: if ``url`` is not a string, is empty, is missing the
            ``://`` separator, uses a non-``filesystem`` scheme, has a
            netloc component, has a fragment, has duplicate query params,
            has unknown/any query params, or has a relative path component.
    """
    # Step 1: type + empty check
    if not isinstance(url, str) or not url:
        raise ValueError("URL must be a non-empty string")

    # Step 2: presence of scheme separator
    if "://" not in url:
        raise ValueError(f"URL missing scheme separator '://': {_redact_url(url)!r}")

    # Step 3: parse
    parsed = urlparse(url)

    # Step 4: scheme check
    if parsed.scheme.lower() != "filesystem":
        raise ValueError(
            f"Expected 'filesystem://' scheme; got scheme {parsed.scheme!r} "
            f"from URL {_redact_url(url)!r}"
        )

    # Step 5: netloc check
    if parsed.netloc:
        raise ValueError(
            f"filesystem:// URL must not have a netloc (host) component; "
            f"got netloc {parsed.netloc!r} from URL {_redact_url(url)!r}"
        )

    # Step 6: fragment check
    if parsed.fragment:
        raise ValueError(
            f"filesystem:// URL must not have a fragment component; "
            f"got fragment {parsed.fragment!r} from URL {_redact_url(url)!r}"
        )

    # Step 7: query params check (corpus filesystem backend accepts none)
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        # Detect duplicate query params (parse_qs keeps lists; len > 1 means dupe)
        for key, values in params.items():
            if len(values) > 1:
                raise ValueError(
                    f"filesystem:// URL has duplicate query param {key!r} "
                    f"from URL {_redact_url(url)!r}"
                )
        # No known query params for filesystem corpus backend -- reject all
        raise ValueError(
            f"filesystem:// URL does not accept query params; "
            f"got {parsed.query!r} from URL {_redact_url(url)!r}"
        )

    # Step 8: absolute path check
    path = parsed.path
    if not path.startswith("/"):
        raise ValueError(
            f"filesystem:// URL must use an absolute path (starting with '/'); "
            f"got {_redact_url(url)!r}"
        )

    # Step 9: construct (side-effect-free per spec/34 MUST #2)
    return FilesystemCorpusBackend(Path(path))

"""FilesystemBackend — the default MemoryBackend implementation.

Implements MemoryBackend against the standard atomic-agents vault layout:
  <agent_root>/
    memory/
      *.md                    # atomic notes
      INDEX.md                # routing index
      .versions/
        <note-stem>/
          <timestamp>_<hash>.md   # immutable snapshots
    dreams/
      .staging-<uuid>/
        memory/               # staging area for apply_staging

The logic here was lifted from the previous _capture.py and _versioning.py
modules. Those modules are now thin compatibility wrappers that delegate
here with deprecation warnings.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

import frontmatter

from .backend import (
    MemoryStats,
    Note,
    NoteRef,
    StagedMemory,
    VersionRef,
    WritePolicy,
)
from .._io import atomic_write, safe_resolve_under
from .._locks import AgentLock
from .._schema import CURRENT_SCHEMA_VERSION, derive_filename
from ..exceptions import (
    MemoryPreconditionFailed,
    PathTraversalError,
    SchemaValidationError,
    StagingNotApplied,
    VersionNotFound,
    WritePathViolation,
)

if TYPE_CHECKING:
    from ..types import Capture


# Files that must never be versioned or listed as notes.
_EXCLUDED_FILES = {"INDEX.md"}

# Type-to-section mapping for INDEX.md
_TYPE_TO_SECTION = {
    "user": "User Profile",
    "feedback": "Critical Feedback",
    "project": "Active Projects",
    "decision": "Locked Decisions",
    "reference": "Reference",
}


# ──────────────────────────────────────────────────────────────────
# FilesystemStagedMemory

@dataclass
class FilesystemStagedMemory(StagedMemory):
    """Filesystem-specific staging area for dream-style bulk operations."""
    staging_dir: Path    # <agent_root>/dreams/.staging-<uuid>/memory/
    _applied: bool = False
    _discarded: bool = False

    def _check_active(self) -> None:
        if self._applied or self._discarded:
            raise StagingNotApplied(
                f"StagedMemory {self.backend_id!r} has already been "
                + ("applied" if self._applied else "discarded")
                + " — cannot operate on it"
            )

    def write_note(self, capture: "Capture", policy: WritePolicy) -> NoteRef:
        """Write a note to the staging area (no policy enforcement on staging dir itself)."""
        self._check_active()
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        filename = derive_filename(capture.type, capture.name)
        target = self.staging_dir / filename
        today = date.today()
        content = _render_note(capture, today)
        atomic_write(target, content)
        _update_index(self.staging_dir / "INDEX.md", capture, filename)
        return _path_to_note_ref(target)

    def render_index_summary(self) -> str:
        """Return INDEX.md from the staging area, or generate from notes."""
        index_path = self.staging_dir / "INDEX.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return _generate_index_from_dir(self.staging_dir)

    def stats(self) -> MemoryStats:
        """Return stats for the staged memory area."""
        return _compute_stats(self.staging_dir, versions_dir=None)


# ──────────────────────────────────────────────────────────────────
# FilesystemBackend

class FilesystemBackend:
    """Default MemoryBackend — reads/writes to the standard vault layout.

    Instantiated by AtomicAgent.__init__ and by thin wrappers in
    _capture.py / _versioning.py.

    agent_root: path to the agent's root directory
    memory_subdir: subdirectory name under agent_root for notes (default "memory")
    """

    def __init__(self, agent_root: Path, memory_subdir: str = "memory"):
        self._agent_root = agent_root
        self._memory_dir = agent_root / memory_subdir
        self._versions_dir = self._memory_dir / ".versions"

    # ───── Internal helpers ─────────────────────────────────────────

    def _enforce(
        self, target: Path, policy: WritePolicy
    ) -> None:
        """Raise WritePathViolation if target violates policy."""
        _enforce_write_path(target, policy.write_paths, policy.read_only_paths)

    # ───── Read operations ──────────────────────────────────────────

    def list_notes(
        self,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]:
        if not self._memory_dir.exists():
            return []
        refs = []
        for path in sorted(self._memory_dir.glob("*.md")):
            if path.name in _EXCLUDED_FILES:
                continue
            ref = _path_to_note_ref(path)
            if ref is None:
                continue
            if not include_archived and ref.archived:
                continue
            if not include_superseded and ref.superseded_by:
                continue
            refs.append(ref)
        return refs

    def read_note(self, name: str) -> Note | None:
        """Return full Note for the named note, or None if not found."""
        safe_resolve_under(name, self._memory_dir)
        path = self._memory_dir / name
        if not path.exists():
            return None
        return _path_to_note(path)

    def list_pinned(self) -> list[NoteRef]:
        if not self._memory_dir.exists():
            return []
        refs = []
        for path in sorted(self._memory_dir.glob("*.md")):
            if path.name in _EXCLUDED_FILES:
                continue
            ref = _path_to_note_ref(path)
            if ref and ref.pinned:
                refs.append(ref)
        return refs

    def list_recent(
        self,
        n: int,
        exclude_pinned: bool = True,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> list[NoteRef]:
        all_refs = self.list_notes(
            include_archived=include_archived,
            include_superseded=include_superseded,
        )
        if exclude_pinned:
            all_refs = [r for r in all_refs if not r.pinned]
        # Sort by last_seen DESC (None sorts to the end)
        def _sort_key(r: NoteRef):
            return r.last_seen if r.last_seen is not None else date.min
        all_refs.sort(key=_sort_key, reverse=True)
        return all_refs[:n]

    def list_stale(
        self,
        threshold_days: int,
        exclude_pinned: bool = True,
    ) -> list[NoteRef]:
        cutoff = date.today() - timedelta(days=threshold_days)
        refs = self.list_notes(include_archived=False, include_superseded=False)
        stale = []
        for ref in refs:
            if exclude_pinned and ref.pinned:
                continue
            if ref.last_seen is None:
                continue
            if ref.last_seen < cutoff:
                stale.append(ref)
        return stale

    def list_orphans(self) -> list[NoteRef]:
        if not self._memory_dir.exists():
            return []
        index_path = self._memory_dir / "INDEX.md"
        if not index_path.exists():
            # All notes are technically orphans if there's no INDEX
            all_refs = []
            for path in sorted(self._memory_dir.glob("*.md")):
                if path.name in _EXCLUDED_FILES:
                    continue
                ref = _path_to_note_ref(path)
                if ref:
                    all_refs.append(ref)
            return all_refs
        try:
            index_text = index_path.read_text(encoding="utf-8")
        except OSError:
            index_text = ""
        orphans = []
        for path in sorted(self._memory_dir.glob("*.md")):
            if path.name in _EXCLUDED_FILES:
                continue
            stem = path.stem
            if path.name not in index_text and stem not in index_text:
                ref = _path_to_note_ref(path)
                if ref:
                    orphans.append(ref)
        return orphans

    def list_by_type(self, type_name: str) -> list[NoteRef]:
        return [r for r in self.list_notes(include_archived=True, include_superseded=True)
                if r.type == type_name]

    def render_index_summary(self) -> str:
        index_path = self._memory_dir / "INDEX.md"
        if index_path.exists():
            try:
                return index_path.read_text(encoding="utf-8")
            except OSError:
                pass
        return _generate_index_from_dir(self._memory_dir)

    # ───── Write operations ─────────────────────────────────────────

    def write_note(
        self,
        capture: "Capture",
        policy: WritePolicy,
        expected_content_sha256: str | None = None,
    ) -> NoteRef:
        """Write a capture to memory/. Enforces policy. See spec for merge semantics."""
        today = date.today()
        memory_dir = self._memory_dir

        # Enforce that the memory dir itself is under write_paths.
        self._enforce(memory_dir, policy)

        if capture.merge_into:
            # Case 1: merge into existing note
            target = memory_dir / capture.merge_into
            try:
                safe_resolve_under(capture.merge_into, memory_dir)
            except PathTraversalError as e:
                raise WritePathViolation(
                    f"merge_into path {capture.merge_into!r} resolves outside memory dir"
                ) from e
            self._enforce(target.resolve(), policy)
            if not target.exists():
                raise SchemaValidationError(
                    f"merge_into target {capture.merge_into} doesn't exist"
                )
            with _per_file_lock(target):
                if expected_content_sha256 is not None:
                    _check_precondition(target, expected_content_sha256)
                _snapshot(target)
                _merge_into_existing(target, capture, today)
            return _path_to_note_ref(target)

        # Derive filename for new/existing note
        filename = derive_filename(capture.type, capture.name)
        target = memory_dir / filename

        if target.exists():
            if _is_same_capture_content(target, capture):
                # Case 3: orphan-recovery
                with _per_file_lock(target):
                    if expected_content_sha256 is not None:
                        _check_precondition(target, expected_content_sha256)
                    _snapshot(target)
                    _update_index(memory_dir / "INDEX.md", capture, filename)
                return _path_to_note_ref(target)
            # Case 4: conflict
            raise SchemaValidationError(
                f"atomic note {filename} already exists; use merge_into to update"
            )

        # Case 2: fresh write
        if expected_content_sha256 is not None:
            raise MemoryPreconditionFailed(
                f"expected_content_sha256 was provided but {filename} doesn't exist",
                actual_sha256=None,
            )
        note_content = _render_note(capture, today)
        atomic_write(target, note_content)
        _update_index(memory_dir / "INDEX.md", capture, filename)
        return _path_to_note_ref(target)

    # ───── Versioning ────────────────────────────────────────────────

    def list_versions(self, name: str) -> list[VersionRef]:
        safe_resolve_under(name, self._memory_dir)
        stem = Path(name).stem
        vdir = self._versions_dir / stem
        if not vdir.exists():
            return []
        paths = sorted(vdir.glob("*.md"), reverse=True)
        return [VersionRef(backend_id=p.name) for p in paths]

    def read_version(self, version_ref: VersionRef) -> Note:
        vpath = self._resolve_version_ref(version_ref)
        return _path_to_note(vpath)

    def restore_version(
        self,
        name: str,
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> NoteRef:
        live_note = safe_resolve_under(name, self._memory_dir)
        vpath = self._resolve_version_ref(version_ref)
        # Enforce write policy on target
        self._enforce(live_note, policy)
        # Snapshot pre-restore state
        _snapshot(live_note)
        # Write version content atomically
        content = vpath.read_text(encoding="utf-8")
        atomic_write(live_note, content)
        return _path_to_note_ref(live_note)

    def redact_version(
        self,
        version_ref: VersionRef,
        replacement: str = "[REDACTED]",
    ) -> None:
        vpath = self._resolve_version_ref(version_ref)
        parsed = frontmatter.load(vpath)
        redacted_post = frontmatter.Post(replacement, **parsed.metadata)
        atomic_write(vpath, frontmatter.dumps(redacted_post) + "\n")

    def resolve_version_token(self, name: str, token: str) -> VersionRef:
        """Convert a CLI token (version filename) to an opaque VersionRef.

        Raises VersionNotFound if the token doesn't match any version.
        """
        safe_resolve_under(name, self._memory_dir)
        stem = Path(name).stem
        vdir = self._versions_dir / stem
        if not vdir.exists():
            raise VersionNotFound(
                f"No versions found for {name!r} — .versions/{stem}/ doesn't exist"
            )
        # Guard against path traversal in the token
        try:
            vpath = safe_resolve_under(token, vdir)
        except PathTraversalError:
            raise VersionNotFound(
                f"Version token {token!r} resolves outside versions directory"
            )
        if not vpath.exists():
            raise VersionNotFound(
                f"Version {token!r} not found for note {name!r}"
            )
        return VersionRef(backend_id=token)

    def _resolve_version_ref(self, version_ref: VersionRef) -> Path:
        """Resolve a VersionRef to an absolute path. Raises VersionNotFound if gone."""
        token = version_ref.backend_id
        # token is a version filename; find it under any note stem
        for stem_dir in self._versions_dir.iterdir() if self._versions_dir.exists() else []:
            if not stem_dir.is_dir():
                continue
            candidate = stem_dir / token
            if candidate.exists():
                return candidate
        raise VersionNotFound(f"Version {token!r} not found in {self._versions_dir}")

    # ───── Bulk staging ─────────────────────────────────────────────

    def create_staging(self) -> FilesystemStagedMemory:
        staging_id = f".staging-{uuid.uuid4().hex[:12]}"
        staging_dir = self._agent_root / "dreams" / staging_id / "memory"
        staging_dir.mkdir(parents=True, exist_ok=True)
        return FilesystemStagedMemory(
            backend_id=staging_id,
            staging_dir=staging_dir,
        )

    def apply_staging(self, staging: StagedMemory, policy: WritePolicy) -> None:
        if not isinstance(staging, FilesystemStagedMemory):
            raise TypeError("apply_staging expects a FilesystemStagedMemory instance")
        staging._check_active()

        staged_memory = staging.staging_dir
        if not staged_memory.exists():
            raise StagingNotApplied(
                f"Staged memory directory {staged_memory} doesn't exist"
            )

        current_memory = self._memory_dir
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        archived = self._agent_root / f"memory.archived-{ts}"

        # Acquire the per-agent lock to serialize against in-flight agent.call() writes.
        agent_lock = AgentLock(self._agent_root, wait_seconds=30)
        agent_lock.acquire()
        try:
            if current_memory.exists():
                os.rename(str(current_memory), str(archived))
            os.rename(str(staged_memory), str(current_memory))
            staging._applied = True
        finally:
            agent_lock.release()

    def discard_staging(self, staging: StagedMemory) -> None:
        if not isinstance(staging, FilesystemStagedMemory):
            raise TypeError("discard_staging expects a FilesystemStagedMemory instance")
        staging._check_active()
        parent = staging.staging_dir.parent
        if parent.exists():
            shutil.rmtree(str(parent))
        staging._discarded = True

    # ───── Stats ─────────────────────────────────────────────────────

    def stats(self) -> MemoryStats:
        return _compute_stats(self._memory_dir, self._versions_dir)

    def version_count(self, name: str) -> int:
        return len(self.list_versions(name))

    def last_mutation_at(self, name: str) -> datetime | None:
        """Return the timestamp of the most recent snapshot or write for a note."""
        safe_resolve_under(name, self._memory_dir)
        stem = Path(name).stem
        vdir = self._versions_dir / stem
        if vdir.exists():
            paths = sorted(vdir.glob("*.md"), reverse=True)
            if paths:
                # Parse timestamp from filename: YYYYMMDDTHHMMSSffffffZ_<hash>.md
                fname = paths[0].name
                try:
                    ts_str = fname[:15]  # YYYYMMDDTHHMMSSf
                    # Try microsecond precision first
                    try:
                        dt = datetime.strptime(fname[:22], "%Y%m%dT%H%M%S%fZ")
                    except ValueError:
                        dt = datetime.strptime(ts_str, "%Y%m%dT%H%M%S")
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
        # Fall back to file mtime
        path = self._memory_dir / name
        if path.exists():
            mtime = path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc)
        return None

    # ───── Capability advertisement ─────────────────────────────────

    @property
    def supports_semantic_search(self) -> bool:
        return False

    def search(self, query: str, limit: int = 10) -> list[NoteRef]:
        """Substring search fallback (no semantic support in filesystem backend)."""
        query_lower = query.lower()
        results = []
        for ref in self.list_notes(include_archived=True, include_superseded=True):
            if (query_lower in ref.name.lower()
                    or query_lower in ref.description.lower()):
                results.append(ref)
                if len(results) >= limit:
                    break
        return results

    # ───── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        pass  # Filesystem backend has no persistent resources to close.


# ──────────────────────────────────────────────────────────────────
# Internal helpers (package-private; shared with thin wrappers)

def _enforce_write_path(
    target: Path,
    allowed: list[Path],
    read_only_paths: list[Path] | None = None,
) -> None:
    """Raise WritePathViolation if target is outside allowed or inside read-only."""
    target_resolved = target.resolve()

    if read_only_paths:
        for ro_path in read_only_paths:
            try:
                target_resolved.relative_to(ro_path.resolve())
                raise WritePathViolation(
                    f"write to {target} blocked — path is declared read-only: {ro_path}"
                )
            except ValueError:
                continue

    for allowed_path in allowed:
        try:
            target_resolved.relative_to(allowed_path.resolve())
            return
        except ValueError:
            continue
    raise WritePathViolation(
        f"write to {target} blocked — not under any tools.md write path: {allowed}"
    )


@contextlib.contextmanager
def _per_file_lock(target: Path):
    """Acquire an exclusive POSIX flock on <target>.lock to close TOCTOU window."""
    if sys.platform == "win32" or not hasattr(os, "O_RDWR"):
        yield
        return

    import fcntl as _fcntl

    lock_path = target.parent / (target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        yield
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _check_precondition(target: Path, expected_sha256: str) -> None:
    """Raise MemoryPreconditionFailed if sha256 of file != expected."""
    current_content = target.read_text(encoding="utf-8")
    actual = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
    if actual != expected_sha256:
        raise MemoryPreconditionFailed(
            f"content of {target.name} has changed "
            f"(expected {expected_sha256[:16]}..., actual {actual[:16]}...); "
            f"re-read and retry",
            actual_sha256=actual,
        )


def _sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _version_filename(content: str) -> str:
    """Return a version filename: <ISO-ts>_<8-char-hash>.md."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    short_hash = _sha256_hex(content)[:8]
    return f"{ts}_{short_hash}.md"


def _snapshot(target: Path) -> Path | None:
    """Snapshot the current on-disk content of target into .versions/.

    Returns the version path written, or None if target doesn't exist.
    INDEX.md is excluded.
    """
    if target.name in _EXCLUDED_FILES:
        return None
    if not target.exists():
        return None

    content = target.read_text(encoding="utf-8")
    stem = target.stem
    versions_dir = target.parent / ".versions" / stem
    versions_dir.mkdir(parents=True, exist_ok=True)

    version_name = _version_filename(content)
    version_path = versions_dir / version_name
    atomic_write(version_path, content)
    return version_path


def _render_note(capture: "Capture", captured_date: date) -> str:
    """Build markdown content for a new atomic note."""
    fm: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "name": capture.name,
        "description": capture.description,
        "type": capture.type,
        "captured": captured_date.isoformat(),
        "last_seen": captured_date.isoformat(),
        "sources": capture.sources,
        "confidence": capture.confidence,
    }
    if capture.pinned:
        fm["pinned"] = True
    if capture.expires_at:
        fm["expires_at"] = capture.expires_at
    if capture.supersedes:
        fm["supersedes"] = capture.supersedes
    if capture.tags:
        fm["tags"] = capture.tags

    post = frontmatter.Post(capture.body, **fm)
    return frontmatter.dumps(post) + "\n"


def _merge_into_existing(target: Path, capture: "Capture", today: date) -> None:
    """Update last_seen and sources of an existing note (body preserved)."""
    parsed = frontmatter.load(target)
    parsed.metadata["last_seen"] = today.isoformat()
    existing_sources = list(parsed.metadata.get("sources", []))
    for src in capture.sources:
        if src not in existing_sources:
            existing_sources.append(src)
    parsed.metadata["sources"] = existing_sources
    atomic_write(target, frontmatter.dumps(parsed) + "\n")


def _is_same_capture_content(existing_path: Path, capture: "Capture") -> bool:
    """Return True if the existing note matches the incoming capture (orphan detection)."""
    try:
        parsed = frontmatter.load(existing_path)
    except Exception:
        return False
    return (
        parsed.metadata.get("type") == capture.type
        and parsed.metadata.get("name") == capture.name
        and parsed.metadata.get("description") == capture.description
        and parsed.content.strip() == capture.body.strip()
    )


def _update_index(index_path: Path, capture: "Capture", filename: str) -> None:
    """Add or update an entry in memory/INDEX.md under the right section."""
    section_header = "## " + _section_for_type(capture.type)
    new_line = f"- [{capture.name}]({filename}) — {capture.description}"

    if not index_path.exists():
        initial = "# Memory Index\n\n" + section_header + "\n"
        atomic_write(index_path, initial)

    text = index_path.read_text(encoding="utf-8")

    # Remove any existing line referencing this filename (idempotent)
    pattern = re.compile(
        rf"^- \[.*?\]\({re.escape(filename)}\).*?$",
        re.MULTILINE,
    )
    text = pattern.sub("", text)

    if section_header not in text:
        text = text.rstrip() + f"\n\n{section_header}\n{new_line}\n"
    else:
        lines = text.splitlines()
        out_lines = []
        inserted = False
        for line in lines:
            out_lines.append(line)
            if line.strip() == section_header and not inserted:
                out_lines.append(new_line)
                inserted = True
        text = "\n".join(out_lines) + "\n"

    # Clean up extra blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    atomic_write(index_path, text)


def _section_for_type(type_str: str) -> str:
    return _TYPE_TO_SECTION.get(type_str, "Reference")


def _path_to_note_ref(path: Path) -> NoteRef | None:
    """Parse a note file into a NoteRef. Returns None on parse failure."""
    try:
        parsed = frontmatter.load(path)
        meta = parsed.metadata

        # Parse dates carefully
        def _parse_date(val: Any) -> date | None:
            if val is None:
                return None
            if isinstance(val, date):
                return val
            s = str(val)
            try:
                if "T" in s:
                    return datetime.fromisoformat(s).date()
                return date.fromisoformat(s[:10])
            except (ValueError, TypeError):
                return None

        return NoteRef(
            name=path.name,
            type=meta.get("type", "reference"),
            description=meta.get("description", ""),
            captured=_parse_date(meta.get("captured")),
            last_seen=_parse_date(meta.get("last_seen")),
            pinned=bool(meta.get("pinned", False)),
            confidence=meta.get("confidence", "medium"),
            archived=bool(meta.get("archived", False)),
            superseded_by=meta.get("superseded_by") or None,
        )
    except Exception:
        return None


def _path_to_note(path: Path) -> Note:
    """Parse a note/version file into a full Note object."""
    try:
        parsed = frontmatter.load(path)
        meta = parsed.metadata
    except Exception:
        meta = {}
        parsed = type("_P", (), {"content": "", "metadata": {}})()

    def _parse_date(val: Any) -> date | None:
        if val is None:
            return None
        if isinstance(val, date):
            return val
        s = str(val)
        try:
            if "T" in s:
                return datetime.fromisoformat(s).date()
            return date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            return None

    # Collect known fields; remainder goes into extra_frontmatter
    known_fields = {
        "type", "name", "description", "confidence", "sources", "body",
        "supersedes", "merge_into", "pinned", "expires_at", "tags",
        "captured", "last_seen", "archived", "superseded_by",
        "schema_version",
    }
    extra = {k: v for k, v in meta.items() if k not in known_fields}

    sources = meta.get("sources")
    if not isinstance(sources, list):
        sources = [str(sources)] if sources else []

    tags = meta.get("tags")
    if not isinstance(tags, list):
        tags = [str(tags)] if tags else []

    return Note(
        type=meta.get("type", "reference"),
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        confidence=meta.get("confidence", "medium"),
        sources=sources,
        body=parsed.content,
        supersedes=meta.get("supersedes"),
        merge_into=meta.get("merge_into"),
        pinned=bool(meta.get("pinned", False)),
        expires_at=str(meta.get("expires_at")) if meta.get("expires_at") else None,
        tags=tags,
        captured=_parse_date(meta.get("captured")),
        last_seen=_parse_date(meta.get("last_seen")),
        archived=bool(meta.get("archived", False)),
        superseded_by=meta.get("superseded_by") or None,
        schema_version=int(meta.get("schema_version", 1)),
        extra_frontmatter=extra,
    )


def _generate_index_from_dir(memory_dir: Path) -> str:
    """Generate an INDEX.md-style text from notes in a directory."""
    sections: dict[str, list[str]] = defaultdict(list)
    if memory_dir.exists():
        for path in sorted(memory_dir.glob("*.md")):
            if path.name in _EXCLUDED_FILES:
                continue
            ref = _path_to_note_ref(path)
            if ref:
                sections[ref.type].append(f"- [{ref.name}]({ref.name}) — {ref.description}")

    lines = ["# Memory Index\n"]
    for type_key in ("user", "feedback", "project", "decision", "reference"):
        if type_key in sections:
            section_name = _section_for_type(type_key)
            lines.append(f"\n## {section_name}\n")
            lines.extend(sections[type_key])
    return "\n".join(lines) + "\n"


def _dir_size(path: Path, exclude_subdirs: bool = False) -> int:
    """Compute total bytes in a directory."""
    if not path.exists():
        return 0
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
            elif entry.is_dir(follow_symlinks=False) and not exclude_subdirs:
                total += _dir_size(Path(entry.path))
    except OSError:
        pass
    return total


def _compute_stats(memory_dir: Path, versions_dir: Path | None) -> MemoryStats:
    """Compute MemoryStats for a given memory directory."""
    by_type: dict[str, int] = defaultdict(int)
    total_notes = 0

    if memory_dir.exists():
        for path in memory_dir.glob("*.md"):
            if path.name in _EXCLUDED_FILES:
                continue
            total_notes += 1
            ref = _path_to_note_ref(path)
            if ref:
                by_type[ref.type] = by_type[ref.type] + 1
            else:
                by_type["unknown"] = by_type.get("unknown", 0) + 1

    live_bytes = _dir_size(memory_dir, exclude_subdirs=True)
    version_history_bytes = _dir_size(versions_dir) if versions_dir else 0

    # Version churn: count snapshots per note
    churn: list[tuple[str, int]] = []
    if versions_dir and versions_dir.exists():
        for stem_dir in versions_dir.iterdir():
            if not stem_dir.is_dir():
                continue
            count = sum(1 for _ in stem_dir.glob("*.md"))
            if count > 0:
                churn.append((f"{stem_dir.name}.md", count))
    churn.sort(key=lambda x: x[1], reverse=True)

    return MemoryStats(
        total_notes=total_notes,
        by_type=dict(by_type),
        live_bytes=live_bytes,
        version_history_bytes=version_history_bytes,
        most_churned=churn[:20],
    )

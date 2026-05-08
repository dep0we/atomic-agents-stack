"""Capture marker parsing + atomic write of new atomic notes per spec/05.

Two parser pathways supported (per Wave 5 spec):

- **Path 1: tool calls** (preferred) — agent emits a structured tool call via
  the SDK. SDK validates inputs against schema before they reach the helper.
  Implemented via CAPTURE_TOOL_SCHEMA + the provider-formatter helpers
  (anthropic_tool_definition / openai_tool_definition) and
  extract_tool_call_captures.
- **Path 2: fenced JSON blocks** (fallback) — agent emits a ```atomic_capture
  JSON``` fenced markdown block in its response text. Parsed via regex.

Both paths work; agents can use either or both. extract_all_captures parses
both and dedupes by (type, name, body hash). Tool-call captures take priority
when the same observation is emitted via both paths.
"""

from __future__ import annotations
import contextlib
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter

from ._io import atomic_write, atomic_append_jsonl
from ._schema import (
    derive_filename,
    validate_capture,
    CURRENT_SCHEMA_VERSION,
)
from ._versioning import snapshot_memory_version
from .exceptions import (
    CaptureParseError,
    MemoryPreconditionFailed,
    SchemaValidationError,
    WritePathViolation,
)
from .types import Capture

# Match ```atomic_capture or ````atomic_capture (3+ backticks) blocks
CAPTURE_BLOCK_PATTERN = re.compile(
    r"^(`{3,4})atomic_capture\s*\n(.*?)\n\1\s*$",
    re.MULTILINE | re.DOTALL,
)


# ──────────────────────────────────────────────────────────────────
# Path 1: tool-call schema

# JSON Schema for the atomic_capture tool. Used by both Anthropic
# (input_schema) and OpenAI (function.parameters) providers — the schema is
# identical; only the format wrapper differs per provider.
CAPTURE_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["user", "feedback", "project", "decision", "reference"],
            "description": "Atomic note type from the locked taxonomy.",
        },
        "name": {
            "type": "string",
            "maxLength": 80,
            "description": "Human-readable title for the memory note.",
        },
        "description": {
            "type": "string",
            "maxLength": 200,
            "description": "One-line hook explaining when this memory matters.",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "high = locked, medium = strong, low = tentative.",
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Source pointers (conversation IDs, doc paths). Must be non-empty.",
        },
        "body": {
            "type": "string",
            "description": "Full markdown body of the memory note.",
        },
        "supersedes": {
            "type": ["string", "null"],
            "description": "Filename of an older memory this one replaces (null if none).",
        },
        "merge_into": {
            "type": ["string", "null"],
            "description": "Filename of an existing note to merge into instead of creating new.",
        },
        "pinned": {
            "type": "boolean",
            "description": "If true, always loaded into context (use sparingly).",
        },
        "expires_at": {
            "type": ["string", "null"],
            "description": "YYYY-MM-DD when this memory becomes archive-candidate (null if none).",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Free-form tags for grouping (optional).",
        },
    },
    "required": ["type", "name", "description", "confidence", "sources", "body"],
    "additionalProperties": False,
}

CAPTURE_TOOL_DESCRIPTION = (
    "Capture a durable observation as an atomic memory note. Use when the user "
    "explicitly asks you to remember, when they correct your behavior, when they "
    "lock a decision, or when you learn something durable about them. Do NOT use "
    "for ephemeral conversation context, routine task outputs, or anything already "
    "in your persona/memory files (per spec/05 capture rules)."
)


def anthropic_tool_definition() -> dict:
    """Return the atomic_capture tool definition formatted for Anthropic Messages API."""
    return {
        "name": "atomic_capture",
        "description": CAPTURE_TOOL_DESCRIPTION,
        "input_schema": CAPTURE_TOOL_SCHEMA,
    }


def openai_tool_definition() -> dict:
    """Return the atomic_capture tool definition formatted for OpenAI function-calling."""
    return {
        "type": "function",
        "function": {
            "name": "atomic_capture",
            "description": CAPTURE_TOOL_DESCRIPTION,
            "parameters": CAPTURE_TOOL_SCHEMA,
        },
    }


def extract_tool_call_captures(
    tool_uses: list[dict],
) -> tuple[list[Capture], list[tuple[dict, str]]]:
    """Parse a list of tool_use blocks from an LLM response into Captures.

    `tool_uses` is the normalized list returned by `_llm.call_llm()` when the
    response contains tool_use blocks. Each entry is shaped:

        {"name": "atomic_capture", "input": {...typed args...}, "id": "..."}

    Only entries with name="atomic_capture" are processed; other tools are
    ignored (they belong to other tool definitions the agent may have).

    Returns (captures, parse_failures). parse_failures is a list of
    (raw_input_dict, error_message) tuples for entries that failed validation.
    """
    captures: list[Capture] = []
    failures: list[tuple[dict, str]] = []
    for tool_use in tool_uses:
        if tool_use.get("name") != "atomic_capture":
            continue
        raw_input = tool_use.get("input", {}) or {}
        try:
            validate_capture(raw_input)
            captures.append(_dict_to_capture(raw_input))
        except (SchemaValidationError, TypeError, ValueError, KeyError) as e:
            failures.append((raw_input, str(e)))
    return captures, failures


def extract_all_captures(
    response_text: str, tool_uses: list[dict] | None = None,
) -> tuple[list[Capture], list[tuple[Any, str]]]:
    """Extract captures from BOTH text (Path 2) and tool_use blocks (Path 1).

    Dedupes by (type, name, body hash). Tool-call captures take priority over
    text-fenced ones when content matches (Path 1 is more reliable per spec/05).

    Returns (captures, all_failures).
    """
    fenced_captures, fenced_failures = extract_captures(response_text)
    tool_captures: list[Capture] = []
    tool_failures: list[tuple[Any, str]] = []
    if tool_uses:
        tool_captures, t_fail = extract_tool_call_captures(tool_uses)
        tool_failures = list(t_fail)

    seen: dict[tuple, Capture] = {}
    for c in tool_captures:
        seen[(c.type, c.name, hash(c.body))] = c
    for c in fenced_captures:
        key = (c.type, c.name, hash(c.body))
        if key not in seen:
            seen[key] = c

    all_failures: list[tuple[Any, str]] = list(fenced_failures) + tool_failures
    return list(seen.values()), all_failures


def extract_captures(response_text: str) -> tuple[list[Capture], list[tuple[str, str]]]:
    """Find all atomic_capture fenced blocks in the response text.

    Returns (captures, parse_failures) where parse_failures is a list of
    (raw_content, error_message) tuples for blocks that didn't validate.
    """
    captures: list[Capture] = []
    failures: list[tuple[str, str]] = []

    for match in CAPTURE_BLOCK_PATTERN.finditer(response_text):
        raw = match.group(2)
        try:
            data = json.loads(raw)
            validate_capture(data)
            captures.append(_dict_to_capture(data))
        except (json.JSONDecodeError, SchemaValidationError, TypeError, ValueError) as e:
            failures.append((raw, str(e)))

    # Deduplicate within a single response (per spec/05 idempotency rule)
    seen = set()
    deduped = []
    for c in captures:
        key = (c.type, c.name, hash(c.body))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    return deduped, failures


def _dict_to_capture(d: dict[str, Any]) -> Capture:
    return Capture(
        type=d["type"],
        name=d["name"],
        description=d["description"],
        confidence=d["confidence"],
        sources=list(d["sources"]),
        body=d["body"],
        supersedes=d.get("supersedes"),
        merge_into=d.get("merge_into"),
        pinned=d.get("pinned", False),
        expires_at=d.get("expires_at"),
        tags=list(d.get("tags", [])),
    )


def write_atomic_note(
    agent_root: Path,
    capture: Capture,
    write_paths: list[Path],
    today: date | None = None,
    expected_content_sha256: str | None = None,
    read_only_paths: list[Path] | None = None,
    log_target: Path | None = None,
) -> Path:
    """Write a captured atomic note to memory/, then update memory/INDEX.md.

    Atomic write order per spec/04:
    1. Snapshot old content (versioning) — before any overwrite or merge
    2. Write the note file (temp + fsync + rename)
    3. Update INDEX.md (temp + fsync + rename)

    If step 3 fails, the result is an orphan note (lint catches it).
    Step 2 failing leaves nothing behind.

    Returns the path of the written note. Raises WritePathViolation if the
    target is outside any of `write_paths` or inside a read-only path.

    Args:
        agent_root: Path to the agent's root directory.
        capture: The Capture to write.
        write_paths: Directories the agent is allowed to write to.
        today: Override for today's date (used in tests).
        expected_content_sha256: Optional precondition. When provided:
            - For new notes (target doesn't exist): raises MemoryPreconditionFailed
              (caller expected an existing note).
            - For overwrite/merge: reads current content, hashes it, compares.
              Mismatch raises MemoryPreconditionFailed. Match proceeds normally.
        read_only_paths: Paths that must not be written to even if under write_paths.
        log_target: Optional JSONL log file path for versioning events.
    """
    today = today or date.today()
    memory_dir = agent_root / "memory"
    enforce_write_path(memory_dir, write_paths, read_only_paths=read_only_paths)

    if capture.merge_into:
        # Merge: update an existing note's last_seen + sources rather than create new
        target = memory_dir / capture.merge_into
        enforce_write_path(target.resolve(), write_paths, read_only_paths=read_only_paths)
        if not target.exists():
            raise SchemaValidationError(
                f"merge_into target {capture.merge_into} doesn't exist"
            )
        # Wrap precondition + snapshot + write in a per-file lock to close
        # the TOCTOU window between sha256 check and write (codex R2-D4).
        with _per_file_lock(target):
            # Optimistic concurrency check (for merge target)
            if expected_content_sha256 is not None:
                _check_precondition(target, expected_content_sha256)
            # Snapshot before merge
            version_path = snapshot_memory_version(target)
            if version_path is not None and log_target is not None:
                _log_version_created(log_target, target.name, version_path, agent_root)
            _merge_into_existing(target, capture, today)
        # INDEX entry already exists for the existing note; nothing to add
        return target

    # New note path
    filename = derive_filename(capture.type, capture.name)
    target = memory_dir / filename

    if target.exists():
        # Same-name file already exists. Check if it's an INDEX orphan (the note
        # was written but INDEX update failed on a previous run). If the content
        # matches the incoming capture, repair the INDEX and return — don't refuse.
        # If the content differs, this is a real name conflict; raise as before.
        if _is_same_capture_content(target, capture):
            # Orphan-recovery path: re-run INDEX update idempotently.
            # Snapshot before overwriting (orphan repair is still a mutation)
            with _per_file_lock(target):
                if expected_content_sha256 is not None:
                    _check_precondition(target, expected_content_sha256)
                version_path = snapshot_memory_version(target)
                if version_path is not None and log_target is not None:
                    _log_version_created(log_target, target.name, version_path, agent_root)
                _update_index(memory_dir / "INDEX.md", capture, filename)
            return target
        raise SchemaValidationError(
            f"atomic note {filename} already exists; use merge_into to update"
        )

    # Fresh write — target does not exist
    if expected_content_sha256 is not None:
        # Caller supplied a precondition but the note doesn't exist yet.
        raise MemoryPreconditionFailed(
            f"expected_content_sha256 was provided but {filename} doesn't exist",
            actual_sha256=None,
        )

    # Phase 1 — write the note (no snapshot: no prior version exists)
    note_content = _render_note(capture, today)
    atomic_write(target, note_content)

    # Phase 2 — update INDEX
    _update_index(memory_dir / "INDEX.md", capture, filename)

    return target


@contextlib.contextmanager
def _per_file_lock(target: Path):
    """Acquire an exclusive POSIX flock on ``<target>.lock``.

    This serializes the precondition-check + snapshot + write sequence so two
    concurrent writers cannot both pass the sha256 check and then race on the
    write — closing the TOCTOU window described in codex finding R2-D4.

    The lock file is ``<target>.lock`` (a sidecar next to the note).  It is
    created automatically and never removed (removal would introduce a new race
    between unlink and re-open).

    **Platform note:** ``fcntl`` is POSIX-only.  On Windows the lock is a
    no-op (best-effort precondition, not atomic).  This is documented behaviour
    per spec/04: full atomicity is guaranteed only on POSIX systems.
    """
    if sys.platform == "win32" or not hasattr(os, "O_RDWR"):
        # Windows / non-POSIX: yield without locking — best-effort only.
        yield
        return

    import fcntl as _fcntl

    lock_path = target.parent / (target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # Blocking exclusive lock — waits until any concurrent holder releases.
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        yield
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _check_precondition(target: Path, expected_sha256: str) -> None:
    """Raise MemoryPreconditionFailed if target content sha256 != expected_sha256.

    Must be called while holding the per-file lock (``_per_file_lock``) so that
    the check and the subsequent write are serialized against concurrent writers.
    """
    current_content = target.read_text(encoding="utf-8")
    actual = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
    if actual != expected_sha256:
        raise MemoryPreconditionFailed(
            f"content of {target.name} has changed (expected {expected_sha256[:16]}..., "
            f"actual {actual[:16]}...); re-read and retry",
            actual_sha256=actual,
        )


def _log_version_created(
    log_target: Path, note_name: str, version_path: Path, agent_root: Path
) -> None:
    """Append a memory_version_created event to the agent log."""
    try:
        rel = version_path.relative_to(agent_root)
    except ValueError:
        rel = version_path
    atomic_append_jsonl(log_target, json.dumps({
        "trigger": "memory_version_created",
        "note": note_name,
        "version_path": str(rel),
    }))


def _is_same_capture_content(existing_path: Path, capture: Capture) -> bool:
    """Return True if the existing note on disk matches the incoming capture.

    Compares the fields most likely to identify a duplicate vs a conflict:
    type, name, description, and body. Sources and metadata are intentionally
    excluded — the orphan-recovery path should tolerate minor metadata drift
    (e.g., a different run_id in sources) while still repairing the index.

    If the file cannot be read or parsed, returns False (safe default — refuse
    rather than silently repair something we can't verify).
    """
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


def _render_note(capture: Capture, captured_date: date) -> str:
    """Build the markdown content for a new atomic note."""
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


def _merge_into_existing(target: Path, capture: Capture, today: date) -> None:
    """Update an existing note's last_seen and sources without changing the body.

    Used when a capture has merge_into set — the agent saw the same observation
    again and wants to refresh recency + add a new source reference, not create
    a duplicate note.
    """
    parsed = frontmatter.load(target)
    parsed.metadata["last_seen"] = today.isoformat()
    existing_sources = list(parsed.metadata.get("sources", []))
    for src in capture.sources:
        if src not in existing_sources:
            existing_sources.append(src)
    parsed.metadata["sources"] = existing_sources
    atomic_write(target, frontmatter.dumps(parsed) + "\n")


def _update_index(index_path: Path, capture: Capture, filename: str) -> None:
    """Add an entry to memory/INDEX.md under the right type section.

    Idempotent — if an entry for this filename already exists, replace its line.
    """
    if not index_path.exists():
        # Create a minimal INDEX
        initial = "# Memory Index\n\n## " + _section_for_type(capture.type) + "\n"
        atomic_write(index_path, initial)

    text = index_path.read_text(encoding="utf-8")
    section_header = "## " + _section_for_type(capture.type)
    new_line = f"- [{capture.name}]({filename}) — {capture.description}"

    # Remove any existing line referencing this filename (idempotent)
    pattern = re.compile(
        rf"^- \[.*?\]\({re.escape(filename)}\).*?$",
        re.MULTILINE,
    )
    text = pattern.sub("", text)

    # Find the section; create if missing
    if section_header not in text:
        text = text.rstrip() + f"\n\n{section_header}\n{new_line}\n"
    else:
        # Insert under the section header
        lines = text.splitlines()
        out_lines = []
        inserted = False
        for i, line in enumerate(lines):
            out_lines.append(line)
            if line.strip() == section_header and not inserted:
                out_lines.append(new_line)
                inserted = True
        text = "\n".join(out_lines) + "\n"

    # Clean up empty lines from removed entries
    text = re.sub(r"\n{3,}", "\n\n", text)
    atomic_write(index_path, text)


def _section_for_type(type_str: str) -> str:
    """Map atomic-note type to its INDEX.md section header."""
    return {
        "user": "User Profile",
        "feedback": "Critical Feedback",
        "project": "Active Projects",
        "decision": "Locked Decisions",
        "reference": "Reference",
    }[type_str]


def enforce_write_path(
    target: Path,
    allowed: list[Path],
    read_only_paths: list[Path] | None = None,
) -> None:
    """Raise WritePathViolation if `target` is outside allowed write paths or
    inside a read-only path.

    Per spec/01-anatomy + Codex finding #6: this is the helper-side enforcement
    layer. tools.md is policy; this is enforcement.

    read_only paths win even when a target is also under an allowed write path —
    the explicit read-only declaration is the stronger constraint.
    """
    target_resolved = target.resolve()

    # Check read-only paths first — they override write_paths.
    if read_only_paths:
        for ro_path in read_only_paths:
            try:
                target_resolved.relative_to(ro_path.resolve())
                raise WritePathViolation(
                    f"write to {target} blocked — path is declared read-only: {ro_path}"
                )
            except ValueError:
                continue  # not under this read-only path

    for allowed_path in allowed:
        try:
            target_resolved.relative_to(allowed_path.resolve())
            return  # target is under an allowed path
        except ValueError:
            continue
    raise WritePathViolation(
        f"write to {target} blocked — not under any tools.md write path: {allowed}"
    )

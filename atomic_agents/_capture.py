"""Capture marker parsing + atomic write of new atomic notes per spec/05.

Two parser pathways supported (per Wave 5 spec):
- Path 1: tool calls (handled by the LLM SDK; not parsed from text)
- Path 2: fenced ```atomic_capture``` JSON blocks in response text

This module implements Path 2. Path 1 is handled in _llm.py when tool calls
are returned by the SDK.
"""

from __future__ import annotations
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter

from ._io import atomic_write
from ._schema import (
    derive_filename,
    validate_capture,
    CURRENT_SCHEMA_VERSION,
)
from .exceptions import CaptureParseError, SchemaValidationError, WritePathViolation
from .types import Capture

# Match ```atomic_capture or ````atomic_capture (3+ backticks) blocks
CAPTURE_BLOCK_PATTERN = re.compile(
    r"^(`{3,4})atomic_capture\s*\n(.*?)\n\1\s*$",
    re.MULTILINE | re.DOTALL,
)


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
) -> Path:
    """Write a captured atomic note to memory/, then update memory/INDEX.md.

    Atomic write order per spec/04:
    1. Write the note file (temp + fsync + rename)
    2. Update INDEX.md (temp + fsync + rename)

    If step 2 fails, the result is an orphan note (lint catches it).
    Step 1 failing leaves nothing behind.

    Returns the path of the written note. Raises WritePathViolation if the
    target is outside any of `write_paths`.
    """
    today = today or date.today()
    memory_dir = agent_root / "memory"
    enforce_write_path(memory_dir, write_paths)

    if capture.merge_into:
        # Merge: update an existing note's last_seen + sources rather than create new
        target = memory_dir / capture.merge_into
        if not target.exists():
            raise SchemaValidationError(
                f"merge_into target {capture.merge_into} doesn't exist"
            )
        _merge_into_existing(target, capture, today)
        # INDEX entry already exists for the existing note; nothing to add
        return target

    # New note path
    filename = derive_filename(capture.type, capture.name)
    target = memory_dir / filename

    if target.exists():
        # Same-name file already exists; treat as duplicate, refuse rather than overwrite
        raise SchemaValidationError(
            f"atomic note {filename} already exists; use merge_into to update"
        )

    # Phase 1 — write the note
    note_content = _render_note(capture, today)
    atomic_write(target, note_content)

    # Phase 2 — update INDEX
    _update_index(memory_dir / "INDEX.md", capture, filename)

    return target


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


def enforce_write_path(target: Path, allowed: list[Path]) -> None:
    """Raise WritePathViolation if `target` is outside any allowed write path.

    Per spec/01-anatomy + Codex finding #6: this is the helper-side enforcement
    layer. tools.md is policy; this is enforcement.
    """
    target_resolved = target.resolve()
    for allowed_path in allowed:
        try:
            target_resolved.relative_to(allowed_path.resolve())
            return  # target is under an allowed path
        except ValueError:
            continue
    raise WritePathViolation(
        f"write to {target} blocked — not under any tools.md write path: {allowed}"
    )

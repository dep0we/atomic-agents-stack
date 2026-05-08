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
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import frontmatter

from ._io import atomic_append_jsonl
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

# Re-export internal helpers from filesystem.py so existing importers
# (dream.py, tests) continue to find them here.
from .memory.filesystem import (
    _render_note,
    _update_index,
    _section_for_type,
    _per_file_lock,
    _check_precondition,
    _is_same_capture_content,
    _merge_into_existing,
)

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

    .. deprecated::
        Use ``FilesystemBackend.write_note()`` (via ``agent.memory.write_note()``)
        instead. This function will emit a DeprecationWarning in v1.0.

    Delegates to FilesystemBackend.write_note(). The ``today`` parameter is
    kept for signature compatibility but unused (the backend always uses
    date.today()). ``log_target``, if provided, receives a JSONL event when a
    version snapshot is created (for backward compat with tests/callers).

    Returns the path of the written note.
    """
    # warnings.warn(
    #     "write_atomic_note() is deprecated; use agent.memory.write_note() instead.",
    #     DeprecationWarning, stacklevel=2,
    # )
    from .memory.filesystem import FilesystemBackend, _snapshot
    from .memory.backend import WritePolicy

    memory_dir = agent_root / "memory"
    backend = FilesystemBackend(agent_root, "memory")
    policy = WritePolicy(
        write_paths=write_paths,
        read_only_paths=read_only_paths or [],
    )

    # For log_target compat: detect whether the operation will snapshot,
    # and if so capture the version path to log it after the write.
    version_path: Path | None = None
    if log_target is not None and capture.merge_into:
        # Merge path will snapshot the target — pre-emptively capture it
        target_for_snap = memory_dir / capture.merge_into
        if target_for_snap.exists():
            # We'll snapshot AFTER write_note to get the actual path
            pass

    ref = backend.write_note(capture, policy, expected_content_sha256)
    note_path = agent_root / "memory" / ref.name

    # If log_target was provided, check whether a snapshot was created
    # during this write and log it.
    if log_target is not None:
        note_name = ref.name
        stem = note_path.stem
        versions_dir = memory_dir / ".versions" / stem
        if versions_dir.exists():
            versions = sorted(versions_dir.glob("*.md"), reverse=True)
            if versions:
                version_path = versions[0]
                _log_version_created(log_target, note_name, version_path, agent_root)

    return note_path


# The private helper functions (_render_note, _update_index, _section_for_type,
# _per_file_lock, _check_precondition, _is_same_capture_content, _merge_into_existing)
# have moved to atomic_agents.memory.filesystem and are re-exported above.
# They remain importable from this module for backward compatibility.


def _log_version_created(
    log_target: Path, note_name: str, version_path: Path, agent_root: Path
) -> None:
    """Append a memory_version_created event to the agent log.

    Kept here for any code that still calls it directly. The new path
    goes through FilesystemBackend which handles snapshotting internally.
    """
    try:
        rel = version_path.relative_to(agent_root)
    except ValueError:
        rel = version_path
    atomic_append_jsonl(log_target, json.dumps({
        "trigger": "memory_version_created",
        "note": note_name,
        "version_path": str(rel),
    }))


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

"""Frontmatter validation per spec/03-file-formats.

Validates atomic note + wiki page + capture frontmatter. Raises
SchemaValidationError on failure with the specific field that broke.
"""

from __future__ import annotations
import re
from datetime import date
from pathlib import Path
from typing import Any

from .exceptions import SchemaValidationError

CURRENT_SCHEMA_VERSION = 1

VALID_TYPES = {"user", "feedback", "project", "decision", "reference"}
VALID_WIKI_TYPES = {"wiki_page"}
VALID_CONFIDENCE = {"high", "medium", "low"}

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_MAX = 80
DESCRIPTION_MAX = 200

# Filename pattern per spec/03 — allows optional date/quarter suffix for
# time-bounded content (per Wave 6 update)
NOTE_FILENAME_PATTERN = re.compile(
    r"^(user|feedback|project|decision|reference)"
    r"(_\d{4}(-q[1-4])?)?"        # optional _YYYY or _YYYY-q1 suffix
    r"_[a-z0-9_]+\.md$"
)


def validate_wiki_frontmatter(meta: dict[str, Any], filename: str | None = None) -> None:
    """Validate a wiki page's frontmatter. Raises SchemaValidationError on first failure.

    Wiki pages live under <agent>/wiki/ and use type: wiki_page.
    `meta` is the dict from a parsed frontmatter file.
    `filename` is the bare filename for basic sanity checks.
    """
    _require(meta, "schema_version", int)
    if meta["schema_version"] != CURRENT_SCHEMA_VERSION:
        raise SchemaValidationError(
            f"schema_version is {meta['schema_version']}; current is {CURRENT_SCHEMA_VERSION}. "
            f"Run migrations per spec/03."
        )

    _require(meta, "type", str)
    if meta["type"] not in VALID_WIKI_TYPES:
        raise SchemaValidationError(
            f"wiki page type must be one of {VALID_WIKI_TYPES}; got '{meta['type']}'"
        )

    _require(meta, "name", str, max_length=NAME_MAX)
    _require(meta, "description", str, max_length=DESCRIPTION_MAX)

    # Optional fields — if present, validate
    if "tags" in meta and not isinstance(meta["tags"], list):
        raise SchemaValidationError("tags must be a list of strings")
    if "pinned" in meta and not isinstance(meta["pinned"], bool):
        raise SchemaValidationError("pinned must be a boolean")


def validate_atomic_note_frontmatter(meta: dict[str, Any], filename: str | None = None) -> None:
    """Validate one atomic note's frontmatter. Raises SchemaValidationError on first failure.

    `meta` is the dict from a parsed frontmatter file.
    `filename` is the bare filename (e.g., "feedback_debt_priority.md") for filename pattern check.
    """
    _require(meta, "schema_version", int)
    if meta["schema_version"] != CURRENT_SCHEMA_VERSION:
        raise SchemaValidationError(
            f"schema_version is {meta['schema_version']}; current is {CURRENT_SCHEMA_VERSION}. "
            f"Run migrations per spec/03."
        )

    _require(meta, "name", str, max_length=NAME_MAX)
    _require(meta, "description", str, max_length=DESCRIPTION_MAX)
    _require(meta, "type", str)
    if meta["type"] not in VALID_TYPES:
        raise SchemaValidationError(
            f"type must be one of {VALID_TYPES}; got '{meta['type']}'"
        )

    _require_date(meta, "captured")
    _require_date(meta, "last_seen")

    _require(meta, "sources", list)
    if not meta["sources"]:
        raise SchemaValidationError("sources must be a non-empty list")

    _require(meta, "confidence", str)
    if meta["confidence"] not in VALID_CONFIDENCE:
        raise SchemaValidationError(
            f"confidence must be one of {VALID_CONFIDENCE}; got '{meta['confidence']}'"
        )

    # Optional fields — if present, validate
    if "pinned" in meta and not isinstance(meta["pinned"], bool):
        raise SchemaValidationError("pinned must be a boolean")
    if "expires_at" in meta and meta["expires_at"] is not None:
        if not isinstance(meta["expires_at"], (str, date)):
            raise SchemaValidationError("expires_at must be a YYYY-MM-DD string or null")
        if isinstance(meta["expires_at"], str) and not DATE_PATTERN.match(meta["expires_at"]):
            raise SchemaValidationError(f"expires_at must be YYYY-MM-DD; got '{meta['expires_at']}'")
    if "supersedes" in meta and meta["supersedes"] is not None:
        if not isinstance(meta["supersedes"], str):
            raise SchemaValidationError("supersedes must be a string filename or null")
    if "tags" in meta and not isinstance(meta["tags"], list):
        raise SchemaValidationError("tags must be a list of strings")

    # Filename pattern check (if filename provided)
    if filename is not None and not NOTE_FILENAME_PATTERN.match(filename):
        raise SchemaValidationError(
            f"filename '{filename}' doesn't match pattern "
            f"{{type}}_[YYYY[-q#]_]{{topic}}.md per spec/03"
        )


def validate_capture(capture_dict: dict[str, Any]) -> None:
    """Validate a capture marker dict (subset of full atomic-note schema).

    A capture is what the agent emits inline; the helper turns it into a
    full atomic note by adding captured/last_seen/schema_version automatically.
    """
    for field_name in ("type", "name", "description", "confidence", "sources", "body"):
        if field_name not in capture_dict:
            raise SchemaValidationError(f"capture missing required field '{field_name}'")

    if capture_dict["type"] not in VALID_TYPES:
        raise SchemaValidationError(
            f"capture type must be one of {VALID_TYPES}; got '{capture_dict['type']}'"
        )
    if capture_dict["confidence"] not in VALID_CONFIDENCE:
        raise SchemaValidationError(
            f"capture confidence must be one of {VALID_CONFIDENCE}"
        )
    if not isinstance(capture_dict["sources"], list) or not capture_dict["sources"]:
        raise SchemaValidationError("capture sources must be a non-empty list")
    if len(capture_dict["name"]) > NAME_MAX:
        raise SchemaValidationError(f"capture name exceeds {NAME_MAX} chars")
    if len(capture_dict["description"]) > DESCRIPTION_MAX:
        raise SchemaValidationError(f"capture description exceeds {DESCRIPTION_MAX} chars")


def derive_filename(capture_type: str, name: str) -> str:
    """Derive the atomic note filename from type + name per spec/03 conventions.

    Lowercases, snake_cases the name. Doesn't add date suffix — caller does
    that for time-bounded types if needed.
    """
    topic = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"{capture_type}_{topic}.md"


def _require(meta: dict, key: str, expected_type: type, max_length: int | None = None) -> None:
    if key not in meta:
        raise SchemaValidationError(f"missing required field '{key}'")
    if not isinstance(meta[key], expected_type):
        raise SchemaValidationError(
            f"field '{key}' must be {expected_type.__name__}; got {type(meta[key]).__name__}"
        )
    if max_length is not None and isinstance(meta[key], str) and len(meta[key]) > max_length:
        raise SchemaValidationError(
            f"field '{key}' exceeds max length {max_length}"
        )


def _require_date(meta: dict, key: str) -> None:
    if key not in meta:
        raise SchemaValidationError(f"missing required field '{key}'")
    val = meta[key]
    if isinstance(val, date):
        return
    if isinstance(val, str) and DATE_PATTERN.match(val):
        return
    raise SchemaValidationError(f"field '{key}' must be a YYYY-MM-DD date; got {val!r}")

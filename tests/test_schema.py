"""Tests for atomic_agents._schema."""

import pytest

from atomic_agents._schema import (
    derive_filename,
    validate_atomic_note_frontmatter,
    validate_capture,
    NOTE_FILENAME_PATTERN,
)
from atomic_agents.exceptions import SchemaValidationError


def _valid_note_meta():
    return {
        "schema_version": 1,
        "name": "Bottom-line-first communication",
        "description": "Dan wants the recommendation in 1-3 sentences before any working",
        "type": "feedback",
        "captured": "2026-04-12",
        "last_seen": "2026-05-04",
        "sources": ["conversation_2026-04-12"],
        "confidence": "high",
    }


def test_valid_note_passes(tmp_path):
    validate_atomic_note_frontmatter(_valid_note_meta(), filename="feedback_communication_style.md")


def test_missing_required_field_fails():
    meta = _valid_note_meta()
    del meta["name"]
    with pytest.raises(SchemaValidationError, match="name"):
        validate_atomic_note_frontmatter(meta)


def test_invalid_type_fails():
    meta = _valid_note_meta()
    meta["type"] = "bogus"
    with pytest.raises(SchemaValidationError, match="type"):
        validate_atomic_note_frontmatter(meta)


def test_invalid_confidence_fails():
    meta = _valid_note_meta()
    meta["confidence"] = "very_high"
    with pytest.raises(SchemaValidationError, match="confidence"):
        validate_atomic_note_frontmatter(meta)


def test_empty_sources_fails():
    meta = _valid_note_meta()
    meta["sources"] = []
    with pytest.raises(SchemaValidationError, match="sources"):
        validate_atomic_note_frontmatter(meta)


def test_invalid_date_fails():
    meta = _valid_note_meta()
    meta["captured"] = "April 12, 2026"
    with pytest.raises(SchemaValidationError, match="captured"):
        validate_atomic_note_frontmatter(meta)


def test_invalid_filename_fails():
    meta = _valid_note_meta()
    with pytest.raises(SchemaValidationError, match="filename"):
        validate_atomic_note_frontmatter(meta, filename="random_file.md")


def test_filename_with_year_quarter_suffix_passes():
    """Wave 6 — time-bounded filename pattern."""
    meta = _valid_note_meta()
    meta["type"] = "decision"
    validate_atomic_note_frontmatter(
        meta, filename="decision_2026-q3_income_target.md"
    )


def test_filename_with_year_only_suffix_passes():
    meta = _valid_note_meta()
    meta["type"] = "project"
    validate_atomic_note_frontmatter(
        meta, filename="project_2026_april_consulting_launch.md"
    )


def test_validate_capture_valid():
    capture = {
        "type": "feedback",
        "name": "Q1 bonus reaffirmation",
        "description": "Dan reaffirmed bonuses route to credit-cards-first",
        "confidence": "high",
        "sources": ["conversation_2026-05-06"],
        "body": "Body content here.",
    }
    validate_capture(capture)


def test_validate_capture_missing_field():
    with pytest.raises(SchemaValidationError, match="missing required field"):
        validate_capture({"type": "feedback"})


def test_derive_filename_basic():
    assert derive_filename("feedback", "Bottom-line communication") == \
        "feedback_bottom_line_communication.md"


def test_derive_filename_handles_punctuation():
    assert derive_filename("decision", "Q3 income — target") == \
        "decision_q3_income_target.md"

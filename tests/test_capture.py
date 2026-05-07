"""Tests for atomic_agents._capture."""

import json
from pathlib import Path

import pytest

from atomic_agents._capture import (
    extract_captures,
    write_atomic_note,
    enforce_write_path,
)
from atomic_agents.exceptions import (
    SchemaValidationError,
    WritePathViolation,
)
from atomic_agents.types import Capture


def test_extract_one_capture():
    response = '''Here is my response.

```atomic_capture
{
  "type": "feedback",
  "name": "Test capture",
  "description": "A test",
  "confidence": "high",
  "sources": ["conversation_2026-05-08"],
  "body": "Body content."
}
```

End of response.'''
    captures, failures = extract_captures(response)
    assert len(captures) == 1
    assert len(failures) == 0
    assert captures[0].type == "feedback"
    assert captures[0].name == "Test capture"


def test_extract_multiple_captures():
    response = '''Response.

```atomic_capture
{"type":"feedback","name":"A","description":"x","confidence":"high","sources":["s1"],"body":"a"}
```

More.

```atomic_capture
{"type":"decision","name":"B","description":"y","confidence":"high","sources":["s2"],"body":"b"}
```'''
    captures, failures = extract_captures(response)
    assert len(captures) == 2
    assert captures[0].type == "feedback"
    assert captures[1].type == "decision"


def test_extract_handles_quadruple_backtick_fence():
    """Per spec/05 — quadruple-backticks for body containing triple backticks."""
    response = '''Response.

````atomic_capture
{"type":"feedback","name":"C","description":"x","confidence":"high","sources":["s1"],"body":"contains ```code```"}
````'''
    captures, failures = extract_captures(response)
    assert len(captures) == 1


def test_extract_invalid_json_recorded_as_failure():
    response = '''```atomic_capture
{"type": "feedback", missing quote
```'''
    captures, failures = extract_captures(response)
    assert len(captures) == 0
    assert len(failures) == 1


def test_extract_invalid_schema_recorded_as_failure():
    response = '''```atomic_capture
{"type":"bogus","name":"x","description":"y","confidence":"high","sources":["s1"],"body":"b"}
```'''
    captures, failures = extract_captures(response)
    assert len(captures) == 0
    assert len(failures) == 1


def test_extract_dedupes_identical_captures():
    capture_json = '{"type":"feedback","name":"Same","description":"x","confidence":"high","sources":["s1"],"body":"identical body"}'
    response = f'```atomic_capture\n{capture_json}\n```\n\n```atomic_capture\n{capture_json}\n```'
    captures, failures = extract_captures(response)
    assert len(captures) == 1


def test_write_atomic_note(tmp_path):
    """Full write cycle — capture creates a note + INDEX entry."""
    agent_root = tmp_path / "myagent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = Capture(
        type="feedback",
        name="Bottom-line first",
        description="Dan wants the recommendation in 1-3 sentences",
        confidence="high",
        sources=["conversation_2026-05-08"],
        body="Body content here.",
    )

    path = write_atomic_note(
        agent_root, capture, write_paths=[memory_dir]
    )

    assert path.exists()
    assert path.name == "feedback_bottom_line_first.md"
    content = path.read_text()
    assert "schema_version: 1" in content
    assert "type: feedback" in content
    assert "Body content here." in content

    # INDEX should have an entry
    index_text = (memory_dir / "INDEX.md").read_text()
    assert "[Bottom-line first]" in index_text
    assert "feedback_bottom_line_first.md" in index_text


def test_write_outside_path_raises(tmp_path):
    agent_root = tmp_path / "myagent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    capture = Capture(
        type="feedback",
        name="x",
        description="x",
        confidence="high",
        sources=["s1"],
        body="b",
    )

    # write_paths only includes other_dir, not the agent's memory dir
    with pytest.raises(WritePathViolation):
        write_atomic_note(agent_root, capture, write_paths=[other_dir])


def test_enforce_write_path_under_allowed(tmp_path):
    target = tmp_path / "memory" / "x.md"
    enforce_write_path(target, [tmp_path / "memory"])  # should not raise


def test_enforce_write_path_outside_raises(tmp_path):
    target = tmp_path / "elsewhere" / "x.md"
    with pytest.raises(WritePathViolation):
        enforce_write_path(target, [tmp_path / "memory"])

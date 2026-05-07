"""Tests for atomic_agents._capture."""

import json
from pathlib import Path

import pytest

from atomic_agents._capture import (
    CAPTURE_TOOL_DESCRIPTION,
    CAPTURE_TOOL_SCHEMA,
    anthropic_tool_definition,
    extract_all_captures,
    extract_captures,
    extract_tool_call_captures,
    enforce_write_path,
    openai_tool_definition,
    write_atomic_note,
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


# ──────────────────────────────────────────────────────────────────
# Path 1: tool-call schema and extractor


def test_anthropic_tool_definition_shape():
    td = anthropic_tool_definition()
    assert td["name"] == "atomic_capture"
    assert td["description"] == CAPTURE_TOOL_DESCRIPTION
    assert td["input_schema"] is CAPTURE_TOOL_SCHEMA


def test_openai_tool_definition_shape():
    td = openai_tool_definition()
    assert td["type"] == "function"
    assert td["function"]["name"] == "atomic_capture"
    assert td["function"]["description"] == CAPTURE_TOOL_DESCRIPTION
    assert td["function"]["parameters"] is CAPTURE_TOOL_SCHEMA


def test_capture_tool_schema_required_fields():
    """Schema must require the same 6 fields the fenced-block validator does."""
    required = set(CAPTURE_TOOL_SCHEMA["required"])
    assert required == {"type", "name", "description", "confidence", "sources", "body"}


def test_capture_tool_schema_type_enum_matches_taxonomy():
    type_enum = set(CAPTURE_TOOL_SCHEMA["properties"]["type"]["enum"])
    assert type_enum == {"user", "feedback", "project", "decision", "reference"}


def test_capture_tool_schema_disallows_additional_properties():
    """No surprise fields — keeps the schema and the validator in sync."""
    assert CAPTURE_TOOL_SCHEMA["additionalProperties"] is False


def test_extract_tool_call_captures_one_valid():
    tool_uses = [{
        "id": "toolu_01abc",
        "name": "atomic_capture",
        "input": {
            "type": "feedback",
            "name": "Path 1 works",
            "description": "tool-call extracted via SDK",
            "confidence": "high",
            "sources": ["conversation_2026-05-06"],
            "body": "Body from a tool call.",
        },
    }]
    captures, failures = extract_tool_call_captures(tool_uses)
    assert len(captures) == 1
    assert len(failures) == 0
    assert captures[0].name == "Path 1 works"
    assert captures[0].body == "Body from a tool call."


def test_extract_tool_call_captures_ignores_other_tools():
    """Non-atomic_capture tool calls should be ignored, not error."""
    tool_uses = [
        {"id": "t1", "name": "some_other_tool", "input": {"foo": "bar"}},
        {"id": "t2", "name": "atomic_capture", "input": {
            "type": "decision", "name": "Pick X",
            "description": "We picked X for reason Y",
            "confidence": "high", "sources": ["s1"], "body": "Body.",
        }},
    ]
    captures, failures = extract_tool_call_captures(tool_uses)
    assert len(captures) == 1
    assert captures[0].name == "Pick X"
    assert len(failures) == 0


def test_extract_tool_call_captures_invalid_input_recorded_as_failure():
    """Schema-invalid tool input should land in failures, not raise."""
    tool_uses = [{
        "id": "t1", "name": "atomic_capture",
        "input": {"type": "bogus_type", "name": "x", "description": "y",
                  "confidence": "high", "sources": ["s1"], "body": "b"},
    }]
    captures, failures = extract_tool_call_captures(tool_uses)
    assert len(captures) == 0
    assert len(failures) == 1
    assert "bogus" in failures[0][1].lower() or "type" in failures[0][1].lower()


def test_extract_tool_call_captures_missing_required_field():
    tool_uses = [{
        "id": "t1", "name": "atomic_capture",
        "input": {"type": "feedback", "name": "x"},  # missing description, confidence, sources, body
    }]
    captures, failures = extract_tool_call_captures(tool_uses)
    assert len(captures) == 0
    assert len(failures) == 1


def test_extract_tool_call_captures_empty_input_ignored():
    tool_uses = [{"id": "t1", "name": "atomic_capture", "input": {}}]
    captures, failures = extract_tool_call_captures(tool_uses)
    assert len(captures) == 0
    assert len(failures) == 1


def test_extract_tool_call_captures_empty_list():
    captures, failures = extract_tool_call_captures([])
    assert captures == []
    assert failures == []


def test_extract_all_captures_combines_both_paths():
    """Both tool_use AND fenced JSON in same response — both captured, deduped."""
    text = '''Some text.

```atomic_capture
{"type":"feedback","name":"From text","description":"x","confidence":"high","sources":["s1"],"body":"text body"}
```

End.'''
    tool_uses = [{
        "id": "t1", "name": "atomic_capture",
        "input": {
            "type": "decision", "name": "From tool call",
            "description": "y", "confidence": "high",
            "sources": ["s2"], "body": "tool body",
        },
    }]
    captures, failures = extract_all_captures(text, tool_uses=tool_uses)
    names = {c.name for c in captures}
    assert names == {"From text", "From tool call"}
    assert len(failures) == 0


def test_extract_all_captures_dedupes_when_both_paths_emit_same_observation():
    """If model emits same capture in both tool_use and fenced block, keep one."""
    same_input = {
        "type": "feedback", "name": "Same observation",
        "description": "x", "confidence": "high",
        "sources": ["s1"], "body": "Identical body.",
    }
    text = f'''Response.

```atomic_capture
{json.dumps(same_input)}
```'''
    tool_uses = [{"id": "t1", "name": "atomic_capture", "input": same_input}]
    captures, failures = extract_all_captures(text, tool_uses=tool_uses)
    assert len(captures) == 1
    assert captures[0].name == "Same observation"


def test_extract_all_captures_with_no_tool_uses_falls_back_to_text():
    text = '''```atomic_capture
{"type":"feedback","name":"Only text","description":"x","confidence":"high","sources":["s1"],"body":"b"}
```'''
    captures, failures = extract_all_captures(text, tool_uses=None)
    assert len(captures) == 1
    assert captures[0].name == "Only text"


def test_extract_all_captures_empty_text_only_tool_uses():
    tool_uses = [{
        "id": "t1", "name": "atomic_capture",
        "input": {
            "type": "user", "name": "Tool only",
            "description": "x", "confidence": "high",
            "sources": ["s1"], "body": "b",
        },
    }]
    captures, failures = extract_all_captures("", tool_uses=tool_uses)
    assert len(captures) == 1
    assert captures[0].name == "Tool only"


def test_extract_all_captures_aggregates_failures_from_both_paths():
    """Bad fenced block + bad tool input → both reported in failures."""
    bad_text = '''```atomic_capture
{"type": "feedback", malformed
```'''
    bad_tool_uses = [{
        "id": "t1", "name": "atomic_capture",
        "input": {"type": "bogus", "name": "x", "description": "y",
                  "confidence": "high", "sources": ["s1"], "body": "b"},
    }]
    captures, failures = extract_all_captures(bad_text, tool_uses=bad_tool_uses)
    assert len(captures) == 0
    assert len(failures) == 2  # one fenced, one tool


def test_merge_into_blocks_path_traversal(tmp_path):
    """merge_into with a relative traversal path must raise WritePathViolation."""
    agent_root = tmp_path / "myagent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    # Create a file outside memory/ that a traversal payload could target
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("sensitive content\n")

    capture = Capture(
        type="feedback",
        name="Malicious merge",
        description="Attempt to traverse outside memory/",
        confidence="high",
        sources=["attacker"],
        body="Overwritten content.",
        merge_into="../outside.md",
    )

    with pytest.raises(WritePathViolation):
        write_atomic_note(agent_root, capture, write_paths=[memory_dir])


def test_extract_tool_call_captures_with_optional_fields():
    """Optional fields (pinned, expires_at, supersedes, tags) flow through."""
    tool_uses = [{
        "id": "t1", "name": "atomic_capture",
        "input": {
            "type": "project", "name": "Big project",
            "description": "Q3 launch coordination",
            "confidence": "medium", "sources": ["s1"],
            "body": "Body.",
            "pinned": True,
            "expires_at": "2026-09-30",
            "tags": ["q3", "launch"],
        },
    }]
    captures, failures = extract_tool_call_captures(tool_uses)
    assert len(captures) == 1
    cap = captures[0]
    assert cap.pinned is True
    assert cap.expires_at == "2026-09-30"
    assert cap.tags == ["q3", "launch"]


# ──────────────────────────────────────────────────────────────────
# Orphan recovery (Codex finding #2)


def test_capture_orphan_recovery_repairs_index(tmp_path):
    """If a note exists but INDEX is missing/stale, re-submitting the same
    capture must repair the INDEX instead of raising 'already exists'."""
    agent_root = tmp_path / "myagent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    capture = Capture(
        type="feedback",
        name="Orphan note",
        description="This note was written but INDEX update failed.",
        confidence="high",
        sources=["conversation_orphan"],
        body="The orphaned body content.",
    )

    # Phase 1: write the note directly (simulating a successful Phase 1 +
    # failed Phase 2 on a previous run). INDEX intentionally absent.
    from atomic_agents._capture import _render_note
    from atomic_agents._io import atomic_write
    from datetime import date

    filename = "feedback_orphan_note.md"
    target = memory_dir / filename
    atomic_write(target, _render_note(capture, date.today()))

    # Verify INDEX doesn't mention this note yet.
    index_path = memory_dir / "INDEX.md"
    assert not index_path.exists() or filename not in index_path.read_text()

    # Phase 2: submit the identical capture again — should repair, not raise.
    result_path = write_atomic_note(agent_root, capture, write_paths=[memory_dir])

    assert result_path == target
    index_text = (memory_dir / "INDEX.md").read_text()
    assert "Orphan note" in index_text
    assert filename in index_text


def test_capture_existing_note_different_content_still_raises(tmp_path):
    """Same filename, different body — must still raise SchemaValidationError
    (real conflict, operator must investigate)."""
    agent_root = tmp_path / "myagent"
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True)

    original_capture = Capture(
        type="feedback",
        name="Same name note",
        description="Original description.",
        confidence="high",
        sources=["s1"],
        body="Original body.",
    )
    # Write original note + INDEX.
    write_atomic_note(agent_root, original_capture, write_paths=[memory_dir])

    # Now try a second capture with same name but different body.
    conflicting_capture = Capture(
        type="feedback",
        name="Same name note",
        description="Original description.",  # same description
        confidence="high",
        sources=["s2"],
        body="Completely different body — real conflict.",
    )
    with pytest.raises(SchemaValidationError, match="already exists"):
        write_atomic_note(agent_root, conflicting_capture, write_paths=[memory_dir])

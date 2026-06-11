"""Round-trip conformance tests for the Exportable Protocol (spec/40 PR1).

Tests the canonical-shape export contract for the five PR1 state backends:
Memory, Log, Mandate, Corpus, and Lock. Also tests SecretBackend's
never-leak invariant (MUST 9).

Architecture (spec/40 §"Conformance test architecture"):
- ONE shared assert_canonical_roundtrip() helper with per-protocol fixture
  factories. The helper's job: call write_fn, call backend.export(), render
  bytes, compare to expected_bytes.
- Capability-gated skips via get_supports_canonical_export() from
  test_export_capability_advertisement.py.
- Per-protocol test files (this module) own fixture creation AND
  expected-byte generation.

Tier A (filesystem) fidelity caveats (spec/40 §"Tier A fidelity caveats"):
- BYTE-FOR-BYTE holds for notes/pages without extra_frontmatter.
- BYTE-FOR-BYTE holds for RunRecord written via LogBackend Protocol.
- Pre-Protocol legacy records are Tier B equivalent (key-order may differ).

The expected-bytes for each round-trip are verified against files written
by the REAL backend write methods (append/write_note/write_page), not just
against the renderer's output in isolation — this forces the test to catch
any sort_keys divergence (spec/40 prep finding P1).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_export_capability_advertisement import get_supports_canonical_export


# ──────────────────────────────────────────────────────────────────────────────
# Shared assert_canonical_roundtrip helper (spec/40 §"Conformance test
# architecture").
#
# Design: assert_canonical_roundtrip(backend, write_fn, expected_bytes_fn)
# - write_fn(backend): populates the backend with test data.
# - expected_bytes_fn(backend): returns the expected export bytes (reading
#   from REAL on-disk fixture files written by write_fn).
# - The helper calls export(), renders bytes, compares to expected.


def assert_canonical_roundtrip(backend, write_fn, expected_bytes_fn, *, query=None):
    """Assert that export() → renderer produces byte-exact expected output.

    Args:
        backend: a backend that satisfies Exportable.
        write_fn: callable(backend) -> Any — populates the backend.
        expected_bytes_fn: callable(backend) -> bytes — returns expected bytes
            read from the REAL on-disk fixture (not renderer output).
        query: optional export query. None for unbounded.
    """
    if not get_supports_canonical_export(backend):
        pytest.skip(
            f"{type(backend).__name__} does not support canonical export "
            "(supports_canonical_export=False)"
        )

    # Populate the backend
    write_fn(backend)

    # Export
    result = backend.export(query)
    assert result is not None, "export() must not return None"

    # Get expected bytes (from real on-disk files)
    expected = expected_bytes_fn(backend)

    # Compare
    assert expected == b"SKIP_COMPARISON" or isinstance(expected, bytes), (
        "expected_bytes_fn must return bytes or b'SKIP_COMPARISON'"
    )
    if expected != b"SKIP_COMPARISON":
        actual = _render_export_result(result)
        assert actual == expected, (
            f"Round-trip bytes mismatch for {type(backend).__name__}.\n"
            f"Expected ({len(expected)} bytes):\n{expected[:500]!r}\n"
            f"Actual ({len(actual)} bytes):\n{actual[:500]!r}"
        )

    return result


def _render_export_result(result) -> bytes:
    """Dispatch to the right renderer based on export result type."""
    from atomic_agents.export.renderer import (
        render_corpus_page_bytes_from_raw,
        render_note_bytes_from_raw,
    )
    from atomic_agents.export.types import (
        CorpusExport,
        GoalExport,
        LockExport,
        LogExport,
        MandateExport,
        MemoryExport,
        SecretExport,
    )

    if isinstance(result, GoalExport):
        # Tier A passthrough: goal.md bytes + history lines + archive bytes,
        # all raw (already CRLF-normalized by the backend). No re-serialization.
        parts = [result.goal_md_bytes]
        parts.extend(result.history_records_with_bytes)
        for _slug, raw_bytes in result.archived_goals_with_bytes:
            parts.append(raw_bytes)
        return b"".join(parts)
    if isinstance(result, MemoryExport):
        parts = []
        for _note, raw_bytes in result.notes_with_bytes:
            parts.append(render_note_bytes_from_raw(raw_bytes))
        return b"".join(parts)
    elif isinstance(result, LogExport):
        parts = []
        for _record, raw_bytes in result.records_with_bytes:
            parts.append(raw_bytes)
        return b"".join(parts)
    elif isinstance(result, MandateExport):
        from atomic_agents.export.renderer import render_mandates_md

        parts = []
        for scope, mandates in result.mandates_by_scope.items():
            meta = result.meta_by_scope.get(scope)
            text = render_mandates_md(mandates, meta)
            parts.append(text.encode("utf-8"))
        return b"".join(parts)
    elif isinstance(result, CorpusExport):
        parts = []
        for corpus_name in ("wiki", "raw"):
            for _page, raw_bytes in result.pages_with_bytes.get(corpus_name, []):
                parts.append(render_corpus_page_bytes_from_raw(raw_bytes))
        return b"".join(parts)
    elif isinstance(result, LockExport):
        # LockExport has no data bytes — it's a configuration record only
        return b""
    elif isinstance(result, SecretExport):
        from atomic_agents.export.renderer import render_secret_export_bytes

        return render_secret_export_bytes(result.entries)
    else:
        raise TypeError(f"Unknown export result type: {type(result)}")


# ──────────────────────────────────────────────────────────────────────────────
# Memory conformance


@pytest.fixture
def memory_backend(tmp_path: Path):
    from atomic_agents.memory.filesystem import FilesystemBackend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    return FilesystemBackend(agent_root)


def _make_capture(
    name="test_note",
    body="Test body content.",
    type_="feedback",
    description="A test note",
):
    from atomic_agents.types import Capture

    return Capture(
        name=name,
        type=type_,
        description=description,
        body=body,
        confidence="high",
        sources=["test"],
        supersedes=None,
        merge_into=None,
        pinned=False,
        expires_at=None,
        tags=[],
    )


def test_memory_export_returns_memory_export_type(memory_backend) -> None:
    """export() returns a MemoryExport instance."""
    from atomic_agents.export.types import MemoryExport

    result = memory_backend.export()
    assert isinstance(result, MemoryExport)


def test_memory_export_empty_returns_empty_notes(memory_backend) -> None:
    """export() on an empty memory returns empty notes list."""
    from atomic_agents.export.types import MemoryExport

    result = memory_backend.export()
    assert isinstance(result, MemoryExport)
    assert result.notes_with_bytes == []


def test_memory_export_all_equals_export_none(memory_backend) -> None:
    """export_all() produces the same result as export(None)."""
    from atomic_agents.memory.backend import WritePolicy

    capture = _make_capture()
    policy = WritePolicy(write_paths=[memory_backend._agent_root])
    memory_backend.write_note(capture, policy)

    result_none = memory_backend.export(None)
    result_all = memory_backend.export_all()
    assert len(result_none.notes_with_bytes) == len(result_all.notes_with_bytes)


def test_memory_export_single_note_roundtrip(memory_backend) -> None:
    """Writing a note and exporting returns byte-exact raw file content."""
    from atomic_agents.memory.backend import WritePolicy

    capture = _make_capture(
        name="roundtrip_note",
        body="Round-trip test body.",
        description="A round-trip test note",
    )
    policy = WritePolicy(write_paths=[memory_backend._agent_root])
    memory_backend.write_note(capture, policy)

    # The filesystem backend prepends the type to the filename (derive_filename).
    # Use list_notes() to discover the actual filename rather than guessing.
    memory_dir = memory_backend._memory_dir
    refs = memory_backend.list_notes()
    assert len(refs) == 1, f"Expected 1 note after write, got {len(refs)}"
    note_path = memory_dir / refs[0].name
    assert note_path.exists(), f"Note file must exist at {note_path}"

    # Read raw bytes from disk — the ground truth for Tier A
    expected_bytes = note_path.read_bytes()

    result = memory_backend.export()
    assert len(result.notes_with_bytes) == 1
    _note, raw_bytes = result.notes_with_bytes[0]

    # The export MUST return the raw file bytes (not re-rendered)
    assert raw_bytes == expected_bytes, (
        "MemoryExport raw_bytes must match the on-disk file bytes exactly "
        "(Tier A byte-exact fidelity, spec/40 MUST 4)"
    )


def test_memory_export_note_has_no_crlf_no_bom(memory_backend) -> None:
    """Exported note bytes must use LF line endings and must not have a UTF-8 BOM.

    spec/40 MUST 5: UTF-8, LF line endings, NO byte-order mark.
    """
    from atomic_agents.memory.backend import WritePolicy

    capture = _make_capture()
    policy = WritePolicy(write_paths=[memory_backend._agent_root])
    memory_backend.write_note(capture, policy)

    result = memory_backend.export()
    for _note, raw_bytes in result.notes_with_bytes:
        assert b"\r\n" not in raw_bytes, (
            "Note bytes must not contain CRLF (spec/40 MUST 5: LF line endings)"
        )
        assert not raw_bytes.startswith(b"\xef\xbb\xbf"), (
            "Note bytes must not start with UTF-8 BOM (spec/40 MUST 5: no BOM)"
        )


def test_memory_export_backend_id_and_scope(memory_backend) -> None:
    """MemoryExport carries backend_id and scope."""
    result = memory_backend.export()
    assert result.backend_id == "filesystem"
    assert result.scope != ""


def test_memory_export_multiple_notes(memory_backend) -> None:
    """export() returns all notes written to the backend."""
    from atomic_agents.memory.backend import WritePolicy

    policy = WritePolicy(write_paths=[memory_backend._agent_root])
    for i in range(3):
        capture = _make_capture(name=f"note_{i}", description=f"Note {i}")
        memory_backend.write_note(capture, policy)

    result = memory_backend.export()
    assert len(result.notes_with_bytes) == 3


# ──────────────────────────────────────────────────────────────────────────────
# Log conformance


@pytest.fixture
def log_backend(tmp_path: Path):
    from atomic_agents.logs.filesystem import FilesystemLogBackend

    return FilesystemLogBackend(tmp_path / "agent")


def _make_run_record(
    ts: str | None = None,
    summary: str = "test record",
    primitive: str = "agent_call",
):
    from atomic_agents.logs.types import RunRecord

    if ts is None:
        ts = datetime.now(tz=timezone.utc).isoformat()
    return RunRecord(
        ts=ts,
        run_id="test-run-001",
        primitive=primitive,
        status="ok",
        summary=summary,
        model="claude-3-5-sonnet-20241022",
        input_tokens=100,
        output_tokens=50,
    )


def test_log_export_returns_log_export_type(log_backend) -> None:
    """export() returns a LogExport instance."""
    from atomic_agents.export.types import LogExport

    result = log_backend.export()
    assert isinstance(result, LogExport)


def test_log_export_empty_returns_empty_records(log_backend) -> None:
    """export() on an empty log returns empty records list."""
    from atomic_agents.export.types import LogExport

    result = log_backend.export()
    assert isinstance(result, LogExport)
    assert result.records_with_bytes == []


def test_log_export_single_record_roundtrip(log_backend, tmp_path: Path) -> None:
    """Appending a record and exporting returns byte-exact JSONL content.

    CRITICAL: The expected bytes come from real on-disk file content written by
    append(), NOT from re-running the renderer in isolation. This forces the
    test to catch any sort_keys divergence (spec/40 prep finding P1 / MUST 8).
    """
    record = _make_run_record(summary="roundtrip test")
    log_backend.append(record)

    # Find the JSONL file that was written
    log_dir = log_backend._log_dir
    assert log_dir.exists(), "Log dir must be created by append()"
    jsonl_files = list(log_dir.rglob("*.jsonl"))
    assert len(jsonl_files) == 1, "Exactly one JSONL file after one append"

    # Read the raw bytes from disk — ground truth for Tier A
    on_disk_content = jsonl_files[0].read_bytes()

    result = log_backend.export()
    assert len(result.records_with_bytes) == 1
    _rec, raw_bytes = result.records_with_bytes[0]

    # The export bytes must match the on-disk bytes exactly
    assert raw_bytes == on_disk_content, (
        "LogExport raw_bytes must match the on-disk JSONL bytes exactly "
        "(Tier A byte-exact fidelity, spec/40 MUST 4 + MUST 8)"
    )


def test_log_export_bytes_use_ts_first_order(log_backend) -> None:
    """Exported record bytes must use ts-first insertion order, NOT sorted keys.

    This is the spec/40 MUST 8 check: json.dumps(record.to_dict()) NOT
    canonical_json(). The ts field must appear first in the JSON object.
    """
    record = _make_run_record(summary="key order test")
    log_backend.append(record)

    result = log_backend.export()
    assert len(result.records_with_bytes) == 1
    _rec, raw_bytes = result.records_with_bytes[0]

    # Parse the JSON and verify ts is first key
    line = raw_bytes.decode("utf-8").strip()
    parsed = json.loads(line)
    keys = list(parsed.keys())
    assert keys[0] == "ts", (
        f"First key in exported RunRecord must be 'ts' (ts-first insertion order). "
        f"Got: {keys[0]!r}. This indicates canonical_json (sort_keys=True) was "
        f"used instead of json.dumps(record.to_dict()) — spec/40 MUST 8 violation."
    )


def test_log_export_legacy_line_exported_verbatim(log_backend) -> None:
    """A legacy/reordered on-disk JSONL line must export BYTE-FOR-BYTE.

    spec/40 MUST 4 + prep finding P1: export_log reads the EXACT on-disk bytes,
    not bytes re-derived from the parsed record. A line written with a different
    key order than the current to_dict() (e.g. a hand-edited or legacy line) must
    round-trip verbatim — re-serializing through json.dumps(record.to_dict())
    would silently reorder keys and lose this fidelity.
    """
    # Write the first record normally so the backend creates its shard layout.
    rec = _make_run_record(summary="ordering anchor")
    log_backend.append(rec)
    log_dir = log_backend._log_dir
    jsonl_files = list(log_dir.rglob("*.jsonl"))
    assert len(jsonl_files) == 1
    shard = jsonl_files[0]

    # Append a hand-crafted legacy line with run_id FIRST (not ts-first) and a
    # string-typed token count — a shape the current to_dict() would not emit.
    legacy_line = (
        '{"run_id": "legacy-001", "ts": "2026-06-01T00:00:00+00:00", '
        '"primitive": "agent_call", "status": "ok", "summary": "legacy record", '
        '"input_tokens": "100"}'
    )
    with shard.open("a", encoding="utf-8") as fh:
        fh.write(legacy_line + "\n")

    result = log_backend.export()
    # Find the exported tuple for the legacy record.
    legacy_pairs = [
        (rec, rb) for rec, rb in result.records_with_bytes if rec.run_id == "legacy-001"
    ]
    assert len(legacy_pairs) == 1, "legacy record must appear exactly once"
    _rec, raw_bytes = legacy_pairs[0]
    assert raw_bytes == (legacy_line + "\n").encode("utf-8"), (
        "Legacy on-disk line must export byte-for-byte — export_log must read "
        "the actual disk bytes, NOT re-serialize via json.dumps(to_dict()) "
        "(spec/40 MUST 4)"
    )


def test_log_export_bytes_not_sort_keys(log_backend) -> None:
    """Exported bytes must NOT use sort_keys=True (would produce alphabetical order)."""
    record = _make_run_record(summary="no sort keys")
    log_backend.append(record)

    result = log_backend.export()
    _rec, raw_bytes = result.records_with_bytes[0]

    line = raw_bytes.decode("utf-8").strip()
    # If sort_keys were used, "cache_hit_tokens" would appear before "input_tokens"
    # and "model" would appear before "primitive". Verify ts is first.
    assert line.startswith('{"ts"'), (
        'Exported JSONL must start with {"ts" — sort_keys was not used (spec/40 MUST 8)'
    )


def test_log_export_no_crlf_no_bom(log_backend) -> None:
    """Exported log bytes must use LF line endings and must not have a UTF-8 BOM.

    spec/40 MUST 5: UTF-8, LF line endings, NO byte-order mark.
    """
    record = _make_run_record()
    log_backend.append(record)

    result = log_backend.export()
    for _rec, raw_bytes in result.records_with_bytes:
        assert b"\r\n" not in raw_bytes, (
            "Log bytes must not contain CRLF (spec/40 MUST 5: LF line endings)"
        )
        assert not raw_bytes.startswith(b"\xef\xbb\xbf"), (
            "Log bytes must not start with UTF-8 BOM (spec/40 MUST 5: no BOM)"
        )


def test_log_export_backend_id_and_scope(log_backend) -> None:
    """LogExport carries backend_id and scope."""
    result = log_backend.export()
    assert result.backend_id == "filesystem"
    assert result.scope != ""


def test_log_export_all_equals_export_none(log_backend) -> None:
    """export_all() produces same records as export(None)."""
    record = _make_run_record()
    log_backend.append(record)

    result_none = log_backend.export(None)
    result_all = log_backend.export_all()
    assert len(result_none.records_with_bytes) == len(result_all.records_with_bytes)


def test_log_export_bounded_query_skips_out_of_window_shards(
    log_backend, tmp_path: Path
) -> None:
    """A bounded LogExportQuery MUST NOT read shards outside its date window.

    spec/40 FIX 1: the shard walk in export_log must be gated on the query's
    date window so export(query) does not re-introduce the full-materialization
    that the #379 granularity ruling forbade.

    Test strategy: write records into two far-apart month shards, query only
    the recent one, assert (a) the result contains only the in-window record
    and (b) the out-of-window record is absent from the result (correctness
    check — if both shards were read, both records would appear).
    """
    from atomic_agents.export.types import LogExportQuery
    from atomic_agents.logs.types import LogQuery

    # ── Write an OLD record manually into a 2020-01 shard ─────────────────────
    # We bypass append() to place the shard in the past without mocking the clock.
    old_ts = "2020-01-15T12:00:00+00:00"
    old_run_id = "old-run-2020"
    old_shard = log_backend._log_dir / "2020-01" / "2020-01-15.jsonl"
    old_shard.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    old_line = _json.dumps(
        {
            "ts": old_ts,
            "run_id": old_run_id,
            "primitive": "agent_call",
            "status": "ok",
            "summary": "old record in 2020",
        }
    )
    old_shard.write_text(old_line + "\n", encoding="utf-8")

    # ── Write a RECENT record via the normal append() path ────────────────────
    recent_ts = "2026-06-01T10:00:00+00:00"
    recent_record = _make_run_record(ts=recent_ts, summary="recent record in 2026")
    log_backend.append(recent_record)

    # ── Bounded export: only records from 2026 ────────────────────────────────
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    until = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    bounded_query = LogExportQuery(log_query=LogQuery(since=since, until=until))

    result = log_backend.export(bounded_query)

    run_ids = {rec.run_id for rec, _ in result.records_with_bytes}

    # The bounded export MUST include the in-window record.
    assert recent_record.run_id in run_ids, (
        "Bounded export must include the in-window (2026) record."
    )

    # The bounded export MUST NOT include the 2020 record — if both shards were
    # read, the old record would appear here.
    assert old_run_id not in run_ids, (
        "Bounded export MUST NOT include the 2020 record when the query window "
        "is 2026-only. If this fails, export_log is reading ALL shards regardless "
        "of the date window (spec/40 FIX 1 regression)."
    )

    # Sanity: unbounded export_all() DOES include both records.
    all_result = log_backend.export_all()
    all_run_ids = {rec.run_id for rec, _ in all_result.records_with_bytes}
    assert old_run_id in all_run_ids, (
        "export_all() must include the 2020 shard (unbounded walk)."
    )
    assert recent_record.run_id in all_run_ids, (
        "export_all() must include the 2026 record."
    )


def test_log_export_all_blank_ts_line_exported_verbatim(
    log_backend, tmp_path: Path
) -> None:
    """export_all() MUST export a blank-ts line byte-for-byte (regression guard).

    A blank-ts line lands in today's shard via _record_date's date.today()
    fallback.  Before the F1 fix, export_all derived the shard window from
    min/max ts of the queried records.  A blank-ts line produces a RunRecord
    with ts="" which is excluded from the derived min/max, so today's shard
    could be skipped — causing the line to be re-serialized (key-reordering
    loss of verbatim fidelity) instead of exported verbatim.

    This test MUST FAIL against the records-derived-window code and PASS
    after the query-bounds fix (spec/40 F1 regression guard).
    """
    # Write a normal record first to ensure the log dir exists.
    _make_run_record(ts="2020-01-15T00:00:00+00:00", summary="anchor record")
    old_shard = log_backend._log_dir / "2020-01" / "2020-01-15.jsonl"
    old_shard.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    old_shard.write_text(
        _json.dumps(
            {
                "ts": "2020-01-15T00:00:00+00:00",
                "run_id": "anchor-001",
                "primitive": "agent_call",
                "status": "ok",
                "summary": "anchor record",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Place a hand-crafted blank-ts line in today's shard directly.
    # blank-ts lines land in today's shard because _record_date() falls back
    # to date.today() when ts is absent or blank.
    blank_ts_run_id = "blank-ts-verbatim-001"
    blank_ts_line = (
        f'{{"run_id": "{blank_ts_run_id}", "ts": "", '
        '"primitive": "agent_call", "status": "ok", "summary": "blank-ts line"}'
    )
    from datetime import date as _date

    today_str = _date.today().isoformat()
    today_month = today_str[:7]
    today_shard = log_backend._log_dir / today_month / f"{today_str}.jsonl"
    today_shard.parent.mkdir(parents=True, exist_ok=True)
    with today_shard.open("a", encoding="utf-8") as fh:
        fh.write(blank_ts_line + "\n")

    # export_all() MUST include the blank-ts line AND export it verbatim.
    all_result = log_backend.export_all()
    blank_pairs = [
        (rec, rb)
        for rec, rb in all_result.records_with_bytes
        if rec.run_id == blank_ts_run_id
    ]
    assert len(blank_pairs) == 1, (
        f"export_all() must include the blank-ts line; got {len(blank_pairs)} matches. "
        "If zero, the shard containing it was skipped (records-derived-window bug, "
        "spec/40 F1 regression)."
    )
    _rec, raw_bytes = blank_pairs[0]
    assert raw_bytes == (blank_ts_line + "\n").encode("utf-8"), (
        "Blank-ts line must export byte-for-byte (verbatim fidelity, spec/40 MUST 4). "
        f"Expected: {(blank_ts_line + chr(10)).encode()!r}\n"
        f"Got:      {raw_bytes!r}"
    )


def test_log_export_all_misfiled_line_exported_verbatim(
    log_backend, tmp_path: Path
) -> None:
    """export_all() MUST export a misfiled line byte-for-byte (regression guard).

    A 'misfiled' line has a ts in one month but physically lives in a
    different month's shard (e.g., hand-edited into the wrong file).
    Before the F1 fix, export_all derived the shard window from the queried
    records' ts values.  If the misfiled shard's month fell outside that
    derived window, the shard would be skipped and the line re-serialized
    instead of exported verbatim.

    This test MUST FAIL against the records-derived-window code and PASS
    after the query-bounds fix (spec/40 F1 regression guard).
    """
    import json as _json

    # Place a misfiled line: ts says 2025-06-15 but lives in the 2020-01 shard.
    misfiled_run_id = "misfiled-verbatim-001"
    misfiled_ts = "2025-06-15T10:00:00+00:00"
    misfiled_line = _json.dumps(
        {
            "run_id": misfiled_run_id,
            "ts": misfiled_ts,
            "primitive": "agent_call",
            "status": "ok",
            "summary": "misfiled record",
        }
    )
    # File it in the 2020-01 shard (WRONG month for this ts).
    wrong_shard = log_backend._log_dir / "2020-01" / "2020-01-15.jsonl"
    wrong_shard.parent.mkdir(parents=True, exist_ok=True)
    with wrong_shard.open("a", encoding="utf-8") as fh:
        fh.write(misfiled_line + "\n")

    # Also add a normal 2025 record so the records list is non-empty for
    # the pre-fix code's records-derived-window path.
    recent_record = _make_run_record(
        ts="2025-06-15T12:00:00+00:00", summary="normal 2025 record"
    )
    log_backend.append(recent_record)

    # export_all() MUST include the misfiled line AND export it verbatim.
    all_result = log_backend.export_all()
    misfiled_pairs = [
        (rec, rb)
        for rec, rb in all_result.records_with_bytes
        if rec.run_id == misfiled_run_id
    ]
    assert len(misfiled_pairs) == 1, (
        f"export_all() must include the misfiled line; got {len(misfiled_pairs)} matches. "
        "If zero, the 2020-01 shard was skipped because the records-derived window "
        "was narrowed to 2025 only (spec/40 F1 regression guard)."
    )
    _rec, raw_bytes = misfiled_pairs[0]
    assert raw_bytes == (misfiled_line + "\n").encode("utf-8"), (
        "Misfiled line must export byte-for-byte (verbatim fidelity, spec/40 MUST 4). "
        f"Expected: {(misfiled_line + chr(10)).encode()!r}\n"
        f"Got:      {raw_bytes!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Mandate conformance


@pytest.fixture
def mandate_backend(tmp_path: Path):
    from atomic_agents.mandate.filesystem import FilesystemMandateBackend

    return FilesystemMandateBackend(tmp_path / "scope")


def _write_mandates_md(scope_root: Path, scope: str, content: str) -> None:
    """Write a mandates.md file for the given scope."""
    if scope.startswith("project:"):
        path = scope_root / "mandates.md"
    else:
        name = scope.split(":", 1)[1]
        path = scope_root / name / "mandates.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_SIMPLE_MANDATES_MD = """\
## allow-read-files
granted_by: operator
granted_at: 2026-06-01T00:00:00Z
expires_at: 2026-12-31T23:59:59Z
revocable_by: operator
scope: |
  Allow reading files and listing directories.
constraints:
  allowed_tools:
    - read_file
    - list_directory
revocation_state: active
revoked_at: null
revocation_reason: null
"""


def test_mandate_export_returns_mandate_export_type(mandate_backend) -> None:
    """export() returns a MandateExport instance."""
    from atomic_agents.export.types import MandateExport

    result = mandate_backend.export()
    assert isinstance(result, MandateExport)


def test_mandate_export_empty_returns_empty(mandate_backend) -> None:
    """export() with no mandates.md files returns empty mandates_by_scope."""
    from atomic_agents.export.types import MandateExport

    result = mandate_backend.export()
    assert isinstance(result, MandateExport)
    # No mandates.md files exist — should return empty dict
    assert result.mandates_by_scope == {}


def test_mandate_export_with_mandates(mandate_backend) -> None:
    """export() with a mandates.md returns parsed Mandate objects."""
    from atomic_agents.export.types import MandateExportQuery

    scope_root = mandate_backend._scope_root
    scope = f"project:{scope_root.name}"
    _write_mandates_md(scope_root, scope, _SIMPLE_MANDATES_MD)

    result = mandate_backend.export(MandateExportQuery(scopes=[scope]))
    assert scope in result.mandates_by_scope
    mandates = result.mandates_by_scope[scope]
    assert len(mandates) == 1
    assert mandates[0].mandate_id == "allow-read-files"


def test_mandate_export_backend_id_and_scope_root(mandate_backend) -> None:
    """MandateExport carries backend_id and scope_root."""
    result = mandate_backend.export()
    assert result.backend_id == "filesystem"
    assert result.scope_root != ""


def test_mandate_export_render_roundtrip(mandate_backend, tmp_path) -> None:
    """render_mandates_md output must actually RE-PARSE through the mandate parser.

    spec/40 Tier A round-trip for MandateBackend: the rendered text MUST feed
    back through parse_mandates_md and reproduce the same dispatch-relevant
    fields. A substring check would mask the omitted parser-required ``scope`` /
    ``revocation_state`` fields — this test re-parses and asserts equality
    (spec/40 prep finding P1).
    """
    from atomic_agents.export.renderer import render_mandates_md
    from atomic_agents.export.types import MandateExportQuery
    from atomic_agents.mandates_md import parse_mandates_md

    scope_root = mandate_backend._scope_root
    scope = f"project:{scope_root.name}"
    _write_mandates_md(scope_root, scope, _SIMPLE_MANDATES_MD)

    result = mandate_backend.export(MandateExportQuery(scopes=[scope]))
    mandates = result.mandates_by_scope[scope]
    assert len(mandates) == 1
    original = mandates[0]

    # Render, then write the rendered text to a fresh mandates.md and re-parse
    # it through the REAL parser (not a substring check).
    rendered = render_mandates_md(mandates)
    rendered_path = tmp_path / "rendered_mandates.md"
    rendered_path.write_text(rendered, encoding="utf-8")

    _meta, reparsed = parse_mandates_md(
        rendered_path, scope=scope, is_project_root=True
    )
    assert len(reparsed) == 1, (
        f"Rendered mandates.md must re-parse to exactly one mandate; "
        f"got {len(reparsed)}.\nRendered text:\n{rendered}"
    )
    rt = reparsed[0]

    # Dispatch-relevant fields MUST survive the render → re-parse round-trip.
    assert rt.mandate_id == original.mandate_id
    assert rt.granted_by == original.granted_by
    assert rt.revocation_state == original.revocation_state
    assert rt.prose_scope == original.prose_scope
    assert rt.constraints.allowed_tools == original.constraints.allowed_tools
    assert rt.expires_at == original.expires_at

    # source_path is a backend-local resolution detail and MUST NOT appear in
    # the portable rendered text (deployment-agnostic export, spec/40).
    assert "source_path" not in rendered, (
        "render_mandates_md MUST NOT bake the backend-local source_path into "
        "the portable canonical export (spec/40 portability rule)"
    )


_MANDATES_MD_WITH_META = """\
## _meta
per_agent_mandate_policy: forbidden
allowed_per_agent_ids:
  - allow-read-files

## allow-read-files
granted_by: operator
granted_at: 2026-06-01T00:00:00Z
scope: |
  Allow reading files.
constraints:
  allowed_tools:
    - read_file
revocation_state: active
"""


def test_mandate_export_preserves_project_root_meta(mandate_backend, tmp_path) -> None:
    """Project-root ## _meta policy MUST survive the export round-trip.

    The _meta block (per_agent_mandate_policy + allowed_per_agent_ids) is a
    security boundary. list_mandates() discards it, so export_mandate re-parses
    the project-root scope to recover it onto MandateExport.meta_by_scope, and
    render_mandates_md(mandates, meta) re-emits it. Dropping it would silently
    revert a 'forbidden' policy to the 'open' default on re-import (spec/40
    Round-2 finding).
    """
    from atomic_agents.export.renderer import render_mandates_md
    from atomic_agents.export.types import MandateExportQuery
    from atomic_agents.mandate.types import ProjectMandateMeta
    from atomic_agents.mandates_md import parse_mandates_md

    scope_root = mandate_backend._scope_root
    scope = f"project:{scope_root.name}"
    _write_mandates_md(scope_root, scope, _MANDATES_MD_WITH_META)

    result = mandate_backend.export(MandateExportQuery(scopes=[scope]))

    # The _meta block MUST be captured on the export result.
    assert scope in result.meta_by_scope, (
        "export() must capture the project-root _meta policy block; "
        f"meta_by_scope keys: {list(result.meta_by_scope)}"
    )
    meta = result.meta_by_scope[scope]
    assert isinstance(meta, ProjectMandateMeta)
    assert meta.per_agent_mandate_policy == "forbidden"
    assert meta.allowed_per_agent_ids == frozenset({"allow-read-files"})

    # Render with meta, re-parse, and confirm the policy survives byte→object.
    mandates = result.mandates_by_scope[scope]
    rendered = render_mandates_md(mandates, meta)
    rendered_path = tmp_path / "rendered_with_meta.md"
    rendered_path.write_text(rendered, encoding="utf-8")

    reparsed_meta, reparsed = parse_mandates_md(
        rendered_path, scope=scope, is_project_root=True
    )
    assert reparsed_meta is not None, (
        "render_mandates_md(mandates, meta) must emit a re-parseable ## _meta "
        f"section. Rendered text:\n{rendered}"
    )
    assert reparsed_meta.per_agent_mandate_policy == "forbidden", (
        "A 'forbidden' policy MUST NOT silently revert to 'open' on re-import"
    )
    assert reparsed_meta.allowed_per_agent_ids == frozenset({"allow-read-files"})
    assert len(reparsed) == 1


def test_mandate_export_no_meta_when_absent(mandate_backend) -> None:
    """A mandates.md without a ## _meta section yields no meta_by_scope entry."""
    from atomic_agents.export.types import MandateExportQuery

    scope_root = mandate_backend._scope_root
    scope = f"project:{scope_root.name}"
    _write_mandates_md(scope_root, scope, _SIMPLE_MANDATES_MD)

    result = mandate_backend.export(MandateExportQuery(scopes=[scope]))
    assert scope not in result.meta_by_scope


def test_mandate_export_all_equals_export_none(mandate_backend) -> None:
    """export_all() produces same result as export(None)."""
    result_none = mandate_backend.export(None)
    result_all = mandate_backend.export_all()
    assert result_none.mandates_by_scope == result_all.mandates_by_scope
    assert result_none.meta_by_scope == result_all.meta_by_scope


# Mandate content with a past expires_at so list_mandates() derives EXPIRED state.
_EXPIRED_MANDATE_SECTION = """\
## old-trial-license
granted_by: operator
granted_at: 2020-01-01T00:00:00Z
expires_at: 2020-02-01T00:00:00Z
scope: |
  Trial license for legacy-tool integration.
constraints:
  allowed_tools:
    - legacy_tool
revocation_state: active
"""


def test_mandate_export_expired_state_normalizes_on_render(
    mandate_backend, tmp_path: Path
) -> None:
    """render_mandates_md normalizes RevocationState.EXPIRED → 'active' on emit.

    list_mandates() returns a Mandate with revocation_state=EXPIRED when
    expires_at is in the past (derived state, spec/29). The mandates.md parser
    rejects 'expired' as an authored value, so render_mandates_md must map it
    back to 'active' (the value that was on disk). This tests the Tier A
    shipping path for expired mandates.

    Asserts:
      (a) export → render → re-parse does NOT raise.
      (b) 'revocation_state: expired' does NOT appear in the rendered YAML.
      (c) The re-parsed mandate round-trips the core dispatch-relevant fields
          consistently (mandate_id, granted_by, constraints).
    """
    from atomic_agents.export.renderer import render_mandates_md
    from atomic_agents.export.types import MandateExportQuery
    from atomic_agents.mandate.types import RevocationState
    from atomic_agents.mandates_md import parse_mandates_md

    scope_root = mandate_backend._scope_root
    # Use an agent scope so the expired section doesn't require a _meta block.
    agent_scope = "agent:test-agent"
    scope_path = scope_root / "test-agent"
    scope_path.mkdir(parents=True, exist_ok=True)
    (scope_path / "mandates.md").write_text(_EXPIRED_MANDATE_SECTION, encoding="utf-8")

    result = mandate_backend.export(MandateExportQuery(scopes=[agent_scope]))
    mandates = result.mandates_by_scope.get(agent_scope, [])
    assert len(mandates) == 1, (
        f"Expected 1 mandate for scope {agent_scope!r}; got {len(mandates)}."
    )
    m = mandates[0]

    # Confirm the backend derived EXPIRED state (prerequisite for the test).
    assert m.revocation_state == RevocationState.EXPIRED, (
        f"Expected revocation_state=EXPIRED (derived from past expires_at); "
        f"got {m.revocation_state!r}. The test fixture or backend may have changed."
    )

    # (a) render → re-parse must NOT raise.
    rendered = render_mandates_md(mandates)
    rendered_path = tmp_path / "expired_rendered.md"
    rendered_path.write_text(rendered, encoding="utf-8")

    _meta, reparsed = parse_mandates_md(
        rendered_path, scope=agent_scope, is_project_root=False
    )
    assert len(reparsed) == 1, (
        f"Rendered mandate with EXPIRED state must re-parse to exactly 1 mandate; "
        f"got {len(reparsed)}.\nRendered text:\n{rendered}"
    )

    # (b) 'revocation_state: expired' must NOT appear in the rendered text.
    assert "revocation_state: expired" not in rendered, (
        "render_mandates_md MUST NOT emit 'revocation_state: expired' — the "
        "parser rejects 'expired' as an authored value. The EXPIRED state must "
        "be normalized to 'active' on emit (spec/40 renderer contract)."
    )

    # (c) Core dispatch-relevant fields survive the round-trip.
    rt = reparsed[0]
    assert rt.mandate_id == m.mandate_id, (
        f"mandate_id must survive render→re-parse; got {rt.mandate_id!r}"
    )
    assert rt.granted_by == m.granted_by, (
        f"granted_by must survive render→re-parse; got {rt.granted_by!r}"
    )
    assert rt.constraints.allowed_tools == m.constraints.allowed_tools, (
        f"allowed_tools must survive render→re-parse; got {rt.constraints.allowed_tools!r}"
    )


def test_mandate_export_no_crlf_no_bom(mandate_backend) -> None:
    """Rendered mandate bytes must use LF line endings and must not have a UTF-8 BOM.

    spec/40 MUST 5: UTF-8, LF line endings, NO byte-order mark. render_mandates_md()
    uses yaml.dump + '\\n'.join, but we assert the rendered bytes satisfy MUST 5.
    """
    from atomic_agents.export.renderer import render_mandates_md
    from atomic_agents.export.types import MandateExportQuery

    scope_root = mandate_backend._scope_root
    scope = f"project:{scope_root.name}"
    _write_mandates_md(scope_root, scope, _SIMPLE_MANDATES_MD)

    result = mandate_backend.export(MandateExportQuery(scopes=[scope]))
    for _scope, mandates in result.mandates_by_scope.items():
        rendered = render_mandates_md(mandates)
        raw_bytes = rendered.encode("utf-8")
        assert b"\r\n" not in raw_bytes, (
            "Mandate bytes must not contain CRLF (spec/40 MUST 5: LF line endings)"
        )
        assert not raw_bytes.startswith(b"\xef\xbb\xbf"), (
            "Mandate bytes must not start with UTF-8 BOM (spec/40 MUST 5: no BOM)"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Corpus conformance


@pytest.fixture
def corpus_backend(tmp_path: Path):
    from atomic_agents.corpus.filesystem import FilesystemCorpusBackend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    return FilesystemCorpusBackend(agent_root)


def _make_corpus_write_policy(agent_root: Path):
    from atomic_agents.memory.backend import WritePolicy

    return WritePolicy(write_paths=[agent_root])


def test_corpus_export_returns_corpus_export_type(corpus_backend) -> None:
    """export() returns a CorpusExport instance."""
    from atomic_agents.export.types import CorpusExport

    result = corpus_backend.export()
    assert isinstance(result, CorpusExport)


def test_corpus_export_empty_returns_empty_pages(corpus_backend) -> None:
    """export() on an empty corpus returns empty page lists."""
    result = corpus_backend.export()
    assert result.pages_with_bytes.get("wiki", []) == []
    assert result.pages_with_bytes.get("raw", []) == []


def test_corpus_export_single_page_roundtrip(corpus_backend) -> None:
    """Writing a wiki page and exporting returns byte-exact raw file content."""
    policy = _make_corpus_write_policy(corpus_backend._agent_root)
    body = "This is wiki page content."
    fm = {"name": "Test Page", "description": "A test page"}
    corpus_backend.write_page("test-page", body, "wiki", policy, frontmatter=fm)

    wiki_dir = corpus_backend._corpus_dir("wiki")
    page_path = wiki_dir / "test-page.md"
    assert page_path.exists(), "Page file must be written by write_page()"

    expected_bytes = page_path.read_bytes()

    result = corpus_backend.export()
    wiki_pages = result.pages_with_bytes.get("wiki", [])
    assert len(wiki_pages) == 1
    _page, raw_bytes = wiki_pages[0]

    assert raw_bytes == expected_bytes, (
        "CorpusExport raw_bytes must match on-disk file bytes exactly "
        "(Tier A byte-exact fidelity, spec/40 MUST 4)"
    )


def test_corpus_export_uses_list_pages_not_query(corpus_backend) -> None:
    """export() must enumerate via list_pages(), not query(text).

    MUST 6: export is state extraction, not semantic retrieval.
    Verifies that all pages are returned (not filtered by text match).
    """
    policy = _make_corpus_write_policy(corpus_backend._agent_root)
    for i in range(3):
        body = f"Page {i} content with unique body"
        corpus_backend.write_page(f"page-{i}", body, "wiki", policy)

    result = corpus_backend.export()
    wiki_pages = result.pages_with_bytes.get("wiki", [])
    assert len(wiki_pages) == 3, (
        "export() must return ALL pages (state enumeration via list_pages()), "
        "not a filtered subset (spec/40 MUST 6)"
    )


def test_corpus_export_both_corpora(corpus_backend) -> None:
    """export() with query.corpus=None exports both wiki and raw."""
    policy = _make_corpus_write_policy(corpus_backend._agent_root)
    corpus_backend.write_page("wiki-page", "Wiki content", "wiki", policy)
    corpus_backend.write_page("raw-doc", "Raw content", "raw", policy)

    result = corpus_backend.export()
    assert len(result.pages_with_bytes.get("wiki", [])) == 1
    assert len(result.pages_with_bytes.get("raw", [])) == 1


def test_corpus_export_raw_non_md_file_roundtrip(corpus_backend) -> None:
    """A raw-corpus file with a NON-.md extension must export byte-for-byte.

    list_pages('raw') enumerates non-.md raw files (ref.name == stem), so the
    export MUST resolve the real on-disk file from the ref instead of guessing
    {ref.name}.md — otherwise the page is silently dropped (spec/40 prep finding
    P0: direct data loss breaking the round-trip contract).
    """
    # Write one normal wiki .md page AND place a raw .txt file directly on disk
    # (the framework write path always writes .md; manual placement is the only
    # way a non-.md raw file appears, which is exactly the dropped case).
    raw_dir = corpus_backend._corpus_dir("raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    txt_path = raw_dir / "data.txt"
    txt_bytes = b"plain text raw content, no frontmatter\n"
    txt_path.write_bytes(txt_bytes)

    # Also place a raw .md page so we exercise the mixed case.
    md_path = raw_dir / "notes.md"
    md_bytes = b"# Notes\n\nSome raw markdown.\n"
    md_path.write_bytes(md_bytes)

    result = corpus_backend.export()
    raw_pages = result.pages_with_bytes.get("raw", [])

    # BOTH files must appear — the .txt must NOT be dropped.
    names = {page.ref.name for page, _ in raw_pages}
    assert names == {"data", "notes"}, (
        f"Raw export must include the non-.md file; got {names}. "
        "A missing 'data' means the {name}.md-only resolution silently dropped it."
    )

    bytes_by_name = {page.ref.name: rb for page, rb in raw_pages}
    assert bytes_by_name["data"] == txt_bytes, (
        "Non-.md raw file must export byte-for-byte (Tier A, spec/40 MUST 4)"
    )
    assert bytes_by_name["notes"] == md_bytes


def test_corpus_export_raw_stem_collision_distinct_files(corpus_backend) -> None:
    """Two raw files sharing a stem MUST each export their own bytes.

    list_pages('raw') yields one ref per file with ref.name == stem, so
    data.txt and data.json both produce ref.name == 'data'. The export MUST
    pair each ref to a DISTINCT on-disk file — resolving both to the first
    sorted candidate would drop one file and export the other twice (spec/40
    round-trip Round-2 finding: silent data loss + duplication).
    """
    raw_dir = corpus_backend._corpus_dir("raw")
    raw_dir.mkdir(parents=True, exist_ok=True)

    txt_bytes = b"plain text content for data.txt\n"
    json_bytes = b'{"source": "data.json"}\n'
    (raw_dir / "data.txt").write_bytes(txt_bytes)
    (raw_dir / "data.json").write_bytes(json_bytes)

    result = corpus_backend.export()
    raw_pages = result.pages_with_bytes.get("raw", [])

    # BOTH same-stem files must appear — exactly two pages, no duplication.
    assert len(raw_pages) == 2, (
        f"Expected 2 raw pages for the data.txt + data.json stem collision; "
        f"got {len(raw_pages)} (one dropped or one duplicated)."
    )

    exported_bytes = sorted(rb for _page, rb in raw_pages)
    assert exported_bytes == sorted([txt_bytes, json_bytes]), (
        "Each same-stem raw file must export its OWN byte content exactly once. "
        f"Got: {exported_bytes!r}"
    )


def test_corpus_export_corpus_filter(corpus_backend) -> None:
    """export() with corpus filter returns only the specified corpus."""
    from atomic_agents.export.types import CorpusExportQuery

    policy = _make_corpus_write_policy(corpus_backend._agent_root)
    corpus_backend.write_page("wiki-page", "Wiki content", "wiki", policy)
    corpus_backend.write_page("raw-doc", "Raw content", "raw", policy)

    result = corpus_backend.export(CorpusExportQuery(corpus="wiki"))
    assert len(result.pages_with_bytes.get("wiki", [])) == 1
    assert result.pages_with_bytes.get("raw", []) == []


def test_corpus_export_wiki_plus_raw_associativity(corpus_backend) -> None:
    """Exporting wiki+raw together == exporting wiki then raw separately.

    spec/40 §"CorpusBackend export scope": associativity invariant.
    """
    from atomic_agents.export.types import CorpusExportQuery

    policy = _make_corpus_write_policy(corpus_backend._agent_root)
    corpus_backend.write_page("wiki-page", "Wiki content", "wiki", policy)
    corpus_backend.write_page("raw-doc", "Raw content", "raw", policy)

    combined = corpus_backend.export()
    wiki_only = corpus_backend.export(CorpusExportQuery(corpus="wiki"))
    raw_only = corpus_backend.export(CorpusExportQuery(corpus="raw"))

    combined_wiki = combined.pages_with_bytes.get("wiki", [])
    combined_raw = combined.pages_with_bytes.get("raw", [])
    separate_wiki = wiki_only.pages_with_bytes.get("wiki", [])
    separate_raw = raw_only.pages_with_bytes.get("raw", [])

    assert len(combined_wiki) == len(separate_wiki)
    assert len(combined_raw) == len(separate_raw)


def test_corpus_export_no_crlf_no_bom(corpus_backend) -> None:
    """Exported corpus bytes must use LF line endings and must not have a UTF-8 BOM.

    spec/40 MUST 5: UTF-8, LF line endings, NO byte-order mark.
    """
    policy = _make_corpus_write_policy(corpus_backend._agent_root)
    corpus_backend.write_page("no-crlf", "Test content", "wiki", policy)

    result = corpus_backend.export()
    for corpus_name, pages in result.pages_with_bytes.items():
        for _page, raw_bytes in pages:
            assert b"\r\n" not in raw_bytes, (
                f"Corpus page bytes ({corpus_name}) must not contain CRLF "
                "(spec/40 MUST 5: LF line endings)"
            )
            assert not raw_bytes.startswith(b"\xef\xbb\xbf"), (
                f"Corpus page bytes ({corpus_name}) must not start with UTF-8 BOM "
                "(spec/40 MUST 5: no BOM)"
            )


def test_corpus_export_backend_id_and_scope(corpus_backend) -> None:
    """CorpusExport carries backend_id and scope."""
    result = corpus_backend.export()
    assert result.backend_id == "filesystem"
    assert result.scope != ""


# ──────────────────────────────────────────────────────────────────────────────
# Lock conformance


@pytest.fixture
def lock_backend(tmp_path: Path):
    from atomic_agents.locks.filesystem import FilesystemLockBackend

    return FilesystemLockBackend(tmp_path / "agent")


def test_lock_export_returns_lock_export_type(lock_backend) -> None:
    """export() returns a LockExport instance."""
    from atomic_agents.export.types import LockExport

    result = lock_backend.export()
    assert isinstance(result, LockExport)


def test_lock_export_always_returns_zero_records(lock_backend) -> None:
    """LockExport MUST always return zero lock records.

    Per spec/40 §"LockBackend export contract": ephemeral runtime lock
    state MUST NOT be included. The True declaration affirms Protocol
    composition; it does NOT imply there is live state to migrate.
    """
    result = lock_backend.export()
    assert result.lock_file_names == [], (
        "LockExport.lock_file_names must always be [] — runtime lease files "
        "are ephemeral and MUST NOT be exported (spec/40 §'LockBackend export contract')"
    )


def test_lock_export_carries_scope_root(lock_backend) -> None:
    """LockExport carries scope_root for location-map portability."""
    result = lock_backend.export()
    assert result.scope_root != ""
    assert result.backend_id == "filesystem"


def test_lock_export_not_affected_by_held_lock(lock_backend) -> None:
    """export() result must not include runtime lock state even when a lock is held."""
    # Acquire a lock
    handle = lock_backend.acquire(name="test", timeout=0.0)
    try:
        result = lock_backend.export()
        assert result.lock_file_names == [], (
            "export() must not include runtime lock state even when a lock is held"
        )
    finally:
        lock_backend.release(handle)


def test_lock_export_all_equals_export_none(lock_backend) -> None:
    """export_all() == export(None) for LockBackend."""
    r1 = lock_backend.export(None)
    r2 = lock_backend.export_all()
    assert r1.scope_root == r2.scope_root
    assert r1.backend_id == r2.backend_id
    assert r1.lock_file_names == r2.lock_file_names


# ──────────────────────────────────────────────────────────────────────────────
# Secret never-leak invariant (MUST 9)
# This test is NOT capability-gated — the never-leak invariant holds even
# when supports_canonical_export=False. It is an absolute invariant.


@pytest.fixture
def secret_backend():
    from atomic_agents.secret_backend.filesystem import FilesystemSecretBackend

    return FilesystemSecretBackend()


def test_secret_export_returns_secret_export_type(secret_backend) -> None:
    """export() returns a SecretExport instance."""
    from atomic_agents.export.types import SecretExport

    result = secret_backend.export()
    assert isinstance(result, SecretExport)


def test_secret_export_no_plaintext_in_entries(secret_backend, monkeypatch) -> None:
    """MUST 9: SecretExport entries MUST NOT contain resolved plaintext values.

    This test FAILS LOUDLY if any resolved credential value appears in the
    export output. Configures a known secret value via environment variable
    and verifies it does NOT appear in any export entry.

    spec/40 MUST 9 — absolute invariant, never gated on capability flag.
    """
    from atomic_agents.export.renderer import render_secret_export_bytes

    # Set a known test value in the environment
    known_value = "sk-test-PLAINTEXT-MUST-NOT-APPEAR-12345"
    monkeypatch.setenv("ANTHROPIC_API_KEY", known_value)

    result = secret_backend.export()
    rendered = render_secret_export_bytes(result.entries)
    rendered_str = rendered.decode("utf-8")

    assert known_value not in rendered_str, (
        "NEVER-LEAK INVARIANT VIOLATED: plaintext credential value appeared "
        "in SecretExport output. spec/40 MUST 9: export() MUST NEVER emit "
        "resolved credential values."
    )


def test_secret_export_no_env_var_names_in_hints(secret_backend) -> None:
    """SecretExportRef hints must not contain env-var names or path separators.

    spec/40 ruling: LOGICAL-SECRET + BINDING-HINT abstraction. Hints must
    be deployment-agnostic (not 'env:ANTHROPIC_API_KEY').
    """
    result = secret_backend.export()
    for entry in result.entries:
        hint = entry.hint
        # Must not contain deployment-specific source prefixes
        assert "env:" not in hint, (
            f"SecretExportRef.hint must not contain 'env:' — must be "
            f"deployment-agnostic (got: {hint!r})"
        )
        assert "keychain:" not in hint, (
            f"SecretExportRef.hint must not contain 'keychain:' (got: {hint!r})"
        )
        # Must not contain ANY path separators — hints must be plain
        # deployment-agnostic descriptions, not resource paths.
        assert "/" not in hint, (
            f"SecretExportRef.hint must not contain '/' (got: {hint!r})"
        )


def test_secret_export_entries_have_logical_keys(secret_backend) -> None:
    """Each SecretExportRef must have a non-empty logical_key and hint."""
    result = secret_backend.export()
    for entry in result.entries:
        assert entry.logical_key, "SecretExportRef.logical_key must not be empty"
        assert entry.hint, "SecretExportRef.hint must not be empty"
        assert isinstance(entry.present, bool), "SecretExportRef.present must be bool"


def test_secret_export_renders_to_bytes(secret_backend) -> None:
    """render_secret_export_bytes() produces valid UTF-8 JSON bytes."""
    from atomic_agents.export.renderer import render_secret_export_bytes

    result = secret_backend.export()
    rendered = render_secret_export_bytes(result.entries)
    assert isinstance(rendered, bytes)
    # Must be valid JSON
    parsed = json.loads(rendered.decode("utf-8"))
    assert isinstance(parsed, list)


def test_secret_export_all_equals_export_none(secret_backend) -> None:
    """export_all() == export(None) for SecretBackend."""
    r1 = secret_backend.export(None)
    r2 = secret_backend.export_all()
    assert len(r1.entries) == len(r2.entries)
    assert r1.backend_id == r2.backend_id


# ──────────────────────────────────────────────────────────────────────────────
# Renderer module tests


def test_render_run_record_bytes_ts_first() -> None:
    """render_run_record_bytes produces ts-first JSON, NOT sorted keys."""
    from atomic_agents.export.renderer import render_run_record_bytes

    record = _make_run_record(summary="renderer test")
    raw = render_run_record_bytes(record)
    line = raw.decode("utf-8").strip()
    assert line.startswith('{"ts"'), (
        "render_run_record_bytes must produce ts-first JSON (MUST 8)"
    )
    assert raw.endswith(b"\n"), "render_run_record_bytes must end with newline"


def test_render_run_record_bytes_not_canonical_json() -> None:
    """render_run_record_bytes must NOT use canonical_json (sort_keys=True)."""
    from atomic_agents.export.renderer import render_run_record_bytes
    from atomic_agents._canonical import canonical_json

    record = _make_run_record(summary="not canonical")
    rendered = render_run_record_bytes(record).decode("utf-8").strip()

    # canonical_json would sort keys alphabetically (cache_hit_tokens first)
    canonical = canonical_json(record.to_dict())

    # They must differ because canonical_json sorts keys
    assert rendered != canonical, (
        "render_run_record_bytes must NOT use canonical_json "
        "(would break Tier A byte-match, spec/40 MUST 8)"
    )


def test_render_note_bytes_from_raw_passthrough() -> None:
    """render_note_bytes_from_raw must pass bytes through unchanged."""
    from atomic_agents.export.renderer import render_note_bytes_from_raw

    test_bytes = b"---\nname: test\n---\nBody content\n"
    result = render_note_bytes_from_raw(test_bytes)
    assert result == test_bytes, "render_note_bytes_from_raw must be a passthrough"


def test_render_corpus_page_bytes_from_raw_passthrough() -> None:
    """render_corpus_page_bytes_from_raw must pass bytes through unchanged."""
    from atomic_agents.export.renderer import render_corpus_page_bytes_from_raw

    test_bytes = b"---\nname: wiki page\n---\nContent here\n"
    result = render_corpus_page_bytes_from_raw(test_bytes)
    assert result == test_bytes


# ──────────────────────────────────────────────────────────────────────────────
# assert_canonical_roundtrip wired end-to-end
#
# The helper is defined above and used for self-certification. These tests call
# it directly (rather than through the per-backend write/export calls) to
# ensure the helper itself is exercised and third-party self-certification
# works end-to-end. spec/40 §"Conformance test architecture".


def test_assert_canonical_roundtrip_memory_endtoend(memory_backend) -> None:
    """assert_canonical_roundtrip drives the full Memory round-trip via the helper.

    This test proves the shared helper actually runs the full pipeline:
    write → export() → render → compare-to-on-disk-bytes.
    """
    from atomic_agents.memory.backend import WritePolicy

    def write_fn(b):
        policy = WritePolicy(write_paths=[b._agent_root])
        b.write_note(
            _make_capture(name="roundtrip_via_helper", body="Helper round-trip body."),
            policy,
        )

    def expected_bytes_fn(b):
        refs = b.list_notes()
        if not refs:
            return b"SKIP_COMPARISON"
        return (b._memory_dir / refs[0].name).read_bytes()

    assert_canonical_roundtrip(memory_backend, write_fn, expected_bytes_fn)


def test_assert_canonical_roundtrip_log_endtoend(log_backend) -> None:
    """assert_canonical_roundtrip drives the full Log round-trip via the helper."""

    def write_fn(b):
        b.append(_make_run_record(summary="helper round-trip log record"))

    def expected_bytes_fn(b):
        log_dir = b._log_dir
        jsonl_files = list(log_dir.rglob("*.jsonl"))
        if not jsonl_files:
            return b"SKIP_COMPARISON"
        return jsonl_files[0].read_bytes()

    assert_canonical_roundtrip(log_backend, write_fn, expected_bytes_fn)


# ──────────────────────────────────────────────────────────────────────────────
# Mandate renderer round-trip — byte-fidelity tests for each constraint shape.
#
# Each test: build a Mandate programmatically → render_mandates_md → write to
# tmp file → parse_mandates_md → compare dispatch-relevant fields field-by-field.
# This is the same pattern as test_mandate_export_render_roundtrip but covers
# the specific constraint branches that had mismatch risk (time_window key names,
# allowed_targets/blocked_targets dict shape, action_class, unconstrained, revoked).


def _make_minimal_mandate(
    mandate_id: str,
    constraints,
    *,
    revocation_state=None,
    revoked_at=None,
    revoked_by=None,
    revocation_reason=None,
    expires_at=None,
):
    """Build a Mandate dataclass from components.  Fills in sensible defaults."""
    from datetime import datetime, timezone

    from atomic_agents.mandate.types import Mandate, RevocationState

    if revocation_state is None:
        revocation_state = RevocationState.ACTIVE

    return Mandate(
        mandate_id=mandate_id,
        scope="project:test-project",
        granted_by="operator",
        granted_at=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
        expires_at=expires_at,
        revocation_state=revocation_state,
        revoked_at=revoked_at,
        revoked_by=revoked_by,
        revocation_reason=revocation_reason,
        constraints=constraints,
        source_hash="sha256:aabbcc",
        source_path=None,
        prose_scope="Allow the agent to perform a specific task.",
    )


def _render_and_reparse(mandates, tmp_path, scope="project:test-project"):
    """Render mandates to text, write to tmp file, re-parse through parser."""
    from atomic_agents.export.renderer import render_mandates_md
    from atomic_agents.mandates_md import parse_mandates_md

    rendered = render_mandates_md(mandates)
    rendered_path = tmp_path / "mandates.md"
    rendered_path.write_text(rendered, encoding="utf-8")
    _meta, reparsed = parse_mandates_md(
        rendered_path, scope=scope, is_project_root=True
    )
    return rendered, reparsed


def test_mandate_roundtrip_time_window(tmp_path) -> None:
    """A mandate with constraints.time_window must round-trip through render → re-parse.

    Verifies the time_window key names emitted by render_mandates_md are the
    keys the parser expects (spec/40 constraint-shape fidelity).
    """
    from datetime import time as dt_time

    from atomic_agents.mandate.types import MandateConstraints, TimeWindow

    tw = TimeWindow(start_utc=dt_time(9, 0), end_utc=dt_time(17, 0))
    c = MandateConstraints(time_window=tw)
    mandate = _make_minimal_mandate("allow-with-window", c)

    rendered, reparsed = _render_and_reparse([mandate], tmp_path)
    assert len(reparsed) == 1, (
        f"time_window mandate must re-parse to exactly one mandate.\n"
        f"Rendered:\n{rendered}"
    )
    rt = reparsed[0]
    assert rt.mandate_id == "allow-with-window"
    assert rt.constraints.time_window is not None, (
        "time_window MUST survive the render → re-parse round-trip"
    )
    assert rt.constraints.time_window.start_utc == tw.start_utc, (
        f"start_utc mismatch after round-trip: "
        f"expected {tw.start_utc!r}, got {rt.constraints.time_window.start_utc!r}"
    )
    assert rt.constraints.time_window.end_utc == tw.end_utc, (
        f"end_utc mismatch after round-trip: "
        f"expected {tw.end_utc!r}, got {rt.constraints.time_window.end_utc!r}"
    )


def test_mandate_roundtrip_time_window_seconds_preserved(tmp_path) -> None:
    """time_window seconds MUST survive the render → re-parse round-trip.

    TimeWindow.start_utc / end_utc are datetime.time objects that carry seconds.
    render_mandates_md MUST emit %H:%M:%S (not %H:%M) so a mandate with
    start=09:00:30 does not silently round-trip to 09:00:00 — that would shift
    a security time-window constraint by up to 59 s (spec/40 F2 bug fix).
    """
    from datetime import time as dt_time

    from atomic_agents.mandate.types import MandateConstraints, TimeWindow

    tw = TimeWindow(start_utc=dt_time(9, 0, 30), end_utc=dt_time(17, 0, 45))
    c = MandateConstraints(time_window=tw)
    mandate = _make_minimal_mandate("allow-with-window-seconds", c)

    rendered, reparsed = _render_and_reparse([mandate], tmp_path)
    assert len(reparsed) == 1, (
        f"time_window-with-seconds mandate must re-parse to exactly one mandate.\n"
        f"Rendered:\n{rendered}"
    )
    rt = reparsed[0]
    assert rt.constraints.time_window is not None, (
        "time_window MUST survive the render → re-parse round-trip"
    )
    assert rt.constraints.time_window.start_utc == tw.start_utc, (
        f"start_utc SECONDS must be preserved after round-trip: "
        f"expected {tw.start_utc!r} (09:00:30), got {rt.constraints.time_window.start_utc!r}. "
        f"render_mandates_md must emit %H:%M:%S not %H:%M (spec/40 F2)."
    )
    assert rt.constraints.time_window.end_utc == tw.end_utc, (
        f"end_utc SECONDS must be preserved after round-trip: "
        f"expected {tw.end_utc!r} (17:00:45), got {rt.constraints.time_window.end_utc!r}. "
        f"render_mandates_md must emit %H:%M:%S not %H:%M (spec/40 F2)."
    )


def test_mandate_roundtrip_budget_constraints(tmp_path) -> None:
    """A mandate with daily/monthly/cumulative_token_usd must round-trip.

    All three USD budget fields must be preserved through render → re-parse.
    """
    from atomic_agents.mandate.types import MandateConstraints

    c = MandateConstraints(
        daily_token_usd=0.50,
        monthly_token_usd=10.0,
        cumulative_token_usd=100.0,
    )
    mandate = _make_minimal_mandate("allow-with-budgets", c)

    rendered, reparsed = _render_and_reparse([mandate], tmp_path)
    assert len(reparsed) == 1, (
        f"Budget-constraint mandate must re-parse to exactly one mandate.\n"
        f"Rendered:\n{rendered}"
    )
    rt = reparsed[0]
    assert rt.mandate_id == "allow-with-budgets"
    assert rt.constraints.daily_token_usd == pytest.approx(0.50), (
        "daily_token_usd must survive the round-trip"
    )
    assert rt.constraints.monthly_token_usd == pytest.approx(10.0), (
        "monthly_token_usd must survive the round-trip"
    )
    assert rt.constraints.cumulative_token_usd == pytest.approx(100.0), (
        "cumulative_token_usd must survive the round-trip"
    )


def test_mandate_roundtrip_allowed_and_blocked_targets(tmp_path) -> None:
    """A mandate with allowed_targets AND blocked_targets must round-trip.

    Verifies the dict shape emitted by render_mandates_md for target patterns
    is accepted by the parser (spec/40 constraint-shape fidelity).
    """
    from atomic_agents.mandate.types import MandateConstraints, TargetPattern

    c = MandateConstraints(
        allowed_targets=(
            TargetPattern(pattern="api.example.com", kind="exact"),
            TargetPattern(pattern="*.internal.", kind="prefix"),
        ),
        blocked_targets=(TargetPattern(pattern="evil.example.com", kind="exact"),),
    )
    mandate = _make_minimal_mandate("allow-with-targets", c)

    rendered, reparsed = _render_and_reparse([mandate], tmp_path)
    assert len(reparsed) == 1, (
        f"Target-constraint mandate must re-parse to exactly one mandate.\n"
        f"Rendered:\n{rendered}"
    )
    rt = reparsed[0]
    assert rt.mandate_id == "allow-with-targets"

    # Exact set equality (stronger than membership, and avoids the
    # py/incomplete-url-substring-sanitization CodeQL heuristic that misreads
    # `"host" in <set>` as URL-substring sanitization — this is set membership).
    allowed_patterns = {tp.pattern for tp in rt.constraints.allowed_targets}
    assert allowed_patterns == {"api.example.com", "*.internal."}, (
        "allowed_targets patterns must survive the round-trip exactly"
    )

    blocked_patterns = {tp.pattern for tp in rt.constraints.blocked_targets}
    assert blocked_patterns == {"evil.example.com"}, (
        "blocked_targets patterns must survive the round-trip exactly"
    )


def test_mandate_roundtrip_unconstrained(tmp_path) -> None:
    """A mandate with unconstrained=True + justification must round-trip.

    The unconstrained escape-hatch block must survive render → re-parse.
    """
    from atomic_agents.mandate.types import MandateConstraints

    c = MandateConstraints(
        unconstrained=True,
        unconstrained_justification="Trust-the-prose; manually reviewed on each run.",
    )
    mandate = _make_minimal_mandate("allow-unconstrained", c)

    rendered, reparsed = _render_and_reparse([mandate], tmp_path)
    assert len(reparsed) == 1, (
        f"Unconstrained mandate must re-parse to exactly one mandate.\n"
        f"Rendered:\n{rendered}"
    )
    rt = reparsed[0]
    assert rt.mandate_id == "allow-unconstrained"
    assert rt.constraints.unconstrained is True, (
        "constraints.unconstrained must survive the round-trip"
    )
    assert rt.constraints.unconstrained_justification is not None, (
        "constraints.unconstrained_justification must survive the round-trip"
    )
    assert "manually reviewed" in rt.constraints.unconstrained_justification


def test_mandate_roundtrip_action_class(tmp_path) -> None:
    """A mandate with a non-default action_class must round-trip correctly.

    render_mandates_md emits action_class when it is NOT the default
    (external_side_effect). The parser must read it back, otherwise the
    round-tripped mandate silently reverts to the default action class
    (spec/40 constraint-shape fidelity — REAL BUG if this fails).
    """
    from atomic_agents.mandate.types import ActionClass, MandateConstraints

    c = MandateConstraints(
        allowed_tools=frozenset(["read_file"]),
        action_class=ActionClass.READ_ONLY,
    )
    mandate = _make_minimal_mandate("allow-read-only-class", c)

    rendered, reparsed = _render_and_reparse([mandate], tmp_path)
    assert len(reparsed) == 1, (
        f"action_class mandate must re-parse to exactly one mandate.\n"
        f"Rendered:\n{rendered}"
    )
    rt = reparsed[0]
    assert rt.mandate_id == "allow-read-only-class"
    assert rt.constraints.action_class == ActionClass.READ_ONLY, (
        f"action_class MUST survive the render → re-parse round-trip. "
        f"Expected READ_ONLY, got {rt.constraints.action_class!r}. "
        f"This is a real round-trip bug: render_mandates_md emits 'action_class' "
        f"but _parse_constraints never reads it back."
    )


def test_mandate_roundtrip_revoked(tmp_path) -> None:
    """A revoked mandate must round-trip with all revocation fields intact.

    revocation_state=revoked + revoked_at + revoked_by + revocation_reason
    must all survive render → re-parse.
    """
    from datetime import datetime, timezone

    from atomic_agents.mandate.types import MandateConstraints, RevocationState

    c = MandateConstraints(allowed_tools=frozenset(["legacy_tool"]))
    mandate = _make_minimal_mandate(
        "allow-legacy-revoked",
        c,
        revocation_state=RevocationState.REVOKED,
        revoked_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc),
        revoked_by="admin",
        revocation_reason="Tool deprecated; replaced by new-tool.",
    )

    rendered, reparsed = _render_and_reparse([mandate], tmp_path)
    assert len(reparsed) == 1, (
        f"Revoked mandate must re-parse to exactly one mandate.\nRendered:\n{rendered}"
    )
    rt = reparsed[0]
    assert rt.mandate_id == "allow-legacy-revoked"
    assert rt.revocation_state == RevocationState.REVOKED, (
        "revocation_state=revoked must survive the round-trip"
    )
    assert rt.revoked_at is not None, "revoked_at must survive the round-trip"
    assert (
        rt.revoked_at.year == 2026
        and rt.revoked_at.month == 6
        and rt.revoked_at.day == 5
    )
    assert rt.revoked_by == "admin", "revoked_by must survive the round-trip"
    assert rt.revocation_reason is not None and "deprecated" in rt.revocation_reason


# ──────────────────────────────────────────────────────────────────────────────
# Mandate scope discovery — per-agent mandate scope


def test_mandate_export_discovers_agent_scoped_mandates(tmp_path) -> None:
    """Per-agent mandates.md files are discovered and exported.

    Creates an agent sub-directory with its own mandates.md, calls export()
    with query=None, and asserts the agent-scoped mandates are discovered and
    included in the export result.  Exercises the ``agent:{name}`` branch in
    _discover_mandate_scopes (filesystem.py).
    """
    from atomic_agents.export.types import MandateExport
    from atomic_agents.mandate.filesystem import FilesystemMandateBackend

    scope_root = tmp_path / "project"
    scope_root.mkdir()

    # Create a per-agent mandates.md (NOT project-root).
    agent_dir = scope_root / "my-agent"
    agent_dir.mkdir()
    agent_mandates_content = """\
## allow-agent-read
granted_by: operator
granted_at: 2026-06-01T00:00:00Z
scope: |
  Allow the agent to read files.
constraints:
  allowed_tools:
    - read_file
revocation_state: active
"""
    (agent_dir / "mandates.md").write_text(agent_mandates_content, encoding="utf-8")

    backend = FilesystemMandateBackend(scope_root)
    result = backend.export()

    assert isinstance(result, MandateExport)
    agent_scope = "agent:my-agent"
    assert agent_scope in result.mandates_by_scope, (
        f"Per-agent scope {agent_scope!r} must be discovered by export(None). "
        f"Found scopes: {list(result.mandates_by_scope.keys())}"
    )
    mandates = result.mandates_by_scope[agent_scope]
    assert len(mandates) == 1
    assert mandates[0].mandate_id == "allow-agent-read"


def test_mandate_export_discovers_both_project_and_agent_scopes(tmp_path) -> None:
    """Both project-root and per-agent scopes are discovered in a single export().

    When the scope_root contains a mandates.md at root AND sub-agent mandates.md
    files, export(None) must return ALL of them.
    """
    from atomic_agents.mandate.filesystem import FilesystemMandateBackend

    scope_root = tmp_path / "project"
    scope_root.mkdir()

    # Project-root mandates.md
    (scope_root / "mandates.md").write_text(
        """\
## allow-project-wide
granted_by: operator
granted_at: 2026-06-01T00:00:00Z
scope: |
  Project-wide read access.
constraints:
  allowed_tools:
    - list_directory
revocation_state: active
""",
        encoding="utf-8",
    )

    # Per-agent mandates.md
    agent_dir = scope_root / "sub-agent"
    agent_dir.mkdir()
    (agent_dir / "mandates.md").write_text(
        """\
## allow-sub-agent-write
granted_by: operator
granted_at: 2026-06-01T00:00:00Z
scope: |
  Sub-agent write access.
constraints:
  allowed_tools:
    - write_file
revocation_state: active
""",
        encoding="utf-8",
    )

    backend = FilesystemMandateBackend(scope_root)
    result = backend.export()

    project_scope = f"project:{scope_root.name}"
    agent_scope = "agent:sub-agent"

    assert project_scope in result.mandates_by_scope, (
        f"project scope {project_scope!r} must be in mandates_by_scope"
    )
    assert agent_scope in result.mandates_by_scope, (
        f"agent scope {agent_scope!r} must be in mandates_by_scope"
    )
    assert len(result.mandates_by_scope[project_scope]) == 1
    assert len(result.mandates_by_scope[agent_scope]) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Secret bounded query — SecretExportQuery(logical_keys=[subset])


def test_secret_export_bounded_query_only_returns_requested_keys(
    secret_backend,
) -> None:
    """export_secret with SecretExportQuery(logical_keys=subset) returns only those keys.

    Exercises the bounded-query branch in export_secret (filesystem.py).
    Also verifies the never-leak invariant (no plaintext values) for the
    bounded result.
    """
    from atomic_agents.export.types import SecretExportQuery

    # Request only 'anthropic' — not 'openai' or 'moonshot'
    query = SecretExportQuery(logical_keys=["anthropic"])
    result = secret_backend.export(query)

    assert len(result.entries) == 1, (
        f"Bounded query with logical_keys=['anthropic'] must return exactly 1 entry; "
        f"got {len(result.entries)}: {[e.logical_key for e in result.entries]}"
    )
    assert result.entries[0].logical_key == "anthropic"


def test_secret_export_bounded_query_never_leaks_values(
    secret_backend, monkeypatch
) -> None:
    """Bounded query export must never include plaintext values.

    MUST 9 applies to bounded queries too — the never-leak invariant is
    absolute, not scoped to full exports.
    """
    from atomic_agents.export.renderer import render_secret_export_bytes
    from atomic_agents.export.types import SecretExportQuery

    known_value = "sk-bounded-PLAINTEXT-MUST-NOT-APPEAR-99999"
    monkeypatch.setenv("ANTHROPIC_API_KEY", known_value)

    query = SecretExportQuery(logical_keys=["anthropic"])
    result = secret_backend.export(query)
    rendered = render_secret_export_bytes(result.entries)
    rendered_str = rendered.decode("utf-8")

    assert known_value not in rendered_str, (
        "NEVER-LEAK INVARIANT VIOLATED: bounded query export leaked a plaintext "
        "credential value. spec/40 MUST 9 applies to bounded exports too."
    )


def test_secret_export_bounded_query_empty_list_returns_no_entries(
    secret_backend,
) -> None:
    """SecretExportQuery(logical_keys=[]) returns an empty entries list.

    An empty explicit key list means 'export nothing'; it must NOT fall back
    to exporting all known provider keys.
    """
    from atomic_agents.export.types import SecretExportQuery

    query = SecretExportQuery(logical_keys=[])
    result = secret_backend.export(query)
    assert result.entries == [], (
        "SecretExportQuery(logical_keys=[]) must produce zero entries, "
        f"not fall back to all keys; got {len(result.entries)} entries"
    )


def test_secret_export_bounded_query_multi_key(secret_backend) -> None:
    """export_secret with two keys in logical_keys returns exactly those two."""
    from atomic_agents.export.types import SecretExportQuery

    query = SecretExportQuery(logical_keys=["anthropic", "openai"])
    result = secret_backend.export(query)

    assert len(result.entries) == 2, (
        f"Bounded query with 2 keys must return 2 entries; got {len(result.entries)}"
    )
    returned_keys = {e.logical_key for e in result.entries}
    assert returned_keys == {"anthropic", "openai"}


# ──────────────────────────────────────────────────────────────────────────────
# assert_canonical_roundtrip end-to-end — mandate / corpus / lock


def test_assert_canonical_roundtrip_mandate_endtoend(mandate_backend, tmp_path) -> None:
    """assert_canonical_roundtrip drives the full Mandate round-trip via the helper.

    Mirrors the existing memory/log end-to-end tests.  Expected bytes use
    b'SKIP_COMPARISON' (mandate export produces Mandate objects, not raw-file
    bytes, so the byte comparison is through render_mandates_md; the helper
    exercises the export→render path structurally).
    """
    from atomic_agents.export.renderer import render_mandates_md
    from atomic_agents.export.types import MandateExportQuery

    scope_root = mandate_backend._scope_root
    scope = f"project:{scope_root.name}"

    def write_fn(b):
        _write_mandates_md(b._scope_root, scope, _SIMPLE_MANDATES_MD)

    def expected_bytes_fn(b):
        # The mandate export path is object-graph, not raw-file bytes. Use
        # SKIP_COMPARISON here and verify structural correctness in the return
        # value check below — the goal is to exercise the full helper pipeline.
        return b"SKIP_COMPARISON"

    result = assert_canonical_roundtrip(
        mandate_backend,
        write_fn,
        expected_bytes_fn,
        query=MandateExportQuery(scopes=[scope]),
    )

    # Post-helper structural check: rendered bytes must re-parse to the same mandate.
    from atomic_agents.mandates_md import parse_mandates_md

    for _scope, mandates in result.mandates_by_scope.items():
        if not mandates:
            continue
        rendered = render_mandates_md(mandates)
        rendered_path = tmp_path / "rt_helper.md"
        rendered_path.write_text(rendered, encoding="utf-8")
        _meta, reparsed = parse_mandates_md(
            rendered_path, scope=_scope, is_project_root=True
        )
        assert len(reparsed) == len(mandates), (
            "assert_canonical_roundtrip: rendered mandate count must match original"
        )


def test_assert_canonical_roundtrip_corpus_endtoend(corpus_backend) -> None:
    """assert_canonical_roundtrip drives the full Corpus round-trip via the helper.

    Mirrors the existing memory/log end-to-end tests.
    """
    from atomic_agents.memory.backend import WritePolicy

    def write_fn(b):
        policy = WritePolicy(write_paths=[b._agent_root])
        b.write_page(
            "helper-roundtrip-page",
            "Corpus round-trip body via helper.",
            "wiki",
            policy,
        )

    def expected_bytes_fn(b):
        wiki_dir = b._corpus_dir("wiki")
        page_path = wiki_dir / "helper-roundtrip-page.md"
        if not page_path.exists():
            return b"SKIP_COMPARISON"
        return page_path.read_bytes()

    assert_canonical_roundtrip(corpus_backend, write_fn, expected_bytes_fn)


def test_assert_canonical_roundtrip_lock_endtoend(lock_backend) -> None:
    """assert_canonical_roundtrip drives the full Lock round-trip via the helper.

    LockExport has no live state to compare; the helper exercises the
    export() call and capability-gate path. Expected bytes is b'' (always).
    """

    def write_fn(b):
        # Lock export has no writable state — nothing to populate.
        pass

    def expected_bytes_fn(b):
        # _render_export_result returns b"" for LockExport.
        return b""

    assert_canonical_roundtrip(lock_backend, write_fn, expected_bytes_fn)


# ──────────────────────────────────────────────────────────────────────────────
# Goal conformance (spec/41 — registered in the shared #379 export harness)


@pytest.fixture
def goal_backend(tmp_path: Path):
    from atomic_agents.goal.filesystem import FilesystemGoalBackend

    return FilesystemGoalBackend(tmp_path / "agent")


def _write_goal_md(agent_root: Path, *, intent: str = "Harness goal") -> None:
    agent_root.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        "schema_version: 1\n"
        "active: true\n"
        f"intent: {intent}\n"
        "priority: high\n"
        "created: 2026-06-11\n"
        "last_progress_check: 2026-06-11\n"
        "success_criteria:\n"
        "  - done\n"
        "sub_goals: []\n"
        "---\n"
        "\nGoal body prose.\n"
    )
    (agent_root / "goal.md").write_text(content, encoding="utf-8")


def test_goal_export_returns_goal_export_type(goal_backend) -> None:
    """export() returns a GoalExport instance."""
    from atomic_agents.export.types import GoalExport

    result = goal_backend.export()
    assert isinstance(result, GoalExport)


def test_goal_export_is_exportable_result(goal_backend) -> None:
    """GoalExport is an ExportableResult subclass (generic Protocol narrowing).

    This is the assertion the shared harness exists to enforce uniformly across
    backends — it catches a GoalExport that forgot to subclass ExportableResult,
    which @runtime_checkable Exportable would NOT catch (method-presence only).
    """
    from atomic_agents.export.types import ExportableResult

    result = goal_backend.export()
    assert isinstance(result, ExportableResult)


def test_goal_export_empty_returns_empty_components(goal_backend) -> None:
    """export() with no goal.md returns empty components, not an error."""
    from atomic_agents.export.types import GoalExport

    result = goal_backend.export()
    assert isinstance(result, GoalExport)
    assert result.goal_md_bytes == b""
    assert result.history_records_with_bytes == []
    assert result.archived_goals_with_bytes == []


def test_goal_export_single_roundtrip(goal_backend) -> None:
    """Exported goal.md bytes match the on-disk file bytes exactly (Tier A)."""

    def write_fn(b):
        _write_goal_md(b._agent_root, intent="Roundtrip goal")

    def expected_bytes_fn(b):
        return (b._agent_root / "goal.md").read_bytes()

    assert_canonical_roundtrip(goal_backend, write_fn, expected_bytes_fn)


def test_goal_export_no_crlf_no_bom(goal_backend) -> None:
    """Exported bytes use LF and have no UTF-8 BOM even when goal.md does."""
    bom = b"\xef\xbb\xbf"
    crlf_content = (
        b"---\r\nschema_version: 1\r\nactive: true\r\nintent: CRLF goal\r\n"
        b"priority: high\r\ncreated: 2026-06-11\r\nlast_progress_check: 2026-06-11\r\n"
        b"success_criteria:\r\n  - done\r\nsub_goals: []\r\n---\r\n\r\nbody\r\n"
    )
    goal_backend._agent_root.mkdir(parents=True, exist_ok=True)
    (goal_backend._agent_root / "goal.md").write_bytes(bom + crlf_content)

    result = goal_backend.export()
    assert not result.goal_md_bytes.startswith(bom), "BOM must be stripped"
    assert b"\r\n" not in result.goal_md_bytes, "CRLF must be normalized to LF"
    assert b"CRLF goal" in result.goal_md_bytes


def test_goal_export_backend_id_and_scope(goal_backend) -> None:
    """GoalExport carries backend_id and scope."""
    result = goal_backend.export()
    assert result.backend_id == "filesystem"
    assert result.scope == str(goal_backend._agent_root)


def test_goal_export_all_equals_export_none(goal_backend) -> None:
    """export_all() is the unbounded alias of export(None)."""
    _write_goal_md(goal_backend._agent_root, intent="Alias goal")
    result_none = goal_backend.export(None)
    result_all = goal_backend.export_all()
    assert result_none.goal_md_bytes == result_all.goal_md_bytes
    assert result_none.scope == result_all.scope
    assert result_none.backend_id == result_all.backend_id


def test_goal_history_export_normalizes_trailing_newline(goal_backend) -> None:
    """History export is line-NORMALIZED (newline-terminated), not byte-verbatim.

    Pins the spec/41 export contract clause: a final goal_history.jsonl line
    lacking a trailing newline (reachable only via a hand-edited or
    alternate-backend file — atomic_append_jsonl always terminates lines) is
    exported with one appended. The chosen contract is line-normalization, so
    the parity reference for the Log backend's strict-verbatim export
    (test_log_export_legacy_line_exported_verbatim) is DELIBERATELY not mirrored
    here. The non-final lines are still byte-exact (no json.dumps re-serialize),
    so key ordering is preserved.
    """
    agent_root = goal_backend._agent_root
    _write_goal_md(agent_root, intent="Trailing newline goal")
    history = agent_root / "goal_history.jsonl"
    # Two records; the FINAL line deliberately has no trailing newline.
    history.write_bytes(
        b'{"ts": "2026-06-11T00:00:00", "event": "first", "x": 1}\n'
        b'{"ts": "2026-06-11T00:00:01", "event": "second", "y": 2}'
    )

    result = goal_backend.export()
    lines = result.history_records_with_bytes

    assert len(lines) == 2
    # Every exported line is newline-terminated (normalization invariant).
    assert all(ln.endswith(b"\n") for ln in lines)
    # Non-final line is byte-exact (key order preserved, no re-serialize).
    assert lines[0] == b'{"ts": "2026-06-11T00:00:00", "event": "first", "x": 1}\n'
    # Final line got the trailing newline appended (normalized, not verbatim).
    assert lines[1] == b'{"ts": "2026-06-11T00:00:01", "event": "second", "y": 2}\n'

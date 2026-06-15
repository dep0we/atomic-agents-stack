"""Filesystem-specific tests for ``FilesystemLogBackend``.

The conformance suite (``test_log_protocol_conformance.py``) pins
behavior every backend MUST satisfy. These tests pin behavior unique
to the filesystem reference impl — the on-disk shape, atomic-write
integration, registry resolution. PR 2 leans on the byte-for-byte
preservation invariant pinned here: the JSONL line shape after
``append()`` MUST parse identically through the legacy reader at
``dashboard/costs.py:_record_from_dict`` so the four
dashboard/cost-walker call sites keep working until PR 2 rewires them.
"""

from __future__ import annotations

import errno
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.dashboard.costs import _record_from_dict
from atomic_agents.exceptions import BackendNotRegistered, LogBackendReadError
from atomic_agents.logs import (
    FilesystemLogBackend,
    LogAggregate,
    LogQuery,
    RunRecord,
    get_default_log_backend,
    get_log_backend,
    list_log_backends,
)


def _ts_at(year: int, month: int, day: int, hour: int = 12) -> str:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────
# Identity


def test_backend_id_is_filesystem(tmp_path):
    backend = FilesystemLogBackend(tmp_path)
    assert backend.backend_id == "filesystem"


def test_scope_root_is_readable(tmp_path):
    backend = FilesystemLogBackend(tmp_path)
    assert backend.scope_root == tmp_path


# ──────────────────────────────────────────────────────────────────
# On-disk shape (the load-bearing PR 2 invariant)


def test_append_writes_to_log_yyyy_mm_yyyy_mm_dd_jsonl(tmp_path):
    """Path matches agent.py:3427 byte-for-byte."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="r1",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="claude-opus-4-7",
            input_tokens=10,
            output_tokens=5,
        )
    )
    expected = tmp_path / "log" / "2026-05" / "2026-05-15.jsonl"
    assert expected.exists()


def test_append_line_byte_for_byte_compatible_with_legacy_reader(tmp_path):
    """JSONL line written through the backend reads identically via the
    legacy ``dashboard/costs._record_from_dict`` reader.

    This pins the PR 2 invariant: PR 2 can route ``agent.py:_log()``
    through the backend WITHOUT first rewiring the dashboard readers,
    because the on-disk shape stays the same. PR 2 then rewires the
    readers through ``query()`` in the same PR or a follow-up.
    """
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="r1",
            primitive="agent_call",
            trigger="agent_call",  # legacy reader uses trigger
            status="ok",
            summary="legacy reader compat",
            model="claude-opus-4-7",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
            latency_ms=1234.5,
            cache_hit_tokens=20,
            cache_miss_tokens=80,
        )
    )
    log_file = tmp_path / "log" / "2026-05" / "2026-05-15.jsonl"
    raw = log_file.read_text(encoding="utf-8").strip()
    rec = json.loads(raw)
    legacy = _record_from_dict(rec, agent="test-agent")
    assert legacy is not None
    assert legacy.run_id == "r1"
    assert legacy.trigger == "agent_call"
    assert legacy.model == "claude-opus-4-7"
    assert legacy.input_tokens == 100
    assert legacy.output_tokens == 50
    assert legacy.cost_usd == pytest.approx(0.001)
    assert legacy.latency_ms == 1234


def test_append_first_key_is_ts(tmp_path):
    """The JSONL line MUST place ``ts`` first — matches agent.py:3425
    `{"ts": ..., **record}` shape."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="r1",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    log_file = tmp_path / "log" / "2026-05" / "2026-05-15.jsonl"
    line = log_file.read_text(encoding="utf-8").strip()
    assert line.startswith('{"ts":'), f"line must start with ts; got: {line[:30]}"


def test_atomic_append_jsonl_integration(tmp_path):
    """append() routes through _io.atomic_append_jsonl (inherits fsync)."""
    backend = FilesystemLogBackend(tmp_path)
    record = RunRecord(
        ts=_ts_at(2026, 5, 15),
        run_id="r1",
        primitive="agent_call",
        status="ok",
        summary="t",
        model="m",
        input_tokens=0,
        output_tokens=0,
    )
    # The backend imports atomic_append_jsonl from .._io into its module
    # namespace — patch the module-level reference inside the backend
    # module, not the original _io location.
    with patch("atomic_agents.logs.filesystem.atomic_append_jsonl") as mock_append:
        backend.append(record)
        assert mock_append.called
        # First positional arg is the target path; second is the JSON string.
        call_args = mock_append.call_args
        assert call_args[0][0] == tmp_path / "log" / "2026-05" / "2026-05-15.jsonl"
        # Round-trip the JSON to confirm structure.
        d = json.loads(call_args[0][1])
        assert d["run_id"] == "r1"


def test_append_falls_back_to_today_for_malformed_ts(tmp_path):
    """Malformed ts → date.today() — matches existing _log() resilience."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts="not-a-timestamp",
            run_id="r1",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    # Expect a file under today's month/day to exist.
    today = date.today()
    expected = tmp_path / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    assert expected.exists()


# ──────────────────────────────────────────────────────────────────
# Read paths against multi-month state


def test_query_walks_month_directories(tmp_path):
    """Records across three months all surface via a single query."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 1, 15),
            run_id="jan",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 3, 15),
            run_id="mar",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="may",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    out = backend.query(LogQuery())
    assert {r.run_id for r in out} == {"jan", "mar", "may"}


def test_query_skips_missing_log_dir(tmp_path):
    """Fresh backend, no log dir yet — query returns [] without raising."""
    backend = FilesystemLogBackend(tmp_path)
    assert backend.query(LogQuery()) == []


def test_query_skips_unrelated_files_and_dirs(tmp_path):
    """Stray files in the log tree are skipped (matches legacy walker defensive shape)."""
    backend = FilesystemLogBackend(tmp_path)
    # Real record so the log dir exists.
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="real",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    # Stray files / directories in the log tree.
    (tmp_path / "log" / "README.md").write_text("not a JSONL")
    (tmp_path / "log" / "stray-dir").mkdir()
    (tmp_path / "log" / "2026-05" / "stray.txt").write_text("not JSONL")
    (tmp_path / "log" / "13-foo").mkdir()  # malformed month dir
    out = backend.query(LogQuery())
    assert len(out) == 1
    assert out[0].run_id == "real"


def test_query_handles_malformed_jsonl_lines(tmp_path):
    """Malformed lines are skipped — matches dashboard.costs._record_from_dict defensive shape."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="real",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    log_file = tmp_path / "log" / "2026-05" / "2026-05-15.jsonl"
    with log_file.open("a") as f:
        f.write("not json\n")
        f.write('{"corrupt": ')  # missing close brace
        f.write("\n")
    out = backend.query(LogQuery())
    assert len(out) == 1
    assert out[0].run_id == "real"


# ──────────────────────────────────────────────────────────────────
# Retention behavior


def test_delete_older_than_removes_full_month_dirs(tmp_path):
    """A full month before cutoff: every day file dropped + month dir cleaned."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 1, 15),
            run_id="jan",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="may",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.delete_older_than(datetime(2026, 3, 1, tzinfo=timezone.utc))
    # Jan month dir should be gone.
    assert not (tmp_path / "log" / "2026-01").exists()
    # May should still be there.
    assert (tmp_path / "log" / "2026-05" / "2026-05-15.jsonl").exists()


def test_delete_older_than_partial_day_rewrite(tmp_path):
    """Same-day cutoff: file rewritten atomically with only records ts >= threshold."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15, 8),
            run_id="early",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15, 14),
            run_id="late",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    threshold = datetime(2026, 5, 15, 12, tzinfo=timezone.utc)
    deleted = backend.delete_older_than(threshold)
    assert deleted == 1
    remaining = backend.query(LogQuery())
    assert len(remaining) == 1
    assert remaining[0].run_id == "late"


# ──────────────────────────────────────────────────────────────────
# Tail — multi-file reverse walk


def test_tail_reads_reverse_across_files(tmp_path):
    """Last 3 records across two day files in chronological order."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 14),
            run_id="r1",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15, 10),
            run_id="r2",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15, 14),
            run_id="r3",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    out = backend.tail(2)
    # Newest two in chronological order.
    assert [r.run_id for r in out] == ["r2", "r3"]


# ──────────────────────────────────────────────────────────────────
# Stats — size_bytes


def test_stats_size_bytes_sums_files(tmp_path):
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15),
            run_id="r1",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 16),
            run_id="r2",
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
        )
    )
    stats = backend.stats()
    actual_bytes = sum(p.stat().st_size for p in (tmp_path / "log").rglob("*.jsonl"))
    assert stats.size_bytes == actual_bytes
    assert stats.size_bytes > 0


# ──────────────────────────────────────────────────────────────────
# Aggregate against extra fields


def test_aggregate_group_by_extra_field(tmp_path):
    """group_by names can resolve through ``extra`` for primitive-specific aggregations."""
    backend = FilesystemLogBackend(tmp_path)
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15, 10),
            run_id="r1",
            primitive="outcome_iteration",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
            extra={"iteration": 0},
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15, 11),
            run_id="r1",
            primitive="outcome_iteration",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
            extra={"iteration": 1},
        )
    )
    backend.append(
        RunRecord(
            ts=_ts_at(2026, 5, 15, 12),
            run_id="r1",
            primitive="outcome_iteration",
            status="ok",
            summary="t",
            model="m",
            input_tokens=0,
            output_tokens=0,
            extra={"iteration": 1},
        )
    )
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("iteration",), metric="count"),
    )
    assert result == {(0,): 1, (1,): 2}


# ──────────────────────────────────────────────────────────────────
# Registry resolution


def test_registry_resolves_filesystem():
    assert get_log_backend("filesystem") is FilesystemLogBackend
    assert "filesystem" in list_log_backends()


def test_get_default_log_backend_returns_filesystem_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND", raising=False)
    backend = get_default_log_backend(tmp_path)
    assert isinstance(backend, FilesystemLogBackend)


def test_get_default_log_backend_honors_filesystem_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "filesystem")
    backend = get_default_log_backend(tmp_path)
    assert isinstance(backend, FilesystemLogBackend)


def test_get_default_log_backend_unknown_id_includes_sqlite_in_hint(
    tmp_path, monkeypatch
):
    """Unknown backend_id error message must point operators forward to PR 3's
    sqlite, not give them 'Available: [filesystem]' and conclude no other
    backends exist (mirrors locks/__init__.py:191 Step 11 P0-3 finding)."""
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "bogus_typo")
    with pytest.raises(BackendNotRegistered) as excinfo:
        get_default_log_backend(tmp_path)
    msg = str(excinfo.value)
    assert "bogus_typo" in msg
    assert "sqlite" in msg
    assert "filesystem" in msg


def test_get_log_backend_unknown_id_includes_sqlite_in_hint():
    """get_log_backend's error message must match get_default_log_backend's
    forward-pointer shape — Step 9.1 maintainability specialist surfaced
    the two raise sites had different error-message policies."""
    with pytest.raises(BackendNotRegistered) as excinfo:
        get_log_backend("not_a_real_backend")
    msg = str(excinfo.value)
    assert "not_a_real_backend" in msg
    assert "sqlite" in msg
    assert "filesystem" in msg


def test_get_default_log_backend_redacts_url_credential_in_error(tmp_path, monkeypatch):
    """An operator who accidentally pastes a URL into
    ATOMIC_AGENTS_LOG_BACKEND (instead of _URL) MUST NOT see the
    credential echoed in the BackendNotRegistered exception message.
    Same credential-leak failure mode as the locks arc PR 3 fix."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_LOG_BACKEND",
        "datadog://super-secret-api-key-do-not-leak@ingest.host",
    )
    with pytest.raises(BackendNotRegistered) as excinfo:
        get_default_log_backend(tmp_path)
    msg = str(excinfo.value)
    # The credential MUST NOT appear in the message.
    assert "super-secret-api-key" not in msg
    assert "ingest.host" not in msg
    # The scheme should still appear so the operator can debug.
    assert "datadog" in msg


# ──────────────────────────────────────────────────────────────────
# spec/22 read-failure addendum (#497): empty-vs-failure boundary
#
# The conformance suite covers the directory-level NON-ENOENT OSError → raise
# path (NotADirectoryError) for query/tail/aggregate. These filesystem-specific
# tests pin the finer-grained errno boundary that distinguishes the ABSENT-state
# contract (return []) and the TOCTOU skip branches from the FAIL-CLOSED raise
# branches:
#   - directory-level ENOENT (dir vanished after .exists()) → return []
#   - month-dir ENOENT mid-walk → skip that month (other months still read)
#   - per-file ENOENT (file vanished after listing) → skip that file
#   - per-file non-ENOENT OSError (EIO/EACCES) → raise LogBackendReadError
# Each test seeds a real record first so the walk body is entered (not the
# absent-backend early-return), and the assertions only pass if the injected
# errno branch actually fired (e.g. a no-op patch would return the seeded
# record, not []).


def _seed(backend: FilesystemLogBackend, ts: str, run_id: str) -> None:
    backend.append(
        RunRecord(
            ts=ts,
            run_id=run_id,
            primitive="agent_call",
            status="ok",
            summary="t",
            model="m",
            input_tokens=1,
            output_tokens=1,
        )
    )


def test_query_dir_level_enoent_after_exists_returns_empty(tmp_path, monkeypatch):
    """log_dir vanishes between .exists() and iterdir() (TOCTOU) → return []."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 6, 10), "r1")
    log_dir = tmp_path / "log"
    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        if self == log_dir:
            raise OSError(errno.ENOENT, "log dir vanished")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    # If the ENOENT branch did NOT fire, the seeded record would come back.
    assert backend.query(LogQuery()) == []


def test_query_month_dir_enoent_mid_walk_skips_month(tmp_path, monkeypatch):
    """A month dir vanishing mid-walk is skipped; other months still read."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 5, 10), "may")
    _seed(backend, _ts_at(2026, 6, 10), "jun")
    gone_month = tmp_path / "log" / "2026-06"
    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        if self == gone_month:
            raise OSError(errno.ENOENT, "month vanished")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    results = backend.query(LogQuery())
    # June skipped (not raised, not total []), May still returned.
    assert [r.run_id for r in results] == ["may"]


def test_query_per_file_enoent_skips_that_file(tmp_path, monkeypatch):
    """A day file vanishing between listing and open is skipped, not raised."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 6, 10), "keep")
    _seed(backend, _ts_at(2026, 6, 11), "vanish")
    gone_file = tmp_path / "log" / "2026-06" / "2026-06-11.jsonl"
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == gone_file:
            raise OSError(errno.ENOENT, "file vanished")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    results = backend.query(LogQuery())
    assert [r.run_id for r in results] == ["keep"]


def test_query_per_file_non_enoent_oserror_raises(tmp_path, monkeypatch):
    """A listed-but-unreadable day file (EIO) fails closed → LogBackendReadError."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 6, 10), "r1")
    day_file = tmp_path / "log" / "2026-06" / "2026-06-10.jsonl"
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == day_file:
            raise OSError(errno.EIO, "I/O error")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with pytest.raises(LogBackendReadError):
        backend.query(LogQuery())


def test_tail_dir_level_enoent_after_exists_returns_empty(tmp_path, monkeypatch):
    """tail() shares query()'s boundary: dir-level ENOENT (TOCTOU) → return []."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 6, 10), "r1")
    log_dir = tmp_path / "log"
    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        if self == log_dir:
            raise OSError(errno.ENOENT, "log dir vanished")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    assert backend.tail(5) == []


def test_tail_per_file_non_enoent_oserror_raises(tmp_path, monkeypatch):
    """tail() fails closed on a listed-but-unreadable day file (EIO)."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 6, 10), "r1")
    day_file = tmp_path / "log" / "2026-06" / "2026-06-10.jsonl"
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == day_file:
            raise OSError(errno.EIO, "I/O error")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with pytest.raises(LogBackendReadError):
        backend.tail(5)


def test_tail_month_dir_enoent_mid_walk_skips_month(tmp_path, monkeypatch):
    """tail() skips a month dir that vanishes mid-walk; other months still read."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 5, 10), "may")
    _seed(backend, _ts_at(2026, 6, 10), "jun")
    gone_month = tmp_path / "log" / "2026-06"
    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        if self == gone_month:
            raise OSError(errno.ENOENT, "month vanished")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    results = backend.tail(10)
    # June skipped (not raised, not total []), May still returned.
    assert [r.run_id for r in results] == ["may"]


def test_tail_per_file_enoent_skips_that_file(tmp_path, monkeypatch):
    """tail() skips a day file that vanishes between listing and open."""
    backend = FilesystemLogBackend(tmp_path)
    _seed(backend, _ts_at(2026, 6, 10), "keep")
    _seed(backend, _ts_at(2026, 6, 11), "vanish")
    gone_file = tmp_path / "log" / "2026-06" / "2026-06-11.jsonl"
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == gone_file:
            raise OSError(errno.ENOENT, "file vanished")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    results = backend.tail(10)
    assert [r.run_id for r in results] == ["keep"]

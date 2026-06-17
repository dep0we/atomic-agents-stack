"""PR2 wiring tests for IdempotencyBackend — spec/45 PR2.

Tests the new code paths introduced in PR2:
- Two-phase gate in agent.call() (lookup before lock, begin after cost gate)
- deduped Response fields + Response.deduped_response()
- DedupInFlight raise + in_flight audit record
- release_lease() via try/finally on exception
- commit() after JSONL write (W4 ordering)
- idempotency_key on every keyed run + replayed_run_id join
- zero-spend cost_usd absent for deduped/in_flight
- serve HTTP 200 deduped / HTTP 409 in_flight
- queue payload key extraction (extract_queue_idempotency_key)
- cron_tick_key bucketing (same-tick collides / next-tick differs)
- dedup_body_hash_enabled sha256 key derivation
- RunRecord round-trip (to_dict/from_dict)
- LogQuery.idempotency_key filter (filesystem + SQLite)
- SQLite v1->v2 migration
- _model.py Dedup Body Hash section parser

Each invariant has a per-invariant negative control per
MEMORY.md feedback_false_green_test_needs_per_invocation_negative_control.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────
# Helper


def _ts() -> str:
    return datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _make_record(
    run_id: str = "run-1",
    idempotency_key: str | None = None,
    replayed_run_id: str | None = None,
    status: str = "ok",
):
    from atomic_agents.logs import RunRecord

    return RunRecord(
        ts=_ts(),
        run_id=run_id,
        primitive="agent_call",
        status=status,
        summary="test run",
        model="claude-haiku",
        input_tokens=10,
        output_tokens=5,
        idempotency_key=idempotency_key,
        replayed_run_id=replayed_run_id,
    )


# ──────────────────────────────────────────────────────────────────
# RunRecord round-trip: idempotency_key + replayed_run_id


def test_runrecord_idempotency_key_roundtrip():
    """RunRecord to_dict/from_dict preserves idempotency_key."""
    r = _make_record(run_id="run-abc", idempotency_key="my-key")
    d = r.to_dict()
    assert d["idempotency_key"] == "my-key"

    from atomic_agents.logs import RunRecord

    r2 = RunRecord.from_dict(d)
    assert r2.idempotency_key == "my-key"


def test_runrecord_idempotency_key_roundtrip_negative():
    """NEGATIVE: idempotency_key NOT set -> absent from to_dict (None-omit pattern)."""
    r = _make_record(run_id="run-abc")
    d = r.to_dict()
    # None-omit: key absent or value is None
    assert "idempotency_key" not in d or d["idempotency_key"] is None


def test_runrecord_replayed_run_id_roundtrip():
    """RunRecord to_dict/from_dict preserves replayed_run_id."""
    r = _make_record(
        run_id="run-xyz",
        idempotency_key="ikey",
        replayed_run_id="run-original",
        status="deduped",
    )
    d = r.to_dict()
    assert d["replayed_run_id"] == "run-original"

    from atomic_agents.logs import RunRecord

    r2 = RunRecord.from_dict(d)
    assert r2.replayed_run_id == "run-original"


def test_runrecord_replayed_run_id_negative():
    """NEGATIVE: replayed_run_id NOT set -> absent from to_dict."""
    r = _make_record(run_id="run-xyz")
    d = r.to_dict()
    assert "replayed_run_id" not in d or d["replayed_run_id"] is None


# ──────────────────────────────────────────────────────────────────
# LogQuery.idempotency_key filter — filesystem backend


def test_log_filesystem_query_idempotency_key_filter(tmp_path):
    """LogQuery.idempotency_key filters correctly on filesystem backend."""
    from atomic_agents.logs import FilesystemLogBackend, LogQuery

    backend = FilesystemLogBackend(tmp_path)
    backend.append(_make_record(run_id="run-1", idempotency_key="key-a"))
    backend.append(_make_record(run_id="run-2", idempotency_key="key-b"))
    backend.append(_make_record(run_id="run-3", idempotency_key="key-a"))

    results = backend.query(LogQuery(idempotency_key="key-a"))
    run_ids = {r.run_id for r in results}
    assert run_ids == {"run-1", "run-3"}


def test_log_filesystem_query_idempotency_key_filter_negative(tmp_path):
    """NEGATIVE: wrong key -> zero results."""
    from atomic_agents.logs import FilesystemLogBackend, LogQuery

    backend = FilesystemLogBackend(tmp_path)
    backend.append(_make_record(run_id="run-1", idempotency_key="key-a"))

    results = backend.query(LogQuery(idempotency_key="key-WRONG"))
    assert results == []


def test_log_filesystem_query_no_idempotency_key_filter(tmp_path):
    """LogQuery with no idempotency_key returns all records."""
    from atomic_agents.logs import FilesystemLogBackend, LogQuery

    backend = FilesystemLogBackend(tmp_path)
    backend.append(_make_record(run_id="run-1", idempotency_key="key-a"))
    backend.append(_make_record(run_id="run-2", idempotency_key="key-b"))

    results = backend.query(LogQuery())
    assert len(results) == 2


# ──────────────────────────────────────────────────────────────────
# LogQuery.idempotency_key filter — SQLite backend


def test_log_sqlite_query_idempotency_key_filter(tmp_path):
    """LogQuery.idempotency_key filters correctly on SQLite backend."""
    from atomic_agents.logs import LogQuery
    from atomic_agents.logs.sqlite import SQLiteLogBackend

    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record(run_id="run-1", idempotency_key="key-a"))
    backend.append(_make_record(run_id="run-2", idempotency_key="key-b"))
    backend.append(_make_record(run_id="run-3", idempotency_key="key-a"))

    results = backend.query(LogQuery(idempotency_key="key-a"))
    run_ids = {r.run_id for r in results}
    assert run_ids == {"run-1", "run-3"}


def test_log_sqlite_query_idempotency_key_filter_negative(tmp_path):
    """NEGATIVE: wrong key -> zero results (SQLite backend)."""
    from atomic_agents.logs import LogQuery
    from atomic_agents.logs.sqlite import SQLiteLogBackend

    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record(run_id="run-1", idempotency_key="key-a"))

    results = backend.query(LogQuery(idempotency_key="key-WRONG"))
    assert results == []


# ──────────────────────────────────────────────────────────────────
# SQLite v1->v2 migration


def _build_v1_db(db_path: Path) -> None:
    """Create a v1 SQLite log DB (full v1 schema — without idempotency_key/replayed_run_id).

    Mirrors the actual v1 CREATE TABLE produced by SQLiteLogBackend before
    the v1->v2 migration (spec/45 PR2). Column set matches _CREATE_RUN_RECORDS
    minus idempotency_key and replayed_run_id.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            run_id TEXT NOT NULL,
            primitive TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL,
            cost_source TEXT,
            latency_ms REAL,
            cache_hit_tokens INTEGER,
            cache_miss_tokens INTEGER,
            mandate_id TEXT,
            parent_run_id TEXT,
            parent_agent TEXT,
            trigger TEXT,
            agent_name TEXT,
            fallback INTEGER,
            critical INTEGER,
            extra TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO run_records
          (ts, run_id, primitive, status, summary, model,
           input_tokens, output_tokens, cost_usd, trigger, agent_name, extra)
        VALUES
          ('2026-01-01T00:00:00+00:00', 'run-legacy', 'agent_call', 'ok',
           'legacy run', 'claude', 10, 5, 0.001, 'cron', 'agent-a', '{}')
        """
    )
    conn.commit()
    conn.close()


def test_sqlite_v1_to_v2_migration(tmp_path):
    """Opening a v1 DB auto-migrates to v2 (adds idempotency_key + replayed_run_id)."""
    from atomic_agents.logs.sqlite import SQLiteLogBackend

    db = tmp_path / "logs.db"
    _build_v1_db(db)

    # Opening the backend with a v1 DB triggers the migration
    backend = SQLiteLogBackend(db)
    backend.append(_make_record(run_id="run-new", idempotency_key="key-after-migrate"))

    # Verify schema_version bumped to 2
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert int(row[0]) == 2

    # Verify legacy record survived (NULL idempotency_key — not backfilled)
    rows = conn.execute(
        "SELECT run_id, idempotency_key FROM run_records WHERE run_id='run-legacy'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "run-legacy"
    assert rows[0][1] is None

    # Verify new record has idempotency_key
    rows2 = conn.execute(
        "SELECT run_id, idempotency_key FROM run_records WHERE run_id='run-new'"
    ).fetchall()
    assert len(rows2) == 1
    assert rows2[0][1] == "key-after-migrate"
    conn.close()


def test_sqlite_v1_db_baseline_lacks_column(tmp_path):
    """BASELINE (not a strip-control): documents the starting state of the
    migration fixture — a raw v1 DB built bypassing the backend has NO
    idempotency_key column. This is the precondition the v1→v2 migration test
    relies on; it does NOT go RED if the migration code is stripped (that
    coverage is `test_sqlite_v1_to_v2_migration`, which fails with
    `sqlite3.OperationalError: no such column` when the migration block is
    removed). Named 'baseline' so the 'negative control' label stays reserved
    for tests that verify by stripping the fix.
    """
    db = tmp_path / "logs_raw.db"
    _build_v1_db(db)

    conn = sqlite3.connect(db)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(run_records)").fetchall()]
    conn.close()
    assert "idempotency_key" not in cols


def test_sqlite_v2_idempotency_key_index_created(tmp_path):
    """v2 schema creates idx_idempotency_key index."""
    from atomic_agents.logs.sqlite import SQLiteLogBackend

    db = tmp_path / "logs.db"
    backend = SQLiteLogBackend(db)
    backend.append(_make_record())

    conn = sqlite3.connect(db)
    indexes = [
        row[1]
        for row in conn.execute("SELECT * FROM sqlite_master WHERE type='index'")
        if row[1]
    ]
    conn.close()
    assert "idx_idempotency_key" in indexes


def test_sqlite_v2_index_negative(tmp_path):
    """NEGATIVE: v1 DB (no migration) does NOT have idx_idempotency_key."""
    db = tmp_path / "raw.db"
    _build_v1_db(db)

    conn = sqlite3.connect(db)
    indexes = [
        row[1]
        for row in conn.execute("SELECT * FROM sqlite_master WHERE type='index'")
        if row[1]
    ]
    conn.close()
    assert "idx_idempotency_key" not in indexes


# ──────────────────────────────────────────────────────────────────
# cron_tick_key bucketing (spec/45 MUST 14 — cron trigger key helper)


def test_cron_tick_key_same_hour_same_key():
    """spec/45 MUST 14: two times within the same hour produce the same key."""
    from atomic_agents.idempotency import cron_tick_key

    t1 = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 16, 10, 45, 0, tzinfo=timezone.utc)
    assert cron_tick_key("agent", "sched", t1, "hour") == cron_tick_key(
        "agent", "sched", t2, "hour"
    )


def test_cron_tick_key_same_hour_negative():
    """NEGATIVE: adjacent hours produce different keys."""
    from atomic_agents.idempotency import cron_tick_key

    t1 = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 16, 11, 0, 0, tzinfo=timezone.utc)
    assert cron_tick_key("agent", "sched", t1, "hour") != cron_tick_key(
        "agent", "sched", t2, "hour"
    )


def test_cron_tick_key_same_day_same_key():
    """Two times within the same UTC day produce the same key."""
    from atomic_agents.idempotency import cron_tick_key

    t1 = datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 16, 23, 59, 59, tzinfo=timezone.utc)
    assert cron_tick_key("agent", "brief", t1, "day") == cron_tick_key(
        "agent", "brief", t2, "day"
    )


def test_cron_tick_key_different_day_different_key():
    """NEGATIVE: different UTC days produce different keys."""
    from atomic_agents.idempotency import cron_tick_key

    t1 = datetime(2026, 6, 16, 23, 59, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)
    assert cron_tick_key("agent", "brief", t1, "day") != cron_tick_key(
        "agent", "brief", t2, "day"
    )


def test_cron_tick_key_format():
    """Key format is agent_name:schedule_name:bucket_epoch_seconds."""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, 10, 30, 0, tzinfo=timezone.utc)
    key = cron_tick_key("my-agent", "daily", t, "hour")
    parts = key.split(":")
    assert parts[0] == "my-agent"
    assert parts[1] == "daily"
    # Epoch for 2026-06-16 10:00:00 UTC (floored to hour)
    assert int(parts[2]) == int(
        datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    )


def test_cron_tick_key_invalid_granularity():
    """ValueError on unsupported granularity."""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="granularity"):
        cron_tick_key("agent", "sched", t, "decade")


# ──────────────────────────────────────────────────────────────────
# extract_queue_idempotency_key


def test_extract_queue_idempotency_key_returns_key():
    """extract_queue_idempotency_key returns payload['idempotency_key']."""
    from atomic_agents._cascade import extract_queue_idempotency_key

    payload = {"idempotency_key": "my-key", "work_item": "do something"}
    assert extract_queue_idempotency_key(payload) == "my-key"


def test_extract_queue_idempotency_key_missing_returns_none():
    """NEGATIVE: missing idempotency_key field -> None."""
    from atomic_agents._cascade import extract_queue_idempotency_key

    payload = {"work_item": "do something"}
    assert extract_queue_idempotency_key(payload) is None


def test_extract_queue_idempotency_key_empty_string_returns_none():
    """NEGATIVE: empty string idempotency_key -> None (falsy guard)."""
    from atomic_agents._cascade import extract_queue_idempotency_key

    payload = {"idempotency_key": "", "work_item": "do something"}
    assert extract_queue_idempotency_key(payload) is None


def test_extract_queue_idempotency_key_non_string_returns_none():
    """NEGATIVE: non-string idempotency_key -> None."""
    from atomic_agents._cascade import extract_queue_idempotency_key

    assert extract_queue_idempotency_key({"idempotency_key": 123}) is None
    assert extract_queue_idempotency_key({"idempotency_key": None}) is None


def test_extract_queue_idempotency_key_non_dict_returns_none():
    """NEGATIVE: non-dict payload -> None (not raised)."""
    from atomic_agents._cascade import extract_queue_idempotency_key

    assert extract_queue_idempotency_key(None) is None  # type: ignore[arg-type]
    assert extract_queue_idempotency_key("not-a-dict") is None  # type: ignore[arg-type]


def test_extract_queue_idempotency_key_does_not_fall_back_to_id():
    """spec/45 W8: an 'id'-only payload MUST NOT dedup -> None.

    Queue item ids are not guaranteed globally unique across distinct work
    items; falling back to 'id' could false-dedup two unrelated runs (silently
    dropping a real run). The trigger surface fails OPEN (returns None -> no
    dedup -> the LLM runs) rather than risk a dropped run. This pins W8: only
    an explicit caller-supplied 'idempotency_key' is honored.
    """
    from atomic_agents._cascade import extract_queue_idempotency_key

    # 'id' present, idempotency_key absent -> no dedup.
    assert extract_queue_idempotency_key({"id": "item-42", "work_item": "x"}) is None
    # 'id' present alongside an explicit key -> the explicit key wins (id ignored).
    assert (
        extract_queue_idempotency_key({"id": "item-42", "idempotency_key": "k"}) == "k"
    )


# ──────────────────────────────────────────────────────────────────
# _model.py: dedup_body_hash_enabled


def test_parse_model_md_dedup_body_hash_default_false():
    """dedup_body_hash_enabled defaults to False."""
    from atomic_agents._model import parse_model_md_text

    result = parse_model_md_text("")
    assert result["dedup_body_hash_enabled"] is False


def test_parse_model_md_dedup_body_hash_section_enables():
    """'## Dedup Body Hash' section sets dedup_body_hash_enabled=True."""
    from atomic_agents._model import parse_model_md_text

    text = "## Default model\nclaude-haiku\n\n## Dedup Body Hash\n"
    result = parse_model_md_text(text)
    assert result["dedup_body_hash_enabled"] is True


def test_parse_model_md_dedup_body_hash_section_enables_with_body():
    """Section presence with body text still enables dedup_body_hash_enabled."""
    from atomic_agents._model import parse_model_md_text

    text = "## Default model\nclaude-haiku\n\n## Dedup Body Hash\nenabled\n"
    result = parse_model_md_text(text)
    assert result["dedup_body_hash_enabled"] is True


def test_parse_model_md_dedup_body_hash_negative_absent():
    """NEGATIVE: absent '## Dedup Body Hash' -> False."""
    from atomic_agents._model import parse_model_md_text

    text = "## Default model\nclaude-haiku\n"
    result = parse_model_md_text(text)
    assert result["dedup_body_hash_enabled"] is False


def test_parse_model_md_dedup_body_hash_h3_heading_does_not_enable():
    """NEGATIVE: an h3 '### Dedup Body Hash' must NOT enable dedup.

    The match is anchored to a standalone h2 line; an unanchored regex would
    match the first two '#' of '###' and spuriously enable implicit dedup the
    operator did not intend.
    """
    from atomic_agents._model import parse_model_md_text

    text = "## Default model\nclaude-haiku\n\n### Dedup Body Hash\n"
    result = parse_model_md_text(text)
    assert result["dedup_body_hash_enabled"] is False


def test_parse_model_md_dedup_body_hash_longer_heading_does_not_enable():
    """NEGATIVE: a longer h2 heading like '## Dedup Body Hash Strategy' must
    NOT enable dedup — the section name must match exactly, not as a prefix.
    """
    from atomic_agents._model import parse_model_md_text

    text = "## Default model\nclaude-haiku\n\n## Dedup Body Hash Strategy\n"
    result = parse_model_md_text(text)
    assert result["dedup_body_hash_enabled"] is False


# ──────────────────────────────────────────────────────────────────
# Response.deduped_response() fields


def test_response_deduped_response_fields():
    """Response.deduped_response() sets deduped=True and correct fields."""
    from atomic_agents.types import Response

    resp = Response.deduped_response(
        prior_run_id="run-orig",
        replayed_run_id="run-orig",
        result_ref="run-orig",
        model="claude",
    )
    assert resp.deduped is True
    assert resp.prior_run_id == "run-orig"
    assert resp.replayed_run_id == "run-orig"
    assert resp.result_ref == "run-orig"
    # cost_usd is 0.0 on the Response object (no LLM spend)
    assert resp.cost_usd == 0.0
    assert resp.input_tokens == 0
    assert resp.output_tokens == 0


def test_response_deduped_response_negative_normal_response():
    """NEGATIVE: normal Response has deduped=False."""
    from atomic_agents.types import Response

    resp = Response(
        text="Hello",
        model="claude",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
    )
    assert resp.deduped is False
    assert resp.prior_run_id is None
    assert resp.replayed_run_id is None


# ──────────────────────────────────────────────────────────────────
# ServeConfig: idempotency_header field + serve.md parsing


def test_serve_config_idempotency_header_default():
    """ServeConfig.idempotency_header defaults to 'Idempotency-Key'."""
    from atomic_agents.serve._config import ServeConfig

    cfg = ServeConfig()
    assert cfg.idempotency_header == "Idempotency-Key"


def test_parse_serve_md_idempotency_header():
    """'## Idempotency Header' section overrides the default."""
    from atomic_agents.serve._config import _parse_serve_md

    text = "## Identity Header\nX-Custom\n\n## Idempotency Header\nX-Idempotency-Key\n"
    cfg = _parse_serve_md(text)
    assert cfg.idempotency_header == "X-Idempotency-Key"


def test_parse_serve_md_idempotency_header_env_override(monkeypatch):
    """ATOMIC_AGENTS_SERVE_IDEMPOTENCY_HEADER env var overrides serve.md."""
    from atomic_agents.serve._config import _parse_serve_md

    monkeypatch.setenv("ATOMIC_AGENTS_SERVE_IDEMPOTENCY_HEADER", "X-My-Idemp")
    cfg = _parse_serve_md("")
    assert cfg.idempotency_header == "X-My-Idemp"


def test_parse_serve_md_idempotency_header_negative_no_section():
    """NEGATIVE: no '## Idempotency Header' section -> default 'Idempotency-Key'."""
    from atomic_agents.serve._config import _parse_serve_md

    cfg = _parse_serve_md("## Identity Header\nX-Custom\n")
    assert cfg.idempotency_header == "Idempotency-Key"


# ──────────────────────────────────────────────────────────────────
# make_app: idempotency_header baked into app.state


def test_make_app_idempotency_header_baked_into_state():
    """make_app() sets app.state.idempotency_header from the kwarg."""
    pytest.importorskip("starlette")
    from atomic_agents.serve._app import make_app

    app = make_app(idempotency_header="X-Custom-Idempotency")
    assert app.state.idempotency_header == "X-Custom-Idempotency"


def test_make_app_idempotency_header_default():
    """make_app() default idempotency_header is 'Idempotency-Key'."""
    pytest.importorskip("starlette")
    from atomic_agents.serve._app import make_app

    app = make_app()
    assert app.state.idempotency_header == "Idempotency-Key"


def test_make_app_idempotency_header_negative_custom():
    """NEGATIVE: non-default kwarg -> baked into state (not the default)."""
    pytest.importorskip("starlette")
    from atomic_agents.serve._app import make_app

    app = make_app(idempotency_header="X-Other")
    assert app.state.idempotency_header != "Idempotency-Key"


# ──────────────────────────────────────────────────────────────────
# serve: HTTP 200 deduped response


def _build_agent_root(agents_root: Path, name: str) -> Path:
    """Build a minimal agent folder for serve tests."""
    agent_dir = agents_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-20251201\n",
        encoding="utf-8",
    )
    return agents_root


def test_serve_call_deduped_response_http_200(tmp_path):
    """Deduped Response -> HTTP 200 with status='deduped' and served_from_cache=true."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.serve._app import make_app

    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        from atomic_agents.types import Response

        return (
            "run-dedup-001",
            Response.deduped_response(
                prior_run_id="run-orig-001",
                replayed_run_id="run-orig-001",
                result_ref="run-orig-001",
                model="claude-haiku",
            ),
        )

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call",
        side_effect=fake_run_agent_call,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "Hello"},
                headers={"Idempotency-Key": "my-key"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deduped"
    assert body["served_from_cache"] is True
    assert body["replayed_run_id"] == "run-orig-001"
    assert body["run_id"] == "run-dedup-001"
    # cost_usd MUST be absent from the HTTP body (spec/22 addendum)
    assert "cost_usd" not in body
    # The dedup ledger is MARKER-ONLY (spec/45 MUST 5) — no result body is
    # stored, so the deduped HTTP response MUST NOT emit a (misleadingly empty)
    # "output" field. The caller fetches the real bytes via result_ref. P1 fix.
    assert "output" not in body, (
        "deduped HTTP body must NOT inline an 'output' field — the ledger is "
        "marker-only; the caller resolves the result via result_ref"
    )
    assert body["result_ref"] == "run-orig-001", (
        "deduped HTTP body must carry result_ref as the fetch handle"
    )


def test_serve_call_deduped_response_negative_normal_200(tmp_path):
    """NEGATIVE: normal (non-deduped) Response -> HTTP 200 with status='ok', NOT 'deduped'."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.serve._app import make_app

    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        return (
            "run-001",
            MagicMock(
                deduped=False,
                skipped=False,
                text="Hello",
                model="claude-haiku",
                cost_usd=0.001,
                input_tokens=10,
                output_tokens=5,
            ),
        )

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call",
        side_effect=fake_run_agent_call,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "Hello"},
            )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ──────────────────────────────────────────────────────────────────
# serve: HTTP 409 in_flight response


def test_serve_call_in_flight_http_409(tmp_path):
    """DedupInFlightWithRunId -> HTTP 409 with status='in_flight', prior_run_id, run_id."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.exceptions import DedupInFlight
    from atomic_agents.serve._app import make_app
    from atomic_agents.serve._runner import DedupInFlightWithRunId

    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        original = DedupInFlight(
            "In flight: key held by run-prior-001",
            prior_run_id="run-prior-001",
        )
        raise DedupInFlightWithRunId(original, run_id="run-this-001")

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call",
        side_effect=fake_run_agent_call,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "Hello"},
                headers={"Idempotency-Key": "my-key"},
            )
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "in_flight"
    assert body["prior_run_id"] == "run-prior-001"
    assert body["run_id"] == "run-this-001"


def test_serve_call_in_flight_http_409_negative_not_500(tmp_path):
    """NEGATIVE: DedupInFlightWithRunId -> 409, NOT 500 (not swallowed as InternalError)."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.exceptions import DedupInFlight
    from atomic_agents.serve._app import make_app
    from atomic_agents.serve._runner import DedupInFlightWithRunId

    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        original = DedupInFlight("In flight", prior_run_id="run-prior")
        raise DedupInFlightWithRunId(original, run_id="run-this")

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call",
        side_effect=fake_run_agent_call,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "Hello"},
            )
    assert resp.status_code != 500


# ──────────────────────────────────────────────────────────────────
# serve: idempotency key header validation (422 on invalid)


def test_serve_call_idempotency_key_with_slash_rejected(tmp_path):
    """Idempotency-Key header with '/' -> HTTP 422 (path separator guard)."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.serve._app import make_app

    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "Hello"},
            headers={"Idempotency-Key": "my/evil/key"},
        )
    assert resp.status_code == 422


def test_serve_call_idempotency_key_slash_rejected_negative(tmp_path):
    """NEGATIVE: Idempotency-Key without path separator -> not 422 (valid key passes)."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.serve._app import make_app

    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        return (
            "run-001",
            MagicMock(
                deduped=False,
                skipped=False,
                text="Hello",
                model="claude-haiku",
                cost_usd=0.001,
                input_tokens=10,
                output_tokens=5,
            ),
        )

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call",
        side_effect=fake_run_agent_call,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "Hello"},
                headers={"Idempotency-Key": "valid-key-123"},
            )
    assert resp.status_code != 422


def test_serve_call_idempotency_key_backslash_rejected(tmp_path):
    """Idempotency-Key with backslash -> HTTP 422."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.serve._app import make_app

    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "Hello"},
            headers={"Idempotency-Key": "my\\evil"},
        )
    assert resp.status_code == 422


def test_serve_call_idempotency_key_over_length_rejected(tmp_path):
    """An over-length Idempotency-Key -> HTTP 422, NOT silent truncation.

    An idempotency key is a correctness-bearing identifier: silently narrowing
    it to a prefix would alias two distinct caller keys into one dedup bucket,
    serving a genuinely-new request the cached result of an unrelated one (a
    dropped real run — the false-dedup the queue helper explicitly refuses). The
    serve cap mirrors the backend's _MAX_KEY_LEN, so the loud rejection here
    fires exactly where the backend would also reject.
    """
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.serve._app import (
        _IDEMPOTENCY_KEY_MAX_LEN,
        make_app,
    )

    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)

    over = "a" * (_IDEMPOTENCY_KEY_MAX_LEN + 1)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "Hello"},
            headers={"Idempotency-Key": over},
        )
    assert resp.status_code == 422
    assert "too long" in resp.json()["error"]


def test_serve_call_idempotency_key_at_cap_not_rejected(tmp_path):
    """NEGATIVE CONTROL: a key EXACTLY at the cap is accepted (boundary).

    Strips the over-length guard's correctness: if the cap were applied as
    `>= cap` instead of `> cap`, or if truncation were reinstated, this
    boundary key would behave differently. A key at the cap is a legal key.
    """
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from atomic_agents.serve._app import (
        _IDEMPOTENCY_KEY_MAX_LEN,
        make_app,
    )

    agents_root = _build_agent_root(tmp_path, "testbot")

    captured: dict[str, Any] = {}

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        captured["idempotency_key"] = kwargs.get("idempotency_key")
        return (
            "run-001",
            MagicMock(
                deduped=False,
                skipped=False,
                text="Hello",
                model="claude-haiku",
                cost_usd=0.001,
                input_tokens=10,
                output_tokens=5,
            ),
        )

    at_cap = "a" * _IDEMPOTENCY_KEY_MAX_LEN
    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call",
        side_effect=fake_run_agent_call,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "Hello"},
                headers={"Idempotency-Key": at_cap},
            )
    assert resp.status_code != 422
    # The key reaches the runner UNTRUNCATED — full length preserved.
    assert captured["idempotency_key"] == at_cap
    assert len(captured["idempotency_key"]) == _IDEMPOTENCY_KEY_MAX_LEN


# ──────────────────────────────────────────────────────────────────
# dedup_body_hash_enabled — sha256 key derivation is stable


def test_dedup_body_hash_key_derived_in_call_matches_production_format():
    """When dedup_body_hash_enabled=True and no explicit key, agent.call() derives
    the key as sha256 of the EXACT production input and passes it to the backend.

    This drives real agent.call() and captures the key the backend actually
    received via a spy (so it IS load-bearing for the feature, not the stdlib).
    The expected value below INTENTIONALLY recomputes the production format
    string as an explicit oracle — this exact-format assertion is paired with
    test_dedup_body_hash_key_stability_and_discrimination below, which is
    format-agnostic (immune to a lockstep template edit) and pins the
    stability/discrimination invariant that holds regardless of serialization.
    """
    import hashlib

    captured: dict[str, Any] = {}

    class _SpyBackend:
        def lookup(self, key: str):
            captured["lookup_key"] = key
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            captured["begin_key"] = key
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            captured["commit_key"] = key

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full,
        model_md=(
            "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"
        ),
        idempotency_backend=_SpyBackend(),
        # spec/45 FIX 2: body-hash AUTO-derivation only fires on external delivery
        # triggers (http/queue/cron). Use 'http' so the derivation under test runs.
        trigger="http",
    )
    # Resolve the same four inputs production uses, then compute the expected key.
    work_item = "write a report"
    expected_input = (
        f"work_item={work_item!r},"
        f"model={agent.config.default_model!r},"
        f"max_tokens={agent.config.max_output_tokens!r},"
        f"temperature={agent.config.temperature!r}"
    )
    expected_key = hashlib.sha256(expected_input.encode("utf-8")).hexdigest()

    _run_call(agent, work_item=work_item)

    assert captured.get("lookup_key") == expected_key, (
        f"call() must derive + use the body-hash key. Got "
        f"{captured.get('lookup_key')!r}, expected {expected_key!r}"
    )
    assert captured.get("begin_key") == expected_key


def test_dedup_body_hash_key_stability_and_discrimination():
    """FORMAT-AGNOSTIC oracle for the body-hash key (P2 hardening): two calls with
    identical (work_item, model, max_tokens, temperature) MUST derive the SAME
    key, and a call with a differing work_item MUST derive a DIFFERENT key.

    This invariant holds regardless of the exact serialization format, so it is
    immune to a lockstep template edit that the exact-format test above would
    silently follow. Together the two tests pin: (1) the key IS the derived
    body hash [exact-format test], and (2) the derivation is stable +
    discriminating [this test]."""
    keys: list[str] = []

    class _SpyBackend:
        def lookup(self, key: str):
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            keys.append(key)
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            pass

    def _fresh_agent():
        return _make_call_agent(
            _build_agent_root_full,
            model_md=(
                "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"
            ),
            idempotency_backend=_SpyBackend(),
            # spec/45 FIX 2: external trigger so body-hash auto-derivation fires.
            trigger="http",
        )

    # Same inputs twice -> same key (stability).
    _run_call(_fresh_agent(), work_item="identical work")
    _run_call(_fresh_agent(), work_item="identical work")
    assert keys[0] == keys[1], "identical inputs MUST derive the same body-hash key"

    # Different work_item -> different key (discrimination).
    _run_call(_fresh_agent(), work_item="DIFFERENT work")
    assert keys[2] != keys[0], "a different work_item MUST derive a different key"


def test_dedup_body_hash_key_negative_disabled_no_key():
    """NEGATIVE: dedup_body_hash_enabled NOT set (no '## Dedup Body Hash' section)
    -> no key is derived and the backend lookup/begin are never called.

    This is the negative control for the body-hash block: strip the
    '## Dedup Body Hash' section and the dedup gate must not fire.
    """
    captured: dict[str, Any] = {}

    class _SpyBackend:
        def lookup(self, key: str):
            captured["lookup_key"] = key
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            captured["begin_key"] = key
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full,
        model_md="## Default model\nclaude-haiku-4-5-20251001\n",  # NO dedup section
        idempotency_backend=_SpyBackend(),
    )
    _run_call(agent, work_item="write a report")
    assert "lookup_key" not in captured, (
        "no body-hash section -> dedup gate must NOT fire (no derived key)"
    )
    assert "begin_key" not in captured


def test_dedup_body_hash_explicit_key_wins():
    """An explicit idempotency_key always wins over body-hash derivation (zero-override)."""
    captured: dict[str, Any] = {}

    class _SpyBackend:
        def lookup(self, key: str):
            captured["lookup_key"] = key
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full,
        model_md=(
            "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"
        ),
        idempotency_backend=_SpyBackend(),
    )
    _run_call(agent, work_item="write a report", idempotency_key="explicit-key-123")
    assert captured["lookup_key"] == "explicit-key-123", (
        "explicit key must override body-hash derivation"
    )


# ──────────────────────────────────────────────────────────────────
# DedupInFlight exception shape


def test_dedup_in_flight_exception_carries_prior_run_id():
    """DedupInFlight exception carries prior_run_id attribute."""
    from atomic_agents.exceptions import DedupInFlight

    exc = DedupInFlight("Key is in flight", prior_run_id="run-prior-999")
    assert exc.prior_run_id == "run-prior-999"


def test_dedup_in_flight_exception_negative_no_run_id():
    """NEGATIVE: DedupInFlight with no prior_run_id -> prior_run_id is None."""
    from atomic_agents.exceptions import DedupInFlight

    exc = DedupInFlight("In flight", prior_run_id=None)
    assert exc.prior_run_id is None


# ──────────────────────────────────────────────────────────────────
# DedupInFlightWithRunId shape


def test_dedup_in_flight_with_run_id_carries_both():
    """DedupInFlightWithRunId carries both run_id (current) and prior_run_id."""
    from atomic_agents.exceptions import DedupInFlight
    from atomic_agents.serve._runner import DedupInFlightWithRunId

    original = DedupInFlight("In flight", prior_run_id="run-prior-001")
    wrapped = DedupInFlightWithRunId(original, run_id="run-this-001")
    assert wrapped.prior_run_id == "run-prior-001"
    assert wrapped.run_id == "run-this-001"


def test_dedup_in_flight_with_run_id_negative_distinct():
    """NEGATIVE: DedupInFlightWithRunId.run_id is distinct from prior_run_id."""
    from atomic_agents.exceptions import DedupInFlight
    from atomic_agents.serve._runner import DedupInFlightWithRunId

    original = DedupInFlight("In flight", prior_run_id="run-prior")
    wrapped = DedupInFlightWithRunId(original, run_id="run-current")
    assert wrapped.run_id != wrapped.prior_run_id


# ══════════════════════════════════════════════════════════════════════
# agent.call() two-phase dedup gate — INTEGRATION tests (spec/45 W1-W7).
#
# These drive a REAL AtomicAgent.call() with an injected idempotency backend
# and a patched LLM, exercising the load-bearing gate that the serve/unit
# tests above never reach (the serve tests mock run_agent_call). Each invariant
# has a per-invariant negative control verified empirically (strip the guard ->
# the matching test goes RED). Covers: W1 Phase-1 lookup-before-lock COMPLETED;
# W2 deduped record/Response; W3 begin IN_FLIGHT raise + in_flight record;
# W4 commit-AFTER-_log ordering; W5 release-on-failure (incl. crash); W6
# idempotency_key on every keyed ok run; W7 Phase-2 begin->COMPLETED (the race).
# ══════════════════════════════════════════════════════════════════════


def _build_agent_root_full(agents_root: Path, name: str = "callbot") -> Path:
    """Build the on-disk shape AtomicAgent.__init__ requires for call() tests."""
    agent_dir = agents_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "persona").mkdir(exist_ok=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (agent_dir / "tools.md").write_text(
        "## Write paths\n- memory/\n\n## Read-only paths\n(none)\n",
        encoding="utf-8",
    )
    (agent_dir / "memory").mkdir(exist_ok=True)
    return agents_root


class _FakeLockBackend:
    """Deterministic in-memory LockBackend for call()-integration tests.

    The call()-integration tests inject a fake idempotency backend but must NOT
    depend on the REAL FilesystemLockBackend's on-disk ``.lock`` artifact: under
    the full suite that exposes them to the macOS APFS WAL / lock-file timing
    flake (MEMORY.md feedback_ship_end_to_end_no_shortcuts), producing a ~15%
    intermittent failure. This fake grants the lock purely in-process so the
    dedup gate is exercised with zero filesystem timing dependence.
    """

    backend_id = "fake-inmemory"

    def __init__(self) -> None:
        self._held = False

    def acquire(self, name: str = "", timeout: float = 0.0):
        import time as _time

        from atomic_agents.exceptions import LockBusy
        from atomic_agents.locks.types import LockHandle

        if self._held:
            raise LockBusy(f"lock {name!r} already held (fake)")
        self._held = True
        handle = LockHandle(
            name=name,
            acquired_at=_time.time(),
            holder_pid=0,
            backend_state=object(),
        )
        # Route __exit__ back through release() like a real backend.
        object.__setattr__(handle, "_backend", self)
        return handle

    def release(self, handle) -> None:
        self._held = False
        # Clear backend_state so a released handle cannot be re-entered,
        # mirroring the real backend's contract.
        try:
            object.__setattr__(handle, "backend_state", None)
        except Exception:
            pass

    def renew(self, handle) -> bool:
        return True

    def is_held(self, name: str = "") -> bool:
        return self._held

    def capabilities(self):
        from atomic_agents.locks.types import LockCapabilities

        return LockCapabilities()

    def scope(self, sub_path: str):
        return self


class _FakeLogBackend:
    """In-memory LogBackend so call()-integration tests never touch the real
    filesystem JSONL writer (which under the full suite shares the APFS timing
    surface). Records are captured in ``.records`` for assertions; the dedup
    tests additionally patch ``agent._log`` so this is belt-and-suspenders, but
    construction MUST be side-effect-free and fast."""

    backend_id = "fake-inmemory"

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(dict(record))

    def query(self, q):  # pragma: no cover - not exercised by these tests
        return list(self.records)

    def tail(self, n: int = 50):  # pragma: no cover
        return list(self.records)[-n:]

    def aggregate(self, *a, **k):  # pragma: no cover
        return {}


def _make_call_agent(
    root_builder,
    *,
    model_md: str = "## Default model\nclaude-haiku-4-5-20251001\n",
    idempotency_backend: Any = None,
    name: str = "callbot",
    tmp_path: Path | None = None,
    trigger: str = "manual",
):
    """Construct an AtomicAgent wired with an injected idempotency backend.

    Lock + log backends are injected as deterministic in-memory fakes so the
    call()-integration tests do not depend on real-filesystem lock/log timing
    (the documented macOS APFS WAL flake). Only the dedup decision path — the
    behavior under test — uses the injected ``idempotency_backend``.
    """
    import tempfile

    base = tmp_path or Path(tempfile.mkdtemp())
    agents_root = root_builder(base, name)
    (agents_root / name / "model.md").write_text(model_md, encoding="utf-8")

    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name=name,
        trigger=trigger,
        agents_root=agents_root,
        idempotency_backend=idempotency_backend,
        lock_backend=_FakeLockBackend(),
        log_backend=_FakeLogBackend(),
    )
    return agent


def _fake_llm_response(text: str = "hello"):
    resp = MagicMock()
    resp.text = text
    resp.tool_uses = []
    resp.input_tokens = 7
    resp.output_tokens = 3
    resp.cache_hit_tokens = 0
    resp.cache_miss_tokens = 0
    resp.raw = {}
    return resp


def _run_call(
    agent,
    *,
    work_item: str = "ping",
    idempotency_key=None,
    log_sink=None,
    llm_mock=None,
):
    """Invoke agent.call() with the LLM + heavy persona loading patched out.

    ``log_sink`` (if provided) is a list that receives a copy of every JSONL
    record passed to agent._log(). Returns the Response (or re-raises).

    ``llm_mock`` (if provided) is the EXACT mock installed as
    ``atomic_agents._llm.call_llm`` for the duration of the call. Tests that
    want to assert "the LLM did NOT run" MUST pass their own spy here and
    assert against it — otherwise an inner-vs-outer ``patch`` of the same
    target shadows the test's spy and the ``call_count == 0`` assertion is
    vacuously true (false-green, per
    feedback_false_green_test_needs_per_invocation_negative_control).
    """

    def fake_log(record: dict) -> None:
        if log_sink is not None:
            log_sink.append(dict(record))

    kwargs = {"work_item": work_item}
    if idempotency_key is not None:
        kwargs["idempotency_key"] = idempotency_key

    _llm_patch = (
        patch("atomic_agents._llm.call_llm", llm_mock)
        if llm_mock is not None
        else patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response())
    )

    with (
        _llm_patch,
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok", cost_data_degraded=False),
        ),
    ):
        return agent.call(**kwargs)


# ──────────────────────────────────────────────────────────────────
# W1 + W2: Phase-1 lookup -> COMPLETED short-circuit


def test_call_phase1_lookup_completed_returns_deduped_no_llm(tmp_path):
    """COMPLETED at Phase-1 lookup() -> deduped Response, NO LLM call, NO begin(),
    + a status='deduped' JSONL record with idempotency_key + replayed_run_id set
    and cost_usd ABSENT (spec/22 addendum)."""
    from atomic_agents.idempotency.types import COMPLETED, DedupDecision

    begin_calls: list = []

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id="run-orig-77",
                prior_result_ref="run-orig-77",
            )

        def begin(self, key: str, run_id: str):
            begin_calls.append(key)
            return DedupDecision(
                is_duplicate=False,
                state="fresh",
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )
    sink: list = []
    # Pass the spy AS the patch target so the body uses THIS mock (no inner-
    # vs-outer shadowing); a stripped COMPLETED branch would then run the LLM
    # and drive call_count > 0 -> RED (true negative control).
    llm_spy = MagicMock(return_value=_fake_llm_response())
    resp = _run_call(agent, idempotency_key="k1", log_sink=sink, llm_mock=llm_spy)

    assert resp.deduped is True
    assert resp.replayed_run_id == "run-orig-77"
    assert resp.result_ref == "run-orig-77"
    assert llm_spy.call_count == 0, "COMPLETED lookup must NOT run the LLM"
    assert begin_calls == [], "Phase-1 COMPLETED must short-circuit before begin()"
    dedup_recs = [r for r in sink if r.get("status") == "deduped"]
    assert len(dedup_recs) == 1
    rec = dedup_recs[0]
    assert rec["idempotency_key"] == "k1"
    assert rec["replayed_run_id"] == "run-orig-77"
    assert "cost_usd" not in rec, "deduped record MUST omit cost_usd (spec/22 addendum)"


def test_call_phase1_lookup_completed_negative_fresh_runs_llm(tmp_path):
    """NEGATIVE control for W1: a FRESH lookup must NOT short-circuit — the LLM
    runs and the response is NOT deduped. (Strip the `state == COMPLETED` check
    and this stays green while the COMPLETED test goes wrong — they bracket it.)"""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )
    resp = _run_call(agent, idempotency_key="k1")
    assert resp.deduped is False
    # The fake LLM returns text "hello"; a non-deduped response means the body
    # (and thus the LLM call) ran. A deduped short-circuit returns text="".
    assert resp.text == "hello", "FRESH lookup must run the LLM body, not short-circuit"


# ──────────────────────────────────────────────────────────────────
# W7: Phase-2 begin() -> COMPLETED race (the P0 fix)


def test_call_phase2_begin_completed_serves_deduped_no_llm(tmp_path):
    """W7 / P0: lookup() returns FRESH (twin not yet committed) but begin() returns
    COMPLETED (twin committed between our lookup and begin). call() MUST serve the
    cached result — NO LLM run, NO commit() of a key we don't own, a status='deduped'
    record, and a deduped Response. Stripping the Phase-2 COMPLETED branch causes a
    double-spend (LLM runs) -> this test goes RED."""
    from atomic_agents.idempotency.types import COMPLETED, DedupDecision, FRESH

    commit_calls: list = []

    class _RaceBackend:
        def lookup(self, key: str):
            # Phase 1: looks fresh.
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            # Phase 2: a concurrent twin committed in the meantime.
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id="run-twin-9",
                prior_result_ref="run-twin-9",
            )

        def commit(self, key: str, result_ref: str) -> None:
            commit_calls.append(key)

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_RaceBackend(), tmp_path=tmp_path
    )
    sink: list = []
    # Spy IS the body's mock (no shadowing) so the P0 double-spend guard is
    # load-bearing: strip the Phase-2 COMPLETED branch and the LLM runs ->
    # call_count > 0 -> RED.
    llm_spy = MagicMock(return_value=_fake_llm_response())
    resp = _run_call(agent, idempotency_key="krace", log_sink=sink, llm_mock=llm_spy)

    assert resp.deduped is True, (
        "Phase-2 begin()->COMPLETED must serve the cached result"
    )
    assert resp.replayed_run_id == "run-twin-9"
    assert llm_spy.call_count == 0, (
        "begin()->COMPLETED must NOT run the LLM (P0 double-spend)"
    )
    assert commit_calls == [], "must NOT commit a key whose lease we never claimed"
    dedup_recs = [r for r in sink if r.get("status") == "deduped"]
    assert len(dedup_recs) == 1
    assert dedup_recs[0]["replayed_run_id"] == "run-twin-9"
    assert "cost_usd" not in dedup_recs[0]


def test_call_phase2_begin_fresh_negative_runs_llm_and_commits(tmp_path):
    """NEGATIVE control for W7: begin()->FRESH must run the LLM and commit().
    Brackets the COMPLETED branch so a mis-wired COMPLETED-as-FRESH is caught."""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    commit_calls: list = []

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            commit_calls.append(key)

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )
    resp = _run_call(agent, idempotency_key="kfresh")
    assert resp.deduped is False
    # The fake LLM returns "hello"; non-deduped + text proves the body ran.
    assert resp.text == "hello", (
        "FRESH begin() must run the LLM body, not short-circuit"
    )
    assert commit_calls == ["kfresh"], "FRESH path must commit the key after the run"


# ──────────────────────────────────────────────────────────────────
# W3: begin() -> IN_FLIGHT raises DedupInFlight + writes in_flight record


def test_call_begin_in_flight_raises_and_writes_record(tmp_path):
    """begin()->IN_FLIGHT raises DedupInFlight AND writes a status='in_flight'
    record (idempotency_key set, replayed_run_id + cost_usd ABSENT), and does NOT
    release the lease (the caller never owned it)."""
    from atomic_agents.exceptions import DedupInFlight
    from atomic_agents.idempotency.types import DedupDecision, FRESH, IN_FLIGHT

    release_calls: list = []

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=True,
                state=IN_FLIGHT,
                prior_run_id="run-holder-3",
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            release_calls.append(key)

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )
    sink: list = []
    # Spy IS the body's mock (no shadowing): the "IN_FLIGHT never runs the LLM"
    # guard goes RED if begin()->IN_FLIGHT stops raising before the body.
    llm_spy = MagicMock(return_value=_fake_llm_response())
    with pytest.raises(DedupInFlight) as exc_info:
        _run_call(agent, idempotency_key="kbusy", log_sink=sink, llm_mock=llm_spy)

    assert exc_info.value.prior_run_id == "run-holder-3"
    assert llm_spy.call_count == 0, "IN_FLIGHT must NOT run the LLM"
    in_flight_recs = [r for r in sink if r.get("status") == "in_flight"]
    assert len(in_flight_recs) == 1, "must write exactly one in_flight audit record"
    rec = in_flight_recs[0]
    assert rec["idempotency_key"] == "kbusy"
    assert "replayed_run_id" not in rec, (
        "in_flight record MUST NOT carry replayed_run_id"
    )
    assert "cost_usd" not in rec, (
        "in_flight record MUST omit cost_usd (spec/22 addendum)"
    )
    assert release_calls == [], "must NOT release a lease the caller never claimed"


def test_call_begin_in_flight_negative_no_record_when_fresh(tmp_path):
    """NEGATIVE control for W3: FRESH begin() writes NO in_flight record and raises
    nothing. Strip the in_flight record write and the positive test above goes RED;
    this one stays green either way (it asserts ABSENCE on the FRESH path)."""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )
    sink: list = []
    _run_call(agent, idempotency_key="kok", log_sink=sink)
    assert [r for r in sink if r.get("status") == "in_flight"] == []


# ──────────────────────────────────────────────────────────────────
# W4: commit() runs AFTER _log() on the success path (durable JSONL first)


def test_call_commit_after_log_ordering(tmp_path):
    """W4: the terminal JSONL record is written BEFORE commit() on the ok path.
    Instrument both sinks into one ordered event list and assert _log precedes
    commit. Swapping the two production lines reverses the order -> RED."""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    events: list = []

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            events.append(("commit", key))

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )

    def fake_log(record: dict) -> None:
        # Only the terminal ok record is relevant for ordering.
        if record.get("status") == "ok":
            events.append(("log_ok", record.get("idempotency_key")))

    with (
        patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response()),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok", cost_data_degraded=False),
        ),
    ):
        agent.call(work_item="ping", idempotency_key="korder")

    kinds = [e[0] for e in events]
    assert "log_ok" in kinds and "commit" in kinds, f"missing events: {events}"
    assert kinds.index("log_ok") < kinds.index("commit"), (
        f"_log(ok) MUST precede commit() (W4). Order was: {kinds}"
    )


# ──────────────────────────────────────────────────────────────────
# W5: release-on-failure via try/finally (incl. crash)


def test_call_release_lease_on_exception_in_body(tmp_path):
    """W5: an exception mid-body (after begin()->FRESH) releases the lease in
    finally. Stripping the finally release_lease() leaves release_calls empty -> RED."""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    release_calls: list = []

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            release_calls.append(key)

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )

    with (
        patch(
            "atomic_agents._llm.call_llm",
            side_effect=RuntimeError("boom mid-call"),
        ),
        patch.object(agent, "_log"),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok", cost_data_degraded=False),
        ),
    ):
        with pytest.raises(RuntimeError):
            agent.call(work_item="ping", idempotency_key="kcrash")

    assert release_calls == ["kcrash"], (
        "lease MUST be released in finally on a mid-body exception (W5)"
    )


def test_call_release_lease_on_baseexception_crash(tmp_path):
    """W5 crash variant: even a BaseException (e.g. KeyboardInterrupt) mid-body
    releases the lease in finally so the key never wedges IN_FLIGHT."""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    release_calls: list = []

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            release_calls.append(key)

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )

    with (
        patch(
            "atomic_agents._llm.call_llm",
            side_effect=KeyboardInterrupt(),
        ),
        patch.object(agent, "_log"),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok", cost_data_degraded=False),
        ),
    ):
        with pytest.raises(BaseException):
            agent.call(work_item="ping", idempotency_key="kint")

    assert release_calls == ["kint"], (
        "lease MUST be released in finally even on BaseException (W5 crash)"
    )


def test_call_no_release_when_lease_not_held_negative(tmp_path):
    """NEGATIVE control for W5: when begin() returned COMPLETED (lease never held),
    a later return must NOT call release_lease()."""
    from atomic_agents.idempotency.types import COMPLETED, DedupDecision, FRESH

    release_calls: list = []

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id="r",
                prior_result_ref="r",
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            release_calls.append(key)

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )
    _run_call(agent, idempotency_key="kdedup")
    assert release_calls == [], "no lease held -> finally must NOT release"


# ──────────────────────────────────────────────────────────────────
# W6: idempotency_key recorded on every keyed ok run


def test_call_ok_record_carries_idempotency_key(tmp_path):
    """W6: a FRESH ok run records idempotency_key on the terminal record (no
    replayed_run_id — no prior result served). Strip the ok-path tag -> RED."""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    class _Backend:
        def lookup(self, key: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def release_lease(self, key: str) -> None:
            pass

    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=_Backend(), tmp_path=tmp_path
    )
    sink: list = []
    _run_call(agent, idempotency_key="kkeyed", log_sink=sink)
    ok_recs = [r for r in sink if r.get("status") == "ok"]
    assert ok_recs, f"no ok record found in {sink}"
    assert ok_recs[-1].get("idempotency_key") == "kkeyed"
    assert "replayed_run_id" not in ok_recs[-1], (
        "ok record MUST NOT carry replayed_run_id (no prior result served)"
    )


def test_call_ok_record_no_key_when_unkeyed_negative(tmp_path):
    """NEGATIVE control for W6: an UNKEYED run (no idempotency_key) writes an ok
    record with NO idempotency_key field, and the dedup gate never fires."""
    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=None, tmp_path=tmp_path
    )
    sink: list = []
    _run_call(agent, log_sink=sink)  # no idempotency_key
    ok_recs = [r for r in sink if r.get("status") == "ok"]
    assert ok_recs, f"no ok record found in {sink}"
    assert "idempotency_key" not in ok_recs[-1], (
        "unkeyed run MUST NOT tag idempotency_key on the ok record"
    )


# ══════════════════════════════════════════════════════════════════════
# FIX 1 — deferred (ESCALATE) run MUST NOT commit() the idempotency key
# ══════════════════════════════════════════════════════════════════════


class _LedgerSpyBackend:
    """In-memory IdempotencyBackend spy: tracks begin/commit/release + ledger
    state so a test can assert a deferred run leaves the key UNcommitted and a
    retry re-runs (sees FRESH again)."""

    def __init__(self) -> None:
        self.committed: dict[str, str] = {}  # key -> result_ref
        self.in_flight: dict[str, str] = {}  # key -> run_id
        self.begin_calls: list = []
        self.commit_calls: list = []
        self.release_calls: list = []

    def lookup(self, key: str):
        from atomic_agents.idempotency.types import (
            COMPLETED,
            DedupDecision,
            FRESH,
        )

        if key in self.committed:
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id=self.committed[key],
                prior_result_ref=self.committed[key],
            )
        return DedupDecision(
            is_duplicate=False, state=FRESH, prior_run_id=None, prior_result_ref=None
        )

    def begin(self, key: str, run_id: str):
        from atomic_agents.idempotency.types import (
            COMPLETED,
            DedupDecision,
            FRESH,
            IN_FLIGHT,
        )

        self.begin_calls.append(key)
        if key in self.committed:
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id=self.committed[key],
                prior_result_ref=self.committed[key],
            )
        if key in self.in_flight:
            return DedupDecision(
                is_duplicate=True,
                state=IN_FLIGHT,
                prior_run_id=self.in_flight[key],
                prior_result_ref=None,
            )
        self.in_flight[key] = run_id
        return DedupDecision(
            is_duplicate=False, state=FRESH, prior_run_id=None, prior_result_ref=None
        )

    def commit(self, key: str, result_ref: str) -> None:
        self.commit_calls.append(key)
        self.committed[key] = result_ref
        self.in_flight.pop(key, None)

    def release_lease(self, key: str) -> None:
        self.release_calls.append(key)
        self.in_flight.pop(key, None)


def _run_call_deferred(agent, *, work_item="ping", idempotency_key=None, log_sink=None):
    """Drive agent.call() where the body produces a DEFERRED Response.

    Patches the loop so the final Response carries deferred=True with an
    escalation_queue_id (the ESCALATE shape). The simplest robust way is to
    patch the LLM to return no tool_uses (single turn) and then patch the
    Response construction is brittle; instead we patch the agent's
    _derive_summary path is unnecessary — we monkeypatch the module-level
    Response so the success-path Response is built deferred. We instead set up
    a judge-deferral via the tool loop, but that is heavy. The minimal, robust
    approach: patch atomic_agents.agent.Response so the ok-path construction
    yields deferred=True.
    """
    from unittest.mock import patch as _patch

    real_response_cls = None
    from atomic_agents import types as _types

    real_response_cls = _types.Response

    def _deferred_factory(*a, **k):
        # Force the success-path Response to be deferred regardless of inputs.
        k["deferred"] = True
        k.setdefault("escalation_queue_ids", ["esc-queue-1"])
        return real_response_cls(*a, **k)

    def fake_log(record: dict) -> None:
        if log_sink is not None:
            log_sink.append(dict(record))

    kwargs = {"work_item": work_item}
    if idempotency_key is not None:
        kwargs["idempotency_key"] = idempotency_key

    import atomic_agents.agent as _agent_mod

    with (
        _patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response()),
        _patch.object(_agent_mod, "Response", side_effect=_deferred_factory),
        _patch.object(agent, "_log", side_effect=fake_log),
        _patch.object(agent, "load"),
        _patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        _patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        _patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok", cost_data_degraded=False),
        ),
    ):
        return agent.call(**kwargs)


def test_call_deferred_run_does_not_commit_and_retry_reruns(tmp_path):
    """FIX 1 / spec/45: a keyed run that returns deferred=True MUST NOT commit()
    the idempotency key — the ledger stays UNcommitted (lookup after = FRESH),
    and a SECOND call with the same key re-runs and again returns deferred=True
    (the escalation is re-surfaced), NOT a deduped empty Response."""
    from atomic_agents.idempotency.types import FRESH

    backend = _LedgerSpyBackend()
    agent = _make_call_agent(
        _build_agent_root_full,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="http",
    )

    resp1 = _run_call_deferred(agent, idempotency_key="kdefer")
    assert resp1.deferred is True, "first run must be a deferred (ESCALATE) Response"
    assert backend.commit_calls == [], (
        "a deferred run MUST NOT commit() the idempotency key"
    )
    # Ledger stays FRESH after the deferred run (the lease was released).
    assert backend.lookup("kdefer").state == FRESH, (
        "deferred run must leave the ledger UNcommitted (FRESH)"
    )
    assert backend.release_calls == ["kdefer"], (
        "the lease MUST be released so a retry re-runs"
    )

    # Second call with the same key: must RE-RUN (deferred again), NOT dedup.
    # Build a fresh agent (new run_id) sharing the same ledger.
    agent2 = _make_call_agent(
        _build_agent_root_full,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="http",
        name="callbot2",
    )
    resp2 = _run_call_deferred(agent2, idempotency_key="kdefer")
    assert resp2.deferred is True, (
        "retry of a deferred key MUST re-run and re-surface the escalation, "
        "NOT return a deduped empty Response"
    )
    assert resp2.deduped is False, "retry MUST NOT be served as a dedup"


def test_call_non_deferred_run_does_commit_negative(tmp_path):
    """NEGATIVE CONTROL for FIX 1: a NON-deferred ok run DOES commit() — so the
    deferred guard is load-bearing (it gates only the deferred case). With the
    `if not ...deferred` guard removed, the deferred test above would let the
    deferred run commit and the retry would return deduped — this control proves
    the commit path itself works for the ordinary (non-deferred) success run."""
    backend = _LedgerSpyBackend()
    agent = _make_call_agent(
        _build_agent_root_full,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="http",
    )
    resp = _run_call(agent, idempotency_key="kok")
    assert resp.deferred is False
    assert backend.commit_calls == ["kok"], (
        "an ordinary (non-deferred) ok run MUST commit the key"
    )


# ══════════════════════════════════════════════════════════════════════
# FIX 2 — body-hash auto-derivation gated to external delivery triggers
# ══════════════════════════════════════════════════════════════════════


def _spy_backend_capture():
    """Return (backend, captured) where captured records lookup/begin keys."""
    captured: dict[str, Any] = {"lookup_keys": [], "begin_keys": []}

    class _Spy:
        def lookup(self, key: str):
            from atomic_agents.idempotency.types import (
                COMPLETED,
                DedupDecision,
                FRESH,
            )

            captured["lookup_keys"].append(key)
            # First sighting FRESH; a repeat of the SAME key -> COMPLETED so the
            # second identical call is served as a dedup (proves dedup fired).
            if key in captured.get("_committed", set()):
                return DedupDecision(
                    is_duplicate=True,
                    state=COMPLETED,
                    prior_run_id="prior",
                    prior_result_ref="prior",
                )
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str):
            from atomic_agents.idempotency.types import DedupDecision, FRESH

            captured["begin_keys"].append(key)
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def commit(self, key: str, result_ref: str) -> None:
            captured.setdefault("_committed", set()).add(key)

        def release_lease(self, key: str) -> None:
            pass

    return _Spy(), captured


def test_body_hash_not_derived_for_manual_trigger(tmp_path):
    """FIX 2 (a): dedup_body_hash_enabled=True with trigger='manual' MUST NOT
    auto-derive a key — two identical calls BOTH run (no dedup gate fires)."""
    backend, captured = _spy_backend_capture()
    md = "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"
    agent = _make_call_agent(
        _build_agent_root_full,
        model_md=md,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="manual",
    )
    _run_call(agent, work_item="identical")
    assert captured["lookup_keys"] == [], (
        "trigger='manual' MUST NOT auto-derive a body-hash key (internal caller)"
    )
    assert captured["begin_keys"] == []


def test_body_hash_not_derived_for_delegate_trigger(tmp_path):
    """FIX 2 (a, delegate variant): trigger='delegate' is a framework-internal
    caller and MUST NOT auto-derive a body-hash key."""
    backend, captured = _spy_backend_capture()
    md = "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"
    agent = _make_call_agent(
        _build_agent_root_full,
        model_md=md,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="delegate",
    )
    _run_call(agent, work_item="identical")
    assert captured["lookup_keys"] == [], (
        "trigger='delegate' MUST NOT auto-derive a body-hash key"
    )


def test_body_hash_derived_for_http_trigger(tmp_path):
    """FIX 2 (b): the SAME agent with trigger='http' DOES auto-derive — the
    body-hash key is computed and the dedup gate fires.

    This is the NEGATIVE CONTROL for the trigger gate: remove the
    `self.trigger in _BODY_HASH_AUTO_DERIVE_TRIGGERS` check and
    test_body_hash_not_derived_for_manual_trigger goes RED (manual would derive).
    This test proves the external-trigger path still derives, so the gate is not
    simply disabling the feature."""
    backend, captured = _spy_backend_capture()
    md = "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"
    agent = _make_call_agent(
        _build_agent_root_full,
        model_md=md,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="http",
    )
    _run_call(agent, work_item="identical")
    assert len(captured["lookup_keys"]) == 1, (
        "trigger='http' MUST auto-derive a body-hash key (external delivery)"
    )
    # The derived key is a sha256 hex digest (64 chars).
    assert len(captured["lookup_keys"][0]) == 64


def test_body_hash_http_dedups_second_identical_call(tmp_path):
    """FIX 2 (b cont.): trigger='http' dedups the SECOND identical call.

    First call: FRESH -> runs -> commits. Second identical call: lookup() sees
    the committed key and serves a deduped Response (the LLM does not run)."""
    backend, captured = _spy_backend_capture()
    md = "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"

    def _mk():
        return _make_call_agent(
            _build_agent_root_full,
            model_md=md,
            idempotency_backend=backend,
            tmp_path=tmp_path,
            trigger="http",
            name="httpbot",
        )

    r1 = _run_call(_mk(), work_item="identical")
    assert r1.deduped is False, "first identical http call runs (FRESH)"
    r2 = _run_call(_mk(), work_item="identical")
    assert r2.deduped is True, (
        "second identical http call MUST dedup (body-hash auto-derivation)"
    )


def test_body_hash_explicit_key_dedups_on_manual_trigger(tmp_path):
    """FIX 2 (c): an EXPLICIT idempotency_key dedups regardless of trigger — even
    trigger='manual' (which never AUTO-derives) honors an explicit key. Second
    identical explicit-keyed manual call is served as a dedup."""
    backend, captured = _spy_backend_capture()
    md = "## Default model\nclaude-haiku-4-5-20251001\n\n## Dedup Body Hash\n"

    def _mk():
        return _make_call_agent(
            _build_agent_root_full,
            model_md=md,
            idempotency_backend=backend,
            tmp_path=tmp_path,
            trigger="manual",
            name="manualbot",
        )

    r1 = _run_call(_mk(), work_item="x", idempotency_key="explicit-k")
    assert r1.deduped is False, "first explicit-keyed manual call runs (FRESH)"
    r2 = _run_call(_mk(), work_item="x", idempotency_key="explicit-k")
    assert r2.deduped is True, (
        "an explicit idempotency_key MUST dedup on ANY trigger, incl. manual"
    )
    assert captured["lookup_keys"][0] == "explicit-k", (
        "explicit key is honored verbatim on manual trigger (not auto-derived)"
    )


# ══════════════════════════════════════════════════════════════════════
# FIX 3 — W6: cost-skip records on keyed runs carry idempotency_key
# ══════════════════════════════════════════════════════════════════════


def test_preloop_cost_skip_record_carries_idempotency_key(tmp_path):
    """FIX 3 / W6: a keyed run refused at the PRE-LOOP cost gate tags
    idempotency_key on the skip record AND never commits (ledger uncommitted).

    NEGATIVE CONTROL: strip the `_skip_record['idempotency_key'] = ...` line and
    this assertion goes RED."""
    from atomic_agents.idempotency.types import DedupDecision, FRESH

    backend = _LedgerSpyBackend()
    agent = _make_call_agent(
        _build_agent_root_full,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="http",
    )

    sink: list = []

    def fake_log(record: dict) -> None:
        sink.append(dict(record))

    with (
        patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response()),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=False, reason="cap exceeded", cost_data_degraded=False
            ),
        ),
    ):
        agent.call(work_item="ping", idempotency_key="kskip")

    skip_recs = [r for r in sink if r.get("status") == "skipped"]
    assert len(skip_recs) == 1, f"expected one pre-loop skip record, got {sink}"
    assert skip_recs[0].get("idempotency_key") == "kskip", (
        "pre-loop cost-skip record MUST carry idempotency_key (W6)"
    )
    # Pre-loop skip is BEFORE begin() -> no lease claimed, no commit.
    assert backend.begin_calls == [], "cost-skip MUST NOT begin() a lease"
    assert backend.commit_calls == [], "cost-skip MUST NOT commit the key"


def test_preloop_cost_skip_unkeyed_no_idempotency_key_negative(tmp_path):
    """NEGATIVE CONTROL for FIX 3: an UNKEYED pre-loop cost-skip writes NO
    idempotency_key field on the skip record."""
    agent = _make_call_agent(
        _build_agent_root_full, idempotency_backend=None, tmp_path=tmp_path
    )
    sink: list = []

    def fake_log(record: dict) -> None:
        sink.append(dict(record))

    with (
        patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response()),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=False, reason="cap exceeded", cost_data_degraded=False
            ),
        ),
    ):
        agent.call(work_item="ping")  # no idempotency_key

    skip_recs = [r for r in sink if r.get("status") == "skipped"]
    assert len(skip_recs) == 1
    assert "idempotency_key" not in skip_recs[0], (
        "unkeyed cost-skip MUST NOT tag idempotency_key"
    )


def test_midloop_cost_skip_record_carries_idempotency_key_and_no_commit(tmp_path):
    """FIX 3 / W6 (mid-loop variant): a keyed run that passes the first cost gate,
    runs one tool iteration, then hits the cap mid-loop tags idempotency_key on
    the mid-loop skip record AND leaves the ledger uncommitted (the lease is
    released by the finally -> a retry re-runs).

    NEGATIVE CONTROL: strip the `_mid_loop_skip['idempotency_key'] = ...` line and
    the idempotency_key assertion goes RED."""
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.idempotency.types import FRESH
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    backend = _LedgerSpyBackend()
    agents_root = _build_agent_root_full(tmp_path, "midbot")

    # Register a custom tool so the first LLM response can return a tool_use that
    # forces iteration_count > 1 (mirrors test_serve_agent_layer mid-loop test).
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="noop_tool",
            description="no-op for testing",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda _: "done",
        )
    )

    agent = AtomicAgent(
        name="midbot",
        trigger="http",
        agents_root=agents_root,
        tools=registry,
        idempotency_backend=backend,
        lock_backend=_FakeLockBackend(),
        log_backend=_FakeLogBackend(),
    )

    # First cost check (allow), second (mid-loop) deny.
    guardrails_call_count = 0

    def _cost_side_effect(*a, **k):
        nonlocal guardrails_call_count
        guardrails_call_count += 1
        if guardrails_call_count == 1:
            return MagicMock(allow=True, action="ok", cost_data_degraded=False)
        return MagicMock(allow=False, reason="mid-loop cap", cost_data_degraded=False)

    tool_use_response = MagicMock()
    tool_use_response.text = ""
    tool_use_response.tool_uses = [{"name": "noop_tool", "id": "tu_1", "input": {}}]
    tool_use_response.input_tokens = 100
    tool_use_response.output_tokens = 50
    tool_use_response.cache_hit_tokens = 0
    tool_use_response.cache_miss_tokens = 0
    tool_use_response.raw = {}

    sink: list = []

    def fake_log(record: dict) -> None:
        sink.append(dict(record))

    with (
        patch("atomic_agents._llm.call_llm", return_value=tool_use_response),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(agent, "_check_cost_guardrails", side_effect=_cost_side_effect),
    ):
        agent.load()
        agent.call(work_item="ping", idempotency_key="kmid")

    skip_recs = [r for r in sink if r.get("status") == "skipped"]
    assert skip_recs, (
        f"expected a mid-loop skip record, got statuses: {[r.get('status') for r in sink]}"
    )
    assert skip_recs[-1].get("idempotency_key") == "kmid", (
        "mid-loop cost-skip record MUST carry idempotency_key (W6)"
    )
    # begin() ran (FRESH claimed) but commit() must NOT have run; the finally
    # released the lease so a retry re-runs.
    assert backend.begin_calls == ["kmid"], "mid-loop path runs after begin()"
    assert backend.commit_calls == [], "mid-loop cost-skip MUST NOT commit the key"
    assert backend.release_calls == ["kmid"], (
        "mid-loop cost-skip MUST release the lease (retry re-runs)"
    )
    assert backend.lookup("kmid").state == FRESH


# ══════════════════════════════════════════════════════════════════════
# FIX 4 — cron_tick_key rejects colon / path-separator collisions
# ══════════════════════════════════════════════════════════════════════


def test_cron_tick_key_rejects_colon_in_agent_name():
    """FIX 4: a colon in agent_name raises ValueError (it would corrupt the
    colon-delimited key)."""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="colon|':'|delimit"):
        cron_tick_key("a:b", "sched", t, "hour")


def test_cron_tick_key_rejects_colon_in_schedule_name():
    """FIX 4: a colon in schedule_name raises ValueError."""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="colon|':'|delimit"):
        cron_tick_key("agent", "s:x", t, "hour")


def test_cron_tick_key_colon_collision_rejected_negative_control():
    """FIX 4 NEGATIVE CONTROL: ('a:b','c') and ('a','b:c') would collide into the
    SAME key if colons were NOT rejected. Both MUST raise. (Strip the rejection
    and both calls would succeed, producing identical keys — a false-dedup. This
    test pins that the rejection prevents the collision.)"""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        cron_tick_key("a:b", "c", t, "hour")
    with pytest.raises(ValueError):
        cron_tick_key("a", "b:c", t, "hour")


def test_cron_tick_key_rejects_path_separators():
    """FIX 4: '/' and '\\' in either component raise ValueError."""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        cron_tick_key("a/b", "sched", t, "hour")
    with pytest.raises(ValueError):
        cron_tick_key("agent", "s\\x", t, "hour")


def test_cron_tick_key_rejects_control_chars():
    """FIX 4: a control character (ord < 32) in a component raises ValueError."""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        cron_tick_key("a\x00b", "sched", t, "hour")


def test_cron_tick_key_clean_names_still_work_negative():
    """NEGATIVE CONTROL for FIX 4: clean names (no colon/separator) still produce
    a valid key — the rejection does not over-reject legitimate names."""
    from atomic_agents.idempotency import cron_tick_key

    t = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    key = cron_tick_key("my-agent", "daily_brief", t, "hour")
    assert key.startswith("my-agent:daily_brief:")


# ══════════════════════════════════════════════════════════════════════
# FIX 5 — begin() runs AFTER the cost gate (no lease for a refused run)
# ══════════════════════════════════════════════════════════════════════


def test_begin_not_called_when_cost_gate_refuses(tmp_path):
    """FIX 5: when _check_cost_guardrails returns allow=False, begin() is NEVER
    called (no lease reserved for a refused run) and release_lease() is never
    called; the returned Response is the cost-skipped one.

    This pins the W1 ordering invariant (begin AFTER the cost gate): if begin()
    were moved BEFORE the cost gate, begin_calls would be non-empty -> RED."""
    backend = _LedgerSpyBackend()
    agent = _make_call_agent(
        _build_agent_root_full,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="http",
    )

    with (
        patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response()),
        patch.object(agent, "_log"),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are CallBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are CallBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=False, reason="cap exceeded", cost_data_degraded=False
            ),
        ),
    ):
        resp = agent.call(work_item="ping", idempotency_key="krefused")

    assert backend.begin_calls == [], (
        "begin() MUST NOT run for a cost-refused call (W1: begin after cost gate)"
    )
    assert backend.release_lease.__name__ or True  # backend has release_lease
    assert backend.release_calls == [], (
        "no lease reserved -> release_lease MUST NOT be called"
    )
    assert resp.skipped is True, "a cost-refused call returns the skipped Response"


def test_begin_called_when_cost_gate_allows_negative(tmp_path):
    """NEGATIVE CONTROL for FIX 5: with allow=True, the FRESH path DOES call
    begin() — proving begin() is reachable and the refused-path assertion above is
    not vacuously true."""
    backend = _LedgerSpyBackend()
    agent = _make_call_agent(
        _build_agent_root_full,
        idempotency_backend=backend,
        tmp_path=tmp_path,
        trigger="http",
    )
    _run_call(agent, idempotency_key="kallow")
    assert backend.begin_calls == ["kallow"], "allow=True FRESH path MUST call begin()"


# ══════════════════════════════════════════════════════════════════════
# FIX 6 — cron_tick_key tz-aware guard + minute/week buckets
# ══════════════════════════════════════════════════════════════════════


def test_cron_tick_key_naive_datetime_raises():
    """FIX 6: a naive datetime (no tzinfo) raises ValueError with a tz-aware msg."""
    from atomic_agents.idempotency import cron_tick_key

    naive = datetime(2026, 6, 16, 10, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware|naive|tzinfo|timezone"):
        cron_tick_key("agent", "sched", naive, "hour")


def test_cron_tick_key_tz_aware_does_not_raise_negative():
    """NEGATIVE CONTROL for FIX 6: the SAME call WITH timezone.utc does NOT raise."""
    from atomic_agents.idempotency import cron_tick_key

    aware = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
    key = cron_tick_key("agent", "sched", aware, "hour")
    assert key.startswith("agent:sched:")


def test_cron_tick_key_minute_bucket_same_and_adjacent():
    """FIX 6: 'minute' granularity — same minute collides, adjacent minute differs."""
    from atomic_agents.idempotency import cron_tick_key

    t1 = datetime(2026, 6, 16, 10, 30, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 16, 10, 30, 59, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 16, 10, 31, 0, tzinfo=timezone.utc)
    assert cron_tick_key("a", "s", t1, "minute") == cron_tick_key(
        "a", "s", t2, "minute"
    )
    assert cron_tick_key("a", "s", t1, "minute") != cron_tick_key(
        "a", "s", t3, "minute"
    )


def test_cron_tick_key_week_bucket_same_and_adjacent():
    """FIX 6: 'week' granularity — same epoch-anchored week collides, adjacent
    week differs. (Epoch anchor: 1970-01-01 is a Thursday, so weeks run
    Thursday->Wednesday; dedup is unaffected.)"""
    from atomic_agents.idempotency import cron_tick_key

    # 604800 s = 1 week. Pick two times within the same week bucket and one in
    # the next bucket.
    base = datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)
    same_week = base + __import__("datetime").timedelta(days=2)
    next_week = base + __import__("datetime").timedelta(days=8)
    k_base = cron_tick_key("a", "s", base, "week")
    # same_week may or may not be in the same bucket depending on the anchor;
    # assert against a time KNOWN to be in the same bucket: base + 1 hour.
    same_bucket = base + __import__("datetime").timedelta(hours=1)
    assert cron_tick_key("a", "s", same_bucket, "week") == k_base, (
        "two times one hour apart MUST be in the same week bucket"
    )
    assert cron_tick_key("a", "s", next_week, "week") != k_base, (
        "a time 8 days later MUST be in a different week bucket"
    )
    # Sanity: the epoch anchor floors to a multiple of 604800.
    bucket_epoch = int(k_base.rsplit(":", 1)[1])
    assert bucket_epoch % 604800 == 0


# ══════════════════════════════════════════════════════════════════════
# FIX 8 — doctor probes release_lease (MUST 13)
# ══════════════════════════════════════════════════════════════════════


def test_doctor_idempotency_probes_release_lease(tmp_path):
    """FIX 8: check_idempotency_backend exercises the release_lease leg — a
    backend whose release_lease() does NOT actually release (so begin() after it
    returns IN_FLIGHT, not FRESH) FAILs the doctor check.

    NEGATIVE CONTROL is the no-op-release backend below: it passes begin/commit/
    lookup but FAILs because release_lease is a no-op."""
    from atomic_agents.doctor import check_idempotency_backend

    # A healthy real filesystem backend PASSes (release_lease works).
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    result = check_idempotency_backend(agent_dir)
    assert result.status in ("pass", "warn"), (
        f"healthy backend should PASS/WARN, got {result.status}: {result.message}"
    )


def test_doctor_idempotency_fails_when_release_lease_is_noop(tmp_path, monkeypatch):
    """FIX 8 NEGATIVE CONTROL: a backend whose release_lease() is a no-op (lease
    NOT released) FAILs the doctor check — proving the release_lease leg is
    load-bearing. Strip the release_lease probe from doctor and this goes from
    FAIL back to PASS (the bug would be undetected)."""
    from atomic_agents import doctor as _doctor
    from atomic_agents.idempotency.filesystem import FilesystemDedupLedger

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    class _NoReleaseBackend(FilesystemDedupLedger):
        def release_lease(self, key: str) -> None:
            # Intentionally do NOT release — simulates a buggy backend.
            return None

    backend = _NoReleaseBackend(agent_dir)
    monkeypatch.setattr(
        _doctor,
        "get_default_idempotency_backend",
        lambda root: backend,
        raising=False,
    )
    # get_default_idempotency_backend is imported INSIDE the function from
    # .idempotency; patch there too.
    import atomic_agents.idempotency as _idemp

    monkeypatch.setattr(
        _idemp, "get_default_idempotency_backend", lambda root: backend, raising=False
    )

    result = _doctor.check_idempotency_backend(agent_dir)
    assert result.status == "fail", (
        f"a no-op release_lease MUST FAIL the doctor probe, got {result.status}: "
        f"{result.message}"
    )
    assert "release" in result.message.lower()

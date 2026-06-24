"""Tests for atomic_agents.dashboard.quality — aggregation layer."""

from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.dashboard.quality import (
    aggregate_quality,
    _load_eval_runs,
    _build_eval_trend,
    _date_ge,
)


def _write_eval_run(
    agents_root: Path, agent: str, when: date, records: list[dict]
) -> Path:
    runs_dir = agents_root / agent / "evals" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{when.isoformat()}.jsonl"
    lines = []
    for rec in records:
        rec.setdefault("ts", when.isoformat())
        rec.setdefault("test_id", "test-001")
        rec.setdefault("weighted_score", 4.0)
        rec.setdefault("hard_fails", [])
        rec.setdefault("scores", {"accuracy": 4, "clarity": 4})
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_load_eval_runs_basic(tmp_path):
    today = date.today()
    _write_eval_run(
        tmp_path,
        "alice",
        today,
        [
            {"weighted_score": 3.5, "test_id": "t1"},
            {"weighted_score": 4.0, "test_id": "t2"},
        ],
    )
    records = _load_eval_runs(tmp_path, "alice", today - timedelta(days=5), today)
    assert len(records) == 2
    assert records[0].test_id == "t1"
    assert records[0].weighted_score == 3.5


def test_load_eval_runs_date_filter(tmp_path):
    today = date.today()
    old_date = today - timedelta(days=100)
    _write_eval_run(tmp_path, "alice", today, [{"weighted_score": 4.0}])
    _write_eval_run(tmp_path, "alice", old_date, [{"weighted_score": 2.0}])

    # Only last 90 days
    cutoff = today - timedelta(days=90)
    records = _load_eval_runs(tmp_path, "alice", cutoff, today)
    assert len(records) == 1
    assert records[0].weighted_score == 4.0


def test_build_eval_trend(tmp_path):
    today = date.today()
    records = []
    cutoff_30d = today - timedelta(days=30)
    # 5 days of evals, slowly improving
    for i in range(5):
        d = today - timedelta(days=4 - i)
        records.append(
            type(
                "R",
                (),
                {
                    "ts": d.isoformat(),
                    "weighted_score": 3.0 + i * 0.2,
                    "scores": {"accuracy": 3 + i * 0.1},
                },
            )()
        )

    # Build using raw dataclass-like objects
    from atomic_agents.dashboard.quality import EvalRunRecord

    rec_list = [
        EvalRunRecord(
            ts=(today - timedelta(days=4 - i)).isoformat(),
            agent="alice",
            test_id=f"t{i}",
            weighted_score=3.0 + i * 0.2,
            hard_fails=[],
            scores={"accuracy": float(3 + i)},
        )
        for i in range(5)
    ]

    trend = _build_eval_trend("alice", rec_list, cutoff_30d, today)
    assert trend.agent == "alice"
    assert trend.latest_score is not None
    assert trend.latest_score == pytest.approx(3.8, rel=1e-2)
    assert len(trend.daily_scores) == 5
    # Trend is upward
    assert trend.delta_30d is not None
    assert trend.delta_30d > 0


def test_hard_fail_detection(tmp_path):
    today = date.today()
    two_weeks_ago = today - timedelta(days=14)
    # alice must have log/ dir to be discovered
    (tmp_path / "alice" / "log").mkdir(parents=True)
    (tmp_path / "alice" / "model.md").write_text("# model\n")
    _write_eval_run(
        tmp_path,
        "alice",
        today,
        [
            {
                "weighted_score": 2.0,
                "hard_fails": ["response_too_short"],
                "test_id": "bad",
            },
        ],
    )
    _write_eval_run(
        tmp_path,
        "alice",
        two_weeks_ago,
        [
            {"weighted_score": 4.5, "hard_fails": [], "test_id": "good"},
        ],
    )

    data = aggregate_quality(tmp_path, today=today)
    assert len(data.hard_fails_30d) == 1
    assert data.hard_fails_30d[0].test_id == "bad"
    assert "response_too_short" in data.hard_fails_30d[0].hard_fails


def test_tuning_proposal_listing(tmp_path):
    tuning_dir = tmp_path / "alice" / "evals" / "tuning_reports"
    tuning_dir.mkdir(parents=True)
    (tuning_dir / "report_2026-05-01.md").write_text("# tuning proposal")
    (tuning_dir / "report_2026-04-15.md").write_text("# older proposal")
    # Ensure alice is discoverable
    (tmp_path / "alice" / "log").mkdir(parents=True)
    (tmp_path / "alice" / "model.md").write_text("# model\n")

    today = date.today()
    data = aggregate_quality(tmp_path, today=today)
    assert len(data.tuning_proposals) == 2
    # Newest first (by mtime — files just written so both are recent)
    assert all(tp.agent == "alice" for tp in data.tuning_proposals)


def test_empty_agents(tmp_path):
    today = date.today()
    data = aggregate_quality(tmp_path, today=today)
    assert data.eval_trends == []
    assert data.hard_fails_30d == []
    assert data.tuning_proposals == []


def test_date_ge_helper():
    d = date(2026, 5, 1)
    assert _date_ge("2026-05-01", d) is True
    assert _date_ge("2026-05-02T10:00:00Z", d) is True
    assert _date_ge("2026-04-30", d) is False
    assert _date_ge("invalid", d) is False


def test_eval_trend_no_data_returns_none_fields(tmp_path):
    from atomic_agents.dashboard.quality import EvalRunRecord

    # Single data point — can't compute 30d delta
    rec = EvalRunRecord(
        ts=date.today().isoformat(),
        agent="alice",
        test_id="t1",
        weighted_score=4.0,
        hard_fails=[],
        scores={"accuracy": 4.0},
    )
    trend = _build_eval_trend(
        "alice", [rec], date.today() - timedelta(days=30), date.today()
    )
    assert trend.latest_score == pytest.approx(4.0)
    assert trend.delta_30d is None  # only 1 data point


def test_count_provenance_degrades_to_zero_on_read_error(tmp_path, monkeypatch):
    """_count_provenance degrades to (0, 0) on LogBackendReadError.

    spec/22 read-failure addendum (#497): the quality dashboard is a reporting
    surface, not a control gate — an unrecoverable blind read returns (0, 0)
    rather than crashing the render. (Empty/absent already returns (0, 0).)
    """
    from unittest.mock import MagicMock

    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError
    from atomic_agents.dashboard.quality import _count_provenance

    mock_backend = MagicMock()
    mock_backend.query.side_effect = LogBackendReadError("corrupt log")
    monkeypatch.setattr(logs_mod, "get_default_log_backend", lambda root: mock_backend)

    today = date.today()
    result = _count_provenance(tmp_path, "alice", today, today)
    assert result == (0, 0)
    # False-green guard: prove the backend was consulted and the exception
    # path (not the absent-dir (0, 0) path) was exercised.
    assert mock_backend.query.called

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


def test_quality_html_renders_score_as_percentage(tmp_path):
    """FIX #690: quality.html renders eval scores as integer percentages, not raw decimals.

    weighted_score=4.0 (1-5 rubric scale) → '75%' in the trend table.
    weighted_score=2.0 (1-5)              → '25%'.
    Neither raw decimal ('4.00', '2.00') nor an absurd value ('400%') must appear.
    """
    from atomic_agents.dashboard.quality import aggregate_quality, render_quality

    today = date.today()
    # Write two agents with eval data so the trend table is non-empty.
    for agent, ws in [("alice", 4.0), ("bob", 2.0)]:
        runs_dir = tmp_path / agent / "evals" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (tmp_path / agent / "log").mkdir(parents=True)
        (tmp_path / agent / "model.md").write_text("# model\n")
        rec = {
            "ts": today.isoformat(),
            "test_id": "t1",
            "weighted_score": ws,
            "hard_fails": [],
            "scores": {"accuracy": ws},
        }
        (runs_dir / f"{today.isoformat()}.jsonl").write_text(json.dumps(rec) + "\n")

    data = aggregate_quality(tmp_path, today=today)
    out_path = render_quality(tmp_path, data)
    html_content = out_path.read_text(encoding="utf-8")

    # weighted_score=4.0 → (4.0-1)/4*100 = 75%
    assert "75%" in html_content, (
        "quality.html must render weighted_score=4.0 (1-5 scale) as '75%', "
        "not as raw '4.00' or '400%'. FIX #690."
    )
    # weighted_score=2.0 → (2.0-1)/4*100 = 25%
    assert "25%" in html_content, (
        "quality.html must render weighted_score=2.0 (1-5 scale) as '25%'. FIX #690."
    )
    # Raw decimals like '4.00' must not appear in score cells.
    assert ">4.00<" not in html_content, (
        "quality.html must not render the raw float '4.00' in a score cell. FIX #690."
    )
    assert ">2.00<" not in html_content, (
        "quality.html must not render the raw float '2.00' in a score cell. FIX #690."
    )
    # Sanity: no absurd percentages (>100%) from un-normalized 1-5 values.
    assert "400%" not in html_content, (
        "quality.html must not render '400%' — the 1-5 scale must be normalized. FIX #690."
    )


# ──────────────────────────────────────────────────────────────────
# FIX #690 round-2 — quality.html delta display (rubric scale)
#
# delta_30d is a raw 1-5 rubric-point difference.
# Correct display: delta / 4 * 100.
# Old broken display: abs(delta) * 100 (4× overstated for |delta| ≤ 1).


def test_quality_html_delta_rubric_scale(tmp_path):
    """FIX #690 r2: quality.html 30d delta uses rubric normalisation.

    Two data points for 'alice' within the 30d window: 2.0 → 3.0.
    delta_30d = +1.0 raw rubric points → +25% (not +100%).

    Both points must be within the 30d window so the delta is computed.
    The 30d window is [today-30d, today]; use 25d and 2d to stay inside it.
    """
    from atomic_agents.dashboard.quality import aggregate_quality, render_quality

    today = date.today()
    prior = (today - timedelta(days=25)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()

    runs_dir = tmp_path / "alice" / "evals" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "alice" / "log").mkdir(parents=True)
    (tmp_path / "alice" / "model.md").write_text("# model\n")

    # Prior: weighted_score=2.0; recent: weighted_score=3.0 → delta = +1.0.
    for ts, ws in [(prior, 2.0), (recent, 3.0)]:
        rec = {
            "ts": f"{ts}T12:00:00+00:00",
            "test_id": "t1",
            "weighted_score": ws,
            "hard_fails": [],
            "scores": {},
        }
        day = ts[:10]
        path = runs_dir / f"{day}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    data = aggregate_quality(tmp_path, today=today)
    out_path = render_quality(tmp_path, data)
    html_content = out_path.read_text(encoding="utf-8")

    # delta_30d = +1.0 → +1.0/4*100 = +25% (rubric-correct)
    assert "+25%" in html_content, (
        "FIX #690 r2: delta_30d=+1.0 (rubric scale) must display as '+25%' "
        "in quality.html, not '+100%'. Old code: abs(1.0)*100 = 100."
    )
    assert "+100%" not in html_content, (
        "FIX #690 r2: '+100%' must NOT appear — that is the 4× overstated old value. "
        "Correct display for +1.0 rubric delta is '+25%'."
    )


def test_quality_html_delta_small_rubric(tmp_path):
    """FIX #690 r2: small rubric deltas are not inflated 4× by the old heuristic.

    delta_30d = +0.5 raw rubric → +12% (0.5/4*100 = 12.5, banker's rounding → 12).
    Old value-auto-detect: abs(0.5) <= 1.0 → treated as legacy → +50%. Wrong.

    Both data points must be within the 30d window (use 25d and 2d).
    """
    from atomic_agents.dashboard.quality import aggregate_quality, render_quality

    today = date.today()
    prior = (today - timedelta(days=25)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()

    runs_dir = tmp_path / "alice" / "evals" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "alice" / "log").mkdir(parents=True)
    (tmp_path / "alice" / "model.md").write_text("# model\n")

    # Prior: 3.0; recent: 3.5 → delta = +0.5.
    for ts, ws in [(prior, 3.0), (recent, 3.5)]:
        rec = {
            "ts": f"{ts}T12:00:00+00:00",
            "test_id": "t1",
            "weighted_score": ws,
            "hard_fails": [],
            "scores": {},
        }
        day = ts[:10]
        path = runs_dir / f"{day}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    data = aggregate_quality(tmp_path, today=today)
    out_path = render_quality(tmp_path, data)
    html_content = out_path.read_text(encoding="utf-8")

    # +0.5/4*100 = 12.5 → Python banker's rounding → 12, so the rendered delta is "+12%".
    assert "+12%" in html_content, (
        "FIX #689: '+12%' must appear — 0.5 rubric delta × 100/4 = 12.5, "
        "banker's rounding → 12. Got something else."
    )
    assert "+50%" not in html_content, (
        "FIX #690 r2: '+50%' must NOT appear — that is the old value-auto-detect "
        "treating +0.5 as a legacy 0-1 delta (×100). Correct is +12%."
    )

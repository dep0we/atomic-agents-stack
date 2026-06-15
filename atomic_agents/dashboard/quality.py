"""Quality Trends tab — aggregation + render.

Answers "are my agents getting better or worse?" — weekly review.

Reads evals/runs/*.jsonl, evals/tuning_reports/*.md, and log JSONL
(for helper provenance).  Pure Python, no LLM calls.
"""

from __future__ import annotations
import html
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .costs import discover_agents, load_runs
from ._shared import (
    page_shell,
    sparkline,
    truncate,
)
from .._io import atomic_write


# ──────────────────────────────────────────────────────────────────
# Data structures

@dataclass
class EvalRunRecord:
    """One row from evals/runs/<YYYY-MM-DD>.jsonl."""
    ts: str
    agent: str
    test_id: str
    weighted_score: float
    hard_fails: list[str]
    scores: dict[str, float]             # {dimension: score}


@dataclass
class AgentEvalTrend:
    agent: str
    daily_scores: list[tuple[str, float]]   # [(date_iso, weighted_score), ...] sorted asc
    latest_score: float | None
    delta_30d: float | None                 # latest - score 30 days ago
    per_dimension_latest: dict[str, float]  # {dimension: latest_score}
    per_dimension_delta: dict[str, float | None]  # {dimension: 30d delta}


@dataclass
class HardFailEntry:
    ts: str
    agent: str
    test_id: str
    hard_fails: list[str]
    weighted_score: float


@dataclass
class TuningProposal:
    agent: str
    filename: str
    mtime: float
    mtime_iso: str
    rel_path: str  # relative path for file:// link


@dataclass
class ProvenanceHealth:
    agent: str
    calls_total: int
    calls_preserved: int
    pct_preserved: float


@dataclass
class QualityData:
    generated_at: datetime
    eval_trends: list[AgentEvalTrend]
    hard_fails_30d: list[HardFailEntry]
    tuning_proposals: list[TuningProposal]
    provenance_health: list[ProvenanceHealth]


# ──────────────────────────────────────────────────────────────────
# Aggregation

def aggregate_quality(
    agents_root: Path,
    today: date | None = None,
    now: datetime | None = None,
    lookback_days: int = 90,
) -> QualityData:
    """Build QualityData for the Quality Trends tab."""
    today = today or date.today()
    now = now or datetime.now(tz=timezone.utc)
    cutoff_90d = today - timedelta(days=lookback_days)
    cutoff_30d = today - timedelta(days=30)

    agent_names = discover_agents(agents_root)

    eval_trends: list[AgentEvalTrend] = []
    all_hard_fails: list[HardFailEntry] = []
    tuning_proposals: list[TuningProposal] = []
    provenance_health: list[ProvenanceHealth] = []

    for agent in agent_names:
        # ── Eval runs
        eval_records = _load_eval_runs(agents_root, agent, cutoff_90d, today)
        if eval_records:
            trend = _build_eval_trend(agent, eval_records, cutoff_30d, today)
            eval_trends.append(trend)
            # Hard fails (30d)
            for rec in eval_records:
                if rec.hard_fails and _date_ge(rec.ts, cutoff_30d):
                    all_hard_fails.append(HardFailEntry(
                        ts=rec.ts,
                        agent=agent,
                        test_id=rec.test_id,
                        hard_fails=rec.hard_fails,
                        weighted_score=rec.weighted_score,
                    ))

        # ── Tuning proposals
        tuning_dir = agents_root / agent / "evals" / "tuning_reports"
        if tuning_dir.exists():
            for md_path in sorted(tuning_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    mtime = md_path.stat().st_mtime
                except OSError:
                    continue
                mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                tuning_proposals.append(TuningProposal(
                    agent=agent,
                    filename=md_path.name,
                    mtime=mtime,
                    mtime_iso=mtime_dt.strftime("%Y-%m-%d %H:%M"),
                    rel_path=str(md_path.resolve()),
                ))

        # ── Helper provenance health (last 30 days)
        runs_30d = load_runs(agents_root, agent, cutoff_30d, today)
        helper_runs = [r for r in runs_30d if r.trigger == "helper"]
        if helper_runs:
            # We check if a run record has provenance_preserved — this field is
            # optional in the log. Count how many have it set to True.
            # Since RunRecord doesn't carry this field, we reload raw lines.
            preserved, total = _count_provenance(agents_root, agent, cutoff_30d, today)
            provenance_health.append(ProvenanceHealth(
                agent=agent,
                calls_total=total,
                calls_preserved=preserved,
                pct_preserved=preserved / total * 100 if total else 0.0,
            ))

    # Sort hard fails newest first
    all_hard_fails.sort(key=lambda x: x.ts, reverse=True)
    # Sort tuning proposals newest first
    tuning_proposals.sort(key=lambda x: x.mtime, reverse=True)

    return QualityData(
        generated_at=now,
        eval_trends=eval_trends,
        hard_fails_30d=all_hard_fails,
        tuning_proposals=tuning_proposals,
        provenance_health=provenance_health,
    )


def _load_eval_runs(
    agents_root: Path,
    agent: str,
    since: date,
    until: date,
) -> list[EvalRunRecord]:
    """Load evals/runs/<YYYY-MM-DD>.jsonl files for one agent in [since, until]."""
    runs_dir = agents_root / agent / "evals" / "runs"
    if not runs_dir.exists():
        return []
    records: list[EvalRunRecord] = []
    for path in sorted(runs_dir.glob("*.jsonl")):
        # Filename convention: YYYY-MM-DD.jsonl
        try:
            stem_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if stem_date < since or stem_date > until:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts") or path.stem
            test_id = rec.get("test_id") or rec.get("case_id") or "?"
            weighted = float(rec.get("weighted_score", 0.0) or 0.0)
            hard_fails = list(rec.get("hard_fails") or [])
            scores_raw = rec.get("scores") or {}
            scores = {k: float(v) for k, v in scores_raw.items() if isinstance(v, (int, float))}
            records.append(EvalRunRecord(
                ts=str(ts),
                agent=agent,
                test_id=str(test_id),
                weighted_score=weighted,
                hard_fails=hard_fails,
                scores=scores,
            ))
    return records


def _build_eval_trend(
    agent: str,
    records: list[EvalRunRecord],
    cutoff_30d: date,
    today: date,
) -> AgentEvalTrend:
    """Compute trend data from a list of eval records for one agent."""
    # Group by date — average weighted_score per day
    by_date: dict[str, list[float]] = {}
    for rec in records:
        day = rec.ts[:10] if len(rec.ts) >= 10 else rec.ts
        by_date.setdefault(day, []).append(rec.weighted_score)

    daily_scores = sorted(
        [(d, sum(v) / len(v)) for d, v in by_date.items()],
        key=lambda x: x[0],
    )

    latest_score: float | None = daily_scores[-1][1] if daily_scores else None

    # 30d delta: compare latest to oldest score within past 30d
    scores_30d = [s for d, s in daily_scores if d >= cutoff_30d.isoformat()]
    if len(scores_30d) >= 2 and latest_score is not None:
        delta_30d = latest_score - scores_30d[0]
    else:
        delta_30d = None

    # Per-dimension: latest score and 30d delta
    dim_by_date: dict[str, dict[str, list[float]]] = {}  # dim → {date → [scores]}
    for rec in records:
        day = rec.ts[:10] if len(rec.ts) >= 10 else rec.ts
        for dim, score in rec.scores.items():
            dim_by_date.setdefault(dim, {}).setdefault(day, []).append(score)

    per_dim_latest: dict[str, float] = {}
    per_dim_delta: dict[str, float | None] = {}
    for dim, date_map in dim_by_date.items():
        sorted_days = sorted(date_map.keys())
        if sorted_days:
            latest_dim = sum(date_map[sorted_days[-1]]) / len(date_map[sorted_days[-1]])
            per_dim_latest[dim] = round(latest_dim, 2)
            # 30d delta
            days_30d_in_dim = [d for d in sorted_days if d >= cutoff_30d.isoformat()]
            if len(days_30d_in_dim) >= 2:
                first_score = sum(date_map[days_30d_in_dim[0]]) / len(date_map[days_30d_in_dim[0]])
                per_dim_delta[dim] = round(latest_dim - first_score, 2)
            else:
                per_dim_delta[dim] = None

    return AgentEvalTrend(
        agent=agent,
        daily_scores=daily_scores,
        latest_score=round(latest_score, 2) if latest_score is not None else None,
        delta_30d=round(delta_30d, 2) if delta_30d is not None else None,
        per_dimension_latest=per_dim_latest,
        per_dimension_delta=per_dim_delta,
    )


def _date_ge(ts_str: str, cutoff: date) -> bool:
    """Return True if ts_str (ISO date/datetime) >= cutoff."""
    try:
        d = date.fromisoformat(ts_str[:10])
        return d >= cutoff
    except (ValueError, TypeError):
        return False


def _count_provenance(
    agents_root: Path,
    agent: str,
    since: date,
    until: date,
) -> tuple[int, int]:
    """Count helper runs with provenance_preserved=True vs. total helper runs.

    Per #61 PR 2: routes through ``LogBackend.query()`` instead of
    walking the filesystem directly. ``provenance_preserved`` lives
    in ``record.extra`` (primitive-specific key); we read it via
    ``RunRecord.extra.get`` rather than parsing raw JSONL.

    Helper-record discrimination is done IN-MEMORY (not via
    ``LogQuery(primitive="helper")``) because legacy on-disk records
    written before PR 2 do not have a ``primitive`` field — they would
    be re-materialized via ``RunRecord.from_dict`` with the default
    ``PRIMITIVE_OTHER`` and silently filtered out by the backend's
    predicate evaluator BEFORE the in-method belt-and-suspenders check
    could run. Step 11 adversarial P0 #2 caught this. Querying the
    full window and filtering in-Python preserves backward compat for
    every legacy helper record.

    Returns (preserved_count, total_count). On an unrecoverable
    ``LogBackendReadError`` (corruption / I/O / lost connection) this
    degrades to ``(0, 0)`` rather than crashing — the quality dashboard
    is a reporting surface, not a control gate (spec/09 §"Cost-read
    fail-closed posture"; spec/22 read-failure addendum). An
    empty/absent log already returns ``(0, 0)`` without raising.
    """
    from datetime import datetime, time as dt_time
    from ..logs import LogQuery, get_default_log_backend, LogBackendReadError

    backend = get_default_log_backend(agents_root / agent)
    since_dt = datetime.combine(since, dt_time.min).astimezone()
    until_dt = datetime.combine(until, dt_time.max).astimezone()

    preserved = 0
    total = 0
    # NO primitive filter in the LogQuery — see docstring rationale.
    # agent_name filter prevents shared-backend cross-agent record mix
    # (Step 11 P0 #1).
    #
    # spec/22 read-failure addendum (issue #497): a blind read degrades to
    # (0, 0) rather than crashing the dashboard render.
    try:
        records = backend.query(LogQuery(
            since=since_dt, until=until_dt, agent_name=agent,
        ))
    except LogBackendReadError:
        return 0, 0
    for rec in records:
        # Helper records are identified by EITHER primitive (post-PR-2)
        # OR trigger (legacy, pre-PR-2). Belt-and-suspenders.
        if rec.primitive != "helper" and rec.trigger != "helper":
            continue
        total += 1
        if rec.extra.get("provenance_preserved"):
            preserved += 1
    return preserved, total


# ──────────────────────────────────────────────────────────────────
# Rendering

def render_quality(agents_root: Path, data: QualityData) -> Path:
    """Write _dashboard/quality.html and return the path."""
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    has_goals = any(
        (agents_root / agent / "goal.md").exists()
        for agent in discover_agents(agents_root)
    )
    html_content = _render_quality_template(data, has_goals=has_goals)
    out_path = out_dir / "quality.html"
    atomic_write(out_path, html_content)
    return out_path


def _render_quality_template(data: QualityData, has_goals: bool = True) -> str:
    # ── Eval score trend (sparkline per agent)
    if data.eval_trends:
        trend_rows = []
        for t in data.eval_trends:
            spark_values = [s for _, s in t.daily_scores]
            spark_html = f'<span class="sparkline">{sparkline(spark_values)}</span>' if spark_values else "—"
            latest_str = f"{t.latest_score:.2f}" if t.latest_score is not None else "—"
            delta = t.delta_30d
            if delta is None:
                delta_str = '<span class="muted">—</span>'
            elif delta > 0:
                delta_str = f'<span style="color: var(--good)">+{delta:.2f}</span>'
            elif delta < 0:
                delta_str = f'<span style="color: var(--error)">{delta:.2f}</span>'
            else:
                delta_str = '<span class="muted">0.00</span>'
            trend_rows.append(
                f'<tr>'
                f'<td>{html.escape(t.agent)}</td>'
                f'<td class="num">{latest_str}</td>'
                f'<td>{delta_str}</td>'
                f'<td class="num">{len(t.daily_scores)} days</td>'
                f'<td>{spark_html}</td>'
                f'</tr>'
            )
        eval_table = (
            '<table>'
            '<thead><tr><th>Agent</th><th class="right">Latest score</th>'
            '<th>30d delta</th><th>Days w/ evals</th><th>Trend (90d)</th></tr></thead>'
            f'<tbody>{"".join(trend_rows)}</tbody>'
            '</table>'
        )
    else:
        eval_table = '<p class="empty-note">No eval run data found in the last 90 days.</p>'

    # ── Per-dimension trend tables
    dim_panels = []
    for t in data.eval_trends:
        if not t.per_dimension_latest:
            continue
        rows = []
        for dim in sorted(t.per_dimension_latest.keys()):
            score = t.per_dimension_latest[dim]
            delta = t.per_dimension_delta.get(dim)
            if delta is None:
                delta_html = '<span class="muted">—</span>'
            elif delta > 0:
                delta_html = f'<span style="color: var(--good)">+{delta:.2f}</span>'
            elif delta < 0:
                delta_html = f'<span style="color: var(--error)">{delta:.2f}</span>'
            else:
                delta_html = '<span class="muted">0.00</span>'
            rows.append(
                f'<tr><td>{html.escape(dim)}</td>'
                f'<td class="right num">{score:.2f}</td>'
                f'<td>{delta_html}</td></tr>'
            )
        dim_table = (
            f'<p style="font-size:12px; color: var(--muted); margin-bottom: 8px">'
            f'{html.escape(t.agent)}</p>'
            '<table>'
            '<thead><tr><th>Dimension</th><th class="right">Latest</th>'
            '<th>30d delta</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
        dim_panels.append(f'<section class="panel">{dim_table}</section>')

    if dim_panels:
        dim_section_inner = "".join(dim_panels)
        dim_section = f'<div class="grid-2">{dim_section_inner}</div>'
    else:
        dim_section = '<section class="panel"><p class="empty-note">No dimension data found.</p></section>'

    # ── Hard fails (30d)
    if data.hard_fails_30d:
        rows = []
        for entry in data.hard_fails_30d:
            hf_str = ", ".join(html.escape(f) for f in entry.hard_fails)
            rows.append(
                f'<tr class="row-error">'
                f'<td class="num">{html.escape(entry.ts[:16])}</td>'
                f'<td>{html.escape(entry.agent)}</td>'
                f'<td>{html.escape(entry.test_id)}</td>'
                f'<td style="color: var(--error)">{hf_str}</td>'
                f'<td class="right num">{entry.weighted_score:.2f}</td>'
                f'</tr>'
            )
        hard_fail_table = (
            '<table>'
            '<thead><tr><th>When</th><th>Agent</th><th>Test ID</th>'
            '<th>Hard fails</th><th class="right">Score</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        hard_fail_table = '<p class="empty-note">No hard-fail occurrences in the last 30 days.</p>'

    # ── Tuning proposals
    if data.tuning_proposals:
        rows = []
        for tp in data.tuning_proposals:
            file_link = f'<a href="file://{html.escape(tp.rel_path)}" style="color: var(--accent)">review</a>'
            rows.append(
                f'<tr>'
                f'<td class="num">{html.escape(tp.mtime_iso)}</td>'
                f'<td>{html.escape(tp.agent)}</td>'
                f'<td>{html.escape(tp.filename)}</td>'
                f'<td>{file_link}</td>'
                f'</tr>'
            )
        tuning_table = (
            '<table>'
            '<thead><tr><th>Generated</th><th>Agent</th><th>Report</th>'
            '<th>Link</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        tuning_table = '<p class="empty-note">No tuning proposals found.</p>'

    # ── Helper provenance health
    if data.provenance_health:
        rows = []
        for ph in data.provenance_health:
            pct_color = "var(--good)" if ph.pct_preserved >= 80 else (
                "var(--warn)" if ph.pct_preserved >= 50 else "var(--error)"
            )
            rows.append(
                f'<tr>'
                f'<td>{html.escape(ph.agent)}</td>'
                f'<td class="right num">{ph.calls_total}</td>'
                f'<td class="right num">{ph.calls_preserved}</td>'
                f'<td class="right num" style="color: {pct_color}">{ph.pct_preserved:.1f}%</td>'
                f'</tr>'
            )
        prov_table = (
            '<table>'
            '<thead><tr><th>Agent</th><th class="right">Helper calls (30d)</th>'
            '<th class="right">Provenance preserved</th>'
            '<th class="right">% preserved</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        prov_table = '<p class="empty-note">No helper calls with provenance data in the last 30 days.</p>'

    gen_ts = data.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    body = f"""
<section class="panel">
  <h2>Eval score trend · last 90 days</h2>
  {eval_table}
</section>

<h2>Per-dimension breakdown</h2>
{dim_section}

<section class="panel">
  <h2>Hard-fail occurrences · last 30 days</h2>
  {hard_fail_table}
</section>

<section class="panel">
  <h2>Pending tuning proposals</h2>
  {tuning_table}
</section>

<section class="panel">
  <h2>Helper provenance health · last 30 days</h2>
  {prov_table}
</section>
"""

    return page_shell(
        title="Quality Trends",
        body=body,
        current_tab="quality",
        has_goals=has_goals,
        subtitle=f"as of {gen_ts}",
    )

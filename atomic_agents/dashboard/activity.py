"""Activity Pulse tab — aggregation + render.

Answers "what's happening with my fleet right now?" — daily check-in.

Reads log JSONL files, dreams manifests, and agent .lock files.
Pure Python, no LLM calls.
"""

from __future__ import annotations
import html
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .costs import discover_agents, load_runs, RunRecord
from ._shared import (
    CSS,
    page_shell,
    relative_time,
    truncate,
    status_pill,
)
from .._io import atomic_write
from ..memory.filesystem import FilesystemBackend


# ──────────────────────────────────────────────────────────────────
# Data structures

@dataclass
class ActivityHeadline:
    runs_24h: int
    runs_7d: int
    failures_24h: int
    agents_active_24h: int
    agents_total: int


@dataclass
class LockEntry:
    agent: str
    held_seconds: float
    is_stale: bool   # held > 5 minutes


@dataclass
class DreamEntry:
    ts: str
    agent: str
    dream_id: str
    status: str
    consolidations: int
    promotions: int
    marked_stale: int
    applied: bool


@dataclass
class ActivityData:
    generated_at: datetime
    headline: ActivityHeadline
    recent_runs: list[RunRecord]         # last 50, newest first
    recent_failures: list[RunRecord]     # failures in last 24h
    recent_tool_calls: list[RunRecord]   # last 50 with trigger==tool_call
    recent_delegations: list[RunRecord]  # last 50 with trigger==delegate
    lock_states: list[LockEntry]         # stale locks only (held > 5 min)
    recent_dreams: list[DreamEntry]      # last 20 dream manifests
    recent_captures: list[dict]          # last 50 memory file captures (by mtime)


# ──────────────────────────────────────────────────────────────────
# Aggregation

def aggregate_activity(
    agents_root: Path,
    now: datetime | None = None,
    max_recent: int = 50,
) -> ActivityData:
    """Build ActivityData for the Activity Pulse tab."""
    now = now or datetime.now(tz=timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    seven_days_ago = today - timedelta(days=7)

    agent_names = discover_agents(agents_root)

    all_runs_7d: list[RunRecord] = []
    for agent in agent_names:
        runs = load_runs(agents_root, agent, seven_days_ago, today)
        all_runs_7d.extend(runs)

    # Sort newest first
    all_runs_7d.sort(key=lambda r: r.ts, reverse=True)

    # Headline
    cutoff_24h = now - timedelta(hours=24)
    runs_24h = [r for r in all_runs_7d if _ts_aware(r.ts) >= cutoff_24h]
    failures_24h = [r for r in runs_24h if r.status not in ("ok",)]
    active_agents_24h = {r.agent for r in runs_24h}
    headline = ActivityHeadline(
        runs_24h=len(runs_24h),
        runs_7d=len(all_runs_7d),
        failures_24h=len(failures_24h),
        agents_active_24h=len(active_agents_24h),
        agents_total=len(agent_names),
    )

    # Last 50 runs
    recent_runs = all_runs_7d[:max_recent]

    # Recent failures (24h) — status != "ok" or trigger ends in "_error"
    recent_failures = [
        r for r in all_runs_7d
        if _ts_aware(r.ts) >= cutoff_24h
        and (r.status not in ("ok",) or r.trigger.endswith("_error"))
    ]

    # Recent tool calls (last 50)
    recent_tool_calls = [
        r for r in all_runs_7d if r.trigger == "tool_call"
    ][:max_recent]

    # Recent delegations (last 50)
    recent_delegations = [
        r for r in all_runs_7d if r.trigger == "delegate"
    ][:max_recent]

    # Lock states — check each agent's .lock file
    lock_states: list[LockEntry] = []
    for agent in agent_names:
        lock_path = agents_root / agent / ".lock"
        if lock_path.exists():
            try:
                mtime = lock_path.stat().st_mtime
                held_secs = time.time() - mtime
                is_stale = held_secs > 300  # 5 minutes
                if is_stale:
                    lock_states.append(LockEntry(
                        agent=agent,
                        held_seconds=held_secs,
                        is_stale=True,
                    ))
            except OSError:
                pass

    # Recent dreams — scan dreams/**/manifest.json
    recent_dreams = _scan_dreams(agents_root, agent_names, limit=20)

    # Recent captures — newest .md files in memory/ dirs by mtime
    recent_captures = _scan_recent_captures(agents_root, agent_names, limit=max_recent)

    return ActivityData(
        generated_at=now,
        headline=headline,
        recent_runs=recent_runs,
        recent_failures=recent_failures,
        recent_tool_calls=recent_tool_calls,
        recent_delegations=recent_delegations,
        lock_states=lock_states,
        recent_dreams=recent_dreams,
        recent_captures=recent_captures,
    )


def _ts_aware(ts: datetime) -> datetime:
    """Return ts with UTC tzinfo if naive."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _scan_dreams(
    agents_root: Path,
    agent_names: list[str],
    limit: int = 20,
) -> list[DreamEntry]:
    """Scan <agent>/dreams/*/manifest.json and return the most recent entries."""
    entries: list[tuple[str, DreamEntry]] = []  # (ts_str, entry)

    for agent in agent_names:
        dreams_dir = agents_root / agent / "dreams"
        if not dreams_dir.exists():
            continue
        for dream_dir in dreams_dir.iterdir():
            if not dream_dir.is_dir():
                continue
            manifest_path = dream_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            started_at = data.get("started_at", "")
            entry = DreamEntry(
                ts=started_at,
                agent=agent,
                dream_id=data.get("dream_id", dream_dir.name),
                status=data.get("status", "unknown"),
                consolidations=len(data.get("consolidated", [])),
                promotions=len(data.get("promoted", [])),
                marked_stale=len(data.get("marked_stale", [])),
                applied=bool(data.get("applied_at")),
            )
            entries.append((started_at, entry))

    # Sort by ts newest first
    entries.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in entries[:limit]]


def _scan_recent_captures(
    agents_root: Path,
    agent_names: list[str],
    limit: int = 50,
) -> list[dict]:
    """Return last N memory/*.md files sorted by last-mutation timestamp, newest first.

    Uses FilesystemBackend.last_mutation_at() (falls back to file mtime when no
    version history exists) — codex P2 #7 fix for TOCTOU-unreliable mtime sorting.
    """
    files: list[tuple[float, dict]] = []
    for agent in agent_names:
        agent_root = agents_root / agent
        memory_dir = agent_root / "memory"
        if not memory_dir.exists():
            continue
        backend = FilesystemBackend(agent_root, "memory")
        for md_path in memory_dir.glob("*.md"):
            if md_path.name == "INDEX.md":
                continue
            try:
                # Prefer last_mutation_at (version-aware); fall back to mtime
                mutation_dt = backend.last_mutation_at(md_path.name)
                if mutation_dt is None:
                    mtime = md_path.stat().st_mtime
                    mutation_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                sort_key = mutation_dt.timestamp()
            except OSError:
                continue
            files.append((sort_key, {
                "agent": agent,
                "filename": md_path.name,
                "mtime": sort_key,
                "mtime_dt": mutation_dt,
            }))
    files.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in files[:limit]]


# ──────────────────────────────────────────────────────────────────
# Rendering

def render_activity(agents_root: Path, data: ActivityData) -> Path:
    """Write _dashboard/activity.html and return the path."""
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    has_goals = any(
        (agents_root / agent / "goal.md").exists()
        for agent in discover_agents(agents_root)
    )
    html_content = _render_activity_template(data, has_goals=has_goals)
    out_path = out_dir / "activity.html"
    atomic_write(out_path, html_content)
    return out_path


def _render_activity_template(data: ActivityData, has_goals: bool = True) -> str:
    h = data.headline
    now = data.generated_at

    # ── Headline KPIs
    kpis = f"""
<section class="kpis">
  <div class="kpi">
    <div class="value">{h.runs_24h}</div>
    <div class="label">Runs last 24h</div>
  </div>
  <div class="kpi">
    <div class="value">{h.runs_7d}</div>
    <div class="label">Runs last 7d</div>
  </div>
  <div class="kpi">
    <div class="value" style="color: {'var(--error)' if h.failures_24h else 'var(--good)'}">{h.failures_24h}</div>
    <div class="label">Failures 24h</div>
  </div>
  <div class="kpi">
    <div class="value">{h.agents_active_24h} / {h.agents_total}</div>
    <div class="label">Agents active 24h</div>
  </div>
</section>
"""

    # ── Last 50 runs
    if data.recent_runs:
        rows = []
        for r in data.recent_runs:
            rel = relative_time(r.ts, now)
            ts_abs = r.ts.strftime("%b %d %H:%M")
            cost_str = f"${r.cost_usd:.4f}" if r.cost_usd else "—"
            dur_str = f"{r.latency_ms}ms" if r.latency_ms else "—"
            row_class = ""
            if r.status not in ("ok",):
                row_class = ' class="row-error"'
            rows.append(
                f'<tr{row_class}>'
                f'<td class="num" title="{ts_abs}">{html.escape(rel)}</td>'
                f'<td>{html.escape(r.agent)}</td>'
                f'<td>{html.escape(r.trigger)}</td>'
                f'<td>{status_pill(r.status)}</td>'
                f'<td>{html.escape(truncate(r.summary, 80))}</td>'
                f'<td class="right num">{dur_str}</td>'
                f'<td class="right num">{cost_str}</td>'
                f'</tr>'
            )
        runs_table = (
            '<table>'
            '<thead><tr><th>When</th><th>Agent</th><th>Trigger</th>'
            '<th>Status</th><th>Summary</th>'
            '<th class="right">Duration</th><th class="right">Cost</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        runs_table = '<p class="empty-note">No runs in the last 7 days.</p>'

    # ── Recent failures
    if data.recent_failures:
        rows = []
        for r in data.recent_failures:
            rel = relative_time(r.ts, now)
            rows.append(
                f'<tr class="row-error">'
                f'<td class="num">{html.escape(rel)}</td>'
                f'<td>{html.escape(r.agent)}</td>'
                f'<td>{html.escape(r.trigger)}</td>'
                f'<td>{status_pill(r.status)}</td>'
                f'<td>{html.escape(truncate(r.summary, 80))}</td>'
                f'</tr>'
            )
        failures_table = (
            '<table>'
            '<thead><tr><th>When</th><th>Agent</th><th>Trigger</th>'
            '<th>Status</th><th>Summary</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        failures_table = '<p class="empty-note">No failures in the last 24 hours.</p>'

    # ── Recent tool calls
    if data.recent_tool_calls:
        rows = []
        for r in data.recent_tool_calls:
            rel = relative_time(r.ts, now)
            rows.append(
                f'<tr>'
                f'<td class="num">{html.escape(rel)}</td>'
                f'<td>{html.escape(r.agent)}</td>'
                f'<td>{status_pill(r.status)}</td>'
                f'<td>{html.escape(truncate(r.summary, 80))}</td>'
                f'</tr>'
            )
        tool_calls_table = (
            '<table>'
            '<thead><tr><th>When</th><th>Agent</th><th>Status</th><th>Summary</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        tool_calls_table = '<p class="empty-note">No tool calls in the last 7 days.</p>'

    # ── Recent delegations
    if data.recent_delegations:
        rows = []
        for r in data.recent_delegations:
            rel = relative_time(r.ts, now)
            parent = r.parent_agent or "—"
            rows.append(
                f'<tr>'
                f'<td class="num">{html.escape(rel)}</td>'
                f'<td>{html.escape(r.agent)}</td>'
                f'<td>{html.escape(parent)}</td>'
                f'<td>{status_pill(r.status)}</td>'
                f'<td>{html.escape(truncate(r.summary, 80))}</td>'
                f'</tr>'
            )
        delegations_table = (
            '<table>'
            '<thead><tr><th>When</th><th>Agent</th><th>Delegated from</th>'
            '<th>Status</th><th>Summary</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        delegations_table = '<p class="empty-note">No delegations in the last 7 days.</p>'

    # ── Recent captures
    if data.recent_captures:
        rows = []
        for c in data.recent_captures:
            rel = relative_time(c["mtime_dt"], now)
            rows.append(
                f'<tr>'
                f'<td class="num">{html.escape(rel)}</td>'
                f'<td>{html.escape(c["agent"])}</td>'
                f'<td>{html.escape(c["filename"])}</td>'
                f'</tr>'
            )
        captures_table = (
            '<table>'
            '<thead><tr><th>When</th><th>Agent</th><th>File</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        captures_table = '<p class="empty-note">No memory captures found.</p>'

    # ── Recent dreams
    if data.recent_dreams:
        rows = []
        for d in data.recent_dreams:
            applied_badge = (
                '<span class="pill ok">applied</span>'
                if d.applied
                else '<span class="pill neutral">pending</span>'
            )
            status_html = status_pill(d.status)
            rows.append(
                f'<tr>'
                f'<td class="num">{html.escape(d.ts[:16] if d.ts else "—")}</td>'
                f'<td>{html.escape(d.agent)}</td>'
                f'<td class="muted">{html.escape(d.dream_id[:20])}</td>'
                f'<td>{status_html}</td>'
                f'<td class="right num">{d.consolidations}</td>'
                f'<td class="right num">{d.promotions}</td>'
                f'<td class="right num">{d.marked_stale}</td>'
                f'<td>{applied_badge}</td>'
                f'</tr>'
            )
        dreams_table = (
            '<table>'
            '<thead><tr><th>Started</th><th>Agent</th><th>Dream ID</th><th>Status</th>'
            '<th class="right">Consol.</th><th class="right">Promoted</th>'
            '<th class="right">Staled</th><th>Applied</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        dreams_table = '<p class="empty-note">No dream runs found.</p>'

    # ── Lock state
    if data.lock_states:
        rows = []
        for lk in data.lock_states:
            held_min = lk.held_seconds / 60
            rows.append(
                f'<tr class="row-error">'
                f'<td>{html.escape(lk.agent)}</td>'
                f'<td class="num" style="color: var(--error)">{held_min:.1f} min</td>'
                f'<td><span class="pill error">stale</span></td>'
                f'</tr>'
            )
        lock_table = (
            '<table>'
            '<thead><tr><th>Agent</th><th>Held for</th><th>State</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
        lock_panel_style = ""
    else:
        lock_table = '<p class="empty-note">No stale locks detected. All clear.</p>'
        lock_panel_style = ""

    body = f"""
{kpis}

<section class="panel">
  <h2>Last {len(data.recent_runs)} runs</h2>
  {runs_table}
</section>

<section class="panel">
  <h2>Recent failures · 24h ({len(data.recent_failures)})</h2>
  {failures_table}
</section>

<div class="grid-2">
  <section class="panel">
    <h2>Recent tool calls · last 7d</h2>
    {tool_calls_table}
  </section>
  <section class="panel">
    <h2>Recent delegations · last 7d</h2>
    {delegations_table}
  </section>
</div>

<section class="panel">
  <h2>Recent memory captures</h2>
  {captures_table}
</section>

<section class="panel">
  <h2>Recent dream runs</h2>
  {dreams_table}
</section>

<section class="panel">
  <h2>Lock state — stale locks ({len(data.lock_states)} detected)</h2>
  {lock_table}
</section>
"""

    gen_ts = data.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    return page_shell(
        title="Activity Pulse",
        body=body,
        current_tab="activity",
        has_goals=has_goals,
        subtitle=f"as of {gen_ts}",
    )

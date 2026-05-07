"""Goals & Outcomes tab — aggregation + render.

Answers "what are my agents working on?" — conditionally rendered.

Only renders goals.html if at least one agent has a goal.md file.
Reads goal.md, goal_history.jsonl, and outcomes/runs/*/result.json.
Pure Python, no LLM calls.
"""

from __future__ import annotations
import html
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .costs import discover_agents
from ._shared import page_shell, truncate
from .._io import atomic_write


# ──────────────────────────────────────────────────────────────────
# Data structures

@dataclass
class SubGoalRow:
    id: str
    label: str
    status: str
    blocked_by: str | None
    last_advance_ts: str | None


@dataclass
class ActiveGoalEntry:
    agent: str
    intent: str
    priority: str
    created: str
    deadline: str | None
    days_since_start: int | None
    days_until_deadline: int | None
    is_overdue: bool
    total_sub_goals: int
    status_counts: dict[str, int]   # {status: count}
    sub_goals: list[SubGoalRow]


@dataclass
class BlockedSubGoal:
    agent: str
    goal_intent: str
    sub_goal_id: str
    sub_goal_label: str
    blocked_by: str | None
    blocked_at: str | None   # from goal_history.jsonl if available


@dataclass
class OutcomeRunRecord:
    ts: str
    agent: str
    run_id: str
    description: str
    status: str
    iterations: int
    max_iterations: int
    total_cost_usd: float


@dataclass
class GoalsData:
    generated_at: datetime
    active_goals: list[ActiveGoalEntry]
    blocked_sub_goals: list[BlockedSubGoal]
    recent_outcome_runs: list[OutcomeRunRecord]    # last 50
    iteration_histogram: dict[int, int]            # iteration_count → run_count (completed, 90d)
    has_any_goal: bool


# ──────────────────────────────────────────────────────────────────
# Aggregation

def has_any_goal(agents_root: Path) -> bool:
    """Return True if at least one agent has a goal.md file."""
    for agent in discover_agents(agents_root):
        if (agents_root / agent / "goal.md").exists():
            return True
    return False


def aggregate_goals(
    agents_root: Path,
    today: date | None = None,
    now: datetime | None = None,
    max_outcome_runs: int = 50,
    outcome_histogram_days: int = 90,
) -> GoalsData:
    """Build GoalsData for the Goals & Outcomes tab."""
    today = today or date.today()
    now = now or datetime.now(tz=timezone.utc)
    cutoff_90d = today - timedelta(days=outcome_histogram_days)

    agent_names = discover_agents(agents_root)

    active_goals: list[ActiveGoalEntry] = []
    all_blocked: list[BlockedSubGoal] = []
    all_outcome_runs: list[OutcomeRunRecord] = []
    histogram: dict[int, int] = defaultdict(int)

    for agent in agent_names:
        agent_root = agents_root / agent
        goal_path = agent_root / "goal.md"

        if not goal_path.exists():
            continue

        # Parse goal.md via GoalManager if available; fallback to manual parse
        goal_data = _load_goal_data(goal_path)
        if goal_data is None:
            continue

        # Build blocked_at map from goal_history.jsonl
        blocked_at_map = _load_blocked_at_from_history(agent_root)

        sub_goal_rows: list[SubGoalRow] = []
        status_counts: dict[str, int] = defaultdict(int)

        for sg in goal_data.get("sub_goals", []):
            sg_id = str(sg.get("id", ""))
            sg_status = str(sg.get("status", "pending"))
            status_counts[sg_status] += 1
            sub_goal_rows.append(SubGoalRow(
                id=sg_id,
                label=str(sg.get("label", "")),
                status=sg_status,
                blocked_by=sg.get("blocked_by"),
                last_advance_ts=blocked_at_map.get(sg_id),
            ))
            if sg_status == "blocked":
                all_blocked.append(BlockedSubGoal(
                    agent=agent,
                    goal_intent=str(goal_data.get("intent", "")),
                    sub_goal_id=sg_id,
                    sub_goal_label=str(sg.get("label", "")),
                    blocked_by=sg.get("blocked_by"),
                    blocked_at=blocked_at_map.get(sg_id),
                ))

        created_str = str(goal_data.get("created", ""))
        deadline_str = goal_data.get("deadline")
        days_since_start: int | None = None
        days_until_deadline: int | None = None
        is_overdue = False

        try:
            created_d = date.fromisoformat(created_str)
            days_since_start = (today - created_d).days
        except (ValueError, TypeError):
            pass

        if deadline_str:
            try:
                deadline_d = date.fromisoformat(str(deadline_str)[:10])
                days_until_deadline = (deadline_d - today).days
                is_overdue = days_until_deadline < 0
            except (ValueError, TypeError):
                pass

        active_goals.append(ActiveGoalEntry(
            agent=agent,
            intent=str(goal_data.get("intent", "")),
            priority=str(goal_data.get("priority", "medium")),
            created=created_str,
            deadline=str(deadline_str) if deadline_str else None,
            days_since_start=days_since_start,
            days_until_deadline=days_until_deadline,
            is_overdue=is_overdue,
            total_sub_goals=len(sub_goal_rows),
            status_counts=dict(status_counts),
            sub_goals=sub_goal_rows,
        ))

        # Outcome runs
        outcomes_dir = agent_root / "outcomes" / "runs"
        if outcomes_dir.exists():
            for run_dir in sorted(outcomes_dir.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
                result_path = run_dir / "result.json"
                if not result_path.exists():
                    continue
                try:
                    res = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                run_ts = res.get("started_at") or res.get("ts") or ""
                status = str(res.get("status", "unknown"))
                iterations_data = res.get("iterations") or []
                n_iterations = len(iterations_data) if isinstance(iterations_data, list) else int(iterations_data or 0)
                max_iters = int(res.get("max_iterations") or 3)
                cost = float(res.get("total_cost_usd") or 0.0)
                desc = str(res.get("description") or "")
                run_id = res.get("run_id") or run_dir.name

                or_rec = OutcomeRunRecord(
                    ts=run_ts,
                    agent=agent,
                    run_id=str(run_id),
                    description=desc,
                    status=status,
                    iterations=n_iterations,
                    max_iterations=max_iters,
                    total_cost_usd=cost,
                )
                all_outcome_runs.append(or_rec)

                # Histogram for completed runs in 90d
                try:
                    run_date = date.fromisoformat(run_ts[:10])
                except (ValueError, TypeError):
                    run_date = None
                if run_date and run_date >= cutoff_90d and status == "satisfied":
                    histogram[n_iterations] += 1

    # Sort
    all_outcome_runs.sort(key=lambda x: x.ts, reverse=True)
    recent_outcome_runs = all_outcome_runs[:max_outcome_runs]

    return GoalsData(
        generated_at=now,
        active_goals=active_goals,
        blocked_sub_goals=all_blocked,
        recent_outcome_runs=recent_outcome_runs,
        iteration_histogram=dict(histogram),
        has_any_goal=len(active_goals) > 0,
    )


def _load_goal_data(goal_path: Path) -> dict | None:
    """Parse goal.md frontmatter. Returns dict or None on failure."""
    try:
        import frontmatter as _fm
        parsed = _fm.load(goal_path)
        return dict(parsed.metadata)
    except ImportError:
        pass
    except Exception:
        return None

    # Fallback: manual parse
    try:
        text = goal_path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not text.startswith("---"):
        return None

    lines = text.splitlines()
    result: dict = {}
    in_fm = False
    yaml_lines: list[str] = []
    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            in_fm = True
            continue
        if in_fm and line.strip() == "---":
            break
        if in_fm:
            yaml_lines.append(line)

    try:
        import yaml  # type: ignore
        result = yaml.safe_load("\n".join(yaml_lines)) or {}
    except Exception:
        # Ultra-minimal parse: only key: value pairs, no sub_goals list
        for line in yaml_lines:
            if ":" in line and not line.startswith(" "):
                key, _, val = line.partition(":")
                result[key.strip()] = val.strip()

    return result if result else None


def _load_blocked_at_from_history(agent_root: Path) -> dict[str, str]:
    """Parse goal_history.jsonl to get blocked_at timestamps per sub_goal_id.

    Returns {sub_goal_id: ts_str}.
    """
    history_path = agent_root / "goal_history.jsonl"
    if not history_path.exists():
        return {}

    blocked_at: dict[str, str] = {}
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "sub_goal_blocked" or "blocked" in str(rec.get("event", "")):
            sg_id = rec.get("sub_goal_id")
            ts = rec.get("ts", "")
            if sg_id and ts:
                blocked_at[sg_id] = ts

    return blocked_at


# ──────────────────────────────────────────────────────────────────
# Rendering

def render_goals(agents_root: Path, data: GoalsData) -> Path | None:
    """Write _dashboard/goals.html and return the path.

    Returns None if there are no goals to render.
    """
    if not data.has_any_goal:
        return None

    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    html_content = _render_goals_template(data, has_goals=True)
    out_path = out_dir / "goals.html"
    atomic_write(out_path, html_content)
    return out_path


def _status_badge(status: str) -> str:
    """HTML badge for a sub-goal status."""
    badges = {
        "pending":     ("neutral", "pending"),
        "in_progress": ("warn",    "in progress"),
        "complete":    ("ok",      "complete"),
        "blocked":     ("error",   "blocked"),
        "abandoned":   ("neutral", "abandoned"),
    }
    cls, label = badges.get(status, ("neutral", status))
    return f'<span class="pill {cls}">{html.escape(label)}</span>'


def _render_goals_template(data: GoalsData, has_goals: bool = True) -> str:
    # ── Active goals
    if data.active_goals:
        goal_panels = []
        for g in data.active_goals:
            # Status summary line
            sc = g.status_counts
            status_line = (
                f"{sc.get('complete', 0)} complete · "
                f"{sc.get('in_progress', 0)} in progress · "
                f"{sc.get('pending', 0)} pending · "
                f"{sc.get('blocked', 0)} blocked · "
                f"{sc.get('abandoned', 0)} abandoned"
            )
            # Deadline / pace info
            if g.days_since_start is not None:
                pace_html = f'<span class="muted">Started {g.days_since_start}d ago.</span>'
            else:
                pace_html = ""
            if g.deadline:
                if g.is_overdue:
                    pace_html += f' <span style="color: var(--error)">OVERDUE by {abs(g.days_until_deadline or 0)}d</span>'
                else:
                    pace_html += f' <span class="muted">Deadline: {html.escape(g.deadline)} ({g.days_until_deadline}d remaining).</span>'

            # Sub-goal table
            sg_rows = []
            for sg in g.sub_goals:
                blocked_html = html.escape(sg.blocked_by or "—")
                last_ts = html.escape(sg.last_advance_ts[:16] if sg.last_advance_ts else "—")
                sg_rows.append(
                    f'<tr>'
                    f'<td class="muted">{html.escape(sg.id)}</td>'
                    f'<td>{html.escape(sg.label)}</td>'
                    f'<td>{_status_badge(sg.status)}</td>'
                    f'<td class="muted">{blocked_html}</td>'
                    f'<td class="num">{last_ts}</td>'
                    f'</tr>'
                )
            sg_table = (
                '<table>'
                '<thead><tr><th>ID</th><th>Label</th><th>Status</th>'
                '<th>Blocked by</th><th>Last advance</th></tr></thead>'
                f'<tbody>{"".join(sg_rows)}</tbody>'
                '</table>'
            ) if sg_rows else '<p class="empty-note">No sub-goals defined.</p>'

            priority_color = {
                "high": "var(--error)",
                "medium": "var(--warn)",
                "low": "var(--muted)",
            }.get(g.priority, "var(--muted)")

            goal_panels.append(f"""
<section class="panel">
  <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px;">
    <div>
      <strong>{html.escape(g.agent)}</strong>
      <span style="margin-left: 12px; font-size: 13px;">{html.escape(g.intent)}</span>
    </div>
    <div>
      <span class="pill neutral" style="color: {priority_color}; border-color: {priority_color}">{html.escape(g.priority)}</span>
    </div>
  </div>
  <p class="muted" style="margin-bottom: 8px">{status_line}</p>
  <p style="margin-bottom: 12px; font-size: 13px">{pace_html}</p>
  {sg_table}
</section>
""")
        goals_section = "".join(goal_panels)
    else:
        goals_section = '<section class="panel"><p class="empty-note">No active goals found.</p></section>'

    # ── Blocked sub-goals (operator action queue)
    if data.blocked_sub_goals:
        rows = []
        for b in data.blocked_sub_goals:
            rows.append(
                f'<tr class="row-error">'
                f'<td>{html.escape(b.agent)}</td>'
                f'<td class="muted">{html.escape(truncate(b.goal_intent, 50))}</td>'
                f'<td>{html.escape(b.sub_goal_id)}</td>'
                f'<td>{html.escape(b.sub_goal_label)}</td>'
                f'<td class="muted">{html.escape(b.blocked_by or "—")}</td>'
                f'<td class="num">{html.escape(b.blocked_at[:16] if b.blocked_at else "—")}</td>'
                f'</tr>'
            )
        blocked_table = (
            '<table>'
            '<thead><tr><th>Agent</th><th>Goal</th><th>Sub-goal ID</th>'
            '<th>Label</th><th>Blocked by</th><th>Blocked at</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        blocked_table = '<p class="empty-note">No blocked sub-goals. Good state.</p>'

    # ── Recent outcome runs
    if data.recent_outcome_runs:
        rows = []
        for r in data.recent_outcome_runs:
            status_badge = _status_badge(r.status)
            iter_str = f"{r.iterations} / {r.max_iterations}"
            cost_str = f"${r.total_cost_usd:.4f}" if r.total_cost_usd else "—"
            rows.append(
                f'<tr>'
                f'<td class="num">{html.escape(r.ts[:16] if r.ts else "—")}</td>'
                f'<td>{html.escape(r.agent)}</td>'
                f'<td>{html.escape(truncate(r.description, 60))}</td>'
                f'<td>{status_badge}</td>'
                f'<td class="right num">{iter_str}</td>'
                f'<td class="right num">{cost_str}</td>'
                f'</tr>'
            )
        outcome_table = (
            '<table>'
            '<thead><tr><th>Started</th><th>Agent</th><th>Description</th>'
            '<th>Status</th><th class="right">Iterations</th>'
            '<th class="right">Cost</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        outcome_table = '<p class="empty-note">No outcome runs found.</p>'

    # ── Iteration histogram
    if data.iteration_histogram:
        max_count = max(data.iteration_histogram.values())
        hist_rows = []
        for iters in sorted(data.iteration_histogram.keys()):
            count = data.iteration_histogram[iters]
            bar_width = int(count / max_count * 200) if max_count > 0 else 0
            hist_rows.append(
                f'<tr>'
                f'<td class="right num">{iters}</td>'
                f'<td>'
                f'<div style="display: inline-block; height: 12px; width: {bar_width}px; background: var(--accent); border-radius: 2px; vertical-align: middle;"></div>'
                f' <span class="num" style="margin-left: 6px">{count}</span>'
                f'</td>'
                f'</tr>'
            )
        histogram_table = (
            '<table>'
            '<thead><tr><th class="right">Iterations</th><th>Run count (satisfied, 90d)</th></tr></thead>'
            f'<tbody>{"".join(hist_rows)}</tbody>'
            '</table>'
        )
    else:
        histogram_table = '<p class="empty-note">No completed outcome runs in the last 90 days.</p>'

    gen_ts = data.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    body = f"""
<h2>Active goals</h2>
{goals_section}

<section class="panel">
  <h2>Blocked sub-goals — operator action queue ({len(data.blocked_sub_goals)})</h2>
  {blocked_table}
</section>

<section class="panel">
  <h2>Recent outcome runs · last {len(data.recent_outcome_runs)}</h2>
  {outcome_table}
</section>

<section class="panel">
  <h2>Outcome iteration histogram · satisfied runs · 90 days</h2>
  {histogram_table}
</section>
"""

    return page_shell(
        title="Goals & Outcomes",
        body=body,
        current_tab="goals",
        has_goals=True,
        subtitle=f"as of {gen_ts}",
    )

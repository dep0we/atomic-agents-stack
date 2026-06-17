"""HTML rendering for the cost dashboard.

Pure-Python templates (no Jinja2) for portability and to keep deps minimal.
The HTML is deliberately matched to samples/caldwell/dashboard.html — that's
the visual contract.

Self-contained output: inline CSS, no external assets, no JavaScript
dependencies. Opens in any browser. Refresh button only does anything
when the optional Flask server (serve.py) is running.
"""

from __future__ import annotations
import html
import json
import shutil
import urllib.parse
from datetime import date
from pathlib import Path

from .._io import atomic_write
from .costs import (
    AgentDashboardData,
    AgentSummary,
    GlobalSummary,
    aggregate_agent,
    aggregate_global,
    discover_agents,
    to_json_dict,
)
from ._shared import nav_bar as _nav_bar, CSS as _SHARED_CSS, _CSP as _SHARED_CSP


# ──────────────────────────────────────────────────────────────────
# Public entry points


def render_all(
    agents_root: Path,
    today: date | None = None,
    tab: str = "all",
) -> dict:
    """Render the global dashboard + per-agent dashboards + new tabs.

    tab: "all" (default) renders everything; or one of
         "cost" | "activity" | "quality" | "memory" | "goals"
         to render only that tab (useful for fast iteration).

    Returns a dict with paths of files written, for caller logging.
    """
    from datetime import datetime, timezone
    from .activity import aggregate_activity, render_activity
    from .quality import aggregate_quality, render_quality
    from .memory import aggregate_memory, render_memory
    from .goals import aggregate_goals, render_goals, has_any_goal

    today = today or date.today()
    now = datetime.now(tz=timezone.utc)
    written: dict = {"global": None, "per_agent": []}

    render_cost = tab in ("all", "cost")
    render_activity_tab = tab in ("all", "activity")
    render_quality_tab = tab in ("all", "quality")
    render_memory_tab = tab in ("all", "memory")
    render_goals_tab = tab in ("all", "goals")

    if render_cost:
        global_summary = aggregate_global(agents_root, today=today)
        global_path = render_global(agents_root, global_summary)
        written["global"] = str(global_path)

        for agent_name in discover_agents(agents_root):
            data = aggregate_agent(agents_root, agent_name, today=today)
            agent_path = render_agent(agents_root, data)
            written["per_agent"].append(str(agent_path))

    if render_activity_tab:
        activity_data = aggregate_activity(agents_root, now=now)
        activity_path = render_activity(agents_root, activity_data)
        written["activity"] = str(activity_path)

    if render_quality_tab:
        quality_data = aggregate_quality(agents_root, today=today, now=now)
        quality_path = render_quality(agents_root, quality_data)
        written["quality"] = str(quality_path)

    if render_memory_tab:
        memory_data = aggregate_memory(agents_root, today=today, now=now)
        memory_path = render_memory(agents_root, memory_data)
        written["memory"] = str(memory_path)

    if render_goals_tab and has_any_goal(agents_root):
        goals_data = aggregate_goals(agents_root, today=today, now=now)
        goals_path = render_goals(agents_root, goals_data)
        if goals_path:
            written["goals"] = str(goals_path)

    return written


def render_global(agents_root: Path, summary: GlobalSummary) -> Path:
    """Render <agents_root>/_dashboard/index.html. Returns the written path."""
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    has_goals = any(
        (agents_root / agent / "goal.md").exists()
        for agent in discover_agents(agents_root)
    )
    html = _render_global_template(summary, has_goals=has_goals)
    out_path = out_dir / "index.html"
    atomic_write(out_path, html)

    # Pre-aggregated JSON for fast page-load by future versions
    data_dir = out_dir / "data"
    data_dir.mkdir(exist_ok=True)
    label_safe = summary.period_label.replace(" ", "_").lower()
    atomic_write(
        data_dir / f"{label_safe}.json", json.dumps(to_json_dict(summary), indent=2)
    )

    return out_path


def render_agent(agents_root: Path, data: AgentDashboardData) -> Path:
    """Render <agents_root>/<agent>/dashboard.html. Returns the written path."""
    html = _render_agent_template(data)
    out_path = agents_root / data.name / "dashboard.html"
    atomic_write(out_path, html)
    return out_path


# ──────────────────────────────────────────────────────────────────
# Templates (pure Python f-strings — kept simple)

# The visual style matches samples/caldwell/dashboard.html — see that file
# for the design contract. Inline CSS, no external dependencies, opens in
# any browser, works on phone.

CSS = """
:root {
  --bg: #0f1419; --card: #1a2028; --text: #e6e6e6; --muted: #8a96a3;
  --accent: #4ec9b0; --warn: #d19a66; --error: #e06c75; --good: #98c379;
  --border: #2a323d;
  --opus: #c678dd; --sonnet: #61afef; --haiku: #98c379;
  --gpt: #c0a050; --kimi: #5fb3b3; --local: #888;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", system-ui, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
  padding: 32px 48px; max-width: 1400px; margin: 0 auto;
}
header {
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
}
h1 { font-size: 24px; font-weight: 600; }
h2 { font-size: 16px; font-weight: 600; margin: 32px 0 12px;
     color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.period { color: var(--muted); font-size: 14px; }
.breadcrumb { color: var(--muted); font-size: 13px; margin-bottom: 4px; }
.breadcrumb a { color: var(--accent); text-decoration: none; }
.breadcrumb a:hover { text-decoration: underline; }
.refresh-btn {
  background: var(--card); color: var(--text); border: 1px solid var(--border);
  padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.refresh-btn:hover { border-color: var(--accent); }

.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 8px; }
.kpi {
  background: var(--card); border: 1px solid var(--border);
  padding: 20px; border-radius: 10px;
}
.kpi .value { font-size: 28px; font-weight: 700; margin-bottom: 4px; }
.kpi .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.kpi .delta { font-size: 12px; margin-top: 6px; }
.kpi .delta.up { color: var(--error); }
.kpi .delta.down { color: var(--good); }
.kpi .delta.neutral { color: var(--muted); }

.panel {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 24px; margin-bottom: 16px;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th {
  text-align: left; color: var(--muted); font-weight: 500; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.05em;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
}
tbody td { padding: 10px 12px; border-bottom: 1px solid var(--border); }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: rgba(78, 201, 176, 0.04); }
.num { font-variant-numeric: tabular-nums; }
.right { text-align: right; }
.muted { color: var(--muted); font-size: 12px; }
.pill {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 500; border: 1px solid;
}
.pill.opus { color: var(--opus); border-color: var(--opus); background: rgba(198, 120, 221, 0.1); }
.pill.sonnet { color: var(--sonnet); border-color: var(--sonnet); background: rgba(97, 175, 239, 0.1); }
.pill.haiku { color: var(--haiku); border-color: var(--haiku); background: rgba(152, 195, 121, 0.1); }
.pill.gpt { color: var(--gpt); border-color: var(--gpt); background: rgba(192, 160, 80, 0.1); }
.pill.kimi { color: var(--kimi); border-color: var(--kimi); background: rgba(95, 179, 179, 0.1); }
.pill.local { color: var(--local); border-color: var(--local); background: rgba(136, 136, 136, 0.1); }
.pill.fallback { color: var(--warn); border-color: var(--warn); background: rgba(209, 154, 102, 0.1); margin-left: 4px; }
.pill.helper { color: var(--haiku); border-color: var(--haiku); background: rgba(152, 195, 121, 0.05); font-size: 10px; }
.pill.warn { color: var(--warn); border-color: var(--warn); background: rgba(209, 154, 102, 0.1); }
.degraded-banner { margin-bottom: 16px; padding: 10px 16px; border-radius: 8px;
  background: rgba(209, 154, 102, 0.08); border: 1px solid rgba(209, 154, 102, 0.3);
  font-size: 13px; color: var(--warn); }

.day-chart { display: flex; align-items: flex-end; height: 220px; gap: 12px; padding: 16px 0; }
.day-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; }
.day-stack { width: 100%; display: flex; flex-direction: column-reverse; min-height: 1px; }
.day-seg { width: 100%; transition: opacity 0.2s; }
.day-seg.opus { background: var(--opus); }
.day-seg.sonnet { background: var(--sonnet); }
.day-seg.haiku { background: var(--haiku); }
.day-col:hover .day-seg { opacity: 0.7; }
.day-label { font-size: 11px; color: var(--muted); }
.day-value { font-size: 12px; font-variant-numeric: tabular-nums; font-weight: 500; }

.model-mix-bar { display: flex; height: 32px; border-radius: 6px; overflow: hidden; margin: 12px 0; }
.model-mix-seg { display: flex; align-items: center; justify-content: center;
                  font-size: 11px; color: rgba(0,0,0,0.7); font-weight: 600; min-width: 0; }
.model-mix-seg.opus { background: var(--opus); }
.model-mix-seg.sonnet { background: var(--sonnet); }
.model-mix-seg.haiku { background: var(--haiku); }
.model-mix-seg.gpt { background: var(--gpt); }
.model-mix-seg.kimi { background: var(--kimi); }
.model-mix-seg.local { background: var(--local); }

.legend { display: flex; gap: 16px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }
.legend-item { display: flex; align-items: center; gap: 6px; }
.legend-dot { width: 10px; height: 10px; border-radius: 2px; }

.savings-card {
  background: linear-gradient(135deg, rgba(152, 195, 121, 0.08), rgba(78, 201, 176, 0.04));
  border: 1px solid rgba(152, 195, 121, 0.2); padding: 20px; border-radius: 10px;
}
.savings-headline { font-size: 22px; font-weight: 700; color: var(--good); }
.savings-detail { font-size: 13px; color: var(--muted); margin-top: 6px; }

.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

.empty-note { color: var(--muted); font-style: italic; font-size: 13px; padding: 12px 0; }

footer {
  margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--muted); font-size: 12px;
  display: flex; justify-content: space-between;
}
/* Top navigation bar */
.tab-nav {
  display: flex; gap: 4px; margin-bottom: 24px;
  border-bottom: 1px solid var(--border); padding-bottom: 0;
}
.tab-nav a {
  display: inline-block; padding: 8px 16px; font-size: 13px; font-weight: 500;
  color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tab-nav a:hover { color: var(--text); border-bottom-color: var(--border); }
.tab-nav a.active { color: var(--accent); border-bottom-color: var(--accent); }
"""


def _model_pill_class(model: str) -> str:
    """Map a model id to a CSS class for its pill color."""
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    if m.startswith("gpt"):
        return "gpt"
    if "kimi" in m or m.startswith("moonshot"):
        return "kimi"
    if m.startswith("local") or "qwen" in m or "llama" in m:
        return "local"
    return "opus"  # fallback color


def _model_pill(model: str, label: str | None = None) -> str:
    cls = _model_pill_class(model)
    label = label or _short_model_name(model)
    # label is log-derived (model id) on some call sites; escape at the HTML
    # boundary so a crafted model string can't inject markup (#517).
    return f'<span class="pill {cls}">{html.escape(label)}</span>'


def _short_model_name(model: str) -> str:
    """Compact display name for a model id."""
    if "opus" in model.lower():
        return "Opus"
    if "sonnet" in model.lower():
        return "Sonnet"
    if "haiku" in model.lower():
        return "Haiku"
    if model.startswith("gpt-5-mini"):
        return "GPT-5 mini"
    if model.startswith("gpt-5-nano"):
        return "GPT-5 nano"
    if model.startswith("gpt-5"):
        return "GPT-5"
    if "kimi" in model.lower():
        return "Kimi"
    return model.split("/")[-1][:24]


def _delta_class(pct: float) -> str:
    if pct > 1:
        return "up"
    if pct < -1:
        return "down"
    return "neutral"


def _delta_label(pct: float) -> str:
    if abs(pct) < 0.1:
        return "no change vs. last month"
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}% vs. last month"


def _render_global_template(s: GlobalSummary, has_goals: bool = True) -> str:
    """The global dashboard HTML."""
    delta_pct = s.delta_vs_prior_period.get("cost_pct", 0.0)

    # Per-agent table rows
    if s.agents:
        agent_rows = []
        for a in s.agents:
            models_html = "".join(
                f'<span class="pill {_model_pill_class(m)}" style="margin-right: 4px;">'
                f"{html.escape(_short_model_name(m))}: ${cost:.4f}</span>"
                for m, cost in a.cost_by_model.items()
            )
            _name_href = urllib.parse.quote(a.name, safe="")
            _name_text = html.escape(a.name)
            agent_rows.append(
                f"<tr>"
                f'<td><a href="../{_name_href}/dashboard.html" style="color: var(--accent); text-decoration: none;">{_name_text}</a></td>'
                f'<td class="right num">${a.cost_usd:.4f}</td>'
                f'<td class="right num">{a.runs}</td>'
                f'<td class="right num">{a.errors}</td>'
                f'<td class="right num">{a.cache_hit_pct}%</td>'
                f"<td>{models_html}</td>"
                f"</tr>"
            )
        agents_table = (
            "<table>"
            '<thead><tr><th>Agent</th><th class="right">Cost</th>'
            '<th class="right">Runs</th><th class="right">Errors</th>'
            '<th class="right">Cache hit</th><th>Models used</th></tr></thead>'
            f"<tbody>{''.join(agent_rows)}</tbody>"
            "</table>"
        )
    else:
        agents_table = '<p class="empty-note">No agent activity this period.</p>'

    # Top runs table
    if s.top_runs:
        top_rows = []
        for r in s.top_runs:
            ts_short = r.ts.strftime("%b %d · %H:%M")
            top_rows.append(
                f"<tr>"
                f'<td class="num">{ts_short}</td>'
                f"<td>{html.escape(r.agent)}</td>"
                f"<td>{html.escape(r.trigger)}</td>"
                f"<td>{_model_pill(r.model)}</td>"
                f'<td class="right num">{r.input_tokens:,} / {r.output_tokens:,}</td>'
                f'<td class="right num"><strong>${r.cost_usd:.4f}</strong></td>'
                f"<td>{html.escape(_truncate(r.summary, 60))}</td>"
                f"</tr>"
            )
        top_table = (
            "<table>"
            "<thead><tr><th>Date</th><th>Agent</th><th>Trigger</th>"
            '<th>Model</th><th class="right">Tokens (in / out)</th>'
            '<th class="right">Cost</th><th>Summary</th></tr></thead>'
            f"<tbody>{''.join(top_rows)}</tbody>"
            "</table>"
        )
    else:
        top_table = '<p class="empty-note">No runs this period.</p>'

    # Model mix bar
    model_bar_html = _render_model_mix_bar(s.by_model_global, s.total_cost)

    # Provider breakdown
    if s.by_provider:
        provider_rows = "".join(
            f'<tr><td>{p}</td><td class="right num">${c:.4f}</td>'
            f'<td class="right num">{(c / s.total_cost * 100 if s.total_cost else 0):.1f}%</td></tr>'
            for p, c in sorted(s.by_provider.items(), key=lambda x: -x[1])
        )
        provider_html = (
            "<table>"
            '<thead><tr><th>Provider</th><th class="right">Cost</th>'
            '<th class="right">% of spend</th></tr></thead>'
            f"<tbody>{provider_rows}</tbody>"
            "</table>"
        )
    else:
        provider_html = '<p class="empty-note">No provider activity.</p>'

    _nav = _nav_bar("cost", has_goals=has_goals)
    _degraded_banner = (
        '<div class="degraded-banner">'
        '<span class="pill warn">⚠ data may be incomplete</span>'
        " &nbsp;One or more log reads failed. The figures below reflect"
        " partial data only — some agent costs may be missing or understated."
        "</div>"
        if s.cost_data_degraded
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_SHARED_CSP}">
<title>Atomic Agents — {html.escape(s.period_label)}</title>
<style>{CSS}</style>
</head>
<body>

<header>
  <div>
    <h1>Atomic Agents</h1>
    <div class="period">{html.escape(s.period_label)} · as of {s.today.isoformat()}</div>
  </div>
  <div>
    <button class="refresh-btn" onclick="refresh()">↻ Refresh</button>
  </div>
</header>

{_nav}
{_degraded_banner}
<section class="kpis">
  <div class="kpi">
    <div class="value">${s.total_cost:.2f}</div>
    <div class="label">Spend this month</div>
    <div class="delta {_delta_class(delta_pct)}">{_delta_label(delta_pct)}</div>
  </div>
  <div class="kpi">
    <div class="value">{s.total_runs}</div>
    <div class="label">Total runs</div>
  </div>
  <div class="kpi">
    <div class="value">{s.composite_cache_hit_pct}%</div>
    <div class="label">Cache hit rate</div>
  </div>
  <div class="kpi">
    <div class="value">{s.total_errors}</div>
    <div class="label">Errors</div>
  </div>
</section>

<section class="panel">
  <h2>Per-agent breakdown</h2>
  {agents_table}
</section>

<div class="grid-2">
  <section class="panel">
    <h2>Model mix · this month</h2>
    {model_bar_html}
  </section>

  <section class="panel">
    <h2>Provider breakdown</h2>
    {provider_html}
  </section>
</div>

<section class="panel">
  <h2>Top {len(s.top_runs)} most expensive runs</h2>
  {top_table}
</section>

<footer>
  <div>Generated {s.today.isoformat()} by atomic_agents.dashboard</div>
  <div>Aggregated from log/YYYY-MM/*.jsonl across {len(s.agents)} agents</div>
</footer>

<script>
function refresh() {{
  fetch('/regenerate', {{method: 'POST'}})
    .then(r => {{ if (r.ok) location.reload(); else fallback(); }})
    .catch(fallback);
}}
function fallback() {{
  // No server running. Plain reload — shows last cron-built data.
  location.reload();
}}
</script>

</body>
</html>
"""


def _render_model_mix_bar(by_model: dict[str, float], total: float) -> str:
    """Render the stacked model-mix bar + table."""
    if not by_model or total <= 0:
        return '<p class="empty-note">No model activity.</p>'

    # Sort by cost desc
    items = sorted(by_model.items(), key=lambda x: -x[1])
    bar_segs = []
    for model, cost in items:
        pct = cost / total * 100
        if pct < 0.5:
            label = ""
        else:
            # model is log-derived; escape both the bar label (text) and the
            # title attribute (a raw " would break out of the attribute) (#517).
            label = f"{html.escape(_short_model_name(model))} {pct:.1f}%"
        cls = _model_pill_class(model)
        bar_segs.append(
            f'<div class="model-mix-seg {cls}" style="width: {pct}%" '
            f'title="{html.escape(model)}: ${cost:.4f}">'
            f"{label}</div>"
        )

    table_rows = []
    for model, cost in items:
        pct = cost / total * 100
        table_rows.append(
            f"<tr>"
            f"<td>{_model_pill(model, model)}</td>"
            f'<td class="right num">${cost:.4f}</td>'
            f'<td class="right num">{pct:.1f}%</td>'
            f"</tr>"
        )

    return (
        f'<div class="model-mix-bar">{"".join(bar_segs)}</div>'
        "<table>"
        '<thead><tr><th>Model</th><th class="right">Cost</th>'
        '<th class="right">% spend</th></tr></thead>'
        f"<tbody>{''.join(table_rows)}</tbody>"
        "</table>"
    )


def _render_agent_template(d: AgentDashboardData) -> str:
    """The per-agent dashboard HTML."""
    s = d.summary_this_month

    # Daily cost chart (current month)
    if d.daily_costs:
        max_daily = max(d.daily_costs.values()) if d.daily_costs else 1.0
        max_height = 200
        day_cols = []
        for day_iso, cost in d.daily_costs.items():
            day_short = day_iso.split("-")[-1]
            height = max(1, int(cost / max_daily * max_height)) if max_daily > 0 else 1
            day_cols.append(
                f'<div class="day-col">'
                f'<div class="day-value">${cost:.3f}</div>'
                f'<div class="day-stack" style="height: {height}px">'
                f'<div class="day-seg opus" style="height: {height}px" title="${cost:.4f}"></div>'
                f"</div>"
                f'<div class="day-label">{day_short}</div>'
                f"</div>"
            )
        daily_chart = f'<div class="day-chart">{"".join(day_cols)}</div>'
    else:
        daily_chart = '<p class="empty-note">No runs this month.</p>'

    # Top runs
    if d.top_runs:
        top_rows = []
        for r in d.top_runs:
            ts_short = r.ts.strftime("%b %d · %H:%M")
            tags = ""
            if r.fallback:
                tags += '<span class="pill fallback">fallback</span>'
            if r.trigger == "helper":
                trigger_pill = '<span class="pill helper">helper</span>'
            else:
                trigger_pill = html.escape(r.trigger)
            top_rows.append(
                f"<tr>"
                f'<td class="num">{ts_short}</td>'
                f"<td>{trigger_pill}</td>"
                f"<td>{_model_pill(r.model)}{tags}</td>"
                f'<td class="right num">{r.input_tokens:,} / {r.output_tokens:,}</td>'
                f'<td class="right num"><strong>${r.cost_usd:.4f}</strong></td>'
                f"<td>{html.escape(_truncate(r.summary, 60))}</td>"
                f"</tr>"
            )
        top_table = (
            "<table>"
            "<thead><tr><th>Time</th><th>Trigger</th><th>Model</th>"
            '<th class="right">Tokens (in/out)</th><th class="right">Cost</th>'
            "<th>Summary</th></tr></thead>"
            f"<tbody>{''.join(top_rows)}</tbody>"
            "</table>"
        )
    else:
        top_table = '<p class="empty-note">No runs this month.</p>'

    # Helper savings card
    if d.helper_savings and d.helper_savings.helper_calls > 0:
        hs = d.helper_savings
        helper_html = (
            f'<div class="savings-card">'
            f'<div class="savings-headline">${hs.saved:.4f} saved</div>'
            f'<div class="savings-detail">'
            f"{hs.helper_calls} helper call{'s' if hs.helper_calls != 1 else ''} cost <strong>${hs.helper_actual_cost:.4f}</strong>. "
            f"Same work on the parent's main model would have cost <strong>${hs.hypothetical_main_cost:.4f}</strong>. "
            f"That's a <strong>{hs.cost_ratio:.1f}×</strong> cost ratio on the helper-handled portion."
            f"</div>"
            f"</div>"
        )
    else:
        helper_html = '<p class="empty-note">No helper calls this month.</p>'

    # Model mix table
    model_bar_html = _render_model_mix_bar(s.cost_by_model, s.cost_usd)

    # Suggested caps
    if d.suggested_caps:
        sc = d.suggested_caps
        caps_html = (
            f"<p>Based on {sc['based_on_days']} days of observed usage:</p>"
            f'<ul style="margin-left: 20px; line-height: 1.8;">'
            f"<li>Average daily: <strong>${sc['avg_daily']:.4f}</strong></li>"
            f"<li>P95 daily: <strong>${sc['p95_daily']:.4f}</strong></li>"
            f"<li>Projected monthly: <strong>${sc['projected_monthly']:.4f}</strong></li>"
            f"</ul>"
            f'<p style="margin-top: 12px;">Suggested caps:</p>'
            f'<ul style="margin-left: 20px; line-height: 1.8;">'
            f"<li>Daily: <strong>${sc['suggested_daily_cap_usd']:.2f}</strong> (3× avg)</li>"
            f"<li>Monthly: <strong>${sc['suggested_monthly_cap_usd']:.2f}</strong> (1.5× projected)</li>"
            f"</ul>"
            f'<p class="muted" style="margin-top: 12px;">'
            f"Set these in <code>model.md</code> under <code>cost_guardrails</code> "
            f"and flip <code>enabled: true</code>.</p>"
        )
    else:
        caps_html = (
            '<p class="empty-note">'
            "Need ~14 days of data before suggesting caps. "
            "Run the agent for a couple of weeks; come back here."
            "</p>"
        )

    # Cache savings line
    if d.cache_savings_usd > 0:
        cache_line = f"You saved <strong>${d.cache_savings_usd:.4f}</strong> this month by prompt caching."
    else:
        cache_line = '<span class="muted">No cache hits recorded yet.</span>'

    _agent_name_safe = html.escape(d.name)
    _agent_degraded_banner = (
        '<div class="degraded-banner">'
        '<span class="pill warn">⚠ data may be incomplete</span>'
        " &nbsp;One or more log reads failed for this agent. The figures"
        " below reflect partial data only — some costs may be understated."
        "</div>"
        if d.cost_data_degraded
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_SHARED_CSP}">
<title>{_agent_name_safe} — Atomic Agents</title>
<style>{CSS}</style>
</head>
<body>

<header>
  <div>
    <div class="breadcrumb"><a href="../_dashboard/index.html">&#8592; All agents</a></div>
    <h1>{_agent_name_safe}</h1>
    <div class="period">{html.escape(d.period_label)} · as of {date.today().isoformat()}</div>
  </div>
  <div>
    <button class="refresh-btn" onclick="refresh()">↻ Refresh</button>
  </div>
</header>

{_agent_degraded_banner}
<section class="kpis">
  <div class="kpi">
    <div class="value">${s.cost_usd:.4f}</div>
    <div class="label">Spend this month</div>
  </div>
  <div class="kpi">
    <div class="value">{s.runs}</div>
    <div class="label">Total runs</div>
  </div>
  <div class="kpi">
    <div class="value">{s.cache_hit_pct}%</div>
    <div class="label">Cache hit rate</div>
  </div>
  <div class="kpi">
    <div class="value">{s.errors}</div>
    <div class="label">Errors</div>
  </div>
</section>

<section class="panel">
  <h2>Daily cost · this month</h2>
  {daily_chart}
  <p class="muted" style="margin-top: 12px;">{cache_line}</p>
</section>

<div class="grid-2">
  <section class="panel">
    <h2>Model mix · this month</h2>
    {model_bar_html}
  </section>

  <section class="panel">
    <h2>Helper savings · this month</h2>
    {helper_html}
  </section>
</div>

<section class="panel">
  <h2>Suggested cost caps</h2>
  {caps_html}
</section>

<section class="panel">
  <h2>Top {len(d.top_runs)} most expensive runs</h2>
  {top_table}
</section>

<footer>
  <div>Generated {date.today().isoformat()} by atomic_agents.dashboard</div>
  <div>Aggregated from log/YYYY-MM/*.jsonl</div>
</footer>

<script>
function refresh() {{
  fetch('/regenerate', {{method: 'POST'}})
    .then(r => {{ if (r.ok) location.reload(); else fallback(); }})
    .catch(fallback);
}}
function fallback() {{ location.reload(); }}
</script>

</body>
</html>
"""


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"

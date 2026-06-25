"""HTML rendering for the Fleet Console and cost dashboard.

Pure-Python templates (no Jinja2) for portability and to keep deps minimal.
The HTML is deliberately matched to samples/caldwell/dashboard.html — that's
the visual contract.

Self-contained output: inline CSS, no external assets, no JavaScript
dependencies. Opens in any browser. Refresh button only does anything
when the optional Flask server (serve.py) is running.

BEHAVIOR CHANGE (spec/52 PR1): The dashboard home page (GET /) now serves the
Fleet Console Attention Queue. The cost view has moved to GET /cost (cost.html).
Direct links to /_dashboard/index.html now land on the console home.
This is a named backward-compat callout — see spec/52 §'Migration notes' and
CHANGELOG [Unreleased] §'BEHAVIOR CHANGE'.
"""

from __future__ import annotations
import html
import json
import logging
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

logger = logging.getLogger(__name__)

# The console home is always index.html (the new front door).
# The cost view moves to cost.html.
_CONSOLE_HOME = "index.html"
_COST_VIEW = "cost.html"


# ──────────────────────────────────────────────────────────────────
# Public entry points


def render_all(
    agents_root: Path,
    today: date | None = None,
    tab: str = "all",
) -> dict:
    """Render the fleet console + cost dashboard + per-agent dashboards + tabs.

    tab: "all" (default) renders everything; or one of
         "console" | "cost" | "activity" | "quality" | "memory" | "goals"
         to render only that tab (useful for fast iteration).

    tab='console' renders index.html (console home) only — skips the cost
    aggregation to keep it fast. tab='cost' renders cost.html only (NOT
    index.html — the console home is only updated on 'all' or 'console' runs).

    Returns a dict with paths of files written, for caller logging.
    """
    from datetime import datetime, timezone
    from .activity import aggregate_activity, render_activity
    from .quality import aggregate_quality, render_quality
    from .memory import aggregate_memory, render_memory
    from .goals import aggregate_goals, render_goals, has_any_goal
    from .attention import aggregate_console, QualitySignal

    today = today or date.today()
    now = datetime.now(tz=timezone.utc)
    written: dict = {"global": None, "per_agent": []}

    render_console_tab = tab in ("all", "console")
    render_cost = tab in ("all", "cost")
    render_activity_tab = tab in ("all", "activity")
    render_quality_tab = tab in ("all", "quality")
    render_memory_tab = tab in ("all", "memory")
    render_goals_tab = tab in ("all", "goals")

    # Render the cost view first so quality signals can be shared with the
    # console aggregation on full renders (avoids a second evals/ read).
    quality_signals = None

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
        # Extract quality signals for the console so we don't re-read evals/
        # on a full render (progressive disclosure Principle #6). The aggregate's
        # per-agent trends live on `quality_data.eval_trends` (a list of
        # AgentEvalTrend) — NOT a `.agents` attribute. No broad except here: if
        # `aggregate_quality()` returned, `eval_trends` is a guaranteed dataclass
        # field, so a binding error means a real rename and MUST fail loud rather
        # than silently disabling the share and falling back to a second read.
        quality_signals = [
            QualitySignal(
                agent=t.agent,
                latest_score=t.latest_score,
                delta_30d=t.delta_30d,
            )
            for t in quality_data.eval_trends
        ]

    if render_memory_tab:
        memory_data = aggregate_memory(agents_root, today=today, now=now)
        memory_path = render_memory(agents_root, memory_data)
        written["memory"] = str(memory_path)

    if render_goals_tab and has_any_goal(agents_root):
        goals_data = aggregate_goals(agents_root, today=today, now=now)
        goals_path = render_goals(agents_root, goals_data)
        if goals_path:
            written["goals"] = str(goals_path)

    if render_console_tab:
        console_data = aggregate_console(
            agents_root, today=today, quality_signals=quality_signals
        )
        console_path = render_console(agents_root, console_data)
        written["console"] = str(console_path)

    return written


def render_global(agents_root: Path, summary: GlobalSummary) -> Path:
    """Render <agents_root>/_dashboard/cost.html. Returns the written path.

    BEHAVIOR CHANGE (spec/52 PR1): previously wrote index.html; now writes
    cost.html so the console home can occupy index.html. The serve.py routing
    and nav_bar() href are updated to match.
    """
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    has_goals = any(
        (agents_root / agent / "goal.md").exists()
        for agent in discover_agents(agents_root)
    )
    html_content = _render_global_template(summary, has_goals=has_goals)
    out_path = out_dir / _COST_VIEW
    atomic_write(out_path, html_content)

    # Pre-aggregated JSON for fast page-load by future versions
    data_dir = out_dir / "data"
    data_dir.mkdir(exist_ok=True)
    label_safe = summary.period_label.replace(" ", "_").lower()
    atomic_write(
        data_dir / f"{label_safe}.json", json.dumps(to_json_dict(summary), indent=2)
    )

    return out_path


def render_console(agents_root: Path, console_data) -> Path:
    """Render <agents_root>/_dashboard/index.html (the Fleet Console home).

    This is the new landing page (spec/52 PR1). Writes the rendered alert_keys
    sidecar at _console/rendered_alert_keys.json for POST /alerts/ack validation.
    Returns the written path.

    spec/53 PR2: compute_fleet_health() is called here (NOT inside
    _render_console_template) so the template stays a pure HTML formatter.
    If the advisor fails for any reason, console_data.fleet_health stays None
    and the header band is absent — the PR1 axis panels render normally.
    """
    from .._io import atomic_write

    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    has_goals = any(
        (agents_root / agent / "goal.md").exists()
        for agent in discover_agents(agents_root)
    )

    # ── spec/53 PR2: Fleet Health Score ──────────────────────────────
    # Compute fleet health score before rendering. Fail-soft: the PR1 console
    # renders fully even when the advisor is unavailable.
    if console_data.fleet_health is None:
        try:
            from ..advisor.score import compute_fleet_health

            console_data.fleet_health = compute_fleet_health(agents_root)
        except Exception as exc:
            logger.warning(
                "advisor.compute_fleet_health failed (%s); rendering without health band",
                type(exc).__name__,
            )
            console_data.fleet_health = None

    html_content = _render_console_template(console_data, has_goals=has_goals)
    out_path = out_dir / _CONSOLE_HOME
    atomic_write(out_path, html_content)

    # Persist rendered alert_keys for closed-allowlist validation in POST handlers.
    # Written atomically after a successful render so the sidecar is absent only
    # before the first render (not after a failed one). POST /alerts/ack reads this
    # file; if absent it returns 503 (spec/52 MUST 4 closed-allowlist).
    _write_rendered_alert_keys(agents_root, console_data.rendered_alert_keys)

    return out_path


def _write_rendered_alert_keys(agents_root: Path, keys: frozenset) -> None:
    """Write the currently-rendered alert_keys to the _console/ sidecar JSON.

    Called by render_console() after every successful render. The POST ack/snooze
    handlers read this sidecar to validate the submitted alert_key against the
    closed allowlist (spec/52 MUST 4). Written atomically; absent before the first
    render — POST returns 503 in that case rather than accepting all keys.
    """
    console_dir = agents_root / "_console"
    console_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = console_dir / "rendered_alert_keys.json"
    atomic_write(sidecar_path, json.dumps(sorted(keys), indent=2))


def render_agent(agents_root: Path, data: AgentDashboardData) -> Path:
    """Render <agents_root>/<agent>/dashboard.html. Returns the written path."""
    html_content = _render_agent_template(data)
    out_path = agents_root / data.name / "dashboard.html"
    atomic_write(out_path, html_content)
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
    <div class="breadcrumb"><a href="../_dashboard/index.html">&#8592; Fleet Console</a></div>
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


# ──────────────────────────────────────────────────────────────────
# Fleet Console template


_CONSOLE_CSS_EXTRA = """
/* Fleet Console — attention queue + three-axis trend panels */
.attention-queue { margin-bottom: 24px; }
.queue-empty {
  padding: 32px; text-align: center; color: var(--good);
  font-size: 15px; background: var(--card); border: 1px solid var(--border);
  border-radius: 10px;
}
.alert-row { display: grid; grid-template-columns: 80px 1fr 120px 200px 100px; gap: 12px;
  align-items: start; padding: 12px 16px; border-bottom: 1px solid var(--border);
  font-size: 13px; }
.alert-row:last-child { border-bottom: none; }
.alert-row:hover { background: rgba(78, 201, 176, 0.03); }
.alert-header { display: grid; grid-template-columns: 80px 1fr 120px 200px 100px; gap: 12px;
  padding: 8px 16px; font-size: 11px; font-weight: 500; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border); }
.sev-critical { color: var(--error); font-weight: 700; }
.sev-high { color: var(--warn); font-weight: 600; }
.sev-medium { color: var(--accent); }
.sev-low { color: var(--muted); }
.sev-info { color: var(--muted); font-style: italic; }
.status-new { color: var(--error); font-size: 10px; font-weight: 700; text-transform: uppercase; }
.status-recurring { color: var(--warn); font-size: 10px; font-weight: 600; text-transform: uppercase; }
.status-known { color: var(--muted); font-size: 10px; text-transform: uppercase; }
.alert-actions { display: flex; gap: 6px; }
.alert-btn {
  background: var(--card); color: var(--muted); border: 1px solid var(--border);
  padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: 11px;
}
.alert-btn:hover { border-color: var(--accent); color: var(--accent); }
.alert-btn.acked { color: var(--good); border-color: var(--good); }

/* Three-axis panels */
.axis-panels { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 24px; }
.axis-panel { background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 20px; }
.axis-title { font-size: 12px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
.axis-row { display: flex; justify-content: space-between; align-items: center;
  padding: 6px 0; border-bottom: 1px solid rgba(42, 50, 61, 0.5); font-size: 13px; }
.axis-row:last-child { border-bottom: none; }
.axis-val { font-variant-numeric: tabular-nums; font-weight: 500; }
.axis-spike { color: var(--error); }
.axis-ok { color: var(--good); }
.axis-warn { color: var(--warn); }
.axis-muted { color: var(--muted); }

/* Agent card grid */
.agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.agent-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; font-size: 13px;
}
.agent-card a { color: var(--accent); text-decoration: none; font-weight: 500; }
.agent-card a:hover { text-decoration: underline; }
.agent-card .meta { color: var(--muted); font-size: 12px; margin-top: 6px; }
.agent-card .alerts-badge {
  display: inline-block; background: var(--error); color: #fff;
  border-radius: 10px; padding: 1px 7px; font-size: 10px;
  font-weight: 700; margin-left: 6px;
}
.snooze-modal {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
  align-items: center; justify-content: center; z-index: 100;
}
.snooze-modal.open { display: flex; }
.snooze-box {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 24px; min-width: 320px;
}
.snooze-box h3 { margin-bottom: 12px; font-size: 15px; }
.snooze-options { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
.snooze-btn {
  background: transparent; border: 1px solid var(--border); color: var(--text);
  padding: 8px 12px; border-radius: 6px; cursor: pointer; text-align: left; font-size: 13px;
}
.snooze-btn:hover { border-color: var(--accent); }
.snooze-cancel {
  background: transparent; border: none; color: var(--muted);
  cursor: pointer; font-size: 13px; padding: 0;
}

/* Fleet Health header band (spec/53 PR2) */
.health-band {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 20px 24px; margin-bottom: 24px;
}
.health-band-header {
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
  margin-bottom: 16px;
}
.health-score-composite {
  font-size: 36px; font-weight: 700; line-height: 1;
}
.health-band-green .health-score-composite { color: var(--good); }
.health-band-amber .health-score-composite { color: var(--warn); }
.health-band-red .health-score-composite { color: var(--error); }
.health-band-unknown .health-score-composite { color: var(--muted); }
.health-coverage { color: var(--muted); font-size: 13px; }
.health-sub-chips { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.health-chip {
  display: flex; flex-direction: column; align-items: center;
  background: rgba(255,255,255,0.04); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 16px; min-width: 100px;
}
.health-chip-label { font-size: 10px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.05em; margin-bottom: 4px; }
.health-chip-value { font-size: 20px; font-weight: 600; }
.health-chip-green .health-chip-value { color: var(--good); }
.health-chip-amber .health-chip-value { color: var(--warn); }
.health-chip-red .health-chip-value { color: var(--error); }
.health-chip-unknown .health-chip-value { color: var(--muted); }
.health-scorecard { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
.health-scorecard th {
  text-align: left; color: var(--muted); font-weight: 500; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.05em;
  padding: 6px 10px; border-bottom: 1px solid var(--border);
}
.health-scorecard td { padding: 6px 10px; border-bottom: 1px solid rgba(42,50,61,0.4); }
.health-scorecard tr:last-child td { border-bottom: none; }
.health-no-data { color: var(--muted); font-style: italic; }
.health-degraded { color: var(--warn); font-style: italic; }
.health-defaults-note { font-size: 11px; color: var(--muted); margin-top: 8px; }
.wow-good { color: var(--good); }
.wow-bad { color: var(--error); }
.wow-flat { color: var(--muted); }
"""


def _severity_class(severity: str) -> str:
    return {
        "critical": "sev-critical",
        "high": "sev-high",
        "medium": "sev-medium",
        "low": "sev-low",
        "info": "sev-info",
    }.get(severity, "sev-info")


def _queue_status_html(status: str) -> str:
    cls = {
        "new": "status-new",
        "recurring": "status-recurring",
        "known": "status-known",
    }.get(status, "status-known")
    return f'<span class="{cls}">{html.escape(status)}</span>'


def _render_health_band(fleet_health) -> str:
    """Render the Fleet Health header band for spec/53 PR2.

    Always called from _render_console_template(); returns an empty string when
    fleet_health is None (fail-soft absent, not a crash).
    All HTML generation lives here — the advisor module exports only dataclasses.
    """
    if fleet_health is None:
        return ""

    # Band CSS class
    band = getattr(fleet_health, "fleet_band", "unknown")
    band_cls = f"health-band health-band-{band}"

    # Composite score display
    composite = getattr(fleet_health, "fleet_composite", None)
    coverage_n = getattr(fleet_health, "coverage_n", 0)
    coverage_m = getattr(fleet_health, "coverage_m", 0)
    worst_agent = getattr(fleet_health, "worst_agent", None)
    worst_composite = getattr(fleet_health, "worst_agent_composite", None)
    fh_degraded = getattr(fleet_health, "degraded", False)
    used_defaults = getattr(fleet_health, "used_targets_defaults", False)

    if composite is not None:
        score_html = f'<span class="health-score-composite">{composite:.0f}</span>'
        coverage_html = (
            f'<span class="health-coverage">Health {composite:.0f}'
            f" | Coverage {coverage_n}/{coverage_m}"
            + (
                f" | Worst: {html.escape(worst_agent)} ({worst_composite:.0f})"
                if worst_agent
                else ""
            )
            + "</span>"
        )
    else:
        score_html = (
            '<span class="health-score-composite" style="color:var(--muted)">—</span>'
        )
        coverage_html = (
            f'<span class="health-coverage">insufficient data'
            f" | Coverage {coverage_n}/{coverage_m}</span>"
        )

    # Sub-score chips (cost / quality / reliability)
    agents = getattr(fleet_health, "agents", [])
    axis_scores: dict[str, list[float]] = {"cost": [], "quality": [], "reliability": []}
    for ah in agents:
        cs = getattr(ah, "cost_score", None)
        qs = getattr(ah, "quality_score", None)
        rs = getattr(ah, "reliability_score", None)
        if cs is not None:
            axis_scores["cost"].append(cs)
        if qs is not None:
            axis_scores["quality"].append(qs)
        if rs is not None:
            axis_scores["reliability"].append(rs)

    def _chip(label: str, axis: str) -> str:
        vals = axis_scores.get(axis, [])
        if not vals:
            return (
                f'<div class="health-chip health-chip-unknown">'
                f'<div class="health-chip-label">{html.escape(label)}</div>'
                f'<div class="health-chip-value" style="color:var(--muted);font-size:14px;">no data</div>'
                f"</div>"
            )
        mean_v = sum(vals) / len(vals)
        chip_band = "green" if mean_v >= 80 else ("amber" if mean_v >= 60 else "red")
        return (
            f'<div class="health-chip health-chip-{chip_band}">'
            f'<div class="health-chip-label">{html.escape(label)}</div>'
            f'<div class="health-chip-value">{mean_v:.0f}</div>'
            f"</div>"
        )

    chips_html = (
        '<div class="health-sub-chips">'
        + _chip("Cost", "cost")
        + _chip("Quality", "quality")
        + _chip("Reliability", "reliability")
        + "</div>"
    )

    # Scorecard table — aggregate all per-agent scorecard rows by metric
    # Show fleet-mean value, target, mean score, and WoW summary
    from collections import defaultdict

    metric_values: dict[str, list] = defaultdict(list)
    metric_targets: dict[str, float | None] = {}
    metric_scores: dict[str, list[float]] = defaultdict(list)
    metric_wows: dict[str, list[str]] = defaultdict(list)
    metric_axis: dict[str, str] = {}

    for ah in agents:
        for row in getattr(ah, "scorecard", []):
            key = row.metric
            metric_axis[key] = row.axis
            if row.target is not None:
                metric_targets[key] = row.target
            if row.value is not None:
                metric_values[key].append(row.value)
            if row.score is not None:
                metric_scores[key].append(row.score)
            if row.wow is not None:
                metric_wows[key].append(row.wow)

    def _wow_symbol(wows: list[str], direction: str) -> str:
        """Render the aggregated WoW arrow, colored by GOODNESS not raw direction.

        The glyph reflects raw movement (↑ = the metric value rose, ↓ = it fell).
        The color reflects whether that movement is good or bad for THIS metric:
        for a 'lower'-is-better metric (error_rate, spend_vs_trend, ...) a rising
        value is BAD (red), a falling value is GOOD (green) — the inverse of a
        'higher'-is-better metric (pass_rate, cheaper_model_share). Coloring on raw
        direction alone would paint a rising error rate green (spec/53 §6).
        """
        if not wows:
            return '<span class="wow-flat">—</span>'
        ups = wows.count("up")
        downs = wows.count("down")
        if ups == downs:
            return '<span class="wow-flat">→</span>'
        rose = ups > downs
        glyph = "↑" if rose else "↓"
        # good = the movement improves the metric.
        if direction == "lower":
            good = not rose  # falling is good for lower-is-better
        else:
            good = rose  # rising is good for higher-is-better
        cls = "wow-good" if good else "wow-bad"
        return f'<span class="{cls}">{glyph}</span>'

    scorecard_rows_html = ""
    # Ordered display: reliability first, then quality, then cost.
    # Each entry carries the metric's optimization direction ('higher'|'lower') so
    # the WoW arrow is colored by goodness, not by raw value direction (spec/53 §6).
    # Six of eight metrics are lower-is-better; only pass_rate and
    # cheaper_model_share are higher-is-better.
    #
    # SCOPE NOTE (spec/53 §6, PR2): these directions are the DEFAULT directions. An
    # operator who overrides a metric's `direction` in targets.md changes how the
    # SCORE column is computed (targets.py _parse_metric), but the WoW arrow COLOR
    # here still uses the default direction below — the two can diverge for an
    # overridden metric. Honoring a targets.md direction override in the WoW color
    # (threading MetricTarget.direction through ScorecardRow into the render) is
    # deferred to the recommendations PR (#616); PR2 colors WoW on defaults only.
    display_order = [
        ("reliability", "error_rate", "lower"),
        ("reliability", "blocked_rate", "lower"),
        ("reliability", "skipped_rate", "lower"),
        ("quality", "pass_rate", "higher"),
        ("quality", "hard_fail_rate", "lower"),
        ("cost", "cheaper_model_share", "higher"),
        ("cost", "tokens_per_output", "lower"),
        ("cost", "spend_vs_trend", "lower"),
    ]
    for axis_name, metric_name, direction in display_order:
        vals = metric_values.get(metric_name, [])
        scores = metric_scores.get(metric_name, [])
        tgt = metric_targets.get(metric_name)
        wows = metric_wows.get(metric_name, [])

        val_str = (
            f"{sum(vals) / len(vals):.3f}"
            if vals
            else '<span class="health-no-data">no data</span>'
        )
        tgt_str = f"{tgt:.3f}" if tgt is not None else "—"
        score_str = (
            f"{sum(scores) / len(scores):.0f}"
            if scores
            else '<span class="health-no-data">—</span>'
        )
        wow_str = _wow_symbol(wows, direction)

        scorecard_rows_html += (
            f"<tr>"
            f"<td>{html.escape(axis_name)}</td>"
            f"<td>{html.escape(metric_name)}</td>"
            f"<td>{val_str}</td>"
            f"<td>{tgt_str}</td>"
            f"<td>{score_str}</td>"
            f"<td>{wow_str}</td>"
            f"</tr>"
        )

    scorecard_html = (
        '<table class="health-scorecard">'
        "<thead><tr>"
        "<th>Axis</th><th>Metric</th><th>Value (fleet mean)</th>"
        "<th>Target</th><th>Score</th><th>WoW</th>"
        "</tr></thead>"
        f"<tbody>{scorecard_rows_html}</tbody>"
        "</table>"
    )

    degraded_note = (
        '<div class="degraded-banner" style="margin-top:8px;">'
        '<span class="pill warn">⚠ scoring data may be incomplete</span>'
        " One or more axis reads degraded — affected sub-scores excluded from composite."
        "</div>"
        if fh_degraded
        else ""
    )

    defaults_note = (
        '<div class="health-defaults-note">Some scoring targets using defaults (targets.md absent or partially missing).</div>'
        if used_defaults
        else ""
    )

    return (
        f'<div class="{band_cls}">'
        f'<div class="health-band-header">{score_html}{coverage_html}</div>'
        f"{chips_html}"
        f"{scorecard_html}"
        f"{degraded_note}"
        f"{defaults_note}"
        f"</div>"
    )


def _render_console_template(console_data, has_goals: bool = True) -> str:
    """Fleet Console home page HTML — the new index.html landing page (spec/52 PR1)."""
    from datetime import datetime, timezone

    _nav = _nav_bar("console", has_goals=has_goals)
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    _degraded_banner = (
        '<div class="degraded-banner">'
        '<span class="pill warn">⚠ data may be incomplete</span>'
        " &nbsp;One or more backend reads failed. Metrics below may be partial."
        "</div>"
        if console_data.degraded
        else ""
    )

    # ── Attention Queue ─────────────────────────────────────────────
    queue = console_data.attention_queue
    # Show only open + recurring items at the top; acked/snoozed shown muted
    active_items = [a for a in queue if a.ack_snooze_status == "open"]
    known_items = [a for a in queue if a.ack_snooze_status in ("acked", "snoozed")]

    if not queue:
        queue_html = (
            '<div class="queue-empty">'
            "✓ All agents healthy — no items need attention."
            "</div>"
        )
    else:
        rows = []
        for item in active_items + known_items:
            muted_style = (
                ' style="opacity: 0.5;"' if item.ack_snooze_status != "open" else ""
            )
            sev_cls = _severity_class(item.severity)
            agent_safe = html.escape(item.agent)
            reason_safe = html.escape(item.reason)
            next_step_safe = html.escape(item.next_step)
            owner_safe = html.escape(item.owner or "—")
            key_safe = html.escape(item.alert_key)
            ack_label = "Acked" if item.ack_snooze_status == "acked" else "Ack"
            ack_btn_cls = (
                "alert-btn acked" if item.ack_snooze_status == "acked" else "alert-btn"
            )
            rows.append(
                f'<div class="alert-row"{muted_style}>'
                f'<div class="{sev_cls}">{html.escape(item.severity.upper())}'
                f"<br>{_queue_status_html(item.status)}</div>"
                f"<div><strong>{agent_safe}</strong>"
                f'<div class="muted">{reason_safe}</div>'
                f'<div class="muted" style="margin-top:4px;font-size:11px;">'
                f"Next: {next_step_safe}</div></div>"
                f"<div>{owner_safe}</div>"
                f'<div class="muted">{html.escape(item.alert_class)}'
                f"/{html.escape(item.alert_subclass)}</div>"
                f'<div class="alert-actions">'
                f'<button class="{ack_btn_cls}" onclick="ackAlert(\'{key_safe}\')">{ack_label}</button>'
                f'<button class="alert-btn" onclick="openSnooze(\'{key_safe}\')">Snooze</button>'
                f"</div>"
                f"</div>"
            )
        queue_html = (
            '<div class="attention-queue">'
            '<div class="alert-header">'
            "<div>Severity</div><div>Agent / Reason</div>"
            "<div>Owner</div><div>Class</div><div>Actions</div>"
            "</div>" + "".join(rows) + "</div>"
        )

    # ── Three-axis trend panels ─────────────────────────────────────
    # Cost trend
    if console_data.cost_trends:
        cost_rows = []
        for ct in console_data.cost_trends[:8]:
            spike_html = (
                ' <span class="axis-spike">▲spike</span>' if ct.spike_detected else ""
            )
            agent_safe = html.escape(ct.agent)
            cost_rows.append(
                f'<div class="axis-row">'
                f"<div>{agent_safe}</div>"
                f'<div class="axis-val">${ct.total_usd_30d:.3f}/30d{spike_html}</div>'
                f"</div>"
            )
        cost_panel = (
            '<div class="axis-panel">'
            '<div class="axis-title">Cost · 30-day total</div>'
            + "".join(cost_rows)
            + "</div>"
        )
    else:
        cost_panel = (
            '<div class="axis-panel">'
            '<div class="axis-title">Cost · 30-day total</div>'
            '<p class="empty-note">No cost data.</p>'
            "</div>"
        )

    # Quality trend
    if console_data.quality_signals:
        qual_rows = []
        for qs in sorted(
            console_data.quality_signals,
            key=lambda q: q.latest_score or 0,
        )[:8]:
            if qs.latest_score is None:
                score_html = '<span class="axis-muted">no evals</span>'
            else:
                delta_str = ""
                if qs.delta_30d is not None:
                    sign = "+" if qs.delta_30d >= 0 else ""
                    delta_cls = "axis-ok" if qs.delta_30d >= 0 else "axis-spike"
                    delta_str = (
                        f' <span class="{delta_cls}">{sign}{qs.delta_30d:.2f}</span>'
                    )
                score_html = (
                    f'<span class="axis-val">{qs.latest_score:.2f}{delta_str}</span>'
                )
            qual_rows.append(
                f'<div class="axis-row">'
                f"<div>{html.escape(qs.agent)}</div>"
                f"<div>{score_html}</div>"
                f"</div>"
            )
        quality_panel = (
            '<div class="axis-panel">'
            '<div class="axis-title">Quality · eval score (30d delta)</div>'
            + "".join(qual_rows)
            + "</div>"
        )
    else:
        quality_panel = (
            '<div class="axis-panel">'
            '<div class="axis-title">Quality · eval score (30d delta)</div>'
            '<p class="empty-note">No eval data yet.</p>'
            "</div>"
        )

    # Reliability trend
    if console_data.reliability_metrics:
        rel_rows = []
        for rm in sorted(
            console_data.reliability_metrics,
            key=lambda r: -r.error_rate,
        )[:8]:
            if rm.total_runs == 0:
                val_html = '<span class="axis-muted">no runs</span>'
            else:
                err_pct = int(rm.error_rate * 100)
                blk_pct = int(rm.blocked_rate * 100)
                err_cls = "axis-spike" if rm.error_rate >= 0.2 else "axis-ok"
                val_html = (
                    f'<span class="axis-val">'
                    f'<span class="{err_cls}">{err_pct}% err</span>'
                    f" · {blk_pct}% blk"
                    f"</span>"
                )
            rel_rows.append(
                f'<div class="axis-row">'
                f"<div>{html.escape(rm.agent)}</div>"
                f"<div>{val_html}</div>"
                f"</div>"
            )
        reliability_panel = (
            '<div class="axis-panel">'
            '<div class="axis-title">Reliability · error / blocked rate (30d)</div>'
            + "".join(rel_rows)
            + "</div>"
        )
    else:
        reliability_panel = (
            '<div class="axis-panel">'
            '<div class="axis-title">Reliability · error / blocked rate (30d)</div>'
            '<p class="empty-note">No run data yet.</p>'
            "</div>"
        )

    # ── Agent Card Grid ─────────────────────────────────────────────
    alerts_by_agent: dict[str, int] = {}
    for item in active_items:
        alerts_by_agent[item.agent] = alerts_by_agent.get(item.agent, 0) + 1

    agent_names = sorted(
        set(
            [ct.agent for ct in console_data.cost_trends]
            + [rm.agent for rm in console_data.reliability_metrics]
        )
    )

    if agent_names:
        cards = []
        for agent in agent_names:
            agent_safe = html.escape(agent)
            agent_href = urllib.parse.quote(agent, safe="")
            n_alerts = alerts_by_agent.get(agent, 0)
            badge = (
                f'<span class="alerts-badge">{n_alerts}</span>' if n_alerts > 0 else ""
            )
            ct = next((c for c in console_data.cost_trends if c.agent == agent), None)
            cost_str = f"${ct.total_usd_30d:.3f}/30d" if ct else "no cost data"
            cards.append(
                f'<div class="agent-card">'
                f'<a href="../{agent_href}/dashboard.html">{agent_safe}</a>{badge}'
                f'<div class="meta">{html.escape(cost_str)}</div>'
                f"</div>"
            )
        agent_grid_html = f'<div class="agent-grid">{"".join(cards)}</div>'
    else:
        agent_grid_html = '<p class="empty-note">No agents discovered.</p>'

    # ── Snooze Modal ────────────────────────────────────────────────
    snooze_modal = """
<div class="snooze-modal" id="snoozeModal">
  <div class="snooze-box">
    <h3>Snooze alert</h3>
    <div class="snooze-options">
      <button class="snooze-btn" onclick="snoozeFor(4)">Snooze 4 hours</button>
      <button class="snooze-btn" onclick="snoozeFor(24)">Snooze 24 hours</button>
      <button class="snooze-btn" onclick="snoozeFor(72)">Snooze 3 days</button>
      <button class="snooze-btn" onclick="snoozeFor(168)">Snooze 1 week</button>
    </div>
    <button class="snooze-cancel" onclick="closeSnooze()">Cancel</button>
  </div>
</div>
"""

    active_count = len(active_items)
    fleet_size = console_data.agent_count

    # ── Fleet Health header band (spec/53 PR2) ────────────────────────
    # Rendered ABOVE the three axis panels. Absent (empty string) when
    # fleet_health is None — the PR1 panels render normally either way.
    health_band_html = _render_health_band(getattr(console_data, "fleet_health", None))
    health_band_section = (
        f"\n<h2>Fleet Health Score</h2>\n{health_band_html}\n"
        if health_band_html
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_SHARED_CSP}">
<title>Atomic Agents — Fleet Console</title>
<style>{CSS}{_CONSOLE_CSS_EXTRA}</style>
</head>
<body>

<header>
  <div>
    <h1>Fleet Console</h1>
    <div class="period">{fleet_size} agent{"s" if fleet_size != 1 else ""} · {now_str}</div>
  </div>
  <div>
    <button class="refresh-btn" onclick="refresh()">&#8635; Refresh</button>
  </div>
</header>

{_nav}
{_degraded_banner}
{health_band_section}
<h2>Operator Attention Queue
  {'<span class="pill error" style="margin-left:8px;">' + str(active_count) + " open</span>" if active_count else '<span class="pill ok" style="margin-left:8px;">0 open</span>'}
</h2>
{queue_html}

<h2>Fleet Trends</h2>
<div class="axis-panels">
{cost_panel}
{quality_panel}
{reliability_panel}
</div>

<h2>Agent Fleet</h2>
{agent_grid_html}

<footer>
  <div>Generated {date.today().isoformat()} by atomic_agents.dashboard</div>
  <div>Fleet Console · spec/52 + spec/53</div>
</footer>

{snooze_modal}

<script>
var _snoozeKey = null;

function refresh() {{
  fetch('/regenerate', {{method: 'POST'}})
    .then(r => {{ if (r.ok) location.reload(); else fallback(); }})
    .catch(fallback);
}}
function fallback() {{ location.reload(); }}

function ackAlert(key) {{
  fetch('/alerts/ack', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{alert_key: key}})
  }}).then(r => {{ if (r.ok) location.reload(); }});
}}

function openSnooze(key) {{
  _snoozeKey = key;
  document.getElementById('snoozeModal').classList.add('open');
}}

function closeSnooze() {{
  _snoozeKey = null;
  document.getElementById('snoozeModal').classList.remove('open');
}}

function snoozeFor(hours) {{
  if (!_snoozeKey) return;
  var until = new Date(Date.now() + hours * 3600 * 1000).toISOString();
  fetch('/alerts/snooze', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{alert_key: _snoozeKey, snooze_until: until}})
  }}).then(r => {{ closeSnooze(); if (r.ok) location.reload(); }});
}}
</script>

</body>
</html>
"""

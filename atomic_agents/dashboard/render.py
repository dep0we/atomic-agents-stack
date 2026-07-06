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
    render_monitor_tab = tab in (
        "all",
        "console",
        "monitor",
    )  # monitor renders when console data is fresh
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

        # MUST 5 (double-write guard): when render_console_tab=True the console pass
        # will re-render per-agent detail pages with the SAME console_data/now snapshot
        # as the Monitor.  Rendering them here too (with a standalone snapshot) would
        # write the file twice — once without fleet_health and once with.  Skip the
        # cost-loop write so the console pass is the sole author of detail pages when
        # it will run.  When render_console_tab=False (e.g. tab='cost') the console
        # pass won't run, so we must write detail pages from the cost loop.
        if not render_console_tab:
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
        # Thread `today` AND `now` through so the health-band scoring, recommendation
        # windows, and status_for_agent() STALE checks use the SAME pinned date/time
        # as the cost/reliability aggregation and as render_monitor() (spec/56 MUST 12).
        # No #623-class midnight divergence; no staleness-boundary disagreement between
        # the home fleet-status counts and the monitor status counts.
        console_path = render_console(agents_root, console_data, today=today, now=now)
        written["console"] = str(console_path)

        # Fleet Monitor (spec/56 #653): render alongside the console home from the
        # SAME console_data so home summary counts and monitor counts agree (MUST 12).
        if render_monitor_tab:
            try:
                from .render_monitor import render_monitor as _render_monitor

                agent_list = list(discover_agents(agents_root))
                _has_goals = any(
                    (agents_root / a / "goal.md").exists() for a in agent_list
                )
                monitor_path = _render_monitor(
                    agents_root,
                    console_data,
                    today=today,
                    now=now,
                    has_goals=_has_goals,
                )
                written["monitor"] = str(monitor_path)
            except Exception as exc:
                logger.warning(
                    "render_monitor failed (%s); skipping monitor.html",
                    type(exc).__name__,
                )

        # spec/57 MUST 5 — co-render per-agent detail pages with the SAME console_data
        # (fleet_health + now) that the Monitor used so the Monitor row and the detail
        # banner for the same agent are identical by construction (shared snapshot).
        # This pass re-renders (or first-renders, if tab='console') the detail pages.
        try:
            from .render_agent_detail import (
                render_agent_detail as _render_agent_detail,
                render_agent_detail_resolver as _render_resolver,
            )

            _detail_agent_list = list(discover_agents(agents_root))
            _detail_written = []
            for _agent_id in _detail_agent_list:
                try:
                    _detail_path = _render_agent_detail(
                        agents_root,
                        _agent_id,
                        console_data=console_data,
                        today=today,
                        now=now,
                    )
                    _detail_written.append(str(_detail_path))
                except Exception as _exc:
                    logger.warning(
                        "render_agent_detail failed for '%s' (%s); skipping",
                        _agent_id,
                        type(_exc).__name__,
                    )
            # Update per_agent with the detail paths (may already have cost paths from
            # the render_cost loop; replace them with the detail paths which are the
            # same files — this ensures written["per_agent"] reflects the final state).
            if _detail_written:
                written["per_agent"] = _detail_written

            # Resolver (MUST 1) — always regenerate alongside the detail pages.
            try:
                _resolver_path = _render_resolver(agents_root)
                written["agent_detail_resolver"] = str(_resolver_path)
            except Exception as _exc:
                logger.warning(
                    "render_agent_detail_resolver failed (%s); skipping",
                    type(_exc).__name__,
                )
        except Exception as exc:
            logger.warning(
                "render_agent_detail pass failed (%s); detail pages not updated",
                type(exc).__name__,
            )

    # Generate the resolver even when render_console_tab is False (e.g. tab='cost')
    # so the _dashboard/agent-detail.html file exists whenever cost pages are written.
    if render_cost and not render_console_tab:
        try:
            from .render_agent_detail import render_agent_detail_resolver as _rr

            _rp = _rr(agents_root)
            written["agent_detail_resolver"] = str(_rp)
        except Exception as exc:
            logger.warning(
                "render_agent_detail_resolver (cost-only pass) failed (%s)",
                type(exc).__name__,
            )

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


def render_console(
    agents_root: Path,
    console_data,
    today: date | None = None,
    now=None,
) -> Path:
    """Render <agents_root>/_dashboard/index.html (the Fleet Console home).

    This is the new landing page (spec/52 PR1). Writes the rendered alert_keys
    sidecar at _console/rendered_alert_keys.json for POST /alerts/ack validation.
    Returns the written path.

    spec/53 PR2: compute_fleet_health() is called here (NOT inside panels)
    so panels receive pre-computed data via PanelContext (MUST 13 — no
    render-time backend I/O from panels).

    spec/52 §16 (Cockpit PR-A): the layout engine composes registered panels by
    slot+order; the sidecar is written from the engine-aggregated alert_keys union
    ONLY (MUST 17) — NOT from the pre-computed ConsoleData.rendered_alert_keys
    field. The attention-queue panel contributes the keys for every queue item it
    renders; if that panel raises, its keys legitimately drop from the allowlist
    (those items are not on the page, so they cannot be acked) — that is the
    intended MUST 11 fail-soft, not a reason to fall back to the seed.

    spec/56 MUST 12: accepts an optional `now` so render_all() can thread the
    SAME datetime into both render_console() and render_monitor(), guaranteeing
    they share one snapshot for status derivation (no staleness-boundary divergence).
    When called standalone (e.g. from serve.py after an ack), now defaults to a
    fresh datetime.now() — that is the correct behaviour for a single-surface render.
    """
    from datetime import datetime, timezone

    from .._io import atomic_write

    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Single reference date/time pinned once ──────────────────────────────
    # Both advisor calls and PanelContext use the SAME today/now values so
    # health-band scoring, recommendation windows, and status_for_agent()
    # STALE checks are coherent — no midnight-boundary races (#623-class).
    # `now` is accepted as a parameter so render_all() can share one clock with
    # render_monitor() (spec/56 MUST 12).
    advisor_today = today or date.today()
    now = now if now is not None else datetime.now(tz=timezone.utc)

    # ── has_goals (single discover_agents pass) ────────────────────────────
    # has_goals (for the nav bar + the only capability a panel gates on today)
    # is computed here in a single pass over the agent list (MUST 13 — all loader
    # I/O before the engine loop).
    agent_list = list(discover_agents(agents_root))
    has_goals = any((agents_root / a / "goal.md").exists() for a in agent_list)

    # ── spec/53 PR2: Fleet Health Score ──────────────────────────────────
    # Compute fleet health score BEFORE building PanelContext so panels receive
    # the computed FleetHealth via ctx.console_data.fleet_health (MUST 13).
    if console_data.fleet_health is None:
        try:
            from ..advisor.score import compute_fleet_health

            console_data.fleet_health = compute_fleet_health(
                agents_root, today=advisor_today
            )
        except Exception as exc:
            logger.warning(
                "advisor.compute_fleet_health failed (%s); rendering without health band",
                type(exc).__name__,
            )
            console_data.fleet_health = None

    # ── spec/54 PR3: Recommendations ─────────────────────────────────────
    # Compute recommendations BEFORE PanelContext so the recommendations panel
    # reads from ctx.console_data (MUST 13 — no render-time I/O from panels).
    if console_data.recommendations is None:
        try:
            from ..advisor.recommend import recommend_fleet

            console_data.recommendations = recommend_fleet(
                agents_root, today=advisor_today, fleet_health=console_data.fleet_health
            )
        except Exception as exc:
            logger.warning(
                "advisor.recommend_fleet failed (%s); rendering without recommendations",
                type(exc).__name__,
            )
            console_data.recommendations = None

    # ── Build PanelContext (all I/O complete by here) ─────────────────────
    # Importing the panels package also triggers panel registration as an import
    # side effect (spec/52 §16.5) before _render_console_template composes them.
    from .panels import ConsoleCapabilities, PanelContext

    capabilities = ConsoleCapabilities(
        has_goals=has_goals,
    )
    ctx = PanelContext(
        console_data=console_data,
        capabilities=capabilities,
        today=advisor_today,
        now=now,
    )

    # ── Compose layout via panel registry (MUST 10, MUST 11, MUST 17) ────
    html_content = _render_console_template(console_data, has_goals=has_goals, ctx=ctx)
    out_path = out_dir / _CONSOLE_HOME
    atomic_write(out_path, html_content)

    # MUST 17: write the sidecar from the engine-aggregated alert_keys union ONLY.
    # _render_console_template() stashes the union on console_data._engine_alert_keys
    # (the layout engine has no agents_root, so the write happens here). The fallback
    # is an EMPTY frozenset, never console_data.rendered_alert_keys — if the engine
    # produced no keys (e.g. the attention panel raised), the allowlist is empty by
    # construction, which is the correct MUST 11 fail-soft (an item not on the page
    # cannot be acked). Falling back to the pre-computed seed would make the engine
    # aggregation non-load-bearing and defeat MUST 17's strip-RED.
    engine_keys = getattr(console_data, "_engine_alert_keys", frozenset())
    _write_rendered_alert_keys(agents_root, engine_keys)

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
    """Render <agents_root>/<agent>/dashboard.html as the B7 detail cockpit (spec/57).

    MUST 2: path is unchanged — <agent>/dashboard.html — so render_all()["per_agent"],
    the served /agents/<name> route, and existing consumers keep working.

    Delegates to render_agent_detail() which writes the Fable "Briefing" B7 layout.
    console_data is not available at this call site (render_agent is called from the
    cost-aggregation loop); a standalone fresh snapshot is built inside
    render_agent_detail(). When render_all() calls this after populating console_data,
    the status/health come from the cost-aggregation path (standalone snapshot). For the
    co-rendered case (render_all() with console_data), see the render_all() overload
    below that calls render_agent_detail() directly with console_data threaded in.
    """
    from .render_agent_detail import render_agent_detail

    return render_agent_detail(agents_root, data.name, console_data=None)


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

/* NOTE: the per-agent card-grid CSS (.agent-grid / .agent-card / .alerts-badge)
   was REMOVED here as part of MUST 15 (D10 roster relocation) + the D4 CSS
   consolidation — the home no longer renders that grid. Those rules move to the
   Fleet Monitor (#653) when its page ships; keeping them here would be dead CSS. */
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

/* Recommendations panel (spec/54) */
.rec-panel { margin: 24px 0; }
.rec-row { border: 1px solid var(--border); border-radius: 6px; padding: 12px 16px; margin-bottom: 10px; }
.rec-header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
.rec-kind-pill { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; white-space: nowrap; }
.rec-kind-savings_cost { background: rgba(72,199,154,.14); color: var(--good); }
.rec-kind-quality_report { background: rgba(240,163,94,.14); color: var(--warn); }
.rec-kind-governance { background: rgba(240,163,94,.14); color: var(--warn); }
.rec-agent { font-weight: 600; }
.rec-models { font-size: 13px; color: var(--muted); margin-bottom: 4px; }
.rec-models .rec-arrow { margin: 0 4px; color: var(--muted); }
.rec-rationale { font-size: 13px; color: var(--text); }
.rec-deltas { font-size: 12px; color: var(--muted); margin-top: 4px; }
.rec-delta-badge { display: inline-block; padding: 1px 6px; border-radius: 4px; margin-right: 6px; }
.rec-delta-savings { background: rgba(72,199,154,.14); color: var(--good); }
.rec-delta-points { background: rgba(97,175,239,.14); color: var(--sonnet); }
"""

# B6 Cockpit CSS — D4 rule: B6 zone/KPI styles go ONLY here, never in the shared CSS block.
# Scoped to .cockpit-* and .zone-label.cockpit-zone-label so all other tabs are unaffected.
_COCKPIT_CSS = """
/* ── B6 Cockpit zone dividers ─────────────────────────────────────── */
.cockpit-zone-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  padding: 20px 0 6px;
  margin: 0 0 12px;
}

/* ── KPI hero tile strip ──────────────────────────────────────────── */
.cockpit-kpis {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 20px;
}
.cockpit-kpi {
  flex: 1 1 140px;
  min-width: 120px;
  max-width: 200px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px 12px;
}
.cockpit-kpi .k {
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 4px;
}
.cockpit-kpi .v {
  font-size: 24px;
  font-weight: 700;
  color: var(--text);
  line-height: 1.1;
}
.cockpit-kpi .sub2 {
  font-size: 11px;
  color: var(--muted);
  margin-top: 4px;
}
/* Inline 7-day cost sparkline inside the 7-day-spend KPI tile. */
.cockpit-kpi .kpi-spark {
  display: block;
  margin: 6px 0 2px;
  opacity: 0.85;
}
.cockpit-kpi.kpi-alert { border-color: var(--warn); }
.cockpit-kpi.kpi-save { border-color: var(--good); }
.cockpit-kpi.kpi-health-green { border-color: var(--good); }
.cockpit-kpi.kpi-health-amber { border-color: var(--warn); }
.cockpit-kpi.kpi-health-red { border-color: var(--error); }

/* ── Fleet Overview count grid (explore zone) ────────────────────── */
.fo-grid {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.fo-cell {
  text-align: center;
  min-width: 60px;
}
.fc-v {
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
}
.fc-k {
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 4px;
}
.fc-spike { color: var(--error); }
.fc-warn  { color: var(--warn); }
.fc-ok    { color: var(--good); }
.fc-muted { color: var(--muted); }

/* ── B6 typography helpers (matches variant-B6-zones.html) ────────── */
/* .mono: tabular monospace for KPI numerals + timestamps. The cockpit KPI tiles
   render every value as <div class="v mono">; without this rule the digits fall
   back to the proportional body font (design-gate fidelity). */
.mono {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums;
}
/* .foot-note: the Fleet-Status → Monitor link footnote (explore zone). */
.foot-note {
  font-size: 12px;
  color: var(--muted);
  margin-top: 14px;
}

/* ── Display-only "apply →" affordance (ACT zone) ────────────────────
   A static, non-interactive CTA on savings / priority-action rows. It does NOT
   POST or wire to any write endpoint — the management/write layer ships later.
   Matches the mockup's apply-cta (title="Management action — coming with the
   write layer"). Cursor stays default so it does not read as clickable. */
.apply-cta {
  display: inline-block;
  margin-left: 10px;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 8px;
  cursor: default;
  white-space: nowrap;
}

/* ── B7 CHANGE 1: Navigable KPI tiles ────────────────────────────────
   .cockpit-kpi-nav: makes a KPI tile a clickable/focusable nav target.
   Reuses --accent for hover border + subtle lift — no new color tokens.
   text-decoration: none + display:block so the entire card is the hit area. */
.cockpit-kpi-nav {
  text-decoration: none;
  color: inherit;
  display: block;
  transition: transform 0.12s ease, border-color 0.12s ease, box-shadow 0.12s ease;
  cursor: pointer;
}
.cockpit-kpi-nav:hover {
  transform: translateY(-2px);
  border-color: var(--accent) !important;
  box-shadow: 0 4px 16px rgba(78, 201, 176, 0.12);
}
.cockpit-kpi-nav:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* Nav arrow glyph inside navigable tiles — subtle, appears on hover */
.kpi-nav-arrow {
  display: inline-block;
  font-size: 10px;
  color: var(--accent);
  opacity: 0;
  margin-left: 4px;
  transition: opacity 0.12s ease;
  vertical-align: middle;
}
.cockpit-kpi-nav:hover .kpi-nav-arrow { opacity: 1; }

/* ── B7: Fleet status count cells — clickable affordance ─────────────
   .fo-cell-nav: wraps each OK/WARN/ERROR/STALE count so it reads as
   interactive and carries a tooltip. href="monitor.html?status=X" is wired
   per the design now; the Monitor page (#653) resolves when it ships.
   Uses existing tokens — no new palette. */
.fo-cell-nav {
  position: relative;
  display: inline-block;
  cursor: pointer;
}
.fo-cell-nav:hover .fc-v {
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}
/* Tooltip caption on the fleet status count cells */
.fo-cell-nav[data-tip]:hover::after {
  content: attr(data-tip);
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  padding: 4px 8px;
  border-radius: 5px;
  pointer-events: none;
  z-index: 10;
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}

/* ── B7 CHANGE 2: axis tie-back tag on rec cards ─────────────────────
   .rec-axis-tag: scored recs showing which axis they move + how many pts.
   .rec-advisory-tag: "advisory · not scored" for governance + quality_report recs.
   Both use existing accent/muted tokens — no new palette introduced. */
.rec-axis-tag {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
  white-space: nowrap;
  background: rgba(78, 201, 176, 0.12);
  color: var(--accent);
  border: 1px solid rgba(78, 201, 176, 0.3);
  margin-left: auto;
}
.rec-advisory-tag {
  display: inline-block;
  font-size: 11px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 10px;
  white-space: nowrap;
  background: rgba(138, 150, 163, 0.1);
  color: var(--muted);
  border: 1px solid rgba(138, 150, 163, 0.2);
  margin-left: auto;
}
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


# Candidate (axis, metric, default_direction) rows for the Runtime-Health scorecard,
# in display order: reliability, then quality, then cost. This list is FILTERED through
# panels._health._RUNTIME_AXES in _render_health_band before any row is emitted, so a
# row whose axis is not a runtime axis (governance / model_fit / work_mix) never renders
# (MUST 14). Module-level so the MUST 14 strip-RED conformance test can patch it.
_SCORECARD_DISPLAY_ORDER = [
    ("reliability", "error_rate", "lower"),
    ("reliability", "blocked_rate", "lower"),
    ("reliability", "skipped_rate", "lower"),
    ("quality", "pass_rate", "higher"),
    ("quality", "hard_fail_rate", "lower"),
    # Cost HEALTH axis: spend_vs_trend only (#687, spec/53 §3.6 + MUST 14).
    # cheaper_model_share and tokens_per_output are NOT health metrics —
    # they are optimization signals consumed by the recommendations engine.
    ("cost", "spend_vs_trend", "lower"),
]


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

    # Prefer the canonical display integer fields (fleet_composite_display,
    # worst_agent_composite_display) which are set by compute_fleet_health after
    # the critical-cap override (#623 fix, spec/53 §3.3 + MUST 11). Fall back to int(round())
    # for older FleetHealth objects that may not have the field populated.
    composite_display = getattr(fleet_health, "fleet_composite_display", None)
    if composite_display is None and composite is not None:
        composite_display = int(round(composite))
    worst_composite_display = getattr(
        fleet_health, "worst_agent_composite_display", None
    )
    if worst_composite_display is None and worst_composite is not None:
        worst_composite_display = int(round(worst_composite))

    if composite is not None and composite_display is not None:
        score_html = f'<span class="health-score-composite">{composite_display}</span>'
        coverage_html = (
            f'<span class="health-coverage">Health {composite_display}'
            f" | Coverage {coverage_n}/{coverage_m}"
            + (
                f" | Worst: {html.escape(worst_agent)} ({worst_composite_display})"
                if worst_agent and worst_composite_display is not None
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
        # Use int(round()) for display/band consistency (#623 fix): chip value 79.5
        # rounds to 80 and gets a green chip, not an amber chip labeled '80'.
        chip_di = int(round(mean_v))
        # Band the displayed integer via the scoring core's _band (single source
        # of truth for BAND_GREEN_MIN/BAND_AMBER_MIN) so the chip thresholds can
        # never drift from the headline/composite bands (#623 root-cause class).
        from ..advisor.score import _band

        chip_band = _band(chip_di)
        return (
            f'<div class="health-chip health-chip-{chip_band}">'
            f'<div class="health-chip-label">{html.escape(label)}</div>'
            f'<div class="health-chip-value">{chip_di}</div>'
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
    # MUST 14 (Runtime-Health framing): the Runtime-Health scorecard renders ONLY
    # the three real equal-weight axes (cost/quality/reliability). The candidate row
    # order lives in the module-level _SCORECARD_DISPLAY_ORDER; it is filtered HERE
    # through _RUNTIME_AXES (the panel-owned set) before any row is emitted. This is
    # the load-bearing enforcement: adding a ("governance", ...) row to the candidate
    # list changes nothing in the output unless "governance" is ALSO added to
    # _RUNTIME_AXES. (Conformance: test_runtime_health_excludes_governance_strip_red
    # patches _RUNTIME_AXES to include governance and proves the row then renders.)
    from .panels._health import _RUNTIME_AXES

    display_order = [row for row in _SCORECARD_DISPLAY_ORDER if row[0] in _RUNTIME_AXES]
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


def _render_recommendations(recommendations) -> str:
    """Render the Recommendations panel (spec/54 PR3). Fail-soft: returns '' on None."""
    if recommendations is None:
        return ""
    recs = list(recommendations)
    if not recs:
        return (
            "<h2>Recommendations</h2>"
            '<div class="queue-empty">No recommendations at this time — fleet looks optimized.</div>'
        )

    _kind_label = {
        "savings_cost": "Cost Savings",
        "quality_report": "Quality Report",
        "governance": "Governance",
    }
    # Known rec kinds get their own kind-pill color (CSS .rec-kind-{kind}).
    _known_kinds = {"savings_cost", "quality_report", "governance"}

    rows_html = ""
    for rec in recs:
        agent_safe = html.escape(getattr(rec, "agent", ""))
        kind = getattr(rec, "kind", "")
        kind_label = html.escape(_kind_label.get(kind, kind))
        # Use the kind-specific pill class for known kinds; fall back to the
        # generic info pill for any unknown kind (defensive — kind is validated
        # at construction, so this only guards against a future kind not in CSS).
        if kind in _known_kinds:
            pill_html = (
                f'<span class="rec-kind-pill rec-kind-{kind}">{kind_label}</span>'
            )
        else:
            pill_html = f'<span class="pill info">{kind_label}</span>'
        rationale_safe = html.escape(getattr(rec, "rationale", "") or "")
        current_model = getattr(rec, "current_model", None)
        candidate_model = getattr(rec, "candidate_model", None)
        usd_delta = getattr(rec, "projected_usd_delta", None)
        pts_delta = getattr(rec, "projected_points_delta", None)

        model_str = ""
        if current_model and candidate_model:
            model_str = (
                f'<div class="rec-models">'
                f"{html.escape(current_model)}"
                f'<span class="rec-arrow">→</span>'
                f"{html.escape(candidate_model)}"
                f"</div>"
            )

        delta_parts = []
        if usd_delta is not None:
            # Savings recs only ever carry a negative usd_delta (the rec fires
            # only when projected_usd_delta < 0). Render the magnitude as a
            # positive "saved" figure rather than a raw "$-42.50/mo" (#616 review).
            delta_parts.append(
                f'<span class="rec-delta-badge rec-delta-savings">'
                f"${abs(usd_delta):.2f}/mo saved</span>"
            )
        if pts_delta is not None and kind == "savings_cost":
            delta_parts.append(
                f'<span class="rec-delta-badge rec-delta-points">{pts_delta:+.1f} pts</span>'
            )
        delta_html = "".join(delta_parts)

        # Display-only "apply →" affordance (ACT zone, B6 mockup). Static span — no
        # POST, no write wiring; the management/write layer ships later. Rendered on
        # savings_cost rows (the actionable model-right-size recs) per the mockup's
        # priority-action list.
        apply_cta = (
            '<span class="apply-cta" title="Management action — coming with the write layer">'
            "apply &rarr;</span>"
            if kind == "savings_cost"
            else ""
        )

        # B7 CHANGE 2: axis tie-back tag (spec/52 §17 layered rec tie-back).
        # savings_cost recs move the Cost axis — show "→ Cost · +N pts" (teal).
        # quality_report + governance recs do NOT move the 3-axis composite — show
        # "advisory · not scored" (muted). Unknown kinds get no tag.
        tie_back_tag = ""
        if kind == "savings_cost":
            if pts_delta is not None:
                pts_rounded = int(round(pts_delta))
                pts_str = f" &middot; +{pts_rounded} pts" if pts_rounded != 0 else ""
            else:
                pts_str = ""
            tie_back_tag = f'<span class="rec-axis-tag">&#8594; Cost{pts_str}</span>'
        elif kind in ("quality_report", "governance"):
            tie_back_tag = (
                '<span class="rec-advisory-tag">advisory &middot; not scored</span>'
            )

        rows_html += (
            f'<div class="rec-row">'
            f'<div class="rec-header">'
            f"{pill_html}"
            f'<span class="rec-agent">{agent_safe}</span>'
            f"{apply_cta}"
            f"{tie_back_tag}"
            f"</div>"
            f"{model_str}"
            f'<div class="rec-rationale">{rationale_safe}</div>'
            + (f'<div class="rec-deltas">{delta_html}</div>' if delta_html else "")
            + "</div>"
        )

    return f'<h2>Recommendations</h2><div class="rec-panel">{rows_html}</div>'


def _render_console_template(console_data, has_goals: bool = True, ctx=None) -> str:
    """Fleet Console home page HTML — B6 cockpit layout (spec/52 §16, Cockpit PR-A).

    Composes the page via the registry's layout engine (registry.compose()), which
    iterates registered panels by slot (STATUS, ACT, EXPLORE) with per-panel fail-soft
    (MUST 11) and unions every PanelResult.alert_keys (MUST 17). The engine union is the
    SOLE source of the sidecar keys — this function stashes it on
    console_data._engine_alert_keys for render_console() to write; it does NOT seed it
    from console_data.rendered_alert_keys.

    MUST 10: no surface hard-codes a panel inline outside the registry. The only
    non-panel content here is page chrome (header, nav, zone-label dividers, footer,
    snooze modal, JS) — chrome is emitted HERE, keyed on slot, so a panel fail-soft
    degrades only the panel's content, never the zone divider (MUST 11 spirit).
    MUST 15: the agent-grid is NOT rendered on the home page (moved to Fleet Monitor #653).
    """
    from datetime import datetime, timezone

    # Import the panels package to ensure all panels are registered (spec/52 §16.5).
    # This is idempotent — Python's module cache prevents double-registration.
    from .panels import get_registry

    _nav = _nav_bar("console", has_goals=has_goals)
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fleet_size = console_data.agent_count

    _degraded_banner = (
        '<div class="degraded-banner">'
        '<span class="pill warn">⚠ data may be incomplete</span>'
        " &nbsp;One or more backend reads failed. Metrics below may be partial."
        "</div>"
        if console_data.degraded
        else ""
    )

    # ── Build PanelContext if not supplied (supports direct test calls) ───────
    if ctx is None:
        from datetime import date as _date

        from .panels import ConsoleCapabilities, PanelContext

        _now = datetime.now(tz=timezone.utc)
        ctx = PanelContext(
            console_data=console_data,
            capabilities=ConsoleCapabilities(has_goals=has_goals),
            today=_date.today(),
            now=_now,
        )

    # ── Layout engine — compose registered panels by slot (MUST 10) ──────────
    # registry.compose() is the SINGLE engine entry point; it owns the
    # is_available gate (MUST 12), per-panel render fail-soft (MUST 11), and the
    # alert-key union (MUST 17). Conformance tests call the same method, so the
    # behaviors are exercised by production code, not re-implemented in tests.
    registry = get_registry()
    slot_html, engine_keys = registry.compose(ctx)

    # ── MUST 17: stash the engine-aggregated union for render_console() ───────
    # engine_keys is the SOLE source for the sidecar — render_console() writes it
    # (the layout engine has no agents_root). We do NOT OR in
    # console_data.rendered_alert_keys: the attention panel already contributes the
    # keys for every queue item it renders, so the union is complete by
    # construction. Seeding from the pre-computed field would make this aggregation
    # non-load-bearing and defeat MUST 17's strip-RED.
    console_data._engine_alert_keys = engine_keys  # type: ignore[attr-defined]

    # ── Snooze Modal (page chrome — not a panel) ──────────────────────────────
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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_SHARED_CSP}">
<title>Atomic Agents — Fleet Console</title>
<style>{CSS}{_CONSOLE_CSS_EXTRA}{_COCKPIT_CSS}</style>
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

<div class="zone-label cockpit-zone-label">Status</div>
{slot_html["status"]}

<div class="zone-label cockpit-zone-label">Act</div>
{slot_html["act"]}

<div class="zone-label cockpit-zone-label">Explore</div>
{slot_html["explore"]}

<footer>
  <div>Generated {date.today().isoformat()} by atomic_agents.dashboard</div>
  <div>Fleet Console · spec/52 + spec/53 + spec/54</div>
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

// B7 CHANGE 1: smooth-scroll + brief highlight pulse for in-page anchor links
// (e.g. the "Needs Attention" KPI tile → #attention-queue).
document.querySelectorAll('a[href^="#"]').forEach(function(anchor) {{
  anchor.addEventListener('click', function(e) {{
    var target = document.querySelector(this.getAttribute('href'));
    if (target) {{
      e.preventDefault();
      target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      target.style.transition = 'background 0.3s ease';
      target.style.background = 'rgba(78, 201, 176, 0.06)';
      setTimeout(function() {{ target.style.background = ''; }}, 1200);
    }}
  }});
}});
</script>

</body>
</html>
"""

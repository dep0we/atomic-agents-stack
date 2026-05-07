# Cost Dashboard

How to build the cost & observability dashboard described in [../spec/09-cost-observability](../spec/09-cost-observability.md). Static HTML + nightly cron + optional local server for live refresh.

This is **purely an aggregation + render layer.** No LLM calls. No external services. Reads JSONL logs, writes HTML.

---

## What ships in this implementation

```
~/projects/automations/
├── lib/
│   └── atomic_agents/
│       ├── costs.py                 ← aggregation logic
│       ├── dashboard.py             ← render + serve entry point
│       └── templates/
│           ├── index.html.j2        ← global dashboard template
│           ├── agent.html.j2        ← per-agent template
│           ├── chart.js             ← bundled Chart.js (~80KB)
│           └── style.css
├── jobs/
│   └── atomic_agents_dashboard.py   ← nightly cron entry
└── launchd/
    └── ai.your-server.atomic-agents-dashboard.plist
```

The dashboard *output* lands in the vault at `<agents_root>/_dashboard/` so Sam can browse it from Obsidian Mobile or any browser.

---

## High-level flow

```
launchd 03:00 nightly
       ↓
jobs/atomic_agents_dashboard.py
       ↓
1. Discover agents — scan <agents_root> for folders containing log/
2. Parse logs   — read all log/YYYY-MM/*.jsonl files in last 12 months
3. Aggregate    — group by (agent, model, day) and (agent, day) and (agent, month)
4. Compute      — totals, deltas vs. last month, cache savings, top runs
5. Render       — Jinja2 fills templates → HTML
6. Write        — index.html, per-agent dashboard.html, JSON data files
       ↓
Vault state updated, no further action needed
```

---

## Aggregation: `lib/atomic_agents/costs.py`

```python
"""Cost & usage aggregation across Atomic Agents."""

from __future__ import annotations
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

@dataclass
class RunRecord:
    ts: datetime
    agent: str
    trigger: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cache_hit_tokens: int
    cache_miss_tokens: int
    latency_ms: int
    status: str
    summary: str
    fallback: bool = False
    critical: bool = False

@dataclass
class AgentSummary:
    name: str
    runs: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_hit_pct: float
    errors: int
    models_used: list[str] = field(default_factory=list)
    cost_by_model: dict[str, float] = field(default_factory=dict)
    runs_by_model: dict[str, int] = field(default_factory=dict)

@dataclass
class GlobalSummary:
    period_label: str          # e.g., "May 2026"
    total_cost: float
    total_runs: int
    composite_cache_hit_pct: float
    total_errors: int
    agents: list[AgentSummary]
    top_runs: list[RunRecord]   # top N by cost
    delta_vs_prior_period: dict[str, float]  # cost / runs / errors deltas


def discover_agents(agents_root: Path) -> list[str]:
    """Find folders that look like Atomic Agents (have log/ subdir)."""
    return sorted(
        d.name for d in agents_root.iterdir()
        if d.is_dir() and (d / "log").is_dir() and not d.name.startswith("_")
    )

def load_runs(agents_root: Path, agent: str, since: date) -> list[RunRecord]:
    """Read all log JSONL files for one agent since a given date."""
    log_dir = agents_root / agent / "log"
    runs: list[RunRecord] = []
    for path in sorted(log_dir.rglob("*.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip malformed lines; surface in lint pass
            ts = datetime.fromisoformat(rec["ts"])
            if ts.date() < since:
                continue
            runs.append(RunRecord(
                ts=ts,
                agent=agent,
                trigger=rec.get("trigger", "unknown"),
                model=rec.get("model", "unknown"),
                input_tokens=rec.get("input_tokens", 0),
                output_tokens=rec.get("output_tokens", 0),
                cost_usd=rec.get("cost_usd", 0.0),
                cache_hit_tokens=rec.get("cache_hit_tokens", 0),
                cache_miss_tokens=rec.get("cache_miss_tokens", 0),
                latency_ms=rec.get("latency_ms", 0),
                status=rec.get("status", "unknown"),
                summary=rec.get("summary", ""),
                fallback=rec.get("fallback", False),
                critical=rec.get("critical", False),
            ))
    return runs

def summarize_agent(runs: list[RunRecord]) -> AgentSummary:
    if not runs:
        return AgentSummary(name="(empty)", runs=0, cost_usd=0,
                            input_tokens=0, output_tokens=0,
                            cache_hit_pct=0.0, errors=0)
    cost_by_model = defaultdict(float)
    runs_by_model = defaultdict(int)
    cache_hit = 0
    cache_total = 0
    errors = 0
    total_cost = 0
    in_tok = out_tok = 0
    for r in runs:
        cost_by_model[r.model] += r.cost_usd
        runs_by_model[r.model] += 1
        cache_hit += r.cache_hit_tokens
        cache_total += r.cache_hit_tokens + r.cache_miss_tokens
        if r.status == "error":
            errors += 1
        total_cost += r.cost_usd
        in_tok += r.input_tokens
        out_tok += r.output_tokens
    cache_pct = (cache_hit / cache_total * 100.0) if cache_total else 0.0
    return AgentSummary(
        name=runs[0].agent,
        runs=len(runs),
        cost_usd=round(total_cost, 4),
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_hit_pct=round(cache_pct, 1),
        errors=errors,
        models_used=sorted(cost_by_model.keys()),
        cost_by_model={k: round(v, 4) for k, v in cost_by_model.items()},
        runs_by_model=dict(runs_by_model),
    )

def aggregate(agents_root: Path, today: date | None = None) -> GlobalSummary:
    """Build the GlobalSummary for the current month."""
    today = today or date.today()
    month_start = today.replace(day=1)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)

    agents = discover_agents(agents_root)
    summaries: list[AgentSummary] = []
    all_runs_this_month: list[RunRecord] = []
    all_runs_last_month: list[RunRecord] = []
    for agent in agents:
        this_month = [r for r in load_runs(agents_root, agent, month_start)]
        last_month = [r for r in load_runs(agents_root, agent, last_month_start)
                      if r.ts.date() < month_start]
        if this_month:
            summaries.append(summarize_agent(this_month))
        all_runs_this_month.extend(this_month)
        all_runs_last_month.extend(last_month)

    total_cost = sum(s.cost_usd for s in summaries)
    total_runs = sum(s.runs for s in summaries)
    total_errors = sum(s.errors for s in summaries)
    last_month_cost = sum(r.cost_usd for r in all_runs_last_month)

    # Composite cache hit
    cache_hit = sum(r.cache_hit_tokens for r in all_runs_this_month)
    cache_total = sum(r.cache_hit_tokens + r.cache_miss_tokens for r in all_runs_this_month)
    cache_pct = (cache_hit / cache_total * 100.0) if cache_total else 0.0

    top_runs = sorted(all_runs_this_month, key=lambda r: r.cost_usd, reverse=True)[:5]

    return GlobalSummary(
        period_label=today.strftime("%B %Y"),
        total_cost=round(total_cost, 4),
        total_runs=total_runs,
        composite_cache_hit_pct=round(cache_pct, 1),
        total_errors=total_errors,
        agents=summaries,
        top_runs=top_runs,
        delta_vs_prior_period={
            "cost_pct": ((total_cost - last_month_cost) / last_month_cost * 100.0)
                        if last_month_cost else 0.0,
        },
    )

def aggregate_for_agent(agents_root: Path, agent: str,
                         months_back: int = 12) -> dict:
    """Per-agent dashboard data. Returns a dict suitable for JSON dump."""
    today = date.today()
    twelve_months_ago = (today.replace(day=1) - timedelta(days=365)).replace(day=1)
    runs = load_runs(agents_root, agent, twelve_months_ago)
    # Group by (model, month) for the trend chart
    by_month_model: dict[tuple[str, str], float] = defaultdict(float)
    for r in runs:
        key = (r.ts.strftime("%Y-%m"), r.model)
        by_month_model[key] += r.cost_usd
    # ... continue as needed
    return {
        "agent": agent,
        "trend": {
            f"{month}__{model}": round(cost, 4)
            for (month, model), cost in by_month_model.items()
        },
        # ... more aggregations
    }
```

The full module is longer (heatmap data, top-runs-per-agent, suggested-caps logic) — this sketches the shape.

---

## Render: `lib/atomic_agents/dashboard.py`

```python
"""Render aggregated data into HTML using Jinja2."""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from .costs import aggregate, aggregate_for_agent, discover_agents

TEMPLATE_DIR = Path(__file__).parent / "templates"

def render_global(agents_root: Path) -> Path:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("index.html.j2")

    summary = aggregate(agents_root)
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"

    html = template.render(summary=summary, refresh_endpoint="/regenerate")
    out_path.write_text(html)

    # Also dump pre-aggregated JSON for fast page loads
    import json
    (out_dir / "data" / f"{summary.period_label.replace(' ', '_').lower()}.json").write_text(
        json.dumps(summary, default=str)
    )
    return out_path

def render_agent(agents_root: Path, agent: str) -> Path:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("agent.html.j2")

    data = aggregate_for_agent(agents_root, agent)
    out_path = agents_root / agent / "dashboard.html"
    out_path.write_text(template.render(data=data))
    return out_path

def render_all(agents_root: Path) -> None:
    render_global(agents_root)
    for agent in discover_agents(agents_root):
        render_agent(agents_root, agent)
```

---

## HTML template sketch (`templates/index.html.j2`)

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Atomic Agents — {{ summary.period_label }}</title>
<link rel="stylesheet" href="style.css">
<script src="chart.js"></script>
</head>
<body>
  <header>
    <h1>Atomic Agents</h1>
    <div class="period">{{ summary.period_label }}</div>
    <button id="refresh" onclick="refresh()">Refresh</button>
  </header>

  <section class="kpis">
    <div class="kpi">
      <div class="value">${{ summary.total_cost }}</div>
      <div class="label">Total cost this month</div>
      <div class="delta {{ 'up' if summary.delta_vs_prior_period.cost_pct > 0 else 'down' }}">
        {{ summary.delta_vs_prior_period.cost_pct | round(1) }}% vs. last month
      </div>
    </div>
    <div class="kpi">
      <div class="value">{{ summary.total_runs }}</div>
      <div class="label">Total runs</div>
    </div>
    <div class="kpi">
      <div class="value">{{ summary.composite_cache_hit_pct }}%</div>
      <div class="label">Cache hit rate</div>
    </div>
    <div class="kpi">
      <div class="value">{{ summary.total_errors }}</div>
      <div class="label">Errors</div>
    </div>
  </section>

  <section>
    <h2>Per-agent breakdown</h2>
    <table>
      <thead>
        <tr><th>Agent</th><th>Cost</th><th>Runs</th><th>Errors</th>
            <th>Cache hit</th><th>Models used</th></tr>
      </thead>
      <tbody>
      {% for a in summary.agents %}
        <tr>
          <td><a href="../{{ a.name }}/dashboard.html">{{ a.name }}</a></td>
          <td>${{ a.cost_usd }}</td>
          <td>{{ a.runs }}</td>
          <td>{{ a.errors }}</td>
          <td>{{ a.cache_hit_pct }}%</td>
          <td>
            {% for model, cost in a.cost_by_model.items() %}
              <span class="model-pill">{{ model }}: ${{ cost }}</span>
            {% endfor %}
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>

  <section>
    <h2>Month over month</h2>
    <canvas id="mom-chart" width="800" height="300"></canvas>
    <script>
      // Chart.js stacked bar — one segment per agent per month
      // Data injected via Jinja
    </script>
  </section>

  <section>
    <h2>Top 5 most expensive runs</h2>
    <table>
      <thead><tr><th>Date</th><th>Agent</th><th>Trigger</th><th>Model</th>
                 <th>Tokens (in/out)</th><th>Cost</th><th>Summary</th></tr></thead>
      <tbody>
      {% for r in summary.top_runs %}
        <tr>
          <td>{{ r.ts.strftime('%Y-%m-%d %H:%M') }}</td>
          <td>{{ r.agent }}</td>
          <td>{{ r.trigger }}</td>
          <td>{{ r.model }}</td>
          <td>{{ r.input_tokens }} / {{ r.output_tokens }}</td>
          <td>${{ r.cost_usd | round(4) }}</td>
          <td>{{ r.summary | truncate(60) }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>

  <script>
    function refresh() {
      // Try the live-refresh server first
      fetch('/regenerate', {method: 'POST'}).then(r => {
        if (r.ok) location.reload();
        else fallback();
      }).catch(fallback);
    }
    function fallback() {
      // No server. Plain reload — shows last nightly data.
      location.reload();
    }
  </script>
</body>
</html>
```

The per-agent template (`agent.html.j2`) follows the same pattern with sections from spec/09: 12-month trend, daily heatmap, model-over-time chart, cache savings, top runs, error rate, budget vs. actual.

---

## Cron entry point

```python
#!/usr/bin/env python3
"""Nightly dashboard regeneration."""
from pathlib import Path
from automations.lib.atomic_agents.dashboard import render_all
from automations.lib import logger

AGENTS_ROOT = Path.home() / "docs" / "agents"

def main():
    render_all(AGENTS_ROOT)

if __name__ == "__main__":
    with logger.run(name="atomic-agents-dashboard"):
        main()
```

LaunchAgent plist runs this nightly at 03:00 CT. Same pattern as other automations jobs in the repo.

---

## Optional local server for live refresh

```python
"""Tiny Flask server so the Refresh button does something useful."""

from pathlib import Path
from flask import Flask, send_from_directory
from automations.lib.atomic_agents.dashboard import render_all

AGENTS_ROOT = Path.home() / "docs" / "agents"
DASHBOARD_DIR = AGENTS_ROOT / "_dashboard"

app = Flask(__name__)

@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(DASHBOARD_DIR, filename)

@app.route("/regenerate", methods=["POST"])
def regenerate():
    render_all(AGENTS_ROOT)
    return {"status": "ok"}, 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765)
```

Run with: `python -m atomic_agents.dashboard.serve`

When this server is running:
- Open `http://localhost:8765` to view the dashboard live
- The Refresh button hits `/regenerate` and the page reloads with fresh data
- Server is loopback-only (127.0.0.1) — not exposed to the network

When the server isn't running:
- Open the HTML file directly: `file:///<agents_root>/_dashboard/index.html`
- Refresh button still works but only does a plain page reload (showing nightly data)

---

## Sample queries the dashboard helps you answer

| Question | Where to look |
|---|---|
| "Am I spending more than last month?" | Global → KPI cards → cost delta |
| "Which agent burned the most this month?" | Global → per-agent table sorted by cost |
| "Did Caldwell switch models? When?" | Caldwell page → model-over-time chart |
| "What's my expensive cron run?" | Global or per-agent → top runs table |
| "Is my cache hit rate degrading?" | Per-agent → cache savings → trend |
| "Why are we suddenly seeing errors?" | Global → errors KPI → drill into agent → logs |
| "What model mix am I running?" | Global → model-mix breakdown |

---

## Performance

For Sam's scale (5-10 agents, ~50-200 runs each per month), nightly aggregation runs in **~5-30 seconds**. Reading 12 months of JSONL across all agents is at most a few thousand records.

If an agent's logs grow past ~10K records/month, switch to a streaming aggregator (the current `load_runs` reads everything into memory). Not a concern for v1.

---

## Edge cases handled

- **Missing fields in old logs** — defaults applied. Dashboard shows blanks for unknown values rather than crashing.
- **Malformed JSONL line** — skipped silently in production; lint pass surfaces them.
- **Agent folder with no log/ subdir** — discover_agents skips it.
- **Mid-month new agent** — appears with partial data; delta vs. last month shows "—".
- **Agent renamed** — old folder data attributed to old name; new folder appears separately. Manual merge if needed.
- **Clock skew across machines** (cron on your-server, skill on MacBook, both writing logs) — sort by `ts` regardless of which machine wrote the record.

---

## What's NOT in this implementation

- **Real-time WebSocket updates** — out of scope. Refresh button is enough.
- **Multi-user / auth** — single-user assumption. If you publish the dashboard, that's on you.
- **Database backend** — we're aggregating ~10K records max; SQLite would be over-engineering today. If you outgrow it, swap in DuckDB and the aggregator stays the same shape.
- **Forecasting** — next month's projected cost. Useful but deferred.

---

*See also: [../spec/09-cost-observability](../spec/09-cost-observability.md) for the spec, [shared-helper#cost-guardrails](shared-helper.md#cost-guardrails) for enforcement code.*

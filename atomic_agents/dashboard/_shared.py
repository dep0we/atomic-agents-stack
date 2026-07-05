"""Shared helpers for all dashboard tabs.

CSS preamble, navigation bar, time-formatting utilities.
Import from here to keep all five HTML pages visually consistent.
"""

from __future__ import annotations
from datetime import datetime, timezone


# ──────────────────────────────────────────────────────────────────
# CSS — matches render.py exactly; shared so all five tabs stay in sync

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
  margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
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

.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 8px; }
.kpis-5 { display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-bottom: 8px; }
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
tbody td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
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
.pill.ok { color: var(--good); border-color: var(--good); background: rgba(152, 195, 121, 0.1); }
.pill.error { color: var(--error); border-color: var(--error); background: rgba(224, 108, 117, 0.1); }
.pill.warn { color: var(--warn); border-color: var(--warn); background: rgba(209, 154, 102, 0.1); }
.pill.neutral { color: var(--muted); border-color: var(--border); background: transparent; }
.row-error td { background: rgba(224, 108, 117, 0.05); }
.row-warn td { background: rgba(209, 154, 102, 0.05); }

.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }

.empty-note { color: var(--muted); font-style: italic; font-size: 13px; padding: 12px 0; }

/* Sparkline via Unicode block chars */
.sparkline { font-family: monospace; letter-spacing: 2px; font-size: 14px; color: var(--accent); }

footer {
  margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--muted); font-size: 12px;
  display: flex; justify-content: space-between;
}
"""

# Unicode block chars for sparklines (index 0=smallest … 7=largest)
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Return a Unicode sparkline string for a list of floats."""
    if not values:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn
    chars = []
    for v in values:
        if rng == 0:
            idx = 4
        else:
            idx = int((v - mn) / rng * 7)
        chars.append(SPARK_CHARS[max(0, min(7, idx))])
    return "".join(chars)


_KNOWN_TAB_KEYS = frozenset(
    {"console", "monitor", "cost", "activity", "quality", "memory", "goals"}
)


def nav_bar(current: str, has_goals: bool = True) -> str:
    """Render the top navigation bar for a dashboard page.

    current: one of "console", "cost", "activity", "quality", "memory", "goals"
    has_goals: if False, the Goals tab is omitted from the nav.

    BEHAVIOR CHANGE (spec/52 PR1): 'console' is now the FIRST tab (home position)
    and links to index.html. The Cost tab now links to cost.html (not index.html).
    All existing callers that pass current='cost' etc. continue to work unchanged —
    they gain a Console tab link in the nav without any call-site change needed.
    """
    if current not in _KNOWN_TAB_KEYS:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "nav_bar() received unknown current=%r; expected one of %s",
            current,
            sorted(_KNOWN_TAB_KEYS),
        )

    # Console is the FIRST tab (front door per spec/52). Monitor is second (spec/56 #653):
    # it is a primary surface and must be reachable from every page's top nav.
    # Cost tab now points to cost.html (was index.html — backward-compat callout in spec/52).
    tabs = [
        ("console", "index.html", "Console"),
        ("monitor", "monitor.html", "Monitor"),
        ("cost", "cost.html", "Cost"),
        ("activity", "activity.html", "Activity"),
        ("quality", "quality.html", "Quality"),
        ("memory", "memory.html", "Memory"),
    ]
    if has_goals:
        tabs.append(("goals", "goals.html", "Goals"))

    items = []
    for key, href, label in tabs:
        active = ' class="active"' if key == current else ""
        items.append(f'<a href="{href}"{active}>{label}</a>')
    return f'<nav class="tab-nav">{"".join(items)}</nav>'


def relative_time(ts: datetime, now: datetime | None = None) -> str:
    """Return a human-readable relative time string ('2m ago', '3d ago')."""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    # Make both tz-aware or both tz-naive
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    diff = now - ts
    total_seconds = int(diff.total_seconds())
    if total_seconds < 0:
        return "just now"
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m ago"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h ago"
    days = total_seconds // 86400
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"


def truncate(text: str, n: int) -> str:
    """Truncate text to n characters with ellipsis."""
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def status_pill(status: str) -> str:
    """Return an HTML pill for a run status."""
    import html as _html

    s = status.lower()
    if s == "ok":
        cls = "ok"
    elif s in ("error", "failed"):
        cls = "error"
    elif s in ("skipped", "warn", "warning"):
        cls = "warn"
    else:
        cls = "neutral"
    return f'<span class="pill {cls}">{_html.escape(status)}</span>'


_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "connect-src 'self'"
)


def page_shell(
    title: str,
    body: str,
    current_tab: str,
    has_goals: bool = True,
    subtitle: str | None = None,
) -> str:
    """Wrap body content in the standard page shell (DOCTYPE, head, nav, footer)."""
    from datetime import date
    import html as _html

    sub_html = f'<div class="period">{_html.escape(subtitle)}</div>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_CSP}">
<title>{_html.escape(title)} — Atomic Agents</title>
<style>{CSS}</style>
</head>
<body>

<header>
  <div>
    <h1>{_html.escape(title)}</h1>
    {sub_html}
  </div>
  <div>
    <button class="refresh-btn" onclick="refresh()">&#8635; Refresh</button>
  </div>
</header>

{nav_bar(current_tab, has_goals=has_goals)}

{body}

<footer>
  <div>Generated {date.today().isoformat()} by atomic_agents.dashboard</div>
  <div>Atomic Agents fleet observability</div>
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

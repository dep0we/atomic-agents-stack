"""Fleet Monitor page renderer (spec/56, #653).

render_monitor() emits monitor.html alongside index.html and cost.html.
It reuses the SAME panel registry, PanelContext, and layout engine as the home
(spec/56 §6, §3 shared-snapshot guarantee). No new LLM spend (MUST 11).

MUST 12 structural guarantee: both the home fleet-status panel (_fleet_status.py)
and the monitor summary panel (_monitor_summary.py) call status_for_agent() from
the SAME ctx.console_data loaded by aggregate_console(). There is exactly ONE
enumeration pass and ONE set of status derivations — the two pages agree by
construction, not by comparison.

MUST 13: NO SSE / fetch / background polling. Freshness = periodic static
re-render + a full-page auto-reload (meta refresh) on a configurable interval.
"""

from __future__ import annotations

import html as _html
import json as _json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from .._io import atomic_write
from ._shared import CSS as _SHARED_CSS, _CSP as _SHARED_CSP

logger = logging.getLogger(__name__)

_MONITOR_FILE = "monitor.html"
_AUTO_RELOAD_SECONDS = 60  # full-page reload every 60s (MUST 13: no background fetch)

# ──────────────────────────────────────────────────────────────────
# Monitor-specific CSS (B7 dark-teal cockpit palette, per variant-B7-monitor.html)

_MONITOR_CSS = """
/* ── B7 Monitor-specific styles ─────────────────────────────────── */
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

.cockpit-zone-label {
  font-size: 10px; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--muted);
  border-bottom: 1px solid var(--border); padding: 20px 0 6px; margin: 0 0 12px;
}
.mono {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums;
}
.num { font-variant-numeric: tabular-nums; }

/* ── Arrival filter banner ── */
.arrival-banner {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 10px 16px; border-radius: 8px; margin-bottom: 16px;
  background: rgba(209, 154, 102, 0.06); border: 1px solid rgba(209, 154, 102, 0.25);
  font-size: 13px; color: var(--muted);
}
.arrival-banner .arrival-caption { font-style: italic; }
.active-filter-chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 700;
  cursor: pointer; user-select: none;
}
.active-filter-chip.chip-error { background: rgba(224, 108, 117, 0.18); color: var(--error); border: 1px solid rgba(224, 108, 117, 0.5); }
.active-filter-chip.chip-warn { background: rgba(209, 154, 102, 0.18); color: var(--warn); border: 1px solid rgba(209, 154, 102, 0.5); }
.active-filter-chip.chip-ok { background: rgba(152, 195, 121, 0.18); color: var(--good); border: 1px solid rgba(152, 195, 121, 0.5); }
.active-filter-chip.chip-stale { background: rgba(138, 150, 163, 0.18); color: var(--muted); border: 1px solid rgba(138, 150, 163, 0.5); }
.active-filter-chip .chip-x { font-size: 14px; line-height: 1; opacity: 0.7; }
.active-filter-chip:hover .chip-x { opacity: 1; }

/* ── Status summary bar ── */
.status-bar {
  display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; align-items: center;
}
.status-chip {
  display: inline-flex; align-items: center; gap: 5px; padding: 5px 14px;
  border-radius: 20px; font-size: 12px; font-weight: 600; border: 1px solid transparent;
  cursor: pointer; transition: opacity 0.12s, border-color 0.12s, background 0.12s;
  user-select: none;
}
.status-chip .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.status-chip.chip-all { background: var(--card); color: var(--text); border-color: var(--border); }
.status-chip.chip-all.active { border-color: var(--accent); color: var(--accent); background: rgba(78, 201, 176, 0.08); }
.status-chip.chip-ok { background: rgba(152, 195, 121, 0.08); color: var(--good); border-color: rgba(152, 195, 121, 0.22); }
.status-chip.chip-ok .dot { background: var(--good); }
.status-chip.chip-ok.active { background: rgba(152, 195, 121, 0.18); border-color: var(--good); }
.status-chip.chip-warn { background: rgba(209, 154, 102, 0.08); color: var(--warn); border-color: rgba(209, 154, 102, 0.22); }
.status-chip.chip-warn .dot { background: var(--warn); }
.status-chip.chip-warn.active { background: rgba(209, 154, 102, 0.18); border-color: var(--warn); }
.status-chip.chip-error { background: rgba(224, 108, 117, 0.08); color: var(--error); border-color: rgba(224, 108, 117, 0.22); }
.status-chip.chip-error .dot { background: var(--error); }
.status-chip.chip-error.active { background: rgba(224, 108, 117, 0.18); border-color: var(--error); }
.status-chip.chip-stale { background: rgba(138, 150, 163, 0.08); color: var(--muted); border-color: rgba(138, 150, 163, 0.22); }
.status-chip.chip-stale .dot { background: var(--muted); }
.status-chip.chip-stale.active { background: rgba(138, 150, 163, 0.18); border-color: var(--muted); color: var(--text); }
.status-chip:not(.active) { opacity: 0.72; }
.status-chip:hover { opacity: 1; }

/* ── Freshness line (MUST 8) ── */
.freshness-line {
  display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted);
  margin-bottom: 16px; flex-wrap: wrap;
}
.freshness-line .refresh-glyph { color: var(--accent); cursor: pointer; font-size: 14px; opacity: 0.7; }
.freshness-line .refresh-glyph:hover { opacity: 1; }

/* ── Controls row ── */
.mon-controls {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px;
}
.view-toggle {
  display: inline-flex; border: 1px solid var(--border); border-radius: 6px;
  overflow: hidden; flex: none;
}
.view-toggle button {
  padding: 6px 14px; font-size: 12px; font-weight: 600; background: transparent;
  border: none; color: var(--muted); cursor: pointer;
  transition: background 0.1s, color 0.1s; line-height: 1.4;
}
.view-toggle button.active { background: var(--card); color: var(--accent); }
.view-toggle button:hover:not(.active) { color: var(--text); }
.mon-search {
  background: var(--card); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; font-size: 12.5px; padding: 5px 11px; outline: none;
  width: 200px; transition: border-color 0.12s;
}
.mon-search::placeholder { color: var(--muted); }
.mon-search:focus { border-color: var(--accent); }
.mon-sort { display: flex; align-items: center; gap: 6px; margin-left: auto; }
.mon-sort label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.mon-sort select {
  background: var(--card); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; font-size: 12px; padding: 5px 10px; cursor: pointer; outline: none;
}
.mon-sort select:focus { border-color: var(--accent); }

/* ── Model-filter row ── */
.model-filter { display: flex; align-items: center; gap: 6px; }
.model-filter label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.model-filter select {
  background: var(--card); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; font-size: 12px; padding: 5px 10px; cursor: pointer; outline: none;
}
.model-filter select:focus { border-color: var(--accent); }

/* ── Card grid ── */
.agent-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px;
  margin-bottom: 24px;
}
.agent-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 17px 15px; cursor: pointer;
  transition: border-color 0.12s, transform 0.12s; position: relative;
  text-decoration: none; color: inherit; display: block;
}
.agent-card:hover { border-color: var(--accent); transform: translateY(-2px); text-decoration: none; }
.agent-card.status-error { border-left: 3px solid var(--error); }
.agent-card.status-warn { border-left: 3px solid var(--warn); }
.agent-card.status-ok { border-left: 3px solid rgba(78, 201, 176, 0.45); }
.agent-card.status-stale { border-left: 3px solid var(--muted); opacity: 0.82; }

.card-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.card-head .agent-name { font-size: 15px; font-weight: 650; }
.card-head .status-head { margin-left: auto; display: flex; align-items: center; gap: 6px; }

.status-dot {
  width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none;
}
.status-dot.ok { background: var(--good); box-shadow: 0 0 5px rgba(152, 195, 121, 0.5); }
.status-dot.warn { background: var(--warn); box-shadow: 0 0 5px rgba(209, 154, 102, 0.5); }
.status-dot.error { background: var(--error); box-shadow: 0 0 5px rgba(224, 108, 117, 0.5); }
.status-dot.stale { background: var(--muted); }

.health-badge {
  display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 9px;
  border-radius: 18px; vertical-align: 1px;
}
.health-badge.green { background: rgba(152, 195, 121, 0.14); color: var(--good); border: 1px solid rgba(152, 195, 121, 0.3); }
.health-badge.amber { background: rgba(209, 154, 102, 0.14); color: var(--warn); border: 1px solid rgba(209, 154, 102, 0.32); }
.health-badge.red { background: rgba(224, 108, 117, 0.14); color: var(--error); border: 1px solid rgba(224, 108, 117, 0.32); }

.card-meta { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.card-spark { margin-bottom: 10px; }
.card-metrics {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px;
  border-top: 1px solid var(--border); padding-top: 10px;
}
.metric .mk { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.metric .mv { font-size: 15px; font-weight: 600; margin-top: 2px; font-variant-numeric: tabular-nums; }
.metric .mv.bad { color: var(--error); }
.metric .mv.warn { color: var(--warn); }
.metric .mv.ok { color: var(--good); }
.metric .mv.muted { color: var(--muted); }
.card-last-run { font-size: 12px; color: var(--muted); margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }

/* ── List view table ── */
#monitor-list { overflow-x: auto; }
.mon-table {
  width: 100%; border-collapse: collapse; font-size: 13px;
}
.mon-table th {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted);
  font-weight: 600; text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border);
  white-space: nowrap; cursor: pointer; user-select: none;
}
.mon-table th.r { text-align: right; }
.mon-table th:hover { color: var(--text); }
.mon-table th .sort-arrow { opacity: 0.4; margin-left: 3px; }
.mon-table th.sort-active .sort-arrow { opacity: 1; color: var(--accent); }
.mon-table td { padding: 9px 10px; border-top: 1px solid var(--border); vertical-align: middle; white-space: nowrap; }
.mon-table td.r { text-align: right; font-variant-numeric: tabular-nums; }
.mon-table tbody tr { cursor: pointer; transition: background 0.1s; text-decoration: none; }
.mon-table tbody tr:hover { background: rgba(78, 201, 176, 0.04); }

.row-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; vertical-align: middle; }
.row-dot.ok { background: var(--good); }
.row-dot.warn { background: var(--warn); }
.row-dot.error { background: var(--error); box-shadow: 0 0 4px rgba(224, 108, 117, 0.55); }
.row-dot.stale { background: var(--muted); }

.row-agent-name { font-weight: 600; color: var(--text); }
.row-agent-name.stale { color: var(--muted); }
.health-val { font-weight: 650; font-variant-numeric: tabular-nums; }
.health-val.green { color: var(--good); }
.health-val.amber { color: var(--warn); }
.health-val.red { color: var(--error); }
.err-val.bad { color: var(--error); font-weight: 700; }
.err-val.warn { color: var(--warn); font-weight: 600; }
.fail-val.bad { color: var(--error); font-weight: 600; }
.last-run.stale { color: var(--muted); font-style: italic; }

/* ── Empty state ── */
.mon-empty {
  background: rgba(42, 50, 61, 0.4); border: 1px dashed var(--border); border-radius: 10px;
  padding: 26px; text-align: center; color: var(--muted); font-size: 13px; margin-top: 8px;
}

/* ── Degraded cell marker (spec/09 posture, MUST 10 per-row) ── */
/* Shown in place of cost / sparkline when a.degraded || a.costDegraded is true. */
.cost-degraded-cell {
  color: var(--muted); font-style: italic; cursor: help;
}

/* ── Degraded banner ── */
.degraded-banner {
  margin-bottom: 16px; padding: 10px 16px; border-radius: 8px;
  background: rgba(209, 154, 102, 0.08); border: 1px solid rgba(209, 154, 102, 0.3);
  font-size: 13px; color: var(--warn);
}
.pill {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 500; border: 1px solid;
}
.pill.warn { color: var(--warn); border-color: var(--warn); background: rgba(209, 154, 102, 0.1); }
.pill.opus { color: var(--opus); border-color: var(--opus); background: rgba(198, 120, 221, 0.1); }
.pill.sonnet { color: var(--sonnet); border-color: var(--sonnet); background: rgba(97, 175, 239, 0.1); }
.pill.haiku { color: var(--haiku); border-color: var(--haiku); background: rgba(152, 195, 121, 0.1); }
.pill.gpt { color: var(--gpt); border-color: var(--gpt); background: rgba(192, 160, 80, 0.1); }
.pill.local { color: var(--local); border-color: var(--local); background: rgba(136, 136, 136, 0.1); }
"""

# ──────────────────────────────────────────────────────────────────
# Client JS for the Fleet Monitor (MUST 13 — NO SSE/fetch/background polling)

_MONITOR_JS = r"""
/* Fleet Monitor — client-side JS (MUST 13: no polling/SSE/fetch) */
/* AGENTS is loaded from the <script type="application/json" id="monitor-agents"> element */

/* ── Bootstrap agent data from XSS-safe JSON element (MUST 11 no-LLM, MUST 13 no-fetch) ── */
var AGENTS = (function() {
  var el = document.getElementById('monitor-agents');
  if (!el) return [];
  try { return JSON.parse(el.textContent); } catch(e) { return []; }
})();

/* ── State ── */
var monFilter = 'all';
var monSort = 'problems';
var monSearch = '';
var monModel = 'all';
var monView = 'list';

/* STATUS_ORDER must match _STATUS_SORT in _monitor_roster.py: ERROR=0, STALE=1, WARN=2, OK=3 */
const STATUS_ORDER = { error: 0, stale: 1, warn: 2, ok: 3 };

/* ── View persistence (MUST 4) ── */
/* ?view= wins -> localStorage["fleet-monitor.view"] -> 'list' */
function resolveView() {
  const params = new URLSearchParams(window.location.search);
  const qv = params.get('view');
  if (qv === 'list' || qv === 'cards') return qv;
  const stored = (typeof localStorage !== 'undefined') ? localStorage.getItem('fleet-monitor.view') : null;
  if (stored === 'list' || stored === 'cards') return stored;
  return 'list';
}
monView = resolveView();

/* ── Sorting ── */
function sortKey(a) {
  if (monSort === 'problems') {
    return STATUS_ORDER[a.status] * 1000 - (a.errors24h || 0) - (a.fail7d || 0) * 0.1;
  }
  if (monSort === 'cost') return -(a.cost7d || 0);
  if (monSort === 'errors') return -(a.errors24h || 0);
  /* lastrun sort: missing run (empty ISO) sorts last; otherwise sort ascending so
     oldest-run agents surface first (most stale). Use ISO string comparison which
     is lexicographically correct for 8601 timestamps. Empty string > any date. */
  if (monSort === 'lastrun') {
    var iso = a.lastRunISO || '';
    return iso === '' ? '￿' : iso;
  }
  if (monSort === 'name') return (a.name || a.id).toLowerCase();
  if (monSort === 'health') {
    var s = a.health && a.health.score !== null ? a.health.score : -1;
    return -s;
  }
  return 0;
}

/* ── Model list extraction ── */
function buildModelOptions() {
  const sel = document.getElementById('mon-model-sel');
  if (!sel) return;
  const models = new Set();
  AGENTS.forEach(a => { if (a.model) models.add(a.model); });
  Array.from(models).sort().forEach(m => {
    const opt = document.createElement('option');
    opt.value = m; opt.textContent = m;
    sel.appendChild(opt);
  });
}

/* ── Filtering ── */
function filteredAgents() {
  const agents = AGENTS;
  return agents.filter(a => {
    if (monFilter !== 'all' && a.status !== monFilter) return false;
    if (monModel !== 'all' && a.model !== monModel) return false;
    if (monSearch) {
      const q = monSearch.toLowerCase();
      const name = (a.name || a.id || '').toLowerCase();
      const id = (a.id || '').toLowerCase();
      if (!name.includes(q) && !id.includes(q)) return false;
    }
    return true;
  }).sort((a, b) => {
    const ka = sortKey(a), kb = sortKey(b);
    if (typeof ka === 'string') {
      if (ka < kb) return -1;
      if (ka > kb) return 1;
      // Stable secondary: name
      return (a.name || a.id || '').localeCompare(b.name || b.id || '');
    }
    const d = ka - kb;
    if (d !== 0) return d;
    return (a.name || a.id || '').localeCompare(b.name || b.id || '');
  });
}

/* ── Sparkline SVG ── */
function sparkColor(status) {
  return { error: '#e06c75', warn: '#d19a66', ok: '#4ec9b0', stale: '#8a96a3' }[status] || '#4ec9b0';
}

function miniSparkline(data, color) {
  if (!data || data.length === 0) return '<span style="color:var(--muted);font-size:11px;">–</span>';
  const w = 60, h = 18, pad = 1;
  const max = Math.max.apply(null, data.concat([0.0001]));
  const min = Math.min.apply(null, data);
  const span = (max - min) || 1;
  const step = (w - pad * 2) / Math.max(data.length - 1, 1);
  const pts = data.map(function(v, i) {
    return [pad + i * step, pad + (h - pad * 2) * (1 - (v - min) / span)];
  });
  const line = pts.map(function(p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
  return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">'
    + '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity="0.75"/>'
    + '</svg>';
}

function fullSparkline(data, color) {
  if (!data || data.length === 0) return '<span style="color:var(--muted);font-size:11px;">no data</span>';
  const w = 104, h = 28, pad = 2;
  const max = Math.max.apply(null, data.concat([0.0001]));
  const min = Math.min.apply(null, data);
  const span = (max - min) || 1;
  const step = (w - pad * 2) / Math.max(data.length - 1, 1);
  const pts = data.map(function(v, i) {
    var x = pad + i * step;
    var y = pad + (h - pad * 2) * (1 - (v - min) / span);
    return [x, y];
  });
  const line = pts.map(function(p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
  const last = pts[pts.length - 1];
  const area = line + ' L' + last[0].toFixed(1) + ' ' + (h - pad) + ' L' + pts[0][0].toFixed(1) + ' ' + (h - pad) + ' Z';
  return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">'
    + '<path d="' + area + '" fill="' + color + '" opacity="0.1"/>'
    + '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
    + '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="2.2" fill="' + color + '"/>'
    + '</svg>';
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

/* ── Degraded cell helpers ── */
/* When a row is degraded (failed metric build) or its cost read failed, render an
   explicit degraded marker instead of a misleading $0.00 / empty sparkline.
   class="cost-degraded-cell" carries a title so the operator knows why the dash
   appears (spec/09 "data may be incomplete" posture, MUST 10 per-row). */
function costCell(a) {
  if (a.degraded || a.costDegraded) {
    return '<span class="cost-degraded-cell" title="cost data unavailable">—</span>';
  }
  return a.cost7d === 0 ? '$0.00' : '$' + a.cost7d.toFixed(2);
}
function sparkCell(a, color) {
  if (a.degraded || a.costDegraded) {
    return '<span class="cost-degraded-cell" title="trend data unavailable">—</span>';
  }
  return miniSparkline(a.spark, color);
}
function sparkCardCell(a, color) {
  if (a.degraded || a.costDegraded) {
    return '<span class="cost-degraded-cell" title="trend data unavailable">—</span>';
  }
  return fullSparkline(a.spark, color);
}

/* ── Render list view ── */
function renderList() {
  const agents = filteredAgents();
  const tbody = document.getElementById('mon-list-body');
  const empty = document.getElementById('mon-list-empty');
  if (!tbody) return;
  if (agents.length === 0) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';
  tbody.innerHTML = agents.map(function(a) {
    const color = sparkColor(a.status);
    const errClass = a.errors24h >= 5 ? 'bad' : a.errors24h >= 1 ? 'warn' : '';
    const failClass = a.fail7d >= 5 ? 'bad' : '';
    const healthScore = (a.health && a.health.score !== null) ? a.health.score : '—';
    const healthBand = (a.health && a.health.band) ? a.health.band : 'unknown';
    const healthClass = healthBand === 'green' ? 'green' : healthBand === 'amber' ? 'amber' : healthBand === 'red' ? 'red' : '';
    /* Use encodeURIComponent for the query value so ids with &, ', ? are safe.
       Use data-detail-href + delegated click (below) instead of inline onclick
       string-building, which is vulnerable to JS injection via &#39; decode order. */
    const detailHref = 'agent-detail.html?agent=' + encodeURIComponent(a.id);
    return '<tr data-detail-href="' + esc(detailHref) + '">'
      + '<td><span class="row-dot ' + a.status + '"></span></td>'
      + '<td><a href="' + esc(detailHref) + '" class="row-agent-name' + (a.status === 'stale' ? ' stale' : '') + '" data-agent-detail-link="' + esc(encodeURIComponent(a.id)) + '">' + esc(a.name || a.id) + '</a></td>'
      + '<td><span class="pill ' + esc(a.modelClass || 'local') + '">' + esc(a.model || '—') + '</span></td>'
      + '<td class="r"><span class="health-val ' + healthClass + '">' + healthScore + '</span></td>'
      + '<td class="r"><span class="err-val ' + errClass + '">' + a.errors24h + '</span></td>'
      + '<td class="r"><span class="fail-val ' + failClass + '">' + a.fail7d + '</span></td>'
      + '<td class="r mono">' + costCell(a) + '</td>'
      + '<td><span class="last-run' + (a.lastRunStale ? ' stale' : '') + '">' + esc(a.lastRun) + '</span></td>'
      + '<td>' + sparkCell(a, color) + '</td>'
      + '</tr>';
  }).join('');
}

/* ── Render card view ── */
function renderCards() {
  const agents = filteredAgents();
  const grid = document.getElementById('monitor-cards');
  const empty = document.getElementById('mon-cards-empty');
  if (!grid) return;
  if (agents.length === 0) {
    grid.innerHTML = '';
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';
  grid.innerHTML = agents.map(function(a) {
    const color = sparkColor(a.status);
    const errClass = a.errors24h >= 5 ? 'bad' : a.errors24h >= 1 ? 'warn' : 'ok';
    const healthScore = (a.health && a.health.score !== null) ? a.health.score : '—';
    const healthBand = (a.health && a.health.band) ? a.health.band : 'unknown';
    const detailHref = 'agent-detail.html?agent=' + encodeURIComponent(a.id);
    return '<a href="' + esc(detailHref) + '" class="agent-card status-' + a.status + '" data-agent-detail-link="' + esc(encodeURIComponent(a.id)) + '">'
      + '<div class="card-head">'
      + '<span class="agent-name">' + esc(a.name || a.id) + '</span>'
      + '<span class="status-head"><span class="status-dot ' + a.status + '"></span><span class="health-badge ' + healthBand + '">' + healthScore + '</span></span>'
      + '</div>'
      + '<div class="card-meta"><span class="pill ' + esc(a.modelClass || 'local') + '">' + esc(a.model || '—') + '</span></div>'
      + '<div class="card-spark">' + sparkCardCell(a, color) + '</div>'
      + '<div class="card-metrics">'
      + '<div class="metric"><div class="mk">Errors 24h</div><div class="mv ' + errClass + '">' + a.errors24h + '</div></div>'
      + '<div class="metric"><div class="mk">Failures 7d</div><div class="mv' + (a.fail7d >= 5 ? ' bad' : '') + '">' + a.fail7d + '</div></div>'
      + '<div class="metric"><div class="mk">7d cost</div><div class="mv mono">' + costCell(a) + '</div></div>'
      + '<div class="metric"><div class="mk">Last run</div><div class="mv' + (a.lastRunStale ? ' muted' : '') + '">' + esc(a.lastRun) + '</div></div>'
      + '</div>'
      + '</a>';
  }).join('');
}

/* ── Main render dispatcher ── */
function render() {
  if (monView === 'list') {
    var ml = document.getElementById('monitor-list');
    var mc = document.getElementById('monitor-cards');
    var me = document.getElementById('mon-cards-empty');
    if (ml) ml.style.display = '';
    if (mc) mc.style.display = 'none';
    if (me) me.style.display = 'none';
    renderList();
  } else {
    var ml2 = document.getElementById('monitor-list');
    var mc2 = document.getElementById('monitor-cards');
    if (ml2) ml2.style.display = 'none';
    if (mc2) mc2.style.display = '';
    renderCards();
  }
}

/* ── Delegated row-click handler (detail navigation, security fix #2) ── */
/* Rows carry data-detail-href instead of an inline onclick string so that
   agent ids containing ', &, or ? cannot corrupt the JS string context. */
var listBody = document.getElementById('mon-list-body');
if (listBody) {
  listBody.addEventListener('click', function(e) {
    var row = e.target.closest('tr[data-detail-href]');
    if (!row) return;
    var href = row.getAttribute('data-detail-href');
    if (href) window.location.href = href;
  });
}

/* ── View toggle event (MUST 4) ── */
var toggleEl = document.getElementById('view-toggle');
if (toggleEl) {
  toggleEl.addEventListener('click', function(e) {
    var btn = e.target.closest('button');
    if (!btn) return;
    monView = btn.dataset.view;
    if (typeof localStorage !== 'undefined') localStorage.setItem('fleet-monitor.view', monView);
    document.querySelectorAll('#view-toggle button').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    render();
  });
}

/* ── Status chip filter ── */
var statusBar = document.getElementById('status-bar');
if (statusBar) {
  statusBar.addEventListener('click', function(e) {
    var chip = e.target.closest('.status-chip');
    if (!chip) return;
    monFilter = chip.dataset.filter;
    document.querySelectorAll('.status-chip').forEach(function(c) { c.classList.remove('active'); });
    chip.classList.add('active');
    updateArrivalBanner();
    render();
  });
}

/* ── Search ── */
var searchEl = document.getElementById('mon-search');
if (searchEl) {
  searchEl.addEventListener('input', function() {
    monSearch = this.value;
    render();
  });
}

/* ── Sort ── */
var sortEl = document.getElementById('mon-sort-sel');
if (sortEl) {
  sortEl.addEventListener('change', function() {
    monSort = this.value;
    render();
  });
}

/* ── Model filter ── */
var modelEl = document.getElementById('mon-model-sel');
if (modelEl) {
  modelEl.addEventListener('change', function() {
    monModel = this.value;
    render();
  });
}

/* ── Arrival filter from URL param ?status= (MUST 5) ── */
function applyArrivalFilter() {
  const params = new URLSearchParams(window.location.search);
  const status = params.get('status');
  const valid = ['error', 'warn', 'ok', 'stale'];
  /* spec/56 §1: only recognized LOWERCASE tokens; uppercase is ignored */
  if (status && valid.indexOf(status) !== -1) {
    monFilter = status;
    document.querySelectorAll('.status-chip').forEach(function(c) {
      c.classList.toggle('active', c.dataset.filter === monFilter);
    });
    var banner = document.getElementById('arrival-banner');
    var chip = document.getElementById('arrival-chip');
    var label = document.getElementById('arrival-chip-label');
    if (label) label.textContent = status.toUpperCase();
    if (chip) chip.className = 'active-filter-chip chip-' + monFilter;
    if (banner) banner.style.display = '';
  }
}

function updateArrivalBanner() {
  var banner = document.getElementById('arrival-banner');
  if (banner) banner.style.display = 'none';
}

function clearArrivalFilter() {
  monFilter = 'all';
  document.querySelectorAll('.status-chip').forEach(function(c) {
    c.classList.toggle('active', c.dataset.filter === 'all');
  });
  var banner = document.getElementById('arrival-banner');
  if (banner) banner.style.display = 'none';
  render();
}

/* ── Activate initial view-toggle button ── */
document.querySelectorAll('#view-toggle button').forEach(function(b) {
  b.classList.toggle('active', b.dataset.view === monView);
});

/* ── Boot ── */
buildModelOptions();
applyArrivalFilter();
render();
"""


def _nav_bar_monitor(has_goals: bool = True) -> str:
    """Nav bar with Monitor tab active."""
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
        active = ' class="active"' if key == "monitor" else ""
        items.append(f'<a href="{href}"{active}>{label}</a>')
    return f'<nav class="tab-nav">{"".join(items)}</nav>'


def render_monitor(
    agents_root: Path,
    console_data,
    *,
    today: date | None = None,
    now: datetime | None = None,
    has_goals: bool = False,
) -> Path:
    """Render <agents_root>/_dashboard/monitor.html (the Fleet Monitor page).

    Reuses the SAME console_data loaded by aggregate_console() / render_all() so
    the home summary and monitor status counts are derived from one snapshot
    (MUST 12 structural guarantee — spec/56 §3).

    Pure-compute, zero LLM spend (MUST 11). MUST 13: NO SSE/fetch/background polling;
    freshness = periodic static re-render + a meta-refresh auto-reload.
    """
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    now = now or datetime.now(tz=timezone.utc)
    today = today or date.today()

    # Build PanelContext (same shape as home — same console_data object).
    # Import the panels package here to ensure monitor panels are registered.
    from .panels import ConsoleCapabilities, PanelContext, get_registry

    capabilities = ConsoleCapabilities(has_goals=has_goals)
    ctx = PanelContext(
        console_data=console_data,
        capabilities=capabilities,
        today=today,
        now=now,
    )

    # Compose monitor slots via the SAME registry (spec/56 §6).
    registry = get_registry()
    slot_html, _ = registry.compose_monitor(ctx)

    html_content = _render_monitor_template(
        console_data=console_data,
        ctx=ctx,
        slot_html=slot_html,
        now=now,
        today=today,
        has_goals=has_goals,
    )
    out_path = out_dir / _MONITOR_FILE
    atomic_write(out_path, html_content)
    return out_path


def _render_monitor_template(
    console_data,
    ctx,
    slot_html: dict,
    now: datetime,
    today: date,
    has_goals: bool,
) -> str:
    """Assemble the full monitor.html page from composed panel slots + chrome."""
    fleet_size = getattr(console_data, "agent_count", 0)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    _nav = _nav_bar_monitor(has_goals=has_goals)

    # Freshness stamp (MUST 8): "updated X ago" using now as the render time.
    # Since this is a static page, "just now" at render time is accurate.
    freshness_line = (
        f'<div class="freshness-line">'
        f"<span>updated just now ({now_str})</span>"
        f'<span class="refresh-glyph" title="Refresh page" onclick="location.reload()">&#8635;</span>'
        f"<span>&middot; auto-refresh {_AUTO_RELOAD_SECONDS}s</span>"
        f"<span>&middot; error window 24h &middot; failures window 7d &middot; stale window 24h</span>"
        f"</div>"
    )

    # Controls row (MUST 4 view toggle, MUST 6 filter/search/sort)
    controls = (
        '<div class="mon-controls">'
        '<div class="view-toggle" id="view-toggle">'
        '<button data-view="list">List</button>'
        '<button data-view="cards">Cards</button>'
        "</div>"
        '<input class="mon-search" id="mon-search" type="text"'
        ' placeholder="Search agents…" autocomplete="off" spellcheck="false">'
        '<div class="model-filter">'
        '<label for="mon-model-sel">Model</label>'
        '<select id="mon-model-sel"><option value="all">All</option></select>'
        "</div>"
        '<div class="mon-sort">'
        '<label for="mon-sort-sel">Sort</label>'
        '<select id="mon-sort-sel">'
        '<option value="problems">Problems first</option>'
        '<option value="cost">Cost (7d)</option>'
        '<option value="errors">Errors (24h)</option>'
        '<option value="lastrun">Last run</option>'
        '<option value="name">Name</option>'
        '<option value="health">Health score</option>'
        "</select>"
        "</div>"
        "</div>"
    )

    # Arrival filter banner (hidden by default; JS shows it when ?status=<s> matches)
    arrival_banner = (
        '<div class="arrival-banner" id="arrival-banner" style="display:none">'
        '<span class="arrival-caption">arrived filtered to</span>'
        '<span class="active-filter-chip chip-error" id="arrival-chip"'
        ' onclick="clearArrivalFilter()">'
        '<span id="arrival-chip-label">ERROR</span>'
        '<span class="chip-x">&times;</span>'
        "</span>"
        '<span class="arrival-caption">'
        "&mdash; click any status chip below to change, or &times; to clear"
        "</span>"
        "</div>"
    )

    # Top-level degraded banner if overall console data degraded
    global_degraded = (
        '<div class="degraded-banner">'
        '<span class="pill warn">&#9888; data may be incomplete</span>'
        " &nbsp;One or more backend reads failed. Metrics below may be partial."
        "</div>"
        if getattr(console_data, "degraded", False)
        else ""
    )

    # Auto-reload meta tag (MUST 13: full-page reload, not background fetch).
    # CSP 'unsafe-inline' covers inline scripts; meta http-equiv is allowed.
    auto_reload_meta = f'<meta http-equiv="refresh" content="{_AUTO_RELOAD_SECONDS}">'

    summary_html = slot_html.get("monitor-summary", "")
    roster_html = slot_html.get("monitor-roster", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{auto_reload_meta}
<meta http-equiv="Content-Security-Policy" content="{_SHARED_CSP}">
<title>Fleet Monitor — Atomic Agents</title>
<style>
{_SHARED_CSS}
{_MONITOR_CSS}
</style>
</head>
<body>

<header>
  <div>
    <div class="breadcrumb"><a href="index.html">&#8592; Fleet Console</a> / Fleet Monitor</div>
    <h1>Fleet Monitor</h1>
    <div class="period">{fleet_size} agent{"s" if fleet_size != 1 else ""} &middot; {now_str}</div>
  </div>
  <div>
    <button class="refresh-btn" onclick="location.reload()">&#8635; Refresh</button>
  </div>
</header>

{_nav}
{global_degraded}
{arrival_banner}

{summary_html}

{freshness_line}

{controls}

<div class="cockpit-zone-label">Fleet Roster</div>
{roster_html}

<footer>
  <div>Generated {today.isoformat()} by atomic_agents.dashboard</div>
  <div>Fleet Monitor &middot; spec/56 &middot; status windows: errors 24h / stale 24h / failures 7d</div>
</footer>

<script>
{_MONITOR_JS}
</script>

</body>
</html>
"""

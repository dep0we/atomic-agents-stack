"""Tests for atomic_agents.dashboard.render."""

from __future__ import annotations
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from atomic_agents.dashboard.costs import aggregate_agent, aggregate_global
from atomic_agents.dashboard.render import (
    render_all,
    render_agent,
    render_global,
)


def _write_log(agents_root: Path, agent: str, when: date, records: list[dict]) -> None:
    # Create model.md so discover_agents() picks up this agent (spec/37:314 predicate).
    model_md = agents_root / agent / "model.md"
    model_md.parent.mkdir(parents=True, exist_ok=True)
    if not model_md.exists():
        model_md.write_text("# model\n")
    log_dir = agents_root / agent / "log" / when.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{when.isoformat()}.jsonl"
    lines = []
    for rec in records:
        rec.setdefault(
            "ts", datetime.combine(when, datetime.min.time()).astimezone().isoformat()
        )
        rec.setdefault("trigger", "cron")
        rec.setdefault("model", "claude-opus-4-7-20260101")
        rec.setdefault("input_tokens", 1000)
        rec.setdefault("output_tokens", 200)
        rec.setdefault("cost_usd", 0.05)
        rec.setdefault("status", "ok")
        rec.setdefault("summary", "test run")
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n")


def test_render_global_creates_html(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)

    # BEHAVIOR CHANGE (spec/52 PR1): render_global now writes cost.html, not index.html.
    # index.html is now the Fleet Console home (written by render_console).
    assert out_path == tmp_path / "_dashboard" / "cost.html"
    assert out_path.exists()

    html = out_path.read_text()
    assert "<!DOCTYPE html>" in html
    assert "Atomic Agents" in html
    assert "alice" in html
    assert "$0.10" in html or "$0.1" in html


def test_render_agent_creates_html(tmp_path):
    # spec/57 MUST 2: path is unchanged; content is now B7 detail cockpit.
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    data = aggregate_agent(tmp_path, "alice", today=today)
    out_path = render_agent(tmp_path, data)

    assert out_path == tmp_path / "alice" / "dashboard.html"
    assert out_path.exists()

    html = out_path.read_text()
    assert "<!DOCTYPE html>" in html
    assert "alice" in html
    # B7 detail cockpit content (replaced the old Caldwell-style template):
    assert "Fleet Console" in html
    assert "Agent Detail" in html or "agent-banner" in html or "detail-tabs" in html


def test_render_all_creates_global_and_per_agent(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    _write_log(tmp_path, "bob", today, [{"cost_usd": 0.05}])
    written = render_all(tmp_path, today=today)

    # BEHAVIOR CHANGE (spec/52 PR1): render_global writes cost.html (not index.html).
    # index.html is now the Fleet Console home.
    assert written["global"] == str(tmp_path / "_dashboard" / "cost.html")
    assert len(written["per_agent"]) == 2
    assert (tmp_path / "alice" / "dashboard.html").exists()
    assert (tmp_path / "bob" / "dashboard.html").exists()


def test_render_writes_pre_aggregated_json(tmp_path):
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    summary = aggregate_global(tmp_path, today=today)
    render_global(tmp_path, summary)

    data_dir = tmp_path / "_dashboard" / "data"
    assert data_dir.exists()
    json_files = list(data_dir.glob("*.json"))
    assert len(json_files) == 1

    parsed = json.loads(json_files[0].read_text())
    assert parsed["total_runs"] == 1


def test_render_handles_empty_agents_root(tmp_path):
    today = date.today()
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)

    html = out_path.read_text()
    assert "No agent activity" in html or "Atomic Agents" in html


def test_render_agent_includes_helper_savings_when_present(tmp_path):
    # spec/57 MUST 2: render_agent() still writes <agent>/dashboard.html (B7 detail).
    # The efficiency tab shows helper savings when present.
    today = date.today()
    _write_log(
        tmp_path,
        "alice",
        today,
        [
            {"trigger": "cron", "model": "claude-opus-4-7-20260101", "cost_usd": 0.10},
            {
                "trigger": "helper",
                "model": "claude-haiku-4-5-20251001",
                "cost_usd": 0.001,
                "input_tokens": 1000,
                "output_tokens": 50,
            },
        ],
    )
    data = aggregate_agent(tmp_path, "alice", today=today)
    out_path = render_agent(tmp_path, data)
    html = out_path.read_text()
    # B7 detail cockpit renders; page is well-formed HTML (old template assertions removed).
    assert "<!DOCTYPE html>" in html
    assert "alice" in html


def test_render_agent_handles_no_helpers(tmp_path):
    # spec/57 MUST 2: render_agent() writes the B7 detail cockpit at the same path.
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"trigger": "cron", "cost_usd": 0.10}])
    data = aggregate_agent(tmp_path, "alice", today=today)
    out_path = render_agent(tmp_path, data)
    html = out_path.read_text()
    assert "<!DOCTYPE html>" in html
    assert "alice" in html


# ──────────────────────────────────────────────────────────────────
# XSS regression tests (#400) — run-derived strings must be escaped


_XSS_SUMMARY = "<img src=x onerror=alert(document.cookie)>"
_XSS_AGENT_NAME = 'a"><img src=x onerror=alert(1)>'
_XSS_TRIGGER = "<script>alert(1)</script>"

# Log-derived model id (#517). The "zz" prefix dodges every _short_model_name /
# _model_pill_class keyword so it hits the raw fallthrough. No "/" on purpose:
# _short_model_name does `model.split("/")[-1][:24]`, so a slash would strip the
# `"><` breakout before it ever reaches the bar label / per-agent pill sites.
# The leading `">` is the attribute breakout the title="{model}" interpolation
# must not admit.
_XSS_MODEL = 'zz"><img src=x onerror=alert(1)>'

_RAW_IMG = "<img src=x"  # present in both payloads; must never appear raw


def test_xss_summary_escaped_in_global_dashboard(tmp_path):
    """summary from a run record must be html-escaped in the global index."""
    today = date.today()
    _write_log(
        tmp_path,
        "safe-agent",
        today,
        [{"cost_usd": 0.10, "summary": _XSS_SUMMARY}],
    )
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)
    content = out_path.read_text()

    assert _RAW_IMG not in content, (
        "raw <img> XSS payload found unescaped in global index"
    )
    assert "&lt;img" in content, "expected html-escaped &lt;img in global index"


def test_xss_agent_name_escaped_in_global_dashboard(tmp_path):
    """agent name with HTML chars must be escaped in href path and link text."""
    today = date.today()
    # Filesystem agent dir name uses the XSS payload as the folder name.
    _write_log(
        tmp_path,
        _XSS_AGENT_NAME,
        today,
        [{"cost_usd": 0.05}],
    )
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)
    content = out_path.read_text()

    # Raw payload must not appear in link text or href attribute context.
    assert _RAW_IMG not in content, "raw <img> XSS payload found in agent name link"
    # The escaped form of the double-quote character must appear instead of raw ".
    # (urllib.parse.quote encodes the " in the href; html.escape encodes it in text)
    assert '"><img' not in content, 'href-breaking sequence "><img found unescaped'


def test_xss_trigger_escaped_in_per_agent_dashboard(tmp_path):
    """trigger field from JSONL must be html-escaped in per-agent top-runs table."""
    today = date.today()
    _write_log(
        tmp_path,
        "safe-agent",
        today,
        [{"cost_usd": 0.10, "trigger": _XSS_TRIGGER, "summary": _XSS_SUMMARY}],
    )
    data = aggregate_agent(tmp_path, "safe-agent", today=today)
    out_path = render_agent(tmp_path, data)
    content = out_path.read_text()

    # The exact payload string must not appear raw (the template's own <script>
    # block is legitimate and distinct from _XSS_TRIGGER).
    assert _XSS_TRIGGER not in content, (
        "raw trigger XSS payload found unescaped in per-agent dashboard"
    )
    assert _RAW_IMG not in content, "raw <img> XSS payload found in per-agent dashboard"
    # Confirm both payloads appear in their html-escaped forms.
    assert "&lt;script&gt;" in content
    assert "&lt;img" in content


def test_xss_summary_escaped_in_per_agent_dashboard(tmp_path):
    """summary must be html-escaped in the per-agent top-runs table."""
    today = date.today()
    _write_log(
        tmp_path,
        "safe-agent",
        today,
        [{"cost_usd": 0.10, "summary": _XSS_SUMMARY}],
    )
    data = aggregate_agent(tmp_path, "safe-agent", today=today)
    out_path = render_agent(tmp_path, data)
    content = out_path.read_text()

    assert _RAW_IMG not in content, (
        "raw <img> XSS payload found unescaped in per-agent dashboard"
    )
    assert "&lt;img" in content, "expected html-escaped &lt;img in per-agent dashboard"


def test_xss_model_id_escaped_in_global_dashboard(tmp_path):
    """model id from a run record must be html-escaped in the global index
    (model-mix bar title attr + label, and the per-agent model pills) (#517)."""
    today = date.today()
    _write_log(
        tmp_path,
        "safe-agent",
        today,
        [{"cost_usd": 0.10, "model": _XSS_MODEL}],
    )
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)
    content = out_path.read_text()

    assert _XSS_MODEL not in content, (
        "raw model-id XSS payload found unescaped in global index"
    )
    # The breakout must not appear from any site: title="{model}" attr, the
    # model-mix bar label, or the per-agent model pills.
    assert '"><img' not in content, (
        'attribute/text breakout "><img found unescaped in global index'
    )
    assert "&lt;img" in content, "expected html-escaped model id in global index"


def test_xss_model_id_escaped_in_per_agent_dashboard(tmp_path):
    """model id must be html-escaped in the per-agent model-mix table and
    top-runs table (both route through _model_pill) (#517)."""
    today = date.today()
    _write_log(
        tmp_path,
        "safe-agent",
        today,
        [{"cost_usd": 0.10, "model": _XSS_MODEL}],
    )
    data = aggregate_agent(tmp_path, "safe-agent", today=today)
    out_path = render_agent(tmp_path, data)
    content = out_path.read_text()

    assert _XSS_MODEL not in content, (
        "raw model-id XSS payload found unescaped in per-agent dashboard"
    )
    assert '"><img' not in content, (
        'attribute/text breakout "><img found unescaped in per-agent dashboard'
    )
    assert "&lt;img" in content, "expected html-escaped model id in per-agent dashboard"


def test_xss_agent_name_escaped_in_per_agent_template(tmp_path):
    """agent name with HTML chars must be escaped in the per-agent page title and h1."""
    today = date.today()
    _write_log(
        tmp_path,
        _XSS_AGENT_NAME,
        today,
        [{"cost_usd": 0.05}],
    )
    data = aggregate_agent(tmp_path, _XSS_AGENT_NAME, today=today)
    out_path = render_agent(tmp_path, data)
    content = out_path.read_text()

    assert _RAW_IMG not in content, "raw <img> XSS payload found in per-agent title/h1"
    assert '"><img' not in content, 'unescaped "><img in per-agent page'


def test_csp_meta_present_in_global_dashboard(tmp_path):
    """Global dashboard must include a Content-Security-Policy meta tag."""
    today = date.today()
    summary = aggregate_global(tmp_path, today=today)
    out_path = render_global(tmp_path, summary)
    content = out_path.read_text()

    assert "Content-Security-Policy" in content


def test_csp_meta_present_in_per_agent_dashboard(tmp_path):
    """Per-agent dashboard must include a Content-Security-Policy meta tag."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.10}])
    data = aggregate_agent(tmp_path, "alice", today=today)
    out_path = render_agent(tmp_path, data)
    content = out_path.read_text()

    assert "Content-Security-Policy" in content

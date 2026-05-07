"""Integration tests: render all tabs against a synthetic vault.

Verifies:
- All five HTML files are written when goals exist
- Nav bar present and consistent across all pages
- goals.html OMITTED when no goal.md exists
- Each page has expected content markers
"""

from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atomic_agents.dashboard.render import render_all
from atomic_agents.dashboard._shared import nav_bar


# ──────────────────────────────────────────────────────────────────
# Helpers

def _write_log(agents_root: Path, agent: str, when: date, records: list[dict]) -> None:
    log_dir = agents_root / agent / "log" / when.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{when.isoformat()}.jsonl"
    lines = []
    for rec in records:
        rec.setdefault("ts", datetime.combine(when, datetime.min.time()).isoformat())
        rec.setdefault("trigger", "cron")
        rec.setdefault("model", "claude-opus-4-7-20260101")
        rec.setdefault("input_tokens", 1000)
        rec.setdefault("output_tokens", 200)
        rec.setdefault("cost_usd", 0.05)
        rec.setdefault("status", "ok")
        rec.setdefault("summary", "test run")
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n")


def _write_goal(agents_root: Path, agent: str) -> None:
    agent_root = agents_root / agent
    goal_content = """---
schema_version: 1
active: true
intent: Ship the new feature
priority: high
created: 2026-05-01
last_progress_check: 2026-05-07
success_criteria:
  - feature is shipped
sub_goals:
  - id: sg1
    label: Design
    status: complete
  - id: sg2
    label: Implement
    status: in_progress
---
# Goal history
"""
    (agent_root / "goal.md").write_text(goal_content)


def _build_synthetic_vault(agents_root: Path, with_goals: bool = True) -> None:
    """Create a minimal synthetic vault with two agents."""
    today = date.today()

    for agent in ("alice", "bob"):
        _write_log(agents_root, agent, today, [
            {"cost_usd": 0.10, "status": "ok", "summary": "morning brief"},
            {"cost_usd": 0.05, "status": "error", "summary": "failed run"},
        ])
        # Write some memory notes
        mem_dir = agents_root / agent / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "pref.md").write_text("---\ntype: user\nlast_seen: 2026-05-01\n---\nPreferences.")
        (mem_dir / "INDEX.md").write_text("# Index\n- pref\n")

    if with_goals:
        _write_goal(agents_root, "alice")


# ──────────────────────────────────────────────────────────────────
# Tests

def test_render_all_with_goals_creates_five_files(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=True)
    written = render_all(tmp_path)

    dashboard_dir = tmp_path / "_dashboard"
    assert (dashboard_dir / "index.html").exists()
    assert (dashboard_dir / "activity.html").exists()
    assert (dashboard_dir / "quality.html").exists()
    assert (dashboard_dir / "memory.html").exists()
    assert (dashboard_dir / "goals.html").exists()

    assert written.get("global") is not None
    assert written.get("activity") is not None
    assert written.get("quality") is not None
    assert written.get("memory") is not None
    assert written.get("goals") is not None


def test_render_all_without_goals_omits_goals_html(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    written = render_all(tmp_path)

    dashboard_dir = tmp_path / "_dashboard"
    assert (dashboard_dir / "index.html").exists()
    assert (dashboard_dir / "activity.html").exists()
    assert not (dashboard_dir / "goals.html").exists()
    assert written.get("goals") is None


def test_nav_bar_present_on_all_pages_with_goals(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=True)
    render_all(tmp_path)

    dashboard_dir = tmp_path / "_dashboard"
    for page in ("index.html", "activity.html", "quality.html", "memory.html", "goals.html"):
        html = (dashboard_dir / page).read_text()
        assert 'class="tab-nav"' in html, f"tab-nav missing from {page}"
        # All 5 tab links should be present
        assert "activity.html" in html, f"activity link missing from {page}"
        assert "quality.html" in html, f"quality link missing from {page}"
        assert "memory.html" in html, f"memory link missing from {page}"
        assert "goals.html" in html, f"goals link missing from {page}"


def test_nav_bar_active_class_per_page(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=True)
    render_all(tmp_path)

    dashboard_dir = tmp_path / "_dashboard"
    page_expectations = {
        "index.html":    'href="index.html" class="active"',
        "activity.html": 'href="activity.html" class="active"',
        "quality.html":  'href="quality.html" class="active"',
        "memory.html":   'href="memory.html" class="active"',
        "goals.html":    'href="goals.html" class="active"',
    }
    for page, expected_fragment in page_expectations.items():
        html = (dashboard_dir / page).read_text()
        assert expected_fragment in html, f"{page} missing active class on its own link"


def test_nav_bar_no_goals_link_when_no_goal(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)

    dashboard_dir = tmp_path / "_dashboard"
    for page in ("index.html", "activity.html", "quality.html", "memory.html"):
        html = (dashboard_dir / page).read_text()
        assert "goals.html" not in html, f"goals.html link should be absent from {page}"


def test_index_html_content_unchanged(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)

    html = (tmp_path / "_dashboard" / "index.html").read_text()
    assert "Atomic Agents" in html
    assert "Spend this month" in html
    assert "Per-agent breakdown" in html


def test_activity_html_content(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)

    html = (tmp_path / "_dashboard" / "activity.html").read_text()
    assert "Activity Pulse" in html
    assert "Runs last 24h" in html
    assert "Recent failures" in html


def test_quality_html_content(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)

    html = (tmp_path / "_dashboard" / "quality.html").read_text()
    assert "Quality Trends" in html
    assert "Eval score trend" in html
    assert "Hard-fail" in html


def test_memory_html_content(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)

    html = (tmp_path / "_dashboard" / "memory.html").read_text()
    assert "Memory Snapshot" in html
    assert "Note counts" in html
    assert "Staleness" in html


def test_goals_html_content(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=True)
    render_all(tmp_path)

    html = (tmp_path / "_dashboard" / "goals.html").read_text()
    assert "Goals &amp; Outcomes" in html or "Goals & Outcomes" in html
    assert "Active goals" in html
    assert "Ship the new feature" in html
    assert "Blocked sub-goals" in html


def test_render_tab_filter_activity_only(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    written = render_all(tmp_path, tab="activity")

    assert written.get("activity") is not None
    # Cost/global not rendered with --tab activity
    assert written.get("global") is None
    # File should exist
    assert (tmp_path / "_dashboard" / "activity.html").exists()


def test_render_tab_filter_memory_only(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=False)
    written = render_all(tmp_path, tab="memory")

    assert written.get("memory") is not None
    assert written.get("global") is None
    assert written.get("activity") is None


def test_all_pages_are_valid_html(tmp_path):
    _build_synthetic_vault(tmp_path, with_goals=True)
    render_all(tmp_path)

    dashboard_dir = tmp_path / "_dashboard"
    for page in ("index.html", "activity.html", "quality.html", "memory.html", "goals.html"):
        html = (dashboard_dir / page).read_text()
        assert html.startswith("<!DOCTYPE html>"), f"{page} doesn't start with DOCTYPE"
        assert "</html>" in html, f"{page} is missing closing </html>"
        assert "<body>" in html, f"{page} is missing <body>"
        assert "</body>" in html, f"{page} is missing </body>"


def test_nav_bar_helper_functions():
    """Unit test the nav_bar() helper directly."""
    html_with_goals = nav_bar("cost", has_goals=True)
    assert 'href="index.html"' in html_with_goals
    assert 'href="activity.html"' in html_with_goals
    assert 'href="goals.html"' in html_with_goals
    assert 'class="active"' in html_with_goals

    html_no_goals = nav_bar("activity", has_goals=False)
    assert "goals.html" not in html_no_goals
    assert 'href="activity.html" class="active"' in html_no_goals

    # Each page marks itself as active
    for tab_name in ("cost", "activity", "quality", "memory"):
        nav = nav_bar(tab_name, has_goals=True)
        # The active page's href should have class="active"
        expected_href = "index.html" if tab_name == "cost" else f"{tab_name}.html"
        assert f'href="{expected_href}" class="active"' in nav

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

from atomic_agents.dashboard.render import (
    render_all,
    render_global,
    render_agent,
    _render_global_template,
    _render_agent_template,
)
from atomic_agents.dashboard.costs import aggregate_global, aggregate_agent
from atomic_agents.dashboard._shared import nav_bar


# ──────────────────────────────────────────────────────────────────
# Helpers


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
        _write_log(
            agents_root,
            agent,
            today,
            [
                {"cost_usd": 0.10, "status": "ok", "summary": "morning brief"},
                {"cost_usd": 0.05, "status": "error", "summary": "failed run"},
            ],
        )
        # Write some memory notes
        mem_dir = agents_root / agent / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "pref.md").write_text(
            "---\ntype: user\nlast_seen: 2026-05-01\n---\nPreferences."
        )
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
    for page in (
        "index.html",
        "activity.html",
        "quality.html",
        "memory.html",
        "goals.html",
    ):
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
        "index.html": 'href="index.html" class="active"',
        "activity.html": 'href="activity.html" class="active"',
        "quality.html": 'href="quality.html" class="active"',
        "memory.html": 'href="memory.html" class="active"',
        "goals.html": 'href="goals.html" class="active"',
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
    """index.html is now the Fleet Console home (spec/52 PR1 BEHAVIOR CHANGE).

    The cost view (previously index.html) has moved to cost.html.
    This test is updated to assert the new landing-page content (Fleet Console)
    and a companion assertion verifies cost.html has the old cost-view content.
    """
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)

    # index.html is now the Fleet Console home
    index_html = (tmp_path / "_dashboard" / "index.html").read_text()
    assert "Atomic Agents" in index_html or "Fleet Console" in index_html
    assert "Fleet Console" in index_html

    # cost.html carries the old cost-view content
    cost_html = (tmp_path / "_dashboard" / "cost.html").read_text()
    assert "Spend this month" in cost_html
    assert "Per-agent breakdown" in cost_html


# ──────────────────────────────────────────────────────────────────
# Panelized console layout — structural invariants (spec/52 §16, Cockpit #635)
#
# These replace the old "inline section content" assertions: the home is now
# composed by the panel registry, so the integration test guards the PANELIZED
# structure (zones compose in order, panel markers present, no inline-only
# structures, fail-soft keeps the page whole), not literal inline strings.


def test_console_home_composes_zones_in_order(tmp_path):
    """The home composes STATUS → ACT → EXPLORE in that order, with all three
    zone-label dividers present (the panelized layout contract)."""
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)
    html = (tmp_path / "_dashboard" / "index.html").read_text()

    # Zone dividers appear in STATUS → ACT → EXPLORE order.
    i_status = html.find(">Status<")
    i_act = html.find(">Act<")
    i_explore = html.find(">Explore<")
    assert -1 < i_status < i_act < i_explore, (
        "zones must compose in STATUS → ACT → EXPLORE order"
    )


def test_console_home_panel_markers_present(tmp_path):
    """The home carries the registered panels' distinctive markers — proving the
    page is composed from the registry (KPI strip, attention queue, trends,
    fleet-status), not an inline template."""
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)
    html = (tmp_path / "_dashboard" / "index.html").read_text()

    assert 'class="kpis cockpit-kpis"' in html, "KPI-strip panel marker"
    assert "Operator Attention Queue" in html, "attention-queue panel marker"
    assert "Fleet Trends" in html, "three-axis trends panel marker"
    assert 'class="fo-grid"' in html, "fleet-status summary panel marker"


def test_console_home_no_inline_card_grid(tmp_path):
    """MUST 15 at the integration boundary: the panelized home no longer renders the
    per-agent card grid (it moved to the Fleet Monitor #653)."""
    _build_synthetic_vault(tmp_path, with_goals=False)
    render_all(tmp_path)
    html = (tmp_path / "_dashboard" / "index.html").read_text()
    assert 'class="agent-grid"' not in html
    assert 'class="agent-card"' not in html


def test_console_home_fail_soft_keeps_page_whole(tmp_path):
    """MUST 11 at the integration boundary: if ONE registered panel raises, the page
    still renders with its sibling panels and all zone chrome intact."""
    import atomic_agents.dashboard.panels._registry as _reg_mod
    from atomic_agents.dashboard.panels._registry import PanelRegistry

    _build_synthetic_vault(tmp_path, with_goals=False)

    # Wrap the live registry: make the attention-queue panel raise, leave the rest.
    live = _reg_mod.get_registry()

    class _Boom:
        id = "attention_queue"  # shadow id (we swap into a fresh registry)
        slot = "act"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            raise RuntimeError("intentional panel failure")

    patched = PanelRegistry()
    for p in live.panels:
        patched.register(_Boom() if p.id == "attention_queue" else p)

    original = _reg_mod._REGISTRY
    _reg_mod._REGISTRY = patched
    try:
        render_all(tmp_path)
    finally:
        _reg_mod._REGISTRY = original

    html = (tmp_path / "_dashboard" / "index.html").read_text()
    # The page is whole: sibling panels rendered, all zone dividers present.
    assert "Fleet Console" in html
    assert 'class="kpis cockpit-kpis"' in html, "sibling STATUS panel intact"
    assert "Fleet Trends" in html, "sibling ACT panel intact"
    assert html.count("cockpit-zone-label") >= 3, (
        "all zone dividers survive a panel failure"
    )
    # The failed panel's content is absent (it degraded to empty).
    assert "Operator Attention Queue" not in html


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
    for page in (
        "index.html",
        "activity.html",
        "quality.html",
        "memory.html",
        "goals.html",
    ):
        html = (dashboard_dir / page).read_text()
        assert html.startswith("<!DOCTYPE html>"), f"{page} doesn't start with DOCTYPE"
        assert "</html>" in html, f"{page} is missing closing </html>"
        assert "<body>" in html, f"{page} is missing <body>"
        assert "</body>" in html, f"{page} is missing </body>"


def test_nav_bar_helper_functions():
    """Unit test the nav_bar() helper directly.

    BEHAVIOR CHANGE (spec/52 PR1): the Cost tab now links to cost.html (was
    index.html). index.html is now the Console tab's href (the new front door).
    """
    html_with_goals = nav_bar("cost", has_goals=True)
    # Console tab links to index.html (new front door)
    assert 'href="index.html"' in html_with_goals
    # Cost tab links to cost.html (not index.html any more)
    assert 'href="cost.html"' in html_with_goals
    assert 'href="activity.html"' in html_with_goals
    assert 'href="goals.html"' in html_with_goals
    assert 'class="active"' in html_with_goals

    html_no_goals = nav_bar("activity", has_goals=False)
    assert "goals.html" not in html_no_goals
    assert 'href="activity.html" class="active"' in html_no_goals

    # Each page marks itself as active
    for tab_name in ("cost", "activity", "quality", "memory"):
        nav = nav_bar(tab_name, has_goals=True)
        # The active page's href: cost → cost.html; others → <name>.html
        # (spec/52 PR1: cost tab no longer uses index.html)
        expected_href = f"{tab_name}.html"
        assert f'href="{expected_href}" class="active"' in nav


# ──────────────────────────────────────────────────────────────────
# #498 — degraded-read banner integration tests
#
# Strategy: monkeypatch get_default_log_backend at the logs module level to
# raise LogBackendReadError for a specific agent, then render and assert the
# banner text appears in the output HTML. This directly tests the full chain:
#   _load_runs_with_degraded → aggregate_* → _render_*_template → HTML output
#
# Banner correctness is asserted on the IN-MEMORY template output
# (_render_global_template / _render_agent_template) rather than the
# atomic_write→read_text round-trip — the read-back path inherits this
# project's documented macOS APFS atomic-write/read flake (MEMORY: "macOS
# APFS WAL flake"). The write path is smoke-covered (file exists) without a
# banner-text assertion so the flake cannot produce an intermittent false
# failure on the load-bearing correctness check.
#
# Per the prep findings (P1): use monkeypatch injection rather than file-based
# I/O to trigger the degraded path — avoids tz-aware timestamp edge cases in
# _write_log and directly exercises the LogBackendReadError code path.


def _make_failing_get_default_log_backend(fail_agent: str):
    """Return a patched get_default_log_backend that raises LogBackendReadError
    for the named agent while delegating to the real backend for all others."""
    from unittest.mock import MagicMock

    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError

    original_get = logs_mod.get_default_log_backend

    def _patched(root):
        if root.name == fail_agent:
            mock = MagicMock()
            mock.query.side_effect = LogBackendReadError("injected failure")
            return mock
        return original_get(root)

    return _patched


def test_global_cost_view_banner_appears_on_degraded_read(tmp_path, monkeypatch):
    """Integration: global cost page shows 'data may be incomplete' banner on degraded read.

    Verifies the full chain: LogBackendReadError → aggregate_global →
    GlobalSummary.cost_data_degraded=True → render_global → HTML banner text.

    Also verifies PARTIAL RENDER (alice's data is present) so we know the banner
    is triggered by a degraded read, NOT by a completely empty render path.
    Negative control: without the monkeypatch, the banner is absent.
    """
    today = date.today()
    # Alice writes clean data; bob's backend will fail
    _write_log(
        tmp_path,
        "alice",
        today,
        [{"cost_usd": 0.10, "status": "ok", "summary": "alice run"}],
    )
    _write_log(
        tmp_path,
        "bob",
        today,
        [{"cost_usd": 0.05, "status": "ok", "summary": "bob run"}],
    )

    import atomic_agents.logs as logs_mod

    monkeypatch.setattr(
        logs_mod,
        "get_default_log_backend",
        _make_failing_get_default_log_backend("bob"),
    )

    summary = aggregate_global(tmp_path, today=today)
    assert summary.cost_data_degraded is True  # pre-render sanity check

    # Assert on the in-memory template output, NOT the atomic_write→read_text
    # round-trip. The banner correctness is a property of the template; routing
    # the assertion through disk would inherit the documented macOS APFS
    # atomic-write/read-back flake (MEMORY: "macOS APFS WAL flake") that bit
    # this exact test once on a cold run. The write path is smoke-covered
    # separately below.
    rendered_html = _render_global_template(summary)

    # Branch-distinctive assertion: banner text (not the shared empty-render path)
    assert "data may be incomplete" in rendered_html, (
        "Global cost page must contain 'data may be incomplete' banner when degraded"
    )
    # Partial render check: alice's partial total is present, not a blank page
    assert "Spend this month" in rendered_html

    # Write-path smoke: the file is produced without crashing (no banner-text
    # assertion on the read-back — that would re-introduce the APFS flake).
    out_path = render_global(tmp_path, summary)
    assert out_path.exists()


def test_global_cost_view_no_banner_on_clean_read(tmp_path):
    """Negative control: global cost page does NOT show banner on clean reads."""
    today = date.today()
    _write_log(
        tmp_path,
        "alice",
        today,
        [{"cost_usd": 0.10, "status": "ok", "summary": "alice run"}],
    )

    summary = aggregate_global(tmp_path, today=today)
    assert summary.cost_data_degraded is False

    # In-memory template so the absence assertion is meaningful (a flaky empty
    # read-back would also be banner-absent and falsely "pass").
    rendered_html = _render_global_template(summary)

    assert "data may be incomplete" not in rendered_html, (
        "Banner must NOT appear when all reads succeed"
    )
    # Spot-check the page actually rendered (not an empty string).
    assert "Spend this month" in rendered_html


def test_global_cost_view_no_banner_on_empty_vault(tmp_path):
    """Negative control (render layer): a first-run empty vault (no agents) must
    render the global page WITHOUT the degraded banner.

    Pairs the costs-layer empty-vault test with the render layer so the
    home-user-first-run path is pinned end-to-end (no spurious banner).
    """
    summary = aggregate_global(tmp_path, today=date(2026, 6, 15))
    assert summary.cost_data_degraded is False

    rendered_html = _render_global_template(summary)

    assert "data may be incomplete" not in rendered_html, (
        "Banner must NOT appear on an empty first-run dashboard"
    )
    # The page still renders its chrome (not an empty string / crash).
    assert "Spend this month" in rendered_html


def test_agent_cost_view_banner_appears_on_degraded_read(tmp_path, monkeypatch):
    """Integration: per-agent cost page shows 'data may be incomplete' banner on degraded read.

    Verifies the banner appears in the per-agent drilldown (dashboard.html),
    independent of the global page test — the banner must appear on BOTH views
    per the ruling (#498).
    """
    from unittest.mock import MagicMock

    import atomic_agents.logs as logs_mod
    from atomic_agents import LogBackendReadError

    mock_backend = MagicMock()
    mock_backend.query.side_effect = LogBackendReadError("injected failure")
    monkeypatch.setattr(logs_mod, "get_default_log_backend", lambda root: mock_backend)

    today = date.today()
    data = aggregate_agent(tmp_path, "alice", today=today)
    assert data.cost_data_degraded is True  # pre-render sanity check

    # In-memory template assertion (see global test above for why the banner
    # correctness check is NOT routed through atomic_write→read_text).
    rendered_html = _render_agent_template(data)

    # Branch-distinctive assertion: banner text (not the shared empty-render path)
    assert "data may be incomplete" in rendered_html, (
        "Per-agent cost page must contain 'data may be incomplete' banner when degraded"
    )

    # Write-path smoke: file is produced without crashing.
    out_path = render_agent(tmp_path, data)
    assert out_path.exists()


def test_agent_cost_view_no_banner_on_clean_read(tmp_path):
    """Negative control: per-agent cost page does NOT show banner on clean reads."""
    today = date.today()
    _write_log(
        tmp_path,
        "alice",
        today,
        [{"cost_usd": 0.10, "status": "ok", "summary": "alice run"}],
    )

    data = aggregate_agent(tmp_path, "alice", today=today)
    assert data.cost_data_degraded is False

    # In-memory template so the absence assertion is meaningful.
    rendered_html = _render_agent_template(data)

    assert "data may be incomplete" not in rendered_html, (
        "Banner must NOT appear when all reads succeed"
    )
    assert rendered_html.startswith("<!DOCTYPE html>")

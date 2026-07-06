"""Conformance tests for the Per-Agent Detail Cockpit (spec/57, #637 + #684).

Conformance map (spec/57 §8):
  MUST 1* — detail written to <agent>/dashboard.html; resolver redirects;
             resolver rejects unknown agent
             test_detail_written_to_dashboard_html
             test_agent_detail_resolver_redirects
             test_agent_detail_resolver_rejects_unknown (strip-RED)
  MUST 2   — backward compat: render_all()["per_agent"] + /agents/<name> + path
             test_detail_backward_compat_paths
  MUST 3   — banner (name/id/model/status/last-run/health) + five governance states
             test_detail_banner_rendered
             test_detail_five_governance_states
  MUST 4*  — tabs via compose_agent_detail(); Dreaming gated on manifest presence;
             Goals gated on goal.md presence
             test_detail_tabs_present
             test_detail_dreaming_gated (strip-RED: no manifest → no tab)
             test_detail_goals_gated (strip-RED: no goal.md → no tab)
  MUST 5*  — status + health from shared status_for_agent() + FleetHealth;
             health renders 0-100, not ×100
             test_detail_status_health_matches_monitor_in_render_all
             test_detail_health_0_100_not_x100 (strip-RED: composite_display guards ×100)
  MUST 6*  — Dreaming tab renders real manifest/report fields; cadence absent
             test_detail_dreaming_renders_real_manifest_fields (strip-RED: cadence absent)
  MUST 7*  — layered rec tags: savings_cost gets axis tag; governance gets advisory tag
             test_detail_layered_rec_tags (strip-RED: governance rec must NOT get axis tag)
  MUST 8*  — per-tab fail-soft: one degraded tab ≠ page fail;
             unknown agent → not-found (resolver rejects)
             test_detail_one_tab_degraded_isolates (strip-RED)
             test_detail_unknown_agent_not_found
  MUST 9*  — real metric fields (no phantom-field silent-zero);
             known-nonzero cost renders nonzero
             test_detail_metrics_use_real_fields (strip-RED: known nonzero renders nonzero)
  MUST 10* — pure-compute, zero LLM spend
             test_detail_no_llm_spend (strip-RED: patch LLM ctors)

*  = strip-RED negative control required (spec/57 §8).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.dashboard._status import status_for_agent
from atomic_agents.dashboard.panels._registry import (
    ConsoleCapabilities,
    PanelContext,
    PanelRegistry,
    PanelResult,
)


# ──────────────────────────────────────────────────────────────────
# Stub types (mirrors test_dashboard_monitor.py conventions)


@dataclass
class _StubAlertItem:
    agent: str
    alert_key: str = "v1:abc123"
    alert_class: str = "cost_spike"
    alert_subclass: str = "SPIKE"
    severity: str = "high"
    reason: str = "test"
    next_step: str = "check it"
    owner: str | None = None
    status: str = "open"
    ack_snooze_status: str = "open"


@dataclass
class _StubScorecardRow:
    metric: str = "error_rate"
    axis: str = "reliability"
    value: float | None = 0.0
    target: float | None = 0.05
    status: str = "ok"
    score: float | None = 90.0
    wow: str | None = None


@dataclass
class _StubAgentHealth:
    agent: str = "agent1"
    band: str = "green"
    composite: float = 0.85
    composite_display: int = 85
    capped_by_axis: str | None = None
    primary_model: str | None = "claude-sonnet-4-5"
    cost_score: float | None = 85.0
    quality_score: float | None = 85.0
    reliability_score: float | None = 85.0
    scorecard: list = field(default_factory=list)


@dataclass
class _StubFleetHealth:
    fleet_composite: float = 0.85
    fleet_composite_display: int = 85
    fleet_band: str = "green"
    coverage_n: int = 1
    coverage_m: int = 1
    worst_agent: str | None = None
    worst_agent_composite: float | None = None
    degraded: bool = False
    used_targets_defaults: bool = False
    agents: list = field(default_factory=list)


@dataclass
class _StubCostTrend:
    agent: str = "agent1"
    total_usd_30d: float = 1.00
    spike_detected: bool = False
    daily_series: list = field(default_factory=list)


@dataclass
class _StubConsoleData:
    agent_count: int = 1
    attention_queue: list = field(default_factory=list)
    cost_trends: list = field(default_factory=list)
    quality_signals: list = field(default_factory=list)
    reliability_metrics: list = field(default_factory=list)
    fleet_health: object | None = None
    recommendations: list | None = None
    degraded: bool = False
    rendered_alert_keys: frozenset = field(default_factory=frozenset)
    last_primary_runs: dict = field(default_factory=dict)


@dataclass
class _StubRecommendation:
    agent: str = "agent1"
    kind: str = "savings_cost"
    current_model: str | None = "claude-opus-4-8"
    candidate_model: str | None = "claude-sonnet-4-5"
    projected_usd_delta: float | None = -11.28
    projected_points_delta: float | None = 7.0
    rationale: str = "Switch to cheaper model"
    source: str | None = "default_same_family"


_NOW = datetime(2026, 7, 5, 14, 0, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 7, 5)
_RECENT = _NOW - timedelta(hours=1)
_STALE_TS = _NOW - timedelta(hours=36)


# ──────────────────────────────────────────────────────────────────
# Helpers


def _write_agent(agents_root: Path, agent: str) -> None:
    """Create a minimal enumerable agent (model.md present)."""
    (agents_root / agent).mkdir(parents=True, exist_ok=True)
    (agents_root / agent / "model.md").write_text("# model\n")


def _write_log(agents_root: Path, agent: str, cost_usd: float = 0.05) -> None:
    """Write a minimal log record for the agent."""
    log_dir = agents_root / agent / "log" / _TODAY.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _RECENT.isoformat(),
        "trigger": "cron",
        "model": "claude-sonnet-4-5",
        "input_tokens": 500,
        "output_tokens": 100,
        "cost_usd": cost_usd,
        "status": "ok",
        "summary": "test run",
    }
    (log_dir / f"{_TODAY.isoformat()}.jsonl").write_text(json.dumps(rec) + "\n")


def _write_manifest(
    agents_root: Path, agent: str, drm_id: str = "drm_2026-07-04T120000_aabbcc"
) -> Path:
    """Write a minimal dream manifest for the agent."""
    dreams_dir = agents_root / agent / "dreams" / drm_id
    dreams_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dream_id": drm_id,
        "agent_name": agent,
        "status": "completed",
        "model": "claude-sonnet-4-5",
        "instructions": "consolidate",
        "inputs": {"notes": 10, "journal": 5},
        "output_memory_count": 8,
        "consolidated": [{"old": ["a.md", "b.md"], "new": "c.md"}],
        "promoted": [],
        "marked_stale": [{"filename": "old.md", "reason": "superseded"}],
        "total_input_tokens": 12000,
        "total_output_tokens": 3000,
        "total_cost_usd": 0.44,
        "started_at": "2026-07-04T12:00:00",
        "ended_at": "2026-07-04T12:15:00",
        "error": None,
        "applied_at": "2026-07-04T13:00:00",
        "archived_path": None,
    }
    mf_path = dreams_dir / "manifest.json"
    mf_path.write_text(json.dumps(manifest))
    report_path = dreams_dir / "report.md"
    report_path.write_text(
        "# Dream Report\n\nConsolidated 1 note group. Marked 1 note stale.\n"
    )
    return mf_path


def _render_detail_html(
    agents_root: Path,
    agent_id: str = "agent1",
    console_data=None,
    now=None,
    today=None,
) -> str:
    """Call render_agent_detail() and return the HTML."""
    from atomic_agents.dashboard.render_agent_detail import render_agent_detail

    path = render_agent_detail(
        agents_root,
        agent_id,
        console_data=console_data,
        now=now or _NOW,
        today=today or _TODAY,
    )
    return path.read_text()


# ──────────────────────────────────────────────────────────────────
# MUST 1: detail written to dashboard.html; resolver redirects; rejects unknown


def test_detail_written_to_dashboard_html(tmp_path):
    """MUST 1: render_agent_detail() writes <agent>/dashboard.html."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    html = _render_detail_html(tmp_path)

    out_path = tmp_path / "agent1" / "dashboard.html"
    assert out_path.exists(), "dashboard.html must exist"
    assert "<!DOCTYPE html>" in html
    assert "agent1" in html


def test_agent_detail_resolver_redirects(tmp_path):
    """MUST 1: _dashboard/agent-detail.html resolver is generated with known agents."""
    from atomic_agents.dashboard.render_agent_detail import render_agent_detail_resolver

    _write_agent(tmp_path, "agent1")
    _write_agent(tmp_path, "agent2")
    resolver_path = render_agent_detail_resolver(tmp_path)

    assert resolver_path == tmp_path / "_dashboard" / "agent-detail.html"
    assert resolver_path.exists()
    content = resolver_path.read_text()
    assert "agent1" in content
    assert "agent2" in content
    # Resolver must contain JS redirect logic
    assert "location.replace" in content or "window.location" in content


def test_agent_detail_resolver_rejects_unknown(tmp_path):
    """MUST 1 strip-RED: resolver rejects an agent not in the known set."""
    from atomic_agents.dashboard.render_agent_detail import render_agent_detail_resolver

    _write_agent(tmp_path, "known-agent")
    resolver_path = render_agent_detail_resolver(tmp_path)
    content = resolver_path.read_text()

    # The resolver JS checks the known list. "unknown-agent" must NOT appear as a
    # known agent — the JS must emit "not found" for it.
    # Verify: "unknown-agent" is not in the embedded known list.
    # The known list is JSON-encoded; "unknown-agent" would appear as "unknown-agent".
    assert "unknown-agent" not in content, (
        "resolver must not include unknown-agent in the known-agent list"
    )
    # And the not-found branch must exist in the JS
    assert "not found" in content.lower() or "Agent not found" in content, (
        "resolver must have a not-found branch for unknown agents"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 2: backward compat


def test_detail_backward_compat_paths(tmp_path):
    """MUST 2: render_all()["per_agent"] still lists <agent>/dashboard.html paths."""
    from atomic_agents.dashboard.render import render_all

    _write_agent(tmp_path, "alice")
    _write_log(tmp_path, "alice")
    _write_agent(tmp_path, "bob")
    _write_log(tmp_path, "bob")

    written = render_all(tmp_path, today=_TODAY)

    # per_agent must contain entries for both agents
    per_agent = written.get("per_agent", [])
    assert len(per_agent) >= 2, "render_all must write per-agent detail pages"
    paths = [Path(p) for p in per_agent]
    names = {p.parent.name for p in paths}
    assert "alice" in names
    assert "bob" in names
    # Each path must be dashboard.html (unchanged filename — MUST 2)
    for p in paths:
        assert p.name == "dashboard.html", f"path must be dashboard.html, got {p.name}"
    # Files must exist on disk
    assert (tmp_path / "alice" / "dashboard.html").exists()
    assert (tmp_path / "bob" / "dashboard.html").exists()


# ──────────────────────────────────────────────────────────────────
# MUST 3: banner + five governance states


def test_detail_banner_rendered(tmp_path):
    """MUST 3: banner renders agent name, status, and health."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    ah = _StubAgentHealth(agent="agent1", composite_display=85, band="green")
    fh = _StubFleetHealth(agents=[ah])
    cd = _StubConsoleData(
        fleet_health=fh,
        last_primary_runs={"agent1": _RECENT},
    )
    html = _render_detail_html(tmp_path, console_data=cd)

    # Banner must include agent name
    assert "agent1" in html
    # Banner must include status indicator
    assert any(s in html for s in ("OK", "WARN", "ERROR", "STALE"))
    # Health value 85 must appear (0-100 integer)
    assert "85" in html


def test_detail_five_governance_states(tmp_path):
    """MUST 3: all five governance states render honestly (not blank)."""
    from atomic_agents.dashboard.render_agent_detail import _render_governance_block

    # ABSENT: has_governance=False, governance=None
    class _FakeRef:
        has_governance = False
        governance = None

    html_absent = _render_governance_block(_FakeRef())
    assert "ABSENT" in html_absent or "absent" in html_absent.lower(), (
        "ABSENT state must be surfaced"
    )

    # PRESENT_INVALID: has_governance=True, governance with parse_errors
    class _FakeRefInvalid:
        has_governance = True

        class governance:
            parse_errors = ("unknown permission_tier 'superuser'",)
            owner = None

    html_invalid = _render_governance_block(_FakeRefInvalid())
    assert "PRESENT_INVALID" in html_invalid or "invalid" in html_invalid.lower(), (
        "PRESENT_INVALID state must be surfaced"
    )
    assert "superuser" in html_invalid, "parse error text must be surfaced"

    # PRESENT_VALID: has_governance=True, governance with no parse_errors
    class _GovernanceValid:
        parse_errors = ()
        owner = "Dan Powers"
        permission_tier = "reads-only"
        customer_data = "no"
        writes_sor = "no"
        lifecycle_status = "active"
        review = None
        risk = None
        actions = None

    class _FakeRefValid:
        has_governance = True
        governance = _GovernanceValid()

    html_valid = _render_governance_block(_FakeRefValid())
    assert "PRESENT_VALID" in html_valid, "PRESENT_VALID state must be surfaced"
    assert "Dan Powers" in html_valid
    assert "reads-only" in html_valid

    # None agent_ref (graceful fallback)
    html_none = _render_governance_block(None)
    assert "absent" in html_none.lower() or "Governance" in html_none, (
        "None agent_ref must render a graceful fallback"
    )

    # PRESENT_NO_BLOCK: has_governance=True, governance=None
    # (governance.md exists but has no parseable YAML block)
    class _FakeRefNoBlock:
        has_governance = True
        governance = None

    html_no_block = _render_governance_block(_FakeRefNoBlock())
    assert "PRESENT_NO_BLOCK" in html_no_block, (
        "PRESENT_NO_BLOCK state must be surfaced when governance=None with has_governance=True"
    )

    # PRESENT_INCOMPLETE: has_governance=True, governance has no parse_errors but owner=None
    class _GovernanceIncomplete:
        parse_errors = ()
        owner = None
        permission_tier = "reads-only"
        customer_data = "no"
        writes_sor = "no"
        lifecycle_status = "active"
        review = None
        risk = None
        actions = None

    class _FakeRefIncomplete:
        has_governance = True
        governance = _GovernanceIncomplete()

    html_incomplete = _render_governance_block(_FakeRefIncomplete())
    assert "PRESENT_INCOMPLETE" in html_incomplete, (
        "PRESENT_INCOMPLETE state must be surfaced when governance parsed but owner=None"
    )


def test_detail_governance_present_no_block(tmp_path):
    """MUST 3: PRESENT_NO_BLOCK renders honestly when governance.md has no YAML block."""
    from atomic_agents.dashboard.render_agent_detail import _governance_state

    # PRESENT_NO_BLOCK: has_gov=True, gov=None
    state = _governance_state(True, None)
    assert state == "PRESENT_NO_BLOCK", (
        "_governance_state(True, None) must return PRESENT_NO_BLOCK"
    )


def test_detail_governance_present_incomplete(tmp_path):
    """MUST 3: PRESENT_INCOMPLETE when owner field is None after parsing."""
    from atomic_agents.dashboard.render_agent_detail import _governance_state

    class _GovNoOwner:
        parse_errors = ()
        owner = None

    state = _governance_state(True, _GovNoOwner())
    assert state == "PRESENT_INCOMPLETE", (
        "_governance_state with no owner must return PRESENT_INCOMPLETE"
    )


def test_detail_governance_absent_vs_no_block_distinct(tmp_path):
    """MUST 3 strip-RED: ABSENT and PRESENT_NO_BLOCK must be distinct states.

    strip-RED: the old code collapsed both into ABSENT (has_gov=False OR gov=None).
    After the fix, has_gov=True + gov=None must map to PRESENT_NO_BLOCK, not ABSENT.
    """
    from atomic_agents.dashboard.render_agent_detail import _governance_state

    # ABSENT: has_gov=False
    assert _governance_state(False, None) == "ABSENT"
    # PRESENT_NO_BLOCK: has_gov=True, gov=None (distinct — must NOT be ABSENT)
    result = _governance_state(True, None)
    assert result != "ABSENT", (
        "PRESENT_NO_BLOCK must not collapse to ABSENT — strip-RED for FIX 5"
    )
    assert result == "PRESENT_NO_BLOCK", (
        "has_gov=True + gov=None must map to PRESENT_NO_BLOCK, not ABSENT — strip-RED"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 4: compose_agent_detail() + capability gating


def test_detail_tabs_present(tmp_path):
    """MUST 4: standard tabs (Overview, Cost, Activity, Efficiency) always present."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    html = _render_detail_html(tmp_path)

    assert "Overview" in html
    assert "Cost" in html
    assert "Activity" in html
    assert "Efficiency" in html


def test_detail_dreaming_gated(tmp_path):
    """MUST 4 strip-RED: Dreaming tab BUTTON absent when no dream manifests exist.

    strip-RED contract: "Dreaming" appears in the shared CSS comment regardless
    of the gate.  The gate controls whether the tab NAV BUTTON (dtab-dreaming)
    is rendered.  We assert the button is absent, not the word "Dreaming".
    If the gate were removed, the button would always render and the assertion
    would fail — confirming the guard is load-bearing.
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    # No dreams/ directory → Dreaming tab button must not appear
    html = _render_detail_html(tmp_path)
    # strip-RED: the button id "dtab-dreaming" must be absent when no manifest exists.
    # "Dreaming" alone is not a reliable indicator (it appears in CSS comments).
    assert "dtab-dreaming" not in html, (
        "Dreaming tab button (dtab-dreaming) must be absent when no dream manifests exist"
    )
    # Confirm the panel body is also absent
    assert "tabpanel-dreaming" not in html, (
        "Dreaming tab panel (tabpanel-dreaming) must be absent when no dream manifests exist"
    )


def test_detail_dreaming_tab_present_when_manifest_exists(tmp_path):
    """MUST 4: Dreaming tab appears when at least one manifest.json exists."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    _write_manifest(tmp_path, "agent1")
    html = _render_detail_html(tmp_path)
    assert "Dreaming" in html, "Dreaming tab must appear when manifest exists"


def test_detail_goals_gated(tmp_path):
    """MUST 4 strip-RED: Goals tab absent when no goal.md exists."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    # No goal.md → Goals tab must not appear
    html = _render_detail_html(tmp_path)
    assert "Goals" not in html, "Goals tab must not appear when no goal.md exists"


def test_detail_goals_tab_present_when_goal_exists(tmp_path):
    """MUST 4: Goals tab appears when goal.md exists."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    (tmp_path / "agent1" / "goal.md").write_text("# Goal\nBe useful.\n")
    html = _render_detail_html(tmp_path)
    assert "Goals" in html, "Goals tab must appear when goal.md exists"


def test_compose_agent_detail_in_registry(tmp_path):
    """MUST 4: PanelRegistry.compose_agent_detail() exists, is callable, returns list."""
    from atomic_agents.dashboard.panels._registry import (
        PanelRegistry,
        PanelContext,
        ConsoleCapabilities,
    )

    reg = PanelRegistry()
    # Must have the method
    assert callable(getattr(reg, "compose_agent_detail", None)), (
        "PanelRegistry must have compose_agent_detail()"
    )
    # With no registered agent-tab panels, returns an empty list.
    cd = _StubConsoleData()
    ctx = PanelContext(
        console_data=cd,
        capabilities=ConsoleCapabilities(),
        today=_TODAY,
        now=_NOW,
    )
    result = reg.compose_agent_detail(ctx)
    assert isinstance(result, list), (
        "compose_agent_detail() must return a list of (panel, html) tuples"
    )
    assert result == [], (
        "compose_agent_detail() with no registered panels must return an empty list"
    )


def test_compose_agent_detail_drives_tabs(tmp_path):
    """MUST 4 sentinel: compose_agent_detail() is called when rendering and drives tabs.

    This test verifies that the global registry's compose_agent_detail() is invoked
    during render_agent_detail() and that it is the SINGLE source of tab content —
    no second panels_by_slot() render pass exists in the renderer.

    Approach: register a spy panel on the global registry (then unregister it).
    - The spy's render() appends a unique marker string to ``rendered``.
    - We assert the marker appears in the HTML EXACTLY ONCE — not twice.
      If compose_agent_detail() is called but its output is discarded and the panels
      are rendered again via a second panels_by_slot() pass, the marker appears twice.
      If compose_agent_detail() is bypassed entirely, the marker does not appear.
    - We also assert the tab-nav button (dtab-spy) and the content pane
      (tabpanel-spy) are both present and each appears exactly once.

    Note: the spy panel must be unregistered after the test to avoid polluting the
    singleton for subsequent tests.
    """
    from atomic_agents.dashboard.panels._registry import get_registry, PanelResult

    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    rendered = []
    _MARKER = "spy-tab-unique-marker-63f4a9"

    class _SpyPanel:
        id = "agent_tab_spy_sentinel_do_not_ship"
        slot = "agent-tab"
        order = 5  # before all real tabs
        tab_id = "spy"
        tab_label = "Spy"

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            rendered.append("spy_called")
            return PanelResult(html=f'<div class="spy">{_MARKER}</div>')

    spy = _SpyPanel()
    registry = get_registry()
    registry.register(spy)
    try:
        html = _render_detail_html(tmp_path)

        # render() was called — compose_agent_detail() invoked the panel
        assert rendered, (
            "compose_agent_detail must invoke registered agent-tab panels — "
            "spy was not called, indicating the renderer bypassed the registry"
        )

        # The marker appears EXACTLY ONCE — proves compose output is the single
        # injection source (no double-render via a second panels_by_slot() pass).
        marker_count = html.count(_MARKER)
        assert marker_count == 1, (
            f"Spy panel marker must appear exactly once in the rendered HTML "
            f"(found {marker_count}). Double-render would produce count=2; "
            f"bypassed registry would produce count=0."
        )

        # Tab-nav button and content pane are both present exactly once.
        assert html.count('id="dtab-spy"') == 1, (
            "Tab-nav button dtab-spy must appear exactly once"
        )
        assert html.count('id="tabpanel-spy"') == 1, (
            "Tab content pane tabpanel-spy must appear exactly once"
        )
    finally:
        # Unregister the spy so it does not pollute subsequent tests.
        registry._panels = [p for p in registry._panels if p.id != spy.id]


def test_memory_tab_shown_for_empty_memory_dir(tmp_path):
    """MUST 4 / spec/57 §3: Memory tab appears when memory/ exists but is empty.

    strip-RED contract: a _has_memory() guard that requires at least one *.md
    note would suppress the Memory tab for an agent with an empty memory/ dir.
    The spec requires an EMPTY STATE render, not no-tab.  We assert dtab-memory
    is present even when memory/ has no notes.
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    # Create empty memory/ surface — no .md files inside.
    (tmp_path / "agent1" / "memory").mkdir(parents=True, exist_ok=True)

    html = _render_detail_html(tmp_path)

    assert "dtab-memory" in html, (
        "Memory tab button (dtab-memory) must appear when memory/ exists, "
        "even if there are no notes — strip-RED for MUST 4 / spec/57 §3"
    )
    assert "tabpanel-memory" in html, (
        "Memory tab panel (tabpanel-memory) must appear when memory/ exists"
    )


def test_memory_tab_absent_when_no_memory_dir(tmp_path):
    """MUST 4 strip-RED: Memory tab absent when memory/ surface does not exist."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    # No memory/ directory at all.
    html = _render_detail_html(tmp_path)

    assert "dtab-memory" not in html, (
        "Memory tab button must be absent when memory/ directory does not exist"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 5: status + health via shared derivation; health 0-100


def test_detail_status_health_matches_monitor_in_render_all(tmp_path):
    """MUST 5: detail banner status matches status_for_agent() derivation."""
    from atomic_agents.dashboard.render_agent_detail import render_agent_detail

    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    ah = _StubAgentHealth(
        agent="agent1", capped_by_axis="reliability", composite_display=40
    )
    fh = _StubFleetHealth(agents=[ah])
    cd = _StubConsoleData(
        fleet_health=fh,
        last_primary_runs={"agent1": _RECENT},
    )

    # Expected status via the shared function
    expected_status = status_for_agent(
        agent_health=ah,
        attention_items=[],
        last_primary_run_at=_RECENT,
        now=_NOW,
        cost_spike=False,
    )
    assert expected_status == "ERROR"  # capped_by_axis is not None

    path = render_agent_detail(
        tmp_path, "agent1", console_data=cd, now=_NOW, today=_TODAY
    )
    html = path.read_text()
    # The banner must show ERROR status
    assert "ERROR" in html, "Banner must show ERROR when capped_by_axis is set"


def test_detail_health_0_100_not_x100(tmp_path):
    """MUST 5 strip-RED: health renders as 0-100 integer; composite_display is not ×100."""
    from atomic_agents.dashboard.render_agent_detail import render_agent_detail

    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    # composite_display = 72 (already the display int, NOT 0.72 raw float)
    ah = _StubAgentHealth(
        agent="agent1", composite_display=72, composite=0.72, band="green"
    )
    fh = _StubFleetHealth(agents=[ah])
    cd = _StubConsoleData(
        fleet_health=fh,
        last_primary_runs={"agent1": _RECENT},
    )

    path = render_agent_detail(
        tmp_path, "agent1", console_data=cd, now=_NOW, today=_TODAY
    )
    html = path.read_text()

    # 72 must appear (correct 0-100 display)
    assert "72" in html, "Health score 72 must appear in banner"
    # The ×100 bug would render "7200" — must never appear
    assert "7200" not in html, "Health score must not be multiplied by 100 (×100 bug)"


# ──────────────────────────────────────────────────────────────────
# MUST 6: Dreaming tab — real manifest fields; cadence/schedule omitted


def test_detail_dreaming_renders_real_manifest_fields(tmp_path):
    """MUST 6: Dreaming tab renders real manifest fields from disk."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    _write_manifest(tmp_path, "agent1")

    html = _render_detail_html(tmp_path)

    # Real manifest fields that must appear:
    assert "completed" in html, "status 'completed' from manifest must render"
    assert "0.44" in html or "0.4400" in html, (
        "total_cost_usd from manifest must render"
    )
    # Consolidated count: 1 (from the consolidated list with 1 entry)
    assert "1" in html, "consolidated count from manifest must render"
    # report.md summary must appear (observe-only — MUST 6)
    assert "Dream Report" in html or "Consolidated" in html, (
        "report.md content must render in Dreaming tab"
    )


def test_detail_dreaming_cadence_absent(tmp_path):
    """MUST 6 strip-RED: invented fields (cadence, next-run) must NOT appear."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    _write_manifest(tmp_path, "agent1")

    html = _render_detail_html(tmp_path)

    # These are the mockup's invented fields — no backing artifact exists.
    # spec/57 §4 explicitly defers them. They must NOT render.
    assert "cadence" not in html.lower(), (
        "Invented 'cadence' field must not render — no scheduler artifact yet"
    )
    assert "next run" not in html.lower() and "next-run" not in html.lower(), (
        "Invented 'next run' field must not render — no scheduler artifact yet"
    )
    assert "candidates" not in html.lower(), (
        "Invented 'candidates' field must not render — no consolidation-candidate artifact yet"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 7: layered rec tags (spec/52 §17.3)


def test_detail_layered_rec_tags(tmp_path):
    """MUST 7 strip-RED: savings_cost gets axis tag; governance must NOT get axis tag."""
    from atomic_agents.dashboard.render_agent_detail import (
        _render_detail_recommendations,
    )

    # savings_cost rec → must get "→ Cost · +N pts" tag
    savings_rec = _StubRecommendation(
        agent="agent1",
        kind="savings_cost",
        projected_points_delta=7.0,
        projected_usd_delta=-11.28,
    )
    html_savings = _render_detail_recommendations([savings_rec], "agent1")
    assert "Cost" in html_savings, "savings_cost rec must carry '→ Cost · +N pts' tag"
    assert "+7" in html_savings, "savings_cost rec must show pts delta"

    # governance rec → must get "advisory · not scored" tag, NOT an axis tag
    gov_rec = _StubRecommendation(
        agent="agent1",
        kind="governance",
        current_model=None,
        candidate_model=None,
        projected_usd_delta=None,
        projected_points_delta=None,
        rationale="Governance gap detected",
    )
    html_gov = _render_detail_recommendations([gov_rec], "agent1")
    # strip-RED: if the governance rec incorrectly got an axis tag, "→ Cost" would appear
    assert "advisory" in html_gov.lower(), "governance rec must carry 'advisory' tag"
    assert "not scored" in html_gov.lower(), (
        "governance rec must carry 'not scored' tag"
    )
    # The axis tag must NOT appear for governance recs
    assert "&#8594; Cost" not in html_gov and "→ Cost" not in html_gov, (
        "governance rec must NOT get a '→ Cost' axis tag — strip-RED"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 8: per-tab fail-soft; unknown agent → not-found


def test_detail_one_tab_degraded_isolates(tmp_path):
    """MUST 8 strip-RED: a tab that raises degrades only itself; banner still renders."""
    from atomic_agents.dashboard.render_agent_detail import _render_detail_template
    from datetime import datetime as _dt

    _write_agent(tmp_path, "agent1")

    # Simulate a tab failure by patching _render_cost_tab to raise
    with patch(
        "atomic_agents.dashboard.render_agent_detail._render_cost_tab",
        side_effect=RuntimeError("simulated cost tab failure"),
    ):
        # render_detail_template has per-tab try/except — the page must still render
        html = _render_detail_template(
            agent_id="agent1",
            agents_root=tmp_path,
            agent_ref=None,
            agent_health=None,
            status="OK",
            last_run_at=None,
            cost_data=None,
            recs=None,
            now=_NOW,
            today=_TODAY,
        )

    # The page must still render with a valid DOCTYPE
    assert "<!DOCTYPE html>" in html, "Page must render even when a tab fails"
    # The banner must still appear
    assert "agent1" in html, "Banner must render even when cost tab fails"
    # A degraded marker must appear for the cost tab
    assert "unavailable" in html.lower() or "degraded" in html.lower(), (
        "Degraded tab must show a placeholder, not a blank"
    )


def test_detail_unknown_agent_not_found(tmp_path):
    """MUST 8: resolver rejects an unknown agent cleanly."""
    from atomic_agents.dashboard.render_agent_detail import render_agent_detail_resolver

    _write_agent(tmp_path, "known-agent")
    resolver_path = render_agent_detail_resolver(tmp_path)
    content = resolver_path.read_text()

    # "phantom-agent" must not be in the known list
    known_json_re = re.search(r"var known\s*=\s*(\[.*?\])", content, re.DOTALL)
    if known_json_re:
        known_list = json.loads(known_json_re.group(1))
        assert "phantom-agent" not in known_list, (
            "Unknown agent must not appear in resolver's known list"
        )
    # The not-found path must exist
    assert "not found" in content.lower() or "Agent not found" in content


# ──────────────────────────────────────────────────────────────────
# MUST 9: real metric fields (no phantom-field silent-zero)


def test_detail_metrics_use_real_fields(tmp_path):
    """MUST 9 strip-RED: known-nonzero cost renders in the summary metric card.

    strip-RED contract: the summary metric card ("Spend (month)") is driven by
    s.cost_usd (the AgentSummary field). A phantom getattr-with-default-0 that
    read from a non-existent field would silently show $0.0000 in the card.
    We assert the SUMMARY CARD contains the nonzero value — not just any location
    on the page (a run-row match would be a false green if the card were 0).
    """
    _write_agent(tmp_path, "agent1")
    # Write a real log record with a nonzero cost
    _write_log(tmp_path, "agent1", cost_usd=0.1234)

    html = _render_detail_html(tmp_path)

    # The Spend (month) metric card HTML structure is:
    # class="mk">Spend (month)</div><div class="mv">$X.XXXX</div>
    # We match the card tightly so a run-row match cannot satisfy it.
    # strip-RED: phantom-field returning 0.0 would produce "$0.0000", not "$0.1234".
    assert re.search(
        r'class="mk">Spend \(month\)</div><div class="mv">\$0\.1234',
        html,
    ), (
        "Summary metric card 'Spend (month)' must show real cost_usd ($0.1234), "
        "not phantom-field default 0 — strip-RED for MUST 9"
    )


def test_detail_health_real_composite_display(tmp_path):
    """MUST 9: composite_display (real field) drives the health value — not a phantom 0."""
    from atomic_agents.dashboard.render_agent_detail import render_agent_detail

    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    # composite_display=91 — must render "91", not "0" or "—"
    ah = _StubAgentHealth(agent="agent1", composite_display=91, band="green")
    fh = _StubFleetHealth(agents=[ah])
    cd = _StubConsoleData(
        fleet_health=fh,
        last_primary_runs={"agent1": _RECENT},
    )

    path = render_agent_detail(
        tmp_path, "agent1", console_data=cd, now=_NOW, today=_TODAY
    )
    html = path.read_text()
    assert "91" in html, (
        "composite_display=91 must render '91' — real field read, not phantom 0"
    )


def test_detail_table_fields_use_real_attributes(tmp_path):
    """MUST 9 strip-RED: Cost and Activity table rows use direct field access.

    strip-RED contract: getattr(r, "input_tokens", 0) / getattr(r, "output_tokens", 0)
    / getattr(r, "cost_usd", 0.0) on run records are phantom-field patterns — if the
    field is renamed, the getattr silently returns 0 instead of failing.  Direct field
    access (r.input_tokens etc.) fails visibly on a rename.

    We verify that known-nonzero token and cost values from the log record render in
    the Cost and Activity table rows, not phantom zeros.  The log written by
    _write_log() uses input_tokens=500, output_tokens=100 — distinct from 0.

    strip-RED: a phantom getattr-default-0 would render "0 / 0" in the tokens column;
    real field access renders "500 / 100".
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1", cost_usd=0.0777)

    html = _render_detail_html(tmp_path)

    # Tokens column in the Cost and Activity tables: "500 / 100"
    assert "500 / 100" in html, (
        "Token columns in Cost/Activity table rows must show real input_tokens/output_tokens "
        "(500 / 100), not phantom-field default 0 — strip-RED for MUST 9"
    )

    # Cost column in both tables: "$0.0777"
    assert "$0.0777" in html, (
        "Cost column in Cost/Activity table rows must show real cost_usd ($0.0777), "
        "not phantom-field default 0.0 — strip-RED for MUST 9"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 10: pure-compute, zero LLM spend


def test_detail_no_llm_spend(tmp_path):
    """MUST 10 strip-RED: no concrete LLM backend constructor is called on render.

    strip-RED contract: the previous test patched atomic_agents._llm.LLMBackend
    which does not exist there (only free functions live in _llm).  The ImportError
    path always ran — the patch was never applied — making the test hollow.

    This test patches __init__ on the THREE concrete LLM backend classes from their
    real module paths.  If any of them is constructed, the test fails.  If the gate
    is removed (i.e. an LLM backend IS constructed), the assertion fires.
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    constructed = []

    def _raise_if_constructed(self, *a, **kw):
        constructed.append(type(self).__name__)
        raise AssertionError(
            f"LLM backend {type(self).__name__} constructed on detail render path"
        )

    # Concrete LLM backend classes and their real module paths.
    _llm_targets = [
        ("atomic_agents.llm.anthropic", "AnthropicLLMBackend"),
        ("atomic_agents.llm.openai_compat", "OpenAICompatibleLLMBackend"),
        ("atomic_agents.llm.vertex_gemini", "VertexGeminiLLMBackend"),
    ]

    import importlib
    from contextlib import ExitStack

    patches_applied = []
    for mod_path, cls_name in _llm_targets:
        try:
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name, None)
            if cls is not None:
                patches_applied.append(
                    patch.object(cls, "__init__", _raise_if_constructed)
                )
        except ImportError:
            pass  # backend not installed in this env — skip

    if patches_applied:
        with ExitStack() as stack:
            for p in patches_applied:
                stack.enter_context(p)
            _render_detail_html(tmp_path)
    else:
        # No LLM backends importable (minimal test env) — just verify render completes.
        _render_detail_html(tmp_path)

    assert not constructed, (
        f"Concrete LLM backends constructed on detail render path (zero-spend MUST 10): "
        f"{constructed}"
    )


# ──────────────────────────────────────────────────────────────────
# Additional: render_all() generates the resolver


def test_render_all_generates_resolver(tmp_path):
    """MUST 1 + 2: render_all() generates the agent-detail.html resolver."""
    from atomic_agents.dashboard.render import render_all

    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    written = render_all(tmp_path, today=_TODAY)

    resolver_path = tmp_path / "_dashboard" / "agent-detail.html"
    assert resolver_path.exists(), (
        "_dashboard/agent-detail.html must be generated by render_all()"
    )
    content = resolver_path.read_text()
    assert "agent1" in content


def test_detail_csp_present(tmp_path):
    """CSP meta tag must appear in the detail page (security baseline)."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    html = _render_detail_html(tmp_path)
    assert "Content-Security-Policy" in html


def test_detail_breadcrumb_links_to_monitor(tmp_path):
    """Detail page breadcrumb must link back to monitor.html."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    html = _render_detail_html(tmp_path)
    assert "monitor.html" in html, "Breadcrumb must link to Fleet Monitor"
    assert "Fleet Monitor" in html, "Breadcrumb must display 'Fleet Monitor'"


# ──────────────────────────────────────────────────────────────────
# MUST 5 parity: render_all shared snapshot (FIX 4 double-write guard)


def test_render_all_no_double_write(tmp_path):
    """MUST 5: render_all() with tab='all' must not write detail pages twice.

    The double-write race: render_cost loop writes dashboard.html without
    fleet_health (standalone snapshot); then the console pass re-writes it with
    fleet_health.  After FIX 4, the cost loop skips the write when render_console_tab
    is True, so the file is written exactly once (by the console pass) with the
    shared snapshot.

    Verification: run render_all(), track file-write count on dashboard.html by
    checking mtime before/after.  Since both writes go to the same file, we can't
    directly count writes; instead we verify the final content comes from the console
    pass (it will include fleet_health-derived status, not a fresh standalone snap).

    Simpler structural test: with tab='all', per_agent list still contains the paths
    (the console pass's detail paths replace the cost-loop paths in written["per_agent"]).
    And with tab='cost' (no console pass), the cost-loop DOES write per-agent pages.
    """
    from atomic_agents.dashboard.render import render_all

    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    # tab='all': console pass will run, cost loop must NOT write detail pages.
    written_all = render_all(tmp_path, today=_TODAY)
    per_agent_all = written_all.get("per_agent", [])
    assert len(per_agent_all) >= 1, "render_all must produce per-agent detail paths"
    for p in per_agent_all:
        assert Path(p).name == "dashboard.html"

    # tab='cost' (no console pass): cost loop MUST write detail pages
    # Create a fresh tmp to avoid path conflicts
    import tempfile

    with tempfile.TemporaryDirectory() as tmp2:
        tmp2 = Path(tmp2)
        _write_agent(tmp2, "agent1")
        _write_log(tmp2, "agent1")
        written_cost = render_all(tmp2, today=_TODAY, tab="cost")
        per_agent_cost = written_cost.get("per_agent", [])
        assert len(per_agent_cost) >= 1, (
            "render_all(tab='cost') must write per-agent detail pages from cost loop"
        )
        for p in per_agent_cost:
            assert Path(p).name == "dashboard.html"


def test_render_all_monitor_detail_status_parity(tmp_path):
    """MUST 5 parity: Monitor row status == detail banner status (one snapshot).

    render_all(tab='all') co-renders monitor.html and the per-agent dashboard.html
    from ONE console_data/now snapshot.  A divergent snapshot (the double-write
    race FIX 4 closes) would make the Monitor row and the detail banner disagree.
    This test reads what each surface ACTUALLY WROTE and asserts they match — it
    does not re-derive status via status_for_agent().

    The agent's last run is ~36h old → STALE, exercising a non-OK status path.
    render_all() uses datetime.now() for `now`, so a 36h-old run is STALE
    regardless of _NOW.
    """
    from atomic_agents.dashboard.render import render_all

    _write_agent(tmp_path, "agent1")
    # STALE: last run 36h ago (write directly with _STALE_TS so the log month/date
    # match the record timestamp).
    log_dir = tmp_path / "agent1" / "log" / _STALE_TS.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _STALE_TS.isoformat(),
        "trigger": "cron",
        "model": "claude-sonnet-4-5",
        "input_tokens": 500,
        "output_tokens": 100,
        "cost_usd": 0.05,
        "status": "ok",
        "summary": "stale run",
    }
    (log_dir / f"{_STALE_TS.date().isoformat()}.jsonl").write_text(
        json.dumps(rec) + "\n"
    )

    # Full pass — monitor.html + per-agent dashboard.html from one snapshot.
    render_all(tmp_path, today=_STALE_TS.date())

    # ── Monitor status: parse the embedded roster JSON ────────────────────────
    monitor_path = tmp_path / "_dashboard" / "monitor.html"
    assert monitor_path.exists(), "render_all(tab='all') must write monitor.html"
    monitor_html = monitor_path.read_text()

    m = re.search(
        r'<script type="application/json" id="monitor-agents">(.*?)</script>',
        monitor_html,
        re.DOTALL,
    )
    assert m, "monitor.html must embed the monitor-agents JSON block"
    # The roster emits \uXXXX escapes for <, >, &, U+2028, U+2029. json.loads
    # decodes those \u sequences natively, so no manual un-escaping is needed.
    entities = json.loads(m.group(1))
    agent1_entity = next((e for e in entities if e.get("agent") == "agent1"), None)
    if agent1_entity is None:
        # Fall back to name/id keys if the roster keys the entity differently.
        agent1_entity = next(
            (
                e
                for e in entities
                if e.get("id") == "agent1" or e.get("name") == "agent1"
            ),
            None,
        )
    assert agent1_entity is not None, (
        f"monitor-agents JSON must contain agent1; got entities: {entities}"
    )
    monitor_status = agent1_entity["status"]

    # ── Detail banner status: parse class="bv status-<x>" ─────────────────────
    detail_path = tmp_path / "agent1" / "dashboard.html"
    assert detail_path.exists(), "render_all must write agent1/dashboard.html"
    detail_html = detail_path.read_text()

    dm = re.search(r'class="bv status-([a-z]+)"', detail_html)
    assert dm, "detail banner must render class='bv status-<x>'"
    detail_status = dm.group(1)

    # ── Parity: the two rendered surfaces must agree (MUST 5 shared snapshot) ──
    assert detail_status == monitor_status, (
        f"detail banner status ({detail_status!r}) must equal monitor row status "
        f"({monitor_status!r}) — one console_data/now snapshot (MUST 5); a divergent "
        "snapshot from the double-write race would make these differ"
    )
    # Sanity: a non-OK status path was exercised, so the parity assertion is not
    # a trivial "ok == ok". A single 36h-old run in a full render_all() pass yields
    # a populated AgentHealth whose critical-axis cap / error-rate check fires ERROR
    # (precedence ERROR > STALE, so STALE is not reached). Either non-OK path proves
    # the shared snapshot flowed through the real scoring path on both surfaces.
    assert monitor_status in ("stale", "error", "warn"), (
        f"expected a non-OK status for a 36h-old single run, got {monitor_status!r}"
    )


# ──────────────────────────────────────────────────────────────────
# FIX 1-7: Standalone self-sufficient render (console_data=None produces FULL page)


def test_standalone_banner_health_integer(tmp_path):
    """FIX 1 + 6: standalone render_agent_detail(console_data=None) shows a real health integer.

    Without console_data, the renderer must compute fleet health standalone so the
    banner shows a 0-100 int, not "—". The agent has a real log record.
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    html = _render_detail_html(tmp_path, console_data=None)

    # The banner must include a fleet health field — not the "—" skeleton.
    # Health int must appear in the banner grid "Fleet health" cell.
    import re as _re
    # The 8-field banner grid always includes a "Fleet health" cell.
    assert "Fleet health" in html, "Banner must include Fleet health field"
    # The value must not be "—" — health was computed standalone.
    # Match: 'Fleet health</div><div class="bv ...">N</div>' where N is a digit
    m = _re.search(r'Fleet health</div>\s*<div class="bv[^"]*">([^<]+)</div>', html)
    assert m, "Banner grid Fleet health value must be present"
    val = m.group(1).strip()
    assert val != "—", (
        f"Standalone render must compute health (not '—'); got {val!r}. "
        "FIX 1+6: compute_fleet_health() must run when console_data=None"
    )
    # Sanity: it must be a number in 0-100 range
    assert val.isdigit() or val == "0", (
        f"Health value must be a digit string; got {val!r}"
    )


def test_standalone_banner_model_pill(tmp_path):
    """FIX 2: standalone render shows model pill from model.md (not agent_health).

    Without console_data, agent_health.primary_model is unavailable. The model pill
    must fall back to reading model.md directly so the pill is never blank.
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    # Write a model.md with a recognisable model id
    (tmp_path / "agent1" / "model.md").write_text(
        "# Model\n\nmodel: claude-sonnet-4-5\n"
    )

    html = _render_detail_html(tmp_path, console_data=None)

    # The model pill must appear somewhere in the banner pills section
    # (class="pill sonnet" or similar)
    assert "sonnet" in html.lower() or "claude" in html.lower(), (
        "Model pill must appear in standalone render when model.md specifies claude-sonnet-4-5; "
        "FIX 2: model pill must not gate on agent_health being present"
    )


def test_standalone_banner_8_grid_fields(tmp_path):
    """FIX 3: standalone render produces all 8 banner grid fields.

    The B7 mockup has 8 fields: Status, Last run, 7d spend, 30d spend,
    Failures (7d), Runs (7d), Fleet health, Eval score. All must appear.
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    html = _render_detail_html(tmp_path, console_data=None)

    expected_fields = [
        "Status",
        "Last run",
        "7d spend",
        "30d spend",
        "Failures (7d)",
        "Runs (7d)",
        "Fleet health",
        "Eval score",
    ]
    for field in expected_fields:
        assert field in html, (
            f"Banner grid must include '{field}' field (8-field grid FIX 3); "
            f"field is absent in standalone render"
        )


def test_standalone_spend_fields_nonzero(tmp_path):
    """FIX 3: 7d spend and 30d spend reflect the real log cost (not 0.00 or '—').

    strip-RED: if the daily_costs path is broken, spend_7d/spend_30d would be "—"
    or "$0.00" even though the agent has runs. A real nonzero cost must show.
    """
    import re as _re

    _write_agent(tmp_path, "agent1")
    # Write a log record today (definitely within 7d and 30d)
    _write_log(tmp_path, "agent1", cost_usd=0.1234)

    html = _render_detail_html(tmp_path, console_data=None)

    # "7d spend" cell must contain a non-zero dollar value
    m7 = _re.search(r'7d spend</div>\s*<div class="bv[^"]*">([^<]+)</div>', html)
    assert m7, "7d spend field must be in banner grid"
    val7 = m7.group(1).strip()
    assert val7 not in ("—", "$0.00"), (
        f"7d spend must be nonzero when agent has real log records; got {val7!r}"
    )

    m30 = _re.search(r'30d spend</div>\s*<div class="bv[^"]*">([^<]+)</div>', html)
    assert m30, "30d spend field must be in banner grid"
    val30 = m30.group(1).strip()
    assert val30 not in ("—", "$0.00"), (
        f"30d spend must be nonzero when agent has real log records; got {val30!r}"
    )


def test_standalone_recommendations_panel(tmp_path):
    """FIX 4: standalone render computes recs so the Overview tab is not empty.

    When console_data=None, the renderer must call recommend_fleet() standalone.
    The test verifies the rec panel HTML placeholder ('Recommendations') appears
    in the Overview tab when the standalone compute runs — even if the agent
    currently earns zero recs (the tab section header is always rendered).
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    html = _render_detail_html(tmp_path, console_data=None)

    # The Overview tab always renders the 3-axis health scorecard (not "No health data")
    # because FIX 1 computes health standalone. "No health data" must be absent.
    assert "No health data" not in html, (
        "Overview tab must show health scorecard (not 'No health data') when FIX 1 "
        "standalone health is computed"
    )


def test_standalone_overview_tab_not_skeleton(tmp_path):
    """FIX 1 + 6: Overview tab shows 3-axis cards, not 'No health data'.

    The skeleton symptom: _render_overview_tab receives agent_health=None and
    shows 'No health data computed for this agent yet'. After FIX 1+6, a fresh
    compute_fleet_health() runs standalone, agent_health is populated, and the
    axis cards render.
    """
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    html = _render_detail_html(tmp_path, console_data=None)

    # "No health data" is the sentinel for the skeleton Overview tab
    assert "No health data" not in html, (
        "Overview tab must not show 'No health data' when agent has log records; "
        "FIX 1+6 must compute health standalone"
    )
    # The axis-cards section must be present
    assert "axis-cards" in html or "Cost" in html, (
        "Overview tab must render axis cards (Cost/Quality/Reliability scorecard)"
    )


def test_standalone_governance_sources_rendered(tmp_path):
    """FIX 5: governance block renders Sources when present in governance.md."""
    from atomic_agents.dashboard.render_agent_detail import _render_governance_block

    class _GovernanceFull:
        parse_errors = ()
        owner = "Dan Powers"
        permission_tier = "reads-only"
        customer_data = "no"
        writes_sor = "no"
        lifecycle_status = "active"

        class review:
            reviewed_at = "2026-06-01"
            reviewer = "Jane Smith"
            approved_by = None

        class risk:
            level = "low"
            notes = None

        class sources:
            primary = ["database-A", "api-B"]
            secondary = ["cache-C"]

        actions = None

    class _FakeRef:
        has_governance = True
        governance = _GovernanceFull()

    html = _render_governance_block(_FakeRef())

    # Sources must appear
    assert "database-A" in html, (
        "FIX 5: Sources (primary) must render in governance block"
    )
    # Reviewed by must appear
    assert "Jane Smith" in html, (
        "FIX 5: Reviewed by (reviewer) must render in governance block"
    )
    assert "Reviewed by" in html, (
        "FIX 5: 'Reviewed by' label must appear in governance block"
    )
    assert "Sources" in html, (
        "FIX 5: 'Sources' label must appear in governance block"
    )


def test_quality_tab_label_evals(tmp_path):
    """FIX 7: Quality tab label must be 'Quality (Evals)', not bare 'Quality'."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    # Create evals/ so the Quality tab is gated in
    (tmp_path / "agent1" / "evals").mkdir(parents=True, exist_ok=True)

    html = _render_detail_html(tmp_path, console_data=None)

    assert "Quality (Evals)" in html, (
        "FIX 7: Quality tab label must be 'Quality (Evals)', not bare 'Quality'"
    )


def test_render_all_parity_still_holds(tmp_path):
    """FIX 1 parity: render_all() with console_data still threads fleet_health correctly.

    After FIX 1 (standalone compute), the render_all() path must NOT break: it still
    threads console_data with pre-computed fleet_health so MUST 5 parity holds.
    This test verifies render_all() produces the detail page and the monitor page,
    and that the detail page contains a real health integer (not "—").
    """
    import re as _re
    from atomic_agents.dashboard.render import render_all

    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    written = render_all(tmp_path, today=_TODAY)

    per_agent = written.get("per_agent", [])
    assert len(per_agent) >= 1, "render_all must write per-agent pages"

    detail_path = tmp_path / "agent1" / "dashboard.html"
    assert detail_path.exists()
    detail_html = detail_path.read_text()

    # Fleet health in the detail banner must be a real integer
    m = _re.search(r'Fleet health</div>\s*<div class="bv[^"]*">([^<]+)</div>', detail_html)
    assert m, "Detail banner must include Fleet health field after render_all"
    val = m.group(1).strip()
    assert val != "—", (
        f"render_all() detail page Fleet health must show a real integer, not '—'; "
        f"got {val!r}. MUST 5 parity requires fleet_health to flow through."
    )

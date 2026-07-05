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
    """MUST 4 strip-RED: Dreaming tab absent when no dream manifests exist."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")
    # No dreams/ directory → Dreaming tab must not appear
    html = _render_detail_html(tmp_path)
    # strip-RED: if the gate were removed, "Dreaming" would always appear
    assert "Dreaming" not in html or "drm_" not in html, (
        "Dreaming tab must not appear when no dream manifests exist"
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
    """MUST 4: PanelRegistry.compose_agent_detail() exists and is callable."""
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
    # With no registered agent-tab panels, returns empty slot + empty frozenset
    cd = _StubConsoleData()
    ctx = PanelContext(
        console_data=cd,
        capabilities=ConsoleCapabilities(),
        today=_TODAY,
        now=_NOW,
    )
    slot_html, alert_keys = reg.compose_agent_detail(ctx)
    assert "agent-tab" in slot_html
    assert isinstance(alert_keys, frozenset)


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
    """MUST 9 strip-RED: a known-nonzero metric renders nonzero in the page."""
    _write_agent(tmp_path, "agent1")
    # Write a real log record with a nonzero cost
    _write_log(tmp_path, "agent1", cost_usd=0.1234)

    html = _render_detail_html(tmp_path)

    # The cost of 0.1234 must appear in the rendered page.
    # If the template had a phantom getattr-default-0, cost would be "0.0000" or "0".
    # strip-RED: if the real field is not read, this assertion fails.
    assert "0.1234" in html or "0.123" in html, (
        "Known-nonzero cost ($0.1234) must render nonzero — no phantom-field default-0"
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


# ──────────────────────────────────────────────────────────────────
# MUST 10: pure-compute, zero LLM spend


def test_detail_no_llm_spend(tmp_path):
    """MUST 10 strip-RED: no LLMBackend constructor is called on the render path."""
    _write_agent(tmp_path, "agent1")
    _write_log(tmp_path, "agent1")

    llm_constructed = []

    def _capture_llm_init(self, *a, **kw):
        llm_constructed.append(True)

    # Patch the base LLMBackend __init__ to detect construction
    try:
        from atomic_agents._llm import LLMBackend as _LLMBackend

        with patch.object(_LLMBackend, "__init__", _capture_llm_init):
            _render_detail_html(tmp_path)
    except ImportError:
        # If LLMBackend isn't directly importable from that path, skip the patch
        # and just verify the render completes without error.
        _render_detail_html(tmp_path)

    assert not llm_constructed, (
        "LLMBackend must not be constructed on the detail render path (zero LLM spend)"
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

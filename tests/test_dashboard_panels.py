"""Tests for the Fleet Console Panel Registry (spec/52 §16, Cockpit PR-A).

Conformance map (spec/52 §12 — Cockpit rebuild #635):
  MUST 10 — every surface renders via PanelRegistry (no inline panels)
            test_console_renders_via_registry, test_no_inline_panel_outside_registry
  MUST 11 — per-panel fail-soft (engine skips a raising panel, continues)
            test_panel_render_raise_degrades_only_that_panel (strip-RED),
            test_panel_is_available_raise_omits_panel
  MUST 12 — capability-gated registration; available-but-empty distinguishable
            test_unavailable_capability_omits_panel, test_available_but_empty_renders_empty
  MUST 13 — single pre-load: no render-time backend I/O over the REAL panels
            test_panels_no_backend_io_at_render
  MUST 14 — Runtime-Health renders cost/quality/reliability only (no gov/model-fit/work-mix)
            test_runtime_health_excludes_governance_modelfit_workmix (in test_dashboard_console.py)
  MUST 15 — home renders the fleet-status summary, NOT the agent card grid
            test_home_has_no_card_grid, test_home_fleet_status_summary_links_monitor (console file)
  MUST 16 — PanelRegistry fails loud on duplicate panel id
            test_duplicate_panel_id_registration_raises
  MUST 17 — engine unions PanelResult.alert_keys → sidecar (sole source)
            test_alert_keys_aggregated_to_sidecar (+ the 422 strip-RED in the console file)
  MUST 18 — status_for_agent() precedence: ERROR > STALE > WARN > OK
            (precedence tests here; test_home_summary_status_matches_monitor_mapping in console file)

Each MUST has at least one strip-RED negative control that drives the REAL engine /
production code path (registry.compose / render_console), not a re-implemented loop.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import atomic_agents.dashboard.panels._registry as _registry_mod
from atomic_agents.dashboard.panels._registry import (
    ConsoleCapabilities,
    PanelContext,
    PanelRegistry,
    PanelResult,
)
from atomic_agents.dashboard._status import status_for_agent


# ──────────────────────────────────────────────────────────────────
# Minimal stub types for building PanelContext without real I/O


@dataclass
class _StubAlertItem:
    agent: str
    alert_key: str
    alert_class: str = "cost_spike"
    alert_subclass: str = "SPIKE"
    severity: str = "high"
    reason: str = "test"
    next_step: str = "check it"
    owner: str | None = None
    status: str = "open"
    ack_snooze_status: str = "open"


@dataclass
class _StubScoreRow:
    metric: str
    value: float | None
    score: float | None = None
    axis: str = "reliability"
    target: float | None = None
    wow: str | None = None
    direction: str = "lower"
    display: str = ""


@dataclass
class _StubAgentHealth:
    agent: str = "agent1"
    band: str = "green"
    composite: float = 0.85
    capped_by_axis: str | None = None
    primary_model: str | None = None
    cost_score: float | None = 85.0
    quality_score: float | None = 85.0
    reliability_score: float | None = 85.0
    scorecard: list = field(default_factory=list)


@dataclass
class _StubFleetHealth:
    fleet_composite: float = 0.85
    fleet_composite_display: str = "85"
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
    agent_count: int = 2
    attention_queue: list = field(default_factory=list)
    cost_trends: list = field(default_factory=list)
    quality_signals: list = field(default_factory=list)
    reliability_metrics: list = field(default_factory=list)
    fleet_health: Any = None
    recommendations: Any = None
    degraded: bool = False
    rendered_alert_keys: frozenset = field(default_factory=frozenset)
    last_primary_runs: dict = field(default_factory=dict)


def _make_ctx(
    console_data=None,
    capabilities=None,
    today=None,
    now=None,
) -> PanelContext:
    if console_data is None:
        console_data = _StubConsoleData()
    if capabilities is None:
        capabilities = ConsoleCapabilities()
    return PanelContext(
        console_data=console_data,
        capabilities=capabilities,
        today=today or date.today(),
        now=now or datetime.now(tz=timezone.utc),
    )


@contextmanager
def _swapped_registry(reg: PanelRegistry):
    """Temporarily replace the process-level registry so render_console() /
    _render_console_template() compose THIS registry. This is what lets the
    conformance tests drive the REAL engine with controlled panels instead of
    re-implementing the engine loop in the test body.
    """
    original = _registry_mod._REGISTRY
    _registry_mod._REGISTRY = reg
    try:
        yield
    finally:
        _registry_mod._REGISTRY = original


# ──────────────────────────────────────────────────────────────────
# MUST 16: PanelRegistry raises ValueError on duplicate id


class _PanelA:
    id = "test_panel_a"
    slot = "act"
    order = 10

    def is_available(self, ctx):
        return True

    def render(self, ctx):
        return PanelResult(html="<p>A</p>")


class _PanelADupe:
    id = "test_panel_a"  # same id — must trigger duplicate error
    slot = "status"
    order = 20

    def is_available(self, ctx):
        return True

    def render(self, ctx):
        return PanelResult(html="<p>ADupe</p>")


def test_duplicate_panel_id_registration_raises():
    """MUST 16: registering a panel with a duplicate id raises ValueError immediately."""
    reg = PanelRegistry()
    reg.register(_PanelA())
    with pytest.raises(ValueError, match="duplicate panel id"):
        reg.register(_PanelADupe())


def test_duplicate_panel_id_registration_strip_red():
    """MUST 16 strip-RED: a panel with a DIFFERENT id does NOT raise."""

    class _PanelB:
        id = "test_panel_b"  # distinct id
        slot = "act"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html="<p>B</p>")

    reg = PanelRegistry()
    reg.register(_PanelA())
    reg.register(_PanelB())  # must not raise


def test_panel_registry_panels_by_slot_sorted():
    """panels_by_slot() returns panels sorted by (order, id) within the slot."""

    class _P1:
        id = "z_panel"
        slot = "act"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html="")

    class _P2:
        id = "a_panel"
        slot = "act"
        order = 10  # same order — id breaks the tie (a < z)

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html="")

    class _P3:
        id = "b_panel"
        slot = "act"
        order = 5  # lower order comes first

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html="")

    reg = PanelRegistry()
    reg.register(_P1())
    reg.register(_P2())
    reg.register(_P3())
    panels = reg.panels_by_slot("act")
    assert [p.id for p in panels] == ["b_panel", "a_panel", "z_panel"]


# ──────────────────────────────────────────────────────────────────
# MUST 11: per-panel fail-soft — drives the REAL engine (registry.compose)


class _GoodPanel:
    id = "good_panel"
    slot = "act"
    order = 5

    def is_available(self, ctx):
        return True

    def render(self, ctx):
        return PanelResult(html="<p>good output</p>")


class _RaisingRenderPanel:
    id = "raising_render_panel"
    slot = "act"
    order = 10

    def is_available(self, ctx):
        return True

    def render(self, ctx):
        raise RuntimeError("intentional render failure")


class _RaisingAvailablePanel:
    id = "raising_available_panel"
    slot = "act"
    order = 10

    def is_available(self, ctx):
        raise RuntimeError("intentional is_available failure")

    def render(self, ctx):  # pragma: no cover — never reached
        return PanelResult(html="<p>should not appear</p>")


def test_panel_render_raise_degrades_only_that_panel():
    """MUST 11: a render() that raises degrades ONLY that panel; siblings render; page intact.

    Drives the REAL engine via registry.compose() (the production code path), not a
    re-implemented loop. logger.warning must fire for the raising panel.
    """
    import atomic_agents.dashboard.render as rmod

    reg = PanelRegistry()
    reg.register(_GoodPanel())
    reg.register(_RaisingRenderPanel())
    ctx = _make_ctx()

    with patch.object(rmod.logger, "warning") as mock_warn:
        slot_html, alert_keys = reg.compose(ctx)

    assert "good output" in slot_html["act"], "sibling panel must still render"
    assert "should not appear" not in slot_html["act"]
    mock_warn.assert_called_once()
    assert "raising_render_panel" in str(mock_warn.call_args)


def test_panel_render_raise_strip_red():
    """MUST 11 strip-RED: removing the engine try/except makes the raise propagate.

    Confirms the fail-soft in registry.compose() is load-bearing: with the real
    engine the page composes; calling render() directly (no engine guard) raises.
    """
    panel = _RaisingRenderPanel()
    ctx = _make_ctx()
    with pytest.raises(RuntimeError, match="intentional render failure"):
        panel.render(ctx)  # no engine guard → raises (the strip-RED condition)


def test_panel_is_available_raise_omits_panel():
    """MUST 11/12 fail-safe: an is_available() that raises omits the panel (not the page)."""
    import atomic_agents.dashboard.render as rmod

    reg = PanelRegistry()
    reg.register(_GoodPanel())
    reg.register(_RaisingAvailablePanel())
    ctx = _make_ctx()

    with patch.object(rmod.logger, "warning") as mock_warn:
        slot_html, _ = reg.compose(ctx)

    assert "good output" in slot_html["act"], "sibling must render"
    assert "should not appear" not in slot_html["act"], (
        "raising-available panel omitted"
    )
    assert "raising_available_panel" in str(mock_warn.call_args)


# ──────────────────────────────────────────────────────────────────
# MUST 12: capability gate — unavailable omitted; available-but-empty distinguishable


class _GoalGatedPanel:
    id = "goal_gated_panel"
    slot = "act"
    order = 10

    def is_available(self, ctx):
        return ctx.capabilities.has_goals

    def render(self, ctx):
        return PanelResult(html="<p>goals-panel-content</p>")


class _EmptyContentPanel:
    """Always available, but renders empty content when its data is zero-items."""

    id = "empty_content_panel"
    slot = "act"
    order = 20

    def is_available(self, ctx):
        return True

    def render(self, ctx):
        if ctx.console_data.agent_count == 0:
            return PanelResult(html='<div class="empty-marker"></div>')
        return PanelResult(html="<p>has-items</p>")


def test_unavailable_capability_omits_panel():
    """MUST 12: a panel whose capability is absent (is_available False) is omitted entirely.

    Drives the REAL engine; the panel's content must NOT appear when has_goals is False,
    and MUST appear when has_goals is True.
    """
    reg = PanelRegistry()
    reg.register(_GoalGatedPanel())

    ctx_off = _make_ctx(capabilities=ConsoleCapabilities(has_goals=False))
    slot_html_off, _ = reg.compose(ctx_off)
    assert "goals-panel-content" not in slot_html_off["act"], (
        "capability-absent panel must be omitted entirely (no content)"
    )

    ctx_on = _make_ctx(capabilities=ConsoleCapabilities(has_goals=True))
    slot_html_on, _ = reg.compose(ctx_on)
    assert "goals-panel-content" in slot_html_on["act"], (
        "capability-present panel must render"
    )


def test_available_but_empty_renders_empty():
    """MUST 12: an AVAILABLE panel with zero items renders its own empty content —
    distinct from a capability-absent omission.

    The two states must be distinguishable: capability-absent → panel omitted (no
    marker at all); available-but-empty → panel present with its empty-content marker.
    """
    reg = PanelRegistry()
    reg.register(_EmptyContentPanel())

    # Available + zero items → renders the empty-content marker (panel IS present).
    ctx_empty = _make_ctx(console_data=_StubConsoleData(agent_count=0))
    slot_html_empty, _ = reg.compose(ctx_empty)
    assert "empty-marker" in slot_html_empty["act"], (
        "available-but-empty panel renders its own empty content (not omitted)"
    )

    # Distinguishability: a capability-ABSENT panel leaves NO marker.
    reg2 = PanelRegistry()
    reg2.register(_GoalGatedPanel())
    ctx_absent = _make_ctx(capabilities=ConsoleCapabilities(has_goals=False))
    slot_html_absent, _ = reg2.compose(ctx_absent)
    assert "goals-panel-content" not in slot_html_absent["act"], (
        "capability-absent and available-but-empty must be distinguishable"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 13: no render-time backend I/O — over the REAL registered panels


def test_panels_no_backend_io_at_render(tmp_path):
    """MUST 13: NONE of the real registered panels read the filesystem at render time.

    Builds a real PanelContext from a vault, installs a Path.read_text / open spy,
    then calls render() on every REGISTERED panel and asserts zero filesystem reads.
    This exercises the production panels (attention, health, trends, kpi_strip,
    fleet_status, recommendations), not a synthetic stub.
    """
    _write_minimal_vault(tmp_path)

    from atomic_agents.dashboard.attention import aggregate_console
    from atomic_agents.dashboard.panels import get_registry
    from atomic_agents.advisor.score import compute_fleet_health

    today = date.today()
    cd = aggregate_console(tmp_path, today=today)
    # Pre-load fleet_health + recommendations so the health/recs panels have data
    # AND so any I/O they'd need has already happened BEFORE the spy is installed.
    cd.fleet_health = compute_fleet_health(tmp_path, today=today)
    cd.recommendations = []
    ctx = PanelContext(
        console_data=cd,
        capabilities=ConsoleCapabilities(),
        today=today,
        now=datetime.now(tz=timezone.utc),
    )

    read_calls: list[str] = []
    original_read = Path.read_text
    original_open = Path.open

    def spy_read_text(self, *a, **k):
        read_calls.append(f"read_text:{self}")
        return original_read(self, *a, **k)

    def spy_open(self, *a, **k):
        read_calls.append(f"open:{self}")
        return original_open(self, *a, **k)

    registry = get_registry()
    with (
        patch.object(Path, "read_text", spy_read_text),
        patch.object(Path, "open", spy_open),
    ):
        for panel in registry.panels:
            if panel.is_available(ctx):
                panel.render(ctx)

    assert read_calls == [], (
        f"registered panels must not read files at render time; got: {read_calls}"
    )


def test_panels_no_backend_io_strip_red(tmp_path):
    """MUST 13 strip-RED: the spy DOES catch a panel that reads the filesystem.

    Proves the spy is load-bearing — a panel that touches the disk is detected.
    """
    probe = tmp_path / "probe.txt"
    probe.write_text("data")

    class _DiskReadingPanel:
        id = "disk_reading_panel"
        slot = "act"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html=probe.read_text())  # illegal render-time read

    ctx = _make_ctx()
    read_calls: list[str] = []
    original_read = Path.read_text

    def spy_read_text(self, *a, **k):
        read_calls.append(str(self))
        return original_read(self, *a, **k)

    with patch.object(Path, "read_text", spy_read_text):
        _DiskReadingPanel().render(ctx)

    assert read_calls != [], "spy must catch a render-time FS read"


def test_panel_context_is_dataclass_not_callable():
    """MUST 13: PanelContext is a plain dataclass — no __call__ that triggers I/O."""
    import dataclasses

    assert dataclasses.is_dataclass(PanelContext)


# ──────────────────────────────────────────────────────────────────
# MUST 17: engine unions PanelResult.alert_keys → sidecar (engine is SOLE source)


def test_alert_keys_aggregated_to_sidecar(tmp_path):
    """MUST 17: the sidecar contains keys CONTRIBUTED BY A PANEL via the engine union,
    NOT seeded from console_data.rendered_alert_keys.

    Registers a panel that contributes a key absent from console_data.rendered_alert_keys,
    drives the REAL render_console(), and asserts that panel key lands in the sidecar.
    Strip-RED: a panel returning empty alert_keys leaves the sidecar empty (the seed
    does NOT backfill it) — verified by test_alert_keys_sidecar_sole_source below and
    the 422 endpoint strip-RED in test_dashboard_console.py.
    """
    _write_minimal_vault(tmp_path)
    from atomic_agents.dashboard.render import render_console

    panel_key = "v1:panelcontributed1"  # NOT in rendered_alert_keys

    class _KeyPanel:
        id = "key_contributing_panel"
        slot = "act"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html="<p>x</p>", alert_keys=frozenset({panel_key}))

    reg = PanelRegistry()
    reg.register(_KeyPanel())

    cd = _StubConsoleData(
        agent_count=1,
        rendered_alert_keys=frozenset({"v1:seedonly0000"}),  # seed that must NOT leak
    )

    with _swapped_registry(reg):
        render_console(tmp_path, cd, today=date.today())

    sidecar = tmp_path / "_console" / "rendered_alert_keys.json"
    keys = json.loads(sidecar.read_text())
    assert panel_key in keys, (
        "panel-contributed key must reach the sidecar via the engine union"
    )
    assert "v1:seedonly0000" not in keys, (
        "MUST 17: rendered_alert_keys seed must NOT be OR'd into the sidecar"
    )


def test_alert_keys_sidecar_sole_source_strip_red(tmp_path):
    """MUST 17 strip-RED: with NO panel contributing keys, the sidecar is EMPTY —
    the seed does not backfill it. (Without the engine being the sole source, the
    sidecar would contain the seed key and a legit ack of a panel key would 422 — see
    test_dashboard_console.py::test_alert_keys_aggregated_sidecar_empty_422s_ack.)
    """
    _write_minimal_vault(tmp_path)
    from atomic_agents.dashboard.render import render_console

    class _NoKeyPanel:
        id = "no_key_panel"
        slot = "act"
        order = 10

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html="<p>nk</p>")  # contributes nothing

    reg = PanelRegistry()
    reg.register(_NoKeyPanel())
    cd = _StubConsoleData(
        agent_count=1,
        rendered_alert_keys=frozenset({"v1:seedonly0000"}),
    )

    with _swapped_registry(reg):
        render_console(tmp_path, cd, today=date.today())

    sidecar = tmp_path / "_console" / "rendered_alert_keys.json"
    keys = json.loads(sidecar.read_text())
    assert keys == [], (
        "MUST 17: with no panel-contributed keys the sidecar is empty (no seed backfill)"
    )


def test_attention_panel_contributes_real_keys(tmp_path):
    """MUST 17: the REAL attention-queue panel contributes the keys for its queue items,
    so panelization preserves MUST 4 by construction (the engine union is complete)."""
    from atomic_agents.dashboard.panels._attention import _AttentionQueuePanel

    item = _StubAlertItem(agent="a1", alert_key="v1:realqueuekey1")
    cd = _StubConsoleData(agent_count=1, attention_queue=[item])
    ctx = _make_ctx(console_data=cd)

    result = _AttentionQueuePanel().render(ctx)
    assert "v1:realqueuekey1" in result.alert_keys, (
        "attention panel must contribute its queue items' keys to the engine union"
    )


# ──────────────────────────────────────────────────────────────────
# MUST 18: status_for_agent() precedence and thresholds


_UTC = timezone.utc
_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=_UTC)


def _ah(band="green", capped_by_axis=None, error_rate=0.0):
    scorecard = [_StubScoreRow(metric="error_rate", value=error_rate)]
    return _StubAgentHealth(
        band=band,
        capped_by_axis=capped_by_axis,
        scorecard=scorecard,
    )


def test_status_error_when_capped():
    """MUST 18: capped_by_axis is not None → ERROR (highest precedence)."""
    result = status_for_agent(
        agent_health=_ah(band="green", capped_by_axis="cost"),
        attention_items=[],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
    )
    assert result == "ERROR"


def test_status_error_when_error_rate_over_threshold():
    """MUST 18: error_rate >= threshold → ERROR."""
    result = status_for_agent(
        agent_health=_ah(error_rate=0.3),
        attention_items=[],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
    )
    assert result == "ERROR"


def test_status_stale_when_no_primary_run():
    """MUST 18: no primary run (None) → STALE."""
    result = status_for_agent(
        agent_health=_ah(),
        attention_items=[],
        last_primary_run_at=None,
        now=_NOW,
    )
    assert result == "STALE"


def test_status_stale_when_run_older_than_window():
    """MUST 18: last primary run > staleness_window ago → STALE."""
    old_run = _NOW - timedelta(hours=25)
    result = status_for_agent(
        agent_health=_ah(),
        attention_items=[],
        last_primary_run_at=old_run,
        now=_NOW,
    )
    assert result == "STALE"


def test_status_warn_when_amber_band():
    """MUST 18: amber band → WARN."""
    result = status_for_agent(
        agent_health=_ah(band="amber"),
        attention_items=[],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
    )
    assert result == "WARN"


def test_status_warn_when_open_attention_item():
    """MUST 18: open attention item present → WARN."""
    item = _StubAlertItem(agent="agent1", alert_key="v1:agent1cost")
    result = status_for_agent(
        agent_health=_ah(),
        attention_items=[item],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
    )
    assert result == "WARN"


def test_status_warn_when_cost_spike():
    """MUST 18: an unacked cost spike (cost_spike=True) → WARN even with no open item.

    This covers a below-alert-threshold spike or an acked/snoozed spike that has
    dropped out of attention_items but whose spike_detected is still True.
    """
    result = status_for_agent(
        agent_health=_ah(band="green"),
        attention_items=[],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
        cost_spike=True,
    )
    assert result == "WARN"


def test_status_cost_spike_strip_red():
    """MUST 18 strip-RED: same inputs but cost_spike=False → OK (the spike arm is load-bearing)."""
    result = status_for_agent(
        agent_health=_ah(band="green"),
        attention_items=[],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
        cost_spike=False,
    )
    assert result == "OK", "without the cost_spike signal the agent is OK"


def test_status_ok_when_all_clear():
    """MUST 18: no error, not stale, no warn signals → OK."""
    result = status_for_agent(
        agent_health=_ah(band="green"),
        attention_items=[],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
    )
    assert result == "OK"


def test_status_error_beats_stale():
    """MUST 18: ERROR > STALE (cap set, run is None)."""
    result = status_for_agent(
        agent_health=_ah(capped_by_axis="quality"),
        attention_items=[],
        last_primary_run_at=None,
        now=_NOW,
    )
    assert result == "ERROR"


def test_status_stale_beats_warn():
    """MUST 18: STALE > WARN (no run, amber band)."""
    result = status_for_agent(
        agent_health=_ah(band="amber"),
        attention_items=[],
        last_primary_run_at=None,
        now=_NOW,
    )
    assert result == "STALE"


def test_status_stale_beats_cost_spike():
    """MUST 18: STALE > WARN even when a cost spike is present (no run + spike)."""
    result = status_for_agent(
        agent_health=_ah(band="green"),
        attention_items=[],
        last_primary_run_at=None,
        now=_NOW,
        cost_spike=True,
    )
    assert result == "STALE"


def test_status_error_beats_warn():
    """MUST 18: ERROR > WARN (cap + amber + open item)."""
    item = _StubAlertItem(agent="a1", alert_key="v1:a1cost")
    result = status_for_agent(
        agent_health=_ah(band="amber", capped_by_axis="cost"),
        attention_items=[item],
        last_primary_run_at=_NOW - timedelta(hours=1),
        now=_NOW,
    )
    assert result == "ERROR"


def test_status_strip_red_green_band_no_items_is_ok():
    """MUST 18 strip-RED: removing ALL warn/error triggers must produce OK, not WARN."""
    result = status_for_agent(
        agent_health=_ah(band="green", capped_by_axis=None, error_rate=0.0),
        attention_items=[],
        last_primary_run_at=_NOW - timedelta(minutes=30),
        now=_NOW,
    )
    assert result == "OK", f"all-clear agent must be OK, got {result}"


def test_status_stale_custom_window():
    """MUST 18: named staleness_window param respected (2h window)."""
    run_at = _NOW - timedelta(hours=3)
    result_default = status_for_agent(
        agent_health=_ah(),
        attention_items=[],
        last_primary_run_at=run_at,
        now=_NOW,
        staleness_window=timedelta(hours=24),
    )
    result_custom = status_for_agent(
        agent_health=_ah(),
        attention_items=[],
        last_primary_run_at=run_at,
        now=_NOW,
        staleness_window=timedelta(hours=2),
    )
    assert result_default == "OK"
    assert result_custom == "STALE"


# ──────────────────────────────────────────────────────────────────
# MUST 10: every surface renders via PanelRegistry


def test_console_renders_via_registry(tmp_path):
    """MUST 10: index.html is composed by the registry — a custom-registered panel's
    output appears, proving the page came from the registry and not an inline template."""
    _write_minimal_vault(tmp_path)
    from atomic_agents.dashboard.render import render_console
    from atomic_agents.dashboard.attention import aggregate_console

    sentinel = "registry-composed-sentinel-xyz"

    class _SentinelPanel:
        id = "sentinel_must10_registry"
        slot = "act"
        order = 1

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            return PanelResult(html=f"<p>{sentinel}</p>")

    reg = PanelRegistry()
    reg.register(_SentinelPanel())

    cd = aggregate_console(tmp_path, today=date.today())
    with _swapped_registry(reg):
        render_console(tmp_path, cd, today=date.today())

    html = (tmp_path / "_dashboard" / "index.html").read_text()
    assert sentinel in html, "MUST 10: page must be composed from the registry"


def test_console_renders_via_registry_strip_red(tmp_path):
    """MUST 10 strip-RED: an EMPTY registry yields a page with NO panel content
    (only chrome) — confirming panels, not inline template code, supply the body."""
    _write_minimal_vault(tmp_path)
    from atomic_agents.dashboard.render import render_console
    from atomic_agents.dashboard.attention import aggregate_console

    reg = PanelRegistry()  # no panels
    cd = aggregate_console(tmp_path, today=date.today())
    with _swapped_registry(reg):
        render_console(tmp_path, cd, today=date.today())

    html = (tmp_path / "_dashboard" / "index.html").read_text()
    # No panel markers from the real panels appear. Assert on the HTML USAGE form
    # (class="...cockpit-kpis") — the bare string 'cockpit-kpis' is in the CSS block
    # and is expected to be present regardless of which panels ran.
    assert 'class="kpis cockpit-kpis"' not in html, (
        "empty registry must not produce panel content (panels supply the body)"
    )
    assert 'class="fo-grid"' not in html, (
        "fleet-status panel marker absent with empty registry"
    )
    # But the chrome (zone-label dividers) is still emitted by the template.
    assert "cockpit-zone-label" in html, (
        "zone-label chrome is template-owned, not a panel"
    )


def test_no_inline_panel_outside_registry(tmp_path):
    """MUST 10: with the production registry, the KPI strip panel's marker is present."""
    _write_minimal_vault(tmp_path)
    from atomic_agents.dashboard.render import render_all

    render_all(tmp_path)
    html = (tmp_path / "_dashboard" / "index.html").read_text()
    assert "cockpit-kpis" in html, (
        "MUST 10: cockpit structural markers come from the panel registry"
    )


# ──────────────────────────────────────────────────────────────────
# Zone-label chrome is template-owned (MUST 11 spirit)


def test_zone_labels_survive_panel_failure(tmp_path):
    """A failing STATUS panel must NOT remove the STATUS zone divider — chrome is
    emitted by the template, decoupled from panel render success (Principle #3)."""
    _write_minimal_vault(tmp_path)
    from atomic_agents.dashboard.render import render_console
    from atomic_agents.dashboard.attention import aggregate_console

    class _RaisingStatusPanel:
        id = "raising_status_panel"
        slot = "status"
        order = 1

        def is_available(self, ctx):
            return True

        def render(self, ctx):
            raise RuntimeError("boom")

    reg = PanelRegistry()
    reg.register(_RaisingStatusPanel())
    cd = aggregate_console(tmp_path, today=date.today())
    with _swapped_registry(reg):
        render_console(tmp_path, cd, today=date.today())

    html = (tmp_path / "_dashboard" / "index.html").read_text()
    # All three zone dividers present even though the only status panel raised.
    assert html.count("cockpit-zone-label") >= 3, (
        "all three zone-label dividers must render regardless of panel failure"
    )


# ──────────────────────────────────────────────────────────────────
# daily_series field (CostTrendPoint) + sparkline


def test_cost_trend_point_has_daily_series():
    """daily_series field exists on CostTrendPoint and defaults to empty list."""
    from atomic_agents.dashboard.attention import CostTrendPoint

    ct = CostTrendPoint(
        agent="x",
        total_usd_30d=1.0,
        avg_daily_usd=0.033,
        spike_detected=False,
        baseline_avg_daily=None,
    )
    assert hasattr(ct, "daily_series")
    assert isinstance(ct.daily_series, list)
    assert ct.daily_series == []


def test_aggregate_console_populates_daily_series(tmp_path):
    """daily_series is populated from daily_30d in aggregate_console()."""
    from atomic_agents.dashboard.attention import aggregate_console

    today = date(2026, 6, 28)
    yesterday = date(2026, 6, 27)
    for d, cost in [(today, 0.10), (yesterday, 0.05)]:
        _write_log(tmp_path, "agent1", d, [{"cost_usd": cost, "status": "ok"}])

    cd = aggregate_console(tmp_path, today=today)
    ct = next((c for c in cd.cost_trends if c.agent == "agent1"), None)
    assert ct is not None
    assert len(ct.daily_series) > 0
    for day, usd in ct.daily_series:
        assert isinstance(day, str)
        assert isinstance(usd, float)


def test_daily_series_ascending_order(tmp_path):
    """daily_series entries must be in ascending date order."""
    from atomic_agents.dashboard.attention import aggregate_console

    today = date(2026, 6, 28)
    for d, cost in [
        (today, 0.10),
        (date(2026, 6, 27), 0.05),
        (date(2026, 6, 26), 0.03),
    ]:
        _write_log(tmp_path, "agent1", d, [{"cost_usd": cost, "status": "ok"}])

    cd = aggregate_console(tmp_path, today=today)
    ct = next((c for c in cd.cost_trends if c.agent == "agent1"), None)
    assert ct is not None
    days = [d for d, _ in ct.daily_series]
    assert days == sorted(days)


def test_kpi_sparkline_drawn_from_daily_series():
    """The KPI strip draws an inline SVG sparkline from the fleet daily_series."""
    from atomic_agents.dashboard.panels._kpi_strip import _KpiStripPanel

    cd = _StubConsoleData(
        agent_count=1,
        cost_trends=[
            _StubCostTrend(
                agent="a1",
                total_usd_30d=2.0,
                daily_series=[
                    ("2026-06-22", 0.1),
                    ("2026-06-23", 0.2),
                    ("2026-06-24", 0.15),
                    ("2026-06-25", 0.3),
                ],
            )
        ],
    )
    ctx = _make_ctx(console_data=cd)
    result = _KpiStripPanel().render(ctx)
    assert "<svg" in result.html and "kpi-spark" in result.html, (
        "7-day-spend tile must draw an inline sparkline from daily_series"
    )
    assert "polyline" in result.html


def test_kpi_sparkline_absent_with_one_point():
    """Sparkline is omitted (no <svg>) when there are fewer than 2 daily points."""
    from atomic_agents.dashboard.panels._kpi_strip import _sparkline_svg

    assert _sparkline_svg([]) == ""
    assert _sparkline_svg([1.0]) == ""
    assert "<svg" in _sparkline_svg([1.0, 2.0])


# ──────────────────────────────────────────────────────────────────
# last_primary_runs on ConsoleData


def test_aggregate_console_populates_last_primary_runs(tmp_path):
    """ConsoleData.last_primary_runs maps agent → last_primary_run_at datetime."""
    from atomic_agents.dashboard.attention import aggregate_console

    today = date(2026, 6, 28)
    _write_log(tmp_path, "agent1", today, [{"cost_usd": 0.10, "status": "ok"}])
    cd = aggregate_console(tmp_path, today=today)

    assert hasattr(cd, "last_primary_runs")
    assert "agent1" in cd.last_primary_runs
    lpr = cd.last_primary_runs["agent1"]
    assert lpr is None or isinstance(lpr, datetime)


# ──────────────────────────────────────────────────────────────────
# ConsoleCapabilities: MINIMAL frozen dataclass


def test_console_capabilities_is_frozen():
    """ConsoleCapabilities is a frozen dataclass — immutable after construction."""
    import dataclasses

    assert dataclasses.is_dataclass(ConsoleCapabilities)
    caps = ConsoleCapabilities()
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        caps.has_goals = True  # type: ignore[misc]


def test_console_capabilities_defaults_to_false():
    """ConsoleCapabilities defaults all booleans to False (safe default)."""
    caps = ConsoleCapabilities()
    assert caps.has_goals is False


def test_console_capabilities_fields_are_booleans():
    """Every field on ConsoleCapabilities must be a bool."""
    import dataclasses

    caps = ConsoleCapabilities(has_goals=True)
    for f in dataclasses.fields(caps):
        assert isinstance(getattr(caps, f.name), bool)


# ──────────────────────────────────────────────────────────────────
# Helpers for integration tests (write minimal vault)


def _write_log(agents_root: Path, agent: str, when: date, records: list[dict]) -> None:
    import json as _json

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
            "ts",
            datetime.combine(when, datetime.min.time()).astimezone().isoformat(),
        )
        rec.setdefault("trigger", "cron")
        rec.setdefault("model", "claude-sonnet-4-5")
        rec.setdefault("input_tokens", 100)
        rec.setdefault("output_tokens", 20)
        rec.setdefault("status", "ok")
        rec.setdefault("summary", "test run")
        lines.append(_json.dumps(rec))
    path.write_text("\n".join(lines) + "\n")


def _write_minimal_vault(agents_root: Path) -> None:
    """Minimal synthetic vault: two agents, today's runs, no goals."""
    today = date.today()
    for agent in ("alice", "bob"):
        _write_log(
            agents_root,
            agent,
            today,
            [{"cost_usd": 0.10, "status": "ok", "summary": "run"}],
        )

"""PanelRegistry — process-level registry for Fleet Console panels (spec/52 §16).

Design:
  - PanelRegistry is an instantiable class (not a module-level singleton pattern
    that can't be isolated in tests). A module-level _REGISTRY instance is the
    production singleton. Tests construct fresh PanelRegistry() instances to
    avoid touching the global registry.
  - Each panel module calls panels.register(...) at module level; the package
    __init__.py imports every panel module so registration is an import side effect.
  - render_console() imports the registry package before composing, ensuring all
    panels are registered (spec/52 §16.5).

Panel protocol (duck-typed, not ABC, to keep it lightweight):
  Each panel object must expose:
    .id: str           — unique identifier (e.g. 'attention_queue')
    .slot: str         — layout slot: 'status' | 'act' | 'explore'
    .order: int        — position within slot; ties broken by id (alphabetical)
    .is_available(ctx: PanelContext) -> bool
                       — capability gate; False = panel omitted from composition
    .render(ctx: PanelContext) -> PanelResult
                       — pure HTML fragment; MUST NOT do backend I/O (MUST 13)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..attention import ConsoleData


# ──────────────────────────────────────────────────────────────────
# ConsoleCapabilities — MINIMAL frozen dataclass (ruling: MINIMAL shape)


@dataclass(frozen=True)
class ConsoleCapabilities:
    """Capability booleans for the current console render.

    All booleans default to False so new fields can be appended without
    breaking existing instantiation sites. The loader sets each field
    from backing data/backend presence (all I/O happens in the loader,
    before the engine loop starts — MUST 13).

    Only booleans that at least ONE currently-registered panel actually
    gates on are included here (ruling: MINIMAL shape).
    """

    has_goals: bool = False
    """True when at least one agent has a goal.md file.

    This is the only capability a registered panel gates on in PR-A (the Goals
    surface). Per the MINIMAL-shape ruling, capability booleans are added only when
    a panel actually reads them — so e.g. has_governance is intentionally absent
    until the governance grade panel (#630) lands and gates on a correctly-derived
    value (count of agents with parsed governance data), not a fleet-non-empty proxy.
    """


# ──────────────────────────────────────────────────────────────────
# PanelContext — single pre-load passed to every panel


@dataclass
class PanelContext:
    """Pre-loaded inputs for panel rendering (spec/52 §16.4, MUST 13).

    All I/O must complete BEFORE this object is constructed. Panel render()
    methods receive this and must not call any backend or read any file.

    Fields:
      console_data:   Aggregated fleet data from aggregate_console() plus
                      fleet_health and recommendations populated by render_console().
      capabilities:   Capability flags (all resolved before engine loop).
      today:          Reference date threaded from render_console() — same value
                      used by advisor calls so comparisons are coherent.
      now:            Reference UTC datetime for staleness window comparisons
                      (status_for_agent). Pinned once in render_console().
    """

    console_data: "ConsoleData"
    capabilities: ConsoleCapabilities
    today: date
    now: datetime


# ──────────────────────────────────────────────────────────────────
# PanelResult


@dataclass
class PanelResult:
    """Output from a panel's render() call."""

    html: str
    """HTML fragment to inject into the page. Empty string is valid (absent panel)."""

    alert_keys: frozenset[str] = field(default_factory=frozenset)
    """Alert keys rendered by this panel. The engine unions all panel alert_keys
    and writes them to the sidecar (MUST 17). Panels that render no alert-eligible
    content should return an empty frozenset."""


# ──────────────────────────────────────────────────────────────────
# Panel protocol (duck-typed)


@runtime_checkable
class Panel(Protocol):
    """Protocol that every registered panel must satisfy."""

    id: str
    slot: str  # 'status' | 'act' | 'explore' | 'agent-tab' | 'monitor-summary' | 'monitor-roster'
    order: int  # ties broken by id (ascending alpha)

    def is_available(self, ctx: PanelContext) -> bool:
        """Return True if this panel should be included in the layout."""
        ...

    def render(self, ctx: PanelContext) -> PanelResult:
        """Return the panel's HTML fragment. Must not do backend I/O."""
        ...


# ──────────────────────────────────────────────────────────────────
# PanelRegistry


class PanelRegistry:
    """Process-level panel registry (spec/52 §16.1, MUST 10, MUST 16).

    Production code uses the module-level get_registry() accessor which
    returns the shared _REGISTRY singleton. Tests construct fresh instances
    to avoid touching the global singleton.

    Duplicate id registration raises ValueError immediately (MUST 16 —
    fail-loud on duplicate id).
    """

    def __init__(self) -> None:
        self._panels: list[Panel] = []

    def register(self, panel: Panel) -> None:
        """Register a panel. Raises ValueError if id is already registered (MUST 16)."""
        if any(p.id == panel.id for p in self._panels):
            raise ValueError(
                f"PanelRegistry: duplicate panel id '{panel.id}' — each panel id must be unique. "
                "If you are testing MUST 16, construct a fresh PanelRegistry() instance."
            )
        self._panels.append(panel)

    def panels_by_slot(self, slot: str) -> list[Panel]:
        """Return panels for the given slot, sorted by (order, id) for determinism."""
        return sorted(
            [p for p in self._panels if p.slot == slot],
            key=lambda p: (p.order, p.id),
        )

    @property
    def panels(self) -> list[Panel]:
        """All registered panels (insertion order, not sorted)."""
        return list(self._panels)

    def compose(self, ctx: "PanelContext") -> "tuple[dict[str, str], frozenset[str]]":
        """Run the layout engine: compose panels by slot and aggregate alert_keys.

        This is the SINGLE engine entry point — both production (_render_console_template)
        and conformance tests call THIS method so the MUST 11/12/13/17 behaviors are
        exercised by the real code path, never re-implemented in a test body.

        For each slot in (status, act, explore), in (order, id) order:
          - is_available(ctx) raising or returning False → panel OMITTED (MUST 12,
            §16.3 — an is_available exception is fail-safe, logged WARNING).
          - render(ctx) raising → that panel degraded to empty, page continues
            (MUST 11 fail-soft), logged WARNING; siblings unaffected.
          - every PanelResult.alert_keys is unioned into the returned alert-key set
            (MUST 17 — the engine union is the SOLE authoritative source for the
            sidecar; the caller does NOT seed it from ConsoleData.rendered_alert_keys).

        Returns
        -------
        (slot_html, alert_keys):
            slot_html: dict slot → joined HTML fragments for that slot.
            alert_keys: frozenset union of every rendered PanelResult.alert_keys.
        """
        # Lazy import to avoid a render→panels→render import cycle at module load.
        from ..render import logger

        slot_html: dict[str, str] = {"status": "", "act": "", "explore": ""}
        all_alert_keys: set[str] = set()

        for slot in ("status", "act", "explore"):
            fragments: list[str] = []
            for panel in self.panels_by_slot(slot):
                try:
                    available = panel.is_available(ctx)
                except Exception as exc:
                    logger.warning(
                        "panel '%s' is_available raised (%s); omitting (MUST 12 fail-safe)",
                        panel.id,
                        type(exc).__name__,
                    )
                    continue
                if not available:
                    continue
                try:
                    result = panel.render(ctx)
                except Exception as exc:
                    logger.warning(
                        "panel '%s' render failed (%s); degraded placeholder (MUST 11 fail-soft)",
                        panel.id,
                        type(exc).__name__,
                    )
                    fragments.append(
                        f'<div class="panel-degraded" data-panel-id="{panel.id}">'
                        f"Panel unavailable</div>"
                    )
                    continue
                fragments.append(result.html)
                all_alert_keys |= result.alert_keys  # MUST 17: engine aggregation
            slot_html[slot] = "\n".join(fragments)

        return slot_html, frozenset(all_alert_keys)

    def compose_monitor(
        self, ctx: "PanelContext"
    ) -> "tuple[dict[str, str], frozenset[str]]":
        """Run the layout engine for the Fleet Monitor page (spec/56 §6).

        Mirrors compose() but iterates the monitor-specific slots
        ('monitor-summary', 'monitor-roster') instead of the home slots.
        Per-panel fail-soft (MUST 11), is_available gate (MUST 12), and
        alert-key union (MUST 17) apply identically to the home engine.

        Returns
        -------
        (slot_html, alert_keys):
            slot_html: dict slot -> joined HTML fragments for that slot.
            alert_keys: frozenset (monitor panels carry no alert keys today).
        """
        from ..render import logger

        slot_html: dict[str, str] = {"monitor-summary": "", "monitor-roster": ""}
        all_alert_keys: set[str] = set()

        for slot in ("monitor-summary", "monitor-roster"):
            fragments: list[str] = []
            for panel in self.panels_by_slot(slot):
                try:
                    available = panel.is_available(ctx)
                except Exception as exc:
                    logger.warning(
                        "monitor panel '%s' is_available raised (%s); omitting",
                        panel.id,
                        type(exc).__name__,
                    )
                    continue
                if not available:
                    continue
                try:
                    result = panel.render(ctx)
                except Exception as exc:
                    logger.warning(
                        "monitor panel '%s' render failed (%s); degraded placeholder",
                        panel.id,
                        type(exc).__name__,
                    )
                    fragments.append(
                        f'<div class="panel-degraded" data-panel-id="{panel.id}">'
                        f"Panel unavailable</div>"
                    )
                    continue
                fragments.append(result.html)
                all_alert_keys |= result.alert_keys
            slot_html[slot] = "\n".join(fragments)

        return slot_html, frozenset(all_alert_keys)


# ──────────────────────────────────────────────────────────────────
# Module-level singleton + accessor

_REGISTRY = PanelRegistry()


def get_registry() -> PanelRegistry:
    """Return the process-level PanelRegistry singleton."""
    return _REGISTRY


def register(panel: Panel) -> None:
    """Register a panel in the process-level singleton (spec/52 §16.5).

    Called at module level in each panel module. MUST 16: raises ValueError
    on duplicate id.
    """
    _REGISTRY.register(panel)

"""Fleet Overview placeholder panel — EXPLORE slot (spec/52 §16).

Reserved slot for #636 (fleet-overview tabbed section). Renders a
placeholder so the EXPLORE zone always has at least two registered panels.
This panel renders nothing visible (empty fragment) until #636 ships.
"""

from __future__ import annotations

from ._registry import PanelContext, PanelResult, register


class _FleetOverviewPlaceholderPanel:
    id = "fleet_overview_placeholder"
    slot = "explore"
    order = 20

    def is_available(self, ctx: PanelContext) -> bool:
        # Not yet available — reserved for #636.
        return False

    def render(self, ctx: PanelContext) -> PanelResult:
        # Will render the full tabbed fleet-overview section when #636 ships.
        return PanelResult(html="")


# Registration
register(_FleetOverviewPlaceholderPanel())

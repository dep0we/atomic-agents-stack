"""Fleet Console Panel Registry package (spec/52 §16).

This package is the single authoritative list of all registered panel modules.
Importing this package triggers all panel registrations as import side effects
(spec/52 §16.5).

Panel modules MUST be imported here in dependency order. A missing import
is a silent omission — the panel simply won't appear in the layout.

render_console() imports this package before composing the layout. Tests that
need isolation should construct a fresh PanelRegistry() instance rather than
importing this package, since the global singleton accumulates registrations
across the process lifetime.

Import discipline: panel modules MUST NOT import from advisor/ at module level
(only inside is_available()/render() to avoid triggering the advisor conftest
guard during test collection). See prep finding P1 on import ordering.
"""

from ._registry import (
    ConsoleCapabilities,
    PanelContext,
    PanelRegistry,
    PanelResult,
    get_registry,
    register,
)

# ── Panel module imports (registration side effects) ──────────────────────────
# Import order: STATUS slot panels first, then ACT, then EXPLORE.
# Each import triggers that module's register() call at the bottom of the file.
# Changing this order does not change layout order (panels sort by (slot, order, id)).

# STATUS slot
from . import _kpi_strip  # noqa: F401 — registration side effect (order=10: KPI hero tiles)
from . import _health  # noqa: F401 — registration side effect (order=20: Runtime Health)

# ACT slot
from . import _attention  # noqa: F401 — registration side effect (order=10: Attention Queue)
from . import _trends  # noqa: F401 — registration side effect (order=20: three-axis trends)
from . import _recommendations  # noqa: F401 — registration side effect (order=30: savings)

# EXPLORE slot
from . import _fleet_status  # noqa: F401 — registration side effect (order=10: fleet-status, MUST 15)
from . import _fleet_overview  # noqa: F401 — registration side effect (order=20: #636 placeholder)

# MONITOR slots (spec/56 §6 — amends spec/52 §16 slot set)
from . import _monitor_summary  # noqa: F401 — registration side effect (order=10: status-count bar)
from . import _monitor_roster  # noqa: F401 — registration side effect (order=10: entity list/cards)

# AGENT-TAB slot (spec/57 §3 — per-agent detail telemetry tabs)
from . import _agent_tabs  # noqa: F401 — registration side effect (8 tabs: overview/cost/activity/quality/memory/goals/dreaming/efficiency)

__all__ = [
    "ConsoleCapabilities",
    "PanelContext",
    "PanelRegistry",
    "PanelResult",
    "get_registry",
    "register",
]

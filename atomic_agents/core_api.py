"""The core<->fleet contract surface (TENSIONS T17 / T7).

This is NOT a broadly-advertised public API for arbitrary third-party
callers — it is the narrow, internal-stable surface, versioned together
with core, that lets fleet-shaped tooling (``advisor/``, ``dashboard/``,
the ``manage/`` verbs — destined to extract into a separate
``fair-copy-fleet`` package per TENSIONS T17) reach a handful of core
helpers without importing core's private (leading-underscore) modules
directly.

Six names, nothing else:
    atomic_write        -- atomic (temp + fsync + rename) file write
    safe_resolve_under  -- path-traversal-safe resolve under a root
    get_agents_root     -- resolve the configured agents-root directory
    parse_model_md      -- parse an agent's model.md into a dict
    calc_cost           -- USD cost for one LLM call
    get_model_rates     -- USD input/output rates for one model id (T7)

``get_model_rates()`` replaces direct access to ``_costs.PRICING`` at this
seam. The price table itself stays private to ``_costs.py`` — it is never
exported here as a dict, so a price edit is never an API break (TENSIONS
T7). Callers get a defensive copy back; mutating the result never mutates
the internal table.

Anything not listed in ``__all__`` is not part of this contract and may
change without notice — including the private modules this file re-exports
from, which core is still free to evolve internally.
"""

from __future__ import annotations

from ._costs import calc_cost, get_model_rates
from ._io import atomic_write, safe_resolve_under
from ._model import parse_model_md
from ._platform import get_agents_root

__all__ = [
    "atomic_write",
    "safe_resolve_under",
    "get_agents_root",
    "parse_model_md",
    "calc_cost",
    "get_model_rates",
]

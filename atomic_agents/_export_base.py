"""Leaf module holding the ``ExportableResult`` marker base class (spec/40).

This is a dependency-free leaf so BOTH ``export/types.py`` and
``goal/types.py`` can import ``ExportableResult`` without forming a circular
import. ``goal/types.py`` cannot import from ``export/types.py`` (that would
cycle through ``goal/__init__.py``); it imports the base from here instead so
``GoalExport`` can subclass ``ExportableResult`` like every other export type.

Importers:
  - ``export/types.py`` re-exports ``ExportableResult`` (public surface unchanged).
  - ``goal/types.py`` imports it directly so ``GoalExport(ExportableResult)``.
"""

from __future__ import annotations


class ExportableResult:
    """Marker base class for all canonical export objects.

    Backends return subclasses of ``ExportableResult``; the renderer
    dispatches on the concrete type to produce on-disk bytes.

    Note: this is NOT a dataclass itself — subclasses define their own
    fields. The base class exists solely for static-type narrowing and
    ``isinstance`` dispatch in the renderer.
    """

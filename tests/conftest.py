"""Project-wide pytest fixtures.

The lone autouse fixture here clears the
``_jsonschema_warned_agents`` set that ``_revise._warn_jsonschema_gap_once``
keeps as module-level state. Without this, one test asserting "first
call warns" sees nondeterministic flakes when another test in the same
session already touched validation for the same agent_name.
"""

from __future__ import annotations

import pytest

from atomic_agents.judge import _revise


@pytest.fixture(autouse=True)
def _clear_jsonschema_warned_agents():
    _revise._jsonschema_warned_agents.clear()
    yield
    _revise._jsonschema_warned_agents.clear()


@pytest.fixture(autouse=True)
def _agents_root_env(monkeypatch, tmp_path_factory):
    """Tests run without ~/.config/data-roots.env; give them a root.

    Tests that need the variable unset call ``monkeypatch.delenv`` themselves.
    """
    monkeypatch.setenv(
        "ATOMIC_AGENTS_ROOT", str(tmp_path_factory.mktemp("agents_root"))
    )
    # Never read the real ~/.config/data-roots.env during tests.
    monkeypatch.setenv(
        "DATA_ROOTS_ENV", str(tmp_path_factory.mktemp("noenv") / "absent.env")
    )

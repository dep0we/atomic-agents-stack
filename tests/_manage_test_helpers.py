"""Shared test helpers for the manage-layer test modules (spec/55 #609/#624/#709/#710).

Used by ``tests/test_manage_govern.py`` (govern-specific behavior) and
``tests/test_manage_spine.py`` (verb-parametrized SPINE conformance — the
lock/agent_busy, fail-closed, five-step ordering, snapshot, audit, and
exit-code-ladder guarantees every write verb inherits from the hoisted
``atomic_agents.manage._routine`` helper).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock


def make_agent_dir(tmp_path: Path, agent_name: str = "myagent") -> Path:
    """Create a minimal agent directory with model.md (required by registry MUST-3)."""
    agent_dir = tmp_path / agent_name
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text(
        "# Model\n\n## Default model\n\nclaude-3-5-haiku-20241022\n"
    )
    return agent_dir


def canonical_governance_md(agent_name: str = "test-agent") -> str:
    """Render the canonical governance.md stub via the shared renderer."""
    from atomic_agents.init import render_governance_stub

    return render_governance_stub(agent_name)


def make_governance_md(agent_dir: Path, content: str | None = None) -> Path:
    """Write governance.md to agent_dir. Uses canonical template if content is None."""
    gov_path = agent_dir / "governance.md"
    if content is None:
        content = canonical_governance_md(agent_dir.name)
    gov_path.write_text(content, encoding="utf-8")
    return gov_path


def make_govern_args(
    agent: str,
    agents_root: Path,
    set_fields: list[str] | None = None,
    show: bool = False,
    dry_run: bool = False,
    yes: bool = True,  # default True so tests don't block on TTY
    use_json: bool = False,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    set_json: list[str] | None = None,
    restore: str | None = None,
    list_snapshots: bool = False,
) -> Any:
    """Build a minimal argparse-like namespace for run_govern().

    Every optional flag is explicitly set (never left to MagicMock's
    auto-attribute default) — a bare unset MagicMock attribute reads as
    truthy, which would spuriously trip the list-mutation refusal or the
    single-primary-action refusal (#710).
    """
    ns = MagicMock()
    ns.manage_verb = "govern"  # so the namespace also works with run_manage()
    ns.agent = agent
    ns.set = set_fields
    ns.show = show
    ns.dry_run = dry_run
    ns.yes = yes
    ns.json = use_json
    ns.add = add
    ns.remove = remove
    ns.set_json = set_json
    ns.restore = restore
    ns.list_snapshots = list_snapshots
    ns.agents_root = str(agents_root)
    return ns


def collect_jsonl(log_dir: Path) -> list[dict]:
    """Read every JSONL record under ``log_dir`` (recursive — FilesystemLogBackend
    nests ``YYYY-MM/YYYY-MM-DD.jsonl``), sorted by file path for determinism.
    """
    records: list[dict] = []
    if not log_dir.is_dir():
        return records
    for f in sorted(log_dir.rglob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def get_fleet_log_dir(agents_root: Path) -> Path:
    return agents_root / "_manage" / "log"

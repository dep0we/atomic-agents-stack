"""atomic_agents.init -- interactive scaffold wizard for new agents.

Scaffold a working home-user agent in under 10 minutes via interactive Q&A
or --from-template. The wizard guides the operator through seven questions
covering name, mission, scope, autonomy policy, voice, communication
preferences, and hard refusals, then writes seven plain-markdown files to
the agents root. No LLM calls during setup; a short opt-in smoke test at
the end confirms the API key is live.
"""

from __future__ import annotations
from typing import Any


def run_init(args: Any) -> int:
    """Entry point for the `atomic-agents init` subcommand.

    Lazy-imports wizard.py so that importing this package does not pull in
    rich or any other optional dependency at framework import time.
    """
    from . import wizard  # noqa: PLC0415 -- intentional lazy import

    return wizard.run_init(args)


def render_governance_stub(agent_name: str) -> str:
    """Render the canonical governance.md template for agent_name.

    Loads the canonical template at ``atomic_agents/init/templates/governance.md``
    (via importlib.resources) and substitutes ``${agent_name}``. This is the
    renderer ``manage govern`` uses for its create-absent path (spec/55 ruling
    ``create-absent-governance``).

    The "one canonical governance.md shape in the wild" invariant is currently
    held by a byte-identity LINT TEST (``test_all_governance_templates_are_byte_identical``),
    NOT by a single shared call site: ``init``/wizard.py renders the per-agent-type
    copy (``templates/{advisor,researcher,writer}/governance.md``) while this
    function renders the top-level copy, and the lint test asserts all four copies
    are byte-identical. Do NOT delete the per-type copies on the assumption that a
    shared call path enforces the shape — the lint test is what enforces it. (A
    later cleanup MAY route wizard.py through this renderer to make the shared-call
    invariant literally true, per the spec/55 ruling text.)

    Args:
        agent_name: The agent folder name substituted into ``${agent_name}``.

    Returns:
        Rendered governance.md content as a string.
    """
    import string
    from importlib import resources as _resources  # noqa: PLC0415 -- stdlib, not heavy

    template_file = (
        _resources.files("atomic_agents.init") / "templates" / "governance.md"
    )
    raw = template_file.read_text(encoding="utf-8")
    return string.Template(raw).safe_substitute({"agent_name": agent_name})

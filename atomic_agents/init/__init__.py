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

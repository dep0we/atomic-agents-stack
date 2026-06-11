"""Entry point for `python -m atomic_agents.goal`.

Delegates to the CLI main() function defined in atomic_agents._goal_impl.
This preserves the existing CLI interface byte-identically while the
GoalManager implementation lives in _goal_impl.py.

Operator behavior is unchanged:
    python -m atomic_agents.goal status <agent>
    python -m atomic_agents.goal next <agent>
    python -m atomic_agents.goal advance <agent> <sub_goal_id> [--complete]
    python -m atomic_agents.goal abandon <agent> --reason "..."
    python -m atomic_agents.goal complete <agent>
    python -m atomic_agents.goal report <agent>
    python -m atomic_agents.goal dispatch-outcome <agent> <sub_goal_id> --rubric ...

See atomic_agents/_goal_impl.py for the full CLI implementation.
"""

import sys

from atomic_agents._goal_impl import main

if __name__ == "__main__":
    sys.exit(main())

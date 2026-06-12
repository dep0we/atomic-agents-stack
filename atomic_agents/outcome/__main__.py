"""Entry point for `python -m atomic_agents.outcome`.

Delegates to the CLI main() function defined in atomic_agents.outcome._outcome_impl.
This preserves the existing CLI interface byte-identically while the
OutcomeRunner implementation lives in _outcome_impl.py.

Operator behavior is unchanged:
    python -m atomic_agents.outcome caldwell \\
        --description "Write a Q1 budget summary" \\
        --rubric evals/rubric.md \\
        --max-iterations 3

See atomic_agents/outcome/_outcome_impl.py for the full CLI implementation.
"""

import sys

from atomic_agents.outcome._outcome_impl import main

if __name__ == "__main__":
    sys.exit(main())

"""atomic_agents.manage — the management layer (spec/55 #624).

Write verbs that apply validated, audited, reversible changes to agent config.
Every verb implements the S2 five-step safety routine (validate → preview →
confirm → snapshot+write → audit) and the S3 copilot properties (--json,
--dry-run, --yes). On a TTY without --yes, an interactive confirmation prompt
is issued; pass --yes (or --dry-run) to suppress it for non-interactive use.

Verbs in this arc (PR1):
  govern <agent>  — edit governance.md frontmatter (spec/55 first verb #609)

The module is lazy-imported from cli.py so the base CLI startup for ``run``,
``doctor``, and other commands does not pay the manage module's import cost
(principle #6 / spec/55 note 'keep the base CLI lean').
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def run_manage(args: Any, agents_root: Path) -> int:
    """Dispatch ``atomic-agents manage <verb> ...`` subcommands.

    This is the single lazy-import entry point called from cli.py's
    ``_cmd_manage`` function.

    Args:
        args: parsed argparse namespace (includes args.manage_verb).
        agents_root: resolved fleet root directory.

    Returns:
        Process exit code.
    """
    verb = getattr(args, "manage_verb", None)

    if verb == "govern":
        from .govern import run_govern  # noqa: PLC0415 -- intentional lazy import

        return run_govern(args, agents_root)

    # Unknown verb — should not happen because argparse enforces choices,
    # but guard defensively.
    import sys  # noqa: PLC0415

    print(f"Error: Unknown manage verb {verb!r}", file=sys.stderr)
    return 1

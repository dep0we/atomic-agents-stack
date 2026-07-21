"""atomic_agents.manage — the management layer (spec/55 #624).

Write verbs that apply validated, audited, reversible changes to agent config.
Every verb implements the S2 five-step safety routine (validate → preview →
confirm → snapshot+write → audit) and the S3 copilot properties (--json,
--dry-run, --yes). On a TTY without --yes, an interactive confirmation prompt
is issued; pass --yes (or --dry-run) to suppress it for non-interactive use.

Verbs in this arc:
  govern <agent> --set ...        — edit governance.md frontmatter (#609)
  govern <agent> --restore <id>   — roll back governance.md to a snapshot (#710)
  govern <agent> --show/--list-snapshots — read-only (never touch the manage lease)
  set-model <agent> --model ...   — edit model.md's Default model field (#726)
  set-model <agent> --restore <id> — roll back model.md to a snapshot (#726)
  set-model <agent> --show/--list-snapshots — read-only (never touch the manage lease)
  apply-rec <rec-id>              — apply a Fleet Console savings_cost
                                     recommendation by delegating into
                                     set-model's write routine (#727)

The module is lazy-imported from cli.py so the base CLI startup for ``run``,
``doctor``, and other commands does not pay the manage module's import cost
(principle #6 / spec/55 note 'keep the base CLI lean').

Spine-wide concurrency (spec/55 M11, #709): ``run_manage`` is the SINGLE
central catch point for ``ManageAgentBusyError`` / ``ManageLockUnavailableError``
— every write verb's ``run_managed_write`` call lets these propagate UNCAUGHT
so they are caught HERE, once, not per-verb. This is deliberate (not
incidental): a per-verb catch site risks either double-emission (a local
catch inside a callback also prints before the exception propagates) or a
missed catch (a busy-lock exception raised from inside a nested callback
slips past a too-narrowly-scoped local catch). Catching centrally means every
verb — govern and any future set-model/apply-rec — gets the SAME agent_busy /
lock_backend_unavailable refusal shape for free, and the JSON error is
emitted EXACTLY ONCE per invocation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def run_manage(args: Any, agents_root: Path) -> int:
    """Dispatch ``atomic-agents manage <verb> ...`` subcommands.

    This is the single lazy-import entry point called from cli.py's
    ``_cmd_manage`` function, and the single central catch site for the
    spine-wide manage-lease exceptions (spec/55 M11).

    Args:
        args: parsed argparse namespace (includes args.manage_verb).
        agents_root: resolved fleet root directory.

    Returns:
        Process exit code.
    """
    verb = getattr(args, "manage_verb", None)
    use_json = getattr(args, "json", False)

    # Lazy import — these exceptions live in the manage package, which cli.py
    # never imports eagerly (principle #6).
    from .exceptions import ManageAgentBusyError, ManageLockUnavailableError

    try:
        if verb == "govern":
            from .govern import run_govern  # noqa: PLC0415 -- intentional lazy import

            return run_govern(args, agents_root)

        if verb == "set-model":
            from .set_model import run_set_model  # noqa: PLC0415 -- intentional lazy import

            return run_set_model(args, agents_root)

        if verb == "apply-rec":
            from .apply_rec import run_apply_rec  # noqa: PLC0415 -- intentional lazy import

            return run_apply_rec(args, agents_root)

        # Unknown verb — should not happen because argparse enforces choices,
        # but guard defensively.
        print(f"Error: Unknown manage verb {verb!r}", file=sys.stderr)
        return 1
    except (ManageAgentBusyError, ManageLockUnavailableError) as exc:
        # Spec/55 M11 spine-wide concurrency MUST — caught CENTRALLY, not
        # per-verb. Per M8's pinned status vocabulary, a refusal (including
        # agent_busy) does NOT emit a management RunRecord — contention is
        # visible ONLY via this structured refusal + exit 1, never via an
        # audit line (a RunRecord existing implies the write was applied).
        if use_json:
            print(
                json.dumps(
                    {"ok": False, "error_type": exc.error_type, "reason": str(exc)},
                    indent=2,
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

"""Command-registration mechanism for the ``atomic-agents`` CLI (#736 Phase 2a).

Every top-level subcommand (``run``, ``doctor``, ``manage``, ...) contributes
its argparse wiring plus its dispatch handler through the ``CliCommand``
contract defined here, instead of being hardcoded into ``cli.py`` as an
inline ``sub.add_parser(...)`` block paired with an ``if args.cmd == ...``
branch in ``main()``. ``cli.py`` builds its command table by iterating a
list of registered commands.

Design choice (flagged for review — see #736 report): built-in commands are
built directly as a plain list in ``cli.py`` (``_builtin_commands()``); no
plugin machinery is needed for code that already lives in this package (the
list is rebuilt fresh on every ``main()`` call, not frozen at import time,
so ``unittest.mock.patch("atomic_agents.cli._cmd_x", ...)`` still works the
way it did before this refactor). The part that is load-bearing for the
eventual core/extension split is
out-of-tree discovery — a SEPARATE installed package (e.g. a future
``atomic-agents-fleet`` distribution) contributing a command WITHOUT
``cli.py`` importing it by name. This module resolves that with Python's
standard plugin mechanism, ``importlib.metadata`` entry points, group
``atomic_agents.cli_commands``:

    # pyproject.toml of an out-of-tree extension package
    [project.entry-points."atomic_agents.cli_commands"]
    my-command = "my_extension.cli:COMMAND"

where ``my_extension.cli:COMMAND`` resolves (via ``EntryPoint.load()``) to a
module-level ``CliCommand`` instance. ``discover_commands`` only imports
``my_extension`` if that entry point is present in the environment (i.e. the
extension is actually installed) — core never references the extension
module by name, so nothing breaks when it isn't installed.

The alternative considered and rejected: a bare mutable registry list that
extensions "append to." That shape works when the appending code runs in the
same process *after* being imported somehow — but the only way to get an
out-of-tree package's registration code to run without ``cli.py`` importing
it by name is some other discovery mechanism, which just reinvents entry
points by hand (e.g. scanning a directory of plugin modules, or reading a
config file listing dotted paths to import). Entry points are the standard,
already-battled-tested solution for exactly this shape of plugin discovery
(the same mechanism ``pytest11`` and Flask CLI plugins use), so there is no
reason to hand-roll a weaker version of it.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Callable

# The entry-point group name out-of-tree extensions register commands under.
# Not used by anything in-tree today (all built-in commands are wired
# directly in cli.py); this constant exists so a future extension package
# and this module agree on the group name without either side guessing.
ENTRY_POINT_GROUP = "atomic_agents.cli_commands"


@dataclass(frozen=True)
class CliCommand:
    """One top-level ``atomic-agents`` subcommand's argparse + dispatch contract.

    Attributes:
        name: the subcommand token as typed on the command line (e.g. ``"run"``,
            ``"mcp-registry"``). Must match the ``sub.add_parser(name, ...)``
            call made inside ``register``.
        register: called once per ``main()`` invocation with the top-level
            ``argparse._SubParsersAction``. MUST add exactly one subparser
            named ``name`` — including any nested sub-subparsers (e.g.
            ``persona show``, ``manage govern``) — and MUST NOT import any
            fleet/extension module to do so (heavy backends are loaded lazily
            inside ``dispatch``, never at registration time).
        dispatch: called once with the parsed ``argparse.Namespace`` after
            ``parser.parse_args()`` selects this command. Returns the process
            exit code. Each command owns its own path resolution and error
            handling — there is no shared post-parse special-casing left in
            ``main()``.
    """

    name: str
    register: Callable[["argparse._SubParsersAction"], None]
    dispatch: Callable[[argparse.Namespace], int]


def discover_commands(builtin: list[CliCommand]) -> list[CliCommand]:
    """Merge built-in commands with any entry-point-registered extensions.

    Built-ins always win on a name collision — an out-of-tree extension
    cannot shadow a core command. A colliding or malformed extension entry
    point is skipped with a stderr warning (never silently dropped without a
    trace, never allowed to clobber core behavior, never allowed to crash
    the CLI for every other command).
    """
    commands: list[CliCommand] = list(builtin)
    # Track built-in names separately from plugin-contributed names so a
    # collision diagnostic can say WHICH kind of command was shadowed (a
    # built-in vs another already-accepted plugin) rather than always
    # blaming a built-in.
    builtin_names = {c.name for c in commands}
    plugin_names: set[str] = set()
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as e:  # noqa: BLE001 -- discovery must never break the CLI
        print(f"warning: CLI command plugin discovery failed: {e}", file=sys.stderr)
        return commands
    for ep in eps:
        try:
            command = ep.load()
        except Exception as e:  # noqa: BLE001 -- one bad plugin must not break the rest
            print(
                f"warning: failed to load CLI command plugin {ep.name!r}: {e}",
                file=sys.stderr,
            )
            continue
        if not isinstance(command, CliCommand):
            print(
                f"warning: CLI command plugin {ep.name!r} did not resolve to a "
                f"CliCommand instance; skipping",
                file=sys.stderr,
            )
            continue
        if command.name in builtin_names:
            print(
                f"warning: CLI command plugin {ep.name!r} registers subcommand "
                f"{command.name!r}, which is already a built-in command; "
                f"ignoring the plugin registration",
                file=sys.stderr,
            )
            continue
        if command.name in plugin_names:
            print(
                f"warning: CLI command plugin {ep.name!r} registers subcommand "
                f"{command.name!r}, which another plugin already registered; "
                f"ignoring the later plugin registration",
                file=sys.stderr,
            )
            continue
        commands.append(command)
        plugin_names.add(command.name)
    return commands

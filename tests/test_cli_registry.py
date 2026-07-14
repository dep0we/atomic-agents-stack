"""Tests for the CLI command-registration mechanism (#736).

Covers atomic_agents/_cli_registry.py's discover_commands loop -- the part
that merges built-in commands with out-of-tree entry-point-registered
extension commands -- and cli.py's register()-failure isolation in main().

These exercise the failure modes a real out-of-tree plugin could hit:
    (a) plugin raises on load()               -> skipped + warns, CLI still works
    (b) plugin resolves to a non-CliCommand   -> skipped + warns
    (c) top-level entry_points() raises        -> degrades to built-ins-only
    (d) plugin name collides with a built-in   -> built-in wins, plugin rejected
    (d2) plugin name collides with another plugin -> first plugin wins
    (e) plugin register() raises               -> skipped, other commands still work
    (f) well-formed plugin                     -> command discovered + dispatchable

All use a monkeypatched `entry_points`, so no package actually has to be
installed to register an entry point.
"""

from __future__ import annotations

import argparse

import pytest

from atomic_agents import _cli_registry
from atomic_agents._cli_registry import CliCommand, discover_commands


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint.

    Only `.name` and `.load()` are used by discover_commands.
    """

    def __init__(self, name, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _stub_command(name: str, *, register=None, dispatch=None) -> CliCommand:
    """A well-formed CliCommand whose register/dispatch default to no-ops."""

    def _register(sub: argparse._SubParsersAction) -> None:
        sub.add_parser(name, help=f"stub {name}")

    return CliCommand(
        name=name,
        register=register or _register,
        dispatch=dispatch or (lambda args: 0),
    )


def _patch_entry_points(monkeypatch, eps):
    """Make _cli_registry.entry_points(group=...) return `eps` (a list)."""

    def fake_entry_points(*, group=None):
        assert group == _cli_registry.ENTRY_POINT_GROUP
        return list(eps)

    monkeypatch.setattr(_cli_registry, "entry_points", fake_entry_points)


BUILTIN = _stub_command("run")  # a name that IS a real built-in


# ---------------------------------------------------------------------------
# (f) well-formed plugin is discovered
# ---------------------------------------------------------------------------


def test_wellformed_plugin_is_discovered(monkeypatch):
    plugin = _stub_command("fleet-thing")
    _patch_entry_points(monkeypatch, [FakeEntryPoint("fleet-thing", lambda: plugin)])

    commands = discover_commands([BUILTIN])
    names = [c.name for c in commands]

    assert "run" in names  # built-in preserved
    assert "fleet-thing" in names  # plugin appended
    # It is the exact object we contributed, and it dispatches.
    contributed = next(c for c in commands if c.name == "fleet-thing")
    assert contributed is plugin
    assert contributed.dispatch(argparse.Namespace()) == 0


# ---------------------------------------------------------------------------
# (a) plugin raises on load -> skipped, others survive
# ---------------------------------------------------------------------------


def test_plugin_raising_on_load_is_skipped(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("plugin import blew up")

    good = _stub_command("good-plugin")
    _patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("bad-plugin", _boom),
            FakeEntryPoint("good-plugin", lambda: good),
        ],
    )

    commands = discover_commands([BUILTIN])
    names = [c.name for c in commands]

    assert names == ["run", "good-plugin"]  # bad one dropped, good one kept
    err = capsys.readouterr().err
    assert "bad-plugin" in err
    assert "failed to load" in err


# ---------------------------------------------------------------------------
# (b) plugin resolves to a non-CliCommand -> skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "not a command",
        123,
        object(),
        lambda args: 0,  # a bare callable, not a CliCommand
    ],
)
def test_plugin_not_a_clicommand_is_skipped(monkeypatch, capsys, bad_value):
    _patch_entry_points(
        monkeypatch, [FakeEntryPoint("weird-plugin", lambda: bad_value)]
    )

    commands = discover_commands([BUILTIN])

    assert [c.name for c in commands] == ["run"]
    err = capsys.readouterr().err
    assert "weird-plugin" in err
    assert "did not resolve to a" in err


# ---------------------------------------------------------------------------
# (c) top-level entry_points() raises -> degrade to built-ins only
# ---------------------------------------------------------------------------


def test_entry_points_call_raising_degrades_to_builtins(monkeypatch, capsys):
    def _boom(*, group=None):
        raise RuntimeError("metadata backend unavailable")

    monkeypatch.setattr(_cli_registry, "entry_points", _boom)

    commands = discover_commands([BUILTIN])

    assert [c.name for c in commands] == ["run"]  # built-ins intact
    err = capsys.readouterr().err
    assert "plugin discovery failed" in err


# ---------------------------------------------------------------------------
# (d) plugin name collides with a built-in -> built-in wins
# ---------------------------------------------------------------------------


def test_plugin_colliding_with_builtin_is_rejected(monkeypatch, capsys):
    # Plugin tries to register "run" (a built-in name) with a dispatch that
    # would return 99 if it ever won.
    evil = _stub_command("run", dispatch=lambda args: 99)
    _patch_entry_points(monkeypatch, [FakeEntryPoint("evil", lambda: evil)])

    commands = discover_commands([BUILTIN])

    # Exactly one "run", and it is the built-in (dispatch 0), not the plugin.
    runs = [c for c in commands if c.name == "run"]
    assert len(runs) == 1
    assert runs[0] is BUILTIN
    assert runs[0].dispatch(argparse.Namespace()) == 0
    err = capsys.readouterr().err
    assert "already a built-in command" in err


# ---------------------------------------------------------------------------
# (d2) plugin name collides with ANOTHER plugin -> first wins, accurate message
# ---------------------------------------------------------------------------


def test_two_plugins_same_name_first_wins_with_plugin_message(monkeypatch, capsys):
    first = _stub_command("dupe", dispatch=lambda args: 1)
    second = _stub_command("dupe", dispatch=lambda args: 2)
    _patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("first-plugin", lambda: first),
            FakeEntryPoint("second-plugin", lambda: second),
        ],
    )

    commands = discover_commands([BUILTIN])

    dupes = [c for c in commands if c.name == "dupe"]
    assert len(dupes) == 1
    assert dupes[0] is first  # first registration wins
    err = capsys.readouterr().err
    # Finding #5: message must name the plugin collision, NOT claim built-in.
    assert "another plugin already registered" in err
    assert "already a built-in command" not in err


# ---------------------------------------------------------------------------
# (e) plugin register() raises -> isolated in main()'s loop, others survive
# ---------------------------------------------------------------------------


def test_plugin_register_failure_is_isolated_in_main(monkeypatch, capsys):
    """A plugin whose register() raises must not brick the CLI: built-in
    commands still register and dispatch. Exercised through cli.main() so the
    real registration loop (not just discover_commands) is under test."""
    from atomic_agents import cli

    def _explode(sub: argparse._SubParsersAction) -> None:
        raise ValueError("register() exploded")

    broken = CliCommand(
        name="broken-fleet-cmd", register=_explode, dispatch=lambda args: 0
    )
    _patch_entry_points(monkeypatch, [FakeEntryPoint("broken", lambda: broken)])

    # A built-in command must still work end-to-end despite the broken plugin.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["doctor", "--help"])
    assert exc_info.value.code == 0  # doctor --help still prints and exits 0

    err = capsys.readouterr().err
    assert "broken-fleet-cmd" in err
    assert "failed to register" in err


def test_builtin_register_failure_reraises_loud(monkeypatch):
    """A BUILT-IN command's register() failure is our own bug and must
    re-raise (not be silently skipped) -- the documented choice for finding
    #1. Simulated by monkeypatching a built-in's registrar to raise."""
    from atomic_agents import cli

    # No plugins; force a built-in's register() to raise.
    _patch_entry_points(monkeypatch, [])

    def _explode(sub: argparse._SubParsersAction) -> None:
        raise RuntimeError("built-in registrar bug")

    # Capture the REAL builder before patching so the replacement doesn't
    # recurse into itself.
    real_builtins = cli._builtin_commands

    def _builtins_with_broken_doctor():
        cmds = []
        for c in real_builtins():
            if c.name == "doctor":
                cmds.append(CliCommand("doctor", _explode, c.dispatch))
            else:
                cmds.append(c)
        return cmds

    monkeypatch.setattr(cli, "_builtin_commands", _builtins_with_broken_doctor)

    with pytest.raises(RuntimeError, match="built-in registrar bug"):
        cli.main(["doctor", "--help"])


# ---------------------------------------------------------------------------
# No entry points at all -> built-ins unchanged (the common real-world case)
# ---------------------------------------------------------------------------


def test_no_entry_points_returns_builtins_unchanged(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    commands = discover_commands([BUILTIN])
    assert commands == [BUILTIN]

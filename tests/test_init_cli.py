"""Tests for the atomic-agents init CLI subparser and dispatch.

Coverage strategy: argparse-level shape (args parsed correctly), dispatch routing
(init flows through _cmd_init before the agents_root resolution block), lazy-import
verification (wizard module not loaded unless init is dispatched), and exit-code
threading.

The wizard's actual logic lives in atomic_agents.init.wizard -- those tests are in
test_init_wizard.py. This file tests the CLI plumbing only.

PR 2 additions:
    - --list-templates enumerates all three template names.
    - --from-template researcher and writer are accepted by argparse.
    - Invalid --from-template values are still rejected (argparse exit 2).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from atomic_agents import cli as cli_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_init_noop(args):
    """Stand-in for atomic_agents.init.run_init that records the args it receives."""
    return 0


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def test_init_subparser_registered_exits_zero(capsys):
    """init --help prints init-specific text and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["init", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "init" in out


def test_init_help_mentions_from_template_flag(capsys):
    """init --help output lists the --from-template flag."""
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["init", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--from-template" in out


def test_init_help_mentions_list_templates_flag(capsys):
    """init --help output lists the --list-templates flag."""
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["init", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--list-templates" in out


def test_init_help_mentions_advisor_choice(capsys):
    """init --help shows 'advisor' as a valid --from-template choice."""
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["init", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "advisor" in out


# ---------------------------------------------------------------------------
# Argparse validation
# ---------------------------------------------------------------------------


def test_init_invalid_template_choice_exits_2(capsys):
    """Passing an unknown --from-template value causes argparse to exit 2."""
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["init", "foo", "--from-template", "unknown-template"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Argument parsing + dispatch
# ---------------------------------------------------------------------------


def test_init_list_templates_sets_flag(monkeypatch):
    """--list-templates parses to args.list_templates=True and agent_name=None."""
    captured = {}

    def fake_run_init(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("atomic_agents.init.run_init", fake_run_init)
    exit_code = cli_module.main(["init", "--list-templates"])
    assert exit_code == 0
    assert captured["args"].list_templates is True
    assert captured["args"].agent_name is None


def test_init_from_template_advisor_sets_args(monkeypatch):
    """--from-template advisor sets from_template='advisor' and agent_name='my-agent'."""
    captured = {}

    def fake_run_init(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("atomic_agents.init.run_init", fake_run_init)
    cli_module.main(["init", "my-agent", "--from-template", "advisor"])
    assert captured["args"].from_template == "advisor"
    assert captured["args"].agent_name == "my-agent"


def test_init_agents_root_flag_passes_through(monkeypatch):
    """--agents-root value is threaded through to run_init args unchanged."""
    captured = {}

    def fake_run_init(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("atomic_agents.init.run_init", fake_run_init)
    cli_module.main(["init", "--agents-root", "/tmp/test-vault"])
    assert captured["args"].agents_root == "/tmp/test-vault"


def test_init_missing_agent_name_sets_none(monkeypatch):
    """Calling init without an agent_name leaves args.agent_name as None."""
    captured = {}

    def fake_run_init(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("atomic_agents.init.run_init", fake_run_init)
    cli_module.main(["init"])
    assert captured["args"].agent_name is None


# ---------------------------------------------------------------------------
# Exit code threading
# ---------------------------------------------------------------------------


def test_init_returns_run_init_exit_code(monkeypatch):
    """cli.main forwards run_init's return value as the process exit code."""
    monkeypatch.setattr("atomic_agents.init.run_init", lambda args: 42)
    result = cli_module.main(["init", "foo"])
    assert result == 42


# ---------------------------------------------------------------------------
# Dispatch ordering
# ---------------------------------------------------------------------------


def test_init_dispatch_before_agents_root_resolution(monkeypatch):
    """init is dispatched before the agents_root resolution block.

    The agents_root block (lines 441-444 of cli.py) calls get_agents_root(),
    which can raise when ATOMIC_AGENTS_ROOT is unset. We verify that init
    never reaches that block by poisoning get_agents_root and confirming
    that an init call still succeeds.
    """

    def poisoned_get_agents_root():
        raise RuntimeError("agents_root resolution must not run for init")

    monkeypatch.setattr("atomic_agents.cli.get_agents_root", poisoned_get_agents_root)
    monkeypatch.setattr("atomic_agents.init.run_init", lambda args: 0)

    # Should NOT raise because init is handled before the poisoned path.
    result = cli_module.main(["init", "my-agent"])
    assert result == 0


# ---------------------------------------------------------------------------
# Lazy-import discipline
# ---------------------------------------------------------------------------


def test_cli_module_does_not_import_wizard_at_module_top():
    """The cli module MUST NOT have `from .init import wizard` at module top.

    The lazy-import pattern keeps rich (and any other wizard-only deps) out of
    the import path for non-init invocations. This test verifies the source
    rather than runtime state because runtime sys.modules is unreliable in
    pytest (other tests may have imported wizard already).
    """
    cli_source = Path(cli_module.__file__).read_text(encoding="utf-8")
    # Allow `from .init import` inside a function body (lazy), but NOT at module top.
    # Module-top imports live before the first `def` (or class) declaration.
    first_def = cli_source.find("\ndef ")
    if first_def == -1:
        first_def = cli_source.find("\nclass ")
    module_top = cli_source[:first_def] if first_def > 0 else cli_source
    assert "from .init" not in module_top, (
        "cli.py imports from .init at module top; lazy-import pattern broken"
    )
    assert "import atomic_agents.init" not in module_top, (
        "cli.py imports atomic_agents.init at module top; lazy-import pattern broken"
    )


# ---------------------------------------------------------------------------
# Module docstring
# ---------------------------------------------------------------------------


def test_init_mentioned_in_cli_module_docstring():
    """The cli.py module docstring's Usage section references 'atomic-agents init'."""
    docstring = cli_module.__doc__ or ""
    assert "atomic-agents init" in docstring


# ---------------------------------------------------------------------------
# PR 2: --list-templates enumerates all three templates
# ---------------------------------------------------------------------------


def test_init_list_templates_enumerates_all_three():
    """_cmd_list_templates prints all three template names to the console.

    Called directly on the wizard function so no CLI dispatch is required.
    All three names (advisor, researcher, writer) must appear in the output.
    """
    import io

    from rich.console import Console

    from atomic_agents.init.wizard import _cmd_list_templates

    buf = io.StringIO()
    console = Console(file=buf, highlight=False)
    exit_code = _cmd_list_templates(console)

    assert exit_code == 0

    output = buf.getvalue()
    assert "advisor" in output, (
        f"'advisor' not found in list-templates output:\n{output}"
    )
    assert "researcher" in output, (
        f"'researcher' not found in list-templates output:\n{output}"
    )
    assert "writer" in output, f"'writer' not found in list-templates output:\n{output}"


# ---------------------------------------------------------------------------
# PR 2: researcher and writer choices accepted by argparse
# ---------------------------------------------------------------------------


def test_init_from_template_researcher_choice_accepted(monkeypatch):
    """--from-template researcher is accepted by argparse (choices list includes it)."""
    captured = {}

    def fake_run_init(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("atomic_agents.init.run_init", fake_run_init)
    cli_module.main(["init", "my-r", "--from-template", "researcher"])
    assert captured["args"].from_template == "researcher"
    assert captured["args"].agent_name == "my-r"


def test_init_from_template_writer_choice_accepted(monkeypatch):
    """--from-template writer is accepted by argparse (choices list includes it)."""
    captured = {}

    def fake_run_init(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("atomic_agents.init.run_init", fake_run_init)
    cli_module.main(["init", "my-w", "--from-template", "writer"])
    assert captured["args"].from_template == "writer"
    assert captured["args"].agent_name == "my-w"


# ---------------------------------------------------------------------------
# PR 2: invalid template names still rejected by argparse
# ---------------------------------------------------------------------------


def test_init_from_template_invalid_choice_still_rejected():
    """Passing an unrecognised --from-template value causes argparse to exit 2.

    This protects against accidentally opening the choices list to arbitrary
    template names. The existing test_init_invalid_template_choice_exits_2 covers
    the same contract; this test documents the PR-2-specific regression expectation.
    """
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["init", "my-x", "--from-template", "unknown-template"])
    assert exc_info.value.code == 2

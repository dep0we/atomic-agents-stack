"""Tests for atomic_agents.init.wizard.

Coverage areas:
  A. Q1 agent_name validation
  B. Q4 autonomy preset + customize
  C. Non-TTY guard (MUST 2)
  D. API key pre-flight (MUST 7)
  E. Persona-backend warning (MUST 6)
  F. Collision recovery -- backup+restore (MUST 5)
  G. OSError translation (MUST 3, T-EX1)
  H. Template variable substitution (MUST 13)
  I. agents_root single-resolution (M9, H6)

Filesystem isolation: every test that touches the agent vault uses tmp_path.
Mocking: monkeypatch only; no unittest.mock decorators.
"""

from __future__ import annotations

import errno
import sys
import types
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from atomic_agents.init import constants as C
from atomic_agents.init import wizard as W


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeConsole:
    """Minimal Console stand-in that captures print calls."""

    def __init__(self):
        self.out = StringIO()
        self.is_dumb_terminal = False

    def print(self, *args, **kwargs):
        text = " ".join(str(a) for a in args)
        self.out.write(text + "\n")


def _make_args(
    *,
    agent_name: str | None = None,
    from_template: str | None = None,
    list_templates: bool = False,
    agents_root: str | None = None,
) -> Any:
    """Build a minimal argparse-like namespace for run_init."""
    ns = types.SimpleNamespace()
    ns.agent_name = agent_name
    ns.from_template = from_template
    ns.list_templates = list_templates
    ns.agents_root = agents_root
    return ns


def _prompt_sequence(*answers):
    """Return a Prompt duck-type that yields answers in order."""
    answer_iter = iter(answers)

    class FakePrompt:
        @classmethod
        def ask(cls, _prompt, choices=None, default=None, console=None, **kwargs):
            try:
                return next(answer_iter)
            except StopIteration:
                return default or ""

    return FakePrompt


def _confirm_factory(response: bool):
    """Return a Confirm duck-type that always answers response."""

    class FakeConfirm:
        @classmethod
        def ask(cls, _prompt, console=None, default=None, **kwargs):
            return response

    return FakeConfirm


# ---------------------------------------------------------------------------
# A. Q1 agent_name validation
# ---------------------------------------------------------------------------


def test_q1_valid_name_accepted():
    """_ask_q1_name returns the name unchanged when it passes all checks."""
    console = _FakeConsole()
    Prompt = _prompt_sequence("my-agent")
    result = W._ask_q1_name(console, Prompt)
    assert result == "my-agent"


def test_q1_path_traversal_refused():
    """../foo fails AGENT_NAME_REGEX and triggers a re-prompt."""
    console = _FakeConsole()
    # First answer fails; second answer is valid.
    Prompt = _prompt_sequence("../foo", "good-name")
    result = W._ask_q1_name(console, Prompt)
    assert result == "good-name"
    assert (
        "letters" in console.out.getvalue().lower()
        or "charset" in console.out.getvalue().lower()
        or "names" in console.out.getvalue().lower()
    )


def test_q1_reserved_name_refused():
    """'doctor' is in RESERVED_AGENT_NAMES; wizard re-prompts with MSG_INVALID_NAME_RESERVED."""
    assert "doctor" in C.RESERVED_AGENT_NAMES, "pre-condition: 'doctor' is reserved"
    console = _FakeConsole()
    Prompt = _prompt_sequence("doctor", "my-advisor")
    result = W._ask_q1_name(console, Prompt)
    assert result == "my-advisor"
    assert C.MSG_INVALID_NAME_RESERVED in console.out.getvalue()


def test_q1_empty_name_re_prompts():
    """An empty string triggers re-prompt; the loop exits on the valid answer."""
    console = _FakeConsole()
    Prompt = _prompt_sequence("", "agent-alpha")
    result = W._ask_q1_name(console, Prompt)
    assert result == "agent-alpha"


def test_q1_leading_dash_refused():
    """-foo has a leading dash and fails AGENT_NAME_REGEX."""
    console = _FakeConsole()
    Prompt = _prompt_sequence("-foo", "valid-agent")
    result = W._ask_q1_name(console, Prompt)
    assert result == "valid-agent"
    # Charset error message should have been printed.
    output = console.out.getvalue()
    assert C.MSG_INVALID_NAME_CHARSET in output


# ---------------------------------------------------------------------------
# B. Q4 autonomy preset + customize
# ---------------------------------------------------------------------------


def _make_q4_table():
    """Return a Table stand-in that swallows add_column / add_row."""

    class FakeTable:
        def __init__(self, **kwargs):
            pass

        def add_column(self, *a, **kw):
            pass

        def add_row(self, *a, **kw):
            pass

    return FakeTable


def test_q4_preset_cautious_returns_correct_dict():
    """Choice '1' returns a copy of AUTONOMY_PRESETS[PRESET_CAUTIOUS]."""
    console = _FakeConsole()
    Prompt = _prompt_sequence("1")
    Table = _make_q4_table()
    policies, label = W._ask_q4_autonomy(console, Prompt, Table)
    assert label == C.PRESET_CAUTIOUS
    assert policies == C.AUTONOMY_PRESETS[C.PRESET_CAUTIOUS]


def test_q4_preset_balanced_returns_correct_dict():
    """Choice '2' returns a copy of AUTONOMY_PRESETS[PRESET_BALANCED]."""
    console = _FakeConsole()
    Prompt = _prompt_sequence("2")
    Table = _make_q4_table()
    policies, label = W._ask_q4_autonomy(console, Prompt, Table)
    assert label == C.PRESET_BALANCED
    assert policies == C.AUTONOMY_PRESETS[C.PRESET_BALANCED]


def test_q4_preset_autonomous_returns_correct_dict():
    """Choice '3' returns a copy of AUTONOMY_PRESETS[PRESET_AUTONOMOUS]."""
    console = _FakeConsole()
    Prompt = _prompt_sequence("3")
    Table = _make_q4_table()
    policies, label = W._ask_q4_autonomy(console, Prompt, Table)
    assert label == C.PRESET_AUTONOMOUS
    assert policies == C.AUTONOMY_PRESETS[C.PRESET_AUTONOMOUS]


def test_q4_customize_iterates_4_classes():
    """Choice '4' enters _customize_autonomy, which iterates over all ACTION_CLASSES."""
    console = _FakeConsole()
    # One answer per action class, all choosing option 1 (bypass).
    num_classes = len(C.ACTION_CLASSES)
    # For Q4 choice prompt + num_classes per-class prompts.
    answers = ["4"] + ["1"] * num_classes
    Prompt = _prompt_sequence(*answers)
    Table = _make_q4_table()
    policies, label = W._ask_q4_autonomy(console, Prompt, Table)
    assert label == C.PRESET_CUSTOMIZE
    assert set(policies.keys()) == set(C.ACTION_CLASSES)
    assert len(policies) == len(C.ACTION_CLASSES)


# ---------------------------------------------------------------------------
# C. Non-TTY guard (MUST 2)
# ---------------------------------------------------------------------------


def test_run_init_non_tty_exits_2_with_message(monkeypatch, tmp_path, capsys):
    """When stdin is not a TTY, run_init returns 2 and writes to stderr."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    args = _make_args(agents_root=str(tmp_path))
    rc = W.run_init(args)
    assert rc == 2
    captured = capsys.readouterr()
    assert (
        "interactive terminal" in captured.err.lower() or C.MSG_NO_TTY in captured.err
    )


def test_run_init_non_tty_does_not_import_rich(monkeypatch, tmp_path):
    """Lazy-import guard: rich must NOT be imported when stdin is not a TTY."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    # Remove rich from sys.modules to reset import state.
    for mod_name in list(sys.modules.keys()):
        if mod_name == "rich" or mod_name.startswith("rich."):
            monkeypatch.delitem(sys.modules, mod_name)

    args = _make_args(agents_root=str(tmp_path))
    W.run_init(args)

    # rich should not have been imported by the non-TTY path.
    assert "rich" not in sys.modules


# ---------------------------------------------------------------------------
# D. API key pre-flight (MUST 7)
# ---------------------------------------------------------------------------


def test_api_key_preflight_uses_get_key(monkeypatch, tmp_path, capsys):
    """When _get_key raises AtomicAgentsError, run_init exits 1 with MSG_NO_API_KEY."""
    from atomic_agents.exceptions import AtomicAgentsError

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _raising_get_key(**kwargs):
        raise AtomicAgentsError("no key configured")

    monkeypatch.setattr("atomic_agents._llm._get_key", _raising_get_key)

    # Stub rich imports used inside run_init (post-TTY gate).
    monkeypatch.setattr(
        "atomic_agents.init.wizard._persona_backend_check",
        lambda *a, **kw: True,
    )

    args = _make_args(agents_root=str(tmp_path))
    rc = W.run_init(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert C.MSG_NO_API_KEY in captured.err


def test_api_key_preflight_passes_when_key_available(monkeypatch, tmp_path):
    """When _get_key returns a fake key, _api_key_preflight returns True."""
    monkeypatch.setattr(
        "atomic_agents._llm._get_key",
        lambda **kwargs: "sk-ant-fake-key",
    )
    result = W._api_key_preflight()
    assert result is True


# ---------------------------------------------------------------------------
# E. Persona-backend warning (MUST 6)
# ---------------------------------------------------------------------------


def test_persona_backend_warning_no_url_env_skipped(monkeypatch):
    """When ATOMIC_AGENTS_PERSONA_BACKEND_URL is unset, warning returns True without prompting."""
    monkeypatch.delenv("ATOMIC_AGENTS_PERSONA_BACKEND_URL", raising=False)

    prompt_called = []

    class WatchConfirm:
        @classmethod
        def ask(cls, *a, **kw):
            prompt_called.append(True)
            return False

    console = _FakeConsole()
    result = W._persona_backend_check(console, WatchConfirm)
    assert result is True
    assert not prompt_called, "Prompt.ask should not be called when no URL is set"


def test_persona_backend_warning_url_set_user_declines(monkeypatch):
    """When URL is set and user declines, _persona_backend_check returns False."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_PERSONA_BACKEND_URL", "https://custom.example.com"
    )
    console = _FakeConsole()
    result = W._persona_backend_check(console, _confirm_factory(False))
    assert result is False


def test_persona_backend_warning_url_set_user_accepts(monkeypatch):
    """When URL is set and user accepts, _persona_backend_check returns True."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_PERSONA_BACKEND_URL", "https://custom.example.com"
    )
    console = _FakeConsole()
    result = W._persona_backend_check(console, _confirm_factory(True))
    assert result is True


# ---------------------------------------------------------------------------
# F. Collision recovery -- backup+restore (MUST 5)
# ---------------------------------------------------------------------------


def test_collision_overwrite_success_rmtrees_backup(tmp_path):
    """Successful overwrite: .bak directory is removed after write_func succeeds."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    (agent_dir / "old-file.txt").write_text("original content")

    new_file_path = agent_dir / "new-file.txt"

    def _write_func():
        agent_dir.mkdir(parents=True, exist_ok=True)
        new_file_path.write_text("new content")

    W._collision_overwrite_backup_restore(agent_dir, _write_func)

    # New file should exist.
    assert new_file_path.exists()
    assert new_file_path.read_text() == "new content"

    # No .bak directory should remain.
    bak_dirs = list(tmp_path.glob("my-agent.bak.*"))
    assert not bak_dirs, f"Backup dirs not cleaned up: {bak_dirs}"


def test_collision_overwrite_failure_restores_backup(tmp_path):
    """Failed overwrite: original content is restored from .bak."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    sentinel = agent_dir / "original.txt"
    sentinel.write_text("preserved content")

    def _failing_write():
        raise OSError(errno.EACCES, "Permission denied", str(agent_dir))

    with pytest.raises(OSError):
        W._collision_overwrite_backup_restore(agent_dir, _failing_write)

    # Original directory should be restored.
    assert agent_dir.exists()
    assert sentinel.exists()
    assert sentinel.read_text() == "preserved content"


def test_collision_cancel_returns_zero_no_changes(monkeypatch, tmp_path):
    """When operator chooses Cancel (default), _check_collision returns False.

    The wizard must then return 0 without touching the filesystem.
    """
    agent_dir = tmp_path / "existing-agent"
    agent_dir.mkdir()
    (agent_dir / "existing.txt").write_text("untouched")

    console = _FakeConsole()
    # Confirm.ask returns False (Cancel is the default).
    overwrite = W._check_collision(agent_dir, console, _confirm_factory(False))
    assert overwrite is False

    # Filesystem is unchanged.
    assert (agent_dir / "existing.txt").read_text() == "untouched"


# ---------------------------------------------------------------------------
# G. OSError translation (MUST 3, T-EX1)
# ---------------------------------------------------------------------------


def test_oserror_eacces_translated_to_plain_english(monkeypatch, tmp_path):
    """EACCES from atomic_write is caught and printed as plain English."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("atomic_agents._llm._get_key", lambda **kw: "sk-ant-fake")
    monkeypatch.delenv("ATOMIC_AGENTS_PERSONA_BACKEND_URL", raising=False)

    def _raising_atomic_write(target, content, encoding="utf-8"):
        raise OSError(errno.EACCES, "Permission denied", str(target))

    monkeypatch.setattr(
        "atomic_agents.init.wizard._io.atomic_write", _raising_atomic_write
    )

    # Patch doctor handoff so the test does not invoke LLM.
    monkeypatch.setattr(
        "atomic_agents.init.wizard._doctor_handoff",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "atomic_agents.init.wizard._maybe_test_call",
        lambda *a, **kw: None,
    )

    console = _FakeConsole()

    rc = W._write_scaffold(
        agent_dir=tmp_path / "blocked-agent",
        template_name="advisor",
        vars={
            k: "x"
            for k in [
                C.TEMPLATE_VAR_AGENT_NAME,
                C.TEMPLATE_VAR_MISSION,
                C.TEMPLATE_VAR_SCOPE_IN,
                C.TEMPLATE_VAR_SCOPE_OUT,
                C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL,
                C.TEMPLATE_VAR_AUTONOMY_READ_ONLY,
                C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE,
                C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT,
                C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK,
                C.TEMPLATE_VAR_VOICE,
                C.TEMPLATE_VAR_COMM_PREFS,
                C.TEMPLATE_VAR_HARD_REFUSALS,
            ]
        },
        agent_name="blocked-agent",
        agents_root=tmp_path,
        console=console,
        Confirm=_confirm_factory(False),
        existing=False,
    )

    assert rc == 1
    output = console.out.getvalue()
    assert C.MSG_OSERROR_FIX in output


def test_oserror_no_stack_trace_propagated(monkeypatch, tmp_path, capsys):
    """OSError from atomic_write must not produce a Python traceback on stderr."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("atomic_agents._llm._get_key", lambda **kw: "sk-ant-fake")
    monkeypatch.delenv("ATOMIC_AGENTS_PERSONA_BACKEND_URL", raising=False)

    def _raising_atomic_write(target, content, encoding="utf-8"):
        raise OSError(errno.EACCES, "Permission denied", str(target))

    monkeypatch.setattr(
        "atomic_agents.init.wizard._io.atomic_write", _raising_atomic_write
    )
    monkeypatch.setattr(
        "atomic_agents.init.wizard._doctor_handoff",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "atomic_agents.init.wizard._maybe_test_call",
        lambda *a, **kw: None,
    )

    console = _FakeConsole()

    W._write_scaffold(
        agent_dir=tmp_path / "blocked-agent",
        template_name="advisor",
        vars={
            k: "x"
            for k in [
                C.TEMPLATE_VAR_AGENT_NAME,
                C.TEMPLATE_VAR_MISSION,
                C.TEMPLATE_VAR_SCOPE_IN,
                C.TEMPLATE_VAR_SCOPE_OUT,
                C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL,
                C.TEMPLATE_VAR_AUTONOMY_READ_ONLY,
                C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE,
                C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT,
                C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK,
                C.TEMPLATE_VAR_VOICE,
                C.TEMPLATE_VAR_COMM_PREFS,
                C.TEMPLATE_VAR_HARD_REFUSALS,
            ]
        },
        agent_name="blocked-agent",
        agents_root=tmp_path,
        console=console,
        Confirm=_confirm_factory(False),
        existing=False,
    )

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


# ---------------------------------------------------------------------------
# H. Template variable substitution (MUST 13)
# ---------------------------------------------------------------------------


def test_render_files_uses_safe_substitute(tmp_path):
    """safe_substitute leaves unknown $variables intact; no KeyError is raised.

    A vars map that contains an answer with literal $primary_goal text must not
    raise KeyError even though $primary_goal is not in the vars map.
    """
    agent_dir = tmp_path / "sub-test"

    # Provide all 12 known template vars but do NOT add $primary_goal.
    vars_map = {
        C.TEMPLATE_VAR_AGENT_NAME: "I use $primary_goal as a placeholder",
        C.TEMPLATE_VAR_MISSION: "test mission",
        C.TEMPLATE_VAR_SCOPE_IN: "test scope_in",
        C.TEMPLATE_VAR_SCOPE_OUT: "test scope_out",
        C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: C.PRESET_CAUTIOUS,
        C.TEMPLATE_VAR_AUTONOMY_READ_ONLY: C.POLICY_BYPASS,
        C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE: C.POLICY_ALLOW_WITH_AUDIT,
        C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT: C.POLICY_ESCALATE,
        C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK: C.POLICY_ESCALATE,
        C.TEMPLATE_VAR_VOICE: "clear, direct",
        C.TEMPLATE_VAR_COMM_PREFS: "bullets please",
        C.TEMPLATE_VAR_HARD_REFUSALS: "none",
    }

    # Must not raise KeyError.
    written = W._render_files(agent_dir, "advisor", vars_map)

    assert len(written) > 0
    # Verify the agent_name substitution happened.
    identity_file = agent_dir / "persona" / "IDENTITY.md"
    assert identity_file.exists()
    content = identity_file.read_text()
    # The substituted value appears somewhere (agent_name var in template).
    assert "I use $primary_goal as a placeholder" in content


def test_render_files_writes_through_atomic_write(tmp_path, monkeypatch):
    """Every file write must go through _io.atomic_write (MUST 4)."""
    agent_dir = tmp_path / "aw-test"
    write_calls: list[Path] = []

    original_atomic_write = W._io.atomic_write

    def _spy_atomic_write(target, content, encoding="utf-8"):
        write_calls.append(Path(target))
        return original_atomic_write(target, content, encoding=encoding)

    monkeypatch.setattr("atomic_agents.init.wizard._io.atomic_write", _spy_atomic_write)

    vars_map = {
        C.TEMPLATE_VAR_AGENT_NAME: "aw-test",
        C.TEMPLATE_VAR_MISSION: "m",
        C.TEMPLATE_VAR_SCOPE_IN: "si",
        C.TEMPLATE_VAR_SCOPE_OUT: "so",
        C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: C.PRESET_CAUTIOUS,
        C.TEMPLATE_VAR_AUTONOMY_READ_ONLY: C.POLICY_BYPASS,
        C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE: C.POLICY_ALLOW_WITH_AUDIT,
        C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT: C.POLICY_ESCALATE,
        C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK: C.POLICY_ESCALATE,
        C.TEMPLATE_VAR_VOICE: "calm",
        C.TEMPLATE_VAR_COMM_PREFS: "short",
        C.TEMPLATE_VAR_HARD_REFUSALS: "none",
    }

    written = W._render_files(agent_dir, "advisor", vars_map)

    # Every file returned should have gone through atomic_write.
    assert len(written) > 0
    for f in written:
        assert f in write_calls, f"{f} was not written through atomic_write"


def test_render_files_uses_safe_resolve_under(tmp_path, monkeypatch):
    """Every rendered file MUST pass through _io.safe_resolve_under (C1, MUST 4).

    Verifies the path-traversal validation gate added in Round 1.
    """
    agent_dir = tmp_path / "srt-test"
    resolve_calls: list[Path] = []
    original_resolver = W._io.safe_resolve_under

    def _spy_resolver(child, root):
        resolve_calls.append(Path(root) / Path(str(child)))
        return original_resolver(child, root)

    monkeypatch.setattr(
        "atomic_agents.init.wizard._io.safe_resolve_under", _spy_resolver
    )

    vars_map = {
        C.TEMPLATE_VAR_AGENT_NAME: "srt-test",
        C.TEMPLATE_VAR_MISSION: "m",
        C.TEMPLATE_VAR_SCOPE_IN: "si",
        C.TEMPLATE_VAR_SCOPE_OUT: "so",
        C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: C.PRESET_CAUTIOUS,
        C.TEMPLATE_VAR_AUTONOMY_READ_ONLY: C.POLICY_BYPASS,
        C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE: C.POLICY_ALLOW_WITH_AUDIT,
        C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT: C.POLICY_ESCALATE,
        C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK: C.POLICY_ESCALATE,
        C.TEMPLATE_VAR_VOICE: "v",
        C.TEMPLATE_VAR_COMM_PREFS: "cp",
        C.TEMPLATE_VAR_HARD_REFUSALS: "hr",
    }

    written = W._render_files(agent_dir, "advisor", vars_map)

    # Every rendered file must have had its target validated by safe_resolve_under.
    assert len(written) > 0, "no files written"
    assert len(resolve_calls) >= len(written), (
        f"safe_resolve_under called {len(resolve_calls)} times for "
        f"{len(written)} files; expected at least one call per file"
    )


# ---------------------------------------------------------------------------
# I. agents_root single-resolution (M9, H6)
# ---------------------------------------------------------------------------


def test_from_template_works_in_non_tty(monkeypatch, tmp_path):
    """--from-template proceeds normally even when stdin is not a TTY (MUST 11).

    The non-TTY guard only fires for the interactive Q&A path.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("atomic_agents._llm._get_key", lambda **kw: "sk-ant-fake")
    monkeypatch.delenv("ATOMIC_AGENTS_PERSONA_BACKEND_URL", raising=False)

    # Stub heavy downstream calls so the test does not hit the filesystem or LLM.
    monkeypatch.setattr(
        "atomic_agents.init.wizard._from_template",
        lambda *a, **kw: 0,
    )

    args = _make_args(
        agent_name="my-advisor", from_template="advisor", agents_root=str(tmp_path)
    )
    rc = W.run_init(args)
    # Must NOT return 2 (non-TTY rejection) -- any non-2 is acceptable here.
    assert rc != 2


def test_from_template_requires_agent_name(capsys):
    """--from-template without an agent name returns exit code 2 with a plain-English error."""
    args = _make_args(from_template="advisor", agent_name=None)
    rc = W.run_init(args)
    assert rc == 2
    captured = capsys.readouterr()
    assert "--from-template" in captured.err
    assert "agent name" in captured.err.lower()


def test_run_init_resolves_agents_root_once(monkeypatch, tmp_path):
    """run_init must call get_agents_root AT MOST once regardless of code path taken."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    call_count = []

    def _counting_get_agents_root():
        call_count.append(1)
        return tmp_path

    monkeypatch.setattr(
        "atomic_agents.init.wizard._platform.get_agents_root", _counting_get_agents_root
    )
    monkeypatch.setattr("atomic_agents._llm._get_key", lambda **kw: "sk-ant-fake")
    monkeypatch.delenv("ATOMIC_AGENTS_PERSONA_BACKEND_URL", raising=False)

    # Patch the heavy downstream functions so we stop right after agents_root resolution.
    monkeypatch.setattr(
        "atomic_agents.init.wizard._persona_backend_check",
        lambda *a, **kw: False,  # decline -> return 0 immediately
    )

    # agents_root=None forces use of get_agents_root().
    args = _make_args(agents_root=None)
    W.run_init(args)

    assert len(call_count) <= 1, (
        f"get_agents_root() was called {len(call_count)} times; expected at most 1"
    )

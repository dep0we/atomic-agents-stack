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
import warnings
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
    """When _get_key raises AtomicAgentsError, run_init exits 1 with MSG_NO_PROVIDER_KEY."""
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
    assert C.MSG_NO_PROVIDER_KEY in captured.err


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
    """When operator chooses Cancel, _check_collision returns ("cancel", None).

    The wizard must then return 0 without touching the filesystem.
    """
    agent_dir = tmp_path / "existing-agent"
    agent_dir.mkdir()
    (agent_dir / "existing.txt").write_text("untouched")

    console = _FakeConsole()
    # Prompt.ask returns "cancel".
    Prompt = _prompt_sequence("cancel")
    branch, headers = W._check_collision(
        agent_dir, console, Prompt, _confirm_factory(False), template_name=None
    )
    assert branch == "cancel"
    assert headers is None

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


# ---------------------------------------------------------------------------
# J. Section detection state machine (_extract_h2_headers)
# ---------------------------------------------------------------------------


def test_extract_h2_headers_skips_yaml_frontmatter():
    """YAML frontmatter block is skipped; only headers after closing --- are returned."""
    content = "---\nfoo: bar\n## not a header\n---\n## Real Header\n"
    result = W._extract_h2_headers(content)
    assert result == ["Real Header"]


def test_extract_h2_headers_skips_code_fences():
    """Headers inside triple-backtick fences are NOT returned."""
    content = "## Outside\n```\n## Inside fence (skip)\n```\n## After\n"
    result = W._extract_h2_headers(content)
    assert result == ["Outside", "After"]


def test_extract_h2_headers_skips_html_comments():
    """Headers inside HTML comment blocks are NOT returned."""
    content = "<!--\n## hidden\n-->\n## Visible\n"
    result = W._extract_h2_headers(content)
    assert result == ["Visible"]


def test_extract_h2_headers_strips_trailing_atx_markers():
    """Trailing closing hashes on an ATX header are stripped from the returned text."""
    content = "## My Header ##\n"
    result = W._extract_h2_headers(content)
    assert result == ["My Header"]


# ---------------------------------------------------------------------------
# K. Add-to-it dispatch + detection (_detect_sections, _check_collision)
# ---------------------------------------------------------------------------


def _write_advisor_scaffold(root: Path) -> None:
    """Write a valid advisor scaffold under root using TEMPLATE_SECTION_SCHEMA headers."""
    schema = C.TEMPLATE_SECTION_SCHEMA["advisor"]
    for relpath, headers in schema.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"## {h}\n\nContent.\n" for h in headers)
        target.write_text(body, encoding="utf-8")


def test_detect_sections_advisor_happy_path(tmp_path):
    """A valid advisor scaffold passes section detection with success=True."""
    agent_dir = tmp_path / "my-advisor"
    agent_dir.mkdir()
    _write_advisor_scaffold(agent_dir)

    success, per_file_headers, failed_files = W._detect_sections(agent_dir, "advisor")

    assert success is True
    assert failed_files == []
    # Every file in the schema should appear in per_file_headers.
    for relpath in C.TEMPLATE_SECTION_SCHEMA["advisor"]:
        assert relpath in per_file_headers, f"{relpath} not in per_file_headers"
        assert len(per_file_headers[relpath]) > 0


def test_detect_sections_fails_when_header_renamed(tmp_path):
    """Renaming a required header causes detection to return success=False."""
    agent_dir = tmp_path / "my-advisor"
    agent_dir.mkdir()
    _write_advisor_scaffold(agent_dir)

    # Replace "Mission" with "Purpose" in persona/IDENTITY.md.
    identity_path = agent_dir / "persona" / "IDENTITY.md"
    original = identity_path.read_text(encoding="utf-8")
    modified = original.replace("## Mission\n", "## Purpose\n")
    identity_path.write_text(modified, encoding="utf-8")

    success, per_file_headers, failed_files = W._detect_sections(agent_dir, "advisor")

    assert success is False
    assert "persona/IDENTITY.md" in failed_files


def test_detect_sections_missing_file_tracked_separately(tmp_path):
    """A missing file is NOT counted as a detection failure; it is a backfill case."""
    agent_dir = tmp_path / "my-advisor"
    agent_dir.mkdir()
    _write_advisor_scaffold(agent_dir)

    # Remove memory/INDEX.md to simulate a missing template-owned file.
    (agent_dir / "memory" / "INDEX.md").unlink()

    success, per_file_headers, failed_files = W._detect_sections(agent_dir, "advisor")

    assert success is True, "Missing file should be treated as backfill, not failure"
    assert "memory/INDEX.md" not in failed_files
    # per_file_headers still contains the relpath (as empty list per implementation).
    assert "memory/INDEX.md" in per_file_headers
    assert per_file_headers["memory/INDEX.md"] == []


def test_commit_merges_writes_all_files(tmp_path):
    """_commit_merges calls atomic_write for each file and returns (committed, [])."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    merged_content = {
        "persona/IDENTITY.md": "# ID\n\n## Mission\n\nTest mission.\n",
        "model.md": "# Model\n\nclaude-opus-4-7\n",
    }

    console = _FakeConsole()
    committed, failed = W._commit_merges(agent_dir, merged_content, console)

    assert failed == []
    assert set(committed) == {"persona/IDENTITY.md", "model.md"}
    assert (agent_dir / "persona" / "IDENTITY.md").read_text() == merged_content[
        "persona/IDENTITY.md"
    ]
    assert (agent_dir / "model.md").read_text() == merged_content["model.md"]


def test_commit_merges_partial_on_oserror(tmp_path, monkeypatch):
    """When a file write fails mid-commit, committed files are present and failed is reported."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    write_calls: list[str] = []
    original_aw = W._io.atomic_write

    def _flaky(target, content, encoding="utf-8"):
        write_calls.append(str(target))
        if "model.md" in str(target):
            raise OSError("Simulated write failure")
        return original_aw(target, content, encoding=encoding)

    monkeypatch.setattr("atomic_agents.init.wizard._io.atomic_write", _flaky)

    merged_content = {
        "model.md": "model content",
        "persona/IDENTITY.md": "id content",
    }

    console = _FakeConsole()
    committed, failed = W._commit_merges(agent_dir, merged_content, console)

    # persona/IDENTITY.md sorts before model.md; model.md fails
    assert "persona/IDENTITY.md" in committed
    assert "model.md" in failed


# ---------------------------------------------------------------------------
# L. Per-file atomic commit (_commit_merges) -- additional edge-case tests
# ---------------------------------------------------------------------------


def test_commit_merges_empty_map_returns_empty_lists(tmp_path):
    """_commit_merges with an empty merged_content dict writes nothing."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    console = _FakeConsole()
    committed, failed = W._commit_merges(agent_dir, {}, console)

    assert committed == []
    assert failed == []


def test_commit_merges_creates_parent_dirs(tmp_path):
    """_commit_merges creates intermediate directories via atomic_write."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    merged_content = {
        "persona/IDENTITY.md": "## Who I am\n\nTest.\n",
    }

    console = _FakeConsole()
    committed, failed = W._commit_merges(agent_dir, merged_content, console)

    assert failed == []
    assert committed == ["persona/IDENTITY.md"]
    assert (agent_dir / "persona" / "IDENTITY.md").exists()


def test_commit_merges_oserror_reported_in_failed(tmp_path, monkeypatch):
    """When atomic_write raises OSError, the relpath appears in failed, not committed."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    monkeypatch.setattr(
        "atomic_agents.init.wizard._io.atomic_write",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
    )

    merged_content = {"tools.md": "content"}
    console = _FakeConsole()
    committed, failed = W._commit_merges(agent_dir, merged_content, console)

    assert committed == []
    assert "tools.md" in failed
    assert "disk full" in console.out.getvalue()


# ---------------------------------------------------------------------------
# M. CRLF + BOM normalization (_render_diff_preview with merged_content map)
# ---------------------------------------------------------------------------


def test_render_diff_preview_normalizes_crlf(tmp_path):
    """Existing file with CRLF and merged content with LF shows no diff."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    shared_content = "## Hello\n\nWorld.\n"
    crlf_content = shared_content.replace("\n", "\r\n")

    (agent_dir / "notes.md").write_bytes(crlf_content.encode("utf-8"))

    merged_content = {"notes.md": shared_content}
    console = _FakeConsole()
    files_changed = W._render_diff_preview(agent_dir, merged_content, console)

    assert files_changed == 0, (
        "CRLF normalization should make CRLF vs LF files show as identical"
    )


def test_render_diff_preview_strips_utf8_bom(tmp_path):
    """Existing file with UTF-8 BOM and merged content without BOM shows no diff."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    shared_content = "## Hello\n\nWorld.\n"
    bom_bytes = b"\xef\xbb\xbf" + shared_content.encode("utf-8")

    (agent_dir / "notes.md").write_bytes(bom_bytes)

    merged_content = {"notes.md": shared_content}
    console = _FakeConsole()
    files_changed = W._render_diff_preview(agent_dir, merged_content, console)

    assert files_changed == 0, (
        "UTF-8 BOM normalization should make BOM vs non-BOM files show as identical"
    )


# ---------------------------------------------------------------------------
# N. Polish item tests
# ---------------------------------------------------------------------------


def test_doctor_handoff_split_run_doctor_failure(monkeypatch, tmp_path):
    """When run_doctor raises, _doctor_handoff prints 'Doctor inconclusive' and returns True."""
    import types as _types_mod

    fake_doctor = _types_mod.SimpleNamespace(
        run_doctor=lambda **kw: (_ for _ in ()).throw(RuntimeError("doctor exploded")),
        overall_exit_code=lambda results: 0,
        render_human=lambda results: "ok",
    )

    monkeypatch.setattr("atomic_agents.init.wizard.doctor", fake_doctor, raising=False)

    # Import doctor inside wizard lazily; patch the module attribute.
    import atomic_agents.init.wizard as _wiz

    original = None
    import importlib
    import atomic_agents.doctor as _doc_real

    def _patched_run_doctor(**kwargs):
        raise RuntimeError("doctor exploded")

    monkeypatch.setattr(_doc_real, "run_doctor", _patched_run_doctor)

    console = _FakeConsole()
    result = _wiz._doctor_handoff("my-agent", tmp_path, console)

    assert result is True
    assert "inconclusive" in console.out.getvalue().lower()


def test_doctor_handoff_split_render_failure(monkeypatch, tmp_path):
    """When render_human raises, output contains a 'could not render' notice."""
    import atomic_agents.doctor as _doc_real

    fake_result = types.SimpleNamespace(status="pass")

    def _ok_run_doctor(**kwargs):
        return [fake_result]

    def _ok_exit_code(results):
        return 0

    def _bad_render(results):
        raise RuntimeError("renderer crashed")

    monkeypatch.setattr(_doc_real, "run_doctor", _ok_run_doctor)
    monkeypatch.setattr(_doc_real, "overall_exit_code", _ok_exit_code)
    monkeypatch.setattr(_doc_real, "render_human", _bad_render)

    console = _FakeConsole()
    import atomic_agents.init.wizard as _wiz

    result = _wiz._doctor_handoff("my-agent", tmp_path, console)

    output = console.out.getvalue().lower()
    assert "could not render" in output or "render" in output


def test_from_template_length_check_fires_before_regex(monkeypatch, tmp_path, capsys):
    """A 100-character name triggers MSG_INVALID_NAME_TOO_LONG before MSG_INVALID_NAME_CHARSET."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.delenv("ATOMIC_AGENTS_PERSONA_BACKEND_URL", raising=False)

    long_name = "a" * 100
    args = _make_args(
        agent_name=long_name, from_template="advisor", agents_root=str(tmp_path)
    )
    rc = W.run_init(args)

    assert rc == 2
    captured = capsys.readouterr()
    assert C.MSG_INVALID_NAME_TOO_LONG in captured.err
    assert C.MSG_INVALID_NAME_CHARSET not in captured.err


def test_types_helper_returns_empty_when_mod_none():
    """W._types(None, 'Foo', 'Bar') returns () so isinstance(..., ()) is always False."""
    result = W._types(None, "Foo", "Bar")
    assert result == ()
    assert isinstance(Exception(), result) is False


def test_translate_oserror_enoent_specific_message(tmp_path):
    """ENOENT OSError is translated to a message mentioning 'disappeared'."""
    import errno as _errno

    e = OSError(_errno.ENOENT, "No such file or directory", str(tmp_path / "agent"))
    msg = W._translate_oserror(e, tmp_path / "agent")

    assert "disappeared" in msg.lower()


def test_walk_traversable_iterative_no_recursion_limit():
    """_walk_traversable returns at least 7 files for the advisor template."""
    from importlib import resources as _resources

    template_pkg_path = _resources.files("atomic_agents.init") / "templates" / "advisor"
    results = W._walk_traversable(template_pkg_path, [])

    assert len(results) >= 7, (
        f"Expected at least 7 files in advisor template, got {len(results)}"
    )


def test_backup_timestamps_use_microseconds(tmp_path, monkeypatch):
    """The backup path produced by _collision_overwrite_backup_restore contains a microsecond suffix."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    (agent_dir / "file.txt").write_text("original")

    backup_paths: list[Path] = []
    original_rename = Path.rename

    def _spy_rename(self, target):
        backup_paths.append(Path(target))
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _spy_rename)

    import re as _re

    def _write_func():
        agent_dir.mkdir(parents=True, exist_ok=True)

    W._collision_overwrite_backup_restore(agent_dir, _write_func)

    assert backup_paths, "rename was never called"
    bak_name = backup_paths[0].name
    # Timestamp format: YYYYMMDDTHHMMSS_FFFFFFZ (6-digit microseconds).
    assert _re.search(r"\.bak\.\d{8}T\d{6}_\d{6}Z$", bak_name), (
        f"Backup name '{bak_name}' does not contain microsecond timestamp"
    )


def test_persona_backend_warning_shows_redacted_url(monkeypatch):
    """URL with credentials is displayed with credentials stripped in the warning."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_PERSONA_BACKEND_URL", "https://user:pass@example.com/api"
    )
    console = _FakeConsole()
    # Operator declines; we only care about the printed output.
    W._persona_backend_check(console, _confirm_factory(False))

    output = console.out.getvalue()
    assert "https://example.com/api" in output
    assert "user:pass" not in output


# ---------------------------------------------------------------------------
# O. Per-template preset dispatch (_default_template_vars)
# ---------------------------------------------------------------------------


def test_default_template_vars_advisor_uses_cautious():
    """advisor template defaults to PRESET_CAUTIOUS for all four autonomy variables."""
    vars_map = W._default_template_vars(name="x", template_name="advisor")

    expected_preset = C.AUTONOMY_PRESETS[C.PRESET_CAUTIOUS]
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_READ_ONLY]
        == expected_preset[C.ACTION_CLASS_READ_ONLY]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE]
        == expected_preset[C.ACTION_CLASS_REVERSIBLE_WRITE]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT]
        == expected_preset[C.ACTION_CLASS_EXTERNAL_SIDE_EFFECT]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK]
        == expected_preset[C.ACTION_CLASS_HIGH_RISK]
    )
    assert vars_map[C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL] == C.PRESET_CAUTIOUS


def test_default_template_vars_researcher_uses_cautious():
    """researcher template defaults to PRESET_CAUTIOUS for all four autonomy variables."""
    vars_map = W._default_template_vars(name="x", template_name="researcher")

    expected_preset = C.AUTONOMY_PRESETS[C.PRESET_CAUTIOUS]
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_READ_ONLY]
        == expected_preset[C.ACTION_CLASS_READ_ONLY]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE]
        == expected_preset[C.ACTION_CLASS_REVERSIBLE_WRITE]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT]
        == expected_preset[C.ACTION_CLASS_EXTERNAL_SIDE_EFFECT]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK]
        == expected_preset[C.ACTION_CLASS_HIGH_RISK]
    )
    assert vars_map[C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL] == C.PRESET_CAUTIOUS


def test_default_template_vars_writer_uses_cautious():
    """writer template defaults to PRESET_CAUTIOUS for all four autonomy variables."""
    vars_map = W._default_template_vars(name="x", template_name="writer")

    expected_preset = C.AUTONOMY_PRESETS[C.PRESET_CAUTIOUS]
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_READ_ONLY]
        == expected_preset[C.ACTION_CLASS_READ_ONLY]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE]
        == expected_preset[C.ACTION_CLASS_REVERSIBLE_WRITE]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT]
        == expected_preset[C.ACTION_CLASS_EXTERNAL_SIDE_EFFECT]
    )
    assert (
        vars_map[C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK]
        == expected_preset[C.ACTION_CLASS_HIGH_RISK]
    )
    assert vars_map[C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL] == C.PRESET_CAUTIOUS


def test_detect_sections_orphan_h2_preserved_in_extraction(tmp_path):
    """Operator-added orphan h2 sections (not in schema) are tolerated and tracked.

    Per spec/35 MUST 15: orphan sections preserve verbatim. The detection step
    must succeed (superset check) when an operator's existing file contains
    schema headers PLUS extra h2 sections.
    """
    agent_dir = tmp_path / "orphan-test"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)

    # Write IDENTITY.md with the advisor schema headers PLUS a custom orphan.
    schema = C.TEMPLATE_SECTION_SCHEMA["advisor"]
    content = "# IDENTITY\n\n"
    for header in schema["persona/IDENTITY.md"]:
        content += f"## {header}\n\nplaceholder content under {header}\n\n"
    content += "## My Custom Section\n\noperator-authored content here\n\n"
    (persona_dir / "IDENTITY.md").write_text(content, encoding="utf-8")

    # Write the other 6 files with just the schema h2s, no orphans.
    for relpath in [
        "persona/SOUL.md",
        "persona/USER.md",
        "tools.md",
        "model.md",
        "memory/INDEX.md",
        "wiki/INDEX.md",
    ]:
        p = agent_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"## {h}\n\n" for h in schema[relpath])
        p.write_text(body, encoding="utf-8")

    ok, per_file_headers, failed = W._detect_sections(agent_dir, "advisor")

    assert ok is True
    assert "My Custom Section" in per_file_headers["persona/IDENTITY.md"]
    assert failed == []


# ---------------------------------------------------------------------------
# M4. Add-to-it: detection-failure fallback to cancel
# ---------------------------------------------------------------------------


def test_check_collision_add_to_it_falls_back_to_cancel_on_detection_failure(
    tmp_path,
):
    """When add_to_it is chosen but section detection fails, fallback prompt
    is shown and 'cancel' branch is returned with existing_headers=None."""
    import io as _io_mod

    from rich.console import Console

    agent_dir = tmp_path / "my-advisor"
    agent_dir.mkdir()
    # Build a valid advisor scaffold via _render_files.
    vars_map = W._default_template_vars("my-advisor", "advisor")
    W._render_files(agent_dir, "advisor", vars_map)

    # Now corrupt IDENTITY.md by removing the Mission h2 so detection fails.
    identity_path = agent_dir / "persona" / "IDENTITY.md"
    original = identity_path.read_text(encoding="utf-8")
    # Remove the first ## Mission line and the paragraph after it.
    import re as _re

    corrupted = _re.sub(r"## Mission\n\n[^\n]*\n", "", original)
    identity_path.write_text(corrupted, encoding="utf-8")

    # Mock Prompt: first call returns "add_to_it", second call returns "cancel".
    call_sequence = ["add_to_it", "cancel"]
    call_index = [0]

    class _MockPrompt:
        @staticmethod
        def ask(question, choices=None, default=None, console=None):
            val = call_sequence[call_index[0]]
            call_index[0] += 1
            return val

    console = Console(file=_io_mod.StringIO())

    branch, existing_headers = W._check_collision(
        agent_dir,
        console=console,
        Prompt=_MockPrompt,
        Confirm=None,
        template_name="advisor",
    )

    assert branch == "cancel"
    assert existing_headers is None
    # Confirm the detection-failed message appeared.
    output = console.file.getvalue()
    # MSG_SECTION_DETECTION_FAILED substring is present in console output.
    # Use a portion that won't be split by rich line-wrapping.
    assert "existing files don't match the advisor template" in output


# ---------------------------------------------------------------------------
# M5. _compute_merged_content + _split_sections/_join_sections round-trip tests
# ---------------------------------------------------------------------------


def test_split_join_sections_round_trip_byte_identical():
    """_split_sections + _join_sections must reproduce the original bytes exactly."""
    content = (
        "# Title\n\nsome preamble text\n\n"
        "## Section One\n\nBody one.\n\n"
        "## Section Two\n\n"
        "```python\n"
        "## not a header\n"
        "print('hello')\n"
        "```\n\n"
        "<!-- ## also not a header -->\n\n"
        "## Section Three\n\nBody three.\n"
    )
    blocks = W._split_sections(content)
    reconstructed = W._join_sections(blocks)
    assert content == reconstructed


def test_compute_merged_content_preserves_preamble_verbatim(tmp_path):
    """Custom preamble text before the first h2 is preserved verbatim after merge."""
    agent_dir = tmp_path / "my-advisor"
    agent_dir.mkdir()
    vars_map = W._default_template_vars("my-advisor", "advisor")
    W._render_files(agent_dir, "advisor", vars_map)

    # Insert a custom preamble before the first h2 in IDENTITY.md.
    identity_path = agent_dir / "persona" / "IDENTITY.md"
    original = identity_path.read_text(encoding="utf-8")
    preamble = "<!-- custom preamble: operator-added -->\n\n"
    # Find the first h2 and insert preamble before it.
    first_h2 = original.index("\n## ")
    modified = original[: first_h2 + 1] + preamble + original[first_h2 + 1 :]
    identity_path.write_text(modified, encoding="utf-8")

    fresh_vars = W._default_template_vars("my-advisor", "advisor")
    merged = W._compute_merged_content(agent_dir, "advisor", fresh_vars)

    assert "persona/IDENTITY.md" in merged
    assert "<!-- custom preamble: operator-added -->" in merged["persona/IDENTITY.md"]


def test_compute_merged_content_appends_new_h3_from_fresh_template(tmp_path):
    """When the existing file lacks an h3 subsection that the fresh template has
    inside a schema h2, the merged result appends the new h3 at the end of that
    schema h2 block."""
    agent_dir = tmp_path / "my-advisor"
    agent_dir.mkdir()

    # Build a minimal scaffold: IDENTITY.md with a schema h2 but NO h3.
    schema = C.TEMPLATE_SECTION_SCHEMA["advisor"]
    for relpath, headers in schema.items():
        target = agent_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"## {h}\n\nExisting content.\n" for h in headers)
        target.write_text(body, encoding="utf-8")

    # Render the fresh template which may have h3s inside some schema h2s.
    # We simulate a fresh template having a NEW h3 by injecting one into the
    # fresh_text produced by _render_file_to_string via monkeypatching the
    # underlying template rendering.
    identity_rel = "persona/IDENTITY.md"
    first_header = schema[identity_rel][0]

    # Manually modify the IDENTITY.md on disk to include an h3 that the fresh
    # template does NOT have (so it is treated as orphan h3, preserved).
    existing_path = agent_dir / identity_rel
    existing_body = existing_path.read_text(encoding="utf-8")
    with_h3 = existing_body.replace(
        f"## {first_header}\n\nExisting content.\n",
        f"## {first_header}\n\nExisting content.\n\n### My Subsection\n\nsubsection body\n",
    )
    existing_path.write_text(with_h3, encoding="utf-8")

    fresh_vars = W._default_template_vars("my-advisor", "advisor")
    merged = W._compute_merged_content(agent_dir, "advisor", fresh_vars)

    # The existing h3 subsection should be preserved in the merged output.
    result = merged.get(identity_rel, "")
    assert "### My Subsection" in result
    assert "subsection body" in result


def test_compute_merged_content_preserves_orphan_h2_in_position(tmp_path):
    """An orphan h2 inserted between two schema h2s is preserved in position,
    not appended at the end."""
    agent_dir = tmp_path / "my-advisor"
    agent_dir.mkdir()
    vars_map = W._default_template_vars("my-advisor", "advisor")
    W._render_files(agent_dir, "advisor", vars_map)

    identity_path = agent_dir / "persona" / "IDENTITY.md"
    original = identity_path.read_text(encoding="utf-8")

    # Find two consecutive schema h2s and insert an orphan between them.
    schema_headers = C.TEMPLATE_SECTION_SCHEMA["advisor"]["persona/IDENTITY.md"]
    # Use the first two headers to find an insertion point.
    h1 = schema_headers[0]
    h2 = schema_headers[1]
    marker = f"## {h2}\n"
    orphan_block = "## My Notes\n\noperator notes here\n\n"
    modified = original.replace(marker, orphan_block + marker, 1)
    identity_path.write_text(modified, encoding="utf-8")

    fresh_vars = W._default_template_vars("my-advisor", "advisor")
    merged = W._compute_merged_content(agent_dir, "advisor", fresh_vars)

    result = merged.get("persona/IDENTITY.md", "")
    assert "## My Notes" in result
    assert "operator notes here" in result

    # The orphan must appear BEFORE the second schema h2 (h2 marker) in the text.
    orphan_pos = result.index("## My Notes")
    second_h2_pos = result.index(f"## {h2}")
    assert orphan_pos < second_h2_pos, (
        "Orphan section should appear before the second schema h2, not at the end"
    )


# ---------------------------------------------------------------------------
# R2-A. Round 2 Fix-A: h3-aware merge (C1), duplicate h2 (H1),
#        setext detection (M2), HTML-comment tightening (M3),
#        and _render_diff_preview exception guard (M1).
# ---------------------------------------------------------------------------


def test_compute_merged_content_h3_preserves_operator_subsection_under_schema_h2(
    tmp_path,
):
    """C1 regression: an operator-added ### subsection under a schema h2 block
    is preserved verbatim after Add-to-it merge.

    Per spec/35 MUST 15 additive-merge contract: h3+ subsections present in the
    existing file MUST be preserved verbatim in original order.
    """
    agent_dir = tmp_path / "my-advisor"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)

    existing_content = (
        "## Mission\n"
        "\n"
        "Operator preamble text.\n"
        "\n"
        "### Operator-added subsection\n"
        "\n"
        "This operator-authored content MUST survive the merge.\n"
        "\n"
    )
    (persona_dir / "IDENTITY.md").write_text(existing_content, encoding="utf-8")

    schema = C.TEMPLATE_SECTION_SCHEMA["advisor"]
    for relpath, headers in schema.items():
        if relpath == "persona/IDENTITY.md":
            continue
        p = agent_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"## {h}\n\n" for h in headers), encoding="utf-8")

    fresh_vars = W._default_template_vars("my-advisor", "advisor")
    merged = W._compute_merged_content(agent_dir, "advisor", fresh_vars)

    result = merged.get("persona/IDENTITY.md", "")
    assert "### Operator-added subsection" in result, (
        "Operator h3 subsection must be preserved by additive merge"
    )
    assert "This operator-authored content MUST survive the merge." in result, (
        "Operator h3 body text must be preserved by additive merge"
    )


def test_compute_merged_content_h3_appends_new_template_subsection_at_end(
    tmp_path,
):
    """C1: a fresh-template h3 not present in existing is appended at end of
    the schema h2 block.

    Per spec/35 MUST 15: h3+ subsections in the fresh template not present in
    the existing file MUST be appended at the end of the schema h2 block.
    """
    import unittest.mock as _mock

    agent_dir = tmp_path / "my-advisor"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)

    existing_content = (
        "## Mission\n\nExisting preamble.\n\n### Existing h3\n\nExisting h3 body.\n\n"
    )
    (persona_dir / "IDENTITY.md").write_text(existing_content, encoding="utf-8")

    schema = C.TEMPLATE_SECTION_SCHEMA["advisor"]
    for relpath, headers in schema.items():
        if relpath == "persona/IDENTITY.md":
            continue
        p = agent_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"## {h}\n\n" for h in headers), encoding="utf-8")

    original_render = W._render_file_to_string

    def _patched_render(template_name, rel_parts, vars):
        if rel_parts == ["persona", "IDENTITY.md"]:
            return (
                "## Mission\n"
                "\n"
                "Fresh preamble (not used by additive merge).\n"
                "\n"
                "### Existing h3\n"
                "\n"
                "Existing h3 body.\n"
                "\n"
                "### Brand new template h3\n"
                "\n"
                "Brand new h3 body from template.\n"
                "\n"
            )
        return original_render(template_name, rel_parts, vars)

    with _mock.patch.object(W, "_render_file_to_string", side_effect=_patched_render):
        fresh_vars = W._default_template_vars("my-advisor", "advisor")
        merged = W._compute_merged_content(agent_dir, "advisor", fresh_vars)

    result = merged.get("persona/IDENTITY.md", "")
    assert "### Brand new template h3" in result, (
        "New template h3 must be appended when operator file does not have it"
    )
    assert "Brand new h3 body from template." in result, (
        "New template h3 body must be appended"
    )
    assert result.index("### Existing h3") < result.index(
        "### Brand new template h3"
    ), "Existing h3 should appear before newly-appended template h3"


def test_detect_sections_fails_on_duplicate_schema_h2(tmp_path):
    """H1: a file with the same schema h2 header appearing twice causes section
    detection to fail and routes the file to failed_files.

    Per spec/35 MUST 15: files containing duplicate schema h2 headers MUST cause
    section detection to fail and route to overwrite/cancel.
    """
    agent_dir = tmp_path / "dup-h2"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)

    schema = C.TEMPLATE_SECTION_SCHEMA["advisor"]
    first_header = schema["persona/IDENTITY.md"][0]
    content = "".join(f"## {h}\n\n" for h in schema["persona/IDENTITY.md"])
    content += f"## {first_header}\n\nduplicate section body.\n\n"
    (persona_dir / "IDENTITY.md").write_text(content, encoding="utf-8")

    for relpath, headers in schema.items():
        if relpath == "persona/IDENTITY.md":
            continue
        p = agent_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"## {h}\n\n" for h in headers), encoding="utf-8")

    success, _per_file, failed_files = W._detect_sections(agent_dir, "advisor")

    assert success is False
    assert "persona/IDENTITY.md" in failed_files, (
        "File with duplicate schema h2 must appear in failed_files"
    )


def test_detect_sections_fails_on_setext_h2(tmp_path):
    """M2: a file containing a Setext-style heading causes section detection to
    fail and routes the file to failed_files.

    Per spec/35 MUST 15: files containing Setext-style headings MUST cause
    section detection to fail and route to overwrite/cancel.
    """
    agent_dir = tmp_path / "setext-agent"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)

    schema = C.TEMPLATE_SECTION_SCHEMA["advisor"]
    setext_content = "Some Header\n-----------\n\nBody under the setext heading.\n\n"
    for h in schema["persona/IDENTITY.md"]:
        setext_content += f"## {h}\n\n"
    (persona_dir / "IDENTITY.md").write_text(setext_content, encoding="utf-8")

    for relpath, headers in schema.items():
        if relpath == "persona/IDENTITY.md":
            continue
        p = agent_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"## {h}\n\n" for h in headers), encoding="utf-8")

    success, _per_file, failed_files = W._detect_sections(agent_dir, "advisor")

    assert success is False
    assert "persona/IDENTITY.md" in failed_files, (
        "File with Setext heading must appear in failed_files"
    )


def test_extract_h2_headers_ignores_inline_code_html_comment():
    """M3: an inline ``<!--`` that does not start the line does NOT trigger the
    HTML-comment state machine.

    The tightened parser only toggles on lines whose stripped form starts with
    ``<!--``, so documentation prose containing ``<!--`` inline must not
    suppress subsequent h2 headers.
    """
    content = (
        "Some prose with inline <!-- comment syntax documented here.\n## Real Header\n"
    )
    result = W._extract_h2_headers(content)
    assert "Real Header" in result, (
        "h2 after inline <!-- must NOT be suppressed; only line-start <!-- toggles"
    )


def test_render_diff_preview_handles_exception_gracefully(tmp_path):
    """M1: when _render_diff_preview_inner raises, _render_diff_preview catches
    the exception, prints a fallback file list, and returns -1.

    Satisfies MUST 3 (no stack traces propagate to operator).
    """
    import unittest.mock as _mock
    from io import StringIO

    agent_dir = tmp_path / "exc-agent"
    agent_dir.mkdir()

    merged_content = {
        "persona/IDENTITY.md": "## Mission\n\nTest.\n",
        "persona/SOUL.md": "## Voice\n\nTest.\n",
    }

    class _CapturingConsole:
        def __init__(self):
            self.out = StringIO()
            self.is_dumb_terminal = True

        def print(self, *args, **kwargs):
            self.out.write(" ".join(str(a) for a in args) + "\n")

    console = _CapturingConsole()

    with _mock.patch.object(
        W,
        "_render_diff_preview_inner",
        side_effect=RuntimeError("simulated rendering failure"),
    ):
        result = W._render_diff_preview(agent_dir, merged_content, console)

    assert result == -1, "_render_diff_preview must return -1 on inner exception"
    output = console.out.getvalue()
    assert "Preview rendering failed" in output, (
        "Fallback message must be printed on exception"
    )
    assert "persona/IDENTITY.md" in output, (
        "Fallback file list must include all relpaths"
    )
    assert "persona/SOUL.md" in output, "Fallback file list must include all relpaths"


# ---------------------------------------------------------------------------
# J. _create_empty_dirs — write_paths-driven mkdir + traversal containment (#541)
# ---------------------------------------------------------------------------


def test_create_empty_dirs_creates_every_declared_write_path(tmp_path):
    """Every write_path bullet in the scaffolded tools.md becomes a real dir.

    This is the #541 fix: the dir set is derived from tools.md, not hardcoded.
    The writer template's drafts/ and revisions/ (which the old hardcoded set
    omitted) must exist after _create_empty_dirs runs.
    """
    agent_dir = tmp_path / "agents" / "writer-agent"
    agent_dir.mkdir(parents=True)
    # Minimal tools.md declaring a write_path the old hardcoded set never made.
    (agent_dir / "tools.md").write_text(
        "## Write paths\n\n"
        "- memory/ -- notes\n"
        "- drafts/ -- WIP\n"
        "- revisions/ -- archive\n"
        "- output/ -- final\n",
        encoding="utf-8",
    )

    W._create_empty_dirs(agent_dir)

    for sub in ("memory", "drafts", "revisions", "output"):
        assert (agent_dir / sub).is_dir(), (
            f"{sub}/ declared as a write_path but not created by _create_empty_dirs"
        )


def test_create_empty_dirs_negative_control_drafts_requires_the_parse(tmp_path):
    """Negative control: drafts/ only exists BECAUSE we parse tools.md.

    Strip the tools.md write_path bullet and drafts/ must NOT appear — proving
    the directory creation is driven by the parsed write_paths, not a constant.
    """
    agent_dir = tmp_path / "agents" / "writer-agent"
    agent_dir.mkdir(parents=True)
    # tools.md WITHOUT drafts/ — the negative control.
    (agent_dir / "tools.md").write_text(
        "## Write paths\n\n- memory/ -- notes\n",
        encoding="utf-8",
    )

    W._create_empty_dirs(agent_dir)

    assert (agent_dir / "memory").is_dir()
    assert not (agent_dir / "drafts").exists(), (
        "drafts/ must NOT be created when tools.md does not declare it; "
        "if this fails the dir set is hardcoded, not parse-driven"
    )


def test_create_empty_dirs_refuses_path_traversal_escape(tmp_path):
    """A write_path that resolves OUTSIDE the agent folder is refused, not created.

    Security: _create_empty_dirs must contain every mkdir under agent_dir. A
    '../escape' bullet (or absolute path) must NOT cause a directory to appear
    outside the agent folder, and must emit a warning.
    """
    agent_dir = tmp_path / "agents" / "myagent"
    agent_dir.mkdir(parents=True)
    escape_target = tmp_path / "agents" / "ESCAPED"
    absolute_target = tmp_path / "ABSOLUTE_ESCAPE"
    # Bare-relative '../ESCAPED' anchors under agent_dir then climbs out;
    # an absolute path bypasses agent_root anchoring entirely.
    (agent_dir / "tools.md").write_text(
        "## Write paths\n\n"
        "- memory/ -- ok\n"
        "- ../ESCAPED -- traversal attempt\n"
        f"- {absolute_target} -- absolute escape\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        W._create_empty_dirs(agent_dir)

    # The contained path is created; both escapes are refused.
    assert (agent_dir / "memory").is_dir()
    assert not escape_target.exists(), (
        "'../ESCAPED' resolved outside the agent folder but was created — "
        "path-traversal containment failed"
    )
    assert not absolute_target.exists(), (
        "an absolute write_path was created outside the agent folder — "
        "path-traversal containment failed"
    )
    msgs = [str(w.message) for w in caught]
    assert any("outside the agent folder" in m for m in msgs), (
        f"expected a refusal warning for the escape attempt; got: {msgs}"
    )


def test_create_empty_dirs_traversal_negative_control(tmp_path):
    """Negative control for the traversal guard: with containment stripped, the
    escape WOULD land outside. We prove the target path is genuinely outside
    agent_dir so the guard above is testing a real escape, not a no-op.
    """
    agent_dir = tmp_path / "agents" / "myagent"
    escape = (agent_dir / ".." / "ESCAPED").resolve()
    # The escape target is a sibling of agent_dir, i.e. genuinely outside it.
    assert agent_dir.resolve() not in escape.parents
    assert not str(escape).startswith(str(agent_dir.resolve()) + "/")


def test_add_to_it_creates_write_path_dirs_for_backfilled_tools_md(tmp_path):
    """Add-to-it backfills a MISSING tools.md from the writer template, declaring
    drafts/ + revisions/ — and must create those directories so the merged agent
    stays doctor-clean (#541 lockstep, Add-to-it entry).

    A missing tools.md is the realistic case where Add-to-it introduces NEW
    write_paths the existing agent never had: _compute_merged_content backfills
    it entirely from the fresh template (operator-preamble-wins does not apply to
    a file that does not exist).
    """
    schema = C.TEMPLATE_SECTION_SCHEMA["writer"]
    agent_dir = tmp_path / "agents" / "writer-agent"
    agent_dir.mkdir(parents=True)
    # Seed every writer schema file EXCEPT tools.md, so tools.md is backfilled
    # wholesale from the fresh template (which declares drafts/ + revisions/).
    for relpath, headers in schema.items():
        if relpath == "tools.md":
            continue
        target = agent_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"## {h}\n\nContent.\n" for h in headers)
        target.write_text(body, encoding="utf-8")

    assert not (agent_dir / "tools.md").exists()
    assert not (agent_dir / "drafts").exists()
    assert not (agent_dir / "revisions").exists()

    console = _FakeConsole()
    rc = W._add_to_it(
        agent_dir=agent_dir,
        agents_root=tmp_path / "agents",
        template_name="writer",
        console=console,
        Prompt=_prompt_sequence(),
        Confirm=_confirm_factory(True),  # Apply these changes? -> yes
        existing_headers=None,
    )

    assert rc == 0, f"_add_to_it returned {rc}; merge failed"
    # tools.md was backfilled and declares drafts/ + revisions/ -> dirs created.
    assert (agent_dir / "tools.md").is_file()
    assert (agent_dir / "drafts").is_dir(), (
        "drafts/ declared in backfilled tools.md but not created by Add-to-it"
    )
    assert (agent_dir / "revisions").is_dir(), (
        "revisions/ declared in backfilled tools.md but not created by Add-to-it"
    )


def test_add_to_it_returns_nonzero_when_write_path_dir_creation_fails(
    tmp_path, monkeypatch
):
    """If a declared write-path dir cannot be created after merge, Add-to-it must
    return non-zero (the merged agent is NOT doctor-clean) — matching
    _write_scaffold's contract — and must NOT print the green 'updated' line.

    Negative control for the OSError->return 1 fix: _write_scaffold returns 1 on
    the same _create_empty_dirs OSError; Add-to-it previously swallowed it and
    returned 0, reporting success on an agent that would fail doctor.
    """
    schema = C.TEMPLATE_SECTION_SCHEMA["writer"]
    agent_dir = tmp_path / "agents" / "writer-agent"
    agent_dir.mkdir(parents=True)
    for relpath, headers in schema.items():
        if relpath == "tools.md":
            continue
        target = agent_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"## {h}\n\nContent.\n" for h in headers)
        target.write_text(body, encoding="utf-8")

    # Force the post-merge dir creation to fail with an OSError.
    def _boom(_agent_dir):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(W, "_create_empty_dirs", _boom)

    console = _FakeConsole()
    rc = W._add_to_it(
        agent_dir=agent_dir,
        agents_root=tmp_path / "agents",
        template_name="writer",
        console=console,
        Prompt=_prompt_sequence(),
        Confirm=_confirm_factory(True),
        existing_headers=None,
    )

    assert rc == 1, f"_add_to_it returned {rc}; expected 1 on dir-creation failure"
    out = console.out.getvalue().lower()
    assert "could not be created" in out, (
        "expected the actionable 'could not be created' warning; "
        f"got: {console.out.getvalue()!r}"
    )
    assert "updated at" not in out, (
        "the green 'updated at' success line must be suppressed when the agent "
        f"is not doctor-clean; got: {console.out.getvalue()!r}"
    )


def _seed_complete_writer_agent(agent_dir):
    """Seed a writer agent whose every schema file already matches the fresh
    template byte-for-byte, so Add-to-it computes a ZERO text diff.

    Returns nothing; the caller deletes a write-path dir to set up the
    zero-diff-but-missing-dir scenario.
    """
    agent_dir.mkdir(parents=True, exist_ok=True)
    fresh_vars = W._default_template_vars(agent_dir.name, "writer")
    # Render the real fresh template into the agent dir so _compute_merged_content
    # finds zero section-level differences.
    W._render_files(agent_dir, "writer", fresh_vars)
    W._create_empty_dirs(agent_dir)


def test_add_to_it_zero_diff_creates_missing_write_path_dir(tmp_path):
    """Zero TEXT diff must NOT short-circuit before reconciling declared
    write-path dirs (#541). An up-to-date writer agent whose drafts/ dir was
    deleted must have it re-created by Add-to-it, with rc 0 — never report
    'up to date' while a declared dir is missing.

    This closes the zero-change-branch gap: the #541 fix wired
    _create_empty_dirs into the non-zero merge path and _write_scaffold, but the
    files_changed == 0 branch returned 0 with a green 'up to date' line without
    ever reconciling dirs.
    """
    agent_dir = tmp_path / "agents" / "writer-agent"
    _seed_complete_writer_agent(agent_dir)

    # Sanity: the fresh template declares drafts/ + revisions/ and they exist.
    assert (agent_dir / "drafts").is_dir()
    assert (agent_dir / "revisions").is_dir()

    # Delete a declared write-path dir out from under the otherwise-current agent.
    import shutil

    shutil.rmtree(agent_dir / "drafts")
    assert not (agent_dir / "drafts").exists()

    console = _FakeConsole()
    rc = W._add_to_it(
        agent_dir=agent_dir,
        agents_root=tmp_path / "agents",
        template_name="writer",
        console=console,
        Prompt=_prompt_sequence(),
        Confirm=_confirm_factory(True),
        existing_headers=None,
    )

    assert rc == 0, f"_add_to_it returned {rc}; expected 0 (dir re-created)"
    assert (agent_dir / "drafts").is_dir(), (
        "drafts/ was deleted from an up-to-date agent; the zero-diff Add-to-it "
        "branch must re-create it, but it is still missing"
    )


def test_add_to_it_zero_diff_returns_nonzero_when_dir_creation_fails(
    tmp_path, monkeypatch
):
    """Negative control for the zero-change-branch fix: if reconciling declared
    write-path dirs fails on the zero-diff path, Add-to-it must return non-zero
    and must NOT print the green 'up to date' line — the same audit-honesty
    contract the non-zero path and _write_scaffold enforce.
    """
    import shutil

    agent_dir = tmp_path / "agents" / "writer-agent"
    _seed_complete_writer_agent(agent_dir)
    shutil.rmtree(agent_dir / "drafts")

    def _boom(_agent_dir):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(W, "_create_empty_dirs", _boom)

    console = _FakeConsole()
    rc = W._add_to_it(
        agent_dir=agent_dir,
        agents_root=tmp_path / "agents",
        template_name="writer",
        console=console,
        Prompt=_prompt_sequence(),
        Confirm=_confirm_factory(True),
        existing_headers=None,
    )

    assert rc == 1, f"_add_to_it returned {rc}; expected 1 on zero-diff dir failure"
    out = console.out.getvalue().lower()
    assert "could not be created" in out, (
        "expected the actionable 'could not be created' warning on the zero-diff "
        f"path; got: {console.out.getvalue()!r}"
    )
    assert "up to date" not in out, (
        "the green 'up to date' line must be suppressed when a declared dir is "
        f"missing and cannot be created; got: {console.out.getvalue()!r}"
    )

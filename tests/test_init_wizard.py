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
# K. Add-to-it dispatch + detection (_detect_sections, _check_stale_staging_dirs)
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


def test_check_stale_staging_dirs_finds_glob(tmp_path):
    """A leftover .new.* sibling is detected and returned."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    staging = tmp_path / "my-agent.new.20260101T000000_000000Z"
    staging.mkdir()

    console = _FakeConsole()
    # Operator confirms deletion so the function returns True.
    result = W._check_stale_staging_dirs(agent_dir, console, _confirm_factory(True))

    assert result is True
    # The staging dir should have been deleted by the function.
    assert not staging.exists()


def test_check_stale_staging_dirs_empty_when_none(tmp_path):
    """No stale staging dirs means _check_stale_staging_dirs returns True immediately."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()

    console = _FakeConsole()
    prompt_called = []

    class WatchConfirm:
        @classmethod
        def ask(cls, *a, **kw):
            prompt_called.append(True)
            return False

    result = W._check_stale_staging_dirs(agent_dir, console, WatchConfirm)

    assert result is True
    assert not prompt_called, (
        "Confirm.ask should not be called when no staging dirs exist"
    )


# ---------------------------------------------------------------------------
# L. Staging-dir commit + rollback (_commit_add_to_it)
# ---------------------------------------------------------------------------


def test_commit_add_to_it_success_rmtrees_bak(tmp_path):
    """On success, staged content appears in agent_dir and the backup is removed."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    (agent_dir / "original.txt").write_text("old content")

    staging_dir = tmp_path / "my-agent.new.20260101T000000_000000Z"
    staging_dir.mkdir()
    (staging_dir / "new-file.txt").write_text("staged content")

    console = _FakeConsole()
    W._commit_add_to_it(agent_dir, staging_dir, console)

    # Staged content should be at agent_dir.
    assert (agent_dir / "new-file.txt").exists()
    assert (agent_dir / "new-file.txt").read_text() == "staged content"

    # No backup should remain.
    bak_dirs = list(tmp_path.glob("my-agent.bak.*"))
    assert not bak_dirs, f"Backup dirs not cleaned up: {bak_dirs}"


def test_commit_add_to_it_failure_restores_original(tmp_path, monkeypatch):
    """When the staging rename fails, the original agent_dir content is restored."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    sentinel = agent_dir / "original.txt"
    sentinel.write_text("preserved content")

    staging_dir = tmp_path / "my-agent.new.20260101T000000_000000Z"
    staging_dir.mkdir()
    (staging_dir / "new-file.txt").write_text("staged content")

    # Track how many times rename is called; fail on the second call (staging -> agent).
    rename_calls = []
    original_rename = Path.rename

    def _flaky_rename(self, target):
        rename_calls.append((self, target))
        if len(rename_calls) == 2:
            raise OSError("Simulated rename failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _flaky_rename)

    with pytest.raises(OSError):
        W._commit_add_to_it(agent_dir, staging_dir, console=_FakeConsole())

    # Original content must be restored.
    assert agent_dir.exists(), "agent_dir must be restored after failure"
    assert sentinel.exists(), "original sentinel file must be restored"
    assert sentinel.read_text() == "preserved content"


def test_commit_add_to_it_ki_safe(tmp_path, monkeypatch):
    """KeyboardInterrupt during staging rename restores agent_dir and re-raises."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    (agent_dir / "original.txt").write_text("preserved")

    staging_dir = tmp_path / "my-agent.new.20260101T000000_000000Z"
    staging_dir.mkdir()

    rename_calls = []
    original_rename = Path.rename

    def _ki_on_second(self, target):
        rename_calls.append((self, target))
        if len(rename_calls) == 2:
            raise KeyboardInterrupt
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _ki_on_second)

    with pytest.raises(KeyboardInterrupt):
        W._commit_add_to_it(agent_dir, staging_dir, console=_FakeConsole())

    # agent_dir must be restored.
    assert agent_dir.exists(), "agent_dir must be restored after KeyboardInterrupt"
    assert (agent_dir / "original.txt").read_text() == "preserved"


# ---------------------------------------------------------------------------
# M. CRLF + BOM normalization (_render_diff_preview)
# ---------------------------------------------------------------------------


def test_render_diff_preview_normalizes_crlf(tmp_path):
    """Existing file with CRLF and staged file with LF (same logical content) shows no diff."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    staging_dir = tmp_path / "my-agent.new.ts"
    staging_dir.mkdir()

    shared_content = "## Hello\n\nWorld.\n"
    crlf_content = shared_content.replace("\n", "\r\n")

    (agent_dir / "notes.md").write_bytes(crlf_content.encode("utf-8"))
    (staging_dir / "notes.md").write_text(shared_content, encoding="utf-8")

    console = _FakeConsole()
    files_changed, ins, dels = W._render_diff_preview(agent_dir, staging_dir, console)

    assert files_changed == 0, (
        "CRLF normalization should make CRLF vs LF files show as identical"
    )


def test_render_diff_preview_strips_utf8_bom(tmp_path):
    """Existing file with UTF-8 BOM and staged file without (same text) shows no diff."""
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    staging_dir = tmp_path / "my-agent.new.ts"
    staging_dir.mkdir()

    shared_content = "## Hello\n\nWorld.\n"
    bom_bytes = b"\xef\xbb\xbf" + shared_content.encode("utf-8")

    (agent_dir / "notes.md").write_bytes(bom_bytes)
    (staging_dir / "notes.md").write_text(shared_content, encoding="utf-8")

    console = _FakeConsole()
    files_changed, ins, dels = W._render_diff_preview(agent_dir, staging_dir, console)

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

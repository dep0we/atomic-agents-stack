"""ATOMIC_AGENTS_ROOT is required; there is no silent default under HOME."""

from __future__ import annotations

import pytest

from atomic_agents._platform import AgentsRootNotSetError, get_agents_root
from atomic_agents.doctor import FAIL, check_env, run_doctor


@pytest.mark.parametrize("value", [None, ""])
def test_missing_variable_raises_clear_error(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ATOMIC_AGENTS_ROOT", raising=False)
    else:
        monkeypatch.setenv("ATOMIC_AGENTS_ROOT", value)
    with pytest.raises(AgentsRootNotSetError) as exc:
        get_agents_root()
    assert "ATOMIC_AGENTS_ROOT" in str(exc.value)
    assert "~/.config/data-roots.env" in str(exc.value)


def test_variable_is_used(monkeypatch, tmp_path):
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    assert get_agents_root() == tmp_path.resolve()


def test_doctor_reports_unset_variable(monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_ROOT", raising=False)
    result = check_env(None)
    assert result.status == FAIL
    assert "data-roots.env" in result.fix_hint
    results = run_doctor()
    assert results[0].name == "env" and results[0].status == FAIL


def test_cli_prints_message_not_traceback(monkeypatch, capsys):
    from atomic_agents import cli

    monkeypatch.delenv("ATOMIC_AGENTS_ROOT", raising=False)
    rc = cli.main(["run", "foo", "--work-item", "x"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ATOMIC_AGENTS_ROOT is not set" in err
    assert "Traceback" not in err


# ---- file fallback (text-only read of ~/.config/data-roots.env) ----


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """Point DATA_ROOTS_ENV at a temp file and clear the variable."""
    path = tmp_path / "data-roots.env"
    monkeypatch.setenv("DATA_ROOTS_ENV", str(path))
    monkeypatch.delenv("ATOMIC_AGENTS_ROOT", raising=False)
    return path


def test_env_var_wins_over_file(monkeypatch, env_file, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    env_file.write_text(f"export ATOMIC_AGENTS_ROOT={tmp_path / 'b'}\n")
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path / "a"))
    assert get_agents_root() == (tmp_path / "a").resolve()


def test_file_used_when_variable_unset(env_file, tmp_path):
    env_file.write_text(f"ATOMIC_AGENTS_ROOT={tmp_path}\n")
    assert get_agents_root() == tmp_path.resolve()


def test_file_used_when_variable_empty(monkeypatch, env_file, tmp_path):
    env_file.write_text(f"ATOMIC_AGENTS_ROOT={tmp_path}\n")
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", "")
    assert get_agents_root() == tmp_path.resolve()


def test_export_prefix_and_comments(env_file, tmp_path):
    env_file.write_text(
        "# roots\n"
        "export VAULT_ROOT=/not/this\n"
        f"export ATOMIC_AGENTS_ROOT={tmp_path}  # agents live here\n"
    )
    assert get_agents_root() == tmp_path.resolve()


@pytest.mark.parametrize("q", ["'", '"'])
def test_quoted_values(env_file, tmp_path, q):
    env_file.write_text(f"export ATOMIC_AGENTS_ROOT={q}{tmp_path}{q}\n")
    assert get_agents_root() == tmp_path.resolve()


def test_only_atomic_agents_root_is_read(env_file, tmp_path):
    env_file.write_text("export VAULT_ROOT=/somewhere\nATOMIC_AGENTS_ROOT_X=/x\n")
    with pytest.raises(AgentsRootNotSetError):
        get_agents_root()


@pytest.mark.parametrize(
    "line", ["ATOMIC_AGENTS_ROOT=", "export ATOMIC_AGENTS_ROOT=''"]
)
def test_empty_file_value_counts_as_missing(env_file, line):
    env_file.write_text(line + "\n")
    with pytest.raises(AgentsRootNotSetError):
        get_agents_root()


def test_file_is_never_executed(env_file, tmp_path):
    marker = tmp_path / "ran"
    env_file.write_text(
        f"touch {marker}\n"
        f"$(touch {marker})\n"
        f"`touch {marker}`\n"
        f"export ATOMIC_AGENTS_ROOT={tmp_path}\n"
    )
    assert get_agents_root() == tmp_path.resolve()
    assert not marker.exists()


def test_both_missing_raises(env_file):
    with pytest.raises(AgentsRootNotSetError) as exc:
        get_agents_root()
    assert "export ATOMIC_AGENTS_ROOT=" in str(exc.value)


def test_doctor_reports_environment_source(monkeypatch, tmp_path):
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    result = check_env(None)
    assert "source: environment" in result.message


def test_doctor_reports_file_source(env_file, tmp_path):
    env_file.write_text(f"export ATOMIC_AGENTS_ROOT={tmp_path}\n")
    result = check_env(None)
    assert result.status != FAIL
    assert f"source: file {env_file}" in result.message

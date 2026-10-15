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

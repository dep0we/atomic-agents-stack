"""ATOMIC_AGENTS_ROOT may list several directories, like PATH (DAR-229)."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from atomic_agents import cli
from atomic_agents._platform import (
    AgentRootConflictError,
    AgentsRootNotSetError,
    get_agents_root,
    get_agents_roots,
    resolve_agents_root_for,
)
from atomic_agents.agent import AtomicAgent
from atomic_agents.deploy import _launchd
from atomic_agents.doctor import FAIL, PASS, check_env, run_doctor
from atomic_agents.exceptions import AtomicAgentsError


def _roots(monkeypatch, *paths: Path) -> str:
    value = os.pathsep.join(str(p) for p in paths)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", value)
    return value


@pytest.fixture
def two_roots(tmp_path, monkeypatch):
    a = tmp_path / "roots_a"
    b = tmp_path / "roots_b"
    a.mkdir()
    b.mkdir()
    for root, name in ((a, "alpha"), (b, "beta")):
        (root / name / "persona").mkdir(parents=True)
        (root / name / "persona" / "IDENTITY.md").write_text("# test\n")
    _roots(monkeypatch, a, b)
    return a.resolve(), b.resolve()


# ---- single root: unchanged behavior ----


def test_single_root_unchanged(tmp_path, monkeypatch):
    (tmp_path / "solo").mkdir()
    _roots(monkeypatch, tmp_path)
    assert get_agents_roots() == [tmp_path.resolve()]
    assert get_agents_root() == tmp_path.resolve()
    assert resolve_agents_root_for("solo") == tmp_path.resolve()


def test_single_root_missing_agent_falls_back_to_that_root(tmp_path, monkeypatch):
    _roots(monkeypatch, tmp_path)
    assert resolve_agents_root_for("ghost") == tmp_path.resolve()


def test_single_root_doctor_message_unchanged(tmp_path, monkeypatch):
    _roots(monkeypatch, tmp_path)
    result = check_env(None)
    assert result.status == PASS
    assert result.message.startswith(f"agents-root resolves to {tmp_path.resolve()}")
    assert result.detail["roots"] == [
        {"path": str(tmp_path.resolve()), "exists": True, "is_dir": True}
    ]


# ---- multiple roots ----


def test_multiple_roots_in_order(two_roots):
    a, b = two_roots
    assert get_agents_roots() == [a, b]
    assert get_agents_root() == a  # primary root = first


def test_empty_entries_and_duplicates_are_dropped(tmp_path, monkeypatch):
    sep = os.pathsep
    monkeypatch.setenv(
        "ATOMIC_AGENTS_ROOT", f"{sep}{tmp_path}{sep}{sep}{tmp_path}{sep}"
    )
    assert get_agents_roots() == [tmp_path.resolve()]


def test_agent_found_in_each_root(two_roots):
    a, b = two_roots
    assert resolve_agents_root_for("alpha") == a
    assert resolve_agents_root_for("beta") == b


def test_unknown_agent_resolves_to_first_root(two_roots):
    a, _b = two_roots
    assert resolve_agents_root_for("ghost") == a


def test_atomic_agent_uses_root_holding_the_agent(two_roots):
    a, b = two_roots
    assert AtomicAgent(name="alpha").agent_root == a / "alpha"
    assert AtomicAgent(name="beta").agent_root == b / "beta"


def test_explicit_agents_root_still_wins(two_roots):
    a, _b = two_roots
    with pytest.raises(AtomicAgentsError, match="Agent folder not found"):
        AtomicAgent(name="beta", agents_root=a)


def test_file_source_supports_multiple_roots(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    env_file = tmp_path / "data-roots.env"
    env_file.write_text(f"export ATOMIC_AGENTS_ROOT={a}{os.pathsep}{b}\n")
    monkeypatch.setenv("DATA_ROOTS_ENV", str(env_file))
    monkeypatch.delenv("ATOMIC_AGENTS_ROOT", raising=False)
    assert get_agents_roots() == [a.resolve(), b.resolve()]


# ---- duplicate names ----


def test_duplicate_agent_name_is_an_error_naming_both_paths(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "twin").mkdir(parents=True)
    (b / "twin").mkdir(parents=True)
    _roots(monkeypatch, a, b)
    with pytest.raises(AgentRootConflictError) as exc:
        resolve_agents_root_for("twin")
    msg = str(exc.value)
    assert str((a / "twin").resolve()) in msg
    assert str((b / "twin").resolve()) in msg


def test_duplicate_surfaces_through_agent_and_cli(tmp_path, monkeypatch, capsys):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "twin").mkdir(parents=True)
    (b / "twin").mkdir(parents=True)
    _roots(monkeypatch, a, b)
    with pytest.raises(AgentRootConflictError):
        AtomicAgent(name="twin")
    rc = cli.main(["info", "twin"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "more than one agents root" in err
    assert "Traceback" not in err


def test_doctor_reports_duplicate_agent(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "twin").mkdir(parents=True)
    (b / "twin").mkdir(parents=True)
    _roots(monkeypatch, a, b)
    results = run_doctor("twin")
    conflict = [r for r in results if r.name == "agent-root"]
    assert conflict and conflict[0].status == FAIL


# ---- unset ----


@pytest.mark.parametrize("value", [None, "", os.pathsep])
def test_unset_or_empty_is_a_clear_error(monkeypatch, tmp_path, value):
    monkeypatch.setenv("DATA_ROOTS_ENV", str(tmp_path / "absent.env"))
    if value is None:
        monkeypatch.delenv("ATOMIC_AGENTS_ROOT", raising=False)
    else:
        monkeypatch.setenv("ATOMIC_AGENTS_ROOT", value)
    for call in (get_agents_roots, get_agents_root):
        with pytest.raises(AgentsRootNotSetError) as exc:
            call()
        assert "ATOMIC_AGENTS_ROOT is not set" in str(exc.value)
        assert "export ATOMIC_AGENTS_ROOT=" in str(exc.value)
    with pytest.raises(AgentsRootNotSetError):
        resolve_agents_root_for("anything")


def test_no_home_default_is_used(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    monkeypatch.setenv("DATA_ROOTS_ENV", str(tmp_path / "absent.env"))
    monkeypatch.delenv("ATOMIC_AGENTS_ROOT", raising=False)
    with pytest.raises(AgentsRootNotSetError):
        get_agents_roots()


# ---- doctor ----


def test_doctor_reports_each_root(two_roots):
    a, b = two_roots
    result = check_env(None)
    assert result.status == PASS
    assert str(a) in result.message and str(b) in result.message
    assert [r["exists"] for r in result.detail["roots"]] == [True, True]


def test_doctor_fails_when_one_root_missing(tmp_path, monkeypatch):
    good = tmp_path / "good"
    good.mkdir()
    missing = tmp_path / "missing"
    _roots(monkeypatch, good, missing)
    result = check_env(None)
    assert result.status == FAIL
    assert "MISSING" in result.message
    assert [r["exists"] for r in result.detail["roots"]] == [True, False]


# ---- deploy passes the full value through ----


def test_plist_carries_every_root(two_roots):
    a, b = two_roots
    value = os.pathsep.join([str(a), str(b)])
    rendered = _launchd.render_plist("alpha", 8000, agents_root=value)
    env = plistlib.loads(rendered.plist_bytes)["EnvironmentVariables"]
    assert env["ATOMIC_AGENTS_ROOT"] == value


def test_plist_single_root_unchanged():
    rendered = _launchd.render_plist("alpha", 8000, agents_root=Path("/path/to/agents"))
    env = plistlib.loads(rendered.plist_bytes)["EnvironmentVariables"]
    assert env["ATOMIC_AGENTS_ROOT"] == "/path/to/agents"


def test_deploy_hands_full_value_to_the_plist(two_roots, monkeypatch):
    """The planner builds the plist value from every configured root."""
    from atomic_agents.deploy import _planner

    a, b = two_roots
    seen: dict = {}

    def fake_supervise(agent, agent_root, agents_root, **kwargs):
        seen["root_value"] = agents_root
        raise _planner.DeployError("stop here", exit_code=1)

    monkeypatch.setattr(_planner, "_step_supervise", fake_supervise)
    monkeypatch.setattr(_planner, "_step_doctor_gate", lambda *a, **k: None)
    monkeypatch.setattr(_planner, "_step_provider_key", lambda *a, **k: None)
    import io

    _planner.deploy(
        "alpha",
        assume_yes=True,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert seen["root_value"] == os.pathsep.join([str(a), str(b)])

"""Implementation-level tests for atomic_agents.deploy modules + CLI wiring.

Complements test_deploy_conformance.py (the 12-MUST table). These cover the
finer-grained behaviour of _types / _launchd / _ports / _verify / _exposure /
_conductor and the cli.py `deploy` subcommand parsing/dispatch. All system
interactions are mocked.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from atomic_agents import cli
from atomic_agents import deploy as deploy_mod
from atomic_agents.deploy import _exposure, _launchd, _ports, _verify
from atomic_agents.deploy._types import Plan, Step, StepTag


class FakeRunner:
    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def __call__(self, argv, *a, **k):
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else argv[0]
        rc, out = self.script.get(verb, (0, ""))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


# ── _types: Plan.render ──────────────────────────────────────────────────


def test_plan_render_lists_all_steps():
    plan = Plan(
        agent="x",
        steps=[
            Step(key="a", tag=StepTag.AUTO, title="first"),
            Step(key="b", tag=StepTag.CONSENT, title="second", detail="more"),
        ],
    )
    text = plan.render()
    assert "agent 'x'" in text
    assert "[auto]" in text and "first" in text
    assert "[consent]" in text and "second" in text
    assert "more" in text


def test_plan_deploy_has_seven_ordered_steps():
    plan = deploy_mod.plan_deploy("x")
    keys = [s.key for s in plan.steps]
    assert keys == [
        "preflight",
        "agent-exists",
        "doctor-gate",
        "provider-key",
        "supervise",
        "verify",
        "exposure",
    ]


# ── _launchd: label slug validation reuses init's charset ────────────────


def test_label_for_valid():
    assert _launchd.label_for("my-agent") == "ai.atomic-agents.serve.my-agent"


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "-leading", "trailing-", "has space", "under_score", "init"],
)
def test_label_for_rejects_invalid(bad):
    with pytest.raises(ValueError):
        _launchd.label_for(bad)


def test_label_for_rejects_too_long():
    with pytest.raises(ValueError):
        _launchd.label_for("a" * 65)


def test_plist_path_uses_label(tmp_path):
    p = _launchd.plist_path_for("agentx", launch_agents_dir=tmp_path)
    assert p.name == "ai.atomic-agents.serve.agentx.plist"
    assert p.parent == tmp_path


# ── _launchd: resolve_program_arguments fallback ─────────────────────────


def test_program_arguments_uses_which_when_present(monkeypatch):
    monkeypatch.setattr(_launchd.shutil, "which", lambda b: "/opt/bin/atomic-agents")
    argv = _launchd.resolve_program_arguments("a", 8000)
    assert argv[0] == "/opt/bin/atomic-agents"
    assert argv[:3] == ["/opt/bin/atomic-agents", "serve", "a"]


def test_program_arguments_falls_back_to_sys_executable(monkeypatch):
    monkeypatch.setattr(_launchd.shutil, "which", lambda b: None)
    argv = _launchd.resolve_program_arguments("a", 8000)
    assert Path(argv[0]).is_absolute()
    assert argv[1:5] == ["-m", "atomic_agents.cli", "serve", "a"]


# ── _launchd: install failure removes the orphan plist ───────────────────


def test_install_bootstrap_failure_cleans_up_plist(tmp_path):
    runner = FakeRunner(script={"print": (1, ""), "bootstrap": (1, "boom")})
    rendered = _launchd.render_plist("a", 8000, agents_root=Path("/r"))
    with pytest.raises(_launchd.DeployLaunchdError) as exc:
        _launchd.install_launchd_agent(
            "a", rendered.plist_bytes, launch_agents_dir=tmp_path, runner=runner
        )
    assert "bootstrap failed" in str(exc.value)
    # The plist we wrote is cleaned up on bootstrap failure.
    assert list(tmp_path.iterdir()) == []


def test_install_fresh_writes_plist_and_bootstraps(tmp_path):
    runner = FakeRunner(script={"print": (1, "")})  # not yet bootstrapped
    rendered = _launchd.render_plist("a", 8000, agents_root=Path("/r"))
    p = _launchd.install_launchd_agent(
        "a", rendered.plist_bytes, launch_agents_dir=tmp_path, runner=runner
    )
    assert p.exists()
    verbs = [c[1] for c in runner.calls if len(c) > 1]
    assert "bootout" not in verbs  # fresh: no prior bootout
    assert "bootstrap" in verbs


def test_teardown_idempotent_when_plist_absent(tmp_path):
    runner = FakeRunner()
    # No plist on disk; teardown is a clean no-op (no raise).
    _launchd.teardown_launchd_agent(
        "a", launch_agents_dir=tmp_path, runner=runner, remove_plist=True
    )
    assert any(c[1] == "bootout" for c in runner.calls if len(c) > 1)


# ── _launchd: status parsing ─────────────────────────────────────────────


def test_parse_launchctl_print_pid_and_exit():
    pid, last = _launchd._parse_launchctl_print("\tpid = 4321\n\tlast exit code = 0\n")
    assert pid == 4321
    assert last == 0


def test_parse_launchctl_print_tolerates_garbage():
    pid, last = _launchd._parse_launchctl_print("nonsense\nlast exit code = (never)\n")
    assert pid is None
    assert last is None


# ── _ports: malformed serve.md port must not silently fall back ──────────


def test_resolve_port_malformed_servemd_raises(tmp_path):
    root = tmp_path / "a"
    root.mkdir()
    (root / "serve.md").write_text("## Bind Port\nnotaport\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _ports.resolve_port(root, cli_port=None, environ={})


def test_resolve_port_does_not_leak_environ(tmp_path, monkeypatch):
    """resolve_port restores os.environ after the temporary swap."""
    import os

    root = tmp_path / "a"
    root.mkdir()
    monkeypatch.setenv("SENTINEL_KEEP", "1")
    _ports.resolve_port(
        root, cli_port=None, environ={"ATOMIC_AGENTS_SERVE_PORT": "7001"}
    )
    # The real environ is restored (the temporary swap did not clobber it).
    assert os.environ.get("SENTINEL_KEEP") == "1"


# ── _verify: doctor predicate recomputes exit code from body ─────────────


def test_verify_doctor_predicate_recomputes_not_trusts_200():
    """A 200 /doctor body with a failing check still fails the predicate."""

    def http_get(url):
        if url.endswith("/healthz"):
            return 200, '{"status": "ok"}'
        return 200, '{"results": [{"name": "x", "status": "fail", "message": "m"}]}'

    result = _verify.verify_deployment("a", "127.0.0.1", 8000, http_get=http_get)
    assert result.ok is False


def test_verify_short_circuits_when_healthz_down():
    """When healthz is down, /doctor is never probed."""
    probed = []

    def http_get(url):
        probed.append(url)
        return 503, '{"status": "degraded"}'

    result = _verify.verify_deployment("a", "127.0.0.1", 8000, http_get=http_get)
    assert result.ok is False
    assert all("/doctor" not in u for u in probed)


# ── _exposure: detect returns False when binary absent ───────────────────


def test_detect_tailscale_false_when_binary_absent(monkeypatch):
    monkeypatch.setattr(_exposure.shutil, "which", lambda b: None)
    # runner should never be called when the binary is absent.
    called = []

    def runner(argv, *a, **k):
        called.append(argv)

    assert _exposure.detect_tailscale(runner=runner) is False
    assert called == []


def test_detect_tailscale_false_on_nonzero(monkeypatch):
    monkeypatch.setattr(_exposure.shutil, "which", lambda b: "/usr/bin/tailscale")
    runner = FakeRunner(script={"status": (1, "")})
    assert _exposure.detect_tailscale(runner=runner) is False


# ── _conductor: missing agent fails with init hint ───────────────────────


def test_deploy_missing_agent_fails_with_init_hint(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    out, err = io.StringIO(), io.StringIO()
    rc = deploy_mod.deploy(
        "ghost",
        agents_root=root,
        assume_yes=True,
        out=out,
        err=err,
        launch_agents_dir=tmp_path / "LA",
        binder=lambda h, p: True,
    )
    assert rc == 1
    assert "atomic-agents init ghost" in err.getvalue()


def test_deploy_invalid_name_fails_before_side_effect(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    runner = FakeRunner()
    rc = deploy_mod.deploy(
        "bad name",
        agents_root=tmp_path,
        assume_yes=True,
        out=out,
        err=err,
        launchd_runner=runner,
        launch_agents_dir=tmp_path / "LA",
    )
    assert rc == 1
    assert runner.calls == []  # never reached launchctl


def test_deploy_consent_declined_aborts(tmp_path, monkeypatch):
    """Without --yes, a declined consent prompt aborts before install."""
    root = tmp_path / "agents"
    a = root / "myagent"
    a.mkdir(parents=True)
    (a / "model.md").write_text("## Default model\nclaude-opus-4-7\n", encoding="utf-8")

    import atomic_agents.doctor as doctor

    monkeypatch.setattr(doctor, "run_doctor", lambda **kw: [])
    monkeypatch.setattr(doctor, "overall_exit_code", lambda r: 0)
    monkeypatch.setattr(doctor, "check_provider_keys", lambda d: [])

    runner = FakeRunner()
    out, err = io.StringIO(), io.StringIO()
    rc = deploy_mod.deploy(
        "myagent",
        agents_root=root,
        assume_yes=False,
        prompter=lambda q: False,  # decline
        out=out,
        err=err,
        launchd_runner=runner,
        launch_agents_dir=tmp_path / "LA",
        binder=lambda h, p: True,
    )
    assert rc == 1
    assert "Aborted" in err.getvalue()
    assert runner.calls == []  # never installed


# ── cli.py: deploy subcommand parsing/dispatch ───────────────────────────


def _fake_deploy_module(monkeypatch):
    import types

    calls = []
    fake = types.SimpleNamespace()
    fake.deploy = lambda agent, **kw: (calls.append(("up", agent, kw)), 0)[1]
    fake.deploy_status = lambda agent, **kw: (calls.append(("status", agent, kw)), 0)[1]
    fake.deploy_down = lambda agent, **kw: (calls.append(("down", agent, kw)), 0)[1]

    class DeployError(Exception):
        exit_code = 1

    fake.DeployError = DeployError
    monkeypatch.setitem(__import__("sys").modules, "atomic_agents.deploy", fake)
    import atomic_agents

    monkeypatch.setattr(atomic_agents, "deploy", fake)
    return calls


def test_cli_deploy_up(monkeypatch):
    calls = _fake_deploy_module(monkeypatch)
    rc = cli.main(["deploy", "myagent"])
    assert rc == 0
    assert calls[0][0] == "up"
    assert calls[0][1] == "myagent"


def test_cli_deploy_flags_pass_through(monkeypatch):
    calls = _fake_deploy_module(monkeypatch)
    rc = cli.main(
        ["deploy", "myagent", "--plan", "--yes", "--verify-call", "--port", "9100"]
    )
    assert rc == 0
    kw = calls[0][2]
    assert kw["plan_only"] is True
    assert kw["assume_yes"] is True
    assert kw["verify_call"] is True
    assert kw["cli_port"] == 9100


def test_cli_deploy_status(monkeypatch):
    calls = _fake_deploy_module(monkeypatch)
    assert cli.main(["deploy", "status", "myagent"]) == 0
    assert calls[0][0] == "status"
    assert calls[0][1] == "myagent"


def test_cli_deploy_down(monkeypatch):
    calls = _fake_deploy_module(monkeypatch)
    assert cli.main(["deploy", "down", "myagent"]) == 0
    assert calls[0][0] == "down"
    assert calls[0][1] == "myagent"


def test_cli_deploy_no_agent_errors(monkeypatch, capsys):
    _fake_deploy_module(monkeypatch)
    rc = cli.main(["deploy"])
    assert rc == 1
    assert "requires exactly one agent" in capsys.readouterr().err


def test_cli_deploy_status_missing_agent_errors(monkeypatch, capsys):
    _fake_deploy_module(monkeypatch)
    rc = cli.main(["deploy", "status"])
    assert rc == 1
    assert "requires exactly one agent" in capsys.readouterr().err


def test_cli_deploy_too_many_positionals_errors(monkeypatch, capsys):
    _fake_deploy_module(monkeypatch)
    rc = cli.main(["deploy", "a", "b", "c"])
    assert rc == 1
    assert "exactly one agent" in capsys.readouterr().err

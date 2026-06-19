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


# ── round-1 adversarial-fix regression tests ────────────────────────────────
#
# Each test below is a NEGATIVE CONTROL for a specific fix: it FAILS if the fix
# is reverted. Comments name the fix and how stripping it re-breaks the test.


def _make_agent(tmp_path, *, model="claude-opus-4-7"):
    """Create a minimal agent folder that passes existence + provider checks."""
    root = tmp_path / "agents"
    a = root / "myagent"
    a.mkdir(parents=True)
    (a / "model.md").write_text(f"## Default model\n{model}\n", encoding="utf-8")
    return root


def _patch_doctor_pass(monkeypatch):
    import atomic_agents.doctor as doctor

    monkeypatch.setattr(doctor, "run_doctor", lambda **kw: [])
    monkeypatch.setattr(doctor, "overall_exit_code", lambda r: 0)
    monkeypatch.setattr(doctor, "check_provider_keys", lambda d: [])


def _run_full_deploy(root, tmp_path, monkeypatch, **overrides):
    """Drive a full deploy with doctor patched + all seams mocked."""
    _patch_doctor_pass(monkeypatch)
    out, err = io.StringIO(), io.StringIO()
    kw = dict(
        agents_root=root,
        assume_yes=True,
        out=out,
        err=err,
        launch_agents_dir=tmp_path / "LA",
        binder=lambda h, p: True,
        environ={"HOME": "/h", "USER": "u", "PATH": "/usr/bin"},
        verify_retries=1,
        verify_retry_delay_s=0.0,
        exposure_runner=lambda argv, *a, **k: subprocess.CompletedProcess(
            argv, 1, "", ""
        ),
    )
    kw.update(overrides)
    rc = deploy_mod.deploy("myagent", **kw)
    return rc, out.getvalue(), err.getvalue()


# Fix #1 — verify crashes without rollback (MUST 8).
def test_verify_default_http_get_swallows_urlerror():
    """_default_http_get returns the transport sentinel (never raises) on URLError.

    Negative control: drop the `except URLError` branch in _default_http_get and
    this raises instead of returning (TRANSPORT_FAILURE_STATUS, "").
    """
    import urllib.error

    def boom(url, timeout=10):
        raise urllib.error.URLError("connection refused")

    import urllib.request

    orig = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        status, body = _verify._default_http_get("http://127.0.0.1:9/agents/a/healthz")
    finally:
        urllib.request.urlopen = orig
    assert status == _verify.TRANSPORT_FAILURE_STATUS
    assert body == ""


def test_deploy_verify_raising_http_get_still_rolls_back(tmp_path, monkeypatch):
    """If http_get RAISES (e.g. URLError leaking through a custom seam), deploy
    MUST still bootout the agent + remove the plist + exit non-zero (MUST 8).

    Negative control: remove the try/except wrapping verify_deployment in the
    conductor and the exception propagates uncaught — the plist is left behind
    and this assertion (empty LA dir) fails.
    """
    import urllib.error

    root = _make_agent(tmp_path)
    runner = FakeRunner(script={"print": (1, "")})  # not bootstrapped initially

    def raising_get(url):
        raise urllib.error.URLError("refused")

    rc, _out, err = _run_full_deploy(
        root,
        tmp_path,
        monkeypatch,
        launchd_runner=runner,
        http_get=raising_get,
    )
    assert rc != 0
    assert "rolled back" in err or "could not complete" in err
    # plist removed by rollback (no bootstrapped-but-broken service left behind)
    la = tmp_path / "LA"
    assert list(la.iterdir()) == []
    # bootout was issued during rollback
    assert any(c[1] == "bootout" for c in runner.calls if len(c) > 1)


def test_deploy_production_verify_defaults_are_warmup_window():
    """deploy() defaults retries≈10 / delay≈0.5s for the launchd warm-up window.

    Negative control: revert the defaults to retries=1/delay=0.0 and this fails.
    """
    import inspect

    sig = inspect.signature(deploy_mod.deploy)
    assert sig.parameters["verify_retries"].default >= 5
    assert sig.parameters["verify_retry_delay_s"].default > 0.0


# Fix #2 — doctor predicate false-pass (MUST 9). Multi-part fix: each branch
# (non-2xx status, error-shaped body, missing results key) has its OWN negative
# control whose payload is caught ONLY by that branch, so stripping any single
# branch turns its control red (per the "strip each part separately" rule).
def test_check_doctor_non_2xx_with_passing_results_fails():
    """A 500 whose body is an otherwise-passing results list still FAILs.

    Targets the non-2xx branch ONLY: the body parses to all-pass checks, so the
    error-body and missing-key branches do NOT catch it. Negative control: drop
    the `if not (200 <= status < 300)` guard and this becomes a false PASS.
    """
    ok, msg = _verify._check_doctor(
        500, '{"results": [{"name": "env", "status": "pass", "message": ""}]}'
    )
    assert ok is False
    assert "HTTP 500" in msg


def test_check_doctor_missing_results_key_fails():
    """A 200 body with no results key is a FAIL (no-checks != no-failures)."""
    ok, _msg = _verify._check_doctor(200, '{"summary": {"all_ok": true}}')
    assert ok is False


def test_check_doctor_error_body_with_results_key_fails():
    """A 200 {"status":"error", "results":[...all pass...]} still FAILs.

    Targets the error-body branch ONLY: it carries a valid passing results key,
    so neither the non-2xx nor the missing-key branch catches it. Negative
    control: drop the error-shaped-body guard and this becomes a false PASS.
    """
    ok, _msg = _verify._check_doctor(
        200,
        '{"status": "error", "error": "boom", '
        '"results": [{"name": "env", "status": "pass", "message": ""}]}',
    )
    assert ok is False


def test_check_doctor_well_formed_pass_still_passes():
    """Positive control: a real results list with no failures still passes."""
    ok, _msg = _verify._check_doctor(
        200, '{"results": [{"name": "env", "status": "pass", "message": ""}]}'
    )
    assert ok is True


# Fix #3 — env-var-only key injection (MUST 5), THROUGH the conductor.
def test_deploy_env_only_key_injected_into_plist(tmp_path, monkeypatch):
    """When the provider key's sole source is an env var, deploy injects it into
    the plist (KEY=VALUE) and prints the cleartext caveat.

    Negative control: revert _step_supervise to call render_plist without
    plaintext_key and the env key never lands in the plist (assertion fails).
    """
    # Force "env is the sole source": no keychain / keys.json hit.
    monkeypatch.setattr(
        "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
        lambda name: None,
    )
    monkeypatch.setattr(
        "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
        lambda key: None,
    )

    root = _make_agent(tmp_path)
    runner = FakeRunner(script={"print": (1, "")})
    rc, _out, err = _run_full_deploy(
        root,
        tmp_path,
        monkeypatch,
        launchd_runner=runner,
        environ={
            "HOME": "/h",
            "USER": "u",
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-env-only",
        },
        http_get=_healthz_ok_doctor_ok_modtest,
    )
    assert rc == 0
    assert "cleartext" in err  # caveat printed
    import plistlib

    plist = next((tmp_path / "LA").iterdir())
    pd = plistlib.loads(plist.read_bytes())
    assert pd["EnvironmentVariables"].get("ANTHROPIC_API_KEY") == "sk-env-only"


def test_deploy_keychain_key_not_injected_into_plist(tmp_path, monkeypatch):
    """When the key is reachable from Keychain, deploy MUST NOT write it into the
    plist (serve reads Keychain at runtime).

    Negative control: if _resolve_env_only_provider_key ignored the non-env
    sources and injected on any env hit, the keychain value would leak; here the
    env var IS set too, but the keychain hit must suppress injection.
    """
    monkeypatch.setattr(
        "atomic_agents.secret_backend.filesystem._resolve_from_keychain",
        lambda name: "sk-from-keychain",
    )
    monkeypatch.setattr(
        "atomic_agents.secret_backend.filesystem._resolve_from_keys_json",
        lambda key: None,
    )
    root = _make_agent(tmp_path)
    runner = FakeRunner(script={"print": (1, "")})
    rc, _out, err = _run_full_deploy(
        root,
        tmp_path,
        monkeypatch,
        launchd_runner=runner,
        environ={
            "HOME": "/h",
            "USER": "u",
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-env-too",
        },
        http_get=_healthz_ok_doctor_ok_modtest,
    )
    assert rc == 0
    assert "cleartext" not in err
    import plistlib

    plist = next((tmp_path / "LA").iterdir())
    pd = plistlib.loads(plist.read_bytes())
    env = pd["EnvironmentVariables"]
    assert "ANTHROPIC_API_KEY" not in env  # not injected — keychain source exists
    serialized = plistlib.dumps(pd).decode("utf-8")
    assert "sk-from-keychain" not in serialized
    assert "sk-env-too" not in serialized


# Fix #4 — bootout errors ignored (MUST 7/8/12).
def test_teardown_raises_on_real_bootout_failure(tmp_path):
    """A non-"not found" bootout failure raises; the plist is NOT removed.

    Negative control: revert teardown to ignore the returncode and this raise
    never happens (and the plist would be removed despite a still-loaded service).
    """
    label = _launchd.label_for("myagent")
    plist = tmp_path / f"{label}.plist"
    plist.write_bytes(b"<plist></plist>")
    # returncode 1 with an EPERM-shaped stderr is a REAL failure, not "absent".
    runner = FakeRunner(script={"bootout": (1, "Operation not permitted")})
    with pytest.raises(_launchd.DeployLaunchdError):
        _launchd.teardown_launchd_agent(
            "myagent", launch_agents_dir=tmp_path, runner=runner, remove_plist=True
        )
    assert plist.exists()  # plist preserved — teardown could not confirm


def test_teardown_tolerates_service_not_found(tmp_path):
    """Positive control: the known "could not find" bootout case is tolerated."""
    runner = FakeRunner(script={"bootout": (3, "Could not find specified service")})
    # no raise; clean no-op teardown
    _launchd.teardown_launchd_agent(
        "myagent", launch_agents_dir=tmp_path, runner=runner, remove_plist=True
    )


# Fix #5 — shutil.which relative path (MUST 4).
def test_program_arguments_rejects_relative_which(monkeypatch):
    """A RELATIVE which() result is not trusted; fall back to sys.executable.

    Negative control: revert resolve_program_arguments to use the raw `console`
    and argv[0] becomes the relative path (this assertion fails).
    """
    monkeypatch.setattr(_launchd.shutil, "which", lambda b: "bin/atomic-agents")
    argv = _launchd.resolve_program_arguments("a", 8000)
    assert Path(argv[0]).is_absolute()
    # fell back to the interpreter module form (not the relative which hit)
    assert argv[1:5] == ["-m", "atomic_agents.cli", "serve", "a"]


# Fix #6 — step-2 init handoff.
def test_deploy_missing_agent_consent_hands_off_to_init(tmp_path, monkeypatch):
    """Without --yes, a missing agent prompts + hands off to init (consent path).

    Negative control: revert _step_agent_exists to always raise and the init
    runner is never called (this assertion fails).
    """
    _patch_doctor_pass(monkeypatch)
    root = tmp_path / "agents"
    root.mkdir()
    init_calls = []

    def fake_init(agent, agents_root):
        init_calls.append((agent, agents_root))
        (root / agent).mkdir(parents=True)  # init creates the folder
        (root / agent / "model.md").write_text("## Default model\nx\n")
        return 0

    runner = FakeRunner(script={"print": (1, "")})
    out, err = io.StringIO(), io.StringIO()
    rc = deploy_mod.deploy(
        "ghost",
        agents_root=root,
        assume_yes=False,
        prompter=lambda q: True,  # consent to the handoff
        init_runner=fake_init,
        out=out,
        err=err,
        launch_agents_dir=tmp_path / "LA",
        launchd_runner=runner,
        binder=lambda h, p: True,
        environ={"HOME": "/h", "USER": "u", "PATH": "/usr/bin"},
        http_get=_healthz_ok_doctor_ok_modtest,
        verify_retries=1,
        exposure_runner=lambda argv, *a, **k: subprocess.CompletedProcess(
            argv, 1, "", ""
        ),
    )
    assert init_calls == [("ghost", root)]
    assert rc == 0


def test_deploy_missing_agent_yes_fails_fast(tmp_path, monkeypatch):
    """With --yes, a missing agent fails fast and never invokes init."""
    _patch_doctor_pass(monkeypatch)
    root = tmp_path / "agents"
    root.mkdir()
    init_calls = []
    rc = deploy_mod.deploy(
        "ghost",
        agents_root=root,
        assume_yes=True,
        init_runner=lambda a, r: init_calls.append(a) or 0,
        out=io.StringIO(),
        err=io.StringIO(),
        launch_agents_dir=tmp_path / "LA",
        binder=lambda h, p: True,
        environ={"HOME": "/h", "USER": "u", "PATH": "/usr/bin"},
    )
    assert rc == 1
    assert init_calls == []  # fail-fast — init never run


# Fix #7 — port range validation.
@pytest.mark.parametrize("bad", [0, -1, 65536, 99999])
def test_resolve_port_rejects_out_of_range(tmp_path, bad):
    """A port outside 1..65535 raises PortRangeError before any probe.

    Negative control: drop _validate_port_range and these return the bad port.
    """
    root = tmp_path / "a"
    root.mkdir()
    with pytest.raises(_ports.PortRangeError):
        _ports.resolve_port(root, cli_port=bad, environ={})


def test_resolve_port_accepts_valid(tmp_path):
    """Positive control: a valid port resolves unchanged."""
    root = tmp_path / "a"
    root.mkdir()
    assert _ports.resolve_port(root, cli_port=8000, environ={}) == 8000


# Fix #8 — post-bootstrap address-in-use.
def test_deploy_verify_fail_reports_address_in_use(tmp_path, monkeypatch):
    """On a verify failure where the port is now bound, deploy reports
    "address in use :<port>" before rollback.

    Negative control: drop the re-probe in _rollback_and_report and the
    address-in-use line is never printed (this assertion fails).
    """
    root = _make_agent(tmp_path)
    runner = FakeRunner(script={"print": (1, "")})
    # binder: free at pre-bootstrap probe, then bound (False) at the rollback
    # re-probe — simulate a port that got grabbed.
    states = iter([True, False])

    def binder(h, p):
        try:
            return next(states)
        except StopIteration:
            return False

    rc, _out, err = _run_full_deploy(
        root,
        tmp_path,
        monkeypatch,
        launchd_runner=runner,
        binder=binder,
        http_get=_healthz_bad_modtest,
    )
    assert rc != 0
    assert "address in use" in err


# Fix #9 — launchctl runner timeout maps to launchd error.
def test_default_runner_timeout_maps_to_failure(monkeypatch):
    """A subprocess TimeoutExpired becomes a non-zero CompletedProcess that
    teardown treats as a real failure (DeployLaunchdError).

    Negative control: drop the timeout handling and the runner raises
    TimeoutExpired instead of returning a non-zero result.
    """

    def fake_run(argv, *a, **k):
        raise subprocess.TimeoutExpired(argv, k.get("timeout", 30))

    monkeypatch.setattr(subprocess, "run", fake_run)
    cp = _launchd._default_runner(["launchctl", "bootout", "gui/0/x"])
    assert cp.returncode != 0
    assert "timed out" in cp.stderr
    # and a timeout on bootout surfaces as a launchd error (not "absent")
    assert _launchd._bootout_indicates_absent(cp) is False


# Fix #10a — MUST-11 false-green: tailscale present but only `status` issued.
def test_detect_tailscale_present_only_runs_status(monkeypatch):
    """With tailscale present, the ONLY call is `tailscale status --json`.

    Negative control: this is the stronger version of MUST 11 — it forces
    which() to present so the runner is actually reached (the prior test passed
    even when tailscale was absent because detect returned early).
    """
    monkeypatch.setattr(_exposure.shutil, "which", lambda b: "/usr/bin/tailscale")
    runner = FakeRunner(script={"status": (0, "{}")})
    assert _exposure.detect_tailscale(runner=runner) is True
    assert runner.calls == [["tailscale", "status", "--json"]]
    for call in runner.calls:
        assert "serve" not in call


# Fix #11 — reserved-name collision for status/down/deploy.
@pytest.mark.parametrize("reserved", ["status", "down", "deploy"])
def test_label_for_rejects_deploy_reserved_names(reserved):
    """An agent named status/down/deploy is rejected by the label slug rule.

    Negative control: remove status/down/deploy from RESERVED_AGENT_NAMES and
    label_for accepts them (this raise never happens).
    """
    with pytest.raises(ValueError):
        _launchd.label_for(reserved)


# Shared http_get fakes for the conductor-level tests above.
def _healthz_ok_doctor_ok_modtest(url):
    if url.endswith("/healthz"):
        return 200, '{"status": "ok"}'
    if url.endswith("/doctor"):
        return 200, '{"results": [{"name": "env", "status": "pass", "message": ""}]}'
    raise AssertionError(f"unexpected GET {url}")


def _healthz_bad_modtest(url):
    if url.endswith("/healthz"):
        return 503, '{"status": "degraded"}'
    raise AssertionError(f"unexpected GET {url} after healthz fail")

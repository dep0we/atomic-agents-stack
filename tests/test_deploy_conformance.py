"""Conformance tests for atomic_agents.deploy — the deployment conductor.

spec/49 §"Conformance test outline". One test (at least) per MUST row in the
table. Every system interaction is mocked: launchctl, tailscale, the socket
bind probe, and the HTTP probes are all routed through injectable seams so no
test installs a real launchd agent, runs real tailscale, binds a real
privileged port, or makes a real network/LLM call.

These tests run without the serve extra (no starlette / uvicorn import).
"""

from __future__ import annotations

import io
import plistlib
import subprocess
from pathlib import Path

import pytest

from atomic_agents import deploy as deploy_mod
from atomic_agents.deploy import _conductor, _exposure, _launchd, _ports, _verify
from atomic_agents.deploy._types import DeployState, StepTag


# ──────────────────────────────────────────────────────────────────────────
# Fakes / fixtures
# ──────────────────────────────────────────────────────────────────────────


class FakeRunner:
    """Records every argv handed to it; returns scripted CompletedProcess.

    ``script`` maps a matcher (the launchctl verb, e.g. "bootstrap") to a
    (returncode, stdout) tuple. Unmatched calls default to returncode 0.
    """

    def __init__(self, script: dict | None = None):
        self.calls: list[list[str]] = []
        self.script = script or {}

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        # Find the launchctl verb (argv[1]) or the binary name for matching.
        verb = argv[1] if len(argv) > 1 else argv[0]
        rc, out = self.script.get(verb, (0, ""))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    def issued(self, *needles: str) -> bool:
        """True if any recorded call contains all the given substrings."""
        for call in self.calls:
            joined = " ".join(call)
            if all(n in joined for n in needles):
                return True
        return False


def _free_binder(host, port):
    """Bind probe that always reports the port is free."""
    return True


def _busy_binder(host, port):
    """Bind probe that always reports the port is in use."""
    return False


def _healthz_ok_doctor_ok(url):
    """http_get fake: healthz returns status==ok; doctor returns no failures."""
    if url.endswith("/healthz"):
        return 200, '{"status": "ok", "agent": "x"}'
    if url.endswith("/doctor"):
        return (
            200,
            '{"results": [{"name": "env", "status": "pass", "message": ""}], "summary": {"all_ok": true}}',
        )
    raise AssertionError(f"unexpected GET {url}")


def _healthz_bad(url):
    """http_get fake: healthz reports degraded."""
    if url.endswith("/healthz"):
        return 503, '{"status": "degraded", "reason": "model.md is missing"}'
    raise AssertionError(f"unexpected GET {url} after healthz fail")


def _doctor_fail(url):
    """http_get fake: healthz ok but doctor has a failing check."""
    if url.endswith("/healthz"):
        return 200, '{"status": "ok"}'
    if url.endswith("/doctor"):
        return (
            200,
            '{"results": [{"name": "model", "status": "fail", "message": "bad"}], "summary": {"all_ok": false}}',
        )
    raise AssertionError(f"unexpected GET {url}")


@pytest.fixture
def agent_root(tmp_path):
    """A minimal agent folder that passes the existence + provider-key checks."""
    root = tmp_path / "agents"
    a = root / "myagent"
    a.mkdir(parents=True)
    # A model.md with an anthropic default so provider-key check looks for that.
    (a / "model.md").write_text("## Default model\nclaude-opus-4-7\n", encoding="utf-8")
    return root


@pytest.fixture
def launch_dir(tmp_path):
    """A tmp LaunchAgents dir so no plist lands in the real ~/Library."""
    d = tmp_path / "LaunchAgents"
    d.mkdir()
    return d


def _patch_doctor_pass(monkeypatch):
    """Make the doctor gate + provider-key check pass without real doctor."""
    import atomic_agents.doctor as doctor

    monkeypatch.setattr(doctor, "run_doctor", lambda **kw: [], raising=True)
    monkeypatch.setattr(doctor, "overall_exit_code", lambda results: 0)
    monkeypatch.setattr(doctor, "check_provider_keys", lambda data: [])


def _run_deploy(agent_root, launch_dir, **overrides):
    """Drive a full deploy with all seams mocked. Returns (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    kw = dict(
        agents_root=agent_root,
        assume_yes=True,
        out=out,
        err=err,
        launch_agents_dir=launch_dir,
        binder=_free_binder,
        http_get=_healthz_ok_doctor_ok,
        # Hermetic: an explicit empty environ keeps the env-only-key detection
        # (MUST 5) from probing the host's real env / Keychain during tests.
        environ={"HOME": "/h", "USER": "u", "PATH": "/usr/bin"},
        # Determinism: tests pin retries=1 so a forced-fail verify does not loop
        # through the production warm-up window (retries=10, delay=0.5s).
        verify_retries=1,
        verify_retry_delay_s=0.0,
        exposure_runner=lambda argv, *a, **k: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr=""
        ),
    )
    kw.update(overrides)
    rc = deploy_mod.deploy("myagent", **kw)
    return rc, out.getvalue(), err.getvalue()


# ──────────────────────────────────────────────────────────────────────────
# MUST 1 — Conductor, not a reimplementation
# ──────────────────────────────────────────────────────────────────────────


def test_must1_invokes_doctor_via_entry_point(agent_root, launch_dir, monkeypatch):
    """deploy drives doctor through run_doctor/check_provider_keys, no inline reimpl."""
    import atomic_agents.doctor as doctor

    seen = {"run_doctor": 0, "check_provider_keys": 0}

    def fake_run_doctor(**kw):
        seen["run_doctor"] += 1
        assert kw.get("skip_mcp") is True  # step 3 uses --no-mcp
        return []

    def fake_cpk(data):
        seen["check_provider_keys"] += 1
        return []

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)
    monkeypatch.setattr(doctor, "overall_exit_code", lambda r: 0)
    monkeypatch.setattr(doctor, "check_provider_keys", fake_cpk)

    runner = FakeRunner()
    rc, _, _ = _run_deploy(agent_root, launch_dir, launchd_runner=runner)
    assert rc == 0
    assert seen["run_doctor"] == 1
    assert seen["check_provider_keys"] == 1


def test_must1_no_migrate_invoked(agent_root, launch_dir, monkeypatch):
    """deploy must not run migrations."""
    _patch_doctor_pass(monkeypatch)
    import atomic_agents.deploy._conductor as cond

    # There is no migrate import in the conductor surface.
    src = Path(cond.__file__).read_text(encoding="utf-8")
    assert "migrate" not in src.lower()


# ──────────────────────────────────────────────────────────────────────────
# MUST 2 — No new config format; status/down read launchd
# ──────────────────────────────────────────────────────────────────────────


def test_must2_no_bespoke_state_file(agent_root, launch_dir, monkeypatch):
    """A successful deploy writes only the plist — no sidecar state file."""
    _patch_doctor_pass(monkeypatch)
    runner = FakeRunner()
    rc, _, _ = _run_deploy(agent_root, launch_dir, launchd_runner=runner)
    assert rc == 0
    written = list(launch_dir.iterdir())
    assert len(written) == 1
    assert written[0].suffix == ".plist"


def test_must2_status_reads_launchd_not_sidecar(launch_dir):
    """status derives state from launchctl print, not a cached file."""
    runner = FakeRunner(script={"print": (0, "\tpid = 4321\n")})
    out = io.StringIO()
    rc = deploy_mod.deploy_status(
        "myagent", out=out, launchd_runner=runner, launch_agents_dir=launch_dir
    )
    assert runner.issued("launchctl", "print")
    assert "running" in out.getvalue()
    assert rc == 0


# ──────────────────────────────────────────────────────────────────────────
# MUST 3 — No sudo in the default path
# ──────────────────────────────────────────────────────────────────────────


def test_must3_no_sudo_issued(agent_root, launch_dir, monkeypatch):
    """The default-path deploy issues zero sudo calls."""
    _patch_doctor_pass(monkeypatch)
    runner = FakeRunner()
    rc, _, _ = _run_deploy(agent_root, launch_dir, launchd_runner=runner)
    assert rc == 0
    for call in runner.calls:
        assert "sudo" not in call
    # The launchd domain is gui/$UID (user-level), never system.
    assert runner.issued("bootstrap", "gui/")


def test_must3_privileged_steps_tagged_consent_or_manual():
    """The plan's shared-state steps are consent/manual, never auto."""
    plan = deploy_mod.plan_deploy("myagent")
    by_key = {s.key: s for s in plan.steps}
    assert by_key["agent-exists"].tag == StepTag.CONSENT
    assert by_key["provider-key"].tag == StepTag.MANUAL
    assert by_key["supervise"].tag == StepTag.CONSENT
    assert by_key["exposure"].tag == StepTag.MANUAL


# ──────────────────────────────────────────────────────────────────────────
# MUST 4 — never runs serve in-process; absolute path + serve
# ──────────────────────────────────────────────────────────────────────────


def test_must4_program_arguments_absolute_and_serve():
    """plist ProgramArguments[0] is an absolute path; argv contains serve."""
    rendered = _launchd.render_plist("myagent", 8000, agents_root=Path("/tmp/agents"))
    pd = plistlib.loads(rendered.plist_bytes)
    prog = pd["ProgramArguments"]
    assert Path(prog[0]).is_absolute()
    assert "serve" in prog
    assert "myagent" in prog
    assert "--port" in prog and "8000" in prog


def test_must4_no_uvicorn_imported_in_conductor():
    """The conductor never imports uvicorn / starlette (no in-process serve)."""
    src = Path(_conductor.__file__).read_text(encoding="utf-8")
    assert "uvicorn" not in src
    assert "starlette" not in src
    assert "run_serve" not in src


# ──────────────────────────────────────────────────────────────────────────
# MUST 5 — environment injection; key sourced safely
# ──────────────────────────────────────────────────────────────────────────


def test_must5_plist_has_four_base_env_vars():
    """plist EnvironmentVariables carries HOME/USER/PATH/ATOMIC_AGENTS_ROOT."""
    rendered = _launchd.render_plist(
        "myagent",
        8000,
        agents_root=Path("/tmp/agents"),
        environ={"HOME": "/Users/x", "USER": "x", "PATH": "/usr/bin"},
    )
    env = rendered.environment_variables
    assert env["HOME"] == "/Users/x"
    assert env["USER"] == "x"
    assert env["PATH"] == "/usr/bin"
    assert env["ATOMIC_AGENTS_ROOT"] == "/tmp/agents"


def test_must5_no_plaintext_key_by_default():
    """No provider key lands in the plist when not explicitly injected."""
    rendered = _launchd.render_plist(
        "myagent",
        8000,
        agents_root=Path("/tmp/agents"),
        environ={
            "HOME": "/h",
            "USER": "u",
            "PATH": "/p",
            "ANTHROPIC_API_KEY": "sk-secret",
        },
    )
    pd = plistlib.loads(rendered.plist_bytes)
    serialized = plistlib.dumps(pd).decode("utf-8")
    assert "sk-secret" not in serialized
    assert rendered.wrote_plaintext_key is False


def test_must5_plaintext_key_only_when_explicitly_injected():
    """When the key's sole source is env, injecting it sets the caveat flag."""
    rendered = _launchd.render_plist(
        "myagent",
        8000,
        agents_root=Path("/tmp/agents"),
        environ={"HOME": "/h", "USER": "u", "PATH": "/p"},
        plaintext_keys={"ANTHROPIC_API_KEY": "sk-only-source"},
    )
    assert rendered.wrote_plaintext_key is True
    assert rendered.environment_variables["ANTHROPIC_API_KEY"] == "sk-only-source"


def test_must5_multiple_env_only_keys_all_injected():
    """An agent with default + fallback env-only keys gets BOTH injected (Fix #4).

    Negative control: revert _resolve_env_only_provider_keys to return one pair
    (or render_plist to inject only the first) and the second key is missing.
    """
    rendered = _launchd.render_plist(
        "myagent",
        8000,
        agents_root=Path("/tmp/agents"),
        environ={"HOME": "/h", "USER": "u", "PATH": "/p"},
        plaintext_keys={
            "ANTHROPIC_API_KEY": "sk-anthropic",
            "OPENAI_API_KEY": "sk-openai",
        },
    )
    assert rendered.wrote_plaintext_key is True
    assert rendered.environment_variables["ANTHROPIC_API_KEY"] == "sk-anthropic"
    assert rendered.environment_variables["OPENAI_API_KEY"] == "sk-openai"


# ──────────────────────────────────────────────────────────────────────────
# MUST 6 — --plan is side-effect-free and unbilled
# ──────────────────────────────────────────────────────────────────────────


def test_must6_plan_writes_nothing_installs_nothing(
    agent_root, launch_dir, monkeypatch
):
    """--plan prints the plan, exits 0, and touches no system surface."""
    runner = FakeRunner()

    # Guard: if any doctor call happens during --plan, fail.
    import atomic_agents.doctor as doctor

    def boom(**kw):
        raise AssertionError("doctor must not run during --plan")

    monkeypatch.setattr(doctor, "run_doctor", boom)

    out = io.StringIO()
    rc = deploy_mod.deploy(
        "myagent",
        agents_root=agent_root,
        plan_only=True,
        out=out,
        launch_agents_dir=launch_dir,
        launchd_runner=runner,
        binder=lambda h, p: (_ for _ in ()).throw(
            AssertionError("bind probe must not run during --plan")
        ),
    )
    assert rc == 0
    assert "Deployment plan" in out.getvalue()
    assert runner.calls == []  # no launchctl
    assert list(launch_dir.iterdir()) == []  # no plist written


# ──────────────────────────────────────────────────────────────────────────
# MUST 7 — idempotent re-deploy
# ──────────────────────────────────────────────────────────────────────────


def test_must7_redeploy_bootout_then_bootstrap(launch_dir):
    """When the label is already bootstrapped, install boots out first."""
    # print returns 0 → already bootstrapped; bootout + bootstrap then run.
    runner = FakeRunner(script={"print": (0, "\tpid = 1\n")})
    rendered = _launchd.render_plist("myagent", 8000, agents_root=Path("/a"))
    _launchd.install_launchd_agent(
        "myagent",
        rendered.plist_bytes,
        launch_agents_dir=launch_dir,
        runner=runner,
    )
    verbs = [c[1] for c in runner.calls if len(c) > 1]
    assert "bootout" in verbs
    assert "bootstrap" in verbs
    assert verbs.index("bootout") < verbs.index("bootstrap")


def test_must7_redeploy_of_loaded_label_does_not_raise_port_conflict(
    agent_root, launch_dir, monkeypatch
):
    """Re-deploying an already-loaded label restarts cleanly even with a busy port.

    Our own loaded label holding the port is NOT a conflict — install's
    bootout→bootstrap frees and rebinds it. The pre-bootstrap probe MUST be
    skipped for this case (MUST 7), so a busy binder does NOT abort the deploy.

    Negative control: remove the ``own_label_loaded`` guard in _step_supervise
    and the busy binder makes the pre-probe raise PortConflictError → rc != 0 and
    bootstrap never issues, failing both assertions below.
    """
    _patch_doctor_pass(monkeypatch)
    # print → 0: our label IS already bootstrapped (a re-deploy).
    runner = FakeRunner(script={"print": (0, "\tpid = 1\n")})
    rc, _, _ = _run_deploy(
        agent_root,
        launch_dir,
        launchd_runner=runner,
        binder=_busy_binder,  # port held — but by OUR own loaded serve
        http_get=_healthz_ok_doctor_ok,
    )
    assert rc == 0
    # Clean restart happened: bootout BEFORE bootstrap (install's idempotent path).
    verbs = [c[1] for c in runner.calls if len(c) > 1]
    assert "bootout" in verbs and "bootstrap" in verbs
    assert verbs.index("bootout") < verbs.index("bootstrap")


# ──────────────────────────────────────────────────────────────────────────
# MUST 8 — rollback on post-install verify failure
# ──────────────────────────────────────────────────────────────────────────


def test_must8_verify_fail_rolls_back(agent_root, launch_dir, monkeypatch):
    """A failing verify boots out the agent + removes the plist, exits non-zero."""
    _patch_doctor_pass(monkeypatch)
    runner = FakeRunner()
    rc, _, err = _run_deploy(
        agent_root, launch_dir, launchd_runner=runner, http_get=_healthz_bad
    )
    assert rc != 0
    assert "rolled back" in err
    # plist removed by rollback
    assert list(launch_dir.iterdir()) == []
    # bootout was issued during rollback
    assert runner.issued("bootout")


# ──────────────────────────────────────────────────────────────────────────
# MUST 9 — default verification non-mutating, unbilled, predicate-based
# ──────────────────────────────────────────────────────────────────────────


def test_must9_healthz_not_ok_fails():
    """healthz status != ok → verify fails (a 200 alone is not enough)."""

    def http_get(url):
        # 200 status but body status != ok
        return 200, '{"status": "degraded"}'

    result = _verify.verify_deployment("a", "127.0.0.1", 8000, http_get=http_get)
    assert result.ok is False
    assert result.checks[0][0] == "healthz"
    assert result.checks[0][1] is False


def test_must9_doctor_fail_fails():
    """healthz ok but doctor has a failing check → verify fails."""
    result = _verify.verify_deployment("a", "127.0.0.1", 8000, http_get=_doctor_fail)
    assert result.ok is False
    names = [c[0] for c in result.checks]
    assert "doctor" in names


def test_must9_both_pass_succeeds():
    """healthz ok AND doctor exit 0 → verify passes."""
    result = _verify.verify_deployment(
        "a", "127.0.0.1", 8000, http_get=_healthz_ok_doctor_ok
    )
    assert result.ok is True


def test_must9_verify_call_hits_call_only_when_opted_in():
    """--verify-call fires POST /call; default never does."""
    posted = []

    def http_post(url, body):
        posted.append((url, body))
        return 200, '{"status": "ok"}'

    # default: no /call
    _verify.verify_deployment(
        "a", "127.0.0.1", 8000, http_get=_healthz_ok_doctor_ok, http_post=http_post
    )
    assert posted == []

    # opt-in: /call is fired
    result = _verify.verify_deployment(
        "a",
        "127.0.0.1",
        8000,
        verify_call=True,
        http_get=_healthz_ok_doctor_ok,
        http_post=http_post,
    )
    assert len(posted) == 1
    assert posted[0][0].endswith("/call")
    assert result.called is True


# ──────────────────────────────────────────────────────────────────────────
# MUST 10 — port resolution deterministic; conflict fails loud
# ──────────────────────────────────────────────────────────────────────────


def test_must10_precedence_cli_over_env(tmp_path):
    """--port wins over env / serve.md / default."""
    root = tmp_path / "a"
    root.mkdir()
    port = _ports.resolve_port(
        root, cli_port=9999, environ={"ATOMIC_AGENTS_SERVE_PORT": "7000"}
    )
    assert port == 9999


def test_must10_precedence_env_over_servemd(tmp_path):
    """env wins over serve.md when no --port."""
    root = tmp_path / "a"
    root.mkdir()
    (root / "serve.md").write_text("## Bind Port\n7777\n", encoding="utf-8")
    port = _ports.resolve_port(
        root, cli_port=None, environ={"ATOMIC_AGENTS_SERVE_PORT": "7000"}
    )
    assert port == 7000


def test_must10_precedence_servemd_over_default(tmp_path):
    """serve.md wins over the default when no --port, no env."""
    root = tmp_path / "a"
    root.mkdir()
    (root / "serve.md").write_text("## Bind Port\n7777\n", encoding="utf-8")
    port = _ports.resolve_port(root, cli_port=None, environ={})
    assert port == 7777


def test_must10_default_when_nothing_set(tmp_path):
    root = tmp_path / "a"
    root.mkdir()
    port = _ports.resolve_port(root, cli_port=None, environ={})
    assert port == _ports.DEFAULT_PORT


def test_must10_bind_conflict_fails_loud_no_rebind():
    """A busy port raises PortConflictError naming the port; no rebind."""
    with pytest.raises(_ports.PortConflictError) as exc:
        _ports.probe_port_free("127.0.0.1", 8000, binder=_busy_binder)
    assert "8000" in str(exc.value)
    assert "NOT silently" in str(exc.value)


def test_must10_conflict_in_full_deploy_aborts_before_install(
    agent_root, launch_dir, monkeypatch
):
    """A FOREIGN bind conflict during deploy aborts before any plist is written.

    Foreign = the port is busy AND our own launchd label is NOT loaded
    (``print`` returns non-zero). The pre-bootstrap probe runs and fails loud.
    """
    _patch_doctor_pass(monkeypatch)
    # print → non-zero: our label is NOT bootstrapped, so a busy port is a
    # foreign holder → the pre-bootstrap probe MUST run and fail loud.
    runner = FakeRunner(script={"print": (1, "")})
    rc, _, err = _run_deploy(
        agent_root, launch_dir, launchd_runner=runner, binder=_busy_binder
    )
    assert rc != 0
    assert "already in use" in err
    assert list(launch_dir.iterdir()) == []  # no plist
    assert not runner.issued("bootstrap")  # never installed


def test_must7_bootstrapped_but_not_running_busy_port_fails_loud(
    agent_root, launch_dir, monkeypatch
):
    """A bootstrapped-but-NOT-running own label (loaded/crashed, no live PID)
    does NOT hold the port, so a busy port is a FOREIGN conflict that must still
    fail loud. The probe-skip keys on RUNNING, not merely bootstrapped (round-3
    cross-family P1). Negative control: under the old ``_is_bootstrapped`` skip,
    this busy port was silently swallowed.
    """
    _patch_doctor_pass(monkeypatch)
    # print → rc 0 but NO pid line → bootstrapped + LOADED (not RUNNING).
    runner = FakeRunner(script={"print": (0, "")})
    rc, _, err = _run_deploy(
        agent_root, launch_dir, launchd_runner=runner, binder=_busy_binder
    )
    assert rc != 0
    assert "already in use" in err
    assert list(launch_dir.iterdir()) == []  # no plist written
    assert not runner.issued("bootstrap")  # never installed


# ──────────────────────────────────────────────────────────────────────────
# MUST 11 — exposure is guided, never performed
# ──────────────────────────────────────────────────────────────────────────


def test_must11_never_runs_tailscale_serve(agent_root, launch_dir, monkeypatch):
    """No `tailscale serve` / perimeter command is ever issued during deploy."""
    _patch_doctor_pass(monkeypatch)
    exposure_runner = FakeRunner(script={"tailscale": (0, "{}"), "status": (0, "{}")})
    runner = FakeRunner()
    rc, _, _ = _run_deploy(
        agent_root,
        launch_dir,
        launchd_runner=runner,
        exposure_runner=exposure_runner,
    )
    assert rc == 0
    # The ONLY tailscale call permitted is the read-only `tailscale status --json`.
    for call in exposure_runner.calls:
        if "tailscale" in call:
            assert call[1] == "status", f"forbidden tailscale call: {call}"
            assert "serve" not in call


def test_must11_tailscale_present_prints_exact_command():
    """Tailscale detected → exact `tailscale serve --bg http://127.0.0.1:<port>`."""
    text = _exposure.exposure_guidance(8123, tailscale_present=True)
    assert "tailscale serve --bg http://127.0.0.1:8123" in text
    assert "HTTPS certificates" in text  # cert prerequisite
    assert "first HTTPS request may be slow" in text  # warm-up note


def test_must11_tailscale_absent_prints_perimeter_pointer():
    """Tailscale absent → pointer to perimeter options + loopback-only statement."""
    text = _exposure.exposure_guidance(8123, tailscale_present=False)
    assert "loopback-only" in text
    assert "docs/deployment/serve.md" in text


def test_must11_detect_tailscale_is_read_only(monkeypatch):
    """detect_tailscale only ever runs `tailscale status --json`."""
    monkeypatch.setattr(_exposure.shutil, "which", lambda b: "/usr/bin/tailscale")
    runner = FakeRunner(script={"status": (0, "{}")})
    assert _exposure.detect_tailscale(runner=runner) is True
    assert runner.calls == [["tailscale", "status", "--json"]]


# ──────────────────────────────────────────────────────────────────────────
# MUST 12 — down is complete; status is honest and specific
# ──────────────────────────────────────────────────────────────────────────


def test_must12_down_boots_out_and_removes_plist(launch_dir):
    """down boots out the label and removes the plist (full teardown)."""
    # Pre-create a plist so we can assert removal.
    label = _launchd.label_for("myagent")
    plist = launch_dir / f"{label}.plist"
    plist.write_bytes(b"<plist></plist>")
    runner = FakeRunner()
    out = io.StringIO()
    rc = deploy_mod.deploy_down(
        "myagent", out=out, launchd_runner=runner, launch_agents_dir=launch_dir
    )
    assert rc == 0
    assert runner.issued("bootout", "gui/")
    assert not plist.exists()


@pytest.mark.parametrize(
    "print_rc,print_out,plist_exists,expected",
    [
        (1, "", False, DeployState.ABSENT),  # no plist + not bootstrapped
        (1, "", True, DeployState.LOADED),  # plist present, not loaded
        (0, "\tpid = 1234\n", True, DeployState.RUNNING),  # live PID
        (0, "\tlast exit code = 1\n", True, DeployState.CRASHED),  # no PID, bad exit
    ],
)
def test_must12_status_state_mapping(
    launch_dir, print_rc, print_out, plist_exists, expected
):
    """status returns absent/loaded/running/crashed from mocked launchctl."""
    label = _launchd.label_for("myagent")
    if plist_exists:
        (launch_dir / f"{label}.plist").write_bytes(b"<plist></plist>")
    runner = FakeRunner(script={"print": (print_rc, print_out)})
    status = _launchd.read_launchd_status(
        "myagent", launch_agents_dir=launch_dir, runner=runner
    )
    assert status.state == expected


def test_must12_status_exit_codes(launch_dir):
    """status returns 0 for running/loaded, 1 for absent/crashed."""
    runner_running = FakeRunner(script={"print": (0, "\tpid = 9\n")})
    rc = deploy_mod.deploy_status(
        "myagent",
        out=io.StringIO(),
        launchd_runner=runner_running,
        launch_agents_dir=launch_dir,
    )
    assert rc == 0

    runner_absent = FakeRunner(script={"print": (1, "")})
    rc = deploy_mod.deploy_status(
        "myagent",
        out=io.StringIO(),
        launchd_runner=runner_absent,
        launch_agents_dir=launch_dir,
    )
    assert rc == 1

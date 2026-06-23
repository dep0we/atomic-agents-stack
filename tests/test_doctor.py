"""Tests for atomic_agents.doctor.

Coverage strategy: each individual check has at least one PASS path and one
FAIL path. CLI integration is covered through `cli.main(["doctor", ...])` to
verify exit codes and JSON output shape.

Filesystem isolation: every test that touches the agent vault uses tmp_path
and an explicit agents_root; nothing reads or writes outside the temp dir.
The provider-keys check is exercised against a known-empty environment by
clearing env vars + pointing config-file lookup at a temp HOME.
"""

from __future__ import annotations

import errno
import fcntl
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

from atomic_agents import doctor
from atomic_agents import cli as cli_module
from atomic_agents.doctor import (
    PASS,
    FAIL,
    SKIP,
    CheckResult,
    check_env,
    check_python,
    check_vault,
    check_provider_keys,
    check_model,
    check_locks,
    check_memory_backend,
    check_write_paths,
    run_doctor,
    render_human,
    render_json,
    overall_exit_code,
)


# ──────────────────────────────────────────────────────────────────
# Helpers


def _make_agent(
    root: Path,
    name: str = "test-agent",
    *,
    model_md: str | None = None,
    tools_md: str | None = None,
    with_index: bool = True,
    with_persona: bool = True,
) -> Path:
    """Build a minimal-valid agent vault under <root>/<name>/."""
    agent = root / name
    (agent / "persona").mkdir(parents=True, exist_ok=True)
    (agent / "memory").mkdir(parents=True, exist_ok=True)

    if with_persona:
        (agent / "persona" / "IDENTITY.md").write_text(
            "# IDENTITY\n\nI am a test agent.\n", encoding="utf-8"
        )
    (agent / "tools.md").write_text(
        tools_md
        if tools_md is not None
        else "## Read paths\n\n- "
        + str(agent)
        + "\n\n## Write paths\n\n- "
        + str(agent / "memory")
        + "\n",
        encoding="utf-8",
    )
    (agent / "model.md").write_text(
        model_md
        if model_md is not None
        else ("# model.md\n\n## Default model\n\nclaude-haiku-4-5-20251001\n"),
        encoding="utf-8",
    )
    if with_index:
        (agent / "memory" / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
    return agent


def _isolate_keys(monkeypatch, tmp_home: Path) -> None:
    """Make every provider-key resolution lookup miss.

    Strategy: clear all env vars, point HOME at an empty temp dir (so the
    config-file path resolves to a non-existent location), and stub out the
    `security` subprocess so the Keychain branch always fails. This mirrors
    a fresh install with no key configured.
    """
    for var in (
        "ATOMIC_AGENTS_ANTHROPIC_KEY",
        "ANTHROPIC_API_KEY",
        "ATOMIC_AGENTS_OPENAI_KEY",
        "OPENAI_API_KEY",
        "ATOMIC_AGENTS_MOONSHOT_KEY",
        "MOONSHOT_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_home))

    # Stub `security` so the Keychain branch never returns a key.
    import subprocess

    real_run = subprocess.run

    def fake_run(args, *a, **kw):  # noqa: ANN001
        if args and args[0] == "security":
            raise subprocess.CalledProcessError(returncode=44, cmd=args)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)


# ──────────────────────────────────────────────────────────────────
# check_env


def test_env_pass_with_explicit_override(tmp_path):
    r = check_env(tmp_path)
    assert r.status == PASS
    assert str(tmp_path) in r.message


def test_env_pass_with_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    r = check_env(None)
    assert r.status == PASS
    assert "ATOMIC_AGENTS_ROOT" in r.detail["source"]


def test_env_fail_when_path_missing(tmp_path):
    missing = tmp_path / "does-not-exist"
    r = check_env(missing)
    assert r.status == FAIL
    assert "does not exist" in r.message
    assert "mkdir" in r.fix_hint


def test_env_fail_when_path_is_file(tmp_path):
    p = tmp_path / "iam_a_file"
    p.write_text("not a dir", encoding="utf-8")
    r = check_env(p)
    assert r.status == FAIL
    assert "not a directory" in r.message


# ──────────────────────────────────────────────────────────────────
# check_python


def test_python_pass(monkeypatch):
    # Real interpreter must already meet the floor (pyproject requires >=3.11).
    r = check_python()
    assert r.status == PASS
    assert "Python" in r.message


def test_python_fail_with_old_version(monkeypatch):
    monkeypatch.setattr(doctor, "MIN_PYTHON", (99, 0))
    r = check_python()
    assert r.status == FAIL
    assert "too old" in r.message


# ──────────────────────────────────────────────────────────────────
# check_vault


def test_vault_pass_with_minimal_agent(tmp_path):
    agent = _make_agent(tmp_path)
    r = check_vault(agent)
    assert r.status == PASS


def test_vault_fail_when_agent_missing(tmp_path):
    r = check_vault(tmp_path / "nope")
    assert r.status == FAIL
    assert "does not exist" in r.message


def test_vault_fail_when_persona_missing(tmp_path):
    agent = _make_agent(tmp_path, with_persona=False)
    r = check_vault(agent)
    assert r.status == FAIL
    assert "persona/IDENTITY.md" in r.message


def test_vault_fail_when_index_missing(tmp_path):
    agent = _make_agent(tmp_path, with_index=False)
    r = check_vault(agent)
    assert r.status == FAIL
    assert "memory/INDEX.md" in r.message


# ──────────────────────────────────────────────────────────────────
# check_provider_keys


def test_provider_keys_pass_via_env(monkeypatch, tmp_path):
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    model_data = {"default_model": "claude-haiku-4-5", "fallback_model": None}
    results = check_provider_keys(model_data)
    assert len(results) == 1
    assert results[0].status == PASS
    assert "anthropic" in results[0].name


def test_provider_keys_fail_when_no_key(monkeypatch, tmp_path):
    _isolate_keys(monkeypatch, tmp_path)
    model_data = {"default_model": "claude-haiku-4-5", "fallback_model": None}
    results = check_provider_keys(model_data)
    assert results[0].status == FAIL
    assert "ATOMIC_AGENTS_ANTHROPIC_KEY" in results[0].fix_hint


def test_provider_keys_unique_per_provider(monkeypatch, tmp_path):
    """default + fallback that share a provider yield ONE result."""
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    model_data = {
        "default_model": "claude-opus-4-7",
        "fallback_model": "claude-haiku-4-5",
    }
    results = check_provider_keys(model_data)
    assert len(results) == 1
    assert results[0].status == PASS


def test_provider_keys_two_providers(monkeypatch, tmp_path):
    """Different providers across default + fallback yield two results.

    Stubs `__import__("openai")` so the test does not depend on whether the
    optional `openai` extra is installed in the test env (it is not, by
    default; doctor's SDK check would otherwise fail the openai branch here).
    """
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anth")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")

    real_import = (
        __builtins__["__import__"]
        if isinstance(__builtins__, dict)
        else __builtins__.__import__
    )

    def fake_import(name, *a, **kw):
        if name == "openai":
            return type(sys)("openai")  # minimal stand-in module
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)

    model_data = {
        "default_model": "claude-opus-4-7",
        "fallback_model": "gpt-5",
    }
    results = check_provider_keys(model_data)
    assert len(results) == 2
    assert all(r.status == PASS for r in results), [
        (r.name, r.message) for r in results
    ]
    names = {r.name for r in results}
    assert "provider-keys[anthropic]" in names
    assert "provider-keys[openai]" in names


def test_provider_keys_skip_unrecognised_model(monkeypatch, tmp_path):
    _isolate_keys(monkeypatch, tmp_path)
    model_data = {"default_model": "weird-custom-model"}
    results = check_provider_keys(model_data)
    assert len(results) == 1
    assert results[0].status == SKIP


# ──────────────────────────────────────────────────────────────────
# check_model


def test_model_pass():
    r = check_model({"default_model": "claude-haiku-4-5"})
    assert r.status == PASS


def test_model_fail_when_unknown():
    r = check_model({"default_model": "claude-imaginary-9-9"})
    assert r.status == FAIL
    assert "not in the pricing table" in r.message


def test_model_fail_when_missing():
    r = check_model({})
    assert r.status == FAIL
    assert "no default_model" in r.message


def test_model_fail_when_guardrails_zero_caps():
    r = check_model(
        {
            "default_model": "claude-haiku-4-5",
            "cost_guardrails_enabled": True,
            "daily_cap_usd": 0.0,
            "monthly_cap_usd": 5.0,
        }
    )
    assert r.status == FAIL
    assert "daily_cap_usd" in r.message or "monthly_cap_usd" in r.message


def test_model_pass_with_guardrails_set():
    r = check_model(
        {
            "default_model": "claude-haiku-4-5",
            "cost_guardrails_enabled": True,
            "daily_cap_usd": 1.0,
            "monthly_cap_usd": 10.0,
        }
    )
    assert r.status == PASS


# ──────────────────────────────────────────────────────────────────
# check_locks


def test_locks_pass_when_no_lock_file(tmp_path):
    r = check_locks(tmp_path)
    assert r.status == PASS


def test_locks_pass_when_lock_file_unheld(tmp_path):
    """A lingering .lock file with no flock holder is the normal post-run state."""
    (tmp_path / ".lock").write_text("pid=1234 acquired=0\n", encoding="utf-8")
    r = check_locks(tmp_path)
    assert r.status == PASS


def _hold_lock_for(agent_root_str: str, hold_seconds: float):
    """Child process target: acquires flock and holds it."""
    fd = os.open(Path(agent_root_str) / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, f"pid={os.getpid()} held\n".encode())
    time.sleep(hold_seconds)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_locks_fail_when_held_by_another_process(tmp_path):
    proc = multiprocessing.Process(target=_hold_lock_for, args=(str(tmp_path), 1.5))
    proc.start()
    try:
        time.sleep(0.3)
        r = check_locks(tmp_path)
        assert r.status == FAIL
        assert "held" in r.message
    finally:
        proc.join()


def test_locks_flags_stale_when_held_and_old(tmp_path):
    """Hold the lock, backdate mtime, expect 'stale' marker in the message."""
    proc = multiprocessing.Process(target=_hold_lock_for, args=(str(tmp_path), 1.5))
    proc.start()
    try:
        time.sleep(0.3)
        # Backdate the lock file to look very old
        old = time.time() - 3600
        os.utime(tmp_path / ".lock", (old, old))
        r = check_locks(tmp_path, stale_seconds=60.0)
        assert r.status == FAIL
        assert "stale" in r.message
    finally:
        proc.join()


# ──────────────────────────────────────────────────────────────────
# check_memory_backend


def test_memory_backend_pass(tmp_path):
    agent = _make_agent(tmp_path)
    r = check_memory_backend(agent)
    assert r.status == PASS
    assert "FilesystemBackend ok" in r.message


def test_memory_backend_fail_when_missing(tmp_path):
    r = check_memory_backend(tmp_path / "no-such-agent")
    assert r.status == FAIL
    assert "memory/" in r.message


# ──────────────────────────────────────────────────────────────────
# check_write_paths


def test_write_paths_pass(tmp_path):
    p = tmp_path / "writable"
    p.mkdir()
    r = check_write_paths({"write_paths": [p]})
    assert r.status == PASS


def test_write_paths_skip_when_empty():
    r = check_write_paths({"write_paths": []})
    assert r.status == SKIP


def test_write_paths_fail_when_missing(tmp_path):
    r = check_write_paths({"write_paths": [tmp_path / "ghost"]})
    assert r.status == FAIL
    assert "does not exist" in r.message


def test_write_paths_fail_when_unwritable(tmp_path):
    p = tmp_path / "readonly"
    p.mkdir()
    p.chmod(0o500)  # read+exec only
    try:
        # Skip on platforms / filesystems where chmod doesn't enforce W_OK
        # for the test runner (e.g., when running as root).
        if os.access(p, os.W_OK):
            pytest.skip("chmod 0500 did not remove W_OK; running as root?")
        r = check_write_paths({"write_paths": [p]})
        assert r.status == FAIL
        assert "not writable" in r.message
    finally:
        p.chmod(0o700)


# ──────────────────────────────────────────────────────────────────
# run_doctor — host-only mode


def test_run_doctor_no_agent_skips_agent_checks(tmp_path, monkeypatch):
    # embedding-backend SKIPs only when the opt-in env var is unset; clear it so
    # this enumeration is deterministic regardless of the ambient environment.
    monkeypatch.delenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", raising=False)
    results = run_doctor(agent_name=None, agents_root=tmp_path)
    names = {r.name for r in results}
    assert "env" in names and "python" in names
    # All agent-scoped checks should be SKIP. Enumerate EVERY agent-scoped
    # check name — including each backend check — so that a future drift where a
    # backend check is added to run_doctor's execution sequence but forgotten in
    # the no-agent SKIP enumeration (doctor.py) fails this test loudly. This is
    # the exact class of bug that previously let lock-backend / log-backend /
    # persona-backend silently fall out of the SKIP list.
    skipped = {r.name for r in results if r.status == SKIP}
    assert {
        "vault",
        "provider-keys",
        "model",
        "mcp",
        "lock-backend",
        "locks",
        "log-backend",
        "profile-backend",
        "tool-registry-backend",
        "mandate-backend",
        "policy-backend",
        "persona-backend",
        "corpus-backend",
        "mcp-server-registry-backend",
        "secret-backend",
        "goal-backend",
        "outcome-backend",
        "journal-backend",
        "queue-backend",
        "idempotency-backend",
        "principal-backend",
        "embedding-backend",
        "conversation-backend",
        "memory-backend-config",
        "memory-backend",
        "write-paths",
    } <= skipped


def test_run_doctor_full_pass_against_minimal_agent(monkeypatch, tmp_path):
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    _make_agent(tmp_path, "happy")
    # Use --no-mcp to skip handshake; mcp.md is absent anyway.
    results = run_doctor(agent_name="happy", agents_root=tmp_path, skip_mcp=True)
    failed = [r for r in results if r.status == FAIL]
    assert not failed, f"unexpected failures: {[(r.name, r.message) for r in failed]}"


def test_run_doctor_overall_exit_code_failure(tmp_path):
    """No agent dir under agents_root → vault check fails → exit 1."""
    results = run_doctor(agent_name="ghost", agents_root=tmp_path, skip_mcp=True)
    assert overall_exit_code(results) == 1


def test_run_doctor_skip_mcp_emits_skip(monkeypatch, tmp_path):
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _make_agent(tmp_path, "agent-x")
    results = run_doctor(agent_name="agent-x", agents_root=tmp_path, skip_mcp=True)
    mcp_results = [r for r in results if r.name == "mcp"]
    assert len(mcp_results) == 1
    assert mcp_results[0].status == SKIP


def test_run_doctor_includes_principal_backend_check(monkeypatch, tmp_path):
    """spec/48 (G): check_principal_backend() is WIRED into run_doctor() — not dead
    code. A 'principal-backend' result must appear in a real doctor run.

    Round 1 finding: the check was defined but never appended to run_doctor(), so
    `atomic-agents doctor` never ran the principal coherence/negative probe.
    """
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", raising=False)
    _make_agent(tmp_path, "agent-p")
    results = run_doctor(agent_name="agent-p", agents_root=tmp_path, skip_mcp=True)
    principal_results = [r for r in results if r.name == "principal-backend"]
    assert len(principal_results) == 1, (
        "principal-backend check must appear in a real doctor run "
        "(check_principal_backend wired into run_doctor)"
    )
    # Default (no env var) → LocalPrincipalBackend → PASS.
    assert principal_results[0].status == PASS


def test_run_doctor_principal_backend_fails_on_unknown_env(monkeypatch, tmp_path):
    """An unknown ATOMIC_AGENTS_PRINCIPAL_BACKEND yields a FAIL principal-backend
    result (negative control for the wired-in check).
    """
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", "no-such-backend")
    _make_agent(tmp_path, "agent-q")
    results = run_doctor(agent_name="agent-q", agents_root=tmp_path, skip_mcp=True)
    principal_results = [r for r in results if r.name == "principal-backend"]
    assert len(principal_results) == 1
    assert principal_results[0].status == FAIL


def test_principal_backend_redacts_credential_url(monkeypatch):
    """When ATOMIC_AGENTS_PRINCIPAL_BACKEND is accidentally set to a DSN-shaped
    value, check_principal_backend() FAIL message and detail MUST NOT reproduce
    the raw credential. Mirrors test_conversation_backend_redacts_credential_url —
    the principal check uses the same _redact_for_error_message convention as
    every other doctor check (spec/48 LOCK ceremony, feedback_doctor_check_redacts_env_value_not_exception_string).
    """
    from atomic_agents.doctor import check_principal_backend, FAIL  # noqa: PLC0415

    monkeypatch.setenv(
        "ATOMIC_AGENTS_PRINCIPAL_BACKEND", "postgres://user:hunter2@host/db"
    )
    result = check_principal_backend()

    assert result.status == FAIL
    assert "hunter2" not in result.message
    assert "hunter2" not in str(result.detail)
    # Two-sided guard: the redacted-but-informative form is still shown (scheme
    # prefix kept, credentials stripped) — not an empty/dropped field that would
    # also pass the secret-absence checks above.
    assert "postgres://..." in result.message
    assert "postgres://..." in str(result.detail)


# ──────────────────────────────────────────────────────────────────
# Output rendering


def test_render_human_includes_summary():
    results = [
        CheckResult("env", PASS, "ok"),
        CheckResult("vault", FAIL, "missing", fix_hint="run init"),
    ]
    out = render_human(results)
    assert "[ OK ]" in out and "[FAIL]" in out
    assert "FAIL — 1 failed" in out
    assert "run init" in out  # fix-hint surfaced for the failing check


def test_render_json_shape():
    results = [
        CheckResult("env", PASS, "ok"),
        CheckResult("vault", FAIL, "missing"),
    ]
    payload = json.loads(render_json(results))
    assert payload["summary"] == {
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "all_ok": False,
    }
    assert payload["results"][0]["name"] == "env"
    assert payload["results"][1]["status"] == FAIL


def test_overall_exit_code_passes():
    results = [CheckResult("env", PASS, "ok"), CheckResult("vault", SKIP, "n/a")]
    assert overall_exit_code(results) == 0


def test_overall_exit_code_fails():
    results = [CheckResult("env", FAIL, "broken")]
    assert overall_exit_code(results) == 1


# ──────────────────────────────────────────────────────────────────
# CLI integration


def test_cli_doctor_no_agent_returns_zero(tmp_path, capsys):
    rc = cli_module.main(["doctor", "--agents-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK —" in out


def test_cli_doctor_json_output(tmp_path, capsys):
    rc = cli_module.main(["doctor", "--agents-root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "results" in payload and "summary" in payload


def test_cli_doctor_failure_returns_one(tmp_path, capsys):
    rc = cli_module.main(
        [
            "doctor",
            "--agent",
            "ghost",
            "--agents-root",
            str(tmp_path),
            "--no-mcp",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL —" in out


def test_cli_doctor_crash_returns_two(monkeypatch, tmp_path, capsys):
    """If doctor itself raises, the CLI catches it and returns exit code 2."""

    def boom(*a, **kw):  # noqa: ANN001
        raise RuntimeError("doctor module bug")

    monkeypatch.setattr(doctor, "run_doctor", boom)
    rc = cli_module.main(["doctor", "--agents-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "doctor crashed" in err


# ──────────────────────────────────────────────────────────────────
# Codex-review follow-ups: cascade, parse-error containment,
# optional-SDK detection, MCP read-path enforcement, memory-in-write-paths


def _make_cascade(root: Path, *, project: str = "demo", role: str = "writer") -> Path:
    """Build a minimal cascade layout (spec/06): roles/<role>/ + projects/<p>/agents/<role>/."""
    role_dir = root / "roles" / role
    instance_dir = root / "projects" / project / "agents" / role
    (role_dir).mkdir(parents=True, exist_ok=True)
    (instance_dir / "persona").mkdir(parents=True, exist_ok=True)
    (instance_dir / "memory").mkdir(parents=True, exist_ok=True)

    # Role-level tools.md + model.md (instance has neither — cascade fallback).
    (role_dir / "tools.md").write_text(
        "## Read paths\n\n- " + str(instance_dir) + "\n\n"
        "## Write paths\n\n- " + str(instance_dir / "memory") + "\n",
        encoding="utf-8",
    )
    (role_dir / "model.md").write_text(
        "## Default model\n\nclaude-haiku-4-5-20251001\n", encoding="utf-8"
    )

    # Instance has only persona + memory.
    (instance_dir / "persona" / "IDENTITY.md").write_text("# id\n", encoding="utf-8")
    (instance_dir / "memory" / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
    return instance_dir


def test_check_vault_cascade_passes_with_role_level_files(tmp_path):
    from atomic_agents import _cascade

    instance = _make_cascade(tmp_path)
    cascade = _cascade.detect_cascade(instance)
    assert cascade is not None, "cascade detection precondition"
    r = check_vault(instance, cascade=cascade)
    assert r.status == PASS
    assert r.detail["cascaded"] is True


def test_check_vault_cascade_still_fails_when_instance_missing_persona(tmp_path):
    from atomic_agents import _cascade

    instance = _make_cascade(tmp_path)
    (instance / "persona" / "IDENTITY.md").unlink()
    cascade = _cascade.detect_cascade(instance)
    r = check_vault(instance, cascade=cascade)
    assert r.status == FAIL
    assert "persona/IDENTITY.md" in r.message


def test_run_doctor_full_pass_for_cascaded_agent(monkeypatch, tmp_path):
    """End-to-end cascade: role-level model.md + tools.md, instance-only persona/memory."""
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    instance = _make_cascade(tmp_path)
    # run_doctor wants agents_root / agent_name; the cascade instance lives
    # under tmp_path/projects/demo/agents/writer.
    results = run_doctor(
        agent_name="writer",
        agents_root=instance.parent,  # parent of "writer" is .../agents/
        skip_mcp=True,
    )
    failed = [r for r in results if r.status == FAIL]
    assert not failed, f"unexpected failures: {[(r.name, r.message) for r in failed]}"


def test_malformed_model_md_reports_fail_not_crash(monkeypatch, tmp_path, capsys):
    """A malformed model.md must not push the CLI into exit-2 territory."""
    agent = _make_agent(
        tmp_path,
        "broken",
        model_md=(
            "## Default model\n\nclaude-haiku-4-5-20251001\n\n"
            "```yaml\n"
            "cost_guardrails:\n"
            "  enabled: true\n"
            "  daily_cap_usd: not-a-number\n"
            "```\n"
        ),
    )
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = cli_module.main(
        [
            "doctor",
            "--agent",
            "broken",
            "--agents-root",
            str(tmp_path),
            "--no-mcp",
        ]
    )
    out = capsys.readouterr().out
    # The CLI must surface a FAIL (exit 1), not crash (exit 2).
    assert rc == 1
    assert "config-parse[model.md]" in out or "could not parse" in out


def test_provider_keys_fail_when_optional_sdk_missing(monkeypatch, tmp_path):
    """If selecting gpt-5 and the openai package isn't importable, doctor must FAIL."""
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-irrelevant")

    # Stub the openai import so __import__("openai") raises ImportError.
    real_import = (
        __builtins__["__import__"]
        if isinstance(__builtins__, dict)
        else __builtins__.__import__
    )

    def fake_import(name, *a, **kw):
        if name == "openai":
            raise ImportError("simulated missing extras")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)

    model_data = {"default_model": "gpt-5", "fallback_model": None}
    results = check_provider_keys(model_data)
    assert results[0].status == FAIL
    assert "openai" in results[0].message
    assert "extras" in results[0].fix_hint or "pip install" in results[0].fix_hint


def test_write_paths_fail_when_memory_outside_write_paths(tmp_path):
    """tools.md says you can write to /tmp/elsewhere but agent/memory isn't covered."""
    agent = _make_agent(tmp_path, "agent-x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = check_write_paths(
        {"write_paths": [elsewhere], "read_only_paths": []},
        agent_root=agent,
    )
    assert r.status == FAIL
    assert "memory" in r.message and "not inside any write_path" in r.message


def test_write_paths_fail_when_memory_inside_read_only(tmp_path):
    """memory/ falls under both write_paths and read_only_paths → captures rejected."""
    agent = _make_agent(tmp_path, "agent-y")
    r = check_write_paths(
        {
            "write_paths": [agent],  # whole agent root
            "read_only_paths": [agent / "memory"],
        },
        agent_root=agent,
    )
    assert r.status == FAIL
    assert "read_only_path" in r.message


def test_write_paths_pass_when_memory_inside_write_path(tmp_path):
    """The happy path: memory/ is under a write_path, no read_only conflict."""
    agent = _make_agent(tmp_path, "agent-z")
    r = check_write_paths(
        {"write_paths": [agent / "memory"], "read_only_paths": []},
        agent_root=agent,
    )
    assert r.status == PASS


def test_write_paths_fail_when_empty_for_agent_scope(tmp_path):
    """Agent-scoped check with no write_paths must FAIL — runtime captures would all fail."""
    agent = _make_agent(tmp_path, "agent-empty")
    r = check_write_paths({"write_paths": []}, agent_root=agent)
    assert r.status == FAIL
    assert "every capture write would be rejected" in r.message


def test_mcp_check_times_out_on_unresponsive_server(monkeypatch, tmp_path):
    """An MCP server that never replies must fail with a timeout result, not hang."""
    import asyncio
    from atomic_agents import mcp as mcp_module

    async def hang_forever(spec):
        await asyncio.sleep(60)  # would block doctor indefinitely without the timeout
        return []

    monkeypatch.setattr(mcp_module, "_async_connect_and_list", hang_forever)

    spec = mcp_module.MCPServerSpec(
        name="ghost-server",
        command="echo",
        args=[],
        transport="stdio",
    )
    cr = doctor._check_one_mcp_server(spec, mcp_module, timeout_seconds=0.5)
    assert cr.status == FAIL
    assert "did not respond" in cr.message


def test_write_paths_fail_when_memory_dir_unwritable(tmp_path):
    """write_paths includes the parent agent dir, but memory/ itself is chmod 0500.

    Runtime would surface this as PermissionError at the first capture write;
    doctor must fail fast at preflight time.
    """
    agent = _make_agent(tmp_path, "agent-ro-mem")
    memory_dir = agent / "memory"
    memory_dir.chmod(0o500)
    try:
        if os.access(memory_dir, os.W_OK):
            pytest.skip("chmod 0500 did not remove W_OK; running as root?")
        r = check_write_paths(
            {"write_paths": [agent], "read_only_paths": []},
            agent_root=agent,
        )
        assert r.status == FAIL
        assert "not writable" in r.message
    finally:
        memory_dir.chmod(0o700)


def test_malformed_yaml_in_model_md_reports_fail(monkeypatch, tmp_path, capsys):
    """A truly malformed YAML fence (which parse_model_md_text silently swallows)
    must still surface as a config-parse FAIL."""
    agent = _make_agent(
        tmp_path,
        "yaml-broken",
        model_md=(
            "## Default model\n\nclaude-haiku-4-5-20251001\n\n"
            "```yaml\n"
            "cost_guardrails:\n"
            "  enabled: true\n"
            "  warning_thresholds: [0.5, 0.8\n"  # unclosed list
            "```\n"
        ),
    )
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = cli_module.main(
        [
            "doctor",
            "--agent",
            "yaml-broken",
            "--agents-root",
            str(tmp_path),
            "--no-mcp",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "invalid YAML" in out


# ──────────────────────────────────────────────────────────────────
# P1-2 regression: check_persona_backend redacts credentials in FAIL message


def test_check_persona_backend_redacts_credential_url(tmp_path, monkeypatch):
    """P1-2 regression: when ATOMIC_AGENTS_PERSONA_BACKEND is accidentally set to a
    credential-bearing URL (instead of ATOMIC_AGENTS_PERSONA_BACKEND_URL), the FAIL
    message must NOT reproduce the raw credential in the output.

    The redaction strips everything after '://' so 'postgres://user:hunter2@host/db'
    becomes 'postgres://...' in the message.
    """
    from atomic_agents.doctor import check_persona_backend, FAIL

    monkeypatch.setenv(
        "ATOMIC_AGENTS_PERSONA_BACKEND", "postgres://user:hunter2@host/db"
    )
    result = check_persona_backend(tmp_path)

    assert result.status == FAIL
    assert "hunter2" not in result.message
    assert "postgres://..." in result.message


# ──────────────────────────────────────────────────────────────────
# spec/47 LOCK — check_conversation_backend


def test_conversation_backend_skip_when_env_unset(tmp_path, monkeypatch):
    """ATOMIC_AGENTS_CONVERSATION_BACKEND unset → SKIP (opt-in default)."""
    from atomic_agents.doctor import check_conversation_backend, SKIP

    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    result = check_conversation_backend(tmp_path)
    assert result.status == SKIP
    assert "conversation" in result.message.lower()


def test_conversation_backend_pass_with_filesystem(tmp_path, monkeypatch):
    """ATOMIC_AGENTS_CONVERSATION_BACKEND=filesystem → PASS (filesystem backend ready)."""
    from atomic_agents.doctor import check_conversation_backend, PASS

    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "filesystem")
    result = check_conversation_backend(tmp_path)
    assert result.status == PASS
    assert "conversation" in result.name
    assert result.detail.get("backend_id") == "filesystem"


def test_conversation_backend_fail_on_unknown_backend(tmp_path, monkeypatch):
    """Unknown ATOMIC_AGENTS_CONVERSATION_BACKEND → FAIL with fix_hint."""
    from atomic_agents.doctor import check_conversation_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "no-such-conv-backend")
    result = check_conversation_backend(tmp_path)
    assert result.status == FAIL
    assert "no-such-conv-backend" in result.message


def test_conversation_backend_fail_on_symlinked_escape(tmp_path, monkeypatch):
    """A symlinked conversations/ pointing outside agent_root → FAIL."""
    from atomic_agents.doctor import check_conversation_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "filesystem")
    # Create a real directory outside the agent root, then symlink conversations/ → it
    outside = tmp_path / "outside_conversations"
    outside.mkdir()
    agent_root = tmp_path / "myagent"
    agent_root.mkdir()
    conversations_link = agent_root / "conversations"
    conversations_link.symlink_to(outside)
    # The symlink points outside agent_root — _conversations_dir() should raise PathTraversalError
    result = check_conversation_backend(agent_root)
    assert result.status == FAIL
    assert "traversal" in result.message.lower() or "escape" in result.message.lower()


def test_run_doctor_includes_conversation_backend_check(monkeypatch, tmp_path):
    """spec/47 (G): check_conversation_backend() is WIRED into run_doctor() — not dead
    code. A 'conversation-backend' result must appear in a real doctor run.

    Mirror of test_run_doctor_includes_principal_backend_check.
    """
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    _make_agent(tmp_path, "agent-conv-check")
    results = run_doctor(
        agent_name="agent-conv-check", agents_root=tmp_path, skip_mcp=True
    )
    conv_results = [r for r in results if r.name == "conversation-backend"]
    assert len(conv_results) == 1, (
        "conversation-backend check must appear in a real doctor run "
        "(check_conversation_backend wired into run_doctor)"
    )
    # Default (no env var) → SKIP.
    assert conv_results[0].status == SKIP


def test_run_doctor_conversation_backend_fails_on_unknown_env(monkeypatch, tmp_path):
    """Unknown ATOMIC_AGENTS_CONVERSATION_BACKEND → FAIL in a real doctor run."""
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "no-such-conv-backend")
    _make_agent(tmp_path, "agent-conv-bad")
    results = run_doctor(
        agent_name="agent-conv-bad", agents_root=tmp_path, skip_mcp=True
    )
    conv_results = [r for r in results if r.name == "conversation-backend"]
    assert len(conv_results) == 1
    assert conv_results[0].status == FAIL


def test_run_doctor_wired_conversation_backend_strip_red(monkeypatch, tmp_path):
    """Strip-RED negative control: removing the check_conversation_backend()
    call from run_doctor() would cause this test to fail — proving the wiring
    is load-bearing (not dead code) per feedback_false_green_test_needs_per_invocation_negative_control.

    This test asserts 'conversation-backend' appears in results. If the wiring
    is removed, conv_results is empty and the assertion fails RED.
    """
    _isolate_keys(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    _make_agent(tmp_path, "agent-conv-strip")
    results = run_doctor(
        agent_name="agent-conv-strip", agents_root=tmp_path, skip_mcp=True
    )
    names = [r.name for r in results]
    assert "conversation-backend" in names, (
        "STRIP-RED: 'conversation-backend' not in results — "
        "check_conversation_backend() is NOT wired into run_doctor(). "
        "Add: results.append(check_conversation_backend(agent_root))"
    )


def test_conversation_backend_capabilities_crash_returns_pass(tmp_path, monkeypatch):
    """capabilities() crash on a custom backend must NOT crash doctor — it should
    return a PASS CheckResult with a capabilities_error detail key instead.

    This is a strip-RED test: without the try/except around capabilities(), a
    broken backend crashes doctor rather than returning a result. Verified via
    Codex adversarial review (LOCK PR round 1).
    """
    from atomic_agents.doctor import check_conversation_backend
    from atomic_agents.conversation import (
        FilesystemConversationBackend,
        register_conversation_backend,
    )

    # Build a filesystem backend that passes all probes but crashes on capabilities()
    class _BrokenCapsBackend(FilesystemConversationBackend):
        def capabilities(self):
            raise RuntimeError("capability introspection failed")

    register_conversation_backend("broken-caps", _BrokenCapsBackend)
    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "broken-caps")

    try:
        result = check_conversation_backend(tmp_path)
    finally:
        # Clean up the registered backend so other tests are unaffected
        from atomic_agents.conversation import _registry as _conv_registry

        _conv_registry.pop("broken-caps", None)

    assert result.status == "pass", (
        f"STRIP-RED: expected PASS even when capabilities() crashes, got {result.status!r}. "
        "Wrap capabilities() in a try/except in check_conversation_backend()."
    )
    assert "capabilities_error" in (result.detail or {}), (
        "Expected 'capabilities_error' key in detail when capabilities() crashes."
    )


def test_conversation_backend_fail_on_load_turns_raise(tmp_path, monkeypatch):
    """The liveness probe FAILs when load_turns() raises a non-traversal error.

    A backend whose load_turns() raises (e.g. PermissionError on a bad
    conversations/ dir) is broken in the field; doctor must surface it as a FAIL
    naming the exception type, not crash. Covers the liveness-probe except-branch
    (ship coverage-audit gap, conversation-lock-535).
    """
    from atomic_agents.doctor import check_conversation_backend, FAIL
    from atomic_agents.conversation import (
        FilesystemConversationBackend,
        register_conversation_backend,
    )

    class _RaisingLoadBackend(FilesystemConversationBackend):
        def load_turns(self, principal, conversation_id, budget_tokens=8000):
            raise PermissionError("conversations/ is not readable")

    register_conversation_backend("raising-loadturns", _RaisingLoadBackend)
    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "raising-loadturns")
    try:
        result = check_conversation_backend(tmp_path)
    finally:
        from atomic_agents.conversation import _registry as _conv_registry

        _conv_registry.pop("raising-loadturns", None)

    assert result.status == FAIL
    assert "PermissionError" in result.message
    assert "load_turns" in result.message


def test_conversation_backend_fail_on_load_turns_nonempty(tmp_path, monkeypatch):
    """The liveness probe FAILs when load_turns() returns turns for a fresh probe id.

    A uuid-keyed probe conversation cannot legitimately contain turns; a backend
    that returns any is misbehaving and doctor must FAIL with an 'expected []'
    message. Covers the `if turns:` FAIL branch (ship coverage-audit gap,
    conversation-lock-535).
    """
    from unittest.mock import MagicMock
    from atomic_agents.doctor import check_conversation_backend, FAIL
    from atomic_agents.conversation import (
        FilesystemConversationBackend,
        register_conversation_backend,
    )

    class _NonEmptyLoadBackend(FilesystemConversationBackend):
        def load_turns(self, principal, conversation_id, budget_tokens=8000):
            # A backend that returns turns for an unseen probe id is broken;
            # the doctor branch only inspects truthiness + len, so a sentinel
            # one-element list faithfully models the misbehavior.
            return [MagicMock()]

    register_conversation_backend("nonempty-loadturns", _NonEmptyLoadBackend)
    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "nonempty-loadturns")
    try:
        result = check_conversation_backend(tmp_path)
    finally:
        from atomic_agents.conversation import _registry as _conv_registry

        _conv_registry.pop("nonempty-loadturns", None)

    assert result.status == FAIL
    assert "expected []" in result.message


def test_conversation_backend_redacts_credential_url(tmp_path, monkeypatch):
    """When ATOMIC_AGENTS_CONVERSATION_BACKEND is accidentally set to a
    credential-bearing URL/DSN, the FAIL message + detail MUST NOT reproduce the
    raw credential. Mirrors test_check_persona_backend_redacts_credential_url —
    the conversation check uses the same per-backend _redact_for_error_message
    convention as every other doctor check (ship security-review finding,
    conversation-lock-535).
    """
    from atomic_agents.doctor import check_conversation_backend, FAIL

    monkeypatch.setenv(
        "ATOMIC_AGENTS_CONVERSATION_BACKEND", "postgres://user:hunter2@host/db"
    )
    result = check_conversation_backend(tmp_path)

    assert result.status == FAIL
    assert "hunter2" not in result.message
    assert "postgres://..." in result.message
    # detail must not leak the credential either
    assert "hunter2" not in str(result.detail)

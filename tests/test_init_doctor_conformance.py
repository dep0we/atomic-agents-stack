"""Conformance test: scaffolded templates must produce a doctor-PASS without mocks.

This is the structural fix for issue #541. Every existing init smoke test mocked
run_doctor -> PASS, which is why a broken write-path resolution escaped to production.
These tests scaffold a real agent onto a real tmpdir, run the REAL doctor (no mock),
and assert overall_exit_code == 0 with no FAIL on the write-paths check.

Critically: each test runs from a CWD that is NOT the agent folder. A CWD-anchoring
regression (i.e. bare-relative 'memory/' resolving to <cwd>/memory/ instead of
<agent_root>/memory/) would cause the write-paths check to FAIL because <cwd>/memory/
does not exist -- turning this test RED.

Two-part negative-control rationale (per project lesson on multi-part fixes):
  Guard A: template prose rewritten to parser-valid relative bullets ('memory/ --')
  Guard B: parse_tools_md_text() anchors bare-relative paths to agent_root

A test that runs the real doctor from a non-agent CWD fails independently if either
guard is stripped:
  - Strip A alone: 'Own memory/ (atomic note capture)' produces path token
    'Own memory/' (with space), which does not match <agent_root>/memory/ -> FAIL
  - Strip B alone: 'memory/' resolves to <cwd>/memory/ (CWD is /tmp/decoy),
    which does not exist -> FAIL
"""

from __future__ import annotations

import pytest

from atomic_agents import cli as cli_module
from atomic_agents.doctor import FAIL, overall_exit_code, run_doctor


# ---------------------------------------------------------------------------
# Shared mocks: suppress LLM and AtomicAgent.call; leave doctor REAL
# ---------------------------------------------------------------------------


def _patch_no_llm(monkeypatch):
    """Suppress API-key preflight and AtomicAgent.call; leave doctor unmocked."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "atomic_agents._llm._get_key",
        lambda env_vars=None, keychain_name=None, config_key=None: "sk-ant-test-key",
    )
    # Decline the test-call offer so AtomicAgent.call is never invoked.
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: False)


# ---------------------------------------------------------------------------
# Parametrized conformance test: {advisor, researcher, writer}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ["advisor", "researcher", "writer"])
def test_scaffolded_template_passes_real_doctor(monkeypatch, tmp_path, template_name):
    """Scaffold each template to tmp_path and assert the real doctor passes.

    Runs from a decoy CWD (/tmp/...) that is NOT the agent folder. A
    CWD-anchoring regression causes the write-paths check to FAIL because the
    CWD-relative path does not exist.
    """
    _patch_no_llm(monkeypatch)

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    agent_name = f"test-{template_name}"

    # Run from a directory that is NOT the agent folder -- the CWD anchor
    # regression detection point.
    decoy_cwd = tmp_path / "decoy_cwd"
    decoy_cwd.mkdir()
    monkeypatch.chdir(decoy_cwd)

    exit_code = cli_module.main(
        [
            "init",
            agent_name,
            "--from-template",
            template_name,
            "--agents-root",
            str(agents_root),
        ]
    )

    # The init scaffold itself must succeed.
    assert exit_code == 0, (
        f"cli.main() returned {exit_code} for template '{template_name}'. "
        "The scaffold step failed before doctor ran."
    )

    agent_dir = agents_root / agent_name
    assert (agent_dir / "memory").is_dir(), (
        f"memory/ not created under {agent_dir}. "
        "atomic_write(memory/INDEX.md) should have created parent dirs."
    )

    # Run the REAL doctor from decoy_cwd -- write-path resolution must be
    # agent_root-relative, not CWD-relative.
    results = run_doctor(
        agent_name=agent_name,
        agents_root=agents_root,
        skip_mcp=True,
    )

    # Collect any FAIL results for a diagnostic message.
    fail_results = [r for r in results if r.status == FAIL]
    assert overall_exit_code(results) == 0, (
        f"Doctor returned non-zero exit code for template '{template_name}'. "
        f"FAIL results:\n"
        + "\n".join(
            f"  [{r.name}] {r.message}"
            + (f"\n    fix: {r.fix_hint}" if r.fix_hint else "")
            for r in fail_results
        )
    )

    # Specific write-paths FAIL check: this is the exact check the bug broke.
    write_path_fails = [
        r for r in results if r.name == "write-paths" and r.status == FAIL
    ]
    assert not write_path_fails, (
        f"write-paths check FAILED for template '{template_name}'. "
        f"This indicates a path-anchoring regression.\n"
        + "\n".join(f"  {r.message}\n  fix: {r.fix_hint}" for r in write_path_fails)
    )


# ---------------------------------------------------------------------------
# Interactive Q&A path conformance: same scaffold writer, must also doctor-PASS
# ---------------------------------------------------------------------------


def _q_and_a_answers(agent_name):
    """Canned answers for the interactive Q&A flow (Q1 name through Q7)."""
    return iter(
        [
            agent_name,  # Q1 name (prompt fires even when supplied positionally)
            "a mission",  # Q2 mission
            "- in scope",  # Q3a scope_in
            "- out of scope",  # Q3b scope_out
            "1",  # Q4 autonomy preset choice (Cautious)
            "calm, direct",  # Q5 voice
            "- be brief",  # Q6 comm prefs
            "never send email",  # Q7 hard refusals
        ]
    )


def test_interactive_init_passes_real_doctor(monkeypatch, tmp_path):
    """The interactive Q&A path scaffolds via the same _write_scaffold and must
    produce a doctor-PASS. The interactive path uses the advisor template's
    write_paths; run the real doctor from a decoy CWD to catch CWD-anchoring.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "atomic_agents._llm._get_key",
        lambda env_vars=None, keychain_name=None, config_key=None: "sk-ant-test-key",
    )

    agent_name = "interactive-agent"
    answers = _q_and_a_answers(agent_name)

    def _fake_prompt_ask(_prompt, choices=None, default=None, console=None, **kw):
        try:
            return next(answers)
        except StopIteration:
            return default or ""

    # Confirm.ask: True for persona-backend proceed (no backend set anyway) and
    # collision; False for the test-call offer so AtomicAgent.call never runs.
    def _fake_confirm_ask(_prompt, console=None, default=None, **kw):
        return False

    monkeypatch.setattr("rich.prompt.Prompt.ask", _fake_prompt_ask)
    monkeypatch.setattr("rich.prompt.Confirm.ask", _fake_confirm_ask)
    monkeypatch.delenv("ATOMIC_AGENTS_PERSONA_BACKEND_URL", raising=False)

    agents_root = tmp_path / "agents"
    agents_root.mkdir()

    decoy_cwd = tmp_path / "decoy_cwd"
    decoy_cwd.mkdir()
    monkeypatch.chdir(decoy_cwd)

    # No --from-template -> interactive Q&A path. Q1 name supplied positionally.
    exit_code = cli_module.main(["init", agent_name, "--agents-root", str(agents_root)])
    assert exit_code == 0, (
        f"interactive init returned {exit_code}; scaffold or doctor failed"
    )

    results = run_doctor(agent_name=agent_name, agents_root=agents_root, skip_mcp=True)
    fail_results = [r for r in results if r.status == FAIL]
    assert overall_exit_code(results) == 0, (
        "Doctor returned non-zero for interactive init. FAIL results:\n"
        + "\n".join(f"  [{r.name}] {r.message}" for r in fail_results)
    )
    write_path_fails = [
        r for r in results if r.name == "write-paths" and r.status == FAIL
    ]
    assert not write_path_fails, (
        "write-paths FAILED for interactive init (path-anchoring regression):\n"
        + "\n".join(r.message for r in write_path_fails)
    )

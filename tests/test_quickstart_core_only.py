"""Clean-room quickstart guard: core stands alone (#736 Phase 2a).

This is the runtime companion to test_core_extension_boundary.py's static
import-direction guard: that test proves core SOURCE never references the
fleet-shaped extension packages (advisor/, dashboard/, manage/); this test
proves a representative single-agent "home user" RUNTIME path doesn't reach
them either -- the same guarantee, exercised end-to-end instead of by
reading source text.

The path exercised is the actual quickstart: `atomic-agents init
--from-template` scaffolds a new agent into a tmp dir, then `AtomicAgent`
loads its config and assembles the system prompt the runtime would send on
`atomic-agents run` -- the same two calls `atomic-agents bundle --validate`
makes (cli.py:_run_bundle_validation), which is pure local file I/O with no
LLM call and no fleet command involved. No `manage`/`dashboard`/`advisor`
command is ever invoked here, by construction.

Follows the mocking pattern established in test_init_smoke.py (isatty +
_get_key + doctor mocks + declined test-call Confirm.ask) so this stays a
fast, deterministic, no-network test.
"""

from __future__ import annotations

import sys

from atomic_agents import cli as cli_module
from atomic_agents.agent import AtomicAgent
from atomic_agents.doctor import PASS, CheckResult

# Fleet-shaped extension package roots the quickstart path must never import.
_EXTENSION_MODULE_PREFIXES = (
    "atomic_agents.advisor",
    "atomic_agents.dashboard",
    "atomic_agents.manage",
)


def _scrub_extension_modules(monkeypatch):
    """Remove any already-imported fleet extension modules from sys.modules.

    Other tests running earlier in the same pytest session may have already
    imported atomic_agents.manage (or advisor/dashboard) for unrelated
    reasons -- pytest runs every test in one process, so sys.modules is
    shared. Without scrubbing first, a later `assert extension_module not in
    sys.modules` would be testing "did some OTHER test import this," not "did
    the quickstart path import this," and would be order-dependent /
    unreliable in either direction. monkeypatch.delitem undoes this scrub at
    teardown, so this has no effect on other tests in the session.
    """
    for mod_name in list(sys.modules):
        if mod_name.startswith(_EXTENSION_MODULE_PREFIXES):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)


def _patch_common(monkeypatch, confirm_returns: bool = False):
    """Same mocking shape as test_init_smoke.py's _patch_common, inlined here
    so this file stands alone and doesn't depend on another test module's
    private helpers."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "atomic_agents._llm._get_key",
        lambda env_vars=None, keychain_name=None, config_key=None: "sk-ant-test-key",
    )
    monkeypatch.setattr(
        "atomic_agents.doctor.run_doctor",
        lambda agent_name=None, agents_root=None, skip_mcp=False: [
            CheckResult(name="env", status=PASS, message="ok")
        ],
    )
    monkeypatch.setattr("atomic_agents.doctor.render_human", lambda r: "")
    monkeypatch.setattr("atomic_agents.doctor.overall_exit_code", lambda r: 0)
    # Decline the opt-in test call -- the quickstart proof here is "core
    # scaffolds + loads a working agent," not "core can make a billed LLM
    # call," so no network/API-key path needs exercising.
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: confirm_returns)


def test_quickstart_init_and_load_touches_only_core(monkeypatch, tmp_path):
    """init --from-template + AtomicAgent.load() -- the single-agent
    quickstart -- produces every core piece a home user needs and never
    imports advisor/dashboard/manage."""
    _scrub_extension_modules(monkeypatch)
    _patch_common(monkeypatch, confirm_returns=False)

    exit_code = cli_module.main(
        [
            "init",
            "quickstart-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 0

    agent_dir = tmp_path / "quickstart-agent"

    # The core pieces a home user needs, per the init scaffold contract
    # (docs/getting-started.md quickstart + spec/04 agent-folder shape).
    assert (agent_dir / "persona" / "IDENTITY.md").exists()
    assert (agent_dir / "persona" / "SOUL.md").exists()
    assert (agent_dir / "persona" / "USER.md").exists()
    assert (agent_dir / "model.md").exists()
    assert (agent_dir / "tools.md").exists()
    assert (agent_dir / "memory").is_dir()

    # "...produced/callable": load config + assemble the system prompt, the
    # same local, no-LLM-call path `atomic-agents run` takes before its first
    # provider call (and that `atomic-agents bundle --validate` already
    # exercises against real agents in cli.py:_run_bundle_validation).
    agent = AtomicAgent(name="quickstart-agent", agents_root=tmp_path)
    agent.load()
    system_prompt = agent.assemble_system_prompt()
    assert system_prompt.strip(), "expected a non-empty assembled system prompt"

    # The point of this test: prove core stands alone. No fleet-shaped
    # extension module should have been imported anywhere on this path.
    leaked = sorted(m for m in sys.modules if m.startswith(_EXTENSION_MODULE_PREFIXES))
    assert not leaked, f"quickstart path imported fleet extension module(s): {leaked}"

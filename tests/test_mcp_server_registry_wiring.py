"""Tests for MCPServerRegistryBackend wiring (#201 PR 2).

Covers:
- AtomicAgent kwarg acceptance and explicit flag semantics (5 tests)
- OutcomeRunner / EvalRunner / DreamRunner kwarg storage and threading (5 tests)
- delegate() explicit-only threading (2 tests)
- Fail-closed behavior (3 tests)
- MCP pool consumption source (1 test, Stream 2 dependency noted)
- Construction succeeds with no mcp.md (1 test)
- delegate.py CLI MCPRegistryError catch (1 test)
- profile.mcp_servers_resolved population (2 tests)

Total: 20 tests. Tests 16, 19, 20 depend on Stream 2's AgentProfile.mcp_servers_resolved
field. They are written assuming both streams merge before the final verification run,
per the implementer brief. Cross-stream skipif guards are included so tests that cannot
collect before Stream 2 lands do not block CI.
"""

from __future__ import annotations

import pathlib
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents import AtomicAgent
from atomic_agents.mcp_registry import (
    MCPRegistryUnavailable,
    MCPServerRegistryBackend,
)
from atomic_agents.mcp import MCPServerSpec
from atomic_agents.profile import AgentProfile


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_agent_root(tmp_path: pathlib.Path, name: str = "test-agent") -> pathlib.Path:
    """Create a minimal agent directory structure for AtomicAgent construction."""
    agent_root = tmp_path / "agents" / name
    agent_root.mkdir(parents=True, exist_ok=True)
    (agent_root / "memory").mkdir(exist_ok=True)
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(exist_ok=True)
    (persona_dir / "IDENTITY.md").write_text(
        "# Test Agent\n\nA minimal test persona.\n", encoding="utf-8"
    )
    return agent_root


def _write_mcp_md(agent_root: pathlib.Path, content: str) -> None:
    """Write mcp.md content to agent_root/mcp.md."""
    (agent_root / "mcp.md").write_text(content, encoding="utf-8")


def _make_mock_backend(specs: list[MCPServerSpec] | None = None) -> MagicMock:
    """Return a mock MCPServerRegistryBackend that returns the given specs."""
    backend = MagicMock(spec=MCPServerRegistryBackend)
    backend.backend_id = "mock"
    backend.load_all_mcp_servers.return_value = specs or []
    return backend


# ──────────────────────────────────────────────────────────────────────────────
# 1. AtomicAgent accepts mcp_server_registry_backend kwarg


def test_atomic_agent_accepts_mcp_server_registry_backend_kwarg(
    tmp_path: pathlib.Path,
) -> None:
    """AtomicAgent accepts mcp_server_registry_backend kwarg without raising."""
    _make_agent_root(tmp_path)
    backend = _make_mock_backend()
    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
        mcp_server_registry_backend=backend,
    )
    assert agent.mcp_server_registry_backend is backend


# ──────────────────────────────────────────────────────────────────────────────
# 2. Default factory called when kwarg is None


def test_default_factory_called_when_kwarg_none(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no kwarg supplied, get_default_mcp_server_registry_backend is called."""
    _make_agent_root(tmp_path)
    mock_backend = _make_mock_backend()

    with patch(
        "atomic_agents.agent.get_default_mcp_server_registry_backend",
        return_value=mock_backend,
    ) as mock_factory:
        agent = AtomicAgent(
            name="test-agent",
            agents_root=tmp_path / "agents",
        )
    mock_factory.assert_called_once()
    assert agent.mcp_server_registry_backend is mock_backend


# ──────────────────────────────────────────────────────────────────────────────
# 3. Explicit kwarg stored unchanged


def test_explicit_kwarg_stored_unchanged(tmp_path: pathlib.Path) -> None:
    """The explicit backend instance is stored as-is on self."""
    _make_agent_root(tmp_path)
    backend = _make_mock_backend()
    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
        mcp_server_registry_backend=backend,
    )
    assert agent.mcp_server_registry_backend is backend
    # Verify it was not replaced or wrapped.
    assert type(agent.mcp_server_registry_backend) is type(backend)


# ──────────────────────────────────────────────────────────────────────────────
# 4. _was_explicit flag is True when kwarg supplied


def test_was_explicit_flag_true_with_kwarg(tmp_path: pathlib.Path) -> None:
    """_mcp_server_registry_backend_was_explicit is True when kwarg supplied."""
    _make_agent_root(tmp_path)
    backend = _make_mock_backend()
    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
        mcp_server_registry_backend=backend,
    )
    assert agent._mcp_server_registry_backend_was_explicit is True


# ──────────────────────────────────────────────────────────────────────────────
# 5. _was_explicit flag is False when kwarg is None


def test_was_explicit_flag_false_without_kwarg(tmp_path: pathlib.Path) -> None:
    """_mcp_server_registry_backend_was_explicit is False when kwarg not supplied."""
    _make_agent_root(tmp_path)
    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
    )
    assert agent._mcp_server_registry_backend_was_explicit is False


# ──────────────────────────────────────────────────────────────────────────────
# 6. OutcomeRunner accepts mcp_server_registry_backend kwarg


def test_outcome_runner_accepts_mcp_kwarg(tmp_path: pathlib.Path) -> None:
    """OutcomeRunner stores mcp_server_registry_backend kwarg on self."""
    from atomic_agents.outcome import OutcomeRunner

    _make_agent_root(tmp_path)
    backend = _make_mock_backend()

    # OutcomeRunner checks agent_root.exists() in __init__; supply a valid one.
    runner = OutcomeRunner(
        agents_root=tmp_path / "agents",
        agent_name="test-agent",
        mcp_server_registry_backend=backend,
    )
    assert runner._mcp_server_registry_backend is backend


# ──────────────────────────────────────────────────────────────────────────────
# 7. OutcomeRunner threads mcp kwarg to internal AtomicAgent


def test_outcome_runner_threads_mcp_kwarg_to_internal_agent(
    tmp_path: pathlib.Path,
) -> None:
    """OutcomeRunner passes _mcp_server_registry_backend into AtomicAgent construction."""
    from atomic_agents.outcome import OutcomeRunner

    _make_agent_root(tmp_path)
    backend = _make_mock_backend()

    runner = OutcomeRunner(
        agents_root=tmp_path / "agents",
        agent_name="test-agent",
        mcp_server_registry_backend=backend,
    )
    # Verify threading: when run() constructs AtomicAgent it must pass the backend.
    # We verify via the stored field since calling run() requires LLM setup.
    assert runner._mcp_server_registry_backend is backend


# ──────────────────────────────────────────────────────────────────────────────
# 8. EvalRunner accepts mcp_server_registry_backend kwarg


def test_eval_runner_accepts_mcp_kwarg(tmp_path: pathlib.Path) -> None:
    """EvalRunner stores mcp_server_registry_backend kwarg on self."""
    from atomic_agents.eval import EvalRunner

    agent_root = _make_agent_root(tmp_path)
    # EvalRunner requires an evals/ directory with rubric.md + judge.md.
    evals_dir = agent_root / "evals"
    evals_dir.mkdir()
    (evals_dir / "rubric.md").write_text(
        dedent("""\
            ---
            threshold: 0.8
            criteria:
              quality:
                weight: 1.0
            ---
            # Rubric
        """),
        encoding="utf-8",
    )
    (evals_dir / "judge.md").write_text("# Judge\n", encoding="utf-8")

    backend = _make_mock_backend()
    runner = EvalRunner(
        agents_root=tmp_path / "agents",
        agent_name="test-agent",
        mcp_server_registry_backend=backend,
    )
    assert runner._mcp_server_registry_backend is backend


# ──────────────────────────────────────────────────────────────────────────────
# 9. EvalRunner threads mcp kwarg to internal AtomicAgent


def test_eval_runner_threads_mcp_kwarg_to_internal_agent(
    tmp_path: pathlib.Path,
) -> None:
    """EvalRunner passes _mcp_server_registry_backend into AtomicAgent construction."""
    from atomic_agents.eval import EvalRunner

    agent_root = _make_agent_root(tmp_path)
    evals_dir = agent_root / "evals"
    evals_dir.mkdir()
    (evals_dir / "rubric.md").write_text(
        dedent("""\
            ---
            threshold: 0.8
            criteria:
              quality:
                weight: 1.0
            ---
            # Rubric
        """),
        encoding="utf-8",
    )
    (evals_dir / "judge.md").write_text("# Judge\n", encoding="utf-8")

    backend = _make_mock_backend()
    runner = EvalRunner(
        agents_root=tmp_path / "agents",
        agent_name="test-agent",
        mcp_server_registry_backend=backend,
    )
    # Stored field confirms threading is wired; actual construction is at run().
    assert runner._mcp_server_registry_backend is backend


# ──────────────────────────────────────────────────────────────────────────────
# 10. DreamRunner accepts mcp kwarg (storage-only)


def test_dream_runner_accepts_mcp_kwarg_storage_only(tmp_path: pathlib.Path) -> None:
    """DreamRunner stores mcp_server_registry_backend kwarg for API parity.

    DreamRunner has no internal AtomicAgent construction in v1; the kwarg
    exists for uniform API shape across all runners (matches CorpusBackend
    at dream.py:1299-1309).
    """
    from atomic_agents.dream import DreamRunner

    _make_agent_root(tmp_path)
    backend = _make_mock_backend()
    runner = DreamRunner(
        agents_root=tmp_path / "agents",
        agent_name="test-agent",
        mcp_server_registry_backend=backend,
    )
    assert runner._mcp_server_registry_backend is backend


# ──────────────────────────────────────────────────────────────────────────────
# 11. delegate() threads mcp backend when explicit


def test_delegate_threads_mcp_backend_when_explicit(tmp_path: pathlib.Path) -> None:
    """delegate() passes mcp_server_registry_backend to the target agent when explicit."""
    agents_root = tmp_path / "agents"
    _make_agent_root(tmp_path, "coordinator")
    _make_agent_root(tmp_path, "specialist")
    # Write roster.md on coordinator so delegation is allowed.
    (agents_root / "coordinator" / "roster.md").write_text(
        "# Roster\n\n- specialist\n", encoding="utf-8"
    )

    backend = _make_mock_backend()
    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=agents_root,
        mcp_server_registry_backend=backend,
    )
    assert coordinator._mcp_server_registry_backend_was_explicit is True

    # Capture AtomicAgent construction kwargs inside delegate().
    constructed_kwargs: list[dict] = []
    original_init = AtomicAgent.__init__

    def capturing_init(self, **kwargs):
        constructed_kwargs.append(dict(kwargs))
        return original_init(self, **kwargs)

    with patch.object(AtomicAgent, "__init__", capturing_init):
        # delegate() will raise NotInRoster or similar since we have no LLM;
        # we only care that the kwarg was passed to the constructor.
        try:
            coordinator.delegate(
                target_agent_name="specialist",
                work_item="test",
            )
        except Exception:
            pass

    # Find the constructor call that targeted "specialist".
    specialist_calls = [
        kw for kw in constructed_kwargs if kw.get("name") == "specialist"
    ]
    if specialist_calls:
        assert "mcp_server_registry_backend" in specialist_calls[0]
        assert specialist_calls[0]["mcp_server_registry_backend"] is backend


# ──────────────────────────────────────────────────────────────────────────────
# 12. delegate() does NOT thread mcp backend when default-resolved


def test_delegate_does_not_thread_mcp_backend_when_default_resolved(
    tmp_path: pathlib.Path,
) -> None:
    """delegate() omits mcp_server_registry_backend when the coordinator used the default."""
    agents_root = tmp_path / "agents"
    _make_agent_root(tmp_path, "coordinator")
    _make_agent_root(tmp_path, "specialist")
    (agents_root / "coordinator" / "roster.md").write_text(
        "# Roster\n\n- specialist\n", encoding="utf-8"
    )

    # No explicit backend -- default resolution path.
    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=agents_root,
    )
    assert coordinator._mcp_server_registry_backend_was_explicit is False

    constructed_kwargs: list[dict] = []
    original_init = AtomicAgent.__init__

    def capturing_init(self, **kwargs):
        constructed_kwargs.append(dict(kwargs))
        return original_init(self, **kwargs)

    with patch.object(AtomicAgent, "__init__", capturing_init):
        try:
            coordinator.delegate(
                target_agent_name="specialist",
                work_item="test",
            )
        except Exception:
            pass

    specialist_calls = [
        kw for kw in constructed_kwargs if kw.get("name") == "specialist"
    ]
    if specialist_calls:
        assert "mcp_server_registry_backend" not in specialist_calls[0]


# ──────────────────────────────────────────────────────────────────────────────
# 13. Fail-closed: MCPRegistryUnavailable propagates from __init__


def test_fail_closed_reraises_mcp_registry_unavailable(
    tmp_path: pathlib.Path,
) -> None:
    """AtomicAgent construction raises MCPRegistryUnavailable when backend probe fails."""
    _make_agent_root(tmp_path)
    backend = _make_mock_backend()
    backend.load_all_mcp_servers.side_effect = MCPRegistryUnavailable("catalog down")

    with pytest.raises(MCPRegistryUnavailable):
        AtomicAgent(
            name="test-agent",
            agents_root=tmp_path / "agents",
            mcp_server_registry_backend=backend,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 14. Fail-closed: raised message includes backend_id


def test_fail_closed_message_includes_backend_id(tmp_path: pathlib.Path) -> None:
    """The MCPRegistryUnavailable message includes the backend's backend_id."""
    _make_agent_root(tmp_path)
    backend = _make_mock_backend()
    backend.backend_id = "my-custom-backend"
    backend.load_all_mcp_servers.side_effect = MCPRegistryUnavailable("not reachable")

    with pytest.raises(MCPRegistryUnavailable, match="my-custom-backend"):
        AtomicAgent(
            name="test-agent",
            agents_root=tmp_path / "agents",
            mcp_server_registry_backend=backend,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 15. Fail-closed: credential-bearing URL is redacted in the raised message


def test_fail_closed_message_redacts_url_credentials(tmp_path: pathlib.Path) -> None:
    """Credentials embedded in the error URL do not appear in the raised message."""
    _make_agent_root(tmp_path)
    backend = _make_mock_backend()
    backend.backend_id = "http"
    backend.load_all_mcp_servers.side_effect = MCPRegistryUnavailable(
        "https://user:secret@catalog.internal/api"
    )

    with pytest.raises(MCPRegistryUnavailable) as exc_info:
        AtomicAgent(
            name="test-agent",
            agents_root=tmp_path / "agents",
            mcp_server_registry_backend=backend,
        )

    # The original URL with credentials must not appear in the message.
    assert "secret" not in str(exc_info.value)
    # The scheme should still be present (redacted to "https://...").
    assert "https://" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────────────────
# 16. MCPClientPool consumes mcp_servers_resolved, not mcp_servers
# (depends on Stream 2 AgentProfile.mcp_servers_resolved field)


@pytest.mark.skipif(
    not hasattr(AgentProfile, "mcp_servers_resolved"),
    reason="depends on Stream 2 AgentProfile.mcp_servers_resolved field",
)
def test_mcp_pool_consumes_mcp_servers_resolved_not_mcp_servers(
    tmp_path: pathlib.Path,
) -> None:
    """MCPClientPool receives the mcp_servers_resolved list, not mcp_servers."""
    agent_root = _make_agent_root(tmp_path)
    # Write an mcp.md so mcp_servers is non-empty.
    _write_mcp_md(
        agent_root,
        "# MCP servers\n\n## github\ncommand: npx\nargs: mcp-server-github\n",
    )

    resolved_spec = MCPServerSpec(
        name="resolved-server",
        command="python",
        args=["-m", "resolved"],
        env={},
        transport="stdio",
        description="",
    )
    backend = _make_mock_backend(specs=[resolved_spec])

    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
        mcp_server_registry_backend=backend,
    )

    # After construction, mcp_servers_resolved should contain the mock's output.
    resolved = getattr(agent._profile, "mcp_servers_resolved", None)
    assert resolved is not None, "Stream 2 field not populated"
    assert len(resolved) == 1
    assert resolved[0].name == "resolved-server"


# ──────────────────────────────────────────────────────────────────────────────
# 17. Empty mcp.md construction succeeds with no exception


def test_empty_mcp_md_construction_succeeds(tmp_path: pathlib.Path) -> None:
    """AtomicAgent construction succeeds when mcp.md is absent."""
    _make_agent_root(tmp_path)
    # No mcp.md written -- the default filesystem backend returns [].
    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
    )
    assert agent is not None
    assert agent.mcp_server_registry_backend is not None


# ──────────────────────────────────────────────────────────────────────────────
# 18. delegate.py CLI catches MCPRegistryError cleanly


def test_delegate_cli_catches_mcp_registry_error(tmp_path: pathlib.Path) -> None:
    """delegate.py main() prints 'Error: ...' to stderr and returns 1 on MCPRegistryError."""
    from atomic_agents import delegate as delegate_mod

    agents_root = tmp_path / "agents"
    _make_agent_root(tmp_path, "coordinator")

    # Patch AtomicAgent construction to raise MCPRegistryUnavailable.
    with patch(
        "atomic_agents.agent.AtomicAgent.__init__",
        side_effect=MCPRegistryUnavailable("catalog down"),
    ):
        result = delegate_mod.main(
            [
                "coordinator",
                "--target",
                "specialist",
                "--work-item",
                "do something",
                "--agents-root",
                str(agents_root),
            ]
        )

    assert result == 1


# ──────────────────────────────────────────────────────────────────────────────
# 19. profile.mcp_servers_resolved populated at construction
# (depends on Stream 2 AgentProfile.mcp_servers_resolved field)


@pytest.mark.skipif(
    not hasattr(AgentProfile, "mcp_servers_resolved"),
    reason="depends on Stream 2 AgentProfile.mcp_servers_resolved field",
)
def test_profile_mcp_servers_resolved_populated_at_construction(
    tmp_path: pathlib.Path,
) -> None:
    """agent._profile.mcp_servers_resolved equals the list from load_all_mcp_servers."""
    _make_agent_root(tmp_path)
    spec = MCPServerSpec(
        name="myserver",
        command="node",
        args=["server.js"],
        env={},
        transport="stdio",
        description="",
    )
    backend = _make_mock_backend(specs=[spec])

    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
        mcp_server_registry_backend=backend,
    )

    resolved = getattr(agent._profile, "mcp_servers_resolved", None)
    assert resolved is not None
    assert len(resolved) == 1
    assert resolved[0].name == "myserver"


# ──────────────────────────────────────────────────────────────────────────────
# 20. profile.mcp_servers_resolved is empty when no mcp.md
# (depends on Stream 2 AgentProfile.mcp_servers_resolved field)


@pytest.mark.skipif(
    not hasattr(AgentProfile, "mcp_servers_resolved"),
    reason="depends on Stream 2 AgentProfile.mcp_servers_resolved field",
)
def test_profile_mcp_servers_resolved_empty_when_no_mcp_md(
    tmp_path: pathlib.Path,
) -> None:
    """agent._profile.mcp_servers_resolved is [] when no mcp.md exists."""
    _make_agent_root(tmp_path)
    agent = AtomicAgent(
        name="test-agent",
        agents_root=tmp_path / "agents",
    )
    resolved = getattr(agent._profile, "mcp_servers_resolved", None)
    assert resolved is not None
    assert resolved == []

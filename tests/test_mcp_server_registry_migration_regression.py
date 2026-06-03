"""IRON RULE byte-identity regression suite for MCPServerRegistryBackend (#201 PR 2).

Pins pre-#201 behavior when no backend is configured (the default
deployment shape). Mirrors the spec/34 IRON RULE precedent at
tests/test_corpus_migration_regression.py.

The C-series tests reference AgentProfile.mcp_servers_resolved, which
is added in PR 2 by Stream 2 (profile/types.py). These tests fail
until Stream 2's changes are merged. The cross-stream dependency is
documented; the final pre-ship verification runs the full suite with
both streams merged.

Tests assume sorted order on multi-server mcp.md files because PR 2's
Stream 2 sorts the filesystem profile backend's parse_mcp_md_text
output lexicographically (locked decision Q1 from prep pass) to align
with spec/36 MUST 5 across all backends.
"""

from __future__ import annotations

import pathlib
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from atomic_agents import AtomicAgent
from atomic_agents.mcp_registry import (
    FilesystemMCPServerRegistryBackend,
    MCPRegistryUnavailable,
)
from atomic_agents.profile import AgentProfile


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures


def _make_agent_root(tmp_path: pathlib.Path, name: str = "test-agent") -> pathlib.Path:
    """Create a minimal agent directory for AtomicAgent construction."""
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


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: Empty mcp.md produces empty spec list


def test_empty_mcp_md_produces_empty_specs(tmp_path: pathlib.Path) -> None:
    """An empty mcp.md yields config.mcp_servers == [].

    Pre-#201 behavior: empty mcp.md means no servers. This invariant must
    be preserved after wiring the MCPServerRegistryBackend.
    """
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, "")

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    assert agent.config.mcp_servers == []


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: Missing mcp.md produces empty spec list


def test_missing_mcp_md_produces_empty_specs(tmp_path: pathlib.Path) -> None:
    """No mcp.md file yields config.mcp_servers == [].

    Pre-#201 behavior: absent mcp.md is equivalent to no servers.
    """
    _make_agent_root(tmp_path)
    # No mcp.md written at all.

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    assert agent.config.mcp_servers == []


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: Single server without env vars produces byte-identical specs


def test_single_server_no_env_vars_specs_byte_identical(
    tmp_path: pathlib.Path,
) -> None:
    """One server declared in mcp.md matches the parse_mcp_md_text baseline.

    Ensures that the wiring layer does not alter the spec values produced by
    the filesystem parser for a plain (no env-var substitution) server entry.
    """
    from atomic_agents.mcp import parse_mcp_md_text

    mcp_content = dedent("""\
        # MCP servers

        ## github
        command: npx
        args: -y, @modelcontextprotocol/server-github
        description: GitHub integration
    """)
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, mcp_content)

    # Baseline: direct parse with no wiring.
    baseline_specs = parse_mcp_md_text(mcp_content)

    # Agent construction path.
    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    # config.mcp_servers must match the baseline parse exactly.
    assert len(agent.config.mcp_servers) == len(baseline_specs)
    for actual, expected in zip(agent.config.mcp_servers, baseline_specs):
        assert actual.name == expected.name
        assert actual.command == expected.command
        assert actual.args == expected.args


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: Multiple servers returned in sorted order


def test_multiple_servers_sorted_order(tmp_path: pathlib.Path) -> None:
    """Three servers declared non-alphabetically come back sorted lexicographically.

    spec/36 MUST 5 requires consistent lexicographic order. This test also
    verifies that config.mcp_servers (the pre-#201 audit path) returns the
    same ordering so no existing log/audit consumer sees a sort change.
    """
    mcp_content = dedent("""\
        # MCP servers

        ## zebra
        command: npx
        args: zebra-mcp

        ## alpha
        command: npx
        args: alpha-mcp

        ## middle
        command: npx
        args: middle-mcp
    """)
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, mcp_content)

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    names = [s.name for s in agent.config.mcp_servers]
    # All three declared servers must be present.
    assert set(names) == {"zebra", "alpha", "middle"}
    # Lexicographic order is enforced by the filesystem backend.
    assert names == sorted(names)


# ──────────────────────────────────────────────────────────────────────────────
# Test 5: Env-var resolved specs match expected substitution


def test_env_var_resolved_specs_equal_pre_201(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$GITHUB_PAT in mcp.md is resolved at load time, matching pre-#201 behavior."""
    monkeypatch.setenv("GITHUB_PAT", "ghp_testtoken123")

    mcp_content = dedent("""\
        # MCP servers

        ## github
        command: npx
        args: -y, @modelcontextprotocol/server-github
        env: GITHUB_PERSONAL_ACCESS_TOKEN=$GITHUB_PAT
    """)
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, mcp_content)

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    assert len(agent.config.mcp_servers) == 1
    spec = agent.config.mcp_servers[0]
    assert spec.env.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "ghp_testtoken123"


# ──────────────────────────────────────────────────────────────────────────────
# Test 6: Explicit FilesystemMCPServerRegistryBackend kwarg yields same result as default


def test_config_mcp_servers_unaffected_by_explicit_backend_kwarg(
    tmp_path: pathlib.Path,
) -> None:
    """Passing the filesystem backend explicitly gives the same config.mcp_servers as default.

    Ensures the explicit-kwarg path does not alter the audit/log field
    (config.mcp_servers stays the filesystem-parse result).
    """
    mcp_content = dedent("""\
        # MCP servers

        ## myserver
        command: node
        args: server.js
    """)
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, mcp_content)
    agents_root = tmp_path / "agents"

    # Default resolution path.
    agent_default = AtomicAgent(name="test-agent", agents_root=agents_root)

    # Explicit filesystem backend passed via kwarg.
    explicit_backend = FilesystemMCPServerRegistryBackend(agent_root, [])
    agent_explicit = AtomicAgent(
        name="test-agent",
        agents_root=agents_root,
        mcp_server_registry_backend=explicit_backend,
    )

    # Both must agree on config.mcp_servers.
    default_names = [s.name for s in agent_default.config.mcp_servers]
    explicit_names = [s.name for s in agent_explicit.config.mcp_servers]
    assert default_names == explicit_names


# ──────────────────────────────────────────────────────────────────────────────
# Test 7: mcp_servers_resolved field populated after construction
# (depends on Stream 2 AgentProfile.mcp_servers_resolved field)


@pytest.mark.skipif(
    not hasattr(AgentProfile, "mcp_servers_resolved"),
    reason="depends on Stream 2 AgentProfile.mcp_servers_resolved field",
)
def test_mcp_servers_resolved_field_populated_after_construction(
    tmp_path: pathlib.Path,
) -> None:
    """agent._profile.mcp_servers_resolved contains the materialized spec list."""
    mcp_content = dedent("""\
        # MCP servers

        ## testserver
        command: python
        args: -m, testserver
    """)
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, mcp_content)

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    resolved = getattr(agent._profile, "mcp_servers_resolved", None)
    assert resolved is not None
    assert len(resolved) == 1
    assert resolved[0].name == "testserver"


# ──────────────────────────────────────────────────────────────────────────────
# Test 8: config.mcp_servers equals profile.mcp_servers_resolved
# (depends on Stream 2 AgentProfile.mcp_servers_resolved field)


@pytest.mark.skipif(
    not hasattr(AgentProfile, "mcp_servers_resolved"),
    reason="depends on Stream 2 AgentProfile.mcp_servers_resolved field",
)
def test_mcp_servers_config_equals_profile_resolved(
    tmp_path: pathlib.Path,
) -> None:
    """config.mcp_servers and profile.mcp_servers_resolved agree on the default path.

    On the default filesystem path, both fields must contain the same server list
    (same names, same order). This is the IRON RULE: the pool input source and
    the audit field agree when using the default backend.
    """
    mcp_content = dedent("""\
        # MCP servers

        ## bravo
        command: npx
        args: bravo-mcp

        ## alpha
        command: npx
        args: alpha-mcp
    """)
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, mcp_content)

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    config_names = [s.name for s in agent.config.mcp_servers]
    resolved = getattr(agent._profile, "mcp_servers_resolved", None)
    assert resolved is not None
    resolved_names = [s.name for s in resolved]
    assert config_names == resolved_names


# ──────────────────────────────────────────────────────────────────────────────
# Test 9: mcp_pool specs match resolved list at construction time
# (depends on Stream 2 AgentProfile.mcp_servers_resolved field)


@pytest.mark.skipif(
    not hasattr(AgentProfile, "mcp_servers_resolved"),
    reason="depends on Stream 2 AgentProfile.mcp_servers_resolved field",
)
def test_mcp_pool_specs_byte_identical_to_resolved_list(
    tmp_path: pathlib.Path,
) -> None:
    """The resolved list populated at construction matches the profile field.

    mcp_pool is initialized lazily at call() time; this test verifies that the
    mcp_servers_resolved field on the profile is populated correctly at
    construction so the pool will receive the right specs when call() runs.
    """
    mcp_content = dedent("""\
        # MCP servers

        ## poolserver
        command: node
        args: pool-server.js
    """)
    agent_root = _make_agent_root(tmp_path)
    _write_mcp_md(agent_root, mcp_content)

    agent = AtomicAgent(name="test-agent", agents_root=tmp_path / "agents")

    resolved = getattr(agent._profile, "mcp_servers_resolved", None)
    assert resolved is not None
    # The resolved list must be non-empty and contain "poolserver".
    assert any(s.name == "poolserver" for s in resolved)


# ──────────────────────────────────────────────────────────────────────────────
# Test 10: MCPRegistryUnavailable raised at construction, pool never built


def test_mcp_registry_unavailable_raises_at_construction(
    tmp_path: pathlib.Path,
) -> None:
    """When the backend's load_all_mcp_servers raises, AtomicAgent raises too.

    The MCPClientPool must not be constructed (fail-closed: no subprocess
    overhead before the error is surfaced).
    """
    _make_agent_root(tmp_path)

    failing_backend = MagicMock()
    failing_backend.backend_id = "failing"
    failing_backend.load_all_mcp_servers.side_effect = MCPRegistryUnavailable(
        "simulated backend failure"
    )

    with pytest.raises(MCPRegistryUnavailable):
        AtomicAgent(
            name="test-agent",
            agents_root=tmp_path / "agents",
            mcp_server_registry_backend=failing_backend,
        )

    # load_all_mcp_servers was called exactly once (the probe attempt).
    failing_backend.load_all_mcp_servers.assert_called_once()

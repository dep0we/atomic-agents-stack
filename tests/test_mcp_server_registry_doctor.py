"""Tests for ``doctor.check_mcp_server_registry_backend`` (#201 PR 2).

Cross-model review army surfaced this coverage hole: Testing specialist C4
plus Security specialist plus Codex adversarial all independently flagged
that the new doctor check has no unit tests. Every parallel backend doctor
check (lock, log, profile, tool registry, persona, corpus) ships with 4-5
dedicated tests. PR 1's P0 secret leak in ``mcp-registry show`` was caught
in part because the other backends had redaction tests. This file mirrors
that pattern so future regressions in the credential-redaction path or in
the descriptor-invalid probe (added per cross-model finding F6) are caught.

Covers:
- PASS path: filesystem default with no mcp.md.
- PASS path: filesystem default with valid mcp.md.
- FAIL path: unknown backend_id where the env var value is a pasted URL with
  credentials. The redaction MUST strip them.
- FAIL path: descriptor invalid (malformed mcp.md). This is the new probe
  added in PR 2 to close the doctor-false-PASS gap.
- Capability snapshot completeness: detail dict includes every capability
  field plus mcp_server_count.
"""

from __future__ import annotations

import pathlib
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from atomic_agents.doctor import (
    FAIL,
    PASS,
    check_mcp_server_registry_backend,
)
from atomic_agents.mcp_registry import (
    MCPRegistryDescriptorInvalid,
    MCPServerRegistryBackend,
)


def _make_agent_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal agent dir for the doctor check."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True)
    return agent_root


def test_doctor_pass_filesystem_default_no_mcp_md(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PASS when env var unset and no mcp.md exists.

    Zero-server deployments are normal (the common home-user shape) and
    must report PASS, not WARN.
    """
    monkeypatch.delenv("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND", raising=False)
    agent_root = _make_agent_root(tmp_path)
    result = check_mcp_server_registry_backend(agent_root)
    assert result.status == PASS
    assert "filesystem" in result.message
    assert result.detail["backend_id"] == "filesystem"
    assert result.detail["mcp_server_count"] == 0


def test_doctor_pass_filesystem_default_with_valid_mcp_md(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PASS when valid mcp.md is present.

    list_mcp_servers + load_all_mcp_servers both succeed; the count
    reflects what is mounted.
    """
    monkeypatch.delenv("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND", raising=False)
    agent_root = _make_agent_root(tmp_path)
    (agent_root / "mcp.md").write_text(
        dedent(
            """\
            ## myserver
            command: echo

            args:
              - hello
            """
        ),
        encoding="utf-8",
    )
    result = check_mcp_server_registry_backend(agent_root)
    assert result.status == PASS
    assert result.detail["mcp_server_count"] == 1


def test_doctor_fail_unknown_backend_id_redacts_url_credentials(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL with URL credentials stripped when operator pastes a URL.

    PR 1's P0 class of bug: operator accidentally sets the BACKEND env var
    to a full URL instead of the _URL variant. The FAIL message MUST NOT
    echo the credential. This regression test pins ``_redact_for_error_message``
    usage (the helper handles ``://`` scheme heuristic AND DSN-style
    ``user:pass@host`` patterns AND length truncation; the inline
    truncation in ``check_tool_registry_backend`` misses DSN-style values).
    """
    monkeypatch.setenv(
        "ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND",
        "https://admin:supersecret@catalog.example.com/mcp",
    )
    agent_root = _make_agent_root(tmp_path)
    result = check_mcp_server_registry_backend(agent_root)
    assert result.status == FAIL
    # Credentials MUST NOT appear anywhere.
    rendered = result.message + " " + result.fix_hint + " " + repr(result.detail)
    assert "supersecret" not in rendered
    assert "admin" not in rendered
    # The redacted form is present.
    assert "https://..." in rendered


def test_doctor_fail_descriptor_invalid_predicts_construction_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL on malformed mcp.md so doctor predicts agent construction failure.

    Pre-fix: doctor probed only ``list_mcp_servers()`` which swallowed parse
    errors and returned ``[]``. The agent at construction calls
    ``load_all_mcp_servers()`` which raises ``MCPRegistryDescriptorInvalid``.
    Operator runs doctor, sees PASS, constructs agent, agent crashes.

    Cross-model review army (Codex adversarial Medium + Claude adversarial
    F6 + Testing specialist) all flagged this as the highest-priority real
    correctness bug in the PR. The fix adds a ``load_all_mcp_servers()``
    probe after ``list_mcp_servers()`` succeeds.
    """
    monkeypatch.delenv("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND", raising=False)
    agent_root = _make_agent_root(tmp_path)
    # Inject a backend whose list_mcp_servers succeeds (returns []) but
    # load_all_mcp_servers raises MCPRegistryDescriptorInvalid. This is
    # the exact divergence cross-model review caught: list swallows parse
    # errors, load_all surfaces them.
    fake_backend = MagicMock(spec=MCPServerRegistryBackend)
    fake_backend.backend_id = "filesystem"
    fake_backend.list_mcp_servers.return_value = []
    fake_backend.load_all_mcp_servers.side_effect = MCPRegistryDescriptorInvalid(
        "mcp.md at /tmp/agent/mcp.md could not be parsed: malformed YAML"
    )
    # Patch the factory the doctor calls so the fake backend is used.
    import atomic_agents.mcp_registry as mcp_registry_pkg

    monkeypatch.setattr(
        mcp_registry_pkg,
        "get_default_mcp_server_registry_backend",
        lambda agent_root, read_paths: fake_backend,
    )
    result = check_mcp_server_registry_backend(agent_root)
    assert result.status == FAIL
    assert "malformed descriptor" in result.message
    assert "construction" in result.fix_hint
    # backend_id appears so operator knows which backend is broken.
    assert "filesystem" in result.message


def test_doctor_pass_capability_snapshot_includes_all_fields(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PASS detail dict carries every MCPServerRegistryCapabilities field.

    Operator-facing JSON output for ``atomic-agents doctor --json`` reads
    this dict. A future capability field added to
    ``MCPServerRegistryCapabilities`` without an update here would be
    silently absent from the snapshot, which is a documentation drift
    pattern.
    """
    monkeypatch.delenv("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND", raising=False)
    agent_root = _make_agent_root(tmp_path)
    result = check_mcp_server_registry_backend(agent_root)
    assert result.status == PASS
    expected_keys = {
        "backend_id",
        "supports_install",
        "supports_uninstall",
        "supports_capability_handshake",
        "supports_audit",
        "durable",
        "mcp_server_count",
    }
    assert expected_keys.issubset(result.detail.keys())
    # Capability values come from FilesystemMCPServerRegistryBackend
    # capabilities at PR 2 baseline: install/uninstall False (filesystem
    # writes ship at PR 3), capability_handshake False (HTTP only),
    # audit False, durable True.
    assert result.detail["supports_install"] is False
    assert result.detail["supports_uninstall"] is False
    assert result.detail["supports_capability_handshake"] is False
    assert result.detail["supports_audit"] is False
    assert result.detail["durable"] is True

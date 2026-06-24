"""Conformance tests for AgentRegistryBackend Protocol (spec/51).

These tests verify the behavioral contract every AgentRegistryBackend implementation
must satisfy. They are parametrized on backend implementations (currently
FilesystemAgentRegistryBackend only; future backends extend the parametrize list).

Conformance invariants tested. MUST numbers below cite the spec/51 Implementer
Contract (the single authoritative 10-MUST list — §"Implementer Contract"):
- Contract MUST 2: list_agents() returns [] when agents_root absent / empty / not a dir.
- Contract MUST 4: discovery predicate is model.md present+readable (spec/37:314), not log/.
- Contract MUST 3: _- and .-prefixed dirs are excluded from list_agents().
- Contract MUST 5: malformed governance.md is fail-soft (agent still returned,
  has_governance flags the problem); list_agents() never crashes on a single
  corrupt agent; an out-of-root symlinked agent dir is skipped (same MUST 5
  fail-soft contract).
- Contract MUST 10: register_agent() / unregister_agent() raise
  RegistrationNotSupported on read-only backends.
- Contract MUST 7: get_agent() returns None on miss (TOCTOU-safe).
- Contract MUST 8: get_agent() raises PathTraversalError on '.', '..', separator, empty.
- Contract MUST 9: capabilities is a @property advertising backend_id, etc.
- Env-override behavior (spec/51 §"Registry and env override", NOT a numbered
  Contract MUST): unknown env value → BackendNotRegistered (fail-loud).
- AgentRef field contract (spec/51 Types): list_agents() returns AgentRef with
  .id == folder name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.agent_registry import (
    AgentRegistryBackend,
    AgentRef,
    FilesystemAgentRegistryBackend,
    RegistrationNotSupported,
)
from atomic_agents.exceptions import BackendNotRegistered


# ──────────────────────────────────────────────────────────────────
# Backend parametrize fixture

BACKEND_IDS = ["filesystem"]


def make_backend(backend_id: str, agents_root: Path) -> AgentRegistryBackend:
    if backend_id == "filesystem":
        return FilesystemAgentRegistryBackend(agents_root)
    raise ValueError(f"Unknown backend {backend_id!r}")


@pytest.fixture(params=BACKEND_IDS)
def backend(request, tmp_path):
    return make_backend(request.param, tmp_path)


@pytest.fixture(params=BACKEND_IDS)
def backend_with_agents(request, tmp_path):
    """Backend with two framework agents (model.md) and one non-agent (log/ only)."""
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "model.md").write_text("# model\n")
    (tmp_path / "bob").mkdir()
    (tmp_path / "bob" / "model.md").write_text("# model\n")
    # Non-agent: log/ present but no model.md
    (tmp_path / "data-dir" / "log").mkdir(parents=True)
    return make_backend(request.param, tmp_path), tmp_path


# ──────────────────────────────────────────────────────────────────
# MUST 2: empty / absent root


def test_list_agents_absent_root(tmp_path):
    """MUST 2: list_agents returns [] when agents_root does not exist."""
    backend = FilesystemAgentRegistryBackend(tmp_path / "nonexistent")
    assert backend.list_agents() == []


def test_list_agents_empty_root(backend):
    """MUST 2: list_agents returns [] when agents_root is empty."""
    assert backend.list_agents() == []


def test_list_agents_no_qualifying_dirs(tmp_path):
    """MUST 2: list_agents returns [] when no dir has model.md."""
    (tmp_path / "data" / "log").mkdir(parents=True)  # no model.md
    (tmp_path / "scratch").mkdir()
    backend = FilesystemAgentRegistryBackend(tmp_path)
    assert backend.list_agents() == []


# ──────────────────────────────────────────────────────────────────
# MUST 4: discovery predicate is model.md (spec/37:314)


def test_list_agents_requires_model_md(backend_with_agents):
    """MUST 4: only dirs with model.md are agents; log/ alone is not enough."""
    b, agents_root = backend_with_agents
    result = b.list_agents()
    ids = [r.id for r in result]
    assert "alice" in ids
    assert "bob" in ids
    assert "data-dir" not in ids  # log/ only — not an agent


def test_list_agents_model_md_no_log(tmp_path):
    """MUST 4: a just-deployed agent (model.md, no log/) IS discovered.

    This is the primary regression guard for the spec/51 predicate change —
    the old log/-presence predicate excluded newly-deployed agents.
    """
    (tmp_path / "new-agent").mkdir()
    (tmp_path / "new-agent" / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    assert result[0].id == "new-agent"


def test_list_agents_unreadable_model_md_excluded(tmp_path):
    """MUST 4: agent dir with model.md that raises IOError on read is excluded."""
    import os

    if os.getuid() == 0:
        pytest.skip("chmod 0o000 does not restrict root; cannot test unreadable path")
    agent_dir = tmp_path / "broken"
    agent_dir.mkdir()
    model_md = agent_dir / "model.md"
    model_md.write_text("# model\n")
    # Make it unreadable so parse_model_md raises IOError.
    model_md.chmod(0o000)
    try:
        backend = FilesystemAgentRegistryBackend(tmp_path)
        result = backend.list_agents()
        # The agent with unreadable model.md must be excluded (fail-soft).
        ids = [r.id for r in result]
        assert "broken" not in ids
    finally:
        model_md.chmod(0o644)


# ──────────────────────────────────────────────────────────────────
# MUST 3: _- and .-prefixed dirs excluded


def test_list_agents_excludes_prefixed_dirs(tmp_path):
    """MUST 3: _ and . prefixed dirs are excluded even with model.md."""
    (tmp_path / "real-agent").mkdir()
    (tmp_path / "real-agent" / "model.md").write_text("# model\n")
    (tmp_path / "_dashboard").mkdir()
    (tmp_path / "_dashboard" / "model.md").write_text("# model\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    ids = [r.id for r in result]
    assert "real-agent" in ids
    assert "_dashboard" not in ids
    assert ".hidden" not in ids


# ──────────────────────────────────────────────────────────────────
# MUST 5: governance.md fail-soft


def test_list_agents_absent_governance_md(tmp_path):
    """MUST 5: absent governance.md → has_governance=False, governance=None."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    assert result[0].has_governance is False
    assert result[0].governance is None


def test_list_agents_valid_governance_md(tmp_path):
    """MUST 5: valid governance.md → has_governance=True, GovernanceRecord populated."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "# Governance\n"
        "```yaml\n"
        "governance:\n"
        "  owner: alice@example.com\n"
        "  permission_tier: read-only\n"
        "  lifecycle_status: active\n"
        "  customer_data: 'no'\n"
        "  writes_sor: 'no'\n"
        "```\n"
        "## Forbidden actions\n\n## Failure modes\n\n## Pause / retire criteria\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    assert ref.governance.owner == "alice@example.com"
    assert ref.governance.permission_tier == "read-only"
    assert ref.governance.lifecycle_status == "active"


def test_list_agents_invalid_governance_md_unknown_enum_has_governance_true(tmp_path):
    """MUST 5: unknown permission_tier → has_governance=True + parse_errors, not crash."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "# Governance\n"
        "```yaml\n"
        "governance:\n"
        "  permission_tier: superpower\n"  # invalid enum value
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    # Must not raise — fail-soft per MUST 5.
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    assert len(ref.governance.parse_errors) > 0
    # The bad field name must appear in the error.
    assert any("permission_tier" in e for e in ref.governance.parse_errors)


def test_list_agents_governance_md_invalid_yaml(tmp_path):
    """MUST 5: unparseable YAML → has_governance=True + parse_errors, not crash."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "# Governance\n"
        "```yaml\n"
        "governance: {bad: yaml: [\n"  # malformed YAML
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.has_governance is True


def test_list_agents_corrupt_governance_does_not_abort_loop(tmp_path):
    """MUST 5: one corrupt agent must not abort list_agents() for the rest of the fleet.

    This is the P0 risk: a single agent with corrupt governance.md must not
    crash list_agents() and make ALL agents invisible to the dashboard.
    """
    # Agent A: valid model.md, corrupt governance.md
    (tmp_path / "agent-a").mkdir()
    (tmp_path / "agent-a" / "model.md").write_text("# model\n")
    (tmp_path / "agent-a" / "governance.md").write_text(
        "```yaml\ngovernance:\n  permission_tier: bad-invalid-tier\n```\n"
    )
    # Agent B: valid model.md, no governance.md
    (tmp_path / "agent-b").mkdir()
    (tmp_path / "agent-b" / "model.md").write_text("# model\n")

    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    ids = [r.id for r in result]
    # BOTH agents must appear in the result.
    assert "agent-a" in ids
    assert "agent-b" in ids


# ──────────────────────────────────────────────────────────────────
# MUST 10: register_agent / unregister_agent raise RegistrationNotSupported


def test_register_agent_raises_not_supported(backend):
    """MUST 10: register_agent raises RegistrationNotSupported on read-only backends."""
    ref = AgentRef(
        id="test", location="/tmp/test", discovered_at="2026-06-24T00:00:00+00:00"
    )
    with pytest.raises(RegistrationNotSupported):
        backend.register_agent(ref)


def test_unregister_agent_raises_not_supported(backend):
    """MUST 10: unregister_agent raises RegistrationNotSupported on read-only backends."""
    with pytest.raises(RegistrationNotSupported):
        backend.unregister_agent("test-agent")


def test_registration_not_supported_is_typed(backend):
    """MUST 10: RegistrationNotSupported is instanceof AgentRegistryError."""
    from atomic_agents.exceptions import AgentRegistryError

    ref = AgentRef(id="x", location="/tmp/x", discovered_at="2026-06-24T00:00:00+00:00")
    with pytest.raises(AgentRegistryError):
        backend.register_agent(ref)


# ──────────────────────────────────────────────────────────────────
# env-override (spec/51 §Registry and env override): env var unknown → BackendNotRegistered (fail-loud)


def test_get_default_fails_loud_on_unknown_backend(tmp_path, monkeypatch):
    """env-override (spec/51 §Registry and env override): ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND=bogus fails loud."""
    monkeypatch.setenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", "bogus")
    from atomic_agents.agent_registry import get_default_agent_registry_backend

    with pytest.raises(BackendNotRegistered):
        get_default_agent_registry_backend(tmp_path)


def test_get_default_uses_filesystem_on_empty_env(tmp_path, monkeypatch):
    """env-override (spec/51 §Registry and env override): empty env var → uses filesystem default (no error)."""
    monkeypatch.setenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", "")
    from atomic_agents.agent_registry import get_default_agent_registry_backend

    b = get_default_agent_registry_backend(tmp_path)
    assert b.backend_id == "filesystem"


def test_get_default_uses_filesystem_on_absent_env(tmp_path, monkeypatch):
    """env-override (spec/51 §Registry and env override): absent env var → uses filesystem default (no error)."""
    monkeypatch.delenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", raising=False)
    from atomic_agents.agent_registry import get_default_agent_registry_backend

    b = get_default_agent_registry_backend(tmp_path)
    assert b.backend_id == "filesystem"


def test_get_default_credential_echo_redaction(tmp_path, monkeypatch):
    """env-override (spec/51 §Registry and env override) / feedback_doctor_check_redacts_env_value_not_exception_string:
    DSN-shaped env var must not appear unredacted in the BackendNotRegistered message.
    """
    monkeypatch.setenv(
        "ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND",
        "postgres://user:secret-pass@host/db",
    )
    from atomic_agents.agent_registry import get_default_agent_registry_backend

    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_agent_registry_backend(tmp_path)
    msg = str(exc_info.value)
    # The raw value 'secret-pass' must NOT appear unredacted.
    assert "secret-pass" not in msg


# ──────────────────────────────────────────────────────────────────
# MUST 7: get_agent returns None on miss (TOCTOU-safe)


def test_get_agent_returns_none_on_miss(tmp_path):
    """MUST 7: get_agent returns None when agent does not exist."""
    backend = FilesystemAgentRegistryBackend(tmp_path)
    assert backend.get_agent("nonexistent") is None


def test_get_agent_toctou_safe(tmp_path):
    """MUST 7: get_agent returns None when model.md vanishes after list_agents()."""
    (tmp_path / "agent").mkdir()
    model_md = tmp_path / "agent" / "model.md"
    model_md.write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    # Verify it's found by list_agents first.
    result = backend.list_agents()
    assert any(r.id == "agent" for r in result)
    # Now delete model.md (simulating TOCTOU race).
    model_md.unlink()
    # get_agent must return None, not raise.
    assert backend.get_agent("agent") is None


def test_get_agent_returns_entry_for_existing_agent(tmp_path):
    """MUST 7: get_agent returns AgentRef for a present agent."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    entry = backend.get_agent("agent")
    assert entry is not None
    assert entry.id == "agent"
    assert entry.location.endswith("agent")


def test_get_agent_raises_on_traversal(tmp_path):
    """MUST 8: get_agent raises PathTraversalError on '.', '..', separator, or empty."""
    from atomic_agents.exceptions import PathTraversalError

    backend = FilesystemAgentRegistryBackend(tmp_path)
    with pytest.raises(PathTraversalError):
        backend.get_agent("../escape")
    with pytest.raises(PathTraversalError):
        backend.get_agent("..")
    with pytest.raises(PathTraversalError):
        backend.get_agent("")


# ──────────────────────────────────────────────────────────────────
# MUST 9: capabilities


def test_capabilities_advertises_backend_id(backend):
    """MUST 9: capabilities.backend_id is a non-empty string."""
    caps = backend.capabilities
    assert caps.backend_id
    assert isinstance(caps.backend_id, str)


def test_capabilities_is_property_not_method(backend):
    """MUST 9: capabilities is a @property (no parentheses) — mirrors mcp_registry."""
    # Access as property — if it were a method, accessing without () returns the method.
    caps = backend.capabilities
    # Verify it is an AgentRegistryCapabilities instance, not a method.
    from atomic_agents.agent_registry import AgentRegistryCapabilities

    assert isinstance(caps, AgentRegistryCapabilities)


def test_filesystem_capabilities_no_registration(tmp_path):
    """MUST 9: FilesystemAgentRegistryBackend declares supports_registration=False."""
    backend = FilesystemAgentRegistryBackend(tmp_path)
    caps = backend.capabilities
    assert caps.supports_registration is False
    assert caps.supports_canonical_export is False
    assert caps.single_host_only is True


# ──────────────────────────────────────────────────────────────────
# MUST 5 (fail-soft): symlink containment


def test_list_agents_excludes_symlink_outside_root(tmp_path):
    """MUST 5 (fail-soft): symlinked agent dir resolving outside agents_root is
    skipped, while a same-root symlink to an in-root agent dir IS still
    discovered — so the exclusion is by CONTAINMENT, not a wholesale
    "skip every symlink" rule.

    The positive sibling assertion is the discriminating control: if the
    containment skip were implemented as "skip any symlinked dir", the in-root
    symlink would also be dropped and this test would catch the over-broad rule.
    """
    target_outside = tmp_path / "outside"
    target_outside.mkdir()
    (target_outside / "model.md").write_text("# model\n")

    agents_root = tmp_path / "agents"
    agents_root.mkdir()

    # A real in-root agent that an in-root symlink will point at.
    real_agent = agents_root / "realone"
    real_agent.mkdir()
    (real_agent / "model.md").write_text("# model\n")

    # Create a symlink inside agents_root pointing to the external dir (ESCAPE).
    symlink = agents_root / "escaped"
    symlink.symlink_to(target_outside)

    # A same-root symlink pointing to an in-root agent dir (CONTAINED — allowed).
    in_root_symlink = agents_root / "alias"
    in_root_symlink.symlink_to(real_agent)

    backend = FilesystemAgentRegistryBackend(agents_root)
    result = backend.list_agents()
    ids = [r.id for r in result]
    # The symlinked dir escapes agents_root — must NOT appear in results.
    assert "escaped" not in ids
    # The real in-root agent IS discovered.
    assert "realone" in ids
    # The same-root symlink (resolves to an in-root agent) is NOT skipped
    # wholesale — containment, not "all symlinks dropped".
    assert "alias" in ids


# ──────────────────────────────────────────────────────────────────
# AgentRef-field-contract: AgentRef.id == folder name


def test_list_agents_agent_ref_id_equals_folder_name(backend_with_agents):
    """AgentRef-field-contract: AgentRef.id equals the folder name (for dashboard string compat)."""
    b, agents_root = backend_with_agents
    result = b.list_agents()
    for ref in result:
        expected_dir = agents_root / ref.id
        assert expected_dir.is_dir(), (
            f"AgentRef.id={ref.id!r} has no corresponding folder"
        )


def test_list_agents_sorted_lexicographically(tmp_path):
    """list_agents() returns results sorted lexicographically by id."""
    for name in ("zebra", "alpha", "middle"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    ids = [r.id for r in result]
    assert ids == sorted(ids)


# ──────────────────────────────────────────────────────────────────
# discovered_at is a valid ISO-8601 string (not compared across calls)


def test_list_agents_discovered_at_is_valid_iso8601(tmp_path):
    """discovered_at is a valid ISO-8601 string (call-time timestamp)."""
    from datetime import datetime

    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    # Must be parseable as ISO-8601 datetime.
    dt = datetime.fromisoformat(result[0].discovered_at)
    assert dt.tzinfo is not None  # UTC-aware


# ──────────────────────────────────────────────────────────────────
# AgentRegistryBackend is @runtime_checkable


def test_filesystem_backend_satisfies_protocol(tmp_path):
    """FilesystemAgentRegistryBackend satisfies the AgentRegistryBackend Protocol."""
    backend = FilesystemAgentRegistryBackend(tmp_path)
    assert isinstance(backend, AgentRegistryBackend)


# ──────────────────────────────────────────────────────────────────
# Registry registration functions


def test_register_and_get_backend():
    from atomic_agents.agent_registry import (
        get_agent_registry_backend,
        list_agent_registry_backends,
        register_agent_registry_backend,
        unregister_agent_registry_backend,
    )

    class FakeBackend:
        backend_id = "fake-test-registry"

    try:
        register_agent_registry_backend("fake-test-registry", FakeBackend)
        assert "fake-test-registry" in list_agent_registry_backends()
        cls = get_agent_registry_backend("fake-test-registry")
        assert cls is FakeBackend
    finally:
        unregister_agent_registry_backend("fake-test-registry")
    assert "fake-test-registry" not in list_agent_registry_backends()


def test_get_backend_raises_on_unknown():
    from atomic_agents.agent_registry import get_agent_registry_backend

    with pytest.raises(BackendNotRegistered):
        get_agent_registry_backend("definitely-not-registered-xyz")


# ──────────────────────────────────────────────────────────────────
# GovernanceRecord round-trip


def test_governance_record_round_trip():
    """GovernanceRecord.from_dict(record.to_dict()) == record for a fully-populated record."""
    from atomic_agents.agent_registry import (
        ActionsRecord,
        GovernanceRecord,
        ReviewRecord,
        RiskRecord,
        SourcesRecord,
    )

    record = GovernanceRecord(
        owner="alice@example.com",
        backup_owner="bob@example.com",
        permission_tier="writes",
        customer_data="yes",
        writes_sor="partial",
        lifecycle_status="active",
        created_at="2026-06-24",
        updated_at="2026-06-24",
        review=ReviewRecord(
            reviewer="carol", reviewed_at="2026-06-24", approved_by="dave"
        ),
        risk=RiskRecord(level="medium", notes="some risk"),
        sources=SourcesRecord(primary=["crm"], secondary=["analytics"]),
        actions=ActionsRecord(permitted=["read", "draft"], forbidden=["delete"]),
    )
    d = record.to_dict()
    restored = GovernanceRecord.from_dict(d)
    assert restored == record


def test_governance_record_forward_compat():
    """from_dict ignores unknown keys (forward compat)."""
    from atomic_agents.agent_registry import GovernanceRecord

    d = {
        "owner": "alice",
        "unknown_future_field": "some_value",
        "permission_tier": "read-only",
        "lifecycle_status": "active",
    }
    # Must not raise on unknown keys.
    record = GovernanceRecord.from_dict(d)
    assert record.owner == "alice"
    assert record.permission_tier == "read-only"


def test_governance_record_invalid_enum_raises():
    """Unknown enum value in from_dict raises GovernanceParseError."""
    from atomic_agents.agent_registry import GovernanceRecord
    from atomic_agents.exceptions import GovernanceParseError

    with pytest.raises(GovernanceParseError) as exc_info:
        GovernanceRecord.from_dict({"permission_tier": "superpower"})
    assert "permission_tier" in str(exc_info.value)
    assert "superpower" in str(exc_info.value)


def test_governance_record_coerces_yaml_boolean_tristate():
    """Unquoted tristate values (yes/no) survive PyYAML's bool coercion (#607).

    PyYAML (YAML 1.1) parses the bare words `yes`/`no` as Python bools, so an
    operator who fills in the DOCUMENTED template value `customer_data: no`
    yields the bool False, not the string "no". from_dict() MUST coerce the
    bool back to its canonical tristate spelling rather than raising
    GovernanceParseError — otherwise the documented happy-path value silently
    produces a PRESENT_INVALID record that discards every other field.

    Strip-RED negative control: remove the `isinstance(value, bool)` coercion
    in types.py::_validate_enum and this test fails with GovernanceParseError
    ("invalid value False").
    """
    from atomic_agents.agent_registry import GovernanceRecord

    # The bools are exactly what yaml.safe_load("customer_data: no") produces.
    record = GovernanceRecord.from_dict(
        {
            "owner": "security@example.com",
            "permission_tier": "sends-or-acts",
            "customer_data": False,  # PyYAML('no')
            "writes_sor": True,  # PyYAML('yes')
            "lifecycle_status": "active",
        }
    )
    assert record.parse_errors == ()
    assert record.customer_data == "no"
    assert record.writes_sor == "yes"
    # The valid sibling fields MUST be preserved (not gutted by a spurious
    # PRESENT_INVALID drop-everything path).
    assert record.permission_tier == "sends-or-acts"
    assert record.owner == "security@example.com"

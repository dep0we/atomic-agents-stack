"""Filesystem-specific tests for FilesystemAgentRegistryBackend (spec/51).

Tests that exercise implementation details specific to the filesystem backend,
beyond the Protocol conformance suite in test_agent_registry_conformance.py.

Covers:
- agents_root is a file (not directory) → returns []
- agents_root with PermissionError on iterdir → returns []
- governance.md absent-but-dir-exists → has_governance=False
- governance.md YAML block with correct governance: key
- governance.md has yaml block but no governance: key → has_governance=True, governance=None
- governance.md present but PermissionError → has_governance=False
- discover_agents integration with FilesystemAgentRegistryBackend
- doctor check_agent_registry_backend wiring
- _redact_for_error_message helper
"""

from __future__ import annotations

import pytest

from atomic_agents.agent_registry import (
    FilesystemAgentRegistryBackend,
    _redact_for_error_message,
)


# ──────────────────────────────────────────────────────────────────
# agents_root edge cases


def test_agents_root_is_file_returns_empty(tmp_path):
    """agents_root is a file (not directory) → list_agents returns []."""
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("content")
    backend = FilesystemAgentRegistryBackend(file_path)
    # Must not raise, must return [].
    result = backend.list_agents()
    assert result == []


def test_agents_root_absent_for_get_agent(tmp_path):
    """get_agent on absent agents_root returns None, not raise."""
    backend = FilesystemAgentRegistryBackend(tmp_path / "nonexistent")
    assert backend.get_agent("any-agent") is None


# ──────────────────────────────────────────────────────────────────
# governance.md parsing edge cases


def test_governance_md_no_yaml_block(tmp_path):
    """governance.md with no yaml block → has_governance=True, governance=None."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "# Governance\n\n## Forbidden actions\n\nNo yaml block here.\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    assert result[0].has_governance is True
    assert result[0].governance is None  # yaml block absent, but file IS present


def test_governance_md_yaml_without_governance_key(tmp_path):
    """governance.md yaml block without 'governance:' key → has_governance=True, governance=None."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "# Governance\n```yaml\nother_key:\n  something: value\n```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    assert result[0].has_governance is True
    assert result[0].governance is None


def test_governance_md_unreadable_has_governance_false(tmp_path):
    """governance.md present but PermissionError → has_governance=False."""
    import os

    if os.getuid() == 0:
        pytest.skip("chmod 0o000 does not restrict root; cannot test unreadable path")
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    gov_md = tmp_path / "agent" / "governance.md"
    gov_md.write_text("# Governance\n")
    gov_md.chmod(0o000)
    try:
        backend = FilesystemAgentRegistryBackend(tmp_path)
        result = backend.list_agents()
        assert len(result) == 1
        assert result[0].has_governance is False
    finally:
        gov_md.chmod(0o644)


def test_governance_md_all_enum_values_valid(tmp_path):
    """All valid enum values parse cleanly (no GovernanceParseError)."""
    valid_combos = [
        ("read-only", "yes", "no", "active"),
        ("draft-only", "no", "yes", "paused"),
        ("writes", "partial", "partial", "deprecated"),
        ("sends-or-acts", "yes", "no", "retired"),
    ]
    for i, (pt, cd, ws, ls) in enumerate(valid_combos):
        agent_dir = tmp_path / f"agent-{i}"
        agent_dir.mkdir()
        (agent_dir / "model.md").write_text("# model\n")
        (agent_dir / "governance.md").write_text(
            f"```yaml\n"
            f"governance:\n"
            f"  permission_tier: '{pt}'\n"
            f"  customer_data: '{cd}'\n"
            f"  writes_sor: '{ws}'\n"
            f"  lifecycle_status: '{ls}'\n"
            f"```\n"
        )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 4
    for ref in result:
        assert ref.has_governance is True
        assert ref.governance is not None
        assert ref.governance.parse_errors == ()


def test_governance_md_unquoted_tristate_matches_template_happy_path(tmp_path):
    """The DOCUMENTED unquoted tristate value (`customer_data: no`) parses
    cleanly end-to-end (#607).

    The governance.md template comment instructs `yes / no / partial` and an
    operator naturally types the bare word. PyYAML coerces `no`→False / `yes`
    →True, which previously tripped the enum validator and produced a
    PRESENT_INVALID record that discarded EVERY field (incl. permission_tier).
    This test writes the template-shaped unquoted form and asserts a clean,
    field-preserving parse. The sibling test above
    (`test_governance_md_all_enum_values_valid`) only covers the QUOTED form,
    so this is the regression guard for the operator's actual happy path.

    Strip-RED negative control: remove the bool coercion in
    types.py::_validate_enum and this test fails (parse_errors non-empty,
    permission_tier/owner gutted to None).
    """
    agent_dir = tmp_path / "highrisk"
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text("# model\n")
    # Unquoted tristate values, exactly as the shipped template comment guides.
    (agent_dir / "governance.md").write_text(
        "```yaml\n"
        "governance:\n"
        "  owner: security@example.com\n"
        "  permission_tier: sends-or-acts\n"
        "  customer_data: no\n"
        "  writes_sor: yes\n"
        "  lifecycle_status: active\n"
        "```\n"
    )
    ref = FilesystemAgentRegistryBackend(tmp_path).get_agent("highrisk")
    assert ref is not None
    assert ref.has_governance is True
    assert ref.governance is not None
    assert ref.governance.parse_errors == ()
    assert ref.governance.customer_data == "no"
    assert ref.governance.writes_sor == "yes"
    # The valid, security-relevant sibling fields must survive.
    assert ref.governance.permission_tier == "sends-or-acts"
    assert ref.governance.owner == "security@example.com"


def test_governance_md_invalid_lifecycle_status(tmp_path):
    """Invalid lifecycle_status enum → has_governance=True, parse_errors non-empty."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "```yaml\n"
        "governance:\n"
        "  lifecycle_status: 'pending'\n"  # not in allowed set
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    assert len(ref.governance.parse_errors) > 0


def test_governance_md_invalid_customer_data(tmp_path):
    """Invalid customer_data enum → has_governance=True, parse_errors non-empty."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "```yaml\n"
        "governance:\n"
        "  customer_data: 'maybe'\n"  # not in allowed set (yes/no/partial)
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    ref = result[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    # 'maybe' is not a valid Tristate.
    assert len(ref.governance.parse_errors) > 0


def test_governance_md_malformed_example_block_before_valid_block(tmp_path):
    """A malformed illustrative yaml block placed BEFORE the real governance
    block must NOT shadow it — the parser defers the YAML error, keeps scanning,
    and parses the valid governance block cleanly (parse_errors empty).
    """
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        # An intentionally-malformed EXAMPLE block first (unbalanced bracket).
        "## Example (do not copy verbatim)\n"
        "```yaml\n"
        "example:\n"
        "  bad: [unclosed\n"
        "```\n"
        "## Actual governance\n"
        "```yaml\n"
        "governance:\n"
        "  permission_tier: 'read-only'\n"
        "  lifecycle_status: 'active'\n"
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    assert ref.governance.permission_tier == "read-only"
    # The malformed EXAMPLE block must not produce a parse error: the real
    # governance block was found and parsed cleanly.
    assert ref.governance.parse_errors == ()


def test_governance_md_malformed_block_only_no_governance_key(tmp_path):
    """When the ONLY yaml block is malformed and no governance: block exists,
    the deferred YAML error surfaces as PRESENT_INVALID (has_governance=True +
    non-empty parse_errors) — fail-soft, never crashing the loop.
    """
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "```yaml\nnotes: [unclosed\n```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    assert len(ref.governance.parse_errors) > 0


# ──────────────────────────────────────────────────────────────────
# Symlink-escape guards (MUST 5 fail-soft)


def test_governance_md_symlink_to_out_of_tree_file_not_followed(tmp_path):
    """A governance.md that is a SYMLINK to an out-of-tree file is refused (not
    followed) → has_governance=False, governance=None. The agent itself is still
    discovered (its model.md is real); only governance is skipped fail-soft.

    Strip-RED negative control: remove the `if gov_md.is_symlink()` guard in
    _parse_governance and this test fails — the parser would read_text() the
    out-of-tree target and surface has_governance=True (following the link).
    """
    # An out-of-tree file holding a real governance block (the exfil target).
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_gov = outside / "secret-governance.md"
    secret_gov.write_text(
        "```yaml\ngovernance:\n  permission_tier: 'sends-or-acts'\n```\n"
    )

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    agent_dir = agents_root / "agent"
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text("# model\n")
    # governance.md is a symlink pointing OUT of agents_root.
    (agent_dir / "governance.md").symlink_to(secret_gov)

    backend = FilesystemAgentRegistryBackend(agents_root)
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.id == "agent"
    # The symlinked governance.md must NOT be followed.
    assert ref.has_governance is False
    assert ref.governance is None


def test_model_md_symlink_to_out_of_tree_file_excludes_folder(tmp_path):
    """A model.md that is a SYMLINK to an out-of-tree file makes that folder NOT
    a framework agent: excluded from list_agents() AND get_agent() returns None.

    Strip-RED negative control: remove the `if model_md.is_symlink()` guard in
    BOTH list_agents() and get_agent() and this test fails — the folder would be
    discovered (the symlink target is a readable model.md), letting an attacker
    plant an out-of-tree model.md.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    real_model = outside / "real-model.md"
    real_model.write_text("# model\n")

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    agent_dir = agents_root / "linked"
    agent_dir.mkdir()
    # model.md is a symlink pointing OUT of agents_root.
    (agent_dir / "model.md").symlink_to(real_model)

    backend = FilesystemAgentRegistryBackend(agents_root)
    ids = [r.id for r in backend.list_agents()]
    assert "linked" not in ids
    # get_agent() must also refuse it.
    assert backend.get_agent("linked") is None


def test_governance_md_size_cap_skips_oversized_file(tmp_path):
    """A governance.md larger than the size cap is treated as unreadable
    (has_governance=False), so a hostile multi-MB file can't blow up discovery.
    The agent itself is still discovered (model.md is real).
    """
    from atomic_agents.agent_registry.filesystem import _GOVERNANCE_MAX_BYTES

    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    # Write a governance.md just over the cap.
    big = "x" * (_GOVERNANCE_MAX_BYTES + 1)
    (tmp_path / "agent" / "governance.md").write_text(big)

    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    assert result[0].id == "agent"
    assert result[0].has_governance is False
    assert result[0].governance is None


def test_governance_md_null_block_does_not_shadow_valid_later_block(tmp_path):
    """A null/empty `governance:` block placed BEFORE a valid `governance: {...}`
    block must NOT shadow it — the scan keeps going to the real block (#607 P2).

    Strip-RED negative control: revert the `isinstance(parsed[_GOVERNANCE_KEY],
    dict)` guard in the scan loop and this test fails — the first (null) block
    sets gov_dict=None and breaks, yielding has_governance=True/governance=None
    instead of the populated record.
    """
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        # A null governance: block first (no body).
        "## Stub\n"
        "```yaml\n"
        "governance:\n"
        "```\n"
        "## Actual governance\n"
        "```yaml\n"
        "governance:\n"
        "  owner: real@example.com\n"
        "  permission_tier: 'writes'\n"
        "  lifecycle_status: 'active'\n"
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    assert len(result) == 1
    ref = result[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    assert ref.governance.owner == "real@example.com"
    assert ref.governance.permission_tier == "writes"
    assert ref.governance.parse_errors == ()


def test_governance_md_explicit_null_value_block_does_not_shadow(tmp_path):
    """An explicit `governance: null` block (vs an empty one) before a valid
    block ALSO must not shadow it — the dict-value guard covers both forms.
    """
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "```yaml\n"
        "governance: null\n"
        "```\n"
        "```yaml\n"
        "governance:\n"
        "  permission_tier: 'read-only'\n"
        "  lifecycle_status: 'active'\n"
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    ref = backend.list_agents()[0]
    assert ref.has_governance is True
    assert ref.governance is not None
    assert ref.governance.permission_tier == "read-only"
    assert ref.governance.parse_errors == ()


def test_governance_md_unquoted_yaml_date_round_trips_to_isoformat_str(tmp_path):
    """An UNQUOTED ISO date in governance.md (`created_at: 2026-06-24`) is coerced
    by PyYAML to a datetime.date; from_dict() must coerce it back to an isoformat
    STR (not leave a date object on a field typed str | None) (#607 RT1).

    Strip-RED negative control: remove the _coerce_date helper application in
    types.py::from_dict and this test fails — created_at is a datetime.date, not
    a str.
    """
    import datetime as _dt

    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "model.md").write_text("# model\n")
    (tmp_path / "agent" / "governance.md").write_text(
        "```yaml\n"
        "governance:\n"
        "  permission_tier: 'read-only'\n"
        "  lifecycle_status: 'active'\n"
        "  created_at: 2026-06-24\n"  # unquoted — PyYAML → datetime.date
        "  updated_at: 2026-06-25\n"
        "```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)
    ref = backend.list_agents()[0]
    assert ref.governance is not None
    assert isinstance(ref.governance.created_at, str)
    assert not isinstance(ref.governance.created_at, _dt.date)
    assert ref.governance.created_at == "2026-06-24"
    assert isinstance(ref.governance.updated_at, str)
    assert ref.governance.updated_at == "2026-06-25"


# ──────────────────────────────────────────────────────────────────
# get_agent edge cases


def test_get_agent_folder_deleted_between_list_and_get(tmp_path):
    """TOCTOU: folder deleted between list_agents() and get_agent() → None."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)
    assert len(backend.list_agents()) == 1
    # Delete the whole folder.
    import shutil

    shutil.rmtree(agent_dir)
    assert backend.get_agent("agent") is None


def test_get_agent_traversal_slash(tmp_path):
    """get_agent with slash in agent_id raises PathTraversalError."""
    from atomic_agents.exceptions import PathTraversalError

    backend = FilesystemAgentRegistryBackend(tmp_path)
    with pytest.raises(PathTraversalError):
        backend.get_agent("foo/bar")


def test_get_agent_traversal_dotdot(tmp_path):
    """get_agent with '..' raises PathTraversalError."""
    from atomic_agents.exceptions import PathTraversalError

    backend = FilesystemAgentRegistryBackend(tmp_path)
    with pytest.raises(PathTraversalError):
        backend.get_agent("..")


def test_get_agent_double_dot_substring_is_a_legitimate_name(tmp_path):
    """MUST 8: an agent_id that merely CONTAINS '..' (e.g. 'a..b') is a valid
    bare folder name and must NOT raise — only exact '.'/'..', separators, and
    empty are rejected. safe_resolve_under() provides containment for it.
    """
    from atomic_agents.exceptions import PathTraversalError

    agent_dir = tmp_path / "a..b"
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text("# model\n")
    backend = FilesystemAgentRegistryBackend(tmp_path)

    # Does not raise (would raise if the guard used `'..' in agent_id`).
    ref = backend.get_agent("a..b")
    assert ref is not None
    assert ref.id == "a..b"

    # And the exact-match cases still raise.
    with pytest.raises(PathTraversalError):
        backend.get_agent(".")


def test_get_agent_excludes_prefix_hidden_dir_consistent_with_list(tmp_path):
    """MUST 3 consistency: get_agent() on a `_`- or `.`-prefixed id returns
    None, agreeing with the list_agents() exclusion of those dirs.

    Without this guard get_agent('_dashboard') would resurface a deliberately-
    hidden dir (the dashboard scratch dir, .git, etc.) by id even though
    list_agents() never yields it — breaking the registry invariant that
    get_agent(x) returning non-None implies x is in the list_agents() id set.

    Strip-RED negative control: remove the
    `if agent_id.startswith(("_", ".")): return None` guard in
    filesystem.py::get_agent and this test fails (get_agent returns a populated
    AgentRef for '_dashboard'/'.hidden' that list_agents() excludes).
    """
    # A fully-formed agent dir that the prefix rule deliberately hides.
    for hidden in ("_dashboard", ".hidden"):
        d = tmp_path / hidden
        d.mkdir()
        (d / "model.md").write_text("# model\n")
    # One real agent so list_agents() is non-empty.
    real = tmp_path / "advisor"
    real.mkdir()
    (real / "model.md").write_text("# model\n")

    backend = FilesystemAgentRegistryBackend(tmp_path)

    list_ids = {r.id for r in backend.list_agents()}
    assert list_ids == {"advisor"}
    assert "_dashboard" not in list_ids
    assert ".hidden" not in list_ids

    # get_agent() agrees: the hidden dirs are a miss, the real agent is found.
    assert backend.get_agent("_dashboard") is None
    assert backend.get_agent(".hidden") is None
    assert backend.get_agent("advisor") is not None
    # Invariant: every get_agent hit is in the list_agents id set.
    for agent_id in list_ids:
        assert backend.get_agent(agent_id) is not None


def test_list_agents_include_governance_false_skips_parse(tmp_path):
    """include_governance=False yields entries with has_governance=False even
    when a valid governance.md is present — the per-agent parse is skipped
    (progressive disclosure). The discovery predicate is unchanged.
    """
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text("# model\n")
    (agent_dir / "governance.md").write_text(
        "```yaml\ngovernance:\n  permission_tier: 'read-only'\n```\n"
    )
    backend = FilesystemAgentRegistryBackend(tmp_path)

    # Default path parses governance.
    full = backend.list_agents()
    assert len(full) == 1
    assert full[0].has_governance is True
    assert full[0].governance is not None

    # id-only path skips it.
    lite = backend.list_agents(include_governance=False)
    assert len(lite) == 1
    assert lite[0].id == "agent"
    assert lite[0].has_governance is False
    assert lite[0].governance is None


# ──────────────────────────────────────────────────────────────────
# _redact_for_error_message


def test_redact_url_with_credentials():
    """URL with credentials is redacted to scheme://..."""
    result = _redact_for_error_message("postgres://user:secret@host/db")
    assert "secret" not in result
    assert result == "postgres://..."


def test_redact_schemeless_dsn():
    """DSN without scheme (user:pass@host) is replaced with placeholder."""
    result = _redact_for_error_message("user:secret@host/db")
    assert "secret" not in result
    assert "[redacted" in result


def test_redact_long_value():
    """Long value is truncated at max_len."""
    result = _redact_for_error_message("a" * 100)
    assert len(result) <= 36  # 32 + "..."


def test_redact_normal_backend_id():
    """Normal backend id (e.g. 'filesystem') passes through unchanged."""
    result = _redact_for_error_message("filesystem")
    assert result == "filesystem"


def test_redact_credential_not_in_check_result(tmp_path, monkeypatch):
    """Doctor check_agent_registry_backend must not echo raw DSN in CheckResult."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND",
        "postgres://user:hunter2@host/db",
    )
    from atomic_agents.doctor import check_agent_registry_backend

    result = check_agent_registry_backend(tmp_path)
    msg = result.message or ""
    detail_str = str(result.detail or "")
    assert "hunter2" not in msg
    assert "hunter2" not in detail_str


# ──────────────────────────────────────────────────────────────────
# discover_agents integration with registry (ADOPT-NOW)


def test_discover_agents_routes_through_registry(tmp_path):
    """discover_agents routes through FilesystemAgentRegistryBackend internally."""
    from atomic_agents.dashboard.costs import discover_agents

    # Just model.md — no log/ — newly deployed agent.
    (tmp_path / "new-agent").mkdir()
    (tmp_path / "new-agent" / "model.md").write_text("# model\n")
    # Only log/ — not a framework agent by spec/37:314.
    (tmp_path / "data-dir" / "log").mkdir(parents=True)

    agents = discover_agents(tmp_path)
    assert "new-agent" in agents
    assert "data-dir" not in agents
    # Returns list[str] for backward compat.
    assert all(isinstance(a, str) for a in agents)


def test_discover_agents_degrades_on_bogus_env_var(tmp_path, monkeypatch):
    """A typo'd ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND must NOT crash the dashboard
    discovery path — discover_agents() falls back to the filesystem registry and
    still returns the agent ids. Fail-loud belongs to the doctor check, not the
    render path (resilience; the throughline keeps the fleet view from crashing
    on operator misconfig).
    """
    monkeypatch.setenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", "typo-not-a-backend")
    from atomic_agents.dashboard.costs import discover_agents

    (tmp_path / "new-agent").mkdir()
    (tmp_path / "new-agent" / "model.md").write_text("# model\n")

    agents = discover_agents(tmp_path)
    assert agents == ["new-agent"]


def test_discover_agents_degrades_on_backend_init_raise(tmp_path, monkeypatch):
    """A registered backend whose __init__ raises a NON-BackendNotRegistered error
    (e.g. a future DB-backed registry that opens a connection in its constructor)
    must NOT crash the dashboard discovery path. discover_agents() degrades to the
    filesystem default and still returns the filesystem-discovered ids.

    Negative control for the round-3 broadening of the costs.py fallback from
    `except BackendNotRegistered` to `except Exception` — the narrow catch did NOT
    cover a backend whose constructor raises, so reverting the broadening makes
    THIS test go RED (the RuntimeError would propagate and crash discovery).
    """
    from atomic_agents.agent_registry import (
        register_agent_registry_backend,
        unregister_agent_registry_backend,
    )
    from atomic_agents.dashboard.costs import discover_agents

    class _ExplodingInitBackend:
        def __init__(self, agents_root):
            raise RuntimeError("connection to db://user:pw@host failed")

    register_agent_registry_backend("exploding-init", _ExplodingInitBackend)
    monkeypatch.setenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", "exploding-init")
    try:
        (tmp_path / "new-agent").mkdir()
        (tmp_path / "new-agent" / "model.md").write_text("# model\n")
        agents = discover_agents(tmp_path)
        assert agents == ["new-agent"]
    finally:
        unregister_agent_registry_backend("exploding-init")


def test_discover_agents_degrades_on_list_agents_raise(tmp_path, monkeypatch):
    """A registered backend that constructs fine but whose list_agents() raises
    must ALSO degrade — the costs.py try/except spans both construction AND the
    list_agents() enumeration, so the resilience promise is cross-backend, not
    filesystem-only.

    Negative control for widening the try to cover the list_agents() call: if the
    enumeration call were left outside the try, this RuntimeError would propagate
    and crash every dashboard tab (the exact failure the docstring claims to
    prevent).
    """
    from atomic_agents.agent_registry import (
        register_agent_registry_backend,
        unregister_agent_registry_backend,
    )
    from atomic_agents.dashboard.costs import discover_agents

    class _ExplodingListBackend:
        def __init__(self, agents_root):
            self.agents_root = agents_root

        def list_agents(self, *, include_governance: bool = True):
            raise RuntimeError("enumeration to db://user:pw@host failed")

    register_agent_registry_backend("exploding-list", _ExplodingListBackend)
    monkeypatch.setenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", "exploding-list")
    try:
        (tmp_path / "new-agent").mkdir()
        (tmp_path / "new-agent" / "model.md").write_text("# model\n")
        agents = discover_agents(tmp_path)
        assert agents == ["new-agent"]
    finally:
        unregister_agent_registry_backend("exploding-list")


def test_discover_agents_degrades_to_empty_when_filesystem_default_raises(
    tmp_path, monkeypatch
):
    """The HOME-USER default path: env var unset → the filesystem backend is BOTH
    the primary and the fallback. If FilesystemAgentRegistryBackend.list_agents()
    raises (an ELOOP/permission OSError on a single entry that escapes the
    per-entry guard, or any other unexpected failure), the naive fallback would
    re-run the exact same code and re-raise — crashing every dashboard tab. The
    fallback is ITSELF guarded and must degrade to [] instead of propagating.

    Strip-RED negative control for the second (fallback) try/except in
    discover_agents(): if that guard is removed, the OSError propagates out of
    discover_agents() and this test fails (the assert never runs).
    """
    from atomic_agents.dashboard.costs import discover_agents

    monkeypatch.delenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", raising=False)

    (tmp_path / "agent-a").mkdir()
    (tmp_path / "agent-a" / "model.md").write_text("# model\n")

    def _boom(self, *, include_governance: bool = True):
        raise OSError("ELOOP on agents_root walk")

    monkeypatch.setattr(
        "atomic_agents.agent_registry.FilesystemAgentRegistryBackend.list_agents",
        _boom,
    )

    # Both the primary (filesystem default) AND the fallback (also filesystem)
    # raise the SAME OSError; discover_agents() must degrade to [], not crash.
    assert discover_agents(tmp_path) == []


# ──────────────────────────────────────────────────────────────────
# MUST 5 (fail-soft): per-entry resolution error skips ONE entry, not the loop


def test_list_agents_skips_entry_whose_is_dir_raises_oserror(tmp_path, monkeypatch):
    """MUST 5 (fail-soft), per-entry resolution arm: a single directory entry
    whose ``is_dir()`` raises ``OSError`` (e.g. ELOOP / permission denial on an
    intermediate path component) is SKIPPED, and the fleet enumeration continues
    to return the healthy agents — it does NOT abort the whole loop.

    This exercises the real ``FilesystemAgentRegistryBackend.list_agents()``
    surface (not an import-only stub): two healthy agents plus one pathological
    entry are placed in agents_root, and ``Path.is_dir`` is patched to raise
    ``OSError`` for ONLY the pathological entry. The symlink-containment test
    covers the ``PathTraversalError`` arm; this covers the ``(OSError,
    RuntimeError)`` arm that the containment skip does not reach.

    Strip-RED negative control: if the per-entry ``except (OSError, RuntimeError)``
    guard in ``list_agents()`` is removed, the OSError propagates out of
    ``list_agents()`` and this test fails (the healthy agents are never returned
    and the assert raises instead).
    """
    from pathlib import Path

    for name in ("good-a", "good-b", "boom"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "model.md").write_text("# model\n")

    real_is_dir = Path.is_dir

    def _is_dir(self):
        # Raise only for the one pathological entry; every other path (including
        # agents_root itself at the top of list_agents) uses the real is_dir, so
        # the test isolates the per-entry arm rather than breaking the root probe.
        if self.name == "boom" and self.parent == tmp_path:
            raise OSError("ELOOP on intermediate path component")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _is_dir)

    backend = FilesystemAgentRegistryBackend(tmp_path)
    result = backend.list_agents()
    ids = [r.id for r in result]

    # The pathological entry is skipped; the loop survived and returned the
    # healthy agents in sorted order.
    assert ids == ["good-a", "good-b"]
    assert "boom" not in ids


def test_list_agents_skips_entry_whose_resolve_raises(tmp_path, monkeypatch):
    """MUST 5 (fail-soft), per-entry resolution arm via ``resolve()``: an entry
    that passes ``is_dir()`` and the prefix check but whose containment
    resolution raises a non-PathTraversalError ``OSError``/``RuntimeError`` (e.g.
    a symlink loop surfaced during resolution) is SKIPPED, and the enumeration
    continues.

    Exercises the real surface by patching ``safe_resolve_under`` (the function
    ``list_agents()`` actually calls for containment) to raise for ONLY the
    pathological entry name.

    Strip-RED negative control: removing the ``except (OSError, RuntimeError)``
    arm makes the RuntimeError propagate and this test fails.
    """
    import atomic_agents.agent_registry.filesystem as fs_mod

    for name in ("alpha", "loopy"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "model.md").write_text("# model\n")

    real_resolve = fs_mod.safe_resolve_under

    def _resolve(name, root):
        if name == "loopy":
            raise RuntimeError("Symlink loop during containment resolution")
        return real_resolve(name, root)

    monkeypatch.setattr(fs_mod, "safe_resolve_under", _resolve)

    backend = FilesystemAgentRegistryBackend(tmp_path)
    ids = [r.id for r in backend.list_agents()]

    assert ids == ["alpha"]
    assert "loopy" not in ids


# ──────────────────────────────────────────────────────────────────
# Doctor check wiring


def test_doctor_check_agent_registry_backend_pass(tmp_path):
    """check_agent_registry_backend returns PASS for clean agents_root."""
    from atomic_agents.doctor import check_agent_registry_backend

    (tmp_path / "agent-x").mkdir()
    (tmp_path / "agent-x" / "model.md").write_text("# model\n")

    result = check_agent_registry_backend(tmp_path)
    assert result.name == "agent-registry-backend"
    assert result.status in ("pass", "warn")  # warn ok if reconcile finds profile gap
    assert result.detail is not None
    assert result.detail.get("agent_count") == 1


def test_doctor_reconcile_warns_registry_has_agent_profile_missing(tmp_path):
    """Reconcile direction A: an agent with model.md but no profile sentinel
    (no persona/IDENTITY.md and no persona.link.md) is visible to the registry
    but not the profile backend → WARN.
    """
    from atomic_agents.doctor import check_agent_registry_backend

    (tmp_path / "registry-only").mkdir()
    (tmp_path / "registry-only" / "model.md").write_text("# model\n")

    result = check_agent_registry_backend(tmp_path)
    assert result.status == "warn"
    warnings = result.detail.get("reconcile_warnings", [])
    assert any("registry-only" in w for w in warnings)


def test_doctor_reconcile_warns_profile_has_agent_registry_missing(tmp_path):
    """Reconcile direction B (the bidirectional spec/51 §"Doctor check" contract): an agent
    the profile backend knows (persona/IDENTITY.md present) but whose model.md
    is absent is invisible to the registry → WARN. This is the direction a
    one-sided reconcile would silently miss.
    """
    from atomic_agents.doctor import check_agent_registry_backend

    # Profile-visible (IDENTITY.md) but registry-invisible (no model.md).
    (tmp_path / "profile-only" / "persona").mkdir(parents=True)
    (tmp_path / "profile-only" / "persona" / "IDENTITY.md").write_text("# Identity\n")

    result = check_agent_registry_backend(tmp_path)
    assert result.status == "warn"
    warnings = result.detail.get("reconcile_warnings", [])
    assert any("profile-only" in w for w in warnings), (
        "the profile-has / registry-missing reconcile direction must surface a "
        'WARN naming the vanished agent (spec/51 §"Doctor check" bidirectional reconcile)'
    )


def test_doctor_reconcile_ignores_underscore_prefixed_full_agent_dir(tmp_path):
    """A '_'-prefixed dir with a full agent layout (model.md AND persona/IDENTITY.md)
    must NOT fire a spurious direction-B WARN. The profile backend skips only
    '.'-prefixed dirs, while the registry skips BOTH '_' and '.'-prefixed dirs, so
    a naive set difference would see '_internal' in profile_ids but not
    registry_ids and falsely report 'model.md missing or unreadable?' when model.md
    is actually present and the dir was intentionally excluded by the '_'-prefix
    convention. Reconcile must compare the same candidate universe both stores use.

    Negative control: drop the '_'/'.' filter on profile_ids and this goes RED
    (a '_internal' WARN appears).
    """
    from atomic_agents.doctor import check_agent_registry_backend

    # A real agent so the check has a non-empty registry.
    (tmp_path / "real-agent" / "persona").mkdir(parents=True)
    (tmp_path / "real-agent" / "model.md").write_text("# model\n")
    (tmp_path / "real-agent" / "persona" / "IDENTITY.md").write_text("# Identity\n")

    # A '_'-prefixed dir with a FULL agent layout — intentionally excluded by the
    # registry's '_'-prefix convention, present to the '.'-only profile filter.
    (tmp_path / "_internal" / "persona").mkdir(parents=True)
    (tmp_path / "_internal" / "model.md").write_text("# model\n")
    (tmp_path / "_internal" / "persona" / "IDENTITY.md").write_text("# Identity\n")

    result = check_agent_registry_backend(tmp_path)
    warnings = result.detail.get("reconcile_warnings", [])
    assert not any("_internal" in w for w in warnings), (
        "a '_'-prefixed full-agent dir must not produce a spurious 'model.md "
        "missing' reconcile WARN — it is intentionally excluded by the registry's "
        "'_'-prefix convention, and the profile id set must be filtered to the "
        "same candidate universe before the set difference"
    )
    # And the dir is genuinely excluded from discovery.
    assert result.detail.get("agent_count") == 1


def test_doctor_check_agent_registry_backend_empty_vault(tmp_path):
    """check_agent_registry_backend handles empty vault (0 agents) without error."""
    from atomic_agents.doctor import check_agent_registry_backend

    result = check_agent_registry_backend(tmp_path)
    assert result.name == "agent-registry-backend"
    assert result.status == "pass"
    assert result.detail.get("agent_count") == 0


def test_doctor_check_agent_registry_backend_fail_on_bad_env(tmp_path, monkeypatch):
    """check_agent_registry_backend returns FAIL when env var names unknown backend."""
    monkeypatch.setenv("ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND", "unknown-xyz")
    from atomic_agents.doctor import check_agent_registry_backend

    result = check_agent_registry_backend(tmp_path)
    assert result.name == "agent-registry-backend"
    assert result.status == "fail"


def test_run_doctor_includes_agent_registry_backend_in_skip_list(tmp_path):
    """run_doctor without --agent includes 'agent-registry-backend' in SKIP results."""
    from atomic_agents.doctor import run_doctor

    results = run_doctor(agent_name=None, agents_root=tmp_path)
    skip_names = {r.name for r in results if r.status == "skip"}
    assert "agent-registry-backend" in skip_names, (
        "run_doctor() without --agent must emit SKIP for 'agent-registry-backend' "
        "to maintain the consistent check-roster invariant"
    )


def test_run_doctor_includes_agent_registry_backend_exactly_once(tmp_path):
    """run_doctor with --agent includes 'agent-registry-backend' exactly once."""
    from atomic_agents.doctor import run_doctor

    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text("# model\n")
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n")
    (agent_dir / "tools.md").write_text("# Tools\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "memory" / "INDEX.md").write_text("# Index\n")

    results = run_doctor(agent_name="my-agent", agents_root=tmp_path, skip_mcp=True)
    registry_results = [r for r in results if r.name == "agent-registry-backend"]
    assert len(registry_results) == 1, (
        "agent-registry-backend must appear exactly once in run_doctor() results"
    )


# ──────────────────────────────────────────────────────────────────
# Init template governance.md stub


def test_init_templates_have_governance_md():
    """Every init template must include governance.md (spec/51 ADOPT-NOW)."""
    import importlib.resources as _resources

    template_names = ["advisor", "researcher", "writer"]
    for template_name in template_names:
        template_path = (
            _resources.files("atomic_agents.init") / "templates" / template_name
        )
        gov_file = template_path / "governance.md"
        # Check the file exists by trying to read it.
        try:
            content = gov_file.read_text(encoding="utf-8")
        except Exception as e:
            pytest.fail(f"Template '{template_name}' is missing governance.md: {e}")
        assert "governance:" in content, (
            f"Template '{template_name}/governance.md' must contain a 'governance:' YAML key"
        )
        assert "permission_tier" in content, (
            f"Template '{template_name}/governance.md' must mention 'permission_tier'"
        )


def test_init_wizard_writes_governance_md(tmp_path, monkeypatch):
    """atomic-agents init writes governance.md to new agent scaffold.

    Exercises the _render_files() path by calling cli.main() directly with
    mocked TTY/doctor guards (same pattern as test_init_smoke.py).
    """
    from atomic_agents import cli as cli_module
    from atomic_agents.doctor import PASS, CheckResult

    # Mirror test_init_smoke.py's _patch_common pattern.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "atomic_agents._llm._get_key",
        lambda env_vars=None, keychain_name=None, config_key=None: "sk-ant-test-key",
    )
    passing_result = CheckResult(name="env", status=PASS, message="ok")
    monkeypatch.setattr(
        "atomic_agents.doctor.run_doctor",
        lambda agent_name=None, agents_root=None, skip_mcp=False: [passing_result],
    )
    monkeypatch.setattr("atomic_agents.doctor.render_human", lambda r: "")
    monkeypatch.setattr("atomic_agents.doctor.overall_exit_code", lambda r: 0)
    # Decline the test call so no AtomicAgent.call() is attempted.
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: False)

    exit_code = cli_module.main(
        [
            "init",
            "test-gov-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 0

    gov_md = tmp_path / "test-gov-agent" / "governance.md"
    assert gov_md.exists(), "init must create governance.md for --template advisor"
    content = gov_md.read_text()
    assert "governance:" in content
    assert "permission_tier" in content

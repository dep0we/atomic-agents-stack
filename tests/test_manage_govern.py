"""Tests for ``atomic-agents manage govern`` (spec/55 #609/#624).

Covers:
- S1 registry-as-selector: target resolved through AgentRegistryBackend
- S2 five-step safety routine: validate → preview → confirm → snapshot+write → audit
- M2 surgical preservation: comments, key order, prose body survive byte-for-byte
- M3 snapshot: taken before write, at the correct path
- M4 validation: unknown field, invalid enum, dotted-path, malformed token
- M5 confirm-by-default: --dry-run writes nothing; non-TTY requires --yes
- M6 copilot properties: --json structured output on success + refusal
- M8 audit: RunRecord in both per-agent and fleet log scopes; audit-drop is non-fatal
- M9 composition gates: empty for govern (documented intentional no-op)
- create-absent governance: renders canonical template; does NOT clobber existing
- PRESENT_INVALID guard: refuse when governance.md has parse_errors
- hyphen-to-underscore field name mapping
- updated_at auto-stamp on every applied write
- run_id is a valid UUID v4 in the audit record
- RunRecord.to_dict() is fully JSON-serialisable (no datetime.date objects)
- Template lint: advisor / researcher / writer governance.md are byte-identical
  with the shared canonical template at init/templates/governance.md
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.manage.govern import (
    _edit_governance_block,
    _find_governance_block_span,
    _parse_set_token,
    _validate_field_value,
    _today_iso,
    run_govern,
)
from atomic_agents.manage.exceptions import (
    ManageControlCharRefused,
    ManageInvalidEnumError,
    ManageNestedPathRefused,
    ManageUnknownFieldError,
)
from atomic_agents.logs.types import PRIMITIVE_MANAGE_GOVERN, RunRecord
from atomic_agents.agent_registry.types import GovernanceRecord


# ──────────────────────────────────────────────────────────────────
# Helpers: canonical governance.md template content


def _canonical_governance_md(agent_name: str = "test-agent") -> str:
    """Render the canonical governance.md stub via the shared renderer."""
    from atomic_agents.init import render_governance_stub

    return render_governance_stub(agent_name)


def _make_agent_dir(tmp_path: Path, agent_name: str = "myagent") -> Path:
    """Create a minimal agent directory with model.md (required by registry MUST-3)."""
    agent_dir = tmp_path / agent_name
    agent_dir.mkdir()
    (agent_dir / "model.md").write_text(
        "# Model\n\n## Default model\n\nclaude-3-5-haiku-20241022\n"
    )
    return agent_dir


def _make_governance_md(agent_dir: Path, content: str | None = None) -> Path:
    """Write governance.md to agent_dir. Uses canonical template if content is None."""
    gov_path = agent_dir / "governance.md"
    if content is None:
        content = _canonical_governance_md(agent_dir.name)
    gov_path.write_text(content, encoding="utf-8")
    return gov_path


def _make_args(
    agent: str,
    agents_root: Path,
    set_fields: list[str] | None = None,
    show: bool = False,
    dry_run: bool = False,
    yes: bool = True,  # default True so tests don't block on TTY
    use_json: bool = False,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    set_json: list[str] | None = None,
) -> Any:
    """Build a minimal argparse-like namespace for run_govern().

    The list-mutation flags (add/remove/set_json) mirror argparse's ``default=None``
    so the mock namespace matches the real one — otherwise a bare ``MagicMock``
    attribute would read as a truthy value and spuriously trip the PR1
    list-mutation refusal.
    """
    ns = MagicMock()
    ns.agent = agent
    ns.set = set_fields
    ns.show = show
    ns.dry_run = dry_run
    ns.yes = yes
    ns.json = use_json
    ns.add = add
    ns.remove = remove
    ns.set_json = set_json
    ns.agents_root = str(agents_root)
    return ns


def _get_fleet_log_dir(agents_root: Path) -> Path:
    return agents_root / "_manage" / "log"


# ──────────────────────────────────────────────────────────────────
# Template lint: all four governance.md templates must be byte-identical


def test_all_governance_templates_are_byte_identical():
    """The three per-template copies and the shared canonical copy must match.

    This test enforces the spec/55 'one canonical governance.md shape' invariant:
    when any template changes, all four copies must be updated together.
    """
    from importlib import resources as _r

    canonical = (
        _r.files("atomic_agents.init") / "templates" / "governance.md"
    ).read_text(encoding="utf-8")

    for tmpl in ("advisor", "researcher", "writer"):
        per_tmpl = (
            _r.files("atomic_agents.init") / "templates" / tmpl / "governance.md"
        ).read_text(encoding="utf-8")
        assert per_tmpl == canonical, (
            f"init/templates/{tmpl}/governance.md differs from the canonical "
            "init/templates/governance.md — keep all four copies in sync."
        )


# ──────────────────────────────────────────────────────────────────
# render_governance_stub


def test_render_governance_stub_contains_agent_name():
    content = _canonical_governance_md("billing-agent")
    assert "billing-agent" in content
    assert "governance:" in content  # has the YAML block


def test_render_governance_stub_is_deterministic_for_same_agent_name():
    """render_governance_stub is deterministic: same agent_name → byte-identical output.

    NOTE: this does NOT assert init/wizard and govern share a single call site —
    wizard renders its own per-type ``templates/<type>/governance.md`` copy and does
    NOT call render_governance_stub. The "one canonical shape" invariant is held by
    ``test_all_governance_templates_are_byte_identical`` (the byte-identity lint),
    not by a shared renderer call. This test only pins renderer determinism.
    """
    from atomic_agents.init import render_governance_stub

    a = render_governance_stub("my-agent")
    b = render_governance_stub("my-agent")
    assert a == b
    assert a == _canonical_governance_md("my-agent")


# ──────────────────────────────────────────────────────────────────
# PRIMITIVE_MANAGE_GOVERN in logs/types.py


def test_primitive_manage_govern_in_logs_types():
    """PRIMITIVE_MANAGE_GOVERN must live in atomic_agents.logs.types."""
    from atomic_agents.logs import types as lt

    assert hasattr(lt, "PRIMITIVE_MANAGE_GOVERN")
    assert lt.PRIMITIVE_MANAGE_GOVERN == "manage_govern"


def test_primitive_manage_govern_round_trips_in_run_record(tmp_path):
    """A RunRecord with PRIMITIVE_MANAGE_GOVERN appends and queries cleanly."""
    from atomic_agents.logs import FilesystemLogBackend

    backend = FilesystemLogBackend(tmp_path)
    run_id = str(uuid.uuid4())
    rec = RunRecord(
        ts="2026-07-01T00:00:00+00:00",
        run_id=run_id,
        primitive=PRIMITIVE_MANAGE_GOVERN,
        status="applied",
        summary="test manage event",
        model="n/a",
        input_tokens=0,
        output_tokens=0,
        agent_name="test-agent",
        extra={"principal_id": "local", "changed_fields": ["owner"]},
    )
    backend.append(rec)
    from atomic_agents.logs.types import LogQuery

    results = backend.query(LogQuery())
    assert len(results) == 1
    assert results[0].primitive == PRIMITIVE_MANAGE_GOVERN
    assert results[0].run_id == run_id


# ──────────────────────────────────────────────────────────────────
# Field validation


def test_parse_set_token_basic():
    key, val = _parse_set_token("owner=alice@example.com")
    assert key == "owner"
    assert val == "alice@example.com"


def test_parse_set_token_hyphen_to_underscore():
    key, val = _parse_set_token("permission-tier=writes")
    assert key == "permission_tier"
    assert val == "writes"


def test_parse_set_token_value_contains_equals():
    """Values may contain '=' (emails, base64, URLs); split on first '=' only."""
    key, val = _parse_set_token("owner=user+tag=extra@example.com")
    assert key == "owner"
    assert val == "user+tag=extra@example.com"


def test_parse_set_token_null_value():
    key, val = _parse_set_token("owner=null")
    assert key == "owner"
    assert val is None


def test_parse_set_token_empty_value():
    key, val = _parse_set_token("owner=")
    assert key == "owner"
    assert val is None


def test_parse_set_token_unknown_field_raises():
    with pytest.raises(ManageUnknownFieldError) as exc_info:
        _parse_set_token("nonexistent-field=value")
    assert exc_info.value.error_type == "unknown_field"
    assert "nonexistent-field" in str(exc_info.value)


def test_parse_set_token_dotted_path_refused():
    """Dotted paths (review.reviewer) are reserved for PR2+."""
    with pytest.raises(ManageNestedPathRefused) as exc_info:
        _parse_set_token("review.reviewer=Alice")
    assert exc_info.value.error_type == "nested_path_refused"
    assert "edit governance.md directly" in str(exc_info.value)


def test_parse_set_token_no_equals_raises_value_error():
    with pytest.raises(ValueError, match="field=value"):
        _parse_set_token("just-a-field-no-equals")


def test_validate_enum_permission_tier_valid():
    for v in ("read-only", "draft-only", "writes", "sends-or-acts"):
        _validate_field_value("permission_tier", v)  # must not raise


def test_validate_enum_permission_tier_invalid():
    with pytest.raises(ManageInvalidEnumError) as exc_info:
        _validate_field_value("permission_tier", "super-admin")
    assert exc_info.value.error_type == "invalid_enum"


def test_validate_enum_tristate_valid():
    for f in ("customer_data", "writes_sor"):
        for v in ("yes", "no", "partial"):
            _validate_field_value(f, v)


def test_validate_enum_tristate_invalid():
    with pytest.raises(ManageInvalidEnumError):
        _validate_field_value("customer_data", "maybe")


def test_validate_enum_lifecycle_status_valid():
    for v in ("active", "paused", "deprecated", "retired"):
        _validate_field_value("lifecycle_status", v)


def test_validate_enum_lifecycle_status_invalid():
    with pytest.raises(ManageInvalidEnumError):
        _validate_field_value("lifecycle_status", "archived")


def test_validate_null_clears_any_field():
    """None (clearing) skips enum validation for any field."""
    _validate_field_value("permission_tier", None)
    _validate_field_value("lifecycle_status", None)


# ──────────────────────────────────────────────────────────────────
# Surgical YAML editor — find_governance_block_span


def test_find_governance_block_span_finds_correct_block():
    content = _canonical_governance_md("agent1")
    span = _find_governance_block_span(content)
    assert span is not None
    start, end = span
    block_body = content[start:end]
    assert "owner:" in block_body


def test_find_governance_block_span_non_governance_block_first():
    """When a non-governance YAML block precedes the governance block, the editor
    must select the CORRECT governance block, not the first one."""
    text = (
        "# Header\n\n"
        "```yaml\nsome_config:\n  key: value\n```\n\n"
        "More prose.\n\n"
        "```yaml\ngovernance:\n  owner: null\n  permission_tier: null\n```\n"
    )
    span = _find_governance_block_span(text)
    assert span is not None
    start, end = span
    block = text[start:end]
    assert "owner:" in block
    assert "some_config" not in block


def test_find_governance_block_span_absent():
    span = _find_governance_block_span("# No yaml blocks here\n\nJust prose.\n")
    assert span is None


# ──────────────────────────────────────────────────────────────────
# Surgical YAML editor — _edit_governance_block


def _simple_gov_file(owner: str = "null", permission_tier: str = "null") -> str:
    return (
        "# Governance : test-agent\n\nProse body.\n\n"
        "```yaml\n"
        "governance:\n"
        f"  owner: {owner}               # e.g. owner comment\n"
        "  backup_owner: null        # e.g. backup comment\n"
        f"  permission_tier: {permission_tier}\n"
        "  customer_data: null\n"
        "  writes_sor: null\n"
        "  lifecycle_status: active\n"
        "  created_at: null          # date comment\n"
        "  updated_at: null\n"
        "```\n\n"
        "## Forbidden actions\n\nProse section.\n"
    )


def test_edit_block_sets_owner():
    text = _simple_gov_file()
    result = _edit_governance_block(text, "owner", "alice@example.com")
    assert '"alice@example.com"' in result or "alice@example.com" in result
    # Prose body must survive byte-for-byte.
    assert "## Forbidden actions\n\nProse section.\n" in result


def test_edit_block_preserves_inline_comments():
    """M2(b): untargeted inline comments survive byte-for-byte."""
    text = _simple_gov_file()
    result = _edit_governance_block(text, "owner", "bob@example.com")
    # The backup_owner comment must survive.
    assert "# e.g. backup comment" in result
    # The date comment must survive.
    assert "# date comment" in result


def test_edit_block_targeted_line_inline_comment_survives():
    """M2(b): inline comment ON the targeted line must survive."""
    text = _simple_gov_file()
    result = _edit_governance_block(text, "owner", "bob@example.com")
    # The comment "# e.g. owner comment" on the owner line must survive.
    assert "# e.g. owner comment" in result


def test_edit_block_sets_null():
    text = _simple_gov_file(owner="alice@example.com")
    result = _edit_governance_block(text, "owner", None)
    assert "  owner: null" in result


def test_edit_block_sets_permission_tier():
    text = _simple_gov_file()
    result = _edit_governance_block(text, "permission_tier", "writes")
    assert "  permission_tier: writes" in result


def test_edit_block_prose_survives_byte_for_byte():
    """M2(a): prose outside the YAML block is preserved byte-for-byte.

    Uses the full governance block's match boundaries so the assertion is
    byte-equality on the pre-block AND post-block segments — not just a
    substring check that misses whitespace / indentation mutations.
    """
    original = _simple_gov_file()
    result = _edit_governance_block(original, "lifecycle_status", "paused")

    orig_match = next(
        m
        for m in re.finditer(r"```yaml\s*\n(.*?)```", original, re.DOTALL)
        if "governance:" in m.group(1)
    )
    result_match = next(
        m
        for m in re.finditer(r"```yaml\s*\n(.*?)```", result, re.DOTALL)
        if "governance:" in m.group(1)
    )
    # Pre-block and post-block prose must be byte-for-byte identical.
    assert result[: result_match.start()] == original[: orig_match.start()]
    assert result[result_match.end() :] == original[orig_match.end() :]


def test_edit_block_untargeted_keys_survive():
    """M2(b): all untargeted keys inside the block survive."""
    text = _simple_gov_file()
    result = _edit_governance_block(text, "owner", "carol@example.com")
    # backup_owner line untouched.
    assert "  backup_owner: null" in result
    # customer_data line untouched.
    assert "  customer_data: null" in result
    # lifecycle_status untouched.
    assert "  lifecycle_status: active" in result


def test_edit_block_does_not_edit_non_governance_block():
    """A non-governance YAML block before the governance block must not be edited."""
    text = (
        "# Header\n\n"
        "```yaml\nsome_config:\n  owner: old-value\n```\n\n"
        "```yaml\ngovernance:\n  owner: null\n  permission_tier: null\n  customer_data: null\n"
        "  writes_sor: null\n  lifecycle_status: active\n  created_at: null\n  updated_at: null\n```\n"
    )
    result = _edit_governance_block(text, "owner", "new@example.com")
    # The non-governance block's owner must be untouched.
    assert "  owner: old-value" in result
    # The governance block's owner must be updated.
    # (The new value appears in the second block only.)
    blocks = re.findall(r"```yaml\n(.*?)```", result, re.DOTALL)
    assert len(blocks) == 2
    assert "old-value" in blocks[0]  # non-governance block unchanged
    assert "new@example.com" in blocks[1]  # governance block updated


def test_edit_block_comment_only_lines_skipped():
    """Comment lines (first non-whitespace char is '#') must never be matched."""
    text = (
        "# Governance\n\n"
        "```yaml\n"
        "governance:\n"
        "  # Required: who is responsible for this agent\n"  # comment line with owner keyword
        "  owner: null\n"
        "  permission_tier: null\n"
        "  customer_data: null\n"
        "  writes_sor: null\n"
        "  lifecycle_status: active\n"
        "  created_at: null\n"
        "  updated_at: null\n"
        "```\n"
    )
    result = _edit_governance_block(text, "owner", "dan@example.com")
    # Comment line unchanged.
    assert "  # Required: who is responsible for this agent\n" in result
    # Owner line updated.
    assert "  owner:" in result
    assert "dan@example.com" in result


def test_edit_block_inserts_missing_key():
    """A key absent from the block is INSERTED (not KeyError) — partial files are
    the verb's whole point; the registry accepts them as present_valid."""
    text = (
        "```yaml\ngovernance:\n  owner: null\n  lifecycle_status: active\n  permission_tier: null\n"
        "  customer_data: null\n  writes_sor: null\n  created_at: null\n  updated_at: null\n```\n"
    )
    # Remove owner from the block to simulate a missing key.
    text_no_owner = text.replace("  owner: null\n", "")
    result = _edit_governance_block(text_no_owner, "owner", "x@example.com")
    # The absent key is inserted under the governance: root line.
    assert "owner:" in result
    assert "x@example.com" in result
    # Untargeted keys survive.
    assert "  lifecycle_status: active" in result
    # The inserted value re-parses to the exact input (round-trip).
    import yaml as _yaml

    block = re.search(r"```yaml\n(.*?)```", result, re.DOTALL).group(1)
    assert _yaml.safe_load(block)["governance"]["owner"] == "x@example.com"


def test_edit_block_insert_missing_key_on_partial_file():
    """A valid partial governance.md (owner + permission_tier only) is editable —
    the missing updated_at auto-stamp target is inserted, not a hard-fail (P1)."""
    partial = (
        "# Governance\n\n"
        "```yaml\n"
        "governance:\n"
        "  owner: alice@example.com\n"
        "  permission_tier: writes\n"
        "```\n"
    )
    result = _edit_governance_block(partial, "updated_at", "2026-07-06")
    import yaml as _yaml

    block = re.search(r"```yaml\n(.*?)```", result, re.DOTALL).group(1)
    parsed = _yaml.safe_load(block)["governance"]
    assert str(parsed["updated_at"]) == "2026-07-06"
    assert parsed["owner"] == "alice@example.com"


# ──────────────────────────────────────────────────────────────────
# end-to-end run_govern tests (via CLI entry point)


def test_govern_dry_run_writes_nothing(tmp_path):
    """--dry-run must write nothing and exit 0 (M5)."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], dry_run=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0
    assert gov_path.read_text() == before


def test_govern_dry_run_with_yes_still_writes_nothing(tmp_path):
    """--dry-run --yes must write nothing (dry-run takes precedence over --yes, M5)."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text()

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        dry_run=True,
        yes=True,
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0
    assert gov_path.read_text() == before


def test_govern_dry_run_no_audit_records(tmp_path):
    """--dry-run must NOT write audit records (M8 + P1 prep finding)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], dry_run=True
    )
    run_govern(args, tmp_path)

    # Check no log files created in either scope.
    agent_log = agent_dir / "log"
    fleet_log = tmp_path / "_manage" / "log"
    assert not agent_log.exists() or not any(agent_log.rglob("*.jsonl"))
    assert not fleet_log.exists() or not any(fleet_log.rglob("*.jsonl"))


def test_govern_applies_owner_set(tmp_path):
    """--set owner= round-trips through governance.md (M2/M3)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0

    new_content = (agent_dir / "governance.md").read_text()
    assert "alice@example.com" in new_content


def test_govern_preserves_prose_body(tmp_path):
    """M2(a): prose body outside the YAML block survives byte-for-byte.

    Asserts byte-equality on the pre-block and post-block segments rather than
    substring presence — substring checks pass even when whitespace mutates.
    """
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)
    original = (agent_dir / "governance.md").read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    result = (agent_dir / "governance.md").read_text()
    orig_match = next(
        m
        for m in re.finditer(r"```yaml\s*\n(.*?)```", original, re.DOTALL)
        if "governance:" in m.group(1)
    )
    result_match = next(
        m
        for m in re.finditer(r"```yaml\s*\n(.*?)```", result, re.DOTALL)
        if "governance:" in m.group(1)
    )
    # Pre-block and post-block prose must be byte-for-byte identical.
    assert result[: result_match.start()] == original[: orig_match.start()]
    assert result[result_match.end() :] == original[orig_match.end() :]


def test_edit_block_prose_byte_equality_negative_control():
    """P2-2 negative control: the byte-equality check detects any prose mutation.

    Artificially mutates the prose in the result and asserts the check catches it.
    This proves the == assertions in the M2(a) tests above are load-bearing: they
    would fail if _edit_governance_block (or run_govern) mutated prose whitespace.
    """
    original = _simple_gov_file()
    result = _edit_governance_block(original, "lifecycle_status", "paused")
    # Corrupt the prose in the result (trailing space on a header).
    mutated = result.replace("Prose body.", "Prose body. ")

    orig_match = next(
        m
        for m in re.finditer(r"```yaml\s*\n(.*?)```", original, re.DOTALL)
        if "governance:" in m.group(1)
    )
    mutated_match = next(
        m
        for m in re.finditer(r"```yaml\s*\n(.*?)```", mutated, re.DOTALL)
        if "governance:" in m.group(1)
    )
    # The byte-equality check MUST detect the prose corruption.
    # If this assertion were False the positive M2(a) == check would trivially pass
    # even on a corrupted result — meaning the guard would be inert.
    assert mutated[: mutated_match.start()] != original[: orig_match.start()]


def test_govern_preserves_inline_comments(tmp_path):
    """M2(b): inline comments in the YAML block survive byte-for-byte."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    new_content = (agent_dir / "governance.md").read_text()
    # The 'e.g.' inline comments from the template must survive.
    assert "# e.g." in new_content


def test_govern_auto_stamps_updated_at(tmp_path):
    """updated_at is auto-stamped to today's date on every applied write (P2 prep finding)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    new_content = (agent_dir / "governance.md").read_text()
    today = _today_iso()
    assert today in new_content, f"updated_at={today!r} not found in governance.md"


def test_govern_hyphen_maps_to_underscore(tmp_path):
    """CLI hyphen form (permission-tier) must write the underscore schema key."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["permission-tier=writes"], yes=True
    )
    run_govern(args, tmp_path)

    new_content = (agent_dir / "governance.md").read_text()
    # The writes value must appear on the permission_tier line.
    assert "permission_tier: writes" in new_content


def test_govern_writes_sor_hyphen(tmp_path):
    """writes-sor (CLI) -> writes_sor (schema key) mapping.

    'yes' is a YAML 1.1 boolean, so it must be written quoted ("yes") to avoid
    silent bool coercion on next read. GovernanceRecord.from_dict() handles both
    forms via the bool-to-tristate coercion (types.py lines 226-229).
    """
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(agent_dir.name, tmp_path, set_fields=["writes-sor=yes"], yes=True)
    run_govern(args, tmp_path)

    new_content = (agent_dir / "governance.md").read_text()
    # 'yes' is a YAML 1.1 bool, so it is single-quote-escaped to avoid coercion.
    assert "writes_sor: 'yes'" in new_content


def test_govern_creates_absent_governance_md(tmp_path):
    """When governance.md is absent, create it from the canonical template (M3)."""
    agent_dir = _make_agent_dir(tmp_path)
    # governance.md is absent — do NOT create it.
    assert not (agent_dir / "governance.md").exists()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0

    gov_path = agent_dir / "governance.md"
    assert gov_path.exists()
    content = gov_path.read_text()
    assert "alice@example.com" in content
    # governance: YAML block must be present.
    assert "governance:" in content


def test_govern_does_not_clobber_existing_governance_md(tmp_path):
    """The create-absent path MUST NOT overwrite an existing governance.md (M3)."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    # Manually set a field to confirm it is preserved.
    original_text = gov_path.read_text()
    # Replace owner to a known value.
    original_text = original_text.replace(
        "  owner: null", '  owner: "preserved@example.com"'
    )
    gov_path.write_text(original_text)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["lifecycle-status=paused"], yes=True
    )
    run_govern(args, tmp_path)

    new_content = gov_path.read_text()
    # The pre-existing owner value must survive.
    assert "preserved@example.com" in new_content


# ──────────────────────────────────────────────────────────────────
# Snapshot (M3)


def test_govern_takes_snapshot_before_write(tmp_path):
    """M3: snapshot file exists at .config-snapshots/govern/ after an applied write."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    snapshots_dir = agent_dir / ".config-snapshots" / "govern"
    assert snapshots_dir.exists(), "snapshot directory must be created"
    snap_files = list(snapshots_dir.glob("*.md"))
    assert len(snap_files) == 1, f"expected 1 snapshot file, found {snap_files}"


def test_govern_snapshot_contains_prior_content(tmp_path):
    """M3: snapshot contains the PRE-write file content (rollback source)."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    prior_content = gov_path.read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    snap_file = next((agent_dir / ".config-snapshots" / "govern").glob("*.md"))
    assert snap_file.read_text() == prior_content


def test_govern_snapshot_does_not_overlap_with_memory_versions(tmp_path):
    """M3: snapshot dir (.config-snapshots/) must be separate from memory .versions/."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    snapshots_dir = agent_dir / ".config-snapshots"
    versions_dir = agent_dir / "memory" / ".versions"
    # Ensure snapshot dir does not reside inside .versions/.
    try:
        snapshots_dir.relative_to(versions_dir)
        pytest.fail(".config-snapshots/ must not be inside memory/.versions/")
    except ValueError:
        pass  # correct — they are separate


def test_govern_snapshot_uuid_prevents_collision(tmp_path):
    """Two concurrent govern writes produce distinct snapshot files (UUID suffix)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args1 = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    args2 = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=bob@example.com"], yes=True
    )
    run_govern(args1, tmp_path)
    run_govern(args2, tmp_path)

    snap_files = list((agent_dir / ".config-snapshots" / "govern").glob("*.md"))
    assert len(snap_files) == 2
    assert snap_files[0].name != snap_files[1].name


def test_govern_snapshot_not_in_memory_note_list(tmp_path):
    """The .config-snapshots/ dir must be invisible to memory note recall."""
    agent_dir = _make_agent_dir(tmp_path)
    # Create a .config-snapshots dir with a file.
    snap_dir = agent_dir / ".config-snapshots" / "govern"
    snap_dir.mkdir(parents=True)
    (snap_dir / "snap.md").write_text("snapshot")

    # The dot-prefix must prevent the memory backend from listing it.
    # We verify by checking the path starts with '.' (dot-hidden convention).
    assert ".config-snapshots" not in [
        d.name for d in agent_dir.iterdir() if not d.name.startswith(".")
    ]


# ──────────────────────────────────────────────────────────────────
# Audit (M8)


def _collect_jsonl(log_dir: Path) -> list[dict]:
    """Collect all JSONL records from a log directory."""
    records = []
    for jsonl_file in log_dir.rglob("*.jsonl"):
        for line in jsonl_file.read_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def test_govern_audit_appends_to_per_agent_log(tmp_path):
    """M8: an applied write writes a PRIMITIVE_MANAGE_GOVERN record to the per-agent log."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(agent_dir / "log")
    assert len(records) == 1
    assert records[0]["primitive"] == PRIMITIVE_MANAGE_GOVERN
    assert records[0]["status"] == "applied"


def test_govern_audit_appends_to_fleet_log(tmp_path):
    """M8: an applied write writes a PRIMITIVE_MANAGE_GOVERN record to agents_root/_manage/log."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    fleet_log_dir = tmp_path / "_manage" / "log"
    records = _collect_jsonl(fleet_log_dir)
    assert len(records) == 1
    assert records[0]["primitive"] == PRIMITIVE_MANAGE_GOVERN


def test_govern_audit_backend_construction_failure_is_nonfatal(
    tmp_path, monkeypatch, capsys
):
    """P1: a swapped/misconfigured LogBackend that fails at CONSTRUCTION (not just
    .append()) must degrade to a non-fatal audit-drop warning — the write already
    landed, so the verb exits 0 with a surfaced warning, never a traceback (M8)."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)

    # An unknown backend id makes get_default_log_backend raise BackendNotRegistered
    # at construction, BEFORE any .append() — the exact leak the fix closes.
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "totally-bogus-backend")

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 0, "audit-drop must not fail the verb (M8)"
    # The governance.md edit landed on disk despite the audit drop.
    assert "alice@example.com" in gov_path.read_text(encoding="utf-8")
    # A non-fatal warning was surfaced (never silent).
    err = capsys.readouterr().err
    assert "could not be constructed" in err


def test_govern_audit_backend_construction_failure_json_exit0(
    tmp_path, monkeypatch, capsys
):
    """P1 + S3: under --json a construction failure must still emit clean JSON on
    stdout with audit_status='warn' and exit 0 — not a stderr traceback with no JSON."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "totally-bogus-backend")

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        yes=True,
        use_json=True,
    )
    rc = run_govern(args, tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # stdout must be parseable JSON, not a crash
    assert payload["ok"] is True
    assert payload["audit_status"] == "warn"


def test_physical_store_key_reference_attr_names_exist(tmp_path):
    """Drift guard: the shared-store detection duck-types on three PRIVATE attrs of
    the reference LogBackends. If any is renamed, _physical_store_key silently
    degrades to append-once (a silent M8 fleet-copy drop), so pin the names here so
    a rename fails LOUD in tests rather than at runtime.

    _physical_store_key must key each reference backend so distinct stores are
    detected — assert on the produced key, not just the attribute presence."""
    import inspect

    from atomic_agents.logs.filesystem import FilesystemLogBackend
    from atomic_agents.logs.sqlite import SQLiteLogBackend
    from atomic_agents.manage._routine import _physical_store_key

    fs = FilesystemLogBackend(tmp_path / "fslog")
    assert hasattr(fs, "_scope_root"), "FilesystemLogBackend lost _scope_root"
    assert _physical_store_key(fs) is not None and _physical_store_key(fs).startswith(
        "fs:"
    )

    sq = SQLiteLogBackend(":memory:")
    assert hasattr(sq, "_db_path_str"), "SQLiteLogBackend lost _db_path_str"
    assert _physical_store_key(sq) is not None

    # Postgres construction needs a live DSN; assert the attribute is still set in
    # __init__ source without constructing (the class does ``self._safe_url = ...``).
    from atomic_agents.logs.postgres import PostgresLogBackend

    assert "self._safe_url" in inspect.getsource(PostgresLogBackend.__init__), (
        "PostgresLogBackend lost _safe_url — shared-store detection would break"
    )


def test_govern_audit_run_id_is_uuid4(tmp_path):
    """M8 / P0 prep finding: the audit RunRecord must carry a valid UUID v4 run_id."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(agent_dir / "log")
    run_id = records[0]["run_id"]
    assert run_id, "run_id must be non-empty"
    # Validate UUID v4 format.
    parsed = uuid.UUID(run_id)
    assert parsed.version == 4


def test_govern_audit_agent_name_set(tmp_path):
    """M8: audit record must carry agent_name for fleet LogQuery(agent_name=...) queries."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(tmp_path / "_manage" / "log")
    assert records[0]["agent_name"] == agent_dir.name


def test_govern_audit_record_is_json_serialisable(tmp_path):
    """P0 prep finding: the audit RunRecord.to_dict() must JSON-serialise without TypeError."""
    agent_dir = _make_agent_dir(tmp_path)
    # Write a governance.md with an unquoted date to trigger PyYAML date coercion.
    gov_text = _canonical_governance_md(agent_dir.name)
    gov_text = gov_text.replace("  created_at: null", "  created_at: 2026-06-24")
    (agent_dir / "governance.md").write_text(gov_text)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    # Check the JSONL line is parseable (no serialisation errors).
    records = _collect_jsonl(agent_dir / "log")
    assert len(records) == 1
    # The line must be valid JSON.
    raw_lines = []
    for jsonl_file in (agent_dir / "log").rglob("*.jsonl"):
        raw_lines.extend(jsonl_file.read_text().splitlines())
    assert all(json.loads(line) is not None for line in raw_lines if line.strip())


def test_govern_audit_no_cost_usd_key(tmp_path):
    """P2 prep finding: management records must omit cost_usd (not cost_usd=0.0)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(agent_dir / "log")
    assert "cost_usd" not in records[0], "management records must NOT carry cost_usd"
    assert "critical" not in records[0], "management records must NOT carry critical"


def test_govern_audit_extra_before_after(tmp_path):
    """M8: audit extra{} carries before/after for the changed fields."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(agent_dir / "log")
    r = records[0]
    assert "changed_fields" in r
    assert "owner" in r["changed_fields"]
    assert "before" in r
    assert "after" in r
    assert r["after"]["owner"] == "alice@example.com"


def test_govern_audit_drop_is_non_fatal(tmp_path, capsys):
    """M8: a LogBackend.append() failure warns and exits 0 (write already succeeded)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    # Stub append to raise for the per-agent backend only.
    from atomic_agents.logs import FilesystemLogBackend

    def _failing_append(self, record):
        raise OSError("simulated append failure")

    with patch.object(FilesystemLogBackend, "append", _failing_append):
        args = _make_args(
            agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
        )
        exit_code = run_govern(args, tmp_path)

    # Write succeeded despite audit failure.
    assert exit_code == 0
    content = (agent_dir / "governance.md").read_text()
    assert "alice@example.com" in content

    # Warning must be on stderr.
    captured = capsys.readouterr()
    assert "Warning" in captured.err or "audit" in captured.err.lower()


def test_govern_audit_per_agent_and_fleet_same_run_id(tmp_path):
    """M8: the same RunRecord (same run_id) must appear in BOTH log scopes."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    per_agent_records = _collect_jsonl(agent_dir / "log")
    fleet_records = _collect_jsonl(tmp_path / "_manage" / "log")

    assert len(per_agent_records) == 1
    assert len(fleet_records) == 1
    assert per_agent_records[0]["run_id"] == fleet_records[0]["run_id"]


# ──────────────────────────────────────────────────────────────────
# Validation failures (M4) — no write on any failure


def test_govern_unknown_field_no_write(tmp_path):
    """M4: unknown field refuses before write and exits non-zero."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["not-a-field=value"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0
    assert gov_path.read_text() == before


def test_govern_invalid_enum_no_write(tmp_path):
    """M4: invalid enum value refuses before write and exits non-zero."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["permission-tier=super-admin"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0
    assert gov_path.read_text() == before


def test_govern_nested_path_refused_no_write(tmp_path):
    """dotted paths (review.reviewer) return the 'edit directly' refusal, not 'unknown field'."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["review.reviewer=Alice"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0
    assert gov_path.read_text() == before


def test_govern_nested_path_refused_no_audit_record(tmp_path):
    """M4 refusal must not emit an audit record (no write happened)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["review.reviewer=Alice"], yes=True
    )
    run_govern(args, tmp_path)

    per_agent_log = agent_dir / "log"
    fleet_log = tmp_path / "_manage" / "log"
    assert not per_agent_log.exists() or not any(per_agent_log.rglob("*.jsonl"))
    assert not fleet_log.exists() or not any(fleet_log.rglob("*.jsonl"))


def test_govern_invalid_enum_no_audit_record(tmp_path):
    """M4 refusal must not emit an audit record."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["permission-tier=invalid"], yes=True
    )
    run_govern(args, tmp_path)

    assert not (agent_dir / "log").exists() or not any(
        (agent_dir / "log").rglob("*.jsonl")
    )


# ──────────────────────────────────────────────────────────────────
# PRESENT_INVALID guard (P1 prep finding)


def test_govern_present_invalid_refuses_set(tmp_path):
    """When governance.md has parse_errors, --set is refused before write."""
    agent_dir = _make_agent_dir(tmp_path)
    # Write a governance.md with an invalid enum to create PRESENT_INVALID state.
    invalid_gov = _canonical_governance_md(agent_dir.name)
    invalid_gov = invalid_gov.replace(
        "  permission_tier: null", "  permission_tier: super-admin"
    )
    (agent_dir / "governance.md").write_text(invalid_gov)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0


def test_govern_present_invalid_no_write(tmp_path):
    """PRESENT_INVALID guard must not write anything."""
    agent_dir = _make_agent_dir(tmp_path)
    invalid_gov = _canonical_governance_md(agent_dir.name)
    invalid_gov = invalid_gov.replace(
        "  permission_tier: null", "  permission_tier: super-admin"
    )
    gov_path = agent_dir / "governance.md"
    gov_path.write_text(invalid_gov)
    before = gov_path.read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    assert gov_path.read_text() == before


# ──────────────────────────────────────────────────────────────────
# Non-TTY requires --yes (M5 / spec/55 P1 prep finding)


def test_govern_non_tty_without_yes_refuses(tmp_path, monkeypatch):
    """Non-TTY without --yes must exit non-zero and write nothing."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=False
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0
    assert gov_path.read_text() == before


# ──────────────────────────────────────────────────────────────────
# --json structured output (M6 / S3)


def test_govern_json_success_output(tmp_path, capsys):
    """--json emits {ok: true, agent, changes, snapshot_path, audit_status} on success."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        yes=True,
        use_json=True,
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["agent"] == agent_dir.name
    assert "changes" in payload
    assert "snapshot_path" in payload
    assert "audit_status" in payload


def test_govern_json_unknown_field_refusal(tmp_path, capsys):
    """--json emits {ok: false, error_type, reason} on unknown-field refusal."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["not-a-field=value"],
        yes=True,
        use_json=True,
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error_type"] == "unknown_field"
    assert "reason" in payload


def test_govern_json_invalid_enum_refusal(tmp_path, capsys):
    """--json emits {ok: false, error_type: 'invalid_enum', reason} on enum refusal."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["permission-tier=godmode"],
        yes=True,
        use_json=True,
    )
    run_govern(args, tmp_path)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_enum"


def test_govern_json_nested_path_refusal(tmp_path, capsys):
    """--json emits {ok: false, error_type: 'nested_path_refused'} for dotted paths."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["review.reviewer=Alice"],
        yes=True,
        use_json=True,
    )
    run_govern(args, tmp_path)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error_type"] == "nested_path_refused"


def test_govern_json_dry_run_output(tmp_path, capsys):
    """--dry-run --json emits {ok: true, dry_run: true, changes}."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        dry_run=True,
        use_json=True,
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert "changes" in payload


def test_govern_show_json(tmp_path, capsys):
    """--show --json emits governance state with has_governance + governance_state."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(agent_dir.name, tmp_path, show=True, use_json=True)
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert "has_governance" in payload
    assert "governance_state" in payload


# ──────────────────────────────────────────────────────────────────
# --json abort emits structured refusal (P2-4 / S3 contract)


def test_govern_json_abort_n_emits_structured_refusal(tmp_path, capsys, monkeypatch):
    """S3 / P2-4: --json + 'n' answer emits {ok:false, error_type:'aborted'} to stdout.

    Before the fix, the abort path printed 'Aborted.' to stderr and returned 0
    with empty stdout — violating the S3 contract that --json stdout is ALWAYS
    machine-readable.
    """
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    # Simulate a TTY so the interactive prompt fires, then answer 'n'.
    # yes=False so the confirm gate is reached (default is True, which bypasses it).
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        use_json=True,
        yes=False,
    )
    exit_code = run_govern(args, tmp_path)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "aborted"


def test_govern_json_abort_eof_emits_structured_refusal(tmp_path, capsys, monkeypatch):
    """S3 / P2-4: --json + EOF emits {ok:false, error_type:'aborted'} to stdout.

    Before the fix, the EOFError branch printed to stderr and returned 0 with
    empty stdout — same S3 contract hole as the 'n' branch.
    """
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    # yes=False so the confirm gate is reached.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _raise_eof():
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        use_json=True,
        yes=False,
    )
    exit_code = run_govern(args, tmp_path)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "aborted"


# ──────────────────────────────────────────────────────────────────
# Path traversal guard (P1 prep finding)


def test_govern_path_traversal_agent_name_refused(tmp_path):
    """A crafted agent name (../escape) must be refused before any write."""
    args = _make_args("../escape", tmp_path, set_fields=["owner=x"], yes=True)
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0


# ──────────────────────────────────────────────────────────────────
# Fleet log scope: _manage dir is underscore-prefixed


def test_manage_dir_skipped_by_registry(tmp_path):
    """The '_manage' fleet log dir must not appear as an agent in list_agents()."""
    from atomic_agents.agent_registry import FilesystemAgentRegistryBackend

    agent_dir = _make_agent_dir(tmp_path, "real-agent")
    _make_governance_md(agent_dir)

    # Simulate a fleet log write creating _manage/.
    manage_log = tmp_path / "_manage" / "log"
    manage_log.mkdir(parents=True)
    (manage_log / "2026-07").mkdir()
    (manage_log / "2026-07" / "2026-07-01.jsonl").write_text(
        '{"ts":"t","run_id":"x"}\n'
    )

    backend = FilesystemAgentRegistryBackend(tmp_path)
    agents = backend.list_agents()
    agent_ids = [a.id for a in agents]
    assert "_manage" not in agent_ids
    assert "real-agent" in agent_ids


# ──────────────────────────────────────────────────────────────────
# M9 composition gates: empty for govern (documented intentional no-op)


def test_govern_composition_gates_empty_set(tmp_path):
    """M9: govern's composition set is EMPTY by design — every governance field is
    descriptive metadata with no runtime-rejection counterpart. The step 1
    composition check resolves to enum/format validation only (zero extra gates).

    This test asserts that setting any governance field does NOT trigger a
    PRICING / caps / policy composition check (those are set-model's job)."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    # Setting all flat scalars must succeed without PRICING / policy checks.
    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=[
            "owner=alice@example.com",
            "backup-owner=bob@example.com",
            "permission-tier=writes",
            "customer-data=yes",
            "writes-sor=no",
            "lifecycle-status=active",
        ],
        yes=True,
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0


# ──────────────────────────────────────────────────────────────────
# S1: unknown agent returns non-zero


def test_govern_unknown_agent_returns_nonzero(tmp_path):
    """S1/M7: if the registry cannot find the agent, exit non-zero."""
    # Empty agents_root — no agents registered.
    args = _make_args("nonexistent-agent", tmp_path, set_fields=["owner=x"], yes=True)
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0


# ──────────────────────────────────────────────────────────────────
# take_config_snapshot unit tests


def test_take_config_snapshot_creates_file(tmp_path):
    from atomic_agents.manage._routine import take_config_snapshot

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    content = "prior governance content"

    snap_path = take_config_snapshot(agent_dir, content)
    assert snap_path.exists()
    assert snap_path.read_text() == content


def test_take_config_snapshot_path_pattern(tmp_path):
    from atomic_agents.manage._routine import take_config_snapshot

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    snap_path = take_config_snapshot(agent_dir, "content")

    # Must be under .config-snapshots/govern/.
    assert ".config-snapshots" in str(snap_path)
    assert "govern" in str(snap_path)
    assert snap_path.suffix == ".md"


def test_take_config_snapshot_uuid_in_filename(tmp_path):
    """Snapshot filenames include a UUID hex suffix to prevent collision."""
    from atomic_agents.manage._routine import take_config_snapshot

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    snap1 = take_config_snapshot(agent_dir, "content1")
    snap2 = take_config_snapshot(agent_dir, "content2")

    assert snap1 != snap2


# ──────────────────────────────────────────────────────────────────
# RESERVED_AGENT_NAMES (P2 prep finding)


def test_manage_and_govern_in_reserved_names():
    from atomic_agents.init.constants import RESERVED_AGENT_NAMES

    assert "manage" in RESERVED_AGENT_NAMES
    assert "govern" in RESERVED_AGENT_NAMES


# ──────────────────────────────────────────────────────────────────
# YAML scalar serialization round-trip (P1 shortcut — corrupting write class)


def _reparse_owner(text: str):
    import yaml as _yaml

    block = re.search(r"```yaml\n(.*?)```", text, re.DOTALL).group(1)
    return _yaml.safe_load(block)["governance"]["owner"]


@pytest.mark.parametrize(
    "value",
    [
        'Jane "JD" Doe',  # embedded double-quotes
        r"C:\Program Files\bot",  # backslashes (double-quote emission would eat these)
        "[test]",  # flow indicator — bare would reparse as a list
        "a 'b' c",  # embedded single-quotes (must be doubled)
        "123",  # number-shaped — bare would reparse as int
        "value: with, colon # hash",  # colon + comma + hash
        "café-∆",  # non-ASCII
        # YAML 1.1 non-decimal int spellings — plain-safe by charset but PyYAML
        # would coerce a BARE token to int on re-read. The authoritative
        # safe_load round-trip in _needs_quoting must quote these.
        "0x10",  # hex int -> 16 if emitted bare
        "0xFF",  # hex int -> 255
        "0b101",  # binary int -> 5
        "12_000",  # underscore-grouped int -> 12000
        "1_0",  # underscore int -> 10
        "on",  # YAML 1.1 bool -> True
        "off",  # YAML 1.1 bool -> False
    ],
)
def test_edit_block_value_round_trips_exact(value):
    """Per-invocation control: each special-char value must round-trip byte-exact.

    A naive double-quote wrap corrupts backslashes / embedded quotes and mis-types
    bare brackets/numbers; a hand-rolled number regex misses hex/binary/underscore
    int spellings. The authoritative-quoting emission must round-trip all."""
    text = _simple_gov_file()
    result = _edit_governance_block(text, "owner", value)
    assert _reparse_owner(result) == value


def test_edit_block_bare_enum_stays_unquoted():
    """A conservatively-plain-safe value (enum) is emitted BARE, not quoted."""
    text = _simple_gov_file()
    result = _edit_governance_block(text, "permission_tier", "writes")
    assert "  permission_tier: writes" in result  # bare, no quotes


def _reparse_field(text: str, field: str):
    import yaml as _yaml

    block = re.search(r"```yaml\n(.*?)```", text, re.DOTALL).group(1)
    return _yaml.safe_load(block)["governance"][field]


# ── Value/comment boundary controls (P1: empty-value line with inline comment;
#    P2: '#' glued to a value; P2: '#' inside a quoted scalar). A '#' is a YAML
#    comment ONLY when preceded by whitespace and outside quotes. ──────────────


def test_edit_block_empty_value_with_inline_comment_no_fusion():
    """P1: editing a `key:  # comment` line must NOT fuse the value to the comment.

    Prior corruption: `  owner:  # fill` + set owner=dan wrote `  owner:  dan# fill`,
    which reparses to 'dan# fill' (a '#' not preceded by whitespace is not a
    comment). The value must reparse to exactly 'dan'.
    """
    text = (
        "```yaml\ngovernance:\n"
        "  owner:  # fill in your email\n"
        "  permission_tier: null\n```\n"
    )
    result = _edit_governance_block(text, "owner", "dan")
    assert _reparse_field(result, "owner") == "dan"
    # The instructional comment must survive.
    assert "# fill in your email" in result


def test_edit_block_empty_value_enum_with_comment_stays_present_valid():
    """P1 enum variant: `permission_tier:  # set me` + set writes -> valid record."""
    import yaml as _yaml

    text = (
        "```yaml\ngovernance:\n"
        "  owner: null\n"
        "  permission_tier:  # set me\n"
        "  customer_data: null\n"
        "  writes_sor: null\n"
        "  lifecycle_status: active\n"
        "  created_at: null\n"
        "  updated_at: null\n```\n"
    )
    result = _edit_governance_block(text, "permission_tier", "writes")
    assert _reparse_field(result, "permission_tier") == "writes"
    block = re.search(r"```yaml\n(.*?)```", result, re.DOTALL).group(1)
    rec = GovernanceRecord.from_dict(_yaml.safe_load(block)["governance"])
    assert rec.permission_tier == "writes"
    assert not rec.parse_errors


def test_edit_block_hash_glued_to_prior_value_writes_clean():
    """P2: a '#' glued to the PRIOR value (no whitespace) is not a comment.

    `  owner: alice#1` + set owner=bob must write exactly 'bob', not 'bob#1'.
    """
    text = "```yaml\ngovernance:\n  owner: alice#1\n  permission_tier: null\n```\n"
    result = _edit_governance_block(text, "owner", "bob")
    assert _reparse_field(result, "owner") == "bob"
    assert "bob#1" not in result


def test_edit_block_quoted_scalar_with_inner_hash_no_leftover_junk():
    """P2: re-editing a single-quoted scalar whose value contains ' # ' leaves no junk.

    The R1 emitter writes any value containing '#' as a single-quoted scalar; a
    later --set on the same field must not mistake the in-scalar ' # ' for a
    trailing comment and leak a fragment into the line.
    """
    # Owner line carries NO trailing comment, so any '#' in the re-edited line
    # can only be leftover junk from the quoted value's inner ' # '.
    text = "```yaml\ngovernance:\n  owner: null\n  permission_tier: null\n```\n"
    # First write produces a single-quoted scalar (value contains ' # ').
    first = _edit_governance_block(text, "owner", "a # b")
    assert _reparse_field(first, "owner") == "a # b"
    # Re-edit the same field; the new value must be clean, no leftover '# b'.
    second = _edit_governance_block(first, "owner", "carol")
    assert _reparse_field(second, "owner") == "carol"
    owner_line = next(
        ln for ln in second.splitlines() if ln.strip().startswith("owner:")
    )
    assert "#" not in owner_line  # no leftover comment fragment on the value line


# ── List-mutation grammar pinning (P1: --add / --remove / --set-json are
#    RECOGNISED by the parser but return a clean structured refusal in PR1, never
#    an argparse `unrecognized arguments` exit-2 whose meaning shifts later). ────


@pytest.mark.parametrize(
    "field,value",
    [
        ("add", "sources.primary=x"),
        ("remove", "sources.primary=x"),
        ("set_json", "sources.primary=[]"),
    ],
)
def test_run_govern_list_mutation_refused_structured(tmp_path, capsys, field, value):
    """--add / --remove / --set-json each refuse cleanly (exit 1) with a --json body."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    kwargs = {field: [value]}
    args = _make_args(agent_dir.name, tmp_path, use_json=True, **kwargs)
    exit_code = run_govern(args, tmp_path)

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error_type"] == "list_mutation_unsupported"
    assert "edit governance.md directly" in out["reason"]


def test_cli_set_json_flag_is_recognised_not_argparse_error(tmp_path, capsys):
    """P1: `manage govern ... --set-json ... --json` exits 1 with structured JSON.

    If the flag were unregistered, argparse would exit 2 with a usage message on
    stderr — the exact 'parser error whose meaning shifts in a later PR' the spec
    forbids. Recognised-and-refused is the pinned contract.
    """
    from atomic_agents.cli import main

    exit_code = main(
        [
            "manage",
            "govern",
            "someagent",
            "--set-json",
            "sources.primary=[]",
            "--json",
            "--agents-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 1  # NOT 2 (argparse usage error)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error_type"] == "list_mutation_unsupported"


def test_cli_add_and_remove_flags_are_recognised(tmp_path):
    """P1: --add and --remove parse (exit 1 refusal), not argparse exit 2."""
    from atomic_agents.cli import main

    for flag in ("--add", "--remove"):
        code = main(
            [
                "manage",
                "govern",
                "someagent",
                flag,
                "sources.primary=x",
                "--agents-root",
                str(tmp_path),
            ]
        )
        assert code == 1


def test_govern_value_round_trips_through_run_govern(tmp_path):
    """End-to-end: a tricky owner value written by run_govern reparses exact."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    tricky = 'A\\B "Q" [x]'
    args = _make_args(
        agent_dir.name, tmp_path, set_fields=[f"owner={tricky}"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0

    content = (agent_dir / "governance.md").read_text()
    assert _reparse_owner(content) == tricky
    # The written file must still be a valid (present_valid) governance record.
    import yaml as _yaml

    block = re.search(r"```yaml\n(.*?)```", content, re.DOTALL).group(1)
    rec = GovernanceRecord.from_dict(_yaml.safe_load(block)["governance"])
    assert rec.owner == tricky


# ──────────────────────────────────────────────────────────────────
# Date validation (M4 / conformance outline malformed-date control)


def test_govern_malformed_date_refused_no_write(tmp_path):
    """M4: a malformed date for created_at refuses before write, exit non-zero."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["created-at=garbage"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0
    assert gov_path.read_text() == before


def test_govern_malformed_date_json_error_type(tmp_path, capsys):
    """M6: malformed date emits {ok: false, error_type: 'invalid_date'}."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["updated-at=2026-13-99"],  # regex-shaped but not a real date
        yes=True,
        use_json=True,
    )
    run_govern(args, tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_date"


def test_govern_valid_iso_date_accepted(tmp_path):
    """A well-formed ISO date for created_at is accepted and written."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["created-at=2026-06-24"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0
    assert "2026-06-24" in (agent_dir / "governance.md").read_text()


# ──────────────────────────────────────────────────────────────────
# Missing-key auto-stamp on a partial file (P1 — end-to-end)


def test_govern_edits_partial_file_missing_updated_at(tmp_path):
    """P1: a valid partial governance.md lacking updated_at is editable — the
    auto-stamp inserts it rather than hard-failing the operator's --set owner."""
    agent_dir = _make_agent_dir(tmp_path)
    partial = (
        "# Governance\n\nProse.\n\n"
        "```yaml\n"
        "governance:\n"
        "  owner: null\n"
        "  permission_tier: read-only\n"
        "```\n"
    )
    (agent_dir / "governance.md").write_text(partial)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0

    content = (agent_dir / "governance.md").read_text()
    assert _reparse_owner(content) == "alice@example.com"
    # updated_at was auto-stamped (inserted) even though it was absent.
    import yaml as _yaml

    block = re.search(r"```yaml\n(.*?)```", content, re.DOTALL).group(1)
    assert _yaml.safe_load(block)["governance"].get("updated_at") is not None


# ──────────────────────────────────────────────────────────────────
# Snapshot ordering: no orphan snapshot on a doomed edit (P2)


def test_govern_no_orphan_snapshot_on_edit_failure(tmp_path):
    """A doomed edit (no governance block) must NOT leave an orphan snapshot."""
    agent_dir = _make_agent_dir(tmp_path)
    # governance.md exists but has NO yaml governance block.
    (agent_dir / "governance.md").write_text("# Governance\n\nno yaml block here\n")

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code != 0
    # No snapshot should have been written (edit computed before snapshot).
    snap_dir = agent_dir / ".config-snapshots" / "govern"
    assert not snap_dir.exists() or not list(snap_dir.glob("*.md"))


def test_govern_dry_run_no_block_returns_edit_error(tmp_path, capsys):
    """P2-3 negative control: --dry-run on a PRESENT_NO_BLOCK file returns edit_error.

    Before the fix, --dry-run reported {ok:true, changes:[...]} then apply
    refused with edit_error — a preview-as-artifact break (M6/M7). The fix
    computes the edit before the dry-run exit so both paths agree.
    """
    agent_dir = _make_agent_dir(tmp_path)
    (agent_dir / "governance.md").write_text("# Governance\n\nNo yaml block here.\n")

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        dry_run=True,
        use_json=True,
    )
    exit_code = run_govern(args, tmp_path)

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "edit_error"


def test_govern_dry_run_duplicate_key_returns_edit_error(tmp_path, capsys):
    """P2-3 negative control: --dry-run on a duplicate-key file returns edit_error.

    Same preview-as-artifact break: a duplicate-key file's --dry-run must refuse
    consistently with what apply returns rather than showing a fake success.
    """
    agent_dir = _make_agent_dir(tmp_path)
    dup = (
        "# Governance\n\n"
        "```yaml\n"
        "governance:\n"
        "  owner: first@example.com\n"
        "  owner: second@example.com\n"
        "```\n"
    )
    (agent_dir / "governance.md").write_text(dup)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=new@example.com"],
        dry_run=True,
        use_json=True,
    )
    exit_code = run_govern(args, tmp_path)

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "edit_error"


def test_govern_create_absent_takes_no_snapshot(tmp_path):
    """create-absent write has no prior file, so it takes NO snapshot (M3)."""
    agent_dir = _make_agent_dir(tmp_path)
    assert not (agent_dir / "governance.md").exists()

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0
    assert (agent_dir / "governance.md").exists()
    # No snapshot directory for the created file.
    snap_dir = agent_dir / ".config-snapshots" / "govern"
    assert not snap_dir.exists() or not list(snap_dir.glob("*.md"))


def test_govern_create_absent_audit_marks_created(tmp_path):
    """create-absent audit record carries created=True and null snapshot_path."""
    agent_dir = _make_agent_dir(tmp_path)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(agent_dir / "log")
    assert records[0]["created"] is True
    assert records[0]["snapshot_path"] is None


# ──────────────────────────────────────────────────────────────────
# Redaction at the PERSISTED echo site (M8 — the pinned shape later verbs inherit)


def test_govern_audit_redacts_secret_shaped_field(tmp_path, monkeypatch):
    """M8 negative control: a secret-shaped field name must be <redacted> in the
    persisted JSONL before/after AND the --json changes — not just the console."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    # Make 'owner' secret-shaped for this invocation (govern has no real secret
    # field; this proves the persisted echo path redacts for later verbs).
    from atomic_agents.manage import govern as govern_mod

    monkeypatch.setattr(govern_mod, "_SECRET_SUBSTRINGS", ("owner",))

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=topsecret@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(agent_dir / "log")
    r = records[0]
    assert r["after"]["owner"] == "<redacted>"
    assert r["before"]["owner"] == "<redacted>"
    # The raw value must NOT appear anywhere in the persisted line.
    for jsonl_file in (agent_dir / "log").rglob("*.jsonl"):
        assert "topsecret@example.com" not in jsonl_file.read_text()


def test_govern_audit_not_redacted_without_secret_shape(tmp_path):
    """Strip control: without the secret-shape match, the real value is stored."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    records = _collect_jsonl(agent_dir / "log")
    assert records[0]["after"]["owner"] == "alice@example.com"


# ──────────────────────────────────────────────────────────────────
# Control-character refusal (round-3 P2): single-quoted YAML folds a
# newline into a space on re-read, so a value with a line break would
# persist differently from what --json reports. Refuse, don't mangle.


def test_validate_field_value_control_char_raises():
    """Unit: a value with a newline is refused by the validator (all fields)."""
    with pytest.raises(ManageControlCharRefused):
        _validate_field_value("owner", "line1\nline2")


def test_validate_field_value_control_char_carriage_return_raises():
    """A carriage return is also a control character → refused."""
    with pytest.raises(ManageControlCharRefused):
        _validate_field_value("backup_owner", "a\rb")


def test_validate_field_value_nel_u0085_raises():
    """U+0085 NEL folds to a space on PyYAML re-read → refused (round-trip guard).

    NEL is outside the C0/DEL range a hand-rolled blocklist would catch, but PyYAML
    treats it as a line break and folds it — so the persisted value would diverge
    from the audit ``after`` (principle #5). The authoritative emit→reload round-trip
    guard catches it where an ``ord(ch) < 0x20 or == 0x7F`` blocklist would not.
    """
    with pytest.raises(ManageControlCharRefused):
        _validate_field_value("owner", "alice\x85bob")


def test_validate_field_value_nul_and_vtab_raise():
    """PyYAML-rejected control chars (NUL, vertical tab) also refuse via round-trip."""
    with pytest.raises(ManageControlCharRefused):
        _validate_field_value("owner", "a\x00b")
    with pytest.raises(ManageControlCharRefused):
        _validate_field_value("owner", "a\x0bb")


def test_validate_field_value_plain_free_text_ok():
    """Strip control: a plain single-line owner value passes (guard is load-bearing)."""
    # No exception.
    _validate_field_value("owner", "alice@example.com")


def test_govern_control_char_refused_no_write_no_audit(tmp_path):
    """M4: --set owner with an embedded newline refuses before write, exit 1, no audit."""
    agent_dir = _make_agent_dir(tmp_path)
    gov_path = _make_governance_md(agent_dir)
    before = gov_path.read_text(encoding="utf-8")

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=line1\nline2"], yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 1
    # governance.md is untouched.
    assert gov_path.read_text(encoding="utf-8") == before
    # No audit record was written (refused before the audit step).
    assert _collect_jsonl(agent_dir / "log") == []


def test_govern_control_char_refused_json_structured(tmp_path, capsys):
    """S3: the control-char refusal emits a parseable --json error, not a traceback."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=line1\nline2"],
        yes=True,
        use_json=True,
    )
    rc = run_govern(args, tmp_path)
    assert rc == 1

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error_type"] == "control_char"


# ──────────────────────────────────────────────────────────────────
# Duplicate-key refusal (round-3 P2): PyYAML keeps the LAST duplicate,
# the surgical editor rewrites the FIRST — a silent no-op-on-re-read
# with a lying audit trail. Refuse the ambiguous file.


def test_edit_block_duplicate_key_raises():
    """Unit: a governance block with a key duplicated at indent-2 is refused."""
    text = (
        "```yaml\n"
        "governance:\n"
        "  owner: first@example.com\n"
        "  owner: second@example.com\n"
        "```\n"
    )
    with pytest.raises(ValueError, match="Duplicate"):
        _edit_governance_block(text, "owner", "new@example.com")


def test_govern_duplicate_key_refused_no_write(tmp_path):
    """A hand-authored duplicate-key file is refused (edit_error), governance.md untouched."""
    agent_dir = _make_agent_dir(tmp_path)
    dup = (
        "# Governance\n\n"
        "```yaml\n"
        "governance:\n"
        "  owner: first@example.com\n"
        "  owner: second@example.com\n"
        "  permission_tier: null\n"
        "```\n"
    )
    gov_path = _make_governance_md(agent_dir, content=dup)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=new@example.com"], yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 1
    assert gov_path.read_text(encoding="utf-8") == dup
    assert _collect_jsonl(agent_dir / "log") == []


def test_edit_block_single_key_still_edits():
    """Strip control: a single (non-duplicated) key still edits (guard not over-broad)."""
    text = "```yaml\ngovernance:\n  owner: old@example.com\n```\n"
    out = _edit_governance_block(text, "owner", "new@example.com")
    assert "new@example.com" in out
    assert "old@example.com" not in out


# ──────────────────────────────────────────────────────────────────
# Unreadable-governance.md structured refusal (round-3 P2): a present-
# but-unreadable file must yield a clean --json refusal, not a traceback.


def test_govern_unreadable_governance_md_structured_refusal(tmp_path, capsys):
    """A read error on governance.md returns a structured refusal, not a traceback."""
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    # Force _read_or_create_governance to raise OSError. chmod 000 is not
    # reliable as root/CI, so patch the function itself. Patching Path.read_text
    # was the original approach but the read site now uses Path.open (newline=""
    # for CRLF fidelity — spec/55 M2); patching the helper directly is
    # implementation-independent and works regardless of the internal I/O call.
    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        yes=True,
        use_json=True,
    )

    from atomic_agents.manage import govern as _gov_module

    def _raise_perm(*a, **k):
        raise PermissionError("Permission denied")

    with patch.object(_gov_module, "_read_or_create_governance", _raise_perm):
        rc = run_govern(args, tmp_path)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "read_error"
    # No write happened.
    assert _collect_jsonl(agent_dir / "log") == []


# ──────────────────────────────────────────────────────────────────
# P1 conformance: under a SHARED-store LogBackend the two management
# scopes collapse to one table — exactly one row per run_id, no dup.


def test_govern_audit_single_row_under_shared_store(tmp_path, monkeypatch):
    """P1: a shared SQLite LogBackend must get exactly ONE row per run_id (no dup).

    Under ATOMIC_AGENTS_LOG_BACKEND=sqlite with a shared URL, get_default_log_backend
    ignores scope_root, so the per-agent scope and the fleet scope resolve to the
    SAME table. A naive dual-append would insert the same run_id twice and
    double-count every fleet aggregation. The backend-aware routine must collapse
    to a single append.
    """
    import sqlite3

    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    db_path = tmp_path / "shared-logs.db"
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "sqlite")
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND_URL", f"sqlite:///{db_path}")

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    rc = run_govern(args, tmp_path)
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT run_id, COUNT(*) FROM run_records "
            "WHERE primitive = ? GROUP BY run_id",
            (PRIMITIVE_MANAGE_GOVERN,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, f"expected one management run_id, got {rows}"
    assert rows[0][1] == 1, f"run_id {rows[0][0]} was written {rows[0][1]} times (dup)"


def test_govern_audit_distinct_stores_still_dual_write(tmp_path):
    """Strip control: the default Filesystem backend (distinct dirs) still dual-writes.

    This guards against the P1 fix over-collapsing — the two-copy fleet-survives-
    deletion guarantee must hold for physically distinct stores.
    """
    agent_dir = _make_agent_dir(tmp_path)
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    run_govern(args, tmp_path)

    per_agent = _collect_jsonl(agent_dir / "log")
    fleet = _collect_jsonl(tmp_path / "_manage" / "log")
    assert len(per_agent) == 1
    assert len(fleet) == 1
    # Same immutable event (identical run_id) in both distinct stores.
    assert per_agent[0]["run_id"] == fleet[0]["run_id"]


# ──────────────────────────────────────────────────────────────────
# CRLF line-terminator fidelity (P2-5 / M2 byte-fidelity on CRLF files)


def test_edit_block_crlf_targeted_line_preserves_terminator():
    """P2-5 unit: editing a CRLF-terminated targeted line keeps its \\r\\n.

    Without the \\r in _split_scalar_and_comment's rstrip set, the replacement
    line loses its \\r, producing a lone-LF line in an otherwise-CRLF block.
    """
    # Build a simple governance block with CRLF line endings.
    lf_text = "```yaml\r\ngovernance:\r\n  owner: null\r\n  lifecycle_status: active\r\n```\r\n"
    result = _edit_governance_block(lf_text, "owner", "alice@example.com")
    # The edited owner line must end with \r (i.e. be \r\n after join).
    owner_line = next(
        ln for ln in result.split("\n") if "owner:" in ln and "governance" not in ln
    )
    assert owner_line.endswith("\r"), f"CRLF lost on targeted line: {owner_line!r}"


def test_edit_block_crlf_insert_missing_key_preserves_terminator():
    """P2-5 unit: inserting an absent key into a CRLF block uses \\r\\n.

    Without the inserted-line terminator fix, the new line is LF-only among
    CRLF lines — breaking byte-fidelity for any downstream CRLF round-trip.
    """
    lf_text = "```yaml\r\ngovernance:\r\n  lifecycle_status: active\r\n```\r\n"
    result = _edit_governance_block(lf_text, "owner", "alice@example.com")
    inserted_line = next(ln for ln in result.split("\n") if "owner:" in ln)
    assert inserted_line.endswith("\r"), f"Inserted line lost CRLF: {inserted_line!r}"


def test_edit_block_crlf_full_round_trip():
    """P2-5 round-trip: _edit_governance_block preserves CRLF on all lines including prose.

    Exercises the function in isolation (no filesystem I/O). The end-to-end
    path through run_govern is covered by
    test_govern_crlf_file_round_trip_end_to_end, which writes a CRLF file to
    disk and asserts the output bytes on disk are fully CRLF.
    """
    # Full governance file with prose, block, and trailing prose — all CRLF.
    crlf_text = (
        "# Governance : test-agent\r\n"
        "\r\n"
        "Prose body.\r\n"
        "\r\n"
        "```yaml\r\n"
        "governance:\r\n"
        "  owner: null\r\n"
        "  lifecycle_status: active\r\n"
        "```\r\n"
        "\r\n"
        "## Forbidden actions\r\n"
        "\r\n"
        "Prose section.\r\n"
    )
    result = _edit_governance_block(crlf_text, "owner", "alice@example.com")
    # Every non-empty line must keep its \r.
    for line in result.split("\n")[:-1]:
        if line:
            assert line.endswith("\r"), f"CRLF lost on line: {line!r}"


# ──────────────────────────────────────────────────────────────────
# End-to-end CRLF / LF fidelity through run_govern (M2 byte-fidelity)
#
# These tests go through the FULL run_govern path (read → edit → write), not
# just the _edit_governance_block unit.  They are the load-bearing
# verification that the read site in _read_or_create_governance preserves
# \r\n.  If the read site reverts to Path.read_text() (universal-newline
# mode), CRLF is silently normalised to LF before the editor sees it and the
# per-line assertions below go RED.


def test_govern_crlf_file_round_trip_end_to_end(tmp_path):
    """M2 end-to-end: run_govern preserves CRLF on every output line of a
    CRLF-authored governance.md, including the targeted and auto-stamped lines.

    Negative control: if _read_or_create_governance uses Path.read_text()
    (which normalises \\r\\n → \\n), the CRLF block editor never sees \\r and
    produces an all-LF output file.  The lone-LF assertion below catches that.

    To exercise the negative control manually: replace the
    ``open(..., newline="")`` read in _read_or_create_governance with
    ``read_text(encoding="utf-8")`` and confirm this test fails.
    """
    agent_dir = _make_agent_dir(tmp_path)

    # Write a CRLF governance.md as raw bytes so Python's text-mode
    # universal-newline translation never touches the content at write time.
    # The file has all mandatory fields so the registry parses it as
    # PRESENT_VALID (no PRESENT_INVALID refusal before the edit runs).
    crlf_content = (
        "# Governance: myagent\r\n"
        "\r\n"
        "Prose body preserved byte-for-byte.\r\n"
        "\r\n"
        "```yaml\r\n"
        "governance:\r\n"
        "  owner: null\r\n"
        "  backup_owner: null\r\n"
        "  permission_tier: read-only\r\n"
        "  customer_data: no\r\n"
        "  writes_sor: no\r\n"
        "  lifecycle_status: active\r\n"
        "  created_at: 2026-01-01\r\n"
        "  updated_at: 2026-01-01\r\n"
        "```\r\n"
    )
    gov_path = agent_dir / "governance.md"
    gov_path.write_bytes(crlf_content.encode("utf-8"))

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        yes=True,
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0, "run_govern must exit 0 on a valid CRLF apply"

    # Read output as raw bytes; decode without any newline translation so the
    # assertion is against what is physically on disk.
    result_bytes = gov_path.read_bytes()
    result_text = result_bytes.decode("utf-8")

    # Every LF in the output must be preceded by \r — no lone-LF lines.
    import re as _re

    lone_lf = [m.start() for m in _re.finditer(b"(?<!\r)\n", result_bytes)]
    assert not lone_lf, (
        f"Lone LF at byte positions {lone_lf} — CRLF was normalised somewhere "
        "in the read→edit→write path.  Check _read_or_create_governance: it "
        "must use open(..., newline='') not Path.read_text()."
    )

    # The targeted owner line and the auto-stamped updated_at line must also
    # carry CRLF.  Split on \n; each line should end with \r.
    lines = result_text.split("\n")
    for line in lines[:-1]:  # skip the final empty element after the last \n
        assert line.endswith("\r"), f"CRLF lost on line: {line!r}"

    # Confirm the targeted field was actually updated.  The value is
    # single-quoted in the YAML output because '@' is not plain-scalar-safe
    # ("'alice@example.com'" not "alice@example.com"), so search for the
    # substring without quoting.
    assert any("alice@example.com" in ln for ln in lines), (
        "owner value was not updated — the surgical edit did not run"
    )

    # Confirm updated_at was auto-stamped (it was present in the original file
    # as '2026-01-01'; run_govern must have overwritten it with today's date).
    assert not any("updated_at: 2026-01-01" in ln for ln in lines), (
        "updated_at was not auto-stamped — the edit loop may have stalled"
    )


def test_govern_lf_file_round_trip_end_to_end(tmp_path):
    """M2 end-to-end mirror: run_govern produces no \\r in an LF-authored file.

    Confirms the CRLF-preservation fix does not inject carriage returns into
    files that were authored with LF line endings.  This is the LF negative
    control: if the fix incorrectly added \\r to LF content this test goes RED.
    """
    agent_dir = _make_agent_dir(tmp_path)
    # _make_governance_md uses the canonical template which is always LF.
    _make_governance_md(agent_dir)

    args = _make_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        yes=True,
    )
    exit_code = run_govern(args, tmp_path)
    assert exit_code == 0, "run_govern must exit 0 on a valid LF apply"

    result_bytes = (agent_dir / "governance.md").read_bytes()
    assert b"\r" not in result_bytes, (
        "\\r found in output of an LF-authored file — the CRLF fix must have "
        "incorrectly introduced carriage returns.  Check _edit_governance_block "
        "and _read_or_create_governance."
    )

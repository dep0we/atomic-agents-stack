"""Tests for agent-to-agent delegation primitive per spec/15.

Covers:
- delegate() loads target with its own persona
- delegate() returns a Response
- NotInRoster when target not in roster
- NotInRoster when roster is empty
- SelfDelegationError when delegating to self
- CostGuardrailBlocked when parent cap is hit
- Per-call JSONL log with trigger=delegate and parent_run_id
- Per-run rollup embedded in coordinator's run-log record
- Target's captures land in target's memory (not coordinator's)
- delegate_parallel() fans out and returns ordered Responses
- delegate_parallel() reservation blocks overshoot
- delegate_parallel() max_concurrent clamped at [1, 25]
- Cascade layout: peer-role resolution
- Single-agent layout: top-level sibling resolution
- critical=True bypasses cap
- Roster.md parser: comments and blank lines
"""

from __future__ import annotations
import json
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.agent import AtomicAgent
from atomic_agents.exceptions import (
    CostGuardrailBlocked,
    NotInRoster,
    SelfDelegationError,
)
from atomic_agents.types import Response
from atomic_agents._roster import parse_roster_md_text


# ──────────────────────────────────────────────────────────────────
# Shared helpers


def _build_minimal_agent(
    agents_root: Path,
    name: str,
    identity_text: str = "# Identity\nTestAgent.",
    roster_text: str = "",
    model: str = "claude-haiku-4-5-20251001",
    guardrails_block: str = "",
) -> Path:
    """Create a minimal agent directory under agents_root."""
    agent_dir = agents_root / name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text(identity_text)
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    model_text = f"## Default model\n{model}\n"
    if guardrails_block:
        model_text += f"\n{guardrails_block}\n"
    (agent_dir / "model.md").write_text(model_text)
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    if roster_text:
        (agent_dir / "roster.md").write_text(roster_text)
    return agent_dir


def _make_anthropic_resp(text: str, *, input_tokens=10, output_tokens=20):
    """Mock Anthropic SDK response shape (matches test_helper_provenance pattern)."""
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return types.SimpleNamespace(content=[block], usage=usage)


def _tight_cap_guardrails(daily_cap_usd: float = 0.000001) -> str:
    return (
        "```yaml\n"
        "cost_guardrails:\n"
        "  enabled: true\n"
        f"  daily_cap_usd: {daily_cap_usd}\n"
        "  monthly_cap_usd: 100.0\n"
        "  daily_cap_action: skip\n"
        "  monthly_cap_action: alert\n"
        "```"
    )


# ──────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def tmp_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    return agents_root


@pytest.fixture
def coord_and_target(tmp_agents):
    """Coordinator with [editor] in roster, plus an editor agent."""
    roster = "# Roster\n\n## Delegate to\n\n- editor — edits stuff\n"
    _build_minimal_agent(
        tmp_agents, "director",
        identity_text="# Identity\nDirector.",
        roster_text=roster,
    )
    _build_minimal_agent(
        tmp_agents, "editor",
        identity_text="# Identity\nEditor — I edit.",
    )
    coordinator = AtomicAgent(name="director", agents_root=tmp_agents)
    return coordinator, tmp_agents


# ──────────────────────────────────────────────────────────────────
# 1. delegate() loads target with its own persona


def test_delegate_loads_target_with_own_persona(coord_and_target):
    coordinator, tmp_agents = coord_and_target
    resp = _make_anthropic_resp("Edited text.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        coordinator.delegate(target_agent_name="editor", work_item="Please edit this draft.")

    # The LLM should have been called; verify the system prompt contains editor's persona
    assert fake_client.messages.create.called
    _, call_kwargs = fake_client.messages.create.call_args
    # system is a list of cache-control blocks; grab all text
    sys_text = " ".join(
        block.get("text", "") if isinstance(block, dict) else ""
        for block in call_kwargs.get("system", [])
    )
    assert "Editor" in sys_text, f"Expected editor persona in system prompt, got: {sys_text[:200]}"


# ──────────────────────────────────────────────────────────────────
# 2. delegate() returns a Response


def test_delegate_returns_response(coord_and_target):
    coordinator, _ = coord_and_target
    resp = _make_anthropic_resp("Edited.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = coordinator.delegate(target_agent_name="editor", work_item="Edit this.")

    assert isinstance(result, Response)
    assert result.text == "Edited."
    assert result.input_tokens == 10
    assert result.output_tokens == 20
    assert not result.skipped


# ──────────────────────────────────────────────────────────────────
# 3. delegate() refuses target not in roster


def test_delegate_refuses_target_not_in_roster(tmp_agents):
    roster = "# Roster\n\n## Delegate to\n\n- alpha\n- beta\n"
    _build_minimal_agent(tmp_agents, "coord", roster_text=roster)
    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)

    with pytest.raises(NotInRoster, match="gamma"):
        coordinator.delegate(target_agent_name="gamma", work_item="anything")


# ──────────────────────────────────────────────────────────────────
# 4. delegate() refuses empty roster


def test_delegate_refuses_empty_roster(tmp_agents):
    _build_minimal_agent(tmp_agents, "coord", roster_text="")  # no roster.md section
    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)
    assert coordinator.config.roster == []

    with pytest.raises(NotInRoster):
        coordinator.delegate(target_agent_name="anything", work_item="x")


# ──────────────────────────────────────────────────────────────────
# 5. delegate() refuses self-delegation


def test_delegate_refuses_self_delegation(tmp_agents):
    roster = "# Roster\n\n## Delegate to\n\n- coord\n"
    _build_minimal_agent(tmp_agents, "coord", roster_text=roster)
    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)

    with pytest.raises(SelfDelegationError, match="itself"):
        coordinator.delegate(target_agent_name="coord", work_item="self-call")


# ──────────────────────────────────────────────────────────────────
# 6. delegate() refuses when parent cap hit


def test_delegate_refuses_when_parent_cap_hit(tmp_agents):
    roster = "# Roster\n\n## Delegate to\n\n- editor\n"
    _build_minimal_agent(
        tmp_agents, "director",
        roster_text=roster,
        guardrails_block=_tight_cap_guardrails(daily_cap_usd=0.000001),
    )
    _build_minimal_agent(tmp_agents, "editor")

    coordinator = AtomicAgent(name="director", agents_root=tmp_agents)

    # Write a log record to eat up the cap
    from datetime import date
    today = date.today()
    log_path = (
        coordinator.agent_root / "log"
        / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({"cost_usd": 0.001, "ts": "x"}) + "\n")

    fake_client = MagicMock()
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with pytest.raises(CostGuardrailBlocked):
            coordinator.delegate(target_agent_name="editor", work_item="edit this")

    # No LLM calls should have been made
    assert not fake_client.messages.create.called


# ──────────────────────────────────────────────────────────────────
# 7. delegate() logs per-call with parent_run_id


def test_delegate_logs_per_call_with_parent_run_id(coord_and_target):
    coordinator, _ = coord_and_target
    resp = _make_anthropic_resp("Done.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        coordinator.delegate(
            target_agent_name="editor",
            work_item="Edit this.",
            summary="test delegation",
        )

    today = date.today()
    log_path = (
        coordinator.agent_root / "log"
        / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    lines = log_path.read_text().strip().splitlines()
    delegate_records = [
        json.loads(L) for L in lines
        if json.loads(L).get("trigger") == "delegate"
    ]
    assert len(delegate_records) == 1
    rec = delegate_records[0]
    assert rec["parent_agent"] == "director"
    assert rec["delegated_agent"] == "editor"
    assert rec["parent_run_id"] == coordinator.run_id
    assert "delegate_run_id" in rec
    assert rec["summary"] == "test delegation"


# ──────────────────────────────────────────────────────────────────
# 8. delegate() run rollup embedded in coordinator's call log


def test_delegate_run_rollup_in_call_log(coord_and_target):
    """When call() wraps a delegate, the parent's run-log record has `delegations`."""
    coordinator, tmp_agents = coord_and_target
    resp = _make_anthropic_resp("Output from editor.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        # Simulate a coordinator call() that internally delegates
        # We call directly since we're testing the rollup mechanism
        coordinator._delegations_this_run = []
        coordinator.delegate(target_agent_name="editor", work_item="edit.")
        # Manually trigger a run-log record as call() would
        log_record = {
            "trigger": "manual",
            "model": coordinator.config.default_model,
            "input_tokens": 5,
            "output_tokens": 10,
            "status": "ok",
            "summary": "coordinator ran",
            "run_id": coordinator.run_id,
        }
        if coordinator._delegations_this_run:
            log_record["delegations"] = list(coordinator._delegations_this_run)
        coordinator._log(log_record)

    today = date.today()
    log_path = (
        coordinator.agent_root / "log"
        / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    lines = log_path.read_text().strip().splitlines()
    coord_records = [
        json.loads(L) for L in lines
        if json.loads(L).get("trigger") == "manual"
    ]
    assert len(coord_records) >= 1
    rec = coord_records[-1]
    assert "delegations" in rec
    assert rec["delegations"][0]["target"] == "editor"


# ──────────────────────────────────────────────────────────────────
# 9. target writes captures to target memory, not coordinator


def test_delegate_target_writes_own_captures_to_target_memory(coord_and_target):
    coordinator, tmp_agents = coord_and_target

    capture_json = json.dumps({
        "type": "reference",
        "name": "test capture",
        "description": "a test",
        "confidence": "high",
        "sources": ["test-run"],
        "body": "Some content.",
    })
    response_text = (
        "Here is the result.\n\n"
        f"```atomic_capture\n{capture_json}\n```\n"
    )

    resp = _make_anthropic_resp(response_text)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = coordinator.delegate(target_agent_name="editor", work_item="edit.")

    # If captures were written, they should be in EDITOR's memory, not director's
    editor_memory = tmp_agents / "editor" / "memory"
    coordinator_memory = tmp_agents / "director" / "memory"

    editor_notes = list(editor_memory.glob("*.md"))
    coordinator_notes = list(coordinator_memory.glob("*.md"))

    # Coordinator memory should have no new notes from this delegation
    assert len(coordinator_notes) == 0, (
        f"coordinator memory should be empty; found: {coordinator_notes}"
    )

    # Editor memory may have the capture (if write_captures=True is honored)
    # We just verify no cross-contamination — editor got it, not coordinator
    if result.captures:
        assert len(editor_notes) >= len(result.captures)


# ──────────────────────────────────────────────────────────────────
# 10. delegate_parallel() fans out and returns ordered responses


def test_delegate_parallel_fans_out(tmp_agents):
    roster = "# Roster\n\n## Delegate to\n\n- alpha\n- beta\n- gamma\n"
    _build_minimal_agent(tmp_agents, "coord",
                         roster_text=roster,
                         guardrails_block=_tight_cap_guardrails(daily_cap_usd=10.0))
    for name in ("alpha", "beta", "gamma"):
        _build_minimal_agent(tmp_agents, name, identity_text=f"# Identity\n{name.title()}.")

    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)

    call_counter = [0]

    def mock_create(**kwargs):
        call_counter[0] += 1
        n = call_counter[0]
        return _make_anthropic_resp(f"Response from call {n}")

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = mock_create
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        results = coordinator.delegate_parallel(
            calls=[
                ("alpha", "Work item for alpha"),
                ("beta", "Work item for beta"),
                ("gamma", "Work item for gamma"),
            ],
            max_concurrent=2,
        )

    assert len(results) == 3
    assert all(isinstance(r, Response) for r in results)
    assert all(not r.skipped for r in results)


# ──────────────────────────────────────────────────────────────────
# 11. delegate_parallel() reservation blocks overshoot


def test_delegate_parallel_reservation_blocks_overshoot(tmp_agents):
    roster = "# Roster\n\n## Delegate to\n\n- alpha\n- beta\n"
    _build_minimal_agent(
        tmp_agents, "coord",
        roster_text=roster,
        guardrails_block=_tight_cap_guardrails(daily_cap_usd=0.000001),
    )
    for name in ("alpha", "beta"):
        _build_minimal_agent(tmp_agents, name)

    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)

    fake_client = MagicMock()
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        with pytest.raises(CostGuardrailBlocked, match="reservation|Parallel|cap"):
            coordinator.delegate_parallel(
                calls=[
                    ("alpha", "work a"),
                    ("beta", "work b"),
                ],
                max_concurrent=2,
                max_tokens=1024,
            )

    assert not fake_client.messages.create.called


# ──────────────────────────────────────────────────────────────────
# 12. delegate_parallel() max_concurrent clamped at [1, 25]


def test_delegate_parallel_max_concurrent_clamped(tmp_agents):
    roster = "# Roster\n\n## Delegate to\n\n- alpha\n"
    _build_minimal_agent(tmp_agents, "coord", roster_text=roster)

    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)

    with pytest.raises(ValueError, match="25"):
        coordinator.delegate_parallel(
            calls=[("alpha", "work")],
            max_concurrent=26,
        )

    with pytest.raises(ValueError, match="1"):
        coordinator.delegate_parallel(
            calls=[("alpha", "work")],
            max_concurrent=0,
        )


# ──────────────────────────────────────────────────────────────────
# 13. Cascade layout resolves peer role under same project


def test_delegate_in_cascaded_layout_resolves_peer_role(tmp_path, monkeypatch):
    """In a cascaded layout, delegate('editor') resolves to
    <system>/projects/<project>/agents/editor/.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_ANTHROPIC_KEY", "fake-key")

    # Build a cascaded layout
    system = tmp_path / "muse"
    role_dir = system / "roles" / "director"
    role_dir.mkdir(parents=True)
    (role_dir / "PROMPT.md").write_text("You are the director.")
    (role_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (role_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")

    proj = system / "projects" / "the-unfinished"
    instance = proj / "agents" / "director"
    (instance / "persona").mkdir(parents=True)
    (instance / "persona" / "IDENTITY.md").write_text("# Identity\nDirector on Unfinished.")
    (instance / "memory").mkdir()
    (instance / "log").mkdir()
    (instance / "roster.md").write_text(
        "# Roster\n\n## Delegate to\n\n- editor\n"
    )

    coordinator = AtomicAgent(name="director", agents_root=proj / "agents")
    assert coordinator.cascade is not None, "Expected cascade to be detected"

    resolved = coordinator._resolve_delegated_agent_path("editor")
    expected = proj / "agents" / "editor"
    assert resolved == expected, f"Expected {expected}, got {resolved}"


# ──────────────────────────────────────────────────────────────────
# 14. Single-agent layout resolves top-level sibling


def test_delegate_in_single_agent_layout_resolves_top_level(tmp_agents):
    """In a single-agent layout, delegate('editor') resolves to
    <agents_root>/editor/.
    """
    roster = "# Roster\n\n## Delegate to\n\n- editor\n"
    _build_minimal_agent(tmp_agents, "director", roster_text=roster)
    coordinator = AtomicAgent(name="director", agents_root=tmp_agents)

    assert coordinator.cascade is None, "Expected no cascade"
    resolved = coordinator._resolve_delegated_agent_path("editor")
    expected = tmp_agents / "editor"
    assert resolved == expected, f"Expected {expected}, got {resolved}"


# ──────────────────────────────────────────────────────────────────
# 15. critical=True bypasses cap


def test_delegate_critical_bypasses_cap(tmp_agents):
    """critical=True lets the delegation proceed even when cap is hit."""
    roster = "# Roster\n\n## Delegate to\n\n- editor\n"
    _build_minimal_agent(
        tmp_agents, "director",
        roster_text=roster,
        guardrails_block=_tight_cap_guardrails(daily_cap_usd=0.000001),
    )
    _build_minimal_agent(tmp_agents, "editor")

    coordinator = AtomicAgent(name="director", agents_root=tmp_agents)

    # Eat up the cap
    from datetime import date
    today = date.today()
    log_path = (
        coordinator.agent_root / "log"
        / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({"cost_usd": 0.001, "ts": "x"}) + "\n")

    resp = _make_anthropic_resp("Critical response.")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = resp
    fake_anthropic = types.SimpleNamespace(Anthropic=lambda api_key: fake_client)

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        result = coordinator.delegate(
            target_agent_name="editor",
            work_item="edit this",
            critical=True,
        )

    assert isinstance(result, Response)
    assert result.text == "Critical response."

    # Verify the log record carries critical: true
    today_log_path = (
        coordinator.agent_root / "log"
        / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    lines = today_log_path.read_text().strip().splitlines()
    delegate_records = [
        json.loads(L) for L in lines
        if json.loads(L).get("trigger") == "delegate"
    ]
    assert len(delegate_records) == 1
    assert delegate_records[0].get("critical") is True


# ──────────────────────────────────────────────────────────────────
# 16. Roster parser: comments, blank lines, notes section ignored


def test_roster_md_parser_handles_comments_and_blanks():
    text = """
# Roster

## Delegate to

- editor — proofreads drafts, checks style
- director — high-level scope and continuity
- researcher — fact-checks claims

## Notes

This section should be ignored entirely.
- fake-agent — should not appear
"""
    names = parse_roster_md_text(text)
    assert names == ["editor", "director", "researcher"]


def test_roster_md_parser_empty_returns_empty():
    assert parse_roster_md_text("") == []
    assert parse_roster_md_text("# Roster\n\n## Notes\n\n- nothing\n") == []


def test_roster_md_parser_various_separator_styles():
    text = """
## Delegate to

- alpha — description with em dash
- beta - description with hyphen
- gamma (description in parens)
- delta, comma-separated comment
"""
    names = parse_roster_md_text(text)
    assert "alpha" in names
    assert "beta" in names
    assert "gamma" in names
    assert "delta" in names


def test_roster_md_parser_blank_lines_and_extra_whitespace():
    text = """
## Delegate to


  - editor — edits

  - writer — writes

"""
    names = parse_roster_md_text(text)
    assert names == ["editor", "writer"]


# ──────────────────────────────────────────────────────────────────
# 17. Path-traversal guard on delegate target (codex R2-B regression)


def test_delegate_refuses_dotdot_target_in_roster(tmp_agents):
    """A roster entry like '../other' triggers NotInRoster (path traversal guard)."""
    # The name passes the roster membership check but should fail path traversal.
    # We put the dotdot name directly in the roster so _enforce_roster_membership
    # passes, then _resolve_delegated_agent_path should catch the traversal.
    roster = "# Roster\n\n## Delegate to\n\n- ../other\n"
    _build_minimal_agent(tmp_agents, "coord", roster_text=roster)
    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)

    with pytest.raises(NotInRoster, match="traversal|resolves outside"):
        coordinator.delegate(target_agent_name="../other", work_item="anything")


def test_delegate_refuses_absolute_path_in_roster(tmp_agents):
    """A roster entry that is an absolute path triggers NotInRoster."""
    roster = "# Roster\n\n## Delegate to\n\n- /tmp/evil\n"
    _build_minimal_agent(tmp_agents, "coord", roster_text=roster)
    coordinator = AtomicAgent(name="coord", agents_root=tmp_agents)

    with pytest.raises(NotInRoster, match="traversal|resolves outside"):
        coordinator.delegate(target_agent_name="/tmp/evil", work_item="anything")

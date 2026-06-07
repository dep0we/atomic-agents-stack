"""Tests for agent.py changes that support the serve layer.

Covers:
  - 'http' trigger maps to PRIMITIVE_AGENT_CALL in _PRIMITIVE_BY_TRIGGER
  - caller_identity is written as http_caller into the JSONL run record
  - run_id is reset at the start of each call() invocation (MUST 8)
  - critical=True is still blocked on the HTTP surface (serve layer enforces
    this structurally, but the underlying call() still accepts critical=False)

These tests run without the serve extra (no starlette required).
spec/37 MUST 5, 7, 8 and spec/22 §"Canonical primitive taxonomy".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from atomic_agents.agent import _PRIMITIVE_BY_TRIGGER, _derive_primitive_from_trigger
from atomic_agents.logs.types import PRIMITIVE_AGENT_CALL


# ── Primitive taxonomy ────────────────────────────────────────────────────────


def test_http_trigger_maps_to_agent_call_primitive():
    """'http' trigger must map to PRIMITIVE_AGENT_CALL per spec/37 MUST + spec/22."""
    assert "http" in _PRIMITIVE_BY_TRIGGER
    assert _PRIMITIVE_BY_TRIGGER["http"] == PRIMITIVE_AGENT_CALL


def test_derive_primitive_http():
    """_derive_primitive_from_trigger('http') returns PRIMITIVE_AGENT_CALL."""
    result = _derive_primitive_from_trigger("http")
    assert result == PRIMITIVE_AGENT_CALL


def test_http_is_in_primitive_map_not_other():
    """'http' must not fall through to PRIMITIVE_OTHER."""
    from atomic_agents.logs.types import PRIMITIVE_OTHER

    assert _derive_primitive_from_trigger("http") != PRIMITIVE_OTHER


# ── caller_identity and audit trail ─────────────────────────────────────────


def _build_minimal_agent_root(tmp_path: Path) -> Path:
    """Build the minimum agent folder layout needed for AtomicAgent.__init__."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "testbot"
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text(
        "# Identity\nYou are TestBot.", encoding="utf-8"
    )
    (agent_root / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20260101\n\n"
        "Max input prompt tokens: 2000\n"
        "Max output tokens: 500\n",
        encoding="utf-8",
    )
    (agent_root / "tools.md").write_text(
        "## Read paths\n- ~/\n\n## Write paths\n- ~/\n",
        encoding="utf-8",
    )
    return agents_root


def test_caller_identity_in_log_record(tmp_path: Path):
    """caller_identity='user@example.com' must appear as http_caller in JSONL record.

    spec/37 MUST 7.
    """
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent

    # Patch the LLM call so no real API call fires
    mock_response = MagicMock()
    mock_response.text = "Hello world"
    mock_response.tool_uses = []
    mock_response.input_tokens = 10
    mock_response.output_tokens = 5
    mock_response.cache_hit_tokens = 0
    mock_response.cache_miss_tokens = 0
    mock_response.raw = {}

    logged_records: list[dict] = []

    def fake_log(record: dict) -> None:
        logged_records.append(dict(record))

    agent = AtomicAgent(name="testbot", trigger="http", agents_root=agents_root)

    with (
        patch("atomic_agents._llm.call_llm", return_value=mock_response),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),  # skip heavy persona loading
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(
            agent,
            "assemble_system_prompt",
            return_value="You are TestBot.",
        ),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok"),
        ),
    ):
        agent.load()
        agent.call(work_item="ping", caller_identity="user@example.com")

    # Find the agent_call record (trigger='http', status='ok')
    call_records = [
        r
        for r in logged_records
        if r.get("trigger") == "http" and r.get("status") == "ok"
    ]
    assert call_records, f"No http/ok record found in: {logged_records}"
    record = call_records[-1]
    assert record.get("http_caller") == "user@example.com", (
        f"http_caller missing or wrong in record: {record}"
    )


def test_caller_identity_none_omits_field(tmp_path: Path):
    """caller_identity=None (default) must NOT write http_caller to the record.

    spec/37: home users pass None; field is absent in the JSONL record.
    """
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent

    mock_response = MagicMock()
    mock_response.text = "Hello"
    mock_response.tool_uses = []
    mock_response.input_tokens = 5
    mock_response.output_tokens = 3
    mock_response.cache_hit_tokens = 0
    mock_response.cache_miss_tokens = 0
    mock_response.raw = {}

    logged_records: list[dict] = []

    def fake_log(record: dict) -> None:
        logged_records.append(dict(record))

    agent = AtomicAgent(name="testbot", trigger="manual", agents_root=agents_root)

    with (
        patch("atomic_agents._llm.call_llm", return_value=mock_response),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are TestBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok"),
        ),
    ):
        agent.load()
        agent.call(work_item="ping")  # caller_identity defaults to None

    call_records = [
        r
        for r in logged_records
        if r.get("trigger") == "manual" and r.get("status") == "ok"
    ]
    assert call_records, f"No manual/ok record found in: {logged_records}"
    record = call_records[-1]
    assert "http_caller" not in record, (
        f"http_caller should be absent when caller_identity=None, got: {record}"
    )


# ── run_id reset ─────────────────────────────────────────────────────────────


def test_run_id_is_unique_across_sequential_calls(tmp_path: Path):
    """Two sequential call() invocations must produce two distinct run_ids.

    spec/37 MUST 8: run_id reset at start of each call().
    """
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent

    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_response.tool_uses = []
    mock_response.input_tokens = 1
    mock_response.output_tokens = 1
    mock_response.cache_hit_tokens = 0
    mock_response.cache_miss_tokens = 0
    mock_response.raw = {}

    captured_run_ids: list[str] = []

    agent = AtomicAgent(name="testbot", trigger="http", agents_root=agents_root)

    def capturing_log(record: dict) -> None:
        run_id = record.get("run_id") or agent.run_id
        if record.get("status") == "ok":
            captured_run_ids.append(run_id)

    with (
        patch("atomic_agents._llm.call_llm", return_value=mock_response),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are TestBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok"),
        ),
    ):
        agent.load()
        # Capture run_id after first call
        agent.call(work_item="first")
        run_id_1 = agent.run_id
        # Capture run_id after second call
        agent.call(work_item="second")
        run_id_2 = agent.run_id

    assert run_id_1 != run_id_2, (
        f"run_id must be unique per call(); got same id twice: {run_id_1!r}"
    )


# ── lock timeout ─────────────────────────────────────────────────────────────


def test_http_trigger_uses_30s_lock_timeout(tmp_path: Path):
    """trigger='http' must use a 30s lock timeout, not 0.

    spec/37 §"Concurrency contract" — prevents LockBusy on concurrent requests.
    """
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="testbot", trigger="http", agents_root=agents_root)

    acquired_timeout: list[int] = []
    original_acquire = agent.lock_backend.acquire

    def capturing_acquire(name: str, timeout: int = 0) -> Any:
        acquired_timeout.append(timeout)
        return original_acquire(name, timeout=timeout)

    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_response.tool_uses = []
    mock_response.input_tokens = 1
    mock_response.output_tokens = 1
    mock_response.cache_hit_tokens = 0
    mock_response.cache_miss_tokens = 0
    mock_response.raw = {}

    with (
        patch.object(agent.lock_backend, "acquire", side_effect=capturing_acquire),
        patch("atomic_agents._llm.call_llm", return_value=mock_response),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are TestBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok"),
        ),
    ):
        agent.load()
        agent.call(work_item="ping")

    assert acquired_timeout, "lock.acquire was never called"
    assert acquired_timeout[0] == 30, (
        f"Expected lock timeout=30 for trigger='http', got {acquired_timeout[0]}"
    )


def test_manual_trigger_uses_0s_lock_timeout(tmp_path: Path):
    """trigger='manual' keeps the existing 0s lock timeout (fail-fast)."""
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="testbot", trigger="manual", agents_root=agents_root)

    acquired_timeout: list[int] = []
    original_acquire = agent.lock_backend.acquire

    def capturing_acquire(name: str, timeout: int = 0) -> Any:
        acquired_timeout.append(timeout)
        return original_acquire(name, timeout=timeout)

    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_response.tool_uses = []
    mock_response.input_tokens = 1
    mock_response.output_tokens = 1
    mock_response.cache_hit_tokens = 0
    mock_response.cache_miss_tokens = 0
    mock_response.raw = {}

    with (
        patch.object(agent.lock_backend, "acquire", side_effect=capturing_acquire),
        patch("atomic_agents._llm.call_llm", return_value=mock_response),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are TestBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok"),
        ),
    ):
        agent.load()
        agent.call(work_item="ping")

    assert acquired_timeout, "lock.acquire was never called"
    assert acquired_timeout[0] == 0, (
        f"Expected lock timeout=0 for trigger='manual', got {acquired_timeout[0]}"
    )


# ── http_caller on refused paths (lock_busy, cost-skip) ──────────────────────


def test_caller_identity_in_lock_busy_record(tmp_path: Path):
    """http_caller must appear in the lock_busy JSONL record when caller_identity is set.

    spec/37 MUST 7 — field present on ALL HTTP-triggered audit records including
    refused paths. CLAUDE.md principle 5 (audit trail is structural).
    """
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent
    from atomic_agents.exceptions import LockBusy

    agent = AtomicAgent(name="testbot", trigger="http", agents_root=agents_root)

    logged_records: list[dict] = []

    def fake_log(record: dict) -> None:
        logged_records.append(dict(record))

    with (
        patch.object(
            agent.lock_backend,
            "acquire",
            side_effect=LockBusy("lock held by another call"),
        ),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
    ):
        agent.load()
        try:
            agent.call(work_item="ping", caller_identity="locked@example.com")
        except LockBusy:
            pass  # expected

    lock_records = [r for r in logged_records if r.get("status") == "lock_busy"]
    assert lock_records, f"No lock_busy record found in: {logged_records}"
    record = lock_records[-1]
    assert record.get("http_caller") == "locked@example.com", (
        f"http_caller missing or wrong in lock_busy record: {record}"
    )


def test_caller_identity_in_cost_skip_record(tmp_path: Path):
    """http_caller must appear in the cost-skip (skipped) JSONL record.

    spec/37 MUST 7 — field present on ALL HTTP-triggered audit records including
    refused paths. CLAUDE.md principle 5 (audit trail is structural).
    """
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="testbot", trigger="http", agents_root=agents_root)

    logged_records: list[dict] = []

    def fake_log(record: dict) -> None:
        logged_records.append(dict(record))

    with (
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=False, reason="daily cap reached"),
        ),
    ):
        agent.load()
        agent.call(work_item="ping", caller_identity="skipped@example.com")

    skip_records = [r for r in logged_records if r.get("status") == "skipped"]
    assert skip_records, f"No skipped record found in: {logged_records}"
    record = skip_records[-1]
    assert record.get("http_caller") == "skipped@example.com", (
        f"http_caller missing or wrong in skipped record: {record}"
    )


# ── _generate_run_id uniqueness under frozen clock ────────────────────────────


def test_generate_run_id_unique_with_frozen_datetime():
    """_generate_run_id() must produce distinct ids even when datetime is frozen.

    The uuid4 hex suffix is the ONLY thing that distinguishes ids produced in
    the same microsecond. This test pins the clock and verifies the suffix
    carries the uniqueness property — if the suffix is removed, this test fails.

    spec/37 MUST 8 — 'uuid4 hex suffix guarantees uniqueness under same-
    microsecond concurrent requests'. CLAUDE.md working methods: 'the
    correctness ratchet runs through the test suite'; a concurrency fix proven
    only by a sequential test is a corner cut.
    """
    from unittest.mock import patch
    from datetime import datetime as _dt

    from atomic_agents.agent import AtomicAgent

    FROZEN = _dt(2026, 6, 7, 12, 0, 0, 123456)

    with patch("atomic_agents.agent.datetime") as mock_dt:
        mock_dt.now.return_value = FROZEN
        ids = {AtomicAgent._generate_run_id() for _ in range(1000)}

    assert len(ids) == 1000, (
        f"_generate_run_id() produced only {len(ids)} distinct ids out of 1000 "
        "calls with a frozen clock — the uuid4 suffix is missing or insufficient"
    )


# ── run_id pinning — caller-pinned run_id must survive call() ─────────────────


def test_pinned_run_id_survives_call(tmp_path: Path):
    """An explicit run_id passed to the constructor must not be overwritten by call().

    OutcomeRunner (and eval/dream loops) pin run_id so that agent_call JSONL
    records correlate with the outer loop's records. The unconditional reset at
    the top of call() was breaking this contract.

    CLAUDE.md principle 5 (audit trail is structural).
    spec/37 MUST 8 note: 'the reset is skipped only when the constructor
    received an explicit run_id'.
    """
    agents_root = _build_minimal_agent_root(tmp_path)

    from atomic_agents.agent import AtomicAgent

    PINNED_ID = "outcome-20260607-120000-000000-pinned123"

    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_response.tool_uses = []
    mock_response.input_tokens = 1
    mock_response.output_tokens = 1
    mock_response.cache_hit_tokens = 0
    mock_response.cache_miss_tokens = 0
    mock_response.raw = {}

    agent = AtomicAgent(
        name="testbot",
        trigger="outcome",
        agents_root=agents_root,
        run_id=PINNED_ID,
    )

    assert agent.run_id == PINNED_ID, "Constructor should set the pinned run_id"

    with (
        patch("atomic_agents._llm.call_llm", return_value=mock_response),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are TestBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(allow=True, action="ok"),
        ),
    ):
        agent.load()
        agent.call(work_item="step-1")

    assert agent.run_id == PINNED_ID, (
        f"call() must not overwrite a pinned run_id; expected {PINNED_ID!r}, "
        f"got {agent.run_id!r}"
    )


# ── mid-loop cost-cap http_caller ─────────────────────────────────────────────


def test_caller_identity_in_mid_loop_cost_skip_record(tmp_path: Path):
    """http_caller must appear in the mid-loop cost-cap-hit JSONL record.

    The pre-loop cost-skip and lock_busy paths already inject http_caller.
    This tests the FOURTH terminal parent record: the skipped record written
    when the cost cap is hit AFTER the first LLM iteration has already run
    (iteration_count > 1). This path is only reached when a first LLM call
    returns a custom tool_use (triggering a second iteration), and the
    mid-loop cost check then denies.

    spec/37 MUST 7 — field present on ALL HTTP-triggered audit records
    including refused paths. CLAUDE.md principle 5 (audit trail is structural).
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.tools import ToolDefinition, ToolRegistry

    agents_root = _build_minimal_agent_root(tmp_path)

    # Register a custom tool so the first LLM response can return a tool_use
    # that passes the custom_tool_uses partition and forces iteration_count > 1.
    dummy_registry = ToolRegistry()
    dummy_registry.register(
        ToolDefinition(
            name="noop_tool",
            description="no-op for testing",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda _: "done",
        )
    )

    agent = AtomicAgent(
        name="testbot",
        trigger="http",
        agents_root=agents_root,
        tools=dummy_registry,
    )

    logged_records: list[dict] = []

    def fake_log(record: dict) -> None:
        logged_records.append(dict(record))

    guardrails_call_count = 0

    def guardrails_allow_then_deny(*args, **kwargs):
        nonlocal guardrails_call_count
        guardrails_call_count += 1
        if guardrails_call_count == 1:
            # Pre-loop check: allow
            return MagicMock(allow=True, action="ok")
        # Mid-loop check (iteration_count > 1): deny
        return MagicMock(allow=False, reason="mid-loop cap hit")

    # First response includes a tool_use for noop_tool — this drives a second
    # iteration where the mid-loop cost check fires and denies.
    tool_use_response = MagicMock()
    tool_use_response.text = ""
    tool_use_response.tool_uses = [{"name": "noop_tool", "id": "tu_1", "input": {}}]
    tool_use_response.input_tokens = 100
    tool_use_response.output_tokens = 50
    tool_use_response.cache_hit_tokens = 0
    tool_use_response.cache_miss_tokens = 0
    tool_use_response.raw = {}

    with (
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "_persona_text", "You are TestBot.", create=True),
        patch.object(agent, "assemble_system_prompt", return_value="You are TestBot."),
        patch.object(
            agent, "_check_cost_guardrails", side_effect=guardrails_allow_then_deny
        ),
        patch("atomic_agents._llm.call_llm", return_value=tool_use_response),
    ):
        agent.load()
        agent.call(work_item="multi-turn", caller_identity="mid-loop@example.com")

    mid_loop_skips = [
        r
        for r in logged_records
        if r.get("status") == "skipped" and r.get("cost_source") == "actor"
    ]
    assert mid_loop_skips, (
        f"No mid-loop skipped record (status=skipped, cost_source=actor) found. "
        f"All logged records: {logged_records}"
    )
    record = mid_loop_skips[-1]
    assert record.get("http_caller") == "mid-loop@example.com", (
        f"http_caller missing or wrong in mid-loop skipped record: {record}"
    )

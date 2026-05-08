"""Tests for atomic_agents.dream — memory consolidation pipeline."""

from __future__ import annotations

import json
import os
import sys
import types
import threading
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest

from atomic_agents.dream import (
    DreamRunner,
    DreamResult,
    DreamInputs,
    ConsolidatedNote,
    PromotedNote,
    StaleMarking,
    _detect_stale_notes,
    _cluster_by_type_and_name,
    _new_dream_id,
    _read_manifest,
    _write_manifest,
    _DreamLock,
)
from atomic_agents.exceptions import (
    AtomicAgentsError,
    AgentLockBusy,
    DreamInProgress,
    DreamNotFound,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures

def _build_agent(tmp_path: Path, agent_name: str = "dreamer") -> Path:
    """Build a minimal agent directory layout."""
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / agent_name
    (agent_dir / "persona").mkdir(parents=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\nDreamer agent.")
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    return agents_root


def _write_note(agent_dir: Path, filename: str, note_type: str, name: str,
                body: str, last_seen: str, pinned: bool = False,
                tags: list | None = None) -> Path:
    """Write a memory note to agent_dir/memory/."""
    memory_dir = agent_dir / "memory"
    memory_dir.mkdir(exist_ok=True)
    post = frontmatter.Post(
        body,
        schema_version=1,
        name=name,
        description=name[:80],
        type=note_type,
        captured=last_seen,
        last_seen=last_seen,
        sources=["test"],
        confidence="medium",
    )
    if pinned:
        post.metadata["pinned"] = True
    if tags:
        post.metadata["tags"] = tags
    path = memory_dir / filename
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return path


def _make_llm_response(text: str, input_tokens: int = 10, output_tokens: int = 20):
    """Build a mock _RawLLMResponse (as returned by _llm.call_llm)."""
    # atomic_agents._llm.call_llm returns _RawLLMResponse, not the raw SDK object
    from atomic_agents._llm import _RawLLMResponse
    return _RawLLMResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=input_tokens,
        raw=None,
        tool_uses=[],
    )


# ──────────────────────────────────────────────────────────────────
# Pure logic tests (no LLM)

def test_detect_duplicates_clusters_same_type_similar_names():
    """Notes of same type with similar names should cluster together."""
    notes = [
        {"filename": "feedback_debt_priority.md", "meta": {"type": "feedback", "name": "Debt priority rule"}, "body": "Body A"},
        {"filename": "feedback_debt_prioritization.md", "meta": {"type": "feedback", "name": "Debt prioritization"}, "body": "Body B"},
        {"filename": "decision_api_design.md", "meta": {"type": "decision", "name": "API design choice"}, "body": "Body C"},
    ]
    clusters = _cluster_by_type_and_name(notes)
    # The two feedback notes should end up in the same cluster
    cluster_sizes = sorted([len(c) for c in clusters], reverse=True)
    assert cluster_sizes[0] == 2, f"Expected cluster of 2, got {cluster_sizes}"
    assert cluster_sizes[1] == 1


def test_detect_stale_marks_old_unpinned_notes():
    """Notes with last_seen older than threshold (not pinned) should be marked stale."""
    old_date = (date.today() - timedelta(days=100)).isoformat()
    notes = [
        {
            "filename": "feedback_old.md",
            "meta": {"type": "feedback", "last_seen": old_date, "pinned": False},
            "body": "Old note body.",
        }
    ]
    markings = _detect_stale_notes(notes)
    assert len(markings) == 1
    assert markings[0].note == "feedback_old.md"
    # expires_at should be in the future
    from datetime import date as dt
    expires = dt.fromisoformat(markings[0].new_expires_at)
    assert expires > dt.today()


def test_detect_stale_skips_pinned_notes():
    """Pinned notes should never be marked stale regardless of last_seen."""
    old_date = (date.today() - timedelta(days=200)).isoformat()
    notes = [
        {
            "filename": "feedback_pinned.md",
            "meta": {"type": "feedback", "last_seen": old_date, "pinned": True},
            "body": "Pinned note body.",
        }
    ]
    markings = _detect_stale_notes(notes)
    assert len(markings) == 0


# ──────────────────────────────────────────────────────────────────
# Full pipeline tests (mock LLM)

def _no_op_response():
    """Helper response that claims no duplicates, no promotions."""
    return _make_llm_response(
        json.dumps({"is_duplicate": False, "merged_body": None, "merged_name": None})
    )


def _synthesis_response():
    return _make_llm_response(json.dumps({"confirmed": True, "notes": "all good"}))


def _promotion_response():
    return _make_llm_response(json.dumps({"promotions": []}))


def _contradiction_response():
    return _make_llm_response(json.dumps({"contradictions": []}))


@pytest.fixture
def agents_root(tmp_path):
    return _build_agent(tmp_path)


def test_dream_pipeline_satisfied_path(agents_root, monkeypatch):
    """Full happy-path: manifest reaches 'completed', output dir has notes."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test feedback",
                "Test body", "2026-01-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    assert result.status == "completed"
    assert result.dream_id.startswith("drm_")
    assert result.error is None
    # Output dir should exist
    out_dir = agent_dir / "dreams" / result.dream_id / "memory"
    assert out_dir.exists()
    # Should contain our note
    assert any(out_dir.glob("*.md"))


def test_dream_pipeline_writes_report_md(agents_root, monkeypatch):
    """report.md is written and has sections per change."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test feedback",
                "Test body", "2026-01-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    report_path = agent_dir / "dreams" / result.dream_id / "report.md"
    assert report_path.exists()
    content = report_path.read_text()
    assert "Dream Report" in content
    assert "Summary" in content


def test_dream_apply_atomically_swaps_directories(agents_root, monkeypatch):
    """Apply: old memory/ is archived, dreamed memory/ becomes live memory/."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Test body", "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    archived = runner.apply(result.dream_id)

    # Original memory should be archived
    assert archived.exists(), f"Archived dir should exist at {archived}"
    assert (archived / "feedback_test.md").exists()

    # New memory should be the dreamed one
    current_memory = agent_dir / "memory"
    assert current_memory.exists()
    assert (current_memory / "INDEX.md").exists()

    # Manifest should show applied_at
    manifest = runner.status(result.dream_id)
    assert manifest.applied_at is not None


def test_dream_apply_refuses_uncompleted_dream(agents_root, monkeypatch):
    """Applying a dream that isn't completed should raise."""
    agent_dir = agents_root / "dreamer"
    dream_id = _new_dream_id()
    dream_dir = agent_dir / "dreams" / dream_id
    dream_dir.mkdir(parents=True)

    # Write a manifest with status=running
    result = DreamResult(
        dream_id=dream_id,
        agent_name="dreamer",
        status="running",
        model="claude-haiku-4-5-20251001",
        instructions="",
        inputs=DreamInputs(
            memory_count=0, journal_lookback_days=30,
            journal_count=0, log_lookback_days=30, log_line_count=0,
        ),
        output_memory_count=0,
        consolidated=[],
        promoted=[],
        marked_stale=[],
        total_input_tokens=0,
        total_output_tokens=0,
        total_cost_usd=0.0,
        started_at="2026-05-07T12:00:00",
        ended_at=None,
    )
    _write_manifest(dream_dir, result)

    runner = DreamRunner(agents_root, "dreamer")
    with pytest.raises(AtomicAgentsError, match="status is 'running'"):
        runner.apply(dream_id)


def test_dream_apply_refuses_already_applied(agents_root, monkeypatch):
    """Applying a dream twice should raise."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Test body", "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    runner.apply(result.dream_id)

    with pytest.raises(AtomicAgentsError, match="already applied"):
        runner.apply(result.dream_id)


def test_dream_discard_removes_dir(agents_root, monkeypatch):
    """Discarding a dream removes its directory."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Body", "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    dream_dir = agent_dir / "dreams" / result.dream_id
    assert dream_dir.exists()

    runner.discard(result.dream_id)
    assert not dream_dir.exists()


def test_dream_discard_refuses_applied(agents_root, monkeypatch):
    """Discarding an already-applied dream should raise."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Body", "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    runner.apply(result.dream_id)

    with pytest.raises(AtomicAgentsError, match="already applied"):
        runner.discard(result.dream_id)


def test_dream_list_returns_newest_first(agents_root, monkeypatch):
    """list_dreams() returns dreams with the most recent first."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Body", "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        r1 = runner.start()

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        r2 = runner.start()

    dreams = runner.list_dreams()
    assert len(dreams) >= 2
    # Newest first: r2.dream_id should be first
    assert dreams[0].dream_id == r2.dream_id
    assert dreams[1].dream_id == r1.dream_id


def test_dream_concurrent_run_blocked_by_lock(agents_root, monkeypatch):
    """Second dream while first holds the lock raises DreamInProgress."""
    agent_dir = agents_root / "dreamer"
    dreams_dir = agent_dir / "dreams"
    dreams_dir.mkdir(parents=True, exist_ok=True)

    # Acquire the lock directly
    lock = _DreamLock(dreams_dir, wait_seconds=0)
    lock.acquire()

    try:
        runner = DreamRunner(agents_root, "dreamer")
        with pytest.raises(DreamInProgress):
            # zero wait means fail immediately if lock is busy
            inner_lock = _DreamLock(dreams_dir, wait_seconds=0)
            inner_lock.acquire()
    finally:
        lock.release()


def test_dream_cost_guardrail_pre_check_refuses(tmp_path):
    """Tight cost cap should block the dream before any LLM call."""
    agents_root = _build_agent(tmp_path, "capped")
    agent_dir = agents_root / "capped"

    # Write a model.md with an extremely tight cap
    model_md = (
        "## Default model\nclaude-haiku-4-5-20251001\n\n"
        "```yaml\n"
        "cost_guardrails:\n"
        "  enabled: true\n"
        "  daily_cap_usd: 0.000001\n"
        "  monthly_cap_usd: 0.000001\n"
        "  daily_cap_action: skip\n"
        "  monthly_cap_action: skip\n"
        "```\n"
    )
    (agent_dir / "model.md").write_text(model_md)

    # Write some notes to inflate the cost estimate
    for i in range(5):
        _write_note(agent_dir, f"feedback_note_{i}.md", "feedback", f"Note {i}",
                    "Body " * 100, "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "capped")
        with pytest.raises(ValueError, match="[Cc]ost|guardrail|headroom"):
            runner.start()

    # LLM should NOT have been called
    mock_llm.assert_not_called()


def test_dream_critical_bypasses_cap(tmp_path):
    """critical=True should run despite tight cost cap."""
    agents_root = _build_agent(tmp_path, "capped2")
    agent_dir = agents_root / "capped2"

    # Tight cap
    model_md = (
        "## Default model\nclaude-haiku-4-5-20251001\n\n"
        "```yaml\n"
        "cost_guardrails:\n"
        "  enabled: true\n"
        "  daily_cap_usd: 0.000001\n"
        "  monthly_cap_usd: 0.000001\n"
        "  daily_cap_action: skip\n"
        "  monthly_cap_action: skip\n"
        "```\n"
    )
    (agent_dir / "model.md").write_text(model_md)
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Body " * 100, "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "capped2")
        result = runner.start(critical=True)

    assert result.status == "completed"


def test_dream_failed_pipeline_preserves_partial_output(agents_root, monkeypatch):
    """Exception during synthesis → manifest=failed, error captured, partial output preserved."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Body", "2026-03-01")

    # Fail on the first synthesis call (which is the only call when there's 1 note
    # and no duplicate clusters needing LLM checks)
    def mock_llm_side_effect(*args, **kwargs):
        raise RuntimeError("Simulated synthesis failure")

    with patch("atomic_agents.dream._llm.call_llm", side_effect=mock_llm_side_effect):
        runner = DreamRunner(agents_root, "dreamer")
        with pytest.raises(RuntimeError, match="Simulated"):
            runner.start()

    # Find the most recent dream dir
    dreams = list((agent_dir / "dreams").iterdir()) if (agent_dir / "dreams").exists() else []
    dream_dirs = [d for d in dreams if d.is_dir() and d.name.startswith("drm_")]
    assert len(dream_dirs) >= 1

    # Read manifest — should show failed
    for d in dream_dirs:
        manifest_path = d / "manifest.json"
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text())
            assert data["status"] == "failed"
            assert data["error"] is not None
            break
    else:
        pytest.fail("No dream manifest found after failed pipeline")


def test_dream_empty_vault_completes_with_zero_changes(agents_root, monkeypatch):
    """Agent with no memory notes → completed dream with zero changes."""
    # Don't write any notes — empty memory dir already exists from _build_agent

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _synthesis_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    assert result.status == "completed"
    assert len(result.consolidated) == 0
    assert len(result.promoted) == 0
    assert result.output_memory_count == 0


# ──────────────────────────────────────────────────────────────────
# Codex R2 regression tests
# ──────────────────────────────────────────────────────────────────

def test_dream_apply_takes_agent_lock(agents_root, monkeypatch):
    """apply() must acquire the AgentLock and wait/fail if held by another process.

    We simulate an in-flight agent.call() by acquiring the AgentLock directly
    before calling apply().  With wait_seconds=0 apply() should raise AgentLockBusy
    quickly rather than hanging.  We patch the lock in apply() to use wait_seconds=0
    so the test doesn't take 30 s.
    """
    from atomic_agents._locks import AgentLock
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test",
                "Test body", "2026-03-01")

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    # Manually set status to completed with a dreamed memory dir
    # (already done by start())
    assert result.status == "completed"

    # Acquire the agent lock directly, simulating an in-flight call()
    held_lock = AgentLock(agent_dir, wait_seconds=0)
    held_lock.acquire()
    try:
        # Patch AgentLock in dream.py to use wait_seconds=0 so apply() fails fast
        original_agent_lock = __import__("atomic_agents.dream", fromlist=["AgentLock"]).AgentLock

        def fast_failing_lock(agent_root, wait_seconds=30):
            return original_agent_lock(agent_root, wait_seconds=0)

        with patch("atomic_agents.dream.AgentLock", side_effect=fast_failing_lock):
            with pytest.raises(AgentLockBusy):
                runner.apply(result.dream_id)
    finally:
        held_lock.release()


def test_dream_discard_refuses_dotdot_dream_id(agents_root):
    """--discard ../../persona must raise DreamNotFound (invalid format), not rmtree."""
    runner = DreamRunner(agents_root, "dreamer")
    with pytest.raises(DreamNotFound, match="Invalid dream_id"):
        runner.discard("../../persona")


def test_dream_discard_refuses_absolute_path_as_dream_id(agents_root):
    """An absolute-path-style dream_id must be rejected before path construction."""
    runner = DreamRunner(agents_root, "dreamer")
    # Slashes are forbidden characters per the validation regex
    with pytest.raises(DreamNotFound, match="Invalid dream_id"):
        runner.discard("/etc/passwd")


def test_dream_discard_refuses_dream_id_with_slash(agents_root):
    """A dream_id containing a slash must be rejected (path separator not allowed)."""
    runner = DreamRunner(agents_root, "dreamer")
    with pytest.raises(DreamNotFound, match="Invalid dream_id"):
        runner.discard("some/nested/path")

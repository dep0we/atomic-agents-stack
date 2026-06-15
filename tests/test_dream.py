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


def _write_note(
    agent_dir: Path,
    filename: str,
    note_type: str,
    name: str,
    body: str,
    last_seen: str,
    pinned: bool = False,
    tags: list | None = None,
) -> Path:
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
        {
            "filename": "feedback_debt_priority.md",
            "meta": {"type": "feedback", "name": "Debt priority rule"},
            "body": "Body A",
        },
        {
            "filename": "feedback_debt_prioritization.md",
            "meta": {"type": "feedback", "name": "Debt prioritization"},
            "body": "Body B",
        },
        {
            "filename": "decision_api_design.md",
            "meta": {"type": "decision", "name": "API design choice"},
            "body": "Body C",
        },
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
    _write_note(
        agent_dir,
        "feedback_test.md",
        "feedback",
        "Test feedback",
        "Test body",
        "2026-01-01",
    )

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
    _write_note(
        agent_dir,
        "feedback_test.md",
        "feedback",
        "Test feedback",
        "Test body",
        "2026-01-01",
    )

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    report_path = agent_dir / "dreams" / result.dream_id / "report.md"
    assert report_path.exists()
    content = report_path.read_text()
    assert "Dream Report" in content
    assert "Summary" in content


def test_dream_pipeline_reads_journal_entry_through_real_adapter(
    agents_root, monkeypatch
):
    """#427 regression: a dated journal entry nested in a month-subdir reaches the
    REAL DreamRunner.start() -> _run_pipeline journal adapter via query_by_date,
    carried to the LLM as path.name (NOT the nested path). Locks the subdir-loss
    fix and the real call-site flow — the conformance suite only proves the adapter
    via an inline reconstruction, not by driving the real dream pipeline.
    """
    agent_dir = agents_root / "dreamer"
    # A memory note so the contradiction/promotion stages (which embed the journal
    # filename + text into their prompts) actually run.
    _write_note(
        agent_dir,
        "feedback_test.md",
        "feedback",
        "Test feedback",
        "Test body",
        "2026-01-01",
    )
    # Journal entry nested in a YYYY-MM/ subdir, recent enough to be in lookback.
    recent = date.today() - timedelta(days=1)
    month_dir = recent.strftime("%Y-%m")
    entry_name = f"{recent.isoformat()}.md"
    jdir = agent_dir / "journal" / month_dir
    jdir.mkdir(parents=True)
    (jdir / entry_name).write_text(
        "Journal narrative DREAM_JOURNAL_SENTINEL.", encoding="utf-8"
    )

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    assert result.status == "completed"
    # Flatten every prompt the real pipeline handed to the LLM.
    all_prompts = (
        " ".join(str(a) for call in mock_llm.call_args_list for a in call.args)
        + " "
        + " ".join(
            str(v) for call in mock_llm.call_args_list for v in call.kwargs.values()
        )
    )
    # The entry's text reached a real prompt -> the real adapter read it off disk.
    assert "DREAM_JOURNAL_SENTINEL" in all_prompts
    # ...carried as the bare filename (path.name), NOT the nested subdir path.
    assert entry_name in all_prompts
    assert f"{month_dir}/{entry_name}" not in all_prompts


def test_dream_apply_atomically_swaps_directories(agents_root, monkeypatch):
    """Apply: old memory/ is archived, dreamed memory/ becomes live memory/."""
    agent_dir = agents_root / "dreamer"
    _write_note(
        agent_dir, "feedback_test.md", "feedback", "Test", "Test body", "2026-03-01"
    )

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
            memory_count=0,
            journal_lookback_days=30,
            journal_count=0,
            log_lookback_days=30,
            log_line_count=0,
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
    _write_note(
        agent_dir, "feedback_test.md", "feedback", "Test", "Test body", "2026-03-01"
    )

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
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test", "Body", "2026-03-01")

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
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test", "Body", "2026-03-01")

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
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test", "Body", "2026-03-01")

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


def _hold_dream_lock_for_test(
    dreams_dir_str: str, hold_seconds: float, ready_path: str
) -> None:
    """Module-level helper for multiprocessing spawn (must be pickle-able)."""
    import time as _time
    from atomic_agents.locks import FilesystemLockBackend

    backend = FilesystemLockBackend(Path(dreams_dir_str))
    handle = backend.acquire("", timeout=0)
    Path(ready_path).write_text("ready")
    _time.sleep(hold_seconds)
    backend.release(handle)


def test_dream_concurrent_run_blocked_by_lock(agents_root, monkeypatch):
    """Second dream while first holds the lock raises DreamInProgress via DreamRunner.start().

    Post-#60 PR 2: exercises the PRODUCTION callsite in ``dream.start()``
    rather than an inline reimplementation of the wrap. A child process
    holds the dream lock; the parent's ``start()`` call hits the kernel
    flock collision, raises LockBusy internally, and ``start()`` wraps
    it in DreamInProgress with PEP-3134 chaining.
    """
    import multiprocessing
    import time as _time
    from atomic_agents.exceptions import LockBusy

    agent_dir = agents_root / "dreamer"
    dreams_dir = agent_dir / "dreams"
    dreams_dir.mkdir(parents=True, exist_ok=True)

    ready = agents_root / ".dream-concurrent-ready"
    child = multiprocessing.Process(
        target=_hold_dream_lock_for_test, args=(str(dreams_dir), 2.0, str(ready))
    )
    child.start()
    try:
        deadline = _time.monotonic() + 10.0
        while not ready.exists():
            if _time.monotonic() >= deadline:
                raise AssertionError("child never wrote sentinel")
            _time.sleep(0.02)
        with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
            mock_llm.return_value = _no_op_response()
            # dream_lock_timeout=0 forces fail-fast against the held lock.
            runner = DreamRunner(agents_root, "dreamer", dream_lock_timeout=0.0)
            with pytest.raises(DreamInProgress) as exc_info:
                runner.start()
        assert isinstance(exc_info.value.__cause__, LockBusy)
    finally:
        child.join(timeout=5)
        assert child.exitcode == 0, f"child crashed with exitcode {child.exitcode}"


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
        _write_note(
            agent_dir,
            f"feedback_note_{i}.md",
            "feedback",
            f"Note {i}",
            "Body " * 100,
            "2026-03-01",
        )

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
    _write_note(
        agent_dir, "feedback_test.md", "feedback", "Test", "Body " * 100, "2026-03-01"
    )

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "capped2")
        result = runner.start(critical=True)

    assert result.status == "completed"


def test_dream_failed_pipeline_preserves_partial_output(agents_root, monkeypatch):
    """Exception during synthesis → manifest=failed, error captured, partial output preserved."""
    agent_dir = agents_root / "dreamer"
    _write_note(agent_dir, "feedback_test.md", "feedback", "Test", "Body", "2026-03-01")

    # Fail on the first synthesis call (which is the only call when there's 1 note
    # and no duplicate clusters needing LLM checks)
    def mock_llm_side_effect(*args, **kwargs):
        raise RuntimeError("Simulated synthesis failure")

    with patch("atomic_agents.dream._llm.call_llm", side_effect=mock_llm_side_effect):
        runner = DreamRunner(agents_root, "dreamer")
        with pytest.raises(RuntimeError, match="Simulated"):
            runner.start()

    # Find the most recent dream dir
    dreams = (
        list((agent_dir / "dreams").iterdir())
        if (agent_dir / "dreams").exists()
        else []
    )
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
    from atomic_agents.locks import FilesystemLockBackend
    from atomic_agents.memory.filesystem import FilesystemBackend

    agent_dir = agents_root / "dreamer"
    _write_note(
        agent_dir, "feedback_test.md", "feedback", "Test", "Test body", "2026-03-01"
    )

    with patch("atomic_agents.dream._llm.call_llm") as mock_llm:
        mock_llm.return_value = _no_op_response()
        runner = DreamRunner(agents_root, "dreamer")
        result = runner.start()

    # Manually set status to completed with a dreamed memory dir
    # (already done by start())
    assert result.status == "completed"

    # Acquire the agent lock directly, simulating an in-flight call().
    # Post-#60 PR 2: agent.call() acquires via FilesystemLockBackend
    # on ``<agent_root>/.lock`` (empty-name). This held backend uses the
    # same scope_root + empty name so the held .lock collides with
    # apply_staging's acquire.
    held_backend = FilesystemLockBackend(agent_dir)
    held_handle = held_backend.acquire("", timeout=0)
    try:
        # Force apply_staging's internal lock acquire to fail fast
        # (default is 30s — too long for a held-lock test). The
        # constructor kwarg ``apply_staging_lock_timeout`` is the
        # documented override surface (per-instance, immutable
        # post-construction — class-level monkey-patching was rejected
        # by Step 9.1 security review as a process-wide mutation risk).
        # Patch ``FilesystemBackend.__init__`` so the existing runner's
        # internal backend re-instantiates with the fail-fast value.
        runner._backend = FilesystemBackend(agent_dir, apply_staging_lock_timeout=0.0)
        with pytest.raises(AgentLockBusy):
            runner.apply(result.dream_id)
    finally:
        held_backend.release(held_handle)


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


def test_read_log_lines_degrades_on_read_error(tmp_path):
    """_read_log_lines degrades to an empty signal on LogBackendReadError.

    spec/22 read-failure addendum (#497): the log read runs BEFORE the dream
    cost gate and any LLM batch, so a raise here cannot leak uncosted spend —
    but it WOULD hard-crash a dream run. Dream consolidation is analysis, not a
    control gate: it degrades (loses the log signal, completes) rather than
    crashing, matching the cost reader and dashboard.
    """
    from atomic_agents import LogBackendReadError
    from atomic_agents.dream import _read_log_lines

    mock_backend = MagicMock()
    mock_backend.query.side_effect = LogBackendReadError("corrupt log")

    result = _read_log_lines(tmp_path, 7, log_backend=mock_backend, agent_name="alice")
    assert result == []
    # False-green guard: prove the backend was consulted and the exception
    # path was exercised (not the log_backend-is-None legacy walk).
    assert mock_backend.query.called

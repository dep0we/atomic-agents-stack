"""Integration tests pinning the #61 PR 2 log-wiring contract.

These tests assert that the production log sites + ``doctor.check_log_backend``
route through the LogBackend Protocol instead of writing JSONL directly.
Conformance + filesystem-shape tests live in
``tests/test_log_protocol_conformance.py`` and ``tests/test_log_filesystem_backend.py``;
this file pins the *wiring* — every site reaches the backend via the
right path, the operator-pinned ``log_backend=`` kwarg is honored,
and the byte-for-byte on-disk invariant from PR 1 still holds end-to-end.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.logs import (
    FilesystemLogBackend,
    LogBackend,
    LogQuery,
    RunRecord,
)


# ──────────────────────────────────────────────────────────────────
# Helpers

def _build_minimal_agent_dir(tmp_path: Path, name: str = "test") -> Path:
    """Construct the minimal on-disk shape AtomicAgent.__init__ requires."""
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n")
    (agent_dir / "tools.md").write_text(
        "## Write paths\n- memory/\n\n"
        "## Read-only paths\n(none)\n"
    )
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n"
    )
    (agent_dir / "memory").mkdir()
    return agent_dir


class _CapturingLogBackend:
    """Test-only LogBackend that records every append() call.

    Used to verify wiring without writing to disk — pins that the
    operator-supplied backend is actually consulted rather than the
    framework silently constructing its own (the
    DreamRunner-kwarg-drop trap shape).
    """

    backend_id = "capturing"

    def __init__(self):
        self.appended: list[RunRecord] = []

    def append(self, record: RunRecord) -> None:
        self.appended.append(record)

    def query(self, filter: LogQuery) -> list[RunRecord]:
        return list(self.appended)

    def tail(self, n: int) -> list[RunRecord]:
        return self.appended[-n:] if n > 0 else []

    def aggregate(self, filter, agg):
        return {}

    def delete_older_than(self, threshold) -> int:
        return 0

    def stats(self):
        from atomic_agents.logs import LogStats
        return LogStats(
            total_records=len(self.appended),
            oldest_ts=self.appended[0].ts if self.appended else None,
            newest_ts=self.appended[-1].ts if self.appended else None,
            size_bytes=None,
            records_today=0,
            records_this_month=0,
        )

    def capabilities(self):
        from atomic_agents.logs import LogCapabilities
        return LogCapabilities(
            supports_aggregation_pushdown=False,
            supports_streaming=False,
            supports_retention=False,
            durable=False,
        )


# ──────────────────────────────────────────────────────────────────
# Site 1: AtomicAgent public surface


def test_agent_has_public_log_backend_attribute(tmp_path, monkeypatch):
    """AtomicAgent exposes ``self.log_backend`` (public, mirrors ``self.memory`` / ``self.lock_backend``)."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    agent = AtomicAgent(name="test")

    assert hasattr(agent, "log_backend"), \
        "AtomicAgent must expose ``log_backend`` as a public attribute"
    assert isinstance(agent.log_backend, LogBackend), \
        "agent.log_backend must satisfy the LogBackend Protocol"
    assert isinstance(agent.log_backend, FilesystemLogBackend), \
        "default agent.log_backend is FilesystemLogBackend per spec/22"


def test_agent_log_backend_scoped_to_agent_root(tmp_path, monkeypatch):
    """append() produces the legacy ``<agent_root>/log/YYYY-MM/...`` artifact."""
    from atomic_agents.agent import AtomicAgent

    agent_root = _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    agent._log({"trigger": "agent_call", "model": "m", "status": "ok"})

    today = date.today()
    expected = (
        agent_root / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    assert expected.exists(), \
        f"Default backend MUST write to legacy path; got nothing at {expected}"


def test_agent_log_backend_kwarg_overrides_default(tmp_path, monkeypatch):
    """``AtomicAgent(..., log_backend=...)`` ALWAYS wins over env-var resolution.

    This is the DreamRunner-kwarg-drop trap mitigation — verify the
    operator-supplied backend is the one used, not a freshly
    constructed default.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    # Set env var to filesystem so we can prove kwarg overrides env.
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "filesystem")

    custom = _CapturingLogBackend()
    agent = AtomicAgent(name="test", log_backend=custom)

    assert agent.log_backend is custom, \
        "Constructor kwarg MUST win over env-var-resolved default"

    agent._log({"trigger": "agent_call", "model": "m", "status": "ok"})

    assert len(custom.appended) == 1, "Records must flow through the kwarg-supplied backend"


# ──────────────────────────────────────────────────────────────────
# Site 2: agent._log() builds RunRecord with primitive + run_id


def test_log_derives_primitive_from_trigger(tmp_path, monkeypatch):
    """_log() must derive ``primitive`` from the legacy ``trigger`` string."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    custom = _CapturingLogBackend()
    agent = AtomicAgent(name="test", log_backend=custom)

    cases = [
        ("agent_call", "agent_call"),
        ("outcome_iteration", "outcome_iteration"),
        ("helper", "helper"),
        ("helper_batch_reservation", "helper"),
        ("delegate", "delegate"),
        ("tool_call", "tool"),
        ("cost_warning", "cost_warning"),
        ("judgment", "judgment"),
        ("escalation_resolved", "escalation"),
        ("bogus_unknown", "other"),
    ]
    for trigger, expected_primitive in cases:
        custom.appended.clear()
        agent._log({"trigger": trigger, "model": "m", "status": "ok"})
        assert len(custom.appended) == 1
        assert custom.appended[0].primitive == expected_primitive, \
            f"trigger={trigger!r} should derive primitive={expected_primitive!r}"


def test_log_defaults_run_id_to_agent_run_id(tmp_path, monkeypatch):
    """_log() must default run_id to self.run_id when caller doesn't set it."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    custom = _CapturingLogBackend()
    agent = AtomicAgent(name="test", log_backend=custom)

    agent._log({"trigger": "agent_call", "model": "m", "status": "ok"})
    assert custom.appended[0].run_id == agent.run_id

    # Explicit override should win.
    custom.appended.clear()
    agent._log({"trigger": "agent_call", "model": "m", "status": "ok", "run_id": "explicit"})
    assert custom.appended[0].run_id == "explicit"


def test_log_preserves_legacy_byte_shape(tmp_path, monkeypatch):
    """A record written via _log() reads identically through the legacy
    ``dashboard.costs._record_from_dict`` parser — proves the byte-for-byte
    invariant holds end-to-end across PR 2's wiring."""
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.dashboard.costs import _record_from_dict

    agent_root = _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")
    agent._log({
        "trigger": "agent_call",
        "model": "claude-opus-4-7",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.001,
        "latency_ms": 1234,
        "status": "ok",
        "summary": "byte-for-byte",
    })

    today = date.today()
    log_path = (
        agent_root / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    )
    raw_line = log_path.read_text().strip()
    legacy = _record_from_dict(json.loads(raw_line), agent="test")
    assert legacy is not None
    assert legacy.trigger == "agent_call"
    assert legacy.model == "claude-opus-4-7"
    assert legacy.input_tokens == 100
    assert legacy.output_tokens == 50
    assert legacy.cost_usd == pytest.approx(0.001)


# ──────────────────────────────────────────────────────────────────
# Site 3: outcome.OutcomeRunner routes through agent.log_backend


def test_outcome_iteration_routes_through_agent_log_backend(tmp_path, monkeypatch):
    """OutcomeRunner._append_iteration_log writes through agent.log_backend,
    NOT directly via atomic_append_jsonl."""
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.outcome import OutcomeRunner

    agent_root = _build_minimal_agent_dir(tmp_path, "alice")
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    # Construct a OutcomeRunner and pin a capturing mock backend
    # on the AtomicAgent it builds internally.
    captured = _CapturingLogBackend()

    # Simulate the iteration-log code path directly: build an agent with
    # the capturing backend, then call _append_iteration_log via the
    # OutcomeRunner instance method against a fake record.
    from atomic_agents.outcome import IterationRecord

    runner = OutcomeRunner(
        agents_root=tmp_path,
        agent_name="alice",
        judge_model="gpt-5",
    )
    fake_agent = AtomicAgent(name="alice", log_backend=captured)
    record = IterationRecord(
        iteration=0,
        timestamp=datetime.now(timezone.utc).isoformat(),
        agent_response="test response",
        agent_input_tokens=10,
        agent_output_tokens=5,
        agent_cost_usd=0.0001,
        agent_latency_ms=100,
        judge_response_raw="judge raw",
        judge_verdict=True,
        judge_input_tokens=20,
        judge_output_tokens=8,
        judge_cost_usd=0.0002,
        artifact_path=None,
    )
    runner._append_iteration_log(fake_agent, "run-xyz", record, True)

    assert len(captured.appended) == 1
    rec = captured.appended[0]
    assert rec.primitive == "outcome_iteration"
    assert rec.run_id == "run-xyz"
    assert rec.trigger == "outcome_iteration"
    assert rec.extra["iteration"] == 0
    assert rec.extra["satisfied"] is True


# ──────────────────────────────────────────────────────────────────
# Site 4: _costs.sum_cost_for_period routes through backend


def test_sum_cost_routes_through_backend_when_provided(tmp_path):
    """_costs.sum_cost_for_period with backend= queries via the Protocol."""
    from atomic_agents import _costs

    captured = _CapturingLogBackend()
    # Inject a record into the capturing backend.
    ts_today = datetime.now().astimezone().isoformat()
    captured.appended.append(RunRecord(
        ts=ts_today,
        run_id="r1",
        primitive="agent_call",
        status="ok",
        summary="t",
        model="claude-opus-4-7",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.42,
        cost_source="actor",
    ))

    total = _costs.sum_cost_for_period(
        tmp_path / "log", "today", source="actor", backend=captured,
    )
    assert total == pytest.approx(0.42)


def test_sum_cost_falls_back_to_filesystem_when_no_backend(tmp_path):
    """sum_cost_for_period without backend= uses the legacy filesystem walk
    (backward compat for external callers)."""
    from atomic_agents import _costs

    today = date.today()
    log_dir = tmp_path / "alice" / "log"
    day_file = log_dir / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    day_file.parent.mkdir(parents=True, exist_ok=True)
    day_file.write_text(json.dumps({
        "ts": datetime.now().astimezone().isoformat(),
        "cost_usd": 0.99,
        "cost_source": "actor",
    }) + "\n")

    total = _costs.sum_cost_for_period(log_dir, "today", source="actor")
    assert total == pytest.approx(0.99)


# ──────────────────────────────────────────────────────────────────
# Site 5: dream.DreamRunner threads log_backend through


def test_dream_runner_log_backend_kwarg_threaded(tmp_path, monkeypatch):
    """DreamRunner accepts log_backend= and stores it as self._log_backend
    — the kwarg-drop-equivalent trap mitigation for the dream pipeline."""
    from atomic_agents.dream import DreamRunner

    _build_minimal_agent_dir(tmp_path, "alice")
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    custom = _CapturingLogBackend()
    runner = DreamRunner(
        agents_root=tmp_path,
        agent_name="alice",
        log_backend=custom,
    )
    assert runner._log_backend is custom, \
        "DreamRunner MUST honor log_backend= kwarg without re-constructing"


# ──────────────────────────────────────────────────────────────────
# Site 6: dashboard.costs.load_runs routes through backend


def test_dashboard_load_runs_routes_through_default_backend(tmp_path, monkeypatch):
    """dashboard/costs.load_runs reads via get_default_log_backend so
    operator-pinned backends drive dashboard reads (no split-brain)."""
    from atomic_agents.dashboard.costs import load_runs

    today = date.today()
    log_dir = tmp_path / "alice" / "log" / today.strftime("%Y-%m")
    log_dir.mkdir(parents=True)
    log_dir.joinpath(f"{today.isoformat()}.jsonl").write_text(
        json.dumps({
            "ts": datetime.now().astimezone().isoformat(),
            "trigger": "agent_call",
            "model": "claude-opus-4-7-20260101",
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.5,
            "status": "ok",
            "summary": "test",
        }) + "\n"
    )

    runs = load_runs(tmp_path, "alice", today, today)
    assert len(runs) == 1
    assert runs[0].cost_usd == pytest.approx(0.5)


# ──────────────────────────────────────────────────────────────────
# Site 7: doctor.check_log_backend


def test_doctor_check_log_backend_filesystem_pass(tmp_path, monkeypatch):
    """check_log_backend returns PASS for the default filesystem backend."""
    from atomic_agents.doctor import check_log_backend

    agent_root = _build_minimal_agent_dir(tmp_path, "alice")
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND", raising=False)

    result = check_log_backend(agent_root)
    assert result.status == "pass"
    assert result.detail["backend_id"] == "filesystem"
    assert "total_records" in result.detail


def test_doctor_check_log_backend_unknown_id_fail(tmp_path, monkeypatch):
    """check_log_backend returns FAIL with known-id list (incl. sqlite forward-pointer)."""
    from atomic_agents.doctor import check_log_backend

    agent_root = _build_minimal_agent_dir(tmp_path, "alice")
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "bogus_typo")

    result = check_log_backend(agent_root)
    assert result.status == "fail"
    assert "bogus_typo" in result.message
    assert "sqlite" in result.message  # forward-pointer
    assert "filesystem" in result.message


def test_doctor_check_log_backend_sqlite_passes_after_pr3(tmp_path, monkeypatch):
    """check_log_backend for ``sqlite`` returns PASS post-PR-3.

    Prior to PR 3 this raised FAIL with "not yet registered"; PR 3
    registered SQLiteLogBackend and the doctor now reports its stats.
    """
    from atomic_agents.doctor import check_log_backend

    agent_root = _build_minimal_agent_dir(tmp_path, "alice")
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "sqlite")
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND_URL", raising=False)

    result = check_log_backend(agent_root)
    assert result.status == "pass"
    assert result.detail["backend_id"] == "sqlite"
    assert "total_records" in result.detail


# ──────────────────────────────────────────────────────────────────
# Step 11 adversarial follow-ups — gaps the Sonnet review surfaced.


def test_count_provenance_wiring_handles_legacy_records(tmp_path, monkeypatch):
    """`_count_provenance` (PR 2 wiring) reads helper records without
    primitive set — pre-PR-2 records had only trigger='helper'. Step 11
    P0 #2: the original LogQuery(primitive='helper') would exclude these
    because RunRecord.from_dict defaults missing primitive to 'other',
    and the backend's predicate evaluator runs BEFORE the belt-and-
    suspenders check in the function body. Fix removes the primitive
    filter from LogQuery and filters in-Python."""
    from atomic_agents.dashboard.quality import _count_provenance

    today = date.today()
    log_dir = tmp_path / "alice" / "log" / today.strftime("%Y-%m")
    log_dir.mkdir(parents=True)
    ts = datetime.now().astimezone().isoformat()
    # Legacy record: has trigger="helper" but NO primitive field.
    log_dir.joinpath(f"{today.isoformat()}.jsonl").write_text(
        json.dumps({
            "ts": ts, "trigger": "helper", "model": "claude-haiku",
            "input_tokens": 100, "output_tokens": 50, "status": "ok",
            "summary": "legacy helper", "provenance_preserved": True,
        }) + "\n"
        + json.dumps({
            "ts": ts, "trigger": "helper", "model": "claude-haiku",
            "input_tokens": 100, "output_tokens": 50, "status": "ok",
            "summary": "legacy helper unpreserved",
        }) + "\n"
    )
    preserved, total = _count_provenance(tmp_path, "alice", today, today)
    assert total == 2, "Legacy helper records must be counted via trigger fallback"
    assert preserved == 1


def test_outcome_runner_kwarg_threaded_to_internal_agent(tmp_path, monkeypatch):
    """OutcomeRunner accepts ``log_backend=`` kwarg and threads it to the
    internal AtomicAgent constructed at ``run()`` — Step 11 P0 #3
    (DreamRunner-kwarg-drop trap mitigation at OutcomeRunner)."""
    from atomic_agents.outcome import OutcomeRunner

    _build_minimal_agent_dir(tmp_path, "alice")
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    custom = _CapturingLogBackend()
    runner = OutcomeRunner(
        agents_root=tmp_path,
        agent_name="alice",
        log_backend=custom,
    )
    assert runner._log_backend is custom, \
        "OutcomeRunner MUST honor log_backend= kwarg"


def test_sum_cost_with_real_filesystem_backend(tmp_path):
    """End-to-end: sum_cost_for_period(backend=FilesystemLogBackend(...))
    correctly sums today's records. Step 11 specialist gap: the
    integration test for the backend path used _CapturingLogBackend
    which ignores filters; this pins the real filesystem semantic."""
    from atomic_agents import _costs
    from atomic_agents.logs import FilesystemLogBackend, RunRecord

    today = date.today()
    log_dir = tmp_path / "alice" / "log"
    backend = FilesystemLogBackend(tmp_path / "alice")
    backend.append(RunRecord(
        ts=datetime.now().astimezone().isoformat(),
        run_id="r1", primitive="agent_call", status="ok",
        summary="t", model="m", input_tokens=10, output_tokens=5,
        cost_usd=0.25, cost_source="actor",
    ))
    backend.append(RunRecord(
        ts=datetime.now().astimezone().isoformat(),
        run_id="r2", primitive="agent_call", status="ok",
        summary="t", model="m", input_tokens=10, output_tokens=5,
        cost_usd=0.50, cost_source="actor",
    ))
    backend.append(RunRecord(
        ts=datetime.now().astimezone().isoformat(),
        run_id="r3", primitive="judgment", status="ok",
        summary="t", model="m", input_tokens=10, output_tokens=5,
        cost_usd=0.10, cost_source="judge",
    ))
    # Filesystem backend preserves legacy walk semantic (Step 11 P0
    # #4 mitigation) — sums by file location, filters by cost_source.
    total_actor = _costs.sum_cost_for_period(
        log_dir, "today", source="actor", backend=backend,
    )
    assert total_actor == pytest.approx(0.75)
    total_judge = _costs.sum_cost_for_period(
        log_dir, "today", source="judge", backend=backend,
    )
    assert total_judge == pytest.approx(0.10)


def test_sum_cost_filesystem_tolerates_malformed_ts_record(tmp_path):
    """Step 11 P0 #4: filesystem backend MUST count records with
    malformed ts because the legacy file-walk semantic was the
    safety-load-bearing contract for cost guardrails. Records with
    ``ts="x"`` land in the day file's filename-derived date even
    though their ts content is unparseable."""
    from atomic_agents import _costs
    from atomic_agents.logs import FilesystemLogBackend

    today = date.today()
    log_dir = tmp_path / "alice" / "log"
    day_file = log_dir / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
    day_file.parent.mkdir(parents=True, exist_ok=True)
    # Direct disk write with malformed ts — simulates legacy on-disk
    # records or rotated/corrupted writes.
    day_file.write_text(
        json.dumps({"cost_usd": 0.42, "cost_source": "actor", "ts": "x"}) + "\n"
    )
    backend = FilesystemLogBackend(tmp_path / "alice")
    total = _costs.sum_cost_for_period(
        log_dir, "today", source="actor", backend=backend,
    )
    # Legacy semantic preserved — the malformed-ts record counts.
    assert total == pytest.approx(0.42)


def test_doctor_check_log_backend_redacts_url_credential(tmp_path, monkeypatch):
    """check_log_backend's error message MUST NOT echo URL credentials.
    Step 9.1 security CRITICAL #3 (PR 2). Parity with check_lock_backend.

    Uses an UNKNOWN scheme with a credential — guaranteed to error out
    of ``make_sqlite_backend_from_url`` regardless of which backend ID
    is set, so the redaction is exercised."""
    from atomic_agents.doctor import check_log_backend

    agent_root = _build_minimal_agent_dir(tmp_path, "alice")
    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "sqlite")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_LOG_BACKEND_URL",
        # Wrong scheme triggers ValueError in URL parser.
        "postgres://super-secret-token@host/db",
    )

    result = check_log_backend(agent_root)
    # Backend construction failed; message MUST NOT leak credential.
    assert result.status == "fail"
    assert "super-secret-token" not in result.message
    if "url" in (result.detail or {}):
        assert "super-secret-token" not in result.detail["url"]

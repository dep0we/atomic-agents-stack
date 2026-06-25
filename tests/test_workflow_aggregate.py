"""Tests for aggregate_workflow() and WorkflowSummary (spec/22 versioned normative
addendum, issue #622 PR1).

Three test groups:
1. WorkflowSummary field correctness (structure, defaults).
2. aggregate_workflow count-once semantics (the correctness contract):
   - delegate-double-count negative control (MUST NOT count the mirror record)
   - helper-undercount guard (MUST count helper records)
   - embed_cost inclusion guard (MUST count embed_cost records)
   - propagation-leakage negative control (workflow_id=None runs MUST NOT appear)
   - single-agent home case (MUST work with one agent dir, no delegation)
   - degraded-read banner (MUST surface cost_data_degraded on LogBackendReadError)
3. RunRecord workflow_id round-trip (field canonicalization, _CANONICAL_FIELDS).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from atomic_agents.dashboard.costs import WorkflowSummary, aggregate_workflow
from atomic_agents.logs import LogBackendReadError
from atomic_agents.logs.types import (
    PRIMITIVE_DELEGATE,
    RunRecord,
    _CANONICAL_FIELDS,
)


# ──────────────────────────────────────────────────────────────────
# Self-containment guard


@pytest.fixture(autouse=True)
def _neutralize_log_backend_env(monkeypatch):
    """Pin these tests to the filesystem default LogBackend.

    aggregate_workflow() resolves its backend via get_default_log_backend(),
    which honors ATOMIC_AGENTS_LOG_BACKEND / ATOMIC_AGENTS_LOG_BACKEND_URL from
    the environment. These tests write filesystem JSONL fixtures and assert the
    backend they actually exercise; if either env var leaks in from a developer
    shell, CI, or another test's monkeypatch.setenv (e.g. test_log_sqlite_backend
    sets ATOMIC_AGENTS_LOG_BACKEND=sqlite), aggregate_workflow would read the
    wrong store and return total=0.0 with the filesystem fixtures invisible.
    Deleting both vars makes every test in this module self-contained.
    """
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND_URL", raising=False)


# ──────────────────────────────────────────────────────────────────
# Helpers


def _ts(hour: int = 12) -> str:
    return datetime(2026, 5, 15, hour, tzinfo=timezone.utc).isoformat()


def _make_jsonl(
    run_id: str = "r",
    trigger: str = "agent_call",
    primitive: str = "agent_call",
    cost_usd: float | None = 0.001,
    status: str = "ok",
    workflow_id: str | None = None,
    agent_name: str | None = None,
    delegated_agent: str | None = None,
    delegate_run_id: str | None = None,
) -> dict:
    rec: dict[str, Any] = {
        "ts": _ts(),
        "run_id": run_id,
        "primitive": primitive,
        "trigger": trigger,
        "status": status,
        "summary": "test",
        "model": "claude-haiku-4-5",
        "input_tokens": 100,
        "output_tokens": 50,
    }
    if cost_usd is not None:
        rec["cost_usd"] = cost_usd
    if workflow_id is not None:
        rec["workflow_id"] = workflow_id
    if agent_name is not None:
        rec["agent_name"] = agent_name
    # Mirror-only marker keys: present ONLY on the coordinator's delegate mirror
    # record (agent.py delegate() logging). The delegated child's own terminal
    # record — which is ALSO trigger='delegate' — never carries these.
    if delegated_agent is not None:
        rec["delegated_agent"] = delegated_agent
    if delegate_run_id is not None:
        rec["delegate_run_id"] = delegate_run_id
    return rec


def _write_agent_log(agent_dir: Path, records: list[dict]) -> None:
    """Write JSONL records to an agent's log directory (filesystem backend layout)."""
    log_dir = agent_dir / "log" / "2026-05"
    log_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "model.md").write_text("## Model\nclaude-haiku-4-5\n")
    log_file = log_dir / "2026-05-15.jsonl"
    with log_file.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ──────────────────────────────────────────────────────────────────
# WorkflowSummary dataclass


def test_workflow_summary_defaults():
    """cost_data_degraded defaults to False; other fields are provided."""
    ws = WorkflowSummary(
        total=1.23,
        cost_by_agent={"coordinator": 0.80, "specialist": 0.43},
        run_count=2,
        errors=0,
    )
    assert ws.total == 1.23
    assert ws.cost_by_agent == {"coordinator": 0.80, "specialist": 0.43}
    assert ws.run_count == 2
    assert ws.errors == 0
    assert ws.cost_data_degraded is False


def test_workflow_summary_with_degraded():
    ws = WorkflowSummary(
        total=0.0,
        cost_by_agent={},
        run_count=0,
        errors=0,
        cost_data_degraded=True,
    )
    assert ws.cost_data_degraded is True


# ──────────────────────────────────────────────────────────────────
# aggregate_workflow — count-once correctness


def test_aggregate_workflow_empty_agents_root(tmp_path):
    """No agent dirs → zero totals, no error."""
    ws = aggregate_workflow(tmp_path, "wf-001")
    assert ws.total == 0.0
    assert ws.run_count == 0
    assert ws.errors == 0
    assert ws.cost_data_degraded is False


def test_aggregate_workflow_no_matching_records(tmp_path):
    """Agent dirs exist but none carry the queried workflow_id → zero totals."""
    agent = tmp_path / "coordinator"
    _write_agent_log(
        agent,
        [_make_jsonl(run_id="r1", cost_usd=0.005, workflow_id=None)],
    )
    ws = aggregate_workflow(tmp_path, "wf-999")
    assert ws.total == 0.0
    assert ws.run_count == 0
    assert ws.cost_data_degraded is False


def test_aggregate_workflow_single_agent(tmp_path):
    """Home-user case: one agent, no delegation — MUST return correct total."""
    agent = tmp_path / "alice"
    _write_agent_log(
        agent,
        [
            _make_jsonl(
                run_id="r1",
                trigger="agent_call",
                cost_usd=0.010,
                workflow_id="wf-1",
            )
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-1")
    assert ws.total == pytest.approx(0.010, abs=1e-9)
    assert ws.run_count == 1
    assert ws.errors == 0
    assert "alice" in ws.cost_by_agent
    assert ws.cost_data_degraded is False


def test_aggregate_workflow_delegate_count_once(tmp_path):
    """CRITICAL: count delegated spend EXACTLY once.

    This fixture mirrors the REAL runtime record shapes (the earlier version of
    this test fabricated the child as trigger='agent_call', which the framework
    NEVER produces for a delegated child — that false-green masked the count-once
    bug). At runtime:
      - the delegated child is constructed with trigger='delegate', so its own
        terminal ok-record is trigger='delegate', carries the child's real
        cost_usd, IS workflow_id-stamped, and has NO mirror marker;
      - the coordinator's mirror record is ALSO trigger='delegate', carries the
        same child cost_usd, has the mirror marker (delegated_agent /
        delegate_run_id), and is NOT workflow_id-stamped.

    The coordinator has:
    - One agent_call record ($0.005, workflow_id=wf-1) — INCLUDE
    - One delegate MIRROR record ($0.020, marker present, workflow_id NOT stamped)
      — EXCLUDE (both by the marker guard and by the LogQuery filter)

    The specialist (child) has:
    - One trigger='delegate' terminal record ($0.020, NO marker, workflow_id=wf-1)
      — INCLUDE (this is the child's real spend; the bug was dropping it)

    Correct total: $0.005 + $0.020 = $0.025.
    Wrong total under the old over-broad trigger guard: $0.005 (child dropped).
    Wrong total if the mirror were double-counted: $0.045.
    """
    coordinator = tmp_path / "coordinator"
    specialist = tmp_path / "specialist"

    _write_agent_log(
        coordinator,
        [
            # agent_call (the coordinator's own LLM work) — INCLUDE
            _make_jsonl(
                run_id="coord-call",
                trigger="agent_call",
                cost_usd=0.005,
                workflow_id="wf-1",
            ),
            # delegate MIRROR record — has the mirror marker, NOT workflow_id-stamped.
            # Excluded by the marker guard (and never enters via the LogQuery filter).
            _make_jsonl(
                run_id="coord-delegate",
                trigger=PRIMITIVE_DELEGATE,
                cost_usd=0.020,
                workflow_id=None,
                delegated_agent="specialist",
                delegate_run_id="spec-call",
            ),
        ],
    )
    _write_agent_log(
        specialist,
        [
            # specialist's own terminal record — trigger='delegate' (NOT agent_call),
            # NO mirror marker, workflow_id-stamped. This is the child's real spend
            # and MUST be counted exactly once.
            _make_jsonl(
                run_id="spec-call",
                trigger=PRIMITIVE_DELEGATE,
                cost_usd=0.020,
                workflow_id="wf-1",
            )
        ],
    )

    ws = aggregate_workflow(tmp_path, "wf-1")
    # coordinator agent_call ($0.005) + specialist's delegate-trigger child ($0.020)
    assert ws.total == pytest.approx(0.025, abs=1e-9), (
        f"Count-once failed: expected $0.025 but got ${ws.total:.6f}. "
        "The coordinator's delegate MIRROR record MUST be excluded (by its marker), "
        "and the delegated child's own trigger='delegate' record MUST be counted."
    )
    assert ws.run_count == 2
    assert "coordinator" in ws.cost_by_agent
    assert ws.cost_by_agent["coordinator"] == pytest.approx(0.005, abs=1e-9)
    assert "specialist" in ws.cost_by_agent
    assert ws.cost_by_agent["specialist"] == pytest.approx(0.020, abs=1e-9)


def test_aggregate_workflow_child_delegate_record_is_counted(tmp_path):
    """A bare trigger='delegate' record with NO mirror marker IS the child's real
    spend and MUST be counted.

    This directly guards against the over-broad `if rec.trigger == PRIMITIVE_DELEGATE:
    continue` guard that would drop the delegated child's spend entirely. The
    discriminator is the mirror marker (delegate_run_id / delegated_agent), not bare
    trigger.
    """
    child = tmp_path / "specialist"
    _write_agent_log(
        child,
        [
            # Child's own delegate-trigger terminal record — no mirror marker.
            _make_jsonl(
                run_id="child-call",
                trigger=PRIMITIVE_DELEGATE,
                cost_usd=0.010,
                workflow_id="wf-2",
            ),
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-2")
    assert ws.total == pytest.approx(0.010, abs=1e-9), (
        "A trigger='delegate' record WITHOUT the mirror marker is the delegated "
        "child's real spend and MUST be counted. The exclusion guard keys on the "
        "mirror marker, NOT on bare trigger."
    )
    assert ws.run_count == 1


def test_aggregate_workflow_mirror_marker_excluded(tmp_path):
    """A trigger='delegate' record carrying the mirror marker IS excluded even if
    it somehow also carries workflow_id.

    The mirror is normally not workflow_id-stamped (so the LogQuery filter alone
    excludes it). This tests the in-loop belt-and-suspenders marker guard: if a
    marker-bearing record ever shows up in the result set, it is dropped.
    """
    agent = tmp_path / "coordinator"
    _write_agent_log(
        agent,
        [
            # agent_call record — INCLUDE
            _make_jsonl(
                run_id="r1",
                trigger="agent_call",
                cost_usd=0.010,
                workflow_id="wf-3",
            ),
            # mirror record WITH workflow_id (shouldn't happen per spec, but verify
            # the marker guard drops it regardless of the field being set)
            _make_jsonl(
                run_id="r2",
                trigger=PRIMITIVE_DELEGATE,
                cost_usd=0.010,
                workflow_id="wf-3",
                delegated_agent="specialist",
                delegate_run_id="spec-1",
            ),
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-3")
    # Only the agent_call ($0.010) — the marker-bearing mirror is excluded
    assert ws.total == pytest.approx(0.010, abs=1e-9), (
        "A trigger='delegate' record carrying the mirror marker MUST be excluded "
        "from the cost sum regardless of whether workflow_id is set."
    )
    assert ws.run_count == 1


def test_aggregate_workflow_helper_records_included(tmp_path):
    """helper records MUST be included in the count-once sum.

    The ruling: aggregate_workflow includes agent_call, helper, embed_cost, and the
    delegated child's own delegate-trigger record (any cost_source='actor' record
    that is NOT the coordinator's delegate mirror). tool_call records are never
    workflow_id-stamped and carry no cost_usd (tool spend is folded into the parent
    agent_call record), so they never enter the rollup. This test guards against an
    over-narrow exclusion that drops helpers.
    """
    agent = tmp_path / "researcher"
    _write_agent_log(
        agent,
        [
            _make_jsonl(
                run_id="main-call",
                trigger="agent_call",
                cost_usd=0.008,
                workflow_id="wf-3",
            ),
            _make_jsonl(
                run_id="helper-call",
                trigger="helper",
                primitive="helper",
                cost_usd=0.003,
                workflow_id="wf-3",
            ),
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-3")
    assert ws.total == pytest.approx(0.011, abs=1e-9), (
        "helper records MUST be included in the count-once sum. "
        "Under-count guard failed."
    )
    assert ws.run_count == 2


def test_aggregate_workflow_embed_cost_records_included(tmp_path):
    """embed_cost records MUST be included in the count-once sum.

    The ruling: embed_cost is one of the child-cost records that aggregate_workflow
    must include. This test guards against embed spend being silently dropped.
    """
    agent = tmp_path / "embedder"
    _write_agent_log(
        agent,
        [
            _make_jsonl(
                run_id="main-call",
                trigger="agent_call",
                cost_usd=0.005,
                workflow_id="wf-4",
            ),
            _make_jsonl(
                run_id="embed-cost",
                trigger="embed_cost",
                primitive="embed",
                cost_usd=0.002,
                workflow_id="wf-4",
            ),
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-4")
    assert ws.total == pytest.approx(0.007, abs=1e-9), (
        "embed_cost records MUST be included in the count-once sum. "
        "Embed spend under-count guard failed."
    )
    assert ws.run_count == 2


def test_aggregate_workflow_propagation_leakage_negative_control(tmp_path):
    """Records without workflow_id MUST NOT appear in the rollup.

    This tests that a previous call's records (workflow_id=None) are not
    contaminating the workflow query. The LogQuery(workflow_id=...) filter
    is the gate; this verifies the filter works end-to-end.
    """
    agent = tmp_path / "agent-a"
    _write_agent_log(
        agent,
        [
            # Unrelated call (no workflow_id) — MUST NOT appear
            _make_jsonl(run_id="unrelated", cost_usd=0.050, workflow_id=None),
            # Workflow-tagged call — MUST appear
            _make_jsonl(run_id="tagged", cost_usd=0.010, workflow_id="wf-5"),
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-5")
    assert ws.total == pytest.approx(0.010, abs=1e-9), (
        "Records without workflow_id MUST NOT contaminate the workflow rollup. "
        "Propagation-leakage guard failed."
    )
    assert ws.run_count == 1


def test_aggregate_workflow_stale_object_state_leakage(tmp_path):
    """Two separate workflows MUST NOT bleed into each other.

    This tests the no-self-storage invariant: workflow_id from one call
    must not appear in a subsequent call's records.
    """
    agent = tmp_path / "agent-b"
    _write_agent_log(
        agent,
        [
            _make_jsonl(run_id="r-wf1", cost_usd=0.010, workflow_id="wf-A"),
            _make_jsonl(run_id="r-wf2", cost_usd=0.020, workflow_id="wf-B"),
            _make_jsonl(run_id="r-none", cost_usd=0.030, workflow_id=None),
        ],
    )
    ws_a = aggregate_workflow(tmp_path, "wf-A")
    ws_b = aggregate_workflow(tmp_path, "wf-B")
    assert ws_a.total == pytest.approx(0.010, abs=1e-9)
    assert ws_b.total == pytest.approx(0.020, abs=1e-9)
    assert ws_a.run_count == 1
    assert ws_b.run_count == 1


def test_aggregate_workflow_errors_field(tmp_path):
    """errors counts RunRecord entries with status='error' in the filtered set."""
    agent = tmp_path / "error-agent"
    _write_agent_log(
        agent,
        [
            _make_jsonl(run_id="ok-r", status="ok", cost_usd=0.005, workflow_id="wf-6"),
            _make_jsonl(
                run_id="err-r", status="error", cost_usd=0.003, workflow_id="wf-6"
            ),
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-6")
    assert ws.errors == 1
    assert ws.run_count == 2


def test_aggregate_workflow_cost_data_degraded_on_read_error(tmp_path, monkeypatch):
    """cost_data_degraded=True when any agent backend raises LogBackendReadError.

    Mirrors the GlobalSummary degraded-read posture (#498). A partial read
    failure must NOT crash aggregate_workflow() — it must degrade gracefully
    and signal incompleteness via the flag.
    """
    agent_a = tmp_path / "agent-ok"
    agent_b = tmp_path / "agent-bad"

    _write_agent_log(
        agent_a,
        [_make_jsonl(run_id="ok-r", cost_usd=0.010, workflow_id="wf-7")],
    )
    # agent-bad dir with model.md so discover_agents picks it up
    agent_b.mkdir(parents=True, exist_ok=True)
    (agent_b / "model.md").write_text("## Model\nclaude-haiku-4-5\n")

    import atomic_agents.logs as logs_mod

    original_get = logs_mod.get_default_log_backend

    def _patched_get(root: Path):
        if root.name == "agent-bad":
            mock = MagicMock()
            mock.query.side_effect = LogBackendReadError("injected failure")
            return mock
        return original_get(root)

    monkeypatch.setattr(logs_mod, "get_default_log_backend", _patched_get)

    ws = aggregate_workflow(tmp_path, "wf-7")
    assert ws.cost_data_degraded is True, (
        "cost_data_degraded MUST be True when any agent backend raises "
        "LogBackendReadError. The read-failure posture must mirror GlobalSummary."
    )
    # The clean agent's records must still appear
    assert ws.total == pytest.approx(0.010, abs=1e-9), (
        "Records from the clean agent MUST still be included despite one "
        "agent's backend failing. Fail-soft posture required."
    )


# ──────────────────────────────────────────────────────────────────
# RunRecord workflow_id field — unit tests


def test_runrecord_workflow_id_in_canonical_fields():
    """workflow_id MUST be in _CANONICAL_FIELDS to prevent double-key in extra."""
    assert "workflow_id" in _CANONICAL_FIELDS, (
        "'workflow_id' must be in _CANONICAL_FIELDS so from_dict() routes it "
        "to the RunRecord field, not into extra{}. Without this, "
        "LogQuery.workflow_id filtering is broken on filesystem backends."
    )


def test_runrecord_workflow_id_round_trips():
    """workflow_id MUST survive to_dict() → from_dict() round-trip with no duplicate in extra."""
    rec = RunRecord(
        ts="2026-05-15T12:00:00+00:00",
        run_id="rt-1",
        primitive="agent_call",
        status="ok",
        summary="test",
        model="claude-haiku",
        input_tokens=10,
        output_tokens=5,
        workflow_id="wf-rt-123",
    )
    d = rec.to_dict()
    assert d.get("workflow_id") == "wf-rt-123", (
        "to_dict() must include workflow_id when set"
    )
    # workflow_id must NOT appear in extra as well (double-key hazard)
    assert "workflow_id" not in d.get("extra", {}), (
        "workflow_id must not appear in extra{} after round-trip — "
        "_CANONICAL_FIELDS exclusion is not working"
    )

    rec2 = RunRecord.from_dict(d)
    assert rec2.workflow_id == "wf-rt-123", (
        "from_dict() must populate workflow_id from the top-level key"
    )
    assert "workflow_id" not in rec2.extra, (
        "workflow_id must not land in extra{} on from_dict() read-back"
    )


def test_runrecord_workflow_id_none_round_trips():
    """workflow_id=None must round-trip correctly: omitted from to_dict(), None on from_dict()."""
    rec = RunRecord(
        ts="2026-05-15T12:00:00+00:00",
        run_id="rt-none",
        primitive="agent_call",
        status="ok",
        summary="test",
        model="claude-haiku",
        input_tokens=10,
        output_tokens=5,
    )
    d = rec.to_dict()
    assert "workflow_id" not in d, (
        "to_dict() must OMIT workflow_id when None (same None-omit pattern "
        "as conversation_id and idempotency_key)"
    )
    rec2 = RunRecord.from_dict(d)
    assert rec2.workflow_id is None


def test_runrecord_from_dict_workflow_id_not_in_extra():
    """from_dict() with an on-disk JSONL record carrying workflow_id must NOT
    route it into extra{} — it must land in the top-level field.

    This is the _CANONICAL_FIELDS exclusion test: if workflow_id is missing
    from _CANONICAL_FIELDS, from_dict() would route it to extra while ALSO
    extracting it explicitly (producing the double-key shape that
    test_runrecord_workflow_id_round_trips catches at to_dict time).
    """
    raw = {
        "ts": "2026-05-15T12:00:00+00:00",
        "run_id": "from-disk",
        "primitive": "agent_call",
        "status": "ok",
        "summary": "test",
        "model": "claude-haiku",
        "input_tokens": 10,
        "output_tokens": 5,
        "workflow_id": "wf-disk",
        "some_extra_key": "value",
    }
    rec = RunRecord.from_dict(raw)
    assert rec.workflow_id == "wf-disk"
    assert "workflow_id" not in rec.extra, (
        "workflow_id must be extracted to the top-level field, not extra{}"
    )
    assert "some_extra_key" in rec.extra, (
        "Unrelated extra keys must still flow into extra{}"
    )


# ──────────────────────────────────────────────────────────────────
# aggregate_workflow — via LogBackend protocol (not legacy reader)


def test_aggregate_workflow_uses_log_backend_not_legacy_reader(tmp_path, monkeypatch):
    """aggregate_workflow MUST query via LogBackend.query() — NEVER through
    the legacy _record_from_dict reader in dashboard/costs.py.

    The legacy reader does not have a workflow_id field, so routing through it
    would silently lose the workflow_id correlation and return wrong results.
    This test verifies aggregate_workflow() works end-to-end via the backend
    protocol path by checking that the correct record is returned when it would
    be missed by the legacy reader's _CANONICAL_KEYS filtering.
    """
    agent = tmp_path / "protocol-agent"
    _write_agent_log(
        agent,
        [
            _make_jsonl(run_id="target", cost_usd=0.015, workflow_id="wf-proto"),
            _make_jsonl(run_id="other", cost_usd=0.999, workflow_id=None),
        ],
    )
    ws = aggregate_workflow(tmp_path, "wf-proto")
    # Only the workflow-tagged record; the legacy reader path would lose the
    # workflow_id filter and potentially return both. Via the backend protocol,
    # LogQuery(workflow_id='wf-proto') returns exactly the tagged record.
    assert ws.run_count == 1
    assert ws.total == pytest.approx(0.015, abs=1e-9)


# ──────────────────────────────────────────────────────────────────
# PRODUCER integration — drive a REAL agent.call()/delegate() and assert the
# emitted records carry (or, for the mirror, do NOT carry) workflow_id.
#
# Every test above builds RunRecords by hand and exercises only the CONSUMER
# (aggregate_workflow). These tests close the false-green gap: the load-bearing
# propagation through call()'s ok-path and through delegate()'s child-call /
# coordinator-mirror split is verified by a running call, not by reading the diff.
# Per methodology "False-green tests need per-invocation negative control", the
# ok-path test includes a strip-RED control (workflow_id=None → not stamped).


def _build_agent_root(agents_root: Path, name: str) -> Path:
    agent_dir = agents_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "persona").mkdir(exist_ok=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (agent_dir / "tools.md").write_text(
        "## Write paths\n- memory/\n\n## Read-only paths\n(none)\n", encoding="utf-8"
    )
    (agent_dir / "memory").mkdir(exist_ok=True)
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n", encoding="utf-8"
    )
    return agents_root


class _FakeLockBackend:
    backend_id = "fake-inmemory"

    def __init__(self) -> None:
        self._held = False

    def acquire(self, name: str = "", timeout: float = 0.0):
        import time as _time

        from atomic_agents.exceptions import LockBusy
        from atomic_agents.locks.types import LockHandle

        if self._held:
            raise LockBusy(f"lock {name!r} already held (fake)")
        self._held = True
        handle = LockHandle(
            name=name, acquired_at=_time.time(), holder_pid=0, backend_state=object()
        )
        object.__setattr__(handle, "_backend", self)
        return handle

    def release(self, handle) -> None:
        self._held = False

    def renew(self, handle) -> bool:
        return True

    def is_held(self, name: str = "") -> bool:
        return self._held

    def capabilities(self):
        from atomic_agents.locks.types import LockCapabilities

        return LockCapabilities()

    def scope(self, sub_path: str):
        return self


def _fake_llm_response(text: str = "reply"):
    resp = MagicMock()
    resp.text = text
    resp.tool_uses = []
    resp.input_tokens = 7
    resp.output_tokens = 3
    resp.cache_hit_tokens = 0
    resp.cache_miss_tokens = 0
    resp.raw = {}
    return resp


def _make_call_agent(tmp_path: Path, name: str = "wfbot"):
    agents_root = _build_agent_root(tmp_path, name)
    from atomic_agents.agent import AtomicAgent

    return AtomicAgent(
        name=name,
        trigger="manual",
        agents_root=agents_root,
        lock_backend=_FakeLockBackend(),
    )


def _run_real_call(agent, *, work_item: str, workflow_id, log_sink: list):
    """Run a REAL agent.call() with LLM + heavy loading patched, capturing every
    emitted JSONL record into log_sink. The ok-path stamping logic itself runs
    unpatched — only the LLM, system-prompt assembly, and cost gate are stubbed.
    """
    from unittest.mock import patch

    def _capture_log(record: dict) -> None:
        log_sink.append(dict(record))

    kwargs: dict[str, Any] = {"work_item": work_item}
    if workflow_id is not None:
        kwargs["workflow_id"] = workflow_id

    with (
        patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response()),
        patch.object(agent, "_log", side_effect=_capture_log),
        patch.object(agent, "load"),
        patch.object(agent, "assemble_system_prompt", return_value="You are WfBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=True,
                action="ok",
                reason="cap",
                cost_data_degraded=False,
            ),
        ),
    ):
        return agent.call(**kwargs)


def test_real_call_ok_record_carries_workflow_id(tmp_path):
    """A real agent.call(workflow_id='wf') stamps workflow_id on the ok-path
    terminal record. Strip-RED control below proves it is the workflow_id kwarg
    doing the stamping, not an unconditional field."""
    agent = _make_call_agent(tmp_path)
    sink: list[dict] = []
    _run_real_call(agent, work_item="ping", workflow_id="wf-prod-1", log_sink=sink)

    ok = [r for r in sink if r.get("status") == "ok" and "cost_usd" in r]
    assert ok, (
        f"no ok-path record emitted; got triggers={[r.get('trigger') for r in sink]}"
    )
    assert all(r.get("workflow_id") == "wf-prod-1" for r in ok), (
        "ok-path terminal record MUST carry workflow_id when call(workflow_id=...) "
        f"is set. Records: {ok}"
    )


def test_real_call_no_workflow_id_is_not_stamped(tmp_path):
    """Strip-RED negative control: call() WITHOUT workflow_id must NOT stamp the
    field on the ok-path record. Pairs with the test above to prove the stamp is
    driven by the kwarg, not unconditional (per-invocation negative control)."""
    agent = _make_call_agent(tmp_path)
    sink: list[dict] = []
    _run_real_call(agent, work_item="ping", workflow_id=None, log_sink=sink)

    ok = [r for r in sink if r.get("status") == "ok" and "cost_usd" in r]
    assert ok, "no ok-path record emitted"
    assert all("workflow_id" not in r for r in ok), (
        "ok-path record MUST NOT carry workflow_id when call() is invoked without "
        f"one (None-omit, backward-compat). Records: {ok}"
    )


def test_real_delegate_threads_child_and_leaves_mirror_unstamped(tmp_path):
    """The load-bearing count-once producer invariant, end-to-end:

    (1) delegate(workflow_id='wf') threads workflow_id into the CHILD's call(), so
        the child's own terminal record will carry it.
    (2) the coordinator's MIRROR record is NOT stamped with workflow_id (that
        absence is what keeps the mirror out of the LogQuery-filtered rollup).

    The child's call() is spied (not run for real) so the test is fast and
    deterministic; the spy asserts the workflow_id kwarg actually reaches it.
    """
    from unittest.mock import patch

    from atomic_agents.agent import AtomicAgent
    from atomic_agents.types import Response

    # Two agents on disk so delegate() can resolve the target as a sibling.
    agents_root = _build_agent_root(tmp_path, "coordinator")
    _build_agent_root(tmp_path, "specialist")
    coordinator = AtomicAgent(
        name="coordinator",
        trigger="manual",
        agents_root=agents_root,
        lock_backend=_FakeLockBackend(),
    )
    coordinator.config.roster = {"specialist": "specialist"}

    child_call_kwargs: dict[str, Any] = {}

    def _spy_child_call(self, *args, **kwargs):
        # Only intercept the delegated child (a fresh instance constructed inside
        # delegate() with trigger='delegate'); record what it received.
        if getattr(self, "trigger", None) == "delegate":
            child_call_kwargs.update(kwargs)
            return Response(
                text="child-reply",
                model="claude-haiku-4-5-20251001",
                input_tokens=5,
                output_tokens=2,
                cost_usd=0.020,
            )
        raise AssertionError("unexpected non-delegate call() under spy")

    mirror_records: list[dict] = []

    def _capture_mirror(record: dict) -> None:
        mirror_records.append(dict(record))

    with (
        patch.object(AtomicAgent, "call", _spy_child_call),
        patch.object(coordinator, "_log", side_effect=_capture_mirror),
        patch.object(
            coordinator,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=True, action="ok", reason="cap", cost_data_degraded=False
            ),
        ),
    ):
        coordinator.delegate(
            target_agent_name="specialist",
            work_item="do work",
            workflow_id="wf-deleg",
        )

    # (1) the child's call() received the workflow_id.
    assert child_call_kwargs.get("workflow_id") == "wf-deleg", (
        "delegate() MUST thread workflow_id into the child's call() so the child's "
        f"own terminal record carries it. child kwargs: {child_call_kwargs}"
    )

    # (2) the coordinator's mirror record is emitted, carries the mirror marker,
    # and is NOT stamped with workflow_id.
    mirror = [r for r in mirror_records if r.get("trigger") == PRIMITIVE_DELEGATE]
    assert mirror, f"no delegate mirror record emitted; got {mirror_records}"
    for m in mirror:
        assert "delegated_agent" in m or "delegate_run_id" in m, (
            "mirror record MUST carry the mirror marker (delegated_agent / "
            f"delegate_run_id). Record: {m}"
        )
        assert "workflow_id" not in m, (
            "coordinator's delegate MIRROR record MUST NOT carry workflow_id — that "
            "absence is the count-once enforcement mechanism (it keeps the mirror out "
            f"of the LogQuery-filtered rollup). Record: {m}"
        )

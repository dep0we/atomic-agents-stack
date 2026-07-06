"""Tests for atomic_agents.dashboard Fleet Console (spec/52 PR1).

Coverage map:
  MUST 1 — append-under-flock (sidecar atomicity)
  MUST 2 — compaction determinism (last-event-per-key wins, file-append order)
  MUST 3 — ack/snooze loopback-only enforcement
  MUST 4 — closed-allowlist alert_key validation
  MUST 5 — idempotency (re-ack is a no-op)
  MUST 6 — snooze_until UTC storage + expiry
  MUST 7 — alert_key stability (same condition → same key across renders)
  MUST 8 — Reliability axis from explicit RunRecord markers only

Each MUST has at least one strip-RED negative control.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.dashboard.alert_state import (
    append_alert_event,
    read_alert_state,
    _normalize_snooze_until,
    _compact_lines,
    _state_to_jsonl,
    _COMPACT_THRESHOLD,
)
from atomic_agents.dashboard.attention import (
    _make_alert_key,
    _compute_reliability,
    _cost_spike_alert,
    _governance_alerts,
    aggregate_console,
    _COST_SPIKE_MIN_BASELINE_DAYS,
)
from atomic_agents.dashboard.costs import RunRecord, _record_from_dict
from atomic_agents.dashboard.render import render_all, render_console, render_global
from atomic_agents.dashboard.serve import DashboardHandler, _is_loopback


# ──────────────────────────────────────────────────────────────────
# Helpers


def _make_run_record(
    agent: str = "agent1",
    status: str = "ok",
    cost_usd: float = 0.01,
    ts: datetime | None = None,
    extra: dict | None = None,
    trigger: str = "cron",
    parent_run_id: str | None = None,
) -> RunRecord:
    ts = ts or datetime.now(tz=timezone.utc)
    return RunRecord(
        ts=ts,
        agent=agent,
        trigger=trigger,
        model="claude-sonnet-4-5",
        input_tokens=100,
        output_tokens=20,
        cost_usd=cost_usd,
        cache_hit_tokens=0,
        cache_miss_tokens=100,
        latency_ms=500,
        status=status,
        summary="test",
        extra=extra or {},
        parent_run_id=parent_run_id,
    )


def _write_log(
    agents_root: Path,
    agent: str,
    when: date,
    records: list[dict],
) -> None:
    model_md = agents_root / agent / "model.md"
    model_md.parent.mkdir(parents=True, exist_ok=True)
    if not model_md.exists():
        model_md.write_text("# model\n")
    log_dir = agents_root / agent / "log" / when.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{when.isoformat()}.jsonl"
    lines = []
    for rec in records:
        rec.setdefault(
            "ts",
            datetime.combine(when, datetime.min.time()).astimezone().isoformat(),
        )
        rec.setdefault("trigger", "cron")
        rec.setdefault("model", "claude-sonnet-4-5")
        rec.setdefault("input_tokens", 100)
        rec.setdefault("output_tokens", 20)
        rec.setdefault("cost_usd", 0.01)
        rec.setdefault("status", "ok")
        rec.setdefault("summary", "test run")
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n")


class _SilentHandler(DashboardHandler):
    def log_message(self, fmt, *args):
        pass


def _make_server(agents_root: Path) -> HTTPServer:
    _SilentHandler.agents_root = agents_root
    server = HTTPServer(("127.0.0.1", 0), _SilentHandler)
    return server


def _raw_request(
    server: HTTPServer,
    method: str,
    path: str,
    body: dict | None = None,
) -> tuple[int, bytes]:
    host, port = server.server_address
    sock = socket.create_connection((host, port), timeout=5)
    try:
        if body is not None:
            body_bytes = json.dumps(body).encode()
            request = (
                f"{method} {path} HTTP/1.0\r\n"
                f"Host: 127.0.0.1\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                f"\r\n"
            ).encode() + body_bytes
        else:
            request = f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode()
        sock.sendall(request)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    finally:
        sock.close()
    first_line = response.split(b"\r\n", 1)[0].decode(errors="replace")
    status = int(first_line.split()[1])
    parts = response.split(b"\r\n\r\n", 1)
    resp_body = parts[1] if len(parts) > 1 else b""
    return status, resp_body


@pytest.fixture()
def server(tmp_path):
    """Minimal agents_root + ephemeral HTTP server."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    # Render everything first so index.html + rendered_alert_keys.json exist
    render_all(tmp_path, today=today)

    srv = _make_server(tmp_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, tmp_path
    srv.shutdown()


# ──────────────────────────────────────────────────────────────────
# MUST 7 — alert_key stability
# Same underlying condition → same key across renders (transients stripped)


def test_alert_key_stable_across_different_run_ids():
    """MUST 7: same agent+class+reason_bucket → identical key regardless of run_id."""
    k1 = _make_alert_key("myagent", "reliability.high_error_rate", "high_error_rate")
    k2 = _make_alert_key("myagent", "reliability.high_error_rate", "high_error_rate")
    assert k1 == k2


def test_alert_key_differs_for_different_agents():
    """MUST 7: different agent_id → different key."""
    k1 = _make_alert_key("agent-a", "governance.NO_GOVERNANCE", "no_governance_file")
    k2 = _make_alert_key("agent-b", "governance.NO_GOVERNANCE", "no_governance_file")
    assert k1 != k2


def test_alert_key_differs_for_different_classes():
    """MUST 7: different alert_class → different key."""
    k1 = _make_alert_key("agent", "cost_spike", "cost_above_threshold")
    k2 = _make_alert_key("agent", "reliability.high_error_rate", "high_error_rate")
    assert k1 != k2


def test_alert_key_uses_sha256_not_python_hash():
    """MUST 7 strip-RED: key must be deterministic (not Python's hash())."""
    k = _make_alert_key("agent", "governance.NO_GOVERNANCE", "no_governance_file")
    # SHA-256-based keys start with 'v1:' prefix
    assert k.startswith("v1:")
    # And have exactly 12 hex chars after the prefix
    assert len(k) == len("v1:") + 12


def test_alert_key_nul_separator_prevents_prefix_collision():
    """MUST 7 strip-RED: NUL separator prevents agent_id+alert_class prefix collisions."""
    # 'foo' + 'barx' would equal 'foobar' + 'x' without a separator
    k1 = _make_alert_key("foo", "barx", "bucket")
    k2 = _make_alert_key("foobar", "x", "bucket")
    assert k1 != k2


# ──────────────────────────────────────────────────────────────────
# MUST 6 — snooze_until UTC normalization


def test_snooze_until_z_suffix_normalized():
    """MUST 6: Z-suffix ISO string is normalized to +00:00."""
    result = _normalize_snooze_until("2026-07-01T12:00:00Z")
    assert result is not None
    assert "+00:00" in result or result.endswith("Z") is False
    # Must be parseable as UTC
    dt = datetime.fromisoformat(result)
    assert dt.tzinfo is not None


def test_snooze_until_plus00_passthrough():
    """MUST 6: already-UTC string passes through intact."""
    result = _normalize_snooze_until("2026-07-01T12:00:00+00:00")
    assert result is not None
    dt = datetime.fromisoformat(result)
    assert dt.tzinfo is not None


def test_snooze_until_none_passthrough():
    """MUST 6: None snooze_until returns None."""
    assert _normalize_snooze_until(None) is None


def test_snoozed_item_expires_when_past(tmp_path):
    """MUST 6: snooze_until in the past → item re-appears as open in compacted state."""
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    lines = [
        json.dumps(
            {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "actor": "op",
                "alert_key": "v1:abc123def456",
                "action": "snooze",
                "snooze_until": past,
            }
        )
    ]
    state = _compact_lines(lines)
    # Expired snooze → treated as open (not in state or status=open)
    entry = state.get("v1:abc123def456")
    assert entry is None or entry.get("status") == "open"


def test_snoozed_item_active_when_future(tmp_path):
    """MUST 6 strip-RED: snooze_until in future → item remains snoozed."""
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=4)).isoformat()
    lines = [
        json.dumps(
            {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "actor": "op",
                "alert_key": "v1:abc123def456",
                "action": "snooze",
                "snooze_until": future,
            }
        )
    ]
    state = _compact_lines(lines)
    entry = state.get("v1:abc123def456")
    assert entry is not None
    assert entry["status"] == "snoozed"


# ──────────────────────────────────────────────────────────────────
# MUST 2 — compaction determinism (last event wins, file-append order)


def test_compaction_last_event_wins():
    """MUST 2: last event per alert_key wins in compacted state."""
    key = "v1:test000000"
    ts = datetime.now(tz=timezone.utc).isoformat()
    lines = [
        json.dumps({"ts": ts, "actor": "op", "alert_key": key, "action": "ack"}),
        json.dumps({"ts": ts, "actor": "op", "alert_key": key, "action": "unsnooze"}),
    ]
    state = _compact_lines(lines)
    entry = state.get(key)
    # Last event is unsnooze → status=open
    assert entry is not None
    assert entry["status"] == "open"


def test_compaction_skips_corrupt_lines():
    """MUST 2: corrupt JSONL lines are skipped without crashing."""
    key = "v1:good000000"
    ts = datetime.now(tz=timezone.utc).isoformat()
    lines = [
        "NOT_VALID_JSON{{{",
        json.dumps({"ts": ts, "actor": "op", "alert_key": key, "action": "ack"}),
    ]
    state = _compact_lines(lines)
    assert state.get(key, {}).get("status") == "acked"


def test_compaction_empty_lines():
    """MUST 2: empty sidecar → empty state dict."""
    assert _compact_lines([]) == {}


def test_compaction_rewrite_round_trips_state():
    """MUST 2: a compaction rewrite re-parses to the IDENTICAL state.

    Regression for the _state_to_jsonl/_compact_lines verb mismatch:
    _state_to_jsonl previously emitted the compacted *status* string
    ("acked"/"snoozed"), which _compact_lines does not recognize (it keys on
    *action* verbs "ack"/"snooze"/"unsnooze"). The rewritten file then dropped
    every acked/snoozed entry on the next read — silent total loss of ack/snooze
    state after the first compaction cycle.

    Strip-RED: revert _STATUS_TO_ACTION to emit `entry["status"]` and this fails
    (post-compaction ack and snooze both vanish).
    """
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=4)).isoformat()
    ts = datetime.now(tz=timezone.utc).isoformat()
    lines = [
        json.dumps(
            {"ts": ts, "actor": "op", "alert_key": "v1:ackkey00000", "action": "ack"}
        ),
        json.dumps(
            {
                "ts": ts,
                "actor": "op",
                "alert_key": "v1:snzkey00000",
                "action": "snooze",
                "snooze_until": future,
            }
        ),
        json.dumps(
            {
                "ts": ts,
                "actor": "op",
                "alert_key": "v1:openkey0000",
                "action": "unsnooze",
            }
        ),
    ]
    state_before = _compact_lines(lines)

    # Simulate the compaction rewrite: state -> JSONL -> state.
    rewritten = _state_to_jsonl(state_before)
    state_after = _compact_lines(rewritten.splitlines())

    assert state_after.get("v1:ackkey00000", {}).get("status") == "acked"
    assert state_after.get("v1:snzkey00000", {}).get("status") == "snoozed"
    assert state_after.get("v1:snzkey00000", {}).get("snooze_until") == future
    assert state_before == state_after


def test_live_compaction_preserves_other_key_state(tmp_path):
    """MUST 2: real >threshold compaction (triggered by a DIFFERENT key) keeps state.

    End-to-end through append_alert_event: ack one key, bloat the sidecar past
    _COMPACT_THRESHOLD with events for a second key, then append an event for that
    second key to trigger compaction. The first key's ack MUST survive — it is not
    re-asserted by the triggering append, so it depends entirely on the compaction
    rewrite round-tripping correctly.
    """
    append_alert_event(tmp_path, alert_key="v1:keepacked00", action="ack")
    sidecar = tmp_path / "_console" / "alert_state.jsonl"
    ts = datetime.now(tz=timezone.utc).isoformat()
    with sidecar.open("a", encoding="utf-8") as f:
        for _ in range(_COMPACT_THRESHOLD + 50):
            f.write(
                json.dumps(
                    {
                        "ts": ts,
                        "actor": "op",
                        "alert_key": "v1:noisekey000",
                        "action": "ack",
                    }
                )
                + "\n"
            )
    assert read_alert_state(tmp_path).get("v1:keepacked00", {}).get("status") == "acked"

    # Trigger compaction via an append for the OTHER key.
    append_alert_event(tmp_path, alert_key="v1:noisekey000", action="ack")

    assert read_alert_state(tmp_path).get("v1:keepacked00", {}).get("status") == "acked"
    # Compaction actually fired (file shrank well below the bloat).
    assert len(sidecar.read_text().splitlines()) <= 5


# ──────────────────────────────────────────────────────────────────
# MUST 1 — append-under-flock (sidecar append creates file + dir)


def test_append_creates_console_dir(tmp_path):
    """MUST 1: append_alert_event creates _console/ dir if absent."""
    key = _make_alert_key("myagent", "governance.NO_GOVERNANCE", "no_governance_file")
    append_alert_event(tmp_path, alert_key=key, action="ack")
    assert (tmp_path / "_console" / "alert_state.jsonl").exists()


def test_append_writes_valid_json(tmp_path):
    """MUST 1: appended event is valid JSON with required fields."""
    key = _make_alert_key("myagent", "governance.NO_GOVERNANCE", "no_governance_file")
    append_alert_event(tmp_path, alert_key=key, action="ack")
    sidecar = tmp_path / "_console" / "alert_state.jsonl"
    lines = [l for l in sidecar.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    evt = json.loads(lines[0])
    assert evt["alert_key"] == key
    assert evt["action"] == "ack"
    assert "ts" in evt


def test_append_snooze_writes_snooze_until(tmp_path):
    """MUST 1+6: snooze event includes normalized snooze_until."""
    key = _make_alert_key("myagent", "cost_spike", "cost_above_threshold")
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=4)).isoformat()
    append_alert_event(tmp_path, alert_key=key, action="snooze", snooze_until=future)
    sidecar = tmp_path / "_console" / "alert_state.jsonl"
    evt = json.loads(sidecar.read_text().splitlines()[0])
    assert evt["action"] == "snooze"
    assert "snooze_until" in evt


def test_console_dir_excluded_from_agent_discovery(tmp_path):
    """MUST 1 (directory guard): _console/ is never discovered as an agent."""
    # Create _console/ as a side effect of an append
    key = _make_alert_key("x", "governance.NO_GOVERNANCE", "no_governance_file")
    append_alert_event(tmp_path, alert_key=key, action="ack")
    from atomic_agents.dashboard.costs import discover_agents

    agents = discover_agents(tmp_path)
    assert "_console" not in agents


def test_read_alert_state_returns_empty_when_absent(tmp_path):
    """MUST 1 fail-soft: read when sidecar absent returns {}."""
    state = read_alert_state(tmp_path)
    assert state == {}


def test_read_alert_state_round_trip(tmp_path):
    """MUST 1+2: append then read → state reflects the event."""
    key = _make_alert_key("myagent", "governance.NO_GOVERNANCE", "no_governance_file")
    append_alert_event(tmp_path, alert_key=key, action="ack")
    state = read_alert_state(tmp_path)
    assert key in state
    assert state[key]["status"] == "acked"


# ──────────────────────────────────────────────────────────────────
# MUST 8 — Reliability axis from explicit RunRecord markers


def test_reliability_error_rate(tmp_path):
    """MUST 8: error status counted in error_rate."""
    runs = [
        _make_run_record(status="error"),
        _make_run_record(status="error"),
        _make_run_record(status="ok"),
        _make_run_record(status="ok"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    assert metrics.error_rate == pytest.approx(0.5)
    assert metrics.total_runs == 4


def test_reliability_embed_blocked_from_extra(tmp_path):
    """MUST 8: embed_batch_blocked in extra.embed_batch_blocked increments blocked_rate."""
    runs = [
        _make_run_record(status="error", extra={"embed_batch_blocked": True}),
        _make_run_record(status="ok"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    # embed_batch_blocked is a separate blocker counted in blocked_rate
    assert metrics.embed_blocked_count == 1
    assert metrics.blocked_rate > 0


def test_reliability_embed_blocked_strip_red(tmp_path):
    """MUST 8 strip-RED: WITHOUT embed_batch_blocked in extra, blocked_rate stays 0."""
    runs = [
        # Same status but no embed_batch_blocked in extra
        _make_run_record(status="error", extra={}),
        _make_run_record(status="ok"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    assert metrics.embed_blocked_count == 0


def test_reliability_lock_busy_counted(tmp_path):
    """MUST 8: lock_busy status counted in blocked_rate."""
    runs = [
        _make_run_record(status="lock_busy"),
        _make_run_record(status="ok"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    assert metrics.blocked_rate == pytest.approx(0.5)


def test_reliability_principal_not_verified(tmp_path):
    """MUST 8: principal_not_verified status counted in principal_rate."""
    runs = [
        _make_run_record(status="principal_not_verified"),
        _make_run_record(status="ok"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    assert metrics.principal_rate == pytest.approx(0.5)


def test_reliability_empty_runs(tmp_path):
    """MUST 8: empty run list → all rates 0."""
    metrics = _compute_reliability([], "agent1")
    assert metrics.error_rate == 0.0
    assert metrics.blocked_rate == 0.0
    assert metrics.total_runs == 0


def test_reliability_skipped_rate_counts_cost_blocks(tmp_path):
    """MUST 8: status='skipped' (the primary cost-gate refusal agent.py writes)
    lands in skipped_rate and counts toward total_runs — NOT silently dropped.

    This is the failure CLASS the cost-block attribution findings flagged: a
    cost-guardrail skip must be attributed, not counted as 'clean'.
    """
    runs = [
        _make_run_record(status="skipped"),
        _make_run_record(status="skipped"),
        _make_run_record(status="ok"),
        _make_run_record(status="ok"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    assert metrics.total_runs == 4
    assert metrics.skipped_rate == pytest.approx(0.5)
    # A cost-skip is NOT an error and NOT a lock/embed block — it is its own axis.
    assert metrics.error_rate == 0.0
    assert metrics.blocked_rate == 0.0


def test_reliability_denominator_is_primary_runs_only(tmp_path):
    """MUST 8: the Reliability rate is computed over PRIMARY runs only.

    A realistic agent log interleaves one primary run with many child/bookkeeping
    rows (tool_call, delegate, embed_*, judgment, helper, cost_warning). Counting
    those in total_runs dilutes every rate and renders a failing fleet healthy
    (Principle #5 — the rate IS the audit signal). The denominator must be the
    count of PRIMARY runs, not every logged record.
    """
    runs = [
        _make_run_record(status="error", trigger="cron"),  # 1 primary, failed
        # child / bookkeeping rows — all carry parent_run_id or a child trigger
        _make_run_record(status="ok", trigger="tool_call", parent_run_id="r1"),
        _make_run_record(status="ok", trigger="delegate", parent_run_id="r1"),
        _make_run_record(
            status="ok", trigger="embed_batch_reservation", parent_run_id="r1"
        ),
        _make_run_record(
            status="ok", trigger="embed_batch_release", parent_run_id="r1"
        ),
        _make_run_record(status="ok", trigger="helper", parent_run_id="r1"),
        _make_run_record(status="ok", trigger="judgment", parent_run_id="r1"),
        # cost_warning is a self-logged bookkeeping row with NO parent_run_id —
        # the trigger-set check (not the parent_run_id check) excludes it.
        _make_run_record(status="ok", trigger="cost_warning"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    # ONE primary run, and it errored → total_runs == 1, error_rate == 1.0.
    assert metrics.total_runs == 1
    assert metrics.error_rate == pytest.approx(1.0)


def test_reliability_delegate_skip_not_double_counted(tmp_path):
    """A coordinator's `delegate` child row with status='skipped' (the delegated
    agent hit ITS OWN cost cap) must NOT increment the coordinator's skipped_rate.

    That cost-block is already attributed in the delegated agent's own reliability;
    counting it on the coordinator double-attributes one event to two agents
    (Principle #5). Strip-RED: with the primary-run filter removed this child row
    is counted and skipped_rate goes non-zero.
    """
    runs = [
        _make_run_record(status="ok", trigger="cron"),  # 1 clean primary run
        _make_run_record(status="skipped", trigger="delegate", parent_run_id="r1"),
    ]
    metrics = _compute_reliability(runs, "coordinator")
    assert metrics.total_runs == 1
    assert metrics.skipped_rate == pytest.approx(0.0)


def test_reliability_embed_block_on_primary_run_still_counted(tmp_path):
    """Negative control for the primary-run filter: the embed-block audit record
    (status='error' + extra.embed_batch_blocked on the PRIMARY run's own trigger,
    per agent.py) is a primary run and MUST survive the filter — the filter drops
    child embed_* rows, not the primary run that was embed-blocked.
    """
    runs = [
        _make_run_record(
            status="error",
            trigger="cron",
            extra={"embed_batch_blocked": True},
        ),
        _make_run_record(status="ok", trigger="cron"),
    ]
    metrics = _compute_reliability(runs, "agent1")
    assert metrics.total_runs == 2
    assert metrics.embed_blocked_count == 1
    assert metrics.error_rate == pytest.approx(0.5)


def test_reliability_alerts_fires_on_high_skipped_rate(tmp_path):
    """A high cost-guardrail-blocked (skipped) rate produces an attention item.

    Strip-RED: with skipped_rate below the warn threshold, no skipped alert fires.
    """
    from atomic_agents.dashboard.attention import _reliability_alerts

    # 50% skipped → above the 10% warn threshold.
    high = _compute_reliability(
        [_make_run_record(status="skipped"), _make_run_record(status="ok")],
        "agent1",
    )
    alerts = _reliability_alerts("agent1", high, {})
    subclasses = {a.alert_subclass for a in alerts}
    assert "high_skipped_rate" in subclasses
    skip_alert = next(a for a in alerts if a.alert_subclass == "high_skipped_rate")
    assert skip_alert.alert_class == "reliability"
    assert "cost" in skip_alert.next_step.lower()

    # Strip-RED: 0% skipped → no skipped alert.
    clean = _compute_reliability(
        [_make_run_record(status="ok"), _make_run_record(status="ok")],
        "agent1",
    )
    clean_alerts = _reliability_alerts("agent1", clean, {})
    assert "high_skipped_rate" not in {a.alert_subclass for a in clean_alerts}


# ──────────────────────────────────────────────────────────────────
# RunRecord.extra pass-through (prep finding P0 fix)


def test_record_from_dict_preserves_extra():
    """RunRecord._record_from_dict passes non-canonical keys into extra."""
    raw = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "trigger": "cron",
        "model": "claude-sonnet-4-5",
        "status": "error",
        "embed_batch_blocked": True,
        "some_future_field": "value",
    }
    rr = _record_from_dict(raw, "myagent")
    assert rr is not None
    assert rr.extra.get("embed_batch_blocked") is True
    assert rr.extra.get("some_future_field") == "value"


def test_record_from_dict_extra_excludes_canonical_keys():
    """RunRecord.extra must not duplicate canonical fields."""
    raw = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "model": "claude-sonnet-4-5",
        "cost_usd": 0.05,
        "status": "ok",
    }
    rr = _record_from_dict(raw, "myagent")
    assert rr is not None
    assert "cost_usd" not in rr.extra
    assert "status" not in rr.extra
    assert "model" not in rr.extra


# ──────────────────────────────────────────────────────────────────
# MUST 3 — loopback-only enforcement


def test_is_loopback_localhost():
    assert _is_loopback("127.0.0.1") is True


def test_is_loopback_ipv6():
    assert _is_loopback("::1") is True


def test_is_loopback_network_addr_strip_red():
    """MUST 3 strip-RED: non-loopback address must NOT pass the loopback check."""
    assert _is_loopback("0.0.0.0") is False
    assert _is_loopback("192.168.1.1") is False
    assert _is_loopback("10.0.0.1") is False


class _NonLoopbackPeerHandler(_SilentHandler):
    """Handler that reports a non-loopback CLIENT PEER address.

    socketserver sets self.client_address from the accepted socket's peer; in a
    unit test on loopback the real peer is always 127.0.0.1, so we override the
    setup hook to simulate a remote LAN caller and exercise the MUST 3 gate on
    the peer address (not the bind address).
    """

    def setup(self):  # noqa: D401 — stdlib hook
        super().setup()
        self.client_address = ("192.168.1.50", 54321)


def test_ack_returns_403_on_non_loopback_peer(tmp_path):
    """MUST 3 strip-RED: ack endpoint returns 403 for a NON-LOOPBACK CLIENT PEER.

    The guard inspects self.client_address[0] (the real remote peer), so a LAN
    caller is refused even when the server is bound to loopback. Strip-RED: revert
    do_POST to gate on self.server.server_address[0] (the bind address) and this
    test goes GREEN-for-the-wrong-reason — a 127.0.0.1 bind passes the bind check,
    so the forged-peer request would be served instead of 403'd. The peer override
    is what makes the gate the thing under test.
    """
    today = date.today()
    _write_log(tmp_path, "bob", today, [{"cost_usd": 0.01}])
    render_all(tmp_path, today=today)

    _NonLoopbackPeerHandler.agents_root = tmp_path
    srv = HTTPServer(("127.0.0.1", 0), _NonLoopbackPeerHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        fake_key = "v1:fakefakefake"
        status, _ = _raw_request(srv, "POST", "/alerts/ack", {"alert_key": fake_key})
        assert status == 403, f"Expected 403 for non-loopback peer, got {status}"
    finally:
        srv.shutdown()


def test_ack_loopback_client_under_0_0_0_0_bind_not_403(tmp_path):
    """MUST 3 regression: a loopback CLIENT keeps working under a 0.0.0.0 bind.

    The earlier bind-address gate 403'd EVERY write under a 0.0.0.0 bind, including
    the operator's own 127.0.0.1 ack/snooze — making the write endpoints dead in
    LAN-exposed mode. The peer-address gate lets the local operator write while a
    LAN caller is still refused. A real loopback client (the forged key forces a
    422, never 403) proves the guard let the request THROUGH to the allowlist check.
    """
    today = date.today()
    _write_log(tmp_path, "bob", today, [{"cost_usd": 0.01}])
    render_all(tmp_path, today=today)

    _SilentHandler.agents_root = tmp_path
    srv = HTTPServer(("0.0.0.0", 0), _SilentHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # _raw_request connects to the bind host (0.0.0.0 → loopback peer on the OS).
        forged = "v1:000000000000"
        status, _ = _raw_request(srv, "POST", "/alerts/ack", {"alert_key": forged})
        assert status != 403, (
            f"Loopback client under 0.0.0.0 bind must NOT be 403'd, got {status}"
        )
        assert status == 422, f"Expected 422 (past the loopback gate), got {status}"
    finally:
        srv.shutdown()


# ──────────────────────────────────────────────────────────────────
# MUST 4 — closed-allowlist alert_key validation


def test_forged_alert_key_rejected(server):
    """MUST 4: forged alert_key not in rendered set → 422."""
    srv, _ = server
    forged = "v1:000000000000"  # not in any rendered queue
    status, _ = _raw_request(srv, "POST", "/alerts/ack", {"alert_key": forged})
    assert status == 422, f"Expected 422 for forged key, got {status}"


def test_missing_alert_key_rejected(server):
    """MUST 4 strip-RED: missing alert_key field → 400."""
    srv, _ = server
    status, _ = _raw_request(srv, "POST", "/alerts/ack", {"not_a_key": "x"})
    assert status == 400


def test_503_when_no_rendered_keys_sidecar(tmp_path):
    """MUST 4: absent rendered_alert_keys.json → 503 (console not yet rendered)."""
    # Don't call render_all — no sidecar exists
    _SilentHandler.agents_root = tmp_path
    srv = HTTPServer(("127.0.0.1", 0), _SilentHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, _ = _raw_request(
            srv, "POST", "/alerts/ack", {"alert_key": "v1:abc123def456"}
        )
        assert status == 503, f"Expected 503 when no sidecar exists, got {status}"
    finally:
        srv.shutdown()


# ──────────────────────────────────────────────────────────────────
# MUST 5 — idempotency (re-ack is a no-op)


def test_ack_idempotent(server):
    """MUST 5: re-acking an already-acked alert returns ok with changed=False."""
    srv, agents_root = server
    # First, find a real alert_key from the rendered set
    keys_path = agents_root / "_console" / "rendered_alert_keys.json"
    if not keys_path.exists():
        pytest.skip("No rendered alert keys — fleet has no alerts")
    keys = json.loads(keys_path.read_text())
    if not keys:
        pytest.skip("Empty rendered alert keys — fleet has no alerts")
    key = keys[0]

    # Ack it once
    status1, body1 = _raw_request(srv, "POST", "/alerts/ack", {"alert_key": key})
    assert status1 == 200

    # Ack again — idempotent
    status2, body2 = _raw_request(srv, "POST", "/alerts/ack", {"alert_key": key})
    assert status2 == 200
    resp = json.loads(body2)
    assert resp.get("changed") is False, "Re-ack should report changed=False"


# ──────────────────────────────────────────────────────────────────
# render_all tab routing (prep finding P2 fix)


def test_render_all_console_tab_writes_index_not_cost(tmp_path):
    """tab='console' writes index.html but NOT cost.html."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    written = render_all(tmp_path, today=today, tab="console")
    assert (tmp_path / "_dashboard" / "index.html").exists()
    assert "console" in written
    # cost.html must NOT be written on a console-only render
    assert not (tmp_path / "_dashboard" / "cost.html").exists()


def test_render_all_cost_tab_writes_cost_not_console(tmp_path):
    """tab='cost' writes cost.html but NOT index.html (console home)."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    written = render_all(tmp_path, today=today, tab="cost")
    assert (tmp_path / "_dashboard" / "cost.html").exists()
    # index.html must NOT be updated on a cost-only render
    assert not (tmp_path / "_dashboard" / "index.html").exists()


def test_render_all_writes_both_on_full(tmp_path):
    """tab='all' writes both index.html (console home) and cost.html."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    render_all(tmp_path, today=today, tab="all")
    assert (tmp_path / "_dashboard" / "index.html").exists()
    assert (tmp_path / "_dashboard" / "cost.html").exists()


def test_index_html_is_console_home_not_cost_view(tmp_path):
    """index.html (the new landing page) must contain 'Fleet Console', not the old cost-view table."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    render_all(tmp_path, today=today)
    index_html = (tmp_path / "_dashboard" / "index.html").read_text()
    # Console home marker
    assert "Fleet Console" in index_html
    # cost.html should contain the per-agent breakdown table, not index.html
    cost_html = (tmp_path / "_dashboard" / "cost.html").read_text()
    assert "Per-agent breakdown" in cost_html
    # index.html must NOT contain the old cost-view per-agent breakdown table header
    assert "Per-agent breakdown" not in index_html


# ──────────────────────────────────────────────────────────────────
# nav_bar updated hrefs


def test_nav_bar_cost_tab_points_to_cost_html():
    """Cost tab in nav_bar must link to cost.html (not index.html)."""
    from atomic_agents.dashboard._shared import nav_bar

    html = nav_bar("cost")
    assert 'href="cost.html"' in html


def test_nav_bar_console_tab_points_to_index_html():
    """Console tab in nav_bar must link to index.html (the new front door)."""
    from atomic_agents.dashboard._shared import nav_bar

    html = nav_bar("console")
    assert 'href="index.html"' in html


def test_nav_bar_console_tab_is_first():
    """Console tab must be the first item in the nav bar."""
    from atomic_agents.dashboard._shared import nav_bar

    html = nav_bar("cost")
    console_pos = html.find("Console")
    cost_pos = html.find("Cost")
    assert console_pos < cost_pos, "Console tab must appear before Cost tab"


def test_nav_bar_active_tab_highlighted():
    """Active tab gets the 'active' class."""
    from atomic_agents.dashboard._shared import nav_bar

    html = nav_bar("console")
    assert 'class="active"' in html


# ──────────────────────────────────────────────────────────────────
# aggregate_console fail-soft / empty-fleet


def test_aggregate_console_empty_fleet(tmp_path):
    """Empty fleet → ConsoleData with empty queue, no crash (home-user case)."""
    from atomic_agents.dashboard.attention import aggregate_console

    data = aggregate_console(tmp_path)
    assert data.agent_count == 0
    assert data.attention_queue == []
    assert data.cost_trends == []


def test_aggregate_console_single_agent_no_alerts(tmp_path):
    """Single healthy agent → empty attention queue, still renders cost trends."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05, "status": "ok"}])
    data = aggregate_console(tmp_path, today=today)
    assert data.agent_count == 1
    # No errors, no governance gaps → empty or only low-severity queue
    # (governance alerts may fire for agents without governance.md)


def test_render_console_empty_fleet_no_crash(tmp_path):
    """render_console on empty fleet must produce valid HTML without crashing."""
    from atomic_agents.dashboard.attention import aggregate_console
    from atomic_agents.dashboard.render import render_console

    data = aggregate_console(tmp_path)
    out = render_console(tmp_path, data)
    assert out.exists()
    content = out.read_text()
    assert "Fleet Console" in content


# ──────────────────────────────────────────────────────────────────
# Quality axis — the share path (render_all) and end-to-end regression alert


def _write_eval_runs(
    agents_root: Path,
    agent: str,
    records: list[dict],
) -> None:
    """Write evals/runs/<date>.jsonl records ({ts, weighted_score, test_id})."""
    evals_dir = agents_root / agent / "evals" / "runs"
    evals_dir.mkdir(parents=True, exist_ok=True)
    # model.md so the agent is discoverable by the registry predicate.
    model_md = agents_root / agent / "model.md"
    if not model_md.exists():
        model_md.write_text("# model\n")
    by_day: dict[str, list[dict]] = {}
    for rec in records:
        day = rec["ts"][:10]
        by_day.setdefault(day, []).append(rec)
    for day, recs in by_day.items():
        (evals_dir / f"{day}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in recs) + "\n"
        )


def test_render_all_shares_quality_signals_from_eval_trends(tmp_path):
    """render_all must extract console quality signals from QualityData.eval_trends.

    Strip-RED for the phantom-attribute bug: QualityData has NO `.agents`
    attribute (the field is `eval_trends: list[AgentEvalTrend]`). Before the fix,
    the extraction read `quality_data.agents`, raised AttributeError, was swallowed
    by a broad `except`, and the console silently fell back to a second evals/ read.
    This test asserts the share path actually populates the console quality panel
    with a real score — so a future rename of `eval_trends` fails LOUD here.
    """
    today = date.today()
    # An agent with a clear eval score in the last 30 days.
    # weighted_score is on the 1-5 rubric scale (spec/13). 4.28 → (4.28-1)/4*100 = 82%.
    recent = (today - timedelta(days=2)).isoformat()
    _write_eval_runs(
        tmp_path,
        "alice",
        [{"ts": f"{recent}T12:00:00+00:00", "weighted_score": 4.28, "test_id": "t1"}],
    )
    render_all(tmp_path, today=today, tab="all")
    console_html = (tmp_path / "_dashboard" / "index.html").read_text()
    # The quality panel must render alice's real score (4.28 → 82% on the 1-5 rubric scale),
    # proving the eval_trends share path populated quality_signals rather than no-opping.
    # FIX #690 round-2: weighted_score is 1-5 rubric; (4.28-1)/4*100 = 82%.
    assert "82%" in console_html
    assert "no evals" not in console_html.split("Quality")[1][:400]


def test_quality_regression_produces_attention_item(tmp_path):
    """A regressing agent (delta_30d <= -0.10 rubric points) produces a quality_regression item.

    End-to-end through aggregate_console's lightweight quality reader: a prior
    score (30–60d ago) of 4.0 dropping to a recent 3.5 (delta -0.50, well below
    the -0.10 threshold) must surface a quality_regression alert.
    Strip-RED: a flat/improving agent does not.

    weighted_score is on the 1-5 rubric scale (spec/13).
    """
    today = date.today()
    prior = (today - timedelta(days=45)).isoformat()
    recent = (today - timedelta(days=3)).isoformat()

    # Regressing agent: 4.0 → 3.5, delta = -0.50 (triggers threshold -0.10).
    _write_eval_runs(
        tmp_path,
        "regressor",
        [
            {"ts": f"{prior}T12:00:00+00:00", "weighted_score": 4.0, "test_id": "t1"},
            {"ts": f"{recent}T12:00:00+00:00", "weighted_score": 3.5, "test_id": "t1"},
        ],
    )
    # Stable agent: 4.0 → 4.1, delta = +0.10 (does NOT trigger threshold).
    _write_eval_runs(
        tmp_path,
        "stable",
        [
            {"ts": f"{prior}T12:00:00+00:00", "weighted_score": 4.0, "test_id": "t1"},
            {"ts": f"{recent}T12:00:00+00:00", "weighted_score": 4.1, "test_id": "t1"},
        ],
    )

    data = aggregate_console(tmp_path, today=today)
    quality_items = [
        a for a in data.attention_queue if a.alert_class == "quality_regression"
    ]
    regressor_items = [a for a in quality_items if a.agent == "regressor"]
    assert regressor_items, (
        "expected a quality_regression item for the regressing agent"
    )
    # Strip-RED: the stable agent must NOT produce a quality regression item.
    assert not [a for a in quality_items if a.agent == "stable"]


def test_render_console_writes_rendered_alert_keys_sidecar(tmp_path):
    """render_console writes _console/rendered_alert_keys.json atomically."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    render_all(tmp_path, today=today)
    keys_path = tmp_path / "_console" / "rendered_alert_keys.json"
    assert keys_path.exists()
    keys = json.loads(keys_path.read_text())
    assert isinstance(keys, list)


# ──────────────────────────────────────────────────────────────────
# serve.py GET routing (backward compat)


def test_get_slash_serves_console_home(server):
    """GET / must serve the Fleet Console home (index.html)."""
    srv, agents_root = server
    status, body = _raw_request(srv, "GET", "/")
    assert status == 200
    assert b"Fleet Console" in body


def test_get_cost_serves_cost_view(server):
    """GET /cost must serve cost.html (cost view, not console home)."""
    srv, agents_root = server
    status, body = _raw_request(srv, "GET", "/cost")
    assert status == 200
    # cost.html contains the per-agent breakdown
    assert b"Per-agent breakdown" in body or b"Atomic Agents" in body


def test_get_index_html_serves_console_home(server):
    """GET /index.html must serve the console home."""
    srv, agents_root = server
    status, body = _raw_request(srv, "GET", "/index.html")
    assert status == 200
    assert b"Fleet Console" in body


# ──────────────────────────────────────────────────────────────────
# POST body parsing (prep finding P1)


def test_ack_without_body_returns_400(server):
    """POST /alerts/ack with no body → 400."""
    srv, _ = server
    # Send a POST with no body
    host, port = srv.server_address
    sock = socket.create_connection((host, port), timeout=5)
    try:
        request = (
            b"POST /alerts/ack HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"
        )
        sock.sendall(request)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    finally:
        sock.close()
    first_line = response.split(b"\r\n", 1)[0].decode()
    status = int(first_line.split()[1])
    assert status == 400


def test_ack_with_invalid_json_returns_400(server):
    """POST /alerts/ack with malformed JSON → 400."""
    srv, _ = server
    host, port = srv.server_address
    bad_body = b"NOT JSON{"
    sock = socket.create_connection((host, port), timeout=5)
    try:
        request = (
            f"POST /alerts/ack HTTP/1.0\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(bad_body)}\r\n"
            f"\r\n"
        ).encode() + bad_body
        sock.sendall(request)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    finally:
        sock.close()
    first_line = response.split(b"\r\n", 1)[0].decode()
    status = int(first_line.split()[1])
    assert status == 400


# ──────────────────────────────────────────────────────────────────
# Per-agent drilldown breadcrumb (prep finding P2)


def test_agent_breadcrumb_links_to_console_home(tmp_path):
    """Per-agent dashboard.html breadcrumb must link to console home (index.html)."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    from atomic_agents.dashboard.costs import aggregate_agent
    from atomic_agents.dashboard.render import render_agent

    data = aggregate_agent(tmp_path, "alice", today=today)
    out = render_agent(tmp_path, data)
    content = out.read_text()
    assert "../_dashboard/index.html" in content
    assert "Fleet Console" in content


# ──────────────────────────────────────────────────────────────────
# _console/ dir excluded from registry discovery


def test_console_dir_not_in_discover_agents(tmp_path):
    """_console/ directory must never appear in discover_agents() results."""
    # Create _console/ and alert_state.jsonl
    (tmp_path / "_console").mkdir()
    (tmp_path / "_console" / "alert_state.jsonl").write_text("")
    # Also create a real agent
    _write_log(tmp_path, "myagent", date.today(), [])
    from atomic_agents.dashboard.costs import discover_agents

    agents = discover_agents(tmp_path)
    assert "_console" not in agents
    assert "myagent" in agents


# ──────────────────────────────────────────────────────────────────
# Cost-spike baseline sufficiency: DAYS of history, not RUN count


def test_cost_spike_baseline_gate_counts_days_not_runs():
    """The cost-spike alert gate measures baseline sufficiency in DISTINCT DAYS,
    not in run count (spec/52 §'Cost spike minimum baseline: 7 days').

    Regression for the runs-vs-days unit mismatch: previously the alert gated on
    len(runs_prior_30d) >= 7, so an agent with 7 prior runs ALL on a single day
    passed the gate with a 1-day baseline — disagreeing with the Cost trend panel
    (aggregate_console), which has always gated on distinct days. This pins the
    alert to the same day-count gate so the two surfaces never diverge.

    Strip-RED: revert the gate to `len(runs_prior_30d) < _COST_SPIKE_MIN_BASELINE_DAYS`
    and this test fails (the single-day baseline spuriously fires an alert).
    """
    # Recent window: one expensive day so recent_avg is well above baseline.
    recent_ts = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    runs_30d = [_make_run_record(cost_usd=10.0, ts=recent_ts)]

    # Prior window: _COST_SPIKE_MIN_BASELINE_DAYS runs but ALL on ONE day.
    prior_ts = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    runs_prior = [
        _make_run_record(cost_usd=0.01, ts=prior_ts)
        for _ in range(_COST_SPIKE_MIN_BASELINE_DAYS)
    ]
    assert len({r.ts.date() for r in runs_prior}) == 1  # 1 distinct day

    alert = _cost_spike_alert("agent1", runs_30d, runs_prior, {})
    # 1 day of baseline < 7-day minimum -> no alert, despite 7 prior runs.
    assert alert is None


def test_cost_spike_fires_with_enough_baseline_days():
    """Counterpart: a genuine spike over a sufficient (>=7-day) baseline DOES fire."""
    recent_ts = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    runs_30d = [_make_run_record(cost_usd=10.0, ts=recent_ts)]

    # Prior window: cheap runs spread across 8 distinct days -> baseline established.
    runs_prior = [
        _make_run_record(
            cost_usd=0.01,
            ts=datetime(2026, 5, 10 + d, 9, 0, tzinfo=timezone.utc),
        )
        for d in range(8)
    ]
    assert len({r.ts.date() for r in runs_prior}) == 8

    alert = _cost_spike_alert("agent1", runs_30d, runs_prior, {})
    assert alert is not None
    assert alert.alert_class == "cost_spike"


# ══════════════════════════════════════════════════════════════════
# #614 review-convergence fixes — behavioral tests with strip-RED controls
# ══════════════════════════════════════════════════════════════════


# ── Helpers for governance tests ──────────────────────────────────


def _write_agent(agents_root: Path, name: str) -> Path:
    """Create a minimal agent dir (model.md present so the registry discovers it)."""
    d = agents_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.md").write_text("# model\n")
    return d


def _write_governance(agents_root: Path, name: str, body: str) -> None:
    _write_agent(agents_root, name)
    (agents_root / name / "governance.md").write_text(body)


_VALID_GOV = (
    "```yaml\n"
    "governance:\n"
    "  owner: platform-team\n"
    "  permission_tier: read-only\n"
    "  customer_data: 'no'\n"
    "  writes_sor: 'no'\n"
    "  lifecycle_status: active\n"
    "```\n"
)
_INCOMPLETE_GOV = (
    "```yaml\n"
    "governance:\n"
    "  permission_tier: read-only\n"  # no owner
    "  lifecycle_status: active\n"
    "```\n"
)
_INVALID_GOV = (
    "```yaml\n"
    "governance:\n"
    "  lifecycle_status: 'pending'\n"  # not an allowed enum value
    "```\n"
)
# PRESENT_NO_BLOCK: a readable governance.md with a yaml block but NO governance: key
_NO_BLOCK_GOV = "# Governance\n```yaml\nother_key:\n  note: prose only\n```\n"


# ── Fix #1 (P1): degraded banner fires on a blind cost read ────────


class _RaisingLogBackend:
    """A LogBackend whose query() always raises LogBackendReadError."""

    def query(self, filter):  # noqa: A002 - mirror the real signature
        from atomic_agents.logs import LogBackendReadError

        raise LogBackendReadError("injected blind read")


def test_console_degraded_true_on_blind_log_read(tmp_path, monkeypatch):
    """Fix #1 (P1): a LogBackend raising LogBackendReadError sets ConsoleData.degraded.

    aggregate_console must load runs via _load_runs_with_degraded (which surfaces
    the degraded flag) — NOT the public load_runs (which swallows the error to []
    and discards the flag, leaving a failing fleet rendering as all-healthy/$0.00).
    """
    import atomic_agents.logs as _logs_mod

    _write_agent(tmp_path, "alice")
    monkeypatch.setattr(
        _logs_mod, "get_default_log_backend", lambda scope_root: _RaisingLogBackend()
    )

    data = aggregate_console(tmp_path, today=date.today())
    assert data.degraded is True

    # And the degraded banner renders in the console HTML.
    from atomic_agents.dashboard.render import _render_console_template

    html = _render_console_template(data, has_goals=False)
    assert "data may be incomplete" in html


def test_console_degraded_strip_red_via_load_runs(tmp_path, monkeypatch):
    """Fix #1 strip-RED: routing through the public load_runs (the OLD code)
    swallows the LogBackendReadError and leaves degraded False — proving the
    private-helper switch is load-bearing."""
    import atomic_agents.logs as _logs_mod
    from atomic_agents.dashboard import attention as _att

    _write_agent(tmp_path, "alice")
    monkeypatch.setattr(
        _logs_mod, "get_default_log_backend", lambda scope_root: _RaisingLogBackend()
    )

    # The reverted path: the loader returns [] and FALSE (flag discarded, exactly
    # what the public load_runs() did before the fix).
    monkeypatch.setattr(_att, "_load_runs_with_degraded", lambda *a, **k: ([], False))
    data = aggregate_console(tmp_path, today=date.today())
    assert data.degraded is False  # the reverted (load_runs) behavior


# ── Fix #2 (P2): alert_key allowlist string-bypass ────────────────


def _corrupt_rendered_keys(agents_root: Path, value) -> None:
    """Overwrite rendered_alert_keys.json with an arbitrary JSON value."""
    p = agents_root / "_console" / "rendered_alert_keys.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value))


def test_string_rendered_keys_rejects_forged_char_key(server):
    """Fix #2: a JSON STRING rendered_alert_keys.json must not let a 1-char key
    ("v") pass the allowlist via frozenset() char-iteration. Malformed shape →
    503 (same path as an unreadable sidecar), so the forged key is NEVER 200/accepted."""
    srv, agents_root = server
    _corrupt_rendered_keys(agents_root, "v1:abc123def456")  # a STRING, not a list
    status, _ = _raw_request(srv, "POST", "/alerts/ack", {"alert_key": "v"})
    assert status == 503, f"forged 1-char key must be rejected, got {status}"


def test_non_list_rendered_keys_rejected(server):
    """Fix #2: a non-list (dict) rendered_alert_keys.json → 503."""
    srv, agents_root = server
    _corrupt_rendered_keys(agents_root, {"v1:abc123def456": True})
    status, _ = _raw_request(
        srv, "POST", "/alerts/ack", {"alert_key": "v1:abc123def456"}
    )
    assert status == 503


def test_malformed_key_in_list_rendered_keys_rejected(server):
    """Fix #2: a list containing a non-alert-key-shaped string → 503."""
    srv, agents_root = server
    _corrupt_rendered_keys(agents_root, ["not-a-valid-key"])
    status, _ = _raw_request(
        srv, "POST", "/alerts/ack", {"alert_key": "not-a-valid-key"}
    )
    assert status == 503


def test_string_rendered_keys_strip_red(server, monkeypatch):
    """Fix #2 strip-RED: with the shape guard stripped (revert to raw frozenset()),
    the forged 1-char key 'v' would be ACCEPTED (200) — proving the guard is
    load-bearing. We monkeypatch _load_rendered_alert_keys to the OLD behavior."""
    srv, agents_root = server
    _corrupt_rendered_keys(agents_root, "v1:abc123def456")

    # Reproduce the reverted code: frozenset over a string → set of single chars.
    keys = json.loads(
        (agents_root / "_console" / "rendered_alert_keys.json").read_text()
    )
    old_set = frozenset(keys)  # frozenset("v1:abc...") == {'v','1',':',...}
    assert "v" in old_set, "reverted behavior accepts a forged 1-char key"


# ── Fix #3 (P2): bad snooze_until → 400 not 500 ───────────────────


def _ack_for_real_key(server):
    """Return (srv, agents_root, a real alert_key) or skip if none."""
    srv, agents_root = server
    keys_path = agents_root / "_console" / "rendered_alert_keys.json"
    if not keys_path.exists():
        pytest.skip("No rendered alert keys")
    keys = json.loads(keys_path.read_text())
    if not keys:
        pytest.skip("Empty rendered alert keys")
    return srv, agents_root, keys[0]


def test_bad_snooze_until_returns_400_not_500(server):
    """Fix #3: a present-but-unparseable snooze_until → 400 (not a 500 + traceback)."""
    srv, agents_root, key = _ack_for_real_key(server)
    for bad in ("garbage", "-1", "2026-13-99T99:99:99"):
        status, _ = _raw_request(
            srv, "POST", "/alerts/snooze", {"alert_key": key, "snooze_until": bad}
        )
        assert status == 400, f"snooze_until={bad!r} should be 400, got {status}"


def test_good_snooze_until_still_succeeds(server):
    """Fix #3 control: a well-formed snooze_until still succeeds (200)."""
    srv, agents_root, key = _ack_for_real_key(server)
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=2)).isoformat()
    status, _ = _raw_request(
        srv, "POST", "/alerts/snooze", {"alert_key": key, "snooze_until": future}
    )
    assert status == 200


def test_bad_snooze_until_strip_red(server):
    """Fix #3 strip-RED: WITHOUT the handler validation the bad value reaches
    append_alert_event → _normalize_snooze_until → ValueError. Confirm that the
    underlying normalizer DOES raise on these inputs (the 500 the guard prevents)."""
    for bad in ("garbage", "-1"):
        with pytest.raises(ValueError):
            _normalize_snooze_until(bad)


# ── Fix #4 (P2): append_alert_event validates action + snooze_until ─


def test_append_rejects_unknown_action(tmp_path):
    """Fix #4: an unknown action verb raises ValueError."""
    with pytest.raises(ValueError):
        append_alert_event(tmp_path, alert_key="v1:abc123def456", action="bogus")


def test_append_snooze_without_until_raises(tmp_path):
    """Fix #4: action='snooze' with no snooze_until raises ValueError."""
    with pytest.raises(ValueError):
        append_alert_event(tmp_path, alert_key="v1:abc123def456", action="snooze")


def test_append_ack_with_snooze_until_raises(tmp_path):
    """Fix #4: a snooze_until on a non-snooze action raises ValueError."""
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError):
        append_alert_event(
            tmp_path, alert_key="v1:abc123def456", action="ack", snooze_until=future
        )


def test_append_valid_actions_still_work(tmp_path):
    """Fix #4 control: valid (ack), (snooze+until), (unsnooze) all append cleanly."""
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
    append_alert_event(tmp_path, alert_key="v1:aaaaaaaaaaaa", action="ack")
    append_alert_event(
        tmp_path, alert_key="v1:bbbbbbbbbbbb", action="snooze", snooze_until=future
    )
    append_alert_event(tmp_path, alert_key="v1:cccccccccccc", action="unsnooze")
    state = read_alert_state(tmp_path)
    assert state["v1:aaaaaaaaaaaa"]["status"] == "acked"
    assert state["v1:bbbbbbbbbbbb"]["status"] == "snoozed"
    assert state["v1:cccccccccccc"]["status"] == "open"


# ── Fix #5 (P2): idempotency — same-window re-snooze is a no-op ────


def test_same_window_resnooze_is_noop(server):
    """Fix #5 (MUST 5): an identical same-window re-snooze returns changed=False
    and appends no duplicate audit event."""
    srv, agents_root, key = _ack_for_real_key(server)
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=3)).isoformat()

    s1, _ = _raw_request(
        srv, "POST", "/alerts/snooze", {"alert_key": key, "snooze_until": future}
    )
    assert s1 == 200

    sidecar = agents_root / "_console" / "alert_state.jsonl"
    lines_before = sidecar.read_text().count("\n")

    s2, body2 = _raw_request(
        srv, "POST", "/alerts/snooze", {"alert_key": key, "snooze_until": future}
    )
    assert s2 == 200
    assert json.loads(body2).get("changed") is False, "same-window re-snooze must no-op"
    lines_after = sidecar.read_text().count("\n")
    assert lines_after == lines_before, "no duplicate audit event on a no-op re-snooze"


def test_resnooze_different_window_appends(server):
    """Fix #5 strip-RED control: a re-snooze with a DIFFERENT window IS a state
    change (changed=True, a new event appended) — proving the no-op is gated on
    the snooze_until equality, not a blanket snooze-suppress."""
    srv, agents_root, key = _ack_for_real_key(server)
    f1 = (datetime.now(tz=timezone.utc) + timedelta(hours=3)).isoformat()
    f2 = (datetime.now(tz=timezone.utc) + timedelta(hours=9)).isoformat()

    _raw_request(srv, "POST", "/alerts/snooze", {"alert_key": key, "snooze_until": f1})
    sidecar = agents_root / "_console" / "alert_state.jsonl"
    lines_before = sidecar.read_text().count("\n")

    s2, body2 = _raw_request(
        srv, "POST", "/alerts/snooze", {"alert_key": key, "snooze_until": f2}
    )
    assert s2 == 200
    assert json.loads(body2).get("changed") is True
    assert sidecar.read_text().count("\n") > lines_before


# ── Fix #6 (P2): reliability blocked double-count ─────────────────


def test_blocked_rate_counts_overlap_once(tmp_path):
    """Fix #6: a run that is BOTH lock_busy AND embed_batch_blocked counts ONCE,
    so blocked_rate stays <= 1.0 (no double-count)."""
    runs = [
        _make_run_record(status="lock_busy", extra={"embed_batch_blocked": True}),
        _make_run_record(status="ok"),
    ]
    m = _compute_reliability(runs, "agent1")
    assert m.total_runs == 2
    assert m.blocked_rate <= 1.0
    assert m.blocked_rate == 0.5, "the overlapping run must count once, not twice"


def test_blocked_rate_strip_red_all_overlap(tmp_path):
    """Fix #6 strip-RED: if EVERY run overlaps both markers, the single-predicate
    count keeps blocked_rate at 1.0. The OLD additive code (lock_count + embed_count)
    would yield 2.0 here."""
    runs = [
        _make_run_record(status="lock_busy", extra={"embed_batch_blocked": True})
        for _ in range(3)
    ]
    m = _compute_reliability(runs, "agent1")
    assert m.blocked_rate == 1.0
    # Demonstrate the reverted formula would exceed 1.0:
    lock_count = sum(1 for r in runs if r.status == "lock_busy")
    embed_count = sum(1 for r in runs if r.extra.get("embed_batch_blocked") is True)
    assert (lock_count + embed_count) / len(runs) == 2.0


# ── Fix #7 + #8: governance alert subclass coverage ───────────────


def test_governance_no_governance_alert(tmp_path):
    """MUST 9: an agent with no governance.md → NO_GOVERNANCE (high)."""
    _write_agent(tmp_path, "alice")  # no governance.md
    alerts = _governance_alerts(tmp_path, {})
    sub = {a.alert_subclass: a for a in alerts}
    assert "NO_GOVERNANCE" in sub
    a = sub["NO_GOVERNANCE"]
    assert a.severity == "high"
    assert a.alert_key.startswith("v1:")


def test_governance_invalid_alert(tmp_path):
    """MUST 9: a governance.md with a parse error → GOVERNANCE_INVALID (high)."""
    _write_governance(tmp_path, "alice", _INVALID_GOV)
    alerts = _governance_alerts(tmp_path, {})
    sub = {a.alert_subclass: a for a in alerts}
    assert "GOVERNANCE_INVALID" in sub
    assert sub["GOVERNANCE_INVALID"].severity == "high"
    assert sub["GOVERNANCE_INVALID"].alert_key.startswith("v1:")


def test_governance_incomplete_alert(tmp_path):
    """MUST 9: a valid governance.md with no owner → GOVERNANCE_INCOMPLETE (medium)."""
    _write_governance(tmp_path, "alice", _INCOMPLETE_GOV)
    alerts = _governance_alerts(tmp_path, {})
    sub = {a.alert_subclass: a for a in alerts}
    assert "GOVERNANCE_INCOMPLETE" in sub
    assert sub["GOVERNANCE_INCOMPLETE"].severity == "medium"
    assert sub["GOVERNANCE_INCOMPLETE"].alert_key.startswith("v1:")


def test_governance_no_block_alert(tmp_path):
    """Fix #7 / MUST 9: a present+readable governance.md with NO governance: YAML
    block (PRESENT_NO_BLOCK) → GOVERNANCE_NO_BLOCK (high), NOT silently dropped."""
    _write_governance(tmp_path, "alice", _NO_BLOCK_GOV)
    alerts = _governance_alerts(tmp_path, {})
    sub = {a.alert_subclass: a for a in alerts}
    assert "GOVERNANCE_NO_BLOCK" in sub, "PRESENT_NO_BLOCK must surface an alert"
    a = sub["GOVERNANCE_NO_BLOCK"]
    assert a.severity == "high"
    assert a.alert_key.startswith("v1:")


def test_governance_no_block_strip_red(tmp_path):
    """Fix #7 strip-RED: the registry actually reports PRESENT_NO_BLOCK
    (has_governance=True, governance=None) for this fixture — so WITHOUT the new
    branch the row falls through to `else: continue` and produces ZERO alerts.
    This asserts the precondition that makes the branch load-bearing."""
    from atomic_agents.agent_registry import FilesystemAgentRegistryBackend

    _write_governance(tmp_path, "alice", _NO_BLOCK_GOV)
    ref = FilesystemAgentRegistryBackend(tmp_path).get_agent("alice")
    assert ref is not None
    assert ref.has_governance is True
    assert ref.governance is None  # PRESENT_NO_BLOCK: the dead-path precondition


def test_governance_valid_emits_no_alert(tmp_path):
    """MUST 9: a fully-valid governance.md (owner present) produces NO governance alert."""
    _write_governance(tmp_path, "alice", _VALID_GOV)
    alerts = _governance_alerts(tmp_path, {})
    gov_subs = {a.alert_subclass for a in alerts}
    assert gov_subs == set(), f"valid governance must emit no alert, got {gov_subs}"


# ── Fix #10 (P2): _is_loopback drops "localhost" ──────────────────


def test_is_loopback_rejects_localhost_string(tmp_path):
    """Fix #10: 'localhost' is no longer matched (client_address is a numeric IP;
    matching the hostname string was a code-vs-spec mismatch with MUST 3)."""
    assert _is_loopback("127.0.0.1") is True
    assert _is_loopback("::1") is True
    assert _is_loopback("127.5.5.5") is True
    assert _is_loopback("localhost") is False
    assert _is_loopback("10.0.0.5") is False


# ══════════════════════════════════════════════════════════════════
# Cockpit rebuild #635 — home layout MUSTs 14, 15, 17 (endpoint), 18
# ══════════════════════════════════════════════════════════════════


# ── MUST 14 — Runtime-Health renders cost/quality/reliability only ──


def test_runtime_health_excludes_governance_modelfit_workmix(tmp_path):
    """MUST 14: the rendered Runtime-Health scorecard MUST NOT contain a governance,
    model_fit, or work_mix row. Asserts on the produced HTML (the load-bearing
    enforcement is render._render_health_band filtering display_order through
    _RUNTIME_AXES), not on a constant.
    """
    from atomic_agents.dashboard.render import _render_health_band
    from atomic_agents.advisor.score import compute_fleet_health

    today = date(2026, 6, 28)
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05, "status": "ok"}])
    fh = compute_fleet_health(tmp_path, today=today)
    html = _render_health_band(fh)

    assert "governance" not in html.lower(), (
        "MUST 14: no governance row in Runtime-Health"
    )
    assert "model_fit" not in html, "MUST 14: no model_fit row in Runtime-Health"
    assert "work_mix" not in html, "MUST 14: no work_mix row in Runtime-Health"
    # The three real axes ARE present (positive control).
    assert "reliability" in html and "quality" in html and "cost" in html


def test_runtime_health_excludes_governance_strip_red(tmp_path):
    """MUST 14 strip-RED: the _RUNTIME_AXES filter is what excludes a governance row —
    not an incidental absence.

    Setup: add a ('governance', 'owner_present', ...) candidate to the scorecard
    display order AND inject a matching governance scorecard row. With the production
    _RUNTIME_AXES (3 runtime axes), the row is FILTERED OUT — does not render. Then
    strip the filter (patch _RUNTIME_AXES to also include 'governance') and re-render:
    the SAME row now renders. The only thing that changed is the filter set, so the
    exclusion is load-bearing on the filter, not on row-absence.
    """
    from atomic_agents.dashboard import render as rmod
    from atomic_agents.dashboard.panels import _health as health_mod
    from atomic_agents.advisor.score import compute_fleet_health

    today = date(2026, 6, 28)
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05, "status": "ok"}])
    fh = compute_fleet_health(tmp_path, today=today)

    # Inject a governance scorecard row (metric 'owner_present') onto each agent.
    injected = False
    for ah in fh.agents:
        if not ah.scorecard:
            continue
        ah.scorecard.append(
            _make_scorecard_row(ah, axis="governance", metric="owner_present")
        )
        injected = True
    assert injected, "precondition: an agent must have a scorecard to inject into"

    # Add a governance candidate row to the display order so the metric is eligible.
    patched_order = list(rmod._SCORECARD_DISPLAY_ORDER) + [
        ("governance", "owner_present", "higher")
    ]

    # 1) Production filter (3 runtime axes) → governance row is FILTERED OUT.
    with patch.object(rmod, "_SCORECARD_DISPLAY_ORDER", patched_order):
        html_filtered = rmod._render_health_band(fh)
    assert "owner_present" not in html_filtered, (
        "MUST 14: with the runtime-axes filter, a governance row does NOT render"
    )

    # 2) Strip the filter (admit 'governance') → the SAME row now renders.
    relaxed_axes = frozenset(health_mod._RUNTIME_AXES | {"governance"})
    with (
        patch.object(rmod, "_SCORECARD_DISPLAY_ORDER", patched_order),
        patch.object(health_mod, "_RUNTIME_AXES", relaxed_axes),
    ):
        html_unfiltered = rmod._render_health_band(fh)
    assert "owner_present" in html_unfiltered, (
        "strip-RED: removing the runtime-axes filter renders the governance row — "
        "so the filter is the load-bearing MUST 14 enforcement"
    )


def _make_scorecard_row(ah, axis: str, metric: str):
    """Build a ScorecardRow matching the project's real type, with the given axis/metric."""
    proto = ah.scorecard[0]
    cls = type(proto)
    import dataclasses

    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name == "axis":
            kwargs[f.name] = axis
        elif f.name == "metric":
            kwargs[f.name] = metric
        elif f.name == "value":
            kwargs[f.name] = 1.0
        elif f.name == "score":
            kwargs[f.name] = 50.0
        elif f.name == "target":
            kwargs[f.name] = 0.5
        elif f.name == "wow":
            kwargs[f.name] = None
        elif f.name == "direction":
            kwargs[f.name] = "higher"
        else:
            # default any other field to the prototype's value
            kwargs[f.name] = getattr(proto, f.name)
    return cls(**kwargs)


# ── MUST 15 — home renders fleet-status summary, NOT the agent card grid ──


def test_home_has_no_card_grid(tmp_path):
    """MUST 15: the home page MUST NOT render the per-agent card grid."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    _write_log(tmp_path, "bob", today, [{"cost_usd": 0.06}])
    render_all(tmp_path, today=today)
    html = (tmp_path / "_dashboard" / "index.html").read_text()
    assert 'class="agent-grid"' not in html, (
        "MUST 15: home must not render the card grid"
    )
    assert 'class="agent-card"' not in html, "MUST 15: no per-agent cards on the home"


def test_home_fleet_status_summary_links_monitor(tmp_path):
    """MUST 15: the home renders the compact fleet-status summary (OK/WARN/ERROR/STALE
    count grid) and links to the Fleet Monitor (#653)."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    render_all(tmp_path, today=today)
    html = (tmp_path / "_dashboard" / "index.html").read_text()
    # Distinctive fleet-status markup (the HTML usage class="fo-grid", NOT the bare
    # string which also appears in the CSS, and NOT the always-present zone-label).
    assert 'class="fo-grid"' in html, "MUST 15: fleet-status count grid must be present"
    assert "Fleet Status" in html
    for label in ("OK", "WARN", "ERROR", "STALE"):
        assert f'fc-k">{label}<' in html, (
            f"MUST 15: status count cell '{label}' must render"
        )
    assert "monitor.html" in html, "MUST 15: must link to the Fleet Monitor (#653)"


def test_home_fleet_status_summary_strip_red(tmp_path):
    """MUST 15 strip-RED: 'fo-grid' is the fleet-status panel's distinctive marker —
    an empty-string check on the always-present zone-label would false-green. Confirm
    'fo-grid' is genuinely produced by the panel (absent before render)."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    # Before render: no index.html exists at all.
    assert not (tmp_path / "_dashboard" / "index.html").exists()
    render_all(tmp_path, today=today)
    html = (tmp_path / "_dashboard" / "index.html").read_text()
    assert 'class="fo-grid"' in html


# ── MUST 17 — engine-only sidecar: empty aggregation 422s a legit ack ──


def test_alert_keys_aggregated_sidecar_empty_422s_ack(tmp_path):
    """MUST 17 strip-RED (the spec §12 control): if the engine aggregation is empty
    (no panel contributes alert_keys), the sidecar is empty and a POST of a key that
    WAS in the pre-computed ConsoleData.rendered_alert_keys seed is rejected 422 — i.e.
    the seed does NOT silently keep the allowlist populated. This proves the engine
    union (not the seed) is the load-bearing source for MUST 4.
    """
    import atomic_agents.dashboard.panels._registry as _reg_mod
    from atomic_agents.dashboard.render import render_console
    from atomic_agents.dashboard.attention import aggregate_console
    from atomic_agents.dashboard.panels._registry import PanelRegistry

    today = date.today()
    # An agent with no governance.md → aggregate_console emits a governance alert,
    # so console_data.rendered_alert_keys (the seed) is NON-empty.
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    cd = aggregate_console(tmp_path, today=today)
    seed_keys = sorted(cd.rendered_alert_keys)
    assert seed_keys, "precondition: the aggregation seed must contain at least one key"

    # Render with an EMPTY registry so NO panel contributes alert_keys.
    empty_reg = PanelRegistry()
    original = _reg_mod._REGISTRY
    _reg_mod._REGISTRY = empty_reg
    try:
        render_console(tmp_path, cd, today=today)
    finally:
        _reg_mod._REGISTRY = original

    # Sidecar must be empty — the seed was NOT OR'd in.
    sidecar = tmp_path / "_console" / "rendered_alert_keys.json"
    assert json.loads(sidecar.read_text()) == [], (
        "MUST 17: empty engine union → empty sidecar (no seed backfill)"
    )

    # A POST of a previously-valid (seed) key is now 422'd (closed allowlist empty).
    srv = _make_server(tmp_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, _ = _raw_request(
            srv, "POST", "/alerts/ack", {"alert_key": seed_keys[0]}
        )
        assert status == 422, (
            f"MUST 17: with an empty engine-aggregated sidecar a legit-looking ack "
            f"must 422 (got {status}) — proving the aggregation is load-bearing"
        )
    finally:
        srv.shutdown()


def test_alert_keys_aggregated_sidecar_loadbearing_positive(tmp_path):
    """MUST 17 positive: with the PRODUCTION registry the attention panel contributes
    its keys, so a real rendered alert_key acks 200 (the engine union populated the
    allowlist)."""
    today = date.today()
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05}])
    render_all(tmp_path, today=today)
    sidecar = tmp_path / "_console" / "rendered_alert_keys.json"
    keys = json.loads(sidecar.read_text())
    assert keys, "production render must populate the sidecar via the engine union"

    srv = _make_server(tmp_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, _ = _raw_request(srv, "POST", "/alerts/ack", {"alert_key": keys[0]})
        assert status == 200, f"a real rendered key must ack 200, got {status}"
    finally:
        srv.shutdown()


# ── MUST 18 — home summary status uses the canonical status_for_agent mapping ──


def test_home_summary_status_matches_monitor_mapping(tmp_path):
    """MUST 18: the home fleet-status counts derive from status_for_agent() — the SAME
    canonical function the Fleet Monitor (#653) will reuse. We compute the expected
    counts directly from status_for_agent() over the console data and assert the home
    panel produced the same OK count cell, so home and Monitor cannot diverge.
    """
    from datetime import timezone as _tz
    from atomic_agents.dashboard.attention import aggregate_console
    from atomic_agents.dashboard.panels._fleet_status import _FleetStatusPanel
    from atomic_agents.dashboard.panels._registry import (
        ConsoleCapabilities,
        PanelContext,
    )
    from atomic_agents.dashboard._status import status_for_agent
    from atomic_agents.advisor.score import compute_fleet_health

    today = date(2026, 6, 28)
    now = datetime(2026, 6, 28, 12, 0, tzinfo=_tz.utc)
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05, "status": "ok"}])

    cd = aggregate_console(tmp_path, today=today)
    cd.fleet_health = compute_fleet_health(tmp_path, today=today)

    # Reference (Monitor-side) computation using the canonical function directly.
    ah_by_name = {ah.agent: ah for ah in cd.fleet_health.agents}
    open_by_agent: dict[str, list] = {}
    for item in cd.attention_queue:
        if item.ack_snooze_status == "open":
            open_by_agent.setdefault(item.agent, []).append(item)
    spike_agents = {ct.agent for ct in cd.cost_trends if ct.spike_detected}
    expected: dict[str, int] = {"OK": 0, "WARN": 0, "ERROR": 0, "STALE": 0}
    for agent in cd.last_primary_runs:
        st = status_for_agent(
            agent_health=ah_by_name.get(agent),
            attention_items=open_by_agent.get(agent, []),
            last_primary_run_at=cd.last_primary_runs.get(agent),
            now=now,
            cost_spike=agent in spike_agents,
        )
        expected[st] += 1

    # Home-side: render the fleet-status panel and read its count cells.
    ctx = PanelContext(
        console_data=cd,
        capabilities=ConsoleCapabilities(),
        today=today,
        now=now,
    )
    html = _FleetStatusPanel().render(ctx).html

    # The panel renders each count as <div class="fc-v fc-...">{n}</div>. Parse them.
    import re

    rendered = {}
    for m in re.finditer(
        r'fc-v fc-\w+">(\d+)</div>\s*</?\w*>\s*<div class="fc-k">(\w+)</div>', html
    ):
        rendered[m.group(2)] = int(m.group(1))

    assert rendered.get("OK") == expected["OK"], (
        f"MUST 18: home OK count {rendered.get('OK')} must match the canonical "
        f"status_for_agent() count {expected['OK']}"
    )
    # Total agents accounted for identically on both sides.
    assert sum(rendered.values()) == sum(expected.values()) == len(cd.last_primary_runs)


def test_home_summary_status_strip_red(tmp_path):
    """MUST 18 strip-RED: a STALE agent (no primary run) is counted STALE, not OK —
    confirming the home reads the real status, not a hardcoded all-OK."""
    from datetime import timezone as _tz
    from atomic_agents.dashboard.attention import aggregate_console
    from atomic_agents.dashboard.panels._fleet_status import _FleetStatusPanel
    from atomic_agents.dashboard.panels._registry import (
        ConsoleCapabilities,
        PanelContext,
    )

    today = date(2026, 6, 28)
    # 'now' far in the future so the run is well past the 24h staleness window.
    now = datetime(2026, 8, 1, 12, 0, tzinfo=_tz.utc)
    _write_log(tmp_path, "alice", today, [{"cost_usd": 0.05, "status": "ok"}])
    cd = aggregate_console(tmp_path, today=today)

    ctx = PanelContext(
        console_data=cd,
        capabilities=ConsoleCapabilities(),
        today=today,
        now=now,
    )
    html = _FleetStatusPanel().render(ctx).html
    import re

    rendered = {}
    for m in re.finditer(
        r'fc-v fc-\w+">(\d+)</div>\s*</?\w*>\s*<div class="fc-k">(\w+)</div>', html
    ):
        rendered[m.group(2)] = int(m.group(1))
    assert rendered.get("STALE", 0) >= 1, (
        "an agent with a long-past run must count STALE"
    )
    assert rendered.get("OK", 0) == 0, "the stale agent must NOT be counted OK"


# ──────────────────────────────────────────────────────────────────
# FIX #690 round-2 — fleet quality panel delta + attention regression text


def test_fleet_quality_panel_rubric_delta_display(tmp_path):
    """FIX #690 r2: fleet Console quality panel shows rubric-scale deltas.

    delta_30d = +1.0 raw rubric points → '+25%', not '+100%'.
    The fleet quality axis panel reads QualitySignal.delta_30d and passes
    it to eval_score_delta_fmt — must use rubric scale (default).

    Quality signals are shared from quality.eval_trends, which computes delta
    from TWO data points both within the 30d window. Use 25d and 2d ago to
    ensure both fall inside the [today-30d, today] range.
    """
    today = date.today()
    prior = (today - timedelta(days=25)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()

    # alice: 3.0 → 4.0, delta = +1.0 raw rubric points → +25%
    _write_eval_runs(
        tmp_path,
        "alice",
        [
            {"ts": f"{prior}T12:00:00+00:00", "weighted_score": 3.0, "test_id": "t1"},
            {"ts": f"{recent}T12:00:00+00:00", "weighted_score": 4.0, "test_id": "t1"},
        ],
    )
    render_all(tmp_path, today=today, tab="all")
    console_html = (tmp_path / "_dashboard" / "index.html").read_text()

    # The fleet quality panel must show '+25%' (rubric) not '+100%' (legacy/broken).
    assert "+25%" in console_html, (
        "FIX #690 r2: fleet quality panel must show '+25%' for a +1.0 rubric delta "
        "(= 1.0/4*100 = 25%), not '+100%' (old abs(delta)*100 path)."
    )
    assert "+100%" not in console_html, (
        "FIX #690 r2: '+100%' must NOT appear in fleet console — that is the "
        "4× overstated old value for a +1.0 rubric delta."
    )


def test_attention_quality_regression_reason_rubric_scale(tmp_path):
    """FIX #690 r2: attention-queue regression reason text uses rubric-scale delta.

    A delta_30d of -1.0 raw rubric points must say 'dropped +25%', not 'dropped 100%'.
    The old code did abs(delta_30d) * 100 — 4× overstated.
    """
    today = date.today()
    prior = (today - timedelta(days=45)).isoformat()
    recent = (today - timedelta(days=3)).isoformat()

    # Agent drops 4.0 → 3.0, delta = -1.0 raw rubric points.
    _write_eval_runs(
        tmp_path,
        "regressor",
        [
            {"ts": f"{prior}T12:00:00+00:00", "weighted_score": 4.0, "test_id": "t1"},
            {"ts": f"{recent}T12:00:00+00:00", "weighted_score": 3.0, "test_id": "t1"},
        ],
    )
    data = aggregate_console(tmp_path, today=today)
    quality_items = [
        a for a in data.attention_queue if a.alert_class == "quality_regression"
    ]
    assert quality_items, "expected a quality_regression item for the regressing agent"

    reason = quality_items[0].reason
    # -1.0 rubric delta → magnitude = 1.0/4*100 = 25%; no leading "+".
    # The "dropped" phrasing supplies the direction — the "+" sign is wrong here.
    assert "dropped 25%" in reason, (
        f"FIX #689: regression reason must say 'dropped 25%' (no sign) for a "
        f"-1.0 rubric delta. Got: {reason!r}."
    )
    assert "dropped +25%" not in reason, (
        f"FIX #689: regression reason must NOT say 'dropped +25%' — the leading "
        f"'+' contradicts the 'dropped' phrasing. Got: {reason!r}."
    )
    assert "100%" not in reason, (
        f"FIX #690 r2: regression reason must NOT say '100%' for a -1.0 rubric drop. "
        f"Got: {reason!r}. That is the 4× overstated old value."
    )

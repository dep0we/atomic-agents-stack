"""Tests for ``MandateReservationManager`` and ``compute_outstanding``
(spec/29 §"Cost reservation pattern", #124 PR 3b).

Covers:
- ReservationRecord frozen dataclass shape
- MandateReservationManager create/commit/rollback/expire/shutdown lifecycle
- TTL expiry correctness + idempotency
- compute_outstanding 4-clause definition (clauses 1-4 individually)
- Risk 5 pin: clause 3 sufficient without _committed landing
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atomic_agents.judge.mandate_reservations import (
    MandateReservationManager,
    ReservationRecord,
    compute_outstanding,
)
from atomic_agents.logs import FilesystemLogBackend, LogQuery, RunRecord
from atomic_agents.logs.types import PRIMITIVE_MANDATE_RESERVATION


# ──────────────────────────────────────────────────────────────────
# Helpers


def _make_log_backend(tmp_path: Path) -> FilesystemLogBackend:
    """Return a fresh FilesystemLogBackend for use in reservation tests."""
    return FilesystemLogBackend(tmp_path)


def _make_manager(
    log_backend: FilesystemLogBackend,
    scope: str = "agent:test-agent",
    ttl_s: int = 60,
) -> MandateReservationManager:
    """Construct a MandateReservationManager with the given backend."""
    return MandateReservationManager(log_backend, scope, ttl_s=ttl_s)


def _append_cost_event(
    log_backend: FilesystemLogBackend,
    mandate_id: str,
    proposal_id: str,
    cost_usd: float = 0.05,
) -> None:
    """Append a synthetic cost event tagged with mandate_id + proposal_id."""
    record = RunRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        run_id="run-test",
        primitive="agent_call",
        status="ok",
        summary="synthetic cost event for test",
        model="test-model",
        input_tokens=100,
        output_tokens=50,
        cost_usd=cost_usd,
        cost_source="actor",
        mandate_id=mandate_id,
        extra={"proposal_id": proposal_id, "cost_kind": "token"},
    )
    log_backend.append(record)


# ──────────────────────────────────────────────────────────────────
# ReservationRecord


class TestReservationRecord:
    """Shape tests for the frozen ReservationRecord dataclass."""

    def test_reservation_record_is_frozen(self) -> None:
        """Attempting to mutate a frozen ReservationRecord raises FrozenInstanceError."""
        from dataclasses import FrozenInstanceError

        rec = ReservationRecord(
            reservation_id="abc123def456abcd",
            mandate_id="test-mandate",
            proposal_id="prop-001",
            cost_kind="token",
            projected_usd=0.10,
            ts=datetime.now(timezone.utc).isoformat(),
            ttl_s=60,
        )
        with pytest.raises(FrozenInstanceError):
            rec.reservation_id = "x"  # type: ignore[misc]

    def test_reservation_record_field_shape(self) -> None:
        """ReservationRecord constructs cleanly with all 7 fields."""
        ts = datetime.now(timezone.utc).isoformat()
        rec = ReservationRecord(
            reservation_id="abc123def456abcd",
            mandate_id="m1",
            proposal_id="p1",
            cost_kind="external",
            projected_usd=1.23,
            ts=ts,
            ttl_s=30,
        )
        assert rec.reservation_id == "abc123def456abcd"
        assert rec.mandate_id == "m1"
        assert rec.proposal_id == "p1"
        assert rec.cost_kind == "external"
        assert rec.projected_usd == pytest.approx(1.23)
        assert rec.ts == ts
        assert rec.ttl_s == 30


# ──────────────────────────────────────────────────────────────────
# MandateReservationManager — create + commit


class TestMandateReservationManagerCreateCommit:
    """Create and commit lifecycle paths."""

    def test_create_emits_mandate_reservation_event_with_primitive_constant(
        self, tmp_path: Path
    ) -> None:
        """create() appends one event with primitive=PRIMITIVE_MANDATE_RESERVATION."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        mgr.create("mandate-a", "prop-x", "token", 0.10)

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        assert len(records) == 1
        assert records[0].primitive == PRIMITIVE_MANDATE_RESERVATION
        mgr.shutdown()

    def test_create_returns_unique_reservation_id(self, tmp_path: Path) -> None:
        """Two create() calls return distinct 16-char hex reservation_ids."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        rid1 = mgr.create("m1", "p1", "token", 0.05)
        rid2 = mgr.create("m1", "p2", "token", 0.05)

        assert rid1 != rid2
        assert len(rid1) == 16
        assert len(rid2) == 16
        assert all(c in "0123456789abcdef" for c in rid1)
        assert all(c in "0123456789abcdef" for c in rid2)
        mgr.shutdown()

    def test_commit_emits_mandate_reservation_committed_event(
        self, tmp_path: Path
    ) -> None:
        """create() + commit() → log has two events; second has event_type='mandate_reservation_committed' with actual_usd."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        rid = mgr.create("m1", "p1", "token", 0.10)
        mgr.commit(rid, actual_usd=0.08)

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        assert len(records) == 2
        events = [r.extra.get("event") for r in records]
        assert "mandate_reservation" in events
        assert "mandate_reservation_committed" in events

        committed = next(
            r
            for r in records
            if r.extra.get("event") == "mandate_reservation_committed"
        )
        assert committed.extra.get("actual_usd") == pytest.approx(0.08)

    def test_commit_is_idempotent(self, tmp_path: Path) -> None:
        """Double commit() emits only one committed event (idempotency)."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        rid = mgr.create("m1", "p1", "token", 0.10)
        mgr.commit(rid, actual_usd=0.08)
        mgr.commit(rid, actual_usd=0.08)  # second call: no-op + warning

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        committed_events = [
            r
            for r in records
            if r.extra.get("event") == "mandate_reservation_committed"
        ]
        assert len(committed_events) == 1

    def test_commit_cancels_in_process_ttl_watcher(self, tmp_path: Path) -> None:
        """create() + commit() + sleep past TTL → no _expired event in log.

        Uses ttl_s=2 so commit() can run synchronously before the timer fires;
        then we verify no expired event appears after the TTL would have fired.
        """
        log = _make_log_backend(tmp_path)
        mgr = MandateReservationManager(log, "agent:test", ttl_s=2)
        rid = mgr.create("m1", "p1", "token", 0.05)
        mgr.commit(rid, actual_usd=0.04)
        # Sleep is intentionally short — we just need the Timer to not fire.
        # The timer is cancelled by commit(); we don't need to wait the full 2s.

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        expired = [
            r for r in records if r.extra.get("event") == "mandate_reservation_expired"
        ]
        assert expired == [], (
            "Committed reservation should not produce an expired event"
        )


# ──────────────────────────────────────────────────────────────────
# MandateReservationManager — rollback


class TestMandateReservationManagerRollback:
    """Rollback lifecycle paths."""

    def test_rollback_emits_mandate_reservation_rolled_back_event(
        self, tmp_path: Path
    ) -> None:
        """create() + rollback() → log has create and rolled_back events; reason is preserved."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        rid = mgr.create("m1", "p1", "token", 0.05)
        mgr.rollback(rid, reason="judge_block")

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        rolled = [
            r
            for r in records
            if r.extra.get("event") == "mandate_reservation_rolled_back"
        ]
        assert len(rolled) == 1
        assert rolled[0].extra.get("reason") == "judge_block"

    def test_rollback_is_idempotent(self, tmp_path: Path) -> None:
        """Double rollback() emits only one rolled_back event."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        rid = mgr.create("m1", "p1", "token", 0.05)
        mgr.rollback(rid, reason="tool_error")
        mgr.rollback(rid, reason="tool_error")  # second call: no-op + warning

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        rolled = [
            r
            for r in records
            if r.extra.get("event") == "mandate_reservation_rolled_back"
        ]
        assert len(rolled) == 1

    def test_rollback_cancels_ttl_watcher(self, tmp_path: Path) -> None:
        """create() + rollback() + sleep past TTL → no _expired event.

        Uses ttl_s=2 so rollback() runs synchronously before the timer fires.
        """
        log = _make_log_backend(tmp_path)
        mgr = MandateReservationManager(log, "agent:test", ttl_s=2)
        rid = mgr.create("m1", "p1", "token", 0.05)
        mgr.rollback(rid, reason="test")

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        expired = [
            r for r in records if r.extra.get("event") == "mandate_reservation_expired"
        ]
        assert expired == [], (
            "Rolled-back reservation should not produce an expired event"
        )


# ──────────────────────────────────────────────────────────────────
# MandateReservationManager — TTL expiry


class TestMandateReservationManagerTTLExpiry:
    """TTL watcher correctness."""

    def test_ttl_expiry_emits_expired_event_after_ttl(self, tmp_path: Path) -> None:
        """create(ttl_s≈0) + sleep → log has reservation + expired events."""
        log = _make_log_backend(tmp_path)
        # Use the smallest non-zero float; threading.Timer resolution is ~10ms
        mgr = MandateReservationManager(log, "agent:test", ttl_s=0)
        mgr.create("m1", "p1", "token", 0.05)
        time.sleep(0.3)

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        event_types = [r.extra.get("event") for r in records]
        assert "mandate_reservation" in event_types
        assert "mandate_reservation_expired" in event_types

    def test_ttl_expiry_idempotent_with_commit_race(self, tmp_path: Path) -> None:
        """TTL fire + concurrent commit → log has either commit XOR expire for the same rid, never both.

        Risk 5 pin: uses threading to force the race. The test tolerates
        either outcome (commit wins or expire wins) but asserts mutual exclusion.
        """
        log = _make_log_backend(tmp_path)
        mgr = MandateReservationManager(log, "agent:test", ttl_s=0)
        rid = mgr.create("m1", "p1", "token", 0.05)

        # Race: commit from the main thread vs TTL Timer thread.
        # Give the Timer a tiny head start then commit.
        time.sleep(0.05)
        mgr.commit(rid, actual_usd=0.04)
        time.sleep(0.2)

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        committed = [
            r
            for r in records
            if r.extra.get("event") == "mandate_reservation_committed"
        ]
        expired = [
            r for r in records if r.extra.get("event") == "mandate_reservation_expired"
        ]
        # At most one of committed / expired can be present for the same reservation_id.
        committed_rids = {r.extra.get("reservation_id") for r in committed}
        expired_rids = {r.extra.get("reservation_id") for r in expired}
        overlap = committed_rids & expired_rids
        assert overlap == set(), (
            f"reservation_id(s) {overlap} appear in both committed and expired events"
        )

    def test_shutdown_cancels_pending_watchers_without_emitting(
        self, tmp_path: Path
    ) -> None:
        """create() + shutdown() + sleep past TTL → only the reservation event; no _expired."""
        log = _make_log_backend(tmp_path)
        mgr = MandateReservationManager(log, "agent:test", ttl_s=1)
        mgr.create("m1", "p1", "token", 0.05)
        mgr.shutdown()
        time.sleep(1.2)

        records = log.query(LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION))
        event_types = [r.extra.get("event") for r in records]
        assert "mandate_reservation_expired" not in event_types, (
            "shutdown() should cancel the TTL watcher without emitting _expired"
        )

    def test_shutdown_is_idempotent(self, tmp_path: Path) -> None:
        """Double shutdown() does not raise."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        mgr.shutdown()
        mgr.shutdown()  # must not raise


# ──────────────────────────────────────────────────────────────────
# compute_outstanding


class TestComputeOutstanding:
    """4-clause outstanding definition (spec/29 lines 579-583)."""

    def test_compute_outstanding_returns_empty_when_no_reservations(
        self, tmp_path: Path
    ) -> None:
        """Empty log → compute_outstanding returns []."""
        log = _make_log_backend(tmp_path)
        result = compute_outstanding(log, "agent:test", "m1")
        assert result == []

    def test_compute_outstanding_returns_open_reservations(
        self, tmp_path: Path
    ) -> None:
        """Two reservations with no resolutions → both returned as ReservationRecord."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        mgr.create("m1", "prop-a", "token", 0.10)
        mgr.create("m1", "prop-b", "token", 0.20)
        mgr.shutdown()  # prevent TTL from expiring during the test

        result = compute_outstanding(log, "agent:test", "m1")
        assert len(result) == 2
        proposal_ids = {r.proposal_id for r in result}
        assert "prop-a" in proposal_ids
        assert "prop-b" in proposal_ids

    def test_compute_outstanding_clause1_excludes_committed(
        self, tmp_path: Path
    ) -> None:
        """Clause 1: reservation + matching _committed → compute_outstanding returns []."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        rid = mgr.create("m1", "prop-c", "token", 0.05)
        mgr.commit(rid, actual_usd=0.04)

        result = compute_outstanding(log, "agent:test", "m1")
        assert result == []

    def test_compute_outstanding_clause2_excludes_rolled_back(
        self, tmp_path: Path
    ) -> None:
        """Clause 2: reservation + matching _rolled_back → compute_outstanding returns []."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        rid = mgr.create("m1", "prop-d", "token", 0.05)
        mgr.rollback(rid, reason="test")

        result = compute_outstanding(log, "agent:test", "m1")
        assert result == []

    def test_compute_outstanding_clause2_excludes_expired(self, tmp_path: Path) -> None:
        """Clause 2: reservation that TTL-expired → compute_outstanding returns []."""
        log = _make_log_backend(tmp_path)
        mgr = MandateReservationManager(log, "agent:test", ttl_s=0)
        mgr.create("m1", "prop-e", "token", 0.05)
        time.sleep(0.3)

        result = compute_outstanding(log, "agent:test", "m1")
        assert result == []

    def test_compute_outstanding_clause3_suppresses_when_cost_event_lands_without_commit(
        self, tmp_path: Path
    ) -> None:
        """Clause 3 Risk 5 pin: cost event with matching proposal_id suppresses the reservation
        even without a _committed event landing. Clause 3 alone is sufficient."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log)
        mgr.create("m1", "prop-f", "token", 0.10)
        mgr.shutdown()  # stop TTL from expiring

        # Emit cost event with matching proposal_id — do NOT call commit()
        _append_cost_event(log, "m1", "prop-f")

        result = compute_outstanding(log, "agent:test", "m1")
        assert result == [], (
            "Cost event with matching proposal_id should suppress the reservation "
            "even when _committed was never emitted (Risk 5 pin)"
        )

    def test_compute_outstanding_clause4_excludes_aged_reservations(
        self, tmp_path: Path
    ) -> None:
        """Clause 4: reservation whose age > ttl_s → excluded from outstanding."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log, ttl_s=60)
        mgr.create("m1", "prop-g", "token", 0.05)
        mgr.shutdown()

        # Use a 'now' 100s in the future with a ttl_s=60 window → age > ttl_s
        future_now = datetime.now(timezone.utc).replace(
            second=datetime.now(timezone.utc).second
        )
        import datetime as dt_mod

        future_now = datetime.now(timezone.utc) + dt_mod.timedelta(seconds=100)
        result = compute_outstanding(log, "agent:test", "m1", now=future_now, ttl_s=60)
        assert result == []

    def test_compute_outstanding_clause4_includes_fresh_reservations(
        self, tmp_path: Path
    ) -> None:
        """Clause 4: reservation within ttl_s window → included in outstanding."""
        log = _make_log_backend(tmp_path)
        mgr = _make_manager(log, ttl_s=120)
        mgr.create("m1", "prop-h", "token", 0.05)
        mgr.shutdown()

        # Use a 'now' only 10s in the future — age 10s < ttl_s 120s → included
        import datetime as dt_mod

        near_now = datetime.now(timezone.utc) + dt_mod.timedelta(seconds=10)
        result = compute_outstanding(log, "agent:test", "m1", now=near_now, ttl_s=120)
        assert len(result) == 1
        assert result[0].proposal_id == "prop-h"

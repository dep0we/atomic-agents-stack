"""Mandate cost-reservation pattern (spec/29 §"Cost reservation pattern").

Implements the reservation lifecycle — create → commit → rollback → expire —
that ``MandateCheck`` uses to defend against the stale-budget race (spec/29
line 558): two concurrent actions both passing a cumulative-budget check against
the same pre-action spend, then both executing, overrunning the cap.

**Risk 4 amendment (landed HEAD, commit 15089f2):**
``mandate_used`` is a derived view over cost events, NOT a separately-emitted
JSONL line. This module emits ONLY the reservation lifecycle events. The cost
event written by the caller (agent.py's cost-write path) IS the ``mandate_used``
audit surface. See ``MandateReservationManager.commit()`` docstring for ordering.

**TTL expiry is in-process-only** (spec/29 line 610, Risk H):
``_expire()`` fires only inside the process that created the reservation.
Cross-process orphan recovery goes through ``MandateBackend.recover_orphan_reservations``
(crash-recovery path), not this manager's TTL watcher.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from ..logs.types import PRIMITIVE_MANDATE_RESERVATION, LogQuery, RunRecord

if TYPE_CHECKING:
    from ..logs.backend import LogBackend

logger = logging.getLogger(__name__)

# ── Event type strings (spec/29 lines 570-574) ────────────────────────────────

_EVENT_RESERVATION = "mandate_reservation"
_EVENT_COMMITTED = "mandate_reservation_committed"
_EVENT_ROLLED_BACK = "mandate_reservation_rolled_back"
_EVENT_EXPIRED = "mandate_reservation_expired"

# Cost events (spec/09 shape) that carry proposal_id land under any primitive;
# we query all records for clause-3 cost-event detection.
_COST_EVENT_PRIMITIVES = (
    "agent_call",
    "outcome_iteration",
    "dream",
    "helper",
    "delegate",
    "tool",
)

# Resolution event types for clauses 1 and 2 of the "outstanding" definition.
_RESOLUTION_EVENTS = frozenset(
    {
        _EVENT_COMMITTED,
        _EVENT_ROLLED_BACK,
        _EVENT_EXPIRED,
        "mandate_reservation_committed_on_recovery",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_reservation_id() -> str:
    """16 hex chars (64 bits of entropy) — collision-resistant at v1 scale.

    At 1 000 reservations/sec the collision probability after one day is
    ~3 × 10⁻¹⁰ (birthday paradox over 2⁶⁴ space). uuid4 is cryptographically
    random on all supported platforms (spec/29 pragmatic-defaults convention).
    """
    return uuid.uuid4().hex[:16]


# ── ReservationRecord ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReservationRecord:
    """A live reservation reconstructed from a JSONL log scan.

    Returned by ``compute_outstanding()``. Read-only — mutations to the
    reservation lifecycle go through ``MandateReservationManager`` methods,
    which emit new JSONL events.
    """

    reservation_id: str
    mandate_id: str
    proposal_id: str
    cost_kind: Literal["token", "external"]
    projected_usd: float
    ts: str  # ISO-8601 of the reservation event
    ttl_s: int  # value at create time; not the current remaining


# ── MandateReservationManager ─────────────────────────────────────────────────


class MandateReservationManager:
    """Per-agent reservation lifecycle: create → commit → rollback → expire.

    TTL expiry is IN-PROCESS-ONLY (spec/29 Risk H). Cross-process
    reservations that orphan via crash are reconciled through
    ``MandateBackend.recover_orphan_reservations(...)``, not through this
    manager's TTL watcher.

    Threading: a per-instance ``threading.Lock`` serializes the in-process
    reservation table; events themselves are appended via ``LogBackend``
    which is independently thread-safe.
    """

    def __init__(
        self,
        log_backend: "LogBackend",
        scope: str,
        *,
        ttl_s: int = 60,
        agent_name: str | None = None,
    ) -> None:
        """
        Args:
            log_backend: Backend that receives reservation lifecycle events.
            scope: ``"agent:<name>"`` or ``"project:<name>"`` — used to stamp
                the ``summary`` field and logged for diagnostic purposes.
            ttl_s: In-process TTL in seconds (default 60). After this window
                the TTL watcher calls ``_expire()`` if no commit/rollback landed.
            agent_name: When set, stamped as ``agent_name`` on every ``RunRecord``
                so shared-backend backends can isolate by agent.
        """
        self._log_backend = log_backend
        self._scope = scope
        self._ttl_s = ttl_s
        self._agent_name = agent_name

        # In-process reservation table.
        # Key: reservation_id
        # Value: dict with keys: mandate_id, proposal_id, cost_kind, projected_usd, ts
        # Entries are removed on commit, rollback, or expire.
        self._pending: dict[str, dict] = {}

        # Per-reservation TTL watcher threads.
        # Key: reservation_id, Value: threading.Timer
        self._timers: dict[str, threading.Timer] = {}

        self._lock = threading.Lock()
        self._shutdown = False

    # ── Public API ────────────────────────────────────────────────────────────

    def create(
        self,
        mandate_id: str,
        proposal_id: str,
        cost_kind: Literal["token", "external"],
        projected_usd: float,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> str:
        """Emit a ``mandate_reservation`` event and return the reservation_id.

        The ``event`` field in the JSONL record is ``"mandate_reservation"``
        (spec/29 line 568); ``primitive`` is ``PRIMITIVE_MANDATE_RESERVATION``
        so backends can index-scan the reservation family without a full-log
        walk.

        Registers an in-process TTL watcher (a ``threading.Timer`` daemon that
        calls ``_expire()`` after ``ttl_s`` seconds if no commit/rollback has
        occurred). Per spec/29 line 602, this watcher fires ONLY in the process
        that created the reservation — cross-process orphan handling goes through
        the crash-recovery path.

        Thread-safe.

        Args:
            mandate_id: The mandate this action is citing.
            proposal_id: The proposal that triggered this reservation (16-char
                hex id from ``proposal.py``). Carried on the cost event later
                for clause-3 double-count suppression.
            cost_kind: ``"token"`` for LLM inference cost; ``"external"`` for
                real-money external actions (Stripe, vendor APIs, etc.).
            projected_usd: Best-effort worst-case cost estimate used to reserve
                budget headroom.
            run_id: Agent run id for the ``RunRecord``. Optional — reservation
                events are queryable by ``primitive`` + ``mandate_id`` without it.
            parent_run_id: Parent run id (for dashboard rollup coherence,
                spec/29 line 605).

        Returns:
            The ``reservation_id`` (16 hex chars, 64-bit entropy) to pass to
            ``commit()`` or ``rollback()``.
        """
        reservation_id = _make_reservation_id()
        ts = _now_iso()

        with self._lock:
            if self._shutdown:
                raise RuntimeError(
                    "MandateReservationManager is shut down; no new reservations."
                )
            self._pending[reservation_id] = {
                "mandate_id": mandate_id,
                "proposal_id": proposal_id,
                "cost_kind": cost_kind,
                "projected_usd": projected_usd,
                "ts": ts,
            }
            # Register TTL watcher INSIDE the lock so _expire() cannot fire
            # before the entry is in _pending (race between Timer fire and lock
            # acquisition would still be serialized through self._lock in _expire,
            # but starting inside the lock keeps the table consistent).
            timer = threading.Timer(self._ttl_s, self._expire, args=(reservation_id,))
            timer.daemon = True
            self._timers[reservation_id] = timer
            timer.start()

        record = self._build_record(
            event=_EVENT_RESERVATION,
            status="ok",
            summary=f"mandate_reservation created scope={self._scope} rid={reservation_id}",
            mandate_id=mandate_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            extra={
                "event": _EVENT_RESERVATION,
                "reservation_id": reservation_id,
                "mandate_id": mandate_id,
                "proposal_id": proposal_id,
                "cost_kind": cost_kind,
                "projected_usd": projected_usd,
                "ttl_s": self._ttl_s,
            },
            ts=ts,
        )
        self._log_backend.append(record)
        return reservation_id

    def commit(
        self,
        reservation_id: str,
        actual_usd: float,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        """Emit ``mandate_reservation_committed``. Cancels the TTL watcher.

        **Ordering (spec/29 Risk 4 amendment + Risk 6):**
        The caller (agent.py cost-write path) MUST:
        1. Emit the cost event with ``mandate_id`` + ``proposal_id`` on
           ``RunRecord.extra`` (this is the ``mandate_used`` audit surface).
        2. THEN call ``commit()`` to emit the lifecycle resolution event.
        Post-action verification (Risk 6) fires AFTER this commit step.

        This method emits ONLY the ``_committed`` lifecycle resolution event.
        The actual spend is the cost event the caller already wrote; ``actual_usd``
        here is informational (helps recovery scans reconcile without re-querying
        cost events).

        Idempotent: a second call for the same ``reservation_id`` is a no-op
        + warning log (the first commit already resolved the lifecycle).

        Args:
            reservation_id: The id returned by ``create()``.
            actual_usd: The real cost incurred (may differ from ``projected_usd``).
        """
        entry, already_resolved = self._resolve(reservation_id)
        if already_resolved:
            logger.warning(
                "mandate_reservation commit() called twice for reservation_id=%s — no-op",
                reservation_id,
            )
            return

        if entry is None:
            logger.warning(
                "mandate_reservation commit() called for unknown reservation_id=%s — no-op",
                reservation_id,
            )
            return

        record = self._build_record(
            event=_EVENT_COMMITTED,
            status="ok",
            summary=f"mandate_reservation_committed scope={self._scope} rid={reservation_id}",
            mandate_id=entry["mandate_id"],
            run_id=run_id,
            parent_run_id=parent_run_id,
            extra={
                "event": _EVENT_COMMITTED,
                "reservation_id": reservation_id,
                "mandate_id": entry["mandate_id"],
                "proposal_id": entry["proposal_id"],
                "cost_kind": entry["cost_kind"],
                "actual_usd": actual_usd,
            },
        )
        self._log_backend.append(record)

    def rollback(
        self,
        reservation_id: str,
        reason: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        """Emit ``mandate_reservation_rolled_back``. Cancels the TTL watcher.

        Suggested ``reason`` values: ``"judge_block"``, ``"tool_error"``,
        ``"operator_revoked"``, ``"verification_diverged"``.

        Idempotent: a second call is a no-op + warning log.

        Args:
            reservation_id: The id returned by ``create()``.
            reason: Operator-readable reason for the rollback.
        """
        entry, already_resolved = self._resolve(reservation_id)
        if already_resolved:
            logger.warning(
                "mandate_reservation rollback() called twice for reservation_id=%s — no-op",
                reservation_id,
            )
            return

        if entry is None:
            logger.warning(
                "mandate_reservation rollback() called for unknown reservation_id=%s — no-op",
                reservation_id,
            )
            return

        record = self._build_record(
            event=_EVENT_ROLLED_BACK,
            status="ok",
            summary=(
                f"mandate_reservation_rolled_back scope={self._scope} "
                f"rid={reservation_id} reason={reason}"
            ),
            mandate_id=entry["mandate_id"],
            run_id=run_id,
            parent_run_id=parent_run_id,
            extra={
                "event": _EVENT_ROLLED_BACK,
                "reservation_id": reservation_id,
                "mandate_id": entry["mandate_id"],
                "proposal_id": entry["proposal_id"],
                "cost_kind": entry["cost_kind"],
                "reason": reason,
            },
        )
        self._log_backend.append(record)

    def shutdown(self) -> None:
        """Cancel all pending TTL watchers without emitting events.

        Called on framework shutdown. Pending reservations become orphans
        on next boot — crash-recovery (``MandateBackend.recover_orphan_reservations``)
        picks them up, NOT this manager. Idempotent.
        """
        with self._lock:
            self._shutdown = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            # Leave _pending intact — crash-recovery needs the JSONL trail to
            # detect these as orphans. This process simply stops maintaining
            # them.

    # ── Internal ──────────────────────────────────────────────────────────────

    def _expire(self, reservation_id: str) -> None:
        """Emit ``mandate_reservation_expired`` if the reservation is still pending.

        Called by the daemon Timer after ``ttl_s`` seconds. IN-PROCESS-ONLY
        (spec/29 line 610, Risk H) — only the process that created the
        reservation via ``create()`` ever calls this. Cross-process readers
        MUST NOT call this for reservations they didn't create.

        Idempotent + race-safe: if ``commit()`` or ``rollback()`` landed
        between the Timer firing and this method acquiring the lock, ``_resolve``
        returns ``already_resolved=True`` and we silently no-op (the final log
        has commit XOR expire, never both).
        """
        entry, already_resolved = self._resolve(reservation_id)
        if already_resolved or entry is None:
            # Concurrent commit/rollback won the race — nothing to do.
            return

        record = self._build_record(
            event=_EVENT_EXPIRED,
            status="ok",
            summary=f"mandate_reservation_expired scope={self._scope} rid={reservation_id}",
            mandate_id=entry["mandate_id"],
            run_id=None,
            parent_run_id=None,
            extra={
                "event": _EVENT_EXPIRED,
                "reservation_id": reservation_id,
                "mandate_id": entry["mandate_id"],
                "proposal_id": entry["proposal_id"],
                "cost_kind": entry["cost_kind"],
            },
        )
        self._log_backend.append(record)

    def _resolve(self, reservation_id: str) -> tuple[dict | None, bool]:
        """Remove ``reservation_id`` from in-process state and cancel its timer.

        Returns ``(entry, already_resolved)``:
        - If the reservation was pending: returns ``(entry_dict, False)`` — the
          caller should emit the resolution event.
        - If the reservation was already resolved (not in ``_pending``): returns
          ``(None, True)`` — the caller should no-op + warn.
        - If the reservation was never created here (unknown id): returns
          ``(None, False)`` — the caller should no-op + warn (different message).

        We distinguish the "never existed" case from "already resolved" via a
        sentinel set. Track resolved ids so idempotency detection works without
        keeping the full entry in memory.
        """
        with self._lock:
            if reservation_id in self._pending:
                entry = self._pending.pop(reservation_id)
                # Cancel and remove the timer.
                timer = self._timers.pop(reservation_id, None)
                if timer is not None:
                    timer.cancel()
                # Track that this id has been resolved (for idempotency).
                if not hasattr(self, "_resolved_ids"):
                    self._resolved_ids: set[str] = set()
                self._resolved_ids.add(reservation_id)
                return entry, False
            # Not in pending — either already resolved or never existed.
            resolved = getattr(self, "_resolved_ids", set())
            if reservation_id in resolved:
                return None, True
            return None, False

    def _build_record(
        self,
        *,
        event: str,
        status: str,
        summary: str,
        mandate_id: str,
        run_id: str | None,
        parent_run_id: str | None,
        extra: dict,
        ts: str | None = None,
    ) -> RunRecord:
        """Construct a ``RunRecord`` for a reservation lifecycle event."""
        return RunRecord(
            ts=ts or _now_iso(),
            run_id=run_id or "",
            primitive=PRIMITIVE_MANDATE_RESERVATION,
            status=status,
            summary=summary,
            model="n/a",
            input_tokens=0,
            output_tokens=0,
            mandate_id=mandate_id,
            parent_run_id=parent_run_id,
            agent_name=self._agent_name,
            extra=extra,
        )


# ── compute_outstanding ───────────────────────────────────────────────────────


def compute_outstanding(
    log_backend: "LogBackend",
    scope: str,
    mandate_id: str,
    *,
    now: datetime | None = None,
    ttl_s: int = 60,
    agent_name: str | None = None,
) -> list[ReservationRecord]:
    """Return live reservations for ``mandate_id`` per the 4-clause definition.

    **"Outstanding" iff ALL four clauses hold** (spec/29 lines 579-583):

    1. No ``_committed`` / ``_committed_on_recovery`` event exists for this
       ``proposal_id`` in the log.
    2. No ``_rolled_back`` / ``_expired`` event exists for this ``proposal_id``.
    3. **No cost event tagged with the same ``proposal_id`` exists** — this
       suppresses the reservation during the wall-clock window between the cost
       event landing and ``_committed`` landing (the double-count window,
       spec/29 Risk A). Clause 3 alone is sufficient; it is NOT AND-ed with
       clause 1 (i.e., a cost event suppresses even if ``_committed`` never
       lands — the crash-recovery story handles that orphan separately).
    4. The reservation event's age (``now`` minus event ``ts``) is below
       ``ttl_s``.

    Reads JSONL via ``LogBackend.query()`` — does NOT open log files directly
    (Protocol discipline). All queries are AND-filtered by
    ``primitive=PRIMITIVE_MANDATE_RESERVATION`` or cost-event primitives; the
    ``mandate_id`` filter bounds the scan on SQLite-backed deployments.

    Results are sorted ascending by reservation ``ts`` (insertion order in JSONL).

    Args:
        log_backend: The log backend to query.
        scope: ``"agent:<name>"`` or ``"project:<name>"`` — for documentation
            and future filtering; not currently used as a LogQuery filter because
            scope is baked into the backend's root. Callers should instantiate
            one backend per agent root.
        mandate_id: The mandate whose outstanding reservations to sum.
        now: Reference time for clause-4 TTL check. Defaults to
            ``datetime.now(timezone.utc)``. Inject for deterministic tests.
        ttl_s: TTL window in seconds (default 60, matches ``create()`` default).
        agent_name: When set, AND-filters ``LogQuery.agent_name`` for shared
            backends (spec/22 §"Implementer contract for queryable backends").

    Returns:
        A list of ``ReservationRecord`` instances for reservations that are
        currently outstanding, in ascending-``ts`` order.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # ── Step 1: fetch all mandate_reservation events for this mandate ─────────
    reservation_query = LogQuery(
        primitive=PRIMITIVE_MANDATE_RESERVATION,
        mandate_id=mandate_id,
        agent_name=agent_name,
    )
    all_reservation_records = log_backend.query(reservation_query)

    # Partition into: create events vs resolution events.
    create_events: list[RunRecord] = []
    resolution_proposal_ids: set[str] = set()

    for rec in all_reservation_records:
        event_type = rec.extra.get("event", "")
        if event_type == _EVENT_RESERVATION:
            create_events.append(rec)
        elif event_type in _RESOLUTION_EVENTS:
            # Clauses 1 + 2: any resolution event for a proposal_id removes it.
            proposal_id = rec.extra.get("proposal_id")
            if proposal_id:
                resolution_proposal_ids.add(proposal_id)

    if not create_events:
        return []

    # ── Step 2: collect proposal_ids that have a committed cost event ─────────
    # Clause 3: a cost event carrying proposal_id suppresses the reservation.
    # This defends the ~ms window between cost-event-landed and _committed-landed
    # where compute_outstanding would otherwise double-count both reservation and
    # actual spend (spec/29 Risk A). Clause 3 is sufficient on its own —
    # we do NOT require _committed to also be absent.
    cost_event_proposal_ids: set[str] = set()
    cost_query = LogQuery(
        primitive=tuple(_COST_EVENT_PRIMITIVES),
        mandate_id=mandate_id,
        agent_name=agent_name,
        cost_source="actor",
    )
    cost_records = log_backend.query(cost_query)
    for rec in cost_records:
        # Round 1 Finding 5: only ACTOR-source cost records suppress a
        # reservation (helper/delegate proposal_id annotations are not spend).
        if rec.cost_source != "actor":
            continue
        proposal_id = rec.extra.get("proposal_id")
        if proposal_id:
            cost_event_proposal_ids.add(proposal_id)

    # ── Step 3: apply all four clauses ───────────────────────────────────────
    outstanding: list[ReservationRecord] = []

    for rec in create_events:
        extra = rec.extra
        proposal_id: str | None = extra.get("proposal_id")
        if not proposal_id:
            continue  # malformed reservation event — skip

        # Clause 1 + 2: no resolution event for this proposal_id.
        if proposal_id in resolution_proposal_ids:
            continue

        # Clause 3: no cost event with this proposal_id exists.
        # This clause alone is sufficient for the double-count-window case
        # (cost event landed; _committed may or may not have landed yet).
        if proposal_id in cost_event_proposal_ids:
            continue

        # Clause 4: reservation age < ttl_s.
        ts_str: str = rec.ts
        try:
            ts_dt = datetime.fromisoformat(ts_str)
        except ValueError:
            continue  # malformed ts — skip

        # Make now tz-aware if ts_dt is tz-aware (or vice versa).
        if ts_dt.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        elif ts_dt.tzinfo is None and now.tzinfo is not None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)

        age_s = (now - ts_dt).total_seconds()
        if age_s >= ttl_s:
            continue

        reservation_id: str | None = extra.get("reservation_id")
        cost_kind = extra.get("cost_kind", "token")
        projected_usd = float(extra.get("projected_usd", 0.0))
        ttl_at_create = int(extra.get("ttl_s", ttl_s))

        if not reservation_id:
            continue  # malformed — skip

        outstanding.append(
            ReservationRecord(
                reservation_id=reservation_id,
                mandate_id=mandate_id,
                proposal_id=proposal_id,
                cost_kind=cost_kind,  # type: ignore[arg-type]
                projected_usd=projected_usd,
                ts=ts_str,
                ttl_s=ttl_at_create,
            )
        )

    # Results in ascending-ts order (LogBackend.query guarantees chronological
    # order, so no additional sort is needed unless the backend mixes primitives
    # and reorders — sort defensively to keep the contract stable).
    outstanding.sort(key=lambda r: r.ts)
    return outstanding


def build_mandate_log_record_extras(
    successful_cites: list[tuple[str, str]],
) -> dict[str, object]:
    """Build the mandate tagging fields for an agent_call cost log record.

    Round 1 Finding 1 + Round 2 R2-2 — extracted into a pure helper so the
    test suite can pin the tagging contract without re-implementing the logic
    inline. The agent loop calls this with its
    ``_successful_mandate_cites_this_call`` list at log_record build time.

    Returns a dict to merge into the log_record:
      - Empty dict when no mandate cites committed this call (back-compat).
      - {'mandate_id': X, 'proposal_id': Y} on single-mandate calls.
      - {'mandate_id': X (most-recent), 'proposal_id': Y, 'mandate_cites_in_call': [...]}
        on multi-mandate calls so operators see the v1 under-attribution.

    Spec/29 §"Atomic emission of `mandate_used`" (commit 15089f2 amendment):
    the cost event IS the mandate_used surface. ``_sum_prior_token_cost``
    queries by ``cost_source=actor + mandate_id``; the top-level
    ``mandate_id`` field is load-bearing for that query.
    """
    if not successful_cites:
        return {}
    distinct_mids = {m for m, _ in successful_cites}
    last_mid, last_pid = successful_cites[-1]
    extras: dict[str, object] = {
        "mandate_id": last_mid,
        "proposal_id": last_pid,
    }
    if len(distinct_mids) > 1:
        extras["mandate_cites_in_call"] = sorted(distinct_mids)
    return extras

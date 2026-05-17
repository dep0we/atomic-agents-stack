"""``MandateStateManager`` — lifecycle event dedup + throttle state for
``MandateCheck`` (spec/29 §"Lifecycle event deduplication", lines 620-659;
§"Suspicious-rebind throttle", lines 394-427; §"Concurrent state writes",
lines 739-741).

PR 3a of #124. Scope: this module only. Does NOT implement MandateCheck
(Agent A), target extraction (Agent C), or judges_md.py extensions (Agent D).

Design overview
---------------
Lifecycle dedup prevents the framework from re-emitting ``mandate_granted``,
``mandate_revoked``, and ``mandate_expired`` events on every agent run. On
each load the caller (``MandateCheck``, PR 3a Agent A) passes the current set
of ``Mandate`` instances obtained from ``MandateBackend.list_mandates``.
``compute_transitions`` reads the per-scope state sidecar, compares each
mandate's current ``RevocationState`` against the last-seen state, emits any
new transition events as dicts, updates the state, and persists via
``MandateBackend.write_state``. Events are returned to the caller for
forwarding to ``LogBackend.append``; this module does NOT touch the log
directly (separation of concerns — the caller is the log-write authority).

Suspicious-rebind throttle (spec/29 lines 394-427) persists under the
``"throttles"`` key in the same state shape. In-memory-only persistence is
explicitly forbidden because a crash-restart loop can bypass the throttle,
defeating the prompt-injection defence (spec/29 line 408, plan-subagent
Risk C). Throttle state therefore goes through ``MandateBackend.write_state``
on every arm and survives process restarts.

Concurrency model (spec/29 line 741)
--------------------------------------
Within a single agent process the read-modify-write on state is serialized via
a ``threading.Lock`` per ``MandateStateManager`` instance. Each instance is
scoped to ONE scope string (e.g., ``"agent:caldwell"``), so the lock is
per-scope within the process — matching the spec's "per-scope within single
process" language. Cross-process writes are eventual-consistency: the spec
documents this as a known limitation (plan-subagent Risk D); operators needing
strict cross-process atomicity should use a SQL-backed ``MandateBackend``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..mandate.types import MandateStateSchemaUnsupported, RevocationState

if TYPE_CHECKING:
    from ..mandate.backend import MandateBackend
    from ..mandate.types import Mandate


class MandateStateManager:
    """Lifecycle event dedup + throttle state for ``MandateCheck``.

    Wraps ``MandateBackend.read_state`` / ``write_state`` with the
    transition-detection logic per spec/29 §"Lifecycle event deduplication"
    (lines 620-659) and the suspicious-rebind throttle persistence per
    spec/29 §"Suspicious-rebind throttle" (lines 394-427).

    **Events returned, not emitted.** ``compute_transitions`` returns a list
    of lifecycle-event dicts ready to forward to ``LogBackend.append``. The
    caller (``MandateCheck``) is responsible for the actual append. This
    module performs no log I/O.

    **Threading model.** A ``threading.Lock`` per instance serializes the
    read-modify-write state computation within a single agent process. One
    ``MandateStateManager`` instance per scope per process is the expected
    wiring (spec/29 line 741: "serialized via a threading.Lock on the
    MandateCheck instance's per-scope state computation").

    State shape (schema_version 1, spec/29 lines 631-646 + 410-425):

    .. code-block:: json

        {
          "schema_version": 1,
          "scope": "agent:<name>",
          "mandates": {
            "<mandate_id>": {
              "last_seen_state": "active" | "revoked" | "expired",
              "last_seen_revoked_at": "..." | null,
              "last_seen_expired_at": "..." | null,
              "last_seen_source_hash": "sha256:..."
            }
          },
          "throttles": {
            "<mandate_id>": {
              "agent_run_id": "...",
              "expires_at_iso": "...",
              "original_state_inconsistent_at": "..."
            }
          }
        }
    """

    _DEFAULT_SCHEMA_VERSION = 1  # spec/29 line 648: schema_version 1 mandatory from PR 3a

    def __init__(self, mandate_backend: MandateBackend, scope: str) -> None:
        """Construct a manager for one scope.

        Args:
            mandate_backend: A ``MandateBackend`` Protocol implementor.
                ``read_state(scope)`` and ``write_state(scope, state)`` are
                the only methods called by this class.
            scope: The scope key, e.g. ``"agent:caldwell"`` or
                ``"project:highland"``. Validated at the ``MandateBackend``
                boundary before any I/O; this class passes it through without
                re-validating (single responsibility — backend owns validation,
                spec/29 MUST #1).
        """
        self._mandate_backend = mandate_backend
        self._scope = scope
        # Per-scope threading.Lock (spec/29 line 741) — one lock per
        # MandateStateManager instance. Serializes read-modify-write within
        # a single process. Cross-process writes are eventual-consistency;
        # see module docstring "Concurrency model" above.
        self._lock = threading.Lock()

    # ── Public interface ──────────────────────────────────────────────────────

    def compute_transitions(self, loaded_mandates: list[Mandate]) -> list[dict]:
        """Detect lifecycle transitions, persist updated state, return events.

        Reads the scope's state sidecar, compares each mandate in
        ``loaded_mandates`` against its last-seen state, and emits one event
        dict per observed transition (spec/29 lines 650-654):

        - **New mandate** (ID not in state): ``mandate_granted`` event.
        - **active → revoked**: ``mandate_revoked`` event.
        - **active → expired**: ``mandate_expired`` event (EXPIRED is derived
          state — framework computes from ``expires_at < now``, not from a
          file mutation; spec/29 line 614).
        - **Same-mandate multi-transition**: at most ONE event per call, the
          most-significant transition observed (revoke > expire; a mandate
          cannot be simultaneously revoked and expired in a single diff).
        - **Source-hash drift on still-active mandate**: no event (doctor
          surfaces via ``check_mandate_source_hash_drift``, PR 4).

        Event ordering: ``mandate_granted`` first (new IDs), then
        ``mandate_revoked``, then ``mandate_expired``.

        Updated state is persisted atomically via
        ``MandateBackend.write_state`` (spec/29 MUST #3 — backend handles
        temp + fsync + rename). Throttle GC (expiry cleanup) runs inside
        every write so the state sidecar does not accumulate stale entries.

        The returned list may be empty when no transitions are observed (all
        mandates are already known and in their current state). The caller
        (``MandateCheck``) decides whether to emit the events.

        Args:
            loaded_mandates: The current ``Mandate`` instances for this
                scope, obtained from ``MandateBackend.list_mandates``. May
                be empty when no ``mandates.md`` is present.

        Returns:
            A list of lifecycle event dicts. Each dict has an ``"event"``
            key (``"mandate_granted"`` | ``"mandate_revoked"`` |
            ``"mandate_expired"``) and the payload fields documented in
            spec/29 lines 609-618.

        Raises:
            MandateStateSchemaUnsupported: ``read_state`` returned state with
                an unknown ``schema_version`` (spec/29 MUST #7). Re-raised
                without wrapping — callers must surface this loudly.
        """
        with self._lock:
            state = self._read_state_or_default()
            events: list[dict] = []
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()

            state_mandates: dict[str, dict] = state.get("mandates", {})
            granted_events: list[dict] = []
            revoked_events: list[dict] = []
            expired_events: list[dict] = []

            for mandate in loaded_mandates:
                prior = state_mandates.get(mandate.mandate_id)

                if prior is None:
                    # First time this mandate ID is observed in this scope —
                    # emit mandate_granted (spec/29 line 653: "mandate_granted
                    # emits ONLY when the mandate ID is not in the state file").
                    granted_events.append(
                        {
                            "event": "mandate_granted",
                            "mandate_id": mandate.mandate_id,
                            "granted_by": mandate.granted_by,
                            "granted_at": mandate.granted_at.isoformat(),
                            "expires_at": (
                                mandate.expires_at.isoformat()
                                if mandate.expires_at is not None
                                else None
                            ),
                            "scope": mandate.scope,
                            # spec/29 line 611: constraints is part of payload;
                            # PR 3a ships the raw dict representation; Agent A
                            # (MandateCheck) may expand to structured form later.
                            "source_hash": mandate.source_hash,
                            "ts": now_iso,
                        }
                    )
                    state_mandates[mandate.mandate_id] = {
                        "last_seen_state": mandate.revocation_state.value,
                        "last_seen_revoked_at": (
                            mandate.revoked_at.isoformat()
                            if mandate.revoked_at is not None
                            else None
                        ),
                        "last_seen_expired_at": None,
                        "last_seen_source_hash": mandate.source_hash,
                    }
                    continue

                # Existing mandate — evaluate transition from last_seen_state.
                prior_state_str: str = prior.get("last_seen_state", "active")

                if (
                    prior_state_str == "active"
                    and mandate.revocation_state == RevocationState.REVOKED
                ):
                    # active → revoked transition (spec/29 line 653:
                    # "mandate_revoked emits ONLY when last_seen_state == 'active'
                    # and the loaded state is revoked").
                    revoked_at_iso = (
                        mandate.revoked_at.isoformat()
                        if mandate.revoked_at is not None
                        else now_iso
                    )
                    revoked_events.append(
                        {
                            "event": "mandate_revoked",
                            "mandate_id": mandate.mandate_id,
                            "revoked_at": revoked_at_iso,
                            "revoked_by": mandate.revoked_by,
                            "revocation_reason": mandate.revocation_reason,
                            # spec/29 line 613 also lists cumulative cost fields;
                            # those are queried by Agent A (MandateCheck) against
                            # the LogBackend — out of scope for this module.
                            "ts": now_iso,
                        }
                    )
                    prior["last_seen_state"] = "revoked"
                    prior["last_seen_revoked_at"] = revoked_at_iso

                elif (
                    prior_state_str == "active"
                    and mandate.revocation_state == RevocationState.EXPIRED
                ):
                    # active → expired transition (spec/29 line 653: "mandate_expired
                    # emits ONLY when the derived expired state is reached for the
                    # first time"). EXPIRED is derived from expires_at < now at load
                    # time; mandates.md is never edited (spec/29 line 614).
                    expired_at_iso = (
                        mandate.expires_at.isoformat()
                        if mandate.expires_at is not None
                        else now_iso
                    )
                    expired_events.append(
                        {
                            "event": "mandate_expired",
                            "mandate_id": mandate.mandate_id,
                            "expired_at": expired_at_iso,
                            # spec/29 line 614 also lists cumulative cost fields;
                            # queried by Agent A (MandateCheck) — out of scope here.
                            "ts": now_iso,
                        }
                    )
                    prior["last_seen_state"] = "expired"
                    prior["last_seen_expired_at"] = expired_at_iso

                # Source-hash drift on a still-active mandate: record the
                # updated hash but emit no event. Doctor surfaces hash drift
                # via check_mandate_source_hash_drift (PR 4 scope).
                prior["last_seen_source_hash"] = mandate.source_hash

            # Preserve spec/29-mandated event ordering: granted, revoked, expired.
            events.extend(granted_events)
            events.extend(revoked_events)
            events.extend(expired_events)

            # Persist updated state. Also GC stale throttle entries so the
            # sidecar does not accumulate orphaned throttles over time.
            state["mandates"] = state_mandates
            state["throttles"] = self._gc_expired_throttles(
                state.get("throttles", {}), now_iso
            )
            # write_state is atomic per MandateBackend spec/29 MUST #3
            # (temp + fsync + rename for FilesystemMandateBackend).
            self._mandate_backend.write_state(self._scope, state)

            return events

    def is_rebind_throttled(self, mandate_id: str, agent_run_id: str) -> bool:
        """Return ``True`` when ``(mandate_id, agent_run_id)`` is currently
        throttled.

        The suspicious-rebind throttle (spec/29 lines 394-408) prevents an
        actor from immediately re-binding to a mandate that just surfaced
        ``mandate_state_inconsistent`` (hash mismatch). Per-
        ``(mandate_id, agent_run_id)`` keying means a different agent run
        citing the same mandate is NOT throttled — cross-run usage is not
        serialized.

        Throttle expiry is evaluated by comparing ``expires_at_iso`` against
        the current UTC clock. Cleanup of expired entries happens lazily on
        the next write path (``compute_transitions`` or ``arm_rebind_throttle``)
        — this method is read-only.

        Args:
            mandate_id: The mandate whose throttle to check.
            agent_run_id: The calling agent's current run identifier.

        Returns:
            ``True`` if a live (unexpired) throttle exists for this
            ``(mandate_id, agent_run_id)`` pair. ``False`` otherwise.
        """
        with self._lock:
            state = self._read_state_or_default()
            throttle = state.get("throttles", {}).get(mandate_id)
            if throttle is None:
                return False
            # Throttle is per-(mandate_id, agent_run_id) per spec/29 line 398.
            if throttle.get("agent_run_id") != agent_run_id:
                return False
            expires_at_iso = throttle.get("expires_at_iso")
            if not expires_at_iso:
                return False
            try:
                expires_at = datetime.fromisoformat(expires_at_iso)
            except ValueError:
                # Malformed ISO timestamp in state — treat as not throttled;
                # better to allow than to permanently block on corrupt state.
                return False
            return expires_at > datetime.now(timezone.utc)

    def arm_rebind_throttle(
        self,
        mandate_id: str,
        agent_run_id: str,
        throttle_seconds: int,
    ) -> None:
        """Arm the suspicious-rebind throttle for ``(mandate_id, agent_run_id)``.

        Called by ``MandateCheck`` after step 2 surfaces
        ``mandate_state_inconsistent`` for a mandate (spec/29 lines 396-408).
        Persists the throttle to the state sidecar immediately — in-memory-only
        persistence is explicitly forbidden per spec/29 line 408 and plan-
        subagent Risk C (a crash-restart loop bypasses an in-memory throttle).

        If a throttle for this ``mandate_id`` already exists (e.g., from a
        prior run), it is replaced with a fresh expiry window starting now.
        This is intentional: if the operator has not yet finished the revocation
        edit, the actor should not escape the throttle by waiting for the
        prior one to expire.

        GC of other expired throttle entries runs inside this write.

        Args:
            mandate_id: The mandate to throttle.
            agent_run_id: The calling agent's current run identifier.
            throttle_seconds: Duration (seconds) for which the throttle is
                active. Configured via ``judges.md`` ``## Mandates /
                suspicious_rebind_throttle_s`` (default 60 per spec/29
                line 403). Agent A (MandateCheck) resolves the config value
                and passes it here.
        """
        with self._lock:
            state = self._read_state_or_default()
            now = datetime.now(timezone.utc)
            throttles: dict[str, dict] = state.get("throttles", {})
            throttles[mandate_id] = {
                "agent_run_id": agent_run_id,
                "expires_at_iso": (
                    now + timedelta(seconds=throttle_seconds)
                ).isoformat(),
                "original_state_inconsistent_at": now.isoformat(),
            }
            # GC before write so expired entries don't grow unbounded.
            state["throttles"] = self._gc_expired_throttles(throttles, now.isoformat())
            # Atomic persist per MandateBackend spec/29 MUST #3.
            self._mandate_backend.write_state(self._scope, state)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _read_state_or_default(self) -> dict:
        """Read the scope's state sidecar; return canonical default when absent.

        Delegates to ``MandateBackend.read_state`` which returns ``{}``
        (or a default-empty dict) when the sidecar file does not yet exist
        (spec/29 §"Lifecycle event deduplication": "Returns a default
        empty-state dict when the file is absent — the state file is lazily
        created on the first write_state call").

        Re-raises ``MandateStateSchemaUnsupported`` without wrapping —
        the spec (MUST #7) requires this to be loud: callers must not
        silently migrate forward-incompatible state (spec/29 line 648).

        Returns:
            A mutable dict with at minimum ``schema_version``, ``scope``,
            ``mandates``, and ``throttles`` keys. Callers may mutate and
            pass back to ``write_state``.

        Raises:
            MandateStateSchemaUnsupported: forwarded from ``read_state``
                when the sidecar contains an unknown ``schema_version``.
        """
        try:
            # MandateStateSchemaUnsupported propagates unmodified per MUST #7.
            state = self._mandate_backend.read_state(self._scope)
        except MandateStateSchemaUnsupported:
            raise  # loud; do NOT swallow

        if not state:
            # Absent or empty sidecar — return canonical default (spec/29 line
            # 648: schema_version 1 mandatory from PR 3a).
            return {
                "schema_version": self._DEFAULT_SCHEMA_VERSION,
                "scope": self._scope,
                "mandates": {},
                "throttles": {},
            }

        # Forward-compat: ensure keys added in this PR are present on state
        # read from a PR 1 sidecar that predates "throttles". Additive only —
        # never drop existing keys (spec/29 §"Backward compatibility").
        state.setdefault("throttles", {})
        state.setdefault("mandates", {})
        state.setdefault("schema_version", self._DEFAULT_SCHEMA_VERSION)
        return state

    def _gc_expired_throttles(self, throttles: dict, now_iso: str) -> dict:
        """Return a copy of ``throttles`` with expired entries removed.

        An entry is expired when its ``expires_at_iso`` is at or before
        ``now_iso``. Malformed / missing ``expires_at_iso`` entries are
        also dropped (they can never un-expire, so retaining them would
        permanently grow the state sidecar — cleaner to discard).

        This method does NOT take the instance lock — it is always called
        from within a ``with self._lock`` block (``compute_transitions`` and
        ``arm_rebind_throttle``). Callers hold the lock; this function is
        pure-transformation over dicts.

        Args:
            throttles: The current ``"throttles"`` sub-dict from state.
            now_iso: Current UTC time as an ISO 8601 string.

        Returns:
            A new dict containing only the non-expired entries.
        """
        try:
            now = datetime.fromisoformat(now_iso)
        except ValueError:
            # Malformed now_iso — this should never happen (we always pass
            # datetime.now(timezone.utc).isoformat()). Defensively keep all
            # entries rather than dropping the whole throttle map.
            return dict(throttles)

        result: dict[str, dict] = {}
        for mandate_id, entry in throttles.items():
            expires_at_iso = entry.get("expires_at_iso")
            if not expires_at_iso:
                # No expiry recorded — drop (cannot determine liveness).
                continue
            try:
                expires_at = datetime.fromisoformat(expires_at_iso)
            except ValueError:
                # Malformed expiry — drop.
                continue
            if expires_at > now:
                result[mandate_id] = entry
        return result

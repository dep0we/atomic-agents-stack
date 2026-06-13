"""QueueBackend Protocol — the contract every queue implementation satisfies.

This is one of the open protocols in the protocol-pattern series (spec/44).
It carves the cascade work-queue cluster from atomic_agents/_cascade.py
into a swappable Protocol so future Redis / SQS / DB backends can plug in
without forking the claim logic.

Closes TENSIONS T4 (cascade queue is filesystem-only).

Protocol method surface (Hybrid Option B, per arc ruling 428-pr1-args.json):

  Four atomicity primitives ON the Protocol:
    claim_next(role, lease_token, lease_seconds)  — atomic claim (POSIX rename or equiv)
    release(lease_token, original_name)           — move to done/ (atomic)
    move_to_dead_letter(lease_token, original_name, reason)  — terminal failure
    renew_lease(lease_token, original_name, additional_seconds)  — extend lease

  One enumeration READ primitive ON the Protocol:
    list_claimed(role=None)  — return currently-held items (basis for recovery)

  Shared recovery code ABOVE the Protocol:
    recover_stale_claims(backend, lease_seconds) — free function calling
    list_claimed() + the backend's reclaim primitive
    (_reclaim_to_recovered, or _recover_stale_claims_native for the
    filesystem fast path) so every backend gets the same recovery logic
    with no drift. It does NOT call claim_next().

  Plus the standard Protocol surface:
    capabilities()    — return QueueCapabilities
    export(query)     — spec/40 canonical export

Atomicity guarantee on Protocol primitives:
  The PRIMARY guarantee for claim_next / release / move_to_dead_letter is
  the atomicity of the STATE TRANSITION (the work file moves from one
  well-known directory to another in an all-or-nothing operation). For the
  filesystem backend this is a POSIX rename. Sidecar writes (.lease.json,
  .reason.txt) are BEST-EFFORT and may be absent after a crash — this does
  NOT violate the guarantee. The work file's new location determines the
  item's state; an orphaned sidecar in claimed/ after a crash is harmless
  (recovery falls to mtime fallback).

State vocabulary (spec/06 conceptual → on-disk):
  spec/06 'pending'     → queue/queued/<role>/
  spec/06 'in_progress' → queue/claimed/<lease_token>/
  spec/06 'completed'   → queue/done/<lease_token>/
  spec/06 'dead_letter' → queue/dead-letter/<lease_token>/

The scope token is project_root (NOT agent_root) — this is the one
project-scoped backend in the v1.5 wave, matching spec/06 where the queue
is a shared project resource, not a per-agent resource.

See docs/spec/44-queue-backend.md for the full normative contract.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from .types import QueueCapabilities, QueueItem, QueueExport

_logger = logging.getLogger(__name__)


@runtime_checkable
class QueueBackend(Protocol):
    """Contract every queue backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The @runtime_checkable decorator enables
    isinstance(obj, QueueBackend) to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope: bound at construction. FilesystemQueueBackend(project_root)
    operates on <project_root>/queue/ (the shared cascade work queue).
    The scope token is project_root (NOT agent_root) — this divergence
    from the agent-scope siblings is correct per spec/06, not a defect.

    The backend is STATELESS at the Protocol level — it holds project_root
    only. All in-memory state is managed by the caller above the Protocol.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'filesystem', 'postgres', 'redis'.

        Used by the registry for lookup and by diagnostic tooling. Treat as
        a backwards-compatibility surface — operator deployments may pin
        against these strings.
        """
        ...

    def claim_next(
        self,
        role: str,
        lease_token: str,
        lease_seconds: int = 3600,
    ) -> QueueItem | None:
        """Atomically claim the next pending work item for the given role.

        Atomicity guarantee (spec/44 MUST 4, see MUST 9 for the rename
        primitive): the state transition from 'queued' to 'claimed' is
        all-or-nothing. Under concurrent callers, only ONE caller claims any
        given item — no double-claim.

        For the filesystem backend, atomicity comes from POSIX rename.
        The sidecar write (.lease.json) is best-effort and may be absent
        after a crash — the work file's location in claimed/ is the
        authoritative state record.

        Args:
            role: the role queue to claim from (e.g. 'writer').
            lease_token: caller-supplied unique identifier for this claim
                session. Namespaces the claimed/ directory.
            lease_seconds: lease duration in seconds (default 3600).
                After this time, recover_stale_claims() may reclaim.

        Returns:
            A QueueItem (or FilesystemQueueItem for filesystem backends)
            with original_name, role, lease_token, claimed_at set.
            Returns None when the queue is empty.
        """
        ...

    def release(self, lease_token: str, original_name: str) -> None:
        """Mark a claimed item as completed by moving it to done/.

        Atomicity guarantee (spec/44 MUST 4): the state transition from
        'claimed' to 'done' is all-or-nothing (the work file moves).
        Sidecar cleanup (.lease.json removal) is best-effort.

        Args:
            lease_token: the lease identifier used in claim_next().
            original_name: the original filename of the work item.
        """
        ...

    def move_to_dead_letter(
        self,
        lease_token: str,
        original_name: str,
        reason: str = "",
    ) -> None:
        """Move a claimed item to dead-letter/ — a terminal failure state.

        Atomicity guarantee (spec/44 MUST 10): dead-work-stays-dead. The
        state transition from 'claimed' to 'dead-letter' is all-or-nothing.
        Once a file is in dead-letter/, no claim, recover, or release
        operation affects it. Sidecar cleanup is best-effort.

        Args:
            lease_token: the lease identifier used in claim_next().
            original_name: the original filename of the work item.
            reason: optional failure reason. Written as a .reason.txt
                sidecar alongside the dead-letter item.
        """
        ...

    def renew_lease(
        self,
        lease_token: str,
        original_name: str,
        additional_seconds: int | None = None,
    ) -> None:
        """Extend the lease for an actively-worked item.

        Updates the lease expiry to now + additional_seconds. If
        additional_seconds is None, reuses the original lease_seconds
        from the sidecar (i.e., resets to full duration from now).

        Long-running workers should call this periodically — recommended
        cadence is every lease_seconds / 3 seconds — to prevent
        recover_stale_claims() from reclaiming an actively-worked item.

        Args:
            lease_token: the lease identifier used in claim_next().
            original_name: the original filename of the work item.
            additional_seconds: new lease duration from now. None = reuse
                the original lease_seconds.
        """
        ...

    def list_claimed(self, role: str | None = None) -> list[QueueItem]:
        """Return all currently-held (claimed) work items.

        This is the enumeration READ primitive that enables recover_stale_claims
        to be implemented as shared code ABOVE the Protocol, calling
        list_claimed + the backend's reclaim primitive
        (_reclaim_to_recovered / _recover_stale_claims_native) — NOT claim_next.

        For the filesystem backend: scans queue/claimed/ subdirectories and
        returns one QueueItem (FilesystemQueueItem) per work file, with
        lease_expires_at populated from the .lease.json sidecar (or None
        for legacy claims without a sidecar).

        Args:
            role: optional filter — return only items from this role's
                queue. None = return all claimed items across all roles.

        Returns:
            List of QueueItem objects (or subclasses). Empty list when
            claimed/ is absent or no items are currently held.
        """
        ...

    def export(self, query: Any = None) -> QueueExport:
        """Export durable queue state as a canonical QueueExport (spec/40).

        INCLUDES (durable, irreplaceable):
            - queue/queued/<role>/* — pending backlog
            - queue/done/<lease_token>/* — completed items
            - queue/dead-letter/<lease_token>/* — permanently failed items
            - .reason.txt sidecars alongside dead-letter items

        EXCLUDES (ephemeral, double-claim hazard):
            - queue/claimed/<lease_token>/* — in-flight items
            - All .lease.json sidecar files

        The structural exclusion (whitelist: enumerate only queued/done/
        dead-letter) mirrors the LOCKED LockExport.lock_file_names=[] precedent.

        Args:
            query: unused (reserved for future bounded-export filtering).

        Returns:
            QueueExport with items_with_bytes (relative to project_root),
            backend_id, and scope.
        """
        ...

    def export_all(self) -> QueueExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        ...

    def capabilities(self) -> QueueCapabilities:
        """Backend capability declaration — see QueueCapabilities.

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.

        MUST 12: single_host_only advertised honestly. See QueueCapabilities
        docstring for the full conformance contract.
        """
        ...


# ──────────────────────────────────────────────────────────────────
# Free-function recovery layer (ABOVE the Protocol)


def recover_stale_claims(
    backend: QueueBackend,
    lease_seconds: int = 3600,
) -> list[QueueItem]:
    """Find claimed items whose lease has expired and move them back to queued.

    This is shared recovery code ABOVE the Protocol, built from list_claimed()
    + the backend's reclaim primitive (_reclaim_to_recovered, or
    _recover_stale_claims_native for the filesystem fast path) — it does NOT
    call claim_next(). This means EVERY registered QueueBackend gets the same
    recovery logic with no drift — a Redis backend, a Postgres backend, and the
    filesystem backend all use this function.

    Per arc-ruling 428-pr1-args.json hybrid-protocol-surface: recover_stale_claims
    is a free function above the Protocol. The filesystem-specific internals
    (path.rename to _recovered/, mtime fallback, malformed-sidecar fall-through)
    live in FilesystemQueueBackend's implementation of list_claimed() — NOT here.
    This function only checks lease_expires_at and calls Protocol primitives.

    Lease detection: each QueueItem returned by list_claimed() carries
    lease_expires_at. When that timestamp is in the past (or None and the item
    is older than lease_seconds by mtime — checked by the backend's list_claimed
    impl), the item is stale. The stale item is moved back to queued via the
    backend's internal reclaim primitive — ``_reclaim_to_recovered(item)``; for
    the filesystem backend this renames the work file into ``queued/_recovered/``
    (there is no "claim from _recovered/" step). A non-filesystem backend MUST
    provide ``_reclaim_to_recovered`` for recovery to function.

    NOTE for filesystem backend: FilesystemQueueBackend.list_claimed() directly
    handles the mtime fallback and malformed-sidecar fall-through. This function
    sees only the normalized QueueItem with lease_expires_at populated (or None
    for items the backend's list_claimed() already determined are stale via mtime).

    Args:
        backend: any registered QueueBackend implementation.
        lease_seconds: lease duration in seconds (used for mtime fallback
            when list_claimed returns items with lease_expires_at=None).

    Returns:
        List of QueueItem objects for the items that were recovered
        (now back in queued state).
    """
    from datetime import datetime, timezone
    import time

    # Delegate to the backend's native recover_stale_claims if it exposes one.
    # FilesystemQueueBackend implements this natively for performance (one pass
    # over claimed/ with mtime fallback), but the Protocol path above is the
    # canonical cross-backend implementation.
    if hasattr(backend, "_recover_stale_claims_native"):
        return backend._recover_stale_claims_native(lease_seconds=lease_seconds)  # type: ignore[attr-defined]

    # Generic Protocol-based recovery (for non-filesystem backends):
    now = time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    recovered: list[QueueItem] = []

    claimed_items = backend.list_claimed()
    for item in claimed_items:
        is_stale = False
        if item.lease_expires_at is not None:
            try:
                expires_dt = datetime.fromisoformat(item.lease_expires_at)
                if expires_dt.tzinfo is None:
                    expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                is_stale = expires_dt < now_dt
            except (ValueError, OSError):
                # Malformed lease_expires_at — treat as stale
                is_stale = True
        else:
            # No expiry info — treat as stale (conservative, safe for recovery)
            is_stale = True

        if is_stale:
            # Move back to queued via the backend's internal reclaim mechanism.
            # For filesystem: item is in claimed/, we move it to queued/_recovered/.
            # This is done by the backend's own release/reclaim primitive.
            reclaim = getattr(backend, "_reclaim_to_recovered", None)
            if reclaim is None:
                # A backend that structurally conforms to the Protocol but omits
                # this private primitive would silently recover nothing — make
                # the contract gap loud rather than returning [] with no signal.
                _logger.warning(
                    "QueueBackend %s found a stale claimed item but exposes no "
                    "_reclaim_to_recovered() primitive; cannot recover it. A "
                    "non-filesystem backend MUST provide _reclaim_to_recovered "
                    "for recover_stale_claims() to function.",
                    type(backend).__name__,
                )
                continue
            result = reclaim(item)
            if result is not None:
                recovered.append(result)

    return recovered

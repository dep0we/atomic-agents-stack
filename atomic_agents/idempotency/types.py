"""Canonical types for the IdempotencyBackend Protocol (spec/45).

IdempotencyBackend is the eighteenth backend Protocol in the atomic-agents
framework (v1.5 wave). It provides a deduplication ledger so that a run
identified by a caller-supplied idempotency key either executes once or
reports the result of a prior completion.

Closes the "at-most-once execution" gap for trigger-fired agents (serve,
queue, cron) where the same trigger may fire more than once.

NOTE: WritePolicy is NOT part of the IdempotencyBackend Protocol. The ledger
path is fixed at construction (agent_root/idempotency/). This mirrors
QueueBackend and GoalBackend, not MemoryBackend. The conformance suite MUST
NOT include a WritePolicy test for IdempotencyBackend.

State vocabulary:
    FRESH    — key has never been seen; the caller may proceed and own the run.
    IN_FLIGHT — a prior begin() claimed this key and commit() has not been called;
                the caller should wait or abort.
    COMPLETED — commit() was called for this key; the prior result reference is
                available in prior_result_ref.

DedupDecision is a VALUE OBJECT: begin() ALWAYS returns DedupDecision in all
non-error code paths. begin() MUST NOT raise any exception to signal a known
duplicate state. Only unrecoverable I/O errors raise IdempotencyBackendError.

See docs/spec/45-idempotency-backend.md for the full normative contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .._export_base import ExportableResult

# ──────────────────────────────────────────────────────────────────
# State constants — use these instead of bare strings

FRESH = "fresh"
IN_FLIGHT = "in_flight"
COMPLETED = "completed"

IdempotencyState = Literal["fresh", "in_flight", "completed"]


# ──────────────────────────────────────────────────────────────────
# Core Protocol types


@dataclass(frozen=True)
class DedupDecision:
    """Value-object return from IdempotencyBackend.begin() (spec/45 MUST 1).

    begin() ALWAYS returns a DedupDecision — it NEVER raises to signal a
    known duplicate. Only unrecoverable I/O errors (PathTraversalError,
    OSError) propagate as exceptions.

    Fields:
        is_duplicate: True when the key was previously seen (IN_FLIGHT or
            COMPLETED). False when the key is FRESH — the caller owns the run.
        state: REQUIRED (no default). One of 'fresh', 'in_flight', 'completed'.
            Use the module-level constants FRESH, IN_FLIGHT, COMPLETED.
            MUST be set even when is_duplicate=False (state='fresh').
            Distinguishes wait-and-retry (IN_FLIGHT) from use-prior-result
            (COMPLETED) — callers MUST inspect state, not only is_duplicate.
        prior_run_id: the run_id string recorded when begin() first claimed
            this key. None when state='fresh'. Opaque caller-supplied string
            stored in the ledger at begin() time.
        prior_result_ref: the opaque result reference stored by commit().
            None when state='fresh' or state='in_flight'.
    """

    is_duplicate: bool
    state: IdempotencyState  # REQUIRED — no default (spec/45 MUST 1)
    prior_run_id: str | None
    prior_result_ref: str | None


@dataclass(frozen=True)
class IdempotencyCapabilities:
    """Per-backend capability declaration for IdempotencyBackend (spec/45).

    Matches the frozen-dataclass convention of every other *Capabilities type.

    Fields:
        backend_id: stable backend identifier string (required, no default).
        single_host_only: True when the backend is safe ONLY for single-host
            deployments. FilesystemDedupLedger claims True because O_EXCL
            file-creation atomicity does not extend across hosts.

            This field is REQUIRED (no default) so a new backend that omits
            it gets a TypeError at instantiation rather than silently claiming
            False (multi-host-safe when it is not). Matches LockCapabilities
            and QueueCapabilities — the single-vs-multi-host axis is always
            relevant.

        atomic_claim: True when begin() provides an atomic check-and-reserve
            (first call wins; concurrent second call returns IN_FLIGHT, not
            FRESH). FilesystemDedupLedger claims True via O_EXCL.

            This field is REQUIRED (no default) — always-relevant for dedup
            correctness. A backend forgetting to set it would silently advertise
            non-atomic claims, violating the protocol's core guarantee.

        supports_ttl: True when the backend implements TTL sweep so that stale
            in-flight leases are automatically expired. FilesystemDedupLedger
            ships supports_ttl=False in PR1 — the sweep is a follow-up.
            Default False so existing instantiation sites keep working.

        supports_canonical_export: True when the backend implements the
            spec/40 Exportable Protocol. FilesystemDedupLedger=True.
            Default False to match the backward-compatible pattern used by
            LogCapabilities and QueueCapabilities.

    NOTE: WritePolicy is NOT part of the IdempotencyBackend Protocol. The ledger
    path is fixed at construction. Mirrors QueueBackend and GoalBackend, not
    MemoryBackend. The conformance suite MUST NOT include a WritePolicy test.

    Field ordering: backend_id (required, no default) first so positional
    construction IdempotencyCapabilities("filesystem", True, True) is
    meaningful; then the two required boolean axes; then optional booleans
    with defaults last.
    """

    backend_id: str
    single_host_only: (
        bool  # REQUIRED — no default (LockCapabilities/QueueCapabilities pattern)
    )
    atomic_claim: bool  # REQUIRED — no default (always-relevant dedup axis)
    supports_ttl: bool = False
    supports_canonical_export: bool = False


# ──────────────────────────────────────────────────────────────────
# Export result type


@dataclass
class IdempotencyExport(ExportableResult):
    """Canonical export from an IdempotencyBackend (spec/40 §"Per-backend export contracts").

    Embeds raw bytes for TERMINAL ledger entries ONLY.

    INCLUDES (durable, irreplaceable):
        - Terminal entries: key + prior_run_id + result_ref + terminal flag
          These represent completed deduplication records that must survive
          migration. Their marker-only shape means they are small and safe
          to embed.

    EXCLUDES (ephemeral, phantom-block hazard):
        - In-flight lease files — exporting an in-flight entry and importing
          it to a new deployment would cause begin() to return IN_FLIGHT on
          a key that was never completed (permanent phantom block).

    The structural exclusion (whitelist: enumerate only terminal marker files)
    mirrors QueueBackend's claimed/ exclusion precedent.

    Fields:
        entries_with_bytes: list of (relative_path_str, raw_bytes) tuples
            for all terminal ledger entries. relative_path_str is relative
            to agent_root (e.g. 'idempotency/<key_hash>.terminal.json').
            Excludes lease files ('*.lease.json').
        backend_id: stable backend identifier.
        scope: agent root path as a string.
    """

    entries_with_bytes: list[tuple[str, bytes]]  # list of (relative_path_str, bytes)
    backend_id: str
    scope: str  # agent root path as a string

"""Canonical types for the QueueBackend Protocol (spec/44).

QueueBackend is the seventeenth backend Protocol in the atomic-agents
framework (v1.5 wave). It carves the cascade work-queue cluster from
atomic_agents/_cascade.py into a swappable Protocol so future Redis /
SQS / DB backends can plug in without forking the claim logic.

Closes TENSIONS T4 (cascade queue is filesystem-only).

NOTE: WritePolicy is NOT part of the QueueBackend Protocol. The queue
path is fixed at construction (project_root/queue/) and does not require
per-call policy enforcement. Mirrors GoalBackend and LogBackend, not
MemoryBackend. The conformance suite MUST NOT include a WritePolicy test
for QueueBackend.
Per arc-ruling 428-pr1-args.json writepolicy-presence: Option 1.

State vocabulary mapping (spec/06 conceptual → on-disk):
  spec/06 'pending'     → on-disk directory: queue/queued/<role>/
  spec/06 'in_progress' → on-disk directory: queue/claimed/<lease_token>/
  spec/06 'completed'   → on-disk directory: queue/done/<lease_token>/
  spec/06 'dead_letter' → on-disk directory: queue/dead-letter/<lease_token>/

The spec/06 vocabulary is conceptual and pre-dates this spec. spec/44 is
normative for the on-disk directory layout. Operators with existing queue
directories under the _cascade.py layout (queued/claimed/done/dead-letter)
are unaffected — the layout did not change.

See docs/spec/44-queue-backend.md for the full normative contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .._export_base import ExportableResult


# ──────────────────────────────────────────────────────────────────
# Abstract Protocol-level types (NO filesystem-specific fields)


@dataclass
class QueueItem:
    """One claimed work item returned by QueueBackend.claim_next().

    This is the ABSTRACT Protocol-level type. It carries only
    backend-agnostic fields so future Redis / SQS / DB backends can
    return QueueItem without fabricating a filesystem path.

    Fields:
        original_name: the filename of the work item as it existed in
            the queued state (e.g. '001_chapter_a.md').
        role: the role queue from which this item was claimed.
        lease_token: the caller-supplied lease identifier. Unique per
            claim session; used to namespace the claimed/, done/, and
            dead-letter/ directories in the filesystem backend.
        claimed_at: wall-clock epoch timestamp of when the item was
            claimed (time.time()). Populated by claim_next().
        lease_expires_at: ISO-8601 timestamp string of when the lease
            expires. Populated from the sidecar on filesystem; from the
            backend's own record on non-filesystem backends. None when
            not populated (legacy claims, or backends that do not track
            expiry on the QueueItem surface directly).
    """

    original_name: str
    role: str
    lease_token: str
    claimed_at: float
    lease_expires_at: str | None = None

    # NOTE: path is intentionally ABSENT from this abstract type.
    # FilesystemQueueItem (defined in queue/filesystem.py) adds path: Path
    # for filesystem-specific callers (cron scripts via spec/06).
    # See arc-ruling 428-pr1-args.json adopt-now-vs-scaffolding-only.


@dataclass(frozen=True)
class QueueCapabilities:
    """Per-backend capability declaration for QueueBackend (spec/44).

    Matches the frozen-dataclass convention of every other *Capabilities type.

    Fields:
        backend_id: stable backend identifier string (required, no default).
        single_host_only: True when the backend is safe ONLY for single-host
            deployments. FilesystemQueueBackend claims True because POSIX
            rename atomicity does not extend across hosts. Named single_host_only
            (NOT multi_host_safe) to match the existing LockCapabilities.single_host_only
            field convention.

            This field is REQUIRED (no default) so a new backend that omits it
            gets a TypeError at instantiation rather than silently claiming False
            (multi-host-safe when it is not). This matches LockCapabilities —
            the single-vs-multi-host axis is always relevant, unlike the
            optional-feature booleans that default to False.

        supports_canonical_export: True when the backend implements the
            spec/40 Exportable Protocol. FilesystemQueueBackend=True.
            Default False so existing instantiation sites without this kwarg
            keep working (backward-compatibility pattern from LogCapabilities).

    NOTE: WritePolicy is NOT part of the QueueBackend Protocol. Queue path is
    fixed at construction (project_root/queue/). Mirrors GoalBackend and
    LogBackend, not MemoryBackend. The conformance suite MUST NOT include a
    WritePolicy test for QueueBackend. Per arc-ruling 428-pr1-args.json
    writepolicy-presence: Option 1.

    MUST 12 contract (single_host_only=True conformance):
    A QueueBackend claiming single_host_only=True MUST advertise this
    consistently across ALL calls to capabilities(). The conformance suite
    MUST verify that a backend claiming True (a) has capabilities().single_host_only
    == True on every call, and (b) does NOT self-contradict by claiming False on
    a second call. The doctor check MUST issue a WARN (not FAIL) when
    capabilities().single_host_only=True is detected in a deployment that declares
    multi-host operation (via ATOMIC_AGENTS_MULTI_HOST=true).

    Field ordering: backend_id (required, no default) first so positional
    construction QueueCapabilities("filesystem", True) is meaningful;
    then single_host_only (required, no default — always-relevant axis);
    then optional capability booleans with defaults last.
    """

    backend_id: str
    single_host_only: bool  # REQUIRED — no default (LockCapabilities pattern)
    supports_canonical_export: bool = False


# ──────────────────────────────────────────────────────────────────
# Export result type


@dataclass
class QueueExport(ExportableResult):
    """Canonical export from a QueueBackend (spec/40 §"Per-backend export contracts").

    Embeds raw bytes for the DURABLE queue-owned state:
        - queued/<role>/* — pending backlog (irreplaceable, cannot be
          reconstructed from LogBackend audit stream)
        - done/<lease_token>/* — completed work items
        - dead-letter/<lease_token>/* — permanently failed items
        - .reason.txt sidecars — failure reasons for dead-letter items

    EXCLUDES (ephemeral, double-claim hazard):
        - claimed/<lease_token>/* — in-flight work items
        - All .lease.json sidecar files — runtime-bound lease records

    This mirrors the LOCKED LockExport.lock_file_names=[] precedent:
    runtime lease state is ephemeral and MUST NOT be included.

    The structural exclusion (whitelist: enumerate only queued/done/dead-letter)
    is preferred over a filter-based exclusion to guarantee the invariant.

    OPEN spec/44 nuance (to be settled in adversarial rounds): whether
    done/ and dead-letter/ are EMBEDDED or treated as reconstructable from
    LogBackend. The queued/ backlog exports regardless. FilesystemQueueBackend
    embeds all three durable directories.

    Fields:
        items_with_bytes: list of (relative_path_str, raw_bytes) tuples
            for all durable queue files. relative_path_str is relative to
            project_root (e.g. 'queue/queued/writer/001_chapter.md').
            Excludes claimed/ directory and all .lease.json files.
        backend_id: stable backend identifier.
        scope: project root path as a string.
    """

    items_with_bytes: list[tuple[str, bytes]]  # list of (relative_path_str, bytes)
    backend_id: str
    scope: str  # project root path as a string

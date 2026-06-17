"""IdempotencyBackend Protocol — the contract every dedup implementation satisfies.

This is one of the open protocols in the protocol-pattern series (spec/45).
It provides an at-most-once execution guarantee for trigger-fired agents by
maintaining a deduplication ledger keyed on caller-supplied idempotency keys.

Protocol method surface:

  Three operations ON the Protocol:
    begin(key, run_id)                 — atomic check-reserve-or-report
    commit(key, result_ref)            — mark key as permanently COMPLETED
    lookup(key)                        — read-only state query (no side effects)

  Plus the standard Protocol surface:
    capabilities()    — return IdempotencyCapabilities
    export(query)     — spec/40 canonical export (TERMINAL entries only)

Atomicity guarantee:
  begin() is a single atomic check-reserve-or-report (FRESH-reserved |
  IN_FLIGHT | COMPLETED). For the filesystem backend, atomicity is provided
  by O_EXCL file creation: the first caller to open(path, O_EXCL) wins FRESH;
  concurrent second callers get FileExistsError and return IN_FLIGHT.

begin() VALUE-OBJECT contract:
  begin() ALWAYS returns DedupDecision — it NEVER raises to signal a known
  duplicate state (FRESH, IN_FLIGHT, COMPLETED). Only unrecoverable I/O errors
  (PathTraversalError, OSError from disk failure) propagate as exceptions.
  This is the load-bearing portability property: callers branch on
  DedupDecision.state, not try/except.

Scope:
  agent_root (NOT project_root) — mirrors GoalBackend, JournalBackend.
  FilesystemDedupLedger is agent-scoped; cross-agent dedup requires a
  shared backend (Redis, Postgres, or a project-root-scoped FilesystemDedupLedger
  instantiated at the project root) — see spec/45 §"Scope" for the follow-up
  issue reference.

See docs/spec/45-idempotency-backend.md for the full normative contract.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from .types import IdempotencyCapabilities, DedupDecision, IdempotencyExport

_logger = logging.getLogger(__name__)


@runtime_checkable
class IdempotencyBackend(Protocol):
    """Contract every idempotency backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The @runtime_checkable decorator enables
    isinstance(obj, IdempotencyBackend) to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope: bound at construction. FilesystemDedupLedger(agent_root)
    operates on <agent_root>/idempotency/. This is an agent-scoped backend
    matching GoalBackend/JournalBackend, not the project-scoped QueueBackend.

    The backend is STATELESS at the Protocol level — it holds agent_root
    only. All in-memory state is managed by the caller above the Protocol.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'filesystem', 'redis', 'postgres'.

        Used by the registry for lookup and by diagnostic tooling. Treat as
        a backwards-compatibility surface — operator deployments may pin
        against these strings.
        """
        ...

    def begin(self, key: str, run_id: str) -> DedupDecision:
        """Atomic check-reserve-or-report for an idempotency key (spec/45 MUST 4).

        Single atomic operation: check whether key was seen before, and if not,
        reserve it. Returns a DedupDecision VALUE OBJECT — NEVER raises to
        signal a duplicate. Only unrecoverable I/O errors propagate as exceptions.

        State transitions:
            Key absent (FRESH):   reserves the key, returns DedupDecision(
                                      is_duplicate=False, state='fresh',
                                      prior_run_id=None, prior_result_ref=None)
            Key in-flight:        returns DedupDecision(
                                      is_duplicate=True, state='in_flight',
                                      prior_run_id=<original_run_id>,
                                      prior_result_ref=None)
            Key completed (TERMINAL): returns DedupDecision(
                                      is_duplicate=True, state='completed',
                                      prior_run_id=<original_run_id>,
                                      prior_result_ref=<result_ref from commit()>)

        Atomicity guarantee (spec/45 MUST 4):
            For the filesystem backend, the O_EXCL create is the atomic
            boundary. Under concurrent callers, exactly one receives FRESH;
            the rest receive IN_FLIGHT. No TOCTOU window between check and reserve.

        Args:
            key: caller-supplied idempotency key. Must be a bare filename
                component (no path separators, not empty, not '.' or '..').
                The backend hashes this key for the on-disk path; the original
                key is stored inside the ledger entry for verification.
            run_id: opaque caller-supplied run identifier stored in the lease
                (e.g. the agent's run_id from the current call). Used to
                populate prior_run_id on subsequent calls.

        Returns:
            DedupDecision with is_duplicate, state, prior_run_id, prior_result_ref.

        Raises:
            PathTraversalError: when key is invalid (contains path separators,
                is empty, '.' or '..', or contains NUL/control characters).
                Raised by key validation BEFORE any I/O — distinct from the
                IdempotencyBackendError I/O-failure path below.
            IdempotencyBackendError: on unrecoverable I/O failure (disk error,
                permission denied, symlink escape). NOT raised for duplicate
                detection — that is expressed as DedupDecision(is_duplicate=True).
        """
        ...

    def commit(self, key: str, result_ref: str) -> None:
        """Mark a previously-claimed key as permanently COMPLETED (spec/45 MUST 5).

        Writes a MARKER-ONLY terminal entry: key + prior_run_id + result_ref +
        terminal flag. Does NOT store result content. result_ref is an opaque
        reference string (a run_id, path, or URI) — the caller owns the actual
        result bytes.

        Atomicity: uses atomic_write (temp + fsync + rename) so a crash between
        open and close leaves a .tmp file, not a corrupt terminal marker. A corrupt
        terminal marker that passes the file-exists check in begin() but fails JSON
        parsing would re-expose FRESH, silently re-executing a completed run.

        After commit(), begin(key) returns DedupDecision(is_duplicate=True,
        state='completed', prior_run_id=..., prior_result_ref=result_ref).

        Args:
            key: the same key passed to begin() (must have FRESH claim from begin()).
            result_ref: opaque result reference string (max 1024 characters,
                counted via len()). result_ref is stored verbatim as a JSON VALUE
                in the terminal marker — it is NEVER used as a path component on
                disk (the on-disk filename is derived from sha256(key)). Path
                separators ('/', '\\') are therefore PERMITTED: a URI
                (s3://bucket/key) or a path (runs/2026/abc.json) is the most
                natural result reference. Only the length bound is enforced.

        Raises:
            PathTraversalError: when key is invalid (contains path separators,
                is empty, '.' or '..', or contains NUL/control characters).
                Raised by key validation BEFORE any I/O — distinct from the
                IdempotencyBackendError I/O-failure path below.
            IdempotencyBackendError: on I/O failure or an over-length result_ref.
                NOT raised for a separator-bearing result_ref (separators are
                permitted) and NOT raised for duplicate detection.
        """
        ...

    def lookup(self, key: str) -> DedupDecision:
        """Read-only state query for an idempotency key (spec/45 MUST 6).

        Returns the current DedupDecision WITHOUT reserving the key. Equivalent
        to begin() but with no side effects — does not create a lease.

        Returns DedupDecision(is_duplicate=False, state='fresh', ...) when the
        key is absent. This is the authoritative FRESH signal — no fallback scan.

        Args:
            key: caller-supplied idempotency key (same validation as begin()).

        Returns:
            DedupDecision with current state. FRESH when key is unknown.

        Raises:
            PathTraversalError: when key is invalid (contains path separators,
                is empty, '.' or '..', or contains NUL/control characters).
                Raised by key validation BEFORE any I/O — distinct from the
                IdempotencyBackendError I/O-failure path below.
            IdempotencyBackendError: on unrecoverable I/O failure.
        """
        ...

    def release_lease(self, key: str) -> None:
        """Best-effort release of an IN_FLIGHT lease (spec/45 MUST 13).

        Called by ``agent.call()`` on error/exception (via try/finally) to
        remove the in-flight lease so that TTL-free deployments do not wedge
        permanently. This is a best-effort operation — a genuine I/O failure
        raises ``IdempotencyBackendError`` but the call() finally block catches
        it (must not propagate from the finally block and interrupt lock release).

        MUST be idempotent: no error raised when the lease file does not exist
        (already committed, or never created). MUST NOT raise on a missing key.

        Key validation is run before any I/O — ``PathTraversalError`` on invalid
        key (caller bug surfaced loudly). Only raises ``IdempotencyBackendError``
        on genuine I/O failure (EACCES, ENOSPC, etc.) — not on ENOENT.

        Raises:
            PathTraversalError: when ``key`` is invalid.
            IdempotencyBackendError: on genuine I/O failure other than ENOENT.
        """
        ...

    def export(self, query: Any = None) -> IdempotencyExport:
        """Export TERMINAL ledger entries as a canonical IdempotencyExport (spec/40).

        INCLUDES (durable, irreplaceable):
            - Terminal marker entries (*.terminal.json): completed dedup records

        EXCLUDES (ephemeral, phantom-block hazard):
            - In-flight lease files (*.lease.json) — exporting an in-flight
              entry and importing it to a new deployment permanently blocks
              begin() for that key.

        The structural exclusion (whitelist: enumerate only *.terminal.json
        files) is preferred over filter-based exclusion to guarantee the
        invariant.

        Args:
            query: unused (reserved for future bounded-export filtering).

        Returns:
            IdempotencyExport with entries_with_bytes (relative to agent_root),
            backend_id, and scope.
        """
        ...

    def export_all(self) -> IdempotencyExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        ...

    def capabilities(self) -> IdempotencyCapabilities:
        """Backend capability declaration — see IdempotencyCapabilities.

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.

        MUST: single_host_only and atomic_claim advertised honestly (both
        REQUIRED fields, no defaults). See IdempotencyCapabilities docstring
        for the full conformance contract.
        """
        ...

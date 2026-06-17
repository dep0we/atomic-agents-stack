"""FilesystemDedupLedger — directory-tree reference implementation (spec/45).

This is the default backend for single-host deployments. It stores idempotency
ledger entries under <agent_root>/idempotency/ as JSON files.

Directory layout (under agent_root/idempotency/):
    <key_hash>.lease.json      — in-flight lease marker (ephemeral)
    <key_hash>.terminal.json   — completed terminal marker (permanent, marker-only)

Where <key_hash> = sha256(key.encode()).hexdigest() — the full, untruncated
64-char hex digest (no slicing). The original key is stored inside the JSON
entry for round-trip verification (hash collision guard: if the stored key
doesn't match the caller's key on a lookup, the entry is treated as a hash
collision and skipped).

Atomicity contract for begin() (spec/45 MUST 4):
    The PRIMARY guarantee is O_EXCL (os.open with O_WRONLY|O_CREAT|O_EXCL)
    on the lease file. Under concurrent callers, exactly one open() succeeds
    for any given key; the loser gets FileExistsError (maps to EEXIST) and
    reads the existing entry to determine IN_FLIGHT vs COMPLETED.

    Sequence:
        1. Check for terminal marker (COMPLETED fast path — no O_EXCL needed)
        2. Attempt O_EXCL create of lease file → FRESH on success
        3. On FileExistsError → read existing lease file → IN_FLIGHT (or
           re-read terminal if a concurrent commit() raced us)

Atomicity contract for commit() (spec/45 MUST 5):
    Uses atomic_write() (temp + fsync + rename) for the terminal marker.
    A crash mid-write leaves a .tmp file, not a corrupt terminal marker.
    After atomic_write succeeds, the lease file is unlinked (best-effort).

MARKER-ONLY terminal entries (maintainer ruling, spec/45 MUST 5):
    Terminal entries store ONLY key + prior_run_id + result_ref + terminal flag.
    No result content is stored at any scale. result_ref is an opaque caller-
    supplied string (run_id, path, or URI) — the caller owns the actual bytes.

Symlink containment (spec/45 security contract):
    Per the QueueBackend reframe lesson (#428 round 6): point-check whack-a-mole
    fails. There is ONE consolidated per-entry guard for ALL sinks.

    _ledger_root() resolves both agent_root and agent_root/'idempotency',
    then checks is_relative_to before trusting idempotency/ as the containment
    root (a symlinked idempotency/ DIRECTORY escape is the perimeter check).

    _require_canonical_ledger_path() is the single consolidated per-entry
    invariant: resolves BOTH paths, asserts the resolved leaf is_relative_to the
    resolved root, asserts the leaf is not a symlink. It is called at EVERY
    ledger read/write/CLAIM sink (begin's O_EXCL create, commit's terminal write,
    both reads, and export's per-leaf check). The begin() claim sink additionally
    opens with O_NOFOLLOW so a forged symlink leaf cannot be followed even in the
    window between the guard and the open.

Fail-closed vs fail-open boundary:
    - ledger directory absent (FileNotFoundError / ENOENT) → FRESH (authoritative)
    - key file absent (FileNotFoundError / ENOENT) → FRESH (authoritative)
    - LEASE file present but unreadable (non-ENOENT OSError, JSON parse error, or
      containment violation) → fail-closed: treat as IN_FLIGHT (log.error;
      safer than re-running)
    - TERMINAL file present but unreadable (non-ENOENT OSError, JSON parse error,
      or containment violation) → fail-closed: treat as COMPLETED (log.error;
      a garbled/tampered terminal is safest treated as 'do not re-run')
    - I/O error on ledger directory enumeration (non-ENOENT OSError) → raise
      IdempotencyBackendError

TTL sweep:
    supports_ttl=False in PR1. A follow-up sweep() method will expire stale
    lease files (begin() with no commit(), process crashed). Until then, stale
    leases require operator intervention (delete the *.lease.json file).

Import boundary (circular-import safety):
    Imports only from ..exceptions, .._io, .types — no imports from
    ..idempotency (the package root) or any module that imports ..idempotency
    at module level. This keeps idempotency/__init__.py importable without
    loading the LLM stack.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .._io import atomic_write
from ..exceptions import IdempotencyBackendError, PathTraversalError
from .types import (
    COMPLETED,
    FRESH,
    IN_FLIGHT,
    DedupDecision,
    IdempotencyCapabilities,
    IdempotencyExport,
)

_logger = logging.getLogger(__name__)

# Maximum allowed length (in CHARACTERS, via len()) for key and result_ref
# strings. These are character counts, not byte counts — a multibyte string at
# the limit may exceed this many UTF-8 bytes on disk. The bound exists to catch
# caller bugs (unbounded keys), not to enforce a disk-byte budget.
_MAX_KEY_LEN = 2048
_MAX_RESULT_REF_LEN = 1024


# ──────────────────────────────────────────────────────────────────
# Private validation helpers


def _validate_key(key: str) -> None:
    """Reject idempotency_key values that would escape the ledger directory.

    A valid key must be non-empty, not '.' or '..', must not contain path
    separators, and must not exceed _MAX_KEY_LEN. The key is hashed for the
    on-disk path, so it never becomes a direct path component — but an invalid
    key at the API boundary is rejected early to surface caller bugs loudly.

    Raises:
        PathTraversalError: when key is empty, '.' or '..', contains path
            separators, or contains a NUL byte / C0 control character
            (chr(0)–chr(31)). This matches _validate_bare_component in
            queue/filesystem.py for API consistency; the NUL/control rejection
            is defense-in-depth (a NUL byte can truncate a path at the syscall
            boundary, control chars are never legitimate in a key).
        IdempotencyBackendError: when key exceeds _MAX_KEY_LEN.
    """
    if not key or key in (".", "..") or key != Path(key).name:
        raise PathTraversalError(
            "idempotency_key must be a bare string (no path separators, not empty, "
            "not '.' or '..')",
            child=key,
            root="<idempotency_key validation>",
        )
    if any(ord(ch) < 32 for ch in key):
        raise PathTraversalError(
            "idempotency_key must not contain NUL bytes or control characters "
            "(chr(0)–chr(31))",
            child=key,
            root="<idempotency_key validation>",
        )
    if len(key) > _MAX_KEY_LEN:
        raise IdempotencyBackendError(
            f"idempotency_key exceeds maximum length {_MAX_KEY_LEN}: {len(key)}"
        )


def _validate_result_ref(result_ref: str) -> None:
    """Reject result_ref values that exceed the maximum length.

    result_ref is an opaque reference string (run_id, path, URI). We store it
    verbatim as a JSON VALUE in the terminal marker — it is NEVER used as a path
    component on disk (the on-disk filename is derived from sha256(key), not
    result_ref). Path separators ('/', '\\') are therefore PERMITTED: a URI
    (s3://bucket/key) or a path (runs/2026/abc.json) is the single most natural
    "result reference" and the documented intent ("run_id, path, URI"). Only the
    length bound is enforced — to catch caller bugs (unbounded refs), not to
    enforce a disk-byte budget.

    Raises:
        IdempotencyBackendError: when result_ref exceeds _MAX_RESULT_REF_LEN
            characters (character count via len(), not a UTF-8 byte count).
    """
    if len(result_ref) > _MAX_RESULT_REF_LEN:
        raise IdempotencyBackendError(
            f"result_ref exceeds maximum length {_MAX_RESULT_REF_LEN}: "
            f"{len(result_ref)}"
        )


def _key_hash(key: str) -> str:
    """Return the on-disk filename component for key.

    This is the FULL sha256 hex digest (64 chars, untruncated — no slicing).
    The original key is stored inside the JSON entry for round-trip verification
    so that a hash collision (extremely unlikely with a full 256-bit digest) is
    detected on read.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────
# FilesystemDedupLedger


class FilesystemDedupLedger:
    """Filesystem reference impl for IdempotencyBackend Protocol (spec/45).

    Scoped to one agent root — agent_root/idempotency/. This is an agent-scoped
    backend matching GoalBackend/JournalBackend.

    Cross-agent dedup requires a shared backend (Redis, Postgres, or a
    project-root-scoped FilesystemDedupLedger instantiated at the project root).
    See spec/45 §"Scope" for the follow-up issue reference.

    FilesystemDedupLedger is single-host-only: O_EXCL atomicity does not extend
    across hosts. Operators running multi-host deployments should use a Redis or
    Postgres IdempotencyBackend for cross-host atomicity.

    TTL sweep: supports_ttl=False in PR1. A follow-up sweep() method will
    expire stale lease files (begin() with no commit(), process crashed). Until
    then, stale leases require operator intervention (delete *.lease.json).

    Construction is side-effect-free (no filesystem I/O in __init__).

    Args:
        agent_root: the agent-level root directory. The ledger lives at
            agent_root/idempotency/. Paths containing a literal '..'
            component are rejected with ValueError.
    """

    def __init__(self, agent_root: Path) -> None:
        """Construct a FilesystemDedupLedger for agent_root.

        Side-effect-free: no filesystem I/O during construction.

        Args:
            agent_root: the agent's root directory. The ledger lives at
                agent_root/idempotency/. Paths containing '..' components
                are rejected with ValueError.

        Raises:
            ValueError: when agent_root contains '..' path components.
        """
        raw = Path(agent_root)
        for part in raw.parts:
            if part == "..":
                raise ValueError(
                    f"FilesystemDedupLedger: agent_root contains '..' component: "
                    f"{agent_root!r}"
                )
        self._agent_root = raw

    @property
    def backend_id(self) -> str:
        """Stable backend identifier."""
        return "filesystem"

    # ──────────────────────────────────────────────────────────────
    # Symlink containment guards

    def _ledger_root(self) -> Path:
        """Return the UNRESOLVED idempotency/ dir after a resolved containment check.

        Resolves both agent_root and agent_root/'idempotency' PURELY to run the
        is_relative_to containment check (a symlinked idempotency/ pointing outside
        agent_root is refused). On success returns the UNRESOLVED
        self._agent_root / 'idempotency' path — NOT the resolved path — so that
        caller-visible paths stay in the caller's own path representation.

        Mirrors FilesystemQueueBackend._queue_root() and
        FilesystemJournalBackend._journal_dir() exactly.

        Returns:
            The UNRESOLVED agent_root/idempotency/ path.

        Raises:
            PathTraversalError: when idempotency/ resolves outside agent_root
                (symlinked ancestor escape), OR when either path cannot be
                resolved (symlink loop / inaccessible ancestor).
        """
        try:
            agent_root_resolved = self._agent_root.resolve()
            ledger_resolved = (self._agent_root / "idempotency").resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "idempotency/ path could not be resolved "
                "(symlink loop or inaccessible ancestor)",
                child="idempotency",
                root=str(self._agent_root),
            ) from exc
        if not ledger_resolved.is_relative_to(agent_root_resolved):
            raise PathTraversalError(
                "idempotency/ resolves outside agent_root (symlinked ancestor refused)",
                child="idempotency",
                root=str(agent_root_resolved),
            )
        return self._agent_root / "idempotency"

    @staticmethod
    def _require_canonical_ledger_path(
        ledger_file_path: Path,
        ledger_root: Path,
    ) -> None:
        """Single consolidated containment invariant for all ledger read/write/claim sinks.

        This is the ONE guard that prevents whack-a-mole (QueueBackend lesson,
        #428 round 6 reframe). It subsumes:
            (a) regular-file invariant (no symlink leaf)
            (b) resolved path is strictly under ledger_root.resolve()
            (c) symlinked-parent rejection (via canonical equality)

        Call this at EVERY ledger write/read/claim sink BEFORE touching the path.
        Write operations raise on violation (caller must know the claim failed,
        not silently succeed). Read operations (lookup) return FRESH on violation
        (read-side: empty/unavailable is authoritative FRESH).

        Raises:
            PathTraversalError: on any containment violation.
        """
        try:
            lr = ledger_root.resolve()
            fp = ledger_file_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "ledger entry path could not be resolved",
                child=str(ledger_file_path),
                root=str(ledger_root),
            ) from exc
        if not fp.is_relative_to(lr):
            raise PathTraversalError(
                "ledger entry path escapes idempotency/ root",
                child=str(ledger_file_path),
                root=str(ledger_root),
            )
        if ledger_file_path.is_symlink():
            raise PathTraversalError(
                "ledger entry leaf is a symlink",
                child=str(ledger_file_path),
                root=str(ledger_root),
            )

    # ──────────────────────────────────────────────────────────────
    # Internal ledger helpers

    def _lease_path(self, ledger_root: Path, key: str) -> Path:
        """Return the in-flight lease file path for key."""
        return ledger_root / f"{_key_hash(key)}.lease.json"

    def _terminal_path(self, ledger_root: Path, key: str) -> Path:
        """Return the terminal marker file path for key."""
        return ledger_root / f"{_key_hash(key)}.terminal.json"

    def _read_terminal(
        self, terminal_path: Path, ledger_root: Path, key: str
    ) -> DedupDecision | None:
        """Read and validate a terminal marker file.

        Returns:
            DedupDecision(state='completed') if terminal marker is valid and
            matches key. None if the file does not exist (ENOENT). If the file
            exists but is corrupt/unreadable → treat as COMPLETED (fail-closed:
            a garbled terminal entry is safer to treat as 'do not re-run').
        """
        # is_symlink() is a NO-FOLLOW lstat — it MUST be checked BEFORE the
        # exists() gate. exists() FOLLOWS the symlink, so a DANGLING symlink leaf
        # (target removed) would otherwise return False here, return None, and
        # leak to FRESH — re-running a key whose terminal marker was tampered
        # with (replaced by a dangling symlink). Routing the symlink leaf into
        # the containment branch first makes a tampered terminal fail-closed to
        # COMPLETED ("do not re-run"), per the spec/45 boundary table.
        if not terminal_path.is_symlink() and not terminal_path.exists():
            return None
        # Containment check on read: fail-closed if containment violated.
        try:
            self._require_canonical_ledger_path(terminal_path, ledger_root)
        except PathTraversalError:
            _logger.error(
                "idempotency ledger terminal path containment violation — "
                "treating as COMPLETED to fail-closed: %s",
                terminal_path,
            )
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id=None,
                prior_result_ref=None,
            )
        try:
            data = json.loads(terminal_path.read_text(encoding="utf-8"))
            # Hash-collision guard: verify stored key matches the caller's key.
            stored_key = data.get("key", "")
            if stored_key != key:
                _logger.warning(
                    "idempotency ledger hash collision detected "
                    "(stored_key=%r, caller_key=%r) — treating as FRESH",
                    stored_key,
                    key,
                )
                return None
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id=data.get("prior_run_id"),
                prior_result_ref=data.get("result_ref"),
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            _logger.error(
                "idempotency ledger terminal entry unreadable — "
                "treating as COMPLETED to fail-closed: %s",
                terminal_path,
            )
            return DedupDecision(
                is_duplicate=True,
                state=COMPLETED,
                prior_run_id=None,
                prior_result_ref=None,
            )

    def _read_lease(
        self, lease_path: Path, ledger_root: Path, key: str
    ) -> DedupDecision | None:
        """Read and validate an in-flight lease file.

        Returns:
            DedupDecision(state='in_flight') if lease file is valid and matches key.
            None if the file does not exist (ENOENT). If the file exists but is
            corrupt/unreadable → treat as IN_FLIGHT (fail-closed).
        """
        # is_symlink() (NO-FOLLOW lstat) MUST precede the exists() gate — a
        # DANGLING symlink lease leaf would otherwise return False from exists()
        # (which FOLLOWS the link) and leak to FRESH. A tampered lease leaf
        # (symlink, dangling or not) fails-closed to IN_FLIGHT via the
        # containment branch below. See _read_terminal for the full rationale.
        if not lease_path.is_symlink() and not lease_path.exists():
            return None
        # Containment check on read.
        try:
            self._require_canonical_ledger_path(lease_path, ledger_root)
        except PathTraversalError:
            _logger.error(
                "idempotency ledger lease path containment violation — "
                "treating as IN_FLIGHT to fail-closed: %s",
                lease_path,
            )
            return DedupDecision(
                is_duplicate=True,
                state=IN_FLIGHT,
                prior_run_id=None,
                prior_result_ref=None,
            )
        try:
            data = json.loads(lease_path.read_text(encoding="utf-8"))
            stored_key = data.get("key", "")
            if stored_key != key:
                _logger.warning(
                    "idempotency ledger lease hash collision detected "
                    "(stored_key=%r, caller_key=%r) — treating as FRESH",
                    stored_key,
                    key,
                )
                return None
            return DedupDecision(
                is_duplicate=True,
                state=IN_FLIGHT,
                prior_run_id=data.get("run_id"),
                prior_result_ref=None,
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            _logger.error(
                "idempotency ledger lease entry unreadable — "
                "treating as IN_FLIGHT to fail-closed: %s",
                lease_path,
            )
            return DedupDecision(
                is_duplicate=True,
                state=IN_FLIGHT,
                prior_run_id=None,
                prior_result_ref=None,
            )

    # ──────────────────────────────────────────────────────────────
    # Protocol methods

    def begin(self, key: str, run_id: str) -> DedupDecision:
        """Atomic check-reserve-or-report for an idempotency key.

        Atomicity via O_EXCL: the POSIX open(O_WRONLY|O_CREAT|O_EXCL) is the
        single atomic check-and-create. First caller wins FRESH; concurrent
        callers get FileExistsError → IN_FLIGHT.

        Sequence (per spec/45 MUST 4):
            1. Validate key (reject traversal attempts early).
            2. Get ledger_root (containment check for idempotency/ dir).
            3. Check terminal marker (COMPLETED fast path — read-only, no O_EXCL).
            4. Attempt O_EXCL create of lease file → FRESH on success.
            5. On FileExistsError → read existing lease/terminal → IN_FLIGHT or COMPLETED.

        Empty/absent ledger is AUTHORITATIVE FRESH (Lesson 8/9). Only non-ENOENT
        OSError on directory operations raises IdempotencyBackendError.
        """
        _validate_key(key)

        try:
            ledger_root = self._ledger_root()
        except PathTraversalError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger root containment violation: {exc}"
            ) from exc

        # Containment check: get the expected file paths (we compute the hash,
        # so no traversal risk from key itself — but we still guard the root).
        terminal_path = self._terminal_path(ledger_root, key)
        lease_path = self._lease_path(ledger_root, key)

        # Step 3: Check terminal marker FIRST so an already-committed key returns
        # COMPLETED without attempting the O_EXCL lease create. (This is still a
        # filesystem read — exists() + read_text() — not an I/O-free path.)
        terminal_result = self._read_terminal(terminal_path, ledger_root, key)
        if terminal_result is not None:
            return terminal_result

        # Step 4: Attempt O_EXCL create of lease file (POSIX atomic check-reserve).
        # Create ledger_root directory if absent (first use for this agent).
        try:
            ledger_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger directory could not be created: {exc}"
            ) from exc

        lease_content = json.dumps(
            {"key": key, "run_id": run_id, "state": IN_FLIGHT}
        ).encode("utf-8")

        # MUST 10: guard the CLAIM sink with the SAME consolidated containment
        # invariant as every read/write sink — do NOT rely on O_EXCL's incidental
        # symlink behavior (a dangling/forged symlink at lease_path would otherwise
        # make O_EXCL raise FileExistsError and strand the key permanently). The
        # check is inside the try so a PathTraversalError maps to
        # IdempotencyBackendError consistently with the other sinks.
        try:
            self._require_canonical_ledger_path(lease_path, ledger_root)
        except PathTraversalError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger lease path containment violation: {exc}"
            ) from exc

        try:
            fd = os.open(
                str(lease_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, lease_content)
                # No fsync on the lease (intentional, asymmetric with commit()'s
                # atomic_write): the lease is non-durable by design. A crash that
                # loses an uncommitted lease re-exposes FRESH on the next begin(),
                # which is SAFE for a run that never completed. Only commit()'s
                # terminal marker is durability-critical. Do NOT copy this pattern
                # into commit().
            finally:
                os.close(fd)
            # We won the O_EXCL race. Before claiming FRESH, re-check the terminal
            # marker (MUST 4 at-most-once): a commit() that interleaved between our
            # Step-3 terminal read and this O_EXCL create writes the terminal AND
            # unlinks the lease — which is exactly why our O_EXCL create succeeded.
            # Without this re-check we would re-run an already-COMPLETED key.
            terminal_after = self._read_terminal(terminal_path, ledger_root, key)
            if terminal_after is not None:
                # A concurrent commit() raced in. Clean up the spurious lease we
                # just created and report COMPLETED.
                try:
                    if lease_path.is_symlink() is False:
                        lease_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return terminal_after
            # We own FRESH.
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )
        except FileExistsError:
            # Another caller won the O_EXCL race (or a stale lease/forged leaf is
            # present). Determine IN_FLIGHT vs COMPLETED.
            # A concurrent commit() may have raced us — check terminal again.
            terminal_result = self._read_terminal(terminal_path, ledger_root, key)
            if terminal_result is not None:
                return terminal_result
            # Still in-flight (or corrupt — _read_lease handles fail-closed).
            lease_result = self._read_lease(lease_path, ledger_root, key)
            if lease_result is not None:
                return lease_result
            # The lease file existed (FileExistsError) but neither terminal nor
            # lease is now readable. This is a benign transient: a concurrent
            # commit() unlinked the lease after writing the terminal, then the
            # terminal also vanished (only a TTL sweep could do this, and
            # supports_ttl=False in PR1, so this branch is currently unreachable
            # in normal operation). Re-attempting the O_EXCL claim is correct and
            # resolves to FRESH or a freshly re-created lease. One bounded retry
            # (no recursion) avoids surfacing a recoverable race as an exception
            # to a caller that branches on DedupDecision.state (MUST 1).
            return self._begin_after_vanished(
                terminal_path, lease_path, ledger_root, key, lease_content
            )
        except OSError as exc:
            # Genuine I/O error (ENOSPC, EACCES, etc.) — surface as a backend
            # error. NOTE: a symlink leaf does NOT reach here: under O_EXCL the
            # open raises EEXIST/FileExistsError (POSIX checks existence before
            # dereferencing the link), handled by the FileExistsError branch
            # above. The pre-create _require_canonical_ledger_path guard is the
            # primary symlink defense; O_NOFOLLOW is defense-in-depth.
            raise IdempotencyBackendError(
                f"idempotency ledger begin() I/O error for key={key!r}: {exc}"
            ) from exc

    def _begin_after_vanished(
        self,
        terminal_path: Path,
        lease_path: Path,
        ledger_root: Path,
        key: str,
        lease_content: bytes,
    ) -> DedupDecision:
        """One bounded retry of the O_EXCL claim after a benign double-vanish race.

        Called when begin() got FileExistsError but both the terminal and lease
        re-reads returned None. Re-attempts the O_EXCL create exactly once. On a
        second FileExistsError we re-read once more (the file came back) and
        return its state; if still nothing, we raise — at that point the disk is
        behaving adversarially and a value-object answer would be a lie.
        """
        try:
            self._require_canonical_ledger_path(lease_path, ledger_root)
            fd = os.open(
                str(lease_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, lease_content)
            finally:
                os.close(fd)
            terminal_after = self._read_terminal(terminal_path, ledger_root, key)
            if terminal_after is not None:
                try:
                    if lease_path.is_symlink() is False:
                        lease_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return terminal_after
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )
        except FileExistsError:
            terminal_result = self._read_terminal(terminal_path, ledger_root, key)
            if terminal_result is not None:
                return terminal_result
            lease_result = self._read_lease(lease_path, ledger_root, key)
            if lease_result is not None:
                return lease_result
            raise IdempotencyBackendError(
                f"idempotency ledger race: key={key!r} lease file vanished "
                "repeatedly under begin() retry — disk may be unstable"
            )
        except PathTraversalError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger lease path containment violation: {exc}"
            ) from exc
        except OSError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger begin() I/O error for key={key!r}: {exc}"
            ) from exc

    def commit(self, key: str, result_ref: str) -> None:
        """Mark a previously-claimed key as permanently COMPLETED.

        Writes a MARKER-ONLY terminal entry via atomic_write (temp+fsync+rename).
        Does NOT store result content — result_ref is an opaque reference string.

        prior_run_id is recovered from the in-flight lease (via _read_lease) so the
        terminal marker preserves the audit link back to the originating run
        (Principle 5).

        First-commit-wins (Principle 5 audit-link preservation): if a terminal
        already exists for this key — a redelivered/retried commit() — this call is
        a no-op that preserves the original terminal (including prior_run_id). A
        second commit cannot sever the audit link by resolving prior_run_id=None
        (the first commit already unlinked the lease) and overwriting the marker.

        After atomic_write succeeds, unlinks the lease file (best-effort).
        """
        _validate_key(key)
        _validate_result_ref(result_ref)

        try:
            ledger_root = self._ledger_root()
        except PathTraversalError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger root containment violation: {exc}"
            ) from exc

        terminal_path = self._terminal_path(ledger_root, key)
        lease_path = self._lease_path(ledger_root, key)

        # Read current lease to get the original run_id (for the terminal record).
        # Route through _read_lease so a dangling/tampered symlinked lease leaf is
        # OBSERVED (is_symlink()-before-exists() ordering) instead of skipped by a
        # symlink-following exists() pre-gate — otherwise a tampered lease would
        # silently drop prior_run_id, severing the audit link to the originating
        # run (Principle 5). _read_lease returns None only for a genuinely-absent
        # lease; a fail-closed IN_FLIGHT decision still carries prior_run_id=None,
        # which is the correct (and unavoidable) value when the lease is unreadable.
        prior_run_id: str | None = None
        lease_decision = self._read_lease(lease_path, ledger_root, key)
        if lease_decision is not None:
            prior_run_id = lease_decision.prior_run_id

        # Write terminal marker atomically.
        # Containment check on destination before write.
        try:
            self._require_canonical_ledger_path(terminal_path, ledger_root)
        except PathTraversalError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger terminal path containment violation: {exc}"
            ) from exc

        # Re-entrancy (Principle 5 audit-link preservation): if a terminal already
        # exists for this key — a redelivered/retried commit(), the exact
        # at-least-once shape PR2 must tame — commit() is a no-op (first commit
        # wins). Overwriting would clobber the original prior_run_id: the first
        # commit unlinks the lease, so a second call resolves prior_run_id=None and
        # would sever the audit link to the originating run.
        if self._read_terminal(terminal_path, ledger_root, key) is not None:
            try:
                if lease_path.exists() and not lease_path.is_symlink():
                    lease_path.unlink(missing_ok=True)
            except OSError:
                pass
            return

        terminal_data = json.dumps(
            {
                "key": key,
                "prior_run_id": prior_run_id,
                "result_ref": result_ref,
                "terminal": True,
            },
            sort_keys=True,
        )
        try:
            atomic_write(terminal_path, terminal_data, encoding="utf-8")
        except OSError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger commit() I/O error for key={key!r}: {exc}"
            ) from exc

        # Unlink lease (best-effort — a missing lease is harmless after terminal write).
        try:
            if lease_path.exists() and not lease_path.is_symlink():
                lease_path.unlink(missing_ok=True)
        except OSError:
            pass

    def release_lease(self, key: str) -> None:
        """Best-effort release of an IN_FLIGHT lease (spec/45 MUST 13).

        Unlinks the ``<key_hash>.lease.json`` file, removing the in-flight
        claim for ``key``. Called by ``agent.call()`` on error/exception via
        a try/finally so that a crash never permanently wedges a key IN_FLIGHT
        (in the absence of TTL sweep, which is ``supports_ttl=False`` in PR1).

        Idempotent — no error raised when the lease file does not exist
        (already committed, or never created). Raises ``IdempotencyBackendError``
        on genuine I/O failure (EACCES, ENOSPC, etc.) — never on ENOENT — and
        also when ``key`` exceeds ``_MAX_KEY_LEN`` (raised by ``_validate_key``
        before any I/O).

        Key validation is run before any I/O so caller bugs are surfaced loudly:
        separator/empty/``.``/``..``/NUL/C0-control keys raise
        ``PathTraversalError``; over-length keys raise ``IdempotencyBackendError``.
        The ledger root containment check is run to refuse a symlinked
        idempotency/ escape. If the containment check fails, the method returns
        without raising (best-effort — no lease file can safely be unlinked in
        that state).

        Raises:
            PathTraversalError: when ``key`` contains path separators, is empty,
                ``.``/``..``, or contains NUL/C0 control chars.
            IdempotencyBackendError: when ``key`` exceeds ``_MAX_KEY_LEN``
                (raised by ``_validate_key`` before any I/O), OR on genuine I/O
                failure (EACCES, ENOSPC) — never on ENOENT.
        """
        _validate_key(key)

        try:
            ledger_root = self._ledger_root()
        except PathTraversalError:
            # Ledger root containment failure — best-effort, do not raise.
            _logger.error(
                "idempotency release_lease: ledger root containment violation "
                "— skipping lease unlink for key=%r",
                key,
            )
            return

        lease_path = self._lease_path(ledger_root, key)

        # Guard the unlink sink — same pattern as commit()'s lease unlink.
        # Only unlink a non-symlink lease leaf (symlinked lease = fail-closed
        # in begin/lookup; we cannot trust its content so don't touch it).
        try:
            if lease_path.is_symlink():
                # Tampered lease leaf — don't unlink (could unlink a real file
                # at the symlink target). Log and return.
                _logger.error(
                    "idempotency release_lease: lease path is a symlink — "
                    "refusing unlink: %s",
                    lease_path,
                )
                return
            lease_path.unlink(missing_ok=True)
        except OSError as exc:
            raise IdempotencyBackendError(
                f"idempotency ledger release_lease() I/O error for key={key!r}: {exc}"
            ) from exc

    def lookup(self, key: str) -> DedupDecision:
        """Read-only state query for an idempotency key (no side effects).

        Returns FRESH when the key is unknown (ledger absent or key file absent) —
        an absent entry is authoritative FRESH (Lesson 9, no fallback scan).

        Fail-direction on a TAMPERED entry (two distinct cases — do not conflate):

        * Symlinked idempotency/ DIRECTORY escaping agent_root (``_ledger_root()``
          raises): the whole ledger is untrustworthy, so we cannot distinguish
          "key absent" from "key present". Returns FRESH (read-side fail-soft) —
          the directory escape is reported by the doctor check, and treating an
          unreadable ledger as empty matches "empty is authoritative".

        * Symlinked or unreadable terminal/lease LEAF that passes the file-exists
          check (``_read_terminal``/``_read_lease`` hit their containment or
          parse branch): a tampered/garbled entry for a SPECIFIC key is treated
          as a duplicate (COMPLETED for a terminal leaf, IN_FLIGHT for a lease
          leaf), NOT FRESH. Re-running a key whose marker was tampered with is the
          unsafe direction; "do not re-run" is the safe one. This is the same
          fail-closed posture as the corrupt-JSON rows in the spec/45 boundary
          table and is the maintainer ruling for marker-only terminal entries.
        """
        _validate_key(key)

        try:
            ledger_root = self._ledger_root()
        except PathTraversalError:
            # Symlinked idempotency/ escaping agent_root: fail-soft on reads.
            _logger.error(
                "idempotency ledger root containment violation on lookup — returning FRESH"
            )
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        terminal_path = self._terminal_path(ledger_root, key)
        lease_path = self._lease_path(ledger_root, key)

        # Check terminal first. Delegate the present/absent decision to
        # _read_terminal (which is_symlink()-checks before exists() so a dangling
        # symlink leaf fails-closed, not FRESH). Do NOT pre-gate on exists() here
        # — exists() follows symlinks and would re-introduce the dangling-leaf
        # leak. None means genuinely absent (or hash-collision) → fall through.
        terminal_result = self._read_terminal(terminal_path, ledger_root, key)
        if terminal_result is not None:
            return terminal_result

        # Check lease (same no-pre-gate rationale as terminal above).
        lease_result = self._read_lease(lease_path, ledger_root, key)
        if lease_result is not None:
            return lease_result

        # Neither file exists — FRESH (authoritative empty-is-FRESH).
        return DedupDecision(
            is_duplicate=False,
            state=FRESH,
            prior_run_id=None,
            prior_result_ref=None,
        )

    def export(self, query: Any = None) -> IdempotencyExport:
        """Export TERMINAL ledger entries only (spec/40 + spec/45).

        Whitelist: enumerate ONLY *.terminal.json files.
        Structurally excludes *.lease.json (in-flight) — not filtered.

        Per-leaf containment: each terminal file is checked via the ONE
        consolidated guard (_require_canonical_ledger_path) before its bytes are
        read — same invariant as every read/write/claim sink (spec/45: "exactly
        one per-entry containment helper; do not add a second"). Read-side
        semantics: a containment violation SKIPS the entry rather than raising.
        """
        try:
            ledger_root = self._ledger_root()
        except PathTraversalError:
            return IdempotencyExport(
                entries_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        if not ledger_root.is_dir():
            return IdempotencyExport(
                entries_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        entries_with_bytes: list[tuple[str, bytes]] = []

        for file_path in sorted(ledger_root.iterdir()):
            # Whitelist: only terminal marker files.
            if not file_path.name.endswith(".terminal.json"):
                continue
            if not file_path.is_file():
                continue
            # Per-leaf containment via the ONE consolidated guard (Lesson 6 /
            # spec/45 "exactly one per-entry containment helper; do not add a
            # second"). Read-side semantics: a containment violation (symlink
            # leaf, escape, unresolvable path) SKIPS the entry rather than
            # raising — an untrustworthy leaf is simply not exported.
            try:
                self._require_canonical_ledger_path(file_path, ledger_root)
            except PathTraversalError:
                continue
            try:
                raw_bytes = file_path.read_bytes()
                rel = file_path.relative_to(self._agent_root)
                entries_with_bytes.append((str(rel), raw_bytes))
            except (OSError, ValueError):
                pass

        return IdempotencyExport(
            entries_with_bytes=entries_with_bytes,
            backend_id=self.backend_id,
            scope=str(self._agent_root),
        )

    def export_all(self) -> IdempotencyExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        return self.export(None)

    def capabilities(self) -> IdempotencyCapabilities:
        """Backend capability declaration for FilesystemDedupLedger.

        single_host_only=True: O_EXCL atomicity does not extend across hosts.
        atomic_claim=True: begin() uses O_EXCL (single atomic check-reserve).
        supports_ttl=False: TTL sweep is a follow-up PR.
        supports_canonical_export=True: export() is implemented.
        """
        return IdempotencyCapabilities(
            backend_id=self.backend_id,
            single_host_only=True,
            atomic_claim=True,
            supports_ttl=False,
            supports_canonical_export=True,
        )

"""FilesystemQueueBackend — directory-tree reference implementation (spec/44).

This is the default backend for single-host deployments. It carves the
cascade work-queue cluster from atomic_agents/_cascade.py into the
QueueBackend Protocol package as a behavior-preserving, scaffolding-only
carve (zero runtime caller changes, per arc ruling 428-pr1-args.json
adopt-now-vs-scaffolding-only). Runtime semantics, on-disk directory layout,
and atomicity guarantees are unchanged, with two disclosed intentional
deviations:
  1. An additive ``role`` key in the .lease.json sidecar (see _write_sidecar)
     to support list_claimed(role=...) filtering — additive and ignored by
     legacy readers, so existing sidecars and callers are unaffected.
  2. claim_next() rmdir's an empty claimed/<lease_token>/ directory after a
     no-candidate claim (the pre-carve _cascade.py left it behind). Functionally
     harmless — list_claimed skips empty dirs and recover_stale_claims rmdir's
     them anyway — but it is a behavior change, disclosed here so the
     "directory layout unchanged" claim stays honest.

Directory layout (under project_root/queue/):
    queued/<role>/           — pending work items (FIFO by sorted name)
    claimed/<lease_token>/   — in-flight items (one dir per claim session)
    done/<lease_token>/      — completed items
    dead-letter/<lease_token>/  — permanently failed items
    queued/_recovered/<lease_token>/  — stale items recovered from claimed/

State vocabulary mapping (spec/06 conceptual → on-disk):
    spec/06 'pending'     → queue/queued/<role>/
    spec/06 'in_progress' → queue/claimed/<lease_token>/
    spec/06 'completed'   → queue/done/<lease_token>/
    spec/06 'dead_letter' → queue/dead-letter/<lease_token>/

Sidecar files:
    <work_file>.lease.json  — lease metadata (in claimed/ only, ephemeral)
    <work_file>.reason.txt  — failure reason (in dead-letter/ only)

Atomicity contract for claim_next() (spec/44 MUST 4, see MUST 9 for the rename
primitive):
    The PRIMARY guarantee is the POSIX rename that moves the work file
    from queued/<role>/ to claimed/<lease_token>/. Under concurrent callers,
    only one rename succeeds for any given file; the loser gets
    FileNotFoundError and tries the next candidate.
    The sidecar write (.lease.json) is BEST-EFFORT: if the process crashes
    between rename and sidecar write, the work file is in claimed/ but has
    no sidecar. recover_stale_claims() handles this via mtime fallback.
    POSIX rename is the atomicity boundary; the sidecar follows.

Atomicity contract for release() and move_to_dead_letter() (spec/44 MUST 4 /
MUST 10):
    The PRIMARY guarantee is the POSIX rename that moves the work file to
    done/ or dead-letter/. The sidecar cleanup (.lease.json unlink from the
    OLD claimed/ location) is best-effort. An orphaned sidecar in done/ or
    dead-letter/ is harmless — no recovery code looks there. The work file's
    location determines the item's state.

    Sidecar location after rename: the sidecar lives at
    claimed/<lease_token>/<original_name>.lease.json even AFTER the work
    file is renamed to done/ or dead-letter/. We compute the sidecar path
    from first principles (self._queue_root / 'claimed' / lease_token /
    (original_name + '.lease.json')) so we never hold a stale path handle
    to the post-rename location of the work file.

Symlink containment (spec/44 security contract):
    _queue_root() resolves both project_root and project_root/'queue',
    then checks is_relative_to before trusting queue/ as the containment
    root. When a symlinked queue/ (or symlinked subdirectory) points outside
    project_root, the containment guard raises PathTraversalError internally;
    every operation catches it and fails SOFT — writes (claim_next, release,
    move_to_dead_letter, renew_lease) return None / no-op, and reads
    (list_claimed, recover, export) skip / return []. This is the deliberate,
    carve-consistent choice: the pre-carve _cascade.py had no containment check
    at all, so no operation ever propagated an exception to its caller. Mirrors
    FilesystemJournalBackend._journal_dir() and FilesystemOutcomeBackend._runs_root()
    in resolving-then-checking; it differs from them in failing soft on writes
    rather than raising, to preserve the pre-carve no-exception contract.

Export contract (spec/40 + spec/44):
    export() enumerates ONLY the three durable directories by name:
        queued/, done/, dead-letter/
    It does NOT enumerate queue/ as a whole. The claimed/ directory is
    structurally excluded (whitelist, not filter). .lease.json sidecars
    are also excluded by the whitelist (they only live in claimed/).

Import boundary (circular-import safety):
    - Imports only from ..exceptions, .._io, .types — no imports from
      ..queue (the package root) or any module that imports ..queue at
      module level. This keeps queue/__init__.py importable without
      loading the LLM stack.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..exceptions import PathTraversalError
from .types import QueueCapabilities, QueueItem, QueueExport


# ──────────────────────────────────────────────────────────────────
# Filesystem-specific QueueItem with path


@dataclass
class FilesystemQueueItem(QueueItem):
    """QueueItem with a filesystem path field added.

    FilesystemQueueBackend returns this subtype from claim_next() so callers
    that need item.path (cron scripts via spec/06, the _cascade.py shim,
    test_cascade.py) continue to work unchanged.

    The abstract Protocol-level QueueItem in types.py has NO path field —
    that is the load-bearing portability property. FilesystemQueueItem adds
    path: Path for filesystem-only callers.

    The _cascade.py shim re-exports FilesystemQueueItem and aliases it as
    QueueItem for backward compatibility with existing callers.

    Fields:
        path: the on-disk path to the claimed work file. Lives at
            project_root/queue/claimed/<lease_token>/<original_name>.
            Defaults to None ONLY to keep the dataclass inheritance chain clean
            (the base QueueItem has required fields with no defaults, so a
            subclass field added after them needs a default). Every real
            construction site — claim_next(), list_claimed(), recovery — always
            passes path. There is intentionally NO validation hook here: a
            None default with a runtime guard would reject the inheritance
            default itself; callers are trusted to supply path.
    """

    path: Path = None  # type: ignore[assignment]  # always set by the backend


# ──────────────────────────────────────────────────────────────────
# Private helpers (filesystem implementation details)


def _sidecar_path(work_file: Path) -> Path:
    """Return the path of the lease sidecar for a given work file."""
    return work_file.parent / (work_file.name + ".lease.json")


def _validate_bare_component(value: str, label: str) -> None:
    """Reject a caller-supplied path component that is not a bare filename.

    A valid bare component must be a single path component with no separators,
    not empty, and not the reserved names '.' or '..'.  Any of those conditions
    would allow a traversing value (e.g. 'a/b' or '../../evil') to compose into
    a path that escapes its containing directory when concatenated with a base dir.

    Used for original_name, role, and lease_token — all caller-supplied values
    that are appended to directory paths at the sink layer.

    Args:
        value: the caller-supplied string to validate.
        label: a human-readable field label for the error message (e.g.
            "original_name", "role", "lease_token").

    Raises:
        PathTraversalError: when value contains path separators, is empty,
            or is '.' or '..'.
    """
    if not value or value in (".", "..") or value != Path(value).name:
        raise PathTraversalError(
            f"{label} must be a bare filename (no path separators, not empty, "
            f"not '.' or '..')",
            child=value,
            root=f"<{label} validation>",
        )


def _validate_original_name(original_name: str) -> None:
    """Reject original_name values that contain path separators or are reserved names.

    Delegates to _validate_bare_component.  Kept as a named alias so
    existing call sites remain readable and test names stay descriptive.

    Raises:
        PathTraversalError: when original_name contains path separators, is empty,
            or is '.' or '..'.
    """
    _validate_bare_component(original_name, "original_name")


def _write_sidecar(
    work_file: Path,
    lease_token: str,
    lease_seconds: int,
    role: str = "",
) -> None:
    """Write a lease sidecar alongside *work_file*.

    NOTE: This uses raw Path.write_text() — NOT atomic_write(). This preserves
    _cascade.py's write mechanism (best-effort sidecar; the claim atomicity
    guarantee is on the rename, not the sidecar). A torn sidecar is recoverable
    via the mtime fallback path in recover_stale_claims(). The mtime fallback
    reads the work file's mtime (not the sidecar's mtime), so atomic_write
    for sidecars would be safe — but raw write_text matches the carved-from
    behavior.

    The sidecar gains an additive ``role`` key (absent in the pre-carve
    _cascade.py sidecar) so list_claimed(role=...) filtering works on the
    claimed/ side. It is additive and ignored by legacy readers — the only
    intentional on-disk shape change in this otherwise behavior-preserving
    carve. The sidecar is therefore NOT byte-identical to the pre-carve sidecar,
    but every other field and the rename atomicity contract are unchanged.

    Args:
        work_file: path to the work file (in claimed/).
        lease_token: the lease token for this claim.
        lease_seconds: how many seconds the lease is valid for.
        role: the queue role (e.g. 'writer'). Written to sidecar so
            list_claimed(role=...) filtering works correctly.
    """
    now = datetime.now(tz=timezone.utc)
    expires_at = datetime.fromtimestamp(
        now.timestamp() + lease_seconds, tz=timezone.utc
    )
    sidecar = {
        "lease_token": lease_token,
        "claimed_at": now.isoformat(),
        "lease_expires_at": expires_at.isoformat(),
        "lease_seconds": lease_seconds,
        "role": role,
    }
    _sidecar_path(work_file).write_text(json.dumps(sidecar), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# FilesystemQueueBackend


class FilesystemQueueBackend:
    """Filesystem reference impl for QueueBackend Protocol (spec/44).

    Scoped to one project root — project_root/queue/ (the shared cascade
    work queue). The scope token is project_root (NOT agent_root) — this is
    the one project-scoped backend in the v1.5 wave, matching spec/06 where
    the queue is a shared project resource.

    Construction is side-effect-free (no filesystem I/O in __init__).

    The factory docstring notes: scope is project_root (NOT agent_root) —
    this is the one project-scoped backend in the v1.5 wave.

    Args:
        project_root: the project-level root directory. The queue lives at
            project_root/queue/.
    """

    def __init__(self, project_root: Path) -> None:
        """Construct a FilesystemQueueBackend for project_root.

        Side-effect-free: no filesystem I/O during construction.

        Args:
            project_root: the cascade project's root directory. The queue
                lives at project_root/queue/. Paths containing a literal '..'
                component are rejected with ValueError.

        Raises:
            ValueError: when project_root contains '..' path components.
        """
        raw = Path(project_root)
        for part in raw.parts:
            if part == "..":
                raise ValueError(
                    f"FilesystemQueueBackend: project_root contains '..' component: "
                    f"{project_root!r}"
                )
        self._project_root = raw

    @property
    def backend_id(self) -> str:
        """Stable backend identifier."""
        return "filesystem"

    # ──────────────────────────────────────────────────────────────
    # Symlink containment guard

    def _queue_root(self) -> Path:
        """Return the UNRESOLVED queue/ dir after a resolved containment check.

        Resolves both project_root and project_root/'queue' PURELY to run the
        is_relative_to containment check (a symlinked queue/ pointing outside
        project_root is refused). On success returns the UNRESOLVED
        ``self._project_root / 'queue'`` — NOT the resolved path — so that
        caller-visible ``item.path`` values stay in the caller's own path
        representation (byte-identical to the pre-carve _cascade.py behavior,
        and to the documented spec/06 cron/project-runner API). Returning the
        resolved path here would silently rewrite item.path to the real on-disk
        location whenever project_root is reached through a symlink (a symlinked
        $HOME, /tmp→/private/tmp on macOS, a bind mount), breaking
        item.path.relative_to(project_root) for external callers and leaking the
        absolute on-disk location.

        Mirrors FilesystemJournalBackend._journal_dir() exactly: resolve to
        CHECK containment, return the unresolved root for file operations.

        Returns:
            The UNRESOLVED project_root/queue/ path.

        Raises:
            PathTraversalError: when queue/ resolves outside project_root
                (symlinked ancestor escape), OR when either path cannot be
                resolved (symlink loop / inaccessible ancestor).
        """
        try:
            project_root_resolved = self._project_root.resolve()
            queue_resolved = (self._project_root / "queue").resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "queue/ path could not be resolved "
                "(symlink loop or inaccessible ancestor)",
                child="queue",
                root=str(self._project_root),
            ) from exc
        if not queue_resolved.is_relative_to(project_root_resolved):
            raise PathTraversalError(
                "queue/ resolves outside the project root (symlinked ancestor refused)",
                child="queue",
                root=str(project_root_resolved),
            )
        return self._project_root / "queue"

    @staticmethod
    def _require_canonical_source(
        work_path: Path,
        queue_root: Path,
        allowed_segment_lists: list[tuple[str, ...]],
    ) -> None:
        """Require work_path to resolve to EXACTLY queue_root/<segments> for one of
        the allowed segment lists, and to not be a symlink leaf.

        This is the single containment invariant: it subsumes under-queue_root
        containment, source-state (claimed/ vs _recovered/), basename match,
        symlink-leaf rejection, AND symlinked-PARENT rejection (a symlinked parent
        makes resolve() diverge from the expected canonical path).

        Raises PathTraversalError on any violation; callers wrap in their
        existing fail-soft try/except.
        """
        try:
            qr = queue_root.resolve()
            wr = work_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "work item path could not be resolved",
                child=str(work_path),
                root=str(queue_root),
            ) from exc
        expected = [qr.joinpath(*segs) for segs in allowed_segment_lists]
        if wr not in expected:
            raise PathTraversalError(
                "work item path is not its expected canonical location",
                child=str(work_path),
                root=str(queue_root),
            )
        if work_path.is_symlink():
            raise PathTraversalError(
                "work item leaf is a symlink",
                child=str(work_path),
                root=str(queue_root),
            )

    @staticmethod
    def _safe_under_queue(queue_root: Path, *parts: str) -> Path:
        """Resolve ``queue_root/<parts...>`` to refuse escape, return UNRESOLVED.

        ``_queue_root()`` only proves that ``queue/`` itself is contained. The
        per-operation subdirectories (queued/, claimed/, done/, dead-letter/ and
        their lease-token / role namespaces) are attacker-influenced names AND
        can themselves be symlinks pointing outside ``queue_root`` — the #426
        ``_runs_root()`` ancestor-escalation pattern. Without this guard a
        symlinked ``claimed/`` (or done/, dead-letter/, queued/) directory lets
        a claim_next()/release()/move_to_dead_letter() rename land OUTSIDE the
        project root, leaking work-item bytes.

        Resolves the full target and enforces ``is_relative_to(queue_root_resolved)``
        — mirrors ``_io.safe_resolve_under`` and the sibling Filesystem*Backend
        containment guards. Works for not-yet-created namespaces because
        ``Path.resolve()`` resolves the existing ancestor portion (so a
        symlinked parent is caught before the leaf is created).

        Returns the UNRESOLVED ``queue_root.joinpath(*parts)`` so that returned
        ``item.path`` values stay in the caller's path representation (see
        _queue_root). The resolved form is used ONLY for the containment check,
        never returned — mirroring FilesystemJournalBackend._journal_dir().

        Raises:
            PathTraversalError: when the resolved target escapes ``queue_root``
                (symlinked subdirectory), or when it cannot be resolved.
        """
        target = queue_root.joinpath(*parts)
        try:
            resolved = target.resolve()
            queue_root_resolved = queue_root.resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "queue subpath could not be resolved "
                "(symlink loop or inaccessible ancestor)",
                child="/".join(parts),
                root=str(queue_root),
            ) from exc
        if not resolved.is_relative_to(queue_root_resolved):
            raise PathTraversalError(
                "queue subpath resolves outside queue/ (symlinked ancestor refused)",
                child="/".join(parts),
                root=str(queue_root),
            )
        return target

    # ──────────────────────────────────────────────────────────────
    # Protocol methods

    def claim_next(
        self,
        role: str,
        lease_token: str,
        lease_seconds: int = 3600,
    ) -> FilesystemQueueItem | None:
        """Atomically claim the next item from queue/queued/<role>/.

        See module docstring for the full atomicity contract.
        """
        try:
            _validate_bare_component(role, "role")
            _validate_bare_component(lease_token, "lease_token")
            queue_root = self._queue_root()
            queued_dir = self._safe_under_queue(queue_root, "queued", role)
            claimed_dir = self._safe_under_queue(queue_root, "claimed", lease_token)
        except PathTraversalError:
            return None

        if not queued_dir.is_dir():
            return None

        claimed_dir.mkdir(parents=True, exist_ok=True)

        # Sort to give deterministic FIFO-by-name behavior.
        # `p.is_file()` follows symlinks — a symlink to a regular file passes.
        # The canonical-source invariant below is the explicit guard (regular-file
        # invariant + containment + symlinked-parent rejection).
        candidates = sorted(p for p in queued_dir.iterdir() if p.is_file())
        for src in candidates:
            # CANONICAL SOURCE INVARIANT: src must resolve to EXACTLY
            # queue/queued/<role>/<src.name>. This subsumes: queue_root containment,
            # symlink-leaf rejection (is_symlink), AND symlinked-PARENT rejection
            # (a symlinked queued/<role>/ makes resolve() diverge from the expected
            # canonical path — round-6 fix). Using src.name (not an attacker value)
            # as the leaf because iteration gives us the actual on-disk name.
            try:
                self._require_canonical_source(
                    src, queue_root, [("queued", role, src.name)]
                )
            except PathTraversalError:
                continue
            dst = claimed_dir / src.name
            try:
                src.rename(dst)
            except FileNotFoundError:
                # Another worker raced us to this file. Try the next.
                continue
            _write_sidecar(
                dst, lease_token=lease_token, lease_seconds=lease_seconds, role=role
            )
            return FilesystemQueueItem(
                original_name=src.name,
                role=role,
                lease_token=lease_token,
                claimed_at=time.time(),
                path=dst,
            )

        # No candidates could be claimed; clean up empty claimed_dir.
        try:
            if claimed_dir.exists() and not any(claimed_dir.iterdir()):
                claimed_dir.rmdir()
        except OSError:
            pass

        return None

    def release(self, lease_token: str, original_name: str) -> None:
        """Mark a claimed item as completed by moving it to done/.

        Atomicity: POSIX rename is the state transition. Sidecar cleanup
        (.lease.json at claimed/<lease_token>/<original_name>.lease.json)
        is best-effort. An orphaned sidecar in claimed/ after the item
        is in done/ is harmless — no recovery code looks there.
        """
        try:
            queue_root = self._queue_root()
            claimed_dir = self._safe_under_queue(queue_root, "claimed", lease_token)
        except PathTraversalError:
            return

        # Protocol entry point renames from the canonical claimed/ location.
        # The _cascade.py shim, which must finalize items at ANY depth (e.g. a
        # recovered item under queued/_recovered/), calls _release_at_path()
        # directly with the actual work-file path.
        self._release_at_path(
            claimed_dir / original_name, queue_root, lease_token, original_name
        )

    @staticmethod
    def _release_at_path(
        work_path: Path,
        queue_root: Path,
        lease_token: str,
        original_name: str,
    ) -> None:
        """Move *work_path* to done/<lease_token>/<original_name>, renaming from the
        actual path (not a reconstructed claimed/ path).

        Restores pre-carve any-depth behavior: the original _cascade.py
        release_claim() did ``item.path.rename(done_dir/...)``, so it worked for a
        work file at any depth — a normally claimed item under claimed/<token>/
        AND a recovered item under queued/_recovered/<token>/. Reconstructing
        claimed/<token>/<name> here would FileNotFoundError on recovered items.

        Validates original_name and lease_token at the sink so both the Protocol
        entry point AND any direct caller (e.g. the _cascade.py shim) are covered.
        A traversing original_name or nested lease_token raises PathTraversalError;
        the Protocol method's try/except and the _cascade.py shim's try/except both
        handle it as a no-op.
        """
        # FIX A: the ENTIRE operation — destination validation, source containment,
        # mkdir, rename, sidecar cleanup — is inside ONE try/except PathTraversalError
        # so ANY PathTraversalError (destination OR source, including unresolvable
        # work_path) fails soft for ALL callers (Protocol methods + _cascade shims).
        # FileNotFoundError/other OSError from rename propagate normally (not swallowed).
        try:
            _validate_original_name(original_name)
            _validate_bare_component(lease_token, "lease_token")
            done_dir = FilesystemQueueBackend._safe_under_queue(
                queue_root, "done", lease_token
            )
            # CANONICAL SOURCE INVARIANT: work_path must resolve to EXACTLY one of
            # the two legitimate source locations for a release operation:
            #   (1) queue/claimed/<lease_token>/<original_name>
            #   (2) queue/queued/_recovered/<lease_token>/<original_name>
            # This single call subsumes: under-queue_root containment, source-state
            # check, basename==original_name match, symlink-leaf rejection, AND
            # symlinked-PARENT rejection (a symlinked parent makes resolve() diverge
            # from the expected canonical path, so it also fails the equality check).
            FilesystemQueueBackend._require_canonical_source(
                work_path,
                queue_root,
                [
                    ("claimed", lease_token, original_name),
                    ("queued", "_recovered", lease_token, original_name),
                ],
            )
            done_dir.mkdir(parents=True, exist_ok=True)
            try:
                work_path.rename(done_dir / original_name)
            except FileNotFoundError:
                # The work file is no longer at the claimed/ source — it was
                # already moved (dead-lettered, recovered, or race-lost). The
                # dead-work-stays-dead contract (spec/44 MUST 10) requires that
                # release() does NOT affect a dead-lettered item. Since the
                # item is already gone from claimed/, there is nothing to move
                # to done/; silently no-op to satisfy the fail-soft contract.
                return
            # Remove the sidecar from the (now-moved) original location.
            sc = _sidecar_path(work_path)
            if sc.exists():
                sc.unlink(missing_ok=True)
        except PathTraversalError:
            return

    def move_to_dead_letter(
        self,
        lease_token: str,
        original_name: str,
        reason: str = "",
    ) -> None:
        """Move a claimed item to dead-letter/ — a terminal failure state.

        Dead-work-stays-dead (spec/44 MUST 10): once in dead-letter/, no
        claim, recover, or release operation affects this item.

        Atomicity: POSIX rename is the state transition. Sidecar cleanup
        (.lease.json removal) and reason write (.reason.txt) are best-effort.
        An orphaned sidecar in dead-letter/ is harmless.
        """
        try:
            queue_root = self._queue_root()
            claimed_dir = self._safe_under_queue(queue_root, "claimed", lease_token)
        except PathTraversalError:
            return

        # Protocol entry point renames from the canonical claimed/ location.
        # The _cascade.py shim, which must finalize items at ANY depth (e.g. a
        # recovered item under queued/_recovered/), calls _dead_letter_at_path()
        # directly with the actual work-file path.
        self._dead_letter_at_path(
            claimed_dir / original_name,
            queue_root,
            lease_token,
            original_name,
            reason,
        )

    @staticmethod
    def _dead_letter_at_path(
        work_path: Path,
        queue_root: Path,
        lease_token: str,
        original_name: str,
        reason: str = "",
    ) -> None:
        """Move *work_path* to dead-letter/<lease_token>/<original_name>, renaming
        from the actual path (not a reconstructed claimed/ path).

        Restores pre-carve any-depth behavior (see _release_at_path): the original
        _cascade.py move_to_dead_letter() did ``item.path.rename(target)``, so it
        worked for a work file at any depth, including a recovered item under
        queued/_recovered/<token>/. Reconstructing claimed/<token>/<name> here
        would FileNotFoundError on recovered items.

        Validates original_name and lease_token at the sink so both the Protocol
        entry point AND any direct caller (e.g. the _cascade.py shim) are covered.
        A traversing original_name or nested lease_token raises PathTraversalError;
        callers catch it as a no-op.
        """
        # FIX A: the ENTIRE operation — destination validation, source containment,
        # mkdir, rename, sidecar cleanup — is inside ONE try/except PathTraversalError
        # so ANY PathTraversalError (destination OR source, including unresolvable
        # work_path) fails soft for ALL callers (Protocol methods + _cascade shims).
        # FileNotFoundError/other OSError from rename propagate normally (not swallowed).
        try:
            _validate_original_name(original_name)
            _validate_bare_component(lease_token, "lease_token")
            dl_dir = FilesystemQueueBackend._safe_under_queue(
                queue_root, "dead-letter", lease_token
            )
            # CANONICAL SOURCE INVARIANT: same invariant as _release_at_path but
            # for the dead-letter transition. work_path must resolve to EXACTLY one of:
            #   (1) queue/claimed/<lease_token>/<original_name>
            #   (2) queue/queued/_recovered/<lease_token>/<original_name>
            # Subsumes: source-state, basename match, symlink-leaf, symlinked-parent.
            FilesystemQueueBackend._require_canonical_source(
                work_path,
                queue_root,
                [
                    ("claimed", lease_token, original_name),
                    ("queued", "_recovered", lease_token, original_name),
                ],
            )
            dl_dir.mkdir(parents=True, exist_ok=True)
            target = dl_dir / original_name
            work_path.rename(target)
            if reason:
                (dl_dir / (original_name + ".reason.txt")).write_text(
                    reason, encoding="utf-8"
                )
            # Remove the sidecar from the (now-moved) original location.
            sc = _sidecar_path(work_path)
            if sc.exists():
                sc.unlink(missing_ok=True)
        except PathTraversalError:
            return

    def renew_lease(
        self,
        lease_token: str,
        original_name: str,
        additional_seconds: int | None = None,
    ) -> None:
        """Extend the lease for an actively-worked item.

        Protocol entry point. Resolves the sidecar at the canonical claimed/
        location (claimed/<lease_token>/<original_name>.lease.json) and renews
        it. For renewing an item whose work file lives elsewhere (e.g. a
        recovered item under queued/_recovered/), use _renew_lease_at_sidecar()
        with the sidecar path computed next to the actual work file — that is
        what the _cascade.py shim does to match pre-carve behavior at any depth.

        NOTE: Uses raw Path.write_text() — behavior-neutral carve preserving
        the _cascade.py implementation. See _write_sidecar docstring.
        """
        try:
            queue_root = self._queue_root()
            claimed_dir = self._safe_under_queue(queue_root, "claimed", lease_token)
        except PathTraversalError:
            return

        sidecar = claimed_dir / (original_name + ".lease.json")
        try:
            self._renew_lease_at_sidecar(
                sidecar,
                lease_token=lease_token,
                original_name=original_name,
                additional_seconds=additional_seconds,
                queue_root=queue_root,
            )
        except PathTraversalError:
            return

    @staticmethod
    def _renew_lease_at_sidecar(
        sidecar: Path,
        lease_token: str,
        original_name: str = "",
        additional_seconds: int | None = None,
        queue_root: Path | None = None,
    ) -> None:
        """Renew (read-modify-write) the lease sidecar at an explicit path.

        Writes the sidecar wherever *sidecar* points — it does NOT reconstruct
        the path from a lease_token/depth assumption. This restores the
        pre-carve _cascade.py renew_lease() semantics, which wrote the sidecar
        directly next to the work file via _sidecar_path(item.path) and so
        worked for an item at ANY path depth (including recovered items under
        queued/_recovered/). The _cascade.py shim passes _sidecar_path(item.path)
        here; the Protocol renew_lease() passes the canonical claimed/ location.

        original_name is validated when provided (non-empty): a traversing name
        would compose into a path escaping the sidecar's directory if used as a
        filename component.  The _cascade.py shim derives the sidecar path from
        _sidecar_path(item.path) (no name concatenation), so it passes "" and
        skips the validation — consistent with its pre-carve any-depth semantics.
        The Protocol renew_lease() always supplies original_name.

        NOTE: Uses raw Path.write_text() — behavior-neutral carve. See
        _write_sidecar docstring.

        Raises:
            PathTraversalError: when original_name is provided and is not a bare
                filename (no separators, not empty, not '.' or '..').
        """
        if original_name:
            _validate_original_name(original_name)

        # SOURCE containment: the sidecar target path must resolve under queue_root.
        # The Protocol renew_lease() always anchors the sidecar under claimed/
        # (guarded by _safe_under_queue); the _cascade.py shim derives it via
        # _sidecar_path(item.path) and passes queue_root here so we can check.
        # Out-of-tree forged paths are refused; legit claimed/ and recovered/ paths pass.
        if queue_root is not None:
            try:
                if not sidecar.resolve().is_relative_to(queue_root.resolve()):
                    raise PathTraversalError(
                        "sidecar path escapes queue/",
                        child=str(sidecar),
                        root=str(queue_root),
                    )
            except (OSError, RuntimeError) as exc:
                raise PathTraversalError(
                    "sidecar path could not be resolved",
                    child=str(sidecar),
                    root=str(queue_root),
                ) from exc

        if sidecar.is_file() and not sidecar.is_symlink():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
        else:
            data = {}

        lease_secs = additional_seconds
        if lease_secs is None:
            lease_secs = data.get("lease_seconds", 3600)

        now = datetime.now(tz=timezone.utc)
        data["lease_expires_at"] = datetime.fromtimestamp(
            now.timestamp() + lease_secs, tz=timezone.utc
        ).isoformat()
        data.setdefault("lease_token", lease_token)
        data.setdefault("claimed_at", now.isoformat())
        data["lease_seconds"] = lease_secs

        sidecar.write_text(json.dumps(data), encoding="utf-8")

    def list_claimed(self, role: str | None = None) -> list[FilesystemQueueItem]:
        """Return all currently-held (claimed) work items.

        Scans queue/claimed/ subdirectories. Each subdirectory is a
        lease_token namespace. Returns one FilesystemQueueItem per work
        file (skipping .lease.json sidecars).

        Populates lease_expires_at from the sidecar when present.
        Falls back to None when the sidecar is absent or malformed
        (legacy claims). The recover_stale_claims() caller treats
        None as stale (conservative, safe for recovery).

        For the mtime fallback: items with lease_expires_at=None are
        marked by setting lease_expires_at to a past timestamp if the
        item's mtime is older than lease_seconds. This population happens
        inside recover_stale_claims() (the caller), NOT here — list_claimed
        simply returns what it knows from the sidecar.

        Args:
            role: optional filter. When set, only items whose sidecar 'role'
                matches are returned. Items with an empty/absent role in the
                sidecar are INCLUDED regardless of the filter (the role check
                is skipped when the item's role is falsy — legacy sidecars
                without a role key are never hidden). The role field on each
                returned item is populated from the sidecar. Pass None for all
                claimed items.

        Returns:
            List of FilesystemQueueItem objects. Empty list when claimed/
            is absent or no items are currently held.
        """
        try:
            queue_root = self._queue_root()
            claimed_root = self._safe_under_queue(queue_root, "claimed")
        except PathTraversalError:
            return []

        if not claimed_root.is_dir():
            return []

        items: list[FilesystemQueueItem] = []
        for lease_dir in claimed_root.iterdir():
            if not lease_dir.is_dir():
                continue
            lease_token = lease_dir.name
            # Per-lease-dir containment: a symlinked lease dir (real claimed/,
            # symlinked child) would bypass the one-time claimed/ guard above and
            # expose external bytes or crash on cleanup.  Mirror export()'s
            # per-leaf guard — skip any lease dir that escapes queue_root.
            try:
                self._safe_under_queue(queue_root, "claimed", lease_token)
            except PathTraversalError:
                continue
            for path in lease_dir.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".lease.json"):
                    continue
                # Per-work-file containment: a symlinked work file inside an
                # otherwise-legit lease dir could still escape queue_root.
                try:
                    self._safe_under_queue(
                        queue_root, "claimed", lease_token, path.name
                    )
                except PathTraversalError:
                    continue
                # Read sidecar for metadata
                sidecar = _sidecar_path(path)
                item_role = ""
                lease_expires_at = None
                # Guard the mtime fallback: between the is_file() check above and
                # this stat(), a concurrent release()/move_to_dead_letter()/recovery
                # can rename the work file out of claimed/. Skip vanished files
                # rather than raising — mirrors _recover_stale_claims_native.
                # Concurrency-robust enumeration is implied by spec/44 MUST 11's
                # "list_claimed MUST return all currently-held items": a vanished
                # item is no longer held, so omitting it is correct.
                try:
                    claimed_at = path.stat().st_mtime  # fallback
                except FileNotFoundError:
                    continue
                if sidecar.is_file() and not sidecar.is_symlink():
                    try:
                        data = json.loads(sidecar.read_text(encoding="utf-8"))
                        item_role = data.get("role", "")
                        lease_expires_at = data.get("lease_expires_at")
                        claimed_at_str = data.get("claimed_at")
                        if claimed_at_str:
                            dt = datetime.fromisoformat(claimed_at_str)
                            claimed_at = dt.timestamp()
                    except (ValueError, OSError, KeyError):
                        pass
                # role filter
                if role is not None and item_role and item_role != role:
                    continue
                items.append(
                    FilesystemQueueItem(
                        original_name=path.name,
                        role=item_role,
                        lease_token=lease_token,
                        claimed_at=claimed_at,
                        lease_expires_at=lease_expires_at,
                        path=path,
                    )
                )
        return items

    def export(self, query: Any = None) -> QueueExport:
        """Export durable queue state as a canonical QueueExport (spec/40).

        Enumerates ONLY the three durable directories by name (whitelist):
            queue/queued/ + queue/done/ + queue/dead-letter/

        Does NOT enumerate queue/ as a whole. The claimed/ directory is
        structurally excluded — not filtered. .lease.json sidecars only
        live in claimed/ and are thus automatically excluded.

        Snapshot consistency: a concurrent claim_next() completing between
        enumeration of queued/ and the read of a specific file may cause the
        exported bytes to reflect the post-claim state (file moved to claimed/).
        Such files would appear absent from the export (the whitelist finds no
        file where it was). This is the acknowledged spec/44 MUST 7 snapshot-
        consistency bound; callers requiring strict consistency MUST hold a
        LockBackend before calling export().
        """
        try:
            queue_root = self._queue_root()
        except PathTraversalError:
            return QueueExport(
                items_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._project_root),
            )

        items_with_bytes: list[tuple[str, bytes]] = []
        durable_dirs = ["queued", "done", "dead-letter"]

        # file_path values come from the UNRESOLVED queue_root (see _queue_root /
        # _safe_under_queue), so relativize against the UNRESOLVED project root.
        # The two share the same representation, so relative_to() never raises
        # for the symlinked-project_root case. The per-subdir and per-leaf
        # containment guards below use a separately-resolved queue_root anchor.
        try:
            queue_root_resolved = queue_root.resolve()
        except (OSError, RuntimeError):
            queue_root_resolved = queue_root

        def _walk_dir_no_follow(root_dir: Path) -> list[Path]:
            """Iterdir-based recursive walk that refuses to follow symlinked subdirs.

            Replaces rglob('*') to prevent the Python 3.13 vector where rglob
            follows directory symlinks by default, enabling unbounded traversal
            through a symlinked subdir pointing to a large or cyclic tree
            (the #477 DoS vector).

            At each subdirectory descent, re-asserts resolve() +
            is_relative_to(queue_root_resolved) before recursing. Skips any
            subdir that is a symlink (not just those that escape queue_root) —
            same per-subdirectory containment pattern as
            FilesystemConversationBackend.export()'s per-principal iterdir walk.

            Returns a sorted list of all file paths (files only, no dirs).
            NOT os.walk(followlinks=False): os.walk returns (dirpath, dirs,
            files) tuples without per-entry resolve checks and doesn't give
            per-subdir containment before descent.
            """
            collected: list[Path] = []
            try:
                entries = list(root_dir.iterdir())
            except (OSError, PermissionError):
                return collected
            for entry in entries:
                if entry.is_symlink():
                    # Symlinked subdirs: skip without descending (DoS prevention).
                    # Symlinked files: let the per-leaf guard below handle them.
                    if entry.is_dir():
                        continue
                    # Symlinked file — fall through to collection; per-leaf guard
                    # will reject containment-escaping symlinks.
                if entry.is_dir():
                    # Re-assert containment before descending into this subdir.
                    try:
                        subdir_resolved = entry.resolve()
                    except (OSError, RuntimeError):
                        continue
                    if not subdir_resolved.is_relative_to(queue_root_resolved):
                        continue
                    collected.extend(_walk_dir_no_follow(entry))
                else:
                    collected.append(entry)
            return collected

        for dir_name in durable_dirs:
            try:
                dir_path = self._safe_under_queue(queue_root, dir_name)
            except PathTraversalError:
                # Symlinked durable dir escaping queue/ — skip it (fail-soft for reads).
                continue
            if not dir_path.is_dir():
                continue
            for file_path in sorted(_walk_dir_no_follow(dir_path)):
                if not file_path.is_file():
                    continue
                # Exclude .lease.json files (belt-and-suspenders: they only
                # exist in claimed/, which we skip by whitelist, but be explicit)
                if file_path.name.endswith(".lease.json"):
                    continue
                # Per-LEAF symlink containment: _safe_under_queue() proved only
                # that the durable directory (queued/done/dead-letter) is
                # contained. A symlinked FILE inside that directory pointing
                # outside queue_root would otherwise have its bytes read and
                # embedded into the portable export (host-file exfiltration —
                # the #426/#427 leaf-escape class). Re-assert containment on the
                # resolved leaf; fail-soft (skip) for reads. Mirrors the spec/44
                # §"Per-subdirectory symlink containment" MUST and the
                # FilesystemOutcomeBackend.export() is_relative_to leaf guard.
                try:
                    leaf_resolved = file_path.resolve()
                except (OSError, RuntimeError):
                    continue
                if not leaf_resolved.is_relative_to(queue_root_resolved):
                    continue
                # SYMLINK INVARIANT: a symlink leaf that resolves under queue_root
                # passes the containment check above but could point from done/ into
                # claimed/ (or another excluded/ephemeral directory), bypassing the
                # spec/40 durable/ephemeral export boundary. Legit work files are
                # always regular files — skipping symlinks drops no legitimate content.
                if file_path.is_symlink():
                    continue
                try:
                    raw_bytes = file_path.read_bytes()
                    rel = file_path.relative_to(self._project_root)
                    items_with_bytes.append((str(rel), raw_bytes))
                except (OSError, ValueError):
                    pass

        return QueueExport(
            items_with_bytes=items_with_bytes,
            backend_id=self.backend_id,
            scope=str(self._project_root),
        )

    def export_all(self) -> QueueExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        return self.export(None)

    def capabilities(self) -> QueueCapabilities:
        """Backend capability declaration for FilesystemQueueBackend.

        single_host_only=True: POSIX rename atomicity does not extend
        across hosts. This backend is safe for single-host deployments only.
        Operators running multi-host deployments should use a Redis/SQS/DB
        backend that provides cross-host atomicity.

        supports_canonical_export=True: export() is implemented and
        registered in the spec/40 harness.
        """
        return QueueCapabilities(
            backend_id=self.backend_id,
            single_host_only=True,
            supports_canonical_export=True,
        )

    # ──────────────────────────────────────────────────────────────
    # Native recovery (Protocol-transparent optimization)

    def _recover_stale_claims_native(
        self, lease_seconds: int = 3600
    ) -> list[FilesystemQueueItem]:
        """Native filesystem recovery — preserves _cascade.py recovery behavior.

        This is the native recovery implementation called by the free-function
        recover_stale_claims() when available. It preserves the original
        _cascade.py logic:
          1. For items WITH a sidecar: read lease_expires_at.
          2. For items WITHOUT a sidecar (legacy claims): fall back to mtime.
          3. Malformed sidecar: fall back to mtime.
          4. Move stale items to queued/_recovered/<lease_token>/.
          5. Clean up empty lease dirs.

        This is a native Protocol-transparent optimization. The free function
        recover_stale_claims() calls it when the backend exposes it, ensuring
        the filesystem-specific mtime fallback logic runs on the filesystem
        backend while the Protocol-based path remains available for other backends.

        Returns list of FilesystemQueueItem (now in queued/_recovered/).
        """
        try:
            queue_root = self._queue_root()
            claimed_root = self._safe_under_queue(queue_root, "claimed")
        except PathTraversalError:
            return []

        if not claimed_root.is_dir():
            return []

        now = time.time()
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        recovered: list[FilesystemQueueItem] = []

        for lease_dir in claimed_root.iterdir():
            if not lease_dir.is_dir():
                continue
            lease_token = lease_dir.name
            # Per-lease-dir containment: a symlinked lease dir bypasses the
            # one-time claimed/ guard.  The rename below would otherwise move
            # external bytes into queued/_recovered/ (exfiltration), and the
            # rmdir cleanup would crash with NotADirectoryError on the symlink.
            try:
                self._safe_under_queue(queue_root, "claimed", lease_token)
            except PathTraversalError:
                continue
            for path in lease_dir.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".lease.json"):
                    continue
                # Per-work-file containment: mirrors export()'s per-leaf guard —
                # a symlinked work file inside a legit lease dir must not be
                # moved outside queue_root.
                try:
                    self._safe_under_queue(
                        queue_root, "claimed", lease_token, path.name
                    )
                except PathTraversalError:
                    continue
                sidecar = _sidecar_path(path)
                is_stale = False
                if sidecar.is_file() and not sidecar.is_symlink():
                    try:
                        data = json.loads(sidecar.read_text(encoding="utf-8"))
                        expires_at = datetime.fromisoformat(data["lease_expires_at"])
                        if expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=timezone.utc)
                        is_stale = expires_at < now_dt
                    except (KeyError, ValueError, OSError):
                        try:
                            is_stale = (now - path.stat().st_mtime) >= lease_seconds
                        except FileNotFoundError:
                            continue
                else:
                    try:
                        is_stale = (now - path.stat().st_mtime) >= lease_seconds
                    except FileNotFoundError:
                        continue

                if is_stale:
                    try:
                        recovered_dir = self._safe_under_queue(
                            queue_root, "queued", "_recovered", lease_token
                        )
                    except PathTraversalError:
                        continue
                    recovered_dir.mkdir(parents=True, exist_ok=True)
                    target = recovered_dir / path.name
                    try:
                        path.rename(target)
                        if sidecar.exists():
                            sidecar.unlink(missing_ok=True)
                        recovered.append(
                            FilesystemQueueItem(
                                original_name=path.name,
                                role="_recovered",
                                lease_token=lease_token,
                                claimed_at=now,
                                path=target,
                            )
                        )
                    except FileNotFoundError:
                        pass

            # Clean up empty lease dirs.  Wrap the rmdir in a broad OSError
            # catch so a symlinked or vanished lease_dir (NotADirectoryError /
            # FileNotFoundError) never aborts the whole recovery sweep.
            try:
                next(lease_dir.iterdir())
            except StopIteration:
                try:
                    lease_dir.rmdir()
                except OSError:
                    pass
            except (FileNotFoundError, OSError):
                pass

        return recovered

    def _reclaim_to_recovered(self, item: QueueItem) -> FilesystemQueueItem | None:
        """Protocol-based reclaim for the generic recovery path.

        Moves the item from claimed/<lease_token>/<name> to
        queued/_recovered/<lease_token>/<name>. Used by the generic
        Protocol-based recover_stale_claims path (not the native path).
        """
        try:
            # FIX B: validate bare components and source containment inside the
            # existing try/except PathTraversalError so a forged/malformed QueueItem
            # fed to the generic recovery path fails soft (returns None, no raise).
            _validate_bare_component(item.lease_token, "lease_token")
            _validate_original_name(item.original_name)
            queue_root = self._queue_root()
            claimed_dir = self._safe_under_queue(
                queue_root, "claimed", item.lease_token
            )
            recovered_dir = self._safe_under_queue(
                queue_root, "queued", "_recovered", item.lease_token
            )
            src = claimed_dir / item.original_name
            # CANONICAL SOURCE INVARIANT: src must resolve to EXACTLY
            # queue/claimed/<lease_token>/<original_name>. Subsumes: queue_root
            # containment, symlink-leaf rejection, AND symlinked-parent rejection
            # (a symlinked claimed/<token>/ makes resolve() diverge from the
            # expected canonical path).
            FilesystemQueueBackend._require_canonical_source(
                src,
                queue_root,
                [("claimed", item.lease_token, item.original_name)],
            )
            recovered_dir.mkdir(parents=True, exist_ok=True)
            target = recovered_dir / item.original_name
        except PathTraversalError:
            return None

        try:
            src.rename(target)
            sc = _sidecar_path(src)
            if sc.exists():
                sc.unlink(missing_ok=True)
            return FilesystemQueueItem(
                original_name=item.original_name,
                role=item.role,
                lease_token=item.lease_token,
                claimed_at=time.time(),
                path=target,
            )
        except FileNotFoundError:
            return None

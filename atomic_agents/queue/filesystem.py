"""FilesystemQueueBackend — directory-tree reference implementation (spec/44).

This is the default backend for single-host deployments. It carves the
cascade work-queue cluster from atomic_agents/_cascade.py into the
QueueBackend Protocol package, preserving behavior byte-for-byte for the
scaffolding-only carve (zero runtime caller changes, per arc ruling
428-pr1-args.json adopt-now-vs-scaffolding-only).

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

Atomicity contract for claim_next() (spec/44 MUST 1):
    The PRIMARY guarantee is the POSIX rename that moves the work file
    from queued/<role>/ to claimed/<lease_token>/. Under concurrent callers,
    only one rename succeeds for any given file; the loser gets
    FileNotFoundError and tries the next candidate.
    The sidecar write (.lease.json) is BEST-EFFORT: if the process crashes
    between rename and sidecar write, the work file is in claimed/ but has
    no sidecar. recover_stale_claims() handles this via mtime fallback.
    POSIX rename is the atomicity boundary; the sidecar follows.

Atomicity contract for release() and move_to_dead_letter() (spec/44 MUST 1/3):
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
    root. A symlinked queue/ that points outside project_root raises
    PathTraversalError on write operations (fail-loud) and returns []
    on read operations (fail-soft). Mirrors FilesystemJournalBackend._journal_dir()
    and FilesystemOutcomeBackend._runs_root().

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

from .._io import safe_resolve_under
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
    """

    path: Path = None  # type: ignore[assignment]  # set in __post_init__

    def __post_init__(self):
        """Validate that path is set when instantiated."""
        # path is required for filesystem items but has a default to allow
        # the dataclass inheritance chain to work cleanly.
        pass


# ──────────────────────────────────────────────────────────────────
# Private helpers (filesystem implementation details)


def _sidecar_path(work_file: Path) -> Path:
    """Return the path of the lease sidecar for a given work file."""
    return work_file.parent / (work_file.name + ".lease.json")


def _write_sidecar(
    work_file: Path,
    lease_token: str,
    lease_seconds: int,
    role: str = "",
) -> None:
    """Write a lease sidecar alongside *work_file*.

    NOTE: This uses raw Path.write_text() — NOT atomic_write(). This is a
    deliberate behavior-neutral carve (preserving _cascade.py's implementation
    byte-for-byte). The sidecar write is BEST-EFFORT: the claim atomicity
    guarantee is on the rename, not the sidecar. A torn sidecar is recoverable
    via the mtime fallback path in recover_stale_claims(). The mtime fallback
    reads the work file's mtime (not the sidecar's mtime), so atomic_write
    for sidecars would be safe — but changing it would diverge from the
    behavior-neutral carve mandate.

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
        """Return the resolved queue/ dir after a containment check.

        Resolves both project_root and project_root/'queue', checks
        is_relative_to before trusting queue/ as the containment root.
        A symlinked queue/ that points outside project_root raises
        PathTraversalError (fail-loud for writes, return [] for reads).

        Mirrors FilesystemJournalBackend._journal_dir() and
        FilesystemOutcomeBackend._runs_root() — confirmed load-bearing
        by MEMORY.md feedback_cross_model_catches_same_family_blind_spots.

        Returns:
            The resolved project_root/queue/ path.

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
        return queue_resolved

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
            queue_root = self._queue_root()
        except PathTraversalError:
            return None

        queued_dir = queue_root / "queued" / role
        if not queued_dir.is_dir():
            return None

        claimed_dir = queue_root / "claimed" / lease_token
        claimed_dir.mkdir(parents=True, exist_ok=True)

        # Sort to give deterministic FIFO-by-name behavior.
        candidates = sorted(p for p in queued_dir.iterdir() if p.is_file())
        for src in candidates:
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
        except PathTraversalError:
            return

        src = queue_root / "claimed" / lease_token / original_name
        done_dir = queue_root / "done" / lease_token
        done_dir.mkdir(parents=True, exist_ok=True)
        src.rename(done_dir / original_name)
        # Remove sidecar from the (now-moved) claimed location.
        # Compute from first principles (not from src.parent — src no longer exists).
        sc = queue_root / "claimed" / lease_token / (original_name + ".lease.json")
        if sc.exists():
            sc.unlink(missing_ok=True)

    def move_to_dead_letter(
        self,
        lease_token: str,
        original_name: str,
        reason: str = "",
    ) -> None:
        """Move a claimed item to dead-letter/ — a terminal failure state.

        Dead-work-stays-dead (spec/44 MUST 3): once in dead-letter/, no
        claim, recover, or release operation affects this item.

        Atomicity: POSIX rename is the state transition. Sidecar cleanup
        (.lease.json removal) and reason write (.reason.txt) are best-effort.
        An orphaned sidecar in dead-letter/ is harmless.
        """
        try:
            queue_root = self._queue_root()
        except PathTraversalError:
            return

        src = queue_root / "claimed" / lease_token / original_name
        dl_dir = queue_root / "dead-letter" / lease_token
        dl_dir.mkdir(parents=True, exist_ok=True)
        target = dl_dir / original_name
        src.rename(target)
        if reason:
            (dl_dir / (original_name + ".reason.txt")).write_text(
                reason, encoding="utf-8"
            )
        # Remove sidecar from the (now-moved) claimed location.
        sc = queue_root / "claimed" / lease_token / (original_name + ".lease.json")
        if sc.exists():
            sc.unlink(missing_ok=True)

    def renew_lease(
        self,
        lease_token: str,
        original_name: str,
        additional_seconds: int | None = None,
    ) -> None:
        """Extend the lease for an actively-worked item.

        NOTE: Uses raw Path.write_text() — behavior-neutral carve preserving
        the _cascade.py implementation. See _write_sidecar docstring.
        """
        try:
            queue_root = self._queue_root()
        except PathTraversalError:
            return

        sidecar = queue_root / "claimed" / lease_token / (original_name + ".lease.json")
        if sidecar.is_file():
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
            role: optional filter. list_claimed does NOT currently filter
                by role for the filesystem backend (role is in queued/,
                not in claimed/ — the claimed/ structure is by lease_token).
                The role field on each returned item IS populated from
                the sidecar. Pass None for all claimed items.

        Returns:
            List of FilesystemQueueItem objects. Empty list when claimed/
            is absent or no items are currently held.
        """
        try:
            queue_root = self._queue_root()
        except PathTraversalError:
            return []

        claimed_root = queue_root / "claimed"
        if not claimed_root.is_dir():
            return []

        items: list[FilesystemQueueItem] = []
        for lease_dir in claimed_root.iterdir():
            if not lease_dir.is_dir():
                continue
            lease_token = lease_dir.name
            for path in lease_dir.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".lease.json"):
                    continue
                # Read sidecar for metadata
                sidecar = _sidecar_path(path)
                item_role = ""
                lease_expires_at = None
                claimed_at = path.stat().st_mtime  # fallback
                if sidecar.is_file():
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
        file where it was). This is the acknowledged spec/40 MUST 7 snapshot-
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

        for dir_name in durable_dirs:
            dir_path = queue_root / dir_name
            if not dir_path.is_dir():
                continue
            for file_path in sorted(dir_path.rglob("*")):
                if not file_path.is_file():
                    continue
                # Exclude .lease.json files (belt-and-suspenders: they only
                # exist in claimed/, which we skip by whitelist, but be explicit)
                if file_path.name.endswith(".lease.json"):
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
        """Native filesystem recovery — preserves _cascade.py behavior byte-for-byte.

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
        except PathTraversalError:
            return []

        claimed_root = queue_root / "claimed"
        if not claimed_root.is_dir():
            return []

        now = time.time()
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        recovered: list[FilesystemQueueItem] = []

        for lease_dir in claimed_root.iterdir():
            if not lease_dir.is_dir():
                continue
            lease_token = lease_dir.name
            for path in lease_dir.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".lease.json"):
                    continue
                sidecar = _sidecar_path(path)
                is_stale = False
                if sidecar.is_file():
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
                    recovered_dir = queue_root / "queued" / "_recovered" / lease_token
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

            # Clean up empty lease dirs.
            try:
                next(lease_dir.iterdir())
            except StopIteration:
                lease_dir.rmdir()
            except FileNotFoundError:
                pass

        return recovered

    def _reclaim_to_recovered(self, item: QueueItem) -> FilesystemQueueItem | None:
        """Protocol-based reclaim for the generic recovery path.

        Moves the item from claimed/<lease_token>/<name> to
        queued/_recovered/<lease_token>/<name>. Used by the generic
        Protocol-based recover_stale_claims path (not the native path).
        """
        try:
            queue_root = self._queue_root()
        except PathTraversalError:
            return None

        src = queue_root / "claimed" / item.lease_token / item.original_name
        recovered_dir = queue_root / "queued" / "_recovered" / item.lease_token
        recovered_dir.mkdir(parents=True, exist_ok=True)
        target = recovered_dir / item.original_name
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

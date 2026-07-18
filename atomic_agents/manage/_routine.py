"""Shared S2 five-step safety routine infrastructure for the manage layer (spec/55).

Every management verb (govern --set, govern --restore, and future set-model,
set-goal, apply-rec) reuses these helpers so the safety contract is enforced
identically across all verbs.

The five-step order (MUST NOT reorder — M3):
  1. Validate  — caller's responsibility (verb-specific schema + enum checks)
  2. Preview   — caller renders before/after diff (ADVISORY read — see below)
  3. Confirm   — --dry-run exits here; --yes or TTY prompt (lease NOT held yet)
  4. Snapshot  — ``take_config_snapshot()`` — BEFORE overwrite, INSIDE the lease
  4b. Write    — ``core_api.atomic_write()`` — INSIDE the lease
  5. Audit     — ``append_management_audit()`` — AFTER the lease is released,
                 non-fatal on error

``run_managed_write`` is the genuine hoisted spine helper (spec/55 M11 +
#709/#710): it wraps steps 4/4b in the per-agent manage lease and re-reads the
write base FRESH from disk *inside* the lease, so two concurrent writers
cannot lost-update each other. The pre-lock read a verb performs for its S2
step 2 preview (and for --dry-run) is ADVISORY ONLY — shown to the operator
for confirmation, but never reused as the applied write's base. A base that
changed between preview and the locked write is silently accepted: the write
proceeds against the fresh base, and audit before/after reflect actual disk
state at write time (govern's ``--set`` values are absolute, not deltas, so
re-applying the same parsed field tokens against fresh content is safe and
needs no additional CAS/conflict logic).

Locking (spec/55 M11, #709):
  ``manage_lease()`` acquires a per-agent, non-blocking manage lease via
  ``get_default_lock_backend(agent_dir).acquire('manage', timeout=0)`` — a
  HIDDEN ``<agent_dir>/.manage.lock`` on the filesystem default (mirrors the
  ``.config-snapshots`` invisibility discipline; NOT a visible ``manage/``
  subdir). This is the SAME idiom ``migration/filesystem.py`` uses for its
  vault-level lock (``acquire("migration", timeout=0)``) — a genuinely
  distinct named lease, never the agent's main ``''``-named lock, so
  management writes never contend with live ``agent.call()`` runs.

  The SHAPE (context manager: acquire, yield, release-in-finally) mirrors
  ``goal/filesystem.py``'s ``_goal_lock`` idiom, but the underlying primitive
  is the LOCKED ``LockBackend.acquire()`` Protocol call — non-blocking,
  raising ``LockBusy`` on contention (mapped here to ``ManageAgentBusyError``)
  — NOT a hand-rolled ``fcntl.flock``. ``_goal_lock`` predates/bypasses
  LockBackend for GoalBackend's own internal (blocking) serialization; do not
  copy its fcntl body here (principle #2 — no hand-rolled locking once a
  Protocol exists).

  Distributed-backend agent isolation: ``get_default_lock_backend(scope_root)``
  honors ``scope_root`` only for the filesystem-shaped reference backends
  (spec/21 — "distributed backends ignore it in favor of key_prefix scoping").
  A Redis-backed deployment resolves EVERY agent's bare ``'manage'`` acquire to
  the SAME key, which would collapse the per-agent isolation this lease is
  chartered to provide. ``manage_lease()`` checks
  ``backend.capabilities().single_host_only`` and, when False, folds the
  resolved agent id into the acquired resource name
  (``f"manage:{agent_id}"``) so a distributed backend's key namespace stays
  per-agent. The filesystem backend keeps the bare ``"manage"`` name
  unchanged, preserving the ruled ``<agent>/.manage.lock`` artifact shape.

  Construction-failure vs. contention are DISTINCT, separately-caught error
  paths (P1 hardening): a misconfigured/unreachable LockBackend (bad env var,
  missing extra) raises ``ManageLockUnavailableError`` (fail-closed refusal,
  error_type='lock_backend_unavailable'); a genuine non-blocking-acquire
  contention raises ``ManageAgentBusyError`` (error_type='agent_busy'). A
  broad ``except Exception`` spanning BOTH phases would swallow the busy
  case into the generic unavailable case and lose that distinction.

  Lease scope is deliberately narrow: it is acquired AFTER the interactive
  confirm (an idle TTY prompt must never hold the lease — see the module
  docstring's ordering above) and released BEFORE the audit append (a slow
  or network-partitioned LogBackend must never extend the lease's
  availability blast radius; M8 already treats audit as best-effort/
  non-fatal, and the lease must not undermine that).

Fleet-level log scope (M8, spec/55):
  get_default_log_backend(agents_root / '_manage')
  The '_manage' dir is underscore-prefixed, so FilesystemAgentRegistryBackend
  MUST-3's prefix guard skips it during discovery — it does not surface as an agent.
  The dual-scope append is backend-aware: it fires only when the per-agent and
  fleet scopes resolve to PHYSICALLY DISTINCT stores (the default Filesystem
  backend). Under a URL-backed distributed LogBackend both scopes share one table,
  so the fleet append is skipped to avoid a duplicate row — see
  ``append_management_audit`` for the full rationale.

Snapshot retention (deferred to #750): ``.config-snapshots/<subdir>/`` grows
unboundedly — every applied write (govern --set, govern --restore, and every
future write verb) leaves one snapshot file behind forever. Growth scales
with write FREQUENCY x FLEET SIZE, not elapsed time: a home user's occasional
manual edits accumulate negligibly; an org running automated bulk sweeps
(#726 set-model, #727 apply-rec) will accumulate snapshots far faster per
agent. Retention/pruning policy is explicitly OUT OF SCOPE here — #750 owns
it. Do not build a retention policy in this module.
"""

from __future__ import annotations

import re
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..core_api import atomic_write, safe_resolve_under
from ..exceptions import LockBusy, PathTraversalError
from .exceptions import (
    ManageAgentBusyError,
    ManageLockUnavailableError,
    ManageSnapshotNotFoundError,
)


# ── Manage lease (spec/55 M11, #709) ───────────────────────────────────────────

_MANAGE_LOCK_NAME = "manage"


def get_manage_lock_backend(agent_dir: Path) -> Any:
    """Construct (but do not acquire) the manage-lease LockBackend for ``agent_dir``.

    Fail-closed (maintainer ruling ``lockbackend-construction-failure-posture``):
    any construction failure — a misconfigured ``ATOMIC_AGENTS_LOCK_BACKEND_URL``,
    an unregistered backend id, a missing optional extra (``redis`` not
    installed) — raises ``ManageLockUnavailableError`` rather than silently
    falling back to an unlocked write. ``capabilities()`` is probed here too
    (not just at acquire time) since it is what decides the per-agent key
    scoping for distributed backends, and a broken backend should fail this
    early, symmetric probe rather than surface only once ``acquire()`` runs.

    Callers should construct ONCE (right after the agent_dir containment
    guard, before S2 step 1) so a misconfigured lock backend refuses BEFORE
    any validate/preview work runs, and pass the same backend into
    ``manage_lease()`` / ``run_managed_write()`` to avoid a second
    construction.
    """
    from ..locks import get_default_lock_backend  # noqa: PLC0415

    try:
        backend = get_default_lock_backend(agent_dir)
        backend.capabilities()
    except Exception as exc:  # noqa: BLE001 -- fail-closed on ANY construction error
        raise ManageLockUnavailableError(str(exc)) from exc
    return backend


@contextmanager
def manage_lease(
    agent_dir: Path, agent_id: str, *, backend: Any = None
) -> Iterator[None]:
    """Acquire the per-agent manage lease for the write critical section.

    Non-blocking (``timeout=0``): a contended lease raises
    ``ManageAgentBusyError`` immediately rather than waiting. Released in
    ``finally`` — the caller's ``with manage_lease(...):`` block should
    contain ONLY the read(fresh)->snapshot->atomic-write region (see module
    docstring); audit happens AFTER this context manager exits.

    Args:
        agent_dir: resolved absolute path to the agent folder — the lease
            scope for the filesystem-default backend (produces the hidden
            ``<agent_dir>/.manage.lock`` artifact).
        agent_id: the resolved agent identifier. Folded into the acquired
            resource name for distributed (non-``single_host_only``)
            backends so cross-agent Redis contention does not collapse into
            one shared key (spec/21's key_prefix is deployment-wide, not
            per-agent).
        backend: an already-constructed LockBackend (from
            ``get_manage_lock_backend``) to avoid a second construction.
            Constructed on-demand when omitted.

    Raises:
        ManageLockUnavailableError: backend construction failed.
        ManageAgentBusyError: the lease is held by another process/verb.
    """
    if backend is None:
        backend = get_manage_lock_backend(agent_dir)

    lock_name = _MANAGE_LOCK_NAME
    try:
        single_host_only = backend.capabilities().single_host_only
    except Exception as exc:  # noqa: BLE001 -- fail-closed, same posture as construction
        raise ManageLockUnavailableError(str(exc)) from exc
    if not single_host_only:
        # Distributed backend (e.g. Redis): scope_root/agent_dir is ignored by
        # get_default_lock_backend's key derivation (spec/21), so fold the
        # agent id into the RESOURCE NAME to keep per-agent isolation.
        lock_name = f"{_MANAGE_LOCK_NAME}:{agent_id}"

    try:
        handle = backend.acquire(lock_name, timeout=0)
    except LockBusy as exc:
        raise ManageAgentBusyError(agent_id) from exc
    except Exception as exc:  # noqa: BLE001 -- fail-closed on any OTHER acquire error
        raise ManageLockUnavailableError(str(exc)) from exc

    try:
        yield
    finally:
        backend.release(handle)


@dataclass(frozen=True)
class ManagedWriteResult:
    """Result of a hoisted-spine locked write (``run_managed_write``).

    Attributes:
        prior_content: the FRESH (in-lock) base content read immediately
            before the edit was applied — the authoritative "before" state
            for audit purposes (NOT the earlier, advisory preview-time read).
        new_content: the content actually written to ``write_path``.
        file_existed: whether ``write_path`` existed BEFORE this write (i.e.
            whether a restorable snapshot was taken). False on a
            create-absent write.
        snapshot_path: absolute path to the pre-write snapshot, or ``None``
            when ``file_existed`` is False (no prior state to snapshot).
    """

    prior_content: str
    new_content: str
    file_existed: bool
    snapshot_path: Path | None


def run_managed_write(
    *,
    agent_dir: Path,
    agent_id: str,
    write_path: Path,
    subdir: str,
    read_base: Callable[[], tuple[str, bool]],
    apply_edit: Callable[[str], str],
    lock_backend: Any = None,
) -> ManagedWriteResult:
    """Execute the hoisted S2 steps 4/4b under the per-agent manage lease.

    This is the genuine shared helper every write verb calls for its
    read(fresh)->snapshot->atomic-write critical section (spec/55 M11,
    #709/#710 hoist). Steps 1-3 (validate/preview/confirm) happen in the
    CALLER before this is invoked — in particular, the interactive confirm
    prompt must never run while the lease is held (an idle TTY would starve
    every other manage write on the agent). Step 5 (audit) happens in the
    caller AFTER this returns, with the lease already released.

    Args:
        agent_dir: resolved absolute agent folder.
        agent_id: resolved agent identifier (lease key on distributed
            backends — see ``manage_lease``).
        write_path: absolute path of the file to atomically write.
        subdir: the snapshot subdir passed to ``take_config_snapshot``
            (explicit per verb — e.g. ``"govern"``; #709 parameterization).
        read_base: zero-arg callable returning ``(content, file_existed)``.
            Called INSIDE the lease so the base is fresh — the P0 fix for
            the lost-update race (a pre-lock read reused as the write base
            would let a lease acquired after a stale read silently clobber
            a write that landed while this caller was blocked on confirm).
        apply_edit: callable taking the fresh base content and returning the
            new content to write. May raise (e.g. a doomed edit); the
            exception propagates after the lease is released (no snapshot,
            no write attempted for a rejected edit — the snapshot only ever
            covers a base that FOLLOWS a successful edit computation, so a
            raise here writes nothing and leaves the lease's only visible
            effect as "briefly held, then released").
        lock_backend: pre-constructed LockBackend (from
            ``get_manage_lock_backend``), to avoid re-constructing inside
            the lease context. Constructed on-demand when omitted.

    Returns:
        ManagedWriteResult with the fresh prior_content/new_content/
        file_existed/snapshot_path the caller uses to build its audit record.

    Raises:
        ManageLockUnavailableError, ManageAgentBusyError: see ``manage_lease``.
        Whatever ``apply_edit`` raises (e.g. ``ValueError`` for a doomed
        edit) or ``OSError``/``Exception`` from the snapshot/write calls.
    """
    with manage_lease(agent_dir, agent_id, backend=lock_backend):
        prior_content, file_existed = read_base()
        new_content = apply_edit(prior_content)

        snapshot_path: Path | None = None
        if file_existed:
            snapshot_path = take_config_snapshot(
                agent_dir, prior_content, subdir=subdir
            )

        atomic_write(write_path, new_content)

    return ManagedWriteResult(
        prior_content=prior_content,
        new_content=new_content,
        file_existed=file_existed,
        snapshot_path=snapshot_path,
    )


# ── Snapshot infrastructure ────────────────────────────────────────────────────

_SNAPSHOT_DIR = ".config-snapshots"

# Shape of a snapshot filename actually produced by take_config_snapshot():
# <14-digit-UTC-timestamp>-<8-hex-char-uuid-suffix>.md (see the ts/suffix
# construction below). Anchored full-match so a crafted snapshot_id that
# merely CONTAINS this shape as a substring does not slip through. Used by
# both resolve_snapshot_path() (reject non-shaped ids outright, adversarial
# review hardening) and list_snapshots() (list only genuinely-generated snapshot
# files, so a planted .tmp / evil.md is neither listed nor restorable).
_SNAPSHOT_FILENAME_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}\.md\Z")


def take_config_snapshot(agent_dir: Path, content: str, *, subdir: str) -> Path:
    """Snapshot ``content`` (pre-write file bytes) to the dedicated config-snapshot dir.

    Snapshot location (spec/55 M3):
        <agent_dir>/.config-snapshots/<subdir>/<ISO8601-timestamp>-<uuid8>.md

    ``subdir`` is an explicit, per-verb parameter (#709 parameterization) —
    e.g. ``"govern"`` for the ``govern`` verb — so a future verb's (set-model,
    apply-rec) snapshot namespace never collides with govern's. The dir is
    dot-prefixed so it is invisible to memory recall and registry discovery.
    Each snapshot is a single file containing the verbatim prior file
    content. The snapshot_path is returned for inclusion in the audit
    extra{}.

    Called BEFORE ``core_api.atomic_write`` — the ordering invariant M3
    requires. A failure here aborts the write (snapshot is the rollback
    foundation; a write without a snapshot is unrecoverable).

    Args:
        agent_dir: resolved absolute path to the agent folder.
        content: the current (pre-write) file content to snapshot.
        subdir: the per-verb snapshot namespace (required, no default —
            #709: every caller names its own namespace explicitly).

    Returns:
        Absolute Path to the written snapshot file.

    Raises:
        OSError: if the snapshot cannot be written.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    filename = f"{ts}-{suffix}.md"

    snapshot_path = agent_dir / _SNAPSHOT_DIR / subdir / filename

    # Canonical-containment invariant (F2): resolve the snapshot path and assert
    # it stays under agent_dir. A planted symlink at agent/.config-snapshots
    # pointing outside the agent directory causes .resolve() to follow the
    # symlink out — safe_resolve_under raises PathTraversalError, which is
    # re-raised as OSError so take_config_snapshot's documented OSError contract
    # is preserved and run_govern surfaces a clean refusal before any write.
    # This is ONE canonical invariant, not a per-name symlink check: any
    # .config-snapshots variant that escapes agent_dir is caught here, whether
    # it is a symlink, a bind-mount, or a future escaping scenario.
    try:
        safe_resolve_under(snapshot_path, agent_dir)
    except Exception as exc:
        raise OSError(
            f"Snapshot path escapes agent directory — refused: {exc}"
        ) from exc

    atomic_write(snapshot_path, content)
    return snapshot_path


def list_snapshots(agent_dir: Path, subdir: str) -> list[Path]:
    """Return every snapshot file under ``<agent_dir>/.config-snapshots/<subdir>/``.

    Read-only, sorted lexicographically (== chronologically, since the
    filename is an ISO-8601-prefixed timestamp) oldest-first. Used by
    ``--list-snapshots`` (a pure read — MUST NOT acquire the manage lease;
    see the spec/55 M11 note that reads never contend with writes).

    This is a directory listing only (no content parse per entry), so it
    stays fast as the directory grows — snapshot RETENTION/pruning is
    deferred to #750, and an O(n) full-content read on every list call would
    make that deferred problem worse, not neutral.

    Returns an empty list when the subdir does not exist (no snapshots taken
    yet — not an error).
    """
    snap_dir = agent_dir / _SNAPSHOT_DIR / subdir
    try:
        safe_resolve_under(snap_dir, agent_dir)
    except PathTraversalError:
        return []
    if not snap_dir.is_dir():
        return []
    return sorted(
        p
        for p in snap_dir.iterdir()
        if p.is_file() and _SNAPSHOT_FILENAME_RE.match(p.name)
    )


def resolve_snapshot_path(agent_dir: Path, subdir: str, snapshot_id: str) -> Path:
    """Resolve a ``--restore <snapshot-id>`` argument to its on-disk path.

    ``snapshot_id`` is untrusted CLI input. Two defenses (#710 restore
    cross-agent / path-traversal hardening, mirroring the same
    canonical-containment idiom ``take_config_snapshot`` already uses on the
    write side):

    1. Reject any snapshot_id containing a path separator or a leading dot
       BEFORE any path construction — a crafted id like
       ``../../other-agent/.config-snapshots/govern/x.md`` or an absolute
       path must never reach ``Path.__truediv__``.
    2. Resolve the constructed path and verify containment under
       ``agent_dir`` via ``safe_resolve_under`` (catches a symlink-swap
       TOCTOU the same way the write-side snapshot guard does).

    Args:
        agent_dir: resolved absolute path to the TARGET agent's folder (the
            agent the operator is restoring, resolved via the registry —
            never derived from the snapshot_id itself).
        subdir: the verb's snapshot namespace (e.g. ``"govern"``).
        snapshot_id: operator-supplied identifier — expected to be a bare
            filename like ``20260718T120000-abcd1234.md``.

    Returns:
        The resolved, contained, absolute Path to the snapshot file.

    Raises:
        ManageSnapshotNotFoundError: the id is malformed (contains a path
            separator / is empty), does not match the exact filename shape
            take_config_snapshot() generates (adversarial review hardening — rejects a
            plausible-but-fabricated id outright, before any path
            construction), or the resolved file does not exist under THIS
            agent's snapshot directory (no cross-agent restore — a snapshot
            id that is only valid for a different agent resolves to a
            non-existent path under this agent_dir and hits this same
            not-found refusal, never leaking whether it exists elsewhere).
    """
    if (
        not snapshot_id
        or "/" in snapshot_id
        or "\\" in snapshot_id
        or snapshot_id
        in (
            ".",
            "..",
        )
        or _SNAPSHOT_FILENAME_RE.match(snapshot_id) is None
    ):
        raise ManageSnapshotNotFoundError(snapshot_id, agent_dir.name)

    snap_dir = agent_dir / _SNAPSHOT_DIR / subdir

    # Two-layer containment (adversarial review hardening): (1) the snapshot dir itself
    # must not escape agent_dir — blocks a whole-dir symlink swap (e.g.
    # .config-snapshots/govern symlinked to an external location); (2) the
    # resolved candidate must stay under the snapshot dir ITSELF, not merely
    # somewhere under agent_dir — blocks a snapshot_id that is itself a
    # symlink (or reaches one) pointing to another file inside agent_dir but
    # outside the snapshot dir (e.g. governance.md itself). Root=agent_dir
    # alone (the pre-hardening shape) misses case (2); root=snap_dir alone misses
    # case (1) — a fully-symlinked snap_dir resolves consistently against
    # itself, so checking candidate-under-snap_dir with an UNVERIFIED snap_dir
    # never catches a directory-level swap. Both checks are required.
    try:
        safe_resolve_under(snap_dir, agent_dir)
    except PathTraversalError as exc:
        raise ManageSnapshotNotFoundError(snapshot_id, agent_dir.name) from exc

    candidate = snap_dir / snapshot_id

    try:
        resolved = safe_resolve_under(candidate, snap_dir)
    except PathTraversalError as exc:
        raise ManageSnapshotNotFoundError(snapshot_id, agent_dir.name) from exc

    if not resolved.is_file():
        raise ManageSnapshotNotFoundError(snapshot_id, agent_dir.name)

    return resolved


# ── Audit infrastructure ───────────────────────────────────────────────────────


def _physical_store_key(backend: Any) -> str | None:
    """Return a stable key identifying the PHYSICAL store a LogBackend writes to.

    Two backends that return the SAME key write to the same underlying store; two
    that return DIFFERENT keys write to distinct stores. ``None`` means the backend
    type is unrecognised and its scoping cannot be determined.

    This exists because ``get_default_log_backend(scope_root)`` only honors
    ``scope_root`` for the scope-aware reference backends (Filesystem = a distinct
    ``<scope_root>/log`` dir; SQLite-no-URL = a distinct ``<scope_root>/.logs.db``
    file). The URL-backed distributed backends (SQLite+URL, Postgres) IGNORE
    ``scope_root`` (logs/__init__.py: "future distributed backends ignore it") and
    resolve to one shared table — so a naive two-scope append would write the SAME
    ``run_id`` twice into one store and double-count every fleet aggregation.

    Duck-typed on each reference backend's storage-identity attribute (no imports,
    to keep the base CLI lean and avoid pulling the psycopg extra):
      - Filesystem : ``_scope_root``   → keyed on the resolved log-root dir
      - SQLite     : ``_db_path_str``  → keyed on the resolved db file path
                     (``:memory:`` is per-connection, so keyed on instance id)
      - Postgres   : ``_safe_url``     → keyed on the (redacted) DSN
    """
    scope_root = getattr(backend, "_scope_root", None)
    if scope_root is not None:
        return f"fs:{Path(scope_root).resolve()}"

    db_path = getattr(backend, "_db_path_str", None)
    if db_path is not None:
        if db_path == ":memory:":
            # Each ``:memory:`` connection is its own database (distinct store).
            return f"sqlite-mem:{id(backend)}"
        return f"sqlite:{Path(db_path).resolve()}"

    safe_url = getattr(backend, "_safe_url", None)
    if safe_url is not None:
        return f"pg:{safe_url}"

    return None


def append_management_audit(
    record: Any,
    agent_dir: Path,
    agents_root: Path,
) -> tuple[bool, list[str]]:
    """Append the management RunRecord to the per-agent and fleet log scopes.

    Called AFTER the manage lease has already been released (``run_managed_write``
    returns before this is invoked) — a slow or network-partitioned LogBackend
    append must never extend the lease's contention window (M8 already treats
    audit as best-effort/non-fatal; a lease held through this call would
    contradict that by turning a non-fatal audit hiccup into an availability
    hazard for every OTHER manage write on the same agent).

    Also verifies ``record.to_dict()`` is JSON-serialisable BEFORE attempting
    either append — a caller bug (e.g. a raw ``Path`` landing in an ``extra``
    value instead of a ``str``) must warn-and-continue here, not raise: the
    governed write (or restore) has ALREADY been applied by the time this
    function runs, so an uncaught exception here would surface a false
    "command failed" exit code on an operation that actually succeeded. Every
    verb that calls this shared helper inherits the same "audit-drop warns,
    never raises, never undoes an already-applied write" guarantee for free.

    Spec/55 M8 — the event is appended to two scopes so that:
      - Per-agent:  ``get_default_log_backend(agent_dir)``            — zero-config
        home-user visibility (the event shows up in the agent's own log)
      - Fleet-wide: ``get_default_log_backend(agents_root / '_manage')`` — a stream
        that survives the agent's deletion.

    Backend-aware two-scope model (M8, P1 convergence fix). The two-copy shape is
    correct ONLY when the two scopes resolve to physically DISTINCT stores (the
    default Filesystem backend: two separate ``log/`` dirs; SQLite-no-URL: two
    separate db files). Under a URL-backed distributed LogBackend (Postgres, or
    SQLite with a shared ``ATOMIC_AGENTS_LOG_BACKEND_URL``) ``scope_root`` is
    ignored and BOTH scopes resolve to the SAME table — appending twice would emit
    a DUPLICATE row with an identical ``run_id`` and double-count every fleet
    COUNT / GROUP-BY-agent / cost aggregation (audit integrity is structural,
    principle #5). When the stores collapse, the single per-agent append IS the
    fleet stream (the rows already survive agent-folder deletion because they live
    in the central table, queryable by ``primitive=manage_govern``/``manage_restore``),
    so the fleet append is skipped. If the backend type is unrecognised, we
    conservatively treat the stores as collapsed (append once) to avoid duplicate
    rows.

    Both appends are in separate try/except blocks. A failure in either is caught;
    the warning list is returned to the caller. The verb exits 0 on audit-drop
    (M8: "warn, never fail silently, MUST NOT compromise rollback").

    Args:
        record: the RunRecord to append (must be fully populated before this call).
        agent_dir: resolved absolute path to the agent folder.
        agents_root: the fleet root (CLI --agents-root or env default).

    Returns:
        Tuple of (all_ok: bool, warnings: list[str]).
    """
    import json  # noqa: PLC0415

    # Verify JSON-serialisability BEFORE any append attempt (P1 hoist — this
    # check previously lived only in govern.py's call site; hoisting it here
    # means EVERY verb sharing this helper — govern and restore alike — gets
    # the same warn-never-raise guarantee, including restore's new
    # ``restored_from`` / ``snapshot_path`` extra{} keys).
    try:
        json.dumps(record.to_dict())
    except (TypeError, ValueError) as exc:
        msg = f"Warning: management audit record is not JSON-serialisable: {exc}"
        print(msg, file=sys.stderr)
        return False, [msg]

    # Lazy import to keep the base CLI lean (spec/55 note; principle #6).
    from ..logs import get_default_log_backend  # noqa: PLC0415

    warnings: list[str] = []

    # Resolve both backends up front so we can detect a shared-store collapse
    # before appending twice into it. Construction (not just .append()) can raise
    # for a swapped-but-misconfigured LogBackend: ATOMIC_AGENTS_LOG_BACKEND=<typo>
    # → BackendNotRegistered; =postgres without a URL → ValueError; without psycopg
    # → ImportError. That failure must degrade to the same non-fatal audit-drop
    # warning as an append failure (M8: warn, never fail silently, MUST NOT
    # compromise rollback) — the governance.md write + snapshot already landed, so
    # the verb still exits 0. The home-user Filesystem default constructs without
    # I/O, so this only trips on a swapped/misconfigured backend.
    try:
        per_agent_backend = get_default_log_backend(agent_dir)
        # The '_manage' dir is underscore-prefixed, so FilesystemAgentRegistryBackend
        # MUST-3's prefix guard skips it during discovery — never surfaces as an agent.
        fleet_backend = get_default_log_backend(agents_root / "_manage")
        per_agent_key = _physical_store_key(per_agent_backend)
        fleet_key = _physical_store_key(fleet_backend)
    except Exception as exc:  # noqa: BLE001
        msg = f"Warning: management audit backend could not be constructed: {exc}"
        print(msg, file=sys.stderr)
        return False, [msg]

    # Distinct only when BOTH keys are known and differ. Equal keys → the two
    # scopes share one store → append once (the shared-store collapse). Unknown
    # keys (an unrecognised custom backend) → we cannot confirm the collapse, so
    # we still append once (no duplicate-row risk) but surface a non-fatal warning
    # below so the skipped fleet copy is observable, not silent (M8).
    stores_identifiable = per_agent_key is not None and fleet_key is not None
    stores_are_distinct = stores_identifiable and per_agent_key != fleet_key

    # Per-agent log backend (always appended — this is the home-user surface, and
    # under a shared store it is also the fleet stream).
    try:
        per_agent_backend.append(record)
    except Exception as exc:  # noqa: BLE001
        msg = f"Warning: per-agent management audit record could not be written: {exc}"
        print(msg, file=sys.stderr)
        warnings.append(msg)

    # Fleet-level log backend — only when it is a physically distinct store, else
    # the per-agent append already landed the one immutable copy in the shared store.
    if stores_are_distinct:
        try:
            fleet_backend.append(record)
        except Exception as exc:  # noqa: BLE001
            msg = f"Warning: fleet management audit record could not be written: {exc}"
            print(msg, file=sys.stderr)
            warnings.append(msg)
    elif not stores_identifiable:
        # An unrecognised custom LogBackend whose scopes MIGHT be physically
        # distinct: we could not read a store identity, so a second append could
        # be either the required fleet copy or a duplicate row. We take the
        # no-duplicate-row path (append once) but must not do so silently — M8
        # requires a dropped fleet copy to surface a warning, never fail silently.
        msg = (
            "Warning: fleet management audit copy skipped — the configured "
            "LogBackend does not expose a recognised store identity, so a "
            "shared-store collapse could not be confirmed (the per-agent copy "
            "landed). Use a reference LogBackend, or a backend that exposes a "
            "store-identity attribute, to receive the distinct fleet copy."
        )
        print(msg, file=sys.stderr)
        warnings.append(msg)

    return len(warnings) == 0, warnings

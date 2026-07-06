"""Shared S2 five-step safety routine infrastructure for the manage layer (spec/55).

Every management verb (govern, set-model, set-goal, apply-rec ...) reuses these
helpers so the safety contract is enforced identically across all verbs.

The five-step order (MUST NOT reorder — M3):
  1. Validate  — caller's responsibility (verb-specific schema + enum checks)
  2. Preview   — caller renders before/after diff
  3. Confirm   — --dry-run exits here; --yes or TTY prompt
  4. Snapshot  — ``take_config_snapshot()`` — BEFORE overwrite
  4b. Write    — ``_io.atomic_write()`` — called by the verb
  5. Audit     — ``append_management_audit()`` — AFTER write, non-fatal on error

``take_config_snapshot`` and ``append_management_audit`` are exported so each verb
can call them in the correct sequence without copy-pasting the error handling.

Fleet-level log scope (M8, spec/55):
  get_default_log_backend(agents_root / '_manage')
  The '_manage' dir is underscore-prefixed, so FilesystemAgentRegistryBackend
  MUST-3's prefix guard skips it during discovery — it does not surface as an agent.
  The dual-scope append is backend-aware: it fires only when the per-agent and
  fleet scopes resolve to PHYSICALLY DISTINCT stores (the default Filesystem
  backend). Under a URL-backed distributed LogBackend both scopes share one table,
  so the fleet append is skipped to avoid a duplicate row — see
  ``append_management_audit`` for the full rationale.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._io import atomic_write, safe_resolve_under


# ── Snapshot infrastructure ────────────────────────────────────────────────────

_SNAPSHOT_DIR = ".config-snapshots"
_SNAPSHOT_SUBDIR = "govern"


def take_config_snapshot(agent_dir: Path, content: str) -> Path:
    """Snapshot ``content`` (pre-write file bytes) to the dedicated config-snapshot dir.

    Snapshot location (spec/55 M3):
        <agent_dir>/.config-snapshots/govern/<ISO8601-timestamp>-<uuid8>.md

    The dir is dot-prefixed so it is invisible to memory recall and registry
    discovery. Each snapshot is a single file containing the verbatim prior
    governance.md content. The snapshot_path is returned for inclusion in the
    audit extra{}.

    Called BEFORE ``_io.atomic_write`` — the ordering invariant M3 requires.
    A failure here aborts the write (snapshot is the rollback foundation;
    a write without a snapshot is unrecoverable).

    Args:
        agent_dir: resolved absolute path to the agent folder.
        content: the current (pre-write) file content to snapshot.

    Returns:
        Absolute Path to the written snapshot file.

    Raises:
        OSError: if the snapshot cannot be written.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    filename = f"{ts}-{suffix}.md"

    snapshot_path = agent_dir / _SNAPSHOT_DIR / _SNAPSHOT_SUBDIR / filename

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
    in the central table, queryable by ``primitive=manage_govern``), so the fleet
    append is skipped. If the backend type is unrecognised, we conservatively
    treat the stores as collapsed (append once) to avoid duplicate rows.

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

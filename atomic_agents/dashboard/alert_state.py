"""Alert state sidecar for the Fleet Console (spec/52).

Implements the append-only JSONL event log at <agents_root>/_console/alert_state.jsonl.
Current state is replayed on read (last-event-per-key wins); the size-reducing log
compaction (rewrite to one event per live alert) runs on WRITE inside
append_alert_event() once the log exceeds _COMPACT_THRESHOLD lines — read_alert_state()
never rewrites the file. Each append is serialized under an exclusive fcntl.flock on a
sidecar lock — the JournalBackend spec/43 shape applied at fleet level.

The _console/ directory is excluded from registry discovery by the _ prefix (same
predicate used by FilesystemAgentRegistryBackend). The lock file is at
_console/.alert_state.lock. This module is POSIX-only — fcntl.flock is not available
on Windows, consistent with journal/filesystem.py (no try/except shim; a Windows
operator gets a clear ImportError, not a silent no-op bypass).

Audit-native (Principle #5): each event is {ts, actor, alert_key, action,
snooze_until?}. The event log is the source of truth; the compacted state is
derived-on-read.

Atomicity contract (spec/52 MUST 1):
    - All appends acquire LOCK_EX on _console/.alert_state.lock.
    - Compaction (the read-modify-rewrite that reduces log size) ALSO runs under
      LOCK_EX on the same lock file, so a concurrent append cannot interleave with
      a compaction rewrite.
    - Concurrent readers acquire LOCK_SH before reading, so they cannot see a
      torn line from a concurrent append.

snooze_until timezone contract (spec/52 MUST 6):
    All snooze_until values are stored and compared as UTC ISO-8601 strings
    (ending in +00:00). The Z-suffix normalization happens at write time.

This module is imported by both render.py (read path) and serve.py (write path).
"""

from __future__ import annotations

import fcntl  # POSIX-only — intentional, see module docstring
import json
import os
from datetime import datetime, timezone
from pathlib import Path


# The _console dir name is load-bearing: the leading _ keeps it out of
# FilesystemAgentRegistryBackend's list_agents() predicate. Never rename
# to 'console' (no underscore) without updating that predicate.
_CONSOLE_DIRNAME = "_console"
_SIDECAR_FILENAME = "alert_state.jsonl"
_LOCK_FILENAME = ".alert_state.lock"

# Compact when the JSONL file exceeds this many lines (tunable in PR2).
_COMPACT_THRESHOLD = 1000


def _console_dir(agents_root: Path) -> Path:
    """Return the _console/ directory path (does NOT create it)."""
    return agents_root / _CONSOLE_DIRNAME


def _sidecar_path(agents_root: Path) -> Path:
    return _console_dir(agents_root) / _SIDECAR_FILENAME


def _lock_path(agents_root: Path) -> Path:
    return _console_dir(agents_root) / _LOCK_FILENAME


def _normalize_snooze_until(value: str | None) -> str | None:
    """Normalize a snooze_until string to UTC ISO-8601 (+00:00 suffix).

    Replaces trailing Z with +00:00, parses as aware datetime, and returns
    the canonical UTC ISO-8601 string. Raises ValueError on unparseable input.
    """
    if value is None:
        return None
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        # Treat naive as UTC (defense-in-depth; callers should always supply tz-aware)
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def append_alert_event(
    agents_root: Path,
    alert_key: str,
    action: str,
    actor: str = "operator",
    snooze_until: str | None = None,
) -> None:
    """Append one ack/snooze event to the alert_state sidecar under exclusive flock.

    spec/52 MUST 1: this function is the ONLY write path for alert state.
    The lock covers the ENTIRE validate-and-append: we hold LOCK_EX across
    the read (compact-check), the compaction if needed, and the append.

    action: "ack" | "snooze" | "unsnooze". Any other value raises ValueError.
    snooze_until: REQUIRED when action == "snooze" (raises ValueError if absent);
        MUST be absent for "ack" / "unsnooze" (raises ValueError otherwise);
        UTC ISO-8601 string, normalized via _normalize_snooze_until.

    Idempotency (MUST 5) is enforced by the serve-layer handler (it reads current
    state and no-ops an unchanged ack / same-window re-snooze / re-unsnooze before
    calling this); this function is the unconditional, validated write primitive.
    Schema validation here is defense-in-depth so a direct caller cannot append a
    malformed event (an unknown action, or a snooze with no window).
    """
    if action not in ("ack", "snooze", "unsnooze"):
        raise ValueError(
            f"invalid action {action!r}: must be one of ack / snooze / unsnooze"
        )
    if action == "snooze" and snooze_until is None:
        raise ValueError("snooze_until is required for action 'snooze'")
    if action != "snooze" and snooze_until is not None:
        raise ValueError(f"snooze_until must be absent for action {action!r}")

    console = _console_dir(agents_root)
    # mkdir before flock attempt so the lock-file open cannot fail with ENOENT
    # (mirrors journal/filesystem.py:260 exact pattern)
    console.mkdir(parents=True, exist_ok=True)

    event: dict = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "actor": actor,
        "alert_key": alert_key,
        "action": action,
    }
    if snooze_until is not None:
        event["snooze_until"] = _normalize_snooze_until(snooze_until)

    lock_file = str(_lock_path(agents_root))
    sidecar = _sidecar_path(agents_root)

    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # Compact if over threshold (read+rewrite under the same lock)
            if sidecar.exists():
                lines = sidecar.read_text(encoding="utf-8").splitlines()
                if len(lines) > _COMPACT_THRESHOLD:
                    state = _compact_lines(lines)
                    compact_text = _state_to_jsonl(state)
                    # atomic_write not used here — we're under the flock so
                    # concurrent readers are blocked (LOCK_SH on read path),
                    # but atomic_write's temp+rename is still better than
                    # open('w') for crash safety.
                    _write_under_lock(sidecar, compact_text)

            # Append the new event
            line = json.dumps(event, sort_keys=True) + "\n"
            with sidecar.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_alert_state(agents_root: Path) -> dict[str, dict]:
    """Read and compact the alert state sidecar, returning current state.

    Returns a dict keyed by alert_key, each value being:
        {"status": "acked" | "snoozed" | "open",
         "ts": <last-event ISO>,
         "snooze_until": <UTC ISO> | None}

    Holds LOCK_SH during the read so no concurrent append can produce a
    torn line in the middle of our read (spec/52 MUST 2 compaction determinism).
    Returns {} if the sidecar is absent (normal before any events).
    Fail-soft on unreadable sidecar: returns {} with a warning (never crashes
    the dashboard render — Principle #8 degrade-rather-than-crash).
    """
    sidecar = _sidecar_path(agents_root)
    if not sidecar.exists():
        return {}

    console = _console_dir(agents_root)
    console.mkdir(parents=True, exist_ok=True)
    lock_file = str(_lock_path(agents_root))

    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            try:
                lines = sidecar.read_text(encoding="utf-8").splitlines()
            except OSError:
                return {}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    return _compact_lines(lines)


def _compact_lines(lines: list[str]) -> dict[str, dict]:
    """Replay JSONL event lines in file order; last-event-per-key wins.

    spec/52 MUST 2: ordering is file-append order; last event per alert_key
    determines the current state. snooze_until expiry is checked at read time
    against datetime.now(tz=timezone.utc).

    Returns a dict keyed by alert_key.
    """
    now = datetime.now(tz=timezone.utc)
    state: dict[str, dict] = {}

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            # Skip corrupt lines — degrade rather than crash
            continue

        key = event.get("alert_key")
        if not key:
            continue
        action = event.get("action", "")
        ts = event.get("ts", "")

        if action == "ack":
            state[key] = {"status": "acked", "ts": ts, "snooze_until": None}
        elif action == "snooze":
            snooze_until = event.get("snooze_until")
            # Check if snooze has already expired
            if snooze_until:
                try:
                    until_dt = datetime.fromisoformat(snooze_until)
                    if until_dt.tzinfo is None:
                        until_dt = until_dt.replace(tzinfo=timezone.utc)
                    if until_dt <= now:
                        # Expired — treat as open
                        state[key] = {"status": "open", "ts": ts, "snooze_until": None}
                        continue
                except (ValueError, TypeError):
                    pass
            state[key] = {"status": "snoozed", "ts": ts, "snooze_until": snooze_until}
        elif action == "unsnooze":
            state[key] = {"status": "open", "ts": ts, "snooze_until": None}

    return state


# Compacted-state status → append-event action verb. _compact_lines only
# recognizes the *action* verbs ("ack"/"snooze"/"unsnooze"); the compacted
# state carries *status* values ("acked"/"snoozed"/"open"). _state_to_jsonl
# MUST emit action verbs so the rewritten file re-parses identically — emitting
# the status string ("acked"/"snoozed") produces lines _compact_lines drops,
# silently losing all ack/snooze state after the first compaction (spec/52 MUST 2
# compaction determinism: the post-compaction file must reconstruct the same state).
_STATUS_TO_ACTION = {
    "acked": "ack",
    "snoozed": "snooze",
    "open": "unsnooze",
}


def _state_to_jsonl(state: dict[str, dict]) -> str:
    """Convert compacted state back to JSONL for compaction rewrite.

    Emits the append-event *action* verb (not the compacted *status* string) so
    the rewritten file re-parses to the identical state on the next read
    (spec/52 MUST 2). See _STATUS_TO_ACTION for the mapping.
    """
    lines = []
    for alert_key, entry in state.items():
        status = entry["status"]
        event = {
            "ts": entry["ts"],
            "actor": "compaction",
            "alert_key": alert_key,
            "action": _STATUS_TO_ACTION.get(status, "unsnooze"),
        }
        if entry.get("snooze_until"):
            event["snooze_until"] = entry["snooze_until"]
        lines.append(json.dumps(event, sort_keys=True))
    return "\n".join(lines) + "\n" if lines else ""


def _write_under_lock(path: Path, content: str) -> None:
    """Write content to path — called inside an existing flock, so no temp+rename needed.

    Uses a direct open('w') because we already hold the exclusive lock;
    atomic_write would acquire its own temp file but we can't use it here
    without releasing the lock (which would defeat the purpose).
    """
    with path.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())

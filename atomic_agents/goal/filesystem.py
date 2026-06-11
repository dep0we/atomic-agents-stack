"""FilesystemGoalBackend — directory-tree reference implementation (spec/41).

This is the default backend for single-host deployments. It wraps the same
on-disk shape GoalManager has used since the framework's first goal support:
    <agent_root>/goal.md            — active goal (frontmatter + markdown body)
    <agent_root>/goal_history.jsonl — structured history events (append-only)
    <agent_root>/goal_archive/      — archived goals (one .md per archived goal)

Zero behavior change for existing reactive/hybrid agents: construction is
side-effect-free (no filesystem I/O in __init__), goal_text() returns ''
when goal.md is absent, list_archived() returns [] when goal_archive/ is
absent. Agents without goal.md pass doctor.check_goal_backend with a PASS
note (not a SKIP — matching the corpus_backend pattern for agents without
a corpus).

Ordering contract for apply_transition():
    The method acquires an exclusive advisory lock (fcntl.flock on
    <agent_root>/.goal.lock) before the read-modify-write sequence. Both
    the goal.md atomic_write AND the goal_history.jsonl append happen under
    this lock, serialized and ordered: goal.md is written FIRST, the JSONL
    audit line second. The lock guarantees serialization + ordering, NOT
    crash-rollback — a crash between the two writes is permissible and leaves
    goal.md updated with no audit line (recoverable, retry-safe). The
    conformance requirement (spec/41 MUST 6) is only that the reverse never
    occur: a JSONL audit line MUST NOT exist for a goal.md state that was not
    written. A Postgres backend wraps the same sequence in a SQL transaction,
    which additionally rolls back on failure. The lock is released in a finally
    block.

archive_goal() also acquires the goal lock so the collision-suffix loop is
race-free (no TOCTOU window between .exists() check and atomic_write).

spec/40 addendum: GoalBackend composes the Exportable Protocol at definition
time (not retrofitted). FilesystemGoalBackend.capabilities() returns
GoalCapabilities(backend_id='filesystem', supports_canonical_export=True,
supports_archive=True, supports_history_query=True).

Import boundary (circular-import safety):
    - Imports only from ..exceptions, .._io, .types — no imports from
      ..goal (the shim) or any module that imports ..goal. This keeps
      goal/__init__.py importable without loading the LLM stack.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import frontmatter

from .._io import atomic_write, atomic_append_jsonl
from ..exceptions import AtomicAgentsError, GoalCorrupted, SchemaValidationError
from .types import (
    VALID_SUB_GOAL_STATUSES,
    Goal,
    GoalCapabilities,
    GoalExport,
    SubGoal,
    build_goal_frontmatter,
    validate_goal,
)

# Schema version + valid-status sets are the single-source constants in
# goal/types.py (imported above where needed). Validation goes through the
# shared validate_goal() — there is no second, weaker validator in this module.


class FilesystemGoalBackend:
    """Filesystem reference impl for GoalBackend Protocol (spec/41).

    Scoped to one agent root — <agent_root>/goal.md and
    <agent_root>/goal_archive/. Constructed once per agent; construction is
    side-effect-free (no filesystem I/O in __init__).

    Thread/process safety: apply_transition() and archive_goal() acquire an
    exclusive advisory lock (fcntl.flock) on <agent_root>/.goal.lock before
    any read-modify-write. This serializes concurrent CLI + cron invocations
    against the same agent root.

    Path-traversal guard: __init__ resolves agent_root to an absolute path.
    Relative paths and paths containing '..' are rejected at construction.
    """

    @property
    def backend_id(self) -> str:
        return "filesystem"

    def __init__(self, agent_root: Path) -> None:
        """Construct a FilesystemGoalBackend for agent_root.

        Side-effect-free: no filesystem I/O during construction.
        The agent directory need not exist at construction time (reactive
        agents that have no goal.md are valid and goal_text() returns '').

        Args:
            agent_root: absolute path to the agent's root directory.
                Must be absolute (or convertible to absolute via Path.resolve()).
                Paths containing '..' are rejected with ValueError.

        Raises:
            ValueError: when agent_root resolves to a relative path or
                when the raw value contains '..' path components.
        """
        # Resolve to absolute so all subsequent path operations are unambiguous.
        resolved = Path(agent_root).resolve()
        if not resolved.is_absolute():
            raise ValueError(
                f"FilesystemGoalBackend requires an absolute agent_root; "
                f"got {agent_root!r}"
            )
        # Belt-and-suspenders: reject raw '..' components even after resolve()
        # so callers get a clear error rather than silently accessing a
        # different directory.
        raw = Path(agent_root)
        for part in raw.parts:
            if part == "..":
                raise ValueError(
                    f"FilesystemGoalBackend: agent_root contains '..' component: "
                    f"{agent_root!r}"
                )
        self._agent_root = resolved
        self._goal_path = resolved / "goal.md"
        self._history_path = resolved / "goal_history.jsonl"
        self._archive_dir = resolved / "goal_archive"
        self._lock_path = resolved / ".goal.lock"

    # ──────────────────────────────────────────────────────────────
    # Internal: exclusive file lock

    @contextmanager
    def _goal_lock(self) -> Iterator[None]:
        """Acquire exclusive advisory lock on .goal.lock for the duration.

        This is the filesystem serialization primitive — equivalent to a SQL
        transaction in a Postgres backend. Both goal.md writes and
        goal_history.jsonl appends that must be atomic happen under this lock.

        The lock file is created if absent. The lock is released in finally.
        """
        self._agent_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ──────────────────────────────────────────────────────────────
    # Internal: history helpers

    def _make_history_event(self, ts: str, event: str, **fields: Any) -> dict[str, Any]:
        """Build a history event dict with 'ts' first and 'event' second.

        Ensures ts-first key order in the JSON output regardless of how
        callers construct the remaining fields (spec/40 MUST 8 analogue).
        """
        entry: dict[str, Any] = {"ts": ts, "event": event}
        entry.update(fields)
        return entry

    def _append_history_prose(self, goal: Goal, entry: str, today: str) -> None:
        """Append a timestamped prose line to the goal body ## History section.

        Mutates goal.body in-place. The GoalManager's _append_history method
        has the same logic; this copy lives here so apply_transition() can
        update the prose under the file lock without requiring a GoalManager
        instance.
        """
        history_marker = "## History"
        if history_marker not in goal.body:
            goal.body = goal.body.rstrip() + f"\n\n{history_marker} (auto-appended)\n"
        goal.body = goal.body.rstrip() + f"\n- {today} — {entry}"

    def _write_goal(self, goal: Goal) -> None:
        """Serialize and atomic_write goal.md (no lock — caller holds lock)."""
        fm = build_goal_frontmatter(goal)
        post = frontmatter.Post(goal.body, **fm)
        atomic_write(self._goal_path, frontmatter.dumps(post) + "\n")

    def _append_jsonl(self, event: dict[str, Any]) -> None:
        """Serialize event and append to goal_history.jsonl (caller holds lock)."""
        line = json.dumps(event)
        atomic_append_jsonl(self._history_path, line)

    # ──────────────────────────────────────────────────────────────
    # Protocol: load / save

    def load_goal(self, agent_id: str) -> Goal:  # noqa: ARG002 (agent_id unused for filesystem)
        """Deserialize and validate goal.md. Return a Goal.

        agent_id is unused in the filesystem impl (agent_root was set at
        construction time). The parameter exists for Protocol conformance.

        Raises:
            AtomicAgentsError: when goal.md is absent.
            GoalCorrupted: when goal.md is unparseable.
            SchemaValidationError: when frontmatter is invalid.
        """
        if not self._goal_path.is_file():
            raise AtomicAgentsError(f"No goal.md at {self._goal_path}")
        try:
            parsed = frontmatter.load(self._goal_path)
        except Exception as e:
            raise GoalCorrupted(f"goal.md unparseable: {e}") from e

        meta = dict(parsed.metadata)
        # Shared validator — identical acceptance criteria to GoalManager.load(),
        # including the blocked_by type + referential-integrity checks. A corrupt
        # dependency graph must not load silently through the backend path.
        validate_goal(meta)

        sub_goals = [
            SubGoal(
                id=sg["id"],
                label=sg.get("label", ""),
                status=sg.get("status", "pending"),
                assigned=sg.get("assigned"),
                deadline=str(sg["deadline"]) if sg.get("deadline") else None,
                blocked_by=sg.get("blocked_by"),
                completed=str(sg["completed"]) if sg.get("completed") else None,
                output=sg.get("output"),
                body=sg.get("body"),
                acceptance_criteria=list(sg.get("acceptance_criteria") or []),
            )
            for sg in meta.get("sub_goals", [])
        ]

        return Goal(
            schema_version=int(meta["schema_version"]),
            active=bool(meta["active"]),
            intent=str(meta["intent"]),
            priority=str(meta["priority"]),
            created=str(meta["created"]),
            last_progress_check=str(meta["last_progress_check"]),
            success_criteria=list(meta["success_criteria"]),
            sub_goals=sub_goals,
            deadline=str(meta["deadline"]) if meta.get("deadline") else None,
            parent_goal=meta.get("parent_goal"),
            related_atomic_notes=list(meta.get("related_atomic_notes", [])),
            related_decisions=list(meta.get("related_decisions", [])),
            related_canon_pages=list(meta.get("related_canon_pages", [])),
            body=parsed.content,
        )

    def save_goal(self, agent_id: str, goal: Goal) -> None:  # noqa: ARG002
        """Persist the Goal to goal.md verbatim.

        Write-what-I-give-you: does NOT mutate goal.last_progress_check.
        Uses atomic_write (temp+fsync+rename).

        This method does NOT acquire the goal lock — callers that need
        atomicity (apply_transition, archive_goal) acquire the lock themselves.
        Direct callers (GoalManager.save()) call this without a lock because
        single-session saves are safe without it.
        """
        self._write_goal(goal)

    # ──────────────────────────────────────────────────────────────
    # Protocol: atomic transition

    def apply_transition(
        self,
        agent_id: str,  # noqa: ARG002
        sub_goal_id: str,
        to_status: str,
        fields: dict[str, Any],
        history_prose: str,
        history_event: dict[str, Any],
    ) -> Goal:
        """Serialized + ordered: flip sub-goal status, write goal.md, append JSONL.

        All operations happen under an exclusive fcntl.flock on .goal.lock.
        The lock covers both the atomic_write(goal.md) and the
        atomic_append_jsonl(goal_history.jsonl), serializing and ORDERING them
        (goal.md first, JSONL second). This is NOT crash-rollback: a crash
        between the two writes is permissible and leaves goal.md updated with no
        audit line (recoverable). The invariant (spec/41 MUST 6) is only that the
        reverse never happen — no JSONL audit line for an un-written goal.md
        state. A Postgres backend wraps this in a SQL transaction (with rollback).

        history_event must have "ts" and "event" keys; the backend enforces
        ts-first order in the serialized JSON via _make_history_event.

        Raises:
            SchemaValidationError: when to_status is not in VALID_SUB_GOAL_STATUSES
                — validated BEFORE any write so a goal.md the backend cannot
                reload is never persisted (fail-closed, no write-time/read-time
                asymmetry).
            AtomicAgentsError: when sub_goal_id is not found.
        """
        # Fail-closed: reject an unknown to_status BEFORE acquiring the lock or
        # writing anything. Without this, apply_transition() could durably persist
        # a goal.md that the backend's own load_goal() would reject — a write/read
        # validation asymmetry. The manager path only passes typed statuses today,
        # but this is the public Protocol primitive #448 wiring + alternate
        # backends call directly.
        if to_status not in VALID_SUB_GOAL_STATUSES:
            raise SchemaValidationError(
                f"to_status must be one of {sorted(VALID_SUB_GOAL_STATUSES)}; "
                f"got {to_status!r}"
            )
        today = date.today().isoformat()
        ts = history_event.get("ts") or datetime.now().astimezone().isoformat()
        event_name = history_event.get("event", "transition")

        # Build additional fields (everything except ts and event)
        extra_fields = {
            k: v for k, v in history_event.items() if k not in ("ts", "event")
        }

        with self._goal_lock():
            goal = self.load_goal(agent_id)

            # Find and update the sub-goal
            sg = next((s for s in goal.sub_goals if s.id == sub_goal_id), None)
            if sg is None:
                raise AtomicAgentsError(
                    f"sub_goal not found: {sub_goal_id!r} in {self._goal_path}"
                )
            sg.status = to_status
            # `to_status` is the authoritative status channel and was already
            # enum-validated above. Never let the free-field `fields` channel
            # overwrite sg.status — a fields={"status": <invalid>} would reopen
            # the exact write-time/read-time validation asymmetry the to_status
            # guard closes (it would persist a goal.md load_goal() rejects).
            for k, v in fields.items():
                if k == "status":
                    continue
                if hasattr(sg, k):
                    setattr(sg, k, v)

            # Append prose to goal body
            self._append_history_prose(goal, history_prose, today)

            # Build ts-first event dict
            structured_event = self._make_history_event(ts, event_name, **extra_fields)

            # Write goal.md and append JSONL — both under the lock
            self._write_goal(goal)
            self._append_jsonl(structured_event)

        return goal

    # ──────────────────────────────────────────────────────────────
    # Protocol: history

    def append_history_event(
        self,
        agent_id: str,
        event: dict[str, Any],  # noqa: ARG002
    ) -> None:
        """Append one structured event to goal_history.jsonl.

        The backend enforces ts-first key ordering. This method ALWAYS acquires
        the goal lock before appending, because an event payload can exceed
        PIPE_BUF (4096 bytes) — e.g. outcome explanations — so an unlocked
        O_APPEND write could produce a torn line. The lock is unconditional;
        large payloads are the WHY, not a conditional trigger.
        """
        ts = event.get("ts") or datetime.now().astimezone().isoformat()
        event_name = event.get("event", "event")
        extra_fields = {k: v for k, v in event.items() if k not in ("ts", "event")}
        structured_event = self._make_history_event(ts, event_name, **extra_fields)

        # Always acquire the goal lock for history appends to avoid PIPE_BUF
        # torn-line risk when event payloads are large (e.g., outcome explanations
        # can exceed 4096 bytes). The lock overhead is negligible vs. the
        # correctness guarantee.
        with self._goal_lock():
            self._append_jsonl(structured_event)

    # ──────────────────────────────────────────────────────────────
    # Protocol: archive

    def archive_goal(
        self,
        agent_id: str,
        reason: str = "completed",  # noqa: ARG002
    ) -> str:
        """Archive the active goal to goal_archive/ with crash safety.

        Implements the three behavioral MUSTs (spec/41 MUST 7/8/9):
        MUST 7 (no data loss): archive file is written BEFORE goal.md is unlinked.
        MUST 8 (collision-safe): numeric suffix loop is race-free under the lock.
        MUST 9 (idempotency on retry-after-unlink): if goal.md is absent (a prior
            partial run completed the unlink step) AND at least one archive file
            is present, return the most-recently-modified archive slug rather than
            raising. This is a best-effort retry guard, not a per-goal lookup —
            with no goal.md, the intent slug cannot be reconstructed, so the
            newest archive is returned. See spec/41 MUST 9. (Multi-goal exactness
            is tracked as a follow-up; the common single-goal retry is correct.)

        The entire operation runs under the goal lock so the suffix loop is
        race-free (no TOCTOU window between .exists() check and atomic_write).
        """
        today = date.today().isoformat()

        with self._goal_lock():
            # MUST 9 (idempotency): if goal.md is absent, look for an existing
            # archive rather than raising. Covers the retry-after-unlink path.
            if not self._goal_path.is_file():
                if self._archive_dir.exists():
                    # Find any archive file whose base name starts with today's date
                    # or any date (we can't know the exact slug without the intent).
                    # Return the most recently modified one as the idempotent result.
                    # Secondary sort on name so coarse-mtime ties (two archives
                    # written in the same filesystem tick) resolve deterministically
                    # rather than relying on OS-arbitrary glob order.
                    existing = sorted(
                        self._archive_dir.glob("*.md"),
                        key=lambda p: (p.stat().st_mtime, p.name),
                        reverse=True,
                    )
                    if existing:
                        return existing[0].stem
                raise AtomicAgentsError(
                    f"No active goal to archive at {self._goal_path}"
                )

            goal = self.load_goal(agent_id)

            self._archive_dir.mkdir(parents=True, exist_ok=True)
            intent_slug = re.sub(r"[^a-z0-9]+", "_", goal.intent.lower()).strip("_")[
                :60
            ]
            base_name = f"{today}_{intent_slug}"
            archive_path = self._archive_dir / f"{base_name}.md"

            # MUST 8: collision-safe suffix loop (race-free under lock)
            # Check for existing archive with same base name before creating new
            counter = 0
            while archive_path.exists():
                counter += 1
                archive_path = self._archive_dir / f"{base_name}_{counter}.md"

            # Mark inactive + record in body history before writing archive
            goal.active = False
            today_str = date.today().isoformat()
            # Bump last_progress_check to the archive day, matching
            # GoalManager.archive()'s last_progress_check=today behavior.
            #
            # NOT a byte-for-byte match with GoalManager.archive(): this backend
            # serializes via build_goal_frontmatter(), which PRESERVES the
            # optional fields (deadline, parent_goal, related_atomic_notes,
            # related_decisions, related_canon_pages) that GoalManager.archive()
            # drops by building an explicit dict. That is an intentional
            # data-loss fix, not a regression — the backend never silently
            # discards goal metadata on archive. Convergence of GoalManager
            # onto build_goal_frontmatter is tracked for the #448 runtime-wiring
            # PR. The zero-behavior-change guarantee of this arc covers the live
            # GoalManager path (untouched), not this not-yet-wired backend.
            goal.last_progress_check = today_str
            self._append_history_prose(goal, f"goal archived ({reason})", today_str)

            # MUST 7: write archive FIRST, then unlink goal.md
            fm = build_goal_frontmatter(goal)
            fm["archived_at"] = today_str
            fm["archive_reason"] = reason
            post = frontmatter.Post(goal.body, **fm)
            atomic_write(archive_path, frontmatter.dumps(post) + "\n")

            # Remove goal.md only after archive is safely written
            self._goal_path.unlink()

        return archive_path.stem

    def list_archived(self, agent_id: str) -> list[str]:  # noqa: ARG002
        """Return archive slugs (filenames without .md extension), sorted.

        Returns [] when goal_archive/ does not exist (common case for agents
        that have never archived a goal). MUST NOT raise FileNotFoundError.
        """
        if not self._archive_dir.exists():
            return []
        return sorted(p.stem for p in self._archive_dir.glob("*.md"))

    # ──────────────────────────────────────────────────────────────
    # Protocol: schema version

    def read_schema_version(self, agent_id: str) -> int | None:  # noqa: ARG002
        """Return schema_version from goal.md frontmatter, or None if absent.

        NOTE: This version counter is goal-layer-specific. It is INDEPENDENT
        of the vault-wide memory/wiki migration version counter.
        DO NOT register goal.md as a MigratableUnit — migration/types.py
        Literal is locked to ['memory', 'wiki'] and goal.md is not a content
        migration target.

        Returns:
            int schema_version when goal.md is present, parseable, and carries
            a schema_version key. The value is coerced to int so the declared
            int | None contract holds even if the frontmatter wrote it as a
            string (e.g. ``schema_version: "1"``).
            None when goal.md is absent (reactive agents without a goal).
            Returning None (not CURRENT_GOAL_SCHEMA_VERSION) when absent
            distinguishes "not present" from "at current version".

        Raises:
            GoalCorrupted: when goal.md is present but unparseable, OR when the
                schema_version key is present but not coercible to int.
        """
        if not self._goal_path.is_file():
            return None
        try:
            parsed = frontmatter.load(self._goal_path)
        except Exception as e:
            raise GoalCorrupted(
                f"goal.md unparseable in read_schema_version: {e}"
            ) from e
        raw = parsed.metadata.get("schema_version")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError) as e:
            raise GoalCorrupted(
                f"goal.md schema_version is not an integer: {raw!r}"
            ) from e

    # ──────────────────────────────────────────────────────────────
    # Protocol: prompt slice

    def goal_text(self, agent_id: str) -> str:  # noqa: ARG002
        """Return raw text of goal.md for prompt assembly. Returns '' if absent.

        Side-effect-free read — no parsing, no validation, no mutation.
        Same behavior as the current goal_path.read_text() in profile/filesystem.py
        (zero behavior change for agents without goal.md).
        """
        if not self._goal_path.is_file():
            return ""
        return self._goal_path.read_text(encoding="utf-8")

    # ──────────────────────────────────────────────────────────────
    # Protocol: export (spec/40 Exportable)

    def export(self, query: Any = None) -> GoalExport:  # noqa: ARG002
        """Export goal state as a canonical GoalExport.

        Read ordering: goal.md first (authoritative state), then
        goal_history.jsonl, then archives. CRLF → LF normalized (MUST 5).

        Tier A (filesystem): bytes are read via read_bytes() and CRLF/BOM-
        normalized; history lines are NOT re-serialized through json.dumps()
        (preserving insertion-order key ordering). History lines are also
        newline-terminated — a final line lacking a trailing "\\n" (only
        reachable via a hand-edited or alternate-backend file; atomic_append_jsonl
        always terminates lines) is exported with one appended, so export is
        line-normalized rather than strict byte-for-byte passthrough.

        See GoalExport docstring for the snapshot-consistency acknowledgment
        (spec/40 MUST 7).
        """
        # --- goal.md bytes ---
        goal_md_bytes = b""
        if self._goal_path.is_file():
            raw = self._goal_path.read_bytes()
            goal_md_bytes = _normalize_crlf(raw)

        # --- goal_history.jsonl bytes (CRLF/BOM-normalized + newline-terminated;
        #     key order preserved, NOT re-serialized through json.dumps) ---
        history_records_with_bytes: list[bytes] = []
        if self._history_path.is_file():
            raw_history = self._history_path.read_bytes()
            # Split into lines; normalize CRLF; keep non-empty lines
            for raw_line in raw_history.splitlines(keepends=True):
                line = _normalize_crlf(raw_line)
                if line.strip():
                    # Ensure trailing newline
                    if not line.endswith(b"\n"):
                        line = line + b"\n"
                    history_records_with_bytes.append(line)

        # --- archived goals ---
        archived_goals_with_bytes: list[tuple[str, bytes]] = []
        if self._archive_dir.exists():
            # glob("*.md") already excludes atomic_write .tmp temp files (they end
            # in a different suffix), so no extra .tmp filter is needed here.
            for p in sorted(self._archive_dir.glob("*.md")):
                raw = p.read_bytes()
                archived_goals_with_bytes.append((p.stem, _normalize_crlf(raw)))

        return GoalExport(
            goal_md_bytes=goal_md_bytes,
            history_records_with_bytes=history_records_with_bytes,
            archived_goals_with_bytes=archived_goals_with_bytes,
            backend_id=self.backend_id,
            scope=str(self._agent_root),
        )

    def export_all(self) -> GoalExport:
        """Convenience wrapper — unbounded export."""
        return self.export(query=None)

    # ──────────────────────────────────────────────────────────────
    # Protocol: capabilities

    def capabilities(self) -> GoalCapabilities:
        """Return GoalCapabilities for this backend."""
        return GoalCapabilities(
            backend_id="filesystem",
            supports_canonical_export=True,
            supports_archive=True,
            supports_history_query=True,
        )


# ──────────────────────────────────────────────────────────────────
# Internal helpers


def _normalize_crlf(data: bytes) -> bytes:
    """Normalize CRLF → LF and strip BOM (spec/40 MUST 5)."""
    # Strip UTF-8 BOM if present
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    # CRLF → LF
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

"""FilesystemGoalBackend — directory-tree reference implementation (spec/41).

This is the default backend for single-host deployments. It wraps the same
on-disk shape GoalManager has used since the framework's first goal support:

Standing goal (backward-compat, unchanged):
    <agent_root>/goal.md            — active goal (frontmatter + markdown body)
    <agent_root>/goal_history.jsonl — structured history events (append-only)
    <agent_root>/goal_archive/      — archived goals (one .md per archived goal)
    <agent_root>/.goal.lock         — exclusive advisory lock sidecar

Addressed run-goals (spec/41 #642 multi-goal addendum):
    <agent_root>/goals/<goal_id>/goal.md
    <agent_root>/goals/<goal_id>/goal_history.jsonl
    <agent_root>/goals/<goal_id>/goal_archive/
    <agent_root>/goals/<goal_id>/.goal.lock

goals/ is a RESERVED directory name within agent_root (spec/41 #642 addendum,
§"goals/ reserved directory"). Do not create goals/ manually — list_goals() and
the export() guard interpret its contents as framework-managed run-goal state.

A failed create_goal() (directory created, goal.md not written due to a
pre-probe failure or lock contention) may leave an empty goals/<goal_id>/
directory. This is inert (list_goals() skips directories without goal.md)
but accumulates over time. Cleanup is out of scope for this PR; see #643.

Zero behavior change for existing reactive/hybrid agents: construction is
side-effect-free (no filesystem I/O in __init__), goal_text() returns ''
when goal.md is absent, list_archived() returns [] when goal_archive/ is
absent. Agents without goal.md pass doctor.check_goal_backend with a PASS
note (not a SKIP — matching the corpus_backend pattern for agents without
a corpus).

Ordering contract for apply_transition():
    The method acquires an exclusive advisory lock (fcntl.flock on
    <agent_root>/.goal.lock for the standing goal, or
    <agent_root>/goals/<goal_id>/.goal.lock for an addressed goal) before the
    read-modify-write sequence. Both the goal.md atomic_write AND the
    goal_history.jsonl append happen under this lock, serialized and ordered:
    goal.md is written FIRST, the JSONL audit line second. Per-goal lock
    granularity: concurrent run-goals on different goal_ids never block.
    The lock guarantees serialization + ordering, NOT crash-rollback — a crash
    between the two writes is permissible and leaves goal.md updated with no
    audit line (recoverable, retry-safe). The conformance requirement (spec/41
    MUST 6) is only that the reverse never occur: a JSONL audit line MUST NOT
    exist for a goal.md state that was not written. A Postgres backend wraps
    the same sequence in a SQL transaction, which additionally rolls back on
    failure. The lock is released in a finally block.

archive_goal() also acquires the goal lock so the collision-suffix loop is
race-free (no TOCTOU window between .exists() check and atomic_write).

spec/40 addendum: GoalBackend composes the Exportable Protocol at definition
time (not retrofitted). FilesystemGoalBackend.capabilities() returns
GoalCapabilities(backend_id='filesystem', supports_canonical_export=True,
supports_archive=True, supports_history_query=True, supports_multi_goal=True).

spec/41 #642: FilesystemGoalBackend also implements AddressableGoalBackend
(for_goal() factory method). Callers MUST check isinstance(backend,
AddressableGoalBackend) before calling for_goal().

Import boundary (circular-import safety):
    - Imports only from ..exceptions, .._io, .types — no imports from
      ..goal (the shim) or any module that imports ..goal. This keeps
      goal/__init__.py importable without loading the LLM stack.
"""

from __future__ import annotations

import dataclasses
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
from ..exceptions import (
    AtomicAgentsError,
    GoalAlreadyExists,
    GoalConcurrentModification,
    GoalCorrupted,
    PathTraversalError,
    SchemaValidationError,
)
from .types import (
    STANDING_GOAL_ID,
    SUB_GOAL_TRANSITION_FIELDS,
    VALID_SUB_GOAL_STATUSES,
    _GOAL_ID_RE,
    Goal,
    GoalCapabilities,
    GoalExport,
    SubGoal,
    build_goal_frontmatter,
    validate_goal,
    validate_goal_id,
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
            agent_root: the agent's root directory. A relative path is resolved
                to absolute against the process cwd (matching the sibling
                filesystem backends' accept-and-resolve convention). Paths
                containing a literal '..' component are rejected with ValueError.

        Raises:
            ValueError: when the raw value contains '..' path components.
        """
        # Resolve to absolute so all subsequent path operations are unambiguous.
        # (Path.resolve() always returns an absolute path, so there is no
        # separate is_absolute() rejection — a relative agent_root is accepted
        # and resolved, like the memory/persona/corpus filesystem backends.)
        resolved = Path(agent_root).resolve()
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
        lock_path = self._require_within_root(self._lock_path, ".goal.lock")
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ──────────────────────────────────────────────────────────────
    # Symlink containment guard

    def _require_within_root(self, path: Path, label: str) -> Path:
        """Resolve ``path`` and verify it stays under the resolved agent_root.

        The single canonical containment invariant for this backend (#448 PR1).
        ``__init__`` resolves ``agent_root`` and rejects ``..`` components, but a
        symlinked ``goal.md`` / ``goal_history.jsonl`` / ``goal_archive`` /
        ``.goal.lock`` pointing OUTSIDE the resolved root would otherwise be
        followed on read/write — a perimeter escape (a write landing outside the
        agent vault). This mirrors the sibling backends'
        ``FilesystemOutcomeBackend._runs_root()`` and
        ``FilesystemJournalBackend._journal_dir()`` guards (resolve + verify
        ``is_relative_to``), consolidated into ONE helper applied at every I/O
        boundary rather than per-path point-checks (MEMORY.md
        feedback_containment_reframe_not_whackamole).

        Within-vault integrity (a writer who can already plant a symlink INSIDE
        the resolved agent_root) is OUT of scope — that actor is inside the trust
        boundary and could write goal.md directly; adversarial/multi-host
        deployments use a real-authz backend (the spec/44 trust-model ruling).
        The defended class is the PERIMETER: a write must never escape the vault.

        Resolution failure (symlink loop / inaccessible ancestor) is folded into
        PathTraversalError rather than surfacing a raw OSError/RuntimeError —
        get_default_goal_backend() is now constructed at AtomicAgent.__init__
        (#448 PR1), so a bare resolve() crash would take down agent construction.

        Returns:
            The resolved, contained path (safe to read/write).

        Raises:
            PathTraversalError: when ``path`` resolves outside agent_root, or
                cannot be resolved at all.
        """
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                f"{label} could not be resolved (symlink loop or inaccessible "
                f"ancestor)",
                child=label,
                root=str(self._agent_root),
            ) from exc
        if not resolved.is_relative_to(self._agent_root):
            raise PathTraversalError(
                f"{label} resolves outside the agent vault (symlinked path refused)",
                child=label,
                root=str(self._agent_root),
            )
        return resolved

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
        """Serialize and atomic_write goal.md (no lock — caller holds lock).

        Validates the resulting frontmatter with the SAME validate_goal() the
        read path uses, BEFORE the durable write, so the backend can never
        persist a goal.md its own load_goal() would reject (the write/read
        validation symmetry — spec/41 MUST 6). This closes the deeper asymmetry
        the apply_transition `fields` allow-set alone does not: a permitted
        field carrying a bad value (e.g. fields={"blocked_by": "<unknown-id>"})
        would otherwise write a goal.md that load_goal() then refuses, locking
        the agent out of its own goal. SchemaValidationError here fails closed —
        nothing is written, and on the apply_transition path no JSONL audit line
        is appended for an un-written state (the write precedes the append).
        """
        goal_path = self._require_within_root(self._goal_path, "goal.md")
        fm = build_goal_frontmatter(goal)
        validate_goal(fm)
        post = frontmatter.Post(goal.body, **fm)
        atomic_write(goal_path, frontmatter.dumps(post) + "\n")

    def _append_jsonl(self, event: dict[str, Any]) -> None:
        """Serialize event and append to goal_history.jsonl (caller holds lock)."""
        history_path = self._require_within_root(
            self._history_path, "goal_history.jsonl"
        )
        line = json.dumps(event)
        atomic_append_jsonl(history_path, line)

    def _build_goal_created_event(
        self,
        ts: str,
        goal_id: str,
        *,
        intent: str,
        created: str,
        schema_version: int,
        conductor_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Build the canonical ``goal_created`` history event.

        Shared by both create_goal() write paths (the fresh-create path and the
        complete-on-partial recoverability path) so the JSONL schema can never
        diverge between them. ``conductor_run_id`` is OMITTED (not set to None)
        when None — key absence is the home-user contract the conformance suite
        asserts (TEST 79); it appears only when the conductor threads it.
        """
        fields: dict[str, Any] = {
            "goal_id": goal_id,
            "intent": intent,
            "created": created,
            "schema_version": schema_version,
        }
        if conductor_run_id is not None:
            fields["conductor_run_id"] = conductor_run_id
        return self._make_history_event(ts, "goal_created", **fields)

    def _classify_create_history(
        self, history_path: Path, goal_id: str, *, goal_path: Path
    ) -> str:
        """Classify goals/<id>/goal_history.jsonl for create_goal (caller holds lock).

        create_goal() reaches this only when the scoped goals/<id>/goal.md is
        PRESENT. The history file decides whether that goal.md is COMPLETE, a
        healable PARTIAL, or a corrupt/ambiguous state that must fail closed.
        Returns one of three string verdicts:

          'complete' — at least one ``goal_created`` event is present. The goal
            was fully created; the caller refuses with GoalAlreadyExists (no
            overwrite, no upsert).
          'empty' — the history file is ABSENT, or present but EMPTY /
            whitespace-only. This is the genuine post-goal.md-write crash shape
            (goal.md landed, the goal_created audit line never did). The caller
            SELF-HEALS: it appends the missing goal_created event and returns the
            persisted goal.
          'corrupt' — the history holds one or more parseable events but NONE is
            ``goal_created``. A goal.md carrying transition events (written by
            save_goal() / apply_transition(), which do NOT emit goal_created) with
            no creation marker is NOT a clean partial — it is an ambiguous,
            already-authored goal. The caller FAILS CLOSED (raises) rather than
            minting a spurious, mis-ordered goal_created over it. This is the
            distinction the old has-events-but-no-creation → heal predicate got
            wrong (it self-healed a legitimately-authored goal).

        FAIL CLOSED on unreadability (Principle #4 / #5): if the history file
        exists but cannot be read, or any line cannot be parsed as JSON, raise
        GoalAlreadyExists rather than guess. The operator inspects the file and
        decides. The error messages reference the SCOPED ``goal_path`` /
        ``history_path`` the operator should actually inspect — NOT the standing
        ``self._goal_path`` (create_goal() runs on the parent backend).

        Returns:
            'complete', 'empty', or 'corrupt'.

        Raises:
            GoalAlreadyExists: when the history is present but unreadable or has
                an unparseable line (completeness cannot be determined).
        """
        if not history_path.is_file():
            # goal.md present, no history file at all → genuine partial → heal.
            return "empty"
        try:
            raw = history_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GoalAlreadyExists(
                f"goal_id={goal_id!r}: goal.md is present at {goal_path} but "
                f"its goal_history.jsonl ({history_path}) could not be read to "
                f"determine whether the goal is complete ({exc}). Refusing to "
                f"complete or overwrite (fail-closed). Inspect {history_path} "
                f"manually."
            ) from exc
        saw_event = False
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as exc:
                # json.JSONDecodeError is a ValueError subclass.
                raise GoalAlreadyExists(
                    f"goal_id={goal_id!r}: goal.md is present at {goal_path} but "
                    f"its goal_history.jsonl ({history_path}) has an unparseable "
                    f"line, so completeness cannot be determined ({exc}). Refusing "
                    f"to complete or overwrite (fail-closed). Inspect "
                    f"{history_path} manually."
                ) from exc
            saw_event = True
            if isinstance(event, dict) and event.get("event") == "goal_created":
                return "complete"
        if saw_event:
            # Parseable events present, but NONE is goal_created → corrupt /
            # ambiguous (a goal authored via save_goal()/apply_transition()),
            # NOT a clean partial. Fail closed — do NOT heal.
            return "corrupt"
        # No non-blank lines (empty / whitespace-only file) → genuine partial.
        return "empty"

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
        goal_path = self._require_within_root(self._goal_path, "goal.md")
        if not goal_path.is_file():
            raise AtomicAgentsError(f"No goal.md at {goal_path}")
        try:
            parsed = frontmatter.load(goal_path)
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
                # PR2 (#581): gate_decision_id for conductor gate suspension atomics.
                gate_decision_id=sg.get("gate_decision_id"),
                # PR3 (#582): held_conflict_keys so a conflict scan reads them via
                # one load_goal() per goal (O(n_goals) loads, no per-goal JSONL parse).
                held_conflict_keys=list(sg.get("held_conflict_keys") or []),
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

        PR3 (#582, closes #655): save_goal() NOW acquires the per-goal fcntl.flock
        before writing. Before PR3 the lock was omitted on the assumption that
        single-session writes were always safe. That assumption is the #655
        single-writer gap: any direct save_goal() caller (e.g. GoalManager.save())
        that runs while a concurrent apply_transition()/archive_goal() holds the
        lock on the same goal_id could interleave a full-file rewrite with the
        transition's read-modify-write, losing one of the two writes. Acquiring the
        lock here serializes save_goal() against apply_transition() and
        archive_goal() (which already hold the same lock), eliminating that window.
        The conductor's conflict scan does NOT call save_goal() — it only reads
        (load_goal); held_conflict_keys are cleared via apply_transition(), which
        already holds the lock.
        """
        with self._goal_lock():
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
        expected_from_status: str | None = None,
        expected_decision_id: str | None = None,
        when: date | None = None,
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

        Args:
            when: the date used for the ## History prose bullet date prefix.
                Defaults to date.today() when None. Injected for clock-
                determinism in tests. Does NOT affect the JSONL `ts` field
                (which is always the real wall-clock time supplied by the caller
                via history_event['ts'] — an audit timestamp, not a date label).
                Mirrors JournalBackend.append_entry(when=...) precedent.

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
        # `when` controls the ## History prose date prefix ONLY — not the JSONL
        # `ts` field (which is a real wall-clock audit timestamp from the caller).
        today = (when or date.today()).isoformat()
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

            # Compare-and-set guard (spec/41 MUST 10): UNDER THE LOCK, after
            # load_goal() and before any write — check the current on-disk
            # status against expected_from_status. If they differ, another
            # writer moved the goal between the coordinator's first transition
            # (pending→in_progress) and this terminal transition. Reject with
            # GoalConcurrentModification (no write, no JSONL line).
            # The check is inside the lock so no concurrent write can slip
            # between the check and the durable write (no TOCTOU race).
            # Default None = no check (backward-compatible: all callers that
            # omit this parameter are unaffected). Alternate backend authors:
            # mirror this placement inside your own lock/transaction.
            if expected_from_status is not None and sg.status != expected_from_status:
                raise GoalConcurrentModification(
                    f"sub_goal '{sub_goal_id}' expected status "
                    f"'{expected_from_status}' but found '{sg.status}' on disk "
                    f"— concurrent modification detected (spec/41 MUST 10)"
                )

            # Gate-decision CAS guard (spec/41 MUST 14, PR3 #582): UNDER THE LOCK,
            # AFTER the expected_from_status check, verify gate_decision_id matches
            # the caller's expected value. Protects against a stale duplicate-resume
            # that arrives after the canonical resume has already answered and cleared
            # the decision_id. Placed AFTER the status check so a status mismatch is
            # surfaced first (clearer diagnostic when the gate is already complete).
            if expected_decision_id is not None and (
                sg.gate_decision_id != expected_decision_id
            ):
                raise GoalConcurrentModification(
                    f"sub_goal '{sub_goal_id}' expected gate_decision_id "
                    f"'{expected_decision_id}' but found "
                    f"{sg.gate_decision_id!r} on disk — stale or duplicate "
                    f"gate answer detected (spec/41 MUST 14)"
                )

            sg.status = to_status
            # The `fields` channel may ONLY set transition metadata
            # (SUB_GOAL_TRANSITION_FIELDS) — never identity (`id`/`label`) and
            # never `status`. `to_status` is the authoritative, already
            # enum-validated status channel; letting `fields` set `status` would
            # reopen the write-time/read-time validation asymmetry the to_status
            # guard closes (persisting a goal.md load_goal() rejects). Letting it
            # set `id`/`label` would silently rewrite sub-goal identity
            # mid-transition. Explicit allow-set fails closed: any field not named
            # (incl. a future SubGoal field) is ignored, not blindly setattr'd.
            for k, v in fields.items():
                if k in SUB_GOAL_TRANSITION_FIELDS:
                    setattr(sg, k, v)

            # Append prose to goal body
            self._append_history_prose(goal, history_prose, today)

            # Build ts-first event dict, then PROVE it serializes BEFORE the
            # durable goal.md write. A non-JSON-serializable history_event
            # (Path/datetime/etc.) is normal bad input, not a crash — without
            # this probe json.dumps() would raise only inside _append_jsonl()
            # AFTER goal.md is already committed, leaving a transition with no
            # audit line (a silent partial commit). Serializing here fails
            # closed: nothing is written. The real append still happens AFTER
            # goal.md (MUST 6 ordering) via _append_jsonl below — this probe
            # gates only on serializability, not write order.
            structured_event = self._make_history_event(ts, event_name, **extra_fields)
            json.dumps(structured_event)

            # Write goal.md FIRST, then append the JSONL audit line — both under
            # the lock (spec/41 MUST 6 ordering: never a JSONL line for an
            # un-written goal.md state).
            self._write_goal(goal)
            self._append_jsonl(structured_event)

        return goal

    # ──────────────────────────────────────────────────────────────
    # Protocol: history

    def append_history_event(
        self,
        agent_id: str,  # noqa: ARG002 (filesystem ignores agent_id — scoped at construction time)
        event: dict[str, Any],
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
        agent_id: str,  # noqa: ARG002 (filesystem ignores agent_id — scoped at construction time)
        reason: str = "completed",
        when: date | None = None,
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

        Args:
            when: the date to use for archived_at, last_progress_check, the
                ## History datestamp, and the archive slug date prefix. Defaults
                to date.today() when None. Compute ONCE at method entry (before
                the lock) and use consistently throughout — eliminates the
                multi-call date.today() split-clock divergence (Grok-flagged
                byte-identity bug). Backward-compatible: callers not passing
                `when` continue to work with the wall-clock default.
        """
        # Resolve the injectable clock ONCE before the lock so all date-stamped
        # fields (slug prefix, archived_at, last_progress_check, ## History prose)
        # are byte-identical under a pinned clock. Computing date.today() multiple
        # times inside the lock body would diverge across a midnight boundary.
        today = (when or date.today()).isoformat()

        with self._goal_lock():
            # Containment: refuse a symlinked goal.md / goal_archive that escapes
            # the vault BEFORE any read/glob/mkdir/write (perimeter guard).
            goal_path = self._require_within_root(self._goal_path, "goal.md")
            archive_dir = self._require_within_root(self._archive_dir, "goal_archive")
            # MUST 9 (idempotency): if goal.md is absent, look for an existing
            # archive rather than raising. Covers the retry-after-unlink path.
            if not goal_path.is_file():
                if archive_dir.exists():
                    # Return the most-recently-modified archive as the idempotent
                    # result. This match is DATE-AGNOSTIC: `when`/`today` does NOT
                    # filter it — without goal.md the intent slug is unrecoverable,
                    # so newest-by-mtime is the best-effort idempotent result per
                    # spec/41 MUST 9 (the glob below spans ALL archives, not just
                    # today's). Secondary sort on name so coarse-mtime ties (two archives
                    # written in the same filesystem tick) resolve deterministically
                    # rather than relying on OS-arbitrary glob order.
                    existing = sorted(
                        archive_dir.glob("*.md"),
                        key=lambda p: (p.stat().st_mtime, p.name),
                        reverse=True,
                    )
                    if existing:
                        return existing[0].stem
                raise AtomicAgentsError(f"No active goal to archive at {goal_path}")

            goal = self.load_goal(agent_id)

            archive_dir.mkdir(parents=True, exist_ok=True)
            intent_slug = re.sub(r"[^a-z0-9]+", "_", goal.intent.lower()).strip("_")[
                :60
            ]
            base_name = f"{today}_{intent_slug}"
            archive_path = archive_dir / f"{base_name}.md"

            # MUST 8: collision-safe suffix loop (race-free under lock)
            # Check for existing archive with same base name before creating new
            counter = 0
            while archive_path.exists():
                counter += 1
                archive_path = archive_dir / f"{base_name}_{counter}.md"

            # Mark inactive + record in body history before writing archive.
            # Use `today` (resolved once at method entry from the injectable `when`
            # param, before lock acquisition) for all date-stamped fields. Using a
            # single pre-computed string eliminates the split-clock divergence
            # (Grok-flagged bug) where multiple date.today() calls inside the lock
            # could disagree across a midnight boundary (slug date vs archived_at
            # vs history prose). (#483 PR1: clock injection — `when` param above.)
            goal.active = False
            # Bump last_progress_check to the archive day, matching
            # GoalManager.archive()'s last_progress_check=today behavior.
            goal.last_progress_check = today
            self._append_history_prose(goal, f"goal archived ({reason})", today)

            # MUST 7: write archive FIRST, then unlink goal.md
            fm = build_goal_frontmatter(goal)
            fm["archived_at"] = today
            fm["archive_reason"] = reason
            post = frontmatter.Post(goal.body, **fm)
            atomic_write(archive_path, frontmatter.dumps(post) + "\n")

            # Remove goal.md only after archive is safely written (guarded local)
            goal_path.unlink()

        return archive_path.stem

    def list_archived(self, agent_id: str) -> list[str]:  # noqa: ARG002
        """Return archive slugs (filenames without .md extension), sorted.

        Returns [] when goal_archive/ does not exist (common case for agents
        that have never archived a goal). MUST NOT raise FileNotFoundError.
        """
        archive_dir = self._require_within_root(self._archive_dir, "goal_archive")
        if not archive_dir.exists():
            return []
        return sorted(p.stem for p in archive_dir.glob("*.md"))

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
        goal_path = self._require_within_root(self._goal_path, "goal.md")
        if not goal_path.is_file():
            return None
        try:
            parsed = frontmatter.load(goal_path)
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
        goal_path = self._require_within_root(self._goal_path, "goal.md")
        if not goal_path.is_file():
            return ""
        return goal_path.read_text(encoding="utf-8")

    # ──────────────────────────────────────────────────────────────
    # Protocol: export (spec/40 Exportable)

    def export(self, query: Any = None) -> GoalExport:  # noqa: ARG002
        """Export goal state as a canonical GoalExport.

        FAIL-LOUD GUARD (spec/41 #642, first operation): if the agent has any
        addressed run-goals (goals/<id>/goal.md present), export() MUST raise
        before reading any bytes. The guard never silently returns a partial
        snapshot that drops run-goal state.

        The predicate requires at least one valid goals/*/goal.md — an empty
        goals/ directory (cleanup debris) does NOT trigger the guard (conformance
        test: export() on an agent with empty goals/ must NOT raise).

        See issue #643 for the multi-goal export extension.

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
        # FAIL-LOUD GUARD — FIRST operation, before any bytes are read.
        # Predicate: goals/ dir exists AND has at least one CONTAINED
        # goals/*/goal.md. An empty goals/ directory (partial create_goal()
        # debris) is NOT grounds for the guard (home-user safety: a stray empty
        # goals/ dir never blocks export). Only a genuine addressed goal with a
        # committed goal.md triggers.
        #
        # Containment consistency (Codex #3): the guard fires only on addressed
        # goals that actually live within this vault. An escaping symlinked
        # goals/ directory (or an individual goals/<id> whose goal.md resolves
        # outside the vault root) is NOT "addressed run-goal state within this
        # vault" — for_goal()/list_goals() would refuse/skip it, so it must not
        # block export here either. The guard stays fail-loud for genuinely-
        # contained addressed goals.
        goals_dir = self._agent_root / "goals"
        try:
            self._require_within_root(goals_dir, "goals/")
            goals_dir_is_dir = goals_dir.is_dir()
        except PathTraversalError:
            goals_dir_is_dir = False
        addressed_goal_present = False
        if goals_dir_is_dir:
            for candidate in goals_dir.glob("*/goal.md"):
                if not candidate.is_file():
                    continue
                try:
                    self._require_within_root(candidate, "goals/<id>/goal.md")
                except PathTraversalError:
                    # Escaping symlinked goals/<id> — not within this vault; skip.
                    continue
                addressed_goal_present = True
                break
        if addressed_goal_present:
            raise AtomicAgentsError(
                f"export() refused: agent at {self._agent_root} has addressed "
                f"run-goals under goals/. Exporting a multi-goal agent without "
                f"including all run-goal state would silently drop data. "
                f"Both whole-agent export AND standing-goal export are blocked "
                f"while addressed run-goals exist (calling export() on a "
                f"standing-scoped backend re-runs this same guard). "
                f"Multi-goal export is tracked in issue #643. "
                f"To export a single addressed run-goal in the meantime, use "
                f"for_goal(<run_goal_id>).export()."
            )

        # Containment: refuse symlinked goal.md / goal_history.jsonl / goal_archive
        # that escape the vault before reading their bytes into the export.
        goal_path = self._require_within_root(self._goal_path, "goal.md")
        history_path = self._require_within_root(
            self._history_path, "goal_history.jsonl"
        )
        archive_dir = self._require_within_root(self._archive_dir, "goal_archive")

        # --- goal.md bytes ---
        goal_md_bytes = b""
        if goal_path.is_file():
            raw = goal_path.read_bytes()
            goal_md_bytes = _normalize_crlf(raw)

        # --- goal_history.jsonl bytes (CRLF/BOM-normalized + newline-terminated;
        #     key order preserved, NOT re-serialized through json.dumps) ---
        history_records_with_bytes: list[bytes] = []
        if history_path.is_file():
            raw_history = history_path.read_bytes()
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
        if archive_dir.exists():
            # glob("*.md") already excludes atomic_write .tmp temp files (they end
            # in a different suffix), so no extra .tmp filter is needed here.
            for p in sorted(archive_dir.glob("*.md")):
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
            supports_multi_goal=True,
        )

    # ──────────────────────────────────────────────────────────────────
    # Protocol: multi-goal addressing (spec/41 #642)

    def create_goal(
        self,
        agent_id: str,  # noqa: ARG002 (filesystem ignores agent_id — scoped at construction time)
        goal_id: str,
        goal: Goal,
        when: date | None = None,
    ) -> Goal:
        """Atomically create a new addressed run-goal at goals/<goal_id>/.

        Collision semantics — refuse-on-COMPLETE, heal-only-the-genuine-PARTIAL,
        fail-closed on everything ambiguous (the recoverability refinement,
        spec/41 MUST 11). The complete decision table, all UNDER the per-goal lock:
          - goal.md ABSENT → create normally (write goal.md, then append the
            goal_created event).
          - goal.md PRESENT, goal_history.jsonl ABSENT or EMPTY / whitespace-only
            → a genuine half-created state (goal.md landed, the audit line never
            did — the rare post-goal.md I/O-failure outcome). create_goal()
            SELF-HEALS it: it appends the missing goal_created event and returns
            the goal, making the operation idempotent over a partial create.
            **The persisted goal.md is AUTHORITATIVE on this path** — the supplied
            `goal` arg's body is NOT re-written; the goal_created event is built
            ENTIRELY from the persisted goal (intent/created/schema_version AND
            conductor_run_id all come from the persisted goal — persisted wins for
            every field).
          - goal.md PRESENT, goal_history.jsonl contains a goal_created event →
            the goal is COMPLETE → raise GoalAlreadyExists (no overwrite, no
            upsert).
          - goal.md PRESENT, goal_history.jsonl contains events but NO
            goal_created event → FAIL CLOSED: raise GoalAlreadyExists. A goal with
            transition events but no creation marker was authored via
            save_goal()/apply_transition() (which do NOT emit goal_created); it is
            corrupt/ambiguous, NOT a clean partial. Healing it would mint a
            spurious, mis-ordered goal_created over a legitimately-authored goal.
          - goal.md PRESENT but goal_history.jsonl unreadable/unparseable →
            FAIL CLOSED: raise GoalAlreadyExists (never silently complete or
            overwrite an ambiguous state). See _classify_create_history().

        Mirrors apply_transition()'s MUST 6 ordering: json.dumps pre-probe BEFORE
        any goal.md/JSONL write. The completeness check and the
        complete-or-refuse decision happen UNDER THE SAME per-goal lock as the
        write (TOCTOU-safe).

        Write sequence (steps 5+ hold the per-goal lock; steps 1–4 are pre-lock):
          1. validate_goal_id(goal_id) — charset + reserved-name (pre-lock).
          2. Stamp goal.created from `when` (or date.today()) on a COPY (pre-lock).
          3. Build the scoped backend via for_goal(goal_id) — applies the
             canonical containment guard to goals/<goal_id> (pre-lock).
          4. Build the goal_created event for the fresh-create path (pre-lock).
          5. Stray-file guard: refuse if goals/<goal_id> exists as a regular FILE
             (not a directory) — a clear GoalAlreadyExists instead of the raw
             FileExistsError the lock's lazy mkdir would otherwise leak (pre-lock).
          6. Acquire the scoped backend's per-goal lock (lazily mkdirs
             goals/<goal_id>/).
          7. Two-leaf pre-verification: _require_within_root on BOTH the goal.md
             leaf AND the goal_history.jsonl leaf BEFORE the goal.md write, so a
             planted symlinked history leaf is an all-or-nothing REFUSE (no
             goal.md committed).
          8. Branch on goal.md presence (complete-on-partial logic above).
          9. Fresh-create path: json.dumps pre-probe → _write_goal() (goal.md
             FIRST) → _append_jsonl() (goal_history.jsonl SECOND).

        Containment (MEMORY feedback_containment_reframe_not_whackamole — ONE
        canonical invariant applied at EVERY I/O boundary, not per-path point
        checks): the writes are routed through the for_goal(goal_id)-scoped
        backend's _goal_lock()/_write_goal()/_append_jsonl(), each of which calls
        _require_within_root on its leaf (goal.md AND goal_history.jsonl) before
        opening it; step 7 additionally verifies both leaves UP FRONT so the
        goal.md write never lands when a sibling history leaf escapes. This closes
        the perimeter escape a pre-planted symlinked
        goals/<goal_id>/goal_history.jsonl (append mode FOLLOWS symlinks) would
        otherwise open — the goal_created audit line can never be written outside
        the vault (Principle #5). for_goal() additionally resolves-and-verifies
        the goals/<goal_id> directory node itself, so a symlinked goal dir is
        refused before the scoped backend is even constructed.

        Single-writer assumption (Codex #1; #655 closed in #582 PR3):
        create_goal()'s atomicity holds against ALL other writers on the same
        goal_id — apply_transition(), archive_goal(), create_goal(), AND
        save_goal() all share the per-goal lock, so they serialize. As of #582
        PR3, save_goal() acquires self._goal_lock() before writing (closing #655,
        the prior single-writer gap), so a concurrent save_goal() can no longer
        interleave with the create window. Serializing multiple conductor runs on
        one goal_id (conflict keys / queue-behind-decision) is the conductor's
        concern (spec/50 conflict-scan lease).

        Raises:
            ValueError: invalid charset or reserved goal_id (pre-lock, no I/O).
            GoalAlreadyExists: goal.md present and COMPLETE; goal.md present with
                history events but no goal_created marker (corrupt/ambiguous,
                fail-closed); goals/<goal_id> is a stray regular file (or a file
                raced into place between the pre-lock check and lock mkdir); or
                goal_history.jsonl is unreadable/unparseable so completeness
                cannot be determined (fail-closed).
            SchemaValidationError: the fresh-create validate path — _write_goal()
                runs validate_goal() before the durable write and rejects an
                invalid Goal.
            GoalCorrupted: the self-heal path reloads the persisted goal.md via
                load_goal(), which raises GoalCorrupted if it is unparseable.
            PathTraversalError: a symlinked goal dir or history/goal.md leaf
                escapes the vault perimeter.
        """
        # Step 1: validate goal_id BEFORE any I/O (pre-lock).
        # validate_goal_id raises ValueError for bad charset or reserved '_standing'.
        validate_goal_id(goal_id)

        # Step 2: stamp goal.created from `when` on a COPY — do NOT mutate the
        # caller's Goal object (sibling save_goal() promises write-verbatim / no
        # caller-object mutation; create_goal() honors the same no-surprise
        # contract). This stamping is the single exception to MUST 4's
        # write-verbatim rule (spec/41 #642 addendum), but it lands on the copy
        # we persist + return, never on the caller's input. (Principle #8.)
        today = (when or date.today()).isoformat()
        goal = dataclasses.replace(goal, created=today)

        # Step 3: build the scoped backend. for_goal() applies the canonical
        # _require_within_root containment guard to goals/<goal_id> against the
        # parent root (refuses a symlinked goal dir escaping the vault) and
        # returns a backend whose _goal_lock()/_write_goal()/_append_jsonl()
        # re-verify EVERY write leaf — NOT a per-path point check. validate_goal_id
        # above already rejected STANDING_GOAL_ID, so for_goal() never routes to
        # the standing layout here.
        scoped = self.for_goal(goal_id)

        # Step 4: build the goal_created event for the fresh-create path via the
        # shared ts-first helper so this event inherits any future key-ordering/
        # normalization centralized in _make_history_event (every other writer to
        # goal_history.jsonl uses it). conductor_run_id is OMITTED (not set to
        # None) for home-user goals; key absence is what the conformance test
        # asserts. A Goal carries no conductor_run_id field today, so the getattr
        # resolves to None (omitted) — the hook is forward-compatible only.
        ts = datetime.now().astimezone().isoformat()
        conductor_run_id = getattr(goal, "conductor_run_id", None)
        goal_created_event = self._build_goal_created_event(
            ts,
            goal_id,
            intent=goal.intent,
            created=today,
            schema_version=goal.schema_version,
            conductor_run_id=conductor_run_id,
        )

        # Step 5: stray-file guard (pre-lock). If goals/<goal_id> exists as a
        # regular FILE (not a directory), the lock's lazy mkdir below would leak a
        # raw FileExistsError. Detect it and raise GoalAlreadyExists with an
        # actionable message — the goals/ tree is framework-reserved. (A symlinked
        # goal dir escaping the vault was already refused in for_goal() above.)
        goal_dir = self._agent_root / "goals" / goal_id
        if goal_dir.exists() and not goal_dir.is_dir():
            raise GoalAlreadyExists(
                f"goal_id={goal_id!r}: a non-directory already occupies "
                f"{goal_dir}. The goals/ tree is framework-reserved for addressed "
                f"run-goals; move the stray path aside (or pick a different "
                f"goal_id) before calling create_goal()."
            )

        # Steps 6+: under the scoped backend's contained per-goal lock.
        # TOCTOU-hardened stray-file guard: the pre-lock check (step 5) can be
        # raced — a regular file planted at goals/<goal_id> BETWEEN that check and
        # the lock's lazy mkdir(parents=True, exist_ok=True) makes mkdir raise a
        # raw FileExistsError (exist_ok=True only suppresses when the existing path
        # is a directory). That OSError is outside the documented Raises contract,
        # so catch it at the lock-acquire boundary and re-raise it as the same
        # GoalAlreadyExists reserved-dir refusal the pre-lock check emits. The
        # create body itself never raises FileExistsError (atomic_write uses
        # rename; os.open uses O_CREAT without O_EXCL), so wrapping the whole
        # block cannot mask an unrelated error. GoalAlreadyExists / PathTraversal
        # raised inside the body are NOT FileExistsError and pass through.
        try:
            with scoped._goal_lock():
                # Step 7: TWO-LEAF pre-verification — resolve-and-verify BOTH write
                # leaves through the canonical containment guard BEFORE the goal.md
                # write. _write_goal()/_append_jsonl() each re-verify their own
                # leaf, but doing both UP FRONT makes a planted symlinked
                # goal_history.jsonl leaf an all-or-nothing REFUSE: no goal.md is
                # committed when the sibling history leaf escapes the vault. The
                # existence check below reuses the verified goal.md path
                # (TOCTOU-safe — inside the lock).
                goal_path = scoped._require_within_root(scoped._goal_path, "goal.md")
                history_path = scoped._require_within_root(
                    scoped._history_path, "goal_history.jsonl"
                )

                # Step 8: branch on goal.md presence (the complete-on-partial
                # logic). Two concurrent create_goal() calls for the same goal_id
                # serialize on this lock; the winner proceeds, the loser sees
                # goal.md present. _classify_create_history() FAILS CLOSED (raises)
                # if the history cannot be read/parsed to decide, and reports
                # 'corrupt' (NOT 'empty'/heal) when the history holds non-creation
                # events — a legitimately-authored goal must never be mis-healed.
                if goal_path.is_file():
                    verdict = self._classify_create_history(
                        history_path, goal_id, goal_path=goal_path
                    )
                    if verdict == "complete":
                        # COMPLETE — refuse (no overwrite, no upsert).
                        raise GoalAlreadyExists(
                            f"goal_id={goal_id!r} already exists at {goal_path}. "
                            f"Use a unique goal_id or call list_goals() to "
                            f"enumerate existing goals."
                        )
                    if verdict == "corrupt":
                        # FAIL CLOSED — history holds events but no goal_created
                        # marker. A goal authored via save_goal()/apply_transition()
                        # is NOT a clean partial; healing it would mint a spurious,
                        # mis-ordered goal_created over a legitimate goal.
                        raise GoalAlreadyExists(
                            f"goal_id={goal_id!r}: goal.md is present at "
                            f"{goal_path} and its goal_history.jsonl "
                            f"({history_path}) is non-empty but holds NO "
                            f"goal_created marker — a goal with transitions and no "
                            f"creation record is corrupt/ambiguous, not a clean "
                            f"partial. Refusing to complete or overwrite "
                            f"(fail-closed). Inspect {history_path} manually."
                        )
                    # verdict == 'empty' → genuine PARTIAL — complete-on-partial
                    # recoverability (self-healing / idempotent over a partial
                    # create). The PERSISTED goal.md is AUTHORITATIVE; we only
                    # append the missing goal_created event (built ENTIRELY from
                    # the persisted goal — intent/created/schema_version AND
                    # conductor_run_id all read off the reloaded goal so "persisted
                    # wins" holds for every field) and return the persisted goal.
                    persisted = scoped.load_goal(agent_id)
                    completion_event = self._build_goal_created_event(
                        datetime.now().astimezone().isoformat(),
                        goal_id,
                        intent=persisted.intent,
                        created=persisted.created,
                        schema_version=persisted.schema_version,
                        conductor_run_id=getattr(persisted, "conductor_run_id", None),
                    )
                    # json.dumps pre-probe BEFORE the append (mirror MUST 6 order).
                    json.dumps(completion_event)
                    scoped._append_jsonl(completion_event)
                    return persisted

                # goal.md ABSENT — fresh create.
                # Step 9a: json.dumps serializability pre-probe BEFORE any goal.md/
                # JSONL write. A non-serializable value raises here (inside the
                # lock, before goal.md is touched) — no goal state is committed (an
                # empty goals/<goal_id>/ directory may remain from the lock's lazy
                # mkdir; see the partial-create debris note in the module
                # docstring). The real append still happens AFTER goal.md (MUST 6
                # ordering).
                json.dumps(goal_created_event)

                # Step 9b: write goal.md FIRST (MUST 6 ordering) — _write_goal()
                # calls _require_within_root on the goal.md leaf before
                # atomic_write.
                scoped._write_goal(goal)

                # Step 9c: append goal_created JSONL line AFTER goal.md —
                # _append_jsonl() calls _require_within_root on the
                # goal_history.jsonl leaf before atomic_append_jsonl (closes the
                # symlinked-leaf escape).
                scoped._append_jsonl(goal_created_event)
        except FileExistsError as exc:
            # A regular file raced into goals/<goal_id> between the pre-lock check
            # (step 5) and the lock's mkdir. Map the raw OSError onto the same
            # reserved-dir refusal so the Raises contract holds (no FileExistsError
            # escapes).
            raise GoalAlreadyExists(
                f"goal_id={goal_id!r}: a non-directory already occupies "
                f"{goal_dir}. The goals/ tree is framework-reserved for addressed "
                f"run-goals; move the stray path aside (or pick a different "
                f"goal_id) before calling create_goal()."
            ) from exc

        return goal

    def list_goals(self, agent_id: str) -> list[str]:  # noqa: ARG002
        """Enumerate all goal_ids for this agent. Returns sorted list.

        Includes STANDING_GOAL_ID ('_standing') when agent_root/goal.md exists.
        Includes addressed run-goals from goals/<id>/goal.md (requires goal.md
        presence — partial create_goal() directories with no goal.md are skipped).

        Filters goals/ subdirectories by:
        - is_dir() (not files like stray notes.md)
        - name matches [a-z0-9_-]{1,64} charset allow-list
        - name != STANDING_GOAL_ID (reserved sentinel, not a run-goal dir)
        - contains goals/<id>/goal.md (presence predicate — not just directory)

        Consistency guarantee (Codex #3): the goal.md presence predicate keeps
        partial-create debris (an empty goals/<id>/ directory with no goal.md)
        out of the list, AND each candidate is run through the SAME
        resolve-then-verify-under-root containment guard for_goal()/the write
        leaves use (_require_within_root). A listed id is therefore one
        for_goal(<id>) can actually open: an escaping symlinked goals/<id>
        directory whose goal.md resolves outside the vault root is SKIPPED here
        (not listed), exactly as for_goal() would refuse it with
        PathTraversalError. The goals/ directory itself is contained too — if
        agent_root/goals resolves outside the vault, it is treated as no
        addressed goals. This removes the prior list/for_goal asymmetry so
        discovery/doctor/resume never hand out a goal_id for_goal() cannot open
        (durability-consistency, Principle #13). Under the T15 trust model the
        perimeter is the vault root; the containment guard lives at the I/O
        boundary, and the enumeration now mirrors it.

        Return ordering: sorted() — '_standing' sorts before alphabetic (a-z)
        run-goal names (ASCII '_' (95) < 'a' (97)). It is NOT unconditionally
        first: a goal_id beginning with '-' (45) or a digit (48) sorts before
        '_standing'. No code depends on the position; only sorted() order is
        contractual.
        """
        result: list[str] = []

        # Standing goal: present when agent_root/goal.md is a regular file.
        standing_goal_path = self._require_within_root(self._goal_path, "goal.md")
        if standing_goal_path.is_file():
            result.append(STANDING_GOAL_ID)

        # Addressed run-goals: scan goals/ directory.
        # Contain the goals/ directory itself first (Codex #3): if
        # agent_root/goals resolves outside the vault (escaping symlink), there
        # are no addressed goals within this vault to enumerate.
        goals_dir = self._agent_root / "goals"
        try:
            self._require_within_root(goals_dir, "goals/")
            goals_dir_is_dir = goals_dir.is_dir()
        except PathTraversalError:
            goals_dir_is_dir = False
        if goals_dir_is_dir:
            for entry in goals_dir.iterdir():
                if not entry.is_dir():
                    # Skip non-directory entries (stray files in goals/).
                    continue
                name = entry.name
                # Skip the reserved sentinel.
                if name == STANDING_GOAL_ID:
                    continue
                # Charset allow-list: skip non-conforming names silently
                # (e.g. pre-existing dirs from other tools, macOS .DS_Store).
                if not _GOAL_ID_RE.match(name):
                    continue
                # Presence predicate: require goal.md, not just the directory.
                # Skips partial create_goal() debris (empty dirs).
                if not (entry / "goal.md").is_file():
                    continue
                # Containment consistency (Codex #3): apply the SAME
                # resolve-then-verify-under-root guard for_goal() uses, so a
                # listed id is one for_goal() can actually open. An escaping
                # symlinked goals/<id> whose goal.md resolves outside the vault
                # is SKIPPED here rather than listed-then-refused.
                try:
                    self._require_within_root(
                        entry / "goal.md", f"goals/{name}/goal.md"
                    )
                except PathTraversalError:
                    continue
                result.append(name)

        return sorted(result)

    def for_goal(self, goal_id: str | None) -> "FilesystemGoalBackend":
        """Return a FilesystemGoalBackend scoped to the addressed goal.

        Routing (backward-compat invariant — spec/41 #642):
        - goal_id is None or goal_id == STANDING_GOAL_ID ('_standing'):
          Returns a backend scoped to agent_root/ (the standing-goal layout
          UNCHANGED). agent_root/goal.md is the target — NOT goals/_standing/.
          This is the critical backward-compat branch: any agent that already
          has agent_root/goal.md continues to read/write it correctly.
        - Any other valid goal_id: Returns a backend scoped to
          agent_root/goals/<goal_id>/. That backend's _goal_path is
          goals/<goal_id>/goal.md, _history_path is goals/<goal_id>/
          goal_history.jsonl, _archive_dir is goals/<goal_id>/goal_archive/,
          _lock_path is goals/<goal_id>/.goal.lock — per-goal lock granularity.

        The returned backend satisfies the full GoalBackend + AddressableGoalBackend
        contract. Callers MUST check isinstance(backend, AddressableGoalBackend)
        before calling this method.

        Args:
            goal_id: the goal identifier or None (alias for '_standing').

        Raises:
            ValueError: when goal_id is not None/STANDING_GOAL_ID and fails
                charset validation.
            PathTraversalError: when goals/<goal_id> resolves outside the vault
                (a symlinked goal dir escaping the perimeter is refused before
                the scoped backend is constructed — parity with create_goal).
        """
        if goal_id is None or goal_id == STANDING_GOAL_ID:
            # Return a backend scoped to the original agent_root layout.
            # _goal_path = agent_root/goal.md (unchanged).
            # _history_path = agent_root/goal_history.jsonl (unchanged).
            # _lock_path = agent_root/.goal.lock (unchanged).
            return FilesystemGoalBackend(self._agent_root)
        # Charset validation for non-standing goal_ids.
        if not _GOAL_ID_RE.match(goal_id):
            raise ValueError(
                f"goal_id {goal_id!r} contains invalid characters or exceeds "
                f"the maximum length. Use only [a-z0-9_-] up to 64 chars."
            )
        # Containment guard — the same canonical invariant create_goal relies on
        # (MEMORY feedback_containment_reframe_not_whackamole). Resolve
        # goals/<goal_id> against the PARENT root and refuse a symlinked goal dir
        # that escapes the vault BEFORE constructing the scoped backend. Without
        # this, FilesystemGoalBackend.__init__ would resolve() the symlink and
        # re-anchor the scoped backend's containment root on the escaped target —
        # so a subsequent write via the scoped backend's leaves (each of which
        # checks against the re-anchored root) could land outside the vault. The
        # leaf may not yet exist (create_goal calls this before any mkdir);
        # _require_within_root resolves the non-existent tail against its existing
        # ancestors and still detects an escaping symlink ancestor.
        self._require_within_root(
            self._agent_root / "goals" / goal_id, f"goals/{goal_id}/"
        )
        # Return a backend scoped to goals/<goal_id>/.
        # FilesystemGoalBackend.__init__ sets:
        #   _goal_path     = goals/<goal_id>/goal.md
        #   _history_path  = goals/<goal_id>/goal_history.jsonl
        #   _archive_dir   = goals/<goal_id>/goal_archive/
        #   _lock_path     = goals/<goal_id>/.goal.lock
        # Per-goal lock granularity: concurrent run-goals never block.
        return FilesystemGoalBackend(self._agent_root / "goals" / goal_id)


# ──────────────────────────────────────────────────────────────────
# Internal helpers


def _normalize_crlf(data: bytes) -> bytes:
    """Normalize CRLF → LF and strip BOM (spec/40 MUST 5)."""
    # Strip UTF-8 BOM if present
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    # CRLF → LF
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

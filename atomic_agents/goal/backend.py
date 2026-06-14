"""GoalBackend Protocol — the contract every goal implementation satisfies.

This is one of the open protocols in the protocol-pattern series (spec/41).
It decouples the goal storage layer from the GoalManager business logic, so
alternate goal storage backends (Postgres, cloud storage) can register without
forking the framework.

Protocol method surface — HYBRID design (per arc ruling):

  Coarse persistence methods:
    load_goal()           — deserialize + validate goal.md → Goal
    save_goal()           — persist a Goal verbatim (write-what-I-give-you;
                            no side-effect mutations like last_progress_check)
    append_history_event()— append one structured event to goal_history.jsonl
    archive_goal()        — move active goal to goal_archive/ with crash safety
    list_archived()       — enumerate archive slugs
    read_schema_version() — return schema_version int (or None if absent)
    goal_text()           — cheap read-only goal.md text slice for prompt assembly
    export()              — spec/40 canonical export

  Atomic transition primitive:
    apply_transition()    — serialized + ordered: flip one sub-goal status +
                            update fields, write goal.md FIRST, then append one
                            history JSONL line — both under one exclusive lock.
                            The guarantee is ordering (goal.md before JSONL) so a
                            crash never produces an orphan audit line for a state
                            goal.md does not reflect; it is NOT crash-rollback.
                            Filesystem impl uses fcntl.flock; a Postgres impl
                            uses a SQL transaction (which does add rollback).

  Capability advertisement:
    capabilities()        — return GoalCapabilities

Pure computation stays ABOVE the Protocol in GoalManager:
  legal-transition rules, cycle detection (_would_cycle), evaluate_completion,
  next_sub_goal, status_summary, progress_report, etc.

This mirrors the MigrationBackend snapshot/restore-on-Protocol precedent (T13)
— coarse ops on the Protocol, computation in the manager layer.

See docs/spec/41-goal-backend.md for the full normative contract.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, runtime_checkable

from .types import Goal, GoalCapabilities, GoalExport


@runtime_checkable
class GoalBackend(Protocol):
    """Contract every goal backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The @runtime_checkable decorator enables
    isinstance(obj, GoalBackend) to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope: bound at construction. FilesystemGoalBackend(agent_root) operates
    on <agent_root>/goal.md and <agent_root>/goal_archive/.

    The backend is STATELESS at the Protocol level — it holds the agent_root
    path only. All in-memory state (the loaded Goal object) is managed by
    GoalManager above the Protocol.

    apply_transition() is the single-writer primitive — it flips sub-goal
    status, updates fields, and appends one history JSONL line as a serialized,
    ordered step (goal.md written before the JSONL audit line). On filesystem:
    serialized by fcntl.flock (ordering, not crash-rollback). On Postgres:
    inside a SQL transaction (which adds rollback). Dropping the lock
    reintroduces the lost-update race; reversing the write order would let a
    crash leave an orphan audit line (spec/41 MUST 6 forbids that direction).

    save_goal() is the coarse persistence op — writes a Goal verbatim.
    It does NOT mutate last_progress_check (that is GoalManager's concern,
    not the backend's). The Implementer Contract MUST 4-case write test
    asserts save_goal round-trips exactly what was passed.

    read_schema_version() is a diagnostic-only reader; goal.md is NOT a
    MigratableUnit and MUST NOT be registered with MigrationBackend.
    The version counter is goal-layer-specific, independent of the
    vault-wide memory/wiki migration version counter.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'filesystem', 'postgres'.

        Used by the registry for lookup and by diagnostic tooling. Treat as
        a backwards-compatibility surface — operator deployments may pin
        against these strings.
        """
        ...

    def load_goal(self, agent_id: str) -> Goal:
        """Deserialize and validate goal.md for agent_id. Return a Goal.

        Raises:
            AtomicAgentsError: when goal.md is absent.
            GoalCorrupted: when goal.md is unparseable or frontmatter is invalid.
            SchemaValidationError: when required fields are missing or values
                are out of range (e.g. unknown priority, unknown sub_goal status).
        """
        ...

    def save_goal(self, agent_id: str, goal: Goal) -> None:
        """Persist the Goal to goal.md verbatim.

        Write-what-I-give-you: does NOT mutate goal.last_progress_check or
        any other field. The caller (GoalManager) sets fields before calling.

        Atomic: uses temp+fsync+rename so the file is always complete on disk.

        Args:
            agent_id: the agent directory name (relative to agents_root).
            goal: the Goal to write. Mutating the Goal after this call does
                NOT affect the on-disk state.
        """
        ...

    def apply_transition(
        self,
        agent_id: str,
        sub_goal_id: str,
        to_status: str,
        fields: dict[str, Any],
        history_prose: str,
        history_event: dict[str, Any],
        expected_from_status: str | None = None,
        when: date | None = None,
    ) -> Goal:
        """Atomic transition: flip sub-goal status + write history as one durable unit.

        This is the single primitive for sub-goal state changes. It performs
        the following as a serialized, ordered sequence under one exclusive lock:
            1. Load the current Goal from disk.
            2. Find the target sub-goal by sub_goal_id.
            3. Mutate the sub-goal: set status=to_status and apply fields.
            4. Append history_prose to the goal body (## History section).
            5. atomic_write goal.md with the updated state.
            6. Append history_event (as a JSONL line) to goal_history.jsonl.

        The guarantee is ORDERING, not crash-rollback (spec/41 MUST 6): goal.md
        (step 5) is written BEFORE the JSONL audit line (step 6). A crash AFTER
        step 5 but BEFORE step 6 is permissible — it leaves goal.md updated with
        no audit line (recoverable, retry-safe). The conformance requirement is
        only that the reverse never occur: a goal_history.jsonl audit line MUST
        NOT exist for a goal.md state that was never written. On filesystem:
        steps 5 and 6 run under an exclusive file lock (fcntl.flock on a
        .goal.lock sidecar). On Postgres: inside a SQL transaction (which, unlike
        the filesystem impl, additionally rolls back on failure).

        Callers MUST NOT also call save_goal() after apply_transition() —
        apply_transition() owns the write.

        Args:
            agent_id: the agent directory name.
            sub_goal_id: the sub-goal to transition.
            to_status: the target status string (e.g. "in_progress", "complete").
            fields: optional field updates to apply to the sub-goal (e.g.
                {"completed": "2026-06-11", "output": "/path/to/artifact"}).
            history_prose: prose entry for the ## History section, e.g.
                "sub_goal `sg1` → complete". The backend appends this under
                a timestamped bullet. Must NOT include the date prefix (backend
                adds it).
            history_event: structured event dict for goal_history.jsonl. The
                backend MUST place "ts" as the first key and "event" as the
                second key regardless of the dict's insertion order.
                All other fields are passed through. When the history_event
                carries event="sub_goal_outcome_dispatched", it is the
                coordinator's terminal audit record (spec/41 MUST 6 + MUST 10).
            expected_from_status: optional compare-and-set guard (spec/41 MUST 10).
                When not None, the backend MUST, UNDER THE LOCK (after
                load_goal(), before the write), check the sub-goal's current
                on-disk status against this value. If they differ, MUST raise
                GoalConcurrentModification (no write, no JSONL line). Default
                None = no check (backward-compatible — all existing callers
                that omit this parameter are unaffected). Alternate backend
                authors: the check MUST be under the lock/transaction so a
                concurrent write cannot slip between the check and the write
                (TOCTOU guard).
            when: the date used for the ## History prose bullet date prefix
                (e.g. "- 2026-05-08 — sub_goal ..."). Defaults to date.today()
                when None. Injected for clock-determinism in tests. Does NOT
                affect the JSONL `ts` field, which is always the real wall-clock
                time supplied by the caller via history_event['ts'] (audit
                timestamp, not a date label). Mirrors JournalBackend.append_entry
                (when=...) precedent (spec/43). Backward-compatible default:
                callers not passing `when` continue to work.

        Note:
            `fields` MUST NOT carry a "status" key — `to_status` is the sole
            status channel and is enum-validated. A conforming backend ignores
            (or rejects) a "status" key in `fields` so it cannot bypass the
            enum gate (spec/41 MUST 6).

        Returns:
            The updated Goal after the transition.

        Raises:
            SchemaValidationError: when `to_status` is not a member of
                VALID_SUB_GOAL_STATUSES. Validated fail-closed, BEFORE any
                write — no partial goal.md and no orphan JSONL line are
                produced (spec/41 MUST 6). This raise is a conformance
                requirement; the conformance suite asserts it for every backend.
            GoalConcurrentModification: when expected_from_status is not None
                and the sub-goal's current on-disk status differs from
                expected_from_status — another writer moved the goal between
                the caller's lock release and re-acquisition (spec/41 MUST 10).
                No write is performed; no JSONL line is appended.
            AtomicAgentsError: when goal.md is absent.
            AtomicAgentsError: when sub_goal_id is not found in the goal.
        """
        ...

    def append_history_event(self, agent_id: str, event: dict[str, Any]) -> None:
        """Append one structured event to goal_history.jsonl.

        Used for non-transition events (e.g., outcome-dispatch audit events
        written by the GoalManager). The backend MUST place "ts" as the first
        key in the serialized JSON line and "event" as the second key.

        This method does NOT write to goal.md — prose history is a manager
        concern, managed via save_goal() or apply_transition().

        Args:
            agent_id: the agent directory name.
            event: dict with at minimum "ts" and "event" keys. Additional keys
                are serialized in insertion order.
        """
        ...

    def archive_goal(
        self, agent_id: str, reason: str = "completed", when: date | None = None
    ) -> str:
        """Archive the active goal to goal_archive/. Return the archive slug.

        Implementer Contract — three behavioral MUSTs (spec/41 MUST 7/8/9):
        MUST 7 (write ordering) — no data loss if op fails mid-way: archive file
            is written BEFORE goal.md is unlinked. A crash between write and
            unlink leaves both files present (recoverable state).
        MUST 8 (collision-safe slug) — collision-safe archive identity: if an
            archive with the same (intent_slug, date) already exists, a numeric
            suffix is appended (_1, _2, ...) until a free path is found. No prior
            archive record is silently overwritten.
        MUST 9 (idempotency) — idempotency on retry-after-unlink: if archive_goal() is called
            again and goal.md is already absent (a prior partial run completed
            the unlink step) AND at least one archive file is present, the
            implementation MUST return the most-recently-modified archive slug
            in goal_archive/ rather than raising. This prevents a hard failure
            on retry. (With no goal.md present, the intent slug cannot be
            reconstructed, so the newest archive is the idempotent result.)

        The filesystem reference impl holds the named goal lock across the
        full operation so the collision-suffix loop is race-free.

        Args:
            agent_id: the agent directory name.
            reason: archive reason string embedded in the archive frontmatter.
            when: the date to use for archived_at, last_progress_check, the
                ## History datestamp, and the archive slug date prefix. Defaults
                to date.today() when None. Injected for clock-determinism in
                tests. ALL date-stamped fields in one archive operation MUST use
                the same resolved date — no split-clock divergence (Grok-flagged
                byte-identity bug). Mirrors JournalBackend.append_entry(when=...)
                precedent (spec/43). Backward-compatible default: callers not
                passing `when` continue to work.

        Returns:
            The archive slug (filename without .md extension), e.g.
            "2026-06-11_complete_novel_first_draft".

        Raises:
            AtomicAgentsError: when no goal.md is present AND no matching
                archive already exists (truly nothing to archive).
        """
        ...

    def list_archived(self, agent_id: str) -> list[str]:
        """Return archive slugs (filenames without .md) for all archived goals.

        MUST return [] when goal_archive/ does not exist (common for agents
        that have never archived a goal). MUST NOT raise FileNotFoundError.

        Returns:
            Sorted list of archive slug strings.
        """
        ...

    def read_schema_version(self, agent_id: str) -> int | None:
        """Return the schema_version from goal.md frontmatter, or None if absent.

        NOTE: This version is independent of the vault-wide migration runner.
        DO NOT register goal.md as a MigratableUnit (migration/types.py
        Literal is locked to ['memory', 'wiki']). Use read_schema_version()
        only for GoalBackend-internal schema evolution and doctor checks.

        Returns:
            int schema_version when goal.md is present and parseable.
            None when goal.md is absent (reactive agent with no goal).

        Raises:
            GoalCorrupted: when goal.md is present but unparseable.
        """
        ...

    def goal_text(self, agent_id: str) -> str:
        """Return the raw text of goal.md for prompt assembly.

        Side-effect-free read — does NOT parse, validate, or modify goal.md.
        Returns '' when goal.md is absent (reactive agents, agents with no
        active goal). This is the cheap slice used by the profile backend and
        prompt assembly in place of a raw path.read_text().

        Args:
            agent_id: the agent directory name.

        Returns:
            Raw UTF-8 text of goal.md, or '' when absent.
        """
        ...

    def export(self, query: Any = None) -> GoalExport:
        """Export goal state as a canonical GoalExport (spec/40 Exportable).

        Enumerates via list paths (not semantic query). Best-effort
        point-in-time snapshot; does not acquire the agent LockBackend
        across the full read pass (spec/40 MUST 7).

        Read ordering: goal.md first (authoritative state), then
        goal_history.jsonl, then archives. This ordering means a concurrent
        apply_transition() that completes between reads may produce a snapshot
        where goal.md reflects the transition but history does not — the caller
        must hold the agent LockBackend to prevent this.

        UTF-8, LF, no-BOM throughout (spec/40 MUST 5). The filesystem impl
        normalizes CRLF → LF in all exported bytes.

        Args:
            query: unused (reserved for future bounded-export filtering).

        Returns:
            GoalExport with goal_md_bytes, history_records_with_bytes, and
            archived_goals_with_bytes. Each component is empty (b"" / [] / [])
            when the corresponding file/directory is absent.
        """
        ...

    def export_all(self) -> GoalExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        ...

    def capabilities(self) -> GoalCapabilities:
        """Backend capability declaration — see GoalCapabilities.

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.
        """
        ...

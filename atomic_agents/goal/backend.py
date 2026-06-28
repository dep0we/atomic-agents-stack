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

  Multi-goal addressing (spec/41 #642 addendum):
    create_goal()         — atomically create a new addressed run-goal at
                            goals/<goal_id>/ with O_EXCL collision semantics
    list_goals()          — enumerate all goal_ids: '_standing' (if goal.md
                            exists) + addressed run-goals under goals/

  Capability advertisement:
    capabilities()        — return GoalCapabilities

Separate AddressableGoalBackend Protocol (spec/41 #642):
    for_goal(goal_id)     — thin scope-handle factory; returns a view satisfying
                            the GoalBackend contract scoped to the addressed goal.
                            Mirrors LockBackend.scope() precedent. The twelve
                            pre-#642 GoalBackend signatures stay BYTE-IDENTICAL.
                            Operators MUST check isinstance(backend,
                            AddressableGoalBackend) before calling for_goal().

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
        expected_decision_id: str | None = None,
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
            expected_decision_id: optional gate-decision CAS guard (spec/41 MUST 14,
                PR3 #582). When not None, the backend MUST, UNDER THE LOCK
                (after load_goal(), AFTER the expected_from_status check, before
                the write), compare the sub-goal's gate_decision_id field against
                this value. If they differ (or gate_decision_id is None), MUST
                raise GoalConcurrentModification (no write, no JSONL line).
                Protects against a stale duplicate-resume replaying a gate answer
                for a decision_id that has already been answered and cleared.
                Default None = no check (backward-compatible).
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
                Also raised when expected_decision_id is not None and the
                sub-goal's gate_decision_id differs — detects a stale gate
                answer for an already-cleared decision (spec/41 MUST 14).
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

    def create_goal(
        self,
        agent_id: str,
        goal_id: str,
        goal: Goal,
        when: date | None = None,
    ) -> Goal:
        """Atomically create a new addressed run-goal at goals/<goal_id>/.

        Implements O_EXCL collision semantics via lock-guarded existence check:
        under the per-goal lock, check goal.md absence, raise GoalAlreadyExists
        if present, then probe serializability and write atomically. The lock
        serializes the check+write so no TOCTOU race exists.

        Write ordering (mirrors MUST 6 exactly):
          1. Validate goal_id (charset + reserved-name) — before any disk I/O.
          2. Stamp goal.created from `when` (or date.today() when None).
          3. Build goal_created event dict (ts-first per JSONL key ordering).
          4. Acquire per-goal lock.
          5. Check goal.md absence → raise GoalAlreadyExists if present.
          6. json.dumps(goal_created_event) pre-probe (serializability gate,
             BEFORE any goal.md/JSONL write — fails closed; no goal state is
             committed if it raises. An empty goals/<goal_id>/ directory may
             remain — see partial-create debris in MUST 11 / the module docstring).
          7. atomic_write goal.md (goals/<goal_id>/goal.md).
          8. atomic_append_jsonl goal_created event to goals/<goal_id>/goal_history.jsonl.

        The goal_created JSONL event schema:
          {ts, event:'goal_created', goal_id, intent, created, schema_version,
           conductor_run_id?}
          ts is FIRST (JSONL key ordering MUST). conductor_run_id is OMITTED
          (not set to None) for home-user goals — the key is absent.

        Note: create_goal() MUST stamp goal.created from `when` (overriding any
        value the caller placed on the Goal object). This is the single exception
        to MUST 4's write-verbatim rule, which applies to save_goal() only.
        See spec/41 #642 addendum for the distinct MUST.

        Args:
            agent_id: the agent directory name (unused in filesystem impl).
            goal_id: the addressed goal identifier. Must match [a-z0-9_-]{1,64}
                and MUST NOT equal STANDING_GOAL_ID ('_standing').
            goal: a fully-constructed, validated Goal object. The backend stamps
                goal.created from `when` before persisting.
            when: the date to stamp as goal.created and the goal_created event's
                `created` field. Defaults to date.today() when None.

        Returns:
            The persisted Goal (with goal.created stamped from `when`).

        Raises:
            ValueError: when goal_id fails charset validation or is the reserved
                STANDING_GOAL_ID ('_standing') — the reserved-name check raises
                ValueError, NOT GoalAlreadyExists (it is a rejection, not a
                collision).
            GoalAlreadyExists: when a goal with that goal_id already exists.
            SchemaValidationError: when the goal fails validate_goal().
            PathTraversalError: when goals/<goal_id> or a write leaf (goal.md /
                goal_history.jsonl) resolves outside the agent vault perimeter
                (a symlinked goal dir or leaf is refused before any byte is
                written — spec/41 #642).
        """
        ...

    def list_goals(self, agent_id: str) -> list[str]:
        """Enumerate all goal_ids for this agent.

        Returns a sorted list containing:
        - STANDING_GOAL_ID ('_standing') when agent_root/goal.md is present.
        - One entry per addressed run-goal whose goals/<id>/goal.md exists
          and whose id passes the [a-z0-9_-]{1,64} charset allow-list.

        Empty goals/ directories (partial create_goal()) are SKIPPED —
        list_goals() requires goals/<id>/goal.md to be present, not just the
        directory. This makes list_goals() / for_goal() / load_goal() consistent
        (no list entry that load_goal() would then fail on).

        Return ordering: sorted() — '_standing' sorts before alphabetic (a-z)
        run-goal names ('_' (95) < 'a' (97) in ASCII), but it is NOT
        unconditionally first: a goal_id beginning with '-' (45) or a digit (48)
        sorts before '_standing'. No code depends on the position.
        Conformance tests assert assert result == sorted(result).

        Returns:
            Sorted list of goal_id strings. [] when no goals exist.

        Raises:
            Never raises FileNotFoundError (absent goals/ → empty list). May
            raise PathTraversalError from the standing goal.md containment check
            (the only _require_within_root call in list_goals) if agent_root/
            goal.md resolves outside the vault.
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

        FAIL-LOUD GUARD (spec/41 #642): when the agent has any addressed goals
        (goals/<id>/goal.md present), export() MUST raise before reading any
        bytes. The guard fires as the FIRST operation. See issue #643 for the
        multi-goal export extension. The guard predicate requires at least one
        valid goals/*/goal.md — an empty goals/ directory does NOT trigger it.

        Args:
            query: unused (reserved for future bounded-export filtering).

        Returns:
            GoalExport with goal_md_bytes, history_records_with_bytes, and
            archived_goals_with_bytes. Each component is empty (b"" / [] / [])
            when the corresponding file/directory is absent.

        Raises:
            AtomicAgentsError: when the agent has addressed run-goals (guard
                fires before any bytes are read; points at #643).
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


# ──────────────────────────────────────────────────────────────────────────────
# AddressableGoalBackend Protocol (spec/41 #642)
# SEPARATE from GoalBackend — mirrors LockBackend.scope() precedent.
# The twelve pre-#642 GoalBackend signatures are BYTE-IDENTICAL.


@runtime_checkable
class AddressableGoalBackend(Protocol):
    """Thin scope-handle factory for multi-goal agents (spec/41 #642).

    SEPARATE Protocol from GoalBackend — mirrors LockBackend.scope() precedent.
    The twelve pre-#642 GoalBackend signatures stay BYTE-IDENTICAL. This Protocol
    adds only for_goal(goal_id), which returns a scoped view satisfying the
    full GoalBackend contract for the addressed goal.

    Callers MUST check isinstance(backend, AddressableGoalBackend) before
    calling for_goal() — this is the runtime gate. Do NOT call for_goal() on
    a bare GoalBackend that doesn't implement this Protocol.

    Routing contract:
    - for_goal(STANDING_GOAL_ID) or for_goal(None): returns a GoalBackend
      scoped to agent_root/ (i.e., agent_root/goal.md, goal_history.jsonl,
      etc. — UNCHANGED from the standing-goal layout). Backward-compat alias.
    - for_goal(other_id): returns a GoalBackend scoped to
      agent_root/goals/<other_id>/ (own goal.md, goal_history.jsonl,
      goal_archive/, .goal.lock). Per-goal lock granularity: concurrent
      run-goals never block each other.

    FilesystemGoalBackend implements both GoalBackend AND AddressableGoalBackend.
    """

    def for_goal(self, goal_id: str | None) -> GoalBackend:
        """Return a GoalBackend view scoped to the addressed goal.

        Args:
            goal_id: the goal identifier. STANDING_GOAL_ID ('_standing') or
                None routes to the standing goal (agent_root/goal.md).
                Any other valid goal_id routes to goals/<goal_id>/.

        Returns:
            A GoalBackend instance whose paths are all scoped to the addressed
            goal's directory. The returned backend satisfies the full GoalBackend
            contract (all 13 methods + the backend_id property — 14 protocol
            attributes as of #642).

        Raises:
            ValueError: when goal_id is not None and fails charset validation.
        """
        ...

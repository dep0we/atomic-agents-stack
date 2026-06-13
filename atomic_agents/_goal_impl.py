"""Goal manager — for goal-driven and hybrid Atomic Agents.

Per the spec at <vault>/Atomic Agents/spec/12-goals-and-intent.md.

Most agents are reactive — they answer one question at a time. Goal-driven
agents (Muse Director, future research-pursuit agents) maintain a persistent
intent across many sessions and decompose it into work via sub_goals + the
project queue.

This module handles the goal data model, sub-goal lifecycle transitions,
completion-criteria evaluation, abandonment, and progress reports. It does
NOT automatically dispatch work (that requires the multi-agent project
cascade — see issue #6); for v0.4, dispatch is manual via `goal advance`.

Usage:

    from atomic_agents.goal import GoalManager
    from pathlib import Path

    gm = GoalManager(Path.home() / "agents", "muse-director")
    gm.load()
    print(gm.status_summary())
    next_sg = gm.next_sub_goal()
    if next_sg:
        gm.mark_in_progress(next_sg.id, assigned="self")

CLI:

    python -m atomic_agents.goal status <agent>
    python -m atomic_agents.goal next <agent>
    python -m atomic_agents.goal advance <agent> <sub_goal_id> [--complete]
    python -m atomic_agents.goal abandon <agent> --reason "..."
    python -m atomic_agents.goal report <agent>

HARD RULES (per spec/12):
- Goals are operator-set; the agent doesn't auto-generate them
- Success criteria are operator-set; the agent doesn't tune them
- Missed deadlines surface to operator; never auto-extended
- Locked decisions in policy/ are never overridden
- Sequential goals only (one active per agent in v0.4)
"""

from __future__ import annotations
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .outcome import OutcomeResult

import frontmatter

from ._io import atomic_write
from ._platform import get_agents_root
from .exceptions import (
    AtomicAgentsError,
    GoalCorrupted,
    SchemaValidationError,
)

# ── Canonical types AND validation live in goal/types.py — import from there
# so all callers share the same class identity AND the same single validator
# regardless of whether they import via 'from atomic_agents.goal import SubGoal'
# or 'from atomic_agents._goal_impl import SubGoal'. The constants + validate_goal
# are re-exported here (and via goal/__init__.py's __getattr__) for backward
# compatibility — there is no second copy of the validator.
from .goal.types import (  # noqa: F401 (constants + validate_agent_mode re-exported for backward compat)
    CURRENT_GOAL_SCHEMA_VERSION,
    VALID_AGENT_MODES,
    VALID_PRIORITIES,
    VALID_SUB_GOAL_STATUSES,
    CompletionEvaluation,
    Goal,
    SubGoal,
    build_goal_frontmatter as _build_goal_frontmatter,
    validate_agent_mode,
    validate_goal,
)

# GoalBackend imported from the direct submodule (NOT goal/__init__.py) for
# circular-import safety. goal/__init__.py uses __getattr__ to lazily load
# _goal_impl; importing from goal/__init__.py here at module level would
# close that cycle and break bootstrap. goal.backend imports only from
# goal/types.py + stdlib — no heavy deps, no cycle risk.
from .goal.backend import GoalBackend


# ──────────────────────────────────────────────────────────────────
# IDENTITY.md mode parsing


def parse_agent_mode(identity_path: Path) -> str:
    """Parse the 'Operating mode' section of IDENTITY.md from disk.

    Thin wrapper around ``parse_agent_mode_text`` for callers who only
    have a path. Profile-backend callers that have already loaded the
    text (e.g., ``FilesystemAgentProfileBackend.load_profile`` reads
    IDENTITY.md once for ``persona_identity``) should call
    ``parse_agent_mode_text`` directly to avoid the second read.
    """
    if not identity_path.exists():
        return "reactive"
    try:
        text = identity_path.read_text(encoding="utf-8")
    except OSError:
        return "reactive"
    return parse_agent_mode_text(text)


def parse_agent_mode_text(text: str) -> str:
    """Derive the agent mode from already-loaded IDENTITY.md content.

    Looks for a line like 'This agent is **reactive**' or 'This agent
    is goal-driven' inside the ``## Operating mode`` section. Defaults
    to 'reactive' if the section is missing or no recognized mode
    keyword is found (per spec/01).

    Empty input returns 'reactive'. Same defaults as ``parse_agent_mode``.
    """
    if not text:
        return "reactive"

    section_match = re.search(
        r"##\s+Operating mode\b.*?(?=\n##\s|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return "reactive"

    section = section_match.group(0)
    for mode in ("hybrid", "goal-driven", "reactive"):
        if re.search(rf"\b{re.escape(mode)}\b", section, re.IGNORECASE):
            return mode
    return "reactive"


# ──────────────────────────────────────────────────────────────────
# GoalManager


class GoalManager:
    """Manages one agent's goal.md.

    Single-agent path: <agent_root>/goal.md
    Multi-agent project path: <agents_root>/<system>/projects/<project>/goal.md

    For v0.4, only the single-agent path is supported via this class. The
    multi-agent project goal is the same data model — issue #6 will add a
    project-aware variant.
    """

    def __init__(
        self,
        agents_root: Path | None = None,
        agent_name: str = "",
        today: date | None = None,
        goal_backend: GoalBackend | None = None,
    ):
        self.agents_root = agents_root or get_agents_root()
        self.agent_name = agent_name
        self.today = today or date.today()
        self.agent_root = self.agents_root / agent_name

        if not self.agent_root.exists():
            raise AtomicAgentsError(f"Agent folder not found: {self.agent_root}")

        self.goal_path = self.agent_root / "goal.md"
        self.archive_dir = self.agent_root / "goal_archive"
        self._goal: Goal | None = None

        # GoalBackend instance. kwarg-wins-over-env: if a backend was explicitly
        # passed, use it; otherwise resolve via the operator-config factory (which
        # reads ATOMIC_AGENTS_GOAL_BACKEND). Lazy import inside __init__ to avoid
        # closing the bootstrap cycle that goal/__init__.py's __getattr__ exists
        # to prevent (profile/filesystem.py imports parse_agent_mode_text from
        # atomic_agents.goal during package bootstrap). Mirrors agent.py's
        # journal_backend resolution (kwarg-wins-over-env, default factory).
        if goal_backend is None:
            from .goal import get_default_goal_backend  # noqa: PLC0415

            self.goal_backend: GoalBackend = get_default_goal_backend(self.agent_root)
        else:
            self.goal_backend = goal_backend

    # ────────────────────────────────────────────────────────────
    # Load / save

    def has_goal(self) -> bool:
        return self.goal_path.exists()

    def has_active_goal(self) -> bool:
        if not self.has_goal():
            return False
        try:
            self.load()
            return self._goal is not None and self._goal.active
        except (GoalCorrupted, SchemaValidationError):
            return False

    def load(self) -> Goal:
        """Load + validate goal.md. Returns the Goal.

        Routes through self.goal_backend.load_goal() (COARSE-ROUTE adoption,
        #448 PR1). self._goal is set so subsequent method calls that guard on
        'if self._goal is None: self.load()' work correctly.
        """
        if not self.has_goal():
            raise AtomicAgentsError(f"No goal.md at {self.goal_path}")
        self._goal = self.goal_backend.load_goal(self.agent_name)
        return self._goal

    def save(self) -> None:
        """Write the current goal back to goal.md. Updates last_progress_check.

        COARSE-ROUTE adoption (#448 PR1): routes through self.goal_backend.save_goal().
        Caller-side stamping (A2 ruling): self._goal.last_progress_check is set
        FIRST with self.today (injectable clock), then save_goal() writes verbatim.
        self.goal_backend.save_goal() is intentionally lock-free — single-session
        saves are safe without the lock. The backend's apply_transition() (PR3's
        coordinator primitive) holds the lock for its atomic sequence; this coarse
        save path does not (accepted COARSE-ROUTE ordering contract, spec/41).
        (Note: archive() also takes no GoalManager-level lock and is NOT routed
        through backend.archive_goal() this PR — see #483.)

        BEHAVIOR DELTA vs. pre-#448 save() (conscious, spec/41 MUST 5-aligned,
        documented in CHANGELOG): the old save() wrote goal.md with NO frontmatter
        validation. save_goal() routes through the backend's _write_goal(), which
        runs validate_goal() BEFORE the durable write and fails closed (raises
        SchemaValidationError, writes nothing) on an in-memory Goal whose
        serialized frontmatter the backend's own load_goal() would reject. This is
        BYTE-IDENTICAL for any valid goal (validation is read-only) — every
        mark_*/add_sub_goal mutator produces valid state. It only changes the
        failure mode on an already-invalid in-memory Goal from "write invalid
        bytes" to "raise" — a hardening that closes the write-time/read-time
        validation asymmetry spec/41 MUST 5 requires of EVERY write path
        (save_goal AND apply_transition). The one reachable public path that hits
        this is mark_blocked() with a forward reference (sub-goal A blocked_by a
        later-listed B): the validator's forward-only referential check rejects on
        save where the old unvalidated path persisted it. Guarded by
        test_save_forward_ref_blocked_by_fails_closed below.
        """
        if self._goal is None:
            raise AtomicAgentsError("No goal loaded to save")
        # A2 ruling: stamp last_progress_check BEFORE calling save_goal so the
        # backend writes the injectable self.today clock value, not a stale date.
        self._goal.last_progress_check = self.today.isoformat()
        self.goal_backend.save_goal(self.agent_name, self._goal)

    # ────────────────────────────────────────────────────────────
    # Sub-goal lifecycle

    def find_sub_goal(self, sub_goal_id: str) -> SubGoal | None:
        if self._goal is None:
            self.load()
        for sg in self._goal.sub_goals:
            if sg.id == sub_goal_id:
                return sg
        return None

    def next_sub_goal(self) -> SubGoal | None:
        """Find the next pending, unblocked sub_goal.

        Raises GoalCorrupted if a sub_goal's blocked_by references an id that
        doesn't exist — that inconsistency means the goal graph is untrustworthy
        and dispatching would violate operator intent.
        """
        if self._goal is None:
            self.load()
        for sg in self._goal.sub_goals:
            if sg.status != "pending":
                continue
            if sg.blocked_by:
                blocker = self.find_sub_goal(sg.blocked_by)
                if blocker is None:
                    raise GoalCorrupted(
                        f"sub_goal '{sg.id}' blocked_by '{sg.blocked_by}' which does not exist; "
                        f"goal graph is inconsistent — operator must repair goal.md"
                    )
                if blocker.status != "complete":
                    continue
            return sg
        return None

    def mark_in_progress(
        self, sub_goal_id: str, assigned: str | None = None
    ) -> SubGoal:
        """Transition sub_goal to in_progress. Optionally update assigned.

        When transitioning from blocked → in_progress, blocked_by is automatically
        cleared and the previous blocker is recorded in the history so the state
        remains consistent (in_progress must not carry a blocked_by).
        """
        sg = self._require_sub_goal(sub_goal_id)
        if sg.status not in ("pending", "blocked"):
            raise AtomicAgentsError(
                f"sub_goal {sub_goal_id} is {sg.status}; can only transition to "
                f"in_progress from pending or blocked"
            )
        previous_blocked_by = sg.blocked_by
        sg.status = "in_progress"
        sg.blocked_by = None
        if assigned is not None:
            sg.assigned = assigned
        history_msg = f"sub_goal `{sub_goal_id}` → in_progress"
        if assigned:
            history_msg += f" (assigned: {assigned})"
        if previous_blocked_by:
            history_msg += f" (unblocked; previous_blocked_by: {previous_blocked_by})"
        self._append_history(history_msg)
        return sg

    def mark_complete(self, sub_goal_id: str, output: str | None = None) -> SubGoal:
        """Transition sub_goal to complete. Optionally record output artifact path."""
        sg = self._require_sub_goal(sub_goal_id)
        if sg.status == "complete":
            return sg  # idempotent
        if sg.status not in ("in_progress", "pending"):
            raise AtomicAgentsError(
                f"sub_goal {sub_goal_id} is {sg.status}; can only complete from "
                f"in_progress or pending"
            )
        sg.status = "complete"
        sg.completed = self.today.isoformat()
        if output is not None:
            sg.output = output
        self._append_history(
            f"sub_goal `{sub_goal_id}` → complete"
            + (f" (output: {output})" if output else "")
        )
        return sg

    def mark_blocked(self, sub_goal_id: str, blocked_by: str) -> SubGoal:
        """Mark sub_goal as blocked by another sub_goal.

        Raises AtomicAgentsError if:
        - blocked_by references an unknown sub_goal
        - sub_goal_id == blocked_by (self-block)
        - the operation would introduce a cycle in the blocked_by graph
        """
        sg = self._require_sub_goal(sub_goal_id)
        # Validate the blocker exists
        if not self.find_sub_goal(blocked_by):
            raise AtomicAgentsError(
                f"blocked_by references unknown sub_goal: {blocked_by}"
            )
        if sub_goal_id == blocked_by:
            raise AtomicAgentsError(f"sub_goal '{sub_goal_id}' cannot block itself")
        if self._would_cycle(sub_goal_id, blocked_by):
            raise AtomicAgentsError(
                f"marking '{sub_goal_id}' blocked by '{blocked_by}' would create a cycle"
            )
        sg.status = "blocked"
        sg.blocked_by = blocked_by
        self._append_history(f"sub_goal `{sub_goal_id}` → blocked by `{blocked_by}`")
        return sg

    def mark_abandoned(self, sub_goal_id: str, reason: str = "") -> SubGoal:
        """Mark sub_goal as abandoned (won't be pursued)."""
        sg = self._require_sub_goal(sub_goal_id)
        sg.status = "abandoned"
        msg = f"sub_goal `{sub_goal_id}` → abandoned"
        if reason:
            msg += f": {reason}"
        self._append_history(msg)
        return sg

    def add_sub_goal(
        self,
        id: str,
        label: str,
        assigned: str | None = None,
        deadline: str | None = None,
        blocked_by: str | None = None,
    ) -> SubGoal:
        """Add a new sub_goal to the active goal. Operator action."""
        if self._goal is None:
            self.load()
        if self.find_sub_goal(id):
            raise AtomicAgentsError(f"sub_goal {id} already exists")
        if blocked_by is not None:
            if blocked_by == id:
                raise AtomicAgentsError(f"sub_goal {id} cannot block itself")
            if not self.find_sub_goal(blocked_by):
                raise AtomicAgentsError(
                    f"sub_goal {id} blocked_by references unknown id '{blocked_by}'"
                )
            # Check that adding this sub_goal with the given blocked_by won't
            # create a cycle (the new node can't already be a transitive
            # dependency of blocked_by, since it doesn't exist yet, so only
            # a self-reference is possible — already handled above).
            # If blocked_by itself would form a cycle through the new id
            # this can only happen if the graph already contains a path from
            # id to blocked_by.  Since id is new, no such path can exist.
        sg = SubGoal(
            id=id,
            label=label,
            status="pending",
            assigned=assigned,
            deadline=deadline,
            blocked_by=blocked_by,
        )
        self._goal.sub_goals.append(sg)
        self._append_history(f"sub_goal `{id}` added")
        return sg

    def _require_sub_goal(self, sub_goal_id: str) -> SubGoal:
        sg = self.find_sub_goal(sub_goal_id)
        if sg is None:
            raise AtomicAgentsError(f"sub_goal not found: {sub_goal_id}")
        return sg

    # ────────────────────────────────────────────────────────────
    # Completion + abandonment

    def evaluate_completion(self) -> CompletionEvaluation:
        """Check whether the goal's success criteria are met.

        v0.4 heuristic: 'all criteria met' iff every sub_goal is complete or
        abandoned AND no sub_goals are pending/in_progress/blocked.
        Operator confirms before mark-complete (this method just reports).
        """
        if self._goal is None:
            self.load()
        sg = self._goal.sub_goals
        complete = sum(1 for s in sg if s.status == "complete")
        in_progress = sum(1 for s in sg if s.status == "in_progress")
        blocked = sum(1 for s in sg if s.status == "blocked")
        pending = sum(1 for s in sg if s.status == "pending")

        all_done = len(sg) > 0 and pending == 0 and in_progress == 0 and blocked == 0

        # Deadline analysis
        days_until_deadline: int | None = None
        overdue = False
        if self._goal.deadline:
            try:
                d = date.fromisoformat(self._goal.deadline)
                days_until_deadline = (d - self.today).days
                overdue = days_until_deadline < 0 and self._goal.active
            except ValueError:
                pass

        return CompletionEvaluation(
            all_criteria_met=all_done,
            sub_goals_complete=complete,
            sub_goals_total=len(sg),
            sub_goals_in_progress=in_progress,
            sub_goals_blocked=blocked,
            sub_goals_pending=pending,
            days_until_deadline=days_until_deadline,
            overdue=overdue,
        )

    def archive(self, reason: str = "completed") -> Path:
        """Archive the current goal — move goal.md to goal_archive/.

        Used for both successful completion and operator-initiated abandonment.

        Collision safety: if <date>_<slug>.md already exists (same day, same intent
        slug), a numeric suffix is appended (_1, _2, …) until a free path is found.
        This prevents overwriting a previous archive.

        Write ordering: the archive file is written first; goal.md is unlinked only
        after the archive has been successfully committed to disk.  A failure between
        the two steps leaves both files present (recoverable state) rather than
        losing data.

        Idempotent: if goal.md is already absent when this is called, a second call
        via has_goal() guard will raise AtomicAgentsError rather than silently
        double-archiving.
        """
        if self._goal is None:
            self.load()
        if not self.has_goal():
            raise AtomicAgentsError("No active goal to archive")

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        intent_slug = re.sub(r"[^a-z0-9]+", "_", self._goal.intent.lower()).strip("_")[
            :60
        ]
        base_name = f"{self.today.isoformat()}_{intent_slug}"
        archive_path = self.archive_dir / f"{base_name}.md"

        # Increment suffix until a free path is found (collision safety)
        counter = 0
        while archive_path.exists():
            counter += 1
            archive_path = self.archive_dir / f"{base_name}_{counter}.md"

        # Mark inactive + update last_progress_check + record reason in history.
        # Mutation ORDER MATTERS for build_goal_frontmatter (reads Goal fields
        # directly — no override kwargs): active and last_progress_check MUST be
        # mutated on self._goal BEFORE calling build_goal_frontmatter so the
        # archive file reflects active=False and today's date. (A3 ruling fix.)
        self._goal.active = False
        self._goal.last_progress_check = self.today.isoformat()
        self._append_history(f"goal archived ({reason})")

        # A3 fix (intentional data-loss fix, #448 PR1): swap the hand-rolled
        # frontmatter dict for build_goal_frontmatter(self._goal). The old dict
        # silently dropped deadline, parent_goal, related_atomic_notes,
        # related_decisions, related_canon_pages. build_goal_frontmatter
        # preserves all optional Goal fields. archived_at + archive_reason are
        # NOT Goal dataclass fields; they are appended to the dict after the call.
        # archive() is NOT routed through backend.archive_goal() this PR — that
        # drags in the backend's date.today() and loses the injectable self.today
        # clock for archived_at. Full archive-path adoption is a filed follow-up.
        # NOTE: Not lock-protected at GoalManager level — concurrent archive()
        # calls on the same agent could race (rare in single-session use). The
        # backend's archive_goal() is lock-protected; full adoption tracked in
        # the follow-up issue for routing archive() through backend.archive_goal().
        fm = _build_goal_frontmatter(self._goal)
        fm["archived_at"] = self.today.isoformat()
        fm["archive_reason"] = reason
        post = frontmatter.Post(self._goal.body, **fm)
        atomic_write(archive_path, frontmatter.dumps(post) + "\n")

        # Remove the active goal.md only after archive is safely written
        self.goal_path.unlink()
        self._goal = None
        return archive_path

    def abandon(self, reason: str) -> Path:
        """Operator-initiated abandonment. Same mechanism as completion-archive,
        but with a stated reason."""
        return self.archive(reason=f"abandoned: {reason}")

    # ────────────────────────────────────────────────────────────
    # Reports

    def status_summary(self) -> str:
        """One-screen summary suitable for `goal status` output."""
        if self._goal is None:
            try:
                self.load()
            except AtomicAgentsError as e:
                return f"No goal: {e}"
        ev = self.evaluate_completion()
        next_sg = self.next_sub_goal()

        lines = [
            f"Goal: {self._goal.intent}",
            f"Active: {self._goal.active}  |  Priority: {self._goal.priority}",
            f"Created: {self._goal.created}  |  Last check: {self._goal.last_progress_check}",
        ]
        if self._goal.deadline:
            deadline_note = (
                f"  ({ev.days_until_deadline} days remaining)"
                if ev.days_until_deadline is not None
                else ""
            )
            if ev.overdue:
                deadline_note = f"  ⚠ OVERDUE by {abs(ev.days_until_deadline)} days"
            lines.append(f"Deadline: {self._goal.deadline}{deadline_note}")

        lines.append("")
        lines.append("Success criteria:")
        for c in self._goal.success_criteria:
            lines.append(f"  - {c}")

        lines.append("")
        lines.append(
            f"Sub-goals: {ev.sub_goals_complete}/{ev.sub_goals_total} complete  "
            f"({ev.sub_goals_in_progress} in progress, {ev.sub_goals_pending} pending, "
            f"{ev.sub_goals_blocked} blocked)"
        )
        for sg in self._goal.sub_goals:
            status_marker = {
                "pending": "○",
                "in_progress": "▶",
                "complete": "✓",
                "blocked": "⛔",
                "abandoned": "✗",
            }.get(sg.status, "?")
            assigned = f" [{sg.assigned}]" if sg.assigned else ""
            deadline = f"  (due {sg.deadline})" if sg.deadline else ""
            lines.append(f"  {status_marker} {sg.id}: {sg.label}{assigned}{deadline}")

        if next_sg:
            lines.append("")
            lines.append(f"Next dispatchable: {next_sg.id} — {next_sg.label}")

        if ev.all_criteria_met:
            lines.append("")
            lines.append(
                "✓ All sub-goals complete. Run `goal complete <agent>` to archive."
            )

        return "\n".join(lines)

    def progress_report(self) -> str:
        """Longer-form report suitable for periodic check-ins (per spec/12)."""
        if self._goal is None:
            self.load()
        ev = self.evaluate_completion()

        # Compute pacing if deadline + creation are present
        try:
            created_d = date.fromisoformat(self._goal.created)
            elapsed_days = (self.today - created_d).days
            if self._goal.deadline:
                deadline_d = date.fromisoformat(self._goal.deadline)
                total_days = (deadline_d - created_d).days
            else:
                total_days = None
        except ValueError:
            elapsed_days = None
            total_days = None

        sg_total = ev.sub_goals_total
        if sg_total > 0:
            pct_complete = ev.sub_goals_complete / sg_total * 100
        else:
            pct_complete = 0.0

        lines = [
            f"Goal: {self._goal.intent}",
            f"Status as of {self.today.isoformat()}:",
            f"  ▸ {ev.sub_goals_complete} of {sg_total} sub-goals complete ({pct_complete:.1f}%)",
        ]
        if ev.sub_goals_in_progress > 0:
            in_progress = [
                sg for sg in self._goal.sub_goals if sg.status == "in_progress"
            ]
            for sg in in_progress:
                assigned = f" ({sg.assigned})" if sg.assigned else ""
                lines.append(f"  ▸ In progress: {sg.label}{assigned}")
        if ev.sub_goals_blocked > 0:
            blocked = [sg for sg in self._goal.sub_goals if sg.status == "blocked"]
            for sg in blocked:
                lines.append(f"  ▸ Blocked: {sg.label} (by {sg.blocked_by})")

        if elapsed_days is not None and total_days is not None and total_days > 0:
            elapsed_pct = elapsed_days / total_days * 100
            pacing_note = ""
            if pct_complete > elapsed_pct + 10:
                pacing_note = " — ahead of pace"
            elif pct_complete < elapsed_pct - 10:
                pacing_note = " — behind pace"
            lines.append(
                f"  ▸ Time used: {elapsed_pct:.0f}%  ({elapsed_days}/{total_days} days){pacing_note}"
            )

        if ev.overdue:
            lines.append(f"  ▸ ⚠ OVERDUE by {abs(ev.days_until_deadline)} days")

        return "\n".join(lines)

    # ────────────────────────────────────────────────────────────
    # Goal-outcome composition

    def dispatch_as_outcome(
        self,
        sub_goal_id: str,
        rubric: "str | Path",
        max_iterations: int = 3,
        extra_context: str | None = None,
        judge_model: str | None = None,
    ) -> "tuple[OutcomeResult, SubGoal]":
        """Dispatch a sub-goal as an outcome and update sub-goal status from result.

        Thin shim over ``goal/coordinator.py::dispatch_sub_goal_as_outcome()``.

        All dispatch logic — pre-dispatch cost gate, apply_transition
        pre/terminal transitions (spec/41 MUST 6 ordering: goal.md written
        BEFORE the JSONL audit line, both under the lock), terminal-state
        mapping, and GoalConcurrentModification (CAS) protection — lives in the
        coordinator (spec/41 §"Goal-outcome composition", CLAUDE.md Principle #4).

        The CLI path (``python -m atomic_agents.goal dispatch-outcome``) calls
        this method and the coordinator runs end-to-end. A programmatic caller
        does NOT need to call save() after dispatch — the coordinator's
        apply_transition calls are self-contained.

        Cost gate (Principle #4 — live on this path): this shim constructs a real
        ``AtomicAgent`` (keyword args: ``name``/``agents_root``/``goal_backend``)
        and passes it to the coordinator, so the pre-dispatch fail-closed cost
        gate consults the SAME budget universe (model.md caps) the
        ``OutcomeRunner`` will spend. The agent is constructed OUTSIDE any
        try/except — a construction failure propagates (fail-closed, never
        swallowed into "allowed"). When the gate fires, ``CostGuardrailBlocked``
        propagates to the CLI (exit 3) BEFORE the run + construction overhead is
        paid ("refuse before paying overhead").

        Returns:
            (OutcomeResult, updated SubGoal)

        Raises:
            GoalCorrupted: sub-goal is not pending/in_progress, or has an
                unresolved blocked_by dependency.
            CostGuardrailBlocked: pre-dispatch cost gate fired (model.md cap hit);
                the coordinator_dispatch_rejected event was appended first.
            GoalConcurrentModification: terminal apply_transition detected a
                concurrent modification (sub-goal moved off in_progress during run).
        """
        from .agent import AtomicAgent  # noqa: PLC0415
        from .goal.coordinator import dispatch_sub_goal_as_outcome  # noqa: PLC0415

        # Construct a real AtomicAgent for the coordinator's fail-closed cost gate
        # (Principle #4). KEYWORD args (the #425 bug constructed it positionally);
        # goal_backend threaded so the agent shares this manager's persistence
        # universe. NO try/except — a construction failure must propagate (a
        # swallowed TypeError is exactly the #425 fail-OPEN bug).
        gate_agent = AtomicAgent(
            name=self.agent_name,
            agents_root=self.agents_root,
            goal_backend=self.goal_backend,
        )
        outcome_result, updated_sg = dispatch_sub_goal_as_outcome(
            agent=gate_agent,
            goal_manager=self,
            sub_goal_id=sub_goal_id,
            rubric=rubric,
            max_iterations=max_iterations,
            extra_context=extra_context,
            judge_model=judge_model,
        )

        # The coordinator wrote goal.md via apply_transition(). Reload the in-memory
        # _goal so the manager's state is consistent with the on-disk state (the
        # coordinator set the terminal status + history body), then stamp
        # last_progress_check via self.save() (A2 ruling: injectable today clock).
        # self.save() calls save_goal() which is a verbatim write-what-I-give-you
        # after stamping last_progress_check — it rewrites the file (now including
        # the terminal status written by apply_transition) with the updated date.
        # The CLI's trailing gm.save() after this call is a harmless no-op.
        #
        # DELIBERATE lock-free re-stamp: this trailing save() is OUTSIDE the goal
        # lock (save_goal is the COARSE-ROUTE path) and exists ONLY to stamp
        # last_progress_check with the injectable self.today clock, which
        # apply_transition's non-injectable date.today() cannot do. The CAS-guarded
        # terminal apply_transition already persisted the authoritative status under
        # the lock; this re-stamp is accepted under the single-session COARSE-ROUTE
        # contract (the CLI is single-session). The injectable-clock surface that
        # would let apply_transition stamp last_progress_check under the lock — and
        # retire this trailing save — is tracked in #483.
        self._goal = self.goal_backend.load_goal(self.agent_name)
        self.save()  # stamps last_progress_check = self.today (lock-free, see above)

        return outcome_result, updated_sg

    def _build_outcome_description_from_sub_goal(self, sg: SubGoal) -> str:
        """Build a clear outcome description from sub-goal fields.

        Pattern:
            <sg.label>

            <sg.body if present>

            Acceptance criteria for this sub-goal:
              - <criterion>
              ...
        """
        parts: list[str] = [sg.label]

        if sg.body and sg.body.strip():
            parts.append(sg.body.strip())

        if sg.acceptance_criteria:
            criteria_lines = "\n".join(f"  - {c}" for c in sg.acceptance_criteria)
            parts.append(f"Acceptance criteria for this sub-goal:\n{criteria_lines}")

        return "\n\n".join(parts)

    # ────────────────────────────────────────────────────────────
    # Internals

    def _would_cycle(self, sub_goal_id: str, new_blocker_id: str) -> bool:
        """Return True if adding blocked_by=new_blocker_id to sub_goal_id would
        create a cycle in the blocked_by dependency graph.

        We check whether sub_goal_id is reachable from new_blocker_id by
        following blocked_by edges.  If it is, then adding the edge
        sub_goal_id → new_blocker_id would close a cycle.

        Also handles the trivial self-block case (sub_goal_id == new_blocker_id).
        """
        if self._goal is None:
            return False
        # Build an adjacency map: id → blocked_by (or None)
        # We also speculatively include the would-be new edge.
        blocked_by_map: dict[str, str | None] = {}
        for sg in self._goal.sub_goals:
            blocked_by_map[sg.id] = sg.blocked_by
        # Temporarily add the proposed edge
        blocked_by_map[sub_goal_id] = new_blocker_id

        # DFS from new_blocker_id; if we reach sub_goal_id, there's a cycle.
        visited: set[str] = set()
        stack = [new_blocker_id]
        while stack:
            current = stack.pop()
            if current == sub_goal_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            blocker = blocked_by_map.get(current)
            if blocker is not None:
                stack.append(blocker)
        return False

    def _append_history(self, entry: str) -> None:
        """Append a timestamped line to the goal's body history section."""
        if self._goal is None:
            return
        history_marker = "## History"
        if history_marker not in self._goal.body:
            self._goal.body = (
                self._goal.body.rstrip() + f"\n\n{history_marker} (auto-appended)\n"
            )
        self._goal.body = (
            self._goal.body.rstrip() + f"\n- {self.today.isoformat()} — {entry}"
        )


# ──────────────────────────────────────────────────────────────────
# CLI


def main(argv: list[str] | None = None, goal_backend: GoalBackend | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="atomic-agents.goal",
        description="Goal manager for goal-driven and hybrid Atomic Agents",
    )
    parser.add_argument(
        "--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Print current goal status")
    p_status.add_argument("agent")

    p_next = sub.add_parser("next", help="Print next dispatchable sub-goal")
    p_next.add_argument("agent")

    p_advance = sub.add_parser("advance", help="Advance a sub-goal status")
    p_advance.add_argument("agent")
    p_advance.add_argument("sub_goal_id")
    p_advance.add_argument(
        "--complete",
        action="store_true",
        help="mark complete (default: mark in_progress)",
    )
    p_advance.add_argument("--assigned", default=None)
    p_advance.add_argument(
        "--output", default=None, help="path to artifact (when --complete)"
    )

    p_abandon = sub.add_parser(
        "abandon", help="Abandon the goal — non-destructive archive"
    )
    p_abandon.add_argument("agent")
    p_abandon.add_argument("--reason", required=True)

    p_complete = sub.add_parser(
        "complete", help="Mark the entire goal complete + archive"
    )
    p_complete.add_argument("agent")

    p_report = sub.add_parser(
        "report", help="Periodic progress report (suitable for journal)"
    )
    p_report.add_argument("agent")

    p_dispatch_outcome = sub.add_parser(
        "dispatch-outcome",
        help="Run a sub-goal as an outcome loop and machine-decide completion",
    )
    p_dispatch_outcome.add_argument("agent")
    p_dispatch_outcome.add_argument("sub_goal_id")
    p_dispatch_outcome.add_argument(
        "--rubric",
        required=True,
        help="path to rubric file, or 'inline:<text>'",
    )
    p_dispatch_outcome.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="max iterations for the outcome loop (default 3)",
    )
    p_dispatch_outcome.add_argument(
        "--judge-model",
        default=None,
        help="override the judge model",
    )

    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root
        else get_agents_root()
    )

    try:
        # goal_backend kwarg: None uses operator env-var default; non-None injects
        # a backend (programmatic callers / test fixtures) without a CLI flag.
        # The CLI itself does not expose --goal-backend (deployment-env territory);
        # env var ATOMIC_AGENTS_GOAL_BACKEND is the operator override surface.
        gm = GoalManager(agents_root, args.agent, goal_backend=goal_backend)
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.cmd == "status":
        print(gm.status_summary())
        return 0

    if args.cmd == "next":
        if not gm.has_goal():
            print("No goal.md for this agent.", file=sys.stderr)
            return 1
        gm.load()
        next_sg = gm.next_sub_goal()
        if next_sg:
            print(f"{next_sg.id}: {next_sg.label}")
            if next_sg.assigned:
                print(f"  assigned: {next_sg.assigned}")
            if next_sg.deadline:
                print(f"  deadline: {next_sg.deadline}")
            return 0
        else:
            print(
                "No dispatchable sub-goal — all are complete, in progress, or blocked."
            )
            return 0

    if args.cmd == "advance":
        gm.load()
        if args.complete:
            sg = gm.mark_complete(args.sub_goal_id, output=args.output)
            print(f"✓ {sg.id} → complete")
        else:
            sg = gm.mark_in_progress(args.sub_goal_id, assigned=args.assigned)
            print(
                f"▶ {sg.id} → in_progress"
                + (f" (assigned: {sg.assigned})" if sg.assigned else "")
            )
        gm.save()
        return 0

    if args.cmd == "abandon":
        archive_path = gm.abandon(args.reason)
        print(f"Goal abandoned. Archived to {archive_path}")
        return 0

    if args.cmd == "complete":
        gm.load()
        ev = gm.evaluate_completion()
        if not ev.all_criteria_met:
            print(
                f"Cannot mark complete: {ev.sub_goals_pending} pending, "
                f"{ev.sub_goals_in_progress} in progress, {ev.sub_goals_blocked} blocked.",
                file=sys.stderr,
            )
            return 1
        archive_path = gm.archive(reason="completed — all sub-goals done")
        print(f"✓ Goal complete. Archived to {archive_path}")
        return 0

    if args.cmd == "report":
        print(gm.progress_report())
        return 0

    if args.cmd == "dispatch-outcome":
        import sys as _sys

        gm.load()

        # Resolve rubric arg
        rubric: "str | Path"
        if args.rubric.startswith("inline:"):
            rubric = args.rubric[len("inline:") :]
        else:
            rubric = Path(args.rubric)

        from .exceptions import CostGuardrailBlocked, GoalConcurrentModification  # noqa: PLC0415

        try:
            result, sg = gm.dispatch_as_outcome(
                sub_goal_id=args.sub_goal_id,
                rubric=rubric,
                max_iterations=args.max_iterations,
                judge_model=args.judge_model,
            )
        except GoalCorrupted as e:
            print(f"Error: {e}", file=_sys.stderr)
            return 1
        except CostGuardrailBlocked as e:
            print(f"Error: cost gate blocked dispatch — {e}", file=_sys.stderr)
            return 3
        except GoalConcurrentModification as e:
            print(f"Error: concurrent modification detected — {e}", file=_sys.stderr)
            return 4

        gm.save()

        status_labels = {
            "satisfied": "SATISFIED",
            "max_iterations_reached": "MAX ITERATIONS REACHED",
            "failed": "FAILED",
            "interrupted": "INTERRUPTED",
        }
        outcome_label = status_labels.get(result.status, result.status.upper())
        print(f"\n=== Outcome: {outcome_label} ===")
        print(f"Run ID:              {result.run_id}")
        print(
            f"Iterations:          {len(result.iterations)} / {result.max_iterations}"
        )
        print(f"Total cost:          ${result.total_cost_usd:.4f}")
        print(f"Explanation:         {result.explanation}")
        print(f"Sub-goal '{sg.id}' → {sg.status}")
        print()

        if result.status == "satisfied":
            return 0
        if result.status == "interrupted":
            return 2
        return 1

    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())

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
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .outcome import OutcomeResult

import frontmatter

from ._io import atomic_write, atomic_append_jsonl
from ._platform import get_agents_root
from .exceptions import (
    AtomicAgentsError,
    GoalCorrupted,
    SchemaValidationError,
)


CURRENT_GOAL_SCHEMA_VERSION = 1

VALID_SUB_GOAL_STATUSES = {
    "pending", "in_progress", "complete", "blocked", "abandoned"
}
VALID_PRIORITIES = {"high", "medium", "low"}
VALID_AGENT_MODES = {"reactive", "goal-driven", "hybrid"}


# ──────────────────────────────────────────────────────────────────
# Data model

@dataclass
class SubGoal:
    """One decomposed unit of work toward the parent goal."""
    id: str
    label: str
    status: str = "pending"
    assigned: str | None = None        # role name | "self" | None
    deadline: str | None = None         # YYYY-MM-DD
    blocked_by: str | None = None       # id of another sub_goal
    completed: str | None = None        # YYYY-MM-DD when status=complete
    output: str | None = None           # path to artifact this sub_goal produced
    body: str | None = None             # optional longer description / narrative
    acceptance_criteria: list[str] = field(default_factory=list)  # optional per-sub-goal criteria


@dataclass
class Goal:
    """An agent's persistent objective."""
    schema_version: int
    active: bool
    intent: str
    priority: str
    created: str                       # YYYY-MM-DD
    last_progress_check: str
    success_criteria: list[str]
    sub_goals: list[SubGoal] = field(default_factory=list)
    deadline: str | None = None
    parent_goal: str | None = None
    related_atomic_notes: list[str] = field(default_factory=list)
    related_decisions: list[str] = field(default_factory=list)
    related_canon_pages: list[str] = field(default_factory=list)
    body: str = ""                     # narrative + history (markdown body of goal.md)


@dataclass
class CompletionEvaluation:
    """Result of checking whether a goal's success_criteria are met."""
    all_criteria_met: bool
    sub_goals_complete: int
    sub_goals_total: int
    sub_goals_in_progress: int
    sub_goals_blocked: int
    sub_goals_pending: int
    days_until_deadline: int | None
    overdue: bool


# ──────────────────────────────────────────────────────────────────
# Validation

def validate_goal(goal_dict: dict[str, Any]) -> None:
    """Validate a goal.md frontmatter dict. Raises SchemaValidationError on failure."""
    for field_name in ("schema_version", "active", "intent", "priority",
                        "created", "last_progress_check", "success_criteria"):
        if field_name not in goal_dict:
            raise SchemaValidationError(f"goal missing required field '{field_name}'")

    if goal_dict["schema_version"] != CURRENT_GOAL_SCHEMA_VERSION:
        raise SchemaValidationError(
            f"goal schema_version is {goal_dict['schema_version']}; "
            f"current is {CURRENT_GOAL_SCHEMA_VERSION}"
        )

    if not isinstance(goal_dict["active"], bool):
        raise SchemaValidationError("goal active must be boolean")

    if goal_dict["priority"] not in VALID_PRIORITIES:
        raise SchemaValidationError(
            f"goal priority must be one of {VALID_PRIORITIES}; got '{goal_dict['priority']}'"
        )

    sg = goal_dict.get("sub_goals", [])
    if not isinstance(sg, list):
        raise SchemaValidationError("goal sub_goals must be a list")

    seen_ids = set()
    for i, sub in enumerate(sg):
        if not isinstance(sub, dict):
            raise SchemaValidationError(f"sub_goal[{i}] must be a dict")
        for sf in ("id", "label", "status"):
            if sf not in sub:
                raise SchemaValidationError(f"sub_goal[{i}] missing required '{sf}'")
        if sub["id"] in seen_ids:
            raise SchemaValidationError(f"duplicate sub_goal id: {sub['id']}")
        seen_ids.add(sub["id"])
        if sub["status"] not in VALID_SUB_GOAL_STATUSES:
            raise SchemaValidationError(
                f"sub_goal[{sub['id']}] status must be one of {VALID_SUB_GOAL_STATUSES}; "
                f"got '{sub['status']}'"
            )
        bb = sub.get("blocked_by")
        if bb is not None and not isinstance(bb, str):
            raise SchemaValidationError(f"sub_goal[{sub['id']}] blocked_by must be string or null")
        if bb is not None and bb not in seen_ids:
            raise SchemaValidationError(
                f"sub_goal[{sub['id']}] blocked_by references unknown id '{bb}'"
            )


def validate_agent_mode(mode: str) -> None:
    if mode not in VALID_AGENT_MODES:
        raise SchemaValidationError(
            f"agent mode must be one of {VALID_AGENT_MODES}; got '{mode}'"
        )


# ──────────────────────────────────────────────────────────────────
# IDENTITY.md mode parsing

def parse_agent_mode(identity_path: Path) -> str:
    """Parse the 'Operating mode' section of IDENTITY.md.

    Looks for a line like 'This agent is **reactive**' or 'This agent is goal-driven'.
    Defaults to 'reactive' if no mode declared (per spec/01).
    """
    if not identity_path.exists():
        return "reactive"
    try:
        text = identity_path.read_text(encoding="utf-8")
    except OSError:
        return "reactive"

    # Look for the Operating mode section
    section_match = re.search(
        r"##\s+Operating mode\b.*?(?=\n##\s|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return "reactive"

    section = section_match.group(0)
    # Look for one of the mode keywords
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
        self, agents_root: Path | None = None, agent_name: str = "",
        today: date | None = None,
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
        """Load + validate goal.md. Returns the Goal."""
        if not self.has_goal():
            raise AtomicAgentsError(f"No goal.md at {self.goal_path}")
        try:
            parsed = frontmatter.load(self.goal_path)
        except Exception as e:
            raise GoalCorrupted(f"goal.md unparseable: {e}") from e
        meta = dict(parsed.metadata)
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

        self._goal = Goal(
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
        return self._goal

    def save(self) -> None:
        """Write the current goal back to goal.md. Updates last_progress_check."""
        if self._goal is None:
            raise AtomicAgentsError("No goal loaded to save")
        self._goal.last_progress_check = self.today.isoformat()

        # Build frontmatter dict, dropping None values for cleanliness
        fm: dict[str, Any] = {
            "schema_version": self._goal.schema_version,
            "active": self._goal.active,
            "intent": self._goal.intent,
            "priority": self._goal.priority,
            "created": self._goal.created,
            "last_progress_check": self._goal.last_progress_check,
            "success_criteria": self._goal.success_criteria,
            "sub_goals": [self._serialize_sub_goal(sg) for sg in self._goal.sub_goals],
        }
        if self._goal.deadline:
            fm["deadline"] = self._goal.deadline
        if self._goal.parent_goal:
            fm["parent_goal"] = self._goal.parent_goal
        if self._goal.related_atomic_notes:
            fm["related_atomic_notes"] = self._goal.related_atomic_notes
        if self._goal.related_decisions:
            fm["related_decisions"] = self._goal.related_decisions
        if self._goal.related_canon_pages:
            fm["related_canon_pages"] = self._goal.related_canon_pages

        post = frontmatter.Post(self._goal.body, **fm)
        atomic_write(self.goal_path, frontmatter.dumps(post) + "\n")

    @staticmethod
    def _serialize_sub_goal(sg: SubGoal) -> dict:
        d: dict[str, Any] = {
            "id": sg.id,
            "label": sg.label,
            "status": sg.status,
        }
        if sg.assigned is not None:
            d["assigned"] = sg.assigned
        if sg.deadline:
            d["deadline"] = sg.deadline
        if sg.blocked_by:
            d["blocked_by"] = sg.blocked_by
        if sg.completed:
            d["completed"] = sg.completed
        if sg.output:
            d["output"] = sg.output
        if sg.body:
            d["body"] = sg.body
        if sg.acceptance_criteria:
            d["acceptance_criteria"] = sg.acceptance_criteria
        return d

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

    def mark_in_progress(self, sub_goal_id: str, assigned: str | None = None) -> SubGoal:
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
        self._append_history(f"sub_goal `{sub_goal_id}` → complete" +
                              (f" (output: {output})" if output else ""))
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
            raise AtomicAgentsError(
                f"sub_goal '{sub_goal_id}' cannot block itself"
            )
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
                raise AtomicAgentsError(
                    f"sub_goal {id} cannot block itself"
                )
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
            id=id, label=label, status="pending",
            assigned=assigned, deadline=deadline, blocked_by=blocked_by,
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

        all_done = (
            len(sg) > 0
            and pending == 0 and in_progress == 0 and blocked == 0
        )

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
        intent_slug = re.sub(r"[^a-z0-9]+", "_", self._goal.intent.lower()).strip("_")[:60]
        base_name = f"{self.today.isoformat()}_{intent_slug}"
        archive_path = self.archive_dir / f"{base_name}.md"

        # Increment suffix until a free path is found (collision safety)
        counter = 0
        while archive_path.exists():
            counter += 1
            archive_path = self.archive_dir / f"{base_name}_{counter}.md"

        # Mark inactive + record reason in history before saving to archive
        self._goal.active = False
        self._append_history(f"goal archived ({reason})")

        # Write archive first — unlink goal.md only after successful write
        post = frontmatter.Post(self._goal.body, **{
            "schema_version": self._goal.schema_version,
            "active": False,
            "intent": self._goal.intent,
            "priority": self._goal.priority,
            "created": self._goal.created,
            "last_progress_check": self.today.isoformat(),
            "success_criteria": self._goal.success_criteria,
            "sub_goals": [self._serialize_sub_goal(sg) for sg in self._goal.sub_goals],
            "archived_at": self.today.isoformat(),
            "archive_reason": reason,
        })
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
            deadline_note = f"  ({ev.days_until_deadline} days remaining)" if ev.days_until_deadline is not None else ""
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
            lines.append("✓ All sub-goals complete. Run `goal complete <agent>` to archive.")

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
            in_progress = [sg for sg in self._goal.sub_goals if sg.status == "in_progress"]
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

        Behavior:
        - Refuses if sub-goal is not ``pending`` or ``in_progress`` (raises GoalCorrupted).
        - Refuses if sub-goal has unresolved blocked_by dependencies (raises GoalCorrupted).
        - Marks sub-goal ``in_progress`` before dispatch (idempotent if already in_progress).
        - Builds outcome ``description`` from the sub-goal's label + body + acceptance criteria.
        - Calls OutcomeRunner(agents_root, agent_name).run(description, rubric,
          max_iterations, extra_context, judge_model).
        - Maps terminal state to sub-goal status:
            - satisfied              → complete
            - max_iterations_reached → blocked (reason cites run_id)
            - failed                 → blocked (reason cites judge explanation)
            - interrupted            → stays in_progress (caller decides whether to retry)
        - Records a dedicated ``sub_goal_outcome_dispatched`` event in goal_history.jsonl.
        - Returns (OutcomeResult, updated SubGoal).
        """
        # Lazy import to avoid circular imports
        from .outcome import OutcomeRunner  # noqa: PLC0415

        if self._goal is None:
            self.load()

        sg = self._require_sub_goal(sub_goal_id)

        # Refuse degenerate states
        if sg.status not in ("pending", "in_progress"):
            raise GoalCorrupted(
                f"sub_goal '{sub_goal_id}' is '{sg.status}'; "
                f"dispatch_as_outcome only accepts pending or in_progress sub-goals"
            )

        # Refuse if blocked_by is unresolved
        if sg.blocked_by:
            blocker = self.find_sub_goal(sg.blocked_by)
            if blocker is None:
                raise GoalCorrupted(
                    f"sub_goal '{sub_goal_id}' blocked_by '{sg.blocked_by}' which does not exist; "
                    f"goal graph is inconsistent — operator must repair goal.md"
                )
            if blocker.status != "complete":
                raise GoalCorrupted(
                    f"sub_goal '{sub_goal_id}' has unresolved blocked_by dependency "
                    f"'{sg.blocked_by}' (status: {blocker.status}); "
                    f"resolve the blocker before dispatching as outcome"
                )

        # Mark in_progress before running so observers see the dispatch in-flight
        if sg.status == "pending":
            sg.status = "in_progress"
            self._append_history(f"sub_goal `{sub_goal_id}` → in_progress (outcome dispatch)")

        # Build the outcome description from the sub-goal
        description = self._build_outcome_description_from_sub_goal(sg)

        # Run the outcome
        runner = OutcomeRunner(
            agents_root=self.agents_root,
            agent_name=self.agent_name,
            judge_model=judge_model,
        )
        result = runner.run(
            description=description,
            rubric=rubric,
            max_iterations=max_iterations,
            extra_context=extra_context,
        )

        # Map terminal state to sub-goal status
        applied_status: str
        if result.status == "satisfied":
            sg.status = "complete"
            sg.completed = self.today.isoformat()
            applied_status = "complete"
            self._append_history(
                f"sub_goal `{sub_goal_id}` → complete "
                f"(outcome {result.run_id} satisfied)"
            )
        elif result.status == "max_iterations_reached":
            sg.status = "blocked"
            sg.blocked_by = None  # no sub-goal blocker — narrative in history
            applied_status = "blocked"
            self._append_history(
                f"sub_goal `{sub_goal_id}` → blocked "
                f"(max_iterations_reached on outcome {result.run_id})"
            )
        elif result.status == "failed":
            sg.status = "blocked"
            sg.blocked_by = None
            applied_status = "blocked"
            explanation_short = (result.explanation or "")[:200]
            self._append_history(
                f"sub_goal `{sub_goal_id}` → blocked "
                f"(outcome failed — {explanation_short})"
            )
        else:
            # interrupted — leave in_progress; caller decides whether to retry
            applied_status = "in_progress"
            self._append_history(
                f"sub_goal `{sub_goal_id}` stays in_progress "
                f"(outcome {result.run_id} interrupted)"
            )

        # Record dedicated JSONL history entry
        self._append_goal_history_jsonl({
            "ts": datetime.now().astimezone().isoformat(),
            "event": "sub_goal_outcome_dispatched",
            "sub_goal_id": sub_goal_id,
            "outcome_run_id": result.run_id,
            "terminal_state": result.status,
            "applied_status": applied_status,
            "iterations": len(result.iterations),
            "total_cost_usd": result.total_cost_usd,
        })

        return result, sg

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
            self._goal.body = self._goal.body.rstrip() + f"\n\n{history_marker} (auto-appended)\n"
        self._goal.body = self._goal.body.rstrip() + f"\n- {self.today.isoformat()} — {entry}"

    def _append_goal_history_jsonl(self, entry: dict) -> None:
        """Append a structured event to goal_history.jsonl in the agent root."""
        history_path = self.agent_root / "goal_history.jsonl"
        atomic_append_jsonl(history_path, json.dumps(entry))


# ──────────────────────────────────────────────────────────────────
# CLI

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="atomic-agents.goal",
        description="Goal manager for goal-driven and hybrid Atomic Agents",
    )
    parser.add_argument("--agents-root", default=None,
                        help="override ATOMIC_AGENTS_ROOT")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Print current goal status")
    p_status.add_argument("agent")

    p_next = sub.add_parser("next", help="Print next dispatchable sub-goal")
    p_next.add_argument("agent")

    p_advance = sub.add_parser("advance", help="Advance a sub-goal status")
    p_advance.add_argument("agent")
    p_advance.add_argument("sub_goal_id")
    p_advance.add_argument("--complete", action="store_true",
                           help="mark complete (default: mark in_progress)")
    p_advance.add_argument("--assigned", default=None)
    p_advance.add_argument("--output", default=None,
                           help="path to artifact (when --complete)")

    p_abandon = sub.add_parser("abandon", help="Abandon the goal — non-destructive archive")
    p_abandon.add_argument("agent")
    p_abandon.add_argument("--reason", required=True)

    p_complete = sub.add_parser("complete", help="Mark the entire goal complete + archive")
    p_complete.add_argument("agent")

    p_report = sub.add_parser("report", help="Periodic progress report (suitable for journal)")
    p_report.add_argument("agent")

    p_dispatch_outcome = sub.add_parser(
        "dispatch-outcome",
        help="Run a sub-goal as an outcome loop and machine-decide completion",
    )
    p_dispatch_outcome.add_argument("agent")
    p_dispatch_outcome.add_argument("sub_goal_id")
    p_dispatch_outcome.add_argument(
        "--rubric", required=True,
        help="path to rubric file, or 'inline:<text>'",
    )
    p_dispatch_outcome.add_argument(
        "--max-iterations", type=int, default=3,
        help="max iterations for the outcome loop (default 3)",
    )
    p_dispatch_outcome.add_argument(
        "--judge-model", default=None,
        help="override the judge model",
    )

    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root else get_agents_root()
    )

    try:
        gm = GoalManager(agents_root, args.agent)
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
            print("No dispatchable sub-goal — all are complete, in progress, or blocked.")
            return 0

    if args.cmd == "advance":
        gm.load()
        if args.complete:
            sg = gm.mark_complete(args.sub_goal_id, output=args.output)
            print(f"✓ {sg.id} → complete")
        else:
            sg = gm.mark_in_progress(args.sub_goal_id, assigned=args.assigned)
            print(f"▶ {sg.id} → in_progress" +
                  (f" (assigned: {sg.assigned})" if sg.assigned else ""))
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
            rubric = args.rubric[len("inline:"):]
        else:
            rubric = Path(args.rubric)

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
        print(f"Iterations:          {len(result.iterations)} / {result.max_iterations}")
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

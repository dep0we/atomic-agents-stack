"""Canonical types for the GoalBackend Protocol (spec/41).

NOTE: Goal and SubGoal are MUTABLE dataclasses — deliberate divergence from
the frozen-DTO convention in logs/types.py. The goal layer is a state machine:
apply_transition() mutates sub-task status in-place before the durable write.
Freezing these types would break every status-transition method
(mark_in_progress, mark_complete, mark_blocked, etc. all mutate SubGoal.status
directly). See CLAUDE.md Principle #3 (layers compose; they do not merge) and
the goal-dataclass-shape ruling in the arc governing this module.

CompletionEvaluation is frozen=True — it is a pure value object returned by
evaluate_completion(), not a state-bearing object.

GoalCapabilities is frozen=True — frozen dataclass convention matches every
other *Capabilities type (LockCapabilities, LogCapabilities, etc.) for
backward-compatible additive extension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..exceptions import SchemaValidationError
from .._export_base import ExportableResult

# ──────────────────────────────────────────────────────────────────
# Canonical goal constants — single source of truth.
#
# These live here (in the canonical-types module) rather than being duplicated
# in filesystem.py and _goal_impl.py so a future schema bump cannot leave the
# backend's load_goal() validation and GoalManager's validate_goal() disagreeing
# about what a valid goal.md is. Both sites import from here.

CURRENT_GOAL_SCHEMA_VERSION = 1
VALID_SUB_GOAL_STATUSES = {"pending", "in_progress", "complete", "blocked", "abandoned"}
VALID_PRIORITIES = {"high", "medium", "low"}
VALID_AGENT_MODES = {"reactive", "goal-driven", "hybrid"}


# ──────────────────────────────────────────────────────────────────
# Mutable state-machine types (diverge from frozen-DTO convention)


@dataclass
class SubGoal:
    """One decomposed unit of work toward the parent goal.

    MUTABLE — apply_transition() modifies fields in-place before the backend
    writes durably. Deliberate divergence from logs/types.py frozen-DTO pattern.
    """

    id: str
    label: str
    status: str = "pending"
    assigned: str | None = None  # role name | "self" | None
    deadline: str | None = None  # YYYY-MM-DD
    blocked_by: str | None = None  # id of another sub_goal
    completed: str | None = None  # YYYY-MM-DD when status=complete
    output: str | None = None  # path to artifact this sub_goal produced
    body: str | None = None  # optional longer description / narrative
    acceptance_criteria: list[str] = field(
        default_factory=list
    )  # optional per-sub-goal criteria


@dataclass
class Goal:
    """An agent's persistent objective.

    MUTABLE — the goal layer is a state machine. archive() sets active=False;
    apply_transition() mutates sub_goals in-place. The body field holds the
    markdown prose + ## History section, accumulated by the manager layer.

    Deliberate divergence from frozen-DTO convention. See module docstring.
    """

    schema_version: int
    active: bool
    intent: str
    priority: str
    created: str  # YYYY-MM-DD
    last_progress_check: str
    success_criteria: list[str]
    sub_goals: list[SubGoal] = field(default_factory=list)
    deadline: str | None = None
    parent_goal: str | None = None
    related_atomic_notes: list[str] = field(default_factory=list)
    related_decisions: list[str] = field(default_factory=list)
    related_canon_pages: list[str] = field(default_factory=list)
    body: str = ""  # narrative + history (markdown body of goal.md)


# ──────────────────────────────────────────────────────────────────
# Frozen value objects


@dataclass(frozen=True)
class CompletionEvaluation:
    """Result of checking whether a goal's success_criteria are met.

    Frozen — pure value returned by evaluate_completion(). Not state-bearing.
    """

    all_criteria_met: bool
    sub_goals_complete: int
    sub_goals_total: int
    sub_goals_in_progress: int
    sub_goals_blocked: int
    sub_goals_pending: int
    days_until_deadline: int | None
    overdue: bool


@dataclass(frozen=True)
class GoalCapabilities:
    """Per-backend capability declaration for GoalBackend (spec/41).

    Matches the frozen-dataclass convention of every other *Capabilities type.
    All capability booleans have defaults=False so new fields can be added at
    the end without breaking existing instantiation sites.

    Fields:
        backend_id: stable backend identifier string (required, no default).
        supports_canonical_export: True when the backend implements the
            spec/40 Exportable Protocol. FilesystemGoalBackend=True.
            Default False so existing instantiation sites without this kwarg
            keep working (backward-compatibility pattern from LogCapabilities).
        supports_archive: True when archive_goal() and list_archived() are
            implemented. FilesystemGoalBackend=True.
        supports_history_query: True when the backend implements
            append_history_event(); history records are enumerable only via
            export() — there is no dedicated history-query method on the
            Protocol. FilesystemGoalBackend=True.

    Field ordering: backend_id (required, no default) first so positional
    construction GoalCapabilities("filesystem") is meaningful; capability
    booleans with defaults last so adding a new field at the end does not
    break existing instantiation sites.
    """

    backend_id: str
    supports_canonical_export: bool = False
    supports_archive: bool = False
    supports_history_query: bool = False


# ──────────────────────────────────────────────────────────────────
# Export result type


@dataclass
class GoalExport(ExportableResult):
    """Canonical export from a GoalBackend (spec/40 §"Per-backend export contracts").

    Composite ExportableResult carrying three components:

    1. goal_md_bytes: bytes of the active goal.md, CRLF/BOM-normalized (read via
       path.read_bytes(), then _normalize_crlf — NOT re-serialized through a
       frontmatter library, so the file's exact content is preserved modulo
       CRLF→LF and a leading BOM strip). Empty bytes when no goal.md is present.
    2. history_records_with_bytes: JSONL lines from goal_history.jsonl as bytes.
       Each element is a single line, newline-terminated.
       For Tier A (filesystem), lines are CRLF/BOM-normalized and NOT
       re-serialized through json.dumps, so each line's key order is preserved
       exactly as written. They are line-normalized, not strict byte-for-byte:
       a final line lacking a trailing newline (only reachable via a hand-edited
       or alternate-backend file — atomic_append_jsonl always terminates lines)
       is exported with one appended.
       For Tier B (DB backends), lines are reconstructed with ts-first key order
       via json.dumps(to_dict()).encode("utf-8") + b"\\n".
    3. archived_goals_with_bytes: list of (archive_slug, bytes) tuples for
       each file in goal_archive/. CRLF/BOM-normalized bytes per archive file
       (read_bytes() + _normalize_crlf — same normalization as goal_md_bytes,
       not raw passthrough). Empty list when goal_archive/ does not exist.

    Snapshot consistency: export() reads goal.md first (authoritative state),
    then goal_history.jsonl, then archives. A concurrent apply_transition() that
    completes between these reads may cause the exported history to contain events
    whose goal.md effect is not reflected in the snapshot. Callers requiring
    strict consistency MUST hold the agent LockBackend before calling export().
    This is the acknowledged spec/40 MUST 7 snapshot-consistency bound.

    Fields:
        goal_md_bytes: CRLF/BOM-normalized bytes of goal.md or b"" when absent.
        history_records_with_bytes: list of CRLF/BOM-normalized, newline-
            terminated JSONL line bytes.
        archived_goals_with_bytes: list of (slug, normalized_bytes) tuples.
        backend_id: stable backend identifier.
        scope: agent root path as a string.
    """

    goal_md_bytes: bytes
    history_records_with_bytes: list[bytes]
    archived_goals_with_bytes: list[tuple[str, bytes]]
    backend_id: str
    scope: str


# ──────────────────────────────────────────────────────────────────
# Shared serialization helper (used by GoalManager and FilesystemGoalBackend)


def serialize_sub_goal(sg: SubGoal) -> dict[str, Any]:
    """Serialize a SubGoal to a plain dict for frontmatter embedding."""
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


def build_goal_frontmatter(goal: Goal) -> dict[str, Any]:
    """Build the frontmatter dict for a Goal (excluding None optional fields)."""
    fm: dict[str, Any] = {
        "schema_version": goal.schema_version,
        "active": goal.active,
        "intent": goal.intent,
        "priority": goal.priority,
        "created": goal.created,
        "last_progress_check": goal.last_progress_check,
        "success_criteria": goal.success_criteria,
        "sub_goals": [serialize_sub_goal(sg) for sg in goal.sub_goals],
    }
    if goal.deadline:
        fm["deadline"] = goal.deadline
    if goal.parent_goal:
        fm["parent_goal"] = goal.parent_goal
    if goal.related_atomic_notes:
        fm["related_atomic_notes"] = goal.related_atomic_notes
    if goal.related_decisions:
        fm["related_decisions"] = goal.related_decisions
    if goal.related_canon_pages:
        fm["related_canon_pages"] = goal.related_canon_pages
    return fm


# ──────────────────────────────────────────────────────────────────
# Shared validation (single source — used by GoalManager AND FilesystemGoalBackend)


def validate_goal(goal_dict: dict[str, Any]) -> None:
    """Validate a goal.md frontmatter dict. Raises SchemaValidationError on failure.

    THE single goal validator. Both GoalManager.load() (above the Protocol) and
    FilesystemGoalBackend.load_goal() (the backend, future-canonical reader) call
    this so a corrupt goal.md — including a dangling or wrong-typed blocked_by
    reference — is rejected identically on both paths. A second, weaker copy is
    exactly the cross-path divergence Principle #2 (single source of validation)
    and Principle #5 (a corrupt dependency graph must not load silently) forbid.
    """
    for field_name in (
        "schema_version",
        "active",
        "intent",
        "priority",
        "created",
        "last_progress_check",
        "success_criteria",
    ):
        if field_name not in goal_dict:
            raise SchemaValidationError(f"goal missing required field '{field_name}'")

    # Coerce schema_version to int before comparing — matches
    # FilesystemGoalBackend.read_schema_version()'s int() coercion so the two
    # do not disagree about a hand-edited `schema_version: "1"` (string). The
    # common int case (atomic_write produces int) is unaffected.
    try:
        schema_version = int(goal_dict["schema_version"])
    except (TypeError, ValueError):
        raise SchemaValidationError(
            f"goal schema_version is not an integer: {goal_dict['schema_version']!r}"
        )
    if schema_version != CURRENT_GOAL_SCHEMA_VERSION:
        raise SchemaValidationError(
            f"goal schema_version is {schema_version}; "
            f"current is {CURRENT_GOAL_SCHEMA_VERSION}"
        )

    if not isinstance(goal_dict["active"], bool):
        raise SchemaValidationError("goal active must be boolean")

    if goal_dict["priority"] not in VALID_PRIORITIES:
        raise SchemaValidationError(
            f"goal priority must be one of {VALID_PRIORITIES}; "
            f"got '{goal_dict['priority']}'"
        )

    sg_list = goal_dict.get("sub_goals", [])
    if not isinstance(sg_list, list):
        raise SchemaValidationError("goal sub_goals must be a list")

    seen_ids: set[str] = set()
    for i, sub in enumerate(sg_list):
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
                f"sub_goal[{sub['id']}] status must be one of "
                f"{VALID_SUB_GOAL_STATUSES}; got '{sub['status']}'"
            )
        bb = sub.get("blocked_by")
        if bb is not None and not isinstance(bb, str):
            raise SchemaValidationError(
                f"sub_goal[{sub['id']}] blocked_by must be string or null"
            )
        if bb is not None and bb not in seen_ids:
            raise SchemaValidationError(
                f"sub_goal[{sub['id']}] blocked_by references unknown id '{bb}'"
            )


def validate_agent_mode(mode: str) -> None:
    """Validate an agent mode string. Raises SchemaValidationError on failure."""
    if mode not in VALID_AGENT_MODES:
        raise SchemaValidationError(
            f"agent mode must be one of {VALID_AGENT_MODES}; got '{mode}'"
        )

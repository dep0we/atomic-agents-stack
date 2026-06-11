# spec/41: GoalBackend Protocol

> **Status:** DRAFT at PR 1 (issue #425). Conformance suite covers all 9 Implementer Contract MUSTs for `FilesystemGoalBackend` (`test_goal_backend_conformance.py`) plus filesystem-specific tests (`test_goal_filesystem.py`). Goal is also registered in the shared #379 export conformance harness (`test_export_protocol_conformance.py`) and the capability-advertisement harness (`test_export_capability_advertisement.py`: 2 goal-specific tests — `test_goal_backend_advertises_canonical_export`, `test_goal_backend_is_exportable` — plus the shared `test_all_capability_flags_are_bool` parametrization extended to cover goal). The four pre-existing goal tests (`test_goal.py`, `test_agent_goal_loading.py`, `test_dashboard_goals.py`, `test_goal_outcome_composition.py`) remain the zero-behavior-change regression guard; they are not modified by this protocol change (as of PR 1, 2026-06-11).

---

## Origin

Carved out from the flat `goal.py` module in place since the framework's initial release. Filed as [#425](https://github.com/dep0we/atomic-agents-stack/issues/425) as a follow-on to the canonical-export contract (spec/40, #379) which required `GoalExport` to be defined before it could be composed into the Exportable Protocol.

The motivation: `goal.py` held GoalManager (a runtime object), Goal/SubGoal dataclasses (domain model), schema constants, and CLI entry point all in a single flat module. The backend protocol separates the storage abstraction (GoalBackend) from the runtime behavior (GoalManager, now in `_goal_impl.py`), following the pattern established by MemoryBackend (spec/20), LogBackend (spec/22), and SecretBackend (spec/38).

**Cross-links:**
- spec/12. Goals and Intent. The narrative contract: what goal.md contains, the History section, sub-goal lifecycle.
- spec/14. Outcomes. The `dispatch_as_outcome()` pathway that transitions sub-goals to terminal status.
- spec/40. Canonical Export. `GoalExport` is a first-class ExportableResult; `FilesystemGoalBackend` implements `Exportable`.
- spec/27. Doctor. `check_goal_backend()` uses the dual-probe pattern (list_archived + load_goal).

---

## Shipping plan (1 PR)

- **PR 1 (this PR).** Protocol scaffold + dataclasses + capability advertisement + `FilesystemGoalBackend` reference impl + compatibility re-export (`goal.py` → `goal/` package; the documented `from atomic_agents.goal import GoalManager` path stays a **supported** public import, NOT deprecated — no `DeprecationWarning` is emitted because the path is intentionally permanent) + `GoalManager` relocated to `_goal_impl.py` + single shared `validate_goal()`/constants in `goal/types.py` + `GoalExport` (an `ExportableResult` subclass) wired into `atomic_agents.export` + `check_goal_backend()` doctor check + full conformance suite + filesystem-specific tests + spec/41 DRAFT. Goal-outcome composition (a coordinator wiring `GoalBackend` + `OutcomeRunner` with a pre-dispatch cost gate) is deferred to a follow-up that wires the backend into the runtime, with its own tests — it is NOT part of this scaffolding PR.

---

## Overview

`GoalBackend` is the **fourteenth** backend Protocol in the protocol-pattern series. It abstracts goal state persistence (reading, writing, transitioning sub-goals, archiving completed goals, and exporting goal state) behind a Protocol so the framework's core stays small and alternate goal substrates (SQLite, Postgres, project-management APIs) can drop in without forking.

The framework's existing flat `goal.py` module has been **superseded**: GoalManager now lives in `_goal_impl.py`; Goal/SubGoal/CompletionEvaluation dataclasses live in `goal/types.py`; the `atomic_agents.goal` import path is preserved via `goal/__init__.py`'s `__getattr__` lazy loader.

---

## Module layout

```
atomic_agents/goal/
├── __init__.py        # registry: register_goal_backend /
│                      # get_goal_backend / list_goal_backends +
│                      # get_default_goal_backend factory +
│                      # __getattr__ lazy loader (GoalManager, validate_goal, …)
├── types.py           # canonical types: Goal, SubGoal, CompletionEvaluation,
│                      # GoalCapabilities, GoalExport (ExportableResult subclass),
│                      # serialize_sub_goal(), build_goal_frontmatter(),
│                      # the single validate_goal()/validate_agent_mode() +
│                      # the goal constants (CURRENT_GOAL_SCHEMA_VERSION, …)
├── backend.py         # GoalBackend Protocol (@runtime_checkable)
└── filesystem.py      # FilesystemGoalBackend reference implementation

atomic_agents/_goal_impl.py   # GoalManager (runtime behavior); imports the
                               # canonical types + the single validate_goal()
                               # from goal/types.py (no second copy); plus
                               # parse_agent_mode, parse_agent_mode_text,
                               # main() CLI entry point
atomic_agents/_export_base.py # ExportableResult marker base (leaf module both
                               # goal/types.py and export/types.py import to
                               # avoid a circular import)
```

Package name `goal/` replaces the flat `goal.py`. Python resolves `atomic_agents.goal` to the package; the shim's `__getattr__` preserves backward-compatible access to `GoalManager`, `Goal`, `SubGoal`, etc. via `from atomic_agents.goal import GoalManager`.

---

## Deliberate divergence: mutable dataclasses

`Goal` and `SubGoal` are **mutable** dataclasses (`@dataclass`, not `@dataclass(frozen=True)`). This is a deliberate divergence from the frozen-DTO convention used by `LogEntry` (spec/22) and other read-side types.

**Rationale:** `Goal` and `SubGoal` are state-machine objects. A sub-goal transitions from `pending` → `in_progress` → `complete` (or `blocked`/`abandoned`; see `VALID_SUB_GOAL_STATUSES` in `goal/types.py` and spec/12's authoritative sub-goal status enum). GoalManager mutates status in-place during `apply_transition()`. Freezing them would require allocating a new `SubGoal` object for every transition call; the resulting code is more complex and harder to read than the mutable equivalent, with no correctness payoff (goal state is single-owner inside GoalManager). `CompletionEvaluation` is frozen because it is a pure value object with no lifecycle.

---

## WritePolicy applicability

`GoalBackend` is **read-write with an atomic transition primitive**. The framework does not offer a WritePolicy for goal transitions because sub-goal state is strictly ordered (pending → in_progress → terminal). Unlike memory notes (which can use `OVERWRITE` or `MERGE` policies) or log entries (append-only), goal transitions must be:

1. Mutually exclusive (two callers cannot both claim `in_progress` for the same sub-goal).
2. Ordered (you cannot skip from `pending` to `complete` without a `dispatch_as_outcome()` call).
3. Durable (the transition must persist before the caller proceeds).

`apply_transition()` provides **mutual exclusion (1)** and **durability (3)** directly: it holds the exclusive goal lock across the `goal.md` write and the JSONL append, so the transition is atomic and persisted before the lock is released. None of the three is a WritePolicy enum.

Two distinct concerns are easy to conflate here; the backend enforces exactly one of them:

- **(a) Enum-domain validation — ENFORCED by the backend, a conformance requirement.** `to_status` MUST be a member of `VALID_SUB_GOAL_STATUSES` (the spec/12 status enum: `pending`, `in_progress`, `complete`, `blocked`, `abandoned`). `apply_transition()` MUST reject an unknown `to_status` **fail-closed, before any write**, raising `SchemaValidationError`. This closes the write-time/read-time validation asymmetry: a `to_status` the backend's own `load_goal()` would reject must never be persisted. The `fields` parameter MUST NOT be allowed to overwrite `sub_goal.status` through the side door (the reference impl skips any `status` key in `fields`; `to_status` is the authoritative status channel). The conformance suite pins both the front-door (`to_status`) and side-door (`fields={"status": …}`) cases across **every** backend.
- **(b) Transition-graph legality (ordering) — NOT enforced inside `apply_transition()`.** Which `from → to` edges are legal (e.g. you cannot skip `pending → complete` without a `dispatch_as_outcome()` call) is pure computation that stays ABOVE the Protocol in `GoalManager` (see `backend.py`'s "computation stays above the Protocol" note). The backend is the atomic write primitive for a status already known to be a valid enum member; it is not the state-machine *ordering* validator.

---

## Protocol surface

```python
@runtime_checkable
class GoalBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    def load_goal(self, agent_id: str) -> Goal: ...
    def save_goal(self, agent_id: str, goal: Goal) -> None: ...
    def apply_transition(
        self,
        agent_id: str,
        sub_goal_id: str,
        to_status: str,
        fields: dict[str, Any],
        history_prose: str,
        history_event: dict[str, Any],
    ) -> Goal: ...
    def append_history_event(
        self, agent_id: str, event: dict[str, Any]
    ) -> None: ...

    def archive_goal(self, agent_id: str, reason: str = "completed") -> str: ...
    def list_archived(self, agent_id: str) -> list[str]: ...

    def read_schema_version(self, agent_id: str) -> int | None: ...
    def goal_text(self, agent_id: str) -> str: ...

    def export(self, query: Any = None) -> GoalExport: ...
    def export_all(self) -> GoalExport: ...
    def capabilities(self) -> GoalCapabilities: ...
```

---

## Capability advertisement

`GoalCapabilities` is a frozen dataclass with four fields:

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `backend_id` | `str` | (required) | Stable identifier matching `backend_id` property |
| `supports_canonical_export` | `bool` | `False` | Implements `export()` per spec/40 |
| `supports_archive` | `bool` | `False` | Implements `archive_goal()` and `list_archived()` |
| `supports_history_query` | `bool` | `False` | Implements `append_history_event()`; history is enumerable only via `export()` — there is no dedicated history-query method on the Protocol |

`FilesystemGoalBackend` advertises all three capability flags as `True`.

---

## Schema migration note

`read_schema_version()` returns the integer `schema_version` from the goal's frontmatter, or `None` when goal.md is absent OR present without a `schema_version` key; it raises `GoalCorrupted` when goal.md is present but unparseable, or carries a `schema_version` that is not integer-coercible. It is **NOT** a `MigratableUnit` and MUST NOT be registered with the migration runner (spec/03), whose `unit_type` is locked to `Literal["memory", "wiki"]` and therefore cannot represent goal state. Goal schema evolution is **GoalBackend-internal** — handled inside the backend's own `load_goal()`/`save_goal()` via the `CURRENT_GOAL_SCHEMA_VERSION` check in `validate_goal()`, entirely separate from the spec/03 migration runner. `read_schema_version()` is a diagnostic-only probe for the doctor and backend-internal version checks, not a migration controller.

---

## Implementer Contract (9 MUSTs)

These MUSTs bind every conforming GoalBackend implementation. The conformance test suite in `tests/test_goal_backend_conformance.py` covers all nine.

### MUST 1 — Side-effect-free construction

`__init__` MUST NOT touch the filesystem, open network connections, acquire locks, or create directories. The object must be fully constructed (and functional for subsequent calls) with no I/O. A backend constructed for an agent root that does not exist must succeed; methods that require the directory to exist must create it lazily on first write.

### MUST 2 — Capability honesty

`capabilities()` MUST return a `GoalCapabilities` instance whose boolean fields honestly reflect what the backend implements. A backend that does not implement `archive_goal()` (beyond raising `NotImplementedError`) MUST advertise `supports_archive=False`. Advertising a capability and then raising `NotImplementedError` on the corresponding method is a conformance violation.

### MUST 3 — `goal_text()` is a read-only slice

`goal_text(agent_id)` MUST return the raw text of `goal.md` as a `str`, or `''` when goal.md is absent. It MUST NOT modify goal.md, create the agent root directory, or write any file.

### MUST 4 — `save_goal()` write-what-I-give-you

`save_goal(agent_id, goal)` MUST write the `Goal` object verbatim. It MUST NOT silently mutate `goal.last_progress_check`, `goal.active`, or any other field before writing. If the backend wants to update a derived field (e.g., `last_progress_check`), that mutation MUST happen explicitly in the caller before `save_goal()` is called.

### MUST 5 — `load_goal()` / `save_goal()` round-trip fidelity

`load_goal(agent_id)` MUST return a `Goal` whose fields match what `save_goal(agent_id, goal)` wrote. Specifically: `goal.intent`, `goal.priority`, `goal.active`, `goal.sub_goals[*].status`, and the `## History` body section must all survive a round-trip.

### MUST 6 — `apply_transition()` is a single atomic unit (and enum-validates fail-closed)

`apply_transition()` MUST write the updated `goal.md` AND append the `history_event` to `goal_history.jsonl` as a single unit. If the backend uses filesystem locking (e.g., `fcntl.flock`), both writes MUST complete before the lock is released. A crash between the two writes is permissible; a crash that leaves `goal_history.jsonl` written but `goal.md` un-updated is a conformance violation (the JSONL is the audit trail for a committed state change).

`apply_transition()` MUST also **enum-validate fail-closed**: if `to_status` is not a member of `VALID_SUB_GOAL_STATUSES`, it MUST raise `SchemaValidationError` **before any write** (no partial `goal.md`, no orphan JSONL line). The `fields` parameter MUST NOT overwrite the sub-goal status — `to_status` is the sole status channel, so a `fields={"status": <anything>}` MUST NOT reach the persisted `goal.md`. This guarantees the backend never persists a `goal.md` its own `load_goal()` would reject (no write-time/read-time validation asymmetry). Transition-graph *ordering* legality is NOT enforced here — see §"WritePolicy applicability" (a) vs (b).

### MUST 7, 8, 9 — `archive_goal()` behavioral constraints

**MUST 7 (write ordering):** `archive_goal()` MUST write the archive file to `goal_archive/<slug>.md` BEFORE unlinking `goal.md`. If the process crashes between the two operations, the archive file exists and `goal.md` also exists — a recoverable state. There is no window where both are absent.

**MUST 8 (collision-safe slug):** When an archive file with the computed slug already exists, `archive_goal()` MUST append a numeric suffix (`_1`, `_2`, …) until a free name is found. The loop MUST terminate (backends may impose a reasonable maximum, e.g., 999).

**MUST 9 (idempotency on retry-after-unlink):** The idempotency condition is — no `goal.md` present AND at least one archive file present. Under that condition, `archive_goal()` MUST return the most-recently-modified archive slug in `goal_archive/` without writing a second file, rather than raising. This handles the retry-after-crash case (a prior partial run completed the unlink step). With no `goal.md` present the intent slug cannot be reconstructed, so the newest archive is returned; this is a best-effort retry guard, correct for the common single-goal case. (Per-goal exactness for agents that have archived many goals over their lifetime is tracked as a follow-up; it does not change the Protocol contract.)

---

## `apply_transition()` JSONL key ordering

Every `history_event` dict appended by `apply_transition()` or `append_history_event()` MUST be serialized with `"ts"` as the **first key** in the JSON object. This is enforced by `_make_history_event()` which builds an ordered dict starting with `"ts"` and `"event"` before any caller-supplied extra fields. The ordering is load-bearing for log-reader tools that extract timestamps from the first JSON field without full deserialization.

---

## `export()` contract (spec/40 Tier A passthrough)

`export()` returns a `GoalExport` with three byte fields:

| Field | Content |
|-------|---------|
| `goal_md_bytes` | Raw bytes of `goal.md`, CRLF-normalized and BOM-stripped. `b""` when absent. |
| `history_records_with_bytes` | Lines of `goal_history.jsonl`, CRLF/BOM-normalized and newline-terminated. `[]` when absent. |
| `archived_goals_with_bytes` | List of `(slug, bytes)` tuples for each file in `goal_archive/`, CRLF-normalized. `[]` when dir absent. |

All bytes are **Tier A passthrough** (spec/40 §"Tier A passthrough"): the backend reads the raw file bytes and CRLF/BOM-normalizes; it does NOT re-serialize through `json.dumps()` or frontmatter libraries, so each line's insertion-order key ordering is preserved as written. History export is **line-normalized, not strict byte-for-byte**: a final history line lacking a trailing newline (only reachable via a hand-edited or alternate-backend file — `atomic_append_jsonl` always terminates lines) is exported with one appended. This contract is pinned by `test_goal_history_export_normalizes_trailing_newline` in `test_export_protocol_conformance.py`.

---

## Goal-outcome composition (deferred to a follow-up)

This scaffolding PR ships the `GoalBackend` Protocol and `FilesystemGoalBackend`, but does NOT wire the backend into the live runtime — `GoalManager.dispatch_as_outcome()` (in `_goal_impl.py`) remains the live CLI path and continues to do direct file I/O.

A future PR (filed as #448) will add a goal-outcome coordinator — a thin function (not a `GoalBackend` method) that composes the backend with `OutcomeRunner`, adding a pre-dispatch cost gate (`_check_cost_guardrails(critical=False)` → raise `CostGuardrailBlocked` on a blocked cap), the `pending → in_progress` pre-transition, the outcome run (without holding the goal lock), and the terminal transition. It is deferred rather than shipped here because (a) it needs the backend actually adopted by the runtime to be exercised, and (b) shipping an un-wired, parallel copy of the cost-guardrail path risks silent drift from the live gate (CLAUDE.md Principle #4). It will ship WITH its own tests when the backend is adopted.

The coordinator will NOT be a `GoalBackend` method: it needs both the backend AND the runtime (`AtomicAgent`, `OutcomeRunner`) simultaneously, and making it a Protocol method would invert the dependency direction (the backend must not depend on the runtime).

---

## Operator override surface

As of PR 1 (this scaffolding PR), GoalBackend is operator-configurable via **two** surfaces — the env var and the factory. The `AtomicAgent` constructor kwarg + public attribute are **deferred to the runtime-wiring PR (#448)**, mirroring how the goal-outcome coordinator is deferred above. This PR does NOT wire the backend into `AtomicAgent.__init__`, so there is no `goal_backend` constructor parameter or `AtomicAgent.goal_backend` attribute today.

| Surface | Mechanism | Status |
|---------|-----------|--------|
| Environment variable | `ATOMIC_AGENTS_GOAL_BACKEND=<backend_id>` | Ships in PR 1 |
| Factory function | `get_default_goal_backend(agent_root)` | Ships in PR 1 |
| Constructor kwarg | `AtomicAgent(goal_backend=my_backend)` + `AtomicAgent.goal_backend` attribute | Deferred to #448 (runtime wiring) |

The doctor's `check_goal_backend()` constructs the backend via `get_default_goal_backend(agent_root)` directly and runs the dual-probe health check against it (it does not read any `AtomicAgent` attribute — none exists yet).

---

## Doctor check

`check_goal_backend()` in `atomic_agents/doctor.py` is catalogued in spec/27 §`### goal-backend` and uses the **dual-probe pattern** (precedent: spec/27 `### mcp-server-registry-backend`; MEMORY.md `feedback_doctor_dual_probe_pattern`):

1. **Light probe** — calls `list_archived(agent_id)` to verify the backend initializes and the lightweight list operation completes without error.
2. **Heavy probe** — if `goal.md` exists, calls `load_goal(agent_id)` to verify the backend can parse the live goal state. If `goal.md` is absent, reports PASS with the message suffix `(no goal.md for this agent)` and detail `goal_md_present=False`. A missing goal.md is NOT a failure condition (reactive agents have none).

A backend that PASSes the light probe but FAILs the heavy probe is genuinely broken (backend returns an empty archive list by swallowing errors, but load_goal reveals the real problem). Both probes are required; passing only one is not sufficient.

---

## Addendum: spec/40 composition

`FilesystemGoalBackend` implements the `Exportable` Protocol (spec/40). The `isinstance(backend, Exportable)` check passes because `FilesystemGoalBackend.export()` matches the protocol surface. `GoalExport` subclasses `ExportableResult` (verified by `isinstance(backend.export(), ExportableResult)` in both the capability and the shared #379 export-protocol harness) and is re-exported from `atomic_agents.export`:

```python
from atomic_agents.export import GoalExport
```

`ExportableResult` lives in the dependency-free leaf module `atomic_agents/_export_base.py` so that `goal/types.py` can subclass it without forming a circular import (`goal/types.py` → `export/types.py` would cycle through `goal/__init__.py`). `export/types.py` re-exports it, so the public surface `from atomic_agents.export import ExportableResult` is unchanged.

Capability advertisement is verified in `test_export_capability_advertisement.py` by 2 goal-specific tests:
- `test_goal_backend_advertises_canonical_export` — `supports_canonical_export is True`
- `test_goal_backend_is_exportable` — `isinstance(goal_backend, Exportable)` is `True`

plus the shared `test_all_capability_flags_are_bool` parametrization, extended to cover goal, which asserts every capability flag is `bool`-typed. The shared #379 round-trip harness (`test_export_protocol_conformance.py`) additionally enforces the byte-exact Tier A round-trip and the generic `isinstance(result, ExportableResult)` narrowing for goal alongside its peers. These checks are unconditional assertions on the filesystem reference implementation.

---

## Addendum: spec/12 cross-reference

spec/12 (Goals and Intent) describes the **narrative contract** for `goal.md`: frontmatter schema, the `## History` section, sub-goal lifecycle states, and the prose conventions an agent author follows. spec/41 is the **storage abstraction** layer below spec/12: it defines the Protocol that makes goal.md's content swappable behind an interface without changing the narrative contract.

The two specs compose as follows:
- spec/12 defines what goal.md CONTAINS (schema, fields, prose conventions).
- spec/41 defines how goal.md is ACCESSED (Protocol, Implementer Contract, operator override).
- A backend conforming to spec/41 MUST preserve the spec/12 narrative contract — `save_goal()` must not corrupt the `## History` section, and `apply_transition()` must append to it rather than overwriting it.

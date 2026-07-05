# spec/41: GoalBackend Protocol

> **Status:** LOCKED — Protocol scaffold: #425 PR1 (2026-06-11); write-path adoption: #448 PR1 (2026-06-13); audit + CAS conformance: #448 PR2 (2026-06-13); coordinator + fail-closed cost gate + spec/41 LOCK: #448 PR3 (2026-06-13); clock-injection addendum + GoalManager shim + agent_root resolution: #483 PR1 (2026-06-13); backend-universe alignment (coordinator threads log/policy/profile backends into OutcomeRunner): #496 PR1 (2026-06-14); multi-goal addressing + create_goal RE-LOCK: #642 PR1 (2026-06-26); conductor gate statuses (awaiting_decision + skipped) + gate_decision_id transition field RE-LOCK: #581 PR2 (2026-06-27); **conductor concurrency + conflict serialization RE-LOCK: #582 PR3 (2026-06-27) — MUST 14 (expected_decision_id CAS under goal lock), save_goal per-goal lock (#655 closed), held_conflict_keys on SubGoal + SUB_GOAL_TRANSITION_FIELDS, serialize_sub_goal emits held_conflict_keys when non-empty; 14 MUSTs total, 3 new conformance tests (TEST 64–66: expected_decision_id CAS, save_goal held-lease serialization closing #655, concurrent two-thread gate-answer race closing #660)**. Conformance suite covers all 14 Implementer Contract MUSTs for `FilesystemGoalBackend` (`test_goal_backend_conformance.py`, 67 tests + `test_goal_multigoal_642.py`, TEST 60–129, 74 collected) plus filesystem-specific tests (`test_goal_filesystem.py`) and coordinator integration tests (`test_goal_coordinator.py`, 22 tests; +1 actor-model wiring guard added in #668). Goal is also registered in the shared #379 export conformance harness (`test_export_protocol_conformance.py`) and the capability-advertisement harness (`test_export_capability_advertisement.py`: 2 goal-specific tests — `test_goal_backend_advertises_canonical_export`, `test_goal_backend_is_exportable` — plus the shared `test_all_capability_flags_are_bool` parametrization extended to cover goal). The four pre-existing goal tests (`test_goal.py`, `test_agent_goal_loading.py`, `test_dashboard_goals.py`, `test_goal_outcome_composition.py`) remain the zero-behavior-change regression guard (assertions unchanged); archive golden assertions updated in #448 PR1 for the A3 data-loss fix (the one sanctioned exception to the freeze), and the `test_goal_outcome_composition.py` `agent_with_goal` fixture gained a minimal `persona/IDENTITY.md` in #448 PR3 so the shim can construct the real `AtomicAgent` the now-live cost gate requires (fixture-only; every assertion is byte-identical).

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

## Shipping plan (4 PRs — all merged)

- **PR 1 (#487).** Protocol scaffold + dataclasses + capability advertisement + `FilesystemGoalBackend` reference impl + compatibility re-export (`goal.py` → `goal/` package; the documented `from atomic_agents.goal import GoalManager` path stays a **supported** public import, NOT deprecated — no `DeprecationWarning` is emitted because the path is intentionally permanent) + `GoalManager` relocated to `_goal_impl.py` + single shared `validate_goal()`/constants in `goal/types.py` + `GoalExport` (an `ExportableResult` subclass) wired into `atomic_agents.export` + `check_goal_backend()` doctor check + full conformance suite + filesystem-specific tests + spec/41 DRAFT. Goal-outcome composition deferred to PR 3.
- **PR 2 (#490).** Write-path adoption: `dispatch_as_outcome()` routed through `backend.save_goal` + `backend.append_history_event` (A3 data-loss fix, golden test updates); `AtomicAgent(goal_backend=...)` constructor kwarg + `AtomicAgent.goal_backend` attribute; audit-ordering test (`test_goal_dispatch_audit_ordering.py`, 2 tests); golden-test regression suite (`test_goal_adoption_golden.py`, 4 tests).
- **PR 3 (#448 PR3, this PR).** Goal-outcome coordinator (`goal/coordinator.py` — thin free function `dispatch_sub_goal_as_outcome()`); fail-closed cost gate (`CostGuardrailBlocked`); compare-and-set `apply_transition()` (`expected_from_status` param, `GoalConcurrentModification`); `dispatch_as_outcome()` refactored to a thin shim; coordinator integration tests (`test_goal_coordinator.py`, 19 tests — extended to 21 in #496 PR1); 2 CAS conformance tests (TEST 54–55, total 56 conformance tests as of PR3); spec/41 LOCKED.
- **PR 4 (#483/#485/#486 PR1, this PR — goal-adoption cleanup bundle).** Injectable clock (`when: date | None = None`) on `apply_transition()` + `archive_goal()` (closes the split-clock byte-identity bug); `GoalManager.archive()`/`abandon()` reduced to thin shims over `backend.archive_goal(when=self.today)` (backend owns the "goal archived" prose exclusively — no double-write); `GoalManager.agent_root`/`agents_root` resolved at `__init__` for path consistency with the backend's containment check (#486; trust boundary unchanged — see the addendum's "Trust boundary" note); 4 new conformance tests (TEST 56–59 — `archive_goal(when=…)` clock injection, single-prose, `append_history_event` ts-first reorder, `apply_transition(when=…)` prose-vs-ts independence) for a total of 60; golden byte-identity test under a pinned clock; spec/41 versioned normative addendum (this section). Stays LOCKED.

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
├── filesystem.py      # FilesystemGoalBackend reference implementation
└── coordinator.py     # dispatch_sub_goal_as_outcome() — thin free function
                       # composing GoalBackend + OutcomeRunner with a
                       # pre-dispatch fail-closed cost gate; NOT a Protocol
                       # method (needs both backend AND runtime)

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

- **(a) Enum-domain validation — ENFORCED by the backend, a conformance requirement.** `to_status` MUST be a member of `VALID_SUB_GOAL_STATUSES` (the spec/12 status enum: `pending`, `in_progress`, `complete`, `blocked`, `abandoned`, `awaiting_decision`, `skipped` — 7 members as of PR2 #581; see versioned normative addendum §"PR2 #581: conductor gate statuses" below). `apply_transition()` MUST reject an unknown `to_status` **fail-closed, before any write**, raising `SchemaValidationError`. This closes the write-time/read-time validation asymmetry: a `to_status` the backend's own `load_goal()` would reject must never be persisted. The `fields` parameter MUST NOT be allowed to overwrite `sub_goal.status` through the side door (the reference impl skips any `status` key in `fields`; `to_status` is the authoritative status channel). The conformance suite pins both the front-door (`to_status`) and side-door (`fields={"status": …}`) cases across **every** backend.
- **(b) Transition-graph legality (ordering) — NOT enforced inside `apply_transition()`.** Which `from → to` edges are legal (e.g. you cannot skip `pending → complete` without a `dispatch_as_outcome()` call) is pure computation that stays ABOVE the Protocol in `GoalManager` (see `backend.py`'s "computation stays above the Protocol" note). The backend is the atomic write primitive for a status already known to be a valid enum member; it is not the state-machine *ordering* validator.

---

## Protocol surface

The GoalBackend Protocol exposes **14 protocol attributes** as of #642 (13 methods + the `backend_id` property); the pre-#642 LOCK surface was 12 attributes (11 methods + the property). The twelve pre-#642 signatures are byte-identical; `create_goal()` and `list_goals()` are new first-class Protocol methods; `export()` gains a normative fail-loud guard. `for_goal()` is NOT on `GoalBackend` — it lives on the separate `AddressableGoalBackend` Protocol.

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
        expected_from_status: str | None = None,  # MUST 10 — CAS guard
        when: date | None = None,  # injectable clock for ## History prose date
    ) -> Goal: ...
    def append_history_event(
        self, agent_id: str, event: dict[str, Any]
    ) -> None: ...

    def archive_goal(
        self,
        agent_id: str,
        reason: str = "completed",
        when: date | None = None,  # injectable clock — all date-stamped fields use this
    ) -> str: ...
    def list_archived(self, agent_id: str) -> list[str]: ...

    def read_schema_version(self, agent_id: str) -> int | None: ...
    def goal_text(self, agent_id: str) -> str: ...

    def export(self, query: Any = None) -> GoalExport: ...
    def export_all(self) -> GoalExport: ...
    def capabilities(self) -> GoalCapabilities: ...

    # New in #642 — MUST 11 and MUST 12
    def create_goal(
        self,
        agent_id: str,
        goal_id: str,
        goal: Goal,
        when: date | None = None,
    ) -> Goal: ...
    def list_goals(self, agent_id: str) -> list[str]: ...
```

`AddressableGoalBackend` is a **separate** `@runtime_checkable` Protocol that carries only the scope-handle factory. It MUST NOT inherit from `GoalBackend`.

```python
@runtime_checkable
class AddressableGoalBackend(Protocol):
    """Thin scope-handle factory for multi-goal agents (MUST 13).

    Callers MUST check isinstance(backend, AddressableGoalBackend) before
    calling for_goal(). FilesystemGoalBackend implements both GoalBackend
    and AddressableGoalBackend.
    """
    def for_goal(self, goal_id: str | None) -> GoalBackend: ...
```

**Breaking change for third-party GoalBackend authors.** Adding `create_goal()` and `list_goals()` to `GoalBackend.__protocol_attrs__` means any existing class that satisfies `isinstance(obj, GoalBackend)` via the runtime_checkable check must now also provide these two methods or the check will fail. A stub that raises `NotImplementedError` preserves Protocol membership but MUST advertise `supports_multi_goal=False` (capability honesty, MUST 2).

---

## Capability advertisement

`GoalCapabilities` is a frozen dataclass with **five** fields (one added in #642):

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `backend_id` | `str` | (required) | Stable identifier matching `backend_id` property |
| `supports_canonical_export` | `bool` | `False` | Implements `export()` per spec/40 |
| `supports_archive` | `bool` | `False` | Implements `archive_goal()` and `list_archived()` |
| `supports_history_query` | `bool` | `False` | Implements `append_history_event()`; history is enumerable only via `export()` — there is no dedicated history-query method on the Protocol |
| `supports_multi_goal` | `bool` | `False` | Implements `create_goal()`, `list_goals()`, and (via AddressableGoalBackend) `for_goal()` |

The new field is appended last to preserve positional backward-compatibility (`GoalCapabilities(backend_id='x')` still constructs with all defaults False). `FilesystemGoalBackend` advertises all five flags as `True`.

---

## Schema migration note

`read_schema_version()` returns the integer `schema_version` from the goal's frontmatter, or `None` when goal.md is absent OR present without a `schema_version` key; it raises `GoalCorrupted` when goal.md is present but unparseable, or carries a `schema_version` that is not integer-coercible. It is **NOT** a `MigratableUnit` and MUST NOT be registered with the migration runner (spec/03), whose `unit_type` is locked to `Literal["memory", "wiki"]` and therefore cannot represent goal state. Goal schema evolution is **GoalBackend-internal** — handled inside the backend's own `load_goal()`/`save_goal()` via the `CURRENT_GOAL_SCHEMA_VERSION` check in `validate_goal()`, entirely separate from the spec/03 migration runner. `read_schema_version()` is a diagnostic-only probe for the doctor and backend-internal version checks, not a migration controller.

---

## Implementer Contract (14 MUSTs)

These MUSTs bind every conforming GoalBackend implementation (MUST 1–12 are GoalBackend; MUST 13 binds the separate AddressableGoalBackend Protocol; MUST 14 extends `apply_transition()` with an inner-lock decision CAS). The conformance test suite in `tests/test_goal_backend_conformance.py` covers spec/41 MUSTs 1–10 and MUST 14 (67 tests total, including 2 CAS tests at TEST 54–55, 4 clock-injection / key-ordering tests at TEST 56–59, the #581 PR2 conductor gate-status tests at TEST 60–63: 'awaiting_decision' + 'skipped' enum acceptance, the 7-member-set rejection regression guard, and gate_decision_id round-trip through apply_transition; TEST 64: expected_decision_id CAS match/mismatch; TEST 65: `save_goal()` held-lease serialization under the per-goal lock (#655); and TEST 66: a genuinely concurrent two-thread gate-answer race where exactly one wins and the other raises `GoalConcurrentModification` (#660) — all #582 PR3). MUST 11 and MUST 12 are covered by `tests/test_goal_multigoal_642.py` (TEST 60–129, 74 collected). MUST 13 (AddressableGoalBackend) is covered by TEST 72–74 and TEST 93–98.

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

**Write/read validation symmetry.** A backend MUST NOT persist a `goal.md` (or its store-native equivalent) that its own `load_goal()` would then reject. Every write path — `save_goal()` AND `apply_transition()` — MUST validate the resulting goal against the same schema `load_goal()` enforces, *before* the durable write, and fail closed (raise `SchemaValidationError`, write nothing) on invalid state. This closes the asymmetry where `apply_transition()`'s `fields` channel could set a permitted-but-invalid value (e.g. `fields={"blocked_by": "<unknown-id>"}`) that writes successfully but locks the agent out of its own goal on the next read. The reference impl centralizes this in `_write_goal()` (the single serializer both paths call). The conformance suite pins the `blocked_by`-to-unknown-id case across every backend.

### MUST 6 — `apply_transition()` is a single atomic unit (and enum-validates fail-closed)

`apply_transition()` MUST write the updated `goal.md` AND append the `history_event` to `goal_history.jsonl` as a single unit. If the backend uses filesystem locking (e.g., `fcntl.flock`), both writes MUST complete before the lock is released. A crash between the two writes is permissible; a crash that leaves `goal_history.jsonl` written but `goal.md` un-updated is a conformance violation (the JSONL is the audit trail for a committed state change).

**Injectable clock (`when` parameter).** The `when: date | None = None` parameter controls the date prefix in the `## History` prose line only. When `when=None`, the backend MUST default to `date.today()`. The `when` parameter MUST NOT affect the JSONL `ts` field in `history_event` — that field is a real wall-clock audit timestamp provided by the caller and MUST be written as-is. The two clocks are intentionally independent: `when` is a reproducible prose stamp; `ts` is the immutable audit record of when the call actually happened.

`apply_transition()` MUST also **enum-validate fail-closed**: if `to_status` is not a member of `VALID_SUB_GOAL_STATUSES`, it MUST raise `SchemaValidationError` **before any write** (no partial `goal.md`, no orphan JSONL line). The `fields` parameter is a constrained channel: it MAY set only transition metadata — the members of `SUB_GOAL_TRANSITION_FIELDS` (`assigned`, `deadline`, `blocked_by`, `completed`, `output`, `body`, `acceptance_criteria`, `gate_decision_id` — the last added in PR2 #581 to carry the conductor's CAS-verification token for gate stages) — and MUST NOT reach `sub_goal.status` (the sole status channel is `to_status`, so `fields={"status": <anything>}` MUST NOT be persisted) or the immutable identity fields `id`/`label` (so `fields={"id": …}` MUST NOT rewrite a sub-goal's identity mid-transition). The allow-set fails closed: any key not in `SUB_GOAL_TRANSITION_FIELDS` — including a future `SubGoal` field — is ignored, not blindly applied. This guarantees the backend never persists a `goal.md` its own `load_goal()` would reject (no write-time/read-time validation asymmetry) and never silently corrupts sub-goal identity. The conformance suite pins the `status` side-door, the `id`/`label` identity side-door, and a co-passed legitimate field across **every** backend. Transition-graph *ordering* legality is NOT enforced here — see §"WritePolicy applicability" (a) vs (b).

### MUST 7, 8, 9 — `archive_goal()` behavioral constraints

**MUST 7 (write ordering):** `archive_goal()` MUST write the archive file to `goal_archive/<slug>.md` BEFORE unlinking `goal.md`. If the process crashes between the two operations, the archive file exists and `goal.md` also exists — a recoverable state. There is no window where both are absent.

**MUST 8 (collision-safe slug):** When an archive file with the computed slug already exists, `archive_goal()` MUST append a numeric suffix (`_1`, `_2`, …) until a free name is found. The loop MUST terminate (backends may impose a reasonable maximum, e.g., 999).

**MUST 9 (idempotency on retry-after-unlink):** The idempotency condition is — no `goal.md` present AND at least one archive file present. Under that condition, `archive_goal()` MUST return the most-recently-modified archive slug in `goal_archive/` without writing a second file, rather than raising. This handles the retry-after-crash case (a prior partial run completed the unlink step). With no `goal.md` present the intent slug cannot be reconstructed, so the newest archive is returned; this is a best-effort retry guard, correct for the common single-goal case. (Per-goal exactness for agents that have archived many goals over their lifetime is tracked as a follow-up; it does not change the Protocol contract.)

**Injectable clock (`when` parameter — all three MUSTs).** `archive_goal()` accepts `when: date | None = None`. The backend MUST resolve this ONCE at method entry: `today = (when or date.today()).isoformat()`. The resolved value MUST be used for ALL date-stamped fields in a single archive operation: the slug date prefix, the `archived_at` frontmatter field, the `last_progress_check` frontmatter field, and the `## History` prose date. Using `date.today()` separately for any of these fields while `when` is supplied, OR calling `date.today()` more than once, is a conformance violation (split-clock divergence). The conformance suite verifies field-value parity of all four date-stamped fields under a pinned `when` value (TEST 56); the filesystem golden suite additionally pins the full archive-file bytes under a pinned clock (`test_goal_adoption_golden.py::test_archive_shim_byte_identity_under_pinned_clock`) so a serializer change (key reorder, whitespace) that keeps the four dates correct still fails.

### MUST 10 — `apply_transition()` compare-and-set guard (CAS)

When the optional `expected_from_status` parameter is supplied (not `None`), `apply_transition()` MUST:

1. Re-read the sub-goal's current `status` from durable storage **under the same lock** used for the write (i.e., after acquiring `fcntl.flock` on the filesystem reference impl, before any mutation).
2. Compare the on-disk status against `expected_from_status`.
3. If they differ, raise `GoalConcurrentModification` **with no write to goal.md and no JSONL append** — the lock is released, the goal state is identical to before the call.
4. If they match, proceed with the normal `apply_transition()` write path.

This closes the TOCTOU window in the goal-outcome coordinator: the coordinator pre-transitions the sub-goal `pending → in_progress`, then runs `OutcomeRunner` (without holding the goal lock), then calls the terminal `apply_transition(expected_from_status='in_progress')`. If a concurrent writer moved the sub-goal to `complete` (or any other non-`in_progress` status) during the run, the terminal transition MUST be rejected — not silently applied over the concurrent write.

`expected_from_status=None` (the default) is backward-compatible: existing callers that do not pass the parameter get the pre-MUST-10 behavior (no CAS check, no `GoalConcurrentModification` risk).

The conformance suite pins both the match path (TEST 54, transition succeeds) and the mismatch path (TEST 55, `GoalConcurrentModification` raised, goal.md bytes identical before/after, no orphan JSONL line, sub-goal status unchanged).

---

## `apply_transition()` JSONL key ordering

Every `history_event` dict appended by `apply_transition()` or `append_history_event()` MUST be serialized with `"ts"` as the **first key** and `"event"` as the **second key** in the JSON object, regardless of the order in which the caller supplies keys. If the caller passes a dict with `"ts"` not first, or with extra keys before `"ts"`, the backend MUST actively reorder — it MUST NOT write the caller's key ordering as-is. This reorder is enforced by `_make_history_event()` which builds an ordered dict with `"ts"` and `"event"` first, then merges any remaining caller-supplied extra fields. The ordering is load-bearing for log-reader tools that extract timestamps from the first JSON field without full deserialization. The conformance suite verifies both the byte-level `startswith('{"ts"')` invariant AND that `"event"` is the second key, even when the caller passes extra leading keys before `"ts"` in the input dict (TEST 58).

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

## Goal-outcome composition (coordinator shipped in #448 PR3)

`GoalManager.dispatch_as_outcome()` is now a thin shim that delegates to `dispatch_sub_goal_as_outcome()` in `goal/coordinator.py`. The coordinator is a thin free function (NOT a `GoalBackend` method) that composes the backend with `OutcomeRunner` and enforces the pre-dispatch fail-closed cost gate. The cost gate is **live on the CLI path** (`python -m atomic_agents.goal dispatch-outcome`): the shim constructs a real `AtomicAgent` (keyword args `name`/`agents_root`/`goal_backend`, outside any try/except so a construction failure propagates) and passes it to the coordinator, so the gate consults the same cost universe the `OutcomeRunner` will spend (model.md caps read from the shared `agent_root`, and — critically — the same `log_backend` cost store threaded into the runner; see the universe-alignment note below for which threaded backends align cost vs run-side construction). A `CostGuardrailBlocked` propagates to the CLI as exit 3.

1. **Validation.** Sub-goal must be `pending` or `in_progress`; `blocked_by` (if set) must reference a `complete` sub-goal.
2. **Cost gate (fail-closed, Principle #4).** `result = agent._check_cost_guardrails(critical=False)`. Read `result.allow` as a dataclass attribute (NOT 2-tuple). If `not result.allow`: append the `coordinator_dispatch_rejected` event to `goal_history.jsonl` FIRST (audit-before-raise ordering), then raise `CostGuardrailBlocked(result.reason)`. Sub-goal stays `pending`; `OutcomeRunner` is never called. The `coordinator_dispatch_rejected` event field set is `{ts, event: "coordinator_dispatch_rejected", sub_goal_id, reason}` (`reason` is `CostCheckResult.reason`); the event name deliberately omits the `blocked` substring so `dashboard/goals.py`'s substring match does not stamp a spurious `blocked_at`. If the append itself fails (IO error), the IO error propagates as a distinct error from `CostGuardrailBlocked` — the coordinator does NOT dispatch after a failed audit write (fail-closed: never pretend the block was audited).
3. **Pre-transition (if pending).** `apply_transition(to_status='in_progress', event='sub_goal_outcome_started')`. Lock released after write. In-memory `goal_manager._goal` updated to the returned `Goal` so callers see `in_progress` during the run.
4. **Run.** `OutcomeRunner(...).run(...)` — no goal lock held during the LLM calls.
5. **Terminal transition.** `apply_transition(to_status=<mapped>, expected_from_status='in_progress', history_event={"event": "sub_goal_outcome_dispatched", outcome_run_id, terminal_state, applied_status, iterations, total_cost_usd, ts})`. CAS guard (MUST 10) rejects concurrent modifications that moved the sub-goal off `in_progress` during the run.

**Terminal-state mapping:**
| `OutcomeResult.status` | `applied_status` (sub-goal `to_status`) | `fields` |
|---|---|---|
| `satisfied` | `complete` | `completed=today` |
| `max_iterations_reached` | `blocked` | `blocked_by=None` |
| `failed` | `blocked` | `blocked_by=None` |
| `interrupted` | `in_progress` | `{}` (stays in_progress; CAS passes) |

> **Non-normative note (universe alignment, #496 PR1):** The coordinator reads `agent.log_backend`, `agent.policy_backend`, and `agent.profile_backend` from the gate agent and passes them as keyword args into `OutcomeRunner(log_backend=..., policy_backend=..., profile_backend=...)`, so the runner's internal `AtomicAgent` is constructed against the same backend instances the gate agent carries. The three backends align at different boundaries — only `log_backend` is load-bearing for the gate↔run cost alignment:
> - **`log_backend` (the cost-critical one — aligns the gate AND the run).** The pre-dispatch cost gate (`_check_cost_guardrails`) reads spend from `self.log_backend`, and the run writes cost records to the runner's `log_backend`. Without threading, a custom `LogBackend` (SQLite/Postgres) would make the gate read one store and the run write another — the gate's allow/deny decision based on a different store than where spend actually lands. Threading aligns both onto one store.
> - **`policy_backend` (aligns the RUN's in-`call()` caps, NOT the gate).** Policy-layer caps are read only from `_policy_snapshot_this_call`, which is `None` outside `agent.call()` — so NEITHER pre-dispatch gate (the coordinator's nor the runner's per-iteration gate, both of which run before `agent.call()`) consults policy caps at all. Threading `policy_backend` does not change either gate decision; it ensures the runner's internal `agent.call()` multi-turn loop resolves the SAME `PolicyBackend` the gate agent carried (in-run cap consistency), not gate↔run alignment.
> - **`profile_backend` (run-side identity/model-config consistency, not cost).** `profile_backend` carries agent identity/model config; it does not participate in cost accounting. It is threaded so the runner's agent is the same identity/config the caller wired, not for any budget reason.
>
> **Scope caveat — caps are not yet *fully* aligned (`mandate_backend`).** The effective cost cap is composed via MIN with the mandate (inside `_check_cost_guardrails`'s MIN composition + `MandateCheck`), and `mandate_backend` is deliberately **not** threaded by this change (the ruling scoped the threaded set to log/policy/profile). On the custom-backend programmatic path, an operator who pins a custom `mandate_backend` on the gate agent (tightening the effective cap) would have the gate check against the tightened cap while the runner's internal `AtomicAgent` resolves its own default mandate and runs against a looser cap — so a mandate-derived cap can still diverge. Tracked in #503. (The default-filesystem CLI path is unaffected: the gate agent and runner both resolve the same filesystem mandate at the shared `agent_root`.)
>
> `outcome_backend` is intentionally **not** threaded — the runner owns its own outcome write-path topology. Other `OutcomeRunner` backends (`persona`, `corpus`, `mcp_server_registry`, `mandate`, `tool_registry`) are also not threaded and default to filesystem resolution. For callers using `GoalManager.dispatch_as_outcome()` (the CLI shim): the shim constructs the gate agent without custom backends, so all three forwarded attrs are filesystem defaults — byte-identical behavior for default-filesystem deployments.

The coordinator is NOT a `GoalBackend` method: it needs both the backend AND the runtime (`AtomicAgent`, `OutcomeRunner`) simultaneously, and making it a Protocol method would invert the dependency direction (the backend must not depend on the runtime). A lazy in-function import of `OutcomeRunner` guards the `goal/` bootstrap cycle (`goal/__init__.py`'s `__getattr__` lazy loader); `AtomicAgent` is passed in by the caller (the shim constructs it) and is never imported in the coordinator.

---

## Operator override surface

GoalBackend is operator-configurable via **three** surfaces — the env var, the factory, and the `AtomicAgent` constructor kwarg. The `AtomicAgent` constructor kwarg + public attribute ship in **#448 PR1**.

| Surface | Mechanism | Status |
|---------|-----------|--------|
| Environment variable | `ATOMIC_AGENTS_GOAL_BACKEND=<backend_id>` | Ships in #425 PR1 |
| Factory function | `get_default_goal_backend(agent_root)` | Ships in #425 PR1 |
| Constructor kwarg | `AtomicAgent(goal_backend=my_backend)` + `AtomicAgent.goal_backend` attribute | Ships in #448 PR1 |

`AtomicAgent(goal_backend=...)` kwarg wins over the env-var factory (same pattern as `journal_backend`). `AtomicAgent.goal_backend` is the per-agent persistence handle (NOT the goal_text reader — `_load_goal_text()` stays on `self._profile.goal_text` per Principle #6). The doctor's `check_goal_backend()` constructs the backend via `get_default_goal_backend(agent_root)` directly and runs the dual-probe health check against it.

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

---

## Addendum — #483 PR1 (2026-06-13): clock injection, GoalManager shim, agent_root resolution

This versioned normative addendum records the Protocol surface changes and behavioral contracts added in #483 PR1. The spec remains LOCKED; these changes are backward-compatible (new optional parameters with `None` defaults).

### Protocol surface changes

Two method signatures extended with `when: date | None = None`:

- `apply_transition(…, when: date | None = None)` — controls the `## History` prose date prefix only. Default `date.today()`. MUST NOT affect the JSONL `ts` field.
- `archive_goal(…, when: date | None = None)` — controls ALL date-stamped fields in the archive operation (slug prefix, `archived_at`, `last_progress_check`, `## History` prose date). Single-resolution contract: compute `today = (when or date.today()).isoformat()` ONCE at method entry; use for all four fields.

### Closed defects

**Split-clock bug (#483).** `archive_goal()` in `FilesystemGoalBackend` previously called `date.today()` independently for the slug prefix (line ~515) and the `archived_at` / `last_progress_check` fields (line ~559). A midnight-boundary call would produce a slug dated `2026-06-13` and frontmatter fields dated `2026-06-14` — a byte-identity-breaking inconsistency with no recovery path. Fixed by single-resolution `today = (when or date.today()).isoformat()` computed once before the file lock.

**`apply_transition` prose date not injectable (#483).** `today` was hardcoded to `date.today()` before the `when` parameter existed. Now `today = (when or date.today()).isoformat()` for prose; JSONL `ts` unchanged.

**`GoalManager.archive()` in-place duplicate (#483).** `GoalManager.archive()` previously contained ~60 lines of archival logic that duplicated `FilesystemGoalBackend.archive_goal()`, including its own frontmatter parsing, slug construction, and a separate `_append_history("goal archived")` call. This created double history prose ("goal archived" appearing twice in the `## History` section) when routed through the backend. Fixed by replacing the in-place implementation with a thin shim that delegates to `backend.archive_goal(…, when=self.today)`, reconstructs the `Path` from the returned slug, and nulls `self._goal`. The backend now owns the "goal archived" prose exclusively.

**`GoalManager.agent_root` not resolved (#486).** `self.agent_root` was set as `self.agents_root / agent_name` without calling `.resolve()`. Relative `agents_root` values (or paths with symlinks) produced unresolved paths that diverged from `FilesystemGoalBackend._require_within_root`'s resolved-path containment check. Fixed by resolving both `self.agents_root` and `self.agent_root` at `__init__` construction time via `Path(…).resolve()` (folding `OSError`/`RuntimeError` from a symlink loop into `PathTraversalError` so a malformed vault path fails closed instead of crashing construction). All derived paths (`goal_path`, `archive_dir`) inherit the resolved root.

**Trust boundary (not a perimeter guard).** This resolution is for path *consistency* with the backend's containment check, NOT a defense against an escaping agent-directory symlink. Per TENSIONS T15 / spec/44 "Security model", the trust boundary is the vault root and a writer who can plant a symlink inside the vault is already inside the trust zone (within-perimeter planting is out of scope). `resolve()` FOLLOWS an agent-directory symlink even when it points outside the vault: the resolved (escaped) path becomes the manager's root and the default backend is constructed against it, so the backend's containment then anchors on the escaped target rather than rejecting it. That is acceptable because it requires a vault-writer; adversarial / multi-tenant deployments MUST use a real-authz backend (Postgres/Redis), not a hardened filesystem backend.

### Coordinator clock threading

`goal/coordinator.py` passes `when=goal_manager.today` to both `apply_transition()` calls (pre-transition `pending → in_progress` and terminal transition). `GoalManager.today` is a plain `date` attribute set ONCE at construction as `self.today = today or date.today()` (`_goal_impl.py`) — it is NOT a property, there is no `_today` backing field, and the value is frozen at construction time (not re-evaluated per call). For a single-session CLI dispatch (the COARSE-ROUTE contract) this is correct: the whole dispatch shares one reproducible date. A long-lived `GoalManager` instance held across a midnight boundary would stamp its construction-time date; that is acceptable under the single-session contract and is the same frozen-clock shape `GoalManager` already uses for `mark_complete`/`save`. This mirrors the injectable-clock intent of `JournalBackend.append_entry(when=…)` from spec/43 (the backend method accepts an injected date; the caller threads a fixed value).

### New conformance tests (TEST 56–59)

| TEST | Name | Contract pinned |
|------|------|-----------------|
| 56 | `test_archive_goal_when_parameter_accepted` | `when=date(2026,1,1)` → slug/`archived_at`/`last_progress_check`/body all contain "2026-01-01"; "goal archived" in body |
| 57 | `test_archive_goal_prose_appears_exactly_once` | `body.count("goal archived") == 1` (no double-prose from shim + backend) |
| 58 | `test_append_history_event_reorders_ts_first_when_ts_not_first` | Input dict with extra leading keys before `"ts"`; JSONL line `startswith('{"ts"')`, `"event"` is second key, all fields present |
| 59 | `test_apply_transition_when_parameter_stamps_prose_not_ts` | `apply_transition(when=date(2026,1,1))` with a distinct wall-clock `ts` → `## History` prose bullet carries "2026-01-01" (durable on reload) AND the JSONL `ts` equals the caller-supplied wall-clock value verbatim (the `when`→`ts` independence MUST 6 binds) |

### GoalManager runtime boundary — deliberate narrowing of backend MUST 9

The `GoalManager.archive()` / `abandon()` runtime boundary (above the Protocol) **fails closed**: it raises `AtomicAgentsError` ("No active goal to archive") when no active `goal.md` is present, and does **NOT** inherit backend MUST 9's retry-after-unlink idempotency (which returns the newest archive slug at exit 0). MUST 9 is scoped to direct `backend.archive_goal()` crash-retry callers — the runtime layer narrows it because the `GoalManager` public boundary must surface "nothing to archive" as a clean error rather than a stale slug (the false-success regression guard restored in #483 PR1; pinned by `test_archive_on_already_archived_agent_raises_not_stale_return`). This is a runtime-boundary contract, not a backend conformance violation (`GoalManager` is not a backend).

### Unchanged invariants

- MUST 1–5, MUST 10: no changes.
- MUST 6–9 clock-injection clauses: additive normative language only (backward-compatible defaults). MUST 6's `apply_transition(when=…)` clause is conformance-guarded by TEST 59 (prose-date injection + JSONL `ts` independence); MUST 7–9's `archive_goal(when=…)` clause by TEST 56.
- `FilesystemGoalBackend.append_history_event()` JSONL `ts` field: unchanged (caller-supplied, not affected by `when`).
- The `GoalManager.archive(reason)` public signature: unchanged (`returns Path`; thin shim behavior-compatible).
- `AtomicAgent.goal_backend` public attribute: unchanged.
- All pre-existing conformance tests (TEST 1–55): no behavioral change, all passing.

---

## Addendum — #642 (2026-06-26): multi-goal addressing + create_goal RE-LOCK

This addendum records the Protocol surface additions and behavioral contracts introduced in #642 PR1. Because the new methods (`create_goal`, `list_goals`) exceed the addendum pattern (they are first-class `__protocol_attrs__` entries, not keyword-only extensions to existing methods), a **full re-LOCK ceremony** was required. The spec status was updated from LOCKED (10 MUSTs) to LOCKED (13 MUSTs) — three new MUSTs were added (MUST 11 `create_goal`, MUST 12 `list_goals` on GoalBackend; MUST 13 `for_goal` on the separate AddressableGoalBackend Protocol).

### New protocol methods

Two methods added to `GoalBackend`, one method added via `AddressableGoalBackend`:

| Method | Contract | MUST |
|--------|----------|------|
| `create_goal(agent_id, goal_id, goal, when=None) -> Goal` | refuse-on-COMPLETE / complete-on-PARTIAL collision semantics; MUST 11 | MUST 11 |
| `list_goals(agent_id) -> list[str]` | sorted; includes `'_standing'`; MUST 12 | MUST 12 |
| `for_goal(goal_id) -> GoalBackend` | on AddressableGoalBackend only | MUST 13 |

### MUST 11 — `create_goal()` atomicity and collision semantics

`create_goal(agent_id, goal_id, goal, when=None)` MUST atomically create a new addressed run-goal at `goals/<goal_id>/`. All of the following are REQUIRED:

1. **charset validation before I/O.** `goal_id` MUST match `[a-z0-9_-]{1,64}`. STANDING_GOAL_ID (`'_standing'`) is reserved and MUST be rejected before the charset check (because it passes the charset regex). Rejection is loud (`ValueError`); no normalize-or-slugify behavior.
2. **MUST stamp `goal.created` from `when`.** The backend resolves `today = (when or date.today()).isoformat()` and writes `goal.created = today`, overriding any caller-supplied value. This is the single normative exception to MUST 4's write-verbatim rule (`create_goal` has different stamping semantics).
3. **Collision semantics — refuse-on-COMPLETE, heal-only-the-genuine-PARTIAL, fail-closed on ambiguity (recoverability refinement).** Evaluated UNDER the per-goal lock against `goals/<goal_id>/`. The complete decision table:
   - `goal.md` ABSENT → create normally (write `goal.md`, then append the `goal_created` event).
   - `goal.md` PRESENT, `goal_history.jsonl` ABSENT or EMPTY / whitespace-only → a **genuine PARTIAL** / half-created state (goal.md landed but the audit line never did — the rare post-goal.md I/O-failure outcome). The backend MUST **complete it**: append the missing `goal_created` event and return the goal, making `create_goal()` idempotent / self-healing over a partial create. On this path the **persisted `goal.md` is AUTHORITATIVE** — the supplied `goal` argument's body MUST NOT be re-written; the appended event is built **ENTIRELY from the persisted goal** (`intent`/`created`/`schema_version` AND `conductor_run_id` all read off the reloaded goal — "persisted wins" for every field, no field is sourced from the supplied `goal` arg).
   - `goal.md` PRESENT, `goal_history.jsonl` contains a `goal_created` event → the goal is **COMPLETE** → the backend MUST raise `GoalAlreadyExists` (subclass of `AtomicAgentsError`) **with no write to goal.md and no JSONL append**. No overwrite, no upsert.
   - `goal.md` PRESENT, `goal_history.jsonl` contains events but **no** `goal_created` event → **FAIL CLOSED**: the backend MUST raise (the reference impl raises `GoalAlreadyExists` with a clear message naming the scoped `goals/<id>/goal.md` and history path). A goal with transition events but no creation marker was authored via `save_goal()` / `apply_transition()` (which do NOT emit `goal_created`); it is corrupt/ambiguous, NOT a clean partial. Healing it would mint a spurious, mis-ordered `goal_created` over a legitimately-authored goal. The classifier MUST distinguish absent/empty history (heal) from has-events-but-no-creation (fail closed).
   - `goal.md` PRESENT but `goal_history.jsonl` cannot be read or a line cannot be parsed (completeness is undeterminable) → **FAIL CLOSED**: the backend MUST raise (the reference impl raises `GoalAlreadyExists` with a clear message) rather than silently complete or overwrite an ambiguous state.

   Also under the lock: if `goals/<goal_id>` exists as a regular **file** (not a directory), the backend MUST raise `GoalAlreadyExists` with an actionable message (the `goals/` tree is framework-reserved) rather than leaking the raw `FileExistsError` the lock's lazy `mkdir` would otherwise surface. This MUST hold even when the file is **raced** into place between the pre-lock stray-file check and the lock's `mkdir` (TOCTOU): the reference impl catches the `FileExistsError` from the lock-acquire/`mkdir` path and re-raises it as `GoalAlreadyExists` so the raw `OSError` never escapes the documented Raises contract.
4. **MUST 6 ordering mirror.** Inside the lock, on the fresh-create path:  `json.dumps(goal_created_event)` pre-probe FIRST (raises before any `goal.md`/`goal_history.jsonl` write if non-serializable — no goal state is committed, though an empty `goals/<goal_id>/` directory may already exist from the lock's lazy `mkdir`; see partial-create debris below), then `atomic_write(goal.md)`, then `atomic_append_jsonl(goal_history.jsonl)`. The reverse (JSONL before goal.md) is a conformance violation. On the complete-on-partial path the goal.md already exists, so only the `json.dumps` pre-probe + `atomic_append_jsonl(goal_history.jsonl)` run.
5. **goal_created JSONL schema.** The appended JSONL line MUST contain `{ts, event: "goal_created", goal_id, intent, created, schema_version}`. `ts` MUST be the first key. `conductor_run_id` MUST be **absent** (not `None`) for home-user goals (it is present only when the conductor threads it — Conductor is out of scope for this PR).
6. **per-goal lock granularity.** The lock file MUST be `goals/<goal_id>/.goal.lock`. Concurrent `create_goal()` calls for **different** goal_ids MUST NOT contend on the same lock. The completeness check and the complete-or-refuse decision MUST happen under the SAME lock as the write (TOCTOU-safe).

Two-layer containment: charset allow-list validation (`validate_goal_id`) is the first layer; the canonical resolve-and-verify guard is the second independent layer, applied at **every** I/O boundary — NOT to the parent `goals/` alone. The guard MUST (a) resolve-and-verify the ACTUAL create target directory `goals/<goal_id>` (checking the parent does not contain a symlinked `goals/<goal_id>` that escapes the vault — the escape is at the child node) AND (b) re-resolve-and-verify each write leaf it opens — both `goals/<goal_id>/goal.md` AND `goals/<goal_id>/goal_history.jsonl` — before opening it. (b) is load-bearing because `goal_history.jsonl` is opened in append mode, which FOLLOWS a symlink: a pre-planted symlinked history leaf inside an otherwise-contained goal dir would otherwise write the `goal_created` audit line OUTSIDE the vault (Principle #5). The reference impl satisfies both by routing `create_goal`'s writes through the `for_goal(goal_id)`-scoped backend's `_goal_lock()`/`_write_goal()`/`_append_jsonl()`, each of which calls `_require_within_root` on its leaf; it additionally **verifies BOTH write leaves up front** (before the goal.md write) so a planted symlinked history leaf is an all-or-nothing REFUSE — no goal.md is committed when the sibling leaf escapes. Both layers (charset + per-boundary containment) MUST be present.

Partial-create debris acknowledgment: a failed `create_goal()` (directory created, goal.md not written) may leave an empty `goals/<goal_id>/` directory. `list_goals()` MUST skip these (see MUST 12). Cleanup is tracked in #643.

Single-writer assumption (#642 follow-up, Codex #1; #655 closed in #582 PR3): `create_goal()`'s atomicity holds against ALL other writers on the same `goal_id` — `apply_transition()`, `archive_goal()`, `create_goal()`, AND `save_goal()` all share the per-goal lock and therefore serialize. As of #582 PR3, `save_goal()` acquires `self._goal_lock()` before writing (closing #655, the prior single-writer gap), so a concurrent `save_goal()` can no longer interleave with the create. The whole-fleet serialization of multiple conductor runs on one `goal_id` (conflict keys / queue-behind-decision) is handled by the conductor's conflict-scan lease (spec/50 §"Concurrency and conflict serialization"). Note for custom backends: a caller already holding `_goal_lock()` must not call `save_goal()` while holding it (deadlock risk); the reference impl does not.

### MUST 12 — `list_goals()` enumeration

`list_goals(agent_id) -> list[str]` MUST return a sorted list of all goal_ids for this agent:

- Include `STANDING_GOAL_ID` (`'_standing'`) when `<agent_root>/goal.md` is a regular file.
- Include any `goals/<id>` where `id` matches `[a-z0-9_-]{1,64}`, `id != STANDING_GOAL_ID`, and `goals/<id>/goal.md` is a regular file (presence predicate — directories without goal.md are skipped).
- Return `sorted(result)` — `'_standing'` naturally appears before alpha names because `'_' < 'a'` in ASCII.
- MUST NOT raise when `goals/` is absent, when it is empty, or when it contains non-conforming entries.
- MUST return `[]` for a reactive agent with no goal.md and no goals/ directory.

**Containment consistency (#642 follow-up, Codex #3).** Each enumerated candidate MUST also pass the SAME resolve-and-verify-under-root containment guard `for_goal()` applies (`_require_within_root` in the reference impl), and the `goals/` directory itself MUST be contained. An escaping symlinked `goals/<id>` directory whose `goals/<id>/goal.md` resolves OUTSIDE the vault root MUST be SKIPPED (not listed), and if `agent_root/goals` itself resolves outside the vault it MUST be treated as no addressed goals. This makes a listed id one that `for_goal(<id>)` can actually open — closing the prior list/`for_goal` asymmetry where an escaping entry was listed here yet refused with `PathTraversalError` by `for_goal()`, which let discovery/doctor/resume hand out a goal_id the backend could not open (a durability-consistency footgun). The containment check only ADDS to the charset + presence predicates; it does not relax them.

### MUST 13 — `AddressableGoalBackend.for_goal()` routing

`AddressableGoalBackend` is a **separate** `@runtime_checkable` Protocol (not a subclass of `GoalBackend`). Its single method is `for_goal(goal_id: str | None) -> GoalBackend`.

Routing contract:

- `goal_id is None` or `goal_id == STANDING_GOAL_ID`: return a backend scoped to `agent_root/`. The returned backend's `_goal_path` MUST be `agent_root/goal.md` (byte-identical to the pre-#642 standing-goal layout).
- Any other valid `goal_id`: return a backend scoped to `agent_root/goals/<goal_id>/`. The returned backend's `_goal_path` MUST be `agent_root/goals/<goal_id>/goal.md`.
- An invalid `goal_id` (fails charset) MUST raise `ValueError` before any I/O.
- A `goals/<goal_id>` that resolves OUTSIDE the vault (a symlinked goal dir escaping the perimeter) MUST be refused with `PathTraversalError` **before the scoped backend is constructed** — parity with `create_goal`'s leaf containment. Without this, the scoped backend's construction-time `resolve()` would re-anchor its containment root on the escaped target, and a subsequent scoped write could land outside the vault. (Within-vault symlinks remain in the T15 trust zone.) The same parity applies to `GoalManager.for_goal()` in the reference impl.

The returned backend implements the full `GoalBackend` contract scoped to the target directory. Per-goal lock granularity is preserved: `for_goal('run-a').apply_transition(…)` and `for_goal('run-b').apply_transition(…)` MUST NOT share a lock.

Callers MUST check `isinstance(backend, AddressableGoalBackend)` before calling `for_goal()`. `GoalBackend` does NOT declare `for_goal()`.

### `goals/` reserved directory

`goals/` is a RESERVED directory name within `<agent_root>`. Operators and agent scripts MUST NOT create a `goals/` directory manually. `list_goals()`, `export()`, and the export guard interpret its contents as framework-managed run-goal state.

**Backward-compat note (newly reserved name).** The reservation is new in #642. An existing agent that happened to carry a stray `goals/<x>/goal.md` of its own (unrelated to framework run-goals) now trips the `export()` fail-loud guard where pre-#642 it did not — the guard fires on any `goals/*/goal.md`, regardless of content. This is the fail-safe direction (refuse rather than silently drop possible run-goal state) and is bounded: an agent with no `goals/` directory is byte-for-byte unaffected. An upgrading operator who has repurposed a `goals/` path must move it aside (or, per #643, wait for the content-aware predicate). Tightening the guard to require parseable goal frontmatter is a #643-adjacent follow-up.

### `export()` multi-goal fail-loud guard

`export()` MUST raise `AtomicAgentsError` as its FIRST operation (before reading any bytes) if the agent has at least one addressed run-goal (`goals/*/goal.md` is a regular file). The error message MUST reference issue #643.

The predicate is `goals_dir.is_dir() AND any(p.is_file() for p in goals_dir.glob("*/goal.md"))`, with each matching `goal.md` additionally required to be CONTAINED within the vault root (#642 follow-up, Codex #3): the guard runs `_require_within_root` on `goals/` and on each candidate `goals/<id>/goal.md`, and an escaping symlinked entry whose `goal.md` resolves OUTSIDE the vault is NOT counted as addressed run-goal state within this vault (it is skipped, mirroring `list_goals()`/`for_goal()`). The guard stays fail-loud for genuinely-contained addressed goals. An empty `goals/` directory (partial-create debris) MUST NOT trigger the guard.

Note: this guard predicate (`glob("*/goal.md")`, no charset filter) is intentionally broader than `list_goals()` (which admits a directory only when its name passes the charset allow-list). A non-conforming directory such as `goals/My-Goal/goal.md` is therefore invisible to `list_goals()` yet still trips the guard. This is the fail-safe direction (refuse rather than silently drop run-goal state) and is deliberate; reconciling the two predicates is a #643-adjacent follow-up, not a behavior change required for this LOCK. The containment requirement is the one alignment the guard and `list_goals()` now share: an escaping symlinked entry is excluded from BOTH.

While addressed run-goals exist, **both whole-agent `export()` AND standing-goal export are blocked**: calling `export()` on a standing-scoped backend (`for_goal(None)` / `for_goal('_standing')`) re-runs this identical guard against the same `goals/*/goal.md` and raises again. The only export path that works while #643 is open is **per-run-goal**: `for_goal(<run_goal_id>).export()` (that backend is scoped to `agent_root/goals/<run_goal_id>/`, which has no nested `goals/`, so the guard does not fire). Whole-agent export and standing-goal-while-addressed-goals-exist are deferred to #643.

**Dashboard / discovery blind spot (#654).** The same gap exists on the read-observability side: the dashboard Goals & Outcomes tab, `discover_agents()`, and `list_archived()` currently surface only the STANDING goal (`agent_root/goal.md`) — addressed run-goals under `goals/<id>/goal.md` are NOT shown. This mirrors how the `export()` guard points at #643: the export gap is fail-loud (refuses), the dashboard gap is silently-incomplete (renders only the standing goal). Surfacing addressed run-goals in the dashboard is tracked in **#654**; it is read-only observability work and is intentionally out of scope for the #642 create/address LOCK. No dashboard behavior changes in #642.

### Module layout update (addendum to §"Module layout")

```
atomic_agents/goal/
├── types.py           # + STANDING_GOAL_ID, _GOAL_ID_MAX_LEN, _GOAL_ID_RE,
│                      #   validate_goal_id(), GoalCapabilities.supports_multi_goal
├── backend.py         # + create_goal(), list_goals() on GoalBackend Protocol;
│                      #   AddressableGoalBackend Protocol (separate)
└── filesystem.py      # + create_goal(), list_goals(), for_goal() implementations;
                       #   export() fail-loud guard

atomic_agents/exceptions.py   # + GoalAlreadyExists (direct AtomicAgentsError subclass,
                               #   sibling NOT subclass of GoalConcurrentModification)
atomic_agents/_goal_impl.py   # + GoalManager.for_goal(goal_id) scope-binding handle
```

### Backward compatibility

Zero behavior change for existing reactive/hybrid agents:

- `goal_id=None` is a `for_goal()` backward-compat alias for `STANDING_GOAL_ID`.
- `<agent_root>/goal.md` layout is byte-identical; `for_goal(None).load_goal()` reads it.
- `list_goals()` returns `[]` for agents with no goal.md and no goals/ directory.
- `GoalCapabilities(backend_id='x')` construction with positional `backend_id` continues to work; `supports_multi_goal` defaults to `False`.
- All 60 pre-existing conformance tests (TEST 1–59) pass without modification.

### Doctor probe extension

`check_goal_backend()` now runs a new `list_goals()` probe inserted between the `list_archived()` probe and the `load_goal()` probe (making three probes in sequence — `list_archived()`, then `list_goals()`, then the heavy `load_goal()`):

- Calls `list_goals(agent_id)` and asserts the return value is a `list`.
- If `goal.md` exists, asserts `STANDING_GOAL_ID` appears in the returned list.
- A `list_goals()` that returns `None` or raises produces a FAIL result.

The `detail` dict now includes `goal_ids: list[str]` and `supports_multi_goal: bool` alongside the existing fields.

### Export round-trip gap (normative)

Multi-goal export (a round-trip that includes all addressed run-goals in a single `GoalExport`) is **out of scope for this PR**. The current `export()` guard (MUST: raise before reading any bytes when addressed goals are present) is the normative gap marker for this gap. See issue #643 for the tracking issue.

Agents with addressed run-goals MUST NOT be passed to the #379 export conformance harness until #643 ships — the harness would trigger the fail-loud guard and report a false failure.

### New conformance tests (TEST 60–129, `test_goal_multigoal_642.py`)

TEST 60–129 in `tests/test_goal_multigoal_642.py` (74 collected functions — TEST 113 is parametrized across five trailing-whitespace chars, so 70 TEST-number labels = 74 collected functions) cover:

- TEST 60–68: `validate_goal_id()` — all valid chars, reserved-name-first rejection, 64/65-char boundary, uppercase/separator/whitespace rejection, charset-passes-standing proof.
- TEST 69–71: `GoalCapabilities.supports_multi_goal` — default False, FilesystemGoalBackend True, bool type.
- TEST 72–74: `AddressableGoalBackend` Protocol — separate from GoalBackend, FilesystemGoalBackend isinstance, GoalBackend has no for_goal().
- TEST 75–86: `create_goal()` — happy path, `when` stamping, date.today() fallback, ts-first JSONL, conductor_run_id absent, required fields, GoalAlreadyExists collision, original-unchanged after collision, STANDING_GOAL_ID reject before I/O, invalid-charset reject before I/O, pre-probe raises before goal.md, write ordering (goal.md first).
- TEST 87–92: `list_goals()` — empty agent, standing-only, standing+run-goals sorted, partial debris skipped, non-conforming dirs skipped, '_standing' first.
- TEST 93–98: `for_goal()` — None routes to agent_root, '_standing' routes to agent_root, valid_id scopes goal_path, invalid charset raises, apply_transition writes to correct location, load_goal reads correct location.
- TEST 99–102: `export()` guard — raises when addressed goals present, no raise on empty goals/, succeeds with no goals/ dir, error message mentions '#643'.
- TEST 103–108: `GoalManager.for_goal()` — returns scoped manager, raises when goal absent, None raises, '_standing' raises, invalid charset raises, goal_path inside goals/<id>/.
- TEST 109–112: doctor `check_goal_backend()` — includes goal_ids + supports_multi_goal, includes '_standing' when goal.md present, PASS for empty list, FAIL when list_goals returns non-list.
- TEST 113–116: anchor / containment / fail-closed-dispatch negative controls — `validate_goal_id()` rejects trailing newline/CR/tab/VT/FF (the `\Z`-anchor fix; parametrized ×5), `for_goal()` charset gate rejects a trailing-newline id (shared-regex parity), `create_goal()` refuses a symlinked `goals/<id>` dir escaping the vault (dir-node containment), and a `GoalManager.for_goal()`-scoped `dispatch_as_outcome()` raises `NotImplementedError` (#580) instead of running an ungated LLM cost path.
- TEST 117: `create_goal()` leaf-node containment negative control — with `goals/<id>/` a legitimate directory but `goal_history.jsonl` pre-planted as a symlink to an out-of-vault target, `create_goal()` raises `PathTraversalError` and the `goal_created` audit line does NOT land outside the vault (closes the append-mode-follows-symlink escape the dir-node check alone does not — the P1 fix that routes the write through the contained `_append_jsonl`).
- TEST 118: `list_goals()` ordering negative control — a hyphen/digit-prefixed run-goal sorts BEFORE `'_standing'` (`'-'`/digit < `'_'` in ASCII), locking the corrected "sorts before alphabetic names, not unconditionally first" claim.
- TEST 119–124: `create_goal()` complete-on-partial recoverability (the #642 fix set) — fresh goal_id writes goal.md + one goal_created event (119); goal.md present WITH a goal_created event raises `GoalAlreadyExists` (COMPLETE, 120); goal.md present with EMPTY (`""`) goal_history.jsonl self-heals (appends the missing event, returns the PERSISTED goal — body-authoritative — and stays idempotent / no duplicate event on re-run, 121); a symlinked `goal_history.jsonl` leaf REFUSES with `PathTraversalError` and leaves NO goal.md committed (the two-leaf pre-verification / all-or-nothing control, distinct from TEST 117's no-escape control, 122); a stray regular FILE at `goals/<id>` raises `GoalAlreadyExists` (not a raw `FileExistsError`, 123); and a goal.md present alongside an unparseable `goal_history.jsonl` FAILS CLOSED (raises, leaving goal.md + the corrupt history untouched — no silent complete/overwrite, 124).
- TEST 125–128: the review-driven tightening of the complete-on-partial predicate (the #642 fix set continued) — a NUL-byte goal_id (`'a\x00b'`) is rejected by both `validate_goal_id()` and `for_goal()` with a valid-id positive control (125); `for_goal('a')` and `for_goal('b')` resolve to DIFFERENT `.goal.lock` paths, both nested under `goals/<id>/` (per-goal lock isolation, 126); `create_goal()` self-heals when `goal_history.jsonl` is ABSENT (unlinked, goal.md kept) — the other genuine partial shape distinct from TEST 121's empty-file case — appending exactly one goal_created and staying idempotent (127); and the load-bearing fail-closed branch: goal.md present with a `goal_history.jsonl` holding a transition event but NO goal_created marker RAISES `GoalAlreadyExists` (fail closed) and mints NO spurious goal_created, leaving history untouched (128 — the exact case the pre-fix predicate mis-healed; its `_count_goal_created == 0` assertion is the negative control).
- TEST 129: `list_goals()`/`for_goal()` containment agreement — `list_goals()` SKIPS an escaping symlinked goal dir whose `goal.md` resolves outside the vault, and `list_goals()` and `for_goal()` AGREE the id is unusable (the list/`for_goal` containment-consistency control detailed under "Containment consistency (#642 follow-up, Codex #3)" above; strip-RED negative control).

---

## Versioned normative addendum — PR2 #581: conductor gate statuses (2026-06-27)

This addendum documents a normative extension to `VALID_SUB_GOAL_STATUSES` and `SUB_GOAL_TRANSITION_FIELDS` that RE-LOCKs spec/41 after the conductor PR2 gate-suspension arc. The 13 Implementer Contract MUSTs are unchanged in count and normative text; this addendum tightens the *values* of two constants that MUST 6 normalizes.

### Two new statuses added to `VALID_SUB_GOAL_STATUSES`

`VALID_SUB_GOAL_STATUSES` now contains **7 members** (was 5 as of PR1 #580):

| Status | Terminal? | Who produces it | When |
|--------|-----------|-----------------|------|
| `pending` | No | GoalManager (init) | Sub-goal created, not started |
| `in_progress` | No | Coordinator pre-dispatch | Stage running |
| `complete` | Yes | Coordinator post-dispatch | Stage satisfied + result stored |
| `blocked` | No | Coordinator (terminal failure) | Max iterations reached / failed |
| `abandoned` | Yes | Conductor (halt ruling) | Operator halts or explicit abandon |
| `awaiting_decision` | **No** | Conductor gate path | Gate suspended, human decision pending |
| `skipped` | Yes | Conductor gate (skip ruling) | Gate answered with disposition='skip' |

**`blocked`** — NOT terminal (corrected #581 PR2: the table previously marked it `Yes`). `blocked` is the coordinator's terminal *mapping* for a max-iterations/failed dispatch, but it is NOT terminal-*done*: `evaluate_completion()` requires `blocked == 0` for `all_done` (a blocked sub-goal blocks completion), and the conductor resume cursor NORMALIZES `blocked → in_progress` and RE-RUNS it (recovery, because `blocked` is conductor/coordinator-produced). Contrast `abandoned`, which IS terminal on resume (human-set, never auto-revived).

**`awaiting_decision`** — NOT terminal. A sub-goal in this status means the conductor has paused at a gate stage and is waiting for a human decision via `conductor.resume()`. The run returns `ConductorState(status='awaiting_decision')`. The resume cursor is this status (not the audit event); the next `run()` call with the same `conductor_run_id` will re-surface the suspension.

**`skipped`** — Terminal-done. Produced when the operator calls `resume()` with `disposition='skip'`. The sub-goal is permanently skipped. It counts as terminal-done in `evaluate_completion()` (does NOT block `all_done`), and `completed_stage_ids` includes skipped stage IDs. This is a DISTINCT terminal status from `abandoned` (which means "halted by halt ruling") — do NOT reuse `blocked` or `abandoned` for this purpose (ruling: awaiting-decision-enum-rollout, REJECTED).

### `gate_decision_id` added to `SUB_GOAL_TRANSITION_FIELDS`

`SUB_GOAL_TRANSITION_FIELDS` gains `gate_decision_id` (was: `assigned`, `deadline`, `blocked_by`, `completed`, `output`, `body`, `acceptance_criteria`). This field carries the conductor's CAS-verification token for gate stages:

- Written on suspension: `apply_transition(to_status='awaiting_decision', fields={'gate_decision_id': decision_id})`.
- Read on resume: `_record_gate_answer()` loads the goal, finds the sub-goal whose `gate_decision_id` matches the caller's `decision_id`, then calls `apply_transition()` with `expected_from_status='awaiting_decision'` (MUST 10 CAS guard). A mismatched or absent `gate_decision_id` raises `GoalConcurrentModification` (stale/duplicate rejection — c5 ruling).

### Conductor gate audit events (append-only, NOT normative for GoalBackend)

Two JSONL event names are introduced in `goal_history.jsonl` by the conductor (not by GoalBackend itself — the conductor writes them via `append_history_event` / the `history_event` parameter to `apply_transition`). They are documented here for cross-spec discoverability:

- `conductor_gate_pending` — written on gate suspension. Fields: `ts`, `event`, `conductor_run_id`, `stage_id`, `decision_id`, `prompt`, `options`, `context_ref`, `held_conflict_keys`.
- `conductor_gate_answered` — written on resume (embedded as `history_event` in the `apply_transition()` call — transition-first, MUST 6 compliance). Fields: `ts`, `event`, `conductor_run_id`, `stage_id`, `decision_id`, `disposition`, `answer`, `answered_by`, `answered_at`, `rationale`.

The two events are linked by `decision_id`. The resume cursor is the sub-goal STATUS via `apply_transition`, NOT event replay (the events are pure audit; the status is the durable machine state — C1/C2 principle).

### Impact on `evaluate_completion()`

`CompletionEvaluation` gains two additive fields (default 0, backward-compatible):
- `sub_goals_awaiting_decision: int` — count of sub-goals in `awaiting_decision` status.
- `sub_goals_skipped: int` — count of sub-goals in `skipped` status.

`all_done` is now negated by any `awaiting` count in addition to `pending` / `in_progress` / `blocked`. `skipped` sub-goals are terminal-done and do NOT block `all_done` — a fully-skipped playbook can complete.

---

## Versioned normative addendum — #582 PR3 (2026-06-27): conductor concurrency + conflict serialization

This addendum records the Protocol surface additions and behavioral contracts introduced in #582 PR3. The additions close the per-goal lock gap (#655) and add an inner-lock decision CAS (MUST 14) to guard against stale resume() races that the outer expected_from_status check cannot catch alone.

### MUST 14 — `apply_transition()` inner-lock decision CAS (`expected_decision_id`)

`apply_transition()` gains an optional `expected_decision_id: str | None = None` keyword parameter. When non-None, the backend MUST verify — **under the goal lock, after the `expected_from_status` check** — that the current sub-goal's `gate_decision_id` matches the supplied value. If it does not match, the backend MUST raise `GoalConcurrentModification` and perform NO writes (goal.md and JSONL must remain unchanged).

This closes a race that `expected_from_status` alone cannot catch: two concurrent `resume()` callers, both finding `status='awaiting_decision'`, both pass the `expected_from_status` CAS. The one that acquires the lock first transitions the status to `complete`; the second's `expected_from_status` check would then *correctly* catch the status change. But there is a TOCTOU window: if the second caller reads the status *before* the first writes, both could proceed past the outer check. The `expected_decision_id` check runs under the lock, after the status check, closing that window.

**Behavior contract:**
- `expected_decision_id=None` (the default): no decision-id check is performed — backward-compatible with all callers that do not supply it.
- `expected_decision_id=<id>` and `sub_goal.gate_decision_id == <id>`: the CAS passes, writes proceed normally.
- `expected_decision_id=<id>` and `sub_goal.gate_decision_id != <id>`: raise `GoalConcurrentModification` with a message that mentions "expected gate_decision_id". No writes are performed.

**Conformance:** TEST 64 in `test_goal_backend_conformance.py`.

### `save_goal()` per-goal lock (#655 closed)

`save_goal()` previously bypassed the per-goal lock, creating a write-gap where a concurrent `apply_transition()` could interleave with a `save_goal()` on the same goal_id. In PR3, `save_goal()` now acquires `self._goal_lock()` before calling `self._write_goal(goal)`, matching the lock discipline of `apply_transition()` and `archive_goal()`. This closes #655 (single-writer assumption).

**Impact on callers:** `save_goal()` is now serialized per-goal. Callers holding an outer lock that already includes `_goal_lock()` must not call `save_goal()` while holding that lock (deadlock risk). The reference impl does not do this; custom backends should be aware.

### `held_conflict_keys` added to `SubGoal` and `SUB_GOAL_TRANSITION_FIELDS`

`SubGoal` gains `held_conflict_keys: list[str]` (default `[]`, backward-compatible). This field stores the `StageSpec.conflict_keys` copied onto the sub-goal at gate suspension time. It exists so `_scan_active_conflicts()` in the conductor can detect conflicts in O(n_goals) goal loads instead of O(n_goals × n_events) JSONL parses.

`SUB_GOAL_TRANSITION_FIELDS` gains `"held_conflict_keys"` so it can be written via `apply_transition(fields={'held_conflict_keys': [...]})` (written at suspension time) and cleared via `apply_transition(fields={'held_conflict_keys': []})` (written at gate answer time, alongside `gate_decision_id: None`).

`serialize_sub_goal()` now emits `held_conflict_keys` in the serialized dict when the list is non-empty. This keeps goal.md clean for the common case (automated stages, no conflict scope) while ensuring the field round-trips correctly through save_goal/load_goal for gate sub-goals that have it set.

**Conflict scan protocol:** the conductor's `_scan_active_conflicts(agent, stage_conflict_keys, own_conductor_run_id)` iterates `list_goals()`, loads each goal via `for_goal(id).load_goal()`, and checks for sub-goals with `status='awaiting_decision'` and `held_conflict_keys` that intersect `stage_conflict_keys`. It skips the caller's own `conductor_run_id`. Returns `(blocking_run_id, blocking_decision_id)` on the first hit, or `None` if no conflict found. Errors on individual goal loads are swallowed (fail-open: a load error cannot prove a conflict).

### Backward compatibility

All changes are additive and backward-compatible:
- `expected_decision_id=None` is the default, so all existing `apply_transition()` callers are unaffected.
- `held_conflict_keys: list[str] = field(default_factory=list)` is a default-empty field; existing SubGoal constructions are unaffected.
- `serialize_sub_goal()` only emits `held_conflict_keys` when non-empty; existing goal.md files round-trip unchanged.
- `save_goal()` acquiring the lock is a behavioral correctness fix, not a protocol change.

# spec/41: GoalBackend Protocol

> **Status:** LOCKED — Protocol scaffold: #425 PR1 (2026-06-11); write-path adoption: #448 PR1 (2026-06-13); audit + CAS conformance: #448 PR2 (2026-06-13); coordinator + fail-closed cost gate + spec/41 LOCK: #448 PR3 (2026-06-13); clock-injection addendum + GoalManager shim + agent_root resolution: #483 PR1 (2026-06-13). Conformance suite covers all 10 Implementer Contract MUSTs for `FilesystemGoalBackend` (`test_goal_backend_conformance.py`, 60 tests) plus filesystem-specific tests (`test_goal_filesystem.py`) and coordinator integration tests (`test_goal_coordinator.py`, 19 tests). Goal is also registered in the shared #379 export conformance harness (`test_export_protocol_conformance.py`) and the capability-advertisement harness (`test_export_capability_advertisement.py`: 2 goal-specific tests — `test_goal_backend_advertises_canonical_export`, `test_goal_backend_is_exportable` — plus the shared `test_all_capability_flags_are_bool` parametrization extended to cover goal). The four pre-existing goal tests (`test_goal.py`, `test_agent_goal_loading.py`, `test_dashboard_goals.py`, `test_goal_outcome_composition.py`) remain the zero-behavior-change regression guard (assertions unchanged); archive golden assertions updated in #448 PR1 for the A3 data-loss fix (the one sanctioned exception to the freeze), and the `test_goal_outcome_composition.py` `agent_with_goal` fixture gained a minimal `persona/IDENTITY.md` in #448 PR3 so the shim can construct the real `AtomicAgent` the now-live cost gate requires (fixture-only; every assertion is byte-identical).

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
- **PR 3 (#448 PR3, this PR).** Goal-outcome coordinator (`goal/coordinator.py` — thin free function `dispatch_sub_goal_as_outcome()`); fail-closed cost gate (`CostGuardrailBlocked`); compare-and-set `apply_transition()` (`expected_from_status` param, `GoalConcurrentModification`); `dispatch_as_outcome()` refactored to a thin shim; coordinator integration tests (`test_goal_coordinator.py`, 19 tests); 2 CAS conformance tests (TEST 54–55, total 56 conformance tests as of PR3); spec/41 LOCKED.
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

## Implementer Contract (10 MUSTs)

These MUSTs bind every conforming GoalBackend implementation. The conformance test suite in `tests/test_goal_backend_conformance.py` covers all ten (60 tests total, including 2 CAS tests at TEST 54–55 and 4 clock-injection / key-ordering tests at TEST 56–59).

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

`apply_transition()` MUST also **enum-validate fail-closed**: if `to_status` is not a member of `VALID_SUB_GOAL_STATUSES`, it MUST raise `SchemaValidationError` **before any write** (no partial `goal.md`, no orphan JSONL line). The `fields` parameter is a constrained channel: it MAY set only transition metadata — the members of `SUB_GOAL_TRANSITION_FIELDS` (`assigned`, `deadline`, `blocked_by`, `completed`, `output`, `body`, `acceptance_criteria`) — and MUST NOT reach `sub_goal.status` (the sole status channel is `to_status`, so `fields={"status": <anything>}` MUST NOT be persisted) or the immutable identity fields `id`/`label` (so `fields={"id": …}` MUST NOT rewrite a sub-goal's identity mid-transition). The allow-set fails closed: any key not in `SUB_GOAL_TRANSITION_FIELDS` — including a future `SubGoal` field — is ignored, not blindly applied. This guarantees the backend never persists a `goal.md` its own `load_goal()` would reject (no write-time/read-time validation asymmetry) and never silently corrupts sub-goal identity. The conformance suite pins the `status` side-door, the `id`/`label` identity side-door, and a co-passed legitimate field across **every** backend. Transition-graph *ordering* legality is NOT enforced here — see §"WritePolicy applicability" (a) vs (b).

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

`GoalManager.dispatch_as_outcome()` is now a thin shim that delegates to `dispatch_sub_goal_as_outcome()` in `goal/coordinator.py`. The coordinator is a thin free function (NOT a `GoalBackend` method) that composes the backend with `OutcomeRunner` and enforces the pre-dispatch fail-closed cost gate. The cost gate is **live on the CLI path** (`python -m atomic_agents.goal dispatch-outcome`): the shim constructs a real `AtomicAgent` (keyword args `name`/`agents_root`/`goal_backend`, outside any try/except so a construction failure propagates) and passes it to the coordinator, so the gate consults the same budget universe (model.md caps) the `OutcomeRunner` will spend. A `CostGuardrailBlocked` propagates to the CLI as exit 3.

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

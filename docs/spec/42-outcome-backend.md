# spec/42: OutcomeBackend Protocol

> **Status:** LOCKED at PR 2 (issue #448 PR2, 2026-06-13). Conformance suite covers all 9 Implementer Contract MUSTs for `FilesystemOutcomeBackend` (`test_outcome_backend_conformance.py`, 76 tests) plus write-path adoption golden tests (`test_outcome_adoption_golden.py`) and filesystem-specific tests. OutcomeBackend is also registered in the shared spec/40 export conformance harness (`test_export_protocol_conformance.py`) and the capability-advertisement harness (`test_export_capability_advertisement.py`: 2 outcome-specific tests — `test_outcome_backend_advertises_canonical_export`, `test_outcome_backend_is_exportable` — plus the shared `test_all_capability_flags_are_bool` parametrization extended to cover outcome). The two pre-existing outcome tests (`test_outcome.py`, `test_goal_outcome_composition.py`) remain the zero-behavior-change regression guard.
>
> **Note:** The issue body and PR1 kickoff scope reference spec/13 as the cross-link. This is a typo. The correct cross-reference is **spec/14 (Outcomes)**, where the outcome behavior narrative lives. spec/13 covers Research Integrity, an unrelated domain. This note self-documents the correction.

---

## Origin

Carved out from the flat `outcome.py` module in place since the framework's initial outcome support. Filed as [#426](https://github.com/dep0we/atomic-agents-stack/issues/426) as part of the #383 four-protocol wave (alongside GoalBackend #425, MigrationBackend #429, and a future DreamBackend child arc).

The motivation: `outcome.py` held `OutcomeRunner` (a runtime object), `OutcomeResult`/`IterationRecord` dataclasses (domain model), and the CLI entry point all in a single flat module. The backend protocol separates the storage abstraction (`OutcomeBackend`) from the runtime behavior (`OutcomeRunner`, now in `_outcome_impl.py`), following the pattern established by GoalBackend (spec/41).

**Cross-links:**
- spec/14. Outcomes. The behavioral narrative: what `result.json` contains, the iterate-to-rubric loop, `OutcomeRunner` lifecycle.
- spec/12. Goals and Intent. The `dispatch_as_outcome()` pathway that produces `OutcomeResult` objects.
- spec/40. Canonical Export. `OutcomeExport` is a first-class `ExportableResult`; `FilesystemOutcomeBackend` implements `Exportable`.
- spec/27. Doctor. `check_outcome_backend()` uses the dual-probe pattern (`list_runs` + `read_result`).

---

## Shipping history (2 PRs)

- **PR 1 (#426, merged).** Protocol scaffold + dataclasses + capability advertisement + `FilesystemOutcomeBackend` reference impl + compatibility re-export (`outcome.py` → `outcome/` package; `from atomic_agents.outcome import OutcomeRunner` stays a supported public import, NOT deprecated) + `OutcomeRunner` relocated to `_outcome_impl.py` + `OutcomeResult`/`IterationRecord` canonical types in `outcome/types.py` + `OutcomeExport` (an `ExportableResult` subclass) wired into `atomic_agents.export` + `check_outcome_backend()` doctor check + `AtomicAgent.outcome_backend` scaffolding attribute (scaffolding-only at PR 1 — upgraded to the per-agent coordinator/inspection handle at PR 2) + full conformance suite + spec/42 DRAFT.

- **PR 2 (#448, merged).** Replaces the single `run()` call site — `self._write_result_json(output_dir, result)` → `self.outcome_backend.write_result(self.agent_name, run_id, result)`. `_write_result_json` stays intact as the reference serializer (TEST 30 in `test_outcome_backend_conformance.py` depends on it). Adds the `outcome_backend=` kwarg to `OutcomeRunner.__init__` (keyword-only, default `get_default_outcome_backend(self.agent_root)`, kwarg-wins-over-env). `AtomicAgent.outcome_backend` remains the per-agent HANDLE for operator inspection and the future PR3 coordinator — it is NOT the write path. `AtomicAgent` does NOT construct or feed an OutcomeRunner (GoalManager and the CLI do that independently). Locks spec/42.

---

## Overview

`OutcomeBackend` is the **fifteenth** backend Protocol in the protocol-pattern series (counting the fourteen from v1.0 + v1.5). It abstracts outcome run state persistence (writing `result.json`, reading completed runs, enumerating run history, and exporting run state portably) behind a Protocol so the framework's core stays small and alternate outcome substrates can drop in without forking.

The framework's existing flat `outcome.py` module has been **superseded**: `OutcomeRunner` now lives in `_outcome_impl.py`; `OutcomeResult`/`IterationRecord` canonical types live in `outcome/types.py`; the `atomic_agents.outcome` import path is preserved via `outcome/__init__.py`'s module-level re-exports.

---

## Module layout

```
atomic_agents/outcome/
├── __init__.py        # registry: register_outcome_backend /
│                      # get_outcome_backend / list_outcome_backends +
│                      # get_default_outcome_backend factory +
│                      # backward-compat re-exports (OutcomeRunner,
│                      # OutcomeResult, IterationRecord, AtomicAgent,
│                      # _llm module, private test-facing names)
├── types.py           # canonical types: OutcomeResult, IterationRecord
│                      # (MUTABLE), OutcomeCapabilities (frozen),
│                      # OutcomeExport (ExportableResult subclass)
├── backend.py         # OutcomeBackend Protocol (@runtime_checkable)
├── filesystem.py      # FilesystemOutcomeBackend reference implementation
├── _outcome_impl.py   # OutcomeRunner (runtime behavior); imports canonical
│                      # types from outcome/types.py. AtomicAgent imported
│                      # LAZILY inside run() so test patches on the outcome
│                      # namespace work.
└── __main__.py        # python -m atomic_agents.outcome CLI entrypoint

atomic_agents/_export_base.py   # ExportableResult marker base (leaf module both
                                  # outcome/types.py and export/types.py import to
                                  # avoid a circular import)
```

Package name `outcome/` replaces the flat `outcome.py`. Python resolves `atomic_agents.outcome` to the package; the shim's module-level re-exports preserve backward-compatible access to `OutcomeRunner`, `OutcomeResult`, `IterationRecord`, and private names used by the test suite.

---

## Deliberate divergence: mutable dataclasses

`OutcomeResult` and `IterationRecord` are **mutable** dataclasses (`@dataclass`, not `@dataclass(frozen=True)`). This is a deliberate divergence from the frozen-DTO convention used by `LogEntry` (spec/22) and other read-side types.

**Rationale:** The outcome layer is a state machine DURING the run. `OutcomeRunner.run()` mutates `result.status`, `result.explanation`, `result.iterations`, and `result.output_files` in-place across the iterate-to-rubric loop. Freezing these types would break every mutation in the loop. `OutcomeCapabilities` is frozen because it is a pure value object returned by `capabilities()`, not a state-bearing object. This mirrors `goal/types.py`'s `Goal`/`SubGoal` mutable + `GoalCapabilities` frozen pattern exactly.

The `outcome/types.py` module docstring documents this divergence explicitly with the same wording as `goal/types.py`.

---

## Protocol method surface — THIN envelope-only

Per the `protocol-method-surface` arc ruling: the `OutcomeBackend` Protocol exposes only storage primitives. Artifact discovery (output_dir.glob diffing between iterations), output_dir resolution, and run_id minting STAY in `OutcomeRunner` ABOVE the Protocol.

```
write_result(agent_id, run_id, result)  — write result.json for one run (write-once)
read_result(agent_id, run_id)           — read + reconstruct OutcomeResult
list_runs(agent_id)                     — enumerate run_ids (sorted, lexicographic)
export(query=None)                      — spec/40 canonical export (portable artifact refs)
export_all()                            — convenience alias for export(None)
capabilities()                          — return OutcomeCapabilities

properties:
  backend_id                            — stable backend identifier string
```

No `query/filter` method ships in this version (deferred to #454 until the outcome-catalog consumer's PR lands with its known filter shape, per Principle #2 "no abstractions for hypothetical future needs").

---

## Artifact-reference portability

**On-disk `result.json` is byte-identical to the pre-adoption serializer.** The live write path now routes through `self.outcome_backend.write_result()` (#448 PR2); `OutcomeRunner._write_result_json` is retained only as the byte-identity reference serializer (`test_outcome_backend_conformance.py` TEST 30 + `test_outcome_adoption_golden.py` pin `write_result` against it). For the DEFAULT `output_dir` the file lands at the same canonical location as before (`outcomes/runs/<run_id>/result.json` — byte-identical-location). For a CUSTOM `--output-dir` the audit envelope now relocates to the canonical `outcomes/runs/<run_id>/result.json` (the A1 conscious correctness fix — see §Shipping history PR 2), while agent artifact files still go to `output_dir`.

> The #426 PR1 design here was "Option C" — keep `_write_result_json` as the direct `atomic_write` write path and leave a custom-`output_dir` `result.json` where the operator pointed it. A1 (#448 PR2) deliberately superseded that: a filesystem-only "write the receipt to an arbitrary operator folder" model cannot survive a swapped backend keyed by `run_id`, and it left custom-`output_dir` receipts invisible to `list_runs`/`read_result`/`export` (the orphan bug). The backend now owns the canonical path.

**The net-new `FilesystemOutcomeBackend.export()` emits PORTABLE artifact references** by rebasing absolute artifact paths to relative-to-`agent_root`:

```python
if p.is_relative_to(self._agent_root):
    artifact_refs.append(str(p.relative_to(self._agent_root)))
else:
    # Fallback: artifact outside agent_root — keep absolute
    artifact_refs.append(str(p))
```

**Artifact paths in the export are:**
- Relative to `agent_root` when the artifact was written under `agent_root` (the common case, e.g. `outcomes/runs/outcome-20260611-.../output.md`).
- Absolute when the artifact lives outside `agent_root` (operator-specified `output_dir` outside the agent vault — the `is_relative_to` fallback).

**Callers requiring fully portable exports MUST ensure `output_dir` is under `agent_root`.**

**Deliberate departure from the arc ruling's literal pattern.** The `artifact-reference-portability` arc ruling cited the `relative_to(self.agents_root)` pattern from the old `outcome.py:555/565-566/572` sites as the rebasing shape. This implementation rebases against **`agent_root`** (this agent's own root) rather than `agents_root` (the fleet root). Rationale: Principle #1 ("same files = same agent") is about a **whole-agent move** — an export whose artifact refs are relative to the agent's own root survives the agent directory being relocated anywhere on disk, which is the portability property OutcomeBackend exists to deliver (T15 / Position B). An `agents_root`-relative ref (fleet-prefixed, `agentname/outcomes/...`) would bake the fleet layout into the export. `FilesystemOutcomeBackend` is therefore scoped to one `agent_root`; `agents_root` is not retained as state. The departure is recorded here and in the PR body per the "no silent ruling deviation" rule.

`OutcomeResult.output_files` and `IterationRecord.artifact_path` stay `Path`-typed ON the dataclass. Portability is handled at `export()`, NOT by retyping the dataclass (retyping would change on-disk bytes, violating the zero-behavior-change guarantee).

`OutcomeExport.artifact_refs` are agent_root-relative strings (or absolute fallback), NOT reconstructable as `OutcomeResult.artifact_path` without re-rooting. `OutcomeResult.from_dict()` is for on-disk result.json (absolute paths) ONLY.

---

## Types

### OutcomeResult (mutable)

```python
@dataclass
class OutcomeResult:
    run_id: str
    description: str
    rubric_source: str
    max_iterations: int
    status: str           # 'satisfied' | 'max_iterations_reached' | 'failed' | 'interrupted'
    explanation: str
    iterations: list[IterationRecord]
    final_iteration_idx: int
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    started_at: str       # ISO 8601
    ended_at: str
    output_files: list[Path]  # Path-typed; coerced to str in to_dict()/write_result()
```

NOTE: `OutcomeResult` and `IterationRecord` are MUTABLE dataclasses — deliberate divergence from the frozen-DTO convention in `logs/types.py`. The outcome layer is a state machine DURING the run: `status`/`explanation`/`iterations`/`output_files` are mutated across the `OutcomeRunner` loop. Freezing would break the mutation pattern. See `goal/types.py` for the GoalBackend analog.

### IterationRecord (mutable)

```python
@dataclass
class IterationRecord:
    iteration: int
    agent_response: str
    agent_input_tokens: int
    agent_output_tokens: int
    agent_cost_usd: float
    agent_latency_ms: int
    judge_response_raw: str
    judge_verdict: dict
    judge_cost_usd: float
    judge_input_tokens: int
    judge_output_tokens: int
    artifact_path: Path | None  # Path-typed; coerced to str in to_dict()/write_result()
    timestamp: str
```

### OutcomeCapabilities (frozen)

```python
@dataclass(frozen=True)
class OutcomeCapabilities:
    backend_id: str
    supports_canonical_export: bool = False
    supports_artifact_storage: bool = False
    # DEFERRED: supports_run_query — ships with the filter method (Principle #12)
```

`FilesystemOutcomeBackend.capabilities()` returns `OutcomeCapabilities(backend_id="filesystem", supports_canonical_export=True, supports_artifact_storage=True)`.

### OutcomeExport (ExportableResult subclass)

```python
@dataclass
class OutcomeExport(ExportableResult):
    run_id: str                   # most recent run_id, or "" when no runs
    result_json_bytes: bytes      # raw bytes of result.json (byte-identical to on-disk)
    artifact_refs: list[str]      # relative-to-agent_root (or absolute fallback)
    backend_id: str               # e.g. "filesystem"
    scope: str                    # agent root path as string
```

`OutcomeExport` is defined in `outcome/types.py` (imports `ExportableResult` from `_export_base.py` — the leaf module, NOT from `export/types.py`, to avoid a circular import). It is re-exported from `atomic_agents/export/types.py` so callers can use `from atomic_agents.export import OutcomeExport`.

---

## Implementer Contract — 9 MUSTs

The base shape is the 8-MUST PersonaBackend base (spec/33); the unique 9th MUST is write-once / result-immutability.

**MUST 1 — side-effect-free construction.**
Backend construction MUST be side-effect-free. `FilesystemOutcomeBackend.__init__` MUST NOT create `outcomes/` or any subdirectory. The `outcomes/runs/<run_id>/` directory is created lazily by `write_result()` only. Conformance: `test_construction_is_side_effect_free`.

**MUST 2 — capability honesty.**
`capabilities()` MUST return an `OutcomeCapabilities` instance where all boolean fields are Python `bool` (not truthy int or `None`). A capability advertised as `True` MUST be backed by a working implementation that does not raise on the corresponding operation. A capability advertised as `False` (or absent via default) MUST NOT have been silently implemented as a no-op. `FilesystemOutcomeBackend` MUST advertise `supports_canonical_export=True` and `supports_artifact_storage=True`. Conformance: `test_capabilities_returns_outcome_capabilities`, `test_capabilities_fields_are_bool`, `test_filesystem_advertises_canonical_export`, `test_filesystem_advertises_artifact_storage`.

**MUST 3 — list_runs() returns [] when outcomes/ absent, sorted otherwise.**
`list_runs(agent_id)` MUST return `[]` when `outcomes/runs/` does not exist. MUST NOT raise `FileNotFoundError`. MUST return run_ids in lexicographic (sorted) order. Conformance: `test_list_runs_returns_empty_when_absent`, `test_list_runs_returns_sorted`.

**MUST 4 — write_result() uses atomic writes.**
`write_result(agent_id, run_id, result)` MUST use temp+fsync+rename (`atomic_write`) so `result.json` is always complete on disk (no half-written files). Filesystem reference impl uses `atomic_write` from `_io.py`.

**MUST 5 — read_result() round-trips write_result() exactly.**
`read_result(agent_id, run_id)` MUST reconstruct an `OutcomeResult` with `Path`-typed fields (`output_files` as `list[Path]`, `IterationRecord.artifact_path` as `Path | None`). The reconstructed result MUST be semantically equivalent to the original (same `run_id`, `status`, `iterations`, `output_files`). Conformance: `test_write_read_roundtrip`.

**MUST 6 — read_result() raises OutcomeCorrupted for unparseable result.json.**
When `result.json` is present but cannot be parsed as a valid `OutcomeResult` (invalid JSON, missing required fields, wrong types), `read_result()` MUST raise `OutcomeCorrupted` (a subclass of `AtomicAgentsError`). `OutcomeCorrupted` is defined in `atomic_agents.exceptions` alongside `GoalCorrupted`. Conformance: `test_read_result_corrupt_json_raises_outcome_corrupted`, `test_read_result_missing_fields_raises_outcome_corrupted`.

**MUST 7 — read_result() raises AtomicAgentsError for absent run.**
When no `result.json` exists for the given `run_id`, `read_result()` MUST raise `AtomicAgentsError` (NOT `OutcomeCorrupted` — the run is absent, not corrupt). Conformance: `test_read_result_absent_raises_error`.

**MUST 8 — backend_id stability.**
`backend_id` MUST be a stable, non-empty string that does not change between construction calls. `FilesystemOutcomeBackend.backend_id` is always `"filesystem"`. Operator deployments may pin against these strings. Conformance: `test_filesystem_backend_id_is_filesystem`.

**MUST 9 — write-once / result-immutability (the unique axis).**
Once `result.json` is written for a `run_id`, no second write is permitted. `write_result()` MUST check existence before writing and MUST raise `AtomicAgentsError` if `result.json` already exists for the `run_id`. Rationale: outcome runs write a terminal, judged envelope keyed by `run_id`; mutating a completed result post-facto would corrupt the audit trail. Conformance: `test_write_result_write_once_contract`.

---

## Export fidelity (spec/40 addendum)

Export fidelity is a spec/40 addendum concern, NOT a core MUST (following GoalBackend spec/41 precedent). The export contract:

1. `FilesystemOutcomeBackend.export()` returns an `OutcomeExport` with `result_json_bytes` byte-identical to the on-disk `result.json`.
2. Artifact refs in the export are relative-to-`agent_root` where possible (is_relative_to guard), absolute otherwise (fallback).
3. `export_all()` is a convenience alias for `export(None)`.

**Deliberate single-run scope (divergence from spec/40 `export_all()` wording).** The spec/40 `Exportable.export_all()` contract documents `export_all` as "unbounded export (all records, no filter)." OutcomeBackend's `OutcomeExport` shape is **single-run** — one `run_id`, one `result_json_bytes`, one `artifact_refs` — so both `export()` and `export_all()` here deliberately return ONLY the most-recent run (`list_runs()[-1]`), NOT every run. This is honest for the current implementation: the outcome-catalog / cross-run quality-rollup consumer that needs multi-run export builds it OVER `read_result`/`export` for now, and a multi-run `OutcomeExport` shape (carrying `list[run]`) ships WITH that consumer's PR and its conformance tests — filed inline as [#454](https://github.com/dep0we/atomic-agents-stack/issues/454). Adding a multi-run shape now would be an abstraction for a hypothetical future need (Principle #2/#6). The `export()`/`export_all()` docstrings carry the same note.

The golden-file conformance test asserts BOTH (a) byte-identical on-disk `result.json` AND (b) a portable export with relative artifact refs (`test_golden_file_byte_identity`, `test_export_artifact_refs_are_relative`).

---

## Operator override surface

```
env var:   ATOMIC_AGENTS_OUTCOME_BACKEND    (default: "filesystem")
factory:   get_default_outcome_backend(agent_root: Path) → OutcomeBackend
scope:     ONE agent root — <agent_root>/outcomes/runs/<run_id>/result.json
```

`OutcomeBackend` is scoped to ONE agent root (matching `get_default_goal_backend`). `get_default_outcome_backend(agent_root)` takes `agent_root`, NOT `agents_root`.

`agents_root` is derived as `agent_root.parent` (the framework-wide invariant: `agents_root / agent_name = agent_root`). Operators with non-standard layouts (multi-tenant, nested agents) should instantiate `FilesystemOutcomeBackend(agents_root, agent_name)` directly.

`AtomicAgent.outcome_backend` is a public attribute initialized at construction with `get_default_outcome_backend(self.agent_root)`. It is the per-agent HANDLE for operator inspection and the future PR3 goal-outcome coordinator. **It is NOT the write path.** The live write path routes through `OutcomeRunner.outcome_backend` (active as of #448 PR2) — resolved independently by the runner via `get_default_outcome_backend(agent_root)` at `OutcomeRunner.__init__` time, or via the `outcome_backend=` kwarg added to `OutcomeRunner.__init__` in #448 PR2. `AtomicAgent` does NOT construct or feed an OutcomeRunner; the two `outcome_backend` instances are separate objects (both pointing at the same agent root in the default case). Do NOT add `outcome_backend=` to `AtomicAgent.__init__` (the runner is independently constructed — a kwarg on AtomicAgent would be dead code in the same shape #426 warned against).

---

## Doctor check

`doctor.check_outcome_backend(agent_root)` validates the configured backend:

**Light probe:** `list_runs(agent_id)` — MUST NOT raise even when `outcomes/` is absent. Returns `[]` for agents with no completed outcome runs (normal state — reactive agents that have never run an outcome).

**Heavy probe:** `read_result(agent_id, run_id)` for the most recent run returned by `list_runs()`, skipped (with `outcome_runs_present=False`, `read_result_probed=False`, `run_count=0` in the PASS detail) when `list_runs()` returns `[]`. The limited-probe-depth when no runs exist is documented in the check's detail dict — not a shortcut, an honest boundary.

The full PASS detail dict the implementation returns is: `backend_id`, `outcome_runs_present` (did `list_runs()` find any runs on disk), `run_count` (`len(list_runs())`), `read_result_probed` (was the heavy probe run), `read_result_vanished` (`True` only on the benign TOCTOU path — `list_runs()` returned a run but `read_result()` found it gone to concurrent cleanup; still PASS), `supports_canonical_export`, and `supports_artifact_storage`. The conformance suite asserts `run_count` directly (`== 0` with no runs, `== 1` after one write).

`OutcomeCorrupted` is caught BEFORE bare `AtomicAgentsError` so corruption (real FAIL) is not swallowed by the TOCTOU absent-run race (benign fall-through to PASS). This is the dual-probe pattern from `MEMORY.md feedback_doctor_dual_probe_pattern`.

PASS / FAIL ladder:
- **FAIL:** `get_default_outcome_backend()` raises; OR `list_runs()` raises; OR runs exist AND `read_result()` raises.
- **PASS:** factory, list_runs, and (when runs exist) read_result all succeed.

---

## Per-iteration telemetry boundary

Per-iteration telemetry STAYS in `LogBackend` (`PRIMITIVE_OUTCOME_ITERATION` / `_append_iteration_log`). `OutcomeBackend` covers only the terminal `result.json` envelope. These are intentionally non-redundant: the `LogBackend` stream is the queryable audit tape; the `result.json` envelope is the portable self-contained result file. The same fact serving two consumers is how a real audit trail works (spec/42 `iteration-telemetry-boundary` ruling, #383 scope).

---

## dream.py scope boundary

`dream.py`'s `manifest.json` stays dream-scoped (a future `DreamBackend` child arc of #383). `OutcomeBackend` covers `result.json` ONLY. The two documents share envelope fields (`status`, `tokens`, `cost`, `timestamps`) but model unrelated lifecycles (`OutcomeResult`: rubric + iteration list + convergence index; `DreamResult`: consolidated notes + promoted notes + stale markings). Principle #3 says stop there.

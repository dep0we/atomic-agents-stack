# spec/44: QueueBackend Protocol

> **Status:** DRAFT at PR 1 (issue #428). Conformance suite covers all 12 Implementer Contract MUSTs for `FilesystemQueueBackend` (`test_queue_backend_conformance.py`) plus filesystem-specific tests (`test_queue_filesystem.py`). QueueBackend is also registered in the shared #379 export conformance harness (`test_export_protocol_conformance.py`) and the capability-advertisement harness (`test_export_capability_advertisement.py`). This is a SCAFFOLDING-ONLY carve — zero internal runtime callers wired; the `_cascade.py` free-function API is preserved via a thin non-deprecated shim. Closes TENSIONS T4.

---

## Origin

`QueueBackend` is the **seventeenth** backend Protocol in the protocol-pattern series (last of the v1.5 wave, alongside GoalBackend spec/41, OutcomeBackend spec/42, JournalBackend spec/43). It carves the cascade work-queue cluster from `atomic_agents/_cascade.py` into a swappable Protocol so Redis / SQS / DB backends can plug in without forking the claim logic.

Prior to spec/44, queue-claim atomicity was locked to POSIX `Path.rename()` (T4 in `docs/TENSIONS.md`). The logic was embedded in free functions in `_cascade.py`:
`_sidecar_path`, `_write_sidecar`, `claim_next_queued`, `release_claim`, `move_to_dead_letter`, `recover_stale_claims`, `renew_lease`.

Filed as [#428](https://github.com/dep0we/atomic-agents-stack/issues/428) as the seventeenth backend protocol for v1.5.

**Cross-links:**

- spec/06. Multi-agent projects. Queue state vocabulary defined here (see §State vocabulary mapping).
- spec/27. Doctor. `check_queue_backend()` uses the dual-probe pattern + single_host_only WARN.
- spec/40. Canonical Export. `QueueExport` is a first-class `ExportableResult`; `FilesystemQueueBackend` implements `Exportable`.
- spec/44 completes the v1.5 backend arc (alongside GoalBackend spec/41, OutcomeBackend spec/42, JournalBackend spec/43).

---

## Shipping plan (1 PR for scaffolding)

**PR 1 (this PR, SCAFFOLDING-ONLY).** Protocol scaffold + dataclasses + capability advertisement + `FilesystemQueueBackend` reference impl + factory/env/doctor + thin non-deprecated `_cascade.py` shim + full conformance suite + filesystem-specific tests + spec/44 DRAFT.

The free-function cron/project-runner API (`claim_next_queued` etc.) is preserved in `_cascade.py` via wrapper functions that construct `FilesystemQueueBackend(project_root)` internally and delegate. NO internal runtime callers are wired — matching the #425 GoalBackend / #426 OutcomeBackend scaffolding shape.

Follow-up work is tracked in these filed issues:
- **#468** — Queue inspection CLI (`queue inspect` / `queue list-dead-letter` / `queue recover`), after spec/44 locks.
- **#469** — Runtime adoption (cascade-runner wiring through QueueBackend Protocol), after spec/44 locks.
- **#470** — Harmonizing `ATOMIC_AGENTS_MULTI_HOST` with LockBackend's single_host_only pattern.
- **#472** — Extending `doctor.check_queue_backend`'s containment probe to per-subdirectory symlinks.

---

## State vocabulary mapping

`spec/06` defines a conceptual queue model using a THREE-state vocabulary. The `_cascade.py` reference implementation uses a FOUR-state on-disk vocabulary. spec/44 is normative for the on-disk layout.

| spec/06 conceptual | On-disk directory | Notes |
|--------------------|-------------------|-------|
| `pending` | `queue/queued/<role>/` | Pending backlog, bucketed by role |
| `in_progress` | `queue/claimed/<lease_token>/` | In-flight, namespaced by lease |
| `completed` | `queue/done/<lease_token>/` | Completed, namespaced by lease |
| `dead_letter` | `queue/dead-letter/<lease_token>/` | Terminal failure, namespaced by lease |
| _(new in spec/44)_ | `queue/queued/_recovered/<lease_token>/` | Stale claims recovered by `recover_stale_claims()` |

The spec/06 vocabulary is conceptual and pre-dates this spec. spec/44 is normative for on-disk directory names. Operators with existing queue directories (built from the `_cascade.py` implementation) are unaffected — the layout did not change.

Tracked as **#471**: update spec/06's queue section to cross-reference spec/44 for the authoritative on-disk layout after spec/44 locks.

---

## Overview

`QueueBackend` abstracts cascade work-queue claim/release operations for a project. The backend is **project-scoped** — all queue state lives under `<project_root>/queue/`. This is the one project-scoped backend in the v1.5 wave; all other v1.5 backends (Goal, Outcome, Journal) are agent-scoped.

The `FilesystemQueueBackend` reference implementation wraps the same POSIX-rename atomic claim mechanism from `_cascade.py`. It adds:

1. **Protocol surface** — four atomicity primitives + one enumeration READ primitive, enabling shared recovery code above the Protocol.
2. **Symlink containment** — `_queue_root()` raises `PathTraversalError` when `queue/` resolves outside `project_root` (mirrors `FilesystemJournalBackend._journal_dir()` pattern).
3. **spec/40 export** — `export()` returns a `QueueExport` with the durable queue state (queued/ + done/ + dead-letter/) excluding ephemeral in-flight state (claimed/ + .lease.json sidecars).
4. **single_host_only capability honesty** — `capabilities().single_host_only=True` plus `doctor.check_queue_backend` WARN when deployed in a declared multi-host environment.

---

## Module layout

```
atomic_agents/queue/
├── __init__.py     # registry: register_queue_backend /
│                   # get_queue_backend / list_queue_backends /
│                   # unregister_queue_backend +
│                   # get_default_queue_backend factory (scope: project_root) +
│                   # _redact_for_error_message (credential safety)
├── types.py        # canonical types: QueueItem (abstract, no path),
│                   # QueueCapabilities (frozen, single_host_only REQUIRED),
│                   # QueueExport (ExportableResult subclass)
├── backend.py      # QueueBackend Protocol (@runtime_checkable) +
│                   # recover_stale_claims() free function ABOVE the Protocol
└── filesystem.py   # FilesystemQueueBackend reference implementation +
                    # FilesystemQueueItem(QueueItem) with path: Path +
                    # _sidecar_path / _write_sidecar private helpers
```

**Re-export shim** in `atomic_agents/_cascade.py`: thin NON-deprecated wrappers that preserve the VERBATIM free-function signatures (`claim_next_queued(project_root, ...)` etc.) of the spec/06 cron/project-runner API. Sunset at the v1.0/T10 unified shim-retirement pass.

---

## Deliberate design choices

### QueueItem.path is filesystem-only

The abstract `QueueItem` (in `queue/types.py`) has NO `path` field. Future Redis / SQS / DB backends have no filesystem path to populate — forcing them to fabricate a `Path` value would book a future breaking change.

`FilesystemQueueItem(QueueItem)` (in `queue/filesystem.py`) adds `path: Path` for filesystem-specific callers (cron scripts via spec/06, `_cascade.py` shim, `test_cascade.py`). The shim re-exports `FilesystemQueueItem` aliased as `QueueItem` so existing `item.path` access continues to work unchanged.

### No WritePolicy

`QueueBackend` has **no WritePolicy enum**. The queue path is fixed at construction (`project_root/queue/`). There is no write-path decision to defer to policy — unlike MemoryBackend's `write_note()` (which accepts `OVERWRITE` / `MERGE`). The conformance suite MUST NOT include a WritePolicy test for `QueueBackend`.

Per arc-ruling `428-pr1-args.json` writepolicy-presence: Option 1.

### single_host_only is REQUIRED (no default)

`QueueCapabilities.single_host_only` has no default value. This is the `LockCapabilities` pattern — the single-vs-multi-host axis is always relevant. A new backend that omits it gets a `TypeError` at instantiation rather than silently claiming `False` (multi-host-safe when it may not be).

### Atomicity guarantee on Protocol primitives

The PRIMARY guarantee for `claim_next` / `release` / `move_to_dead_letter` is the **state transition of the work file** (the file moves from one well-known directory to another in an all-or-nothing operation). For the filesystem backend this is a POSIX rename. Sidecar writes (`.lease.json`, `.reason.txt`) are BEST-EFFORT and may be absent after a crash. An orphaned sidecar after a crash does NOT constitute a live claim — `recover_stale_claims()` handles this via mtime fallback.

### recover_stale_claims is ABOVE the Protocol

`recover_stale_claims(backend, lease_seconds)` is a free function in `queue/backend.py`. It calls ONLY `backend.list_claimed()` + `backend._recover_stale_claims_native()` (or `backend._reclaim_to_recovered()` for the generic path). This means every registered `QueueBackend` gets the same recovery logic without drift. A Redis backend's `list_claimed()` returns Redis-resident items; the same free function recovers them without any filesystem access.

The filesystem-specific internals (mtime fallback, malformed-sidecar fall-through, `_recovered/` naming) live in `FilesystemQueueBackend._recover_stale_claims_native()` — NOT in the free function.

### Project-scoped vs agent-scoped

The queue is project-scoped (constructor: `FilesystemQueueBackend(project_root)`). All other v1.5 backends are agent-scoped. This matches spec/06: the work queue is a shared project resource consumed by multiple agents/roles. The `get_default_queue_backend(project_root)` factory takes `project_root` not `agent_root`. The `doctor.check_queue_backend` check derives `project_root` from `detect_cascade(agent_root)` and SKIPs for single-agent layouts.

---

## Doctor check

`doctor.check_queue_backend(agent_root)` implements the dual-probe pattern:

1. **Cascade detection** — calls `detect_cascade(agent_root)`. SKIP result if not in a cascade (no project queue to probe).
2. **Construction probe** — calls `get_default_queue_backend(project_root)`. FAIL on bad env var.
3. **Lightweight list** — calls `backend.list_claimed()`. FAIL if raises.
4. **Containment probe** — calls `backend._queue_root()`. FAIL on `PathTraversalError`.
5. **Single-host WARN** — WARNs when `capabilities().single_host_only=True` AND `ATOMIC_AGENTS_MULTI_HOST=true` (or `'1'`).

### Operator override surface

`ATOMIC_AGENTS_QUEUE_BACKEND` — set to a registered `backend_id` to override the default `filesystem` backend. Default: `filesystem`.

`ATOMIC_AGENTS_MULTI_HOST` — set to `true` or `1` to declare a multi-host deployment. When set with a `single_host_only=True` backend, `doctor.check_queue_backend` emits WARN. Defined in spec/44 §Doctor check. Follow-up: harmonize with LockBackend's single_host_only pattern.

### PASS / WARN / FAIL ladder

| Status | Condition |
|--------|-----------|
| SKIP | No cascade detected (single-agent layout) |
| FAIL | Bad env var, construction fails |
| FAIL | `list_claimed()` raises |
| FAIL | `_queue_root()` raises `PathTraversalError` |
| WARN | `single_host_only=True` AND `ATOMIC_AGENTS_MULTI_HOST=true` |
| PASS | All probes passed |

---

## Spec/40 export contract

`QueueExport(ExportableResult)` is registered in the LOCKED spec/40 harness at definition time. `FilesystemQueueBackend` advertises `supports_canonical_export=True`.

**INCLUDE (durable, irreplaceable):**
- `queue/queued/<role>/*` — pending backlog
- `queue/done/<lease_token>/*` — completed items
- `queue/dead-letter/<lease_token>/*` — permanently failed items
- `.reason.txt` sidecars alongside dead-letter items

**EXCLUDE (ephemeral, double-claim hazard):**
- `queue/claimed/<lease_token>/*` — in-flight items
- All `.lease.json` sidecar files

The structural exclusion (whitelist: enumerate only `queued/`, `done/`, `dead-letter/`) mirrors the LOCKED `LockExport.lock_file_names=[]` precedent. The conformance test MUST assert the ephemeral exclusion even when a claim IS held (not skip-and-assume).

**SETTLED (was OPEN in the discovery panel):** `done/` and `dead-letter/` are **EMBEDDED** as raw bytes, NOT treated as reconstructable from the LogBackend audit stream. Rationale: (1) the LogBackend audit stream records per-iteration *telemetry*, not the *work-item file bytes* — a completed/failed item's payload (the markdown work file, its `.reason.txt`) is not recoverable from log lines, so "reconstructable from LogBackend" is false for the bytes a restore needs; (2) embedding all three durable directories keeps the export self-contained (a restore needs only the `QueueExport`, not a join against a separate log corpus) and matches the maintainer's register-now-complete-durable-subset ruling in `428-pr1-args.json` (`spec40-exportable-shape`); (3) the durable/ephemeral boundary stays a single clean rule — *embed everything the backend durably owns, exclude only runtime-bound lease state* — rather than a three-way embed/derive/exclude split. The `queued/` backlog exports regardless. `FilesystemQueueBackend` embeds all three durable directories.

### Per-subdirectory symlink containment (security)

`_queue_root()` proves only that `queue/` itself is contained under `project_root`. The per-operation subdirectories — `queued/`, `claimed/`, `done/`, `dead-letter/`, and their `<role>` / `<lease_token>` namespaces — are themselves attacker-influenceable and MAY be symlinks. A symlinked `claimed/` (or any durable subdir) pointing outside `queue/` would let a `claim_next()`/`release()`/`move_to_dead_letter()` rename land OUTSIDE `project_root`, leaking work-item bytes (the `#426 _runs_root()` ancestor-escalation pattern). Every write AND read site (`claim_next`, `release`, `move_to_dead_letter`, `renew_lease`, `list_claimed`, `recover`, `export`) MUST resolve its target subdirectory and re-assert `is_relative_to(queue_root_resolved)` before use. In `FilesystemQueueBackend` the containment guard raises `PathTraversalError` internally and every operation catches it and fails SOFT: writes (`claim_next`, `release`, `move_to_dead_letter`, `renew_lease`) return `None` / no-op, and reads (`list_claimed`, `recover`, `export`) skip / return `[]`. This preserves the pre-carve `_cascade.py` contract, which had no containment check and so never propagated an exception to a caller. (A backend MAY instead fail-loud on writes; that is an implementation choice, not a Protocol requirement.) The containment check uses the RESOLVED anchor, but the path RETURNED for file operations is the UNRESOLVED `queue_root.joinpath(...)` — mirroring `FilesystemJournalBackend._journal_dir()` — so caller-visible `item.path` stays in the operator's own path representation (byte-identical to the pre-carve `_cascade.py` cron API; a project reached through a symlink does not get `item.path` silently rewritten to the real on-disk location). Because returned paths AND the `export()` relativization base (`self._project_root`) share that same unresolved representation, `relative_to()` never raises for the symlinked-`project_root` case, so the export is never silently emptied. `export()` ADDITIONALLY re-asserts per-leaf containment: each durable work FILE is resolved and checked `is_relative_to(queue_root_resolved)` before its bytes are read, so a symlinked work file pointing outside `queue/` cannot exfiltrate host bytes into the portable export.

---

## Implementer Contract — 12 MUSTs

**Base 8 (from PersonaBackend pattern):**

1. **Side-effect-free construction.** `QueueBackend(project_root)` MUST perform no filesystem I/O. `claim_next()` returns None when the queue is absent; construction never fails on a missing queue directory.

2. **Lease-expiry-recovery correctness.** `recover_stale_claims(backend, lease_seconds)` MUST correctly identify and recover items whose lease has expired (via `lease_expires_at` field from `list_claimed()` or mtime fallback for items without a sidecar). MUST NOT recover items with a valid future lease.

3. **Capability honesty.** `capabilities()` MUST return a `QueueCapabilities` instance whose values match the backend's actual behavior. A backend claiming `supports_canonical_export=True` MUST implement `export()`. A backend claiming `single_host_only=False` MUST support cross-host atomic claim (not just POSIX rename). `capabilities()` MUST return the same values on every call.

4. **Atomic claim — no double-claim under race.** `claim_next(role, lease_token, lease_seconds)` MUST be atomic: under concurrent callers, only ONE caller claims any given work item. The POSIX-rename guarantee (filesystem) or backend-equivalent guarantee (Redis `SETNX`, SQS `ReceiveMessage`) is the atomicity primitive. _(Conformance: this requirement shares its race assertion with MUST 9 — see TEST 30/31 in `test_queue_backend_conformance.py`. MUST 4 states the contract; MUST 9 fixes the rename-based exclusion primitive and its test.)_

5. **Storage isolation.** Two `QueueBackend` instances scoped to different `project_root` values MUST NOT see each other's items. Operations on one backend MUST NOT affect the other.

6. **URL redaction in error messages.** When `ATOMIC_AGENTS_QUEUE_BACKEND` carries a URL- or DSN-shaped value, the backend factory MUST redact it before echoing in error messages (prevents credential leakage).

7. **Snapshot consistency bound.** `export()` is a best-effort point-in-time snapshot. The export MUST correctly exclude `claimed/` and `.lease.json` even when a claim is held concurrently. Strict consistency requires the caller to hold a `LockBackend` across the export.

8. **backend_id stability.** `backend.backend_id` MUST return the same string across all calls. The string MUST match the registry key under which the backend was registered.

**Queue-unique axes (4 additional MUSTs):**

9. **Atomic claim via rename / no-double-claim under race.** A concurrent two-claimer race where both workers attempt to claim the same work item MUST result in exactly one successful claim (one worker gets the item, the other gets None or claims a different item). The filesystem backend achieves this via `src.rename(dst)` — the POSIX rename is the exclusion primitive. Non-filesystem backends MUST provide an equivalent exclusive-take primitive (Redis `BRPOPLPUSH`, SQS visibility timeout, DB row-level lock).

10. **Dead-letter terminal transition / dead-work-stays-dead.** `move_to_dead_letter()` MUST atomically transition the work item to a terminal state (`dead-letter/`). Once a file is in `dead-letter/`, no `claim_next()`, `recover_stale_claims()`, or `release()` operation MUST affect it. The state transition MUST be the file-move (POSIX rename or equivalent); `.reason.txt` and sidecar cleanup are best-effort. An orphaned `.lease.json` in `dead-letter/` MUST NOT constitute a live claim.

11. **list_claimed enumeration and Protocol-based recovery.** `list_claimed(role=None)` MUST return all currently-held (claimed) items. The returned items MUST include `lease_expires_at` (populated from the sidecar when present, None when absent). `recover_stale_claims(backend, ...)` built from `list_claimed()` + `_recover_stale_claims_native()` / `_reclaim_to_recovered()` MUST correctly recover stale items without direct filesystem access in the shared free function.

12. **single_host_only capability honesty (MUST define conformance).** A `QueueBackend` claiming `single_host_only=True` MUST advertise this consistently across ALL calls to `capabilities()`. The conformance suite MUST verify that a backend claiming `True` (a) has `capabilities().single_host_only == True` on every call, and (b) does NOT self-contradict by claiming `False` on a second call. `QueueCapabilities.single_host_only` is a REQUIRED field (no default) to prevent a new backend from silently advertising `False` (multi-host-safe) when it is not. The `doctor.check_queue_backend` check MUST issue a WARN (not FAIL) when `capabilities().single_host_only=True` is detected in a deployment that declares multi-host operation via `ATOMIC_AGENTS_MULTI_HOST=true`. A conformance test MUST assert the WARN fires under those conditions.

# spec/45 — IdempotencyBackend Protocol

**Status:** DRAFT  
**Issue:** #520 PR1  
**Depends on:** spec/40 (canonical export), spec/44 (queue backend pattern)

---

## Overview

The IdempotencyBackend Protocol provides an at-most-once execution guarantee
for agents that may receive the same trigger more than once (serve, queue,
cron). A caller-supplied idempotency key gates execution: the first `begin()`
for a key succeeds (FRESH); subsequent calls return IN_FLIGHT or COMPLETED
without triggering re-execution.

This is the eighteenth backend Protocol in the atomic-agents framework (v1.5
wave). PR1 (this spec) covers scaffolding only: the Protocol + types +
`FilesystemDedupLedger` + conformance tests + doctor check + canonical export.
Agent wiring (`idempotency_key` on `agent.call()`, trigger integration) is PR2.

---

## Scope

**Agent-scoped** (NOT project-scoped). `FilesystemDedupLedger(agent_root)`
operates on `<agent_root>/idempotency/`. This matches GoalBackend and
JournalBackend, not the project-scoped QueueBackend.

Cross-agent dedup (preventing duplicate runs across agents in a cascade) requires
a shared backend — a Redis, Postgres, or project-root-scoped
`FilesystemDedupLedger` instantiated at the project root. This is a follow-up
issue; PR1 establishes the Protocol that such a backend will implement.

---

## State vocabulary

| State      | Meaning                                                      |
|------------|--------------------------------------------------------------|
| `fresh`    | Key has never been seen. Caller may proceed and own the run. |
| `in_flight`| A prior `begin()` claimed this key; `commit()` not yet called. |
| `completed`| `commit()` was called. Prior result reference is available.   |

---

## Protocol surface

### `begin(key: str, run_id: str) -> DedupDecision`

**Single atomic check-reserve-or-report** (MUST 4). Checks whether `key` was
seen before, and if not, reserves it. Returns a `DedupDecision` value object.

**MUST NOT raise for duplicate detection.** FRESH, IN_FLIGHT, and COMPLETED
are all expressed as `DedupDecision` fields, not exceptions. Only
unrecoverable I/O errors (disk failure, symlink escape) raise
`IdempotencyBackendError`.

**Invalid-key exception (all three of `begin()`, `commit()`, `lookup()`):** an
invalid key (path separators, empty, `'.'`/`'..'`, or NUL/control characters)
raises `PathTraversalError` from key validation BEFORE any I/O — a distinct
exception from the `IdempotencyBackendError` I/O-failure path. This is a caller
bug surfaced loudly, not a dedup state. See §"Key validation" for the full rule.

State transitions:

- Key absent → reserve key, return `DedupDecision(is_duplicate=False, state='fresh', ...)`
- Key in-flight → return `DedupDecision(is_duplicate=True, state='in_flight', prior_run_id=..., prior_result_ref=None)`
- Key completed → return `DedupDecision(is_duplicate=True, state='completed', prior_run_id=..., prior_result_ref=<result_ref>)`

Atomicity guarantee: `FilesystemDedupLedger` uses `O_EXCL` (`os.open` with
`O_WRONLY|O_CREAT|O_EXCL`) on the lease file. Under concurrent callers,
exactly one `open()` succeeds for any given key; the loser gets
`FileExistsError` (maps to EEXIST) and returns IN_FLIGHT. No TOCTOU window
between check and reserve.

### `commit(key: str, result_ref: str) -> None`

**Mark a previously-claimed key as permanently COMPLETED** (MUST 5).

Writes a MARKER-ONLY terminal entry: `key + prior_run_id + result_ref +
terminal: true`. Does NOT store result content. `result_ref` is an opaque
reference string (run_id, path, URI) — the caller owns the actual result bytes.

MUST use `atomic_write()` (temp + fsync + rename) so the terminal marker is
crash-safe. A corrupt terminal marker that passes the file-exists check in
`begin()` but fails JSON parsing is treated as COMPLETED (fail-closed — a
garbled terminal entry is safer to treat as "do not re-run" than as "run
again").

After `commit()`, `begin(key)` returns `state='completed'`.

`commit()` MUST be idempotent — if a terminal entry already exists for the key,
`commit()` is a no-op that preserves the original terminal (including
`prior_run_id`), so a redelivered/retried `commit()` cannot sever the audit link
to the originating run. (The first `commit()` unlinks the in-flight lease, so a
naive second `commit()` would resolve `prior_run_id=None` and overwrite the
marker — first-commit-wins prevents that.)

### `lookup(key: str) -> DedupDecision`

**Read-only state query** (MUST 6). Returns the current `DedupDecision`
WITHOUT reserving the key. Equivalent to `begin()` with no side effects.

Returns `DedupDecision(state='fresh')` when the key is absent. This is the
authoritative FRESH signal — no fallback scan.

### `capabilities() -> IdempotencyCapabilities`

Returns the backend's capability advertisement. See `IdempotencyCapabilities`.

### `export(query=None) -> IdempotencyExport`

Returns a canonical export per spec/40. See §"spec/40 export contract" below.

### `export_all() -> IdempotencyExport`

Convenience wrapper — equivalent to `export(None)`.

---

## `DedupDecision` value object (MUST 1)

```python
@dataclass(frozen=True)
class DedupDecision:
    is_duplicate: bool
    state: Literal['fresh', 'in_flight', 'completed']  # REQUIRED — no default
    prior_run_id: str | None
    prior_result_ref: str | None
```

`state` is REQUIRED with no default. An absent `state` would force callers to
re-query, creating split-brain with the "empty result is authoritative" rule
(Project Lesson 9). Callers MUST inspect `state`, not only `is_duplicate`, to
distinguish wait-and-retry (IN_FLIGHT) from use-prior-result (COMPLETED).

---

## `IdempotencyCapabilities` (MUST 3)

```python
@dataclass(frozen=True)
class IdempotencyCapabilities:
    backend_id: str          # REQUIRED — no default
    single_host_only: bool   # REQUIRED — no default (LockCapabilities pattern)
    atomic_claim: bool       # REQUIRED — no default (always-relevant dedup axis)
    supports_ttl: bool = False
    supports_canonical_export: bool = False
```

`single_host_only` and `atomic_claim` are REQUIRED (no default) so a new
backend that omits either gets a `TypeError` at instantiation rather than
silently claiming False when it is not.

`FilesystemDedupLedger` advertises:
- `single_host_only=True` — O_EXCL atomicity does not extend across hosts.
- `atomic_claim=True` — `begin()` uses O_EXCL (single atomic check-reserve).
- `supports_ttl=False` — TTL sweep is a follow-up PR.
- `supports_canonical_export=True` — `export()` is implemented.

**WritePolicy is NOT part of the IdempotencyBackend Protocol.** The ledger
path is fixed at construction. Mirrors QueueBackend and GoalBackend, not
MemoryBackend. The conformance suite MUST NOT include a WritePolicy test.

---

## `FilesystemDedupLedger` reference implementation

### Directory layout

```
<agent_root>/idempotency/
    <key_hash>.lease.json      — in-flight lease marker (ephemeral)
    <key_hash>.terminal.json   — completed terminal marker (permanent, marker-only)
```

Where `<key_hash> = sha256(key.encode()).hexdigest()` (64 hex chars).

The original key is stored inside the JSON entry for round-trip verification
(hash collision guard: if the stored key doesn't match the caller's key on
lookup, the entry is skipped and FRESH is returned).

### On-disk JSON shapes

**Lease file (`*.lease.json`):**
```json
{"key": "<original_key>", "run_id": "<caller_run_id>", "state": "in_flight"}
```

**Terminal file (`*.terminal.json`):**
```json
{"key": "<original_key>", "prior_run_id": "<original_run_id>",
 "result_ref": "<opaque_reference>", "terminal": true}
```

MARKER-ONLY: the terminal file stores only the reference, never result bytes.
The file size MUST remain bounded (< 4 KB for normal usage).

### Atomicity contract for `begin()` (O_EXCL)

```
1. Validate key (reject traversal attempts early).
2. Get ledger_root (containment check for idempotency/ dir).
3. Check terminal marker (COMPLETED fast path — no O_EXCL needed).
4. Attempt O_EXCL create of lease file → FRESH on success.
5. On FileExistsError → read existing lease/terminal → IN_FLIGHT or COMPLETED.
```

The O_EXCL create is the single atomic POSIX syscall that fails with
`FileExistsError` if the file already exists. Under concurrent callers, exactly
one `open()` succeeds; the loser returns IN_FLIGHT.

### Fail-closed vs fail-open boundary

The fail-direction differs by file TYPE: an unreadable/tampered LEASE is treated
as IN_FLIGHT, an unreadable/tampered TERMINAL as COMPLETED. Both directions mean
"do not re-run" — re-running a key whose marker was corrupted or tampered is the
unsafe outcome.

| Condition | `begin()` behavior |
|-----------|-------------------|
| ledger dir absent (ENOENT) | FRESH (authoritative) |
| key file absent (ENOENT) | FRESH (authoritative) |
| lease file present, readable | parse → IN_FLIGHT |
| terminal file present, readable | parse → COMPLETED |
| lease file present, unreadable (non-ENOENT OSError, corrupt JSON, containment violation) | IN_FLIGHT (fail-closed, log.error) |
| terminal file present, unreadable (non-ENOENT OSError, corrupt JSON, containment violation) | COMPLETED (fail-closed, log.error) |
| I/O error on ledger dir creation (non-ENOENT OSError) | raise `IdempotencyBackendError` |
| symlink escape on `_ledger_root()` (whole-ledger directory escape) | raise `IdempotencyBackendError` |

`lookup()` uses the same table with ONE difference: a whole-ledger DIRECTORY
escape (`_ledger_root()` raises) is fail-soft on the read side — it returns FRESH
rather than raising, because an unreadable ledger cannot distinguish "key absent"
from "key present" and treating it as empty matches "empty is authoritative"
(Lesson 9). A tampered/unreadable per-key LEAF, however, still resolves to the
fail-closed duplicate state above (COMPLETED for a terminal leaf, IN_FLIGHT for a
lease leaf) on both `begin()` and `lookup()` — "do not re-run" is the safe
direction for a specific key whose marker was tampered with.

### Symlink containment (security contract)

ONE consolidated per-entry guard prevents whack-a-mole (#428 round 6 reframe).
There is exactly one per-entry containment helper; do not add a second.

**`_ledger_root()`** (perimeter check): Resolves both `agent_root` and
`agent_root/'idempotency'` and checks `is_relative_to` before returning the
unresolved path. A symlinked `idempotency/` directory pointing outside
`agent_root` raises `PathTraversalError`.

**`_require_canonical_ledger_path(ledger_file_path, ledger_root)`** (the single
per-entry invariant): called at EVERY ledger read/write/CLAIM sink — including
`begin()`'s O_EXCL lease create:
1. Resolves BOTH paths.
2. Asserts `resolved_child.is_relative_to(resolved_root)`.
3. Asserts `not ledger_file_path.is_symlink()` (leaf symlink rejection).
4. Raises `PathTraversalError` on any violation.

`begin()`'s claim sink ALSO opens with `O_NOFOLLOW` as defense-in-depth, so a
symlink leaf forged in the window between the guard and the `os.open()` cannot be
followed. The pre-create `_require_canonical_ledger_path` guard is the primary
symlink defense; `O_NOFOLLOW` is belt-and-suspenders. (Mechanism note: with
`O_EXCL` set, an existing symlink leaf — dangling or not — makes the open raise
`EEXIST`/`FileExistsError`, NOT `ELOOP`, because POSIX checks `O_EXCL` existence
before dereferencing. The `FileExistsError` path then re-reads the leaf, where
the read-side containment guard catches the symlink and fails-closed.)

Write/claim operations (begin/commit) RAISE `IdempotencyBackendError` on a
containment violation — the caller must know the claim failed, not silently
succeed. Read operations differ by scope: a whole-ledger DIRECTORY escape is
fail-soft (lookup returns FRESH, export returns empty / skips), but a tampered
per-key LEAF resolves to the fail-closed duplicate state (see the boundary
table above) — never re-run a key whose marker was tampered with.

### Key validation

`_validate_key(key)` rejects: empty string, `'.'` or `'..'`, any value where
`key != Path(key).name` (contains separators), and any value containing a NUL
byte or C0 control character (`chr(0)`–`chr(31)`). Raises `PathTraversalError`.
Called at the top of `begin()`, `commit()`, and `lookup()` BEFORE any path
composition (and before any I/O). The key never becomes a direct path component
(the on-disk filename derives from `sha256(key)`), so this rejection is
defense-in-depth: a NUL byte can truncate a path at the syscall boundary and
control characters are never legitimate in an idempotency key.

`_validate_key` also rejects keys exceeding 2048 characters (counted via
`len()`, not UTF-8 bytes — a multibyte string at the limit may exceed 2048
bytes on disk; the bound exists to catch caller bugs, not to enforce a
disk-byte budget). Raises `IdempotencyBackendError`. This mirrors the
`result_ref` length bound below — the on-disk filename derives from
`sha256(key)` (always 64 hex chars), so the bound never affects path safety;
it is a caller-bug guard only.

`_validate_result_ref(result_ref)` rejects only values exceeding 1024 characters
(counted via `len()`, not UTF-8 bytes — a multibyte string at the limit may
exceed 1024 bytes on disk; the bound exists to catch caller bugs, not to enforce
a disk-byte budget). Raises `IdempotencyBackendError`. Path separators are NOT
rejected: `result_ref` is stored as a JSON value (the on-disk filename derives
from `sha256(key)`, never from `result_ref`), so a URI (`s3://bucket/key`) or
path (`runs/2026/abc.json`) is a valid result reference — the documented intent.

### TTL sweep

`supports_ttl=False` in PR1. A follow-up `sweep()` method will expire stale
lease files (begin() with no commit(), process crashed). Until then, stale
leases require operator intervention (delete the `*.lease.json` file).

FilesystemDedupLedger is single-host-only: stale-lease recovery is not needed
for correctness in single-host deployments because the same process can
correlate lease files with active threads. Multi-host deployments that need
TTL should use a Redis or Postgres IdempotencyBackend.

---

## Implementer Contract

Backends implementing `IdempotencyBackend` MUST satisfy these MUSTs:

**MUST 1** — `DedupDecision` is a value object. `begin()` MUST return
`DedupDecision` in ALL non-error code paths. `begin()` MUST NOT raise for
FRESH, IN_FLIGHT, or COMPLETED detection. `DedupDecision.state` is REQUIRED
with no default.

**MUST 2** — Construction is side-effect-free. No filesystem I/O during
`__init__`. No directories created at construction time.

**MUST 3** — `capabilities()` returns an `IdempotencyCapabilities` dataclass
with `backend_id`, `single_host_only` (REQUIRED, no default), and
`atomic_claim` (REQUIRED, no default) honestly advertised. `supports_ttl` and
`supports_canonical_export` MUST reflect actual implementation state.
WritePolicy is NOT part of the Protocol.

**MUST 4** — `begin()` is atomic check-reserve-or-report. Under concurrent
callers, exactly one MUST receive FRESH; all others MUST receive IN_FLIGHT or
COMPLETED. No TOCTOU window between check and reserve. Key validation (separator
rejection plus the bounded key-length guard, see "Key validation" above) MUST
occur before any path composition. After winning the atomic
claim, the backend MUST re-check the terminal marker before returning FRESH: a
`commit()` that interleaved between the initial terminal check and the claim
writes the terminal AND removes the in-flight marker (which is why the claim
succeeded), and returning FRESH there would re-run an already-COMPLETED key.

**MUST 5** — `commit()` writes a MARKER-ONLY terminal entry. The terminal entry
MUST contain: key, prior_run_id, result_ref, terminal=true. MUST NOT store
result content bytes. MUST use crash-safe write (atomic_write or equivalent).
`result_ref` MUST be validated for bounded length. `result_ref` is an opaque
reference (run_id, path, or URI) stored as a JSON value, NEVER as a path
component, so path separators (`/`, `\`) are PERMITTED — backends MUST NOT
reject a URI or path result_ref.

**MUST 6** — `lookup()` is read-only (no side effects). Returns FRESH for
unknown keys. Empty/absent ledger is authoritative FRESH — NOT a fail-closed
condition.

**MUST 7** — Single-host-only backends MUST advertise
`capabilities().single_host_only=True` honestly. The doctor check MUST issue
a WARN (not FAIL) when `single_host_only=True` is detected with
`ATOMIC_AGENTS_MULTI_HOST=true`.

**MUST 8** — `backend_id` is stable across calls (no randomness, no
session-scoping).

**MUST 9** — Storage isolation: two backends with different `agent_root` values
MUST NOT share state.

**MUST 10** — Canonical path containment: every ledger read/write/CLAIM sink —
including the atomic claim create — MUST verify the entry path resolves strictly
under `agent_root` AND is not a symlink leaf, via ONE consolidated guard (not
per-sink whack-a-mole). The filesystem claim sink MUST NOT rely on the atomic
create syscall's incidental symlink behavior alone; it MUST call the consolidated
guard before the create AND open with a no-follow flag (`O_NOFOLLOW`) so a leaf
forged in the guard→create window cannot be followed. (The consolidated guard is
the primary defense; `O_NOFOLLOW` is defense-in-depth. Note that under `O_EXCL`,
a symlink leaf raises `EEXIST`/`FileExistsError`, not `ELOOP` — the existence
check precedes symlink dereference — so the no-follow flag's incidental error is
not the load-bearing protection.)

**MUST 11** — `export()` MUST emit TERMINAL entries only. MUST NOT include
in-flight lease files (phantom-block hazard). The structural whitelist
(enumerate only `*.terminal.json`) is preferred over filter-based exclusion.

**MUST 12** — `export()` per-leaf containment: each terminal file MUST be
verified as (a) a regular file, (b) resolved under `agent_root`, (c) not a
symlink, before its bytes are read into the export.

---

## spec/40 export contract

`FilesystemDedupLedger.export()` participates in spec/40 canonical export with
`supports_canonical_export=True`.

**INCLUDES (durable, irreplaceable):**
- `*.terminal.json` — completed dedup records (small, marker-only)

**EXCLUDES (ephemeral, phantom-block hazard):**
- `*.lease.json` — in-flight lease files
- Any other files in the ledger directory

The structural whitelist (enumerate only `*.terminal.json`) guarantees the
invariant without filter-based logic.

`IdempotencyExport` fields:
```python
@dataclass
class IdempotencyExport(ExportableResult):
    entries_with_bytes: list[tuple[str, bytes]]  # (relative_path, raw_bytes)
    backend_id: str
    scope: str  # agent_root path as a string
```

`IdempotencyExport` is re-exported from `atomic_agents.export` for public
surface consistency with all other v1.5 wave export types.

The backend joins the shared spec/40 round-trip harness in
`tests/test_export_protocol_conformance.py` (alongside Goal/Outcome/Journal/Queue):
a byte-exact `assert_canonical_roundtrip` (exported terminal bytes ==
on-disk `*.terminal.json` bytes), a relative-path-format assertion
(`idempotency/<key_hash>.terminal.json`, non-absolute), in-flight-lease
exclusion, type narrowing, `backend_id`/`scope`, `export_all() == export(None)`,
and the top-level-import resolution check.

---

## Doctor check

`check_idempotency_backend(agent_root)` implements the dual-probe pattern
(MEMORY.md `feedback_doctor_dual_probe_pattern`). All probes run against the
operator's REAL configured backend (a `uuid`-keyed `temp_key` that cannot collide
with a real idempotency key), so a read-only or mis-permissioned ledger FAILs
here rather than false-PASSing:

1. `lookup(temp_key)` — lightweight read (must return FRESH for new key)
2. `begin(temp_key)` — write/claim path against the real ledger (must return FRESH)
3. `commit(temp_key, result_ref='__doctor_probe__')` — terminal write to the real ledger
4. `lookup(temp_key)` post-commit — must return COMPLETED
5. Cleanup — unlink the probe's lease + terminal markers from the real ledger
   (`_doctor_cleanup_idempotency_probe`, best-effort; runs on every exit path)

**FAIL** when any probe raises or returns unexpected state.  
**WARN** when `capabilities().single_host_only=True` and
`ATOMIC_AGENTS_MULTI_HOST=true`.  
**PASS** otherwise.

Registered in `run_doctor()` immediately after `check_queue_backend()`.

---

## Environment variable

`ATOMIC_AGENTS_IDEMPOTENCY_BACKEND` — a registered backend_id. Defaults to
`'filesystem'` (the only backend registered in PR1). Connection-string forms are
NOT consumed in PR1; a future Redis/Postgres backend defines its own connection
handling. Single variable (matches Journal/Queue/Goal/Outcome precedent, not the
dual-var SecretBackend pattern).

`_redact_for_error_message()` strips credentials before any error output
(URL → `scheme://...`, DSN → `[redacted-connection-string]`).

---

## Conformance suite

`tests/test_idempotency_backend_conformance.py` — 58 tests covering:
- Protocol-behavior tests (parametrized over BACKEND_FACTORIES)
- DedupDecision/IdempotencyCapabilities dataclass tests
- Registry dispatch tests
- Doctor check tests
- Export importability test
- WritePolicy absence test
- Race condition (O_EXCL barrier) test
- Error-path branch assertions (caplog for typed-handler confirmation)
- result_ref is opaque: URI/path/backslash result_ref round-trips (not rejected)

`tests/test_idempotency_filesystem.py` — 41 filesystem-specific tests covering:
- Symlink containment (ledger root perimeter, leaf symlink, claim sink)
- O_EXCL atomicity + post-claim terminal re-check (at-most-once TOCTOU close)
- atomic_write crash-safety
- Key/result_ref validation
- On-disk JSON field verification
- Hash collision guard
- Fail-closed / fail-open boundary (directory perimeter vs per-key leaf)
- Dangling-symlink leaf fail-closed (is_symlink-before-exists masking fix)
- begin() bounded-retry recovery (_begin_after_vanished both branches)
- Per-guard negative controls (each strip verified RED)

---

## PR2 scope (NOT in this spec)

The following are explicitly deferred to PR2:
- `idempotency_key` parameter on `agent.call()`
- Trigger wiring (serve/queue/cron)
- `RunRecord`/JSONL audit-shape changes
- spec/22 addendum
- spec LOCK ceremony
- TTL sweep implementation

---

## Cross-references

- spec/40 §"Per-backend export contracts" — canonical export pattern
- spec/44 §"QueueBackend" — scaffolding-only carve template
- spec/41 §"GoalBackend" — agent-scoped backend pattern
- TENSIONS.md T4 — queue-is-filesystem-only (closed by #428; same pattern here)

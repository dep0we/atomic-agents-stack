# spec/47 — ConversationBackend Protocol

**Status:** LOCKED — implementation complete. LOCK PR (#535 PR2) added model-aware
token-budget derivation (per-model context window via `LLMCapabilities.max_input_tokens`),
`check_conversation_backend` doctor check (wired into `run_doctor()`), and the #553
write-back reorder fix (deferred runs no longer persist orphaned turn pairs).
The per-principal serve wiring — deriving a verified `Principal` at the serve perimeter and
threading it through `agent.call()` to gate conversation access — SHIPPED with PrincipalBackend
(#556, spec/48): the serve layer's fail-closed opt-in HYBRID flow now produces the `Principal`
that `load_turns()` / `write_turn()` here consume.

**Issue:** [#535](https://github.com/dep0we/atomic-agents-stack/issues/535)
**Protocol number:** 20 (the twentieth backend Protocol, v1.5 wave)

---

## Overview

ConversationBackend is the persistence contract for multi-turn conversation history. It
lets `agent.call()` inject prior turns into the LLM messages array so the model sees
prior context without polluting the cacheable system-prompt prefix (see TENSIONS T16,
approved and committed on `docs/tensions-t16-conversation-flex`).

The default (`None`) is today's exact single-shot behavior — no backend configured, no
`conversations/` directory created on agent construction (rule #14, backward compatibility).

---

## Ownership model

Conversations are owned by the **(principal, agent)** pair.

- A `conversation_id` is scoped to `(principal.identifier, agent_root)`.
- Cross-principal reads MUST fail-closed (`ConversationAccessDenied`).
- Cross-agent reads are NOT implicit — agent A cannot read principal P's conversation
  with agent B. That capability is an explicit audited extension deferred to a future PR.

Filesystem layout:

```
<agent_root>/
  conversations/
    <principal_id>/              — one subdir per principal identifier
      .conv.lock                 — per-principal exclusive write lock
      <conversation_id>/         — one subdir per conversation
        <iso_ts>_<run_id>_<NN>_<role>.json  — one file per turn (NN = per-call seq)
```

---

## Principal primitive

`Principal` is a thin typed authorization key:

```python
@dataclass(frozen=True)
class Principal:
    identifier: str          # bare filename component; stable opaque id
    derivation_source: str   # 'local' | 'jwt' | 'oidc' | 'mtls' (advisory)
    is_verified: bool = False
```

`LOCAL_PRINCIPAL = Principal(identifier='local', derivation_source='local', is_verified=True)`
is the home-user default — trusted by construction, zero config.

**MUST NOT be confused with `caller_identity`** (spec/37 MUST 6). `caller_identity` is
an explicitly-unverified HTTP header value passed through to the audit trail. `Principal`
is the authorization key for conversation ownership. The serve layer MAY derive a
`Principal` from a verified token (JWT/OIDC/mTLS); it MUST NOT pass `caller_identity`
raw as `Principal.identifier` without a verification step.

---

## Turn schema (minimal)

```python
@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str               # UTF-8 turn text
    ts: str                    # ISO-8601 UTC, MUST be datetime.now(timezone.utc).isoformat()
    run_id: str                # links to RunRecord JSONL audit trail
    seq: int = 0               # per-call sequence: user=0, assistant=1
    schema_version: int = 1    # TURN_SCHEMA_VERSION
```

Rich fields (`tool_calls`, `sources`, `cost_usd`) stay in the `RunRecord` JSONL keyed by
`run_id`. No parallel audit path (CLAUDE.md Principle #5).

A single `call()` writes BOTH a user turn and an assistant turn that share ONE `run_id`
AND ONE `ts` — so `run_id` alone is NOT unique within a call. The `seq` field
disambiguates them (user=0, assistant=1); a backend MUST incorporate it (and not the
`ts`+`run_id` pair alone) when deriving the on-disk turn identity, or the assistant turn
overwrites the user turn and every user turn is silently lost.

`ts` MUST be produced by `datetime.now(timezone.utc).isoformat()` (+00:00 suffix) to
guarantee lexicographic sort order on filenames. The filesystem backend normalizes
non-UTC timestamps with a WARNING, but callers MUST supply UTC.

---

## Three-channel seam (backend selection)

Priority: constructor kwarg wins → `ATOMIC_AGENTS_CONVERSATION_BACKEND` env →
model.md `## Conversation Backend` section (LOCKED field). All channels resolve to
`None` when unset — `None == single-shot` is MANDATORY (rule #14).

The result is cached on `self._conversation_backend_resolved` for the agent's lifetime.
If `model.md` is reloaded via `agent.load()`, the cached backend is NOT updated.
Operators who need to swap backends between calls must reconstruct the agent (same
behavior as `JournalBackend` and `IdempotencyBackend`).

---

## Token-budget window (MUST 8)

`load_turns()` loads the most-recent turns that fit within `budget_tokens` (character
count / 4 approximation). Oldest-first eviction when the window overflows.

`agent.call()` derives `budget_tokens` per-call as:

```
max(1000, model_context_limit - system_prompt_tokens_est - max_output_tokens - safety_margin)
```

where `model_context_limit` is `LLMCapabilities.max_input_tokens` for the resolved
model, `system_prompt_tokens_est = len(system_prompt) // 4` (same character-to-token
approximation as `load_turns()`), and `safety_margin = 2000` tokens. Fails soft to
`8000` on any error (e.g. `UnknownModelError` for a custom model id) so a
misconfigured model never crashes `call()`.

**One-turn floor:** when the single newest turn alone exceeds `budget_tokens`, `load_turns()`
returns `[]`. Callers MUST supply a budget large enough for at least one turn, or accept
single-shot behavior at very small budgets.

---

## Assembly slot and mechanism (MUST 6)

Prior turns are injected as **real role-tagged entries in the `messages[]` array** BEFORE
the current `work_item` turn. They are NOT flattened into `assemble_system_prompt()`.

This preserves the T14 cacheable prefix (CLAUDE.md Principle #6 bounded flex, see
TENSIONS T16). The `#6/#7/#8 assembly-order` framing in agent.py is documentation, not
the mechanism — the mechanism is list prepend.

---

## Turn write-back failure posture (MUST 7)

Write the turn pair AFTER `_log(log_record)` (JSONL-first principle, mirrors
`idempotency.commit()` ordering). On failure from either `write_turn()` call:

- Return the billed `Response` unchanged (the LLM work succeeded).
- Set `response.continuity_persisted = False`.
- Log a WARNING carrying `run_id`.

**PR1 crash boundary:** a crash between the user-turn write and the assistant-turn write
leaves an orphaned user turn. The orphan is identified by `run_id` in the filename.
Manual recovery: delete the dangling file. A two-turn atomic path ships in a future PR.

---

## Implementer Contract — 10 MUSTs

### MUST 1 — Side-effect-free construction

`__init__(agent_root)` MUST perform no filesystem I/O. The `conversations/` directory is
created lazily on the first `write_turn()` call. This matches the `FilesystemDedupLedger`
and `FilesystemJournalBackend` conventions and allows construct-then-reprovision for tests
and migrations.

**Conformance test:** construct `FilesystemConversationBackend(tmp_path)` and assert that
`(tmp_path / 'conversations')` does not exist.

### MUST 2 — Principal-scoped fail-closed isolation

`load_turns()` and `write_turn()` MUST refuse to return or write turns when the requesting
principal does not own the target directory. Cross-principal reads MUST raise
`ConversationAccessDenied`.

**Two guards defending DIFFERENT classes** (both in `load_turns()` and `write_turn()`).
They are NOT symmetric — Guard (1) defends perimeter escape, Guard (2) is the
load-bearing cross-principal identity guard:
1. `safe_resolve_under(principal_dir, conv_root)` — **perimeter** guard. Defends
   path-escape OUTSIDE `conversations/`. A sibling symlink
   `conv_root/bob -> conv_root/alice` PASSES this guard (alice IS under
   `conversations/`), so Guard (1) does not defend principal identity.
2. `_verify_principal_directory()` — **identity** guard, the load-bearing guard for
   cross-principal isolation. It is now a **two-part** check, because resolved-basename
   comparison alone is insufficient on case-insensitive and unicode-normalizing
   filesystems:
   - **(2a) resolved-basename comparison** — defends **symlink redirection**: the
     symlink `conv_root/bob -> conv_root/alice` produces `resolved.name == 'alice'`,
     which differs from `'bob'`, raising `ConversationAccessDenied`.
   - **(2b) inode-identity on-disk name check** — `_ondisk_principal_name(conv_root,
     identifier)` finds the REAL on-disk directory entry by **inode identity**
     (`st_ino`, `st_dev`) and raises `ConversationAccessDenied` when the on-disk name
     differs **in bytes** from the requested identifier. This defends the
     **case-insensitive (macOS APFS) / NFC-NFD (unicode-normalizing) aliasing class**:
     `Path.resolve()` returns the caller's spelling, not the on-disk name, so without
     (2b) principal `alice` could read+write principal `Alice` (case-fold) or an
     NFC/NFD-equivalent spelling of the same name — a verified cross-principal leak.

**What is NOT defended:** hardlinks remain an inherent filesystem limitation — two
directory entries with the same inode are indistinguishable by `st_ino`/`st_dev`, so a
hardlinked principal directory is not caught by (2b). This is a documented not-defended
class (per `feedback_containment_reframe_not_whackamole`: name the trust boundary and
what falls outside it rather than chasing every variant). Adversarial multi-principal
deployments should route to a real-authz backend, not the filesystem reference impl.

Stripping EITHER part of Guard (2) MUST cause its negative-control conformance test
to fail (RED): a strip of (2a) leaks the symlink-redirection attack, a strip of (2b)
leaks the case-fold / NFC-NFD aliasing attack. Guard (1) defends a
DIFFERENT class (perimeter escape outside `conversations/`); on the READ path it is
**subsumed by Layer 2** (`_require_canonical_turn_path`, the per-entry canonical-path
check that runs at every read sink), so a perimeter-escape test that asserts only the
final `[]` cannot pin Guard (1) by itself — Layer 2 produces the same `[]`. The
conformance suite therefore isolates Guard (1) with a dedicated control that
**neutralizes Layer 2** (patches `_require_canonical_turn_path` to a no-op) and then
asserts the asymmetry: WITH Guard (1) the escape is refused (`[]`), and STRIPPING
Guard (1) leaks the external turn (RED if Guard (1) stops being load-bearing). (Naming
each guard's actual defense — and not claiming a strip-RED that a sibling guard
covers — follows `feedback_containment_reframe_not_whackamole` and
`feedback_false_green_test_needs_per_invocation_negative_control`.)

**Conformance tests:** (symlink redirection — Guard 2a) write turns as principal A;
symlink `conversations/bob -> conversations/alice`; call `load_turns(bob_principal,
conv_id)`; assert `ConversationAccessDenied` raised (shipped code blocks the attack); a
separate documented-vulnerability test strips the resolved-basename check and asserts
the attack WOULD succeed (proving Guard 2a is load-bearing). (case-insensitive / NFC-NFD
aliasing — Guard 2b) on a case-folding or normalizing filesystem, write turns as
`Alice`, then call as `alice` (or an NFC/NFD-equivalent spelling); assert
`ConversationAccessDenied` raised because the on-disk name differs in bytes; a separate
strip of `_ondisk_principal_name` asserts the cross-principal read WOULD succeed
(proving Guard 2b is load-bearing). Each shipped-code assertion goes RED if its part of
Guard (2) is removed.

### MUST 3 — Path traversal guard on all inputs

`principal.identifier` and `conversation_id` MUST be validated as bare filename
components (no `/`, `\`, `.`, `..`, NUL/control chars, empty string) via
`_validate_conversation_component()` BEFORE any path arithmetic. Raises `PathTraversalError`
on invalid input.

**Conformance test:** pass `conversation_id='../evil'`; assert `PathTraversalError` raised;
strip the validation; verify RED.

### MUST 4 — Atomic turn writes (temp+fsync+rename)

`write_turn()` MUST use `atomic_write()` (temp + fsync + rename) for crash safety. A
crash mid-write leaves a stale `.tmp` file that is excluded from `load_turns()` by the
`*.json` glob. The `.tmp` file is safe to delete manually.

**Conformance test:** call `write_turn()` and assert that the turn file exists as a
committed `.json` file, and that no `.tmp` file remains.

### MUST 5 — Honest capability declaration

`capabilities()` MUST return a `ConversationCapabilities` with `backend_id` set to the
stable identifier and `supports_principal_isolation=True`. A backend that claims
`supports_principal_isolation=False` MUST NOT be used in production.

**Conformance test:** assert `capabilities().supports_principal_isolation == True` and
`capabilities().backend_id` is a non-empty string.

### MUST 6 — Assembly slot (messages[], not system prompt)

Prior turns loaded by `load_turns()` MUST be injected as role-tagged message dicts in
the LLM `messages[]` array BEFORE the current `work_item` entry. They MUST NOT be
injected into `assemble_system_prompt()`. This preserves the T14 cacheable prefix.

**Conformance note:** this MUST is enforced in `agent.call()`, not in the backend impl.
The backend MUST return turns in chronological order (oldest first) so the caller can
prepend them directly.

**Injection normalization:** before injecting, `agent.call()` MUST normalize the
prior-turn sequence so the provider API does not reject the request:
(a) drop empty-content turns (a tool-only assistant turn persists `content=''`);
(b) collapse consecutive same-role entries (keep the latest). **Context-loss caveat:** this is not pure dedupe — when (a) removes an empty-content
turn BETWEEN two same-role turns (e.g. `[user_a, assistant_'', user_b]` → after (a)
`[user_a, user_b]`), (b) keeps only `user_b` and silently discards `user_a` (a real
prior message). `continuity_persisted` stays `True` and the caller cannot detect the
loss. This only fires on an empty-content adjacency (rare) and keeps output alternation
valid. Concatenating same-role content instead of discarding the older turn is tracked
as a follow-up issue (#NNN — file on first occurrence);
(c) drop a trailing user turn (it would sit immediately before the new `work_item` user
turn — consecutive same-role — and is the orphan left by a failed assistant write-back);
(d) drop any LEADING assistant turn(s) — budget eviction is newest-first, so when it cuts
mid-pair the oldest kept turn can be an assistant turn, and the provider rejects a
`messages[]` array whose first message role is `assistant`. This is the symmetric
counterpart to (c).
The result is strict role alternation that BEGINS with a user turn and has no empty
content block.

### MUST 7 — Turn write-back failure is non-fatal

On any `ConversationBackendError` OR sibling `PathTraversalError` (raised on a malformed
caller-supplied `conversation_id`) from `write_turn()`, the implementation MUST:
- NOT raise (the LLM call already succeeded and is billed).
- Return the `Response` with `continuity_persisted=False`.
- Log a WARNING carrying `run_id`.

The same widened catch (`ConversationBackendError` AND `PathTraversalError`) applies on
the `load_turns()` path: a bad `conversation_id` MUST degrade to single-shot (no prior
context), never crash the billed call. `agent.call()` MUST also set
`continuity_persisted=False` on the mid-loop cost-cap skip Response (no write-back ran).

**Conformance test:** inject a backend that raises `ConversationBackendError` on
`write_turn()`; assert `response.continuity_persisted == False`; assert WARNING logged
with `run_id`. A second test passes `conversation_id='../evil'` and asserts the call
returns (no uncaught traceback) with `continuity_persisted=False`.

### MUST 8 — Budget-bounded deterministic load

`load_turns(budget_tokens=N)` MUST return the most-recent turns whose accumulated token
estimate fits within `N` (oldest-first eviction). When `budget_tokens <= 0`, MUST return
`[]`. The token estimate MUST be `(len(role) + len(content)) // 4 + 1` (character-to-token
approximation).

**One-turn floor:** when the newest turn alone exceeds `budget_tokens`, return `[]`.
Callers MUST supply a budget large enough for at least one turn.

**Conformance test:** write 5 turns; call with `budget_tokens` sized to hold exactly the 3
most-recent; assert exactly those 3 are returned in chronological order.

**Load-side schema validation (defense-in-depth):** `load_turns()` MUST validate each
parsed turn before returning it — `role` in `{"user", "assistant"}`, `content` is a
`str`, and `seq` is a non-negative non-bool `int`. A turn file that violates the schema
(e.g. a malformed or maliciously-written file injecting an out-of-contract provider
role such as `"system"`) MUST raise `ConversationCorrupted`, which is treated like any
other corrupted turn — skipped with a WARNING and `run_id` logged — never surfaced into
the `messages[]` array. This is the load-side counterpart to the MUST 3 write-side
component validation: the on-disk turn files are not trusted blindly on read.

### MUST 9 — Backward-compatible None default

`None` (no backend configured) MUST preserve today's exact single-shot behavior. NO
`conversations/` directory created on agent construction. No behavioral change for callers
that never set `conversation_id`. This is rule #14 (backward compatibility by default).

**Conformance test:** construct `AtomicAgent` without setting a conversation backend;
call `agent.call(work_item)` without `conversation_id`; assert `conversations/` does not
exist under `agent_root`.

### MUST 10 — spec/40 Exportable companion

`export()` MUST return a `ConversationExport` containing all durable turn files as
`(relative_path, raw_bytes)` tuples. Stale `.tmp` files MUST be excluded. The
`supports_canonical_export` capability MUST be `True`.

**Conformance test:** write turns; call `export()`; assert all turn files appear in
`entries_with_bytes`; assert no `.tmp` files appear.

---

## Filesystem backend specifics

### Symlink containment — two layers

**Layer 1** (`_conversations_dir()`): resolves `agent_root/'conversations'` and checks
`is_relative_to(agent_root.resolve())`. A symlinked `conversations/` pointing outside
`agent_root` raises `PathTraversalError` on writes; returns `[]` on reads (absent semantics).

**Layer 2** (`_require_canonical_turn_path()`): resolves BOTH a containment root AND
the per-entry turn file path, asserts `is_relative_to`, and asserts the entry is NOT a
symlink. Called at every read/write sink. On the WRITE path the containment root MUST be
the per-conversation target directory (`conv_dir`), NOT the `conversations/` root.
`turn.ts` and `turn.role` are interpolated raw into the on-disk filename and are NOT
bare-component validated (only `turn.run_id` is); a `conversations/`-scoped check would
let a custom caller passing `turn.ts='../../sibling'` land the turn file in a SIBLING
principal/conversation subtree (still under `conversations/`) and poison another
principal's history. Scoping the single canonical-path invariant to `conv_dir` refuses
any filename-component escape in one place (one canonical-path invariant, not per-field
checks). On the READ path the turn files are enumerated from `conv_dir.glob("*.json")`,
so they are already conv_dir-scoped at the source.

`export()` does NOT use a flat `conversations/`-rooted `rglob()`: it iterates principal
directories explicitly and runs the same per-principal IDENTITY guard the read path uses
before enumerating each subtree, so a redirecting symlink `conversations/bob ->
conversations/alice` is skipped rather than aliasing alice's turns into bob's exported
namespace (the latter would double-count on Python 3.13+ where `rglob` follows directory
symlinks). This is the FIRST principal-scoped filesystem backend, so its export carries
this extra check beyond the flat-`rglob()` pattern of the dedup/journal backends.

### Conv-dir containment in write_turn()

Before `conv_dir.mkdir()`, `safe_resolve_under(conv_dir, principal_dir)` is called.
A pre-staged symlink `conv_dir -> /etc/` would pass `mkdir` but this guard fires first.

### Concurrency

An exclusive `fcntl.flock` on `<agent_root>/conversations/<principal>/.conv.lock`
serializes ALL turn writes for a given principal across concurrent `call()` invocations.

### Crash recovery

Stale `*.tmp` files left by crashed `atomic_write()` calls are excluded from `load_turns()`
by the `*.json` glob. The `.conv.lock` sidecar persists but is not matched by `*.json`.
Both are safe to leave on disk. Doctor MUST NOT treat their presence as corruption.

---

## LogQuery.conversation_id (spec/22 versioned normative addendum)

`conversation_id` is tagged on ALL terminal JSONL records when `agent.call()` is invoked
with `conversation_id` set — all seven sites: ok, dedup, lock_busy, pre-loop cost-skip,
in_flight, mid-loop cost-skip, and security-abort.

`LogQuery.conversation_id` (added to `logs/types.py`) is a versioned normative
addendum to spec/22 (mirroring the `idempotency_key` predicate, spec/22 item 4):
conforming `LogBackend` implementations MUST support it as an AND-predicate returning
ONLY records whose `conversation_id` matches — `None` means no filter (all records).
SQLite and Postgres MUST resolve it via the `idx_conversation_id` partial index
(equality `= ?` / `= %s` lookup matching the partial predicate, so it is an index
seek). Shipping the column + index WITHOUT the predicate is non-conforming: the
parametrized round-trip conformance test (`test_query_filters_by_conversation_id`,
with a strip negative control) fails such a backend rather than passing it.

SQLite and Postgres `LogBackend` implementations bump to schema `v3` in this PR:
- SQLite: `v2→v3` migration adds `conversation_id TEXT` column + `idx_conversation_id`
  partial index (PRAGMA table_info guard for crash-resumability).
- Postgres: `v2→v3` migration adds `conversation_id TEXT` column +
  `idx_conversation_id` partial index (`ADD COLUMN IF NOT EXISTS`, idempotent).

Both `_SCHEMA_VERSION` constants move from `2` to `3` in this PR.

**Note:** DB-gated tests (`requires_postgres`) skip locally; the SQLite + Filesystem
`conversation_id` conformance tests run locally on every `pytest` (the SQLite log backend
uses a no-service file/in-memory DB). Schema-version assertions in all three log backend
test files must be updated when this PR lands; expect a red-CI cycle only on the Postgres
lane if any assertion hardcodes `schema_version == 2`.

---

## Response.continuity_persisted

`Response.continuity_persisted: bool = True` (in `atomic_agents/types.py`):
- `True`: turn write-back succeeded, or `conversation_id` was not set (irrelevant).
- `False`: turn write-back failed; the LLM call succeeded and is billed; the turn is NOT
  persisted. Mirrors `cost_data_degraded` (#498) / `deduped` (#520) field pattern.

---

## Deferred to later PRs

- `PostgresConversationBackend` — Phase 3 scale-out PR. The filesystem reference
  implementation is production-suitable for single-host deployments. Postgres (with
  pgvector for semantic conversation search) is the org-shape backend, deferred to
  align with the Phase 3 state-backend scale-out arc (#344).
- ~~spec/37 serve `conversation_id` wiring (HTTP path).~~ **SHIPPED with PrincipalBackend
  (#556):** `serve/_app.py` now extracts + validates `conversation_id` from the request body
  (bare component, no separators/control chars, max 512 chars; 422 on violation) and threads
  it through `run_agent_call()` to `agent.call()`, gated by the verified `Principal` from the
  serve HYBRID flow.
- Summarization (PR3, budget-bounded same contract).
- Doctor check for orphaned `.tmp` turn files.
- `write_turns(list[Turn])` — atomic two-turn write path (eliminates PR1 crash boundary).
- **`continuity_persisted` flag-flip for deferred/short-circuit paths.** When
  `response.deferred is True` (ESCALATE path), the conversation write-back is now
  gated out (the #553 fix), so no turns are written — but `continuity_persisted`
  retains its `True` default (same as refusal short-circuits). Callers MUST NOT
  rely on `continuity_persisted` being meaningful when `response.deferred is True`.
  A future PR MAY flip the flag to `False` on deferred paths so callers can
  distinguish "stored this run" from "paused, not yet stored".
- **Idempotency × conversation interaction.** A deduped / in-flight `call()`
  short-circuit does NOT write conversation turns (idempotency must not double-write),
  so the conversation history reflects only first-delivery runs — a deduped call's
  user+assistant turns never enter the thread. The terminal dedup JSONL record IS
  tagged with `conversation_id` (audit-complete, queryable via `LogQuery`), but
  `continuity_persisted` keeps its `True` default on refusal short-circuits (the field
  is documented as "not meaningful on refusal paths"). A future PR MAY decide whether a
  deduped call should replay the prior turn pair and/or carry `replayed_run_id`-awareness
  so a caller can distinguish "stored this run" from "replayed a prior run".
- **Implicit body-hash dedup is SKIPPED for conversation calls.** `agent.call()`'s
  implicit body-hash idempotency-key auto-derivation is gated on `conversation_id is
  None`. The implicit hash covers only `work_item` + model + `max_tokens` +
  `temperature` and OMITS prior conversation turns, so auto-deduping a conversation call
  would replay a stale / cross-conversation result. Explicit caller-supplied
  idempotency keys are unaffected (the caller owns the key's semantics).

---

## Serve HYBRID flow — principal threading (spec/48 clarifying note)

> **NOTE (non-normative):** The `Principal` parameter in `load_turns()` and `write_turn()`
> was defined here specifically to support serve-layer multi-principal threading.
> The serve layer's HYBRID flow (spec/48) derives a verified `Principal` from the
> perimeter identity header and passes it as the `principal` argument to `agent.call()`,
> which threads it to these methods. No normative rule in this spec changes; the
> extension point was always present.

---

## Cross-references

- TENSIONS T16 (raw-transcript injection — approved, committed on `docs/tensions-t16-conversation-flex`)
- spec/22 (LogBackend — `conversation_id` versioned normative addendum)
- spec/40 (canonical export — `ConversationExport` companion)
- spec/45 (IdempotencyBackend — `continuity_persisted` field precedent)
- spec/48 (PrincipalBackend — identity derivation + HARD-REFUSE gate; the `Principal` type and `LOCAL_PRINCIPAL` live in `conversation/types.py` and are re-exported from `principal/`)
- spec/37 (serve — `caller_identity` vs `Principal` distinction)

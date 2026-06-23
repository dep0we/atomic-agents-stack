# spec/40: Canonical-shape Export Contract (Exportable Protocol)

> **Status:** LOCKED (issue #379, 2026-06-10). Delivered in #379 PR 1: the `Exportable` companion Protocol + filesystem reference impls + a 91-test round-trip conformance suite covering all MUSTs for the five PR1 state backends (Memory, Log, Mandate, Corpus, Lock) plus the SecretBackend never-leak invariant (MUST 9). Locked on filesystem proof per the contract-first ruling; per-backend SQLite/Postgres/Redis/GCP/HTTP-registry export impls are later PRs that conform to this locked contract. See `tests/test_export_capability_advertisement.py` and `tests/test_export_protocol_conformance.py`.

---

## Origin

Filed as [#379](https://github.com/dep0we/atomic-agents-stack/issues/379) after the twelve v1.0 backend protocols shipped. Addresses TENSIONS.md T15 "Position B" decision: **the canonical file shape is always reconstructable** — portability is "the file shape is round-trippable," not "the files are always the truth." This spec delivers the implementation contract (spec/40) and the `Exportable` companion Protocol that backends compose when they satisfy that contract.

**Cross-links:**
- CLAUDE.md Principle #1 (vault-as-truth; T15 flexes for swapped-backend deployments — see TENSIONS.md T15 and this spec).
- TENSIONS.md T15. "Position B" ruling: state portability via canonical round-trip export, not lock-in to vault-relative paths.
- spec/20 (MemoryBackend), spec/21 (LogBackend), spec/22 (LockBackend), spec/29 (MandateBackend), spec/34 (CorpusBackend) — all carry spec/40 addenda declaring `supports_canonical_export=True`.
- spec/38 (SecretBackend) — carries spec/40 addendum; MUST 9 (never-leak) applies unconditionally.

---

## Shipping plan (1 PR)

- **PR 1 (this PR).** `Exportable` companion Protocol + type hierarchy + per-backend filesystem export impls (Memory, Log, Mandate, Corpus, Lock, Secret) + `supports_canonical_export` capability field on each backend's XCapabilities frozen dataclass + parametrized round-trip conformance scaffold + spec/40 DRAFT + cross-references.

---

## Overview

`Exportable` is an **optional companion Protocol** that state backends compose when they can produce a canonical on-disk byte-shape for portability testing and round-trip validation. It is NOT a method bolted onto the thirteen locked backend Protocols — those Protocol surfaces are frozen (CLAUDE.md Principle #2: "Don't re-lock specs to add a method").

```
Backend (locked Protocol surface)
    +
Exportable (optional companion Protocol — structural, not inherited)
    ↓
isinstance(backend, Exportable)  → True when backend has export() + export_all()
```

The `supports_canonical_export` flag on each backend's XCapabilities dataclass is the advertised claim. The `isinstance` check is the structural verification. Conformance tests assert claim-vs-behavior parity (no silent False).

---

## Module layout

```
atomic_agents/export/
├── __init__.py     # public surface: Exportable, all result types, all query types
├── backend.py      # Exportable Protocol + snapshot-consistency documentation
├── types.py        # ExportableResult hierarchy + SecretExportRef + query types
├── renderer.py     # shared per-type renderers (one renderer, consumed by Tier A + B)
└── filesystem.py   # filesystem reference export impls (Memory, Log, Mandate, Corpus, Lock, Secret)
```

Filesystem export functions live in `atomic_agents/export/filesystem.py` (export side) rather than inside each backend module (to keep the 13 backend modules frozen and to locate all export logic in one place per TENSIONS.md T15).

---

## Type hierarchy

```
ExportableResult (marker base)
├── MemoryExport(notes_with_bytes: list[tuple[Note, bytes]], backend_id, scope)
├── LogExport(records_with_bytes: list[tuple[RunRecord, bytes]], backend_id, scope)
├── MandateExport(mandates_by_scope: dict[str, list[Mandate]], backend_id, scope_root)
├── CorpusExport(pages_with_bytes: dict[str, list[tuple[Page, bytes]]], backend_id, scope)
├── LockExport(scope_root, backend_id, lock_file_names: list[str])
└── SecretExport(entries: list[SecretExportRef], backend_id)

SecretExportRef(logical_key, hint, present)  — NEVER contains resolved plaintext
```

Query types mirror result types:
```
MemoryExportQuery(include_archived=False, include_superseded=False, include_versions=False)
LogExportQuery(log_query=None)
MandateExportQuery(scopes=None)
CorpusExportQuery(corpus=None, limit=None, offset=0)
LockExportQuery()
SecretExportQuery(logical_keys=None)
```

---

## Tier A vs Tier B fidelity

**Tier A — byte-exact round-trip (filesystem reference impl):**
- Note bytes: raw file bytes read directly (NOT re-rendered through `frontmatter.dumps()`). Re-rendering diverges on date formatting (quoted vs unquoted) and `extra_frontmatter` key ordering. The renderer `render_note_bytes_from_raw()` is a passthrough.
- RunRecord bytes: `json.dumps(record.to_dict()).encode("utf-8") + b"\n"`. NOT `canonical_json()` (which uses `sort_keys=True` and would produce different key order than `to_dict()`'s ts-first insertion order).
- Corpus page bytes: raw file bytes read directly (same passthrough approach as notes).

**Tier B — field-lossless (structured-DB backends, future):**
- Re-serialization through the shared renderer is permitted. Formatting (date quote style, key ordering) may differ from the original write. The canonical object graph (all fields, all values) MUST round-trip exactly.
- Documented formatting loss is acceptable for Tier B. A conformance test MUST assert field-level round-trip, not byte-level.

**Tier A fidelity constraints (UTF-8, LF, no BOM):**
All exported bytes MUST be UTF-8 encoded, MUST use LF (`\n`) line endings (no CRLF), and MUST NOT include a byte-order mark. These constraints hold for both Tier A and Tier B exports.

---

## Implementer Contract (MUST)

All backends that advertise `supports_canonical_export=True` MUST satisfy the following:

**MUST 1 — Protocol composition.** The backend MUST satisfy `isinstance(backend, Exportable)` — it MUST have both `export(query=None) -> ExportableResult` and `export_all() -> ExportableResult` methods with the correct signatures.

**MUST 2 — Capability claim honesty.** The backend MUST advertise `supports_canonical_export=True` via the appropriate capability surface (direct `@property`, callable `capabilities()` returning a dataclass, or `@property capabilities` returning a dataclass). A backend that advertises True but raises `NotImplementedError` from `export()` is a conformance failure.

**MUST 3 — `export_all()` thin wrapper.** `export_all()` MUST call `export(query=None)` and return its result unchanged. It MUST NOT pre-collect state differently or apply filters that `export(None)` does not apply.

**MUST 4 — Tier A byte-exact fidelity.** For filesystem reference implementations, `export()` MUST return raw file bytes — NOT bytes produced by re-serializing the canonical object through a renderer under test. Concretely: note and corpus-page bytes MUST be read via `path.read_bytes()`. RunRecord bytes MUST be produced by `json.dumps(record.to_dict()).encode("utf-8") + b"\n"`.

**MUST 5 — UTF-8, LF, no BOM.** All exported bytes MUST be UTF-8 encoded, MUST use LF line endings, and MUST NOT include a byte-order mark. This applies to both Tier A and Tier B exports.

**MUST 6 — State enumeration, not retrieval.** Export MUST enumerate state via Protocol list methods (`list_notes()`, `list_pages()`, `list_mandates()`, etc.), NEVER via semantic-retrieval methods (`query(text)`, embedding search). Export is state extraction; query is semantic search. The distinction is load-bearing for correctness: semantic retrieval returns a filtered subset, not the full state.

**MUST 7 — Snapshot consistency bound.** `export()` produces a best-effort point-in-time snapshot. It MUST NOT acquire a cross-backend lock during the read pass (that is the caller's responsibility if cross-object consistency is required). Each individual object MUST be read atomically (not mid-write). Implementations MUST document this bound.

**MUST 8 — RunRecord key order.** Exported RunRecord bytes MUST use `json.dumps(record.to_dict())` (ts-first insertion order) — NOT `canonical_json()` (which uses `sort_keys=True`). This is the critical Tier A fidelity constraint for LogBackend export. The conformance test MUST assert the first key is `"ts"`.

**MUST 9 — SecretBackend never-leak invariant (ABSOLUTE).** The `SecretBackend` export MUST NEVER contain resolved plaintext credential values. The export adapter MUST call `backend.locate(key)` exclusively — NEVER `backend.get()` or `backend.get_optional()`. `SecretExportRef` MUST NOT contain env-var names, file paths, or source strings — only a logical key and a deployment-agnostic hint. A conformance test MUST assert that no plaintext value appears in any export output bytes. This MUST is an absolute invariant — it is NOT capability-gated and applies regardless of `supports_canonical_export` flag value.

---

## Per-backend export contracts

### MemoryBackend export contract

`MemoryExport.notes_with_bytes` contains `(Note, raw_bytes)` tuples. `raw_bytes` is the raw file content from `<memory_dir>/<note_stem>.md` (Tier A). The Note object is for metadata inspection; the raw bytes are what the renderer emits.

`include_versions=False` is the default per `MemoryExportQuery`. Version history is an opt-in SHOULD (deferred to [#433](https://github.com/dep0we/atomic-agents-stack/issues/433) — setting `True` currently behaves as `False`).

MemoryBackend uses the `@property supports_canonical_export` idiom (matching `supports_semantic_search`). A `MemoryCapabilities` dataclass convergence is deferred ([#431](https://github.com/dep0we/atomic-agents-stack/issues/431)).

### LogBackend export contract

`LogExport.records_with_bytes` contains `(RunRecord, raw_bytes)` tuples. `raw_bytes` is the EXACT JSONL line as it exists on disk for that record — the filesystem impl reads the actual shard files and pairs each queried record with its verbatim on-disk bytes, matched by the line's natural identity `(run_id, ts)` (which survives the type coercion `RunRecord.from_dict` applies — e.g. a string-typed `input_tokens` on disk parses to an int on the record, so a content hash of `to_dict()` would NOT resolve back to the original line, but the natural-identity key does). A legacy or hand-edited line therefore round-trips byte-for-byte. A record that the current `to_dict()` would write is byte-identical to `json.dumps(record.to_dict()).encode("utf-8") + b"\n"` (the ts-first insertion order of MUST 8); a record not resident on disk falls back to that same renderer output. See MUST 8.

**Verbatim fidelity guarantee.** `export_all()` (unbounded query) MUST export every on-disk line byte-for-byte, including blank-ts lines (which land in today's shard via the `_record_date` fallback) and hand-misfiled lines (whose physical shard month does not match their `ts` value). The filesystem impl achieves this by deriving the shard-walk window from the QUERY's own `since`/`until` bounds — not from the min/max ts of the matched records. For an unbounded query all shards are walked with no prefilter. **Residual bounded-query limit (documented per Principle 13):** a hand-misfiled line whose physical shard falls outside a DATE-BOUNDED `LogExportQuery`'s `since`/`until` window may be re-serialized via `render_run_record_bytes()` rather than emitted verbatim. This is a rare anomaly (correct filing is the normal case); a bounded export is a filtered view and the re-serialized bytes are functionally correct. The verbatim-fidelity guarantee is absolute for `export_all()` and for all correctly-filed records in bounded exports.

### MandateBackend export contract

`MandateExport.mandates_by_scope` is a `dict[scope_str, list[Mandate]]`. Keys are `"project:<name>"` or `"agent:<name>"` scope strings. Scope discovery for `query=None`: the filesystem impl scans for `mandates.md` files under `scope_root`.

The `.judge-state/mandates.json` dedup sidecar is intentionally excluded from export — it is an implementation detail, not a portable agent artifact.

The project-root `## _meta` policy block IS captured. `list_mandates()` parses but discards the `(_meta, mandates)` tuple's first element, so `export_mandate` re-parses each project-root scope to recover the `ProjectMandateMeta` (`per_agent_mandate_policy` + `allowed_per_agent_ids`) and carries it on `MandateExport.meta_by_scope`. This block is a **security boundary** governing which agents may hold mandates — dropping it would silently revert a `forbidden` policy to the `open` default on re-import. `render_mandates_md(mandates, meta)` emits a leading `## _meta` section when `meta` is present, and re-parsing with `is_project_root=True` reconstructs the same `ProjectMandateMeta`.

`render_mandates_md()` produces RE-PARSEABLE text: feeding its output back through `parse_mandates_md` reproduces the same `mandate_id`, prose scope, constraints, `granted_by`, and `revocation_state`. The parser requires `granted_by`, `granted_at`, `scope`, and `revocation_state` unconditionally, so the renderer always emits all four — `scope` comes from `Mandate.prose_scope` (the human-readable authority description retained at parse time), and `revocation_state` is emitted even for the `active` value. The backend-local `source_path` is NOT emitted: it is a deployment-specific absolute path, not part of the portable authored shape (same portability rule `SecretExportRef` enforces against source strings). The conformance test re-parses the rendered bytes and asserts field equality — not a substring check.

**Tier A fidelity caveats (MandateBackend).** Two authored-or-derived fields do NOT survive the definition-export-and-reimport round-trip, both acceptable Tier-A-not-byte-exact behavior but stated here so operators are not misled:
- `revocable_by` — parsed (`mandates_md.py`) but NOT stored on the `Mandate` dataclass, so it is already dropped on the read path; a re-parse resets it to the default `operator`. It is not enforcement-dispatch-relevant.
- per-mandate `source_hash` — recomputed (SHA-256 over the canonical section text) on every parse. The re-rendered text differs from the original section (YAML key reorder, `revocable_by` dropped, quote style), so a re-imported mandate gets a fresh `source_hash`. A deployment that enforces spec/29's suspicious-rebind throttle (which keys on `source_hash`) will treat a re-imported mandate as a new binding.

### CorpusBackend export contract

`CorpusExport.pages_with_bytes` is a `dict[corpus_name, list[(Page, raw_bytes)]]`. Both `"wiki"` and `"raw"` corpora are included by default. `CorpusExportQuery(corpus="wiki")` filters to one corpus only.

Associativity invariant: exporting both corpora in one call MUST produce the same page sets as exporting each corpus separately (see conformance test).

### LockBackend export contract

`LockExport` carries `scope_root` and `backend_id` only. `lock_file_names` is ALWAYS `[]` — runtime lease files (`.lease.json`, `.lock`) are ephemeral and MUST NOT be exported. `supports_canonical_export=True` affirms Protocol composition; it does NOT imply there is persistent lock state to migrate. The conformance test MUST assert `lock_file_names == []` even when a lock is currently held (not skip-and-assume).

### SecretBackend export contract (PR1 scope)

PR1 scopes export to the framework's canonical provider keys (`anthropic`, `openai`, `moonshot`) only. Custom operator keys are deferred ([#432](https://github.com/dep0we/atomic-agents-stack/issues/432)).

For each logical key, the export calls `backend.locate(key)` to determine presence. The result is a `SecretExportRef(logical_key, hint, present)` — NEVER the resolved value, NEVER the env-var name or keychain label. `hint` is a deployment-agnostic description (e.g., `"Anthropic Claude API credential"`).

---

## `supports_canonical_export` capability field

Each backend's XCapabilities frozen dataclass gains `supports_canonical_export: bool = False` as the LAST field with a default to preserve backward compatibility at all existing instantiation sites:

| Backend | Capability dataclass | Default | Filesystem ref impl |
|---------|---------------------|---------|---------------------|
| MemoryBackend | `@property` (minority idiom) | — | `True` |
| LogBackend | `LogCapabilities` | `False` | `True` |
| LockBackend | `LockCapabilities` | `False` | `True` |
| MandateBackend | `MandateCapabilities` | `False` | `True` |
| CorpusBackend | `CorpusCapabilities` | `False` | `True` |
| MCPServerRegistryBackend | `MCPServerRegistryCapabilities` | `False` | deferred |
| SecretBackend | `SecretCapabilities` | `False` | `True` |

The remaining non-state backends (LLM, Judge, Policy, AgentProfile, ToolRegistry, Persona) do NOT carry a `supports_canonical_export` field in PR1. They advertise `False` implicitly: `get_supports_canonical_export()` (the conformance helper) returns `False` for any backend whose capability surface lacks the attribute/property. They are not export surfaces per the T15 Position B ruling. Adding the field to those dataclasses is deferred to whichever later PR (if any) makes one an export surface — PR1 adds it only to the surfaces that advertise `True` (the state backends + MCPServerRegistry, which carries it defaulting `False`).

---

## Renderer module

`atomic_agents/export/renderer.py` provides ONE shared renderer per type. There are two renderer classes, with different byte sinks:

**(a) Byte-paired Tier-A passthrough + RunRecord renderers** — consumed by the filesystem export impls IN PRODUCTION. `export_memory`, `export_corpus`, and `export_log` call these directly inside their export path, so the reference read-path and export-path cannot structurally disagree:

```python
render_run_record_bytes(record)          # json.dumps(record.to_dict()) + "\n"  (export_log fallback)
render_note_bytes_from_raw(raw_bytes)    # Tier A passthrough  (export_memory)
render_corpus_page_bytes_from_raw(raw)   # Tier A passthrough  (export_corpus)
```

**(b) Object-graph renderers** — for the types where the typed `ExportableResult` IS the export and byte-rendering is the consumer's job. `export_mandate()` returns a `MandateExport` of typed `Mandate` objects (+ `_meta`) and `export_secret()` returns a `SecretExport` of `SecretExportRef` objects; neither calls a renderer. These renderers are consumed by export consumers — the [#430](https://github.com/dep0we/atomic-agents-stack/issues/430) CLI and the conformance suite — and by any future Tier B (structured-DB) backend that has no raw bytes to pass through:

```python
render_note_bytes_from_object(note)      # Tier B re-serialize via frontmatter.dumps()
render_corpus_page_bytes_from_object(p)  # Tier B re-serialize
render_mandates_md(mandates, meta=None)  # list[Mandate] (+ _meta) → mandates.md text
render_secret_export_bytes(entries)      # list[SecretExportRef] → JSON bytes (no plaintext)
```

The renderer is the shared path — when a Tier B (structured-DB) backend implements `export()` and must serialize to bytes (e.g. for the #430 CLI), it calls the same renderer the conformance suite uses. This keeps byte formatting consistent across backends.

---

## Conformance test architecture

Two test files, one shared helper:

- `tests/test_export_capability_advertisement.py` — NOT capability-gated. Asserts directly that all six PR1 filesystem backends advertise `supports_canonical_export=True`. Prevents the skip-all-by-accident failure mode.
- `tests/test_export_protocol_conformance.py` — capability-gated round-trip tests via `assert_canonical_roundtrip(backend, write_fn, expected_bytes_fn)`. Per-backend sections for Memory, Log, Mandate, Corpus, Lock, Secret.

The `get_supports_canonical_export(backend)` helper in `tests/test_export_capability_advertisement.py` handles all three capability-access shapes:
1. Direct `@property` on the backend (MemoryBackend)
2. Callable `capabilities()` method returning a dataclass (Log/Lock/Corpus/Mandate)
3. `@property capabilities` returning a dataclass directly (MCPRegistry/Secret)

The expected-bytes in `assert_canonical_roundtrip` come from REAL on-disk fixture files written by the backend's write methods — NOT from the renderer in isolation. This forces the test to catch any divergence between the renderer and the actual write path.

---

## Granularity and export_all() guidance

`export(query=...)` with a bounded query is the org-fleet path. `export_all()` is a
thin wrapper that calls `export(query=None)` — it materialises **all** matching objects
into memory. For backends with large histories (>10K notes, >1M log records), callers
MUST use `export(query=...)` with a bounded time window or limit instead of `export_all()`.

Home-user deployments typically have small histories and `export_all()` is appropriate.
Org-fleet exporters MUST choose an appropriate `LogExportQuery(log_query=LogQuery(since=...))`,
`MemoryExportQuery(include_archived=False)`, or similar to bound the **returned result set**.

**LogBackend shard prefilter (F1 fix).** A bounded `LogExportQuery` (one that carries `LogQuery.since` or `LogQuery.until`) gates the shard walk on the query's own date bounds via `_month_overlaps_window`, so disk I/O is bounded to the relevant month/day shards. An unbounded query (export_all) walks all shards — this is required so blank-ts and misfiled lines are found verbatim (see §"LogBackend export contract"). The prefilter derives its window from the query's explicit bounds, NOT from the ts values of the matched records; the latter approach would incorrectly skip today's shard for blank-ts lines (spec/40 F1 bug fix, resolved in PR1).

---

## Cross-spec addenda

The following spec docs carry spec/40 addenda:

| Spec | Backend | `supports_canonical_export` |
|------|---------|----------------------------|
| spec/20 | MemoryBackend | True (via `@property` idiom) |
| spec/21 | LockBackend | True (LockCapabilities field) |
| spec/22 | LogBackend | True (LogCapabilities field) |
| spec/29 | MandateBackend | True (MandateCapabilities field) |
| spec/34 | CorpusBackend | True (CorpusCapabilities field) |
| spec/38 | SecretBackend | True (SecretCapabilities field, MUST 9 absolute) |
| spec/47 | ConversationBackend | True (ConversationCapabilities field, MUST 10) |

The non-state backends (LLM, Judge, Policy, AgentProfile, ToolRegistry, Persona,
GCPSecretManagerBackend) advertise `False` and are not export surfaces. With ONE
exception, their capability surfaces do NOT carry a `supports_canonical_export`
field in PR1 — they advertise `False` implicitly because `get_supports_canonical_export()`
returns `False` for any backend whose capability surface lacks the attribute. The
exception is `MCPServerRegistryCapabilities`, which carries the field defaulting to
`False` (its filesystem/HTTP export is deferred). PR1 adds the field only to the six
state-backend surfaces that advertise `True` (Log/Lock/Mandate/Corpus/Secret
dataclasses + Memory's `@property`) plus `MCPServerRegistryCapabilities`.

### Versioned normative addendum — ConversationBackend export contract (spec/47 MUST 10)

`ConversationBackend.export()` MUST return a `ConversationExport` containing all
durable turn files as `(relative_path, raw_bytes)` tuples. Stale `.tmp` files MUST
be excluded.

**Principal-scoped export (first principal-scoped backend):** unlike the flat-`rglob()`
pattern used by dedup/journal backends, `FilesystemConversationBackend.export()` iterates
principal directories explicitly and runs the per-principal IDENTITY guard (MUST 2:
resolved-basename + inode-identity two-part check) before enumerating each subtree. A
redirecting symlink `conversations/bob -> conversations/alice` is skipped rather than
aliasing alice's turns into bob's exported namespace. This is required because Python
3.13+ `rglob()` follows directory symlinks, which would double-count on a flat walk.

**Stale `.tmp` exclusion:** the `*.json` glob already excludes stale `.tmp` files from
`load_turns()`. The same glob applies in `export()`.

**`supports_canonical_export`:** `ConversationCapabilities.supports_canonical_export`
MUST be `True` for the filesystem reference implementation.

Added OUTSIDE the spec/40 MUST 1–10 count, following the existing cross-spec addenda
precedent.

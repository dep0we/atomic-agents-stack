# spec/33 — PersonaBackend (shared-persona identity layer)

> **Status: RFC.** Locked at PR 4 of #62.

> **Pre-#62 PR 1 amendments (2026-05-26):** Drafted in PR 1 from the /office-hours + /plan-eng-review locked design doc (`~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-design-20260522-134508.md`). 7 architectural decisions locked (D1-D7) + 5 supplementary decisions (D1a, D2a, D-ER-1 through D-ER-4) + 1 pre-impl prep amendment (D-PI-1 2026-05-26). Persona exceptions land in `atomic_agents/exceptions.py` (not `persona/types.py`) to prevent cross-module import cycles.

## Overview

`PersonaBackend` is the tenth open Protocol in the protocol-pattern series (Memory, LLM, Judge, Lock, Log, AgentProfile, ToolRegistry, Mandate, Policy, **Persona**). Each Protocol decouples one storage / dispatch axis so the framework's core stays small and alternate implementations drop in without forking.

PersonaBackend ships as a **separate Protocol from AgentProfileBackend** on architectural grounds (Premise 1 from the locked design doc):

- **Persona is identity; AgentProfile is configuration.** The two have different lifecycle cadences: persona drifts on soul-evolution timelines; agent config drifts on capability timelines. Bundling them conflates two independent concepts.
- **Persona is shareable across agents.** One `customer-support-v3` persona record can be referenced by N regional agents. AgentProfile is per-agent by definition.
- **Fleet-scale and marketplace use cases are persona-centric.** A team running 5 customer-support agents today maintains 5 separate `SOUL.md` files that drift. With PersonaBackend, one canonical persona record serves all 5 agents with consistent identity.
- **Persona templates are conceptually distinct from agent-config templates.** Operators bring their own model + tools to a persona; the template dimension is about "who this agent is," not "how it acts." The `supports_templates` capability field (D5) retires spec/24 line 436's `TemplateProfileBackend` reservation: templates are PersonaBackend's domain.

The composition shape (D1): PersonaBackend is source of truth; AgentProfile fields (`persona_identity` / `persona_soul` / `persona_user`) are denormalized snapshots populated at `load_profile()` time when PersonaBackend owns the agent's persona (signaled by `<agent>/persona.link.md` presence, introduced in PR 3).

**The backwards-compatibility promise:** home users with one agent running the legacy three-file layout (`<agent>/persona/{IDENTITY,SOUL,USER}.md`) see zero behavior change through PR 1, PR 2, and PR 3 unless they explicitly create a `persona.link.md` shared-reference. The 166 existing `AtomicAgent(...)` construction sites remain byte-identical.

## Locked decisions (from /office-hours + /plan-eng-review 2026-05-22 and 2026-05-25)

| # | Topic | Lock |
|---|-------|------|
| D1 | Composition shape | PersonaBackend is source of truth; AgentProfile fields are denormalized snapshots populated at `load_profile()` when PersonaBackend owns the agent's persona |
| D1a | Cross-Protocol wiring | AgentProfileBackend does NOT import PersonaBackend; ownership check is storage-layer-local (`persona.link.md` presence on filesystem; `persona_id` column on SQLite) |
| D2 | One filesystem reference impl | `FilesystemPersonaBackend(personas_root)` only; bound-to-one-agent variant dropped (pollutes conformance pattern) |
| D2a | Mutual exclusion | `<agent>/persona.link.md` AND `<agent>/persona/IDENTITY.md` coexisting raises `PersonaOwnershipConflict` at `AgentProfileBackend.load_profile()` |
| D3 | Snapshot composition | `AgentProfileBackend.snapshot/restore` skip persona fields when PersonaBackend owns the agent's persona; persona has its own snapshot history |
| D4 | Storage namespace | `<scope_root>/.personas/<persona_id>/` (hidden directory mirrors `.snapshots/` pattern; structurally separate from agent namespace) |
| D5 | TemplateProfileBackend retirement | `supports_templates` capability field on PersonaCapabilities retires spec/24 line 436's `TemplateProfileBackend` reservation; templates are PersonaBackend's domain |
| D6 | AgentProfile save discipline | `AgentProfileBackend.save_profile()` ignores `persona_identity/soul/user` when PersonaBackend owns the agent's persona (mirrors spec/24 Decision 6's `agent_mode` pattern) |
| D7 | A/B persona testing | Deferred to v1.1 as `PolicyBackend.get_effective_persona()` extension; NOT in v1.0 |
| D-ER-1 | Ownership Protocol method | `AgentProfileBackend.is_persona_externally_owned(agent_id) -> bool` added in PR 2 so the bootstrap path can check ownership without importing PersonaBackend |
| D-ER-2 | `delegate.py` threading | `delegate.py` threads `persona_backend` ONLY when explicitly supplied at the coordinator (mirrors Policy's `_policy_backend_was_explicit` precedent at `agent.py:401`). Default-resolved PersonaBackends do not leak the coordinator's `personas_root` to delegates. See body section "`delegate.py` threading (D-ER-2)" for full rationale. |
| D-ER-3 | `persona.link.md` parser | Parser (`persona_link_md.py`) lands in PR 2; raises `PersonaLinkInvalid` on malformed YAML, missing `kind:`, unsupported kind, missing `persona_id:`, or invalid `persona_id:` charset |
| D-ER-4 | `persona.link.md` format | YAML in a code block; two scalar fields: `kind: shared` + `persona_id: customer-support-v3` |
| D-PI-1 | Exception placement | Persona exceptions live in `atomic_agents/exceptions.py` (not `persona/types.py`) to prevent cross-module import cycles (`PersonaOwnershipConflict` raised by profile backends; `PersonaLinkInvalid` raised by parser) |

## `PersonaBackend` Protocol surface

Every backend implementation MUST satisfy this contract (structurally; do not subclass -- the Protocol is `@runtime_checkable`):

```python
@runtime_checkable
class PersonaBackend(Protocol):
    backend_id: str

    # Core persona CRUD
    def load_persona(self, persona_id: str) -> Persona: ...
    def save_persona(
        self, persona_id: str, persona: Persona, *, overwrite: bool = False
    ) -> None: ...
    def list_personas(self) -> list[str]: ...
    def exists(self, persona_id: str) -> bool: ...
    def clone(
        self,
        source_id: str,
        target_id: str,
        overrides: dict | None = None,
    ) -> None: ...

    # Snapshot trio (capability-gated; raises NotImplementedError when
    # capabilities().supports_snapshot is False)
    def snapshot(self, persona_id: str, label: str | None = None) -> str: ...
    def restore(self, persona_id: str, snapshot_id: str) -> None: ...
    def list_snapshots(self, persona_id: str) -> list[PersonaSnapshot]: ...

    # Capabilities
    def capabilities(self) -> PersonaCapabilities: ...
```

Where `Persona`, `PersonaSnapshot`, and `PersonaCapabilities` are frozen dataclasses defined in `atomic_agents.persona.types`. The `backend_id` class attribute is a stable string identifier (e.g., `"filesystem"`, `"postgres"`, `"saas"`) used by the registry.

## Canonical types

```python
@dataclass(frozen=True, slots=True)
class Persona:
    identity: str       # contents of IDENTITY.md body
    soul: str           # contents of SOUL.md body
    user: str           # contents of USER.md body
    version: int        # monotone counter; starts at 1 on create, increments on save
    created_at: str     # ISO 8601 timestamp of when this version was saved
    label: str | None = None  # optional human-readable label for this version


@dataclass(frozen=True, slots=True)
class PersonaSnapshot:
    snapshot_id: str      # backend-issued unique identifier
    persona_id: str       # the persona this snapshot belongs to
    created_at: str       # ISO 8601 timestamp of snapshot capture
    persona: Persona      # full Persona record captured at snapshot time
    label: str | None = None  # optional label supplied at snapshot time


@dataclass(frozen=True, slots=True)
class PersonaCapabilities:
    supports_save: bool         # True if save_persona is implemented
    supports_clone: bool        # True if clone is implemented
    supports_snapshot: bool     # True if the snapshot trio is implemented
    supports_subscribe: bool    # True if change-notification subscriptions are supported
    durable: bool               # True if records persist across process restart
    supports_templates: bool    # True if backend provides read-only persona templates
```

## Capabilities semantics

Each field in `PersonaCapabilities` declares what the backend supports. Conformance tests assert claim-vs-behavior parity.

- **`supports_save`**: True if `save_persona()` is implemented and persists records. False for read-only backends (e.g., a pip-installable persona-template library that operators install but cannot write to).

- **`supports_clone`**: True if `clone()` is implemented. `FilesystemPersonaBackend` sets this True.

- **`supports_snapshot`**: True if the snapshot trio (`snapshot()`, `restore()`, `list_snapshots()`) is fully implemented. `FilesystemPersonaBackend` sets this **False in PR 1**; the capability flips to True in PR 3 when the filesystem snapshot trio lands. Backends with `supports_snapshot=False` MUST raise `NotImplementedError` on all three snapshot methods.

- **`supports_subscribe`**: True if the backend supports change-notification subscriptions (reserved for v1.1+; all v1 backends set False).

- **`durable`**: True if the backend persists records across process restart. Filesystem and database backends are durable; in-memory test-fixture backends are not.

- **`supports_templates`**: True if the backend provides read-only persona templates (e.g., a pip-installable persona marketplace package). **Retires spec/24 line 436's `TemplateProfileBackend` reservation (D5)** -- templates are PersonaBackend's domain because they are persona-centric: operators bring their own model and tools to a persona. All v1.0 backends set this False; the marketplace surface is v1.1+.

## D2a: Ownership rule

The `<agent>/persona.link.md` file is the ownership trigger. Its presence signals that PersonaBackend owns this agent's persona; its absence means the legacy three-file layout is in effect.

- **`<agent>/persona.link.md` present, `<agent>/persona/IDENTITY.md` absent**: PersonaBackend owns the agent's persona. `AgentProfileBackend.load_profile()` reads `persona_identity / soul / user` from `persona_backend.load_persona(persona_id)` and populates the AgentProfile snapshot fields.

- **`<agent>/persona.link.md` absent, `<agent>/persona/IDENTITY.md` present** (or absent): Legacy layout. All existing AgentProfile behavior preserved byte-identical. PersonaBackend is not consulted.

- **Both present** (D2a conflict): `AgentProfileBackend.load_profile()` raises `PersonaOwnershipConflict`. Operators must choose one layout: remove `persona.link.md` to keep the legacy layout, or remove `persona/{IDENTITY,SOUL,USER}.md` to use the shared-persona reference.

The `PersonaOwnershipConflict` raise site lands in PR 2 (composition wiring). PR 1 ships only the exception definition. The `is_persona_externally_owned(agent_id) -> bool` Protocol method is added to `AgentProfileBackend` in PR 2 (D-ER-1) so the framework-level bootstrap path can check ownership without importing PersonaBackend.

## D-ER-4: `persona.link.md` format

The `persona.link.md` file uses a YAML code block following the same `model.md` / `mandates.md` / `policy.md` / `judges.md` pattern (spec/03 embedded-YAML convention):

```yaml
kind: shared
persona_id: customer-support-v3
```

Two scalar fields:

- `kind`: must be `shared` in v1. Future values (`template`, `git`, `vault`) are reserved.
- `persona_id`: must match the charset `[a-zA-Z0-9_.+@-]+` (same pattern as storage).

The single-scalar `persona_id: shared:customer-support-v3` format was rejected at /plan-eng-review (colon in the value violates D4's persona_id charset -- `shared:customer-support-v3` would fail `_validate_persona_id` because `:` is not in the allowed set). The two-field format keeps `persona_id` as a clean identifier.

The `persona_link_md.py` parser (D-ER-3) raises `PersonaLinkInvalid` when:
- The YAML code block cannot be parsed.
- The `kind:` field is missing or its value is not `"shared"`.
- The `persona_id:` field is missing.
- The `persona_id:` value fails the charset pattern.

The parser lands in PR 2. PR 1 ships only the exception definition.

## `persona_id` charset rule

```
[a-zA-Z0-9_.+@-]+
```

Validated at the API boundary on **every method that accepts a `persona_id`**. Refuses:

- Path-traversal tokens: `..`, `/`, `\`
- Control characters: `\x00`-`\x1f` and `\x7f`
- Newlines: `\n`, `\r` (covered by control-char check)
- Leading dot: `.hidden` guards against `.personas/` internal directory access
- Empty string

Examples of valid `persona_id` values: `customer-support-v3`, `analyst.v2`, `ops@fleet`, `coach-v1+tone`. Mirrors `PolicyBackend`'s `_AGENT_NAME_PATTERN` for cross-Protocol uniformity (D4).

## Storage layout

Personas live under a hidden directory (mirrors the `.snapshots/` pattern from spec/24) so the namespace is structurally separate from agents:

```
<personas_root>/<persona_id>/IDENTITY.md   -- identity body
<personas_root>/<persona_id>/SOUL.md       -- soul body
<personas_root>/<persona_id>/USER.md       -- user body
<personas_root>/<persona_id>/metadata.json -- schema_version (int = 1), version (int), label (str or null), created_at (ISO 8601 string)
```

The `metadata.json` sidecar carries the structured metadata fields that are not naturally represented as markdown:

```json
{
  "schema_version": 1,
  "version": 1,
  "label": "post-tone-rewrite",
  "created_at": "2026-05-26T12:00:00Z"
}
```

`schema_version` is `1` for all records written by this release. Future schema changes will increment this value; `load_persona` raises `PersonaCorrupted` when it encounters an unsupported schema version. Records without `schema_version` default to `1` (backward-compat for pre-PR1-adversarial-fix records).

`label` is `null` when not supplied. `created_at` is an ISO 8601 timestamp string (the backend writes it; callers supply it as part of the `Persona` dataclass fields).

The default `personas_root` is `<scope_root>/.personas/` resolved by `get_default_persona_backend`. For the filesystem backend, the `.personas/` hidden directory keeps `list_agents()` from discovering personas when it skips dot-entries.

## Snapshot storage layout

Persona snapshots live nested inside the persona's own directory under a
dot-prefixed `.snapshots/` subdirectory:

```
<personas_root>/<persona_id>/.snapshots/<snapshot_id>/IDENTITY.md
<personas_root>/<persona_id>/.snapshots/<snapshot_id>/SOUL.md
<personas_root>/<persona_id>/.snapshots/<snapshot_id>/USER.md
<personas_root>/<persona_id>/.snapshots/<snapshot_id>/metadata.json
```

The dot-prefix ensures `list_personas()` (which filters dot-entries when
walking `<personas_root>/`) does not surface snapshot directories as
personas.

Cross-persona isolation is enforced geometrically by the path shape: a
snapshot record always resides under its parent persona's directory, so
a snapshot_id from persona A cannot reference data under persona B's
storage. `restore(persona_id, snapshot_id)` MUST additionally verify the
resolved snapshot path is under `<personas_root>/<persona_id>/` (via
`resolved.relative_to(persona_dir.resolve())`) as defense-in-depth
against snapshot_id charset edge cases.

`snapshot()` builds the snapshot directory atomically: all four files
are written to a sibling temp directory, then the directory is renamed
into place. The 20-iteration retry bound from MUST #5 applies here too.

## Registry primitives

Full signatures matching `atomic_agents/persona/backend.py`:

```python
def register_persona_backend(backend_id: str, cls: type[PersonaBackend]) -> None: ...
def unregister_persona_backend(backend_id: str) -> None: ...
def get_persona_backend(backend_id: str) -> type[PersonaBackend]: ...
def list_persona_backends() -> list[str]: ...
def get_default_persona_backend(scope_root: Path) -> PersonaBackend: ...
```

- `register_persona_backend`: silently replaces on collision (matches Lock / Log / Profile / LLM / Judge / Policy pattern). The `_bootstrap_filesystem()` call at module bottom is idempotent (checks presence first).
- `unregister_persona_backend`: idempotent (no-op if absent). Used by conformance fixtures for register-in-setup + unregister-in-teardown hygiene (mirrors Policy arc D9 fold #3).
- `get_persona_backend`: returns the registered class. Raises `BackendNotRegistered` when the id is not in the registry.
- `list_persona_backends`: returns registered backend ids in lexicographic order.
- `get_default_persona_backend`: honors `ATOMIC_AGENTS_PERSONA_BACKEND` env var (default `"filesystem"`). Unknown values raise `BackendNotRegistered` with credential-redacted error messages. When `ATOMIC_AGENTS_PERSONA_BACKEND_URL` is set and the backend is `"filesystem"`, the URL is passed directly to `make_filesystem_persona_backend_from_url`.

Convention: `backend_id: str` + `cls: type[PersonaBackend]` (mirrors the 9 prior backends). The design doc originally used `register_persona_backend(scheme, factory)`; the implementation uses the established convention.

### URL factory (filesystem only)

```python
def make_filesystem_persona_backend_from_url(url: str) -> FilesystemPersonaBackend: ...
```

The filesystem URL factory handles `filesystem:///absolute/path` URLs. See §"URL factory" below. There is no process-local URL factory registry: `get_default_persona_backend` dispatches to the filesystem URL factory directly, matching the `PolicyBackend` pattern (Policy also has no URL factory registry).

## Operator override surface

**Environment variables:**

- `ATOMIC_AGENTS_PERSONA_BACKEND`: backend id string (default `"filesystem"`). Follows the established `ATOMIC_AGENTS_<PRIMITIVE>_BACKEND` pattern.
- `ATOMIC_AGENTS_PERSONA_BACKEND_URL`: optional `filesystem:///path` URL. When set alongside `ATOMIC_AGENTS_PERSONA_BACKEND=filesystem`, passed directly to `make_filesystem_persona_backend_from_url` to override the default `<scope_root>/.personas` root.

**Constructor kwarg (PR 2):**

The `AtomicAgent(..., persona_backend=...)` constructor kwarg is wired in PR 2. When supplied, it always wins over the env var (programmatic path beats environment). PR 1 ships only the env var surface; the kwarg threading lands in PR 2.

**Per-runner kwargs (PR 2):**

`OutcomeRunner`, `EvalRunner`, and `DreamRunner` gain `persona_backend=...` constructor kwargs in PR 2, threading through to internal sub-agents.

## `delegate.py` threading (D-ER-2)

`delegate.py` threads `persona_backend` ONLY when the operator supplied the backend explicitly via the `AtomicAgent(..., persona_backend=...)` kwarg. When the backend was resolved from the framework default (via `get_default_persona_backend(scope_root)`), the delegate constructs its own default at ITS scope — preventing the coordinator's `<agents_root>/.personas/` directory from being silently consulted by a cross-vault delegate. Mirrors the `PolicyBackend` precedent at `atomic_agents/agent.py:401` (`_policy_backend_was_explicit`).

Rationale: persona is per-agent semantic context. A delegate's persona is the delegate's own identity; it should not inherit from its coordinator's PersonaBackend by accident. This mirrors the Mandate precedent (per-agent scoped, delegate.py deliberately NOT threaded) and is distinct from Policy (fleet-scoped, always threaded) and AgentProfile (fleet-scoped, always threaded). The distinction between explicit-kwarg threading and default-resolved threading is the same boundary PolicyBackend draws: an operator who consciously passes `persona_backend=my_shared_backend` signals intent to share; an operator who relies on the default signals per-agent isolation.

## Snapshot trio shape (PR 3)

```python
def snapshot(self, persona_id: str, label: str | None = None) -> str: ...
def restore(self, persona_id: str, snapshot_id: str) -> None: ...
def list_snapshots(self, persona_id: str) -> list[PersonaSnapshot]: ...
```

PR 1 ships the Protocol methods with `capabilities().supports_snapshot=False`. `FilesystemPersonaBackend` raises `NotImplementedError` on all three. PR 3 implements the trio and flips the capability to True.

Snapshot ids are backend-issued (monotonic or sortable for chronological ordering). Cross-persona isolation is enforced at the storage layer: a snapshot id from persona A MUST raise `PersonaSnapshotNotFound` when restored to persona B.

## Cross-Protocol composition (PR 2 preview)

PR 2 wires the full composition path:

- `AgentProfileBackend.load_profile()` learns to delegate persona reads to `persona_backend.load_persona(persona_id)` when one is configured AND the agent's `persona.link.md` names a `persona_id`. The `is_persona_externally_owned(agent_id) -> bool` Protocol method (D-ER-1) is added to `AgentProfileBackend` in PR 2.

- `AgentProfileBackend.save_profile()` ignores `profile.persona_identity / soul / user` when `is_persona_externally_owned()` returns True (D2a). Writes go through `persona_backend.save_persona()` only.

- `AgentProfileBackend.snapshot()` and `.restore()` skip persona fields when PersonaBackend owns the agent's persona (D3). Persona has its own snapshot history via PersonaBackend.

The bootstrap path in `AtomicAgent.__init__` (PR 2): reads `agent_profile_backend.load_profile(agent_id)` first, checks `is_persona_externally_owned()`, and if True calls `persona_backend.load_persona(persona_id)` to repopulate the persona fields before system prompt assembly. AgentProfileBackend does NOT import PersonaBackend (D1a) -- the protocols stay decoupled.

## URL factory

`make_filesystem_persona_backend_from_url` accepts `filesystem:///path` scheme; refuses non-filesystem schemes, netloc, fragments, duplicate query params, and unknown query params. Credentials are redacted from all `ValueError` sites via `_redact_url`.

Valid URL format: `filesystem:///absolute/path/to/personas_root`

Triple-slash convention: `filesystem://` (scheme + empty authority) + `/abs/path` (absolute path starting with `/`).

Refused formats:

- Non-filesystem scheme: `"postgres://..."`, `"sqlite:///..."` raise `ValueError`
- Netloc component: `"filesystem://hostname/path"` raises `ValueError`
- Fragment: `"filesystem:///path#fragment"` raises `ValueError`
- Unknown query params: `"filesystem:///path?unknown=foo"` raises `ValueError`
- Relative path: `"filesystem://relative/path"` raises `ValueError`

## Implementer contract for persona backends

**DRAFT -- finalized at PR 4 lock based on what the implementation pins.**

A backend that implements the `PersonaBackend` Protocol commits to the contract below. The reference `FilesystemPersonaBackend` ships in #62 PR 1; future Postgres / SaaS / git adapters slot in via `register_persona_backend(...)` without forking core.

Implementers MUST:

1. **persona_id charset validation at API boundary.** Every Protocol method validates `persona_id` against `[a-zA-Z0-9_.+@-]+` BEFORE any storage or dict access. Reject path-traversal tokens (`..`, `/`, `\`), control characters, newlines, leading dots, and empty strings with `ValueError`. Reference: `filesystem.py::_validate_persona_id`.

2. **Side-effect-free construction.** Backend `__init__` MUST NOT stat the filesystem, query a database, call an external API, or read any environment variable. The first method call performs lazy initialization. Malformed operator config surfaces on the first method call, not at construction. Preserves the framework's byte-identical-construction promise for the 166 existing `AtomicAgent(...)` test sites.

3. **Capability honesty.** `capabilities() -> PersonaCapabilities` is a contract, not a hint. Backends declaring `supports_snapshot=False` MUST raise `NotImplementedError` on `snapshot()`, `restore()`, and `list_snapshots()`. Backends declaring `supports_save=False` MUST raise on `save_persona()`. Conformance tests gate on capability flags; backends that lie produce silent failures rather than loud refusals.

4. **URL credential redaction across all `ValueError` sites in factory functions.** URL factories and `get_default_persona_backend` error paths MUST NOT echo raw URL credentials. The reference uses `_redact_url` / `_redact_for_error_message` helpers that strip after `://` and truncate. Operators may accidentally paste `postgres://user:password@host/db` into env vars.

5. **Group-atomic save on `save_persona`.** The entire persona record (all four files) is written as a group-atomic operation so a mid-save crash leaves no partial state. For the `overwrite=False` path, `mkdir(exist_ok=False)` claims the directory exclusively before writing, eliminating the TOCTOU race between an `is_dir()` check and the first write. For the `overwrite=True` path, all four files are written to a sibling temp directory and then the temp directory is renamed into place via a swap-and-delete sequence; if the rename fails, the old record is restored. Concurrent saves on the same `persona_id` with `overwrite=True` are last-writer-wins by documented semantics. The reference implementation bounds the swap retry loop at 20 iterations (sized for 16-thread contention on macOS APFS `ENOTEMPTY` semantics); exhaustion under exceptional contention surfaces `PersonaError`. Alternate backends MAY adopt different retry policies or unbounded retries; the contract is last-writer-wins ON SUCCESS, not a per-call guarantee.

6. **Snapshot id determinism.** Backend-issued snapshot ids MUST be monotonic or sortable (supporting `list_snapshots` returning chronological order). Cross-persona isolation MUST be enforced at the storage layer: restoring snapshot `X` from persona A to persona B MUST raise `PersonaSnapshotNotFound`.

7. **`backend_id` property stable across calls.** The property MUST return the same string across calls and MUST match what `list_persona_backends()` registered the class under.

8. **Snapshot id format.** Filesystem backend snapshot ids MUST use the
   format `snap_<YYYY-MM-DDTHHMMSS>_<12hex>` where `<12hex>` is
   `secrets.token_hex(6)` (48-bit random tail). This matches the
   AgentProfileBackend snapshot id format documented in spec/24
   Implementer Contract #8. The timestamp component uses
   `strftime('%Y-%m-%dT%H%M%S')` without a timezone suffix, matching the
   project-wide AgentProfile convention. Cross-Protocol uniformity allows
   a shared path-security validator (`_validate_snapshot_id`) and
   equivalent collision resistance across both snapshot Protocols.
   Database-backed and other non-filesystem backends MAY use their native
   id-generation schemes provided ids remain monotonic or sortable (MUST
   #6) and collision probability is at least as low as 48-bit random.

   Snapshot `metadata.json` MUST contain the following keys:

   - `snapshot_id` (str): the snapshot's own id
   - `persona_id` (str): the persona this snapshot belongs to
   - `label` (str | None): operator-supplied label, null when absent
   - `created_at` (str): ISO 8601 timestamp with timezone

   Additional keys are permitted; consumers MUST NOT assume they are
   absent.

Mark this section "DRAFT -- finalized at PR 4 lock based on what the implementation pins."

## Exceptions

All persona exceptions live in `atomic_agents.exceptions` (D-PI-1) and are re-exported from `atomic_agents.persona` for ergonomic access:

- `PersonaError`: base class for all persona subsystem errors.
- `PersonaNotFound`: `load_persona(persona_id)` called with an id the backend does not know about.
- `PersonaExists`: `save_persona()` or `clone()` refused to overwrite an existing persona (no-overwrite default; `save_persona(..., overwrite=True)` bypasses this).
- `PersonaCorrupted`: the persona directory exists but its contents are corrupt or structurally invalid. Raised by `load_persona` when `metadata.json` is unparseable JSON, is missing a required key (`version`, `created_at`), uses an unsupported `schema_version`, or a body file contains non-UTF-8 bytes. Distinct from `PersonaNotFound` -- the record EXISTS but cannot be read.
- `PersonaSnapshotNotFound`: `restore(persona_id, snapshot_id)` referenced an unknown snapshot or a snapshot belonging to a different persona.
- `PersonaOwnershipConflict`: both `<agent>/persona.link.md` and `<agent>/persona/IDENTITY.md` exist at agent construction (D2a). Raised by profile backends in PR 2.
- `PersonaLinkInvalid`: `persona.link.md` is malformed or the `persona_id:` value fails the charset pattern. Raised by the `persona_link_md.py` parser in PR 2 (D-ER-3).

Cross-module placement matches the convention used by `AgentProfileNotFound`, `ToolNotInRegistry`, etc. `PersonaOwnershipConflict` is raised by profile backends (outside the `persona/` module); `PersonaLinkInvalid` is raised by the parser (outside the `persona/` module). Both would create import cycles if they lived in `persona/types.py`.

## Spec/24 cross-reference

**spec/24 line 436 `TemplateProfileBackend` reservation is explicitly retired by D5.** The retirement is documented in this spec/33 RFC (PR 1). The actual line removal from spec/24 lands in PR 4 per the design doc. `supports_templates` in `PersonaCapabilities` is the canonical home for "starter personas / canned agent configs" -- templates are persona-centric, not config-centric.

## Out of scope

- **`AtomicAgent` wiring** -- shipped in PR 2 (`AtomicAgent(..., persona_backend=...)` kwarg + per-runner kwargs + `doctor.check_persona_backend` + `is_persona_externally_owned()` Protocol method on `AgentProfileBackend`).
- **Shared-persona consumption** -- shipped in PR 3 (`persona.link.md` parser, shared-persona resolution in `AgentProfileBackend.load_profile()`, persona-only snapshot CLI).
- **Filesystem snapshot trio implementation** -- shipped in PR 3 (capability flips to `supports_snapshot=True`).
- **A/B persona testing** -- deferred to v1.1 as `PolicyBackend.get_effective_persona()` extension (D7).
- **`persona.link.md` parser** -- deferred to PR 2 (D-ER-3). PR 1 ships only the `PersonaLinkInvalid` exception definition.
- **`AtomicAgent.is_persona_externally_owned()`** -- the `is_persona_externally_owned(agent_id) -> bool` method on `AgentProfileBackend` Protocol (D-ER-1) lands in PR 2.
- **SQLite `agents.persona_id` column migration** -- schema v1 to v2 migration in `profile/sqlite.py`, forward-only upgrade routine. Ships in PR 2 (D1a).

## References

- `docs/spec/03-file-formats.md` -- the embedded-YAML-in-markdown convention `persona.link.md` follows
- `docs/spec/06-multi-agent-projects.md` -- project-root and scope_root resolution patterns
- `docs/spec/24-agent-profile-backend.md` -- AgentProfile Protocol; `persona_identity / soul / user` fields; spec/24 Decision 6 (`agent_mode` ignore-on-save pattern that D1+D6 extend)
- `docs/spec/27-doctor.md` -- extended with `check_persona_backend` in PR 2
- `docs/spec/32-policy-backend.md` -- Policy Protocol (sibling; A/B persona deferred to v1.1 PolicyBackend extension per D7)
- `~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-design-20260522-134508.md` -- full architectural rationale, 9 locks (D1-D7 + D1a + D2a + D-ER-1 through D-ER-4 + D-PI-1)
- #62 -- the umbrella issue

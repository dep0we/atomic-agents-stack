# 25 — ToolRegistryBackend Protocol

**Status:** **DRAFT** at #64 PR 1 — locks at #64 PR 4.
**Origin:** [#64](https://github.com/dep0we/atomic-agents-stack/issues/64).
**Shipping plan across four PRs:** PR 1 (Protocol scaffolding + `FilesystemToolRegistryBackend` reference impl + conformance suite + DRAFT spec — **this PR**), PR 2 (wire `AtomicAgent.__init__` + per-runner kwargs on OutcomeRunner / EvalRunner / DreamRunner / delegate.py + `doctor.check_tool_registry_backend` coherence check), PR 3 (second reference impl — `SQLiteToolRegistryBackend` — with `install` / `uninstall` flipped True, parametrized conformance suite, plan-subagent-vetted-before-implementation, Step 11 adversarial mandatory), PR 4 (spec LOCKED + `Implementer contract for registry-backed tool backends` documented + README / CLAUDE.md status flip to "**seven backend protocols shipped**").

## Overview

Today an Atomic Agent's tool catalog is split across two paths: `AtomicAgent.__init__` walks `<agent>/skills/<name>/SKILL.md` for skills (then the operator's programmatic `tools=ToolRegistry()` kwarg registers custom callable tools), and the MCP discovery overlay at agent.py:2337 registers any MCP-server-provided tools at call time. There is **no on-disk discovery path for custom tools today**: every operator-defined tool lives only in Python code the operator writes to construct a `ToolRegistry`.

This works on a single host where the operator is the developer. It **breaks** the moment the deployment shape becomes:

- **Plugin marketplace.** A SaaS UI lists "available tools" for an agent's owner to enable; the framework needs a catalog API, not a Python registration call.
- **Version-pinned tools.** "Pin `query_database` to version `1.2.3`" needs a versioning surface; today's `ToolRegistry` has no version field.
- **SaaS multi-tenant tool catalogs.** "Tenant A sees a different tool catalog than Tenant B" needs per-tenant scoping; today's filesystem walk is per-agent-directory.
- **Audit / approval flows on install.** "Operator approves before a tool can be installed for this agent" needs an `install()` Protocol seam with an atomic safety check.
- **Sandboxed validation.** "Validate this tool's handler in an isolated process before letting it run live" needs a `validate()` Protocol seam that distinguishes static checks from runtime sandbox.
- **PyPI / git / company-internal HTTP discovery.** All require a catalog Protocol — the framework cannot guess the operator's source-of-tools.

`ToolRegistryBackend` is the seventh open Protocol in the protocol-pattern series alongside the shipped `MemoryBackend` (spec/20), `LLMBackend` (spec/31), `JudgeBackend` (spec/28), `LockBackend` (spec/21), `LogBackend` (spec/22), and `AgentProfileBackend` (spec/24). It is the **discovery-layer** seam for tools — the existing in-memory `ToolRegistry` (`atomic_agents/tools.py:149`) stays as the LLM-tool-loop dispatch class. Sealing the discovery layer unblocks plugin marketplaces, version-pinned tools, multi-tenant catalogs, audit flows, and the eventual sandboxed-execution shape.

The Protocol is **not** a generic plugin-loading API. It is the minimal contract the framework needs to satisfy the markdown-as-config aesthetic (CLAUDE.md §7) and the audit trail invariant (CLAUDE.md §5). Backends that meet this contract participate fully in agent tool discovery without forking core.

## Module layout

```
atomic_agents/registry/
├── __init__.py        # registry: register_tool_registry_backend / get_tool_registry_backend / list_tool_registry_backends + get_default_tool_registry_backend factory + _redact_for_error_message helper
├── types.py           # canonical types: ToolRef, ToolRegistryCapabilities, ValidationResult
├── backend.py         # ToolRegistryBackend Protocol contract
└── filesystem.py      # FilesystemToolRegistryBackend reference implementation
```

Mirrors `atomic_agents/profile/{__init__.py, types.py, backend.py, filesystem.py}` and `atomic_agents/logs/{__init__.py, types.py, backend.py, filesystem.py}`. The split into `types.py` separate from `backend.py` matches the precedent — canonical types ship without pulling in the Protocol contract or any reference implementation.

The package name is `registry` (not `tools` — that's the existing in-memory dispatch module — nor `tool_registry` — single-word + lowercased package names match the established `profile` / `logs` / `locks` pattern). The PR 3 sibling for the alternate backend lands at `atomic_agents/registry/sqlite.py` alongside the filesystem reference.

## Load-bearing design decisions

These decisions are surfaced by reading the actual tool dispatch implementation (`agent.py:2685`, `tools.py:149`) + the locked spec/24 spec + the open #64 issue body, and pressure-tested against the recurring trap shapes from #60/#61/#63. **Each has a "Why" that future contributors and alternate backend authors need before changing the shape.**

### Decision 1: `ToolRegistryBackend` owns the *discovery layer*, NOT the in-memory `ToolRegistry`

`atomic_agents/tools.py:ToolRegistry` (line 149) is the per-agent in-memory tool dispatcher used during `call()` — `register()` / `unregister()` / `execute()` / `to_anthropic_definitions()` / `to_openai_definitions()`. **It stays.** `ToolRegistryBackend` lives one level above: it's the **catalog** (what tools are available to install/load) that produces `ToolDefinition` instances which then get registered into the existing in-memory `ToolRegistry` at agent construction. The two seams compose:

```
backend.list_tools() → list[ToolRef]              # discovery
backend.load_tool(name) → ToolDefinition          # materialization
agent.tool_registry.register(td)                  # in-memory dispatch (unchanged)
```

**Why:** the in-memory `ToolRegistry` is the LLM-tool-loop surface — replacing it would touch every provider format builder (`to_anthropic_definitions`, `to_openai_definitions`), the multi-turn loop's `tool_registry.execute()` call site at agent.py:930/1137/2685, and the MCP overlay at agent.py:2344. Massive blast radius for no benefit. The discovery layer is the actual hardcoded-filesystem seam that needs lifting. This is the same relationship `JudgeBackend` has to the in-memory judge dispatch path — the Protocol covers *which judges exist*, not the in-memory invocation machinery.

The pattern parallels `AgentProfileBackend.load_profile` → fields-on-the-profile → `AtomicAgent` consumes the fields (spec/24 Decision 1). Discovery surfaces meta; consumer code keeps its dispatch shape.

### Decision 2: Strict separation of skill ownership — `AgentProfileBackend` owns per-agent **mounted** skills, `ToolRegistryBackend` reserves the **catalog** skill surface

`AgentProfileBackend.list_skills(agent_id) -> list[SkillManifest]` is the **agent-mounted** surface — "what skills does this agent's IDENTITY currently subscribe to?" It returns objects with `skill_dir`, `skill_md_path`, body line counts — manifests of the **concrete files this agent loads at init**. Spec/24 Decision 2 + Decision 8 locked this shape (and ship reality confirms it across all four #63 PRs).

`ToolRegistryBackend` reserves `list_skills_catalog() -> list[ToolRef]` for the **catalog** surface — "what installable skills does this registry publish?" Returns metadata-only refs reusing `ToolRef` as the canonical type (skill catalog entries share the same shape — name + description + classification + version + source). Pure metadata, NO `skill_dir`. The relationship parallels pip's perspective: `pip list` (mounted) vs. `pip search` / index browsing (catalog). When the catalog surface lands, a dedicated `SkillRef` MAY be introduced if the shapes diverge — until then, `ToolRef` is the single canonical metadata type across both tool + skill catalog entries.

**PR 1 ships ONLY the tool surface** (`list_tools` / `load_tool` / `validate`). Skill catalog is a reserved capability flag (`supports_skills_catalog: bool`) on `ToolRegistryCapabilities`; filesystem returns False, PyPI / Git backends would flip True in their own arc. Spec/24 §"Reserved future capabilities" `save_skill` reservation stays intact — it's the *mounting* primitive on the profile side.

**Why this carve-out (vs. issue body's literal "list_skills / load_skill" ask):**

1. The issue body was written before spec/24 locked. The locked spec/24 owns per-agent skill listing — flipping it now means re-opening a locked spec.
2. Spec/18's progressive-disclosure principle (CLAUDE.md §6) is a per-agent property — "what this agent declares it knows about" is identity-layer, not catalog-layer. The catalog is upstream of identity.
3. Keeping the catalog surface reserved (not shipped) means PR 1 stays scope-disciplined; future expansion has a clean landing pad in `ToolRegistryCapabilities.supports_skills_catalog`.
4. A future SaaS deployment will need BOTH — `AgentProfileBackend` answering "what does this agent currently know about?" AND `ToolRegistryBackend` answering "what's available to teach this agent?" Merging the two now means picking a wrong shape and re-doing it later.

**Operator-facing consequence:** today operators copy `tools/<name>.{md,py}` + `skills/<name>/SKILL.md` into the agent dir manually; PR 1 lifts the `tools/` discovery to a Protocol; skills/ discovery still walks the filesystem via `discover_skills(agent_root)` until a future ToolRegistryBackend ships catalog skills. Both home and SaaS shapes work in both regimes.

### Decision 3: MCP server discovery stays in its own module — `MCPServerSpec` is NOT a `ToolRef`

MCP servers are **processes** with subprocess lifecycle, env-var-resolved credentials, transport selection, per-call asyncio overhead. Tools are **functions** with a JSON schema and a handler. **The two are different invocation models that happen to both show up in agent.tool_registry after MCP discovery.** Conflating their Protocols means:

- `ToolRegistryBackend` implementers would need to model subprocess specs that aren't relevant to filesystem-tool catalogs.
- The `mcp.md` → `MCPServerSpec` security boundary (env-var resolution at parse time, the `$VAR` carry-over for save_profile per spec/24 Decision 1) doesn't translate to a registry catalog idea.
- MCP servers scope credentials per-agent in a way that a shared catalog would have to invent re-scoping for.

**Reserved future:** a separate `MCPServerRegistryBackend` Protocol could land in its own arc when the SaaS-tenancy story actually needs it. Documented here in §"Out of scope" + a forward-pointer in spec/19 (existing MCP spec) so future readers find the carve-out. A successor issue (`[backend] MCPServerRegistryBackend — unify MCP server discovery across agents`) will be filed before #64 PR 4 merges.

**Why:** Layers compose; they don't merge (CLAUDE.md §3). The `AgentProfileBackend` already snapshots `mcp_servers: list[MCPServerSpec]` on `AgentProfile` from `mcp_md_raw`; PR 2 wiring routes the MCP pool's `_specs` from `self._profile.mcp_servers` (already in place after #63 PR 2 at `agent.py:2018` + `agent.py:2337`). No additional indirection needed; the Profile backend covers the per-agent MCP server list; the eventual MCP-registry layer covers the cross-agent install/audit story.

### Decision 4: Version field on `ToolRef` ships now, semantics deferred behind capability flag

`ToolRef.version: str | None = None` ships in PR 1's canonical types. `FilesystemToolRegistryBackend` always sets `version=None` (filesystem layout has no version semantics today). `ToolRegistryBackend.load_tool(name: str) -> ToolDefinition` does NOT accept a `version` parameter — adding it pre-emptively would lock a wrong shape (which of pip-style / npm-style / git-style wins?). `ToolRegistryCapabilities.supports_versioning: bool` reserves the field.

Documented here in §"Reserved future capabilities" with **pip-style** as the default-target when the flag lands. Rationale: matches the framework's pip-shaped Python ecosystem position. npm-style multi-version-coexistence is explicitly documented as out-of-scope (operators wanting it use multiple registries scoped per-agent, which already works through the per-agent backend instance pattern).

**Why ship the field now, not later:** `to_dict / from_dict` round-trip preservation requires the field on the canonical type from PR 1 — adding it in PR 3 would break the JSON shape forward-compat (same lesson as spec/24 Decision 1's `mcp_md_raw` preservation discipline). Cheap to add now (one Optional field on a frozen dataclass); expensive to retrofit later.

### Decision 5: `ToolRef` is metadata-only; `load_tool` materializes the handler

```python
@dataclass(frozen=True)
class ToolRef:
    name: str                                    # tool name as the LLM will call it
    description: str                             # for the system-prompt advert + LLM-routing
    classification: str | None = None            # ActionClass string (#112)
    version: str | None = None                   # reserved (Decision 4)
    source: str = ""                             # backend-specific origin marker
```

NO `handler`, NO `input_schema`. `list_tools()` returns enough to **advertise** tools (system prompt + judge dispatch) without paying the cost of importing every tool's Python module. `load_tool(name) -> ToolDefinition` is the lazy-materialization path that runs the (potentially expensive) import + handler wiring. This mirrors:

- Spec/18 progressive disclosure (skills metadata in prompt, bodies on demand).
- `SkillManifest` → `load_skill_body()` separation (locked in spec/24 Decision 2).
- The framework's "pay context tokens for capability awareness, not capability content" principle (CLAUDE.md §6).

**Why:** the filesystem reference impl walks `tools/<name>.md` for metadata (cheap, just markdown parsing) and ONLY runs `importlib.util.spec_from_file_location` when `load_tool(name)` is called. A 50-tool agent under filesystem pays no Python import cost at construction time. The current `agent.py` does NOT load tool implementations from `tools/<name>.py` at all today — custom tools are registered programmatically by the operator. PR 1 ships the filesystem reference to ALSO discover + materialize `tools/<name>.py` modules in the spirit of the issue's "tools/<name>.md descriptor + tools/<name>.py implementation" framing. **This expands operator surface (a new discovery path), so PR 1's filesystem backend MUST behave as a no-op when `tools/` dir is empty or absent.** Existing tests must not see new registrations.

### Decision 6: `ToolRegistryBackend.validate(name)` is a static check, NOT a live invocation

The issue body lists `validate(name)` as an optional capability — "sandbox check before live load." PR 1 ships it as a **static** check: parses the descriptor, attempts the import in a `try/except`, returns `ValidationResult(ok: bool, errors: list[str], warnings: list[str])`. **Does NOT run the handler.** Sandboxed-execution-as-validation is reserved for a future capability flag (`supports_sandbox_validate`). The filesystem backend's `validate()` checks:

1. Descriptor parses (frontmatter present, YAML valid, root is dict, `input_schema` is dict).
2. Handler module imports without error.
3. Handler is callable (the module exposes a callable named `handler`).
4. Classification (if present) is a valid `ActionClass` enum value.

Soft warnings cover the hygiene issues that don't break dispatch: missing description, missing classification (the runtime falls back to `external_side_effect` per spec/28).

**Why:** running operator handlers as a side-effect of `validate()` violates least-astonishment — `validate` is an audit-time call, not a runtime call. The runtime safety story is `ToolRegistry.execute()` and the judge layer (spec/28), which is the right place for sandboxed dispatch. Decisions about sandbox process model (subprocess? container? wasm?) are too unsettled to lock in #64 PR 1.

### Decision 7: `install` / `uninstall` are capability-gated, ABSENT from filesystem backend

Filesystem backend declares `supports_install=False`. `install(source, version)` / `uninstall(name)` raise `NotImplementedError`. Operators on filesystem do the equivalent by editing `<agent>/tools/<name>.{md,py}` directly (today's UX, preserved). PyPI / Git / Remote backends would flip `supports_install=True` in their own arc.

**Why:** filesystem-as-source-of-truth (CLAUDE.md §1) — the filesystem backend doesn't own install semantics because the operator's text editor + `cp` are the install primitive there. Inventing a filesystem-backend `install()` would mean either (a) downloading from a URL and writing to disk (network call from a "filesystem" backend — surprising), or (b) copying from another path (a shell `cp` wrapper — superfluous). Capability flag honestly signals the shape; future backends do the work.

### Decision 8: `ToolNameCollision` semantics survive the indirection — backend tools register AFTER operator tools, with `allow_overwrite=False`

The legacy `ToolNameCollision` (raised by `ToolRegistry.register()` with default `allow_overwrite=False`) keeps its semantics. PR 2 wiring path:

1. `AtomicAgent.__init__` calls `self.tool_registry_backend = tool_registry_backend or get_default_tool_registry_backend(self.agent_root)`.
2. After in-memory `self.tool_registry = tools if tools is not None else ToolRegistry()` is constructed (agent.py:243), PR 2 inserts a loop: `for ref in self.tool_registry_backend.list_tools(): td = self.tool_registry_backend.load_tool(ref.name); self.tool_registry.register(td)`.
3. If the operator ALSO passes `tools=ToolRegistry()` with pre-registered tools, the order is: operator's tools register first (operator intent wins on collisions), then backend tools register with `allow_overwrite=False` (collisions surface loudly as `ToolNameCollision`).
4. MCP discovery at agent.py:2337 — unchanged. MCP tools register with `allow_overwrite=True` (existing semantics). MCP tool names ALWAYS namespace as `server__tool`, so collisions with backend-loaded tools are vanishingly rare and indicate a real conflict.

**Why:** preserves all 96 `AtomicAgent(...)` test sites' behavior when filesystem backend's `tools/` dir is empty (backend yields zero `ToolRef`s; the wiring loop is a no-op). New behavior (backend yields tools) only activates when operators populate `tools/` with descriptor+handler files. **Surface change is opt-in via filesystem layout, not opt-in via API.**

### Decision 9: Per-agent backend instance (NOT process-shared) for filesystem; SaaS / catalog backends MAY be process-shared

`FilesystemToolRegistryBackend(agent_root)` is constructor-scoped per-agent — different from `FilesystemAgentProfileBackend` which is rooted at `agents_root`. The difference is structural:

- **Profile** backend is `agents_root`-rooted because agents are siblings (a process loading agent A and agent B uses ONE profile backend instance that resolves both via `load_profile(agent_id)`).
- **Tool registry** backend is `agent_root`-rooted because tools are per-agent (`<agent>/tools/<name>.md` belongs to that agent only; agent A's tool catalog is unrelated to agent B's).

A shared-catalog backend (PyPI / company-internal HTTP) would be process-shared because the catalog IS shared. The constructor signature differs across backend shapes (`agent_root` for filesystem; `package_index_url` for PyPI; `git_remote` for git); the Protocol does NOT prescribe a constructor signature — only the methods. Matches spec/24 §"Reference implementations" precedent.

### Decision 10: Env-var operator surface — `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` + `..._URL` mirroring the established pattern

Operator surface (parallel to spec/22 + spec/24):

- `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` — default `"filesystem"`. Future values: `"sqlite"`, `"pypi"`, `"git"`, `"http"`.
- `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL` — URL / connection string for non-filesystem backends.
- `AtomicAgent(..., tool_registry_backend=...)` constructor kwarg ALWAYS wins (programmatic path).
- Per-runner threading on `OutcomeRunner` / `EvalRunner` / `DreamRunner` / `delegate.py` — same kwarg-drop-trap discipline as #61 + #63.
- Credential redaction in `BackendNotRegistered` error messages (the `_redact_for_error_message` helper).

Single-host operators wanting SQLite (PR 3+) flip `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND=sqlite` and get a working default (the SQLite backend will default to `<agent_root>/.tools.db` when URL is absent, mirroring spec/24's SQLite default path convention).

## Canonical types

### `ToolRef` — the unit of catalog listing

```python
@dataclass(frozen=True)
class ToolRef:
    name: str                              # tool name as the LLM will call it
    description: str                       # for the system-prompt advert + LLM-routing
    classification: str | None = None      # ActionClass string (#112)
    version: str | None = None             # reserved (Decision 4)
    source: str = ""                       # backend-specific origin marker
```

`ToolRef.to_dict()` / `from_dict()` round-trip is byte-shape preserving for every field.

### `ToolRegistryCapabilities`

```python
@dataclass(frozen=True)
class ToolRegistryCapabilities:
    supports_install: bool                 # False on filesystem (Decision 7)
    supports_uninstall: bool               # False on filesystem (Decision 7)
    supports_versioning: bool              # False on filesystem (Decision 4)
    supports_sandbox_validate: bool        # False on filesystem (Decision 6)
    supports_skills_catalog: bool          # False on filesystem (Decision 2)
    durable: bool                          # True on filesystem (the on-disk dir is durable)
```

Conformance tests assert claim-vs-behavior parity (mirrors `ProfileCapabilities` + `LogCapabilities` precedent).

### `ValidationResult`

```python
@dataclass(frozen=True)
class ValidationResult:
    ok: bool                               # equivalent to `not errors`
    errors: list[str]                      # tool unusable
    warnings: list[str]                    # tool usable but flagged
```

## `ToolRegistryBackend` Protocol surface

```python
@runtime_checkable
class ToolRegistryBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    # Core discovery (always implemented)
    def list_tools(self) -> list[ToolRef]: ...
    def load_tool(self, name: str) -> ToolDefinition: ...
    def validate(self, name: str) -> ValidationResult: ...

    # Capability-gated mutation (MAY raise NotImplementedError)
    def install(self, source: str, version: str | None = None) -> ToolRef: ...
    def uninstall(self, name: str) -> None: ...

    # Reserved skill catalog (capability-gated; both reference impls raise)
    def list_skills_catalog(self) -> list[ToolRef]: ...
    def load_skill_catalog_body(self, name: str) -> str: ...

    def capabilities(self) -> ToolRegistryCapabilities: ...
```

### `list_tools` semantics

- Returns `[]` (NOT raise) when catalog is empty or backing dir is absent.
- Excludes implementation-internal entries: filesystem reference skips hidden files (`.foo.md`), Python module helpers (`_helper.py`, `__init__.py`), and any file lacking the descriptor suffix.
- Order is lexicographic by `name`. Database backends MUST `ORDER BY name`.
- Cheap by construction — descriptor parsing only, no handler import.

### `load_tool` semantics

- Returns a fully-populated `ToolDefinition` (handler + input_schema + description + classification).
- MUST raise `ToolNotInRegistry` when `name` is absent from the catalog. Distinct from `ToolNotRegistered` (in-memory `ToolRegistry.execute` raises that one for LLM-emitted tool_use against an unknown tool).
- MUST raise `ToolDescriptorInvalid` when the descriptor can't be parsed.
- MUST raise `ToolHandlerImportFailed` when the handler module can't be imported.
- MUST validate `name` against path-traversal at the API boundary BEFORE any disk access (spec/25 MUST #1 — locked at PR 4).
- Handlers MUST be re-importable across `load_tool` calls. The filesystem reference impl does NOT populate `sys.modules` — each `load_tool` call produces a fresh module via `importlib.util.spec_from_file_location` + `exec_module`. Operators with side-effecting top-level code in their handler module see the side effect re-fire on every `load_tool`. Backends MAY opt to cache (insert into `sys.modules` under a deterministic qualname), but MUST NOT cache the returned `ToolDefinition` instance — callers may mutate fields before registering. PR 2 wires `load_tool` exactly once per agent construction, so re-import overhead lands only on agent re-construction.

### `validate` semantics

- **Does NOT execute the handler** (Decision 6).
- Returns `ValidationResult(ok, errors, warnings)`.
- MUST NOT raise on missing tool — absent tool surfaces as `ValidationResult(ok=False, errors=["tool 'X' not in registry"], warnings=[])`.

### `install` / `uninstall` semantics

- MAY raise `NotImplementedError` when capabilities flag is False.
- `install` MUST be atomic at the tool level (concurrent `install(source=X)` calls — exactly one wins; the others raise `ToolAlreadyInstalled`). Reference SQLite backend (#64 PR 3) uses `INSERT ... ON CONFLICT(agent_scope, name) DO NOTHING` returning row count.
- `uninstall` MUST be idempotent — uninstalling a name that doesn't exist is a no-op (no exception). Matches in-memory `ToolRegistry.unregister` precedent.
- Both MUST validate `name` against path-traversal at the API boundary.

### `list_skills_catalog` / `load_skill_catalog_body` semantics

- Reserved (Decision 2). Both reference backends in PR 1 raise `NotImplementedError`.
- Future PyPI / git / company-internal-HTTP backends flip `supports_skills_catalog=True` in their own arc.

### `capabilities` semantics

- Returns `ToolRegistryCapabilities`. Conformance suite enforces claim-vs-behavior parity (mirrors `ProfileCapabilities` / `LogCapabilities`).

## Reference implementations

### `FilesystemToolRegistryBackend`

Conforms to the Protocol with the constructor signature `FilesystemToolRegistryBackend(agent_root)`.

- `agent_root` is the agent's directory (NOT `agents_root`). Tools live under `<agent_root>/tools/`.
- `agent_root` MAY not exist at construction (test fixtures may build the backend before populating the dir). `list_tools()` returns `[]` for missing / empty dirs.
- Descriptor format: `<agent_root>/tools/<name>.md` with YAML frontmatter:

  ```text
  ---
  name: query_database
  description: Run a read-only SQL query against the analytics warehouse.
  classification: read_only
  input_schema:
    type: object
    properties:
      query:
        type: string
        description: The SQL query to run.
    required: [query]
  ---

  # Operator notes (ignored by the framework)
  ```

- Handler convention: `<agent_root>/tools/<name>.py` exposing a callable named `handler` with signature `handler(input: dict) -> Any`.
- `name` validated against path-traversal at API boundary — refuses `/`, `\\`, `..`, leading `.`.
- Helper file exclusion: stems starting with `_` or named `__init__` skipped from `list_tools`.
- `list_tools()` skips malformed descriptors silently (operator triages via `validate(name)`).
- `load_tool(name)` raises `ToolDescriptorInvalid` for malformed descriptors, `ToolHandlerImportFailed` for import failures.
- `validate(name)` runs all the static checks (parse + import + signature + classification value); descriptor name field must match file stem.

Capabilities: `supports_install=False, supports_uninstall=False, supports_versioning=False, supports_sandbox_validate=False, supports_skills_catalog=False, durable=True`.

### `SQLiteToolRegistryBackend` (#64 PR 3 — planned)

Conforms to the Protocol with the constructor signature `SQLiteToolRegistryBackend(db_path, agent_scope)`. Constructed from the `sqlite://` URL family via `make_sqlite_tool_registry_backend_from_url(url)` honoring the `sqlite:///<path>?agent_scope=<name>` shape.

- **No optional dependency** — stdlib `sqlite3` only.
- **Schema** (planned): `tools(name PK, agent_scope, descriptor_json, handler_blob, version, classification, created_at, updated_at)` with `idx_tools_scope_name` composite index; `meta(key PK, value)` schema-version tracking with idempotent `INSERT OR IGNORE` cold-start race fix (#61 PR 3 + #63 PR 3 lesson).
- **Handler storage** (planned): base64-encoded Python source + `handler_function_name` column; `load_tool()` decodes + `exec()`-loads into a namespace + pulls out the named function. Sandbox concerns addressed via the judge layer at dispatch time; raw `exec` is acceptable here because the registry IS the trust boundary — operators choose what to install.
- **Cross-agent isolation** (planned): schema includes `agent_scope` so a single DB file can serve multiple agents (SaaS / multi-tenant story). `list_tools()` filters `WHERE agent_scope = ?`. **Critical security primitive** — Step 11 adversarial will probe this (R3 in plan §6).
- **Capabilities** (planned): `supports_install=True, supports_uninstall=True, supports_versioning=False (RESERVED — field round-trips but no resolution), supports_sandbox_validate=False, supports_skills_catalog=False, durable=True (False for :memory:)`.

PR 3 will be plan-subagent-vetted before implementation (lesson from #63 PR 3 — caught 2 design risks pre-impl) and Step 11 adversarial is mandatory post-implementation (#63 PR 3 caught 6 P0/P1 findings, including a REPRODUCED cross-agent path-traversal).

## Exception surface

- `ToolNotInRegistry` — `load_tool(name)` called with an unknown name. Distinct from `ToolNotRegistered` (in-memory dispatch miss).
- `ToolDescriptorInvalid` — descriptor (frontmatter) cannot be parsed.
- `ToolHandlerImportFailed` — handler module cannot be imported, or lacks the `handler` callable.
- `ToolAlreadyInstalled` — `install(source)` collided on tool name (PR 3+).
- `BackendNotRegistered` — operator-pinned backend_id isn't in the registry.
- `ValueError` — invalid tool name (path separator, empty, parent-dir token, leading `.`), or `install(version=X)` against a backend without versioning support.
- `NotImplementedError` — capability-gated method on a backend that doesn't support it.

## Registry

```python
from atomic_agents.registry import (
    register_tool_registry_backend, get_tool_registry_backend,
    list_tool_registry_backends,
)

register_tool_registry_backend("filesystem", FilesystemToolRegistryBackend)
cls = get_tool_registry_backend("filesystem")     # → FilesystemToolRegistryBackend
backend = cls(agent_root)                          # caller instantiates with scope
ids = list_tool_registry_backends()                # ["filesystem"]
```

The registry stores **classes**, not instances (matches `ProfileBackend` + `LogBackend` + `LockBackend` registries). Tool-registry backends carry per-scope construction arguments; the registry's role is the operator-pin lookup that resolves a backend_id to a class.

The default `"filesystem"` registration happens at import time inside `atomic_agents/registry/__init__.py`.

## Operator surface

Tool-registry backend choice is a **deployment-level** decision (the whole framework instance picks "filesystem" or "sqlite" or "pypi"), not an agent-author-level decision. Contrast with:

- `judges.md` — per-agent because judge policy is per-agent-author concern.
- `tools.md` — per-agent because tool path/policy decisions are per-agent.

There is no `tool_registry.md` markdown config — the tool-registry backend is the layer that LOADS tools; circular.

PR 2 of #64 will expose the choice via TWO paths (parallel to the LogBackend / LockBackend / ProfileBackend operator surfaces):

1. **Constructor kwarg** — programmatic operators (Python entry-points wiring the framework into Cloud Run, Kubernetes deployments with custom database connections) pass `AtomicAgent(..., tool_registry_backend=SQLiteToolRegistryBackend(...))` to bypass env-var resolution entirely.

2. **Environment variables** — deployment-config operators (Docker, launchd, Cloud Run env, systemd units) set:
   - `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` — backend id (default `filesystem`). Recognized in PR 1: `filesystem`. PR 3 adds `sqlite`.
   - `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL` — connection / path string for non-filesystem backends. SQLite shape (PR 3): `sqlite:///absolute/path/to/tools.db?agent_scope=<name>`.

   **Credential safety**: `get_default_tool_registry_backend` sanitizes the `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` value before echoing it in error messages — strips anything following `://` and truncates at 32 chars — so an operator who accidentally pastes a URL credential into the BACKEND env var (instead of `..._URL`) does not see the credential echoed in the resulting `BackendNotRegistered` exception text. Mirrors the fixes from `logs/__init__.py:316`, `profile/__init__.py:_redact_for_error_message`, and the same shape in this module's `__init__.py`.

The constructor kwarg ALWAYS wins. Operator-config layering: env vars are deployment-level (per-instance, per-host); the kwarg is per-agent-construction. A test that constructs an `AtomicAgent` with an explicit `tool_registry_backend=` bypasses any env vars the deployment may have set.

## What PR 1 does NOT do (PR 2-4 do)

- **No wiring** — `AtomicAgent.__init__` does not accept `tool_registry_backend` yet, and the in-memory `ToolRegistry` construction path is unchanged. (PR 2.)
- **No second backend** — `SQLiteToolRegistryBackend` ships in PR 3.
- **No `install` / `uninstall`** — filesystem reference impl declares False for both capability flags; the methods raise `NotImplementedError`. (PR 3 ships SQLite with both flipped True.)
- **No skill catalog** — `list_skills_catalog` / `load_skill_catalog_body` raise `NotImplementedError` on both PR-1-era backends; capability flag reserved (Decision 2). PR 4 documents the reservation in the locked spec.
- **No sandboxed validation** — `validate()` is static-only; capability flag reserved (Decision 6).
- **No MCP integration** — MCP discovery stays on its own seam (Decision 3); successor issue files the `MCPServerRegistryBackend` before #64 PR 4 merges.
- **No `doctor` check** — `doctor.check_tool_registry_backend` lands in PR 2 (operator-config coherence, capability snapshot, tool-count probe, URL credential redaction).

## Out of scope

- **MCP server unification.** A separate `MCPServerRegistryBackend` Protocol is the right shape for SaaS-tenancy MCP server discovery; tracked as a successor issue, not part of #64. (Decision 3.)
- **Sandboxed execution as default.** `validate()` is static-only; sandboxed validation is a reserved capability flag (`supports_sandbox_validate`). The process model (subprocess / container / wasm) is too unsettled to lock in #64. (Decision 6.)
- **npm-style multi-version coexistence.** Versioning, when it lands, is pip-style (one-version-per-agent, resolved at install). Operators wanting multi-version use per-agent backend instances with different version pins. (Decision 4.)
- **Tool-removal from in-memory `ToolRegistry` after `uninstall`.** PR 2 wiring runs the backend → in-memory bridge only at agent construction. Hot uninstall during a running call is a future capability (would require the `subscribe` shape reserved on `ProfileBackend`); not in #64.

## Known gaps deferred to follow-up issues

These rough edges in the PR 1 filesystem reference surfaced during Step 11 adversarial review and are tracked here for future implementer awareness. They are NOT blocking for the locked v1.0 surface (PR 4) but should be addressed in follow-up arcs:

- **Symlink resolution under `tools/` is unconstrained.** `FilesystemToolRegistryBackend._import_handler` (and `_parse_descriptor`) does not enforce that `<agent>/tools/<name>.{py,md}` resolves under `tools_dir`. An operator (or, in a future shared-filesystem multi-tenant deployment, a co-tenant) placing a symlink at `tools/foo.py` pointing outside `tools/` causes the handler to load from the symlink target. This is by-design for single-tenant operators who legitimately symlink shared tool modules from a fleet-wide path — refusing symlinks would break that workflow. A future `supports_symlink_refusal=True` capability flag (or a more direct `strict_path_scoping` operator config) is the right shape when SaaS multi-tenant deployments need defense-in-depth here.

- **TOCTOU between `list_tools()` and `load_tool(name)`.** PR 2's wiring loop (`for ref in backend.list_tools(): td = backend.load_tool(ref.name); registry.register(td)`) re-reads each descriptor from disk inside `load_tool` — if an operator (or concurrent process) swaps the descriptor between the two calls, the in-memory registry holds metadata from version A while dispatch routes through handler code from version B. The fix shape — internal `_iter_parsed()` helper that returns `(ref, parsed_descriptor)` pairs and a `load_tool(name, parsed=...)` overload — also closes the redundant-parse perf gap flagged by Step 9.1's perf specialist. Tracked for PR 2 implementation.

- **`ValueError` from `_validate_tool_name` escapes the `AtomicAgentsError` hierarchy.** Operators catching `except AtomicAgentsError` won't catch path-traversal refusal from `load_tool` / `uninstall`. Matches the precedent in `FilesystemAgentProfileBackend._agent_root` (also raises bare `ValueError`) — consistent across sibling Protocol backends. Future arc may introduce a tagged `ToolNameInvalid(AtomicAgentsError)` class and migrate both backends together; the parity matters more than the hierarchy purity in v1.

## Reserved future capabilities

These are NOT committed in the locked v1.0 surface but are reserved in the namespace so future expansions don't need a breaking Protocol change:

- **`supports_versioning=True` + `load_tool(name, version=None)` overload.** Future PyPI / git backends honor version pins. Pip-style as the default-target — one-version-per-agent. Conformance tests will gate version-dispatch tests on this capability.
- **`supports_install=True` + `supports_uninstall=True` on the filesystem backend.** Could land as a "filesystem v2" mode that wraps `cp` / `rm` operations behind the Protocol. Today's filesystem-as-source-of-truth principle (CLAUDE.md §1) argues against this; tracked here for completeness.
- **`supports_sandbox_validate=True` + sandboxed `validate(name)`.** Process model TBD (subprocess + resource-cap? container? wasm?). Will land with the operator config for the sandbox itself.
- **`supports_skills_catalog=True` + `list_skills_catalog()` / `load_skill_catalog_body(name)`.** Catalog-side skill surface for PyPI / git / company-internal-HTTP backends. Spec/24's `save_skill` reservation stays the per-agent mounting primitive; this surface is upstream of identity (Decision 2).
- **`AsyncToolRegistryBackend`** — async variant for HTTP-served deployments. Same shape; `list_tools` / `load_tool` become `async def`.
- **`SubscribeToolRegistryBackend`** — adds `subscribe(callback) -> handle` for backends that push catalog-change events. Reserved alongside `supports_subscribe` on `ProfileBackend`.

## Conformance test surface

The conformance suite (PR 4 lock — parametrized across registered backends):

- `tests/test_tool_registry_protocol_conformance.py` — **43 conformance test functions parametrized via `BACKEND_FACTORIES`** (only filesystem in PR 1 → 55 invocations, of which 8 skip on `supports_install` / `supports_skills_catalog` / `supports_versioning` capabilities the filesystem reference declares False; PR 3 adds SQLite for ×2 invocations + the capability-gated skip set adjusts to whatever SQLite declares). Tests cover: Protocol surface, `backend_id` stability, `capabilities()` shape, `list_tools` empty/populated/lexicographic, `load_tool` returns `ToolDefinition` with callable handler, `load_tool` raises `ToolNotInRegistry` for missing, `validate` static (does NOT execute handler), `validate` reports import + descriptor errors, capability-gated install/uninstall raise NotImplementedError on filesystem, classification round-trip on `ToolRef`, version field round-trip (currently None on filesystem), descriptor frontmatter validation, descriptor handler-module-not-found, descriptor handler-not-callable, path-traversal on tool name refused, source attribution populated, integration with existing `ToolNameCollision`.
- `tests/test_tool_registry_filesystem_backend.py` — **35 filesystem-specific test functions** (40 invocations after pytest parametrization for the few multi-input cases): on-disk descriptor parsing, hidden-file exclusion, `tools/` absent → empty list, `tools/__init__.py` ignored, `tools/_helper.py` ignored, handler module re-import semantics, `validate()` warnings vs errors, registry resolution via `register_tool_registry_backend`, `get_default_tool_registry_backend` env-var dispatch + invalid backend_id + credential redaction + truncation, source field shows descriptor's filesystem path, plus Step 11 regression coverage (256 KB descriptor size cap, control-character refusal in tool names, empty/null frontmatter rejection, `agent_root=''` / `agent_root='.'` refusal at construction).
- `tests/test_tool_registry_sqlite_backend.py` (PR 3) — **~30 SQLite-specific tests**: schema creation, cold-start race, install/uninstall round-trip, cross-agent isolation, handler exec, URL parsing.
- `tests/test_tool_registry_integration.py` (PR 2) — **~17 wiring tests**: kwarg override, runner threading for all 4 runners, doctor PASS/WARN/FAIL paths, per-agent isolation, tool-collision preservation.

Total at PR 1 close (shipped): **+87 newly passing tests** (95 invocations − 8 capability-gated skips); full suite went 1750 → 1837 passing, 16 → 24 skipped, 1766 → 1861 total collected.
Total at arc close (PR 4 lock): the conformance suite re-parametrizes across both reference backends; budgeting ~104 conformance invocations + ~50 backend-specific + ~17 wiring = ~170 tests verifying the Protocol contract.

## Related

- spec/20 — `MemoryBackend` (the original Protocol pattern; this spec mirrors its shape).
- spec/21 — `LockBackend` (immediate-sibling template; this spec mirrors its `types.py`/`backend.py`/registry split and operator-surface rationale).
- spec/22 — `LogBackend` (sibling template; this spec follows its DRAFT → lock cadence + Implementer contract structure).
- spec/24 — `AgentProfileBackend` (freshest locked spec; this spec mirrors its scaffolding shape and Decision 1-10 articulation).
- spec/28 — `JudgeBackend` (third-template; the tool-registry arc adopts the same "lock spec at PR 4" discipline and `classification` round-trip).
- spec/31 — `LLMBackend` (second-template; this spec mirrors its types/backend split).
- spec/17 — In-memory `ToolRegistry` + `ToolDefinition` (the dispatch-layer type the discovery layer materializes into).
- spec/19 — MCP (the carve-out target — Decision 3 keeps MCP separate; a successor `MCPServerRegistryBackend` issue will trace).
- CLAUDE.md §1 — "The vault is the source of truth." The filesystem backend's edit-`<agent>/tools/`-directly UX (Decision 7) is in service of this rule.
- CLAUDE.md §3 — "Layers compose; they don't merge." Decisions 2 + 3 enforce this for the skill + MCP carve-outs.
- CLAUDE.md §6 — "Progressive disclosure by default." `ToolRef` metadata + lazy `load_tool` (Decision 5) is in service of this rule.
- Issue [#64](https://github.com/dep0we/agent-stack/issues/64) — motivation + acceptance criteria.

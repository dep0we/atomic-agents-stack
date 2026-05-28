# 25 — ToolRegistryBackend Protocol

**Status:** **LOCKED** at #64 PR 4.
**Origin:** [#64](https://github.com/dep0we/atomic-agents-stack/issues/64).
**Shipping plan across four PRs (all landed):** PR 1 (Protocol scaffolding + `FilesystemToolRegistryBackend` reference impl + conformance suite + DRAFT spec — merged at #197), PR 2 (wire `AtomicAgent.__init__` + per-runner kwargs on OutcomeRunner / EvalRunner / DreamRunner + `doctor.check_tool_registry_backend` coherence check — merged at #198), PR 3 (second reference impl — `SQLiteToolRegistryBackend` — with `install` / `uninstall` flipped True, parametrized conformance suite, plan-subagent-vetted before implementation, Step 11 adversarial review fixes — merged at #199), **PR 4 (spec LOCKED + `Implementer contract for registry-backed tool backends` documented + README / CLAUDE.md status flip to "seven backend protocols shipped" — this PR)**.

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

The package name is `registry` (not `tools` — that's the existing in-memory dispatch module — nor `tool_registry` — single-word + lowercased package names match the established `profile` / `logs` / `locks` pattern). The SQLite sibling for the alternate backend lives at `atomic_agents/registry/sqlite.py` alongside the filesystem reference.

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

**The arc ships ONLY the tool surface** (`list_tools` / `load_tool` / `validate` / `install` / `uninstall`). Skill catalog stays a reserved capability flag (`supports_skills_catalog: bool`) on `ToolRegistryCapabilities`; both reference backends declare False, future PyPI / Git backends would flip True in their own arc. Spec/24 §"Reserved future capabilities" `save_skill` reservation stays intact — it's the *mounting* primitive on the profile side.

**Why this carve-out (vs. issue body's literal "list_skills / load_skill" ask):**

1. The issue body was written before spec/24 locked. The locked spec/24 owns per-agent skill listing — flipping it now means re-opening a locked spec.
2. Spec/18's progressive-disclosure principle (CLAUDE.md §6) is a per-agent property — "what this agent declares it knows about" is identity-layer, not catalog-layer. The catalog is upstream of identity.
3. Keeping the catalog surface reserved (not shipped) keeps the v1.0 surface scope-disciplined; future expansion has a clean landing pad in `ToolRegistryCapabilities.supports_skills_catalog`.
4. A future SaaS deployment will need BOTH — `AgentProfileBackend` answering "what does this agent currently know about?" AND `ToolRegistryBackend` answering "what's available to teach this agent?" Merging the two now means picking a wrong shape and re-doing it later.

**Operator-facing consequence:** today operators copy `tools/<name>.{md,py}` + `skills/<name>/SKILL.md` into the agent dir manually; ToolRegistryBackend lifts the `tools/` discovery to a Protocol; skills/ discovery still walks the filesystem via `discover_skills(agent_root)` until a future ToolRegistryBackend ships catalog skills. Both home and SaaS shapes work in both regimes.

### Decision 3: MCP server discovery stays in its own module — `MCPServerSpec` is NOT a `ToolRef`

MCP servers are **processes** with subprocess lifecycle, env-var-resolved credentials, transport selection, per-call asyncio overhead. Tools are **functions** with a JSON schema and a handler. **The two are different invocation models that happen to both show up in agent.tool_registry after MCP discovery.** Conflating their Protocols means:

- `ToolRegistryBackend` implementers would need to model subprocess specs that aren't relevant to filesystem-tool catalogs.
- The `mcp.md` → `MCPServerSpec` security boundary (env-var resolution at parse time, the `$VAR` carry-over for save_profile per spec/24 Decision 1) doesn't translate to a registry catalog idea.
- MCP servers scope credentials per-agent in a way that a shared catalog would have to invent re-scoping for.

**Reserved future:** a separate `MCPServerRegistryBackend` Protocol could land in its own arc when the SaaS-tenancy story actually needs it. Documented here in §"Out of scope" + a forward-pointer in spec/19 (existing MCP spec) so future readers find the carve-out. The successor issue [#201](https://github.com/dep0we/atomic-agents-stack/issues/201) tracks the carve-out.

**Why:** Layers compose; they don't merge (CLAUDE.md §3). The `AgentProfileBackend` already snapshots `mcp_servers: list[MCPServerSpec]` on `AgentProfile` from `mcp_md_raw`; the MCP pool's `_specs` are routed from `self._profile.mcp_servers` (in place after #63 PR 2 at `agent.py:2018` + `agent.py:2337`). No additional indirection needed; the Profile backend covers the per-agent MCP server list; the eventual MCP-registry layer covers the cross-agent install/audit story.

### Decision 4: Version field on `ToolRef` ships now, semantics deferred behind capability flag

`ToolRef.version: str | None = None` is part of the canonical types. `FilesystemToolRegistryBackend` always sets `version=None` (filesystem layout has no version semantics today). `ToolRegistryBackend.load_tool(name: str) -> ToolDefinition` does NOT accept a `version` parameter — adding it pre-emptively would lock a wrong shape (which of pip-style / npm-style / git-style wins?). `ToolRegistryCapabilities.supports_versioning: bool` reserves the field.

Documented here in §"Reserved future capabilities" with **pip-style** as the default-target when the flag lands. Rationale: matches the framework's pip-shaped Python ecosystem position. npm-style multi-version-coexistence is explicitly documented as out-of-scope (operators wanting it use multiple registries scoped per-agent, which already works through the per-agent backend instance pattern).

**Why the field ships upfront:** `to_dict / from_dict` round-trip preservation requires the field on the canonical type from the first release — adding it later would break the JSON shape forward-compat (same lesson as spec/24 Decision 1's `mcp_md_raw` preservation discipline). Cheap to add upfront (one Optional field on a frozen dataclass); expensive to retrofit later.

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

**Why:** the filesystem reference impl walks `tools/<name>.md` for metadata (cheap, just markdown parsing) and ONLY runs `importlib.util.spec_from_file_location` when `load_tool(name)` is called. **The lazy/eager distinction is at the BACKEND layer — not the wiring layer.** `AtomicAgent.__init__` calls BOTH `list_tools()` AND `load_tool(name)` for every backend ref at construction time, so handler-module imports fire on every agent construction (not lazily on first dispatch). Operators with side-effecting top-level code in `<agent>/tools/<name>.py` see the side effect at agent construction. This is acceptable in the filesystem-trust-boundary model (operators own `<agent>/tools/`); a future capability flag `lazy_handler_import: bool` MAY ship to support fully-lazy dispatch for backends serving untrusted catalogs (PyPI / SaaS / HTTP).

A 50-tool agent under filesystem still pays no Python import cost on `list_tools()` alone (frontmatter parsing only) — the eager import happens via the wiring loop's per-ref `load_tool(name)` call. The pre-#64 `agent.py` did NOT load tool implementations from `tools/<name>.py` at all — custom tools were registered programmatically by the operator. The filesystem reference ALSO discovers + materializes `tools/<name>.py` modules in the spirit of the issue's "tools/<name>.md descriptor + tools/<name>.py implementation" framing. **The filesystem backend behaves as a no-op when `tools/` dir is empty or absent** (the wiring loop iterates zero times — agents with no `tools/` directory see byte-identical pre-#64 behavior).

### Decision 6: `ToolRegistryBackend.validate(name)` is a static check, NOT a live invocation

The issue body lists `validate(name)` as an optional capability — "sandbox check before live load." Both reference backends ship `validate()` as a **static** check: parses the descriptor, attempts the import in a `try/except`, returns `ValidationResult(ok: bool, errors: list[str], warnings: list[str])`. **Does NOT run the handler.** Sandboxed-execution-as-validation is reserved for a future capability flag (`supports_sandbox_validate`). The filesystem backend's `validate()` checks:

1. Descriptor parses (frontmatter present, YAML valid, root is dict, `input_schema` is dict).
2. Handler module imports without error.
3. Handler is callable (the module exposes a callable named `handler`).
4. Classification (if present) is a valid `ActionClass` enum value.

Soft warnings cover the hygiene issues that don't break dispatch: missing description, missing classification (the runtime falls back to `external_side_effect` per spec/28).

**Why:** running operator handlers as a side-effect of `validate()` violates least-astonishment — `validate` is an audit-time call, not a runtime call. The runtime safety story is `ToolRegistry.execute()` and the judge layer (spec/28), which is the right place for sandboxed dispatch. Decisions about sandbox process model (subprocess? container? wasm?) are too unsettled to lock in v1.0.

### Decision 7: `install` / `uninstall` are capability-gated, ABSENT from filesystem backend

Filesystem backend declares `supports_install=False`. `install(source, version)` / `uninstall(name)` raise `NotImplementedError`. Operators on filesystem do the equivalent by editing `<agent>/tools/<name>.{md,py}` directly (today's UX, preserved). PyPI / Git / Remote backends would flip `supports_install=True` in their own arc.

**Why:** filesystem-as-source-of-truth (CLAUDE.md §1) — the filesystem backend doesn't own install semantics because the operator's text editor + `cp` are the install primitive there. Inventing a filesystem-backend `install()` would mean either (a) downloading from a URL and writing to disk (network call from a "filesystem" backend — surprising), or (b) copying from another path (a shell `cp` wrapper — superfluous). Capability flag honestly signals the shape; future backends do the work.

### Decision 8: `ToolNameCollision` semantics survive the indirection — backend tools register AFTER operator tools, with `allow_overwrite=False`

The legacy `ToolNameCollision` (raised by `ToolRegistry.register()` with default `allow_overwrite=False`) keeps its semantics. Wiring path:

1. `AtomicAgent.__init__` calls `self.tool_registry_backend = tool_registry_backend or get_default_tool_registry_backend(self.agent_root)`.
2. After in-memory `self.tool_registry = tools if tools is not None else ToolRegistry()` is constructed (agent.py:243), the framework runs: `for ref in self.tool_registry_backend.list_tools(): td = self.tool_registry_backend.load_tool(ref.name); self.tool_registry.register(td)`.
3. If the operator ALSO passes `tools=ToolRegistry()` with pre-registered tools, the order is: operator's tools register first (operator intent wins on collisions), then backend tools register with `allow_overwrite=False` (collisions surface loudly as `ToolNameCollision`).
4. MCP discovery at agent.py:2337 — unchanged. MCP tools register with `allow_overwrite=True` (existing semantics). MCP tool names ALWAYS namespace as `server__tool`, so collisions with backend-loaded tools are vanishingly rare and indicate a real conflict.

**Why:** preserves all 115 `AtomicAgent(...)` construction sites in the test suite — when filesystem backend's `tools/` dir is empty, the backend yields zero `ToolRef`s and the wiring loop is a no-op. New behavior (backend yields tools) only activates when operators populate `tools/` with descriptor+handler files. **Surface change is opt-in via filesystem layout, not opt-in via API.**

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

Single-host operators wanting SQLite flip `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND=sqlite` and get a working default — the SQLite backend defaults to `<agent_root>/.tools.db` when URL is absent, mirroring spec/24's SQLite default path convention.

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
- Handlers MUST be re-importable across `load_tool` calls. The filesystem reference impl does NOT populate `sys.modules` — each `load_tool` call produces a fresh module via `importlib.util.spec_from_file_location` + `exec_module`. Operators with side-effecting top-level code in their handler module see the side effect re-fire on every `load_tool`. Backends MAY opt to cache (insert into `sys.modules` under a deterministic qualname), but MUST NOT cache the returned `ToolDefinition` instance — callers may mutate fields before registering. The framework calls `load_tool` exactly once per agent construction, so re-import overhead lands only on agent re-construction.

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

- Reserved (Decision 2). Both reference backends raise `NotImplementedError`.
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

### `SQLiteToolRegistryBackend` (#64 PR 3)

Conforms to the Protocol with the constructor signature `SQLiteToolRegistryBackend(db_path, agent_scope, *, handlers_root=None)`. Constructed from the `sqlite://` URL family via `make_sqlite_tool_registry_backend_from_url(url)` honoring the `sqlite:///<path>?agent_scope=<name>` shape (default `agent_scope="default"` when the query param is absent).

- **No optional dependency** — stdlib `sqlite3` only.
- **Hybrid storage shape (plan-subagent Risk A fix)**: SQLite stores **metadata only** (descriptor JSON + handler path + version + classification + scope + timestamps); handler **bodies** are .py files on disk under `handlers_root/<agent_scope>/<name>.py`. The base64-exec'd-source approach was rejected at the plan-subagent stage because:
  1. `exec(source, namespace)` creates a function whose `__globals__` is the synthetic exec namespace — module-level `import requests; session = requests.Session()` patterns at handler-module top-level produce a `handler` that loses access to those imports at first invocation (`NameError` at runtime).
  2. Decorators, closures-over-module-globals, and any `from __future__ import` directives don't work cleanly.
  3. Operators lose `cat <path>` inspectability.
  Filepath storage uses the same `importlib.util.spec_from_file_location` + `exec_module` path the filesystem reference uses — handler ergonomics are identical to filesystem. The SQLite layer owns metadata, version, scope, and atomic install/uninstall semantics; the on-disk file body is the same shape Python expects.
- **Schema**: `tools(agent_scope TEXT NOT NULL, name TEXT NOT NULL, descriptor_json TEXT, handler_path TEXT, version TEXT, classification TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY (agent_scope, name))` — composite primary key so two scopes can both have a tool named the same. `meta(key PRIMARY KEY, value)` schema-version tracking with idempotent `INSERT OR IGNORE` cold-start race fix (#61 PR 3 + #63 PR 3 lesson).
- **`handlers_root`**: constructor kwarg (default `db_path.parent / "handlers"`). Created with `mkdir(parents=True, exist_ok=True)` on first install. Layout: `<handlers_root>/<agent_scope>/<name>.py` — per-scope subdir for cross-scope isolation at the filesystem layer too (defense in depth — even if a future migration drops the agent_scope SQL filter, the per-scope subdir keeps handler files separated).
- **`install(source, version=None)` semantics**:
  - `source` is a filesystem path to a directory containing `<name>.md` (descriptor) + `<name>.py` (handler module).
  - `version` MUST be `None` when `supports_versioning=False` (which SQLite declares); the column accepts non-NULL values for forward compatibility but the backend rejects them at the call site with `ValueError` (plan-subagent Risk L — capability honesty).
  - Handler source path is resolved + validated under the source directory (no path-traversal via name).
  - Handler .py file is copied into `<handlers_root>/<agent_scope>/<name>.py` via `_io.atomic_write`.
  - INSERT INTO tools ON CONFLICT(agent_scope, name) DO NOTHING — if `cursor.rowcount == 0`, raises `ToolAlreadyInstalled`. Atomic at the storage layer (plan-subagent Risk D).
- **`uninstall(name)`**: DELETE FROM tools WHERE agent_scope=? AND name=? + remove handler file. Idempotent — uninstalling an absent name is a no-op.
- **Cross-scope isolation**: schema includes `agent_scope` so a single DB file can serve multiple agents (SaaS / multi-tenant story). `list_tools()` / `load_tool()` / `uninstall()` ALL filter `WHERE agent_scope = ?`. The scope is hardcoded from the constructor; install() never accepts a scope parameter. **Critical security primitive** — Step 11 adversarial probes this.
- **Trust model (plan-subagent Risk K)**: SQLite is the SaaS-shape backend per spec/25 Decision 9, but the catalog being process-shared does NOT mean trust is process-shared. Multi-tenant deployments MUST scope at the process level (one framework process per tenant), NOT just at the `agent_scope` column. The framework is NOT a sandbox; install() chooses what code executes in the framework's process. The judge layer (spec/28) is the runtime defense, not the registry layer.
- **Concurrency**: `threading.local` connection pool gives each thread its own `sqlite3.Connection`. WAL journal mode + `synchronous=NORMAL` for multi-process safety on local filesystems. **Network-mounted filesystems (NFS, SMB) NOT supported** — SQLite WAL on NFS is documented-broken upstream.
- **In-memory mode**: `SQLiteToolRegistryBackend(":memory:")` or URL `sqlite::memory:` / `sqlite:///:memory:` constructs a non-persistent backend; emits `RuntimeWarning` and reports `durable=False`. Single-threaded (test-only — `check_same_thread=True`); cross-thread access raises `ProgrammingError` honestly rather than silently corrupting (plan-subagent Risk G — the F-1 docstring-lying-about-thread-safety shape from #63 PR 3).
- **Schema migration**: when a future arc bumps schema_version, the open path mirrors `profile/sqlite.py` — raises `RuntimeError` with the expected-vs-found versions and a migration-required note. No auto-migration; operators run an explicit migrate command (successor issue tracked).
- **Capabilities**: `supports_install=True, supports_uninstall=True, supports_versioning=False (column exists but not dispatched on — plan-subagent Risk L), supports_sandbox_validate=False, supports_skills_catalog=False, durable=True (False for :memory:)`.

## Exception surface

- `ToolNotInRegistry` — `load_tool(name)` called with an unknown name. Distinct from `ToolNotRegistered` (in-memory dispatch miss).
- `ToolDescriptorInvalid` — descriptor (frontmatter) cannot be parsed.
- `ToolHandlerImportFailed` — handler module cannot be imported, or lacks the `handler` callable.
- `ToolAlreadyInstalled` — `install(source)` collided on tool name.
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

The operator surface exposes the choice via TWO paths (parallel to the LogBackend / LockBackend / ProfileBackend operator surfaces):

1. **Constructor kwarg** — programmatic operators (Python entry-points wiring the framework into Cloud Run, Kubernetes deployments with custom database connections) pass `AtomicAgent(..., tool_registry_backend=SQLiteToolRegistryBackend(...))` to bypass env-var resolution entirely.

2. **Environment variables** — deployment-config operators (Docker, launchd, Cloud Run env, systemd units) set:
   - `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` — backend id (default `filesystem`). Recognized: `filesystem`, `sqlite`.
   - `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL` — connection / path string for non-filesystem backends. SQLite shape: `sqlite:///absolute/path/to/tools.db?agent_scope=<name>`.

   **Credential safety**: `get_default_tool_registry_backend` sanitizes the `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` value before echoing it in error messages — strips anything following `://` and truncates at 32 chars — so an operator who accidentally pastes a URL credential into the BACKEND env var (instead of `..._URL`) does not see the credential echoed in the resulting `BackendNotRegistered` exception text. Mirrors the fixes from `logs/__init__.py:316`, `profile/__init__.py:_redact_for_error_message`, and the same shape in this module's `__init__.py`.

The constructor kwarg ALWAYS wins. Operator-config layering: env vars are deployment-level (per-instance, per-host); the kwarg is per-agent-construction. A test that constructs an `AtomicAgent` with an explicit `tool_registry_backend=` bypasses any env vars the deployment may have set.

## Reserved at lock (per Decision 2 / 4 / 6 / 7)

- **No skill catalog yet.** `list_skills_catalog` / `load_skill_catalog_body` raise `NotImplementedError` on both reference backends; `supports_skills_catalog=False` on both. The capability flag is reserved for future PyPI / git / company-internal-HTTP backends.
- **No sandboxed validation.** `validate()` stays static-only on both backends; `supports_sandbox_validate=False`. Sandboxed validation is a reserved capability flag (process model TBD).
- **No version dispatch.** `ToolRef.version` round-trips on the canonical type but `load_tool` does NOT accept a `version` parameter; `supports_versioning=False` on both reference backends. Pip-style resolution is the documented default-target when the flag flips.
- **No MCP integration.** MCP discovery stays in `atomic_agents/mcp.py` (Decision 3); a separate `MCPServerRegistryBackend` Protocol is the right shape for the SaaS-tenancy MCP story. Successor issue (`[backend] MCPServerRegistryBackend — unify MCP server discovery across agents`) tracks the carve-out.
- **No filesystem `install` / `uninstall`.** Filesystem reference declares both `False` (operators edit `<agent>/tools/` directly — Decision 7). SQLite reference flips both `True`.

## Out of scope

- **MCP server unification.** A separate `MCPServerRegistryBackend` Protocol is the right shape for SaaS-tenancy MCP server discovery; tracked as a successor issue, not part of #64. (Decision 3.)
- **Sandboxed execution as default.** `validate()` is static-only; sandboxed validation is a reserved capability flag (`supports_sandbox_validate`). The process model (subprocess / container / wasm) is too unsettled to lock in #64. (Decision 6.)
- **npm-style multi-version coexistence.** Versioning, when it lands, is pip-style (one-version-per-agent, resolved at install). Operators wanting multi-version use per-agent backend instances with different version pins. (Decision 4.)
- **Tool-removal from in-memory `ToolRegistry` after `uninstall`.** The framework runs the backend → in-memory bridge only at agent construction. Hot uninstall during a running call is a future capability (would require the `subscribe` shape reserved on `ProfileBackend`); not in #64.

## Known gaps deferred to follow-up issues

These rough edges in the filesystem reference surfaced during #64 PR 1 Step 11 adversarial review and are tracked here for future implementer awareness. They are NOT blocking for the locked v1.0 surface but should be addressed in follow-up arcs:

- **Symlink resolution under `tools/` is unconstrained.** `FilesystemToolRegistryBackend._import_handler` (and `_parse_descriptor`) does not enforce that `<agent>/tools/<name>.{py,md}` resolves under `tools_dir`. An operator (or, in a future shared-filesystem multi-tenant deployment, a co-tenant) placing a symlink at `tools/foo.py` pointing outside `tools/` causes the handler to load from the symlink target. This is by-design for single-tenant operators who legitimately symlink shared tool modules from a fleet-wide path — refusing symlinks would break that workflow. A future `supports_symlink_refusal=True` capability flag (or a more direct `strict_path_scoping` operator config) is the right shape when SaaS multi-tenant deployments need defense-in-depth here. Tracked in [#202](https://github.com/dep0we/atomic-agents-stack/issues/202).

- **TOCTOU between `list_tools()` and `load_tool(name)` + redundant-parse perf gap.** The wiring loop (`for ref in backend.list_tools(): td = backend.load_tool(ref.name); registry.register(td)`) re-reads each descriptor from disk inside `load_tool` — if an operator (or concurrent process) swaps the descriptor between the two calls, the in-memory registry holds metadata from version A while dispatch routes through handler code from version B. At N=50 tools the redundant parse wastes ~3 ms per agent construction (~60 μs per tool, measured by #64 PR 2 Step 9.1 perf specialist). Fix shape: internal `_iter_parsed()` helper returning `(ref, parsed_descriptor)` pairs + a `load_tool(name, parsed=...)` overload threaded through the wiring loop. Tracked in [#203](https://github.com/dep0we/atomic-agents-stack/issues/203).
- **Handler module re-import on every agent construction in the same process.** `FilesystemToolRegistryBackend._import_handler` uses `importlib.util.spec_from_file_location` which does NOT populate `sys.modules` — each `load_tool` call re-executes the handler module's top-level code. For runner-loop deployments (`EvalRunner` constructing one agent per golden test row, `OutcomeRunner` per iteration) this is `N_tools × N_invocations` re-executions per batch. Operators with module-level resource setup (`session = requests.Session()` at top-level) pay session construction once per agent build. Fix shape: `(handler_path, mtime)` memoization cache in `_import_handler`. Tracked in [#204](https://github.com/dep0we/atomic-agents-stack/issues/204).

- **`ValueError` from `_validate_tool_name` escapes the `AtomicAgentsError` hierarchy.** Operators catching `except AtomicAgentsError` won't catch path-traversal refusal from `load_tool` / `uninstall`. Matches the precedent in `FilesystemAgentProfileBackend._agent_root` (also raises bare `ValueError`) — consistent across sibling Protocol backends. Future arc may introduce a tagged `BackendInputInvalid(AtomicAgentsError, ValueError)` multi-inherit class and migrate both backends together; the parity matters more than the hierarchy purity in v1. Tracked in [#205](https://github.com/dep0we/atomic-agents-stack/issues/205).

## Implementer contract for registry-backed tool backends (#64 PR 4)

A backend that participates in the `ToolRegistryBackend` registry alongside the two reference impls (`FilesystemToolRegistryBackend`, `SQLiteToolRegistryBackend`) is committing to the contract documented above plus the operational guarantees below. The reference impls + the parametrized conformance suite (`tests/test_tool_registry_protocol_conformance.py`) are the canonical examples; the MUSTs below are what future PyPI / git / company-internal-HTTP / SaaS-database adapters need to honor for the framework to treat their backend as interchangeable with the references. Concretely, **implementers MUST**:

1. **Refuse path-traversal `name` at the API boundary.** `name` is operator-controlled and flows from `load_tool` / `validate` / `install` / `uninstall` straight into handler-file paths, descriptor paths, and (for SQLite-shape backends) primary-key lookups. Backends with native filesystem semantics MUST validate `name` BEFORE any disk access — refuse path separators (`/`, `\\`), parent-dir tokens (`..`), leading `.`, control characters, and empty / oversized values. The reference `FilesystemToolRegistryBackend._validate_tool_name` ships this check (`_validate_tool_name` raises `ValueError` for every malformed shape); `SQLiteToolRegistryBackend.install` mirrors it before touching the `tools` table or the `<handlers_root>/<agent_scope>/<name>.py` path. **Path-traversal refusal applies in BOTH directions** — operator-supplied `name` AND operator-supplied `agent_scope` (the SQLite constructor refuses scope tokens like `..`, `/`, or empty after `_validate_scope` runs at construction). #64 PR 1 Step 11 adversarial REPRODUCED a `name="../../etc/passwd"` traversal pre-fix; the refusal is the structural defense.

2. **Cross-scope isolation MUST be enforced at the storage layer.** For shared-catalog backends (one DB file / one HTTP service / one git remote serving multiple agents), `list_tools` / `load_tool` / `uninstall` / any read MUST filter on the backend's scope-identifier at the storage primitive — NOT in a post-fetch Python loop. The reference `SQLiteToolRegistryBackend` filters `WHERE agent_scope = ?` on every query; the `agent_scope` value is hardcoded from the constructor and refused at construction for path-traversal tokens (defense-in-depth on top of SQL parametrization). Backends with per-agent natural scoping (filesystem reference's per-agent `<agent>/tools/` dir) get this structurally — there's no shared catalog to leak across. Backends with shared scoping MUST treat the scope-identifier as opaque (never concatenate into a path or template), MUST never accept it as a per-call parameter (it belongs to the backend instance, not the call), and MUST refuse silent cross-scope reads at the SQL / API layer. Multi-tenant SaaS deployments rely on this; #63 PR 3 Step 11 F-3 caught the analogous gap on `AgentProfileBackend.list_snapshots` and the pattern is the same here.

3. **`install()` MUST be atomic at the tool level.** Concurrent `install(source="x", ...)` calls — exactly one wins, the others raise `ToolAlreadyInstalled`. The reference `SQLiteToolRegistryBackend.install` ships the canonical pattern after PR 3 Step 11 adversarial REPRODUCED a 50/50 TOCTOU race in the original ordering: **INSERT-first + atomic_write-on-success-only.** The order matters: `INSERT INTO tools ON CONFLICT(agent_scope, name) DO NOTHING` runs first; if `cursor.rowcount == 0`, raise `ToolAlreadyInstalled` WITHOUT touching disk; only after the INSERT wins does the handler `.py` file get atomic-written under `<handlers_root>/<agent_scope>/<name>.py`. The original order (handler atomic_write → INSERT) caused the loser's rollback `unlink()` to destroy the WINNER's handler file in a concurrent race, leaving the catalog row pointing to a missing handler permanently. Backends with non-SQL storage (HTTP services, git remotes) MUST identify the equivalent atomic primitive (CAS write, ETag-conditional PUT, ref-update transaction) and pin the invariant via a regression test that pins both legs: winner's body survives + winner remains loadable post-race.

4. **Descriptor round-trip MUST be lossless for the dispatch fields, with backend-shape-appropriate fidelity.** The contract has two tiers:

   **Tier A — raw-text-preserving backends (filesystem-shape).** Backends that serve operator-edited markdown descriptors directly MUST persist the raw descriptor body (frontmatter + markdown comments + operator notes) and reconstruct via parsing on each `load_tool`. Never store ONLY the parsed structured fields. The reference `FilesystemToolRegistryBackend` reads `<agent>/tools/<name>.md` verbatim from disk on each `load_tool`. The lesson from spec/24 Decision 1 / MUST #4 applies: parsers are lossy (operator notes after the markdown body, comments inside YAML, etc.) and a structured-only persistence shape would silently drop them.

   **Tier B — structured-storage backends (SQLite-shape).** Backends storing descriptors as structured rows (`descriptor_json` columns, document fields, API payloads) MUST document the lossy parse in the module docstring (operator notes / YAML comments are NOT preserved across `install()` → `load_tool()`) AND MUST round-trip every `ToolDefinition` field that affects dispatch — `name`, `description`, `classification`, `input_schema`, `handler` — losslessly. The reference `SQLiteToolRegistryBackend` serializes the parsed descriptor as JSON + stores the handler `.py` file on disk; on `load_tool` it re-derives `ToolDefinition` from the JSON + re-imports the handler from the stored path. Operators wanting raw-text fidelity on a Tier B backend run filesystem alongside SQLite (multi-backend deployment) OR wait for a future Tier B backend that also persists the raw descriptor text in a sidecar column.

   **Both tiers**: `ToolRef` is **append-only** on the canonical type — removing fields is a breaking change; adding fields is a minor-version bump with `None` / empty defaults. Backends MUST round-trip ALL `ToolRef` fields (including `version` and `source`, which the filesystem reference always sets to `None` / a path string respectively) so JSON-shape forward compat holds across future field additions.

   **Conformance-test pinning status (v1 lock):** the parametrized conformance suite pins round-trip on `name` (via `list_tools()` + `load_tool()`), `classification` (via `test_tool_ref_classification_round_trip`), `version` (currently `None` on both reference backends), and — closed by [#207](https://github.com/dep0we/atomic-agents-stack/issues/207) — Tier B `input_schema` / `description` / `handler` callable round-trip on structured-storage backends via three parametrized conformance tests in `tests/test_tool_registry_protocol_conformance.py`: `test_load_tool_round_trips_input_schema` (strict dict equality on a non-trivial schema with nested objects + integer constraints + booleans), `test_load_tool_round_trips_description` (exact-string round-trip with quotes + unicode), and `test_load_tool_round_trips_handler_callable` (handler invocation through a real call with structured input). Both reference backends pass without code changes; a future Tier B adapter (PyPI, SaaS-database) that silently drops or normalizes any of these fields fails at the conformance gate rather than passing into production.

5. **Schema initialization MUST be idempotent across processes AND `PRAGMA busy_timeout` MUST precede `PRAGMA journal_mode=WAL` AND the WAL transition MUST retry on `SQLITE_BUSY` / `SQLITE_LOCKED` for SQLite-shape backends.** Multi-process operators may have N replicas all opening a fresh backend simultaneously (Cloud Run / Kubernetes / multi-replica shapes). The reference `SQLiteToolRegistryBackend._ensure_schema` ships THREE lessons learned across the protocol-pattern arcs: (a) the `INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1')` cold-start race fix from #61 PR 3 SQLiteLogBackend Step 11 P0 #2 (multi-process schema init mustn't deadlock or raise UNIQUE-constraint errors), (b) the lesson from #64 PR 3 Step 11: `PRAGMA busy_timeout=5000` MUST be set BEFORE `PRAGMA journal_mode=WAL` (the WAL-transition lock contention manifests as `OperationalError: database is locked`, REPRODUCED 3/5 pre-fix in PR 3 Step 11), AND (c) the **third lesson from #208 (SQLiteLogBackend) + #215 (SQLiteToolRegistryBackend follow-on)**: `PRAGMA busy_timeout` alone is insufficient because SQLite's journal-mode-switch path is NOT fully covered by the busy_handler — empirically, busy_timeout alone failed 13/20 times on macOS under heavy contention and surfaced as the CI 3.11 flake in #214. The WAL transition MUST be wrapped in a retry loop matching on `exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)` (Python 3.11+) with exponential backoff bounded at the busy_timeout window. Match on errorcode (not message text) so a future SQLite wording change can't silently re-raise legitimate corruption errors. Future Postgres / MySQL / non-SQLite backends MUST identify the equivalent cold-start-race primitive (transactional schema migrations, advisory locks during DDL) and document it in the backend's module docstring.

6. **Capability honesty: claim-vs-behavior parity is the load-bearing invariant.** `capabilities() -> ToolRegistryCapabilities` is a contract, not a hint. Backends declaring `supports_install=True` MUST implement `install()` such that a freshly installed tool surfaces in the NEXT `list_tools()` call AND is `load_tool`-able by name (the `test_capability_parity_install` conformance test pins this round-trip — #64 PR 3 Step 11 caught the original version as a tautology that only checked `NotImplementedError` wasn't raised, and was rewritten to a real install + verify-in-list_tools round-trip). Backends declaring `supports_versioning=False` MUST reject non-None `version` arguments at `install()` time with `ValueError` (plan-subagent Risk L from PR 3 — the column round-trips for forward compat but the dispatch doesn't honor it, so accepting a version arg would silently store it and load_tool would ignore it — operator footgun). Backends declaring `supports_uninstall=True` MUST make `uninstall(name)` idempotent — uninstalling a name that doesn't exist is a no-op (no exception); this mirrors in-memory `ToolRegistry.unregister` precedent. Conformance tests gate capability-specific tests on the flag; backends that lie produce silent failures rather than loud refusals.

7. **Trust model: shared-catalog backends are CATALOG-shared, NOT TRUST-shared.** Backends running operator-supplied handler source code (the SQLite reference's filepath-stored `.py` handler bodies; any future PyPI / git backend; any HTTP-backed catalog) MUST document the trust boundary at `install()` time — operators are responsible for source vetting BEFORE install. The framework is NOT a sandbox; `install()` is the choice of what code executes in the framework's process. Multi-tenant deployments MUST scope at the **process level** (one framework process per tenant), NOT just at the `agent_scope` column — process-shared catalog + process-shared trust is the wrong shape, and the column boundary doesn't constrain Python execution. The judge layer (spec/28) is the runtime safety net for tool dispatch — judges classify and gate side-effectful tool calls before execution; the registry layer chooses WHICH code is in the process, not what it's allowed to do. Backends MUST NOT claim sandboxing they don't implement (matches spec/24 MUST #8 capability honesty); `supports_sandbox_validate=False` is the honest signal until a sandbox-process model lands. Documented in spec/25 §"Reference implementations" → SQLite trust-model paragraph (plan-subagent Risk K from PR 3).

8. **Connection / handler lifecycle MUST be safe under thread-life-tied cleanup.** Backends MUST be safe to construct, use, and abandon without explicit `close()` — there is no `release()` Protocol method because the framework's call-site lifecycle (one `ToolRegistryBackend` instance per `AtomicAgent` for the agent's full life, plus shared instances threaded through `OutcomeRunner` / `EvalRunner` / `DreamRunner`) doesn't have a deterministic teardown point. Backends with per-thread connections (SQLite, Postgres, MySQL) MUST use `threading.local` so connections accumulate only with thread count, not call count — the reference `SQLiteToolRegistryBackend` uses `threading.local` for file-backed deployments; `:memory:` mode is single-threaded test-only (`check_same_thread=True`) so cross-thread access raises `ProgrammingError` HONESTLY rather than producing silent corruption (the F-1 docstring-lying-about-thread-safety shape from #63 PR 3 — plan-subagent Risk G). Backends storing handler bodies as `.py` files (SQLite reference's hybrid storage; any future hybrid backend) MUST default `handlers_root` to a per-instance `tempfile.mkdtemp()` for `:memory:` deployments (PR 3 Step 11 P2 fix — the original default to `Path.cwd() / .handlers/` contradicted the "non-persistent / test-only" docstring promise AND let two `:memory:` instances clobber each other's on-disk handlers). `handlers_root` validation MUST refuse paths with `<= 1` component after resolution (root-write defense — PR 3 Step 11 P2). Handler module re-import is intentionally NOT cached on the reference filesystem backend (`importlib.util.spec_from_file_location` produces a fresh module per `load_tool` call, NOT populating `sys.modules`) — backends MAY opt to cache (insert into `sys.modules` under a deterministic qualname), but MUST NOT cache the returned `ToolDefinition` instance (callers may mutate fields before registering). The `(handler_path, mtime)` memoization shape is documented as a runner-loop-deployment follow-up (spec/25 §"Known gaps" — `[perf] _import_handler memoization across runner loops`).

The reference `FilesystemToolRegistryBackend` (`atomic_agents/registry/filesystem.py`) and `SQLiteToolRegistryBackend` (`atomic_agents/registry/sqlite.py`) are the canonical examples of this contract. Future PyPI / git / company-internal-HTTP / SaaS-database adapters should mirror these shapes; the parametrized conformance suite (`tests/test_tool_registry_protocol_conformance.py`) runs against every registered backend so the contract is verified by the same tests that pin `list_tools` / `load_tool` / `install` / `uninstall` / `validate` / `capabilities` semantics — no separate "registry-backend" test suite required.

## Reserved future capabilities

These are NOT committed in the locked v1.0 surface but are reserved in the namespace so future expansions don't need a breaking Protocol change:

- **`supports_versioning=True` + `load_tool(name, version=None)` overload.** Future PyPI / git backends honor version pins. Pip-style as the default-target — one-version-per-agent. Conformance tests will gate version-dispatch tests on this capability.
- **`supports_install=True` + `supports_uninstall=True` on the filesystem backend.** Could land as a "filesystem v2" mode that wraps `cp` / `rm` operations behind the Protocol. Today's filesystem-as-source-of-truth principle (CLAUDE.md §1) argues against this; tracked here for completeness.
- **`supports_sandbox_validate=True` + sandboxed `validate(name)`.** Process model TBD (subprocess + resource-cap? container? wasm?). Will land with the operator config for the sandbox itself.
- **`supports_skills_catalog=True` + `list_skills_catalog()` / `load_skill_catalog_body(name)`.** Catalog-side skill surface for PyPI / git / company-internal-HTTP backends. Spec/24's `save_skill` reservation stays the per-agent mounting primitive; this surface is upstream of identity (Decision 2).
- **`AsyncToolRegistryBackend`** — async variant for HTTP-served deployments. Same shape; `list_tools` / `load_tool` become `async def`.
- **`SubscribeToolRegistryBackend`** — adds `subscribe(callback) -> handle` for backends that push catalog-change events. Reserved alongside `supports_subscribe` on `ProfileBackend`.

## Conformance test surface

The conformance suite (parametrized across both reference backends):

- `tests/test_tool_registry_protocol_conformance.py` — **43 conformance test functions parametrized via `BACKEND_FACTORIES`** running across filesystem + SQLite, with capability-gated skips. 18 invocations skip across the matrix (10 filesystem-shape tests skip on SQLite when the test asserts a filesystem-specific behavior; 8 `supports_uninstall=False` path-traversal + idempotent-uninstall variants skip on filesystem). `_capability_parity_install` is rewritten as a real install + verify-in-`list_tools` round-trip after PR 3 Step 11's "tautology" CRITICAL finding. Tests cover: Protocol surface, `backend_id` stability, `capabilities()` shape, `list_tools` empty/populated/lexicographic, `load_tool` returns `ToolDefinition` with callable handler, `load_tool` raises `ToolNotInRegistry` for missing, `validate` static (does NOT execute handler), `validate` reports import + descriptor errors, capability parity for `supports_install` / `supports_uninstall` / `supports_versioning`, classification round-trip on `ToolRef`, version field round-trip (None on both reference backends; field shape is forward-compat), descriptor frontmatter validation, descriptor handler-module-not-found, descriptor handler-not-callable refused at install time (PR 3 Step 11 testing CRITICAL fix), path-traversal on tool name refused at the API boundary, source attribution populated, integration with existing `ToolNameCollision`. The conformance helper `make_tool_in_backend(backend, ...)` uses each backend's Protocol surface for setup (filesystem writes `.md`+`.py` to disk; SQLite calls `install()`) so backend-specific factory differences don't leak into the conformance tests — plan-subagent Risk J from PR 3.
- `tests/test_tool_registry_filesystem_backend.py` — **35 filesystem-specific test functions**: on-disk descriptor parsing, hidden-file exclusion, `tools/` absent → empty list, `tools/__init__.py` ignored, `tools/_helper.py` ignored, handler module re-import semantics, `validate()` warnings vs errors, registry resolution via `register_tool_registry_backend`, `get_default_tool_registry_backend` env-var dispatch + invalid backend_id + credential redaction + truncation, source field shows descriptor's filesystem path, plus Step 11 regression coverage (256 KB descriptor size cap defending against YAML alias-bomb DoS, control-character refusal in tool names defending against log injection, empty/null frontmatter rejection, `agent_root=''` / `agent_root='.'` refusal at construction, `chmod-000 tools/` treated as empty rather than `PermissionError`-crashing every agent construction).
- `tests/test_tool_registry_sqlite_backend.py` — **52 SQLite-specific tests**: schema creation + version row + idempotent cold-start race (Risk B / spec MUST #5), multi-process WAL race resolved by `PRAGMA busy_timeout=5000` before WAL pragma (PR 3 Step 11 P1 REPRODUCED 3/5 fix), WAL journal mode probe, in-memory `RuntimeWarning` + non-durable + single-threaded `check_same_thread=True`, install round-trip + duplicate-install raises `ToolAlreadyInstalled`, install rejects non-callable handler at install time (PR 3 Step 11 testing CRITICAL fix), install rejects non-None version when `supports_versioning=False` (plan-subagent Risk L), uninstall idempotency, install TOCTOU race regression (winner's handler file survives — PR 3 Step 11 P1 REPRODUCED 50/50 fix), cross-scope isolation (agent A install doesn't appear for agent B list / load / uninstall), URL parsing (`?agent_scope=` query param + refused netloc / non-sqlite scheme / fragments / duplicate query params / unknown query params), URL factory credential redaction (PR 3 Step 11 P1 — postgres URLs no longer leak passwords in `ValueError` messages), `:memory:` `handlers_root` defaults to per-instance tempdir (PR 3 Step 11 P2), `handlers_root` refuses `<= 1`-component paths (PR 3 Step 11 P2 root-write defense), constructor parent-dir creation, registry resolution + env-var dispatch.
- `tests/test_tool_registry_integration.py` — **20 wiring tests**: `AtomicAgent.tool_registry_backend` public attribute, kwarg override beats env var, backend tools register into `agent.tool_registry`, operator-passed `tools=` wins on collisions, runner threading via monkeypatch on OutcomeRunner / EvalRunner / DreamRunner (catches the kwarg-drop trap from #61/#63 PR 2 prior arcs), delegate non-threading verified by monkeypatch (delegate.py deliberately does NOT thread tool_registry_backend — per-agent scoping per spec/25 Decision 9), doctor PASS / WARN / FAIL paths + capability snapshot + tool-count probe + URL credential redaction, per-agent `tools/` isolation, fault-injection regressions (chmod-000 tools/, control-char filename, broken-handler module).

Across the arc the full suite went 1750 → **1953 passing** + 34 skipped on Python 3.11/3.12 under full CI extras (`uv sync --extra dev --extra openai --extra validation --extra redis`). The 4 test files above contribute 43 + 35 + 52 + 20 = **150 functions** that parametrize to **221 invocations** (conformance ×2 backends, path-traversal-input fan-out on uninstall, etc.); 18 skip on capability gates (10 filesystem-shape tests skip on SQLite, 8 `supports_uninstall=False` variants skip on filesystem), 203 pass — which is exactly the +203 arc delta.

## Related

- spec/20 — `MemoryBackend` (the original Protocol pattern; this spec mirrors its shape).
- spec/21 — `LockBackend` (immediate-sibling template; this spec mirrors its `types.py`/`backend.py`/registry split and operator-surface rationale).
- spec/22 — `LogBackend` (sibling template; this spec follows its DRAFT → lock cadence + Implementer contract structure).
- spec/24 — `AgentProfileBackend` (freshest locked spec; this spec mirrors its scaffolding shape and Decision 1-10 articulation).
- spec/28 — `JudgeBackend` (third-template; the tool-registry arc adopts the same "lock spec at PR 4" discipline and `classification` round-trip).
- spec/31 — `LLMBackend` (second-template; this spec mirrors its types/backend split).
- spec/17 — In-memory `ToolRegistry` + `ToolDefinition` (the dispatch-layer type the discovery layer materializes into).
- spec/19 — MCP (the carve-out target — Decision 3 keeps MCP separate; tracked in [#201](https://github.com/dep0we/atomic-agents-stack/issues/201)).
- CLAUDE.md §1 — "The vault is the source of truth." The filesystem backend's edit-`<agent>/tools/`-directly UX (Decision 7) is in service of this rule.
- CLAUDE.md §3 — "Layers compose; they don't merge." Decisions 2 + 3 enforce this for the skill + MCP carve-outs.
- CLAUDE.md §6 — "Progressive disclosure by default." `ToolRef` metadata + lazy `load_tool` (Decision 5) is in service of this rule.
- Issue [#64](https://github.com/dep0we/agent-stack/issues/64) — motivation + acceptance criteria.

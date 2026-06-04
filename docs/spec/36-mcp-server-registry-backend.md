# spec/36: MCPServerRegistryBackend Protocol

> **Status:** LOCKED at PR 5 (v1.0.0). HTTP write paths shipped; conformance suite covers all 10 MUSTs across both reference implementations.

---

## Origin

Carved out from [#64](https://github.com/dep0we/atomic-agents-stack/issues/64) (`ToolRegistryBackend`) per spec/25 Decision 3. Filed as [#201](https://github.com/dep0we/atomic-agents-stack/issues/201) before #64 PR 4 merged. The twelfth and final backend protocol for v1.0.

**Current arc state (2026-06-01 EOD):** Eleven of twelve backend protocols shipped. CorpusBackend (#65) arc closed at PR #316 (`e91286c`); only MCPServerRegistryBackend remains for v1.0. The seven-clean-ship streak in the post-#285-revert period (#286, #293, #294, #297, #298, #304, #316) holds; PR 1 of this arc extends the streak to 8.

**Cross-links:**
- spec/19. MCP integration. `MCPServerSpec` dataclass shape unchanged. `MCPClientPool` keeps subprocess lifecycle.
- spec/21. LockBackend. `LockBackend.acquire(name, timeout)` signature used in filesystem install/uninstall.
- spec/24. AgentProfileBackend. `mcp_md_raw` snapshot pattern unchanged; `mcp_servers_resolved` sibling field added (Decision 9).
- spec/25. ToolRegistryBackend. Decision 3 carve-out is the origin of this arc.
- spec/32. PolicyBackend. `_policy_backend_was_explicit` delegate-threading precedent.
- spec/33. PersonaBackend. Most recently locked spec; Implementer Contract structure used as template.
- spec/34. CorpusBackend. `@property capabilities` declaration and 9-MUST contract used as template.

---

## Shipping plan (5 PRs, revised per Decision 6)

- **PR 1.** Protocol scaffolding + dataclasses + `FilesystemMCPServerRegistryBackend` reference impl + spec/36 DRAFT + ~60 conformance and filesystem-specific tests + `atomic-agents mcp-registry list/show/validate/refresh-capabilities` CLI (read-only subcommands).
- **PR 2.** `agent.py:_load_config()` wiring + per-runner kwargs on `OutcomeRunner`/`EvalRunner`/`DreamRunner` + `delegate.py` explicit-only threading + `AgentProfile.mcp_servers_resolved` sibling field (spec/24 addendum) + IRON RULE byte-identical regression suite + `doctor.check_mcp_server_registry_backend` coherence check + ~35 tests. Filesystem-only at this PR; the audit-snapshot pattern is proven before HTTP introduces additional risk.
- **PR 3.** Filesystem CLI install/uninstall subcommands + `_io.atomic_write` + `LockBackend` lease integration + ~15 tests. Filesystem-only write path; the LockBackend acquisition pattern is proven before HTTP introduces wire-format atomicity concerns.
- **PR 4.** `HTTPMCPServerRegistryBackend` reference impl (read-only mode: `list/load/load_all/validate`) + tier-1/2/3 capability negotiation + lazy probe with `probe_failure_cache_s` + fail-closed wiring + auth (bearer token via env var) + URL credential redaction + ~31 tests. HTTP backend arrives with the audit pattern and LockBackend pattern already proven.
- **PR 5.** HTTP install/uninstall (write path with capability gating) + tier-3 audit capability advertisement + spec/36 LOCKED at 10 MUSTs + arc-closer CHANGELOG + status flip to "Twelve of twelve backend protocols shipped" across CLAUDE.md, README.md, ROADMAP.md + v1.0 RELEASE candidate.

---

## Overview

`MCPServerRegistryBackend` is the **twelfth** open Protocol in the protocol-pattern series (Memory, LLM, Judge, Lock, Log, AgentProfile, ToolRegistry, Mandate, Policy, Persona, Corpus, **MCPServerRegistry**). It abstracts the set of MCP servers this agent is configured to use behind a Protocol so the framework's core stays small and alternate catalog substrates (HTTP-backed, SaaS tenant catalog, org-internal registry) drop in without forking.

`AtomicAgent` exposes `agent.mcp_server_registry_backend: MCPServerRegistryBackend`. The existing `MCPClientPool` (`atomic_agents/mcp.py:126`) stays as the runtime subprocess-lifecycle class. The two seams compose:

```
backend.list_mcp_servers()       -> list[MCPServerRef]     # this agent's mounted servers (metadata)
backend.load_mcp_server(name)    -> MCPServerSpec           # one mounted server (full spec)
backend.load_all_mcp_servers()   -> list[MCPServerSpec]     # all this agent's mounted specs (bulk)
agent.mcp_pool = MCPClientPool(specs, agents_root)          # subprocess lifecycle (spec/19, unchanged)
```

The Protocol is **not** a generic MCP-server-management API and is **not** an org-wide catalog browser. It is the minimal contract the framework needs to keep the markdown-as-config aesthetic (CLAUDE.md §7) and the audit trail invariant (CLAUDE.md §5) intact while making SaaS, org, and public-registry futures real at v1.0.

**Backwards-compatibility promise.** Existing `<agent>/mcp.md` files work unchanged. Filesystem backend wraps the existing `parse_mcp_md()` semantics. All 166 existing `AtomicAgent(...)` construction sites observe byte-identical behavior when no backend is configured. Wiring lands in PR 2.

---

## Why MCP servers need a Protocol seam

Today, an atomic agent's MCP server set is declared in `<agent>/mcp.md` per spec/19. The `parse_mcp_md()` function at `atomic_agents/mcp.py:410` walks the file and produces a `list[MCPServerSpec]`; `MCPClientPool(_specs, agents_root)` consumes the list, manages subprocess lifecycle (connect_all in try, disconnect_all in finally), and registers MCP-provided tools into the agent's `ToolRegistry`. This works on a single host where each operator hand-maintains their agent's `mcp.md`.

It breaks the moment the deployment shape becomes:

- **SaaS multi-tenant catalog.** A hosted dashboard publishes a vetted set of MCP servers per tenant; each tenant's agents subscribe. The framework needs a catalog API, not a per-agent file walk.
- **Org-internal vetted registry.** A company posts a manifest of approved MCP servers with mandatory audit logging, capability flags, and allow/deny semantics. Agents subscribe over HTTP. The framework needs to consume the catalog without owning storage.
- **Public registry / package-registry-style discovery.** The MCP ecosystem is publicly discussing a "server registry" protocol analogous to npm or PyPI for MCP servers. Agents discover available servers over HTTP. The framework needs a catalog Protocol that survives upstream protocol evolution.
- **Cross-agent install audit.** "Operator A enabled server X for tenant T at 2026-04-01" needs a catalog-level audit trail. Today's per-agent `mcp.md` doesn't have one.

The Protocol is the **mounted server source** seam for MCP: it returns the set of MCP servers THIS AGENT uses, scoped per-agent via `agent_root` (filesystem) or `agent_scope` (HTTP / SaaS). The catalog-server's view of "what's available org-wide" lives upstream of the framework in the catalog server's own admin tooling; the framework's HTTP backend consumes only the agent's mounted subset.

---

## Module layout

```
atomic_agents/mcp_registry/
├── __init__.py        # registry: register_mcp_server_registry_backend /
│                      # get_mcp_server_registry_backend /
│                      # list_mcp_server_registry_backends +
│                      # get_default_mcp_server_registry_backend factory +
│                      # _redact_for_error_message helper
├── types.py           # canonical types: MCPServerRef, MCPServerRegistryCapabilities,
│                      # ValidationResult
├── backend.py         # MCPServerRegistryBackend Protocol contract + exception classes +
│                      # _default_load_all helper
├── filesystem.py      # FilesystemMCPServerRegistryBackend reference implementation
└── http.py            # HTTPMCPServerRegistryBackend reference implementation +
                       # tier negotiation
```

Package name `mcp_registry` (not `mcp` since spec/19 owns that; not `registry` since spec/25 owns that). The split into `types.py` separate from `backend.py` matches the precedent across `profile/`, `logs/`, `registry/`, `corpus/`, `persona/`.

Per the project's `register_backend_placement_convention` learning (2026-05-29): `register_mcp_server_registry_backend` lives in `__init__.py` alongside the factory and redaction helper, matching the dominant pattern (6 of 10 backends including `locks/`, `logs/`, `profile/`, `registry/`, `judge/`, and `corpus/`).

---

## Load-bearing design decisions

These decisions are surfaced by reading the existing MCP code surface (`atomic_agents/mcp.py:84` for `MCPServerSpec`; `agent.py:2337` for the discovery overlay; `parse_mcp_md` for env-var resolution and path-traversal validation) and pressure-tested against the recurring trap shapes from the 11 prior arcs. Each has a Why clause that future contributors and alternate backend authors need before changing the shape.

### Decision 1: `MCPServerRegistryBackend` owns the mounted-server-source layer; `MCPClientPool` keeps subprocess lifecycle

`atomic_agents/mcp.py:MCPClientPool` (line 126) is the per-agent subprocess pool used during `call()` (connect_all in try; disconnect_all in finally). It stays. `MCPServerRegistryBackend` lives one level above: it is the **mounted server source** (what MCP servers THIS AGENT uses) that produces `MCPServerSpec` instances which then get consumed by the pool at agent construction.

**Per-agent scoping is structural, not optional.** Filesystem backend is scoped to `agent_root` (one `mcp.md` per agent). HTTP backend is scoped to `agent_scope` (every wire request includes `?agent_scope=<scope>`; the catalog server filters to the mounted subset for that scope server-side). A backend that returns the org-wide "what's available to install" surface from `list_mcp_servers()` is non-conformant; that is the catalog server's own admin tooling, not the framework's responsibility.

**Why:** spec/19's `connect_all` / `disconnect_all` discipline is locked. The `finally`-block teardown invariant prevents process leaks; replacing it would touch every code path that spawns or tears down a subprocess. Mounted-server-source and lifecycle are different concerns; merging them violates the layers-compose principle (CLAUDE.md §3) and inflates the Protocol surface with subprocess-management methods that filesystem-backed sources don't need. The pattern parallels `ToolRegistryBackend` (spec/25): the Protocol covers which tools this agent uses, not the in-memory invocation machinery.

**Framework-level behavior on catalog unreachable:** `_load_config()` MUST re-raise `MCPRegistryUnavailable` if `load_all_mcp_servers()` fails at agent construction (fail-closed per Decision 1). No silent fallback to a different backend; no soft-degrade to empty pool. Operator's explicit backend pin is respected. The operator-facing error message names the resolved backend with credentials redacted per MUST 4.

### Decision 2: `mcp.md` is the filesystem reference's file format, NOT the universal Protocol interface

Today's `<agent>/mcp.md` (spec/19) stays unchanged. Future `HTTPMCPServerRegistryBackend` / `SaaSMCPServerRegistryBackend` / `OrgRegistryMCPServerRegistryBackend` backends do NOT need an `mcp.md` file. They use a CLI (`atomic-agents mcp-registry install`) or a dashboard. The mcp.md format is preserved as the filesystem reference's documented file format, not as the Protocol's universal interface.

**Why:** the catalog is what's available; the file is one of many possible storage shapes. Locking mcp.md as the universal interface would force a SaaS deployment to invent a file just to satisfy the Protocol. Same shape as `ToolRegistryBackend` (filesystem reads `<agent>/tools/<name>.{md,py}`; SQLite reads rows; HTTP reads JSON). Markdown-as-config (CLAUDE.md §7) is a per-backend aesthetic, not a Protocol-wide constraint.

### Decision 3: Two reference implementations, filesystem and HTTP-client

The Protocol arc ships two reference backends:

- **`FilesystemMCPServerRegistryBackend(agent_root, read_paths)`:** wraps the existing `parse_mcp_md()` semantics; provides atomic install/uninstall via `_io.atomic_write` of `mcp.md`.
- **`HTTPMCPServerRegistryBackend(catalog_url, agent_scope, *, auth_token=None, request_timeout_s=10)`:** talks JSON over HTTPS to a catalog server; adapts to multiple server tiers via capability negotiation (Decision 4).

**Why:** matches the README's existing v1 promise. The HTTP-client reference makes "behind an HTTP service" concrete for the first time. SQLite was considered as the second ref impl (matches the 11-arc precedent) but rejected because MCP catalogs in production are HTTP-shaped, not SQLite-shaped. The three non-filesystem futures (SaaS tenant catalog, org-internal vetted registry, public package registry) are all HTTP. Shipping SQLite would have been precedent-following without serving the actual user shape. Per `feedback_atomic_agents_best_not_cheapest`: default to BEST. HTTP is BEST here.

### Decision 4: HTTP backend uses tier-negotiated capability handshake; ONE implementation adapts to multiple server tiers

The framework's HTTP backend works against three documented catalog-server shapes:

- **Tier 1: Read-only public catalog.** Server implements `GET /mcp-servers` and `GET /mcp-servers/<name>` (REQUIRED). `GET /mcp-servers/<name>/validate` is OPTIONAL; if absent, HTTP backend returns `ValidationResult(ok=False, errors=["catalog server does not implement /validate; cannot statically validate"], warnings=[])`. Auth optional. Use case: a company posts a manifest of vetted MCP servers behind nginx; tenants read it; install lives elsewhere.
- **Tier 2: Read-write authenticated catalog.** Tier 1 plus `POST /mcp-servers` and `DELETE /mcp-servers/<name>`. Bearer-token auth required. Per-tenant scoped storage via the `agent_scope` constructor argument. Use case: SaaS deployment where each tenant has their own catalog; framework's CLI is the canonical install surface.
- **Tier 3: Admin catalog with capability advertisement.** Tier 2 plus `GET /capabilities` (advertises audit logging, custom capability flags, server-side validation policies). Use case: enterprise deployments with compliance requirements.

The framework ships ONE `HTTPMCPServerRegistryBackend` implementation that probes capabilities at first non-construction call (lazy; per MUST 2 in the Implementer Contract). The probe is side-effect-free and uses HTTP-standard method-discovery semantics.

**Capability probe sequence (deterministic, side-effect-free):**

1. `GET /capabilities`: if 200, parse server tier from response body. Expected JSON shape: `{"tier": <int 1-3>, "supports_install": bool, "supports_uninstall": bool, "supports_audit": bool, "wire_version": "<semver>"}`. This is authoritative; values from this endpoint win.
2. If `GET /capabilities` returns 404 (server doesn't implement the endpoint), fall back to method discovery via `OPTIONS /mcp-servers`. The HTTP-standard `Allow` response header lists supported methods. Tier inference: `Allow: GET` only means tier 1 (read-only); `Allow: GET, POST, DELETE` means tier 2 (read-write). Tier 3 is only detectable via the explicit `GET /capabilities` endpoint; absence of `/capabilities` implies the server is at most tier 2.
3. If `OPTIONS /mcp-servers` returns 404 or 405 (server doesn't implement OPTIONS), default to **tier 1 (read-only)**. Conservative fallback; the operator can run `atomic-agents mcp-registry refresh-capabilities` after confirming the catalog server supports more.
4. If `GET /capabilities` OR `OPTIONS /mcp-servers` returns 5xx or fails with a network error, raise `MCPRegistryUnavailable`. Backend caches the failure for `probe_failure_cache_s` seconds before re-probing on the next call; explicit `.refresh_capabilities()` always bypasses the cache.
5. If `GET /capabilities` returns 401 (auth required and `auth_token` absent), raise `MCPRegistryAuthRequired`. Caching applies.

The backend's `capabilities` property returns the **runtime view** reflecting what the connected server actually allows. Static class capabilities (`supports_install=True` as the class default) are distinct from runtime capabilities (`supports_install` may report False if the catalog server is tier 1).

**Mid-session tier regression (cached capability stale).** If a tier-2 server later regresses to tier 1 (e.g., admin disables writes), the backend's cached capability is stale. Behavior contract: a stale `supports_install=True` followed by a `POST /mcp-servers` that returns 405 from the now-tier-1 server triggers an inline re-probe (one extra round-trip), updates the cached capabilities, and raises `NotImplementedError` to the caller (consistent with how a statically-False capability behaves). No silent retry; the operator-facing error message names the tier change explicitly.

**Edge case: re-probe returns tier 2 after a 405.** If the re-probe after a 405 still returns tier 2 (the server claims write support despite having returned 405), the backend MUST trust the original 405 and raise `NotImplementedError` with an "inconsistent server" message. NO retry or second attempt. The operator must investigate the catalog server. Rationale: adding a silent retry loop in this case creates an unbounded retry hazard when the catalog server is in a transitional or misconfigured state. The safe default is fail-loud with clear operator direction.

**Why:** the wire-format-divergence risk vs. upstream MCP ecosystem registry-protocol discussions becomes manageable when the framework's HTTP backend can adapt across multiple server shapes. When upstream MCP ships a registry protocol, it slots in as another tier (tier 4+) without breaking tiers 1-3. spec/36 v1.0 documents the spectrum; future tiers are additive, not revisions. This is the structural escape hatch, not just a soft mitigation.

### Decision 5: Unified install path across all backends, with capability flags that evolve as methods land

Both reference implementations target `supports_install=True` as the **eventual static class default**, but the flag flips True only at the PR where the method ships. Capability honesty (MUST 3) is preserved at every PR by aligning the static class default with the method's implementation state:

| Backend | PR 1 | PR 2 | PR 3 | PR 4 | PR 5 |
|---|---|---|---|---|---|
| `FilesystemMCPServerRegistryBackend.capabilities.supports_install` | False (no install method yet) | False (still no method) | **True (method lands)** | True | True |
| `HTTPMCPServerRegistryBackend.capabilities.supports_install` | n/a | n/a | n/a | **False static class default** (read-only mode at PR 4) | **True after successful tier-2+ negotiation; static pre-probe default stays conservative False/False per B-F11; tier-1 negotiated outcome stays False/False (a tier-1 catalog does not support writes)** |

```
atomic-agents mcp-registry install github  # works on filesystem from PR 3 onward
atomic-agents mcp-registry install github  # works on HTTP tier 2+ from PR 5 onward
```

**Why staggered class defaults:** MUST 3 (capability honesty) says if `supports_install=True` is reported, calling `install()` must not raise `NotImplementedError`. Shipping `supports_install=True` at PR 1 while `install()` is unimplemented would be a capability lie; the conformance suite would have to `@pytest.mark.skip` MUST 9 tests to mask it. Instead: the static class default flips True at the PR where the method first works. The conformance suite runs all 10 MUST tests at every PR; MUST 9 tests trivially pass at PR 1-2 (capability False; conformance asserts the absent-method path raises `NotImplementedError`) and become meaningful at PR 3. No skip masks; no capability lies in any PR's snapshot.

**Why:** same operator UX across every deployment shape. The framework's CLI is the canonical install surface. The README's "same agent definitions, same call flow, different backends" promise extends to operator commands at v1.0. Per `feedback_no_scaling_down_in_planning`, the full vision is the target.

### Decision 6: Static-vs-dynamic capability flags

Capability flags on `MCPServerRegistryCapabilities` distinguish two concepts:

- **Static (class-level):** what the backend's class is capable of doing. `FilesystemMCPServerRegistryBackend` and `HTTPMCPServerRegistryBackend` both static-support install and uninstall.
- **Dynamic (runtime):** what the connected resource actually allows right now. For filesystem, static equals dynamic (no remote dependency). For HTTP, dynamic depends on the catalog server's tier; the backend negotiates and reflects the truth.

**Why:** filesystem backend has no remote dependency, so its `capabilities` property is a constant. HTTP backend's runtime depends on the catalog server's response to the capability probe; static-only capability flags would lie. The capability honesty MUST (#3 in the Implementer Contract) requires claim-vs-behavior parity, which means HTTP backend's `capabilities` property MUST reflect the runtime, not the static class default. This is novel relative to the 11 prior arcs (which all had static-equals-dynamic capabilities). The Implementer Contract documents it explicitly so future Postgres / SaaS / Redis backends know which kind of capability flags they own.

### Decision 7: Env-var references resolve at `load_mcp_server` time on ALL backends

Today's `parse_mcp_md()` resolves `$VAR` references in `env:` lines at parse time, against the framework's process environment. The resolved values land in `MCPServerSpec.env`. Under the Protocol:

- All backends MUST resolve `$VAR` references at `load_mcp_server(name)` time, against the client process environment.
- HTTP catalog servers MAY store unresolved `$VAR` strings (the wire format permits them); the framework's HTTP client resolves at materialization.
- Filesystem backend preserves today's behavior but moves the resolution from `parse_mcp_md` time to `load_mcp_server` time.

**Why:** substrate-agnostic security. An HTTP catalog server returning resolved values from its OWN process env would silently leak secrets across deployment boundaries (the server's `$GITHUB_PAT` is not the client's `$GITHUB_PAT`). Client-side resolution is the only secure default.

**Implementation note (spec/19 addendum):** the existing `parse_mcp_md_text()` at `atomic_agents/mcp.py:432` resolves `$VAR` references inline inside `_build_spec()` at `mcp.py:558-566`. PR 1 adds an optional `resolve_env: bool = True` parameter to `parse_mcp_md_text` (and `_build_spec`); existing callers (`parse_mcp_md`, `doctor.py:678`) keep the True default and observe byte-identical behavior. `FilesystemMCPServerRegistryBackend` calls with `resolve_env=False` so spec values come back with unresolved `$VAR` strings, then performs resolution itself inside `load_mcp_server(name)`. spec/19 gets an addendum note pointing to spec/36's resolution-timing contract; no normative change to spec/19's existing "resolved at parse time" claim for direct `parse_mcp_md` callers.

### Decision 8: Path-traversal validation moves inside `load_mcp_server` on the filesystem backend

Today `parse_mcp_md(read_paths)` validates path-shape args at parse time (per spec/19 §"Path-traversal best-effort"). Under the Protocol:

- Filesystem backend captures `read_paths` at construction (`FilesystemMCPServerRegistryBackend(agent_root, read_paths)`).
- `load_mcp_server(name)` applies the path-traversal check at materialization time.
- The Protocol surface `load_mcp_server(name) -> MCPServerSpec` does NOT take a `read_paths` parameter; per-backend security policy can vary.

**Why:** the universal Protocol surface stays clean. `read_paths` is a filesystem-only concept (HTTP catalog server's responses arrive as text, with no filesystem-path semantics; the catalog server is trusted to have validated its own content). Leaking `read_paths` into the Protocol would force HTTP / SaaS backends to fake a value they don't have.

### Decision 9: `AgentProfile` gains a sibling `mcp_servers_resolved` field; `mcp_md_raw` stays for backward-compat (spec/24 D1 addendum)

The existing per-agent snapshot discipline (`AgentProfile.mcp_md_raw` captures the unresolved mcp.md text at snapshot time, per spec/24 Decision 1) stays unchanged for filesystem backend. For HTTP / SaaS backends, `mcp_md_raw` would be empty/`None` and the audit snapshot would NOT record what specs the agent actually ran with. That breaks the framework's audit-as-structural invariant (CLAUDE.md §5).

**Fix (spec/24 addendum, additive):** AgentProfile gains a sibling field:

```python
mcp_servers_resolved: list[MCPServerSpec] = field(default_factory=list)
```

`agent.py:_load_config()` populates this field with the materialized MCPServerSpec list (the same list passed to `MCPClientPool`) at agent construction, BEFORE the pool consumes them. For filesystem: both fields populate; the audit trail has redundant information (text and parsed specs). For HTTP: only `mcp_servers_resolved` populates; the audit trail still answers "what specs did this agent run with at time T?"

**Why:** the framework promises "audit trail is structural" (CLAUDE.md §5) and "same agent definitions, same call flow, same audit trail, different backends" (README throughline). A sibling field substrate-agnostic-by-construction is the minimal additive fix. spec/24 LOCK gets a small addendum; the existing `mcp_md_raw` field stays for backward-compat so legacy callers see byte-identical behavior on filesystem.

---

## Canonical types

All types are defined in `atomic_agents/mcp_registry/types.py`.

### `MCPServerRef`: the unit of catalog listing

```python
@dataclass(frozen=True)
class MCPServerRef:
    name: str                              # operator-chosen short name (matches MCPServerSpec.name)
    description: str = ""                  # operator-readable note (defaults to empty per MCPServerSpec parity)
    transport: str = "stdio"               # catalog filtering by transport
    version: str | None = None             # reserved (matches ToolRegistry Decision 4)
    source: str = ""                       # backend-specific origin marker
```

`MCPServerRef.to_dict()` / `from_dict()` round-trip is byte-shape preserving for every field. The Ref carries metadata only; it does NOT include `command` / `args` / `env` (those are part of the materialized `MCPServerSpec` from spec/19, returned by `load_mcp_server(name)`). This lazy/eager distinction matches `ToolRegistryBackend` Decision 5.

**Operator note: MCPServerRef.source contains the raw catalog URL.** If the operator embedded credentials in `catalog_url` (e.g., `https://user:pass@host/`), they appear in `source` verbatim. Downstream consumers (CLI output, audit log persistence, dashboard rendering) MUST redact this field before display or storage. Use `_redact_for_error_message(ref.source)` from `atomic_agents.mcp_registry`. The raw URL form is intentional for navigation use cases.

**Projection from `MCPServerSpec`:** `install(spec) -> MCPServerRef` constructs the returned Ref by projecting `name`, `description`, `transport` from the input `MCPServerSpec`; `version` defaults to None; `source` is set by the backend (e.g., filesystem returns `source=f"mcp.md#section:{name}"`; HTTP returns `source=f"{catalog_url}/mcp-servers/{name}"`). The projection is mechanical; the conformance suite asserts the round-trip.

### `MCPServerRegistryCapabilities`

```python
@dataclass(frozen=True)
class MCPServerRegistryCapabilities:
    supports_install: bool                 # filesystem: static True (at PR 3+); HTTP: dynamic per tier
    supports_uninstall: bool               # filesystem: static True (at PR 3+); HTTP: dynamic per tier
    supports_capability_handshake: bool    # True only on HTTP backend (Decision 4)
    supports_audit: bool                   # reserved; tier-3 HTTP servers flip True; both ref impls return False at v1.0
    durable: bool                          # True on both ref impls
                                           # durable=False is reserved for future in-memory test-fixture backends
```

Conformance tests assert claim-vs-behavior parity (per Implementer Contract MUST 3). Mirrors `ProfileCapabilities` / `LogCapabilities` / `CorpusCapabilities` precedent.

### `ValidationResult`

Same shape as `ToolRegistryBackend` (spec/25 §"Canonical types"):

```python
@dataclass(frozen=True)
class ValidationResult:
    ok: bool                               # equivalent to `not errors`
    errors: list[str]                      # server unusable
    warnings: list[str]                    # server usable but flagged
```

---

## `MCPServerRegistryBackend` Protocol surface

```python
@runtime_checkable
class MCPServerRegistryBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    # Core discovery (always implemented)
    def list_mcp_servers(self) -> list[MCPServerRef]: ...
    def load_mcp_server(self, name: str) -> MCPServerSpec: ...   # MCPServerSpec from spec/19
    def load_all_mcp_servers(self) -> list[MCPServerSpec]: ...   # bulk; default impl iterates list+load
    def validate(self, name: str) -> ValidationResult: ...

    # Capability-gated lifecycle
    def install(self, spec: MCPServerSpec) -> MCPServerRef: ...
    def uninstall(self, name: str) -> None: ...

    @property
    def capabilities(self) -> MCPServerRegistryCapabilities: ...

    def refresh_capabilities(self) -> MCPServerRegistryCapabilities: ...
    def close(self) -> None: ...
```

**Property vs method:** `capabilities` is a `@property` (read like an attribute: `backend.capabilities.supports_install`); `refresh_capabilities()` is a method (call to trigger re-probe: `backend.refresh_capabilities()`). This matches CorpusBackend at `corpus/backend.py:70-71`. All design call sites and test assertions use the property syntax `backend.capabilities.supports_X`, NOT `backend.capabilities().supports_X`.

`load_all_mcp_servers()` exists for the agent-construction hot path (avoids N+1 HTTP round-trips on materialization). Default impl in `mcp_registry/backend.py`:

```python
def _default_load_all(backend: MCPServerRegistryBackend) -> list[MCPServerSpec]:
    return [backend.load_mcp_server(ref.name) for ref in backend.list_mcp_servers()]
```

Backends MAY override for performance; HTTP backend overrides with a single bulk GET (`GET /mcp-servers?expand=spec`). Backends overriding MUST preserve consistency: `load_all_mcp_servers()` output is semantically equivalent to the default-impl iteration (MUST 10).

`refresh_capabilities()` is on the Protocol surface (not HTTP-backend-specific) so the CLI does not duck-check. Filesystem implementation: returns the cached static `MCPServerRegistryCapabilities` instance (no-op refresh; static capabilities don't change). HTTP implementation: re-runs the capability probe sequence (bypassing any cache) and returns the updated runtime view.


### `list_mcp_servers` semantics

- Returns `[]` (NOT raise) when catalog is empty or backing resource is absent.
- Lexicographic order by `name`. Database / HTTP backends MUST sort consistently.
- Cheap by construction: no subprocess spawn, no handler import, no MCP server connection.

### `load_mcp_server` semantics

- Returns a fully-populated `MCPServerSpec` (the spec/19 dataclass) with all 6 fields populated.
- MUST raise `MCPServerNotInRegistry` when `name` is absent from the catalog.
- MUST validate `name` against path-traversal at the API boundary BEFORE any backend access.
- MUST resolve `$VAR` env-var references against the client process environment (per Decision 7).
- Unresolvable `$VAR` reference raises `MCPServerConnectFailed` (matches spec/19's existing exception shape; the registry layer does not invent a new exception class for env-var failures).

### `validate` semantics

- Static check; does NOT spawn the MCP server subprocess (matches `ToolRegistryBackend` Decision 6).
- Filesystem implementation checks: descriptor parses; `command` exists on PATH (best-effort `shutil.which`); `$VAR` references resolve; `transport` value is recognized.
- HTTP implementation: calls `GET /mcp-servers/<name>/validate`; relays the server's verdict.
- Returns `ValidationResult(ok, errors, warnings)`. MUST NOT raise on missing server (returns `ValidationResult(ok=False, errors=["server 'X' not in registry"], warnings=[])` instead).

### `install` / `uninstall` semantics

Unlike `ToolRegistryBackend.install(source: str, version: str | None = None)` which takes a package source string for PyPI/git discovery, `MCPServerRegistryBackend.install(spec: MCPServerSpec)` takes the full spec because MCP servers don't have package-index sources at v1.0.

- Both MAY raise `NotImplementedError` when the runtime capability flag (`capabilities.supports_install` / `supports_uninstall`) reports False. Filesystem always allows (at PR 3+); HTTP depends on tier.
- `install(spec)` MUST be atomic at the server-name level (per MUST 9). Filesystem: atomic edit of `mcp.md` via `_io.atomic_write` with collision detection. HTTP: `POST /mcp-servers` returning 409 on collision; backend translates to `MCPServerAlreadyInstalled`.
- `uninstall(name)` MUST be idempotent. Uninstalling a name that doesn't exist is a no-op (no exception). Matches `ToolRegistryBackend` uninstall precedent.
- Both MUST validate `name` against path-traversal at the API boundary.

### `capabilities` semantics

- Returns `MCPServerRegistryCapabilities` reflecting the **runtime** view.
- Filesystem: static (constant across the backend's lifetime).
- HTTP: dynamic; reflects connected catalog server's tier. Lazy probe at first non-construction call; explicit `.refresh_capabilities()` method available for operators who want to re-probe after a server upgrade.

### `close` semantics

- Idempotent (matches MUST 6).
- Filesystem: no-op (no resources to release).
- HTTP: closes the underlying `httpx.Client()`.

---

## Reference implementations

### `FilesystemMCPServerRegistryBackend`

Ships at PR 1 (read paths) and PR 3 (install/uninstall).

```python
FilesystemMCPServerRegistryBackend(
    agent_root: Path,
    read_paths: list[Path],
    *,
    lock_backend: LockBackend | None = None,
    install_lock_timeout: float = 30.0,
)
```

- `agent_root` is the agent's directory. mcp.md lives at `<agent_root>/mcp.md`.
- `agent_root` MAY not exist at construction (matches `FilesystemToolRegistryBackend` precedent). `list_mcp_servers()` returns `[]` for missing / empty mcp.md.
- `read_paths` is the list of paths the agent declares it may read (from `tools.md`). Used by `load_mcp_server(name)` to apply path-traversal validation (per Decision 8).
- `lock_backend` (per MUST 9): if absent, defaults to `get_default_lock_backend(agent_root)` from `atomic_agents.locks` (respects the `ATOMIC_AGENTS_LOCK_BACKEND` env var so multi-host operators on Cloud Run / Kubernetes pinning `=redis` get a `RedisLockBackend` automatically; single-host operators get `FilesystemLockBackend(agent_root)`). The lock is acquired with `name="mcp_registry"` which the filesystem backend maps to `<agent_root>/.mcp_registry.lock`. This lock file is **distinct** from the agent's main `.lock` (which the runtime acquires inside `agent.call()` for cost-cap and run-id serialization); the two locks never overlap and cannot deadlock even on non-reentrant `LockBackend` implementations. Operators passing a custom `lock_backend` MUST scope it to a registry-specific resource, NOT the agent's main lock; the docstring on the constructor parameter calls this out explicitly and names the failure mode (passing the same backend used for `agent.call()` raises `LockBusy` whenever `agent.call()` is in flight, because both operations would compete for the same lock resource).
- `install_lock_timeout` (per MUST 9): seconds to wait for the registry lock during `install` and `uninstall` before raising `MCPRegistryUnavailable`. Default 30 seconds matches typical operator-CLI patience. CI pipelines that want fail-fast set `install_lock_timeout=0.0`; NFS-mounted deployments with slow filesystems may raise it. The kwarg is per-instance and immutable post-construction (mirrors the `apply_staging_lock_timeout` precedent on `FilesystemBackend` per spec/21).
- Reuses `parse_mcp_md_text()` from `atomic_agents/mcp.py:432` with the new `resolve_env=False` parameter (per Decision 7's implementation note); the backend resolves `$VAR` references itself at `load_mcp_server(name)` time.
- `name` validated against path-traversal at API boundary: refuses `/`, `\\`, `..`, leading `.`, control chars, newlines.

#### Install / uninstall semantics (PR 3)

The PR 3 write paths implement MUST 9 (atomicity + idempotency) through a strict read-modify-write critical section guarded by the `LockBackend` lease. Both `install(spec)` and `uninstall(name)` follow the same shape with one branch difference.

**Common preamble (both methods).** Validate `name` (or `spec.name`) charset at the API boundary per MUST 1 via `_validate_server_name(...)` BEFORE acquiring the lock or touching the filesystem. Refuses path-traversal tokens, control chars, newlines, leading dot, empty string. Raises `ValueError` cheaply for invalid input without contending for the lock.

**Critical section (steps 2-7, wrapped in the lock).** Use the spec/21 context-manager idiom so every exit path (success, collision, parse error, atomic_write failure, lease loss) releases the lock cleanly:

```python
try:
    handle = self._lock_backend.acquire("mcp_registry", timeout=self._install_lock_timeout)
except LockBusy as exc:
    raise MCPRegistryUnavailable(
        f"mcp_registry lock contention: {exc}"
    ) from exc

with handle:
    # Step 2: read current mcp.md (FileNotFoundError -> treat as empty)
    # Step 3: parse with parse_mcp_md_text(content, resolve_env=False)
    # Step 4: dual-probe collision check (install) OR absent-name early-return (uninstall)
    # Step 5: check_lock_lost(handle) -- raises LockLost if a lease-backed lock expired mid-critical-section
    # Step 6: render mutated spec list via render_mcp_md_full(updated_specs)
    # Step 7: _io.atomic_write(mcp_md, rendered_content)
```

The outer `try/except LockBusy` translates the lock-timeout to `MCPRegistryUnavailable` so the framework's fail-closed wrapper at `agent.py:__init__` catches it as `MCPRegistryError`. The `with handle:` block guarantees `backend.release(handle)` runs on every path including exceptions.

**Step 4 (install) -- dual-probe collision detection.** A single check against the parsed-spec list misses malformed sections (`## name` header present but `command:` absent, which `_build_spec` silently skips with a warning). The install MUST check BOTH the parsed-name set AND a raw H2 regex scan against the file content:

```python
h2_names = set(re.findall(r"^## (\S+)", content, re.MULTILINE))
parsed_names = {s.name for s in specs}
if spec.name in h2_names or spec.name in parsed_names:
    raise MCPServerAlreadyInstalled(
        f"MCP server {spec.name!r} is already in mcp.md."
    )
```

The exception message MUST contain ONLY `spec.name`, never the full spec repr (env values could leak through `repr(spec)` if the spec was constructed with literal env values; the operator-visible error message is operator input, not framework-resolved values).

**Step 4 (uninstall) -- absent-name idempotency.** If `name` is not present in either the parsed-name set or the H2 regex scan, the uninstall is a no-op: log at DEBUG, skip the `atomic_write` (avoids unnecessary mtime bump), exit the `with` block (releases lock), return `None`. No exception is raised. There is no pre-lock fast-path; the lock MUST be acquired before reading mcp.md, because a concurrent `install` could add the name between an unlocked check and the subsequent read.

**Step 5 -- mid-critical-section lease check.** Before the `atomic_write`, call `check_lock_lost(handle)` from `atomic_agents.locks`. This is a no-op on filesystem (no heartbeat / TTL: POSIX `fcntl.flock` releases automatically on process death), but for lease-backed backends (`RedisLockBackend`) it raises `LockLost` if the lease expired mid-critical-section (Redis network blip). The implementation MUST catch `LockLost` and re-raise as `MCPRegistryUnavailable` so the framework treats it as a transient failure.

**Step 6 -- full-file render.** The "append new section" phrasing in MUST 9 is conceptual; the implementation does a full read-modify-write. The renderer `render_mcp_md_full(specs)` from `atomic_agents/mcp.py` produces a complete mcp.md content string from the mutated spec list (install: existing specs + new spec; uninstall: existing specs minus removed). The renderer's round-trip property: `parse_mcp_md_text(render_mcp_md_full(specs), resolve_env=False) == specs`. The renderer writes `$VAR` references verbatim (never resolved values; resolved env never persists to disk per Decision 7).

**MCPServerRef projection on install return.** `install(spec) -> MCPServerRef` projects `name`, `description` (single-line, newlines stripped), `transport` from the input spec; `version=None`; `source=f"mcp.md#section:{name}"`. The Ref carries no `command`, no `args`, no `env`, so the CLI handler can safely echo the Ref without secret-leak risk.

**uninstall return.** `uninstall(name) -> None`. Both the present-and-removed path and the absent-no-op path return `None`. Matches `SQLiteToolRegistryBackend.uninstall` precedent.

**Crash safety analysis.** All crash points produce recoverable state:
- Crash before step 7 atomic_write (lock held, file unchanged): lock released by OS on process death (filesystem) or by lease expiry (Redis); next install retries cleanly.
- Crash during atomic_write (rename incomplete): temp file `.mcp.md.<random>.tmp` left in `agent_root`; mcp.md still has original content (atomic rename guarantee). `cleanup_stale_tempfiles(agent_root)` from `_io.py` handles the orphan; PR 3 calls this from `FilesystemMCPServerRegistryBackend.__init__` as a side-effect-free recovery sweep.
- Crash after atomic_write (rename committed, lock held): mcp.md has new content; stale lock released as above. Correct on-disk state.

#### LockBackend integration (PR 3)

**Default factory routes through `get_default_lock_backend`.** When the constructor receives `lock_backend=None`, the backend lazily resolves the default via `atomic_agents.locks.get_default_lock_backend(agent_root)`. This respects `ATOMIC_AGENTS_LOCK_BACKEND` (env var that the agent's main lock already respects), so operators on Cloud Run / Kubernetes pinning `=redis` automatically get `RedisLockBackend` for registry writes too. Single-host operators get `FilesystemLockBackend(agent_root)`. The framework convention is consistent across the agent's main lock and the registry lock.

**Context-manager idiom is the canonical release pattern.** `LockHandle` (per spec/21) implements `__enter__` and `__exit__`; `__exit__` invokes `backend.release(handle)` on every path including exceptions. The 7-step critical section MUST be wrapped in `with handle:` (NOT bare `try/finally` with `handle.release()` because `LockHandle` is a frozen dataclass without a `release()` method; release is a backend method, not a handle method).

**`install_lock_timeout` is the operator knob.** Constructor kwarg with 30s default. Tests use `install_lock_timeout=0.0` for fail-fast assertions. CI pipelines that want immediate failure on contention set it to a low value. NFS-mounted deployments with slow filesystems raise it.

**`LockBusy` translates to `MCPRegistryUnavailable` inside install/uninstall.** The `acquire()` call may raise `LockBusy` from spec/21 after the timeout elapses. The implementation catches it at the boundary and re-raises as `MCPRegistryUnavailable` so the CLI's exception handler + the framework's fail-closed wrapper at `agent.py:__init__` (both catch `MCPRegistryError`) handle it cleanly. Raw `LockBusy` escaping to either layer would bypass operator-readable error messages.

**`check_lock_lost(handle)` discipline.** Called before the `atomic_write` step. No-op for filesystem (`supports_lease=False`); raises `LockLost` for lease-backed backends if the lease expired mid-critical-section. Caught and re-raised as `MCPRegistryUnavailable`. This closes the Redis-network-blip corruption window where two installs could both believe they hold the lock.

**Custom `lock_backend` operator surface.** Operators passing a custom `lock_backend` MUST scope it to a registry-specific resource (`.mcp_registry.lock` namespace) NOT the agent's main lock. Passing the agent's main lock backend causes install/uninstall to raise `LockBusy` (translated to `MCPRegistryUnavailable`) whenever `agent.call()` is in flight, because both operations would compete for `<agent_root>/.lock`. The constructor docstring names this failure mode explicitly.

**Multi-host pinning via env var.** Operators on Cloud Run / Kubernetes set `ATOMIC_AGENTS_LOCK_BACKEND=redis` + `ATOMIC_AGENTS_LOCK_BACKEND_URL=redis://...`. The default factory routes correctly without per-construction operator config. Redis keys are scoped per `agent_root` for cross-agent isolation (spec/21 §"Operator surface").

**Non-reentrant by default.** `FilesystemLockBackend` and `RedisLockBackend` both report `supports_reentrancy=False`. A caller that pre-acquires `acquire("mcp_registry", ...)` externally and then calls `backend.install(spec)` gets `LockBusy` on the internal acquire attempt. Operators wanting batch install should call `install(spec)` sequentially (each call is individually atomic) rather than wrapping in an external acquire.

**`validate(name)`:** runs descriptor parses; `command` exists on PATH (warn if absent; do not fail validation since the agent's PATH at run time may differ); `transport` value recognized; `$VAR` refs resolve against current `os.environ` (warn if not).

**Capabilities (PR 3+):** `supports_install=True, supports_uninstall=True, supports_capability_handshake=False, supports_audit=False, durable=True`. At PR 3 the install / uninstall methods land and the capability flags flip True (was False at PR 1/2 per Decision 5 evolution table). Static; no runtime probe required.

**`list_mcp_servers()`** MUST call `parse_mcp_md_text(content, resolve_env=False, read_paths=None)` with `read_paths=None` explicitly, NOT `self._read_paths`. Passing `self._read_paths` at list time triggers `validate_mcp_server_args` at parse time and violates Decision 8's "validation at `load_mcp_server` boundary" invariant (prep notes Theme 3).

**`load_mcp_server(name)`** MUST call `validate_mcp_server_args(spec, self._read_paths)` from `mcp.py:589` after materializing the MCPServerSpec but before returning. Do NOT reimplement the check inline.

**`load_all_mcp_servers()`** at PR 1 delegates to `_default_load_all(self)` (NOT a custom loop). MUST 10 conformance asserts set equality AND ordering parity; a custom loop could silently drift on sort order (prep notes Theme 4).

### `HTTPMCPServerRegistryBackend`

Ships at PR 4 (read-only) and PR 5 (write paths).

```python
HTTPMCPServerRegistryBackend(
    catalog_url: str,
    agent_scope: str,
    *,
    auth_token: str | None = None,
    request_timeout_s: float = 10.0,
    probe_failure_cache_s: float = 60.0,
)
```

`request_timeout_s` (default 10s) is the per-request HTTP timeout (how long to wait for one response). `probe_failure_cache_s` (default 60s) is the failure-cache timeout (how long after a probe failure before re-probing). The two are deliberately separate: a 10s request timeout makes sense for one HTTP call; a 10s failure cache would cause thrashing on sustained catalog outages (30+ probe attempts in 5 minutes). With 60s failure cache, sustained outage produces approximately 5 probes per 5 minutes, not approximately 30.

Constructed from the URL family via `make_http_mcp_server_registry_backend_from_url(url)` honoring the `https://<host>[:port]/?agent_scope=<name>` shape (default `agent_scope="default"` when the query param is absent).

- **Side-effect-free construction.** `__init__` does NOT call the network. Capability probe is lazy (first non-construction call) per MUST 2.
- **Capability negotiation** (Decision 4): first non-construction call triggers `_probe_capabilities()`. The full probe sequence is specified under Decision 4; this section does not re-specify it.
- **Wire format (documented in spec/36 §"HTTP wire format" added at PR 4):**
  - `GET /mcp-servers?agent_scope=<scope>` returns the mounted server set for this agent_scope as `{"servers": [{"name": ..., "description": ..., "transport": ..., "version": null, "source": ""}, ...]}`. Catalog servers MUST filter server-side by `agent_scope`; returning the org-wide catalog from this endpoint is non-conformant.
  - `GET /mcp-servers?agent_scope=<scope>&expand=spec` returns the bulk-expansion form for `load_all_mcp_servers()`: `{"servers": [<full MCPServerSpec JSON>, ...]}`. Single round trip for N servers; eliminates N+1 cost at agent construction.
  - `GET /mcp-servers/<name>?agent_scope=<scope>` returns the full MCPServerSpec as JSON for the single mounted server.
  - `GET /mcp-servers/<name>/validate?agent_scope=<scope>` returns `{"ok": bool, "errors": [...], "warnings": [...]}`.
  - `POST /mcp-servers?agent_scope=<scope>` accepts the MCPServerSpec as JSON; returns 201 with the created Ref on success, 409 on collision.
  - `DELETE /mcp-servers/<name>?agent_scope=<scope>` returns 204 on success or absence (idempotent). Unmounts from the specified scope only.
- **Auth:** if `auth_token` provided, every request includes `Authorization: Bearer <token>`. If absent, requests go unauthenticated. Catalog servers requiring auth respond 401; backend translates to `MCPRegistryAuthRequired`.
- **Transient failure:** network errors (DNS, connection refused, timeout) raise `MCPRegistryUnavailable`. HTTP 5xx responses also raise `MCPRegistryUnavailable`. HTTP 404 on `/mcp-servers/<name>` raises `MCPServerNotInRegistry` (per MUST 7).
- **URL credential redaction:** if `catalog_url` contains embedded credentials (`https://user:pass@host/`), they are stripped from error messages (per MUST 4; matches the spec/22 / spec/24 / spec/25 / spec/32 / spec/34 precedent).
- **Cross-tenant isolation:** every request includes `?agent_scope=<scope>`; the catalog server filters by scope at the SQL / storage layer. Scope is hardcoded from the constructor; `install()` does NOT accept a scope parameter (per MUST 5; same shape as `SQLiteToolRegistryBackend`).
- **HTTP client library:** `httpx` via a new optional extra `[http]` in `pyproject.toml`. The HTTP backend lazy-imports `httpx` at first use; if absent, raises `ImportError` with a clear message: `"HTTPMCPServerRegistryBackend requires 'httpx'. Install with: pip install atomic-agents-stack[http]"`. Mirrors how `[redis]` is shipped for `RedisLockBackend`.

**Capabilities:** dynamic per tier. Default class-level at PR 4: `supports_install=False, supports_uninstall=False, supports_capability_handshake=True, supports_audit=False, durable=True`. At PR 5: `supports_install=True, supports_uninstall=True` (dynamic per tier at runtime). Runtime values may differ from class defaults based on tier negotiation.

#### Install / uninstall semantics (HTTP) (PR 5)

The PR 5 write paths for the HTTP backend implement MUST 9 (atomicity + idempotency) by delegating transactional responsibility to the catalog server's storage layer. The HTTP backend does NOT acquire a `LockBackend` lease; cross-process atomicity is the catalog server's concern (per the "Out of scope" section: "Cross-process catalog locking. The HTTP catalog server owns transactionality at the storage layer").

**Common preamble (both methods).** Validate `name` (or `spec.name`) charset via `_validate_server_name` BEFORE any network call. Raise `ValueError` cheaply for invalid input. Then call `_ensure_probed()` to populate the runtime capability cache. Then check the relevant capability flag.

**Capability gate.** The gate ordering is: `_ensure_probed()` first, THEN check `capabilities.supports_install` (resp. `supports_uninstall`). This order is mandatory because the pre-probe conservative default is `False` (see `capabilities` property). A naive "check capability then probe" ordering would always raise `NotImplementedError` on the first install call regardless of server tier, because the conservative pre-probe default is always `False`.

**Env-var input contract for install().** `install(spec)` requires `spec.env` to contain ONLY unresolved `$VAR` references (the form an operator types when authoring a spec, not the form returned by `load_mcp_server()`). Literal env values are rejected at the API boundary with `ValueError` before any network call. This protects against the `load_mcp_server -> install` pipeline accidentally sending resolved secrets to the catalog server in the POST body. Callers MUST pass raw `$VAR` references; if they need to copy a spec from another backend, they must first restore the unresolved env shape (typically by re-reading the source mcp.md or by re-constructing the spec with `$VAR` placeholders).

**`install(spec)` -- POST semantics.**

1. Validate `spec.name` charset (MUST 1). Raise `ValueError` on invalid.
2. **Env-var input contract (Decision A, v1.0).** Iterate `spec.env`. For any value that is non-empty and does not start with `$`, raise `ValueError` with a message naming the server name and the offending key. This check runs BEFORE `_ensure_probed()`, BEFORE the capability gate, BEFORE any network call. It is pure input validation at the API boundary.
3. Call `_ensure_probed()` to populate capability cache.
4. Check `capabilities.supports_install`. If `False`, raise `NotImplementedError` with a tier-1 message naming the catalog URL.
5. POST `spec.to_dict()` to `/mcp-servers?agent_scope=<scope>` with auth headers.
6. On HTTP 405: call `_handle_tier_regression("install")` (see below). This never returns normally.
7. On HTTP 409: raise `MCPServerAlreadyInstalled` naming the server.
8. On HTTP 201: project and return `MCPServerRef` from the input `spec` (NOT from parsing the 201 response body; see D-PR5-6). The 201 body is informational only.

**`uninstall(name)` -- DELETE semantics.**

1. Validate `name` charset (MUST 1). Raise `ValueError` on invalid.
2. Call `_ensure_probed()` to populate capability cache.
3. Check `capabilities.supports_uninstall`. If `False`, raise `NotImplementedError`.
4. DELETE `/mcp-servers/<name>?agent_scope=<scope>` with auth headers.
5. On HTTP 405: call `_handle_tier_regression("uninstall")`. This never returns normally.
6. On HTTP 204: return `None`. Do NOT call `resp.json()` on a 204 response (empty body).

**Idempotency.** The catalog server returns 204 whether the name exists or not. No special handling for the absent-name case; 204 on absence is the contract (per MUST 9: "uninstall MUST be idempotent").

**MCPServerRef projection on install return (D-PR5-6).** `install(spec) -> MCPServerRef` constructs the Ref by projecting `name`, `description` (first line only, newlines stripped), `transport` from the input spec; `version=None`; `source=f"{self._catalog_url}/mcp-servers/{spec.name}"`. The 201 response body is NOT parsed for Ref construction. This avoids defense-in-depth gaps where a malformed 201 body would cause `KeyError` or `TypeError`.

**No LockBackend lease.** The HTTP backend does NOT call `check_lock_lost`. There is no `LockHandle` for HTTP write operations. The catalog server's own storage layer (SQL transaction, MVCC, or equivalent) provides atomicity. This is structurally equivalent to the SQLiteToolRegistryBackend pattern: the database engine serializes concurrent writers; the framework does not add a second lock layer.

**Concurrent thundering-herd 405 behavior.** Multiple concurrent callers may each observe a 405 on the same POST or DELETE after a mid-session tier regression. Each caller independently triggers `_handle_tier_regression`. Each re-probe is a separate network round trip (probes run outside the lock per D-PR4-3). Each caller raises `NotImplementedError` independently. Last-writer-wins on the capability cache update inside `_capabilities_lock`; all callers converge to the correct tier after the first re-probe lands. No retry, no coordination between concurrent callers. This is the intended behavior.

**Fail-late carve-out (MUST 3 compatibility).** The tier-regression handler raises `NotImplementedError` DYNAMICALLY after a 405, even when the capability cache at method-call-entry reported `supports_install=True`. This is compatible with MUST 3 because the capability was `True` at call entry (honesty at introspection time). The dynamic downgrade is a mid-session server state change, not a capability lie at introspection time. Conformance tests SHOULD add a docstring note for MUST 3: "Tier-regression handler raises NotImplementedError dynamically AFTER 405; this fail-late state is COMPATIBLE with MUST 3 (cap was True at call entry)."

**`_handle_tier_regression(operation)` helper.** A dedicated `-> NoReturn` method on the backend class (not routed through `_handle_http_error` because it needs access to `self` for re-probing). Steps:

1. Call `self.refresh_capabilities()` OUTSIDE any lock (D-PR4-3 discipline).
2. On `(MCPRegistryUnavailable, MCPRegistryAuthRequired)` from `refresh_capabilities()`: re-raise as `MCPRegistryUnavailable` with message: `f"catalog server at {self._safe_catalog_url} returned 405 on {operation} and re-probe failed: {original_exc}. Capability cache may be stale."`.
3. After re-probe succeeds: read `capabilities.supports_install` (for `operation="install"`) or `capabilities.supports_uninstall` (for `operation="uninstall"`) from the updated cache.
4. If re-probe STILL returns tier 2 (the contradictory case): raise `NotImplementedError` with an "inconsistent server" message: `f"catalog server at {self._safe_catalog_url} returned 405 on {operation} but re-probe still reports tier 2. Inconsistent catalog server state; operator investigation required."`.
5. Otherwise: raise `NotImplementedError` with the standard tier-regression message naming the previous-tier to new-tier transition and the operation name.

ALL operator-facing messages in this helper MUST use `self._safe_catalog_url`, never `self._catalog_url` (MUST 4 URL credential redaction).

---

### HTTP wire format (PR 4)

All HTTP endpoints accept and return JSON with snake_case keys. Every request includes `?agent_scope=<scope>` to identify the per-agent catalog partition.

**MCPServerSpec field contract (catalog server authors).** Each server entry in the wire format has the following fields:

| Field | Required | Type | Default | Notes |
|---|---|---|---|---|
| `name` | YES | string | (none) | MUST match charset `[a-zA-Z0-9_.+@-]+`; framework refuses path-traversal / newlines / control chars at parse boundary (MUST 1 + defense-in-depth against catalog injection) |
| `command` | YES | string | (none) | The executable for the MCP server subprocess |
| `args` | optional | list of strings | `[]` | Command-line arguments |
| `env` | optional | object mapping string to string | `{}` | Environment variables; `$VAR` references resolved client-side at materialization (MUST 8) |
| `transport` | optional | string | `"stdio"` | Only `"stdio"` supported at v1.0 |
| `description` | optional | string | `""` | Operator-readable note |

**MCPServerRef field contract** (lightweight listing form returned by `GET /mcp-servers`):

| Field | Required | Type | Default | Notes |
|---|---|---|---|---|
| `name` | YES | string | (none) | Same charset rule as MCPServerSpec |
| `description` | optional | string | `""` | Same as MCPServerSpec |
| `transport` | optional | string | `"stdio"` | Same as MCPServerSpec |
| `version` | optional | string OR null | `null` | Reserved; empty string is normalized to `null` for round-trip stability |
| `source` | optional | string | `""` | Backend-specific origin marker; for HTTP responses the framework sets this from the raw catalog URL on receipt (operators should not populate it server-side) |

Extra keys on either shape are silently ignored for forward-compatibility with future wire format extensions.

**GET /mcp-servers?agent_scope=scope**

Returns the set of servers mounted for the given scope. Response shape:

```json
{
  "servers": [
    {"name": "github", "description": "GitHub repo access", "transport": "stdio", "version": null, "source": ""},
    {"name": "filesystem-tools", "description": "", "transport": "stdio", "version": null, "source": ""}
  ]
}
```

**GET /mcp-servers?agent_scope=scope&expand=spec**

Returns the full `MCPServerSpec` shape for every mounted server. Used by `load_all_mcp_servers()` to eliminate N+1 round trips. Response shape:

```json
{
  "servers": [
    {"name": "github", "command": "npx", "args": ["-y", "@mcp/github"], "env": {"GITHUB_TOKEN": "$GITHUB_PAT"}, "transport": "stdio", "description": ""},
    {"name": "filesystem-tools", "command": "npx", "args": ["-y", "@mcp/fs", "/data"], "env": {}, "transport": "stdio", "description": ""}
  ]
}
```

**GET /mcp-servers/name?agent_scope=scope**

Returns the full `MCPServerSpec` for a single mounted server. Response shape is the same as one entry from the `expand=spec` list above. Returns 404 when the name is not mounted for the scope.

**GET /mcp-servers/name/validate?agent_scope=scope**

Returns a static validation result. Response shape:

```json
{"ok": true, "errors": [], "warnings": ["command 'npx' not found on catalog server PATH"]}
```

Returns 404 when the catalog server does not implement this endpoint (tier-1 servers are not required to). The HTTP backend returns `ValidationResult(ok=False, errors=["...does not implement /validate..."])` on 404 rather than raising.

**GET /capabilities**

Optional. Returns the catalog server's tier advertisement. Response shape:

```json
{"tier": 2, "supports_install": true, "supports_uninstall": true, "supports_audit": false, "wire_version": "1.0"}
```

Returns 404 when the catalog server does not implement capability advertisement. Absence implies at most tier 2 (tier 3 is only detectable via this endpoint).

---

### Tier negotiation (PR 4)

The HTTP backend runs the following deterministic probe sequence on the first non-construction call. The probe fires outside any lock; the cache write happens inside `_capabilities_lock`.

**Step 1:** `GET /capabilities`. If 200, parse the response body; the server's reported tier is authoritative. Values from this endpoint win over any inference.

**Step 2:** If `GET /capabilities` returns 404 (endpoint absent), fall through to step 3. If it returns any other non-200 status, handle per the exception table below (401 raises `MCPRegistryAuthRequired`; 5xx raises `MCPRegistryUnavailable`; other 4xx raises `MCPRegistryUnavailable`). Non-404 4xx responses MUST NOT silently fall back to OPTIONS.

**Step 3:** `OPTIONS /mcp-servers`. Parse the `Allow` response header using set-membership: `allowed = {m.strip().upper() for m in allow_header.split(",")}`. Tier inference: `{"GET", "POST", "DELETE"}.issubset(allowed)` implies tier 2 (read-write). GET-only implies tier 1 (read-only). Extra methods (HEAD, OPTIONS) do not affect inference.

**Step 4:** If `OPTIONS /mcp-servers` returns 404 or 405, default to tier 1 (read-only). This is the conservative fallback; operators can call `refresh_capabilities()` after confirming their catalog server supports more.

**Step 5:** Any network error, timeout, or 5xx at any probe step raises `MCPRegistryUnavailable`. The failure is cached for `probe_failure_cache_s` seconds; subsequent calls within the cache window raise immediately without re-probing. Explicit `refresh_capabilities()` always bypasses the failure cache.

**Probe failure cache:** prevents thrashing when the catalog server is unreachable. With the 60-second default, sustained outage produces roughly 5 probes per 5 minutes rather than 30+. `request_timeout_s` (default 10s) is the per-request timeout; `probe_failure_cache_s` (default 60s) is the failure-window timeout. The two are deliberately separate.

---

### Capability handshake (PR 4)

The `supports_capability_handshake` flag distinguishes the HTTP backend from the filesystem backend. It is `True` on `HTTPMCPServerRegistryBackend` and `False` on `FilesystemMCPServerRegistryBackend`.

Two levels of capability:

**Static (class-level):** what the backend's class is capable of. `HTTPMCPServerRegistryBackend` supports capability negotiation by definition (`supports_capability_handshake=True`); this is constant regardless of probe state.

**Runtime (probe-dependent):** what the connected catalog server actually allows. `supports_install` and `supports_uninstall` reflect the tier negotiation result after the first successful probe. Before the first probe, the `capabilities` property returns a conservative pre-probe default (all write capabilities `False`).

The `capabilities` property returns the runtime view. Callers should use `backend.capabilities.supports_install` (property, not method call) to check before invoking write operations. Conformance tests assert claim-vs-behavior parity (MUST 3) so the runtime value is trustworthy.

---

### Per-scope filtering (PR 4)

Catalog servers MUST filter by `agent_scope` server-side. The `agent_scope` query parameter is included on every HTTP request: `?agent_scope=<scope>`. A catalog server that returns the org-wide catalog from `GET /mcp-servers?agent_scope=<scope>` (ignoring the scope parameter) is non-conformant with MUST 5.

The scope is hardcoded from the `HTTPMCPServerRegistryBackend` constructor; no API method accepts a scope override. This mirrors the `SQLiteToolRegistryBackend` pattern where the scope is a constructor argument, not a per-call parameter.

Scope isolation is the catalog server's responsibility at the storage layer. The framework's HTTP backend does not perform any client-side filtering; it passes the scope and trusts the catalog server to filter correctly.

---

## Exception surface

All exceptions live in `atomic_agents/exceptions.py` and are re-exported from `atomic_agents.mcp_registry` for ergonomic access (per the PersonaBackend D-PI-1 precedent).

- `MCPServerNotInRegistry`: `load_mcp_server(name)` called with an unknown name; HTTP 404. Distinct from `MCPServerConnectFailed` (spec/19's runtime subprocess failure).
- `MCPServerAlreadyInstalled`: `install(spec)` collided on server name; HTTP 409.
- `MCPRegistryUnavailable`: transient failure (network, file lock contention, server 5xx). Operators retry; framework does NOT auto-retry.
- `MCPRegistryAuthRequired`: HTTP 401 without `auth_token`. Operators set the env var or constructor kwarg.
- `MCPRegistryDescriptorInvalid`: mcp.md parse failure (filesystem); HTTP response body invalid JSON (HTTP).
- `BackendNotRegistered`: operator-pinned `backend_id` isn't in the registry. Matches every prior arc's `BackendNotRegistered` shape.
- `ValueError`: invalid server name (path separator, empty, parent-dir token, leading `.`, control chars). Also raised by `install()` when `spec.env` contains literal values (likely resolved secrets); callers MUST pass unresolved `$VAR` refs.
- `NotImplementedError`: capability-gated method on a backend that doesn't support it at runtime.
- `MCPServerConnectFailed`: re-raised from `load_mcp_server` when env-var resolution fails (matches existing spec/19 exception; not a new exception class).

**HTTP backend exception mapping (PR 4).** Every `httpx` exception is caught and mapped before escaping `http.py`. `httpx.InvalidURL` requires a separate `except` clause because it does NOT inherit from `httpx.HTTPError`.

| httpx exception | Maps to | Condition |
|---|---|---|
| `httpx.HTTPStatusError` (401) | `MCPRegistryAuthRequired` | Auth required; no token provided. |
| `httpx.HTTPStatusError` (404 on /mcp-servers/name) | `MCPServerNotInRegistry` | Named server absent from catalog. |
| `httpx.HTTPStatusError` (404 on /mcp-servers collection) | `MCPRegistryUnavailable` | Tier-1 server must implement GET /mcp-servers. |
| `httpx.HTTPStatusError` (5xx) | `MCPRegistryUnavailable` | Server-side transient failure. |
| `httpx.HTTPStatusError` (409 on POST /mcp-servers) | `MCPServerAlreadyInstalled` | Server name collision; catalog server already has this name for this scope. |
| `httpx.HTTPStatusError` (405 on POST or DELETE /mcp-servers) | triggers `_handle_tier_regression` -> `NotImplementedError` | Mid-session tier regression: catalog server was tier 2 but is now tier 1. Re-probe fires before raising. |
| `httpx.HTTPStatusError` (other 4xx) | `MCPRegistryUnavailable` | Conservative; non-404 4xx MUST NOT silently fall back. |
| HTTP 204 on DELETE /mcp-servers | success; return `None` | Idempotent uninstall; 204 returned whether name was present or absent. |
| `httpx.LocalProtocolError` | `MCPRegistryDescriptorInvalid` | Client sent invalid HTTP (framework bug). |
| `httpx.DecodingError` | `MCPRegistryDescriptorInvalid` | Response body cannot be decoded. |
| `httpx.TimeoutException` (all variants) | `MCPRegistryUnavailable` | Connection or read timeout. |
| `httpx.NetworkError` (all variants) | `MCPRegistryUnavailable` | DNS failure, connection refused, dropped mid-response. |
| `httpx.ProtocolError` (all variants) | `MCPRegistryUnavailable` | Protocol-level HTTP error. |
| `httpx.HTTPError` (catch-all) | `MCPRegistryUnavailable` | Any other httpx subclass. |
| `httpx.InvalidURL` (separate clause) | `ValueError` | Operator config error; not a transient catalog failure. |
| `json.JSONDecodeError` | `MCPRegistryDescriptorInvalid` | Response body is not valid JSON. |

---

## Registry

```python
from atomic_agents.mcp_registry import (
    register_mcp_server_registry_backend,
    get_mcp_server_registry_backend,
    list_mcp_server_registry_backends,
)

register_mcp_server_registry_backend("filesystem", FilesystemMCPServerRegistryBackend)
register_mcp_server_registry_backend("http", HTTPMCPServerRegistryBackend)
cls = get_mcp_server_registry_backend("filesystem")     # returns FilesystemMCPServerRegistryBackend
backend = cls(agent_root, read_paths)                    # caller instantiates with scope
ids = list_mcp_server_registry_backends()                # ["filesystem", "http"]
```

The registry stores **classes**, not instances (matches `ProfileBackend` + `LogBackend` + `LockBackend` + `ToolRegistryBackend` + `CorpusBackend` registries). Default `"filesystem"` and `"http"` registrations happen at import time inside `atomic_agents/mcp_registry/__init__.py`.

---

## Operator surface

MCP server registry backend choice is a **deployment-level** decision (the whole framework instance picks "filesystem" or "http" or a future SaaS backend), not an agent-author-level decision. Contrast with `mcp.md` (per-agent, declarative).

There is no `mcp_registry.md` markdown config: the catalog backend is the layer that LOADS the catalog; circular.

The operator surface exposes the choice via TWO paths (parallel to `LogBackend` / `LockBackend` / `ProfileBackend` / `ToolRegistryBackend` / `CorpusBackend`):

1. **Constructor kwarg.** Programmatic operators pass `AtomicAgent(..., mcp_server_registry_backend=HTTPMCPServerRegistryBackend(...))` to bypass env-var resolution entirely.

2. **Environment variables:**
   - `ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND`: backend id (default `filesystem`). Recognized: `filesystem`, `http`.
   - `ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL`: connection string for non-filesystem backends. HTTP shape: `https://catalog.example.com/?agent_scope=<scope>`.
   - `ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN`: bearer token for HTTP (optional; absence means unauthenticated requests).

**Default factory `http` branch (PR 4).** When `ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND=http`, `get_default_mcp_server_registry_backend` lazily imports `make_http_mcp_server_registry_backend_from_url` from `atomic_agents.mcp_registry.http` and reads `ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL` (required). If that env var is absent or empty, raises `BackendNotRegistered` with an operator-readable message naming the required variable and expected URL format. The lazy import means filesystem operators never pay the `httpx` import cost.

```sh
export ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND=http
export ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL="https://catalog.example.com/?agent_scope=my-agent"
export ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN="token123"   # optional
```

**Credential safety:** `get_default_mcp_server_registry_backend` sanitizes the BACKEND env var before echoing in error messages (strips anything following `://` and truncates at 32 chars). Mirrors the redaction helper from `logs/__init__.py:316` and `profile/__init__.py:_redact_for_error_message`.

The constructor kwarg ALWAYS wins. Operator-config layering: env vars are deployment-level; the kwarg is per-agent-construction.

**Per-runner threading.** Mirrors the established pattern from spec/22 / 24 / 25 / 32 / 33 / 34:

- `OutcomeRunner(..., mcp_server_registry_backend=...)` threads to `_outcome_iteration_loop`.
- `EvalRunner(..., mcp_server_registry_backend=...)` threads to `_eval_iteration_loop`.
- `DreamRunner(..., mcp_server_registry_backend=...)` stores as `self._mcp_server_registry_backend` for API parity.
- `delegate.py` threads the backend via the `_mcp_server_registry_backend_was_explicit` flag pattern (mirrors `PersonaBackend` D-ER-2 at `agent.py:401` and `CorpusBackend` at `agent.py:431`). The catalog is per-agent semantic context, not fleet-scoped, so delegates only inherit when explicitly threaded; default-resolved backends do not leak the coordinator's `agent_root` or `catalog_url` to delegates.

---

## CLI surface

`atomic-agents mcp-registry` subcommands (zero LLM calls; env-var-aware):

- `atomic-agents mcp-registry list`: calls `list_mcp_servers()`, prints names, descriptions, and transport in a table.
- `atomic-agents mcp-registry show <name>`: calls `load_mcp_server(name)`, prints the full MCPServerSpec.
- `atomic-agents mcp-registry validate <name>`: calls `validate(name)`, prints the ValidationResult.
- `atomic-agents mcp-registry install <name> --command <cmd> --args <a,b,c> --env <K=V,L=W> --description <text>`: builds an MCPServerSpec from CLI flags, calls `install(spec)`. Ships in PR 3 for filesystem; PR 5 for HTTP.
- `atomic-agents mcp-registry uninstall <name>`: calls `uninstall(name)`. Ships in PR 3 for filesystem; PR 5 for HTTP.
- `atomic-agents mcp-registry refresh-capabilities`: for HTTP backends; re-runs the capability probe and reports the current tier.

CLI catches `MCPRegistryUnavailable`, `MCPServerNotInRegistry`, `MCPServerAlreadyInstalled`, and `MCPRegistryAuthRequired` cleanly with operator-readable error messages.

---

## Doctor coherence check

`doctor.check_mcp_server_registry_backend` (PR 2 scope) validates operator-config coherence with a PASS / WARN / FAIL ladder. Doctor uses `backend.validate(name)` (not `parse_mcp_md` directly) when checking server-level validity.

- **PASS:** backend resolves cleanly; `list_mcp_servers()` returns without raising; capability snapshot matches the env var (e.g., `=http` resolves to `HTTPMCPServerRegistryBackend`).
- **WARN:** transient probe failure (e.g., HTTP backend can't reach catalog server right now but the env var is correctly set); operator-facing message names the catalog_url with credentials redacted.
- **FAIL:** unknown backend_id in env var; missing required env var (URL absent when backend is HTTP); auth required but no token provided.

Doctor output includes a capability snapshot (current `capabilities` view) so operators see at a glance which tier their HTTP catalog server is at.

---

## Reserved at lock

Items with reserved capability flags that ship unsupported at v1.0:

- **Tier 4+ capability flags.** Future upstream MCP ecosystem registry protocols slot in additively. spec/36 v1.0 documents tiers 1-3; future arcs add tier-4 negotiation.
- **SQLite-backed local catalog.** Reserved for a future arc if the single-host multi-agent use case emerges with real operator demand. The Protocol seam supports it; no v1.0 work.
- **OAuth flows for HTTP catalog auth.** Operators pre-resolve tokens into the `AUTH_TOKEN` env var. Matches spec/19's v1 OAuth deferral.
- **Audit log surfacing.** Tier 3 catalog servers may advertise `supports_audit=True`; the framework's HTTP backend exposes the capability flag but does not consume the audit endpoint in v1.0. Reserved for a future arc.

---

## Out of scope

- **Postgres / vector-backed catalog.** Deferred to a future arc if operator demand justifies it. Mirrors the `PgvectorCorpusBackend` deferral pattern from spec/34 where `PgvectorCorpusBackend` is deferred to the coordinated #258 Postgres-adapter family release.
- **Subprocess sandboxing.** spec/28's judge layer covers runtime safety (pre-action validation, REVISE / ESCALATE). The registry covers catalog only.
- **HTTP transport for MCP servers themselves.** Still stdio-only per spec/19's v1. The HTTP added here is for the *catalog server*, not the *MCP server's transport*.
- **MCP resource subscriptions / prompt templates.** Deferred per spec/19's existing reservations.
- **Cross-process catalog locking.** The HTTP catalog server owns transactionality at the storage layer; the framework does NOT add a separate LockBackend dependency for HTTP install/uninstall (relies on the server's own consistency).
- **`supports_skills_catalog`.** Not relevant for MCP server registry. The skill catalog reservation lives on `ToolRegistryCapabilities` per spec/25 Decision 2.

---

## Implementer contract (10 normative MUSTs)

Backends implementing `MCPServerRegistryBackend` MUST satisfy all 10 MUSTs. The conformance suite at `tests/test_mcp_server_registry_conformance.py` parametrizes across registered backends and asserts each MUST.

The MUST count is 10 because the static-vs-dynamic capability distinction (Decision 6) is novel relative to the 11 prior arcs (which all had static-equals-dynamic capabilities) and requires two additional MUSTs (MUST 3 extended to cover the static/dynamic distinction; MUST 10 added for `load_all_mcp_servers()` consistency) beyond the 8-MUST range of prior arcs.

**MUST 1: Name charset validation at API boundary.** Server names MUST match `[a-zA-Z0-9_.+@-]+`. Validation MUST happen BEFORE any backend access (disk read, HTTP call, parse). Path-traversal tokens (`..`, `/`, `\\`, control chars, newlines, leading dot) MUST raise `ValueError` at the API boundary. Matches the locked charset across PolicyBackend, PersonaBackend, CorpusBackend.

**MUST 2: Side-effect-free construction.** `__init__` MUST NOT call the network, spawn subprocesses, open file handles for the catalog, or do any other side-effecting work. Construction is cheap and synchronous. First non-construction call may trigger lazy probing. This preserves byte-identical pre-#201 behavior for all existing `AtomicAgent(...)` construction sites when no backend is configured.

**MUST 3: Capability honesty.** `capabilities` (the `@property`) MUST reflect what the backend actually supports. Conformance suite enforces: if `supports_install=True` is reported, `install(spec)` MUST NOT raise `NotImplementedError`. Static-vs-dynamic capability distinction (Decision 6) is documented in each backend's `capabilities` property docstring; HTTP-style backends MUST distinguish static class capability (constant) from runtime capability (probe-dependent).

**MUST 4: URL credential redaction.** Operator-facing error messages (including `BackendNotRegistered`, `MCPRegistryUnavailable`, `MCPRegistryAuthRequired`) MUST redact embedded credentials in URLs. The `_redact_for_error_message` helper from `mcp_registry/__init__.py` is the canonical implementation; backends use it for any error path that surfaces a URL.

**MUST 5: Cross-agent isolation at storage layer.** Filesystem backends scope by `agent_root` (one mcp.md per agent). HTTP / SQLite / SaaS backends scope by `agent_scope` (column / query param / URL path segment). The scope is hardcoded from the constructor; `install` / `uninstall` / `list` / `load` / `validate` ALL filter by scope. No cross-scope reads or writes regardless of backend.

**MUST 6: `backend_id` stability and `close()` idempotency.**

- (a) `backend_id` is a stable identifier (constant across the backend's lifetime; matches the registry key used in `register_mcp_server_registry_backend`).
- (b) `close()` is idempotent: calling it twice does NOT raise; calling it before any other method is a no-op.

**MUST 7: Transient-vs-permanent failure honesty.** Backends MUST distinguish "catalog unreachable" (network error, file lock contention, server 5xx) from "server not in catalog" (404, missing mcp.md section). The former raises `MCPRegistryUnavailable`; the latter raises `MCPServerNotInRegistry`. Operators rely on this distinction for retry decisions; conflating them breaks the operator surface.

**MUST 8: Env-var resolution semantics on MCPServerSpec.** `$VAR` references in `MCPServerSpec.env` values MUST be resolved against the client process environment at `load_mcp_server(name)` time. Unresolvable references MUST raise `MCPServerConnectFailed` (the existing spec/19 exception; not a new exception class). This applies to ALL backends regardless of storage substrate: HTTP catalog servers MAY store unresolved `$VAR` strings, and the framework's HTTP client MUST resolve at materialization.

**MUST 9: Install / uninstall atomicity.** Concurrent `install(spec)` calls for the same server name MUST produce exactly one winner; the others MUST raise `MCPServerAlreadyInstalled`. Filesystem implementations MUST acquire a `LockBackend` (spec/21) lease around the read-modify-write critical section in `install` and `uninstall` (read mcp.md, parse, check name collision via dual-probe across raw H2 regex and parsed-name set, write back via the renderer); the `_io.atomic_write` discipline ensures crash-safety on the individual write, while the lock serializes concurrent callers. The lock acquisition uses the spec/21 context-manager idiom: `with lock_backend.acquire("mcp_registry", timeout=self._install_lock_timeout) as handle:` wrapping the critical section. Implementations MUST catch `LockBusy` from `acquire` and re-raise as `MCPRegistryUnavailable` so the framework's fail-closed wrapper at `agent.py:__init__` (catches `MCPRegistryError`) sees a coherent exception type. Implementations MUST call `check_lock_lost(handle)` from `atomic_agents.locks` immediately before the `atomic_write` step; for lease-backed backends (`RedisLockBackend`) this raises `LockLost` if the lease expired mid-critical-section, which MUST be re-raised as `MCPRegistryUnavailable`. HTTP implementations rely on the catalog server's transactional storage and translate HTTP 409 to `MCPServerAlreadyInstalled`. `uninstall(name)` MUST be idempotent (no exception when the name is absent). There is no pre-lock fast-path for absent names on `uninstall`; the lock MUST be acquired before reading mcp.md, because a concurrent `install` could add the name between an unlocked check and the subsequent read. Absent-name uninstall MAY skip the `atomic_write` step (mtime preservation), but MUST still hold the lock for the read-and-check critical section.

**MUST 10: `load_all_mcp_servers()` consistency.** The output of `load_all_mcp_servers()` MUST be semantically equivalent to `[load_mcp_server(ref.name) for ref in list_mcp_servers()]` for any given backend state. Backends MAY optimize the bulk implementation (HTTP backend uses single bulk GET via `?expand=spec`; SQLite backend can use a single SELECT) but MUST preserve the equivalence guarantee. The conformance suite asserts: for every registered backend, `set(load_all_mcp_servers())` equals `set([load_mcp_server(ref.name) for ref in list_mcp_servers()])` across populated and empty catalog states.

**Framework-level invariant (NOT a per-backend MUST):** `agent.py:_load_config()` MUST populate `AgentProfile.mcp_servers_resolved` with the materialized MCPServerSpec list (via `load_all_mcp_servers()`) BEFORE `MCPClientPool` consumes it. This is enforced at the framework integration layer in PR 2, not in each backend. Per Decision 9; preserves the audit-trail-is-structural invariant (CLAUDE.md §5) across all backends.

---

## Shipping plan detail

### PR 1: Protocol scaffolding + filesystem read paths + DRAFT spec

**Code:**
- `atomic_agents/mcp_registry/__init__.py`: register / get / list functions + `get_default_mcp_server_registry_backend` factory + `_redact_for_error_message` helper.
- `atomic_agents/mcp_registry/types.py`: `MCPServerRef`, `MCPServerRegistryCapabilities`, `ValidationResult`.
- `atomic_agents/mcp_registry/backend.py`: `MCPServerRegistryBackend` Protocol + exception classes + `_default_load_all(backend)` convenience helper.
- `atomic_agents/mcp_registry/filesystem.py`: `FilesystemMCPServerRegistryBackend` constructor + read paths (`list_mcp_servers`, `load_mcp_server`, `load_all_mcp_servers`, `validate`, `capabilities`, `refresh_capabilities`, `close`). Install/uninstall ship in PR 3 alongside the LockBackend integration.
- `atomic_agents/mcp.py`: additive `resolve_env: bool = True` parameter on `parse_mcp_md_text` and `_build_spec`. Backward-compatible (existing callers see byte-identical behavior with default True).

**Spec:** `docs/spec/36-mcp-server-registry-backend.md` (this file, DRAFT).

**Tests:**
- `tests/test_mcp_server_registry_conformance.py`: ~40 conformance tests parametrized across registered backends (filesystem only at this PR; HTTP joins in PR 4). Covers MUSTs 1, 2, 3, 5, 6, 7, 8, 10 (install/uninstall MUST 9 covered in PR 3 when those land).
- `tests/test_mcp_server_registry_filesystem_backend.py`: ~23 filesystem-specific tests (path-traversal at API boundary; env-var resolution at load time with `resolve_env=False` parse and lazy resolution; mcp.md parse semantics; empty / missing mcp.md returns `[]`; `load_all_mcp_servers` default impl consistency; malformed mcp.md raises `MCPRegistryDescriptorInvalid`; multi-section mcp.md returns multiple specs; `MCPServerRef.source` field equals `"mcp.md#section:<name>"`).

**CLI:** `atomic-agents mcp-registry list / show / validate / refresh-capabilities` (read-only subcommands).

**No agent.py wiring yet.** Wiring lands in PR 2 alongside the audit/profile sibling field.

**Expected test count delta:** +60. Total test count after PR 1: approximately 2997.

### PR 2: agent.py wiring + audit/profile sibling field + IRON RULE regression + doctor

**Code:**
- `atomic_agents/agent.py:__init__` (after `profile_backend.load_profile()` at line 496 and before `_load_config()` is called at line 770): resolve backend from env vars and constructor kwarg; default to `FilesystemMCPServerRegistryBackend(agent_root, read_paths)` via `get_default_mcp_server_registry_backend(agent_root, self._profile.tool_config['read_paths'])`. Call `backend.load_all_mcp_servers()` to materialize the spec list; populate `AgentProfile.mcp_servers_resolved` via `dataclasses.replace(self._profile, mcp_servers_resolved=materialized)` BEFORE `MCPClientPool` consumes them at the `call()` site. Re-raise `MCPRegistryUnavailable` on probe failure (fail-closed per Decision 1; framework-level invariant). Note: the original spec text said `_load_config()` as the wiring location; the correct location is `__init__` because `read_paths` is available from the loaded profile and `_load_config()` is a pure reader that must not mutate `self._profile`.
- `atomic_agents/profile/types.py`: `AgentProfile.mcp_servers_resolved: list[MCPServerSpec] = field(default_factory=list)` (Decision 9 sibling field).
- `docs/spec/24-agent-profile-backend.md`: D1 addendum documenting the sibling field; `mcp_md_raw` stays as filesystem-backend backward-compat.
- `OutcomeRunner` / `EvalRunner` / `DreamRunner` per-runner kwargs.
- `delegate.py` explicit-only threading via `_mcp_server_registry_backend_was_explicit` flag.
- `doctor.py`: add `check_mcp_server_registry_backend` with PASS/WARN/FAIL ladder + capability snapshot + URL credential redaction.

**Expected test count delta:** +35. Total after PR 2: approximately 3032.

### PR 3: Filesystem CLI install/uninstall + LockBackend integration

**Code:**
- `atomic_agents/mcp_registry/filesystem.py`: add `install(spec)` and `uninstall(name)` with `LockBackend` lease and atomic mcp.md edits. Lock acquisition uses the spec/21 signature `acquire("mcp_registry", timeout=30)`; release via the returned `LockHandle.release()`. Filesystem capability flags flip to `supports_install=True, supports_uninstall=True`.
- `atomic_agents/cli.py`: `atomic-agents mcp-registry install / uninstall` subcommands (filesystem-only at this PR; HTTP install/uninstall land in PR 5).

**Spec:** `docs/spec/36-mcp-server-registry-backend.md`: add §"Install / uninstall semantics" and §"LockBackend integration" subsections.

**Expected test count delta:** +15. Total after PR 3: approximately 3047.

### PR 4: HTTP-client reference impl + tier negotiation + fail-closed + auth + bulk

**Code:**
- `atomic_agents/mcp_registry/http.py`: `HTTPMCPServerRegistryBackend` (read-only mode: `list / load / load_all / validate / refresh_capabilities`) with tier-1/2/3 capability negotiation + lazy probe + `probe_failure_cache_s` (default 60s) + URL credential redaction + bulk endpoint (`GET /mcp-servers?expand=spec`) overriding `_default_load_all`. Install/uninstall stubs raise `NotImplementedError` at this PR (write path lands in PR 5).
- `pyproject.toml`: add `[project.optional-dependencies]` entry: `http = ["httpx>=0.27"]`. Matches the existing `redis` / `openai` / `validation` extras pattern.

**Spec:** `docs/spec/36-mcp-server-registry-backend.md`: add §"HTTP wire format" + §"Tier negotiation" + §"Capability handshake" + §"Per-scope filtering" (catalog server MUST filter by `agent_scope` server-side; non-conformant catalog servers that return org-wide listings are out of spec).

**Expected test count delta:** +31. Total after PR 4: approximately 3,307 (actual post-PR-4 count).

### PR 5: HTTP install/uninstall + tier-3 audit + spec/36 LOCKED + v1.0 RELEASE candidate

**Code:**
- `atomic_agents/mcp_registry/http.py`: add `install / uninstall` write paths (POST and DELETE) with capability gating (dynamic `supports_install` based on tier negotiation); mid-session tier regression handling (inline re-probe + `NotImplementedError` on 405 from a previously-tier-2-now-tier-1 server).
- `atomic_agents/cli.py`: wire HTTP install/uninstall into the existing `atomic-agents mcp-registry install / uninstall` subcommands.

**Spec:** `docs/spec/36-mcp-server-registry-backend.md` LOCKED at 10 MUSTs.

**Doc updates:**
- `CHANGELOG.md` `[Unreleased]`: arc-closer entry covering the full 5-PR series.
- `README.md` backend protocols table: flip `MCPServerRegistryBackend` row from `Planned` to `Shipped`; flip Status block from "Eleven of twelve" to "Twelve of twelve".
- `CLAUDE.md`: add 12th backend lock-paragraph.
- `~/ObsidianVault/Atomic Agents/ROADMAP.md`: flip MCPServerRegistry row; append the Tier 2 backend-protocol scaling roadmap closer.

**Expected test count delta:** +12 to +18. Total after PR 5: approximately 3,319-3,325 tests collected (base: 3,307 post-PR-4 actual).

**After merge:**
- `/land-and-deploy` verification on main-branch CI green on merge commit.
- v1.0 RELEASE tag cut as `v1.0.0`.
- PyPI release fires (`uv build` + `uv publish`).
- GitHub Release notes pulled from CHANGELOG.md `[v1.0.0]` entry.

---

## Deferred decisions

Items 1-3 from the design doc (package name `mcp_registry`, bearer header for HTTP auth, snake_case for JSON keys) are resolved per the decisions above. Remaining open questions:

**4. Tier-4 trigger.** What upstream signal should we watch to start a v1.1 tier-4 arc? File a tracking issue at PR 4 LOCK for "v1.1 spec/36 tier-4 / upstream MCP registry protocol"; watch MCP spec discussions and pin the issue to that thread. Tracked in successor issue at PR 4.

**5. `refresh_capabilities` ergonomics.** Should the HTTP backend auto-refresh capabilities on `MCPRegistryUnavailable` retry, or strictly on explicit operator call? Explicit only at v1.0; auto-refresh is a v1.1 ergonomics arc once operator behavior is observed. Tracked in successor issue at PR 4.

---

## Success criteria

- All 5 PRs ship through formal `/ship` Skill end-to-end (extending the streak from 7 to 12 consecutive clean ships post-#285-revert).
- `/land-and-deploy` verification on each PR's merge commit with main-branch CI green.
- Test count grows approximately 153-169 total across the arc (PR 1: +60, PR 2: +35, PR 3: +15, PR 4: +31, PR 5: +12 to +18). Final count approximately 3,319-3,325 tests collected (base: 3,307 post-PR-4).
- spec/36 LOCK at PR 5 passes adversarial review (Opus subagent, 2-5 rounds per CLAUDE.md §11).
- v1.0 RELEASE cuts after PR 5 lands. CHANGELOG `[Unreleased]` converts to `[v1.0.0]`. PyPI publishes.

---

## References

- `docs/spec/19-mcp-integration.md`: `MCPServerSpec` dataclass shape, `MCPClientPool` subprocess lifecycle, `parse_mcp_md_text` function signature, `validate_mcp_server_args` helper, env-var resolution at parse time.
- `docs/spec/20-memory-backend.md`: MemoryBackend Protocol; the template the protocol-pattern series follows.
- `docs/spec/21-lock-backend.md`: `LockBackend.acquire(name="", timeout=0.0)` signature; `LockHandle.release()` idiom; `LockLost` lease-expiry discipline.
- `docs/spec/24-agent-profile-backend.md`: `mcp_md_raw` snapshot pattern (unchanged); `mcp_servers_resolved` sibling field added (spec/24 D1 addendum). Implementer Contract shape.
- `docs/spec/25-tool-registry-backend.md`: Decision 3 carve-out is the origin of this arc. `ToolRegistryBackend.install` signature comparison. `ValidationResult` shape. Protocol surface parallels.
- `docs/spec/27-doctor.md`: PASS/WARN/FAIL ladder shape.
- `docs/spec/32-policy-backend.md`: `_policy_backend_was_explicit` delegate-threading precedent.
- `docs/spec/33-persona-backend.md`: Most recently locked Protocol spec; Implementer Contract (8 MUSTs) used as template. Exception placement (D-PI-1). `delegate.py` threading rationale (D-ER-2).
- `docs/spec/34-corpus-backend.md`: `@property capabilities` declaration at `corpus/backend.py:70-71`. 9-MUST contract structure used as template. `_redact_for_error_message` helper pattern. `Out of scope` table format. `References` section format.
- `atomic_agents/mcp.py:84`: `MCPServerSpec` dataclass.
- `atomic_agents/mcp.py:126`: `MCPClientPool` (subprocess lifecycle; stays unchanged).
- `atomic_agents/mcp.py:410`: `parse_mcp_md` function (always resolves env vars; no `resolve_env` knob).
- `atomic_agents/mcp.py:432`: `parse_mcp_md_text` function (gains `resolve_env: bool = True` at PR 1).
- `atomic_agents/mcp.py:558-566`: `_build_spec` env-var resolution block (extracted to `_resolve_env_vars` helper at PR 1).
- `atomic_agents/mcp.py:589`: `validate_mcp_server_args(spec, agent_read_paths)` helper (reused by `load_mcp_server`; do NOT reimplement inline).
- `atomic_agents/locks/backend.py:82`: `LockBackend.acquire` signature.
- `atomic_agents/_io.py:42`: `atomic_write` primitive.
- `atomic_agents/corpus/backend.py:70-71`: `@property capabilities` declaration used as the precedent for this Protocol.
- `agent.py:2337`: MCP discovery overlay.
- `~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-design-20260601-165020.md`: Full architectural rationale. APPROVED (3 adversarial rounds, 9/10 quality, zero P0/P1 remaining). 10 MUSTs, 5-PR shipping plan, `mcp_servers_resolved` sibling field on AgentProfile, bulk Protocol method, fail-closed catalog wiring, LockBackend API corrected to spec/21 signature, `probe_failure_cache_s` (60s) distinct from `request_timeout_s` (10s).
- `~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-pr1-prep-notes-20260602-114910.md`: PR 1 implementation prep notes. 35 findings (5 P0, 15 P1, 15 P2) from 5 parallel Sonnet review streams.
- `~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-eng-review-test-plan-20260601-174020.md`: Test plan (~151 tests across 5 PRs).
- [#201](https://github.com/dep0we/atomic-agents-stack/issues/201): The umbrella issue for this arc.

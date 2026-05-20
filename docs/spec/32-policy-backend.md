# spec/32 — PolicyBackend (fleet-wide settings layer)

**Status:** RFC. Locks when the implementation matches and the conformance suite pins the contract. Target lock: PR 4 of #89.

> **Pre-#89 PR 1 amendments (2026-05-19):** Drafted in PR 1 from the /office-hours + /plan-eng-review locked design doc (`~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-design-20260519-084540.md`). 11 architectural decisions locked + 14 plan-subagent prep findings folded.

## Overview

`PolicyBackend` is the ninth open Protocol in the protocol-pattern series (Memory, LLM, Judge, Lock, Log, AgentProfile, ToolRegistry, Mandate, **Policy**). Each Protocol decouples one storage / dispatch axis so the framework's core stays small and alternate implementations drop in without forking.

Policy is a **fleet-wide, configuration-time settings layer**. An operator with a fleet of agents authors a single `<project_root>/policy.md` declaring fleet-default cost caps, tool allowlists, MCP server allowlists, and model selection — with optional per-agent overrides under a nested `agents:` section. The framework reads Policy ONCE per `agent.call()` entry (a snapshot), and the existing layers consult the snapshot for the rest of the call.

Eight backend protocols already ship. PolicyBackend closes the cross-agent configuration cliff: operators currently hand-syncing `model.md` / `tools.md` / `mcp.md` across N agents get a single fleet-wide source of truth, with a Protocol seam ready for SaaS / Postgres / org-admin-console adapters from day 1.

## Why this exists

The framework's elegance promise — *a home user with one agent and an org with a fleet experience the same framework — graceful, coherent, self-explanatory at every scale* — breaks for Tier 2 (operator with a fleet) without PolicyBackend. Today the workaround (editing each agent's `model.md` / `tools.md` / `mcp.md` individually) drifts within weeks and has no audit trail for "what is the operator's current policy across the fleet."

The Mandate primitive (shipped at PR #230) closed the per-agent durable-authorization gap. PolicyBackend closes the cross-agent configuration gap. The two compose — Mandate is per-agent action-time authorization with state; Policy is fleet-wide configuration-time defaults; both are operator-issued. They DON'T overlap on what they do; they overlap on caps (cost) where MIN composition applies, and on allowlists (tool / MCP) where AND composition applies. Both denials emit through the SAME `policy_decision` event family with a `decision_kind` discriminator so operators reading the audit log see ONE event, not two events to correlate.

## Locked decisions (from /office-hours + /plan-eng-review 2026-05-19)

| # | Topic | Lock |
|---|-------|------|
| Premise 1 | Composition math | Most-restrictive wins (MIN for caps, AND for allowlists, deny-takes-precedence within a layer) |
| Premise 2 | Protocol shape | Full Protocol pattern; mirrors the 8 shipped backends |
| Premise 3 | Layer placement | Settings layer (consumed by existing layers); snapshot at `agent.call()` entry; tool dispatch consults the snapshot on every dispatch site WITHOUT re-querying the backend |
| Premise 4 | Mandate-vs-Policy boundary | Holds, with single `policy_decision` event family + `decision_kind` discriminator |
| Premise 5 (revised per D7) | Default semantics | No `policy.md` = no opinion. `policy.md` present but agent NOT in `agents:` section = fleet-default rules apply. `policy.md` present and agent IN `agents:` section = per-agent override (MIN-composed for caps, MERGE+UNION for allowlists, REPLACE for model) |
| Premise 6 | Cross-host bound | (replica count) × (per-call cost_cap ceiling); per-call ceiling is the safety net |
| D1 | `delegate.py` threading | Thread it (coordinator's `PolicyBackend` flows to delegates per PR 2) |
| D2 | Cost-cap math | Per-dimension MIN (daily / monthly independent; cumulative deferred to v1.1 per plan-subagent D1) |
| D3 | Enforcement flag granularity | Single `ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP` flag covering tools + MCP + model together |
| D4 | Strict-mode capability | Don't expose in v1; parser handles deny-default via `tools.allow:` non-empty |
| D5 | Cache TTL | Expose `PolicyCapabilities.cache_ttl_s: int | None`; filesystem returns 0 with mtime+size-gated parse cache as filesystem-specific behavior |

## `PolicyBackend` Protocol surface

Every backend implementation MUST satisfy this contract (structurally; do not subclass — the Protocol is `@runtime_checkable`):

```python
@runtime_checkable
class PolicyBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    def get_effective_caps(self, agent_name: str) -> CostCaps: ...
    def is_tool_allowed(self, agent_name: str, tool_name: str) -> bool: ...
    def is_mcp_server_allowed(self, agent_name: str, server_name: str) -> bool: ...
    def get_effective_model(self, agent_name: str) -> str | None: ...

    def capabilities(self) -> PolicyCapabilities: ...
```

Where `CostCaps`, `PolicyCapabilities`, and the `PolicyDecision` event schema are frozen dataclasses defined in `atomic_agents.policy.types`. See §"Canonical types" below.

**Registry primitives** mirror the established pattern (Lock / Log / Profile / LLM / Judge already ship these; Mandate is the outlier that didn't ship `unregister_*`):

```python
register_policy_backend(backend_id: str, cls: type[PolicyBackend]) -> None
unregister_policy_backend(backend_id: str) -> None
get_policy_backend(backend_id: str) -> type[PolicyBackend]
list_policy_backends() -> list[str]
get_default_policy_backend(scope_root: Path) -> PolicyBackend
```

`register_policy_backend` silently replaces on collision (matches Lock pattern); `_bootstrap_filesystem()` at module bottom is idempotent (checks presence first). `unregister_policy_backend` is idempotent (no-op if absent) and used by conformance fixtures for register-in-setup + unregister-in-teardown hygiene (closes registry pollution across test suites).

`get_default_policy_backend` honors the `ATOMIC_AGENTS_POLICY_BACKEND` env var (default `"filesystem"`). Unknown values raise `BackendNotRegistered` with credential-redacted error messages (URL credentials are stripped via the `_redact_for_error_message` helper).

## `policy.md` file format

`policy.md` lives at the **project-root only** on filesystem. Per-agent configuration constraints stay in `model.md` / `tools.md` / `mcp.md` / `judges.md` exactly as today — there is no per-agent `policy.md`, and no cascade between project-root and per-agent. Per the design's locked answer: Policy is fundamentally fleet-wide; adding a per-agent file would duplicate the per-agent surface and create a "which layer wins inside one agent" question with no clean answer.

Format: markdown wrapping embedded YAML (matches the `model.md cost_guardrails:` block convention). Both a fenced ` ```yaml ` block or a fence-less YAML body are accepted by the parser.

Worked example:

```yaml
# Top-level fleet defaults — apply to any agent not explicitly overridden
cost_caps:
  daily_usd: 50.0
  monthly_usd: 1000.0

tools:
  allow:
    - read_file
    - search
    - write_note
  deny:
    - delete_file

mcp_servers:
  allow:
    - filesystem
    - weather
  deny:
    - insecure-server

model: claude-opus-4-7

# Per-agent overrides
agents:
  caldwell:
    cost_caps:
      daily_usd: 30.0          # stricter; MIN(50, 30) = 30 effective
    tools:
      deny:
        - search                # in addition to fleet deny

  procurement:
    model: gpt-4                # this agent uses gpt-4 instead of fleet's claude-opus-4-7

  empty-agent: {}               # no override; fleet defaults apply (F12)
```

### Parser composition (the F7 resolution)

Composition between fleet defaults and per-agent overrides:

- **Cost caps:** per-dimension MIN. `effective_daily = min(fleet.daily, agent.daily)`. `None` at any layer means "no opinion at this layer" and drops out of MIN.
- **Tool / MCP allowlist:** field-merge with deny-takes-precedence within layer.
  - `effective_allow = fleet.allow | agent.allow` (union)
  - `effective_deny = fleet.deny | agent.deny` (union)
  - `is_allowed(item)` = `item in effective_deny ? False : (effective_allow is empty OR item in effective_allow)`
- **Model selection:** per-agent REPLACES fleet (there is only one model).
- **Empty agent body (`agents: foo: {}`):** no override; fleet defaults apply. Explicit revocation requires explicit `null` per field (e.g., `cost_caps: null` or `tools: { allow: [] }`).

### Parser validation refusals (`PolicyInvalid`)

- Malformed YAML.
- Negative cost-cap value.
- `agents:` is not a mapping (e.g., a list).
- `agent_name` containing path-traversal tokens (`..`, `/`, `\`), control characters, newlines, or leading dots; or not matching `[a-zA-Z0-9_.+@-]+` (D2 loosened charset — dots, plus, at-sign permitted to cover real operator names like `caldwell.research`, `ops@fleet`, `team-2024+ops`).
- Tool / MCP server name containing control characters or newlines.

Same tool in both fleet allow AND fleet deny is NOT a refusal (deny wins per the in-layer rule) but emits a logger warning at parse time.

## Cache contract

`PolicyCapabilities.cache_ttl_s` means **"operator-observable upper bound on staleness at the API boundary"** — NOT internal cache TTL.

- `cache_ttl_s=None`: no staleness contract; the backend is expected to be authoritative on every read (no cache, or content-keyed cache with no time bound).
- `cache_ttl_s=0`: fresh on every call as observed at the API boundary. The reference `FilesystemPolicyBackend` returns 0 — operators observe edits within 0 seconds of mtime change.
- `cache_ttl_s=N` (positive): operator-observable upper bound is N seconds. Dynamic backends (Postgres, SaaS) declare their real internal TTL.

This contract permits internal optimizations (like the filesystem reference's mtime+size-gated parse cache) as long as they do not change operator-observable staleness. SaaS adapters with LRUs declare their LRU TTL; operators reading `doctor.check_policy_backend` see the bound and size fleet-cap headroom accordingly.

### Filesystem cache shape

The reference `FilesystemPolicyBackend` holds one cached `PolicySnapshot` keyed by `(st_mtime_ns, st_size)` from `os.stat(<project_root>/policy.md)`. On every method call:

1. `os.stat(policy.md)` — if `FileNotFoundError`, return no-opinion `PolicySnapshot()`.
2. Compute key `(st_mtime_ns, st_size)`. If unchanged since last parse, return cached snapshot.
3. Otherwise parse and cache the new snapshot.

**Same-second edits on 1-second-mtime-granularity filesystems** (legacy HFS+, ext4 without sub-second timestamps): the composite key catches all realistic operator edits via the size proxy. Same-size same-second edits (e.g., changing `daily_usd: 5.0` to `daily_usd: 9.0`) are a known edge case — operators on legacy filesystems can workaround by `sleep 1` between edit and next agent run.

**Concurrent parse is idempotent.** Two threads simultaneously stat → parse → store on the same mtime change both produce identical `PolicySnapshot` objects; last-write-wins on the cache is correct. No lock — adding one costs more than the occasional duplicate parse work. Documented because SaaS adapters with expensive load semantics SHOULD lock-serialize their own cache loads.

## Composition math

Per Premise 1 (most-restrictive wins) + D2 (per-dimension MIN; cumulative deferred to v1.1):

```
effective_daily_cap   = MIN(policy.daily, model_md.daily)     # per-call ceiling is separate
effective_monthly_cap = MIN(policy.monthly, model_md.monthly)

# Note: cumulative_usd dimension deferred to v1.1 (plan-subagent D1).
# v1 ships daily + monthly only, matching model.md cost_guardrails dimensions.

per_call_ceiling = agent.call(cost_cap=N)                     # bounds same-dimension cap arithmetic

# Allowlist (tool + MCP)
effective_allow = (fleet.allow | agent.allow)
effective_deny  = (fleet.deny  | agent.deny)
is_allowed(x) = x not in effective_deny AND (effective_allow is empty OR x in effective_allow)
```

`MandateCheck` (spec/29) steps 7-8 consume the PRE-COMPOSED effective caps so Policy and Mandate cost-cap checks share the same arithmetic. Landed in PR 3a — PR 1 + 2 shipped only the contract.

### Denying-layer resolution

When multiple layers contribute the same MIN-composed cap, the `denying_layer` field in `PolicyDecision` names the TIGHTEST contributing layer. Tiebreaker order (most-fleet to least-fleet):

```
policy > mandate > model_md > per_call
```

Example: fleet cap = 50, model_md cap = 30, per_call cap = 30 → `denying_layer = "model_md"` (ties broken toward most-fleet contributor, model_md beats per_call).

## Per-agent vs fleet-default resolution

Per Premise 5 (revised per D7):

| Scenario | Behavior |
|----------|----------|
| `policy.md` absent | All queries return no-opinion (`CostCaps()` / `True` / `None`). Zero-config users see byte-identical pre-#89 behavior. |
| `policy.md` present; agent NOT in `agents:` section | Fleet defaults apply (the top-level fields). Closes the D7 delegate-threading correctness gap: a delegate whose name isn't in `policy.md` gets fleet defaults, not silent-allow. |
| `policy.md` present; agent IN `agents:` section | Per-agent override applies per the F7 composition rules above. |

## Cross-host bound (Premise 6)

Filesystem `policy.md` on a shared filesystem (Cloud Run / Kubernetes / shared NFS) may produce momentarily-different Policy state across replicas. Cumulative cost tracking via `LogBackend` provides eventual consistency, but during the propagation window the cap may be overrun by:

```
worst_case_overrun = (replica_count) × (per_call_cost_cap_ceiling)
```

SaaS / Postgres / org-admin-console adapters with linearizable state get exact-cap semantics (their `get_effective_caps()` implementation makes the consistency guarantee). Document the bound in operator-facing material so fleet operators on shared-FS deployments size headroom against the product, not against the bare "≤1 in-flight call per replica" framing.

## Policy decision event schema

The `policy_decision` event family is the unified audit-trail event for Policy + Mandate denials and Policy model overrides. SaaS / Postgres adapters target this schema from day 1; the field set is frozen for v1.

PR 1 ships the schema as the `PolicyDecision` frozen dataclass in `atomic_agents.policy.types`. PR 3 emits instances via `LogBackend.append(record)`.

```python
@dataclass(frozen=True)
class PolicyDecision:
    decision_kind: Literal["deny", "override"]
    denying_layer: Literal["policy", "mandate", "model_md", "per_call"] | None
    agent_name: str
    axis: Literal["cost_cap", "tool_allowlist", "mcp_allowlist", "model_selection"]
    # axis-specific (None when not relevant):
    cap_dimension: Literal["daily", "monthly", "per_call"] | None = None  # cumulative dropped D1
    attempted_value: float | None = None
    effective_cap: float | None = None
    tool_name: str | None = None
    mcp_server_name: str | None = None
    model_from_md: str | None = None
    model_from_policy: str | None = None
    # enforcement flag:
    enforced: bool = False
    # common:
    cache_ttl_s: int | None = None
    ts: datetime | None = None
    proposal_id: str | None = None
```

**Discriminator semantics:**

- `decision_kind="deny"`: some layer denied the action. `denying_layer` names which (`policy` / `mandate` / `model_md` / `per_call`). Axis-specific fields populate per the surface: `cost_cap` → cap_dimension + attempted_value + effective_cap; `tool_allowlist` → tool_name; `mcp_allowlist` → mcp_server_name.
- `decision_kind="override"`: Policy returned a non-None model selection that supersedes `model.md`. `denying_layer` is None (no denial occurred — the override applied without violation). Axis is `model_selection`; `model_from_md` and `model_from_policy` populate.

**`enforced` field semantics:**

- `enforced=True`: the denial or override blocked / altered the action. Cost-cap denials set `enforced=True` when `cap_action="skip"` (the default — the call is blocked outright). For `cap_action ∈ {"alert", "fallback"}` the call proceeds (alert logs a warning + continues; fallback substitutes a cheaper model + continues) and the cost-cap event records `enforced=False` so the audit trail truthfully reflects whether money was actually spent. Operators reading `LogQuery(primitive="policy_decision", enforced=True)` for billing-incident attribution see only the actually-blocked events.
- `enforced=False`: log-only mode — the denial or override was recorded in the audit trail but the action proceeded. Non-cap surfaces (tools / MCP / model) set `enforced=value_of_ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP` (default `false`). That env-var and those surfaces ship in PR 3b.

Operators reading the audit log filter by `decision_kind` first, then `axis`. The single event family closes the Premise 4 promise that operators see ONE event for "was this Policy or Mandate?" with `denying_layer` answering the question directly.

### `agent_name` character set

The accepted charset for `agent_name` at the API boundary is `[a-zA-Z0-9_.+@-]+` (D2 loosened from the RFC's `[a-zA-Z0-9_-]+`). Still rejected: path-traversal tokens (`..`, `/`, `\`), leading dots, control characters, newlines, empty strings. The loosening covers real operator deployment names observed in the test corpus: `caldwell.research` (dot-qualified), `team-2024+ops` (plus suffix), `ops@fleet` (at-sign scoped).

## Implementer contract for policy backends

A backend that implements the `PolicyBackend` Protocol commits to the contract below. The reference `FilesystemPolicyBackend` (markdown + embedded YAML, mtime+size cache) ships in #89 PR 1; future Postgres / SaaS / org-admin-console adapters slot in via `register_policy_backend(...)` without forking core. Mirrors the spec/22/24/25/29 Implementer contract patterns; Policy has fewer MUSTs (7) because Policy has no state, no reservations, and no lifecycle events beyond `policy_decision`.

**Implementers MUST:**

1. **Path-traversal refusal at the API boundary.** Every Protocol method validates `agent_name` against `[a-zA-Z0-9_.+@-]+` BEFORE any storage access (D2 loosened charset — see §"`agent_name` character set"). Reject path-traversal tokens (`..`, `/`, `\`), control characters, newlines, leading dots, or empty strings with `ValueError`. `tool_name` and `mcp_server_name` validation rejects only control characters and newlines (allows dots, dashes, colons because `mcp:server:tool.name` is legitimate). The reference filesystem backend rejects before any dict access; SQL backends parameterize but still refuse at the API boundary as defense-in-depth.

2. **Per-agent isolation enforced at the storage layer.** A `get_effective_caps("foo")` query MUST NOT leak agent `bar`'s caps. For SQL backends, every query filters `WHERE agent_name = ?`. For filesystem, the parsed `PolicySnapshot.agent_overrides` dict is keyed strictly by `agent_name` — no fallback to fuzzy matching, no prefix matching.

3. **Fresh re-read with `cache_ttl_s`-bounded staleness.** The backend's `capabilities().cache_ttl_s` value is a contract — operators read it via `doctor.check_policy_backend` and size fleet-cap headroom accordingly. Backends declaring `cache_ttl_s=0` MUST observe operator edits at filesystem-granularity (the reference filesystem backend's `(mtime_ns, st_size)` composite key). Backends declaring `cache_ttl_s=N>0` MUST refresh internally within N seconds. Backends declaring `cache_ttl_s=None` make no staleness contract (the backend is authoritative on every read).

4. **Construction is side-effect-free.** Backend `__init__` MUST NOT stat the filesystem, query a database, call an external API, or read any environment variable. The first method call performs lazy initialization. Malformed operator config MUST surface on the first method call, NOT at construction. This preserves the framework's byte-identical-construction promise for the 115 existing `AtomicAgent(...)` test sites under PR 1's "no consumption" scope and PR 2's "wired but unconsumed" scope. The URL factory (e.g., `make_filesystem_policy_backend_from_url`) MUST NOT validate path existence at construction either.

5. **Capability honesty.** `capabilities() -> PolicyCapabilities` is a contract, not a hint. Backends declaring a capability MUST implement the corresponding behavior such that the parametrized conformance suite's capability-gated tests pass. Backends declaring `False` get the capability-specific tests skipped (not silently passed). Conformance tests gate on the flag; backends that lie produce silent failures rather than loud refusals.

6. **URL credential redaction in factory `ValueError` sites.** The URL factory and `get_default_policy_backend` error paths MUST NOT echo raw URL credentials. The reference uses a `_redact_url` / `_redact_for_error_message` helper that strips after `://` and truncates. Operators may accidentally paste `postgres://user:password@host/db` into `ATOMIC_AGENTS_POLICY_BACKEND` or pass a credentialed URL to the factory; the error message MUST be safe to surface in `doctor` output and CI logs.

7. **`PolicyDecision` event schema compliance.** Backends emitting `policy_decision` events (PR 3 onwards) MUST use the schema in §"Policy decision event schema" above without extending field semantics. Custom backend-specific fields MAY be added in an `extra` dict if needed for backend-internal debugging, but the canonical fields keep their canonical semantics. SaaS adapters MUST emit events that the dashboard's `policy_decision` filter can parse uniformly.

*Count provisional; PR 4 may adjust based on PR 3 adversarial review findings.*

The reference `FilesystemPolicyBackend` implementation in `atomic_agents/policy/filesystem.py` is the canonical example of this contract. Future Postgres / SaaS / org-admin-console adapters should mirror its shape; the conformance suite (`tests/test_policy_protocol_conformance.py`) parametrizes across registered backends so the contract is verified by the same tests that pin `get_effective_caps` / `is_tool_allowed` / `is_mcp_server_allowed` / `get_effective_model` / `capabilities` semantics.

## Out of scope

This spec describes the **what** and the **where**. It does not pin:

- **AtomicAgent wiring** — PR 2 of #89 adds `AtomicAgent(..., policy_backend=...)` kwarg + per-runner kwargs + `doctor.check_policy_backend` + delegate threading per D1.
- **Consumption logic** — PR 3 of #89 wires `_check_cost_guardrails` MIN composition, `MandateCheck` steps 7-9 pre-composed cap consumption, tool dispatch site, MCP discovery site, model-selection site, and `policy_decision` event emission behind the `ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP=false` flag (log-only mode for non-cap surfaces; cost caps enforce immediately).
- **Flag flip to enforce** — PR 4 of #89 flips `ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP` default to `true`, locks this spec, and bumps the framework's status from "eight backend protocols shipped" to "nine".
- **Strict-mode capability flag** (D4 locked "don't expose in v1"). If compliance-shaped deployments demand explicit strict-mode audit posture later, retrofit `PolicyCapabilities.strict_mode` as an additive change.
- **`is_model_allowed(model_name) -> bool`** — model-selection allowlist semantics deferred. Current `get_effective_model() -> str | None` covers the override case; allowlist is additive.
- **Spec-defined model-compatibility families** — `get_effective_model()` override compatibility is operator-owned (D9 fold #2); framework does not enforce.
- **Per-surface enforcement flags** (`..._ENFORCE_TOOLS` / `..._ENFORCE_MCP` / `..._ENFORCE_MODEL`) — D3 locked single flag; if soak reveals real partial-ungate need, splitting is additive.
- **Cross-process Policy state synchronization** — the cap-overrun bound (Premise 6) is the documented eventual-consistency contract. Linearizable cross-host semantics ship via SaaS / Postgres backends' own consistency layers, not the Protocol.
- **Strangler-fig replacement of existing `model.md cost_guardrails`** — `model.md` continues to work unchanged; PolicyBackend layers on top via MIN-composition.

## References

- `docs/spec/03-file-formats.md` — the embedded-YAML-in-markdown convention `policy.md` follows
- `docs/spec/06-multi-agent-projects.md` — project-root resolution patterns
- `docs/spec/09-cost-observability.md` — cost event format that `policy_decision` extends
- `docs/spec/17-tools.md` — `tools.md` (per-agent tool allowlists composed with Policy)
- `docs/spec/19-mcp.md` — `mcp.md` (per-agent MCP server lists composed with Policy)
- `docs/spec/22-log-backend.md` — `LogBackend` (the `policy_decision` event family flows here)
- `docs/spec/27-doctor.md` — extended with `check_policy_backend` in PR 2
- `docs/spec/28-judge-layer.md` — judge layer (MandateCheck consumes pre-composed caps in PR 3)
- `docs/spec/29-mandates.md` — Mandate primitive (sibling boundary; both denials emit the unified `policy_decision` event)
- `~/.gstack/projects/dep0we-atomic-agents-stack/dep0we-main-design-20260519-084540.md` — locked scope-design + /plan-eng-review output with 11 decisions + 14 plan-subagent prep findings
- #89 — the umbrella issue

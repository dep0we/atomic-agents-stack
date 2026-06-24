# spec/51 — AgentRegistryBackend Protocol

**Status: DRAFT** | Introduced: v2.0.0 | Issue: #607 (epic: #606)

---

## Purpose

`AgentRegistryBackend` is the twenty-second backend Protocol added to the framework. It closes the fleet-discovery gap: prior to this spec, `discover_agents()` (dashboard `costs.py`) walked the filesystem directly, was implicitly coupled to the `log/` directory as the discovery sentinel, and excluded any agent that had not yet written a run. This meant a just-deployed agent was invisible to the dashboard until its first run.

This Protocol decouples fleet discovery from the log-file layout, provides a typed surface for per-agent governance metadata (`governance.md`), and opens the path for future backends (database-backed service registry, cloud metadata stores) to replace filesystem scanning.

**Key design constraint:** `AgentRegistryBackend` is a fleet-scoped read-only discovery Protocol in the reference implementation. Its `agents_root` constructor parameter is the parent directory containing all agent folders — the same root passed to `AtomicAgent(agents_root=...)`. Write operations (`register_agent`, `unregister_agent`) are declared on the Protocol surface but raise `RegistrationNotSupported` on the filesystem reference implementation.

---

## What it replaces

Before this spec, `discover_agents(agents_root)` in `atomic_agents/dashboard/costs.py` implemented agent discovery by walking `agents_root` and returning any subdirectory that contained a `log/` subdirectory. This predicate:

1. Excluded newly-deployed agents that had no run history.
2. Included non-agent directories that happened to contain a `log/` folder.
3. Was not overrideable — operators with non-filesystem agent catalogs had no injection point.

The `ADOPT-NOW` change in this PR rewires `discover_agents()` to a thin adapter over `FilesystemAgentRegistryBackend.list_agents()`, preserving the `list[str]` return type for backward compatibility.

---

## Discovery predicate (spec/37:314)

The `FilesystemAgentRegistryBackend` uses the predicate from spec/37 §"Agent root resolution":

> A directory under `agents_root` is a framework agent if and only if:
> 1. Its name does NOT begin with `_` or `.`.
> 2. A file `model.md` exists within it.
> 3. `model.md` is readable and parseable (`parse_model_md()` tolerates malformed YAML). Exclusion is fail-soft on a hard read or parse failure: an `IOError`, or any other unexpected exception from `parse_model_md()` — the agent is logged and skipped, never crashing discovery (cf. spec/37 §"Agent root resolution", which contemplates a hard parsing failure beyond `IOError`).

The old `log/` predicate is replaced. A just-deployed agent (no runs yet) satisfies conditions 1–3 and is discovered immediately.

### Backward-compatibility callout (predicate-change behavior shift)

The `discover_agents()` predicate changed from **`log/`-presence** to **`model.md`-presence**. The new predicate gains a just-deployed agent (no runs yet); it also DROPS, in the opposite direction, any directory that has a `log/` (historical runs and cost) but **no parseable `model.md`**. Such a directory is no longer enumerated by `discover_agents()`, so its historical spend disappears from the dashboard's fleet-wide global cost rollup. This is a known, deliberate behavior change — a "framework agent" is now defined by its config (`model.md`), not by the side-effect of having run before. An operator who wants a `log/`-only directory to keep contributing to the rollup must give it a parseable `model.md` (which is what a real agent has anyway).

---

## governance.md schema

Each agent folder may contain a `governance.md` file. This file is **operator-curated** — `atomic-agents init` creates a stub on first scaffold; add-to-it (re-run) preserves it unchanged.

The file uses one embedded fenced YAML block with the root key `governance:` plus free-prose markdown sections. A valid governance.md looks like:

````markdown
# Governance — my-agent

```yaml
governance:
  owner: alice@example.com
  backup_owner: bob@example.com
  permission_tier: read-only     # read-only | draft-only | writes | sends-or-acts
  customer_data: 'no'            # yes | no | partial
  writes_sor: 'no'               # yes | no | partial
  lifecycle_status: active       # active | paused | deprecated | retired
  created_at: "2026-06-24"
  updated_at: "2026-06-24"
```

## Forbidden actions

- ...

## Failure modes

- ...

## Pause / retire criteria

- ...
````

The structured block is optional. If absent, or if the file is unreadable, `has_governance=False` is returned and the agent entry is still emitted. If the block is present but contains an unknown enum value, the entry is returned with `has_governance=True` and `parse_errors` non-empty (fail-soft).

---

## AgentRegistryBackend Protocol

```python
from pathlib import Path
from typing import Protocol, runtime_checkable
from .types import AgentRef, AgentRegistryCapabilities

@runtime_checkable
class AgentRegistryBackend(Protocol):
    @property
    def backend_id(self) -> str:
        """Unique lowercase identifier for this backend (e.g. 'filesystem')."""
        ...

    def list_agents(self, *, include_governance: bool = True) -> list[AgentRef]:
        """Return all discovered agents as AgentRef entries.

        Behavior is governed by the Implementer Contract below — the single
        authoritative MUST list. In summary: returns [] on absent/empty
        agents_root (MUST 2); each entry satisfies the discovery predicate
        (MUST 4); _- and .-prefixed dirs are excluded (MUST 3); per-agent parse
        failures and out-of-root symlinks are fail-soft — skipped/flagged, never
        raised (MUST 5); entries are sorted lexicographically by id (MUST 6).

        include_governance=False skips the per-agent governance.md read+parse
        (progressive disclosure) — those entries carry has_governance=False,
        governance=None. The discovery predicate and sort order are unchanged.
        """
        ...

    def get_agent(self, agent_id: str) -> AgentRef | None:
        """Return a single AgentRef by id, or None if not found.

        Returns None (not raises) on miss or TOCTOU vanish (MUST 7). Raises
        PathTraversalError for an agent_id equal to '.' or '..', containing a
        path separator, or empty — before any filesystem access (MUST 8).
        """
        ...

    @property
    def capabilities(self) -> AgentRegistryCapabilities:
        """Capability advertisement for this backend.

        capabilities is a @property, not a method call (MUST 9).
        """
        ...

    def register_agent(self, entry: AgentRef) -> None:
        """Register a new agent entry.

        Raises RegistrationNotSupported on read-only backends (MUST 10).
        """
        ...

    def unregister_agent(self, agent_id: str) -> None:
        """Remove an agent entry by id.

        Raises RegistrationNotSupported on read-only backends (MUST 10).
        """
        ...
```

> **MUST numbering:** the **Implementer Contract** below (10 MUSTs) is the
> single authoritative numbering. The per-method summaries above cite those
> Contract numbers; they do not define a separate scheme.

---

## Types

### AgentRef / AgentEntry

```python
@dataclass(frozen=True)
class AgentRef:
    id: str                     # agent folder name; lookup key for get_agent()
    location: str               # absolute path string to agent dir
    discovered_at: str          # ISO-8601 UTC timestamp of this list_agents() call
    has_governance: bool = False
    governance: GovernanceRecord | None = None

AgentEntry = AgentRef           # alias for clarity at get_agent() call sites
```

### AgentRegistryCapabilities

```python
@dataclass(frozen=True)
class AgentRegistryCapabilities:
    backend_id: str
    supports_registration: bool = False
    supports_canonical_export: bool = False   # spec/40 seam — future backends may be True
    single_host_only: bool = False
```

`supports_canonical_export` is explicitly `False` on `AgentRegistryCapabilities` for the filesystem reference implementation. The spec/40 export contract is left open for future backends.

### GovernanceRecord

All fields are optional (default `None` or `()`). Frozen dataclass.

| Field | Type | Allowed values |
|---|---|---|
| `owner` | `str \| None` | free text |
| `backup_owner` | `str \| None` | free text |
| `permission_tier` | `PermissionTier \| None` | `read-only`, `draft-only`, `writes`, `sends-or-acts` |
| `customer_data` | `Tristate \| None` | `yes`, `no`, `partial` |
| `writes_sor` | `Tristate \| None` | `yes`, `no`, `partial` |
| `lifecycle_status` | `LifecycleStatus \| None` | `active`, `paused`, `deprecated`, `retired` |
| `created_at` | `str \| None` | free text (ISO-8601 recommended) |
| `updated_at` | `str \| None` | free text |
| `review` | `ReviewRecord \| None` | sub-dataclass |
| `risk` | `RiskRecord \| None` | sub-dataclass |
| `sources` | `SourcesRecord \| None` | sub-dataclass |
| `actions` | `ActionsRecord \| None` | sub-dataclass |
| `parse_errors` | `tuple[str, ...]` | non-empty when governance is PRESENT_INVALID |

`GovernanceRecord.from_dict()` raises `GovernanceParseError` for unknown enum values. The caller (filesystem backend) catches this and stores the error string in `parse_errors`; the agent entry is still returned.

**YAML-boolean tristate coercion (normative).** The two `Tristate` fields (`customer_data`, `writes_sor`) have the vocabulary `yes` / `no` / `partial`. Under YAML 1.1 (PyYAML `safe_load`), the bare words `yes` and `no` parse to the Python bools `True` / `False`, NOT to the strings `"yes"` / `"no"`. So an operator who fills in the documented unquoted value `customer_data: no` would yield `False`. `from_dict()` MUST coerce a boolean enum value back to its canonical tristate spelling (`True`→`"yes"`, `False`→`"no"`) before validation, so the documented vocabulary parses cleanly whether or not the operator quoted the value. The two tristate fields are the only enums whose vocabulary overlaps YAML's boolean words (`permission_tier` / `lifecycle_status` values are never YAML booleans), so the coercion only ever fires for the tristate case. Without this coercion the documented happy-path value produces a `PRESENT_INVALID` record that discards every other field — including the security-relevant `permission_tier`.

---

## Implementer Contract (10 MUSTs)

1. `backend_id` MUST be a non-empty lowercase string unique across registered backends.
2. `list_agents()` MUST return `[]` (not raise) when `agents_root` is absent, empty, or not a directory.
3. `list_agents()` MUST exclude dirs whose names start with `_` or `.`. `get_agent()` MUST agree with this universe: a `_`- or `.`-prefixed `agent_id` is a miss (returns `None`, not a populated entry), so the registry never resurfaces a deliberately-hidden dir by id.
4. `list_agents()` MUST use the spec/37:314 discovery predicate: `model.md` present AND `parse_model_md()` returns without raising. A hard read/parse failure (an `IOError`, or any other unexpected exception from `parse_model_md()`) is fail-soft skipped, never raised — matching the §Discovery-predicate prose and the impl's broad `except`.
5. `list_agents()` MUST be fail-soft per agent: a single malformed/symlinked agent MUST NOT abort discovery for the rest of the fleet. This covers a corrupt `governance.md`, an out-of-root symlinked agent dir (skipped under the containment guard), AND a per-entry resolution error (an `OSError`/`RuntimeError` from `is_dir()`/`resolve()`, e.g. ELOOP or a permission denial on one entry) — each is logged and skipped, never raised.
6. `list_agents()` MUST return results sorted lexicographically by `id`.
7. `get_agent()` MUST return `None` (not raise) on miss or TOCTOU vanish. A `_`- or `.`-prefixed `agent_id` is a miss (per MUST 3), returned as `None` rather than raised — the dir is inside `agents_root` with a real `model.md`, just not a registry-recognized agent.
8. `get_agent()` MUST raise `PathTraversalError` for an `agent_id` that is empty, equal to `.` or `..`, or contains a path separator (`/` or `\`). A name that merely *contains* `..` as a substring (e.g. `a..b`) is a legitimate folder name and is NOT rejected by this guard — `safe_resolve_under()` provides containment for it.
9. `capabilities` MUST be a `@property` (not a method) returning `AgentRegistryCapabilities`.
10. `register_agent()` and `unregister_agent()` MUST raise `RegistrationNotSupported` on read-only backends. They MUST NOT be silently ignored.

---

## Errors

All new error classes are defined in `atomic_agents/exceptions.py`.

| Exception | Superclass | When raised |
|---|---|---|
| `AgentRegistryError` | `AtomicAgentsError` | Base class for this subsystem |
| `RegistrationNotSupported` | `AgentRegistryError` | `register_agent()` / `unregister_agent()` on read-only backend |
| `GovernanceParseError` | `AgentRegistryError` | Unknown enum value in `governance.md` structured block |

---

## Reference implementation

`FilesystemAgentRegistryBackend` lives in `atomic_agents/agent_registry/filesystem.py`.

- Constructor: `__init__(agents_root: Path)` — side-effect free (no filesystem I/O). Stores `agents_root` UNRESOLVED; `safe_resolve_under()` resolves the root internally on each `list_agents()`/`get_agent()` call, so the constructor performs no `resolve()`.
- `list_agents(*, include_governance=True)`: `iterdir()` walk with a per-entry fail-soft guard (`_`/`.` skip + `safe_resolve_under` containment check, MUST 5) that catches `PathTraversalError` (out-of-root symlink) AND `OSError`/`RuntimeError` (a resolution failure on one entry — ELOOP/permission/symlink-loop on `is_dir()`/`resolve()`), skipping that entry rather than aborting the loop; the resolved path is reused for `AgentRef.location` so the candidate is resolved exactly once under the guard. Then `model.md` existence + `parse_model_md()` try/except IOError (MUST 4), then `_parse_governance()` (skipped when `include_governance=False`).
- `get_agent(agent_id)`: validates `agent_id` (no path separator; not equal to `.` or `..` — a name merely *containing* `..` like `a..b` is allowed, MUST 8), returns `None` for a `_`/`.`-prefixed name (MUST 3 consistency — those dirs are excluded from `list_agents()` and must not be resurfaced by id), wraps all reads in `try/except OSError → None`.
- `capabilities`: `AgentRegistryCapabilities(backend_id="filesystem", supports_registration=False, supports_canonical_export=False, single_host_only=True)`.
- `register_agent()` / `unregister_agent()`: both raise `RegistrationNotSupported`.
- `_parse_governance(agent_dir)`: returns `(has_governance: bool, GovernanceRecord | None)` with five states: ABSENT (False, None), PRESENT_VALID (True, record), PRESENT_INVALID (True, GovernanceRecord with parse_errors), PRESENT_NO_BLOCK (True, None — readable file with no `governance:` YAML block), PRESENT_UNREADABLE (False, None + WARNING log).

---

## Registry and env override

`atomic_agents/agent_registry/__init__.py` exports:

- `register_agent_registry_backend(id, cls)` — process-local registry
- `unregister_agent_registry_backend(id)`
- `get_agent_registry_backend(id)` — raises `BackendNotRegistered` on unknown id
- `list_agent_registry_backends()` — returns registered ids
- `get_default_agent_registry_backend(agents_root)` — resolves from `ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND` env var; raises `BackendNotRegistered` (fail-loud) on unknown value with credential-redacted error message

Auto-registered backends on import:

| id | class |
|---|---|
| `filesystem` | `FilesystemAgentRegistryBackend` |

---

## ADOPT-NOW: discover_agents rewire

`atomic_agents/dashboard/costs.py::discover_agents()` is a thin adapter:

```python
def discover_agents(agents_root: Path) -> list[str]:
    from ..agent_registry import (
        FilesystemAgentRegistryBackend,
        get_default_agent_registry_backend,
    )

    try:
        backend = get_default_agent_registry_backend(agents_root)
        return sorted(ref.id for ref in backend.list_agents(include_governance=False))
    except Exception as exc:
        # Degrade rather than crash every dashboard tab. ANY construction or
        # enumeration failure falls back here — a typo'd env var
        # (BackendNotRegistered) AND a registered backend whose __init__ or
        # list_agents() raises (e.g. a future DB-backed registry that opens a
        # connection). The doctor check is where fail-loud belongs.
        logger.warning(
            "agent registry discovery failed (%s); falling back to the "
            "filesystem registry. Run `atomic-agents doctor`.",
            type(exc).__name__,
        )
        # The fallback is ITSELF guarded: with the env var unset (the home-user
        # default) the failing backend WAS the filesystem default, so a naive
        # fallback re-runs the same code and re-raises. FilesystemAgentRegistry-
        # Backend.list_agents() CAN raise mid-loop (ELOOP/permission OSError on
        # is_dir()/resolve()), so degrade to [] (the original absent-root
        # behavior every downstream caller tolerates) rather than crashing.
        try:
            fs = FilesystemAgentRegistryBackend(agents_root)
            return sorted(ref.id for ref in fs.list_agents(include_governance=False))
        except Exception as fallback_exc:
            logger.warning(
                "filesystem fallback discovery also failed (%s); returning [].",
                type(fallback_exc).__name__,
            )
            return []
```

Return type stays `list[str]`. Every cross-module caller of `discover_agents()` (`memory.py`, `goals.py`, `activity.py`, `quality.py`, `render.py`) plus the in-module `aggregate_global()` call site in `costs.py` receives the fix without changes.

Two deliberate refinements over a naive thin adapter:

1. **Progressive disclosure (Principle #6):** `discover_agents()` needs only the id list, so it passes `include_governance=False`. `list_agents()` then SKIPS the per-agent `governance.md` read+parse. `discover_agents()` is called ~10x per dashboard render; parsing every agent's governance on each call would be wasted work for an org fleet.
2. **Resilience:** the dashboard render path is read-only and must never crash a whole render on a registry problem, so it catches ANY backend construction failure (an unregistered id from a typo'd `ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND`, OR a registered backend whose `__init__` or `list_agents()` raises) and falls back to the filesystem default. The `except` spans both construction and enumeration so a non-filesystem backend that raises from `list_agents()` degrades identically — the resilience promise is cross-backend, not filesystem-only. The fallback enumeration is ITSELF guarded: with the env var unset (the home-user default) the failing backend WAS the filesystem default, so a naive fallback would re-run the same code and re-raise — `FilesystemAgentRegistryBackend.list_agents()` can raise mid-loop (an ELOOP/permission `OSError` on `is_dir()`/`resolve()`); a second `except` degrades to `[]` (the original absent-root behavior every downstream caller tolerates) rather than crashing the render. The breadcrumb logs only the exception TYPE (a DB-backed backend may embed a DSN in its error text). The fail-loud signal still surfaces via `check_agent_registry_backend()` (the doctor check is where fail-loud belongs).

---

## ADOPT-NOW: atomic-agents init governance.md stub

`atomic-agents init --from-template <advisor|researcher|writer>` writes a `governance.md` stub to the new agent directory. On add-to-it (re-run against an existing agent), the existing `governance.md` is preserved unchanged — it is operator-curated and must not be overwritten by a template re-render.

---

## Doctor check

`check_agent_registry_backend(agents_root: Path) -> CheckResult` runs as part of `run_doctor()`.

- Construction probe: instantiate backend via `get_default_agent_registry_backend(agents_root)`.
- Capabilities probe: access `backend.capabilities` as a property.
- Liveness probe: call `backend.list_agents()` and report `agent_count`.
- Reconcile sub-probe (bidirectional): compare discovered agent ids against `AgentProfileBackend`-registered ids and WARN on a mismatch in EITHER direction — (A) registry-has / profile-missing: an agent with `model.md` but no profile sentinel (`persona/IDENTITY.md` or `persona.link.md`); and (B) profile-has / registry-missing: an agent the profile backend knows whose `model.md` is absent/unreadable, so it vanished from discovery. Direction B is the one most worth surfacing. The sub-probe is best-effort: if the profile probe itself errors, `detail["reconcile_skipped"]` records the exception type so a clean result is not mistaken for a passed reconcile.
- Returns: PASS / WARN (reconcile gap) / FAIL (construction or liveness error).

Without `--agent` (fleet-level `run_doctor()` call): emits SKIP for `agent-registry-backend` in the consistent check-roster, consistent with other fleet-scoped checks.

With `--agent my-agent` (per-agent call): runs the full check and includes `agent-registry-backend` exactly once in results.

Credential redaction: the raw `ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND` value MUST NOT appear in `CheckResult.message` or `CheckResult.detail`. The `_redact_for_error_message()` helper in `agent_registry/__init__.py` returns `scheme://...` for a `://`-scheme URL and `[redacted-connection-string]` for a schemeless `user:pass@host/db` DSN (both short-circuit, no further truncation); only a non-matching value is truncated at 32 chars.

---

## Test coverage

| File | Tests | Scope |
|---|---|---|
| `tests/test_agent_registry_conformance.py` | 37 | Protocol conformance (cites the spec/51 Implementer-Contract MUST numbers), GovernanceRecord round-trip, env override |
| `tests/test_agent_registry_filesystem.py` | 39 | Filesystem edge cases (incl. deferred-YAML-error governance parse, `a..b` legitimate name, `include_governance=False` skip, get_agent `_`/`.`-prefix exclusion (MUST 3 consistency with list_agents), bidirectional reconcile, bogus-env degrade, backend-`__init__`-raise + `list_agents()`-raise discover degrade, filesystem-default-raises discover degrade-to-`[]`, two MUST-5 per-entry fail-soft negative controls (`is_dir()`/`resolve()` raise → skip one entry, enumeration survives), `_`-prefixed full-agent-dir reconcile filter), doctor wiring, init template, redaction |
| `tests/test_dashboard_costs.py` | +2 | discover_agents predicate change (model.md sentinel, not log/) |
| `tests/test_init_templates.py` | +1 | governance.md placeholder-render regression (`${agent_name}` substitutes, no literal `$` survives) |

**79 new tests** (37 conformance + 39 filesystem + 2 dashboard-costs predicate-change guards + 1 init-template-render-regression). Suite collect: 5,656 → 5,735.

---

## Versioned normative addenda

### spec/09 — no change

### spec/22 — no normative addendum in PR 1

`discover_agents()` now routes through the registry. The JSONL audit shape for runs is unchanged — `AgentRef` metadata is fleet-discovery-only and does not appear in run records.

### spec/40 — canonical export seam

`AgentRegistryCapabilities.supports_canonical_export = False` on the filesystem reference implementation. Future backends may advertise `True` and implement the export contract defined in spec/40. No normative text is added to spec/40 in PR 1.

---

## Not in PR 1

- `export()` method and `AgentRegistryExport` type (spec/40 integration deferred)
- PostgreSQL or Redis reference implementations
- Write-capable backend (e.g. a shared service registry for multi-host fleets)
- Per-agent governance enforcement (mandate / policy integration)
- spec/40 canonical export contract for `AgentRegistryBackend`

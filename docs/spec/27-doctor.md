# 27 — Preflight Doctor

How an operator verifies an atomic-agents installation is correctly configured
before the first scheduled run, and what every check guarantees.

---

## Overview

`atomic-agents doctor` runs a fixed set of independent checks against the
host and (optionally) one agent's vault. Each check returns a `pass`, `fail`,
or `skip`; the CLI's exit code is `0` if everything passed (skips count as
ok), `1` if any check failed, and `2` if doctor itself crashed.

Doctor is the trust foundation for every deployment runbook in this repo.
Once doctor exits 0 against a host, every later runbook step (`launchd
load`, `cron install`, container probe, etc.) can assume the install is
ready and stop enumerating failure modes by hand.

The implementation lives in `atomic_agents/doctor.py`. The CLI surface
(`cli.py::_cmd_doctor`) is a thin wrapper around `run_doctor()` plus
`render_human()` / `render_json()`.

---

## Module layout

```
atomic_agents/doctor.py         # CheckResult + every check_* function +
                                # run_doctor + render_human / render_json
atomic_agents/cli.py::_cmd_doctor  # CLI wiring; only this file knows about argparse
tests/test_doctor.py            # one PASS + one FAIL test per check + CLI integration
```

---

## CLI

```
atomic-agents doctor [--agent <name>] [--agents-root <path>] [--json] [--no-mcp]
```

| Flag | Meaning |
|------|---------|
| `--agent NAME` | Run agent-scoped checks against this agent. Omit for host-only. |
| `--agents-root PATH` | Override `ATOMIC_AGENTS_ROOT` for this run. |
| `--json` | Emit machine-readable JSON instead of the human report. |
| `--no-mcp` | Skip the MCP handshake check. Faster; safe when servers are remote. |

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | All checks passed (skips ok) |
| 1 | One or more checks failed |
| 2 | Doctor itself crashed (bug in doctor.py) |

A `2` always indicates a bug in this repo — never a misconfigured host.

---

## Check catalogue

Each check is independent. One failure does not abort the others; you get
the full report every run.

### `env`

**Verifies:** `ATOMIC_AGENTS_ROOT` (or the default `~/docs/agents`) resolves
to a directory that exists and is a directory.

**Prevents:** "first run silently writes the agent vault into the wrong
place" — the most common install-time mistake.

### `python`

**Verifies:** `sys.version_info >= (3, 11)` (matches `pyproject.toml`'s
`requires-python`).

**Prevents:** Running on the macOS system Python (3.13/3.14 default) or
on Linux distros with 3.10 or older as `python3`.

### `vault` *(agent-scoped)*

**Verifies:** Required files exist under `<agents_root>/<agent>/`. The
rule depends on the layout:

- **Single-agent layout:** `persona/IDENTITY.md`, `tools.md`, `model.md`,
  and `memory/INDEX.md` must all be present at the instance root.
- **Cascaded layout** (spec/06, `<system>/projects/<project>/agents/<role>`):
  `persona/IDENTITY.md` and `memory/INDEX.md` must be at the instance root.
  `tools.md` and `model.md` may live at the role layer
  (`<system>/roles/<role>/`); doctor follows the same fallback rules as
  `_cascade.resolve_*`.

**Prevents:** First-call `FileNotFoundError` on the persona/memory load
step in `agent.py`, while still letting valid cascaded agents pass.

### `provider-keys` *(agent-scoped, one result per provider)*

**Verifies:**

1. The provider's optional SDK (`openai` for `gpt-*` and `moonshot/*`) is
   importable. `anthropic` is a hard dependency, so it always is.
2. For each provider referenced by `model.md`'s `default_model` or
   `fallback_model`, the production lookup chain (env vars → Keychain →
   `~/.config/atomic_agents/keys.json`) returns a key. Reuses
   `atomic_agents._llm._get_key()` so doctor's verdict can never disagree
   with runtime behaviour.

Provider inference follows `_costs.PRICING` keys: `claude-*` → anthropic,
`gpt-*` → openai, `moonshot/*` → moonshot.

**Prevents:**

- First-call `ImportError` from `_call_openai` / `_call_moonshot` when the
  optional `openai` extra wasn't installed.
- First-call `AtomicAgentsError("No API key found for ...")` hours into a
  scheduled run.

### `model` *(agent-scoped)*

**Verifies:** `default_model` is in `_costs.PRICING`. If
`cost_guardrails_enabled: true`, both `daily_cap_usd` and `monthly_cap_usd`
must be non-zero.

**Prevents:** Silently falling back to fallback pricing for an unknown
model, or enabling guardrails with a `0` cap that disables the feature
without warning.

### `mcp` *(agent-scoped, one result per server; can be skipped)*

**Verifies:** Each server declared in `mcp.md` responds to a stdio
handshake (`session.initialize` + `list_tools`) within
`DEFAULT_MCP_HANDSHAKE_TIMEOUT_SECONDS` (10s). `parse_mcp_md` is called
with `tools.md`'s `read_paths` so any path-shaped server arg outside the
allowed roots fails with `PathTraversalError` at install time — same as
runtime. Returns one `skip` result when `mcp.md` is absent.

**Prevents:**

- Operator deploys to a host where `npx`, `uv`, or another MCP server
  runner isn't on `PATH`, and the failure only surfaces at the first
  agent call.
- An `mcp.md` whose path-shaped args fall outside `tools.md` `read_paths`
  passing doctor but failing at the first agent call.
- A server that starts but never replies hanging doctor forever (the
  MCP `ClientSession` has no default read timeout, so this matters
  especially for the `--json` liveness-probe use case).

`--no-mcp` skips the handshake. Use when the servers are remote (and
slow to connect) or when running doctor in a fast CI context.

### `locks` *(agent-scoped)*

**Verifies:** The agent's `.lock` file is not currently held by another
process. Lingering files are normal — POSIX `flock` releases on death.
Doctor only fails when an active flock is detected. If the lock file
mtime exceeds `stale_seconds` (default 300s), the message includes a
`stale` marker so the operator knows the holder is likely stuck.

**Prevents:** Scheduling a new run while a previous instance is still
holding the lock — `cron`-shaped deployments would otherwise pile up
silently.

### `lock-backend` *(scope-scoped)*

**Verifies:** Operator-config coherence for the LockBackend (spec/21).
Distinct from the `locks` check above: `locks` runs the POSIX-flock
held-state probe through whatever backend is configured; `lock-backend`
verifies that the operator's configured backend (`ATOMIC_AGENTS_LOCK_BACKEND`
and `ATOMIC_AGENTS_LOCK_BACKEND_URL`) actually constructs and is reachable.
Both checks reuse `get_default_lock_backend(agent_root)` so doctor's verdict
and the runtime's first-acquire behaviour cannot diverge.

PASS / WARN / FAIL ladder:

- **PASS** when `ATOMIC_AGENTS_LOCK_BACKEND` is unset or `filesystem` —
  today's deployment shape, no extras required. Detail carries
  `backend_id: filesystem`.
- **PASS** when a non-filesystem `backend_id` (e.g. `redis`) constructs
  via the URL factory AND the lightweight `is_held("")` probe returns.
  Detail includes the credential-redacted URL — `urlparse` + `_replace`
  strips the password from `netloc` so `redis://user:password@host` does
  not leak through CI logs or telemetry.
- **FAIL** when `ATOMIC_AGENTS_LOCK_BACKEND` is set to an id not in
  `list_lock_backends()` (with `redis` treated as known via lazy
  resolution).
- **FAIL** when a non-filesystem `backend_id` is selected but
  `ATOMIC_AGENTS_LOCK_BACKEND_URL` is unset.
- **FAIL** when the registered backend's optional extra isn't installed
  (`ImportError` surfaces with the `pip install 'atomic-agents-stack[<id>]'`
  fix hint).
- **FAIL** when the factory raises for any other reason during
  construction.
- **WARN** when construction succeeds but the `is_held("")` reachability
  probe raises — backend is configured but not reachable from this host
  (matches `check_provider_keys`' don't-crash-on-optional-infra rule;
  the runtime will fail at first acquire if the backend is truly down).

**Prevents:** First-call `BackendNotRegistered` when an agent's
reservation pattern tries to acquire a lock from an unregistered
backend, and silent fall-through to the filesystem default when an
operator typo'd `ATOMIC_AGENTS_LOCK_BACKEND` — a typo would otherwise
let a multi-host Cloud Run / Kubernetes deployment pile up concurrent
runs against the same agent because filesystem flock doesn't span
hosts.

### `memory-backend` *(agent-scoped)*

**Verifies:** `FilesystemBackend(<agent>, "memory").stats()` returns
without raising.

**Prevents:** Corrupt frontmatter or a missing `INDEX.md` blowing up
inside `agent.call()`'s memory load step.

### `log-backend` *(scope-scoped)*

**Verifies:** Operator-config coherence for the LogBackend (spec/22).
Mirrors `check_lock_backend`'s shape — both checks reuse the framework's
`get_default_log_backend(agent_root)` factory so doctor's verdict and
the runtime's first-`append()` behaviour cannot diverge.

PASS / WARN / FAIL ladder:

- **PASS** when `ATOMIC_AGENTS_LOG_BACKEND` is unset or `filesystem`
  and `FilesystemLogBackend(agent_root).stats()` returns. Detail
  carries `backend_id: filesystem` + the `LogStats` snapshot
  (`total_records`, `records_today`, `records_this_month`,
  `size_bytes`).
- **PASS** when a non-filesystem `backend_id` (e.g. `sqlite`)
  constructs via the URL factory AND `stats()` returns. Detail
  includes the credential-redacted URL plus the same `LogStats`
  snapshot — schema-version health is implicit in the probe (SQLite
  raises if the schema is behind the expected version).
- **FAIL** when `ATOMIC_AGENTS_LOG_BACKEND` is set to an id not in
  `list_log_backends()` (which is now authoritative for `sqlite` per
  #61 PR 3 — no lazy forward-pointer reserved-id list).
- **FAIL** when the registered backend's factory raises during
  construction. The verbatim exception text is dropped (connection
  errors from backend constructors commonly embed the full URL with
  credentials) — the `fix_hint` points the operator at DEBUG logging
  for the unredacted exception.
- **WARN** when construction succeeds but `stats()` raises — backend
  reachable but schema-degraded or transient I/O error. Same
  credential-redaction rule applies to the probe-error path.

**Prevents:** First-`append()` `BackendNotRegistered` when an agent
records its run metadata to an unregistered backend; silent
fall-through to JSONL-on-disk when an operator typo'd
`ATOMIC_AGENTS_LOG_BACKEND` (would defeat the dashboard-perf win on
multi-replica deployments that pinned SQLite for indexed queries);
URL credential leakage from `redis://user:pw@host`-shaped envs into
CI logs or error-tracking pipelines.

### `persona-backend` *(scope-scoped)*

**Verifies:** Operator-config coherence for the persona backend
(spec/33). Scope-flat (not agent-scoped) because `list_personas()`
enumerates the shared `<scope_root>/.personas/` directory.

PASS / WARN / FAIL ladder:

- **PASS** when `ATOMIC_AGENTS_PERSONA_BACKEND` is unset or `filesystem`
  and `FilesystemPersonaBackend(scope_root)` constructs cleanly +
  `capabilities()` + `list_personas()` return without raising. Detail
  carries the capability snapshot (`supports_save`, `supports_clone`,
  `supports_snapshot`, `supports_subscribe`, `supports_templates`,
  `durable`) plus the discovered persona count.
- **PASS** when a non-filesystem `backend_id` is registered, constructs
  via the URL factory (`ATOMIC_AGENTS_PERSONA_BACKEND_URL`), and the
  `capabilities()` + `list_personas()` probe succeeds. Detail includes
  the credential-redacted URL — username AND password stripped from
  `netloc` so token-as-username URLs (common with managed services) do
  not leak through error-tracking pipelines.
- **FAIL** when `ATOMIC_AGENTS_PERSONA_BACKEND` is set to an id not in
  `list_persona_backends()` (typo or missing package). Echo of the env
  value is redacted at `://` to prevent credential leaks if an operator
  pastes a URL into the id env var by mistake.
- **FAIL** when the registered backend's factory raises during
  construction (credentials dropped from the surfaced exception text).
- **WARN** when construction succeeds but `capabilities()` or
  `list_personas()` raises — the backend is reachable but its probe
  surface is degraded.

**Prevents:** First-call `BackendNotRegistered` when an agent with a
`persona.link.md` tries to resolve its shared persona record; silent
fall-through to the filesystem default when an operator typo'd the env
var; credential leakage from URL-bearing config into liveness-probe
output or error-tracking services.

### `agent-profile-backend` *(scope-scoped)*

**Verifies:** Operator-config coherence for the AgentProfileBackend
(spec/24). Scoped at `agents_root` (not `agent_root`) because the
profile backend is the scope-flat layer that holds ALL agents —
`list_agents()` enumerates siblings under the root. Doctor reports
this check under the `profile-backend` name in `CheckResult.name` for
historical consistency with #63 PR 2's wire-up.

PASS / WARN / FAIL ladder:

- **PASS** when `ATOMIC_AGENTS_PROFILE_BACKEND` is unset or
  `filesystem` and `FilesystemAgentProfileBackend(agents_root)`
  constructs cleanly + `capabilities()` + `list_agents()` return.
  Detail carries the capability snapshot (`supports_save`,
  `supports_clone`, `supports_snapshot`, `supports_subscribe`,
  `supports_skills`, `durable`) plus the discovered `agent_count`.
- **PASS** when a non-filesystem `backend_id` (e.g. `sqlite`) is
  registered, constructs via the URL factory
  (`ATOMIC_AGENTS_PROFILE_BACKEND_URL`; when `=sqlite` without URL,
  defaults to `<scope_root>/.profile.db`), and the `capabilities()`
  + `list_agents()` probe succeeds. Detail includes the
  credential-redacted URL — username AND password stripped from
  `netloc` so token-as-username URLs (Upstash-shaped `ghp_TOKEN@host`,
  PlanetScale `API_KEY@host`) do not leak.
- **FAIL** when `ATOMIC_AGENTS_PROFILE_BACKEND` is set to an id not
  in `list_profile_backends()`.
- **FAIL** when the registered backend's factory raises during
  construction (verbatim exception text dropped — connection errors
  commonly embed credential-bearing URLs).
- **WARN** when construction succeeds but `capabilities()` /
  `list_agents()` raises — backend reachable but its probe surface
  is degraded.

**Prevents:** First-call `BackendNotRegistered` when an agent's
bootstrap path tries to load its profile from an unregistered
backend; silent fall-through to the filesystem default when an
operator typo'd `ATOMIC_AGENTS_PROFILE_BACKEND` (would defeat the
SaaS-shape migration that motivated #63 — a fleet of agents would
keep reading per-agent directory state instead of the operator's
Postgres / SQLite registry).

### `tool-registry-backend` *(scope-scoped)*

**Verifies:** Operator-config coherence for the ToolRegistryBackend
(spec/25). Scoped at `agent_root` because the filesystem reference
is per-agent-rooted (`<agent>/tools/<name>.md` belongs to ONE agent),
distinct from `agent-profile-backend` which sits at the scope-flat
`agents_root` layer. Doctor reuses
`get_default_tool_registry_backend(agent_root)` so the verdict and
the runtime's first-`load_tool` behaviour cannot diverge.

PASS / WARN / FAIL ladder:

- **PASS** when `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` is unset or
  `filesystem` and `FilesystemToolRegistryBackend(agent_root)`
  constructs cleanly + `capabilities()` + `list_tools()` return.
  Detail carries the capability snapshot (`supports_install`,
  `supports_uninstall`, `supports_versioning`,
  `supports_sandbox_validate`, `supports_skills_catalog`, `durable`)
  plus the discovered `tool_count` (0 is the typical case — most
  agents don't ship a `tools/` dir, that's not a failure mode).
- **PASS** when a non-filesystem `backend_id` (e.g. `sqlite`)
  registers, constructs via the URL factory
  (`ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL`;
  `sqlite:///path?agent_scope=<name>`; when `=sqlite` without URL,
  defaults to `<agent_root>/.tools.db` with
  `agent_scope=<agent_root.name>`), and the `capabilities()` +
  `list_tools()` probe succeeds. Detail includes the
  credential-redacted URL.
- **FAIL** when `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` is set to an
  id not in `list_tool_registry_backends()`. The echoed env value is
  redacted at `://` (`scheme://...`) and truncated at 32 chars —
  defends against operators accidentally pasting a credential-bearing
  URL into the id env var.
- **FAIL** when the registered backend's factory raises during
  construction (verbatim exception text dropped to prevent
  credential leak; `fix_hint` points at DEBUG logging for the
  unredacted exception).
- **WARN** when construction succeeds but `capabilities()` /
  `list_tools()` raises — backend reachable but its probe surface
  is degraded.

**Prevents:** First-`load_tool` `BackendNotRegistered` when an agent
calls a registered tool from an unregistered backend; silent
fall-through to filesystem when an operator typo'd
`ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` (a future PyPI / git / SaaS
adapter pinned in production would be bypassed and the agent would
silently read the empty / outdated `<agent>/tools/` dir);
credential leakage from `agent_scope=<name>` query strings or
managed-service URLs into CI logs.

### `mandate-backend` *(scope-scoped)*

**Verifies:** Operator-config coherence for the MandateBackend
(spec/29). Scope-shape mirrors `check_policy_backend` —
mandate descriptors live at `<scope_root>/mandates.md` (project scope)
or `<scope_root>/<agent>/mandates.md` (agent scope); doctor probes the
operator-configured backend resolves cleanly without invoking the
`MandateCheck` judge specialist.

PASS / WARN / FAIL ladder:

- **PASS** when `ATOMIC_AGENTS_MANDATE_BACKEND` is unset or
  `filesystem` and `FilesystemMandateBackend(scope_root)`
  constructs cleanly + `capabilities()` returns + `list_mandates(scope)`
  returns. Detail carries the capability snapshot
  (`supports_revocation`,
  `supports_external_state_change_notification`, `durable`,
  `supports_crash_recovery`) plus the discovered `mandate_count`.
- **PASS** when a non-filesystem `backend_id` is registered,
  constructs via the URL factory
  (`ATOMIC_AGENTS_MANDATE_BACKEND_URL`), and the `capabilities()` +
  `list_mandates()` probe succeeds. Detail includes the
  credential-redacted URL — username AND password stripped from
  `netloc` so token-as-username managed-service URLs do not leak.
- **FAIL** when `ATOMIC_AGENTS_MANDATE_BACKEND` is set to an id not
  in `list_mandate_backends()`. The echoed env value is redacted at
  `://` to prevent credential leaks if an operator pasted a URL into
  the id env var by mistake.
- **FAIL** when the registered backend's factory raises during
  construction (credentials dropped from the surfaced exception
  text).
- **WARN** when construction succeeds but `mandates.md` is absent at
  `scope_root` — no operator-granted authorities exist at this scope.
  Informational so the operator knows they haven't authored mandates
  yet (not a failure — pre-#124 deployments and single-agent home
  users naturally hit this path).
- **WARN** when construction succeeds but `capabilities()` /
  `list_mandates()` raises — backend reachable but its probe surface
  is degraded (e.g. SaaS adapter responds to handshake but state
  table is unreachable).

**Prevents:** First-`MandateCheck` `BackendNotRegistered` when an
agent under a mandate-aware judge tries to validate an action against
its authority record; silent fall-through to filesystem when an
operator typo'd `ATOMIC_AGENTS_MANDATE_BACKEND` (would defeat the
durable-authorization story — a procurement mandate pinned in
Postgres would be bypassed and the agent would read stale or
non-existent filesystem state, allowing actions that have been
revoked in the operator's source of truth); credential leakage from
URL-bearing config into liveness-probe output or error-tracking
services.

### `policy-backend` *(scope-scoped)*

**Verifies:** Operator-config coherence for the PolicyBackend
(spec/32). Scope-scoped at the fleet root — `<scope_root>/policy.md`
declares fleet-default cost caps, tool allowlists, MCP server
allowlists, and model selection that apply across all agents under
the root. When a `cascade` is supplied, doctor also warns when a
`FilesystemPolicyBackend` is scoped at `agents_root` instead of
`cascade.project_root` (the runtime auto-corrects this; doctor
surfaces it before production traffic hits the wrong scope per #236
fix).

PASS / WARN / FAIL ladder:

- **PASS** when `ATOMIC_AGENTS_POLICY_BACKEND` is unset or
  `filesystem` and `FilesystemPolicyBackend(scope_root)` constructs
  cleanly + `capabilities()` returns AND `policy.md` exists. Detail
  carries the capability snapshot (`cache_ttl_s`, `durable`),
  `policy_md_exists: true`, and the resolved `policy.md` path.
- **PASS** when a non-filesystem `backend_id` is registered,
  constructs via the URL factory, the `capabilities()` probe
  succeeds, AND `policy.md` exists. Detail includes the
  credential-redacted URL (`urlparse` + `_replace` strips username
  AND password from `netloc` — covers token-as-username managed
  services).
- **FAIL** when `ATOMIC_AGENTS_POLICY_BACKEND` is set to an id not
  in `list_policy_backends()`.
- **FAIL** when the registered backend's factory raises during
  construction (verbatim exception text dropped to prevent
  credential leak; `fix_hint` points at DEBUG logging for the
  unredacted exception).
- **WARN** when construction succeeds but `policy.md` is absent —
  every agent operates in no-opinion mode. Informational so the
  operator knows they haven't authored fleet policy yet (not a
  failure — pre-#89 deployments naturally hit this path).
- **WARN** when a `FilesystemPolicyBackend` is scoped at
  `agents_root` in a cascade layout instead of
  `cascade.project_root` (cascade-scope mismatch — runtime
  auto-corrects via re-resolution; doctor surfaces the mismatch so
  an explicit `policy_backend=` kwarg with the wrong scope is
  caught at install time).
- **WARN** when construction succeeds but the `capabilities()`
  probe raises — backend reachable but its probe surface is
  degraded.

**Prevents:** First-`agent.call()` `BackendNotRegistered` when an
operator pinned a non-filesystem PolicyBackend (Postgres / SaaS /
org-admin-console) but the registry doesn't have it; silent
fall-through to filesystem when an operator typo'd
`ATOMIC_AGENTS_POLICY_BACKEND` (would defeat the cross-agent
configuration story — fleet-default cost caps from the operator's
canonical Postgres `policy.md` would be bypassed and each agent
would silently revert to its per-agent `model.md` cap); cascade-shape
scope mismatch where the operator-supplied backend reads from
`agents_root` while the cascade-resolved Policy lookup is keyed
against `project_root`; URL credential leakage into liveness-probe
output.

### `write-paths` *(agent-scoped)*

**Verifies:**

1. `tools.md` declares at least one `write_path` (an empty list would
   make every capture write fail with `WritePathViolation`).
2. Every path in `tools.md`'s `write_paths` exists and the current user
   has `os.W_OK` on it.
3. The agent's `memory/` directory is inside at least one `write_path`
   AND not inside any `read_only_path`. `FilesystemBackend.write_note()`
   enforces both at runtime; doctor enforces them at install time.
4. `memory/` itself is writable on disk (`os.W_OK`) — catches the case
   where the parent in `write_paths` is writable but `memory/` is
   `chmod 0500`.

**Prevents:**

- First capture write failing with `WritePathViolation`, `PermissionError`,
  or a missing-write_path error after the agent has already spent tokens
  generating the response.
- A `tools.md` whose `write_paths` look reasonable but happen not to cover
  the canonical memory location.

---

## Output formats

### Human (default)

```
[ OK ]  env                       agents-root resolves to /Users/jane/docs/agents (default)
[ OK ]  python                    Python 3.12.11 (>= 3.11 required)
[ OK ]  vault                     all required files present under …
[FAIL]  provider-keys[anthropic]  anthropic API key not found
           Choose one:
             - export ATOMIC_AGENTS_ANTHROPIC_KEY='<key>'
             - security add-generic-password -a $USER -s atomic-agents-anthropic -w '<key>' (macOS Keychain)
             - add {"anthropic": "<key>"} to ~/.config/atomic_agents/keys.json
[ OK ]  model                     default_model 'claude-sonnet-4-6-20260101' priced; guardrails ok
[skip]  mcp                       --no-mcp specified; skipped
[ OK ]  locks                     no lock file (agent has not run yet)
[ OK ]  memory-backend            FilesystemBackend ok (12 notes)
[ OK ]  write-paths               all 3 write_paths exist and are writable

FAIL — 1 failed, 7 passed, 1 skipped
```

The human format always surfaces the `fix_hint` for failing checks. The
hint is one or more concrete commands the operator can copy-paste.

### JSON

```json
{
  "results": [
    {
      "name": "env",
      "status": "pass",
      "message": "agents-root resolves to /tmp/x (--agents-root /tmp/x)",
      "fix_hint": "",
      "detail": {"path": "/tmp/x", "source": "--agents-root /tmp/x"}
    }
  ],
  "summary": {"passed": 1, "failed": 0, "skipped": 0, "all_ok": true}
}
```

The JSON format is intended for programmatic consumption — Cloud Run
liveness probes, launchd health checks, CI gates.

---

## Acceptance criteria

- `atomic-agents doctor --agent caldwell` runs against the bundled
  Caldwell sample and exits 0 after `cp -r docs/samples/caldwell
  ~/agents/caldwell` and adding the Anthropic key (env, Keychain, or
  `keys.json`).
- Each check is exercised by at least one PASS test and one FAIL test
  in `tests/test_doctor.py`.
- A failing check's `fix_hint` includes the literal command needed to
  resolve it (e.g. `security add-generic-password ... -s atomic-agents-anthropic -w '<key>'`).
- `getting-started.md` ends with a "Verify your install" step pointing
  at `atomic-agents doctor`.

---

## What this spec does NOT define

- **Auto-remediation.** Doctor never modifies state. Operators run the
  fix hints themselves.
- **Continuous monitoring.** Doctor is a snapshot. For continuous health,
  wrap `doctor --json` in a liveness probe.
- **Cost / quota checks.** Today's spend vs the cap is `dashboard`
  territory, not doctor.
- **External-service reachability beyond MCP.** Provider API keys are
  verified by *resolution*, not by hitting the actual endpoint. A
  smoke-test `--live` mode is a future option (#69 territory).

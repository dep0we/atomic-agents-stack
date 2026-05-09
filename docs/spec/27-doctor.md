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

### `memory-backend` *(agent-scoped)*

**Verifies:** `FilesystemBackend(<agent>, "memory").stats()` returns
without raising.

**Prevents:** Corrupt frontmatter or a missing `INDEX.md` blowing up
inside `agent.call()`'s memory load step.

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

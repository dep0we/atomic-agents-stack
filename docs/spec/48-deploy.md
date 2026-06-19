---
spec: 48
title: atomic-agents deploy — the deployment conductor
status: DRAFT
created: 2026-06-19
issue: 558
---

# spec/48 — `atomic-agents deploy`: the deployment conductor

**Status:** DRAFT — ships with issue #558; locked after conformance tests pass.
Revised after a cross-family (Codex) review and a plan-eng-review architecture finding
(exposure is guide-not-perform; see below).

---

## Purpose

`atomic-agents deploy` is a conductor that takes an operator from "I have an agent
folder" to "the agent is running, supervised, and verified on this machine," by
orchestrating the surfaces that already exist (`init`, `doctor`, `serve`). It owns no
new runtime: it sequences existing commands, installs a supervised process, verifies it,
and then **guides** the operator's network-exposure step without performing it.

The pain it removes (proven by a clean-host dogfood, issue #558): the runtime works,
but nothing walks an operator through install → scaffold → key → run, and the exposure
step is undocumented. `deploy` collapses the first part into one verified command and
prints correct, tailored guidance for the second.

This spec covers the MVP that ships in issue #558:

- `atomic-agents deploy <agent>` — plan + execute a loopback deployment, verify, then
  print exposure guidance
- `atomic-agents deploy status <agent>` — report the live deployment state
- `atomic-agents deploy down <agent>` — tear a deployment down
- `--plan` (dry-run), `--yes` (assume-yes for consent steps), `--verify-call` (opt in to
  a billed end-to-end probe)

**Out of scope in this arc (deferred with tracked issues, or owned by the operator):**

- **Performing network exposure / running any perimeter tool.** `deploy` MUST NOT run
  `tailscale serve`, configure a reverse proxy / IAP / Cloudflare, or terminate TLS. The
  perimeter is the operator's layer (spec/37). `deploy` prints guidance; the operator
  runs it. (The earlier "`--target tailnet` performs Tailscale" design was dropped here.)
- `--print-only` / structured-manifest emit for CI and the cloud phase — the cloud
  target that would consume it is out of MVP scope (no abstractions for hypothetical
  needs). `--plan` covers the present dry-run need.
- Container / cloud targets — a later phase; the MVP optimizes the home/Mac path.
- A writable secret store and a non-mutating HTTP `/call` probe — see Provider key and
  Verification below; both are named runtime follow-ups, not assumed here.

---

## What `deploy` is and is NOT

`deploy` is a **verb**, not a config layer, not a runtime, and not a perimeter. It MUST
NOT reimplement `init`/`doctor`/`serve`; it invokes them. It MUST NOT introduce a new
config-file format: an agent's identity/config stays in its markdown (CLAUDE.md rule 7),
and the persistent record of *what was deployed* is the launchd label + plist on disk —
not a bespoke deploy state file. It MUST NOT perform network exposure (spec/37: the
operator owns auth/TLS/perimeter). `deploy status` / `deploy down` read launchd state,
not a cached sidecar.

The boundary, stated plainly: **`deploy` owns getting the agent running and verified on
loopback; the operator owns the perimeter.** `deploy` bridges the two with accurate
guidance, never by reaching into the operator's network.

---

## Execution model — planner → executor

`deploy` builds an ordered **plan** of steps, then **executes** it, then **verifies**,
then **guides exposure**. It never runs `serve` in its own process (serve blocks); it
installs a launchd agent whose program invokes `atomic-agents serve` and returns.

Each step carries a tag, decided by a stated predicate:

- `auto` — user-space, no consequence beyond the agent's own folder/process. Run silently.
- `consent` — automatable but touches shared/user state (a shell-profile edit, installing
  the launchd agent, running `init`). Prompt unless `--yes`.
- `manual` — unautomatable or operator-owned (provider-key setup; network exposure).
  Print precise instructions and pause/finish.

`--plan` prints the tagged plan and exits without executing anything and without billing.

### Step list (macOS)

1. Preflight: Python 3.11/3.12 (offer uv-pinned install) `[consent]`; PATH includes the
   tool install dir `[auto if present | consent if a profile edit is needed]`;
   `ATOMIC_AGENTS_ROOT` resolved `[auto]`.
2. Agent exists? If the folder is absent, either run `atomic-agents init <agent>
   --agents-root <root>` (interactive — `[consent]`) or, with `--yes`, fail with
   "agent not found; run `atomic-agents init <agent>` first." `init` is interactive and
   writes files, so it is never an `auto` step.
3. `doctor` gate — must pass (post-#541 templates are healthy). Run `doctor --agent
   <agent> --no-mcp` unless the agent uses MCP. `[auto]`
4. Provider key: run `doctor`'s provider-key check; if missing, print the three supported
   setup options (env `ATOMIC_AGENTS_<PROVIDER>_KEY` / macOS Keychain
   `atomic-agents-<provider>` / `~/.config/atomic_agents/keys.json`) and pause, then
   re-check. `deploy` does NOT store the key itself — SecretBackend (spec/38) and the
   `secrets` CLI are read-only. `[manual]`
5. Resolve port; render + `bootstrap` the user-level launchd agent (see Supervision).
   `[consent]`
6. Verify (see Verification): healthz ok + doctor pass predicate, on loopback. `[auto]`
7. Exposure guidance (guide, NOT perform — see Exposure guidance). `[manual]`
8. On any post-install failure in steps 5-6: rollback (see Rollback) + recovery message.

---

## Supervision — user-level launchd agent

`deploy` installs a per-user launchd agent (domain `gui/$UID`, no `sudo`).

- **Label / plist:** `ai.atomic-agents.serve.<slug>`, where `<slug>` is `<agent>` run
  through the same agent-name charset/validation `init` enforces (a route/path segment is
  not guaranteed launchd-label-safe). Plist at `~/Library/LaunchAgents/<label>.plist`.
  One label per agent — this is the deploy state record.
- **ProgramArguments:** an ABSOLUTE executable path, not the bare `atomic-agents` (a
  `gui/$UID` agent does not inherit the interactive PATH). Resolve via
  `shutil.which("atomic-agents")`, falling back to `[sys.executable, "-m",
  "atomic_agents.cli", "serve", "<agent>", "--host", "127.0.0.1", "--port", "<port>"]`.
- **Persistence:** `RunAtLoad=true`, `KeepAlive=true`.
- **EnvironmentVariables:** always inject `HOME`, `USER`, `PATH`, and
  `ATOMIC_AGENTS_ROOT`. The provider key is NOT written into the plist by default
  (plaintext-in-plist is a disclosure risk); rely on the macOS Keychain / `keys.json`
  source that serve's `_llm._get_key()` already reads, confirmed by the step-4 `doctor`
  check. Inject the key as a `KEY=VALUE` env var ONLY when its sole source is an env var,
  and document that this writes the key in cleartext to the plist.

Install is `launchctl bootstrap gui/$UID <plist>`; teardown is `launchctl bootout
gui/$UID/<label>`.

### Idempotent re-deploy

Re-running `deploy <agent>` when `<label>` already exists MUST `bootout` then `bootstrap`
(clean restart) rather than failing or double-binding the port.

### Rollback

If verification (step 6) fails after the launchd agent was installed, `deploy` MUST
`bootout` the just-installed agent, remove the plist it wrote, and report the failure with
the recovery command. No bootstrapped-but-broken service is left behind (CLAUDE.md rule 8
— no half-finished state).

---

## Verification

Default verification is free and non-mutating, on loopback, with a defined pass predicate
(a 200 response is not enough — `/doctor` returns 200 with JSON even when checks fail):

1. `GET /agents/<agent>/healthz` — pass iff the JSON `status == "ok"`.
2. `GET /agents/<agent>/doctor` — pass iff `doctor.overall_exit_code(results) == 0` (no
   failing check). The HTTP `/doctor` route already runs with `skip_mcp=True`
   (`serve/_app.py`), so it is cheap and makes no LLM call.

Verification MUST NOT report success on a process start alone, nor on a bare HTTP 200.

`--verify-call` additionally fires a real `POST /agents/<agent>/call`. This bills tokens
and writes a capture (the HTTP `/call` route has no write-captures suppression today —
`--no-write-captures` is `run`-only), so it is opt-in, never the default. A non-mutating
HTTP probe mode is a named runtime follow-up.

---

## Exposure guidance (guide, NOT perform)

After a verified loopback deployment, `deploy` prints the operator's next step to reach
the agent from another device. It detects the environment to tailor the guidance, but it
never performs the exposure:

- **Tailscale present** (`tailscale status --json` succeeds): print the exact command —
  `tailscale serve --bg http://127.0.0.1:<port>` — plus the one-time prerequisite (enable
  HTTPS certificates in the tailnet admin console) and a note that the first HTTPS request
  may be slow while the cert provisions. Point at `docs/deployment/serve.md` (the
  authoritative, #543-corrected recipe).
- **Tailscale absent:** print a short pointer to the perimeter options
  (Tailscale Serve, Cloudflare Access, a reverse proxy, IAP) in `docs/deployment/serve.md`,
  and state plainly that the agent is currently loopback-only.

`deploy` MUST NOT run `tailscale serve`, edit a perimeter config, open a firewall, or
terminate TLS. Guidance is text output; the operator runs it.

---

## Port resolution

`deploy` resolves the port using serve's own precedence — an explicit `deploy --port` >
`ATOMIC_AGENTS_SERVE_PORT` > `serve.md` Bind Port > default — and passes the resolved
value explicitly via `--port` in the launchd `ProgramArguments`. Because serve runs inside
launchd (deploy can't read its bind error directly), a bind conflict is detected by a
pre-bootstrap socket-bind probe on the chosen host/port; if the probe fails, `deploy`
fails loud naming the port and how to override, and MUST NOT silently pick a different
port. A post-bootstrap health failure that maps to "address in use" is reported before
rollback.

---

## Open decision (NOT resolved in this spec) — supervision reboot survival

A user-level (`gui/$UID`) launchd agent loads when the user's GUI session becomes active,
which on a headless Mac without auto-login happens only at console login. So the no-`sudo`
supervision path may not survive an unattended reboot. The fork (issue #539):

1. **Require/enable auto-login on the host** as a documented `deploy` prerequisite —
   keeps the clean no-`sudo` `gui/$UID` path.
2. **A system-domain LaunchDaemon** (`/Library/LaunchDaemons`) — survives reboot
   regardless of login, but needs `sudo`, changing the no-`sudo` premise.

Resolution requires a reboot test on the target host and is deferred (maintainer's call;
off the table for now). Until then `deploy` ships the `gui/$UID` install with the
idempotent/rollback semantics above and documents the limitation: "supervised; survives
until reboot — reboot-persistence TBD (#539)."

---

## Implementer Contract (MUSTs)

Normative requirements. Any conforming implementation MUST satisfy all of them.

**MUST 1 — Conductor, not a reimplementation:** `deploy` MUST drive `init`/`doctor`/
`serve` through their existing entry points; it MUST NOT duplicate their logic or run
migrations.

**MUST 2 — No new config format:** `deploy` MUST NOT write a bespoke deploy config/state
file. The deployment record is the launchd label + plist. `status`/`down` MUST derive
state from launchd, not a cached sidecar.

**MUST 3 — No `sudo` in the default path:** the `gui/$UID` install MUST NOT invoke
`sudo`. Any step needing privilege MUST be tagged `consent`/`manual`, never run silently.

**MUST 4 — `deploy` never runs `serve` in-process:** it MUST install a launchd agent
whose `ProgramArguments` invoke `atomic-agents serve` via an absolute executable path
(`shutil.which` or `sys.executable -m atomic_agents.cli`), then return.

**MUST 5 — Environment injection; key sourced safely:** the plist MUST set
`EnvironmentVariables` carrying at least `HOME`, `USER`, `PATH`, `ATOMIC_AGENTS_ROOT`. The
provider key MUST be sourced from Keychain / `keys.json` rather than written into the
plist, EXCEPT when the key's only source is an env var (then it MAY be injected with a
documented cleartext caveat). A conformance test MUST assert the four base vars and
no-plaintext-key-when-a-Keychain/keys.json-source-exists.

**MUST 6 — `--plan` is side-effect-free and unbilled:** it MUST print the tagged plan and
exit without executing any step, installing anything, or making any billed/LLM call.

**MUST 7 — Idempotent re-deploy:** re-running against an existing `<label>` MUST cleanly
restart (`bootout` + `bootstrap`), never double-bind or error.

**MUST 8 — Rollback on post-install verify failure:** on verify failure after install,
`deploy` MUST `bootout` the agent and remove the plist it wrote, then report. No
bootstrapped-but-broken service may remain.

**MUST 9 — Default verification is non-mutating, unbilled, predicate-based:** it MUST use
only `/healthz` + `/doctor`, and pass only when healthz `status == "ok"` AND
`overall_exit_code(doctor results) == 0`. A 200 alone MUST NOT count. A real `/call` MUST
require `--verify-call`.

**MUST 10 — Port resolution deterministic; conflict fails loud:** port MUST resolve
`deploy --port` > env > `serve.md` > default, passed explicitly via `--port`. A bind
conflict (pre-bootstrap probe or post-bootstrap address-in-use) MUST fail with a clear
message and MUST NOT silently rebind.

**MUST 11 — Exposure is guided, never performed:** `deploy` MUST NOT run `tailscale
serve`, configure any perimeter, open a firewall, or terminate TLS. It MUST instead print
accurate, environment-tailored exposure guidance: when Tailscale is detected, the exact
`tailscale serve --bg http://127.0.0.1:<port>` command plus the cert prerequisite and
warm-up note; otherwise a pointer to the perimeter options in `docs/deployment/serve.md`.
The agent's reachability is the operator's perimeter responsibility (spec/37).

**MUST 12 — `down` is complete; `status` is honest and specific:** `down` MUST `bootout`
and remove the plist (full teardown). `status` MUST report a defined state — `absent`,
`loaded`, `running`, or `crashed` — derived at call time from plist existence,
`launchctl print gui/$UID/<label>` (state + PID + LastExitStatus), and optionally a
`/healthz` probe. It MUST NOT infer from a cached file.

---

## Conformance test outline

| MUST | Test |
|------|------|
| 1 | invokes init/doctor/serve via entry points (spy/patch), no inline reimpl, no migrate |
| 2 | no bespoke state file; `status`/`down` read launchd (mocked) |
| 3 | default-path run issues zero `sudo` calls; privileged steps tagged consent/manual |
| 4 | no uvicorn in-process; plist `ProgramArguments[0]` is absolute + `serve` |
| 5 | plist env has HOME/USER/PATH/ROOT; no plaintext key when Keychain/keys.json source exists |
| 6 | `--plan` writes/installs/bills nothing; exits 0 with the plan |
| 7 | second `deploy` → bootout+bootstrap; no port double-bind |
| 8 | forced verify failure → bootout + plist removed; non-zero exit + recovery message |
| 9 | healthz!=ok → fail; doctor FAIL → fail; both pass → success; `--verify-call` hits `/call` |
| 10 | precedence deploy --port>env>serve.md>default; bind-probe conflict → clear error, no rebind |
| 11 | NO `tailscale serve`/perimeter call is ever issued; tailscale-present → exact command printed; absent → perimeter-doc pointer printed |
| 12 | `down` removes plist + boots out; `status` returns absent/loaded/running/crashed from mocked launchctl |

---

## Cross-references

- spec/37 (`atomic-agents serve`) — the runtime `deploy` supervises, and the **boundary
  this spec respects**: the operator owns auth/TLS/perimeter (MUST 11). Port precedence,
  `/healthz` + `/doctor` (skip_mcp).
- spec/35 (`atomic-agents init`) — the scaffolder `deploy` hands off to (step 2); source
  of the agent-name charset reused for the launchd label slug.
- spec/38 (SecretBackend) — read-only; why `deploy` cannot store provider keys (step 4).
- issue #558 — tracking epic. #543 — the corrected exposure docs `deploy` points at
  (MUST 11). #539 — deferred supervision reboot-survival fork. #542 — CLI
  positional-agent consistency. #537 — deferred standalone auth/TLS.
- CLAUDE.md rule 7 (MUST 2), rule 8 (MUST 7/8), "refusal is a feature" (MUST 11: guide,
  don't perform).

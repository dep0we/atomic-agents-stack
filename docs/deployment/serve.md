# Deploying agents with `atomic-agents serve`

`atomic-agents serve` wraps any agent folder in a thin HTTP layer so it can run
inside Cloud Run, GKE, Fly.io, Render, or any container platform.  The
framework owns the agent loop; your infrastructure layer owns TLS, auth,
rate-limiting, and audit logging.

---

## Quick start

```bash
pip install atomic-agents-stack[serve]
atomic-agents serve myagent --host 0.0.0.0 --port 8080 --allow-no-auth
```

**Do not use `--allow-no-auth` in production without a perimeter in place.**
See the [No-auth default](#no-auth-default) section below.

---

## Routes

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/agents/<name>/call` | Invoke `agent.call()`, returns JSON response |
| `GET` | `/agents/<name>/healthz` | Cheap liveness probe (filesystem checks only) |
| `GET` | `/agents/<name>/doctor` | Full diagnostic run — off the hot path |
| `GET` | `/agents` | List available agent names |

---

## Serving one agent vs. all agents

**Single-agent mode** (recommended for production):

```bash
atomic-agents serve myagent --host 0.0.0.0 --port 8080 --allow-no-auth
```

Only routes for `myagent` are reachable; every other name returns HTTP 404.
The agent's own `serve.md` (if present) sets the default bind host, port, and
identity header.

**All-agents mode** (dev / internal tooling):

```bash
atomic-agents serve --all --host 127.0.0.1 --port 8080
```

Every agent folder in the vault root is reachable. Use with care — all agents
share one server process, and any agent folder discovered at runtime becomes
accessible.

---

## No-auth default

The default is **refuse-by-default** (`--allow-no-auth` is the explicit opt-in):

- **Non-loopback binding** (`0.0.0.0`, a VPC address, etc.): the server
  **refuses to start** unless `--allow-no-auth` is passed or
  `## Allow No Auth` is present in `serve.md`.  The error message is:

  ```
  Error: atomic-agents serve refuses to bind to '0.0.0.0' without auth.
  Add --allow-no-auth or '## Allow No Auth' to serve.md only after
  your perimeter (IAP, ALB, Cloudflare Access, Tailscale Serve, etc.)
  is in place. See docs/deployment/serve.md §"No-auth default".
  ```

- **Loopback-only binding** (`127.0.0.1`, `localhost`, `::1`): the server
  starts with a warning on stderr. This is acceptable for local development.

`--allow-no-auth` is a deliberate, auditable consent signal — not a
convenience shortcut.  It tells the framework "I have a perimeter in place;
the framework does not need to enforce auth."  Never pass it without a real
perimeter protecting the port.

---

## IAP / ALB / Cloudflare Access / Tailscale Serve pass-through

The serve layer reads one HTTP header per request and writes its raw value
into the JSONL audit record as `http_caller`.  The header name is configurable;
the default matches Google IAP's assertion header:

```
X-Goog-IAP-JWT-Assertion
```

**The framework never verifies or decodes the header value.**  Your perimeter
(IAP, ALB, Cloudflare Access, Tailscale Serve, etc.) is the trust boundary;
the framework assumes headers that reach it have already been authenticated by
the perimeter.  This is a security invariant — the framework will never add
JWT parsing or signature validation here.

### IAP on Cloud Run

Configure IAP in front of the Cloud Run service; IAP strips unauthenticated
requests before they reach the container and injects
`X-Goog-IAP-JWT-Assertion` on authenticated ones.  The serve layer reads that
header and records the caller identity in every JSONL audit line.

### AWS ALB authentication

Set the ALB listener to "Authenticate" with your OIDC provider.  The ALB adds
`X-Amzn-Oidc-Identity` (or your configured claim header) to authenticated
requests.  Update `serve.md` (or `ATOMIC_AGENTS_SERVE_IDENTITY_HEADER`) to
match:

```markdown
## Identity Header
X-Amzn-Oidc-Identity
```

### Cloudflare Access

Cloudflare Access adds `Cf-Access-Jwt-Assertion` to requests it passes.
Set the identity header to:

```markdown
## Identity Header
Cf-Access-Jwt-Assertion
```

### Tailscale Serve

For local and small-team deployments, Tailscale Serve routes traffic through
your Tailscale network so only enrolled devices can reach the port.  Bind to
loopback and let Tailscale handle the overlay:

```bash
atomic-agents serve myagent --host 127.0.0.1 --port 8080
tailscale serve https / http://127.0.0.1:8080
```

No identity header injection is needed for Tailscale; `http_caller` will be
absent from audit records (callers are authenticated by Tailscale at the
network layer, not at the HTTP header layer).

---

## `serve.md` per-agent config

Place a `serve.md` file in the agent folder to set per-agent defaults:

```markdown
## Identity Header
X-Goog-IAP-JWT-Assertion

## Bind Host
127.0.0.1

## Bind Port
8000

## Allow No Auth
```

| Section | Default | Notes |
|---------|---------|-------|
| `## Identity Header` | `X-Goog-IAP-JWT-Assertion` | Header name for caller identity. |
| `## Bind Host` | `127.0.0.1` | Override to `0.0.0.0` for network binding. |
| `## Bind Port` | `8000` | Integer port number. |
| `## Allow No Auth` | absent (false) | Presence of this section sets `allow_no_auth=True`. |

Environment variable overrides (highest priority):

| Env var | Overrides |
|---------|-----------|
| `ATOMIC_AGENTS_SERVE_HOST` | Bind Host |
| `ATOMIC_AGENTS_SERVE_PORT` | Bind Port |
| `ATOMIC_AGENTS_SERVE_IDENTITY_HEADER` | Identity Header |

Resolution order: env var > `serve.md` section > default.

---

## Cloud Run entrypoint example

A minimal `Dockerfile` that serves a single agent on Cloud Run:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install the serve extra
RUN pip install atomic-agents-stack[serve]

# Copy the agent vault
COPY ./agents /app/agents

ENV ATOMIC_AGENTS_ROOT=/app/agents
ENV ATOMIC_AGENTS_SERVE_HOST=0.0.0.0
# Do NOT set ATOMIC_AGENTS_SERVE_PORT here. Cloud Run injects its own PORT
# env var (not always 8080). The --port flag below honours Cloud Run's PORT
# and falls back to 8080 only when PORT is absent. Setting
# ATOMIC_AGENTS_SERVE_PORT=8080 would shadow Cloud Run's PORT and break
# the container health check if Cloud Run assigns a different port.
CMD ["sh", "-c", "atomic-agents serve myagent --host 0.0.0.0 --port ${PORT:-8080} --allow-no-auth"]
```

Put IAP in front of the Cloud Run service so `--allow-no-auth` is safe — IAP
handles authentication before traffic reaches the container.

For a complete Cloud Run service definition, IAP setup, Cloud Scheduler
triggers, and observability config, see
[`extras/gcp/`](../../extras/gcp/README.md) — the GCP-as-harness reference
deployment.

---

## Liveness probe configuration

Use `GET /agents/<name>/healthz` as the Cloud Run liveness and readiness probe:

- Executes only filesystem checks (agent folder exists, `model.md` parseable).
- No LLM API calls, no MCP subprocess spawns, no provider key probes.
- Returns HTTP 200 `{"status": "ok"}` when healthy, HTTP 503 when degraded.
- Safe to poll every 10 seconds per container.

**Do not use `/agents/<name>/doctor` as a liveness probe.** The doctor
endpoint may spawn MCP subprocesses and probe remote backends — it is
a diagnostic tool, not a health check.

---

## Security notes

**`/doctor` response body disclosure:** The `/doctor` response discloses
absolute filesystem paths (`agents_root`, per-check write paths) and
provider-key-presence booleans for every configured LLM provider.  An
attacker who can reach this endpoint learns which AI providers are keyed and
where agent data lives on disk.  Keep `/doctor` behind the same perimeter as
`/call` (IAP, Tailscale Serve, etc.).

**`critical=True` is structurally unavailable via HTTP.** The serve layer
hard-codes `critical=False` in every `agent.call()` dispatch.  A request body
containing `{"critical": true}` returns HTTP 422.  The cost guardrail cannot
be bypassed from the network layer.  See spec/37 MUST 5.

**Thread-pool saturation under concurrent same-agent load.** The default
thread pool is bounded at approximately `min(32, cpu_count + 4)` threads.
Each concurrent same-agent request parks a thread for up to the 30 s lock
timeout while waiting for the active call to finish.  Enough concurrent
same-agent requests can saturate the pool entirely.  The mitigation today is
to use single-agent mode with per-replica locking at the infrastructure layer
(one Cloud Run instance per agent, autoscaled) or keep `max_instances` sized
to the expected concurrency so the platform handles saturation via instance
spin-up rather than thread queuing.  (A `ATOMIC_AGENTS_SERVE_WORKERS` env var
for explicit pool sizing is a future knob — not yet implemented.)
See TENSIONS.md T2 Trigger B.

---

## Not yet available

The following capabilities are deferred and not yet implemented:

- **SSE streaming responses** — `POST /agents/<name>/call` returns a complete
  JSON response only. Streaming via Server-Sent Events requires a
  `StreamingLLMBackend` protocol that is not yet shipped. Tracked at
  [#105](https://github.com/dep0we/atomic-agents-stack/issues/105).
- **MCP server mode** (`POST /mcp/<name>`) — exposing an agent as an MCP
  server over HTTP is a separate arc. Tracked at
  [#90](https://github.com/dep0we/atomic-agents-stack/issues/90).

---

## Cross-references

- `docs/spec/37-serve.md` — full normative spec (routes, MUSTs, audit record shape)
- `docs/deployment/programmatic.md` — using agents without the HTTP wrapper
- `TENSIONS.md` T2 — the Hybrid Option C thread-pool adapter decision

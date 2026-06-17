---
spec: 37
title: atomic-agents serve — thin HTTP wrapper
status: DRAFT
created: 2026-06-07
issue: 342
---

# spec/37 — `atomic-agents serve`: thin HTTP wrapper

**Status:** DRAFT — ships with issue #342 PR 1; locked after conformance tests pass.

---

## Purpose

`atomic-agents serve` exposes `agent.call()` over HTTP so agents run inside
Cloud Run, GKE, Fly.io, Render, or any container platform. The framework owns
the agent loop; the operator's infrastructure layer owns auth, rate limiting,
TLS, and audit logging (IAP / ALB / Cloudflare Access / Cloud Armor /
Cloud Logging).

This spec covers only the routes that ship in issue #342:

- `POST /agents/<name>/call` — invoke `agent.call()`, return the response
- `GET /agents/<name>/healthz` — cheap liveness check
- `GET /agents/<name>/doctor` — full doctor run (off the hot path)
- `GET /agents` — list available agent names

**Out of scope in this arc (deferred with tracked issues):**

- `POST /mcp/<name>` — MCP server protocol endpoint (own arc, own spec);
  tracked in issue #90 ([backend] MCP server primitives)
- SSE streaming — gated on `StreamingLLMBackend` being implemented; all three
  current LLMBackend impls return `capabilities().streaming == False` (spec/31
  lines 205-210); tracked in issue #105 ([backend] StreamingLLMBackend Protocol)
- Auth, rate limiting, TLS, multi-tenant scoping (operator's perimeter layer)
- WebSockets, gRPC

---

## Installation

```bash
pip install atomic-agents-stack[serve]
```

The `serve` extra adds `starlette` and `uvicorn` as opt-in dependencies.
The base package import (`import atomic_agents`) does NOT import Starlette;
home users pay zero import cost for the serve surface.

---

## Config file — `serve.md`

Each agent may carry a `serve.md` file in its agent folder. The file uses
`## Section` headers matching the `model.md` / `mcp.md` pattern. A missing
`serve.md` is not an error — defaults apply.

```markdown
## Identity Header
X-Goog-IAP-JWT-Assertion

## Bind Host
127.0.0.1

## Bind Port
8000

## Allow No Auth
```

**Section semantics:**

| Section | Default | Notes |
|---------|---------|-------|
| `## Identity Header` | `X-Goog-IAP-JWT-Assertion` | Header name the perimeter uses for verified caller identity. Read-log-passthrough only; the framework MUST NOT verify the header value. |
| `## Bind Host` | `127.0.0.1` | Default to loopback; operators override to `0.0.0.0` for network binding. |
| `## Bind Port` | `8000` | Port number as a string. |
| `## Allow No Auth` | absent (false) | Presence of this section (any value or empty) sets allow\_no\_auth=True. |
| `## Max Body Bytes` | `1048576` (1 MiB) | Maximum request body size in bytes (integer). Larger requests are rejected with HTTP 413. |
| `## Idempotency Header` | `Idempotency-Key` | Header name the caller-supplied idempotency key is read from. Presence with a non-empty body sets the header name; absent → default. Opt-in: absent header on a request → no dedup. (spec/45 PR2) |

**Environment variable overrides (highest priority):**

| Env var | Overrides |
|---------|-----------|
| `ATOMIC_AGENTS_SERVE_HOST` | Bind Host |
| `ATOMIC_AGENTS_SERVE_PORT` | Bind Port |
| `ATOMIC_AGENTS_SERVE_IDENTITY_HEADER` | Identity Header |
| `ATOMIC_AGENTS_SERVE_IDEMPOTENCY_HEADER` | Idempotency Header |

Resolution order: env var > `serve.md` section > default.

**Parser shape:** `_parse_serve_md(text: str) -> ServeConfig` follows the
regex section-match pattern from `_model.py:65-113`. Empty section body
uses the default for that field.

`serve.md` MUST be parsed eagerly at startup, before accepting requests.
A malformed `serve.md` field MUST cause the server to refuse to start rather
than degrading silently (design principle 8 — atomic + idempotent everywhere;
no half-finished state).

---

## No-auth default

The serve layer's default is **refuse-by-default + explicit opt-in**.

- **Non-loopback binding (`0.0.0.0` / `::` / any address outside 127.0.0.0/8 or ::1)**:
  The server MUST NOT start without `--allow-no-auth` (CLI flag) or
  `## Allow No Auth` present in `serve.md`. Attempting to bind to a
  non-loopback address without the flag MUST exit with a clear error message:

  ```
  Error: atomic-agents serve refuses to bind to <host> without auth.
  Add --allow-no-auth or '## Allow No Auth' to serve.md only after
  your perimeter (IAP, ALB, Cloudflare Access, Tailscale Serve, etc.)
  is in place. See docs/deployment/serve.md §"No-auth default".
  ```

- **Loopback-only binding (`127.0.0.0/8` range, `::1`, or `localhost`)**:
  The server starts with a startup warning on stderr:

  ```
  Warning: atomic-agents serve is running with no auth on loopback.
  This is acceptable for local development only. Configure your
  perimeter before exposing this server to a network.
  ```

This is the framework's highest-blast-radius default. The safe path stays
zero-friction (localhost just works); the dangerous path requires deliberate,
auditable consent.

---

## Routes

### `POST /agents/<name>/call`

Invoke `agent.call()` for the named agent.

**Path validation:** `<name>` MUST be passed through `safe_resolve_under(name, agents_root)`.
A traversal attempt MUST return HTTP 400.

**Request body (JSON):**

```json
{
  "work_item": "string (required)",
  "model_override": "string (optional)",
  "max_tokens": 4096,
  "temperature": 0.6
}
```

`work_item` MUST be a non-empty string after whitespace stripping; an absent,
non-string, empty, or whitespace-only `work_item` returns HTTP 422 — a
semantically-empty prompt MUST NOT reach the LLM (CLAUDE.md principle 4,
cost is first-class). `temperature` MUST be in `[0.0, 1.0]`; out-of-range
numeric values (e.g. `1.5`) return HTTP 422. The cap matches Anthropic's
maximum — accepting OpenAI's wider `[0.0, 2.0]` range would pass validation
but produce a 500 on the default Anthropic backend, defeating pre-dispatch
validation. Boolean values (`true`/`false`) also return 422 — they are a
distinct JSON type, not numbers. `max_tokens` MUST be a positive integer;
boolean and non-numeric values return 422 on the same principle.
`model_override` MUST be a non-empty string when present; a non-string value
(number, boolean, object), an empty string (`""`), or a whitespace-only
string (`"   "`) returns HTTP 422 on the same fail-loud-on-bad-input
principle — an empty or whitespace value would either silently fall back to
the default model or produce a 500 on the backend, both of which defeat
pre-dispatch validation (CLAUDE.md principle 4).

**Forbidden fields:** `critical` and `parent_remaining_headroom_usd` MUST NOT
appear in the accepted request body schema. The serve layer MUST hardcode
`critical=False` in every `agent.call()` dispatch, regardless of what the
request body contains. A conformance test MUST assert that a request body
containing `{"work_item": "...", "critical": true}` either returns HTTP 422
or is silently stripped and the call proceeds with `critical=False`. Callers
cannot bypass the cost guardrail from the network layer. (CLAUDE.md principle 4.)

**Identity extraction:** Before calling `agent.call()`, the serve handler
reads the configured identity header from the request. The raw header value
(or `None` if absent) is passed as `caller_identity=` to `agent.call()`.
The framework MUST NOT verify or decode the header — the perimeter has already
authenticated the caller. The header value is written into the JSONL run record
under the key `http_caller` (in `extra`). MUST 7.

**Agent construction:** A fresh `AtomicAgent` instance MUST be constructed
per HTTP request OR `agent.run_id` MUST be reset at the start of each
`call()` invocation. The current implementation conditionally resets
`self.run_id` at the very start of `call()`, **before** lock acquisition, so
even the `lock_busy` audit record carries a unique `run_id` for the refused
invocation. The reset is skipped only when the constructor received an explicit
`run_id` (e.g. `OutcomeRunner` pins a correlation id across loop iterations) —
those callers satisfy uniqueness via per-call construction, not via the reset.
MUST 8.

**Response body (JSON — HTTP 200):**

```json
{
  "run_id": "run-20260607-120000-000000-a1b2c3d4",
  "status": "ok",
  "output": "Agent response text here",
  "model": "claude-opus-4-5",
  "cost_usd": 0.0012,
  "input_tokens": 1234,
  "output_tokens": 456
}
```

Excluded from the HTTP response: `helper_provenance`, `tool_calls` internals,
`captures` list, `delegations`. These are internal implementation details; the
JSONL audit log is the authoritative record. The caller receives what it needs
to display the result and attribute the cost; it does not receive the agent's
internal bookkeeping.

**Skipped response (HTTP 402):** When `agent.call()` returns
`Response(skipped=True)`, the serve layer returns HTTP 402 (Payment Required)
with `{"status": "skipped", "reason": "<skip_reason>", "run_id": "<run_id>"}`.
The `run_id` lets the caller correlate the refused HTTP response to its JSONL
audit record — the same MUST 8 reset-before-cost-check guarantee ensures the
audit record and this response body carry the same id. This signals the caller
that the cost cap was hit, not that the agent failed.

**Lock busy (HTTP 503):** `LockBusy` raised from `agent.call()` returns HTTP
503 with `{"status": "lock_busy", "reason": "...", "run_id": "<run_id>"}`.
The `run_id` lets the caller correlate the 503 response to its JSONL audit
record. `agent.run_id` is reset before lock acquisition (MUST 8), so even the
`lock_busy` record and this response body carry the same unique id.

**Deduped (HTTP 200):** When the request carries an `## Idempotency Header`
value matching a prior COMPLETED run, `agent.call()` short-circuits before the
LLM runs and returns `Response.deduped_response()`. The serve layer returns
HTTP 200 with `{"status": "deduped", "served_from_cache": true, "run_id":
"<this run_id>", "replayed_run_id": "<original run_id>", "result_ref":
"<fetch handle>"}`. `served_from_cache` is a derived JSON field signalling the
output was served from the prior run, not freshly computed. The cached output is
NOT inlined — `result_ref` is the handle the caller fetches the stored result
through, and `cost_usd` is absent from the audit record (spec/22 addendum;
spec/45 W2/W7). `replayed_run_id` joins this deduped record to the original
completed run whose result is served.

**In-flight (HTTP 409):** When a concurrent twin already holds the idempotency
lease, `agent.call()` raises `DedupInFlight` and the serve layer returns HTTP
409 (Conflict) with `{"status": "in_flight", "prior_run_id": "<owner run_id>",
"run_id": "<this run_id>"}`. This is a refusal — NOT a 500 — telling the caller
the same key is being processed right now; retry after the owner completes.
`cost_usd` is absent from the audit record (spec/22 addendum; spec/45 W3). The
`run_id` correlates this 409 to its `status='in_flight'` JSONL audit record.

**Agent not found (HTTP 404):** `AtomicAgentsError` for a missing agent folder
returns HTTP 404.

**All other errors (HTTP 500):** Unexpected exceptions and non-404
`AtomicAgentsError` subtypes return HTTP 500 with a **generic** body:
```json
{"status": "error", "error": "Internal error processing agent <name>"}
```
The full exception type, message, and traceback MUST be logged server-side
only — MUST NOT be echoed to the caller. Exception messages may embed
absolute on-disk vault paths (e.g. from `AgentProfileNotFound`,
`profile/filesystem.py`, `registry/filesystem.py`); exposing them would
allow callers to infer vault layout from the HTTP response.

**Confidentiality (MUST):** A conforming `POST /agents/<name>/call`
implementation MUST return a masked body for all 500-class errors. A
conformance test MUST assert that an `AtomicAgentsError` (non-404) and an
unexpected `Exception` both return `{"status": "error", "error":
"Internal error processing agent <name>"}` and that the raw exception type
and message do NOT appear in the response body.

**Concurrency contract:** `AtomicAgent.call()` is NOT safe for concurrent
invocations on the same instance. The per-agent filesystem lock (acquired
at `call()` entry) serializes concurrent HTTP requests targeting the same
agent. The serve handler dispatches each call in a thread-pool executor
(`loop.run_in_executor`) so Starlette's async event loop is not blocked.
See TENSIONS.md T2 for the async-first rebuild decision and triggers. MUST 9.

**Thread-pool saturation risk:** Same-agent requests beyond the first block a
thread-pool worker for up to the 30 s lock timeout (`trigger='http'` path).
The default pool is bounded at approximately `min(32, cpu_count + 4)` threads.
Enough simultaneous same-agent requests can saturate the pool entirely — every
worker blocked waiting for the lock, none available for other agents or other
work. This is the exact saturation mode documented in TENSIONS.md T2 Trigger B
(P95 queue wait > 500 ms / >50 concurrent). Operators serving agents under
concurrent load SHOULD use single-agent mode with per-replica locking at the
infrastructure layer (one Cloud Run instance per agent, autoscaled) so the
platform handles saturation via instance spin-up rather than thread queuing.
Explicit thread-pool sizing via `ATOMIC_AGENTS_SERVE_WORKERS` is a deferred
knob (TENSIONS.md T2 Trigger B); the env var is NOT read today — file an issue
if a real need surfaces. CLAUDE.md: "don't add abstractions for hypothetical
future needs."

**`asyncio.run()` inside the thread-pool thread:** MCP tool calls inside
`agent.call()` use `asyncio.run()` per invocation (`mcp.py:351+`). This is
legal when `call()` runs in a thread-pool executor thread (which has no
running event loop) but would crash in Starlette's async request handler
directly. The thread-pool dispatch is the load-bearing architectural choice
that makes MCP tools work from HTTP context. TENSIONS.md T2 §"Decision
recorded".

---

### `GET /agents/<name>/healthz`

Cheap liveness/readiness check. MUST execute three local filesystem checks
only — no backend probes, no provider key validation, no MCP subprocess
spawns:

1. `agents_root` directory is readable.
2. `<agents_root>/<name>/` folder exists.
3. `model.md` is present and `parse_model_md()` returns without raising (the
   same parser the runtime uses). Note: `parse_model_md()` tolerates malformed
   embedded YAML by design — a malformed `cost_guardrails:` block does NOT
   degrade healthz, matching the runtime's fallback-to-defaults behavior.
   Only a completely unreadable file (IOError) or a hard parsing failure
   raises and triggers a 503.

**Response (HTTP 200):**
```json
{"status": "ok", "agent": "<name>"}
```

**Response (HTTP 503):**
```json
{"status": "degraded", "reason": "<which check failed>"}
```

This endpoint is designed for Cloud Run liveness probes: O(1), no network I/O,
safe to call every 10 seconds per container. The full `doctor` surface is
intentionally NOT called here. MUST 4.

---

### `GET /agents/<name>/doctor`

Run the full `doctor.run_doctor()` for the named agent and return results
as JSON. This is the equivalent of `atomic-agents doctor --agent <name> --json`.

This endpoint is explicitly off the hot path — it is NOT suitable as a
Cloud Run liveness probe because `doctor` may spawn MCP subprocess handshakes
and probe remote backends (check_mcp, check_provider_keys). Use
`/agents/<name>/healthz` for liveness probes.

**Security note:** `/doctor` has two distinct exposure axes. First, it may
spawn MCP subprocesses on every call — unauthenticated access could drive
subprocess spawn load. Second, and more significant: the `/doctor` response
body discloses absolute filesystem paths (`agents_root`, per-check write paths)
and provider-key-presence booleans for every configured LLM provider. An
attacker who reaches this endpoint learns which AI providers are keyed and where
agent data lives on disk. The endpoint does NOT make LLM API calls, so there is
no LLM spend risk. Operators MUST treat `/doctor` as a diagnostic tool behind
the same perimeter as `/call` (IAP, Tailscale Serve, etc.) — path and
key-presence disclosure is the higher-stakes confidentiality risk, not
subprocess load.

**Response (HTTP 200):** The `run_doctor()` JSON output verbatim.

---

### `GET /agents`

List available agent names in the vault root. Returns only names (not
full paths, config content, or model fields).

**Response (HTTP 200):**
```json
{"agents": ["myagent", "researcher", "writer"]}
```

Every dynamically-discovered name (all-agents mode) is validated through
`safe_resolve_under()` before being included in the response. In single-agent
mode the single agent name originates from a trusted CLI argument and is exempt
from this validation. Path traversal in a subsequent route using the returned
name MUST still call `safe_resolve_under()` independently — the list is
informational, not a grant.

---

## Audit record shape

Every HTTP-invoked `agent.call()` writes a JSONL run record with:

```json
{
  "ts": "2026-06-07T12:00:00.000+00:00",
  "run_id": "run-20260607-120000-000000-a1b2c3d4",
  "trigger": "http",
  "primitive": "agent_call",
  "status": "ok",
  "model": "claude-opus-4-5",
  "input_tokens": 1234,
  "output_tokens": 456,
  "cost_usd": 0.0012,
  "agent_name": "myagent",
  "http_caller": "<identity header value or absent if None>",
  "summary": "first 80 chars of work_item..."
}
```

`trigger="http"` maps to `primitive="agent_call"` via `_PRIMITIVE_BY_TRIGGER`
in `agent.py` — HTTP-served calls are agent invocations over a different
transport surface, not a different class of compute primitive. spec/22.

`http_caller` carries the raw value of the configured identity header extracted
at the HTTP boundary. The field lands in `RunRecord.extra` (unknown keys route
there via `from_dict`). Log queries filter on `extra.http_caller` to identify
calls by a given principal without cross-referencing perimeter logs.

**Identity is never re-verified:** The serve layer reads the header value and
passes it through. It MUST NOT attempt JWT verification, signature validation,
or claims parsing. The perimeter (IAP / ALB / Cloudflare Access / Tailscale
Serve) has already authenticated the caller; re-verifying would require
provider-specific crypto dependencies and would be stale by the time the
framework sees the request. This is a security INVARIANT — callers that
couple to it can rely on "the framework never re-verifies the identity header."
MUST 6.

**`http_caller` attribution for child records (delegate/helper/tool calls):**
`http_caller` is recorded on the parent run record only. Child JSONL lines
(delegate calls, helper calls, tool calls spawned during an HTTP-triggered
`agent.call()`) carry `parent_run_id` linking back to the parent record — the
parent record is the authoritative attribution source. Answering "which HTTP
principal caused child call X?" is a parent-record join via `parent_run_id`.
This matches the existing audit rollup model (CLAUDE.md principle 5) and avoids
widening the child-record call signature. Log tooling that needs per-call HTTP
attribution should join on `parent_run_id` rather than expecting `http_caller`
on every child record.

---

## Cost gate on HTTP path

The HTTP surface MUST enforce the same cost guardrails as all other runtime
surfaces (`_check_cost_guardrails` in `agent.py`). The serve layer MUST NOT
expose a mechanism to bypass the cost cap.

Specifically:

- `critical=True` is structurally unavailable via HTTP. The serve handler
  hard-codes `critical=False` in every `agent.call()` call. MUST 5.
- `parent_remaining_headroom_usd` is not in the HTTP request schema.
- `model_override`, `max_tokens`, and `temperature` are the only per-call
  controls exposed. Operators may restrict `model_override` further via
  Policy (`serve.md` or policy.md).

---

## CLI — `atomic-agents serve`

```
atomic-agents serve <agent> [options]
atomic-agents serve --all [options]
```

**Arguments (exactly one required):**
- `<agent>` — agent name to serve (single-agent mode). Only routes for this
  agent are reachable; all other agent names return HTTP 404.
- `--all` — serve all agents found in agents-root. Mutually exclusive with
  `<agent>`; providing both is an error. Omitting both is also an error.

**Options (permanent; ready to ship):**
- `--host HOST` — bind address (default: 127.0.0.1; override from `ATOMIC_AGENTS_SERVE_HOST`).
- `--port PORT` — bind port (default: 8000; override from `ATOMIC_AGENTS_SERVE_PORT`).
- `--allow-no-auth` — explicitly allow serving without a perimeter auth layer.
- `--agents-root PATH` — override ATOMIC_AGENTS_ROOT.

**Flag shape rationale:** `--host` and `--port` separately (not `--bind`) —
easier to override one without the other in Cloud Run CMD arrays; avoids the
`--bind` conflation problem. The `--bind` vs `--host/--port` decision is
resolved here: this spec commits to `--host` + `--port`. MUST 3.

---

## `serve.md` parser

`_parse_serve_md(text: str) -> ServeConfig` follows the same `## Section`
header convention as `model.md` / `mcp.md`, using a generic section-iteration
regex (`^##\s+(.+)$`) that extracts all sections rather than the named-section
targeted patterns in `_model.py`. The shared convention is the `##`-header
config aesthetic (spec design principle 7); the regex shapes differ.

```python
@dataclass
class ServeConfig:
    identity_header: str = "X-Goog-IAP-JWT-Assertion"
    host: str = "127.0.0.1"
    port: int = 8000
    allow_no_auth: bool = False
```

Section bodies are stripped of whitespace. `## Allow No Auth` presence sets
`allow_no_auth=True` regardless of body content (the section existing is the
signal). Env vars override after parsing.

---

## Implementer Contract (MUSTs)

The following are normative requirements. Any conforming implementation MUST
satisfy all of them. A test suite MAY use these as the basis for a conformance
test set.

**MUST 1 — Serve extra is opt-in:**
`starlette` and `uvicorn` MUST NOT appear in the base `dependencies` list in
`pyproject.toml`. They MUST appear only under `[project.optional-dependencies]`
as the `serve` extra. Importing `atomic_agents` without the extra MUST NOT
import Starlette.

**MUST 2 — serve.md parsed eagerly:**
`serve.md` MUST be parsed at server startup before the first request is
accepted. A malformed field (e.g. a non-integer `## Bind Port`) MUST cause
the server to exit with an error message rather than silently falling back
to a default value (principle 8 — no half-finished state).

**MUST 3 — Flag surface is --host / --port:**
The CLI subcommand uses `--host` and `--port` (separate flags). `--bind` MUST
NOT be added as a synonym. The env var names are `ATOMIC_AGENTS_SERVE_HOST`
and `ATOMIC_AGENTS_SERVE_PORT`.

**MUST 4 — /healthz is cheap:**
`GET /agents/<name>/healthz` MUST execute only filesystem checks (agent folder
exists, model.md present and parseable by `parse_model_md()`). It MUST NOT call
`doctor.run_doctor()`, `check_provider_keys()`, or any MCP subprocess. Violating
this MUST is a liveness-probe correctness issue on Cloud Run. An absent model.md
MUST return 503 degraded (not 200 ok) — agents relying on default model config
without a model.md will report degraded until a model.md is added. Note:
`parse_model_md()` tolerates malformed embedded YAML fields (e.g. a bad
`cost_guardrails:` block) by falling back to defaults — a malformed block does
NOT trigger 503; only a hard parse failure or IOError does. This matches the
runtime's fault-tolerance and prevents healthz from being stricter than the call
path.

**MUST 5 — critical is hard-refused on HTTP:**
The HTTP request body schema for `POST /agents/<name>/call` MUST NOT accept
`critical` as a field. The serve handler MUST call `agent.call()` with
`critical=False` hard-coded, never from request data. A conformance test MUST
assert this.

**MUST 6 — Identity is never re-verified:**
The serve layer MUST read the configured identity header value and pass it
through to `agent.call(caller_identity=...)`. It MUST NOT attempt JWT
verification, signature parsing, or claims decoding. This is a security
invariant: the perimeter's authentication decision is final.

**MUST 7 — Identity in audit trail:**
When `caller_identity` is not None, `agent.call()` MUST write the value into
the JSONL run record under the key `http_caller`. The field MUST be present in
the record for HTTP-triggered calls where the identity header was present in
the request. A conformance test MUST assert the field appears in the JSONL
record.

**MUST 8 — Unique run_id per call:**
Each `agent.call()` invocation MUST produce a unique `run_id` in the JSONL
audit record. For HTTP-served agents, the implementation conditionally resets
`self.run_id = self._generate_run_id()` at the very start of `call()`,
**before** lock acquisition, so every audit record (including `lock_busy` and
cost-skip records) carries a run_id unique to that invocation. The reset is
skipped only when the constructor received an explicit `run_id` (callers such
as `OutcomeRunner` pin a correlation id across loop iterations; those callers
satisfy uniqueness via per-call construction, not via the reset). Conformance:
two sequential HTTP requests to the same agent MUST produce two distinct
`run_id` values in the format `run-YYYYMMDD-HHMMSS-ffffff-xxxxxxxx` (uuid4
hex suffix guarantees uniqueness under same-microsecond concurrent requests).

**MUST 9 — Dispatch via thread-pool executor:**
The Starlette request handler for `POST /agents/<name>/call` MUST dispatch
`agent.call()` via `loop.run_in_executor(<executor>, ...)`. The current
implementation uses a shared module-level `ThreadPoolExecutor`
(`run_in_executor(_get_executor(), ...)`) — the default `None` executor is
also acceptable. Direct `await agent.call()` MUST NOT be attempted — `call()`
is synchronous and contains `asyncio.run()` calls that would fail inside an
active event loop. Note: the `/doctor` route dispatches via
`run_in_executor(None, ...)` (default executor); this is intentional and
consistent with the same MUST.

**MUST 10 — Path traversal guard:**
Every route that accepts `<name>` from the URL path MUST call
`safe_resolve_under(name, agents_root)` and return HTTP 400 on
`PathTraversalError`. This applies to all four routes. Implementation note:
Starlette normalises URL-encoded dot-segments before routing, so literal `..`
traversal attempts may be rejected by the router before the handler runs —
either HTTP 400 or HTTP 404 is acceptable for traversal-shaped names; both
signal a non-retryable client error. In single-agent mode the single-agent
allow check runs before `safe_resolve_under()`, so a traversal-shaped name that
doesn't match the single agent returns HTTP 404 rather than 400; this is
consistent with the "router may normalise first" property above.

**MUST 11 — At-least-once redelivery is expected; dedup is opt-in:**
`POST /agents/<name>/call` may receive at-least-once redelivery from any
event-delivery source. The caller — or an interposed transform between the
delivery source and this route — MUST supply the Idempotency-Key header (the
header name configured via `## Idempotency Header` in `serve.md`) for dedup to
apply; an absent header means no dedup — the run executes unconditionally
(unless body-hash auto-derivation is enabled per spec/45). The framework
deduplicates per spec/45 when the header is present. This spec does not
prescribe how any particular delivery source maps its native delivery
identifier to an Idempotency-Key; provider-specific reference patterns
(e.g. managed message queues and task queues) live in `extras/`, outside
this normative spec.

Note for implementors: MUST 11 is a normative documentation of existing
behaviour shipped in spec/45 PR2. No new code is required. Future arcs adding
MUSTs to spec/37 (e.g. the /ingest route, issue #524) MUST pick up from
MUST 12 onward.

---

## Cross-references

- spec/22 §"Canonical primitive taxonomy" — `trigger='http'` maps to
  `primitive='agent_call'`.
- spec/27 §"What this spec does NOT define" ("Continuous monitoring" bullet) —
  operators running `atomic-agents serve` SHOULD use `GET /agents/<name>/healthz`
  as the liveness probe instead of `doctor --json`. The doctor endpoint is
  available at `/agents/<name>/doctor` for one-off diagnostics.
- TENSIONS.md T2 — the Hybrid Option C decision (thread-pool adapter + deferred
  async-first rebuild with named triggers).
- CLAUDE.md design principle 4 — cost is first-class, not bolted on (MUST 5:
  `critical=False` hard-coded; cost guardrails enforced on every HTTP path).
- CLAUDE.md design principle 5 — audit trail is structural (MUST 7: http_caller
  in every audit record including lock_busy and cost-skip paths).
- CLAUDE.md design principle 8 — atomic + idempotent everywhere (MUST 2:
  malformed serve.md exits rather than degrading silently).
- CLAUDE.md design principle 6 — progressive disclosure by default (Starlette
  is opt-in via the `serve` extra).

---
spec: 39
title: OpenTelemetry trace export
status: DRAFT
created: 2026-06-08
issue: 341
---

# spec/39 — OpenTelemetry trace export

**Status:** DRAFT — ships with issue #341 PR 1; locked after conformance tests
pass across the full arc.

---

## Purpose

Emit OpenTelemetry (OTel) spans for `agent.call()` so operators running agents
inside a tracing-aware host (Cloud Run, GKE, Fly.io, any OTLP collector) get a
distributed-trace view of agent execution — outcome, cost, token usage, tool
iterations — alongside the JSONL audit trail the framework already writes.

Tracing is a **second projection of the same audit data**, not a replacement.
The JSONL run record (CLAUDE.md Principle 5) stays authoritative; spans are a
translation for operators who already run an OTel pipeline. This satisfies the
throughline: a home user with no collector pays zero cost and sees no change; an
org with a collector gets first-class traces from the same code.

This is **not** a backend Protocol. There is no `TracingBackend`, no
capability advertisement, no swappable reference impl. Tracing is an
instrumentation seam: a single module (`atomic_agents/tracing.py`) that wraps
the OTel API, plus span-open/close calls inside `agent.call()`. The seam
conforms to the existing locked specs (spec/13 audit shape, spec/37 serve) and
adds no new storage surface.

---

## Activation model — off by default

Tracing activates only when **both** of these hold:

1. `ATOMIC_AGENTS_TRACING_ENABLED=true` (the primary on/off toggle), AND
2. `OTEL_EXPORTER_OTLP_ENDPOINT` is set (the OTLP/HTTP collector endpoint).

If either is absent, the framework installs **no** TracerProvider and leaves the
OTel API's default `ProxyTracerProvider` (which is non-recording) in place. No
spans are recorded, no background threads start, no warnings are emitted.

`opentelemetry-api` ships in the **core** dependency set because it provides the
genuine no-op `ProxyTracerProvider` — importing `tracing.py` therefore costs
nothing when tracing is off. The SDK and OTLP/HTTP exporter
(`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`) ship behind the
`[otel]` optional extra and are imported **only** inside the setup function on
the fully-enabled path. **HTTP exporter only — no gRPC** (avoids the grpcio
dependency weight; the OTLP/HTTP exporter defaults to port 4318).

Optional environment variables (read by the OTel SDK / this module):

| Env var | Default | Meaning |
|---------|---------|---------|
| `ATOMIC_AGENTS_TRACING_ENABLED` | unset (off) | `true` to opt in |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Base OTLP/HTTP collector URL (e.g. `http://collector:4318`); required to activate. The framework uses this var **only as a presence gate** to decide whether to activate — it does **NOT** pass it to the exporter. It constructs `OTLPSpanExporter()` with no `endpoint=` argument, so the OTLP/HTTP exporter reads the OTel-standard env vars itself: it appends the `/v1/traces` signal path to this base URL, and honors the signal-specific `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` override verbatim when that var is also set — provided this base var is set too (it is the activation gate; setting only the signal-specific var leaves tracing disabled). |
| `OTEL_SERVICE_NAME` | `atomic-agents` | `service.name` resource attribute |

---

## Scope — PR 1 of the arc

This PR ships **only the parent `atomic_agents.call` span**. The child-span
vocabulary (`gen_ai.client.chat`, `atomic_agents.tool_call`,
`atomic_agents.helper_call`, `atomic_agents.delegate`) and all `gen_ai.*`
attribute constants are **declared** in `tracing.py` (locked names, see MUST 4)
but **not yet emitted** — wiring them at the LLM / tool / helper / delegate call
sites is deferred to a later PR of this arc. No child spans are produced in PR 1;
the constants are pre-declared so the locked names are ratified by the spec
before any code depends on them.

---

## Span model

PR 1 emits exactly one span per `agent.call()`:

- **Name:** `atomic_agents.call`
- **Lifecycle:** opened immediately after `run_id` reset (so the span carries the
  correct fresh `run_id`), attached as the active context, and ended on every
  exit path (MUST 3).

### Attributes (locked names — MUST 4)

| Attribute | Type | When set |
|-----------|------|----------|
| `atomic_agents.agent_name` | str | at open |
| `atomic_agents.trigger` | str | at open |
| `atomic_agents.run_id` | str | at open |
| `atomic_agents.model` | str | at open, refreshed at close with the actual model used |
| `atomic_agents.outcome` | str | at close (MUST 8) |
| `atomic_agents.cost_usd` | float | at close (MUST 1) |
| `atomic_agents.input_tokens` | int | at close |
| `atomic_agents.output_tokens` | int | at close |
| `atomic_agents.tool_iterations` | int | at close |

### Outcome values (locked — MUST 8)

`ok`, `skipped`, `deferred`, `error`, `lock_busy`.

---

## Implementer Contract

These are the normative MUSTs the implementation cites by number. They describe
what is true today (CLAUDE.md Principle 10 — spec describes the implementation,
not an aspiration).

**MUST 1 — cost recorded on all paths.** The `atomic_agents.cost_usd` attribute
MUST be set on every exit path. Span cost/token/iteration accumulators MUST be
declared before any code that can raise, so the teardown finalizer can always
read them regardless of where an exception fires. On no-LLM-call paths
(lock_busy, pre-loop cost-skip) the cost MUST be `0.0`; on the mid-loop cost-cap
path it MUST be the true partial spend, not `0.0`.

**MUST 2 — deferred, guarded SDK import.** The `opentelemetry-sdk` and OTLP
exporter imports MUST be deferred to the tracing-setup function (not module
scope) and wrapped in `try/except ImportError`. A base install without the
`[otel]` extra MUST import `tracing.py` and run `agent.call()` without error;
the missing-SDK path MUST warn once and fall back to the non-recording default
provider.

**MUST 3 — span ended on all exit paths.** The `atomic_agents.call` span MUST be
ended exactly once, and its context token detached exactly once, on every exit
path: normal return, exception, and the lock_busy / pre-loop cost-skip /
mid-loop cost-cap early returns. Teardown lives in TWO finalize sites, both
calling the same idempotent finalizer: (1) the body `try/finally`, for every
path that reaches the body; and (2) a dedicated finalize for the two acquire-time
pre-body paths — `lock_busy` and any other acquire exception (e.g. a
`PermissionError` on a read-only filesystem) — that raise before the body try is
entered and so can never reach the body finally. The finalizer MUST be idempotent
so early-exit paths that set their own outcome do not double-end. The non-throwing
guarantee is uniform across the WHOLE seam, not just the finalizer's attribute
body: every span mutation — the OPEN path (`start_span` + `context.attach` + the
open-time `set_attribute` calls), every `set_status` / `set_attribute` /
`record_exception` mutation site, AND the inner teardown (`context.detach` +
`span.end`) — MUST be wrapped so a tracing failure can never propagate. On a
host-provider recording span any of these can raise (a custom SpanProcessor's
`on_start` / `on_end`, `SpanLimits`, a misbehaving exception `__str__`, or a
stale/foreign context token). Propagation at the open path would crash `call()`
before any `agent_call` audit line is written (Principle 5); propagation at a
mutation or teardown site would abort the body finally before lock release
(leaking the agent lock, Principle 8) or replace an in-flight refusal exception
with a tracing error. A tracing failure can never mask the real return value or
in-flight exception, and can never prevent the agent run from executing. The open
path, on failure, falls back to a non-recording sentinel span so the finalizer
no-ops safely. (`Exception` is caught, not `BaseException`, so
`KeyboardInterrupt` / `SystemExit` still propagate.)

**MUST 4 — locked span + attribute names.** The span names and attribute names
in `tracing.py` are locked per CLAUDE.md Principle 14 — renaming any of them is a
v2.0 event. The `gen_ai.*` keys follow the OTel GenAI semantic-conventions
incubating draft; the two `atomic_agents.*` extensions (`cost_usd`, integer
`llm.cache_hit_tokens`) are deliberate non-semconv additions for data the
semconv does not cover and MUST keep the `atomic_agents.*` namespace.

**MUST 5 — version floor.** `opentelemetry-api>=1.20,<2.0` is required in core;
`opentelemetry-sdk>=1.20,<2.0` and
`opentelemetry-exporter-otlp-proto-http>=1.20,<2.0` are required when the
`[otel]` extra is installed.

**MUST 6 — never clobber a host provider.** On the disabled, no-endpoint, and
SDK-missing branches the framework MUST NOT call `set_tracer_provider()` — the
default `ProxyTracerProvider` is already non-recording, and calling
`set_tracer_provider()` when a host process already installed one is refused by
OTel with a per-process WARNING ("Overriding of current TracerProvider is not
allowed"), breaking the silent-when-off promise. On the fully-enabled path the
framework MUST install its provider **only** when the current global provider is
still the bare `ProxyTracerProvider`; if a host process already installed a real
provider, the framework MUST defer to it (spans still export through the host's
pipeline).

**MUST 7 — provider inference from model.** Until `provider_id` is carried on
`_RawLLMResponse`, `gen_ai.system` MUST be inferred from the model-name prefix
(`claude*` → anthropic, `gpt-*`/`o1*`/`o3*` → openai, `moonshot/*` → moonshot,
else `unknown`). The `gpt-*` prefix (hyphenated) matches the framework's existing
`model_namespace` convention in `openai_compat.py` and every shipped OpenAI model
id. The preferred `provider_id`-on-response path is tracked as
future work for a later PR of this arc.

**MUST 8 — outcome precedence.** The `atomic_agents.outcome` attribute MUST be
derived with precedence `error > skipped > deferred > ok`. An in-flight
exception MUST set `outcome=error` and span status `ERROR`. Otherwise the
outcome MUST be derived from the returned `Response` (`skipped=True` → skipped,
`deferred=True` → deferred, else ok), unless an early-exit path supplied an
explicit outcome.

**MUST 9 — cost-skip outcome + status.** The pre-loop cost-skip and mid-loop
cost-cap paths MUST set `outcome=skipped` and span status `ERROR` (a refusal,
not a crash), and MUST record the correct cost (`0.0` pre-loop; the partial
spend mid-loop). The lock_busy path MUST set `outcome=lock_busy` and span status
`ERROR`.

---

## What this spec does NOT cover (deferred, tracked)

- **Child spans** (`gen_ai.client.chat`, tool / helper / delegate) — later PR of
  this arc; constants pre-declared here under MUST 4.
- **`provider_id` on `_RawLLMResponse`** — replaces the MUST 7 prefix heuristic;
  later PR.
- **Metrics / logs signals** — only traces are in scope.
- **gRPC OTLP exporter** — HTTP only; gRPC explicitly excluded.

---

## Conformance tests

`tests/test_otel_tracing.py` MUST assert at minimum:

1. Base import works without the `[otel]` extra (module imports; no error).
2. Disabled path records zero spans and emits no "Overriding ..." warning.
3. Enabled-but-no-endpoint path records zero spans and warns once.
4. A successful `call()` exports exactly one `atomic_agents.call` span with
   `outcome=ok`, status UNSET (OTel convention reserves OK for an explicit
   override; the framework only ever calls `set_status(ERROR)`, on refusals /
   errors), and cost/token/iteration attributes set.
5. An exception inside `call()` exports exactly one span with `outcome=error`
   and status ERROR, and re-raises the original exception. The error span MUST
   carry the true partial spend (`cost_usd` / `input_tokens` > 0) when the raise
   fires AFTER real LLM spend — NOT `0.0` (MUST 1).
6. lock_busy, pre-loop cost-skip, and mid-loop cost-cap each export exactly one
   span with the correct outcome and ERROR status. The pre-loop cost-skip span
   MUST record `cost_usd=0.0`; the mid-loop cost-cap span MUST record the true
   partial spend (`cost_usd` > 0, `tool_iterations` == completed-iteration count)
   to pin the MUST 1 partial-spend sync.
7. After any `call()`, the active OTel context returns to baseline (the context
   token was detached — no leak across calls).
8. The fully-enabled `_ensure_tracer_provider()` path installs an SDK
   `TracerProvider` only when the global is still a bare `ProxyTracerProvider`
   (MUST 6 install), defers (no `set_tracer_provider`) when a host provider is
   already installed (MUST 6 defer), and warns-once + does not install when the
   SDK is missing (MUST 2).
9. When a host-provider recording span's `set_attribute` / `record_exception` /
   `set_status` / `end` raises inside the finalizer, OR when `context.detach`
   raises in the inner teardown, the original `call()` error/return still
   propagates unchanged AND the agent lock is released (a subsequent call
   succeeds, not `LockBusy`) — proving the finalizer is non-throwing (MUST 3).
10. When the span-OPEN path (`start_span` / `context.attach` / the open-time
    `set_attribute` calls) raises, `call()` still runs the agent, writes the
    `agent_call` audit JSONL line, leaves no OTel context token attached, and
    releases the lock — proving the open seam is as non-throwing as the
    finalizer (MUST 3 applied symmetrically).
11. The OTLP/HTTP exporter is constructed with NO `endpoint` argument so the
    exporter reads `OTEL_EXPORTER_OTLP_ENDPOINT` itself (appending `/v1/traces`)
    and honors `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` verbatim — the framework
    uses the base var only as a presence gate, never as a pass-through.

"""OTel instrumentation seam for atomic-agents-stack.

This module is the single entry point for all tracing state. It is designed to
be imported unconditionally at the top of agent.py — opentelemetry-api (always
present in core) provides a genuine no-op TracerProvider when no SDK is
registered, so importing this file costs zero when tracing is off.

The SDK (opentelemetry-sdk) and OTLP exporter
(opentelemetry-exporter-otlp-proto-http) are OPTIONAL — they are only imported
here inside factory functions when:

1. ATOMIC_AGENTS_TRACING_ENABLED=true (the primary on/off toggle), AND
2. OTEL_EXPORTER_OTLP_ENDPOINT is set (the exporter endpoint).

If neither condition holds the module is still importable and returns
no-op span objects with zero runtime cost.

Spec reference: docs/spec/39-otel-export.md
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# opentelemetry-api is a core dependency — safe to import at module scope.
# The API ships a genuine ProxyTracerProvider (non-recording) when no SDK is
# registered, so these imports are free even when the [otel] extra is not
# installed. opentelemetry.context is part of opentelemetry-api (always present
# in core), so it is hoisted here rather than imported per-call() in agent.py
# (spec/39 — progressive disclosure does not apply to a guaranteed core dep).
import opentelemetry.context as _otel_ctx
import opentelemetry.trace as _otel_trace

_logger = logging.getLogger(__name__)

# ── Module-level tracer provider state ────────────────────────────────────────
# _tracer_provider_configured: set to True after the first call to
# _ensure_tracer_provider(), preventing multiple initializations per process.
# Tests reset this between cases via _reset_for_testing().
_tracer_provider_configured: bool = False

# The module-level tracer instance. Initialized lazily on first use.
_tracer: _otel_trace.Tracer | None = None


def _reset_for_testing() -> None:
    """Reset module state so tests can exercise different env-var configs.

    Called by the test suite's fixtures. NOT for production use.
    """
    global _tracer_provider_configured, _tracer
    _tracer_provider_configured = False
    _tracer = None
    # Reset OTel's global provider state so a test that installed a real SDK
    # TracerProvider doesn't bleed into subsequent tests, and so the next
    # set_tracer_provider() call is honored (OTel installs the provider only
    # ONCE per process unless the set-once flag is cleared).
    #
    # We must NOT do `set_tracer_provider(ProxyTracerProvider())`: a
    # ProxyTracerProvider resolves its tracers by calling get_tracer_provider(),
    # so installing one AS the global makes get_tracer() self-referential and
    # recurses infinitely. Instead reset OTel's private module state directly to
    # the genuine default (a fresh ProxyTracerProvider held in OTel's own slot,
    # which resolves to the no-op default until a real provider is set).
    #
    # These underscored names are NOT part of OTel's API-stability promise and
    # can drift across the pinned floor (opentelemetry-api>=1.20,<2.0). If they
    # drift, a silent no-op would let span state bleed across tests with zero red
    # signal (the in_memory_exporter fixture force-assigns _tracer, so spans
    # still route and tests stay green). So we fail LOUD on AttributeError — this
    # is test-only code, and a clear failure is the right signal to re-audit the
    # pin. A dedicated test (test_reset_restores_proxy_provider) asserts the reset
    # actually returns the global to a ProxyTracerProvider so drift surfaces.
    try:
        _otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
        _otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    except AttributeError as exc:  # pragma: no cover - drift guard
        raise RuntimeError(
            "OTel private reset state moved — _reset_for_testing() can no longer "
            "clear the set-once TracerProvider guard. Audit the installed "
            "opentelemetry-api version against the >=1.20,<2.0 pin in pyproject."
        ) from exc


def _ensure_tracer_provider() -> None:
    """Configure the TracerProvider once per process on first get_tracer() call.

    Reads ATOMIC_AGENTS_TRACING_ENABLED and OTEL_EXPORTER_OTLP_ENDPOINT from
    the environment at first-call time (not at module import time). This means
    a process that starts without the env vars and later sets them (test
    monkeypatching) will pick up the change correctly.

    Behavior:
    - ATOMIC_AGENTS_TRACING_ENABLED != "true"  →  leave the default provider
      untouched (the API's ProxyTracerProvider is already non-recording)
    - ATOMIC_AGENTS_TRACING_ENABLED=true  +  OTEL_EXPORTER_OTLP_ENDPOINT set
      →  real SDK TracerProvider with BatchSpanProcessor + OTLPSpanExporter
    - ATOMIC_AGENTS_TRACING_ENABLED=true  +  OTEL_EXPORTER_OTLP_ENDPOINT unset
      →  leave the default provider untouched (spec/39 MUST 6: tracing MUST NOT
         activate if endpoint is absent, even when SDK is installed — prevents
         noisy connection-refused log spam for home users)

    Why "leave untouched" and not "install NoOpTracerProvider": OTel allows the
    global provider to be set only ONCE per process. If the host process (serve /
    Cloud Run) already configured a TracerProvider, calling set_tracer_provider()
    here is REFUSED and logs "Overriding of current TracerProvider is not allowed"
    as a WARNING on every fresh process where tracing is off — breaking the
    "silent when off" promise. The default ProxyTracerProvider is itself
    non-recording, so doing nothing is the correct, warning-free no-op.

    spec/39 MUST 5 (version floor): opentelemetry-api>=1.20 required;
    opentelemetry-sdk>=1.20 required when [otel] is installed.
    """
    global _tracer_provider_configured
    if _tracer_provider_configured:
        return

    _tracer_provider_configured = True

    enabled = os.environ.get("ATOMIC_AGENTS_TRACING_ENABLED", "").strip().lower()
    if enabled != "true":
        # Not enabled — do NOT call set_tracer_provider. The default global
        # ProxyTracerProvider is already non-recording (is_recording() == False),
        # so leaving it untouched is the correct no-op and, crucially, emits no
        # "Overriding of current TracerProvider is not allowed" warning when a
        # host process has installed its own SDK provider. spec/39 MUST 6.
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        # Enabled flag but no endpoint — spec/39 MUST 6: do NOT activate, and do
        # NOT clobber the global provider (see above). Just warn once.
        _logger.warning(
            "ATOMIC_AGENTS_TRACING_ENABLED=true but OTEL_EXPORTER_OTLP_ENDPOINT "
            "is not set. Tracing is disabled. Set the endpoint to activate."
        )
        return

    # SDK and exporter imports are deferred here (not at module scope) so
    # the base install (no [otel] extra) never hits an ImportError. The check
    # only runs when the operator has explicitly opted in via env vars.
    # spec/39 MUST 2: 'SDK and OTLP exporter imports MUST be deferred to the
    # tracing-setup function and wrapped in try/except ImportError.'
    try:
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )
    except ImportError:
        # SDK not installed — do NOT clobber the global provider (see the
        # disabled-branch rationale above). Warn once and fall back to the
        # default non-recording proxy. spec/39 MUST 2 / MUST 6.
        _logger.warning(
            "ATOMIC_AGENTS_TRACING_ENABLED=true but opentelemetry-sdk / "
            "opentelemetry-exporter-otlp-proto-http are not installed. "
            "Install via: pip install 'atomic-agents-stack[otel]'"
        )
        return

    # spec/39 MUST 6: only atomic-agents-owned processes should install the
    # provider. If a host process (serve / Cloud Run) already installed a real
    # SDK provider, defer to it rather than triggering the "Overriding ... is not
    # allowed" warning that a clobber would produce. A bare ProxyTracerProvider
    # (the default) is fine to replace.
    existing = _otel_trace.get_tracer_provider()
    if not isinstance(existing, _otel_trace.ProxyTracerProvider):
        _logger.info(
            "atomic-agents tracing enabled, but a TracerProvider is already "
            "installed by the host process; deferring to it (spans still export "
            "through the host's pipeline)."
        )
        return

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "atomic-agents"),
        }
    )
    # Construct the exporter with NO endpoint= argument. We use the env var only
    # as a PRESENCE gate (the `if not endpoint: return` above) — we do NOT pass
    # it through. The OTLP/HTTP exporter applies the OTel-standard per-signal path
    # logic when it reads the env itself: it appends `/v1/traces` to the base
    # OTEL_EXPORTER_OTLP_ENDPOINT (e.g. http://collector:4318 → .../v1/traces)
    # and honors the signal-specific OTEL_EXPORTER_OTLP_TRACES_ENDPOINT verbatim.
    # Passing endpoint=<base> would make the exporter treat it as the FINAL URL
    # and POST spans to the bare base (no /v1/traces) — most collectors 404 that,
    # and the BatchSpanProcessor swallows the failure, so spans vanish silently.
    # spec/39 line 62 documents OTEL_EXPORTER_OTLP_ENDPOINT as the base collector
    # URL; this keeps the code true to that doc. (Principles 12 + 13.)
    exporter = OTLPSpanExporter()
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _otel_trace.set_tracer_provider(provider)
    _logger.info(
        "atomic-agents tracing enabled — exporting spans to OTLP/HTTP base %s "
        "(exporter appends the /v1/traces signal path)",
        endpoint,
    )


def get_tracer() -> _otel_trace.Tracer:
    """Return the module-level tracer, initialising the provider on first call.

    The tracer is safe to call at any time. When tracing is disabled it returns
    a no-op tracer whose span context managers cost a negligible amount.
    """
    global _tracer
    if _tracer is None:
        _ensure_tracer_provider()
        _tracer = _otel_trace.get_tracer("atomic_agents", "1.0.0")
    return _tracer


def safe_span_op(op: "Callable[[], Any]", _what: str = "span operation") -> None:
    """Run a single span mutation so a tracing failure can never propagate.

    spec/39 MUST 3: a tracing failure can never mask the real return value or the
    in-flight exception. Every span mutation (set_attribute / set_status /
    record_exception / start_span side effects) goes through this helper so the
    seam is uniformly non-throwing — the contract holds for EVERY mutation site,
    not just the close-time finalizer.

    Once a host process installs a real SDK TracerProvider the span becomes a
    recording span: a custom SpanProcessor (on_start / on_end), SpanLimits, or a
    misbehaving exception ``__str__`` can make any of these calls raise. Such a
    fault must never crash ``call()`` — at the OPEN site it would abort the run
    before any audit line is written (Principle 5), at a mutation site it would
    swap the real outcome for a tracing error, and at the finalizer it would
    abort the body finally before lock release (Principle 8 — leaked agent lock).

    ``Exception`` (not ``BaseException``) is caught so KeyboardInterrupt /
    SystemExit still propagate, matching the finalizer's rationale.
    """
    try:
        op()
    except Exception:
        _logger.warning(
            "atomic_agents tracing: %s failed (swallowed; spec/39 MUST 3)",
            _what,
            exc_info=True,
        )


# ── Span-name constants (locked per Principle 14 — do NOT rename until v2.0) ──
SPAN_AGENT_CALL = "atomic_agents.call"
SPAN_LLM_CALL = "gen_ai.client.chat"  # matches OTel GenAI semconv span name
SPAN_TOOL_CALL = "atomic_agents.tool_call"
SPAN_HELPER_CALL = "atomic_agents.helper_call"
SPAN_DELEGATE = "atomic_agents.delegate"

# ── Attribute-name constants (locked per Principle 14) ────────────────────────
# agent.call span
ATTR_AGENT_NAME = "atomic_agents.agent_name"
ATTR_TRIGGER = "atomic_agents.trigger"
ATTR_OUTCOME = "atomic_agents.outcome"
ATTR_COST_USD = "atomic_agents.cost_usd"
ATTR_INPUT_TOKENS = "atomic_agents.input_tokens"
ATTR_OUTPUT_TOKENS = "atomic_agents.output_tokens"
ATTR_MODEL = "atomic_agents.model"
ATTR_TOOL_ITERATIONS = "atomic_agents.tool_iterations"
ATTR_RUN_ID = "atomic_agents.run_id"
ATTR_PARENT_RUN_ID = "atomic_agents.parent_run_id"

# gen_ai.client.chat span. Locked to these exact strings per spec/39 MUST 4,
# independent of upstream semconv evolution — renaming is a v2.0 event.
# NOTE: only the gen_ai.* keys below follow the OTel GenAI semconv (incubating
# draft). The last two constants are deliberate atomic_agents.* extensions for
# data the semconv does not cover (per-call USD cost, integer cache-hit token
# count) — they keep the ATTR_GEN_AI_ prefix only for grouping, not to claim
# semconv membership.
ATTR_GEN_AI_SYSTEM = "gen_ai.system"
ATTR_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
ATTR_GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_GEN_AI_USAGE_INPUT_CACHED_TOKENS = "gen_ai.usage.input_cached_tokens"
# atomic_agents.* extensions (NOT gen_ai.* semconv keys):
ATTR_GEN_AI_COST_USD = "atomic_agents.cost_usd"  # atomic_agents.* namespace for cost
ATTR_GEN_AI_CACHE_HIT_TOKENS = "atomic_agents.llm.cache_hit_tokens"  # int, NOT bool

# tool_call span
ATTR_TOOL_NAME = "atomic_agents.tool_name"
ATTR_TOOL_ERROR = "atomic_agents.tool_error"
ATTR_TOOL_LATENCY_MS = "atomic_agents.tool_latency_ms"
ATTR_TOOL_COST_USD = "atomic_agents.tool_cost_usd"  # CONDITIONAL: only when expected_external_cost_usd non-null

# helper_call span
ATTR_HELPER_COST_USD = "atomic_agents.cost_usd"  # same key as agent.call

# delegate span
ATTR_DELEGATE_TARGET = "atomic_agents.delegate_target"
ATTR_DELEGATE_COST_USD = "atomic_agents.cost_usd"  # same key


# ── Outcome string values (locked per Principle 14) ───────────────────────────
OUTCOME_OK = "ok"
OUTCOME_SKIPPED = "skipped"
OUTCOME_DEFERRED = "deferred"
OUTCOME_ERROR = "error"
OUTCOME_LOCK_BUSY = "lock_busy"
OUTCOME_DEDUPED = "deduped"  # spec/45 PR2 — COMPLETED replay short-circuit
OUTCOME_IN_FLIGHT = "in_flight"  # spec/45 PR2 — concurrent IN_FLIGHT key


def _derive_outcome(response: Any) -> str:
    """Derive the canonical outcome string from a Response object.

    Derivation rule (per spec/39 MUST 8, updated precedence order:
    error > deduped > skipped > deferred > ok):
      - error:   span status was set to ERROR (handled externally via span.set_status)
      - deduped: Response.deduped=True (checked before skipped — a deduped call
                 is NOT a cost-skip; misclassifying it as OUTCOME_SKIPPED would
                 produce misleading traces) (spec/45 PR2)
      - skipped: Response.skipped=True
      - deferred: Response.deferred=True and Response.skipped=False
      - ok:      otherwise
    """
    if getattr(response, "deduped", False):
        return OUTCOME_DEDUPED
    if getattr(response, "skipped", False):
        return OUTCOME_SKIPPED
    if getattr(response, "deferred", False):
        return OUTCOME_DEFERRED
    return OUTCOME_OK


def _provider_id_from_model(model: str) -> str:
    """Infer gen_ai.system from model name prefix (spec/39 MUST 7).

    The preferred path is to carry provider_id in _RawLLMResponse (see spec/39
    §'Future: provider_id in _RawLLMResponse'). Until that field lands, this
    heuristic covers the three shipped reference implementations.
    """
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
        return "openai"
    if model.startswith("moonshot/"):
        return "moonshot"
    return "unknown"


# ── Status codes ──────────────────────────────────────────────────────────────
StatusCode = _otel_trace.StatusCode

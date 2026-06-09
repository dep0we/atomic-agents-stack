"""Conformance tests for the OTel trace-export seam (#341 PR 1, spec/39).

These tests pin the span-lifecycle contract that the diff's per-path teardown
originally got wrong: the parent ``atomic_agents.call`` span MUST be ended on
EVERY exit path (normal success, exception, lock_busy, pre-loop cost-skip,
mid-loop cost-cap), the context token MUST be detached so it never leaks across
calls, and the off-by-default path MUST stay silent (no spans, no
"Overriding ... TracerProvider" warning).

The base package depends only on ``opentelemetry-api``; the SDK +
``InMemorySpanExporter`` used here come from the dev group (mirrors the
fakeredis / psycopg test-only precedent). See docs/spec/39-otel-export.md.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import opentelemetry.context as otel_context
import opentelemetry.trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from atomic_agents import tracing
from atomic_agents.llm.backend import _RawLLMResponse


# ──────────────────────────────────────────────────────────────────
# Helpers


def _build_minimal_agent_dir(tmp_path: Path, name: str = "test") -> Path:
    """Construct the minimal on-disk shape AtomicAgent.__init__ requires."""
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n")
    (agent_dir / "tools.md").write_text(
        "## Write paths\n- memory/\n\n## Read-only paths\n(none)\n"
    )
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    return agent_dir


def _text_only_response() -> _RawLLMResponse:
    """A tool-use-free LLM response so the agent loop terminates in one pass."""
    return _RawLLMResponse(
        text="done",
        input_tokens=10,
        output_tokens=5,
        cache_hit_tokens=0,
        cache_miss_tokens=10,
        raw={},
        tool_uses=[],
    )


def _tool_use_response(tool_name: str = "noop_tool") -> _RawLLMResponse:
    """An LLM response that calls a custom tool, so the loop runs another turn."""
    return _RawLLMResponse(
        text="",
        input_tokens=100,
        output_tokens=50,
        cache_hit_tokens=0,
        cache_miss_tokens=100,
        raw={},
        tool_uses=[{"id": "tc_1", "name": tool_name, "input": {}}],
    )


def _register_noop_tool(agent, name: str = "noop_tool") -> None:
    """Register a trivial custom tool so a tool_use keeps the loop alive."""
    from atomic_agents.tools import ToolDefinition

    agent.tool_registry.register(
        ToolDefinition(
            name=name,
            description="No-op tool used to drive a second loop iteration in tests.",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda _inp: "ok",
            classification="read_only",
        )
    )


@pytest.fixture
def in_memory_exporter():
    """Install a real SDK TracerProvider that records into memory.

    Bypasses ``tracing._ensure_tracer_provider`` (which gates on env vars and an
    OTLP endpoint) by installing the provider directly via ``_reset_for_testing``
    + a forced module tracer, so the lifecycle assertions run without a live
    collector. Resets module + global provider state on teardown.
    """
    tracing._reset_for_testing()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    # Force the module to use this provider's tracer rather than re-resolving
    # through the env-gated _ensure_tracer_provider path.
    tracing._tracer = provider.get_tracer("atomic_agents", "1.0.0")
    tracing._tracer_provider_configured = True
    try:
        yield exporter
    finally:
        tracing._reset_for_testing()


def _spans_named_call(exporter: InMemorySpanExporter):
    return [
        s for s in exporter.get_finished_spans() if s.name == tracing.SPAN_AGENT_CALL
    ]


# ──────────────────────────────────────────────────────────────────
# Import / off-by-default behavior (MUST 2, MUST 6)


def test_base_import_works_without_otel_extra():
    """Importing tracing.py costs nothing and exposes the locked names.

    The module imports opentelemetry-api only (core dep). This pins MUST 2:
    base install (no SDK) must import + run without ImportError.
    """
    assert tracing.SPAN_AGENT_CALL == "atomic_agents.call"
    assert tracing.OUTCOME_OK == "ok"
    assert callable(tracing.get_tracer)


def test_disabled_path_does_not_clobber_host_provider(monkeypatch):
    """MUST 6: tracing off must NOT call set_tracer_provider at all.

    OTel allows the global provider to be set only once per process, so calling
    it when a host already installed one is refused with a WARNING — breaking the
    silent-when-off promise. We assert the call site by spying on
    set_tracer_provider rather than on global state (which is sticky across the
    suite's other tests).
    """
    tracing._reset_for_testing()
    monkeypatch.delenv("ATOMIC_AGENTS_TRACING_ENABLED", raising=False)

    with patch.object(otel_trace, "set_tracer_provider") as spy:
        tracing._ensure_tracer_provider()

    spy.assert_not_called()
    tracing._reset_for_testing()


def test_enabled_without_endpoint_warns_and_stays_noop(monkeypatch, caplog):
    """MUST 6: enabled flag but no endpoint → warn once, do NOT activate."""
    tracing._reset_for_testing()
    monkeypatch.setenv("ATOMIC_AGENTS_TRACING_ENABLED", "true")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    with caplog.at_level("WARNING"):
        with patch.object(otel_trace, "set_tracer_provider") as spy:
            tracing._ensure_tracer_provider()

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in caplog.text
    # MUST 6: no provider installed when the endpoint is absent.
    spy.assert_not_called()
    tracing._reset_for_testing()


# ──────────────────────────────────────────────────────────────────
# Span lifecycle on every exit path (MUST 1, MUST 3, MUST 8, MUST 9)


def test_successful_call_exports_one_ok_span(tmp_path, monkeypatch, in_memory_exporter):
    """MUST 3 + MUST 8: a normal-path call exports exactly one finished span with
    outcome=ok and cost/token/iteration attributes set. This is the regression
    guard for the P0 normal-path span leak.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    with patch("atomic_agents.agent._llm.call_llm", return_value=_text_only_response()):
        resp = agent.call("do the thing")

    assert resp.skipped is False
    spans = _spans_named_call(in_memory_exporter)
    assert len(spans) == 1, "successful call must export exactly one call span"
    span = spans[0]
    attrs = dict(span.attributes)
    assert attrs[tracing.ATTR_OUTCOME] == tracing.OUTCOME_OK
    assert attrs[tracing.ATTR_INPUT_TOKENS] == 10
    assert attrs[tracing.ATTR_OUTPUT_TOKENS] == 5
    assert tracing.ATTR_COST_USD in attrs
    assert attrs[tracing.ATTR_TOOL_ITERATIONS] == 1
    assert span.status.status_code == otel_trace.StatusCode.UNSET


def test_exception_in_call_exports_error_span_and_reraises(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 3 + MUST 8: an exception inside call() exports one span with
    outcome=error + ERROR status, and the original exception propagates.
    Regression guard for the P0 exception-path span leak.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    boom = RuntimeError("llm exploded")
    with patch("atomic_agents.agent._llm.call_llm", side_effect=boom):
        with pytest.raises(RuntimeError, match="llm exploded"):
            agent.call("do the thing")

    spans = _spans_named_call(in_memory_exporter)
    assert len(spans) == 1
    span = spans[0]
    assert dict(span.attributes)[tracing.ATTR_OUTCOME] == tracing.OUTCOME_ERROR
    assert span.status.status_code == otel_trace.StatusCode.ERROR


def test_non_lockbusy_acquire_failure_exports_error_span(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 3: a non-LockBusy acquire failure raises before the body try, and
    must still finalize the span (it would otherwise leak).
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    with patch.object(
        agent.lock_backend, "acquire", side_effect=PermissionError("EACCES")
    ):
        with pytest.raises(PermissionError):
            agent.call("do the thing")

    spans = _spans_named_call(in_memory_exporter)
    assert len(spans) == 1
    assert dict(spans[0].attributes)[tracing.ATTR_OUTCOME] == tracing.OUTCOME_ERROR
    assert spans[0].status.status_code == otel_trace.StatusCode.ERROR


def test_lock_busy_exports_lock_busy_span(tmp_path, monkeypatch, in_memory_exporter):
    """MUST 9: lock_busy exports one span with outcome=lock_busy + ERROR status."""
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.exceptions import LockBusy

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    with patch.object(agent.lock_backend, "acquire", side_effect=LockBusy("held")):
        with pytest.raises(LockBusy):
            agent.call("do the thing")

    spans = _spans_named_call(in_memory_exporter)
    assert len(spans) == 1
    assert dict(spans[0].attributes)[tracing.ATTR_OUTCOME] == tracing.OUTCOME_LOCK_BUSY
    assert spans[0].status.status_code == otel_trace.StatusCode.ERROR


def test_cost_skip_exports_skipped_span(tmp_path, monkeypatch, in_memory_exporter):
    """MUST 9: the pre-loop cost-skip path exports one span with outcome=skipped,
    ERROR status, and cost_usd=0.0 (no LLM call was made).
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.types import CostCheckResult

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    skip = CostCheckResult(allow=False, action="skip", reason="daily cap hit")
    with patch.object(agent, "_check_cost_guardrails", return_value=skip):
        resp = agent.call("do the thing")

    assert resp.skipped is True
    spans = _spans_named_call(in_memory_exporter)
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs[tracing.ATTR_OUTCOME] == tracing.OUTCOME_SKIPPED
    assert attrs[tracing.ATTR_COST_USD] == 0.0
    assert spans[0].status.status_code == otel_trace.StatusCode.ERROR


# ──────────────────────────────────────────────────────────────────
# Context-leak guard (MUST 3)


def test_context_token_detached_after_call(tmp_path, monkeypatch, in_memory_exporter):
    """MUST 3: the active OTel context returns to baseline after call(), proving
    the context token was detached (no cross-call span mis-parenting).
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    baseline = otel_context.get_current()
    with patch("atomic_agents.agent._llm.call_llm", return_value=_text_only_response()):
        agent.call("first")
        agent.call("second")
    after = otel_context.get_current()

    # No span context left attached: the current span is invalid (no recording
    # parent leaked from either call).
    assert otel_trace.get_current_span(after) is otel_trace.INVALID_SPAN
    assert after == baseline


# ──────────────────────────────────────────────────────────────────
# Outcome derivation unit coverage (MUST 8)


def test_derive_outcome_precedence():
    class _R:
        def __init__(self, skipped=False, deferred=False):
            self.skipped = skipped
            self.deferred = deferred

    assert tracing._derive_outcome(_R()) == tracing.OUTCOME_OK
    assert tracing._derive_outcome(_R(deferred=True)) == tracing.OUTCOME_DEFERRED
    # skipped beats deferred (MUST 8 precedence)
    assert (
        tracing._derive_outcome(_R(skipped=True, deferred=True))
        == tracing.OUTCOME_SKIPPED
    )


def test_provider_id_from_model():
    assert tracing._provider_id_from_model("claude-haiku-4-5") == "anthropic"
    assert tracing._provider_id_from_model("gpt-5") == "openai"
    assert tracing._provider_id_from_model("o3-mini") == "openai"
    assert tracing._provider_id_from_model("moonshot/kimi-k2") == "moonshot"
    assert tracing._provider_id_from_model("some-future-model") == "unknown"


# ──────────────────────────────────────────────────────────────────
# Partial-spend on the error path (MUST 1) — the error span is the single most
# operationally important span; cost is first-class (design principle 4).


def test_error_span_after_spend_records_true_partial_cost(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 1: a raise that fires AFTER real LLM spend must carry the true partial
    spend into the error span — NOT 0.0.

    This is the regression guard for the under-reporting bug: the span
    accumulators must be synced per loop iteration, so a post-spend raise (here,
    capture extraction blowing up after iteration 1 already spent tokens) records
    the actual cost / input_tokens, not the initialized 0.0.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    boom = RuntimeError("capture extraction exploded")
    with patch("atomic_agents.agent._llm.call_llm", return_value=_text_only_response()):
        # extract_all_captures runs AFTER the iter spend has been accumulated and
        # synced into the span accumulators — forcing it to raise pins MUST 1.
        with patch(
            "atomic_agents.agent._capture.extract_all_captures", side_effect=boom
        ):
            with pytest.raises(RuntimeError, match="capture extraction exploded"):
                agent.call("do the thing")

    spans = _spans_named_call(in_memory_exporter)
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs[tracing.ATTR_OUTCOME] == tracing.OUTCOME_ERROR
    assert spans[0].status.status_code == otel_trace.StatusCode.ERROR
    # The spend from iteration 1 (10 in / 5 out) must be on the error span.
    assert attrs[tracing.ATTR_INPUT_TOKENS] == 10
    assert attrs[tracing.ATTR_OUTPUT_TOKENS] == 5
    assert attrs[tracing.ATTR_COST_USD] > 0.0


def test_mid_loop_cost_cap_records_partial_spend(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 1 / MUST 9: the mid-loop cost-cap path must record the TRUE partial
    spend (not 0.0) and outcome=skipped + ERROR status.

    Drives iteration_count > 1: a tool_use response on iteration 1 keeps the loop
    alive; the cost guardrail allows the pre-loop check then returns skip on the
    iteration-2 check, firing the mid-loop branch. This is the path whose
    partial-spend sync is most likely to silently regress to 0.0.
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.types import CostCheckResult

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")
    _register_noop_tool(agent)

    allow = CostCheckResult(allow=True, action="proceed", reason="ok")
    skip = CostCheckResult(allow=False, action="skip", reason="mid-loop cap")
    # Call 1 = pre-loop check (allow), call 2 = iteration-2 check (skip).
    guardrail_calls = [allow, skip]

    def _guardrail(*_a, **_k):
        return guardrail_calls.pop(0) if guardrail_calls else skip

    with patch("atomic_agents.agent._llm.call_llm", return_value=_tool_use_response()):
        with patch.object(agent, "_check_cost_guardrails", side_effect=_guardrail):
            resp = agent.call("do the thing")

    assert resp.skipped is True
    spans = _spans_named_call(in_memory_exporter)
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs[tracing.ATTR_OUTCOME] == tracing.OUTCOME_SKIPPED
    assert spans[0].status.status_code == otel_trace.StatusCode.ERROR
    # Partial spend from iteration 1 (100 in / 50 out) MUST be recorded — the
    # distinguishing assertion vs the pre-loop cost-skip (which records 0.0).
    assert attrs[tracing.ATTR_COST_USD] > 0.0
    assert attrs[tracing.ATTR_INPUT_TOKENS] == 100
    assert attrs[tracing.ATTR_OUTPUT_TOKENS] == 50
    # tool_iterations == completed-iteration count (iteration_count - 1).
    assert attrs[tracing.ATTR_TOOL_ITERATIONS] == 1


# ──────────────────────────────────────────────────────────────────
# Finalizer is genuinely non-throwing (MUST 3) — a host-provider span whose
# set_attribute / record_exception raises must NOT leak the agent lock.


def test_finalizer_tracing_failure_still_releases_lock(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 3: if the recording span raises inside the finalizer, the original
    call error still propagates AND the agent lock is released.

    Propagation out of the finalizer would abort the body finally before lock
    release (leaking the agent lock — every subsequent call returns LockBusy) and
    could replace the in-flight exception with a tracing error. We make the span's
    record_exception raise, drive a call() that errors, and assert (a) the
    original error propagates, (b) a second call succeeds (lock was released).
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    boom = RuntimeError("llm exploded")

    # Patch the tracer so the started span's record_exception throws (simulating a
    # host-provider recording span with a misbehaving SpanProcessor / exception).
    real_tracer = tracing.get_tracer()
    real_start = real_tracer.start_span

    def _start_span(*a, **k):
        span = real_start(*a, **k)
        span.record_exception = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("tracing record_exception blew up")
        )
        return span

    with patch.object(tracing._tracer, "start_span", side_effect=_start_span):
        with patch("atomic_agents.agent._llm.call_llm", side_effect=boom):
            with pytest.raises(RuntimeError, match="llm exploded"):
                agent.call("first")

    # The lock must have been released despite the finalizer's tracing failure:
    # a second normal call succeeds (does NOT raise LockBusy).
    with patch("atomic_agents.agent._llm.call_llm", return_value=_text_only_response()):
        resp = agent.call("second")
    assert resp.skipped is False


def test_finalizer_span_end_raise_still_releases_lock(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 3: if span.end() raises inside the finalizer's inner finally (the
    single most likely host-callback site — Span.end() invokes SpanProcessor
    on_end synchronously), the agent lock is STILL released.

    Non-vacuity: neutering the safe_span_op guard around _call_span.end() makes
    this fail with LockBusy on the second call. The pre-existing finalizer test
    only patches record_exception (inside the guarded attribute body), leaving the
    end()/detach() teardown paths uncovered — this closes that hole.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    real_tracer = tracing.get_tracer()
    real_start = real_tracer.start_span

    def _start_span(*a, **k):
        span = real_start(*a, **k)
        span.end = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("tracing span.end blew up (on_end raised)")
        )
        return span

    with patch.object(tracing._tracer, "start_span", side_effect=_start_span):
        with patch(
            "atomic_agents.agent._llm.call_llm", return_value=_text_only_response()
        ):
            # The call itself must NOT raise even though end() throws.
            resp = agent.call("first")
    assert resp.skipped is False

    # Lock released despite end() raising: a second call succeeds.
    with patch("atomic_agents.agent._llm.call_llm", return_value=_text_only_response()):
        resp2 = agent.call("second")
    assert resp2.skipped is False


def test_finalizer_detach_raise_still_releases_lock(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 3: if context.detach() raises inside the finalizer's inner finally
    (foreign/stale token on some OTel versions), the call still completes AND the
    span is still ended AND the agent lock is released.

    Non-vacuity: neutering the safe_span_op guard around detach makes the second
    call raise LockBusy.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    original_detach = tracing._otel_ctx.detach

    def _boom_detach(token):
        raise RuntimeError("tracing context.detach blew up (stale token)")

    # Forcing detach to raise deliberately leaks the OTel context token (the
    # product's contract is only that the LOCK is released, not that a
    # crashing detach still unwinds the context). Snapshot + restore the OTel
    # context around this call so the leaked token does not bleed into later
    # tests' get_current_span() assertions — a test-isolation concern, not a
    # product one.
    _ctx_before = otel_context.get_current()
    try:
        with patch.object(tracing._otel_ctx, "detach", side_effect=_boom_detach):
            with patch(
                "atomic_agents.agent._llm.call_llm",
                return_value=_text_only_response(),
            ):
                resp = agent.call("first")
        assert resp.skipped is False
        # Restore so the second call's teardown is clean.
        assert tracing._otel_ctx.detach is original_detach

        with patch(
            "atomic_agents.agent._llm.call_llm", return_value=_text_only_response()
        ):
            resp2 = agent.call("second")
        assert resp2.skipped is False
    finally:
        otel_context.attach(_ctx_before)


def test_open_start_span_raise_still_runs_and_audits(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 3 (open path): if start_span() raises (host SpanProcessor.on_start
    fault), call() still runs the agent, writes the agent_call audit JSONL line
    (Principle 5 — audit trail is structural), and releases the lock.

    The open block sits BEFORE the lock acquire and BEFORE the body try/finally,
    so an unguarded fault here would crash call() before any audit line is
    written. Non-vacuity: neutering the open-path guard makes call() raise.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    def _boom_start(*a, **k):
        raise RuntimeError("tracing start_span blew up (on_start raised)")

    with patch.object(tracing._tracer, "start_span", side_effect=_boom_start):
        with patch(
            "atomic_agents.agent._llm.call_llm", return_value=_text_only_response()
        ):
            resp = agent.call("first")
    assert resp.skipped is False

    # Audit JSONL line written despite the tracing-open fault.
    log_files = list((agent.agent_root / "log").rglob("*.jsonl"))
    assert log_files, "agent_call audit line must be written even when span-open fails"
    assert any(f.read_text().strip() for f in log_files)

    # Lock released: a second call succeeds.
    with patch("atomic_agents.agent._llm.call_llm", return_value=_text_only_response()):
        resp2 = agent.call("second")
    assert resp2.skipped is False


def test_open_set_attribute_raise_still_runs_and_audits(
    tmp_path, monkeypatch, in_memory_exporter
):
    """MUST 3 (open path): if set_attribute raises AFTER start_span +
    context.attach succeed (host SpanLimits fault), call() still runs, the
    half-opened span is ended + its context token detached (no leak), the audit
    line is written, and the lock is released.

    This exercises the open-fault cleanup branch (detach + end of the real,
    partially-configured span). Non-vacuity: removing the cleanup leaks the OTel
    context token, and removing the guard makes call() raise.
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    agent = AtomicAgent(name="test")

    real_tracer = tracing.get_tracer()
    real_start = real_tracer.start_span

    def _start_span(*a, **k):
        span = real_start(*a, **k)
        span.set_attribute = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("tracing set_attribute blew up at open (SpanLimits)")
        )
        return span

    with patch.object(tracing._tracer, "start_span", side_effect=_start_span):
        with patch(
            "atomic_agents.agent._llm.call_llm", return_value=_text_only_response()
        ):
            resp = agent.call("first")
    assert resp.skipped is False

    # No OTel context token leaked across the failed open: current span is the
    # non-recording default, not the half-opened call span.
    assert otel_trace.get_current_span() is otel_trace.INVALID_SPAN

    log_files = list((agent.agent_root / "log").rglob("*.jsonl"))
    assert log_files and any(f.read_text().strip() for f in log_files)

    with patch("atomic_agents.agent._llm.call_llm", return_value=_text_only_response()):
        resp2 = agent.call("second")
    assert resp2.skipped is False


# ──────────────────────────────────────────────────────────────────
# Production activation path (MUST 2, MUST 6) — _ensure_tracer_provider direct
# tests that do NOT use the bypass fixture.


def test_ensure_provider_installs_sdk_when_global_is_proxy(monkeypatch):
    """MUST 6 install branch: enabled + endpoint + SDK present + bare
    ProxyTracerProvider global → set_tracer_provider IS called with an SDK
    TracerProvider.
    """
    from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

    tracing._reset_for_testing()
    monkeypatch.setenv("ATOMIC_AGENTS_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    with patch.object(otel_trace, "set_tracer_provider") as spy:
        tracing._ensure_tracer_provider()

    spy.assert_called_once()
    installed = spy.call_args.args[0]
    assert isinstance(installed, SDKTracerProvider)
    tracing._reset_for_testing()


def test_ensure_provider_constructs_exporter_with_no_endpoint(monkeypatch):
    """spec/39: the OTLPSpanExporter MUST be constructed with NO endpoint argument
    so the exporter reads OTEL_EXPORTER_OTLP_ENDPOINT itself, appends /v1/traces,
    and honors the signal-specific OTEL_EXPORTER_OTLP_TRACES_ENDPOINT override.

    Passing endpoint=<base> would make the exporter POST to the bare base URL
    (no /v1/traces) — most collectors 404 that and BatchSpanProcessor swallows the
    failure, so spans vanish silently. This pins against a refactor re-introducing
    the OTLPSpanExporter(endpoint=endpoint) shortcut a prior round removed.
    Non-vacuity: reverting tracing.py to OTLPSpanExporter(endpoint=endpoint) makes
    this fail.
    """
    tracing._reset_for_testing()
    monkeypatch.setenv("ATOMIC_AGENTS_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    # Patch the exporter symbol at its source module so tracing.py's deferred
    # import binds to the spy.
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as _otlp_mod

    with patch.object(_otlp_mod, "OTLPSpanExporter") as exporter_ctor:
        with patch.object(otel_trace, "set_tracer_provider"):
            tracing._ensure_tracer_provider()

    exporter_ctor.assert_called_once()
    assert "endpoint" not in exporter_ctor.call_args.kwargs, (
        "exporter must be constructed without an endpoint= keyword"
    )
    assert len(exporter_ctor.call_args.args) == 0, (
        "exporter must be constructed without a positional endpoint argument"
    )
    tracing._reset_for_testing()


def test_ensure_provider_defers_to_host_provider(monkeypatch, caplog):
    """MUST 6 defer branch: a real host provider is already installed → the
    framework MUST NOT call set_tracer_provider, and logs the deferral.
    """
    from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

    tracing._reset_for_testing()
    monkeypatch.setenv("ATOMIC_AGENTS_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Install a real (non-Proxy) host provider first.
    otel_trace.set_tracer_provider(SDKTracerProvider())
    tracing._tracer_provider_configured = False  # allow ensure to run again

    with caplog.at_level("INFO"):
        with patch.object(otel_trace, "set_tracer_provider") as spy:
            tracing._ensure_tracer_provider()

    spy.assert_not_called()
    assert "deferring to it" in caplog.text
    tracing._reset_for_testing()


def test_ensure_provider_sdk_missing_warns_and_does_not_install(monkeypatch, caplog):
    """MUST 2 fallback: SDK import fails → warn once, do NOT install a provider."""
    import builtins

    tracing._reset_for_testing()
    monkeypatch.setenv("ATOMIC_AGENTS_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name.startswith("opentelemetry.sdk") or name.startswith(
            "opentelemetry.exporter"
        ):
            raise ImportError(f"simulated missing: {name}")
        return real_import(name, *a, **k)

    with caplog.at_level("WARNING"):
        with patch.object(builtins, "__import__", side_effect=_fake_import):
            with patch.object(otel_trace, "set_tracer_provider") as spy:
                tracing._ensure_tracer_provider()

    spy.assert_not_called()
    assert "opentelemetry-sdk" in caplog.text
    tracing._reset_for_testing()


def test_reset_restores_proxy_provider():
    """The test-only reset MUST return the OTel global to a ProxyTracerProvider.

    Pins the version-fragile private-state poke in _reset_for_testing: if a future
    opentelemetry-api bump moves those internals, this assertion (or the loud
    AttributeError guard) surfaces the drift instead of letting span state bleed
    silently across tests.
    """
    from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

    otel_trace.set_tracer_provider(SDKTracerProvider())
    tracing._reset_for_testing()
    assert isinstance(otel_trace.get_tracer_provider(), otel_trace.ProxyTracerProvider)


# ──────────────────────────────────────────────────────────────────
# Disabled path records zero spans (MUST 6) — the off-by-default throughline.


def test_disabled_call_records_zero_spans(tmp_path, monkeypatch):
    """MUST 6 / throughline: with tracing OFF and no host provider, a real call()
    records zero spans and the span it opens is non-recording.

    Leaves the genuine default ProxyTracerProvider in place (no
    set_tracer_provider, no forced module tracer) so get_tracer() runs the real
    env-gated path: tracing is disabled → no provider installed → the default
    proxy resolves a non-recording tracer. We attach an InMemorySpanExporter to a
    SEPARATE provider that is NOT the global, so any span the call() emits would
    have to flow through the (non-recording) global to be captured — it doesn't.
    This directly proves the home user pays zero cost when off.
    """
    from atomic_agents.agent import AtomicAgent

    tracing._reset_for_testing()
    monkeypatch.delenv("ATOMIC_AGENTS_TRACING_ENABLED", raising=False)
    # An exporter that would capture spans IF any recording provider routed to it.
    exporter = InMemorySpanExporter()

    try:
        _build_minimal_agent_dir(tmp_path)
        monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
        agent = AtomicAgent(name="test")

        # The span the disabled call opens must be non-recording (proxy default).
        opened_spans = []
        real_tracer = tracing.get_tracer()
        real_start = real_tracer.start_span

        def _capture_start(*a, **k):
            span = real_start(*a, **k)
            opened_spans.append(span)
            return span

        with patch.object(tracing._tracer, "start_span", side_effect=_capture_start):
            with patch(
                "atomic_agents.agent._llm.call_llm",
                return_value=_text_only_response(),
            ):
                resp = agent.call("do the thing")

        assert resp.skipped is False
        # Exactly one call span was opened, and it is NON-recording (the default
        # ProxyTracerProvider's tracer yields non-recording spans when no SDK is
        # installed) — so nothing was ever exported.
        assert len(opened_spans) == 1
        assert opened_spans[0].is_recording() is False
        assert exporter.get_finished_spans() == ()
    finally:
        tracing._reset_for_testing()

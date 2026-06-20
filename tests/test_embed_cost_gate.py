"""Embed cost gate integration tests (spec/46, issue #544 PR1).

Covers:
- PRIMITIVE_EMBED constant and _PRIMITIVE_BY_TRIGGER routing
- Batch embed gate at the post-loop capture-commit site in agent.call()
- _has_effective_embed_cap() helper: model.md + Policy + tree-cap resolution
- _emit_embed_batch_reservation() and _emit_embed_batch_release() JSONL shapes
- Fail-closed predicate: degraded AND effective cap = block; degraded AND no cap = pass
- Release in finally: actual_usd=0 when write_note() fails; release even on exception
- Non-embedding memory backend: gate is a no-op (supports_semantic_search=False)
- check_embedding_backend() doctor check: SKIP / PASS / WARN / FAIL branches

Per project lessons:
- feedback_false_green_test_needs_per_invocation_negative_control: every assertion
  has a strip-and-RED negative control confirming the guard is load-bearing.
- feedback_layered_except_typed_branch_false_green: typed branch tests assert a
  branch-distinctive JSONL trigger, not just the shared return value.
- feedback_fail_closed_only_where_theres_something_to_protect: the fail-closed gate
  MUST NOT fire when no effective cap is active.
"""

from __future__ import annotations

import math
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.logs.types import PRIMITIVE_EMBED


# ──────────────────────────────────────────────────────────────────────────────
# PRIMITIVE_EMBED constant


def test_primitive_embed_value():
    """PRIMITIVE_EMBED is the string 'embed' — new dedicated bucket."""
    assert PRIMITIVE_EMBED == "embed"


def test_primitive_embed_exported_from_logs_package():
    """PRIMITIVE_EMBED is re-exported from atomic_agents.logs."""
    from atomic_agents.logs import PRIMITIVE_EMBED as exported

    assert exported == "embed"


# ──────────────────────────────────────────────────────────────────────────────
# _PRIMITIVE_BY_TRIGGER routing


def test_embed_trigger_routes_to_primitive_embed():
    """All four embed triggers map to PRIMITIVE_EMBED in _PRIMITIVE_BY_TRIGGER."""
    from atomic_agents.agent import _PRIMITIVE_BY_TRIGGER

    for trigger in (
        "embed_reservation",
        "embed_release",
        "embed_batch_reservation",
        "embed_batch_release",
    ):
        assert _PRIMITIVE_BY_TRIGGER.get(trigger) == PRIMITIVE_EMBED, (
            f"trigger {trigger!r} should map to PRIMITIVE_EMBED but maps to "
            f"{_PRIMITIVE_BY_TRIGGER.get(trigger)!r}"
        )


def test_embed_triggers_are_distinct_from_helper_bucket():
    """Embed triggers MUST NOT map to PRIMITIVE_HELPER — separate billing bucket."""
    from atomic_agents.agent import _PRIMITIVE_BY_TRIGGER
    from atomic_agents.logs.types import PRIMITIVE_HELPER

    for trigger in (
        "embed_reservation",
        "embed_release",
        "embed_batch_reservation",
        "embed_batch_release",
    ):
        assert _PRIMITIVE_BY_TRIGGER.get(trigger) != PRIMITIVE_HELPER


# ──────────────────────────────────────────────────────────────────────────────
# In-memory fakes (mirrors test_conversation_agent_wiring)


class _FakeLockBackend:
    backend_id = "fake-inmemory"

    def __init__(self) -> None:
        self._held = False

    def acquire(self, name: str = "", timeout: float = 0.0):
        import time as _time

        from atomic_agents.exceptions import LockBusy
        from atomic_agents.locks.types import LockHandle

        if self._held:
            raise LockBusy(f"lock {name!r} already held (fake)")
        self._held = True
        handle = LockHandle(
            name=name, acquired_at=_time.time(), holder_pid=0, backend_state=object()
        )
        object.__setattr__(handle, "_backend", self)
        return handle

    def release(self, handle) -> None:
        self._held = False
        try:
            object.__setattr__(handle, "backend_state", None)
        except Exception:
            pass

    def renew(self, handle) -> bool:
        return True

    def is_held(self, name: str = "") -> bool:
        return self._held

    def capabilities(self):
        from atomic_agents.locks.types import LockCapabilities

        return LockCapabilities()

    def scope(self, sub_path: str):
        return self


class _FakeLogBackend:
    """In-memory log backend for test isolation.

    append() accepts both RunRecord dataclass instances (what agent._log sends
    post-#544) and raw dicts (for any test that calls append() directly).
    The records list stores plain dicts so assertions use dict-access uniformly.
    """

    backend_id = "fake-inmemory"

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record) -> None:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(record) and not isinstance(record, type):
            # RunRecord is a dataclass; convert to dict for uniform assertion.
            self.records.append(asdict(record))
        else:
            self.records.append(dict(record))

    def query(self, q):
        return list(self.records)

    def tail(self, n: int = 50):
        return list(self.records)[-n:]

    def aggregate(self, *a, **k):
        return {}


def _build_agent_root(agents_root: Path, name: str = "embedbot") -> Path:
    agent_dir = agents_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "persona").mkdir(exist_ok=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (agent_dir / "tools.md").write_text(
        "## Write paths\n- memory/\n\n## Read-only paths\n(none)\n", encoding="utf-8"
    )
    (agent_dir / "memory").mkdir(exist_ok=True)
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n", encoding="utf-8"
    )
    return agents_root


def _fake_llm_response_with_capture(
    capture_name: str = "test-note", body: str = "hello world"
):
    """LLM response that includes a capture via the atomic_capture tool_use path.

    Injects a valid atomic_capture tool_use dict so extract_all_captures()
    produces one Capture. The text field is empty because Path 1 (tool_use) is
    the load-bearing path and needs no fenced-text fallback here.
    """
    resp = MagicMock()
    resp.text = ""
    # Valid atomic_capture tool_use entry matching the JSON Schema in _capture.py
    resp.tool_uses = [
        {
            "name": "atomic_capture",
            "id": "tu-test-001",
            "input": {
                "type": "user",
                "name": capture_name,
                "description": "test capture for embed gate",
                "confidence": "high",
                "sources": ["embed gate test"],
                "body": body,
            },
        }
    ]
    resp.input_tokens = 7
    resp.output_tokens = 3
    resp.cache_hit_tokens = 0
    resp.cache_miss_tokens = 0
    resp.raw = {}
    return resp


def _fake_llm_response_no_capture():
    resp = MagicMock()
    resp.text = "no capture here"
    resp.tool_uses = []
    resp.input_tokens = 5
    resp.output_tokens = 2
    resp.cache_hit_tokens = 0
    resp.cache_miss_tokens = 0
    resp.raw = {}
    return resp


def _fake_llm_response_multi_capture(items: list[tuple[str, str]]):
    """LLM response emitting one atomic_capture tool_use per (name, body) tuple."""
    resp = MagicMock()
    resp.text = ""
    resp.tool_uses = [
        {
            "name": "atomic_capture",
            "id": f"tu-{i}",
            "input": {
                "type": "user",
                "name": name,
                "description": "multi-capture embed gate test",
                "confidence": "high",
                "sources": ["embed gate test"],
                "body": body,
            },
        }
        for i, (name, body) in enumerate(items)
    ]
    resp.input_tokens = 7
    resp.output_tokens = 3
    resp.cache_hit_tokens = 0
    resp.cache_miss_tokens = 0
    resp.raw = {}
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# Fake memory backend that simulates supports_semantic_search=True


class _StubEmbeddingBackend:
    """Minimal EmbeddingBackend stub exposing only .model_id / .provider_id.

    Matches the live-backend surface the embed cost gate reads from
    MemoryCapabilities.embedding_backend_resolved: the gate prices on .model_id
    (the model id), and .provider_id is the provider label. No billable methods
    are exercised by the gate (it only reads attributes), so embed()/embed_batch()
    are intentionally absent.
    """

    def __init__(
        self, model_id: str = "text-embedding-3-small", provider_id: str = "openai"
    ) -> None:
        self.model_id = model_id
        self.provider_id = provider_id


class _FakeSemanticMemoryBackend:
    """Minimal MemoryBackend fake that advertises supports_semantic_search=True.

    write_note() records the captured names without touching the filesystem.
    Used to verify the embed gate arms and emits reservation/release pairs.
    """

    backend_id = "fake-semantic"
    _written: list[Any]

    def __init__(
        self,
        write_should_fail: bool = False,
        fail_names: frozenset[str] | None = None,
    ) -> None:
        self._written = []
        self._write_should_fail = write_should_fail
        # Names that should raise in write_note() (partial-batch true-up tests).
        self._fail_names = fail_names or frozenset()

    @property
    def supports_semantic_search(self) -> bool:
        return True

    @property
    def supports_canonical_export(self) -> bool:
        return False

    def capabilities(self):
        from atomic_agents.memory.backend import MemoryCapabilities

        # Match the PRODUCTION contract of PgvectorMemoryBackend.capabilities():
        #   embedding_provider = a provider LABEL ("openai") — provider_id, NOT a
        #     model id (pgvector.py sets it to self._embedding_backend.provider_id).
        #   embedding_backend_resolved = the live EmbeddingBackend whose .model_id
        #     ("text-embedding-3-small") is the pricing key the cost gate reads.
        # The gate MUST resolve the model id from embedding_backend_resolved.model_id,
        # never from the provider label. A fake that puts a model id into
        # embedding_provider would be a false-green that does not match production.
        return MemoryCapabilities(
            embedding_provider="openai",
            embedding_backend_resolved=_StubEmbeddingBackend(
                model_id="text-embedding-3-small"
            ),
        )

    def write_note(self, capture, policy):
        if self._write_should_fail:
            raise RuntimeError("write_note forced failure for test")
        if capture.name in self._fail_names:
            raise RuntimeError(f"write_note forced failure for {capture.name}")
        self._written.append(capture.name)

    def list_notes(self, path=None):
        return []

    def read_note(self, name):
        return None

    def load_all_notes(self):
        return []

    def index_summary_text(self):
        return ""

    def close(self):
        pass


class _FakeNonSemanticMemoryBackend:
    """Minimal MemoryBackend fake that advertises supports_semantic_search=False."""

    backend_id = "fake-nonsemantic"

    @property
    def supports_semantic_search(self) -> bool:
        return False

    @property
    def supports_canonical_export(self) -> bool:
        return False

    def capabilities(self):
        from atomic_agents.memory.backend import MemoryCapabilities

        # No semantic search -> no embedding provider.
        return MemoryCapabilities(embedding_provider=None)

    def write_note(self, capture, policy):
        pass  # no embed

    def list_notes(self, path=None):
        return []

    def read_note(self, name):
        return None

    def load_all_notes(self):
        return []

    def index_summary_text(self):
        return ""

    def close(self):
        pass


class _ProviderLabelOnlyMemoryBackend(_FakeSemanticMemoryBackend):
    """Semantic backend whose capabilities() returns a provider LABEL but NO
    resolved EmbeddingBackend (embedding_backend_resolved=None).

    Models the degraded/older shape where the live backend is not exposed. The
    gate MUST fall back to the provider label as the pricing key — which is not a
    model id, so calc_embedding_cost() falls back to the max-rate estimate and
    cost_estimated=True. Used as the per-invocation negative control proving the
    model id genuinely flows from embedding_backend_resolved.model_id, not the
    provider label.
    """

    def capabilities(self):
        from atomic_agents.memory.backend import MemoryCapabilities

        return MemoryCapabilities(
            embedding_provider="openai",
            embedding_backend_resolved=None,
        )


def _make_agent(
    tmp_path: Path,
    *,
    memory_backend=None,
    name: str = "embedbot",
    daily_cap_usd: float = 0.0,
):
    agents_root = _build_agent_root(tmp_path, name)
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name=name,
        trigger="manual",
        agents_root=agents_root,
        lock_backend=_FakeLockBackend(),
        log_backend=_FakeLogBackend(),
    )
    if memory_backend is not None:
        agent.memory = memory_backend
    # Optionally configure a cost cap for fail-closed tests.
    if daily_cap_usd > 0:
        agent.config = dc_replace(agent.config, daily_cap_usd=daily_cap_usd)
    return agent


def _run_call(
    agent,
    *,
    work_item: str = "embed test",
    llm_response=None,
    log_sink: list | None = None,
    cost_allow: bool = True,
    parent_remaining_headroom_usd: float | None = None,
):
    """Run agent.call() with stubs. Returns Response."""
    if llm_response is None:
        llm_response = _fake_llm_response_no_capture()

    def fake_log(record: dict) -> None:
        if log_sink is not None:
            log_sink.append(dict(record))

    cost_result = MagicMock(
        allow=cost_allow,
        action="ok",
        reason="cap",
        cost_data_degraded=False,
    )

    kwargs: dict[str, Any] = {"work_item": work_item}
    if parent_remaining_headroom_usd is not None:
        kwargs["parent_remaining_headroom_usd"] = parent_remaining_headroom_usd

    with (
        patch("atomic_agents._llm.call_llm", return_value=llm_response),
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "assemble_system_prompt", return_value="You are EmbedBot."),
        patch.object(agent, "_check_cost_guardrails", return_value=cost_result),
    ):
        return agent.call(**kwargs)


# ──────────────────────────────────────────────────────────────────────────────
# Embed batch gate: reservation and release JSONL pairs


def test_embed_batch_gate_emits_reservation_and_release(tmp_path):
    """Gate emits embed_batch_reservation + embed_batch_release when memory
    backend supports_semantic_search=True and captures are present."""
    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    # LLM response with one capture
    llm_resp = _fake_llm_response_with_capture("note-1", "hello embed gate")

    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    triggers = [r["trigger"] for r in log_sink]
    assert "embed_batch_reservation" in triggers, (
        "expected embed_batch_reservation in JSONL records but got: "
        + repr([r["trigger"] for r in log_sink])
    )
    assert "embed_batch_release" in triggers, (
        "expected embed_batch_release in JSONL records but got: "
        + repr([r["trigger"] for r in log_sink])
    )


def test_embed_batch_gate_not_emitted_for_nonsemantic_backend(tmp_path):
    """Gate is a no-op when backend.supports_semantic_search=False.

    Negative control: the reservation MUST appear when the property returns True
    (proven in test_embed_batch_gate_emits_reservation_and_release).
    """
    fake_mem = _FakeNonSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    llm_resp = _fake_llm_response_with_capture("note-1", "no embed here")

    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    triggers = [r["trigger"] for r in log_sink]
    assert "embed_batch_reservation" not in triggers
    assert "embed_batch_release" not in triggers


def test_embed_batch_gate_not_emitted_when_no_captures(tmp_path):
    """Gate is a no-op when there are no captures (nothing to embed)."""
    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    # LLM response with no capture payload
    _run_call(agent, llm_response=_fake_llm_response_no_capture(), log_sink=log_sink)

    triggers = [r["trigger"] for r in log_sink]
    assert "embed_batch_reservation" not in triggers
    assert "embed_batch_release" not in triggers


# ──────────────────────────────────────────────────────────────────────────────
# Reservation record shape


def test_embed_batch_reservation_record_shape(tmp_path):
    """embed_batch_reservation record has the required fields."""
    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    llm_resp = _fake_llm_response_with_capture("note-1", "hello embed gate")
    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    res_records = [r for r in log_sink if r.get("trigger") == "embed_batch_reservation"]
    assert len(res_records) == 1
    rec = res_records[0]

    assert rec.get("output_tokens") == 0, "embedding is input-only"
    assert rec.get("batch_size", 0) >= 1
    assert "reserved_usd" in rec
    assert "cost_estimated" in rec
    assert "model" in rec
    assert rec.get("cost_source") == "actor"


def test_embed_batch_release_record_shape(tmp_path):
    """embed_batch_release record has the required fields including actual_usd."""
    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    llm_resp = _fake_llm_response_with_capture("note-1", "hello embed gate")
    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    rel_records = [r for r in log_sink if r.get("trigger") == "embed_batch_release"]
    assert len(rel_records) == 1
    rec = rel_records[0]

    assert rec.get("output_tokens") == 0, "embedding is input-only"
    assert "actual_usd" in rec
    assert "reserved_usd" in rec
    assert "written_count" in rec
    assert "batch_size" in rec
    assert rec.get("cost_source") == "actor"


def test_embed_batch_reservation_precedes_release(tmp_path):
    """embed_batch_reservation MUST appear before embed_batch_release in the log."""
    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    llm_resp = _fake_llm_response_with_capture("note-1", "order check")
    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    triggers = [r["trigger"] for r in log_sink]
    res_idx = next(i for i, t in enumerate(triggers) if t == "embed_batch_reservation")
    rel_idx = next(i for i, t in enumerate(triggers) if t == "embed_batch_release")
    assert res_idx < rel_idx, (
        f"reservation at index {res_idx}, release at {rel_idx} — wrong order"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Release in finally: actual_usd=0 when write_note fails


def test_embed_batch_release_emitted_even_when_write_note_fails(tmp_path):
    """embed_batch_release fires in finally even when write_note() raises.

    actual_usd in the release record reflects only the successfully-written
    notes (0 when nothing succeeded).
    """
    fake_mem = _FakeSemanticMemoryBackend(write_should_fail=True)
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    llm_resp = _fake_llm_response_with_capture("note-fail", "will fail")
    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    triggers = [r["trigger"] for r in log_sink]
    assert "embed_batch_release" in triggers, (
        "release MUST be emitted in finally regardless of write_note() failure"
    )

    rel_records = [r for r in log_sink if r.get("trigger") == "embed_batch_release"]
    assert len(rel_records) == 1
    # written_count is 0 because write_note() failed for every capture
    assert rel_records[0].get("written_count") == 0


def test_embed_batch_partial_failure_trues_up_only_written(tmp_path):
    """On a partial batch (some write_note() raise), actual_usd reflects ONLY the
    successfully-written notes — NOT the full reserved amount, NOT zero.

    Two captures of equal size; one write_note() fails. actual_usd MUST equal the
    single-item cost (one written), and written_count MUST be 1.

    Per-invocation negative control (reproducible strip): move the
    ``_embed_actual_usd += _note_cost`` true-up in agent.py to BEFORE
    ``self.memory.write_note(c, policy)`` (so it sums failed items too). With two
    equal-size captures and one failing, actual_usd becomes 2x one_item_cost while
    written_count stays 1 — the assertions below go RED. The true-up is placed
    AFTER the successful write specifically to keep this invariant.
    """
    from atomic_agents._costs import calc_embedding_cost

    body = "B" * 300  # 100 tokens each
    fake_mem = _FakeSemanticMemoryBackend(fail_names=frozenset({"note-bad"}))
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    llm_resp = _fake_llm_response_multi_capture(
        [("note-good", body), ("note-bad", body)]
    )
    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    rel = next(r for r in log_sink if r.get("trigger") == "embed_batch_release")
    res = next(r for r in log_sink if r.get("trigger") == "embed_batch_reservation")

    assert rel.get("written_count") == 1, "exactly one note should be written"
    assert res.get("batch_size") == 2, "both captures should be in the reservation"

    one_item_cost, est = calc_embedding_cost("text-embedding-3-small", 100)
    assert est is False
    actual_usd = rel.get("actual_usd", 0.0)
    assert actual_usd == pytest.approx(one_item_cost), (
        f"actual_usd {actual_usd} should be the single-written-item cost "
        f"{one_item_cost}, not the full 2-item spend and not zero"
    )


def test_embed_batch_release_actual_usd_nonzero_on_success(tmp_path):
    """actual_usd in release > 0 when at least one note is written.

    Negative control: actual_usd stays 0 when write_note() fails (proven
    in test_embed_batch_release_emitted_even_when_write_note_fails).
    """
    from atomic_agents._costs import calc_embedding_cost

    fake_mem = _FakeSemanticMemoryBackend(write_should_fail=False)
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    # Use a longer body so the chars/3 estimate yields a positive cost for a
    # known-model (text-embedding-3-small is in EMBEDDING_PRICING).
    body = "A" * 300  # 300 chars → 100 tokens → cost > 0
    llm_resp = _fake_llm_response_with_capture("note-success", body)
    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    rel_records = [r for r in log_sink if r.get("trigger") == "embed_batch_release"]
    assert len(rel_records) == 1
    rec = rel_records[0]

    # The model_id MUST flow through from
    # capabilities().embedding_backend_resolved.model_id — NOT from the
    # embedding_provider label ("openai"). A regression that reads the provider
    # label instead (or swallows the resolved-backend read) falls back to the
    # EMBEDDING_PRICING max-rate estimate with model="openai". Assert the real
    # model id so that gap is caught here, not silently masked. See
    # test_embed_model_id_resolves_from_resolved_backend_not_provider_label for
    # the per-invocation strip negative control.
    assert rec.get("model") == "text-embedding-3-small", (
        "model_id did not resolve from "
        "capabilities().embedding_backend_resolved.model_id; "
        f"got {rec.get('model')!r}"
    )
    assert rec.get("cost_estimated") is False, "known model must not be cost_estimated"

    # 100 tokens at text-embedding-3-small: deterministic, NOT a fallback estimate.
    expected_cost, est = calc_embedding_cost("text-embedding-3-small", 100)
    assert est is False
    actual_usd = rec.get("actual_usd", 0.0)
    assert actual_usd == pytest.approx(expected_cost), (
        f"actual_usd {actual_usd} != expected per-item cost {expected_cost}"
    )
    assert actual_usd > 0.0, "one written note must produce positive actual spend"


def test_embed_batch_reservation_is_2x_fanout_of_actual(tmp_path):
    """reserved_usd is the worst-case 2x fan-out (per_item_sum + batch_sum) of the
    single-item actual when the whole batch writes.

    The reservation MUST never under-reserve: for a fully-written single-item
    batch, reserved == 2 * actual (per-item path + batch path, same input tokens).

    Per-invocation negative control (reproducible strip): change
    ``_embed_reserved_usd = 2.0 * _per_item_sum`` in agent.py to ``1.0 *`` and the
    ``reserved == 2 * actual`` assertion below goes RED (reserved would equal
    actual). The 2x term is the fan-out buffer this asserts.
    """
    fake_mem = _FakeSemanticMemoryBackend(write_should_fail=False)
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    log_sink: list[dict] = []

    body = "A" * 300  # 100 tokens
    llm_resp = _fake_llm_response_with_capture("note-2x", body)
    _run_call(agent, llm_response=llm_resp, log_sink=log_sink)

    res = next(r for r in log_sink if r.get("trigger") == "embed_batch_reservation")
    rel = next(r for r in log_sink if r.get("trigger") == "embed_batch_release")

    reserved = res.get("reserved_usd", 0.0)
    actual = rel.get("actual_usd", 0.0)
    assert reserved == pytest.approx(2.0 * actual), (
        f"reserved {reserved} should be 2x the single-item actual {actual} "
        "(per-item + batch fan-out worst case); a 1x reservation under-reserves"
    )
    assert reserved > actual, "worst-case reservation must exceed actual spend"


# ──────────────────────────────────────────────────────────────────────────────
# Model-id resolution: from embedding_backend_resolved.model_id, NOT the label


def test_embed_model_id_resolves_from_resolved_backend_not_provider_label(tmp_path):
    """The pricing model id MUST come from
    capabilities().embedding_backend_resolved.model_id, NOT embedding_provider.

    Production contract (PgvectorMemoryBackend): embedding_provider is the
    provider LABEL ("openai") and the model id lives on the resolved backend.
    The audit record's `model` field must be the model id ("text-embedding-3-small")
    and cost_estimated must be False for a priced model.

    Per-invocation negative control: _ProviderLabelOnlyMemoryBackend returns the
    SAME provider label but NO resolved backend, so the gate falls back to the
    label as the pricing key — which is not in EMBEDDING_PRICING — yielding
    model="openai" and cost_estimated=True. That divergence proves the model id
    is genuinely read from the resolved backend, not the label (strip the
    resolved-backend read in agent.py and this test's first assertion goes RED).
    """
    body = "A" * 300  # 100 tokens

    # Resolved-backend path → real model id, exact pricing.
    agent_ok = _make_agent(
        tmp_path, memory_backend=_FakeSemanticMemoryBackend(), name="resolvedbot"
    )
    sink_ok: list[dict] = []
    _run_call(
        agent_ok,
        llm_response=_fake_llm_response_with_capture("note-r", body),
        log_sink=sink_ok,
    )
    rec_ok = next(r for r in sink_ok if r.get("trigger") == "embed_batch_release")
    assert rec_ok.get("model") == "text-embedding-3-small", (
        "model id must resolve from embedding_backend_resolved.model_id; "
        f"got {rec_ok.get('model')!r}"
    )
    assert rec_ok.get("cost_estimated") is False, (
        "priced model must not be flagged cost_estimated"
    )

    # Provider-label-only path (no resolved backend) → fallback estimate.
    agent_label = _make_agent(
        tmp_path,
        memory_backend=_ProviderLabelOnlyMemoryBackend(),
        name="labelbot",
    )
    sink_label: list[dict] = []
    _run_call(
        agent_label,
        llm_response=_fake_llm_response_with_capture("note-l", body),
        log_sink=sink_label,
    )
    rec_label = next(r for r in sink_label if r.get("trigger") == "embed_batch_release")
    assert rec_label.get("model") == "openai", (
        "with no resolved backend the gate falls back to the provider label as "
        f"the pricing key; got {rec_label.get('model')!r}"
    )
    assert rec_label.get("cost_estimated") is True, (
        "an unpriced provider label must flag cost_estimated=True (max-rate fallback)"
    )


def test_embed_token_estimate_uses_utf8_bytes_not_code_points(tmp_path):
    """The token estimate is math.ceil(utf8_bytes / 3), NOT len(str)/3.

    Multibyte scripts (CJK) are ≥1 BPE token per char and ≥3 UTF-8 bytes per char,
    so a code-point estimate under-reserves ~3x. The byte-based estimate must be
    >= the code-point count (a conservative real-token lower bound).

    Per-invocation negative control: a 100-CJK-char body has 300 UTF-8 bytes →
    ceil(300/3)=100-token estimate, vs the code-point estimate ceil(100/3)=34.
    The reserved cost (built on the byte estimate) MUST equal the cost of >=100
    tokens, NOT 34. Strip the `.encode("utf-8")` in agent.py (revert to code
    points) and this assertion goes RED.
    """
    from atomic_agents._costs import calc_embedding_cost

    cjk = "語" * 100  # 100 code points, 300 UTF-8 bytes
    assert len(cjk) == 100
    assert len(cjk.encode("utf-8")) == 300

    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    sink: list[dict] = []
    _run_call(
        agent,
        llm_response=_fake_llm_response_with_capture("note-cjk", cjk),
        log_sink=sink,
    )
    rel = next(r for r in sink if r.get("trigger") == "embed_batch_release")
    res = next(r for r in sink if r.get("trigger") == "embed_batch_reservation")
    actual = rel.get("actual_usd", 0.0)
    reserved = res.get("reserved_usd", 0.0)

    # Byte-based estimate: 300 bytes / 3 = 100 tokens.
    cost_byte_based, _ = calc_embedding_cost("text-embedding-3-small", 100)
    # Code-point estimate (the bug): 100 chars / 3 → 34 tokens.
    cost_code_point, _ = calc_embedding_cost("text-embedding-3-small", 34)

    # There are TWO independent encode sites in agent.py (the reservation loop and
    # the per-note true-up loop). Assert on BOTH outputs so a strip of EITHER
    # site's .encode("utf-8") is independently caught (per the lesson: strip each
    # independent part separately). actual_usd exercises the true-up loop;
    # reserved_usd (= 2x per_item_sum) exercises the reservation loop.
    assert actual == pytest.approx(cost_byte_based), (
        f"actual_usd {actual} must reflect the UTF-8-byte token estimate "
        f"({cost_byte_based}), not the code-point estimate ({cost_code_point}) — "
        "true-up-loop encode site"
    )
    assert reserved == pytest.approx(2.0 * cost_byte_based), (
        f"reserved_usd {reserved} must reflect 2x the UTF-8-byte estimate "
        f"({2.0 * cost_byte_based}), not 2x the code-point estimate "
        f"({2.0 * cost_code_point}) — reservation-loop encode site"
    )
    assert cost_byte_based > cost_code_point, (
        "byte-based estimate must exceed code-point estimate for multibyte text "
        "(otherwise the test topology is not load-bearing)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Enforcement: reserved > headroom raises (gate is a real cap, not audit-only)


def test_embed_gate_blocks_when_reservation_exceeds_headroom(tmp_path):
    """The embed gate RAISES CostGuardrailBlocked when the worst-case reservation
    exceeds remaining headroom — it is a real guardrail, not an audit-only log.

    A capped agent ($1.00 daily) whose chat spend is already $0.999999 has ~$1e-6
    headroom; a batch reserving more than that must be refused.

    Per-invocation negative control: test_embed_gate_passes_within_headroom proves
    the same batch is allowed when headroom is ample. Strip the headroom-enforce
    block in agent.py and this test stops raising.
    """
    from atomic_agents.exceptions import CostGuardrailBlocked
    from atomic_agents._costs import CostReadResult

    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem, daily_cap_usd=1.0)
    agent.config = dc_replace(
        agent.config, cost_guardrails_enabled=True, daily_cap_usd=1.0
    )
    agent._policy_snapshot_this_call = None

    # Reliable (non-degraded) read showing the agent is at $0.999999 of its $1 cap.
    near_cap = CostReadResult(total_usd=0.999999, degraded=False, dropped_records=0)

    body = "B" * 30000  # large body → reservation well above $1e-6 headroom
    llm_resp = _fake_llm_response_with_capture("note-big", body)

    with pytest.raises(CostGuardrailBlocked, match="exceeds remaining headroom"):
        with (
            patch("atomic_agents._llm.call_llm", return_value=llm_resp),
            patch.object(agent, "load"),
            patch.object(
                agent, "assemble_system_prompt", return_value="You are EmbedBot."
            ),
            patch.object(
                agent,
                "_check_cost_guardrails",
                return_value=MagicMock(
                    allow=True, action="ok", reason="cap", cost_data_degraded=False
                ),
            ),
            patch("atomic_agents._costs.sum_cost_for_period", return_value=near_cap),
        ):
            agent.call("embed test")


def test_embed_gate_passes_within_headroom(tmp_path):
    """Negative control for the enforcement block: the same capped agent with
    ample headroom completes without raising."""
    from atomic_agents._costs import CostReadResult

    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem, daily_cap_usd=1.0)
    agent.config = dc_replace(
        agent.config, cost_guardrails_enabled=True, daily_cap_usd=1.0
    )
    agent._policy_snapshot_this_call = None

    ample = CostReadResult(total_usd=0.0, degraded=False, dropped_records=0)
    body = "B" * 30000
    llm_resp = _fake_llm_response_with_capture("note-big", body)

    with (
        patch("atomic_agents._llm.call_llm", return_value=llm_resp),
        patch.object(agent, "load"),
        patch.object(agent, "assemble_system_prompt", return_value="You are EmbedBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=True, action="ok", reason="cap", cost_data_degraded=False
            ),
        ),
        patch("atomic_agents._costs.sum_cost_for_period", return_value=ample),
    ):
        resp = agent.call("embed test")
    assert resp is not None


def test_embed_block_writes_refusal_audit_record_with_run_id(tmp_path):
    """When the embed gate raises CostGuardrailBlocked, the call() except handler
    MUST write exactly one terminal refusal JSONL record carrying a run_id, the
    chat cost_usd already incurred this call, status=error, and the distinctive
    embed_batch_blocked=True marker — Principle #5 (a cost block is exactly the
    event that most needs an audit trail) + the #495/#497/#498 under-counting
    guard (the chat spend must not vanish from the ledger).

    Per-invocation negative control: strip the `elif isinstance(_call_exc,
    CostGuardrailBlocked)` branch in agent.py and this test goes RED — no record
    with embed_batch_blocked carries a run_id; the block leaves no JSONL line.
    The marker (not the trigger) is the branch-distinctive assertion per
    feedback_layered_except_typed_branch_false_green: the success record uses the
    same self.trigger, so only the marker + status=error distinguishes the refusal.
    """
    from atomic_agents.exceptions import CostGuardrailBlocked
    from atomic_agents._costs import CostReadResult

    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem, daily_cap_usd=1.0)
    agent.config = dc_replace(
        agent.config, cost_guardrails_enabled=True, daily_cap_usd=1.0
    )
    agent._policy_snapshot_this_call = None

    sink: list[dict] = []

    def fake_log(record: dict) -> None:
        sink.append(dict(record))

    # Reliable read at $0.999999 of a $1 cap → tiny headroom; large batch refused.
    near_cap = CostReadResult(total_usd=0.999999, degraded=False, dropped_records=0)
    body = "B" * 30000
    llm_resp = _fake_llm_response_with_capture("note-big", body)

    with pytest.raises(CostGuardrailBlocked, match="exceeds remaining headroom"):
        with (
            patch("atomic_agents._llm.call_llm", return_value=llm_resp),
            patch.object(agent, "_log", side_effect=fake_log),
            patch.object(agent, "load"),
            patch.object(
                agent, "assemble_system_prompt", return_value="You are EmbedBot."
            ),
            patch.object(
                agent,
                "_check_cost_guardrails",
                return_value=MagicMock(
                    allow=True, action="ok", reason="cap", cost_data_degraded=False
                ),
            ),
            patch("atomic_agents._costs.sum_cost_for_period", return_value=near_cap),
        ):
            agent.call("embed test")

    blocked = [r for r in sink if r.get("embed_batch_blocked") is True]
    assert len(blocked) == 1, (
        "expected exactly one embed_batch_blocked refusal audit record after the "
        f"gate raised; got {[r.get('trigger') for r in sink]}"
    )
    rec = blocked[0]
    assert rec.get("run_id") == agent.run_id, "refusal record must carry the run_id"
    assert rec.get("status") == "error"
    assert "cost_usd" in rec, (
        "refusal record MUST carry the chat cost_usd so the spend already incurred "
        "this call lands in the ledger (#495/#497/#498 under-counting guard)"
    )
    assert rec.get("cost_source") == "actor"
    # No success-path terminal record (status=ok) is written — the call did not
    # complete. The only terminal record is the refusal.
    ok_records = [r for r in sink if r.get("status") == "ok" and "run_id" in r]
    assert not ok_records, (
        "a blocked call must not also write a status=ok terminal run record"
    )


def test_embed_actual_usd_not_folded_into_cost_usd_this_pr(tmp_path):
    """Embed records are audit-only this PR: they carry reserved_usd/actual_usd,
    NOT a `cost_usd` field, so embed spend does not enter sum_cost_for_period's
    running total (which aggregates only `cost_usd`). Cross-call embed accounting
    is deferred to #544 PR2; this asserts the documented current behavior so it is
    not silently assumed enforced.
    """
    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    sink: list[dict] = []
    _run_call(
        agent,
        llm_response=_fake_llm_response_with_capture("note-1", "A" * 300),
        log_sink=sink,
    )
    embed_records = [
        r
        for r in sink
        if r.get("trigger") in {"embed_batch_reservation", "embed_batch_release"}
    ]
    assert embed_records, "expected embed audit records"
    for rec in embed_records:
        assert "cost_usd" not in rec, (
            "embed records must NOT carry cost_usd this PR (audit-only; embed "
            "spend is not yet folded into the cross-call cost total — #544 PR2)"
        )


# ──────────────────────────────────────────────────────────────────────────────
# _has_effective_embed_cap


def test_has_effective_embed_cap_with_model_daily_cap(tmp_path):
    """_has_effective_embed_cap() returns True when model.md daily cap is set."""
    agent = _make_agent(tmp_path, daily_cap_usd=1.0)
    # Simulate cost_guardrails_enabled (default True)
    agent.config = dc_replace(
        agent.config, cost_guardrails_enabled=True, daily_cap_usd=1.0
    )
    result = agent._has_effective_embed_cap(parent_remaining_headroom_usd=None)
    assert result is True


def test_has_effective_embed_cap_no_cap_returns_false(tmp_path):
    """_has_effective_embed_cap() returns False when no cap at any layer.

    Negative control for the fail-closed predicate: when this returns False,
    a degraded read MUST NOT block the agent (no budget to protect).
    """
    agent = _make_agent(tmp_path)
    agent.config = dc_replace(
        agent.config,
        cost_guardrails_enabled=True,
        daily_cap_usd=0.0,
        monthly_cap_usd=0.0,
    )
    # No policy snapshot either
    agent._policy_snapshot_this_call = None
    result = agent._has_effective_embed_cap(parent_remaining_headroom_usd=None)
    assert result is False


def test_has_effective_embed_cap_with_tree_cap(tmp_path):
    """_has_effective_embed_cap() returns True when parent_remaining_headroom_usd is set."""
    agent = _make_agent(tmp_path)
    agent.config = dc_replace(
        agent.config,
        cost_guardrails_enabled=True,
        daily_cap_usd=0.0,
        monthly_cap_usd=0.0,
    )
    agent._policy_snapshot_this_call = None
    result = agent._has_effective_embed_cap(parent_remaining_headroom_usd=5.0)
    assert result is True


def test_has_effective_embed_cap_guardrails_disabled(tmp_path):
    """_has_effective_embed_cap() returns False when cost_guardrails_enabled=False."""
    agent = _make_agent(tmp_path, daily_cap_usd=1.0)
    agent.config = dc_replace(
        agent.config,
        cost_guardrails_enabled=False,
        daily_cap_usd=1.0,
    )
    result = agent._has_effective_embed_cap(parent_remaining_headroom_usd=None)
    assert result is False


def test_has_effective_embed_cap_with_policy_only_cap(tmp_path):
    """_has_effective_embed_cap() returns True when the ONLY cap is a
    Policy-composed cap (no model.md cap, no tree-cap).

    Covers the effective-cap branch that reads
    _policy_snapshot_this_call.effective_caps (#512 lesson: gate on the EFFECTIVE
    control, not one source). Without this, a Policy-only-capped agent would be
    wrongly treated as uncapped by the fail-closed predicate.

    Negative control: effective_caps all-None with no model.md cap and no tree-cap
    returns False (the no-cap case).
    """
    from atomic_agents.policy.types import CostCaps

    agent = _make_agent(tmp_path)
    agent.config = dc_replace(
        agent.config,
        cost_guardrails_enabled=True,
        daily_cap_usd=0.0,
        monthly_cap_usd=0.0,
    )

    # Policy snapshot with a daily cap only.
    agent._policy_snapshot_this_call = MagicMock(
        effective_caps=CostCaps(daily_usd=2.5, monthly_usd=None)
    )
    assert agent._has_effective_embed_cap(parent_remaining_headroom_usd=None) is True, (
        "a Policy-composed daily cap must register as an effective cap"
    )

    # Negative control: no Policy cap either → no effective cap.
    agent._policy_snapshot_this_call = MagicMock(
        effective_caps=CostCaps(daily_usd=None, monthly_usd=None)
    )
    assert (
        agent._has_effective_embed_cap(parent_remaining_headroom_usd=None) is False
    ), "all-None Policy caps with no model.md cap must NOT register an effective cap"


# ──────────────────────────────────────────────────────────────────────────────
# Fail-closed gate: degraded AND effective cap = block


def test_fail_closed_blocks_when_degraded_and_has_cap(tmp_path):
    """Embed gate raises CostGuardrailBlocked when cost log is degraded AND
    the agent has an effective cap."""
    from atomic_agents.exceptions import CostGuardrailBlocked
    from atomic_agents._costs import CostReadResult

    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem, daily_cap_usd=1.0)
    agent.config = dc_replace(
        agent.config, cost_guardrails_enabled=True, daily_cap_usd=1.0
    )
    agent._policy_snapshot_this_call = None

    llm_resp = _fake_llm_response_with_capture("note-1", "fail-closed test")

    # Make sum_cost_for_period return a degraded CostReadResult
    degraded_result = CostReadResult(total_usd=0.0, degraded=True, dropped_records=5)

    with pytest.raises(CostGuardrailBlocked, match="embed batch blocked"):
        with (
            patch("atomic_agents._llm.call_llm", return_value=llm_resp),
            patch.object(agent, "load"),
            patch.object(
                agent, "assemble_system_prompt", return_value="You are EmbedBot."
            ),
            patch.object(
                agent,
                "_check_cost_guardrails",
                return_value=MagicMock(
                    allow=True, action="ok", reason="cap", cost_data_degraded=False
                ),
            ),
            patch(
                "atomic_agents._costs.sum_cost_for_period", return_value=degraded_result
            ),
        ):
            agent.call("embed test")


def test_fail_closed_passes_when_degraded_and_no_cap(tmp_path):
    """Embed gate MUST NOT block when cost log is degraded but agent has no cap.

    This is the critical anti-spurious-block invariant from
    feedback_fail_closed_only_where_theres_something_to_protect: a degraded
    read cannot block an uncapped agent (no budget to protect).

    Negative control: the block fires when daily_cap_usd > 0 (proven in
    test_fail_closed_blocks_when_degraded_and_has_cap).
    """
    from atomic_agents._costs import CostReadResult

    fake_mem = _FakeSemanticMemoryBackend()
    agent = _make_agent(tmp_path, memory_backend=fake_mem)
    agent.config = dc_replace(
        agent.config,
        cost_guardrails_enabled=True,
        daily_cap_usd=0.0,
        monthly_cap_usd=0.0,
    )
    agent._policy_snapshot_this_call = None

    llm_resp = _fake_llm_response_with_capture("note-1", "no cap test")

    degraded_result = CostReadResult(total_usd=0.0, degraded=True, dropped_records=5)

    # Should complete without raising (no cap to protect)
    with (
        patch("atomic_agents._llm.call_llm", return_value=llm_resp),
        patch.object(agent, "load"),
        patch.object(agent, "assemble_system_prompt", return_value="You are EmbedBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=True, action="ok", reason="cap", cost_data_degraded=False
            ),
        ),
        patch("atomic_agents._costs.sum_cost_for_period", return_value=degraded_result),
    ):
        resp = agent.call("embed test")
    # No exception = pass
    assert resp is not None


# ──────────────────────────────────────────────────────────────────────────────
# Token estimation: chars/3 ceiling formula


def test_token_estimate_ceiling():
    """math.ceil(len(text) / 3) gives ceiling integer tokens from char length."""
    assert math.ceil(9 / 3) == 3
    assert math.ceil(10 / 3) == 4  # 10 chars → ceil(3.33) = 4
    assert math.ceil(11 / 3) == 4  # 11 chars → ceil(3.67) = 4
    assert math.ceil(12 / 3) == 4  # 12 chars → ceil(4.0) = 4
    assert math.ceil(0 / 3) == 0  # empty body → 0 tokens
    assert math.ceil(1 / 3) == 1  # 1 char → ceil(0.33) = 1


# ──────────────────────────────────────────────────────────────────────────────
# check_embedding_backend() doctor check


def test_doctor_embedding_backend_skip_when_unset(monkeypatch):
    """Returns SKIP when ATOMIC_AGENTS_EMBEDDING_BACKEND is not set."""
    from atomic_agents.doctor import check_embedding_backend, SKIP

    monkeypatch.delenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", raising=False)
    result = check_embedding_backend()
    assert result.status == SKIP
    assert result.name == "embedding-backend"


def test_doctor_embedding_backend_skip_when_blank(monkeypatch):
    """Returns SKIP when ATOMIC_AGENTS_EMBEDDING_BACKEND is set but blank."""
    from atomic_agents.doctor import check_embedding_backend, SKIP

    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "  ")
    result = check_embedding_backend()
    assert result.status == SKIP


def test_doctor_embedding_backend_fail_when_unknown_provider(monkeypatch):
    """Returns FAIL when ATOMIC_AGENTS_EMBEDDING_BACKEND is an unknown provider id."""
    from atomic_agents.doctor import check_embedding_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "not-a-real-provider")
    # Patch get_default_embedding_backend to raise BackendNotRegistered
    from atomic_agents.exceptions import BackendNotRegistered

    with patch(
        "atomic_agents.doctor.check_embedding_backend.__module__",
        create=True,
    ):
        pass
    with patch(
        "atomic_agents.embedding.registry.get_default_embedding_backend",
        side_effect=BackendNotRegistered("not-a-real-provider not found"),
    ):
        result = check_embedding_backend()
    assert result.status == FAIL
    assert result.name == "embedding-backend"
    assert "not-a-real-provider" in result.message


def test_doctor_embedding_backend_warn_when_api_key_missing(monkeypatch):
    """Returns WARN when backend constructs but _api_key is None (key missing).

    Negative control: check_embedding_backend() returns PASS when _api_key is
    not None (proven in test_doctor_embedding_backend_pass_when_key_present).
    """
    from atomic_agents.doctor import check_embedding_backend, WARN

    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")

    from atomic_agents.embedding.backend import EmbeddingCapabilities

    class _KeylessFakeBackend:
        # No backend_id: the real EmbeddingBackend / OpenAIEmbeddingBackend
        # surface exposes provider_id / model_id / dimensions, NOT backend_id.
        # Carrying a phantom backend_id here false-greens the WARN branch (which
        # builds its message from provider_id) — removing it makes this a real
        # negative control for the doctor.py:5208 message string.
        provider_id = "openai"
        model_id = "text-embedding-3-small"
        dimensions = 1536
        _api_key = None  # key absent

        def capabilities(self):
            return EmbeddingCapabilities(
                max_batch_size=2048,
                max_input_tokens=8192,
                supports_input_type=False,
            )

    with patch(
        "atomic_agents.embedding.registry.get_default_embedding_backend",
        return_value=_KeylessFakeBackend(),
    ):
        result = check_embedding_backend()

    assert result.status == WARN
    assert result.name == "embedding-backend"
    assert "no API key" in result.message or "api_key" in result.message.lower()


def test_doctor_embedding_backend_pass_when_key_present(monkeypatch):
    """Returns PASS when backend constructs successfully with a resolved key.

    Negative control: returns WARN when _api_key=None (proven in
    test_doctor_embedding_backend_warn_when_api_key_missing).
    """
    from atomic_agents.doctor import check_embedding_backend, PASS

    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")

    from atomic_agents.embedding.backend import EmbeddingCapabilities

    class _ReadyFakeBackend:
        # No backend_id: mirrors the real EmbeddingBackend surface (provider_id /
        # model_id / dimensions). See _KeylessFakeBackend rationale.
        provider_id = "openai"
        model_id = "text-embedding-3-small"
        dimensions = 1536
        _api_key = "sk-test"  # key present

        def capabilities(self):
            return EmbeddingCapabilities(
                max_batch_size=2048,
                max_input_tokens=8192,
                supports_input_type=False,
            )

    with patch(
        "atomic_agents.embedding.registry.get_default_embedding_backend",
        return_value=_ReadyFakeBackend(),
    ):
        result = check_embedding_backend()

    assert result.status == PASS
    assert result.name == "embedding-backend"
    assert "text-embedding-3-small" in result.message


def test_doctor_embedding_backend_pass_without_api_key_attr_is_not_unresolved(
    monkeypatch,
):
    """A backend WITHOUT a _api_key attribute (non-OpenAI EmbeddingBackend) PASSes
    and MUST NOT be labeled key-not-resolved.

    The tri-state probe reports api_key_probe='n/a' (key resolution does not apply),
    never api_key_resolved=False, which would misrepresent a fully-usable backend.
    """
    from atomic_agents.doctor import check_embedding_backend, PASS
    from atomic_agents.embedding.backend import EmbeddingCapabilities

    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "local")

    class _NoKeyAttrBackend:
        # No backend_id: mirrors the real EmbeddingBackend surface.
        provider_id = "local"
        model_id = "all-MiniLM-L6-v2"
        dimensions = 384
        # No _api_key attribute at all.

        def capabilities(self):
            return EmbeddingCapabilities(
                max_batch_size=64,
                max_input_tokens=512,
                supports_input_type=False,
            )

    with patch(
        "atomic_agents.embedding.registry.get_default_embedding_backend",
        return_value=_NoKeyAttrBackend(),
    ):
        result = check_embedding_backend()

    assert result.status == PASS
    assert result.detail.get("api_key_probe") == "n/a", (
        "a backend without _api_key must report api_key_probe='n/a', not a "
        "misleading key-unresolved signal"
    )
    assert result.detail.get("api_key_resolved") is None, (
        "the misleading api_key_resolved=False detail must be gone"
    )


def test_doctor_embedding_backend_fail_on_embedding_error(monkeypatch):
    """Returns FAIL when backend raises EmbeddingError during construction."""
    from atomic_agents.doctor import check_embedding_backend, FAIL
    from atomic_agents.exceptions import EmbeddingError

    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")

    with patch(
        "atomic_agents.embedding.registry.get_default_embedding_backend",
        side_effect=EmbeddingError("bad dimensions"),
    ):
        result = check_embedding_backend()

    assert result.status == FAIL
    assert result.name == "embedding-backend"
    assert "EmbeddingError" in result.message or "bad dimensions" in result.message


def test_doctor_embedding_backend_fail_on_import_error(monkeypatch):
    """Returns FAIL when the openai extra is not installed (ImportError)."""
    from atomic_agents.doctor import check_embedding_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_EMBEDDING_BACKEND", "openai")

    with patch(
        "atomic_agents.embedding.registry.get_default_embedding_backend",
        side_effect=ImportError("openai not installed"),
    ):
        result = check_embedding_backend()

    assert result.status == FAIL
    assert result.name == "embedding-backend"


def test_doctor_run_includes_embedding_backend_check():
    """run_doctor() result list includes the 'embedding-backend' check.

    No --agent → returns SKIP. Negative control: the name is not omitted.
    """
    from atomic_agents.doctor import run_doctor

    results = run_doctor()
    names = [r.name for r in results]
    assert "embedding-backend" in names, (
        "embedding-backend check missing from run_doctor() output; "
        "check that check_embedding_backend() is appended in run_doctor()"
    )


def test_doctor_run_agent_includes_embedding_backend_check(tmp_path):
    """run_doctor(agent_name=...) result list includes 'embedding-backend' check."""
    from atomic_agents.doctor import run_doctor

    # Build a minimal agent root so run_doctor() can proceed
    name = "embedbot"
    agents_root = _build_agent_root(tmp_path, name)

    results = run_doctor(agent_name=name, agents_root=agents_root)
    names = [r.name for r in results]
    assert "embedding-backend" in names, (
        "embedding-backend check missing from run_doctor(agent_name=...) output"
    )

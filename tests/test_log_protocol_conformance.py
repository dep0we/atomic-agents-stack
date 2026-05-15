"""Conformance test suite for the LogBackend Protocol (spec/22).

Parametrized over a ``backend_factory`` fixture. Each registered backend
that ships in core (``FilesystemLogBackend`` today; PR 3 of #61 adds
``SQLiteLogBackend``) is exercised against the same contract. A
third-party backend in a downstream package imports this test module's
``backend_factory`` parametrization to verify its own conformance.

What this suite asserts:

1. Protocol surface — ``isinstance(backend, LogBackend)`` passes; all
   required attributes/methods are present.
2. ``backend_id`` is a stable non-empty string.
3. ``append`` persists a record retrievable via ``tail(1)``.
4. ``append`` preserves primitive-specific keys via ``extra``.
5. ``append`` is NOT idempotent — two identical appends produce two records.
6. ``append`` does not mutate the input record.
7. ``query`` on empty backend returns ``[]``.
8. ``query`` filters by ``run_id``.
9. ``query`` filters by ``primitive`` (single string).
10. ``query`` filters by ``primitive`` (tuple of acceptable values).
11. ``query`` filters by ``status``.
12. ``query`` filters by ``since/until`` window.
13. ``query`` filters by ``cost_source`` (spec/28+30 path).
14. ``query`` filters by ``mandate_id`` (spec/29 path).
15. ``query`` filters by ``parent_run_id`` (rollup path).
16. ``query`` honors ``limit`` AFTER sort.
17. ``query`` returns chronological order.
18. ``tail`` returns last n in chronological-LAST order.
19. ``tail(0)`` returns ``[]``.
20. ``tail`` past the total returns all records.
21. ``tail`` raises on negative ``n``.
22. ``aggregate`` counts grouped by primitive.
23. ``aggregate`` sums cost_usd grouped by model.
24. ``aggregate`` raises ``ValueError`` for unknown metrics.
25. ``aggregate`` avg_latency_ms returns None for all-None bucket.
26. ``aggregate`` with empty group_by returns single-entry dict.
27. ``delete_older_than`` removes old records and returns count.
28. ``delete_older_than`` is idempotent.
29. ``stats`` reflects appended records.
30. ``capabilities`` returns a LogCapabilities instance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from atomic_agents.logs import (
    FilesystemLogBackend,
    LogAggregate,
    LogBackend,
    LogCapabilities,
    LogQuery,
    RunRecord,
)


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization
#
# PR 3 of #61 appends ``("sqlite", _sqlite_factory)`` to this list.
# Until then, only filesystem participates — but the parametrization
# scaffolding is already in place so PR 3's wiring is a one-line edit.

BackendFactory = Callable[[Path], LogBackend]


def _filesystem_factory(scope_root: Path) -> LogBackend:
    return FilesystemLogBackend(scope_root)


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
]


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend_factory(request) -> BackendFactory:
    """Yields a callable ``(scope_root: Path) -> LogBackend``."""
    return request.param[1]


@pytest.fixture
def backend(backend_factory, tmp_path) -> LogBackend:
    """A backend rooted at a per-test tmp_path."""
    return backend_factory(tmp_path)


# ──────────────────────────────────────────────────────────────────
# Helpers for record construction


def _make_record(
    *,
    ts: str | None = None,
    run_id: str = "run-abc",
    primitive: str = "agent_call",
    status: str = "ok",
    summary: str = "test",
    model: str = "claude-opus-4-7",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cost_usd: float | None = 0.001,
    cost_source: str | None = None,
    latency_ms: float | None = 1234.5,
    cache_hit_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    mandate_id: str | None = None,
    parent_run_id: str | None = None,
    parent_agent: str | None = None,
    trigger: str | None = None,
    agent_name: str | None = None,
    fallback: bool | None = None,
    critical: bool | None = None,
    extra: dict | None = None,
) -> RunRecord:
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return RunRecord(
        ts=ts,
        run_id=run_id,
        primitive=primitive,
        status=status,
        summary=summary,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        cost_source=cost_source,
        latency_ms=latency_ms,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        mandate_id=mandate_id,
        parent_run_id=parent_run_id,
        parent_agent=parent_agent,
        trigger=trigger,
        agent_name=agent_name,
        fallback=fallback,
        critical=critical,
        extra=extra or {},
    )


def _ts_at(year: int, month: int, day: int, hour: int = 12) -> str:
    """ISO-8601 UTC string at a specific moment."""
    return datetime(year, month, day, hour, tzinfo=timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────
# Surface conformance


def test_protocol_isinstance(backend):
    """isinstance check passes — backend exposes the full Protocol."""
    assert isinstance(backend, LogBackend)


def test_backend_id_is_stable_nonempty_string(backend):
    backend_id = backend.backend_id
    assert isinstance(backend_id, str)
    assert backend_id != ""
    assert backend.backend_id == backend_id


# ──────────────────────────────────────────────────────────────────
# Append


def test_append_persists_record(backend):
    rec = _make_record(run_id="appended")
    backend.append(rec)
    out = backend.tail(1)
    assert len(out) == 1
    assert out[0].run_id == "appended"


def test_append_preserves_extra_fields(backend):
    rec = _make_record(
        extra={
            "iteration": 3,
            "tool_calls": [{"tool_name": "search", "latency_ms": 42}],
            "helper_provenance": [{"helper_name": "summarize"}],
        },
    )
    backend.append(rec)
    out = backend.tail(1)
    assert out[0].extra["iteration"] == 3
    assert out[0].extra["tool_calls"][0]["tool_name"] == "search"
    assert out[0].extra["helper_provenance"][0]["helper_name"] == "summarize"


def test_append_does_not_dedup(backend):
    """Two identical appends MUST produce two records — dedup is the caller's job."""
    rec = _make_record(ts=_ts_at(2026, 5, 15, 10))
    backend.append(rec)
    backend.append(rec)
    assert len(backend.tail(10)) == 2


def test_append_does_not_mutate_record(backend):
    rec = _make_record(extra={"original_key": "value"})
    snapshot_extra = dict(rec.extra)
    backend.append(rec)
    assert rec.extra == snapshot_extra
    assert "original_key" in rec.extra


# ──────────────────────────────────────────────────────────────────
# Query — empty backend


def test_query_empty_backend_returns_empty_list(backend):
    assert backend.query(LogQuery()) == []


# ──────────────────────────────────────────────────────────────────
# Query — filters


def test_query_filters_by_run_id(backend):
    backend.append(_make_record(run_id="a", ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(run_id="b", ts=_ts_at(2026, 5, 15, 11)))
    out = backend.query(LogQuery(run_id="b"))
    assert len(out) == 1
    assert out[0].run_id == "b"


def test_query_filters_by_primitive_single(backend):
    backend.append(_make_record(primitive="agent_call", ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(primitive="helper", ts=_ts_at(2026, 5, 15, 11)))
    out = backend.query(LogQuery(primitive="agent_call"))
    assert len(out) == 1
    assert out[0].primitive == "agent_call"


def test_query_filters_by_primitive_tuple(backend):
    backend.append(_make_record(primitive="agent_call", ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(primitive="helper", ts=_ts_at(2026, 5, 15, 11)))
    backend.append(_make_record(primitive="delegate", ts=_ts_at(2026, 5, 15, 12)))
    out = backend.query(LogQuery(primitive=("helper", "delegate")))
    assert {r.primitive for r in out} == {"helper", "delegate"}


def test_query_filters_by_status(backend):
    backend.append(_make_record(status="ok", ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(status="error", ts=_ts_at(2026, 5, 15, 11)))
    out = backend.query(LogQuery(status="error"))
    assert len(out) == 1
    assert out[0].status == "error"


def test_query_filters_by_since_until_window(backend):
    backend.append(_make_record(ts=_ts_at(2026, 1, 1, 12), run_id="jan"))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15, 12), run_id="may"))
    backend.append(_make_record(ts=_ts_at(2026, 12, 31, 12), run_id="dec"))
    out = backend.query(
        LogQuery(
            since=datetime(2026, 4, 1, tzinfo=timezone.utc),
            until=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
    )
    assert len(out) == 1
    assert out[0].run_id == "may"


def test_query_filters_by_cost_source(backend):
    """Legacy records without cost_source count as 'actor' for backward compat."""
    backend.append(_make_record(cost_source="actor", ts=_ts_at(2026, 5, 15, 10), run_id="actor1"))
    backend.append(_make_record(cost_source="judge", ts=_ts_at(2026, 5, 15, 11), run_id="judge1"))
    backend.append(_make_record(cost_source=None, ts=_ts_at(2026, 5, 15, 12), run_id="legacy"))
    out_actor = backend.query(LogQuery(cost_source="actor"))
    assert {r.run_id for r in out_actor} == {"actor1", "legacy"}
    out_judge = backend.query(LogQuery(cost_source="judge"))
    assert {r.run_id for r in out_judge} == {"judge1"}


def test_query_filters_by_mandate_id(backend):
    backend.append(_make_record(mandate_id="m-1", ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(mandate_id="m-2", ts=_ts_at(2026, 5, 15, 11)))
    backend.append(_make_record(mandate_id=None, ts=_ts_at(2026, 5, 15, 12)))
    out = backend.query(LogQuery(mandate_id="m-1"))
    assert len(out) == 1
    assert out[0].mandate_id == "m-1"


def test_query_filters_by_parent_run_id(backend):
    backend.append(_make_record(parent_run_id="parent-x", run_id="child-1", ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(parent_run_id="parent-x", run_id="child-2", ts=_ts_at(2026, 5, 15, 11)))
    backend.append(_make_record(parent_run_id="parent-y", run_id="child-3", ts=_ts_at(2026, 5, 15, 12)))
    out = backend.query(LogQuery(parent_run_id="parent-x"))
    assert {r.run_id for r in out} == {"child-1", "child-2"}


def test_query_respects_limit(backend):
    for i in range(5):
        backend.append(_make_record(ts=_ts_at(2026, 5, 15, i), run_id=f"r{i}"))
    out = backend.query(LogQuery(limit=2))
    assert len(out) == 2
    # Limit applied AFTER sort — oldest two.
    assert [r.run_id for r in out] == ["r0", "r1"]


def test_query_returns_chronological_order(backend):
    # Append in shuffled order.
    backend.append(_make_record(ts=_ts_at(2026, 5, 15, 11), run_id="b"))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15, 9), run_id="a"))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15, 13), run_id="c"))
    out = backend.query(LogQuery())
    assert [r.run_id for r in out] == ["a", "b", "c"]


# ──────────────────────────────────────────────────────────────────
# Tail


def test_tail_returns_last_n_chronological(backend):
    """tail(n) returns oldest-first, newest-last — pairs with sort."""
    for i in range(5):
        backend.append(_make_record(ts=_ts_at(2026, 5, 15, i), run_id=f"r{i}"))
    out = backend.tail(3)
    assert [r.run_id for r in out] == ["r2", "r3", "r4"]


def test_tail_zero_returns_empty(backend):
    backend.append(_make_record())
    assert backend.tail(0) == []


def test_tail_more_than_total_returns_all(backend):
    for i in range(3):
        backend.append(_make_record(ts=_ts_at(2026, 5, 15, i), run_id=f"r{i}"))
    out = backend.tail(100)
    assert len(out) == 3


def test_tail_negative_raises_value_error(backend):
    with pytest.raises(ValueError):
        backend.tail(-1)


# ──────────────────────────────────────────────────────────────────
# Aggregate


def test_aggregate_count_by_primitive(backend):
    backend.append(_make_record(primitive="agent_call", ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(primitive="agent_call", ts=_ts_at(2026, 5, 15, 11)))
    backend.append(_make_record(primitive="helper", ts=_ts_at(2026, 5, 15, 12)))
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("primitive",), metric="count"),
    )
    assert result == {("agent_call",): 2, ("helper",): 1}


def test_aggregate_sum_cost_by_model(backend):
    backend.append(_make_record(
        model="claude-opus-4-7", cost_usd=0.5, ts=_ts_at(2026, 5, 15, 10),
    ))
    backend.append(_make_record(
        model="claude-opus-4-7", cost_usd=1.0, ts=_ts_at(2026, 5, 15, 11),
    ))
    backend.append(_make_record(
        model="claude-haiku-4-5", cost_usd=0.01, ts=_ts_at(2026, 5, 15, 12),
    ))
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("model",), metric="sum_cost_usd"),
    )
    # Float equality with a tolerance — guards against fp noise.
    assert result[("claude-opus-4-7",)] == pytest.approx(1.5)
    assert result[("claude-haiku-4-5",)] == pytest.approx(0.01)


def test_aggregate_unknown_metric_raises_value_error(backend):
    backend.append(_make_record())
    with pytest.raises(ValueError, match="Unknown aggregate metric"):
        backend.aggregate(
            LogQuery(),
            LogAggregate(group_by=("primitive",), metric="bogus_metric"),
        )


def test_aggregate_avg_latency_handles_none_bucket(backend):
    """avg_latency_ms over all-None bucket returns None, not 0.0 or crash."""
    backend.append(_make_record(
        primitive="cost_warning", latency_ms=None, ts=_ts_at(2026, 5, 15, 10),
    ))
    backend.append(_make_record(
        primitive="cost_warning", latency_ms=None, ts=_ts_at(2026, 5, 15, 11),
    ))
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("primitive",), metric="avg_latency_ms"),
    )
    assert result[("cost_warning",)] is None


def test_aggregate_empty_group_by_returns_single_entry(backend):
    """group_by=() aggregates over everything; result keyed by empty tuple."""
    backend.append(_make_record(cost_usd=1.0, ts=_ts_at(2026, 5, 15, 10)))
    backend.append(_make_record(cost_usd=2.0, ts=_ts_at(2026, 5, 15, 11)))
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=(), metric="sum_cost_usd"),
    )
    assert result == {(): pytest.approx(3.0)}


# ──────────────────────────────────────────────────────────────────
# Retention


def test_delete_older_than_removes_old_records(backend):
    backend.append(_make_record(ts=_ts_at(2026, 1, 15), run_id="jan"))
    backend.append(_make_record(ts=_ts_at(2026, 3, 15), run_id="mar"))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15), run_id="may"))
    deleted = backend.delete_older_than(datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert deleted == 2
    remaining = backend.query(LogQuery())
    assert {r.run_id for r in remaining} == {"may"}


def test_delete_older_than_idempotent(backend):
    backend.append(_make_record(ts=_ts_at(2026, 1, 15), run_id="jan"))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15), run_id="may"))
    threshold = datetime(2026, 4, 1, tzinfo=timezone.utc)
    first = backend.delete_older_than(threshold)
    second = backend.delete_older_than(threshold)
    assert first == 1
    assert second == 0


# ──────────────────────────────────────────────────────────────────
# Stats


def test_stats_reflects_appended_records(backend):
    backend.append(_make_record(ts=_ts_at(2026, 1, 15), run_id="jan"))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15), run_id="may"))
    s = backend.stats()
    assert s.total_records == 2
    assert s.oldest_ts is not None
    assert s.newest_ts is not None
    assert s.oldest_ts < s.newest_ts


# ──────────────────────────────────────────────────────────────────
# Capabilities


def test_capabilities_returns_logcapabilities(backend):
    caps = backend.capabilities()
    assert isinstance(caps, LogCapabilities)
    assert isinstance(caps.supports_aggregation_pushdown, bool)
    assert isinstance(caps.supports_streaming, bool)
    assert isinstance(caps.supports_retention, bool)
    assert isinstance(caps.durable, bool)

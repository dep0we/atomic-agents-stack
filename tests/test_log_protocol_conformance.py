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

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import pytest

from atomic_agents import LogBackendReadError
from atomic_agents.exceptions import AtomicAgentsError
from atomic_agents.logs import (
    FilesystemLogBackend,
    LogAggregate,
    LogBackend,
    LogCapabilities,
    LogQuery,
    RunRecord,
    SQLiteLogBackend,
)


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization — every conformance test runs once
# per registered backend. Both reference impls today (#61 PR 3 added
# the sqlite factory); third-party backends import this module's
# BACKEND_FACTORIES list to verify their own conformance.

BackendFactory = Callable[[Path], LogBackend]


def _filesystem_factory(scope_root: Path) -> LogBackend:
    return FilesystemLogBackend(scope_root)


def _sqlite_factory(scope_root: Path) -> LogBackend:
    # Per-test fresh database file — isolation via the per-test
    # tmp_path fixture (matches the FilesystemLogBackend approach).
    return SQLiteLogBackend(scope_root / "logs.db")


# Postgres factory — conditional on ATOMIC_AGENTS_TEST_POSTGRES_URL.
# When the env var is set (CI service container), the conformance suite
# runs against real Postgres. Without it, the Postgres factory is absent
# from BACKEND_FACTORIES and tests skip (for local dev without Postgres).
# The mock-cursor tests in test_log_postgres_backend.py are separate and
# labeled NON-CONFORMANCE — they must NOT appear here.
_POSTGRES_URL = os.environ.get("ATOMIC_AGENTS_TEST_POSTGRES_URL")
_POSTGRES_AVAILABLE = False

if _POSTGRES_URL:
    try:
        import psycopg as _psycopg  # noqa: F401

        _POSTGRES_AVAILABLE = True
    except ImportError:
        pass


def _postgres_factory(scope_root: Path) -> LogBackend:
    """Real Postgres conformance factory — only added to BACKEND_FACTORIES when
    ATOMIC_AGENTS_TEST_POSTGRES_URL is set and psycopg is installed.

    The factory returns a backend pointing at the shared Postgres database.
    Per-test isolation is handled by the ``postgres_truncate`` autouse fixture
    below, which TRUNCATEs run_records (RESTART IDENTITY) before each
    Postgres-parametrized test; meta is preserved so every test exercises the
    warm schema_version validation read path in _ensure_schema().  The
    ``backend`` fixture also calls backend.close() on teardown to release the
    server-side connection after each test.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    assert _POSTGRES_URL is not None
    return PostgresLogBackend(_POSTGRES_URL)


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
    ("sqlite", _sqlite_factory),
]

if _POSTGRES_AVAILABLE:
    BACKEND_FACTORIES.append(("postgres", _postgres_factory))


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend_factory(request) -> BackendFactory:
    """Yields a callable ``(scope_root: Path) -> LogBackend``."""
    return request.param[1]


@pytest.fixture(autouse=True)
def postgres_truncate(request):
    """Truncate run_records (RESTART IDENTITY) before each Postgres conformance test.

    Filesystem and SQLite backends get per-test isolation for free via a
    fresh tmp_path each time.  Postgres points at a shared database, so
    rows accumulate across tests without explicit cleanup.  This fixture
    TRUNCATEs run_records (RESTART IDENTITY to reset the BIGSERIAL counter —
    keeps same-ts insertion-order tests deterministic) before each test that
    uses the Postgres factory.

    meta is intentionally NOT truncated: the schema_version row must persist
    across tests so every test exercises the warm version-validation read path
    in _ensure_schema(), not just the cold-start INSERT path.  Truncating meta
    would silently route every test through INSERT ON CONFLICT DO NOTHING and
    never test the SELECT/validate branch — leaving the schema-mismatch
    refusal path (postgres.py:_ensure_schema version check) unverified against
    real Postgres.

    autouse=True — runs for every test in this module.  The guard
    ``_is_postgres`` skips the TRUNCATE for filesystem and SQLite tests so
    there is zero overhead for those backends.

    IMPORTANT: this fixture pulls ``backend_factory`` LAZILY via
    ``request.getfixturevalue`` only for tests that actually request it
    (directly or transitively through the ``backend`` fixture). Declaring
    ``backend_factory`` as a direct parameter would force its
    parametrization onto EVERY test in the module — including the
    read-failure tests that use ``backend_and_breaker`` instead — producing
    a spurious cross-product of test invocations. The lazy lookup keeps each
    test parametrized by exactly the backend axis it consumes.
    """
    # Only the tests that consume backend_factory (directly or via the
    # ``backend`` fixture) participate in Postgres truncation. Tests that use
    # ``backend_and_breaker`` instead never request backend_factory, so we skip
    # without forcing its parametrization onto them.
    if "backend_factory" not in request.fixturenames:
        yield
        return

    backend_factory = request.getfixturevalue("backend_factory")
    # Determine whether this test is using the Postgres factory.
    # backend_factory is a callable; compare by identity to _postgres_factory.
    _is_postgres = backend_factory is _postgres_factory and _POSTGRES_URL is not None
    if not _is_postgres:
        yield
        return

    import psycopg  # already confirmed importable when _POSTGRES_AVAILABLE

    conn = psycopg.connect(_POSTGRES_URL, autocommit=True)
    try:
        # On a fresh CI database the tables may not exist yet (the backend
        # creates them lazily on first connection).  Guard against
        # UndefinedTable — the per-test ``backend`` fixture will create the
        # schema when the backend is constructed.
        #
        # Truncate ONLY run_records (RESTART IDENTITY to reset the BIGSERIAL
        # counter — keeps same-ts insertion-order tests deterministic).
        # meta is intentionally NOT truncated: the schema_version row must
        # persist across tests so every test exercises the warm version-
        # validation read path in _ensure_schema(), not just the cold-start
        # INSERT path.  Truncating meta would silently route every test
        # through INSERT ON CONFLICT DO NOTHING and never test the
        # SELECT/validate branch — leaving the schema-mismatch refusal path
        # (postgres.py:_ensure_schema version check) unverified against real
        # Postgres.
        try:
            conn.execute("TRUNCATE run_records RESTART IDENTITY")
        except psycopg.errors.UndefinedTable:
            # Tables do not exist yet — the subsequent backend fixture will
            # create them.  Nothing to truncate; this is not an error.
            pass
    finally:
        conn.close()

    yield
    # No post-test cleanup needed — next test's pre-truncate handles it.


@pytest.fixture
def backend(backend_factory, tmp_path):
    """A backend rooted at a per-test tmp_path; closed on teardown."""
    b = backend_factory(tmp_path)
    yield b
    # Close the backend after each test to release connections (no-op for
    # filesystem/sqlite which don't hold network connections).
    if hasattr(b, "close"):
        try:
            b.close()
        except Exception:
            pass


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
    idempotency_key: str | None = None,
    replayed_run_id: str | None = None,
    conversation_id: str | None = None,
    workflow_id: str | None = None,
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
        idempotency_key=idempotency_key,
        replayed_run_id=replayed_run_id,
        conversation_id=conversation_id,
        workflow_id=workflow_id,
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


def test_query_filters_by_model(backend):
    """LogQuery.model is a Protocol-documented filter — pin it.

    The maintainability specialist surfaced that model was only
    tested as a group_by dimension in aggregate; a SQLite backend with
    a broken model WHERE clause would pass all conformance tests
    without this.
    """
    backend.append(
        _make_record(
            model="claude-opus-4-7",
            ts=_ts_at(2026, 5, 15, 10),
            run_id="opus",
        )
    )
    backend.append(
        _make_record(
            model="claude-haiku-4-5",
            ts=_ts_at(2026, 5, 15, 11),
            run_id="haiku",
        )
    )
    out = backend.query(LogQuery(model="claude-opus-4-7"))
    assert len(out) == 1
    assert out[0].run_id == "opus"


def test_query_until_is_inclusive(backend):
    """LogQuery.until is documented inclusive — pin the boundary.

    A SQLite backend that naively translates this to `WHERE ts <
    :until` (exclusive) would pass the broader since/until window test
    without this boundary check. Mirror for since lower bound.
    """
    ts_exact = _ts_at(2026, 5, 15, 12)
    ts_after = _ts_at(2026, 5, 15, 13)
    backend.append(_make_record(ts=ts_exact, run_id="at_boundary"))
    backend.append(_make_record(ts=ts_after, run_id="after_boundary"))
    out = backend.query(LogQuery(until=datetime(2026, 5, 15, 12, tzinfo=timezone.utc)))
    assert len(out) == 1
    assert out[0].run_id == "at_boundary"


def test_query_since_is_inclusive(backend):
    """LogQuery.since is documented inclusive — pin the boundary."""
    ts_before = _ts_at(2026, 5, 15, 11)
    ts_exact = _ts_at(2026, 5, 15, 12)
    backend.append(_make_record(ts=ts_before, run_id="before_boundary"))
    backend.append(_make_record(ts=ts_exact, run_id="at_boundary"))
    out = backend.query(LogQuery(since=datetime(2026, 5, 15, 12, tzinfo=timezone.utc)))
    assert len(out) == 1
    assert out[0].run_id == "at_boundary"


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


def test_query_filter_sub_second_precision(backend):
    """since/until comparison must hold at sub-second precision.

    A backend that truncates ts to date or to seconds would mis-filter
    rapid-fire records (multiple appends within one second). This
    pins full ISO-8601 precision.
    """
    base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    backend.append(
        _make_record(
            ts=(base.replace(microsecond=100_000)).isoformat(),
            run_id="early",
        )
    )
    backend.append(
        _make_record(
            ts=(base.replace(microsecond=900_000)).isoformat(),
            run_id="late",
        )
    )
    out = backend.query(LogQuery(since=base.replace(microsecond=500_000)))
    assert len(out) == 1
    assert out[0].run_id == "late"


def test_query_filters_by_cost_source(backend):
    """Legacy records without cost_source count as 'actor' for backward compat."""
    backend.append(
        _make_record(cost_source="actor", ts=_ts_at(2026, 5, 15, 10), run_id="actor1")
    )
    backend.append(
        _make_record(cost_source="judge", ts=_ts_at(2026, 5, 15, 11), run_id="judge1")
    )
    backend.append(
        _make_record(cost_source=None, ts=_ts_at(2026, 5, 15, 12), run_id="legacy")
    )
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


def test_query_filters_by_idempotency_key(backend):
    """spec/22 addendum item 4: every conforming backend MUST support the
    ``idempotency_key`` AND-predicate, returning only matching records. Mirrors
    the ``mandate_id`` precedent so a third-party backend that omits the filter
    fails the canonical gate rather than passing it (Design Principle #2/#10).
    """
    backend.append(
        _make_record(run_id="k1", idempotency_key="k-1", ts=_ts_at(2026, 5, 15, 10))
    )
    backend.append(
        _make_record(run_id="k2", idempotency_key="k-2", ts=_ts_at(2026, 5, 15, 11))
    )
    backend.append(
        _make_record(run_id="kn", idempotency_key=None, ts=_ts_at(2026, 5, 15, 12))
    )
    out = backend.query(LogQuery(idempotency_key="k-1"))
    assert len(out) == 1
    assert out[0].run_id == "k1"
    assert out[0].idempotency_key == "k-1"
    # Negative arm: no idempotency_key predicate returns all three records.
    out_all = backend.query(LogQuery())
    assert len(out_all) == 3


def test_query_idempotency_key_round_trips_replayed_run_id(backend):
    """replayed_run_id is a canonical field (spec/22 addendum item 3) and MUST
    survive the append → query round trip alongside idempotency_key on a
    status='deduped' record.
    """
    backend.append(
        _make_record(
            run_id="replay",
            status="deduped",
            idempotency_key="dk-1",
            replayed_run_id="orig-run",
            ts=_ts_at(2026, 5, 15, 13),
        )
    )
    out = backend.query(LogQuery(idempotency_key="dk-1"))
    assert len(out) == 1
    assert out[0].status == "deduped"
    assert out[0].replayed_run_id == "orig-run"


def test_query_filters_by_conversation_id(backend):
    """spec/22 versioned normative addendum (spec/47 PR1): every conforming
    backend MUST support the ``conversation_id`` AND-predicate, returning ONLY
    records whose conversation_id matches. Mirrors the ``idempotency_key``
    precedent so a backend that ships the column + index but FORGETS the WHERE
    clause fails this canonical gate instead of silently returning all records
    (Design Principle #2/#10/#12 — the audit-trail filter promise must be real,
    not a documented-but-unwired surface).

    Negative-control shape: the third record has conversation_id=None. A backend
    that skipped the predicate would return all three for LogQuery(conversation_id=
    "c-1"); this asserts exactly one. The no-filter arm asserts the predicate is
    not over-broad (returns all three).
    """
    backend.append(
        _make_record(run_id="c1", conversation_id="c-1", ts=_ts_at(2026, 5, 15, 10))
    )
    backend.append(
        _make_record(run_id="c2", conversation_id="c-2", ts=_ts_at(2026, 5, 15, 11))
    )
    backend.append(
        _make_record(run_id="cn", conversation_id=None, ts=_ts_at(2026, 5, 15, 12))
    )
    out = backend.query(LogQuery(conversation_id="c-1"))
    assert len(out) == 1, (
        "conversation_id predicate MUST scope results to the matching record; "
        "returning more means the WHERE/skip clause is missing (the SHORTCUT "
        "this test guards against)"
    )
    assert out[0].run_id == "c1"
    assert out[0].conversation_id == "c-1"
    # Distinct second key returns only its own record (not a c-1 echo).
    out_c2 = backend.query(LogQuery(conversation_id="c-2"))
    assert len(out_c2) == 1 and out_c2[0].run_id == "c2"
    # No predicate → all three records (predicate is not over-broad).
    assert len(backend.query(LogQuery())) == 3


def test_query_conversation_id_round_trips(backend):
    """conversation_id is a canonical RunRecord field (spec/47 PR1) and MUST
    survive the append → query round trip — present when set, absent (None)
    when not.
    """
    backend.append(
        _make_record(
            run_id="conv-rt",
            conversation_id="thread-7",
            ts=_ts_at(2026, 5, 15, 13),
        )
    )
    out = backend.query(LogQuery(conversation_id="thread-7"))
    assert len(out) == 1
    assert out[0].conversation_id == "thread-7"


def test_query_filters_by_workflow_id(backend):
    """spec/22 versioned normative addendum (issue #622 PR1): every conforming
    backend MUST support the ``workflow_id`` AND-predicate, returning ONLY records
    whose workflow_id matches. Mirrors test_query_filters_by_conversation_id so
    a backend that ships the column + index but FORGETS the WHERE clause fails
    this canonical gate instead of silently returning all records.

    Negative-control shape: the third record has workflow_id=None. A backend
    that skipped the predicate would return all three for LogQuery(workflow_id=
    'w-1'); this asserts exactly one. The no-filter arm asserts the predicate
    is not over-broad (returns all three).
    """
    backend.append(
        _make_record(run_id="w1", workflow_id="w-1", ts=_ts_at(2026, 5, 15, 10))
    )
    backend.append(
        _make_record(run_id="w2", workflow_id="w-2", ts=_ts_at(2026, 5, 15, 11))
    )
    backend.append(
        _make_record(run_id="wn", workflow_id=None, ts=_ts_at(2026, 5, 15, 12))
    )
    out = backend.query(LogQuery(workflow_id="w-1"))
    assert len(out) == 1, (
        "workflow_id predicate MUST scope results to the matching record; "
        "returning more means the WHERE/skip clause is missing (the SHORTCUT "
        "this test guards against). A None-workflow_id record MUST NOT match."
    )
    assert out[0].run_id == "w1"
    assert out[0].workflow_id == "w-1"
    # Distinct second key returns only its own record (not a w-1 echo).
    out_w2 = backend.query(LogQuery(workflow_id="w-2"))
    assert len(out_w2) == 1 and out_w2[0].run_id == "w2"
    # No predicate → all three records (predicate is not over-broad).
    assert len(backend.query(LogQuery())) == 3


def test_query_workflow_id_round_trips(backend):
    """workflow_id is a canonical RunRecord field (spec/22 addendum, issue #622
    PR1) and MUST survive the append → query round trip — present when set,
    absent (None) when not. A backend that adds the column but fails to read
    it back in _row_to_record produces workflow_id=None on every record —
    this test catches that silent failure.
    """
    backend.append(
        _make_record(
            run_id="wf-rt",
            workflow_id="wf-round-trip",
            ts=_ts_at(2026, 5, 15, 14),
        )
    )
    out = backend.query(LogQuery(workflow_id="wf-round-trip"))
    assert len(out) == 1
    assert out[0].workflow_id == "wf-round-trip"
    # A record with no workflow_id must round-trip to None, not empty string.
    backend.append(
        _make_record(run_id="wf-none", workflow_id=None, ts=_ts_at(2026, 5, 15, 15))
    )
    all_recs = backend.query(LogQuery())
    wf_none_recs = [r for r in all_recs if r.run_id == "wf-none"]
    assert len(wf_none_recs) == 1
    assert wf_none_recs[0].workflow_id is None


def test_query_filters_by_parent_run_id(backend):
    backend.append(
        _make_record(
            parent_run_id="parent-x", run_id="child-1", ts=_ts_at(2026, 5, 15, 10)
        )
    )
    backend.append(
        _make_record(
            parent_run_id="parent-x", run_id="child-2", ts=_ts_at(2026, 5, 15, 11)
        )
    )
    backend.append(
        _make_record(
            parent_run_id="parent-y", run_id="child-3", ts=_ts_at(2026, 5, 15, 12)
        )
    )
    out = backend.query(LogQuery(parent_run_id="parent-x"))
    assert {r.run_id for r in out} == {"child-1", "child-2"}


def test_query_filters_by_agent_name_isolates_explicit_agent_records(backend):
    """Records with explicit agent_name are isolated by the filter.

    Step 11 P0 #1 pin: shared-backend deployments (single SQLite/
    Postgres file across agents) MUST isolate cross-agent records.
    Alice's reads MUST NOT include bob's explicitly-stamped records.
    """
    backend.append(
        _make_record(
            agent_name="alice",
            run_id="a1",
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    backend.append(
        _make_record(
            agent_name="bob",
            run_id="b1",
            ts=_ts_at(2026, 5, 15, 11),
        )
    )
    backend.append(
        _make_record(
            agent_name="alice",
            run_id="a2",
            ts=_ts_at(2026, 5, 15, 12),
        )
    )
    out = backend.query(LogQuery(agent_name="alice"))
    assert {r.run_id for r in out} == {"a1", "a2"}
    out_bob = backend.query(LogQuery(agent_name="bob"))
    assert {r.run_id for r in out_bob} == {"b1"}


def test_query_filters_by_agent_name_lenient_on_missing(backend):
    """Records WITHOUT agent_name match the filter (legacy compat).

    Pre-PR-2 records on disk don't carry agent_name. Under filesystem's
    per-agent-dir scoping, every record in the dir IS the named
    agent's — strict filtering would break dashboard reads of legacy
    data. Lenient matching (column == filter OR column IS NULL)
    preserves backward compat without weakening cross-agent isolation
    for explicitly-stamped records.
    """
    backend.append(
        _make_record(
            agent_name=None,
            run_id="legacy",
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    backend.append(
        _make_record(
            agent_name="alice",
            run_id="explicit_alice",
            ts=_ts_at(2026, 5, 15, 11),
        )
    )
    backend.append(
        _make_record(
            agent_name="bob",
            run_id="explicit_bob",
            ts=_ts_at(2026, 5, 15, 12),
        )
    )
    out = backend.query(LogQuery(agent_name="alice"))
    # Legacy record matches (no agent_name) AND alice's explicit
    # records match. Bob's explicit records are excluded.
    assert {r.run_id for r in out} == {"legacy", "explicit_alice"}


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


def test_tail_empty_backend_returns_empty_list(backend):
    """tail(n>0) against an empty backend MUST return [] without raising."""
    assert backend.tail(5) == []
    assert backend.tail(1) == []


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
    backend.append(
        _make_record(
            model="claude-opus-4-7",
            cost_usd=0.5,
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    backend.append(
        _make_record(
            model="claude-opus-4-7",
            cost_usd=1.0,
            ts=_ts_at(2026, 5, 15, 11),
        )
    )
    backend.append(
        _make_record(
            model="claude-haiku-4-5",
            cost_usd=0.01,
            ts=_ts_at(2026, 5, 15, 12),
        )
    )
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
    backend.append(
        _make_record(
            primitive="cost_warning",
            latency_ms=None,
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    backend.append(
        _make_record(
            primitive="cost_warning",
            latency_ms=None,
            ts=_ts_at(2026, 5, 15, 11),
        )
    )
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


def test_delete_older_than_strictly_before(backend):
    """Records with ts == threshold MUST survive (strict-before semantic).

    The spec MUST language pins this; a backend that implements `<=`
    instead of `<` would pass the broader idempotency test without
    this boundary check.
    """
    boundary = _ts_at(2026, 5, 15, 12)
    backend.append(_make_record(ts=boundary, run_id="at_boundary"))
    backend.append(_make_record(ts=_ts_at(2026, 1, 1), run_id="before"))
    deleted = backend.delete_older_than(datetime(2026, 5, 15, 12, tzinfo=timezone.utc))
    assert deleted == 1
    remaining = backend.query(LogQuery())
    assert {r.run_id for r in remaining} == {"at_boundary"}


def test_delete_older_than_empty_backend_returns_zero(backend):
    """delete on empty backend MUST return 0 without raising."""
    deleted = backend.delete_older_than(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert deleted == 0


def test_delete_older_than_rejects_naive_datetime(backend):
    """Naive datetime threshold MUST raise ValueError.

    Silent local-vs-UTC conversion is the failure shape that produces
    off-by-one-day retention errors near midnight. Pins the spec MUST.
    """
    backend.append(_make_record())
    with pytest.raises(ValueError):
        backend.delete_older_than(datetime(2026, 5, 15, 12))  # naive


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


def test_stats_empty_backend(backend):
    """stats() on an empty backend MUST return zero/None per the spec."""
    s = backend.stats()
    assert s.total_records == 0
    assert s.oldest_ts is None
    assert s.newest_ts is None
    assert s.records_today == 0
    assert s.records_this_month == 0


# ──────────────────────────────────────────────────────────────────
# Append — open primitive vocabulary


def test_append_accepts_arbitrary_primitive(backend):
    """Spec: 'Backends MUST accept arbitrary strings — the closed set
    is documentation, not enforcement.' Pins that a backend validating
    primitive against PRIMITIVE_* constants would fail conformance."""
    backend.append(
        _make_record(
            primitive="my_custom_primitive",
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    out = backend.tail(1)
    assert out[0].primitive == "my_custom_primitive"


# ──────────────────────────────────────────────────────────────────
# Aggregate — token sums


def test_aggregate_sum_input_tokens_returns_int(backend):
    """sum_input_tokens MUST return int (not float)."""
    backend.append(
        _make_record(
            input_tokens=100,
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    backend.append(
        _make_record(
            input_tokens=200,
            ts=_ts_at(2026, 5, 15, 11),
        )
    )
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=(), metric="sum_input_tokens"),
    )
    assert result[()] == 300
    assert isinstance(result[()], int)


def test_aggregate_sum_output_tokens_returns_int(backend):
    backend.append(
        _make_record(
            output_tokens=50,
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    backend.append(
        _make_record(
            output_tokens=75,
            ts=_ts_at(2026, 5, 15, 11),
        )
    )
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=(), metric="sum_output_tokens"),
    )
    assert result[()] == 125
    assert isinstance(result[()], int)


# ──────────────────────────────────────────────────────────────────
# Capabilities


def test_capabilities_returns_logcapabilities(backend):
    caps = backend.capabilities()
    assert isinstance(caps, LogCapabilities)
    assert isinstance(caps.supports_aggregation_pushdown, bool)
    assert isinstance(caps.supports_streaming, bool)
    assert isinstance(caps.supports_retention, bool)
    assert isinstance(caps.durable, bool)


def test_capabilities_retention_claim_matches_behavior(backend):
    """Spec: 'Conformance tests assert claim-vs-behavior parity.'

    A backend that claims supports_retention=True MUST implement
    delete_older_than without raising NotImplementedError.
    """
    caps = backend.capabilities()
    if caps.supports_retention:
        backend.append(_make_record(ts=_ts_at(2026, 5, 15, 10)))
        # MUST NOT raise NotImplementedError when the claim is True.
        backend.delete_older_than(datetime(2026, 1, 1, tzinfo=timezone.utc))


# ──────────────────────────────────────────────────────────────────
# Append/round-trip empty-string preservation


def test_append_preserves_empty_string_optional_fields(backend):
    """Empty-string optional fields MUST round-trip preserved.

    Round-tripping ``RunRecord(trigger="")`` through to_dict → JSON →
    from_dict MUST return a RunRecord with trigger=="" (not None).
    Treating empty string as missing was the silent-data-loss failure
    mode the Step 11 adversarial review caught.
    """
    backend.append(
        _make_record(
            trigger="",
            agent_name="",
            ts=_ts_at(2026, 5, 15, 10),
        )
    )
    out = backend.tail(1)
    assert out[0].trigger == ""
    assert out[0].agent_name == ""


# ──────────────────────────────────────────────────────────────────
# Aggregate — multi-field group_by with extra-resolved fields


def test_aggregate_two_extra_fields_group_by(backend):
    """aggregate() with two extra-resolved (JSONB) group_by fields MUST return
    correct keys and values.

    Pins the P1 regression where Postgres's unaliased JSONB expressions
    (``(extra->>'field')``) both get the generated column name ``?column?``.
    With psycopg ``dict_row``, duplicate names collapse — the second key
    overwrites the first, leaving only one entry in the row dict instead
    of two, so ``row_vals[len(group_exprs)]`` raised IndexError and the
    surviving key was wrong.

    The fix (deterministic aliases g0/g1/metric) is verified by this test
    running against the real Postgres backend in CI and against the
    SQLite/Filesystem backends locally (which were already correct).
    """
    backend.append(
        _make_record(
            ts=_ts_at(2026, 5, 15, 10),
            extra={"zone": "us-east", "env": "prod"},
        )
    )
    backend.append(
        _make_record(
            ts=_ts_at(2026, 5, 15, 11),
            extra={"zone": "us-east", "env": "prod"},
        )
    )
    backend.append(
        _make_record(
            ts=_ts_at(2026, 5, 15, 12),
            extra={"zone": "eu-west", "env": "staging"},
        )
    )
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("zone", "env"), metric="count"),
    )
    # Two distinct (zone, env) buckets — keys are text from extra ->> operator.
    assert len(result) == 2
    assert result[("us-east", "prod")] == 2
    assert result[("eu-west", "staging")] == 1


def test_aggregate_extra_field_key_type_divergence(backend):
    """aggregate() group_by on an extra (JSONB/JSON) field has a KNOWN, accepted
    cross-backend key-TYPE divergence — pinned here so it can't drift silently
    into a parity claim (tracked by #366).

    The Postgres ``->>`` JSONB accessor always yields TEXT; the Filesystem and
    SQLite backends return the value's NATIVE Python type (``json_extract`` /
    dict round-trip). String-valued extra fields are identical across all
    backends (text either way) — which is exactly why the string-only
    test_aggregate_two_extra_fields_group_by above never surfaced this.

    The divergence — and whether the documented ``str(k)`` operator mitigation
    actually bridges it — differs by JSON value class. This test pins BOTH the
    NUMERIC case (``str(k)``-bridgeable) and the BOOLEAN case (NOT bridgeable:
    three distinct string forms across backends), because a divergence
    enumeration that pins only numeric would let the spec assert a parity
    (``str(k)`` portability) that silently fails for booleans. See #366 for the
    divergence and remediation options.

    Per-backend key shapes for the SAME data (verified against real PG + SQLite):
      * numeric {'iteration': 1} → ('1',) postgres / (1,) fs+sqlite — str(k) bridges
      * boolean {'flag': True}   → ('true',) postgres / (1,) sqlite / (True,) fs
        — str(k) does NOT bridge: 'true' != '1' != 'True'
    """
    # ── Numeric arm: str(k)-bridgeable divergence ────────────────────────────
    backend.append(_make_record(ts=_ts_at(2026, 5, 15, 10), extra={"iteration": 0}))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15, 11), extra={"iteration": 1}))
    backend.append(_make_record(ts=_ts_at(2026, 5, 15, 12), extra={"iteration": 1}))
    result = backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("iteration",), metric="count"),
    )
    # Normalize keys to str — the cross-backend-stable view FOR NUMERICS (the
    # divergence is in key TYPE, not in key string-form or in the counts).
    by_str = {str(k[0]): v for k, v in result.items()}
    assert by_str["0"] == 1
    assert by_str["1"] == 2
    # Pin the divergence DIRECTION per backend — not a permissive (int|str)
    # union, which would stay green if Postgres regressed to int keys or
    # fs/sqlite regressed to str keys (the exact silent drift this test exists to
    # catch). The Postgres JSONB ``->>`` accessor always yields TEXT; the
    # Filesystem/SQLite backends return the value's NATIVE Python type.
    is_postgres = backend.backend_id == "postgres"
    for raw_key in result:
        assert isinstance(raw_key, tuple) and len(raw_key) == 1
        elem = raw_key[0]
        if is_postgres:
            assert isinstance(elem, str), (
                f"postgres ->> must yield TEXT keys; got {type(elem).__name__} "
                f"{elem!r} (regression: a CAST/normalization crept in?)"
            )
            # bool is a subclass of int but not str — guard against a stray bool.
            assert elem in ("0", "1"), raw_key
        else:
            assert isinstance(elem, int) and not isinstance(elem, bool), (
                f"fs/sqlite must return NATIVE int keys; got "
                f"{type(elem).__name__} {elem!r} (regression: stringified?)"
            )
            assert elem in (0, 1), raw_key

    # ── Boolean arm: str(k)-UNbridgeable divergence (three distinct forms) ────
    # This is the case the spec's str(k) mitigation does NOT cover, so it is
    # pinned explicitly rather than left to imply portability that doesn't hold.
    backend.append(_make_record(ts=_ts_at(2026, 5, 16, 10), extra={"flag": True}))
    backend.append(_make_record(ts=_ts_at(2026, 5, 16, 11), extra={"flag": True}))
    backend.append(_make_record(ts=_ts_at(2026, 5, 16, 12), extra={"flag": False}))
    # Scope to the boolean records via `since` so the earlier numeric records
    # (no `flag` field) do not add a NULL bucket and confuse the key assertions.
    bool_result = backend.aggregate(
        LogQuery(since=datetime(2026, 5, 16, 0, tzinfo=timezone.utc)),
        LogAggregate(group_by=("flag",), metric="count"),
    )
    # Counts are correct on EVERY backend regardless of key shape.
    counts = sorted(bool_result.values())
    assert counts == [1, 2], bool_result
    # Pin the per-backend boolean key shape. bool is a subclass of int, so the
    # fs (native bool) and sqlite (int) cases must be distinguished by exact
    # type, not value membership.
    for raw_key in bool_result:
        assert isinstance(raw_key, tuple) and len(raw_key) == 1
        elem = raw_key[0]
        if is_postgres:
            # JSONB ->> emits the JSON text literal — NOT '1'/'0', NOT 'True'.
            assert isinstance(elem, str) and elem in ("true", "false"), (
                f"postgres bool ->> must yield 'true'/'false'; got "
                f"{type(elem).__name__} {elem!r}"
            )
        elif backend.backend_id == "sqlite":
            # json_extract yields an int (1/0) for a JSON boolean.
            assert isinstance(elem, int) and not isinstance(elem, bool), (
                f"sqlite bool json_extract must yield int 1/0; got "
                f"{type(elem).__name__} {elem!r}"
            )
            assert elem in (0, 1), raw_key
        else:
            # Filesystem round-trips the native Python bool.
            assert isinstance(elem, bool), (
                f"fs bool must round-trip native bool; got "
                f"{type(elem).__name__} {elem!r}"
            )
    # The mitigation gap is real: str(k) does not collapse these to one form.
    # (Asserted only off-postgres where we can compute the fs/sqlite str forms
    # without a live PG; the postgres form is pinned as 'true'/'false' above and
    # str('true') == 'true' != '1'/'True', so the three-way gap holds.)
    if not is_postgres:
        bool_str_keys = {str(k[0]) for k in bool_result}
        # fs yields {'True','False'}; sqlite yields {'1','0'} — neither equals
        # postgres's {'true','false'}, so no single str(k) view is portable.
        assert bool_str_keys & {"true", "false"} == set(), (
            f"unexpected postgres-shaped bool keys off-postgres: {bool_str_keys}"
        )


# ──────────────────────────────────────────────────────────────────
# spec/22 read-failure posture conformance tests (issue #497 addendum)
#
# These tests verify that conforming backends raise LogBackendReadError
# on unrecoverable read failures (not the empty/absent → [] path).
#
# Design:
#   - break_read() is a TEST-SIDE callable, not a method on the backend
#     class (backends MUST NOT have test-only APIs per CLAUDE.md principle).
#   - A separate ``backend_and_breaker`` fixture carries the (backend,
#     break_read_fn) pair WITHOUT changing BackendFactory's type or the
#     existing ``backend`` fixture used by the 48 other conformance tests.
#   - Filesystem: replace log/ dir with a regular file so iterdir() raises
#     NotADirectoryError (an OSError subclass; NOT ENOENT — that returns []).
#     Exercises the directory-level OSError → raise path.
#   - SQLite: overwrite the real on-disk .db file with garbage bytes AFTER
#     dropping the thread-local connection and the WAL/SHM sidecars, so the
#     next call builds a fresh connection and surfaces a genuine
#     sqlite3.DatabaseError ("file is not a database") at connect/schema time
#     — the realistic corruption path the addendum's boundary table calls out,
#     which _get_conn_for_read() wraps.
#   - Postgres: monkeypatch _run_with_reconnect to raise, so the read
#     methods' try/except converts it to LogBackendReadError. Note: the
#     postgres backend itself is still constructed against a LIVE server (the
#     fixture appends a seed record first), so the postgres read-failure test
#     is SKIPPED unless ATOMIC_AGENTS_TEST_POSTGRES_URL + psycopg are present;
#     monkeypatching only avoids a live server FOR THE BREAK INJECTION, not for
#     constructing the backend.
# ──────────────────────────────────────────────────────────────────


def _fs_break_read(backend: FilesystemLogBackend) -> None:
    """Replace the log/ directory with a regular file → iterdir() raises ENOTDIR."""
    log_dir = backend._log_dir
    if log_dir.exists():
        import shutil

        shutil.rmtree(log_dir)
    # Create a regular FILE at the path where the directory should be.
    # iterdir() on a non-directory raises NotADirectoryError (OSError subclass).
    log_dir.write_text("corrupt-marker")


def _sqlite_break_read(backend: "SQLiteLogBackend") -> None:  # type: ignore[name-defined]
    """Corrupt the real on-disk .db file so the next read raises DatabaseError.

    This exercises the REALISTIC corruption path the addendum's boundary table
    names ("sqlite3.DatabaseError on execute() (corruption, I/O error)"): a
    corrupt SQLite file surfaces its DatabaseError during connection setup
    (_get_conn's WAL pragma / _ensure_schema SELECT), which
    ``_get_conn_for_read()`` wraps into LogBackendReadError — NOT via a mocked
    inner ``execute()``. Steps:

      1. Drop the cached thread-local connection so the next call reconnects.
      2. Delete the WAL/SHM sidecars (otherwise sqlite may recover from them).
      3. Overwrite the main .db file with non-database garbage bytes.

    In-memory backends have no on-disk file to corrupt and are not used by the
    sqlite conformance factory (which points at ``scope_root / 'logs.db'``).
    """
    import os

    # 1. Drop the thread-local connection (close it first to release the file).
    conn = getattr(backend._tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        backend._tls.conn = None

    db_path = backend._db_path
    # 2. Remove the WAL/SHM sidecars so SQLite cannot recover the header.
    for sidecar in (f"{db_path}-wal", f"{db_path}-shm"):
        try:
            os.remove(sidecar)
        except FileNotFoundError:
            pass

    # 3. Overwrite the main database file with non-database garbage. The SQLite
    #    header magic ("SQLite format 3\000") no longer matches, so the next
    #    connection's first execute() raises sqlite3.DatabaseError.
    with open(db_path, "wb") as fh:
        fh.write(b"this is not a sqlite database file " * 8)


def _make_postgres_break_read(backend):
    """Return a break_read_fn that monkeypatches _run_with_reconnect to raise.

    Raises a REAL ``psycopg.OperationalError`` (a ``psycopg.Error`` subclass),
    NOT a generic exception: spec/22's boundary table scopes the Postgres wrap
    to "psycopg error surviving the one-shot reconnect retry → raise
    LogBackendReadError", and the impl now catches ``psycopg.Error`` NARROWLY
    (so a generic RuntimeError would NOT be wrapped — it would propagate as a
    code defect, symmetric with SQLite's narrow ``sqlite3.DatabaseError``).
    Injecting the spec-named type proves the actual read-failure boundary
    rather than a generic catch-all. psycopg is importable here because this
    factory only runs when ``_POSTGRES_AVAILABLE`` is True.
    """
    import psycopg  # noqa: PLC0415

    def _break():
        def _raise(_op):
            # Simulate a psycopg OperationalError escaping _run_with_reconnect
            # (e.g., connection drop on the one-shot retry attempt as well).
            raise psycopg.OperationalError(
                "simulated unrecoverable postgres read failure"
            )

        backend._run_with_reconnect = _raise

    return _break


# Backend-and-breaker parameter list (parallel structure to BACKEND_FACTORIES
# but carries the break_read_fn). filesystem + sqlite are always present
# (local, no server). postgres is added ONLY when _POSTGRES_AVAILABLE is True
# (ATOMIC_AGENTS_TEST_POSTGRES_URL set AND psycopg importable) — the fixture
# constructs the backend against a LIVE server and appends a seed record before
# breaking, so the postgres read-failure test is SKIPPED without a server.
# Monkeypatching only avoids a server FOR THE BREAK INJECTION, not for
# constructing/seeding the backend.

_BACKEND_AND_BREAKER_PARAMS: list[tuple[str, Callable, Callable]] = []

# Filesystem entry.
_BACKEND_AND_BREAKER_PARAMS.append(("filesystem", _filesystem_factory, _fs_break_read))

# SQLite entry.
_BACKEND_AND_BREAKER_PARAMS.append(("sqlite", _sqlite_factory, _sqlite_break_read))

# Postgres entry — added only when a live server is configured (see note above).
if _POSTGRES_AVAILABLE:
    _BACKEND_AND_BREAKER_PARAMS.append(
        ("postgres", _postgres_factory, _make_postgres_break_read)
    )


@pytest.fixture(
    params=_BACKEND_AND_BREAKER_PARAMS,
    ids=lambda p: p[0],
)
def backend_and_breaker(request, tmp_path):
    """Yields (backend, break_read_fn) for read-failure injection tests.

    break_read_fn is a zero-argument callable that corrupts the backend's
    read path so the next query/tail/aggregate call hits a genuine
    unrecoverable failure.  The backend has already had one record appended
    before break_read_fn is called, so the backend body is entered (not the
    early-return [] path).
    """
    name, factory_fn, make_breaker = request.param
    backend = factory_fn(tmp_path)
    # Append a record to confirm the backend is live and to ensure the break
    # exercises the read path (not the absent-backend early-return).
    rec = _make_record(run_id="before-break")
    backend.append(rec)

    # For postgres: breaker is a factory (needs the backend instance).
    # For fs/sqlite: breaker takes the backend directly.
    if name == "postgres":
        break_read_fn = make_breaker(backend)
    else:
        break_read_fn = lambda: make_breaker(backend)  # noqa: E731

    yield backend, break_read_fn

    # Teardown: filesystem break replaces log/ with a file; restore is not
    # strictly needed (tmp_path is cleaned up by pytest), but we do close
    # the backend if it has a close() method (Postgres cleanup).
    if hasattr(backend, "close"):
        try:
            backend.close()
        except Exception:
            pass


def test_query_raises_log_backend_read_error(backend_and_breaker):
    """query() MUST raise LogBackendReadError on unrecoverable read failure.

    spec/22 read-failure posture addendum: break_read() injects a genuine
    I/O failure AFTER a successful append so the query() body is entered,
    not the absent-backend early-return path.

    Asserts:
    - LogBackendReadError is raised (not the raw native error type)
    - The raised exception is a subclass of AtomicAgentsError
    - __cause__ is the underlying native error (chained exception) for ALL
      backends — every reference impl wraps with ``raise ... from exc``.
    """
    backend, break_read_fn = backend_and_breaker
    break_read_fn()
    with pytest.raises(LogBackendReadError) as exc_info:
        backend.query(LogQuery())
    # Typed exception is part of the AtomicAgentsError hierarchy.
    assert isinstance(exc_info.value, AtomicAgentsError)
    # Exception must be properly chained (not a bare raise) — all three
    # reference impls use ``raise LogBackendReadError(...) from exc``.
    assert exc_info.value.__cause__ is not None


def test_tail_raises_log_backend_read_error(backend_and_breaker):
    """tail() MUST raise LogBackendReadError on unrecoverable read failure.

    Mirrors test_query_raises_log_backend_read_error for the tail() path,
    including the ``raise ... from exc`` chaining assertion.
    """
    backend, break_read_fn = backend_and_breaker
    break_read_fn()
    with pytest.raises(LogBackendReadError) as exc_info:
        backend.tail(1)
    # Same chaining contract as query(): a bare ``raise LogBackendReadError``
    # (no ``from exc``) would pass conformance otherwise.
    assert exc_info.value.__cause__ is not None


def test_aggregate_raises_log_backend_read_error(backend_and_breaker):
    """aggregate() MUST raise LogBackendReadError on unrecoverable read failure.

    Mirrors test_query_raises_log_backend_read_error for the aggregate() path,
    including the ``raise ... from exc`` chaining assertion. The
    ValueError-for-unknown-metric guard fires before any I/O, so we use a valid
    metric (count) to reach the I/O path.
    """
    backend, break_read_fn = backend_and_breaker
    break_read_fn()
    with pytest.raises(LogBackendReadError) as exc_info:
        backend.aggregate(
            LogQuery(), LogAggregate(group_by=("primitive",), metric="count")
        )
    assert exc_info.value.__cause__ is not None


def test_sum_via_backend_fails_closed_on_log_backend_read_error(tmp_path, caplog):
    """_sum_via_backend MUST return degraded=True when the backend raises
    LogBackendReadError (reader-seam test).

    spec/22 read-failure addendum: the layered catch in _costs._sum_via_backend
    catches LogBackendReadError FIRST (typed catch — clean log), then the broad
    Exception backstop. This test verifies the typed branch fires and returns
    CostReadResult(total_usd=0.0, degraded=True).

    Discriminating assertion (review #497): degraded=True alone does NOT prove
    the TYPED branch fired — the broad ``except Exception`` also catches
    LogBackendReadError (it is an Exception subclass) and returns the same
    degraded result. We assert the typed branch's distinctive log line ("genuine
    unrecoverable read failure"); the broad backstop logs "raised unexpected"
    instead, so this FAILS if the typed handler is removed.
    """
    import logging
    from datetime import date
    from unittest.mock import MagicMock

    from atomic_agents._costs import _sum_via_backend, CostReadResult

    # Build a mock backend whose query() raises LogBackendReadError.
    mock_backend = MagicMock()
    mock_backend.query.side_effect = LogBackendReadError("injected read failure")

    with caplog.at_level(logging.WARNING, logger="atomic_agents._costs"):
        result = _sum_via_backend(
            backend=mock_backend,
            today=date(2026, 6, 14),
            period="today",
            source=None,
            mandate_id=None,
            agent_name=None,
        )

    assert isinstance(result, CostReadResult)
    assert result.degraded is True
    assert result.total_usd == 0.0
    # Verify the TYPED branch fired (not the broad backstop): distinctive log.
    assert mock_backend.query.called
    assert "genuine unrecoverable read failure" in caplog.text
    assert "raised unexpected" not in caplog.text

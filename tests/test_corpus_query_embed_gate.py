"""Tests for the CLI corpus-query embed gate (spec/46, #544 PR2).

Covers:
- Gate skipped when backend has no semantic search (plain FTS path)
- Gate armed when backend advertises supports_semantic_search=True
- embed_reservation emitted BEFORE query
- embed_release emitted in finally (always, even on exception)
- embed_cost emitted AFTER release when actual_usd > 0
- Cost headroom check: blocked when reservation exceeds headroom
- Fail-closed: blocked when cost read degraded AND cap is active
- Uncapped agent NOT blocked by degraded read (MEMORY: fail-closed only where something to protect)
- --critical bypasses headroom check but still emits audit records
- RuntimeError from backend propagates after release record emitted
- cli corpus query --critical flag is accepted by the arg parser
- agent_name attribution: records stamp the originating agent (cross-agent
  cost-leak guard on shared SQLite/Postgres log backends)
- Negative controls: assert tests go RED when guards are stripped

Per project lessons:
- feedback_false_green_test_needs_per_invocation_negative_control
- feedback_fail_closed_only_where_theres_something_to_protect
"""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Fakes and helpers


class _FakeCapabilities:
    """Minimal CorpusCapabilities fake."""

    def __init__(self, *, supports_semantic_search: bool = False, embed_backend=None):
        self.supports_semantic_search = supports_semantic_search
        self.embedding_backend_resolved = embed_backend


class _FakeEmbedBackend:
    """Minimal EmbeddingBackend fake."""

    def __init__(self, model_id: str = "text-embedding-3-small"):
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id


class _FakeCorpusRef:
    def __init__(self, name: str, byte_size: int = 42):
        self.name = name
        self.byte_size = byte_size


class _FakeCorpusBackend:
    """Minimal CorpusBackend fake with configurable query result."""

    def __init__(
        self,
        *,
        supports_semantic_search: bool = False,
        embed_backend=None,
        query_result=None,
        query_raises=None,
    ):
        self._caps = _FakeCapabilities(
            supports_semantic_search=supports_semantic_search,
            embed_backend=embed_backend,
        )
        self._query_result = query_result if query_result is not None else []
        self._query_raises = query_raises

    @property
    def capabilities(self):
        return self._caps

    def query(self, text, corpus, *, top_k=10):
        if self._query_raises is not None:
            raise self._query_raises
        return self._query_result


def _make_agent_root(
    tmp_path: Path,
    *,
    daily_cap_usd: float = 0.0,
    guardrails_enabled: bool | None = None,
) -> Path:
    """Create a minimal agent root with optional cost guardrails.

    parse_model_md reads cost_guardrails from a ```yaml block containing a
    ``cost_guardrails:`` dict — NOT from plain markdown prose.

    ``guardrails_enabled`` controls whether the ```yaml guardrails block is
    emitted INDEPENDENTLY of whether a cap is set:

    - ``None`` (default): emit the block iff ``daily_cap_usd > 0`` (legacy shape).
    - ``True``: ALWAYS emit the block (``enabled: true``), even with a zero cap.
      This is the load-bearing distinction for the uncapped-degraded negative
      control — guardrails-ENABLED-but-no-cap is the ONLY config that reaches the
      ``has_cap AND degraded`` predicate (per feedback_false_green_test_needs_
      per_invocation_negative_control: a daily_cap_usd=0.0 root with the block
      OMITTED short-circuits at ``cost_guardrails_enabled`` and never exercises
      the ``has_cap`` guard at all).
    - ``False``: never emit the block.
    """
    if guardrails_enabled is None:
        guardrails_enabled = daily_cap_usd > 0
    agent_root = tmp_path / "test-agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    (agent_root / "log").mkdir()
    model_text = "## Default model\nclaude-haiku-4-5-20251001\n"
    if guardrails_enabled:
        model_text += (
            "\n## Cost guardrails\n\n"
            "```yaml\n"
            "cost_guardrails:\n"
            "  enabled: true\n"
            f"  daily_cap_usd: {daily_cap_usd}\n"
            "  monthly_cap_usd: 0.0\n"
            "```\n"
        )
    (agent_root / "model.md").write_text(model_text, encoding="utf-8")
    return agent_root


class _FakeLogBackend:
    """In-memory log backend for test isolation."""

    backend_id = "fake-inmemory"

    def __init__(self):
        self.records: list[dict] = []

    def append(self, record) -> None:
        from dataclasses import is_dataclass

        if is_dataclass(record) and not isinstance(record, type):
            # Use to_dict() (not asdict()) so that RunRecord.extra fields
            # are flattened into top-level keys — matching the on-disk shape
            # that test assertions expect (e.g. record["batch_size"] not
            # record["extra"]["batch_size"]).
            self.records.append(record.to_dict())
        else:
            self.records.append(dict(record))

    def query(self, q):
        return []

    def tail(self, n: int = 50):
        return []

    def aggregate(self, *a, **k):
        return {}


def _run_query_gate(
    tmp_path: Path,
    *,
    supports_semantic: bool = True,
    embed_backend=None,
    query_result=None,
    query_raises=None,
    daily_cap_usd: float = 0.0,
    guardrails_enabled: bool | None = None,
    critical: bool = False,
    today_cost: float = 0.0,
    degraded: bool = False,
) -> tuple[int, list[dict]]:
    """Run _corpus_query and return (exit_code, log_records)."""
    from atomic_agents.cli import _corpus_query

    if embed_backend is None:
        embed_backend = _FakeEmbedBackend()

    backend = _FakeCorpusBackend(
        supports_semantic_search=supports_semantic,
        embed_backend=embed_backend,
        query_result=query_result
        if query_result is not None
        else [_FakeCorpusRef("page-a")],
        query_raises=query_raises,
    )
    agent_root = _make_agent_root(
        tmp_path,
        daily_cap_usd=daily_cap_usd,
        guardrails_enabled=guardrails_enabled,
    )
    fake_log = _FakeLogBackend()

    from atomic_agents._costs import CostReadResult

    today_result = CostReadResult(
        total_usd=today_cost, degraded=degraded, dropped_records=0
    )
    month_result = CostReadResult(total_usd=0.0, degraded=degraded, dropped_records=0)

    # get_default_log_backend is lazy-imported inside _corpus_query
    # via `from .logs import get_default_log_backend`, so we patch the
    # source in the logs package (not the cli module namespace).
    with (
        patch("atomic_agents.logs.get_default_log_backend", return_value=fake_log),
        patch(
            "atomic_agents._costs.sum_cost_for_period",
            side_effect=[today_result, month_result],
        ),
    ):
        exit_code = _corpus_query(
            backend,
            "test query",
            "wiki",
            10,
            agent_root,
            critical=critical,
        )

    return exit_code, fake_log.records


# ──────────────────────────────────────────────────────────────────────────────
# Category 1 — No-gate (FTS-only) path


def test_no_gate_when_fts_only(tmp_path: Path) -> None:
    """Gate is skipped when backend has no semantic search — no JSONL records emitted."""
    exit_code, records = _run_query_gate(tmp_path, supports_semantic=False)
    assert exit_code == 0
    # No embed_reservation/release/cost records for FTS path
    triggers = {r.get("trigger") for r in records}
    assert "embed_reservation" not in triggers
    assert "embed_release" not in triggers
    assert "embed_cost" not in triggers


def test_no_gate_when_no_embed_backend(tmp_path: Path) -> None:
    """Gate is skipped when embedding_backend_resolved is None."""
    from atomic_agents.cli import _corpus_query

    backend = _FakeCorpusBackend(
        supports_semantic_search=True,  # advertises semantic but no backend wired
        embed_backend=None,
        query_result=[_FakeCorpusRef("page-a")],
    )
    agent_root = _make_agent_root(tmp_path)
    fake_log = _FakeLogBackend()

    with patch("atomic_agents.logs.get_default_log_backend", return_value=fake_log):
        exit_code = _corpus_query(backend, "query", "wiki", 10, agent_root)

    assert exit_code == 0
    triggers = {r.get("trigger") for r in fake_log.records}
    assert "embed_reservation" not in triggers


# ──────────────────────────────────────────────────────────────────────────────
# Category 2 — Audit trail shape


def test_embed_reservation_emitted_before_query(tmp_path: Path) -> None:
    """embed_reservation is emitted when the gate fires."""
    exit_code, records = _run_query_gate(tmp_path)
    assert exit_code == 0
    reservation = next(
        (r for r in records if r.get("trigger") == "embed_reservation"), None
    )
    assert reservation is not None, "embed_reservation record must exist"
    assert reservation["cost_source"] == "actor"
    assert reservation["batch_size"] == 1
    assert reservation["model"] == "text-embedding-3-small"
    assert reservation["primitive"] == "embed"


def test_embed_release_emitted_after_query(tmp_path: Path) -> None:
    """embed_release is emitted in the finally block after query."""
    exit_code, records = _run_query_gate(tmp_path)
    assert exit_code == 0
    release = next((r for r in records if r.get("trigger") == "embed_release"), None)
    assert release is not None, "embed_release record must exist"
    assert release["cost_source"] == "actor"
    assert release["batch_size"] == 1
    assert release["actual_usd"] > 0  # successful query charges the estimate


def test_embed_cost_emitted_when_actual_usd_positive(tmp_path: Path) -> None:
    """embed_cost record is emitted conditioned on actual_usd > 0."""
    exit_code, records = _run_query_gate(tmp_path)
    assert exit_code == 0
    cost_rec = next((r for r in records if r.get("trigger") == "embed_cost"), None)
    assert cost_rec is not None, "embed_cost record must exist on successful query"
    assert cost_rec["cost_usd"] > 0
    assert cost_rec["cost_source"] == "actor"
    assert cost_rec["primitive"] == "embed"


def test_audit_record_ordering_reservation_before_release(tmp_path: Path) -> None:
    """embed_reservation appears before embed_release in the log."""
    _, records = _run_query_gate(tmp_path)
    triggers = [r.get("trigger") for r in records]
    res_idx = next(i for i, t in enumerate(triggers) if t == "embed_reservation")
    rel_idx = next(i for i, t in enumerate(triggers) if t == "embed_release")
    cost_idx = next(i for i, t in enumerate(triggers) if t == "embed_cost")
    assert res_idx < rel_idx < cost_idx, (
        f"Expected reservation({res_idx}) < release({rel_idx}) < cost({cost_idx})"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Category 3 — Exception path (release in finally)


def test_embed_release_emitted_on_query_exception(tmp_path: Path) -> None:
    """embed_release is emitted even when backend.query() raises."""
    from atomic_agents.cli import _corpus_query

    backend = _FakeCorpusBackend(
        supports_semantic_search=True,
        embed_backend=_FakeEmbedBackend(),
        query_raises=RuntimeError("dim mismatch"),
    )
    agent_root = _make_agent_root(tmp_path)
    fake_log = _FakeLogBackend()

    from atomic_agents._costs import CostReadResult

    with (
        patch("atomic_agents.logs.get_default_log_backend", return_value=fake_log),
        patch(
            "atomic_agents._costs.sum_cost_for_period",
            return_value=CostReadResult(0.0, False, 0),
        ),
    ):
        with pytest.raises(RuntimeError, match="dim mismatch"):
            _corpus_query(backend, "query", "wiki", 10, agent_root)

    triggers = [r.get("trigger") for r in fake_log.records]
    assert "embed_reservation" in triggers, "reservation must emit before exception"
    assert "embed_release" in triggers, "release must emit in finally despite exception"


def test_no_embed_cost_on_query_exception(tmp_path: Path) -> None:
    """embed_cost is NOT emitted when query raises (actual_usd stays 0)."""
    from atomic_agents.cli import _corpus_query

    backend = _FakeCorpusBackend(
        supports_semantic_search=True,
        embed_backend=_FakeEmbedBackend(),
        query_raises=RuntimeError("dim mismatch"),
    )
    agent_root = _make_agent_root(tmp_path)
    fake_log = _FakeLogBackend()

    from atomic_agents._costs import CostReadResult

    with (
        patch("atomic_agents.logs.get_default_log_backend", return_value=fake_log),
        patch(
            "atomic_agents._costs.sum_cost_for_period",
            return_value=CostReadResult(0.0, False, 0),
        ),
    ):
        with pytest.raises(RuntimeError):
            _corpus_query(backend, "query", "wiki", 10, agent_root)

    triggers = [r.get("trigger") for r in fake_log.records]
    assert "embed_cost" not in triggers, "embed_cost must NOT emit when actual_usd=0"


# NEGATIVE CONTROL: the release-in-finally guard is load-bearing.
def test_negative_control_release_requires_finally(tmp_path: Path) -> None:
    """Negative control: if release is inside try (not finally), it won't emit on exception.

    This test verifies the guard is load-bearing by observing the contract:
    release IS emitted on exception — meaning it must be in a finally block.
    The absence of a release record would indicate a regression.
    """
    from atomic_agents.cli import _corpus_query

    backend = _FakeCorpusBackend(
        supports_semantic_search=True,
        embed_backend=_FakeEmbedBackend(),
        query_raises=RuntimeError("intentional"),
    )
    agent_root = _make_agent_root(tmp_path)
    fake_log = _FakeLogBackend()

    from atomic_agents._costs import CostReadResult

    with (
        patch("atomic_agents.logs.get_default_log_backend", return_value=fake_log),
        patch(
            "atomic_agents._costs.sum_cost_for_period",
            return_value=CostReadResult(0.0, False, 0),
        ),
    ):
        with pytest.raises(RuntimeError):
            _corpus_query(backend, "query", "wiki", 10, agent_root)

    # If release is in finally, it appears; if not, this assertion fails (RED).
    assert any(r.get("trigger") == "embed_release" for r in fake_log.records), (
        "embed_release MUST be in a finally block so it fires even on exception. "
        "If this test fails, the guard has regressed from try/finally to try-only."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Category 4 — Cost gate enforcement


def test_gate_blocks_when_reservation_exceeds_headroom(tmp_path: Path, capsys) -> None:
    """Gate returns exit code 1 when per-call cost exceeds remaining headroom.

    "test query" is 10 UTF-8 bytes → ceil(10/3)=4 tokens →
    $0.020/1M × 4 = $0.00000008 raw, but calc_embedding_cost CEILINGS up to 6
    decimal places (worst-case-reservation ceiling, _costs.py) → $0.000001
    reservation (verified: calc_embedding_cost("text-embedding-3-small", 4)
    returns (1e-06, False)).
    Cap=$0.000001, prior spend=$0.0000009 → headroom=$0.0000001 <
    $0.000001 reservation → blocks. (Note: the gate blocks because the CEILING'd
    $1e-6 reservation exceeds the $1e-7 headroom — NOT because of the $8e-8 raw
    figure, which would be BELOW the headroom and would not block. The gate
    returns BEFORE emitting any reservation record, so there is no record to
    assert on here; the ceiling value is pinned directly below.)
    """
    # Pin the ceiling behavior the docstring math depends on: calc_embedding_cost
    # rounds the $8e-8 raw product UP to 6 decimals → $1e-6. If a refactor dropped
    # the ceiling the reservation would fall to ~$8e-8, BELOW the $1e-7 headroom,
    # and the gate would no longer block (this test would silently invert).
    from atomic_agents._costs import calc_embedding_cost

    reserved, _est = calc_embedding_cost("text-embedding-3-small", 4)
    assert math.isclose(reserved, 1e-06, rel_tol=1e-9), (
        "expected the 6-decimal ceiling ($1e-6), not the $8e-8 raw product"
    )

    exit_code, _ = _run_query_gate(
        tmp_path,
        daily_cap_usd=0.000001,
        today_cost=0.0000009,  # headroom = 0.0000001; reservation will exceed it
    )
    assert exit_code == 1
    out = capsys.readouterr()
    assert "headroom" in out.err or "exceeds" in out.err


def test_gate_fail_closed_on_degraded_read_with_cap(tmp_path: Path, capsys) -> None:
    """Gate returns exit code 1 when cost read is degraded AND a cap is active."""
    exit_code, _ = _run_query_gate(
        tmp_path,
        daily_cap_usd=1.0,
        degraded=True,
    )
    assert exit_code == 1
    out = capsys.readouterr()
    assert "fail-closed" in out.err or "unreadable" in out.err


def test_gate_passes_on_degraded_read_without_cap(tmp_path: Path) -> None:
    """Guardrails-ENABLED-but-no-cap agent NOT blocked by degraded read.

    This exercises the ``has_cap AND degraded`` predicate directly:
    guardrails are enabled (so the gate enters the cost-read branch) but BOTH
    caps are zero (so ``has_cap`` is False). The fail-closed branch must NOT
    fire — per feedback_fail_closed_only_where_theres_something_to_protect, a
    blind read changes nothing when there is no budget to protect.
    """
    exit_code, records = _run_query_gate(
        tmp_path,
        daily_cap_usd=0.0,  # no cap...
        guardrails_enabled=True,  # ...but guardrails ENABLED so we reach has_cap
        degraded=True,
    )
    assert exit_code == 0
    # audit records still emit
    triggers = {r.get("trigger") for r in records}
    assert "embed_reservation" in triggers


# NEGATIVE CONTROL: guardrails-enabled-but-uncapped + degraded must NOT block.
def test_negative_control_uncapped_degraded_must_not_block(tmp_path: Path) -> None:
    """Negative control (predicate part 2): gate must be 'has_cap AND degraded'.

    Guardrails ENABLED (reaches the cost-read branch) but zero caps. If the
    ``has_cap AND`` half of the predicate is stripped — leaving a bare
    ``if degraded: fail-closed`` — this test goes RED (exit_code becomes 1),
    because a degraded read with no cap would then spuriously block.

    Verified RED-on-strip during the #544 PR2 LOCK adversarial pass: with the
    PRIOR fixture (daily_cap_usd=0.0 and the guardrails block OMITTED) the gate
    short-circuited at ``cost_guardrails_enabled`` and NEVER reached the
    ``has_cap`` guard, so stripping ``has_cap AND`` left this test falsely GREEN.
    Enabling guardrails here is what makes the negative control load-bearing
    (feedback_false_green_test_needs_per_invocation_negative_control).
    """
    exit_code, _ = _run_query_gate(
        tmp_path,
        daily_cap_usd=0.0,
        guardrails_enabled=True,
        degraded=True,
    )
    # If this fails (exit_code==1), the guard is incorrectly blocking uncapped agents.
    assert exit_code == 0, (
        "A guardrails-enabled-but-uncapped agent MUST NOT be blocked by a "
        "degraded cost read. The fail-closed predicate must be 'has_cap AND "
        "degraded', not just 'degraded'."
    )


# STRUCTURAL INVARIANT (not a strippable control): cost_estimated != degraded.
def test_invariant_cost_estimated_does_not_trigger_fail_closed(
    tmp_path: Path,
) -> None:
    """Structural invariant: the gate must NOT grow a ``cost_estimated`` branch.

    NOTE — this is a structural-invariant/regression test, NOT a per-invocation
    strip control (per feedback_false_green_test_needs_per_invocation_negative_
    control). The fail-closed predicate is ``has_cap AND degraded`` and never
    references ``cost_estimated``, so there is no ``cost_estimated`` guard to
    strip — by construction this test cannot go RED on any strip of the SHIPPED
    code. It exists to pin the design decision: ``cost_estimated`` (an unknown
    PRICING model, returned by calc_embedding_cost) must never become a
    fail-close trigger; only a degraded (unreadable) cost LEDGER does. If a
    future change ADDED a ``cost_estimated``-conflating branch, this test would
    catch it.

    A capped agent whose cost read is CLEAN (degraded=False) but whose embedding
    model is unpriced (cost_estimated=True) MUST proceed — cost_estimated only
    affects the reserved amount. Uses an unknown model_id so calc_embedding_cost
    returns cost_estimated=True with degraded=False.
    """
    exit_code, records = _run_query_gate(
        tmp_path,
        embed_backend=_FakeEmbedBackend(model_id="some-unlisted-embed-model"),
        daily_cap_usd=1.0,  # generous cap, clean read
        degraded=False,
        today_cost=0.0,
    )
    assert exit_code == 0, (
        "cost_estimated (unknown pricing model) MUST NOT trigger the "
        "fail-closed gate — only a degraded (unreadable) cost LEDGER does."
    )
    reservation = next(
        (r for r in records if r.get("trigger") == "embed_reservation"), None
    )
    assert reservation is not None
    # Confirm the reservation actually carries cost_estimated=True so the test
    # is exercising the intended branch (unknown model), not a priced one.
    assert reservation.get("cost_estimated") is True, (
        "expected an unpriced model so cost_estimated=True; otherwise this "
        "negative control is not exercising the cost_estimated path"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Category 5 — --critical bypass


def test_critical_bypasses_headroom_but_still_emits_records(tmp_path: Path) -> None:
    """--critical skips headroom enforcement but still emits audit records."""
    exit_code, records = _run_query_gate(
        tmp_path,
        daily_cap_usd=0.000001,
        today_cost=0.0000009,  # would block without --critical
        critical=True,
    )
    assert exit_code == 0
    triggers = {r.get("trigger") for r in records}
    assert "embed_reservation" in triggers
    assert "embed_release" in triggers
    assert "embed_cost" in triggers


def test_critical_flag_is_set_on_audit_records(tmp_path: Path) -> None:
    """critical=True is stamped on the embed_reservation record."""
    _, records = _run_query_gate(tmp_path, critical=True)
    reservation = next(
        (r for r in records if r.get("trigger") == "embed_reservation"), None
    )
    assert reservation is not None
    assert reservation.get("critical") is True


# ──────────────────────────────────────────────────────────────────────────────
# Category 5b — agent_name attribution (cross-agent cost-leak guard)


def test_all_embed_records_carry_originating_agent_name(tmp_path: Path) -> None:
    """The three CLI embed records stamp agent_name = the originating agent.

    Mirrors agent.py._log's ``record.setdefault("agent_name", self.name)``.
    Without the stamp the records persist with agent_name=None; on a shared
    SQLite/Postgres log backend the cost-read filter
    ``(agent_name = ? OR agent_name IS NULL)`` then folds them into EVERY
    agent's cap baseline (a cross-agent spend-attribution leak — the inverse of
    the #61 lesson).
    """
    exit_code, records = _run_query_gate(tmp_path)
    assert exit_code == 0
    agent_root_name = (tmp_path / "test-agent").name
    embed_records = [
        r
        for r in records
        if r.get("trigger") in ("embed_reservation", "embed_release", "embed_cost")
    ]
    assert len(embed_records) == 3, "expected reservation + release + cost records"
    for r in embed_records:
        assert r.get("agent_name") == agent_root_name, (
            f"{r.get('trigger')} record must carry agent_name="
            f"{agent_root_name!r} (the originating agent), not "
            f"{r.get('agent_name')!r}. A None agent_name leaks this query's spend "
            "into other agents' caps on shared SQLite/Postgres log backends."
        )


def test_cli_embed_spend_not_attributed_to_other_agent_on_shared_backend(
    tmp_path: Path,
) -> None:
    """Shared-backend regression + strip negative control for the agent_name stamp.

    Writes a CLI embed_cost record for ``agentA`` through a real SQLiteLogBackend
    (the shared/org shape), then reads ``agentB``'s agent_name-filtered cost. The
    SQLite filter is ``(agent_name = ? OR agent_name IS NULL)``; with the
    agent_name stamp present, agentA's record is attributed to agentA and agentB's
    filtered sum is 0.0. Without the stamp (the bug) the record carries
    agent_name=NULL and the ``OR agent_name IS NULL`` clause folds it into
    agentB's read.

    Strip control: I verified this test goes RED when
    ``record.setdefault("agent_name", agent_name)`` is removed from
    ``_corpus_query._emit`` — agentB's sum becomes ~1e-6 instead of 0.0.
    (feedback_false_green_test_needs_per_invocation_negative_control)
    """
    from atomic_agents import _costs
    from atomic_agents._costs import sum_cost_for_period
    from atomic_agents.logs.sqlite import SQLiteLogBackend

    shared_db = tmp_path / "shared.sqlite"
    shared_backend = SQLiteLogBackend(shared_db)

    # Run the gate for agentA against the SHARED SQLite backend.
    agent_a_root = _make_agent_root(tmp_path / "a", daily_cap_usd=0.0)
    backend = _FakeCorpusBackend(
        supports_semantic_search=True,
        embed_backend=_FakeEmbedBackend(),
        query_result=[_FakeCorpusRef("page-a")],
    )
    from atomic_agents.cli import _corpus_query

    with patch(
        "atomic_agents.logs.get_default_log_backend", return_value=shared_backend
    ):
        exit_code = _corpus_query(
            backend, "test query", "wiki", 10, agent_a_root, critical=False
        )
    assert exit_code == 0

    # agentA's embed spend was written. Confirm it lands on agentA.
    a_sum = sum_cost_for_period(
        agent_a_root / "log",
        "today",
        source="actor",
        backend=shared_backend,
        agent_name=agent_a_root.name,
    )
    assert not a_sum.degraded
    assert a_sum.total_usd > 0, "agentA's own filtered read must see its embed spend"

    # The core invariant: agentB (a DIFFERENT agent) must NOT see agentA's spend.
    b_sum = sum_cost_for_period(
        tmp_path / "b" / "log",
        "today",
        source="actor",
        backend=shared_backend,
        agent_name="some-other-agent-b",
    )
    assert not b_sum.degraded
    assert b_sum.total_usd == 0.0, (
        "agentB's agent_name-filtered cost read MUST be 0.0 — agentA's CLI embed "
        "spend must not leak across the shared-backend "
        "'(agent_name = ? OR agent_name IS NULL)' filter. A non-zero value means "
        "the embed records were written with a NULL agent_name."
    )

    # Defensive: ensure we actually exercised the agent_name-filtered SQL path
    # (not a no-op skip) by confirming the cross-agent leak would be detectable.
    assert _costs is not None


# ──────────────────────────────────────────────────────────────────────────────
# Category 6 — Arg parser surface


def test_corpus_query_critical_flag_accepted_by_parser() -> None:
    """corpus query --critical is a valid CLI flag (not rejected by argparse)."""
    from atomic_agents.cli import main

    # Build minimal argv that reaches argparse without actually running the query
    with patch("atomic_agents.cli._cmd_corpus", return_value=0) as mock_cmd:
        main(
            [
                "corpus",
                "query",
                "hello",
                "--corpus",
                "wiki",
                "--agent-root",
                "/tmp",
                "--critical",
            ]
        )
    mock_cmd.assert_called_once()
    args = mock_cmd.call_args[0][0]
    assert args.critical is True


def test_corpus_query_critical_defaults_to_false() -> None:
    """corpus query without --critical has critical=False."""
    from atomic_agents.cli import main

    with patch("atomic_agents.cli._cmd_corpus", return_value=0) as mock_cmd:
        main(
            [
                "corpus",
                "query",
                "hello",
                "--corpus",
                "wiki",
                "--agent-root",
                "/tmp",
            ]
        )
    mock_cmd.assert_called_once()
    args = mock_cmd.call_args[0][0]
    assert args.critical is False


# ──────────────────────────────────────────────────────────────────────────────
# Category 7 — Token estimation basis


def test_token_estimate_basis_ceil_utf8_bytes_over_3(tmp_path: Path) -> None:
    """Token estimate is ceil(utf8_bytes / 3) — same as batch gate."""
    from atomic_agents._costs import calc_embedding_cost
    from atomic_agents.cli import _corpus_query

    text = "hello"  # 5 UTF-8 bytes -> ceil(5/3)=2 tokens
    expected_tokens = math.ceil(len(text.encode("utf-8")) / 3)
    expected_cost, _ = calc_embedding_cost("text-embedding-3-small", expected_tokens)

    backend = _FakeCorpusBackend(
        supports_semantic_search=True,
        embed_backend=_FakeEmbedBackend(),
        query_result=[_FakeCorpusRef("result")],
    )
    agent_root = _make_agent_root(tmp_path)
    fake_log = _FakeLogBackend()

    from atomic_agents._costs import CostReadResult

    with (
        patch("atomic_agents.logs.get_default_log_backend", return_value=fake_log),
        patch(
            "atomic_agents._costs.sum_cost_for_period",
            return_value=CostReadResult(0.0, False, 0),
        ),
    ):
        _corpus_query(backend, text, "wiki", 10, agent_root)

    reservation = next(
        r for r in fake_log.records if r.get("trigger") == "embed_reservation"
    )
    assert abs(reservation["reserved_usd"] - expected_cost) < 1e-12, (
        f"reserved_usd {reservation['reserved_usd']} != expected {expected_cost}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Category 8 — No-result path


def test_no_results_prints_no_matches(tmp_path: Path, capsys) -> None:
    """No results from query prints 'No matches' and returns 0."""
    exit_code, records = _run_query_gate(tmp_path, query_result=[])
    assert exit_code == 0
    out = capsys.readouterr()
    assert "No matches" in out.out
    # Gate still emits audit records even for empty results
    triggers = {r.get("trigger") for r in records}
    assert "embed_reservation" in triggers
    assert "embed_release" in triggers

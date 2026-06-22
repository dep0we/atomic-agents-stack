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
- Negative controls: assert tests go RED when guards are stripped

Per project lessons:
- feedback_false_green_test_needs_per_invocation_negative_control
- feedback_fail_closed_only_where_theres_something_to_protect
"""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _make_agent_root(tmp_path: Path, *, daily_cap_usd: float = 0.0) -> Path:
    """Create a minimal agent root with optional cost guardrails.

    parse_model_md reads cost_guardrails from a ```yaml block containing a
    ``cost_guardrails:`` dict — NOT from plain markdown prose.
    """
    agent_root = tmp_path / "test-agent"
    agent_root.mkdir()
    (agent_root / "log").mkdir()
    model_text = "## Default model\nclaude-haiku-4-5-20251001\n"
    if daily_cap_usd > 0:
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
    agent_root = _make_agent_root(tmp_path, daily_cap_usd=daily_cap_usd)
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
    $0.020/1M × 4 = $0.00000008 reservation.
    Cap=$0.000001, prior spend=$0.0000009 → headroom=$0.0000001 < reservation.
    """
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
    """Uncapped agent NOT blocked by degraded read (fail-closed only where protection needed)."""
    exit_code, records = _run_query_gate(
        tmp_path,
        daily_cap_usd=0.0,  # no cap
        degraded=True,
    )
    assert exit_code == 0
    # audit records still emit
    triggers = {r.get("trigger") for r in records}
    assert "embed_reservation" in triggers


# NEGATIVE CONTROL: uncapped + degraded must NOT block.
def test_negative_control_uncapped_degraded_must_not_block(tmp_path: Path) -> None:
    """Negative control: the fail-closed guard must gate on 'has_cap AND degraded', not just degraded."""
    exit_code, _ = _run_query_gate(
        tmp_path,
        daily_cap_usd=0.0,
        degraded=True,
    )
    # If this fails (exit_code==1), the guard is incorrectly blocking uncapped agents.
    assert exit_code == 0, (
        "An uncapped agent MUST NOT be blocked by a degraded cost read. "
        "The fail-closed predicate must be 'has_cap AND degraded', not just 'degraded'."
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
# Category 6 — Arg parser surface


def test_corpus_query_critical_flag_accepted_by_parser() -> None:
    """corpus query --critical is a valid CLI flag (not rejected by argparse)."""
    from atomic_agents.cli import main

    # Build minimal argv that reaches argparse without actually running the query
    with patch("atomic_agents.cli._cmd_corpus", return_value=0) as mock_cmd:
        exit_code = main(
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
        exit_code = main(
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

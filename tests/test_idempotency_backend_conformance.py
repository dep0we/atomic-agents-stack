"""Conformance tests for IdempotencyBackend Protocol (spec/45).

Tests covering the IdempotencyBackend Implementer Contract (normative MUSTs).
The protocol-behavior tests are parametrized over every registered backend via
the ``backend`` fixture; see PARAMETRIZATION below.

NOTE ON NUMBERING: the "TEST N" labels below are this file's own granular
test-case indices, NOT spec/45 MUST numbers.

  TEST 1  — side-effect-free construction (spec/45 MUST 2)
  TEST 2  — lookup() returns FRESH for unknown key (spec/45 MUST 6)
  TEST 3  — begin() returns FRESH for new key (spec/45 MUST 4)
  TEST 4  — DedupDecision has all required fields (spec/45 MUST 1)
  TEST 5  — DedupDecision.state is REQUIRED (no default) (spec/45 MUST 1)
  TEST 6  — begin() returns IN_FLIGHT for in-flight key (spec/45 MUST 4)
  TEST 7  — begin() returns COMPLETED for committed key (spec/45 MUST 4/5)
  TEST 8  — commit() writes terminal marker, lookup() returns COMPLETED (spec/45 MUST 5)
  TEST 9  — commit() does not store result content (MARKER-ONLY) (spec/45 MUST 5)
  TEST 10 — begin() on empty ledger (first use) returns FRESH, not fail-closed (spec/45 MUST 4)
  TEST 11 — lookup() on absent ledger returns FRESH (spec/45 MUST 6)
  TEST 12 — capabilities() returns IdempotencyCapabilities (spec/45 MUST 3)
  TEST 13 — IdempotencyCapabilities is frozen dataclass
  TEST 14 — single_host_only is REQUIRED (no default) on IdempotencyCapabilities (spec/45 MUST 3)
  TEST 15 — atomic_claim is REQUIRED (no default) on IdempotencyCapabilities (spec/45 MUST 3)
  TEST 16 — capabilities().atomic_claim consistent across calls
  TEST 17 — capabilities().single_host_only consistent across calls
  TEST 18 — supports_canonical_export=True for filesystem (spec/45 MUST 3 / spec/40)
  TEST 19 — export() returns IdempotencyExport type (spec/45 export contract)
  TEST 20 — export() empty when ledger absent (spec/45 MUST 2 / export contract)
  TEST 21 — export() excludes in-flight lease (spec/45 export contract)
  TEST 22 — export() includes terminal entry after commit (spec/45 export contract)
  TEST 23 — export_all() equals export(None) (spec/45 export contract)
  TEST 24 — backend_id stable across calls (spec/45 MUST 8)
  TEST 25 — storage isolation: two backends don't share state (spec/45 MUST 5)
  TEST 26 — begin() race: O_EXCL barrier — exactly one wins FRESH (spec/45 MUST 4)
  TEST 27 — begin() rejects key with path separator (spec/45 MUST 4)
  TEST 28 — begin() rejects empty key (spec/45 MUST 4)
  TEST 29 — begin() rejects '.' key (spec/45 MUST 4)
  TEST 30 — begin() rejects '..' key (spec/45 MUST 4)
  TEST 31 — commit() ACCEPTS a URI/path/backslash result_ref (opaque, separators permitted) (spec/45 MUST 5)
  TEST 32 — _redact_for_error_message() URL redaction (spec/45 MUST 6)
  TEST 33 — _redact_for_error_message() DSN redaction (spec/45 MUST 6)
  TEST 34 — _redact_for_error_message() truncation (spec/45 MUST 6)
  TEST 35 — _redact_for_error_message() passthrough for short value (spec/45 MUST 6)
  TEST 36 — get_default_idempotency_backend() uses filesystem by default
  TEST 37 — get_idempotency_backend() raises BackendNotRegistered for unknown id
  TEST 38 — env var dispatches registered custom backend
  TEST 39 — get_default_idempotency_backend() unknown env var raises BackendNotRegistered
  TEST 40 — IdempotencyBackend is @runtime_checkable (isinstance check)
  TEST 41 — doctor.check_idempotency_backend PASS for valid agent (spec/45)
  TEST 42 — doctor.check_idempotency_backend FAIL for bad env var (spec/45)
  TEST 43 — doctor.check_idempotency_backend WARN single_host_only + MULTI_HOST (spec/45 MUST 3)
  TEST 44 — doctor dual-probe: begin() fail triggers FAIL (not PASS) (spec/45 MUST 3)
  TEST 45 — IdempotencyExport importable from atomic_agents.export (spec/40)
  TEST 46 — WritePolicy NOT in IdempotencyBackend Protocol surface
  TEST 47 — supports_ttl=False for filesystem (PR1 — sweep is follow-up)
  TEST 48 — begin() is VALUE OBJECT: never raises for duplicate detection (spec/45 MUST 1)
  TEST 49 — error-path branch assertion: typed log line via caplog (Project Lesson 2)

PARAMETRIZATION: protocol-behavior tests use the ``backend`` fixture parametrized
over BACKEND_FACTORIES (currently just 'filesystem'). Adding a second backend to
BACKEND_FACTORIES picks up every protocol-behavior test automatically.

Filesystem-specific tests are deliberately NOT parametrized: O_EXCL race,
containment guards, ATOMIC_AGENTS_IDEMPOTENCY_BACKEND registry dispatch.
Pure-dataclass tests (DedupDecision/IdempotencyCapabilities) need no backend.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.idempotency.backend import IdempotencyBackend
from atomic_agents.idempotency.filesystem import (
    FilesystemDedupLedger,
    IdempotencyBackendError,
)
from atomic_agents.idempotency.types import (
    COMPLETED,
    FRESH,
    IN_FLIGHT,
    DedupDecision,
    IdempotencyCapabilities,
    IdempotencyExport,
)
from atomic_agents.exceptions import BackendNotRegistered, PathTraversalError


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures

BACKEND_FACTORIES = {
    "filesystem": lambda agent_root: FilesystemDedupLedger(agent_root),
}


@pytest.fixture(params=list(BACKEND_FACTORIES.keys()))
def backend(request, tmp_path):
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    factory = BACKEND_FACTORIES[request.param]
    return factory(agent_root)


@pytest.fixture
def fs_backend(tmp_path):
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    return FilesystemDedupLedger(agent_root)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 1 — side-effect-free construction


def test_construction_is_side_effect_free(tmp_path) -> None:
    """Construction MUST NOT create any filesystem artifacts (spec/45 MUST 2)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    FilesystemDedupLedger(agent_root)
    # No idempotency/ directory should be created.
    assert not (agent_root / "idempotency").exists(), (
        "FilesystemDedupLedger construction MUST be side-effect-free"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 2 — lookup() FRESH for unknown key


def test_lookup_fresh_for_unknown_key(backend) -> None:
    """lookup() MUST return FRESH for a key that has never been seen (spec/45 MUST 6)."""
    decision = backend.lookup("my-idempotency-key")
    assert not decision.is_duplicate
    assert decision.state == FRESH
    assert decision.prior_run_id is None
    assert decision.prior_result_ref is None


# ──────────────────────────────────────────────────────────────────────────────
# TEST 3 — begin() FRESH for new key


def test_begin_fresh_for_new_key(backend) -> None:
    """begin() MUST return FRESH for a key that has never been seen (spec/45 MUST 4)."""
    decision = backend.begin("new-key", run_id="run-abc")
    assert not decision.is_duplicate
    assert decision.state == FRESH
    assert decision.prior_run_id is None
    assert decision.prior_result_ref is None


# ──────────────────────────────────────────────────────────────────────────────
# TEST 4 — DedupDecision has all required fields


def test_dedup_decision_has_required_fields() -> None:
    """DedupDecision must carry all four fields on every code path (spec/45 MUST 1)."""
    d = DedupDecision(
        is_duplicate=False, state=FRESH, prior_run_id=None, prior_result_ref=None
    )
    assert hasattr(d, "is_duplicate")
    assert hasattr(d, "state")
    assert hasattr(d, "prior_run_id")
    assert hasattr(d, "prior_result_ref")
    assert d.state == FRESH


# ──────────────────────────────────────────────────────────────────────────────
# TEST 5 — DedupDecision.state is REQUIRED (no default)


def test_dedup_decision_state_is_required() -> None:
    """DedupDecision.state MUST be required (no default) — spec/45 MUST 1.

    A missing state field turns the Protocol into a boolean that callers must
    re-query — split-brain with 'empty result is authoritative' (Lesson 9).
    """
    with pytest.raises(TypeError):
        DedupDecision(is_duplicate=False)  # type: ignore[call-arg]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 6 — begin() IN_FLIGHT for in-flight key


def test_begin_in_flight_for_claimed_key(backend) -> None:
    """begin() MUST return IN_FLIGHT when the key is already claimed (spec/45 MUST 4)."""
    # First claim — wins FRESH.
    first = backend.begin("race-key", run_id="run-1")
    assert first.state == FRESH

    # Second claim — must return IN_FLIGHT (not FRESH, not raise).
    second = backend.begin("race-key", run_id="run-2")
    assert second.is_duplicate
    assert second.state == IN_FLIGHT


# ──────────────────────────────────────────────────────────────────────────────
# TEST 7 — begin() COMPLETED for committed key


def test_begin_completed_after_commit(backend) -> None:
    """begin() MUST return COMPLETED when the key has a terminal marker (spec/45 MUST 4/5)."""
    backend.begin("commit-key", run_id="run-1")
    backend.commit("commit-key", result_ref="run-1-result")

    decision = backend.begin("commit-key", run_id="run-2")
    assert decision.is_duplicate
    assert decision.state == COMPLETED
    assert decision.prior_result_ref == "run-1-result"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 8 — commit() writes terminal, lookup() returns COMPLETED


def test_commit_then_lookup_completed(backend) -> None:
    """commit() MUST make lookup() return COMPLETED (spec/45 MUST 5)."""
    backend.begin("lookup-commit-key", run_id="run-x")
    backend.commit("lookup-commit-key", result_ref="the-result-ref")

    decision = backend.lookup("lookup-commit-key")
    assert decision.is_duplicate
    assert decision.state == COMPLETED
    assert decision.prior_result_ref == "the-result-ref"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 9 — commit() MARKER-ONLY: terminal file size is bounded


def test_commit_marker_only_no_result_content(tmp_path) -> None:
    """commit() MUST NOT store result content — marker-only (spec/45 MUST 5)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    backend.begin("marker-key", run_id="run-1")
    # Use a large-ish result_ref (but still valid, no separators).
    result_ref = "a" * 500
    backend.commit("marker-key", result_ref=result_ref)

    # Terminal file must exist and be small (marker-only).
    ledger_root = agent_root / "idempotency"
    terminal_files = list(ledger_root.glob("*.terminal.json"))
    assert len(terminal_files) == 1
    size = terminal_files[0].stat().st_size
    assert size < 4096, (
        f"terminal marker file is {size} bytes — too large for a MARKER-ONLY entry"
    )
    # Content must not contain large blobs.
    data = json.loads(terminal_files[0].read_text())
    assert "key" in data
    assert "terminal" in data
    assert data["terminal"] is True
    assert data["result_ref"] == result_ref


# ──────────────────────────────────────────────────────────────────────────────
# TEST 10 — begin() on empty ledger (first use) returns FRESH


def test_begin_empty_ledger_returns_fresh(tmp_path) -> None:
    """begin() on a brand-new agent (no idempotency/ dir) MUST return FRESH.

    This is the authoritative empty-is-FRESH contract. An absent ledger is NOT
    a fail-closed condition (Project Lesson 8/9).
    """
    agent_root = tmp_path / "fresh-agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    # No idempotency/ dir yet.
    assert not (agent_root / "idempotency").exists()

    decision = backend.begin("first-ever-key", run_id="run-1")
    assert not decision.is_duplicate
    assert decision.state == FRESH


# ──────────────────────────────────────────────────────────────────────────────
# TEST 11 — lookup() on absent ledger returns FRESH


def test_lookup_absent_ledger_returns_fresh(tmp_path) -> None:
    """lookup() on a brand-new agent MUST return FRESH (not raise, not fail-closed)."""
    agent_root = tmp_path / "fresh-agent-lookup"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    assert not (agent_root / "idempotency").exists()

    decision = backend.lookup("any-key")
    assert not decision.is_duplicate
    assert decision.state == FRESH


# ──────────────────────────────────────────────────────────────────────────────
# TEST 12 — capabilities() returns IdempotencyCapabilities


def test_capabilities_returns_idempotency_capabilities(backend) -> None:
    """capabilities() MUST return an IdempotencyCapabilities instance (spec/45 MUST 3)."""
    caps = backend.capabilities()
    assert isinstance(caps, IdempotencyCapabilities)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 13 — IdempotencyCapabilities is frozen dataclass


def test_idempotency_capabilities_is_frozen() -> None:
    """IdempotencyCapabilities MUST be a frozen dataclass."""
    caps = IdempotencyCapabilities(
        backend_id="test", single_host_only=True, atomic_claim=True
    )
    with pytest.raises((AttributeError, TypeError)):
        caps.backend_id = "other"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 14 — single_host_only REQUIRED (no default)


def test_single_host_only_is_required() -> None:
    """IdempotencyCapabilities.single_host_only MUST be required (no default).

    A backend that omits it must get a TypeError, not silently claim False
    (multi-host-safe when it is not) — LockCapabilities/QueueCapabilities pattern.
    """
    with pytest.raises(TypeError):
        IdempotencyCapabilities(backend_id="test", atomic_claim=True)  # type: ignore[call-arg]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 15 — atomic_claim REQUIRED (no default)


def test_atomic_claim_is_required() -> None:
    """IdempotencyCapabilities.atomic_claim MUST be required (no default).

    A backend that omits it must get a TypeError — the atomic-claim axis is
    always relevant for dedup correctness.
    """
    with pytest.raises(TypeError):
        IdempotencyCapabilities(backend_id="test", single_host_only=True)  # type: ignore[call-arg]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 16 — capabilities().atomic_claim consistent across calls


def test_atomic_claim_consistent_across_calls(backend) -> None:
    """capabilities().atomic_claim MUST be consistent across calls."""
    caps1 = backend.capabilities()
    caps2 = backend.capabilities()
    assert caps1.atomic_claim == caps2.atomic_claim


# ──────────────────────────────────────────────────────────────────────────────
# TEST 17 — capabilities().single_host_only consistent across calls


def test_single_host_only_consistent_across_calls(backend) -> None:
    """capabilities().single_host_only MUST be consistent across calls."""
    caps1 = backend.capabilities()
    caps2 = backend.capabilities()
    assert caps1.single_host_only == caps2.single_host_only


# ──────────────────────────────────────────────────────────────────────────────
# TEST 18 — supports_canonical_export=True for filesystem


def test_filesystem_advertises_canonical_export(fs_backend) -> None:
    """FilesystemDedupLedger MUST advertise supports_canonical_export=True (spec/40)."""
    caps = fs_backend.capabilities()
    assert caps.supports_canonical_export is True, (
        "FilesystemDedupLedger must advertise supports_canonical_export=True "
        "per spec/40 and spec/45"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 19 — export() returns IdempotencyExport


def test_export_returns_idempotency_export(backend) -> None:
    """export() MUST return an IdempotencyExport instance."""
    result = backend.export()
    assert isinstance(result, IdempotencyExport)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 20 — export() empty when ledger absent


def test_export_empty_when_ledger_absent(tmp_path) -> None:
    """export() MUST return empty entries when ledger directory is absent."""
    agent_root = tmp_path / "fresh"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    result = backend.export()
    assert isinstance(result, IdempotencyExport)
    assert result.entries_with_bytes == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 21 — export() excludes in-flight lease


def test_export_excludes_in_flight_lease(backend) -> None:
    """export() MUST NOT include in-flight lease entries (spec/45 export contract).

    In-flight entries exported and then imported would permanently block begin()
    on the restored system (phantom block).
    """
    backend.begin("in-flight-key", run_id="run-1")
    result = backend.export()
    # The in-flight key must NOT appear in the export.
    keys_in_export = [path_str for path_str, _ in result.entries_with_bytes]
    assert not any("in-flight-key" in k for k in keys_in_export), (
        "export() must not include in-flight lease entries"
    )
    # Also check by key hash: no .lease.json should appear.
    assert not any(
        path_str.endswith(".lease.json") for path_str, _ in result.entries_with_bytes
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 22 — export() includes terminal entry after commit


def test_export_includes_terminal_after_commit(backend) -> None:
    """export() MUST include terminal entries after commit() (spec/45 export contract)."""
    backend.begin("export-key", run_id="run-1")
    backend.commit("export-key", result_ref="result-ref-1")

    result = backend.export()
    assert len(result.entries_with_bytes) >= 1
    # The terminal entry must be present (as a .terminal.json).
    terminal_paths = [
        p for p, _ in result.entries_with_bytes if p.endswith(".terminal.json")
    ]
    assert len(terminal_paths) == 1


# ──────────────────────────────────────────────────────────────────────────────
# TEST 23 — export_all() equals export(None)


def test_export_all_equals_export_none(backend) -> None:
    """export_all() MUST be equivalent to export(None)."""
    backend.begin("export-all-key", run_id="run-a")
    backend.commit("export-all-key", result_ref="ref-a")

    result_all = backend.export_all()
    result_none = backend.export(None)
    assert result_all.entries_with_bytes == result_none.entries_with_bytes
    assert result_all.backend_id == result_none.backend_id
    assert result_all.scope == result_none.scope


# ──────────────────────────────────────────────────────────────────────────────
# TEST 24 — backend_id stable across calls


def test_backend_id_stable_across_calls(backend) -> None:
    """backend_id MUST be stable and consistent across calls."""
    id1 = backend.backend_id
    id2 = backend.backend_id
    assert id1 == id2
    assert isinstance(id1, str)
    assert len(id1) > 0


# ──────────────────────────────────────────────────────────────────────────────
# TEST 25 — storage isolation: two backends don't share state


def test_storage_isolation(tmp_path) -> None:
    """Two FilesystemDedupLedger backends with different agent_roots MUST NOT share state."""
    root_a = tmp_path / "agent-a"
    root_b = tmp_path / "agent-b"
    root_a.mkdir()
    root_b.mkdir()

    backend_a = FilesystemDedupLedger(root_a)
    backend_b = FilesystemDedupLedger(root_b)

    backend_a.begin("shared-key", run_id="run-a")
    backend_a.commit("shared-key", result_ref="ref-a")

    # backend_b must not see backend_a's entry.
    decision_b = backend_b.lookup("shared-key")
    assert not decision_b.is_duplicate
    assert decision_b.state == FRESH


# ──────────────────────────────────────────────────────────────────────────────
# TEST 26 — begin() race: O_EXCL barrier, exactly one wins FRESH


def test_begin_race_only_one_wins_fresh(tmp_path) -> None:
    """begin() MUST guarantee first-claim-wins via O_EXCL: exactly one FRESH under concurrency.

    Spawns two threads at a threading.Barrier — exactly one must win FRESH,
    the other must return IN_FLIGHT. This is the core dedup guarantee.
    """
    agent_root = tmp_path / "race-agent"
    agent_root.mkdir()

    results: list[DedupDecision] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def claim_worker(run_id: str) -> None:
        try:
            backend = FilesystemDedupLedger(agent_root)
            barrier.wait()
            decision = backend.begin("race-key", run_id=run_id)
            results.append(decision)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=claim_worker, args=("run-1",))
    t2 = threading.Thread(target=claim_worker, args=("run-2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Unexpected errors in race test: {errors}"
    assert len(results) == 2
    fresh_count = sum(1 for d in results if d.state == FRESH)
    in_flight_count = sum(1 for d in results if d.state == IN_FLIGHT)
    assert fresh_count == 1, f"Expected exactly 1 FRESH, got {fresh_count}: {results}"
    assert in_flight_count == 1, (
        f"Expected exactly 1 IN_FLIGHT, got {in_flight_count}: {results}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 27-30 — key validation


@pytest.mark.parametrize(
    "bad_key",
    [
        "../../escape",
        "sub/key",
        "a/b/c",
    ],
)
def test_begin_rejects_key_with_path_separator(bad_key, fs_backend) -> None:
    """begin() MUST reject keys containing path separators (spec/45 MUST 4)."""
    with pytest.raises(PathTraversalError):
        fs_backend.begin(bad_key, run_id="run-1")


def test_begin_rejects_empty_key(fs_backend) -> None:
    """begin() MUST reject empty key."""
    with pytest.raises(PathTraversalError):
        fs_backend.begin("", run_id="run-1")


def test_begin_rejects_dot_key(fs_backend) -> None:
    """begin() MUST reject '.' key."""
    with pytest.raises(PathTraversalError):
        fs_backend.begin(".", run_id="run-1")


def test_begin_rejects_dotdot_key(fs_backend) -> None:
    """begin() MUST reject '..' key."""
    with pytest.raises(PathTraversalError):
        fs_backend.begin("..", run_id="run-1")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 31 — commit() ACCEPTS a URI/path result_ref (spec/45 MUST 5)
#
# result_ref is an opaque reference stored as a JSON value, NEVER as a path
# component (the on-disk filename derives from sha256(key)). The documented
# intent is "run_id, path, URI" — so a URI or path result_ref MUST round-trip,
# not be rejected. (Previously these tests asserted rejection, which baked a
# spec/code contradiction into the protocol; inverted per the #520 PR1 round-2
# result_ref ruling.)


def test_commit_accepts_uri_result_ref(fs_backend) -> None:
    """commit() MUST accept a URI result_ref and round-trip it (spec/45 MUST 5)."""
    fs_backend.begin("commit-uri-key", run_id="run-1")
    fs_backend.commit("commit-uri-key", result_ref="s3://bucket/path/to/result.json")
    decision = fs_backend.lookup("commit-uri-key")
    assert decision.state == "completed"
    assert decision.prior_result_ref == "s3://bucket/path/to/result.json"


def test_commit_accepts_path_result_ref(fs_backend) -> None:
    """commit() MUST accept a filesystem-path result_ref and round-trip it."""
    fs_backend.begin("commit-path-key", run_id="run-1")
    fs_backend.commit("commit-path-key", result_ref="runs/2026/abc.json")
    decision = fs_backend.lookup("commit-path-key")
    assert decision.state == "completed"
    assert decision.prior_result_ref == "runs/2026/abc.json"


def test_commit_accepts_backslash_result_ref(fs_backend) -> None:
    """commit() MUST accept a backslash-containing result_ref (Windows path / URI)."""
    fs_backend.begin("commit-backslash-key", run_id="run-1")
    fs_backend.commit("commit-backslash-key", result_ref="runs\\2026\\abc.json")
    decision = fs_backend.lookup("commit-backslash-key")
    assert decision.state == "completed"
    assert decision.prior_result_ref == "runs\\2026\\abc.json"


def test_commit_rejects_overlong_result_ref(fs_backend) -> None:
    """commit() MUST reject a result_ref exceeding the length bound (caller-bug guard)."""
    fs_backend.begin("commit-overlong-key", run_id="run-1")
    with pytest.raises(IdempotencyBackendError):
        fs_backend.commit("commit-overlong-key", result_ref="x" * 5000)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 32-35 — _redact_for_error_message


def test_redact_url() -> None:
    """URL values MUST be redacted to scheme://... (spec/45 MUST 6)."""
    from atomic_agents.idempotency import _redact_for_error_message

    result = _redact_for_error_message("redis://user:password@host:6379/0")
    assert result == "redis://..."


def test_redact_dsn() -> None:
    """DSN values MUST be redacted to [redacted-connection-string] (spec/45 MUST 6)."""
    from atomic_agents.idempotency import _redact_for_error_message

    result = _redact_for_error_message("user:secret@host/dbname")
    assert result == "[redacted-connection-string]"


def test_redact_truncation() -> None:
    """Long values MUST be truncated at max_len (spec/45 MUST 6)."""
    from atomic_agents.idempotency import _redact_for_error_message

    long_value = "a" * 100
    result = _redact_for_error_message(long_value, max_len=32)
    assert len(result) <= 35  # max_len + "..."
    assert result.endswith("...")


def test_redact_passthrough_short() -> None:
    """Short non-URL values MUST pass through unchanged (spec/45 MUST 6)."""
    from atomic_agents.idempotency import _redact_for_error_message

    result = _redact_for_error_message("filesystem")
    assert result == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 36 — get_default_idempotency_backend() uses filesystem by default


def test_default_backend_is_filesystem(tmp_path) -> None:
    """get_default_idempotency_backend() MUST return FilesystemDedupLedger by default."""
    from atomic_agents.idempotency import get_default_idempotency_backend

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = get_default_idempotency_backend(agent_root)
    assert isinstance(backend, FilesystemDedupLedger)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 37 — get_idempotency_backend() raises BackendNotRegistered for unknown id


def test_get_idempotency_backend_raises_for_unknown() -> None:
    """get_idempotency_backend() MUST raise BackendNotRegistered for unknown id."""
    from atomic_agents.idempotency import get_idempotency_backend

    with pytest.raises(BackendNotRegistered):
        get_idempotency_backend("nonexistent-backend-xyz")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 38 — env var dispatches registered custom backend


def test_env_var_dispatches_custom_backend(tmp_path, monkeypatch) -> None:
    """ATOMIC_AGENTS_IDEMPOTENCY_BACKEND env var MUST dispatch to a registered backend."""
    from atomic_agents.idempotency import (
        get_default_idempotency_backend,
        register_idempotency_backend,
        unregister_idempotency_backend,
    )

    class FakeBackend:
        def __init__(self, agent_root: Path) -> None:
            self.agent_root = agent_root

        @property
        def backend_id(self) -> str:
            return "fake"

    register_idempotency_backend("fake", FakeBackend)
    try:
        monkeypatch.setenv("ATOMIC_AGENTS_IDEMPOTENCY_BACKEND", "fake")
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        backend = get_default_idempotency_backend(agent_root)
        assert isinstance(backend, FakeBackend)
    finally:
        unregister_idempotency_backend("fake")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 39 — unknown env var raises BackendNotRegistered


def test_unknown_env_var_raises(tmp_path, monkeypatch) -> None:
    """ATOMIC_AGENTS_IDEMPOTENCY_BACKEND with unknown value MUST raise BackendNotRegistered."""
    from atomic_agents.idempotency import get_default_idempotency_backend

    monkeypatch.setenv("ATOMIC_AGENTS_IDEMPOTENCY_BACKEND", "unknown-backend-xyz")
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    with pytest.raises(BackendNotRegistered):
        get_default_idempotency_backend(agent_root)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 40 — IdempotencyBackend is @runtime_checkable


def test_idempotency_backend_runtime_checkable(fs_backend) -> None:
    """IdempotencyBackend MUST be @runtime_checkable."""
    assert isinstance(fs_backend, IdempotencyBackend), (
        "FilesystemDedupLedger must satisfy IdempotencyBackend Protocol"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 41 — doctor.check_idempotency_backend PASS


def test_doctor_check_idempotency_backend_pass(tmp_path) -> None:
    """doctor.check_idempotency_backend MUST PASS for a valid agent root."""
    from atomic_agents.doctor import check_idempotency_backend, PASS

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    result = check_idempotency_backend(agent_root)
    assert result.status == PASS
    assert result.name == "idempotency-backend"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42 — doctor.check_idempotency_backend FAIL for bad env var


def test_doctor_check_idempotency_backend_fail_bad_env(tmp_path, monkeypatch) -> None:
    """doctor.check_idempotency_backend MUST FAIL when env var names unknown backend."""
    from atomic_agents.doctor import check_idempotency_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_IDEMPOTENCY_BACKEND", "bad-backend-xyz")
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    result = check_idempotency_backend(agent_root)
    assert result.status == FAIL
    assert result.name == "idempotency-backend"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 43 — doctor WARN single_host_only + MULTI_HOST


def test_doctor_warn_single_host_multi_host(tmp_path, monkeypatch) -> None:
    """doctor.check_idempotency_backend MUST WARN when single_host_only=True and
    ATOMIC_AGENTS_MULTI_HOST is set."""
    from atomic_agents.doctor import check_idempotency_backend, WARN

    monkeypatch.setenv("ATOMIC_AGENTS_MULTI_HOST", "true")
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    result = check_idempotency_backend(agent_root)
    assert result.status == WARN
    assert result.name == "idempotency-backend"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 44 — doctor dual-probe: begin() fail triggers FAIL (not PASS)
# Applied Project Lesson 7: dual-probe shape — verify each probe independently.


def test_doctor_dual_probe_begin_fail_triggers_fail(tmp_path) -> None:
    """doctor dual-probe: if begin() raises but lookup() succeeds, result is FAIL not PASS.

    This is the core dual-probe test: a mock backend where lookup() passes but
    begin() raises must produce FAIL — not PASS. Without begin() probe, a broken
    write path would false-PASS (Project Lesson 7).
    """
    from atomic_agents.doctor import check_idempotency_backend, FAIL
    from atomic_agents.idempotency import (
        register_idempotency_backend,
        unregister_idempotency_backend,
    )

    class LookupPassBeginFailBackend:
        """Mock backend: lookup() passes but begin() raises."""

        def __init__(self, agent_root: Path) -> None:
            self._agent_root = agent_root

        @property
        def backend_id(self) -> str:
            return "dual-probe-test"

        def lookup(self, key: str) -> DedupDecision:
            return DedupDecision(
                is_duplicate=False,
                state=FRESH,
                prior_run_id=None,
                prior_result_ref=None,
            )

        def begin(self, key: str, run_id: str) -> DedupDecision:
            raise IdempotencyBackendError("simulated begin() failure")

        def commit(self, key: str, result_ref: str) -> None:
            pass

        def export(self, query=None):
            return IdempotencyExport(
                entries_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        def export_all(self):
            return self.export(None)

        def capabilities(self) -> IdempotencyCapabilities:
            return IdempotencyCapabilities(
                backend_id=self.backend_id,
                single_host_only=True,
                atomic_claim=True,
                supports_canonical_export=False,
            )

    register_idempotency_backend("dual-probe-test", LookupPassBeginFailBackend)
    try:
        agent_root = tmp_path / "agent"
        agent_root.mkdir()

        with patch.dict(
            os.environ, {"ATOMIC_AGENTS_IDEMPOTENCY_BACKEND": "dual-probe-test"}
        ):
            result = check_idempotency_backend(agent_root)

        assert result.status == FAIL, (
            f"doctor must FAIL when begin() raises even if lookup() passes — got {result.status}"
        )
    finally:
        unregister_idempotency_backend("dual-probe-test")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 44b — doctor write round-trip runs against the REAL store (read-only FAILs)
# Regression guard for the dual-probe false-PASS (feedback_doctor_dual_probe_pattern):
# the begin()+commit() write arm MUST exercise the operator's real
# agent_root/idempotency/, NOT a disposable temp dir. A read-only real ledger
# must FAIL, not PASS. Negative control below proves the guard is not vacuous.


def test_doctor_fails_when_real_ledger_dir_read_only(tmp_path) -> None:
    """doctor MUST FAIL when the operator's real idempotency/ ledger is unwritable.

    The headline regression for the round-3 dual-probe fix: if the begin()+commit()
    write round-trip ran against a disposable temp dir (as a prior revision did),
    a read-only REAL ledger would false-PASS while runtime begin() raises. Pointing
    the backend at a chmod 0500 real ledger dir must surface FAIL.
    """
    import os as _os

    from atomic_agents.doctor import check_idempotency_backend, FAIL, PASS

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    # Pre-create idempotency/ and make it read-only so begin()'s O_EXCL create fails.
    ledger = agent_root / "idempotency"
    ledger.mkdir()
    ledger.chmod(0o500)  # read+exec only — no write
    try:
        # Skip on platforms/filesystems where chmod doesn't enforce W_OK (e.g. root).
        if _os.access(ledger, _os.W_OK):
            pytest.skip("chmod 0500 did not remove W_OK; running as root?")
        result = check_idempotency_backend(agent_root)
        assert result.status == FAIL, (
            "doctor must FAIL when the real idempotency/ ledger is read-only — "
            f"a temp-dir-only write probe would have false-PASSed. Got {result.status}"
        )
        assert result.name == "idempotency-backend"
    finally:
        ledger.chmod(0o700)  # restore so tmp_path cleanup can remove it

    # Negative control: a writable real ledger PASSes (proves the FAIL above is
    # caused by the read-only dir, not an unconditionally-failing probe).
    result_ok = check_idempotency_backend(agent_root)
    assert result_ok.status == PASS, (
        "doctor must PASS once the real ledger is writable again (negative control) "
        f"— got {result_ok.status}"
    )


def test_doctor_cleans_up_probe_markers_from_real_ledger(tmp_path) -> None:
    """doctor MUST NOT leave probe markers in the operator's real ledger.

    The write round-trip mutates the real store; doctor must unlink both the lease
    and terminal markers afterward (and must not create idempotency/ files that
    survive the check).
    """
    from atomic_agents.doctor import check_idempotency_backend, PASS

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    result = check_idempotency_backend(agent_root)
    assert result.status == PASS
    ledger = agent_root / "idempotency"
    leftover = list(ledger.glob("*.json")) if ledger.exists() else []
    assert leftover == [], f"doctor left probe markers in the real ledger: {leftover}"
    # The probe's begin() mkdir creates idempotency/; when the probe was the only
    # writer the dir must be removed too (no empty-dir residue on every run).
    assert not ledger.exists(), (
        f"doctor left an empty idempotency/ directory behind: {ledger}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 45 — IdempotencyExport importable from atomic_agents.export


def test_idempotency_export_importable_from_export() -> None:
    """IdempotencyExport MUST be importable from atomic_agents.export (spec/40)."""
    from atomic_agents.export import IdempotencyExport as IEFromExport

    assert IEFromExport is IdempotencyExport, (
        "IdempotencyExport must be re-exported from atomic_agents.export "
        "per spec/40 public surface convention"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 46 — WritePolicy NOT in IdempotencyBackend Protocol surface


def test_write_policy_not_in_protocol_surface() -> None:
    """WritePolicy MUST NOT be part of the IdempotencyBackend Protocol surface.

    The ledger path is fixed at construction. Mirrors QueueBackend and GoalBackend.
    """
    from atomic_agents.idempotency.backend import IdempotencyBackend as IB

    assert not hasattr(IB, "write_policy"), (
        "WritePolicy must NOT appear in the IdempotencyBackend Protocol surface"
    )


def test_write_policy_not_in_filesystem_impl() -> None:
    """WritePolicy MUST NOT appear on FilesystemDedupLedger either."""
    assert not hasattr(FilesystemDedupLedger, "write_policy"), (
        "WritePolicy must NOT appear on FilesystemDedupLedger"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 47 — supports_ttl=False for filesystem (PR1)


def test_filesystem_supports_ttl_false(fs_backend) -> None:
    """FilesystemDedupLedger MUST advertise supports_ttl=False in PR1.

    The TTL sweep is a follow-up PR. Advertising True without an implementation
    would be a false capability claim (Principle 13: docs match reality).
    """
    caps = fs_backend.capabilities()
    assert caps.supports_ttl is False, (
        "FilesystemDedupLedger must advertise supports_ttl=False in PR1 "
        "(sweep is a follow-up)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 48 — begin() is VALUE OBJECT: never raises for duplicate detection


def test_begin_value_object_never_raises_for_duplicate(backend) -> None:
    """begin() MUST return DedupDecision for ALL non-error paths, never raise.

    This is the load-bearing Protocol contract (spec/45 MUST 1). Callers
    must be able to branch on DedupDecision.state, not try/except.
    """
    # Claim a key.
    decision1 = backend.begin("value-object-key", run_id="run-1")
    assert decision1.state == FRESH

    # Second call on same key MUST return DedupDecision, not raise.
    try:
        decision2 = backend.begin("value-object-key", run_id="run-2")
    except Exception as e:
        pytest.fail(
            f"begin() raised {type(e).__name__} for a duplicate key — "
            f"it MUST return DedupDecision(is_duplicate=True) instead"
        )
    assert isinstance(decision2, DedupDecision)
    assert decision2.is_duplicate
    assert decision2.state == IN_FLIGHT


# ──────────────────────────────────────────────────────────────────────────────
# TEST 49 — error-path branch assertion: typed log line via caplog
# Applied Project Lesson 2: layered-except typed-branch tests must assert
# a branch-distinctive log line, not just the return value.


def test_unreadable_lease_returns_in_flight_with_log(tmp_path, caplog) -> None:
    """Unreadable lease file MUST return IN_FLIGHT and emit a distinctive log line.

    Project Lesson 2: if except IdempotencyBackendError and broad except Exception
    both return DedupDecision(state='in_flight'), a test asserting only the return
    value is false-green. Assert the branch-distinctive log message to confirm the
    typed handler (not the backstop) fired.
    """
    agent_root = tmp_path / "corrupt-agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # Claim a key to create the lease file.
    backend.begin("corrupt-key", run_id="run-1")

    # Corrupt the lease file so JSON parsing fails.
    ledger_root = agent_root / "idempotency"
    lease_files = list(ledger_root.glob("*.lease.json"))
    assert len(lease_files) == 1
    lease_files[0].write_bytes(b"NOT VALID JSON {{{")

    # Now a second begin() must detect the corrupt lease and return IN_FLIGHT.
    with caplog.at_level(logging.ERROR, logger="atomic_agents.idempotency.filesystem"):
        decision = backend.begin("corrupt-key", run_id="run-2")

    # Assert return value.
    assert decision.is_duplicate
    assert decision.state == IN_FLIGHT

    # Assert branch-distinctive log line (Project Lesson 2).
    assert any(
        "unreadable" in record.message and "IN_FLIGHT" in record.message
        for record in caplog.records
    ), (
        "Expected a log.error containing 'unreadable' and 'IN_FLIGHT' — "
        "the typed handler must emit a distinctive log line, not just return a value"
    )


def test_doctor_registered_in_run_doctor(tmp_path) -> None:
    """run_doctor() MUST include check_idempotency_backend in its output."""
    from atomic_agents.doctor import run_doctor

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    # Create minimal agent structure so run_doctor doesn't crash on other checks.
    results = run_doctor(agent_root)
    check_names = [r.name for r in results]
    assert "idempotency-backend" in check_names, (
        "run_doctor() must include 'idempotency-backend' check"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 50 — commit() is idempotent: first-commit-wins preserves prior_run_id
# (Principle 5 audit-link preservation). A redelivered/retried commit() (the
# at-least-once shape PR2 must tame) MUST NOT clobber the original terminal.


def test_double_commit_preserves_prior_run_id(backend) -> None:
    """A second commit() MUST be a no-op preserving the original prior_run_id.

    The first commit() unlinks the lease, so a second commit() resolves
    prior_run_id=None. If commit() overwrote the terminal, the audit link back to
    the originating run would be severed. First-commit-wins keeps it intact.
    """
    backend.begin("double-commit-key", run_id="r1")
    backend.commit("double-commit-key", "ref1")
    # Redelivered/retried commit with a different result_ref and no lease present.
    backend.commit("double-commit-key", "ref2")

    decision = backend.lookup("double-commit-key")
    assert decision.state == COMPLETED
    assert decision.prior_run_id == "r1", (
        "first-commit-wins: second commit() must not null out prior_run_id"
    )
    assert decision.prior_result_ref == "ref1", (
        "first-commit-wins: second commit() must not overwrite result_ref"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 51 — key validation rejects NUL bytes and C0 control characters
# (defense-in-depth: a NUL can truncate a path at the syscall boundary).


@pytest.mark.parametrize("bad_char", ["\x00", "\x01", "\x1f"])
def test_key_rejects_nul_and_control_chars(backend, bad_char) -> None:
    """begin/lookup/commit MUST reject keys with NUL/control chars (PathTraversalError)."""
    bad_key = f"key{bad_char}suffix"
    with pytest.raises(PathTraversalError):
        backend.begin(bad_key, run_id="run-1")
    with pytest.raises(PathTraversalError):
        backend.lookup(bad_key)
    with pytest.raises(PathTraversalError):
        backend.commit(bad_key, "ref")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 52 — prior_run_id is carried on IN_FLIGHT and COMPLETED decisions
# (Red Team coverage: the audit-link field must survive both the lease read and
# the terminal read).


def test_prior_run_id_carried_in_flight_and_completed(backend) -> None:
    """IN_FLIGHT (from lease) and COMPLETED (from terminal) MUST carry prior_run_id."""
    # First claim establishes the lease with run_id='r1'.
    first = backend.begin("prior-run-key", run_id="r1")
    assert first.state == FRESH

    # Second begin() reads the lease → IN_FLIGHT and MUST surface prior_run_id='r1'.
    in_flight = backend.begin("prior-run-key", run_id="r2")
    assert in_flight.state == IN_FLIGHT
    assert in_flight.prior_run_id == "r1", (
        "IN_FLIGHT decision must carry the originating run_id from the lease"
    )

    # After commit, both begin() and lookup() read the terminal → COMPLETED and
    # MUST surface prior_run_id='r1' (recovered from the lease at commit time).
    backend.commit("prior-run-key", "ref-1")
    completed_begin = backend.begin("prior-run-key", run_id="r3")
    assert completed_begin.state == COMPLETED
    assert completed_begin.prior_run_id == "r1", (
        "COMPLETED decision (via begin) must carry the originating run_id"
    )
    completed_lookup = backend.lookup("prior-run-key")
    assert completed_lookup.state == COMPLETED
    assert completed_lookup.prior_run_id == "r1", (
        "COMPLETED decision (via lookup) must carry the originating run_id"
    )

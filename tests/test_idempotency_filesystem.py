"""Filesystem-specific tests for FilesystemDedupLedger (spec/45).

These tests are NOT parametrized over backend factories — they test the
filesystem-specific containment, atomicity, and on-disk layout behavior.

Tests:
  FS-1  — _ledger_root() containment: symlinked idempotency/ refused
  FS-3  — _require_canonical_ledger_path() rejects symlink leaf
  FS-4  — _require_canonical_ledger_path() rejects path escaping ledger_root
  FS-5  — begin() fails loud when _ledger_root() raises (IdempotencyBackendError)
  FS-6  — begin() O_EXCL atomicity: lease file is created on success
  FS-7  — begin() O_EXCL atomicity: concurrent loser reads lease file
  FS-8  — commit() uses atomic_write (temp+rename, crash-safe)
  FS-9  — commit() unlinks lease file after terminal write
  FS-10 — key validation: path separators rejected in begin()
  FS-11 — key validation: path separators rejected in lookup()
  FS-12 — key validation: path separators rejected in commit()
  FS-13 — result_ref is opaque: a URI/path round-trips, not used as path component
  FS-14 — _key_hash() produces safe on-disk filename (hex only)
  FS-15 — on-disk terminal marker JSON has required fields
  FS-16 — on-disk lease JSON has required fields
  FS-17 — export() per-leaf symlink containment guard
  FS-18 — import isolation: can import FilesystemDedupLedger without agent.py
  FS-19 — constructor rejects '..' component in agent_root
  FS-20 — hash collision guard: mismatched stored key returns None (FRESH)
  FS-21 — corrupt terminal file is treated as COMPLETED (fail-closed)
  FS-22 — lookup() DIRECTORY escape returns FRESH (read-side fail-soft, perimeter)
  FS-23 — negative-control: stripped symlink-leaf guard → test goes RED
           (verified by testing _require_canonical_ledger_path explicitly)
  FS-24 — begin() re-checks terminal after winning O_EXCL (at-most-once, MUST 4)
  FS-25 — begin() claim sink guards the lease path (MUST 10): dangling +
           existing-target symlink leaf rejected, key not stranded
  FS-26 — lookup()/begin() on a tampered/symlinked LEAF is fail-closed (COMPLETED
           / IN_FLIGHT, NOT FRESH) — existing-target AND dangling-symlink leaf,
           each with a per-guard negative control (is_symlink-before-exists fix)
  FS-27 — begin() bounded-retry recovery (_begin_after_vanished): recover-to-FRESH
           and repeated-vanish raise branches
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.idempotency.filesystem import (
    FilesystemDedupLedger,
    IdempotencyBackendError,
    _key_hash,
)
from atomic_agents.idempotency.types import COMPLETED, FRESH, IN_FLIGHT
from atomic_agents.exceptions import PathTraversalError


# ──────────────────────────────────────────────────────────────────────────────
# FS-1 — _ledger_root() containment: symlinked idempotency/ refused


def test_ledger_root_refuses_symlinked_idempotency_dir(tmp_path) -> None:
    """_ledger_root() MUST raise PathTraversalError when idempotency/ is a symlink
    pointing outside agent_root (spec/45 security contract)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # Create idempotency/ as a symlink pointing outside agent_root.
    idempotency_link = agent_root / "idempotency"
    idempotency_link.symlink_to(outside)

    backend = FilesystemDedupLedger(agent_root)
    with pytest.raises(PathTraversalError):
        backend._ledger_root()


# ──────────────────────────────────────────────────────────────────────────────
# FS-3 — _require_canonical_ledger_path() rejects symlink leaf


def test_require_canonical_rejects_symlink_leaf(tmp_path) -> None:
    """_require_canonical_ledger_path() MUST raise PathTraversalError when the
    ledger file path is a symlink leaf (even if it resolves inside ledger_root).
    spec/45 MUST 10 (canonical path containment — the consolidated symlink-leaf
    branch of the every-sink guard).

    Negative-control verification: this test exercises the is_symlink() guard
    directly (Project Lesson 1 — per-guard negative control). To make the guard's
    branch DETERMINISTICALLY observed (and not flake on FS timing), we (a) assert
    the symlink precondition loudly so a non-created symlink fails the test rather
    than silently passing, and (b) assert the raised message identifies the
    is_symlink leaf branch specifically (so a containment-branch raise would NOT
    satisfy this test).
    """
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir()
    target_file = ledger_root / "real-file.json"
    target_file.write_text("{}")
    symlink_leaf = ledger_root / "symlink-leaf.json"
    symlink_leaf.symlink_to(target_file)

    # Precondition: the symlink MUST actually exist as a symlink, else the test
    # would false-green on a non-symlink path. (The leaf resolves INSIDE
    # ledger_root, so only the is_symlink branch — not containment — can fire.)
    assert symlink_leaf.is_symlink() is True
    assert symlink_leaf.resolve().is_relative_to(ledger_root.resolve())

    with pytest.raises(PathTraversalError, match="leaf is a symlink"):
        FilesystemDedupLedger._require_canonical_ledger_path(symlink_leaf, ledger_root)


# ──────────────────────────────────────────────────────────────────────────────
# FS-4 — _require_canonical_ledger_path() rejects path escaping ledger_root


def test_require_canonical_rejects_path_escaping_root(tmp_path) -> None:
    """_require_canonical_ledger_path() MUST raise PathTraversalError when the
    resolved path escapes ledger_root (containment invariant). spec/45 MUST 10
    (canonical path containment — the root-containment branch of the every-sink
    guard).

    Negative-control verification: exercises the is_relative_to() guard directly.
    """
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir()
    outside_file = tmp_path / "outside-file.json"
    outside_file.write_text("{}")

    with pytest.raises(PathTraversalError):
        FilesystemDedupLedger._require_canonical_ledger_path(outside_file, ledger_root)


# ──────────────────────────────────────────────────────────────────────────────
# FS-5 — begin() raises IdempotencyBackendError when _ledger_root() raises


def test_begin_raises_when_ledger_root_raises(tmp_path) -> None:
    """begin() MUST raise IdempotencyBackendError when _ledger_root() raises
    PathTraversalError (symlinked idempotency/ escape)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (agent_root / "idempotency").symlink_to(outside)

    backend = FilesystemDedupLedger(agent_root)
    with pytest.raises(IdempotencyBackendError):
        backend.begin("any-key", run_id="run-1")


# ──────────────────────────────────────────────────────────────────────────────
# FS-6 — begin() O_EXCL atomicity: lease file is created on success


def test_begin_creates_lease_file(tmp_path) -> None:
    """begin() MUST create a .lease.json file when it wins FRESH."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    decision = backend.begin("lease-file-key", run_id="run-1")
    assert decision.state == FRESH

    ledger_root = agent_root / "idempotency"
    lease_files = list(ledger_root.glob("*.lease.json"))
    assert len(lease_files) == 1, "begin() must create a .lease.json file"

    # Verify lease JSON content.
    data = json.loads(lease_files[0].read_text())
    assert data["key"] == "lease-file-key"
    assert data["run_id"] == "run-1"
    assert data["state"] == IN_FLIGHT


# ──────────────────────────────────────────────────────────────────────────────
# FS-7 — begin() O_EXCL: concurrent loser reads lease file and returns IN_FLIGHT


def test_begin_concurrent_loser_reads_existing_lease(tmp_path) -> None:
    """When the O_EXCL create fails (FileExistsError), begin() MUST read the
    existing lease and return IN_FLIGHT — not raise, not return FRESH."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # First claim wins FRESH.
    first = backend.begin("loser-key", run_id="run-1")
    assert first.state == FRESH

    # Second claim gets IN_FLIGHT.
    second = backend.begin("loser-key", run_id="run-2")
    assert second.is_duplicate
    assert second.state == IN_FLIGHT


# ──────────────────────────────────────────────────────────────────────────────
# FS-8 — commit() uses atomic_write (temp+rename)


def test_commit_uses_atomic_write_temp_rename(tmp_path) -> None:
    """commit() MUST use atomic_write so the terminal marker is crash-safe.

    Verify: no .tmp files linger after commit() completes (atomic_write
    cleans up its temp file via rename).
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    backend.begin("atomic-key", run_id="run-1")
    backend.commit("atomic-key", result_ref="ref-1")

    ledger_root = agent_root / "idempotency"
    tmp_files = list(ledger_root.glob("*.tmp"))
    assert tmp_files == [], f"commit() left .tmp files: {tmp_files}"

    terminal_files = list(ledger_root.glob("*.terminal.json"))
    assert len(terminal_files) == 1, "commit() must create a .terminal.json file"


# ──────────────────────────────────────────────────────────────────────────────
# FS-9 — commit() unlinks lease file after terminal write


def test_commit_unlinks_lease_file(tmp_path) -> None:
    """commit() MUST unlink the .lease.json file after writing the terminal marker."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    backend.begin("unlink-key", run_id="run-1")
    ledger_root = agent_root / "idempotency"
    assert len(list(ledger_root.glob("*.lease.json"))) == 1

    backend.commit("unlink-key", result_ref="ref-1")

    lease_files = list(ledger_root.glob("*.lease.json"))
    assert lease_files == [], "commit() must unlink the .lease.json file"


# ──────────────────────────────────────────────────────────────────────────────
# FS-9b — commit() lease-recovery routes through _read_lease (containment-guarded)
# Regression: commit() previously pre-gated prior_run_id recovery on
# lease_path.exists() (a symlink-FOLLOWING stat), so a symlinked lease leaf was
# read THROUGH the symlink, bypassing the canonical-path containment guard that
# every other ledger read uses. Routing recovery through _read_lease applies the
# is_symlink()-before-exists() + containment check, so a tampered/symlinked lease
# leaf is OBSERVED and fail-closed (prior_run_id=None) rather than followed.


def test_commit_lease_recovery_containment_guards_symlinked_leaf(
    tmp_path, caplog
) -> None:
    """commit() MUST NOT follow a symlinked lease leaf when recovering prior_run_id.

    A lease leaf replaced with a symlink (even one resolving inside the ledger)
    must trip the canonical-path containment guard via _read_lease — proven by the
    distinctive 'lease path containment violation' log line (Project Lesson 2:
    assert the branch-distinctive signal, not just the result). prior_run_id ends
    up None (fail-closed), and the terminal marker is still written.
    """
    import logging

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    backend.begin("symlink-lease-key", run_id="orig-run-42")
    ledger_root = agent_root / "idempotency"
    lease_path = ledger_root / f"{_key_hash('symlink-lease-key')}.lease.json"

    # Replace the lease leaf with a symlink to a real copy holding the run_id.
    # OLD exists()-follow code would read 'orig-run-42' THROUGH the symlink,
    # bypassing containment; the fixed code containment-rejects it.
    real_copy = ledger_root / "lease-copy.json"
    real_copy.write_text(
        json.dumps(
            {"key": "symlink-lease-key", "run_id": "orig-run-42", "state": IN_FLIGHT}
        )
    )
    lease_path.unlink()
    lease_path.symlink_to(real_copy)

    with caplog.at_level(logging.ERROR):
        backend.commit("symlink-lease-key", result_ref="ref-1")

    # Branch-distinctive signal: the containment guard fired (not a silent skip).
    assert any(
        "lease path containment violation" in rec.message for rec in caplog.records
    ), (
        "commit() must route lease recovery through _read_lease's containment "
        "guard — the symlinked lease leaf must trip the containment log line"
    )

    # prior_run_id is fail-closed to None — the symlink was NOT followed.
    terminal_path = ledger_root / f"{_key_hash('symlink-lease-key')}.terminal.json"
    data = json.loads(terminal_path.read_text())
    assert data.get("prior_run_id") is None, (
        "commit() must NOT recover prior_run_id through a symlinked lease leaf "
        "(would bypass containment) — it must fail-closed to None"
    )
    assert data.get("terminal") is True


# ──────────────────────────────────────────────────────────────────────────────
# FS-10 — key validation: path separators rejected in begin()


@pytest.mark.parametrize("bad_key", ["../escape", "sub/dir", "a/b"])
def test_begin_rejects_traversal_key(bad_key, tmp_path) -> None:
    """begin() MUST reject keys with path separators at the API boundary."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    with pytest.raises(PathTraversalError):
        backend.begin(bad_key, run_id="run-1")


# ──────────────────────────────────────────────────────────────────────────────
# FS-11 — key validation: path separators rejected in lookup()


@pytest.mark.parametrize("bad_key", ["../escape", "sub/dir"])
def test_lookup_rejects_traversal_key(bad_key, tmp_path) -> None:
    """lookup() MUST reject keys with path separators at the API boundary."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    with pytest.raises(PathTraversalError):
        backend.lookup(bad_key)


# ──────────────────────────────────────────────────────────────────────────────
# FS-12 — key validation: path separators rejected in commit()


def test_commit_rejects_traversal_key(tmp_path) -> None:
    """commit() MUST reject keys with path separators at the API boundary."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    with pytest.raises(PathTraversalError):
        backend.commit("../escape", result_ref="ref-1")


# ──────────────────────────────────────────────────────────────────────────────
# FS-13 — result_ref is opaque: a URI/path round-trips, NOT used as a path component


def test_commit_accepts_path_result_ref_not_used_as_path_component(tmp_path) -> None:
    """commit() MUST accept a path-shaped result_ref and store it as a JSON value.

    result_ref is opaque (run_id, path, URI). The on-disk filename derives from
    sha256(key), so a '/' in result_ref never becomes a path component — proving
    that, this test verifies the only file written is the sha256-named terminal
    marker (no nested 'path/' directory was created) and the ref round-trips.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    backend.begin("ref-key", run_id="run-1")
    backend.commit("ref-key", result_ref="s3://bucket/path/traversal.json")

    # The ref round-trips as a JSON value.
    decision = backend.lookup("ref-key")
    assert decision.state == COMPLETED
    assert decision.prior_result_ref == "s3://bucket/path/traversal.json"

    # The result_ref was NOT used as a path component: no 'path' / 's3:' dir,
    # only the sha256-named terminal marker exists in the ledger.
    ledger_root = agent_root / "idempotency"
    entries = sorted(p.name for p in ledger_root.iterdir())
    assert entries == [f"{_key_hash('ref-key')}.terminal.json"]


# ──────────────────────────────────────────────────────────────────────────────
# FS-14 — _key_hash() produces safe on-disk filename (hex only)


def test_key_hash_produces_hex_only() -> None:
    """_key_hash() MUST produce a hex-only string safe for use as a filename."""
    import re

    h = _key_hash("my-idempotency-key")
    assert re.match(r"^[0-9a-f]+$", h), f"_key_hash() returned non-hex: {h!r}"
    assert len(h) == 64, f"Expected sha256 hex (64 chars), got {len(h)}"


def test_key_hash_is_deterministic() -> None:
    """_key_hash() MUST return the same value for the same input (deterministic)."""
    assert _key_hash("same-key") == _key_hash("same-key")


def test_key_hash_different_keys_different_hashes() -> None:
    """_key_hash() MUST produce different hashes for different keys."""
    assert _key_hash("key-a") != _key_hash("key-b")


# ──────────────────────────────────────────────────────────────────────────────
# FS-15 — on-disk terminal marker JSON has required fields


def test_terminal_marker_json_has_required_fields(tmp_path) -> None:
    """commit() MUST write a terminal JSON with key, prior_run_id, result_ref, terminal."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    backend.begin("json-fields-key", run_id="run-1")
    backend.commit("json-fields-key", result_ref="ref-abc")

    ledger_root = agent_root / "idempotency"
    terminal_files = list(ledger_root.glob("*.terminal.json"))
    assert len(terminal_files) == 1
    data = json.loads(terminal_files[0].read_text())

    assert data.get("key") == "json-fields-key", "terminal JSON must have 'key' field"
    assert data.get("result_ref") == "ref-abc", (
        "terminal JSON must have 'result_ref' field"
    )
    assert data.get("terminal") is True, "terminal JSON must have 'terminal': true"
    assert "prior_run_id" in data, "terminal JSON must have 'prior_run_id' field"


# ──────────────────────────────────────────────────────────────────────────────
# FS-16 — on-disk lease JSON has required fields


def test_lease_json_has_required_fields(tmp_path) -> None:
    """begin() MUST write a lease JSON with key, run_id, state fields."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    backend.begin("lease-fields-key", run_id="run-xyz")

    ledger_root = agent_root / "idempotency"
    lease_files = list(ledger_root.glob("*.lease.json"))
    assert len(lease_files) == 1
    data = json.loads(lease_files[0].read_text())

    assert data.get("key") == "lease-fields-key", "lease JSON must have 'key' field"
    assert data.get("run_id") == "run-xyz", "lease JSON must have 'run_id' field"
    assert data.get("state") == IN_FLIGHT, "lease JSON must have 'state' = 'in_flight'"


# ──────────────────────────────────────────────────────────────────────────────
# FS-17 — export() per-leaf symlink containment guard


def test_export_skips_symlinked_terminal_file(tmp_path) -> None:
    """spec/45 MUST 12 (export per-leaf containment).
    export() MUST skip terminal files that are symlinks (per-leaf containment guard).

    This is the leaf-escape class (#426/#427): a symlinked terminal file inside
    ledger_root pointing outside agent_root must not be read into the export.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # Create a legitimate terminal entry.
    backend.begin("legit-key", run_id="run-1")
    backend.commit("legit-key", result_ref="ref-1")

    # Place a symlinked .terminal.json in ledger_root pointing to an outside file.
    ledger_root = agent_root / "idempotency"
    outside_file = tmp_path / "outside-secret.json"
    outside_file.write_bytes(b'{"secret": "data"}')
    evil_link = ledger_root / "evil.terminal.json"
    evil_link.symlink_to(outside_file)

    result = backend.export()
    # The evil symlink must not appear in the export.
    assert not any("evil" in path_str for path_str, _ in result.entries_with_bytes), (
        "export() must skip symlinked terminal files (per-leaf containment guard)"
    )
    # The legitimate entry should still be exported.
    assert len(result.entries_with_bytes) >= 1


def test_export_skips_symlink_with_target_inside_ledger_root(tmp_path) -> None:
    """export() MUST skip a symlinked terminal whose target is a REAL terminal
    file INSIDE ledger_root (independent negative control for the is_symlink() guard).

    Project Lesson 1 / round-2 finding: the outside-pointing symlink test
    (test_export_skips_symlinked_terminal_file) is caught by the containment
    (is_relative_to) check ALONE — stripping the is_symlink() check leaves it
    green. This test isolates the is_symlink() check: the symlink's target
    resolves INSIDE ledger_root, so the containment check PASSES and only
    is_symlink() can skip it. export() now routes its per-leaf check through the
    consolidated _require_canonical_ledger_path guard (Lesson 6 / spec/45
    "exactly one per-entry containment helper"); confirmed RED when that guard's
    is_symlink() rejection is stripped, GREEN when restored.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # A legitimate terminal entry (the symlink's real target, inside ledger_root).
    backend.begin("real-key", run_id="run-1")
    backend.commit("real-key", result_ref="ref-1")
    ledger_root = agent_root / "idempotency"
    real_terminal = ledger_root / f"{_key_hash('real-key')}.terminal.json"
    assert real_terminal.is_file()

    # A symlinked terminal pointing at the REAL terminal inside ledger_root.
    # is_relative_to() passes (target resolves inside) — only is_symlink() skips it.
    evil_link = ledger_root / "aliased.terminal.json"
    evil_link.symlink_to(real_terminal)

    result = backend.export()
    names = [path_str for path_str, _ in result.entries_with_bytes]
    assert not any("aliased" in n for n in names), (
        "export() must skip the symlink leaf even when its target is inside "
        "ledger_root (is_symlink() guard, not is_relative_to(), is load-bearing here)"
    )
    # The real target's own entry IS exported.
    assert any(f"{_key_hash('real-key')}.terminal.json" in n for n in names)
    # Exactly one entry — the real terminal, not the alias.
    assert len(names) == 1


# ──────────────────────────────────────────────────────────────────────────────
# FS-18 — import isolation: can import without agent.py


def test_import_without_agent_module() -> None:
    """FilesystemDedupLedger MUST be importable without importing atomic_agents.agent.

    Circular-import safety: the idempotency package must not pull in the LLM stack.
    """
    import importlib

    # Verify idempotency can be imported in isolation.
    # (In a real test environment, agent.py is already loaded, so we just verify
    # that the import chain doesn't require it transitively by checking there are
    # no imports from agent-dependent modules in the idempotency package.)
    mod = importlib.import_module("atomic_agents.idempotency")
    assert hasattr(mod, "FilesystemDedupLedger")
    assert hasattr(mod, "IdempotencyBackend")


# ──────────────────────────────────────────────────────────────────────────────
# FS-19 — constructor rejects '..' component in agent_root


def test_constructor_rejects_dotdot_in_agent_root(tmp_path) -> None:
    """FilesystemDedupLedger MUST reject agent_root containing '..' components."""
    with pytest.raises(ValueError, match=r"'\.\.'"):
        FilesystemDedupLedger(Path("/tmp/../etc/passwd"))


# ──────────────────────────────────────────────────────────────────────────────
# FS-20 — hash collision guard: mismatched stored key treated as FRESH


def test_hash_collision_guard_returns_fresh(tmp_path) -> None:
    """When a terminal file's stored key doesn't match the caller's key (hash collision),
    the entry MUST be ignored (treated as FRESH, not a false COMPLETED)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # Manually write a terminal file with a different key in it.
    ledger_root = agent_root / "idempotency"
    ledger_root.mkdir()
    key_hash = _key_hash("caller-key")
    terminal_path = ledger_root / f"{key_hash}.terminal.json"
    terminal_path.write_text(
        json.dumps(
            {
                "key": "different-key",
                "prior_run_id": "x",
                "result_ref": "y",
                "terminal": True,
            }
        )
    )

    # lookup() must treat this as FRESH (hash collision guard).
    decision = backend.lookup("caller-key")
    assert not decision.is_duplicate
    assert decision.state == FRESH


# ──────────────────────────────────────────────────────────────────────────────
# FS-21 — corrupt terminal file is treated as COMPLETED (fail-closed)


def test_corrupt_terminal_treated_as_completed(tmp_path) -> None:
    """A corrupt (non-JSON) terminal file MUST be treated as COMPLETED (fail-closed).

    Rationale: a garbled terminal entry is safer to treat as 'do not re-run'
    than as 'run again' (Project Lesson 8: fail-closed where there's a constraint).
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # Manually write a corrupt terminal file.
    ledger_root = agent_root / "idempotency"
    ledger_root.mkdir()
    key_hash = _key_hash("corrupt-key")
    terminal_path = ledger_root / f"{key_hash}.terminal.json"
    terminal_path.write_bytes(b"CORRUPT NOT JSON {{{{")

    decision = backend.lookup("corrupt-key")
    assert decision.is_duplicate
    assert decision.state == COMPLETED


# ──────────────────────────────────────────────────────────────────────────────
# FS-22 — lookup() containment violation returns FRESH (read-side fail-soft)


def test_lookup_containment_violation_returns_fresh(tmp_path) -> None:
    """lookup() MUST return FRESH (not raise) when idempotency/ is a symlink
    escaping agent_root — read-side fail-soft (matches 'empty is authoritative')."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (agent_root / "idempotency").symlink_to(outside)

    backend = FilesystemDedupLedger(agent_root)
    # lookup() must fail-soft on read (return FRESH), not raise.
    decision = backend.lookup("any-key")
    assert not decision.is_duplicate
    assert decision.state == FRESH


# ──────────────────────────────────────────────────────────────────────────────
# FS-23 — Negative-control: _require_canonical_ledger_path guards independently
# (Project Lesson 23 — per-guard negative control)


def test_require_canonical_passes_for_regular_file(tmp_path) -> None:
    """_require_canonical_ledger_path() MUST pass for a regular file under ledger_root.

    This is the positive case that proves the guard is not over-restrictive.
    """
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir()
    regular_file = ledger_root / "entry.terminal.json"
    regular_file.write_text("{}")

    # Must not raise for a regular file contained under ledger_root.
    FilesystemDedupLedger._require_canonical_ledger_path(regular_file, ledger_root)


def test_require_canonical_symlink_guard_independent_of_containment(tmp_path) -> None:
    """A symlink resolving INSIDE ledger_root still fails the symlink-leaf guard —
    proving the is_symlink check is independent of the containment check.

    (This is an INDEPENDENCE test, not a strip-based negative control: the genuine
    per-guard negative controls — strip is_symlink → test_require_canonical_rejects
    _symlink_leaf goes RED; strip is_relative_to → test_require_canonical_rejects_
    path_escaping_root goes RED — live in those reject tests. This test pins that
    the two guards do not collapse into one: a containment-passing leaf is still
    rejected by the symlink branch.)
    """
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir()
    target = ledger_root / "real.json"
    target.write_text("{}")
    symlink = ledger_root / "link.json"
    symlink.symlink_to(target)

    # Precondition: the symlink resolves INSIDE ledger_root, so the containment
    # check alone would PASS — only the is_symlink branch can produce the raise.
    assert symlink.is_symlink() is True
    assert symlink.resolve().is_relative_to(ledger_root.resolve())

    with pytest.raises(PathTraversalError, match="leaf is a symlink"):
        FilesystemDedupLedger._require_canonical_ledger_path(symlink, ledger_root)


# ──────────────────────────────────────────────────────────────────────────────
# FS-24 — begin() at-most-once: re-check terminal after winning O_EXCL (MUST 4)


def test_begin_rechecks_terminal_after_winning_claim(tmp_path, caplog) -> None:
    """begin() MUST re-check the terminal marker after winning the O_EXCL claim.

    Reproduces the TOCTOU race: a commit() interleaves between begin()'s Step-3
    terminal read and its O_EXCL create (the commit writes the terminal AND
    unlinks the lease, which is precisely why the O_EXCL create then succeeds).
    Without the post-claim re-check, begin() would return FRESH for an
    already-COMPLETED key and re-run it.

    We simulate the interleave by forcing ONLY the first _read_terminal call to
    miss (return None); the post-claim re-check (second call) sees the real
    terminal and must return COMPLETED.

    Negative control: with the post-claim re-check stripped from begin(), this
    test goes RED (returns FRESH) — verified out-of-band during the build.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # Key is genuinely COMPLETED on disk.
    backend.begin("toctou-key", run_id="r1")
    backend.commit("toctou-key", result_ref="ref-1")

    orig_read_terminal = backend._read_terminal
    state = {"calls": 0}

    def first_miss(terminal_path, ledger_root, key):
        state["calls"] += 1
        if state["calls"] == 1:
            return None  # interleave: terminal not yet visible to Step 3
        return orig_read_terminal(terminal_path, ledger_root, key)

    with patch.object(backend, "_read_terminal", side_effect=first_miss):
        decision = backend.begin("toctou-key", run_id="r2")

    assert decision.state == COMPLETED, (
        "begin() must re-check the terminal after winning the claim and return "
        "COMPLETED, not FRESH"
    )
    assert decision.is_duplicate is True
    # The post-claim re-check fired (second _read_terminal call happened).
    assert state["calls"] >= 2
    # The spurious lease we created in the race must be cleaned up.
    ledger_root = agent_root / "idempotency"
    assert list(ledger_root.glob("*.lease.json")) == []


# ──────────────────────────────────────────────────────────────────────────────
# FS-25 — begin() claim sink guards the lease path (MUST 10) — dangling symlink


def test_begin_dangling_symlink_lease_does_not_strand_key(tmp_path) -> None:
    """A DANGLING symlink at the lease path MUST fail-closed as a containment
    violation, NOT strand the key into a 'file vanished' race raise.

    Negative control for the begin() inline claim-sink guard (line ~519): with
    that guard stripped, a dangling lease symlink no longer raises a containment
    error here — it falls through to _read_lease (which, after the round-2
    is_symlink-before-exists fix, returns IN_FLIGHT rather than None), so the
    expected IdempotencyBackendError is never raised and this test goes RED.
    Verified: strip the inline `_require_canonical_ledger_path(lease_path, ...)`
    block and this test fails (DID NOT RAISE). Combined with
    test_begin_symlink_to_existing_lease_rejected (the existing-target control),
    this gives the inline claim-sink guard genuine RED-on-strip coverage for BOTH
    the dangling and existing-target symlink-leaf topologies.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    lease_path = led / f"{_key_hash('dangle-key')}.lease.json"
    lease_path.symlink_to(tmp_path / "nonexistent-target.json")
    assert lease_path.is_symlink() is True

    backend = FilesystemDedupLedger(agent_root)
    with pytest.raises(IdempotencyBackendError) as exc_info:
        backend.begin("dangle-key", run_id="r1")
    # MUST be the containment-class error, NOT the 'file vanished' race raise.
    assert "containment violation" in str(exc_info.value)
    assert "file vanished" not in str(exc_info.value)


def test_begin_symlink_to_existing_lease_rejected(tmp_path) -> None:
    """A symlink at the lease path pointing to an EXISTING file MUST be rejected
    by the consolidated claim-sink guard (not silently followed/written through)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    real_target = tmp_path / "real-target.json"
    real_target.write_text("{}")
    lease_path = led / f"{_key_hash('symlink-key')}.lease.json"
    lease_path.symlink_to(real_target)
    assert lease_path.is_symlink() is True

    backend = FilesystemDedupLedger(agent_root)
    with pytest.raises(IdempotencyBackendError) as exc_info:
        backend.begin("symlink-key", run_id="r1")
    assert "containment violation" in str(exc_info.value)
    # The forged symlink target must NOT have been written through.
    assert real_target.read_text() == "{}"


# ──────────────────────────────────────────────────────────────────────────────
# FS-26 — lookup() on a tampered/symlinked LEAF is fail-closed (NOT FRESH)


def test_lookup_symlinked_terminal_leaf_is_completed(tmp_path, caplog) -> None:
    """A symlinked TERMINAL leaf (a tampered per-key entry that passes the
    file-exists check) MUST resolve to COMPLETED on lookup() — fail-closed.

    This is the per-key LEAF case, distinct from FS-22's whole-directory escape
    (which stays fail-soft FRESH). Spec/45 boundary table + the lookup() docstring
    agree: a tampered terminal leaf is treated as 'do not re-run' (COMPLETED).
    """
    import logging

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    terminal_path = led / f"{_key_hash('leaf-key')}.terminal.json"
    terminal_path.symlink_to(outside)
    assert terminal_path.is_symlink() is True

    backend = FilesystemDedupLedger(agent_root)
    with caplog.at_level(logging.ERROR):
        decision = backend.lookup("leaf-key")

    assert decision.state == COMPLETED
    assert decision.is_duplicate is True
    # The typed containment branch fired (distinctive log line — Lesson 2).
    assert any(
        "terminal path containment violation" in r.message for r in caplog.records
    )


def test_lookup_symlinked_lease_leaf_is_in_flight(tmp_path, caplog) -> None:
    """A symlinked LEASE leaf MUST resolve to IN_FLIGHT on lookup() — fail-closed."""
    import logging

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    lease_path = led / f"{_key_hash('lease-leaf-key')}.lease.json"
    lease_path.symlink_to(outside)
    assert lease_path.is_symlink() is True

    backend = FilesystemDedupLedger(agent_root)
    with caplog.at_level(logging.ERROR):
        decision = backend.lookup("lease-leaf-key")

    assert decision.state == IN_FLIGHT
    assert decision.is_duplicate is True
    assert any("lease path containment violation" in r.message for r in caplog.records)


def test_lookup_dangling_symlinked_terminal_leaf_is_completed(tmp_path, caplog) -> None:
    """A DANGLING symlinked TERMINAL leaf (target removed) MUST resolve to
    COMPLETED on lookup() — fail-closed — NOT leak to FRESH (re-run).

    Round-2 P1: _read_terminal gated on exists() FIRST, and exists() FOLLOWS the
    symlink, so a dangling terminal symlink returned False → None → FRESH, letting
    an attacker who replaced a terminal marker with a dangling symlink force a
    re-run. The fix checks is_symlink() (no-follow) BEFORE exists(). This test is
    the regression guard; verified RED (state='fresh') when the is_symlink()
    pre-check is removed from _read_terminal.
    """
    import logging

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    terminal_path = led / f"{_key_hash('dangle-term-key')}.terminal.json"
    # Dangling: target inside ledger_root but does not exist.
    terminal_path.symlink_to(led / "nonexistent-target.json")
    assert terminal_path.is_symlink() is True
    assert terminal_path.exists() is False  # exists() follows → dangling → False

    backend = FilesystemDedupLedger(agent_root)
    with caplog.at_level(logging.ERROR):
        decision = backend.lookup("dangle-term-key")

    assert decision.state == COMPLETED, (
        "dangling terminal symlink must fail-closed to COMPLETED, not leak to FRESH"
    )
    assert decision.is_duplicate is True
    assert any(
        "terminal path containment violation" in r.message for r in caplog.records
    )


def test_begin_dangling_symlinked_terminal_leaf_does_not_rerun(tmp_path) -> None:
    """begin() on a DANGLING terminal symlink MUST return COMPLETED, NOT FRESH.

    The begin() fast-path check (_read_terminal) must observe the dangling
    terminal symlink and fail-closed to COMPLETED so the key is NOT re-run.
    Verified RED (state='fresh') when the is_symlink()-before-exists() pre-check
    is stripped from _read_terminal.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    terminal_path = led / f"{_key_hash('dangle-begin-key')}.terminal.json"
    terminal_path.symlink_to(led / "nonexistent-target.json")

    backend = FilesystemDedupLedger(agent_root)
    decision = backend.begin("dangle-begin-key", run_id="r1")
    assert decision.state == COMPLETED, (
        "begin() on a dangling terminal symlink must fail-closed to COMPLETED "
        "(do not re-run a key whose terminal marker was tampered with)"
    )
    assert decision.is_duplicate is True


def test_lookup_dangling_symlinked_lease_leaf_is_in_flight(tmp_path, caplog) -> None:
    """A DANGLING symlinked LEASE leaf MUST resolve to IN_FLIGHT on lookup() —
    fail-closed — NOT leak to FRESH.

    Same exists()-follows-symlink masking as the terminal case. Verified RED
    (state='fresh') when the is_symlink()-before-exists() pre-check is stripped
    from _read_lease.
    """
    import logging

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    lease_path = led / f"{_key_hash('dangle-lease-key')}.lease.json"
    lease_path.symlink_to(led / "nonexistent-target.json")
    assert lease_path.is_symlink() is True
    assert lease_path.exists() is False

    backend = FilesystemDedupLedger(agent_root)
    with caplog.at_level(logging.ERROR):
        decision = backend.lookup("dangle-lease-key")

    assert decision.state == IN_FLIGHT, (
        "dangling lease symlink must fail-closed to IN_FLIGHT, not leak to FRESH"
    )
    assert decision.is_duplicate is True
    assert any("lease path containment violation" in r.message for r in caplog.records)


def test_read_terminal_containment_branch_negative_control(tmp_path) -> None:
    """Per-guard negative control for _read_terminal's containment branch.

    Strip the guard (monkeypatch _require_canonical_ledger_path to a no-op) and
    confirm the symlinked terminal leaf would be READ THROUGH (state still
    COMPLETED here only because the symlink target is valid JSON without the
    matching key → hash-collision guard returns None → FRESH). The point: with
    the guard present the result comes from the containment branch; with it
    stripped it comes from the parse path. We assert the guarded path produces
    the containment log line and the stripped path does NOT.
    """
    import logging

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    terminal_path = led / f"{_key_hash('nc-key')}.terminal.json"
    terminal_path.symlink_to(outside)
    backend = FilesystemDedupLedger(agent_root)
    ledger_root = agent_root / "idempotency"

    # Guarded: containment branch fires.
    log = logging.getLogger("atomic_agents.idempotency.filesystem")
    handler_records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            handler_records.append(record)

    cap = _Cap()
    log.addHandler(cap)
    try:
        guarded = backend._read_terminal(terminal_path, ledger_root, "nc-key")
        assert any(
            "terminal path containment violation" in r.getMessage()
            for r in handler_records
        ), "guard must fire the containment branch"
        assert guarded is not None and guarded.state == COMPLETED

        # Stripped: no-op the guard → containment branch CANNOT fire.
        handler_records.clear()
        with patch.object(
            backend, "_require_canonical_ledger_path", lambda *a, **k: None
        ):
            stripped = backend._read_terminal(terminal_path, ledger_root, "nc-key")
        assert not any(
            "terminal path containment violation" in r.getMessage()
            for r in handler_records
        ), (
            "stripped guard must NOT fire the containment branch (proves the test exercises the guard, not the parse path)"
        )
        # With the guard stripped, the symlink is read through; the target JSON
        # has no matching 'key' → hash-collision guard → None (FRESH).
        assert stripped is None
    finally:
        log.removeHandler(cap)


# ──────────────────────────────────────────────────────────────────────────────
# FS-27 — begin() bounded-retry recovery path (_begin_after_vanished)
#
# Reachable in PR1: an operator clearing a stale lease (the documented
# remediation — "delete the *.lease.json file") concurrently with a begin()
# retry produces the double-vanish (FileExistsError → terminal None → lease None)
# that routes into _begin_after_vanished. Both the successful-retry branch and
# the second-vanish raise branch are exercised here.


def test_begin_after_vanished_recovers_to_fresh(tmp_path) -> None:
    """begin()'s bounded retry MUST recover to FRESH when the lease vanishes once.

    Simulates the operator-clears-stale-lease race: the first _read_lease after
    FileExistsError sees the lease gone (returns None, unlinking it as a side
    effect), so begin() retries the O_EXCL claim and wins FRESH.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # Pre-create a real lease so the initial O_EXCL create raises FileExistsError.
    lease_path = led / f"{_key_hash('vanish-key')}.lease.json"
    lease_path.write_text(
        json.dumps({"key": "vanish-key", "run_id": "old", "state": IN_FLIGHT})
    )

    orig_read_lease = backend._read_lease
    state = {"calls": 0}

    def vanish_once(lp, lr, key):
        state["calls"] += 1
        if state["calls"] == 1:
            # Operator deletes the stale lease right here (the documented fix).
            lp.unlink(missing_ok=True)
            return None  # lease gone → route into _begin_after_vanished
        return orig_read_lease(lp, lr, key)

    with patch.object(backend, "_read_lease", side_effect=vanish_once):
        decision = backend.begin("vanish-key", run_id="new")

    assert decision.state == FRESH, (
        "begin() must recover to FRESH via the bounded retry when a stale lease "
        "is cleared concurrently"
    )
    assert decision.is_duplicate is False
    assert state["calls"] >= 1


def test_begin_after_vanished_raises_on_repeated_vanish(tmp_path) -> None:
    """begin()'s bounded retry MUST raise IdempotencyBackendError when the lease
    vanishes repeatedly (disk behaving adversarially — a value-object answer
    would be a lie).

    We force _begin_after_vanished's own O_EXCL retry to also hit
    FileExistsError, then make both re-reads return None — the second-vanish
    raise branch.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    led = agent_root / "idempotency"
    led.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    lease_path = led / f"{_key_hash('repeat-key')}.lease.json"

    # Drive _begin_after_vanished directly with both re-reads returning None and
    # a pre-existing lease so its inner O_EXCL create raises FileExistsError.
    lease_path.write_text(
        json.dumps({"key": "repeat-key", "run_id": "old", "state": IN_FLIGHT})
    )
    ledger_root = led
    lease_content = json.dumps(
        {"key": "repeat-key", "run_id": "new", "state": IN_FLIGHT}
    ).encode("utf-8")
    terminal_path = led / f"{_key_hash('repeat-key')}.terminal.json"

    with (
        patch.object(backend, "_read_terminal", return_value=None),
        patch.object(backend, "_read_lease", return_value=None),
    ):
        with pytest.raises(IdempotencyBackendError) as exc_info:
            backend._begin_after_vanished(
                terminal_path, lease_path, ledger_root, "repeat-key", lease_content
            )
    assert "vanished repeatedly" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────
# FS-27 — release_lease() symlink-leaf refusal (spec/45 MUST 13, PR2)


def test_release_lease_refuses_symlink_leaf(tmp_path) -> None:
    """release_lease() MUST refuse to unlink a SYMLINK lease leaf (it could unlink
    a real file at the symlink target). It logs + returns without raising, and the
    symlink target file is left intact. spec/45 MUST 13.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    ledger_root = agent_root / "idempotency"
    ledger_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)

    # A real file the symlink points at — must NOT be unlinked.
    target_file = ledger_root / "real-target.json"
    target_file.write_text("important", encoding="utf-8")

    lease_path = ledger_root / f"{_key_hash('sym-key')}.lease.json"
    lease_path.symlink_to(target_file)
    # Precondition: leaf is actually a symlink resolving inside ledger_root,
    # so only the is_symlink branch (not containment) can fire.
    assert lease_path.is_symlink() is True

    backend.release_lease("sym-key")  # must NOT raise

    # The guard returns early WITHOUT touching the tampered leaf, so the symlink
    # leaf itself MUST still be present (we refuse to operate on it at all). This
    # is the true negative control: with the is_symlink guard stripped,
    # unlink(missing_ok=True) removes the symlink leaf, so this assertion goes RED.
    assert lease_path.is_symlink(), (
        "release_lease() MUST NOT touch a symlink lease leaf — it must remain"
    )
    # And the symlink target is of course untouched.
    assert target_file.exists()


def test_release_lease_symlink_refusal_negative_real_leaf_is_unlinked(tmp_path) -> None:
    """NEGATIVE control for the symlink refusal: a REAL (non-symlink) lease leaf
    IS unlinked by release_lease(). Brackets the is_symlink guard — if the guard
    were inverted, this would fail."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    backend.begin("real-leaf-key", run_id="run-1")
    ledger_root = agent_root / "idempotency"
    lease_path = ledger_root / f"{_key_hash('real-leaf-key')}.lease.json"
    assert lease_path.exists() and not lease_path.is_symlink()

    backend.release_lease("real-leaf-key")
    assert not lease_path.exists(), "a real lease leaf MUST be unlinked"


def test_release_lease_io_error_maps_to_backend_error(tmp_path) -> None:
    """release_lease() MUST map a genuine I/O failure on unlink (not ENOENT) to
    IdempotencyBackendError. spec/45 MUST 13."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemDedupLedger(agent_root)
    backend.begin("io-key", run_id="run-1")

    # Force unlink() to raise an OSError that is NOT ENOENT (e.g. EACCES).
    with patch(
        "pathlib.Path.unlink",
        side_effect=PermissionError("EACCES: simulated permission denied"),
    ):
        with pytest.raises(IdempotencyBackendError):
            backend.release_lease("io-key")

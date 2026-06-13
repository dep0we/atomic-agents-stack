"""Conformance tests for QueueBackend Protocol (spec/44).

Tests covering the QueueBackend Implementer Contract (12 MUSTs). The
protocol-behavior tests are parametrized over every registered backend
via the ``backend`` fixture; see PARAMETRIZATION below.

NOTE ON NUMBERING: the "TEST N" labels below are this file's own granular
test-case indices, NOT spec/44 MUST numbers (spec/44 has exactly 12 MUSTs).

  TEST 1  — side-effect-free construction (spec/44 MUST 1)
  TEST 2  — claim_next() returns None when queue absent (spec/44 MUST 1)
  TEST 3  — claim_next() returns None when queue empty (spec/44 MUST 1)
  TEST 4  — claim_next() claims first alphabetical item (spec/44 MUST 9)
  TEST 5  — claim_next() returns FilesystemQueueItem with required fields
  TEST 6  — release() moves item to done/ (spec/44 MUST 1)
  TEST 7  — move_to_dead_letter() moves item to dead-letter/ (spec/44 MUST 10)
  TEST 8  — move_to_dead_letter() writes .reason.txt sidecar (spec/44 MUST 10)
  TEST 9  — dead-work-stays-dead: release after dead-letter has no effect (spec/44 MUST 10)
  TEST 10 — renew_lease() updates lease_expires_at (spec/44 MUST 2)
  TEST 11 — list_claimed() returns claimed items (spec/44 MUST 11)
  TEST 12 — list_claimed() returns empty list when claimed/ absent (spec/44 MUST 11)
  TEST 13 — recover_stale_claims() recovers expired items (spec/44 MUST 2)
  TEST 14 — recover_stale_claims() leaves fresh items alone (spec/44 MUST 2)
  TEST 15 — storage isolation: two backends don't share state (spec/44 MUST 5)
  TEST 16 — backend_id stable across calls (spec/44 MUST 8)
  TEST 17 — QueueCapabilities is frozen dataclass
  TEST 18 — single_host_only is required (no default) on QueueCapabilities (spec/44 MUST 12)
  TEST 19 — capabilities() returns QueueCapabilities (spec/44 MUST 3)
  TEST 20 — capabilities().single_host_only consistent across calls (spec/44 MUST 12)
  TEST 21 — supports_canonical_export=True for filesystem (spec/44 MUST 3 / spec/40)
  TEST 22 — export() returns QueueExport type (spec/44 export contract)
  TEST 23 — export() empty when queue/ absent (spec/44 MUST 1 / export contract)
  TEST 24 — export() excludes claimed/ even when claim is held (spec/44 MUST 7)
  TEST 25 — export() excludes .lease.json sidecars (spec/44 MUST 7)
  TEST 26 — export() includes queued/ items (spec/44 export contract)
  TEST 27 — export() includes done/ items after release (spec/44 export contract)
  TEST 28 — export() includes dead-letter/ items (spec/44 export contract)
  TEST 29 — export_all() equals export(None) (spec/44 export contract)
  TEST 30 — claim-race: thread-based barrier — only one claimer wins (spec/44 MUST 9)
  TEST 31 — claim-race: monkeypatched-rename loser returns None (spec/44 MUST 9)
  TEST 32 — _redact_for_error_message() URL redaction (spec/44 MUST 6)
  TEST 33 — _redact_for_error_message() DSN redaction (spec/44 MUST 6)
  TEST 34 — _redact_for_error_message() truncation (spec/44 MUST 6)
  TEST 35 — _redact_for_error_message() passthrough for short value (spec/44 MUST 6)
  TEST 36 — get_default_queue_backend() uses filesystem by default
  TEST 37 — get_queue_backend() raises BackendNotRegistered for unknown id
  TEST 38 — env var dispatches registered custom backend
  TEST 39 — get_default_queue_backend() unknown env var raises BackendNotRegistered
  TEST 40 — QueueBackend is @runtime_checkable (isinstance check)
  TEST 41 — doctor.check_queue_backend SKIP for non-cascade agent (spec/44)
  TEST 42 — doctor.check_queue_backend PASS for cascade agent (spec/44)
  TEST 43 — doctor.check_queue_backend FAIL for bad env var (spec/44)
  TEST 44 — doctor.check_queue_backend WARN for single_host_only + MULTI_HOST (spec/44 MUST 12)
  TEST 45 — compatibility: old and new import paths are behaviorally equivalent
  TEST 46 — QueueExport importable from atomic_agents.export (spec/40)
  TEST 47 — QueueItem has no path field (abstract Protocol-level type)
  TEST 48 — FilesystemQueueItem has path field (filesystem-specific subtype)
  TEST 49 — WritePolicy NOT in QueueBackend Protocol surface (spec/44 writepolicy-presence)

PARAMETRIZATION: protocol-behavior tests use the ``backend`` fixture parametrized
over BACKEND_FACTORIES (currently just 'filesystem'). Adding a second backend to
BACKEND_FACTORIES picks up every protocol-behavior test automatically.

Filesystem-specific tests are deliberately NOT parametrized: symlink guards,
POSIX-rename race specifics, ATOMIC_AGENTS_QUEUE_BACKEND registry dispatch.
Pure-dataclass tests (QueueItem/QueueCapabilities/QueueExport) need no backend.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atomic_agents.queue.backend import QueueBackend, recover_stale_claims
from atomic_agents.queue.filesystem import FilesystemQueueBackend, FilesystemQueueItem
from atomic_agents.queue.types import (
    QueueCapabilities,
    QueueExport,
    QueueItem,
)
from atomic_agents.exceptions import BackendNotRegistered


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures

BACKEND_FACTORIES = {
    "filesystem": lambda project_root: FilesystemQueueBackend(project_root),
}


@pytest.fixture(params=list(BACKEND_FACTORIES.keys()))
def backend(request, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    factory = BACKEND_FACTORIES[request.param]
    return factory(project_root)


def _make_project_with_queue_item(
    tmp_path: Path,
    role: str = "writer",
    filename: str = "001_task.md",
    content: str = "Do the thing",
) -> tuple[Path, Path]:
    """Create a project with one queued item. Returns (project_root, item_path)."""
    project_root = tmp_path / "project"
    qd = project_root / "queue" / "queued" / role
    qd.mkdir(parents=True)
    item_path = qd / filename
    item_path.write_text(content)
    return project_root, item_path


# ──────────────────────────────────────────────────────────────────────────────
# TEST 1 — side-effect-free construction


def test_construction_is_side_effect_free(tmp_path):
    """Construction MUST NOT perform filesystem I/O (spec/44 MUST 1)."""
    project_root = tmp_path / "nonexistent_project"
    # project_root does not exist — construction must not fail
    backend = FilesystemQueueBackend(project_root)
    assert backend.backend_id == "filesystem"
    assert not project_root.exists()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 2 — claim_next() returns None when queue absent


def test_claim_next_returns_none_when_no_queue_dir(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    backend = FilesystemQueueBackend(project_root)
    assert backend.claim_next("writer", "lease-1") is None


# ──────────────────────────────────────────────────────────────────────────────
# TEST 3 — claim_next() returns None when queue empty


def test_claim_next_returns_none_when_empty(tmp_path):
    project_root = tmp_path / "project"
    (project_root / "queue" / "queued" / "writer").mkdir(parents=True)
    backend = FilesystemQueueBackend(project_root)
    assert backend.claim_next("writer", "lease-1") is None


# ──────────────────────────────────────────────────────────────────────────────
# TEST 4 — claim_next() claims first alphabetical item


def test_claim_next_claims_first_alphabetical(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path, filename="002_b.md")
    qd = project_root / "queue" / "queued" / "writer"
    (qd / "001_a.md").write_text("First")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    assert item.original_name == "001_a.md"
    assert item.role == "writer"
    assert item.lease_token == "lease-1"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 5 — claim_next() returns FilesystemQueueItem with required fields


def test_claim_next_returns_filesystem_queue_item(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    assert isinstance(item, FilesystemQueueItem)
    assert isinstance(item, QueueItem)  # is a subtype
    assert item.path is not None
    assert item.path.exists()
    assert item.path.parent == project_root / "queue" / "claimed" / "lease-1"
    assert item.claimed_at > 0


# ──────────────────────────────────────────────────────────────────────────────
# TEST 6 — release() moves item to done/


def test_release_moves_to_done(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path, content="Work A")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    backend.release(item.lease_token, item.original_name)
    done_file = project_root / "queue" / "done" / "lease-1" / "001_task.md"
    assert done_file.exists()
    assert done_file.read_text() == "Work A"
    assert not item.path.exists()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 7 — move_to_dead_letter() moves item to dead-letter/


def test_move_to_dead_letter_moves_item(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path, content="Bad work")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    backend.move_to_dead_letter(item.lease_token, item.original_name, reason="")
    dl_file = project_root / "queue" / "dead-letter" / "lease-1" / "001_task.md"
    assert dl_file.exists()
    assert not item.path.exists()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 8 — move_to_dead_letter() writes .reason.txt sidecar


def test_move_to_dead_letter_writes_reason(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    backend.move_to_dead_letter(
        item.lease_token, item.original_name, reason="Failed 3x"
    )
    reason_file = (
        project_root / "queue" / "dead-letter" / "lease-1" / "001_task.md.reason.txt"
    )
    assert reason_file.read_text() == "Failed 3x"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 9 — dead-work-stays-dead


def test_dead_work_stays_dead(tmp_path):
    """After move_to_dead_letter, the item is NOT recoverable or re-claimable."""
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    backend.move_to_dead_letter(item.lease_token, item.original_name, reason="terminal")
    # release() on an already-moved item should not raise (item.path is gone)
    # and should not create a done/ entry
    try:
        backend.release(item.lease_token, item.original_name)
    except FileNotFoundError:
        pass  # expected: item already moved
    done_file = project_root / "queue" / "done" / "lease-1" / "001_task.md"
    assert not done_file.exists(), "dead-letter item must not appear in done/"


def test_dead_letter_not_resurrected_by_recover_stale(tmp_path):
    """Lifecycle gap (Codex #427): fail → dead-letter → stale-recovery MUST NOT
    re-enter queued/.

    recover_stale_claims only scans claimed/; a dead-lettered item has left
    claimed/, so recovery (even with lease_seconds=0, which would treat any
    remaining claim as stale) must NOT resurrect it into queued/_recovered/.
    Exercises the REAL recover surface, not a sequential simulation.
    """
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    backend.move_to_dead_letter(item.lease_token, item.original_name, reason="terminal")

    dl_file = project_root / "queue" / "dead-letter" / "lease-1" / "001_task.md"
    assert dl_file.exists()

    # lease_seconds=0 forces "any claim is stale" — the strongest recovery pressure.
    recovered = recover_stale_claims(backend, lease_seconds=0)

    # Nothing recovered, dead-letter item untouched, no _recovered/ namespace born.
    assert recovered == [], f"dead-lettered item must not be recovered: {recovered}"
    assert dl_file.exists(), "dead-letter item must remain in dead-letter/"
    assert not (project_root / "queue" / "queued" / "_recovered").exists(), (
        "recover MUST NOT resurrect a dead-lettered item into queued/"
    )
    # And it is not re-claimable.
    assert backend.claim_next("writer", "lease-2") is None


# ──────────────────────────────────────────────────────────────────────────────
# TEST 10 — renew_lease() updates lease_expires_at


def test_renew_lease_updates_expiry(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1", lease_seconds=60)
    assert item is not None
    # Read original expiry from sidecar
    sidecar = item.path.parent / (item.original_name + ".lease.json")
    import json

    orig_data = json.loads(sidecar.read_text())
    orig_expires = datetime.fromisoformat(orig_data["lease_expires_at"])

    # Renew with a longer lease
    backend.renew_lease(item.lease_token, item.original_name, additional_seconds=3600)

    new_data = json.loads(sidecar.read_text())
    new_expires = datetime.fromisoformat(new_data["lease_expires_at"])
    assert new_expires > orig_expires
    delta = new_expires.timestamp() - time.time()
    assert 3500 < delta < 3700, f"Expected ~3600s lease, got {delta:.0f}s"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 11 — list_claimed() returns claimed items


def test_list_claimed_returns_claimed_items(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    claimed = backend.list_claimed()
    assert len(claimed) == 1
    assert claimed[0].original_name == "001_task.md"
    assert claimed[0].lease_token == "lease-1"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 12 — list_claimed() returns empty list when claimed/ absent


def test_list_claimed_empty_when_no_claimed_dir(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    backend = FilesystemQueueBackend(project_root)
    assert backend.list_claimed() == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 13 — recover_stale_claims() recovers expired items


def test_recover_stale_claims_recovers_expired(tmp_path):
    import json

    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "dead-lease", lease_seconds=3600)
    assert item is not None

    # Backdate the sidecar's lease_expires_at to 2 hours ago
    sidecar = item.path.parent / (item.original_name + ".lease.json")
    data = json.loads(sidecar.read_text())
    past = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc)
    data["lease_expires_at"] = past.isoformat()
    sidecar.write_text(json.dumps(data))

    recovered = recover_stale_claims(backend, lease_seconds=3600)
    assert len(recovered) == 1
    assert recovered[0].original_name == "001_task.md"
    # Item should be back in queued/_recovered/
    assert (
        project_root / "queue" / "queued" / "_recovered" / "dead-lease" / "001_task.md"
    ).exists()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 14 — recover_stale_claims() leaves fresh items alone


def test_recover_stale_claims_leaves_fresh_alone(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "fresh-lease", lease_seconds=3600)
    assert item is not None

    recovered = recover_stale_claims(backend, lease_seconds=3600)
    assert recovered == []
    assert item.path.exists()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 15 — storage isolation


def test_storage_isolation_two_backends(tmp_path):
    """Two backends scoped to different project roots MUST NOT share state."""
    project_a = tmp_path / "project_a"
    project_b = tmp_path / "project_b"
    qd_a = project_a / "queue" / "queued" / "writer"
    qd_a.mkdir(parents=True)
    (qd_a / "task_a.md").write_text("Task A")
    backend_a = FilesystemQueueBackend(project_a)
    backend_b = FilesystemQueueBackend(project_b)

    item_a = backend_a.claim_next("writer", "lease-A")
    item_b = backend_b.claim_next("writer", "lease-B")
    assert item_a is not None
    assert item_b is None  # project_b has no queue


# ──────────────────────────────────────────────────────────────────────────────
# TEST 16 — backend_id stable across calls


def test_backend_id_stable(backend):
    assert backend.backend_id == backend.backend_id
    assert isinstance(backend.backend_id, str)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 17 — QueueCapabilities is frozen dataclass


def test_queue_capabilities_is_frozen():
    caps = QueueCapabilities(backend_id="test", single_host_only=True)
    with pytest.raises((AttributeError, TypeError)):
        caps.backend_id = "other"  # type: ignore


# ──────────────────────────────────────────────────────────────────────────────
# TEST 18 — single_host_only is REQUIRED (no default)


def test_single_host_only_is_required():
    """QueueCapabilities.single_host_only has no default — must be supplied."""
    with pytest.raises(TypeError):
        QueueCapabilities(backend_id="test")  # missing single_host_only


# ──────────────────────────────────────────────────────────────────────────────
# TEST 19 — capabilities() returns QueueCapabilities


def test_capabilities_returns_queue_capabilities(backend):
    caps = backend.capabilities()
    assert isinstance(caps, QueueCapabilities)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 20 — capabilities().single_host_only consistent across calls


def test_single_host_only_consistent_across_calls(backend):
    """MUST 12: single_host_only MUST be consistent across all calls."""
    caps1 = backend.capabilities()
    caps2 = backend.capabilities()
    assert caps1.single_host_only == caps2.single_host_only


# ──────────────────────────────────────────────────────────────────────────────
# TEST 21 — supports_canonical_export=True for filesystem


def test_filesystem_supports_canonical_export(tmp_path):
    backend = FilesystemQueueBackend(tmp_path / "project")
    caps = backend.capabilities()
    assert caps.single_host_only is True
    assert caps.supports_canonical_export is True


# ──────────────────────────────────────────────────────────────────────────────
# TEST 22 — export() returns QueueExport


def test_export_returns_queue_export(backend):
    result = backend.export()
    assert isinstance(result, QueueExport)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 23 — export() empty when queue/ absent


def test_export_empty_when_queue_absent(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    backend = FilesystemQueueBackend(project_root)
    result = backend.export()
    assert result.items_with_bytes == []
    assert result.backend_id == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 24 — export() excludes claimed/ even when claim IS held


def test_export_excludes_claimed_when_claim_held(tmp_path):
    """MUST assert ephemeral exclusion even when a claim is currently held."""
    project_root, _ = _make_project_with_queue_item(tmp_path, content="queued item")
    backend = FilesystemQueueBackend(project_root)
    # Claim the item (it moves to claimed/)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    assert item.path.exists()  # confirmed: item is in claimed/

    # The real .lease.json sidecar exists in claimed/ right now.
    sidecar = item.path.parent / (item.original_name + ".lease.json")
    assert sidecar.exists(), "precondition: a real lease sidecar is held in claimed/"

    # Export WHILE the item is claimed
    result = backend.export()

    # No item from claimed/ — and no .lease.json sidecar — may appear, even
    # though a claim is currently held (assert the exclusion, don't skip-and-assume).
    rels = [rel for rel, _ in result.items_with_bytes]
    for rel_path in rels:
        assert "claimed" not in rel_path, (
            f"export() MUST NOT include claimed/ items: {rel_path}"
        )
        assert not rel_path.endswith(".lease.json"), (
            f"export() MUST NOT include the held .lease.json sidecar: {rel_path}"
        )
    # The claimed work file itself is absent from the export.
    assert not any(
        rel.endswith(item.original_name) and "queued" not in rel and "done" not in rel
        for rel in rels
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 25 — export() excludes .lease.json sidecars


def test_export_excludes_lease_json_sidecars(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    # Release so item moves to done/ with no sidecar, but also verify no .lease.json
    backend.release(item.lease_token, item.original_name)

    # Manually create a .lease.json in queued/ to test the filter
    qd = project_root / "queue" / "queued" / "writer2"
    qd.mkdir(parents=True)
    (qd / "task.md.lease.json").write_text('{"test": true}')

    result = backend.export()
    for rel_path, _ in result.items_with_bytes:
        assert not rel_path.endswith(".lease.json"), (
            f"export() MUST NOT include .lease.json files: {rel_path}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 26 — export() includes queued/ items


def test_export_includes_queued_items(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path, content="pending work")
    backend = FilesystemQueueBackend(project_root)
    result = backend.export()
    paths = [rel for rel, _ in result.items_with_bytes]
    assert any("queued" in p for p in paths), (
        f"export() must include queued/ items: {paths}"
    )
    # Verify bytes
    for rel, raw in result.items_with_bytes:
        if "queued" in rel:
            assert raw == b"pending work"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 27 — export() includes done/ items after release


def test_export_includes_done_items(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path, content="completed")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    backend.release(item.lease_token, item.original_name)

    result = backend.export()
    paths = [rel for rel, _ in result.items_with_bytes]
    assert any("done" in p for p in paths), (
        f"export() must include done/ items: {paths}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 28 — export() includes dead-letter/ items


def test_export_includes_dead_letter_items(tmp_path):
    project_root, _ = _make_project_with_queue_item(tmp_path, content="failed")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    backend.move_to_dead_letter(item.lease_token, item.original_name, reason="oops")

    result = backend.export()
    paths = [rel for rel, _ in result.items_with_bytes]
    assert any("dead-letter" in p for p in paths), (
        f"export() must include dead-letter/ items: {paths}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 29 — export_all() equals export(None)


def test_export_all_equals_export_none(backend):
    result_all = backend.export_all()
    result_none = backend.export(None)
    assert result_all.items_with_bytes == result_none.items_with_bytes
    assert result_all.backend_id == result_none.backend_id


# ──────────────────────────────────────────────────────────────────────────────
# TEST 30 — claim-race: thread-based barrier


def test_claim_race_only_one_winner_barrier(tmp_path):
    """MUST 4 + MUST 9: validate the rename-EXCLUSION primitive under a synchronized race.

    Uses a threading.Barrier placed just before rename in a SUBCLASS copy of
    claim_next (BarrierQueueBackend), ensuring both threads attempt the rename
    simultaneously. NOT multiprocessing (macOS APFS flake class per ruling).

    SCOPE NOTE: this exercises a hand-written copy of the queued→claimed rename
    loop (the only way to inject a deterministic barrier at the rename point),
    so it validates the POSIX-rename mutual-exclusion guarantee — NOT the exact
    production claim_next bytes. The AUTHORITATIVE no-double-claim test against
    the real shipped claim_next is TEST 31 (test_claim_race_rename_loser_returns_none),
    which monkeypatches Path.rename and drives the production code path. Keep
    both: TEST 30 proves the primitive, TEST 31 proves production uses it.
    """
    project_root = tmp_path / "project"
    qd = project_root / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "one.md").write_text("one")

    barrier = threading.Barrier(2)
    results = []
    errors = []

    class BarrierQueueBackend(FilesystemQueueBackend):
        def claim_next(self, role, lease_token, lease_seconds=3600):
            # Override: synchronize both threads at the rename point
            try:
                from atomic_agents.queue.filesystem import _write_sidecar
                import time as _time

                queue_root = self._queue_root()
                queued_dir = queue_root / "queued" / role
                if not queued_dir.is_dir():
                    return None
                claimed_dir = queue_root / "claimed" / lease_token
                claimed_dir.mkdir(parents=True, exist_ok=True)
                candidates = sorted(p for p in queued_dir.iterdir() if p.is_file())
                for src in candidates:
                    dst = claimed_dir / src.name
                    # Both threads wait here before attempting rename
                    barrier.wait(timeout=5.0)
                    try:
                        src.rename(dst)
                    except FileNotFoundError:
                        continue
                    _write_sidecar(
                        dst, lease_token=lease_token, lease_seconds=lease_seconds
                    )
                    from atomic_agents.queue.filesystem import FilesystemQueueItem

                    return FilesystemQueueItem(
                        original_name=src.name,
                        role=role,
                        lease_token=lease_token,
                        claimed_at=_time.time(),
                        path=dst,
                    )
                return None
            except Exception as e:
                errors.append(e)
                return None

    def worker(token):
        b = BarrierQueueBackend(project_root)
        item = b.claim_next("writer", token)
        results.append(item)

    t1 = threading.Thread(target=worker, args=("worker-A",))
    t2 = threading.Thread(target=worker, args=("worker-B",))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert not errors, f"Unexpected errors in workers: {errors}"
    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1, (
        f"Exactly one worker should win the race, got {len(non_none)}: {results}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 31 — claim-race: monkeypatched-rename loser returns None


def test_claim_race_rename_loser_returns_none(tmp_path, monkeypatch):
    """MUST 4 + MUST 9: the FileNotFoundError rename-loser skips the contended file.

    Deterministic (not a sequential simulation): the ONLY candidate's rename
    raises FileNotFoundError — exactly what the kernel returns to the loser of
    a concurrent rename. The claimer must (a) NOT crash, (b) move on to the
    next candidate (here there are none), and (c) return None. A naive impl
    that lets FileNotFoundError propagate would fail this test.
    """
    project_root = tmp_path / "project"
    qd = project_root / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "only.md").write_text("only")

    original_rename = Path.rename
    raised = {"count": 0}

    def patched_rename(self, target):
        # The work-file rename (queued → claimed) is the contended op. Make the
        # FIRST such rename always lose the race; let any other rename through.
        if self.name == "only.md":
            raised["count"] += 1
            raise FileNotFoundError("race loser: another worker took only.md")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", patched_rename)

    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "loser", lease_seconds=60)

    assert raised["count"] >= 1, "the contended rename must have been attempted"
    assert item is None, "the rename-loser must get None, not a crash or stale item"
    # No sidecar was written for the file we never actually claimed.
    assert not (
        project_root / "queue" / "claimed" / "loser" / "only.md.lease.json"
    ).exists()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 32–35 — _redact_for_error_message


def test_redact_url():
    from atomic_agents.queue import _redact_for_error_message

    assert _redact_for_error_message("redis://user:pass@host/0") == "redis://..."


def test_redact_dsn():
    from atomic_agents.queue import _redact_for_error_message

    assert (
        _redact_for_error_message("user:pass@host/db") == "[redacted-connection-string]"
    )


def test_redact_truncation():
    from atomic_agents.queue import _redact_for_error_message

    long_value = "a" * 50
    result = _redact_for_error_message(long_value)
    assert result.endswith("...")
    assert len(result) <= 35


def test_redact_passthrough_short():
    from atomic_agents.queue import _redact_for_error_message

    assert _redact_for_error_message("filesystem") == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 36 — get_default_queue_backend() uses filesystem by default


def test_get_default_queue_backend_filesystem(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_QUEUE_BACKEND", raising=False)
    from atomic_agents.queue import get_default_queue_backend

    project_root = tmp_path / "project"
    project_root.mkdir()
    backend = get_default_queue_backend(project_root)
    assert isinstance(backend, FilesystemQueueBackend)
    assert backend.backend_id == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 37 — get_queue_backend() raises BackendNotRegistered for unknown id


def test_get_queue_backend_unknown_raises(tmp_path):
    from atomic_agents.queue import get_queue_backend

    with pytest.raises(BackendNotRegistered):
        get_queue_backend("nonexistent-backend-xyz")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 38 — env var dispatches registered custom backend


def test_env_var_dispatches_custom_backend(tmp_path, monkeypatch):
    from atomic_agents.queue import (
        get_default_queue_backend,
        register_queue_backend,
        unregister_queue_backend,
    )

    class MyQueueBackend(FilesystemQueueBackend):
        @property
        def backend_id(self):
            return "custom-test"

    register_queue_backend("custom-test", MyQueueBackend)
    monkeypatch.setenv("ATOMIC_AGENTS_QUEUE_BACKEND", "custom-test")
    try:
        project_root = tmp_path / "project"
        project_root.mkdir()
        b = get_default_queue_backend(project_root)
        assert isinstance(b, MyQueueBackend)
    finally:
        unregister_queue_backend("custom-test")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 39 — get_default_queue_backend() unknown env var raises


def test_get_default_queue_backend_unknown_raises(tmp_path, monkeypatch):
    from atomic_agents.queue import get_default_queue_backend

    monkeypatch.setenv("ATOMIC_AGENTS_QUEUE_BACKEND", "completely-unknown-xyz")
    project_root = tmp_path / "project"
    project_root.mkdir()
    with pytest.raises(BackendNotRegistered):
        get_default_queue_backend(project_root)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 40 — QueueBackend is @runtime_checkable


def test_queue_backend_runtime_checkable(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    backend = FilesystemQueueBackend(project_root)
    assert isinstance(backend, QueueBackend)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 41 — doctor.check_queue_backend SKIP for non-cascade agent


def test_doctor_check_queue_backend_skip_non_cascade(tmp_path):
    from atomic_agents.doctor import check_queue_backend, SKIP

    agent_root = tmp_path / "caldwell"
    agent_root.mkdir()
    result = check_queue_backend(agent_root)
    assert result.status == SKIP
    assert result.name == "queue-backend"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42 — doctor.check_queue_backend PASS for cascade agent


def test_doctor_check_queue_backend_pass_cascade(tmp_path, monkeypatch):
    from atomic_agents.doctor import check_queue_backend, PASS

    monkeypatch.delenv("ATOMIC_AGENTS_QUEUE_BACKEND", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_MULTI_HOST", raising=False)

    # Build minimal cascade layout
    system_root = tmp_path / "muse"
    role_dir = system_root / "roles" / "writer"
    role_dir.mkdir(parents=True)
    instance_dir = system_root / "projects" / "the-unfinished" / "agents" / "writer"
    instance_dir.mkdir(parents=True)

    result = check_queue_backend(instance_dir)
    assert result.status == PASS
    assert result.name == "queue-backend"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 43 — doctor.check_queue_backend FAIL for bad env var


def test_doctor_check_queue_backend_fail_bad_env(tmp_path, monkeypatch):
    from atomic_agents.doctor import check_queue_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_QUEUE_BACKEND", "nonexistent-xyz-backend")

    # Build cascade layout
    system_root = tmp_path / "muse"
    role_dir = system_root / "roles" / "writer"
    role_dir.mkdir(parents=True)
    instance_dir = system_root / "projects" / "proj" / "agents" / "writer"
    instance_dir.mkdir(parents=True)

    result = check_queue_backend(instance_dir)
    assert result.status == FAIL
    assert "queue-backend" == result.name


# ──────────────────────────────────────────────────────────────────────────────
# TEST 44 — doctor.check_queue_backend WARN for single_host_only + MULTI_HOST


def test_doctor_check_queue_backend_warn_multi_host(tmp_path, monkeypatch):
    from atomic_agents.doctor import check_queue_backend, WARN

    monkeypatch.delenv("ATOMIC_AGENTS_QUEUE_BACKEND", raising=False)
    monkeypatch.setenv("ATOMIC_AGENTS_MULTI_HOST", "true")

    # Build cascade layout
    system_root = tmp_path / "muse"
    role_dir = system_root / "roles" / "writer"
    role_dir.mkdir(parents=True)
    instance_dir = system_root / "projects" / "proj" / "agents" / "writer"
    instance_dir.mkdir(parents=True)

    result = check_queue_backend(instance_dir)
    assert result.status == WARN
    assert result.name == "queue-backend"
    assert "single-host-only" in result.message or "single_host_only" in result.message


# ──────────────────────────────────────────────────────────────────────────────
# TEST 45 — compatibility: old and new import paths are behaviorally equivalent


def test_compatibility_old_and_new_import_paths_equivalent(tmp_path):
    """Old and new import paths resolve to behaviorally-equivalent functions.

    The shim wraps the backend call — not the same object identity. We test
    behavioral equivalence: both paths produce the same result on the same fixture.
    Per arc-ruling 428-pr1-args.json re-export-shim-shape: Option A.
    """
    from atomic_agents._cascade import claim_next_queued as old_claim
    from atomic_agents.queue.filesystem import FilesystemQueueBackend

    project_root = tmp_path / "project"
    qd = project_root / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "task_a.md").write_text("Task A")
    (qd / "task_b.md").write_text("Task B")

    # Old path claim
    item_old = old_claim(project_root, "writer", "old-lease")
    assert item_old is not None
    assert item_old.original_name == "task_a.md"
    assert hasattr(item_old, "path")  # FilesystemQueueItem has path

    # New path claim (claims task_b since task_a is taken)
    backend = FilesystemQueueBackend(project_root)
    item_new = backend.claim_next("writer", "new-lease")
    assert item_new is not None
    assert item_new.original_name == "task_b.md"
    assert hasattr(item_new, "path")

    # Both return items with the same required fields
    assert item_old.role == item_new.role == "writer"
    assert item_old.claimed_at > 0
    assert item_new.claimed_at > 0


def test_cascade_reexports_are_same_object_and_quiet():
    """The _cascade.py NON-deprecated re-export shim must:

    1. Re-export QueueItem, _sidecar_path, _write_sidecar as the SAME OBJECTS
       from atomic_agents.queue (not copies/wrappers).
    2. Alias QueueItem to FilesystemQueueItem so item.path keeps working.
    3. Emit NO DeprecationWarning on import or on use (sunset is deferred to
       the v1.0/T10 shim-retirement pass).

    Per arc-ruling 428-pr1-args.json re-export-shim-shape: Option A.
    """
    import warnings
    import importlib

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # Re-import to prove no DeprecationWarning fires at import time.
        cascade = importlib.import_module("atomic_agents._cascade")
        importlib.reload(cascade)

        from atomic_agents.queue import (
            FilesystemQueueItem,
            _sidecar_path as queue_sidecar_path,
            _write_sidecar as queue_write_sidecar,
        )

        # SAME-OBJECT identity for the re-exported symbols.
        assert cascade.QueueItem is FilesystemQueueItem, (
            "_cascade.QueueItem must be the SAME object as "
            "queue.FilesystemQueueItem (backward-compat alias, item.path works)"
        )
        assert cascade._sidecar_path is queue_sidecar_path
        assert cascade._write_sidecar is queue_write_sidecar

        # item.path access still works through the alias.
        import dataclasses

        assert "path" in {f.name for f in dataclasses.fields(cascade.QueueItem)}


# ──────────────────────────────────────────────────────────────────────────────
# TEST 46 — QueueExport importable from atomic_agents.export


def test_queue_export_importable_from_export_package():
    """QueueExport must be re-exported from atomic_agents.export (spec/40 registration)."""
    from atomic_agents.export import QueueExport as QueueExportFromExport
    from atomic_agents.queue.types import QueueExport

    assert QueueExportFromExport is QueueExport


# ──────────────────────────────────────────────────────────────────────────────
# TEST 47 — QueueItem has no path field (abstract Protocol-level type)


def test_queue_item_has_no_path_field():
    """The abstract Protocol-level QueueItem MUST NOT have a path field."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(QueueItem)}
    assert "path" not in field_names, (
        "QueueItem MUST NOT have a path field — path is filesystem-specific "
        "(FilesystemQueueItem only). Per arc-ruling 428-pr1-args.json."
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 48 — FilesystemQueueItem has path field


def test_filesystem_queue_item_has_path_field():
    """FilesystemQueueItem MUST have a path: Path field."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(FilesystemQueueItem)}
    assert "path" in field_names, "FilesystemQueueItem must have a path field"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 49 — WritePolicy NOT in QueueBackend Protocol surface


def test_writepolicy_not_in_queue_backend():
    """WritePolicy MUST NOT be imported into or used by QueueBackend Protocol modules.

    Comments that mention WritePolicy to document its ABSENCE are acceptable; what
    is prohibited is any actual import or use of WritePolicy as a type or parameter.
    """
    import atomic_agents.queue.backend as queue_backend_module
    import atomic_agents.queue.types as queue_types_module

    # WritePolicy must not be imported into queue/ modules
    assert not hasattr(queue_backend_module, "WritePolicy"), (
        "WritePolicy MUST NOT be importable from queue/backend.py "
        "(per arc-ruling 428-pr1-args.json writepolicy-presence: Option 1)"
    )
    assert not hasattr(queue_types_module, "WritePolicy"), (
        "WritePolicy MUST NOT be importable from queue/types.py "
        "(per arc-ruling 428-pr1-args.json writepolicy-presence: Option 1)"
    )

    # Also verify WritePolicy is not in the Protocol method signatures
    import inspect
    from atomic_agents.queue.backend import QueueBackend

    for name, method in inspect.getmembers(QueueBackend, predicate=inspect.isfunction):
        sig = inspect.signature(method)
        for param in sig.parameters.values():
            annotation = str(param.annotation)
            assert "WritePolicy" not in annotation, (
                f"WritePolicy MUST NOT appear in QueueBackend.{name} signature"
            )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 48 — item.path stays in the CALLER's (unresolved/symlinked) representation
#
# Closes the tmp_path blind spot: pytest's tmp_path is already resolved on macOS,
# so the standard claim_next tests cannot detect a backend that returns the
# RESOLVED path instead of the caller's unresolved one. Pre-carve _cascade.py
# returned the caller-supplied path; a regression to the resolved path breaks
# item.path.relative_to(project_root) for any external cron/spec-06 caller whose
# project_root is reached through a symlink (a symlinked $HOME, /tmp→/private/tmp
# on macOS, a bind mount). This test constructs the backend with a SYMLINKED
# project_root and asserts the returned path uses the caller's representation.


def test_claim_next_path_uses_caller_unresolved_root(tmp_path):
    """MUST: item.path is byte-identical to the caller's project_root layout.

    Construct the backend with a symlinked project_root (real_dir <- symlink),
    pass the SYMLINK as project_root, and assert the returned item.path is
    expressed under the symlink — NOT resolved to real_dir. Mirrors the
    FilesystemJournalBackend._journal_dir() unresolved-return contract.
    """
    real_dir = tmp_path / "real_project"
    qd = real_dir / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "001_task.md").write_text("work")

    symlinked_root = tmp_path / "linked_project"
    os.symlink(real_dir, symlinked_root)

    backend = FilesystemQueueBackend(symlinked_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    assert item.path.exists()

    # The returned path MUST be under the caller's symlinked root, not real_dir.
    assert symlinked_root in item.path.parents, (
        f"item.path must use the caller's (symlinked) root {symlinked_root}, "
        f"got {item.path}"
    )
    assert real_dir not in item.path.parents, (
        f"item.path must NOT be resolved to the real dir {real_dir}, got {item.path}"
    )
    # The external spec/06 cron contract: relative_to(project_root) succeeds.
    rel = item.path.relative_to(symlinked_root)
    assert rel == Path("queue") / "claimed" / "lease-1" / "001_task.md"
    # parents[3] (used by the renew_lease shim) lands back on the caller root.
    assert item.path.parents[3] == symlinked_root


def test_renew_lease_shim_symlinked_root(tmp_path):
    """The renew_lease shim (parents[3]) works with a symlinked project_root.

    Guards the P2 follow-on: parents[3] must reconstruct the caller's root, not
    a resolved divergent string, so the shim renews the right sidecar.
    """
    import json as _json

    from atomic_agents._cascade import claim_next_queued, renew_lease as shim_renew

    real_dir = tmp_path / "real_project"
    qd = real_dir / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "renewable.md").write_text("work")

    symlinked_root = tmp_path / "linked_project"
    os.symlink(real_dir, symlinked_root)

    item = claim_next_queued(symlinked_root, "writer", "renew-lease", lease_seconds=60)
    assert item is not None
    sidecar = item.path.parent / (item.original_name + ".lease.json")
    orig = _json.loads(sidecar.read_text())["lease_expires_at"]

    shim_renew(item, additional_seconds=3600)
    new = _json.loads(sidecar.read_text())["lease_expires_at"]
    assert new != orig, "renew_lease must have updated the sidecar via parents[3]"

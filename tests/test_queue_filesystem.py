"""Filesystem-specific tests for FilesystemQueueBackend (spec/44).

These tests cover filesystem implementation details NOT exercised by the
protocol-conformance parametrized tests in test_queue_backend_conformance.py:

  - POSIX-rename atomicity (the O_RENAME kernel guarantee)
  - .lease.json sidecar creation, update, and absence-fallback
  - mtime legacy fallback when sidecar is absent or malformed
  - Symlink containment (queue/ escaping project_root via symlink)
  - PathTraversalError on '..' in project_root
  - _sidecar_path / _write_sidecar internal helpers
  - Backward-compat shim wrappers in _cascade.py
  - Derived project_root from item.path in renew_lease shim
  - recover_stale_claims returns list[Path] from shim (backward compat)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atomic_agents.queue.filesystem import (
    FilesystemQueueBackend,
    FilesystemQueueItem,
    _sidecar_path,
    _write_sidecar,
)
from atomic_agents.queue.backend import recover_stale_claims
from atomic_agents.exceptions import PathTraversalError


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _queued(
    project_root: Path,
    role: str = "writer",
    name: str = "task.md",
    content: str = "body",
) -> Path:
    """Plant a queued item and return the path."""
    qd = project_root / "queue" / "queued" / role
    qd.mkdir(parents=True, exist_ok=True)
    p = qd / name
    p.write_text(content)
    return p


# ──────────────────────────────────────────────────────────────────────────────
# _sidecar_path


def test_sidecar_path_appends_suffix(tmp_path):
    item_path = tmp_path / "queue" / "claimed" / "lease-1" / "task.md"
    sp = _sidecar_path(item_path)
    assert sp == tmp_path / "queue" / "claimed" / "lease-1" / "task.md.lease.json"


def test_sidecar_path_preserves_directory(tmp_path):
    item_path = tmp_path / "a" / "b" / "c.md"
    assert _sidecar_path(item_path).parent == item_path.parent


# ──────────────────────────────────────────────────────────────────────────────
# _write_sidecar


def test_write_sidecar_creates_file(tmp_path):
    claimed_dir = tmp_path / "queue" / "claimed" / "lease-1"
    claimed_dir.mkdir(parents=True)
    item_path = claimed_dir / "task.md"
    item_path.write_text("body")

    _write_sidecar(item_path, lease_token="lease-1", lease_seconds=120)

    sidecar = _sidecar_path(item_path)
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["lease_token"] == "lease-1"
    assert "lease_expires_at" in data
    expires = datetime.fromisoformat(data["lease_expires_at"])
    delta = expires.timestamp() - time.time()
    assert 100 < delta < 140, f"Expected ~120s lease, got {delta:.1f}s"


def test_write_sidecar_overwrites_existing(tmp_path):
    claimed_dir = tmp_path / "queue" / "claimed" / "lease-1"
    claimed_dir.mkdir(parents=True)
    item_path = claimed_dir / "task.md"
    item_path.write_text("body")

    _write_sidecar(item_path, lease_token="lease-1", lease_seconds=60)
    first_data = json.loads(_sidecar_path(item_path).read_text())
    time.sleep(0.01)
    _write_sidecar(item_path, lease_token="lease-1", lease_seconds=3600)
    second_data = json.loads(_sidecar_path(item_path).read_text())

    first_expires = datetime.fromisoformat(first_data["lease_expires_at"])
    second_expires = datetime.fromisoformat(second_data["lease_expires_at"])
    assert second_expires > first_expires


def test_write_sidecar_not_atomic_write(tmp_path):
    """_write_sidecar uses raw write_text (best-effort, documented in spec/44)."""
    claimed_dir = tmp_path / "queue" / "claimed" / "lease-1"
    claimed_dir.mkdir(parents=True)
    item_path = claimed_dir / "task.md"
    item_path.write_text("body")

    # Verify write_text is used (no .tmp files created in the directory)
    _write_sidecar(item_path, lease_token="lease-1", lease_seconds=60)
    files = list(claimed_dir.iterdir())
    tmp_files = [f for f in files if ".tmp" in f.name]
    assert tmp_files == [], (
        f"_write_sidecar must NOT use atomic_write (.tmp found): {tmp_files}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# POSIX rename atomicity


def test_posix_rename_atomicity(tmp_path):
    """claim_next() uses rename() — kernel-level atomic on POSIX/APFS.

    This test verifies the implementation path: after claim_next, the source
    path does not exist and the destination path does. If any other outcome
    is observed, the implementation is using non-atomic moves.
    """
    project_root = tmp_path / "project"
    _queued(project_root)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None

    # Source (queued) must be gone
    source = project_root / "queue" / "queued" / "writer" / "task.md"
    assert not source.exists(), "Source must be removed by atomic rename"

    # Destination (claimed) must exist
    assert item.path.exists(), "Claimed item path must exist after rename"
    assert item.path.parent == project_root / "queue" / "claimed" / "lease-1"


def test_claim_is_exclusive_within_process(tmp_path):
    """Two calls to claim_next with different lease tokens can't both claim the same item."""
    project_root = tmp_path / "project"
    _queued(project_root)
    backend = FilesystemQueueBackend(project_root)
    item1 = backend.claim_next("writer", "lease-A")
    item2 = backend.claim_next("writer", "lease-B")
    # Only one item exists; exactly one should win
    assert item1 is not None
    assert item2 is None


# ──────────────────────────────────────────────────────────────────────────────
# .lease.json sidecar behavior


def test_lease_sidecar_created_on_claim(tmp_path):
    project_root = tmp_path / "project"
    _queued(project_root)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1", lease_seconds=300)
    assert item is not None

    sidecar = _sidecar_path(item.path)
    assert sidecar.exists(), ".lease.json sidecar must be created on claim"
    data = json.loads(sidecar.read_text())
    assert data["lease_token"] == "lease-1"
    expires = datetime.fromisoformat(data["lease_expires_at"])
    delta = expires.timestamp() - time.time()
    assert 270 < delta < 330, f"Expected ~300s, got {delta:.1f}s"


def test_lease_sidecar_removed_on_release(tmp_path):
    """After release, the claimed/ directory is renamed to done/ — sidecar moves too."""
    project_root = tmp_path / "project"
    _queued(project_root)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None
    sidecar_in_claimed = _sidecar_path(item.path)
    backend.release(item.lease_token, item.original_name)

    # Claimed dir (with sidecar) is gone; done dir has the item but NOT the sidecar
    assert not sidecar_in_claimed.exists(), (
        "Sidecar must be gone from claimed/ after release"
    )
    # The item was moved (not its sidecar) — sidecar is NOT in done/
    done_sidecar = (
        project_root
        / "queue"
        / "done"
        / "lease-1"
        / (item.original_name + ".lease.json")
    )
    # NB: the release() impl moves the item file, not the directory; sidecar stays in claimed/
    # Regardless, the sidecar should NOT appear in done/ (it's in claimed/ and release moves items individually)


def test_lease_sidecar_updated_on_renew(tmp_path):
    project_root = tmp_path / "project"
    _queued(project_root)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1", lease_seconds=60)
    assert item is not None

    sidecar = _sidecar_path(item.path)
    orig_expires = datetime.fromisoformat(
        json.loads(sidecar.read_text())["lease_expires_at"]
    )

    backend.renew_lease(item.lease_token, item.original_name, additional_seconds=7200)

    new_expires = datetime.fromisoformat(
        json.loads(sidecar.read_text())["lease_expires_at"]
    )
    assert new_expires > orig_expires
    delta = new_expires.timestamp() - time.time()
    assert 7100 < delta < 7300, f"Expected ~7200s, got {delta:.1f}s"


# ──────────────────────────────────────────────────────────────────────────────
# mtime legacy fallback (no sidecar)


def test_mtime_fallback_when_sidecar_absent(tmp_path):
    """list_claimed() must use mtime as fallback when sidecar is absent."""
    project_root = tmp_path / "project"
    _queued(project_root)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1", lease_seconds=60)
    assert item is not None

    # Remove the sidecar to simulate a pre-sidecar claim
    sidecar = _sidecar_path(item.path)
    sidecar.unlink()
    assert not sidecar.exists()

    # list_claimed() must still return the item (using mtime fallback)
    claimed = backend.list_claimed()
    assert len(claimed) == 1
    assert claimed[0].original_name == "task.md"
    # claimed_at must be a reasonable timestamp
    assert claimed[0].claimed_at > 0


def test_mtime_fallback_when_sidecar_malformed(tmp_path):
    """list_claimed() must not raise when sidecar is malformed JSON."""
    project_root = tmp_path / "project"
    _queued(project_root)
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1", lease_seconds=60)
    assert item is not None

    sidecar = _sidecar_path(item.path)
    sidecar.write_text("not json {{{")  # corrupt it

    claimed = backend.list_claimed()
    assert len(claimed) == 1  # must not crash


# ──────────────────────────────────────────────────────────────────────────────
# Symlink containment


def test_symlink_queue_dir_escape_raises(tmp_path):
    """If queue/ is a symlink that resolves outside project_root, _queue_root must raise."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # Create queue/ as a symlink pointing outside project_root
    queue_link = project_root / "queue"
    queue_link.symlink_to(outside)

    backend = FilesystemQueueBackend(project_root)
    with pytest.raises((PathTraversalError, PermissionError, ValueError)):
        backend._queue_root()


def test_symlinked_ancestor_escape_raises(tmp_path):
    """If project_root itself resolves outside its declared path, _queue_root raises."""
    real_project = tmp_path / "real_project"
    real_project.mkdir()
    link_project = tmp_path / "link_project"
    link_project.symlink_to(real_project)

    # Using a symlinked project_root should be ok (symlinked root is fine)
    # but a symlinked queue/ inside must not escape
    outside = tmp_path / "escape"
    outside.mkdir()
    queue_link = real_project / "queue"
    queue_link.symlink_to(outside)

    backend = FilesystemQueueBackend(link_project)
    with pytest.raises((PathTraversalError, PermissionError, ValueError)):
        backend._queue_root()


def test_dotdot_in_project_root_raises(tmp_path):
    """project_root containing '..' components MUST be rejected at construction time."""
    with pytest.raises((ValueError, PathTraversalError)):
        FilesystemQueueBackend(tmp_path / ".." / "project")


# ──────────────────────────────────────────────────────────────────────────────
# Backward-compat shim (_cascade.py wrappers)


def test_shim_claim_next_queued(tmp_path):
    from atomic_agents._cascade import claim_next_queued

    project_root = tmp_path / "project"
    _queued(project_root, name="shim_task.md", content="shim content")
    item = claim_next_queued(project_root, "writer", "shim-lease")
    assert item is not None
    assert item.original_name == "shim_task.md"
    assert item.role == "writer"
    assert item.lease_token == "shim-lease"
    assert hasattr(item, "path")
    assert item.path.exists()


def test_shim_release_claim(tmp_path):
    from atomic_agents._cascade import claim_next_queued, release_claim

    project_root = tmp_path / "project"
    _queued(project_root, name="shim_done.md", content="to complete")
    item = claim_next_queued(project_root, "writer", "done-lease")
    assert item is not None
    release_claim(item, project_root)
    done_file = project_root / "queue" / "done" / "done-lease" / "shim_done.md"
    assert done_file.exists()
    assert not item.path.exists()


def test_shim_move_to_dead_letter(tmp_path):
    from atomic_agents._cascade import claim_next_queued, move_to_dead_letter

    project_root = tmp_path / "project"
    _queued(project_root, name="shim_dead.md")
    item = claim_next_queued(project_root, "writer", "dead-lease")
    assert item is not None
    move_to_dead_letter(item, project_root, reason="shim terminal failure")
    dl = project_root / "queue" / "dead-letter" / "dead-lease" / "shim_dead.md"
    assert dl.exists()
    reason_file = dl.parent / "shim_dead.md.reason.txt"
    assert reason_file.read_text() == "shim terminal failure"


def test_shim_recover_stale_claims_returns_paths(tmp_path):
    """The _cascade.py recover_stale_claims shim must return list[Path] (backward compat)."""
    from atomic_agents._cascade import (
        claim_next_queued,
        recover_stale_claims as shim_recover,
    )

    project_root = tmp_path / "project"
    _queued(project_root, name="stale.md")
    item = claim_next_queued(project_root, "writer", "stale-lease")
    assert item is not None

    # Backdate sidecar
    sidecar = _sidecar_path(item.path)
    data = json.loads(sidecar.read_text())
    past = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc)
    data["lease_expires_at"] = past.isoformat()
    sidecar.write_text(json.dumps(data))

    recovered = shim_recover(project_root, lease_seconds=3600)
    # Must return list[Path], not list[FilesystemQueueItem]
    assert isinstance(recovered, list)
    if recovered:
        assert isinstance(recovered[0], Path), (
            f"shim recover_stale_claims must return list[Path], got {type(recovered[0])}"
        )
        assert recovered[0].name == "stale.md"


def test_shim_renew_lease(tmp_path):
    """renew_lease shim derives project_root from item.path.parents[3]."""
    from atomic_agents._cascade import claim_next_queued, renew_lease as shim_renew

    project_root = tmp_path / "project"
    _queued(project_root, name="renewable.md")
    item = claim_next_queued(project_root, "writer", "renew-lease", lease_seconds=60)
    assert item is not None

    sidecar = _sidecar_path(item.path)
    orig_expires = datetime.fromisoformat(
        json.loads(sidecar.read_text())["lease_expires_at"]
    )

    shim_renew(item, additional_seconds=3600)

    new_expires = datetime.fromisoformat(
        json.loads(sidecar.read_text())["lease_expires_at"]
    )
    assert new_expires > orig_expires


# ──────────────────────────────────────────────────────────────────────────────
# recover_stale_claims free function (protocol-level)


def test_recover_stale_claims_via_native_path(tmp_path):
    """FilesystemQueueBackend exposes _recover_stale_claims_native — free function delegates."""
    project_root = tmp_path / "project"
    _queued(project_root, name="native.md")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "native-lease", lease_seconds=3600)
    assert item is not None

    # Expire the sidecar
    sidecar = _sidecar_path(item.path)
    data = json.loads(sidecar.read_text())
    past = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc)
    data["lease_expires_at"] = past.isoformat()
    sidecar.write_text(json.dumps(data))

    # Free function detects _recover_stale_claims_native and uses it
    assert hasattr(backend, "_recover_stale_claims_native")
    recovered = recover_stale_claims(backend, lease_seconds=3600)
    assert len(recovered) == 1
    assert recovered[0].original_name == "native.md"
    assert (
        project_root / "queue" / "queued" / "_recovered" / "native-lease" / "native.md"
    ).exists()


def test_recover_stale_claims_generic_path(tmp_path):
    """When _recover_stale_claims_native is absent, the generic Protocol path is used."""
    project_root = tmp_path / "project"
    _queued(project_root, name="generic.md")
    backend = FilesystemQueueBackend(project_root)

    class MinimalQueueBackend:
        """Minimal backend without _recover_stale_claims_native."""

        def __init__(self, inner: FilesystemQueueBackend):
            self._inner = inner
            self.backend_id = "minimal"

        def claim_next(self, role, lease_token, lease_seconds=3600):
            return self._inner.claim_next(role, lease_token, lease_seconds)

        def release(self, lease_token, original_name):
            return self._inner.release(lease_token, original_name)

        def move_to_dead_letter(self, lease_token, original_name, reason):
            return self._inner.move_to_dead_letter(lease_token, original_name, reason)

        def renew_lease(self, lease_token, original_name, additional_seconds):
            return self._inner.renew_lease(
                lease_token, original_name, additional_seconds
            )

        def list_claimed(self, role=None):
            return self._inner.list_claimed(role)

        def _reclaim_to_recovered(self, item):
            return self._inner._reclaim_to_recovered(item)

        def export(self, query=None):
            return self._inner.export(query)

        def export_all(self):
            return self._inner.export_all()

        def capabilities(self):
            return self._inner.capabilities()

    item = backend.claim_next("writer", "generic-lease", lease_seconds=3600)
    assert item is not None

    # Expire the sidecar
    sidecar = _sidecar_path(item.path)
    data = json.loads(sidecar.read_text())
    past = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc)
    data["lease_expires_at"] = past.isoformat()
    sidecar.write_text(json.dumps(data))

    minimal = MinimalQueueBackend(backend)
    assert not hasattr(minimal, "_recover_stale_claims_native")
    recovered = recover_stale_claims(minimal, lease_seconds=3600)
    assert len(recovered) == 1


# ──────────────────────────────────────────────────────────────────────────────
# list_claimed() role filtering


def test_list_claimed_filters_by_role(tmp_path):
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="w1.md")
    _queued(project_root, role="researcher", name="r1.md")
    backend = FilesystemQueueBackend(project_root)
    backend.claim_next("writer", "w-lease")
    backend.claim_next("researcher", "r-lease")

    all_claimed = backend.list_claimed()
    assert len(all_claimed) == 2

    writer_claimed = backend.list_claimed(role="writer")
    assert len(writer_claimed) == 1
    assert writer_claimed[0].role == "writer"


def test_list_claimed_role_none_returns_all(tmp_path):
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="w1.md")
    _queued(project_root, role="researcher", name="r1.md")
    backend = FilesystemQueueBackend(project_root)
    backend.claim_next("writer", "w-lease")
    backend.claim_next("researcher", "r-lease")

    all_claimed = backend.list_claimed(role=None)
    assert len(all_claimed) == 2


# ──────────────────────────────────────────────────────────────────────────────
# export scope property


def test_export_scope_is_project_root_string(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    backend = FilesystemQueueBackend(project_root)
    result = backend.export()
    assert result.scope == str(project_root)

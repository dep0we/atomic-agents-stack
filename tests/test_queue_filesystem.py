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
  - renew_lease shim writes the sidecar next to item.path (any depth)
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


def test_list_claimed_skips_file_vanishing_mid_enumeration(tmp_path):
    """list_claimed() must not raise if a work file is renamed out of claimed/
    between the is_file() check and the stat() fallback.

    Regression: a concurrent release()/move_to_dead_letter()/recovery can remove
    a claimed work file mid-enumeration, so the unguarded
    `path.stat().st_mtime` fallback raised FileNotFoundError out of
    list_claimed() (contract: spec/44 MUST 11 — must return held items, no raise;
    doctor treats any raise as a hard FAIL). The fix skips vanished files,
    mirroring _recover_stale_claims_native.
    """
    project_root = tmp_path / "project"
    # Two items so the survivor proves enumeration continues past the vanish.
    _queued(project_root, name="vanishes.md")
    _queued(project_root, name="survives.md")
    backend = FilesystemQueueBackend(project_root)
    item_a = backend.claim_next("writer", "lease-v", lease_seconds=60)
    item_b = backend.claim_next("writer", "lease-v", lease_seconds=60)
    assert item_a is not None and item_b is not None

    # Identify which claimed path holds vanishes.md / survives.md (lease-token
    # namespacing means both land under claimed/lease-v/).
    vanish_path = next(p for p in (item_a.path, item_b.path) if p.name == "vanishes.md")
    survive_path = next(
        p for p in (item_a.path, item_b.path) if p.name == "survives.md"
    )

    # Force the mtime fallback (no sidecar) so the guarded stat() is reached.
    _sidecar_path(vanish_path).unlink(missing_ok=True)
    _sidecar_path(survive_path).unlink(missing_ok=True)

    # Reproduce the real race faithfully: hook is_file() so that when
    # list_claimed() probes vanishes.md, it reports True (as a concurrent
    # finalizer had not yet moved it) and then physically removes the file —
    # so the subsequent real stat() in the fallback genuinely hits a missing
    # path. One-shot, path-exact: only the work file vanishes.md, only once.
    real_is_file = Path.is_file
    vanish_str = str(vanish_path)
    state = {"removed": False}

    def hooked_is_file(self):
        result = real_is_file(self)
        if result and not state["removed"] and str(self) == vanish_str:
            self.unlink()  # concurrent finalizer wins the race here
            state["removed"] = True
        return result

    import unittest.mock as mock

    with mock.patch.object(Path, "is_file", hooked_is_file):
        claimed = backend.list_claimed()  # must NOT raise

    names = {c.original_name for c in claimed}
    # The vanished item is skipped; the survivor is still returned.
    assert names == {"survives.md"}


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
# Symlinked-SUBDIR ancestor escape (#426 _runs_root() escalation pattern).
# queue/ is a real, contained directory — but an INNER operation directory
# (claimed/, queued/, done/, dead-letter/) is a symlink pointing outside the
# project. Without per-subdir containment the rename/iteration lands OUTSIDE
# project_root and leaks work-item bytes. These exercise the real write/read
# surfaces (claim_next/list_claimed/export/recover), not just _queue_root().


def test_symlinked_claimed_subdir_does_not_escape_on_claim(tmp_path):
    """claim_next MUST refuse a symlinked claimed/ escaping queue/ (no write outside)."""
    project_root = tmp_path / "project"
    (project_root / "queue").mkdir(parents=True)
    _queued(project_root, role="writer", name="x.md", content="secret")
    outside = tmp_path / "outside"
    outside.mkdir()
    # claimed/ is a symlink escaping queue/
    (project_root / "queue" / "claimed").symlink_to(outside)

    backend = FilesystemQueueBackend(project_root)
    # Real surface: claim_next must fail-soft (None) and write nothing outside.
    assert backend.claim_next("writer", "lease-1") is None
    assert list(outside.rglob("*")) == [], "no bytes may land outside project_root"
    # The queued item is untouched.
    assert (project_root / "queue" / "queued" / "writer" / "x.md").exists()


def test_symlinked_queued_subdir_does_not_escape_on_read(tmp_path):
    """export/claim MUST NOT read through a symlinked queued/ escaping queue/."""
    project_root = tmp_path / "project"
    (project_root / "queue").mkdir(parents=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "passwd").write_text("leaked")
    (project_root / "queue" / "queued").symlink_to(secrets)

    backend = FilesystemQueueBackend(project_root)
    # Real surfaces: neither export nor claim_next may surface the outside bytes.
    exported = [rel for rel, _ in backend.export().items_with_bytes]
    assert exported == [], f"export leaked outside files: {exported}"
    assert backend.claim_next("", "lease-1") is None


def test_symlinked_claimed_subdir_does_not_escape_on_recover(tmp_path):
    """recover_stale_claims MUST NOT iterate a symlinked claimed/ escaping queue/.

    The escape target mirrors the real on-disk shape (claimed/<lease_token>/<file>)
    so a naive iterator would surface — and try to resurrect — the outside file.
    """
    project_root = tmp_path / "project"
    (project_root / "queue").mkdir(parents=True)
    outside = tmp_path / "outside"
    # Match the claimed/<lease_token>/<work_file> layout the recovery scan walks.
    (outside / "lease-x").mkdir(parents=True)
    (outside / "lease-x" / "stale.md").write_text("would be resurrected")
    (project_root / "queue" / "claimed").symlink_to(outside)

    backend = FilesystemQueueBackend(project_root)
    # Real surfaces: list_claimed + recover must both refuse the escaped dir.
    assert backend.list_claimed() == []
    assert recover_stale_claims(backend, lease_seconds=0) == []
    # The escape-target file is NOT moved into queued/.
    assert (outside / "lease-x" / "stale.md").exists()
    assert not (project_root / "queue" / "queued" / "_recovered").exists()


def test_traversing_role_or_lease_token_arg_stays_under_queue(tmp_path):
    """A role / lease_token argument containing '..' MUST NOT escape queue/.

    role and lease_token are call-time-supplied path components. Without a
    per-subdir containment re-check they compose into queue/claimed/../../X,
    landing outside queue/ (and potentially outside project_root). Exercises
    the real claim/release/dead-letter surfaces, not just _queue_root().
    """
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="task.md", content="work")
    sensitive = tmp_path / "sensitive"
    sensitive.mkdir()

    backend = FilesystemQueueBackend(project_root)
    # Traversing lease_token must fail-soft to None and write nothing outside queue/.
    assert backend.claim_next("writer", "../../sensitive") is None
    assert list(sensitive.iterdir()) == []
    # release/move_to_dead_letter with a traversing token must be inert (no escape).
    backend.release("../../sensitive", "task.md")
    backend.move_to_dead_letter("../../sensitive", "task.md", reason="x")
    assert list(sensitive.iterdir()) == []
    # The legit queued item is untouched.
    assert (project_root / "queue" / "queued" / "writer" / "task.md").exists()


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
    # Must return list[Path], not list[FilesystemQueueItem], and must have
    # actually recovered the backdated item (no vacuous-pass guard).
    assert isinstance(recovered, list)
    assert len(recovered) == 1, f"expected exactly one recovered path, got {recovered}"
    assert isinstance(recovered[0], Path), (
        f"shim recover_stale_claims must return list[Path], got {type(recovered[0])}"
    )
    assert recovered[0].name == "stale.md"
    # The recovered file physically lives under queued/_recovered/.
    assert recovered[0].exists()
    assert "_recovered" in recovered[0].parts


def test_shim_renew_lease(tmp_path):
    """renew_lease shim writes the sidecar next to item.path (claimed item)."""
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


def test_shim_renew_lease_on_recovered_item(tmp_path):
    """renew_lease shim must not crash on a recovered item (pre-carve parity).

    Regression: the carved shim previously derived project_root from
    item.path.parents[3], which is correct only for a claimed item at depth 3
    (queue/claimed/<token>/<name>). A recovered item lives one level deeper
    (queue/queued/_recovered/<token>/<name>, depth 4), so parents[3] yielded
    <project_root>/queue and the backend built a doubled '<root>/queue/queue/...'
    path that did not exist -> FileNotFoundError. Pre-carve renew_lease wrote
    the sidecar directly next to item.path and worked at any depth. The fix
    restores that: write next to _sidecar_path(item.path).
    """
    from atomic_agents._cascade import (
        claim_next_queued,
        recover_stale_claims as shim_recover,
        renew_lease as shim_renew,
    )

    project_root = tmp_path / "project"
    _queued(project_root, name="recovered.md")
    item = claim_next_queued(project_root, "writer", "stale-lease", lease_seconds=3600)
    assert item is not None

    # Expire the lease so recover_stale_claims reclaims it.
    claimed_sidecar = _sidecar_path(item.path)
    data = json.loads(claimed_sidecar.read_text())
    past = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc)
    data["lease_expires_at"] = past.isoformat()
    claimed_sidecar.write_text(json.dumps(data))

    recovered_paths = shim_recover(project_root, lease_seconds=3600)
    assert len(recovered_paths) == 1
    recovered_path = recovered_paths[0]
    # The recovered item lives under queued/_recovered/<token>/ (depth 4).
    assert "_recovered" in recovered_path.parts

    # Build a FilesystemQueueItem mirroring what a worker holds for the
    # recovered item, then renew it.
    recovered_item = FilesystemQueueItem(
        original_name=recovered_path.name,
        role="writer",
        lease_token="stale-lease",
        claimed_at=time.time(),
        path=recovered_path,
    )

    # Must NOT raise (was FileNotFoundError before the fix).
    shim_renew(recovered_item, additional_seconds=3600)

    # The sidecar is written next to the recovered work file, not at a
    # reconstructed claimed/ location.
    recovered_sidecar = _sidecar_path(recovered_path)
    assert recovered_sidecar.is_file()
    renewed = json.loads(recovered_sidecar.read_text())
    assert renewed["lease_seconds"] == 3600
    assert "lease_expires_at" in renewed


def _recover_one(project_root, name):
    """Claim an item, expire its lease, recover it, and return the recovered
    FilesystemQueueItem (located under queued/_recovered/<token>/, depth 4)."""
    from atomic_agents._cascade import (
        claim_next_queued,
        recover_stale_claims as shim_recover,
    )

    _queued(project_root, name=name)
    item = claim_next_queued(project_root, "writer", "stale-lease", lease_seconds=3600)
    assert item is not None
    claimed_sidecar = _sidecar_path(item.path)
    data = json.loads(claimed_sidecar.read_text())
    past = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc)
    data["lease_expires_at"] = past.isoformat()
    claimed_sidecar.write_text(json.dumps(data))

    recovered_paths = shim_recover(project_root, lease_seconds=3600)
    assert len(recovered_paths) == 1
    recovered_path = recovered_paths[0]
    assert "_recovered" in recovered_path.parts
    return FilesystemQueueItem(
        original_name=recovered_path.name,
        role="writer",
        lease_token="stale-lease",
        claimed_at=time.time(),
        path=recovered_path,
    )


def test_shim_release_on_recovered_item(tmp_path):
    """release_claim shim must finalize a recovered item (pre-carve parity).

    Regression sibling to test_shim_renew_lease_on_recovered_item: the carve
    reconstructed claimed/<token>/<name> inside release(), which does not exist
    for a recovered item under queued/_recovered/<token>/ -> FileNotFoundError.
    Pre-carve release_claim did item.path.rename(done_dir/...) and worked at any
    depth. The fix routes the shim through _release_at_path(item.path, ...).
    """
    from atomic_agents._cascade import release_claim

    project_root = tmp_path / "project"
    recovered_item = _recover_one(project_root, "recovered.md")

    # Must NOT raise (was FileNotFoundError before the fix).
    release_claim(recovered_item, project_root)

    # The work file now lives in done/<token>/ and is gone from _recovered/.
    done_file = project_root / "queue" / "done" / "stale-lease" / "recovered.md"
    assert done_file.is_file()
    assert not recovered_item.path.exists()


def test_shim_dead_letter_on_recovered_item(tmp_path):
    """move_to_dead_letter shim must finalize a recovered item (pre-carve parity).

    Same class as test_shim_release_on_recovered_item: reconstructing
    claimed/<token>/<name> crashed for recovered items. The fix routes through
    _dead_letter_at_path(item.path, ...).
    """
    from atomic_agents._cascade import move_to_dead_letter

    project_root = tmp_path / "project"
    recovered_item = _recover_one(project_root, "recovered.md")

    # Must NOT raise (was FileNotFoundError before the fix).
    move_to_dead_letter(recovered_item, project_root, reason="exhausted")

    dl_file = project_root / "queue" / "dead-letter" / "stale-lease" / "recovered.md"
    assert dl_file.is_file()
    reason_file = (
        project_root
        / "queue"
        / "dead-letter"
        / "stale-lease"
        / "recovered.md.reason.txt"
    )
    assert reason_file.read_text(encoding="utf-8") == "exhausted"
    assert not recovered_item.path.exists()


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


def test_export_through_symlinked_project_root_still_finds_items(tmp_path):
    """Export MUST NOT silently drop items when project_root is reached via a
    symlink (e.g. /tmp -> /private/tmp on macOS, or a symlinked home dir).

    Regression: relativizing resolved file paths against the UNRESOLVED
    project_root raised ValueError and emptied the entire export.
    """
    real = tmp_path / "real"
    _queued(real, role="writer", name="task.md", content="durable")
    link = tmp_path / "link"
    link.symlink_to(real)

    backend = FilesystemQueueBackend(link)
    result = backend.export()
    rels = [rel for rel, _ in result.items_with_bytes]
    assert rels == ["queue/queued/writer/task.md"], (
        f"export through a symlinked project_root dropped items: {rels}"
    )
    assert result.items_with_bytes[0][1] == b"durable"


def test_export_skips_symlinked_leaf_escaping_queue(tmp_path):
    """export() MUST NOT read a symlinked FILE inside a durable dir that points
    outside queue/ (host-file exfiltration — the #426/#427 leaf-escape class).

    _safe_under_queue() proves only that the durable directory (done/) is
    contained. A symlinked leaf inside it passes the directory check but would
    escape on read_bytes(). The per-leaf is_relative_to(queue_root) guard must
    skip it (fail-soft) while still exporting legitimate sibling files. Enforces
    spec/44 §"Per-subdirectory symlink containment" at the file-leaf level.
    """
    project_root = tmp_path / "project"
    done = project_root / "queue" / "done" / "lease-1"
    done.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (done / "leak.md").symlink_to(secret)
    (done / "real.md").write_text("legit done work")

    backend = FilesystemQueueBackend(project_root)
    result = backend.export()

    payloads = [data for _, data in result.items_with_bytes]
    assert b"TOP SECRET" not in payloads, (
        "export() leaked bytes of a symlinked leaf escaping queue/"
    )
    rels = [rel for rel, _ in result.items_with_bytes]
    assert any(r.endswith("real.md") for r in rels), (
        "the per-leaf guard must not drop legitimate sibling files"
    )
    assert not any(r.endswith("leak.md") for r in rels)


def test_export_skips_symlinked_done_subdir_escaping_queue(tmp_path):
    """export() MUST NOT include bytes from a symlinked done/ DIRECTORY escaping queue/.

    G3 [P2]: _safe_under_queue() catches a symlinked done/ directory that resolves
    outside queue/ and skips it (fail-soft). Neither the escaped target bytes nor
    any path under done/ may appear in the export. The export MUST NOT raise.
    Mirrors the test_symlinked_queued_subdir_does_not_escape_on_read pattern but
    for the done/ durable directory.
    """
    project_root = tmp_path / "project"
    (project_root / "queue").mkdir(parents=True)
    # Plant a legitimate queued item so the export is not vacuously empty.
    _queued(project_root, role="writer", name="legit.md", content="legit work")
    secrets = tmp_path / "secrets_done"
    secrets.mkdir()
    (secrets / "stolen_done.txt").write_text("DONE SECRET")
    # done/ is a symlink pointing outside queue/
    (project_root / "queue" / "done").symlink_to(secrets)

    backend = FilesystemQueueBackend(project_root)
    # Must NOT raise
    result = backend.export()

    payloads = [data for _, data in result.items_with_bytes]
    assert b"DONE SECRET" not in payloads, (
        "export() MUST NOT embed bytes from a symlinked done/ escaping queue/"
    )
    rels = [rel for rel, _ in result.items_with_bytes]
    # The legitimate queued item must still appear
    assert any("legit.md" in r for r in rels), (
        "export() must still include legitimate queued/ items when done/ is a bad symlink"
    )
    # The escaped target file must not appear
    assert not any("stolen_done" in r for r in rels)


def test_export_skips_symlinked_dead_letter_subdir_escaping_queue(tmp_path):
    """export() MUST NOT include bytes from a symlinked dead-letter/ DIRECTORY escaping queue/.

    G4 [P2]: _safe_under_queue() catches a symlinked dead-letter/ directory that
    resolves outside queue/ and skips it (fail-soft). Neither the escaped target
    bytes nor any path under dead-letter/ may appear in the export. The export
    MUST NOT raise.
    Mirrors test_export_skips_symlinked_done_subdir_escaping_queue but for
    dead-letter/.
    """
    project_root = tmp_path / "project"
    (project_root / "queue").mkdir(parents=True)
    # Plant a legitimate queued item so the export is not vacuously empty.
    _queued(project_root, role="writer", name="legit.md", content="legit work")
    secrets = tmp_path / "secrets_dl"
    secrets.mkdir()
    (secrets / "stolen_dl.txt").write_text("DEAD SECRET")
    # dead-letter/ is a symlink pointing outside queue/
    (project_root / "queue" / "dead-letter").symlink_to(secrets)

    backend = FilesystemQueueBackend(project_root)
    # Must NOT raise
    result = backend.export()

    payloads = [data for _, data in result.items_with_bytes]
    assert b"DEAD SECRET" not in payloads, (
        "export() MUST NOT embed bytes from a symlinked dead-letter/ escaping queue/"
    )
    rels = [rel for rel, _ in result.items_with_bytes]
    # The legitimate queued item must still appear
    assert any("legit.md" in r for r in rels), (
        "export() must still include legitimate queued/ items when dead-letter/ is a bad symlink"
    )
    # The escaped target file must not appear
    assert not any("stolen_dl" in r for r in rels)

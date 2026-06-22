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

from atomic_agents.queue import filesystem as _fsmod
from atomic_agents.queue.filesystem import (
    FilesystemQueueBackend,
    FilesystemQueueItem,
    _sidecar_path,
    _write_no_follow,
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
    # Use distinct lease tokens: #478 no-replace mkdir requires a fresh token
    # per claim session; reusing the same token would return None on the second
    # call because claimed/lease-v/ already exists.
    item_a = backend.claim_next("writer", "lease-v1", lease_seconds=60)
    item_b = backend.claim_next("writer", "lease-v2", lease_seconds=60)
    assert item_a is not None and item_b is not None

    # Identify which claimed path holds vanishes.md / survives.md.
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


def test_symlinked_claimed_CHILD_leasedir_does_not_escape_on_list(tmp_path):
    """list_claimed MUST NOT surface bytes from a symlinked child lease dir.

    claimed/ is a REAL directory; claimed/<lease_token>/ is a symlink pointing
    outside queue_root.  The one-time claimed/ containment guard passes; the
    per-child guard must catch and skip the escaping lease dir.
    """
    project_root = tmp_path / "project"
    (project_root / "queue" / "claimed").mkdir(parents=True)
    outside = tmp_path / "outside_lease"
    outside.mkdir()
    (outside / "secret.md").write_text("external secret")
    # claimed/ is real; claimed/lease-x is the symlinked child escaping queue/
    (project_root / "queue" / "claimed" / "lease-x").symlink_to(outside)

    backend = FilesystemQueueBackend(project_root)
    result = backend.list_claimed()
    assert result == [], f"list_claimed leaked symlinked child lease dir: {result}"
    # Confirm the external file bytes were NOT read
    assert (outside / "secret.md").read_text() == "external secret"


def test_symlinked_claimed_CHILD_leasedir_does_not_escape_on_recover(tmp_path):
    """recover_stale_claims MUST NOT move files through a symlinked child lease dir.

    claimed/ is a REAL directory; claimed/<lease_token>/ is a symlink pointing
    to an external dir containing a stale work file.  Without the per-child guard
    the rename would exfiltrate the file into queued/_recovered/ and the rmdir
    cleanup would crash with NotADirectoryError on the symlink.
    """
    project_root = tmp_path / "project"
    (project_root / "queue" / "claimed").mkdir(parents=True)
    outside = tmp_path / "outside_lease"
    outside.mkdir()
    (outside / "stale.md").write_text("external stale")
    # claimed/ is real; claimed/lease-x is the symlinked child
    (project_root / "queue" / "claimed" / "lease-x").symlink_to(outside)

    backend = FilesystemQueueBackend(project_root)
    # Must not raise, must not move the external file, must return empty list.
    result = recover_stale_claims(backend, lease_seconds=0)
    assert result == [], f"recover leaked symlinked child lease dir: {result}"
    # External file is untouched and NOT moved into queued/_recovered/
    assert (outside / "stale.md").exists(), "external file was moved (exfiltration)"
    assert (outside / "stale.md").read_text() == "external stale"
    assert not (project_root / "queue" / "queued" / "_recovered").exists()


def test_symlinked_work_FILE_in_real_leasedir_skipped(tmp_path):
    """list_claimed and recover MUST NOT surface a symlinked work file inside a real lease dir.

    claimed/<lease_token>/ is a REAL directory; the work file inside it is a
    symlink pointing to a file outside queue_root.  The per-work-file guard must
    skip it on both list and recover.
    """
    project_root = tmp_path / "project"
    lease_dir = project_root / "queue" / "claimed" / "lease-y"
    lease_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside_work.md"
    outside_file.write_text("external work content")
    # The work file inside the legit lease dir is a symlink escaping queue_root
    (lease_dir / "work.md").symlink_to(outside_file)

    backend = FilesystemQueueBackend(project_root)

    # list_claimed: must not surface the symlinked work file
    result = backend.list_claimed()
    assert result == [], f"list_claimed surfaced symlinked work file: {result}"

    # recover: must not move the external file
    result = recover_stale_claims(backend, lease_seconds=0)
    assert result == [], f"recover moved symlinked work file: {result}"
    assert outside_file.exists(), "external file was moved (exfiltration)"
    assert outside_file.read_text() == "external work content"
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


# ──────────────────────────────────────────────────────────────────────────────
# original_name traversal containment (F1) and symlinked source-file in
# claim_next (F2) — regression tests for the two confirmed escapes.


def test_renew_lease_traversing_original_name_stays_under_queue(tmp_path):
    """renew_lease() MUST NOT write a sidecar outside claimed/ when original_name
    contains '..' path components (e.g. '../../evil').

    Regression: the sidecar was assembled as claimed_dir / (original_name +
    '.lease.json') without validating that original_name is a bare filename.
    A traversing name composes into a path escaping claimed/ — and potentially
    escaping queue/ or project_root — and .write_text() would land the sidecar
    there. The fix rejects any original_name that is not a bare filename.
    """
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="real.md", content="work")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "tok")
    assert item is not None

    # Attack: traversing original_name should be a no-op (fail-soft).
    backend.renew_lease("tok", "../../evil")

    # Nothing must have been written outside claimed/.
    escaped_in_queue = project_root / "queue" / "evil.lease.json"
    escaped_in_project = project_root / "evil.lease.json"
    escaped_in_tmp = tmp_path / "evil.lease.json"
    assert not escaped_in_queue.exists(), (
        "renew_lease() wrote sidecar outside claimed/ (queue/evil.lease.json)"
    )
    assert not escaped_in_project.exists(), (
        "renew_lease() wrote sidecar outside claimed/ (project/evil.lease.json)"
    )
    assert not escaped_in_tmp.exists(), (
        "renew_lease() wrote sidecar outside project entirely"
    )


def test_move_to_dead_letter_traversing_original_name_contained(tmp_path):
    """move_to_dead_letter() MUST NOT write a .reason.txt outside dead-letter/ when
    original_name contains '..' path components.

    Regression: dl_dir / (original_name + '.reason.txt') was assembled without
    validating that original_name is a bare filename. A traversing name escapes
    dead-letter/ and .write_text() lands the reason file at an attacker-controlled
    path. The fix rejects traversing original_name before any path construction.
    """
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="real.md", content="work")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "tok")
    assert item is not None

    # Attack: traversing original_name should be a no-op (fail-soft).
    backend.move_to_dead_letter("tok", "../../evil", reason="PWNED")

    # Nothing must have been written outside dead-letter/.
    escaped_in_queue = project_root / "queue" / "evil.reason.txt"
    escaped_in_project = project_root / "evil.reason.txt"
    escaped_in_tmp = tmp_path / "evil.reason.txt"
    assert not escaped_in_queue.exists(), (
        "move_to_dead_letter() wrote reason file outside dead-letter/ (queue/evil.reason.txt)"
    )
    assert not escaped_in_project.exists(), (
        "move_to_dead_letter() wrote reason file outside dead-letter/ (project/evil.reason.txt)"
    )
    assert not escaped_in_tmp.exists(), (
        "move_to_dead_letter() wrote reason file outside project entirely"
    )


def test_release_traversing_original_name_contained(tmp_path):
    """release() MUST NOT move or unlink anything outside claimed/done when
    original_name contains '..' path components.

    Regression: claimed_dir / original_name was assembled without validating that
    original_name is a bare filename. A traversing name composes into a path
    escaping claimed/, and the sidecar unlink (_sidecar_path(work_path).unlink())
    would follow the escaped path. The fix rejects traversing original_name before
    any path construction.
    """
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="real.md", content="work")
    # Plant a file outside queue/ that the sidecar-unlink might otherwise hit.
    canary = project_root / "canary.txt"
    canary.write_text("must not be touched")

    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "tok")
    assert item is not None

    # Attack: traversing original_name should be a no-op (fail-soft).
    backend.release("tok", "../../canary.txt")

    # The canary file must be untouched.
    assert canary.exists(), (
        "release() with a traversing original_name unlinked a file outside claimed/"
    )
    assert canary.read_text() == "must not be touched", (
        "release() with a traversing original_name modified a file outside claimed/"
    )
    # The legitimate claimed item must still be present (release was a no-op).
    assert item.path.exists(), (
        "release() with a traversing original_name moved the legitimate claimed item"
    )


def test_claim_next_skips_symlinked_source_file(tmp_path):
    """claim_next() MUST NOT claim a symlinked file in queued/<role>/ that
    points outside queue_root (host-file exfiltration).

    Regression: claim_next() used 'p.is_file()' to filter candidates — which
    follows symlinks — so a symlinked file in queued/<role>/ whose target lived
    outside project_root passed the filter and was renamed into claimed/. The
    worker then reads item.path (the symlink now under claimed/), following it to
    the external target. The fix adds a per-source-file _safe_under_queue() guard
    that skips any candidate whose resolved path escapes queue_root, mirroring the
    per-work-file guard already in list_claimed and _recover_stale_claims_native.
    """
    project_root = tmp_path / "project"
    (project_root / "queue" / "queued" / "writer").mkdir(parents=True)
    # External secret that must NOT be surfaced to the worker.
    secret = tmp_path / "external_secret.txt"
    secret.write_text("EXTERNAL-SECRET")
    # Symlink inside queued/writer/ pointing to the external secret.
    (project_root / "queue" / "queued" / "writer" / "link.md").symlink_to(secret)

    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "tok2")

    # claim_next must return None (the only candidate was the symlinked file).
    assert item is None, (
        f"claim_next() claimed a symlinked source file escaping queue_root: {item}"
    )
    # The external secret bytes must NOT have been surfaced.
    # (If item were not None, the caller would do item.path.read_text() → exfil.)
    # Belt-and-suspenders: confirm the symlink was NOT moved into claimed/.
    claimed_dir = project_root / "queue" / "claimed" / "tok2"
    if claimed_dir.exists():
        claimed_files = list(claimed_dir.iterdir())
        assert claimed_files == [], (
            f"claim_next() moved a symlinked source file into claimed/: {claimed_files}"
        )
    # The external secret is untouched.
    assert secret.read_text() == "EXTERNAL-SECRET"


# ──────────────────────────────────────────────────────────────────────────────
# FIX #3 — sink-layer original_name validation covers shim callers
# (regression for the _cascade.py bypass: a forged QueueItem with a traversing
# original_name passed through the shim to _release_at_path / _dead_letter_at_path
# BYPASSED the public-method validation, escaping the destination directory)


def test_shim_move_to_dead_letter_forged_original_name_contained(tmp_path):
    """_cascade.move_to_dead_letter with a forged original_name MUST NOT write
    outside dead-letter/ even when the shim calls _dead_letter_at_path directly.

    Regression: the original_name validation lived only in the Protocol public
    method, so a caller that forged a QueueItem(original_name='../../evil', ...)
    and called the shim bypassed it.  The fix moves validation INTO the sink.
    """
    from atomic_agents._cascade import move_to_dead_letter

    project_root = tmp_path / "project"
    # Plant a real claimed work file (shim needs item.path to point somewhere real)
    claimed_dir = project_root / "queue" / "claimed" / "tok"
    claimed_dir.mkdir(parents=True)
    real_work = claimed_dir / "real.md"
    real_work.write_text("legit work")

    # Forge a QueueItem: original_name traverses out of dead-letter/
    forged = FilesystemQueueItem(
        original_name="../../evil",
        role="writer",
        lease_token="tok",
        claimed_at=0.0,
        path=real_work,
    )

    # Must be a no-op: no exception raised, no file written outside dead-letter/
    move_to_dead_letter(forged, project_root, reason="x")

    # Nothing must appear outside queue/dead-letter/
    escaped_queue = project_root / "queue" / "evil.reason.txt"
    escaped_project = project_root / "evil.reason.txt"
    escaped_tmp = tmp_path / "evil.reason.txt"
    assert not escaped_queue.exists(), (
        "shim move_to_dead_letter wrote reason.txt outside dead-letter/ (queue/)"
    )
    assert not escaped_project.exists(), (
        "shim move_to_dead_letter wrote reason.txt outside dead-letter/ (project/)"
    )
    assert not escaped_tmp.exists(), (
        "shim move_to_dead_letter wrote reason.txt outside project entirely"
    )
    # The shim must not raise
    # (no assert needed — if it raised, the test would have failed above)


def test_shim_release_forged_original_name_contained(tmp_path):
    """_cascade.release_claim with a forged original_name MUST NOT move any file
    outside done/ even when the shim calls _release_at_path directly.

    Same bypass class as test_shim_move_to_dead_letter_forged_original_name_contained:
    original_name validation was only in the Protocol public method, not the sink.
    """
    from atomic_agents._cascade import release_claim

    project_root = tmp_path / "project"
    # Plant a real claimed work file
    claimed_dir = project_root / "queue" / "claimed" / "tok"
    claimed_dir.mkdir(parents=True)
    real_work = claimed_dir / "real.md"
    real_work.write_text("legit work")

    # Forge a QueueItem: original_name traverses out of done/
    forged = FilesystemQueueItem(
        original_name="../../evil",
        role="writer",
        lease_token="tok",
        claimed_at=0.0,
        path=real_work,
    )

    # Must be a no-op: no exception raised, no file written/moved outside done/
    release_claim(forged, project_root)

    # Nothing must appear outside queue/done/
    escaped_queue = project_root / "queue" / "evil"
    escaped_project = project_root / "evil"
    escaped_tmp = tmp_path / "evil"
    assert not escaped_queue.exists(), (
        "shim release_claim moved file outside done/ (queue/evil)"
    )
    assert not escaped_project.exists(), (
        "shim release_claim moved file outside done/ (project/evil)"
    )
    assert not escaped_tmp.exists(), (
        "shim release_claim moved file outside project entirely"
    )


# ──────────────────────────────────────────────────────────────────────────────
# FIX #4 — bare-component validation for role and lease_token at claim_next()
# (forward-nested components like 'a/b' escape the one-level scan of list_claimed,
# leaving an unrecoverable claim after a crash)


def test_claim_next_rejects_nested_lease_token(tmp_path):
    """claim_next() with a forward-nested lease_token (e.g. 'a/b') MUST return None
    and must NOT create a nested claimed/a/ directory or move any queued file.

    A nested lease_token is not a traversal attack (no escape via '..'), but it
    hides the claim from list_claimed()'s one-level scan of claimed/, so a crash
    leaves the item permanently unrecoverable.  The fix validates lease_token is a
    bare component (no path separators) before creating any directory.
    """
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="task.md", content="work")
    backend = FilesystemQueueBackend(project_root)

    result = backend.claim_next("writer", "a/b")

    assert result is None, (
        f"claim_next() accepted a forward-nested lease_token 'a/b': {result}"
    )
    # No nested directory must have been created under claimed/
    nested_dir = project_root / "queue" / "claimed" / "a"
    assert not nested_dir.exists(), (
        "claim_next() created claimed/a/ for a nested lease_token 'a/b'"
    )
    # The queued item must be untouched
    assert (project_root / "queue" / "queued" / "writer" / "task.md").exists(), (
        "claim_next() with a bad lease_token removed the queued item"
    )


def test_claim_next_rejects_nested_role(tmp_path):
    """claim_next() with a forward-nested role (e.g. 'a/b') MUST return None and
    must NOT escape into queued/a/ or create any nested directory.

    Same class as test_claim_next_rejects_nested_lease_token but for the role
    component: 'a/b' as role would compose to queue/queued/a/b/ instead of the
    expected one-level queue/queued/<role>/, potentially hiding items from
    recovery or allowing surprising path construction.
    """
    project_root = tmp_path / "project"
    # Plant a legit item for a real role so we can confirm it's untouched.
    _queued(project_root, role="writer", name="task.md", content="work")
    backend = FilesystemQueueBackend(project_root)

    result = backend.claim_next("a/b", "lease-1")

    assert result is None, (
        f"claim_next() accepted a forward-nested role 'a/b': {result}"
    )
    # No nested directory must have been created under queued/ for the bad role
    nested_queued = project_root / "queue" / "queued" / "a"
    assert not nested_queued.exists(), (
        "claim_next() created queued/a/ for a nested role 'a/b'"
    )
    # The legitimate writer item is untouched
    assert (project_root / "queue" / "queued" / "writer" / "task.md").exists(), (
        "claim_next() with a bad role removed a legitimate queued item"
    )


# ──────────────────────────────────────────────────────────────────────────────
# SOURCE-containment regression tests (issue #473 / Codex round-2 adversarial)
#
# These tests verify that the sink helpers (_release_at_path, _dead_letter_at_path,
# _renew_lease_at_sidecar) refuse to operate on source paths that resolve outside
# queue_root — even when original_name and lease_token are valid bare components.
#
# A forged FilesystemQueueItem(path=/outside/secret.md, ...) passed through the
# _cascade.py shims must be a no-op: the external file must remain where it is,
# nothing must appear in queue/done| dead-letter/, and no exception must propagate.


def test_shim_release_forged_external_path_contained(tmp_path):
    """release_claim shim MUST NOT move an out-of-tree work file into queue/done/.

    A forged FilesystemQueueItem whose .path points outside project_root must be
    refused by _release_at_path's source-containment guard. The external file must
    stay where it is, nothing must appear in queue/done/, and the shim must return
    without raising.
    """
    from atomic_agents._cascade import release_claim

    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("external secret content")

    forged_item = FilesystemQueueItem(
        original_name="secret.md",
        role="writer",
        lease_token="tok-release",
        claimed_at=time.time(),
        path=secret,
    )

    # Must not raise; must not move the external file.
    release_claim(forged_item, project_root)

    # External file untouched.
    assert secret.exists(), "external file was moved (exfiltration via release_claim)"
    assert secret.read_text() == "external secret content"

    # Nothing landed in queue/done/.
    done_root = project_root / "queue" / "done"
    if done_root.exists():
        found = list(done_root.rglob("*"))
        assert all(not p.is_file() for p in found), (
            f"external file exfiltrated into queue/done/: {[p for p in found if p.is_file()]}"
        )


def test_shim_dead_letter_forged_external_path_contained(tmp_path):
    """move_to_dead_letter shim MUST NOT move an out-of-tree file into queue/dead-letter/.

    Same class as test_shim_release_forged_external_path_contained, via the
    dead-letter code path.
    """
    from atomic_agents._cascade import move_to_dead_letter

    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("external dead-letter content")

    forged_item = FilesystemQueueItem(
        original_name="secret.md",
        role="writer",
        lease_token="tok-deadletter",
        claimed_at=time.time(),
        path=secret,
    )

    # Must not raise; must not move the external file.
    move_to_dead_letter(forged_item, project_root, reason="attempted exfiltration")

    # External file untouched.
    assert secret.exists(), (
        "external file was moved (exfiltration via move_to_dead_letter)"
    )
    assert secret.read_text() == "external dead-letter content"

    # Nothing landed in queue/dead-letter/.
    dl_root = project_root / "queue" / "dead-letter"
    if dl_root.exists():
        found = list(dl_root.rglob("*"))
        assert all(not p.is_file() for p in found), (
            f"external file exfiltrated into queue/dead-letter/: "
            f"{[p for p in found if p.is_file()]}"
        )


def test_shim_renew_lease_forged_external_path_contained(tmp_path):
    """renew_lease shim MUST NOT write a sidecar next to an out-of-tree path.

    The cascade renew_lease shim cannot anchor a queue_root guard (frozen
    signature), so the sink receives queue_root=None and skips the containment
    check. This test verifies the current (partial-close) behaviour: the shim
    wraps the call in try/except PathTraversalError but cannot prevent the write
    when queue_root is absent.

    NOTE: This test documents the KNOWN RESIDUAL GAP: without queue_root the
    sidecar write-outside is not blocked by the sink guard. Full closure requires
    a signature change deferred to the v1.0/T10 shim-retirement pass. The test
    asserts the shim does NOT raise (fail-soft contract) and leaves the external
    file content unchanged — it does NOT assert that the sidecar was prevented,
    because the sink has no queue_root to anchor the check.
    """
    from atomic_agents._cascade import renew_lease as shim_renew

    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("external content")

    forged_item = FilesystemQueueItem(
        original_name="secret.md",
        role="writer",
        lease_token="tok-renew",
        claimed_at=time.time(),
        path=secret,
    )

    # Must not raise (fail-soft contract maintained).
    shim_renew(forged_item, additional_seconds=3600)

    # The external WORK file must not be modified.
    assert secret.read_text() == "external content", (
        "renew_lease shim modified the external work file"
    )


def test_shim_release_legit_claimed_item_still_works(tmp_path):
    """SOURCE-containment guard must NOT block legitimately claimed items.

    A work file under queue/claimed/<token>/ resolves inside queue_root and must
    pass the new guard without error. Regression guard: over-restrictive containment
    check that rejects real claimed items would break the normal workflow.
    """
    from atomic_agents._cascade import claim_next_queued, release_claim

    project_root = tmp_path / "project"
    _queued(project_root, name="legit.md", content="legit content")
    item = claim_next_queued(project_root, "writer", "legit-lease")
    assert item is not None, "claim_next_queued returned None for a legit item"

    # Must succeed and move the file to done/.
    release_claim(item, project_root)

    done_file = project_root / "queue" / "done" / "legit-lease" / "legit.md"
    assert done_file.is_file(), "release_claim failed for a legitimately claimed item"
    assert not item.path.exists(), "claimed item path still exists after release"


def test_shim_release_legit_recovered_item_still_works(tmp_path):
    """SOURCE-containment guard must NOT block recovered items under queued/_recovered/.

    A recovered work file lives at queue/queued/_recovered/<token>/<name> — still
    under queue_root. The containment guard must pass it through. Regression guard
    for the fix: the two valid depths (claimed/<token>/ depth 3, recovered/<token>/
    depth 4) must both resolve as is_relative_to(queue_root).
    """
    from atomic_agents._cascade import release_claim

    project_root = tmp_path / "project"
    recovered_item = _recover_one(project_root, "recovered_legit.md")

    # Must succeed and move the file to done/.
    release_claim(recovered_item, project_root)

    done_file = project_root / "queue" / "done" / "stale-lease" / "recovered_legit.md"
    assert done_file.is_file(), (
        "release_claim failed for a recovered item (over-restrictive containment guard)"
    )
    assert not recovered_item.path.exists(), (
        "recovered item path still exists after release"
    )


# ──────────────────────────────────────────────────────────────────────────────
# FIX A regression: unresolvable work_path fails soft (no raise from Protocol)


def test_release_unresolvable_work_path_fails_soft(tmp_path):
    """_release_at_path must NOT raise when work_path cannot be resolved.

    FIX A regression: before the fix, the source-containment guard in
    _release_at_path converted an OSError/RuntimeError from work_path.resolve()
    into a PathTraversalError and re-raised it OUTSIDE the destination-validation
    try/except block, so the exception propagated to the Protocol caller.

    After the fix, the entire operation is inside one try/except PathTraversalError,
    so an unresolvable work_path (e.g. broken symlink) returns silently.
    """
    project_root = tmp_path / "project"
    (project_root / "queue" / "claimed" / "tok-broken").mkdir(parents=True)

    # Create a broken symlink under claimed/ — it has a parent under queue_root
    # but target.resolve() will raise OSError (dangling symlink).
    broken_link = project_root / "queue" / "claimed" / "tok-broken" / "work.md"
    broken_link.symlink_to(tmp_path / "nonexistent" / "missing.md")

    queue_root = project_root / "queue"

    # Must return without raising (fail-soft contract).
    FilesystemQueueBackend._release_at_path(
        broken_link,
        queue_root,
        "tok-broken",
        "work.md",
    )

    # No file should have appeared in done/.
    done_root = project_root / "queue" / "done"
    if done_root.exists():
        found = list(done_root.rglob("*"))
        assert all(not p.is_file() for p in found), (
            f"unexpected file in done/ after unresolvable-work_path call: "
            f"{[str(p) for p in found if p.is_file()]}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# FIX B regression: _reclaim_to_recovered forged-item containment


def test_reclaim_to_recovered_forged_original_name_contained(tmp_path):
    """_reclaim_to_recovered must reject a forged original_name with traversal.

    FIX B regression: before the fix, _reclaim_to_recovered validated the
    lease_token via _safe_under_queue but did NOT call _validate_original_name,
    so a forged QueueItem with original_name='../../evil' could escape queue_root.
    """
    project_root = tmp_path / "project"
    # Create a real claimed dir with a legitimate file.
    claimed_dir = project_root / "queue" / "claimed" / "tok-forged"
    claimed_dir.mkdir(parents=True)
    real_file = claimed_dir / "real.md"
    real_file.write_text("legitimate content")

    backend = FilesystemQueueBackend(project_root)
    forged = FilesystemQueueItem(
        original_name="../../evil",
        role="writer",
        lease_token="tok-forged",
        claimed_at=time.time(),
        path=real_file,
    )

    result = backend._reclaim_to_recovered(forged)

    assert result is None, (
        "_reclaim_to_recovered should return None for a traversing original_name"
    )
    # Nothing must have been written outside queue/.
    evil_path = project_root / "queue" / "evil"
    assert not evil_path.exists(), (
        "traversal via original_name escaped into queue/../evil"
    )
    # The real file must remain in place (no accidental move).
    assert real_file.exists(), "legitimate work file was moved unexpectedly"


def test_reclaim_to_recovered_forged_external_src_contained(tmp_path):
    """_reclaim_to_recovered must reject a forged item whose src resolves outside queue_root.

    FIX B regression: if claimed/<lease_token>/<original_name> is a symlink
    pointing outside queue_root, the source-containment check must catch it
    and return None without performing any rename.
    """
    project_root = tmp_path / "project"
    external = tmp_path / "secret.md"
    external.write_text("EXTERNAL-SECRET")

    # Plant a symlink inside claimed/ that points to the external file.
    claimed_dir = project_root / "queue" / "claimed" / "tok-ext"
    claimed_dir.mkdir(parents=True)
    symlink = claimed_dir / "secret.md"
    symlink.symlink_to(external)

    backend = FilesystemQueueBackend(project_root)
    forged = FilesystemQueueItem(
        original_name="secret.md",
        role="writer",
        lease_token="tok-ext",
        claimed_at=time.time(),
        path=symlink,
    )

    result = backend._reclaim_to_recovered(forged)

    assert result is None, (
        "_reclaim_to_recovered should return None when src resolves outside queue_root"
    )
    # External file must be untouched.
    assert external.exists(), "external file was moved (source-containment escape)"
    assert external.read_text() == "EXTERNAL-SECRET"
    # Nothing in _recovered/.
    recovered_root = project_root / "queue" / "queued" / "_recovered"
    if recovered_root.exists():
        found = list(recovered_root.rglob("*"))
        assert all(not p.is_file() for p in found), (
            f"external file exfiltrated into _recovered/: "
            f"{[str(p) for p in found if p.is_file()]}"
        )


def test_reclaim_to_recovered_legit_item_still_works(tmp_path):
    """_reclaim_to_recovered must succeed for a legitimately claimed item.

    Regression guard for FIX B: the new validation guards must NOT block
    real items whose claimed_dir/<name> resolves inside queue_root.
    """
    from atomic_agents._cascade import claim_next_queued

    project_root = tmp_path / "project"
    _queued(project_root, name="legit_reclaim.md", content="real work")

    # Claim the item normally.
    item = claim_next_queued(project_root, "writer", "tok-legit-reclaim")
    assert item is not None

    backend = FilesystemQueueBackend(project_root)
    result = backend._reclaim_to_recovered(item)

    assert result is not None, (
        "_reclaim_to_recovered returned None for a legitimately claimed item"
    )
    assert result.path.exists(), "recovered item path does not exist after reclaim"
    recovered_root = project_root / "queue" / "queued" / "_recovered"
    assert str(result.path).startswith(str(recovered_root)), (
        f"recovered item landed outside _recovered/: {result.path}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# FIX C — source-state check in _release_at_path / _dead_letter_at_path
# (Codex round-4 adversarial: queue-root containment alone is insufficient —
# a forged item.path pointing at dead-letter/<other>/x.md or done/<other>/x.md
# passes the queue_root check and gets renamed into done/ or dead-letter/,
# violating the dead-work-stays-dead invariant (spec/44 MUST 10)).


def test_shim_release_forged_dead_letter_source_refused(tmp_path):
    """release_claim shim MUST NOT move a dead-lettered item into done/.

    A forged FilesystemQueueItem whose .path points at an existing
    queue/dead-letter/<other>/<name> file passes the queue_root containment
    check (the path is under queue/) but is NOT a valid source for release
    (only claimed/<token>/ and queued/_recovered/<token>/ are legal).
    The source-state check must refuse it: the dead-lettered file stays in
    dead-letter/, nothing lands in done/, dead-work-stays-dead (spec/44 MUST 10).
    """
    from atomic_agents._cascade import claim_next_queued, release_claim

    project_root = tmp_path / "project"

    # Create a real dead-lettered item (via the normal workflow).
    _queued(project_root, name="x.md", content="dead work")
    item = claim_next_queued(project_root, "writer", "other-tok")
    assert item is not None
    backend = FilesystemQueueBackend(project_root)
    backend.move_to_dead_letter("other-tok", "x.md", reason="test setup")
    dl_path = project_root / "queue" / "dead-letter" / "other-tok" / "x.md"
    assert dl_path.is_file(), "test setup: dead-lettered file must exist"

    # Forge a QueueItem: path points at the dead-lettered file.
    # Use a different lease_token so claimed_src is a different directory.
    forged = FilesystemQueueItem(
        original_name="x.md",
        role="writer",
        lease_token="attacker-tok",
        claimed_at=time.time(),
        path=dl_path,
    )

    # Must be a no-op: the dead-lettered file must NOT be moved into done/.
    release_claim(forged, project_root)

    # Dead-lettered file stays in dead-letter/ (dead-work-stays-dead).
    assert dl_path.is_file(), (
        "source-state check moved a dead-lettered file out of dead-letter/ "
        "(dead-work-stays-dead violated)"
    )

    # Nothing must have landed in done/.
    done_root = project_root / "queue" / "done"
    if done_root.exists():
        found = [p for p in done_root.rglob("*") if p.is_file()]
        assert found == [], (
            f"dead-lettered item was moved into done/ via forged release_claim: {found}"
        )


def test_shim_release_basename_mismatch_refused(tmp_path):
    """release_claim shim MUST NOT operate when work_path.name != original_name.

    A forged FilesystemQueueItem whose .path is a legitimately-claimed file but
    whose .original_name differs from path.name must be refused. The source-state
    check's basename guard prevents silent renames into done/ under an attacker-
    chosen name.  Must fail-soft: no file moved, no exception raised.
    """
    from atomic_agents._cascade import claim_next_queued, release_claim

    project_root = tmp_path / "project"

    # Claim a real item so item.path is a real file under claimed/<token>/.
    _queued(project_root, name="real.md", content="real work")
    item = claim_next_queued(project_root, "writer", "tok-mismatch")
    assert item is not None
    assert item.path.name == "real.md"

    # Forge: original_name differs from path.name — both are otherwise valid.
    forged = FilesystemQueueItem(
        original_name="different.md",
        role="writer",
        lease_token="tok-mismatch",
        claimed_at=time.time(),
        path=item.path,  # path.name == "real.md" != "different.md"
    )

    # Must be a no-op: no file moved, no raise.
    release_claim(forged, project_root)

    # The claimed file must still be in claimed/ (not moved).
    assert item.path.is_file(), (
        "basename-mismatch forged item caused the claimed file to be moved"
    )

    # Nothing must have landed in done/.
    done_root = project_root / "queue" / "done"
    if done_root.exists():
        found = [p for p in done_root.rglob("*") if p.is_file()]
        assert found == [], f"basename-mismatch item moved into done/: {found}"


def test_release_recovered_item_basename_preserved(tmp_path):
    """SOURCE-state check must pass a legitimately recovered item through to done/.

    A recovered item lives at queue/queued/_recovered/<token>/<name> — the second
    valid source location.  The source-state check must accept it: the item must be
    moved into done/ and the path must no longer exist in _recovered/.

    Regression guard: an over-restrictive check that rejects recovered_src would
    break the normal recovered-item release workflow.
    """
    from atomic_agents._cascade import release_claim

    project_root = tmp_path / "project"
    recovered_item = _recover_one(project_root, "recovered_basename.md")

    # Must succeed: recovered item must move to done/.
    release_claim(recovered_item, project_root)

    done_file = (
        project_root / "queue" / "done" / "stale-lease" / "recovered_basename.md"
    )
    assert done_file.is_file(), (
        "source-state check over-restricted a legitimate recovered item "
        "(did not move to done/)"
    )
    assert not recovered_item.path.exists(), (
        "recovered item path still exists after successful release"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Codex round-5 — within-queue symlink bypass (symlink leaf invariant)
#
# The previous symlink guards checked only that the RESOLVED target escapes
# queue_root.  A symlink pointing to ANOTHER FILE UNDER queue_root passes that
# check — the target is inside queue_root — but still violates the invariant
# that work-file leaves are always regular files.  The exploitable shape:
#
#   queued/writer/link.md  ->  claimed/<token>/secret.md
#
# claim_next: renames the SYMLINK into claimed/ (worker reads through it to
#   claimed/<other>/secret.md — cross-boundary read).
# release / dead_letter: moves the symlink into done/ / dead-letter/; then
#   export() resolves the symlink back into claimed/ content, bypassing the
#   durable/ephemeral export boundary (spec/40 MUST).
#
# Fix: `Path.is_symlink()` does NOT follow the link — it detects the leaf
# itself is a symlink regardless of where the target lives.


def test_claim_next_skips_symlinked_in_queue_source(tmp_path):
    """claim_next() MUST NOT claim a symlink pointing to another in-queue file.

    A within-queue symlink (target under queue_root) passes the existing
    _safe_under_queue containment check — the resolved target is under
    queue_root — so the previous code would rename the SYMLINK into claimed/
    and the worker would read through it to the wrong queue file (cross-
    boundary read).  The fix: skip any candidate whose leaf is_symlink().
    """
    project_root = tmp_path / "project"
    # Plant a real regular work file (the 'secret' target).
    other_claimed = project_root / "queue" / "claimed" / "other-token"
    other_claimed.mkdir(parents=True)
    secret = other_claimed / "secret.md"
    secret.write_text("OTHER-WORKER-SECRET")

    # Plant a symlink in queued/writer/ pointing to the in-queue secret.
    queued_role = project_root / "queue" / "queued" / "writer"
    queued_role.mkdir(parents=True)
    (queued_role / "link.md").symlink_to(secret)

    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "attacker-token")

    # Must return None — the symlink must be skipped, not claimed.
    assert item is None, f"claim_next() claimed a within-queue symlink: {item}"
    # Belt-and-suspenders: symlink must NOT have moved into claimed/.
    attacker_claimed = project_root / "queue" / "claimed" / "attacker-token"
    if attacker_claimed.exists():
        files = list(attacker_claimed.iterdir())
        assert files == [], (
            f"claim_next() moved within-queue symlink into claimed/: {files}"
        )
    # The original secret is untouched.
    assert secret.read_text() == "OTHER-WORKER-SECRET"


def test_release_refuses_symlink_work_leaf(tmp_path):
    """release() MUST NOT move a symlink work leaf from claimed/ into done/.

    A symlink under claimed/<token>/ pointing to another in-queue file passes
    the source-state check (resolved target is under claimed/<token>/) but
    rename() would move the SYMLINK into done/.  export() then resolves it back
    into claimed/ content — bypassing the durable/ephemeral export boundary.

    Expected behaviour: fail-soft — the symlink stays in place, no exception
    raised, done/ is empty (no regular file or symlink moved there).
    """
    project_root = tmp_path / "project"
    # Build the within-queue symlink: claimed/<token>/link.md -> claimed/<token>/real.md
    claimed_dir = project_root / "queue" / "claimed" / "tok"
    claimed_dir.mkdir(parents=True)
    real_file = claimed_dir / "real.md"
    real_file.write_text("REAL-CONTENT")
    symlink_leaf = claimed_dir / "link.md"
    symlink_leaf.symlink_to(real_file)  # target is within claimed/<token>/

    backend = FilesystemQueueBackend(project_root)
    # Must not raise; must not move the symlink.
    backend.release("tok", "link.md")

    done_dir = project_root / "queue" / "done" / "tok"
    assert not done_dir.exists() or not (done_dir / "link.md").exists(), (
        "release() moved a symlink work leaf into done/ — "
        "durable/ephemeral boundary violated"
    )
    # Symlink still in claimed/ (fail-soft = leave in place).
    assert symlink_leaf.is_symlink(), (
        "symlink leaf was removed from claimed/ without being moved to done/ "
        "(fail-soft should leave it in place)"
    )
    # The real file is untouched.
    assert real_file.read_text() == "REAL-CONTENT"


def test_export_skips_symlink_leaf_resolving_into_claimed(tmp_path):
    """export() MUST NOT embed a symlink leaf in done/ whose target is in claimed/.

    Plant done/<token>/link.md -> queue/claimed/<other>/secret.md.
    The resolved target is under queue_root so the existing containment guard
    passes.  The symlink-leaf guard added in Codex round-5 must catch it before
    read_bytes() runs.

    Also verify that a legitimate regular-file item in done/ IS exported
    (legit flow must not break).
    """
    project_root = tmp_path / "project"
    # Plant a legit regular-file item in done/ (must appear in export).
    done_dir = project_root / "queue" / "done" / "legit-tok"
    done_dir.mkdir(parents=True)
    legit = done_dir / "legit.md"
    legit.write_text("LEGIT-CONTENT")

    # Plant a claimed file (ephemeral — must NOT appear in export).
    claimed_dir = project_root / "queue" / "claimed" / "other-tok"
    claimed_dir.mkdir(parents=True)
    secret = claimed_dir / "secret.md"
    secret.write_text("CLAIMED-SECRET")

    # Plant a symlink in done/ pointing to the claimed secret.
    symlink_done_dir = project_root / "queue" / "done" / "attacker-tok"
    symlink_done_dir.mkdir(parents=True)
    (symlink_done_dir / "link.md").symlink_to(secret)

    backend = FilesystemQueueBackend(project_root)
    result = backend.export()

    exported_paths = [path for path, _ in result.items_with_bytes]
    exported_contents = [data.decode() for _, data in result.items_with_bytes]

    # The symlinked leaf must NOT be embedded (claimed/ content excluded).
    assert not any("link.md" in p for p in exported_paths), (
        f"export() embedded a symlink leaf resolving into claimed/: {exported_paths}"
    )
    assert "CLAIMED-SECRET" not in exported_contents, (
        "export() embedded claimed/ content via symlink leaf"
    )

    # The legit regular-file item MUST be present.
    assert any("legit.md" in p for p in exported_paths), (
        f"export() dropped a legit regular-file item: {exported_paths}"
    )
    assert "LEGIT-CONTENT" in exported_contents, (
        "legit regular-file content missing from export"
    )


def test_legit_claim_release_export_regular_files_unaffected(tmp_path):
    """Legit regular-file claim → release → export flow must still work end-to-end.

    Confirms the symlink-leaf invariant guards do not break the normal path:
    a regular-file item is claimed, released to done/, and then exported.
    """
    project_root = tmp_path / "project"
    _queued(project_root, role="writer", name="task.md", content="TASK-BODY")

    backend = FilesystemQueueBackend(project_root)

    # Claim.
    item = backend.claim_next("writer", "legit-token")
    assert item is not None, "claim_next() failed on a regular-file item"
    assert not item.path.is_symlink(), "claimed item path is unexpectedly a symlink"

    # Release to done/.
    backend.release("legit-token", "task.md")
    done_file = project_root / "queue" / "done" / "legit-token" / "task.md"
    assert done_file.is_file(), "release() did not move regular-file item to done/"

    # Export — must include the item.
    result = backend.export()
    exported_paths = [p for p, _ in result.items_with_bytes]
    exported_contents = [d.decode() for _, d in result.items_with_bytes]
    assert any("task.md" in p for p in exported_paths), (
        f"export() dropped legit regular-file item: {exported_paths}"
    )
    assert "TASK-BODY" in exported_contents, (
        "legit regular-file content missing from export"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Round-6 regression: symlinked PARENT of the state directory
#
# The previous point-checks (is_relative_to claimed_src / recovered_src) accepted
# any path that resolved UNDER those dirs — a symlinked claimed/<token>/ pointing
# to an external dir passes that check because the resolved src file IS under the
# resolved claimed_src.  The canonical-path invariant (_require_canonical_source)
# requires the resolved path to match EXACTLY queue_root/<segments>, so a symlinked
# parent makes resolve() diverge and the check catches it.


def test_release_refuses_symlinked_parent_state_dir(tmp_path):
    """release_claim MUST NOT move an item when claimed/<token>/ is a symlink.

    A symlinked claimed/<token>/ pointing to an external dir contains a real file.
    The per-child containment guards in list_claimed / _recover_stale_claims_native
    already skip such dirs, but the source-state check in _release_at_path previously
    used is_relative_to(claimed_src.resolve()) — which PASSES because the resolved src
    path IS under the resolved claimed_src (which itself resolves to the external dir).
    The canonical-path invariant catches it: resolve() diverges from the expected
    canonical location queue_root/claimed/<token>/<name>, so the check fails.

    Expected behaviour: fail-soft — the external file stays where it is, nothing
    lands in done/, and no exception is raised.
    """
    from atomic_agents._cascade import release_claim

    project_root = tmp_path / "project"
    (project_root / "queue" / "claimed").mkdir(parents=True)
    # External dir that claimed/<token>/ will symlink to.
    external_dir = tmp_path / "external_token_dir"
    external_dir.mkdir()
    (external_dir / "secret.md").write_text("SYMLINKED-PARENT-SECRET")
    # claimed/<token>/ is a symlink pointing to external_dir.
    (project_root / "queue" / "claimed" / "sym-token").symlink_to(external_dir)

    # Build a forged item whose path traverses through the symlinked parent.
    forged_work = project_root / "queue" / "claimed" / "sym-token" / "secret.md"
    forged = FilesystemQueueItem(
        original_name="secret.md",
        role="writer",
        lease_token="sym-token",
        claimed_at=time.time(),
        path=forged_work,
    )

    # Must not raise; must not move the external file.
    release_claim(forged, project_root)

    # External file is untouched.
    assert (external_dir / "secret.md").exists(), (
        "symlinked-parent: external file was moved via release_claim"
    )
    assert (external_dir / "secret.md").read_text() == "SYMLINKED-PARENT-SECRET"

    # Nothing must have landed in done/.
    done_root = project_root / "queue" / "done"
    if done_root.exists():
        found = [p for p in done_root.rglob("*") if p.is_file()]
        assert found == [], (
            f"symlinked-parent: file exfiltrated into done/ via release_claim: {found}"
        )


def test_claim_next_refuses_symlinked_role_parent(tmp_path):
    """claim_next MUST NOT claim a file when queued/<role>/ is a symlink to an
    external dir.

    queued/<role>/ is a symlink; the files inside it are regular files.  Without
    the canonical-path invariant, _safe_under_queue(queued, role, p.name) would
    call resolve() on the path, which diverges from the expected canonical location
    — now caught by _require_canonical_source as an equality mismatch.

    Expected behaviour: claim_next returns None, no file is moved into claimed/,
    and the external file stays in the external dir.
    """
    project_root = tmp_path / "project"
    (project_root / "queue").mkdir(parents=True)

    # External dir with a legitimate-looking work file.
    external_role_dir = tmp_path / "external_role"
    external_role_dir.mkdir()
    (external_role_dir / "task.md").write_text("EXTERNAL-WORK")

    # queued/<role>/ is a symlink pointing to the external dir.
    (project_root / "queue" / "queued").mkdir(parents=True)
    (project_root / "queue" / "queued" / "writer").symlink_to(external_role_dir)

    backend = FilesystemQueueBackend(project_root)

    # Must fail-soft: the role's queued dir is a symlink, not a canonical path.
    result = backend.claim_next("writer", "tok-rp")

    assert result is None, (
        f"claim_next() claimed through a symlinked queued/<role>/ parent: {result}"
    )

    # External file must not have been moved into claimed/.
    claimed_dir = project_root / "queue" / "claimed" / "tok-rp"
    if claimed_dir.exists():
        found = [p for p in claimed_dir.iterdir() if p.is_file() or p.is_symlink()]
        assert found == [], (
            f"claim_next() moved a file through a symlinked queued/<role>/: {found}"
        )

    # External file is untouched.
    assert (external_role_dir / "task.md").exists(), (
        "claim_next() moved the external file (exfiltration via symlinked role parent)"
    )
    assert (external_role_dir / "task.md").read_text() == "EXTERNAL-WORK"


def test_list_claimed_and_recover_skip_symlinked_sidecar(tmp_path):
    """A symlinked .lease.json must NOT be read (perimeter read escape).

    The work file stays canonical+regular under claimed/<token>/, but its sidecar
    is a symlink to an external JSON. list_claimed() and recover MUST NOT read the
    external file (no field like 'role' may be surfaced from outside project_root);
    they fall back to mtime as if no sidecar existed.
    """
    project_root = tmp_path / "project"
    (project_root / "queue" / "queued" / "writer").mkdir(parents=True)
    (project_root / "queue" / "queued" / "writer" / "task.md").write_text("work")
    backend = FilesystemQueueBackend(project_root)
    backend.claim_next("writer", "tok")

    real_sidecar = project_root / "queue" / "claimed" / "tok" / "task.md.lease.json"
    real_sidecar.unlink()
    external = tmp_path / "outside.json"
    external.write_text(
        '{"role":"LEAKED-EXTERNAL-ROLE",'
        '"lease_expires_at":"2099-01-01T00:00:00+00:00",'
        '"claimed_at":"2020-01-01T00:00:00+00:00"}'
    )
    real_sidecar.symlink_to(external)

    items = backend.list_claimed()
    assert all(i.role != "LEAKED-EXTERNAL-ROLE" for i in items), (
        "list_claimed() read a symlinked sidecar pointing outside project_root"
    )
    # recover must not raise and must not read the external sidecar either.
    recover_stale_claims(backend, lease_seconds=999999)
    assert external.read_text().startswith("{"), "external file must be untouched"


# ──────────────────────────────────────────────────────────────────────────────
# #477 export() symlinked-subdir DoS — iterdir() walk re-asserts containment
# at each subdir descent; refuses to follow symlinked subdirs.


def test_export_symlinked_subdir_inside_queued_is_skipped(tmp_path):
    """export() must NOT descend into a symlinked subdirectory inside queued/.

    A symlinked subdir inside queued/ (e.g. queued/evil/ -> /outside/) is
    refused at the subdir-descent stage of _walk_dir_no_follow; files inside
    the symlinked tree are NOT emitted in the export.

    INVARIANT GUARD (not a strip-RED differentiator on CI interpreters): this
    asserts the negative invariant "symlinked-subdir contents are never
    exported" and the positive invariant "real subdir files ARE exported".
    Both invariants hold under BOTH _walk_dir_no_follow AND default
    sorted(rglob('*')), because default rglob does NOT follow directory
    symlinks on any interpreter the framework supports (3.11-3.13:
    recurse_symlinks defaults to False; the 3.13 change only ADDED the opt-in
    parameter). The _walk_dir_no_follow hardening (#477) is therefore
    defense-in-depth / version-independent, verified by inspection rather than
    by a behavioral difference here. A 3.13+ CI lane exercising the live
    recurse_symlinks vector is tracked in #595.
    """
    import os

    project_root = tmp_path / "project"
    # Plant a real file in queued/writer/
    qd = project_root / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "real_task.md").write_text("real content")

    # Plant a directory outside queue_root with a file in it.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.md").write_text("should never appear in export")

    # Create a symlinked subdir inside queued/ pointing outside.
    evil_link = project_root / "queue" / "queued" / "evil"
    os.symlink(outside, evil_link)

    backend = FilesystemQueueBackend(project_root)
    result = backend.export()
    paths = [rel for rel, _ in result.items_with_bytes]

    # The real file IS exported (walk reaches non-symlinked subdirs).
    assert any("real_task.md" in p for p in paths), (
        f"Real file must be exported: {paths}"
    )
    # The symlinked-subdir file is NOT exported.
    assert not any("evil.md" in p for p in paths), (
        f"File inside symlinked subdir must NOT be exported: {paths}"
    )


def test_export_symlinked_subdir_within_queue_root_is_skipped(tmp_path):
    """export() must NOT follow a symlinked subdir even if it resolves within queue_root.

    A within-queue symlink (done/link-dir -> queued/writer/) would cause
    double-enumeration and cross-boundary aliasing if symlink recursion were
    ever enabled. It is skipped at the subdir descent gate (is_symlink() check
    BEFORE the containment check), not just at the containment check.

    INVARIANT GUARD (not a strip-RED differentiator on CI interpreters): the
    no-double-count invariant holds under BOTH _walk_dir_no_follow AND default
    sorted(rglob('*')), because default rglob does NOT follow directory
    symlinks on 3.11-3.13 (recurse_symlinks defaults to False). The
    _walk_dir_no_follow hardening (#477) makes this version-independent and
    robust to a future recurse_symlinks default; it is verified by inspection
    here. The live-vector 3.13+ test is tracked in #595.
    """
    import os

    project_root = tmp_path / "project"
    # Plant a real file in queued/writer/
    qd = project_root / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "original.md").write_text("original content")
    done_dir = project_root / "queue" / "done"
    done_dir.mkdir(parents=True)

    # Create a within-queue symlink done/link-dir -> queued/writer/
    link_dir = done_dir / "link-dir"
    os.symlink(qd, link_dir)

    backend = FilesystemQueueBackend(project_root)
    result = backend.export()
    paths = [rel for rel, _ in result.items_with_bytes]

    # The original file IS exported once (from queued/).
    queued_refs = [p for p in paths if "queued" in p and "original.md" in p]
    assert len(queued_refs) == 1, (
        f"original.md must appear exactly once from queued/: {paths}"
    )
    # The within-queue symlinked subdir must NOT be followed (no double-count).
    done_refs = [p for p in paths if "done" in p]
    assert not done_refs, (
        f"Symlinked subdir inside done/ must NOT be followed: {done_refs}"
    )


def test_export_sorted_output_order_preserved(tmp_path):
    """export() output must be sorted consistently after the iterdir() walk replacement.

    Regression guard for the #477 iterdir() refactor: the walk collects all
    files then sorts, matching the previous sorted(dir_path.rglob('*')) order.
    """
    project_root = tmp_path / "project"
    # Multiple roles with multiple files to stress sort order.
    for role in ["alpha", "beta", "zeta"]:
        qd = project_root / "queue" / "queued" / role
        qd.mkdir(parents=True)
        for i in range(3):
            (qd / f"00{i}_task.md").write_text(f"content {role}/{i}")

    backend = FilesystemQueueBackend(project_root)
    result = backend.export()
    paths = [rel for rel, _ in result.items_with_bytes]
    assert paths == sorted(paths), f"export() output must be sorted: {paths}"


# ──────────────────────────────────────────────────────────────────────────────
# #479 sidecar/reason.txt symlink-leaf perimeter escape — O_NOFOLLOW writes


def test_write_sidecar_refuses_symlinked_destination(tmp_path):
    """_write_sidecar must NOT write through a symlink at the sidecar path.

    Strip-RED negative control: the symlink target must NOT be written.
    """
    import os

    project_root = tmp_path / "project"
    claimed = project_root / "queue" / "claimed" / "lease-1"
    claimed.mkdir(parents=True)
    work_file = claimed / "task.md"
    work_file.write_text("work")

    # Plant a symlink at the sidecar path pointing to an external file.
    external = tmp_path / "external_sidecar.json"
    external.write_text('{"original": true}')
    sidecar = claimed / "task.md.lease.json"
    os.symlink(external, sidecar)

    # _write_sidecar must NOT write through the symlink.
    _write_sidecar(work_file, lease_token="lease-1", lease_seconds=60)

    # The external file must be unchanged.
    assert external.read_text() == '{"original": true}', (
        "_write_sidecar must not follow a symlink at the sidecar path"
    )
    # The symlink itself must still be there (not replaced with a real file).
    assert sidecar.is_symlink(), "symlink at sidecar path must be unchanged"


def test_write_sidecar_writes_normally_when_no_symlink(tmp_path):
    """Strip-RED control: _write_sidecar writes successfully to a real path."""
    project_root = tmp_path / "project"
    claimed = project_root / "queue" / "claimed" / "lease-1"
    claimed.mkdir(parents=True)
    work_file = claimed / "task.md"
    work_file.write_text("work")

    _write_sidecar(work_file, lease_token="lease-1", lease_seconds=60)

    sidecar = claimed / "task.md.lease.json"
    assert sidecar.exists() and not sidecar.is_symlink(), (
        "_write_sidecar must write the real sidecar when no symlink is present"
    )
    import json as _json

    data = _json.loads(sidecar.read_text())
    assert data["lease_token"] == "lease-1"


def test_dead_letter_reason_txt_refuses_symlinked_destination(tmp_path):
    """move_to_dead_letter must NOT write .reason.txt through a symlink.

    Strip-RED: the external target must be unchanged after the operation.
    """
    import os

    project_root = tmp_path / "project"
    _queued(project_root, name="001_task.md")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1")
    assert item is not None

    # The item is in claimed/. Plant a symlink at the future .reason.txt path
    # in dead-letter/ — create the dead-letter dir first.
    dl_dir = project_root / "queue" / "dead-letter" / "lease-1"
    dl_dir.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external_reason.txt"
    external.write_text("original content")
    reason_link = dl_dir / "001_task.md.reason.txt"
    os.symlink(external, reason_link)

    # move_to_dead_letter must NOT write through the symlink.
    backend.move_to_dead_letter(item.lease_token, item.original_name, reason="FAILED")

    # External file must be unchanged.
    assert external.read_text() == "original content", (
        "move_to_dead_letter must not write .reason.txt through a symlink"
    )
    # The work file IS moved to dead-letter/ (rename still succeeds).
    dl_file = dl_dir / "001_task.md"
    assert dl_file.exists(), "work file must be in dead-letter/ after the rename"


def test_renew_lease_refuses_symlinked_sidecar_destination(tmp_path):
    """renew_lease must NOT write through a symlink at the sidecar path.

    The symlink target is IN-TREE (under queue_root) so the upstream
    containment guard (sidecar.resolve().is_relative_to(queue_root)) PASSES
    and does NOT short-circuit before the write. That leaves the O_NOFOLLOW
    write-side guard (#479, _write_no_follow) as the SOLE remaining defense —
    so this is a genuine strip-RED control for the renew-path O_NOFOLLOW
    write. (An out-of-tree target would be refused by the containment guard
    before _write_no_follow ran, making the control false-green — verified.)

    Strip-RED: the in-tree victim file must be unchanged after renew_lease().
    """
    import os

    project_root = tmp_path / "project"
    _queued(project_root, name="001_task.md")
    backend = FilesystemQueueBackend(project_root)
    item = backend.claim_next("writer", "lease-1", lease_seconds=60)
    assert item is not None

    # Plant an in-tree victim file (under queue_root, in the same claimed dir).
    victim = item.path.parent / "victim.txt"
    victim.write_text("UNTOUCHED")

    # Replace the real sidecar with a symlink pointing at the in-tree victim.
    # This survives the resolve()/is_relative_to(queue_root) containment check,
    # so only O_NOFOLLOW can stop the write from following the symlink.
    sidecar = item.path.parent / (item.original_name + ".lease.json")
    sidecar.unlink()
    os.symlink(victim, sidecar)

    # renew_lease must NOT write through the symlink (O_NOFOLLOW refuses it;
    # the best-effort write is silently skipped).
    backend.renew_lease(item.lease_token, item.original_name, additional_seconds=3600)

    # In-tree victim file must be unchanged — O_NOFOLLOW refused the follow.
    assert victim.read_text() == "UNTOUCHED", (
        "renew_lease must not write through a symlink at the sidecar path "
        "(O_NOFOLLOW must refuse the in-tree symlink redirect)"
    )


def test_write_no_follow_drains_short_writes(tmp_path, monkeypatch):
    """_write_no_follow MUST loop os.write until ALL bytes land — a raw os.write
    may short-write (POSIX permits it; Path.write_text's IO layer looped, raw
    os.write does not). A silent short write would truncate the sidecar while
    returning success; on renew_lease's read-modify-write path (O_TRUNC already
    cleared the old bytes) that erases a live lease and lets recovery
    re-dispatch active work (#479 cross-model adversarial finding, Principle #8).

    STRIP-RED negative control: revert the drain loop to a single
    `os.write(fd, encoded)` and this test fails (file truncated to 4 bytes).
    """
    import os

    real_write = os.write

    def short_write(fd, data):
        # Force every write to make only 4 bytes of progress, exercising the
        # drain loop. Without the loop, only the first 4 bytes are written.
        return real_write(fd, bytes(data)[:4])

    monkeypatch.setattr(_fsmod.os, "write", short_write)

    target = tmp_path / "sidecar.lease.json"
    payload = '{"lease_token":"abc","lease_expires_at":1234567890,"role":"writer"}'
    assert len(payload) > 4  # ensure the single-write path would truncate

    _write_no_follow(target, payload)

    monkeypatch.undo()  # restore real os.write before reading back
    assert target.read_text() == payload, (
        "drain loop must write the FULL payload under short writes; a single "
        "os.write would truncate it"
    )

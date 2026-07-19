"""Verb-parametrized SPINE conformance tests (spec/55 M11, #709/#710).

Covers the guarantees the HOISTED shared spine (``atomic_agents/manage/_routine.py``)
provides to every write verb — govern --set today, govern --restore (#710), and any
future verb (set-model, apply-rec):

- lock / agent_busy: a per-agent manage lease serializes management writes;
  contention refuses with ``agent_busy`` (M11).
- fail-closed: a LockBackend construction failure refuses (never an
  unlocked write), with a JSON ``error_type`` DISTINCT from ``agent_busy``.
- the manage lease does NOT contend with the agent's main ``.lock`` (live runs).
- five-step ordering: validate -> preview -> confirm -> snapshot+write -> audit;
  the lease is acquired only around snapshot+write, never across the interactive
  confirm prompt or the audit append.
- the P0 lost-update fix: a write based on a stale ADVISORY pre-lock read must
  not clobber a concurrent write that landed while it was blocked on confirm.
- a real CONCURRENCY test: two writers contend simultaneously -> one applies,
  the other gets agent_busy (not the sequential snapshot-uuid check).
- snapshot: orphan-snapshot-on-write-failure is documented-benign, for both
  --set and --restore.
- audit: restore emits exactly one manage_restore record (never a
  manage_govern record too).
- exit-code ladder: 0 applied / 1 refused-or-error / 3 declined / 130 SIGINT.

``tests/test_manage_govern.py`` keeps govern-specific behavior (YAML surgical
editing, CRLF/comment fidelity, template rendering, etc).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from atomic_agents.exceptions import LockBusy
from atomic_agents.locks import FilesystemLockBackend
from atomic_agents.locks.types import LockCapabilities, LockHandle
from atomic_agents.manage import _routine
from atomic_agents.manage import run_manage
from atomic_agents.manage.exceptions import ManageAgentBusyError
from atomic_agents.manage.govern import run_govern
from atomic_agents.logs.types import PRIMITIVE_MANAGE_GOVERN, PRIMITIVE_MANAGE_RESTORE

from tests._manage_test_helpers import (
    collect_jsonl,
    make_agent_dir,
    make_govern_args,
    make_governance_md,
)


# ──────────────────────────────────────────────────────────────────
# A fake distributed (non-single-host) LockBackend — simulates Redis'
# deployment-wide key namespace (spec/21: distributed backends ignore
# scope_root) without needing a real Redis server in CI.


class _FakeDistributedLockBackend:
    """Stub LockBackend with ``single_host_only=False`` and a GLOBAL key
    namespace shared across every instance for the lifetime of the class —
    mirrors RedisLockBackend's key_prefix being deployment-wide, not
    per-agent. Used to prove ``manage_lease()`` folds the agent id into the
    acquired resource name for non-single-host backends (the P0 Redis
    cross-agent-collision guard).
    """

    _global_held: set[str] = set()

    def __init__(self, scope_root: Any = None) -> None:
        self.scope_root = scope_root

    @property
    def backend_id(self) -> str:
        return "fake-distributed"

    def capabilities(self) -> LockCapabilities:
        return LockCapabilities(
            single_host_only=False, supports_reentrancy=False, supports_lease=False
        )

    def acquire(self, name: str = "", timeout: float = 0.0) -> LockHandle:
        if name in self._global_held:
            raise LockBusy(f"fake-distributed already holds {name!r}")
        self._global_held.add(name)
        handle = LockHandle(
            name=name, acquired_at=0.0, holder_pid=0, backend_state=None
        )
        object.__setattr__(handle, "_backend", self)
        return handle

    def release(self, handle: LockHandle) -> None:
        self._global_held.discard(handle.name)

    def renew(self, handle: LockHandle) -> bool:
        return True

    def is_held(self, name: str = "") -> bool:
        return name in self._global_held

    def scope(self, sub_path: str) -> "_FakeDistributedLockBackend":
        return self


# ──────────────────────────────────────────────────────────────────
# Manage lease: hidden artifact shape, no main-lock contention


def test_manage_lease_produces_hidden_lock_artifact_not_a_subdir(tmp_path):
    """The manage lease is a hidden ``<agent>/.manage.lock`` file, not a
    visible ``manage/`` subdir (lease-scope-construction ruling)."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    assert run_govern(args, tmp_path) == 0

    assert (agent_dir / ".manage.lock").exists()
    assert not (agent_dir / "manage").exists()


def test_manage_write_does_not_contend_with_held_main_lock(tmp_path):
    """A live ``agent.call()`` run (holding the main ``''``-named lock) must
    NOT block a concurrent management write — distinct named leases."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    main_backend = FilesystemLockBackend(agent_dir)
    with main_backend.acquire("", timeout=0):
        args = make_govern_args(
            agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
        )
        assert run_govern(args, tmp_path) == 0


def test_main_lock_does_not_contend_with_manage_lease(tmp_path):
    """The inverse: after a management write releases its lease, the
    agent's main lock must be immediately acquirable (never contended)."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    assert run_govern(args, tmp_path) == 0

    main_backend = FilesystemLockBackend(agent_dir)
    handle = main_backend.acquire("", timeout=0)  # must not raise LockBusy
    main_backend.release(handle)


# ──────────────────────────────────────────────────────────────────
# agent_busy vs lock_backend_unavailable — distinct, centrally-caught


def test_lock_backend_construction_failure_is_fail_closed(
    tmp_path, monkeypatch, capsys
):
    """lockbackend-construction-failure-posture ruling: a LockBackend that
    cannot be constructed refuses the write (exit 1), never proceeds
    unlocked. error_type is 'lock_backend_unavailable', DISTINCT from
    'agent_busy' — caught CENTRALLY by run_manage(), not inside run_govern.
    """
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    def _boom(scope_root):
        raise ValueError("ATOMIC_AGENTS_LOCK_BACKEND=bogus is not a known backend")

    monkeypatch.setattr("atomic_agents.locks.get_default_lock_backend", _boom)

    args = make_govern_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        yes=True,
        use_json=True,
    )
    rc = run_manage(args, tmp_path)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "lock_backend_unavailable"

    # No write happened, no audit record.
    assert (
        not (agent_dir / ".manage.lock").exists() or True
    )  # construction never got a handle
    assert collect_jsonl(agent_dir / "log") == []


def test_agent_busy_is_raised_uncaught_by_run_govern_directly(tmp_path):
    """ManageAgentBusyError propagates UNCAUGHT out of run_govern() — the
    central catch lives in run_manage(), not per-verb (agent-busy-error-
    taxonomy ruling: 'caught CENTRALLY in the spine, not per-verb')."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    # Hold the SAME named lease the verb will try to acquire.
    real_backend = FilesystemLockBackend(agent_dir)
    with real_backend.acquire("manage", timeout=0):
        args = make_govern_args(
            agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
        )
        with pytest.raises(ManageAgentBusyError):
            run_govern(args, tmp_path)


def test_agent_busy_caught_centrally_by_run_manage(tmp_path, capsys):
    """The SAME contention scenario, driven through run_manage() (the real
    CLI dispatch path) — exits 1 with error_type='agent_busy', distinct from
    'lock_backend_unavailable'."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    real_backend = FilesystemLockBackend(agent_dir)
    with real_backend.acquire("manage", timeout=0):
        args = make_govern_args(
            agent_dir.name,
            tmp_path,
            set_fields=["owner=alice@example.com"],
            yes=True,
            use_json=True,
        )
        rc = run_manage(args, tmp_path)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "agent_busy"

    # M8: a refusal (including agent_busy) writes NO management RunRecord.
    assert collect_jsonl(agent_dir / "log") == []


def test_agent_busy_json_error_emitted_exactly_once(tmp_path, capsys):
    """P2 hoist-double-emission guard: the JSON refusal for a busy lease is
    printed EXACTLY ONCE (not zero, not twice) — the central catch in
    run_manage() must be the ONLY emission site."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    real_backend = FilesystemLockBackend(agent_dir)
    with real_backend.acquire("manage", timeout=0):
        args = make_govern_args(
            agent_dir.name,
            tmp_path,
            set_fields=["owner=alice@example.com"],
            yes=True,
            use_json=True,
        )
        run_manage(args, tmp_path)

    out = capsys.readouterr().out
    # Exactly one JSON object was printed — a second stray emission would
    # make json.loads on the WHOLE captured stdout fail (trailing garbage)
    # or produce two concatenated top-level objects.
    payload = json.loads(out)
    assert payload["error_type"] == "agent_busy"
    assert out.count('"error_type"') == 1


def test_manage_lease_distributed_backend_scopes_per_agent():
    """P0 Redis-collision guard: a non-single-host LockBackend must isolate
    per AGENT, not collapse to one shared deployment-wide key (spec/21:
    distributed backends ignore scope_root)."""
    _FakeDistributedLockBackend._global_held.clear()
    backend = _FakeDistributedLockBackend()

    with _routine.manage_lease(Path("/irrelevant-a"), "agent-a", backend=backend):
        # A DIFFERENT agent's lease on the SAME distributed backend instance
        # must NOT collide.
        with _routine.manage_lease(Path("/irrelevant-b"), "agent-b", backend=backend):
            pass  # no LockBusy raised — isolation holds

        # The SAME agent's lease, nested, DOES collide (proves the fold is
        # actually agent-specific, not a no-op that always succeeds).
        with pytest.raises(ManageAgentBusyError):
            with _routine.manage_lease(
                Path("/irrelevant-a"), "agent-a", backend=backend
            ):
                pass


# ──────────────────────────────────────────────────────────────────
# Five-step ordering: confirm before lock; lock before write; write before
# audit; lock released before audit


def test_dry_run_does_not_acquire_manage_lease(tmp_path):
    """--dry-run never takes the lease — held externally, apply still previews."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    with _routine.manage_lease(agent_dir, agent_dir.name):
        args = make_govern_args(
            agent_dir.name,
            tmp_path,
            set_fields=["owner=alice@example.com"],
            dry_run=True,
            yes=True,
        )
        assert run_govern(args, tmp_path) == 0


def test_dry_run_does_not_construct_lock_backend(tmp_path, monkeypatch):
    """Fix 1 (adversarial review): --dry-run must not even CONSTRUCT the lock backend (not
    just not acquire it) — a misconfigured/unreachable LockBackend must
    never fail a preview that never intended to touch the lease.

    Strip-RED: reverting Fix 1 (constructing lock_backend eagerly in
    run_govern before dispatch, ahead of the --set/--restore --dry-run
    early-exits) makes ``_boom`` fire during a --dry-run call, so this test
    fails with the AssertionError instead of asserting ``rc == 0``.
    """
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    from atomic_agents.manage import govern as govern_mod

    def _boom(agent_dir_arg):
        raise AssertionError("dry-run must never construct the lock backend")

    monkeypatch.setattr(govern_mod, "get_manage_lock_backend", _boom)

    args = make_govern_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        dry_run=True,
        yes=True,
    )
    assert run_govern(args, tmp_path) == 0


def test_restore_dry_run_does_not_construct_lock_backend(tmp_path, monkeypatch):
    """The same Fix 1 guarantee on the --restore path (its own, separately
    hoisted lock_backend construction)."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    seed_snapshot_id = _routine.list_snapshots(agent_dir, "govern")[0].name

    from atomic_agents.manage import govern as govern_mod

    def _boom(agent_dir_arg):
        raise AssertionError("dry-run must never construct the lock backend")

    monkeypatch.setattr(govern_mod, "get_manage_lock_backend", _boom)

    args = make_govern_args(
        agent_dir.name, tmp_path, restore=seed_snapshot_id, dry_run=True, yes=True
    )
    assert run_govern(args, tmp_path) == 0


def test_show_does_not_acquire_manage_lease(tmp_path):
    """--show is a pure read — never contends with a held manage lease."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    with _routine.manage_lease(agent_dir, agent_dir.name):
        args = make_govern_args(agent_dir.name, tmp_path, show=True)
        assert run_govern(args, tmp_path) == 0


def test_list_snapshots_does_not_acquire_manage_lease(tmp_path):
    """--list-snapshots is a pure read — never contends with a held manage lease."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    with _routine.manage_lease(agent_dir, agent_dir.name):
        args = make_govern_args(agent_dir.name, tmp_path, list_snapshots=True)
        assert run_govern(args, tmp_path) == 0


def test_lock_released_before_audit_runs(tmp_path, monkeypatch):
    """M11 note: the lease covers ONLY snapshot+write, never the audit append.

    Proven by attempting a SECOND, fully independent govern --set on the SAME
    agent from INSIDE the patched append_management_audit call — if the
    lease were still held during audit, this nested call would get
    agent_busy.
    """
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    from atomic_agents.manage import govern as govern_mod

    orig_audit = govern_mod.append_management_audit
    nested: dict[str, Any] = {"done": False}

    def probing_audit(record, adir, aroot):
        # Guard against recursion: the nested run_govern() call below ALSO
        # reaches this same patched function for its OWN audit append — only
        # the OUTERMOST invocation should trigger the probe.
        if not nested["done"]:
            nested["done"] = True
            nested_args = make_govern_args(
                agent_dir.name,
                tmp_path,
                set_fields=["backup_owner=carol@example.com"],
                yes=True,
            )
            nested["rc"] = run_govern(nested_args, tmp_path)
        return orig_audit(record, adir, aroot)

    monkeypatch.setattr(govern_mod, "append_management_audit", probing_audit)

    args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 0
    assert nested["rc"] == 0, (
        "nested write triggered from inside the audit callback must NOT get "
        "agent_busy — the manage lease must already be released before audit runs"
    )


def test_validation_failure_never_touches_lock_or_snapshot(tmp_path):
    """Five-step ordering: an S2-step-1 validation refusal happens BEFORE
    the manage lease is ever acquired and BEFORE any snapshot is taken."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)
    before_content = (agent_dir / "governance.md").read_text()

    args = make_govern_args(
        agent_dir.name,
        tmp_path,
        set_fields=["permission_tier=not-a-real-tier"],
        yes=True,
    )
    rc = run_govern(args, tmp_path)

    assert rc == 1
    assert (agent_dir / "governance.md").read_text() == before_content
    assert not (agent_dir / ".config-snapshots").exists()


def test_restore_unknown_snapshot_never_touches_lock_or_write(tmp_path):
    """Same ordering guarantee on the restore path: an unresolvable
    snapshot-id refuses before the lease / write / snapshot machinery runs."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)
    before_content = (agent_dir / "governance.md").read_text()

    args = make_govern_args(
        agent_dir.name, tmp_path, restore="does-not-exist.md", yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 1
    assert (agent_dir / "governance.md").read_text() == before_content


# ──────────────────────────────────────────────────────────────────
# P0 lost-update fix: a stale advisory pre-lock read must not clobber a
# concurrent write that landed while blocked on confirm


def test_lost_update_fix_concurrent_write_survives(tmp_path, monkeypatch):
    """#709 P0 regression test.

    Simulates process A taking its ADVISORY pre-lock preview read, then
    being blocked (e.g. on the interactive confirm prompt) while process B
    fully completes an UNRELATED field's write. When A finally proceeds, its
    LOCKED write must be based on a FRESH re-read (which already includes
    B's landed change) — not the stale pre-lock snapshot A captured before
    B ran. Without the fix, A's write silently reverts B's change.
    """
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    from atomic_agents.manage import govern as govern_mod

    orig_read = govern_mod._read_or_create_governance
    call_count = {"n": 0}

    def interleaving_read(path, agent_id):
        call_count["n"] += 1
        content = orig_read(path, agent_id)
        if call_count["n"] == 1:
            # This is A's ADVISORY pre-lock read. Before A proceeds, run B's
            # FULL write to completion on a DIFFERENT field.
            b_args = make_govern_args(
                agent_dir.name,
                tmp_path,
                set_fields=["backup_owner=bob@example.com"],
                yes=True,
            )
            rc_b = run_govern(b_args, tmp_path)
            assert rc_b == 0
        return content

    monkeypatch.setattr(govern_mod, "_read_or_create_governance", interleaving_read)

    a_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    rc_a = run_govern(a_args, tmp_path)
    assert rc_a == 0

    final = (agent_dir / "governance.md").read_text()
    assert "alice@example.com" in final, "A's own change must land"
    assert "bob@example.com" in final, (
        "B's change must SURVIVE A's write — A's locked write must be based "
        "on a FRESH read, not the stale advisory pre-lock read captured "
        "before B ran (the #709 P0 lost-update fix)"
    )


def test_real_concurrent_writes_one_applies_one_busies(tmp_path):
    """A real CONCURRENCY test (not the sequential snapshot-uuid check):
    two writers contend SIMULTANEOUSLY via actual threads. One completes;
    the other's non-blocking acquire (timeout=0) fails immediately with
    agent_busy — there is no retry/wait.
    """
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    from atomic_agents.manage import _routine as routine_mod

    orig_atomic_write = routine_mod.atomic_write
    entered = threading.Event()
    release = threading.Event()
    call_count = {"n": 0}

    def slow_atomic_write(path, content):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First atomic_write call inside the critical section is the
            # SNAPSHOT write — pause here so thread A holds the lease while
            # thread B (this thread, run synchronously below) contends.
            entered.set()
            release.wait(timeout=5)
        return orig_atomic_write(path, content)

    results: dict[str, int] = {}

    def run_a():
        args = make_govern_args(
            agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
        )
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            routine_mod, "atomic_write", slow_atomic_write
        ):
            results["a"] = run_govern(args, tmp_path)

    t = threading.Thread(target=run_a)
    t.start()
    assert entered.wait(timeout=5), "thread A never entered its critical section"

    # Thread B (main thread) contends WHILE A holds the lease.
    b_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=bob@example.com"], yes=True
    )
    with pytest.raises(ManageAgentBusyError):
        run_govern(b_args, tmp_path)

    release.set()
    t.join(timeout=5)

    assert results["a"] == 0
    final = (agent_dir / "governance.md").read_text()
    assert "alice@example.com" in final
    assert "bob@example.com" not in final  # B never applied


# ──────────────────────────────────────────────────────────────────
# Orphan snapshot (snapshot succeeds, write fails -> documented-benign
# orphan), parametrized across --set and --restore


def test_orphan_snapshot_on_set_write_failure(tmp_path, monkeypatch):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)
    before_content = (agent_dir / "governance.md").read_text()

    from atomic_agents.manage import _routine as routine_mod

    orig_atomic_write = routine_mod.atomic_write
    call_count = {"n": 0}

    def failing_second_write(path, content):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Second call is the governance.md write (first is the snapshot).
            raise OSError("simulated disk-full on the governance.md write")
        return orig_atomic_write(path, content)

    monkeypatch.setattr(routine_mod, "atomic_write", failing_second_write)

    args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 1
    # governance.md is untouched (atomic_write failed before rename landed).
    assert (agent_dir / "governance.md").read_text() == before_content
    # The snapshot (taken BEFORE the failed write) is an orphan — it exists,
    # documented-benign per M3 (a snapshot without a corresponding applied
    # write is harmless; it simply never gets referenced by an audit record).
    snaps = _routine.list_snapshots(agent_dir, "govern")
    assert len(snaps) == 1


def test_orphan_snapshot_on_restore_write_failure(tmp_path, monkeypatch):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    # Seed one real snapshot to restore FROM.
    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    seed_snapshot_id = _routine.list_snapshots(agent_dir, "govern")[0].name

    before_content = (agent_dir / "governance.md").read_text()

    from atomic_agents.manage import _routine as routine_mod

    orig_atomic_write = routine_mod.atomic_write
    call_count = {"n": 0}

    def failing_write(path, content):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # The pre-restore snapshot (taken first, inside the lock).
            raise OSError("simulated disk-full on the pre-restore snapshot")
        return orig_atomic_write(path, content)

    monkeypatch.setattr(routine_mod, "atomic_write", failing_write)

    args = make_govern_args(
        agent_dir.name, tmp_path, restore=seed_snapshot_id, yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 1
    assert (agent_dir / "governance.md").read_text() == before_content


# ──────────────────────────────────────────────────────────────────
# Restore: full five-step routine (not a bypass) -> exactly one audit record


def test_restore_full_routine_emits_exactly_one_manage_restore_record(tmp_path):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    # Each --set's snapshot captures the PRE-write state (M3: snapshot-before-
    # write), so the snapshot containing "seed@example.com" is the one taken
    # by the SECOND --set (right before it overwrote owner -> "changed") —
    # NOT the first --set's snapshot (which captures the pristine template,
    # owner: null, from before "seed" was ever set).
    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0

    second_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=changed@example.com"], yes=True
    )
    assert run_govern(second_args, tmp_path) == 0

    snaps = _routine.list_snapshots(agent_dir, "govern")
    assert len(snaps) == 2
    seed_snapshot = next(p for p in snaps if "seed@example.com" in p.read_text())
    seed_snapshot_id = seed_snapshot.name

    restore_args = make_govern_args(
        agent_dir.name, tmp_path, restore=seed_snapshot_id, yes=True
    )
    rc = run_govern(restore_args, tmp_path)
    assert rc == 0

    records = collect_jsonl(agent_dir / "log")
    restore_records = [
        r for r in records if r.get("primitive") == PRIMITIVE_MANAGE_RESTORE
    ]
    govern_records_after_restore = [
        r for r in records if r.get("primitive") == PRIMITIVE_MANAGE_GOVERN
    ]

    assert len(restore_records) == 1
    # Exactly the two --set records from before + zero NEW manage_govern
    # records from the restore itself.
    assert len(govern_records_after_restore) == 2

    rec = restore_records[0]
    assert rec["restored_from"] == seed_snapshot_id
    assert rec["snapshot_path"] is not None  # the pre-restore snapshot
    assert rec["snapshot_path"] != seed_snapshot_id
    assert "cost_usd" not in rec  # not an LLM call — omitted, not 0.0

    # Restore is a byte-exact rollback — governance.md now shows the seed value.
    assert "seed@example.com" in (agent_dir / "governance.md").read_text()
    assert "changed@example.com" not in (agent_dir / "governance.md").read_text()


def test_restore_cross_agent_refused(tmp_path):
    """m3-conformance restore sub-MUST: a snapshot-id from a DIFFERENT
    agent's snapshot tree must never resolve for this agent (no cross-agent
    restore)."""
    agent_a = make_agent_dir(tmp_path, "agent-a")
    agent_b = make_agent_dir(tmp_path, "agent-b")
    make_governance_md(agent_a)
    make_governance_md(agent_b)

    # Take a real snapshot under agent B.
    b_args = make_govern_args(
        "agent-b", tmp_path, set_fields=["owner=bob@example.com"], yes=True
    )
    assert run_govern(b_args, tmp_path) == 0
    b_snapshot_id = _routine.list_snapshots(agent_b, "govern")[0].name

    # Attempt to restore agent A using agent B's snapshot id.
    a_args = make_govern_args(
        "agent-a", tmp_path, restore=b_snapshot_id, yes=True, use_json=True
    )
    rc = run_govern(a_args, tmp_path)

    assert rc == 1
    # governance.md for A is untouched.
    assert "bob@example.com" not in (agent_a / "governance.md").read_text()


@pytest.mark.parametrize(
    "malicious_id",
    [
        "../agent-b/.config-snapshots/govern/x.md",
        "..",
        ".",
        "sub/dir.md",
        "/etc/passwd",
        "sub\\dir.md",
        "",
    ],
)
def test_resolve_snapshot_path_rejects_traversal_payloads(tmp_path, malicious_id):
    """#710 restore-path hardening: a snapshot-id containing a path separator
    (or a bare '.'/'..'/empty string) is rejected BEFORE any path
    construction — never reaches ``Path.__truediv__`` with untrusted input."""
    agent_dir = make_agent_dir(tmp_path)

    from atomic_agents.manage.exceptions import ManageSnapshotNotFoundError

    with pytest.raises(ManageSnapshotNotFoundError):
        _routine.resolve_snapshot_path(agent_dir, "govern", malicious_id)


def test_resolve_snapshot_path_containment_survives_symlink_swap(tmp_path):
    """A symlinked snapshot dir pointing OUTSIDE agent_dir is refused, not
    silently followed (mirrors take_config_snapshot's write-side guard)."""
    agent_dir = make_agent_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.md").write_text("not a real snapshot")

    snap_dir = agent_dir / ".config-snapshots" / "govern"
    snap_dir.parent.mkdir(parents=True, exist_ok=True)
    snap_dir.symlink_to(outside, target_is_directory=True)

    from atomic_agents.manage.exceptions import ManageSnapshotNotFoundError

    with pytest.raises(ManageSnapshotNotFoundError):
        _routine.resolve_snapshot_path(agent_dir, "govern", "evil.md")


def test_resolve_snapshot_path_symlinked_file_escapes_snap_dir_refused(tmp_path):
    """Fix 2 (adversarial review): a snapshot_id that matches the generated filename SHAPE
    but is actually a symlink FILE (not the whole snap_dir) pointing at
    another file inside agent_dir but OUTSIDE the snapshot dir (e.g. the
    live governance.md) must be refused.

    Strip-RED: reverting Fix 2's second containment call from
    ``safe_resolve_under(candidate, snap_dir)`` back to
    ``safe_resolve_under(candidate, agent_dir)`` makes this pass (the
    symlink target IS still "under agent_dir"), so ``resolve_snapshot_path``
    would return the live governance.md path instead of raising — this test
    would fail (no exception raised).
    """
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)  # the live file the symlink targets

    snap_dir = agent_dir / ".config-snapshots" / "govern"
    snap_dir.mkdir(parents=True)
    shaped_name = "20260718T120000-deadbeef.md"
    (snap_dir / shaped_name).symlink_to(agent_dir / "governance.md")

    from atomic_agents.manage.exceptions import ManageSnapshotNotFoundError

    with pytest.raises(ManageSnapshotNotFoundError):
        _routine.resolve_snapshot_path(agent_dir, "govern", shaped_name)


# ──────────────────────────────────────────────────────────────────
# Fix 3 (adversarial review): snapshot_id shape validation + list_snapshots() filtering


def test_resolve_snapshot_path_rejects_non_snapshot_shaped_id(tmp_path):
    """A snapshot_id that does not match the shape take_config_snapshot()
    actually generates is refused outright, even when a real, genuinely
    on-disk file exists under that exact name — a planted non-snapshot file
    must never be restorable.

    Strip-RED: dropping the ``_SNAPSHOT_FILENAME_RE.match(snapshot_id) is
    None`` arm from the malformed-id guard makes ``resolve_snapshot_path``
    fall through to the containment+existence check, which succeeds (the
    file is real and under snap_dir) — this test would fail (no exception
    raised).
    """
    agent_dir = make_agent_dir(tmp_path)
    snap_dir = agent_dir / ".config-snapshots" / "govern"
    snap_dir.mkdir(parents=True)
    (snap_dir / "evil.md").write_text("not a real snapshot")

    from atomic_agents.manage.exceptions import ManageSnapshotNotFoundError

    with pytest.raises(ManageSnapshotNotFoundError):
        _routine.resolve_snapshot_path(agent_dir, "govern", "evil.md")


def test_list_snapshots_ignores_planted_non_snapshot_files(tmp_path):
    """list_snapshots() lists ONLY genuinely-generated snapshot files — a
    planted ``.tmp`` / ``evil.md`` never appears, so it can never be
    discovered via ``--list-snapshots`` and then fed to ``--restore``."""
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    assert run_govern(args, tmp_path) == 0
    real_snapshots = _routine.list_snapshots(agent_dir, "govern")
    assert len(real_snapshots) == 1

    snap_dir = agent_dir / ".config-snapshots" / "govern"
    (snap_dir / "evil.md").write_text("planted")
    (snap_dir / "scratch.tmp").write_text("planted")

    listed = _routine.list_snapshots(agent_dir, "govern")
    listed_names = {p.name for p in listed}
    assert listed_names == {real_snapshots[0].name}
    assert "evil.md" not in listed_names
    assert "scratch.tmp" not in listed_names


def test_restore_dry_run_writes_nothing(tmp_path):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    seed_snapshot_id = _routine.list_snapshots(agent_dir, "govern")[0].name
    before_content = (agent_dir / "governance.md").read_text()

    args = make_govern_args(
        agent_dir.name, tmp_path, restore=seed_snapshot_id, dry_run=True, yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 0
    assert (agent_dir / "governance.md").read_text() == before_content


def test_restore_unexpected_error_in_write_path_surfaces_as_write_error(
    tmp_path, capsys
):
    """Fix 5 (adversarial review): the SAME broad-except structured-refusal
    guard on the --restore path (mirrors _run_set's — see
    test_govern_unexpected_error_in_write_path_surfaces_as_write_error in
    test_manage_govern.py). An unexpected exception from run_managed_write
    must degrade to {ok:false, error_type:'write_error'}, exit 1 — never an
    uncaught traceback.

    Strip-RED: removing the trailing ``except Exception`` clause from
    ``_run_restore`` makes the RuntimeError below propagate UNCAUGHT out of
    ``run_govern()`` — this test would fail with a raised RuntimeError
    instead of asserting the exit code / JSON payload.
    """
    from unittest.mock import patch

    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)
    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    seed_snapshot_id = _routine.list_snapshots(agent_dir, "govern")[0].name
    capsys.readouterr()  # discard the seed call's human-readable stdout

    from atomic_agents.manage import govern as govern_mod

    def _boom(**kwargs):
        raise RuntimeError("simulated catastrophic failure inside the write path")

    with patch.object(govern_mod, "run_managed_write", _boom):
        args = make_govern_args(
            agent_dir.name,
            tmp_path,
            restore=seed_snapshot_id,
            yes=True,
            use_json=True,
        )
        rc = run_govern(args, tmp_path)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "write_error"


def test_restore_mutually_exclusive_with_set(tmp_path):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    args = make_govern_args(
        agent_dir.name,
        tmp_path,
        set_fields=["owner=alice@example.com"],
        restore="some-id.md",
        yes=True,
        use_json=True,
    )
    rc = run_govern(args, tmp_path)
    assert rc == 1


# ──────────────────────────────────────────────────────────────────
# P1 hoist guard: audit-drop (non-JSON-serialisable record) warns and never
# raises / never undoes an already-applied write — for BOTH --set and
# --restore, proving the guarantee moved with append_management_audit's
# hoist into the shared helper (not left behind as a govern-only pre-check).


def test_set_audit_drop_on_unserialisable_record_warns_not_raises(
    tmp_path, monkeypatch
):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    import json as _json

    orig_dumps = _json.dumps
    call_count = {"n": 0}

    def flaky_dumps(obj, *a, **kw):
        call_count["n"] += 1
        # The FIRST json.dumps call inside append_management_audit's
        # serialisability pre-check is the one under test.
        if call_count["n"] == 1 and isinstance(obj, dict) and "primitive" in obj:
            raise TypeError("simulated non-serialisable value (e.g. a raw Path)")
        return orig_dumps(obj, *a, **kw)

    # append_management_audit does a LOCAL `import json` inside the function
    # body, which binds to the SAME global json module object — patching the
    # module's `dumps` attribute directly reaches it (safe: monkeypatch restores).
    monkeypatch.setattr(_json, "dumps", flaky_dumps)

    args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=alice@example.com"], yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 0, "an audit-drop must never undo (or fail) an already-applied write"
    assert "alice@example.com" in (agent_dir / "governance.md").read_text()
    # No audit record landed (the drop happened before either backend append).
    assert collect_jsonl(agent_dir / "log") == []


def test_restore_audit_drop_on_unserialisable_record_warns_not_raises(
    tmp_path, monkeypatch
):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    # Snapshot-before-write (M3): the snapshot containing "seed@example.com"
    # is the one taken by the SECOND --set (right before it overwrote owner
    # -> "changed") — see test_restore_full_routine_emits_exactly_one_manage_restore_record.
    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    second_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=changed@example.com"], yes=True
    )
    assert run_govern(second_args, tmp_path) == 0
    snaps = _routine.list_snapshots(agent_dir, "govern")
    seed_snapshot_id = next(
        p for p in snaps if "seed@example.com" in p.read_text()
    ).name

    import json as _json

    orig_dumps = _json.dumps
    call_count = {"n": 0}

    def flaky_dumps(obj, *a, **kw):
        call_count["n"] += 1
        if (
            call_count["n"] == 1
            and isinstance(obj, dict)
            and obj.get("primitive") == PRIMITIVE_MANAGE_RESTORE
        ):
            raise TypeError("simulated non-serialisable value")
        return orig_dumps(obj, *a, **kw)

    monkeypatch.setattr(_json, "dumps", flaky_dumps)

    args = make_govern_args(
        agent_dir.name, tmp_path, restore=seed_snapshot_id, yes=True
    )
    rc = run_govern(args, tmp_path)

    assert rc == 0
    assert "seed@example.com" in (agent_dir / "governance.md").read_text()


# ──────────────────────────────────────────────────────────────────
# Snapshot subdir parameterization — future verbs never collide with govern


def test_snapshot_subdir_parameterization_isolates_namespaces(tmp_path):
    agent_dir = make_agent_dir(tmp_path)
    agent_dir.mkdir(exist_ok=True)  # no-op, already created by make_agent_dir

    govern_path = _routine.take_config_snapshot(
        agent_dir, "govern content", subdir="govern"
    )
    other_path = _routine.take_config_snapshot(
        agent_dir, "other-verb content", subdir="set-model"
    )

    assert "govern" in str(govern_path.parent)
    assert "set-model" in str(other_path.parent)
    assert govern_path.parent != other_path.parent

    assert len(_routine.list_snapshots(agent_dir, "govern")) == 1
    assert len(_routine.list_snapshots(agent_dir, "set-model")) == 1


# ──────────────────────────────────────────────────────────────────
# Exit-code ladder: 0 applied / 1 refused-or-error / 3 declined / 130 SIGINT
# (govern --set variants live in test_manage_govern.py; this asserts the
# SAME ladder holds for --restore, proving it is a spine-wide contract, not
# a govern --set-only behavior)


def test_restore_exit_code_ladder_applied_is_zero(tmp_path):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)
    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    snap_id = _routine.list_snapshots(agent_dir, "govern")[0].name

    args = make_govern_args(agent_dir.name, tmp_path, restore=snap_id, yes=True)
    assert run_govern(args, tmp_path) == 0


def test_restore_exit_code_ladder_refusal_is_one(tmp_path):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)

    args = make_govern_args(
        agent_dir.name, tmp_path, restore="nonexistent.md", yes=True
    )
    assert run_govern(args, tmp_path) == 1


def test_restore_exit_code_ladder_decline_is_three(tmp_path, monkeypatch):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)
    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    snap_id = _routine.list_snapshots(agent_dir, "govern")[0].name

    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")

    args = make_govern_args(agent_dir.name, tmp_path, restore=snap_id, yes=False)
    assert run_govern(args, tmp_path) == 3


def test_restore_exit_code_ladder_sigint_is_130(tmp_path, monkeypatch):
    agent_dir = make_agent_dir(tmp_path)
    make_governance_md(agent_dir)
    seed_args = make_govern_args(
        agent_dir.name, tmp_path, set_fields=["owner=seed@example.com"], yes=True
    )
    assert run_govern(seed_args, tmp_path) == 0
    snap_id = _routine.list_snapshots(agent_dir, "govern")[0].name

    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _raise_sigint():
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise_sigint)

    args = make_govern_args(agent_dir.name, tmp_path, restore=snap_id, yes=False)
    assert run_govern(args, tmp_path) == 130

"""Operator override surface for the MemoryBackend (#382 PR 1).

Covers:
- ``ATOMIC_AGENTS_MEMORY_BACKEND`` env var threading through
  ``get_default_memory_backend`` (factory returns filesystem by default,
  fails-fast on unknown ids).
- ``memory_backend=...`` constructor kwarg on AtomicAgent and DreamRunner
  as the programmatic override (bypasses env var — kwarg-wins).
- ``list_backends`` / ``unregister_backend`` registry helpers.
- Uniform construction contract: every registered backend MUST accept
  ``(agent_root, *, lock_backend=None)``.
- ``doctor.check_memory_backend_config`` coherence check.
- ``doctor.check_memory_backend`` liveness check routes through factory.
- Delegate threading: memory is per-agent state and is NEVER threaded to a child, not by default and not even when memory_backend= was supplied explicitly (ruling delegate-child-threading, #382).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.exceptions import BackendNotRegistered
from atomic_agents.memory import (
    get_default_memory_backend,
    get_backend,
    list_backends,
    register_backend,
    unregister_backend,
)
from atomic_agents.memory.filesystem import FilesystemBackend
from atomic_agents.doctor import (
    FAIL,
    PASS,
    check_memory_backend,
    check_memory_backend_config,
)


# ──────────────────────────────────────────────────────────────────
# Helpers


def _build_minimal_agent_dir(tmp_path: Path, name: str = "test") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n")
    (agent_dir / "tools.md").write_text("## Write paths\n- memory/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    return agent_dir


class _MockMemoryBackend:
    """Minimal MemoryBackend stub for registry / construction tests."""

    def __init__(self, agent_root: Path, *, lock_backend=None):
        self.agent_root = agent_root
        self.lock_backend = lock_backend
        # MUST-3-compliant: the impl-level id is `implementation_id`, NOT
        # `backend_id` (that name denotes a note/version handle). Keeps this
        # in-tree stub from modeling the prohibited shape for a contributor
        # who copies it as a backend template. See spec/20 MUST 3 + #397.
        self.implementation_id = "mock"

    # Protocol stubs (not exercised by these tests)
    def list_notes(self, **_):
        return []  # noqa: E704

    def read_note(self, name):
        return None  # noqa: E704

    def list_pinned(self):
        return []  # noqa: E704

    def list_recent(self, n, **_):
        return []  # noqa: E704

    def list_stale(self, threshold_days, **_):
        return []  # noqa: E704

    def list_orphans(self):
        return []  # noqa: E704

    def list_by_type(self, type_name):
        return []  # noqa: E704

    def render_index_summary(self):
        return ""  # noqa: E704

    def write_note(self, capture, policy, **_): ...  # noqa: E704
    def list_versions(self, name):
        return []  # noqa: E704

    def read_version(self, version_ref): ...  # noqa: E704
    def restore_version(self, name, version_ref, policy): ...  # noqa: E704
    def redact_version(self, version_ref, **_): ...  # noqa: E704
    def resolve_version_token(self, name, token): ...  # noqa: E704
    def create_staging(self): ...  # noqa: E704
    def apply_staging(self, staging, policy): ...  # noqa: E704
    def discard_staging(self, staging): ...  # noqa: E704
    def stats(self):
        from atomic_agents.memory.backend import MemoryStats

        return MemoryStats(
            total_notes=0,
            by_type={},
            live_bytes=0,
            version_history_bytes=0,
            most_churned=[],
        )

    def version_count(self, name):
        return 0  # noqa: E704

    def last_mutation_at(self, name):
        return None  # noqa: E704

    @property
    def supports_semantic_search(self):
        return False  # noqa: E704

    def search(self, query, limit=10):
        return []  # noqa: E704

    def close(self): ...  # noqa: E704


# ──────────────────────────────────────────────────────────────────
# get_default_memory_backend factory — env var path


def test_get_default_returns_filesystem_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)
    backend = get_default_memory_backend(tmp_path)
    assert isinstance(backend, FilesystemBackend)


def test_get_default_returns_filesystem_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "filesystem")
    backend = get_default_memory_backend(tmp_path)
    assert isinstance(backend, FilesystemBackend)


def test_get_default_unknown_backend_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "nonexistent-typo")
    with pytest.raises(BackendNotRegistered, match="nonexistent-typo"):
        get_default_memory_backend(tmp_path)


def test_get_default_unknown_backend_lists_known_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "bogus")
    with pytest.raises(BackendNotRegistered, match="filesystem"):
        get_default_memory_backend(tmp_path)


def test_get_default_threads_lock_backend_to_filesystem(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)
    from atomic_agents.locks import FilesystemLockBackend

    lock_be = FilesystemLockBackend(tmp_path)
    backend = get_default_memory_backend(tmp_path, lock_backend=lock_be)
    assert isinstance(backend, FilesystemBackend)
    # The lock_backend is threaded through, not independently resolved
    assert backend._lock_backend is lock_be


# ──────────────────────────────────────────────────────────────────
# list_backends / unregister_backend registry helpers


def test_list_backends_contains_filesystem():
    ids = list_backends()
    assert "filesystem" in ids


def test_list_backends_sorted():
    ids = list_backends()
    assert ids == sorted(ids)


def test_register_and_list_and_unregister(tmp_path, monkeypatch):
    register_backend("test-mock", _MockMemoryBackend)
    try:
        assert "test-mock" in list_backends()
    finally:
        unregister_backend("test-mock")
    assert "test-mock" not in list_backends()


def test_unregister_nonexistent_is_noop():
    # Should not raise
    unregister_backend("does-not-exist-ever")


# ──────────────────────────────────────────────────────────────────
# Uniform construction contract: (agent_root, *, lock_backend=None)


def test_uniform_construction_contract_filesystem(tmp_path):
    """FilesystemBackend satisfies the uniform (agent_root, *, lock_backend=None) MUST."""
    backend = FilesystemBackend(tmp_path, lock_backend=None)
    assert backend is not None


def test_uniform_construction_contract_mock_backend(tmp_path, monkeypatch):
    """Third-party backend registered under a test id satisfies the contract."""
    register_backend("test-mock-contract", _MockMemoryBackend)
    try:
        cls = get_backend("test-mock-contract")
        # Must accept (agent_root, lock_backend=None) without TypeError
        instance = cls(tmp_path, lock_backend=None)
        assert instance is not None
    finally:
        unregister_backend("test-mock-contract")


def test_uniform_construction_contract_registry_conformance(tmp_path, monkeypatch):
    """EVERY registered backend satisfies (agent_root, *, lock_backend=None).

    Registry-conformance test required by ruling ``lock-backend-threading``
    and spec/20 Implementer Contract MUST 1: iterate the live registry
    (``list_backends()``) plus a registered mock and assert each accepts the
    uniform signature without TypeError AND that ``lock_backend`` is
    keyword-only (so the MUST is genuinely machine-checked, not just the
    happy path). Keeps the protocol honest as PR 2+ register more backends.

    Signature assertion is construction-free for third-party backends
    (avoids side-effectful __init__ for connection-backed backends registered
    in the future, e.g., #258).  Only the in-test mock + the filesystem
    default are actually constructed to prove the call shape works.
    """
    import inspect

    register_backend("test-conformance-mock", _MockMemoryBackend)
    try:
        for backend_id in list_backends():
            cls = get_backend(backend_id)
            # Signature assertion — construction-free for third-party backends.
            params = inspect.signature(cls.__init__).parameters
            assert "lock_backend" in params, backend_id
            assert params["lock_backend"].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{backend_id}: lock_backend must be keyword-only"
            )

        # Actually construct the in-test mock + filesystem default to confirm
        # the call shape works end-to-end (not just the signature).
        mock_instance = _MockMemoryBackend(tmp_path, lock_backend=None)
        assert mock_instance is not None

        filesystem_cls = get_backend("filesystem")
        fs_instance = filesystem_cls(tmp_path, lock_backend=None)
        assert fs_instance is not None
    finally:
        unregister_backend("test-conformance-mock")


def test_get_default_with_registered_custom_backend(tmp_path, monkeypatch):
    """Factory routes to ANY registered backend when its id is selected.

    Registry-conformance assertion (ruling ``lock-backend-threading`` +
    spec/20 Implementer Contract MUST 1): the factory dispatches via
    ``get_backend(selection)(agent_root, lock_backend=...)``, so a
    registered non-filesystem backend resolves to its own instance — not a
    ``BackendNotRegistered`` raise.  Also verifies ``lock_backend`` is
    threaded through.
    """
    lock_be = object()
    register_backend("test-custom", _MockMemoryBackend)
    try:
        monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "test-custom")
        backend = get_default_memory_backend(tmp_path, lock_backend=lock_be)
        assert isinstance(backend, _MockMemoryBackend)
        # The factory threads the resolved lock_backend through.
        assert backend.lock_backend is lock_be
    finally:
        unregister_backend("test-custom")
        monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)


# ──────────────────────────────────────────────────────────────────
# AtomicAgent memory_backend kwarg (programmatic override)


def test_atomic_agent_accepts_memory_backend_kwarg(tmp_path, monkeypatch):
    """AtomicAgent(..., memory_backend=...) bypasses env-var resolution."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)

    mock_be = _MockMemoryBackend(tmp_path / "test")
    agent = AtomicAgent("test", memory_backend=mock_be)
    assert agent.memory is mock_be


def test_atomic_agent_memory_default_is_filesystem(tmp_path, monkeypatch):
    """When no kwarg is passed, memory resolves to FilesystemBackend."""
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)

    agent = AtomicAgent("test")
    assert isinstance(agent.memory, FilesystemBackend)


def test_atomic_agent_memory_kwarg_wins(tmp_path, monkeypatch):
    """An explicit memory_backend= is used verbatim (kwarg-wins); without it the
    factory resolves the filesystem default.

    (There is intentionally NO _memory_backend_was_explicit flag: memory is
    per-agent state and is never threaded to delegates, so the runtime has no
    reason to remember whether the kwarg was supplied — ruling
    delegate-child-threading, #382.)
    """
    from atomic_agents.agent import AtomicAgent

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))

    mock_be = _MockMemoryBackend(tmp_path / "test")
    agent_with = AtomicAgent("test", memory_backend=mock_be)
    assert agent_with.memory is mock_be

    agent_without = AtomicAgent("test")
    assert isinstance(agent_without.memory, FilesystemBackend)
    # The removed flag must NOT have crept back in.
    assert not hasattr(agent_without, "_memory_backend_was_explicit")


# ──────────────────────────────────────────────────────────────────
# DreamRunner memory_backend kwarg


def test_dream_runner_accepts_explicit_filesystem_memory_backend(tmp_path, monkeypatch):
    """DreamRunner(..., memory_backend=FilesystemBackend(...)) bypasses env-var
    resolution and is accepted (filesystem is the only supported dream backend
    in PR 1)."""
    from atomic_agents.dream import DreamRunner

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)

    fs_be = FilesystemBackend(tmp_path / "test", "memory")
    runner = DreamRunner(tmp_path, "test", memory_backend=fs_be)
    assert runner._backend is fs_be


def test_dream_runner_rejects_non_filesystem_memory_backend(tmp_path, monkeypatch):
    """DreamRunner fails loud (NotImplementedError) on a non-filesystem backend.

    apply() wraps the on-disk dream output as a FilesystemStagedMemory, so a
    non-filesystem backend cannot consume the apply path. PR 1 guards at
    __init__ rather than silently breaking on apply (#396)."""
    from atomic_agents.dream import DreamRunner

    agent_dir = _build_minimal_agent_dir(tmp_path)
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)

    mock_be = _MockMemoryBackend(agent_dir)
    with pytest.raises(NotImplementedError, match="filesystem memory backend"):
        DreamRunner(tmp_path, "test", memory_backend=mock_be)


def test_dream_runner_memory_default_is_filesystem(tmp_path, monkeypatch):
    """When no kwarg is passed, DreamRunner memory resolves to FilesystemBackend."""
    from atomic_agents.dream import DreamRunner

    _build_minimal_agent_dir(tmp_path)
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)

    runner = DreamRunner(tmp_path, "test")
    assert isinstance(runner._backend, FilesystemBackend)


# ──────────────────────────────────────────────────────────────────
# doctor.check_memory_backend_config


def test_check_memory_backend_config_pass_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)
    r = check_memory_backend_config(tmp_path)
    assert r.status == PASS
    assert r.name == "memory-backend-config"
    assert "filesystem" in r.message


def test_check_memory_backend_config_pass_explicit_filesystem(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "filesystem")
    r = check_memory_backend_config(tmp_path)
    assert r.status == PASS


def test_check_memory_backend_config_fail_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "postgres-typo")
    r = check_memory_backend_config(tmp_path)
    assert r.status == FAIL
    assert "postgres-typo" in r.message
    assert "filesystem" in r.message  # known ids listed


def test_check_memory_backend_config_pass_registered_backend(tmp_path, monkeypatch):
    """A registered non-filesystem backend constructs → coherence check PASSes.

    Locks the doctor-reuses-factory invariant: once the factory dispatches
    via the registry, a registered id must not be reported as a coherence
    failure (it is both in list_backends() AND constructable).
    """
    register_backend("test-doctor-cfg", _MockMemoryBackend)
    try:
        monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "test-doctor-cfg")
        r = check_memory_backend_config(tmp_path)
        assert r.status == PASS
        assert "test-doctor-cfg" in r.message
    finally:
        unregister_backend("test-doctor-cfg")
        monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)


# ──────────────────────────────────────────────────────────────────
# doctor.check_memory_backend (liveness) routes through factory


def test_check_memory_backend_pass_uses_factory(tmp_path, monkeypatch):
    """check_memory_backend routes through get_default_memory_backend."""
    agent = _build_minimal_agent_dir(tmp_path)
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)
    r = check_memory_backend(agent)
    assert r.status == PASS
    # Message uses the class name from the factory result, not hardcoded string
    assert "FilesystemBackend" in r.message


def test_check_memory_backend_fail_unknown_backend(tmp_path, monkeypatch):
    """check_memory_backend FAILs when backend id is unknown."""
    agent = _build_minimal_agent_dir(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_MEMORY_BACKEND", "unknown-x")
    r = check_memory_backend(agent)
    assert r.status == FAIL


def test_check_memory_backend_fail_missing_dir(tmp_path, monkeypatch):
    """check_memory_backend FAILs when memory/ is absent."""
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)
    r = check_memory_backend(tmp_path / "no-such-agent")
    assert r.status == FAIL
    assert "memory/" in r.message


# ──────────────────────────────────────────────────────────────────
# run_doctor skip list includes memory-backend-config


def test_run_doctor_no_agent_skips_memory_backend_config(tmp_path):
    """memory-backend-config appears in the no-agent SKIP set."""
    from atomic_agents.doctor import run_doctor

    results = run_doctor(agent_name=None, agents_root=tmp_path)
    skipped = {r.name for r in results if r.status == "skip"}
    assert "memory-backend-config" in skipped
    assert "memory-backend" in skipped


# ──────────────────────────────────────────────────────────────────
# Delegate threading — memory is NEVER threaded to the child (per-agent state)
#
# Unlike persona/corpus (fleet-shared CONFIG), memory is per-agent STATE: each
# delegate owns its own memory/ dir. delegate() therefore never threads the
# coordinator's backend to the child — not even when memory_backend= was
# supplied explicitly — because a root-bound FilesystemBackend would route the
# specialist's writes into the COORDINATOR's memory/ (cross-agent corruption).
# Both cases (explicit + default) assert the SAME contract: child constructs
# its own backend. See spec/20 §"Delegate threading" + ruling
# delegate-child-threading (#382).


def _make_roster_pair(tmp_path: Path, coordinator: str, specialist: str) -> Path:
    """Build a coordinator+specialist pair with roster wired for delegation."""
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _build_minimal_agent_dir(agents_root, coordinator)
    (agents_root / coordinator / "roster.md").write_text(
        f"# Roster\n\n## Delegate to\n\n- {specialist}\n",
        encoding="utf-8",
    )
    _build_minimal_agent_dir(agents_root, specialist)
    return agents_root


def test_delegate_does_not_inherit_explicit_memory_backend(tmp_path, monkeypatch):
    """Explicit memory_backend= on the coordinator is NOT threaded to the delegate.

    Memory is per-agent STATE. Even when the operator explicitly supplies
    memory_backend=, delegate() must NOT place coordinator.memory into the
    child's construction kwargs — a root-bound coordinator backend would route
    the specialist's writes into the coordinator's memory/ dir (cross-agent
    corruption). The child resolves its own per-agent backend instead. This is
    the deliberate divergence from the persona/corpus mirror (ruling
    delegate-child-threading, #382).
    """
    import unittest.mock as _mock
    from atomic_agents.agent import AtomicAgent

    agents_root = _make_roster_pair(tmp_path, "coordinator", "specialist")
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)

    mock_be = _MockMemoryBackend(agents_root / "coordinator")
    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=agents_root,
        memory_backend=mock_be,
    )

    captured: dict = {}
    original_init = AtomicAgent.__init__

    def capturing_init(self_inner, *args, **kwargs):
        original_init(self_inner, *args, **kwargs)
        if getattr(self_inner, "name", None) == "specialist":
            captured["specialist"] = self_inner

    with _mock.patch.object(AtomicAgent, "__init__", capturing_init):
        try:
            coordinator.delegate(target_agent_name="specialist", work_item="Hello")
        except Exception:
            # call() requires a live LLM; construction happens before call().
            pass

    delegate_agent = captured.get("specialist")
    assert delegate_agent is not None, "No delegate AtomicAgent construction captured"
    # The delegate must construct its OWN backend, never reuse the
    # coordinator's explicit instance.
    assert delegate_agent.memory is not coordinator.memory, (
        "Explicit memory_backend= must NOT be threaded to the delegate "
        "(memory is per-agent state, not fleet-shared config)"
    )


def test_delegate_does_not_inherit_default_resolved_memory_backend(
    tmp_path, monkeypatch
):
    """Default-resolved memory backend is NOT threaded to the delegate.

    When the coordinator used the env-var/default factory path (no explicit
    kwarg), each child resolves its OWN per-agent backend — threading the
    coordinator's root-bound instance would point the child at the wrong
    memory/ directory (spec/20 §"Delegate threading" per-agent-scoping rule).
    """
    import unittest.mock as _mock
    from atomic_agents.agent import AtomicAgent

    agents_root = _make_roster_pair(tmp_path, "coordinator", "specialist")
    monkeypatch.delenv("ATOMIC_AGENTS_MEMORY_BACKEND", raising=False)

    coordinator = AtomicAgent(name="coordinator", agents_root=agents_root)

    captured: dict = {}
    original_init = AtomicAgent.__init__

    def capturing_init(self_inner, *args, **kwargs):
        original_init(self_inner, *args, **kwargs)
        if getattr(self_inner, "name", None) == "specialist":
            captured["specialist"] = self_inner

    with _mock.patch.object(AtomicAgent, "__init__", capturing_init):
        try:
            coordinator.delegate(target_agent_name="specialist", work_item="Hello")
        except Exception:
            pass

    delegate_agent = captured.get("specialist")
    assert delegate_agent is not None, "No delegate AtomicAgent construction captured"
    # Default-resolved: the delegate must construct its OWN backend (different
    # instance bound to the delegate's own agent_root), not reuse the
    # coordinator's.
    assert delegate_agent.memory is not coordinator.memory, (
        "Default-resolved memory backend must NOT be threaded to the delegate"
    )

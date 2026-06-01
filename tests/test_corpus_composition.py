"""CorpusBackend flag-tracking and delegate threading tests (#65 PR 3).

Tests 1-4 cover _corpus_backend_was_explicit flag tracking + delegate()
explicit-only threading semantics (D-ER-2 corollary for corpus, mirroring the
PersonaBackend pattern at agent.py:443-465 and test_persona_composition.py:702-728).
"""

from __future__ import annotations

import unittest.mock as _mock
from pathlib import Path


from atomic_agents import AtomicAgent
from atomic_agents.corpus import FilesystemCorpusBackend


# ─────────────────────────────────────────────────────────────────────────────
# Helpers


def _create_agent_on_disk(agents_root: Path, agent_name: str) -> Path:
    """Create a minimal agent directory with the legacy three-file persona layout."""
    agent_root = agents_root / agent_name
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "IDENTITY.md").write_text(
        "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    (persona_dir / "SOUL.md").write_text("Curious, direct, honest.", encoding="utf-8")
    (persona_dir / "USER.md").write_text("User is a developer.", encoding="utf-8")
    (agent_root / "memory").mkdir(exist_ok=True)
    return agent_root


def _make_roster_pair(
    agents_root: Path,
    coordinator_name: str,
    specialist_name: str,
) -> None:
    """Create coordinator + specialist pair with roster wired for delegation."""
    coordinator_root = _create_agent_on_disk(agents_root, coordinator_name)
    (coordinator_root / "roster.md").write_text(
        f"# Roster\n\n## Delegate to\n\n- {specialist_name}\n",
        encoding="utf-8",
    )
    _create_agent_on_disk(agents_root, specialist_name)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: flag is False after default resolution


def test_corpus_backend_was_explicit_false_after_default_resolve(
    tmp_path: Path,
) -> None:
    """_corpus_backend_was_explicit is False when no kwarg supplied (#65 PR 3)."""
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _create_agent_on_disk(agents_root, "scout")

    agent = AtomicAgent(name="scout", agents_root=agents_root)

    assert agent._corpus_backend_was_explicit is False
    assert agent.corpus_backend is not None


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: flag is True after explicit kwarg


def test_corpus_backend_was_explicit_true_after_explicit_kwarg(
    tmp_path: Path,
) -> None:
    """_corpus_backend_was_explicit is True when corpus_backend= kwarg supplied (#65 PR 3)."""
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    agent_root = _create_agent_on_disk(agents_root, "scout")

    explicit_backend = FilesystemCorpusBackend(agent_root)
    agent = AtomicAgent(
        name="scout",
        agents_root=agents_root,
        corpus_backend=explicit_backend,
    )

    assert agent._corpus_backend_was_explicit is True
    assert agent.corpus_backend is explicit_backend


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: default-resolved corpus_backend is NOT threaded to delegate


def test_delegate_does_not_thread_default_resolved_corpus_backend(
    tmp_path: Path,
) -> None:
    """Delegate constructs its own default corpus_backend when coordinator used default.

    D-ER-2 corollary for corpus (#65 PR 3): when the operator did NOT pass
    corpus_backend= the coordinator's default-resolved instance must NOT be
    forwarded to the delegate. Each agent should resolve its own backend at its
    own agent_root scope.
    """
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _make_roster_pair(agents_root, "coordinator", "specialist")

    coordinator = AtomicAgent(name="coordinator", agents_root=agents_root)

    captured: dict = {}
    original_init = AtomicAgent.__init__

    def capturing_init(self_inner, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_init(self_inner, *args, **kwargs)
        if getattr(self_inner, "name", None) == "specialist":
            captured["specialist"] = self_inner

    with _mock.patch.object(AtomicAgent, "__init__", capturing_init):
        try:
            coordinator.delegate(
                target_agent_name="specialist",
                work_item="Hello",
            )
        except Exception:
            pass

    delegate_agent = captured.get("specialist")
    assert delegate_agent is not None, "No delegate AtomicAgent construction captured"
    assert delegate_agent.corpus_backend is not coordinator.corpus_backend, (
        "Default-resolved corpus_backend must NOT be threaded to delegate (D-ER-2 corollary) -- "
        "delegate and coordinator share the same instance, indicating threading occurred"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: explicit corpus_backend IS threaded to delegate


def test_delegate_threads_explicit_corpus_backend(
    tmp_path: Path,
) -> None:
    """Delegate inherits coordinator's corpus_backend when explicitly supplied.

    D-ER-2 corollary for corpus (#65 PR 3): when the operator explicitly
    passes corpus_backend= to the coordinator, the same backend instance must
    be forwarded to the delegate so a shared corpus (e.g., SQLiteCorpusBackend
    over a fleet DB) reaches into delegated agents consistently.
    """
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    coordinator_root = _create_agent_on_disk(agents_root, "coordinator")
    _make_roster_pair(agents_root, "coordinator", "specialist")

    explicit_backend = FilesystemCorpusBackend(coordinator_root)
    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=agents_root,
        corpus_backend=explicit_backend,
    )

    captured: dict = {}
    original_init = AtomicAgent.__init__

    def capturing_init(self_inner, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_init(self_inner, *args, **kwargs)
        if getattr(self_inner, "name", None) == "specialist":
            captured["specialist"] = self_inner

    with _mock.patch.object(AtomicAgent, "__init__", capturing_init):
        try:
            coordinator.delegate(
                target_agent_name="specialist",
                work_item="Hello",
            )
        except Exception:
            pass

    delegate_agent = captured.get("specialist")
    assert delegate_agent is not None, "No delegate AtomicAgent construction captured"
    assert delegate_agent.corpus_backend is explicit_backend, (
        "Explicit corpus_backend must be threaded to delegate (D-ER-2 corollary) -- "
        "delegate did not receive the coordinator's explicit backend instance"
    )

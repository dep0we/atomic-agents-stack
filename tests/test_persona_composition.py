"""Cross-Protocol composition tests for PersonaBackend + AgentProfileBackend.

PR 2 of #62 — D-ER-5 lock: 15-20 cross-Protocol composition tests covering
AgentProfile load_profile / save_profile PersonaBackend ownership; bootstrap
sequence persona repopulation and agent_mode re-derivation; per-runner kwarg
threading; and delegate.py D-ER-2 explicit-only threading semantics.

These tests instantiate a real AtomicAgent against a tmp_path agents_root and
exercise the full bootstrap path.  FilesystemPersonaBackend (default) is used
for disk-based tests; MockPersonaBackend (in-memory) is imported from the
conformance suite where threading-only tests do not need I/O.

Test categories
---------------
Bootstrap path — load_profile composition       tests 1-6
save_profile composition                        tests 7-10
Per-runner kwargs                               tests 11-13
delegate.py threading (D-ER-2)                  tests 14-17
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Re-use MockPersonaBackend from the conformance suite (it is importable;
# the conformance module does not auto-register at import — only the
# ``mock_registered`` fixture registers it).
from tests.test_persona_protocol_conformance import MockPersonaBackend

from atomic_agents import AtomicAgent
from atomic_agents.exceptions import PersonaNotFound, PersonaOwnershipConflict
from atomic_agents.persona.filesystem import FilesystemPersonaBackend
from atomic_agents.persona.types import Persona
from atomic_agents.profile import (
    FilesystemAgentProfileBackend,
    SQLiteAgentProfileBackend,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_persona(
    identity: str = "You are a helpful assistant.",
    soul: str = "Curious, direct, honest.",
    user: str = "User is a developer.",
    version: int = 1,
    created_at: str = "2026-05-26T12:00:00Z",
    label: str | None = None,
) -> Persona:
    """Create a Persona dataclass with sensible defaults."""
    return Persona(
        identity=identity,
        soul=soul,
        user=user,
        version=version,
        created_at=created_at,
        label=label,
    )


def _goal_driven_identity() -> str:
    """Return IDENTITY.md text that triggers goal-driven agent_mode derivation."""
    return "# Scout\n\n## Operating mode\n\nThis agent is goal-driven.\n"


def _reactive_identity() -> str:
    """Return IDENTITY.md text that triggers reactive agent_mode derivation."""
    return "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n"


def _create_persona_on_disk(
    personas_root: Path,
    persona_id: str,
    identity: str = "You are a helpful assistant.",
    soul: str = "Curious, direct, honest.",
    user: str = "User is a developer.",
) -> None:
    """Write IDENTITY/SOUL/USER.md + metadata.json under <personas_root>/<persona_id>/."""
    persona_dir = personas_root / persona_id
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "IDENTITY.md").write_text(identity, encoding="utf-8")
    (persona_dir / "SOUL.md").write_text(soul, encoding="utf-8")
    (persona_dir / "USER.md").write_text(user, encoding="utf-8")
    meta = {
        "schema_version": 1,
        "version": 1,
        "label": None,
        "created_at": "2026-05-26T12:00:00Z",
    }
    (persona_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def _create_agent_with_link(
    agents_root: Path,
    agent_name: str,
    persona_id: str,
) -> Path:
    """Create an agent with persona.link.md pointing at persona_id.

    Uses ``FilesystemAgentProfileBackend.set_persona_ownership`` via the
    profile backend Protocol surface so the link file is written in the
    canonical YAML-in-code-block format.  The agent directory is bootstrapped
    with the minimum non-persona config files first so the backend accepts the
    agent as valid.
    """
    agent_root = agents_root / agent_name
    agent_root.mkdir(parents=True, exist_ok=True)
    # Write persona.link.md directly — we cannot call set_persona_ownership
    # before IDENTITY.md or link.md exists (the method requires an existing
    # agent dir sentinel).  Writing the link file first satisfies _is_agent_dir.
    link_body = (
        f"# Persona link\n\n```yaml\nkind: shared\npersona_id: {persona_id}\n```\n"
    )
    (agent_root / "persona.link.md").write_text(link_body, encoding="utf-8")
    (agent_root / "memory").mkdir(exist_ok=True)
    return agent_root


def _create_legacy_agent(
    agents_root: Path,
    agent_name: str,
    identity: str | None = None,
    soul: str = "Curious, direct, honest.",
    user: str = "User is a developer.",
) -> Path:
    """Create an agent with the legacy three-file persona layout."""
    agent_root = agents_root / agent_name
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "IDENTITY.md").write_text(
        identity or _reactive_identity(), encoding="utf-8"
    )
    (persona_dir / "SOUL.md").write_text(soul, encoding="utf-8")
    (persona_dir / "USER.md").write_text(user, encoding="utf-8")
    (agent_root / "memory").mkdir(exist_ok=True)
    return agent_root


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap path — load_profile composition (tests 1-6)


def test_shared_persona_identity_read_from_persona_backend(tmp_path: Path) -> None:
    """AtomicAgent with persona.link.md reads identity from PersonaBackend.

    D-PP-4 bootstrap: load_profile returns empty persona fields when
    persona.link.md is present; the bootstrap sequence repopulates them
    from PersonaBackend.load_persona before system-prompt assembly.
    """
    personas_root = tmp_path / ".personas"
    agents_root = tmp_path / "agents"
    agents_root.mkdir()

    persona_id = "custom-v1"
    identity_text = "You are a custom persona. Curious and bold."
    _create_persona_on_disk(personas_root, persona_id, identity=identity_text)
    _create_agent_with_link(agents_root, "test-agent", persona_id)

    persona_backend = FilesystemPersonaBackend(personas_root)
    agent = AtomicAgent(
        name="test-agent",
        agents_root=agents_root,
        persona_backend=persona_backend,
    )

    assert agent._profile.persona_identity == identity_text


def test_legacy_agent_reads_identity_from_three_files(tmp_path: Path) -> None:
    """Agent without persona.link.md uses legacy three-file behavior.

    external_persona_ref returns None; persona_identity matches IDENTITY.md.
    """
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    identity_text = "# Legacy\n\n## Operating mode\n\nThis agent is reactive.\n"
    _create_legacy_agent(agents_root, "legacy-agent", identity=identity_text)

    agent = AtomicAgent(name="legacy-agent", agents_root=agents_root)

    assert agent._profile.persona_identity == identity_text

    # Verify ownership: external_persona_ref returns None for legacy layout.
    profile_backend = FilesystemAgentProfileBackend(agents_root)
    assert profile_backend.external_persona_ref("legacy-agent") is None


def test_persona_ownership_conflict_raised_at_construction(tmp_path: Path) -> None:
    """Both persona.link.md AND persona/IDENTITY.md → PersonaOwnershipConflict.

    D2a: the two layouts are mutually exclusive. The framework refuses to
    guess which one wins and surfaces the conflict at load_profile time.
    """
    agents_root = tmp_path / "agents"
    personas_root = tmp_path / ".personas"
    agents_root.mkdir()

    persona_id = "conflict-persona"
    _create_persona_on_disk(personas_root, persona_id)
    agent_root = _create_agent_with_link(agents_root, "conflict-agent", persona_id)

    # Also write persona/IDENTITY.md to create the conflict.
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "IDENTITY.md").write_text(_reactive_identity(), encoding="utf-8")

    persona_backend = FilesystemPersonaBackend(personas_root)
    with pytest.raises(PersonaOwnershipConflict, match="mutually exclusive"):
        AtomicAgent(
            name="conflict-agent",
            agents_root=agents_root,
            persona_backend=persona_backend,
        )


def test_persona_not_found_raised_for_unknown_persona_id(tmp_path: Path) -> None:
    """persona.link.md pointing at a non-existent persona_id → PersonaNotFound.

    The personas_root directory exists but contains no matching record.
    """
    agents_root = tmp_path / "agents"
    personas_root = tmp_path / ".personas"
    agents_root.mkdir()
    personas_root.mkdir()

    _create_agent_with_link(agents_root, "lost-agent", "nonexistent-persona-id")

    persona_backend = FilesystemPersonaBackend(personas_root)
    with pytest.raises(PersonaNotFound):
        AtomicAgent(
            name="lost-agent",
            agents_root=agents_root,
            persona_backend=persona_backend,
        )


def test_agent_mode_re_derived_from_persona_backend_identity(tmp_path: Path) -> None:
    """agent_mode is re-derived from PersonaBackend's identity after repopulation.

    D-PP-4: load_profile defaults agent_mode to 'reactive' when persona fields
    are empty (externally owned). The bootstrap sequence reads the real identity
    from PersonaBackend and calls parse_agent_mode_text() to re-derive the mode.
    A 'goal-driven' identity text must yield agent_mode == 'goal-driven'.
    """
    agents_root = tmp_path / "agents"
    personas_root = tmp_path / ".personas"
    agents_root.mkdir()

    persona_id = "goal-persona"
    _create_persona_on_disk(personas_root, persona_id, identity=_goal_driven_identity())
    _create_agent_with_link(agents_root, "goal-agent", persona_id)

    persona_backend = FilesystemPersonaBackend(personas_root)
    agent = AtomicAgent(
        name="goal-agent",
        agents_root=agents_root,
        persona_backend=persona_backend,
    )

    assert agent._profile.agent_mode == "goal-driven"


def test_agent_mode_flips_after_persona_backend_mutation(tmp_path: Path) -> None:
    """Fresh AtomicAgent construction after persona mutation sees new mode.

    Mutate the persona record from 'reactive' to 'goal-driven' via
    PersonaBackend.save_persona(overwrite=True).  A freshly-constructed
    AtomicAgent picks up the updated identity and re-derives the new mode.
    """
    agents_root = tmp_path / "agents"
    personas_root = tmp_path / ".personas"
    agents_root.mkdir()

    persona_id = "mutable-persona"
    _create_persona_on_disk(personas_root, persona_id, identity=_reactive_identity())
    _create_agent_with_link(agents_root, "mutable-agent", persona_id)

    persona_backend = FilesystemPersonaBackend(personas_root)

    # First construction: reactive mode.
    agent1 = AtomicAgent(
        name="mutable-agent",
        agents_root=agents_root,
        persona_backend=persona_backend,
    )
    assert agent1._profile.agent_mode == "reactive"

    # Mutate the persona to goal-driven.
    updated_persona = _make_persona(identity=_goal_driven_identity())
    persona_backend.save_persona(persona_id, updated_persona, overwrite=True)

    # Second construction: mode must now be goal-driven.
    agent2 = AtomicAgent(
        name="mutable-agent",
        agents_root=agents_root,
        persona_backend=persona_backend,
    )
    assert agent2._profile.agent_mode == "goal-driven"


# ─────────────────────────────────────────────────────────────────────────────
# save_profile composition (tests 7-10)


def test_save_profile_drops_persona_fields_when_externally_owned_filesystem(
    tmp_path: Path,
) -> None:
    """save_profile does NOT write IDENTITY/SOUL/USER.md when persona.link.md present.

    D6 + D-PP-8: the filesystem backend skips persona writes when the agent is
    externally owned.  Mutating the in-memory profile's persona_identity and
    calling save_profile must leave the agent directory free of legacy persona
    files.
    """
    agents_root = tmp_path / "agents"
    personas_root = tmp_path / ".personas"
    agents_root.mkdir()

    persona_id = "shared-v1"
    _create_persona_on_disk(personas_root, persona_id)
    agent_root = _create_agent_with_link(agents_root, "owned-agent", persona_id)

    persona_backend = FilesystemPersonaBackend(personas_root)
    agent = AtomicAgent(
        name="owned-agent",
        agents_root=agents_root,
        persona_backend=persona_backend,
    )

    # Mutate in-memory persona fields to simulate an attempted write.
    dirty_profile = agent._profile.replace(
        persona_identity="WRONG — should not land on disk",
        persona_soul="WRONG SOUL",
        persona_user="WRONG USER",
    )

    profile_backend = FilesystemAgentProfileBackend(agents_root)
    profile_backend.save_profile("owned-agent", dirty_profile)

    # Verify: no IDENTITY.md/SOUL.md/USER.md under the agent root.
    assert not (agent_root / "persona" / "IDENTITY.md").exists()
    assert not (agent_root / "persona" / "SOUL.md").exists()
    assert not (agent_root / "persona" / "USER.md").exists()


def test_save_profile_drops_persona_fields_sqlite_when_persona_id_nonnull(
    tmp_path: Path,
) -> None:
    """SQLite save_profile drops inline persona fields when persona_id is non-NULL.

    D-PP-8: the SQLite backend silently drops persona_identity/soul/user on
    save when the row's persona_id column is non-NULL.  Re-loading via
    load_profile must show empty persona fields (the SQLite backend does NOT
    repopulate from PersonaBackend — that is the framework bootstrap layer's job).
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        sqlite_backend = SQLiteAgentProfileBackend(":memory:")

    # Create agent row via a legacy save (no persona_id set yet).
    from atomic_agents.profile.types import AgentProfile

    blank_profile = AgentProfile(
        name="sqlite-agent",
        agent_mode="reactive",
        model_config={},
        tool_config={},
        tool_classifications={},
        judges_config=None,
        roster=[],
        mcp_servers=[],
        persona_identity="Initial identity text",
        persona_soul="Initial soul",
        persona_user="Initial user",
        goal_text="",
        model_md_raw="",
        tools_md_raw="",
        judges_md_raw=None,
        roster_md_raw="",
        mcp_md_raw="",
    )
    sqlite_backend.save_profile("sqlite-agent", blank_profile)

    # Now flag the agent as externally owned.
    sqlite_backend.set_persona_ownership("sqlite-agent", "custom-v1")

    # Save again with persona fields populated — they should be dropped.
    dirty_profile = blank_profile.replace(
        persona_identity="WRONG — should be dropped",
        persona_soul="WRONG SOUL",
        persona_user="WRONG USER",
    )
    sqlite_backend.save_profile("sqlite-agent", dirty_profile)

    # Re-load and verify persona fields are empty after the silent drop.
    reloaded = sqlite_backend.load_profile("sqlite-agent")
    assert reloaded.persona_identity == ""
    assert reloaded.persona_soul == ""
    assert reloaded.persona_user == ""


def test_save_profile_sqlite_emits_one_time_warning_on_persona_drop(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SQLite save_profile emits exactly ONE warning per agent on persona field drop.

    D-PP-8: the warning is emitted at most once per agent per backend instance
    (tracked in ``_warned_drop_agents``).  Calling save_profile twice for the
    same externally-owned agent produces only one log line.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        sqlite_backend = SQLiteAgentProfileBackend(":memory:")

    from atomic_agents.profile.types import AgentProfile

    base_profile = AgentProfile(
        name="warn-agent",
        agent_mode="reactive",
        model_config={},
        tool_config={},
        tool_classifications={},
        judges_config=None,
        roster=[],
        mcp_servers=[],
        persona_identity="Identity text",
        persona_soul="Soul text",
        persona_user="User text",
        goal_text="",
        model_md_raw="",
        tools_md_raw="",
        judges_md_raw=None,
        roster_md_raw="",
        mcp_md_raw="",
    )
    sqlite_backend.save_profile("warn-agent", base_profile)
    sqlite_backend.set_persona_ownership("warn-agent", "some-persona")

    with caplog.at_level(logging.WARNING, logger="atomic_agents.profile.sqlite"):
        sqlite_backend.save_profile("warn-agent", base_profile)
        sqlite_backend.save_profile("warn-agent", base_profile)

    drop_events = [
        r
        for r in caplog.records
        if "agent_profile_save_dropped_persona_fields" in r.message
        and "warn-agent" in r.message
    ]
    assert len(drop_events) == 1, (
        f"Expected exactly 1 warning event per agent; got {len(drop_events)}"
    )


def test_save_profile_preserves_non_persona_fields_when_externally_owned(
    tmp_path: Path,
) -> None:
    """save_profile persists non-persona fields even when persona is externally owned.

    D6 scope: only persona_identity/soul/user are dropped.  goal_text, model_md_raw,
    tools_md_raw, and similar fields are written normally.
    """
    agents_root = tmp_path / "agents"
    personas_root = tmp_path / ".personas"
    agents_root.mkdir()

    persona_id = "shared-v2"
    _create_persona_on_disk(personas_root, persona_id)
    _create_agent_with_link(agents_root, "partial-agent", persona_id)

    persona_backend = FilesystemPersonaBackend(personas_root)
    agent = AtomicAgent(
        name="partial-agent",
        agents_root=agents_root,
        persona_backend=persona_backend,
    )

    # Modify a non-persona field.
    new_goal = "Finish the quarterly report by Friday."
    updated_profile = agent._profile.replace(goal_text=new_goal)

    profile_backend = FilesystemAgentProfileBackend(agents_root)
    profile_backend.save_profile("partial-agent", updated_profile)

    # Reload and verify non-persona field persisted.
    reloaded = profile_backend.load_profile("partial-agent")
    assert reloaded.goal_text == new_goal

    # Persona files must still be absent (ownership is external).
    agent_root = agents_root / "partial-agent"
    assert not (agent_root / "persona" / "IDENTITY.md").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Per-runner kwargs (tests 11-13)


def test_outcome_runner_threads_persona_backend_kwarg(tmp_path: Path) -> None:
    """OutcomeRunner(persona_backend=...) stores the kwarg on _persona_backend.

    The runner stores the kwarg and passes it to the internal AtomicAgent
    at construction time.  Verify the stored reference is the same object.
    """
    from atomic_agents.outcome import OutcomeRunner

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _create_legacy_agent(agents_root, "scout")

    mock_backend = MockPersonaBackend()
    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name="scout",
        persona_backend=mock_backend,
    )
    assert runner._persona_backend is mock_backend


def test_eval_runner_threads_persona_backend_kwarg(tmp_path: Path) -> None:
    """EvalRunner(persona_backend=...) stores the kwarg on _persona_backend."""
    from atomic_agents.eval import EvalRunner

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _create_legacy_agent(agents_root, "scout")
    (agents_root / "scout" / "evals").mkdir()
    (agents_root / "scout" / "evals" / "rubric.md").write_text(
        "---\nweights:\n  correctness: 1.0\nthreshold: 0.5\n---\n# Rubric\n",
        encoding="utf-8",
    )
    (agents_root / "scout" / "evals" / "judge.md").write_text(
        "# Judge prompt\n", encoding="utf-8"
    )
    (agents_root / "scout" / "evals" / "golden").mkdir()

    mock_backend = MockPersonaBackend()
    runner = EvalRunner(
        agents_root=agents_root,
        agent_name="scout",
        persona_backend=mock_backend,
    )
    assert runner._persona_backend is mock_backend


def test_dream_runner_threads_persona_backend_kwarg(tmp_path: Path) -> None:
    """DreamRunner(persona_backend=...) stores the kwarg on _persona_backend."""
    from atomic_agents.dream import DreamRunner

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _create_legacy_agent(agents_root, "scout")

    # DreamRunner requires the agent_root to exist; memory dir is enough.
    mock_backend = MockPersonaBackend()
    runner = DreamRunner(
        agents_root=agents_root,
        agent_name="scout",
        persona_backend=mock_backend,
    )
    assert runner._persona_backend is mock_backend


# ─────────────────────────────────────────────────────────────────────────────
# delegate.py threading — D-ER-2 (tests 14-17)


def _make_roster_agent(
    agents_root: Path,
    coordinator_name: str,
    specialist_name: str,
) -> None:
    """Create coordinator + specialist pair with roster wired for delegation.

    Both agents use the legacy three-file persona layout.
    """
    coordinator_root = _create_legacy_agent(agents_root, coordinator_name)
    # Write roster.md using the "## Delegate to" format the roster parser requires.
    (coordinator_root / "roster.md").write_text(
        f"# Roster\n\n## Delegate to\n\n- {specialist_name}\n",
        encoding="utf-8",
    )
    _create_legacy_agent(agents_root, specialist_name)


def test_delegate_inherits_coordinator_persona_backend_when_explicit(
    tmp_path: Path,
) -> None:
    """Delegate inherits coordinator's PersonaBackend when explicitly supplied.

    D-ER-2: when the operator explicitly passes persona_backend= to the
    coordinator, the same backend instance is threaded to the delegate
    AtomicAgent so fleet-shared backends (e.g., DatabasePersonaBackend)
    reach into delegated agents.

    Mirrors the established pattern from test_profile_integration.py:
    capture the constructed delegate instance (not kwargs) after
    original_init completes, then inspect the attribute.
    """
    import unittest.mock as _mock

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _make_roster_agent(agents_root, "coordinator", "specialist")

    mock_backend = MockPersonaBackend()
    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=agents_root,
        persona_backend=mock_backend,
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
            # call() requires a live LLM; construction happens before call().
            pass

    delegate_agent = captured.get("specialist")
    assert delegate_agent is not None, "No delegate AtomicAgent construction captured"
    assert delegate_agent.persona_backend is mock_backend, (
        "Delegate did not inherit coordinator's explicit persona_backend (D-ER-2)"
    )


def test_delegate_does_not_inherit_default_resolved_persona_backend(
    tmp_path: Path,
) -> None:
    """Delegate constructs its own default backend when coordinator used default.

    D-ER-2: when the operator did NOT supply persona_backend= (default-resolved
    via get_default_persona_backend), the coordinator's instance must NOT be
    threaded to the delegate.  Cross-vault delegation uses the delegate's own
    agents_root scope, so inheriting the coordinator's filesystem path would
    silently point at the wrong .personas/ directory.

    We verify that the delegate's backend is NOT the same object as the
    coordinator's (different instances confirm independent default resolution).
    """
    import unittest.mock as _mock

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _make_roster_agent(agents_root, "coordinator", "specialist")

    # Construct coordinator WITHOUT explicit persona_backend.
    coordinator = AtomicAgent(
        name="coordinator",
        agents_root=agents_root,
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
    # Default-resolved backends: delegate must use its OWN default (different instance).
    assert delegate_agent.persona_backend is not coordinator.persona_backend, (
        "Default-resolved persona_backend must NOT be threaded to delegate (D-ER-2) — "
        "delegate and coordinator have the same instance, indicating threading occurred"
    )


def test_persona_backend_was_explicit_false_after_default_resolve(
    tmp_path: Path,
) -> None:
    """_persona_backend_was_explicit is False when no kwarg supplied."""
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _create_legacy_agent(agents_root, "scout")

    agent = AtomicAgent(name="scout", agents_root=agents_root)
    assert agent._persona_backend_was_explicit is False


def test_persona_backend_was_explicit_true_after_explicit_kwarg(
    tmp_path: Path,
) -> None:
    """_persona_backend_was_explicit is True when kwarg explicitly supplied."""
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    _create_legacy_agent(agents_root, "scout")

    mock_backend = MockPersonaBackend()
    agent = AtomicAgent(
        name="scout",
        agents_root=agents_root,
        persona_backend=mock_backend,
    )
    assert agent._persona_backend_was_explicit is True


# ──────────────────────────────────────────────────────────────────
# P2-3 regression: PersonaNotFound from bootstrap includes agent_id context


def test_persona_not_found_message_includes_agent_name(tmp_path: Path) -> None:
    """P2-3 regression: when persona.link.md references a missing persona record,
    the PersonaNotFound exception message must include the agent's name so the
    operator can identify which agent had the broken link without grepping every
    persona.link.md on disk.
    """
    agents_root = tmp_path / "agents"
    personas_root = tmp_path / ".personas"
    agents_root.mkdir()
    personas_root.mkdir()

    # Create an agent whose link points at a persona_id that does not exist.
    _create_agent_with_link(agents_root, "broken-link-agent", "nonexistent-persona-xyz")

    persona_backend = FilesystemPersonaBackend(personas_root)
    with pytest.raises(PersonaNotFound) as exc_info:
        AtomicAgent(
            name="broken-link-agent",
            agents_root=agents_root,
            persona_backend=persona_backend,
        )

    message = str(exc_info.value)
    assert "broken-link-agent" in message, (
        f"Expected agent name 'broken-link-agent' in PersonaNotFound message; got: {message!r}"
    )
